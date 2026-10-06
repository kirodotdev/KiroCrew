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
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from overload_fakes import (
    memory_below_floor,
    mock_ctx,
    mock_sessions,
    settle_depth_emits,
    settle_store_writes,
    wait_taskq_open,
)

import kiro_crew.subagent as subagent_mod
import kiro_crew.subagent_manager.admission.taskq_bridge as taskq_bridge_mod
import kiro_crew.subagent_manager.run as run_mod
import kiro_crew.taskq.store as store_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission.types import (
    MIN_RECHECK_DELAY_SECS,
    WINDOW_ENTRY_RECOVERING,
)
from kiro_crew.subagent_manager.run import _QUEUE_DEPTH_RETRIES
from kiro_crew.subagent_wait_reasons import QUEUED_REASON_LOW_MEMORY
from kiro_crew.taskq import KIND_SUBAGENT, model
from kiro_crew.taskq.reconcile import reconcile_on_boot
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
#: taking the xdist worker down with the pytest-timeout kill. ``_until`` and the
#: emit drain inside ``_settle`` wait under it too: a Windows runner can freeze
#: every worker for several seconds, and a shorter private ceiling on any one
#: wait turns that freeze into a failure of whichever wait it lands in.
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
    await wait_taskq_open(mgr)
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
            await settle_depth_emits(mgr, timeout=max(0.0, deadline - loop.time()))
            if not mgr._queue_depth_emits:
                return
            continue
        left = deadline - loop.time()
        if left <= 0:
            raise AssertionError(f"_settle: {len(pending)} task(s) never finished: {pending}")
        await asyncio.wait(pending, timeout=left)


async def _until(check: Any, what: str, timeout: float = _SETTLE_SECS) -> None:
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
    """The store's own answer: rows still waiting to START for *parent*.

    Live runs are left out, and so is a ``recovering`` row: claimable, but a run
    that had started before the gateway restarted and is being rebuilt, not a
    spawn waiting for its first start. Read row by row rather than through the
    count the chip itself takes, so the two are compared, not restated.
    """
    live = [aid for aid, info in mgr._agents.items() if not info.done]
    rows = mgr._taskq.list_pending(KIND_SUBAGENT, session_key=parent, exclude_ids=live)
    return sum(1 for rec in rows if rec.state != model.RECOVERING)


def _defer(
    mgr: SubagentManager, count: int, parent: str = _PARENT, **spawn_kw: Any
) -> list[SubagentInfo]:
    """*count* spawns the memory gate defers: rows held by the store alone."""
    with patch.object(subagent_mod, "check_memory_available", memory_below_floor):
        with patch.object(SubagentManager, "_run", new=AsyncMock()):
            infos = [
                mgr.spawn(f"deferred-{i}", parent_session_key=parent, **spawn_kw)
                for i in range(count)
            ]
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
        assert await asyncio.wait_for(stop, 10) == (0, rows)
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
        assert await asyncio.wait_for(stop, 10) == (0, 3)
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
        assert await asyncio.wait_for(stop, 10) == (0, 4)
        await asyncio.wait_for(refill, 10)
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
            await asyncio.wait_for(read.wait(), 10)
            late = mgr.spawn("late", parent_session_key=_PARENT)
            assert late.queued and late.id not in _parent_window(mgr)
            # The slot frees while the stop holds the fence, and that pump pass
            # windows nothing of this parent's.
            assert await asyncio.wait_for(mgr.cancel(holder.id), 10) is True
            await _until(
                lambda: mgr._drain_task is not None and mgr._drain_task.done(),
                "the freed slot's pump pass finished",
            )
            assert late.id not in mgr._tasks and late.id not in _parent_window(mgr)
            go.set()
            assert await asyncio.wait_for(stop, 10) == (0, 4)
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
            await asyncio.wait_for(drained.wait(), 10)
            release.set()
            await asyncio.wait_for(shutdown, 20)

        assert [t for t in mgr._report_tasks if not t.done()] == []
        assert _kinds(events).count("subagent_done") == rows
        assert all(mgr._taskq.state_of(info.id) == model.CANCELLED for info in infos)
        await asyncio.wait_for(stop, 10)
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
            assert await asyncio.wait_for(single, 10) is True
            assert await asyncio.wait_for(stop, 10) == (0, 3)
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
            await asyncio.wait_for(teardown, 10)
            stopped = await asyncio.wait_for(stop, 10)
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
            assert await asyncio.wait_for(stop, 10) == (0, 3)
            assert await asyncio.wait_for(teardown, 10) == 0
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
            assert await asyncio.wait_for(single, 10) is False
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(stop, 10)
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


