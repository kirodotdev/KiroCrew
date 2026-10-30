"""Dev Fleet mutation guards: audit, pod identity, credential-free env, git and reaping.

Covers the SEL audit on mutations, the escalation-cleanup symlink guard, the
cross-repo pod identity guard, the credential-free build environment, the git
config/protocol neutralizers, and process reaping on cancellation and overrun.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew import platform_compat
from kiro_crew.apps.builtins.dev_fleet import fleet_state as fleet_state_mod
from kiro_crew.apps.builtins.dev_fleet import repository as repository_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.apps.builtins.dev_fleet import worktree_ops as worktree_ops_mod
from kiro_crew.testing.wait import async_wait_until, await_until


# --- SEL audit on mutations (Codex R17) ---
async def _run_settled(rid: str) -> None:
    """Wait for run *rid* to leave ``running``; a run that never does fails by name."""

    async def _settled() -> bool:
        async with mod._RUNS_LOCK:
            return mod._RUNS.get(rid, {}).get("status") != "running"

    await async_wait_until(_settled, describe=lambda: f"run {rid!r} is still running")


def _fake_sel_capture(events):
    class _FakeSel:
        def log_tool_invocation(self, **kw):
            events.append(kw)

    return lambda: _FakeSel()


@pytest.mark.asyncio
async def test_mutation_denied_emits_sel_event(monkeypatch):
    """A rejected worktree remove emits exactly one SEL event with outcome=denied."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "nope"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "nope"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with patch(
        "kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names",
        new_callable=AsyncMock,
        return_value={"other"},
    ):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    assert len(events) == 1
    ev = events[0]
    assert ev["outcome"] == "denied"
    assert ev["tool_name"] == "dev_fleet_worktree_remove"
    assert ev["resources"] == "nope"


