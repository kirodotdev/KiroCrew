"""Dev Fleet "Restore installed build" on launchd: the installer launcher record.

The first launchd Make Live records the installer's ``live-gateway`` launcher beside the
pointer, never overwriting a record that exists; Restore writes it back, clears the
pointer, restarts through ``launchctl kill TERM`` and deletes the record; a failed
restart unwinds both surfaces and keeps the record; a missing or tampered record is
refused before anything is written. ``launchctl`` is faked the way the launchd cutover
tests fake it, and the launcher path is redirected under ``tmp_path``.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from dev_fleet_app_helpers import _reset_make_live_committed_latch  # noqa: F401 - fixture
from dev_fleet_app_helpers import _reset_shutdown_admission_state  # noqa: F401 - fixture
from dev_fleet_app_helpers import _venv_maps_to_the_synced_checkout  # noqa: F401 - fixture
from dev_fleet_app_helpers import _mk_make_live_wt, _stub_make_live

import kiro_crew.apps.builtins.dev_fleet.server as mod
from kiro_crew.apps.builtins.dev_fleet import installer_launcher
from kiro_crew.apps.builtins.dev_fleet import live as live_mod
from kiro_crew.apps.builtins.dev_fleet import restore_installed
from kiro_crew.apps.builtins.dev_fleet import runtime as runtime_mod
from kiro_crew.service import live_target
from kiro_crew.service.macos import render_live_program

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="launchd launcher modes and ownership are POSIX semantics"
)


def _launchctl(monkeypatch, *, kill_rc: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_run_cmd(cmd, **kw):
        calls.append(list(cmd))
        if cmd[:2] == ["launchctl", "print"]:
            return (0, "  pid = 99\n", "")
        if cmd[:3] == ["launchctl", "kill", "TERM"]:
            return (kill_rc, "", "" if kill_rc == 0 else "kill refused")
        return (0, "", "")

    monkeypatch.setattr(runtime_mod, "_run_cmd", fake_run_cmd)
    return calls


def _kills(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if c[:3] == ["launchctl", "kill", "TERM"]]


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A launchd host whose launcher is the installer's pass-through."""
    wt = _mk_make_live_wt(tmp_path, venv=True, dist=True)
    ptr_dir = tmp_path / "ptr"
    _stub_make_live(monkeypatch, wt, platform="darwin", pointer_dir=ptr_dir)
    launcher = tmp_path / "launchd" / "live-gateway"
    monkeypatch.setattr(live_mod.gateway_service, "launchd_live_program", lambda: launcher)
    installed = tmp_path / "installed" / "bin" / "kirocrew"
    installed.parent.mkdir(parents=True)
    installed.write_text("#!/bin/sh\n")
    installed.chmod(0o755)
    installer = render_live_program(str(installed))
    launcher.parent.mkdir(parents=True)
    launcher.write_text(installer)
    return {
        "wt": wt,
        "pointer": ptr_dir / "live_target.json",
        "record": ptr_dir / "live-target-staging" / "installer-launcher.json",
        "launcher": launcher,
        "installer": installer,
        "installed": installed,
    }


async def _cutover(monkeypatch, host) -> None:
    _launchctl(monkeypatch)
    res = await mod._make_live(str(host["wt"]))
    assert res["ok"] is True and res.get("cutover") is True
    live_mod._MAKE_LIVE_COMMITTED = False  # the fresh gateway the restart spawns


@pytest.mark.asyncio
async def test_first_cutover_records_the_installer_launcher_owner_only(monkeypatch, host):
    await _cutover(monkeypatch, host)
    record = host["record"]
    data = json.loads(record.read_text())
    assert data == {
        "version": 1,
        "live_program": str(host["launcher"]),
        "content": host["installer"],
    }
    st = os.lstat(record)
    assert stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1
    # The cutover itself still rewrote the launcher for the worktree.
    assert host["launcher"].read_text() != host["installer"]
    assert str(host["wt"]) in host["launcher"].read_text()


