"""An in-flight update installer is stopped with SIGTERM first, so it can roll back.

``cli.sh`` moves the managed venv aside before it rebuilds it, and restores it
from a TERM trap when it is interrupted. A SIGKILL skips that trap and leaves
no venv and no console script, so the cancel arm (shutdown) and the timeout arm
of every apply route that runs an installer (the policy-defined command
provider; the managed venv's unattended apply builds beside the live tree and
runs no installer) must give the installer's group SIGTERM and a grace first.

These tests drive the REAL apply routes against a cli.sh-shaped stand-in (move
aside, a TERM trap that restores, a long step) and assert the venv is back.
They spawn real children, each in its own session; every wait is bounded and
every group is SIGKILLed at teardown, whatever the test's outcome.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import platform_compat

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="POSIX process groups and a POSIX shell"
)

#: A cli.sh-shaped installer: moves the venv aside, restores it on TERM (after
#: an optional delay, "$2"), then runs a long step. Records its process group so
#: teardown can reap it. Touches only the case directory it is handed.
_FAKE_INSTALLER = r"""
set -u
D="$1"
DELAY="${2:-0}"
ps -o pgid= -p $$ | tr -d ' ' > "$D/pgid"
mv "$D/venv" "$D/venv.pre-rebuild"
mkdir "$D/venv"
restore() {
  kill "$step" 2>/dev/null
  sleep "$DELAY"
  rm -rf "$D/venv"
  mv "$D/venv.pre-rebuild" "$D/venv"
  echo restored > "$D/outcome"
  exit 130
}
trap restore TERM INT
sleep 30 &
step=$!
echo moved > "$D/phase"
wait "$step"
"""

#: The same shape with cli.sh's ``_run_step``: the long step runs under
#: ``setsid`` (its own session, outside the group the gateway signals), and the
#: TERM trap stops that step's group before it restores the venv.
_SETSID_INSTALLER = r"""
set -u
D="$1"
ps -o pgid= -p $$ | tr -d ' ' > "$D/pgid"
mv "$D/venv" "$D/venv.pre-rebuild"
mkdir "$D/venv"
setsid sh -c 'while :; do : > "$1/venv/f.$$"; sleep 0.1; done' sh "$D" >/dev/null 2>&1 </dev/null &
step=$!
echo "$step" > "$D/steppid"
interrupted() {
  kill -s TERM -- "-$step" 2>/dev/null || kill -s TERM "$step" 2>/dev/null
  wait "$step" 2>/dev/null
  rm -rf "$D/venv"
  mv "$D/venv.pre-rebuild" "$D/venv"
  echo restored > "$D/outcome"
  exit 130
}
trap interrupted TERM INT
echo moved > "$D/phase"
wait "$step"
"""

#: A child that ignores SIGTERM, to prove the SIGKILL escalation.
_IGNORES_TERM = "trap '' TERM; echo ready > \"$1/phase\"; sleep 30 & wait $!; sleep 30"


def _case(tmp_path: Path) -> Path:
    case = tmp_path / "case"
    (case / "venv" / "bin").mkdir(parents=True)
    (case / "venv" / "bin" / "kirocrew").write_text("#!/bin/sh\n", encoding="utf-8")
    (tmp_path / "installer.sh").write_text(_FAKE_INSTALLER, encoding="utf-8")
    return case


def _installer_command(tmp_path: Path, case: Path, *args: str) -> str:
    argv = [str(tmp_path / "installer.sh"), str(case), *args]
    return "sh " + " ".join(shlex.quote(a) for a in argv)


async def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        await asyncio.sleep(0.02)


def _assert_restored(case: Path) -> None:
    assert (case / "outcome").read_text(encoding="utf-8").strip() == "restored"
    assert (case / "venv" / "bin" / "kirocrew").is_file()
    assert not (case / "venv.pre-rebuild").exists()


@pytest.fixture
def reap_groups(tmp_path):
    """SIGKILL every process group a test started, on every exit path."""
    groups: list[int] = []
    yield groups
    for name in ("pgid", "steppid"):
        recorded = tmp_path / "case" / name
        if recorded.is_file():
            with contextlib.suppress(ValueError):
                groups.append(int(recorded.read_text(encoding="utf-8").strip()))
    for pgid in groups:
        with contextlib.suppress(Exception):
            platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)
    # Bounded wait for every group to empty, so nothing outlives the test. A
    # leader the test's (closed) event loop never reaped is a zombie child of
    # this process, which still counts as a member, so reap it here.
    deadline = time.monotonic() + 10
    while any(platform_compat.pgroup_exists(pgid) for pgid in groups):
        assert time.monotonic() < deadline, "a test process group outlived teardown"
        for pgid in groups:
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(pgid, os.WNOHANG)
        time.sleep(0.05)


async def _cancel_and_settle(task: asyncio.Task) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=15)


class TestTerminateAndReapSync:
    @pytest.mark.timeout(60)
    def test_a_child_that_ignores_term_is_killed_after_the_grace(self, tmp_path, reap_groups):
        import subprocess

        proc = subprocess.Popen(
            ["/bin/sh", "-c", _IGNORES_TERM, "sh", str(tmp_path)],
            cwd=str(tmp_path),
            start_new_session=True,
        )
        reap_groups.append(proc.pid)
        deadline = time.monotonic() + 10
        while not (tmp_path / "phase").exists():
            assert time.monotonic() < deadline, "the child never started"
            time.sleep(0.02)
        grace = 0.5

        started = time.monotonic()
        platform_compat.terminate_and_reap_sync(proc, grace=grace, reap_timeout=5)

        assert time.monotonic() - started >= grace
        assert proc.returncode is not None
        deadline = time.monotonic() + 5
        while platform_compat.pgroup_exists(proc.pid):
            assert time.monotonic() < deadline, "the TERM-ignoring group survived SIGKILL"
            time.sleep(0.05)

    def test_a_pid_no_process_can_own_falls_back_without_raising(self):
        proc = MagicMock()
        proc.pid = 99_999_999_999
        proc.poll.return_value = None

        platform_compat.terminate_and_reap_sync(proc, grace=0.1, reap_timeout=0.1)

        proc.kill.assert_called_once()

    def test_a_ctrl_c_mid_stop_is_held_until_the_stop_finishes(self):
        import subprocess

        proc = MagicMock()
        proc.pid = 99_999_999_999
        proc.poll.return_value = None
        proc.wait.side_effect = [KeyboardInterrupt(), subprocess.TimeoutExpired("sh", 0.1)]

        with pytest.raises(KeyboardInterrupt):
            platform_compat.terminate_and_reap_sync(proc, grace=0.1, reap_timeout=0.1)

        # The kill still happened, before the interrupt was re-raised.
        proc.kill.assert_called_once()


class TestCommandProviderRollsBack:
    @pytest.mark.asyncio
    async def test_cancel_arm_lets_the_apply_command_restore(self, tmp_path, reap_groups):
        from kiro_crew.platform.update_provider import CommandProvider

        case = _case(tmp_path)
        provider = CommandProvider(
            check_command="true", apply_command=_installer_command(tmp_path, case)
        )
        task = asyncio.create_task(provider.apply())
        try:
            await _wait_for_file(case / "phase")
        finally:
            await _cancel_and_settle(task)

        _assert_restored(case)

    @pytest.mark.asyncio
    async def test_kirocrew_update_route_lets_the_apply_command_restore(
        self, monkeypatch, tmp_path, reap_groups
    ):
        # ``kirocrew update`` runs ``asyncio.run(apply_policy_update())``; a
        # Ctrl-C there cancels that task, which reaches the same cancel arm.
        from kiro_crew.platform import update_provider
        from kiro_crew.platform.update_provider import CommandProvider, apply_policy_update

        case = _case(tmp_path)
        provider = CommandProvider(
            check_command="true", apply_command=_installer_command(tmp_path, case)
        )
        monkeypatch.setattr(update_provider, "resolve_provider", lambda: provider)
        task = asyncio.create_task(apply_policy_update())
        try:
            await _wait_for_file(case / "phase")
        finally:
            await _cancel_and_settle(task)

        _assert_restored(case)


class TestTerminateAndReap:
    @staticmethod
    async def _spawn(script: str, arg: Path, groups: list[int]) -> asyncio.subprocess.Process:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-c",
            script,
            "sh",
            str(arg),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Under tmp_path, so a write that escapes cannot land in the repo.
            cwd=str(arg),
            start_new_session=True,
        )
        groups.append(proc.pid)
        return proc

    @pytest.mark.asyncio
    async def test_a_child_that_ignores_term_is_killed_after_the_grace(self, tmp_path, reap_groups):
        proc = await self._spawn(_IGNORES_TERM, tmp_path, reap_groups)
        await _wait_for_file(tmp_path / "phase")
        grace = 0.5

        started = time.monotonic()
        await asyncio.wait_for(
            platform_compat.terminate_and_reap(proc, grace=grace, reap_timeout=5), timeout=15
        )
        elapsed = time.monotonic() - started

        # Escalated only once the grace ran out, and the whole group is gone.
        # Asserted on the group rather than on ``returncode``: a SIGKILLed
        # member lingers as a zombie until init reaps it, and a foreign
        # ``waitpid`` in the test process can leave asyncio reporting 255.
        assert elapsed >= grace
        deadline = time.monotonic() + 5
        while platform_compat.pgroup_exists(proc.pid):
            assert time.monotonic() < deadline, "the TERM-ignoring group survived SIGKILL"
            await asyncio.sleep(0.05)

    @pytest.mark.asyncio
    async def test_a_child_that_honours_term_is_never_killed(self, tmp_path, reap_groups):
        case = _case(tmp_path)
        script = f". {shlex.quote(str(tmp_path / 'installer.sh'))}"
        proc = await self._spawn(script, case, reap_groups)
        await _wait_for_file(case / "phase")

        await asyncio.wait_for(
            platform_compat.terminate_and_reap(proc, grace=10, reap_timeout=5), timeout=15
        )

        # The trap ran to completion, which a SIGKILL would have prevented.
        _assert_restored(case)

    @pytest.mark.asyncio
    async def test_a_rollback_that_holds_no_pipe_still_gets_its_grace(self, tmp_path, reap_groups):
        # ``installer >log 2>&1; ...``: the shell that holds the gateway's pipes
        # dies on TERM at once, so the pipes reach EOF while the installer is
        # still inside its (here deliberately slow) rollback.
        case = _case(tmp_path)
        script = f"{_installer_command(tmp_path, case, '1')} >/dev/null 2>&1; true"
        proc = await self._spawn(script, case, reap_groups)
        await _wait_for_file(case / "phase")

        await asyncio.wait_for(
            platform_compat.terminate_and_reap(proc, grace=10, reap_timeout=5), timeout=20
        )

        _assert_restored(case)

    @pytest.mark.asyncio
    async def test_the_stop_survives_the_caller_being_cancelled(self, tmp_path, reap_groups):
        case = _case(tmp_path)
        script = f". {shlex.quote(str(tmp_path / 'installer.sh'))}"
        proc = await self._spawn(script, case, reap_groups)
        await _wait_for_file(case / "phase")

        stop = asyncio.create_task(platform_compat.terminate_and_reap(proc, grace=10))
        await asyncio.sleep(0)
        await _cancel_and_settle(stop)

        # The cancellation was re-delivered only after the rollback finished.
        _assert_restored(case)

    @pytest.mark.asyncio
    async def test_a_pid_no_process_can_own_falls_back_without_raising(self):
        # A mock carrying the suite's unallocatable pid (see
        # ``test_update_provider._UNALLOCATABLE_PID``): ``getpgid`` raises
        # OverflowError for it, which must not replace the caller's own
        # cancellation or timeout.
        proc = MagicMock()
        proc.pid = 99_999_999_999
        proc.returncode = None
        proc.communicate = AsyncMock(return_value=(b"", b""))

        await asyncio.wait_for(platform_compat.terminate_and_reap(proc, grace=0.1), timeout=10)

        proc.kill.assert_called_once()


def test_the_shutdown_stop_fits_inside_the_graceful_shutdown_cap():
    from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS, UPDATE_INSTALLER_STOP_SECS

    # Shutdown waits for the installer before its teardown; the rest of the cap
    # must remain for that teardown.
    assert UPDATE_INSTALLER_STOP_SECS < GRACEFUL_SHUTDOWN_SECS
