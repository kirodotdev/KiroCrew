"""Host-side MCP stdio bridge for a containerized kiro-cli.

kiro-cli inside the container cannot reach managed MCP servers: those
processes callback to the gateway on ``127.0.0.1``, which is the container,
and the image does not ship ``kiro_crew``. Running them on the HOST and
piping stdio across a unix socket (native Linux) or a 127.0.0.1 TCP
listener (Docker Desktop) keeps ``_api_base()`` on loopback, needs no
``kirocrew`` in the image, and does not open the dashboard on the LAN.

The socket directory is bind-mounted into the container (injected after the
sensitive-path screen — see ``devcontainer.write_build_config``). Connecting
to a socket here is the intended capability of a trusted container: it
reaches the same managed servers a host session would spawn.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import secrets
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway.session_servers import _acp_server_entry

logger = logging.getLogger(__name__)

#: Container-side mount point. Short on purpose: AF_UNIX paths cap around 108
#: bytes, and a data-home path plus two tokens already overflows it.
MCP_BRIDGE_CONTAINER_DIR = "/tmp/kirocrew-mcp-bridge"
MCP_BRIDGE_CLIENT_NAME = "client.py"
_CLIENT_SOURCE = Path(__file__).with_name("devcontainer_mcp_client.py")

#: Managed servers whose host process this bridge stands in for. Names must
#: match ``agent._MANAGED_MCP_SERVERS`` so session-injected entries outrank
#: the spec's own copies (and the pooling stubs, which cannot work in here).
_BRIDGED_SERVERS: tuple[tuple[str, str], ...] = (
    ("kirocrew-core", "mcp-core"),
    ("kirocrew-cron", "mcp-cron"),
    ("kirocrew-computer", "mcp-computer"),
)

#: Per-runtime secret length and the accept-loop wait for the client to offer it.
#: Connecting to the socket or TCP port is not enough; the host MCP child is
#: spawned only after this handshake. Sibling containers that can reach
#: ``host.docker.internal`` still cannot invoke MCP without the secret.
_SECRET_NBYTES = 32
_HANDSHAKE_TIMEOUT_SECS = 5.0


def host_bridge_dir(project_dir: str | Path) -> Path:
    """Gateway-owned directory bind-mounted into this project's container.

    Token-keyed so two projects cannot see each other's sockets. Lives under
    ``/tmp`` on POSIX rather than the data home: a data-home path is both a
    sensitive-path hit (if screened) and too long for AF_UNIX. ``gettempdir()``
    is also too long on macOS (``/var/folders/...``).
    """
    from kiro_crew.devcontainer import _project_token

    if sys.platform != "win32" and os.path.isdir("/tmp"):
        base = Path("/tmp")
    else:
        base = Path(tempfile.gettempdir())
    return base / "kirocrew-mcp" / _project_token(project_dir)


def uses_unix_bridge() -> bool:
    """True when bind-mounted AF_UNIX sockets share a kernel with the gateway.

    Docker Desktop (macOS / Windows) runs containers in a VM: a host unix
    socket bind-mounted across virtiofs is not connectable. Those hosts use
    TCP to ``host.docker.internal`` instead.
    """
    return sys.platform == "linux"


def container_socket_path(runtime_token: str, subcommand: str) -> str:
    """Socket path as the in-container client sees it."""
    return f"{MCP_BRIDGE_CONTAINER_DIR}/{runtime_token}.{subcommand}.sock"


def host_socket_path(project_dir: str | Path, runtime_token: str, subcommand: str) -> Path:
    return host_bridge_dir(project_dir) / f"{runtime_token}.{subcommand}.sock"


#: Injected on native Linux so a TCP client can reach host loopback the same
#: way Docker Desktop already exposes ``host.docker.internal``. Not
#: ``--network=host``.
HOST_GATEWAY_RUNARG = "--add-host=host.docker.internal:host-gateway"


def inject_host_gateway(parsed: dict) -> None:
    """Append the host-gateway extra_hosts entry after the sensitive-path screen."""
    if not uses_unix_bridge():
        return
    raw = parsed.get("runArgs")
    args: list[Any] = list(raw) if isinstance(raw, list) else []
    if any(str(a).startswith("--add-host=host.docker.internal:") for a in args):
        parsed["runArgs"] = args
        return
    args.append(HOST_GATEWAY_RUNARG)
    parsed["runArgs"] = args


def ensure_bridge_layout(project_dir: str | Path) -> Path:
    """Create the bind source and copy the stdlib client into it.

    The directory is 0755 and the client is 0644: the container's
    ``remoteUser`` is routinely a different uid than the gateway, and a
    0700/0600 layout would make the client unreadable and the sockets
    unconnectable. The client carries no secrets; the host MCP child is
    what holds ``KIROCREW_HOME``.
    """
    dest = host_bridge_dir(project_dir)
    dest.mkdir(parents=True, exist_ok=True)
    platform_compat.chmod_safe(dest, 0o755)
    client = dest / MCP_BRIDGE_CLIENT_NAME
    data = _CLIENT_SOURCE.read_bytes()
    if not client.is_file() or client.read_bytes() != data:
        tmp = client.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, client)
        platform_compat.chmod_safe(client, 0o644)
    return dest


def remove_bridge_dir(project_dir: str | Path) -> None:
    """Best-effort teardown of one project's bridge directory.

    Only names under our token dir are touched, and a symlink is unlinked as
    a link so a planted one cannot redirect the delete.
    """
    root = host_bridge_dir(project_dir)
    try:
        if root.is_symlink() or not root.is_dir():
            if root.exists() or root.is_symlink():
                root.unlink()
            return
    except OSError:
        return
    try:
        for child in root.iterdir():
            try:
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    continue
            except OSError:
                logger.debug("devcontainer mcp-bridge: could not reap %s", child, exc_info=True)
        root.rmdir()
    except OSError:
        logger.debug("devcontainer mcp-bridge: could not reap %s", root, exc_info=True)


def mount_entry(project_dir: str | Path) -> str:
    """A ``devcontainer.json`` ``mounts`` string for the bridge directory."""
    host = str(ensure_bridge_layout(project_dir))
    return f"source={host},target={MCP_BRIDGE_CONTAINER_DIR},type=bind"


def inject_bridge_mount(parsed: dict, project_dir: str | Path) -> None:
    """Append the bridge bind after the sensitive-path screen has run.

    Do not re-screen the override: the host path is gateway-owned, not a
    project-declared mount of the data home.
    """
    entry = mount_entry(project_dir)
    raw = parsed.get("mounts")
    mounts: list[Any] = list(raw) if isinstance(raw, list) else []
    if any(MCP_BRIDGE_CONTAINER_DIR in str(m) for m in mounts):
        parsed["mounts"] = mounts
        return
    mounts.append(entry)
    parsed["mounts"] = mounts


def session_server_entries(
    runtime_token: str,
    *,
    python: str = "python3",
    transport: str | None = None,
    ports: dict[str, int] | None = None,
    secret: str = "",
) -> list[dict[str, Any]]:
    """ACP ``mcpServers`` entries that launch the in-container client.

    Same-name override shadows the agent spec and the pooling stubs. The
    stubs dial a host-only unix socket and import ``kiro_crew``, so they
    cannot be the path a containerized session takes.

    ``transport`` is ``unix`` (native Linux) or ``tcp`` (Docker Desktop).
    TCP entries need the ports actually bound by ``McpBridge.start``.
    ``secret`` is the per-runtime handshake token; an empty secret ships
    no entries rather than an unauthenticated client.
    """
    if not secret:
        return []
    kind = transport if transport is not None else ("unix" if uses_unix_bridge() else "tcp")
    client = f"{MCP_BRIDGE_CONTAINER_DIR}/{MCP_BRIDGE_CLIENT_NAME}"
    out: list[dict[str, Any]] = []
    for name, subcommand in _BRIDGED_SERVERS:
        if kind == "tcp":
            port = (ports or {}).get(subcommand)
            if port is None:
                continue
            args: list[str] = [client, "tcp", "host.docker.internal", str(port), secret]
        else:
            args = [client, container_socket_path(runtime_token, subcommand), secret]
        shaped = _acp_server_entry(
            name,
            {
                "command": python,
                "args": args,
                "env": {},
            },
        )
        if shaped is not None:
            out.append(shaped)
    return out


def _host_mcp_argv(subcommand: str) -> list[str]:
    """The same invocation a host session's agent spec would spawn."""
    from kiro_crew.agent import _kirocrew_mcp_invocation

    command, args = _kirocrew_mcp_invocation(subcommand)
    return [command, *args]


