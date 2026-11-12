"""An OPTIONS answer (``interpret_commands=False``) is the agent's answer, never a command."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.chat_delivery import COMMANDS_OFF_META_KEY
from kiro_crew.slack import handler
from kiro_crew.slack.handler_runtime import dispatch

pytestmark = pytest.mark.asyncio


def _turn(text: str, *, interpret_commands: bool) -> MagicMock:
    turn = MagicMock()
    turn.text = text
    turn.interpret_commands = interpret_commands
    turn.route_pinned = False
    turn.target_slot_name = None
    turn.asker_key = None
    turn.action_context = None
    turn.context_builder = None
    turn.channel_agent = None
    turn.channel, turn.user_id = "C1", "UOWNER"
    turn.thread_ts = turn.msg_ts = turn.reply_ts = "100.000001"
    turn.session_key = "slack:100.000001"
    turn.slack = MagicMock()
    turn.slack.post_message = AsyncMock()
    turn.sessions = MagicMock()
    turn.sessions.get_session_for_thread = MagicMock(return_value=None)
    return turn


async def _dispatch(turn: MagicMock) -> tuple[bool, dict[str, AsyncMock]]:
    modifiers = AsyncMock(side_effect=lambda text, cmd, *_a: (text, cmd, False))
    bang, keyword = AsyncMock(return_value=True), AsyncMock(return_value=True)
    linked = AsyncMock(return_value=False)
    with (
        patch.object(handler, "maybe_route_linked_thread", linked),
        patch.object(handler, "expire_slack_options", AsyncMock()),
        patch.object(handler, "get_dashboard_state", lambda: None),
        patch.object(handler, "_get_default_agent", lambda: None),
        patch.object(handler, "maybe_apply_privacy_modifiers", modifiers),
        patch.object(handler, "_route_bang_command", bang),
        patch.object(handler, "maybe_handle_keyword_command", keyword),
    ):
        started = await dispatch._dispatch_before_turn(turn)
    assert linked.await_args.kwargs["interpret_commands"] is turn.interpret_commands
    return started, {"modifiers": modifiers, "bang": bang, "keyword": keyword}


@pytest.mark.parametrize(
    "label", ["!stop", "!incognito", "status", "sessions", "spawn tidy the repo"]
)
async def test_an_options_answer_starts_a_turn_with_its_label(label: str) -> None:
    turn = _turn(label, interpret_commands=False)
    started, commands = await _dispatch(turn)
    assert started is True
    assert [name for name, fn in commands.items() if fn.await_count] == []
    turn.slack.post_message.assert_not_awaited()


async def test_a_typed_message_still_runs_its_command() -> None:
    started, commands = await _dispatch(_turn("!stop", interpret_commands=True))
    assert started is False
    commands["bang"].assert_awaited_once()


def _slot(*, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.running = running
    slot.key = "chat-9-1"
    slot.window_pending = False
    slot._has_reader = False
    return slot


async def _route_to_slot(
    text: str, run: AsyncMock, *, interpret_commands: bool, slot: MagicMock | None = None
) -> bool:
    state = MagicMock()
    state._background_tasks = set()
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", run),
        patch("kiro_crew.dashboard.session_control.containment_meta", return_value={}),
    ):
        return await handler.maybe_route_linked_thread(
            text,
            "slack:300.000001",
            "UOWNER",
            "C0CODE",
            AsyncMock(),
            "300.000001",
            target_slot=slot or _slot(),
            route_pinned=True,
            interpret_commands=interpret_commands,
        )


async def test_a_linked_thread_delivers_a_bang_label_to_its_slot() -> None:
    run = AsyncMock()
    assert await _route_to_slot("!stop", run, interpret_commands=False) is True
    assert run.call_args.args[2] == "!stop"
    typed = AsyncMock()
    assert await _route_to_slot("!stop", typed, interpret_commands=True) is False
    typed.assert_not_awaited()


@pytest.mark.parametrize("interpret_commands", [False, True])
async def test_a_linked_slot_reads_a_slash_label_as_content(interpret_commands: bool) -> None:
    """The slot runs a ``/`` command itself, so the answer carries commands-off into
    its turn when idle and onto its queued entry when busy; a typed one does not."""
    run = AsyncMock()
    assert await _route_to_slot("/clear", run, interpret_commands=interpret_commands) is True
    assert run.call_args.kwargs["_commands_off"] is (not interpret_commands)
    busy = _slot(running=True)
    await _route_to_slot("/clear", AsyncMock(), interpret_commands=interpret_commands, slot=busy)
    meta = busy.queue_append.call_args.kwargs["meta"]
    assert (COMMANDS_OFF_META_KEY in meta) is (not interpret_commands)


async def test_a_linked_slot_on_another_agent_refuses_an_agent_switch_label() -> None:
    """Unpinned: a typed ``/agent <the channel's agent>`` switches the slot, but an
    answer never runs as that switch, so it is refused like any other mismatch."""

    async def routed(*, interpret_commands: bool) -> MagicMock:
        slot, slack = _slot(), MagicMock()
        slack.post_message = AsyncMock()
        state = MagicMock()
        state._background_tasks = set()
        state.get_linked_slot = MagicMock(return_value=slot)
        with (
            patch.object(handler, "_dashboard_state", state),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "linked_slot_channel_mismatch", AsyncMock(return_value="crew-a")),
            patch("kiro_crew.dashboard.chat._run_chat", AsyncMock()),
            patch.object(handler, "append_and_surface") as appended,
        ):
            await handler.maybe_route_linked_thread(
                "/agent crew-a",
                "slack:300.000001",
                "UOWNER",
                "C0CODE",
                slack,
                "300.000001",
                interpret_commands=interpret_commands,
            )
        return appended

    assert (await routed(interpret_commands=False)).call_count == 0
    assert (await routed(interpret_commands=True)).call_count == 1


async def test_a_closed_owner_on_another_agent_refuses_a_bang_label() -> None:
    """Unpinned (an untokened card): the label would run as a turn on the closed
    dashboard owner's agent, so the pinned channel's agent check applies to it."""
    state = MagicMock()
    state.sessions.get_session_for_thread = MagicMock(return_value="dashboard:chat-1")

    async def refused(*, interpret_commands: bool) -> bool:
        slack = MagicMock()
        slack.post_message = AsyncMock()
        with (
            patch.object(handler, "_dashboard_state", state),
            patch.object(handler, "channel_pinned_agent", lambda _c: "crew-a"),
            patch.object(handler, "is_allowed_user", return_value=True),
            patch.object(handler, "_recorded_selection", side_effect=OSError("unreadable")),
        ):
            return await handler.closed_dashboard_owner_refused(
                "!stop",
                "slack:300.000001",
                "UOWNER",
                "C0CODE",
                slack,
                "300.000001",
                interpret_commands=interpret_commands,
            )

    assert await refused(interpret_commands=False) is True
    assert await refused(interpret_commands=True) is False
