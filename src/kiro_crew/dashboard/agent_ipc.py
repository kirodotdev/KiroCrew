"""The agent socket: connection-bound capabilities for Kiro Crew's in-sandbox servers.

Phase 1 of ``docs/request-for-change/rfc-sandbox-ipc-capability.md``. A Kiro Crew
MCP server such as ``kirocrew-core`` calls the gateway's internal routes on every
tool call. Under an ENFORCED harness (codex, opencode, goose, pi) the OS credential
mask hides ``run/`` and ``.local_secret`` from the harness's whole process tree
(``sandbox_leaves._CREW_CHILD_WITHHELD_LEAVES``), so the server holds no internal
secret and every call is refused ``internal_secret_missing``. This module
gives such a server a credential that is not a file and not a bearer value:

1. **Issue.** When the gateway builds a session's ``mcpServers`` array it mints a
   one-time *pickup* for each Kiro Crew server element and puts it on that
   element's own ``env`` (:data:`~kiro_crew.agent_ipc_wire.PICKUP_ENV`). The pickup
   is bound to the session's stub token and to a callable answering the session
   runtime's root pid.
2. **Redeem.** At startup the server sends the pickup over the agent socket
   (:data:`~kiro_crew.agent_ipc_wire.REDEEM_PATH`). The gateway accepts it when the
   pickup is known, the kernel says the peer runs as our uid, and the peer's real
   ancestry reaches the runtime root. It answers with a *capability* bound to the
   peer's pid and process start token.
3. **Use.** Every later request on the agent socket carries the capability. It is
   admitted only from the process it was bound to, only on an internal route, and
   only for the session the bound token currently names. Anything else on this
   socket is refused: there is no secret, cookie or bypass path here.

What the gateway keeps is in memory only: sha256 digests of pickups and
capabilities, never the values. A gateway restart forgets every one, which is the
same lifetime as the sessions they were issued for.

What this does NOT do is in the RFC's later phases: it does not hide the internal
secret from the sandbox, move the dashboard socket, or split the owner secret.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
import threading
import weakref
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.agent_ipc_wire import (
    CAPABILITY_HEADER,
    REDEEM_PATH,
    SESSION_PATH,
    agent_socket_path,
)
from kiro_crew.dashboard.origin import is_proxied_request
from kiro_crew.dashboard.token_auth_paths import _deny, _log_auth
from kiro_crew.executors import subprocess_executor
from kiro_crew.instances.run_marker import pid_start_token
from kiro_crew.mcp_gateway.socketsec import (
    PeerCredResult,
    check_peer_is_self,
    chmod_socket_0600,
    get_peer_pid,
)
from kiro_crew.sel import sel as _sel_fn
from kiro_crew.session_token_sig import verify_session_token

logger = logging.getLogger(__name__)

#: Request key the gate sets on an admitted request. ``token_auth`` reads it to
#: take the internal-grant arm without an ``X-Internal-Secret`` header.
GRANT_KEY = "ipc_capability_grant"

#: App key of the holder the listener records the bound agent socket path in, so
#: shutdown unlinks the file this app bound.
AGENT_SOCKET_HOLDER_KEY = "agent_ipc_socket_holder"

#: Bound on live pickups. A pickup is minted per session array, so this is far above
#: any real gateway's session count; past it the oldest is dropped, which costs that
#: session's server its agent-socket path and nothing else.
_MAX_PICKUPS = 4096

#: How far up the process tree a redemption looks for the runtime root. A Kiro Crew
#: server sits a handful of levels under it (sandbox launcher, harness, adapter).
_MAX_ANCESTRY = 64

#: Largest redeem body read. The body is one short JSON object.
_MAX_REDEEM_BODY = 1024

#: Refusal codes, stable for the client to dispatch on.
REFUSE_PICKUP_UNKNOWN = "ipc_pickup_unknown"
REFUSE_PICKUP_HELD = "ipc_pickup_held"
REFUSE_NOT_IN_SESSION = "ipc_peer_not_in_session"
REFUSE_PEER_UNVERIFIED = "ipc_peer_unverified"
REFUSE_CAPABILITY = "ipc_capability_invalid"
REFUSE_SESSION_UNBOUND = "ipc_session_unbound"
REFUSE_SESSION_MISMATCH = "ipc_session_mismatch"
REFUSE_ROUTE = "ipc_route_not_served"
REFUSE_SECRET = "ipc_secret_not_accepted"


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).digest()


@dataclass
class _Holder:
    pid: int
    start: str
    capability: bytes


@dataclass
class _Pickup:
    server: str
    session_token: str
    runtime_pid: Callable[[], int | None]
    holder: _Holder | None = None


@dataclass(frozen=True)
class Binding:
    """What an admitted capability names: one server of one session's runtime."""

    server: str
    session_token: str
    pid: int


