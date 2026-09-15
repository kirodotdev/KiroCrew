"""Operator-side control of the MCP gateway daemon: find it, describe it, stop it.

The daemon (``mcp_gateway.gatewayd``) is spawned by a gateway process and, since
it carries ``--owner-pid``, exits on its own once that process is gone. This
module is the belt to that suspender: ``kirocrew stop`` / ``kirocrew restart``
call :func:`stop_daemon` so the daemon is gone BEFORE the replacement gateway
starts probing the socket, and ``kirocrew doctor`` calls :func:`describe_daemon`
so an operator can see which code revision the daemon runs next to the one the
gateway runs. Both are synchronous, because the CLI is.

Everything here is best-effort and never raises past its own boundary: a stop
that cannot find a daemon is a no-op, not a failed stop, and a daemon that is
already draining is left to finish.
"""

from __future__ import annotations

import json
import logging
import socket as _socket
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from kiro_crew import platform_compat
from kiro_crew.code_fingerprint import code_fingerprint
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.rewriter import default_socket_path
from kiro_crew.mcp_gateway.shutdown_budget import TOTAL_SHUTDOWN_BUDGET_SECS

logger = logging.getLogger(__name__)

#: One ping round-trip; the daemon answers a ping before touching its pool.
_PING_TIMEOUT_SECS = 2.0

#: The daemon's module path as it appears on its own command line. The pid the
#: pong names is verified against this before any signal is sent, so a pong
#: forged by something else listening on the path cannot aim a SIGTERM.
_DAEMON_ARGV_MARKER = "kiro_crew.mcp_gateway.gatewayd"


@dataclass(frozen=True)
class DaemonInfo:
    """What one ping learned about the daemon on a socket."""

    socket_path: Path
    pid: int
    owner_pid: int
    fingerprint: str
    targets: tuple[str, ...]
    #: The daemon's own ``process_start_time`` as it reported it in the pong.
    #: Empty on a pre-fingerprint daemon; a stop then refuses to signal.
    start_time: str = ""

    @property
    def matches_this_code(self) -> bool:
        return bool(self.fingerprint) and self.fingerprint == code_fingerprint()

    @property
    def owner_alive(self) -> Optional[bool]:
        """Whether the gateway that spawned it still exists; None when unknown."""
        if self.owner_pid <= 0:
            return None
        return platform_compat.pid_exists(self.owner_pid)


def _ping(socket_path: Path) -> Optional[dict[str, Any]]:
    """Blocking ping; the decoded pong or None on any failure."""
    if platform_compat.IS_WINDOWS:
        # The named-pipe transport has no blocking client here; the daemon's own
        # owner-liveness exit covers Windows and the CLI simply reports unknown.
        return None
    try:
        s = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    except OSError:
        return None
    try:
        s.settimeout(_PING_TIMEOUT_SECS)
        s.connect(transport.resolve_address(socket_path))
        s.sendall(b'{"type":"ping"}\n')
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    except OSError:
        return None
    finally:
        try:
            s.close()
        except OSError:
            pass
    line = buf.split(b"\n", 1)[0]
    try:
        msg = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return msg if isinstance(msg, dict) and msg.get("type") == "pong" else None


def configured_socket_path() -> Path:
    """The socket the running gateway actually spawns its daemon on.

    ``mcp_gateway.socket_path`` in config wins when set, exactly as
    ``slack/gateway.py`` resolves it for ``GatewaySpec``; otherwise the data
    home default. Reading the default alone would make ``kirocrew stop`` and
    ``kirocrew doctor`` look at an endpoint nothing listens on whenever an
    operator has moved the socket, and report ``absent`` for a daemon that is
    running. A config that cannot be read falls back to the default rather
    than failing the command that asked.
    """
    try:
        from kiro_crew.config import KiroCrewConfig

        configured = KiroCrewConfig.load().mcp_gateway.socket_path
    except Exception:
        configured = ""
    return Path(configured) if configured else default_socket_path()


def describe_daemon(socket_path: Optional[Path] = None) -> Optional[DaemonInfo]:
    """The daemon serving *socket_path* (default: the configured one), or None."""
    path = Path(socket_path) if socket_path else configured_socket_path()
    pong = _ping(path)
    if pong is None:
        return None
    pid = pong.get("pid")
    owner = pong.get("owner_pid")
    targets = pong.get("targets")
    return DaemonInfo(
        socket_path=path,
        pid=int(pid) if isinstance(pid, int) and not isinstance(pid, bool) else 0,
        owner_pid=int(owner) if isinstance(owner, int) and not isinstance(owner, bool) else 0,
        fingerprint=str(pong.get("fingerprint") or ""),
        targets=tuple(str(t) for t in targets) if isinstance(targets, list) else (),
        start_time=str(pong.get("start_time") or ""),
    )


def _is_daemon_process(pid: int) -> bool:
    if pid <= 0:
        return False
    cmdline = platform_compat.process_command_line(pid)
    return _DAEMON_ARGV_MARKER in cmdline


def stop_daemon(
    socket_path: Optional[Path] = None,
    *,
    wait_secs: float = TOTAL_SHUTDOWN_BUDGET_SECS,
) -> str:
    """SIGTERM the daemon on *socket_path* and wait for it to release the lock.

    Returns one of ``"stopped"`` (it was there and is gone), ``"absent"`` (no
    daemon answered), ``"unverified"`` (a daemon answered but named a pid whose
    command line is not gatewayd's -- nothing signalled), ``"denied"`` (the
    signal was refused), or ``"draining"`` (signalled, but the lock was still
    held when the wait ran out; the daemon finishes on its own).

    SIGTERM, never SIGKILL: the daemon's handler runs ``pool.shutdown_all()``,
    which is what takes the pooled MCP backends down with it. A SIGKILL leaves
    those orphaned -- exactly the population this function exists to clear.
    """
    path = Path(socket_path) if socket_path else configured_socket_path()
    info = describe_daemon(path)
    if info is None:
        return "absent"
    if not _is_daemon_process(info.pid):
        logger.warning(
            "mcp-gateway: the daemon on %s reports pid %d but that process is not "
            "gatewayd; refusing to signal it",
            path,
            info.pid,
        )
        return "unverified"
    # Pinned on the start time the DAEMON reported about itself in the pong,
    # not on one read here: a read after the command-line check has its own
    # window in which the daemon exits and the number is reused, and the
    # replacement's token would then be the one accepted. The pong's token
    # names the process that answered; a mismatch signals nothing and reads
    # as the daemon having exited on its own. A daemon that reported no token
    # is refused rather than guessed at.
    start = info.start_time
    if not start:
        return "unverified"
    # ``kill_pid_pinned`` holds the identity open across the signal on Windows
    # but on POSIX delegates straight to ``os.kill`` -- there is no handle to
    # hold, so the comparison has to be made HERE, immediately before the
    # signal. A token that does not match means the daemon exited and the
    # number was reused: nothing is signalled.
    if not platform_compat.IS_WINDOWS:
        if platform_compat.process_start_time(info.pid) != start:
            return "stopped"
    try:
        if not platform_compat.kill_pid_pinned(info.pid, start, platform_compat.SIGTERM):
            return "stopped"
    except ProcessLookupError:
        return "stopped"
    except PermissionError:
        return "denied"
    deadline = time.monotonic() + max(0.0, wait_secs)
    while time.monotonic() < deadline:
        if transport.singleton_lock_free(path) and not platform_compat.pid_exists(info.pid):
            return "stopped"
        time.sleep(0.1)
    return "draining"
