"""Tests for the off-loop event-loop stall watchdog.

The decision logic (``check``) is driven directly with injected clocks and a
recording dump callback — no real threads or sleeps — so the soft-dump state
machine and the suspend detection are verified deterministically.  The
dump-then-exit alarm is verified through injected ``arm_later``/``cancel_later``
recorders, so tests assert the wiring without ever arming a real process-ending
timer.  ``start``/``stop`` thread lifecycle gets one real-thread smoke test, and
the real alarm is proven end to end in child processes: one blocked in a
syscall, one holding the GIL.
"""

from __future__ import annotations

import logging
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.dashboard import loop_watchdog
from kiro_crew.dashboard.loop_watchdog import (
    SUSPEND_SKEW_MIN_SECS,
    LoopStallWatchdog,
)
from kiro_crew.subprocess_utf8 import UTF8_TEXT


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _make(stall_after: float = 30.0):
    """Soft-dump fixture: armed timer disabled + no-op hooks so it never touches
    real faulthandler regardless of whether start() runs."""
    clock = _Clock()
    dumps: list[float] = []
    wd = LoopStallWatchdog(
        stall_after=stall_after,
        exit_after=None,
        poll_interval=5.0,
        now=clock,
        dump=lambda: dumps.append(clock.t),
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        log=logging.getLogger("test.loop_watchdog"),
    )
    return wd, clock, dumps


# ── Soft daemon-thread dump state machine ────────────────────────────────────


def test_healthy_loop_never_dumps() -> None:
    wd, clock, dumps = _make()
    for _ in range(10):
        wd.beat()
        clock.advance(5.0)  # well under stall_after
        assert wd.check() is False
    assert dumps == []


def test_stall_triggers_single_dump() -> None:
    wd, clock, dumps = _make(stall_after=30.0)
    wd.beat()
    clock.advance(31.0)  # loop went silent past the threshold
    assert wd.check() is True
    assert len(dumps) == 1


def test_stall_dump_is_debounced() -> None:
    wd, clock, dumps = _make(stall_after=30.0)
    wd.beat()
    clock.advance(31.0)
    assert wd.check() is True
    # Still stalled on subsequent polls -> no additional dumps.
    for _ in range(5):
        clock.advance(5.0)
        assert wd.check() is False
    assert len(dumps) == 1


def test_recovery_rearms_for_next_stall() -> None:
    wd, clock, dumps = _make(stall_after=30.0)
    # First stall.
    wd.beat()
    clock.advance(31.0)
    assert wd.check() is True
    # Loop recovers (a fresh beat) -> watchdog re-arms.
    wd.beat()
    assert wd.check() is False
    # Second independent stall -> a second dump.
    clock.advance(31.0)
    assert wd.check() is True
    assert len(dumps) == 2


def test_just_under_threshold_does_not_dump() -> None:
    wd, clock, dumps = _make(stall_after=30.0)
    wd.beat()
    clock.advance(29.9)
    assert wd.check() is False
    assert dumps == []


def test_dump_exception_does_not_propagate() -> None:
    clock = _Clock()

    def _boom() -> None:
        raise RuntimeError("dump blew up")

    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=None,
        now=clock,
        dump=_boom,
        log=logging.getLogger("test.loop_watchdog"),
    )
    wd.beat()
    clock.advance(31.0)
    # A failing dump is swallowed; check() still reports it attempted a dump.
    assert wd.check() is True


# ── C-level dump-then-exit armed timer (faulthandler.dump_traceback_later) ────


def _make_armed(exit_after: float | None = 25.0):
    """Armed-timer fixture with recording arm/cancel hooks (no real timer)."""
    arms: list[float] = []
    cancels: list[int] = []
    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=exit_after,
        poll_interval=0.01,
        dump=lambda: None,
        arm_later=lambda t: arms.append(t),
        cancel_later=lambda: cancels.append(1),
        log=logging.getLogger("test.loop_watchdog"),
    )
    return wd, arms, cancels


_EXIT_BUDGET = 25.0


def test_start_primes_armed_timer() -> None:
    wd, arms, cancels = _make_armed(exit_after=25.0)
    wd.start()
    try:
        # Primed once on start: a cancel of any stale timer, then a fresh arm at
        # exit_after.
        assert arms == [_EXIT_BUDGET]
        assert cancels == [1]
    finally:
        wd.stop()


