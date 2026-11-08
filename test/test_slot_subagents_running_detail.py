"""The ``slot_subagents_running`` 409 names what keeps the slot attached.

The guard refuses while any of three sources says the session still has children:
an unfinished in-memory run, an accepted spawn with no run yet (a task-store row or
a window entry), or a sub-agent result still being delivered. A body that names
none of them leaves a user whose slot keeps refusing with nothing to act on, so the
body lists each source beside the unchanged ``error`` and ``code`` fields: ids and
states only, never task text.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_handlers import api_chat_slot_continue
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.subagent_manager.admission.types import QueuedRun, QueuedRunListing
from kiro_crew.taskq import model

_TASK_TEXT = "summarize the quarterly notes for the team"


def _make_app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/continue", api_chat_slot_continue)
    return app


def _slot() -> _ChatSlot:
    slot = _ChatSlot("s")
    slot.append("user", "spawn some agents", "msg msg-u")
    slot.append("assistant", "Spawned 2 agents.", "msg msg-a")
    return slot


def _state(slot: _ChatSlot, subagents: Any) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.broadcast_ws = MagicMock()
    state.subagents = subagents
    return state


def _subagents(
    running: list[Any] | None, queued_depth: int, listing: QueuedRunListing | None = None
) -> SimpleNamespace:
    """A registry double with the three reads the guard and the body make."""

    async def queued_runs_async(parent_session_key: str | None = None, **_: Any):
        return listing if listing is not None else QueuedRunListing(())

    return SimpleNamespace(
        running_agents_for=MagicMock(return_value=running),
        _queued_depth=MagicMock(return_value=queued_depth),
        queued_runs_async=queued_runs_async,
    )


@pytest.fixture
def _patched():
    mock_sel = MagicMock()
    started = AsyncMock(return_value=True)
    with (
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=mock_sel),
        patch("kiro_crew.dashboard.chat_handlers._start_next_queued_turn", started),
    ):
        yield started


async def _continue(state: DashboardState) -> tuple[int, dict[str, Any], str]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post("/api/chat/slots/s/continue")
        text = await resp.text()
        return resp.status, await resp.json(), text


@pytest.mark.asyncio
async def test_the_409_names_each_unfinished_run_and_its_state(_patched) -> None:
    running = [
        {"id": "sa-run-1", "task": _TASK_TEXT, "agent": "builder", "stalled": False},
        {"id": "sa-run-2", "task": _TASK_TEXT, "agent": "builder", "stalled": True},
    ]
    slot = _slot()
    status, body, text = await _continue(_state(slot, _subagents(running, 0)))

    assert status == 409
    assert body["error"] == "sub-agents are running"
    assert body["code"] == "slot_subagents_running"
    assert body["runs"] == [
        {"id": "sa-run-1", "state": "running"},
        {"id": "sa-run-2", "state": "stalled"},
    ]
    assert body["queued"] == [] and body["queued_partial"] is False
    assert body["deliveries_inflight"] == 0
    # Ids and states only: the run's task text and agent name stay out.
    assert _TASK_TEXT not in text and "builder" not in text
    assert not slot._queue


@pytest.mark.asyncio
async def test_the_409_names_each_queued_row_with_its_store_state(_patched) -> None:
    listing = QueuedRunListing(
        (
            QueuedRun(
                id="sa-row-1", task=_TASK_TEXT, parent_session_key="dashboard:s", state="retry_wait"
            ),
            QueuedRun(id="sa-window-1", task=_TASK_TEXT, parent_session_key="dashboard:s"),
        ),
        partial=True,
    )
    status, body, text = await _continue(_state(_slot(), _subagents([], 2, listing)))

    assert status == 409
    assert body["code"] == "slot_subagents_running"
    assert body["runs"] == []
    assert body["queued"] == [
        {"id": "sa-row-1", "state": "retry_wait"},
        {"id": "sa-window-1", "state": "queued"},
    ]
    assert body["queued_partial"] is True
    assert _TASK_TEXT not in text


@pytest.mark.asyncio
async def test_the_409_counts_the_deliveries_still_in_flight(_patched) -> None:
    slot = _slot()
    slot._subagent_deliveries_inflight = 2
    status, body, _ = await _continue(_state(slot, _subagents([], 0)))

    assert status == 409
    assert body["code"] == "slot_subagents_running"
    assert (body["runs"], body["queued"], body["deliveries_inflight"]) == ([], [], 2)


@pytest.mark.asyncio
async def test_an_unanswered_probe_reads_as_unknown_and_still_refuses(_patched) -> None:
    # A failed running probe is unknown children, so the guard refuses; the body
    # says unknown (null), not "none". A registry with no queued listing marks the
    # queue partial rather than reporting it empty.
    subs = SimpleNamespace(
        running_agents_for=MagicMock(return_value=None),
        _queued_depth=MagicMock(return_value=0),
    )
    status, body, _ = await _continue(_state(_slot(), subs))

    assert status == 409
    assert body["code"] == "slot_subagents_running"
    assert body["runs"] is None
    assert body["queued"] == [] and body["queued_partial"] is True


@pytest.mark.asyncio
async def test_a_listing_that_raises_costs_its_field_not_the_refusal(_patched) -> None:
    async def broken(parent_session_key: str | None = None, **_: Any):
        raise RuntimeError("store gone")

    subs = _subagents([{"id": "sa-run-1", "stalled": False}], 0)
    subs.queued_runs_async = broken
    status, body, _ = await _continue(_state(_slot(), subs))

    assert status == 409
    assert body["runs"] == [{"id": "sa-run-1", "state": "running"}]
    assert body["queued"] == [] and body["queued_partial"] is True


class TestAgainstARealStore:
    """The queued half read from a real task store, through the real registry."""

    @staticmethod
    async def _manager() -> Any:
        from kiro_crew.subagent import SubagentManager

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_agent_selection = MagicMock(return_value=("template", ""))
        provider = AsyncMock()
        provider.stream = MagicMock(side_effect=lambda *a, **k: iter(()))
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
        sessions.get_agent = MagicMock(return_value="")
        sessions.get_approval_policy = MagicMock(return_value="auto")
        sessions.has_session = MagicMock(return_value=True)
        ctx = MagicMock()
        ctx.hooks.auto_approve_subagent_spawn = True
        ctx.hooks.auto_approve_subagent_tools = False
        mgr = SubagentManager(sessions=sessions, ctx_builder=ctx)
        await mgr.wait_taskq_ready()
        mgr._should_use_session_sharing = MagicMock(return_value=False)
        mgr._spawn_stagger_secs = 0.0
        assert mgr._admission.taskq_store() is not None
        return mgr

    @pytest.mark.asyncio
    async def test_a_parked_row_with_no_run_is_named_with_its_store_state(self) -> None:
        mgr = await self._manager()
        store = mgr._admission.taskq_store()
        key = "dashboard:chat-1"
        rec = mgr._admission.taskq_build_record(
            "parked0",
            {"task": _TASK_TEXT, "parent_session_key": key},
            parent_session_key=key,
            memory_store="",
            app="",
            model="",
            allowed_tools=None,
            approval_mode=None,
        )
        assert mgr._admission.taskq_accept_record(rec) is None
        # The shape a lost terminal write leaves: claimed, started, released to
        # ``retry_wait`` with no lease and no registered run. Due later, so no
        # dispatcher claims it while the test reads.
        claimed = store.claim("parked0")
        assert claimed is not None
        gen = claimed.generation
        assert store.transition("parked0", model.STARTING, generation=gen)
        assert store.transition(
            "parked0", model.RETRY_WAIT, generation=gen, next_run_at=store.now() + 3600
        )
        mgr._agents.pop("parked0", None)
        mgr._queue.clear()
        state = SimpleNamespace(subagents=mgr, _slots={})
        slot = _ChatSlot("chat-1")

        resp = await chat_handlers._subagents_attached_response(state, slot, key, "continue")

        assert resp is not None and resp.status == 409
        body = json.loads(resp.body)
        assert body["code"] == "slot_subagents_running"
        assert body["runs"] == []
        assert body["queued"] == [{"id": "parked0", "state": "retry_wait"}]
        assert body["queued_partial"] is False
        assert body["deliveries_inflight"] == 0
        assert _TASK_TEXT not in resp.text
        await mgr.cancel_all()