def _listen_env(session_env: dict[str, str]) -> dict[str, str]:
    """Host environment for one MCP child: gateway env plus the session's keys."""
    env = dict(os.environ)
    env.update(session_env)
    return env


async def _pipe_stdio(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    proc: asyncio.subprocess.Process,
) -> None:
    """Copy one accepted connection onto one host MCP child's stdio."""

    async def to_proc() -> None:
        assert proc.stdin is not None
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    proc.stdin.close()
                    try:
                        await proc.stdin.wait_closed()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                proc.stdin.write(data)
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, ConnectionError):
            pass

    async def from_proc() -> None:
        assert proc.stdout is not None
        try:
            while True:
                data = await proc.stdout.read(65536)
                if not data:
                    return
                writer.write(data)
                await writer.drain()
        except (BrokenPipeError, ConnectionResetError, ConnectionError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    await asyncio.gather(to_proc(), from_proc(), proc.wait())


class McpBridge:
    """Per-runtime accept loops, one listener per managed server."""

    def __init__(
        self,
        project_dir: str,
        runtime_token: str,
        session_env: dict[str, str],
    ) -> None:
        self.project_dir = project_dir
        self.runtime_token = runtime_token
        self._session_env = dict(session_env)
        self._secret = secrets.token_urlsafe(_SECRET_NBYTES)
        self._servers: list[asyncio.AbstractServer] = []
        # Cached so close() does not realpath the project dir on the event loop.
        self._root: Path | None = None
        self._ports: dict[str, int] = {}
        self._unix = uses_unix_bridge()

    def session_servers(self) -> list[dict[str, Any]]:
        if self._unix:
            return session_server_entries(self.runtime_token, transport="unix", secret=self._secret)
        return session_server_entries(
            self.runtime_token, transport="tcp", ports=self._ports, secret=self._secret
        )

    def _socket_path(self, subcommand: str) -> Path:
        root = self._root
        if root is None:
            root = host_bridge_dir(self.project_dir)
        return root / f"{self.runtime_token}.{subcommand}.sock"

    async def start(self) -> None:
        # realpath + mkdir + copy: blocking. The spawn path awaits this on
        # the gateway loop.
        self._root = await asyncio.to_thread(ensure_bridge_layout, self.project_dir)
        env = _listen_env(self._session_env)
        for _name, subcommand in _BRIDGED_SERVERS:
            argv = _host_mcp_argv(subcommand)
            if self._unix:
                self._servers.append(
                    await _serve_unix(self._socket_path(subcommand), argv, env, self._secret)
                )
            else:
                server, port = await _serve_tcp(argv, env, self._secret)
                self._servers.append(server)
                self._ports[subcommand] = port

    async def close(self) -> None:
        for server in self._servers:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                logger.debug("devcontainer mcp-bridge: server close failed", exc_info=True)
        self._servers.clear()
        self._ports.clear()
        root = self._root
        if root is None or not self._unix:
            return
        for _name, subcommand in _BRIDGED_SERVERS:
            path = root / f"{self.runtime_token}.{subcommand}.sock"
            try:
                if path.exists() or path.is_symlink():
                    path.unlink()
            except OSError:
                logger.debug("devcontainer mcp-bridge: unlink %s failed", path, exc_info=True)


async def start_bridge(
    project_dir: str | Path,
    runtime_token: str,
    session_env: dict[str, str] | None = None,
) -> McpBridge:
    """Listen on this runtime's sockets and spawn host MCP children on connect."""
    bridge = McpBridge(str(project_dir), runtime_token, session_env or {})
    await bridge.start()
    return bridge


def _tokens_match(offered: bytes, expected: bytes) -> bool:
    if not offered or not expected or len(offered) != len(expected):
        return False
    return hmac.compare_digest(offered, expected)


async def _accept_handshake(reader: asyncio.StreamReader, secret: str) -> bool:
    """True when the client offered this runtime's secret as its first line."""
    try:
        line = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=_HANDSHAKE_TIMEOUT_SECS)
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return False
    except (BrokenPipeError, ConnectionResetError, ConnectionError):
        return False
    return _tokens_match(line.rstrip(b"\r\n"), secret.encode("ascii"))


