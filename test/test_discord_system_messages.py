"""A Discord system notice is not a prompt.

The gateway delivers system notices as ``MESSAGE_CREATE`` too, and some carry text
in ``content``: a thread rename (type 4) its new name, a thread start (type 18) its
title. Their author is the person who acted, so the bot-author loop guard does not
stop them. Only a person's own message (type 0, or a reply, type 19) becomes a turn.
"""

from __future__ import annotations

import asyncio

import pytest
from test_discord_client_coverage import _make_client

from kiro_crew.discord.client import DiscordInbound


async def _dispatched(payload: dict) -> list[DiscordInbound]:
    seen: list[DiscordInbound] = []

    async def _handler(inbound: DiscordInbound) -> None:
        seen.append(inbound)

    client = _make_client(on_message=_handler)
    client._on_dispatch("MESSAGE_CREATE", payload)
    await asyncio.gather(*tuple(client._handler_tasks))
    return seen


def _message(**fields: object) -> dict:
    return {"channel_id": "c1", "id": "m1", "content": "text", "author": {"id": "u1"}, **fields}


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", [4, 18], ids=["thread-rename", "thread-created"])
async def test_a_system_notice_with_text_runs_no_turn(kind: int) -> None:
    assert await _dispatched(_message(type=kind, content="New thread name")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields", [{"type": 0}, {"type": 19}, {}], ids=["default", "reply", "untyped"]
)
async def test_a_persons_message_still_runs(fields: dict) -> None:
    seen = await _dispatched(_message(**fields))
    assert [inbound.text for inbound in seen] == ["text"]
