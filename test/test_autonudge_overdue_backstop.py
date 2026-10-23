"""A legacy loop left active, unheld and days past its deadline.

Three candidate shapes, each driven with an injected wall clock. On origin/main
the first two stayed stranded with no log line; the third was already rescued by
the two-pass reconciler:

1. every fire is refused, so a live backoff timer re-arms forever;
2. a fire wedges inside the fire window, so the loop sits in ``_firing``;
3. an approval hold is released unarmed and no turn completes afterwards.

The overdue backstop in ``_reconcile_once`` names each with one WARNING per
episode and arms nothing: the refused shape keeps its backoff timer, and a fire
window is never cancelled. The
last three tests pin healthy turns that also leave the deadline behind -- a long
channel turn, a retry in the fire window, a long user turn -- as named by nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import pytest

from kiro_crew.autonudge import AutoNudgeService, NudgeLoop
from kiro_crew.autonudge_service import firing as _firing
from kiro_crew.autonudge_service import timers as _timers
from kiro_crew.config.sections_limits import CHAT_TURN_TIMEOUT_MAX
from kiro_crew.testing.clock import ManualClock


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture
def svc(tmp_path, event_loop):
    service = AutoNudgeService(base_dir=tmp_path)
    yield service
    tasks = [
        t
        for t in (service._reconciler, *service._timers.values(), *service._inflight_adds)
        if t is not None
    ]
    service.stop()
    live = [t for t in tasks if not t.done()]
    for t in live:
        t.cancel()
    if live and not event_loop.is_closed():
        with contextlib.suppress(asyncio.TimeoutError):
            event_loop.run_until_complete(
                asyncio.wait_for(asyncio.gather(*live, return_exceptions=True), timeout=30)
            )


@pytest.fixture
def clock(monkeypatch):
    """The service's wall clock, moved only by the test.

    Installed on ``autonudge_service.timers``, which stamps every deadline and
    runs the reconciler pass, and on ``autonudge_service.firing``, which stamps
    when a fire window opens.
    """
    c = ManualClock(start=time.time())
    c.install(monkeypatch, _timers)
    c.install(monkeypatch, _firing)
    return c


def _move_to(clock: ManualClock, when: float) -> None:
    clock.advance(when - clock.time())


IDLE = 900

# Bound on every wait a test body makes for work it started itself, so a tick
# that never reaches the fire fails at that line instead of hanging the worker.
WAIT_SECS = 30


def _overdue_warnings(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records if r.levelno == logging.WARNING and "overdue" in r.getMessage()
    ]


async def _tick_now(svc: AutoNudgeService, loop: NudgeLoop) -> asyncio.Task:
    """Start one zero-delay tick as the loop's ``_timers`` entry, as production does."""
    svc._cancel_timer(loop.id)
    task = asyncio.get_running_loop().create_task(svc._timer(loop, delay=0))
    svc._timers[loop.id] = task
    return task


@pytest.mark.asyncio
async def test_branch1_refused_fires_are_named_not_rearmed(svc, clock, caplog):
    """Every fire refused: a live backoff timer re-arms forever, the deadline days behind."""

    async def refuse(_loop: NudgeLoop) -> bool:
        return False

    svc._on_fire = refuse
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)
    backoff = svc._timers[loop.id]
    assert not backoff.done()
    assert svc._rearm_fail_count[loop.id] == 1

    _move_to(clock, loop.next_due_ts + 3 * IDLE)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()

    assert loop.active and not loop.approval_stalled
    assert loop.cycle_count == 0
    # On main the reconciler saw a live timer and said nothing. Now: one WARNING
    # naming the shape, and the backoff timer is left to retry on its own.
    warned = _overdue_warnings(caplog)
    assert len(warned) == 1
    assert "refused_fires" in warned[0].getMessage()
    assert svc._timers[loop.id] is backoff

    # Later passes in the same episode do not warn again or arm anything.
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
    assert len(_overdue_warnings(caplog)) == 1
    assert svc._timers[loop.id] is backoff


async def await_tick(svc: AutoNudgeService, loop: NudgeLoop) -> None:
    task = await _tick_now(svc, loop)
    await asyncio.wait_for(task, WAIT_SECS)


@pytest.mark.asyncio
async def test_branch2_wedged_fire_is_named_not_cancelled(svc, clock, caplog):
    """A fire that never returns: the loop stays in ``_firing`` and nothing notices."""
    never: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    entered = asyncio.Event()

    async def wedge(_loop: NudgeLoop) -> bool:
        entered.set()
        return await never

    svc._on_fire = wedge
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    _move_to(clock, loop.next_due_ts + 1)
    tick = await _tick_now(svc, loop)
    await asyncio.wait_for(entered.wait(), WAIT_SECS)
    assert loop.id in svc._firing

    _move_to(clock, svc._fire_started[loop.id] + _timers._WEDGED_FIRE_SECS + 1)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()

    # The fire window is never cancelled, so the tick is still parked in it.
    assert not tick.done()
    assert loop.id in svc._firing
    # On main it was skipped as mid-fire, silently. Now it is named, once.
    warned = _overdue_warnings(caplog)
    assert len(warned) == 1
    assert "wedged_fire" in warned[0].getMessage()
    never.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(tick, WAIT_SECS)


