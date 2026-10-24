"""AutoNudgeService shutdown-drain tests: executor, grace window and dispatch.

One of the sibling topic modules split from the original
``test_autonudge_shutdown_drain.py``. These cover the drain mechanics: an
in-flight executor write finishes (or is cut at the grace boundary), the
durability window spans every settle, and a cut channel turn still drains the
timer/callback bookkeeping it admitted.

``_enable`` is autouse (imported from ``autonudge_helpers``); ``svc_fixture``
registers the ``svc`` fixture.
"""

from __future__ import annotations

import asyncio
import gc
import threading
import time
import weakref
from unittest.mock import AsyncMock

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401
from autonudge_shutdown_drain_helpers import _LOST_RUN_SECS, _wait_until

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    AutoNudgeService,
    NudgeAdmissionRefused,
    NudgeLoop,
)
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDispatchResult,
)
from kiro_crew.testing.wait import async_wait_until


@pytest.mark.asyncio
async def test_rearmed_timer_releases_completed_owner_tasks(tmp_path, monkeypatch):
    svc = AutoNudgeService(base_dir=tmp_path)
    loop = NudgeLoop(id="loop-rearm", slot_key="chat-1-1", message="tick", idle_secs=15)
    task_refs: list[weakref.ReferenceType[asyncio.Task]] = []
    ticks = 500
    finished = asyncio.Event()

    async def _rearm(current: NudgeLoop) -> None:
        task = asyncio.current_task()
        assert task is not None
        task_refs.append(weakref.ref(task))
        if len(task_refs) < ticks:
            svc._arm_timer(current, delay=0.0)
        else:
            finished.set()

    monkeypatch.setattr(svc, "_run_timer_callback", _rearm)
    svc._arm_timer(loop, delay=0.0)
    await asyncio.wait_for(finished.wait(), timeout=_LOST_RUN_SECS)
    final_timer = svc._timers[loop.id]
    await asyncio.wait_for(asyncio.shield(final_timer), timeout=_LOST_RUN_SECS)
    del final_timer
    gc.collect()

    assert len(task_refs) == ticks
    assert sum(ref() is not None for ref in task_refs) <= 1


@pytest.mark.asyncio
async def test_shutdown_waits_for_blocking_executor_persistence(tmp_path, monkeypatch):
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()
    write_finished = threading.Event()

    def blocking_write(payload):
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write(payload)
        write_finished.set()

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    add_task = asyncio.create_task(svc.add(slot_key="slot-drain", message="later", idle_secs=60))
    await async_wait_until(write_started.is_set)
    shutdown_task = asyncio.create_task(svc.shutdown())
    await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")

    assert not shutdown_task.done()
    assert not write_finished.is_set()
    assert svc._inflight_adds
    assert svc._inflight_persistence

    release_write.set()
    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)
    assert write_finished.is_set()
    assert svc._inflight_adds == set()
    assert svc._inflight_persistence == set()

    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("slot-drain").id == loop.id
    await restored.shutdown()


@pytest.mark.asyncio
async def test_cancelled_shutdown_still_drains_executor_write(tmp_path, monkeypatch):
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()

    cancel_observed = asyncio.Event()

    class ObservedDrain(_an._DurabilityDrain):
        def note_cancelled(self) -> None:
            super().note_cancelled()
            cancel_observed.set()

    monkeypatch.setattr(_an, "_DurabilityDrain", ObservedDrain)

    def blocking_write(payload):
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    add_task = asyncio.create_task(svc.add(slot_key="slot-cancel", message="later", idle_secs=60))
    await async_wait_until(write_started.is_set)
    shutdown_task = asyncio.create_task(svc.shutdown())
    await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
    shutdown_task.cancel()
    await asyncio.wait_for(cancel_observed.wait(), timeout=_LOST_RUN_SECS)
    assert not shutdown_task.done()

    release_write.set()
    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)

    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("slot-cancel").id == loop.id
    await restored.shutdown()


def _wedge_write(svc, monkeypatch):
    """Replace the executor write with one that blocks until the test releases it.

    Returns (started, release, finished) threading events. `finished` is set only
    when the REAL write has landed, so a test can tell "abandoned while wedged"
    from "settled within the window" without timing.
    """
    real_write = svc._write_state
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocking_write(payload):
        started.set()
        assert release.wait(timeout=10)
        real_write(payload)
        finished.set()

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    return started, release, finished


