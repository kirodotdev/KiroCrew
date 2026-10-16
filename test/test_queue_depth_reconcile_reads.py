"""What the queued-depth read counts, and when its answer lands.

The count is unstarted spawns only (never a resume entry), a re-read queues
behind a write posted before its request, an unreadable store publishes nothing
and then retries, and a stop whose store pass fails still answers the card.
Shared manager, recorder and waits live in ``queue_depth_reconcile_helpers.py``.
"""

from __future__ import annotations

import asyncio
import functools
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from queue_depth_reconcile_helpers import (
    _DEPTH_LOGGER,
    _PARENT,
    _STALE_WAIT,
    PUMP_MODES,
    _close,
    _defer,
    _depths,
    _fail_chip_reads,
    _manager,
    _park,
    _record,
    _resident_with_resume_entry,
    _settle,
    _store_waiting,
    _until,
    _warned,
)

import kiro_crew.subagent as subagent_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.run import _QUEUE_DEPTH_RETRIES
from kiro_crew.subagent_wait_reasons import QUEUED_REASON_LOW_MEMORY
from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


# ── the count is unstarted spawns only ───────────────────────────────────────


class _SharedRuntime:
    """A shared provider whose shutdown suspends (live) or returns at once (dead)."""

    def __init__(self, live: bool) -> None:
        self.live = live

    async def shutdown(self) -> None:
        if self.live:
            # flake-ok: one yield stands for a live provider's shutdown suspending; no test waits on its timing
            await asyncio.sleep(0)

    def set_keep_transcript(self, keep: bool) -> None:
        pass


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
            # flake-ok: the test resolves hand_back right after read_done is set, and the finally resolves it on any failure
            await hand_back  # the test decides when this read's result lands
        return result

    try:
        (row,) = _defer(mgr, 1)
        await _settle(mgr)
        events = _record(mgr)
        monkeypatch.setattr(TaskStore, "run", run)
        held["armed"] = True
        mgr._emit_queue_depth(_PARENT)
        await await_until(read_done, what="the held store read finishing")
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
            await await_until(release, what="the test's release")
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
        await await_until(entered, what="the hold being entered")
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
@pytest.mark.parametrize("stop", ["single-cancel", "stop-all-store-rows", "stop-all-window-rows"])
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
                rows = [
                    mgr.spawn(f"t{i}", parent_session_key=_PARENT)
                    for i in range(2 if stop == "single-cancel" else 4)
                ]
            await _settle(mgr)
            events = _record(mgr)
            calls = _fail_chip_reads(monkeypatch, times=1)

            if stop == "single-cancel":
                assert await mgr.cancel(rows[0].id) is True
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
            # flake-ok: the store read parks here until the stop awaiting it is cancelled; the raises case never reaches it
            await asyncio.Event().wait()
            return []

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", _read)
        events = _record(mgr)

        stop = asyncio.create_task(mgr.cancel_for_parent(_PARENT))
        await await_until(reached, what="the stop reaching its store read")
        if failure == "cancelled":
            stop.cancel()
        with pytest.raises(RuntimeError if failure == "raises" else asyncio.CancelledError):
            await await_until(stop, what="the stop finishing")
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
    before the running sweep, so no slot is freed for the pump to start it.

    The failure is injected at the row's store cancel, which is the part of
    an unqueue that can raise: the batch runs each row's cancel on its own, so
    one row's error neither stops the others nor reports that row stopped."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            running = mgr.spawn("running", parent_session_key=_PARENT)
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
            events = _record(mgr)
            real = SpawnAdmissionCoordinator.taskq_cancel_queued

            def flaky(self: Any, agent_id: str, *a: Any, **kw: Any) -> Any:
                if agent_id == rows[0].id:
                    raise RuntimeError("unqueue failed")
                return real(self, agent_id, *a, **kw)

            monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_cancel_queued", flaky)
            with pytest.raises(RuntimeError, match="unqueue failed"):
                await mgr.cancel_for_parent(_PARENT)
            await _settle(mgr)

            done = {i for etype, _k, i, _x in events if etype == "subagent_done"}
            assert done == {rows[1].id}
            assert not mgr._agents[running.id].done
            assert mgr._taskq.state_of(rows[0].id) == model.QUEUED
            assert rows[0].id not in mgr._tasks
            # Still waiting, so still windowed: the stop dropped every entry
            # before its batch answered, and puts back the one it did not stop.
            windowed = [q.get("_preassigned_id") for q in mgr._queue]
            assert windowed == [rows[0].id]
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