def test_beat_re_pets_armed_timer() -> None:
    wd, arms, cancels = _make_armed(exit_after=25.0)
    wd.start()
    try:
        wd.beat()
        wd.beat()
        # start() armed once, then each beat cancels + re-arms with exit_after.
        assert arms == [_EXIT_BUDGET] * 3
        assert len(cancels) == 3
    finally:
        wd.stop()


def test_stop_cancels_armed_timer() -> None:
    wd, arms, cancels = _make_armed(exit_after=25.0)
    wd.start()
    cancels.clear()
    wd.stop()
    # stop() cancels the pending dump-then-exit timer so a clean shutdown does
    # not leave a timer that would _exit() the process mid-teardown.
    assert cancels == [1]


def test_beat_does_not_arm_before_start() -> None:
    wd, arms, cancels = _make_armed(exit_after=25.0)
    # Heartbeat may beat() before start() (or in a dashboard-only process where
    # start() is never called) -> the armed timer must stay disarmed.
    wd.beat()
    assert arms == []
    assert cancels == []


def test_exit_after_none_disables_armed_timer() -> None:
    wd, arms, cancels = _make_armed(exit_after=None)
    wd.start()
    try:
        wd.beat()
        wd.beat()
        # Armed timer fully off; only the soft daemon-thread dump remains. This
        # is the dashboard-only process configuration (e.g. `kirocrew chat`,
        # which never enables faulthandler): NO process-killing timer is ever
        # armed, even across start()+beats, so a non-gateway process can never
        # be _exit()'d by the watchdog.
        assert arms == []
        assert cancels == []
    finally:
        wd.stop()


def test_beat_swallows_rearm_failure() -> None:
    # If cancel/arm raise (e.g. faulthandler hiccup), beat() must NOT propagate —
    # petting the watchdog can never be allowed to crash the event-loop heartbeat.
    boom_arms: list[float] = []

    def _boom_arm(_t: float) -> None:
        boom_arms.append(_t)
        raise RuntimeError("arm blew up")

    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=25.0,
        poll_interval=0.01,
        dump=lambda: None,
        arm_later=_boom_arm,
        cancel_later=lambda: None,
        log=logging.getLogger("test.loop_watchdog"),
    )
    wd.start()  # arms once (and swallows the failure)
    try:
        wd.beat()  # must not raise even though re-arm throws
        wd.beat()
        assert len(boom_arms) >= 1  # we did attempt to arm
    finally:
        wd.stop()


def test_start_stop_thread_lifecycle() -> None:
    wd, _arms, _cancels = _make_armed(exit_after=25.0)
    assert wd.is_running() is False
    wd.start()
    assert wd.is_running() is True
    # Idempotent start: a second start() does not spawn a second thread.
    wd.start()
    assert wd.is_running() is True
    wd.stop()
    assert wd.is_running() is False


# ---------------------------------------------------------------------------
# Stall enrichment stage (journal-only capture; dump file is a crash sentinel
# that only faulthandler may write into — see crash_dump_store._is_header_only)
# ---------------------------------------------------------------------------


class _FakeDumpFile:
    """DumpFile stand-in proving the watchdog NEVER writes into the sentinel."""

    def __init__(self) -> None:
        self.written: list[str] = []
        self.flushes = 0

    def write(self, data: str) -> int:
        self.written.append(data)
        return len(data)

    def flush(self) -> None:
        self.flushes += 1


class _RecordingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        self.records.append(record)


def _make_enrich(enrich_after: float = 15.0, stall_after: float = 30.0):
    """Fixture for the enrichment stage: fake clock, sentinel-guard dump file,
    recording collector + logger; armed timer disabled so nothing touches real
    faulthandler."""
    clock = _Clock()
    dump_file = _FakeDumpFile()
    calls: list[float] = []
    handler = _RecordingHandler()
    log = logging.Logger("test.loop_watchdog.enrich")
    log.addHandler(handler)

    def collector(silence: float) -> list[str]:
        calls.append(silence)
        return [f"=== STALL ENRICHMENT (test) silence={silence:.1f}s ==="]

    wd = LoopStallWatchdog(
        stall_after=stall_after,
        exit_after=None,
        poll_interval=5.0,
        now=clock,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        dump_file=dump_file,
        enrich_after=enrich_after,
        enrich=collector,
        log=log,
    )
    return wd, clock, dump_file, calls, handler


