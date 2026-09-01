"""Gateway-backed ACP server: slot mapping, lifecycle, prompt conversion, options.

Covers the pieces that make an editor's ACP session a dashboard chat slot on the
shared path:
- ``prompt_blocks_to_text`` — documented block conversion (no ``[type]`` collapse)
- ``AcpAgentServer`` + ``SessionBackend`` wiring — loadSession cap, session/new
  delegation, session/load, session/cancel bridging
- ``HttpGatewayBackend`` — create/scope, SSE→ACP translation, permission bridge,
  reply-option ``_meta``, list/cancel — exercised against a live aiohttp stub of
  the gateway's ``/api/chat`` surface.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from aiohttp import web

from kiro_crew.acp.types import (
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LIST,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_UPDATE,
    OPTION_ALLOW_ONCE,
    OPTION_REJECT_ONCE,
    OUTCOME_SELECTED,
)
from kiro_crew.acp_server import HttpGatewayBackend, prompt_blocks_to_text
from kiro_crew.acp_server.http_backend import AcpGatewayError
from kiro_crew.acp_server.server import AcpAgentServer, PromptRequest, SessionSink, _Session
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS


class TestSelectorEndpointAuthorization:
    def test_selector_discovery_allows_internal_secret(self) -> None:
        assert "/api/models" in _MIXED_INTERNAL_API_PATHS
        assert "/api/effort-levels" in _MIXED_INTERNAL_API_PATHS


# ── prompt block conversion (R5) ──


class TestPromptBlockConversion:
    def test_text_blocks_concatenate(self) -> None:
        blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
        assert prompt_blocks_to_text(blocks) == "ab"

    def test_inline_image_without_handle_is_dropped(self) -> None:
        # A bare inline-data image has no textual handle a text-only core can use.
        blocks = [
            {"type": "text", "text": "a"},
            {"type": "image", "data": "ignored"},
            {"type": "text", "text": "b"},
        ]
        assert prompt_blocks_to_text(blocks) == "ab"

    def test_resource_link_preserved_as_uri(self) -> None:
        blocks = [{"type": "resource_link", "uri": "file:///x.py"}]
        assert prompt_blocks_to_text(blocks) == "file:///x.py"

    def test_resource_prefers_embedded_text(self) -> None:
        blocks = [{"type": "resource", "resource": {"text": "hi", "uri": "file:///x"}}]
        assert prompt_blocks_to_text(blocks) == "hi"

    def test_resource_falls_back_to_uri(self) -> None:
        blocks = [{"type": "resource", "resource": {"uri": "file:///x"}}]
        assert prompt_blocks_to_text(blocks) == "file:///x"

    def test_image_with_uri_gets_documented_placeholder(self) -> None:
        blocks = [{"type": "image", "uri": "file:///p.png"}]
        assert prompt_blocks_to_text(blocks) == "[image: file:///p.png]"

    def test_unknown_block_uses_text_field_else_dropped(self) -> None:
        blocks = [{"type": "weird", "text": "kept"}, {"type": "weird", "x": 1}]
        assert prompt_blocks_to_text(blocks) == "kept"


# ── SessionBackend wiring (R2/R3/R4) ──


class _FakeTransport:
    """Records agent→client frames; answers permission requests deterministically."""

    def __init__(self, permission: str = OPTION_ALLOW_ONCE) -> None:
        self.results: dict[Any, dict] = {}
        self.errors: dict[Any, tuple[int, str]] = {}
        self.notifications: list[tuple[str, dict]] = []
        self.requests: list[tuple[str, dict]] = []
        self._permission = permission

    async def send_result(self, req_id: Any, result: dict) -> None:
        self.results[req_id] = result

    async def send_error(self, req_id: Any, code: int, message: str) -> None:
        self.errors[req_id] = (code, message)

    async def send_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))

    async def send_request(self, method: str, params: dict, *, timeout: float = 120.0) -> Any:
        self.requests.append((method, params))
        return {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": self._permission}}


class _FakeBackend:
    supports_load = True
    supports_list = True
    supports_resume = True

    def __init__(self) -> None:
        self.created: list[str] = []
        self.loaded: list[tuple[str, str]] = []
        self.listed: list[tuple[str | None, str | None]] = []
        self.resumed: list[tuple[str, str]] = []
        self.cancelled: list[str] = []

    async def create_session(self, cwd: str) -> str:
        self.created.append(cwd)
        return "acp-slot-1"

    async def load_session(self, session_id: str, cwd: str) -> list[dict[str, str]]:
        self.loaded.append((session_id, cwd))
        return [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
        ]

    async def list_sessions(
        self, *, cwd: str | None = None, cursor: str | None = None
    ) -> dict[str, Any]:
        self.listed.append((cwd, cursor))
        return {"sessions": [{"sessionId": "acp-slot-1", "cwd": cwd or ""}]}

    async def resume_session(self, session_id: str, cwd: str) -> None:
        self.resumed.append((session_id, cwd))

    async def cancel(self, session_id: str) -> None:
        self.cancelled.append(session_id)


def _server(backend: Any = None, transport: Any = None) -> tuple[AcpAgentServer, _FakeTransport]:
    tr = transport or _FakeTransport()

    async def _handler(_req: PromptRequest, _sink: SessionSink) -> str:
        return "end_turn"

    return AcpAgentServer(tr, _handler, session_backend=backend), tr  # type: ignore[arg-type]


class TestBackendWiring:
    @pytest.mark.asyncio
    async def test_initialize_advertises_load_with_backend(self) -> None:
        srv, tr = _server(backend=_FakeBackend())
        await srv._handle_initialize({"protocolVersion": 1}, 1)
        capabilities = tr.results[1]["agentCapabilities"]
        assert capabilities["loadSession"] is True
        assert capabilities["sessionCapabilities"] == {"list": {}, "resume": {}}

    @pytest.mark.asyncio
    async def test_initialize_no_load_without_backend(self) -> None:
        srv, tr = _server(backend=None)
        await srv._handle_initialize({"protocolVersion": 1}, 1)
        assert tr.results[1]["agentCapabilities"]["loadSession"] is False
        assert "sessionCapabilities" not in tr.results[1]["agentCapabilities"]

    @pytest.mark.asyncio
    async def test_session_new_delegates_to_backend(self) -> None:
        backend = _FakeBackend()
        srv, tr = _server(backend=backend)
        await srv._handle_session_new({"cwd": "/repo"}, 2)
        assert tr.results[2]["sessionId"] == "acp-slot-1"
        assert backend.created == ["/repo"]
        assert "acp-slot-1" in srv._sessions

    @pytest.mark.asyncio
    async def test_session_new_mints_uuid_without_backend(self) -> None:
        srv, tr = _server(backend=None)
        await srv._handle_session_new({"cwd": "/repo"}, 2)
        assert tr.results[2]["sessionId"].startswith("kirocrew-")

    @pytest.mark.asyncio
    async def test_session_load_rescopes_and_registers(self) -> None:
        backend = _FakeBackend()
        srv, tr = _server(backend=backend)
        await srv._handle_session_load({"sessionId": "acp-slot-9", "cwd": "/w"}, 3)
        assert tr.results[3] == {}
        assert backend.loaded == [("acp-slot-9", "/w")]
        assert "acp-slot-9" in srv._sessions
        updates = [
            params["update"]
            for method, params in tr.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert [update["sessionUpdate"] for update in updates] == [
            "user_message_chunk",
            "agent_message_chunk",
        ]
        assert [update["content"]["text"] for update in updates] == [
            "old question",
            "old answer",
        ]

    @pytest.mark.asyncio
    async def test_cancel_notification_bridges_to_backend(self) -> None:
        backend = _FakeBackend()
        srv, _tr = _server(backend=backend)
        srv._sessions["s1"] = _Session(session_id="s1")
        await srv._on_notification("session/cancel", {"sessionId": "s1"})
        # Cancel is fire-and-forget; let the scheduled task run.
        await asyncio.gather(*srv._cancel_tasks)
        assert backend.cancelled == ["s1"]
        assert srv._sessions["s1"].cancelled.is_set()

    @pytest.mark.asyncio
    async def test_session_list_delegates_to_backend(self) -> None:
        backend = _FakeBackend()
        srv, tr = _server(backend=backend)
        await srv._on_request(METHOD_SESSION_LIST, {"cwd": "/repo", "cursor": "next"}, 4)
        assert backend.listed == [("/repo", "next")]
        assert tr.results[4] == {"sessions": [{"sessionId": "acp-slot-1", "cwd": "/repo"}]}

    @pytest.mark.asyncio
    async def test_session_resume_delegates_and_registers(self) -> None:
        backend = _FakeBackend()
        srv, tr = _server(backend=backend)
        await srv._on_request(
            METHOD_SESSION_RESUME,
            {"sessionId": "acp-slot-9", "cwd": "/repo", "mcpServers": []},
            5,
        )
        assert backend.resumed == [("acp-slot-9", "/repo")]
        assert tr.results[5] == {}
        assert srv._sessions["acp-slot-9"].cwd == "/repo"


# ── HttpGatewayBackend against a live gateway stub (R2/R4/R5/R7) ──


def _make_stub_app() -> web.Application:
    app = web.Application()
    app["slots"] = {}  # name -> dict
    app["projects"] = {}  # slot -> project
    app["approvals"] = []  # (slot, request_id, action)
    app["stops"] = []
    app["approve_events"] = {}

    async def slots_list(_request: web.Request) -> web.Response:
        # GET returns a bare JSON list, matching serialize_slots().
        return web.json_response(list(app["slots"].values()))

    async def slot_create(request: web.Request) -> web.Response:
        body = await request.json()
        name = body["name"]
        app["slots"][name] = {
            "key": name,
            "name": name,
            "title": None,
            "has_options": False,
            "options": [],
            "project": "",
            "last_activity_ts": "2026-08-21T22:00:00+00:00",
        }
        return web.json_response(app["slots"][name])

    async def slot_project(request: web.Request) -> web.Response:
        name = request.match_info["slot"]
        body = await request.json()
        app["projects"][name] = body.get("project", "")
        app["slots"][name]["project"] = body.get("project", "")
        return web.json_response({"ok": True, "project": body.get("project", "")})

    async def slot_resume(request: web.Request) -> web.Response:
        name = request.match_info["slot"]
        app["slots"].setdefault(
            name,
            {
                "key": name,
                "name": name,
                "title": "Old dashboard session",
                "project": "",
                "messages": [
                    {"role": "user", "content": "old question"},
                    {"role": "assistant", "content": "old answer"},
                ],
            },
        )
        return web.json_response({"ok": True, "key": name})

    async def slot_detail(request: web.Request) -> web.Response:
        name = request.match_info["slot"]
        slot = app["slots"].get(name)
        if not slot:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"key": name, "messages": slot.get("messages", [])})

    async def slot_stop(request: web.Request) -> web.Response:
        app["stops"].append(request.match_info["slot"])
        return web.json_response({"ok": True})

    async def slot_approve(request: web.Request) -> web.Response:
        name = request.match_info["slot"]
        body = await request.json()
        app["approvals"].append((name, body.get("request_id"), body.get("action")))
        app["approve_events"].setdefault(name, asyncio.Event()).set()
        return web.json_response({"ok": True})

    async def chat(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        assert "agent" not in body
        slot = body["slot"]
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)

        async def frame(obj: dict) -> None:
            await resp.write(f"data: {json.dumps(obj)}\n\n".encode())

        await frame({"type": "chunk", "content": "Hello ", "cls": ""})
        await frame({"type": "chunk", "content": "thinking…", "cls": "thinking"})
        await frame({"type": "assistant", "content": "Hello world"})  # dropped dup
        await frame(
            {
                "type": "permission",
                "content": "run ls",
                "meta": {
                    "request_id": "r1",
                    "tool_call_id": "t1",
                    "tool_title": "ls",
                    "tool_input": '{"command":"ls"}',
                },
            }
        )
        ev = app["approve_events"].setdefault(slot, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass
        # After approval, the turn produced options.
        app["slots"][slot]["has_options"] = True
        app["slots"][slot]["options"] = ["Yes", "No"]
        await frame({"type": "chunk", "content": "\n[OPTIONS: Yes | No]", "cls": ""})
        await resp.write(b"data: [DONE]\n\n")
        return resp

    app.router.add_get("/api/chat/slots", slots_list)
    app.router.add_post("/api/chat/slots", slot_create)
    app.router.add_post("/api/chat/slots/{slot}/project", slot_project)
    app.router.add_post("/api/chat/slots/{slot}/resume", slot_resume)
    app.router.add_get("/api/chat/slots/{slot}", slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/stop", slot_stop)
    app.router.add_post("/api/chat/slots/{slot}/approve", slot_approve)
    app.router.add_post("/api/chat", chat)
    return app


async def _start_stub() -> tuple[web.AppRunner, str, web.Application]:
    app = _make_stub_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = list(runner.addresses)[0][1] if hasattr(runner, "addresses") else None
    # runner.addresses may be empty on some versions; read from the site's server.
    if not port:
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[attr-defined]
    return runner, f"http://127.0.0.1:{port}", app


class _RecordingSink(SessionSink):
    """A SessionSink over a fake transport, for asserting translated updates."""

    def __init__(self, permission: str = OPTION_ALLOW_ONCE) -> None:
        self.transport = _FakeTransport(permission)
        super().__init__(self.transport, _Session(session_id="acp-slot-1"))  # type: ignore[arg-type]


class TestGatewayCredentialBoundary:
    @pytest.mark.asyncio
    async def test_remote_gateway_requires_explicit_presigned_token(self, tmp_path) -> None:
        secret = tmp_path / ".local_secret"
        secret.write_text("local-only-secret", encoding="utf-8")
        backend = HttpGatewayBackend("https://gateway.example", secret_path=str(secret))
        with pytest.raises(AcpGatewayError, match="presigned token"):
            await backend.open()
        assert backend._secret == ""
        assert backend._session is None


@pytest.mark.asyncio
class TestHttpGatewayBackend:
    async def test_create_session_scopes_project(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            sid = await backend.create_session("/repo/x")
            assert sid.startswith("acp-")
            assert sid in app["slots"]
            assert app["projects"][sid] == "/repo/x"
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_load_dashboard_session_activates_and_returns_history(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            messages = await backend.load_session("dashboard-old", "/repo/x")
            assert app["projects"]["dashboard-old"] == "/repo/x"
            assert messages == [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_prompt_streams_translates_and_bridges_permission(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            sid = await backend.create_session("")
            sink = _RecordingSink()
            sink._session.session_id = sid  # match created slot
            stop = await backend._run_prompt(PromptRequest(session_id=sid, text="hi"), sink)
            assert stop == "end_turn"

            updates = [
                p["update"] for (m, p) in sink.transport.notifications if m == METHOD_SESSION_UPDATE
            ]
            texts = [
                u["content"]["text"]
                for u in updates
                if u.get("sessionUpdate") == "agent_message_chunk" and u["content"].get("text")
            ]
            thoughts = [
                u["content"]["text"]
                for u in updates
                if u.get("sessionUpdate") == "agent_thought_chunk"
            ]
            assert "Hello " in texts
            assert any("thinking" in t for t in thoughts)
            # The consolidated `assistant` frame must NOT be re-emitted.
            assert "Hello world" not in texts

            # Permission surfaced to the editor and answered to the gateway.
            assert any(m == METHOD_REQUEST_PERMISSION for (m, _p) in sink.transport.requests)
            assert app["approvals"] == [(sid, "r1", "approved")]

            # Reply options carried as namespaced _meta, marker still in text.
            meta_updates = [u for u in updates if u.get("_meta")]
            assert meta_updates, "expected an options _meta update"
            assert meta_updates[-1]["_meta"]["kirocrew"]["options"] == ["Yes", "No"]
            assert any("[OPTIONS: Yes | No]" in t for t in texts)
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_tool_frames_receive_monotonic_ids(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate({"type": "tool", "content": "first"}, "s", sink)
        await backend._translate({"type": "tool", "content": "second"}, "s", sink)
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
            and params["update"].get("sessionUpdate") == "tool_call"
        ]
        assert [update["toolCallId"] for update in updates] == ["gw-1", "gw-2"]

    async def test_tool_frame_forwards_locations_from_sse(self) -> None:
        # Zed follow-along: a well-formed ``locations`` array on the SSE tool
        # chunk must reach the ACP wire on ``session/update``.
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "tool",
                "content": "edit main.py",
                "locations": [{"path": "/abs/main.py", "line": 7}],
            },
            "s",
            sink,
        )
        update = next(
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
            and params["update"].get("sessionUpdate") == "tool_call"
        )
        assert update["locations"] == [{"path": "/abs/main.py", "line": 7}]

    async def test_tool_frame_without_locations_omits_the_key(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate({"type": "tool", "content": "run tests"}, "s", sink)
        update = next(
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
            and params["update"].get("sessionUpdate") == "tool_call"
        )
        assert "locations" not in update

    async def test_tool_frame_drops_malformed_location_entries(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "tool",
                "content": "edit",
                "locations": [
                    {"path": ""},  # empty
                    {"path": 5},  # non-string
                    "junk",  # not a dict
                    {"nope": "/a"},  # no path
                    {"path": "/ok", "line": -1},  # bad line dropped
                ],
            },
            "s",
            sink,
        )
        update = next(
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
            and params["update"].get("sessionUpdate") == "tool_call"
        )
        assert update["locations"] == [{"path": "/ok"}]

    async def test_tool_update_refreshes_locations_on_same_call_id(self) -> None:
        # Streamed refinement: kiro-cli's Read tool emits an empty tool_call
        # then a tool_call_update carrying path/start_line. The refinement
        # must reach Zed as session/update tool_call_update against the SAME
        # gw-N id so the follow-along jumps to the right line.
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {"type": "tool", "content": "read", "tool_call_id": "toolu_abc"},
            "s",
            sink,
        )
        await backend._translate(
            {
                "type": "tool_update",
                "tool_call_id": "toolu_abc",
                "locations": [{"path": "/abs/main.py", "line": 42}],
            },
            "s",
            sink,
        )
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert any(
            u.get("sessionUpdate") == "tool_call_update"
            and u.get("toolCallId") == "gw-1"
            and u.get("locations") == [{"path": "/abs/main.py", "line": 42}]
            for u in updates
        ), updates

    async def test_tool_update_unknown_call_id_is_dropped(self) -> None:
        # A stray refinement (e.g. gateway restarted mid-turn) has no gw-N
        # to correlate against; landing it on the wrong tool card would
        # silently move Zed's cursor to an unrelated file. Drop it.
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "tool_update",
                "tool_call_id": "toolu_never_seen",
                "locations": [{"path": "/abs/x.py", "line": 1}],
            },
            "s",
            sink,
        )
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert not any(u.get("sessionUpdate") == "tool_call_update" for u in updates), updates

    async def test_tool_update_missing_call_id_is_dropped(self) -> None:
        # A tool_update chunk with no tool_call_id cannot address any prior
        # tool call, so drop it rather than misroute the follow-along.
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {"type": "tool_update", "locations": [{"path": "/abs/x.py", "line": 1}]},
            "s",
            sink,
        )
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert not any(u.get("sessionUpdate") == "tool_call_update" for u in updates), updates

    async def test_permission_rejection_answers_rejected(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            sid = await backend.create_session("")
            sink = _RecordingSink(permission=OPTION_REJECT_ONCE)
            sink._session.session_id = sid
            await backend._run_prompt(PromptRequest(session_id=sid, text="hi"), sink)
            assert app["approvals"] == [(sid, "r1", "rejected")]
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_prompt_stream_has_no_total_timeout(self) -> None:
        class Response:
            status = 200

            def __init__(self) -> None:
                self.content = self._content()

            async def _content(self):
                if False:
                    yield b""

            async def __aenter__(self) -> "Response":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        class HttpSession:
            def __init__(self) -> None:
                self.timeout: Any = None

            async def post(self, *_args: Any, **kwargs: Any) -> Response:
                self.timeout = kwargs["timeout"]
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        session = HttpSession()
        backend._session = session

        stop = await backend._run_prompt(PromptRequest(session_id="s", text="hi"), _RecordingSink())
        assert stop == "end_turn"
        assert session.timeout.total is None
        assert session.timeout.sock_connect == 10.0

    async def test_prompt_stream_timeout_is_visible_to_client(self) -> None:
        class TimedOutContent:
            def __aiter__(self) -> "TimedOutContent":
                return self

            async def __anext__(self) -> bytes:
                raise asyncio.TimeoutError

        class Response:
            status = 200
            content = TimedOutContent()

            async def __aenter__(self) -> "Response":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        class HttpSession:
            async def post(self, *_args: Any, **_kwargs: Any) -> Response:
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        backend._session = HttpSession()
        sink = _RecordingSink()

        stop = await backend._run_prompt(PromptRequest(session_id="s", text="hi"), sink)
        assert stop == "error"
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert updates[-1]["content"]["text"].startswith("\n\n**Error:** gateway stream failed:")

    async def test_list_sessions(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            a = await backend.create_session("")
            b = await backend.create_session("")
            app["slots"][a]["last_activity_ts"] = "2026-08-21T21:00:00+00:00"
            app["slots"][b]["last_activity_ts"] = "2026-08-21T23:00:00+00:00"
            result = await backend.list_sessions()
            ids = [s["sessionId"] for s in result["sessions"]]
            assert ids[:2] == [b, a]
            assert {a, b} <= set(ids)
            assert all("cwd" in s for s in result["sessions"])
            assert all("updatedAt" in s for s in result["sessions"])
            filtered = await backend.list_sessions(cwd="/missing")
            assert filtered == {"sessions": []}
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_list_sessions_matches_symlink_equivalent_project(self, tmp_path) -> None:
        real_project = tmp_path / "project"
        real_project.mkdir()
        project_alias = tmp_path / "project-alias"
        try:
            project_alias.symlink_to(real_project, target_is_directory=True)
        except (NotImplementedError, OSError):
            pytest.skip("directory symlinks are unavailable on this platform")

        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            matching = await backend.create_session("")
            unrelated = await backend.create_session("")
            app["slots"][matching]["project"] = str(real_project)
            app["slots"][unrelated]["project"] = str(tmp_path / "other-project")

            result = await backend.list_sessions(cwd=str(project_alias))

            assert [session["sessionId"] for session in result["sessions"]] == [matching]
            assert result["sessions"][0]["cwd"] == str(real_project)
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_cancel_calls_stop(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            await backend.cancel("acp-x")
            assert "acp-x" in app["stops"]
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_supports_load_true(self) -> None:
        assert HttpGatewayBackend("http://127.0.0.1:1").supports_load is True


# ── client-supplied MCP hosting (supervisor wiring) ──

# A minimal stdio MCP server: answers initialize, then stays alive until stdin
# closes. Kept local to this file so the http_backend suite is self-contained.
_GOOD_MCP = r"""
import sys, json
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    if msg.get("method") == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"), "result": {
            "protocolVersion": "2024-11-05", "capabilities": {}}}) + "\n")
        sys.stdout.flush()
