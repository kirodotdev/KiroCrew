"""Defense-in-depth helpers for the gatewayd local endpoint.

The endpoint carries every tool call and every tool result for every pooled
session, so two questions have to be answerable at accept time:

* **Is the peer the same principal as us?** :func:`check_peer_is_self` answers
  it deny-by-default -- ``MATCH`` only on positive confirmation, never as a
  consequence of a failed lookup.
* **Which process is on the other end?** :func:`get_peer_pid` answers it, and
  gatewayd uses the PID two ways: to walk the peer's real ancestry for a
  session-key file when a stub registers without one, and to index the
  connection under the peer's *host* PID chain so a later ``claim`` frame
  (which carries a host PID) lands on the right connection even when the
  stub's self-reported PIDs are namespace-local.

Both are per-platform mechanisms rather than one portable call:

===========  ====================================  =========================
Platform     Peer principal                        Peer PID
===========  ====================================  =========================
Linux        ``SO_PEERCRED`` uid                   ``SO_PEERCRED`` pid
Windows      owning-process token SID              ``GetNamedPipeClientProcessId``
macOS        ``LOCAL_PEERCRED`` xucred uid          ``LOCAL_PEERPID``
===========  ====================================  =========================

All three platforms now fail closed: every one of them is in
:data:`PEER_IDENTITY_SUPPORTED`, so an ``UNVERIFIABLE`` lookup is refused rather
than waved through. macOS was the last holdout, and the reason it waited is worth
keeping: turning failed lookups into refusals *before* a real Mac had answered
``MATCH`` is exactly how the Windows port shipped a gate that denied 100% of
connections while presenting as merely strict.

That evidence now exists, and it is enforced rather than merely observed. The
macOS CI job runs ``test_macos_check_matches_a_socket_we_connected_to_ourselves``
by node id and fails unless it PASSES, so the canary cannot quietly stop running
and leave this gate unproven. It deliberately exercises an *accepted* socket
(``transport.serve`` + ``connect``) rather than a socketpair, because the kernel
populates peer credentials by different paths for the two: ``UNP_HAVEPC`` is set
at ``connect()``, while an accepted socket takes the listener's cached cred. A
passing socketpair therefore would not have implied a passing endpoint.

The residual risk is bounded by the stub, not by this module. When gatewayd
refuses a connection the stub's handshake raises ``FallbackRequestedError`` and
it then ``execvpe`` the target backend directly, appending a record to
``stub_fallback.jsonl``. So a ``LOCAL_PEERCRED`` failure on some Mac
configuration costs pooling and leaves an audit trail; it does not break the
session. :func:`socket_owner_only` consequently no longer guards any supported
platform's admission path -- it is the fallback for a POSIX platform that has
neither ``SO_PEERCRED`` nor Darwin's option.

Stdlib-only (``socket``, ``struct``, ``ctypes``, ``os``, ``logging``) plus the
``platform_compat`` leaf; no asyncio imports, so this module is safe to call
from synchronous setup paths (:func:`run_gatewayd` startup) as well as from
async connection handlers.
"""

from __future__ import annotations

import ctypes
import enum
import logging
import os
import socket as _socket
import stat
import struct
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)

# ``ctypes.WinDLL`` exists only in the Windows ctypes stubs; reach it through an
# Any-typed alias so a POSIX mypy run does not flag call sites that are already
# inside Windows-only code paths.
_ct: Any = ctypes

# ``struct ucred`` on Linux is three native unsigned ints: pid, uid, gid.
# ``@`` keeps the platform's native byte order and alignment so the size
# matches what the kernel hands back via ``getsockopt``.
_UCRED_FMT = "@iII"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)

# ``SO_PEERCRED`` lives on ``socket`` on Linux but is absent on macOS /
# Windows. Detect once at import time so callers can branch cheaply.
_SO_PEERCRED: int | None = getattr(_socket, "SO_PEERCRED", None)

# macOS peer-pid option. ``SOL_LOCAL`` is 0 and ``LOCAL_PEERPID`` is 2 in
# ``<sys/un.h>``; Python does not export either, so they are literals. Guarded
# by a darwin check at every use, and any failure degrades to ``None``.
_SOL_LOCAL = 0
_LOCAL_PEERPID = 2

#: ``LOCAL_PEERCRED`` from Darwin's ``bsd/sys/un.h`` (``0x001``). Python's
#: ``socket`` module exposes none of the ``SOL_LOCAL`` options, hence the
#: literal -- same reason ``_LOCAL_PEERPID`` above is a literal.
_LOCAL_PEERCRED = 1

