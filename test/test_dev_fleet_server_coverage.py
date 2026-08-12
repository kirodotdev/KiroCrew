"""Coverage-focused tests for the Dev Fleet standalone backend.

Targets uncovered helper branches, pod-guard refusals, request-handler
validation, the audit decorator's non-success paths, and the lifecycle
hooks of ``kiro_crew.apps.builtins.dev_fleet.server``.

Everything is injected: no real git, no real subprocess, no network, and no
writes outside ``tmp_path``. Where the module reads its config home the
loader's ``config_dir`` is patched, so nothing touches the real one.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import gateway_service

# The launchd label derivation imports the pod launchd module for its label
# prefix; on a host where that optional module is unavailable the function
# falls back to the live label and the pod branch cannot be observed.
_LAUNCHD_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="launchd live-program layout is POSIX-only",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
class _CapturingSel:
    """Minimal SEL stand-in that records every audit call."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def log_tool_invocation(self, **kw) -> None:
        self.events.append(kw)


def _sel_capture(monkeypatch) -> _CapturingSel:
    sink = _CapturingSel()
    monkeypatch.setattr(mod, "_sel", lambda: sink)
    return sink


def _json_request(payload: dict) -> MagicMock:
    """A request double shaped like the one the audit decorator expects."""
    raw = json.dumps(payload).encode()
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    request.json = AsyncMock(return_value=payload)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


def _raw_request(raw: bytes, *, json_error: Exception | None = None) -> MagicMock:
    request = MagicMock()
    request.read = AsyncMock(return_value=raw)
    if json_error is not None:
        request.json = AsyncMock(side_effect=json_error)
    else:
        try:
            request.json = AsyncMock(return_value=json.loads(raw or b"{}"))
        except ValueError as exc:
            request.json = AsyncMock(side_effect=exc)
    request.content_length = len(raw)
    request.can_read_body = True
    return request


def _same(a: str, b: str) -> bool:
    """Path equality that survives Windows 8.3 short paths and /tmp symlinks."""
    return os.path.realpath(a) == os.path.realpath(b)


@pytest.fixture(autouse=True)
def _pin_module_state(monkeypatch):
    """Neutralise the module's cached globals so tests never share state."""
    monkeypatch.setattr(mod, "_UPSTREAM_REMOTE", "origin")
    monkeypatch.setattr(mod, "_HTML_BASE", None)
    monkeypatch.setattr(mod, "_PR_CACHE", {})
    monkeypatch.setattr(mod, "_FALLBACK_REPOS", [])
    monkeypatch.setattr(mod, "_WT_LOCKS", {})
    monkeypatch.setattr(mod, "_PROVISION_INFLIGHT", {})
    monkeypatch.setattr(mod, "_RUNS", {})
    monkeypatch.setattr(mod, "_BUILD_PATH_CACHE", "/usr/bin")
    monkeypatch.setattr(mod, "_warm_build_path", AsyncMock())
    monkeypatch.setattr(mod, "_pod_env", lambda: {})


# --------------------------------------------------------------------------
# _load_dev_fleet_cfg
# --------------------------------------------------------------------------
def test_load_dev_fleet_cfg_overlay_wins(monkeypatch, tmp_path):
    """config.local.json overlays config.json for the dev_fleet section."""
    (tmp_path / "config.json").write_text(
        json.dumps({"dev_fleet": {"a": 1, "keep": "yes"}}), encoding="utf-8", newline="\n"
    )
    (tmp_path / "config.local.json").write_text(
        json.dumps({"dev_fleet": {"a": 2, "b": 3}}), encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    assert mod._load_dev_fleet_cfg() == {"a": 2, "keep": "yes", "b": 3}


def test_load_dev_fleet_cfg_ignores_unusable_files(monkeypatch, tmp_path):
    """Invalid JSON and a non-dict dev_fleet value are skipped, never raised."""
    (tmp_path / "config.json").write_text("{not json", encoding="utf-8", newline="\n")
    (tmp_path / "config.local.json").write_text(
        json.dumps({"dev_fleet": "nope"}), encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)

    assert mod._load_dev_fleet_cfg() == {}


def test_load_dev_fleet_cfg_config_dir_failure_is_empty(monkeypatch):
    """A config home that cannot be resolved yields {} rather than an error."""
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert mod._load_dev_fleet_cfg() == {}


# --------------------------------------------------------------------------
# _launchd_live_worktree
# --------------------------------------------------------------------------
@_LAUNCHD_ONLY
def test_launchd_live_worktree_missing_launcher_is_none(monkeypatch, tmp_path):
    missing = tmp_path / "absent" / "live-gateway"
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: missing)
    )
    assert mod._launchd_live_worktree() is None


@_LAUNCHD_ONLY
@pytest.mark.parametrize(
    "script",
    [
        "#!/bin/sh\nexport FOO=1\n",  # no exec line at all
        "#!/bin/sh\nexec '/usr/local/bin/kirocrew' gateway\n",  # not a venv binary
    ],
)
def test_launchd_live_worktree_unusable_exec_is_none(monkeypatch, tmp_path, script):
    launcher = tmp_path / "live-gateway"
    launcher.write_text(script, encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: launcher)
    )
    assert mod._launchd_live_worktree() is None


@_LAUNCHD_ONLY
def test_launchd_live_worktree_resolves_checkout(monkeypatch, tmp_path):
    """A venv binary in the exec line resolves to its checkout grandparent."""
    checkout = tmp_path / "kirocrew-wt-alpha"
    kcbin = checkout / ".venv" / "bin" / "kirocrew"
    kcbin.parent.mkdir(parents=True)
    kcbin.write_text("", encoding="utf-8", newline="\n")
    launcher = tmp_path / "live-gateway"
    launcher.write_text(f"#!/bin/sh\nexec '{kcbin}' gateway\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(
        gateway_service.LaunchdBackend, "live_program", staticmethod(lambda: launcher)
    )

    resolved = mod._launchd_live_worktree()
    assert resolved is not None
    assert _same(resolved, str(checkout))


# --------------------------------------------------------------------------
# _load_fallback_repos
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_load_fallback_repos_collects_ancestor_remotes(monkeypatch):
    """A remote whose base is an ancestor of upstream becomes a fallback repo."""
    async def fake_run(cmd, **kw):
        if cmd[-1] == "remote":
            return 0, "origin\nfork\nstale\n", ""
        if "--is-ancestor" in cmd:
            return (0 if "fork/main" in cmd else 1), "", ""
        if "get-url" in cmd:
            return 0, "git@github.com:someone/kirocrew.git\n", ""
        return 1, "", "unexpected"

    monkeypatch.setattr(mod, "_run_cmd", fake_run)
    await mod._load_fallback_repos()
    assert mod._FALLBACK_REPOS == ["someone/kirocrew"]


