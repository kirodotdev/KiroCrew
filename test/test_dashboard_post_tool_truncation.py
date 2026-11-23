"""The dashboard's ``PostToolUse`` fire (``_on_tool_result`` in
``kiro_crew/dashboard/chat_turn/tool_results.py``) bounds ``tool_response.output``
to 2000 characters and reports the cut via ``truncated``/``output_length``
(issue #38593): a hook used to get a bare ``[:2000]`` slice with no signal a
cut happened.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent


def _stub_state(tmp_path):
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._yolo = False
    state.slack_client = None
    hook_store = MagicMock()
    hook_store.fire = AsyncMock(return_value=[])
    state._hook_store = hook_store
    return state, hook_store


def _tool_call(tcid: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_TOOL_CALL,
        tool_call_id=tcid,
        title="Running: read_file",
        tool_name="read_file",
        mcp_server_name="",
        raw_tool_params={"path": "x.txt"},
        tool_input=json.dumps({"path": "x.txt"}),
    )


async def _drive(state, slot, events, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    async def _stream(_msg):
        for ev in events:
            yield ev

    client = MagicMock()
    client.stream = _stream
    client.stream_command = _stream
    client.context_usage_pct = MagicMock(return_value=1.0)
    client.client = None
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))
    slot.messages.append({"role": "user", "content": "read it", "ts": "t0", "cls": ""})
    try:
        await chat_runner._run_chat(state, slot, "read it")
    finally:
        tasks = list(state._background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _post_tool_use_calls(hook_store):
    from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE

    return [c for c in hook_store.fire.await_args_list if c.args and c.args[0] == HOOK_EVENT_POST_TOOL_USE]


@pytest.mark.asyncio
async def test_a_long_tool_output_is_cut_and_the_cut_is_reported(tmp_path, monkeypatch):
    state, hook_store = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    text = "x" * 7901
    events = [
        _tool_call("tc-1"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output=text, tool_final=True),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(state, slot, events, monkeypatch)

    calls = _post_tool_use_calls(hook_store)
    assert calls
    response = calls[0].kwargs["tool_response"]
    assert len(response["output"]) == 2000
    assert response["truncated"] is True
    assert response["output_length"] == 7901


@pytest.mark.asyncio
async def test_a_short_tool_output_arrives_unchanged_and_untruncated(tmp_path, monkeypatch):
    state, hook_store = _stub_state(tmp_path)
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    text = "y" * 1500
    events = [
        _tool_call("tc-2"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-2", tool_output=text, tool_final=True),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive(state, slot, events, monkeypatch)

    calls = _post_tool_use_calls(hook_store)
    assert calls
    response = calls[0].kwargs["tool_response"]
    assert response["output"] == text
    assert response["truncated"] is False
    assert response["output_length"] == 1500
