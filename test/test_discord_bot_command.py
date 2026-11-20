"""Discord ``!bot``: list the Bots whose own thread is open, or connect one by name.

A pick goes through the same owner-only, DM-only binding as ``!sessions``.
"""

from __future__ import annotations

from types import SimpleNamespace

import discord_sessions_helpers
import pytest
from chat_test_helpers import _make_state
from discord_sessions_helpers import (
    _ConversationLog,
    _dispatcher,
    _interaction,
    _message,
    _picker_button,
)

from kiro_crew.discord.commands import parse_command
from kiro_crew.messaging.link import ChannelLink

_isolated_home = discord_sessions_helpers._isolated_home

BOT_KEY = "dashboard:member-radar"


def _bot_dm(tmp_path, allowed=frozenset({"u1"})):
    """A dispatcher whose dashboard holds one Bot thread and one ordinary chat."""
    state = _make_state(tmp_path)
    bot = state.get_or_create_slot("member-radar", mode="member")
    bot.title, bot._titled, bot.agent = "Radar", True, "radar"
    chat = state.get_or_create_slot("chat-1")
    chat.title, chat._titled = "Launch plan", True
    log = _ConversationLog(
        [{"key": "dashboard_chat-1", "title": "Launch plan", "memory_mode": "persistent"}],
        {BOT_KEY: [{"role": "assistant", "content": "worker report read"}], "dashboard:chat-1": []},
    )
    dispatcher, client, sessions = _dispatcher(set(allowed), log)
    dispatcher._session_resume.dashboard_state = SimpleNamespace(
        _slots=state._slots, push_slots_update=lambda: None
    )
    return dispatcher, client, sessions


def _labels(client) -> list[str]:
    _, components = client.sent[-1]
    return [button["label"] for row in components or [] for button in row["components"]]


def test_bot_command_aliases() -> None:
    for text in ("!bot", "/bot", "!bots", "!bot radar"):
        assert parse_command(text) == "bot"


@pytest.mark.asyncio
async def test_bot_lists_only_bot_threads_and_a_press_binds_like_sessions(tmp_path) -> None:
    dispatcher, client, sessions = _bot_dm(tmp_path)

    await dispatcher.handle_message(_message("!bot"))

    assert _labels(client) == ["1. Radar"]
    custom_id, message_id = _picker_button(client)
    await dispatcher.on_interaction(_interaction(custom_id, message_id))
    assert sessions.mirror_links[BOT_KEY] == ChannelLink("discord", channel_id="c1")
    assert sessions.mirror_links[BOT_KEY].principal == "u1"
    assert dispatcher._session_resume.resumed_session("c1") == BOT_KEY


@pytest.mark.asyncio
async def test_bot_with_a_name_connects_at_once_and_routes_the_next_message(tmp_path) -> None:
    dispatcher, client, sessions = _bot_dm(tmp_path)

    await dispatcher.handle_message(_message("!bot RAD"))

    assert dispatcher._session_resume.resumed_session("c1") == BOT_KEY
    assert any("Resumed: Radar" in text for _, text, _ in client.edits)
    await dispatcher.handle_message(_message("how are the workers doing?"))
    assert sessions.last_key == BOT_KEY


@pytest.mark.asyncio
async def test_bot_with_no_match_binds_nothing(tmp_path) -> None:
    dispatcher, client, sessions = _bot_dm(tmp_path)

    await dispatcher.handle_message(_message("!bot launch"))

    assert "No Bot named `launch`" in client.sent[-1][0]
    assert sessions.mirror_links == {}


@pytest.mark.asyncio
async def test_a_bot_thread_with_no_transcript_yet_is_not_offered(tmp_path) -> None:
    dispatcher, client, sessions = _bot_dm(tmp_path)
    dispatcher._session_resume.conv_log.messages.pop(BOT_KEY)

    await dispatcher.handle_message(_message("!bot"))
    assert "send it a message" in client.sent[-1][0]
    await dispatcher.handle_message(_message("!bot radar"))
    assert "No Bot named `radar`" in client.sent[-1][0]
    assert sessions.mirror_links == {}


@pytest.mark.asyncio
async def test_bot_is_owner_only_and_dm_only(tmp_path) -> None:
    dispatcher, client, sessions = _bot_dm(tmp_path, allowed={"u1", "u2"})
    await dispatcher.handle_message(_message("!bot radar"))
    assert "requires exactly one" in client.sent[-1][0]

    dispatcher, client, sessions = _bot_dm(tmp_path)
    await dispatcher.handle_message(_message("!bot radar", channel_id="t1", thread_id="t1"))
    assert "only in a direct message" in client.sent[-1][0]
    assert sessions.mirror_links == {}