#: ``struct xucred`` from Darwin's ``bsd/sys/ucred.h``::
#:
#:     u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]
#:
#: ``uid_t``/``gid_t`` are ``__uint32_t`` on Darwin irrespective of pointer
#: width, so the layout is identical on arm64 and x86_64. The ``@`` prefix keeps
#: native alignment, which supplies the two padding bytes between the ``short``
#: and the ``gid_t`` array -- do not "simplify" it to ``=``, which would drop the
#: padding and mis-size the struct.
_XUCRED_FMT = "@IIh16I"
_XUCRED_SIZE = struct.calcsize(_XUCRED_FMT)  # 76

#: ``XUCRED_VERSION``. A different value means the kernel handed back a layout
#: this parser does not understand, which must degrade rather than be guessed at.
_XUCRED_VERSION = 0

# Windows token constants.
_TOKEN_QUERY = 0x0008  # noqa: N806 - Windows API constant
_TOKEN_USER_CLASS = 1  # TokenUser  # noqa: N806 - Windows API constant

#: ``True`` when this platform can positively confirm the peer's principal, so
#: callers fail closed on ``UNVERIFIABLE`` instead of treating it as allow. True
#: for all three supported platforms, each by its own mechanism: Linux
#: ``SO_PEERCRED``, Windows token-SID comparison, macOS ``LOCAL_PEERCRED``. It is
#: ``False`` only on a POSIX platform with none of them, where the caller falls
#: back to :func:`socket_owner_only`.
#:
#: macOS is included on the strength of an enforced real-hardware canary; see the
#: module docstring for the evidence and for why the blast radius of a Darwin
#: getsockopt failure is lost pooling rather than a broken session.
PEER_IDENTITY_SUPPORTED: bool = (
    _SO_PEERCRED is not None
    or platform_compat.IS_WINDOWS
    or platform_compat.IS_MACOS
)


class PeerCredResult(enum.Enum):
    """Outcome of a peer-principal check.

    Deny-by-default authorization primitive: ``MATCH`` is returned ONLY when
    the OS positively confirms the peer is the same principal as this process.
    A failure to verify is never conflated with permission -- it surfaces as
    ``UNVERIFIABLE`` so the *caller* makes an explicit policy decision instead
    of the primitive silently failing open.
    """

    MATCH = "match"            # peer principal positively confirmed == ours
    MISMATCH = "mismatch"      # peer principal positively confirmed != ours (DENY)
    UNVERIFIABLE = "unverifiable"  # could not be read (see check_peer_is_self)


def chmod_socket_0600(path: Path) -> None:
    """Best-effort tighten of ``path`` to mode ``0600``.

    Logs and swallows ``OSError`` -- a chmod failure on the gatewayd
    socket is worth surfacing in the log but must not abort daemon
    startup. The directory-permission gate (``$KIROCREW_HOME`` defaults
    to ``0700``) is the primary access boundary; this is defense in
    depth.

    POSIX only in effect. On Windows the endpoint is a named pipe with no
    filesystem entry and mode bits carry no access meaning; its owner-only
    DACL is applied at creation instead (see ``transport``).
    """
    # chmod_safe already logs + swallows OSError internally (and is a no-op on
    # Windows), so no try/except wrapper here — this is best-effort defense in
    # depth, not a fail-loud boundary (the 0700 home-dir gate is the primary one).
    platform_compat.chmod_safe(path, 0o600)


