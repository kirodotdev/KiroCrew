"""Dev Fleet make-live cutover: service values, launchd, rollback, locking and cancel.

Covers systemd value escaping, the launchd cutover, pointer rollback on a failed
restart, the single-flight cutover lock, pointer-based live resolution and the
staged-cutover cancel.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _sweep_staged_sync_tempdirs  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _mk_make_live_wt, _stub_make_live

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.testing.wait import await_until


# --- make-live: systemd value escaping / unsafe_path ---
def test_sd_value_escapes_and_conditionally_quotes():
    """A clean path is emitted verbatim; `%` specifiers double to `%%`; only
    whitespace/metacharacters trigger double-quoting (with \\ and " escaped)."""
    assert mod._sd_value("/clean/path-1.2/bin") == "/clean/path-1.2/bin"
    assert mod._sd_value("/a b/c") == '"/a b/c"'  # whitespace -> quoted
    assert mod._sd_value("/a%b") == "/a%%b"  # specifier doubled, unquoted
    assert mod._sd_value("/a %b") == '"/a %%b"'  # both
    assert mod._sd_value('/a"b') == '"/a\\"b"'  # embedded quote escaped
    assert mod._sd_value("/a\\b") == '"/a\\\\b"'  # embedded backslash escaped


def test_sd_value_rejects_control_char_paths():
    """Newline / NUL / tab / other C0 control chars are rejected outright."""
    for bad in ["/a\nb", "/a\x00b", "/a\tb", "/a\x1fb", "/a\x7fb", "/a\rb"]:
        with pytest.raises(mod._UnsafeUnitValue):
            mod._sd_value(bad)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_escapes_special_char_worktree(monkeypatch, tmp_path):
    """A worktree path with a space yields a valid plan and a real cutover
    writes the resolved path into the pointer file correctly."""
    name = "kirocrew-wt feat"
    wt = tmp_path / name
    (wt / ".venv" / "bin").mkdir(parents=True)
    _kc = wt / ".venv" / "bin" / "kirocrew"
    _kc.write_text("#!/bin/sh\n")
    _kc.chmod(0o755)
    dd = wt / "src" / "kiro_crew" / "static" / "dist"
    dd.mkdir(parents=True)
    (dd / "index.html").write_text("<html></html>")
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    # dry-run plan names the pointer path and correct exec.
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["exec"] == str(wt / ".venv" / "bin" / "kirocrew")
    # Real cutover writes the pointer with the resolved (space-containing) path.
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    res2 = await mod._make_live(str(wt), dry_run=False)
    assert res2["ok"] is True and res2.get("cutover") is True
    import json as _json

    data = _json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()


@pytest.mark.asyncio
async def test_make_live_unsafe_path_returns_code(monkeypatch, tmp_path):
    """When live_target.validate raises InvalidTarget (from _make_live_plan),
    _make_live refuses with code `unsafe_path` and touches nothing."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)

    def boom(raw):
        raise live_mod.live_target.InvalidTarget("control chars in path")

    monkeypatch.setattr(live_mod.live_target, "validate", boom)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is False and res["code"] == "unsafe_path"
    assert calls == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
async def test_restart_gateway_darwin_requests_graceful_stop(monkeypatch):
    """macOS restart asks launchd for a bounded SIGTERM-first stop.

    Pinned as a DOMAIN-TARGETED ``kill TERM``, not launchctl's legacy label-only
    ``stop``: Dev Fleet spawns every command inside ``sandbox-exec``, and launchd
    refuses the legacy stop routine for a sandboxed caller ("Not privileged to
    stop service.") whatever the seatbelt profile allows. A regression to the
    legacy form makes Restart fail on every Mac, so the shape is asserted here.
    """
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod.gateway_service, "restart_contract_current", lambda _path: True)
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: True
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  state = running\n  pid = 4242\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is True
    # The PID stands in for systemd's monotonic start stamp: it changes on every
    # respawn, which is the edge the frontend handshake waits for.
    assert res["start_id"] == "4242"
    # Addressed via the backend's own domain helper rather than a second copy of
    # the uid logic (which would also break on Windows, where getuid is absent).
    domain = live_mod.gateway_service.LaunchdBackend.domain()
    assert ["launchctl", "kill", "TERM", f"{domain}/{mod._gateway_label()}"] in calls
    # `kickstart -k` would restart it too, but as a hard kill rather than the
    # graceful SIGTERM the ExitTimeOut budget is sized for.
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)
    # The legacy verb is what the sandbox blocks — it must not reappear.
    assert not any(c[:2] == ["launchctl", "stop"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_refuses_stale_loaded_contract(monkeypatch):
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(live_mod.gateway_service, "restart_contract_current", lambda _path: True)
    monkeypatch.setattr(
        live_mod.gateway_service, "loaded_restart_contract_current", lambda _out: False
    )
    calls = []

    async def fake_run_cmd(cmd, **_kw):
        calls.append(cmd)
        return (0, "pid = 4242\n", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is False
    assert "loaded launchd restart contract is outdated" in res["error"]
    assert not any(c[:3] == ["launchctl", "kill", "TERM"] for c in calls)


@pytest.mark.asyncio
async def test_restart_gateway_darwin_not_active(monkeypatch):
    """No loaded agent -> refuse, without attempting a kickstart."""
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="darwin"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/bin/launchctl"))
    )
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_ACTIVE", None, raising=False)
    monkeypatch.setattr(live_mod, "_GATEWAY_SERVICE_CHECK_AT", 0.0, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (1, "", "no such service")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._restart_gateway()
    assert res["ok"] is False
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)


# The launchd cutover tests create a REAL symlink, because the mechanism being
# verified IS the atomic symlink swap. The code under test is macOS-only and
# Windows has no unprivileged symlink creation, so they are POSIX-gated. They
# still run on Linux CI and on the macOS job (whose glob now includes this file),
# so gating costs no coverage.
_posix_symlink_only = pytest.mark.skipif(
    os.name != "posix",
    reason="creates a real symlink to verify the launchd cutover; macOS-only code",
)


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_writes_pointer_and_stops_agent(monkeypatch, tmp_path):
    """A macOS cutover writes the pointer, then asks launchd to stop the agent."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is True and res.get("cutover") is True
    # Pointer file written with the resolved checkout.
    ptr_file = ptr_dir / "live_target.json"
    assert ptr_file.is_file()
    import json as _json

    data = _json.loads(ptr_file.read_text())
    assert Path(data["checkout"]).resolve() == wt.resolve()
    assert any(c[:3] == ["launchctl", "kill", "TERM"] for c in calls)
    assert not any(c[:2] == ["launchctl", "kickstart"] for c in calls)
    assert not any("bootout" in c or "bootstrap" in c for c in calls)


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_rolls_back_pointer_on_restart_failure(monkeypatch, tmp_path):
    """A rejected launchd stop restores the prior pointer state.

    Leaving the new pointer in place would silently activate the new checkout
    on the NEXT unrelated restart.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        if cmd[:3] == ["launchctl", "kill", "TERM"]:
            return (1, "", "restart refused")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    # Pointer rolled back: prior was absent, so the rollback UNPINS by publishing
    # the absent-equivalent stub, never an absent name -- the mask cannot cover
    # a name that does not exist.
    ptr_file = ptr_dir / "live_target.json"
    assert ptr_file.read_text(encoding="utf-8") == live_mod.live_target.NO_TARGET_DOCUMENT
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_darwin_dry_run_plan(monkeypatch, tmp_path):
    """The macOS dry-run plan uses the same pointer mechanism and mutates nothing."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    plan = res["plan"]
    assert plan["mechanism"] == "live-target pointer"
    assert plan["pointer_path"] == str(ptr_dir / "live_target.json")
    assert plan["target"] == str(wt)
    assert calls == []
    assert not (ptr_dir / "live_target.json").exists()


@pytest.mark.asyncio
@_posix_symlink_only
async def test_make_live_refuses_when_prior_pointer_is_unreadable(monkeypatch, tmp_path):
    """An unreadable prior pointer aborts BEFORE anything is staged.

    ``restore(None)`` means "there was nothing here" and UNPINS the pointer (the
    absent-equivalent stub replaces it), so treating a read failure as absent
    would let a failed cutover destroy a live pointer it merely could not read.
    The abort must happen before staging.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)
    # Make snapshot() raise PermissionError (simulating an unreadable pointer).
    monkeypatch.setattr(
        live_mod.live_target,
        "snapshot",
        lambda: (_ for _ in ()).throw(PermissionError("unreadable")),
    )
    calls: list = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(cmd)
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "write_failed"
    # No restart issued.
    assert not any(c[:2] == ["systemd-run", "--user"] for c in calls)
    assert mod._MAKE_LIVE_COMMITTED is False


# --- make-live: pointer write + failure rollback ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_rolls_back_pointer_on_restart_failure(monkeypatch, tmp_path):
    """A restart failure with a prior pointer restores the PRIOR content and
    reports rolled_back. When there was no prior pointer, the file is deleted."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    # Prior was absent -> rollback unpins with the absent-equivalent stub.
    assert (ptr_dir / "live_target.json").read_text(
        encoding="utf-8"
    ) == live_mod.live_target.NO_TARGET_DOCUMENT
    assert mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_rolls_back_pointer_preserves_prior(monkeypatch, tmp_path):
    """When a prior pointer existed, restart failure restores its content."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir(parents=True)
    ptr_file = ptr_dir / "live_target.json"
    prior_content = '{"checkout": "/old/checkout"}\n'
    ptr_file.write_text(prior_content)
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res = await mod._make_live(str(wt), dry_run=False)
    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    assert ptr_file.read_text() == prior_content


# --- make-live: concurrency single-flight lock ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_concurrent_second_call_busy(monkeypatch, tmp_path):
    """While one cutover holds the make-live lock, a concurrent second call is
    refused immediately with ``busy`` (fail-fast, not queued) and the winner
    still completes and releases the lock."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    # Fresh lock so a leaked hold from another test can't poison this one.
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    entered = asyncio.Event()  # set once the first call is inside the lock
    release = asyncio.Event()  # test-controlled gate to hold it there

    # write_target runs inside the critical section, so signalling from it is an
    # exact barrier: no sleep can substitute, because a loaded runner may not
    # have reached the lock yet and the assertion below would then read an
    # unlocked lock and let the second call through.
    original_write_target = live_mod.live_target.write_target

    def signalling_write(checkout, *, previous_checkout=None):
        result = original_write_target(
            checkout,
            previous_checkout=previous_checkout,
        )
        entered.set()
        return result

    monkeypatch.setattr(live_mod.live_target, "write_target", signalling_write)

    async def fake_run_cmd(cmd, **kw):
        # Block here (inside the lock) until released.
        if cmd[:2] == ["systemd-run", "--user"]:
            await release.wait()
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)

    first = asyncio.ensure_future(mod._make_live(str(wt), dry_run=False))
    await await_until(entered, what="make_live entered lock")
    assert mod._MAKE_LIVE_LOCK.locked() is True

    # Second call returns busy without waiting.
    busy = await await_until(
        asyncio.ensure_future(mod._make_live(str(wt), dry_run=False)),
        what="second make_live call done",
    )
    assert busy["ok"] is False and busy["code"] == "busy"
    assert "in progress" in busy["error"]

    release.set()
    res = await await_until(first, what="first make_live done")
    assert res["ok"] is True and res.get("cutover") is True
    assert mod._MAKE_LIVE_LOCK.locked() is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_lock_released_after_failure_and_reusable(monkeypatch, tmp_path):
    """The lock is released on the failure-rollback path too, so a subsequent
    cutover proceeds (never wedged on ``busy``)."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_LOCK", asyncio.Lock())

    # 1) restart fails -> rollback path; the lock MUST be released.
    async def restart_fails(cmd, **kw):
        if cmd[:2] == ["systemd-run", "--user"]:
            return (1, "", "run boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", restart_fails)
    fail = await mod._make_live(str(wt), dry_run=False)
    assert fail["ok"] is False and fail["code"] == "restart_failed"
    assert mod._MAKE_LIVE_LOCK.locked() is False

    # 2) A subsequent all-green cutover proceeds (not refused as busy) and also
    #    releases the lock on the success path.
    async def all_ok(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", all_ok)
    ok = await mod._make_live(str(wt), dry_run=False)
    assert ok["ok"] is True and ok.get("cutover") is True
    assert mod._MAKE_LIVE_LOCK.locked() is False


# --- make-live: pointer-based live worktree resolution ---
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_prefers_pointer_over_service(monkeypatch, tmp_path):
    """_live_worktree_path returns the pointer target in preference to the
    service definition ONCE THE GATEWAY IS RUNNING IT. A cutover writes the
    pointer without touching the definition, so the definition would report the
    stale install checkout."""
    target_wt = tmp_path / "kirocrew-wt-new"
    target_wt.mkdir(parents=True)
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)

    # Write a real pointer file that points at target_wt.
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir(parents=True)
    ptr_file = ptr_dir / "live_target.json"
    import json as _json

    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    # Even with a systemd probe that would return a different path, the pointer
    # takes priority.
    monkeypatch.setattr(live_mod, "sys", MagicMock(platform="linux"))
    monkeypatch.setattr(
        live_mod, "shutil", MagicMock(which=MagicMock(return_value="/usr/bin/systemctl"))
    )

    async def should_not_be_called(cmd, **kw):
        raise AssertionError(f"systemctl should not be called: {cmd}")

    monkeypatch.setattr(runtime_mod, "_run_cmd", should_not_be_called)
    # The gateway is executing the pointer target: the cutover has taken effect.
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: target_wt.resolve())
    got = await mod._live_worktree_path()
    assert got == str(target_wt.resolve())
    assert mod._staged_target() is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_reports_running_image_while_staged(monkeypatch, tmp_path):
    """A staged pointer is NOT live: until the gateway restarts it is still
    executing the previous checkout, and reporting the pointer as live would
    tell the operator a cutover landed while old code serves real data."""
    target_wt = tmp_path / "kirocrew-wt-new"
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)
    running_wt = tmp_path / "kirocrew-running"
    running_wt.mkdir(parents=True)

    ptr_file = tmp_path / "ptr" / "live_target.json"
    ptr_file.parent.mkdir(parents=True)
    import json as _json

    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running_wt)

    got = await mod._live_worktree_path()
    assert got == str(running_wt), "live must name the image actually executing"
    assert got != str(target_wt.resolve())
    assert mod._staged_target() == str(target_wt.resolve())


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_honours_pointer_when_checkout_unknown(monkeypatch, tmp_path):
    """A packaged install is not a checkout, so the running image cannot be
    compared. That is "cannot verify", not a mismatch: the pointer still names the
    live row (so removal guards keep protecting it), but it is marked UNVERIFIED
    so the fleet view does not assert the gateway runs from it."""
    target_wt = tmp_path / "kirocrew-wt-new"
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)

    ptr_file = tmp_path / "ptr" / "live_target.json"
    ptr_file.parent.mkdir(parents=True)
    import json as _json

    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: None)
    monkeypatch.setattr(live_mod, "_LIVE_VERIFIED", True, raising=False)
    monkeypatch.setattr(live_mod, "_POINTER_PROVIDER", None)

    assert await mod._live_worktree_path() == str(target_wt.resolve())
    assert mod._staged_target() is None
    state = await live_mod.pointer_state()
    assert state.live == str(target_wt.resolve())
    assert state.live_verified is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_live_worktree_path_is_verified_when_the_running_checkout_matches(
    monkeypatch, tmp_path
):
    """The pointer agrees with the checkout being executed: that is verified, and a
    previous unverified answer must not stick."""
    target_wt = tmp_path / "kirocrew-wt-new"
    (target_wt / ".venv" / "bin").mkdir(parents=True)
    (target_wt / ".venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n")
    (target_wt / ".venv" / "bin" / "kirocrew").chmod(0o755)
    (target_wt / "src" / "kiro_crew").mkdir(parents=True)
    ptr_file = tmp_path / "ptr" / "live_target.json"
    ptr_file.parent.mkdir(parents=True)
    import json as _json

    ptr_file.write_text(_json.dumps({"checkout": str(target_wt)}) + "\n")

    monkeypatch.setattr(live_mod.live_target, "pointer_path", lambda: ptr_file)
    monkeypatch.setattr(live_mod, "_LIVE_CHECK_AT", 0.0, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_WORKTREE", None, raising=False)
    monkeypatch.setattr(live_mod, "_LIVE_VERIFIED", False, raising=False)
    monkeypatch.setattr(live_mod, "_POINTER_PROVIDER", None)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: target_wt.resolve())

    state = await live_mod.pointer_state()
    assert state.live == str(target_wt.resolve())
    assert state.live_verified is True


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_repointing_at_the_running_checkout_cancels_a_staged_cutover(monkeypatch, tmp_path):
    """While a cutover is staged, naming the checkout that is RUNNING is a cancel.

    Without this the operator has no un-stage route on exactly the host class this
    feature serves: `already_live` refuses (the running image IS that checkout) and
    the UI hides Make live on live rows, so the only ways out are to complete the
    cutover into the wrong code and reverse it — two manual restarts — or to
    hand-delete a keystone-fenced file the product never names.
    """
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()

    # The running image IS `running`, so the already_live branch is the one reached.
    # A host this app cannot drive -- exactly the `service install` case this is
    # about, and the only class where the pointer-only cancel applies.
    _stub_make_live(
        monkeypatch, running, live=str(running), pointer_dir=ptr_dir, unit_status="no_user_unit"
    )
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)

    # A cutover to a DIFFERENT checkout is staged.
    import json as _json

    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running))

    assert res.get("ok") is True, res
    assert res.get("cancelled") is True, res
    assert res.get("code") != "already_live"
    # The pointer is RE-PINNED to the running checkout, not deleted: deleting it
    # would discard the record that this checkout is the chosen live target.
    assert ptr.exists(), "cancelling must not delete the live-target record"
    assert live_mod.live_target.read_target() == running.resolve()
    assert mod._staged_target() is None


def _stage_a_cutover(monkeypatch, tmp_path):
    """running checkout + a pointer staged at a DIFFERENT checkout."""
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()
    # A host this app cannot drive -- exactly the `service install` case this is
    # about, and the only class where the pointer-only cancel applies.
    _stub_make_live(
        monkeypatch, running, live=str(running), pointer_dir=ptr_dir, unit_status="no_user_unit"
    )
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)
    import json as _json

    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(_json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)
    return running, other, ptr


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cutover_unwind_runs_off_the_event_loop(monkeypatch, tmp_path):
    """The rollback must not block the loop.

    restore() ends in restrict_to_owner, whose Windows DACL write can block on
    a network volume round-trip, and svc.rollback() rewrites the service
    definition. Run inline, an unwind would stall every other gateway request
    for the duration of that blocking file IO, so it has to reach the executor
    like the write it is undoing.
    """
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, unit_status="no_user_unit")

    # Force the cutover write to fail so the unwind path runs.
    monkeypatch.setattr(
        live_mod.live_target,
        "write_target",
        lambda _c, **_kw: (_ for _ in ()).throw(OSError(28, "No space")),
    )
    loop_thread = threading.get_ident()
    restore_threads: list = []
    monkeypatch.setattr(
        live_mod.live_target,
        "restore",
        lambda prior: restore_threads.append(threading.get_ident()) or True,
    )

    res = await mod._make_live(str(wt))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    assert restore_threads, "the unwind must have run"
    # The thread identity is the real evidence: observing that
    # subprocess_executor() was called proves nothing, since the cutover write
    # already uses it.
    assert all(
        t != loop_thread for t in restore_threads
    ), "restore ran on the event-loop thread — the unwind was not offloaded"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_drivable_host_with_a_stage_pending_refuses(monkeypatch, tmp_path):
    """On a host Dev Fleet CAN drive, this request must do NOTHING destructive.

    Two wrong answers to avoid. The pointer-only cancel is unsafe here: a drivable
    host also stages a service DEFINITION, so re-pinning just the pointer leaves
    the definition naming a checkout nobody intends to run, and once that is
    pruned the unit fails to start before it ever reads the pointer. But falling
    through to the full cutover is worse -- it bounces a live gateway carrying
    real sessions in response to a request that reads as "keep running what is
    already running". So it refuses and names both real exits.
    """
    running = _mk_make_live_wt(tmp_path / "running", venv=True, dist=True)
    other = _mk_make_live_wt(tmp_path / "other", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    ptr_dir.mkdir()
    _stub_make_live(
        monkeypatch, running, live=str(running), pointer_dir=ptr_dir, unit_status="ok"
    )  # drivable
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: running)
    ptr = live_mod.live_target.pointer_path()
    ptr.write_text(json.dumps({"checkout": str(other)}) + "\n")
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running))

    # Neither the pointer-only cancel...
    assert res.get("ok") is False, res
    assert res.get("cancelled") is not True, res
    assert res.get("plan", {}).get("action") != "cancel_staged_cutover", res
    assert res.get("code") == "staged_cutover_pending", res
    # ...nor a cutover: no definition written, and the staged pointer is intact.
    assert not mod._dropin_path().exists(), "a refusal must not write a definition"
    assert mod._staged_target() == str(other), "the stage must survive untouched"
    # The message names the staged checkout so the operator knows both exits.
    assert other.name in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_stale_cancel_after_stage_completed_never_becomes_a_cutover(monkeypatch, tmp_path):
    """The destructive variant of the two-tab race.

    Stage B COMPLETES while tab A's cancel dialog is open: nothing is staged
    any more and B is live. Tab A's stale POST names checkout A (which was
    live) — without the entry gate it would fall past both cancel branches
    into the FULL CUTOVER path and restart the gateway back into A. A request
    carrying expected_staged must refuse (stage_changed) the moment the stage
    it names is gone, whatever branch it would otherwise reach.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    # Complete the staged cutover: `other` is the running checkout now and the
    # pointer agrees with it, so nothing is staged (and the live path resolves
    # to `other` for the same_as_running branch below the gate).
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: other)
    monkeypatch.setattr(live_mod, "_live_worktree_path", AsyncMock(return_value=str(other)))
    assert mod._staged_target() is None

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    assert "nothing is staged now" in res["error"]
    # Crucially: no cutover side effects — no service definition written and
    # the pointer still names the completed target.
    assert not mod._dropin_path().exists()
    assert live_mod.live_target.read_target() == other.resolve()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_stale_cancel_refuses_when_the_live_checkout_moved(monkeypatch, tmp_path):
    """The other stale-cancel variant: the LIVE side moved.

    A cancel re-pins the checkout the operator saw as live. If a cutover to C
    landed and a new stage appeared while the dialog sat open, the stale
    request's path names a checkout that is not running — matching the
    (re-created) stage alone would let it fall through to the cutover path
    and restart the gateway into the old checkout. The live binding refuses.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    # The gateway moved to a third checkout; the SAME stage happens to exist.
    third = _mk_make_live_wt(tmp_path / "third", venv=True, dist=True)
    monkeypatch.setattr(live_mod, "_running_checkout", lambda: third)
    monkeypatch.setattr(live_mod, "_live_worktree_path", AsyncMock(return_value=str(third)))
    assert mod._staged_target() == str(other)

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    assert "live checkout changed" in res["error"]
    # No cutover side effects: nothing written, the stage survives.
    assert not mod._dropin_path().exists()
    assert mod._staged_target() == str(other)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_refuses_when_the_stage_changed_since_confirm(monkeypatch, tmp_path):
    """A cancel is bound to the stage the operator confirmed.

    Two dashboards race: tab A opens the cancel dialog against stage A, tab B
    cancels A and stages B, tab A submits. Without the binding the POST names
    only the live checkout, so it would discard stage B — a stage tab A's
    operator never saw. With ``expected_staged`` the backend refuses instead.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    confirmed_against = tmp_path / "some-older-stage"

    res = await mod._make_live(str(running), expected_staged=str(confirmed_against))

    assert res.get("ok") is False, res
    assert res.get("code") == "stage_changed", res
    # The current stage survives untouched, and the message names both sides.
    assert mod._staged_target() == str(other)
    assert other.name in res["error"]
    assert confirmed_against.name in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_with_matching_expected_stage_proceeds(monkeypatch, tmp_path):
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running), expected_staged=str(other))

    assert res.get("cancelled") is True, res
    assert mod._staged_target() is None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_keeps_a_pointer_selected_checkout_live(monkeypatch, tmp_path):
    """The scenario that makes deletion wrong.

    Checkout A was made live BY the pointer (so the installed build is something
    else). Staging B and then cancelling must leave A as the live target — if the
    cancel deleted the pointer, the next restart would boot the installed build
    instead of A, silently undoing a cutover the operator never asked to undo.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running))

    assert res.get("cancelled") is True, res
    assert res["plan"]["keeps_live_target"] == str(running)
    # The record survives AND still names the running checkout.
    assert ptr.exists()
    assert live_mod.live_target.read_target() == running.resolve()
    # Nothing is staged any more, so no restart is pending.
    assert mod._staged_target() is None


@pytest.mark.parametrize(
    "boom",
    [
        OSError(28, "No space left on device"),
        OSError(30, "Read-only file system"),
    ],
)
@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_write_failure_is_a_refusal_not_a_crash(monkeypatch, tmp_path, boom):
    """A full or read-only data home must refuse, not raise into a 500.

    write_target mkdirs, writes atomically and re-applies the owner-only mode, so
    the failure mode here is OSError — which the InvalidTarget guard alone (a
    ValueError) does not cover.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise boom

    monkeypatch.setattr(live_mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    assert "could not be re-pinned" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_rolls_the_pointer_back_when_hardening_fails(monkeypatch, tmp_path):
    """write_target can fail AFTER replacing the pointer.

    It re-applies the owner-only mode as its last step, so a failure there leaves
    a code-execution input in place with inherited permissions. The cancel must be
    all-or-nothing: the staged pointer goes back exactly as it was.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    staged_before = ptr.read_text(encoding="utf-8")

    real_write = live_mod.live_target.write_target

    def write_then_fail(checkout):
        real_write(checkout)  # the pointer IS replaced
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(live_mod.live_target, "write_target", write_then_fail)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res
    # Rolled back byte-for-byte: the stage is still staged, nothing half-applied.
    assert ptr.read_text(encoding="utf-8") == staged_before
    assert mod._staged_target() is not None


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_reports_a_failed_rollback(monkeypatch, tmp_path):
    """When the rollback itself fails the operator is told, not left guessing."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def write_then_fail(_checkout):
        raise OSError(5, "SetNamedSecurityInfo failed")

    monkeypatch.setattr(live_mod.live_target, "write_target", write_then_fail)
    monkeypatch.setattr(live_mod.live_target, "restore", lambda _prior: False)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert "rollback also failed" in res["error"]


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_invalid_target_is_a_refusal_not_a_crash(monkeypatch, tmp_path):
    """The validation half of the same guard."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    def explode(_checkout):
        raise live_mod.live_target.InvalidTarget("no src/kiro_crew in target")

    monkeypatch.setattr(live_mod.live_target, "write_target", explode)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "write_failed", res


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_dry_run_cancel_reports_the_plan_without_deleting(monkeypatch, tmp_path):
    """`dry_run` must never mutate.

    The already_live check runs BEFORE the dry_run return because it is
    validation; turning that point into a pointer delete would make a dry run
    destroy a staged cutover it was only asked to describe.
    """
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    res = await mod._make_live(str(running), dry_run=True)

    assert res.get("ok") is True, res
    assert res.get("dry_run") is True
    assert res.get("cancelled") is not True, "dry run must not claim to have acted"
    assert res["plan"]["action"] == "cancel_staged_cutover"
    assert res["plan"]["staged_target"] == str(other)
    assert ptr.exists(), "dry run must NOT delete the pointer"
    assert mod._staged_target() == str(other)


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_fails_fast_while_a_cutover_holds_the_lock(monkeypatch, tmp_path):
    """The cancel mutates the same pointer a cutover writes, so it takes the
    same single-flight lock and reports `busy` instead of racing it."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)

    async with mod._MAKE_LIVE_LOCK:
        res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "busy", res
    assert ptr.exists(), "a contended cancel must not delete the pointer"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_cancel_refuses_once_a_cutover_has_committed(monkeypatch, tmp_path):
    """A committed cutover is already restarting; deleting the pointer then would
    land the pending restart somewhere the operator did not choose."""
    running, other, ptr = _stage_a_cutover(monkeypatch, tmp_path)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", True, raising=False)

    res = await mod._make_live(str(running))

    assert res.get("ok") is False, res
    assert res.get("code") == "restart_pending", res
    assert ptr.exists()


@pytest.mark.asyncio
async def test_loaded_but_inactive_user_unit_is_not_drivable(monkeypatch):
    """A loaded unit is not necessarily the RUNNING gateway.

    `systemctl --user cat` succeeding only proves the unit is known. On a host
    whose gateway runs in the foreground or as a system unit, an idle --user unit
    would otherwise pass the make-live gate: the cutover would bounce that unit,
    the real gateway would keep serving the old code, and the UI would run its
    restart handshake to a false success.
    """
    backend = MagicMock()
    backend.status = AsyncMock(return_value="ok")
    backend.active = AsyncMock(return_value=False)
    monkeypatch.setattr(live_mod, "_gateway_backend", lambda: backend)

    assert await mod._live_user_unit_status() == "user_unit_inactive"
    # The operator-facing reason must say why, not leak the code.
    reason = mod._make_live_status_error("user_unit_inactive")
    assert "not running" in reason
    assert "(user_unit_inactive)" not in reason
    # And it composes into the staged notice, which leads with the remedy.
    notice = mod._staged_notice("main", "user_unit_inactive")
    assert notice.index("kirocrew restart") < notice.index("not running")


@pytest.mark.asyncio
async def test_loaded_and_active_user_unit_is_drivable(monkeypatch):
    """The positive control: loaded AND running still reports ok, so this gate
    did not simply become unreachable."""
    backend = MagicMock()
    backend.status = AsyncMock(return_value="ok")
    backend.active = AsyncMock(return_value=True)
    monkeypatch.setattr(live_mod, "_gateway_backend", lambda: backend)

    assert await mod._live_user_unit_status() == "ok"


def test_running_checkout_resolves_this_checkout():
    """_running_checkout derives the executing checkout from the loaded module,
    which is what makes it authoritative where a service definition is not."""
    got = mod._running_checkout()
    assert got is not None
    assert (got / "src" / "kiro_crew").is_dir()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_make_live_staged_only_allows_subsequent_cutover(monkeypatch, tmp_path):
    """A staged_only cutover does NOT latch, so re-pointing to a DIFFERENT
    worktree proceeds without a restart_pending refusal."""
    wt1 = _mk_make_live_wt(tmp_path / "a", venv=True, dist=True)
    wt2 = _mk_make_live_wt(tmp_path / "b", venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt1, unit_status="no_user_unit", pointer_dir=ptr_dir)
    monkeypatch.setattr(live_mod, "_MAKE_LIVE_COMMITTED", False, raising=False)

    async def fake_run_cmd(cmd, **kw):
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    res1 = await mod._make_live(str(wt1), dry_run=False)
    assert res1["ok"] is True and res1["staged_only"] is True
    assert mod._MAKE_LIVE_COMMITTED is False

    # Second cutover to wt2 also succeeds (not refused as restart_pending).
    _stub_make_live(monkeypatch, wt2, unit_status="no_user_unit", pointer_dir=ptr_dir)
    res2 = await mod._make_live(str(wt2), dry_run=False)
    assert res2["ok"] is True and res2["staged_only"] is True
    # Pointer now points to wt2.
    import json as _json

    data = _json.loads((ptr_dir / "live_target.json").read_text())
    assert Path(data["checkout"]).resolve() == wt2.resolve()
