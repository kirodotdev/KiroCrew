"""Which process is behind a TCP endpoint.

The peer of a loopback connection, from the kernel's exact 4-tuple, and the
listeners on a port with the addresses each can answer.

One of the owners ``kiro_crew.platform_compat`` is split into: every name here stays
readable and patchable as ``kiro_crew.platform_compat.<name>``.
"""

from __future__ import annotations

import ctypes.util
import ipaddress
import logging
import os
import struct
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path
from typing import NamedTuple

from kiro_crew.platform_compat_native import _SUBPROCESS_NO_WINDOW, IS_LINUX, IS_POSIX, IS_WINDOWS
from kiro_crew.platform_compat_trust import trusted_system_bin

# The whole compatibility layer logs under one name, which operator log filters and
# the tests' caplog scopes key on.
logger = logging.getLogger("kiro_crew.platform_compat")


def _normalized_tcp_endpoint(endpoint: tuple[str, int]) -> tuple[str, int]:
    address = ipaddress.ip_address(endpoint[0])
    if isinstance(address, ipaddress.IPv6Address):
        address = address.ipv4_mapped or address
    if not address.is_loopback or not 0 < endpoint[1] <= 65535:
        raise ValueError("A concrete loopback endpoint is required")
    return str(address), endpoint[1]


def _linux_tcp_peer_pid(server: tuple[str, int], client: tuple[str, int]) -> int | None:
    """Map the reverse kernel connection inode to its unique process owner."""

    def endpoint(raw: str) -> tuple[str, int]:
        address, port = raw.split(":")
        packed = bytes.fromhex(address)
        # /proc uses native-endian 32-bit address words, including IPv6.
        packed = b"".join(
            int.from_bytes(packed[i : i + 4], sys.byteorder).to_bytes(4, "big")
            for i in range(0, len(packed), 4)
        )
        return _normalized_tcp_endpoint((str(ipaddress.ip_address(packed)), int(port, 16)))

    inodes: set[str] = set()
    for table in ("tcp", "tcp6"):
        try:
            lines = Path(f"/proc/net/{table}").read_text(encoding="ascii").splitlines()[1:]
        except FileNotFoundError:
            continue  # IPv6 can be disabled on the host.
        for line in lines:
            fields = line.split()
            if len(fields) < 10 or fields[3] != "01":
                continue
            try:
                if endpoint(fields[1]) == client and endpoint(fields[2]) == server:
                    if fields[9].isdigit() and int(fields[9]) > 0:
                        inodes.add(fields[9])
            except ValueError:
                continue  # Non-loopback rows cannot identify this caller.
    if len(inodes) != 1:
        return None
    target = f"socket:[{next(iter(inodes))}]"
    owners: set[int] = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for descriptor in (process / "fd").iterdir():
                try:
                    if os.readlink(descriptor) == target:
                        owners.add(int(process.name))
                        break
                except OSError:
                    continue  # An unrelated descriptor closed during the scan.
        except (OSError, ValueError):
            continue
    # A socket shared across processes, or hidden by /proc access controls,
    # cannot positively identify a caller. In-sandbox clients should use UDS.
    return next(iter(owners)) if len(owners) == 1 else None


def _macos_tcp_peer_pid(server: tuple[str, int], client: tuple[str, int]) -> int | None:
    """Read system lsof's machine fields, never a human-formatted port listing."""
    binary = trusted_system_bin("lsof")
    if binary is None:
        return None
    output = subprocess.check_output(
        [binary, "-nP", "-a", f"-iTCP:{client[1]}", "-sTCP:ESTABLISHED", "-Fpn"],
        stderr=subprocess.DEVNULL,
        timeout=2,
    ).decode("ascii")
    owners: set[int] = set()
    pid = 0
    for line in output.splitlines():
        if line.startswith("p"):
            pid = int(line[1:]) if line[1:].isdigit() else 0
        elif pid > 0 and line.startswith("n") and "->" in line:
            local, remote = line[1:].split("->", 1)
            try:
                local_host, local_port = local.rsplit(":", 1)
                remote_host, remote_port = remote.rsplit(":", 1)
                if (
                    _normalized_tcp_endpoint((local_host.strip("[]"), int(local_port))) == client
                    and _normalized_tcp_endpoint((remote_host.strip("[]"), int(remote_port)))
                    == server
                ):
                    owners.add(pid)
            except ValueError:
                continue
    return next(iter(owners)) if len(owners) == 1 else None


