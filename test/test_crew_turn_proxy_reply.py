"""What the headless-crew turn proxy does with the crew's answer.

The reply is redacted before it reaches the caller, capped before it is read and
typed before it is trusted; a crew's refusal is redacted whole before it is cut,
and is reported as the crew's refusal rather than a content-type complaint. Each
outcome of a turn -- success, declined, partial, failure -- leaves an audit record.

The turn target, the secret and crew-name resolution, the refusal matrix and the
forward itself are pinned in ``test_crew_turn_proxy.py``; the fakes both modules
drive the handler with live in ``crew_turn_proxy_helpers.py``.
"""

from __future__ import annotations

import pytest
from crew_turn_proxy_helpers import (
    _capture_stream,
    _code,
    _connected,
    _FakeResp,
    _install_session,
    _turn_request,
    _wire_handler,
)

from kiro_crew.dashboard import handlers_crew_turn as mod


class TestTheCrewsReplyIsRedactedBeforeItReachesTheCaller:
    """A crew's reply is model output, and model output can carry a credential.

    The crew runs with a Kiro identity and its own control secret in its
    environment, and it reads whatever its tools fetch. So a reply can quote a
    credential either because the model repeated its own environment or because an
    injected page told it to. This route's answer lands in the owner's browser and
    in a transcript, so it is redacted on the way out like every other peer reply.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @pytest.mark.asyncio
    async def test_a_non_streamed_reply_is_redacted(self, monkeypatch):
        import json

        _wire_handler(monkeypatch, status=_connected(), secret="S3C")
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, text=json.dumps({"answer": f"your key is {self._KEY}"})),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )

        assert self._KEY.encode() not in resp.body
        assert b"REDACTED" in resp.body
        # Still the JSON the caller has to parse, not a redacted blob.
        assert json.loads(resp.body)["answer"].startswith("your key is ")

    @pytest.mark.asyncio
    async def test_a_clean_reply_is_passed_through_unchanged(self, monkeypatch):
        """Non-vacuity: the redactor must not be rewriting every answer, or the
        assertions above would pass on a route that returned nothing useful."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"answer": "aarch64"}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert json.loads(resp.body) == {"answer": "aarch64"}

    @pytest.mark.asyncio
    async def test_a_streamed_reply_is_redacted(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        event = b'data: {"delta": "key ' + self._KEY.encode() + b'"}\n\n'
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=(event,)))
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written
        assert b"REDACTED" in written

    @pytest.mark.asyncio
    async def test_a_credential_split_across_two_chunks_is_still_caught(self, monkeypatch):
        """The property that makes this event-by-event rather than chunk-by-chunk.

        ``iter_any`` hands back whatever the socket delivered, so a credential can
        straddle two chunks. Redacting each chunk as it arrived would see neither
        half as a credential and forward both.
        """
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        whole = b'data: {"delta": "key ' + self._KEY.encode() + b'"}\n\n'
        cut = len(b'data: {"delta": "key ') + 8
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=200, chunks=(whole[:cut], whole[cut:]))
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written, "a split credential was forwarded"
        assert b"REDACTED" in written

    @pytest.mark.asyncio
    async def test_a_refusal_detail_from_the_crew_is_redacted(self, monkeypatch):
        """The crew's refusal is the crew's own words too, and it can quote the
        request it refused."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=403, text=f"refused for {self._KEY}")
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames)
        assert self._KEY.encode() not in written
        assert b"crew_refused" in written

    @pytest.mark.asyncio
    async def test_a_leading_bom_does_not_hide_an_escaped_credential(self, monkeypatch):
        """A BOM at the head of the stream must not cost the JSON-aware pass.

        ``_redact_sse_event`` sends a ``data:`` line through the JSON decoder, which
        is what sees a credential written as a JSON escape; every other line goes
        through the plain-text chain, which cannot. So a stream whose first line is
        ``\\ufeffdata: ...`` does not match ``data:``, takes the text chain, and the
        escaped credential passes through -- while the browser, which skips the BOM
        per the SSE spec, parses the field and decodes the credential back.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The literal bytes never spell the key id: only a JSON decode produces it.
        escaped = b"\\u0041" + self._KEY[1:].encode()
        event = b"\xef\xbb\xbf" + b'data: {"delta": "key ' + escaped + b'"}\n\n'
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=(event,)))
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames).decode()
        # Read it the way a BOM-stripping EventSource client does.
        payload = "\n".join(
            line[len("data: ") :]
            for line in written.removeprefix("\ufeff").split("\n")
            if line.startswith("data: ")
        )
        assert self._KEY not in json.dumps(json.loads(payload)), (
            "the BOM sent the data line through the plain-text chain, so the "
            "escaped credential reached the caller"
        )

    @pytest.mark.asyncio
    async def test_a_bom_split_across_chunks_is_still_stripped(self, monkeypatch):
        """The BOM is three bytes and the socket may split it.

        Stripping only when the whole prefix has arrived is why the strip waits for
        the third byte instead of testing the first chunk and giving up.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        escaped = b"\\u0041" + self._KEY[1:].encode()
        stream = b"\xef\xbb\xbf" + b'data: {"delta": "key ' + escaped + b'"}\n\n'
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, chunks=(stream[:1], stream[1:2], stream[2:])),
        )
        resp = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(resp.frames).decode()
        payload = "\n".join(
            line[len("data: ") :]
            for line in written.removeprefix("\ufeff").split("\n")
            if line.startswith("data: ")
        )
        assert self._KEY not in json.dumps(json.loads(payload))


class TestTheCrewsReplyIsBoundedAndTyped:
    """What the far end sends is not what this gateway agrees to hold or forward.

    The crew is a separate machine running a model with tools. Its reply can be
    arbitrarily large, of any content type, and -- once it is on the loop -- the
    redaction of it is CPU work on the sole event loop that also serves chat,
    websockets, timers and the heartbeat. So the reply is capped before it is
    read, typed before it is trusted, and scrubbed in a worker thread.

    The peer proxy already does all three; these pin that this route does too.
    """

    @staticmethod
    def _small_cap(monkeypatch, limit=64):
        """Shrink the cap this module reads, rather than build a real 8 MB reply."""
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", limit)

    def test_the_cap_is_the_shared_one_and_not_a_local_number(self):
        """The shrink above is only legitimate if the module reads the shared cap."""
        from kiro_crew.instances import constants

        assert mod.PROXY_REDACT_BUFFER_MAX_BYTES == constants.PROXY_REDACT_BUFFER_MAX_BYTES

    @pytest.mark.asyncio
    async def test_an_oversized_non_streamed_reply_is_refused_not_buffered(self, monkeypatch):
        """``resp.text()`` would read all of it; the cap stops at the limit.

        What makes this a refusal rather than a truncation: a partial JSON
        document cannot be redacted as a document, so forwarding a prefix of it
        would forward bytes that were never checked.
        """
        _wire_handler(monkeypatch, status=_connected())
        self._small_cap(monkeypatch, 64)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200, chunks=[b"x" * 40, b"y" * 40], content_type="application/json"
            ),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 502
        assert _code(resp) == "crew_reply_too_large"
        assert b"yyyy" not in resp.body

    @pytest.mark.asyncio
    async def test_a_reply_at_the_cap_is_still_forwarded(self, monkeypatch):
        """Non-vacuity: the cap refuses what is OVER it, not everything."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        body = json.dumps({"answer": "a" * 40})
        self._small_cap(monkeypatch, len(body))
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text=body))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert json.loads(resp.body)["answer"] == "a" * 40

    @pytest.mark.asyncio
    async def test_the_redaction_does_not_run_on_the_event_loop(self, monkeypatch):
        """Walking and re-serializing a JSON document is CPU work, and on the loop
        it stalls every other connection this gateway is serving.

        Asserted by the THREAD the redactor observes itself on, not by reading the
        source for ``to_thread``: a call that was moved back onto the loop by any
        other route still fails this.
        """
        import threading

        _wire_handler(monkeypatch, status=_connected())
        seen: list[int] = []

        def _record(text):
            seen.append(threading.get_ident())
            return text

        monkeypatch.setattr(mod, "_redact_peer_payload", _record)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"answer": "ok"}'))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        assert seen, "the reply was forwarded without being redacted at all"
        assert seen[0] != threading.get_ident(), "the redaction ran on the event loop"

    @pytest.mark.asyncio
    async def test_a_non_json_reply_is_refused_before_its_body_is_read(self, monkeypatch):
        """A crew made to serve HTML must not have it land on the hub's own origin,
        where it would run as the authenticated owner."""
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=200, text="<script>alert(1)</script>", content_type="text/html"),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 502
        assert _code(resp) == "crew_content_type_refused"
        assert b"script" not in resp.body

    @pytest.mark.asyncio
    async def test_a_reply_with_no_content_type_is_refused(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch, [], resp=_FakeResp(status=200, text='{"ok": 1}', content_type="")
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert _code(resp) == "crew_content_type_refused"

    @pytest.mark.asyncio
    async def test_a_charset_parameter_does_not_refuse_a_json_reply(self, monkeypatch):
        """``application/json; charset=utf-8`` is JSON. The gate reads the media
        type, so a crew that names its charset is not refused for it."""
        _wire_handler(monkeypatch, status=_connected())
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200, text='{"ok": 1}', content_type="Application/JSON; charset=utf-8"
            ),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_streamed_200_that_is_not_sse_is_refused_as_a_frame(self, monkeypatch):
        """Without the gate a JSON body with no ``data:`` framing is ONE event to
        the reassembly, which sends it down ``_redact_sse_event``'s plain-text
        chain -- the chain that cannot see a credential written as a JSON escape.

        The head went out as 200 before the crew answered, so the refusal is a
        frame; what matters is that none of the body is relayed.
        """
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200,
                chunks=[b'{"choices": [{"delta": {"content": "hi"}}]}'],
                content_type="application/json",
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        written = b"".join(out.frames)
        assert b"choices" not in written, "an unframed body was relayed to the pane"
        assert out.frames[-1] == b"data: [DONE]\n\n"
        first = json.loads(out.frames[0][len(b"data: ") :].decode())
        assert first["error"]["code"] == "crew_content_type_refused"

    @pytest.mark.asyncio
    async def test_a_real_sse_stream_is_still_relayed(self, monkeypatch):
        """Non-vacuity for the gate above."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(
                status=200,
                chunks=[b"data: {}\n\n"],
                content_type="text/event-stream; charset=utf-8",
            ),
        )
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert out.frames == [b"data: {}\n\n"]


class TestTheCrewsRefusalIsRedactedBeforeItIsCut:
    """The streamed error frame carries a slice of the crew's refusal body.

    Cut first and the slice is raw text, so a credential that straddles the cut
    is two fragments the redactor does not recognise and the leading one is
    forwarded -- and a raw-text pass has no JSON decode, which is the pass that
    sees a credential written as a ``\\uXXXX`` escape. So the body is capped,
    redacted whole, and only then cut.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _detail(out):
        import json

        return json.loads(out.frames[0][len(b"data: ") :].decode())["error"]["detail"]

    @pytest.mark.asyncio
    async def test_a_credential_straddling_the_cut_does_not_survive_in_part(self, monkeypatch):
        """Ten characters of an access key id is what cutting first forwarded."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The key starts at 390 and the frame keeps 400 characters, so a cut
        # taken BEFORE the redaction leaves its first ten characters behind.
        body = "refused: " + "x" * 381 + self._KEY + " trailing"
        assert (
            body.index(self._KEY)
            < mod._REFUSAL_DETAIL_CHARS
            < body.index(self._KEY) + len(self._KEY)
        ), "the fixture no longer straddles the cut"
        _install_session(monkeypatch, [], resp=_FakeResp(status=403, text=body))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        assert self._KEY not in detail
        assert self._KEY[:10] not in detail, "the cut split the key and forwarded its head"
        assert len(detail) <= mod._REFUSAL_DETAIL_CHARS

    @pytest.mark.asyncio
    async def test_a_credential_written_as_a_json_escape_is_redacted(self, monkeypatch):
        """``redact_peer_text`` runs on the bytes as they arrived, so an escaped
        credential reads as prose to it and as a credential to the browser.
        ``_redact_peer_payload`` decodes the document first."""
        import json

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        # The literal bytes never spell the key id: only a JSON decode produces it.
        escaped = '{"detail": "refused for \\u0041' + self._KEY[1:] + '"}'
        assert self._KEY not in escaped
        _install_session(monkeypatch, [], resp=_FakeResp(status=403, text=escaped))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        # Read it the way the pane does: the frame's detail is itself a JSON
        # document here, so decode it before looking for the key.
        decoded = json.dumps(json.loads(detail)) if detail.startswith("{") else detail
        assert self._KEY not in decoded, "the escaped credential reached the pane"

    @pytest.mark.asyncio
    async def test_an_oversized_refusal_body_is_not_buffered_whole(self, monkeypatch):
        """And the frame says the detail was withheld rather than carrying nothing."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", 32)
        _install_session(monkeypatch, [], resp=_FakeResp(status=500, chunks=[b"z" * 20, b"w" * 20]))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        detail = self._detail(out)
        assert "wwww" not in detail
        assert "too large" in detail

    @pytest.mark.asyncio
    async def test_the_crews_own_status_still_reaches_the_frame(self, monkeypatch):
        """Non-vacuity: the refusal frame is still a refusal frame."""
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=404, text="no such crew"))
        out = await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        import json

        error = json.loads(out.frames[0][len(b"data: ") :].decode())["error"]
        assert error["status"] == 404
        assert error["detail"] == "no such crew"


