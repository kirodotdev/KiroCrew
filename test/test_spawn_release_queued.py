"""A parent cancels its own spawn the gate is still holding, and it never starts.

``spawn_release`` on a queued id is the parent's control over a run that has not
started. Until such a run starts or is cancelled it keeps the parent attached
(``subagents_attached_async``), which is what makes a reload of the parent
refuse. Real handlers and a real manager whose memory gate defers the spawn, so
the run exists only as a task-store row.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request
from overload_fakes import mock_ctx, mock_sessions, wait_taskq_open

import kiro_crew.subagent as subagent_mod
from kiro_crew import taskq
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.chat_utils import subagents_attached_async
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.mcp_tools import spawn as spawn_tools
from kiro_crew.subagent import SubagentManager
from kiro_crew.subagent_manager.admission import SpawnAdmissionCoordinator
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = [
    pytest.mark.xdist_group("member_memory_api"),
    pytest.mark.usefixtures("close_subagent_managers", "healthy_host_memory"),
]

env = _member_env

PARENT = "dashboard:alice"


def _release(env, run_id: str, session: str):
    return make_request(
        env.state,
        f"/api/spawn/{run_id}/release",
        method="POST",
        match_info={"agent_id": run_id},
        internal=True,
        session=session,
        attested=True,
    )


async def _deferring_manager(monkeypatch) -> tuple[SubagentManager, list[str], dict]:
    """A manager whose memory floor defers every spawn until ``free`` is raised.

    The list is the id of every run that started, in start order.
    """
    cfg = KiroCrewConfig()
    cfg.agent.subagent_cost_gb = 0.5
    cfg.agent.spawn_min_memory_gb = 4.0
    monkeypatch.setattr(KiroCrewConfig, "load", lambda: cfg)
    monkeypatch.setattr(subagent_mod, "Stats", MagicMock())
    monkeypatch.setattr(subagent_mod, "sel", MagicMock())
    monkeypatch.setattr(subagent_mod, "list_agents", lambda: [SimpleNamespace(name="kirocrew")])
    monkeypatch.setattr(SpawnAdmissionCoordinator, "open_store_off_loop", True)
    monkeypatch.setattr(SpawnAdmissionCoordinator, "pump_off_loop", True)
    free = {"gb": 3.0}

    def memory_check(*, min_gb, **_kw):
        return free["gb"] >= min_gb, free["gb"]

    monkeypatch.setattr(subagent_mod, "check_memory_available", memory_check)
    mgr = SubagentManager(sessions=mock_sessions(), ctx_builder=mock_ctx(), max_concurrent=3)
    await wait_taskq_open(mgr)
    mgr._spawn_stagger_secs = 0.0
    mgr._taskq_admit_wait_secs = 30.0
    started: list[str] = []

    async def worker(info) -> None:
        started.append(info.id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(mgr, "_run", AsyncMock(side_effect=worker))
    return mgr, started, free


async def _close(mgr: SubagentManager) -> None:
    mgr._shutting_down = True
    tasks = [task for task in mgr._tasks.values() if not task.done()]
    for task in tasks:
        task.cancel()
    await await_until(
        asyncio.gather(*tasks, return_exceptions=True), timeout=5, what="task teardown"
    )
    mgr._taskq.close()


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_parent_cancels_its_own_deferred_spawn_and_it_never_starts(
    env, monkeypatch
) -> None:
    mgr, started, free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        assert mgr.get(info.id) is None, "fixture: a deferred row has no registered run"
        assert await subagents_attached_async(env.state, None, PARENT, "reload") is True

        # Another session cannot reach it, and the row stays queued.
        other = await messaging.api_spawn_release(_release(env, info.id, "dashboard:bob"))
        assert other.status == 404
        assert await mgr.queued_run_async(info.id) is not None

        resp = await messaging.api_spawn_release(_release(env, info.id, PARENT))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"conversation": info.id, "status": "cancelled"}
        assert await mgr.queued_run_async(info.id) is None
        row = await mgr._taskq.run(mgr._taskq.get, info.id)
        assert row is not None and row.state == taskq.CANCELLED

        # The stop reaches the parent as its own, not as the user's.
        record = mgr.get(info.id)
        assert record is not None and record.queued is True and record.user_stopped is True
        assert record._stop_origin == "cancelled by its parent session"

        # Once its stop report has run, nothing holds the parent attached.
        async def _parent_detached() -> bool:
            return not await subagents_attached_async(env.state, None, PARENT, "reload")

        await async_wait_until(
            _parent_detached, describe=lambda: "the stop report has not released the parent"
        )
        assert await subagents_attached_async(env.state, None, PARENT, "reload") is False

        # A control spawn, deferred the same way and newer than the cancelled
        # row. The memory floor clears and both rows are made eligible now: a
        # pass that starts the control has read past the older, cancelled row,
        # which it would have claimed first had it still been waiting.
        control = await mgr.spawn_async("the work still needed", parent_session_key=PARENT)
        assert control is not None and control.queued is True
        free["gb"] = 32.0
        mgr._taskq_admit_wait_secs = 0.05
        await mgr._taskq.run(mgr._taskq.defer, info.id, wait=-1.0, reason="test: eligible now")
        await mgr._taskq.run(mgr._taskq.defer, control.id, wait=-1.0, reason="test: eligible now")

        def _control_started() -> bool:
            if control.id in started:
                return True
            mgr._drain_queue()
            return False

        await async_wait_until(_control_started, describe=lambda: f"started: {started}")
        assert control.id in started, "the control spawn never started"
        assert started == [control.id], "a cancelled spawn started"
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_cancel_that_finds_the_run_started_releases_it_instead(env, monkeypatch) -> None:
    """A row that registered as a run between the read and the cancel is not stopped
    as a queued one: the release path answers for the live run."""
    mgr, _started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        monkeypatch.setattr(mgr, "cancel_queued", AsyncMock(return_value=False))
        monkeypatch.setattr(
            mgr,
            "release_conversation_async",
            AsyncMock(return_value=(False, "conversation_busy: run in flight")),
        )
        resp = await messaging.api_spawn_release(_release(env, info.id, PARENT))
        assert resp.status == 409
        assert json.loads(resp.text)["code"] == "conversation_busy"
        mgr.cancel_queued.assert_awaited_once_with(
            info.id, PARENT, stop_origin="cancelled by its parent session"
        )
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_cancel_never_touches_the_store_on_the_event_loop(env, monkeypatch) -> None:
    """The row's store cancel runs on the store's writer thread, not in the request.

    A store read or write on the loop holds every session's turn for the store's
    busy timeout whenever another connection contends for it. The single-run
    ``cancel`` takes that call on the loop for a queued row, so the route uses the
    queued stop's off-loop store phase instead.
    """
    import threading

    mgr, _started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    loop_thread = threading.get_ident()
    cancel_threads: list[int] = []
    original = SpawnAdmissionCoordinator.taskq_cancel_queued

    def recording(self, agent_id, **kwargs):
        cancel_threads.append(threading.get_ident())
        return original(self, agent_id, **kwargs)

    monkeypatch.setattr(SpawnAdmissionCoordinator, "taskq_cancel_queued", recording)
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        resp = await messaging.api_spawn_release(_release(env, info.id, PARENT))
        assert json.loads(resp.text)["status"] == "cancelled"
        assert cancel_threads, "the store cancel never ran"
        assert loop_thread not in cancel_threads, "the store cancel ran on the event loop"
    finally:
        await _close(mgr)


def test_the_tool_says_a_queued_run_was_cancelled(monkeypatch) -> None:
    monkeypatch.setattr(
        spawn_tools.mcp_core,
        "_post",
        lambda path, body: {"conversation": "abc123", "status": "cancelled"},
    )
    out = spawn_tools.spawn_release("spawn_release", {"conversation": "abc123"})
    assert out.startswith("Cancelled run abc123 before it started")
    monkeypatch.setattr(
        spawn_tools.mcp_core,
        "_post",
        lambda path, body: {"conversation": "abc123", "status": "released"},
    )
    released = spawn_tools.spawn_release("spawn_release", {"conversation": "abc123"})
    assert released.startswith("Released conversation abc123")


def test_a_queued_run_points_its_parent_at_the_cancel(monkeypatch) -> None:
    monkeypatch.setattr(
        spawn_tools.mcp_core,
        "_get",
        lambda path: {"id": "abc123", "done": False, "queued": True, "status": "queued"},
    )
    out = spawn_tools.spawn_status("spawn_status", {"agent_id": "abc123"})
    assert "cancel it with spawn_release" in out
