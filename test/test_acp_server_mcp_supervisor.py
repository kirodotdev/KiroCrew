"""Lifecycle + proxy-bridge tests for the per-ACP-session stdio MCP supervisor.

These spawn REAL child processes (tiny inline Python MCP servers) so ownership,
teardown, and the Unix-socket proxy relay (initialize / tools/list / tools/call)
are exercised end to end. The OS-level sandbox is bypassed the same way
``test_mcp_discovery`` does — a passthrough for ``sandboxed_spawn_argv``
(``wrap_argv`` fails closed when no sandbox backend is present) — while
``create_subprocess_limited`` is replaced by direct asyncio spawning so the
children remain hermetic test fixtures.

The bridge design under test (H1/F1 fix): the supervisor spawns and OWNS each
sandboxed child for one provider generation, and exposes it through a
token-guarded per-server Unix socket. ``host`` returns TRUSTED proxy
``StdioMcpServer`` specs (running mcp_proxy.py) — the untrusted command/env never
leave the supervisor. Every fresh proxy generation gets a fresh child, so each
child receives one end-to-end MCP initialize lifecycle.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from tmpdir_helpers import SHORT_TMP_PREFIX, short_tmp_base

from kiro_crew import platform_compat
from kiro_crew.acp_server.mcp_config import StdioMcpServer
from kiro_crew.acp_server.mcp_supervisor import (
    _PROXY_SOCKET_ENV,
    _PROXY_TOKEN_FILE_ENV,
    McpSpawnError,
    SessionMcpSupervisor,
    _client_hidden_paths,
    _RunningServer,
    _stderr_tail,
)
from kiro_crew.pinned_fs import fd_real_path

_UNALLOCATABLE_PID = 99_999_999_999

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

# Rejects a second initialize in one process and appends each process generation
# to a pid log. A reconnect succeeds only when the supervisor replaces the child.
_SINGLE_INIT = r"""
import sys, json, os
with open(sys.argv[1], "a") as f:
    f.write(str(os.getpid()) + "\n")
initialized = False
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
        if initialized:
            response = {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32000, "message": "already initialized"}}
        else:
            initialized = True
            response = {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "serverInfo": {"name": "single-init", "version": "0"}}}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    elif method == "tools/list":
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": {
            "tools": [{"name": "echo"}]}}) + "\n")
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

# The leader exits on SIGTERM while its child deliberately ignores SIGTERM.
# Final cleanup must address the retained process-group id, not the dead leader.
_STUBBORN_DESCENDANT = r"""
import os, signal, subprocess, sys, time
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)",
    ],
    stdin=subprocess.DEVNULL,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
signal.signal(signal.SIGTERM, lambda *_args: sys.exit(0))
with open(sys.argv[1], "w") as f:
    f.write(str(child.pid))
time.sleep(30)
"""


def _server(name: str, script: str, *script_args: str) -> StdioMcpServer:
    return StdioMcpServer(name=name, command=sys.executable, args=["-c", script, *script_args])


def _process_alive(pid: int) -> bool:
    """True iff *pid* exists and is not a zombie where that state is available."""
    if not platform_compat.IS_LINUX:
        return platform_compat.pid_exists(pid)
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


async def _reap_fixture_pid(pidfile) -> None:
    """Reap a real fixture child even when the lifecycle assertion regresses."""
    if not pidfile.exists():
        return
    pid = int(pidfile.read_text())
    if _process_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            platform_compat.kill_pid(pid, platform_compat.SIGKILL)
        await _wait_dead(pid)


