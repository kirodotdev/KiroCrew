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
import select
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import loop_watchdog
from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog

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
        self.exits: list[float] = []
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
            exit_now=lambda: self.exits.append(self.clock.t),
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
    assert fx.exits == []
    fx.poll_at(_BUDGET)
    assert fx.arms == []
    assert fx.exits == [fx.wd._last_beat + _BUDGET]
    fx.poll_at(_BUDGET + _POLL)
    assert fx.arms == []
    assert fx.exits == [fx.wd._last_beat + _BUDGET]


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
    assert fx.exits == []
    fx.poll_at(70.0)  # 30s counted
    assert fx.arms == []
    assert fx.exits == [fx.wd._last_beat + 70.0]
    assert len(fx.pause_lines()) == 1


def test_a_pause_just_before_budget_keeps_the_backstop_until_the_thread_rechecks() -> None:
    fx = _Fixture(exit_after=90.0)
    try:
        fx.beat()
        beat = fx.wd._last_beat
        backstop = fx.wd._alarm_due_at
        fx.arms.clear()
        fx.polls(3.0, 88.0)
        assert fx.arms == []  # In particular, no 2s alarm at 88s.
        assert fx.exits == []
        assert backstop == beat + 180.0
        fx.clock.t = beat + 108.0  # No thread ran during this 20s pause.
        assert fx.wd._alarm_due_at == backstop
        assert fx.clock.t < backstop  # The kernel has no expired alarm to deliver.
        assert fx.exits == []
        fx.wd.check()
        assert fx.wd._paused == 15.0
        assert fx.clock.t - beat - fx.wd._paused == 93.0
        assert fx.exits == [beat + 108.0]  # Only the resumed watchdog decides.
        assert fx.arms == []
        fx.poll_at(113.0)
        assert fx.exits == [beat + 108.0]
    finally:
        fx.close()


@pytest.mark.parametrize("recover", ["beat", "stop"])
def test_a_recovery_during_the_poll_prevents_the_thread_exit(fx: _Fixture, recover: str) -> None:
    fx.polls(5.0, 25.0)

    def recover_while_reading_faults(_tid: int | None) -> None:
        getattr(fx.wd, recover)()

    fx.wd._loop_major_faults = recover_while_reading_faults
    fx.poll_at(30.0)
    assert fx.exits == []


def test_the_final_exit_check_accounts_for_an_unsampled_pause(fx: _Fixture) -> None:
    fx.poll_at(5.0)
    fx.clock.t = fx.wd._last_beat + 45.0
    fx.wd._decide_exit(45.0, fx.wd._beat_generation)
    assert fx.wd._paused == 35.0
    assert fx.exits == []
    fx.polls(50.0, 60.0)
    assert fx.exits == []
    fx.poll_at(65.0)
    assert fx.exits == [fx.wd._last_beat + 65.0]


def test_a_beat_before_exit_keeps_the_backstop(fx: _Fixture) -> None:
    fx.beat()
    fx.polls(5.0, _BUDGET - _POLL)
    fx.arms.clear()
    fx.beat()
    assert fx.arms == [2 * _BUDGET]
    fx.polls(5.0, _BUDGET - _POLL)
    assert fx.arms == [2 * _BUDGET]
    assert fx.exits == []


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


def test_thread_exit_preserves_loop_identity_before_enrichment(fx: _Fixture, tmp_path) -> None:
    with (tmp_path / "dump.txt").open("w+", encoding="utf-8") as target:
        fx.wd._dump_file = target
        fx.polls(5.0, _BUDGET)
        assert fx.exits == [fx.wd._last_beat + _BUDGET]
        target.seek(0)
        assert f"{loop_watchdog.LOOP_THREAD_HEADER} 0x{fx.wd._loop_thread_id:016x}" in target.read()


@pytest.mark.parametrize("mechanism", ["alarm", "faulthandler", None])
def test_thread_exit_uses_the_armed_dump_mechanism(monkeypatch, mechanism) -> None:
    calls: list[tuple] = []
    target = object()
    alarm = object()
    monkeypatch.setattr(loop_watchdog, "_armed_mechanism", mechanism)
    monkeypatch.setattr(
        loop_watchdog,
        "signal",
        SimpleNamespace(SIGALRM=alarm, raise_signal=lambda sig: calls.append(("signal", sig))),
    )
    monkeypatch.setattr(
        loop_watchdog,
        "faulthandler",
        SimpleNamespace(
            dump_traceback=lambda **kwargs: calls.append(("dump", kwargs)),
        ),
    )
    monkeypatch.setattr(
        loop_watchdog, "os", SimpleNamespace(_exit=lambda status: calls.append(("exit", status)))
    )
    loop_watchdog._default_exit_now(target)
    if mechanism == "alarm":
        assert calls == [("signal", alarm)]
    elif mechanism == "faulthandler":
        assert calls == [("dump", {"file": target, "all_threads": True}), ("exit", 1)]
    else:
        assert calls == []


# ── A real watchdog, stopped by SIGSTOP ─────────────────────────────────────

