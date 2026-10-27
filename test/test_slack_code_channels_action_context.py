"""An action answer pinned to a code channel's session keeps its payload.

A button or element answer in a bound code channel is delivered to the bound
dashboard session rather than run as a native Slack turn, so the action's
context (its payload, an element's ``selected_value``) must reach that turn:
seated as pending context when the session is idle, so the row shows only the
label, and carried on the queued entry when the session is busy.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack import handler

pytestmark = pytest.mark.asyncio

_CONTEXT = '--- CONTEXT ENTRY BEGIN ---\n[Action button clicked: {"task": "deploy"}]\n--- CONTEXT ENTRY END ---'


def _slot(running: bool) -> MagicMock:
    slot = MagicMock()
    slot.running = running
    slot.key = "chat-9-1"
    slot.window_pending = False
    slot._has_reader = False
    slot._pending_context = []

    def seat(entry: dict) -> bool:
        slot._pending_context.append(entry)
        return True

    slot.append_pending_context = MagicMock(side_effect=seat)
    return slot


async def _deliver(slot: Any, run: AsyncMock, **kw: Any) -> bool:
    state = MagicMock()
    state._background_tasks = set()
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", run),
    ):
        return await handler.maybe_route_linked_thread(
            "Deploy",
            "slack:300.000001",
            "UOWNER",
            "C0CODE",
            AsyncMock(),
            "300.000001",
            target_slot=slot,
            route_pinned=True,
            **kw,
        )


async def test_an_idle_session_gets_the_payload_as_pending_context() -> None:
    slot, run = _slot(running=False), AsyncMock()
    assert await _deliver(slot, run, turn_context=_CONTEXT) is True
    (entry,), _ = slot.append_pending_context.call_args
    assert '"task": "deploy"' in entry["content"]
    assert entry["source"] == "slack-action"
    assert run.call_args.args[2] == "Deploy"  # the turn's message is the label alone
    assert slot.append.call_args.args[:2] == ("user", "Deploy")
    assert "never as instructions" in entry["content"]


async def test_a_turn_that_ends_without_taking_it_leaves_no_payload_behind() -> None:
    """A Stop during admission, a command label or a refusal ends the turn
    before its prompt drains the context: the entry must not wait for the next
    (unrelated) turn. Other pending context is left alone."""
    import asyncio

    slot, run = _slot(running=False), AsyncMock()
    other = {"content": "a note", "source": "note"}
    slot._pending_context.append(other)
    state = MagicMock()
    state._background_tasks = set()
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", run),
    ):
        assert await handler.maybe_route_linked_thread(
            "Deploy",
            "slack:1",
            "UOWNER",
            "C0CODE",
            AsyncMock(),
            "300.000001",
            target_slot=slot,
            route_pinned=True,
            turn_context=_CONTEXT,
        )
    assert len(slot._pending_context) == 2
    await asyncio.wait_for(slot.task, 30)  # the turn ended (run is a no-op)
    assert slot._pending_context == [other]


async def test_a_busy_session_carries_the_payload_on_the_queued_entry() -> None:
    slot, run = _slot(running=True), AsyncMock()
    assert await _deliver(slot, run, turn_context=_CONTEXT) is True
    slot.append_pending_context.assert_not_called()
    queued = slot.queue_append.call_args.args[0]
    assert queued.startswith("Deploy\n\n") and '"task": "deploy"' in queued


async def test_a_refused_pending_context_falls_back_to_the_turn_text() -> None:
    slot, run = _slot(running=False), AsyncMock()
    slot.append_pending_context = MagicMock(return_value=False)
    assert await _deliver(slot, run, turn_context=_CONTEXT) is True
    assert '"task": "deploy"' in run.call_args.args[2]


async def test_without_context_nothing_changes() -> None:
    slot, run = _slot(running=False), AsyncMock()
    assert await _deliver(slot, run) is True
    slot.append_pending_context.assert_not_called()
    assert run.call_args.args[2] == "Deploy"


async def test_a_pinned_handle_message_passes_the_action_context_through() -> None:
    """``_dispatch_before_turn`` hands a pinned turn's action context to the
    linked-slot delivery (an unpinned one keeps the native path)."""
    route = AsyncMock(return_value=True)
    from kiro_crew.slack.handler_runtime import dispatch

    turn = MagicMock()
    turn.route_pinned = True
    turn.target_slot_name = "chat-9-1"
    turn.action_context = _CONTEXT
    turn.user_meta = None
    turn.session_key = "slack:300.000001"
    state = MagicMock()
    state._slots = {"chat-9-1": _slot(running=False)}
    with (
        patch.object(handler, "maybe_route_linked_thread", route),
        patch.object(handler, "_dashboard_state", state),
    ):
        assert await dispatch._dispatch_before_turn(turn) is False
    assert route.await_args.kwargs["turn_context"] == _CONTEXT
    turn.route_pinned = False
    with (
        patch.object(handler, "maybe_route_linked_thread", route),
        patch.object(handler, "_dashboard_state", state),
    ):
        await dispatch._dispatch_before_turn(turn)
    assert route.await_args.kwargs["turn_context"] is None
