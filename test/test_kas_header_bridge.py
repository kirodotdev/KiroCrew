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

import io
import json
import os
import socket
import socketserver
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from kas_agents_helpers import _spec

from kiro_crew import kas_env_shim
from kiro_crew.acp import kas_env_launch
from kiro_crew.acp._dispatch import build_session_new_params
from kiro_crew.acp.kas_agents import hoist_servers_for_session, to_client_custom_agent
from kiro_crew.kas_http_bridge import Bridge

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
        self.seen.append({"path": self.path, "headers": _lower(self.headers), "body": body})
        if self.path == "/moved":
            self._reply(302, Location="/elsewhere")
            return
        if self.headers.get("Authorization") != CANARY:
            self._reply(401, b'{"error":"no token"}')
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
        if self.sse:
            self._reply(
                200,
                b"event: message\ndata: " + payload + b"\n\n",
                "text/event-stream",
                Mcp_Session_Id="sess-1",
            )
        else:
            self._reply(200, payload, Mcp_Session_Id="sess-1")

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

    def test_an_sse_entry_keeps_the_withholding_projection(self, home):
        spec = _spec(mcpServers={"old": _remote_spec("https://x.invalid/sse", type="sse")})
        entry = _project(spec)
        assert entry["mcpServers"]["old"]["url"] == "https://x.invalid/sse"
        assert "headers" not in entry["mcpServers"]["old"]
        assert not (home / "kas-mcp-launch").exists()

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


def _msg(**fields) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **fields}).encode() + b"\n"


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

    def test_an_unreachable_server_answers_the_request(self):
        bridge, out, _err = _bridge(f"http://127.0.0.1:{_closed_port()}/mcp")
        bridge.send(_msg(id=4, method="tools/list"))
        assert "could not be reached" in _lines(out)[0]["error"]["message"]

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