class TestANonStreamedCrewErrorIsARefusalNotAContentTypeComplaint:
    """A crew's 4xx/5xx on the non-streamed path is reported as what it is.

    The content-type gate is for a reply this route would FORWARD. A refusal body
    is never forwarded, so gating it by type reported the crew's error as
    ``crew_content_type_refused`` -- which names the gateway's own check and
    hides the status and the words the crew actually sent. Both types of refusal
    body now become ``crew_refused``, carrying the crew's status and the same
    capped, redacted slice the streamed frame carries.
    """

    _KEY = "AKIAIOSFODNN7EXAMPLE"

    @staticmethod
    def _payload(resp):
        import json as _json

        return _json.loads(resp.body)

    async def _turn(self, monkeypatch, resp):
        _wire_handler(monkeypatch, status=_connected())
        _install_session(monkeypatch, [], resp=resp)
        return await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )

    @pytest.mark.asyncio
    async def test_a_json_error_is_the_crews_refusal(self, monkeypatch):
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=400, text='{"detail": "no such slot"}'),
        )
        payload = self._payload(resp)
        assert resp.status == 400
        assert payload["code"] == "crew_refused"
        assert payload["status"] == 400
        assert "no such slot" in payload["detail"]

    @pytest.mark.asyncio
    async def test_a_text_plain_error_is_the_crews_refusal_too(self, monkeypatch):
        """A refusal body's content type does not decide which refusal is reported."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=429, text="slow down", content_type="text/plain"),
        )
        payload = self._payload(resp)
        assert payload["code"] == "crew_refused"
        assert payload["code"] != "crew_content_type_refused"
        assert payload["status"] == 429
        assert payload["detail"] == "slow down"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_a_forward_that_changed_mid_turn_gets_no_secret(self, monkeypatch, stream):
        """The tunnel drops while the secret is read and another forward takes the
        port. The second status read sees a new connection, so nothing is posted."""
        import kiro_crew.dashboard.handlers_instances as hi

        calls: list = []
        _wire_handler(monkeypatch, status=_connected(connected_at=1.0, local_port=18081))
        reads = iter(
            [
                _connected(connected_at=1.0, local_port=18081),
                _connected(connected_at=2.0, local_port=18081),
            ]
        )
        monkeypatch.setattr(hi, "_status_for", lambda state, instance_id: next(reads))
        if stream:
            _capture_stream(monkeypatch)
        _install_session(monkeypatch, calls, resp=_FakeResp(status=200, text="{}"))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": stream})
        )
        assert calls == [], "the control secret was posted to a forward that changed"
        if stream:
            assert any(b"crew_tunnel_changed" in f for f in resp.frames), resp.frames
        else:
            assert resp.status == 409
            assert _code(resp) == "crew_tunnel_changed"

    @pytest.mark.asyncio
    async def test_a_redirect_is_refused_and_not_forwarded(self, monkeypatch):
        """A 3xx is never followed, so it is answered as a refusal, never relayed."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=307, text='{"choices": []}'),
        )
        payload = self._payload(resp)
        assert resp.status == 502
        assert payload["code"] == "crew_refused"
        assert payload["status"] == 307

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stream", [False, True])
    async def test_neither_path_follows_a_redirect(self, monkeypatch, stream):
        """Following a redirect would carry the control secret out of the tunnel."""
        calls: list = []
        _wire_handler(monkeypatch, status=_connected())
        if stream:
            _capture_stream(monkeypatch)
            resp = _FakeResp(status=307, chunks=(b"",), content_type="text/event-stream")
        else:
            resp = _FakeResp(status=307, text="{}")
        _install_session(monkeypatch, calls, resp=resp)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": stream}))
        assert calls, "the crew was never called"
        assert all(c["allow_redirects"] is False for c in calls), calls

    @pytest.mark.asyncio
    async def test_a_text_plain_200_is_still_refused_for_its_type(self, monkeypatch):
        """Non-vacuity: the gate still holds for a reply this route would forward."""
        resp = await self._turn(
            monkeypatch,
            _FakeResp(status=200, text="not json at all", content_type="text/plain"),
        )
        assert resp.status == 502
        assert _code(resp) == "crew_content_type_refused"
        assert b"not json at all" not in resp.body

    @pytest.mark.asyncio
    async def test_the_detail_is_redacted_before_it_is_cut(self, monkeypatch):
        """Same order as the streamed frame: capped, redacted whole, then cut.

        Cut first and a credential straddling the cut is two fragments the
        redactor does not recognise, and the leading one is forwarded.
        """
        body = "refused: " + "x" * 381 + self._KEY + " trailing"
        assert (
            body.index(self._KEY)
            < mod._REFUSAL_DETAIL_CHARS
            < body.index(self._KEY) + len(self._KEY)
        ), "the fixture no longer straddles the cut"
        resp = await self._turn(monkeypatch, _FakeResp(status=403, text=body))
        payload = self._payload(resp)
        assert payload["code"] == "crew_refused"
        detail = payload["detail"]
        assert self._KEY not in detail
        assert self._KEY[:10] not in detail, "the cut split the key and forwarded its head"
        assert len(detail) <= mod._REFUSAL_DETAIL_CHARS

    @pytest.mark.asyncio
    async def test_an_oversized_error_body_is_not_buffered_whole(self, monkeypatch):
        """And the detail says it was withheld rather than carrying nothing."""
        _wire_handler(monkeypatch, status=_connected())
        monkeypatch.setattr(mod, "PROXY_REDACT_BUFFER_MAX_BYTES", 32)
        _install_session(
            monkeypatch,
            [],
            resp=_FakeResp(status=500, chunks=[b"z" * 20, b"w" * 20], content_type="text/plain"),
        )
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        detail = self._payload(resp)["detail"]
        assert "wwww" not in detail
        assert "too large" in detail


