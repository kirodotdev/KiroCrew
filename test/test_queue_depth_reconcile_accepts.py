"""The rows a ``spawn_async`` accept holds, and wakes that are already due.

An accept cancelled mid-defer leaves a stoppable, counted row; an overdue wake is
never re-armed at zero; a row an accept still holds arms no wake, is counted
once queued and starts once let go; and each frame names only the waves it
answers. Shared manager, recorder and waits live in
``queue_depth_reconcile_helpers.py``.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from overload_fakes import settle_depth_emits, settle_store_writes
from queue_depth_reconcile_helpers import (
    _ADMISSION_LOGGER,
    _PARENT,
    PUMP_MODES,
    _card,
    _close,
    _defer,
    _depths,
    _fail_chip_reads,
    _free_slot,
    _manager,
    _park,
    _record,
    _settle,
    _store_waiting,
    _warned,
)

import kiro_crew.subagent as subagent_mod
import kiro_crew.subagent_manager.run as run_mod
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission.types import MIN_RECHECK_DELAY_SECS
from kiro_crew.taskq import KIND_SUBAGENT, model
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── an accept cancelled mid-defer, and a wake that is already due ───────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_spawn_async_cancelled_during_its_defer_leaves_a_stoppable_counted_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The caller's cancel must neither cancel the defer write nor leave the
    row marked as still being admitted, which every refill, stop and count
    leaves out."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    entered, release = asyncio.Event(), asyncio.Event()
    real_await = SpawnAdmissionCoordinator.await_pending_defer
    wait = {"cancelled": False, "finished": False}

    async def slow_await(self: Any, agent_id: str) -> Any:
        entered.set()
        try:
            await await_until(release, what="the test's release")
        except asyncio.CancelledError:
            wait["cancelled"] = True
            raise
        result = await real_await(self, agent_id)
        wait["finished"] = True
        return result

    monkeypatch.setattr(SpawnAdmissionCoordinator, "await_pending_defer", slow_await)
    try:
        with monkeypatch.context() as low:
            low.setattr(
                subagent_mod, "check_memory_available", lambda min_gb=None, path=None: (False, 0.2)
            )
            with patch.object(SubagentManager, "_run", new=AsyncMock()):
                call = asyncio.create_task(mgr.spawn_async("t", parent_session_key=_PARENT))
                await await_until(entered, what="the hold being entered")
                call.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await await_until(call, what="the spawn call finishing")
        await _settle(mgr)

        # The defer write ran to completion under the caller's cancel.
        assert wait == {"cancelled": False, "finished": True}
        assert not mgr.__dict__.get("_admitting_ids")
        assert mgr._admitting_waiting == set()
        assert mgr.queued_count_for(_PARENT) == 1
        events = _record(mgr)
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)
        assert _depths(events)[-1]["queued"] == 1
        assert await mgr.cancel_for_parent(_PARENT) == (0, 1)
    finally:
        release.set()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_overdue_wake_is_never_rearmed_at_zero(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A deferred row past its wake that no pass may claim re-ran the empty pass
    on every loop turn; the wake is floored, and the stuck row is reported."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    loop = asyncio.get_running_loop()
    delays: list[float] = []
    try:
        store = mgr._taskq
        # Scoped: the loop's own ``call_later`` is back before teardown runs.
        with monkeypatch.context() as timers, caplog.at_level("WARNING", logger=_ADMISSION_LOGGER):
            timers.setattr(loop, "call_later", lambda delay, *a, **k: delays.append(delay))
            mgr._admission._refill_schedule_wake(store, store.now() - 30)
            mgr._admission._refill_schedule_wake(store, store.now() - 30)

        assert delays == [MIN_RECHECK_DELAY_SECS, MIN_RECHECK_DELAY_SECS]
        assert _warned(caplog, _ADMISSION_LOGGER, "past its wake") == 1
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_started_row_is_not_left_waiting_when_its_pop_request_overlapped_a_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pump asks at the pop and again at the start's registration. The pop
    request lands while the previous frame is still being sent, and must
    still be answered on its own."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    policy_hold, policy_release = asyncio.Event(), asyncio.Event()
    publish_hold, publish_release = asyncio.Event(), asyncio.Event()
    armed = {"on": False}
    frames: list[dict[str, Any]] = []

    async def slow_policy(fn: Any, *a: Any, **kw: Any) -> Any:
        policy_hold.set()
        await await_until(policy_release, what="the policy's release")
        return fn(*a, **kw)

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype != "subagent_queued" or info.parent_session_key != _PARENT:
            return
        frames.append(dict(extra))
        if armed["on"]:
            armed["on"] = False
            publish_hold.set()
            await await_until(publish_release, what="the publish's release")

    try:
        with (
            patch.object(SubagentManager, "_run", new=AsyncMock()),
            # flake-ok: the stdlib asyncio.to_thread is patched only for this with-block, and slow_policy's hold is a bounded await_until
            patch("asyncio.to_thread", new=slow_policy),
        ):
            first = mgr.spawn("t0", parent_session_key=_PARENT)
            second = mgr.spawn("t1", parent_session_key=_PARENT)
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
            mgr._on_event = on_event
            armed["on"] = True
            mgr._emit_queue_depth(_PARENT)
            await await_until(publish_hold, what="the publish being held")
            assert frames[-1]["queued"] == 1
            await _free_slot(mgr, first)
            await await_until(policy_hold, what="the policy being held")
            publish_release.set()
            await settle_depth_emits(mgr)
            policy_release.set()
            await _settle(mgr)

        assert second.id in mgr._agents
        assert frames[-1]["queued"] == 0, frames
    finally:
        policy_release.set()
        publish_release.set()
        await mgr.cancel_all()
        _close(mgr)


# ── a row its accept holds: no wake, counted once queued, and let go into a slot


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize("held", ["accepted", "deferred-past-due"])
async def test_a_row_an_accept_still_holds_arms_no_wake_and_no_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    pump_off_loop: bool,
    held: str,
) -> None:
    """A ``spawn_async`` accept in flight holds its row (``_admitting_ids``),
    so the refill may not claim it, and it has no wake to offer. Read as one
    (a fresh row as "due at 0"), an empty pass re-armed itself every 50 ms and
    logged a deferred row decades past its wake on ordinary spawn traffic."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    loop = asyncio.get_running_loop()
    real_call_later = loop.call_later
    wakes: list[float] = []

    def call_later(delay: float, callback: Any, *args: Any, **kw: Any) -> Any:
        if callback == mgr._drain_queue:
            wakes.append(delay)
        return real_call_later(delay, callback, *args, **kw)

    admitting: set[str] = mgr.__dict__.setdefault("_admitting_ids", set())
    try:
        store = mgr._taskq
        row = model.TaskRecord(
            id="held-row",
            kind=KIND_SUBAGENT,
            session_key=_PARENT,
            params={"task": "t", "parent_session_key": _PARENT},
        )
        await store.run(store.accept_one, row)
        if held == "deferred-past-due":
            await store.run(store.defer, row.id, wait=-30, reason="test")
        admitting.add(row.id)
        with monkeypatch.context() as timers, caplog.at_level("WARNING", logger=_ADMISSION_LOGGER):
            timers.setattr(loop, "call_later", call_later)
            if pump_off_loop:
                assert await mgr._admission.taskq_refill_window_async() == 0
            else:
                assert mgr._admission.taskq_refill_window() == 0

        assert wakes == []
        assert _warned(caplog, _ADMISSION_LOGGER, "past its wake") == 0
    finally:
        admitting.discard("held-row")
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_exclusions_survive_an_accept_landing_while_they_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refill reads its exclusions on the store's writer thread while
    ``spawn_async`` adds and discards ``_admitting_ids`` on the loop. An accept
    landing between two ids of that read (pinned here inside the *counted*
    membership test, where a thread switch can fall) must not abort the pump
    pass with "Set changed size during iteration": the read filters a copy."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    admitting: set[str] = mgr.__dict__.setdefault("_admitting_ids", set())
    landed: list[str] = []

    class AcceptsLandMidRead(set[str]):
        def __contains__(self, aid: object) -> bool:
            landed.append(f"landed-{len(landed)}")
            admitting.add(landed[-1])
            return super().__contains__(aid)

    try:
        admitting.update({"held-a", "held-b"})
        excluded = mgr._admission.taskq_dispatch_excluded_ids(
            counted=AcceptsLandMidRead({"held-b"})
        )

        assert landed, "the membership test ran mid-read"
        assert "held-a" in excluded and "held-b" not in excluded
    finally:
        admitting.difference_update({"held-a", "held-b", *landed})
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_capacity_queued_spawn_async_is_counted_and_starts_once_let_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queued behind the cap on disk, a row its ``spawn_async`` still holds is
    counted from the gate's verdict on. Every pump pass during the hold leaves
    it out, so a slot that frees then is the row's once the call lets go:
    nothing else would ask again, as no time holds the row."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    held = {"id": ""}
    entered, release = asyncio.Event(), asyncio.Event()
    real_accept = SpawnAdmissionCoordinator.taskq_accept_record
    real_await = SpawnAdmissionCoordinator.await_pending_defer

    def spy_accept(self: Any, record: Any) -> Any:
        held["id"] = record.id
        return real_accept(self, record)

    async def held_await(self: Any, agent_id: str) -> Any:
        if agent_id == held["id"]:
            entered.set()
            await await_until(release, what="the test's release")
        return await real_await(self, agent_id)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_accept_record", spy_accept)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "await_pending_defer", held_await)
    try:
        # Another parent's deferred row waits outside the window, so the
        # window keeps FIFO with the store and this spawn's row stays there too.
        _defer(mgr, 1, parent="dash:other")
        await _settle(mgr)
        events = _record(mgr)
        mgr._max_concurrent = 0
        with patch.object(SubagentManager, "_run", new=AsyncMock()):
            call = asyncio.create_task(mgr.spawn_async("b", parent_session_key=_PARENT))
            await await_until(entered, what="the hold being entered")
            assert held["id"] not in {q.get("_preassigned_id") for q in mgr._queue}
            await settle_depth_emits(mgr)
            assert _depths(events)[-1]["queued"] == 1
            # A slot frees during the hold; the pass it drives must leave the row.
            mgr._max_concurrent = 1
            mgr._drain_queue()
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
            assert held["id"] not in mgr._agents
            release.set()
            await await_until(call, what="the spawn call finishing")
            await _settle(mgr)

        assert held["id"] in mgr._agents
        assert _card(events) == 0 == _store_waiting(mgr)
    finally:
        release.set()
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_start_answers_the_card_when_its_pop_read_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pump asks at the pop and again at the registration: a pop read the
    store could not answer publishes nothing, and the start itself must still
    take the started row off the card, not the delayed re-read."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            first = mgr.spawn("t0", parent_session_key=_PARENT)
            second = mgr.spawn("t1", parent_session_key=_PARENT)
            await _settle(mgr)
            events = _record(mgr)
            calls = _fail_chip_reads(monkeypatch, times=1)
            await _free_slot(mgr, mgr._agents[first.id])
            await _settle(mgr)

        assert second.id in mgr._tasks
        assert calls, "the pop never asked for the depth"
        assert _depths(events) == [{"queued": 0}]
        assert mgr._queue_depth_retries == {}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize("second", ["wave-b", ""], ids=["another-wave", "no-wave"])
async def test_each_frame_names_only_the_waves_it_answers(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, second: str
) -> None:
    """A request made while a frame is being sent is answered by the next
    frame, which names that request's wave, not the earlier frame's too."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    sending, release = asyncio.Event(), asyncio.Event()
    batches: list[str] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype == "subagent_queued":
            batches.append(info.batch_id)
            if len(batches) == 1:
                sending.set()
                await await_until(release, what="the test's release")

    try:
        mgr._on_event = on_event
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await await_until(sending, what="the consumer taking its first frame")
        mgr._emit_queue_depth(_PARENT, second)
        release.set()
        await _settle(mgr)

        assert batches == ["wave-a", second]
    finally:
        release.set()
        _close(mgr)