@pytest.mark.asyncio
async def test_cancelled_shutdown_stops_waiting_on_a_wedged_write_after_the_grace(
    tmp_path, monkeypatch
):
    """The gateway's bounded wait delivers ONE cancellation. A drain that absorbed
    it and kept waiting on a write that never finishes turned that bound into an
    unbounded one; this pins that the drain leaves once its window closes, that
    the write is neither cancelled nor lost, and that the closed service remains
    published for the gateway's later dashboard teardown."""
    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", 0.05)
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    started, release, finished = _wedge_write(svc, monkeypatch)
    add_task = asyncio.create_task(svc.add(slot_key="slot-wedged", message="later", idle_secs=60))
    await async_wait_until(started.is_set)
    wedged = set(svc._inflight_persistence)
    assert wedged

    shutdown_task = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    shutdown_task.cancel()
    # Bounded by the window, not by the write: nothing has released it.
    done, _ = await asyncio.wait({shutdown_task}, timeout=2)
    assert shutdown_task in done, "shutdown kept absorbing cancellation while the write was wedged"
    assert not finished.is_set()
    with pytest.raises(asyncio.CancelledError):
        shutdown_task.result()
    # The write itself was left alone -- still pending, still registered, and
    # the service is closed to new mutations, so nothing can race it.
    assert all(not future.done() and not future.cancelled() for future in wedged)
    assert svc._inflight_persistence == wedged
    assert svc._accepting_mutations is False
    assert _an.get_instance() is svc

    # Releasing the thread lands the durable write exactly as if the drain had
    # waited: the owner completes and a fresh service reads the slot back.
    release.set()
    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    await async_wait_until(finished.is_set)
    for future in wedged:
        await asyncio.wait_for(future, timeout=_LOST_RUN_SECS)
    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("slot-wedged").id == loop.id
    await restored.shutdown()


@pytest.mark.asyncio
async def test_claim_sets_survive_until_the_drain_settles(tmp_path, monkeypatch):
    """An unsettled shutdown leaves work it stopped waiting on still running, so it
    must not clear the runtime claim sets under it; a settled one releases them."""
    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", 0.05)
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    started, release, finished = _wedge_write(svc, monkeypatch)
    add_task = asyncio.create_task(svc.add(slot_key="slot-claims", message="later", idle_secs=60))
    await async_wait_until(started.is_set)
    svc._accepted_monitor_turns["monitor-x"] = "fp-x"
    svc._reconcile_candidates.add("loop-x")
    svc._maintenance_quiescing.add("loop-x")

    shutdown_task = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    shutdown_task.cancel()
    done, _ = await asyncio.wait({shutdown_task}, timeout=2)
    assert shutdown_task in done
    with pytest.raises(asyncio.CancelledError):
        shutdown_task.result()
    assert not finished.is_set()
    assert svc._accepted_monitor_turns == {"monitor-x": "fp-x"}
    assert svc._reconcile_candidates == {"loop-x"}
    assert svc._maintenance_quiescing == {"loop-x"}
    assert _an.get_instance() is svc

    release.set()
    await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    await async_wait_until(finished.is_set)

    settled = AutoNudgeService(base_dir=tmp_path)
    await settled.start()
    settled._accepted_monitor_turns["monitor-y"] = "fp-y"
    settled._reconcile_candidates.add("loop-y")
    settled._maintenance_quiescing.add("loop-y")
    await settled.shutdown()
    assert settled._accepted_monitor_turns == {}
    assert settled._reconcile_candidates == set()
    assert settled._maintenance_quiescing == set()


