"""Guest-side git HTTP proxy: git in the workspace, credentials on the gateway.

Started by ``worker.py`` inside the guest, so its stdio is the authenticated,
integrity-checked ``wire`` stream to the gateway. It listens on guest loopback
for plain-HTTP git requests, frames each one onto stdout, and plays back the
gateway's framed answer. Commands the agent runs reach it through git's
``url.<base>.insteadOf`` rewrite (see ``workspace_tools.git_proxy_env``), so an
ordinary ``git clone https://github.com/OWNER/REPO`` arrives here as
``GET /github.com/OWNER/REPO/info/refs?service=git-upload-pack``.

This process holds no upstream credential: the gateway checks the
request, adds authentication to the upstream call itself, and returns only the
response. A launch-scoped capability authenticates HTTP callers before any
request is framed. Guest processes sharing an OS identity are not sandboxed
from each other's environment or files by this capability.

Frames are JSON lines, one object each, ``id`` naming the request:

* guest -> gateway: ``req`` {method, path, headers}, ``body`` {b}, ``end``, ``cancel``
* gateway -> guest: ``resp`` {status, headers}, ``body`` {b}, ``end``, or
  ``err`` {status, message} in place of a response

``b`` is base64. Stdlib only and Python 3.9 compatible.
"""

from __future__ import annotations

import base64
import contextlib
import hmac
import http.server
import json
import os
import queue
import re
import socketserver
import sys
import threading
from typing import Callable, Dict, List, Optional

#: Raw bytes per body frame: base64 grows it by a third, and the result stays
#: well inside one ``wire`` data line.
CHUNK = 24 * 1024
#: Request headers git sends that the gateway may use. Everything else stays here.
FORWARD_HEADERS = ("content-type", "accept", "content-encoding", "git-protocol", "user-agent")
#: Frames buffered per request before the reader thread waits for git to drain.
QUEUE_FRAMES = 512
MAX_LIVE_REQUESTS = 16
#: Body frames one request may have in flight before the gateway grants more
#: with a ``credit`` frame per frame it takes (``git_gateway.UPLOAD_WINDOW``).
#: The gateway's reader then never waits on a request, so one stalled upload
#: cannot hold back another request's frames or this one's ``cancel``.
UPLOAD_WINDOW = 64
#: How often a request waiting for credit checks that its git client is still there.
CREDIT_POLL_SECS = 1.0
#: How long a request waits for the gateway's next frame. A little past the
#: gateway's own 30-minute upstream timeout, so its error arrives first.
ANSWER_TIMEOUT = 35 * 60.0


def log(text: str) -> None:
    print(f"crew-git-proxy: {text}", file=sys.stderr, flush=True)


class Answered(Exception):
    """The gateway answered a request (a response or an error) before its body
    was all sent, and reads no more of that body."""


class _Upload:
    """One request's upload credit: frames it may still send, and whether the
    gateway already answered it."""

    def __init__(self) -> None:
        self.credit = threading.Semaphore(UPLOAD_WINDOW)
        self.answered = threading.Event()


