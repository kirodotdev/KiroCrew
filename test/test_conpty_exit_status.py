"""Does the live pywinpty binding actually report a child's exit code?

Every other test of ``WindowsPty.exitstatus()`` stubs the binding, so nothing
proves that ConPTY supplies a code at all. This module is deliberately narrow --
the wrapper and a ``cmd.exe`` that exits, no web terminal and no websocket --
because ``test_terminal_handler.py`` is on ``test/windows-collect-ignore.txt``
and a Windows shard therefore runs no live PTY test without it. This file is on
no ignore list, so it is collected on Windows; on POSIX every case skips, where
pywinpty is neither imported nor installed.
"""

from __future__ import annotations

import os
import time
import warnings

import pytest

from kiro_crew import platform_compat
from kiro_crew.conpty import WindowsPty

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_WINDOWS,
    reason="ConPTY/pywinpty is Windows-only",
)

#: How often to ask ConPTY whether the child is gone. pywinpty's ``wait()``
#: cannot be cancelled, so a caller polls ``isalive()`` instead and this is the
#: gap it sleeps between calls.
_POLL_INTERVAL_S = 0.05

#: The budget a caller can afford to spend resolving a status before it gives up
#: and calls the code unknown. A clean ``cmd.exe`` exit is reported dead after
#: roughly three seconds on a GitHub windows-latest runner, while POSIX resolves
#: in under twenty milliseconds, so this is sized for the slow backend.
_REAP_BOUND_S = 10.0

#: How long this test keeps polling. Above the budget on purpose: a child slower
#: than ``_REAP_BOUND_S`` is a number worth measuring and reporting, not a
#: timeout worth losing.
_MEASURE_CEILING_S = 15.0


def _cmd_exiting_with(code: int) -> list[str]:
    """``cmd.exe`` by absolute path, exiting immediately with *code*.

    ``/d`` skips any ``Command Processor\\AutoRun`` registry command, which would
    otherwise run inside every child.
    """
    comspec = os.path.join(os.environ["SystemRoot"], "System32", "cmd.exe")
    return [comspec, "/d", "/c", f"exit {code}"]


def _measure_death(pty: WindowsPty) -> tuple[bool, float, int]:
    """Poll ``isalive()`` up to the ceiling; return ``(died, elapsed, polls)``.

    Polls past ``_REAP_BOUND_S`` deliberately, because the measured latency is
    what a budget is chosen from.
    """
    polls = 0
    started = time.monotonic()
    deadline = started + _MEASURE_CEILING_S
    while pty.isalive():
        polls += 1
        if time.monotonic() >= deadline:
            return False, time.monotonic() - started, polls
        time.sleep(_POLL_INTERVAL_S)
    return True, time.monotonic() - started, polls


def _warn_if_slower_than_the_budget(elapsed: float, polls: int) -> None:
    """Warn, never fail, when the child outlives ``_REAP_BOUND_S``.

    A wall-clock assertion on a shared runner goes red for scheduling rather
    than for a code fault, so the measured number is reported and the case stays
    green: the mechanism works, only the budget would need widening.
    """
    if elapsed > _REAP_BOUND_S:
        warnings.warn(
            f"ConPTY reported a clean child dead after {elapsed:.2f}s "
            f"({polls} poll(s)), above the {_REAP_BOUND_S}s budget a caller can "
            f"spend resolving a status. A caller bounded at {_REAP_BOUND_S}s "
            "reads every Windows exit as unknown. Raise that bound above "
            f"{elapsed:.2f}s, with headroom -- this runner's subprocess latency "
            "is documented as highly variable.",
            # stacklevel=2 would point at pytest, not at the calling test.
            stacklevel=1,
        )


class TestTheLiveBindingReportsAStatus:
    """Each cause of an unresolved status is told apart by its message.

    A child never reported dead, or reaped with no status at all, FAILS: neither
    is a budget problem and waiting longer cannot fix either one.
    """

    def test_a_clean_child_reports_zero(self, tmp_path) -> None:
        pty = WindowsPty(_cmd_exiting_with(0), cwd=str(tmp_path))
        try:
            died, elapsed, polls = _measure_death(pty)
            assert died, (
                "ConPTY never reported the child dead, so exitstatus() is never "
                f"reached: still alive after {polls} isalive() call(s) over "
                f"{elapsed:.2f}s. Past this ceiling a wider budget is not the "
                "fix -- no bound a terminal close can afford would help."
            )
            status = pty.exitstatus()
            assert status is not None, (
                "pywinpty reaped the child but reports no status: exitstatus() "
                f"answered None after {polls} isalive() call(s) in {elapsed:.2f}s. "
                "Waiting longer cannot resolve this -- ConPTY has nothing to "
                "supply on this runner and the wrapper's premise is wrong."
            )
            assert status == 0
            print(f"ConPTY reap latency: {elapsed:.2f}s over {polls} poll(s)")
            _warn_if_slower_than_the_budget(elapsed, polls)
        finally:
            pty.terminate()

    def test_a_failing_child_reports_its_own_code(self, tmp_path) -> None:
        """Pins the code to the child, not to a constant zero."""
        pty = WindowsPty(_cmd_exiting_with(3), cwd=str(tmp_path))
        try:
            died, elapsed, polls = _measure_death(pty)
            assert died, (
                f"ConPTY never reported the child dead after {elapsed:.2f}s " f"({polls} poll(s))"
            )
            assert pty.exitstatus() == 3
            _warn_if_slower_than_the_budget(elapsed, polls)
        finally:
            pty.terminate()

    def test_a_live_child_has_no_status_yet(self, tmp_path) -> None:
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
                "The status is readable only after isalive() answers False, so "
                "this makes every live shell look like a clean exit."
            )
        finally:
            pty.terminate()
