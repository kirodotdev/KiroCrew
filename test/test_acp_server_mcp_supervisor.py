"""Lifecycle + proxy-bridge tests for the per-ACP-session stdio MCP supervisor.

These spawn REAL child processes (tiny inline Python MCP servers) so ownership,
teardown, and the Unix-socket proxy relay (initialize / tools/list / tools/call)
are exercised end to end. The OS-level sandbox is bypassed the same way
``test_mcp_discovery`` does — a passthrough for ``sandboxed_spawn_argv``
(``wrap_argv`` fails closed when no sandbox backend is present) — while
``create_subprocess_limited`` is replaced by direct asyncio spawning so the
children remain hermetic test fixtures.

The bridge design under test (H1/F1 fix): the supervisor spawns and OWNS the
sandboxed child ONCE, and exposes it through a token-guarded per-server Unix
socket. ``host`` returns TRUSTED proxy ``StdioMcpServer`` specs (running
mcp_proxy.py) — the untrusted command/env never leave the supervisor. The MCP
handshake flows end-to-end through the socket, so the supervisor never consumes
initialize.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp_server.mcp_config import StdioMcpServer
from kiro_crew.acp_server.mcp_supervisor import (
    _PROXY_SOCKET_ENV,
    _PROXY_TOKEN_FILE_ENV,
    McpSpawnError,
    SessionMcpSupervisor,
    _client_hidden_paths,
)

# ── fixture MCP servers (inline Python; argv[1], when present, is a pid file) ──

# Answers initialize, tools/list, and tools/call, then stays alive until stdin
# closes. This is a real, if tiny, MCP server driven THROUGH the proxy socket.
_GOOD = r"""
import sys, json, os
if len(sys.argv) > 1:
    with open(sys.argv[1], "w") as f:
        f.write(str(os.getpid()))
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        msg = json.loads(line)
    except Exception:
        continue
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "serverInfo": {"name": "fixture", "version": "0"}}}) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo"}]}}) + "\n")
        sys.stdout.flush()
    elif method == "tools/call":
        args = (msg.get("params") or {}).get("arguments") or {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo:" + str(args.get("text", ""))}]}}) + "\n")
        sys.stdout.flush()
"""

# Answers initialize with a JSON-RPC error object (relayed to the proxy client).
_ERROR_INIT = r"""
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
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": msg.get("id"),
            "error": {"code": -32000, "message": "boom"}}) + "\n")
        sys.stdout.flush()
"""

# Writes a diagnostic to stderr and exits before doing anything — must fail host.
_EXIT = r"""
import sys
sys.stderr.write("startup failed: fixture refuses to run\n")
sys.stderr.flush()
sys.exit(3)
"""

# Records its pid, then blocks forever. Used for cancellation/liveness tests.
_SLOW = r"""
import sys, os, time
with open(sys.argv[1], "w") as f:
    f.write(str(os.getpid()))
time.sleep(30)
"""


def _server(name: str, script: str, *script_args: str) -> StdioMcpServer:
    return StdioMcpServer(name=name, command=sys.executable, args=["-c", script, *script_args])


def _process_alive(pid: int) -> bool:
    """True iff *pid* exists and is not a zombie (Linux ``/proc``)."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as fh:
            state = fh.read().rsplit(") ", 1)[1].split()[0]
        return state != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):
        return False


