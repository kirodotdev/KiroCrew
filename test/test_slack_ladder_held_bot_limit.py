"""The native Slack ladder never auto-approves a call under a held Bot limit.

``mate_held_rules``: a moved Bot's limit its worker could not carry exactly. Under
``auto`` approval, a trusted session or YOLO, such a call still reaches the
interactive prompt; a call no held limit covers is auto-approved as before.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mate_held_rules
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.slack import handler


def _turn() -> handler._SlackTurn:
    turn = handler._SlackTurn.__new__(handler._SlackTurn)
    for name, value in {
        "slack": MagicMock(),
        "channel": "C1",
        "reply_ts": "1.2",
        "approval_mode": handler.APPROVAL_AUTO,
        "context_builder": None,
        "session_key": "slack:1.2",
        "_agent": "zxc",
        "answer": MagicMock(
            accumulated="",
            prepare_for_approval=AsyncMock(),
            resume_after_approval=AsyncMock(),
            on_tool_rejected=AsyncMock(),
        ),
        "task": MagicMock(),
    }.items():
        setattr(turn, name, value)
    return turn


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_auto_mode_asks_for_a_held_call_and_approves_the_rest(monkeypatch, held):
    monkeypatch.setattr(mate_held_rules, "held_for", lambda *a, **k: ["deny shell"] if held else [])
    monkeypatch.setattr(handler, "sel", MagicMock())
    prompt = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_request_approval", prompt)
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock()
    event = LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: ls", request_id="r1")

    await handler._answer_permission_request(_turn(), client, event)

    if held:
        client.approve_tool.assert_not_awaited()
        prompt.assert_awaited_once()
    else:
        client.approve_tool.assert_awaited_once_with("r1")
        prompt.assert_not_awaited()
