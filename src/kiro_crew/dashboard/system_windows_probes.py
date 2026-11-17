"""Windows sources for the System page's process count and network counters.

Windows has neither ``ps`` nor ``/proc``, so the two probes in
``handlers_system`` read these instead, both without spawning a process:

- process command lines from a Toolhelp snapshot of the pids plus
  ``ntdll!NtQueryInformationProcess(ProcessCommandLineInformation)`` per pid,
  which reads a busy host's table in well under a second;
- interface byte counters from ``iphlpapi!GetIfTable2``, which reports 64-bit
  ``InOctets`` / ``OutOctets`` per interface, cheap enough for the page's live
  network graph.

Each reader answers ``None`` when its source cannot be read, so a caller can tell
"could not measure" apart from a real zero.
"""

from __future__ import annotations

import ctypes
import logging
from typing import Callable, Sequence

from kiro_crew import platform_compat

logger = logging.getLogger(__name__)


def windows_process_command_lines(signatures: Sequence[str]) -> list[tuple[int, str]] | None:
    """Return ``(pid, command line)`` for every process whose command line holds a signature.

    Reads the process table in this process: one Toolhelp snapshot for the pids, then
    each process's command line through ``NtQueryInformationProcess``, matched
    case-insensitively. A process this user cannot open, or one that exits during the
    walk, is skipped. Answers ``None`` when the process table itself cannot be read.
    """
    if not signatures:
        return []
    try:
        reader = _command_line_reader()
        pids = sorted(platform_compat._windows_process_parent_map()) if reader else []
    except OSError as exc:
        logger.debug("Windows process table unreadable: %s", exc)
        return None
    if reader is None:
        return None
    wanted = [sig.lower() for sig in signatures]
    rows: list[tuple[int, str]] = []
    for pid in pids:
        cmd = reader(pid)
        if cmd is not None and any(sig in cmd.lower() for sig in wanted):
            rows.append((pid, cmd))
    return rows


_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
#: ``PROCESSINFOCLASS.ProcessCommandLineInformation`` (Windows 8.1 and later).
_PROCESS_COMMAND_LINE_INFORMATION = 60
#: ``STATUS_INFO_LENGTH_MISMATCH``, ``STATUS_BUFFER_TOO_SMALL`` and
#: ``STATUS_BUFFER_OVERFLOW``: the answer needs the larger buffer it names.
_STATUS_TOO_SMALL = frozenset({0xC0000004, 0xC0000023, 0x80000005})
#: The first buffer tried; a longer command line is read again at the length asked for.
_COMMAND_LINE_FIRST_BUFFER = 4096
#: Windows caps a command line at 32,767 UTF-16 units, so no answer is longer than this.
_COMMAND_LINE_MAX_BUFFER = 65536 + 64


class _UnicodeString(ctypes.Structure):
    _fields_ = [
        ("Length", ctypes.c_uint16),
        ("MaximumLength", ctypes.c_uint16),
        ("Buffer", ctypes.c_void_p),
    ]


