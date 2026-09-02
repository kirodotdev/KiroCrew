"""Unit tests for the shared ``read_bounded_json`` body guard.

It owns the parse-and-shape contract for the endpoints routed through it
(issue #5587), and the 64 KB pre-decode byte cap the two notification
endpoints need (issue #490). It is not yet the dashboard's only such guard --
four siblings survive and are tracked on #5587; the helper's docstring names
them.

The cap half: ``messaging.api_notification_agent_push`` and
``notifications_push.api_push_notification`` previously each inlined a
byte-identical Content-Length precheck + incremental read + 413/400 block, with
the cap as a function-local. Extracting the helper means the cap and the
413/400 contract live in exactly one place and cannot drift.

The shape half: ``await request.json()`` returns a list, string, or number for a
body that is valid JSON but not an object, and a handler that then calls
``.get()`` on it turns a client mistake into a 500. ``knowledge`` used to carry
a second helper for this with a different cap, message, absent-body rule, and
exception breadth; these tests pin the one surviving contract.
"""

import json

import pytest

from kiro_crew.dashboard.handlers import _shared as shared
from kiro_crew.dashboard.handlers._shared import _MAX_BODY_BYTES, read_bounded_json


class _FakeContent:
    """Minimal stand-in for ``aiohttp.StreamReader`` exposing ``iter_chunked``."""

    def __init__(self, data: bytes):
        self._data = data

    async def iter_chunked(self, n: int):
        for i in range(0, len(self._data), n):
            yield self._data[i : i + n]


class _FakeRequest:
    def __init__(
        self,
        data: bytes,
        content_length: int | None = None,
        *,
        charset: str | None = None,
        can_read_body: bool = True,
        read_error: BaseException | None = None,
    ):
        self.content = _FakeContent(data)
        self.content_length = content_length
        self.charset = charset
        self.can_read_body = can_read_body
        self._data = data
        self._read_error = read_error

    async def json(self):
        """Stand in for ``request.json()`` -- the uncapped (max_bytes=None) path.

        Mirrors aiohttp: ``read()`` then ``decode(charset or utf-8)`` then
        ``loads``, so an unknown codec raises LookupError here exactly as it
        would in production.
        """
        if self._read_error is not None:
            raise self._read_error
        return json.loads(self._data.decode(self.charset or "utf-8"))


def _code(resp) -> str:
    """The machine-readable ``code`` from an error response body."""
    return json.loads(resp.text or "")["code"]


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
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    async def test_non_object_body_returns_400(self):
        raw = b"[1, 2, 3]"
        body, err = await read_bounded_json(_FakeRequest(raw, content_length=len(raw)))
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "body_not_object"

    @pytest.mark.asyncio
    async def test_oversize_carries_payload_too_large_code(self):
        body, err = await read_bounded_json(
            _FakeRequest(b"{}", content_length=_MAX_BODY_BYTES + 1)
        )
        assert body is None
        assert err is not None and _code(err) == "payload_too_large"


class TestUncappedReads:
    """``max_bytes=None`` -- the endpoints with no principled byte ceiling."""

    @pytest.mark.asyncio
    async def test_body_far_over_the_default_cap_is_accepted(self):
        # A knowledge bundle import has no defensible maximum size, so opting
        # out of the cap must actually lift it -- not merely raise it.
        raw = b'{"pad": "' + b"a" * (_MAX_BODY_BYTES * 2) + b'"}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw)), max_bytes=None
        )
        assert err is None
        assert body is not None and len(body["pad"]) == _MAX_BODY_BYTES * 2

    @pytest.mark.asyncio
    async def test_shape_guard_still_applies_without_a_cap(self):
        # Dropping the cap must not drop the reason the helper exists.
        raw = b'"just a string"'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw)), max_bytes=None
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "body_not_object"


class TestAllowAbsent:
    @pytest.mark.asyncio
    async def test_absent_body_becomes_empty_object(self):
        body, err = await read_bounded_json(
            _FakeRequest(b"", can_read_body=False), allow_absent=True
        )
        assert err is None
        assert body == {}

    @pytest.mark.asyncio
    async def test_absent_body_is_400_without_the_opt_in(self):
        # Defaulting an absent body is per-endpoint, not the global rule.
        body, err = await read_bounded_json(_FakeRequest(b"", can_read_body=False))
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    async def test_present_but_malformed_body_is_still_400(self):
        # "sent nothing" and "sent garbage" are different facts: only the first
        # one can be defaulted. Answering 200-with-defaults to a client typo
        # runs a different operation than the caller asked for, silently.
        body, err = await read_bounded_json(
            _FakeRequest(b"{not json", content_length=9), allow_absent=True
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"


class TestDecodeContract:
    """Both paths decode like ``request.json()``: decode(charset) then loads.

    Every case runs against the capped path AND the uncapped one: the two must
    differ only in whether the read is bounded, or the helper has two contracts
    wearing one name.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_bytes", [_MAX_BODY_BYTES, None])
    async def test_declared_charset_is_honoured(self, max_bytes):
        raw = '{"name": "caf\u00e9"}'.encode("latin-1")
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), charset="latin-1"),
            max_bytes=max_bytes,
        )
        assert err is None
        assert body == {"name": "caf\u00e9"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_bytes", [_MAX_BODY_BYTES, None])
    async def test_unknown_charset_is_400_not_500(self, max_bytes):
        # charset= names a codec Python does not have: bytes.decode raises
        # LookupError, which is not a ValueError and would otherwise escape the
        # guard as a 500 for what is really a malformed client header.
        raw = b'{"name": "x"}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), charset="not-a-codec"),
            max_bytes=max_bytes,
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("max_bytes", [_MAX_BODY_BYTES, None])
    async def test_undecodable_bytes_are_400_not_500(self, max_bytes):
        raw = b'{"name": "\xff\xfe"}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw)), max_bytes=max_bytes
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    async def test_recursion_error_is_400_not_500(self, monkeypatch):
        # A deeply nested document blows the JSON parser's stack: json.loads
        # raises RecursionError, which is not a ValueError and would otherwise
        # escape the guard as a 500. The raise depth is version- and
        # platform-dependent (~1k on 3.10, ~10k on 3.12, lower on small-stack
        # Windows), so inject at the parse boundary rather than gambling with
        # the test worker's C stack on a real payload.
        class _ParserStackOverflow:
            @staticmethod
            def loads(_text):
                raise RecursionError()

        # Swap only the helper module's ``json`` reference -- patching
        # ``json.loads`` itself would also break this test's own decoding.
        monkeypatch.setattr(shared, "json", _ParserStackOverflow)
        raw = b'{"a": 1}'
        body, err = await read_bounded_json(_FakeRequest(raw, content_length=len(raw)))
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    async def test_recursion_error_from_request_json_is_400_not_500(self):
        # Same contract on the uncapped path, where the parse happens inside
        # request.json() and the helper never sees json.loads at all.
        request = _FakeRequest(b'{"a": 1}', read_error=RecursionError())
        body, err = await read_bounded_json(request, max_bytes=None)
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

    @pytest.mark.asyncio
    async def test_transport_error_propagates_instead_of_becoming_400(self):
        # A disconnect mid-body is not a client JSON mistake; reporting it as
        # 400 invalid_json tells the client its payload was wrong when it was
        # not, and hides the disconnect from the 500 class that owns it.
        request = _FakeRequest(b"{}", read_error=ConnectionResetError())
        with pytest.raises(ConnectionResetError):
            await read_bounded_json(request, max_bytes=None)
