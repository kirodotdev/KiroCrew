"""A host-wide pause shorter than the backstop does not end a managed gateway.

On a managed budget (``exit_after`` at or above ``stall_after``) the watchdog's
daemon thread makes the stall exit, moved later by the time it could not run
itself, and the kernel alarm each beat arms is a backstop one budget later.
The fake-clock tests step :meth:`LoopStallWatchdog.check` by hand and record
every alarm arm, so nothing arms a real process-ending timer; the child-process
tests stop a real watchdog with ``SIGSTOP``, the way a host-wide pause stops
every thread while the monotonic clock keeps running.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import loop_watchdog
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
from kiro_crew.subprocess_utf8 import UTF8_TEXT

_BUDGET = 30.0
_POLL = 5.0


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class _Messages(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


class _Fixture:
    """A started watchdog whose daemon thread never runs, stepped by hand."""

    def __init__(self, *, stall_after: float = 30.0, exit_after: float = _BUDGET) -> None:
        self.clock = _Clock()
        self.arms: list[float] = []
        self.messages = _Messages()
        log = logging.Logger("test.loop_stall_host_pause_exit")
        log.addHandler(self.messages)
        self.wd = LoopStallWatchdog(
            stall_after=stall_after,
            exit_after=exit_after,
            poll_interval=_POLL,
            enrich_after=1000.0,
            now=self.clock,
            suspend_clock=lambda: None,
            dump=lambda: None,
            arm_later=self.arms.append,
            cancel_later=lambda: None,
            loop_major_faults=lambda _tid: None,
            log=log,
        )
        self.wd._run = lambda: None  # type: ignore[method-assign]
        self.wd.start()

    def beat(self) -> None:
        self.wd.beat()

    def poll_at(self, silence: float) -> None:
        self.clock.t = self.wd._last_beat + silence
        self.wd.check()

    def polls(self, start: float, stop: float) -> None:
        silence = start
        while silence <= stop:
            self.poll_at(silence)
            silence += _POLL

    def pause_lines(self) -> list[str]:
        return [m for m in self.messages.lines if "did not run for" in m]

    def close(self) -> None:
        self.wd.stop()


@pytest.fixture
def fx():
    fixture = _Fixture()
    yield fixture
    fixture.close()


def test_a_managed_beat_arms_the_alarm_one_budget_late(fx: _Fixture) -> None:
    fx.arms.clear()
    fx.beat()
    assert fx.arms == [2 * _BUDGET]


def test_the_backstop_never_passes_the_paging_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(  # flake-ok: fake-clock seconds, read once by the arm, never a wall wait
        loop_watchdog, "PAGING_GRACE_MAX_SECS", 10.0
    )
    capped = _Fixture()
    try:
        assert capped.arms == [_BUDGET + 10.0]
    finally:
        capped.close()


def test_a_wedged_loop_still_exits_on_its_budget(fx: _Fixture) -> None:
    """The daemon thread runs on time, so its own exit fires at the budget."""
    fx.beat()
    fx.arms.clear()
    fx.polls(5.0, _BUDGET - _POLL)
    assert fx.arms == []
    fx.poll_at(_BUDGET)
    assert fx.arms == [loop_watchdog.EXIT_ARM_FLOOR_SECS]
    fx.poll_at(_BUDGET + _POLL)
    assert fx.arms == [loop_watchdog.EXIT_ARM_FLOOR_SECS]


def test_a_whole_process_pause_does_not_count_toward_the_exit(fx: _Fixture) -> None:
    """The issue's case: every thread stops for longer than the budget."""
    fx.beat()
    fx.arms.clear()
    fx.poll_at(5.0)
    fx.poll_at(10.0)
    fx.poll_at(55.0)  # 45s since the last poll: this thread stopped for 40s
    fx.poll_at(60.0)
    assert fx.arms == []
    assert len(fx.pause_lines()) == 1
    assert "about 40s" in fx.pause_lines()[0]
    fx.beat()
    assert fx.arms == [2 * _BUDGET]


def test_a_loop_that_stays_silent_after_a_pause_exits_one_budget_of_running_later(
    fx: _Fixture,
) -> None:
    fx.beat()
    fx.arms.clear()
    fx.poll_at(5.0)
    fx.poll_at(50.0)  # 40s paused, 10s counted
    fx.polls(55.0, 65.0)  # 25s counted
    assert fx.arms == []
    fx.poll_at(70.0)  # 30s counted
    assert fx.arms == [loop_watchdog.EXIT_ARM_FLOOR_SECS]
    assert len(fx.pause_lines()) == 1


