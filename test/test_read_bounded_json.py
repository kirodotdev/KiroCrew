"""Unit tests for the shared ``read_bounded_json`` body-cap helper (issue #490).

The two 64 KB body-capped notification endpoints
(``messaging.api_notification_agent_push`` and
``notifications_push.api_push_notification``) previously each inlined a
byte-identical Content-Length precheck + incremental read + 413/400 block, with
the cap as a function-local. Extracting the helper means the cap and the
413/400 contract live in exactly one place and cannot drift. These tests pin
that contract directly against the helper.
"""

import pytest

from kiro_crew.dashboard.handlers._shared import _MAX_BODY_BYTES, read_bounded_json


class _FakeContent:
    """Minimal stand-in for ``aiohttp.StreamReader`` exposing ``iter_chunked``."""

    def __init__(self, data: bytes):
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeRequest:
    def __init__(self, data: bytes, content_length: int | None = None):
        self.content = _FakeContent(data)
        self.content_length = content_length


class TestReadBoundedJson:
    @pytest.mark.asyncio
    async def test_valid_object_returns_body_and_no_error(self):
        raw = b'{"channel": "x", "title": "t"}'
        body, err = await read_bounded_json(_FakeRequest(raw, content_length=len(raw)))
        assert err is None
        assert body == {"channel": "x", "title": "t"}

    @pytest.mark.asyncio
    async def test_content_length_precheck_rejects_before_reading(self):
        # Declared size over the cap -> 413 without draining the stream.
        body, err = await read_bounded_json(
            _FakeRequest(b"{}", content_length=_MAX_BODY_BYTES + 1)
        )
        assert body is None
        assert err is not None and err.status == 413

    @pytest.mark.asyncio
    async def test_streamed_oversize_rejects_when_no_content_length(self):
        # Chunked bodies carry no Content-Length; the incremental read must
        # still enforce the cap. Use a small explicit cap to keep the test fast.
        body, err = await read_bounded_json(
            _FakeRequest(b"x" * 100, content_length=None), max_bytes=16
        )
        assert body is None
        assert err is not None and err.status == 413

    @pytest.mark.asyncio
    async def test_exact_cap_passes_size_gate(self):
        # A body of exactly max_bytes clears the 413 gate (it may still fail
        # later validation, but that is the caller's concern, not the cap's).
        raw = b'"' + b"a" * 12 + b'"'  # 15 bytes, valid JSON string (not a dict)
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw)), max_bytes=len(raw)
        )
        # Cleared 413; rejected as non-object with 400.
        assert body is None
        assert err is not None and err.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        body, err = await read_bounded_json(_FakeRequest(b"{not json", content_length=9))
        assert body is None
        assert err is not None and err.status == 400

    @pytest.mark.asyncio
    async def test_non_object_body_returns_400(self):
        raw = b"[1, 2, 3]"
        body, err = await read_bounded_json(_FakeRequest(raw, content_length=len(raw)))
        assert body is None
        assert err is not None and err.status == 400