@pytest.mark.asyncio
async def test_a_newer_installer_launcher_replaces_an_older_record(monkeypatch, host, tmp_path):
    older = tmp_path / "older" / "kirocrew"
    older.parent.mkdir()
    older.write_text("#!/bin/sh\n")
    older.chmod(0o755)
    live_target.ensure_staging_dir()
    original = json.dumps(
        {
            "version": 1,
            "live_program": str(host["launcher"]),
            "content": render_live_program(str(older)),
        }
    )
    host["record"].write_text(original)
    host["record"].chmod(0o600)
    await _cutover(monkeypatch, host)
    assert host["record"].read_text() != original
    assert json.loads(host["record"].read_text())["content"] == host["installer"]


@pytest.mark.asyncio
async def test_a_cutover_launcher_never_replaces_the_record(monkeypatch, host):
    await _cutover(monkeypatch, host)
    before = host["record"].read_text()
    # A second cutover sees the first one's launcher, which is not the installer's.
    live_mod._MAKE_LIVE_COMMITTED = False
    assert (
        installer_launcher.record_first_cutover(host["launcher"], host["launcher"].read_text())
        is False
    )
    assert host["record"].read_text() == before


def test_a_make_live_launcher_or_a_pinned_pointer_is_not_recorded(monkeypatch, host):
    launcher = host["launcher"]
    make_live = render_live_program(
        str(host["wt"] / ".venv" / "bin" / "kirocrew"),
        working_dir=str(host["wt"]),
        path_prefix=[str(host["wt"] / ".venv" / "bin")],
    )
    assert installer_launcher.record_first_cutover(launcher, make_live) is False
    assert installer_launcher.record_first_cutover(launcher, None) is False
    live_target.write_target(host["wt"])
    # A pointer that is already pinned (a foreground cutover) does not block recording
    # an installer launcher whose binary lies outside the pinned checkout...
    assert installer_launcher.record_first_cutover(launcher, host["installer"]) is True
    host["record"].unlink()
    # ...but a launcher whose binary is inside it is a worktree's own install.
    inside = render_live_program(str(host["wt"] / ".venv" / "bin" / "kirocrew"))
    assert installer_launcher.record_first_cutover(launcher, inside) is False
    assert not host["record"].exists()


@pytest.mark.asyncio
async def test_restore_writes_back_the_launcher_restarts_and_forgets(monkeypatch, host):
    await _cutover(monkeypatch, host)
    calls = _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["restored"] is True
    assert res["plan"]["rewrites_launcher"] is True
    assert host["launcher"].read_text() == host["installer"]
    assert stat.S_IMODE(os.lstat(host["launcher"]).st_mode) == 0o700
    assert host["pointer"].read_text() == live_target.NO_TARGET_DOCUMENT
    assert len(_kills(calls)) == 1
    assert not host["record"].exists()
    assert live_mod._MAKE_LIVE_COMMITTED is True


@pytest.mark.asyncio
async def test_dry_run_on_launchd_mutates_nothing(monkeypatch, host):
    await _cutover(monkeypatch, host)
    before = host["pointer"].read_text(), host["launcher"].read_text()
    calls = _launchctl(monkeypatch)
    res = await restore_installed._restore_installed(dry_run=True)
    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is True
    assert (host["pointer"].read_text(), host["launcher"].read_text()) == before
    assert host["record"].exists() and not _kills(calls)


@pytest.mark.asyncio
async def test_failed_restart_unwinds_and_keeps_the_record(monkeypatch, host):
    await _cutover(monkeypatch, host)
    before = host["pointer"].read_text(), host["launcher"].read_text()
    record = host["record"].read_text()
    _launchctl(monkeypatch, kill_rc=1)

    res = await restore_installed._restore_installed()

    assert res["ok"] is False and res["code"] == "restart_failed"
    assert res["rolled_back"] is True
    assert (host["pointer"].read_text(), host["launcher"].read_text()) == before
    assert host["record"].read_text() == record
    assert live_mod._MAKE_LIVE_COMMITTED is False


