"""The manager, frame recorder and waits shared by the queued-depth reconcile tests.

``test_queue_depth_reconcile.py`` and its ``_reads``, ``_exits``, ``_requests`` and
``_accepts`` siblings import these by name: a real ``SubagentManager`` on either count
path with its task store open, the recorder of the frames it emits and the depths and card
count they leave, the settle and until waits under one ceiling, the store's own count of
rows waiting to start, memory-deferred spawns, a parked run, a chip read that answers
"unreadable", and the loggers the warning counts filter on.
"""

from __future__ import annotations

import asyncio
import logging
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
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_manager.admission.types import MIN_RECHECK_DELAY_SECS
from kiro_crew.subagent_wait_reasons import QUEUED_REASON_LOW_MEMORY
from kiro_crew.taskq import KIND_SUBAGENT, model
from kiro_crew.taskq.waits import WaitRecord
from kiro_crew.testing.wait import async_wait_until

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
    await async_wait_until(check, timeout=timeout, describe=lambda: f"never happened: {what}")


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
    # flake-ok: a parked run holds its slot until the test stops it; _settle leaves the parked tasks out of what it waits for
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


async def _free_slot(mgr: SubagentManager, info: SubagentInfo, *, drain: bool = True) -> None:
    """End a run without a report, the way its own ``finally`` would, and
    (unless *drain* is off) let the pump fill its slot."""
    task = mgr._tasks.get(info.id)
    if task is not None:
        task.cancel()
        # flake-ok: reaps the run task cancelled on the line above; a parked run ends at its first await
        await asyncio.gather(task, return_exceptions=True)
    info.done = True
    mgr._claim_finalize(info)
    assert mgr._release_slot(info), "the run's slot was already released"
    mgr._running_count -= 1
    if drain:
        mgr._drain_queue()


async def _resident_with_resume_entry(mgr: SubagentManager) -> tuple[SubagentInfo, SubagentInfo]:
    """A run that yielded its lane slot to a child and now asks for it back.

    Its resume entry sits in the window behind the full pool. The run's own
    ``finally`` withdraws the entry, as production's does.
    """
    store = mgr._taskq

    async def _resident(self: SubagentManager, info: SubagentInfo) -> None:
        try:
            # flake-ok: the resident run parks until the test stops it, and its finally withdraws the resume entry
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