class _WindowsTcpOwnerRow(NamedTuple):
    """One row from a Windows TCP owner-PID table."""

    state: int
    local_address: bytes
    local_scope_id: int
    local_port: int
    remote_address: bytes
    remote_scope_id: int
    remote_port: int
    pid: int


_WINDOWS_TCP_TABLE_OWNER_PID_LISTENER = 3
_WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS = 4
_WINDOWS_TCP_STATE_LISTEN = 2
_WINDOWS_TCP_STATE_ESTABLISHED = 5
_WINDOWS_TCP_TABLE_MAX_BYTES = 8 * 1024 * 1024
_WINDOWS_ERROR_INSUFFICIENT_BUFFER = 122


def _windows_tcp_owner_rows(
    ipv6: bool,
    table_class: int,
) -> list[_WindowsTcpOwnerRow] | None:
    """Read one Windows TCP owner-PID table, or ``None`` on uncertainty."""
    if not IS_WINDOWS or table_class not in {
        _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
    }:
        return None
    row_format = struct.Struct("<16sII16sIIII") if ipv6 else struct.Struct("<I4sI4sII")
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi", use_last_error=True)  # type: ignore[attr-defined]
        query = iphlpapi.GetExtendedTcpTable
        query.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.BOOL,
            wintypes.ULONG,
            ctypes.c_int,
            wintypes.ULONG,
        ]
        query.restype = wintypes.DWORD
        size = wintypes.DWORD()
        family = 23 if ipv6 else 2  # Windows AF_INET6 / AF_INET.
        if (
            query(None, ctypes.byref(size), False, family, table_class, 0)
            != _WINDOWS_ERROR_INSUFFICIENT_BUFFER
        ):
            return None
        for _ in range(3):
            if not 4 <= size.value <= _WINDOWS_TCP_TABLE_MAX_BYTES:
                return None
            buffer = ctypes.create_string_buffer(size.value)
            result = query(
                buffer,
                ctypes.byref(size),
                False,
                family,
                table_class,
                0,
            )
            if result == _WINDOWS_ERROR_INSUFFICIENT_BUFFER:
                continue
            if result != 0 or not 4 <= size.value <= len(buffer):
                return None
            raw = buffer.raw[: size.value]
            count = struct.unpack_from("<I", raw)[0]
            if 4 + count * row_format.size > len(raw):
                return None
            rows: list[_WindowsTcpOwnerRow] = []
            for index in range(count):
                fields = row_format.unpack_from(raw, 4 + index * row_format.size)
                if ipv6:
                    (
                        local_address,
                        local_scope_id,
                        local_port_raw,
                        remote_address,
                        remote_scope_id,
                        remote_port_raw,
                        state,
                        pid,
                    ) = fields
                else:
                    (
                        state,
                        local_address,
                        local_port_raw,
                        remote_address,
                        remote_port_raw,
                        pid,
                    ) = fields
                    local_scope_id = remote_scope_id = 0
                # Ports occupy the first two bytes of a DWORD in network order.
                local_port = int.from_bytes(struct.pack("<I", local_port_raw)[:2], "big")
                remote_port = int.from_bytes(struct.pack("<I", remote_port_raw)[:2], "big")
                rows.append(
                    _WindowsTcpOwnerRow(
                        int(state),
                        bytes(local_address),
                        int(local_scope_id),
                        local_port,
                        bytes(remote_address),
                        int(remote_scope_id),
                        remote_port,
                        int(pid),
                    )
                )
            return rows
    except (AttributeError, OSError, TypeError, ValueError, struct.error):
        logger.debug("Cannot read Windows TCP owner-PID table", exc_info=True)
    return None


