"""The queued count stays exact: every settle point re-publishes it, coalesced.

``subagent_queued`` is pushed, and the dashboard otherwise resets its count only
from a reconnect's snapshot. A frame it missed, or one that arrived after the
frame that superseded it, leaves "N waiting to start" and the old wait reason on
the card after every run has finished. So the authoritative depth is re-published
at every point a wave settles -- each terminal report of a run that started, each
stop of a waiting row, and a Stop all or stage Cancel that stopped nothing -- and
the emit is coalesced per parent, so a bulk stop costs about one frame. A read
answers every request made before it started, because a posted store write is
queued on the writer thread by the call that posts it; the count covers
unstarted spawns only; and a store that cannot be read publishes nothing rather
than a false 0, then retries.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import (
    memory_critical,
    mock_ctx,
    mock_sessions,
    settle_depth_emits,
    settle_store_writes,
)

import kiro_crew.subagent as subagent_mod
import kiro_crew.subagent_manager.admission.taskq_bridge as taskq_bridge_mod
import kiro_crew.subagent_manager.run as run_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission.types import MIN_RECHECK_DELAY_SECS
from kiro_crew.subagent_manager.run import _QUEUE_DEPTH_RETRIES
from kiro_crew.subagent_wait_reasons import QUEUED_REASON_LOW_MEMORY
from kiro_crew.taskq import KIND_SUBAGENT, model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.taskq.waits import WaitRecord

#: Where each warning these tests count is logged; a count filters on it, so a
#: record another thread in the worker logs cannot move it.
_DEPTH_LOGGER = subagent_mod.logger.name
_ADMISSION_LOGGER = taskq_bridge_mod._glue_logger.name


def _warned(caplog: pytest.LogCaptureFixture, logger: str, text: str) -> int:
    return sum(
        1
        for r in caplog.records
        if r.name == logger and r.levelno >= logging.WARNING and text in r.getMessage()
    )


pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: Both count paths: the store read on the writer thread (production) and the
#: inline one the rest of the suite pins.
PUMP_MODES = pytest.mark.parametrize(
    "pump_off_loop", [True, False], ids=["writer-thread", "on-loop"]
)

#: The label a memory-deferred wave leaves behind for its parent.
_STALE_WAIT = {"reason": QUEUED_REASON_LOW_MEMORY, "available_gb": 6.1, "required_gb": 6.5}

_PARENT = "dash:depth-parent"

#: ``_settle``'s one ceiling for everything a test caused, kept well under the
#: tests' own ``timeout(30)`` so a wedged task fails here, by name, instead of
#: taking the xdist worker down with the pytest-timeout kill.
_SETTLE_SECS = 20.0

Event = tuple[str, str, str, dict[str, Any]]


async def _manager(
    monkeypatch: pytest.MonkeyPatch, *, pump_off_loop: bool, max_concurrent: int = 3
) -> SubagentManager:
    """A real manager on the chosen count path, with its task store open."""
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", pump_off_loop)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", pump_off_loop)
    mgr = SubagentManager(
        sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=max_concurrent
    )
    await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    mgr._spawn_stagger_secs = 0.0
    mgr._last_spawn_ts = 0.0
    # A delayed re-read after an unreadable store comes this soon. Only that
    # delay: ``admit_wait_secs`` stays at its default, because it is also every
    # deferred row's ``next_run_at`` and the pump's re-check, and a deferral
    # that lapsed mid-test would start the very rows a test is about to stop.
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", MIN_RECHECK_DELAY_SECS)
    return mgr


def _record(mgr: SubagentManager) -> list[Event]:
    events: list[Event] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        events.append((etype, info.parent_session_key, info.id, dict(extra)))

    mgr._on_event = on_event
    return events


def _depths(events: list[Event], parent: str = _PARENT) -> list[dict[str, Any]]:
    return [
        extra for etype, key, _id, extra in events if etype == "subagent_queued" and key == parent
    ]


def _card(events: list[Event], parent: str = _PARENT) -> int:
    """The count the dashboard's card holds after these frames: its reducer
    (``sseSubagentQueued`` in ``website/src/store/chat/subagents.ts``) keeps
    the last frame's count per slot, and a 0 deletes the entry."""
    shown = 0
    for frame in _depths(events, parent):
        shown = max(0, int(frame["queued"]))
    return shown


def _kinds(events: list[Event]) -> list[str]:
    return [etype for etype, _key, _id, _extra in events]


async def _settle(mgr: SubagentManager) -> None:
    """Wait until every task the test set going has finished, or fail by name.

    Signals, not sleeps: every task but the test's own and the runs it parks
    on purpose is awaited by its handle, the store's single writer thread is
    drained as a FIFO barrier, and the loop is given turns until nothing new
    appears. One ceiling, :data:`_SETTLE_SECS`, covers the whole wait.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _SETTLE_SECS
    me = asyncio.current_task()
    while True:
        await settle_store_writes(mgr._taskq, rounds=2)
        parked = set(mgr._tasks.values())
        pending = [t for t in asyncio.all_tasks() if t is not me and t not in parked]
        pending = [t for t in pending if not t.done()]
        if not pending:
            await settle_depth_emits(mgr)
            if not mgr._queue_depth_emits:
                return
            continue
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError(f"_settle: {len(pending)} task(s) never finished: {pending}")
        await asyncio.wait(pending, timeout=left)


async def _until(check: Any, what: str, timeout: float = 5.0) -> None:
    """Wait for *check* to hold (a delayed re-read lands on a timer), or fail by name."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not check():
        if loop.time() > deadline:
            raise AssertionError(f"never happened: {what}")
        await asyncio.sleep(0.01)


