"""The in-sandbox half of the agent socket: redeem a pickup, then send by capability.

Phase 1 of ``docs/request-for-change/rfc-sandbox-ipc-capability.md``; the gateway
half is :mod:`kiro_crew.dashboard.agent_ipc`. A Kiro Crew server under an enforced
harness is withheld the internal secret, so the gateway hands its element a one-time
pickup instead (:data:`~kiro_crew.agent_ipc_wire.PICKUP_ENV`) beside the pid of the
gateway that issued it (:data:`~kiro_crew.agent_ipc_wire.GATEWAY_PID_ENV`). At
startup this module takes both out of the environment, makes the process
non-dumpable on Linux so a same-uid neighbour cannot read them from ``/proc``, and
redeems the pickup on the agent socket for a capability bound to this process.

Every connection to the agent socket is checked before a byte is written: the
kernel peer pid of the listener must be that gateway pid. The socket file sits in a
data-home directory a sandboxed process can write, so a process that renames it and
binds its own listener at the path receives neither the pickup nor any request.

Once a capability is held, every gateway request this process sends goes to the
agent socket with the capability and without ``X-Internal-Secret``, through
``unix_socket_urlopen``, which has no TCP handler: a capability is never sent
anywhere else. With no pickup, or a redemption the gateway refused, nothing
changes and the request takes today's path.

Process-wide state, deliberately: the capability names THIS process, which serves
the session its element was built for. A pooled backend is spawned by gatewayd
from its own environment, carries no pickup and never holds one.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

from kiro_crew.agent_ipc_wire import (
    CAPABILITY_HEADER,
    GATEWAY_PID_ENV,
    PICKUP_ENV,
    REDEEM_PATH,
    SESSION_PATH,
    agent_socket_path,
)
from kiro_crew.loopback_http import unix_socket_urlopen

logger = logging.getLogger(__name__)

_PR_SET_DUMPABLE = 4
_REDEEM_TIMEOUT_SECS = 10.0

_lock = threading.Lock()
_pickup = ""
_gateway_pid = 0
_capability = ""
_socket = ""


class ListenerNotGateway(Exception):
    """The agent socket's listener is not the gateway that issued the pickup.

    Not an ``OSError`` on purpose: ``unix_socket_urlopen``'s peer hook lets a
    non-``OSError`` through unwrapped, so this is never mistaken for a gateway that
    is merely down.
    """


def _set_non_dumpable() -> None:
    """``prctl(PR_SET_DUMPABLE, 0)`` on Linux; a no-op elsewhere and on failure."""
    if not sys.platform.startswith("linux"):
        return
    try:
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0)
    except Exception:
        logger.debug("agent ipc: prctl(PR_SET_DUMPABLE) unavailable", exc_info=True)


def take_pickup(environ: "os._Environ[str] | dict[str, str] | None" = None) -> bool:
    """Move the pickup and gateway pid out of the environment. Returns whether both were found.

    Called once at server startup, before any request is read, so a child this process
    starts later inherits neither. A pickup with no gateway pid to check the listener
    against is dropped: it would have to be sent to whoever answers.
    """
    global _pickup, _gateway_pid
    env = os.environ if environ is None else environ
    value = env.pop(PICKUP_ENV, "")
    raw_pid = env.pop(GATEWAY_PID_ENV, "")
    if not value:
        return False
    _set_non_dumpable()
    if not raw_pid.isdigit() or int(raw_pid) <= 1:
        logger.warning("agent ipc: a pickup arrived without its gateway pid; not redeeming it")
        return False
    with _lock:
        _pickup = value
        _gateway_pid = int(raw_pid)
    return True


def _peer_check(expected_pid: int) -> Callable[[socket.socket], None]:
    def _verify(sock: socket.socket) -> None:
        from kiro_crew.mcp_gateway.socketsec import get_peer_pid

        if get_peer_pid(sock) != expected_pid:
            raise ListenerNotGateway("the agent socket's listener is not the issuing gateway")

    return _verify


def _open(req: urllib.request.Request, timeout: float, socket_path: str, gateway_pid: int):
    return unix_socket_urlopen(
        req, timeout=timeout, socket_path=socket_path, verify_peer=_peer_check(gateway_pid)
    )


def _redeem_locked(port_fn: Callable[[], int]) -> None:
    """Spend the held pickup. Caller holds ``_lock``. Keeps it on a transport failure."""
    global _pickup, _capability, _socket
    try:
        sock = str(agent_socket_path(port_fn()))
    except Exception:
        logger.warning("agent ipc: could not resolve the agent socket", exc_info=True)
        return
    req = urllib.request.Request(
        f"http://127.0.0.1{REDEEM_PATH}",
        data=json.dumps({"pickup": _pickup}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _open(req, _REDEEM_TIMEOUT_SECS, sock, _gateway_pid) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        # The gateway answered and refused: the pickup will not become good later.
        logger.warning("agent ipc: the gateway refused the pickup (HTTP %s)", exc.code)
        _pickup = ""
        return
    except ListenerNotGateway:
        # Someone else holds the path. The pickup was not sent; try again later, when
        # the gateway may have its socket back.
        logger.warning("agent ipc: the agent socket is not served by the gateway")
        return
    except Exception as exc:
        # No gateway answered (an older one without the socket, or one restarting).
        # Today's path stays in force, and the next request tries again.
        logger.info("agent ipc: redemption not completed (%s)", exc)
        return
    capability = body.get("capability") if isinstance(body, dict) else None
    _pickup = ""
    if isinstance(capability, str) and capability:
        _capability = capability
        _socket = sock


def current_capability(port_fn: Callable[[], int]) -> tuple[str, str]:
    """The ``(capability, agent socket path)`` this process holds, redeeming if due.

    ``("", "")`` when it holds none, which means today's path.
    """
    with _lock:
        if not _capability and _pickup:
            _redeem_locked(port_fn)
        return _capability, _socket


def redeem_at_startup(port_fn: Callable[[], int]) -> None:
    """Take the pickup and redeem it, before the server reads its first request."""
    if take_pickup():
        current_capability(port_fn)


def capability_request(
    req: "urllib.request.Request | str", capability: str
) -> urllib.request.Request:
    """*req* carrying *capability* in place of any internal secret."""
    out = urllib.request.Request(req) if isinstance(req, str) else req
    # urllib stores header names capitalized; remove both spellings it may hold.
    for name in ("X-internal-secret", "X-Internal-Secret"):
        out.remove_header(name)
    out.add_header(CAPABILITY_HEADER, capability)
    return out


def open_with_capability(
    req: "urllib.request.Request | str", timeout: float, *, capability: str, socket_path: str
):
    """Send *req* to the agent socket only, with *capability*. Never falls back to TCP.

    The listener must be the issuing gateway (:func:`_peer_check`); otherwise this
    raises ``URLError`` before the request is written.
    """
    with _lock:
        gateway_pid = _gateway_pid
    try:
        return _open(capability_request(req, capability), timeout, socket_path, gateway_pid)
    except ListenerNotGateway as exc:
        # Reported to the caller's transport handling as an undelivered request.
        raise urllib.error.URLError(str(exc)) from None


def capability_session_key() -> str:
    """The session this process's capability acts for now, as the gateway says, or ``""``.

    The identity source for a server under the credential mask, which cannot verify
    the signed token mapping because the trust root is masked. ``""`` when no
    capability is held or the gateway does not answer; never raises.
    """
    with _lock:
        capability, sock, gateway_pid = _capability, _socket, _gateway_pid
    if not capability:
        return ""
    req = capability_request(urllib.request.Request(f"http://127.0.0.1{SESSION_PATH}"), capability)
    try:
        with _open(req, _REDEEM_TIMEOUT_SECS, sock, gateway_pid) as resp:
            body = json.loads(resp.read())
    except Exception:
        return ""
    key = body.get("session_key") if isinstance(body, dict) else None
    return key if isinstance(key, str) else ""


def _reset_for_tests() -> None:
    global _pickup, _capability, _socket, _gateway_pid
    with _lock:
        _pickup = _capability = _socket = ""
        _gateway_pid = 0
