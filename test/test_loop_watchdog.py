"""Tests for the off-loop event-loop stall watchdog.

The decision logic (``check``) is driven directly with an injected clock and a
recording dump callback — no real threads or sleeps — so the soft-dump state
machine is verified deterministically.  The C-level dump-then-exit armed timer
is verified through injected ``arm_later``/``cancel_later`` recorders, so tests
assert the wiring without ever arming a real process-killing timer.
``start``/``stop`` thread lifecycle gets one real-thread smoke test.
"""

from __future__ import annotations

import logging

from kiro_crew.dashboard.loop_watchdog import LoopStallWatchdog


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


def test_start_primes_armed_timer() -> None:
    wd, arms, cancels = _make_armed(exit_after=25.0)
    wd.start()
    try:
        # Primed once on start: a cancel of any stale timer, then a fresh arm.
        assert arms == [25.0]
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
        assert arms == [25.0, 25.0, 25.0]
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