def _default_ppid(pid: int) -> int:
    return platform_compat.get_ppid(pid)


class CapabilityStore:
    """Pickups and the capabilities redeemed from them, by digest, in memory.

    Thread-safe: issue runs on the session spawn path (off the loop), redeem and
    verify run on the subprocess executor.
    """

    def __init__(
        self,
        *,
        ppid_fn: Callable[[int], int] = _default_ppid,
        start_fn: Callable[[int], str] = pid_start_token,
    ) -> None:
        self._lock = threading.Lock()
        self._pickups: OrderedDict[bytes, _Pickup] = OrderedDict()
        self._caps: dict[bytes, bytes] = {}
        self._ppid = ppid_fn
        self._start = start_fn

    def issue(
        self,
        *,
        server: str,
        session_token: str,
        runtime_pid: Callable[[], int | None],
        owner: object | None = None,
    ) -> str:
        """Mint a pickup for *server* of the session *session_token* names.

        *owner*, when given, is the object whose lifetime the pickup shares: once it
        is collected the pickup and any capability redeemed from it are dropped.
        """
        if not server or not session_token:
            raise ValueError("a pickup needs a server and a session token")
        pickup = secrets.token_hex(32)
        key = _digest(pickup)
        with self._lock:
            self._pickups[key] = _Pickup(server, session_token, runtime_pid)
            while len(self._pickups) > _MAX_PICKUPS:
                _old_key, old = self._pickups.popitem(last=False)
                if old.holder is not None:
                    self._caps.pop(old.holder.capability, None)
        if owner is not None:
            weakref.finalize(owner, self._retire, key)
        return pickup

    def _retire(self, key: bytes) -> None:
        with self._lock:
            entry = self._pickups.pop(key, None)
            if entry is not None and entry.holder is not None:
                self._caps.pop(entry.holder.capability, None)

    def _descends_from(self, pid: int, root: int) -> bool:
        """Whether *root* is a strict ancestor of *pid* in the real process tree."""
        current = pid
        for _ in range(_MAX_ANCESTRY):
            try:
                current = self._ppid(current)
            except (OSError, ValueError):
                return False
            if current <= 1:
                return False
            if current == root:
                return True
        return False

    def _alive(self, holder: _Holder) -> bool:
        try:
            return bool(holder.start) and self._start(holder.pid) == holder.start
        except Exception:
            return False

    def redeem(self, pickup: str, peer_pid: int) -> tuple[str, str]:
        """Exchange *pickup* for a capability bound to *peer_pid*.

        Returns ``(capability, "")`` or ``("", refusal_code)``. Blocking (/proc).
        """
        if not pickup or peer_pid <= 1:
            return "", REFUSE_PICKUP_UNKNOWN
        key = _digest(pickup)
        with self._lock:
            entry = self._pickups.get(key)
        if entry is None:
            return "", REFUSE_PICKUP_UNKNOWN
        try:
            root = entry.runtime_pid()
        except Exception:
            root = None
        if not root or root <= 1 or not self._descends_from(peer_pid, root):
            return "", REFUSE_NOT_IN_SESSION
        try:
            start = self._start(peer_pid)
        except Exception:
            start = ""
        if not start:
            return "", REFUSE_PEER_UNVERIFIED
        capability = secrets.token_hex(32)
        cap_key = _digest(capability)
        with self._lock:
            # Re-read under the lock: a concurrent retire or redeem may have won.
            entry = self._pickups.get(key)
            if entry is None:
                return "", REFUSE_PICKUP_UNKNOWN
            previous = entry.holder
            if previous is not None and previous.pid != peer_pid and self._alive(previous):
                return "", REFUSE_PICKUP_HELD
            if previous is not None:
                self._caps.pop(previous.capability, None)
            entry.holder = _Holder(peer_pid, start, cap_key)
            self._caps[cap_key] = key
        return capability, ""

    def verify(self, capability: str, peer_pid: int) -> tuple[Binding | None, str]:
        """The binding *capability* names, iff it was issued to *peer_pid* as it is now."""
        if not capability:
            return None, REFUSE_CAPABILITY
        cap_key = _digest(capability)
        with self._lock:
            key = self._caps.get(cap_key)
            entry = self._pickups.get(key) if key is not None else None
            holder = entry.holder if entry is not None else None
        if entry is None or holder is None or holder.capability != cap_key:
            return None, REFUSE_CAPABILITY
        if holder.pid != peer_pid or not self._alive(holder):
            return None, REFUSE_CAPABILITY
        return Binding(entry.server, entry.session_token, peer_pid), ""


