"""The gateway's listeners.

The port reservation with its reclaim ladder, the bind, the TCP site start, the second
loopback family, the unix-socket transport, and the bound port and address exported
for child processes.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import socket
import stat
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.server import (
        _TIME_WAIT_BUDGET_SECS,
        FOREIGN_HOLDER,
        HEALTHY_PEER,
        NO_HOLDER,
        RECLAIMED,
        SECONDARY_LOOPBACK_FOR,
        DashboardState,
        chmod_socket_0600,
        dashboard_socket_path,
        logger,
        platform_compat,
        reclaim_stale_gateway_port,
        release_site,
        subprocess_executor,
    )


def _export_bound_port(runner: web.AppRunner, port: int) -> None:
    """Advertise the actually-bound dashboard port to child processes.

    Sets ``KIROCREW_BOUND_PORT`` in this process's environment once the TCP
    site is listening, so everything the gateway spawns (kiro-cli sessions and
    the MCP stdio servers they start) inherits the port that is really bound
    instead of re-deriving a guess from ``dashboard.url``. A portless URL makes
    ``parse_dashboard_url`` substitute the default port — right for the server
    (it must bind something), wrong for a child aiming a loopback callback at
    a gateway that may be bound elsewhere.

    Deliberately a DISTINCT variable from ``KIROCREW_PORT``: that one means
    "operator-declared port" everywhere else — ``service_environment()`` bakes
    it into persistent unit files, and config code reads it as intent — so
    writing bound truth into it would let a ``--port auto`` ephemeral port be
    frozen into a service install run from a gateway-descended shell, and
    would leak between tests through the process environment.
    ``KIROCREW_BOUND_PORT`` carries ephemeral truth only: consumed by
    ``port_resolution.resolve_client_port`` one step below the operator override,
    never persisted.

    *port* is ``0`` for an OS-assigned ephemeral bind (``--port auto``); the
    real port is then read back from the runner's bound addresses (only the
    TCP site is on the runner when this runs — the unix site is added after).
    Best-effort: when no TCP address is readable the environment is left
    untouched, which is exactly the pre-export behavior.
    """
    bound = _resolved_bound_port(runner, port)
    if bound:
        os.environ["KIROCREW_BOUND_PORT"] = str(bound)
        logger.debug("Exported KIROCREW_BOUND_PORT=%d for child processes", bound)
    else:
        logger.warning(
            "Could not read the bound dashboard port; child processes will "
            "re-derive it from config and the run-marker"
        )


def _resolved_bound_port(runner: web.AppRunner, port: int) -> int:
    """The port actually bound: *port*, or the OS-assigned one when it is ``0``.

    ``0`` means an ephemeral bind (``--port auto``, which ``--test-mode`` also
    implies), so the declared value names no listener and anything keyed by it
    would name the wrong one. Shared by the child-env export and the credential
    publication, which must agree: a credential filed under port ``0`` is
    unreachable for every client, and they would fall back to the shared file --
    which is exactly what the live-sibling guard deliberately leaves pointing at
    the sibling, so the ephemeral gateway would 403 every internal call.

    Returns ``0`` only when no TCP address is readable at all.
    """
    if port:
        return port
    for addr in runner.addresses:
        # TCP socknames are (host, port[, flowinfo, scope_id]) tuples; a
        # unix socket's would be a bare str path.
        if isinstance(addr, (tuple, list)) and len(addr) >= 2 and isinstance(addr[1], int):
            return addr[1]
    return 0


def _resolved_bound_host(runner: web.AppRunner, requested: str) -> str:
    """The address actually bound, falling back to the *requested* one.

    A credential is keyed by a listener, and a listener is an address AND a port.
    Reading the sockname rather than trusting the requested value keeps the key
    paired with what the kernel bound, which is what a client dials.

    Returns ``""`` when neither is readable, which suppresses the listener-keyed
    publication rather than filing the credential under a guess. A reader that
    finds no entry refuses, so the empty case costs an explicit sign-in instead
    of pointing a client at the wrong listener.
    """
    for addr in runner.addresses:
        # Same sockname shape as _resolved_bound_port; a unix socket's is a str.
        if isinstance(addr, (tuple, list)) and len(addr) >= 2 and isinstance(addr[1], int):
            host = addr[0]
            if isinstance(host, str) and host:
                return host
    return requested if isinstance(requested, str) else ""


async def _start_site(
    site: web.TCPSite,
    port: int,
    *,
    retries: int = 30,
    delay: float = 0.5,
    reclaim: Callable[[int], Awaitable[str]] | None = None,
) -> None:
    """Start *site*, reclaiming a stale holder / retrying on EADDRINUSE.

    On the first EADDRINUSE we probe *who* holds the port. A previous gateway
    that died uncleanly (force-exit or ``kill -9``) can leave a process holding
    the LISTEN socket that will never release it, so plain waiting cannot
    recover — :func:`reclaim_stale_gateway_port` terminates such a stale holder
    so the subsequent retry rebinds cleanly. A live, responsive gateway or a
    non-KiroCrew process is never touched; those (and any case where the holder
    can't be identified) fall back to a wait-up-to-*retries*×*delay* loop before
    giving up with ``SystemExit(1)``. Non-EADDRINUSE OSErrors are re-raised.
    """
    _reclaim = reclaim if reclaim is not None else reclaim_stale_gateway_port
    last_exc: OSError | None = None
    for attempt in range(retries):
        try:
            await site.start()
            return
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_exc = exc
            # release the partially-started site before retrying, listener only:
            # TCPSite.stop() would also fire the application's on_shutdown
            # signals and wait on the runner's shutdown timeout, and this
            # application has not started serving yet (see release_site).
            release_site(site)
            if attempt == 0:
                try:
                    outcome = await _reclaim(port)
                except Exception:  # never let a reclaim bug block startup
                    logger.exception(
                        "Port %d reclaim probe failed — falling back to wait/retry.",
                        port,
                    )
                    outcome = ""
                if outcome == RECLAIMED:
                    logger.warning(
                        "Reclaimed port %d from a stale Kiro Crew gateway — rebinding.",
                        port,
                    )
                elif outcome not in (HEALTHY_PEER, FOREIGN_HOLDER):
                    # NO_HOLDER / UNAVAILABLE / RECLAIM_FAILED / reclaim error:
                    # nothing safely reclaimable, so wait for a possible graceful
                    # handover. (A healthy peer / foreign holder won't release, so
                    # we skip this misleading "waiting" message for those.)
                    logger.warning(
                        "Port %d in use — waiting up to %.0fs for the previous"
                        " gateway to release it…",
                        port,
                        retries * delay,
                    )
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    logger.error(
        "Port %d still in use after %.0fs — is another Kiro Crew gateway running?\n"
        "Stop it with: kirocrew stop  or  sudo systemctl stop kirocrew",
        port,
        retries * delay,
    )
    raise SystemExit(1) from last_exc


def _bind_once(host: str, port: int) -> socket.socket:
    """Bind and listen once, synchronously, for dashboard port reservation.

    Family-resolved from *host* (KIROCREW_BIND may name an IPv6 address such
    as ``::`` or an interface-specific literal — an AF_INET socket cannot bind
    those). Callers run this off the event loop (``asyncio.to_thread``):
    getaddrinfo on a non-literal host and the bind syscall are blocking work
    that must not run on the sole loop (no-blocking-call-on-event-loop).
    """
    family = socket.AF_INET
    with contextlib.suppress(OSError):
        family = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)[
            0
        ][0]
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        # Match asyncio.create_server's socket posture. SO_REUSEADDR on POSIX
        # lets our next generation rebind through TIME_WAIT. Windows requires
        # exclusive ownership because SO_REUSEADDR allows a co-resident process
        # to overlap a live listener.
        if os.name == "posix":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        # An IPv6 wildcard must not silently expose the listener on IPv4 too.
        if family == socket.AF_INET6 and hasattr(socket, "IPPROTO_IPV6"):
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        sock.bind((host, port))
        # listen() IMMEDIATELY — this is load-bearing, not cosmetic. A
        # SO_REUSEADDR socket that is bound but NOT listening permits a
        # co-resident process to overlap-bind the same port (both the
        # exact and the specific-over-wildcard forms) and steal the
        # loopback callbacks carrying app secrets; entering TCP_LISTEN
        # makes that bind a hard EADDRINUSE conflict. Listening does NOT
        # serve anything: connections queue in the backlog until
        # SockSite.start() attaches the HTTP protocol, so the boot pass stays
        # race-free and an early child callback waits instead of being refused.
        sock.listen(128)
    except BaseException:
        sock.close()
        raise
    return sock


async def _reserve_dashboard_port(
    host: str,
    port: int,
    *,
    retries: int = 30,
    delay: float = 0.5,
    reclaim: Callable[[int], Awaitable[str]] | None = None,
) -> socket.socket:
    """Bind AND listen on the dashboard port; return the owned socket.

    This is _start_site's reclaim/retry contract moved to the moment of BIND,
    so the gateway OWNS its port before anything downstream (the app-backend
    boot pass) acts on the port's value. Bound-and-LISTENING is the reserved
    state: entering TCP_LISTEN is what makes any overlap bind a hard
    EADDRINUSE for other processes (see the listen() note in _bind_once), yet
    nothing is served — connections queue in the kernel backlog until the
    runner wraps the socket in a SockSite and starts accepting. The bound
    socket's real name is also what makes ``--port auto`` (port 0) knowable
    BEFORE the app backends spawn.

    Same recovery ladder as _start_site: first EADDRINUSE probes/reclaims a
    stale Kiro Crew holder, otherwise wait up to retries*delay for a graceful
    handover, then SystemExit(1). Non-EADDRINUSE OSErrors re-raise. One
    Windows widening: when the probe finds NO live holder (the TIME_WAIT
    signature — remnant connections pin the port with no process to reclaim),
    the wait budget stretches to ``_TIME_WAIT_BUDGET_SECS``, because the
    exclusive bind (``SO_EXCLUSIVEADDRUSE`` in ``_bind_once``) is documented
    to refuse the port until those remnants expire and a routine restart
    right after serving must out-wait them rather than fail boot.
    """
    _reclaim = reclaim if reclaim is not None else reclaim_stale_gateway_port
    last_exc: OSError | None = None
    budget = retries
    attempt = 0
    while attempt < budget:
        try:
            return await asyncio.to_thread(_bind_once, host, port)
        except OSError as exc:
            if exc.errno != errno.EADDRINUSE:
                raise
            last_exc = exc
            if attempt == 0:
                try:
                    outcome = await _reclaim(port)
                except Exception:  # never let a reclaim bug block startup
                    logger.exception(
                        "Port %d reclaim probe failed — falling back to wait/retry.",
                        port,
                    )
                    outcome = ""
                if outcome == RECLAIMED:
                    logger.warning(
                        "Reclaimed port %d from a stale Kiro Crew gateway — rebinding.",
                        port,
                    )
                elif outcome not in (HEALTHY_PEER, FOREIGN_HOLDER):
                    # NO_HOLDER + EADDRINUSE is the TIME_WAIT signature: the
                    # previous generation's connections still pin the port in
                    # the kernel with no process to reclaim. On Windows the
                    # reservation binds SO_EXCLUSIVEADDRUSE (see _bind_once),
                    # which is documented to refuse a bind while TIME_WAIT
                    # remnants exist — so a routine restart right after serving
                    # must be able to out-wait TcpTimedWaitDelay (4 min default)
                    # rather than dying on a 15s ladder sized for a graceful
                    # handover. Gated on NO_HOLDER EXACTLY: UNAVAILABLE (probe
                    # tooling missing) and RECLAIM_FAILED (a live holder that
                    # would not die) are not TIME_WAIT, and stretching on them
                    # would stall boot four minutes for a port that waiting
                    # cannot free. A live holder (healthy peer or foreign
                    # process) keeps the short ladder and its fast exit.
                    if outcome == NO_HOLDER and os.name == "nt":
                        budget = max(budget, int(_TIME_WAIT_BUDGET_SECS / delay))
                    logger.warning(
                        "Port %d in use — waiting up to %.0fs for the previous"
                        " gateway to release it…",
                        port,
                        budget * delay,
                    )
            attempt += 1
            if attempt < budget:
                await asyncio.sleep(delay)
    logger.error(
        "Port %d still in use after %.0fs — is another Kiro Crew gateway running?\n"
        "Stop it with: kirocrew stop  or  sudo systemctl stop kirocrew",
        port,
        budget * delay,
    )
    raise SystemExit(1) from last_exc


def _remove_stale_unix_socket(path: Path) -> None:
    """Best-effort unlink of a leftover unix-socket file before rebind.

    Only a socket inode is removed — anything else at the path is left in
    place (and the subsequent bind fails, degrading to TCP-only). Safe against
    a live sibling instance: the socket name is port-suffixed and the TCP port
    bind (a singleton per port) has already succeeded by the time this runs,
    so an existing file with our port's name can only be stale.
    """
    try:
        st = os.stat(path)
    except OSError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        logger.warning(
            "path %s exists and is not a socket (mode=%o); leaving in place", path, st.st_mode
        )
        return
    try:
        path.unlink()
    except OSError as exc:
        logger.warning("could not remove stale dashboard socket %s: %s", path, exc)


def _holds_every_loopback_family(state: DashboardState) -> bool:
    """Whether this gateway currently holds BOTH loopback families.

    The server-side counterpart of ``listenerSecretsFor``: a name that resolves to
    two families is safe to send a credential to -- or to redirect a
    cookie-carrying navigation onto -- only while one gateway answers on all of
    them.

    Read from the claims this process recorded plus each guard's live socket, so a
    family that was never bound and a family whose listener has since died give the
    same answer. Fails closed before publication, when no claim is recorded yet:
    the gateway has nothing to prove coverage with, and a redirect suppressed
    during boot costs one un-canonicalized document.
    """
    sidecars = getattr(state, "_listener_sidecars", None) or {}
    if "primary" not in sidecars or "secondary" not in sidecars:
        return False
    for attr in ("_listener_guard", "_secondary_listener_guard"):
        guard = getattr(state, attr, None)
        if guard is not None and not guard.listener_open():
            return False
    return True


class SecondaryLoopback(NamedTuple):
    """The second loopback listener: the address it holds, and its live site.

    The site is carried, not discarded, because the address alone cannot be
    maintained. A published sidecar asserts that this gateway holds this address
    NOW, and the only object that can answer whether it still does -- or rebind
    it when it does not -- is the site's own LISTEN socket. Returning the address
    by itself made the claim permanent and the listener unguardable at once.
    """

    address: str
    site: web.SockSite


async def _start_secondary_loopback_site(
    runner: web.AppRunner, port: int, primary_host: str
) -> SecondaryLoopback | None:
    """Additionally serve the OTHER loopback family on the same port.

    A client reaching the gateway by name rather than by address dials
    ``localhost``, which resolves to BOTH loopback families on an ordinary host.
    That name therefore identifies a SET of listeners, and whichever family the
    gateway did not bind is free for a co-resident process to take -- so a
    credential sent to the name can land on a party the gateway never was. Two
    ways out: rewrite the name to a literal at every call site, which moves the
    document's web origin and splits every comparison that holds the configured
    string; or hold both families, so the name can only reach this gateway.

    This is the second. Binding ``::1`` beside ``127.0.0.1`` (or the reverse)
    makes the ambiguity harmless rather than routed around, and the evidence a
    client needs is already published: one ``run/gateway-<port>-<address>.secret``
    per bound address, so "this gateway holds every family the name reaches" is
    readable from local disk with nothing asked of the peer.

    Strictly additive, in the sense ``_start_unix_site`` established: same
    :class:`web.AppRunner`, so both listeners serve the identical app and
    middleware chain, and ANY failure logs once and leaves the primary listener
    exactly as it is. Failure is the interesting case and it is safe: without the
    second entry a client dialling the name finds a family uncovered, refuses to
    send its secret, and falls through to the token prompt. That is one explicit
    sign-in, and it is the same cost a single-family gateway already pays.

    Deliberately NOT using the reclaim/retry ladder that guards the primary bind.
    A process already holding the other family's socket is precisely the threat
    this exists to exclude; reclaiming it would terminate a stranger's listener,
    and waiting for it would delay boot for a port the gateway does not need.
    One attempt, then degrade.

    Only the two loopback literals have a counterpart. A wildcard or an
    interface-specific bind is not a loopback family pair, and
    ``KIROCREW_BIND=<something else>`` is an operator naming one listener on
    purpose, so neither gets a second socket.

    Returns the address actually bound together with its live site, or ``None``
    when there is no second listener -- which the caller treats as "publish one
    address, not two". The site goes back to the caller so the listener can be
    guarded and its sidecar withdrawn if it dies: see
    :func:`_arm_secondary_listener_guard`.
    """
    secondary = SECONDARY_LOOPBACK_FOR.get(primary_host)
    if secondary is None:
        return None
    try:
        # Offloaded for the same reason as the primary reservation: getaddrinfo
        # and bind are blocking syscalls (no-blocking-call-on-event-loop).
        sock = await asyncio.to_thread(_bind_once, secondary, port)
    except OSError as exc:
        logger.info(
            "second loopback listener on [%s]:%d unavailable (%s); clients dialling a "
            "name that resolves there will sign in explicitly",
            secondary,
            port,
            exc,
        )
        return None
    try:
        site = web.SockSite(runner, sock)
        await site.start()
    except Exception as exc:
        with contextlib.suppress(OSError):
            sock.close()
        logger.info(
            "second loopback listener on [%s]:%d could not start (%s); the primary "
            "listener is unaffected",
            secondary,
            port,
            exc,
        )
        return None
    logger.info("dashboard also listening on [%s]:%d", secondary, port)
    return SecondaryLoopback(secondary, site)


async def _start_unix_site(runner: web.AppRunner, port: int) -> Path | None:
    """Additionally serve the internal API on a unix socket (POSIX only).

    Binds ``dashboard_socket_path(port)`` on the same :class:`web.AppRunner`
    as the TCP site, so both transports serve the identical app + middleware
    chain. The unix transport exists so ``token_auth_middleware`` can
    kernel-verify (``SO_PEERCRED`` + /proc ancestry) the session identity an
    internal caller declares in ``X-Session-Key`` — TCP loopback carries no
    peer credentials.

    Strictly additive: skipped entirely on Windows, and ANY failure (bind
    error, permission problem) logs once and degrades to TCP-only, which is
    exactly today's behavior. The socket file inherits the data home's 0700
    directory gate (created here if missing) and is itself tightened to 0600,
    mirroring ``mcp_gateway/transport`` conventions. Returns the bound path,
    or ``None`` when the transport is unavailable.
    """
    if platform_compat.IS_WINDOWS:
        return None
    try:
        path = dashboard_socket_path(port)
        # Offloaded: directory creation, the stale-socket stat/unlink, and the
        # post-bind chmod are blocking fs I/O (no-blocking-call-on-event-loop).
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            subprocess_executor(), platform_compat.make_owner_only_dir, path.parent
        )
        await loop.run_in_executor(subprocess_executor(), _remove_stale_unix_socket, path)
        unix_site = web.UnixSite(runner, str(path))
        await unix_site.start()
        await loop.run_in_executor(subprocess_executor(), chmod_socket_0600, path)
        logger.info("dashboard internal API also listening on unix socket %s", path)
        return path
    except Exception as exc:
        logger.warning("dashboard unix socket unavailable (%s); internal API stays TCP-only", exc)
        return None


def _register_unix_socket_cleanup(app: web.Application, holder: dict[str, Path | None]) -> None:
    """Register best-effort removal of the unix socket file at shutdown.

    Registered BEFORE ``runner.setup()`` freezes the app's signal lists; the
    socket path only becomes known after the site starts, so it is read from
    *holder* lazily. aiohttp does not unlink a ``UnixSite``'s socket file on
    stop, and while startup self-heals a stale file, a clean shutdown should
    not leave one for clients to trip over (each stale connect costs the
    client a refused-connect before its TCP fallback).
    """

    async def _unlink_unix_socket(app_: web.Application) -> None:
        path = holder.get("path")
        if path is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), _remove_stale_unix_socket, path
            )
        except Exception:  # pragma: no cover — cleanup must never break shutdown
            logger.debug("dashboard unix socket cleanup failed", exc_info=True)

    app.on_cleanup.append(_unlink_unix_socket)


def _export_reserved_bind_evidence(sock: socket.socket) -> str:
    """Export the reserved socket's name as bound-port evidence; return its address.

    Run before the app-backend boot pass, whose origin and proof injection in
    ``apps.backend`` is fail-closed on ``KIROCREW_BOUND_PORT``.
    """
    os.environ["KIROCREW_BOUND_PORT"] = str(sock.getsockname()[1])
    # Callback-host evidence, classified by FAMILY. IPv4 loopback and
    # wildcard binds are reachable at 127.0.0.1 (absent var = that
    # default, the shape every existing bound-port consumer assumes). An
    # IPv6 loopback or wildcard bind is NOT: KIROCREW_BIND=::1 listens
    # only on the v6 loopback and leaves IPv4 127.0.0.1:<port> unbound —
    # seizable by a co-resident, which would then receive the backends'
    # secrets — so those export ::1 (reaches a v6-loopback, v6-wildcard,
    # and dual-stack listener alike; the injection brackets it). A
    # SPECIFIC-interface bind of either family exports its own address.
    _bind_ip = str(sock.getsockname()[0])
    if _bind_ip in ("::", "::1"):
        os.environ["KIROCREW_BOUND_HOST"] = "::1"
    elif _bind_ip in ("0.0.0.0", "127.0.0.1", ""):
        os.environ.pop("KIROCREW_BOUND_HOST", None)
    else:
        os.environ["KIROCREW_BOUND_HOST"] = _bind_ip
    return _bind_ip
