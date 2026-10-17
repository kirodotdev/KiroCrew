"""``DELETE /api/spawn/{id}`` on a spawn the gate is still holding cancels it.

``GET /api/spawn/{id}`` answers such a run as ``queued`` and ``POST
.../release`` cancels it; ``DELETE`` on the same id cancels it too, instead of
falling through to the persisted-dismissal arm, a route for cards of FINISHED
runs that an internal caller cannot use. Real handlers and a real manager whose
memory gate defers the spawn, so the run exists only as a task-store row.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request
from test_spawn_release_queued import _close, _deferring_manager

from kiro_crew import taskq
from kiro_crew.dashboard.handlers import messaging

pytestmark = [
    pytest.mark.xdist_group("member_memory_api"),
    pytest.mark.usefixtures("close_subagent_managers", "healthy_host_memory"),
]

env = _member_env

PARENT = "dashboard:alice"


def _delete(env, run_id: str, session: str, *, owner: bool = False):
    """An internal caller's DELETE as *session*, or the dashboard owner's (cookie
    auth, no internal claim) when *owner* is set."""
    return make_request(
        env.state,
        f"/api/spawn/{run_id}",
        method="DELETE",
        match_info={"agent_id": run_id},
        owner=owner,
        internal=not owner,
        session=session,
        attested=True,
    )


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_an_internal_parent_deletes_its_own_queued_spawn(env, monkeypatch) -> None:
    mgr, started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        assert mgr.get(info.id) is None, "fixture: a deferred row has no registered run"

        # Another session cannot reach it, and the row stays queued.
        other = await messaging.api_spawn_delete(_delete(env, info.id, "dashboard:bob"))
        assert other.status == 404
        assert json.loads(other.text)["code"] == "task_scope_denied"
        assert await mgr.queued_run_async(info.id) is not None

        resp = await messaging.api_spawn_delete(_delete(env, info.id, PARENT))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"ok": True, "cancelled": True, "queued": True}
        assert await mgr.queued_run_async(info.id) is None
        row = await mgr._taskq.run(mgr._taskq.get, info.id)
        assert row is not None and row.state == taskq.CANCELLED

        # The stop is reported as the parent's, and the run never starts.
        record = mgr.get(info.id)
        assert record is not None and record.queued is True and record.user_stopped is True
        assert record._stop_origin == "cancelled by its parent session"
        assert started == []
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_the_dashboard_owner_deletes_a_queued_spawn(env, monkeypatch) -> None:
    """The owner's own surface (cookie auth, no internal claim) is admitted without
    the ownership check, as on every spawn control, and the cancel is the same."""
    mgr, _started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        resp = await messaging.api_spawn_delete(_delete(env, info.id, "dashboard:ui", owner=True))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"ok": True, "cancelled": True, "queued": True}
        row = await mgr._taskq.run(mgr._taskq.get, info.id)
        assert row is not None and row.state == taskq.CANCELLED
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_queued_cancel_that_finds_the_run_started_is_not_a_dismissal(
    env, monkeypatch
) -> None:
    """A row that started between the read and the cancel is a live run: it takes
    the live ``cancel`` path, never the persisted-dismissal one."""
    mgr, _started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True

        async def started_meanwhile(agent_id, parent, *, stop_origin):
            # The row started while its cancel was on the writer thread: the
            # manager now holds it as a live run, and the queued stop declines.
            info.queued = False
            mgr._agents[agent_id] = info
            return False

        monkeypatch.setattr(mgr, "cancel_queued", AsyncMock(side_effect=started_meanwhile))
        monkeypatch.setattr(mgr, "cancel", AsyncMock(return_value=True))
        dismissal = AsyncMock(side_effect=AssertionError("a started run was dismissed"))
        monkeypatch.setattr(messaging, "_log_panel_dismissal", dismissal)
        resp = await messaging.api_spawn_delete(_delete(env, info.id, PARENT))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"ok": True, "cancelled": True}
        mgr.cancel_queued.assert_awaited_once_with(
            info.id, PARENT, stop_origin="cancelled by its parent session"
        )
        mgr.cancel.assert_awaited_once_with(info.id)
    finally:
        await _close(mgr)


@pytest.mark.asyncio
@pytest.mark.timeout(30)
async def test_a_started_run_is_never_cancelled_as_queued(env, monkeypatch) -> None:
    """An id the manager registers as a run is not a queued spawn: the queued
    cancel is never asked, whatever the store says."""
    mgr, _started, _free = await _deferring_manager(monkeypatch)
    env.state.subagents = mgr
    try:
        info = await mgr.spawn_async("summarize the log", parent_session_key=PARENT)
        assert info is not None and info.queued is True
        # It starts: the manager now holds it as a live run.
        info.queued = False
        mgr._agents[info.id] = info
        monkeypatch.setattr(mgr, "cancel_queued", AsyncMock(return_value=True))
        monkeypatch.setattr(mgr, "cancel", AsyncMock(return_value=True))
        resp = await messaging.api_spawn_delete(_delete(env, info.id, PARENT))
        assert resp.status == 200, resp.text
        assert json.loads(resp.text) == {"ok": True, "cancelled": True}
        mgr.cancel_queued.assert_not_awaited()
        mgr.cancel.assert_awaited_once_with(info.id)
    finally:
        await _close(mgr)