@pytest.mark.asyncio
async def test_mutation_success_emits_sel_event(monkeypatch):
    """A successful pod action emits exactly one SEL event with outcome=success."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_pod_down

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "feature-x"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "feature-x"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with (
        patch(
            "kiro_crew.apps.builtins.dev_fleet.repository._find_worktree",
            new_callable=AsyncMock,
            return_value=({"name": "feature-x"}, None),
        ),
        patch(
            "kiro_crew.apps.builtins.dev_fleet.worktree_ops._pod_down",
            new_callable=AsyncMock,
            return_value={"ok": True},
        ),
    ):
        resp = await api_dev_fleet_pod_down(request)
    assert resp.status == 200
    assert len(events) == 1
    assert events[0]["outcome"] == "success"
    assert events[0]["tool_name"] == "dev_fleet_pod_down"
    assert events[0]["resources"] == "feature-x"


@pytest.mark.asyncio
async def test_worktree_remove_non_string_name_is_400(monkeypatch):
    """A list-valued 'name' must be a 400, not a TypeError->500 (Codex R17 #2)."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture([])())

    payload = json.dumps({"name": ["feature"]}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": ["feature"]}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 400
    body = json.loads(resp.body)
    assert "non-empty string" in body["error"]


@pytest.mark.asyncio
async def test_mutation_ok_false_audited_as_denied(monkeypatch):
    """A refused operation reported as {"ok": false} with HTTP 200 must be
    audited as denied, never success (Codex R18 #1)."""
    from kiro_crew.apps.builtins.dev_fleet.server import api_dev_fleet_worktree_remove

    events: list = []
    monkeypatch.setattr(runtime_mod, "_sel", lambda: _fake_sel_capture(events)())

    payload = json.dumps({"name": "feature-x"}).encode()

    async def fake_read():
        return payload

    async def fake_json():
        return {"name": "feature-x"}

    request = MagicMock()
    request.read = fake_read
    request.json = fake_json
    request.content_length = len(payload)
    request.can_read_body = True

    with (
        patch(
            "kiro_crew.apps.builtins.dev_fleet.repository._valid_worktree_names",
            new_callable=AsyncMock,
            return_value={"feature-x"},
        ),
        patch(
            "kiro_crew.apps.builtins.dev_fleet.worktree_ops._worktree_remove",
            new_callable=AsyncMock,
            return_value={"ok": False, "error": "worktree is dirty"},
        ),
    ):
        resp = await api_dev_fleet_worktree_remove(request)
    assert resp.status == 200  # handler keeps its HTTP contract
    assert len(events) == 1
    assert events[0]["outcome"] == "denied"
    assert "dirty" in events[0]["error"]


@pytest.mark.asyncio
async def test_run_record_includes_started():
    """New run records carry a 'started' timestamp for FE reattach (Codex R18 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    before = time.time()
    rid = await mod._start_run("test-label", ["true"])
    async with mod._RUNS_LOCK:
        rec = dict(mod._RUNS[rid])
    assert rec["started"] >= before - 1
    assert rec["started"] <= time.time() + 1
    # let the trivial subprocess worker finish before the loop closes
    await _run_settled(rid)


# --- escalation cleanup symlink guard (Codex R19) ---
def test_escalation_cleanup_skips_symlinked_app_dir(tmp_path, monkeypatch):
    """A symlinked escalated app dir must never be followed/deleted — the
    link target lives outside the apps tree (Codex R19 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    # Real directory elsewhere that a malicious/legacy symlink points at
    outside = tmp_path / "outside-target"
    outside.mkdir()
    (outside / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (outside / "data").mkdir()
    (outside / "data" / "keep.txt").write_text("user data")
    (outside / "state.json").write_text("{}")

    (apps_root / "knowledge").symlink_to(outside)

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    # The symlink target must be fully intact — nothing followed or deleted.
    assert (outside / "state.json").exists()
    assert (outside / "data" / "keep.txt").exists()
    assert (apps_root / "knowledge").is_symlink()


# --- cross-repo pod identity guard (Codex R22) ---
@pytest.mark.asyncio
async def test_pod_guard_rejects_foreign_pinned_checkout(monkeypatch, tmp_path):
    """A pod pinned to another repository's checkout must refuse the operation."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "repo-a" / "kirocrew-wt-feature"
    ours.mkdir(parents=True)
    foreign = tmp_path / "repo-b" / "kirocrew-wt-feature"
    foreign.mkdir(parents=True)

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(True, str(foreign))),
    ):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "different checkout" in err


@pytest.mark.asyncio
async def test_pod_guard_allows_matching_or_unpinned(monkeypatch, tmp_path):
    """Matching pin or no pin at all proceeds."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(True, str(ours))),
    ):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None

    # No pin file + no active unit -> pod never booted -> allow
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)),
        patch.object(runtime_mod.rt, "active_names", return_value=set()),
    ):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None


@pytest.mark.asyncio
async def test_pod_guard_fails_closed_on_pin_read_error(monkeypatch, tmp_path):
    """Cannot read the pin state -> refuse the pod operation."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", side_effect=OSError("boom")),
    ):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "cannot verify pod checkout pin" in err


@pytest.mark.asyncio
async def test_pod_guard_denies_pin_file_without_checkout(monkeypatch, tmp_path):
    """A pin file that exists but has no verifiable CHECKOUT is ambiguous
    pod identity -> deny (Codex R23 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(True, None)),
    ):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "ambiguous pod identity" in err