def test_enrichment_fires_once_per_episode_at_threshold() -> None:
    wd, clock, dump_file, calls, handler = _make_enrich()

    clock.advance(10.0)  # below enrich_after
    wd.check()
    assert calls == []

    clock.advance(6.0)  # silence 16s >= 15s
    wd.check()
    assert len(calls) == 1
    assert calls[0] >= 15.0
    assert any("STALL ENRICHMENT" in m for m in handler.messages)

    clock.advance(5.0)  # still the same episode — no second capture
    wd.check()
    assert len(calls) == 1


def test_recoverable_stall_leaves_dump_file_untouched() -> None:
    # Sentinel integrity: a 15-25s stall that recovers must leave loopstall-*.txt
    # byte-identical, or the
    # next boot misclassifies the session as crashed (_is_header_only counts
    # lines) — false "work lost" notification, cautious boot, unreapable file.
    wd, clock, dump_file, calls, handler = _make_enrich()

    clock.advance(16.0)  # cross enrich_after
    wd.check()
    assert len(calls) == 1

    wd.beat()  # loop recovers before exit_after would have fired
    wd.check()
    assert any("recovered after stall enrichment" in m for m in handler.messages)

    clock.advance(16.0)  # a second, distinct stall episode re-enriches
    wd.check()
    assert len(calls) == 2

    # The invariant under test: zero writes into the crash sentinel, ever.
    assert dump_file.written == []
    assert dump_file.flushes == 0


def test_enrichment_collector_failure_is_contained() -> None:
    clock = _Clock()
    dump_file = _FakeDumpFile()
    handler = _RecordingHandler()
    log = logging.Logger("test.loop_watchdog.enrich")
    log.addHandler(handler)

    def broken(_silence: float) -> list[str]:
        raise RuntimeError("collector exploded")

    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=None,
        poll_interval=5.0,
        now=clock,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        dump_file=dump_file,
        enrich_after=15.0,
        enrich=broken,
        log=log,
    )
    clock.advance(16.0)
    wd.check()  # must not raise
    assert any("ENRICHMENT FAILED" in m for m in handler.messages)
    assert dump_file.written == []  # failure marker goes to the log, never the sentinel


def test_enrichment_without_dump_file_only_logs() -> None:
    clock = _Clock()
    calls: list[float] = []
    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=None,
        poll_interval=5.0,
        now=clock,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        dump_file=None,
        enrich_after=15.0,
        enrich=lambda s: (calls.append(s) or ["line"]),
        log=logging.getLogger("test.loop_watchdog.enrich"),
    )
    clock.advance(16.0)
    wd.check()  # must not raise despite no file target
    assert len(calls) == 1


def test_enrichment_precedes_soft_dump_threshold() -> None:
    # One episode crossing both thresholds: enrichment at 15s, soft dump at 30s.
    wd, clock, dump_file, calls, handler = _make_enrich()
    clock.advance(16.0)
    assert wd.check() is False  # enriched, not yet soft-dumped
    assert len(calls) == 1
    clock.advance(15.0)  # silence 31s
    assert wd.check() is True  # soft dump fires; enrichment still once
    assert len(calls) == 1
    assert dump_file.written == []  # even a full episode writes nothing itself


# ---------------------------------------------------------------------------
# A suspend is not a stall: the daemon thread reads the monotonic clock beside
# a suspend-inclusive one and reports a resume; the alarm is the one decider
# ---------------------------------------------------------------------------


class _SuspendFixture:
    """Watchdog with two injected clocks: ``mono`` is the monotonic clock the
    daemon thread reads; ``suspend`` is the suspend-inclusive clock (boot or
    wall) that keeps counting while the host sleeps.  A real suspend is
    modelled by advancing ``suspend`` further than ``mono``.  The alarm is a
    recorder, so "no exit" is "the daemon thread armed nothing and dumped
    nothing"; the alarm's own pause during a suspend is the kernel's property.

    ``poll_interval`` is one hour, so the real daemon thread ``start()`` spawns
    first wakes an hour out — far beyond any test here — and never runs
    ``check()`` under a test: every check is stepped by hand.
    """

    def __init__(
        self,
        *,
        exit_after: float | None = 25.0,
        stall_after: float = 30.0,
        enrich_after: float = 15.0,
    ) -> None:
        self.mono = _Clock()
        self.suspend = _Clock()
        self.arms: list[float] = []
        self.cancels: list[int] = []
        self.dumps: list[float] = []
        self.dump_file = _FakeDumpFile()
        self.handler = _RecordingHandler()
        log = logging.Logger("test.loop_watchdog.suspend")
        log.addHandler(self.handler)
        self.wd = LoopStallWatchdog(
            stall_after=stall_after,
            exit_after=exit_after,
            poll_interval=3600.0,
            now=self.mono,
            suspend_clock=self.suspend,
            dump=lambda: self.dumps.append(self.mono.t),
            arm_later=lambda t: self.arms.append(t),
            cancel_later=lambda: self.cancels.append(1),
            dump_file=self.dump_file,
            enrich_after=enrich_after,
            enrich=lambda s: ["=== STALL ENRICHMENT (test) ==="],
            log=log,
        )

    def tick(self, mono: float, suspend: float | None = None) -> bool:
        """Advance both clocks (``suspend`` defaults to the same delta) and check."""
        self.mono.advance(mono)
        self.suspend.advance(mono if suspend is None else suspend)
        return self.wd.check()

    def records(self, level: int) -> list[str]:
        return [r.getMessage() for r in self.handler.records if r.levelno == level]