@pytest.mark.asyncio
async def test_load_fallback_repos_empty_when_remote_listing_fails(monkeypatch):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "", "boom")))
    await mod._load_fallback_repos()
    assert mod._FALLBACK_REPOS == []


# --------------------------------------------------------------------------
# PR cache + html base
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pr_status_cached_skips_base_branch(monkeypatch):
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(mod, "_fetch_pr_status", fetch)
    assert await mod._pr_status_cached(mod.BASE_BRANCH) is None
    assert await mod._pr_status_cached("") is None
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_merged_entry_is_terminal(monkeypatch):
    """A MERGED entry is served regardless of age — no refetch."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(mod, "_fetch_pr_status", fetch)
    monkeypatch.setattr(mod, "_PR_CACHE", {"feat": {"data": {"state": "MERGED"}, "ts": 0.0}})

    assert (await mod._pr_status_cached("feat"))["state"] == "MERGED"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_pr_status_cached_stale_closed_entry_refetches(monkeypatch):
    """A CLOSED entry can be reopened, so it expires on the normal TTL."""
    fetch = AsyncMock(return_value={"state": "OPEN"})
    monkeypatch.setattr(mod, "_fetch_pr_status", fetch)
    monkeypatch.setattr(mod, "_PR_CACHE", {"feat": {"data": {"state": "CLOSED"}, "ts": 0.0}})

    assert (await mod._pr_status_cached("feat"))["state"] == "OPEN"
    fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_pr_status_cached_fresh_entry_is_served(monkeypatch):
    fetch = AsyncMock(return_value={"state": "MERGED"})
    monkeypatch.setattr(mod, "_fetch_pr_status", fetch)
    monkeypatch.setattr(
        mod, "_PR_CACHE", {"feat": {"data": {"state": "OPEN"}, "ts": time.time()}}
    )

    assert (await mod._pr_status_cached("feat"))["state"] == "OPEN"
    fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_html_repo_base_from_remote_url(monkeypatch):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "git@github.com:o/r.git\n", "")))
    assert await mod._html_repo_base() == "https://github.com/o/r"


@pytest.mark.asyncio
async def test_html_repo_base_falls_back_to_owner_repo(monkeypatch):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "", "no remote")))
    monkeypatch.setattr(mod, "_get_owner_repo", AsyncMock(return_value="o/r"))
    assert await mod._html_repo_base() == "https://github.com/o/r"


@pytest.mark.asyncio
async def test_html_repo_base_cached_value_short_circuits(monkeypatch):
    run = AsyncMock(return_value=(0, "", ""))
    monkeypatch.setattr(mod, "_run_cmd", run)
    monkeypatch.setattr(mod, "_HTML_BASE", "https://example.invalid/o/r")
    assert await mod._html_repo_base() == "https://example.invalid/o/r"
    run.assert_not_awaited()


# --------------------------------------------------------------------------
# _read_pin_strict
# --------------------------------------------------------------------------
def _pin_cfg(tmp_path: Path, text: str | None) -> SimpleNamespace:
    pods = tmp_path / "pods"
    pods.mkdir(exist_ok=True)
    env_path = pods / "feat.env"
    if text is not None:
        env_path.write_text(text, encoding="utf-8", newline="\n")
    return SimpleNamespace(pods_dir=pods, env_file=lambda name: env_path)


def test_read_pin_strict_no_env_file_is_unpinned(tmp_path):
    cfg = _pin_cfg(tmp_path, None)
    assert mod._read_pin_strict(cfg, "feat") == (False, None)


def test_read_pin_strict_refused_read_raises(monkeypatch, tmp_path):
    """A hooks-gate refusal is a DENY, never 'unpinned'."""
    cfg = _pin_cfg(tmp_path, "CHECKOUT=/x\n")
    monkeypatch.setattr(mod.hooks, "safe_read_file_bytes_nolink", lambda *a, **k: None)
    with pytest.raises(OSError, match="refused by hooks read gate"):
        mod._read_pin_strict(cfg, "feat")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("# a comment\nnovalue\nCHECKOUT='/repo/wt'\n", (True, "/repo/wt")),
        ('OTHER=1\nCHECKOUT="/repo/wt"\n', (True, "/repo/wt")),
        ("CHECKOUT=\n", (True, None)),
        ("OTHER=1\n", (True, None)),
    ],
)
def test_read_pin_strict_parses_checkout(monkeypatch, tmp_path, body, expected):
    cfg = _pin_cfg(tmp_path, body)
    monkeypatch.setattr(
        mod.hooks, "safe_read_file_bytes_nolink", lambda *a, **k: body.encode()
    )
    assert mod._read_pin_strict(cfg, "feat") == expected


# --------------------------------------------------------------------------
# _pod_checkout_guard
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_pod_guard_unknown_worktree(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=(None, None)))
    assert "unknown worktree" in (await mod._pod_checkout_guard("ghost") or "")


@pytest.mark.asyncio
async def test_pod_guard_no_pod_subsystem_allows(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: None)
    monkeypatch.setattr(mod, "_POD_AVAILABLE", False)
    assert await mod._pod_checkout_guard("feat") is None


@pytest.mark.asyncio
async def test_pod_guard_unloadable_config_denies(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: None)
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    assert await mod._pod_checkout_guard("feat") == (
        "cannot load pod configuration to verify pod identity"
    )


@pytest.mark.asyncio
async def test_pod_guard_unreadable_pin_denies(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)

    def _boom(cfg, name):
        raise OSError("pin unreadable")

    monkeypatch.setattr(mod, "_read_pin_strict", _boom)
    assert "cannot verify pod checkout pin" in (await mod._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_active_pod_without_pin_denies(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (False, None))
    monkeypatch.setattr(
        mod, "rt", SimpleNamespace(active_names=lambda cfg: {"feat"}), raising=False
    )
    assert "unattributable pod identity" in (await mod._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_inactive_pod_without_pin_allows(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (False, None))
    monkeypatch.setattr(mod, "rt", SimpleNamespace(active_names=lambda cfg: set()), raising=False)
    assert await mod._pod_checkout_guard("feat") is None


@pytest.mark.asyncio
async def test_pod_guard_active_names_failure_denies(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (False, None))

    def _boom(cfg):
        raise RuntimeError("systemctl gone")

    monkeypatch.setattr(mod, "rt", SimpleNamespace(active_names=_boom), raising=False)
    assert "cannot verify active pods" in (await mod._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_pin_without_checkout_denies(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (True, None))
    assert "ambiguous pod identity" in (await mod._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_foreign_checkout_denies(monkeypatch, tmp_path):
    mine = tmp_path / "mine"
    theirs = tmp_path / "theirs"
    mine.mkdir()
    theirs.mkdir()
    monkeypatch.setattr(
        mod, "_find_worktree", AsyncMock(return_value=({"path": str(mine)}, None))
    )
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (True, str(theirs)))
    assert "cross-repository pod operation" in (await mod._pod_checkout_guard("feat") or "")


@pytest.mark.asyncio
async def test_pod_guard_matching_checkout_allows(monkeypatch, tmp_path):
    mine = tmp_path / "mine"
    mine.mkdir()
    monkeypatch.setattr(
        mod, "_find_worktree", AsyncMock(return_value=({"path": str(mine)}, None))
    )
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "_read_pin_strict", lambda cfg, name: (True, str(mine)))
    assert await mod._pod_checkout_guard("feat") is None


# --------------------------------------------------------------------------
# pod operations
# --------------------------------------------------------------------------
@pytest.fixture
def allow_pod(monkeypatch):
    """Pod guard passes and pod state verification is opt-in per test."""
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_load_cfg", lambda: None)
    monkeypatch.setattr(mod, "_POD_AVAILABLE", False)


@pytest.mark.asyncio
async def test_pod_up_refused_by_guard(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value="nope"))
    assert await mod._pod_up("feat") == {"ok": False, "error": "nope"}


@pytest.mark.asyncio
async def test_pod_up_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "", "boom")))
    assert await mod._pod_up("feat") == {"ok": False, "error": "boom"}


@pytest.mark.asyncio
async def test_pod_up_non_json_output_still_ok(monkeypatch, allow_pod):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "not json", "")))
    assert await mod._pod_up("feat") == {"ok": True, "output": "not json"}


@pytest.mark.asyncio
async def test_pod_up_json_output_is_merged(monkeypatch, allow_pod):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, '{"port": 9999}', "")))
    assert await mod._pod_up("feat") == {"ok": True, "port": 9999}


@pytest.mark.asyncio
async def test_pod_up_inactive_after_start_fails_closed(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "{}", "")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(mod, "rt", SimpleNamespace(active_names=lambda cfg: set()), raising=False)
    assert await mod._pod_up("feat") == {"ok": False, "error": "pod not active after start"}


@pytest.mark.asyncio
async def test_pod_up_unverifiable_start_fails_closed(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "{}", "")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)

    def _boom(cfg):
        raise RuntimeError("no bus")

    monkeypatch.setattr(mod, "rt", SimpleNamespace(active_names=_boom), raising=False)
    res = await mod._pod_up("feat")
    assert res["ok"] is False
    assert "cannot verify pod start" in res["error"]


@pytest.mark.asyncio
async def test_pod_down_refused_by_guard(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await mod._pod_down("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_down_cli_failure_is_reported(monkeypatch, allow_pod):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(2, "out", "")))
    assert await mod._pod_down("feat") == {"ok": False, "error": "out"}


@pytest.mark.asyncio
async def test_pod_down_still_active_fails_closed(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    monkeypatch.setattr(
        mod, "rt", SimpleNamespace(active_names=lambda cfg: {"feat"}), raising=False
    )
    assert await mod._pod_down("feat") == {
        "ok": False, "error": "pod still active after shutdown",
    }


@pytest.mark.asyncio
async def test_pod_down_unverifiable_shutdown_fails_closed(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)

    def _boom(cfg):
        raise RuntimeError("no bus")

    monkeypatch.setattr(mod, "rt", SimpleNamespace(active_names=_boom), raising=False)
    res = await mod._pod_down("feat")
    assert res["ok"] is False
    assert "cannot verify pod shutdown" in res["error"]


@pytest.mark.asyncio
async def test_pod_down_success(monkeypatch, allow_pod):
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    assert await mod._pod_down("feat") == {"ok": True, "error": None}


@pytest.mark.asyncio
async def test_pod_restart_stops_on_failed_shutdown(monkeypatch):
    monkeypatch.setattr(mod, "_pod_down", AsyncMock(return_value={"ok": False, "error": "stuck"}))
    up = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mod, "_pod_up", up)

    res = await mod._pod_restart("feat")
    assert res == {"ok": False, "error": "pod shutdown failed: stuck"}
    up.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_restart_starts_after_clean_shutdown(monkeypatch):
    monkeypatch.setattr(mod, "_pod_down", AsyncMock(return_value={"ok": True, "error": None}))
    monkeypatch.setattr(mod, "_pod_up", AsyncMock(return_value={"ok": True, "port": 1}))
    assert await mod._pod_restart("feat") == {"ok": True, "port": 1}


@pytest.mark.asyncio
async def test_pod_token_refused_by_guard(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await mod._pod_token("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_token_without_config(monkeypatch, allow_pod):
    assert await mod._pod_token("feat") == {"ok": False, "error": "PodConfig unavailable"}


@pytest.mark.asyncio
async def test_pod_token_mints_url(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod, "rt",
        SimpleNamespace(
            mint_token=lambda cfg, name, ttl: "SECRET",
            derive_port=lambda cfg, name: 9123,
        ),
        raising=False,
    )
    res = await mod._pod_token("feat")
    assert res["ok"] is True
    assert res["url"].endswith("9123/?token=SECRET")


@pytest.mark.asyncio
async def test_pod_token_reports_mint_failure(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())

    def _boom(cfg, name, ttl):
        raise RuntimeError("keyring locked")

    monkeypatch.setattr(mod, "rt", SimpleNamespace(mint_token=_boom), raising=False)
    assert await mod._pod_token("feat") == {"ok": False, "error": "keyring locked"}


@pytest.mark.asyncio
async def test_pod_logs_refused_by_guard(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await mod._pod_logs("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_logs_without_config(monkeypatch, allow_pod):
    assert await mod._pod_logs("feat") == {"ok": False, "error": "PodConfig unavailable"}


@pytest.mark.asyncio
async def test_pod_logs_returns_redacted_journal(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(
        mod, "rt",
        SimpleNamespace(recent_journal=lambda cfg, name, n: f"lines={n}"),
        raising=False,
    )
    assert await mod._pod_logs("feat", 7) == {"ok": True, "logs": "lines=7"}


@pytest.mark.asyncio
async def test_pod_provision_refused_by_guard(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value="denied"))
    assert await mod._pod_provision("feat") == {"ok": False, "error": "denied"}


@pytest.mark.asyncio
async def test_pod_provision_single_flights_running_build(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_PROVISION_INFLIGHT", {"feat": "run-1"})
    monkeypatch.setattr(mod, "_RUNS", {"run-1": {"status": "running"}})
    start = AsyncMock(return_value="run-2")
    monkeypatch.setattr(mod, "_start_run", start)

    assert await mod._pod_provision("feat") == {
        "ok": False, "error": "provision already running", "run_id": "run-1",
    }
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_provision_starts_run_and_records_it(monkeypatch):
    monkeypatch.setattr(mod, "_pod_checkout_guard", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_PROVISION_INFLIGHT", {"feat": "run-0"})
    monkeypatch.setattr(mod, "_RUNS", {"run-0": {"status": "done"}})
    monkeypatch.setattr(
        mod, "sandboxed_spawn_argv", lambda argv, tier, env=None: (list(argv), {}, None)
    )
    monkeypatch.setattr(mod, "_start_run", AsyncMock(return_value="run-9"))

    assert await mod._pod_provision("feat") == {"ok": True, "run_id": "run-9"}
    assert mod._PROVISION_INFLIGHT["feat"] == "run-9"


# --------------------------------------------------------------------------
# _disk
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disk_in_progress_returns_snapshot(monkeypatch):
    monkeypatch.setattr(mod, "_DISK", {"status": "computing", "total_mb": None, "per": {}})
    assert (await mod._disk())["status"] == "computing"


@pytest.mark.asyncio
async def test_disk_done_snapshot_resets_to_idle(monkeypatch):
    monkeypatch.setattr(mod, "_DISK", {"status": "done", "total_mb": 42, "per": {"a": 42}})
    snap = await mod._disk()
    assert snap["total_mb"] == 42
    assert mod._DISK["status"] == "idle"


@pytest.mark.asyncio
async def test_disk_idle_starts_background_aggregation(monkeypatch):
    monkeypatch.setattr(mod, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(
        mod, "_discover_worktrees",
        AsyncMock(return_value=[{"path": "/repo/wt-a"}, {"path": "/repo/wt-b"}]),
    )

    async def fake_run(cmd, **kw):
        return (0, "12\t" + cmd[-1], "") if cmd[-1].endswith("wt-a") else (1, "", "err")

    monkeypatch.setattr(mod, "_run_cmd", fake_run)

    assert await mod._disk() == {"status": "computing", "total_mb": None, "per": {}}
    for _ in range(50):
        if mod._DISK["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert mod._DISK["per"] == {"wt-a": 12}
    assert mod._DISK["total_mb"] == 12


@pytest.mark.asyncio
async def test_disk_aggregation_failure_reports_unknown(monkeypatch):
    monkeypatch.setattr(mod, "_DISK", {"status": "idle", "total_mb": None, "per": {}})
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(side_effect=RuntimeError("git")))

    await mod._disk()
    for _ in range(50):
        if mod._DISK["status"] == "done":
            break
        await asyncio.sleep(0.01)
    assert mod._DISK == {"status": "done", "total_mb": None, "per": {}}


# --------------------------------------------------------------------------
# rebase
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rebase_unknown_worktree(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=(None, "gone")))
    assert await mod._rebase("feat") == {"ok": False, "error": "gone"}


@pytest.mark.asyncio
async def test_rebase_refuses_main_checkout(monkeypatch):
    monkeypatch.setattr(
        mod, "_find_worktree", AsyncMock(return_value=({"path": "/r", "is_main": True}, None))
    )
    assert await mod._rebase("main") == {
        "ok": False, "error": "refusing to rebase the main checkout",
    }


@pytest.mark.asyncio
async def test_rebase_rejects_concurrent_run(monkeypatch):
    lock = asyncio.Lock()
    await lock.acquire()
    try:
        monkeypatch.setattr(mod, "_WT_LOCKS", {"feat": lock})
        monkeypatch.setattr(
            mod, "_find_worktree", AsyncMock(return_value=({"path": "/r"}, None))
        )
        assert await mod._rebase("feat") == {
            "ok": False, "error": "rebase already running for this worktree",
        }
    finally:
        lock.release()


@pytest.mark.asyncio
async def test_rebase_locked_unverifiable_state(monkeypatch):
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value=None))
    assert await mod._rebase_locked({"path": "/r"}) == {
        "ok": False, "error": "cannot verify worktree state (git status failed)",
    }


@pytest.mark.asyncio
async def test_rebase_locked_refuses_dirty_worktree(monkeypatch):
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value=" M file.py"))
    assert await mod._rebase_locked({"path": "/r"}) == {
        "ok": False, "error": "worktree has uncommitted changes",
    }


@pytest.mark.asyncio
async def test_rebase_locked_fetch_failure(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else None

    monkeypatch.setattr(mod, "_git", fake_git)
    res = await mod._rebase_locked({"path": "/r"})
    assert res["ok"] is False
    assert res["error"] == "git fetch origin main failed"


@pytest.mark.asyncio
async def test_rebase_locked_success(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else "ok"

    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "", "")))
    monkeypatch.setattr(
        mod, "_git_info", AsyncMock(return_value={"head": "abc1234", "behind": 0})
    )
    assert await mod._rebase_locked({"path": "/r"}) == {
        "ok": True, "rebased": True, "head": "abc1234", "behind": 0,
    }


@pytest.mark.asyncio
async def test_rebase_locked_conflict_aborted(monkeypatch):
    async def fake_git(path, *args, **kw):
        return "" if args[0] == "status" else "ok"

    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "CONFLICT", "in f.py")))
    res = await mod._rebase_locked({"path": "/r"})
    assert res["ok"] is False and res["conflict"] is True
    assert "aborted" in res["error"]


@pytest.mark.asyncio
async def test_rebase_locked_conflict_with_failed_abort(monkeypatch):
    """A failed --abort must never be reported as 'aborted'."""
    async def fake_git(path, *args, **kw):
        if args[0] == "status":
            return ""
        if args[0] == "rebase":
            return None
        return "ok"

    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "CONFLICT", "")))
    res = await mod._rebase_locked({"path": "/r"})
    assert res["conflict"] is True
    assert "manual recovery required" in res["error"]


# --------------------------------------------------------------------------
# prune verdicts
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_prunable_dirty_check_failure(monkeypatch):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=None))
    v = await mod._prunable("/nope/missing", "feat")
    assert v == {**v, "ok": False, "code": "dirty_check_failed"}
    assert v["age_h"] is None


@pytest.mark.asyncio
async def test_prunable_merged_but_dirty(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"}))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=True))
    assert (await mod._prunable(str(tmp_path), "feat"))["code"] == "merged_dirty"


@pytest.mark.asyncio
async def test_prunable_merged_unverified_when_oid_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"}))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_fetch_pr_head_oid", AsyncMock(return_value=None))
    assert (await mod._prunable(str(tmp_path), "feat"))["code"] == "merged_unverified"


@pytest.mark.asyncio
async def test_prunable_merged_with_new_commits(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"}))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(mod, "_fetch_pr_head_oid", AsyncMock(return_value="bbb"))
    monkeypatch.setattr(mod, "_head_contained_in_pr", AsyncMock(return_value=False))
    assert (await mod._prunable(str(tmp_path), "feat"))["code"] == "merged_new_commits"


@pytest.mark.asyncio
async def test_prunable_merged_clean_is_candidate(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value={"state": "MERGED"}))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=False))
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(mod, "_fetch_pr_head_oid", AsyncMock(return_value="aaa"))
    monkeypatch.setattr(mod, "_head_contained_in_pr", AsyncMock(return_value=True))
    v = await mod._prunable(str(tmp_path), "feat")
    assert v["ok"] is True and v["code"] == "merged"


@pytest.mark.asyncio
async def test_prunable_fresh_empty_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=False))
    v = await mod._prunable(str(tmp_path), "feat")
    assert v["ok"] is False and v["code"] == "fresh"


@pytest.mark.asyncio
async def test_prunable_active_worktree_is_kept(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value={"state": "OPEN"}))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=3))
    monkeypatch.setattr(mod, "_real_dirty", AsyncMock(return_value=False))
    assert (await mod._prunable(str(tmp_path), "feat"))["code"] == "active"


@pytest.mark.asyncio
async def test_prune_candidates_splits_and_skips_main(monkeypatch):
    monkeypatch.setattr(
        mod, "_discover_worktrees",
        AsyncMock(return_value=[
            {"path": "/r", "is_main": True},
            {"path": "/r/wt-a", "branch": "a"},
            {"path": "/r/wt-b", "branch": "b"},
        ]),
    )

    async def fake_prunable(path, branch):
        ok = branch == "a"
        return {"ok": ok, "code": "merged" if ok else "active"}

    monkeypatch.setattr(mod, "_prunable", fake_prunable)
    out = await mod._prune_candidates()
    assert out["scanned"] == 2
    assert [c["name"] for c in out["candidates"]] == ["wt-a"]
    assert [k["name"] for k in out["kept"]] == ["wt-b"]


# --------------------------------------------------------------------------
# fleet cache helpers
# --------------------------------------------------------------------------
def test_drop_worktrees_ignores_malformed_payload():
    assert mod._drop_worktrees({"worktrees": "nope"}, {"x"}) == {"worktrees": "nope"}


def test_drop_worktrees_returns_same_object_when_nothing_matches():
    data = {"worktrees": [{"name": "a"}]}
    assert mod._drop_worktrees(data, {"z"}) is data


def test_drop_worktrees_copies_without_named_rows():
    data = {"worktrees": [{"name": "a"}, {"name": "b"}], "other": 1}
    out = mod._drop_worktrees(data, {"a"})
    assert out["worktrees"] == [{"name": "b"}]
    assert out["other"] == 1
    assert data["worktrees"] == [{"name": "a"}, {"name": "b"}]


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_ignores_cancellation():
    async def _never():
        await asyncio.sleep(30)

    task = asyncio.create_task(_never())
    await asyncio.sleep(0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    mod._log_fleet_rebuild_failure(task)  # must not raise


@pytest.mark.asyncio
async def test_log_fleet_rebuild_failure_warns_on_exception(caplog):
    async def _boom():
        raise RuntimeError("rebuild died")

    task = asyncio.create_task(_boom())
    try:
        await task
    except RuntimeError:
        pass
    with caplog.at_level("WARNING", logger=mod.logger.name):
        mod._log_fleet_rebuild_failure(task)
    assert "rebuild died" in caplog.text


# --------------------------------------------------------------------------
# GET handlers
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fleet_handler_reports_discovery_error(monkeypatch):
    monkeypatch.setattr(mod, "_fleet_cached", AsyncMock(side_effect=RuntimeError("no git")))
    resp = await mod.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet"))
    assert json.loads(resp.text) == {"worktrees": [], "error": "no git"}


@pytest.mark.asyncio
async def test_fleet_handler_fresh_bypasses_cache(monkeypatch):
    refresh = AsyncMock(return_value={"worktrees": [{"name": "a"}]})
    monkeypatch.setattr(mod, "_fleet_refresh", refresh)
    monkeypatch.setattr(mod, "_fleet_cached", AsyncMock(return_value={"worktrees": []}))
    resp = await mod.api_dev_fleet_fleet(make_mocked_request("GET", "/api/fleet?fresh=1"))
    assert json.loads(resp.text)["worktrees"] == [{"name": "a"}]
    refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_worktree_handler_requires_name():
    resp = await mod.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree"))
    assert resp.status == 400
    assert "missing 'name'" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await mod.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert resp.status == 400
    assert "unknown worktree" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_handler_returns_detail(monkeypatch):
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    monkeypatch.setattr(mod, "_worktree_detail", AsyncMock(return_value={"name": "x"}))
    resp = await mod.api_dev_fleet_worktree(make_mocked_request("GET", "/api/worktree?name=x"))
    assert json.loads(resp.text) == {"name": "x"}


@pytest.mark.asyncio
async def test_pod_logs_handler_requires_name():
    resp = await mod.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_pod_logs_handler_rejects_unknown_name(monkeypatch):
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value=set()))
    resp = await mod.api_dev_fleet_pod_logs(make_mocked_request("GET", "/api/pod/logs?name=x"))
    assert resp.status == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("query", "expected_n"),
    [("", 120), ("&n=notanint", 120), ("&n=0", 1), ("&n=99999", 1000), ("&n=25", 25)],
)
async def test_pod_logs_handler_clamps_line_count(monkeypatch, query, expected_n):
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"x"}))
    seen: list[int] = []

    async def fake_logs(name, n):
        seen.append(n)
        return {"ok": True, "logs": ""}

    monkeypatch.setattr(mod, "_pod_logs", fake_logs)
    await mod.api_dev_fleet_pod_logs(
        make_mocked_request("GET", f"/api/pod/logs?name=x{query}")
    )
    assert seen == [expected_n]


@pytest.mark.asyncio
async def test_run_handler_requires_id():
    resp = await mod.api_dev_fleet_run(make_mocked_request("GET", "/api/run"))
    assert resp.status == 400


@pytest.mark.asyncio
async def test_run_handler_unknown_id_is_404(monkeypatch):
    monkeypatch.setattr(mod, "_RUNS", {})
    resp = await mod.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=nope"))
    assert resp.status == 404


@pytest.mark.asyncio
async def test_run_handler_tails_and_redacts_output(monkeypatch):
    lines = [f"line {i}" for i in range(80)]
    monkeypatch.setattr(mod, "_RUNS", {"r1": {"status": "running", "output": lines}})
    resp = await mod.api_dev_fleet_run(make_mocked_request("GET", "/api/run?id=r1"))
    payload = json.loads(resp.text)
    assert len(payload["output"]) == 60
    assert payload["output"][0] == "line 20"


@pytest.mark.asyncio
async def test_prune_and_disk_handlers_pass_through(monkeypatch):
    monkeypatch.setattr(mod, "_prune_candidates", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(mod, "_prune_status", AsyncMock(return_value={"running": False}))
    monkeypatch.setattr(mod, "_disk", AsyncMock(return_value={"status": "idle"}))

    r1 = await mod.api_dev_fleet_prune_candidates(
        make_mocked_request("GET", "/api/prune-candidates")
    )
    r2 = await mod.api_dev_fleet_prune_status(make_mocked_request("GET", "/api/prune-status"))
    r3 = await mod.api_dev_fleet_disk(make_mocked_request("GET", "/api/disk"))
    assert json.loads(r1.text) == {"ok": True}
    assert json.loads(r2.text) == {"running": False}
    assert json.loads(r3.text) == {"status": "idle"}


# --------------------------------------------------------------------------
# _json_body + POST handler validation
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_json_body_rejects_invalid_json():
    body, err = await mod._json_body(_raw_request(b"{oops", json_error=ValueError("bad")))
    assert body is None and err is not None and err.status == 400


@pytest.mark.asyncio
async def test_json_body_rejects_non_object():
    body, err = await mod._json_body(_raw_request(b"[1, 2]"))
    assert body is None and err is not None
    assert "must be an object" in json.loads(err.text)["error"]


@pytest.mark.asyncio
async def test_json_body_empty_request_is_empty_dict():
    request = MagicMock()
    request.content_length = 0
    body, err = await mod._json_body(request)
    assert body == {} and err is None


@pytest.mark.asyncio
async def test_worktree_remove_handler_rejects_non_bool_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    resp = await mod.api_dev_fleet_worktree_remove(
        _json_request({"name": "feat", "force": "yes"})
    )
    assert resp.status == 400
    assert "force must be a boolean" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_worktree_remove_handler_forwards_force(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"feat"}))
    remove = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mod, "_worktree_remove", remove)

    resp = await mod.api_dev_fleet_worktree_remove(_json_request({"name": "feat", "force": True}))
    assert resp.status == 200
    remove.assert_awaited_once_with("feat", True)


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_invalid_json(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_prune_run(_raw_request(b"{", json_error=ValueError("bad")))
    assert resp.status == 400
    assert json.loads(resp.text)["ok"] is False


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_object_body(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_prune_run(_raw_request(b"[]"))
    assert resp.status == 400
    assert "must be an object" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_non_string_names(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_prune_run(_json_request({"names": ["a", 7]}))
    assert resp.status == 400
    assert "list of strings" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_prune_run_handler_rejects_when_no_name_is_valid(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"other"}))
    resp = await mod.api_dev_fleet_prune_run(_json_request({"names": ["ghost"]}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "no valid names"


@pytest.mark.asyncio
async def test_prune_run_handler_filters_to_valid_names(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_valid_worktree_names", AsyncMock(return_value={"a"}))
    run = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mod, "_prune_run", run)

    resp = await mod.api_dev_fleet_prune_run(_json_request({"names": ["a", "ghost"]}))
    assert resp.status == 200
    run.assert_awaited_once_with(["a"])


@pytest.mark.asyncio
async def test_pod_name_action_rejects_empty_name(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_pod_up(_json_request({"name": ""}))
    assert resp.status == 400
    assert "non-empty string" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_pod_name_action_rejects_ambiguous_basename(monkeypatch):
    """_find_worktree, not set membership, is the validator (collision safety)."""
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        mod, "_find_worktree", AsyncMock(return_value=(None, "ambiguous name 'feat'"))
    )
    resp = await mod.api_dev_fleet_pod_restart(_json_request({"name": "feat"}))
    assert resp.status == 400
    assert json.loads(resp.text)["error"] == "ambiguous name 'feat'"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "action_name"),
    [
        ("api_dev_fleet_pod_up", "_pod_up"),
        ("api_dev_fleet_pod_down", "_pod_down"),
        ("api_dev_fleet_pod_restart", "_pod_restart"),
        ("api_dev_fleet_pod_token", "_pod_token"),
        ("api_dev_fleet_pod_provision", "_pod_provision"),
        ("api_dev_fleet_rebase", "_rebase"),
    ],
)
async def test_pod_handlers_dispatch_to_their_action(monkeypatch, handler_name, action_name):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=({"path": "/w"}, None)))
    action = AsyncMock(return_value={"ok": True, "via": action_name})
    monkeypatch.setattr(mod, action_name, action)

    resp = await getattr(mod, handler_name)(_json_request({"name": "feat"}))
    assert json.loads(resp.text) == {"ok": True, "via": action_name}
    action.assert_awaited_once_with("feat")


@pytest.mark.asyncio
async def test_sync_handler_maps_already_running_to_409(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(
        mod, "_sync", AsyncMock(return_value={"ok": False, "error": "sync already running"})
    )
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await mod.api_dev_fleet_sync(request)
    assert resp.status == 409


@pytest.mark.asyncio
async def test_sync_handler_success_is_200(monkeypatch):
    _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_sync", AsyncMock(return_value={"ok": True}))
    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    resp = await mod.api_dev_fleet_sync(request)
    assert resp.status == 200


# --------------------------------------------------------------------------
# make-live handler
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_make_live_handler_requires_path(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_make_live(_json_request({"path": ""}))
    assert resp.status == 400
    assert "non-empty string" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_make_live_handler_rejects_non_bool_dry_run(monkeypatch):
    _sel_capture(monkeypatch)
    resp = await mod.api_dev_fleet_make_live(_json_request({"path": "/w", "dry_run": "1"}))
    assert resp.status == 400
    assert "dry_run must be a boolean" in json.loads(resp.text)["error"]


@pytest.mark.asyncio
async def test_make_live_handler_forwards_dry_run(monkeypatch):
    sink = _sel_capture(monkeypatch)
    make_live = AsyncMock(return_value={"ok": True, "dry_run": True})
    monkeypatch.setattr(mod, "_make_live", make_live)

    resp = await mod.api_dev_fleet_make_live(_json_request({"path": "/w", "dry_run": True}))
    assert resp.status == 200
    make_live.assert_awaited_once_with("/w", True)
    assert sink.events[0]["resources"] == "/w"


# --------------------------------------------------------------------------
# _audited
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_audited_bodyless_request_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 0
    request.can_read_body = False
    await handler(request)
    assert sink.events[0]["resources"] == ""
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_joins_list_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_json_request({"names": ["a", "b", "c"]}))
    assert sink.events[0]["resources"] == "a,b,c"


@pytest.mark.asyncio
async def test_audited_ignores_unparsable_body(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"not json at all"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_non_dict_body_has_empty_target(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    await handler(_raw_request(b"[1,2,3]"))
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_read_failure_does_not_break_handler(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"ok": True})

    request = MagicMock()
    request.content_length = 5
    request.can_read_body = True
    request.read = AsyncMock(side_effect=RuntimeError("stream gone"))
    await handler(request)
    assert sink.events[0]["resources"] == ""


@pytest.mark.asyncio
async def test_audited_handler_exception_is_audited_and_reraised(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        raise KeyError("kaboom")

    with pytest.raises(KeyError):
        await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "KeyError"


@pytest.mark.asyncio
async def test_audited_server_error_is_failure(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({"error": "internal"}, status=500)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "failure"
    assert sink.events[0]["error"] == "internal"


@pytest.mark.asyncio
async def test_audited_non_json_response_still_audits_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.Response(text="plain body")

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_non_dict_json_response_is_success(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response([1, 2])

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "success"


@pytest.mark.asyncio
async def test_audited_error_free_denial_falls_back_to_status(monkeypatch):
    sink = _sel_capture(monkeypatch)

    @mod._audited("probe")
    async def handler(request):
        return web.json_response({}, status=403)

    await handler(_json_request({"name": "feat"}))
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["error"] == "http_403"


@pytest.mark.asyncio
async def test_audited_preserves_handler_identity():
    async def original(request):
        """Docstring stays."""
        return web.json_response({})

    wrapped = mod._audited("probe")(original)
    assert wrapped.__name__ == "original"
    assert wrapped.__doc__ == "Docstring stays."


# --------------------------------------------------------------------------
# HMAC middleware denials
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hmac_health_path_is_exempt(monkeypatch):
    called: list[bool] = []

    async def handler(request):
        called.append(True)
        return web.json_response({"status": "ok"})

    await mod.hmac_proxy_middleware(make_mocked_request("GET", "/health"), handler)
    assert called == [True]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "secret", "reason"),
    [
        ({}, "", "no app secret configured"),
        ({}, "s3cr3t", "missing X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "nocolon"}, "s3cr3t", "malformed X-KiroCrew-Proxy header"),
        ({"X-KiroCrew-Proxy": "abc:sig"}, "s3cr3t", "invalid timestamp in proxy header"),
        ({"X-KiroCrew-Proxy": "1:sig"}, "s3cr3t", "proxy signature expired"),
    ],
)
async def test_hmac_denials(monkeypatch, headers, secret, reason):
    sink = _sel_capture(monkeypatch)
    monkeypatch.setattr(mod, "_load_app_secret", lambda: secret)

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    request = make_mocked_request("GET", "/api/fleet", headers=headers)
    resp = await mod.hmac_proxy_middleware(request, handler)
    assert resp.status == 401
    assert reason in json.loads(resp.text)["error"]
    assert sink.events[0]["outcome"] == "denied"
    assert sink.events[0]["tool_name"] == "dev-fleet:proxy-hmac"


@pytest.mark.asyncio
async def test_hmac_denial_survives_audit_sink_failure(monkeypatch, caplog):
    """A broken SEL sink must never mask the 401."""
    def _boom():
        raise RuntimeError("sel down")

    monkeypatch.setattr(mod, "_sel", _boom)
    monkeypatch.setattr(mod, "_load_app_secret", lambda: "s3cr3t")

    async def handler(request):  # pragma: no cover - must never run
        raise AssertionError("handler must not be reached")

    with caplog.at_level("WARNING", logger=mod.logger.name):
        resp = await mod.hmac_proxy_middleware(
            make_mocked_request("GET", "/api/fleet"), handler
        )
    assert resp.status == 401
    assert "SEL emit failed" in caplog.text


# --------------------------------------------------------------------------
# gateway identity helpers
# --------------------------------------------------------------------------
def test_gateway_unit_name_defaults_to_live_unit(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert mod._gateway_unit_name() == mod._LIVE_GATEWAY_UNIT


def test_gateway_unit_name_uses_pod_instance(monkeypatch, tmp_path):
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    assert mod._gateway_unit_name() == "kirocrew-pod@feat.service"


def test_gateway_unit_name_falls_back_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert mod._gateway_unit_name() == mod._LIVE_GATEWAY_UNIT


def test_gateway_label_defaults_to_live_agent(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path / "home")
    assert mod._gateway_label() == mod._LIVE_GATEWAY_LABEL


def test_gateway_label_uses_pod_agent(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    pod_config = pytest.importorskip("kiro_crew.pod.config")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.delenv("KIROCREW_POD_UNIT_PREFIX", raising=False)
    expected = f"{launchd.LABEL_PREFIX}.{pod_config.DEFAULT_UNIT_PREFIX}.feat"
    assert mod._gateway_label() == expected


def test_gateway_label_honours_unit_prefix_override(monkeypatch, tmp_path):
    launchd = pytest.importorskip("kiro_crew.pod.launchd")
    home = tmp_path / ".kirocrew-pods" / "feat"
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: home)
    monkeypatch.setenv("KIROCREW_POD_UNIT_PREFIX", "altplane")
    assert mod._gateway_label() == f"{launchd.LABEL_PREFIX}.altplane.feat"


@pytest.mark.parametrize(
    ("home_parts", "expected"),
    [((".kirocrew-pods", "feat"), True), (("home", "kirocrew"), False)],
)
def test_in_pod_detection(monkeypatch, tmp_path, home_parts, expected):
    monkeypatch.setattr(
        "kiro_crew.config.loader.config_dir", lambda: tmp_path.joinpath(*home_parts)
    )
    assert mod._in_pod() is expected


def test_in_pod_is_none_when_home_unresolvable(monkeypatch):
    def _boom():
        raise RuntimeError("no home")

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", _boom)
    assert mod._in_pod() is None


def test_foreground_backend_is_none_off_posix(monkeypatch):
    monkeypatch.setattr(mod.sys, "platform", "win32")
    assert mod._foreground_backend() is None


# --------------------------------------------------------------------------
# drop-in rollback + path selector
# --------------------------------------------------------------------------
def test_restore_dropin_deletes_when_there_was_none(tmp_path):
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("[Service]\n", encoding="utf-8", newline="\n")
    assert mod._restore_dropin(dropin, None) is True
    assert not dropin.exists()


def test_restore_dropin_rewrites_prior_content(tmp_path):
    dropin = tmp_path / "make-live.conf"
    dropin.write_text("new\n", encoding="utf-8", newline="\n")
    assert mod._restore_dropin(dropin, "prior\n") is True
    assert dropin.read_text(encoding="utf-8") == "prior\n"


def test_restore_dropin_reports_failure(monkeypatch, tmp_path):
    def _boom(path, content):
        raise OSError("read-only fs")

    monkeypatch.setattr(gateway_service, "atomic_write_text", _boom)
    assert mod._restore_dropin(tmp_path / "make-live.conf", "prior") is False


@pytest.mark.asyncio
async def test_find_worktree_by_path_requires_path():
    wt, err = await mod._find_worktree_by_path("")
    assert wt is None
    assert err is not None and "non-empty string" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_unknown_path(monkeypatch, tmp_path):
    monkeypatch.setattr(mod, "_discover_worktrees", AsyncMock(return_value=[]))
    wt, err = await mod._find_worktree_by_path(str(tmp_path / "nope"))
    assert wt is None
    assert err is not None and "not a known worktree" in err


@pytest.mark.asyncio
async def test_find_worktree_by_path_matches_known_worktree(monkeypatch, tmp_path):
    wanted = tmp_path / "kirocrew-wt-alpha"
    wanted.mkdir()
    monkeypatch.setattr(
        mod, "_discover_worktrees",
        AsyncMock(return_value=[{"path": str(tmp_path / "other")}, {"path": str(wanted)}]),
    )
    wt, err = await mod._find_worktree_by_path(str(wanted))
    assert err is None
    assert wt is not None and _same(wt["path"], str(wanted))


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_cleanup_kills_runs_and_cancels_workers(monkeypatch):
    async def _never():
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    refresher = asyncio.create_task(_never())
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4242
    kill_tree = AsyncMock()
    monkeypatch.setattr(mod, "_kill_tree", kill_tree)
    monkeypatch.setattr(mod, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(mod, "_refresher_task", refresher)
    monkeypatch.setattr(mod, "_warm_task", None)
    monkeypatch.setattr(mod, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())

    kill_tree.assert_awaited_once_with(4242)
    proc.kill.assert_called_once()
    assert mod._ACTIVE_RUNS == {}
    assert run_task.cancelled() or run_task.done()
    assert refresher.cancelled() or refresher.done()
    assert mod._refresher_task is None


@pytest.mark.asyncio
async def test_cleanup_tolerates_already_dead_process(monkeypatch):
    async def _never():
        await asyncio.sleep(30)

    run_task = asyncio.create_task(_never())
    await asyncio.sleep(0)

    proc = MagicMock()
    proc.returncode = None
    proc.pid = 77
    proc.kill.side_effect = ProcessLookupError
    monkeypatch.setattr(mod, "_kill_tree", AsyncMock())
    monkeypatch.setattr(mod, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(mod, "_refresher_task", None)
    monkeypatch.setattr(mod, "_warm_task", None)
    monkeypatch.setattr(mod, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    assert mod._ACTIVE_RUNS == {}


@pytest.mark.asyncio
async def test_cleanup_skips_finished_process(monkeypatch):
    async def _done():
        return None

    run_task = asyncio.create_task(_done())
    await run_task

    proc = MagicMock()
    proc.returncode = 0
    kill_tree = AsyncMock()
    monkeypatch.setattr(mod, "_kill_tree", kill_tree)
    monkeypatch.setattr(mod, "_ACTIVE_RUNS", {"r1": (run_task, proc)})
    monkeypatch.setattr(mod, "_refresher_task", None)
    monkeypatch.setattr(mod, "_warm_task", None)
    monkeypatch.setattr(mod, "_reaper_task", None)

    await mod.dev_fleet_cleanup(MagicMock())
    kill_tree.assert_not_awaited()
    proc.kill.assert_not_called()


# --------------------------------------------------------------------------
# app factory + entry point
# --------------------------------------------------------------------------
def test_create_app_registers_lifecycle_hooks_by_name():
    app = mod.create_app()
    startup_names = [getattr(h, "__name__", "") for h in app.on_startup]
    cleanup_names = [getattr(h, "__name__", "") for h in app.on_cleanup]
    assert "dev_fleet_startup" in startup_names
    assert "dev_fleet_cleanup" in cleanup_names


def test_create_app_exposes_health_on_both_paths():
    app = mod.create_app()
    paths = {
        r.resource.canonical
        for r in app.router.routes()
        if r.resource is not None
    }
    assert {"/health", "/api/health"} <= paths
    assert "/api/make-live" in paths


def test_main_runs_the_app_on_loopback(monkeypatch):
    seen: dict = {}

    def fake_run_app(app, host=None, port=None, print=None):
        seen.update({"app": app, "host": host, "port": port})

    monkeypatch.setattr(mod.web, "run_app", fake_run_app)
    assert mod.main() == 0
    assert seen["host"] == "127.0.0.1"
    assert seen["port"] == mod.PORT


# --------------------------------------------------------------------------
# worktree detail
# --------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_worktree_detail_unknown_name(monkeypatch):
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=(None, "gone")))
    assert await mod._worktree_detail("ghost") == {"error": "gone"}


@pytest.mark.asyncio
async def test_worktree_detail_includes_pod_state_and_design_docs(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": "feat", "is_main": False}
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        mod, "_git_info",
        AsyncMock(return_value={
            "branch": "feat", "head": "abc1234", "dirty": False,
            "ahead": 0, "behind": 0, "last_updated_at": None,
        }),
    )
    monkeypatch.setattr(mod, "_pr_status_cached", AsyncMock(return_value=None))
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=1))
    monkeypatch.setattr(mod, "_context_cached", AsyncMock(return_value={}))

    async def fake_git(path, *args, **kw):
        if args[0] == "log":
            return "abc1234\x1ffeat: do a thing\x1f2 hours ago\nmalformed line"
        if args[0] == "diff":
            return "docs/design/plan.md\nsrc/app.py\ndocs/design/plan.md\n"
        return ""

    monkeypatch.setattr(mod, "_git", fake_git)
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(0, "31\t.", "")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: SimpleNamespace())
    monkeypatch.setattr(mod, "_POD_AVAILABLE", True)
    name = Path(str(tmp_path)).name
    monkeypatch.setattr(
        mod, "rt",
        SimpleNamespace(
            active_names=lambda cfg: {name},
            derive_port=lambda cfg, nm: 9321,
        ),
        raising=False,
    )

    detail = await mod._worktree_detail(name)
    assert detail["pod_running"] is True
    assert detail["pod_port"] == 9321
    assert detail["disk_mb"] == 31
    assert detail["commits"] == [
        {"hash": "abc1234", "subject": "feat: do a thing", "when": "2 hours ago"}
    ]
    assert detail["design_docs"] == ["docs/design/plan.md"]


@pytest.mark.asyncio
async def test_worktree_detail_survives_pod_probe_failure(monkeypatch, tmp_path):
    wt = {"path": str(tmp_path), "branch": None, "is_main": True}
    monkeypatch.setattr(mod, "_find_worktree", AsyncMock(return_value=(wt, None)))
    monkeypatch.setattr(
        mod, "_git_info",
        AsyncMock(return_value={
            "branch": "main", "head": "abc1234", "dirty": False,
            "ahead": 0, "behind": 0, "last_updated_at": None,
        }),
    )
    monkeypatch.setattr(mod, "_own_commits_count", AsyncMock(return_value=0))
    monkeypatch.setattr(mod, "_context_cached", AsyncMock(return_value={}))
    monkeypatch.setattr(mod, "_git", AsyncMock(return_value=""))
    monkeypatch.setattr(mod, "_run_cmd", AsyncMock(return_value=(1, "", "du failed")))
    monkeypatch.setattr(mod, "_load_cfg", lambda: None)

    detail = await mod._worktree_detail(Path(str(tmp_path)).name)
    assert detail["pod_running"] is False
    assert detail["disk_mb"] is None
    assert detail["commits"] == []
