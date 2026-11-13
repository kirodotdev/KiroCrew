"""Crew OAuth's bridge leg over loopback TCP on Windows, and the bridge's lean imports.

The Windows tests use real sockets, the real kernel TCP table and real SIDs; only
the gateway's run record is pointed at the test process where a test needs it.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import textwrap
import threading
import urllib.error
import urllib.request

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from kiro_crew.subprocess_utf8 import UTF8_TEXT
from kiro_crew.testing.wait import default_timeout

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Win32 TCP table and SIDs")


def test_the_bridge_path_never_imports_the_config_or_platform_stack(tmp_path):
    """One bridge runs per session per Connection, so its start and send path stay
    on leaf modules: the config loader boots the platform and plugin stack. Only a
    refused peer, which ends the send, pays for the audit's imports."""
    script = textwrap.dedent("""
        import io, sys, urllib.request
        import kiro_crew.kas_env_shim
        from kiro_crew import kas_connection_bridge
        doc = {"port": 1, "version": "v", "bridge_token": "t", "connection": "github"}
        bridge = kas_connection_bridge.build_bridge(
            doc, server="github", out=io.BytesIO(), err=io.StringIO()
        )
        try:
            bridge._opener.open(urllib.request.Request("http://127.0.0.1:1/x", data=b"{}"), 2)
        except Exception:
            pass
        import kiro_crew.cli_server_peer  # the peer check every send runs
        loaded = [m for m in ("kiro_crew.config.loader", "kiro_crew.platform") if m in sys.modules]
        assert loaded == [], loaded
        print("ok")
        """)
    env = {**os.environ, "KIROCREW_HOME": str(tmp_path)}
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        timeout=default_timeout(),
        cwd=str(tmp_path),
        env=env,
        **UTF8_TEXT,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("ok")


def _record_gateway(monkeypatch, pid: int | None) -> None:
    """Point the recorded gateway at *pid* (``None``: no record)."""
    from kiro_crew import cli_server_peer

    run_marker = cli_server_peer.run_marker
    record = None if pid is None else (pid, run_marker.pid_start_token(pid))
    monkeypatch.setattr(run_marker, "read_pid_record_path", lambda _path: record)


class _Listener:
    """A loopback listener that records every byte it is sent and can answer once."""

    def __init__(self, answer: bytes = b"") -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.sock.settimeout(default_timeout())
        self.port = self.sock.getsockname()[1]
        self.received = b""
        self._answer = answer
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self.sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(default_timeout())
            try:
                while not self._request_complete():
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    self.received += chunk
                # Answer only after the whole body is read: closing with unread
                # bytes makes Windows reset the connection before the client reads.
                if self._answer:
                    conn.sendall(self._answer)
            except OSError:
                return

    def _request_complete(self) -> bool:
        head, sep, body = self.received.partition(b"\r\n\r\n")
        if not sep:
            return False
        length = 0
        for line in head.split(b"\r\n")[1:]:
            name, _, value = line.partition(b":")
            if name.strip().lower() == b"content-length":
                length = int(value.strip())
        return len(body) >= length

    def close(self) -> None:
        self.sock.close()
        self._thread.join(default_timeout())


@windows_only
def test_the_bridge_refuses_a_listener_that_is_not_the_recorded_gateway(monkeypatch):
    from kiro_crew import kas_connection_bridge

    listener = _Listener()
    try:
        _record_gateway(monkeypatch, None)
        opener = kas_connection_bridge._GatewayOpener(listener.port)
        request = urllib.request.Request(
            f"http://127.0.0.1:{listener.port}/api/connection-proxy",
            data=b"{}",
            headers={"X-Internal-Secret": "never-sent"},
        )
        with pytest.raises(urllib.error.URLError) as refused:
            opener.open(request, 5)
        assert isinstance(refused.value.reason, PermissionError)
    finally:
        listener.close()
    assert listener.received == b"", "refused before the first byte"