def test_suspend_is_not_a_stall() -> None:
    """A wall-clock jump of 120 s with a monotonic delta of 5 s is a suspend:
    exactly one INFO line, no dump, nothing written to the crash file, the
    daemon thread touches no timer, and the heartbeat's next beat re-arms the
    alarm for ``exit_after`` as on any other tick."""
    fx = _SuspendFixture(exit_after=25.0)
    fx.wd.start()
    try:
        fx.wd.beat()
        arms_before, cancels_before = list(fx.arms), list(fx.cancels)
        # Baseline poll, then the host sleeps: the suspend-inclusive clock
        # advances 125 s while the monotonic clock advances 5 s.
        assert fx.tick(5.0) is False
        assert fx.tick(5.0, suspend=125.0) is False
        info = [m for m in fx.records(logging.INFO) if "suspend" in m]
        assert len(info) == 1
        assert "120" in info[0]
        assert fx.dumps == []
        assert fx.dump_file.written == []
        assert (fx.arms, fx.cancels) == (arms_before, cancels_before)
        # A later quiet poll does not repeat the line.
        assert fx.tick(5.0) is False
        assert len([m for m in fx.records(logging.INFO) if "suspend" in m]) == 1
        # The heartbeat's own re-arm is unaffected.
        fx.wd.beat()
        assert fx.arms == arms_before + [25.0]
    finally:
        fx.wd.stop()


def test_overnight_suspend_is_still_not_a_stall() -> None:
    fx = _SuspendFixture(exit_after=25.0)
    fx.wd.start()
    try:
        fx.wd.beat()
        assert fx.tick(5.0) is False
        # Eight hours on the suspend-inclusive clock, one poll on the monotonic one.
        assert fx.tick(5.0, suspend=8 * 3600.0) is False
        assert fx.dumps == []
        assert fx.dump_file.written == []
        assert any("suspend" in m for m in fx.records(logging.INFO))
    finally:
        fx.wd.stop()


def test_skew_below_the_floor_is_not_reported_as_a_suspend() -> None:
    fx = _SuspendFixture(exit_after=25.0)
    fx.wd.start()
    try:
        fx.wd.beat()
        fx.tick(5.0)
        fx.tick(5.0, suspend=5.0 + SUSPEND_SKEW_MIN_SECS / 2)
        assert not any("suspend" in m for m in fx.records(logging.INFO))
    finally:
        fx.wd.stop()


def test_suspend_clock_none_disables_the_comparison() -> None:
    """A platform without a suspend-inclusive clock never reports a resume."""
    clock = _Clock()
    handler = _RecordingHandler()
    log = logging.Logger("test.loop_watchdog.suspend.none")
    log.addHandler(handler)
    wd = LoopStallWatchdog(
        exit_after=None,
        poll_interval=3600.0,
        now=clock,
        suspend_clock=lambda: None,
        dump=lambda: None,
        arm_later=lambda _t: None,
        cancel_later=lambda: None,
        log=log,
    )
    wd.beat()
    for _ in range(3):
        clock.advance(5.0)
        assert wd.check() is False
    assert not any("suspend" in m for m in handler.messages)


