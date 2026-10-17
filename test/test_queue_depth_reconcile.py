"""The queued count stays exact: every settle point re-publishes it, coalesced.

``subagent_queued`` is pushed, and the dashboard otherwise resets its count only
from a reconnect's snapshot. A frame it missed, or one that arrived after the
frame that superseded it, leaves "N waiting to start" and the old wait reason on
the card after every run has finished. So the authoritative depth is re-published
at every point a wave settles -- each terminal report of a run that started, each
stop of a waiting row, and a Stop all that stopped nothing -- and
the emit is coalesced per parent, so a bulk stop costs about one frame. A read
answers every request made before it started, because a posted store write is
queued on the writer thread by the call that posts it; the count covers
unstarted spawns only; and a store that cannot be read publishes nothing rather
than a false 0, then retries.

This module pins the store open under a host freeze, every settle point, the cost of a
burst and the bulk stop on the writer thread. What the count reads (unstarted spawns only,
behind earlier writes, an unreadable store, a stop whose store pass fails) lives in
``test_queue_depth_reconcile_reads.py``, the depth after every exit in
``test_queue_depth_reconcile_exits.py``, request answering and the delayed re-read in
``test_queue_depth_reconcile_requests.py``, and the rows an accept holds in
``test_queue_depth_reconcile_accepts.py``; the shared manager, recorder and waits in
``queue_depth_reconcile_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open
from queue_depth_reconcile_helpers import (
    _ADMISSION_LOGGER,
    _DEPTH_LOGGER,
    _PARENT,
    _SETTLE_SECS,
    _STALE_WAIT,
    PUMP_MODES,
    Event,
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
    _until,
    _warned,
)

import kiro_crew.subagent as subagent_mod
import kiro_crew.subagent_manager.run as run_mod
import kiro_crew.taskq.store as store_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.taskq import model
from kiro_crew.taskq.store import TaskStore, TaskStoreUnavailable
from kiro_crew.testing.wait import await_until
from kiro_crew.testing.wait import finished as _finished

pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _kinds(events: list[Event]) -> list[str]:
    return [etype for etype, _key, _id, _extra in events]


# ── a host freeze during the store open is not a failure ────────────────────

#: A stall longer than five seconds and inside the 5.5 to 6.9 s ones measured on
#: a hosted Windows runner (see ``overload_fakes.STORE_OPEN_CEILING_SECS``).
_HOST_FREEZE_SECS = 6.0


def _freeze_the_open(monkeypatch: pytest.MonkeyPatch, secs: float) -> threading.Event:
    """Hold the manager's off-loop store open on its worker thread for *secs*.

    The worker thread stops, as every thread on the host does while a runner
    freezes; the open then runs as usual. The returned event ends the hold early.
    """
    real = SubagentManager._open_taskq
    release = threading.Event()

    def frozen(self: SubagentManager) -> Any:
        release.wait(secs)
        return real(self)

    monkeypatch.setattr(SubagentManager, "_open_taskq", frozen)
    return release


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_store_open_held_up_by_a_host_freeze_still_attaches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _freeze_the_open(monkeypatch, _HOST_FREEZE_SECS)
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        assert mgr._taskq is not None
        mgr._queue_wait[_PARENT] = dict(_STALE_WAIT)
        events = _record(mgr)

        assert await mgr.cancel_for_parent(_PARENT) == (0, 0)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_store_open_that_never_returns_fails_by_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _freeze_the_open(monkeypatch, _SETTLE_SECS)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=1)
    try:
        with pytest.raises(AssertionError, match="task store did not open within 0.1s"):
            await wait_taskq_open(mgr, ceiling=0.1)
    finally:
        release.set()
        await wait_taskq_open(mgr)
        _close(mgr)


#: A store-open wait with a deadline of its own: ``asyncio.wait_for`` around a
#: manager's ``wait_taskq_ready()``.
_RAW_OPEN_WAIT = re.compile(r"wait_for\(\s*[\w.]+\.wait_taskq_ready\(\)")


def test_every_store_open_wait_in_the_suite_uses_the_shared_ceiling() -> None:
    """A test that bounds the off-loop store open must do it through
    ``overload_fakes.wait_taskq_open``, whose ceiling sits above the hosted
    Windows runner's stalls. A raw ``wait_for`` with a smaller deadline times
    out on an open that would have attached; an unbounded await is fine."""
    here = Path(__file__).parent
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(here.glob("*.py"))
        if path.name != "overload_fakes.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _RAW_OPEN_WAIT.search(line)
    ]
    assert offenders == [], offenders


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
@PUMP_MODES
async def test_a_lifecycle_edge_that_agrees_with_the_published_depth_sends_nothing(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A child start re-derives its parent's held depth (the published-depth
    heal), but a count that is already right is not published a second time."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        _defer(mgr, 2)
        await _settle(mgr)
        assert mgr.published_queued_depths() == {_PARENT: 2}
        events = _record(mgr)

        child = SubagentInfo(id="c1", task="t", parent_session_key=_PARENT)
        await mgr._fire_event("subagent_spawn", child, {})
        await _settle(mgr)

        assert _depths(events) == []
        assert mgr.published_queued_depths() == {_PARENT: 2}
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_heal_that_answers_an_armed_retry_still_publishes_the_frame_it_owed(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A heal read disarms the parent's pending re-read, so it must send the
    frame that re-read owed even when the count is unchanged: here a new wait
    label at the same depth, which would otherwise never reach the card."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    monkeypatch.setattr(run_mod, "_QUEUE_DEPTH_RETRY_SECS", 60.0)  # the heal answers it
    try:
        _defer(mgr, 2)
        await _settle(mgr)
        assert mgr.published_queued_depths() == {_PARENT: 2}
        events = _record(mgr)
        _fail_chip_reads(monkeypatch, times=1)
        mgr._queue_wait[_PARENT] = {"reason": "adaptive_cap_zero"}
        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)
        assert _depths(events) == [] and _PARENT in mgr._queue_depth_retries

        child = SubagentInfo(id="c1", task="t", parent_session_key=_PARENT)
        await mgr._fire_event("subagent_spawn", child, {})
        await _settle(mgr)

        assert _depths(events) == [{"queued": 2, "reason": "adaptive_cap_zero"}]
        assert _PARENT not in mgr._queue_depth_retries
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
    # Ten unsettled dedicated starts at once owe the floor plus ten unlearned
    # start prices, more than ``healthy_host_memory``'s 8 GB host has, so the
    # memory guard would park four of them. This test is about the frames, not
    # the guard: it reads a host big enough for ten, compared honestly.
    monkeypatch.setattr(
        subagent_mod,
        "check_memory_available",
        lambda min_gb=None, path=None: (64.0 >= (min_gb or 0.0), 64.0),
    )
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


# ── a bulk stop cancels its rows on the writer thread ────────────────────────


def _contend_row_cancels(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[threading.Event, threading.Event, list[bool]]:
    """Each row's store cancel taken OFF the loop waits for the loop to release
    it, the way a contended SQLite lock holds a writer.

    Returns ``(entered, release, on_loop)``. A cancel taken ON the loop could
    never be released -- the loop is the thing waiting -- so it does not wait:
    it is recorded in *on_loop*, which records where every cancel ran, and goes
    straight on. The off-loop wait has a deadline only as a backstop well inside
    the tests' ``timeout(30)``; nothing is decided by it.
    """
    entered, release = threading.Event(), threading.Event()
    on_loop: list[bool] = []
    real = SpawnAdmissionCoordinator.taskq_cancel_queued

    def contended(self: Any, agent_id: str, **kw: Any) -> Any:
        here = TaskStore._on_running_loop_thread()
        on_loop.append(here)
        if not here:
            entered.set()
            assert release.wait(timeout=10), "the loop never released a contended cancel"
        return real(self, agent_id, **kw)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_cancel_queued", contended)
    return entered, release, on_loop


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_stop_all_over_store_only_rows_never_blocks_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop all over rows held only by the store: the loop keeps running while
    their cancels wait on the database, and the card still ends at 0.

    A store cancel on the loop holds it for the busy timeout, once per row,
    whenever the store is contended (a measured 10 s for one bulk stop). The
    probe makes every off-loop cancel wait until the LOOP releases it, so the
    stop can only finish if the loop kept turning; the strict guard and
    ``loop_thread_calls`` catch any store call that ran on the loop.
    """
    rows = 6
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        _defer(mgr, rows)
        await _settle(mgr)
        events = _record(mgr)
        entered, release, on_loop = _contend_row_cancels(monkeypatch)
        monkeypatch.setenv(store_mod.STRICT_ON_LOOP_ENV, "1")
        before = mgr._taskq.loop_thread_calls

        stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
        await _until(entered.is_set, "a row's store cancel started")
        release.set()
        assert await await_until(stop, what="the stop finishing") == (0, rows)
        await _settle(mgr)
        # The checks below read the store on the loop themselves.
        monkeypatch.delenv(store_mod.STRICT_ON_LOOP_ENV)

        assert on_loop and not any(on_loop), "a row's store cancel ran on the event loop"
        assert mgr._taskq.loop_thread_calls == before, "the stop called the store on the loop"
        assert _kinds(events).count("subagent_done") == rows
        assert _depths(events)[-1] == {"queued": 0}
        assert _store_waiting(mgr) == 0
    finally:
        _close(mgr)