# The numbers are chosen so that no plausible scheduling delay decides a result.
# The child beats every 0.1 s, so when the parent stops it the last beat is at
# most 0.1 s old plus however long the runner kept the child off a CPU. The
# kernel backstop is then due at that beat + 2 * _REAL_BUDGET = 12 s.
#
# * The short pause is _REAL_BUDGET + 1 = 7 s, so the child must also lose
#   another 5 s (a late last beat, plus the parent's sleep overshooting) before
#   the backstop could expire inside it. On origin/main the exit alarm sits one
#   budget after the beat, at most 6 s after the stop, so it always expires
#   inside the 7 s pause and fires on resume: that is the regression this pins.
# * The long pause is 2 * _REAL_BUDGET + 1 = 13 s. The backstop is due at most
#   12 s after the stop, because the last beat came before the stop, so lateness
#   only moves it earlier. No margin is spent there.
# * A pause is seen by the child as a gap over _REAL_BUDGET between two of its
#   0.1 s beats. The child then beats _REAL_AFTER_RESUME more seconds, more than
#   two of the watchdog's 1 s polls, before it says so.
#
# Both real-child tests run on one xdist worker (the group below), so they never
# cold-start interpreters at the same time as each other. Serial runtime is about
# 7 + 3 + 7 s for the first test and 13 s for the second.
_REAL_BUDGET = 6.0
_REAL_BACKSTOP = 2 * _REAL_BUDGET
_REAL_SHORT_PAUSE = _REAL_BUDGET + 1.0
_REAL_LONG_PAUSE = _REAL_BACKSTOP + 1.0
_REAL_AFTER_RESUME = 3.0
# Half the 120 s pytest timeout: a lost run fails here by name, before the kill.
_REAL_DEADLINE = 60.0

pytestmark_posix = pytest.mark.skipif(
    not (hasattr(signal, "SIGSTOP") and platform_compat.process_alarm_available()),
    reason="needs SIGSTOP and the kernel process alarm",
)
real_child = pytest.mark.xdist_group(name="loop_stall_host_pause_real_child")


def _child(dump: Path) -> str:
    """Beat until a pause, keep beating, say ``survived``, then go silent."""
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
        give_up = time.monotonic() + {_REAL_DEADLINE}
        prev = time.monotonic()
        resumed = None
        # The parent may stop us as soon as it reads this line.
        print("beating", flush=True)
        while resumed is None or prev - resumed < {_REAL_AFTER_RESUME}:
            wd.beat()
            time.sleep(0.1)
            now = time.monotonic()
            if resumed is None and now - prev > {_REAL_BUDGET}:
                resumed = now
            prev = now
            if now > give_up:
                print("no pause seen", flush=True)
                sys.exit(3)
        print("survived", flush=True)
        time.sleep({_REAL_DEADLINE})  # The loop wedges: no more beats.
        sys.exit(0)
        """)


def _read_line(stream, deadline: float) -> str:
    """One stdout line, or ``""`` at EOF; fails by name past ``deadline``."""
    raw = b""
    while not raw.endswith(b"\n"):
        left = deadline - time.monotonic()
        if left <= 0:
            raise AssertionError(f"the child wrote no full line in time; read {raw!r}")
        ready, _, _ = select.select([stream], [], [], left)
        if not ready:
            continue
        chunk = os.read(stream.fileno(), 1)
        if not chunk:
            break
        raw += chunk
    return raw.decode("utf-8", errors="replace").strip()


def _run_paused(tmp_path: Path, pause: float) -> tuple[str, int, Path]:
    """Stop the beating child for ``pause`` seconds; return its next line and status."""
    dump = tmp_path / "loopstall.txt"
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    proc = subprocess.Popen(
        [sys.executable, "-X", "faulthandler", "-c", _child(dump)],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=str(cwd),
        bufsize=0,
    )
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + _REAL_DEADLINE
        assert _read_line(proc.stdout, deadline) == "beating"
        os.kill(proc.pid, signal.SIGSTOP)
        time.sleep(pause)  # flake-ok: the length of the pause is the scenario under test
        os.kill(proc.pid, signal.SIGCONT)
        line = _read_line(proc.stdout, deadline)
        return line, proc.wait(timeout=max(1.0, deadline - time.monotonic())), dump
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
        proc.stdout.close()


@pytestmark_posix
@real_child
def test_a_real_pause_shorter_than_the_backstop_leaves_the_process_running(
    tmp_path: Path,
) -> None:
    """A pause past the budget but inside the backstop: the child keeps beating.

    Once it goes silent afterwards, the daemon thread still ends it on its
    budget, and the alarm still dumps every thread."""
    line, status, dump = _run_paused(tmp_path, _REAL_SHORT_PAUSE)
    assert line == "survived"
    assert status == -signal.SIGALRM
    assert "Thread 0x" in dump.read_text(encoding="utf-8", errors="replace")


@pytestmark_posix
@real_child
def test_a_real_pause_past_the_backstop_still_ends_it_with_a_dump(tmp_path: Path) -> None:
    """The backstop expired during the pause and lands on resume.

    Every thread wakes at once then, and the alarm's GIL-free dump can walk a
    frame that a waking thread is changing, so the child may die by
    ``SIGSEGV`` part way through the dump instead of by ``SIGALRM`` after it.
    Either way the process ends, which is what this pins."""
    line, status, _dump = _run_paused(tmp_path, _REAL_LONG_PAUSE)
    assert line == ""
    assert status in (-signal.SIGALRM, -signal.SIGSEGV)
