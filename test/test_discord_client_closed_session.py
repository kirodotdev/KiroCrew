"""After ``DiscordClient.close()``, a late send opens no new HTTP session.

The rest of the client lifecycle is pinned in ``test_discord_client_coverage.py``.
"""

from __future__ import annotations

import aiohttp
import pytest

from kiro_crew.discord.client import DiscordClient


@pytest.mark.asyncio
async def test_a_send_after_close_opens_no_new_session(monkeypatch) -> None:
    def _no_network(self, *_a, **_k):
        raise aiohttp.ClientConnectionError("offline")

    monkeypatch.setattr(aiohttp.ClientSession, "request", _no_network)
    client = DiscordClient(token="t")
    await client._ensure_session()
    await client.close()

    await client.send_typing("123")  # a typing loop or queued drain that wakes late

    leaked = client._session
    try:
        assert leaked is None, "close() left a freshly created ClientSession behind"
    finally:
        if leaked is not None:
            await leaked.close()


@pytest.mark.asyncio
async def test_start_after_close_can_open_a_session_again() -> None:
    client = DiscordClient(token="t")
    await client._ensure_session()
    await client.close()
    client._closed = False  # what start() does first

    session = await client._ensure_session()
    try:
        assert session is not None and not session.closed
    finally:
        await session.close()
