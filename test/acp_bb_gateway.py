"""Deterministic, offline fake Kiro Crew gateway for the ACP black-box gate.

``kirocrew acp`` (default mode) is a stdio->HTTP adapter: it turns an editor's
ACP calls into calls against the dashboard gateway's HTTP surface
(:class:`kiro_crew.acp_server.http_backend.HttpGatewayBackend`). The black-box
conformance gate treats ``kirocrew acp`` as an external binary, so it stubs that
gateway at its *real HTTP seam* with this server instead of spawning a full
5-15s dashboard gateway + a model. That keeps the suite deterministic, offline,
fast, and free of daemon/user state — while still exercising the genuine adapter
process (transport, server dispatch, http_backend, mcp preflight) over the wire.

Bound to 127.0.0.1 only. Every request is recorded so a test can assert what the
adapter sent (created slots, project scoping, MCP registrations, approvals,
stops). ``POST /api/chat`` streams a deterministic SSE reply keyed on prompt
sentinels (mirroring ``kiro_crew.testing.fake_acp_backend``):

* default            -> one text chunk echoing the message, then ``[DONE]``
* ``[[THINK]]``      -> a thinking chunk, then the text chunk
* ``[[TOOL]]``       -> a tool chunk before the reply (no permission)
* ``[[PERMISSION]]`` -> a permission frame (adapter must bridge it out + answer)
* ``[[OPTIONS]]``    -> marks the slot so the follow-up options lookup returns options
* ``[[SLOW]]``       -> a chunk then periodic keepalives (for cancel-mid-turn)
* ``[[GWERROR]]``    -> HTTP 500 (no SSE) to exercise the -32603 mapping
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeGatewayState:
    """Shared, thread-safe recording of everything the adapter sent us."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.created_slots: list[dict[str, Any]] = []
        self.projects: dict[str, str] = {}
        self.resumes: list[str] = []
        self.stops: list[str] = []
        self.mcp: dict[str, list[dict[str, Any]]] = {}
        self.approvals: list[dict[str, Any]] = []
        self.chat_posts: list[dict[str, Any]] = []
        self._slot_seq = 0
        # Preconfigured, per-test knobs:
        self.slot_messages: dict[str, list[dict[str, str]]] = {}
        self.list_slots: list[dict[str, Any]] = []
        self.options_slots: set[str] = set()
        self.reject_open_403 = False
        # ── selector state (model + reasoning effort) ──
        # Registry-shaped model rows served by GET /api/models (default-first,
        # matching the real endpoint). Reasoning-effort levels served by
        # GET /api/effort-levels (ordered, excludes the "" default).
        self.available_models: list[dict[str, Any]] = [
            {
                "model_name": "sonnet-4.6-1m",
                "display_name": "Claude Sonnet 4.6 (1M)",
                "description": "Default model",
            },
            {
                "model_name": "opus-4.8",
                "display_name": "Claude Opus 4.8",
                "description": "Most capable",
            },
        ]
        self.effort_levels: list[str] = ["low", "medium", "high", "xhigh", "max"]
        # Per-slot current selections ("" = provider default). Mutated by the
        # model / reasoning-effort POST endpoints, read back by slot detail — so
        # a set_* really changes what the next get_session_selectors observes.
        self.slot_model: dict[str, str] = {}
        self.slot_effort: dict[str, str] = {}
        self.model_switches: list[dict[str, str]] = []
        self.effort_switches: list[dict[str, str]] = []
        # A model switch resets the slot session (provider recreated next turn);
        # counted so a test can assert the recreation happened.
        self.provider_recreations = 0
        # Force a selector endpoint to 500 (exercises the -32603 rollback path).
        self.fail_model_switch = False
        self.fail_effort_switch = False

    def next_slot_key(self) -> str:
        with self.lock:
            self._slot_seq += 1
            return f"acp-slot-{self._slot_seq}"


