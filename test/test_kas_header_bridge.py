"""KAS projection: a remote MCP server's withheld headers reach it through a local bridge.

On KAS a remote entry's ``headers`` never cross the wire. Without them a server
that authenticates with a static header answers 401 and the engine falls into an
OAuth sign-in the server does not need. The projection writes the url and
headers into an owner-only launch snapshot and hands the engine Crew's launcher,
which serves the engine on stdio and relays each message to the url with the
headers added (``kiro_crew.kas_http_bridge``). The canary must reach the server's
HTTP requests and must appear in nothing that crosses the wire.
"""

from __future__ import annotations

import contextlib
import errno
import http.client
import io
import json
import os
import queue
import socket
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest
from kas_agents_helpers import _spec

from kiro_crew import kas_env_shim
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent
from kiro_crew.kas_connection_bridge import ConnectionBridge
from kiro_crew.kas_http_bridge import Bridge, _same_origin
from kiro_crew.testing.wait import default_timeout, wait_until

CANARY = "Bearer zq7Xk-26232-canary"
SESSION = "dashboard_chat-26232-1"


@pytest.fixture(autouse=True)
def _no_proxy(_floor_monkeypatch):
    """The fake server is on loopback; a host proxy setting must not route to it."""
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        _floor_monkeypatch.delenv(name, raising=False)
    _floor_monkeypatch.delenv("ALL_PROXY", raising=False)


@pytest.fixture
def home(tmp_path, monkeypatch):
    data_home = tmp_path / "crew-home"
    data_home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(data_home))
    return data_home


def _snapshot(home: Path, launched: dict) -> Path:
    names = kas_env_launch.batch_snapshot_names([{"mcpServers": {"s": launched}}])
    assert len(names) == 1
    return home / "kas-mcp-launch" / names[0]


def _wire_text(agent_entry: dict) -> str:
    """Everything the engine receives for the session, as the frame serializes it."""
    agents, array = hoist_servers_for_session([agent_entry], agent_entry["id"], [])
    return json.dumps(build_session_new_params("/w", mcp_servers=array, kas_custom_agents=agents))


@pytest.fixture
def raw_remote():
    """A loopback server that answers one request with the canned bytes it is given."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    sock.settimeout(10)
    answer: list[bytes] = []
    received: list[bytes] = []

    def _serve():
        try:
            conn, _addr = sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(10)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            length = 0
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            received.append(head)
            conn.sendall(answer[0])

    thread = threading.Thread(target=_serve, daemon=True)
    try:
        yield answer, received, thread, f"http://127.0.0.1:{sock.getsockname()[1]}/mcp"
    finally:
        sock.close()
        thread.join(10)


def _run_entry(entry: dict, timeout: float = 60.0) -> subprocess.CompletedProcess:
    """Start *entry* the way the engine does: its command, args and env alone."""
    inherited = ("PATH",) + (
        ("SYSTEMROOT", "SYSTEMDRIVE", "TEMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
        if os.name == "nt"
        else ()
    )
    env = {k: os.environ[k] for k in inherited if k in os.environ}
    env.update({"PYTHONDONTWRITEBYTECODE": "1", **(entry.get("env") or {})})
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    requests = "\n".join(
        json.dumps(m)
        for m in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}},
        )
    )
    return subprocess.run(
        [entry["command"], *entry["args"]],
        input=requests + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=timeout,
        check=False,
    )


def _closed_port() -> int:
    """A loopback port nothing listens on: bound by the OS, then released."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _lower(headers) -> dict[str, str]:
    """Header names are case-insensitive; compare them that way."""
    return {k.lower(): v for k, v in headers.items()}


