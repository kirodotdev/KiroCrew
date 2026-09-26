"""Does the live pywinpty binding actually report a child's exit code?

Every other test of the ConPTY reap stubs the binding, and
``test_terminal_handler.py`` is on ``test/windows-collect-ignore.txt``, so this
narrow module (no web terminal, no websocket) is what a Windows shard runs.
Skips on POSIX, where pywinpty is not installed.
"""

from __future__ import annotations

import os
import time
import warnings

import pytest

from kiro_crew import platform_compat
from kiro_crew.conpty import WindowsPty
from kiro_crew.dashboard.handlers import terminal

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="ConPTY/pywinpty is Windows-only; the POSIX reap is covered by test_terminal_handler",
)


def _cmd_exiting_with(code: int) -> list[str]:
    """``cmd.exe`` by absolute path, exiting immediately with *code*.

    ``/d`` skips any ``Command Processor\\AutoRun`` registry command, which would
    otherwise run inside every child.
    """
    comspec = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
    return [comspec, "/d", "/c", f"exit {code}"]


#: How long to wait for ConPTY to report a child dead. Far above the shipped
#: bound, so a slow child is measured rather than lost to a timeout.
_MEASURE_CEILING_S = 15.0


def _measure_death(pty: WindowsPty) -> tuple[bool, float, int]:
    """Poll ``isalive()`` up to the ceiling; return ``(died, elapsed, polls)``.

    Does not stop at the shipped bound: the point is the real latency.
    """
    polls = 0
    started = time.monotonic()
    deadline = started + _MEASURE_CEILING_S
    while pty.isalive():
        polls += 1
        if time.monotonic() >= deadline:
            return False, time.monotonic() - started, polls
        time.sleep(terminal._CONPTY_STATUS_POLL_S)
    return True, time.monotonic() - started, polls


class TestTheLiveBindingReportsAStatus:
    """Each unresolved-status cause is told apart by its message. A child never
    reported dead, or reaped with no status, FAILS; a child slower than the
    shipped bound only WARNS, since that is a budget, not a broken mechanism.
    """

    def test_a_clean_child_reports_zero(self, tmp_path):
        pty = WindowsPty(_cmd_exiting_with(0), cwd=str(tmp_path))
        try:
            died, elapsed, polls = _measure_death(pty)
            assert died, (
                "ConPTY never reported the child dead, so exitstatus() is never "
                f"reached: still alive after {polls} isalive() call(s) over "
                f"{elapsed:.2f}s. Past this ceiling the reap bound is NOT the "
                "fix -- no budget a terminal close can afford would help -- so "
                "stop publishing a note when the status is unknown instead."
            )
            status = pty.exitstatus()
            assert status is not None, (
                "pywinpty reaped the child but reports no status: exitstatus() "
                f"answered None after {polls} isalive() call(s) in {elapsed:.2f}s. "
                "Waiting longer cannot resolve this -- ConPTY has nothing to "
                "supply on this runner and the wrapper's premise is wrong."
            )
            assert status == 0
            # WARN tier: never fail on latency. A wall-clock assertion on a
            # shared runner goes red for scheduling, not for a code fault; the
            # warning names the measured number a new bound is chosen from.
            print(
                f"conpty reap latency: {elapsed:.2f}s over {polls} poll(s), "
                f"budget _CHILD_REAP_TIMEOUT_S={terminal._CHILD_REAP_TIMEOUT_S}s"
            )
            if elapsed > terminal._CHILD_REAP_TIMEOUT_S:
                warnings.warn(
                    f"conpty reap budget: ConPTY reported a clean child dead "
                    f"after {elapsed:.2f}s ({polls} poll(s)), but the shipped "
                    f"reap resolves a status only within "
                    f"_CHILD_REAP_TIMEOUT_S="
                    f"{terminal._CHILD_REAP_TIMEOUT_S}s, so every Windows exit "
                    "resolves to unknown and rings the bell feed. Raise the "
                    f"bound above {elapsed:.2f}s (with headroom -- this runner's "
                    "subprocess latency is documented as highly variable) or "
                    "stop publishing on an unknown status.",
                    # stacklevel=2 would point at pytest, not this file.
                    stacklevel=1,
                )
        finally:
            pty.terminate()

    def test_a_failing_child_reports_its_own_code(self, tmp_path):
        """Pins the code to the child, not to a constant 0."""
        pty = WindowsPty(_cmd_exiting_with(3), cwd=str(tmp_path))
        try:
            died, elapsed, polls = _measure_death(pty)
            assert died, f"ConPTY never reported the child dead after {elapsed:.2f}s"
            assert pty.exitstatus() == 3
        finally:
            pty.terminate()

    def test_a_live_child_has_no_status_yet(self, tmp_path):
        """``exitstatus()`` must not invent a code for a running child."""
        pty = WindowsPty(
            [os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe"), "/d"],
            cwd=str(tmp_path),
        )
        try:
            assert pty.isalive(), (
                "the interactive child was already gone, so this case asserts "
                "nothing: it needs a LIVE child to prove exitstatus() withholds "
                "a code. A shell that cannot stay open inside a ConPTY is itself "
                "the finding -- the web terminal spawns exactly this way."
            )
            assert pty.exitstatus() is None, (
                "exitstatus() invented a code for a child that is still running. "
                "The reap reads the status only after isalive() answers False, so "
                "this would make every displaced or still-running shell look like "
                "a clean exit."
            )
        finally:
            pty.terminate()


class TestTheProductionReapResolvesIt:
    """The shipped reap reads the real status, given time.

    The bound is widened to the ceiling so this tests correctness, not the
    budget: green here with a warning above means only the number is wrong.
    """

    @pytest.mark.asyncio
    async def test_child_exit_status_resolves_a_real_conpty_child(self, monkeypatch, tmp_path):
        monkeypatch.setattr(terminal, "_CHILD_REAP_TIMEOUT_S", _MEASURE_CEILING_S)
        pty = WindowsPty(_cmd_exiting_with(0), cwd=str(tmp_path))
        sess = terminal._TerminalSession(
            session_id="conpty-status",
            master_fd=-1,  # wokeignore:rule=master
            winpty=pty,
        )
        try:
            status = await terminal._child_exit_status(sess)
            assert status == 0, (
                f"the shipped reap answered {status!r} for a real ConPTY child "
                f"that exited 0, even with {_MEASURE_CEILING_S}s to do it. This "
                "is not the reap budget -- the fault is in the reap itself or in "
                "the binding, so raising _CHILD_REAP_TIMEOUT_S will not fix it."
            )
        finally:
            pty.terminate()

    @pytest.mark.asyncio
    async def test_a_failing_child_keeps_its_code_through_the_reap(self, monkeypatch, tmp_path):
        monkeypatch.setattr(terminal, "_CHILD_REAP_TIMEOUT_S", _MEASURE_CEILING_S)
        pty = WindowsPty(_cmd_exiting_with(3), cwd=str(tmp_path))
        sess = terminal._TerminalSession(
            session_id="conpty-status-fail",
            master_fd=-1,  # wokeignore:rule=master
            winpty=pty,
        )
        try:
            assert await terminal._child_exit_status(sess) == 3
        finally:
            pty.terminate()
