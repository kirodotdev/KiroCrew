"""A proactive channel send says which cron or chat sent it.

Every session and cron can reach the same owner DM, so a message there carries a
last line naming its sender. The own-conversation leg labels only a cron: a chat
sending into its own conversation is that conversation talking, like a reply.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.handlers import messaging as mod
from kiro_crew.dashboard.state import NEW_SESSION_TITLE

_CHAT = "dashboard:chat-1-1700000000"


def _state(*, jobs: list[Any] | None = None, title: str | None = "Fix login") -> MagicMock:
    state = MagicMock()
    state.crons.list_jobs.return_value = jobs or []
    slot = None if title is None else SimpleNamespace(key="chat-1-1700000000", display_title=title)
    state.get_slot.side_effect = lambda key: slot if key == "chat-1-1700000000" else None
    return state


async def _send(
    state: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    *,
    caller_session: str = "",
    declared_session: str = "",
    channel_target: str = "",
    channel_type: str = "",
) -> list[str]:
    sent: list[str] = []

    async def _dm(_state, _channel, text, *, caller_session):
        sent.append(text)
        return True, "", ""

    async def _own(_state, _key, text, *, channel_type=""):
        sent.append(text)
        return True

    monkeypatch.setattr(mod, "_deliver_channel_dm", _dm)
    monkeypatch.setattr(mod, "_deliver_to_channel", _own)
    await mod._deliver_send_message_fallback(
        state,
        {},
        mod._SendMessageOutcome(),
        text="Digest ready.",
        title="t",
        blocks=None,
        options=[],
        target_channel="",
        target_user="",
        thread_ts=None,
        reply_broadcast=None,
        target_session="",
        job_name=None,
        channel_target=channel_target,
        channel_type=channel_type,
        caller_session=caller_session,
        declared_session=declared_session,
        attribution_session=declared_session,
        is_cron_caller=caller_session.startswith("cron:"),
        send_to_slack=False,
    )
    assert len(sent) == 1
    return sent


_JOB = SimpleNamespace(id="abc123", name="inbox-review", session_key="telegram:dm:42")


@pytest.mark.asyncio
async def test_a_cron_send_to_the_owner_dm_names_the_cron(monkeypatch):
    sent = await _send(
        _state(jobs=[_JOB]), monkeypatch, caller_session="cron:abc123", channel_target="telegram"
    )
    assert sent == ["Digest ready.\n\nSent by cron: inbox-review"]


@pytest.mark.asyncio
async def test_a_stateless_cron_run_key_still_names_its_job(monkeypatch):
    sent = await _send(
        _state(jobs=[_JOB]),
        monkeypatch,
        caller_session="cron:abc123:run-9",
        channel_target="telegram",
    )
    assert sent[0].endswith("Sent by cron: inbox-review")


@pytest.mark.asyncio
async def test_a_cron_whose_job_is_gone_is_named_by_its_id(monkeypatch):
    sent = await _send(
        _state(), monkeypatch, caller_session="cron:abc123", channel_target="discord"
    )
    assert sent[0].endswith("Sent by cron: abc123")


@pytest.mark.asyncio
async def test_a_chat_send_to_the_owner_dm_names_the_chat(monkeypatch):
    sent = await _send(_state(), monkeypatch, declared_session=_CHAT, channel_target="telegram")
    assert sent == ["Digest ready.\n\nSent by chat: Fix login"]


@pytest.mark.asyncio
async def test_an_untitled_chat_is_named_by_its_slot(monkeypatch):
    sent = await _send(
        _state(title=NEW_SESSION_TITLE),
        monkeypatch,
        declared_session=_CHAT,
        channel_target="telegram",
    )
    assert sent[0].endswith("Sent by chat: chat-1-1700000000")


@pytest.mark.asyncio
async def test_a_long_title_is_cut_to_one_short_line(monkeypatch):
    sent = await _send(
        _state(title="word " * 60 + "\nsecond line"),
        monkeypatch,
        declared_session=_CHAT,
        channel_target="telegram",
    )
    line = sent[0].split("\n\n")[-1]
    assert "\n" not in line
    assert len(line.removeprefix("Sent by chat: ")) <= mod._SOURCE_NAME_MAX
    assert line.endswith("…")


@pytest.mark.asyncio
async def test_a_chat_sending_into_its_own_conversation_is_not_labelled(monkeypatch):
    sent = await _send(_state(), monkeypatch, declared_session=_CHAT, channel_type="telegram")
    assert sent == ["Digest ready."]


@pytest.mark.asyncio
async def test_a_cron_sending_into_its_conversation_names_the_cron(monkeypatch):
    sent = await _send(
        _state(jobs=[_JOB]), monkeypatch, caller_session="cron:abc123", channel_type="telegram"
    )
    assert sent == ["Digest ready.\n\nSent by cron: inbox-review"]


@pytest.mark.asyncio
async def test_a_channel_born_caller_gets_no_line(monkeypatch):
    sent = await _send(
        _state(), monkeypatch, declared_session="telegram:dm:42", channel_target="telegram"
    )
    assert sent == ["Digest ready."]


@pytest.mark.asyncio
async def test_a_send_with_no_caller_gets_no_line(monkeypatch):
    sent = await _send(_state(), monkeypatch, channel_target="telegram")
    assert sent == ["Digest ready."]