def _close(mgr: SubagentManager) -> None:
    # ``close`` detaches the store before closing it, as production does, so a
    # pump timer that fires during loop teardown finds no store to restart.
    mgr.close()


def _store_waiting(mgr: SubagentManager, parent: str = _PARENT) -> int:
    """The store's own answer: rows still waiting for *parent*, live runs excluded."""
    live = [aid for aid, info in mgr._agents.items() if not info.done]
    return mgr._taskq.count_pending(KIND_SUBAGENT, session_key=parent, exclude_ids=live)


def _defer(mgr: SubagentManager, count: int, parent: str = _PARENT) -> list[SubagentInfo]:
    """*count* spawns the memory gate defers: rows held by the store alone."""
    with patch.object(subagent_mod, "cached_admission_check", memory_critical):
        with patch.object(SubagentManager, "_run", new=AsyncMock()):
            infos = [mgr.spawn(f"deferred-{i}", parent_session_key=parent) for i in range(count)]
    assert all(info.queued and not info.done for info in infos)
    windowed = {q.get("_preassigned_id") for q in mgr._queue}
    assert not windowed & {info.id for info in infos}
    return infos


async def _park(_self: SubagentManager, _info: SubagentInfo) -> None:
    """A run that holds its slot until it is stopped."""
    await asyncio.Event().wait()


def _fail_chip_reads(monkeypatch: pytest.MonkeyPatch, times: int) -> list[int]:
    """The chip's store read answers "unreadable" for its first *times* calls."""
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    calls: list[int] = []

    async def flaky(self: Any, parent: str) -> int | None:
        calls.append(1)
        if len(calls) <= times:
            return None
        return await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", flaky)
    return calls


