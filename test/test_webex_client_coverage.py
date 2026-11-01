"""Webex client: host and file-safety checks, the one-card send cap, content HEAD
lookups, and a close that survives a failing callback."""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest

from kiro_crew.webex import client as webex_client
from kiro_crew.webex.client import WebexClient


def _client(**kw: Any) -> WebexClient:
    return WebexClient(token="tok", **kw)


class _Head:
    """Stands in for ``session.head``: an async context manager around one reply."""

    def __init__(
        self,
        status: int = 200,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ):
        self.status = status
        self.headers = headers or {}
        self.error = error
        self.urls: list[str] = []

    def head(self, url: str, **kw: Any):
        self.urls.append(url)
        if self.error is not None:
            raise self.error
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _with_session(client: WebexClient, head: _Head) -> None:
    async def session() -> _Head:
        return head

    client._ensure_session = session  # type: ignore[method-assign]


def test_a_malformed_url_is_not_a_webex_host():
    assert webex_client._is_webex_host("http://[::1") is False


def test_a_file_activity_without_items_is_never_safe():
    assert webex_client._files_are_safe({}) is False
    assert webex_client._files_are_safe({"object": {"files": {"items": []}}}) is False


@pytest.mark.asyncio
async def test_a_message_carries_at_most_one_card():
    client = _client()
    sent: list[dict[str, Any]] = []

    async def fake_api(method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        sent.append(payload)
        return {"id": "m1"}

    client._api = fake_api  # type: ignore[method-assign]
    cards = [{"type": "AdaptiveCard", "n": 1}, {"type": "AdaptiveCard", "n": 2}]
    message_id = await client.send_message("room-1", "hi", attachments=cards)
    assert message_id == "m1"
    assert sent[0]["attachments"] == [cards[0]]


@pytest.mark.asyncio
async def test_head_reads_name_type_and_size_from_the_headers():
    client = _client()
    head = _Head(
        headers={
            "Content-Disposition": 'attachment; filename="report.pdf"',
            "Content-Type": "application/pdf; charset=binary",
            "Content-Length": "123",
        }
    )
    _with_session(client, head)
    url = f"{client._api_base}/contents/abc"
    assert await client.head_content(url) == ("report.pdf", "application/pdf", 123)
    assert head.urls == [url]


@pytest.mark.asyncio
async def test_head_never_sends_the_token_to_a_foreign_url():
    client = _client()
    head = _Head()
    _with_session(client, head)
    assert await client.head_content("https://evil.example/contents/abc") == ("", "", 0)
    assert head.urls == []


@pytest.mark.asyncio
async def test_head_non_success_status_reads_as_unknown():
    client = _client()
    _with_session(client, _Head(status=404))
    assert await client.head_content(f"{client._api_base}/contents/abc") == ("", "", 0)


@pytest.mark.asyncio
async def test_a_non_numeric_length_reads_as_zero():
    client = _client()
    _with_session(client, _Head(headers={"Content-Length": "lots", "Content-Type": "text/plain"}))
    name, mimetype, size = await client.head_content(f"{client._api_base}/contents/abc")
    assert (name, mimetype, size) == ("", "text/plain", 0)


@pytest.mark.asyncio
async def test_a_transport_error_on_head_reads_as_unknown():
    client = _client()
    _with_session(client, _Head(error=aiohttp.ClientError("down")))
    assert await client.head_content(f"{client._api_base}/contents/abc") == ("", "", 0)


@pytest.mark.asyncio
async def test_close_survives_a_failing_on_close_callback():
    def broken() -> None:
        raise RuntimeError("callback bug")

    client = _client(on_message=None)
    client.on_close = broken  # type: ignore[assignment]
    await client.close()
    assert client._closed is True