@_POSIX_ONLY
def test_read_pin_strict_propagates_read_errors(tmp_path):
    """_read_pin_strict must raise (not return empty) when the pin file
    exists but cannot be read."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    env = pods / "x.env"
    env.write_text("CHECKOUT='/some/checkout'\n")

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return env

    assert mod._read_pin_strict(FakeCfg(), "x") == (True, "/some/checkout")

    # Unreadable pin file (exists but open fails) must raise, not return empty
    if os.geteuid() != 0:  # chmod is a no-op guard for root
        env.chmod(0o000)
        try:
            with pytest.raises(OSError):
                mod._read_pin_strict(FakeCfg(), "x")
        finally:
            env.chmod(0o644)


def test_escalation_cleanup_skips_symlinked_meta_file(tmp_path, monkeypatch):
    """A symlinked installed.json must never be read (Codex R23 #1)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    # Sensitive file outside the app dir that a malicious symlink targets
    secret = tmp_path / "credentials.json"
    secret.write_text(json.dumps({"origin": "builtin", "token": "s3cr3t"}))
    (esc / "installed.json").symlink_to(secret)
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    # meta must be treated as None -> keep branch -> nothing deleted
    assert (esc / "state.json").exists()
    assert (esc / "installed.json").is_symlink()
    assert secret.exists()


# --- credential-free build env + pin symlink hardening (Codex R24) ---
def test_build_env_excludes_credentials(monkeypatch):
    """Build/CLI subprocess env must be allowlisted — gateway tokens excluded."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/u")

    env = mod._pod_env()
    # Pinned, never inherited. The pin is _TRUSTED_PATH plus (in the
    # credential-FREE tier only) the node toolchain dirs prepended, so that
    # npm's `#!/usr/bin/env node` run-scripts resolve — see
    # test_dev_fleet_node_toolchain.py for that boundary.
    assert env["PATH"] != "/usr/bin"
    assert mod._TRUSTED_PATH in env["PATH"]
    assert env["PATH"].endswith(mod._TRUSTED_PATH)
    assert "SLACK_BOT_TOKEN" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env

    assert env["HOME"] == "/home/u"
    assert env["KIROCREW_POD_REPO"] == mod.MAIN_REPO

    benv = mod._build_env()
    assert "SLACK_BOT_TOKEN" not in benv
    assert "KIROCREW_POD_REPO" not in benv

    # The credential-bearing tier keeps the bare pinned path — git resolves its
    # own helpers (git-remote-https, credential helpers) through PATH.
    assert mod._build_env(with_credentials=True)["PATH"] == mod._TRUSTED_PATH


def test_build_env_passes_npm_registry_but_drops_credential_shaped_keys(monkeypatch):
    """NPM_CONFIG_REGISTRY is a registry URL, not a credential -- it must reach
    every Dev Fleet npm step (preflight AND the real ``npm ci``/``npm run
    build``) so both resolve against the same registry. A credential-shaped
    variable next to it must still be dropped by the same allowlist.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmjs.org")
    monkeypatch.setenv("NPM_CONFIG__AUTHTOKEN", "npm-secret-token")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")

    for env in (mod._build_env(), mod._build_env(with_credentials=True)):
        assert env["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"
        assert "NPM_CONFIG__AUTHTOKEN" not in env
        assert "SLACK_BOT_TOKEN" not in env


def test_build_env_rejects_npm_registry_values_that_smuggle_credentials(monkeypatch):
    """``NPM_CONFIG_REGISTRY`` is forwarded only when it is a bare
    ``http``/``https`` registry URL with no userinfo, query, fragment, or
    embedded whitespace -- URL syntax otherwise permits a credential-bearing
    value (``https://user:token@host/``) or a smuggled second value to reach
    a worktree-controlled build script through this allowlist entry. A value
    that fails validation is dropped outright (fail closed), never rewritten,
    and an unrelated credential-shaped variable next to it is still dropped
    too.
    """
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    dropped = (
        "https://u:tok@registry.example/",  # userinfo
        "https://registry.example/?x=1",  # query
        "https://registry.example/#f",  # fragment
        "file:///etc/passwd",  # non-http(s) scheme
        "",  # empty
        "https://registry.npmjs.org ",  # embedded whitespace
    )
    for value in dropped:
        monkeypatch.setenv("NPM_CONFIG_REGISTRY", value)
        monkeypatch.setenv("NPM_CONFIG__AUTHTOKEN", "npm-secret-token")
        monkeypatch.setenv("NPM_TOKEN", "npm-secret-token-2")
        env = mod._build_env()
        assert "NPM_CONFIG_REGISTRY" not in env, value
        assert "NPM_CONFIG__AUTHTOKEN" not in env
        assert "NPM_TOKEN" not in env

    # A clean value right after a dropped one still passes through.
    monkeypatch.setenv("NPM_CONFIG_REGISTRY", "https://registry.npmjs.org")
    assert mod._build_env()["NPM_CONFIG_REGISTRY"] == "https://registry.npmjs.org"


def test_is_safe_env_key_matches_documented_spelling_on_windows():
    """A mixed-case allowlist entry must still match what ``os.environ`` yields.

    The allowlists write ``SystemRoot`` (Microsoft's documented spelling) while
    CPython's ``os.environ`` upper-cases every key on Windows. Folding is what
    keeps the two ends agreeing; without it the filter drops exactly the
    variables it was extended to carry.
    """
    assert "SystemRoot" in mod._WINDOWS_SAFE_ENV_KEYS
    if platform_compat.IS_WINDOWS:
        # The spelling os.environ actually yields.
        assert mod._is_safe_env_key("SYSTEMROOT")
        # And the documented spelling, so either end may be written.
        assert mod._is_safe_env_key("SystemRoot")
    assert not mod._is_safe_env_key("SLACK_BOT_TOKEN")


def test_is_safe_env_key_stays_exact_on_posix():
    """Folding is Windows-only — POSIX names are case-SENSITIVE.

    ``PATH`` and ``Path`` are genuinely different variables there, so a
    case-insensitive match would let a lookalike through.
    """
    assert mod._is_safe_env_key("PATH")
    if not platform_compat.IS_WINDOWS:
        assert not mod._is_safe_env_key("Path")
        # The Windows set must not leak into POSIX matching at all.
        assert not mod._is_safe_env_key("SystemRoot")


def test_windows_safe_env_keys_carry_no_credentials():
    """The Windows additions are platform paths, never secret-bearing vars."""
    for key in mod._WINDOWS_SAFE_ENV_KEYS:
        for marker in ("TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "APIKEY"):
            assert marker not in key.upper(), f"{key!r} looks credential-bearing"


def test_safe_env_keys_platform_composition():
    """The POSIX set always applies; the Windows set only on Windows."""
    posix = set(mod._POSIX_SAFE_ENV_KEYS)
    windows = set(mod._WINDOWS_SAFE_ENV_KEYS)
    active = set(mod._SAFE_ENV_KEYS)

    assert not (posix & windows), "the two sets must stay disjoint"
    assert posix <= active
    if platform_compat.IS_WINDOWS:
        assert windows <= active
    else:
        assert not (windows & active)


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="Windows-only env semantics")
def test_build_env_carries_systemroot_on_windows(monkeypatch):
    """SYSTEMROOT must reach every build/fetch child.

    Winsock locates its socket catalog through it, so a child without it cannot
    resolve names at all — libcurl reports that as ``getaddrinfo() thread failed
    to start`` and the Pull step fails before it reaches the network.
    """
    monkeypatch.setenv("SYSTEMROOT", r"C:\WINDOWS")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-secret")

    for env in (mod._build_env(), mod._build_env(with_credentials=True), mod._pod_env()):
        assert env["SYSTEMROOT"] == r"C:\WINDOWS"
        # Widening the allowlist must not have widened it to credentials.
        assert "SLACK_BOT_TOKEN" not in env


def test_read_pin_strict_rejects_symlinked_env(tmp_path):
    """A symlinked pin file must raise, never be read (Codex R24 #2)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    secret = tmp_path / "protected.env"
    secret.write_text("CHECKOUT='/attacker/checkout'\n")
    link = pods / "feature.env"
    link.symlink_to(secret)

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return link

    # O_NOFOLLOW open refuses the symlink atomically (ELOOP)
    with pytest.raises(OSError):
        mod._read_pin_strict(FakeCfg(), "feature")


def test_read_pin_strict_rejects_escape(tmp_path):
    """A pin path resolving outside pods_dir must raise."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    pods = tmp_path / "pods"
    pods.mkdir()
    outside = tmp_path / "outside.env"
    outside.write_text("CHECKOUT='/x'\n")

    class FakeCfg:
        pods_dir = pods

        def env_file(self, name):
            return outside  # regular file but not under pods_dir

    with pytest.raises(OSError, match="outside"):
        mod._read_pin_strict(FakeCfg(), "feature")


@pytest.mark.asyncio
async def test_completed_runs_are_evicted_beyond_cap():
    """Completed run records are bounded; running entries survive (Codex R28)."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    async with mod._RUNS_LOCK:
        saved = dict(mod._RUNS)
        mod._RUNS.clear()
        for i in range(mod._RUNS_MAX_COMPLETED + 5):
            mod._RUNS[f"old{i}"] = {
                "status": "done",
                "exit_code": 0,
                "label": "t",
                "output": [],
                "started": float(i),
            }
        mod._RUNS["active"] = {
            "status": "running",
            "exit_code": None,
            "label": "t",
            "output": [],
            "started": 999.0,
        }
    try:
        rid = await mod._start_run("evict-test", ["true"])
        async with mod._RUNS_LOCK:
            completed = [k for k, v in mod._RUNS.items() if v["status"] != "running"]
            assert len(completed) <= mod._RUNS_MAX_COMPLETED
            assert "active" in mod._RUNS  # running never evicted
            assert "old0" not in mod._RUNS  # oldest completed evicted first
        await _run_settled(rid)
    finally:
        async with mod._RUNS_LOCK:
            mod._RUNS.clear()
            mod._RUNS.update(saved)


# --- R29 hardening ---
@pytest.mark.asyncio
async def test_pod_guard_denies_active_unpinned_pod(tmp_path):
    """No pin + ACTIVE unit under the name = unattributable foreign pod -> deny."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    ours = tmp_path / "kirocrew-wt-feature"
    ours.mkdir()

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)),
        patch.object(runtime_mod.rt, "active_names", return_value={"kirocrew-wt-feature"}),
    ):
        err = await mod._pod_checkout_guard("kirocrew-wt-feature")
    assert err is not None
    assert "unattributable" in err

    # No pin + NOT active -> allow (pod never booted)
    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": str(ours)}, None),
        ),
        patch.object(runtime_mod, "_load_cfg", return_value=object()),
        patch.object(worktree_ops_mod, "_read_pin_strict", return_value=(False, None)),
        patch.object(runtime_mod.rt, "active_names", return_value=set()),
    ):
        assert await mod._pod_checkout_guard("kirocrew-wt-feature") is None