# ── every settle point answers ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stop_all_with_nothing_to_stop_publishes_zero_and_forgets_the_label(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        events = _record(mgr)

        assert await mgr.cancel_for_parent(_PARENT) == (0, 0)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert _PARENT not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stage_cancel_with_nothing_to_stop_publishes_zero_and_forgets_the_label(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        events = _record(mgr)

        assert await mgr.cancel_for_boundary(_PARENT, "stage-1") == (0, 0)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert _PARENT not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_refused_stage_cancel_still_publishes_the_depth_once(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        events = _record(mgr)
        monkeypatch.setattr(
            mgr, "_hold_boundary_cancellation", lambda parent, owner: "pending_scope_cap"
        )

        assert await mgr.cancel_for_boundary(_PARENT, "stage-1") == (0, 0)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert _PARENT not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_terminal_report_republishes_its_parents_queued_depth(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        events = _record(mgr)
        info = SubagentInfo(id="a1", task="t", parent_session_key=_PARENT, batch_id="b1")

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )
        await _settle(mgr)

        assert _kinds(events) == ["subagent_done", "subagent_queued"]
        assert _depths(events) == [{"queued": 0}]
        assert _PARENT not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_queued_stop_terminal_adds_no_depth_frame_of_its_own(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The stop that removed the row asked for the depth; its terminal does not."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        events = _record(mgr)
        info = SubagentInfo(
            id="q1",
            task="t",
            parent_session_key=_PARENT,
            batch_id="b1",
            user_stopped=True,
            queued=True,
        )

        await mgr._report_terminal(
            info,
            source="Queued stop",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )
        await _settle(mgr)

        assert _kinds(events) == ["subagent_done"]
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stopping_a_row_held_only_by_the_store_publishes_the_depth(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A row that spilled out of the window is counted, so its stop re-counts."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        rows = _defer(mgr, 2)
        await _settle(mgr)
        events = _record(mgr)

        assert await mgr.cancel(rows[0].id) is True
        await _settle(mgr)

        assert [frame["queued"] for frame in _depths(events)] == [1]
        assert _store_waiting(mgr) == 1
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_failing_depth_emit_never_costs_the_parent_its_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=False)
    try:
        on_done = AsyncMock()
        mgr._on_done = on_done
        monkeypatch.setattr(mgr, "_emit_queue_depth", MagicMock(side_effect=RuntimeError("boom")))
        info = SubagentInfo(id="a2", task="t", parent_session_key=_PARENT)

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )

        on_done.assert_awaited_once_with(info)
        assert await mgr.cancel_for_parent(_PARENT) == (0, 0)
    finally:
        _close(mgr)


# ── a burst costs about one frame ────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stop_all_over_memory_deferred_rows_ends_at_zero_in_one_frame(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """Rows the memory gate deferred live only in the store, outside the window;
    each of their stops asks for the depth, and all of them share one read."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        _defer(mgr, 3)
        await _settle(mgr)
        assert mgr._queue_wait[_PARENT]["reason"]
        events = _record(mgr)

        assert await mgr.cancel_for_parent(_PARENT) == (0, 3)
        await _settle(mgr)

        assert _kinds(events).count("subagent_done") == 3
        assert _depths(events) == [{"queued": 0}]
        assert _store_waiting(mgr) == 0
        assert _PARENT not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@PUMP_MODES
async def test_stop_all_over_thirty_queued_and_ten_running_sends_at_most_three_frames(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """Thirty stops, ten reaped-run terminals: one burst."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=10)
    mgr._taskq._window = 10
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            infos = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(40)]
            await _settle(mgr)
            assert sum(1 for info in infos if info.id in mgr._tasks) == 10
            assert len(mgr._queue) == 10
            assert mgr._admission.taskq_overflow(_PARENT) == 20
            events = _record(mgr)

            assert await mgr.cancel_for_parent(_PARENT) == (10, 30)
            await _settle(mgr)

        assert _kinds(events).count("subagent_done") == 40
        # On the writer-thread path every request lands while a read is in
        # flight; on the inline path a read finishes inside its own step, so
        # only requests made in the same step share it.
        assert 1 <= len(_depths(events)) <= (3 if pump_off_loop else 6)
        assert _depths(events)[-1] == {"queued": 0}
        assert _store_waiting(mgr) == 0
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_stage_cancel_over_several_rows_sends_at_most_two_frames(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            for i in range(4):
                mgr.spawn(f"t{i}", parent_session_key=_PARENT, _stage_boundary_owner="stage-1")
            await _settle(mgr)
            assert mgr.queued_count_for(_PARENT) == 4
            events = _record(mgr)

            assert await mgr.cancel_for_boundary(_PARENT, "stage-1") == (0, 4)
            await _settle(mgr)

        assert 1 <= len(_depths(events)) <= 2
        assert _depths(events)[-1] == {"queued": 0}
        assert _store_waiting(mgr) == 0
        await mgr.cancel_all()
    finally:
        _close(mgr)


async def _free_slot(mgr: SubagentManager, info: SubagentInfo, *, drain: bool = True) -> None:
    """End a run without a report, the way its own ``finally`` would, and
    (unless *drain* is off) let the pump fill its slot."""
    task = mgr._tasks.get(info.id)
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    info.done = True
    mgr._claim_finalize(info)
    assert mgr._release_slot(info), "the run's slot was already released"
    mgr._running_count -= 1
    if drain:
        mgr._drain_queue()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_row_the_pump_starts_costs_at_most_two_exact_frames(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The drain asks for the depth when it pops a row and the row's
    registration asks again; each answer is the count after the pop."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            infos = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(4)]
            await _settle(mgr)
            events = _record(mgr)
            for running, starting, waiting in ((0, 1, 2), (1, 2, 1), (2, 3, 0)):
                before = len(_depths(events))
                await _free_slot(mgr, mgr._agents[infos[running].id])
                await _settle(mgr)
                assert infos[starting].id in mgr._tasks
                frames = [f["queued"] for f in _depths(events)[before:]]
                assert frames in ([waiting], [waiting, waiting]), frames
                assert _card(events) == waiting == _store_waiting(mgr)
    finally:
        await mgr.cancel_all()
        _close(mgr)


# ── the count is unstarted spawns only ───────────────────────────────────────


class _SharedRuntime:
    """A shared provider whose shutdown suspends (live) or returns at once (dead)."""

    def __init__(self, live: bool) -> None:
        self.live = live

    async def shutdown(self) -> None:
        if self.live:
            await asyncio.sleep(0)

    def set_keep_transcript(self, keep: bool) -> None:
        pass


async def _resident_with_resume_entry(mgr: SubagentManager) -> tuple[SubagentInfo, SubagentInfo]:
    """A run that yielded its lane slot to a child and now asks for it back.

    Its resume entry sits in the window behind the full pool. The run's own
    ``finally`` withdraws the entry, as production's does.
    """
    store = mgr._taskq

    async def _resident(self: SubagentManager, info: SubagentInfo) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            if info._resume_pending:
                self._run_events._withdraw_resume(info)

    with patch.object(SubagentManager, "_run", new=_resident):
        resident = mgr.spawn("resident", parent_session_key=_PARENT)
        child = mgr.spawn("child", parent_session_key=_PARENT)
        await _settle(mgr)
        await store.run(store.transition, resident.id, model.RUNNING)
        assert mgr._admission.yield_slot(
            resident, WaitRecord.children([child.id], since=store.now())
        )
        await _settle(mgr)
        assert child.id in mgr._tasks
        assert mgr._admission.request_resume(resident) is True
        await _settle(mgr)
    assert [q.get("_resume_id") for q in mgr._queue] == [resident.id]
    return resident, child


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize("runtime", ["dead", "live"])
async def test_a_resume_entry_is_never_counted_as_waiting(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, runtime: str
) -> None:
    """A resident run's resume entry is not a spawn waiting to start.

    Counted, the card read "1 waiting" for work that had started, and nothing
    corrected it: the entry leaves the window silently. A dead shared runtime
    makes the reap return without suspending, so the reaped run's terminal
    frame is read before the run's ``finally`` withdraws the entry.
    """
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        resident, _child = await _resident_with_resume_entry(mgr)
        live = mgr._agents[resident.id]
        live._session_sharing = True
        live._shared_provider = _SharedRuntime(live=runtime == "live")
        assert mgr.queued_count_for(_PARENT) == 0
        events = _record(mgr)
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)
        assert _depths(events) == [{"queued": 0}]

        await mgr.cancel_for_parent(_PARENT)
        await _settle(mgr)

        assert all(frame["queued"] == 0 for frame in _depths(events))
        assert mgr.queued_count_for(_PARENT) == 0
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_lingering_resume_entry_is_never_counted_as_waiting(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A run whose terminal landed while its resume entry was still windowed.

    Its parent also holds one live run, which Stop all reaps; the freed slot
    lets the pump pop the stale entry, which it refuses without an emit.
    """
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            live = mgr.spawn("live", parent_session_key=_PARENT)
            await _settle(mgr)
            assert live.id in mgr._tasks
            lingering = SubagentInfo(id="lingering", task="t", parent_session_key=_PARENT)
            lingering._slot_released = True
            mgr._agents[lingering.id] = lingering
            assert mgr._admission.request_resume(lingering) is True
            lingering.done = True
            lingering.reaped = True
            events = _record(mgr)

            await mgr.cancel_for_parent(_PARENT)
            await _settle(mgr)

        assert _depths(events)
        assert all(frame["queued"] == 0 for frame in _depths(events))
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("count", [1, 5])
async def test_a_memory_deferred_spawn_async_is_counted_once_it_is_let_go(
    monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    """``spawn_async`` (the ``/api/spawn`` path) holds its row in
    ``_admitting_ids`` while the gate labels the deferral, and every depth read
    leaves such a row out; the count comes once the call lets the row go."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        events = _record(mgr)
        with monkeypatch.context() as low:
            low.setattr(
                subagent_mod, "check_memory_available", lambda min_gb=None, path=None: (False, 0.2)
            )
            with patch.object(SubagentManager, "_run", new=AsyncMock()):
                infos = await asyncio.gather(
                    *(mgr.spawn_async(f"t{i}", parent_session_key=_PARENT) for i in range(count))
                )
        await _settle(mgr)

        assert all(info is not None and info.queued for info in infos)
        assert _store_waiting(mgr) == count
        assert _depths(events)
        assert _depths(events)[-1]["queued"] == count
        assert _depths(events)[-1]["reason"] == QUEUED_REASON_LOW_MEMORY
        assert all(frame["queued"] > 0 for frame in _depths(events))
    finally:
        _close(mgr)


# ── reads land behind earlier writes, and overlapped reads stay unpublished ──


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_reread_lands_behind_a_write_posted_before_its_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The order a natural race produced: a read's result reaches the loop, and
    before the burst resumes a store write is posted and the depth asked for
    again. The re-read must queue behind that write on the writer thread."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    real_run = TaskStore.run
    read_done = asyncio.Event()
    hand_back: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    held = {"armed": False}

    async def run(self: TaskStore, fn: Any, /, *a: Any, **kw: Any) -> Any:
        is_count = isinstance(fn, functools.partial) and fn.func.__name__ == "count_pending"
        result = await real_run(self, fn, *a, **kw)
        if is_count and held["armed"]:
            held["armed"] = False
            read_done.set()
            await hand_back  # the test decides when this read's result lands
        return result

    try:
        (row,) = _defer(mgr, 1)
        await _settle(mgr)
        events = _record(mgr)
        monkeypatch.setattr(TaskStore, "run", run)
        held["armed"] = True
        mgr._emit_queue_depth(_PARENT)
        await asyncio.wait_for(read_done.wait(), 5)
        hand_back.set_result(None)
        # The burst's wake-up is queued now; everything below runs before it.
        assert not mgr._queue_depth_emits[_PARENT].task.done()
        admission = mgr._admission
        admission._post_store_write(
            mgr._taskq, "test cancel", admission.taskq_cancel_queued, row.id
        )
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _store_waiting(mgr) == 0
        assert _depths(events)[-1] == {"queued": 0}
    finally:
        if not hand_back.done():
            hand_back.set_result(None)
        _close(mgr)


async def _block_chip_reads(monkeypatch: pytest.MonkeyPatch) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold the chip's next store read until the returned *release* is set."""
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(self: Any, parent: str) -> int | None:
        if not release.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 5)
        return await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", held)
    return entered, release


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_verdict_during_a_read_keeps_its_reason_on_the_frame(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        _defer(mgr, 1)
        await _settle(mgr)
        events = _record(mgr)
        entered, release = await _block_chip_reads(monkeypatch)
        mgr._emit_queue_depth(_PARENT)
        await asyncio.wait_for(entered.wait(), 5)
        with patch.object(subagent_mod, "check_memory_available", lambda *a, **k: (False, 0.2)):
            with patch.object(SubagentManager, "_run", new=AsyncMock()):
                mgr.spawn("deferred-late", parent_session_key=_PARENT)
        release.set()
        await _settle(mgr)

        assert _depths(events)[-1]["queued"] == 2
        assert _depths(events)[-1]["reason"] == QUEUED_REASON_LOW_MEMORY
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_an_emit_that_never_ran_does_not_silence_the_parent(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        events = _record(mgr)
        mgr._emit_queue_depth(_PARENT)
        mgr._queue_depth_emits[_PARENT].task.cancel()
        await _settle(mgr)
        assert _depths(events) == []

        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
    finally:
        _close(mgr)


# ── an unreadable store publishes nothing, then retries ─────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_an_unreadable_store_publishes_nothing_and_keeps_the_label(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A 0 would clear a card whose rows still wait; a guess is worse than nothing."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    mgr._taskq_admit_wait_secs = 60.0  # no retry inside this test
    try:
        _defer(mgr, 3)
        await _settle(mgr)
        label = dict(mgr._queue_wait[_PARENT])
        events = _record(mgr)

        def _locked(*_a: Any, **_k: Any) -> Any:
            raise TaskStoreUnavailable("database is locked")

        with monkeypatch.context() as outage:
            outage.setattr(TaskStore, "list_pending", _locked)
            outage.setattr(TaskStore, "count_pending", _locked)
            assert await mgr.cancel_for_parent(_PARENT) == (0, 0)
            mgr._emit_queue_depth(_PARENT)
            await _settle(mgr)

        assert _depths(events) == []
        assert mgr._queue_wait[_PARENT] == label
        assert _store_waiting(mgr) == 3
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_card_converges_once_the_store_answers_again(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The stop's own read fails and nothing else will ask: the delayed
    re-read is what repairs the card."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        _defer(mgr, 3)
        await _settle(mgr)
        events = _record(mgr)
        calls = _fail_chip_reads(monkeypatch, times=1)

        assert await mgr.cancel_for_parent(_PARENT) == (0, 3)
        await _until(lambda: bool(_depths(events)), "the delayed re-read published")
        await _settle(mgr)

        assert len(calls) == 2
        assert _depths(events) == [{"queued": 0}]
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_store_that_stays_unreadable_gets_a_bounded_number_of_retries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        events = _record(mgr)
        calls = _fail_chip_reads(monkeypatch, times=1000)
        with caplog.at_level("WARNING", logger=_DEPTH_LOGGER):
            mgr._emit_queue_depth(_PARENT)
            await _until(
                lambda: _warned(caplog, _DEPTH_LOGGER, "unreadable after"),
                "the last retry gave up, at WARNING",
            )
        await _settle(mgr)

        assert len(calls) == 1 + _QUEUE_DEPTH_RETRIES
        assert _depths(events) == []
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize(
    "stop", ["single-cancel", "stage-cancel", "stop-all-store-rows", "stop-all-window-rows"]
)
async def test_every_stop_converges_after_one_unreadable_read(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, stop: str
) -> None:
    """A stop asks for the depth once; when that read fails, the retry answers."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            if stop == "stop-all-store-rows":
                _defer(mgr, 3)
            else:
                # Behind a pool another parent fills, so nothing the stop
                # leaves can start while the retry waits.
                mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
                owner = "stage-1" if stop == "stage-cancel" else ""
                rows = [
                    mgr.spawn(f"t{i}", parent_session_key=_PARENT, _stage_boundary_owner=owner)
                    for i in range(2 if stop == "single-cancel" else 4)
                ]
            await _settle(mgr)
            events = _record(mgr)
            calls = _fail_chip_reads(monkeypatch, times=1)

            if stop == "single-cancel":
                assert await mgr.cancel(rows[0].id) is True
            elif stop == "stage-cancel":
                assert await mgr.cancel_for_boundary(_PARENT, "stage-1") == (0, 4)
            else:
                assert (await mgr.cancel_for_parent(_PARENT))[1] in (3, 4)
            left = 1 if stop == "single-cancel" else 0
            await _until(
                lambda: bool(_depths(events)) and _depths(events)[-1]["queued"] == left,
                f"the card reached {left}",
            )
            await _settle(mgr)

        assert len(calls) >= 2
        assert _depths(events)[-1]["queued"] == left == _store_waiting(mgr)
    finally:
        await mgr.cancel_all()
        _close(mgr)


# ── Stop all when its queued pass fails ──────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize("shape", ["nothing-waiting", "store-rows-waiting", "window-rows-stopped"])
@pytest.mark.parametrize("failure", ["raises", "cancelled"])
async def test_stop_all_answers_the_card_when_its_store_read_fails(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, shape: str, failure: str
) -> None:
    """The store read is the queued pass's one await; whatever ends it, the
    card gets one answer: the call's own request when nothing was stopped
    before the read, the stopped rows' requests (one burst) when some were."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        if shape == "store-rows-waiting":
            _defer(mgr, 2)
            await _settle(mgr)
        elif shape == "window-rows-stopped":
            with patch.object(SubagentManager, "_run", new=_park):
                mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
                for i in range(2):
                    mgr.spawn(f"t{i}", parent_session_key=_PARENT)
                await _settle(mgr)
        else:
            mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        reached = asyncio.Event()

        async def _read(_self: Any, _parent: str) -> list[str]:
            reached.set()
            if failure == "raises":
                raise RuntimeError("store read failed")
            await asyncio.Event().wait()
            return []

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", _read)
        events = _record(mgr)

        stop = asyncio.create_task(mgr.cancel_for_parent(_PARENT))
        await asyncio.wait_for(reached.wait(), 5)
        if failure == "cancelled":
            stop.cancel()
        with pytest.raises(RuntimeError if failure == "raises" else asyncio.CancelledError):
            await asyncio.wait_for(stop, 5)
        await _settle(mgr)

        waiting = _store_waiting(mgr)
        assert waiting == (2 if shape == "store-rows-waiting" else 0)
        assert [frame["queued"] for frame in _depths(events)] == [waiting]
        assert (_PARENT in mgr._queue_wait) is (waiting > 0)
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_failing_queued_pass_stops_nothing_running(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """Reaping after the failure would free a slot the pump fills at once with
    a row the failed pass never reached; the request reports the failure and
    leaves the parent as it found it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            running = mgr.spawn("running", parent_session_key=_PARENT)
            waiting = mgr.spawn("waiting", parent_session_key=_PARENT)
            await _settle(mgr)
            mgr._queue.clear()  # the row now lives in the store alone

            async def _read(_self: Any, _parent: str) -> list[str]:
                raise RuntimeError("store read failed")

            monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", _read)
            with pytest.raises(RuntimeError, match="store read failed"):
                await mgr.cancel_for_parent(_PARENT)
            await _settle(mgr)

            assert not mgr._agents[running.id].done
            assert mgr._taskq.state_of(waiting.id) == model.QUEUED
    finally:
        await mgr.cancel_all()
        _close(mgr)


# ── a stop that fails on one row ─────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_row_that_could_not_be_unqueued_fails_the_stop_and_reaps_nothing(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """That row is still waiting: the rest are stopped, but the call raises
    before the running sweep, so no slot is freed for the pump to start it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            running = mgr.spawn("running", parent_session_key=_PARENT)
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
            events = _record(mgr)
            real = mgr._unqueue

            def flaky(agent_id: str, *a: Any, **kw: Any) -> Any:
                if agent_id == rows[0].id:
                    raise RuntimeError("unqueue failed")
                return real(agent_id, *a, **kw)

            monkeypatch.setattr(mgr, "_unqueue", flaky)
            with pytest.raises(RuntimeError, match="unqueue failed"):
                await mgr.cancel_for_parent(_PARENT)
            await _settle(mgr)

            done = {i for etype, _k, i, _x in events if etype == "subagent_done"}
            assert done == {rows[1].id}
            assert not mgr._agents[running.id].done
            assert mgr._taskq.state_of(rows[0].id) == model.QUEUED
            assert rows[0].id not in mgr._tasks
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_row_whose_report_failed_still_counts_as_stopped(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(3)]
            await _settle(mgr)
            events = _record(mgr)
            real = mgr._report_queued_stop

            def flaky(params: dict, *a: Any, **kw: Any) -> Any:
                if params.get("_preassigned_id") == rows[1].id:
                    raise RuntimeError("report failed")
                return real(params, *a, **kw)

            monkeypatch.setattr(mgr, "_report_queued_stop", flaky)
            assert await mgr.cancel_for_parent(_PARENT) == (0, 3)
            await _settle(mgr)

        done = {i for etype, _k, i, _x in events if etype == "subagent_done"}
        assert done == {rows[0].id, rows[2].id}
        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert _depths(events)[-1] == {"queued": 0}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_retried_queued_stop_report_adds_no_depth_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued-stop report that failed is retried from its retained snapshot,
    which keeps ``queued``: the retry is still the stop's terminal, not a run's."""
    mgr = await _manager(monkeypatch, pump_off_loop=False)
    try:
        parent, owner = _PARENT, "stage-1"
        info = SubagentInfo(
            id="q-retry",
            task="t",
            parent_session_key=parent,
            _stage_boundary_owner=owner,
            batch_id="b1",
            user_stopped=True,
            queued=True,
            done=True,
        )
        mgr._latch_report_failure(info)
        events = _record(mgr)

        assert await mgr._redeliver_boundary_report_payloads(parent, owner) is True
        await _settle(mgr)

        assert _kinds(events) == ["subagent_done"]
    finally:
        _close(mgr)


# ── after every exit, the parent is told what the store holds ────────────────


async def _end_run(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Finish a parked run the way its own ``finally`` would, report included."""
    info.result = "ok"
    await _free_slot(mgr, info, drain=False)
    await mgr._report_terminal(
        info,
        source="test",
        injection_timeout_reason="delivery timed out",
        mark_delivered_on_success=False,
    )


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@PUMP_MODES
@pytest.mark.parametrize("path", ["complete", "cancel", "stop_all", "stage_cancel", "parent_end"])
async def test_after_each_exit_the_published_depth_equals_the_store_count(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, path: str
) -> None:
    """Whatever ends, the last depth the parent is told is what the store holds,
    and what the dashboard's card then shows.

    The parent holds a running run, a resident run parked on its resume entry,
    a row in the window and one the memory gate deferred: every place a
    waiting row, or something that looks like one, can sit. The pump is held
    closed for the exit, as the gateway holds it before its memory barrier, so
    a drain's own emit cannot stand in for the exit's: the frame checked is
    one the exit itself sent. A start the pump makes is checked the same way
    by :func:`test_a_row_the_pump_starts_costs_at_most_two_exact_frames`.
    """
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        _resident, running = await _resident_with_resume_entry(mgr)
        with patch.object(SubagentManager, "_run", new=_park):
            waiting = mgr.spawn("waiting", parent_session_key=_PARENT)
            (deferred,) = _defer(mgr, 1)
            await _settle(mgr)
        assert waiting.id in {q.get("_preassigned_id") for q in mgr._queue}
        assert mgr.queued_count_for(_PARENT) == 2
        mgr._queue_dispatch_held = True
        events = _record(mgr)

        if path == "complete":
            await _end_run(mgr, mgr._agents[running.id])
        elif path == "cancel":
            assert await mgr.cancel(deferred.id) is True
        elif path == "stop_all":
            await mgr.cancel_for_parent(_PARENT)
        elif path == "stage_cancel":
            await mgr.cancel_for_boundary(_PARENT, "owner-of-nothing")
        else:
            selected = mgr.snapshot_teardown_children(_PARENT)
            await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="test")
        await _settle(mgr)

        published = _depths(events)
        assert published, "the exit never told the parent its depth"
        assert _card(events) == published[-1]["queued"] == _store_waiting(mgr)
        if path == "stop_all":
            assert published[-1] == {"queued": 0}
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


# ── every request is answered, and only once ─────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_request_while_a_frame_is_being_sent_is_answered(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A consumer that suspends while it takes the frame: a stop landing then
    must still get a frame read after it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    sending, release = asyncio.Event(), asyncio.Event()
    frames: list[int] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype == "subagent_queued" and info.parent_session_key == _PARENT:
            frames.append(extra["queued"])
            if len(frames) == 1:
                sending.set()
                await asyncio.wait_for(release.wait(), 5)

    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
            mgr._on_event = on_event
            mgr._emit_queue_depth(_PARENT)
            await asyncio.wait_for(sending.wait(), 5)
            for row in rows:
                assert await mgr.cancel(row.id) is True
            release.set()
            await _settle(mgr)

        assert frames[0] == 2
        assert frames[-1] == 0 == _store_waiting(mgr)
    finally:
        release.set()
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_requests_coalesced_before_the_read_starts_are_still_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In one loop step: a request starts a burst, a store write is posted, and
    the depth is asked for again. The burst has not read yet, and the posted
    write is queued on the writer thread ahead of the burst's first read, so
    that one read answers both requests: none is discarded and re-read."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        (row,) = _defer(mgr, 1)
        await _settle(mgr)
        events = _record(mgr)
        admission = mgr._admission
        real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
        reads: list[str] = []

        async def counted(self: Any, parent: str) -> int | None:
            reads.append(parent)
            return await real(self, parent)

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", counted)

        mgr._emit_queue_depth(_PARENT)
        admission._post_store_write(
            mgr._taskq, "test cancel", admission.taskq_cancel_queued, row.id
        )
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _store_waiting(mgr) == 0
        assert _depths(events) == [{"queued": 0}]
        assert reads == [_PARENT], "one read answers the whole coalesced burst"
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_steady_stream_of_requests_cannot_withhold_every_frame(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """Every read is overlapped by a request, so each is discarded and read
    again; once frames have been withheld for the cap, the read is published
    anyway and the burst reads again behind it. The burst's clock is the
    test's: each read "takes" 0.3 s, so the fourth one crosses the 1.0 s cap.
    That overlapped 0 read is published bare and keeps the parent's label (the
    overlapping request may have written it); the read behind it forgets it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    clock = {"now": 0.0}
    monkeypatch.setattr(run_mod, "_queue_depth_clock", lambda: clock["now"])
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    reads: asyncio.Queue[asyncio.Event] = asyncio.Queue()

    async def held(self: Any, parent: str) -> int | None:
        gate = asyncio.Event()
        reads.put_nowait(gate)
        await asyncio.wait_for(gate.wait(), 5)
        return await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", held)
    try:
        events = _record(mgr)
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        mgr._emit_queue_depth(_PARENT)
        for _ in range(4):
            gate = await asyncio.wait_for(reads.get(), 5)
            assert _depths(events) == []
            mgr._emit_queue_depth(_PARENT)  # lands during this read
            clock["now"] += 0.3
            gate.set()
        # The fourth read was published although a request overlapped it, and
        # that request is answered by one more read.
        gate = await asyncio.wait_for(reads.get(), 5)
        assert _depths(events) == [{"queued": 0}]
        assert mgr._queue_wait[_PARENT] == _STALE_WAIT, "an overlapped 0 keeps the label"
        gate.set()
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}, {"queued": 0}]
        assert _PARENT not in mgr._queue_wait, "a 0 no request overlapped forgets it"
    finally:
        _close(mgr)


# ── the delayed re-read: one per parent, answered by any frame ──────────────


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_failing_requests_share_one_retry_chain(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        calls = _fail_chip_reads(monkeypatch, times=1000)
        with caplog.at_level("WARNING", logger=_DEPTH_LOGGER):
            for _ in range(3):
                mgr._emit_queue_depth(_PARENT)
                await settle_depth_emits(mgr)
            await _until(
                lambda: _warned(caplog, _DEPTH_LOGGER, "unreadable after"),
                "the last retry gave up, at WARNING",
            )
        await _settle(mgr)

        assert len(calls) == 3 + _QUEUE_DEPTH_RETRIES
        assert _warned(caplog, _DEPTH_LOGGER, "unreadable after") == 1
        assert mgr._queue_depth_retries == {}
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_has_at_most_one_armed_retry(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        _fail_chip_reads(monkeypatch, times=1000)
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await settle_depth_emits(mgr)
        armed = mgr._queue_depth_retries[_PARENT]
        mgr._emit_queue_depth(_PARENT, "wave-b")
        await settle_depth_emits(mgr)

        assert mgr._queue_depth_retries[_PARENT] is armed
        assert not armed.handle.cancelled()
        assert armed.batch_ids == {"wave-a", "wave-b"}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_frame_published_first_disarms_the_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        events = _record(mgr)
        _fail_chip_reads(monkeypatch, times=1)
        mgr._emit_queue_depth(_PARENT)
        await settle_depth_emits(mgr)
        handle = mgr._queue_depth_retries[_PARENT].handle

        with caplog.at_level("WARNING", logger=_DEPTH_LOGGER):
            mgr._emit_queue_depth(_PARENT)
            await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert handle.cancelled()
        assert mgr._queue_depth_retries == {}
        # The disarm is a sanctioned cancel of a manager-owned timer, so the
        # chokepoint must recognize it rather than log a missing marker.
        assert _warned(caplog, _DEPTH_LOGGER, "WITHOUT a terminal marker") == 0
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_shutdown_cancels_an_armed_retry(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)
    try:
        _fail_chip_reads(monkeypatch, times=1)
        mgr._emit_queue_depth(_PARENT)
        await settle_depth_emits(mgr)
        handle = mgr._queue_depth_retries[_PARENT].handle

        await mgr.cancel_all()

        assert handle.cancelled()
        assert mgr._queue_depth_retries == {}
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_fresh_request_restores_the_retry_budget(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A request that joins a burst on its last retry gets retries of its own:
    otherwise a card stopped during a flapping outage is never repaired."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    outage = {"on": True}
    real = SpawnAdmissionCoordinator.taskq_chip_overflow_async
    reading, release = asyncio.Event(), asyncio.Event()

    async def flapping(self: Any, parent: str) -> int | None:
        if not release.is_set():
            reading.set()
            await asyncio.wait_for(release.wait(), 5)
        return None if outage["on"] else await real(self, parent)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_chip_overflow_async", flapping)
    try:
        events = _record(mgr)
        mgr._run_events._request_queue_depth(_PARENT, set(), attempt=_QUEUE_DEPTH_RETRIES)
        await asyncio.wait_for(reading.wait(), 5)
        mgr._emit_queue_depth(_PARENT)
        release.set()
        await settle_depth_emits(mgr)
        assert _PARENT in mgr._queue_depth_retries
        outage["on"] = False
        await _until(lambda: bool(_depths(events)), "the retry published")

        assert _depths(events) == [{"queued": 0}]
    finally:
        release.set()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_frame_names_a_wave_only_when_every_request_named_it(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        batches: list[str] = []

        async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
            if etype == "subagent_queued":
                batches.append(info.batch_id)

        mgr._on_event = on_event
        mgr._emit_queue_depth(_PARENT, "wave-a")
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await _settle(mgr)
        mgr._emit_queue_depth(_PARENT, "wave-a")
        mgr._emit_queue_depth(_PARENT, "wave-b")
        await _settle(mgr)

        assert batches == ["wave-a", ""]
    finally:
        _close(mgr)


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
            await asyncio.wait_for(release.wait(), 5)
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
                await asyncio.wait_for(entered.wait(), 5)
                call.cancel()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(call, 5)
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
        await asyncio.wait_for(policy_release.wait(), 5)
        return fn(*a, **kw)

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        if etype != "subagent_queued" or info.parent_session_key != _PARENT:
            return
        frames.append(dict(extra))
        if armed["on"]:
            armed["on"] = False
            publish_hold.set()
            await asyncio.wait_for(publish_release.wait(), 5)

    try:
        with (
            patch.object(SubagentManager, "_run", new=AsyncMock()),
            patch("asyncio.to_thread", new=slow_policy),
        ):
            first = mgr.spawn("t0", parent_session_key=_PARENT)
            second = mgr.spawn("t1", parent_session_key=_PARENT)
            await settle_store_writes(mgr._taskq, rounds=4)
            await settle_depth_emits(mgr)
            mgr._on_event = on_event
            armed["on"] = True
            mgr._emit_queue_depth(_PARENT)
            await asyncio.wait_for(publish_hold.wait(), 5)
            assert frames[-1]["queued"] == 1
            await _free_slot(mgr, first)
            await asyncio.wait_for(policy_hold.wait(), 5)
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
            await store.run(store.defer, row.id, store.now() - 30, reason="test")
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
            await asyncio.wait_for(release.wait(), 5)
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
            await asyncio.wait_for(entered.wait(), 5)
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
            await asyncio.wait_for(call, 5)
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
                await asyncio.wait_for(release.wait(), 5)

    try:
        mgr._on_event = on_event
        mgr._emit_queue_depth(_PARENT, "wave-a")
        await asyncio.wait_for(sending.wait(), 5)
        mgr._emit_queue_depth(_PARENT, second)
        release.set()
        await _settle(mgr)

        assert batches == ["wave-a", second]
    finally:
        release.set()
        _close(mgr)