def _parent_window(mgr: SubagentManager, parent: str = _PARENT) -> list[str]:
    """The unstarted window entries *parent* has."""
    return [
        str(p.get("_preassigned_id") or "")
        for p in mgr._queue
        if p.get("parent_session_key") == parent and not p.get("_resume_id")
    ]


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stop_all_queues_its_cancels_and_drops_its_entries_before_it_suspends(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """By the time Stop all first yields, its cancel job is queued and the
    window holds none of the parent's rows: a stagger timer or a pump pass that
    runs in that yield finds nothing of this parent's to start."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(3)]
            await _settle(mgr)
        assert sorted(_parent_window(mgr)) == sorted(r.id for r in rows)
        posted: list[list[str]] = []
        real = SpawnAdmissionCoordinator.taskq_post_cancel_queued

        def post(self: Any, agent_ids: Any) -> Any:
            posted.append(list(agent_ids))
            return real(self, agent_ids)

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_post_cancel_queued", post)

        stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
        await asyncio.sleep(0)

        assert posted and sorted(posted[0]) == sorted(r.id for r in rows)
        assert _parent_window(mgr) == []
        assert await await_until(stop, what="the stop finishing") == (0, 3)
        await _settle(mgr)
        assert _store_waiting(mgr) == 0
        await mgr.cancel_all()
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_refill_queued_before_stop_all_windows_none_of_the_parents_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refill fetch that is already on the writer thread when Stop all is
    clicked runs BEFORE the stop's cancels, so it reads every one of the
    parent's rows as waiting. Windowed, the rows the stop is cancelling would
    come back as entries for cancelled rows, and the store-only ones would be
    left out of the stop's pending read (it skips windowed rows) and stay
    waiting after the stop reported done."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        mgr._taskq._window = 2
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(4)]
            await _settle(mgr)
        windowed = _parent_window(mgr)
        assert len(windowed) == 2 and _store_waiting(mgr) == 4
        events = _record(mgr)
        mgr._taskq._window = 10
        entered, go = threading.Event(), threading.Event()
        real = SpawnAdmissionCoordinator._refill_fetch

        def held(self: Any, *a: Any, **kw: Any) -> Any:
            entered.set()
            assert go.wait(timeout=10), "the test never released the refill fetch"
            return real(self, *a, **kw)

        monkeypatch.setattr(SpawnAdmissionCoordinator, "_refill_fetch", held)
        refill = asyncio.ensure_future(mgr._admission.taskq_refill_window_async())
        await _until(entered.is_set, "the refill fetch started on the writer thread")

        stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
        for _ in range(3):
            await asyncio.sleep(0)
        go.set()
        assert await await_until(stop, what="the stop finishing") == (0, 4)
        await await_until(refill, what="the refill finishing")
        await _settle(mgr)

        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert _parent_window(mgr) == []
        assert _store_waiting(mgr) == 0
        assert _kinds(events).count("subagent_done") == 4
        assert _depths(events)[-1] == {"queued": 0}
        assert mgr._stopping_parents == {}
        await mgr.cancel_all()
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_row_spawned_after_stop_alls_read_starts_once_the_stop_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row the parent spawns after Stop all read its store-only rows is
    neither pass's to cancel, and a refill windows none of that parent's rows
    while the stop runs (``_stopping_parents``). A slot freed meanwhile (here
    another parent's run is stopped) runs its whole pump pass under that fence,
    so nothing is due to bring the row in once the stop ends. The stop's end
    runs one more pass: the fence holds a row back for the stop's span, never
    past it."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        mgr._taskq._window = 2
        with patch.object(SubagentManager, "_run", new=_park):
            holder = mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(4)]
            await _settle(mgr)
            assert len(_parent_window(mgr)) == 2
            read, go = asyncio.Event(), asyncio.Event()
            real = SpawnAdmissionCoordinator.taskq_pending_ids_for_async

            async def held_read(self: Any, parent: str, *a: Any, **kw: Any) -> Any:
                ids = await real(self, parent, *a, **kw)
                read.set()
                await go.wait()
                return ids

            monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", held_read)
            stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
            await await_until(read, what="the stop reading its store rows")
            late = mgr.spawn("late", parent_session_key=_PARENT)
            assert late.queued and late.id not in _parent_window(mgr)
            # The slot frees while the stop holds the fence, and that pump pass
            # windows nothing of this parent's.
            assert await _finished(mgr.cancel(holder.id), "the holder's cancel") is True
            await _until(
                lambda: mgr._drain_task is not None and mgr._drain_task.done(),
                "the freed slot's pump pass finished",
            )
            assert late.id not in mgr._tasks and late.id not in _parent_window(mgr)
            go.set()
            assert await await_until(stop, what="the stop finishing") == (0, 4)
            await _until(lambda: late.id in mgr._tasks, "the late row starts after the stop")

        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert mgr._stopping_parents == {}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_stop_all_whose_post_raises_leaves_the_window_whole(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The cancel job is queued BEFORE the entries are dropped: a post that
    raises cancelled nothing, so every row stays in the window, waiting, and
    the stop reports the failure instead of rows it never stopped."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
        events = _record(mgr)

        def refused(self: Any, agent_ids: Any) -> Any:
            raise RuntimeError("the writer is gone")

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_post_cancel_queued", refused)
        with pytest.raises(RuntimeError, match="the writer is gone"):
            await mgr.cancel_for_parent(_PARENT)
        await _settle(mgr)

        assert sorted(_parent_window(mgr)) == sorted(r.id for r in rows)
        assert all(mgr._taskq.state_of(r.id) == model.QUEUED for r in rows)
        assert "subagent_done" not in _kinds(events)
        assert mgr._stopping_parents == {}
        await mgr.cancel_all()
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_stop_all_reposts_a_row_cancel_that_did_not_land(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool, caplog: pytest.LogCaptureFixture
) -> None:
    """A store outage during Stop all: the window entry is dropped and the stop
    is published, so the cancel that did NOT land is re-posted rather than
    assumed (a row left ``queued`` is dispatchable by the next incarnation),
    and its report keeps the settle, because nothing wrote the row's state."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            row = mgr.spawn("work the user then stops", parent_session_key=_PARENT)
            await _settle(mgr)
        assert _parent_window(mgr) == [row.id]
        store = mgr._taskq
        attempts: list[str] = []
        real_cancel = store.cancel

        def cancel(task_id: str, **kw: Any) -> Any:
            if task_id == row.id:
                attempts.append(task_id)
                if len(attempts) == 1:
                    raise TaskStoreUnavailable("the store could not be reached")
            return real_cancel(task_id, **kw)

        finished: list[str] = []
        real_finish = store.finish

        def finish(task_id: str, *a: Any, **kw: Any) -> Any:
            finished.append(task_id)
            return real_finish(task_id, *a, **kw)

        monkeypatch.setattr(store, "cancel", cancel)
        monkeypatch.setattr(store, "finish", finish)

        with caplog.at_level(logging.DEBUG, logger=_ADMISSION_LOGGER):
            assert await mgr.cancel_for_parent(_PARENT) == (0, 1)
            await _settle(mgr)

        assert _parent_window(mgr) == []
        assert store.state_of(row.id) == model.CANCELLED
        assert attempts == [row.id, row.id], "the cancel that did not land was not re-posted"
        assert row.id in finished, "the report skipped the settle for a cancel that did not land"
        # The re-posted cancel landed first, so that settle's refusal lost
        # nothing; the re-post has already warned once.
        assert _warned(caplog, _ADMISSION_LOGGER, "did not commit") == 0
        await mgr.cancel_all()
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_stop_all_cancelled_mid_batch_still_reports_every_row_it_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The batch cancel is already queued on the writer thread when the request
    is cancelled, so its rows end cancelled either way, and each one's stop is
    still reported: a row cancelled in the store with no terminal report would
    leave its wave waiting for a completion that never comes."""
    rows = 3
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        _defer(mgr, rows)
        await _settle(mgr)
        events = _record(mgr)
        entered, release, _on_loop = _contend_row_cancels(monkeypatch)

        stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
        await _until(entered.is_set, "a row's store cancel started")
        stop.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
        await _settle(mgr)

        assert _kinds(events).count("subagent_done") == rows
        assert _depths(events)[-1] == {"queued": 0}
        assert _store_waiting(mgr) == 0
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_shutdown_during_stop_all_drains_the_reports_its_applier_spawns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shutdown that starts while Stop all's cancel job is still on the writer
    thread drains the queued-stop reports the job's applier spawns once it
    answers. Its first snapshot of the reports holds only the applier, so a
    drain that never re-read them returned as soon as the applier did, with
    every row cancelled in the store and its report still pending as the loop
    closed."""
    rows = 3
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        infos = _defer(mgr, rows)
        await _settle(mgr)
        events = _record(mgr)
        entered, release, _on_loop = _contend_row_cancels(monkeypatch)

        stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
        await _until(entered.is_set, "a row's store cancel started")
        drained = asyncio.Event()
        real_wait = asyncio.wait

        async def wait(tasks: Any, **kw: Any) -> Any:
            # The job is released only once the report drain is waiting on
            # it, so the drain's first snapshot is the applier alone.
            if tasks and all(t in mgr._report_tasks for t in tasks):
                drained.set()
            return await real_wait(tasks, **kw)

        shutdown = asyncio.ensure_future(mgr.cancel_all())
        with patch.object(asyncio, "wait", new=wait):
            await await_until(drained, what="the report drain waiting on the job")
            release.set()
            await await_until(shutdown, what="the shutdown finishing")

        assert [t for t in mgr._report_tasks if not t.done()] == []
        assert _kinds(events).count("subagent_done") == rows
        assert all(mgr._taskq.state_of(info.id) == model.CANCELLED for info in infos)
        await await_until(stop, what="the stop finishing")
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_single_cancel_during_stop_all_joins_the_batch_and_reports_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The user stops one queued card while Stop all's cancel job is on the
    writer thread. That row's window entry is already gone and it has no
    ``_agents`` record. A single cancel that took it for a store-only row
    would cancel it on the loop, land first and report it, and the batch,
    whose own cancel then finds the row already cancelled, would re-post that
    cancel with a WARNING and report the row a second time: two
    ``subagent_done`` and two completions for the parent. So the single
    cancel joins the batch's answer, and the row is reported once."""
    caplog.set_level(logging.WARNING, logger=_DEPTH_LOGGER)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(3)]
            await _settle(mgr)
            events = _record(mgr)
            entered, release, on_loop = _contend_row_cancels(monkeypatch)

            stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
            await _until(entered.is_set, "the batch's cancel job is on the writer thread")
            single = asyncio.ensure_future(mgr.cancel(rows[-1].id))
            # One loop turn runs the single cancel up to its decision: it either
            # finishes on the loop or waits on the batch it joined.
            await asyncio.sleep(0)
            release.set()
            assert await await_until(single, what="the single cancel finishing") is True
            assert await await_until(stop, what="the stop finishing") == (0, 3)
            await _settle(mgr)

        done = [i for etype, _k, i, _x in events if etype == "subagent_done"]
        assert sorted(done) == sorted(r.id for r in rows)
        assert True not in on_loop
        assert _warned(caplog, _DEPTH_LOGGER, "re-posting the cancel") == 0
        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert mgr._batched_stops == {}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("reports_first", ["teardown", "batch"])
