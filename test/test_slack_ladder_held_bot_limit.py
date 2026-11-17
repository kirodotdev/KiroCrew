"""The native Slack ladder never auto-approves a call under a held Bot limit.

``mate_held_rules``: a moved Bot's limit its worker could not carry exactly. On
every rung that would approve on its own (a hook grant by name, the spawn rung,
``auto`` approval, a trusted session, YOLO) such a call still reaches the
interactive prompt; a call no held limit covers is approved by that rung as before.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import mate_held_rules
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.slack import handler


def _turn(approval_mode: str, context_builder=None) -> handler._SlackTurn:
    turn = handler._SlackTurn.__new__(handler._SlackTurn)
    for name, value in {
        "slack": MagicMock(),
        "channel": "C1",
        "reply_ts": "1.2",
        "approval_mode": approval_mode,
        "context_builder": context_builder,
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


def _rung(monkeypatch, rung: str) -> handler._SlackTurn:
    """A turn on which exactly *rung* would approve the call on its own."""
    monkeypatch.setattr(handler, "is_yolo_mode", lambda: rung == "yolo")
    monkeypatch.setattr(handler, "is_session_trusted", lambda key: rung == "trust")
    monkeypatch.setattr(handler, "_should_auto_approve_spawn", lambda cb, ev: rung == "spawn")
    builder = None
    if rung == "hook":
        verdict = SimpleNamespace(action=handler.TOOL_AUTO_APPROVE, reason="")

        async def gate(fn, *args, **kwargs):
            return verdict

        monkeypatch.setattr(handler, "run_in_tool_gate_pool", gate)
        monkeypatch.setattr(handler.name_grant, "refusal_for_event", AsyncMock(return_value=None))
        builder = MagicMock()
    mode = handler.APPROVAL_AUTO if rung == "auto" else handler.APPROVAL_INTERACTIVE
    return _turn(mode, builder)


@pytest.mark.asyncio
@pytest.mark.parametrize("rung", ["hook", "spawn", "auto", "trust", "yolo"])
@pytest.mark.parametrize("held", [True, False])
async def test_every_auto_rung_asks_for_a_held_call_and_approves_the_rest(monkeypatch, rung, held):
    monkeypatch.setattr(mate_held_rules, "held_for", lambda *a, **k: ["deny shell"] if held else [])
    monkeypatch.setattr(handler, "sel", MagicMock())
    prompt = AsyncMock(return_value=False)
    monkeypatch.setattr(handler, "_request_approval", prompt)
    client = MagicMock()
    client.approve_tool = AsyncMock(return_value=True)
    client.reject_tool = AsyncMock()
    event = LLMEvent(kind=EVENT_PERMISSION_REQUEST, title="Run: ls", request_id="r1")

    await handler._answer_permission_request(_rung(monkeypatch, rung), client, event)

    if held:
        client.approve_tool.assert_not_awaited()
        prompt.assert_awaited_once()
    else:
        client.approve_tool.assert_awaited_once_with("r1")
        prompt.assert_not_awaited()
