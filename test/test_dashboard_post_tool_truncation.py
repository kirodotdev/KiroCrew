"""A dashboard turn's ``PostToolUse`` hook is told whether ``tool_response.output`` was cut."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, EVENT_TOOL_RESULT, AcpEvent
from kiro_crew.hooks import HOOK_EVENT_POST_TOOL_USE


@pytest.mark.asyncio
@pytest.mark.parametrize(("length", "kept", "cut"), [(7901, 2000, True), (1500, 1500, False)])
async def test_the_hook_is_told_whether_the_output_was_cut(
    tmp_path, monkeypatch, length, kept, cut
):
    from kiro_crew.dashboard import chat_runner

    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._yolo = False
    state.slack_client = None
    state._hook_store = MagicMock()
    state._hook_store.fire = AsyncMock(return_value=[])
    slot = state.get_or_create_slot("chat-1")
    slot._titled = True
    text = "x" * length
    events = [
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-1",
            title="Running: read_file",
            tool_name="read_file",
            mcp_server_name="",
            raw_tool_params={"path": "x.txt"},
            tool_input=json.dumps({"path": "x.txt"}),
        ),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-1", tool_output=text, tool_final=True),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]

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
        await asyncio.gather(*tasks, return_exceptions=True)

    posts = [
        c
        for c in state._hook_store.fire.await_args_list
        if c.args and c.args[0] == HOOK_EVENT_POST_TOOL_USE
    ]
    assert len(posts) == 1
    assert posts[0].kwargs["tool_response"] == {
        "output": text[:kept],
        "truncated": cut,
        "output_length": length,
    }
