"""A fake model on ``127.0.0.1:<ephemeral>``: no credential, no network.

It speaks just enough of three wire APIs to finish one text turn:

* ``POST .../responses`` -- OpenAI Responses (codex), streamed;
* ``POST .../chat/completions`` -- OpenAI chat completions, streamed or not;
* ``POST .../messages`` -- Anthropic Messages (claude), streamed;
* ``GET`` anything -- a one-model list.

Every POST is recorded as ``{path, bytes, tools: {name: bytes}}``, so the size of
the tool specs a harness sends on a request is observable. Bodies are not kept.
"""

from __future__ import annotations

import json
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG: list[dict] = []
_LOCK = threading.Lock()


def _tools(body: dict) -> dict[str, int]:
    sizes: dict[str, int] = {}
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or (tool.get("function") or {}).get("name") or tool.get("type")
        sizes[str(name)] = len(json.dumps(tool))
    return sizes


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # Skips HTTPServer's getfqdn(), a resolver call this loopback server never needs.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, ctype: str, payload: bytes):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):  # noqa: N802
        models = {"object": "list", "data": [{"id": "stub-model", "object": "model"}], "models": []}
        self._send("application/json", json.dumps(models).encode())

    def do_POST(self):  # noqa: N802
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            body = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            body = {}
        with _LOCK:
            LOG.append({"path": self.path, "bytes": len(raw), "tools": _tools(body)})
            n = len(LOG)
        path = self.path.split("?")[0]
        if path.endswith("/responses"):
            return self._responses(n)
        if path.endswith("/messages"):
            return self._anthropic(n, body)
        return self._chat(n, body)

    def _sse(self, events: list[tuple[str | None, dict | str]]):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for event, data in events:
            text = data if isinstance(data, str) else json.dumps(data)
            if event:
                self.wfile.write(f"event: {event}\n".encode())
            self.wfile.write(f"data: {text}\n\n".encode())
        self.wfile.flush()

    def _responses(self, n: int):
        usage = {
            "input_tokens": 100,
            "input_tokens_details": None,
            "output_tokens": 5,
            "output_tokens_details": None,
            "total_tokens": 105,
        }
        message = {
            "type": "message",
            "role": "assistant",
            "id": f"msg_{n}",
            "content": [{"type": "output_text", "text": "STUB-OK"}],
        }
        self._sse(
            [
                ("response.created", {"type": "response.created", "response": {"id": f"r{n}"}}),
                (
                    "response.output_item.done",
                    {"type": "response.output_item.done", "item": message},
                ),
                (
                    "response.completed",
                    {"type": "response.completed", "response": {"id": f"r{n}", "usage": usage}},
                ),
            ]
        )

    def _chat(self, n: int, body: dict):
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not body.get("stream"):
            reply = {
                "id": f"c{n}",
                "object": "chat.completion",
                "created": 0,
                "model": "stub-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "STUB-OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": usage,
            }
            return self._send("application/json", json.dumps(reply).encode())
        base = {
            "id": f"c{n}",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "stub-model",
        }
        first = {"index": 0, "delta": {"role": "assistant", "content": "STUB-OK"}}
        self._sse(
            [
                (None, {**base, "choices": [first]}),
                (None, {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}),
                (None, {**base, "choices": [], "usage": usage}),
                (None, "[DONE]"),
            ]
        )

    def _anthropic(self, n: int, body: dict):
        message = {
            "id": f"msg_{n}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model") or "stub-model",
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }
        block = {"type": "text", "text": ""}
        delta = {"type": "text_delta", "text": "STUB-OK"}
        stop = {"stop_reason": "end_turn", "stop_sequence": None}
        self._sse(
            [
                ("message_start", {"type": "message_start", "message": message}),
                (
                    "content_block_start",
                    {"type": "content_block_start", "index": 0, "content_block": block},
                ),
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": 0, "delta": delta},
                ),
                ("content_block_stop", {"type": "content_block_stop", "index": 0}),
                (
                    "message_delta",
                    {"type": "message_delta", "delta": stop, "usage": {"output_tokens": 2}},
                ),
                ("message_stop", {"type": "message_stop"}),
            ]
        )


def start() -> int:
    """Serve on an ephemeral loopback port, in a daemon thread; return the port."""
    server = _Server(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server.server_address[1]