class _FakeRemote(BaseHTTPRequestHandler):
    """A streamable-HTTP MCP server that answers only with the right header."""

    seen: list[dict]
    sse = False
    tool_result: dict | None = None
    #: The id each ``initialize`` hands out, and the ids the server does not know
    #: (it restarted): a request carrying one is answered 404.
    session_id = "sess-1"
    forgotten: frozenset[str] = frozenset()
    #: When set, a request carrying a forgotten id waits here before its 404, so
    #: concurrent requests meet the stale session together.
    stale_barrier: threading.Barrier | None = None
    hold_request_id: int | None = None
    held_request_gate: tuple[threading.Event, threading.Event] | None = None
    initialized_gate: tuple[threading.Event, threading.Event] | None = None
    refuse_initialize: int | None = None
    refuse_initialize_once = False
    initialize_answer: str | None = None
    drop_initialize = False
    renewal_without_session_id = False
    refuse_initialized: int | None = None
    refuse_tool_call: int | None = None

    def log_message(self, *_a):  # quiet
        pass

    def _reply(self, status: int, body: bytes = b"", ctype: str = "application/json", **hdrs):
        self.send_response(status)
        if body:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in hdrs.items():
            self.send_header(k.replace("_", "-"), v)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        gate = self.initialized_gate
        if (
            gate is not None
            and body.get("method") == "notifications/initialized"
            and self.headers.get("Mcp-Session-Id") == self.session_id
        ):
            gate[0].set()
            if not gate[1].wait(default_timeout()):
                self._reply(500)
                return
        self.seen.append({"path": self.path, "headers": _lower(self.headers), "body": body})
        if self.path == "/moved":
            self._reply(302, Location="/elsewhere")
            return
        if self.path == "/gone":
            self._reply(404)
            return
        if self.headers.get("Authorization") != CANARY:
            self._reply(401, b'{"error":"no token"}')
            return
        if self.headers.get("Mcp-Session-Id") in self.forgotten:
            if self.stale_barrier is not None:
                with contextlib.suppress(threading.BrokenBarrierError):
                    self.stale_barrier.wait()
            self._reply(404)
            return
        held = self.held_request_gate
        if held is not None and body.get("id") == self.hold_request_id:
            held[0].set()
            if not held[1].wait(default_timeout()):
                self._reply(500)
                return
        renewing = (
            body.get("method") == "initialize"
            and not self.headers.get("Mcp-Session-Id")
            and bool(self.forgotten)
        )
        if renewing and self.drop_initialize:
            with contextlib.suppress(OSError):
                self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        if renewing and self.refuse_initialize is not None:
            status = self.refuse_initialize
            if self.refuse_initialize_once:
                type(self).refuse_initialize = None
            self._reply(status)
            return
        if renewing and self.initialize_answer is not None:
            if self.initialize_answer == "error":
                answer = {"error": {"code": -32603, "message": "no"}}
            elif self.initialize_answer == "noversion":
                answer = {"result": {}}
            else:
                answer = {}
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], **answer}).encode()
            self._reply(200, payload, Mcp_Session_Id=self.session_id)
            return
        if (
            self.refuse_initialized is not None
            and body.get("method") == "notifications/initialized"
            and self.headers.get("Mcp-Session-Id") == self.session_id
        ):
            self._reply(self.refuse_initialized)
            return
        if (
            self.refuse_tool_call is not None
            and body.get("method") == "tools/call"
            and self.headers.get("Mcp-Session-Id") == self.session_id
        ):
            self._reply(self.refuse_tool_call)
            return
        if "id" not in body:
            self._reply(202)
            return
        result = (
            {"protocolVersion": "2025-06-18"}
            if body["method"] == "initialize"
            else self.tool_result or {"text": "authorized"}
        )
        payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}).encode()
        response_session_id = self.headers.get("Mcp-Session-Id")
        if response_session_id is None and not (
            self.renewal_without_session_id and bool(self.forgotten)
        ):
            response_session_id = self.session_id
        response_headers = {"Mcp_Session_Id": response_session_id} if response_session_id else {}
        if self.sse:
            self._reply(
                200,
                b"event: message\ndata: " + payload + b"\n\n",
                "text/event-stream",
                **response_headers,
            )
        else:
            self._reply(200, payload, **response_headers)

    def do_GET(self):  # noqa: N802
        # Where a followed 302 lands: urllib turns the POST into a GET and keeps
        # every non-content header, Authorization included.
        self.seen.append({"path": self.path, "headers": _lower(self.headers), "body": None})
        self._reply(200, b"{}")

    def do_DELETE(self):  # noqa: N802
        self.seen.append({"path": self.path, "headers": _lower(self.headers), "body": None})
        self._reply(200)


class _LoopbackServer(ThreadingHTTPServer):
    """Reached by port, never by name: skip the host lookup ``server_bind`` does."""

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = str(host)
        self.server_port = int(port)


@pytest.fixture
def remote():
    handler = type("Handler", (_FakeRemote,), {"seen": []})
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(10)


class _LegacyRemote(BaseHTTPRequestHandler):
    """An MCP server on the older two-endpoint ``sse`` transport that answers only
    with the right header: a POST to ``/sse`` is refused 405, a GET there opens an
    event stream naming its endpoint, and each POST there is answered on that stream.
    """

    seen: list[dict]
    streams: dict[str, queue.Queue]
    endpoint = "/messages?session={sid}"
    unanswered: frozenset[str] = frozenset()

    def log_message(self, *_a):  # quiet
        pass

    def _status(self, status: int) -> None:
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _record(self, body) -> bool:
        self.seen.append({"path": self.path, "headers": _lower(self.headers), "body": body})
        if self.headers.get("Authorization") == CANARY:
            return True
        self._status(401)
        return False

    def do_GET(self):  # noqa: N802
        if not self._record(None):
            return
        sid = str(len(self.streams) + 1)
        answers: queue.Queue = queue.Queue()
        self.streams[sid] = answers
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(f"event: endpoint\ndata: {self.endpoint.format(sid=sid)}\n\n".encode())
        self.wfile.flush()
        try:
            while (msg := answers.get(timeout=default_timeout())) is not None:
                self.wfile.write(b"event: message\ndata: " + json.dumps(msg).encode() + b"\n\n")
                self.wfile.flush()
        except (queue.Empty, OSError):
            pass

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if not self._record(body):
            return
        path, _, query = self.path.partition("?")
        answers = self.streams.get(query.removeprefix("session="))
        if path != "/messages" or answers is None:
            self._status(405 if path == "/sse" else 404)
            return
        self._status(202)
        if "id" in body and body["method"] not in self.unanswered:
            result = (
                {"protocolVersion": "2024-11-05"}
                if body["method"] == "initialize"
                else {"text": "authorized"}
            )
            answers.put({"jsonrpc": "2.0", "id": body["id"], "result": result})


@pytest.fixture
def legacy():
    handler = type("Handler", (_LegacyRemote,), {"seen": [], "streams": {}})
    server = _LoopbackServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        for answers in handler.streams.values():
            answers.put(None)
        server.shutdown()
        server.server_close()
        thread.join(10)


