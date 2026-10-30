"""AutoNudgeService shutdown-drain tests: ledger extension and patrol displacement.

One of the sibling topic modules split from the original
``test_autonudge_shutdown_drain.py``. These cover shutdown landing in a ledger
extension (whole or dropped) and a displaced default-patrol wake keeping the row
removed through closure.

``_enable`` is autouse (imported from ``autonudge_helpers``); ``svc_fixture``
registers the ``svc`` fixture.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from unittest.mock import AsyncMock

import pytest
from autonudge_helpers import _enable  # noqa: F401
from autonudge_helpers import svc_fixture  # noqa: F401
from autonudge_shutdown_drain_helpers import _LOST_RUN_SECS, _wait_until

from kiro_crew import autonudge as _an
from kiro_crew.autonudge import (
    AutoNudgeService,
    NudgeAdmissionReason,
    NudgeAdmissionRefused,
)
from kiro_crew.monitoring.models import (
    MonitorActionCompletion,
    MonitorActionDisposition,
)
from kiro_crew.testing.wait import async_wait_until


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["extension_write", "ledger_read"])
async def test_shutdown_during_a_ledger_extension_lands_it_whole_or_drops_it(
    tmp_path, monkeypatch, cut
):
    """A spent bound raised for an open work ledger is one admitted write or none.

    The extension runs inside the timer tick and first detaches the tick from the
    timer table, so shutdown reaches it through the running-callback registry and
    cancels it like any running callback. The raise itself goes through ``update``
    under the tick's lease: cut while that write waits on the service lock, the
    write is drained and lands; cut while the tick is still reading the ledger, no
    write starts. Either way the store and the loop in memory agree, and no timer
    is armed once admission has closed.
    """
    from kiro_crew.autonudge_service import firing

    class _ObservedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.contended = asyncio.Event()

        async def acquire(self) -> bool:
            if self.locked():
                self.contended.set()
            return await super().acquire()

    reading = threading.Event()
    release_read = threading.Event()

    def _ledger_has_open_items(_key: str) -> bool:
        reading.set()
        if cut == "ledger_read":
            assert release_read.wait(timeout=_LOST_RUN_SECS)
        return True

    on_fire = AsyncMock(return_value=True)
    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    lock = _ObservedLock()
    svc._lock = lock
    await svc.start()
    loop = await svc.add(slot_key="chat-7-777", message="patrol", idle_secs=600, max_cycles=200)
    svc._cancel_timer(loop.id)
    loop.cycle_count = 200
    await svc._persist_locked()
    pending_setup = [task for task in svc._inflight_adds if not task.done()]
    if pending_setup:
        await asyncio.wait_for(asyncio.gather(*pending_setup), timeout=_LOST_RUN_SECS)
    lock.contended.clear()
    monkeypatch.setattr(firing, "_ledger_has_open_items", _ledger_has_open_items)
    monkeypatch.setattr(firing, "_ledger_backstop_secs", lambda: 7 * 24 * 3600)
    monkeypatch.setattr(AutoNudgeService, "_observes_work_ledger", lambda self, lp: True)
    real_timer = svc._timer
    armed_after_closure: list[str] = []

    def _recording_timer(lp, delay=None):
        if not svc._accepting_mutations:
            armed_after_closure.append(lp.id)
        return real_timer(lp, delay)

    monkeypatch.setattr(svc, "_timer", _recording_timer)

    held = False
    try:
        if cut == "extension_write":
            await asyncio.wait_for(lock.acquire(), timeout=_LOST_RUN_SECS)
            held = True
        svc._arm_timer(loop, delay=0.0)
        tick = svc._timers[loop.id]
        if cut == "extension_write":
            await asyncio.wait_for(lock.contended.wait(), timeout=_LOST_RUN_SECS)
        else:
            await async_wait_until(reading.is_set)
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        if cut == "extension_write":
            assert not shutdown.done(), "shutdown must drain the admitted extension write"
            lock.release()
            held = False
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        if held:
            lock.release()
        release_read.set()

    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    stored = restored.get_by_slot("chat-7-777")
    assert stored is not None
    expected = 250 if cut == "extension_write" else 200
    assert stored.max_cycles == expected, "the extension landed whole or not at all"
    assert loop.max_cycles == expected, "and the loop in memory holds what the store holds"
    assert (stored.cycle_count, stored.active, stored.next_due_ts) == (
        loop.cycle_count,
        loop.active,
        loop.next_due_ts,
    )
    assert loop.active is True and loop.stopped_reason == ""
    assert tick.cancelled(), "the detached tick is still cancelled as a running callback"
    assert armed_after_closure == [], "nothing may be armed after admission closed"
    on_fire.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["delivery", "delivery_record", "none"])
async def test_shutdown_during_a_displaced_default_patrol_wake_keeps_the_row_removed(
    tmp_path, monkeypatch, cut
):
    """A default patrol displaced mid-wake keeps its tick running, outside the table.

    The agent's own create-only arm, issued while the default patrol's channel turn
    runs, detaches the firing tick from the timer table instead of cancelling it, so
    that delivery finishes. The tick is still a running callback: shutdown cancels it
    while it delivers and drains it once it sits inside its own delivery record. In
    every case the removed row is not written back (the drained write snapshots the
    live table, which holds only the replacement), it is never announced as fired, no
    timer is armed for it, and nothing at all is armed once admission has closed.
    """
    delivering = asyncio.Event()
    turn_done = asyncio.Event()
    delivery_cancelled = asyncio.Event()
    delivery_finished = asyncio.Event()

    async def on_fire(_loop):
        delivering.set()
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=_LOST_RUN_SECS)
        except asyncio.CancelledError:
            delivery_cancelled.set()
            raise
        delivery_finished.set()
        return True

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await svc.start()
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda kind, lp: events.append((kind, getattr(lp, "id", ""))))
    patrol = await svc.add(
        slot_key="slack:C1:T1",
        message="patrol",
        idle_secs=600,
        replace_existing=False,
        default_patrol=True,
    )
    svc._cancel_timer(patrol.id)
    real_timer = svc._timer
    armed_after_closure: list[str] = []

    def _recording_timer(lp, delay=None):
        if not svc._accepting_mutations:
            armed_after_closure.append(lp.id)
        return real_timer(lp, delay)

    monkeypatch.setattr(svc, "_timer", _recording_timer)
    svc._arm_timer(patrol, delay=0.0)
    tick = svc._timers[patrol.id]
    await asyncio.wait_for(delivering.wait(), timeout=_LOST_RUN_SECS)

    # The conductor's own monitor_start, arriving while the default patrol's turn runs.
    own = await asyncio.wait_for(
        svc.add(
            slot_key="slack:C1:T1",
            message="my own patrol",
            idle_secs=600,
            replace_existing=False,
            replace_stopped=True,
        ),
        timeout=_LOST_RUN_SECS,
    )
    assert svc.get_by_slot("slack:C1:T1") is own
    assert svc.get_by_id(patrol.id) is None
    assert patrol.id not in svc._timers, "the displaced tick left the timer table"

    held = False
    try:
        if cut == "delivery":
            assert not delivery_cancelled.is_set(), "the arm did not cut the displaced delivery"
            assert tick in svc._running_callbacks, "the displaced tick is a running callback"
            shutdown = asyncio.create_task(svc.shutdown())
            await asyncio.wait_for(delivery_cancelled.wait(), timeout=_LOST_RUN_SECS)
        else:
            if cut == "delivery_record":
                # Held so the delivery record parks on the lock inside its section.
                await asyncio.wait_for(svc._lock.acquire(), timeout=_LOST_RUN_SECS)
                held = True
            turn_done.set()
            await _wait_until(
                lambda: delivery_finished.is_set() or delivery_cancelled.is_set(),
                "the end of the displaced delivery",
            )
            assert delivery_finished.is_set(), "the displaced delivery finished"
            if cut == "delivery_record":
                await _wait_until(
                    lambda: tick in _an._timers._drain_registered_owners(svc),
                    "the displaced delivery record's registration",
                )
                shutdown = asyncio.create_task(svc.shutdown())
                await _wait_until(
                    lambda: not svc._accepting_mutations, "shutdown admission closure"
                )
                assert not shutdown.done(), "shutdown drains the displaced delivery record"
                assert not tick.done()
                svc._lock.release()
                held = False
            else:
                await asyncio.wait_for(asyncio.shield(tick), timeout=_LOST_RUN_SECS)
                assert patrol.id not in svc._timers, "the removed row was not re-armed"
                shutdown = asyncio.create_task(svc.shutdown())
        await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)
    finally:
        if held:
            svc._lock.release()
        turn_done.set()

    if cut == "delivery":
        assert tick.cancelled(), "shutdown cancelled the displaced tick mid-delivery"
        assert not delivery_finished.is_set()
    else:
        assert tick.done() and not tick.cancelled()
    assert patrol.id not in svc._timers
    assert armed_after_closure == [], "nothing may be armed after admission closed"
    assert [kind for kind, loop_id in events if loop_id == patrol.id] == ["added", "removed"]
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    assert [lp.id for lp in restored.list_all()] == [own.id], "the removed row stays removed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tick_state", ["cancelled", "draining"])
async def test_a_failed_displacement_gives_no_tick_back_after_closure(
    tmp_path, monkeypatch, tick_state
):
    """The give-back of a displaced default patrol's tick never outlives admission.

    When the replacement's store write fails, the arm restores the default patrol and
    puts its still-running tick back in the timer table. An arm admitted before
    shutdown reaches that give-back after closure, once the table was emptied; by then
    the tick is either a running callback shutdown cancelled that is still unwinding,
    or one shutdown drains through its delivery record. Neither may return to the
    table, and nothing is armed for the restored row.
    """
    delivering = asyncio.Event()
    turn_done = asyncio.Event()
    delivery_cancelled = asyncio.Event()
    unwound = asyncio.Event()

    async def on_fire(_loop):
        delivering.set()
        try:
            await asyncio.wait_for(turn_done.wait(), timeout=_LOST_RUN_SECS)
        except asyncio.CancelledError:
            delivery_cancelled.set()
            # A cancelled channel turn unwinds over several steps, so its tick is
            # still running when the failed arm reaches the give-back.
            await asyncio.wait_for(unwound.wait(), timeout=_LOST_RUN_SECS)
            raise
        return True

    svc = AutoNudgeService(base_dir=tmp_path, on_fire=on_fire)
    await svc.start()
    registered_after_closure: list[str] = []

    class _ClosureWatchedTimers(dict):
        def __setitem__(self, key, value):
            if not svc._accepting_mutations:
                registered_after_closure.append(key)
            super().__setitem__(key, value)

    svc._timers = _ClosureWatchedTimers(svc._timers)
    patrol = await svc.add(
        slot_key="slack:C1:T1",
        message="patrol",
        idle_secs=600,
        replace_existing=False,
        default_patrol=True,
    )
    svc._cancel_timer(patrol.id)
    real_timer = svc._timer
    armed_after_closure: list[str] = []

    def _recording_timer(lp, delay=None):
        if not svc._accepting_mutations:
            armed_after_closure.append(lp.id)
        return real_timer(lp, delay)

    monkeypatch.setattr(svc, "_timer", _recording_timer)
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()
    writes: list[None] = []

    def _first_write_fails(payload):
        writes.append(None)
        if len(writes) == 1:
            write_started.set()
            assert release_write.wait(timeout=_LOST_RUN_SECS)
            raise OSError("the store refused the replacement")
        real_write(payload)

    svc._arm_timer(patrol, delay=0.0)
    tick = svc._timers[patrol.id]
    await asyncio.wait_for(delivering.wait(), timeout=_LOST_RUN_SECS)
    monkeypatch.setattr(svc, "_write_state", _first_write_fails)
    arm = asyncio.create_task(
        svc.add(
            slot_key="slack:C1:T1",
            message="my own patrol",
            idle_secs=600,
            replace_existing=False,
            replace_stopped=True,
        )
    )
    try:
        await async_wait_until(write_started.is_set)
        assert patrol.id not in svc._timers and svc.get_by_id(patrol.id) is None
        if tick_state == "draining":
            # The delivery returns and its record waits on the lock the arm holds
            # across its write: a drained owner, not a cancelled one.
            turn_done.set()
            await _wait_until(
                lambda: tick in _an._timers._drain_registered_owners(svc),
                "the displaced delivery record's registration",
            )
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        if tick_state == "cancelled":
            await asyncio.wait_for(delivery_cancelled.wait(), timeout=_LOST_RUN_SECS)
        assert not tick.done(), "the tick is still running when the give-back runs"
    finally:
        release_write.set()
    with pytest.raises(OSError):
        await asyncio.wait_for(arm, timeout=_LOST_RUN_SECS)
    assert registered_after_closure == [], "the give-back registered the tick after closure"
    unwound.set()
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    if tick_state == "cancelled":
        assert tick.cancelled()
    else:
        assert tick.done() and not tick.cancelled()
    assert registered_after_closure == []
    assert armed_after_closure == [], "nothing may be armed after admission closed"
    assert svc.get_by_slot("slack:C1:T1") is patrol, "the failed replacement restored the patrol"
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    stored = restored.get_by_slot("slack:C1:T1")
    assert stored is not None and stored.id == patrol.id
    # A drained delivery record lands on the restored row; a cancelled delivery has none.
    assert stored.cycle_count == (1 if tick_state == "draining" else 0)


@pytest.mark.asyncio
async def test_a_displaced_default_patrol_completion_resolves_through_shutdown_unwritten(
    tmp_path, monkeypatch, caplog
):
    """A completion for the displaced default patrol's wake finds no row and writes nothing.

    The agent's own arm replaces an ACTIVE default patrol even while its wake is in
    flight, so that wake's completion accounting can arrive after the row is gone.
    Admitted before closure, it waits on the lock the replacement holds across its
    write and runs after shutdown closed admission: it charges, writes and announces
    nothing, and its drained transaction resolves, so shutdown is not left waiting on
    it. A completion that arrives after closure is refused at admission.
    """

    class _ObservedLock(asyncio.Lock):
        def __init__(self) -> None:
            super().__init__()
            self.contended = asyncio.Event()

        async def acquire(self) -> bool:
            if self.locked():
                self.contended.set()
            return await super().acquire()

    slot = "chat-p-conductor"
    svc = AutoNudgeService(base_dir=tmp_path)
    lock = _ObservedLock()
    svc._lock = lock
    events: list[tuple[str, str]] = []
    svc.subscribe(lambda kind, lp: events.append((kind, getattr(lp, "id", ""))))
    patrol = await svc.add(
        slot_key=slot,
        message="patrol",
        idle_secs=600,
        watch="work-ledger",
        replace_existing=False,
        default_patrol=True,
    )
    svc._cancel_timer(patrol.id)
    assert patrol.monitor is not None
    patrol.monitor.wake_in_flight = True
    patrol.monitor.last_wake_fingerprint = "fp-default"
    completion = MonitorActionCompletion(
        monitor_id=patrol.id,
        fingerprint="fp-default",
        disposition=MonitorActionDisposition.SUCCESS,
        completed_ts=patrol.created_ts + 1.0,
    )
    real_write = svc._write_state
    write_started = threading.Event()
    release_write = threading.Event()
    writes: list[None] = []

    def _counted_write(payload):
        writes.append(None)
        if len(writes) == 1:
            write_started.set()
            assert release_write.wait(timeout=_LOST_RUN_SECS)
        real_write(payload)

    monkeypatch.setattr(svc, "_write_state", _counted_write)
    caplog.set_level(logging.DEBUG, logger="kiro_crew.autonudge")
    arm = asyncio.create_task(
        svc.add(
            slot_key=slot,
            message="my own patrol",
            idle_secs=600,
            replace_existing=False,
            replace_stopped=True,
        )
    )
    try:
        await async_wait_until(write_started.is_set)
        assert svc.get_by_id(patrol.id) is None
        lock.contended.clear()
        accounting = asyncio.create_task(svc.record_monitor_turn_completion(completion))
        await asyncio.wait_for(lock.contended.wait(), timeout=_LOST_RUN_SECS)
        shutdown = asyncio.create_task(svc.shutdown())
        await _wait_until(lambda: not svc._accepting_mutations, "shutdown admission closure")
        assert not shutdown.done(), "shutdown drains the admitted completion"
    finally:
        release_write.set()
    own = await asyncio.wait_for(arm, timeout=_LOST_RUN_SECS)
    assert await asyncio.wait_for(accounting, timeout=_LOST_RUN_SECS) is None
    await asyncio.wait_for(shutdown, timeout=_LOST_RUN_SECS)

    assert len(writes) == 1, "only the replacement wrote; the removed row's completion did not"
    assert svc.get_by_id(patrol.id) is None and svc.get_by_slot(slot) is own
    assert own.cycle_count == 0
    assert [kind for kind, loop_id in events if loop_id == patrol.id] == ["added", "removed"]
    assert any(
        record.levelno == logging.DEBUG
        and "dropping the turn completion" in record.getMessage()
        and patrol.id in record.getMessage()
        for record in caplog.records
    )
    assert svc._inflight_adds == set()
    with pytest.raises(NudgeAdmissionRefused) as refused:
        await asyncio.wait_for(
            svc.record_monitor_turn_completion(completion), timeout=_LOST_RUN_SECS
        )
    assert refused.value.reason is NudgeAdmissionReason.SERVICE_SHUTTING_DOWN
    restored = AutoNudgeService(base_dir=tmp_path)
    await asyncio.to_thread(restored._load)
    assert [lp.id for lp in restored.list_all()] == [own.id]