@pytest.mark.asyncio
async def test_uncancelled_shutdown_waits_past_the_grace_for_a_slow_write(tmp_path, monkeypatch):
    """The window opens on CANCELLATION, never on its own: an uncancelled shutdown
    still waits for a slow write however long it takes, because nobody has asked
    it to stop and abandoning a write nobody rushed would weaken durability for no
    caller. A non-positive window makes any other arming visible on its first pass,
    so no elapsed-time wait is part of this proof."""
    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", -1.0)
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    started, release, finished = _wedge_write(svc, monkeypatch)
    drains: list[_an._DurabilityDrain] = []
    drain_created = asyncio.Event()
    held_write_settling = asyncio.Event()
    real_drain = _an._DurabilityDrain

    class ObservedDrain(real_drain):
        def __init__(self) -> None:
            super().__init__()
            drains.append(self)
            drain_created.set()

        async def settle(self, *tasks):
            if any(task in svc._inflight_persistence for task in tasks):
                held_write_settling.set()
            return await super().settle(*tasks)

    monkeypatch.setattr(_an, "_DurabilityDrain", ObservedDrain)
    add_task = asyncio.create_task(svc.add(slot_key="slot-slow", message="later", idle_secs=60))
    await async_wait_until(started.is_set)

    shutdown_task = asyncio.create_task(svc.shutdown())
    await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
    await asyncio.wait_for(drain_created.wait(), timeout=_LOST_RUN_SECS)
    assert len(drains) == 1
    drain = drains[0]
    receipt_waiter = asyncio.create_task(held_write_settling.wait())
    try:
        assert drain.interrupted is False
        assert drain._deadline is None
        done, _ = await asyncio.wait(
            {receipt_waiter, shutdown_task},
            timeout=_LOST_RUN_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert receipt_waiter in done, "shutdown never offered the held write to the drain"
        await asyncio.sleep(0)
        assert drain.interrupted is False
        assert drain._deadline is None
        assert (
            not shutdown_task.done()
        ), "an uncancelled shutdown gave up on a write nobody cancelled"
        assert not finished.is_set()
    finally:
        if not receipt_waiter.done():
            receipt_waiter.cancel()
            await asyncio.gather(receipt_waiter, return_exceptions=True)
        release.set()

    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)
    assert finished.is_set()
    assert svc._inflight_persistence == set()
    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("slot-slow").id == loop.id
    await restored.shutdown()


@pytest.mark.asyncio
async def test_repeated_cancellation_within_the_grace_still_drains_to_durability(
    tmp_path, monkeypatch
):
    """Cancellations inside the window are remembered, not compounded: a write
    that finishes before the window closes is awaited to durability whether the
    waiter was cancelled once or several times, and the caller sees exactly one
    CancelledError afterwards."""
    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", 5.0)
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    started, release, finished = _wedge_write(svc, monkeypatch)
    first_cancel_observed = asyncio.Event()
    second_cancel_observed = asyncio.Event()

    class ObservedDrain(_an._DurabilityDrain):
        def __init__(self) -> None:
            super().__init__()
            self._cancel_count = 0

        def note_cancelled(self) -> None:
            super().note_cancelled()
            self._cancel_count += 1
            (first_cancel_observed if self._cancel_count == 1 else second_cancel_observed).set()

    monkeypatch.setattr(_an, "_DurabilityDrain", ObservedDrain)
    add_task = asyncio.create_task(svc.add(slot_key="slot-twice", message="later", idle_secs=60))
    await async_wait_until(started.is_set)

    shutdown_task = asyncio.create_task(svc.shutdown())
    await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
    shutdown_task.cancel()
    await asyncio.wait_for(first_cancel_observed.wait(), timeout=_LOST_RUN_SECS)
    shutdown_task.cancel()
    await asyncio.wait_for(second_cancel_observed.wait(), timeout=_LOST_RUN_SECS)
    assert not shutdown_task.done()
    assert not finished.is_set()

    release.set()
    loop = await asyncio.wait_for(add_task, timeout=_LOST_RUN_SECS)
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown_task, timeout=_LOST_RUN_SECS)
    assert finished.is_set()
    assert svc._inflight_persistence == set()
    restored = AutoNudgeService(base_dir=tmp_path)
    await restored.start()
    assert restored.get_by_slot("slot-twice").id == loop.id
    await restored.shutdown()