def test_escalation_cleanup_keeps_dir_when_data_present(tmp_path, monkeypatch):
    """Non-empty data/ -> whole dir kept, no partial deletion (R29 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (esc / "data").mkdir()
    (esc / "data" / "keep.txt").write_text("user data")
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    assert (esc / "state.json").exists()  # nothing partially deleted
    assert (esc / "data" / "keep.txt").exists()


def test_escalation_cleanup_removes_empty_builtin_via_pinned_fd(tmp_path, monkeypatch):
    """No data/ -> dir removed through the pinned descriptor (R29 #2)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))
    (esc / "state.json").write_text("{}")

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    mgr.register_builtin_apps()

    if (
        os.open in os.supports_dir_fd
        and os.unlink in os.supports_dir_fd
        and os.rmdir in os.supports_dir_fd
        and hasattr(os, "O_DIRECTORY")
    ):
        assert not esc.exists()  # dir_fd deletion supported
    else:  # no race-free primitive -> fail closed -> kept
        assert esc.exists()


# --- git config/protocol neutralization chokepoint (Codex R32/R34) ---
def _assert_git_neutralizers(env):
    assert env["GIT_ALLOW_PROTOCOL"] == "https:ssh"
    assert env["GIT_PROTOCOL_FROM_USER"] == "0"
    # Not a config key and not about code execution: it pins WHICH OBJECT GRAPH
    # git answers from, so every answer this handler acts on describes the
    # history the checkout actually holds rather than a grafted substitute.
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    # Also not a config key: it pins WHAT GIT WRITES on a read. `git status`
    # refreshes the index and saves it back under `index.lock`, so a command that
    # is a read to its caller is a write to the repository.
    assert env["GIT_OPTIONAL_LOCKS"] == "0"
    # Full config-driven-execution neutralizer set, injected as env so it
    # covers EVERY git call (background fetch, rebase, sync pull included).
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs == {
        "core.fsmonitor": "false",
        "core.hooksPath": "/dev/null",
        "credential.helper": "",
        "core.sshCommand": "ssh",
        # Signature verification is the third driver class, and it is reached by a
        # READ: `[log] showSignature=true` makes every `git log` verify, and
        # verification EXECS the program these name. All four spellings, because
        # `gpg.openpgp.program` is a synonym that OVERRIDES the bare `gpg.program`.
        "gpg.program": "true",
        "gpg.openpgp.program": "true",
        "gpg.ssh.program": "true",
        "gpg.x509.program": "true",
        "log.showSignature": "false",
    }


@pytest.mark.asyncio
async def test_run_cmd_pins_git_protocols(monkeypatch):
    """Every _run_cmd spawn env carries the full git neutralizer set so an
    ext:: origin / malicious fsmonitor / hooksPath / credential.helper /
    sshCommand from agent-writable .git/config is refused by git itself."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    captured = {}

    def fake_sandbox(cmd, mode, *, env=None, **kw):
        captured["env"] = env
        raise RuntimeError("stop here")  # short-circuit before spawning

    monkeypatch.setattr(runtime_mod, "sandboxed_spawn_argv", fake_sandbox)
    rc, _, err = await mod._run_cmd(["git", "-C", "/x", "fetch"])
    assert rc == -1 and "sandbox unavailable" in err
    _assert_git_neutralizers(captured["env"])


def test_build_env_pins_git_protocols():
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    env = mod._build_env()
    _assert_git_neutralizers(env)


# --- cancellation survives an already-reaped child ---
@pytest.mark.asyncio
async def test_run_cmd_cancel_with_reaped_child_propagates_cancellation(monkeypatch):
    """Cancelling _run_cmd whose child was already reaped must raise
    CancelledError, not ProcessLookupError.

    An unguarded ``proc.kill()`` in the CancelledError branch raises
    ProcessLookupError on a reaped child, REPLACING the in-flight
    cancellation; ``_status_refresher``'s broad handler then swallows it
    and loops forever, hanging ``dev_fleet_cleanup``'s ``await bg_task``
    and the whole pytest-asyncio loop teardown.
    """
    entered = asyncio.Event()  # deterministic rendezvous, no sleeps

    class FakeProc:
        pid = 12345
        returncode = None

        async def communicate(self):
            entered.set()
            # flake-ok: pre-existing stub that parks until the test cancels it; the test bounds its own wait on entry, moved verbatim
            await asyncio.Event().wait()  # block until cancelled

        def kill(self):
            raise ProcessLookupError  # child already reaped

        async def wait(self):
            return 0

    async def fake_spawn(*args, **kwargs):
        return FakeProc()

    monkeypatch.setattr(
        runtime_mod,
        "sandboxed_spawn_argv",
        lambda cmd, mode, *, env=None, **kw: (cmd, env, None),
    )
    monkeypatch.setattr(runtime_mod, "create_subprocess_limited", fake_spawn)
    monkeypatch.setattr(runtime_mod, "_kill_tree", AsyncMock())
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg, and shrink its bound so the reap of a
    # still-blocking communicate() cannot stall this test.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())
    monkeypatch.setattr(platform_compat, "REAP_TIMEOUT_SECS", 0.01)

    task = asyncio.ensure_future(mod._run_cmd(["/bin/true"]))
    # Bounded so a future early-return in _run_cmd fails fast, not a hang.
    await await_until(entered, what="run_cmd entered cancellation point")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# --- stream-overrun subprocess reaping (Codex R34 #2) ---
@pytest.mark.asyncio
async def test_start_run_readline_overrun_kills_process_tree(monkeypatch):
    """When the output stream loop raises (e.g. a single line exceeding the
    64 KiB asyncio stream limit), the still-running subprocess tree is
    killed instead of being orphaned past its run record."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    killed: list[int] = []

    class FakeStdout:
        async def readline(self):
            raise ValueError("Separator is found, but chunk is longer than limit")

    class FakeProc:
        pid = 424242
        returncode: int | None = None
        stdout = FakeStdout()
        wait_calls = 0
        communicate_calls = 0

        def kill(self):
            FakeProc.returncode = -9

        async def communicate(self):
            FakeProc.communicate_calls += 1
            FakeProc.returncode = FakeProc.returncode or -9
            return b"", b""

        async def wait(self):
            FakeProc.wait_calls += 1
            FakeProc.returncode = FakeProc.returncode or -9
            return FakeProc.returncode

    async def fake_exec(*a, **kw):
        return FakeProc()

    async def fake_kill_tree(pid):
        killed.append(pid)

    monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(runtime_mod, "_kill_tree", fake_kill_tree)
    # The shared reap helper also signals the tree; intercept it so the fake
    # pid never reaches a real killpg on the host.
    monkeypatch.setattr(platform_compat, "kill_process_tree_async", AsyncMock())
    FakeProc.returncode = None

    # Absolute: the spawn shim execs without a PATH search, so only a bare name
    # would be resolved (and rejected) before fake_exec is ever reached. The
    # command itself is irrelevant to this test.
    rid = await mod._start_run("overrun-test", ["/usr/bin/whatever"])
    await _run_settled(rid)
    async with mod._RUNS_LOCK:
        rec = dict(mod._RUNS[rid])
    assert rec["status"] == "done" and rec["exit_code"] == -1
    assert any("chunk is longer than limit" in line for line in rec["output"])
    assert killed == [424242]  # tree reaped exactly once
    assert FakeProc.returncode is not None  # proc.kill() ran
    # The reap drains pipes via communicate(), never a bare wait() that a
    # full pipe could hang.
    assert FakeProc.communicate_calls == 1
    assert FakeProc.wait_calls == 0


# --- Codex R35 regressions ---
@pytest.mark.asyncio
async def test_sync_fetch_standard_merge_strict(monkeypatch):
    """The sync runner never uses `git pull`: the network fetch runs at
    "standard" while the checkout-performing ff-merge (which executes
    repo-controlled smudge filters / merge drivers) runs "strict" with
    credential dirs hidden, like the pip/npm build steps."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    captured: list[tuple[list, str]] = []

    def fake_sandbox(argv, mode, *, env=None, **kw):
        captured.append((list(argv), mode))
        return list(argv), dict(env or {}), None

    with (
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value=mod.BASE_BRANCH),
        patch.object(worktree_ops_mod, "_venv_python", return_value=Path("/fake/.venv/bin/python")),
        patch.object(runtime_mod, "_trusted_bin", side_effect=lambda n: f"/usr/bin/{n}"),
        patch.object(runtime_mod, "_build_env", return_value={}),
        patch.object(worktree_ops_mod, "sandboxed_spawn_argv", fake_sandbox),
        patch.object(
            runtime_mod, "_start_run", new_callable=AsyncMock, return_value="rid-sync-test"
        ),
    ):
        worktree_ops_mod._SYNC_RID = None
        res = await mod._sync()
        worktree_ops_mod._SYNC_RID = None
    assert res.get("ok") is not False
    assert not any("pull" in argv for argv, _ in captured)
    fetch = [m for argv, m in captured if "fetch" in argv]
    merge = [m for argv, m in captured if "merge" in argv]
    assert fetch == ["standard"]
    assert merge == ["strict"]
    for argv, m in captured:
        if "pip" in " ".join(map(str, argv)) or argv[0] == "npm":
            assert m == "strict"


@pytest.mark.asyncio
async def test_removal_refuses_when_commit_races_verdict():
    """A commit pushed after merge causes OID divergence -> refuse non-forced removal."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    with (
        patch.object(
            repository_mod,
            "_find_worktree",
            new_callable=AsyncMock,
            return_value=({"path": "/x/wt-feat", "branch": "feat-x", "is_main": False}, None),
        ),
        patch.object(repository_mod, "_real_dirty", new_callable=AsyncMock, return_value=False),
        patch.object(
            fleet_state_mod,
            "_pr_status_cached",
            new_callable=AsyncMock,
            return_value={"state": "MERGED"},
        ),
        patch.object(repository_mod, "_own_commits_count", new_callable=AsyncMock, return_value=0),
        patch.object(repository_mod, "_git", new_callable=AsyncMock, return_value="deadbeef_local"),
        patch.object(
            fleet_state_mod,
            "_fetch_pr_head_oid",
            new_callable=AsyncMock,
            return_value="deadbeef_pr_different",
        ),
        patch.object(
            repository_mod, "_upstream_remote", new_callable=AsyncMock, return_value="origin"
        ),
        patch.object(runtime_mod, "_POD_AVAILABLE", False),
    ):
        res = await mod._worktree_remove("wt-feat", force=False)
    assert res["ok"] is False
    assert "OID diverged" in res["error"]