"""


class TestHttpBackendMcpPreflight:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        import os

        def _passthrough(
            argv,
            mode="standard",
            *,
            env=None,
            strip_python_env=False,
            extra_hidden_dirs=(),
        ):
            return list(argv), dict(env or os.environ), None

        async def _create_subprocess(*argv, **kwargs):
            return await asyncio.create_subprocess_exec(*argv, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _passthrough
        )
        monkeypatch.setattr(
            "kiro_crew.acp_server.mcp_supervisor.create_subprocess_limited",
            _create_subprocess,
        )

    @pytest.mark.asyncio
    async def test_configure_hosts_child_and_posts_proxy_spec(self) -> None:
        import json
        import sys

        from kiro_crew.acp_server.mcp_config import StdioMcpServer

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        server = StdioMcpServer(name="good", command=sys.executable, args=["-c", _GOOD_MCP])
        try:
            await backend.configure_session_mcp("acp-x", [server])
            # The adapter HOSTS the real, long-lived child under the sandbox and
            # keeps it owned by this session — it does NOT merely preflight and
            # discard (H1/F1 finding: client MCP servers must run under
            # Kiro Crew's controls, not be spawned unsupervised by the provider).
            assert backend._mcp.hosted("acp-x") == ["good"]
            assert "acp-x" in backend._mcp_sessions
            # Exactly one registration, scoped to the slot.
            assert len(posted) == 1
            path, body = posted[0]
            assert path == "/api/chat/slots/acp-x/mcp"
            # What is registered is the TRUSTED PROXY spec, never the untrusted
            # client command/args: the proxy runs kiro_crew.acp_server.mcp_proxy
            # against a per-server socket, so kiro-cli never sees the real
            # command/env.
            assert len(body["servers"]) == 1
            spec = body["servers"][0]
            assert spec["name"] == "good"
            assert spec["command"] == sys.executable
            assert "--socket" in spec["args"]
            assert any(a.endswith("mcp_proxy.py") for a in spec["args"])
            # The untrusted client command text NEVER crosses to the gateway.
            assert _GOOD_MCP not in json.dumps(body)
        finally:
            await backend.close()
        # close() reaped the adapter-owned child and cleared its ownership.
        assert backend._mcp.hosted("acp-x") == []

    @pytest.mark.asyncio
    async def test_empty_set_still_posts_to_clear_the_slot(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        await backend.configure_session_mcp("acp-z", [])
        # An empty registration clears any prior config on the slot (replacement).
        assert posted == [("/api/chat/slots/acp-z/mcp", {"servers": []})]
        await backend.close()

    @pytest.mark.asyncio
    async def test_configure_spawn_failure_propagates_before_post(self) -> None:
        from kiro_crew.acp_server.mcp_config import StdioMcpServer
        from kiro_crew.acp_server.mcp_supervisor import McpSpawnError

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        with pytest.raises(McpSpawnError):
            await backend.configure_session_mcp(
                "acp-y", [StdioMcpServer(name="nope", command="/no/such/binary-xyz")]
            )
        assert backend._mcp.hosted("acp-y") == []
        # A failed preflight must NOT register a config the model could never run.
        assert posted == []
        await backend.close()


# ── selector builders + hooks (model / reasoning effort) ──

from kiro_crew.acp.types import CONFIG_OPTION_MODEL, SESSION_MODE_DEFAULT_ID  # noqa: E402
from kiro_crew.acp_server.http_backend import (  # noqa: E402
    build_mode_state,
    build_model_config_option,
)
from kiro_crew.acp_server.server import SelectorBusyError, SelectorState  # noqa: E402

_MODELS = [
    {"model_name": "sonnet-4.6-1m", "display_name": "Sonnet 4.6", "description": "default"},
    {"model_name": "opus-4.8", "display_name": "Opus 4.8", "description": "capable"},
]
_LEVELS = ["low", "medium", "high", "xhigh", "max"]


class TestSelectorBuilders:
    def test_mode_state_default_when_no_effort(self) -> None:
        state = build_mode_state("", _LEVELS)
        assert state is not None
        assert state["currentModeId"] == SESSION_MODE_DEFAULT_ID
        ids = [m["id"] for m in state["availableModes"]]
        assert ids[0] == SESSION_MODE_DEFAULT_ID
        assert ids[1:] == _LEVELS  # levels advertised verbatim, in order

    def test_mode_state_current_level(self) -> None:
        state = build_mode_state("high", _LEVELS)
        assert state is not None and state["currentModeId"] == "high"

    def test_mode_state_unknown_current_falls_back_to_default(self) -> None:
        # A persisted effort no longer offered maps to the default id (kept in
        # availableModes), so currentModeId is always resolvable.
        state = build_mode_state("ludicrous", _LEVELS)
        assert state is not None and state["currentModeId"] == SESSION_MODE_DEFAULT_ID

    def test_mode_state_no_levels_returns_none(self) -> None:
        assert build_mode_state("", []) is None
        assert build_mode_state("", ["", " "][:1]) is None  # blank filtered out

    def test_model_option_select_shape(self) -> None:
        opt = build_model_config_option("", _MODELS)
        assert opt is not None
        assert opt["id"] == CONFIG_OPTION_MODEL
        assert opt["category"] == "model"
        assert opt["type"] == "select"
        # "" (auto/default) resolves to the default-first option.
        assert opt["currentValue"] == "sonnet-4.6-1m"
        assert [o["value"] for o in opt["options"]] == ["sonnet-4.6-1m", "opus-4.8"]
        assert opt["options"][0]["name"] == "Sonnet 4.6"

    def test_model_option_current_when_set(self) -> None:
        opt = build_model_config_option("opus-4.8", _MODELS)
        assert opt is not None and opt["currentValue"] == "opus-4.8"

    def test_model_option_unknown_current_falls_back_to_first(self) -> None:
        opt = build_model_config_option("gpt-9", _MODELS)
        assert opt is not None and opt["currentValue"] == "sonnet-4.6-1m"

    def test_model_option_no_models_returns_none(self) -> None:
        assert build_model_config_option("", []) is None

    def test_model_option_dedups_by_value(self) -> None:
        dupe = _MODELS + [{"model_name": "opus-4.8", "display_name": "dup"}]
        opt = build_model_config_option("", dupe)
        assert opt is not None
        assert [o["value"] for o in opt["options"]] == ["sonnet-4.6-1m", "opus-4.8"]


class _StubHttp:
    """Stubs HttpGatewayBackend's _get_json/_post_json to avoid real HTTP."""

    def __init__(
        self,
        *,
        detail: Any,
        models: Any,
        levels: Any,
        commands: Any = None,
        fail_marker: str | None = None,
    ) -> None:
        self.detail = detail
        self.models = models
        self.levels = levels
        self.commands = commands
        self.fail_marker = fail_marker
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def get_json(self, path: str, *, allow_fail: bool = False) -> Any:
        if path.startswith("/api/models"):
            return self.models
        if path.startswith("/api/effort-levels"):
            return self.levels
        if path == "/api/slash-commands":
            return self.commands
        if path == "/api/chat/slots":
            return [{"key": "acp-1", **self.detail}]
        if path.startswith("/api/chat/slots/"):
            return {"key": "acp-1", "messages": []}
        return None

    async def post_json(
        self, path: str, body: dict[str, Any], *, allow_fail: bool = False
    ) -> dict[str, Any] | None:
        self.posts.append((path, body))
        if self.fail_marker and self.fail_marker in path:
            raise AcpGatewayError("simulated gateway failure")
        return {"ok": True}


