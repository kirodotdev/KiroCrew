"""The CLI's proven route to its own gateway: the loopback address and the peer proof.

Every CLI request that carries the local secret to this install's own gateway
over HTTP (the ``kiro_crew.cli_server`` commands, ``kirocrew spawn``,
``kirocrew artifact`` and ``kirocrew doctor``) goes through
``_owner_verified_secret_urlopen``, which writes the secret only after the peer on that
very connection is proven to be this install's recorded gateway (the owner-only unix
socket's peer credentials, or the kernel's owner of the loopback TCP 4-tuple, against
``run/gateway-<port>.pid`` and its start token) and refuses anything unproven before the
first byte, with a SEL event either way. ``_CLI_LOOPBACK`` is the address those requests
use. This is the lowest owner: the token, logout and update-approve commands, ``kirocrew stop``,
``kirocrew restart`` and ``kirocrew update`` all import from it.

Split out of ``kiro_crew.cli_server``, which stays the import path and the patch target: every
name here is reached as ``kiro_crew.cli_server.<name>``, and a patch through that facade
reaches this module (``kiro_crew._facade_mirror``). Nothing here imports the facade.
New peer-proof work goes here.
"""

from __future__ import annotations

import enum
import os
import socket
import time
import urllib.request

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard.urls import dashboard_socket_path
from kiro_crew.instances import run_marker
from kiro_crew.loopback_http import tcp_verified_urlopen, unix_socket_urlopen
from kiro_crew.mcp_gateway.socketsec import PeerCredResult, check_peer_is_self, get_peer_pid
from kiro_crew.sel import sel

# Loopback address used for the CLI's OWN requests to the gateway. Deliberately
# the literal IPv4 address, never the name ``localhost``: on a dual-stack host
# ``localhost`` may resolve to ``::1`` first, so a different local user who binds
# ``[::1]:<port>`` beside the real IPv4 gateway would receive requests carrying
# ``X-Local-Secret`` — and the listener verification in _gateway_owns_port is
# address-agnostic (``lsof -ti TCP:<port>`` cannot tell the two sockets apart),
# so it would still see the genuine gateway and pass. Pinning the address binds
# the request to the endpoint we actually verified.
#
# This is ONLY for CLI->gateway requests. The URL *printed* for the browser
# stays ``resolve_dashboard_host()`` (``localhost``), which must not change: the
# SPA's per-origin localStorage is keyed on that host, so emitting a different
# origin would make every dashboard setting appear reset.
_CLI_LOOPBACK = "127.0.0.1"


# A connection the gateway has not yet accept()ed sits in the kernel accept
# queue with inode 0, which the /proc peer resolver skips, so the lookup is
# momentarily None against a healthy but slow-to-accept TCP-only gateway. Retry
# the resolve for this bounded window before failing closed, so a transient
# accept-queue race does not refuse a legitimate gateway.
_PEER_RESOLVE_RETRY_SECONDS = 1.0


_PEER_RESOLVE_RETRY_INTERVAL = 0.05


def _recorded_gateway_pid(port: int) -> int | None:
    """The pid the gateway recorded for *port*, only while that record is FRESH.

    Reads ``run/gateway-<port>.pid`` and its ``.start`` sidecar and returns the
    pid when the record exists and its start token still matches the live pid's
    (``run_marker.pid_start_token``) -- the freshness proof that tells the
    recorded pid apart from the same number recycled onto an unrelated process
    after a crash left the sidecar behind. Returns ``None`` when the record is
    absent, the start token is empty, or it does not match the live pid's.

    This is the shared core of every recorded-identity check -- the port-reach
    set (:func:`_verified_loopback_gateway_pids`) and the two connection-bound
    peer proofs (:func:`_connected_unix_peer_is_recorded_gateway`,
    :func:`_connected_peer_verdict`). Each caller then adds its own ADDRESS and
    OWNER test (loopback-reach + uid; connected-peer equality + uid/SID), which
    differ by transport; the record-and-token step does not, so it lives here.
    """
    record = run_marker.read_pid_record_path(
        config_dir() / run_marker.RUN_DIR_NAME / run_marker.pid_file_name(port)
    )
    if record is None:
        return None
    pid, start_token = record
    if not start_token or start_token != run_marker.pid_start_token(pid):
        return None
    return pid


