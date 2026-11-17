"""A compaction result still reaches the chat when its status line cannot be edited.

Both ``!compact`` (Discord) and ``/compact`` (Telegram) post a "Compacting
context…" line and edit it into the result. The line can be deleted during the
wait, or the edit can fail, and then the result has to go out as a new message;
otherwise the chat is left on "Compacting…" with no answer.
"""

from __future__ import annotations

import asyncio

import pytest
from discord_helpers import _dispatcher as _discord_dispatcher
from discord_helpers import _drop_live_config_snapshot  # noqa: F401
from telegram_helpers import _dispatcher as _telegram_dispatcher
from telegram_helpers import _dm

from kiro_crew.messaging.transport import InboundMessage


@pytest.mark.asyncio
async def test_discord_sends_the_result_when_the_edit_fails() -> None:
    d, cli, _sess = _discord_dispatcher({"u1"})
    cli.edit_ok = False
    msg = InboundMessage(
        channel_type="discord", user_id="u1", conversation_id="c1", text="!compact"
    )
    await d.handle_message(msg)
    assert cli.edits, "the status line edit was never tried"
    assert cli.sent[-1][0] == "✅ Context compacted."


def test_telegram_sends_the_result_when_the_edit_fails() -> None:
    d, cli, _sess = _telegram_dispatcher({7})
    cli.edit_ok = False
    asyncio.run(d.handle_message(_dm("/compact")))
    assert cli.edits, "the status line edit was never tried"
    assert cli.sent[-1][0] == "✅ Context compacted."


@pytest.mark.asyncio
async def test_discord_sends_nothing_extra_when_the_edit_lands() -> None:
    d, cli, _sess = _discord_dispatcher({"u1"})
    msg = InboundMessage(
        channel_type="discord", user_id="u1", conversation_id="c1", text="!compact"
    )
    await d.handle_message(msg)
    assert cli.edits[-1][1] == "✅ Context compacted."
    assert "✅ Context compacted." not in [text for text, _ in cli.sent]


def test_telegram_sends_nothing_extra_when_the_edit_lands() -> None:
    d, cli, _sess = _telegram_dispatcher({7})
    asyncio.run(d.handle_message(_dm("/compact")))
    assert cli.edits[-1][1] == "✅ Context compacted."
    assert "✅ Context compacted." not in [text for text, _ in cli.sent]
