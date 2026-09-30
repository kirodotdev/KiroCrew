"""The queued count is re-published at the points a wave settles.

``subagent_queued`` is pushed, and the dashboard otherwise resets its count only
from a reconnect's snapshot. A frame it missed, or one that arrived after the
frame that superseded it, leaves "N waiting to start" and the old wait reason on
the card after every run has finished. Three points answer with the authoritative
depth whether or not it changed: a terminal report of a run that started, Stop
all, and the stage's Cancel -- including a stop that has nothing left to stop.
The synthetic terminal of a row stopped before it started is the exception: its
stop already published the depth, so it adds no frame of its own.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from overload_fakes import mock_ctx, mock_sessions

import kiro_crew.subagent as subagent_mod
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.subagent_wait_reasons import QUEUED_REASON_LOW_MEMORY

#: The label a memory-deferred wave leaves behind for its parent.
_STALE_WAIT = {"reason": QUEUED_REASON_LOW_MEMORY, "available_gb": 6.1, "required_gb": 6.5}


async def _manager(monkeypatch: pytest.MonkeyPatch, *, store: bool) -> SubagentManager:
    """A real manager on either count path: the task store's, or the window's."""
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", store)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", store)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    if store:
        await asyncio.wait_for(mgr.wait_taskq_ready(), 5)
    return mgr


def _record(mgr: SubagentManager) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []

    async def on_event(etype: str, info: Any, extra: dict[str, Any]) -> None:
        events.append((etype, dict(extra)))

    mgr._on_event = on_event
    return events


async def _until_queued(events: list[tuple[str, dict[str, Any]]]) -> None:
    for _ in range(100):
        if any(etype == "subagent_queued" for etype, _extra in events):
            return
        await asyncio.sleep(0.02)


def _close(mgr: SubagentManager) -> None:
    if mgr._taskq is not None:
        mgr._taskq.close()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("store", [True, False], ids=["task-store", "window"])
async def test_stop_all_with_nothing_to_stop_publishes_zero_and_forgets_the_label(
    monkeypatch: pytest.MonkeyPatch, store: bool
) -> None:
    mgr = await _manager(monkeypatch, store=store)
    try:
        mgr._queue_wait["dash:stale"] = dict(_STALE_WAIT)
        events = _record(mgr)

        assert await mgr.cancel_for_parent("dash:stale") == (0, 0)
        await _until_queued(events)

        assert events == [("subagent_queued", {"queued": 0})]
        assert "dash:stale" not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("store", [True, False], ids=["task-store", "window"])
async def test_stage_cancel_with_nothing_to_stop_publishes_zero_and_forgets_the_label(
    monkeypatch: pytest.MonkeyPatch, store: bool
) -> None:
    mgr = await _manager(monkeypatch, store=store)
    try:
        mgr._queue_wait["dash:stage"] = dict(_STALE_WAIT)
        events = _record(mgr)

        assert await mgr.cancel_for_boundary("dash:stage", "stage-1") == (0, 0)
        await _until_queued(events)

        assert events == [("subagent_queued", {"queued": 0})]
        assert "dash:stage" not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_refused_stage_cancel_still_publishes_the_depth_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = await _manager(monkeypatch, store=False)
    try:
        mgr._queue_wait["dash:refused"] = dict(_STALE_WAIT)
        events = _record(mgr)
        monkeypatch.setattr(
            mgr, "_hold_boundary_cancellation", lambda parent, owner: "pending_scope_cap"
        )

        assert await mgr.cancel_for_boundary("dash:refused", "stage-1") == (0, 0)
        await _until_queued(events)
        await asyncio.sleep(0.05)

        assert events == [("subagent_queued", {"queued": 0})]
        assert "dash:refused" not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("store", [True, False], ids=["task-store", "window"])
async def test_a_terminal_report_republishes_its_parents_queued_depth(
    monkeypatch: pytest.MonkeyPatch, store: bool
) -> None:
    mgr = await _manager(monkeypatch, store=store)
    try:
        mgr._queue_wait["dash:term"] = dict(_STALE_WAIT)
        events = _record(mgr)
        info = SubagentInfo(id="a1", task="t", parent_session_key="dash:term", batch_id="b1")

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )
        await _until_queued(events)

        assert [etype for etype, _extra in events] == ["subagent_done", "subagent_queued"]
        assert events[-1][1] == {"queued": 0}
        assert "dash:term" not in mgr._queue_wait
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
@pytest.mark.parametrize("store", [True, False], ids=["task-store", "window"])
async def test_a_queued_stop_terminal_adds_no_depth_frame_of_its_own(
    monkeypatch: pytest.MonkeyPatch, store: bool
) -> None:
    """The stop that removed the row published the depth; its terminal must not."""
    mgr = await _manager(monkeypatch, store=store)
    try:
        events = _record(mgr)
        info = SubagentInfo(
            id="q1",
            task="t",
            parent_session_key="dash:queued",
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
        await asyncio.sleep(0.1)

        assert [etype for etype, _extra in events] == ["subagent_done"]
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_stopping_a_row_held_only_by_the_store_publishes_the_depth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A row that spilled out of the window is counted, so its stop must re-count."""
    mgr = await _manager(monkeypatch, store=False)
    try:
        events = _record(mgr)
        stored = {"_preassigned_id": "q-store", "task": "t", "parent_session_key": "dash:spill"}
        monkeypatch.setattr(
            SpawnAdmissionCoordinator,
            "taskq_cancel_queued",
            lambda self, agent_id, **kw: dict(stored),
        )

        assert await mgr.cancel("q-store") is True
        await _until_queued(events)

        assert sorted(etype for etype, _extra in events) == ["subagent_done", "subagent_queued"]
    finally:
        _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_failing_depth_emit_never_costs_the_parent_its_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mgr = await _manager(monkeypatch, store=False)
    try:
        on_done = AsyncMock()
        mgr._on_done = on_done
        monkeypatch.setattr(mgr, "_emit_queue_depth", MagicMock(side_effect=RuntimeError("boom")))
        info = SubagentInfo(id="a2", task="t", parent_session_key="dash:guard")

        await mgr._report_terminal(
            info,
            source="test",
            injection_timeout_reason="delivery timed out",
            mark_delivered_on_success=False,
        )

        on_done.assert_awaited_once_with(info)
        assert await mgr.cancel_for_parent("dash:guard") == (0, 0)
    finally:
        _close(mgr)