class _GatewayPeerRefused(Exception):
    """The connection-bound ownership proof refused the gateway peer.

    Deliberately NOT an :class:`OSError`. ``AbstractHTTPHandler.do_open`` wraps a
    raised ``OSError`` into ``URLError`` (``except OSError as err: raise
    URLError(err)``), so a ``PermissionError`` from a ``verify_peer`` hook would
    reach the CLI commands as a ``URLError`` and be reported as "gateway not
    running" against a gateway that is in fact live but refused. This type
    escapes that wrap and propagates unwrapped, so the caller reports the real
    refusal.
    """


# Tracebacks and logs name it by the import path its callers use.
_GatewayPeerRefused.__module__ = "kiro_crew.cli_server"


def _audit_peer_denied(operation: str, port: int, detail: str) -> None:
    """Emit a ``denied`` SEL event for a connection-bound ownership refusal.

    The authoritative connection-bound gate can refuse, and that denial must not
    be silent -- backend-security-controls requires every permission decision to
    emit a SEL event. Called by the ``verify_peer`` hooks right before they
    raise.
    """
    sel().log_api_access(
        caller="cli",
        operation=operation,
        outcome="denied",
        source="cli",
        resources=f"port={port} reason=connected_peer_unverified",
        error=detail,
    )


def _audit_peer_allowed(operation: str, port: int) -> None:
    """Emit an ``allowed`` SEL event for a connection-bound ownership grant.

    The authoritative connection-bound gate authorizes the owner-minting secret
    send only on a positively confirmed same-principal peer. That positive
    decision is a permission decision too, and backend-security-controls requires
    every permission decision to emit a SEL event -- symmetric to the ``denied``
    event emitted on refusal. Called by the ``verify_peer`` hooks right after the
    peer verifies and before the secret header is written.
    """
    sel().log_api_access(
        caller="cli",
        operation=operation,
        outcome="allowed",
        source="cli",
        resources=f"port={port} reason=connected_peer_verified",
    )


def _connected_unix_peer_is_recorded_gateway(sock, port: int) -> bool:
    """Whether the connected AF_UNIX peer is the gateway recorded for *port*.

    The uid check (``check_peer_is_self``) proves only that the peer runs as this
    account -- any same-uid process passes it, including one that rebound the
    socket path, which is not on an owner-only floor every platform guarantees.
    So bind the peer to the recorded gateway identity as well: resolve the peer
    pid from the connected socket (``SO_PEERCRED`` on Linux, ``LOCAL_PEERPID`` on
    macOS) and require it to equal the pid recorded for *port* with a matching
    start token -- the same recorded-identity proof the TCP path applies. The
    start token is held outside agent-writable paths, so a same-uid impostor on a
    different pid, or without the real start token, is refused. Fails closed: an
    unreadable peer pid or an absent/mismatched record returns ``False``.
    """
    peer_pid = get_peer_pid(sock)
    if peer_pid is None:
        return False
    pid = _recorded_gateway_pid(port)
    return pid is not None and peer_pid == pid


# Remedy appended to every connection-bound refusal. The recorded identity the
# proof checks (run/gateway-<port>.pid + .start) is written best-effort at boot:
# run_marker.write_marker skips the write when it cannot take its lock, and then
# a healthy gateway is refused here. The refusal is the intended trade for an
# owner-minting credential; this line gives the operator the remedy.
_REPUBLISH_IDENTITY_HINT = (
    "If this is your own gateway, restart it to re-publish its identity record "
    "(run/gateway-<port>.pid)."
)


def _existing_dashboard_socket(port: int) -> str:
    """The gateway's owner-only ``AF_UNIX`` socket path for *port*, if it exists.

    Empty string on a platform without ``AF_UNIX`` (native Windows), when the
    path cannot be derived, or when no socket file is present (a TCP-only
    gateway). Shared by every secret-bearing send so they agree on when the
    peer-verified unix arm is available.
    """
    if not hasattr(socket, "AF_UNIX"):
        return ""
    try:
        socket_path = str(dashboard_socket_path(port))
    except Exception:
        return ""
    return socket_path if os.path.exists(socket_path) else ""