def get_tcp_peer_pid(
    server_endpoint: tuple[str, int], client_endpoint: tuple[str, int]
) -> int | None:
    """Resolve a loopback TCP caller from the kernel's exact 4-tuple.

    Pass the accepted socket's sockname then peername, never HTTP headers.
    Only a unique ESTABLISHED reverse connection is accepted. An unreadable,
    changing or ambiguous table returns None; authorization must fail closed.
    Linux uses /proc socket inodes, macOS system lsof, Windows the owner-PID
    table. Call from a worker thread; Unix socket peer credentials are preferred.
    """
    if not IS_WINDOWS:
        try:
            server_tuple = _normalized_tcp_endpoint(server_endpoint)
            client_tuple = _normalized_tcp_endpoint(client_endpoint)
            if sys.platform == "linux":
                return _linux_tcp_peer_pid(server_tuple, client_tuple)
            if sys.platform == "darwin":
                return _macos_tcp_peer_pid(server_tuple, client_tuple)
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            logger.debug("Cannot verify loopback TCP peer PID", exc_info=True)
        return None
    try:
        server = ipaddress.ip_address(server_endpoint[0])
        client = ipaddress.ip_address(client_endpoint[0])
        addresses = [server, client]
        normalized = [
            (
                address.ipv4_mapped or address
                if isinstance(address, ipaddress.IPv6Address)
                else address
            )
            for address in addresses
        ]
        if not all(address.is_loopback for address in normalized):
            return None
        if not all(0 < endpoint[1] <= 65535 for endpoint in (server_endpoint, client_endpoint)):
            return None
        ipv6 = any(address.version == 6 for address in addresses)
        if ipv6:
            packed = [
                (
                    address.packed
                    if address.version == 6
                    else ipaddress.IPv6Address(f"::ffff:{address}").packed
                )
                for address in addresses
            ]
        else:
            packed = [address.packed for address in addresses]
        rows = _windows_tcp_owner_rows(
            ipv6,
            _WINDOWS_TCP_TABLE_OWNER_PID_CONNECTIONS,
        )
        if rows is None:
            return None
        matches = [
            row.pid
            for row in rows
            if row.state == _WINDOWS_TCP_STATE_ESTABLISHED
            and row.pid > 0
            and row.local_scope_id == 0
            and row.remote_scope_id == 0
            and row.local_address == packed[1]
            and row.local_port == client_endpoint[1]
            and row.remote_address == packed[0]
            and row.remote_port == server_endpoint[1]
        ]
        return matches[0] if len(matches) == 1 else None
    except (OSError, TypeError, ValueError):
        logger.debug("Cannot verify loopback TCP peer PID", exc_info=True)
    return None


def listening_pid_tool() -> str:
    """Return the external tool find_listening_pids relies on: 'lsof' / 'netstat'."""
    return "netstat" if IS_WINDOWS else "lsof"


def listening_pid_tool_available() -> bool:
    """Whether the port->PID lookup tool (lsof on POSIX / netstat on Windows) is
    resolvable. Lets callers distinguish "tool absent" from "no listener found",
    which find_listening_pids() alone collapses into an empty list — without that
    a genuinely-running gateway reads as stopped when lsof is missing.

    Resolves through :func:`trusted_system_bin`, the same lookup
    :func:`find_listening_pids` performs. Probing ``PATH`` here instead would
    let the two disagree: a shim on ``PATH`` would answer "available" for a tool
    the pinned lookup refuses to run, turning a live gateway into one that reads
    as stopped — the exact failure this probe exists to prevent. A tool that is
    installed but outside those directories therefore reads as absent, which
    :func:`trusted_system_bin` logs so the answer can be explained.
    """
    return trusted_system_bin(listening_pid_tool()) is not None


