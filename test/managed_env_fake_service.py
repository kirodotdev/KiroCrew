"""A loopback fake of the managed-environment service, for the lane's tests.

It speaks rpc-v2-cbor on ``127.0.0.1``, keeps environments in memory and records
every request. ``answer(op, status, body)`` queues one scripted reply for an
operation; ``redirect`` makes every call answer a 302 to another host.
"""

from __future__ import annotations

import threading
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler
from typing import Any

from loopback_servers import LoopbackThreadingHTTPServer

from kiro_crew.apps.rpc_cbor import cbor_decode, cbor_encode

PREFIX = "/service/KiroWebBearerService/operation/"


class FakeService:
    def __init__(self) -> None:
        self.sandboxes: dict[str, dict] = {}
        self.requests: list[dict] = []
        self.scripted: dict[str, deque] = defaultdict(deque)
        self.redirect = False
        self.page_size = 0  # 0 = one page
        self._next = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                op = self.path[len(PREFIX) :] if self.path.startswith(PREFIX) else ""
                fake.requests.append(
                    {"op": op, "headers": dict(self.headers), "body": cbor_decode(raw)}
                )
                if fake.redirect:
                    self.send_response(302)
                    self.send_header("Location", "https://elsewhere.example/")
                    self.end_headers()
                    return
                status, out = fake.reply(op, fake.requests[-1]["body"])
                data = out if isinstance(out, bytes) else cbor_encode(out)
                self.send_response(status)
                self.send_header("Content-Type", "application/cbor")
                self.send_header("smithy-protocol", "rpc-v2-cbor")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = LoopbackThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.endpoint = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def answer(self, op: str, status: int, body: Any) -> None:
        self.scripted[op].append((status, body))

    def ops(self) -> list[str]:
        return [r["op"] for r in self.requests]

    def add(self, status: str = "RUNNING", name: str = "other") -> str:
        self._next += 1
        sid = f"env-{self._next:03d}"
        self.sandboxes[sid] = {"execEnvironmentId": sid, "status": status, "displayName": name}
        return sid

    def reply(self, op: str, body: dict) -> tuple[int, Any]:
        if self.scripted[op]:
            return self.scripted[op].popleft()
        sid = body.get("execEnvironmentId") or body.get("identifier", {}).get(
            "executionEnvironmentId"
        )
        if op == "ListSandboxes":
            rows = list(self.sandboxes.values())
            start = int(body.get("nextToken") or 0)
            if not self.page_size:
                return 200, {"sandboxes": rows}
            end = start + self.page_size
            out: dict = {"sandboxes": rows[start:end]}
            if end < len(rows):
                out["nextToken"] = str(end)
            return 200, out
        if op == "CreateSandbox":
            new = self.add("PENDING", body["displayName"])
            return 200, self.sandboxes[new]
        if sid not in self.sandboxes:
            return 404, {"__type": "x#ResourceNotFoundException", "message": "no such sandbox"}
        row = self.sandboxes[sid]
        if op == "StopSandbox":
            row["status"] = "STOPPING"
        elif op == "ResumeSandbox":
            row["status"] = "PENDING"
        elif op == "DeleteSandbox":
            del self.sandboxes[sid]
            return 200, {**row, "status": "DELETING"}
        elif op == "StartSshSession":
            return 200, {"ssmSessionId": "s-1", "streamUrl": "wss://x", "tokenValue": "tv"}
        return 200, dict(row)