def _sse_lines_for(message: str) -> list[str]:
    """The deterministic SSE line sequence for a prompt message (excl. keepalive)."""
    lines: list[str] = []
    if "[[THINK]]" in message:
        lines.append(json.dumps({"type": "chunk", "cls": "thinking", "content": "pondering"}))
    if "[[TOOL]]" in message or "[[PERMISSION]]" in message:
        lines.append(json.dumps({"type": "tool", "content": "demo tool\nran"}))
    if "[[PERMISSION]]" in message:
        lines.append(
            json.dumps(
                {
                    "type": "permission",
                    "content": "approve demo tool?",
                    "meta": {
                        "request_id": "perm-1",
                        "tool_call_id": "tc-1",
                        "tool_title": "demo tool",
                        "tool_input": "echo hi",
                    },
                }
            )
        )
    # The canonical reply chunk always echoes the message so a test can assert
    # content fidelity (e.g. that a resource_link uri survived the boundary).
    lines.append(json.dumps({"type": "chunk", "content": f"pong from fake gateway :: {message}"}))
    return lines


def _make_handler(state: FakeGatewayState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_a: Any) -> None:  # keep test output clean
            pass

        # ── helpers ──
        def _send_json(self, code: int, obj: Any) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(body)

        def _read_body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                out = json.loads(raw or b"{}")
                return out if isinstance(out, dict) else {}
            except (ValueError, TypeError):
                return {}

        def _slot_id_from(self, marker: str) -> str:
            # /api/chat/slots/<id>/<marker>  ->  <id>
            path = self.path.split("?", 1)[0]
            rest = path[len("/api/chat/slots/") :]
            return rest[: -(len(marker) + 1)] if marker else rest

        # ── GET ──
        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler override)
            path = self.path.split("?", 1)[0]
            if path == "/api/models":
                self._send_json(200, list(state.available_models))
                return
            if path == "/api/effort-levels":
                self._send_json(200, list(state.effort_levels))
                return
            if path == "/api/chat/slots":
                if state.reject_open_403:
                    self._send_json(403, {"error": "forbidden"})
                    return
                slots = [dict(slot) for slot in state.list_slots]
                known = {slot.get("key") or slot.get("name") for slot in slots}
                with state.lock:
                    for slot in slots:
                        key = slot.get("key") or slot.get("name")
                        if isinstance(key, str):
                            slot.setdefault("model", state.slot_model.get(key, ""))
                            slot.setdefault("reasoning_effort", state.slot_effort.get(key, ""))
                            slot.setdefault("running", False)
                    for created in state.created_slots:
                        key = created.get("key")
                        if isinstance(key, str) and key not in known:
                            slots.append(
                                {
                                    "key": key,
                                    "model": state.slot_model.get(key, ""),
                                    "reasoning_effort": state.slot_effort.get(key, ""),
                                    "running": False,
                                }
                            )
                            known.add(key)
                    for key in state.options_slots:
                        existing = next(
                            (
                                slot
                                for slot in slots
                                if (slot.get("key") or slot.get("name")) == key
                            ),
                            None,
                        )
                        if existing is not None:
                            existing["has_options"] = True
                            existing["options"] = ["Yes", "No", "Maybe"]
                        else:
                            slots.append(
                                {
                                    "key": key,
                                    "has_options": True,
                                    "options": ["Yes", "No", "Maybe"],
                                    "model": state.slot_model.get(key, ""),
                                    "reasoning_effort": state.slot_effort.get(key, ""),
                                    "running": False,
                                }
                            )
                self._send_json(200, slots)
                return
            if path.startswith("/api/chat/slots/"):
                slot = path[len("/api/chat/slots/") :]
                with state.lock:
                    messages = list(state.slot_messages.get(slot, []))
                    body: dict[str, Any] = {
                        "key": slot,
                        "messages": messages,
                    }
                    if slot in state.options_slots:
                        body["has_options"] = True
                        body["options"] = ["Yes", "No", "Maybe"]
                self._send_json(200, body)
                return
            self._send_json(404, {"error": "not found"})

        # ── POST ──
        def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler override)
            path = self.path.split("?", 1)[0]
            body = self._read_body()

            if path == "/api/chat/slots":
                key = state.next_slot_key()
                with state.lock:
                    state.created_slots.append({"key": key, "body": body})
                self._send_json(200, {"key": key})
                return

            if path == "/api/chat":
                self._stream_chat(body)
                return

            if path.endswith("/project"):
                slot = self._slot_id_from("project")
                with state.lock:
                    state.projects[slot] = str(body.get("project", ""))
                self._send_json(200, {"ok": True})
                return

            if path.endswith("/resume"):
                with state.lock:
                    state.resumes.append(self._slot_id_from("resume"))
                self._send_json(200, {"ok": True})
                return

            if path.endswith("/stop"):
                with state.lock:
                    state.stops.append(self._slot_id_from("stop"))
                self._send_json(200, {"ok": True})
                return

            if path.endswith("/mcp"):
                slot = self._slot_id_from("mcp")
                with state.lock:
                    state.mcp[slot] = list(body.get("servers", []))
                self._send_json(
                    200, {"ok": True, "servers": [s.get("name") for s in body.get("servers", [])]}
                )
                return

            if path.endswith("/approve"):
                slot = self._slot_id_from("approve")
                with state.lock:
                    state.approvals.append({"slot": slot, **body})
                self._send_json(200, {"ok": True})
                return

            if path.endswith("/reasoning-effort"):
                slot = self._slot_id_from("reasoning-effort")
                effort = str(body.get("reasoning_effort", ""))
                with state.lock:
                    state.effort_switches.append({"slot": slot, "reasoning_effort": effort})
                    failed = state.fail_effort_switch
                    if not failed:
                        state.slot_effort[slot] = effort
                if failed:
                    self._send_json(500, {"error": "simulated effort failure"})
                    return
                self._send_json(200, {"ok": True, "reasoning_effort": effort})
                return

            if path.endswith("/model"):
                slot = self._slot_id_from("model")
                model = str(body.get("model", ""))
                with state.lock:
                    state.model_switches.append({"slot": slot, "model": model})
                    failed = state.fail_model_switch
                    if not failed:
                        state.slot_model[slot] = model
                        state.provider_recreations += 1
                if failed:
                    self._send_json(500, {"error": "simulated model failure"})
                    return
                self._send_json(200, {"ok": True, "model": model})
                return

            self._send_json(200, {"ok": True})

        def _stream_chat(self, body: dict[str, Any]) -> None:
            message = str(body.get("message", ""))
            slot = str(body.get("slot", ""))
            with state.lock:
                state.chat_posts.append({"message": message, "slot": slot})
                if "[[OPTIONS]]" in message:
                    state.options_slots.add(slot)

            if "[[GWERROR]]" in message:
                self._send_json(500, {"error": "simulated gateway failure"})
                return

            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            def emit(line: str) -> bool:
                try:
                    self.wfile.write(f"data: {line}\n".encode("utf-8"))
                    self.wfile.flush()
                    return True
                except (BrokenPipeError, ConnectionResetError):
                    return False

            for line in _sse_lines_for(message):
                if not emit(line):
                    return
            if "[[SLOW]]" in message:
                # Keep the turn "running" with periodic keepalives so the adapter's
                # per-line cancel check can fire; bounded so a test can never hang.
                deadline = time.monotonic() + 8.0
                while time.monotonic() < deadline:
                    try:
                        self.wfile.write(b": keepalive\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        return
                    time.sleep(0.1)
            emit("[DONE]")

    return Handler


class FakeGateway:
    """Context-managed threaded fake gateway bound to 127.0.0.1:<ephemeral>."""

    def __init__(self) -> None:
        self.state = FakeGatewayState()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> "FakeGateway":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
