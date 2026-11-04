"""The loopback Telegram wire records what the real client actually sent."""

from __future__ import annotations

import pytest
from telegram_wire_helpers import telegram_wire


@pytest.mark.asyncio
async def test_a_topic_message_reaches_the_wire_with_its_chat_thread_and_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markup = {"inline_keyboard": [[{"text": "OK", "callback_data": "ok"}]]}
    async with telegram_wire(monkeypatch) as wire:
        message_id = await wire.client.send_message(
            -1001, "hello", reply_markup=markup, message_thread_id=7
        )
    [sent] = wire.messages()
    assert (sent.get("chat_id"), sent.get("text"), sent.get("message_thread_id")) == (
        -1001,
        "hello",
        7,
    )
    assert sent.get("reply_markup") == markup
    assert message_id == 101


@pytest.mark.asyncio
async def test_a_dm_message_carries_no_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    async with telegram_wire(monkeypatch) as wire:
        await wire.client.send_message(42, "plain")
    [sent] = wire.messages()
    assert sent.get("text") == "plain"
    assert "message_thread_id" not in sent.params


@pytest.mark.asyncio
async def test_a_rejected_parse_mode_is_resent_as_plain_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with telegram_wire(monkeypatch) as wire:
        wire.fail("sendMessage", 400, "Bad Request: can't parse entities")
        message_id = await wire.client.send_message(42, "<b>x", parse_mode="HTML")
    assert [call.get("parse_mode") for call in wire.messages()] == ["HTML", None]
    assert message_id is None


@pytest.mark.asyncio
async def test_an_edit_names_its_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async with telegram_wire(monkeypatch) as wire:
        ok = await wire.client.edit_message(42, 555, "edited")
    [edit] = wire.sent("editMessageText")
    assert (edit.get("chat_id"), edit.get("message_id"), edit.get("text")) == (42, 555, "edited")
    assert ok is True