def _survivor_of_a_restart(mgr: SubagentManager, *, due: bool) -> str:
    """A run the previous gateway incarnation was executing for the parent,
    settled the way the boot reconcile settles it: ``recovering``, unleased.

    *due* backdates the reconcile so its backoff has already passed and the
    refill may hydrate the row into the window; otherwise it waits on disk.
    """
    store = mgr._taskq
    rec = model.TaskRecord(
        id="survivor",
        kind=KIND_SUBAGENT,
        session_key=_PARENT,
        params={"task": "survivor", "parent_session_key": _PARENT},
        side_effect_class=model.SIDE_EFFECT_NONE,
    )
    store.accept_one(rec)
    assert store.claim(rec.id, owner="previous-incarnation") is not None
    assert store.transition(rec.id, model.STARTING)
    assert store.transition(rec.id, model.RUNNING)
    report = reconcile_on_boot(store, now=store.now() - 3600.0 if due else None)
    assert (report.examined, report.recovering) == (1, 1), report
    survivor = store.get(rec.id)
    assert survivor is not None and survivor.state == model.RECOVERING
    return rec.id


_EXITS = [
    "complete",
    "cancel",
    "stop_all",
    "parent_end",
    "session_reset",
    "resume_grant",
    "restart_recovery",
    "restart_recovery_windowed",
]