def _command_line_reader() -> Callable[[int], str | None] | None:
    """A function reading one pid's command line, or ``None`` off Windows."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        ntdll = ctypes.WinDLL("ntdll")  # type: ignore[attr-defined]
        open_process = kernel32.OpenProcess
        close_handle = kernel32.CloseHandle
        query = ntdll.NtQueryInformationProcess
    except (AttributeError, OSError) as exc:
        logger.debug("process command line reader unavailable: %s", exc)
        return None
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    query.restype = ctypes.c_uint32

    def read(pid: int) -> str | None:
        handle = open_process(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return None
        try:
            size = _COMMAND_LINE_FIRST_BUFFER
            while True:
                buf = ctypes.create_string_buffer(size)
                needed = ctypes.c_uint32(0)
                status = query(
                    handle, _PROCESS_COMMAND_LINE_INFORMATION, buf, size, ctypes.byref(needed)
                )
                if status in _STATUS_TOO_SMALL and size < needed.value:
                    if needed.value > _COMMAND_LINE_MAX_BUFFER:
                        return None
                    size = needed.value
                    continue
                if status != 0:
                    return None
                return _command_line_from_buffer(buf)
        finally:
            close_handle(handle)

    return read


def _command_line_from_buffer(buf: ctypes.Array[ctypes.c_char]) -> str | None:
    """The command line in a ``ProcessCommandLineInformation`` answer.

    The answer is a ``UNICODE_STRING`` whose ``Buffer`` points into the same block;
    a pointer or length outside the block is refused rather than read.
    """
    head = _UnicodeString.from_buffer(buf)
    start = ctypes.addressof(buf)
    offset = (head.Buffer or 0) - start
    length = int(head.Length)
    if not head.Buffer or length % 2 or offset < 0 or offset + length > len(buf):
        return None
    return bytes(buf.raw[offset : offset + length]).decode("utf-16-le", errors="replace")


# ``MIB_IF_ROW2`` from netioapi.h, laid out field by field so ctypes computes the
# same offsets as the Windows SDK (1352 bytes on x64). WCHAR arrays are spelled as
# ``c_uint16`` so the layout is the same on every host the tests run on.
_IF_MAX_STRING_SIZE = 256
_IF_MAX_PHYS_ADDRESS_LENGTH = 32
_IF_TYPE_SOFTWARE_LOOPBACK = 24
# A tunnel interface (Teredo, IP-HTTPS, 6to4) carries bytes that also cross the
# adapter beneath it.
_IF_TYPE_TUNNEL = 131
# Bit 1 of ``InterfaceAndOperStatusFlags``: an NDIS filter interface, which
# repeats the counters of the adapter it is stacked on.
_IF_FLAG_FILTER_INTERFACE = 0x02


class _Guid(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_uint32),
        ("Data2", ctypes.c_uint16),
        ("Data3", ctypes.c_uint16),
        ("Data4", ctypes.c_uint8 * 8),
    ]


class _MibIfRow2(ctypes.Structure):
    _fields_ = [
        ("InterfaceLuid", ctypes.c_uint64),
        ("InterfaceIndex", ctypes.c_uint32),
        ("InterfaceGuid", _Guid),
        ("Alias", ctypes.c_uint16 * (_IF_MAX_STRING_SIZE + 1)),
        ("Description", ctypes.c_uint16 * (_IF_MAX_STRING_SIZE + 1)),
        ("PhysicalAddressLength", ctypes.c_uint32),
        ("PhysicalAddress", ctypes.c_uint8 * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("PermanentPhysicalAddress", ctypes.c_uint8 * _IF_MAX_PHYS_ADDRESS_LENGTH),
        ("Mtu", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TunnelType", ctypes.c_int32),
        ("MediaType", ctypes.c_int32),
        ("PhysicalMediumType", ctypes.c_int32),
        ("AccessType", ctypes.c_int32),
        ("DirectionType", ctypes.c_int32),
        ("InterfaceAndOperStatusFlags", ctypes.c_uint8),
        ("OperStatus", ctypes.c_int32),
        ("AdminStatus", ctypes.c_int32),
        ("MediaConnectState", ctypes.c_int32),
        ("NetworkGuid", _Guid),
        ("ConnectionType", ctypes.c_int32),
        ("TransmitLinkSpeed", ctypes.c_uint64),
        ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("InOctets", ctypes.c_uint64),
        ("InUcastPkts", ctypes.c_uint64),
        ("InNUcastPkts", ctypes.c_uint64),
        ("InDiscards", ctypes.c_uint64),
        ("InErrors", ctypes.c_uint64),
        ("InUnknownProtos", ctypes.c_uint64),
        ("InUcastOctets", ctypes.c_uint64),
        ("InMulticastOctets", ctypes.c_uint64),
        ("InBroadcastOctets", ctypes.c_uint64),
        ("OutOctets", ctypes.c_uint64),
        ("OutUcastPkts", ctypes.c_uint64),
        ("OutNUcastPkts", ctypes.c_uint64),
        ("OutDiscards", ctypes.c_uint64),
        ("OutErrors", ctypes.c_uint64),
        ("OutUcastOctets", ctypes.c_uint64),
        ("OutMulticastOctets", ctypes.c_uint64),
        ("OutBroadcastOctets", ctypes.c_uint64),
        ("OutQLen", ctypes.c_uint64),
    ]


class _MibIfTable2Header(ctypes.Structure):
    """The fixed head of ``MIB_IF_TABLE2``; the rows start at the next 8-byte boundary."""

    _fields_ = [("NumEntries", ctypes.c_uint32), ("_pad", ctypes.c_uint32)]


def _sum_interface_octets(rows: Sequence[_MibIfRow2]) -> tuple[int, int]:
    """Total received and sent bytes, skipping loopback, tunnel and NDIS filter rows."""
    rx_total = 0
    tx_total = 0
    for row in rows:
        if row.Type in (_IF_TYPE_SOFTWARE_LOOPBACK, _IF_TYPE_TUNNEL):
            continue
        if row.InterfaceAndOperStatusFlags & _IF_FLAG_FILTER_INTERFACE:
            continue
        rx_total += int(row.InOctets)
        tx_total += int(row.OutOctets)
    return rx_total, tx_total


def windows_network_octets() -> tuple[int, int] | None:
    """Return the host's total ``(received, sent)`` bytes, or ``None`` when unreadable."""
    try:
        iphlpapi = ctypes.WinDLL("iphlpapi")  # type: ignore[attr-defined]
        get_table = iphlpapi.GetIfTable2
        free_table = iphlpapi.FreeMibTable
    except (AttributeError, OSError) as exc:
        logger.debug("GetIfTable2 unavailable: %s", exc)
        return None
    get_table.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    get_table.restype = ctypes.c_uint32
    free_table.argtypes = [ctypes.c_void_p]
    free_table.restype = None
    table = ctypes.c_void_p()
    status = get_table(ctypes.byref(table))
    if status != 0 or not table.value:
        logger.debug("GetIfTable2 returned status %s", status)
        return None
    try:
        header = _MibIfTable2Header.from_address(table.value)
        rows = (_MibIfRow2 * header.NumEntries).from_address(
            table.value + ctypes.sizeof(_MibIfTable2Header)
        )
        return _sum_interface_octets(list(rows))
    finally:
        free_table(table)
