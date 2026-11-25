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
        (True, False, "unmarked_backend", ""),
        (False, False, "", ""),
        (True, True, "", ""),
        (True, False, "sub_session_id", ""),
        (True, False, "native_child_call", ""),
        (True, False, "queued_context", ""),
        (True, False, "subagent_notice", ""),
        (True, False, "replayed_queued_message", ""),
        (True, False, "replayed_cancelled_message", ""),
        (True, False, "responsive_recovery", ""),
    ],
    ids=[
        "shared-users-turn",
        "a-backend-that-does-not-mark-child-calls",
        "unstamped-turn",
        "code-channel-session",
        "a-childs-call",
        "a-native-childs-call",
        "context-someone-else-queued",
        "a-sub-agents-notice",
        "cold-start-replay-carries-a-queued-message",
        "cold-start-replay-carries-a-cancelled-queued-message",
        "an-interrupted-request-kept-for-recovery",
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
    # A backend whose transport marks its children's calls, unless the case says not.
    asked.child_calls_marked = child != "unmarked_backend"
    if child == "sub_session_id":
        # A background child's call, routed to whichever turn runs the slot now.
        asked.sub_session_id = "child-1"
    elif child == "native_child_call":
        asked.native_child_call = True
    elif child == "queued_context":
        # The owner queued an instruction for the slot (``/context``); the shared
        # user's message then opens the turn that drains it.
        from kiro_crew.dashboard.chat_api.pending_context import _build_pending_context_entry

        entry, refused = _build_pending_context_entry(
            slot, "send the prepared release email", "api", False, None
        )
        assert refused is None and slot.append_pending_context(entry)
    elif child == "subagent_notice":
        # A sub-agent the owner started reports back into the shared user's turn.
        slot._pending_subagent_failures.append("A sub-agent result is waiting on disk.")
    elif child == "responsive_recovery":
        # The owner's interrupted request, kept for the next turn on this session.
        from kiro_crew.dashboard.chat_utils import effective_session_key
        from kiro_crew.dashboard.responsive_chat import ResponsiveTurn

        slot._responsive = ResponsiveTurn(
            session_key=effective_session_key(slot),
            recovery=[{"role": "user", "content": "delete the old release branch"}],
        )
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
    replay = child.startswith("replay")
    # A cold start replays history from the log and the slot's live tail. An owner's
    # reply queued behind this turn (``route_linked_thread`` appends its row first)
    # is in it, and stays in it when the owner cancels the queue entry, so the
    # queue's length says nothing about what the replay carries.
    queued = 1 if child == "replayed_queued_message" else 0
    with (
        _patch_stats(),
        patch.object(chat_runner, "post_linked_approval", AsyncMock(side_effect=_mirror)),
        patch.object(chat_runner, "code_channel_post_thread", return_value=thread),
        patch.object(
            chat_runner,
            "_session_replay_history",
            AsyncMock(return_value="[history] owner: delete the old branch" if replay else ""),
        ),
        patch.object(type(slot), "queue_depth", PropertyMock(return_value=queued)),
    ):
        runner = asyncio.ensure_future(_turn())
        try:
            # Bounded below the suite timeout: a mirror that never answers fails here.
            await asyncio.wait_for(asyncio.shield(runner), timeout=60)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
    assert seen == {"requester": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("reason, expected", [("use staging instead", ""), ("", FRIEND)])
async def test_a_typed_rejection_reason_takes_the_turn_back_from_its_requester(
    tmp_path, reason: str, expected: str
) -> None:
    """The owner rejects a call with a reason on the dashboard card: those words
    reach the model, so the turn stops answering to the shared opener. A plain
    rejection (no words) leaves it as it was."""
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
    asked.takes_rejection_reason = True
    _set_stream(client, [asked, _complete_event()])
    seen: dict[str, str] = {}

    async def _reject(*_a, **_k):
        seen["at_reject"] = turn_requester(slot)

    client.reject_tool = AsyncMock(side_effect=_reject)

    async def _mirror(*_a, **kw):
        if reason:
            slot.set_rejection_reason(str(asked.request_id), kw["future"], reason)
        kw["future"].set_result("rejected")
        return "1700000000.000200"

    async def _turn() -> None:
        slot.task = asyncio.current_task()
        note_turn_requester(slot.task, FRIEND)
        await _run_chat(state, slot, "hello")

    with (
        _patch_stats(),
        patch.object(chat_runner, "post_linked_approval", AsyncMock(side_effect=_mirror)),
        patch.object(chat_runner, "code_channel_post_thread", return_value=slot._slack_thread_ts),
    ):
        runner = asyncio.ensure_future(_turn())
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=60)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
    assert seen == {"at_reject": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [("answered", ""), ("dismissed", FRIEND)])
async def test_an_answered_question_card_takes_the_turn_back_from_its_requester(
    monkeypatch, status: str, expected: str
) -> None:
    """The agent's blocking ``ask_question`` gets someone's answer as tool output:
    from then on the linked turn answers to the owner alone."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import ask_question as ask_routes

    slot = MagicMock(key="chat-1")
    slot.task = asyncio.get_running_loop().create_future()
    note_turn_requester(slot.task, FRIEND)
    state = MagicMock(_slots={"chat-1": slot}, _agent_asks={})
    state.wait_agent_ask = AsyncMock(return_value={"status": status, "answers": {}})
    monkeypatch.setattr(ask_routes, "_attested_session", AsyncMock(return_value="dashboard:chat-1"))
    app = web.Application()
    app["state"] = state
    request = make_mocked_request(
        "POST", "/api/agent-ask/a1/wait", app=app, match_info={"ask_id": "a1"}
    )
    response = await ask_routes.api_agent_ask_wait(request)
    assert response.status == 200
    assert turn_requester(slot) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("action, expected", [("accept", ""), ("decline", FRIEND)])
async def test_an_answered_elicitation_takes_the_turn_back_from_its_requester(
    tmp_path, action: str, expected: str
) -> None:
    """The owner types an answer into the engine's form card on the dashboard:
    those words reach the turn. A decline carries none."""
    import json

    from dashboard_approval_helpers import (
        _complete_event,
        _context_builder,
        _make_slot,
        _make_state,
        _patch_stats,
        _set_stream,
    )

    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import LLMEvent

    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot()
    asked = LLMEvent(kind="elicitation_request", request_id="el-1", title="Pick")
    asked.elicitation = {
        "mode": "form",
        "message": "Pick",
        "fields": [{"name": "confirm", "kind": "boolean", "required": True}],
    }
    _set_stream(client, [asked, _complete_event()])
    seen: dict[str, str] = {}

    async def _answered(*_a, **_k):
        seen["at_answer"] = turn_requester(slot)

    client.answer_elicitation = AsyncMock(side_effect=_answered)
    register = type(slot).register_approval

    def _register_and_answer(self, request_id, fut, row):
        register(self, request_id, fut, row)

        def _answer() -> None:
            # What the dashboard's answer route writes, then the resolve.
            if action == "accept":
                meta = json.loads(row["cls"])
                row["cls"] = json.dumps({**meta, "answer": {"confirm": True}})
            fut.set_result(action)

        asyncio.get_running_loop().call_soon(_answer)

    async def _turn() -> None:
        slot.task = asyncio.current_task()
        note_turn_requester(slot.task, FRIEND)
        await _run_chat(state, slot, "hello")

    with (
        _patch_stats(),
        patch.object(type(slot), "register_approval", _register_and_answer),
    ):
        runner = asyncio.ensure_future(_turn())
        try:
            await asyncio.wait_for(asyncio.shield(runner), timeout=60)
        finally:
            if not runner.done():
                runner.cancel()
                await asyncio.gather(runner, return_exceptions=True)
    assert seen == {"at_answer": expected}


@pytest.mark.asyncio
async def test_forget_without_a_turn_is_a_no_op() -> None:
    slot = MagicMock(task=None)
    forget_turn_requester(slot)
    assert turn_requester(slot) == ""
