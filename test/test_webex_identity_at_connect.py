"""A Webex bot identity lookup that failed at connect is retried on a later message.

``bot_name`` strips the leading @mention before a group message's command and
approval-reply parsing, and ``bot_email`` filters the bot's own echoes. When
``/people/me`` fails at connect, the next message retries it before it is read,
so "Kiro /stop" or "Kiro 1" in a space keeps parsing. The connection is still
served, so a lookup that fails for good never keeps the bot offline.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_webex_client import _async_return, _client, _FakeApiResp, _RecordingSession

from kiro_crew.webex import client as webex_client

_ME = {"emails": ["Bot@Example.test"], "id": "BOT", "displayName": "Kiro"}


@pytest.mark.asyncio
async def test_a_failed_lookup_still_serves_the_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    session = _RecordingSession([_FakeApiResp(503, None)])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    monkeypatch.setattr(c, "_get_websocket_url", _async_return(""))
    # The connect carries on past the lookup to device registration, as on main.
    with pytest.raises(Exception, match="webSocketUrl"):
        await c._connect_and_serve()
    assert c.bot_email == ""


@pytest.mark.asyncio
async def test_a_later_message_learns_the_identity_before_it_is_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _client()
    session = _RecordingSession([_FakeApiResp(503, None), _FakeApiResp(200, _ME)])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    assert await c._fetch_me() is False
    seen: list[str] = []

    async def _on_message(inbound: Any) -> None:
        seen.append(c.bot_name)

    c._on_message = _on_message
    message = {"personId": "USER", "personEmail": "me@example.test", "roomId": "R", "text": "hi"}
    monkeypatch.setattr(c, "_fetch_for_dispatch", _async_return(message))
    monkeypatch.setattr(webex_client, "_IDENTITY_RETRY_SECS", 0.0)

    await c._hydrate_and_dispatch("M1")

    assert seen == ["Kiro"]
    assert (c.bot_email, c.bot_person_id) == ("bot@example.test", "BOT")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reply",
    [_FakeApiResp(200, {}), _FakeApiResp(200, ValueError("not json"))],
    ids=["empty", "unreadable"],
)
async def test_a_reply_that_names_nobody_is_a_failed_lookup(
    monkeypatch: pytest.MonkeyPatch, reply: _FakeApiResp
) -> None:
    c = _client()
    session = _RecordingSession([reply])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    assert await c._fetch_me() is False
    assert c.bot_email == ""


@pytest.mark.asyncio
async def test_a_failing_lookup_is_retried_at_most_once_per_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    c = _client()
    session = _RecordingSession([_FakeApiResp(503, None), _FakeApiResp(503, None)])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    assert await c._fetch_me() is False
    await c._ensure_identity()
    await c._ensure_identity()
    assert len(session.calls) == 1