def socket_owner_only(path: Path) -> bool:
    """Return ``True`` iff the socket file at ``path`` is owner-only (no group
    or other permission bits set).

    This is the filesystem access gate the gateway falls back to where the
    peer's principal cannot be confirmed. No supported platform reaches it any
    more -- Linux reads ``SO_PEERCRED``, Windows compares SIDs and macOS reads
    ``LOCAL_PEERCRED``, so all three fail closed on ``UNVERIFIABLE`` instead.
    It remains for a POSIX platform with none of those mechanisms. A 0600 socket
    already prevents any other uid from ``connect()``-ing. Returns ``False``
    (deny) when the file is missing or any group/other bit is set, so the caller
    can fail closed instead of allowing an unverifiable connection through.

    Never reached on Windows (``PEER_IDENTITY_SUPPORTED`` is ``True`` there),
    which matters because ``st_mode`` is synthetic on Windows and this test
    would be meaningless. Kept rather than deleted because deleting it would
    make an unverifiable peer on an unlisted POSIX platform an *allow*.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        logger.warning("socket_owner_only: stat(%s) failed: %s", path, exc)
        return False
    return mode & 0o077 == 0


# --- Peer PID ----------------------------------------------------------------


def get_peer_pid(transport_or_sock: Any) -> int | None:
    """Extract the peer's PID, or ``None`` when it cannot be read.

    The PID is as seen in *this* process's namespace -- the real host PID when
    gatewayd runs on the host -- which is what makes it usable for claim-frame
    indexing where a stub's self-reported PIDs may be namespace-local.

    Dispatches per platform (see the module docstring). Returns ``None`` rather
    than raising on every failure path, so a caller that cannot get a PID
    simply loses the identity-resolution channel instead of dropping the
    connection.
    """
    if platform_compat.IS_WINDOWS:
        handle = _resolve_pipe_handle(transport_or_sock)
        if handle is None:
            return None
        return _windows_peer_pid(handle)

    sock = _resolve_socket(transport_or_sock)
    if sock is None:
        return None
    if sock.family != _socket.AF_UNIX:
        return None

    if _SO_PEERCRED is not None:
        try:
            raw = sock.getsockopt(_socket.SOL_SOCKET, _SO_PEERCRED, _UCRED_SIZE)
        except OSError:
            return None
        try:
            pid, _uid, _gid = struct.unpack(_UCRED_FMT, raw)
        except struct.error:  # pragma: no cover
            return None
        return pid if pid > 0 else None

    if platform_compat.IS_MACOS:
        # LOCAL_PEERPID yields a single native int. Additive: any failure falls
        # through to None, which is what this function returned on macOS before.
        try:
            raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID, struct.calcsize("@i"))
        except OSError as exc:
            logger.debug("get_peer_pid: getsockopt(LOCAL_PEERPID) failed: %s", exc)
            return None
        try:
            (pid,) = struct.unpack("@i", raw)
        except struct.error as exc:  # pragma: no cover
            logger.debug("get_peer_pid: LOCAL_PEERPID unpack failed: %s", exc)
            return None
        return pid if pid > 0 else None

    return None


def _windows_peer_pid(pipe_handle: int) -> int | None:
    """Peer PID of a connected server pipe handle, or ``None``."""
    from ctypes import wintypes

    try:
        kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
        kernel32.GetNamedPipeClientProcessId.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        ]
        kernel32.GetNamedPipeClientProcessId.restype = wintypes.BOOL
        out = wintypes.ULONG()
        if not kernel32.GetNamedPipeClientProcessId(
            wintypes.HANDLE(pipe_handle), ctypes.byref(out)
        ):
            logger.debug(
                "get_peer_pid: GetNamedPipeClientProcessId failed: %s",
                _ct.get_last_error(),
            )
            return None
        pid = int(out.value)
        return pid if pid > 0 else None
    except Exception as exc:  # noqa: BLE001 - a probe failure must not drop the peer
        logger.debug("get_peer_pid: Windows lookup raised: %s", exc)
        return None


# --- Peer principal ----------------------------------------------------------


def _macos_check_peer_is_self(sock: _socket.socket) -> PeerCredResult:
    """macOS peer-principal check via ``getsockopt(SOL_LOCAL, LOCAL_PEERCRED)``.

    Darwin's analogue of Linux ``SO_PEERCRED``: the kernel captures the peer's
    credentials at ``connect()`` time and hands back a ``struct xucred`` whose
    ``cr_uid`` is the peer's effective uid. Comparing that against our own uid
    answers the only question the admission gate asks -- is the peer the same
    principal as this process?

    Stream sockets only: the kernel returns ``EINVAL`` for ``SOCK_DGRAM``. The
    gateway endpoint is ``SOCK_STREAM``, and a non-``AF_UNIX`` socket is rejected
    by the caller before reaching here.

    IMPORTANT -- every failure path returns ``UNVERIFIABLE``, never ``MISMATCH``.
    ``MISMATCH`` is reserved for a *positively parsed* xucred naming a different
    uid, because the caller treats ``MISMATCH`` as a hard refusal and a
    misparsed struct must not be able to lock a user out of their own gateway.
    An unexpected ``cr_version`` or a short buffer therefore degrades rather
    than being guessed at.
    """
    try:
        raw = sock.getsockopt(_SOL_LOCAL, _LOCAL_PEERCRED, _XUCRED_SIZE)
    except OSError as exc:
        logger.debug(
            "check_peer_is_self: getsockopt(LOCAL_PEERCRED) failed: %s", exc
        )
        return PeerCredResult.UNVERIFIABLE
    if len(raw) < _XUCRED_SIZE:
        logger.debug(
            "check_peer_is_self: LOCAL_PEERCRED returned %d bytes, want %d",
            len(raw),
            _XUCRED_SIZE,
        )
        return PeerCredResult.UNVERIFIABLE
    try:
        version, peer_uid = struct.unpack(_XUCRED_FMT, raw[:_XUCRED_SIZE])[:2]
    except struct.error as exc:  # pragma: no cover - guarded by the length check
        logger.debug("check_peer_is_self: xucred unpack failed: %s", exc)
        return PeerCredResult.UNVERIFIABLE
    if version != _XUCRED_VERSION:
        logger.debug(
            "check_peer_is_self: xucred cr_version=%d, expected %d -- refusing to "
            "interpret an unknown layout",
            version,
            _XUCRED_VERSION,
        )
        return PeerCredResult.UNVERIFIABLE
    own_uid = os.getuid()
    if peer_uid == own_uid:
        return PeerCredResult.MATCH
    logger.warning(
        "check_peer_is_self: peer uid %d is not this process's uid %d",
        peer_uid,
        own_uid,
    )
    return PeerCredResult.MISMATCH


def check_peer_is_self(transport_or_sock: Any) -> PeerCredResult:
    """Positively verify the peer runs as the same principal as this process.

    ``transport_or_sock`` may be a raw :class:`socket.socket` (the test path and
    any synchronous caller) or an asyncio transport / stream writer, from which
    the underlying socket -- or, on Windows, the pipe handle -- is extracted via
    ``get_extra_info``.

    Returns (deny-by-default -- never ``MATCH`` unless positively confirmed):

    * :attr:`PeerCredResult.MATCH` -- the OS reports the peer principal and it
      is ours.
    * :attr:`PeerCredResult.MISMATCH` -- the OS reports the peer principal and
      it is NOT ours. Callers MUST reject.
    * :attr:`PeerCredResult.UNVERIFIABLE` -- it could not be read: no
      underlying socket or pipe, the platform has no mechanism (macOS), the
      socket is not ``AF_UNIX``, the syscall failed, or the payload was
      malformed. The primitive does NOT decide policy for this case -- the
      caller does (see ``gatewayd._handle_connection``), so a platform without
      a mechanism never silently grants access here.
    """
    if platform_compat.IS_WINDOWS:
        return _windows_check_peer_is_self(transport_or_sock)

    sock = _resolve_socket(transport_or_sock)
    if sock is None:
        logger.debug(
            "check_peer_is_self: no underlying socket on %r",
            type(transport_or_sock).__name__,
        )
        return PeerCredResult.UNVERIFIABLE
    if _SO_PEERCRED is None:
        if platform_compat.IS_MACOS:
            return _macos_check_peer_is_self(sock)
        logger.debug("check_peer_is_self: no peer-principal mechanism on this platform")
        return PeerCredResult.UNVERIFIABLE
    if sock.family != _socket.AF_UNIX:
        logger.debug("check_peer_is_self: socket family=%r is not AF_UNIX", sock.family)
        return PeerCredResult.UNVERIFIABLE
    try:
        raw = sock.getsockopt(_socket.SOL_SOCKET, _SO_PEERCRED, _UCRED_SIZE)
    except OSError as exc:
        logger.debug("check_peer_is_self: getsockopt(SO_PEERCRED) failed: %s", exc)
        return PeerCredResult.UNVERIFIABLE
    try:
        _pid, peer_uid, _gid = struct.unpack(_UCRED_FMT, raw)
    except struct.error as exc:  # pragma: no cover — kernel ABI guarantees the size
        logger.debug("check_peer_is_self: struct.unpack failed: %s", exc)
        return PeerCredResult.UNVERIFIABLE
    expected_uid = os.getuid()
    if peer_uid == expected_uid:
        return PeerCredResult.MATCH
    logger.warning(
        "check_peer_is_self: peer_uid=%d != our uid=%d", peer_uid, expected_uid,
    )
    return PeerCredResult.MISMATCH


def _windows_check_peer_is_self(transport_or_sock: Any) -> PeerCredResult:
    """Compare the pipe client's owning user SID against ours.

    Resolves the peer's PID from the pipe (``GetNamedPipeClientProcessId``) and
    reads *that process's* access token. Deliberately NOT
    ``ImpersonateNamedPipeClient``, for two reasons:

    * **Ordering.** Per Microsoft's documentation, impersonation adopts "the
      security context of the last message read from the pipe". This check runs
      at connection admission -- before the Register frame is read -- so there
      is no message to derive a context from, and the call fails (or produces an
      anonymous token that ``OpenThreadToken`` then refuses). Because the
      admission gate is deny-by-default, that failure rejected *every* Windows
      connection. Reading the peer process's own token has no ordering
      requirement, so the gate can stay where it belongs: before any work.
    * **Blast radius.** Impersonation attaches the peer's token to *our* thread
      -- here the event loop thread -- and correctness then depends on
      ``RevertToSelf`` always succeeding. Inspecting the peer's token borrows
      nothing, so that entire failure mode is gone rather than guarded.

    Returns UNVERIFIABLE (never MATCH) whenever any step cannot be completed;
    the caller treats anything short of MATCH as a rejection.
    """
    handle = _resolve_pipe_handle(transport_or_sock)
    if handle is None:
        logger.debug(
            "check_peer_is_self: no pipe handle on %r",
            type(transport_or_sock).__name__,
        )
        return PeerCredResult.UNVERIFIABLE

    ours = platform_compat.current_user_sid()
    if not ours:
        logger.debug("check_peer_is_self: own SID unavailable")
        return PeerCredResult.UNVERIFIABLE

    peer_pid = _windows_peer_pid(handle)
    if peer_pid is None:
        logger.debug("check_peer_is_self: peer pid unavailable")
        return PeerCredResult.UNVERIFIABLE

    peer_sid = platform_compat.process_owner_sid(peer_pid)
    if not peer_sid:
        logger.debug("check_peer_is_self: peer owner SID unavailable")
        return PeerCredResult.UNVERIFIABLE

    # Both sides are canonical SID strings (ConvertSidToStringSidW), whose
    # alphabet is 'S', digits and hyphens -- so no two distinct SIDs can fold
    # together and this cannot admit a foreign principal. A binary EqualSid
    # would be equivalent at more ctypes surface.
    if peer_sid.casefold() == ours.casefold():
        return PeerCredResult.MATCH
    logger.warning(
        "check_peer_is_self: peer sid=%s != our sid=%s", peer_sid, ours,
    )
    return PeerCredResult.MISMATCH


def _windows_server_pid(pipe_handle: int) -> int | None:
    """PID of the process serving a connected client pipe handle, or ``None``.

    Mirror of :func:`_windows_peer_pid` for the other direction.
    """
    from ctypes import wintypes

    try:
        kernel32 = _ct.WinDLL("kernel32", use_last_error=True)
        kernel32.GetNamedPipeServerProcessId.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.ULONG),
        ]
        kernel32.GetNamedPipeServerProcessId.restype = wintypes.BOOL
        out = wintypes.ULONG()
        if not kernel32.GetNamedPipeServerProcessId(
            wintypes.HANDLE(pipe_handle), ctypes.byref(out)
        ):
            logger.debug(
                "check_server_is_self: GetNamedPipeServerProcessId failed: %s",
                _ct.get_last_error(),
            )
            return None
        pid = int(out.value)
        return pid if pid > 0 else None
    except Exception as exc:  # noqa: BLE001 - deny-by-default on any surprise
        logger.debug("check_server_is_self: Windows lookup raised: %s", exc)
        return None


def check_server_is_self(transport_or_pipe: Any) -> PeerCredResult:
    """Verify the pipe we just connected to is served by our own principal.

    The client-side counterpart of :func:`check_peer_is_self`, and Windows-only
    by necessity rather than by choice. On POSIX the endpoint lives in a 0700
    directory, so no other principal can create a socket at that path and the
    question cannot arise. The Windows pipe namespace is machine-global and the
    name is derived from a hash of a well-known path, so a local principal can
    pre-create ``\\\\.\\pipe\\kirocrew-mcp-<hash>`` before the daemon binds and
    receive whatever connects.

    ``FILE_FLAG_FIRST_PIPE_INSTANCE`` already stops a squatter from *joining* a
    daemon that is already listening -- our own bind fails loudly instead of
    silently sharing. What it cannot do is protect a *client* that connects
    while the squatter holds the name: the stub would hand over its ``register``
    and ``claim`` frames, which carry session keys.

    Checked BEFORE the first write, which also closes the impersonation angle:
    ``ImpersonateNamedPipeClient`` adopts the context of "the last message read
    from the pipe", so a server that never receives a message has nothing to
    impersonate us from. That is why this is preferred over passing
    ``SECURITY_SQOS_PRESENT | SECURITY_IDENTIFICATION`` on the client handle --
    those flags would only limit what a squatter could do with our token, while
    this refuses to talk to it at all, and reaching them would mean replacing
    asyncio's private ``connect_pipe`` on the connect path.

    Returns UNVERIFIABLE (never MATCH) when any step fails; the caller refuses
    the connection, which degrades to a per-session MCP server rather than
    trusting an unattributable endpoint.
    """
    if not platform_compat.IS_WINDOWS:
        return PeerCredResult.MATCH

    handle = _resolve_pipe_handle(transport_or_pipe)
    if handle is None:
        logger.debug(
            "check_server_is_self: no pipe handle on %r",
            type(transport_or_pipe).__name__,
        )
        return PeerCredResult.UNVERIFIABLE

    ours = platform_compat.current_user_sid()
    if not ours:
        logger.debug("check_server_is_self: own SID unavailable")
        return PeerCredResult.UNVERIFIABLE

    server_pid = _windows_server_pid(handle)
    if server_pid is None:
        return PeerCredResult.UNVERIFIABLE

    server_sid = platform_compat.process_owner_sid(server_pid)
    if not server_sid:
        logger.debug("check_server_is_self: server owner SID unavailable")
        return PeerCredResult.UNVERIFIABLE

    if server_sid.casefold() == ours.casefold():
        return PeerCredResult.MATCH
    logger.warning(
        "check_server_is_self: pipe server sid=%s != our sid=%s -- refusing",
        server_sid, ours,
    )
    return PeerCredResult.MISMATCH


# --- Handle / socket resolution ----------------------------------------------


def _has_sock_api(obj: Any) -> bool:
    """True if ``obj`` exposes ``family`` plus a callable ``getsockopt`` --
    satisfied by both :class:`socket.socket` and asyncio's ``TransportSocket``
    wrapper."""
    return (
        obj is not None
        and hasattr(obj, "family")
        and callable(getattr(obj, "getsockopt", None))
    )


def _resolve_socket(transport_or_sock: Any) -> Any:
    """Coerce a raw socket, an asyncio transport / stream-writer, or asyncio's
    ``TransportSocket`` wrapper into an object exposing ``family`` +
    ``getsockopt`` (or ``None`` if none is reachable).

    asyncio's ``get_extra_info("socket")`` returns an
    ``asyncio.trsock.TransportSocket`` -- NOT a ``socket.socket`` -- which
    proxies ``family`` and ``getsockopt`` to the underlying socket. We accept
    it (and any object exposing those two members) so ``SO_PEERCRED`` can be
    read off a live asyncio connection; a strict ``isinstance(socket.socket)``
    check would silently degrade to ``UNVERIFIABLE`` on every real gateway
    connection, defeating the check.
    """
    if _has_sock_api(transport_or_sock):
        return transport_or_sock
    get_extra_info = getattr(transport_or_sock, "get_extra_info", None)
    if callable(get_extra_info):
        sock = get_extra_info("socket")
        if _has_sock_api(sock):
            return sock
    return None


def _resolve_pipe_handle(transport_or_pipe: Any) -> int | None:
    """Raw Win32 HANDLE of a connected named pipe, or ``None``.

    The Windows counterpart of :func:`_resolve_socket`. asyncio's proactor
    transports publish the ``PipeHandle`` under ``get_extra_info("pipe")``, a
    public seam, so peer identity on Windows costs no private-API surface. An
    integer or an object exposing ``.handle`` is also accepted so tests can
    pass either.
    """
    if isinstance(transport_or_pipe, int):
        return transport_or_pipe
    handle = getattr(transport_or_pipe, "handle", None)
    if isinstance(handle, int):
        return handle
    get_extra_info = getattr(transport_or_pipe, "get_extra_info", None)
    if callable(get_extra_info):
        pipe = get_extra_info("pipe")
        if isinstance(pipe, int):
            return pipe
        handle = getattr(pipe, "handle", None)
        if isinstance(handle, int):
            return handle
    return None