async def _drive_proxy(
    spec: StdioMcpServer, requests: list[dict], *, token: str | None = None
) -> list[dict]:
    """Connect to the spec's socket as the proxy would, then send/receive JSON-RPC.

    Reads the reusable proxy credential from the spec's token file (as the real
    proxy does) unless *token* is overridden (to exercise auth rejection).
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
    """Bypass OS isolation while keeping child state in one owned short root.

    The passthrough accepts ``extra_hidden_dirs`` so production can hide the
    gateway secret from editor-supplied children.
    """
    with tempfile.TemporaryDirectory(
        prefix=SHORT_TMP_PREFIX + "acp-sup-", dir=short_tmp_base()
    ) as test_root:
        monkeypatch.setattr(tempfile, "tempdir", test_root)
        monkeypatch.setattr(
            "kiro_crew.acp_server.mcp_supervisor._proxy_root_parent", lambda: test_root
        )

        def _passthrough(
            argv, mode="standard", *, env=None, strip_python_env=False, extra_hidden_dirs=()
        ):
            return list(argv), dict(env or os.environ), None

        async def _create_subprocess(*argv, **kwargs):
            descriptor = kwargs.pop("chdir_fd")
            assert isinstance(descriptor, int) and descriptor >= 0
            target = fd_real_path(descriptor)
            assert target is not None
            kwargs["cwd"] = target
            return await asyncio.create_subprocess_exec(*argv, **kwargs)

        monkeypatch.setattr(
            "kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _passthrough
        )
        monkeypatch.setattr(
            "kiro_crew.acp_server.mcp_supervisor.create_subprocess_limited",
            _create_subprocess,
        )
        yield


async def _host(
    supervisor: SessionMcpSupervisor,
    session_id: str,
    servers: list[StdioMcpServer],
) -> list[StdioMcpServer]:
    return await supervisor.host(session_id, servers, cwd=tempfile.gettempdir())


pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="supervisor teardown/socket assertions are POSIX-only"
)


class TestHostAndOwn:
    @pytest.mark.asyncio
    async def test_host_spawns_owns_and_returns_proxy_spec(self, tmp_path) -> None:
        pidfile = tmp_path / "good.pid"
        sup = SessionMcpSupervisor()
        proxies = await _host(sup, "s1", [_server("echo", _GOOD, str(pidfile))])
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
            assert Path(spec.env[_PROXY_SOCKET_ENV]).is_relative_to(Path(tempfile.gettempdir()))
        finally:
            await sup.shutdown()
        assert sup.hosted("s1") == []
        assert running.proc.returncode is not None  # reaped
        assert await _wait_dead(child_pid)
        assert not os.path.exists(spec.env[_PROXY_SOCKET_ENV])  # socket dir removed

    @pytest.mark.asyncio
    async def test_child_uses_session_cwd(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        observed = tmp_path / "cwd.txt"
        code = _GOOD.replace("f.write(str(os.getpid()))", "f.write(os.getcwd())")
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }
        sup = SessionMcpSupervisor()
        try:
            proxies = await sup.host(
                "s1", [_server("cwd", code, str(observed))], cwd=str(workspace)
            )
            assert observed.read_text() == str(workspace)
            observed.unlink()

            await _drive_proxy(proxies[0], [initialize])
            assert not observed.exists()
            await _drive_proxy(proxies[0], [initialize])
            assert observed.read_text() == str(workspace)
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_workspace_retarget_cannot_redirect_a_reconnect_child(self, tmp_path) -> None:
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        moved = tmp_path / "moved-workspace"
        sensitive = tmp_path / "sensitive"
        sensitive.mkdir()
        (sensitive / "credential").write_text("secret", encoding="utf-8")
        observed = tmp_path / "cwd.txt"
        code = _GOOD.replace("f.write(str(os.getpid()))", "f.write(os.getcwd())")
        initialize = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
        }
        sup = SessionMcpSupervisor()
        descriptor = -1
        try:
            proxies = await sup.host(
                "s1", [_server("cwd", code, str(observed))], cwd=str(workspace)
            )
            descriptor = sup._workspace_fds["s1"]
            await _drive_proxy(proxies[0], [initialize])

            workspace.rename(moved)
            workspace.symlink_to(sensitive, target_is_directory=True)
            observed.unlink()

            await _drive_proxy(proxies[0], [initialize])

            assert observed.read_text() == str(moved)
            assert Path(observed.read_text()) != sensitive
        finally:
            await sup.shutdown()
        assert descriptor >= 0
        with pytest.raises(OSError):
            os.fstat(descriptor)

    @pytest.mark.asyncio
    async def test_proxy_root_is_hidden_from_untrusted_children(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        hidden: list[tuple[str, ...]] = []
        modes: list[str] = []
        restricted: list[str] = []

        def _restrict(path) -> None:
            restricted.append(str(path))

        monkeypatch.setattr(platform_compat, "restrict_dir_to_owner", _restrict)

        def _capture(
            argv,
            mode="standard",
            *,
            env=None,
            strip_python_env=False,
            extra_hidden_dirs=(),
        ):
            modes.append(mode)
            hidden.append(tuple(extra_hidden_dirs))
            return list(argv), dict(env or os.environ), None

        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _capture)
        sup = SessionMcpSupervisor()
        await _host(sup, "s1", [_server("echo", _GOOD)])
        try:
            assert sup._proxy_root is not None
            assert modes == ["strict"]
            client_hidden = _client_hidden_paths()
            assert hidden
            assert all(path in hidden[0] for path in client_hidden)
            assert sup._proxy_root in hidden[0]
            assert os.path.commonpath([sup._dirs["s1"], sup._proxy_root]) == sup._proxy_root
            assert restricted == [sup._proxy_root, sup._dirs["s1"]]
        finally:
            await sup.shutdown()
        assert not os.path.exists(hidden[0][-1])

    @pytest.mark.asyncio
    async def test_client_env_is_sanitized_before_launcher(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, str] = {}
        strip_python_env_seen: list[bool] = []

        def _capture(
            argv,
            mode="standard",
            *,
            env=None,
            strip_python_env=False,
            extra_hidden_dirs=(),
        ):
            captured.update(env or {})
            strip_python_env_seen.append(strip_python_env)
            return list(argv), dict(env or {}), None

        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.sandboxed_spawn_argv", _capture)
        monkeypatch.setenv("PYTHONPATH", "/host/python")
        server = StdioMcpServer(
            name="echo",
            command=sys.executable,
            args=["-c", _GOOD],
            env={
                "LD_PRELOAD": "/untrusted/loader.so",
                "DYLD_INSERT_LIBRARIES": "/untrusted/loader.dylib",
                "PYTHONPATH": "/untrusted/python",
                "OK": "kept",
            },
        )
        sup = SessionMcpSupervisor()
        await _host(sup, "s1", [server])
        try:
            assert captured["OK"] == "kept"
            assert "LD_PRELOAD" not in captured
            assert "DYLD_INSERT_LIBRARIES" not in captured
            assert captured["PYTHONPATH"] == "/host/python"
            assert strip_python_env_seen == [True]
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_teardown_cancellation_reaps_every_owned_child(self) -> None:
        kills: list[str] = []

        async def _cancel() -> None:
            raise asyncio.CancelledError

        async def _unexpected() -> None:
            raise AssertionError("later children must use synchronous cancellation cleanup")

        first = SimpleNamespace(terminate=_cancel, kill_now=lambda: kills.append("first"))
        second = SimpleNamespace(terminate=_unexpected, kill_now=lambda: kills.append("second"))
        sup = SessionMcpSupervisor()
        sup._sessions["s1"] = [first, second]

        with pytest.raises(asyncio.CancelledError):
            await sup.teardown("s1")

        assert kills == ["first", "second"]
        assert sup.hosted("s1") == []

    @pytest.mark.asyncio
    async def test_empty_config_is_noop(self) -> None:
        sup = SessionMcpSupervisor()
        assert await _host(sup, "s1", []) == []
        assert sup.hosted("s1") == []


class TestProxyRelay:
    @pytest.mark.asyncio
    async def test_initialize_tools_list_and_call_through_proxy(self) -> None:
        sup = SessionMcpSupervisor()
        proxies = await _host(sup, "s1", [_server("echo", _GOOD)])
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
    async def test_reconnect_restarts_child_before_initialize(self, tmp_path) -> None:
        pidlog = tmp_path / "generations.pid"
        sup = SessionMcpSupervisor()
        proxies = await _host(sup, "s1", [_server("single", _SINGLE_INIT, str(pidlog))])
        try:
            first = await _drive_proxy(
                proxies[0],
                [
                    {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                ],
            )
            first_pid = int(pidlog.read_text().splitlines()[0])
            second = await _drive_proxy(
                proxies[0],
                [
                    {"jsonrpc": "2.0", "id": 3, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 4, "method": "tools/list"},
                ],
            )
            pids = [int(value) for value in pidlog.read_text().splitlines()]
            assert first[0]["result"]["serverInfo"]["name"] == "single-init"
            assert second[0]["result"]["serverInfo"]["name"] == "single-init"
            assert second[1]["result"]["tools"] == [{"name": "echo"}]
            assert len(pids) == 2
            assert pids[1] != first_pid
            assert await _wait_dead(first_pid)
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_wrong_token_is_rejected(self) -> None:
        sup = SessionMcpSupervisor()
        proxies = await _host(sup, "s1", [_server("echo", _GOOD)])
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
        proxies = await _host(sup, "s1", [_server("bad", _ERROR_INIT)])
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
    async def test_command_resolution_runs_off_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

        async def _to_thread(func, *args, **kwargs):
            calls.append((func, args, kwargs))
            return func(*args, **kwargs)

        def _augment(path: str) -> str:
            return f"{path}{os.pathsep}/augmented/bin"

        def _not_found(command: str, *, path: str | None = None) -> None:
            return None

        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.asyncio.to_thread", _to_thread)
        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.augmented_path", _augment)
        monkeypatch.setattr("kiro_crew.acp_server.mcp_supervisor.shutil.which", _not_found)
        sup = SessionMcpSupervisor()
        server = StdioMcpServer(
            name="nope",
            command="missing",
            env={"PATH": "/client/bin"},
        )
        with pytest.raises(McpSpawnError, match="command not found"):
            await _host(sup, "s1", [server])
        resolution_calls = [call for call in calls if call[0] in (_augment, _not_found)]
        cleanup_calls = [call for call in calls if getattr(call[0], "__name__", "") == "_rmtree"]
        assert len(resolution_calls) == 2
        assert cleanup_calls
        augment_func, augment_args, augment_kwargs = resolution_calls[0]
        assert augment_func is _augment
        assert augment_args == (os.environ.get("PATH", ""),)
        assert augment_kwargs == {}
        resolve_func, resolve_args, resolve_kwargs = resolution_calls[1]
        assert resolve_func is _not_found
        assert resolve_args == ("missing",)
        assert str(resolve_kwargs["path"]).startswith(f"/client/bin{os.pathsep}")
        assert str(resolve_kwargs["path"]).endswith(f"{os.pathsep}/augmented/bin")

    @pytest.mark.asyncio
    async def test_command_not_found(self) -> None:
        sup = SessionMcpSupervisor()
        with pytest.raises(McpSpawnError, match="command not found"):
            await _host(sup, "s1", [StdioMcpServer(name="nope", command="/no/such/binary-xyz")])
        assert sup.hosted("s1") == []

    @pytest.mark.asyncio
    async def test_immediate_exit_reported_with_stderr(self) -> None:
        sup = SessionMcpSupervisor(liveness_grace=1.0)
        with pytest.raises(McpSpawnError) as exc:
            await _host(sup, "s1", [_server("dies", _EXIT)])
        assert "exited immediately" in str(exc.value)
        assert "refuses to run" in str(exc.value)  # redacted stderr tail attached
        assert sup.hosted("s1") == []

    @pytest.mark.asyncio
    async def test_partial_failure_reaps_the_good_server(self, tmp_path) -> None:
        pidfile = tmp_path / "good.pid"
        sup = SessionMcpSupervisor(liveness_grace=1.0)
        try:
            with pytest.raises(McpSpawnError):
                await _host(
                    sup,
                    "s1",
                    [_server("good", _GOOD, str(pidfile)), _server("bad", _EXIT)],
                )
            assert sup.hosted("s1") == []
            pid = int(pidfile.read_text())
            assert await _wait_dead(pid)  # the already-started good server was cleaned up
        finally:
            await sup.shutdown()
            await _reap_fixture_pid(pidfile)


class TestReconfigureAndIsolation:
    @pytest.mark.asyncio
    async def test_reconfigure_replaces_previous_set(self) -> None:
        sup = SessionMcpSupervisor()
        await _host(sup, "s1", [_server("first", _GOOD)])
        first = sup._sessions["s1"][0].proc
        try:
            await _host(sup, "s1", [_server("second", _GOOD)])
            assert sup.hosted("s1") == ["second"]
            assert first.returncode is not None  # the old server was torn down
            assert sup._sessions["s1"][0].proc.returncode is None
        finally:
            await sup.shutdown()

    @pytest.mark.asyncio
    async def test_two_sessions_are_isolated(self) -> None:
        sup = SessionMcpSupervisor()
        pa = await _host(sup, "a", [_server("sa", _GOOD)])
        pb = await _host(sup, "b", [_server("sb", _GOOD)])
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
        await _host(sup, "a", [_server("sa", _GOOD)])
        await _host(sup, "b", [_server("sb", _GOOD)])
        procs = [sup._sessions["a"][0].proc, sup._sessions["b"][0].proc]
        await sup.shutdown()
        assert sup.hosted("a") == [] and sup.hosted("b") == []
        assert all(p.returncode is not None for p in procs)


class TestCleanup:
    def test_process_group_reap_uses_retained_group_id(self, monkeypatch) -> None:
        calls: list[tuple[int, object]] = []
        tree_calls: list[tuple[int, object]] = []

        def fake_kill_process_group(pgid: int, sig: object) -> None:
            calls.append((pgid, sig))

        monkeypatch.setattr(platform_compat, "kill_process_group", fake_kill_process_group)
        monkeypatch.setattr(
            platform_compat,
            "kill_process_tree",
            lambda pid, sig: tree_calls.append((pid, sig)),
        )
        monkeypatch.setattr(platform_compat, "pgroup_of", lambda pid: pid)
        running = _RunningServer(
            name="fixture",
            proc=SimpleNamespace(pid=_UNALLOCATABLE_PID, returncode=None),
            process_group_id=_UNALLOCATABLE_PID,
        )
        running._reap_group()
        running._reap_group()

        assert calls == [(_UNALLOCATABLE_PID, platform_compat.SIGKILL)]
        assert tree_calls == []
        assert running.process_group_id is None

    def test_process_group_reap_refuses_reused_leader_pid(self, monkeypatch) -> None:
        calls: list[tuple[int, object]] = []
        monkeypatch.setattr(
            platform_compat,
            "kill_process_group",
            lambda pgid, sig: calls.append((pgid, sig)),
        )
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid == _UNALLOCATABLE_PID)
        running = _RunningServer(
            name="fixture",
            proc=SimpleNamespace(pid=_UNALLOCATABLE_PID, returncode=1),
            process_group_id=_UNALLOCATABLE_PID,
        )

        running._reap_group()

        assert calls == []
        assert running.process_group_id is None

    @pytest.mark.asyncio
    @pytest.mark.skipif(not platform_compat.IS_POSIX, reason="POSIX retained process group")
    async def test_terminate_reaps_descendant_after_leader_exits(self, tmp_path) -> None:
        pidfile = tmp_path / "descendant.pid"
        sup = SessionMcpSupervisor()
        child_pid = 0
        try:
            await _host(sup, "s1", [_server("stubborn", _STUBBORN_DESCENDANT, str(pidfile))])
            for _ in range(200):
                if pidfile.exists():
                    break
                await asyncio.sleep(0.02)
            child_pid = int(pidfile.read_text())

            await sup.teardown("s1")

            assert await _wait_dead(child_pid)
        finally:
            await sup.shutdown()
            if child_pid and _process_alive(child_pid):
                with contextlib.suppress(ProcessLookupError):
                    platform_compat.kill_pid(child_pid, platform_compat.SIGKILL)


class TestCancellation:
    @pytest.mark.asyncio
    async def test_cancel_mid_host_leaves_no_orphan(self, tmp_path) -> None:
        pidfile = tmp_path / "slow.pid"
        # Large liveness grace so host() is still inside _assert_alive when cancelled.
        sup = SessionMcpSupervisor(liveness_grace=30.0)
        task = asyncio.create_task(_host(sup, "s1", [_server("slow", _SLOW, str(pidfile))]))
        try:
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
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await sup.shutdown()
            await _reap_fixture_pid(pidfile)


class TestCredentialMasking:
    def test_client_hidden_paths_name_gateway_secret_and_env(self) -> None:
        hidden_paths = _client_hidden_paths()
        assert any(path.endswith("/.local_secret") for path in hidden_paths)
        assert any(path.endswith("/.env") for path in hidden_paths)

    @pytest.mark.asyncio
    async def test_failure_tail_uses_context_egress_redactor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_crew.acp_server import mcp_supervisor as supervisor_mod

        class Stderr:
            async def read(self, _size: int) -> bytes:
                return b"companion-only-secret"

        seen: list[str] = []

        def redact(text: str) -> str:
            seen.append(text)
            return "[CONTEXT-REDACTED]"

        monkeypatch.setattr(supervisor_mod, "redact_via_context", redact)

        assert await _stderr_tail(SimpleNamespace(stderr=Stderr())) == "[CONTEXT-REDACTED]"
        assert seen == ["companion-only-secret"]