@pytest.mark.asyncio
async def test_branch3_unarmed_hold_release_is_already_rescued(svc, clock, caplog):
    """Hold released with ``arm=False`` and no turn after it: the reconciler re-arms it."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    svc.notify_approval_stalled(loop.slot_key)
    await asyncio.wait_for(asyncio.gather(*list(svc._inflight_adds)), WAIT_SECS)
    assert loop.approval_stalled
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)  # held: fires nothing, arms nothing
    assert svc._timers[loop.id].done()

    released = await svc.release_approval_hold(
        loop.slot_key, why="a person sent a message", arm=False
    )
    assert released and not loop.approval_stalled
    assert svc._timers[loop.id].done()

    _move_to(clock, loop.next_due_ts + 3 * IDLE)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()
    timer = svc._timers.get(loop.id)
    assert timer is not None and not timer.done()
    # The two-pass rescue owns this shape; the backstop names nothing.
    assert _overdue_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_held_loop_past_its_deadline_is_not_overdue(svc, clock, caplog):
    """An approval hold is past its deadline on purpose: no WARNING, no re-arm."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    svc.notify_approval_stalled(loop.slot_key)
    await asyncio.wait_for(asyncio.gather(*list(svc._inflight_adds)), WAIT_SECS)
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)
    held_timer = svc._timers[loop.id]

    _move_to(clock, loop.next_due_ts + 10 * IDLE)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()

    assert loop.approval_stalled
    assert svc._timers[loop.id] is held_timer
    assert _overdue_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_loop_within_two_intervals_is_not_overdue(svc, clock, caplog):
    """Just under the threshold, a refused loop is left to its own backoff."""

    async def refuse(_loop: NudgeLoop) -> bool:
        return False

    svc._on_fire = refuse
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)
    backoff = svc._timers[loop.id]

    _move_to(clock, loop.next_due_ts + 2 * IDLE)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()

    assert svc._timers[loop.id] is backoff
    assert _overdue_warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_new_overdue_episode_warns_again(svc, clock, caplog):
    """A loop back on schedule ends its episode; the next stall is named again."""

    async def refuse(_loop: NudgeLoop) -> bool:
        return False

    svc._on_fire = refuse
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)

    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        _move_to(clock, loop.next_due_ts + 3 * IDLE)
        svc._reconcile_once()
        # Back on schedule: a fresh deadline ahead of the clock.
        loop.next_due_ts = clock.time() + IDLE
        svc._reconcile_once()
        assert loop.id not in svc._overdue_warned
        _move_to(clock, loop.next_due_ts + 3 * IDLE)
        svc._reconcile_once()

    assert [("refused_fires" in r.getMessage()) for r in _overdue_warnings(caplog)] == [True, True]


@pytest.mark.asyncio
async def test_finding1_long_channel_turn_is_not_named(svc, clock, caplog):
    """A channel loop runs its turn inside the fire window: past its deadline is not wedged."""
    never: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

    entered = asyncio.Event()

    async def long_turn(_loop: NudgeLoop) -> bool:
        entered.set()
        return await never

    svc._on_fire = long_turn
    await svc.start()
    loop = await svc.add(slot_key="slack:C123:1700000000.000100", message="go", idle_secs=300)
    _move_to(clock, loop.next_due_ts + 1)
    tick = await _tick_now(svc, loop)
    await asyncio.wait_for(entered.wait(), WAIT_SECS)
    assert loop.id in svc._firing

    # Twelve minutes into the turn: four intervals past the deadline, well inside
    # any turn ceiling.
    _move_to(clock, svc._fire_started[loop.id] + 720)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
        svc._reconcile_once()

    assert _overdue_warnings(caplog) == []
    assert svc._timers[loop.id] is tick
    never.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(tick, WAIT_SECS)
    assert loop.id not in svc._fire_started


def test_wedged_fire_threshold_is_the_highest_turn_ceiling():
    """No turn the gateway allows can stay in the fire window this long."""
    assert _timers._WEDGED_FIRE_SECS == CHAT_TURN_TIMEOUT_MAX


@pytest.mark.asyncio
async def test_finding2_retry_in_fire_window_is_not_named_again(svc, clock, caplog):
    """A refused loop's next retry runs in the fire window: it must not restart the episode."""

    async def refuse(_loop: NudgeLoop) -> bool:
        return False

    svc._on_fire = refuse
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=IDLE)
    _move_to(clock, loop.next_due_ts + 1)
    await await_tick(svc, loop)

    _move_to(clock, loop.next_due_ts + 3 * IDLE)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()
    armed = svc._timers[loop.id]
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        # A retry is in its fire window during one pass, then refused again.
        svc._firing.add(loop.id)
        svc._fire_started[loop.id] = clock.time()
        try:
            svc._reconcile_once()
        finally:
            svc._firing.discard(loop.id)
            svc._fire_started.pop(loop.id, None)
        svc._reconcile_once()

    # No re-arm and no warning for the retry: the episode stays open across it.
    assert loop.id in svc._overdue_warned
    assert _overdue_warnings(caplog) == []
    assert svc._timers[loop.id] is armed


@pytest.mark.asyncio
async def test_finding3_long_user_turn_is_not_named(svc, clock, caplog):
    """A user turn cancels the timer and keeps the deadline: overdue, healthy, unnamed."""
    await svc.start()
    loop = await svc.add(slot_key="chat-1-123", message="go", idle_secs=60)
    svc.notify_user_input(loop.slot_key, human=True)
    assert loop.id not in svc._timers

    # Three minutes into the user's turn: three intervals past the deadline.
    _move_to(clock, loop.next_due_ts + 180)
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()

    assert _overdue_warnings(caplog) == []
    assert loop.id not in svc._timers
    assert loop.id not in svc._overdue_warned

    # The turn ends: the hook arms the overdue beat, a live timer with no refusals.
    svc.notify_turn_complete(loop.slot_key)
    beat = svc._timers[loop.id]
    assert not beat.done()
    with caplog.at_level(logging.WARNING, logger="kiro_crew.autonudge"):
        svc._reconcile_once()

    assert _overdue_warnings(caplog) == []
    assert svc._timers[loop.id] is beat
