"""A tick that applies a terminal bound to its own loop finishes the stop it starts.

Every terminal branch of ``_timer`` deactivates through ``update``, and ``update``
mutates in a shielded task of its own. That task cancels the loop's registered
timer before re-arming, and from inside it the running tick is not "the current
task". Unless the tick is recognised there, it is cancelled while it awaits its own
update: the ``expired`` event that tells the user a bound stopped the loop never
goes out, and anything awaiting the tick sees ``CancelledError`` (a CI flake
whenever a slow runner spends a two-second budget during test setup).

Each case below drives the real path: a timer task created by ``_arm_timer`` with
a zero delay, so no clock or sleep is patched.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import SESSION_START_FAILURE_REASON, AutoNudgeService
from kiro_crew.autonudge_service.model import (
    _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
    CONSECUTIVE_FAILURE_REASON,
)
from kiro_crew.autonudge_service.timers import _ACTING_TIMER
from kiro_crew.testing.wait import await_until, default_timeout

SLOT = "chat-1-4242"


@pytest.fixture(autouse=True)
def _enable(_floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_AUTONUDGE", "1")


@pytest.fixture(autouse=True)
def _unpublish():
    yield
    svc = _an.get_instance()
    if svc is not None:
        try:
            for task in list(getattr(svc, "_inflight_adds", ())):
                task.cancel()
            svc.stop()
        finally:
            _an._INSTANCE = None


async def _finished(task: asyncio.Task, what: str) -> None:
    """Wait for *task* to end, however it ends, under the lost-run bound.

    A bare ``await task`` re-raises a cancellation into the test and waits for as long
    as the task does: a stalled executor write behind it would hold the test to the
    suite timeout and kill the worker. This returns once the task is done (result,
    error or cancelled) and never cancels it, so the test reads the outcome off the
    task and a hang fails by name.
    """
    await await_until(asyncio.gather(task, return_exceptions=True), what=what)


async def _step(call, what: str):
    """Run one service call that writes to disk, under the same lost-run bound.

    The call runs as a task the test owns, so an expired bound fails by name and does
    not cancel a write part-way through its drain.
    """
    return await await_until(asyncio.ensure_future(call), what=what)


def _spend_cycle_cap(loop) -> None:
    loop.max_cycles = 1
    loop.cycle_count = 1


def _spend_runtime_budget(loop) -> None:
    loop.max_runtime_secs = 1
    loop.created_ts = time.time() - 100


def _stand_down_on_start_failures(loop) -> None:
    loop.consecutive_start_failures = _START_FAILURE_STANDDOWN_AFTER


def _stand_down_on_failed_cycles(loop) -> None:
    loop.consecutive_failed_cycles = _CONSECUTIVE_FAILURE_STANDDOWN_AFTER


@pytest.mark.parametrize(
    ("spend", "reason"),
    [
        (_spend_cycle_cap, "cycle_cap"),
        (_spend_runtime_budget, "runtime_budget"),
        (_stand_down_on_start_failures, SESSION_START_FAILURE_REASON),
        (_stand_down_on_failed_cycles, CONSECUTIVE_FAILURE_REASON),
    ],
)
@pytest.mark.asyncio
async def test_a_terminal_bound_stops_the_loop_and_announces_it(tmp_path, spend, reason):
    svc = AutoNudgeService(base_dir=tmp_path)
    events: list[tuple[str, str | None]] = []
    svc.subscribe(lambda kind, loop: events.append((kind, loop.id if loop else None)))
    await _step(svc.start(), "the service start")
    loop = await _step(svc.add(slot_key=SLOT, message="go", idle_secs=600), "adding the loop")
    svc._cancel_timer(loop.id)
    live = svc._loops[loop.id]
    spend(live)

    svc._arm_timer(live, delay=0.0)
    tick = svc._timers[loop.id]
    await _finished(tick, "the tick's terminal stop")

    assert not tick.cancelled(), "the tick must return, not be cancelled"
    tick.result()  # and not have raised
    assert (live.active, live.stopped_reason) == (False, reason)
    assert ("expired", loop.id) in events, "the stop is announced to the user"
    assert loop.id not in svc._timers, "a stopped loop keeps no timer"


@pytest.mark.asyncio
async def test_an_update_from_another_task_still_cancels_the_tick(tmp_path):
    """Healthy side: the binding exempts a tick only from ITS OWN updates. A pause
    sent by anyone else still cancels the armed timer as before."""
    svc = AutoNudgeService(base_dir=tmp_path)
    await _step(svc.start(), "the service start")
    loop = await _step(svc.add(slot_key=SLOT, message="go", idle_secs=600), "adding the loop")
    tick = svc._timers[loop.id]

    await _step(svc.update(loop.id, active=False), "the pause's update")

    # Asserted before awaiting: an exempted tick would sleep out its 600 s interval.
    assert tick.done() or tick.cancelling() == 1, "the pause cancelled the armed tick"
    # The tick absorbs a cancel during its sleep and returns.
    await _finished(tick, "the paused tick's exit")
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_a_remove_from_a_turn_the_tick_started_still_cancels_it(tmp_path):
    """The binding covers the tick's own updates only. A dashboard turn that ``on_fire``
    starts as its own task (and that the agent ends with a remove) is another task:
    its remove cancels the tick exactly as any other remove does."""
    removed = asyncio.Event()
    turns: list[asyncio.Task] = []

    async def _turn(loop_id: str) -> None:
        await svc.remove(loop_id)
        removed.set()

    async def _on_fire(loop) -> bool:
        turns.append(asyncio.ensure_future(_turn(loop.id)))
        # The tick is still running when the remove lands. The remove cancels it here, so
        # this wait is cancelled normally; the bound is for a remove that never lands.
        await asyncio.wait_for(removed.wait(), default_timeout())
        return True

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=_on_fire)
    await _step(svc.start(), "the service start")
    loop = await _step(svc.add(slot_key=SLOT, message="go", idle_secs=600), "adding the loop")
    svc._cancel_timer(loop.id)

    svc._arm_timer(svc._loops[loop.id], delay=0.0)
    tick = svc._timers[loop.id]
    await _finished(tick, "the tick the remove cancels")
    # Awaited as the task itself: the turn's remove drains its write and must not be
    # cancelled by an expired bound.
    await await_until(turns[0], what="the turn's remove")

    assert tick.cancelled(), "the remove cancelled the tick"
    assert loop.id not in svc._loops
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_awaiting_the_timer_directly_leaves_the_callers_context_alone(tmp_path):
    """A caller that awaits ``_timer`` itself (as many tests do) runs the tick in its
    own task. The terminal update binds that task for the update only, so the caller's
    context reads the default again once the tick returns."""
    svc = AutoNudgeService(base_dir=tmp_path)
    await _step(svc.start(), "the service start")
    loop = await _step(svc.add(slot_key=SLOT, message="go", idle_secs=600), "adding the loop")
    svc._cancel_timer(loop.id)
    live = svc._loops[loop.id]
    _spend_cycle_cap(live)

    # Awaited in THIS task: the case is about the caller's own context, which a helper
    # task would copy and so hide a leak. The bound is a timeout scope, not a task.
    async with asyncio.timeout(default_timeout()):
        await svc._timer(live, delay=0)

    assert (live.active, live.stopped_reason) == (False, "cycle_cap")
    assert _ACTING_TIMER.get() is None