def test_drain_grace_fits_inside_the_signal_margin():
    """The window is spent AFTER the graceful budget has already been cancelled, so
    it comes out of the SIGTERM-to-SIGKILL margin along with the rest of teardown.
    A window as long as the margin would hand the exit back to the supervisor."""
    from kiro_crew import gateway_shutdown_budget as budget

    assert _an._DRAIN_CANCEL_GRACE_SECS == budget.PERSISTENCE_DRAIN_GRACE_SECS
    assert 0 < budget.PERSISTENCE_DRAIN_GRACE_SECS < budget.SIGNAL_MARGIN_SECS / 2


@pytest.mark.asyncio
async def test_durability_drain_window_spans_every_settle_on_one_instance(monkeypatch):
    """`shutdown()` drains its control tasks and then its persistence registry
    through ONE drain. The gateway's single cancellation may land during the
    first; the second must still be bounded by it, or a wedged write reached only
    in the second phase would wait forever. Conversely a fresh instance that was
    never cancelled waits past the window: the window opens on cancellation only."""
    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", 0.05)
    loop = asyncio.get_running_loop()
    drain = _an._DurabilityDrain()

    first = loop.create_future()
    waiter = asyncio.create_task(drain.settle(first))
    await asyncio.sleep(0)
    waiter.cancel()
    await asyncio.sleep(0)
    assert not waiter.done()
    first.set_result(None)
    assert await asyncio.wait_for(waiter, timeout=_LOST_RUN_SECS) is True
    assert drain.interrupted is True

    second = loop.create_future()
    second_settlement = asyncio.create_task(drain.settle(second))
    assert await asyncio.wait_for(second_settlement, timeout=_LOST_RUN_SECS) is False
    assert not second.done() and not second.cancelled()

    monkeypatch.setattr(_an, "_DRAIN_CANCEL_GRACE_SECS", -1.0)
    fresh = _an._DurabilityDrain()
    waiter_registered = asyncio.Event()

    class RegisteredFuture(asyncio.Future):
        def add_done_callback(self, fn, *, context=None):
            waiter_registered.set()
            super().add_done_callback(fn, context=context)

    never = RegisteredFuture()
    settling = asyncio.create_task(fresh.settle(never))
    resolved_at_settlement: list[bool] = []
    settling.add_done_callback(lambda _task: resolved_at_settlement.append(never.done()))
    await asyncio.wait_for(waiter_registered.wait(), timeout=_LOST_RUN_SECS)
    await asyncio.sleep(0)
    assert fresh._deadline is None
    assert fresh.interrupted is False
    assert not settling.done()
    never.set_result(None)
    assert await asyncio.wait_for(settling, timeout=_LOST_RUN_SECS) is True
    assert resolved_at_settlement == [True]


@pytest.mark.asyncio
async def test_shutdown_cuts_an_inflight_channel_turn_but_drains_admitted_bookkeeping(tmp_path):
    """A channel loop runs its turn inline in ``_on_fire``, and that turn only ends
    when the gateway closes sessions -- which happens AFTER AutoNudge's drain. A
    drain that waited on the delivering callback therefore always waited out the
    gateway's whole graceful bound. Shutdown cuts the delivery instead, and still
    drains the bookkeeping a delivery that already returned is writing."""
    delivering = asyncio.Event()
    delivery_cancelled = asyncio.Event()
    settle_entered = asyncio.Event()
    settle_release = asyncio.Event()

    async def on_fire(loop):
        if loop.slot_key == "slack:C1:T1":
            delivering.set()
            try:
                await asyncio.wait_for(
                    asyncio.Event().wait(), timeout=_LOST_RUN_SECS
                )  # a turn nothing but session close ends
            except asyncio.CancelledError:
                delivery_cancelled.set()
                raise
        else:
            settle_entered.set()
            await asyncio.wait_for(settle_release.wait(), timeout=_LOST_RUN_SECS)
        return True

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await svc.start()
    turning = await svc.add(slot_key="slack:C1:T1", message="turn", idle_secs=60)
    settling = await svc.add(slot_key="slack:C2:T2", message="settle", idle_secs=60)
    svc._cancel_timer(turning.id)
    svc._cancel_timer(settling.id)
    svc._arm_timer(turning, delay=0.0)
    await asyncio.wait_for(delivering.wait(), timeout=_LOST_RUN_SECS)
    svc._arm_timer(settling, delay=0.0)
    await asyncio.wait_for(settle_entered.wait(), timeout=_LOST_RUN_SECS)
    # Held so the second loop's settlement parks BEFORE it submits its write: the
    # one place where cancelling the callback would lose the delivered cycle.
    await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        settle_release.set()
        settling_timer = svc._timers[settling.id]
        for _ in range(100):
            if settling_timer in _an._timers._drain_registered_owners(svc):
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("the delivered cycle's settlement was never registered")

        shutdown = asyncio.create_task(svc.shutdown())
        await asyncio.wait_for(delivery_cancelled.wait(), timeout=_LOST_RUN_SECS)
        await _wait_until(
            lambda: not svc._accepting_mutations,
            "shutdown admission closure while bookkeeping was registered",
        )
        assert not shutdown.done(), "shutdown did not wait for admitted bookkeeping"
        assert not settling_timer.done()
    finally:
        svc._lock.release()

    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    assert settling_timer.done() and not settling_timer.cancelled()
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    assert restored.get_by_slot("slack:C2:T2").cycle_count == 1
    assert restored.get_by_slot("slack:C1:T1").cycle_count == 0


