"""A test memory provider: the memory provider contract, served from one JSON file.

Stdlib only, so it doubles as the smallest complete example of a provider backend:

- ``GET /health`` answers unsigned (the gateway's health watch probes it directly);
- ``POST /api/<route>/v1/<op>`` answers the contract, and only to a request the
  gateway signed: the replay-proof v2 proxy signature (``X-KiroCrew-Proxy-V2`` with
  ``X-KiroCrew-Proxy-Nonce``) over the per-app secret in ``KIROCREW_PROXY_SECRET``.

Two providers share the items: ``/memory-provider`` reads and writes, and
``/memory-provider-ro`` only reads. Items are kept per ``scope.store_id``, so two
Bots never see each other's memory. ``MEMORY_FIXTURE_FILE`` names the JSON file
(default: ``data/memory.json`` beside this folder).

Run by the gateway as the app's backend (``PORT`` names the port), or in a test
through :func:`make_server` on an ephemeral port.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Any

API_VERSION = 1
PAGE = 50
_SKEW = 60
KINDS = ("preference", "fact", "episode", "summary", "rule", "finding", "index", "note")


class Store:
    """Items per store id, idempotency keys, and the file they live in."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data: dict[str, Any] = {"stores": {}, "keys": {}, "next": 1, "calls": []}
        if path.is_file():
            self.data = json.loads(path.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data), encoding="utf-8")
        os.replace(tmp, self.path)

    def items(self, store_id: str) -> dict[str, dict]:
        return self.data["stores"].setdefault(store_id, {})


class Provider:
    def __init__(self, store: Store, *, read_only: bool) -> None:
        self.store = store
        self.read_only = read_only

    def describe(self, _body: dict) -> dict:
        return {
            "api_version": API_VERSION,
            "read_only": self.read_only,
            "supports_search": True,
            "supports_vectors": False,
            "supports_lessons": not self.read_only,
            "supports_list": True,
            "supports_delete": not self.read_only,
            "ingest": "none" if self.read_only else "raw",
            "multi_store": True,
            "egress": "local",
            "kinds": list(KINDS),
            "max_page": PAGE,
        }

    def health(self, _body: dict) -> dict:
        return {"ok": True, "detail": "fixture"}

    def _active(self, body: dict) -> list[dict]:
        store_id = body["scope"]["store_id"]
        with self.store.lock:
            return [i for i in self.store.items(store_id).values() if i.get("status") != "retired"]

    def context(self, body: dict) -> dict:
        budget = int(body.get("budget_chars") or 0)
        order = {"rule": 0, "preference": 1, "fact": 2, "finding": 3, "index": 4}
        items = sorted(self._active(body), key=lambda i: order.get(i["kind"], 9))
        out, used = [], 0
        for item in items:
            if budget and used + len(item["text"]) > budget:
                continue
            out.append(item)
            used += len(item["text"])
        return {"items": out[:200]}

    def search(self, body: dict) -> dict:
        needle = str(body.get("query", "")).casefold()
        words = [w for w in needle.split() if len(w) > 2]
        kinds = set(body.get("kinds") or KINDS)
        hits = []
        for item in self._active(body):
            text = item["text"].casefold()
            if item["kind"] in kinds and (needle in text or any(w in text for w in words)):
                hits.append({**item, "score": 0.9})
        return {"items": hits[: int(body.get("limit") or 20)]}

    def get(self, body: dict) -> dict:
        with self.store.lock:
            item = self.store.items(body["scope"]["store_id"]).get(body["id"])
        return {"item": item}

    def list(self, body: dict) -> dict:
        offset = int(body.get("cursor") or 0)
        kinds = set(body.get("kinds") or KINDS)
        rows = [i for i in self._active(body) if i["kind"] in kinds]
        page = rows[offset : offset + PAGE]
        more = offset + PAGE < len(rows)
        return {"items": page, "cursor": str(offset + PAGE) if more else None}

    def put(self, body: dict) -> dict:
        if self.read_only:
            raise Refusal("read_only", "this provider only reads")
        scope = body["scope"]
        key = f"{scope['store_id']}\0{body['idempotency_key']}"
        new = body["item"]
        with self.store.lock:
            items = self.store.items(scope["store_id"])
            known = self.store.data["keys"].get(key)
            if known and known in items and not body.get("id"):
                return {"outcome": "unchanged", "id": known}
            for item_id, item in items.items():
                if (
                    item["text"] == new["text"]
                    and item["kind"] == new["kind"]
                    and item.get("scopes", []) == new.get("scopes", [])
                    and not body.get("id")
                ):
                    self.store.data["keys"][key] = item_id
                    self.store.save()
                    return {"outcome": "deduped", "id": item_id, "winner": item["text"]}
            item_id = body.get("id") or f"m{self.store.data['next']}"
            if not body.get("id"):
                self.store.data["next"] += 1
            existed = item_id in items
            now = time.time()
            items[item_id] = {
                **{
                    k: v
                    for k, v in new.items()
                    if k in ("kind", "text", "title", "scopes", "source", "meta", "status")
                },
                "id": item_id,
                "created_at": items.get(item_id, {}).get("created_at", now),
                "updated_at": now,
                "meta": {**new.get("meta", {}), "written_by_session": scope.get("session_id", "")},
            }
            self.store.data["keys"][key] = item_id
            self.store.save()
        return {"outcome": "enriched" if existed else "inserted", "id": item_id}

    def delete(self, body: dict) -> dict:
        if self.read_only:
            raise Refusal("read_only", "this provider only reads")
        with self.store.lock:
            items = self.store.items(body["scope"]["store_id"])
            gone = items.pop(body["id"], None)
            self.store.save()
        return {"outcome": "deleted" if gone else "unchanged", "id": body["id"]}

    def ingest(self, body: dict) -> dict:
        if self.read_only:
            raise Refusal("read_only", "this provider only reads")
        text = " ".join(
            m.get("text", "") for m in body.get("messages", []) if m.get("role") == "user"
        )
        if not text.strip():
            return {"accepted": True, "items": 0}
        put = self.put(
            {
                "scope": body["scope"],
                "item": {"kind": "summary", "text": text[:2000]},
                "idempotency_key": f"ingest:{body.get('receipt_id', '')}",
            }
        )
        return {"accepted": True, "items": 1 if put["outcome"] == "inserted" else 0}


