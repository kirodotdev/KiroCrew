"""``ChannelTurnHooks.on_tool_result`` bounds ``tool_response.output`` to 2000
characters and reports the cut via ``truncated``/``output_length`` (issue
#38593): a hook used to get a bare ``[:2000]`` slice with no signal a cut
happened.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.agent_sdk.spec_hooks import TurnSpecHooks
from kiro_crew.messaging.script_hooks import ScriptHookTurn
from kiro_crew.messaging.tool_hooks import ChannelTurnHooks


def _hooks(monkeypatch, store):
    turn = ScriptHookTurn("sess-1", "do it")
    spec = TurnSpecHooks([], None, False, False)
    hooks = ChannelTurnHooks(SimpleNamespace(session_id="acp-1"), turn, spec)
    monkeypatch.setattr("kiro_crew.messaging.tool_hooks.observer_store", lambda *a, **k: store)
    return hooks


def _result_event(tool_output: str) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call_id="tc-1",
        tool_final=True,
        tool_status=None,
        tool_output=tool_output,
        tool_name="",
        mcp_server_name="",
    )


@pytest.mark.asyncio
async def test_a_long_output_is_cut_and_the_cut_is_reported(monkeypatch):
    store = SimpleNamespace(fire=AsyncMock())
    hooks = _hooks(monkeypatch, store)
    text = "x" * 7901

    await hooks.on_tool_result(_result_event(text))

    _, kwargs = store.fire.call_args
    response = kwargs["tool_response"]
    assert len(response["output"]) == 2000
    assert response["truncated"] is True
    assert response["output_length"] == 7901


@pytest.mark.asyncio
async def test_a_short_output_arrives_unchanged_and_untruncated(monkeypatch):
    store = SimpleNamespace(fire=AsyncMock())
    hooks = _hooks(monkeypatch, store)
    text = "y" * 1500

    await hooks.on_tool_result(_result_event(text))

    _, kwargs = store.fire.call_args
    response = kwargs["tool_response"]
    assert response["output"] == text
    assert response["truncated"] is False
    assert response["output_length"] == 1500
