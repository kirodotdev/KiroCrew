"""Wire-contract tests: each method against a stub Gateway that answers with
the shapes, methods and paths the real routes use.

The stub records every request so a test can assert what went over the wire
(method, path, query, body, cookie) rather than only what came back.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kirocrew_client import GATEWAY_CONFIG_KEYS, KiroCrewClient, KiroCrewError, WsEvent
from kirocrew_client.client import _auth_cookie_name, _compute_backoff
from kirocrew_client.errors import ErrorCode
from kirocrew_client.ws_client import compute_reconnect_delay


class StubGateway:
    """A tiny aiohttp app answering like the Gateway's routes."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[tuple[str, str], Any] = {}
        self.statuses: list[int] = []  # consumed one per request when non-empty
        self.break_stream = False
        self.omit_stream_done = False
        self.chat_response: dict[str, Any] | None = None
        self.app = web.Application()
        self.app.router.add_route("*", "/api/ws", self._ws)
        self.app.router.add_route("*", "/{tail:.*}", self._any)
        self.ws_frames: list[dict[str, Any]] = []
        self.ws_cookies: list[str] = []
        self.ws_origins: list[str] = []
        self.server: TestServer | None = None

    async def _any(self, request: web.Request) -> web.StreamResponse:
        body: Any = None
        ctype = request.headers.get("Content-Type", "")
        if ctype.startswith("multipart/"):
            reader = await request.multipart()
            field = await reader.next()
            body = {
                "field": getattr(field, "name", None),
                "filename": getattr(field, "filename", None),
                "bytes": await field.read() if field is not None else b"",  # type: ignore[union-attr]
            }
        elif request.can_read_body:
            raw = await request.read()
            body = json.loads(raw) if raw else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "raw_path": request.raw_path,
                "query": dict(request.query),
                "body": body,
                "cookie": request.headers.get("Cookie", ""),
            }
        )
        if self.statuses:
            status = self.statuses.pop(0)
            if status >= 400:
                return web.json_response({"error": f"status {status}"}, status=status)
        if request.method == "POST" and request.path == "/api/chat":
            if self.chat_response is not None:
                return web.json_response(self.chat_response)
            return await self._sse(request)
        payload = self.responses.get((request.method, request.path), {"ok": True})
        return web.json_response(payload)

    async def _sse(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        for chunk in ({"cls": "text", "text": "hel"}, {"cls": "text", "text": "lo"}):
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            if self.break_stream:
                transport = request.transport
                assert transport is not None
                transport.close()
                return resp
        if not self.omit_stream_done:
            await resp.write(b"data: [DONE]\n\n")
        return resp

    async def _ws(self, request: web.Request) -> web.StreamResponse:
        origin = request.headers.get("Origin", "")
        self.ws_origins.append(origin)
        if origin != f"{request.scheme}://{request.host}":
            return web.json_response({"error": "cross-origin WebSocket rejected"}, status=403)
        self.ws_cookies.append(request.headers.get("Cookie", ""))
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for frame in self.ws_frames:
            await ws.send_str(json.dumps(frame))
        await ws.close()
        return ws

    @property
    def last(self) -> dict[str, Any]:
        return self.requests[-1]


@pytest.fixture
async def gw():
    stub = StubGateway()
    server = TestServer(stub.app, host="127.0.0.1")
    await server.start_server()
    stub.server = server
    yield stub
    await server.close()


def _client(gw: StubGateway, **kw: Any) -> KiroCrewClient:
    assert gw.server is not None
    kw.setdefault("max_retries", 0)
    return KiroCrewClient(base_url=str(gw.server.make_url("")).rstrip("/"), **kw)


class TestBackoff:
    def test_http_large_attempt_saturates_without_exponentiating(self):
        assert _compute_backoff(5000, 1.0) == 30.0

    def test_websocket_large_attempt_saturates_without_exponentiating(self):
        assert compute_reconnect_delay(5000, 1.0, 30.0) == 30.0


class TestAuthCookie:
    def test_cookie_is_keyed_by_the_dialled_port(self):
        assert _auth_cookie_name("http://localhost:5476") == "mc_token_5476"
        assert _auth_cookie_name("https://crew.example.com") == "mc_token_443"
        assert _auth_cookie_name("http://crew.example.com/") == "mc_token_80"

    async def test_token_is_sent_under_the_port_scoped_name(self, gw):
        async with _client(gw, token="tok") as mc:
            await mc.get_status()
        port = gw.server.port
        assert gw.last["cookie"] == f"mc_token_{port}=tok"

    async def test_cookie_port_override_applies_to_http_and_websocket(self, gw):
        mc = _client(gw, token="tok", cookie_port=7777)
        async with mc:
            await mc.get_status()
            assert gw.last["cookie"] == "mc_token_7777=tok"

            ws = mc.create_ws(reconnect_base_delay=10)
            disconnected = asyncio.Event()
            ws.on_connection_change(
                lambda connected, _reconnect: disconnected.set() if not connected else None
            )
            await ws.connect()
            await asyncio.wait_for(disconnected.wait(), timeout=2)
            await ws.disconnect()

        assert gw.ws_cookies == ["mc_token_7777=tok"]

    async def test_refresh_does_not_spend_the_only_attempt(self, gw):
        # max_retries=0 used to exhaust on the refresh and raise "Request failed".
        gw.statuses = [401]
        gw.responses[("GET", "/api/status")] = {"version": "x"}

        async def refresh() -> str:
            return "fresh"

        async with _client(gw, token="stale", on_auth_expired=refresh) as mc:
            assert await mc.get_status() == {"version": "x"}
        assert gw.last["cookie"].endswith("=fresh")


class TestRequestRetries:
    async def test_post_503_is_not_retried(self, gw):
        gw.statuses = [503]
        async with _client(gw, max_retries=3, retry_base_delay=0) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.ack_all_notifications()
        assert exc.value.code == ErrorCode.SERVER_ERROR
        assert len(gw.requests) == 1

    async def test_get_503_is_retried(self, gw):
        gw.statuses = [503]
        gw.responses[("GET", "/api/status")] = {"version": "ok"}
        async with _client(gw, max_retries=3, retry_base_delay=0) as mc:
            assert await mc.get_status() == {"version": "ok"}
        assert len(gw.requests) == 2

    async def test_post_429_is_retried(self, gw):
        gw.statuses = [429]
        async with _client(gw, max_retries=3, retry_base_delay=0) as mc:
            await mc.ack_all_notifications()
        assert len(gw.requests) == 2

    async def test_post_transport_failure_is_not_retried(self, monkeypatch):
        class FailingRequest:
            async def __aenter__(self):
                raise aiohttp.ClientConnectionError("connection lost")

            async def __aexit__(self, exc_type, exc, tb):
                return None

        class FailingSession:
            def __init__(self):
                self.calls = 0

            def request(self, *args, **kwargs):
                self.calls += 1
                return FailingRequest()

        session = FailingSession()
        mc = KiroCrewClient(max_retries=3, retry_base_delay=0)
        monkeypatch.setattr(mc, "_ensure_session", lambda: session)
        with pytest.raises(KiroCrewError) as exc:
            await mc._post("/api/chat", {"message": "hi"})
        assert exc.value.code == ErrorCode.NETWORK_ERROR
        assert session.calls == 1


class TestShapeFixes:
    async def test_list_wrappers_unwrap_the_gateway_envelope(self, gw):
        gw.responses[("GET", "/api/spawn")] = {"agents": [{"id": "a"}]}
        gw.responses[("GET", "/api/crons")] = {"jobs": [{"id": "j"}], "server_tz": "UTC"}
        gw.responses[("GET", "/api/lessons")] = {"lessons": [{"rule": "r"}], "total": 1}
        gw.responses[("GET", "/api/mcp")] = [{"name": "m"}]
        async with _client(gw) as mc:
            assert await mc.list_subagents() == [{"id": "a"}]
            assert await mc.list_crons() == [{"id": "j"}]
            assert await mc.list_lessons() == [{"rule": "r"}]
            assert await mc.list_mcp_servers() == [{"name": "m"}]

    async def test_missing_list_wrapper_key_is_a_server_error(self, gw):
        gw.responses[("GET", "/api/models")] = {"renamed_models": []}
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.list_models()
        assert exc.value.code == ErrorCode.SERVER_ERROR
        assert str(exc.value) == "unexpected response shape: missing 'models'"

    async def test_update_cron_uses_patch(self, gw):
        async with _client(gw) as mc:
            await mc.update_cron("j/1", enabled=False)
        assert gw.last["method"] == "PATCH"
        assert gw.last["raw_path"] == "/api/crons/j%2F1"
        assert gw.last["body"] == {"enabled": False}

    async def test_memory_search_sends_limit_and_reads_results(self, gw):
        gw.responses[("GET", "/api/memory/episodic/search")] = {"results": [{"id": 1}]}
        async with _client(gw) as mc:
            assert await mc.memory_search("q x", top_k=5) == [{"id": 1}]
        assert gw.last["query"] == {"q": "q x", "limit": "5"}


class TestSlots:
    async def test_history_stop_edit_resend(self, gw):
        async with _client(gw) as mc:
            await mc.get_slot_history("s1", limit=10)
            assert gw.last["query"] == {"limit": "10"}
            await mc.stop_slot("s1", force=True)
            assert (gw.last["path"], gw.last["query"]) == (
                "/api/chat/slots/s1/stop",
                {"force": "true"},
            )
            await mc.edit_resend("s1", "new", index=3)
            assert gw.last["path"] == "/api/chat/slots/s1/edit-resend"
            assert gw.last["body"] == {"content": "new", "index": 3}

    async def test_slot_ids_are_path_escaped(self, gw):
        async with _client(gw) as mc:
            await mc.stop_slot("a/b?c")
        # Unescaped, "a/b?c" would hit /api/chat/slots/a/b with query "c/stop".
        assert gw.last["raw_path"] == "/api/chat/slots/a%2Fb%3Fc/stop"

    async def test_history_rejects_nonpositive_limit(self, gw):
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.get_slot_history("s1", limit=0)
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert gw.requests == []


class TestStreamChat:
    async def test_yields_chunks_until_done(self, gw):
        async with _client(gw) as mc:
            chunks = [c async for c in mc.stream_chat("s1", "hi")]
        assert [c["text"] for c in chunks] == ["hel", "lo"]
        assert gw.last["body"] == {"message": "hi", "slot": "s1"}

    async def test_yields_queued_json_receipt_once(self, gw):
        gw.chat_response = {"ok": True, "queued": True}
        async with _client(gw) as mc:
            chunks = [c async for c in mc.stream_chat("s1", "hi")]
        assert chunks == [{"ok": True, "queued": True}]

    async def test_yields_steered_json_receipt_once(self, gw):
        gw.chat_response = {"ok": True, "steered": True}
        async with _client(gw) as mc:
            chunks = [c async for c in mc.stream_chat("s1", "hi")]
        assert chunks == [{"ok": True, "steered": True}]

    async def test_error_status_raises_before_yielding(self, gw):
        gw.statuses = [404]
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                async for _ in mc.stream_chat("nope", "hi"):
                    pytest.fail("yielded on an error response")
        assert exc.value.code == ErrorCode.NOT_FOUND

    async def test_connection_failure_is_a_network_error(self, unused_tcp_port):
        mc = KiroCrewClient(
            base_url=f"http://127.0.0.1:{unused_tcp_port}",
            max_retries=0,
        )
        async with mc:
            with pytest.raises(KiroCrewError) as exc:
                async for _ in mc.stream_chat("s1", "hi"):
                    pytest.fail("yielded without a connection")
        assert exc.value.code == ErrorCode.NETWORK_ERROR
        assert isinstance(exc.value.__cause__, aiohttp.ClientError)

    async def test_midstream_disconnect_is_a_network_error(self, gw):
        gw.break_stream = True
        chunks = []
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                async for chunk in mc.stream_chat("s1", "hi"):
                    chunks.append(chunk)
        assert chunks == [{"cls": "text", "text": "hel"}]
        assert exc.value.code == ErrorCode.NETWORK_ERROR
        assert isinstance(exc.value.__cause__, aiohttp.ClientError)

    async def test_clean_close_without_done_is_a_network_error(self, gw):
        gw.omit_stream_done = True
        chunks = []
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                async for chunk in mc.stream_chat("s1", "hi"):
                    chunks.append(chunk)
        assert [chunk["text"] for chunk in chunks] == ["hel", "lo"]
        assert exc.value.code == ErrorCode.NETWORK_ERROR
        assert "[DONE]" in str(exc.value)

    async def test_auth_refusal_refreshes_once_before_streaming(self, gw):
        gw.statuses = [401]
        refreshes = 0

        async def refresh() -> str:
            nonlocal refreshes
            refreshes += 1
            return "fresh"

        async with _client(gw, token="stale", on_auth_expired=refresh) as mc:
            chunks = [c async for c in mc.stream_chat("s1", "hi")]

        assert [c["text"] for c in chunks] == ["hel", "lo"]
        assert refreshes == 1
        assert [request["cookie"] for request in gw.requests] == [
            f"mc_token_{gw.server.port}=stale",
            f"mc_token_{gw.server.port}=fresh",
        ]


class TestNotificationsAndApprovals:
    async def test_ack_one_needs_ts_and_ack_all_has_its_own_route(self, gw):
        async with _client(gw) as mc:
            await mc.ack_notification("123.4")
            assert (gw.last["path"], gw.last["body"]) == ("/api/notifications/ack", {"ts": "123.4"})
            await mc.ack_all_notifications()
            assert gw.last["path"] == "/api/notifications/ack-all"
            with pytest.raises(KiroCrewError):
                await mc.ack_notification("")

    async def test_global_resolve_maps_aliases_and_rejects_trust(self, gw):
        async with _client(gw) as mc:
            await mc.resolve_approval("r1", "rejected")
            assert gw.last["path"] == "/api/approvals/r1/reject"
            await mc.resolve_approval("r1", "approved")
            assert gw.last["path"] == "/api/approvals/r1/approve"
            await mc.resolve_approval("r1", "reject_once")
            assert gw.last["path"] == "/api/approvals/r1/reject_once"
            n = len(gw.requests)
            with pytest.raises(KiroCrewError):
                await mc.resolve_approval("r1", "trust")
            assert len(gw.requests) == n

    async def test_slot_resolve_passes_action_through(self, gw):
        async with _client(gw) as mc:
            await mc.resolve_approval("r1", "trust_reads", slot_id="s1")
        assert gw.last["path"] == "/api/chat/slots/s1/approve"
        assert gw.last["body"] == {"action": "trust_reads", "request_id": "r1"}

    @pytest.mark.parametrize(
        ("action", "sent"),
        [
            (None, "approved"),
            ("approve", "approved"),
            ("approved", "approved"),
            ("reject", "rejected"),
            ("rejected", "rejected"),
        ],
    )
    async def test_slot_resolve_sends_the_slot_route_spelling(self, gw, action, sent):
        # The slot route turns any unrecognised action into a denial, so the
        # default "approve" must go over the wire as "approved".
        async with _client(gw) as mc:
            if action is None:
                await mc.resolve_approval("r1", slot_id="s1")
            else:
                await mc.resolve_approval("r1", action, slot_id="s1")
        assert gw.last["path"] == "/api/chat/slots/s1/approve"
        assert gw.last["body"] == {"action": sent, "request_id": "r1"}

    @pytest.mark.parametrize(
        ("action", "pattern"),
        [
            ("trust_command", "python -m pytest"),
            ("trust_base", "python *"),
        ],
    )
    async def test_slot_trust_sends_server_validation_pattern(self, gw, action, pattern):
        async with _client(gw) as mc:
            await mc.resolve_approval("r1", action, slot_id="s1", pattern=pattern)
        assert gw.last["path"] == "/api/chat/slots/s1/approve"
        assert gw.last["body"] == {
            "action": action,
            "request_id": "r1",
            "pattern": pattern,
        }

    @pytest.mark.parametrize("action", ["trust_command", "trust_base"])
    async def test_slot_trust_requires_pattern_before_request(self, gw, action):
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.resolve_approval("r1", action, slot_id="s1")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert gw.requests == []

    async def test_slot_resolve_rejects_unknown_action_before_request(self, gw):
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.resolve_approval("r1", "typo", slot_id="s1")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert gw.requests == []

    async def test_set_slot_scoped_approval_mode(self, gw):
        async with _client(gw) as mc:
            await mc.set_approval_mode("trust_reads", slot_id="s1")
        assert (gw.last["path"], gw.last["body"]) == (
            "/api/chat/mode",
            {"mode": "trust_reads", "slot": "s1"},
        )

    async def test_normal_mode_accepts_slot_scope(self, gw):
        async with _client(gw) as mc:
            await mc.set_approval_mode("normal", slot_id="s1")
        assert (gw.last["path"], gw.last["body"]) == (
            "/api/chat/mode",
            {"mode": "normal", "slot": "s1"},
        )

    async def test_yolo_mode_rejects_slot_scope_before_request(self, gw):
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.set_approval_mode("yolo", slot_id="s1")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert gw.requests == []

    async def test_set_approval_mode_rejects_unknown_mode_before_request(self, gw):
        async with _client(gw) as mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.set_approval_mode("typo", slot_id="s1")
        assert exc.value.code == ErrorCode.VALIDATION_ERROR
        assert gw.requests == []


class TestModelsConfigStt:
    async def test_list_models_accepts_bare_or_wrapped(self, gw):
        async with _client(gw) as mc:
            gw.responses[("GET", "/api/models")] = [{"model_id": "auto"}]
            assert await mc.list_models() == [{"model_id": "auto"}]
            gw.responses[("GET", "/api/models")] = {"models": [{"model_id": "x"}]}
            assert await mc.list_models() == [{"model_id": "x"}]

    async def test_config_key_is_validated_before_any_request(self, gw):
        async with _client(gw) as mc:
            await mc.get_gateway_config("stt")
            assert gw.last["path"] == "/api/config/stt"
            with pytest.raises(KiroCrewError):
                await mc.set_gateway_config("../settings", {})
        assert len(gw.requests) == 1
        assert "default-agent" in GATEWAY_CONFIG_KEYS

    async def test_transcribe_sends_the_audio_field(self, gw):
        gw.responses[("POST", "/api/stt/transcribe")] = {"text": "hello"}
        async with _client(gw, token="tok") as mc:
            assert (
                await mc.transcribe(b"RIFF", filename="a.wav", content_type="audio/wav") == "hello"
            )
        assert gw.last["body"] == {"field": "audio", "filename": "a.wav", "bytes": b"RIFF"}
        assert gw.last["cookie"].endswith("=tok")

    async def test_transcribe_connection_failure_is_a_network_error(self, unused_tcp_port):
        mc = KiroCrewClient(
            base_url=f"http://127.0.0.1:{unused_tcp_port}",
            max_retries=0,
        )
        async with mc:
            with pytest.raises(KiroCrewError) as exc:
                await mc.transcribe(b"RIFF")
        assert exc.value.code == ErrorCode.NETWORK_ERROR
        assert isinstance(exc.value.__cause__, aiohttp.ClientError)

    async def test_transcribe_refreshes_once_after_auth_refusal(self, gw):
        gw.statuses = [401]
        gw.responses[("POST", "/api/stt/transcribe")] = {"text": "hello"}
        refreshes = 0

        async def refresh() -> str:
            nonlocal refreshes
            refreshes += 1
            return "fresh"

        async with _client(gw, token="stale", on_auth_expired=refresh) as mc:
            assert await mc.transcribe(b"RIFF") == "hello"

        assert refreshes == 1
        assert [request["cookie"] for request in gw.requests] == [
            f"mc_token_{gw.server.port}=stale",
            f"mc_token_{gw.server.port}=fresh",
        ]


class TestWebSocket:
    def test_slot_title_key_routes_only_to_the_matching_slot(self):
        ws = KiroCrewClient().create_ws()
        events: list[WsEvent] = []
        unrelated: list[WsEvent] = []
        ws.on_slot("s1", "slot_title", events.append)
        ws.on_slot("s1", "member_projection", unrelated.append)

        ws._dispatch({"type": "slot_title", "data": {"key": "s1", "title": "one"}})
        ws._dispatch({"type": "slot_title", "data": {"key": "s2", "title": "two"}})
        ws._dispatch({"type": "member_projection", "data": {"key": "s1"}})

        assert [event.data["title"] for event in events] == ["one"]
        assert unrelated == []

    async def test_dispatches_typed_and_slot_events_with_auth_cookie(self, gw):
        gw.ws_frames = [
            {"type": "chat_chunk", "data": {"slot": "s1", "text": "a"}},
            {"type": "chat_chunk", "data": {"slot": "s2", "text": "b"}},
            {"type": "notification", "data": "plain"},
        ]
        mc = _client(gw, token="tok")
        ws = mc.create_ws(reconnect_base_delay=10)
        typed: list[WsEvent] = []
        slot: list[WsEvent] = []
        states: list[tuple[bool, bool]] = []
        ws.on("chat_chunk", typed.append)
        ws.on_slot("s1", "chat_chunk", slot.append)
        ws.on("notification", typed.append)
        ws.on_connection_change(lambda c, r: states.append((c, r)))
        await ws.connect()
        for _ in range(100):
            if (False, False) in states:
                break
            await asyncio.sleep(0.02)
        await ws.disconnect()
        await mc.close()

        assert [e.data.get("text") for e in typed[:2]] == ["a", "b"]
        assert typed[2].data == {"value": "plain"}
        assert [e.data["text"] for e in slot] == ["a"]
        assert states[:2] == [(True, False), (False, False)]
        assert gw.ws_cookies == [f"mc_token_{gw.server.port}=tok"]
        assert gw.ws_origins == [f"http://127.0.0.1:{gw.server.port}"]

    @pytest.mark.parametrize("origin", [None, "http://wrong.example"])
    async def test_stub_rejects_missing_or_wrong_origin(self, gw, origin):
        assert gw.server is not None
        kwargs = {"origin": origin} if origin is not None else {}
        async with aiohttp.ClientSession() as session:
            with pytest.raises(aiohttp.WSServerHandshakeError) as exc:
                await session.ws_connect(gw.server.make_url("/api/ws"), **kwargs)
        assert exc.value.status == 403

    def test_ws_url_follows_the_base_url_scheme(self):
        assert KiroCrewClient(base_url="https://h.example:8443", token="t").create_ws()._ws_url == (
            "wss://h.example:8443/api/ws"
        )
        assert KiroCrewClient(base_url="http://localhost:5476").create_ws()._ws_url == (
            "ws://localhost:5476/api/ws"
        )

    def test_explicit_origin_overrides_the_client_default(self):
        ws = KiroCrewClient().create_ws(origin="https://x.example")
        assert ws._origin == "https://x.example"

    def test_explicit_header_provider_overrides_the_client_default(self):
        get_headers = lambda: {"X-Test": "caller"}  # noqa: E731
        ws = KiroCrewClient(token="client-token").create_ws(get_headers=get_headers)
        assert ws._get_headers is get_headers
        assert ws._get_headers() == {"X-Test": "caller"}

    def test_raw_message_alias_is_not_exposed(self):
        ws = KiroCrewClient().create_ws()
        assert not hasattr(ws, "on_raw_message")
        assert not hasattr(ws, "_raw_msg_listeners")