class Refusal(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Nonces:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.seen: dict[str, float] = {}

    def accept(self, nonce: str, now: float) -> bool:
        with self.lock:
            self.seen = {n: t for n, t in self.seen.items() if t > now}
            if nonce in self.seen:
                return False
            self.seen[nonce] = now + _SKEW
            return True


def signed(
    secret: str, headers: Any, method: str, target: str, body: bytes, nonces: _Nonces
) -> bool:
    """The v2 proxy signature check: ``v2:<ts>:<nonce>:<method>:<target>:<sha256(body)>``."""
    value = headers.get("X-KiroCrew-Proxy-V2", "")
    nonce = headers.get("X-KiroCrew-Proxy-Nonce", "")
    ts, _, signature = value.partition(":")
    if not secret or not nonce or not ts.isdigit() or abs(time.time() - int(ts)) > _SKEW:
        return False
    digest = hashlib.sha256(body).hexdigest()
    message = f"v2:{ts}:{nonce}:{method}:{target}:{digest}"
    expected = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature) and nonces.accept(nonce, time.time())


def make_handler(providers: dict[str, Provider], secret: str) -> type[BaseHTTPRequestHandler]:
    nonces = _Nonces()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            return

        def _send(self, status: int, payload: dict) -> None:
            raw = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            if self.path == "/health":
                self._send(200, {"ok": True})
            else:
                self._send(404, {"ok": False, "error": {"code": "invalid", "message": "not found"}})

        def do_POST(self) -> None:  # noqa: N802 - http.server's name
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not signed(secret, self.headers, "POST", self.path, body, nonces):
                self._send(401, {"ok": False, "error": {"code": "refused", "message": "unsigned"}})
                return
            parts = PurePosixPath(self.path.lstrip("/")).parts
            if len(parts) != 4 or parts[0] != "api" or parts[2] != "v1":
                self._send(404, {"ok": False, "error": {"code": "invalid", "message": "no route"}})
                return
            provider = providers.get(parts[1])
            op = getattr(provider, parts[3], None) if provider else None
            if op is None or parts[3].startswith("_"):
                self._send(
                    200,
                    {"ok": False, "error": {"code": "not_supported", "message": parts[3]}},
                )
                return
            delay = float(os.environ.get("MEMORY_FIXTURE_DELAY_SECS") or 0)
            if delay:
                time.sleep(delay)
            try:
                result = op(json.loads(body or b"{}"))
            except Refusal as exc:
                self._send(200, {"ok": False, "error": {"code": exc.code, "message": exc.message}})
                return
            except (KeyError, TypeError, ValueError) as exc:
                self._send(200, {"ok": False, "error": {"code": "invalid", "message": str(exc)}})
                return
            self._send(200, {"ok": True, "result": result})

    return Handler


class _LoopbackServer(ThreadingHTTPServer):
    """Binds without ``HTTPServer.server_bind``'s ``socket.getfqdn``, which can stall on macOS."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]


def make_server(port: int, secret: str, data_path: Path) -> ThreadingHTTPServer:
    store = Store(data_path)
    providers = {
        "memory-provider": Provider(store, read_only=False),
        "memory-provider-ro": Provider(store, read_only=True),
    }
    return _LoopbackServer(("127.0.0.1", port), make_handler(providers, secret))


def main() -> None:
    default = Path(__file__).resolve().parent.parent / "data" / "memory.json"
    path = Path(os.environ.get("MEMORY_FIXTURE_FILE") or default)
    server = make_server(int(os.environ["PORT"]), os.environ.get("KIROCREW_PROXY_SECRET", ""), path)
    server.serve_forever()


if __name__ == "__main__":
    main()