def _project(spec: dict, session_key: str = SESSION) -> dict:
    return to_client_custom_agent("kirocrew", spec, "p", session_key=session_key)


def _remote_spec(url: str, **extra) -> dict:
    return {"url": url, "headers": {"Authorization": CANARY}, **extra}


class TestProjection:
    def test_the_header_reaches_the_server_and_never_the_wire(self, home, remote):
        handler, base = remote
        entry = _project(_spec(mcpServers={"jira": _remote_spec(base + "/mcp")}))
        launched = entry["mcpServers"]["jira"]
        assert CANARY not in _wire_text(entry), "no header value crosses the wire"
        assert "url" not in launched and "headers" not in launched
        assert kas_env_shim.SHIM_MODULE in launched["args"]
        result = _run_entry(launched)
        assert result.returncode == 0, result.stderr
        assert '"authorized"' in result.stdout, "the bridge sent the server its header"
        assert CANARY not in result.stdout + result.stderr
        assert all(r["headers"].get("authorization") == CANARY for r in handler.seen)

    def test_the_snapshot_holds_only_the_url_and_headers_and_is_owner_only(self, home):
        entry = _project(_spec(mcpServers={"jira": _remote_spec("https://x.invalid/mcp")}))
        snapshot = _snapshot(home, entry["mcpServers"]["jira"])
        doc = json.loads(snapshot.read_text(encoding="utf-8"))
        assert doc["url"] == "https://x.invalid/mcp"
        assert doc["headers"] == {"Authorization": CANARY}
        assert set(doc) == {
            kas_env_shim.SNAPSHOT_MARKER,
            "session",
            "agent",
            "server",
            "version",
            "url",
            "headers",
        }
        if os.name == "posix":
            assert snapshot.stat().st_mode & 0o777 == 0o600

    def test_the_timeout_stays_on_the_entry(self, home):
        spec = _spec(mcpServers={"jira": _remote_spec("https://x.invalid/mcp", timeout=9000)})
        assert _project(spec)["mcpServers"]["jira"]["timeout"] == 9000

    def test_an_sse_entry_reaches_its_server_with_the_header(self, home, legacy):
        handler, base = legacy
        entry = _project(_spec(mcpServers={"old": _remote_spec(base + "/sse", type="sse")}))
        launched = entry["mcpServers"]["old"]
        assert CANARY not in _wire_text(entry), "no header value crosses the wire"
        assert "url" not in launched and kas_env_shim.SHIM_MODULE in launched["args"]
        result = _run_entry(launched)
        assert result.returncode == 0, result.stderr
        assert '"authorized"' in result.stdout, "the tool call was answered on the stream"
        assert CANARY not in result.stdout + result.stderr
        assert all(r["headers"].get("authorization") == CANARY for r in handler.seen)

    def test_no_session_key_keeps_the_withholding_projection(self, home):
        entry = _project(_spec(mcpServers={"jira": _remote_spec("https://x.invalid/mcp")}), "")
        assert entry["mcpServers"]["jira"] == {"url": "https://x.invalid/mcp"}
        assert not (home / "kas-mcp-launch").exists()

    def test_a_snapshot_that_cannot_be_written_refuses_to_start(self, home, monkeypatch):
        def _fail(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(kas_env_launch, "atomic_write", _fail)
        entry = _project(_spec(mcpServers={"jira": _remote_spec("https://x.invalid/mcp")}))
        launched = entry["mcpServers"]["jira"]
        assert launched["args"][-1] == "unwritten"
        assert CANARY not in _wire_text(entry)
        result = _run_entry(launched)
        assert result.returncode == 78
        assert "was not started" in result.stderr and CANARY not in result.stderr


def _bridge(url: str, headers: dict | None = None) -> tuple[Bridge, io.BytesIO, io.StringIO]:
    out, err = io.BytesIO(), io.StringIO()
    bridge = Bridge(url, headers or {"Authorization": CANARY}, server="jira", out=out, err=err)
    return bridge, out, err


def _lines(out: io.BytesIO) -> list[dict]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _event_stream(body: bytes) -> io.BytesIO:
    """An open ``text/event-stream`` answer holding *body*, as urllib hands it over."""
    headers = http.client.HTTPMessage()
    headers["Content-Type"] = "text/event-stream"
    stream = io.BytesIO(body)
    stream.headers = headers  # type: ignore[attr-defined]
    return stream


def _msg(**fields) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **fields}).encode() + b"\n"


def _arrived(out: io.BytesIO, count: int) -> list[dict]:
    """The first *count* messages, once the stream's reader thread wrote that many."""
    wait_until(lambda: len(out.getvalue().splitlines()) >= count, describe=out.getvalue)
    return _lines(out)[:count]