@pytest.mark.asyncio
async def test_shutdown_drains_a_callback_inside_its_dispatch_record(tmp_path, monkeypatch):
    """A structured monitor's post-dispatch record is bookkeeping its delivery earned:
    a shutdown that lands while the callback waits for the service lock inside it
    drains the callback instead of cancelling it, so DISPATCHED reaches the store."""
    svc = AutoNudgeService(base_dir=tmp_path, on_monitor_tick=AsyncMock())
    await svc.start()
    monitor = await svc.add_monitor(
        slot_key="chat-1-7",
        kind="github_pull_request",
        target="owner/repo#123",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(),
    )
    assert await svc.mark_monitor_action_in_flight(monitor.id, "fp-a", now=time.time())
    svc._cancel_timer(monitor.id)
    entered = asyncio.Event()

    async def _record_dispatch(loop):
        entered.set()
        await svc.record_monitor_dispatched(loop.id, "fp-a", now=time.time())

    monkeypatch.setattr(svc, "_run_timer_callback", _record_dispatch)
    await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
    try:
        svc._arm_timer(monitor, delay=0.0)
        timer = svc._timers[monitor.id]
        await asyncio.wait_for(entered.wait(), timeout=_LOST_RUN_SECS)
        await _wait_until(
            lambda: timer in _an._timers._drain_registered_owners(svc),
            "the dispatch record registration",
        )
        assert timer in _an._timers._drain_registered_owners(svc)
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        assert not shutdown.done()
        assert not timer.done()
    finally:
        svc._lock.release()

    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    assert timer.done() and not timer.cancelled()
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    restored_monitor = restored.get_by_id(monitor.id)
    assert restored_monitor is not None and restored_monitor.monitor is not None
    assert restored_monitor.monitor.wake_delivery is MonitorDispatchResult.DISPATCHED


@pytest.mark.asyncio
async def test_shutdown_cancels_dormant_timer_before_callback(tmp_path):
    callback = AsyncMock(return_value=True)
    svc = AutoNudgeService(base_dir=tmp_path, on_fire=callback)
    await svc.start()
    loop = await svc.add(slot_key="slot-dormant", message="later", idle_secs=60)
    timer = svc._timers[loop.id]

    await svc.shutdown()

    assert timer.done()
    callback.assert_not_awaited()
    assert loop.id not in svc._timers


@pytest.mark.asyncio
async def test_cancelled_shutdown_drains_started_timer_persistence(tmp_path, monkeypatch):
    svc = AutoNudgeService(base_dir=tmp_path, on_fire=AsyncMock(return_value=True))
    await svc.start()
    loop = await svc.add(slot_key="slot-callback-write", message="go", idle_secs=60)
    svc._cancel_timer(loop.id)
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()
    writes = 0

    def blocking_write(payload):
        nonlocal writes
        writes += 1
        if writes == 1:
            real_write(payload)
            return
        write_started.set()
        assert release_write.wait(timeout=5)
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", blocking_write)
    shutdown = None
    try:
        svc._arm_timer(loop, delay=0.0)
        await async_wait_until(write_started.is_set)

        shutdown = asyncio.create_task(svc.shutdown())
        await asyncio.sleep(0)
        shutdown.cancel()
        await asyncio.sleep(0)
        assert not shutdown.done()
    finally:
        release_write.set()

    assert shutdown is not None
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    assert restored.get_by_slot("slot-callback-write").cycle_count == 1


