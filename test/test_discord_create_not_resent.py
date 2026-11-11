"""A Discord message create that may already have landed is not sent again.

A message create is applied once per copy Discord receives, and a 5xx, a timeout
or a dropped response can follow a write Discord already made, so re-sending it
posts the reply twice. It is retried only when Discord certainly did not apply
it: after a 429, or when the connection failed before the request went out.

A thread create keeps the ladder's retry: re-sending it cannot open a second
thread for one message.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest
from test_discord_rate_limits import _harness, _Resp

from kiro_crew.discord.client import DISCORD_OK, DISCORD_TRANSIENT

_CREATE = "/channels/9111/messages"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [_Resp(500), asyncio.TimeoutError(), aiohttp.ServerDisconnectedError()],
    ids=["5xx", "timeout", "disconnect"],
)
async def test_a_create_that_may_have_landed_is_sent_once(
    monkeypatch: pytest.MonkeyPatch, failure: object
) -> None:
    harness = _harness(monkeypatch, [failure, _Resp(200, {"id": "2"})])
    result = await harness.client.api_json("POST", _CREATE, {"content": "hi"})
    assert result.outcome == DISCORD_TRANSIENT
    assert len(harness.requests) == 1


@pytest.mark.asyncio
async def test_a_create_whose_connection_never_opened_is_retried(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refused = aiohttp.ClientConnectorError(None, OSError("refused"))  # type: ignore[arg-type]
    harness = _harness(monkeypatch, [refused, _Resp(200, {"id": "2"})])
    result = await harness.client.api_json("POST", _CREATE, {"content": "hi"})
    assert result.outcome == DISCORD_OK and result.message_id == "2"
    assert len(harness.requests) == 2


@pytest.mark.asyncio
async def test_a_rate_limited_create_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = _harness(monkeypatch, [_Resp(429, {"retry_after": 0.5}), _Resp(200, {"id": "2"})])
    result = await harness.client.api_json("POST", _CREATE, {"content": "hi"})
    assert result.outcome == DISCORD_OK and result.message_id == "2"
    assert len(harness.requests) == 2


@pytest.mark.asyncio
async def test_a_thread_create_keeps_its_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only a message create is sent once; a thread create is retried as before."""
    harness = _harness(monkeypatch, [asyncio.TimeoutError(), _Resp(200, {"id": "555"})])
    thread_id = await harness.client.create_thread_from_message("9111", "555", "topic")
    assert thread_id == "555"
    assert len(harness.requests) == 2