class _AlarmSeam:
    """The alarm branch's platform bindings, stubbed so the branch runs the same on
    every runner: a stand-in ``ITIMER_REAL`` (never the real one, which a test
    worker's own timeout may own), a ``SIGALRM`` number where the platform has none
    (Windows), a ``getsignal`` answer the test sets through :attr:`owner`, and
    recording ``faulthandler.register`` / ``unregister`` where the real ones are
    absent.  The real ``process_alarm_available`` and ``arm_process_alarm`` then
    take the alarm branch against the fake timer, so what a test asserts is the
    production wiring, not a platform's presence."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.timer = {"value": 0.0}  # seconds pending on the stand-in ITIMER_REAL
        self.owner: object = signal.SIG_DFL  # what ``signal.getsignal(SIGALRM)`` answers
        self.registrations: list[tuple[object, ...]] = []
        monkeypatch.setattr(signal, "SIGALRM", getattr(signal, "SIGALRM", 14), raising=False)
        monkeypatch.setattr(signal, "ITIMER_REAL", 99, raising=False)
        monkeypatch.setattr(
            signal,
            "setitimer",
            lambda which, secs: self.timer.__setitem__("value", secs),
            raising=False,
        )
        monkeypatch.setattr(
            signal, "getitimer", lambda which: (self.timer["value"], 0.0), raising=False
        )
        monkeypatch.setattr(signal, "getsignal", lambda signum: self.owner)
        monkeypatch.setattr(
            loop_watchdog.faulthandler,
            "register",
            lambda signum, file=None, all_threads=True, chain=False: self.registrations.append(
                ("register", signum, file, all_threads, chain)
            ),
            raising=False,
        )
        monkeypatch.setattr(
            loop_watchdog.faulthandler,
            "unregister",
            lambda signum: (self.registrations.append(("unregister", signum)) or False),
            raising=False,
        )
        monkeypatch.setattr(loop_watchdog, "_armed_mechanism", None)


def test_armed_line_names_the_exit_mechanism(monkeypatch: pytest.MonkeyPatch) -> None:
    _AlarmSeam(monkeypatch)
    fx = _SuspendFixture(exit_after=25.0)
    fx.wd.start()
    try:
        armed = [m for m in fx.records(logging.INFO) if "watchdog armed" in m]
        assert len(armed) == 1
        assert "exit_after=25s (alarm)" in armed[0]
    finally:
        fx.wd.stop()


# ── The alarm wiring on both platform branches ───────────────────────────────


def test_default_arm_registers_faulthandler_on_sigalrm_and_starts_the_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The production dump-then-exit: faulthandler dumps from the signal handler
    (chained, so the default disposition then ends the process) and the kernel's
    ITIMER_REAL countdown delivers SIGALRM after ``exit_after``.  The
    registration is released before it is renewed, so a handler a temporary
    owner overwrote is installed again rather than assumed present."""
    seam = _AlarmSeam(monkeypatch)
    dump_file = _FakeDumpFile()
    loop_watchdog._default_arm_later(25.0, dump_file)
    assert seam.registrations == [
        ("unregister", signal.SIGALRM),
        ("register", signal.SIGALRM, dump_file, True, True),
    ]
    assert seam.timer["value"] == 25.0
    assert loop_watchdog.exit_mechanism() == "alarm"
    loop_watchdog._default_cancel_later()
    assert seam.timer["value"] == 0.0


def test_default_arm_falls_back_to_faulthandler_timer_without_a_process_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Where the platform has no process alarm, faulthandler's own timer thread
    carries the dump-then-exit at the same budget, and cancel targets it too.
    This is the Windows branch, so it runs there: ``faulthandler.register``
    does not exist on Windows, hence ``raising=False`` on its guard."""
    later: list[tuple[float, bool, object, bool]] = []
    cancels: list[int] = []
    monkeypatch.setattr(loop_watchdog, "process_alarm_available", lambda: False)
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "register",
        lambda *a, **k: pytest.fail("no signal path here"),
        raising=False,
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "dump_traceback_later",
        lambda timeout, repeat, file, exit: later.append((timeout, repeat, file, exit)),
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler, "cancel_dump_traceback_later", lambda: cancels.append(1)
    )
    dump_file = _FakeDumpFile()
    loop_watchdog._default_arm_later(25.0, dump_file)
    assert later == [(25.0, False, dump_file, True)]
    assert loop_watchdog.exit_mechanism() == "faulthandler"
    loop_watchdog._default_cancel_later()
    assert cancels == [1]


def test_alarm_yields_to_a_foreign_sigalrm_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that already handles SIGALRM from Python (pytest-timeout in a
    test worker, an embedding host) owns ITIMER_REAL too: the watchdog must not
    replace its handler or its deadline, so it uses faulthandler's timer."""
    seam = _AlarmSeam(monkeypatch)
    seam.owner = lambda *_a: None
    monkeypatch.setattr(
        loop_watchdog.faulthandler, "register", lambda *a, **k: pytest.fail("must not take SIGALRM")
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "unregister",
        lambda signum: pytest.fail("must not release the owner's SIGALRM"),
    )
    monkeypatch.setattr(
        loop_watchdog, "arm_process_alarm", lambda secs: pytest.fail("must not touch ITIMER_REAL")
    )
    later: list[float] = []
    cancels: list[int] = []
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "dump_traceback_later",
        lambda timeout, repeat, file, exit: later.append(timeout),
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler, "cancel_dump_traceback_later", lambda: cancels.append(1)
    )
    assert loop_watchdog.exit_mechanism() == "faulthandler"
    loop_watchdog._default_arm_later(25.0, None)
    loop_watchdog._default_cancel_later()
    assert later == [25.0]
    assert cancels == [1]