class PortListener(NamedTuple):
    """One LISTEN socket on a TCP port: the owning PID plus the local address
    it bound, so callers can scope port ownership to the address they actually
    probed instead of claiming every listener on the port."""

    pid: int
    #: Normalized local host part, brackets stripped: ``"127.0.0.1"``,
    #: ``"0.0.0.0"``, ``"::"``, ``"::1"``, a specific interface address, or
    #: ``"*"`` (lsof prints the wildcard bind of either family as ``*``).
    address: str
    #: Address family: ``"4"``, ``"6"``, or ``""`` when the source did not say.
    #: Load-bearing for wildcards — lsof spells both families ``*``, and only
    #: the family tells a v4 wildcard apart from a possibly-v6-only one.
    family: str = ""


# Local addresses whose listener can receive a connect to ``127.0.0.1``: the v4
# loopback itself, the v4 wildcard, lsof's family-agnostic wildcard ``*``, and
# the v6 wildcard ``::`` (dual-stack sockets accept v4-mapped loopback; treating
# it as non-covering would refuse adoption of a legitimately ``[::]``-bound
# backend, which is the breaking direction). ``::1`` is deliberately absent: a
# v6-loopback-only listener can never answer a probe addressed to 127.0.0.1.
_LOOPBACK_COVERING_ADDRESSES = frozenset({"127.0.0.1", "0.0.0.0", "*", "::"})

# Bound so a wedged lsof (stale mount, jammed process table) degrades to "no
# listener found" instead of hanging every caller of the port->PID lookup; the
# Windows netstat branch carries its own inline bound.
_LSOF_TIMEOUT_SECS = 5


def _normalize_local_address(address: str) -> str:
    """Bare lowercase host part: brackets stripped, v4-mapped prefix removed."""
    addr = address.strip().strip("[]").lower()
    if addr.startswith("::ffff:"):
        # v4-mapped form of a v4 address; compare the embedded v4 part.
        addr = addr[len("::ffff:") :]
    return addr


def address_covers_loopback(address: str) -> bool:
    """Whether a listener bound to *address* can receive a ``127.0.0.1`` connect.

    It scopes port ownership to the address a health probe actually talked
    to: a listener on some other specific local address shares the port number
    but was never the thing that answered the probe.
    """
    return _normalize_local_address(address) in _LOOPBACK_COVERING_ADDRESSES


def loopback_owner_pids(listeners: list[PortListener]) -> list[int]:
    """PIDs of the listener(s) a successful ``127.0.0.1:<port>`` connect reached.

    Mirrors the kernel's most-specific-bind dispatch — the first non-empty
    tier wins, and every PID within it is returned (pre-fork / multi-worker
    backends legitimately share one listening socket):

    1. Exact v4-loopback binds (``127.0.0.1``, incl. the v4-mapped spelling):
       when one exists the kernel routes a loopback connect to it, so wildcard
       listeners on the same port never saw the probe.
    2. IPv4 wildcard binds: a v4 connect reaches the v4 wildcard socket in
       preference to a dual-stack v6 one.
    3. Remaining loopback-covering binds (the v6 wildcard, or one whose family
       the source did not report): callers only ask after a successful
       127.0.0.1 probe, so when nothing more specific exists, what is left
       must have been the responder (a dual-stack socket).

    Tier 2 is what keeps an unrelated ``IPV6_V6ONLY`` wildcard process from
    being claimed alongside the real v4 owner sharing its port.
    """
    exact = [e for e in listeners if _normalize_local_address(e.address) == "127.0.0.1"]
    if exact:
        return list(dict.fromkeys(e.pid for e in exact))
    covering = [e for e in listeners if address_covers_loopback(e.address)]
    v4 = [e for e in covering if e.family == "4"]
    if v4:
        return list(dict.fromkeys(e.pid for e in v4))
    return list(dict.fromkeys(e.pid for e in covering))