def _owner_verified_secret_urlopen(
    req: "urllib.request.Request", port: int, timeout: float, *, operation: str
):
    """Open *req* against the local gateway with ownership PROVEN ON THE TRANSPORT.

    The token-minting and dashboard-armed-approval sends attach a credential a
    foreign local listener must never receive (``/api/token/local`` mints owner
    tokens; the approve endpoints carry a single-use nonce plus the local
    secret), so the proof that the peer is this install's own gateway must be
    bound to the very connection that carries the credential -- verifying one
    connection and sending on another leaves a TOCTOU window where the gateway
    can die and a foreign listener bind the freed loopback port between the two.
    NO send path, main or fallback, writes the credential without a
    connection-bound proof.

    The proof is bound to the connection on whichever transport the host offers,
    and a configuration the server documents as supported never loses the
    command:

    1. POSIX with the gateway's own ``AF_UNIX`` socket present (the ordinary
       case): send over that socket -- it lives in an owner-only directory no
       other user can answer -- via :func:`unix_socket_urlopen` (NO TCP handler
       at all), with a ``verify_peer`` hook that runs on the CONNECTED socket
       before any byte is written and raises unless the kernel confirms the peer
       is this principal (``SO_PEERCRED``/``LOCAL_PEERCRED`` via
       :func:`check_peer_is_self`) AND the gateway recorded for this port.

    2. No ``AF_UNIX`` socket to prefer -- a TCP-only gateway (the server
       documents these), or native Windows (no ``AF_UNIX`` at all): send over
       loopback TCP via :func:`tcp_verified_urlopen` with the SAME security
       guarantee bound to the connection -- :func:`_connected_peer_verdict` on
       the CONNECTED socket, right before the first byte, reads the kernel's
       exact 4-tuple to name the pid on the far end and confirm it is this
       install's recorded gateway, owned by this account (uid on POSIX, access-
       token owner SID on Windows). The credential is written ONLY on a
       positively confirmed ``MATCH``; anything else is a ``MISMATCH`` that
       refuses -- a resolved foreign peer and a peer that could not be resolved
       at all (e.g. a host with no trusted ``lsof``) are one fail-closed
       decision, since the unresolvable case is exactly where a foreign listener
       on a crash-freed port could be the far end.

    Because POSIX proves ownership by uid here, a legitimate same-user TCP-only
    gateway resolves to ``MATCH`` -- so a supported loopback-TCP configuration
    keeps working while an unprovable peer is still refused. Linux resolves the
    peer from ``/proc`` with no external tool; Windows reads the kernel owner-PID
    table; only a host that genuinely cannot resolve the connected peer (lsof-
    less macOS) refuses for lack of a resolver, and there the single refusal
    message names the install-lsof / owner-only-socket remedy.

    When a hook refuses, it emits a ``denied`` SEL event for *operation* BEFORE
    raising, and when the peer verifies it emits an ``allowed`` SEL event for
    *operation* before the credential header is written, so both arms of the
    connection-bound decision are auditable. It raises
    :class:`_GatewayPeerRefused`, which is NOT an :class:`OSError`: urllib's
    ``AbstractHTTPHandler.do_open`` wraps a raised ``OSError`` into ``URLError``,
    which would make a live-but-refused gateway read to the caller as "gateway
    not running". The non-``OSError`` refusal propagates unwrapped so the caller
    reports the real reason.
    """
    socket_path = _existing_dashboard_socket(port)
    if socket_path:

        def _verify_peer(sock) -> None:
            # Runs between connect() and the first send; raising refuses
            # before the credential header is written. Deny-by-default: the
            # peer must be this account (uid) AND the gateway recorded for
            # this port (pid + start token) -- the uid alone is not enough,
            # since any same-uid process can rebind the socket path, so bind
            # to the recorded identity the way the TCP path does. Audit both
            # arms -- the denial before raising and the positive grant before
            # the send -- so every connection-bound permission decision is
            # recorded.
            if check_peer_is_self(sock) is not PeerCredResult.MATCH:
                _audit_peer_denied(operation, port, "connected socket peer is not this account")
                raise _GatewayPeerRefused("connected gateway socket peer is not this account")
            if not _connected_unix_peer_is_recorded_gateway(sock, port):
                _audit_peer_denied(
                    operation, port, "connected socket peer is not this install's gateway"
                )
                raise _GatewayPeerRefused(
                    "connected socket peer is a same-user process but not this "
                    "install's recorded gateway. " + _REPUBLISH_IDENTITY_HINT
                )
            _audit_peer_allowed(operation, port)

        return unix_socket_urlopen(req, timeout, socket_path=socket_path, verify_peer=_verify_peer)
    # No owner-only socket here (TCP-only gateway, a supported config): fall
    # through to the connection-bound TCP proof below rather than hard-fail.

    def _verify_tcp_peer(sock) -> None:
        # Bind the proof to the connected TCP socket and FAIL CLOSED: the
        # owner-minting credential is written only on a positively confirmed
        # MATCH. Anything else refuses -- a resolved foreign peer and a peer that
        # cannot be resolved at all (a host with no trusted lsof) are the same
        # decision, because an unproven peer must never receive the local secret:
        # the unresolvable case is exactly where a foreign listener on a crash-
        # freed port could be the far end. POSIX gets a real proof path (uid), so
        # a legitimate TCP-only gateway resolves to MATCH and is not refused.
        if _connected_peer_verdict(sock) is not _ConnectedPeerVerdict.MATCH:
            _audit_peer_denied(
                operation, port, "connected peer could not be proven to be this gateway"
            )
            raise _GatewayPeerRefused(
                "the peer on this established connection could not be proven to be "
                "this install's gateway. If this host cannot resolve a loopback "
                "connection's peer, install lsof so ownership can be proven, or run "
                "the gateway over its owner-only socket. " + _REPUBLISH_IDENTITY_HINT
            )
        _audit_peer_allowed(operation, port)

    return tcp_verified_urlopen(req, timeout, verify_peer=_verify_tcp_peer)