@pytest.mark.asyncio
async def test_shutdown_closure_prevents_started_channel_callback_rearm(tmp_path):
    started = asyncio.Event()
    release = asyncio.Event()

    async def on_fire(_loop):
        started.set()
        await asyncio.wait_for(release.wait(), timeout=_LOST_RUN_SECS)
        return True

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await svc.start()
    loop = await svc.add(slot_key="slack:C1:T1", message="go", idle_secs=60)
    svc._cancel_timer(loop.id)
    svc._arm_timer(loop, delay=0.0)
    timer = svc._timers[loop.id]
    await asyncio.wait_for(started.wait(), timeout=_LOST_RUN_SECS)

    shutdown = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    assert timer.done()
    assert loop.id not in svc._timers
    assert svc._inflight_adds == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "remove"])
async def test_a_callbacks_committed_mutation_survives_shutdown_cutting_its_delivery(
    tmp_path,
    operation,
):
    """A ``remove`` of the callback's own loop takes its task out of the timer
    table, so the shutdown reaches it through the running-callback registry."""
    started = asyncio.Event()
    release_mutation = asyncio.Event()
    mutation_finished = asyncio.Event()
    finish_callback = asyncio.Event()
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    loop = await svc.add(slot_key=f"slot-{operation}", message="before", idle_secs=60)
    svc._cancel_timer(loop.id)

    async def on_fire(_loop):
        started.set()
        await asyncio.wait_for(release_mutation.wait(), timeout=_LOST_RUN_SECS)
        if operation == "update":
            await svc.update(loop.id, message="after")
        else:
            await svc.remove(loop.id)
        mutation_finished.set()
        await asyncio.wait_for(finish_callback.wait(), timeout=_LOST_RUN_SECS)
        return False

    svc._on_fire = on_fire
    svc._arm_timer(loop, delay=0.0)
    timer = svc._timers[loop.id]
    await asyncio.wait_for(started.wait(), timeout=_LOST_RUN_SECS)
    release_mutation.set()
    await asyncio.wait_for(mutation_finished.wait(), timeout=_LOST_RUN_SECS)
    await asyncio.wait_for(svc.shutdown(), timeout=_LOST_RUN_SECS)
    assert timer.cancelled()
    assert not finish_callback.is_set()

    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    persisted = restored.get_by_slot(f"slot-{operation}")
    if operation == "update":
        assert persisted is not None and persisted.message == "after"
    else:
        assert persisted is None


@pytest.mark.asyncio
async def test_started_timer_admission_does_not_leak_to_detached_child(tmp_path):
    started = asyncio.Event()
    release_child = asyncio.Event()
    child_finished = asyncio.Event()
    refusals: list[str] = []
    svc = AutoNudgeService(base_dir=tmp_path)
    await svc.start()
    loop = await svc.add(slot_key="slot-child", message="before", idle_secs=60)
    svc._cancel_timer(loop.id)

    async def on_fire(_loop):
        async def detached_mutation():
            await asyncio.wait_for(release_child.wait(), timeout=_LOST_RUN_SECS)
            try:
                await svc.update(loop.id, message="must-not-land")
            except NudgeAdmissionRefused as exc:
                refusals.append(str(exc))
            finally:
                child_finished.set()

        asyncio.create_task(detached_mutation())
        started.set()
        await asyncio.wait_for(child_finished.wait(), timeout=_LOST_RUN_SECS)
        return False

    svc._on_fire = on_fire
    svc._arm_timer(loop, delay=0.0)
    await asyncio.wait_for(started.wait(), timeout=_LOST_RUN_SECS)
    shutdown = asyncio.create_task(svc.shutdown())
    await asyncio.sleep(0)
    release_child.set()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    await asyncio.wait_for(child_finished.wait(), timeout=_LOST_RUN_SECS)

    assert refusals == [
        "The gateway is restarting, so this was not applied. Try again once it is back."
    ]
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    assert restored.get_by_slot("slot-child").message == "before"