async def _wait_dead(pid: int, timeout: float = 5.0) -> bool:
    """Poll until *pid* is gone/zombie, or the timeout elapses."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if not _process_alive(pid):
            return True
        await asyncio.sleep(0.05)
    return not _process_alive(pid)


async def _drive_proxy(
    spec: StdioMcpServer, requests: list[dict], *, token: str | None = None
) -> list[dict]:
    """Connect to the spec's socket as the proxy would, then send/receive JSON-RPC.

    Reads the one-time token from the spec's token file (as the real proxy does)
    unless *token* is overridden (to exercise auth rejection).
    """
    socket_path = spec.env[_PROXY_SOCKET_ENV]
    if token is None:
        with open(spec.env[_PROXY_TOKEN_FILE_ENV], encoding="utf-8") as fh:
            token = fh.read().strip()
    reader, writer = await asyncio.open_unix_connection(socket_path)
    writer.write((token + "\n").encode("utf-8"))
    await writer.drain()
    responses: list[dict] = []
    for req in requests:
        writer.write((json.dumps(req) + "\n").encode("utf-8"))
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=5.0)
        responses.append(json.loads(line))
    writer.close()
    return responses


@pytest.fixture(autouse=True)
def _passthrough_sandbox(monkeypatch):
    """Bypass OS isolation and post-exec limits for real child test processes.

    The passthrough accepts ``extra_hidden_dirs`` so production can hide the
    gateway secret from editor-supplied children.
    """

    def _passthrough(
        argv, mode="standard", *, env=None, strip_python_env=False, extra_hidden_dirs=()
    ):
        return list(argv), dict(env or os.environ), None

    async def _create_subprocess(*argv, **kwargs):
        return await asyncio.create_subprocess_exec(*argv, **kwargs)

    monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _passthrough)
    monkeypatch.setattr(
        "kiro_crew.acp_server.mcp_supervisor.create_subprocess_limited",
        _create_subprocess,
    )


pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="supervisor teardown/socket assertions are POSIX-only"
)


class TestHostAndOwn:
    @pytest.mark.asyncio
    async def test_host_spawns_owns_and_returns_proxy_spec(self, tmp_path) -> None:
        pidfile = tmp_path / "good.pid"
        sup = SessionMcpSupervisor()
        proxies = await sup.host("s1", [_server("echo", _GOOD, str(pidfile))])
        try:
            # The real child is owned + alive.
            assert sup.hosted("s1") == ["echo"]
            running = sup._sessions["s1"][0]
            assert running.proc.returncode is None
            child_pid = int(pidfile.read_text())
            assert _process_alive(child_pid)
            # The returned spec is the TRUSTED proxy, not the client command.
            assert len(proxies) == 1
            spec = proxies[0]
            assert spec.name == "echo"
            assert spec.command == sys.executable
            assert spec.args[0].endswith("mcp_proxy.py")
            assert "--socket" in spec.args
            # No secret in argv; only a socket path + token FILE path (not the token).
            assert _PROXY_SOCKET_ENV in spec.env
            assert _PROXY_TOKEN_FILE_ENV in spec.env
            assert os.path.exists(spec.env[_PROXY_SOCKET_ENV])
        finally:
            await sup.shutdown()
        assert sup.hosted("s1") == []
        assert running.proc.returncode is not None  # reaped
        assert await _wait_dead(child_pid)
        assert not os.path.exists(spec.env[_PROXY_SOCKET_ENV])  # socket dir removed

    @pytest.mark.asyncio
    async def test_proxy_root_is_hidden_from_untrusted_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hidden: list[tuple[str, ...]] = []

        def _capture(
            argv,
            mode="standard",
            *,
            env=None,
            strip_python_env=False,
            extra_hidden_dirs=(),
        ):
            hidden.append(tuple(extra_hidden_dirs))
            return list(argv), dict(env or os.environ), None

        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _capture)
        sup = SessionMcpSupervisor()
        await sup.host("s1", [_server("echo", _GOOD)])
        try:
            assert sup._proxy_root is not None
            assert hidden and sup._proxy_root in hidden[0]
            assert os.path.commonpath([sup._dirs["s1"], sup._proxy_root]) == sup._proxy_root
        finally:
            await sup.shutdown()
        assert not os.path.exists(hidden[0][-1])

    @pytest.mark.asyncio
    async def test_empty_config_is_noop(self) -> None:
        sup = SessionMcpSupervisor()
        assert await sup.host("s1", []) == []
        assert sup.hosted("s1") == []


class TestProxyRelay:
    @pytest.mark.asyncio
    async def test_initialize_tools_list_and_call_through_proxy(self) -> None:
        sup = SessionMcpSupervisor()
        proxies = await sup.host("s1", [_server("echo", _GOOD)])
        try:
            responses = await _drive_proxy(
                proxies[0],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                    },
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "echo", "arguments": {"text": "hi"}},
                    },
                ],
            )
            assert responses[0]["result"]["serverInfo"]["name"] == "fixture"
            assert responses[1]["result"]["tools"][0]["name"] == "echo"
            assert responses[2]["result"]["content"][0]["text"] == "echo:hi"
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_wrong_token_is_rejected(self) -> None:
        sup = SessionMcpSupervisor()
        proxies = await sup.host("s1", [_server("echo", _GOOD)])
        try:
            socket_path = proxies[0].env[_PROXY_SOCKET_ENV]
            reader, writer = await asyncio.open_unix_connection(socket_path)
            writer.write(b"not-the-token\n")
            await writer.drain()
            # A rejected connection is closed without relaying: read returns EOF.
            data = await asyncio.wait_for(reader.read(), timeout=5.0)
            assert data == b""
            writer.close()
            # A subsequent VALID connection still works (rejection didn't kill the child).
            responses = await _drive_proxy(
                proxies[0],
                [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}],
            )
            assert responses[0]["result"]["tools"][0]["name"] == "echo"
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_initialize_error_surfaces_through_proxy(self) -> None:
        # The supervisor does NOT init the child; a server that errors on
        # initialize hosts fine and the error reaches the client via the proxy.
        sup = SessionMcpSupervisor()
        proxies = await sup.host("s1", [_server("bad", _ERROR_INIT)])
        try:
            responses = await _drive_proxy(
                proxies[0],
                [
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
                    }
                ],
            )
            assert responses[0]["error"]["message"] == "boom"
        finally:
            await sup.shutdown()


class TestSpawnFailures:
    @pytest.mark.asyncio
    async def test_command_not_found(self) -> None:
        sup = SessionMcpSupervisor()
        with pytest.raises(McpSpawnError, match="command not found"):
            await sup.host("s1", [StdioMcpServer(name="nope", command="/no/such/binary-xyz")])
        assert sup.hosted("s1") == []

    @pytest.mark.asyncio
    async def test_immediate_exit_reported_with_stderr(self) -> None:
        sup = SessionMcpSupervisor(liveness_grace=1.0)
        with pytest.raises(McpSpawnError) as exc:
            await sup.host("s1", [_server("dies", _EXIT)])
        assert "exited immediately" in str(exc.value)
        assert "refuses to run" in str(exc.value)  # redacted stderr tail attached
        assert sup.hosted("s1") == []

    @pytest.mark.asyncio
    async def test_partial_failure_reaps_the_good_server(self, tmp_path) -> None:
        pidfile = tmp_path / "good.pid"
        sup = SessionMcpSupervisor(liveness_grace=1.0)
        with pytest.raises(McpSpawnError):
            await sup.host(
                "s1",
                [_server("good", _GOOD, str(pidfile)), _server("bad", _EXIT)],
            )
        assert sup.hosted("s1") == []
        pid = int(pidfile.read_text())
        assert await _wait_dead(pid)  # the already-started good server was cleaned up


class TestReconfigureAndIsolation:
    @pytest.mark.asyncio
    async def test_reconfigure_replaces_previous_set(self) -> None:
        sup = SessionMcpSupervisor()
        await sup.host("s1", [_server("first", _GOOD)])
        first = sup._sessions["s1"][0].proc
        try:
            await sup.host("s1", [_server("second", _GOOD)])
            assert sup.hosted("s1") == ["second"]
            assert first.returncode is not None  # the old server was torn down
            assert sup._sessions["s1"][0].proc.returncode is None
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_two_sessions_are_isolated(self) -> None:
        sup = SessionMcpSupervisor()
        pa = await sup.host("a", [_server("sa", _GOOD)])
        pb = await sup.host("b", [_server("sb", _GOOD)])
        try:
            assert sup.hosted("a") == ["sa"]
            assert sup.hosted("b") == ["sb"]
            # Distinct sockets — one session cannot reach the other's child.
            assert pa[0].env[_PROXY_SOCKET_ENV] != pb[0].env[_PROXY_SOCKET_ENV]
            proc_a = sup._sessions["a"][0].proc
            sock_a = pa[0].env[_PROXY_SOCKET_ENV]
            await sup.teardown("a")
            assert sup.hosted("a") == []
            assert proc_a.returncode is not None
            assert not os.path.exists(sock_a)
            # b is untouched and still reachable.
            assert sup.hosted("b") == ["sb"]
            responses = await _drive_proxy(
                pb[0], [{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]
            )
            assert responses[0]["result"]["tools"][0]["name"] == "echo"
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_shutdown_reaps_every_session(self) -> None:
        sup = SessionMcpSupervisor()
        await sup.host("a", [_server("sa", _GOOD)])
        await sup.host("b", [_server("sb", _GOOD)])
        procs = [sup._sessions["a"][0].proc, sup._sessions["b"][0].proc]
        await sup.shutdown()
        assert sup.hosted("a") == [] and sup.hosted("b") == []
        assert all(p.returncode is not None for p in procs)


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_mid_host_leaves_no_orphan(self, tmp_path) -> None:
        pidfile = tmp_path / "slow.pid"
        # Large liveness grace so host() is still inside _assert_alive when cancelled.
        sup = SessionMcpSupervisor(liveness_grace=30.0)
        task = asyncio.create_task(sup.host("s1", [_server("slow", _SLOW, str(pidfile))]))
        for _ in range(200):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sup.hosted("s1") == []
        pid = int(pidfile.read_text())
        assert await _wait_dead(pid)


class TestCredentialMasking:
    def test_client_children_hide_gateway_secret_and_env(self) -> None:
        # F1: the untrusted client child must not be able to read the gateway
        # secret / .env off disk. The supervisor passes _CLIENT_HIDDEN_FILES to
        # the sandbox; verify the generated Linux launcher masks them even in the
        # "standard" mode these children spawn under.
        from kiro_crew import sandbox

        hidden_paths = _client_hidden_paths()
        script = sandbox._build_launcher_script("standard", extra_hidden_dirs=hidden_paths)
        assert any(path.endswith("/.local_secret") for path in hidden_paths)
        assert any(path.endswith("/.env") for path in hidden_paths)
        for path in hidden_paths:
            assert path in script
        # Without the extras, standard mode masks neither (regression guard).
        plain = sandbox._build_launcher_script("standard")
        for path in hidden_paths:
            assert path not in plain
