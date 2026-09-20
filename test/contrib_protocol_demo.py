#!/usr/bin/env python3
"""A complete out-of-process contributor, in standard-library Python only.

Proves the contribution protocol end to end against a running gateway. It is a
DEMO and a manual verification tool, not a pytest module -- the automated
coverage is ``test/test_contribution_protocol.py``; this is what you run against
a pod to see a contributed card appear in the Members drawer.

What it does, in the order the protocol prescribes:

1. exchanges the app's ``.app_secret`` for an app-scoped token;
2. catches up with ``GET /api/eventlog/member/<slug>/events?after=`` from its
   last folded seq;
3. subscribes over the WebSocket and folds the ``eventlog_event`` stream,
   checking ``seq == last + 1`` and re-reading on a gap -- never folding across
   one;
4. appends ``<app>/ping`` events;
5. publishes the folded ``<app>/count`` view plus its render schema.

Every type and key it writes is prefixed with the app's own name, which is the
protocol's §2 rule: a contributor may only name its own namespace.

Usage::

    python test/contrib_protocol_demo.py --base http://127.0.0.1:PORT \\
        --app demoapp --slug code-reviewer --pings 3

    # then, to prove the gap rule: run it again. It resumes from the seq it
    # stored in --state and reports how many events it caught up on.
    python test/contrib_protocol_demo.py ... --state /tmp/demo.json

Exit code 0 means every step succeeded; anything else prints what failed.

The WebSocket client is hand-rolled (RFC 6455 text frames, client-masked) so the
script needs no third-party package -- the point of the protocol is that a
contributor can be written in anything, and a demo that needs `websockets`
installed proves less.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import socket
import ssl
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlparse


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Gateway:
    def __init__(self, base: str, app: str, token: str) -> None:
        self.base = base.rstrip("/")
        self.app = app
        self.token = token

    @classmethod
    def connect(cls, base: str, app: str, secret: str) -> "Gateway":
        """Exchange the app secret for an app-scoped token."""
        body = cls._request(
            f"{base.rstrip('/')}/api/apps/{app}/token",
            method="POST",
            headers={"X-App-Secret": secret},
            payload=b"",
        )
        token = body.get("token") or body.get("access_token") or ""
        if not token:
            raise SystemExit(f"token exchange returned no token: {body}")
        return cls(base, app, token)

    def _headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json"}

    def _url(self, path: str, query: str = "") -> str:
        """A URL carrying the app token the way the gateway reads it.

        The dashboard's auth middleware takes the token from ``?token=`` or from
        its session cookie -- NOT from an ``Authorization`` header -- so a
        contributor appends it to the query string. The token is app-scoped, so it
        grants only what the app's manifest declares.
        """
        sep = "&" if query else ""
        return f"{self.base}{path}?{query}{sep}token={self.token}"

    @staticmethod
    def _request(url: str, *, method: str, headers: dict[str, str], payload: bytes | None):
        # The reference client only ever talks to the gateway over http(s);
        # reject any other scheme so a crafted base cannot reach file:// or a
        # local scheme handler (the dynamic-urllib audit surface).
        if urlparse(url).scheme not in ("http", "https"):
            raise SystemExit(f"refusing a non-http(s) url: {url!r}")
        req = urllib.request.Request(url, data=payload, method=method)
        for k, v in headers.items():
            req.add_header(k, v)
        try:
            # Scheme is guarded to http(s) above, closing the file:// surface
            # this urllib audit rule warns about.
            # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            res_ctx = urllib.request.urlopen(req, timeout=20)
            with res_ctx as res:
                raw = res.read()
                if res.status == 204 or not raw:
                    return {"_status": res.status}
                out = json.loads(raw)
                if isinstance(out, dict):
                    out["_status"] = res.status
                return out
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                out = json.loads(raw)
            except ValueError:
                out = {"error": raw.decode("utf-8", "replace")}
            out["_status"] = exc.code
            return out

    def get_events(self, kind: str, unit: str, after: int, limit: int = 200) -> dict:
        url = self._url(f"/api/eventlog/{kind}/{unit}/events", f"after={after}&limit={limit}")
        return self._request(url, method="GET", headers=self._headers(), payload=None)

    def append(self, kind: str, unit: str, type_: str, data: dict) -> dict:
        url = self._url(f"/api/eventlog/{kind}/{unit}/events")
        payload = json.dumps({"type": type_, "data": data}).encode()
        return self._request(url, method="POST", headers=self._headers(), payload=payload)

    def publish(self, kind: str, unit: str, key: str, value, seq: int, state_version: int) -> dict:
        url = self._url(f"/api/eventlog/{kind}/{unit}/projections/{quote(key, safe='')}")
        payload = json.dumps({"value": value, "seq": seq, "stateVersion": state_version}).encode()
        return self._request(url, method="POST", headers=self._headers(), payload=payload)

    def put_schema(self, kind: str, unit: str, key: str, schema: dict) -> dict:
        url = self._url(f"/api/eventlog/{kind}/{unit}/projections/{quote(key, safe='')}/schema")
        return self._request(
            url, method="POST", headers=self._headers(), payload=json.dumps(schema).encode()
        )


# ---------------------------------------------------------------------------
# A minimal RFC 6455 client: text frames, client-masked, no extensions
# ---------------------------------------------------------------------------
class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, base: str, token: str) -> "WebSocket":
        url = urlparse(base)
        host = url.hostname or "127.0.0.1"
        port = url.port or (443 if url.scheme == "https" else 80)
        raw = socket.create_connection((host, port), timeout=20)
        if url.scheme == "https":
            # A default context still admits TLSv1 and TLSv1.1 on this
            # interpreter, which CodeQL flags. Pin the floor at TLS 1.2: this
            # client only ever talks to a local gateway, which speaks it.
            ctx = ssl.create_default_context()
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            raw = ctx.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        handshake = (
            # The token rides the query string: the auth middleware reads
            # ``?token=`` or its own cookie, and a contributor has no cookie.
            f"GET /api/ws?token={token} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n"
            # The gateway refuses a cross-origin upgrade, so present its own.
            f"Origin: {url.scheme}://{host}:{port}\r\n"
            f"\r\n"
        )
        raw.sendall(handshake.encode())
        ws = cls(raw)
        head = ws._read_until(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise SystemExit(f"websocket upgrade refused: {head.split(chr(13).encode())[0]!r}")
        return ws

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise SystemExit("connection closed during handshake")
            self._buf += chunk
        head, _, rest = self._buf.partition(marker)
        self._buf = rest
        return head + marker

    def _recv_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send_json(self, obj: dict) -> None:
        payload = json.dumps(obj).encode()
        header = bytearray([0x81])  # FIN + text
        mask = secrets.token_bytes(4)
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self, timeout: float = 10.0) -> dict | None:
        """One text frame as a dict, or None on timeout / close."""
        self.sock.settimeout(timeout)
        try:
            b0, b1 = self._recv_exact(2)
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            return None
        opcode = b0 & 0x0F
        length = b1 & 0x7F
        try:
            if length == 126:
                length = struct.unpack("!H", self._recv_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._recv_exact(8))[0]
            payload = self._recv_exact(length) if length else b""
        except (ConnectionError, TimeoutError, socket.timeout, OSError):
            return None
        if opcode == 0x8:  # close
            return None
        if opcode != 0x1:  # ignore ping/pong/binary
            return {}
        try:
            return json.loads(payload)
        except ValueError:
            return {}

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# The contributor's own fold
# ---------------------------------------------------------------------------
#: Bumped when the fold below changes shape. A higher value re-publishes from
#: zero regardless of seq (protocol §5), which is the whole point of the field.
STATE_VERSION = 1


def fold(state: dict, event: dict, ping_type: str) -> dict:
    """Count this app's ping events. The gateway never runs this."""
    if event.get("type") == ping_type:
        state = dict(state)
        state["pings"] = state.get("pings", 0) + 1
        state["last"] = event.get("data", {}).get("note", "")
    return state