class TestBridge:
    @pytest.mark.parametrize("sse", [False, True])
    def test_a_session_carries_its_id_and_protocol_version(self, remote, sse):
        handler, base = remote
        handler.sse = sse
        bridge, out, _err = _bridge(base + "/mcp")
        stdin = io.BytesIO(
            _msg(id=1, method="initialize", params={}) + _msg(method="notifications/initialized")
        )
        bridge.send(stdin.readline())
        bridge.send(stdin.readline())
        bridge.send(_msg(id=2, method="tools/call", params={}))
        bridge.close()
        assert [m["id"] for m in _lines(out)] == [1, 2]
        later = handler.seen[1:]
        assert all(r["headers"].get("mcp-session-id") == "sess-1" for r in later)
        posts = [r for r in later if r["body"] is not None]
        assert all(r["headers"].get("mcp-protocol-version") == "2025-06-18" for r in posts)
        assert handler.seen[-1]["body"] is None, "close ends the server's session"

    @pytest.mark.parametrize("sse", [False, True])
    def test_a_structured_only_result_reaches_the_engine_as_text(self, remote, sse):
        # Airtable's hosted server answers ``content: []`` with the payload only
        # in ``structuredContent``; the engine reads only ``content``.
        handler, base = remote
        handler.sse = sse
        handler.tool_result = {"content": [], "structuredContent": {"bases": ["Ü"]}}
        bridge, out, _err = _bridge(base + "/mcp")
        bridge.send(_msg(id=2, method="tools/call", params={}))
        (reply,) = _lines(out)
        assert reply["result"]["content"] == [{"type": "text", "text": '{"bases":["Ü"]}'}]
        assert reply["result"]["structuredContent"] == {"bases": ["Ü"]}, "kept for outputSchema"

    def test_every_request_names_the_bridge_not_the_python_library(self, remote):
        # Bot filters (Atlassian Rovo's among them) answer urllib's default
        # "Python-urllib/x.y" with 403, which reads as a bad auth header.
        handler, base = remote
        bridge, _out, _err = _bridge(base + "/mcp")
        bridge.send(_msg(id=1, method="initialize", params={}))
        bridge.close()
        agents = [r["headers"].get("user-agent", "") for r in handler.seen]
        assert len(agents) == 2, "the POST and the closing DELETE"
        assert all(a.startswith("kirocrew-mcp-bridge/") for a in agents), agents

    def test_a_configured_user_agent_is_kept(self, remote):
        handler, base = remote
        bridge, _out, _err = _bridge(
            base + "/mcp", {"Authorization": CANARY, "user-agent": "mine/2"}
        )
        bridge.send(_msg(id=1, method="tools/list"))
        assert handler.seen[0]["headers"]["user-agent"] == "mine/2"

    def test_a_rejection_answers_the_request_without_the_header(self, remote):
        _handler, base = remote
        bridge, out, err = _bridge(base + "/mcp", {"Authorization": "Bearer wrong-value"})
        bridge.send(_msg(id=7, method="tools/list"))
        (reply,) = _lines(out)
        assert reply["id"] == 7 and "HTTP 401" in reply["error"]["message"]
        assert "wrong-value" not in out.getvalue().decode() + err.getvalue()

    def test_a_redirect_is_not_followed(self, remote):
        handler, base = remote
        bridge, out, _err = _bridge(base + "/moved")
        bridge.send(_msg(id=3, method="tools/list"))
        assert [r["path"] for r in handler.seen] == ["/moved"], "the header went nowhere else"
        assert "redirect" in _lines(out)[0]["error"]["message"]

    def _initialized(self, base: str) -> tuple[Bridge, io.BytesIO, io.StringIO]:
        """A bridge whose engine has initialized and holds the server's ``sess-1``."""
        bridge, out, err = _bridge(base + "/mcp")
        bridge.send(_msg(id=1, method="initialize", params={"clientInfo": {"name": "engine"}}))
        bridge.send(_msg(method="notifications/initialized"))
        return bridge, out, err

    def test_a_session_the_server_forgot_is_started_again_and_the_request_retried(self, remote):
        # An IDE's built-in server restarts with the IDE and answers 404 to the id
        # it issued before; the transport spec has the client initialize again.
        handler, base = remote
        bridge, out, err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        bridge.send(_msg(id=2, method="tools/call", params={}))
        assert [(m["id"], "result" in m) for m in _lines(out)] == [(1, True), (2, True)]
        posts = [(r["body"]["method"], r["headers"].get("mcp-session-id")) for r in handler.seen]
        assert posts == [
            ("initialize", None),
            ("notifications/initialized", "sess-1"),
            ("tools/call", "sess-1"),
            ("initialize", None),
            ("notifications/initialized", "sess-2"),
            ("tools/call", "sess-2"),
        ]
        assert handler.seen[3]["body"]["params"] == {"clientInfo": {"name": "engine"}}
        assert "sess-" not in out.getvalue().decode() + err.getvalue(), "never a header value"

    @pytest.mark.parametrize("renewal_without_session_id", [False, True])
    def test_a_late_old_session_answer_does_not_replace_the_renewed_session(
        self, remote, renewal_without_session_id
    ):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.renewal_without_session_id = renewal_without_session_id
        entered, release = threading.Event(), threading.Event()
        handler.hold_request_id = 2
        handler.held_request_gate = (entered, release)
        old = threading.Thread(
            target=bridge.send, args=(_msg(id=2, method="tools/call", params={}),)
        )
        old.start()
        assert entered.wait(default_timeout())
        try:
            handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
            bridge.send(_msg(id=3, method="tools/call", params={}))
        finally:
            release.set()
        old.join(default_timeout())
        assert not old.is_alive()

        bridge.send(_msg(id=4, method="tools/call", params={}))

        assert sorted(m["id"] for m in _lines(out) if "result" in m) == [1, 2, 3, 4]
        assert sum(r["body"]["method"] == "initialize" for r in handler.seen) == 2
        calls = [
            (r["body"]["id"], r["headers"].get("mcp-session-id"))
            for r in handler.seen
            if r["body"]["method"] == "tools/call"
        ]
        renewed = None if renewal_without_session_id else "sess-2"
        assert calls == [(2, "sess-1"), (3, "sess-1"), (3, renewed), (4, renewed)]

    def test_a_renewal_without_a_protocol_version_omits_the_protocol_header(self, remote):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        handler.initialize_answer = "noversion"

        bridge.send(_msg(id=2, method="tools/call", params={}))

        assert _lines(out)[1]["result"] == {"text": "authorized"}
        fresh = [r for r in handler.seen if r["headers"].get("mcp-session-id") == "sess-2"]
        assert [r["body"]["method"] for r in fresh] == [
            "notifications/initialized",
            "tools/call",
        ]
        assert all("mcp-protocol-version" not in r["headers"] for r in fresh)

    def test_a_forgotten_session_without_a_recorded_initialize_is_reported(self, remote):
        handler, base = remote
        bridge, out, err = _bridge(base + "/mcp")
        bridge.send(_msg(id=1, method="tools/list"))
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"

        bridge.send(_msg(id=2, method="tools/call", params={}))
        bridge.send(_msg(id=3, method="tools/call", params={}))

        replies = _lines(out)
        assert [reply["id"] for reply in replies] == [1, 2, 3]
        assert all("HTTP 404" in reply["error"]["message"] for reply in replies[1:])
        assert [r["body"]["method"] for r in handler.seen] == [
            "tools/list",
            "tools/call",
            "tools/call",
        ]
        assert "sess-" not in out.getvalue().decode() + err.getvalue(), "never a header value"

    @pytest.mark.parametrize("status", [404, 503])
    def test_a_retried_request_failure_is_reported_without_a_second_try(self, remote, status):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        handler.refuse_tool_call = status
        bridge.send(_msg(id=2, method="tools/call", params={}))
        reply = _lines(out)[1]
        assert reply["id"] == 2 and f"HTTP {status}" in reply["error"]["message"]
        methods = [r["body"]["method"] for r in handler.seen]
        assert methods == [
            "initialize",
            "notifications/initialized",
            "tools/call",
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        handler.refuse_tool_call = None
        if status == 404:
            handler.forgotten |= frozenset({"sess-2"})
            handler.session_id = "sess-3"
        bridge.send(_msg(id=3, method="tools/call", params={}))
        assert _lines(out)[2]["result"] == {"text": "authorized"}
        expected_initializes = 3 if status == 404 else 2
        assert (
            sum(r["body"]["method"] == "initialize" for r in handler.seen) == expected_initializes
        )

    def test_requests_meeting_a_forgotten_session_together_share_one_new_one(self, remote):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        stale_barrier = threading.Barrier(2, timeout=default_timeout())
        handler.stale_barrier = stale_barrier
        calls = [
            threading.Thread(target=bridge.send, args=(_msg(id=i, method="tools/call", params={}),))
            for i in (2, 3)
        ]
        for call in calls:
            call.start()
        for call in calls:
            call.join(default_timeout())
            assert not call.is_alive()
        assert not stale_barrier.broken
        assert sorted(m["id"] for m in _lines(out) if "result" in m) == [1, 2, 3]
        methods = [r["body"]["method"] for r in handler.seen]
        assert methods.count("initialize") == 2, "one new session for both"
        tool_calls = [
            r["headers"].get("mcp-session-id")
            for r in handler.seen
            if r["body"]["method"] == "tools/call"
        ]
        assert tool_calls == ["sess-1", "sess-1", "sess-2", "sess-2"]

    @pytest.mark.parametrize(
        ("failure", "reason"),
        [
            (404, "HTTP 404"),
            (503, "HTTP 503"),
            ("error", "refused to start a new session"),
            ("missing", "refused to start a new session"),
            ("network", "could not be reached"),
        ],
    )
    def test_a_failed_renewal_keeps_the_stale_session(self, remote, failure, reason):
        handler, base = remote
        bridge, out, err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        if isinstance(failure, int):
            handler.refuse_initialize = failure
        elif failure == "network":
            handler.drop_initialize = True
        else:
            handler.initialize_answer = failure

        bridge.send(_msg(id=2, method="tools/call", params={}))

        failed = _lines(out)[1]
        assert failed["id"] == 2 and reason in failed["error"]["message"]
        assert not any(
            r["body"]["method"] == "tools/call" and r["headers"].get("mcp-session-id") == "sess-2"
            for r in handler.seen
        )
        assert "sess-" not in out.getvalue().decode() + err.getvalue(), "never a header value"
        handler.refuse_initialize = None
        handler.drop_initialize = False
        handler.initialize_answer = None
        bridge.send(_msg(id=3, method="tools/call", params={}))
        assert _lines(out)[2]["result"] == {"text": "authorized"}
        assert sum(r["body"]["method"] == "initialize" for r in handler.seen) == 3

    def test_a_renewal_event_stream_past_its_deadline_is_given_up(self):
        from kiro_crew.kas_http_bridge import _REPEAT_ID, _answer_to

        answer = (
            b": keep-alive\n\n" * 3
            + b'event: message\ndata: {"jsonrpc":"2.0","id":"'
            + _REPEAT_ID.encode()
            + b'","result":{"protocolVersion":"2025-06-18"}}\n\n'
        )
        expired = _event_stream(answer)
        assert _answer_to(_REPEAT_ID, expired, deadline=0) is None
        assert expired.tell() == len(b": keep-alive\n")
        assert _answer_to(_REPEAT_ID, _event_stream(answer)) == {"protocolVersion": "2025-06-18"}

    @pytest.mark.parametrize("first_failure_only", [True, False])
    def test_a_waiter_renews_after_its_predecessor_fails(self, remote, first_failure_only):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        stale_barrier = threading.Barrier(2, timeout=default_timeout())
        handler.stale_barrier = stale_barrier
        handler.refuse_initialize = 503
        handler.refuse_initialize_once = first_failure_only
        calls = [
            threading.Thread(target=bridge.send, args=(_msg(id=i, method="tools/call", params={}),))
            for i in (2, 3)
        ]
        for call in calls:
            call.start()
        for call in calls:
            call.join(default_timeout())
            assert not call.is_alive()
        assert not stale_barrier.broken
        handler.stale_barrier = None

        replies = [reply for reply in _lines(out) if reply["id"] in (2, 3)]
        errors = [reply for reply in replies if "error" in reply]
        results = [reply for reply in replies if "result" in reply]
        assert len(errors) == (1 if first_failure_only else 2)
        assert len(results) == (1 if first_failure_only else 0)
        assert all("HTTP 503" in reply["error"]["message"] for reply in errors)

        handler.refuse_initialize = None
        bridge.send(_msg(id=4, method="tools/call", params={}))
        assert next(reply for reply in _lines(out) if reply["id"] == 4)["result"] == {
            "text": "authorized"
        }
        expected_initializes = 3 if first_failure_only else 4
        assert (
            sum(r["body"]["method"] == "initialize" for r in handler.seen) == expected_initializes
        )

    def test_a_renewed_session_is_not_published_until_initialized(self, remote):
        handler, base = remote
        bridge, out, _err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        entered, release = threading.Event(), threading.Event()
        handler.initialized_gate = (entered, release)
        renewing = threading.Thread(
            target=bridge.send, args=(_msg(id=2, method="tools/call", params={}),)
        )
        renewing.start()
        assert entered.wait(default_timeout())
        concurrent = threading.Thread(
            target=bridge.send, args=(_msg(id=3, method="tools/call", params={}),)
        )
        concurrent.start()
        try:
            wait_until(
                lambda: any(r["body"].get("id") == 3 for r in handler.seen),
                describe=lambda: handler.seen,
            )
        finally:
            release.set()
        for call in (renewing, concurrent):
            call.join(default_timeout())
            assert not call.is_alive()
        fresh = [r for r in handler.seen if r["headers"].get("mcp-session-id") == "sess-2"]
        assert fresh[0]["body"]["method"] == "notifications/initialized"
        assert sorted(m["id"] for m in _lines(out) if "result" in m) == [1, 2, 3]

    @pytest.mark.parametrize("status", [404, 503])
    def test_a_failed_renewal_notification_keeps_the_stale_session(self, remote, status):
        handler, base = remote
        bridge, out, err = self._initialized(base)
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        handler.refuse_initialized = status

        bridge.send(_msg(id=2, method="tools/call", params={}))

        failed = _lines(out)[1]
        assert failed["id"] == 2 and f"HTTP {status}" in failed["error"]["message"]
        assert not any(
            r["body"]["method"] == "tools/call" and r["headers"].get("mcp-session-id") == "sess-2"
            for r in handler.seen
        )
        assert "sess-" not in out.getvalue().decode() + err.getvalue(), "never a header value"
        handler.refuse_initialized = None
        bridge.send(_msg(id=3, method="tools/call", params={}))
        assert _lines(out)[2]["result"] == {"text": "authorized"}
        assert sum(r["body"]["method"] == "initialize" for r in handler.seen) == 3

    def test_a_404_with_no_session_is_reported_as_before(self, remote):
        handler, base = remote
        bridge, out, _err = _bridge(base + "/gone")
        bridge.send(_msg(id=3, method="tools/list"))
        assert "HTTP 404" in _lines(out)[0]["error"]["message"]
        assert len(handler.seen) == 1, "nothing to initialize again"

    def test_an_initialize_404_falls_back_without_renewing(self, remote):
        handler, base = remote
        bridge, out, err = _bridge(base + "/gone")
        bridge.send(_msg(id=1, method="initialize", params={}))
        assert "event stream" in _lines(out)[0]["error"]["message"]
        assert [(r["path"], (r["body"] or {}).get("method")) for r in handler.seen] == [
            ("/gone", "initialize"),
            ("/gone", None),
        ]
        assert "starting a new one" not in err.getvalue()

    def test_an_unreachable_server_answers_the_request_with_the_cause(self):
        bridge, out, err = _bridge(f"http://127.0.0.1:{_closed_port()}/mcp")
        bridge.send(_msg(id=4, method="tools/list"))
        message = "kirocrew: MCP server 'jira' could not be reached (connection refused)"
        assert _lines(out)[0]["error"]["message"] == message
        assert err.getvalue() == message + "\n", "no [Errno ...] socket text"

    def test_an_answer_the_network_cuts_off_is_reported_with_the_cause(self):
        class _TimedOut(io.BytesIO):
            status, headers = 200, http.client.HTTPMessage()

            def read(self, *_args):
                raise TimeoutError(errno.ETIMEDOUT, "Connection timed out")

        out, err = io.BytesIO(), io.StringIO()
        opener = SimpleNamespace(open=lambda _request, timeout: _TimedOut())
        bridge = Bridge(
            "https://example.test/mcp", {}, server="jira", out=out, err=err, opener=opener
        )
        bridge.send(_msg(id=6, method="tools/list"))
        message = "kirocrew: MCP server 'jira' stopped answering (timed out)"
        assert _lines(out)[0]["error"]["message"] == message
        assert err.getvalue() == message + "\n", "no [Errno ...] socket text"

    @pytest.mark.parametrize(
        ("answer", "reason"),
        [
            (
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                b"Content-Length: 500\r\nConnection: close\r\n\r\n"
                b'{"jsonrpc":"2.0","id":',
                "stopped answering (IncompleteRead)",
            ),
            (b"garbage " + CANARY.encode() + b"\r\n\r\n", "could not be reached (BadStatusLine)"),
        ],
        ids=["cut-off-body", "bad-status-line"],
    )
    def test_a_malformed_answer_still_answers_the_request(self, raw_remote, answer, reason):
        canned, received, thread, url = raw_remote
        canned.append(answer)
        thread.start()
        bridge, out, err = _bridge(url)
        bridge.send(_msg(id=5, method="tools/list"))
        thread.join(10)
        assert received and CANARY.encode() in received[0], "the request reached the server"
        (reply,) = _lines(out)
        assert reply["id"] == 5 and reason in reply["error"]["message"]
        assert CANARY not in out.getvalue().decode() + err.getvalue()

    def test_serve_relays_every_line_and_ends_on_eof(self, remote):
        _handler, base = remote
        bridge, out, _err = _bridge(base + "/mcp")
        stdin = io.BytesIO(_msg(id=1, method="initialize", params={}) + b"\n")
        assert bridge.serve(stdin) == 0
        assert [m["id"] for m in _lines(out)] == [1]

    def test_serve_completes_a_notification_before_reading_the_next_request(self):
        """A notification is sent before the next line is read; a request runs alongside."""
        sent: list[str] = []
        inline: dict[str, bool] = {}
        reads: list[list[str]] = []
        release = threading.Event()
        released: list[bool] = []
        serving = threading.current_thread()

        class _Recording(Bridge):
            def send(self, line: bytes) -> None:
                msg = json.loads(line)
                method = msg.get("method", "response")
                inline[method] = threading.current_thread() is serving
                if msg.get("id") == 2:
                    # The tool call holds its thread until serve reads the next line.
                    released.append(release.wait(10))
                sent.append(method)

        lines = [
            _msg(id=1, method="initialize", params={}),
            _msg(method="notifications/initialized"),
            _msg(id=2, method="tools/call", params={}),
            _msg(id="s1", result={}),
            _msg(id=3, method="tools/list"),
        ]

        def _stdin():
            for line in lines:
                reads.append(list(sent))
                if json.loads(line).get("id") == 3:
                    # The response to the server's request was sent before this read.
                    assert "response" in sent
                    release.set()
                yield line

        bridge = _Recording(
            "http://127.0.0.1:9/mcp", {}, server="jira", out=io.BytesIO(), err=io.StringIO()
        )
        assert bridge.serve(_stdin()) == 0
        assert inline == {
            "initialize": True,
            "notifications/initialized": True,
            "tools/call": False,
            "response": True,
            "tools/list": False,
        }
        assert reads[2] == [
            "initialize",
            "notifications/initialized",
        ], "the notification's POST completed before the request after it was read"
        assert released == [True], "the tool call ran while serve read on"
        assert sorted(sent) == sorted(
            ["initialize", "notifications/initialized", "tools/call", "response", "tools/list"]
        )


class TestLegacySse:
    """A server that refuses ``initialize`` with a 4xx speaks the older transport."""

    def test_a_refused_initialize_moves_to_the_event_stream(self, legacy):
        handler, base = legacy
        bridge, out, err = _bridge(base + "/sse")
        bridge.send(_msg(id=1, method="initialize", params={}))
        assert _arrived(out, 1)[0]["result"] == {"protocolVersion": "2024-11-05"}
        bridge.send(_msg(method="notifications/initialized"))
        bridge.send(_msg(id=2, method="tools/call", params={}))
        assert _arrived(out, 2)[1] == {"jsonrpc": "2.0", "id": 2, "result": {"text": "authorized"}}
        assert [r["path"] for r in handler.seen] == ["/sse", "/sse"] + ["/messages?session=1"] * 3
        assert all(r["headers"].get("authorization") == CANARY for r in handler.seen)
        assert err.getvalue() == "", "the refused POST is not an error"

    def test_a_refused_header_is_reported_not_retried(self, legacy):
        handler, base = legacy
        bridge, out, _err = _bridge(base + "/sse", {"Authorization": "Bearer wrong-value"})
        bridge.send(_msg(id=1, method="initialize", params={}))
        assert "HTTP 401 (check its headers)" in _lines(out)[0]["error"]["message"]
        assert len(handler.seen) == 1, "no GET"

    def test_an_endpoint_on_another_site_gets_no_message(self, legacy):
        handler, base = legacy
        handler.endpoint = "https://elsewhere.invalid/messages?session={sid}"
        bridge, out, _err = _bridge(base + "/sse")
        bridge.send(_msg(id=1, method="initialize", params={}))
        assert "another site" in _lines(out)[0]["error"]["message"]
        assert [r["path"] for r in handler.seen] == ["/sse", "/sse"]

    def test_a_closed_stream_is_reopened_with_the_engines_initialize(self, legacy):
        handler, base = legacy
        handler.unanswered = frozenset({"tools/list"})
        bridge, out, _err = _bridge(base + "/sse")
        bridge.send(_msg(id=1, method="initialize", params={"clientInfo": {"name": "engine"}}))
        _arrived(out, 1)
        bridge.send(_msg(method="notifications/initialized"))
        bridge.send(_msg(id=2, method="tools/list"))
        handler.streams["1"].put(None)  # the server restarted
        assert "its event stream closed" in _arrived(out, 2)[1]["error"]["message"]
        bridge.send(_msg(id=3, method="tools/call", params={}))
        assert _arrived(out, 3)[2] == {"jsonrpc": "2.0", "id": 3, "result": {"text": "authorized"}}
        again = [r["body"] for r in handler.seen if r["path"] == "/messages?session=2"]
        assert [b["method"] for b in again] == [
            "initialize",
            "notifications/initialized",
            "tools/call",
        ]
        assert again[0]["params"] == {"clientInfo": {"name": "engine"}}
        assert len(out.getvalue().splitlines()) == 3, "the repeated answer is the bridge's own"

    def test_the_connection_bridge_relays_the_refusal(self, legacy):
        # Its far end is Crew's own proxy, which speaks streamable HTTP only.
        handler, base = legacy
        out = io.BytesIO()
        bridge = ConnectionBridge(
            base + "/sse", {"Authorization": CANARY}, server="c", out=out, err=io.StringIO()
        )
        bridge.send(_msg(id=1, method="initialize", params={}))
        assert "HTTP 405" in _lines(out)[0]["error"]["message"]
        assert len(handler.seen) == 1, "no GET"

    def test_the_connection_bridge_relays_a_forgotten_session_without_a_replay(self, remote):
        # The proxy's approval covers one forwarded call; a replay would be refused.
        handler, base = remote
        out, err = io.BytesIO(), io.StringIO()
        bridge = ConnectionBridge(
            base + "/mcp", {"Authorization": CANARY}, server="c", out=out, err=err
        )
        bridge.send(_msg(id=1, method="initialize", params={}))
        bridge.send(_msg(method="notifications/initialized"))
        handler.forgotten, handler.session_id = frozenset({"sess-1"}), "sess-2"
        bridge.send(_msg(id=2, method="tools/call", params={}))
        reply = _lines(out)[1]
        assert reply["id"] == 2 and "error" in reply, "the provider's answer, not a resent call's"
        assert "HTTP 404 from the provider" in reply["error"]["message"]
        bridge.send(_msg(id=3, method="tools/call", params={}))
        assert _lines(out)[2]["result"] == {
            "text": "authorized"
        }, "the next call uses the new session"
        calls = [
            (r["body"]["method"], r["headers"].get("mcp-session-id"))
            for r in handler.seen
            if r["body"]["method"] in ("initialize", "tools/call")
        ]
        assert calls == [
            ("initialize", None),
            ("tools/call", "sess-1"),
            ("initialize", None),
            ("tools/call", "sess-2"),
        ]
        assert CANARY not in out.getvalue().decode() + err.getvalue()
        assert "sess-" not in out.getvalue().decode() + err.getvalue()


@pytest.mark.parametrize(
    ("endpoint", "stream"),
    [
        ("https://example.invalid:443/messages", "https://example.invalid/sse"),
        ("https://example.invalid/messages", "https://example.invalid:443/sse"),
        ("http://example.invalid:80/messages", "http://example.invalid/sse"),
        ("http://example.invalid/messages", "http://example.invalid:80/sse"),
        ("HTTPS://Example.Invalid/messages", "https://example.invalid:443/sse"),
    ],
)
def test_a_default_port_spelled_out_is_the_same_origin(endpoint, stream):
    assert _same_origin(endpoint, stream)


@pytest.mark.parametrize(
    ("endpoint", "stream"),
    [
        ("https://example.invalid:8443/messages", "https://example.invalid/sse"),
        ("https://example.invalid:80/messages", "https://example.invalid/sse"),
        ("http://example.invalid/messages", "https://example.invalid/sse"),
        ("http://example.invalid:443/messages", "https://example.invalid:443/sse"),
        ("https://elsewhere.invalid/messages", "https://example.invalid/sse"),
        ("https://example.invalid:99999/messages", "https://example.invalid/sse"),
    ],
)
def test_another_port_scheme_or_host_is_another_origin(endpoint, stream):
    assert not _same_origin(endpoint, stream)


def test_the_launcher_runs_the_bridge_for_a_remote_snapshot(home, monkeypatch):
    entry = _project(_spec(mcpServers={"jira": _remote_spec("https://x.invalid/mcp")}))
    launched = entry["mcpServers"]["jira"]
    seen: list[tuple] = []

    def _run(url, headers, *, server):
        seen.append((url, headers, server))
        return 0

    import kiro_crew._ssl_compat as ssl_compat
    import kiro_crew.kas_http_bridge as bridge_module

    monkeypatch.setattr(ssl_compat, "_ensure_ssl_certs", lambda: seen.append("trust"))
    monkeypatch.setattr(bridge_module, "run_bridge", _run)
    monkeypatch.setattr(kas_env_shim.os, "environ", {"KIROCREW_HOME": str(Path(home))})
    args = launched["args"]
    with pytest.raises(SystemExit) as exc:
        kas_env_shim.main(args[args.index("--session") :])
    assert exc.value.code == 0
    assert seen == ["trust", ("https://x.invalid/mcp", {"Authorization": CANARY}, "jira")]
