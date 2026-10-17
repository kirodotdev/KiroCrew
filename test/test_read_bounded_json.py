"""Unit tests for the shared ``read_bounded_json`` body guard.

It owns the parse-and-shape contract for the endpoints routed through it
and the 64 KB pre-decode byte cap the two notification
endpoints need. It is not yet the dashboard's only such guard --
four siblings survive and are tracked separately; the helper's docstring names
them.

The cap half: ``messaging.api_notification_agent_push`` and
``notifications_push.api_push_notification`` each inlined a
byte-identical Content-Length precheck + incremental read + 413/400 block, with
the cap as a function-local. Extracting the helper means the cap and the
413/400 contract live in exactly one place and cannot drift.

The shape half: ``await request.json()`` returns a list, string, or number for a
body that is valid JSON but not an object, and a handler that then calls
``.get()`` on it turns a client mistake into a 500. ``knowledge`` once carried
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
        content_type: str = "application/json",
    ):
        self.content = _FakeContent(data)
        self.content_length = content_length
        self.charset = charset
        self.can_read_body = can_read_body
        self._data = data
        self._read_error = read_error
        # Mirrors ``aiohttp.web.Request.content_type``: the media type alone, with
        # parameters split off onto ``charset``, and ``application/octet-stream``
        # when the client sent no Content-Type at all. The default here is the
        # header every in-tree client sends, so the tests below that are about
        # the cap and the shape read as they did before the media-type gate.
        self.content_type = content_type

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


class TestJsonContentTypeRequired:
    """A body must DECLARE JSON, or it is refused 415 before it is read.

    The primitive this removes: ``text/plain`` and a missing Content-Type are
    both CORS *simple* request types, so a cross-origin page sends either with
    no preflight. The helper parsed the body as JSON regardless, which made an
    ordinary web page able to deliver a JSON command to a local endpoint that
    the browser never asked permission to reach. ``application/json`` is not
    simple, so requiring it puts the preflight back in front of the body.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_type",
        [
            "text/plain",
            # What aiohttp reports when the client sent NO Content-Type. A fetch()
            # with an untyped Blob body is exactly this, and is equally unpreflighted.
            "application/octet-stream",
            # The other two HTML form enctypes, so a bare <form> cannot reach here.
            "application/x-www-form-urlencoded",
            "multipart/form-data",
            # Close-but-not-JSON spellings that must not pass on looks alone.
            "text/json",
            "application/jsonx",
            "",
        ],
    )
    async def test_a_body_with_a_non_json_media_type_is_415(self, content_type: str):
        raw = b'{"channel": "x"}'  # Well-formed JSON: the MEDIA TYPE is what fails.
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type=content_type)
        )
        assert body is None
        assert err is not None and err.status == 415
        assert _code(err) == "unsupported_media_type"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "content_type",
        [
            "application/json",
            # Case is not significant in a media type, and a client may spell it
            # however it likes.
            "Application/JSON",
            "  application/json  ",
            # The ``+json`` structured-suffix family. NOT hypothetical: the app
            # SDK lets a caller set its own Content-Type and a scoped-API test
            # pins ``application/merge-patch+json`` arriving at the gateway, so
            # refusing the suffix would break a shape the product ships.
            "application/merge-patch+json",
            "application/ld+json",
        ],
    )
    async def test_a_json_media_type_is_accepted(self, content_type: str):
        raw = b'{"channel": "x"}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type=content_type)
        )
        assert err is None
        assert body == {"channel": "x"}

    @pytest.mark.asyncio
    async def test_the_uncapped_path_is_gated_too(self):
        # ``max_bytes=None`` reads through request.json(); the gate runs before
        # either path, so opting out of the cap does not opt out of the media type.
        request = _FakeRequest(b'{"a": 1}', content_type="text/plain")
        body, err = await read_bounded_json(request, max_bytes=None)
        assert body is None
        assert err is not None and err.status == 415

    @pytest.mark.asyncio
    async def test_the_gate_runs_before_the_body_is_read(self):
        # Refusing only after draining the stream would leave the bytes read on
        # the loop, so this pins the ORDER: a read that would explode is never
        # reached. ``read_error`` fires inside request.json().
        request = _FakeRequest(
            b'{"a": 1}', content_type="text/plain", read_error=ConnectionResetError()
        )
        body, err = await read_bounded_json(request, max_bytes=None)
        assert body is None
        assert err is not None and err.status == 415

    @pytest.mark.asyncio
    async def test_a_declared_oversize_body_is_still_413_not_415(self):
        # Order pin. Both refusals are free (a header read each), so which one
        # the client is told is the whole question, and "you sent too much" is
        # the actionable one -- it is also the answer this helper gave before the
        # media-type gate existed, which a real end-to-end caller depends on.
        body, err = await read_bounded_json(
            _FakeRequest(
                b"", content_length=_MAX_BODY_BYTES + 1, content_type="application/octet-stream"
            )
        )
        assert body is None
        assert err is not None and err.status == 413
        assert _code(err) == "payload_too_large"

    @pytest.mark.asyncio
    async def test_an_absent_body_keeps_its_existing_status(self):
        # Deliberately NOT gated: an empty body carries no JSON document to be
        # misread, so gating it would restatus a bodiless POST while removing no
        # primitive. ``allow_absent`` still defaults it, and without that flag it
        # is still the 400 it always was -- not a 415.
        defaulted, err = await read_bounded_json(
            _FakeRequest(b"", can_read_body=False, content_type="application/octet-stream"),
            allow_absent=True,
        )
        assert err is None
        assert defaulted == {}

        body, err = await read_bounded_json(
            _FakeRequest(b"", can_read_body=False, content_type="application/octet-stream")
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"


class TestContentTypeGateOptOut:
    """``require_json_content_type=False``: skips the 415, keeps everything else.

    The gate returns BEFORE a single byte is read, which is what makes it cheap
    -- and also what makes it unsafe for a caller that does not RETURN the error
    it is handed. That caller is left holding an unread stream, and if it then
    parses the body itself it does so with no cap at all. One caller in the tree
    is exactly that shape: ``POST /api/messaging/teams`` forwards only a 413,
    because a verdict derived from body CONTENT must not precede its JWT check.
    The end-to-end consequence is pinned in ``test_teams_webhook_hardening.py``;
    these pin the switch itself, including that it switches off the 415 and
    NOTHING else.
    """

    @pytest.mark.asyncio
    async def test_the_gate_is_on_by_default(self):
        # Negative control for every test below: the same request, same media
        # type, refused when the caller does not opt out. Without this, a green
        # run could mean the fake simply never trips the gate.
        raw = b'{"a": 1}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type="text/plain")
        )
        assert body is None
        assert err is not None and err.status == 415

    @pytest.mark.asyncio
    @pytest.mark.parametrize("content_type", ["text/plain", "application/octet-stream"])
    async def test_opting_out_parses_a_non_json_media_type(self, content_type: str):
        raw = b'{"a": 1}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type=content_type),
            require_json_content_type=False,
        )
        assert err is None
        assert body == {"a": 1}

    @pytest.mark.asyncio
    async def test_opting_out_keeps_the_streamed_cap(self):
        """The whole point. A chunked body carries no Content-Length, so the cap
        holds only on the incremental read -- and only if the 415 did not return
        first and leave the stream untouched."""
        body, err = await read_bounded_json(
            _FakeRequest(b"x" * 100, content_length=None, content_type="text/plain"),
            max_bytes=16,
            require_json_content_type=False,
        )
        assert body is None
        assert err is not None and err.status == 413
        assert _code(err) == "payload_too_large"

    @pytest.mark.asyncio
    async def test_opting_out_keeps_the_declared_oversize_413(self):
        body, err = await read_bounded_json(
            _FakeRequest(
                b"", content_length=_MAX_BODY_BYTES + 1, content_type="application/octet-stream"
            ),
            require_json_content_type=False,
        )
        assert body is None
        assert err is not None and err.status == 413
        assert _code(err) == "payload_too_large"

    @pytest.mark.asyncio
    async def test_opting_out_keeps_the_parse_and_shape_guards(self):
        body, err = await read_bounded_json(
            _FakeRequest(b"{not json", content_length=9, content_type="text/plain"),
            require_json_content_type=False,
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "invalid_json"

        raw = b"[1, 2]"
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type="text/plain"),
            require_json_content_type=False,
        )
        assert body is None
        assert err is not None and err.status == 400
        assert _code(err) == "body_not_object"

    @pytest.mark.asyncio
    async def test_opting_out_of_an_uncapped_read_still_parses(self):
        # ``max_bytes=None`` has no cap to preserve, so the opt-out there buys
        # only the parse. Pinned so the flag's meaning does not quietly narrow to
        # "capped reads only".
        raw = b'{"a": 1}'
        body, err = await read_bounded_json(
            _FakeRequest(raw, content_length=len(raw), content_type="text/plain"),
            max_bytes=None,
            require_json_content_type=False,
        )
        assert err is None
        assert body == {"a": 1}

    def test_the_switch_is_keyword_only(self):
        """A positional third argument would land on a different parameter as the
        signature grows; the opt-out must be spelled at every call site."""
        import inspect

        param = inspect.signature(read_bounded_json).parameters["require_json_content_type"]
        assert param.kind is inspect.Parameter.KEYWORD_ONLY
        assert param.default is True