@pytest.mark.asyncio
async def test_owner_repo_failure_not_cached_forever():
    """A transient owner/repo lookup failure retries after the TTL instead
    of disabling PR status until gateway restart; success is cached."""
    import kiro_crew.apps.builtins.dev_fleet.server as mod

    lookups = AsyncMock(side_effect=[None, "own/repo"])
    fleet_state_mod._OWNER_REPO = None
    fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
    try:
        with patch.object(fleet_state_mod, "_repo_owner_name", lookups):
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # Within the TTL: no re-lookup, still None
            assert await mod._get_owner_repo() is None
            assert lookups.await_count == 1
            # TTL expired: retried and success cached
            fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0
            assert await mod._get_owner_repo() == "own/repo"
            assert await mod._get_owner_repo() == "own/repo"
            assert lookups.await_count == 2
    finally:
        fleet_state_mod._OWNER_REPO = None
        fleet_state_mod._OWNER_REPO_RETRY_AT = 0.0


# --- Codex R36: escalation cleanup pins BEFORE validating ---
def test_escalation_cleanup_fails_closed_without_dirfd(tmp_path, monkeypatch):
    """Platforms without dir_fd primitives cannot pin validation to the
    deletion, so cleanup must be skipped entirely (fail closed)."""
    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)
    monkeypatch.setattr(mgr, "_dirfd_ops_supported", lambda: False)

    mgr.register_builtin_apps()

    assert esc.is_dir()  # kept — no safe primitives to delete with
    assert (esc / "installed.json").exists()