class _ConnectedPeerVerdict(enum.Enum):
    """Outcome of proving the peer on an established loopback TCP connection.

    ``MATCH`` -- the kernel 4-tuple table names the far end as this install's
    recorded gateway, owned by this account.
    ``MISMATCH`` -- anything else: the peer resolved to a different pid, a wrong
    start token, or a foreign owner, OR the host could not resolve the connected
    peer at all (macOS without ``lsof``; Linux reads ``/proc`` and Windows the
    kernel owner-PID table, so neither hits this). Both are one fail-closed
    decision, because an owner-minting secret must never be written to a peer
    that cannot be positively proven -- an unresolvable peer is exactly where a
    foreign listener on a crash-freed port could be the far end. POSIX proves
    ownership by uid (not SID), so a legitimate same-user TCP-only gateway
    resolves to ``MATCH``.
    """

    MATCH = "match"
    MISMATCH = "mismatch"


# Its members print as they did before the split: the enum keeps the facade's name.
_ConnectedPeerVerdict.__module__ = "kiro_crew.cli_server"


def _connected_tcp_peer_pid(sock) -> int | None:
    """The pid on the far end of THIS established loopback TCP connection.

    Reads the exact 4-tuple (not who owns the port now) via
    :func:`platform_compat.get_tcp_peer_pid` (Linux /proc inode, macOS lsof,
    Windows ``GetExtendedTcpTable``). That resolver returns the pid owning the
    socket whose ``local`` endpoint equals its ``client_endpoint`` argument and
    whose ``remote`` endpoint equals its ``server_endpoint`` argument. We hold the
    client's end, so to name the SERVER's pid we describe the server's socket: its
    local end is our ``getpeername()`` and its remote end is our ``getsockname()``.

    Retries briefly while the lookup returns ``None``: a connection the server has
    not yet ``accept()``ed sits in the kernel accept queue with inode 0, which the
    /proc resolver skips, so a healthy but slow-to-accept server would otherwise
    read as unresolvable. The window is short and bounded; a foreign or
    unresolvable peer stays ``None`` across it.

    Returns ``None`` when the endpoints are not an address pair or the peer could
    not be resolved. Shared by every loopback-TCP sender that must prove its peer
    before writing a credential, so they agree on how the peer is named.
    """
    local = sock.getsockname()
    peer = sock.getpeername()
    if not (isinstance(local, tuple) and isinstance(peer, tuple)):
        return None
    cli_endpoint = (str(local[0]), int(local[1]))
    gateway_endpoint = (str(peer[0]), int(peer[1]))
    deadline = time.monotonic() + _PEER_RESOLVE_RETRY_SECONDS
    peer_pid = platform_compat.get_tcp_peer_pid(
        server_endpoint=cli_endpoint, client_endpoint=gateway_endpoint
    )
    while peer_pid is None and time.monotonic() < deadline:
        time.sleep(_PEER_RESOLVE_RETRY_INTERVAL)
        peer_pid = platform_compat.get_tcp_peer_pid(
            server_endpoint=cli_endpoint, client_endpoint=gateway_endpoint
        )
    return peer_pid


