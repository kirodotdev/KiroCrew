"""A Discord gateway whose resume host stays unreachable falls back to a fresh identify.

The rest of the reconnect loop is pinned in ``test_discord_client_coverage.py``.
"""

from __future__ import annotations

import aiohttp
import pytest
from local_asyncio_helpers import asyncio_with_sleep

from kiro_crew.discord import client as dc
from kiro_crew.discord.client import DiscordClient

_RESUME_URL = "wss://gateway-us-east1-b.discord.gg/?v=10&encoding=json"
# Read with a default so the test also runs, and fails by assertion, on code
# that predates the constant.
_LIMIT = getattr(dc, "_RESUME_FAILURES_BEFORE_IDENTIFY", 7)


def _client_with_unreachable_resume(monkeypatch, *, stop_after: int, on_connect=None):
    client = DiscordClient(token="t")
    client._session_id = "sid"
    client._seq = 41
    client._resume_url = _RESUME_URL
    urls: list[str] = []

    class _Session:
        closed = False

        def ws_connect(self, url, **_kw):
            urls.append(url)
            if on_connect is not None:
                on_connect(client, len(urls))
            raise aiohttp.ClientConnectionError("proxy refused host")

    async def _ensure():
        return _Session()

    async def _sleep(_delay):
        if len(urls) >= stop_after:
            client._closed = True

    monkeypatch.setattr(client, "_ensure_session", _ensure)
    # Only the client family sleeps on the fake clock; patched through the facade.
    monkeypatch.setattr(dc, "asyncio", asyncio_with_sleep(_sleep))
    return client, urls


@pytest.mark.asyncio
async def test_an_unreachable_resume_url_falls_back_to_identify(monkeypatch) -> None:
    client, urls = _client_with_unreachable_resume(monkeypatch, stop_after=_LIMIT + 2)

    await client._gateway_loop()

    assert urls[:_LIMIT] == [_RESUME_URL] * _LIMIT
    assert urls[_LIMIT] == dc._GATEWAY_URL
    assert client._session_id == ""
    assert client._seq is None


@pytest.mark.asyncio
async def test_a_short_resume_outage_keeps_the_session(monkeypatch) -> None:
    """Fewer failures than the limit still resume, so missed events replay."""
    client, urls = _client_with_unreachable_resume(monkeypatch, stop_after=_LIMIT - 1)

    await client._gateway_loop()

    assert urls == [_RESUME_URL] * (_LIMIT - 1)
    assert client._session_id == "sid"


@pytest.mark.asyncio
async def test_a_resumed_session_resets_the_count(monkeypatch) -> None:
    """Failures split by a successful RESUMED never add up to a fallback."""

    def _resume_halfway(client, attempt):
        if attempt == _LIMIT - 1:
            client._on_dispatch("RESUMED", {})

    client, urls = _client_with_unreachable_resume(
        monkeypatch, stop_after=_LIMIT + 2, on_connect=_resume_halfway
    )

    await client._gateway_loop()

    assert dc._GATEWAY_URL not in urls
    assert client._session_id == "sid"