class TestAiohttpContentTypeSemantics:
    """The gate reads ``request.content_type``; pin what aiohttp puts there.

    Every test above builds a fake request, so none of them can catch the gate
    misreading the real library. Two aiohttp behaviours the gate depends on are
    not guessable from the header the client sent, and each would break the gate
    in a different direction if it were wrong: a wrong absent-header default
    would let an unpreflighted body through, and parameters riding along in
    ``content_type`` would refuse every ordinary charset-bearing request.
    """

    @pytest.mark.asyncio
    async def test_a_charset_parameter_does_not_reach_content_type(self):
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestClient, TestServer

        seen: list[tuple[str, str | None]] = []

        async def handler(request: aioweb.Request) -> aioweb.Response:
            seen.append((request.content_type, request.charset))
            body, err = await read_bounded_json(request)
            return err if err is not None else aioweb.json_response(body)

        app = aioweb.Application()
        app.router.add_post("/x", handler)
        async with TestClient(TestServer(app)) as client:
            resp = await client.post(
                "/x",
                data=b'{"a": 1}',
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
            assert resp.status == 200, await resp.text()
        assert seen == [("application/json", "utf-8")]

    @pytest.mark.asyncio
    async def test_a_client_that_sends_no_content_type_is_refused(self):
        from aiohttp import web as aioweb
        from aiohttp.test_utils import TestClient, TestServer

        seen: list[str] = []

        async def handler(request: aioweb.Request) -> aioweb.Response:
            seen.append(request.content_type)
            body, err = await read_bounded_json(request)
            return err if err is not None else aioweb.json_response(body)

        app = aioweb.Application()
        app.router.add_post("/x", handler)
        async with TestClient(TestServer(app)) as client:
            # ``skip_auto_headers`` stops the client library adding one for us,
            # which is what a fetch() with an untyped Blob body produces.
            resp = await client.post(
                "/x", data=b'{"a": 1}', skip_auto_headers=["Content-Type"]
            )
            assert resp.status == 415
            assert (await resp.json())["code"] == "unsupported_media_type"
        # The default the gate relies on for the absent header.
        assert seen == ["application/octet-stream"]
