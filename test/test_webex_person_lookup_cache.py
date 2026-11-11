"""A card press's person lookup caches only an answer, never a failed request.

A card action carries only the presser's person id, and authorization is an
email allow-list, so the id is resolved to an email first. A 5xx, a timeout or
a second 429 on that lookup is not an answer: caching its empty email refused
every later press by that person until the gateway restarted.
"""

from __future__ import annotations

import pytest
from test_webex_client import _async_return, _client, _FakeApiResp, _RecordingSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "first",
    [
        _FakeApiResp(500, None),
        _FakeApiResp(503, None),
        _FakeApiResp(200, ValueError("not json")),
        _FakeApiResp(200, {}),
    ],
    ids=["500", "503", "unreadable-2xx", "no-email"],
)
async def test_a_lookup_without_an_email_is_asked_again(
    monkeypatch: pytest.MonkeyPatch, first: _FakeApiResp
) -> None:
    c = _client()
    session = _RecordingSession([first, _FakeApiResp(200, {"emails": ["Owner@Example.com"]})])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    assert await c._email_of("P1") == ""
    assert await c._email_of("P1") == "owner@example.com"
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_an_answer_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    c = _client()
    session = _RecordingSession([_FakeApiResp(200, {"emails": ["owner@example.com"]})])
    monkeypatch.setattr(c, "_ensure_session", _async_return(session))
    assert await c._email_of("P1") == "owner@example.com"
    assert await c._email_of("P1") == "owner@example.com"
    assert len(session.calls) == 1