def _on_connect_factory(argv: list[str], env: dict[str, str], secret: str) -> Any:
    async def _on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            if not await _accept_handshake(reader, secret):
                logger.warning("devcontainer mcp-bridge: handshake rejected")
                writer.close()
                return
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except Exception:
            logger.exception("devcontainer mcp-bridge: failed to spawn %s", argv)
            writer.close()
            return
        try:
            await _pipe_stdio(reader, writer, proc)
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass
            try:
                writer.close()
            except Exception:
                pass

    return _on_connect


async def _serve_unix(
    path: Path, argv: list[str], env: dict[str, str], secret: str
) -> asyncio.AbstractServer:
    if path.exists() or path.is_symlink():
        path.unlink()

    server = await asyncio.start_unix_server(_on_connect_factory(argv, env, secret), path=str(path))
    # The container user is a different uid; 0600 would refuse the connect.
    try:
        platform_compat.chmod_safe(path, 0o666)
        mode = path.stat().st_mode
        if not stat.S_ISSOCK(mode):
            raise OSError(f"{path} is not a socket")
    except OSError:
        server.close()
        await server.wait_closed()
        raise
    return server


async def _serve_tcp(
    argv: list[str], env: dict[str, str], secret: str
) -> tuple[asyncio.AbstractServer, int]:
    """Listen on 127.0.0.1 with an ephemeral port. Never 0.0.0.0."""
    server = await asyncio.start_server(
        _on_connect_factory(argv, env, secret), host="127.0.0.1", port=0
    )
    sockets = server.sockets
    if not sockets:
        server.close()
        await server.wait_closed()
        raise OSError("tcp bridge bound no sockets")
    port = int(sockets[0].getsockname()[1])
    return server, port
