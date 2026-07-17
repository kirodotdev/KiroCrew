"""Defense-in-depth helpers for the gatewayd unix socket (Mesh 81785a39).

Two narrow, self-contained primitives:

* :func:`chmod_socket_0600` — best-effort tighten of the bound socket
  file's permissions to ``0600`` so only the owning UID can ``connect()``.
  The default ``umask`` already produces ``0755`` / ``0775`` which is
  more permissive than necessary for a per-user IPC endpoint.

* :func:`check_peer_uid` — read the connecting peer's UID from
  ``SO_PEERCRED`` and return a tri-state :class:`PeerCredResult`
  (``MATCH`` / ``MISMATCH`` / ``UNVERIFIABLE``). Deny-by-default: it
  returns ``MATCH`` only when the kernel positively confirms the peer
  uid equals ``expected_uid``. When the uid cannot be read (no
  ``SO_PEERCRED``, non-``AF_UNIX`` socket, ``getsockopt`` error) it
  returns ``UNVERIFIABLE`` rather than silently granting access — the
  caller decides policy for that case. Used inside the connection
  handler as a belt-and-braces check after the directory-permission
  gate.

Stdlib-only (``socket``, ``struct``, ``os``, ``logging``) plus the
``platform_compat`` leaf (itself stdlib-only); no asyncio imports so this
module is safe to call from synchronous setup paths
(:func:`run_gatewayd` startup) as well as from async connection
handlers.
"""

from __future__ import annotations

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

# ``struct ucred`` on Linux is three native unsigned ints: pid, uid, gid.
# ``@`` keeps the platform's native byte order and alignment so the size
# matches what the kernel hands back via ``getsockopt``.
_UCRED_FMT = "@iII"
_UCRED_SIZE = struct.calcsize(_UCRED_FMT)

# ``SO_PEERCRED`` lives on ``socket`` on Linux but is absent on macOS /
# Windows. Detect once at import time so callers can branch cheaply.
_SO_PEERCRED: int | None = getattr(_socket, "SO_PEERCRED", None)

# Public capability flag: ``True`` when this platform can read peer
# credentials via ``SO_PEERCRED`` (Linux). Callers use it as an explicit,
# documented platform guard to decide policy for the "cannot verify" case —
# deny-by-default where verification is possible, and a deliberate fallback
# to filesystem permissions where it is structurally impossible (macOS) —
# rather than silently treating ``UNVERIFIABLE`` as allow.
PEERCRED_SUPPORTED: bool = _SO_PEERCRED is not None


class PeerCredResult(enum.Enum):
    """Outcome of a SO_PEERCRED peer-uid check.

    Deny-by-default authorization primitive: ``MATCH`` is returned ONLY when
    the kernel positively confirms the peer uid equals the expected uid. A
    failure to verify is never conflated with permission — it surfaces as
    ``UNVERIFIABLE`` so the *caller* makes an explicit policy decision instead
    of the primitive silently failing open.
    """

    MATCH = "match"            # peer uid positively confirmed == expected
    MISMATCH = "mismatch"      # peer uid positively confirmed != expected (DENY)
    UNVERIFIABLE = "unverifiable"  # uid could not be read (see check_peer_uid)


def chmod_socket_0600(path: Path) -> None:
    """Best-effort tighten of ``path`` to mode ``0600``.

    Logs and swallows ``OSError`` — a chmod failure on the gatewayd
    socket is worth surfacing in the log but must not abort daemon
    startup. The directory-permission gate (``$KIROCREW_HOME`` defaults
    to ``0700``) is the primary access boundary; this is defense in
    depth.
    """
    # chmod_safe already logs + swallows OSError internally (and is a no-op on
    # Windows), so no try/except wrapper here — this is best-effort defense in
    # depth, not a fail-loud boundary (the 0700 home-dir gate is the primary one).
    platform_compat.chmod_safe(path, 0o600)


def socket_owner_only(path: Path) -> bool:
    """Return ``True`` iff the socket file at ``path`` is owner-only (no group
    or other permission bits set).

    This is the filesystem access gate the gateway falls back to where
    ``SO_PEERCRED`` is unavailable (e.g. macOS): a 0600 socket already prevents
    any other uid from ``connect()``-ing. Returns ``False`` (deny) when the
    file is missing or any group/other bit is set, so the caller can fail
    closed instead of allowing an unverifiable connection through.
    """
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError as exc:
        logger.warning("socket_owner_only: stat(%s) failed: %s", path, exc)
        return False
    return mode & 0o077 == 0


def check_peer_uid(transport_or_sock: Any, expected_uid: int) -> PeerCredResult:
    """Positively verify the connecting peer's uid via ``SO_PEERCRED``.

    ``transport_or_sock`` may be a raw :class:`socket.socket` (the test path
    and any synchronous caller) or an asyncio transport / stream writer, from
    which the underlying socket is extracted via ``get_extra_info("socket")``.

    Returns (deny-by-default — never ``MATCH`` unless positively confirmed):

    * :attr:`PeerCredResult.MATCH` -- the kernel reports the peer uid and it
      equals ``expected_uid``.
    * :attr:`PeerCredResult.MISMATCH` -- the kernel reports the peer uid and it
      does NOT equal ``expected_uid``. Callers MUST reject.
    * :attr:`PeerCredResult.UNVERIFIABLE` -- the uid could not be read: no
      underlying socket, the platform lacks ``SO_PEERCRED`` (macOS/Windows),
      the socket is not ``AF_UNIX``, ``getsockopt`` raised, or the ``ucred``
      was malformed. The primitive does NOT decide policy for this case — the
      caller does (see ``gatewayd._handle_connection``), so a non-Linux
      platform never silently grants access here.
    """
    sock = _resolve_socket(transport_or_sock)
    if sock is None:
        logger.debug(
            "check_peer_uid: no underlying socket on %r",
            type(transport_or_sock).__name__,
        )
        return PeerCredResult.UNVERIFIABLE
    if _SO_PEERCRED is None:
        logger.debug("check_peer_uid: SO_PEERCRED unavailable on this platform")
        return PeerCredResult.UNVERIFIABLE
    if sock.family != _socket.AF_UNIX:
        logger.debug("check_peer_uid: socket family=%r is not AF_UNIX", sock.family)
        return PeerCredResult.UNVERIFIABLE
    try:
        raw = sock.getsockopt(_socket.SOL_SOCKET, _SO_PEERCRED, _UCRED_SIZE)
    except OSError as exc:
        logger.debug("check_peer_uid: getsockopt(SO_PEERCRED) failed: %s", exc)
        return PeerCredResult.UNVERIFIABLE
    try:
        _pid, peer_uid, _gid = struct.unpack(_UCRED_FMT, raw)
    except struct.error as exc:  # pragma: no cover — kernel ABI guarantees the size
        logger.debug("check_peer_uid: struct.unpack failed: %s", exc)
        return PeerCredResult.UNVERIFIABLE
    if peer_uid == expected_uid:
        return PeerCredResult.MATCH
    logger.warning(
        "check_peer_uid: peer_uid=%d != expected_uid=%d", peer_uid, expected_uid,
    )
    return PeerCredResult.MISMATCH


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
    read off a live asyncio connection; the previous strict
    ``isinstance(socket.socket)`` check silently degraded to ``UNVERIFIABLE``
    on every real gateway connection, defeating the check.
    """
    if _has_sock_api(transport_or_sock):
        return transport_or_sock
    get_extra_info = getattr(transport_or_sock, "get_extra_info", None)
    if callable(get_extra_info):
        sock = get_extra_info("socket")
        if _has_sock_api(sock):
            return sock
    return None