def test_cancel_targets_the_mechanism_the_last_arm_used(monkeypatch: pytest.MonkeyPatch) -> None:
    """A foreign ``signal.signal(SIGALRM, ...)`` installed *after* an arm flips
    :func:`exit_mechanism`.  The next beat must still cancel the itimer that
    arm started (``setitimer(ITIMER_REAL, 0)``), not faulthandler's idle timer;
    re-deriving the mechanism at cancel time strands the pending alarm to fire
    into the foreign handler while a second exit is armed beside it."""
    seam = _AlarmSeam(monkeypatch)
    armed: list[float] = []
    later: list[float] = []
    cancels: list[int] = []
    monkeypatch.setattr(
        loop_watchdog, "arm_process_alarm", lambda secs: (armed.append(secs) or True)
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "dump_traceback_later",
        lambda timeout, repeat, file, exit: later.append(timeout),
    )
    monkeypatch.setattr(
        loop_watchdog.faulthandler, "cancel_dump_traceback_later", lambda: cancels.append(1)
    )
    wd = LoopStallWatchdog(
        stall_after=30.0,
        exit_after=25.0,
        poll_interval=3600.0,
        dump=lambda: None,
        dump_file=_FakeDumpFile(),
        log=logging.getLogger("test.loop_watchdog"),
    )
    wd.start()
    try:
        assert armed == [25.0]  # armed under the alarm; nothing of ours to cancel first
        assert later == []
        # A foreign owner takes SIGALRM mid-run (pytest-timeout's signal method,
        # an embedding host).  The derivation now answers "faulthandler".
        seam.owner = lambda *_a: None
        assert loop_watchdog.exit_mechanism() == "faulthandler"
        wd.beat()
        # The beat cancels the itimer the last arm started ...
        assert armed == [25.0, 0.0]
        # ... not faulthandler's timer, which was never armed ...
        assert cancels == []
        # ... and the re-arm follows the derivation onto faulthandler's timer, so
        # exactly one exit is pending, on the mechanism a foreign owner leaves free.
        assert later == [25.0]
        wd.beat()
        # From here each beat cancels that timer; ITIMER_REAL is left alone.
        assert cancels == [1]
        assert later == [25.0, 25.0]
        assert armed == [25.0, 0.0]
    finally:
        wd.stop()
    # stop() cancels what the last arm used, too.
    assert cancels == [1, 1]
    assert armed == [25.0, 0.0]


def test_alarm_is_used_while_sigalrm_is_at_its_default(monkeypatch: pytest.MonkeyPatch) -> None:
    _AlarmSeam(monkeypatch)
    assert loop_watchdog.exit_mechanism() == "alarm"


def test_a_process_that_owns_sigalrm_keeps_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pytest-timeout's signal method, an embedding host: a Python handler on
    SIGALRM means the process owns ITIMER_REAL and has a deadline pending on it.
    The owner is constructed here through the seam rather than hoped for from
    the runner's configuration, and its deadline is read back off the stand-in
    timer after a full arm/cancel cycle and the successor's inherited-alarm
    check: neither touched it, and faulthandler's registration was left alone."""
    seam = _AlarmSeam(monkeypatch)
    seam.owner = lambda *_a: None
    signal.setitimer(signal.ITIMER_REAL, 7.0)  # the owner's own deadline
    later: list[float] = []
    monkeypatch.setattr(
        loop_watchdog.faulthandler,
        "dump_traceback_later",
        lambda timeout, repeat, file, exit: later.append(timeout),
    )
    monkeypatch.setattr(loop_watchdog.faulthandler, "cancel_dump_traceback_later", lambda: None)
    assert loop_watchdog.exit_mechanism() == "faulthandler"
    loop_watchdog._default_arm_later(25.0, None)
    loop_watchdog._default_cancel_later()
    assert loop_watchdog.disarm_inherited_alarm() is False
    assert later == [25.0]
    assert signal.getitimer(signal.ITIMER_REAL) == (7.0, 0.0)
    assert seam.registrations == []


