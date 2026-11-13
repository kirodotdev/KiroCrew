"""Windows sources for the System page's process count and network counters.

Windows has neither ``ps`` nor ``/proc``, so the two probes in
``handlers_system`` read these instead:

- process command lines from WMI ``Win32_Process`` through PowerShell
  ``Get-CimInstance``, the same source ``platform_compat.process_command_line``
  uses for a single pid;
- interface byte counters from ``iphlpapi!GetIfTable2``, which reports 64-bit
  ``InOctets`` / ``OutOctets`` per interface without spawning a process, so it is
  cheap enough for the page's live network graph.

Each reader answers ``None`` when its source cannot be read, so a caller can tell
"could not measure" apart from a real zero.
"""

from __future__ import annotations

import ctypes
import json
import logging
import re
import subprocess
from typing import Any, Sequence

from kiro_crew.platform_compat_native import _SUBPROCESS_NO_WINDOW
from kiro_crew.platform_compat_trust import trusted_system_bin
from kiro_crew.subprocess_utf8 import UTF8_TEXT

logger = logging.getLogger(__name__)

#: The characters a process signature may hold. Each signature is written into
#: PowerShell source, so anything PowerShell or WQL could read as syntax is out.
_SIGNATURE_SHAPE = re.compile(r"[A-Za-z0-9_.-]+")


def _wql_literal(text: str) -> str:
    """Quote *text* as a WQL ``LIKE`` pattern inside a single-quoted PowerShell string.

    WQL treats ``[`` and ``_`` as pattern characters, so they are bracketed to
    match literally; the PowerShell single quote is doubled.
    """
    escaped = text.replace("[", "[[]").replace("_", "[_]")
    return "%" + escaped.replace("'", "''") + "%"


def windows_process_command_lines(signatures: Sequence[str]) -> list[tuple[int, str]] | None:
    """Return ``(pid, command line)`` for every process whose command line holds a signature.

    One PowerShell call lists the matching processes as JSON; the WQL filter keeps
    the output to the processes the caller counts. Answers ``None`` when PowerShell
    is not found, fails, or prints something that is not the expected JSON.
    """
    if not signatures:
        return []
    if not all(_SIGNATURE_SHAPE.fullmatch(sig) for sig in signatures):
        raise ValueError("a process signature must be letters, digits, '_', '.' or '-'")
    powershell_bin = trusted_system_bin("powershell")
    if powershell_bin is None:
        return None
    wql = " OR ".join(f"CommandLine LIKE '{_wql_literal(sig)}'" for sig in signatures)
    # The WQL filter sits inside a double-quoted PowerShell string, so a ``"``,
    # a ``$`` or a backtick in a signature would end it or expand into code;
    # ``_SIGNATURE_SHAPE`` admits none of them. The querying PowerShell carries
    # every signature in its own command line, so ``$PID`` (its own pid) is
    # dropped from the rows.
    command = (
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        f'$rows = @(Get-CimInstance Win32_Process -Filter "{wql}" '
        "| Where-Object { $_.ProcessId -ne $PID } "
        "| Select-Object ProcessId, CommandLine); "
        "ConvertTo-Json -InputObject $rows -Compress"
    )
    try:
        out = subprocess.check_output(
            [powershell_bin, "-NoProfile", "-NonInteractive", "-Command", command],
            stderr=subprocess.DEVNULL,
            timeout=10,
            creationflags=_SUBPROCESS_NO_WINDOW,
            **UTF8_TEXT,
        )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        logger.debug("Win32_Process query failed: %s", exc)
        return None
    return _parse_process_rows(out)


def _parse_process_rows(out: str) -> list[tuple[int, str]] | None:
    """Read the ``ConvertTo-Json`` output of :func:`windows_process_command_lines`."""
    text = out.strip()
    if not text:
        return []
    try:
        parsed: Any = json.loads(text)
    except ValueError:
        logger.debug("Win32_Process query printed non-JSON output")
        return None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list):
        return None
    rows: list[tuple[int, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        pid = item.get("ProcessId")
        cmd = item.get("CommandLine")
        if isinstance(pid, int) and not isinstance(pid, bool) and isinstance(cmd, str):
            rows.append((pid, cmd))
    return rows


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