def test_escalation_cleanup_swapped_entry_survives(tmp_path, monkeypatch):
    """A directory swapped in at the same name AFTER the descriptor pin must
    not be rmdir'd: the final unlink verifies the entry still refers to the
    pinned inode."""
    import os as _os

    import kiro_crew.apps.manager as mgr

    apps_root = tmp_path / "apps"
    apps_root.mkdir()
    esc = apps_root / "knowledge"
    esc.mkdir()
    (esc / "installed.json").write_text(json.dumps({"origin": "builtin"}))

    monkeypatch.setattr(mgr, "apps_dir", lambda: apps_root)
    monkeypatch.setattr(mgr, "app_dir", lambda name: apps_root / name)

    real_rmtree = mgr._rmtree_dirfd

    def racing_rmtree(fd):
        # Simulate the race: the validated dir is renamed away and an
        # attacker drops a NEW (empty) dir at the same name, right after
        # the pin but before the final unlink-by-name.
        real_rmtree(fd)
        _os.rename(str(esc), str(tmp_path / "moved-away"))
        (apps_root / "knowledge").mkdir()

    monkeypatch.setattr(mgr, "_rmtree_dirfd", racing_rmtree)

    mgr.register_builtin_apps()

    # The swapped-in directory is NOT the validated inode — it must survive.
    assert (apps_root / "knowledge").is_dir()