@pytest.mark.asyncio
@pytest.mark.timeout(60)
@PUMP_MODES
@pytest.mark.parametrize("path", _EXITS)
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

    A parent end stops the deferred row too, though no snapshot can name a row
    held only by the store; a session reset does the same for the conversation
    it ends and spares the row its successor queues before the sweep runs. A
    resume grant answers the card as a settle point. A run that survived a
    restart is never a spawn waiting to start, on disk or hydrated into the
    window, so the first settle point after the boot leaves it out.
    """
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop, max_concurrent=1)
    try:
        resident, running = await _resident_with_resume_entry(mgr)
        with patch.object(SubagentManager, "_run", new=_park):
            waiting = mgr.spawn("waiting", parent_session_key=_PARENT)
            (deferred,) = _defer(mgr, 1)
            await _settle(mgr)
        assert waiting.id in {q.get("_preassigned_id") for q in mgr._queue}
        assert mgr.queued_count_for(_PARENT) == 2
        mgr._queue_dispatch_held = True
        events = _record(mgr)
        successor: SubagentInfo | None = None

        if path == "complete":
            await _end_run(mgr, mgr._agents[running.id])
        elif path == "cancel":
            assert await mgr.cancel(deferred.id) is True
        elif path == "stop_all":
            await mgr.cancel_for_parent(_PARENT)
        elif path in ("parent_end", "session_reset"):
            selected = mgr.snapshot_teardown_children(_PARENT)
            assert deferred.id not in selected
            if path == "session_reset":
                # The successor conversation under the same key queues its
                # first spawn while the reset is still tearing the old one down.
                (successor,) = _defer(mgr, 1)
            await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb=path)
        elif path == "resume_grant":
            # The pump's grant, one entry: the reservation and the durable
            # wake, inline or split across the writer thread as each pump does.
            (index,) = [i for i, q in enumerate(mgr._queue) if q.get("_resume_id") == resident.id]
            entry = mgr._queue.pop(index)
            if pump_off_loop:
                assert mgr._admission.resume_reserve(entry)
                assert await mgr._admission.resume_grant_async(entry)
            else:
                assert mgr._admission.resume_grant(entry)
        else:
            windowed = path == "restart_recovery_windowed"
            survivor = _survivor_of_a_restart(mgr, due=windowed)
            if windowed:
                # The boot pump's first refill hydrates it beside the window row.
                mgr._admission.taskq_refill_window()
                assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
            await _end_run(mgr, mgr._agents[running.id])
        await _settle(mgr)

        published = _depths(events)
        assert published, "the exit never told the parent its depth"
        assert _card(events) == published[-1]["queued"] == _store_waiting(mgr)
        if path in ("stop_all", "parent_end"):
            assert published[-1] == {"queued": 0}
        if path == "session_reset":
            assert successor is not None
            assert published[-1]["queued"] == 1
            row = mgr._taskq.get(successor.id)
            assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
            for retired in (waiting.id, deferred.id):
                gone = mgr._taskq.get(retired)
                assert gone is not None and gone.state == model.CANCELLED
        if path.startswith("restart_recovery"):
            assert published[-1]["queued"] == 2
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_stops_its_store_rows_without_reporting_them_home(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A row held only by the store is the ended conversation's work too: it is
    cancelled, and its stop is recorded without injecting into the parent."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        on_done = AsyncMock()
        mgr._on_done = on_done
        deferred = _defer(mgr, 2)
        await _settle(mgr)

        selected = mgr.snapshot_teardown_children(_PARENT)
        assert selected == ()
        stopped = await mgr.cancel_for_teardown(
            selected, parent_session_key=_PARENT, verb="destroy"
        )
        await _settle(mgr)

        assert stopped == 2
        for info in deferred:
            row = mgr._taskq.get(info.id)
            assert row is not None and row.state == model.CANCELLED
        on_done.assert_not_awaited()
        assert _store_waiting(mgr) == 0
        assert mgr._teardown_store_fences == {} and mgr._teardown_store_sweeps == []
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_sweep_never_names_a_claimed_row(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A claimed-not-started row is its claimer's, and the teardown refuses to
    cancel one (``allow_admitted=False``). So the sweep's store read leaves it
    out, unlike Stop all's: naming it would put it in the delivery gate (the
    claimer's run would then never report) and in the audit as a stopped row."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._on_done = AsyncMock()
        claimed, waiting = _defer(mgr, 2)
        await _settle(mgr)
        # Claimed past its deferral, as the pump claims it once that lapses; the
        # clock is put back so the other row stays deferred.
        store = mgr._taskq
        real_clock = store._clock
        monkeypatch.setattr(store, "_clock", lambda: real_clock() + 3600.0)
        assert store.claim(claimed.id, owner="a-claimer-in-flight") is not None
        monkeypatch.setattr(store, "_clock", real_clock)

        selected = mgr.snapshot_teardown_children(_PARENT)
        stopped = await mgr.cancel_for_teardown(
            selected, parent_session_key=_PARENT, verb="destroy"
        )
        await _settle(mgr)

        assert stopped == 1
        gone = mgr._taskq.get(waiting.id)
        assert gone is not None and gone.state == model.CANCELLED
        row = mgr._taskq.get(claimed.id)
        assert row is not None and row.state == model.ADMITTED
        assert claimed.id not in mgr._teardown_cancelled_ids
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_wall_clock_stepped_back_mid_teardown_never_sweeps_the_successors_row(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The host's clock is stepped back an hour between the snapshot and the
    successor's first spawn (an NTP correction): the store stamps the
    successor's row EARLIER than the retired conversation's. The sweep orders
    by accept, not by that stamp, so the retired row is stopped and the
    successor's is left queued."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(mgr._taskq, "_clock", lambda: clock["now"])
    try:
        (retired,) = _defer(mgr, 1)
        await _settle(mgr)
        clock["now"] += 1.0
        selected = mgr.snapshot_teardown_children(_PARENT)
        clock["now"] -= 3600.0
        (successor,) = _defer(mgr, 1)
        successor_row = mgr._taskq.get(successor.id)
        retired_row = mgr._taskq.get(retired.id)
        assert successor_row is not None and retired_row is not None
        assert successor_row.created_at < retired_row.created_at

        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="session_reset")
        await _settle(mgr)

        row = mgr._taskq.get(successor.id)
        assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
        gone = mgr._taskq.get(retired.id)
        assert gone is not None and gone.state == model.CANCELLED
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_successor_row_accepted_while_the_teardown_cancel_runs_is_spared(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The successor queues its spawn after the cancel has taken the snapshot's
    fence, while it is still stopping the named runs: the fence keeps recording
    until the sweep has read the store, so that row is spared too, and nothing
    is left recording once the cancel returns."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    real = SpawnAdmissionCoordinator.taskq_pending_ids_for_async
    late: list[SubagentInfo] = []

    async def _successor_spawns_first(self: Any, parent: str, **kwargs: Any) -> list[str]:
        late.extend(_defer(mgr, 1))
        return await real(self, parent, **kwargs)

    monkeypatch.setattr(
        SpawnAdmissionCoordinator, "taskq_pending_ids_for_async", _successor_spawns_first
    )
    try:
        (retired,) = _defer(mgr, 1)
        await _settle(mgr)
        selected = mgr.snapshot_teardown_children(_PARENT)

        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="session_reset")
        await _settle(mgr)

        (successor,) = late
        row = mgr._taskq.get(successor.id)
        assert row is not None and row.state == model.QUEUED, "the successor's row was swept"
        gone = mgr._taskq.get(retired.id)
        assert gone is not None and gone.state == model.CANCELLED
        assert mgr._teardown_store_fences == {} and mgr._teardown_store_sweeps == []
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_teardown_cancel_no_snapshot_preceded_sweeps_no_store_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sweep is fenced by the snapshot; with none taken (a recycle takes no
    snapshot, and its children deliver into the resumed conversation) no store
    row is touched."""
    mgr = await _manager(monkeypatch, pump_off_loop=True)
    try:
        (deferred,) = _defer(mgr, 1)
        await _settle(mgr)

        assert await mgr.cancel_for_teardown((), parent_session_key=_PARENT) == 0
        await _settle(mgr)

        row = mgr._taskq.get(deferred.id)
        assert row is not None and row.state == model.QUEUED
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_parent_end_sweeps_a_row_the_refill_windowed_after_its_snapshot(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """A row the retired conversation's store held when the snapshot was taken
    is swept even if the refill pulls it into the window before the sweep reads:
    the sweep keeps the window's rows in its read, and the snapshot's fence is
    what tells it from a successor's row. Left out, the row would stay queued
    and start into whatever the key serves next."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_dispatch_held = True
        rec = model.TaskRecord(
            id="store-only",
            kind=KIND_SUBAGENT,
            session_key=_PARENT,
            params={"task": "store-only", "parent_session_key": _PARENT},
            side_effect_class=model.SIDE_EFFECT_NONE,
        )
        mgr._taskq.accept_one(rec)
        assert rec.id not in {q.get("_preassigned_id") for q in mgr._queue}

        selected = mgr.snapshot_teardown_children(_PARENT)
        assert rec.id not in selected
        # The teardown's awaits let the pump refill run before the sweep reads.
        mgr._admission.taskq_refill_window()
        assert rec.id in {q.get("_preassigned_id") for q in mgr._queue}
        await mgr.cancel_for_teardown(selected, parent_session_key=_PARENT, verb="destroy")
        await _settle(mgr)

        row = mgr._taskq.get(rec.id)
        assert row is not None and row.state == model.CANCELLED
        assert rec.id not in {q.get("_preassigned_id") for q in mgr._queue}
        assert _store_waiting(mgr) == 0
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_restart_survivor_the_gate_requeues_stays_off_the_card(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The pump pops a restart survivor from the window and the gate puts it
    back (here the child reserve; a stagger tick or a cap a sibling's start
    filled does the same). The row is still unclaimed, so still ``recovering``:
    the entry the gate appends keeps the window's mark, and the card never shows
    the survivor as waiting to start, while the parent is still owed it."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    refusals: list[int] = []

    def _reserved(_self: Any) -> bool:
        # One refusal, then the pump is held: a reserve that stayed closed
        # would have the pump re-try the entry on every recheck.
        refusals.append(1)
        mgr._queue_dispatch_held = True
        return False

    monkeypatch.setattr(SpawnAdmissionCoordinator, "root_may_start", _reserved)
    try:
        mgr._queue_dispatch_held = True
        survivor = _survivor_of_a_restart(mgr, due=True)
        mgr._admission.taskq_refill_window()
        assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
        events = _record(mgr)

        with patch.object(SubagentManager, "_run", new=_park):
            mgr._queue_dispatch_held = False
            mgr._drain_queue()
            await _settle(mgr)

        assert refusals == [1], "the gate never re-checked the child reserve"
        row = mgr._taskq.get(survivor)
        assert row is not None and row.state == model.RECOVERING
        published = _depths(events)
        assert published, "the re-queue never told the parent its depth"
        assert [frame["queued"] for frame in published] == [0] * len(published)
        assert _card(events) == 0 == _store_waiting(mgr)
        (entry,) = [q for q in mgr._queue if q.get("_preassigned_id") == survivor]
        assert entry.get(WINDOW_ENTRY_RECOVERING) is True
        assert mgr.queued_count_for(_PARENT) == 1
    finally:
        mgr._queue_dispatch_held = False
        await mgr.cancel_all()
        _close(mgr)


@pytest.mark.parametrize("state", [model.QUEUED, model.RECOVERING])
def test_the_recovering_mark_comes_from_the_row_state_not_its_params(state: str) -> None:
    """The window's ``recovering`` mark is read off the row's state alone: a
    stale copy carried in a row's params never hides a spawn waiting to start
    from the chip, and a ``recovering`` row is marked whatever its params say."""
    rec = model.TaskRecord(
        id="row",
        kind=KIND_SUBAGENT,
        session_key=_PARENT,
        params={"task": "row", "parent_session_key": _PARENT, WINDOW_ENTRY_RECOVERING: True},
        state=state,
    )
    entry = SpawnAdmissionCoordinator._window_entry(rec)
    assert entry.get(WINDOW_ENTRY_RECOVERING, False) is (state == model.RECOVERING)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@PUMP_MODES
async def test_a_restart_survivor_in_the_window_is_owed_but_not_waiting_and_starts(
    monkeypatch: pytest.MonkeyPatch, pump_off_loop: bool
) -> None:
    """The guards that hold a parent's reset still count a run that survived a
    restart; the card does not; and its window entry still starts, the mark
    handed to ``spawn`` as its keyword."""
    mgr = await _manager(monkeypatch, pump_off_loop=pump_off_loop)
    try:
        mgr._queue_dispatch_held = True
        survivor = _survivor_of_a_restart(mgr, due=True)
        mgr._admission.taskq_refill_window()
        assert survivor in {q.get("_preassigned_id") for q in mgr._queue}
        events = _record(mgr)

        mgr._emit_queue_depth(_PARENT)
        await _settle(mgr)

        assert _depths(events) == [{"queued": 0}]
        assert mgr.queued_count_for(_PARENT) == 1
        assert await mgr.queued_count_for_async(_PARENT) == 1

        with patch.object(SubagentManager, "_run", new=_park):
            mgr._queue_dispatch_held = False
            mgr._drain_queue()
            await _settle(mgr)
            await _until(lambda: survivor in mgr._tasks, "the survivor started")
        assert not mgr._queue
        assert all(frame["queued"] == 0 for frame in _depths(events))
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
