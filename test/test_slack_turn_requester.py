"""Who opened a linked dashboard slot's running turn (``slack.turn_requester``).

A shared user (``slack.shared_access``) may answer the mirrored tool prompts of a
turn their own Slack message opened. The record is fail-closed: only a turn that
message started is stamped, and any text steered into it drops the stamp.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.slack.turn_requester import (
    forget_turn_requester,
    note_turn_requester,
    turn_requester,
)

OWNER = "UOWNER0001"
FRIEND = "UFRIEND001"


def _slot(running: bool) -> MagicMock:
    slot = MagicMock()
    type(slot).running = PropertyMock(return_value=running)
    slot.key = "slot1"
    slot._queue = []
    slot.queue_append = MagicMock(return_value="q1")
    return slot


async def _route(slot: MagicMock, user: str, turn: asyncio.Event) -> None:
    """Route *user*'s Slack reply into *slot*, its turn parked on *turn*."""
    from kiro_crew.slack import handler

    async def _parked_turn(*_a, **_k):
        await turn.wait()

    slack = MagicMock(post_message=AsyncMock(), post_blocks=AsyncMock())
    ds = MagicMock()
    ds.get_linked_slot = MagicMock(return_value=slot)
    ds._background_tasks = set()
    with (
        patch.object(handler, "_dashboard_state", ds),
        patch.object(handler, "is_allowed_user", side_effect=lambda u: u == OWNER),
        patch.object(handler, "is_shared_user", side_effect=lambda u: u == FRIEND),
        patch.object(handler, "is_shared_listed", side_effect=lambda u: u == FRIEND),
        patch.object(handler, "shared_user_refused_for_session", AsyncMock(return_value=False)),
        patch("kiro_crew.slack.code_channels.routing.shared_user_refused_in", return_value=False),
        patch(
            "kiro_crew.slack.code_channels.routing.shared_user_refused_unbound",
            AsyncMock(return_value=False),
        ),
        patch("kiro_crew.dashboard.chat._run_chat", side_effect=_parked_turn),
    ):
        outcome = await handler.route_linked_thread("hello", "slack:t1", user, "C1", slack, "t1")
    assert outcome is handler.LinkedRoute.ROUTED


@pytest.mark.asyncio
async def test_a_shared_users_message_that_opens_the_turn_names_them() -> None:
    slot, turn = _slot(running=False), asyncio.Event()
    await _route(slot, FRIEND, turn)
    assert turn_requester(slot) == FRIEND
    turn.set()
    await slot.task
    assert turn_requester(slot) == ""  # the turn is over: nobody is named


@pytest.mark.asyncio
async def test_the_owners_message_names_nobody() -> None:
    slot, turn = _slot(running=False), asyncio.Event()
    await _route(slot, OWNER, turn)
    assert turn_requester(slot) == ""
    turn.set()
    await slot.task


@pytest.mark.asyncio
async def test_a_queued_message_names_nobody_on_the_running_turn() -> None:
    """The running turn is someone else's; the queued text runs as its own task."""
    running_turn = asyncio.Event()
    slot = _slot(running=True)
    slot.task = asyncio.ensure_future(running_turn.wait())
    await _route(slot, FRIEND, asyncio.Event())
    slot.queue_append.assert_called_once()
    assert turn_requester(slot) == ""
    running_turn.set()
    await slot.task


@pytest.mark.asyncio
async def test_any_text_steered_into_the_turn_drops_the_name() -> None:
    from kiro_crew.dashboard import chat_delivery

    slot = _ChatSlot("chat-1")
    turn = asyncio.Event()
    slot.task = asyncio.ensure_future(turn.wait())
    note_turn_requester(slot.task, FRIEND)
    client = MagicMock(supports_steer=True, steer_needs_loss_recovery=False)
    client.steer = AsyncMock(return_value=None)
    slot._acp_client = client
    assert turn_requester(slot) == FRIEND
    await chat_delivery.steer_into_running_turn(MagicMock(), slot, "and also", user_origin=True)
    assert turn_requester(slot) == ""
    turn.set()
    await slot.task


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stamped, code_channel, child, expected",
    [
        (True, False, "", FRIEND),
        (False, False, "", ""),
        (True, True, "", ""),
        (True, False, "sub_session_id", ""),
        (True, False, "native_child_call", ""),
    ],
    ids=[
        "shared-users-turn",
        "unstamped-turn",
        "code-channel-session",
        "a-childs-call",
        "a-native-childs-call",
    ],
)
async def test_the_linked_card_carries_the_turns_requester(
    tmp_path, stamped: bool, code_channel: bool, child: str, expected: str
) -> None:
    from dashboard_approval_helpers import (
        _complete_event,
        _context_builder,
        _make_slot,
        _make_state,
        _patch_stats,
        _permission_event,
        _set_stream,
    )

    from kiro_crew.dashboard import chat_runner
    from kiro_crew.dashboard.chat import _run_chat

    state, client = _make_state(tmp_path, context_builder=_context_builder())
    state.slack_client = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=("", ""))
    slot = _make_slot()
    slot._slack_linked = True
    slot._slack_channel = "C1"
    slot._slack_thread_ts = "1700000000.000100"
    asked = _permission_event()
    if child == "sub_session_id":
        # A background child's call, routed to whichever turn runs the slot now.
        asked.sub_session_id = "child-1"
    elif child == "native_child_call":
        asked.native_child_call = True
    _set_stream(client, [asked, _complete_event()])
    seen: dict[str, str] = {}

    async def _mirror(*_a, **kw):
        seen["requester"] = kw["requester"]
        kw["future"].set_result("approved")
        return "1700000000.000200"

    async def _turn() -> None:
        slot.task = asyncio.current_task()
        if stamped:
            note_turn_requester(slot.task, FRIEND)
        await _run_chat(state, slot, "hello")

    thread = None if code_channel else slot._slack_thread_ts
    with (
        _patch_stats(),
        patch.object(chat_runner, "post_linked_approval", AsyncMock(side_effect=_mirror)),
        patch.object(chat_runner, "code_channel_post_thread", return_value=thread),
    ):
        await asyncio.ensure_future(_turn())
    assert seen == {"requester": expected}


@pytest.mark.asyncio
async def test_forget_without_a_turn_is_a_no_op() -> None:
    slot = MagicMock(task=None)
    forget_turn_requester(slot)
    assert turn_requester(slot) == ""