class TestEveryTurnIsAudited:
    """A turn sends this crew's control secret down a tunnel, so the SEL trail has
    to hold the turns that WORKED and not only the ones that were refused.

    Nothing else records it: the route logs a line for each failure arm and
    nothing at all for a success, so a reader asking which crew this gateway
    authenticated to, and when, had no record to read.
    """

    @staticmethod
    def _record(monkeypatch):
        events: list[tuple] = []

        def _audit(operation, outcome, *, request_id="", error=""):
            events.append((operation, outcome, request_id, error))

        monkeypatch.setattr(mod, "_audit", _audit)
        return events

    def test_the_route_audits_under_one_operation_name(self):
        """The shared gate audits its own denials with this name, so a reader sees
        one operation per turn rather than two that have to be joined."""
        import inspect

        assert mod._AUDIT_OP == "crew_turn"
        assert '_guard(request, "crew_turn")' in inspect.getsource(mod.api_crew_turn)

    @pytest.mark.asyncio
    async def test_a_successful_non_streamed_turn_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, text='{"ok": 1}'))
        resp = await mod.api_crew_turn(
            _turn_request({"message": "hi", "thread": "t", "stream": False})
        )
        assert resp.status == 200
        assert ("crew_turn", "success", "i1", "") in events

    @pytest.mark.asyncio
    async def test_a_non_streamed_crew_error_is_not_audited_as_a_success(self, monkeypatch):
        """The crew answered, so the turn did not fail -- but it did not succeed
        either, and a trail that calls a 500 a success cannot be read for which
        turns the crew actually took. Audited as ``declined``, the same outcome
        the streamed path records for the same refusal."""
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=500, text='{"detail": "boom"}'))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        outcomes = [outcome for _, outcome, _, _ in events]
        assert "success" not in outcomes
        assert ("crew_turn", "declined", "i1", "crew refused with 500") in events

    @pytest.mark.asyncio
    async def test_a_successful_streamed_turn_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=[b"data: {}\n\n"]))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))
        assert ("crew_turn", "success", "i1", "") in events

    @pytest.mark.asyncio
    async def test_a_stream_that_ends_unredactable_is_audited_as_partial_not_success(
        self, monkeypatch
    ):
        """Some frames went out and the rest were dropped, which is neither of the
        two outcomes a reader would otherwise infer."""
        from kiro_crew.dashboard.handlers_instances import ProxyReplyUnredactable

        _wire_handler(monkeypatch, status=_connected())
        _capture_stream(monkeypatch)
        events = self._record(monkeypatch)

        async def _refuse_event(event):
            raise ProxyReplyUnredactable()

        monkeypatch.setattr(mod, "_redact_sse_event_async", _refuse_event)
        _install_session(monkeypatch, [], resp=_FakeResp(status=200, chunks=[b"data: {}\n\n"]))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t"}))

        outcomes = [outcome for _, outcome, _, _ in events]
        assert "partial" in outcomes
        assert "success" not in outcomes

    @pytest.mark.asyncio
    async def test_an_unreachable_crew_is_audited_as_a_failure(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected())
        events = self._record(monkeypatch)
        _install_session(monkeypatch, [], raise_on_post=RuntimeError("connection refused"))
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))

        failures = [e for e in events if e[1] == "failure"]
        assert failures, "an unreachable crew left no audit record"
        # The exception TYPE, never its message: the message can quote the target.
        assert failures[-1][3] == "RuntimeError"
        assert not any("connection refused" in e[3] for e in events)

    @pytest.mark.asyncio
    async def test_a_declined_lane_is_audited(self, monkeypatch):
        inst = _wire_handler(monkeypatch, status=_connected())
        inst.provisioner_id = "aws_fargate"
        events = self._record(monkeypatch)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))
        assert any(outcome == "declined" for _, outcome, _, _ in events)

    @pytest.mark.asyncio
    async def test_an_unavailable_secret_is_audited(self, monkeypatch):
        _wire_handler(monkeypatch, status=_connected(), secret_id="")
        events = self._record(monkeypatch)
        await mod.api_crew_turn(_turn_request({"message": "hi", "thread": "t", "stream": False}))
        assert any(outcome == "failure" for _, outcome, _, _ in events)
