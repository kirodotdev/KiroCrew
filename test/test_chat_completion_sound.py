"""Completion frames distinguish a finished conversation from a turn boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from chat_test_helpers import _make_state

from kiro_crew import autonudge
from kiro_crew.dashboard import chat_runner as cr


@pytest.fixture
def completion_state(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    state.subagents = MagicMock()
    state.subagents.running_agents_for.return_value = []
    state.subagents.has_pending_work_for_async = AsyncMock(return_value=False)
    state.subagents.wait_for_parent_reports = AsyncMock(return_value=False)
    state.subagents._queued_depth.return_value = 0
    state.broadcast_ws = MagicMock()
    slot = state.get_or_create_slot("chat-sound")
    slot._titled = True
    monkeypatch.setattr(cr, "title_then_refresh", AsyncMock())
    monkeypatch.setattr(cr, "generate_session_summary", AsyncMock())
    monkeypatch.setattr(autonudge, "get_instance", lambda: None)
    return state, slot


async def finish_frame(state, slot):
    await cr._finish_queue_cycle(state, slot)
    tasks = list(state._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    frames = [
        call.args[1] for call in state.broadcast_ws.call_args_list if call.args[0] == "chat_done"
    ]
    assert len(frames) == 1
    return frames[0]


@pytest.mark.asyncio
async def test_finished_conversation_is_not_continuing(completion_state):
    state, slot = completion_state
    frame = await finish_frame(state, slot)
    assert frame["slot"] == slot.key
    assert frame.get("continuing", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("work", ["running", "queued", "delivering", "stage", "synthesis"])
async def test_intermediate_completion_keeps_continuation_signal(completion_state, work):
    state, slot = completion_state
    if work == "running":
        state.subagents.running_agents_for.return_value = [{"id": "child"}]
    elif work == "queued":
        state.subagents._queued_depth.return_value = 1
    elif work == "delivering":
        slot._subagent_deliveries_inflight = 1
    elif work == "stage":
        slot._in_stage_execution = True
    else:
        slot._pending_synthesis = True
        slot._synthesis_inflight = True
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is True


@pytest.mark.asyncio
async def test_continuation_uses_linked_session_identity(completion_state):
    state, slot = completion_state
    slot.linked_session_key = "slack:123.456"
    state.subagents.running_agents_for.side_effect = lambda key: (
        [{"id": "child"}] if key == slot.linked_session_key else []
    )
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is True
    # The continuation check must have consulted the LINKED session's workers.
    # ``assert_called_with`` would check only the most recent call, and the
    # background drain in ``finish_frame`` can consult the mock again after
    # the continuation check (on a slow runner it does), so assert membership
    # in the call list instead of ordering against unrelated later calls.
    assert call(slot.linked_session_key) in state.subagents.running_agents_for.call_args_list


@pytest.mark.asyncio
async def test_active_monitor_keeps_conversation_open(completion_state, monkeypatch):
    state, slot = completion_state
    monitor = SimpleNamespace(active=True)
    service = SimpleNamespace(get_by_slot=lambda key: monitor if key == slot.key else None)
    monkeypatch.setattr(autonudge, "get_instance", lambda: service)
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is True


@pytest.mark.asyncio
async def test_stopped_monitor_allows_completion(completion_state, monkeypatch):
    state, slot = completion_state
    service = SimpleNamespace(get_by_slot=lambda key: SimpleNamespace(active=False))
    monkeypatch.setattr(autonudge, "get_instance", lambda: service)
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is False


@pytest.mark.asyncio
async def test_question_is_reported_separately_from_continuation(completion_state):
    state, slot = completion_state
    slot._question_pending = {"question": {"blocking": False}}
    state.subagents.running_agents_for.return_value = [{"id": "child"}]
    frame = await finish_frame(state, slot)
    assert frame.get("needs_input", False) is True
    assert frame.get("continuing", False) is True


@pytest.mark.asyncio
async def test_queued_recovery_is_not_a_finished_conversation(completion_state):
    state, slot = completion_state
    slot.queue_append("continue recovery")
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is True


@pytest.mark.asyncio
async def test_auth_blocked_queue_requires_input_not_automatic_work(completion_state):
    state, slot = completion_state
    slot.queue_append("held until the user signs in")
    slot._last_turn_auth_required = True
    frame = await finish_frame(state, slot)
    assert frame.get("continuing", False) is False


@pytest.mark.asyncio
async def test_final_parent_reply_after_delivery_is_complete(completion_state):
    state, slot = completion_state
    slot._subagent_deliveries_inflight = 1
    first = await finish_frame(state, slot)
    assert first.get("continuing", False) is True
    state.broadcast_ws.reset_mock()
    slot._subagent_deliveries_inflight = 0
    second = await finish_frame(state, slot)
    assert second.get("continuing", False) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_run", [False, True])
async def test_plan_handoff_notifies_only_completion_or_manual_approval(
    completion_state, monkeypatch, tmp_path, auto_run
):
    from kiro_crew.dashboard import chat_orchestrator as orchestrator

    state, slot = completion_state
    slot.mode = "orchestrator"
    slot._stage_titles = ["First", "Second"]
    slot._auto_run = auto_run
    monkeypatch.setattr(orchestrator, "config_dir", lambda: tmp_path)

    async def run_stage(state, slot, message, **kwargs):
        callback = kwargs.get("_on_consumed")
        if callable(callback):
            callback(True)
        slot.append("assistant", "Stage result", "msg msg-a")
        await cr._finish_queue_cycle(state, slot)

    monkeypatch.setattr(orchestrator, "_run_chat", run_stage)
    await asyncio.wait_for(orchestrator._stage_loop(state, slot, auto_run=auto_run), timeout=10)
    tasks = list(state._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    frames = [
        call.args[1] for call in state.broadcast_ws.call_args_list if call.args[0] == "chat_done"
    ]
    assert frames[0]["continuing"] is True
    if auto_run:
        assert len(frames) == 3
        assert frames[1]["continuing"] is True
        assert frames[-1]["continuing"] is False
        assert frames[-1]["needs_input"] is False
    else:
        assert len(frames) == 2
        assert frames[-1]["needs_input"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("linked", ["", "slack:123.456"])
async def test_workflow_activity_is_captured_in_completion_frame(completion_state, linked):
    from kiro_crew.workflows.registry import STATUS_FINISHED, RunHandle, RunRegistry

    state, slot = completion_state
    slot.linked_session_key = linked
    registry = RunRegistry()
    registry.register(
        RunHandle("workflow", "workflow", session_key=linked or f"dashboard:{slot.key}")
    )
    state.workflow_service = SimpleNamespace(registry=registry)
    frame = await finish_frame(state, slot)
    assert frame["continuing"] is True
    registry.mark_terminal("workflow", STATUS_FINISHED)
    state.broadcast_ws.reset_mock()
    frame = await finish_frame(state, slot)
    assert frame["continuing"] is False