class Gateway:
    """The framed stdio channel, shared by every request thread."""

    def __init__(self, out=None, inp=None) -> None:
        self._out = out or sys.stdout
        self._in = inp or sys.stdin
        self._lock = threading.Lock()
        self._next = 0
        self._queues: Dict[int, "queue.Queue[dict]"] = {}
        self._credits: Dict[int, "_Upload"] = {}
        self._qlock = threading.Lock()
        self.closed = threading.Event()
        #: Called with the host list from each ``lend`` frame and whether the
        #: gateway is still looking up other hosts (see serve()).
        self.on_lend: Optional[Callable[[List[str], bool], None]] = None

    def open(self) -> "tuple[int, queue.Queue[dict]]":
        with self._qlock:
            if len(self._queues) >= MAX_LIVE_REQUESTS:
                raise OverflowError("too many live git requests")
            self._next += 1
            rid = self._next
            q: "queue.Queue[dict]" = queue.Queue(QUEUE_FRAMES)
            self._queues[rid] = q
            self._credits[rid] = _Upload()
        return rid, q

    def done(self, rid: int) -> None:
        with self._qlock:
            q = self._queues.pop(rid, None)
            self._credits.pop(rid, None)
        # Release a reader already blocked in put(), even if the HTTP client died.
        if q is not None:
            while True:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break

    def wait_credit(self, rid: int, client_alive: Callable[[], bool]) -> None:
        """Wait until request *rid* may send one more body frame.

        Raises ``Answered`` once the gateway has answered it, with a response or
        an error (it reads no more of this body), and ``ConnectionError`` when the link
        closed or git went away meanwhile, so the caller sends ``cancel``.
        """
        with self._qlock:
            upload = self._credits.get(rid)
        if upload is None:
            raise ConnectionError("the request is closed")
        while True:
            got = upload.credit.acquire(timeout=CREDIT_POLL_SECS)
            if upload.answered.is_set():
                raise Answered()
            if got:
                return
            if self.closed.is_set() or not client_alive():
                raise ConnectionError("git or the gateway link went away")

    def send(self, frame: dict) -> None:
        line = json.dumps(frame, separators=(",", ":")) + "\n"
        with self._lock:
            self._out.write(line)
            self._out.flush()

    def read_forever(self) -> None:
        """Route the gateway's frames to the request that owns each id."""
        try:
            for line in self._in:
                line = line.strip()
                if not line:
                    continue
                try:
                    frame = json.loads(line)
                    rid = int(frame["id"])
                except (ValueError, KeyError, TypeError):
                    log("dropped a malformed frame")
                    continue
                if frame.get("t") == "lend":
                    hosts = frame.get("hosts")
                    if self.on_lend is not None and isinstance(hosts, list):
                        self.on_lend(
                            [h for h in hosts if isinstance(h, str)],
                            frame.get("partial") is True,
                        )
                    continue
                with self._qlock:
                    q = self._queues.get(rid)
                    upload = self._credits.get(rid)
                if frame.get("t") == "credit":
                    if upload is not None:
                        upload.credit.release()
                    continue
                if frame.get("t") in ("resp", "err") and upload is not None:
                    # Answered before the body was all sent (a refused push
                    # answers with resp/body/end): the gateway reads no more.
                    upload.answered.set()
                    upload.credit.release()  # wake a sender waiting for credit
                if q is not None:
                    q.put(frame)
        finally:
            self.closed.set()
            with self._qlock:
                queues = list(self._queues.values())
            for q in queues:
                try:
                    q.put_nowait({"t": "err", "status": 502, "message": "the gateway link closed"})
                except queue.Full:
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        pass
                    q.put_nowait({"t": "err", "status": 502, "message": "the gateway link closed"})


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    gateway: Gateway  # set on the class by serve()
    server: http.server.HTTPServer
    secret: str

    def log_message(self, fmt: str, *args) -> None:  # quiet: stderr is the worker log
        pass

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def _body_chunks(self):
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            while True:
                size_line = self.rfile.readline(1024)
                size = int(size_line.split(b";", 1)[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline(1024) not in (b"\r\n", b"\n", b""):
                        pass
                    return
                left = size
                while left:
                    piece = self.rfile.read(min(CHUNK, left))
                    if not piece:
                        return
                    left -= len(piece)
                    yield piece
                self.rfile.readline(16)
        else:
            left = int(self.headers.get("Content-Length") or 0)
            while left > 0:
                piece = self.rfile.read(min(CHUNK, left))
                if not piece:
                    return
                left -= len(piece)
                yield piece

    def _fail(self, status: int, message: str) -> None:
        body = (message.strip() + "\n").encode("utf-8", "replace")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _client_alive(self) -> bool:
        """Whether git is still connected. A reset discards what git had sent,
        so a peek then fails; a close with nothing pending reads as EOF."""
        import select
        import socket

        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            if not readable:
                return True
            return self.connection.recv(1, socket.MSG_PEEK) != b""
        except (OSError, ValueError):
            return False

    def _proxy(self) -> None:
        expected_host = f"127.0.0.1:{self.server.server_address[1]}"
        presented = self.headers.get_all("Authorization", [])
        if (
            self.headers.get_all("Host", []) != [expected_host]
            or len(presented) != 1
            or not self.secret
            or not hmac.compare_digest(presented[0].encode(), f"Bearer {self.secret}".encode())
        ):
            self._fail(403, "Kiro Crew: git proxy capability required")
            return
        gw = self.gateway
        if gw.closed.is_set():
            self._fail(502, "Kiro Crew: the gateway link is closed")
            return
        try:
            rid, q = gw.open()
        except OverflowError:
            self._fail(503, "Kiro Crew: too many live git requests")
            return
        delivered = False
        try:
            headers = {
                k.lower(): v for k, v in self.headers.items() if k.lower() in FORWARD_HEADERS
            }
            gw.send(
                {
                    "t": "req",
                    "id": rid,
                    "method": self.command,
                    "path": self.path,
                    "headers": headers,
                }
            )
            if self.command == "POST":
                try:
                    for piece in self._body_chunks():
                        gw.wait_credit(rid, self._client_alive)
                        gw.send(
                            {"t": "body", "id": rid, "b": base64.b64encode(piece).decode("ascii")}
                        )
                except Answered:
                    # Answered mid-body (a refused push, an upstream error): the
                    # gateway reads no more of it, so git gets that answer, and
                    # the connection cannot be reused with the rest unread.
                    self.close_connection = True
                    delivered = self._answer(q)
                    return
            gw.send({"t": "end", "id": rid})
            delivered = self._answer(q)
        except (BrokenPipeError, ConnectionError):
            pass
        finally:
            gw.done(rid)
            if not delivered:
                self.close_connection = True
                with contextlib.suppress(BrokenPipeError, ConnectionError):
                    gw.send({"t": "cancel", "id": rid})

    def _answer(self, q: "queue.Queue[dict]") -> bool:
        try:
            first = q.get(timeout=ANSWER_TIMEOUT)
        except queue.Empty:
            self._fail(504, "Kiro Crew: the gateway did not answer")
            return False
        if first.get("t") == "err":
            self._fail(int(first.get("status") or 502), str(first.get("message") or "refused"))
            return True
        if first.get("t") != "resp":
            self._fail(502, "Kiro Crew: unexpected answer from the gateway")
            return False
        self.send_response(int(first.get("status") or 502))
        for key, value in (first.get("headers") or {}).items():
            self.send_header(str(key), str(value))
        # Length unknown up front: stream to git as chunked.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        while True:
            frame = q.get(timeout=ANSWER_TIMEOUT)
            kind = frame.get("t")
            if kind == "body":
                data = base64.b64decode(frame.get("b") or "")
                if data:
                    self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
            elif kind == "end":
                self.wfile.write(b"0\r\n\r\n")
                return True
            else:  # err mid-stream: cut it so git sees a broken response, not a short one
                self.close_connection = True
                return False


class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def server_bind(self) -> None:
        # ``HTTPServer.server_bind`` resolves ``socket.getfqdn(host)`` between
        # bind() and listen(); that resolver lookup can stall for tens of seconds
        # on some hosts (macOS among them) while the port refuses connections,
        # holding back the port file every git command waits for. This proxy is
        # reached by address only, so the name buys nothing.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


def _write_port(port: int, path: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="ascii") as handle:
        handle.write(f"{port}\n")
    os.replace(tmp, path)


def _clear_port(port: int, path: str) -> None:
    # Only our own: a newer proxy for the same workspace may have replaced it.
    try:
        with open(path, encoding="ascii") as handle:
            if handle.read().strip() == str(port):
                os.unlink(path)
    except OSError:
        pass


def lend_file(port_file: str) -> str:
    """Where the hosts the gateway can lend for are kept, beside the port file."""
    return port_file + ".lend"


def _write_lend(port: int, hosts: List[str], path: str, partial: bool = False) -> None:
    # "<port> <host>...": the port ties the list to this proxy, so a file left
    # by another proxy never routes a host here. A "*" after the port (never a
    # host name) marks a list the gateway is still adding to.
    names = [h for h in hosts if re.fullmatch(r"[a-z0-9.-]{1,253}", h)]
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="ascii") as handle:
        handle.write(" ".join([str(port), *(["*"] if partial else []), *names]) + "\n")
    os.replace(tmp, path)


def _clear_lend(port: int, path: str) -> None:
    try:
        with open(path, encoding="ascii") as handle:
            if handle.read().split()[:1] == [str(port)]:
                os.unlink(path)
    except OSError:
        pass


def _exit_on_term(signum, frame) -> None:
    raise SystemExit(0)  # runs serve()'s finally, so the port file goes too


def serve(gateway: Optional[Gateway] = None, *, port_file: str, secret: str) -> int:
    import signal

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, _exit_on_term)
    gw = gateway or Gateway()
    handler = type("BoundHandler", (Handler,), {"gateway": gw, "secret": secret})
    server = Server(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    lend_path = lend_file(port_file)
    with contextlib.suppress(OSError):
        os.unlink(lend_path)  # until the gateway says otherwise, route nothing here
    gw.on_lend = lambda hosts, partial: _write_lend(port, hosts, lend_path, partial)
    _write_port(port, port_file)
    log(f"listening on 127.0.0.1:{port}")
    gw.send({"t": "lend", "id": 0})  # ask which hosts the gateway can lend for
    try:
        gw.read_forever()  # returns when the gateway closes our stdin
    finally:
        _clear_lend(port, lend_path)
        _clear_port(port, port_file)
        server.shutdown()
        server.server_close()
    return 0


def build_digest(path: Optional[str] = None) -> str:
    """The SHA-256 of this file: the gateway passes its own, so a guest and a
    gateway of different builds never speak (their frames may differ)."""
    import hashlib

    with open(path or os.path.abspath(__file__), "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def main(argv: Optional[list] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="git_proxy.py")
    # Required: each proxy gets its own file from the gateway, never a shared default.
    parser.add_argument("--port-file", required=True)
    # Required: the gateway's build of this file. A gateway that passes none, or
    # another build's, is refused before anything is framed, so a protocol
    # change (the upload credit, say) cannot leave either side waiting.
    parser.add_argument("--build", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.build != build_digest():
        raise SystemExit(
            "git_proxy.py in this workspace is from another Kiro Crew build than the "
            "gateway's; restart the session so the gateway installs its own"
        )
    # Resolved here: a relative path names the worker directory this starts in.
    secret = sys.stdin.readline(66).strip()
    if re.fullmatch(r"[0-9a-f]{64}", secret) is None:
        raise SystemExit("invalid git proxy capability")
    return serve(port_file=os.path.abspath(args.port_file), secret=secret)


if __name__ == "__main__":
    sys.exit(main())
