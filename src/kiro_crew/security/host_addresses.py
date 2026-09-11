"""Platform readers for this machine's own interface addresses.

Each helper reads one platform's local address table -- the Windows adapter
table, the Linux netlink RTM_GETADDR dump, the macOS ``getifaddrs`` list --
and returns the addresses as strings, best-effort: a platform mismatch or any
failure contributes an empty set and raises nothing.  ``argv_floor`` composes
them into the own-host name set that backs ``_host_is_self``; they live in
their own module so that ctypes/struct wire-format plumbing does not count
against the floor module's size, whose per-module liveness cap exists to stop
any one file growing back into a monolith.

Blocking discipline is per helper, not per module: the Windows and macOS
sweeps are pure local-table reads (no resolver, no packet) and are safe on
the event-loop synchronous seed, while the Linux netlink dump performs a
blocking socket ``recv`` and must run only in the DNS enrichment worker.
Each helper's docstring states its own contract.

Layer.  This module imports nothing from the package, so the dependency runs
one way: ``argv_floor`` imports these names and re-binds them in its own
namespace, which is also the seam tests monkeypatch
(``argv_floor._windows_interface_addresses`` and siblings).
"""

from __future__ import annotations

import ctypes
import socket
import struct
import sys


def _windows_interface_addresses() -> "set[str]":
    """Unicast addresses of every local adapter on Windows, best-effort.

    Reads the adapter table via ``GetAdaptersAddresses`` (iphlpapi) with the
    DNS-server/anycast/multicast lists skipped: a pure local-table read, no
    resolver and no packet, so it is safe inside the synchronous seed.  Off
    Windows (or on any failure) it contributes an empty set.
    """
    if sys.platform != "win32":
        return set()
    addrs: set[str] = set()
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined]
        # AF_UNSPEC=0 -> both families; skip flags 0x2|0x4|0x8|0x10 drop the
        # anycast/multicast/dns-server/friendly-name lists we never read.
        flags = 0x2 | 0x4 | 0x8 | 0x10
        size = ctypes.c_ulong(16 * 1024)
        for _attempt in range(3):
            buf = ctypes.create_string_buffer(size.value)
            ret = iphlpapi.GetAdaptersAddresses(0, flags, None, buf, ctypes.byref(size))
            if ret == 0:
                break
            if ret != 111:  # ERROR_BUFFER_OVERFLOW: retry with the size it wants
                return set()
        else:
            return set()

        class _SockAddr(ctypes.Structure):
            # c_ubyte, not c_char: a c_char array field NUL-truncates on
            # read, and sin_port's leading zero bytes would empty the blob.
            _fields_ = [("family", ctypes.c_ushort), ("data", ctypes.c_ubyte * 26)]

        # Offsets in IP_ADAPTER_ADDRESSES_LH / IP_ADAPTER_UNICAST_ADDRESS_LH:
        # both start with an 8-byte alignment union followed by ``Next``, so
        # ``Next`` sits at 8 on both pointer widths; ``FirstUnicastAddress``
        # follows ``Next`` and ``AdapterName`` (24 on 64-bit, 16 on 32-bit);
        # a unicast entry's ``lpSockaddr`` follows its ``Next`` (16 / 12).
        ptr_size = ctypes.sizeof(ctypes.c_void_p)
        first_unicast_off = 24 if ptr_size == 8 else 16
        sockaddr_off = 16 if ptr_size == 8 else 12
        adapter = ctypes.cast(buf, ctypes.c_void_p).value
        while adapter:
            unicast = ctypes.c_void_p.from_address(adapter + first_unicast_off).value
            while unicast:
                sa_ptr = ctypes.c_void_p.from_address(unicast + sockaddr_off).value
                if sa_ptr:
                    sa = _SockAddr.from_address(sa_ptr)
                    raw = bytes(sa.data)
                    try:
                        if sa.family == socket.AF_INET:
                            addrs.add(socket.inet_ntop(socket.AF_INET, raw[2:6]))
                        elif sa.family == socket.AF_INET6:
                            addrs.add(socket.inet_ntop(socket.AF_INET6, raw[6:22]))
                    except Exception:
                        pass
                unicast = ctypes.c_void_p.from_address(unicast + 8).value
            adapter = ctypes.c_void_p.from_address(adapter + 8).value
    except Exception:
        return addrs
    return addrs


def _parse_netlink_addr_dump(data: bytes) -> "set[str]":
    """Addresses carried in one RTM_GETADDR netlink reply buffer.

    Pure parser so the wire format is unit-testable without a netlink
    socket: walks nlmsghdr records (16 bytes, ``=LHHLL``), reads the
    ifaddrmsg family byte, then the rtattr list -- IFA_ADDRESS (1) and
    IFA_LOCAL (2) payloads become dotted/compressed address strings.
    Both walks are 4-byte aligned per the netlink ABI; anything malformed
    contributes nothing.
    """
    addrs: set[str] = set()
    off = 0
    while off + 16 <= len(data):
        try:
            msg_len, msg_type = struct.unpack_from("=LH", data, off)
        except Exception:
            break
        if msg_len < 16 or off + msg_len > len(data):
            break
        if msg_type == 20:  # RTM_NEWADDR
            family = data[off + 16]
            attr_off = off + 24  # nlmsghdr(16) + ifaddrmsg(8)
            end = off + msg_len
            while attr_off + 4 <= end:
                try:
                    a_len, a_type = struct.unpack_from("=HH", data, attr_off)
                except Exception:
                    break
                if a_len < 4 or attr_off + a_len > end:
                    break
                if a_type in (1, 2):  # IFA_ADDRESS, IFA_LOCAL
                    payload = data[attr_off + 4 : attr_off + a_len]
                    try:
                        if family == socket.AF_INET and len(payload) == 4:
                            addrs.add(socket.inet_ntop(socket.AF_INET, payload))
                        elif family == socket.AF_INET6 and len(payload) == 16:
                            addrs.add(socket.inet_ntop(socket.AF_INET6, payload))
                    except Exception:
                        pass
                attr_off += (a_len + 3) & ~3
        off += (msg_len + 3) & ~3
    return addrs