def probe_port_listeners(
    port: int, *, process_pid: int | None = None
) -> tuple[list[PortListener], bool]:
    """Return LISTEN sockets on *port* and whether the lookup completed.

    POSIX asks ``lsof -nP -iTCP:<port> -sTCP:LISTEN -Fptn``. When
    *process_pid* is supplied it adds ``-a -p <pid>`` so lsof inspects only that
    process. Exit 1 with no stdout or stderr is lsof's ordinary completed "no
    matches" answer. A diagnostic, timeout, execution error, unavailable binary,
    or any other nonzero exit is not a completed lookup. Windows parses
    ``netstat -ano`` and requires a zero exit; it has no PID-scoped command mode.

    The status bit is for security-sensitive callers that must distinguish a
    completed empty answer from an operational failure. Ordinary callers should
    use :func:`find_port_listeners`, which preserves the existing best-effort
    ``[]``-on-any-failure contract.
    """
    if IS_POSIX:
        lsof_bin = trusted_system_bin("lsof")
        if lsof_bin is None:
            return [], False
        try:
            argv = [lsof_bin, "-nP"]
            if process_pid is not None:
                argv.extend(["-a", "-p", str(process_pid)])
            argv.extend([f"-iTCP:{port}", "-sTCP:LISTEN", "-Fptn"])
            out = subprocess.check_output(
                # -n/-P keep addresses and ports numeric so the field parse
                # below never sees a resolved host or service name; the t
                # (type) field carries the family, without which the two
                # wildcard binds are indistinguishable (both print ``*``).
                argv,
                text=True,  # subprocess-encoding: locale -- lsof writes paths in the locale
                stderr=subprocess.PIPE,
                timeout=_LSOF_TIMEOUT_SECS,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == 1 and not exc.output and not exc.stderr:
                return [], True
            return [], False
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            return [], False
        suffix = f":{port}"
        listeners: list[PortListener] = []
        seen: set[PortListener] = set()
        cur_pid: int | None = None
        cur_family = ""
        for line in out.splitlines():
            if not line:
                continue
            tag, value = line[0], line[1:]
            if tag == "p":
                cur_pid = int(value) if value.isdigit() else None
                cur_family = ""
            elif tag == "t":
                cur_family = {"IPv4": "4", "IPv6": "6"}.get(value, "")
            elif tag == "n" and cur_pid is not None and value.endswith(suffix):
                # ``n127.0.0.1:8080`` / ``n*:8080`` / ``n[::1]:8080`` — strip
                # the port suffix and the v6 brackets to the bare host part.
                entry = PortListener(cur_pid, value[: -len(suffix)].strip("[]"), cur_family)
                if entry not in seen:
                    seen.add(entry)
                    listeners.append(entry)
        return listeners, True
    # Windows: netstat -ano. Lines look like:
    #   TCP    127.0.0.1:7777         0.0.0.0:0    LISTENING    17152   (IPv4)
    #   TCP    [::1]:7777             [::]:0       LISTENING    17152   (IPv6)
    # No `-p tcp`: that flag restricts output to IPv4 TCP on Windows and
    # silently drops IPv6 listeners entirely, so `kirocrew stop` /
    # `kirocrew restart` no-op on a dual-stack or `[::]`-bound gateway
    # Windows netstat labels IPv6 rows with proto column
    # "TCP" too — the bracketed address form is what distinguishes v4 vs
    # v6, not the proto token — so once `-p tcp` is dropped the existing
    # port suffix match already handles both families uniformly.
    netstat_bin = trusted_system_bin("netstat")
    if netstat_bin is None:
        return [], False
    try:
        out = subprocess.check_output(
            [netstat_bin, "-ano"],
            # encoding="oem" (Windows-only pseudo-codec): netstat emits the
            # console OEM codepage when piped; text=True would decode with the
            # ANSI codepage and can raise UnicodeDecodeError on non-Western
            # locales — which, as a ValueError, would escape a
            # (SubprocessError, OSError) net and crash stop/status instead of
            # degrading to []. errors="replace" belts the rest.
            encoding="oem",
            errors="replace",
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=_SUBPROCESS_NO_WINDOW,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError, ValueError):
        return [], False
    suffix = f":{port}"
    listeners = []
    seen = set()
    for line in out.splitlines():
        parts = line.split()
        # Expect: proto local foreign state pid.
        # startswith("TCP") not `== "TCP"` for defensive future-proofing:
        # today Windows netstat prints plain "TCP" for both families, but a
        # future Windows build could relabel IPv6 rows as "TCP6" (the netstat
        # -p flag already accepts "tcpv6"). UDP rows never carry a LISTEN
        # state, so the listener check below is the load-bearing filter for
        # the non-TCP case, but keeping the proto guard avoids feeding
        # malformed ICMPv6 / RAW lines into the port match.
        if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
            continue
        # Listener detection via the FOREIGN address, not the state column:
        # netstat localizes state names ("ABHÖREN" on German Windows,
        # Cyrillic on Russian), so matching the English "LISTENING" literal
        # returns [] on any non-English locale and stop/restart silently
        # no-op. A TCP row whose foreign endpoint is the wildcard 0.0.0.0:0 /
        # [::]:0 is in LISTEN state by definition, locale-independently.
        # Accept the English literal too as a defensive second signal.
        if parts[2] not in ("0.0.0.0:0", "[::]:0") and parts[3].upper() != "LISTENING":
            continue
        local = parts[1]
        # Suffix-match the local-address port. Formats: A.B.C.D:port for
        # TCP4 and [::]:port / [::1]:port / [fe80::...]:port for TCP6.
        # ']' is never a digit, so no false-positive against a bracketed
        # suffix (e.g. [::1234]:7 would not endswith ":234").
        if not local.endswith(suffix):
            continue
        pid_str = parts[-1]
        if pid_str.isdigit():
            # The bracketed address form is what distinguishes v4 vs v6 on
            # Windows netstat output (the proto column says "TCP" for both).
            entry = PortListener(
                int(pid_str),
                local[: -len(suffix)].strip("[]"),
                "6" if local.startswith("[") else "4",
            )
            if entry not in seen:
                seen.add(entry)
                listeners.append(entry)
    return listeners, True


def _linux_loopback_listener_inodes(port: int, proc_root: Path) -> set[str] | None:
    """LISTEN socket inodes on *port* that can answer IPv4 loopback.

    ``/proc/net/tcp`` stores IPv4 addresses little-endian. ``tcp6`` stores each
    32-bit word little-endian. The browser child is asked to bind 127.0.0.1, but
    wildcard and IPv4-mapped wildcard-compatible rows are included because a
    successful loopback health probe can reach them too.
    """
    v4_addresses = {"00000000", "0100007F"}
    v6_addresses = {
        "0" * 32,
        "0000000000000000FFFF00000100007F",
    }
    inodes: set[str] = set()
    completed = False
    for name, accepted_addresses in (("tcp", v4_addresses), ("tcp6", v6_addresses)):
        try:
            rows = (proc_root / "net" / name).read_text(encoding="ascii", errors="replace")
        except OSError:
            continue
        completed = True
        for row in rows.splitlines()[1:]:
            fields = row.split()
            if len(fields) <= 9 or fields[3].upper() != "0A":
                continue
            try:
                address, port_hex = fields[1].rsplit(":", 1)
                bound_port = int(port_hex, 16)
            except (ValueError, IndexError):
                continue
            if bound_port == port and address.upper() in accepted_addresses:
                inode = fields[9]
                if inode.isdigit() and int(inode) > 0:
                    inodes.add(inode)
    return inodes if completed else None


def _windows_loopback_listener_owner_pids(port: int) -> set[int] | None:
    """Owner PIDs for Windows listeners that can answer IPv4 loopback."""
    if not IS_WINDOWS or type(port) is not int or not 1 <= port <= 65535:
        return None
    listeners: list[PortListener] = []
    for ipv6 in (False, True):
        rows = _windows_tcp_owner_rows(
            ipv6,
            _WINDOWS_TCP_TABLE_OWNER_PID_LISTENER,
        )
        if rows is None:
            return None
        family = "6" if ipv6 else "4"
        for row in rows:
            if (
                row.state != _WINDOWS_TCP_STATE_LISTEN
                or row.pid <= 0
                or row.local_scope_id != 0
                or row.local_port != port
            ):
                continue
            try:
                address = str(ipaddress.ip_address(row.local_address))
            except ValueError:
                continue
            listeners.append(PortListener(row.pid, address, family))
    return set(loopback_owner_pids(listeners))


def process_owns_loopback_listener(
    pid: int, port: int, *, proc_root: Path | None = None
) -> bool | None:
    """Whether *pid* owns a LISTEN socket that can answer 127.0.0.1:*port*.

    ``True`` and ``False`` are completed per-process observations. ``None``
    means this host cannot make the observation, so callers may use a separate
    child-specific proof without mistaking probe failure for non-ownership.

    Linux compares socket inodes from ``/proc/net/tcp{,6}`` with symlink targets
    under ``/proc/<pid>/fd``. Other POSIX hosts run the existing listener parser
    with lsof scoped by ``-a -p <pid>``. Windows reads the IPv4 and IPv6 owner-PID
    listener tables in-process with ``GetExtendedTcpTable``; it never needs
    ``netstat`` for this per-process decision.
    """
    if type(pid) is not int or pid <= 0 or type(port) is not int or not 1 <= port <= 65535:
        return None
    if IS_WINDOWS:
        owners = _windows_loopback_listener_owner_pids(port)
        return None if owners is None else pid in owners
    if IS_LINUX:
        root = proc_root if proc_root is not None else Path("/proc")
        listener_inodes = _linux_loopback_listener_inodes(port, root)
        if listener_inodes is None:
            return None
        if not listener_inodes:
            return False
        try:
            descriptors = list((root / str(pid) / "fd").iterdir())
        except OSError:
            return None
        readable = 0
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            readable += 1
            if target.startswith("socket:[") and target.endswith("]"):
                if target[8:-1] in listener_inodes:
                    return True
        if descriptors and readable == 0:
            return None
        return False

    if not IS_POSIX:
        return None
    listeners, completed = probe_port_listeners(port, process_pid=pid)
    if not completed:
        return None
    return pid in loopback_owner_pids(listeners)


def find_port_listeners(port: int) -> list[PortListener]:
    """Return LISTEN sockets on *port*, or ``[]`` on any lookup failure.

    Best-effort, deduped on ``(pid, address, family)``, and never raises. Use
    :func:`probe_port_listeners` when a caller must distinguish a completed
    empty lookup from an operational failure.
    """
    listeners, _completed = probe_port_listeners(port)
    return listeners


def find_listening_pids(port: int) -> list[int]:
    """Return PIDs with a LISTEN socket on TCP *port* (best-effort, deduped).

    Address-agnostic accessor over :func:`find_port_listeners` for callers that
    only care whether/which processes hold the port. A dual-stack listener
    appears once per bound address (``0.0.0.0`` and ``::``) under the same PID —
    collapsed to one entry here, first-seen order preserved. Same failure
    contract: ``[]`` on any failure, never raises.
    """
    return list(dict.fromkeys(entry.pid for entry in find_port_listeners(port)))
