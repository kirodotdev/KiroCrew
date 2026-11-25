"""The entry port: a loopback port for a reverse proxy that forwards other machines.

The gateway listens on loopback, so a caller on another machine reaches it through a
proxy the deployer runs on this machine. Such a proxy connects from loopback,
which is what a program on this machine looks like. So the proxy gets a port of its
own (``dashboard.entry_port``, off by default), bound on both loopback addresses
(``127.0.0.1`` and ``::1``) beside the gateway's, and a request came through the
entry because of the port it arrived on, never because of anything it says. This is
the SSM listener's rule (:mod:`kiro_crew.dashboard.ssm_gate`). Linux and macOS only
for now: on Windows a non-zero port stops the start.

:func:`entry_gate_middleware` runs in the dashboard chain, the one beside which the
entry port opens, just inside the relay header strip. Route latency, the Dashboard
card turn mark, the relay-path rule (which reads headers, can refuse, and verifies
a relay handover) and the relay-header strip run before it. It is outer to the
deny-audit boundary and every barrier and auth layer inside it. On the entry port it:

* marks the request (``origin.ENTRY_REQUEST_KEY``), so it is never a direct local
  request and always a forwarded one, whatever headers the proxy added or left out;
* refuses a header that authenticates a process on this machine
  (``limits.LOCAL_CREDENTIAL_HEADERS``), which no caller behind the proxy holds;
* serves the Crew API alone: any other path is answered 404;
* requires ``Host`` to name this machine's loopback, so no other web origin the
  gateway serves by host name is reachable;
* refuses every Owner route with 403: an approval or a grant is answered on the
  machine;
* requires ``Authorization: Bearer``: no cookie is read as a credential.

A request that passes goes on to token auth, which verifies the bearer exactly as on
the main port. Arriving on the entry port grants nothing.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.dashboard.crew_api_auth import bearer_credential
from kiro_crew.dashboard.crew_api_errors import (
    contract_error,
    is_crew_api_path,
    is_owner_path,
    request_id_for,
)
from kiro_crew.dashboard.kiro_relay.limits import LOCAL_CREDENTIAL_HEADERS
from kiro_crew.dashboard.listener_guard import release_site
from kiro_crew.dashboard.origin import ENTRY_REQUEST_KEY, _split_authority, is_proxied_request

logger = logging.getLogger(__name__)

#: aiohttp app key holding the app's :class:`GateRef`.
ENTRY_GATE_APP_KEY = "entry_gate"
#: The addresses the entry port binds: both loopback addresses. Not a setting.
BIND_ADDRESS = "127.0.0.1"
IPV6_BIND_ADDRESS = "::1"
PORT_MIN, PORT_MAX = 1024, 65535
#: What ``Host`` may name on the entry port. ``[::1]`` splits to ``::1``.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
#: Errors from binding ``::1`` that mean this machine has no IPv6 loopback: the
#: address is not configured (EADDRNOTAVAIL) or the kernel has no IPv6
#: (EAFNOSUPPORT, EPROTONOSUPPORT). EADDRINUSE, another process holding
#: ``[::1]:<port>``, is not among them, and neither is any other error.
_NO_IPV6_ERRNOS = frozenset({errno.EADDRNOTAVAIL, errno.EAFNOSUPPORT, errno.EPROTONOSUPPORT})

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class EntryPortError(RuntimeError):
    """``dashboard.entry_port`` is set and the port cannot be served as configured."""


@dataclass(frozen=True)
class EntryGate:
    """Tells which requests arrived on the entry port."""

    port: int

    def owns(self, request: web.Request) -> bool:
        """Whether *request* arrived on the entry port's loopback socket."""
        transport = getattr(request, "transport", None)
        if transport is None:
            return False
        try:
            info = transport.get_extra_info("sockname")
        except Exception:  # noqa: BLE001 - an unreadable socket address owns nothing
            return False
        if not isinstance(info, (tuple, list)) or len(info) < 2:
            return False
        host, port = info[0], info[1]
        if not isinstance(host, str) or port != self.port:
            return False
        try:
            return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
        except ValueError:
            return False

    @staticmethod
    def can_read_socket(request: web.Request) -> bool:
        """Whether *request*'s local socket address can be read at all.

        A TCP socket names a ``(host, port, ...)`` tuple and the unix socket site a
        path ``str``; either one shows whether the request is on the entry port.
        Anything else, or an error, cannot show it is not.
        """
        transport = getattr(request, "transport", None)
        if transport is None:
            return False
        try:
            info = transport.get_extra_info("sockname")
        except Exception:  # noqa: BLE001 - an unreadable socket address is unknown
            return False
        return isinstance(info, str) or (isinstance(info, (tuple, list)) and len(info) >= 2)


