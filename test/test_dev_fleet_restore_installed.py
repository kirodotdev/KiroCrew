"""Dev Fleet "Restore installed build": unpin the live target and restart.

Pins the contract of ``restore_installed._restore_installed``: after a real Make Live it
removes the systemd drop-in, resets the pointer to the absent-equivalent document and
restarts through the service backend; it refuses with a code when nothing is pinned,
on launchd with no installer record, in the backend process and inside a pod; and a
failed restart unwinds both surfaces exactly like a failed cutover (the launchd twins
live in ``test_dev_fleet_restore_installed_launchd.py``). The last test pins WHY this is the way
back to a release channel: with the pointer cleared the gateway runs without
``KIROCREW_PROJECT_DIR`` set to the worktree, so the install layout is the
installer's, not ``git``.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _POSIX_ONLY, _mk_make_live_wt, _stub_make_live

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import restore_installed
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.platform import update_capability, update_layout
from kiro_crew.service import live_target
from kiro_crew.service.macos import render_live_program


def _recording_run_cmd(monkeypatch, *, restart_rc: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:1] == ["systemd-run"]:
            return (restart_rc, "", "" if restart_rc == 0 else "boom")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    return calls


async def _make_live_first(monkeypatch, tmp_path):
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    _recording_run_cmd(monkeypatch)
    res = await mod._make_live(str(wt))
    assert res["ok"] is True and res.get("cutover") is True
    live_mod._MAKE_LIVE_COMMITTED = False  # the fresh gateway the restart spawns
    return wt, ptr_dir / "live_target.json", mod._dropin_path()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_restore_removes_dropin_clears_pointer_and_restarts(monkeypatch, tmp_path):
    _wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    assert dropin.exists() and json.loads(ptr.read_text())["checkout"]
    calls = _recording_run_cmd(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["restored"] is True
    assert not dropin.exists()
    assert ptr.read_text() == live_target.NO_TARGET_DOCUMENT
    assert live_target.is_pinned() is False
    assert ["systemctl", "--user", "daemon-reload"] in calls
    restarts = [c for c in calls if c[:1] == ["systemd-run"]]
    assert len(restarts) == 1 and restarts[0][-2] == "restart"
    reload_at = calls.index(["systemctl", "--user", "daemon-reload"])
    assert reload_at < calls.index(restarts[0]), "reload must precede the restart"
    assert live_mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_a_second_request_after_commit_is_restart_pending(monkeypatch, tmp_path):
    await _make_live_first(monkeypatch, tmp_path)
    _recording_run_cmd(monkeypatch)
    assert (await restore_installed._restore_installed())["ok"] is True
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "restart_pending"


@pytest.mark.asyncio
async def test_nothing_pinned_is_refused_and_nothing_runs(monkeypatch, tmp_path):
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, pointer_dir=tmp_path / "ptr")
    calls = _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res == {
        "ok": False,
        "code": "no_live_target",
        "error": "no live target is set; the gateway already runs the installed build",
    }
    assert not [c for c in calls if c[:1] == ["systemd-run"]]
    assert not (tmp_path / "ptr" / "live_target.json").exists()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_a_cleared_pointer_with_a_leftover_dropin_still_restores(monkeypatch, tmp_path):
    """The manual half-unpin (pointer cleared by hand, drop-in left) is still state
    that boots the worktree; Restore must clear it rather than say nothing is set."""
    _wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    assert live_target.restore(None)
    _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is True and not dropin.exists()


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_dry_run_mutates_nothing(monkeypatch, tmp_path):
    _wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    before = ptr.read_text(), dropin.read_text()
    calls = _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed(dry_run=True)
    assert res["ok"] is True and res["dry_run"] is True
    assert res["plan"]["clears_dropin"] is True and res["plan"]["restart"] == "service"
    assert (ptr.read_text(), dropin.read_text()) == before
    assert calls == []
    assert live_mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_failed_restart_unwinds_pointer_and_dropin(monkeypatch, tmp_path):
    _wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    before = ptr.read_text(), dropin.read_text()
    calls = _recording_run_cmd(monkeypatch, restart_rc=1)

    res = await restore_installed._restore_installed()

    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    assert (ptr.read_text(), dropin.read_text()) == before
    # The restored override is reloaded so the loaded unit matches the disk again.
    assert calls[-1] == ["systemctl", "--user", "daemon-reload"]
    assert live_mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_failed_pointer_clear_unwinds_before_any_restart(monkeypatch, tmp_path):
    _wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    before = ptr.read_text(), dropin.read_text()
    calls = _recording_run_cmd(monkeypatch)
    real_restore = live_target.restore
    monkeypatch.setattr(
        live_target, "restore", lambda prior: False if prior is None else real_restore(prior)
    )
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "write_failed"
    assert (ptr.read_text(), dropin.read_text()) == before
    assert not [c for c in calls if c[:1] == ["systemd-run"]]


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_launchd_without_a_record_is_refused_and_touches_nothing(monkeypatch, tmp_path):
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, platform="darwin")
    live_target.write_target(wt)
    # The launcher a launchd Make Live rewrote for the worktree, with no record of the
    # installer's: nothing can write the installed build's launcher back.
    launcher = tmp_path / "launchd" / "live-gateway"
    launcher.parent.mkdir()
    launcher.write_text(
        render_live_program(
            str(wt / ".venv" / "bin" / "kirocrew"),
            working_dir=str(wt),
            path_prefix=[str(wt / ".venv" / "bin")],
        )
    )
    monkeypatch.setattr(live_mod.gateway_service, "launchd_live_program", lambda: launcher)
    before = (ptr_dir / "live_target.json").read_text()
    calls = _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "restore_no_installer_record"
    assert (ptr_dir / "live_target.json").read_text() == before
    assert calls == []


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_refused_reload_after_removing_a_dropin_is_not_success(monkeypatch, tmp_path):
    """The drop-in is removed but the manager still holds the worktree definition, so the
    owner's own restart would boot the worktree again: unwind even when nothing restarts here."""
    wt, ptr, dropin = await _make_live_first(monkeypatch, tmp_path)
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr.parent, unit_status="no_user_unit")
    before_ptr, before_dropin = ptr.read_text(), dropin.read_text()
    _recording_run_cmd(monkeypatch)
    monkeypatch.setattr(
        live_mod.gateway_service.SystemdBackend, "reload", AsyncMock(return_value=False)
    )
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "write_failed"
    assert ptr.read_text() == before_ptr and dropin.read_text() == before_dropin


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_a_failing_reload_does_not_block_a_host_that_will_not_restart(monkeypatch, tmp_path):
    """No reachable user manager (`no_user_unit`): nothing restarts, so a refused
    daemon-reload changes nothing and the pointer is still cleared."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, unit_status="no_user_unit")
    live_target.write_target(wt)
    _recording_run_cmd(monkeypatch)
    monkeypatch.setattr(
        live_mod.gateway_service.SystemdBackend, "reload", AsyncMock(return_value=False)
    )
    res = await restore_installed._restore_installed()
    assert res["ok"] is True and res["staged_only"] is True
    assert (ptr_dir / "live_target.json").read_text() == live_target.NO_TARGET_DOCUMENT


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_undrivable_host_clears_the_pointer_and_names_the_command(monkeypatch, tmp_path):
    """A `service install` system unit: nothing here can bounce the gateway, so the
    pointer is cleared and the operator finishes with one command, as for a staged
    cutover."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, unit_status="user_unit_inactive")
    live_target.write_target(wt)
    calls = _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is True and res["staged_only"] is True
    assert res["manual_restart"] == "kirocrew restart"
    assert "kirocrew restart" in res["notice"]
    assert "not one inside a worktree folder" in res["notice"]
    assert "exists but is not running" not in res["notice"]
    assert (ptr_dir / "live_target.json").read_text() == live_target.NO_TARGET_DOCUMENT
    assert not [c for c in calls if c[:1] == ["systemd-run"]]
    assert live_mod._MAKE_LIVE_COMMITTED is False


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_foreground_host_is_never_respawned_into_the_worktree(monkeypatch, tmp_path):
    """A foreground gateway respawns the console script it was started from, which is
    the worktree's, so Restore clears the pointer and leaves the restart to the owner."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir, unit_status="no_user_unit")
    live_target.write_target(wt)
    fg = AsyncMock()
    fg.status.return_value = live_mod.gateway_service.STATUS_OK
    monkeypatch.setattr(live_mod, "_foreground_backend", lambda: fg)
    _recording_run_cmd(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is True and res["staged_only"] is True
    fg.restart_detached.assert_not_called()
    assert (ptr_dir / "live_target.json").read_text() == live_target.NO_TARGET_DOCUMENT
    assert live_mod._MAKE_LIVE_COMMITTED is False


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_failed_reload_unwinds_before_any_restart(monkeypatch, tmp_path):
    """daemon-reload failing leaves the manager on the cached worktree definition, so a
    restart would boot the worktree while reporting success."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, pointer_dir=ptr_dir)
    live_target.write_target(wt)
    before = (ptr_dir / "live_target.json").read_text()
    calls = _recording_run_cmd(monkeypatch)
    monkeypatch.setattr(
        live_mod.gateway_service.SystemdBackend, "reload", AsyncMock(return_value=False)
    )
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "write_failed"
    assert (ptr_dir / "live_target.json").read_text() == before
    assert not [c for c in calls if c[:1] == ["systemd-run"]]
    assert live_mod._MAKE_LIVE_COMMITTED is False


