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
import threading
from typing import Any

import aiohttp
import pytest
from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.acp.types import (
    JSONRPC_INTERNAL_ERROR,
    METHOD_REQUEST_PERMISSION,
    METHOD_SESSION_LIST,
    METHOD_SESSION_RESUME,
    METHOD_SESSION_UPDATE,
    OPTION_ALLOW_ONCE,
    OPTION_REJECT_ONCE,
    OUTCOME_SELECTED,
    STOP_REASON_END_TURN,
)
from kiro_crew.acp_server import HttpGatewayBackend
from kiro_crew.acp_server import http_backend as http_backend_module
from kiro_crew.acp_server import prompt_blocks_to_text
from kiro_crew.acp_server.cleanup_receipts import CleanupReceiptStore
from kiro_crew.acp_server.http_backend import AcpGatewayError
from kiro_crew.acp_server.mcp_config import StdioMcpServer
from kiro_crew.acp_server.server import (
    AcpAgentServer,
    PromptRequest,
    SessionSink,
    _Session,
    _validate_prompt_blocks,
)
from kiro_crew.config.paths import ACP_CLEANUP_RECEIPTS_DIR_NAME
from kiro_crew.dashboard.server import _MIXED_INTERNAL_API_PATHS
from kiro_crew.dashboard.token_auth import generate_token, token_auth_middleware
from kiro_crew.security import is_sensitive_path


class TestCleanupReceipts:
    def test_store_round_trip_and_security_fence(self, tmp_path, monkeypatch) -> None:
        root = tmp_path / ACP_CLEANUP_RECEIPTS_DIR_NAME
        store = CleanupReceiptStore(root)
        receipt = store.add_mcp_clear("http://127.0.0.1:5476", "acp-s", "owner-a")

        assert receipt.owner_pid > 0
        assert store.mcp_owner_is_live(receipt) is True
        assert store.pending() == [receipt]
        store.discard(receipt)
        assert store.pending() == []

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "crew-home"))
        assert is_sensitive_path(str(tmp_path / "crew-home" / ACP_CLEANUP_RECEIPTS_DIR_NAME))

    def test_mcp_owner_liveness_uses_exact_identity(self, tmp_path, monkeypatch) -> None:
        store = CleanupReceiptStore(tmp_path / "receipts")
        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "start-a")
        receipt = store.add_mcp_clear("http://127.0.0.1:5476", "acp-s", "owner-a")

        assert receipt.owner_started == "start-a"

        monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: True)
        assert store.mcp_owner_is_live(receipt) is True

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "start-b")
        assert store.mcp_owner_is_live(receipt) is False

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: None)
        assert store.mcp_owner_is_live(receipt) is True

        monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: False)
        assert store.mcp_owner_is_live(receipt) is False

        monkeypatch.setattr(platform_compat, "get_process_start_id", lambda _pid: "start-a")
        assert store.mcp_owner_is_live(receipt) is False

    @pytest.mark.asyncio
    async def test_failed_mcp_clear_replays_with_original_owner(self, tmp_path) -> None:
        store = CleanupReceiptStore(tmp_path / "receipts")
        first = HttpGatewayBackend("http://127.0.0.1:5476")
        first._cleanup_receipts = store

        async def fail(*_args: Any, **_kwargs: Any) -> None:
            raise AcpGatewayError("gateway unavailable")

        first._post_json_mutation_with_id = fail  # type: ignore[method-assign]
        with pytest.raises(AcpGatewayError, match="gateway unavailable"):
            await first._clear_slot_mcp("acp-s")

        [pending] = store.pending()
        assert pending.owner == first._mcp_owner
        other_gateway = store.add_mcp_clear("http://127.0.0.1:9999", "acp-other", "owner-other")

        replayed: list[tuple[str, dict[str, Any], str]] = []
        second = HttpGatewayBackend("http://127.0.0.1:5476")
        second._cleanup_receipts = store

        async def apply(path: str, body: dict[str, Any], mutation_id: str) -> None:
            replayed.append((path, body, mutation_id))

        store.mcp_owner_is_live = lambda _receipt: False  # type: ignore[method-assign]
        second._post_json_mutation_with_id = apply  # type: ignore[method-assign]
        await second._retry_cleanup_receipts()

        assert replayed == [
            (
                "/api/chat/slots/acp-s/mcp",
                {"servers": [], "owner": first._mcp_owner, "mode": "clear_if_owner"},
                pending.mutation_id,
            )
        ]
        assert store.pending() == [other_gateway]
        store.discard(other_gateway)

    @pytest.mark.asyncio
    async def test_mcp_registration_receipt_waits_for_owner_exit_before_replay(
        self, tmp_path
    ) -> None:
        store = CleanupReceiptStore(tmp_path / "receipts")
        first = HttpGatewayBackend("http://127.0.0.1:5476")
        first._cleanup_receipts = store
        source = StdioMcpServer(name="source", command="/source")
        proxy = StdioMcpServer(name="proxy", command="/proxy")

        async def host(*_args: Any, **_kwargs: Any) -> list[StdioMcpServer]:
            return [proxy]

        async def replace(_session_id: str, _servers: list[dict[str, Any]]) -> None:
            [pending] = store.pending()
            assert pending.owner == first._mcp_owner

        first._mcp.host = host  # type: ignore[method-assign]
        first._replace_slot_mcp = replace  # type: ignore[method-assign]

        await first.configure_session_mcp("acp-s", str(tmp_path), [source])

        [pending] = store.pending()
        replayed: list[tuple[str, dict[str, Any], str]] = []
        second = HttpGatewayBackend("http://127.0.0.1:5476")
        second._cleanup_receipts = store

        async def apply(path: str, body: dict[str, Any], mutation_id: str) -> None:
            replayed.append((path, body, mutation_id))

        second._post_json_mutation_with_id = apply  # type: ignore[method-assign]
        await second._retry_cleanup_receipts()

        assert replayed == []
        assert store.pending() == [pending]

        store.mcp_owner_is_live = lambda _receipt: False  # type: ignore[method-assign]
        await second._retry_cleanup_receipts()

        assert replayed == [
            (
                "/api/chat/slots/acp-s/mcp",
                {"servers": [], "owner": first._mcp_owner, "mode": "clear_if_owner"},
                pending.mutation_id,
            )
        ]
        assert store.pending() == []

    @pytest.mark.asyncio
    async def test_failed_slot_delete_retains_project_for_replay(self, tmp_path) -> None:
        store = CleanupReceiptStore(tmp_path / "receipts")
        first = HttpGatewayBackend("http://127.0.0.1:5476")
        first._cleanup_receipts = store
        first._created_project_fingerprints["acp-s"] = "a" * 64
        first._session = object()

        async def fail(_receipt) -> None:
            raise AcpGatewayError("delete unavailable")

        first._perform_cleanup_receipt = fail  # type: ignore[method-assign]
        await first.delete_session("acp-s")

        [pending] = store.pending()
        assert pending.project_fingerprint == "a" * 64

        replayed = []
        second = HttpGatewayBackend("http://127.0.0.1:5476")
        second._cleanup_receipts = store

        async def apply(receipt) -> None:
            replayed.append(receipt)

        second._perform_cleanup_receipt = apply  # type: ignore[method-assign]
        await second._retry_cleanup_receipts()

        assert replayed == [pending]
        assert store.pending() == []

    @pytest.mark.asyncio
    async def test_slot_delete_replay_sends_atomic_guards(self, tmp_path) -> None:
        store = CleanupReceiptStore(tmp_path / "receipts")
        receipt = store.add_slot_delete(
            "http://127.0.0.1:5476",
            "acp-s",
            project_fingerprint="a" * 64,
        )
        captured: dict[str, Any] = {}

        class Response:
            status = 200

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        class Session:
            def delete(self, url: str, **kwargs: Any) -> Response:
                captured.update(url=url, **kwargs)
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:5476")
        backend._session = Session()

        async def refresh() -> None:
            return None

        backend._refresh_secret = refresh  # type: ignore[method-assign]
        await backend._perform_cleanup_receipt(receipt)

        assert captured["headers"]["X-ACP-Cleanup-Project-Fingerprint"] == "a" * 64
        assert captured["headers"]["X-ACP-Cleanup-Require-Empty"] == "1"
        assert captured["allow_redirects"] is False
        assert store.pending() == [receipt]