# ── The real alarm, end to end, in child processes ───────────────────────────


def _stall_exit_status() -> int:
    """How the platform's dump-then-exit ends a stalled child: the chained default
    disposition of ``SIGALRM`` where the process alarm exists, faulthandler's
    ``_exit(1)`` from its timer thread where it does not (Windows).  Both are the
    production mechanism for that platform, at the same budget, with the same
    dump; the tests below assert whichever one the child under test really has."""
    return -signal.SIGALRM if platform_compat.process_alarm_available() else 1


def _run_child(script: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Run *script* in a child interpreter whose CWD is inside ``tmp_path``, so
    nothing the child creates can land outside the test's own directory.

    ``-X faulthandler`` is diagnostic only: the alarm path registers its own
    ``SIGALRM`` handler either way, and this adds the fatal-signal handlers, so
    a child that dies of ``SIGSEGV`` (``-11``, seen only on macOS) prints
    the crashing thread's stack to stderr instead of dying silently."""
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    return subprocess.run(
        [sys.executable, "-X", "faulthandler", "-c", script],
        capture_output=True,
        timeout=30,
        cwd=str(cwd),
        **UTF8_TEXT,
    )


def _stall_evidence(proc: subprocess.CompletedProcess[str], dump: Path) -> tuple[object, ...]:
    """What a failed exit-status assertion must carry: the status, the child's
    stderr (the fatal-signal stack, when there is one) and the dump file, so a
    red names whether the dump landed before the child died."""
    try:
        text = dump.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        text = f"<dump unreadable: {exc}>"
    return proc.returncode, proc.stderr, text[-4000:]


def _child_script(dump: Path, blocker: str) -> str:
    """A child whose only exit path is the alarm, with its daemon thread parked.

    The alarm dump walks every thread's frames from inside the signal handler,
    with no GIL. A daemon thread that is running Python at that instant can have
    a frame change under the walk, and the child then dies by ``SIGSEGV`` part
    way through the dump instead of by ``SIGALRM`` after it. ``stall_after`` is
    longer than any child lives, so no poll here can ever act; a poll interval
    longer than that keeps the daemon thread blocked in ``Event.wait`` for the
    child's whole life, and the dump only ever walks stable frames.
    """
    return textwrap.dedent(f"""
        import logging, re, signal, sys, time
        from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog
        f = open({str(dump)!r}, "w", encoding="utf-8")
        f.write("# header\\n\\n")
        wd = LoopStallWatchdog(
            stall_after=30.0, exit_after=0.2, poll_interval=60.0, enrich_after=10.0,
            dump_file=f, log=logging.getLogger("t"),
        )
        wd.start()
        {blocker}
        print("still alive", file=sys.stderr)
        sys.exit(3)
        """)


def test_real_stall_ends_the_process_by_the_alarm(tmp_path: Path) -> None:
    """The class every production stall belongs to: the loop thread blocked in
    a syscall (here ``time.sleep``, which releases the GIL) and never beating.
    The alarm fires at ``exit_after``, faulthandler dumps every thread from the
    signal handler, and the chained default disposition ends the process."""
    dump = tmp_path / "loopstall.txt"
    proc = _run_child(_child_script(dump, "time.sleep(10.0)"), tmp_path)
    assert proc.returncode == _stall_exit_status(), _stall_evidence(proc, dump)
    text = dump.read_text(encoding="utf-8", errors="replace")
    assert "Thread 0x" in text
    assert "<module>" in text
    assert "still alive" not in proc.stderr


def test_real_gil_holding_stall_ends_the_process_by_the_alarm(tmp_path: Path) -> None:
    """A thread inside a C call that never releases the GIL (catastrophic
    regular expression on the main thread) starves the heartbeat and the
    daemon thread alike.  The kernel alarm still fires: faulthandler dumps from
    the signal handler with no GIL, and the process ends with SIGALRM."""
    dump = tmp_path / "loopstall.txt"
    # Exponential backtracking: holds the GIL for far longer than any budget here.
    proc = _run_child(_child_script(dump, 're.match(r"(a+)+$", "a" * 64 + "b")'), tmp_path)
    assert proc.returncode == _stall_exit_status(), _stall_evidence(proc, dump)
    text = dump.read_text(encoding="utf-8", errors="replace")
    assert "Thread 0x" in text
    assert "<module>" in text
    assert "still alive" not in proc.stderr


def test_alarm_dump_survives_a_temporary_sigalrm_owner_that_restores_sig_dfl(
    tmp_path: Path,
) -> None:
    """A temporary ``SIGALRM`` owner (a library's own alarm, a test worker's
    timeout) installs a Python handler and later hands the signal back with
    ``SIG_DFL``.  That restore overwrites faulthandler's C handler, and
    ``faulthandler.register`` reinstalls nothing while it believes it still
    holds the signal, so the next beat's re-arm must release the registration
    and register afresh: the alarm that follows still dumps every thread before
    the chained default disposition ends the process.  Without that, the process
    dies by ``SIGALRM`` with an empty dump file.  Where the platform has no
    ``SIGALRM`` there is no owner to hand it back, and the same child stalls
    straight into faulthandler's timer."""
    dump = tmp_path / "loopstall.txt"
    blocker = (
        "alarm = getattr(signal, 'SIGALRM', None); "
        "alarm is None or signal.signal(alarm, lambda *_a: None); "
        "alarm is None or signal.signal(alarm, signal.SIG_DFL); "
        "wd.beat(); time.sleep(10.0)"
    )
    proc = _run_child(_child_script(dump, blocker), tmp_path)
    assert proc.returncode == _stall_exit_status(), _stall_evidence(proc, dump)
    text = dump.read_text(encoding="utf-8", errors="replace")
    assert "Thread 0x" in text, text
    assert "<module>" in text
    assert "still alive" not in proc.stderr


def test_restart_exec_does_not_hand_the_successor_the_alarm(tmp_path: Path) -> None:
    """``execve`` preserves ``ITIMER_REAL`` and resets a caught ``SIGALRM`` to
    its default disposition, so an armed gateway that execs its successor would
    have that successor ended by a deadline it never armed, during its own boot.
    The exec seam cancels the alarm first: here the successor image sleeps past
    the predecessor's 0.2 s budget and exits on its own terms.  Where the
    platform has no process alarm there is no deadline to inherit; the same
    child still proves the exec replaced the predecessor before its budget and
    that the successor ran, while the exit status is the platform's ``execv``
    contract (Windows spawns the successor and ends the caller itself)."""
    dump = tmp_path / "loopstall.txt"
    successor = (
        "import sys, time; time.sleep(1.0); print('successor alive', file=sys.stderr); sys.exit(7)"
    )
    blocker = (
        "from kiro_crew.platform_compat import reexec_launcher; "
        f"reexec_launcher(sys.executable, ['-c', {successor!r}])"
    )
    proc = _run_child(_child_script(dump, blocker), tmp_path)
    if platform_compat.process_alarm_available():
        assert proc.returncode == 7, (proc.returncode, proc.stderr)
    assert "successor alive" in proc.stderr
    assert "still alive" not in proc.stderr  # the predecessor image was replaced
    assert "Traceback" not in proc.stderr
    assert "Thread 0x" not in dump.read_text(encoding="utf-8", errors="replace")


# ── The successor's half: a deadline inherited across exec is not ours ────────


def test_disarm_inherited_alarm_clears_a_timer_nobody_in_this_image_armed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seam = _AlarmSeam(monkeypatch)
    seam.timer["value"] = 25.0  # the predecessor's deadline, preserved across exec
    assert loop_watchdog.disarm_inherited_alarm() is True
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_disarm_inherited_alarm_leaves_a_foreign_owners_deadline_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ownership rule is :func:`exit_mechanism`'s: a Python handler on
    SIGALRM means someone else owns ITIMER_REAL, so nothing here touches it."""
    seam = _AlarmSeam(monkeypatch)
    seam.owner = lambda *_a: None
    seam.timer["value"] = 7.0
    assert loop_watchdog.disarm_inherited_alarm() is False
    assert signal.getitimer(signal.ITIMER_REAL) == (7.0, 0.0)


def test_disarm_inherited_alarm_is_a_no_op_without_a_process_alarm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The Windows branch: no timer exists to inherit, so nothing is asked of
    ``signal``; this test runs there."""
    monkeypatch.setattr(loop_watchdog, "process_alarm_available", lambda: False)
    monkeypatch.setattr(
        loop_watchdog, "arm_process_alarm", lambda secs: pytest.fail("no timer on this platform")
    )
    assert loop_watchdog.disarm_inherited_alarm() is False