def _backend_with(stub: _StubHttp) -> HttpGatewayBackend:
    backend = HttpGatewayBackend("http://127.0.0.1:1")
    backend._get_json = stub.get_json  # type: ignore[assignment]
    backend._post_json = stub.post_json  # type: ignore[assignment]
    return backend


class TestAvailableCommandHook:
    @pytest.mark.asyncio
    async def test_get_available_commands_uses_gateway_catalog(self) -> None:
        commands = [
            {"name": "/help", "description": "Show available commands"},
            {"name": "/model", "description": "Switch the current model"},
        ]
        stub = _StubHttp(detail={}, models=[], levels=[], commands=commands)

        result = await _backend_with(stub).get_available_commands("acp-1")

        assert result == commands

    @pytest.mark.asyncio
    async def test_get_available_commands_degrades_to_unavailable(self) -> None:
        stub = _StubHttp(detail={}, models=[], levels=[], commands=None)

        result = await _backend_with(stub).get_available_commands("acp-1")

        assert result is None


class TestSelectorHooks:
    @pytest.mark.asyncio
    async def test_get_selectors_composes_defaults(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": ""}, models=_MODELS, levels=_LEVELS
        )
        state = await _backend_with(stub).get_session_selectors("acp-1")
        assert isinstance(state, SelectorState)
        assert state.modes is not None and state.modes["currentModeId"] == SESSION_MODE_DEFAULT_ID
        assert state.config_options is not None
        assert state.config_options[0]["currentValue"] == "sonnet-4.6-1m"

    @pytest.mark.asyncio
    async def test_get_selectors_reflects_current(self) -> None:
        stub = _StubHttp(
            detail={"model": "opus-4.8", "reasoning_effort": "high"},
            models=_MODELS,
            levels=_LEVELS,
        )
        state = await _backend_with(stub).get_session_selectors("acp-1")
        assert state.modes is not None and state.modes["currentModeId"] == "high"
        assert state.config_options is not None
        assert state.config_options[0]["currentValue"] == "opus-4.8"

    @pytest.mark.asyncio
    async def test_get_selectors_best_effort_when_degraded(self) -> None:
        # Degraded /api/models (503 -> None) and no levels advertise nothing,
        # rather than failing the lifecycle call.
        stub = _StubHttp(detail={"model": "", "reasoning_effort": ""}, models=None, levels=[])
        state = await _backend_with(stub).get_session_selectors("acp-1")
        assert state.modes is None
        assert state.config_options is None

    @pytest.mark.asyncio
    async def test_set_mode_posts_effort(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": "high"}, models=_MODELS, levels=_LEVELS
        )
        backend = _backend_with(stub)
        await backend.set_session_mode("acp-1", "high")
        assert stub.posts[0][0].endswith("/reasoning-effort")
        assert stub.posts[0][1] == {"reasoning_effort": "high"}

    @pytest.mark.asyncio
    async def test_set_mode_default_posts_empty(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": ""}, models=_MODELS, levels=_LEVELS
        )
        backend = _backend_with(stub)
        await backend.set_session_mode("acp-1", SESSION_MODE_DEFAULT_ID)
        assert stub.posts[0][1] == {"reasoning_effort": ""}

    @pytest.mark.asyncio
    async def test_set_config_option_posts_model(self) -> None:
        stub = _StubHttp(
            detail={"model": "opus-4.8", "reasoning_effort": ""}, models=_MODELS, levels=_LEVELS
        )
        backend = _backend_with(stub)
        state = await backend.set_session_config_option("acp-1", CONFIG_OPTION_MODEL, "opus-4.8")
        assert stub.posts[0][0].endswith("/model")
        assert stub.posts[0][1] == {"model": "opus-4.8"}
        assert isinstance(state, SelectorState)

    @pytest.mark.asyncio
    async def test_set_config_option_unknown_id_raises(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": ""}, models=_MODELS, levels=_LEVELS
        )
        backend = _backend_with(stub)
        with pytest.raises(AcpGatewayError):
            await backend.set_session_config_option("acp-1", "temperature", "hot")
        assert stub.posts == []  # never POSTed an unsupported option

    @pytest.mark.asyncio
    async def test_set_mode_post_failure_raises(self) -> None:
        # A gateway 5xx / transport error propagates as AcpGatewayError, which the
        # server maps to -32603 (rollback: nothing announced).
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": ""},
            models=_MODELS,
            levels=_LEVELS,
            fail_marker="/reasoning-effort",
        )
        backend = _backend_with(stub)
        with pytest.raises(AcpGatewayError):
            await backend.set_session_mode("acp-1", "high")

    @pytest.mark.asyncio
    async def test_set_config_option_post_failure_raises(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": ""},
            models=_MODELS,
            levels=_LEVELS,
            fail_marker="/model",
        )
        backend = _backend_with(stub)
        with pytest.raises(AcpGatewayError):
            await backend.set_session_config_option("acp-1", CONFIG_OPTION_MODEL, "opus-4.8")

    @pytest.mark.asyncio
    async def test_set_mode_rejects_running_slot(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": "", "running": True},
            models=_MODELS,
            levels=_LEVELS,
        )
        with pytest.raises(SelectorBusyError):
            await _backend_with(stub).set_session_mode("acp-1", "high")
        assert stub.posts == []

    @pytest.mark.asyncio
    async def test_set_config_option_rejects_running_slot(self) -> None:
        stub = _StubHttp(
            detail={"model": "", "reasoning_effort": "", "running": True},
            models=_MODELS,
            levels=_LEVELS,
        )
        with pytest.raises(SelectorBusyError):
            await _backend_with(stub).set_session_config_option(
                "acp-1", CONFIG_OPTION_MODEL, "opus-4.8"
            )
        assert stub.posts == []
