"""Per-ACP-session stdio MCP process supervision + trusted proxy bridge.

An ACP client attaches stdio MCP servers to a session via ``mcpServers`` (parsed
and validated by :mod:`kiro_crew.acp_server.mcp_config`). Those servers are
UNTRUSTED — an arbitrary ``command``/``args``/``env`` the editor chose. The
supervisor owns these children so they always run inside Kiro Crew's sandbox and
resource controls:

* :meth:`SessionMcpSupervisor.host` spawns each requested server ONCE, through
  Kiro Crew's asynchronous sandbox chokepoint
  (:func:`sandboxed_spawn_argv_async` — OS isolation + a credential-scrubbed
  environment + the gateway secret/.env hidden on disk + no shell), keeps it
  alive scoped to exactly one ACP session, and reaps it deterministically on
  reconfigure, session teardown, adapter EOF, or cancellation;
* each owned child is exposed to the model-side provider through a per-session,
  per-server **Unix-domain socket** guarded by a reusable proxy credential. ``host`` returns
  a set of *proxy* :class:`StdioMcpServer` specs — each one runs
  :mod:`kiro_crew.acp_server.mcp_proxy` (a trusted Kiro Crew relay) with only a
  socket path (argv) and a token-file path (env). kiro-cli spawns the trusted
  proxy; the untrusted original ``command``/``env`` NEVER reach kiro-cli.

The MCP ``initialize`` handshake flows end-to-end between kiro-cli and the real
child through the proxy/socket — the supervisor never consumes it — so the child
is spawned and initialized exactly once (no double-spawn) and initialization
errors surface through the provider/proxy path.

Reuse over reinvention: the spawn goes through
:func:`sandboxed_spawn_argv_async` / :func:`create_subprocess_limited`
(``sandbox``) and :func:`augmented_path` (``env``) — the same primitives Kiro
Crew-owned MCP servers spawn through.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import logging
import os
import secrets
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from kiro_crew import pinned_fs, platform_compat
from kiro_crew.acp_server.mcp_config import StdioMcpServer
from kiro_crew.config import config_dir
from kiro_crew.env import augmented_path, sanitize_spec_env
from kiro_crew.platform.context import redact_log_via_context, redact_via_context
from kiro_crew.sandbox import (
    bound_agent_workspace_target,
    create_subprocess_limited,
    release_bound_agent_workspace,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
)
from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

# SIGTERM → SIGKILL grace on teardown.
_TERMINATE_GRACE_SECS = 5.0
# A child must survive at least this long after spawn to be considered "up".
# We must NOT read its stdout here (that stream belongs to the proxy relay, so
# kiro-cli's initialize response is not stolen), so liveness == "did not exit".
_LIVENESS_GRACE_SECS = 0.3
# Bytes of a failed server's stderr appended (redacted) to the surfaced error.
_STDERR_TAIL_BYTES = 4096
_STDERR_TAIL_MAX = 500
# Relay copy chunk size.
_CHUNK = 65536
# Seconds to wait for a proxy connection's auth line before dropping it.
_AUTH_TIMEOUT_SECS = 10.0

# The trusted relay the provider (kiro-cli) is told to spawn. Referenced by
# ABSOLUTE PATH (python <file>) so running it does not import the kiro_crew
# package — see mcp_proxy.py.
_PROXY_PATH = str(Path(__file__).with_name("mcp_proxy.py"))
_PROXY_SOCKET_ENV = "KIROCREW_MCP_PROXY_SOCKET"
_PROXY_TOKEN_FILE_ENV = "KIROCREW_MCP_PROXY_TOKEN_FILE"

# Home-relative files hidden from a CLIENT MCP child on disk (F1): the gateway
# internal secret and the seeded credential env file. A client-supplied server
# has no legitimate need to call the gateway API, so it must not be able to read
# the secret that would authenticate it as an internal caller. (First-party
# Kiro Crew MCP servers are spawned elsewhere without this mask, since they DO
# need the secret to call back.)
_CLIENT_HIDDEN_FILES = (".local_secret", ".env")


def _client_hidden_paths() -> tuple[str, ...]:
    home = config_dir()
    return tuple(str(home / leaf) for leaf in _CLIENT_HIDDEN_FILES)


def _proxy_root_parent() -> str:
    """Return a short writable runtime directory for AF_UNIX capability paths."""
    candidates = [os.environ.get("XDG_RUNTIME_DIR", "")]
    if sys.platform.startswith("linux"):
        candidates.append("/dev/shm")
    candidates.append("/tmp")
    for candidate in candidates:
        if (
            candidate
            and len(os.fsencode(candidate)) <= 40
            and os.path.isdir(candidate)
            and os.access(candidate, os.W_OK | os.X_OK)
        ):
            return candidate
    return tempfile.gettempdir()


class McpSpawnError(RuntimeError):
    """A client-supplied stdio MCP server failed to spawn.

    The dispatch layer surfaces this to the ACP client as a JSON-RPC error, so
    :meth:`__str__` is deliberately secret-safe: it names the offending server
    and a fixed reason phrase, plus an optional *redacted* stderr tail — never an
    environment value or the full command line. ``acp_client_safe`` lets the
    protocol core recognise a safe-to-forward message without importing this
    module.
    """

    acp_client_safe = True

    def __init__(self, server_name: str, reason: str, *, detail: str = "") -> None:
        self.server_name = server_name
        self.reason = reason
        self.detail = detail
        super().__init__(self._format())

    def _format(self) -> str:
        msg = f"MCP server {self.server_name!r} failed to start: {self.reason}"
        if self.detail:
            msg = f"{msg}\nstderr: {self.detail}"
        return msg


def _bind_workspace(cwd: str) -> int:
    """Pin a non-sensitive workspace identity without following mutable ancestors."""
    descriptor = pinned_fs.open_dir_pinned(cwd, what="ACP MCP workspace")
    try:
        target = bound_agent_workspace_target(descriptor, cwd)
        if target is None or is_sensitive_path(target):
            raise RuntimeError("session cwd changed or resolves to a sensitive path")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


async def _bind_workspace_async(cwd: str, server_name: str) -> int:
    """Bind off-loop and settle the descriptor owner before propagating cancellation."""
    binding = asyncio.create_task(asyncio.to_thread(_bind_workspace, cwd))
    cancellation: asyncio.CancelledError | None = None
    while not binding.done():
        try:
            await asyncio.shield(binding)
        except asyncio.CancelledError as exc:
            cancellation = exc

    if cancellation is not None:
        try:
            descriptor = binding.result()
        except BaseException:
            raise cancellation
        try:
            await release_bound_agent_workspace(descriptor)
        except asyncio.CancelledError as exc:
            cancellation = exc
        raise cancellation

    try:
        return binding.result()
    except Exception as exc:
        raise McpSpawnError(server_name, "session cwd could not be safely bound") from exc


@dataclass
class _RunningServer:
    """One live stdio MCP child owned by a single ACP session, plus its proxy.

    The supervisor owns the child; the proxy socket exposes it to a
    provider-spawned :mod:`kiro_crew.acp_server.mcp_proxy`. The MCP handshake and
    all traffic flow through the socket, so the supervisor never reads the
    child's stdout itself (that would steal kiro-cli's initialize response).
    """

    name: str
    proc: asyncio.subprocess.Process
    process_group_id: int | None = None
    sandbox_cleanup: str | None = None
    socket_path: str | None = None
    token: str = ""
    server: "asyncio.AbstractServer | None" = None
    conns: set[asyncio.Task[None]] = field(default_factory=set)
    stderr_drain: "asyncio.Task[None] | None" = None
    source: StdioMcpServer | None = None
    cwd: str = ""
    workspace_fd: int = -1
    connected_once: bool = False
    closed: bool = False
    reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    process_group_reaped: bool = False

    async def terminate(self) -> None:
        """Graceful teardown: close relays, SIGTERM the tree, then SIGKILL."""
        self.closed = True
        self._close_server()
        async with self.reconnect_lock:
            await self._cancel_conns()
            await self.stop_child()

    async def stop_child(self) -> None:
        """Reap the child tree while preserving the authenticated proxy listener."""
        self._cancel_stderr_drain()
        proc = self.proc
        try:
            if proc.returncode is None:
                with contextlib.suppress(Exception):
                    if proc.stdin is not None and not proc.stdin.is_closing():
                        proc.stdin.close()
                try:
                    await platform_compat.kill_process_tree_async(proc.pid, platform_compat.SIGTERM)
                except (ProcessLookupError, OSError, ValueError):
                    with contextlib.suppress(ProcessLookupError, Exception):
                        proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_TERMINATE_GRACE_SECS)
                except (asyncio.TimeoutError, Exception):
                    self._reap_group()
                    with contextlib.suppress(ProcessLookupError, Exception):
                        proc.kill()
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.wait(), timeout=2.0)
        finally:
            self._reap_group()
            self._unlink_cleanup()

    def kill_now(self) -> None:
        """Synchronous, non-awaiting teardown for the cancellation path."""
        self.closed = True
        self._close_server()
        for task in self.conns:
            task.cancel()
        self.conns.clear()
        self._cancel_stderr_drain()
        proc = self.proc
        with contextlib.suppress(Exception):
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        self._reap_group()
        with contextlib.suppress(ProcessLookupError, Exception):
            proc.kill()
        self._unlink_cleanup()

    def _close_server(self) -> None:
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.close()
            self.server = None

    async def _cancel_conns(self) -> None:
        for task in list(self.conns):
            task.cancel()
        if self.conns:
            await asyncio.gather(*self.conns, return_exceptions=True)
        self.conns.clear()

    def _cancel_stderr_drain(self) -> None:
        if self.stderr_drain is not None:
            self.stderr_drain.cancel()
            self.stderr_drain = None

    def _reap_group(self) -> None:
        """SIGKILL the owned tree only while its retained group is still attributable."""
        if self.process_group_reaped:
            return
        self.process_group_reaped = True
        pid = self.proc.pid
        pgid = self.process_group_id
        self.process_group_id = None
        if pgid is not None:
            if self.proc.returncode is None:
                if platform_compat.pgroup_of(pid) != pgid:
                    return
            elif platform_compat.pid_exists(pgid):
                # A reaped leader leaves no PID while its descendants keep the
                # group alive; a live PID here may be a recycled group leader.
                return
            with contextlib.suppress(OSError, ValueError):
                platform_compat.kill_process_group(pgid, platform_compat.SIGKILL)
        elif isinstance(pid, int) and pid > 1:
            with contextlib.suppress(OSError, ValueError):
                platform_compat.kill_process_tree(pid, platform_compat.SIGKILL)

    def _unlink_cleanup(self) -> None:
        if self.sandbox_cleanup:
            with contextlib.suppress(OSError):
                Path(self.sandbox_cleanup).unlink(missing_ok=True)
            self.sandbox_cleanup = None


class SessionMcpSupervisor:
    """Owns the stdio MCP child processes for each ACP session and their proxies.

    Ownership is keyed strictly by ``session_id`` so one editor session can never
    read, reconfigure, or reap another's servers. :meth:`host` replaces a
    session's whole set atomically. Any spawn failure reaps the partial set and
    raises :class:`McpSpawnError`, so a session is never left half-hosted.
    """

    def __init__(self, *, liveness_grace: float = _LIVENESS_GRACE_SECS) -> None:
        self._sessions: dict[str, list[_RunningServer]] = {}
        self._dirs: dict[str, str] = {}
        self._workspace_fds: dict[str, int] = {}
        self._proxy_root: str | None = None
        self._lock = asyncio.Lock()
        self._liveness_grace = liveness_grace

    async def host(
        self, session_id: str, servers: list[StdioMcpServer], *, cwd: str
    ) -> list[StdioMcpServer]:
        """Spawn+own *servers* under the sandbox and expose each via a proxy.

        Returns the proxy :class:`StdioMcpServer` specs to hand to the provider
        (kiro-cli): each spec runs the trusted relay against a per-server socket,
        so kiro-cli never sees the untrusted command/env. Replaces any current
        set for *session_id*. Raises :class:`McpSpawnError` if any server fails
        to spawn; every server started during the failed attempt is reaped first.
        An empty list tears the session's set down and returns ``[]``.
        """
        if servers and not os.path.isabs(cwd):
            raise McpSpawnError(servers[0].name, "session cwd must be absolute")
        if servers and not platform_compat.IS_POSIX:
            raise McpSpawnError(servers[0].name, "client MCP servers require a POSIX host")
        async with self._lock:
            await self._teardown_locked(session_id)
            if not servers:
                return []
            workspace_fd = await _bind_workspace_async(cwd, servers[0].name)
            self._workspace_fds[session_id] = workspace_fd
            if self._proxy_root is None:
                self._proxy_root = tempfile.mkdtemp(prefix="mca-", dir=_proxy_root_parent())
                platform_compat.restrict_dir_to_owner(self._proxy_root)
            session_dir = tempfile.mkdtemp(prefix="s-", dir=self._proxy_root)
            platform_compat.restrict_dir_to_owner(session_dir)
            self._dirs[session_id] = session_dir
            spawned: list[_RunningServer] = []
            proxies: list[StdioMcpServer] = []
            try:
                for index, server in enumerate(servers):
                    running, proxy = await self._host_one(
                        session_dir,
                        index,
                        server,
                        cwd=cwd,
                        workspace_fd=workspace_fd,
                    )
                    spawned.append(running)
                    proxies.append(proxy)
            except asyncio.CancelledError:
                for running in spawned:
                    running.kill_now()
                self._dirs.pop(session_id, None)
                await asyncio.to_thread(_rmtree, session_dir)
                await self._release_workspace_locked(session_id)
                await self._drop_proxy_root_if_empty()
                raise
            except Exception:
                for running in spawned:
                    with contextlib.suppress(Exception):
                        await running.terminate()
                self._dirs.pop(session_id, None)
                await asyncio.to_thread(_rmtree, session_dir)
                await self._release_workspace_locked(session_id)
                await self._drop_proxy_root_if_empty()
                raise
            self._sessions[session_id] = spawned
            logger.info(
                "hosted %d stdio MCP server(s) for ACP session %s (sandboxed + proxied)",
                len(spawned),
                session_id,
            )
            return proxies

    async def teardown(self, session_id: str) -> None:
        """Terminate and forget one session's servers. Safe if none are hosted."""
        async with self._lock:
            await self._teardown_locked(session_id)

    async def shutdown(self) -> None:
        """Terminate every hosted server and remove the shared proxy root."""
        async with self._lock:
            for session_id in list(self._sessions):
                await self._teardown_locked(session_id)
            proxy_root = self._proxy_root
            self._proxy_root = None
            if proxy_root:
                await asyncio.to_thread(_rmtree, proxy_root)

    def hosted(self, session_id: str) -> list[str]:
        """Names of the servers currently hosted for *session_id* (read-only)."""
        return [running.name for running in self._sessions.get(session_id, [])]

    async def _teardown_locked(self, session_id: str) -> None:
        running_servers = self._sessions.get(session_id, [])
        try:
            for running in running_servers:
                with contextlib.suppress(Exception):
                    await running.terminate()
        except asyncio.CancelledError:
            for running in running_servers:
                running.kill_now()
            raise
        finally:
            self._sessions.pop(session_id, None)
            session_dir = self._dirs.pop(session_id, None)
            if session_dir:
                await asyncio.to_thread(_rmtree, session_dir)
            await self._release_workspace_locked(session_id)
            await self._drop_proxy_root_if_empty()

    async def _release_workspace_locked(self, session_id: str) -> None:
        descriptor = self._workspace_fds.pop(session_id, None)
        if descriptor is not None:
            await release_bound_agent_workspace(descriptor)

    async def _drop_proxy_root_if_empty(self) -> None:
        if not self._dirs and self._proxy_root:
            proxy_root = self._proxy_root
            self._proxy_root = None
            await asyncio.to_thread(_rmtree, proxy_root)

    async def _host_one(
        self,
        session_dir: str,
        index: int,
        server: StdioMcpServer,
        *,
        cwd: str,
        workspace_fd: int,
    ) -> tuple[_RunningServer, StdioMcpServer]:
        proc, sandbox_cleanup = await self._spawn(
            server,
            self._proxy_root or session_dir,
            cwd=cwd,
            workspace_fd=workspace_fd,
        )
        running = _RunningServer(
            name=server.name,
            proc=proc,
            process_group_id=(
                proc.pid
                if platform_compat.IS_POSIX and isinstance(proc.pid, int) and proc.pid > 1
                else None
            ),
            sandbox_cleanup=sandbox_cleanup,
            source=server,
            cwd=cwd,
            workspace_fd=workspace_fd,
        )
        try:
            # Confirm liveness BEFORE starting the perpetual stderr drain:
            # _assert_alive reads the child's stderr tail when it exits
            # immediately, so the drain must not consume those bytes first (that
            # would strand the failure with an empty diagnostic). Once the child
            # is up, start draining so a chatty child's stderr pipe can never
            # fill and block it.
            await self._assert_alive(running)
            running.stderr_drain = asyncio.ensure_future(_drain_stderr(proc))
            proxy = await self._start_proxy(session_dir, index, running)
        except asyncio.CancelledError:
            running.kill_now()
            raise
        except Exception:
            with contextlib.suppress(Exception):
                await running.terminate()
            raise
        return running, proxy

    async def _assert_alive(self, running: _RunningServer) -> None:
        """Liveness gate that does NOT read the child's stdout.

        The child's stdout belongs to the proxy relay (it carries kiro-cli's
        initialize response), so liveness can only mean "did not exit within the
        grace window". A child that exits immediately failed to start; its
        (redacted) stderr tail is attached to the error.
        """
        proc = running.proc
        try:
            await asyncio.wait_for(proc.wait(), timeout=self._liveness_grace)
        except asyncio.TimeoutError:
            return  # still running after the grace window == up
        detail = await _stderr_tail(proc)
        raise McpSpawnError(
            running.name,
            f"exited immediately (code {proc.returncode})",
            detail=detail,
        )

    async def _start_proxy(
        self, session_dir: str, index: int, running: _RunningServer
    ) -> StdioMcpServer:
        """Create the per-server socket + token and start relaying to the child."""
        socket_path = os.path.join(session_dir, f"{index}.sock")
        token_file = os.path.join(session_dir, f"{index}.tok")
        token = secrets.token_hex(32)
        # 0600 token file — the capability the trusted proxy presents. The token
        # is never placed in argv or the stored config; only the file PATH is.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(token_file, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token)
        running.token = token
        running.socket_path = socket_path

        server = await asyncio.start_unix_server(
            lambda r, w: self._on_proxy_conn(running, r, w), path=socket_path
        )
        with contextlib.suppress(OSError):
            os.chmod(socket_path, 0o600)
        running.server = server

        return StdioMcpServer(
            name=running.name,
            command=sys.executable,
            args=[_PROXY_PATH, "--socket", socket_path],
            env={
                _PROXY_SOCKET_ENV: socket_path,
                _PROXY_TOKEN_FILE_ENV: token_file,
            },
        )

    async def _restart_child(self, running: _RunningServer) -> None:
        """Replace a child before a new proxy generation starts its MCP lifecycle."""
        source = running.source
        if source is None:
            raise McpSpawnError(running.name, "source configuration is unavailable")
        await running.stop_child()
        proc, sandbox_cleanup = await self._spawn(
            source,
            self._proxy_root or os.path.dirname(running.socket_path or ""),
            cwd=running.cwd,
            workspace_fd=running.workspace_fd,
        )
        replacement = _RunningServer(
            name=running.name,
            proc=proc,
            process_group_id=(
                proc.pid
                if platform_compat.IS_POSIX and isinstance(proc.pid, int) and proc.pid > 1
                else None
            ),
            sandbox_cleanup=sandbox_cleanup,
            source=source,
            cwd=running.cwd,
            workspace_fd=running.workspace_fd,
        )
        try:
            await self._assert_alive(replacement)
        except BaseException:
            await replacement.stop_child()
            raise
        running.proc = proc
        running.process_group_id = replacement.process_group_id
        running.process_group_reaped = False
        running.sandbox_cleanup = sandbox_cleanup
        running.stderr_drain = asyncio.ensure_future(_drain_stderr(proc))

    async def _on_proxy_conn(
        self,
        running: _RunningServer,
        sock_reader: asyncio.StreamReader,
        sock_writer: asyncio.StreamWriter,
    ) -> None:
        """Authenticate one proxy connection, then relay it to a fresh lifecycle."""
        try:
            line = await asyncio.wait_for(sock_reader.readline(), timeout=_AUTH_TIMEOUT_SECS)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(Exception):
                sock_writer.close()
            return
        presented = line.decode("utf-8", "replace").strip()
        if not running.token or not hmac.compare_digest(presented, running.token):
            logger.warning("rejected unauthenticated proxy connection for %s", running.name)
            with contextlib.suppress(Exception):
                sock_writer.close()
            return
        try:
            async with running.reconnect_lock:
                if running.closed:
                    sock_writer.close()
                    return
                # A provider recreation starts a new MCP lifecycle. Stop every
                # prior relay first, then replace the initialized child before
                # the fresh proxy can send initialize.
                prior = list(running.conns)
                for task in prior:
                    task.cancel()
                if prior:
                    await asyncio.gather(*prior, return_exceptions=True)
                if running.connected_once:
                    await self._restart_child(running)
                running.connected_once = True
                task = asyncio.ensure_future(self._relay(running, sock_reader, sock_writer))
                running.conns.add(task)
                task.add_done_callback(running.conns.discard)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                sock_writer.close()
            raise
        except Exception:
            logger.exception("failed to restart MCP child for proxy reconnect: %s", running.name)
            with contextlib.suppress(Exception):
                sock_writer.close()
            return
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _relay(
        self,
        running: _RunningServer,
        sock_reader: asyncio.StreamReader,
        sock_writer: asyncio.StreamWriter,
    ) -> None:
        """Bidirectionally copy bytes between the socket and the child's stdio."""
        proc = running.proc
        if proc.stdin is None or proc.stdout is None:
            with contextlib.suppress(Exception):
                sock_writer.close()
            return
        stdin = proc.stdin
        stdout = proc.stdout

        # socket -> child stdin. A proxy disconnect does not close stdin here;
        # the reconnect path owns orderly child replacement after both pumps stop.
        to_child = asyncio.ensure_future(
            _pump(sock_reader.read, stdin.write, stdin.drain, close_fn=None)
        )
        # child stdout -> socket. Close the socket writer when the child closes.
        to_editor = asyncio.ensure_future(
            _pump(stdout.read, sock_writer.write, sock_writer.drain, close_fn=sock_writer.close)
        )
        try:
            await asyncio.wait({to_child, to_editor}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (to_child, to_editor):
                task.cancel()
            await asyncio.gather(to_child, to_editor, return_exceptions=True)
            with contextlib.suppress(Exception):
                sock_writer.close()

    async def _spawn(
        self,
        server: StdioMcpServer,
        proxy_root: str,
        *,
        cwd: str,
        workspace_fd: int,
    ) -> tuple[asyncio.subprocess.Process, str | None]:
        """Spawn one stdio server through the sandbox chokepoint (no init here).

        On any failure the child is reaped before the error propagates. The
        command/args/env are applied with exact stdio semantics and no shell. The
        strict sandbox hides host credential stores plus the gateway secret/.env
        from this untrusted child.
        """
        # Additive env: Kiro Crew's PATH-augmented base, then the client's own
        # entries (PATH prepended). sandboxed_spawn_argv scrubs credential env on
        # top of this, so a client cannot smuggle host secrets into the child.
        env = dict(os.environ)
        env["PATH"] = await asyncio.to_thread(augmented_path, env.get("PATH", ""))
        client_env = sanitize_spec_env(server.env.items())
        if "PATH" in client_env:
            env["PATH"] = client_env["PATH"] + os.pathsep + env["PATH"]
        env.update({k: v for k, v in client_env.items() if k != "PATH"})

        resolved = await asyncio.to_thread(shutil.which, server.command, path=env.get("PATH"))
        if not resolved:
            raise McpSpawnError(server.name, "command not found")

        hidden_paths = await asyncio.to_thread(_client_hidden_paths)
        hidden_paths = (*hidden_paths, proxy_root)
        wrapped_argv, spawn_env, sandbox_cleanup = await sandboxed_spawn_argv_async(
            [resolved, *server.args],
            mode="strict",
            env=env,
            strip_python_env=True,
            extra_hidden_dirs=hidden_paths,
            _prepare=sandboxed_spawn_argv,
        )
        try:
            proc = await create_subprocess_limited(
                *wrapped_argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                chdir_fd=workspace_fd,
                env=spawn_env,
                start_new_session=platform_compat.IS_POSIX,
                limit=_CHUNK,
            )
        except asyncio.CancelledError:
            if sandbox_cleanup:
                with contextlib.suppress(OSError):
                    Path(sandbox_cleanup).unlink(missing_ok=True)
            raise
        except Exception as exc:
            if sandbox_cleanup:
                with contextlib.suppress(OSError):
                    Path(sandbox_cleanup).unlink(missing_ok=True)
            raise McpSpawnError(server.name, f"spawn failed: {type(exc).__name__}") from exc
        return proc, sandbox_cleanup


def _rmtree(path: str) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, ignore_errors=True)


