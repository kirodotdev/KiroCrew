"""Tests for the shared ``read_capped_response`` helper (issue #4829).

Three dashboard HTTP readers (the release-feed fetch, the Aperture feedback
reply, and the Jira issue fetch) previously read the body with a single
``StreamReader.read(cap + 1)``. ``read(n)`` returns UP TO *n* bytes, resolving
as soon as any data is buffered, so on a chunked response with no
Content-Length it hands back only the first buffered chunk and the caller
silently works on a truncated body. These tests pin the shared helper's
stream-to-EOF contract and exercise the three callers with multi-chunk bodies.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew.dashboard.handlers import feedback as feedback_mod
from kiro_crew.dashboard.handlers import source_providers as source_mod
from kiro_crew.dashboard.handlers import updates as updates_mod
from kiro_crew.dashboard.handlers._shared import read_capped_response

# Bind the real coroutine at import time: an autouse conftest guard replaces
# the ``updates._fetch_feed_bytes`` module attribute with a refuser for every
# test, and this suite needs to exercise the real read path (against a fake
# session -- nothing here touches the network).
_REAL_FETCH_FEED_BYTES = updates_mod._fetch_feed_bytes


class _FakeContent:
    """Stand-in for ``aiohttp.StreamReader`` that hands out queued chunks.

    ``read(n)`` returns at most ONE queued chunk (split when longer than *n*)
    and ``b""`` at EOF -- the documented "up to n bytes" contract a single read
    cannot satisfy for a streamed body -- so a single-read implementation is
    still exercised and fails these tests with a truncated body rather than an
    AttributeError.
    """

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def iter_chunked(self, n: int):
        while self._chunks:
            yield await self.read(n)

    async def read(self, n: int = -1) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks.pop(0)
        if n >= 0 and len(chunk) > n:
            self._chunks.insert(0, chunk[n:])
            chunk = chunk[:n]
        return chunk

    @property
    def undelivered(self) -> int:
        """Bytes a caller that stopped early never consumed."""
        return sum(len(c) for c in self._chunks)


class _FakeResponse:
    def __init__(self, chunks, status: int = 200):
        self.status = status
        self.content = _FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, **kwargs):
        return self._response


class TestReadCappedResponse:
    @pytest.mark.asyncio
    async def test_multi_chunk_body_read_whole(self):
        """A body split across chunks is assembled to EOF, not truncated."""
        resp = _FakeResponse([b"a" * 10, b"b" * 10, b"c" * 5])
        assert await read_capped_response(resp, 100) == b"a" * 10 + b"b" * 10 + b"c" * 5

    @pytest.mark.asyncio
    async def test_exact_cap_body_delivered_complete(self):
        """A body of exactly *cap* bytes arrives whole and does not trip the sentinel."""
        resp = _FakeResponse([b"x" * 7, b"y" * 9])
        body = await read_capped_response(resp, 16)
        assert body == b"x" * 7 + b"y" * 9
        assert len(body) <= 16  # callers' ``len(body) > cap`` must stay False

    @pytest.mark.asyncio
    async def test_over_cap_trips_sentinel_and_stops_reading(self):
        """An oversized body returns cap + 1 bytes and is refused mid-stream."""
        resp = _FakeResponse([b"x" * 10, b"y" * 10, b"z" * 1000])
        body = await read_capped_response(resp, 12)
        assert len(body) == 13  # cap + 1: the established over-cap sentinel
        assert resp.content.undelivered == 1000  # not buffered whole

    @pytest.mark.asyncio
    async def test_empty_body(self):
        resp = _FakeResponse([])
        assert await read_capped_response(resp, 10) == b""


class TestFeedbackReadCappedText:
    @pytest.mark.asyncio
    async def test_multi_chunk_response_decoded_whole(self):
        text = "aperture says thanks " * 500
        raw = text.encode("utf-8")
        resp = _FakeResponse([raw[:100], raw[100:5000], raw[5000:]])
        assert await feedback_mod._read_capped_text(resp) == text

    @pytest.mark.asyncio
    async def test_over_cap_body_still_raises(self, monkeypatch):
        monkeypatch.setattr(feedback_mod, "_MAX_RESP_BYTES", 8)
        resp = _FakeResponse([b"0123", b"456789"])
        with pytest.raises(ValueError, match="exceeded cap"):
            await feedback_mod._read_capped_text(resp)


class TestFetchFeedBytes:
    @pytest.mark.asyncio
    async def test_chunked_feed_read_whole(self, monkeypatch):
        """The release feed parses complete even when it arrives in chunks."""
        payload = json.dumps({"version": "9.9.9", "notes": "x" * 30000}).encode("utf-8")
        resp = _FakeResponse([payload[:8192], payload[8192:16384], payload[16384:]])
        monkeypatch.setattr(
            updates_mod.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(resp)
        )
        status, raw = await _REAL_FETCH_FEED_BYTES("https://feed.example/latest-cli.json")
        assert status == 200
        assert raw == payload

    @pytest.mark.asyncio
    async def test_oversized_feed_still_detected(self, monkeypatch):
        """The caller's ``len(raw) > _FEED_MAX_BYTES`` over-cap check still fires."""
        big = b"x" * (updates_mod._FEED_MAX_BYTES + 100)
        resp = _FakeResponse([big[:8192], big[8192:]])
        monkeypatch.setattr(
            updates_mod.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(resp)
        )
        _status, raw = await _REAL_FETCH_FEED_BYTES("https://feed.example/latest-cli.json")
        assert len(raw) > updates_mod._FEED_MAX_BYTES  # sentinel trips
        assert len(raw) == updates_mod._FEED_MAX_BYTES + 1  # but never buffers whole


class TestJiraFetchStreams:
    @pytest.mark.asyncio
    async def test_multi_chunk_jira_response_parses_whole(self, monkeypatch):
        """A Jira document split across chunks reaches json.loads complete."""
        description = "long jira description " * 2000  # well past one 8 KiB chunk
        doc = json.dumps(
            {
                "fields": {
                    "summary": "Streamed issue",
                    "description": description,
                    "status": {"statusCategory": {"key": "new"}},
                }
            }
        ).encode("utf-8")
        resp = _FakeResponse([doc[:8192], doc[8192:16384], doc[16384:]])
        monkeypatch.setattr(
            source_mod.aiohttp, "ClientSession", lambda *a, **kw: _FakeSession(resp)
        )
        monkeypatch.setattr(source_mod, "_get_jira_auth", lambda host: ("e@example.com", "tok"))
        ref = source_mod.parse_source_url("https://acme.atlassian.net/browse/PROJ-123")

        issue = await source_mod._fetch_jira_issue(ref)

        assert issue["title"] == "Streamed issue"
        assert description.strip() in issue["description"]
