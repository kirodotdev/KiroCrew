"""``TelegramClient.close()`` leaves no HTTP session behind a turn that is still running.

The album flush on close is pinned in ``test_telegram_album.py``; the session-close
contract shared with the other channels is in ``test_transport_close_session_leak.py``.
"""

from __future__ import annotations

import asyncio

import aiohttp
import pytest

from kiro_crew.telegram import client as tg_client
from kiro_crew.telegram import client_http
from kiro_crew.telegram.client import TelegramClient


class _FakeSession:
    instances: list[_FakeSession] = []

    def __init__(self, *_a, **_k) -> None:
        self.closed = False
        _FakeSession.instances.append(self)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_sessions(monkeypatch):
    _FakeSession.instances.clear()
    monkeypatch.setattr(client_http.aiohttp, "ClientSession", _FakeSession)
    return _FakeSession.instances


@pytest.mark.asyncio
async def test_a_turn_still_running_after_close_opens_no_new_session(
    monkeypatch, fake_sessions
) -> None:
    monkeypatch.setattr(tg_client, "_CLOSE_DRAIN_GRACE_S", 0)  # no grace: close at once
    client = TelegramClient(token="1:t")
    await client._ensure_session()
    release = asyncio.Event()

    async def _turn() -> None:
        await release.wait()
        await client._ensure_session()  # the turn's next send or edit

    turn = asyncio.create_task(_turn())
    client._track(turn)
    await asyncio.sleep(0)

    await client.close()
    release.set()
    results = await asyncio.gather(turn, return_exceptions=True)

    assert [s for s in fake_sessions if not s.closed] == []
    # The turn is not cancelled: its send fails like a transport error, so its
    # own teardown still runs.
    assert not turn.cancelled()
    assert isinstance(results[0], aiohttp.ClientConnectionError)


@pytest.mark.asyncio
async def test_a_send_after_close_returns_no_result(fake_sessions) -> None:
    client = TelegramClient(token="1:t")
    await client._ensure_session()
    await client.close()

    assert await client._api("sendMessage", {"chat_id": 1, "text": "late"}) is None
    assert [s for s in fake_sessions if not s.closed] == []


@pytest.mark.asyncio
async def test_close_lets_a_quick_turn_finish_before_closing(fake_sessions) -> None:
    client = TelegramClient(token="1:t")
    session = await client._ensure_session()
    sent_on: list[object] = []

    async def _turn() -> None:
        await asyncio.sleep(0)
        sent_on.append(await client._ensure_session())

    client._track(asyncio.create_task(_turn()))

    await client.close()

    assert sent_on == [session], "the turn should finish on the open session"
    assert session.closed


@pytest.mark.asyncio
async def test_start_after_close_can_open_a_session_again(fake_sessions) -> None:
    client = TelegramClient(token="1:t")
    await client._ensure_session()
    await client.close()
    client._closed = False  # what start() does first

    session = await client._ensure_session()

    assert not session.closed