def view(state: dict) -> dict:
    return {"pings": state.get("pings", 0), "last": state.get("last", "")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--base", required=True, help="gateway base URL, e.g. http://127.0.0.1:5479")
    ap.add_argument("--app", default="demoapp")
    ap.add_argument("--kind", default="member")
    ap.add_argument("--slug", required=True, help="the unit id (a member slug)")
    ap.add_argument("--secret", default="", help="app secret; default reads --secret-file")
    ap.add_argument(
        "--secret-file",
        default="",
        help="path to the app's .app_secret (default ~/.kiro/crew/apps/<app>/.app_secret)",
    )
    ap.add_argument("--pings", type=int, default=3, help="how many <app>/ping events to append")
    ap.add_argument("--state", default="", help="where to persist the folded seq between runs")
    ap.add_argument("--no-subscribe", action="store_true", help="skip the WebSocket half")
    args = ap.parse_args()

    secret = args.secret
    if not secret:
        path = (
            Path(args.secret_file)
            if args.secret_file
            else (
                Path(os.environ.get("KIROCREW_HOME", Path.home() / ".kiro" / "crew"))
                / "apps"
                / args.app
                / ".app_secret"
            )
        )
        if not path.exists():
            print(f"FAIL: no app secret at {path}", file=sys.stderr)
            return 2
        secret = path.read_text(encoding="utf-8").strip()

    gw = Gateway.connect(args.base, args.app, secret)
    ping_type = f"{args.app}/ping"
    count_key = f"{args.app}/count"
    print(f"[1/6] token exchanged for app {args.app!r}")

    # Resume from the seq this contributor last folded, if it has one.
    state_path = Path(args.state) if args.state else None
    folded_seq = -1
    state: dict = {}
    if state_path and state_path.exists():
        stored = json.loads(state_path.read_text(encoding="utf-8"))
        folded_seq = int(stored.get("seq", -1))
        state = stored.get("state", {})
        print(f"      resuming from seq {folded_seq} with {state.get('pings', 0)} ping(s) folded")

    # ---- catch up (§3) ----------------------------------------------------
    caught_up = 0
    while True:
        page = gw.get_events(args.kind, args.slug, folded_seq)
        if page.get("_status") != 200:
            print(f"FAIL: catch-up read: {page}", file=sys.stderr)
            return 3
        events = page.get("events", [])
        if not events:
            break
        for event in events:
            if event["seq"] != folded_seq + 1:
                print(
                    f"FAIL: gap at seq {event['seq']} (folded {folded_seq}) INSIDE a catch-up page",
                    file=sys.stderr,
                )
                return 3
            state = fold(state, event, ping_type)
            folded_seq = event["seq"]
            caught_up += 1
        if len(events) < 200:
            break
    print(
        f"[2/6] caught up {caught_up} event(s) to seq {folded_seq}; lastSeq={page.get('lastSeq')}"
    )

    # ---- subscribe (§3) ---------------------------------------------------
    ws = None
    if not args.no_subscribe:
        ws = WebSocket.connect(args.base, gw.token)
        ws.send_json({"type": "eventlog_subscribe", "data": {"kind": args.kind, "id": args.slug}})
        subscribed = None
        for _ in range(50):
            frame = ws.recv_json()
            if frame is None:
                break
            if frame.get("type") == "eventlog_subscribed":
                subscribed = frame["data"]
                break
        if subscribed is None:
            print("FAIL: no eventlog_subscribed frame", file=sys.stderr)
            ws.close()
            return 4
        if "lastSeq" not in subscribed:
            print(f"FAIL: subscribe refused: {subscribed}", file=sys.stderr)
            ws.close()
            return 4
        print(f"[3/6] subscribed; server lastSeq={subscribed['lastSeq']}")

    # ---- append (§4) ------------------------------------------------------
    for i in range(args.pings):
        res = gw.append(args.kind, args.slug, ping_type, {"note": f"ping {i + 1}"})
        if res.get("_status") != 201:
            print(f"FAIL: append: {res}", file=sys.stderr)
            if ws:
                ws.close()
            return 5
    print(f"[4/6] appended {args.pings} {ping_type} event(s)")

    # ---- fold the stream, checking contiguity (§3) -------------------------
    if ws is not None:
        streamed = 0
        while streamed < args.pings:
            frame = ws.recv_json(timeout=10)
            if frame is None:
                print("FAIL: socket closed before every appended event arrived", file=sys.stderr)
                ws.close()
                return 6
            if frame.get("type") != "eventlog_event":
                continue
            event = frame["data"]["event"]
            if event["seq"] != folded_seq + 1:
                # The contract's own recovery path: drop the fold, re-read.
                print(f"      gap at seq {event['seq']} (folded {folded_seq}); re-reading")
                page = gw.get_events(args.kind, args.slug, folded_seq)
                for e in page.get("events", []):
                    state = fold(state, e, ping_type)
                    folded_seq = e["seq"]
                streamed = args.pings
                break
            state = fold(state, event, ping_type)
            folded_seq = event["seq"]
            streamed += 1
        ws.send_json({"type": "eventlog_unsubscribe", "data": {"kind": args.kind, "id": args.slug}})
        ws.close()
        print(f"[5/6] folded {streamed} streamed event(s); at seq {folded_seq}")
    else:
        page = gw.get_events(args.kind, args.slug, folded_seq)
        for e in page.get("events", []):
            state = fold(state, e, ping_type)
            folded_seq = e["seq"]
        print(f"[5/6] folded by catch-up; at seq {folded_seq}")

    # ---- publish (§5) + schema (§7) ---------------------------------------
    schema = gw.put_schema(
        args.kind,
        args.slug,
        count_key,
        {"kind": "keyvalue", "title": "Demo contributor", "path": ["pings", "last"]},
    )
    if schema.get("_status") != 204:
        print(f"FAIL: schema publish: {schema}", file=sys.stderr)
        return 7
    published = gw.publish(args.kind, args.slug, count_key, view(state), folded_seq, STATE_VERSION)
    if published.get("_status") != 204:
        print(f"FAIL: publish: {published}", file=sys.stderr)
        return 7
    print(f"[6/6] published {count_key} = {view(state)} at seq {folded_seq}")

    if state_path:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps({"seq": folded_seq, "state": state}), encoding="utf-8")
        print(f"      state saved to {state_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
