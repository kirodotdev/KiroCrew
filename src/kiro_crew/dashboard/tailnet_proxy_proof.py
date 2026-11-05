"""Kernel proof that a loopback connection was opened by the Tailscale daemon.

``tailscale serve`` reaches the gateway over loopback and names the remote
person in ``X-Forwarded-For``. Loopback alone proves only that the caller runs on
this host: any local process holding a dashboard token can send the same header.
So a forwarded identity is read only when the kernel says the client end of the
socket belongs to the daemon.

The proof, Linux only (``/proc`` is the source):

1. Find the client socket in ``/proc/net/tcp{,6}``: its local end is the
   request's peer address and its remote end is the gateway's own listening
   address, in state ESTABLISHED. The row carries the socket owner's uid.
2. The owner uid differs from the gateway's effective uid. A process running as
   the gateway's own user can read the gateway's tokens, so it can never be the
   trusted proxy. A daemon running as that same user is therefore not provable.
3. The owner uid is the uid of a running process named ``tailscaled``.

Every other platform, and every read failure, answers ``False``: no forwarded
identity, so the allowlist alone decides admission and nobody is named.
:func:`proof_support` says at start why the proof cannot succeed on this host.
Nothing here raises. The blocking reads run on the caller's subprocess executor.
"""

from __future__ import annotations

import ipaddress
import struct
import sys
import threading
import time
from pathlib import Path

from kiro_crew import platform_compat

__all__ = [
    "PROOF_DAEMON_NOT_FOUND",
    "PROOF_DAEMON_SAME_USER",
    "PROOF_PLATFORM_UNPROVABLE",
    "loopback_client_is_daemon",
    "proof_support",
]

#: Why :func:`loopback_client_is_daemon` cannot succeed on this host.
PROOF_PLATFORM_UNPROVABLE = "platform_unprovable"
PROOF_DAEMON_SAME_USER = "daemon_same_user"
PROOF_DAEMON_NOT_FOUND = "daemon_not_found"

_PROC = Path("/proc")
_TCP_ESTABLISHED = "01"
_DAEMON_COMM = "tailscaled"
#: The daemon's uid is stable for its process lifetime; a short TTL bounds the
#: cost of the ``/proc`` scan under a request storm.
_DAEMON_UID_TTL_SECS = 30.0

_daemon_uid_lock = threading.Lock()
_daemon_uid_cache: tuple[float, frozenset[int]] | None = None


def _effective_uid() -> int | None:
    """This process's effective uid, or ``None`` on a platform with no uid."""
    return platform_compat.effective_uid()


def _decode(
    hex_addr: str, hex_port: str
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, int]:
    """Decode one ``/proc/net/tcp{,6}`` address column into ``(ip, port)``."""
    port = int(hex_port, 16)
    if len(hex_addr) == 8:
        return ipaddress.IPv4Address(struct.pack("<I", int(hex_addr, 16))), port
    words = [struct.pack("<I", int(hex_addr[i : i + 8], 16)) for i in range(0, 32, 8)]
    return ipaddress.IPv6Address(b"".join(words)), port


def _norm(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse *addr*, folding an IPv4-mapped IPv6 address to IPv4."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _socket_owner_uid(peer: tuple[str, int], local: tuple[str, int]) -> int | None:
    """The uid owning the client socket ``peer -> local``, or ``None``."""
    want_peer_ip, want_local_ip = _norm(peer[0]), _norm(local[0])
    if want_peer_ip is None or want_local_ip is None:
        return None
    for name in ("tcp", "tcp6"):
        try:
            rows = (_PROC / "net" / name).read_text().splitlines()[1:]
        except OSError:
            continue
        for row in rows:
            cols = row.split()
            # sl local remote st tx:rx tr:when retrnsmt uid timeout inode
            if len(cols) < 8 or cols[3] != _TCP_ESTABLISHED:
                continue
            try:
                lip, lport = _decode(*cols[1].split(":"))
                rip, rport = _decode(*cols[2].split(":"))
                uid = int(cols[7])
            except (ValueError, TypeError, struct.error):
                continue
            if lport != peer[1] or rport != local[1]:
                continue
            if _norm(str(lip)) == want_peer_ip and _norm(str(rip)) == want_local_ip:
                return uid
    return None


def _scan_daemon_uids() -> frozenset[int]:
    """Uids of every running ``tailscaled`` that runs as root or as this gateway's user.

    Any user can name a process ``tailscaled``, so another user's uid is never
    trusted: only root's, which no other user can take.
    """
    uids: set[int] = set()
    trusted = {0, _effective_uid()}
    try:
        entries = list(_PROC.iterdir())
    except OSError:
        return frozenset()
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_bytes().decode("utf-8", "surrogateescape")
            if comm.strip() != _DAEMON_COMM:
                continue
            for line in (entry / "status").read_text(errors="replace").splitlines():
                if line.startswith("Uid:"):
                    # Real, effective, saved, filesystem: the socket is owned
                    # by the effective uid at creation.
                    uid = int(line.split()[2])
                    if uid in trusted:
                        uids.add(uid)
                    break
        except (OSError, ValueError, IndexError):
            continue
    return frozenset(uids)


def _daemon_uids() -> frozenset[int]:
    """Cached :func:`_scan_daemon_uids`."""
    global _daemon_uid_cache
    with _daemon_uid_lock:
        now = time.monotonic()
        if _daemon_uid_cache is not None and _daemon_uid_cache[0] > now:
            return _daemon_uid_cache[1]
        uids = _scan_daemon_uids()
        _daemon_uid_cache = (now + _DAEMON_UID_TTL_SECS, uids)
        return uids


def loopback_client_is_daemon(peer: object, local: object) -> bool:
    """Whether the kernel proves the loopback client ``peer`` is ``tailscaled``.

    *peer* and *local* are the transport's ``peername`` and ``sockname``. Blocking:
    run it off the event loop.
    """
    if not sys.platform.startswith("linux"):
        return False
    if not (isinstance(peer, tuple) and isinstance(local, tuple)):
        return False
    if len(peer) < 2 or len(local) < 2:
        return False
    try:
        peer_addr = (str(peer[0]), int(peer[1]))
        local_addr = (str(local[0]), int(local[1]))
    except (TypeError, ValueError):
        return False
    try:
        owner = _socket_owner_uid(peer_addr, local_addr)
        if owner is None or owner == _effective_uid():
            return False
        return owner in _daemon_uids()
    except Exception:  # noqa: BLE001 - the module contract: nothing here raises
        return False


def proof_support() -> str:
    """``""`` when the proof can succeed on this host, else why it cannot.

    ``platform_unprovable`` off Linux: no source names a loopback socket's owner
    to an unprivileged process there. ``daemon_same_user`` when every running
    ``tailscaled`` has the gateway's effective uid. ``daemon_not_found`` when
    none is running, which may change once the daemon starts. Blocking: it
    scans ``/proc``.
    """
    if not sys.platform.startswith("linux"):
        return PROOF_PLATFORM_UNPROVABLE
    try:
        uids = _scan_daemon_uids()
        euid = _effective_uid()
    except Exception:  # noqa: BLE001 - the module contract: nothing here raises
        return PROOF_DAEMON_NOT_FOUND
    if euid is None:
        return PROOF_PLATFORM_UNPROVABLE
    if not uids:
        return PROOF_DAEMON_NOT_FOUND
    return "" if uids - {euid} else PROOF_DAEMON_SAME_USER
