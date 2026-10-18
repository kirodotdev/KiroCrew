"""``api_instances_proxy``: the policy gates and the redaction of peer replies."""

from __future__ import annotations

import contextlib
import json
import types

import pytest
from instances_helpers import _body, _enable, _FakeReq, _State


class TestProxyHandlerPolicy:
    """api_instances_proxy — the policy gates in front of the carrier.

    The streaming pump itself needs a real transport (StreamResponse.prepare),
    so it is exercised live against a connected peer; every DECISION the
    handler makes before the pump is covered here.
    """

    def _req(self, tmp_path, monkeypatch, *, path, method="GET", manager="stub", enabled=True):
        _enable(tmp_path, monkeypatch, enabled=enabled)
        # The proxy requires the positively-identified OWNER (same bar as
        # federated search); the fake request has no real token subject, so
        # satisfy the gate explicitly. The deny path has its own test below.
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: True)
        from kiro_crew.instances.registry import InstancesRegistry

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        state = _State(reg, manager if manager != "stub" else object())
        req = _FakeReq(state, match={"id": "cd-1", "path": path})
        req.method = method
        return req

    @pytest.mark.asyncio
    async def test_client_disconnect_midstream_does_not_write_after_reset(
        self, tmp_path, monkeypatch
    ):
        """A browser that drops mid-stream must not produce a SECOND write.

        The pump caught `ConnectionResetError` and then fell through to
        `write_eof()` on the very transport that had just refused a write — the
        second exception escaped the handler and crashed the request. The
        handler now returns from the except block, so exactly one write is
        attempted after the reset: none.
        """
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        writes: list[str] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append("write")
                raise ConnectionResetError("client went away")

            async def write_eof(self):
                writes.append("write_eof")  # must never happen after the reset
                raise ConnectionResetError("transport is gone")

        async def _chunks():
            yield b"data: hello\n\n"

        class _Mgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, **kwargs):
                yield types.SimpleNamespace(
                    status=200,
                    headers={"Content-Type": "text/event-stream"},
                    content=types.SimpleNamespace(iter_any=_chunks),
                )

        from kiro_crew.dashboard import handlers_instances as hi

        req = self._req(tmp_path, monkeypatch, path="api/chat/stream", manager=_Mgr())
        req.body_exists = False
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        # Must not raise: the disconnect is terminal, not a crash.
        await api_instances_proxy(req)
        assert writes == ["write"]  # no write_eof on the dead transport

    @pytest.mark.asyncio
    async def test_non_owner_identity_is_refused(self, tmp_path, monkeypatch):
        """A Slack-minted dashboard identity passes _guard but must NOT reach
        the peer: the proxy executes with the owner's manager-held credential."""
        from kiro_crew.dashboard.handlers import source_providers as sp
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots")
        monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: False)
        monkeypatch.setattr(sp, "stale_owner_session_response", lambda r: None)
        resp = await api_instances_proxy(req)
        assert resp.status == 403
        assert _body(resp)["code"] == "owner_only"

    @pytest.mark.asyncio
    async def test_disabled_feature_is_403(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", enabled=False)
        resp = await api_instances_proxy(req)
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_traversal_is_refused_before_any_url_is_built(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/../etc/passwd")
        resp = await api_instances_proxy(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_api_path_is_refused(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="assets/main.js")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.asyncio
    async def test_peer_instances_plane_is_refused_no_chaining(self, tmp_path, monkeypatch):
        """The allowlist subsumes the old explicit deny: `api/instances` is not
        an allowed prefix, so one hub still cannot chain through a peer into a
        third machine's SSH control plane."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/instances/other/proxy/api/status")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.asyncio
    async def test_peer_token_route_is_refused_with_denied_shape(self, tmp_path, monkeypatch):
        """A peer's credential-minting route must never be proxied: its JSON
        reply passes the content-type gate, so a deny-only policy would carry
        a minted peer token back through the hub in-band. The allowlist is the
        no-remote-credential-on-hub invariant, pinned here rather than in prose."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/token/local")
        resp = await api_instances_proxy(req)
        assert resp.status == 400
        assert _body(resp)["code"] == "proxy_path_denied"

    @pytest.mark.parametrize(
        "raw",
        [
            "api/token/local",  # GET /api/token/local mints a dashboard token
            "api/apps/someapp/token",  # POST /api/apps/{name}/token
            "api/webhooks/tokens",  # POST /api/webhooks/tokens
            "api/status",  # harmless, but not part of the chat surface either
            "api",  # the bare prefix names no endpoint
            "api/chatx/slots",  # allowed prefix must match whole segments
        ],
    )
    def test_paths_outside_the_chat_allowlist_are_refused(self, raw):
        """The vet policy is a positive prefix allowlist (`api/chat` today):
        anything the feature never asked for is refused by default instead of
        proxied silently — including every future sensitive peer endpoint."""
        from kiro_crew.dashboard.handlers_instances import (
            _PROXY_PATH_DENIED_REASON,
            _proxy_canonical_path,
        )

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == _PROXY_PATH_DENIED_REASON

    def test_bare_chat_prefix_itself_is_forwarded(self):
        """`POST /api/chat` is the primary send route the feature rides on —
        the prefix itself must pass, not only paths strictly beneath it."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("api/chat") == ("api/chat", "")
        assert _proxy_canonical_path("/api/chat/") == ("api/chat", "")

    def test_event_stream_prefix_is_forwarded(self):
        """`GET /api/stream` is the out-of-turn half of the chat view: the peer's
        own SSE broadcast, carrying session-list and slot-state changes while the
        per-turn reply streams back from `api/chat`. It is a leaf endpoint, so
        the bare prefix is the whole surface this row grants."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("api/stream") == ("api/stream", "")
        assert _proxy_canonical_path("/api/stream/") == ("api/stream", "")

    @pytest.mark.parametrize(
        "raw",
        [
            "api/ws",  # the SSE sibling: an upgrade cannot cross this proxy
            "api/ws/stt",
            "api/streaming",  # whole-segment match, not a string prefix
            "api/stream-x",
            "api/file-stream",  # a DIFFERENT endpoint that merely ends in stream
        ],
    )
    def test_stream_row_does_not_admit_its_neighbours(self, raw):
        """The row is `("api", "stream")` — whole segments, nothing adjacent.

        `api/ws` is the one to keep refused on purpose: it is the same event bus
        over WebSocket, and admitting it would require a `101 Switching
        Protocols` to pass the reply content-type gate that exists to stop a peer
        serving active content onto the authenticated hub origin.
        """
        from kiro_crew.dashboard.handlers_instances import (
            _PROXY_PATH_DENIED_REASON,
            _proxy_canonical_path,
        )

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == _PROXY_PATH_DENIED_REASON

    def test_allowlist_constant_is_pinned_exactly(self):
        """Widening the proxied surface must be a REVIEWED act: this pins the
        constant's exact value, so adding a row fails here until the test is
        updated alongside it. The shape floor (every row >= 2 segments, rooted
        at `api`) guards the fail-open edits an exact pin alone would also
        catch — kept separate so the failure message names the broken
        invariant."""
        from kiro_crew.dashboard.handlers_instances import _PROXY_ALLOWED_PREFIXES

        assert _PROXY_ALLOWED_PREFIXES == (("api", "chat"), ("api", "stream"))
        for prefix in _PROXY_ALLOWED_PREFIXES:
            # An empty row prefix-matches EVERYTHING and a one-segment row
            # restores the whole peer /api/ surface; both must be impossible.
            assert len(prefix) >= 2
            assert prefix[0] == "api"
            assert all(isinstance(seg, str) and seg for seg in prefix)

    def test_malformed_allowlist_row_fails_closed(self, monkeypatch):
        """Even if a bad edit ships an empty or one-segment row, the vet must
        not widen: rows shallower than two segments are ignored, so the
        policy degrades to refusing more, never to forwarding more."""
        from kiro_crew.dashboard import handlers_instances as hi

        monkeypatch.setattr(hi, "_PROXY_ALLOWED_PREFIXES", ((), ("api",)))
        path, reason = hi._proxy_canonical_path("api/token/local")
        assert path == ""
        assert reason

    @pytest.mark.parametrize(
        "raw",
        [
            "api/%2e%2e/api/instances/x",  # one layer: the router already decoded one
            "api/%252e%252e/api/instances/x",  # two layers
            "api/%25252e%25252e/api/instances/x",  # three
            "api/..%2fapi%2finstances/x",  # encoded separator, raw dots
            "api/%2e%2e%2fapi%2finstances/x",  # both encoded
        ],
    )
    def test_encoded_traversal_cannot_reach_the_control_plane(self, raw):
        """Every encoding depth resolves to the SAME refusal.

        The denylist shape this replaced inspected a half-decoded string while
        the peer resolved the fully-decoded one, so `%252e%252e` arrived as
        `%2e%2e`, matched no rule, and normalized back into `api/instances`.
        Decoding to a fixed point before vetting is what closes the class —
        so this is parametrized over depth rather than pinned to one payload.
        """
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason

    def test_canonical_path_is_rebuilt_from_vetted_segments(self):
        """The forwarded path is constructed, not merely approved: a caller
        cannot get one string past the policy and a different one onto the
        wire."""
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        assert _proxy_canonical_path("/api/chat/slots/") == ("api/chat/slots", "")
        assert _proxy_canonical_path("api/%63hat/slots") == ("api/chat/slots", "")
        # `instances` only matters as the FIRST segment under api/ — a session
        # or resource that merely contains the word is still reachable.
        assert _proxy_canonical_path("api/chat/instances") == ("api/chat/instances", "")

    @pytest.mark.parametrize(
        "raw,expect",
        [
            ("api//chat", "empty path segment"),
            ("api/chat%00/x", "illegal character in path segment"),
            ("api/ch at/x", "illegal character in path segment"),
            ("api/%zz/x", "malformed percent-encoding in path"),
            ("api/.../x", "path traversal"),
        ],
    )
    def test_only_plainly_named_segments_are_forwarded(self, raw, expect):
        from kiro_crew.dashboard.handlers_instances import _proxy_canonical_path

        path, reason = _proxy_canonical_path(raw)
        assert path == ""
        assert reason == expect

    @pytest.mark.asyncio
    async def test_disallowed_method_is_405(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", method="OPTIONS")
        resp = await api_instances_proxy(req)
        assert resp.status == 405

    @pytest.mark.asyncio
    async def test_missing_manager_is_503(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=None)
        resp = await api_instances_proxy(req)
        assert resp.status == 503

    @pytest.mark.asyncio
    async def test_hub_token_is_stripped_from_forwarded_query(self, tmp_path, monkeypatch):
        """The browser's ?token= is the HUB's credential — it must never
        cross the tunnel (a peer receiving it holds a replayable credential
        for this gateway)."""
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        captured: dict = {}

        class _Mgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, **kwargs):
                captured.update(kwargs)
                # Refused content type short-circuits before StreamResponse.prepare,
                # so the handler stays testable without a real transport.
                yield types.SimpleNamespace(status=200, headers={"Content-Type": "text/html"})

        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=_Mgr())
        req.query = {"token": "HUB_SECRET", "limit": "5"}
        req.body_exists = False
        resp = await api_instances_proxy(req)
        assert captured["params"] == {"limit": "5"}  # token stripped
        # ... and the HTML reply was refused (content-type gate).
        assert resp.status == 502
        assert _body(resp)["code"] == "proxy_content_type_refused"

    @pytest.mark.asyncio
    async def test_error_bodies_carry_machine_readable_codes(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        req = self._req(tmp_path, monkeypatch, path="assets/x.js")
        assert _body(await api_instances_proxy(req))["code"] == "proxy_path_denied"
        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", method="OPTIONS")
        assert _body(await api_instances_proxy(req))["code"] == "proxy_method_not_allowed"
        req = self._req(tmp_path, monkeypatch, path="api/chat/slots", manager=None)
        assert _body(await api_instances_proxy(req))["code"] == "instances_manager_unavailable"


class TestProxyRedactsPeerReplies:
    """The window renders peer text straight from the proxy, so the proxy is
    the read-path redaction point: a credential the peer emits never reaches
    the browser, in a JSON body or in an SSE frame."""

    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def _req(self, tmp_path, monkeypatch, *, path, chunks, content_type):
        _enable(tmp_path, monkeypatch)
        from kiro_crew.dashboard.handlers import source_providers as sp

        monkeypatch.setattr(sp, "is_owner_dashboard_request", lambda r: True)
        from kiro_crew.instances.registry import InstancesRegistry

        async def _iter():
            for c in chunks:
                yield c

        class _Mgr:
            @contextlib.asynccontextmanager
            async def proxy_request(self, iid, method, path, **kwargs):
                yield types.SimpleNamespace(
                    status=200,
                    headers={"Content-Type": content_type},
                    content=types.SimpleNamespace(iter_any=_iter),
                )

        reg = InstancesRegistry(path=tmp_path / "instances.json")
        req = _FakeReq(_State(reg, _Mgr()), match={"id": "cd-1", "path": path})
        req.method = "GET"
        req.body_exists = False
        return req

    def test_every_forwarded_content_type_has_a_redaction_path(self):
        """The proxy redacts JSON and SSE only, so it may forward nothing else:
        a third allowed type would reach the window unredacted."""
        from kiro_crew.dashboard.handlers_instances import _PROXY_RESP_ALLOW_CONTENT_TYPES

        assert _PROXY_RESP_ALLOW_CONTENT_TYPES == frozenset(
            {"application/json", "text/event-stream"}
        )

    @pytest.mark.asyncio
    async def test_json_body_strings_are_redacted(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        doc = {"messages": [{"role": "assistant", "content": f"key {self.SECRET} here"}]}
        raw = json.dumps(doc).encode()
        # Split mid-secret: the proxy must redact the whole document, not chunks.
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[raw[:20], raw[20:]],
            content_type="application/json",
        )
        resp = await api_instances_proxy(req)
        assert resp.status == 200
        assert self.SECRET not in resp.body.decode()
        assert _body(resp)["messages"][0]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_json_escape_cannot_hide_a_secret(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        hidden = '{"content": "\\u0041' + self.SECRET[1:] + '"}'
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[hidden.encode()],
            content_type="application/json",
        )
        resp = await api_instances_proxy(req)
        assert self.SECRET not in json.dumps(_body(resp))

    @pytest.mark.asyncio
    async def test_too_deeply_nested_json_is_refused_not_forwarded(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        deep = (b"[" * 200_000) + b'"' + self.SECRET.encode() + b'"' + (b"]" * 200_000)
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[deep],
            content_type="application/json",
        )
        resp = await api_instances_proxy(req)
        assert resp.status == 502
        assert _body(resp)["code"] == "proxy_reply_unredactable"

    @pytest.mark.asyncio
    async def test_a_leading_bom_cannot_hide_an_escaped_secret(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        body = '\ufeff{"content": "\\u0041' + self.SECRET[1:] + '"}'
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[body.encode()],
            content_type="application/json",
        )
        resp = await api_instances_proxy(req)
        assert self.SECRET not in json.dumps(_body(resp))

    @pytest.mark.asyncio
    async def test_json_python_cannot_decode_is_refused_not_forwarded(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.handlers_instances import api_instances_proxy

        body = '{"n": ' + "9" * 5000 + ', "content": "\\u0041' + self.SECRET[1:] + '"}'
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[body.encode()],
            content_type="application/json",
        )
        resp = await api_instances_proxy(req)
        assert resp.status == 502
        assert _body(resp)["code"] == "proxy_reply_unredactable"

    @pytest.mark.asyncio
    async def test_oversized_json_is_refused_not_forwarded(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as hi

        monkeypatch.setattr(hi, "PROXY_REDACT_BUFFER_MAX_BYTES", 16)
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/chat/slots/s1",
            chunks=[b'{"content": "' + self.SECRET.encode() + b'"}'],
            content_type="application/json",
        )
        resp = await hi.api_instances_proxy(req)
        assert resp.status == 502
        assert _body(resp)["code"] == "proxy_reply_too_large"

    @pytest.mark.asyncio
    async def test_sse_frames_are_redacted_across_chunk_splits(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as hi

        writes: list[bytes] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append(bytes(chunk))

            async def write_eof(self):
                return None

        frame = json.dumps({"slot": "s1", "content": f"token {self.SECRET}"})
        stream = f"event: chat_message\ndata: {frame}\n\nevent: dashboard\ndata: {{}}\n\n".encode()
        cut = stream.index(self.SECRET.encode()) + 4
        req = self._req(
            tmp_path,
            monkeypatch,
            path="api/stream",
            chunks=[stream[:cut], stream[cut:]],
            content_type="text/event-stream",
        )
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        await hi.api_instances_proxy(req)
        out = b"".join(writes).decode()
        assert self.SECRET not in out
        # Framing survives: both events, each still a parseable data line.
        events = [e for e in out.split("\n\n") if e]
        assert [e.splitlines()[0] for e in events] == ["event: chat_message", "event: dashboard"]
        assert json.loads(events[0].splitlines()[1][len("data: ") :])["slot"] == "s1"


class TestRedactSseEvent:
    """The browser joins an event's data lines before parsing, so redaction
    must see the joined payload, whatever the line ending."""

    SECRET = "AKIA" + "IOSFODNN7EXAMPLE"

    def test_secret_split_over_data_lines_behind_an_escape_is_redacted(self):
        from kiro_crew.dashboard.handlers_instances import _redact_sse_event

        event = (
            'event: chat_message\ndata: {"slot": "s1",\ndata: "content": "\\u0041'
            + self.SECRET[1:]
            + '"}'
        ).encode()
        out = _redact_sse_event(event).decode()
        payload = "\n".join(
            line[len("data: ") :] for line in out.split("\n") if line.startswith("data:")
        )
        assert self.SECRET not in json.dumps(json.loads(payload))
        assert json.loads(payload)["slot"] == "s1"

    @pytest.mark.asyncio
    async def test_a_bom_split_across_chunks_is_stripped(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as hi

        writes: list[bytes] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append(bytes(chunk))

            async def write_eof(self):
                return None

        payload = ('data: {"c": "\\u0041' + self.SECRET[1:] + '"}\n\n').encode()
        stream = b"\xef\xbb\xbf" + payload
        req = TestProxyRedactsPeerReplies()._req(
            tmp_path,
            monkeypatch,
            path="api/stream",
            chunks=[stream[:1], stream[1:2], stream[2:]],
            content_type="text/event-stream",
        )
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        await hi.api_instances_proxy(req)
        out = b"".join(writes).decode()
        data = [line[len("data: ") :] for line in out.split("\n") if line.startswith("data:")]
        assert self.SECRET not in json.dumps(json.loads("\n".join(data)))

    @pytest.mark.asyncio
    async def test_cr_line_ends_are_framed_before_redaction(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as hi

        writes: list[bytes] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append(bytes(chunk))

            async def write_eof(self):
                return None

        stream = ('data: {"a":\rdata: "\\u0041' + self.SECRET[1:] + '"}\r\r').encode()
        req = TestProxyRedactsPeerReplies()._req(
            tmp_path,
            monkeypatch,
            path="api/stream",
            chunks=[stream[:14], stream[14:]],
            content_type="text/event-stream",
        )
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        await hi.api_instances_proxy(req)
        out = b"".join(writes).decode()
        data = [line[len("data: ") :] for line in out.split("\n") if line.startswith("data:")]
        assert self.SECRET not in json.dumps(json.loads("\n".join(data)))

    @pytest.mark.asyncio
    async def test_a_completed_event_over_the_cap_is_dropped(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import handlers_instances as hi

        writes: list[bytes] = []

        class _Resp:
            def __init__(self):
                self.headers: dict = {}

            async def prepare(self, request):
                return None

            async def write(self, chunk):
                writes.append(bytes(chunk))

            async def write_eof(self):
                return None

        monkeypatch.setattr(hi, "PROXY_REDACT_BUFFER_MAX_BYTES", 32)
        big = ('data: "' + "x" * 64 + '"\n\n').encode()
        req = TestProxyRedactsPeerReplies()._req(
            tmp_path,
            monkeypatch,
            path="api/stream",
            chunks=[b'data: "ok"\n\n' + big],
            content_type="text/event-stream",
        )
        monkeypatch.setattr(hi.web, "StreamResponse", lambda **kw: _Resp())
        await hi.api_instances_proxy(req)
        out = b"".join(writes).decode()
        assert '"ok"' in out
        assert "x" * 64 not in out
