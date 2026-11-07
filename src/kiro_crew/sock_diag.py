"""Linux ``NETLINK_SOCK_DIAG``: the kernel's TCP LISTEN sockets, without ``/proc/net/tcp``.

Reading ``/proc/net/tcp`` walks every socket hash bucket, so it costs about 5 ms
on a busy host however few sockets it prints. A ``SOCK_DIAG_BY_FAMILY`` dump
with a LISTEN-only state mask returns the same rows in a fraction of a
millisecond. The answer covers this process's network namespace, which is the
namespace ``/proc/net`` describes for it too.
"""

from __future__ import annotations

import ipaddress
import socket
import struct

_NETLINK_SOCK_DIAG = 4
_SOCK_DIAG_BY_FAMILY = 20
_NLM_F_REQUEST = 0x01
_NLM_F_DUMP = 0x300
_NLMSG_ERROR = 2
_NLMSG_DONE = 3
_TCP_LISTEN = 10
_NLMSG_HEADER = struct.Struct("=IHHII")
#: nlmsghdr, then inet_diag_req_v2: family, protocol, ext, pad, states, and a
#: zeroed 48-byte inet_diag_sockid (no address or port filter).
_DIAG_REQ = struct.Struct("=IHHII BBBxI 48x")
#: inet_diag_msg: family, state, timer, retrans, then inet_diag_sockid (sport and
#: dport big-endian, src[16], dst[16], ifindex, cookie[2]), then expires,
#: rqueue, wqueue, uid, inode.
_DIAG_MSG = struct.Struct("=BBBB 2s2s 16s16s I8x IIIII")


def _address_text(family: int, raw: bytes) -> str:
    """The address as text, an IPv4-mapped one always as ``::ffff:a.b.c.d``.

    ``str(IPv6Address)`` prints a mapped address in hex on Python before 3.13,
    so the mapped form is spelled out here rather than left to the version.
    """
    if family == socket.AF_INET:
        return str(ipaddress.IPv4Address(raw[:4]))
    address = ipaddress.IPv6Address(raw)
    if address.ipv4_mapped is not None:
        return f"::ffff:{address.ipv4_mapped}"
    return str(address)


def listen_sockets(port: int) -> list[tuple[str, int]] | None:
    """``(local address, inode)`` for every TCP LISTEN socket on *port*.

    IPv6 rows report their address as given (``::``, ``::ffff:127.0.0.1``).
    ``None`` when the kernel query cannot be made or its reply does not parse:
    the caller reads ``/proc/net/tcp`` instead, so this never decides alone.
    """
    if not 0 < port <= 65535:
        return None
    try:
        sock = socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, _NETLINK_SOCK_DIAG)
    except (AttributeError, OSError):
        return None
    rows: list[tuple[str, int]] = []
    try:
        sock.settimeout(1.0)
        for seq, family in enumerate((socket.AF_INET, socket.AF_INET6), start=1):
            sock.sendall(
                _DIAG_REQ.pack(
                    _DIAG_REQ.size,
                    _SOCK_DIAG_BY_FAMILY,
                    _NLM_F_REQUEST | _NLM_F_DUMP,
                    seq,
                    0,
                    family,
                    socket.IPPROTO_TCP,
                    0,
                    1 << _TCP_LISTEN,
                )
            )
            done = False
            while not done:
                data = sock.recv(1 << 16)
                if not data:
                    return None
                offset = 0
                while offset + _NLMSG_HEADER.size <= len(data):
                    length, kind, _flags, got_seq, _pid = _NLMSG_HEADER.unpack_from(data, offset)
                    if length < _NLMSG_HEADER.size or offset + length > len(data):
                        return None
                    if got_seq == seq:
                        if kind == _NLMSG_DONE:
                            done = True
                            break
                        if kind == _NLMSG_ERROR:
                            return None
                        body = offset + _NLMSG_HEADER.size
                        if (
                            kind == _SOCK_DIAG_BY_FAMILY
                            and length >= _NLMSG_HEADER.size + _DIAG_MSG.size
                        ):
                            fam, state, _t, _r, sport, _dport, src, _dst, _if, *_rest, inode = (
                                _DIAG_MSG.unpack_from(data, body)
                            )
                            if (
                                state == _TCP_LISTEN
                                and int.from_bytes(sport, "big") == port
                                and inode
                            ):
                                rows.append((_address_text(fam, src), inode))
                    offset += (length + 3) & ~3
    except (OSError, struct.error, ValueError):
        return None
    finally:
        sock.close()
    return rows