#: The gateway's one store. Process-global because the session runtimes that issue
#: pickups and the listener that redeems them live in the same gateway process.
STORE = CapabilityStore()

#: Agent socket paths this process has bound, as strings, so a request can be
#: placed on the agent socket by the listening name its connection was accepted on.
_BOUND_PATHS: set[str] = set()
_BOUND_LOCK = threading.Lock()


def issue_pickup(
    *,
    server: str,
    session_token: str,
    runtime_pid: Callable[[], int | None],
    owner: object | None = None,
) -> str:
    """Mint a pickup on the gateway's store. See :meth:`CapabilityStore.issue`."""
    return STORE.issue(
        server=server, session_token=session_token, runtime_pid=runtime_pid, owner=owner
    )


def agent_socket_bound() -> bool:
    """Whether this process serves an agent socket a pickup could be redeemed on."""
    with _BOUND_LOCK:
        return bool(_BOUND_PATHS)


def request_on_agent_socket(request: web.Request) -> bool:
    """Whether *request* was accepted on an agent socket this process bound.

    Read from the listening name of the accepted ``AF_UNIX`` connection, which the
    kernel reports and the caller cannot choose. Never raises.
    """
    transport = getattr(request, "transport", None)
    if transport is None:
        return False
    try:
        sockname = transport.get_extra_info("sockname")
    except Exception:
        return False
    if isinstance(sockname, bytes):
        sockname = sockname.decode("utf-8", "surrogateescape")
    if not isinstance(sockname, str) or not sockname:
        return False
    with _BOUND_LOCK:
        return sockname in _BOUND_PATHS


def _audit(request: web.Request, *, outcome: str, error: str, caller: str = "agent-ipc") -> None:
    try:
        _sel_fn().log_api_access(
            caller=caller,
            forwarded=is_proxied_request(request),
            operation="agent_ipc",
            outcome=outcome,
            source="agent_ipc",
            resources=request.path,
            error=error,
        )
    except Exception:
        logger.warning("agent ipc: could not record a security event", exc_info=True)


def _refuse(request: web.Request, code: str, detail: str) -> web.Response:
    _audit(request, outcome="denied", error=f"{code}: {detail}")
    _log_auth(request, "agent-ipc", "denied", code)
    return _deny(request, "Forbidden", code)


async def _redeem(request: web.Request, peer_pid: int) -> web.StreamResponse:
    if request.method != "POST":
        return _refuse(request, REFUSE_PICKUP_UNKNOWN, "redeem is POST only")
    if request.content_length is None or request.content_length > _MAX_REDEEM_BODY:
        return _refuse(request, REFUSE_PICKUP_UNKNOWN, "redeem body missing or too large")
    try:
        body = await request.json()
    except Exception:
        body = None
    pickup = body.get("pickup") if isinstance(body, dict) else None
    if not isinstance(pickup, str):
        return _refuse(request, REFUSE_PICKUP_UNKNOWN, "no pickup in the body")
    capability, code = await asyncio.get_running_loop().run_in_executor(
        subprocess_executor(), partial(STORE.redeem, pickup, peer_pid)
    )
    if not capability:
        return _refuse(request, code, f"peer_pid={peer_pid}")
    _audit(request, outcome="granted", error=f"redeemed peer_pid={peer_pid}")
    return web.json_response({"capability": capability})