async def test_a_teardown_cancel_queued_before_stop_all_reports_its_row_once(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, reports_first: str
) -> None:
    """A parent-end teardown's cancel of a queued row is already on the writer
    thread when Stop all pops that row's window entry, so the teardown's
    cancel lands and the batch's own cancel of the row comes back empty. Both
    stops then hold the row, and each builds a fresh ``SubagentInfo`` to
    report it, so the record's one-shot claim cannot stop a second report:
    the claim is taken per row instead, whichever stop reports first. One
    ``subagent_done``, and no retired child's completion injected twice.

    ``batch`` holds the teardown until the batch has applied its answers.
    The batch cannot tell then that the teardown's cancel landed, so it
    re-posts that cancel (a WARNING) and reports the row, and the teardown
    reports nothing. ``teardown`` lets the teardown report first: the batch
    finds the row reported and re-posts nothing. Stop all's count is the same
    in both orders: the row was in the window it took, so it counts once."""
    caplog.set_level(logging.WARNING, logger=_DEPTH_LOGGER)
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(2)]
            await _settle(mgr)
            events = _record(mgr)
            entered, release, _on_loop = _contend_row_cancels(monkeypatch)
            if reports_first == "batch":
                real_async = SpawnAdmissionCoordinator.taskq_cancel_queued_async

                async def after_the_batch(self: Any, agent_id: str, **kw: Any) -> Any:
                    params = await real_async(self, agent_id, **kw)
                    await _until(
                        lambda: agent_id not in mgr._batched_stops, "the batch applied first"
                    )
                    return params

                monkeypatch.setattr(
                    SpawnAdmissionCoordinator, "taskq_cancel_queued_async", after_the_batch
                )

            teardown = asyncio.ensure_future(
                mgr.cancel_for_teardown([rows[0].id], parent_session_key=_PARENT, verb="reset")
            )
            await _until(entered.is_set, "the teardown's cancel is on the writer thread")
            stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
            await _until(lambda: rows[0].id in mgr._batched_stops, "the batch popped the row")
            release.set()
            await await_until(teardown, what="the teardown finishing")
            stopped = await await_until(stop, what="the stop finishing")
            await _settle(mgr)

        done = [i for etype, _k, i, _x in events if etype == "subagent_done"]
        assert sorted(done) == sorted(r.id for r in rows)
        # The batch took both rows from the window, so it counts both,
        # whichever stop reported the contested one.
        assert stopped == (0, 2)
        reposted = _warned(caplog, _DEPTH_LOGGER, "re-posting the cancel")
        assert reposted == (1 if reports_first == "batch" else 0)
        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert mgr._batched_stops == {}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_parent_end_during_stop_all_gates_the_batchs_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stop all, then reset the chat, while the batch's cancels are still on
    the writer thread. The batch has taken its rows from the window and has
    not reported them, so they are in neither ``_queue`` nor ``_agents`` and
    the teardown's snapshot names none of them; once the batch's cancels land
    its store sweep cannot name them either. The batch's report of each row
    must still be held out of the ended conversation: the parent-end gate
    covers every row the batch is cancelling for that parent, while each row
    still ends with its ``subagent_done`` and is stopped exactly once."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    release = threading.Event()
    try:
        delivered: list[str] = []

        async def on_done(info: SubagentInfo) -> None:
            delivered.append(info.id)

        mgr._on_done = on_done
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            rows = [mgr.spawn(f"t{i}", parent_session_key=_PARENT) for i in range(3)]
            await _settle(mgr)
            events = _record(mgr)
            entered, release, _on_loop = _contend_row_cancels(monkeypatch)

            stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
            await _until(entered.is_set, "the batch's cancel job is on the writer thread")
            assert all(r.id in mgr._batched_stops for r in rows)
            assert _parent_window(mgr) == []

            selected = mgr.snapshot_teardown_children(_PARENT)
            # The batch owns these rows' cancels, so the teardown cancels none.
            assert selected == ()
            teardown = asyncio.ensure_future(
                mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="reset")
            )
            release.set()
            assert await await_until(stop, what="the stop finishing") == (0, 3)
            assert await await_until(teardown, what="the teardown finishing") == 0
            await _settle(mgr)

        done = [i for etype, _k, i, _x in events if etype == "subagent_done"]
        assert sorted(done) == sorted(r.id for r in rows)
        assert not set(delivered) & {r.id for r in rows}, "a retired row reported home"
        assert all(mgr._taskq.state_of(r.id) == model.CANCELLED for r in rows)
        assert mgr._batched_stops == {} and mgr._batched_stop_parents == {}
    finally:
        release.set()
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_single_cancel_joined_to_a_batch_that_never_answers_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store closes under a Stop all's batch, cancelling its job, so the
    batch reports nothing. A single cancel that joined it returns False rather
    than waiting forever, and nothing is left filed as batched."""
    mgr = await _manager(monkeypatch, pump_off_loop=True, max_concurrent=1)
    try:
        with patch.object(SubagentManager, "_run", new=_park):
            mgr.spawn("holds the slot", parent_session_key="dash:elsewhere")
            row = mgr.spawn("t0", parent_session_key=_PARENT)
            await _settle(mgr)
            never: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
            monkeypatch.setattr(
                SpawnAdmissionCoordinator, "taskq_post_cancel_queued", lambda self, ids: never
            )

            stop = asyncio.ensure_future(mgr.cancel_for_parent(_PARENT))
            await _until(lambda: row.id in mgr._batched_stops, "the batch filed the row")
            single = asyncio.ensure_future(mgr.cancel(row.id))
            await asyncio.sleep(0)
            never.cancel()
            assert await await_until(single, what="the single cancel finishing") is False
            with pytest.raises(asyncio.CancelledError):
                await await_until(stop, what="the stop finishing")
            assert mgr._batched_stops == {}
    finally:
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
@pytest.mark.parametrize("path", ["stop_all", "parent_end", "cancel"])
async def test_a_queued_stop_skips_the_settle_its_landed_cancel_already_wrote(
    monkeypatch: pytest.MonkeyPatch,
    pump_off_loop: bool,
    path: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A landed cancel IS the row's terminal write, so the queued-stop report
    writes no second one, whichever path stopped the row. That second
    ``finish`` could only be refused (the row is already ``cancelled``) and
    cost a writer job per row; the propagation to a waiting parent is still
    owed."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        infos = _defer(mgr, 3)
        ids = {info.id for info in infos}
        await _settle(mgr)
        finished: list[str] = []
        real_finish = mgr._taskq.finish

        def finish(task_id: str, *a: Any, **kw: Any) -> Any:
            finished.append(task_id)
            return real_finish(task_id, *a, **kw)

        monkeypatch.setattr(mgr._taskq, "finish", finish)
        propagated: list[str] = []
        real_sync = SpawnAdmissionCoordinator.taskq_child_terminal
        real_async = SpawnAdmissionCoordinator.taskq_child_terminal_async

        def child_terminal(self: Any, child: SubagentInfo, state: str) -> None:
            propagated.append(child.id)
            real_sync(self, child, state)

        async def child_terminal_async(self: Any, child: SubagentInfo, state: str) -> None:
            propagated.append(child.id)
            await real_async(self, child, state)

        monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_child_terminal", child_terminal)
        monkeypatch.setattr(
            SpawnAdmissionCoordinator, "taskq_child_terminal_async", child_terminal_async
        )

        with caplog.at_level(logging.DEBUG, logger=_ADMISSION_LOGGER):
            if path == "stop_all":
                assert await mgr.cancel_for_parent(_PARENT) == (0, 3)
            elif path == "parent_end":
                stopped = await mgr.cancel_for_teardown(
                    sorted(ids), parent_session_key=_PARENT, verb="test"
                )
                assert stopped == 3
            else:
                for agent_id in sorted(ids):
                    assert await mgr.cancel(agent_id) is True
            await _settle(mgr)

        assert [i for i in finished if i in ids] == []
        assert _warned(caplog, _ADMISSION_LOGGER, "did not commit") == 0
        assert sorted(propagated) == sorted(ids)
        assert all(mgr._taskq.state_of(i) == model.CANCELLED for i in ids)
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_refused_settle_is_a_warning_only_when_the_row_holds_another_state(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A refused terminal write whose row already holds the state it asked for
    lost nothing -- a queued stop whose re-posted cancel wrote ``cancelled``
    before its settle's ``finish`` -- so it is DEBUG. Any other refusal is a
    run whose outcome the row does not carry, and stays a WARNING."""
    mgr = await _manager(monkeypatch, pump_off_loop=False)
    try:
        (row,) = _defer(mgr, 1)
        await _settle(mgr)
        assert mgr._taskq.cancel(row.id) is not None
        report = SpawnAdmissionCoordinator.taskq_report_refused_settle

        with caplog.at_level(logging.DEBUG, logger=_ADMISSION_LOGGER):
            report(mgr._taskq, row.id, model.CANCELLED, False)
            assert _warned(caplog, _ADMISSION_LOGGER, "did not commit") == 0
            assert any("did not commit" in r.getMessage() for r in caplog.records)

            report(mgr._taskq, row.id, model.DONE, False)
            assert _warned(caplog, _ADMISSION_LOGGER, "did not commit") == 1
    finally:
        _close(mgr)