@windows_only
def test_the_bridge_completes_a_round_trip_with_the_recorded_gateway(monkeypatch):
    from kiro_crew import kas_connection_bridge

    body = b'{"jsonrpc":"2.0","id":1,"result":{}}'
    answer = (
        b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
    )
    listener = _Listener(answer)
    try:
        _record_gateway(monkeypatch, os.getpid())
        opener = kas_connection_bridge._GatewayOpener(listener.port)
        request = urllib.request.Request(
            f"http://127.0.0.1:{listener.port}/api/connection-proxy", data=b"{}"
        )
        with opener.open(request, 5) as resp:
            assert resp.status == 200 and resp.read() == body
    finally:
        listener.close()
    assert listener.received.startswith(b"POST /api/connection-proxy ")


@windows_only
def test_mcp_secrets_refuses_a_listener_that_is_not_the_recorded_gateway(monkeypatch):
    from kiro_crew import mcp_secrets

    listener = _Listener()
    try:
        _record_gateway(monkeypatch, None)
        monkeypatch.setattr(mcp_secrets, "_resolve_session_key", lambda: "member:alice")
        monkeypatch.setattr(mcp_secrets, "current_tool_call_id", lambda: "c1", raising=False)
        monkeypatch.setattr(
            mcp_secrets, "resolve_client_port_src", lambda _cli: (listener.port, "config")
        )
        monkeypatch.setattr(mcp_secrets, "read_local_secret", lambda _port: "never-sent")
        out = mcp_secrets._call_tool_inner(
            "call_api_with_secret",
            {"secret_name": "K", "method": "POST", "url": "https://api.example.com"},
        )
    finally:
        listener.close()
    assert "could not be completed" in out
    assert listener.received == b"", "refused before the first byte"


_CHILD = """
import os, sys, urllib.error, urllib.request
direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # no system proxy
req = urllib.request.Request(
    sys.argv[1], data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    headers={"Content-Type": "application/json", "X-Crew-Bridge": "v1",
             "X-Crew-Bridge-Token": "tok"},
)
def post():
    try:
        direct.open(req, timeout=30).close()
        return 200
    except urllib.error.HTTPError as exc:
        return exc.code
print(os.getpid(), post(), flush=True)
for _ in sys.stdin:
    print(post(), flush=True)
"""


def _child(url: str) -> tuple[subprocess.Popen, int, int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, url],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        **UTF8_TEXT,
    )
    assert proc.stdout is not None
    pid, status = proc.stdout.readline().split()
    return proc, int(pid), int(status)


def _again(proc: subprocess.Popen) -> int:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write("\n")
    proc.stdin.flush()
    return int(proc.stdout.readline())


@windows_only
@pytest.mark.asyncio
async def test_the_proxy_admits_and_pins_a_loopback_tcp_bridge(monkeypatch):
    from kiro_crew import member_memory_auth
    from kiro_crew.connections import crew_oauth, crew_oauth_proxy

    monkeypatch.setattr(crew_oauth, "manages", lambda slug: False)
    monkeypatch.setattr(crew_oauth_proxy, "_bridges", {})
    crew_oauth_proxy.register_bridge(
        version="v1",
        token="tok",
        session_key="s",
        server="github",
        slug="github",
        mcp_url="http://127.0.0.1:1/mcp",
    )

    @web.middleware
    async def _auth(request, handler):
        request["internal_auth"] = True
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app.router.add_post(crew_oauth_proxy.PROXY_PATH, crew_oauth_proxy.handle)
    async with TestServer(app, host="127.0.0.1") as server:
        url = f"http://127.0.0.1:{server.port}{crew_oauth_proxy.PROXY_PATH}"
        first, first_pid, status = await asyncio.to_thread(_child, url)
        second = None
        try:
            # Admitted, then stopped by the switched-off Connection.
            assert status == 409
            assert crew_oauth_proxy._bridges["v1"].pid == first_pid
            second, _pid, status = await asyncio.to_thread(_child, url)
            assert status == 403, "a second live process with the same token is refused"
            # A peer this account does not own names no pid, so it is refused too.
            monkeypatch.setattr(
                member_memory_auth.platform_compat, "current_user_sid", lambda: "S-1-0-0"
            )
            assert await asyncio.to_thread(_again, first) == 403
        finally:
            for proc in (first, second):
                if proc is not None:
                    proc.communicate(timeout=default_timeout())