async def agent_socket_gate(request: web.Request, *, internal: bool) -> web.StreamResponse | None:
    """Decide a request that arrived on the agent socket.

    Returns a response to send (a refusal, or the redeem answer), or ``None`` with
    ``request[GRANT_KEY]`` set when a capability admitted it onto an internal route.
    Fails closed: every path out that is not a verified capability is a refusal.
    """
    transport = getattr(request, "transport", None)
    sock: Any = transport.get_extra_info("socket") if transport is not None else None
    if sock is None or check_peer_is_self(sock) is not PeerCredResult.MATCH:
        return _refuse(request, REFUSE_PEER_UNVERIFIED, "peer uid not confirmed as ours")
    peer_pid = get_peer_pid(sock)
    if peer_pid is None:
        return _refuse(request, REFUSE_PEER_UNVERIFIED, "peer pid unavailable")
    if request.path == REDEEM_PATH:
        return await _redeem(request, peer_pid)
    if "X-Internal-Secret" in request.headers:
        # This listener authenticates by connection, never by a held value.
        return _refuse(request, REFUSE_SECRET, "the agent socket takes no secret")
    is_session_query = request.path == SESSION_PATH
    if not internal and not is_session_query:
        return _refuse(request, REFUSE_ROUTE, "the agent socket serves internal routes only")
    loop = asyncio.get_running_loop()
    binding, code = await loop.run_in_executor(
        subprocess_executor(),
        partial(STORE.verify, request.headers.get(CAPABILITY_HEADER, ""), peer_pid),
    )
    if binding is None:
        return _refuse(request, code, f"peer_pid={peer_pid}")
    session_key = await loop.run_in_executor(
        subprocess_executor(), partial(verify_session_token, binding.session_token)
    )
    if not session_key:
        return _refuse(request, REFUSE_SESSION_UNBOUND, "the bound session token names no session")
    if is_session_query:
        # The holder asks which session it acts for. It learns only its own binding's
        # current owner, the answer every one of its requests is held to below.
        if request.method != "GET":
            return _refuse(request, REFUSE_ROUTE, "the session query is GET only")
        return web.json_response({"session_key": session_key})
    if request.headers.get("X-Session-Key", "") != session_key:
        return _refuse(
            request, REFUSE_SESSION_MISMATCH, "X-Session-Key is not the capability's session"
        )
    request[GRANT_KEY] = binding
    _log_auth(request, "agent-ipc", "granted", f"capability {binding.server}")
    return None


def _prepare_socket_path(path: Path) -> None:
    platform_compat.make_owner_only_dir(path.parent)
    try:
        if path.is_socket() or path.is_symlink():
            path.unlink()
    except FileNotFoundError:
        pass


def _remove_socket(path: Path) -> None:
    try:
        if path.is_socket():
            path.unlink()
    except OSError:
        pass


async def start_agent_site(runner: web.AppRunner, port: int) -> Path | None:
    """Bind the agent socket on *runner*, beside the dashboard's own unix site.

    Strictly additive in the sense ``listener._start_unix_site`` is: Windows and any
    bind failure leave the gateway without it, and an in-sandbox server then takes
    today's path. *port* must be the BOUND port.
    """
    if platform_compat.IS_WINDOWS or not port:
        return None
    try:
        path = agent_socket_path(port)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(subprocess_executor(), _prepare_socket_path, path)
        site = web.UnixSite(runner, str(path))
        await site.start()
        await loop.run_in_executor(subprocess_executor(), chmod_socket_0600, path)
    except Exception as exc:
        logger.warning("agent socket unavailable (%s); in-sandbox servers keep today's path", exc)
        return None
    with _BOUND_LOCK:
        _BOUND_PATHS.add(str(path))
    logger.info("agent socket listening on %s", path)
    return path


def register_agent_site_cleanup(app: web.Application, holder: dict[str, Path | None]) -> None:
    """Unlink the agent socket at shutdown. Registered before ``runner.setup()``."""

    async def _cleanup(_app: web.Application) -> None:
        path = holder.get("path")
        if path is None:
            return
        with _BOUND_LOCK:
            _BOUND_PATHS.discard(str(path))
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _remove_socket, path
            )
        except Exception:  # pragma: no cover - cleanup must never break shutdown
            logger.debug("agent socket cleanup failed", exc_info=True)

    app.on_cleanup.append(_cleanup)