def _linux_netlink_addresses() -> "set[str]":
    """Every address assigned on Linux, secondaries included, best-effort.

    RTM_GETADDR dump over an AF_NETLINK route socket: a kernel-local table
    read -- no resolver, no packet leaves the host -- but the ``recv`` is a
    blocking socket read, so this dump must run only in the DNS enrichment
    worker, never the synchronous seed (``_NETLINK_ADDRS_PUBLISHED`` keeps
    ``_host_is_self`` fail-closed until the worker publishes).  Off Linux
    (or on any failure) it contributes an empty set.
    """
    if not sys.platform.startswith("linux") or not hasattr(socket, "AF_NETLINK"):
        return set()
    addrs: set[str] = set()
    try:
        with socket.socket(socket.AF_NETLINK, socket.SOCK_RAW, 0) as _sock:  # NETLINK_ROUTE
            _sock.bind((0, 0))
            _sock.settimeout(1.0)
            # nlmsghdr(type=RTM_GETADDR(22), flags=NLM_F_REQUEST|NLM_F_DUMP)
            # + ifaddrmsg(family=AF_UNSPEC): dump every family's addresses.
            _sock.send(struct.pack("=LHHLL", 24, 22, 0x301, 1, 0) + bytes(8))
            for _ in range(64):  # dump replies span multiple datagrams
                data = _sock.recv(65536)
                addrs |= _parse_netlink_addr_dump(data)
                if any(
                    struct.unpack_from("=H", data, off + 4)[0] in (2, 3)  # ERROR, DONE
                    for off in _nlmsg_offsets(data)
                ):
                    break
    except Exception:
        pass
    return addrs


def _nlmsg_offsets(data: bytes) -> "list[int]":
    """Byte offsets of each nlmsghdr in a reply buffer (bounded walk)."""
    offs: "list[int]" = []
    off = 0
    while off + 16 <= len(data):
        try:
            msg_len = struct.unpack_from("=L", data, off)[0]
        except Exception:
            break
        if msg_len < 16 or off + msg_len > len(data):
            break
        offs.append(off)
        off += (msg_len + 3) & ~3
    return offs


def _darwin_interface_addresses() -> "set[str]":
    """Addresses of every local interface on macOS, best-effort.

    Reads the interface table via libc ``getifaddrs``: a pure local-table
    read, no resolver and no packet, so it is safe inside the synchronous
    seed.  Off macOS (or on any failure) it contributes an empty set.
    """
    if sys.platform != "darwin":
        return set()
    addrs: set[str] = set()
    libc = None
    head = None
    try:
        libc = ctypes.CDLL(None, use_errno=True)

        class _Ifaddrs(ctypes.Structure):
            pass

        # struct ifaddrs (BSD): ifa_next, ifa_name, ifa_flags, ifa_addr,
        # ifa_netmask, ifa_dstaddr, ifa_data.  Only next/addr are read; the
        # rest keep the layout honest.
        _Ifaddrs._fields_ = [
            ("ifa_next", ctypes.POINTER(_Ifaddrs)),
            ("ifa_name", ctypes.c_char_p),
            ("ifa_flags", ctypes.c_uint),
            ("ifa_addr", ctypes.c_void_p),
            ("ifa_netmask", ctypes.c_void_p),
            ("ifa_dstaddr", ctypes.c_void_p),
            ("ifa_data", ctypes.c_void_p),
        ]

        head = ctypes.POINTER(_Ifaddrs)()
        if libc.getifaddrs(ctypes.byref(head)) != 0:
            return set()
        node = head
        while node:
            ifa = node.contents
            sa_ptr = ifa.ifa_addr
            if sa_ptr:
                # BSD sockaddr: sa_len at byte 0 bounds the read, sa_family at
                # byte 1; sockaddr_in carries the IPv4 address at 4..8,
                # sockaddr_in6 the IPv6 address at 8..24.
                sa_head = (ctypes.c_uint8 * 2).from_address(sa_ptr)
                sa_len, family = sa_head[0], sa_head[1]
                raw = bytes((ctypes.c_uint8 * sa_len).from_address(sa_ptr))
                try:
                    if family == socket.AF_INET and len(raw) >= 8:
                        addrs.add(socket.inet_ntop(socket.AF_INET, raw[4:8]))
                    elif family == socket.AF_INET6 and len(raw) >= 24:
                        addrs.add(socket.inet_ntop(socket.AF_INET6, raw[8:24]))
                except Exception:
                    pass
            node = ifa.ifa_next
    except Exception:
        return addrs
    finally:
        if libc is not None and head:
            try:
                libc.freeifaddrs(head)
            except Exception:
                pass
    return addrs