def _connected_peer_verdict(sock) -> "_ConnectedPeerVerdict":
    """Prove the peer of THIS established connection the secret will travel over.

    Reads the far end of the exact 4-tuple (not who owns the port now, which a
    foreign listener can defeat by accepting the connection then releasing the
    port during a restart) via :func:`platform_compat.get_tcp_peer_pid` (Linux
    /proc inode, macOS lsof, Windows ``GetExtendedTcpTable``). That resolver
    returns the pid owning the socket whose ``local`` endpoint equals its
    ``client_endpoint`` argument and whose ``remote`` endpoint equals its
    ``server_endpoint`` argument. We hold the CLI's end of the connection, so to
    name the GATEWAY's pid we describe the gateway's socket: its local end is our
    ``getpeername()`` (``gateway_endpoint``, passed as ``client_endpoint``) and
    its remote end is our ``getsockname()`` (``cli_endpoint``, passed as
    ``server_endpoint``). That resolved pid must equal the gateway recorded for
    this port, with a matching start token, and be owned by this account.

    Returns ``MISMATCH`` for every outcome that is not a positively proven
    MATCH -- a resolved foreign peer, a wrong pid or start token, a foreign
    owner, or a host that cannot resolve the connected peer at all (lsof-less
    macOS). All fail closed: an unprovable peer never receives the owner-minting
    secret, never a port snapshot. POSIX proves ownership by uid, Windows by
    access-token owner SID.
    """
    try:
        peer = sock.getpeername()
        if not isinstance(peer, tuple):
            return _ConnectedPeerVerdict.MISMATCH
        peer_pid = _connected_tcp_peer_pid(sock)
        if peer_pid is None:
            # No unique connection resolved even after the retry window -- a
            # foreign peer, or a host that cannot resolve the peer at all (lsof-
            # less macOS). Both fail closed: an owner-minting secret is never
            # written to a peer that cannot be positively proven. The caller's
            # single refusal message carries the install-lsof / owner-only-socket
            # remedy.
            return _ConnectedPeerVerdict.MISMATCH
        pid = _recorded_gateway_pid(int(peer[1]))
        if pid is None or peer_pid != pid:
            return _ConnectedPeerVerdict.MISMATCH
        # Prove the peer process is owned by THIS account, in the form each
        # platform can bear: uid on POSIX (os.getuid vs the pid's owner uid),
        # access-token owner SID on Windows (no uid there). Using the SID on
        # POSIX would always be None -> a legitimate same-user TCP-only gateway
        # would never reach MATCH and would be refused; the uid check is the
        # POSIX equivalent that lets it verify. Fails closed (MISMATCH) if
        # ownership is unresolvable on either platform.
        if platform_compat.IS_POSIX:
            owner_uid = platform_compat.process_owner_uid(pid)
            if owner_uid is not None and owner_uid == os.getuid():
                return _ConnectedPeerVerdict.MATCH
            return _ConnectedPeerVerdict.MISMATCH
        me = platform_compat.current_user_sid()
        owner_sid = platform_compat.process_owner_sid(pid)
        if me is not None and owner_sid is not None and owner_sid == me:
            return _ConnectedPeerVerdict.MATCH
        return _ConnectedPeerVerdict.MISMATCH
    except Exception:
        return _ConnectedPeerVerdict.MISMATCH