async def _pump(read, write_fn, drain_fn, *, close_fn) -> None:
    """Copy chunks from *read* to the writer until EOF, then optionally close."""
    try:
        while True:
            data = await read(_CHUNK)
            if not data:
                break
            write_fn(data)
            await drain_fn()
    except (ConnectionError, OSError, asyncio.CancelledError):
        pass
    finally:
        if close_fn is not None:
            with contextlib.suppress(Exception):
                close_fn()


async def _drain_stderr(proc: asyncio.subprocess.Process) -> None:
    """Continuously discard a hosted child's stderr so its pipe never blocks it.

    A redacted tail is logged at debug for diagnostics; the bytes are never
    surfaced to the client from here (that is _stderr_tail's job on failure).
    """
    if proc.stderr is None:
        return
    with contextlib.suppress(Exception):
        while True:
            chunk = await proc.stderr.read(_CHUNK)
            if not chunk:
                break
            if logger.isEnabledFor(logging.DEBUG):
                text = chunk.decode("replace").strip()
                if text:
                    logger.debug(
                        "mcp child stderr: %s",
                        redact_log_via_context(text)[:_STDERR_TAIL_MAX],
                    )


async def _stderr_tail(proc: asyncio.subprocess.Process) -> str:
    """Best-effort, redacted tail of a failed server's stderr for diagnostics."""
    if proc.stderr is None:
        return ""
    try:
        raw = await asyncio.wait_for(proc.stderr.read(_STDERR_TAIL_BYTES), timeout=1.0)
    except (asyncio.TimeoutError, Exception):
        return ""
    text = raw.decode(errors="replace").strip()
    if not text:
        return ""
    return redact_via_context(text)[:_STDERR_TAIL_MAX]
