"""A top-level Slack message that ends with the thread icon only opens a thread.

``kiro_crew.slack.thread_opener`` holds the rule; ``_route_message`` applies it after
every check that decides who is heard, so the opener starts no turn and Crew writes
nothing to Slack. The thread then works from its first reply on: thread-follow admits
the reply, and a dashboard chat the reply goes to gets the opener in front of it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_channel_activation import _make_orch

from kiro_crew.config.loader import (
    ACTIVATION_ALWAYS,
    ACTIVATION_OBSERVE,
    ChannelConfig,
    KiroCrewConfig,
    MessagingConfig,
)
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.slack import thread_opener
from kiro_crew.slack.events import SeenCache, _route_message

#: The only Slack calls routing makes before the rule runs; both are reads.
_READS = {"ensure_channel_team", "get_user_info"}


def _event(text: str, *, channel: str = "C1", ts: str = "100.1", thread_ts: str = "") -> dict:
    event = {"user": "U1", "channel": channel, "text": text, "ts": ts, "team": "T1"}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


async def _route(
    orch: MagicMock, event: dict, seen: SeenCache, *, mention: bool = False, **senders: bool
) -> AsyncMock:
    """Route *event* as the owner (or the sender kinds in *senders*); the native handler mock."""
    patches = {"is_allowed_user": True, **senders}
    with patch("kiro_crew.slack.events.handle_message", new_callable=AsyncMock) as handled:
        with (
            patch(
                "kiro_crew.slack.events.is_allowed_user", return_value=patches["is_allowed_user"]
            ),
            patch(
                "kiro_crew.slack.events.is_shared_user",
                return_value=patches.get("is_shared_user", False),
            ),
            patch(
                "kiro_crew.slack.events.is_shared_listed",
                return_value=patches.get("is_shared_listed", False),
            ),
        ):
            await _route_message(orch, dict(event), seen, is_mention=mention)
            await asyncio.gather(*list(orch._handler_tasks), return_exceptions=True)
    return handled


def _assert_silent(orch: MagicMock) -> None:
    """Nothing started, queued or recorded, and nothing written to Slack."""
    assert {call[0].split(".")[0] for call in orch.slack.mock_calls} <= _READS
    orch.channel_history.push.assert_not_called()
    orch.sessions.enqueue.assert_not_called()
    assert orch._handler_tasks == set()
    assert orch._session_tasks == {}
    assert orch._pending_queue == {}


@pytest.mark.parametrize(
    ("text", "body"),
    [
        ("topic :thread:", "topic :thread:"),
        ("topic \U0001f9f5", "topic \U0001f9f5"),
        ("topic :thread:  \n\n", "topic :thread:"),
        ("line one\nline two :thread:", "line one\nline two :thread:"),
        (":thread:", ":thread:"),
        ("<@UBOT> topic :thread:", "topic :thread:"),
        ("<@UBOT> :thread:", ":thread:"),
    ],
)
def test_a_message_ending_with_the_icon_is_an_opener(text: str, body: str) -> None:
    assert thread_opener.opener_text(text) == body


@pytest.mark.parametrize(
    "text",
    [
        "",
        "topic",
        ":thread: goes in the middle",
        "topic :thread: now",
        "!bot ops :thread:",
        "<@UBOT> !channel always :thread:",
        "!queue plan the launch :thread:",
    ],
)
def test_other_messages_and_commands_are_not_openers(text: str) -> None:
    assert thread_opener.opener_text(text) is None


_AGENT_CHANNEL = {"C1": ChannelConfig(activation=ACTIVATION_ALWAYS, agent="ops")}
_BOT_CHANNEL = {"C1": ChannelConfig(activation=ACTIVATION_ALWAYS, bot="ops")}
_OBSERVE_CHANNEL = {"C1": ChannelConfig(activation=ACTIVATION_OBSERVE)}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "channels", "text", "mention"),
    [
        ("D1", None, "plan the launch :thread:", False),
        ("C1", None, "<@UBOT> plan the launch :thread:", True),
        ("C1", {"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)}, "plan :thread:", False),
        ("C1", _AGENT_CHANNEL, "plan the launch \U0001f9f5", False),
        ("C1", _BOT_CHANNEL, "plan the launch :thread:\n", False),
        ("D1", None, ":thread:", False),
    ],
    ids=["dm", "mention", "always", "agent-channel", "bot-channel", "icon-only"],
)
async def test_an_opener_starts_no_turn_and_writes_nothing(
    channel: str, channels: dict | None, text: str, mention: bool
) -> None:
    orch = _make_orch(channels=channels)
    seen = SeenCache()

    handled = await _route(orch, _event(text, channel=channel), seen, mention=mention)

    handled.assert_not_called()
    _assert_silent(orch)
    assert thread_opener.opened(channel, "100.1")


@pytest.mark.asyncio
async def test_an_observe_channel_records_the_opener_and_starts_no_turn() -> None:
    """Observe mode records every message it reads before the rule runs, an opener too."""
    orch = _make_orch(channels=_OBSERVE_CHANNEL)
    event = _event("<@UBOT> plan the launch :thread:")

    handled = await _route(orch, event, SeenCache(), mention=True)

    handled.assert_not_called()
    orch.channel_history.push.assert_called_once()
    assert {call[0].split(".")[0] for call in orch.slack.mock_calls} <= _READS
    assert orch._handler_tasks == set()


@pytest.mark.asyncio
async def test_an_opener_starts_no_turn_on_the_transport_path() -> None:
    orch = _make_orch()
    orch._cfg = KiroCrewConfig(messaging=MessagingConfig(use_transport=True))

    with patch(
        "kiro_crew.slack.events.handle_message_transport", new_callable=AsyncMock
    ) as transport:
        handled = await _route(orch, _event("plan :thread:", channel="D1"), SeenCache())

    transport.assert_not_called()
    handled.assert_not_called()
    _assert_silent(orch)


@pytest.mark.asyncio
async def test_a_shared_users_opener_starts_no_turn() -> None:
    orch = _make_orch()

    handled = await _route(
        orch,
        _event("plan :thread:", channel="D1"),
        SeenCache(),
        is_allowed_user=False,
        is_shared_user=True,
        is_shared_listed=True,
    )

    handled.assert_not_called()
    _assert_silent(orch)


@pytest.mark.asyncio
async def test_a_refused_senders_opener_is_refused_as_before() -> None:
    orch = _make_orch()
    orch.slack.post_ephemeral = AsyncMock()

    handled = await _route(
        orch, _event("plan :thread:", channel="D1"), SeenCache(), is_allowed_user=False
    )

    handled.assert_not_called()
    orch.slack.post_ephemeral.assert_awaited_once()
    assert not thread_opener.opened("D1", "100.1")


@pytest.mark.asyncio
@pytest.mark.parametrize("first", [False, True], ids=["message-first", "app-mention-first"])
async def test_the_message_and_app_mention_pair_is_left_alone_once(first: bool) -> None:
    orch = _make_orch(channels={"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)})
    seen = SeenCache()
    event = _event("<@UBOT> plan the launch :thread:")

    handled = await _route(orch, event, seen, mention=first)
    handled_again = await _route(orch, event, seen, mention=not first)

    handled.assert_not_called()
    handled_again.assert_not_called()
    _assert_silent(orch)
    assert thread_opener.take_topic("C1", "100.1", "U1") == "plan the launch :thread:"


@pytest.mark.asyncio
async def test_an_opener_with_a_file_is_left_alone_and_the_file_is_not_fetched() -> None:
    orch = _make_orch()
    event = _event("see the chart :thread:", channel="D1")
    event["files"] = [{"id": "F1", "name": "chart.png", "mimetype": "image/png"}]

    with patch("kiro_crew.slack.events.process_slack_files", new_callable=AsyncMock) as fetched:
        handled = await _route(orch, event, SeenCache())

    fetched.assert_not_called()
    handled.assert_not_called()
    _assert_silent(orch)


@pytest.mark.asyncio
async def test_an_opener_is_not_queued_behind_a_running_turn() -> None:
    """A single-session DM keys every top-level message to one session, which may be busy."""
    orch = _make_orch()
    seen = SeenCache()

    with (
        patch("kiro_crew.slack.events._key_busy", return_value=True),
        patch("kiro_crew.slack.events.steer_if_busy", AsyncMock(return_value=False)),
        patch("kiro_crew.slack.events._mark_queued", new_callable=AsyncMock) as marked,
    ):
        await _route(orch, _event("plan :thread:", channel="D1"), seen)
        marked.assert_not_called()
        _assert_silent(orch)
        # The same busy session queues a message without the icon.
        await _route(orch, _event("plan", channel="D1", ts="100.2"), seen)
        marked.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("text", "thread_ts"),
    [
        ("plan the launch", ""),
        (":thread: is the icon I mean", ""),
        ("more on this :thread:", "90.0"),
        ("!title Launch plan :thread:", ""),
    ],
    ids=["no-icon", "icon-in-the-middle", "reply-in-a-thread", "command"],
)
async def test_every_other_message_runs_as_before(text: str, thread_ts: str) -> None:
    orch = _make_orch()

    handled = await _route(orch, _event(text, channel="D1", thread_ts=thread_ts), SeenCache())

    handled.assert_called_once()
    assert handled.call_args[0][3] == text
    assert not thread_opener.opened("D1", "100.1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("channel", "channels", "opener", "mention"),
    [
        ("D1", None, "plan the launch :thread:", False),
        ("C1", None, "<@UBOT> plan the launch :thread:", True),
        ("C1", {"C1": ChannelConfig(activation=ACTIVATION_ALWAYS)}, "plan :thread:", False),
        ("C1", _AGENT_CHANNEL, "plan :thread:", False),
        ("C1", _BOT_CHANNEL, "plan :thread:", False),
        ("C1", _OBSERVE_CHANNEL, "<@UBOT> plan :thread:", True),
    ],
    ids=["dm", "mention", "always", "agent-channel", "bot-channel", "observe"],
)
async def test_the_first_reply_in_the_opened_thread_starts_the_turn(
    channel: str, channels: dict | None, opener: str, mention: bool
) -> None:
    """With no mention: the thread has no session yet, so only the opener admits the reply."""
    orch = _make_orch(channels=channels)
    orch.sessions = MagicMock()
    orch.sessions.is_busy.return_value = False
    orch.sessions.has_session.return_value = False
    orch.sessions.get_session_for_thread.return_value = None
    orch.sessions.enqueue.return_value = False
    orch.sessions.dequeue.return_value = None
    seen = SeenCache()
    await _route(orch, _event(opener, channel=channel), seen, mention=mention)

    reply = _event("start with the date", channel=channel, ts="100.2", thread_ts="100.1")
    handled = await _route(orch, reply, seen)

    handled.assert_called_once()
    assert handled.call_args[0][3] == "start with the date"
    assert handled.call_args[0][4] == "100.1"


@pytest.mark.asyncio
async def test_a_thread_no_opener_reached_crew_still_needs_a_mention() -> None:
    """An unmentioned opener in a mention channel is dropped before the rule, as any message is."""
    orch = _make_orch()
    orch.sessions = MagicMock()
    orch.sessions.has_session.return_value = False
    orch.sessions.get_session_for_thread.return_value = None
    seen = SeenCache()
    await _route(orch, _event("plan the launch :thread:"), seen)

    handled = await _route(orch, _event("start", ts="100.2", thread_ts="100.1"), seen)

    handled.assert_not_called()
    assert not thread_opener.opened("C1", "100.1")


def test_the_opener_is_handed_once_to_its_authors_reply() -> None:
    assert thread_opener.leave_unanswered("C1", "100.1", "U1", "<@UBOT> plan :thread:")

    assert thread_opener.take_topic("C1", "100.1", "U2") == ""
    assert thread_opener.take_topic("C1", "200.1", "U1") == ""
    assert thread_opener.take_topic("C1", "100.1", "U1") == "plan :thread:"
    assert thread_opener.take_topic("C1", "100.1", "U1") == ""
    assert thread_opener.opened("C1", "100.1")


def test_the_oldest_opener_is_forgotten_at_the_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(thread_opener, "_CAP", 2)
    for ts in ("1.0", "2.0", "3.0"):
        thread_opener.leave_unanswered("C1", ts, "U1", "plan :thread:")

    assert [thread_opener.opened("C1", ts) for ts in ("1.0", "2.0", "3.0")] == [False, True, True]


def _linked_state(slot: _ChatSlot) -> MagicMock:
    state = MagicMock()
    state.get_linked_slot = MagicMock(return_value=slot)
    state._slots = {slot.key: slot}
    state._background_tasks = set()
    return state


async def _reply_in_linked_thread(
    slot: _ChatSlot, text: str, user: str = "U1"
) -> list[tuple[str, list[str]]]:
    """Send *text* as a reply in thread ``100.1`` linked to *slot*.

    Returns each turn it started as ``(text, the context seated for that turn)``.
    """
    from kiro_crew.slack import handler

    turns: list[tuple[str, list[str]]] = []

    async def _turn(_state: object, turn_slot: _ChatSlot, turn_text: str, **_kw: object) -> None:
        turns.append((turn_text, [str(e["content"]) for e in turn_slot._pending_context]))

    slack = MagicMock()
    slack.post_message = AsyncMock()
    state = _linked_state(slot)
    with (
        patch.object(handler, "_dashboard_state", state),
        patch.object(handler, "is_allowed_user", return_value=True),
        patch("kiro_crew.dashboard.chat._run_chat", side_effect=_turn),
    ):
        await handler.handle_message(slack, MagicMock(), "C1", text, "100.1", "100.9", user)
        await asyncio.gather(*list(state._background_tasks))
    return turns


@pytest.mark.asyncio
async def test_a_dashboard_chat_gets_the_opener_with_its_authors_first_reply() -> None:
    """A Channel Bot's or Channel Conductor's chat reads no thread parent of its own."""
    slot = _ChatSlot("slot1")
    thread_opener.leave_unanswered("C1", "100.1", "U1", "plan the launch :thread:")

    assert await _reply_in_linked_thread(slot, "who else writes?", user="U2") == [
        ("who else writes?", [])
    ]
    [(text, context)] = await _reply_in_linked_thread(slot, "start with the date")
    assert text == "start with the date"
    assert len(context) == 1 and context[0].endswith("\nplan the launch :thread:")
    assert await _reply_in_linked_thread(slot, "then the venue") == [("then the venue", [])]
    # The rows show what each person typed, and the context is gone with its turn.
    assert [m["content"] for m in slot.messages if m["role"] == "user"] == [
        "who else writes?",
        "start with the date",
        "then the venue",
    ]
    assert slot._pending_context == []


@pytest.mark.asyncio
async def test_a_slash_command_as_the_first_reply_leaves_the_opener_for_the_next() -> None:
    """A ``/`` command ends without the model, so it runs as typed and carries nothing."""
    slot = _ChatSlot("slot1")
    thread_opener.leave_unanswered("C1", "100.1", "U1", "plan the launch :thread:")

    assert await _reply_in_linked_thread(slot, "<@UBOT> /agent ops") == [("/agent ops", [])]
    [(text, context)] = await _reply_in_linked_thread(slot, "start with the date")
    assert text == "start with the date"
    assert len(context) == 1 and context[0].endswith("\nplan the launch :thread:")


@pytest.mark.asyncio
async def test_a_reply_queued_behind_a_running_chat_leaves_the_opener_for_the_next() -> None:
    slot = _ChatSlot("slot1")
    slot.task = asyncio.get_running_loop().create_future()  # a turn is running
    thread_opener.leave_unanswered("C1", "100.1", "U1", "plan the launch :thread:")
    try:
        with patch("kiro_crew.slack.midturn.steer_linked", AsyncMock(return_value=None)):
            assert await _reply_in_linked_thread(slot, "start with the date") == []
    finally:
        slot.task.cancel()

    assert [e["content"] for e in slot._queue] == ["start with the date"]
    assert thread_opener.take_topic("C1", "100.1", "U1") == "plan the launch :thread:"


@pytest.mark.asyncio
async def test_an_opener_the_chat_cannot_seat_is_dropped_not_put_into_the_text() -> None:
    slot = _ChatSlot("slot1")
    thread_opener.leave_unanswered("C1", "100.1", "U1", "plan the launch :thread:")

    with patch.object(_ChatSlot, "append_pending_context", return_value=False):
        assert await _reply_in_linked_thread(slot, "start with the date") == [
            ("start with the date", [])
        ]
    assert thread_opener.take_topic("C1", "100.1", "U1") == ""


@pytest.mark.asyncio
async def test_a_chat_handed_the_thread_parent_at_its_link_gets_no_second_copy() -> None:
    slot = _ChatSlot("slot1")
    slot._pending_thread_parent = ("C1", "100.1", "plan the launch :thread:")
    thread_opener.leave_unanswered("C1", "100.1", "U1", "plan the launch :thread:")

    assert await _reply_in_linked_thread(slot, "start with the date") == [
        ("start with the date", [])
    ]
    assert thread_opener.take_topic("C1", "100.1", "U1") == ""