@dataclass
class GateRef:
    """The entry gate the middleware reads, filled in once the port is bound.

    Put on the app before its runner is set up, since a started app takes no new key.
    """

    gate: EntryGate | None = None


def configured_port(value: object, dashboard_port: int, ssm_port: int | None = None) -> int:
    """The entry port *value* asks for, ``0`` when it is off.

    Raises :class:`EntryPortError` for a non-integer, any non-zero port on Windows,
    an integer that is neither ``0`` nor a port from :data:`PORT_MIN` to
    :data:`PORT_MAX`, the gateway's own port, or *ssm_port* (the SSM listener's
    port, passed only when that listener is configured to run). Shared config
    validation runs before this check: wrong types become the default ``0`` with a
    warning, while ``"8443"`` and ``8443.0`` become integer 8443.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise EntryPortError("dashboard.entry_port must be a whole number")
    if value == 0:
        return 0
    # shortcut: no listener guard for the entry port on Windows, so the setting is
    # refused there; lift when the entry port has a listener guard.
    if platform_compat.IS_WINDOWS:
        raise EntryPortError(
            "dashboard.entry_port is for Linux and macOS for now; set it to 0 on Windows"
        )
    if not PORT_MIN <= value <= PORT_MAX:
        raise EntryPortError(f"dashboard.entry_port must be 0 or {PORT_MIN} to {PORT_MAX}")
    if value == dashboard_port:
        raise EntryPortError("dashboard.entry_port must differ from the gateway's own port")
    if ssm_port is not None and value == ssm_port:
        raise EntryPortError("dashboard.entry_port must differ from dashboard.shared_crew.ssm_port")
    return value


async def _bind_ipv6_loopback(
    bind_once: Callable[[str, int], socket.socket], port: int
) -> socket.socket | None:
    """``[::1]:<port>`` bound and listening, or ``None`` when there is no IPv6 loopback.

    Raises :class:`OSError` for every other failure, ``EADDRINUSE`` included, so
    the start stops rather than leave ``[::1]:<port>`` to another process.
    """
    try:
        return await asyncio.to_thread(bind_once, IPV6_BIND_ADDRESS, port)
    except socket.gaierror as exc:
        # ``::1`` cannot even be resolved to an address here: no IPv6 loopback.
        reason: OSError = exc
    except OSError as exc:
        if exc.errno not in _NO_IPV6_ERRNOS:
            raise
        reason = exc
    logger.info("entry port: no IPv6 loopback on this machine (%s); 127.0.0.1 only", reason)
    return None


def _release(sites: list[web.SockSite], socks: list[socket.socket]) -> None:
    """Close every listener :func:`start` opened, started or not."""
    for site in sites:
        release_site(site)
    for sock in socks:
        with contextlib.suppress(OSError):
            sock.close()


async def start(
    ref: GateRef,
    runner: web.AppRunner,
    value: object,
    dashboard_port: int,
    ssm_port: int | None = None,
) -> list[web.SockSite]:
    """Bind the entry port on both loopback addresses on *runner*. ``[]`` when it is off.

    ``127.0.0.1:<port>`` is always bound; ``[::1]:<port>`` too unless this machine
    has no IPv6 loopback. Raises :class:`EntryPortError` when the validated value
    fails :func:`configured_port` or either address cannot be bound, and then
    leaves no listener and no gate behind. A wrong-typed config value becomes the
    default ``0`` in shared validation, so it binds nothing. The gate is set before
    a site accepts a connection, so a connection on this port is never served
    unmarked.
    """
    port = configured_port(value, dashboard_port, ssm_port)
    if not port:
        return []
    # The gateway's own bind helper, read from the server module, its patch surface.
    from kiro_crew.dashboard import server

    socks: list[socket.socket] = []
    sites: list[web.SockSite] = []
    try:
        # _bind_once sets SO_REUSEADDR on POSIX: without it the port cannot be rebound
        # for a while after a restart and the gateway would not start. The SSM listener
        # binds the same way (reuse_address on POSIX).
        socks.append(await asyncio.to_thread(server._bind_once, BIND_ADDRESS, port))
        ipv6 = await _bind_ipv6_loopback(server._bind_once, port)
        if ipv6 is not None:
            socks.append(ipv6)
        ref.gate = EntryGate(port=port)
        for sock in socks:
            site = web.SockSite(runner, sock)
            # Kept before start(), so a half-started site is released too.
            sites.append(site)
            await site.start()
    except OSError as exc:
        ref.gate = None
        _release(sites, socks)
        raise EntryPortError(f"the entry port could not bind loopback port {port}: {exc}") from exc
    except BaseException:
        ref.gate = None
        _release(sites, socks)
        raise
    logger.info(
        "entry port %d on %s (Crew API only)",
        port,
        " and ".join(str(s.getsockname()[0]) for s in socks),
    )
    return sites


async def _refuse(request: web.Request, status: int, code: str, message: str) -> web.Response:
    """One refusal: its audit row, then the Crew API error body."""

    def write() -> None:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=request.remote or "",
            operation="entry_gate",
            outcome="denied",
            source="entry_gate",
            resources=request.path,
            error=code,
            forwarded=is_proxied_request(request),
        )

    try:
        await asyncio.to_thread(write)
    except Exception:
        logger.warning("No audit row written for an entry gate refusal (%s)", code)
    return contract_error(status, code, message, request_id_for(request))


def _names_loopback(request: web.Request) -> bool:
    parts = _split_authority(request.headers.get("Host", ""))
    return parts is not None and parts[0].lower() in _LOOPBACK_HOSTS


def _is_owner_route(request: web.Request) -> bool:
    """Whether the path, or the route it matched, is an Owner route."""
    resource = request.match_info.route.resource
    return is_owner_path(request.path) or (
        resource is not None and is_owner_path(resource.canonical)
    )


@web.middleware
async def entry_gate_middleware(request: web.Request, handler: _Handler) -> web.StreamResponse:
    """Mark and narrow a request that arrived on the entry port."""
    gate = getattr(request.app.get(ENTRY_GATE_APP_KEY), "gate", None)
    if isinstance(gate, EntryGate) and not gate.can_read_socket(request):
        # The connection is gone or its socket address cannot be read, so it cannot
        # show that it is not the entry port: refused rather than served as if it
        # arrived on the gateway's own port.
        return await _refuse(
            request, 400, "invalid_request", "The connection's socket cannot be read."
        )
    on_entry_port = isinstance(gate, EntryGate) and gate.owns(request)
    if on_entry_port:
        # First, so every later layer and each refusal below reads it as forwarded.
        request[ENTRY_REQUEST_KEY] = True
    if not on_entry_port:
        return await handler(request)
    if any(name in request.headers for name in LOCAL_CREDENTIAL_HEADERS):
        # A request through the entry is not from one of this machine's own processes.
        return await _refuse(
            request,
            400,
            "local_credential_refused",
            "This machine's local credentials are not accepted on the entry port.",
        )
    # A path no route serves is refused here too: token auth matches internal paths by
    # prefix, so a child of a frozen path would reach a branch that reads a cookie or
    # a query token instead of the bearer. A wrong method on a routed path goes on and
    # is answered 405: token auth picks its branch by path, and a routed Crew API
    # path reads the bearer only.
    no_route = isinstance(request.match_info.http_exception, web.HTTPNotFound)
    if not is_crew_api_path(request.path) or no_route:
        return await _refuse(request, 404, "not_found", "The entry port serves the Crew API only.")
    if not _names_loopback(request):
        return await _refuse(
            request,
            400,
            "invalid_request",
            "On the entry port, Host must name 127.0.0.1, [::1] or localhost.",
        )
    if _is_owner_route(request):
        return await _refuse(
            request, 403, "owner_only", "Owner routes are answered on the Crew's own computer."
        )
    if bearer_credential(request) is None:
        return await _refuse(request, 401, "invalid_token", "Token required.")
    return await handler(request)
