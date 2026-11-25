"""A channel turn's ``PostToolUse`` hook is told whether ``tool_response.output`` was cut."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.messaging.script_hooks import ScriptHookTurn
from kiro_crew.messaging.tool_hooks import ChannelTurnHooks


@pytest.mark.asyncio
@pytest.mark.parametrize(("length", "kept", "cut"), [(7901, 2000, True), (1500, 1500, False)])
async def test_the_hook_is_told_whether_the_output_was_cut(monkeypatch, length, kept, cut):
    store = SimpleNamespace(fire=AsyncMock())
    monkeypatch.setattr("kiro_crew.messaging.tool_hooks.observer_store", lambda *a, **k: store)
    hooks = ChannelTurnHooks(
        SimpleNamespace(session_id="acp-1"),
        ScriptHookTurn("sess-1", "do it"),
        TurnSpecHooks([], None, False, False),
    )
    text = "x" * length

    await hooks.on_tool_result(
        SimpleNamespace(
            tool_call_id="tc-1",
            tool_final=True,
            tool_status=None,
            tool_output=text,
            tool_name="",
            mcp_server_name="",
        )
    )

    assert store.fire.call_args.kwargs["tool_response"] == {
        "output": text[:kept],
        "truncated": cut,
        "output_length": length,
    }