def _without_mutation_id(body: dict[str, Any]) -> dict[str, Any]:
    assert isinstance(body.get("mutation_id"), str) and body["mutation_id"]
    return {key: value for key, value in body.items() if key != "mutation_id"}


class TestAcpDiscoveryEndpointAuthorization:
    def test_discovery_allows_internal_secret(self) -> None:
        assert "/api/models" in _MIXED_INTERNAL_API_PATHS
        assert "/api/effort-levels" in _MIXED_INTERNAL_API_PATHS
        assert "/api/slash-commands" in _MIXED_INTERNAL_API_PATHS


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
        blocks = [{"type": "resource_link", "name": "x.py", "uri": "file:///x.py"}]
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


class TestPromptBlockValidation:
    @pytest.mark.parametrize(
        "block",
        [
            {"type": "text"},
            {"type": "text", "text": 1},
            {"type": "image", "data": "bytes"},
            {"type": "image", "data": 1, "mimeType": "image/png"},
            {"type": "audio", "mimeType": "audio/wav"},
            {"type": "audio", "data": "bytes", "mimeType": 1},
            {"type": "resource_link", "name": "README"},
            {"type": "resource_link", "name": 1, "uri": "file:///README"},
            {"type": "resource"},
            {"type": "resource", "resource": {"text": "body"}},
            {"type": "resource", "resource": {"uri": "file:///x", "text": 1}},
            {"type": "unknown", "text": "hidden"},
        ],
    )
    def test_malformed_or_unknown_block_is_rejected(self, block: dict[str, Any]) -> None:
        assert _validate_prompt_blocks([block]) is not None

    @pytest.mark.parametrize(
        "block",
        [
            {"type": "text", "text": ""},
            {"type": "image", "data": "bytes", "mimeType": "image/png"},
            {"type": "audio", "data": "bytes", "mimeType": "audio/wav"},
            {"type": "resource_link", "name": "README", "uri": "file:///README"},
            {"type": "resource", "resource": {"text": "body", "uri": "file:///x"}},
            {"type": "resource", "resource": {"blob": "bytes", "uri": "file:///x"}},
        ],
    )
    def test_valid_block_is_accepted(self, block: dict[str, Any]) -> None:
        assert _validate_prompt_blocks([block]) is None


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
    async def test_session_info_is_forwarded_only_for_registered_session(self) -> None:
        srv, tr = _server(backend=_FakeBackend())
        srv._sessions["acp-slot-1"] = _Session(session_id="acp-slot-1")
        await srv._handle_session_info("acp-slot-1", "Fresh title")
        await srv._handle_session_info("other-slot", "Do not leak")
        updates = [
            params["update"]
            for method, params in tr.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert updates == [{"sessionUpdate": "session_info_update", "title": "Fresh title"}]

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
        srv._sessions["s1"] = _Session(session_id="s1", in_flight=True)
        await srv._on_notification("session/cancel", {"sessionId": "s1"})
        # Cancel is fire-and-forget; let the scheduled task run.
        await asyncio.gather(*srv._cancel_tasks)
        assert backend.cancelled == ["s1"]
        assert srv._sessions["s1"].cancelled.is_set()

    @pytest.mark.asyncio
    async def test_failed_backing_cancel_returns_internal_error(self) -> None:
        class FailingCancelBackend(_FakeBackend):
            async def cancel(self, session_id: str) -> None:
                self.cancelled.append(session_id)
                raise AcpGatewayError("stop failed")

        started = asyncio.Event()

        async def handler(_request: PromptRequest, sink: SessionSink) -> str:
            started.set()
            while not sink.cancelled:
                await asyncio.sleep(0)
            return STOP_REASON_END_TURN

        backend = FailingCancelBackend()
        transport = _FakeTransport()
        server = AcpAgentServer(transport, handler, session_backend=backend)
        server._sessions["s1"] = _Session(session_id="s1")
        prompt = asyncio.create_task(server._handle_prompt({"sessionId": "s1", "prompt": []}, 8))
        await started.wait()
        await server._on_notification("session/cancel", {"sessionId": "s1"})
        await prompt

        assert backend.cancelled == ["s1"]
        assert transport.results == {}
        assert transport.errors[8] == (JSONRPC_INTERNAL_ERROR, "Failed to cancel turn")

    @pytest.mark.asyncio
    async def test_cancel_notification_ignores_unowned_listed_session(self) -> None:
        backend = _FakeBackend()
        srv, _tr = _server(backend=backend)
        await srv._on_request(METHOD_SESSION_LIST, {"cwd": "/repo"}, 4)

        await srv._on_notification("session/cancel", {"sessionId": "acp-slot-1"})
        await asyncio.sleep(0)

        assert backend.cancelled == []
        assert srv._cancel_tasks == set()

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
    app["project_generations"] = {}  # slot -> opaque mutation generation
    app["project_generation_seq"] = 0
    app["approvals"] = []  # (slot, request_id, action)
    app["chat_posts"] = []  # request bodies received by the live chat stream
    app["chat_mcp_owners"] = []
    app["question_answers"] = []  # (slot, card, answer body)
    app["stops"] = []
    app["approve_events"] = {}
    app["title_events"] = asyncio.Queue()
    app["title_ws_connected"] = asyncio.Event()

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
        generation = app["project_generations"].get(name, "")
        expected = body.get("expected_generation")
        if expected is not None and expected != generation:
            return web.json_response(
                {
                    "ok": True,
                    "project": app["slots"][name].get("project", ""),
                    "generation": generation,
                    "applied": False,
                }
            )
        previous_project = app["slots"][name].get("project", "")
        app["project_generation_seq"] += 1
        generation = f"generation-{app['project_generation_seq']}"
        app["project_generations"][name] = generation
        app["projects"][name] = body.get("project", "")
        app["slots"][name]["project"] = body.get("project", "")
        response = {
            "ok": True,
            "project": body.get("project", ""),
            "generation": generation,
        }
        if body.get("return_previous") is True:
            response["previous_project"] = previous_project
        if expected is not None:
            response["applied"] = True
        return web.json_response(response)

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
        app["chat_posts"].append(body)
        app["chat_mcp_owners"].append(request.headers.get("X-ACP-MCP-Owner"))
        slot = body["slot"]
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)

        async def frame(obj: dict) -> None:
            await resp.write(f"data: {json.dumps(obj)}\n\n".encode())

        if body["message"] != "hi":
            await frame({"type": "chunk", "content": "Follow-up received", "cls": ""})
            await resp.write(b"data: [DONE]\n\n")
            return resp

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

    async def question_answer(request: web.Request) -> web.StreamResponse:
        body = await request.json()
        app["question_answers"].append(
            (request.match_info["slot"], request.match_info["card"], body)
        )
        if "Partial?" in body["answers"]:
            return web.json_response({"ok": True, "completed": False})
        resp = web.StreamResponse()
        resp.content_type = "text/event-stream"
        await resp.prepare(request)
        await resp.write(
            b'data: {"type":"chunk","content":"Canonical follow-up received","cls":""}\n\n'
        )
        await resp.write(b"data: [DONE]\n\n")
        return resp

    async def title_events(_request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(_request)
        app["title_ws_connected"].set()
        try:
            while not ws.closed:
                title_event = asyncio.create_task(app["title_events"].get())
                client_message = asyncio.create_task(ws.receive())
                done, pending = await asyncio.wait(
                    {title_event, client_message}, return_when=asyncio.FIRST_COMPLETED
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                if title_event in done:
                    await ws.send_json(title_event.result())
                else:
                    # The adapter sends a subscription frame before waiting for
                    # title events; keep the stub socket open for that handshake.
                    continue
        except asyncio.CancelledError:
            raise
        return ws

    app.router.add_get("/api/ws", title_events)
    app.router.add_get("/api/chat/slots", slots_list)
    app.router.add_post("/api/chat/slots", slot_create)
    app.router.add_post("/api/chat/slots/{slot}/project", slot_project)
    app.router.add_post("/api/chat/slots/{slot}/resume", slot_resume)
    app.router.add_get("/api/chat/slots/{slot}", slot_detail)
    app.router.add_post("/api/chat/slots/{slot}/stop", slot_stop)
    app.router.add_post("/api/chat/slots/{slot}/approve", slot_approve)
    app.router.add_post("/api/chat/slots/{slot}/questions/{card}/answer", question_answer)
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
    async def test_remote_gateway_with_token_requires_tls(self) -> None:
        backend = HttpGatewayBackend("http://gateway.example", token="presigned")
        with pytest.raises(AcpGatewayError, match="require https"):
            await backend.open()
        assert backend._session is None

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
    async def test_remote_gateway_rejects_adapter_local_mcp_before_spawn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        backend = HttpGatewayBackend("https://gateway.example", token="presigned")

        async def unexpected_host(*_args: Any, **_kwargs: Any) -> None:
            raise AssertionError("remote MCP child must not be spawned")

        monkeypatch.setattr(backend._mcp, "host", unexpected_host)
        server = StdioMcpServer(name="echo", command="/bin/echo")

        with pytest.raises(RuntimeError, match="require a loopback gateway"):
            await backend.configure_session_mcp("acp-remote", "/repo", [server])

    @pytest.mark.parametrize(
        "base_url",
        [
            "https://user:password@gateway.example",
            "https://gateway.example?token=secret",
            "https://gateway.example#secret",
        ],
    )
    def test_gateway_url_rejects_credential_carriers(self, base_url: str) -> None:
        with pytest.raises(AcpGatewayError, match="gateway URL must not include"):
            HttpGatewayBackend(base_url, token="presigned")

    def test_gateway_log_origin_omits_path(self) -> None:
        backend = HttpGatewayBackend(
            "https://gateway.example/private-path",
            token="presigned",
        )
        assert backend._base_url == "https://gateway.example/private-path"
        assert backend._log_origin == "https://gateway.example"
        assert backend._headers()["Origin"] == "https://gateway.example"

    @pytest.mark.asyncio
    async def test_presigned_link_is_exchanged_before_api_requests(self) -> None:
        requests: list[tuple[str, str | None, bool]] = []

        async def root(request: web.Request) -> web.Response:
            requests.append(
                (
                    request.path,
                    request.headers.get("X-Presigned-Token"),
                    bool(request.cookies),
                )
            )
            return web.Response(text="dashboard")

        async def slots(request: web.Request) -> web.Response:
            requests.append(
                (
                    request.path,
                    request.headers.get("X-Presigned-Token"),
                    bool(request.cookies),
                )
            )
            return web.json_response([])

        app = web.Application(middlewares=[token_auth_middleware()])
        app.router.add_get("/", root)
        app.router.add_get("/api/chat/slots", slots)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
        backend = HttpGatewayBackend(
            f"http://127.0.0.1:{port}",
            token=generate_token("local-app", ttl_seconds=3600),
        )

        try:
            await backend.open()
            assert backend._token is None
            assert requests[0][0] == "/"
            assert requests[0][1]
            assert requests[0][2] is False
            assert requests[1] == ("/api/chat/slots", None, True)
        finally:
            await backend.close()
            await runner.cleanup()


@pytest.mark.asyncio
class TestHttpGatewayBackend:
    async def test_dashboard_title_event_forwards_to_handler(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        received: list[tuple[str, str]] = []
        delivered = asyncio.Event()

        async def on_title(session_id: str, title: str) -> None:
            received.append((session_id, title))
            delivered.set()

        try:
            await backend.open()
            backend.set_session_info_handler(on_title)
            backend.register_session_info("acp-slot-1")
            await asyncio.wait_for(app["title_ws_connected"].wait(), timeout=2)
            await app["title_events"].put(
                {"type": "slot_title", "data": {"key": "acp-slot-1", "title": "Fresh title"}}
            )
            await asyncio.wait_for(delivered.wait(), timeout=2)
            assert received == [("acp-slot-1", "Fresh title")]
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_register_session_retains_title_refresh_until_complete(self) -> None:
        release = asyncio.Event()

        class OpenWebSocket:
            closed = False

            async def close(self) -> None:
                await release.wait()

        backend = HttpGatewayBackend("http://127.0.0.1:1", agent="")
        backend._title_ws = OpenWebSocket()  # type: ignore[assignment]

        backend.register_session_info("acp-slot-1")

        assert len(backend._title_refresh_tasks) == 1
        tasks = list(backend._title_refresh_tasks)
        release.set()
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        assert backend._title_refresh_tasks == set()

    async def test_title_websocket_handshake_timeout_reconnects(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        reconnected = asyncio.Event()

        class HttpSession:
            def __init__(self) -> None:
                self.attempts = 0

            async def ws_connect(self, *_args: Any, **_kwargs: Any) -> None:
                self.attempts += 1
                if self.attempts == 1:
                    raise asyncio.TimeoutError
                reconnected.set()
                await asyncio.Future()

        monkeypatch.setattr(http_backend_module, "_TITLE_WS_RECONNECT_DELAY_SECS", 0)
        backend = HttpGatewayBackend("http://127.0.0.1:1", token="token")
        session = HttpSession()
        backend._session = session
        task = asyncio.create_task(backend._watch_title_events())
        try:
            await asyncio.wait_for(reconnected.wait(), timeout=2)
            assert session.attempts == 2
        finally:
            backend._closing = True
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_close_continues_after_title_event_task_failure(self) -> None:
        class McpSupervisor:
            def __init__(self) -> None:
                self.shutdown_called = False

            async def shutdown(self) -> None:
                self.shutdown_called = True

        class HttpSession:
            def __init__(self) -> None:
                self.close_called = False

            async def close(self) -> None:
                self.close_called = True

        async def fail() -> None:
            raise RuntimeError("relay failed")

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        mcp = McpSupervisor()
        session = HttpSession()
        backend._mcp = mcp  # type: ignore[assignment]
        backend._session = session
        backend._title_events_task = asyncio.create_task(fail())
        await asyncio.sleep(0)

        await backend.close()

        assert mcp.shutdown_called is True
        assert session.close_called is True
        assert backend._session is None

    async def test_dashboard_message_event_forwards_once_and_ignores_own_origin(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        received: list[tuple[str, str, str, str]] = []
        delivered = asyncio.Event()

        async def on_message(session_id: str, role: str, content: str, message_id: str) -> None:
            received.append((session_id, role, content, message_id))
            delivered.set()

        try:
            await backend.open()
            backend.set_session_message_handler(on_message)
            backend.register_session_info("acp-slot-1")
            await asyncio.wait_for(app["title_ws_connected"].wait(), timeout=2)
            await app["title_events"].put(
                {
                    "type": "acp_message",
                    "data": {
                        "slot": "acp-slot-1",
                        "role": "user",
                        "content": "From dashboard",
                        "messageId": "m-dashboard",
                    },
                }
            )
            await asyncio.wait_for(delivered.wait(), timeout=2)
            await app["title_events"].put(
                {
                    "type": "acp_message",
                    "data": {
                        "slot": "acp-slot-1",
                        "role": "user",
                        "content": "From dashboard",
                        "messageId": "m-dashboard",
                    },
                }
            )
            await app["title_events"].put(
                {
                    "type": "acp_message",
                    "data": {
                        "slot": "acp-slot-1",
                        "role": "assistant",
                        "content": "From Zed",
                        "messageId": "m-zed",
                        "origin": "acp-slot-1",
                    },
                }
            )
            await asyncio.sleep(0)
            assert received == [("acp-slot-1", "user", "From dashboard", "m-dashboard")]
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_dashboard_plan_event_maps_todo_snapshot_to_acp_plan(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        received: list[tuple[str, dict[str, Any]]] = []
        delivered = asyncio.Event()

        async def on_plan(session_id: str, plan: dict[str, Any]) -> None:
            received.append((session_id, plan))
            delivered.set()

        try:
            await backend.open()
            backend.set_session_plan_handler(on_plan)
            backend.register_session_info("acp-slot-1")
            await asyncio.wait_for(app["title_ws_connected"].wait(), timeout=2)
            await app["title_events"].put(
                {
                    "type": "acp_plan",
                    "data": {
                        "slot": "acp-slot-1",
                        "description": "Validate the change",
                        "tasks": [
                            {"id": "1", "text": "inspect code", "completed": True},
                            {"id": "2", "text": "run tests", "completed": False},
                            {"id": "3", "text": "write docs", "completed": False},
                        ],
                    },
                }
            )
            await asyncio.wait_for(delivered.wait(), timeout=2)
            assert received == [
                (
                    "acp-slot-1",
                    {
                        "entries": [
                            {
                                "content": "inspect code",
                                "priority": "medium",
                                "status": "completed",
                            },
                            {
                                "content": "run tests",
                                "priority": "medium",
                                "status": "in_progress",
                            },
                            {"content": "write docs", "priority": "medium", "status": "pending"},
                        ],
                        "_meta": {"kirocrew": {"description": "Validate the change"}},
                    },
                )
            ]
        finally:
            await backend.close()
            await runner.cleanup()

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        received: list[tuple[str, str]] = []

        async def on_title(session_id: str, title: str) -> None:
            received.append((session_id, title))

        backend.set_session_info_handler(on_title)
        await backend._handle_title_event("not json")
        await backend._handle_title_event(json.dumps({"type": "slots", "data": {}}))
        await backend._handle_title_event(
            json.dumps({"type": "slot_title", "data": {"key": "acp-slot-1"}})
        )
        assert received == []

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
        app["slots"]["dashboard-old"] = {
            "key": "dashboard-old",
            "name": "dashboard-old",
            "title": "Old dashboard session",
            "project": "",
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ],
        }
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

    async def test_project_snapshot_and_restore_wrap_failed_lifecycle(self) -> None:
        runner, base, app = await _start_stub()
        app["slots"]["dashboard-old"] = {
            "key": "dashboard-old",
            "name": "dashboard-old",
            "title": "Old dashboard session",
            "project": "/repo/old",
            "messages": [],
        }
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            assert await backend.get_session_cwd("dashboard-old") == "/repo/old"
            await backend.load_session("dashboard-old", "/repo/new")
            assert await backend.restore_session_cwd("dashboard-old", "/repo/old") is True
            assert app["projects"]["dashboard-old"] == "/repo/old"
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_project_restore_is_generation_checked(self) -> None:
        runner, base, app = await _start_stub()
        app["slots"]["dashboard-old"] = {
            "key": "dashboard-old",
            "name": "dashboard-old",
            "title": "Old dashboard session",
            "project": "/repo/old",
            "messages": [],
        }
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            await backend.load_session("dashboard-old", "/repo/first")
            await backend._post_json(
                "/api/chat/slots/dashboard-old/project",
                {"project": "/repo/newer-adapter"},
            )

            assert await backend.restore_session_cwd("dashboard-old", "/repo/old") is False
            assert app["projects"]["dashboard-old"] == "/repo/newer-adapter"

            await backend.load_session("dashboard-old", "/repo/current")
            assert await backend.restore_session_cwd("dashboard-old", "/repo/old") is True
            assert app["projects"]["dashboard-old"] == "/repo/old"
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_project_restore_transport_failure_fences_next_activation(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        backend._project_generations["dashboard-old"] = "generation-old"
        failures_remaining = 2
        posted: list[tuple[str, dict[str, Any]]] = []

        async def post(pathname: str, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            nonlocal failures_remaining
            posted.append((pathname, body))
            if body.get("expected_generation") == "generation-old":
                if failures_remaining:
                    failures_remaining -= 1
                    raise AcpGatewayError("temporary restore failure")
                return {"applied": True}
            if pathname.endswith("/resume"):
                return {"ok": True}
            return {
                "generation": "generation-current",
                "previous_project": "/repo/intervening",
            }

        backend._post_json = post  # type: ignore[assignment]

        with pytest.raises(AcpGatewayError, match="temporary restore failure"):
            await backend.restore_session_cwd("dashboard-old", "/repo/old")
        assert backend._pending_project_restores["dashboard-old"] == (
            "/repo/old",
            "generation-old",
        )

        with pytest.raises(AcpGatewayError, match="temporary restore failure"):
            await backend._activate_session("dashboard-old", "/repo/current")
        assert not any(path.endswith("/resume") for path, _body in posted)

        assert await backend._activate_session("dashboard-old", "/repo/current") == (
            "generation-current",
            "/repo/intervening",
        )
        assert "dashboard-old" not in backend._pending_project_restores
        assert backend._project_generations["dashboard-old"] == "generation-current"
        assert [body.get("expected_generation") for _path, body in posted[:3]] == [
            "generation-old",
            "generation-old",
            "generation-old",
        ]
        assert posted[3][0].endswith("/resume")
        await backend.close()

    async def test_project_assignment_retries_with_same_mutation_id(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[dict[str, Any]] = []

        async def post(_pathname: str, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            posted.append(body)
            if len(posted) == 1:
                try:
                    raise aiohttp.ClientConnectionError("response lost")
                except aiohttp.ClientError as exc:
                    raise AcpGatewayError("ambiguous mutation") from exc
            return {
                "generation": "generation-current",
                "previous_project": "/repo/intervening",
            }

        backend._post_json = post  # type: ignore[assignment]

        assert await backend._set_project("dashboard-old", "/repo/current") == (
            "generation-current",
            "/repo/intervening",
        )
        assert len(posted) == 2
        assert posted[0] == posted[1]
        assert posted[0]["mutation_id"]
        await backend.close()

    async def test_cancelled_activation_restores_committed_project(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        committed = asyncio.Event()
        release_response = asyncio.Event()
        posted: list[dict[str, Any]] = []

        async def summary(_session_id: str, *, required: bool = False) -> dict[str, Any]:
            return {"project": "/repo/old"}

        async def post(pathname: str, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            posted.append(body)
            if pathname.endswith("/resume"):
                return {"ok": True}
            if body.get("expected_generation") == "generation-current":
                return {"ok": True, "applied": True}
            committed.set()
            await release_response.wait()
            return {
                "generation": "generation-current",
                "previous_project": "/repo/intervening",
            }

        backend._get_slot_summary = summary  # type: ignore[assignment]
        backend._post_json = post  # type: ignore[assignment]

        load = asyncio.create_task(backend.load_session("dashboard-old", "/repo/current"))
        await committed.wait()
        load.cancel()
        release_response.set()
        with pytest.raises(asyncio.CancelledError):
            await load

        assert posted[-1]["project"] == "/repo/intervening"
        assert posted[-1]["expected_generation"] == "generation-current"
        assert "dashboard-old" not in backend._project_generations
        await backend.close()

    async def test_create_failure_deletes_partial_slot(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        deleted: list[str] = []

        async def post(pathname: str, _body: dict[str, Any], **_kwargs: Any) -> dict[str, str]:
            if pathname == "/api/chat/slots":
                return {"key": "acp-created"}
            raise AcpGatewayError("project assignment failed")

        async def delete(session_id: str) -> None:
            deleted.append(session_id)

        backend._post_json = post  # type: ignore[assignment]
        backend.delete_session = delete  # type: ignore[assignment]

        with pytest.raises(AcpGatewayError, match="project assignment failed"):
            await backend.create_session("/repo/new")

        assert deleted == ["acp-created"]

    @pytest.mark.parametrize("method", ["load_session", "resume_session"])
    async def test_lifecycle_failure_restores_known_empty_project(self, method: str) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        calls: list[tuple[str, str]] = []

        async def summary(_session_id: str, *, required: bool = False) -> dict[str, str]:
            assert required is True
            return {"project": ""}

        async def activate(session_id: str, cwd: str) -> tuple[str | None, str | None]:
            calls.append(("activate", cwd))
            if method == "resume_session":
                raise AcpGatewayError("resume failed after mutation")
            return "generation-7", ""

        async def history(_pathname: str, **_kwargs: Any) -> None:
            raise AcpGatewayError("history failed after mutation")

        async def restore(
            _session_id: str, project: str, *, expected_generation: str | None
        ) -> None:
            calls.append(("restore", f"{project}:{expected_generation}"))

        backend._get_slot_summary = summary  # type: ignore[assignment]
        backend._activate_session = activate  # type: ignore[assignment]
        backend._get_json = history  # type: ignore[assignment]
        backend._restore_session_project = restore  # type: ignore[assignment]

        with pytest.raises(AcpGatewayError):
            await getattr(backend, method)("dashboard-old", "/repo/new")

        expected_generation = None if method == "resume_session" else "generation-7"
        assert calls == [
            ("activate", "/repo/new"),
            ("restore", f":{expected_generation}"),
        ]

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
            assert backend._tool_id_map == {}
            assert app["chat_mcp_owners"] == [backend._mcp_owner]

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

    async def test_dashboard_user_frame_forwards_with_message_id(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "user",
                "content": "dashboard steer",
                "meta": {"mid": "row-2"},
            },
            "s",
            sink,
        )
        update = next(
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        )
        assert update["sessionUpdate"] == "user_message_chunk"
        assert update["content"]["text"] == "dashboard steer"
        assert update["messageId"] == "row-2"

    async def test_originating_acp_user_frame_is_suppressed(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "user",
                "content": "own prompt",
                "meta": {"mid": "row-1", "_acp_session": "s"},
            },
            "s",
            sink,
        )
        assert sink.transport.notifications == []

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

    async def test_todo_tool_frame_is_not_projected_to_acp(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "tool",
                "content": "Completing #1",
                "tool_call_id": "todo-1",
                "tool_name": "todo_list",
            },
            "s",
            sink,
        )
        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert updates == []

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

    async def test_tool_frame_suppresses_redacted_location(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            http_backend_module,
            "redact_via_context",
            lambda text: text.replace("secret-segment", "[CLEAN]"),
        )
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink = _RecordingSink()
        await backend._translate(
            {
                "type": "tool",
                "content": "edit",
                "locations": [{"path": "/abs/secret-segment/main.py"}],
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
        assert "locations" not in update

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

    async def test_tool_update_correlation_is_scoped_to_session(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        sink_a = _RecordingSink()
        sink_a._session.session_id = "slot-a"
        sink_b = _RecordingSink()
        sink_b._session.session_id = "slot-b"

        await backend._translate(
            {"type": "tool", "content": "read a", "tool_call_id": "toolu_same"},
            "slot-a",
            sink_a,
        )
        await backend._translate(
            {"type": "tool", "content": "read b", "tool_call_id": "toolu_same"},
            "slot-b",
            sink_b,
        )
        await backend._translate(
            {"type": "tool_update", "tool_call_id": "toolu_same"},
            "slot-a",
            sink_a,
        )
        await backend._translate(
            {"type": "tool_update", "tool_call_id": "toolu_same"},
            "slot-b",
            sink_b,
        )

        updates_a = [
            params["update"]
            for method, params in sink_a.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        updates_b = [
            params["update"]
            for method, params in sink_b.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert updates_a[-1]["toolCallId"] == "gw-1"
        assert updates_b[-1]["toolCallId"] == "gw-2"

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
            content_type = "text/event-stream"

            def __init__(self) -> None:
                self.content = self._content()

            async def _content(self):
                yield b"data: [DONE]\n\n"

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

    async def test_gateway_restart_restores_mcp_before_prompt(self) -> None:
        events: list[tuple[str, Any]] = []
        proxy_specs = [
            {
                "name": "editor",
                "command": "/trusted/proxy",
                "args": ["--socket", "/proxy/editor.sock"],
                "env": [],
            }
        ]

        class Response:
            status = 200
            content_type = "text/event-stream"

            def __init__(self) -> None:
                self.content = self._content()

            async def _content(self):
                yield b"data: [DONE]\n\n"

            async def __aenter__(self) -> "Response":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

        class HttpSession:
            async def post(self, *_args: Any, **_kwargs: Any) -> Response:
                events.append(("prompt", None))
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        backend._session = HttpSession()
        backend._secret = "old-generation"
        backend._read_secret = lambda: "new-generation"  # type: ignore[method-assign]
        backend._mcp_sessions.add("s")
        backend._mcp_proxy_specs["s"] = proxy_specs

        async def post_json(pathname: str, body: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            events.append((pathname, body))
            return {"ok": True, "applied": True}

        async def no_pending(_slot: str, _sink: SessionSink) -> bool:
            return False

        async def no_options(_slot: str) -> list[str]:
            return []

        backend._post_json = post_json  # type: ignore[method-assign]
        backend._dispatch_pending_elicitations = no_pending  # type: ignore[method-assign]
        backend._options_for = no_options  # type: ignore[method-assign]

        stop = await backend._run_prompt(PromptRequest(session_id="s", text="hi"), _RecordingSink())

        assert stop == "end_turn"
        assert events[0][0] == "/api/chat/slots/s/mcp"
        assert _without_mutation_id(events[0][1]) == {
            "servers": proxy_specs,
            "owner": backend._mcp_owner,
            "mode": "restore_if_owner",
            "expected_owner": "",
        }
        assert events[1] == ("prompt", None)
        assert "s" not in backend._mcp_restore_needed

    async def test_prompt_stream_requires_completion_sentinel(self) -> None:
        class Response:
            status = 200
            content_type = "text/event-stream"

            def __init__(self) -> None:
                self.content = self._content()

            async def _content(self):
                yield b'data: {"type":"chunk","content":"partial"}\n\n'

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

        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert stop == "error"
        assert updates[0]["content"]["text"] == "partial"
        assert updates[-1]["content"]["text"].endswith("gateway stream ended before completion\n")

    async def test_prompt_stream_timeout_is_visible_to_client(self) -> None:
        class TimedOutContent:
            def __aiter__(self) -> "TimedOutContent":
                return self

            async def __anext__(self) -> bytes:
                raise asyncio.TimeoutError

        class Response:
            status = 200
            content_type = "text/event-stream"
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

    async def test_gateway_error_refuses_redirects_and_redacts_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            http_backend_module,
            "redact_via_context",
            lambda text: text.replace("gateway-secret", "[REDACTED]"),
        )

        class Response:
            status = 403

            async def __aenter__(self) -> "Response":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def text(self) -> str:
                return "gateway-secret"

        class HttpSession:
            def __init__(self) -> None:
                self.allow_redirects: bool | None = None

            async def post(self, *_args: Any, **kwargs: Any) -> Response:
                self.allow_redirects = kwargs["allow_redirects"]
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        session = HttpSession()
        backend._session = session
        sink = _RecordingSink()

        stop = await backend._run_prompt(PromptRequest(session_id="s", text="hi"), sink)

        updates = [
            params["update"]
            for method, params in sink.transport.notifications
            if method == METHOD_SESSION_UPDATE
        ]
        assert stop == "error"
        assert session.allow_redirects is False
        assert "[REDACTED]" in updates[-1]["content"]["text"]
        assert "gateway-secret" not in updates[-1]["content"]["text"]

    async def test_open_installs_websocket_redirect_refusal(self) -> None:
        runner, base, _app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        try:
            await backend.open()
            traces = backend._session._trace_configs
            assert http_backend_module._reject_gateway_redirect in traces[0].on_request_redirect
        finally:
            await backend.close()
            await runner.cleanup()

    async def test_broken_stream_refreshes_rotated_secret_on_next_turn(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        current_secret = {"value": "generation-one"}
        read_ports: list[int] = []

        def read_secret(port: int) -> str:
            read_ports.append(port)
            return current_secret["value"]

        monkeypatch.setattr(http_backend_module, "read_local_secret", read_secret)

        class BrokenContent:
            def __aiter__(self) -> "BrokenContent":
                return self

            async def __anext__(self) -> bytes:
                raise aiohttp.ClientPayloadError("response payload is incomplete")

        class Response:
            def __init__(self, *, broken: bool = False) -> None:
                self.status = 200
                self.content_type = "text/event-stream"
                self.content = BrokenContent() if broken else self._content()

            async def _content(self):
                yield b"data: [DONE]\n"

            async def __aenter__(self) -> "Response":
                return self

            async def __aexit__(self, *_args: Any) -> None:
                return None

            async def json(self) -> dict[str, list[Any]]:
                return {"questions": []}

        class HttpSession:
            def __init__(self) -> None:
                self.prompt_headers: list[str | None] = []

            async def post(self, *_args: Any, **kwargs: Any) -> Response:
                self.prompt_headers.append(kwargs["headers"].get("X-Internal-Secret"))
                return Response(broken=len(self.prompt_headers) == 1)

            def get(self, *_args: Any, **_kwargs: Any) -> Response:
                return Response()

        backend = HttpGatewayBackend("http://127.0.0.1:6123")
        session = HttpSession()
        backend._session = session

        first = await backend._run_prompt(
            PromptRequest(session_id="s", text="first"), _RecordingSink()
        )
        assert first == "error"

        current_secret["value"] = "generation-two"
        second = await backend._run_prompt(
            PromptRequest(session_id="s", text="second"), _RecordingSink()
        )

        assert second == "end_turn"
        assert session.prompt_headers == ["generation-one", "generation-two"]
        assert read_ports and set(read_ports) == {6123}

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

    async def test_list_sessions_resolves_projects_off_loop(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp_server import http_backend as http_backend_mod

        backend = HttpGatewayBackend("http://127.0.0.1:1", agent="")
        event_loop_thread = threading.get_ident()
        resolver_threads: list[int] = []
        original = http_backend_mod._project_paths_match

        def tracked_match(left: str, right: str) -> bool:
            resolver_threads.append(threading.get_ident())
            return original(left, right)

        async def get_slots() -> list[dict[str, Any]]:
            return [{"key": "acp-slot", "project": "/repo"}]

        monkeypatch.setattr(http_backend_mod, "_project_paths_match", tracked_match)
        monkeypatch.setattr(backend, "_get_slots", get_slots)

        result = await backend.list_sessions(cwd="/repo")

        assert result["sessions"][0]["sessionId"] == "acp-slot"
        assert resolver_threads
        assert all(thread_id != event_loop_thread for thread_id in resolver_threads)

    async def test_list_sessions_offers_moved_acp_slot_for_relocation(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        old_project = "/workspace/old-project"
        new_project = "/workspace/new-project"
        try:
            await backend.open()
            session_id = await backend.create_session("")
            app["slots"][session_id]["project"] = old_project
            app["slots"]["dashboard-old"] = {
                "key": "dashboard-old",
                "name": "dashboard-old",
                "project": old_project,
            }

            result = await backend.list_sessions(cwd=new_project)

            assert result["sessions"] == [
                {
                    "sessionId": session_id,
                    "cwd": new_project,
                    "title": None,
                    "updatedAt": "2026-08-21T22:00:00+00:00",
                }
            ]
            assert app["slots"][session_id]["project"] == old_project

            await backend.load_session(session_id, new_project)

            assert app["projects"][session_id] == new_project
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


@pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="editor MCP subprocess proxy requires Unix sockets"
)
class TestHttpBackendMcpPreflight:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch, tmp_path):
        self.mcp_cwd = str(tmp_path)
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
            assert kwargs.get("cwd") == self.mcp_cwd
            descriptor = kwargs.pop("chdir_fd")
            assert isinstance(descriptor, int) and descriptor >= 0
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
            await backend.configure_session_mcp("acp-x", self.mcp_cwd, [server])
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
            assert body["owner"] == backend._mcp_owner
            assert body["mode"] == "replace"
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
        await backend.configure_session_mcp("acp-z", self.mcp_cwd, [])
        # A local empty replacement requests the prior registration for rollback.
        assert [(path, _without_mutation_id(body)) for path, body in posted] == [
            (
                "/api/chat/slots/acp-z/mcp",
                {
                    "servers": [],
                    "owner": backend._mcp_owner,
                    "mode": "replace",
                    "return_previous": True,
                },
            )
        ]
        await backend.close()

    @pytest.mark.asyncio
    async def test_failed_replacement_clears_stale_registration(self) -> None:
        import sys

        from kiro_crew.acp_server.mcp_config import StdioMcpServer
        from kiro_crew.acp_server.mcp_supervisor import McpSpawnError

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        good = StdioMcpServer(name="good", command=sys.executable, args=["-c", _GOOD_MCP])
        try:
            await backend.configure_session_mcp("acp-y", self.mcp_cwd, [good])
            old_proc = backend._mcp._sessions["acp-y"][0].proc
            posted.clear()

            with pytest.raises(McpSpawnError):
                await backend.configure_session_mcp(
                    "acp-y",
                    self.mcp_cwd,
                    [StdioMcpServer(name="nope", command="/no/such/binary-xyz")],
                )

            assert old_proc.returncode is not None
            assert backend._mcp.hosted("acp-y") == []
            assert [(path, _without_mutation_id(body)) for path, body in posted] == [
                (
                    "/api/chat/slots/acp-y/mcp",
                    {
                        "servers": [],
                        "owner": backend._mcp_owner,
                        "mode": "clear_if_owner",
                    },
                )
            ]
            assert "acp-y" not in backend._mcp_sessions
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_local_restore_refuses_newer_owner(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            return {"ok": True, "applied": False}

        backend._post_json = _fake_post  # type: ignore[assignment]

        applied = await backend.restore_local_session_mcp("acp-z", self.mcp_cwd, [])

        assert applied is False
        assert [(path, _without_mutation_id(body)) for path, body in posted] == [
            (
                "/api/chat/slots/acp-z/mcp",
                {
                    "servers": [],
                    "owner": backend._mcp_owner,
                    "mode": "restore_if_owner",
                    "expected_owner": backend._mcp_owner,
                },
            )
        ]
        await backend.close()

    @pytest.mark.asyncio
    async def test_registration_cancellation_clears_and_reaps_replacement(self) -> None:
        import sys

        from kiro_crew.acp_server.mcp_config import StdioMcpServer

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            if body["servers"]:
                raise asyncio.CancelledError
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        server = StdioMcpServer(name="good", command=sys.executable, args=["-c", _GOOD_MCP])
        try:
            with pytest.raises(asyncio.CancelledError):
                await backend.configure_session_mcp("acp-y", self.mcp_cwd, [server])

            assert backend._mcp.hosted("acp-y") == []
            assert posted[-1][0] == "/api/chat/slots/acp-y/mcp"
            assert _without_mutation_id(posted[-1][1]) == {
                "servers": [],
                "owner": backend._mcp_owner,
                "mode": "clear_if_owner",
            }
            assert "acp-y" not in backend._mcp_sessions
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_cancelled_replacement_restores_prior_registration(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1")
        entered = asyncio.Event()
        release = asyncio.Event()
        posted: list[tuple[str, dict]] = []
        previous = {
            "servers": [{"name": "old", "command": "/old", "args": [], "env": []}],
            "owner": "adapter-a",
            "expected_generation": "replacement-generation",
        }

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            if body["mode"] == "replace":
                entered.set()
                await release.wait()
                return {"previous": previous}
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        replacement = asyncio.create_task(backend.configure_session_mcp("acp-y", self.mcp_cwd, []))
        await entered.wait()
        replacement.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await replacement

        assert [body["mode"] for _path, body in posted] == [
            "replace",
            "restore_if_owner",
            "clear_if_owner",
        ]
        assert _without_mutation_id(posted[1][1]) == {
            "servers": previous["servers"],
            "owner": "adapter-a",
            "mode": "restore_if_owner",
            "expected_owner": backend._mcp_owner,
            "expected_generation": previous["expected_generation"],
        }
        await backend.close()

    @pytest.mark.asyncio
    async def test_registration_failure_clears_and_reaps_replacement(self) -> None:
        import sys

        from kiro_crew.acp_server.mcp_config import StdioMcpServer

        backend = HttpGatewayBackend("http://127.0.0.1:1")
        posted: list[tuple[str, dict]] = []

        async def _fake_post(pathname, body, *, allow_fail=False):
            posted.append((pathname, body))
            if body["servers"]:
                raise AcpGatewayError("registration failed")
            return {}

        backend._post_json = _fake_post  # type: ignore[assignment]
        server = StdioMcpServer(name="good", command=sys.executable, args=["-c", _GOOD_MCP])
        try:
            with pytest.raises(AcpGatewayError, match="registration failed"):
                await backend.configure_session_mcp("acp-y", self.mcp_cwd, [server])

            assert backend._mcp.hosted("acp-y") == []
            assert posted[-1][0] == "/api/chat/slots/acp-y/mcp"
            assert _without_mutation_id(posted[-1][1]) == {
                "servers": [],
                "owner": backend._mcp_owner,
                "mode": "clear_if_owner",
            }
            assert "acp-y" not in backend._mcp_sessions
        finally:
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
        # An unavailable persisted effort maps to the resolvable default id.
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


class TestElicitationBridge:
    @pytest.mark.asyncio
    async def test_pending_question_streams_accepted_follow_up(self) -> None:
        class ElicitationTransport(_FakeTransport):
            async def send_request(
                self, method: str, params: dict, *, timeout: float = 120.0
            ) -> Any:
                self.requests.append((method, params))
                return {"action": "accept", "content": {"answer": "Yes"}}

        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        transport = ElicitationTransport()
        try:
            await backend.open()
            sid = await backend.create_session("")
            sink = SessionSink(transport, _Session(session_id=sid, elicitation_supported=True))
            original_get_json = backend._get_json

            async def get_json(path: str, *, allow_fail: bool = False) -> Any:
                if path == f"/api/chat/slots/{sid}/questions":
                    return {
                        "questions": [
                            {
                                "question_id": "card-1",
                                "state": "pending",
                                "answers": {},
                                "questions": [
                                    {
                                        "question": "Ship it?",
                                        "header": "Release",
                                        "options": [
                                            {"label": "Yes", "description": "Ship"},
                                            {"label": "No", "description": "Hold"},
                                        ],
                                        "multiSelect": False,
                                    }
                                ],
                            }
                        ]
                    }
                return await original_get_json(path, allow_fail=allow_fail)

            backend._get_json = get_json  # type: ignore[assignment]
            assert await backend._dispatch_pending_elicitations(sid, sink) is True
            await asyncio.gather(*backend._elicitation_tasks.values())

            form = transport.requests[0][1]
            assert form["requestedSchema"]["properties"]["answer"]["oneOf"] == [
                {"const": "Yes", "title": "Yes", "description": "Ship"},
                {"const": "No", "title": "No", "description": "Hold"},
            ]
            assert app["question_answers"] == [(sid, "card-1", {"answers": {"Ship it?": "Yes"}})]
            updates = [
                params["update"]
                for method, params in transport.notifications
                if method == METHOD_SESSION_UPDATE
            ]
            assert any(
                update.get("content", {}).get("text") == "Canonical follow-up received"
                for update in updates
            )
        finally:
            await backend.close()
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_partial_question_json_ack_is_not_parsed_as_sse(self) -> None:
        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        transport = _FakeTransport()
        try:
            await backend.open()
            sid = await backend.create_session("")
            sink = SessionSink(transport, _Session(session_id=sid, elicitation_supported=True))

            stop = await backend._stream_follow_up(
                f"/api/chat/slots/{sid}/questions/card-1/answer?stream=1",
                {"answers": {"Partial?": "Yes"}},
                sid,
                sink,
            )

            assert stop == "end_turn"
            assert app["question_answers"] == [(sid, "card-1", {"answers": {"Partial?": "Yes"}})]
            assert transport.notifications == []
        finally:
            await backend.close()
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_pending_question_callbacks_remove_their_own_keys(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1", agent="")
        gates = {0: asyncio.Event(), 1: asyncio.Event()}

        async def get_json(_path: str, *, allow_fail: bool = False) -> Any:
            return {
                "questions": [
                    {
                        "question_id": "card-1",
                        "state": "pending",
                        "answers": {},
                        "questions": [
                            {
                                "question": "First?",
                                "options": [
                                    {"label": "Yes", "description": "Proceed"},
                                    {"label": "No", "description": "Stop"},
                                ],
                            },
                            {
                                "question": "Second?",
                                "options": [
                                    {"label": "Yes", "description": "Proceed"},
                                    {"label": "No", "description": "Stop"},
                                ],
                            },
                        ],
                    }
                ]
            }

        async def run_question(
            key: tuple[str, str, int],
            _question: dict[str, Any],
            _form: dict[str, Any],
            _sink: Any,
        ) -> None:
            await gates[key[2]].wait()

        class Sink:
            supports_elicitation = True

        backend._get_json = get_json  # type: ignore[assignment]
        backend._run_question_elicitation = run_question  # type: ignore[method-assign]

        assert await backend._dispatch_pending_elicitations("acp-slot-1", Sink()) is True  # type: ignore[arg-type]
        first = ("acp-slot-1", "card-1", 0)
        second = ("acp-slot-1", "card-1", 1)
        tasks = dict(backend._elicitation_tasks)

        gates[0].set()
        await tasks[first]
        await asyncio.sleep(0)
        assert first not in backend._elicitation_tasks
        assert second in backend._elicitation_tasks

        gates[1].set()
        await tasks[second]
        await asyncio.sleep(0)
        assert backend._elicitation_tasks == {}

    def test_question_form_projects_multi_select_as_string_array(self) -> None:
        form = HttpGatewayBackend._question_form(
            {
                "question": "Choose features",
                "header": "Settings",
                "multiSelect": True,
                "options": [
                    {"label": "One", "description": "First"},
                    {"label": "Two", "description": "Second"},
                ],
            }
        )
        assert form is not None
        answer = form["requestedSchema"]["properties"]["answer"]
        assert answer == {
            "type": "array",
            "title": "Choose features",
            "items": {"type": "string", "enum": ["One", "Two"]},
            "minItems": 1,
        }

    @pytest.mark.asyncio
    async def test_options_fallback_projects_and_streams_multi_select(self) -> None:
        class ElicitationTransport(_FakeTransport):
            async def send_request(
                self, method: str, params: dict, *, timeout: float = 120.0
            ) -> Any:
                self.requests.append((method, params))
                return {"action": "accept", "content": {"answer": ["One", "Two", "One"]}}

        runner, base, app = await _start_stub()
        backend = HttpGatewayBackend(base, agent="")
        transport = ElicitationTransport()
        try:
            await backend.open()
            sid = await backend.create_session("")
            sink = SessionSink(transport, _Session(session_id=sid, elicitation_supported=True))

            await backend._dispatch_options_elicitation(sid, ["One", "Two"], sink)
            await asyncio.gather(*backend._elicitation_tasks.values())

            form = transport.requests[0][1]
            assert form["message"] == "Choose one or more options"
            assert form["requestedSchema"]["properties"]["answer"] == {
                "type": "array",
                "items": {"type": "string", "enum": ["One", "Two"]},
                "minItems": 1,
            }
            assert app["chat_posts"] == [{"message": "One, Two", "slot": sid}]
            updates = [
                params["update"]
                for method, params in transport.notifications
                if method == METHOD_SESSION_UPDATE
            ]
            assert any(
                update.get("content", {}).get("text") == "Follow-up received" for update in updates
            )
        finally:
            await backend.close()
            await runner.cleanup()

    @pytest.mark.asyncio
    async def test_options_fallback_allows_recursive_follow_up_generation(self) -> None:
        backend = HttpGatewayBackend("http://127.0.0.1:1", agent="")
        second_started = asyncio.Event()
        release_second = asyncio.Event()
        forms: list[dict[str, Any]] = []
        streamed: list[dict[str, Any]] = []

        class Result:
            accepted = True

            def __init__(self, answer: str) -> None:
                self.content = {"answer": [answer]}

        class Sink:
            supports_elicitation = True

            async def create_elicitation(self, form: dict[str, Any]) -> Result:
                forms.append(form)
                answer = form["requestedSchema"]["properties"]["answer"]["items"]["enum"][0]
                if len(forms) == 2:
                    second_started.set()
                    await release_second.wait()
                return Result(answer)

        sink = Sink()

        async def stream_follow_up(
            _path: str,
            payload: dict[str, Any],
            slot: str,
            _sink: Any,
        ) -> None:
            streamed.append(payload)
            if len(streamed) == 1:
                await backend._dispatch_options_elicitation(slot, ["Second"], sink)  # type: ignore[arg-type]

        backend._stream_follow_up = stream_follow_up  # type: ignore[method-assign]
        key = ("acp-slot-1", "options", 0)
        await backend._dispatch_options_elicitation("acp-slot-1", ["First"], sink)  # type: ignore[arg-type]
        first = backend._elicitation_tasks[key]
        await first
        await second_started.wait()

        second = backend._elicitation_tasks[key]
        assert second is not first
        release_second.set()
        await second
        await asyncio.sleep(0)

        assert [payload["message"] for payload in streamed] == ["First", "Second"]
        assert backend._elicitation_tasks == {}
        assert backend._elicitation_follow_up_tasks == set()
