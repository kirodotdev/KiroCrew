"""The live-row probe that reads the live checkout from the systemd unit.

``live._service_live_worktree`` asks the live unit for ``WorkingDirectory``, then
for the ``path=`` of ``ExecStart``, and keeps a path only when it is a checkout.
Every ``systemctl`` call goes to a scripted ``_run_cmd`` queue, and every file is
under ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from dev_fleet_app_helpers import _checkout

from kiro_crew.apps.builtins.dev_fleet import live, live_service, runtime


def _run_cmd_queue(monkeypatch, results: list[tuple[int, str, str]]) -> list[list[str]]:
    """Replace ``_run_cmd`` with a scripted queue; return the argv log."""
    seen: list[list[str]] = []

    async def _fake(cmd, **kwargs):
        seen.append(list(cmd))
        return results[len(seen) - 1] if len(seen) <= len(results) else (1, "", "")

    monkeypatch.setattr(runtime, "_run_cmd", _fake)
    return seen


@pytest.fixture(autouse=True)
def _isolate(_floor_monkeypatch):
    """Reset the live-row cache and the recorded running unit."""
    _floor_monkeypatch.setattr(live, "_LIVE_WORKTREE", None)
    _floor_monkeypatch.setattr(live, "_LIVE_CHECK_AT", 0.0)
    _floor_monkeypatch.setattr(live_service, "_LIVE_GATEWAY_UNIT_RUNNING", None)


@pytest.mark.asyncio
async def test_live_worktree_path_reads_an_execstart_path_with_a_space(monkeypatch, tmp_path):
    """systemctl puts `` ; `` between ExecStart fields, so a path with a space resolves whole."""
    checkout = _checkout(tmp_path / "check out")
    exe = checkout / ".venv" / "bin" / "kirocrew"
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "linux")
    monkeypatch.setattr(live.shutil, "which", lambda name: "/bin/systemctl")
    _run_cmd_queue(
        monkeypatch,
        [
            (0, "\n", ""),
            (
                0,
                f"ExecStart={{ path={exe} ; argv[]={exe} gateway --no-open ; ignore_errors=no }}",
                "",
            ),
        ],
    )

    assert await live._live_worktree_path(fresh=True) == str(checkout.resolve())


def _installer_unit_host(monkeypatch, tmp_path, exec_start: Path) -> tuple[Path, list[list[str]]]:
    """A systemd host with no pointer whose ``kirocrew.service`` is the installer's.

    That unit sets ``WorkingDirectory`` to the home directory, which is not a
    checkout. Returns the home directory and the ``systemctl`` argv log."""
    home = tmp_path / "home"
    home.mkdir()
    unit_dir = tmp_path / "xdg" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    (unit_dir / "kirocrew.service").write_text("[Service]\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setattr(live.live_target, "read_target", lambda: None)
    monkeypatch.setattr(live.sys, "platform", "linux")
    monkeypatch.setattr(live.shutil, "which", lambda name: "/bin/systemctl")
    seen = _run_cmd_queue(
        monkeypatch,
        [
            (0, f"{home}\n", ""),
            (0, f"ExecStart={{ path={exec_start} ; argv[]={exec_start} gateway }}", ""),
        ],
    )
    return home, seen


@pytest.mark.asyncio
async def test_live_worktree_path_is_the_running_checkout_when_the_unit_names_home(
    monkeypatch, tmp_path
):
    """The home directory that the installer's unit names is never the live row:
    with no checkout in the unit, the running checkout answers."""
    running = _checkout(tmp_path / "running")
    home, seen = _installer_unit_host(
        monkeypatch, tmp_path, tmp_path / "home" / ".local" / "bin" / "kirocrew"
    )
    monkeypatch.setattr(live, "_running_checkout", lambda: running)

    got = await live._live_worktree_path(fresh=True)

    assert got == str(running)
    assert got != str(home.resolve())
    assert [cmd[3] for cmd in seen] == ["kirocrew.service", "kirocrew.service"]


@pytest.mark.asyncio
async def test_live_worktree_path_reads_execstart_when_the_unit_names_home(monkeypatch, tmp_path):
    """A WorkingDirectory that is not a checkout goes on to ExecStart's path=,
    which the installer's unit points into the checkout's venv."""
    checkout = _checkout(tmp_path / "co")
    _installer_unit_host(monkeypatch, tmp_path, checkout / ".venv" / "bin" / "kirocrew")
    monkeypatch.setattr(live, "_running_checkout", lambda: None)

    assert await live._live_worktree_path(fresh=True) == str(checkout.resolve())