def test_a_beat_after_the_exit_was_armed_puts_the_backstop_back(fx: _Fixture) -> None:
    fx.beat()
    fx.polls(5.0, _BUDGET)
    fx.arms.clear()
    fx.beat()
    assert fx.arms == [2 * _BUDGET]
    fx.polls(5.0, _BUDGET - _POLL)
    assert fx.arms == [2 * _BUDGET]


def test_a_desktop_budget_keeps_the_exact_alarm() -> None:
    """Below ``stall_after`` the desktop app's liveness kill would land before
    a later backstop's dump, so nothing moves and no pause is counted."""
    desktop = _Fixture(exit_after=25.0)
    try:
        desktop.arms.clear()
        desktop.beat()
        desktop.poll_at(5.0)
        desktop.poll_at(50.0)
        assert desktop.arms == [25.0]
        assert desktop.pause_lines() == []
    finally:
        desktop.close()


# ── A real watchdog, stopped by SIGSTOP ─────────────────────────────────────

_REAL_BUDGET = 2.0
_REAL_BACKSTOP = 2 * _REAL_BUDGET

pytestmark_posix = pytest.mark.skipif(
    not (hasattr(signal, "SIGSTOP") and platform_compat.process_alarm_available()),
    reason="needs SIGSTOP and the kernel process alarm",
)


def _child(dump: Path, beat_for: float) -> str:
    return textwrap.dedent(f"""
        import logging, sys, time
        from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
        f = open({str(dump)!r}, "w", encoding="utf-8")
        f.write("# header\\n\\n")
        wd = LoopStallWatchdog(
            stall_after=1.0, exit_after={_REAL_BUDGET}, poll_interval=1.0,
            enrich_after=100.0, dump_file=f, log=logging.getLogger("t"),
        )
        wd.start()
        wd.beat()
        print("beating", flush=True)
        end = time.monotonic() + {beat_for}
        while time.monotonic() < end:
            wd.beat()
            time.sleep(0.1)
        if {beat_for} == 0:
            time.sleep(10.0)
        wd.stop()
        sys.exit(0)
        """)


def _run_paused(tmp_path: Path, *, beat_for: float, pause: float) -> tuple[int, Path]:
    dump = tmp_path / "loopstall.txt"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-X", "faulthandler", "-c", _child(dump, beat_for)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        **UTF8_TEXT,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "beating"
        if pause:
            os.kill(proc.pid, signal.SIGSTOP)
            time.sleep(pause)  # flake-ok: the length of the pause is the scenario under test
            os.kill(proc.pid, signal.SIGCONT)
        return proc.wait(timeout=30), dump
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


@pytestmark_posix
def test_a_real_pause_shorter_than_the_backstop_leaves_the_process_running(
    tmp_path: Path,
) -> None:
    """A pause past the budget but inside the backstop: the child keeps running."""
    status, _dump = _run_paused(tmp_path, beat_for=6.0, pause=_REAL_BUDGET + 1.0)
    assert status == 0


@pytestmark_posix
def test_a_real_pause_past_the_backstop_still_ends_it_with_a_dump(tmp_path: Path) -> None:
    """The backstop expired during the pause and lands on resume.

    Every thread wakes at once then, and the alarm's GIL-free dump can walk a
    frame that a waking thread is changing, so the child may die by
    ``SIGSEGV`` part way through the dump instead of by ``SIGALRM`` after it.
    Either way the process ends, which is what this pins."""
    status, _dump = _run_paused(tmp_path, beat_for=8.0, pause=_REAL_BACKSTOP + 1.5)
    assert status in (-signal.SIGALRM, -signal.SIGSEGV)


@pytestmark_posix
def test_a_real_wedged_loop_on_a_managed_budget_exits_by_the_alarm(tmp_path: Path) -> None:
    """The daemon thread arms the exit, and the alarm still dumps every thread."""
    status, dump = _run_paused(tmp_path, beat_for=0, pause=0)
    assert status == -signal.SIGALRM
    assert "Thread 0x" in dump.read_text(encoding="utf-8", errors="replace")