@_POSIX_ONLY
@pytest.mark.asyncio
async def test_backend_process_and_pod_are_refused(monkeypatch, tmp_path):
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    _stub_make_live(monkeypatch, wt, pointer_dir=tmp_path / "ptr", in_pod=True)
    live_target.write_target(wt)
    assert (await restore_installed._restore_installed())["code"] == "pod"

    async def _provider(fresh):
        raise AssertionError("never read")

    monkeypatch.setattr(live_mod, "_POINTER_PROVIDER", _provider)
    assert (await restore_installed._restore_installed())["code"] == "wrong_process"


@pytest.mark.asyncio
@_POSIX_ONLY
async def test_restore_hands_the_install_layout_back_to_the_installer(monkeypatch, tmp_path):
    """Why Restore unblocks the channel switch on a wheel install.

    While a worktree is live the gateway is exec'd with KIROCREW_PROJECT_DIR set to
    it, so the layout reads ``git`` and ``POST /api/update/channel`` answers
    ``channel_not_applicable_git``. After Restore the pointer is the
    absent-equivalent document: ``maybe_reexec`` returns without exec'ing, the
    gateway keeps the unit's own environment, and the layout is the wheel's.
    """
    _wt, ptr, _dropin = await _make_live_first(monkeypatch, tmp_path)
    worktree = live_target.read_target()
    assert worktree is not None
    monkeypatch.setattr(update_capability, "distribution", lambda: "wheel")
    monkeypatch.setattr(update_layout, "distribution", lambda: "wheel")
    monkeypatch.setattr(
        update_capability, "is_git_worktree", lambda root: bool(root) and root == str(worktree)
    )
    monkeypatch.setattr(update_capability, "running_from_checkout", lambda root: True)
    monkeypatch.delenv(live_target.EXEC_MARKER, raising=False)
    execs: list[dict] = []

    def fake_execve(path, argv, env):
        execs.append(env)

    monkeypatch.setattr(live_target.os, "execve", fake_execve)
    monkeypatch.setattr(live_target.os, "chdir", lambda p: None)

    live_target.maybe_reexec(["gateway"])
    assert execs and execs[0]["KIROCREW_PROJECT_DIR"] == str(worktree)
    monkeypatch.setenv("KIROCREW_PROJECT_DIR", execs[0]["KIROCREW_PROJECT_DIR"])
    assert update_layout.detect_install_layout().is_git is True

    _recording_run_cmd(monkeypatch)
    assert (await restore_installed._restore_installed())["ok"] is True
    # The restarted gateway: a fresh process from the base unit, no inherited override.
    monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
    execs.clear()
    live_target.maybe_reexec(["gateway"])
    assert execs == [], "a cleared pointer must boot the installed build"
    layout = update_layout.detect_install_layout()
    assert layout.is_git is False and layout.kind == "wheel"


def test_is_pinned_tracks_the_pointer_document(monkeypatch, tmp_path):
    ptr = tmp_path / "live_target.json"
    monkeypatch.setattr(live_target, "pointer_path", lambda: ptr)
    assert live_target.is_pinned() is False  # absent
    ptr.write_text(live_target.NO_TARGET_DOCUMENT)
    assert live_target.is_pinned() is False  # the mask stub
    ptr.write_text(json.dumps({"checkout": str(tmp_path / "pruned-worktree")}))
    assert live_target.is_pinned() is True  # unusable but still pinned
    ptr.write_text("{not json")
    assert live_target.is_pinned() is True  # malformed: state to clear, not absence