@pytest.mark.asyncio
async def test_failed_launcher_write_unwinds_before_any_restart(monkeypatch, host):
    await _cutover(monkeypatch, host)
    before = host["pointer"].read_text(), host["launcher"].read_text()
    calls = _launchctl(monkeypatch)
    real = live_mod.gateway_service.write_live_program
    content = host["installer"]

    def flaky(text, path=None):
        if text == content:
            raise OSError("disk full")
        real(text, path)

    monkeypatch.setattr(live_mod.gateway_service, "write_live_program", flaky)
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "write_failed"
    assert (host["pointer"].read_text(), host["launcher"].read_text()) == before
    assert host["record"].exists() and not _kills(calls)


@pytest.mark.asyncio
async def test_a_pinned_host_with_no_record_is_refused(monkeypatch, host):
    await _cutover(monkeypatch, host)
    host["record"].unlink()
    before = host["pointer"].read_text(), host["launcher"].read_text()
    calls = _launchctl(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "restore_no_installer_record"
    assert (host["pointer"].read_text(), host["launcher"].read_text()) == before
    assert not _kills(calls)


@pytest.mark.asyncio
async def test_a_pinned_pointer_over_the_installer_launcher_clears_only_the_pointer(
    monkeypatch, host
):
    # A foreground or staged-only cutover pinned the pointer and never rewrote the
    # launcher, so there is no record and none is needed.
    live_target.write_target(host["wt"])
    assert not host["record"].exists()
    calls = _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["restored"] is True
    assert res["plan"]["rewrites_launcher"] is False
    assert host["launcher"].read_text() == host["installer"]
    assert host["pointer"].read_text() == live_target.NO_TARGET_DOCUMENT
    assert len(_kills(calls)) == 1


@pytest.mark.asyncio
async def test_a_worktree_service_install_launcher_is_restored_from_the_record(monkeypatch, host):
    # Make Live recorded the installer's launcher; the owner then ran `service install`
    # from the worktree's venv, leaving a bare launcher of the installer's shape that
    # execs the worktree. Template shape alone must not skip the record.
    await _cutover(monkeypatch, host)
    host["launcher"].write_text(render_live_program(str(host["wt"] / ".venv" / "bin" / "kirocrew")))
    calls = _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is True
    assert host["launcher"].read_text() == host["installer"]
    assert len(_kills(calls)) == 1


@pytest.mark.asyncio
async def test_a_restore_that_wrote_no_launcher_still_forgets_a_stale_record(monkeypatch, host):
    # Make Live recorded installation A; the owner then ran `service install` from
    # another installation, so the launcher is an installer's and needs no write-back.
    # The old record must not survive to overwrite it on a later restore.
    await _cutover(monkeypatch, host)
    other = host["installed"].parent.parent.parent / "other" / "bin" / "kirocrew"
    other.parent.mkdir(parents=True)
    other.write_text("#!/bin/sh\n")
    other.chmod(0o755)
    newer = render_live_program(str(other))
    host["launcher"].write_text(newer)
    assert host["record"].exists()
    _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is False
    assert host["launcher"].read_text() == newer
    assert not host["record"].exists()


@pytest.mark.asyncio
async def test_a_pruned_worktree_launcher_is_still_restored_from_the_record(monkeypatch, host):
    # The pinned worktree's console script is gone, so the pointer fails validation;
    # the worktree's own `service install` launcher must still not pass for the installer's.
    await _cutover(monkeypatch, host)
    binary = host["wt"] / ".venv" / "bin" / "kirocrew"
    host["launcher"].write_text(render_live_program(str(binary)))
    binary.unlink()
    assert live_target.read_target_reason()[0] is None and live_target.is_pinned()
    calls = _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is True
    assert host["launcher"].read_text() == host["installer"]
    assert len(_kills(calls)) == 1


@pytest.mark.asyncio
async def test_a_missing_launcher_is_recreated_from_the_record(monkeypatch, host):
    await _cutover(monkeypatch, host)
    host["launcher"].unlink()
    calls = _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is True
    assert host["launcher"].read_text() == host["installer"]
    assert len(_kills(calls)) == 1


@pytest.mark.asyncio
async def test_a_missing_launcher_without_a_record_is_refused_before_clearing(monkeypatch, host):
    live_target.write_target(host["wt"])
    host["launcher"].unlink()
    before = host["pointer"].read_text()
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "restore_no_installer_record"
    assert host["pointer"].read_text() == before


@pytest.mark.asyncio
async def test_a_foreground_mac_with_no_launcher_clears_only_the_pointer(monkeypatch, host):
    # No launchd agent was ever installed, so there is no launcher and no record, and
    # nothing for a launcher write-back to serve.
    _stub_make_live(
        monkeypatch,
        host["wt"],
        platform="darwin",
        pointer_dir=host["pointer"].parent,
        unit_status="no_agent",
    )
    live_target.write_target(host["wt"])
    host["launcher"].unlink()
    _launchctl(monkeypatch)

    res = await restore_installed._restore_installed()

    assert res["ok"] is True and res["plan"]["rewrites_launcher"] is False
    assert host["pointer"].read_text() == live_target.NO_TARGET_DOCUMENT
    assert not host["launcher"].exists()


def _tamper_mode(host):
    host["record"].chmod(0o644)


def _tamper_content(host):
    data = json.loads(host["record"].read_text())
    data["content"] = data["content"].replace("exec ", "cd /tmp || exit 1\nexec ")
    host["record"].write_text(json.dumps(data))


def _tamper_relative(host):
    data = json.loads(host["record"].read_text())
    data["content"] = render_live_program("kirocrew")
    host["record"].write_text(json.dumps(data))


def _tamper_program(host):
    data = json.loads(host["record"].read_text())
    data["live_program"] = "/elsewhere/live-gateway"
    host["record"].write_text(json.dumps(data))


def _tamper_shape(host):
    data = json.loads(host["record"].read_text())
    data["extra"] = True
    host["record"].write_text(json.dumps(data))


def _tamper_missing_bin(host):
    host["installed"].unlink()


def _tamper_symlink(host):
    real = host["record"].with_name("elsewhere.json")
    host["record"].rename(real)
    host["record"].symlink_to(real)


def _tamper_hardlink(host):
    os.link(host["record"], host["record"].with_name("second-name.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_mode,
        _tamper_content,
        _tamper_relative,
        _tamper_program,
        _tamper_shape,
        _tamper_missing_bin,
        _tamper_symlink,
        _tamper_hardlink,
    ],
)
async def test_an_untrustworthy_record_is_refused(monkeypatch, host, tamper):
    await _cutover(monkeypatch, host)
    tamper(host)
    before = host["pointer"].read_text(), host["launcher"].read_text()
    calls = _launchctl(monkeypatch)
    res = await restore_installed._restore_installed()
    assert res["ok"] is False and res["code"] == "restore_installer_record_invalid"
    assert (host["pointer"].read_text(), host["launcher"].read_text()) == before
    assert not _kills(calls)


@pytest.mark.asyncio
async def test_a_record_write_failure_refuses_the_cutover(monkeypatch, host):
    def boom(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(installer_launcher, "atomic_write", boom)
    calls = _launchctl(monkeypatch)
    res = await mod._make_live(str(host["wt"]))
    assert res["ok"] is False and res["code"] == "write_failed"
    assert host["launcher"].read_text() == host["installer"]
    assert not host["pointer"].exists() and not _kills(calls)
