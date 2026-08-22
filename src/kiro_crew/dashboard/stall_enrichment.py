"""Stall-time enrichment — record WHAT the wedged loop was attached to.

A faulthandler loop-stall dump answers *where the code is stuck* but not
*which connection it was servicing* or *when the stall actually happened*
(the dump file is named for gateway **boot** time, not stall time, because
faulthandler needs a stable fd for the process lifetime).

Both production loop-stall dumps to date (2026-08-14 and 2026-08-20) froze
the main thread inside ``websockets`` frame parsing on an outbound TLS
socket — and the dump alone could not say which socket, nor whether its
receive queue was backed up.  This module closes that gap: the watchdog's
daemon thread (which keeps running while the loop thread is wedged) calls
:func:`collect_stall_enrichment` once a stall crosses the enrichment
threshold and emits the result to the logger at WARNING.

The capture is deliberately **not** written into the ``loopstall-*.txt``
crash-dump file: that file doubles as the boot-time crash *sentinel*
(``crash_dump_store._is_header_only`` classifies a prior session as
crashed by line count alone), so any watchdog-side append would make a
recovered 15–25s stall read as a fatal crash on the next startup —
false "work in flight was lost" notifications, cautious boot, and an
unreapable file aging real stall evidence out of rotation.  The journal
WARNING carries the capture for both recovered stalls (the process lives
to keep logging) and fatal ones (journald has already persisted it when
``_exit`` fires).

Captured per enrichment:

* an explicit UTC **stall timestamp** and the observed heartbeat silence —
  the crash-dump filename carries boot time, so the log line is what
  records the real stall time;
* every ESTABLISHED non-loopback TCP connection owned by this process,
  with local/remote endpoints and the kernel ``tx_queue``/``rx_queue``
  byte counts (a large ``rx_queue`` at stall time is direct evidence of an
  inbound flood on that socket).

Implementation is Linux-``/proc`` based (no subprocesses, no psutil — the
gateway venv does not ship psutil, and spawning a subprocess from a
watchdog thread in a sick process is riskier than reading procfs).  Socket
inodes come from the existing :func:`kiro_crew.acp.liveness.socket_inodes`
rather than a second spelling of that walk.  On non-Linux platforms or any
parse failure it degrades to a short note; it must never raise into the
watchdog.
"""

from __future__ import annotations

import ipaddress
import os
import struct
import sys
import time
from pathlib import Path

from kiro_crew.acp.liveness import socket_inodes

__all__ = ["collect_stall_enrichment"]

# /proc/net/tcp{,6} socket-state code for ESTABLISHED.
_TCP_ESTABLISHED = "01"


def _decode_proc_addr(hex_addr: str, hex_port: str) -> tuple[str, int]:
    """Decode a ``/proc/net/tcp{,6}`` address pair into ``(ip, port)``.

    IPv4 addresses are one little-endian 32-bit word rendered as 8 hex
    chars (``127.0.0.1`` → ``"0100007F"``); IPv6 are four such words (32
    hex chars).  Ports are plain big-endian hex.
    """
    port = int(hex_port, 16)
    if len(hex_addr) == 8:
        packed = struct.pack("<I", int(hex_addr, 16))
        return str(ipaddress.IPv4Address(packed)), port
    words = [struct.pack("<I", int(hex_addr[i : i + 8], 16)) for i in range(0, 32, 8)]
    return str(ipaddress.IPv6Address(b"".join(words))), port


def _established_lines(proc_file: str, inodes: set[str]) -> list[str]:
    """One line per ESTABLISHED non-loopback connection of ours in *proc_file*."""
    lines: list[str] = []
    try:
        rows = Path(proc_file).read_text().splitlines()[1:]
    except OSError:
        return lines
    for row in rows:
        cols = row.split()
        # sl local remote st tx:rx timers retrnsmt uid timeout inode ...
        if len(cols) < 10 or cols[3] != _TCP_ESTABLISHED or cols[9] not in inodes:
            continue
        try:
            lip, lport = _decode_proc_addr(*cols[1].split(":"))
            rip, rport = _decode_proc_addr(*cols[2].split(":"))
            if ipaddress.ip_address(rip).is_loopback:
                continue
            tx_hex, rx_hex = cols[4].split(":")
            lines.append(
                f"ESTAB {lip}:{lport} -> {rip}:{rport} "
                f"rx_queue={int(rx_hex, 16)}B tx_queue={int(tx_hex, 16)}B "
                f"inode={cols[9]}"
            )
        except (ValueError, OSError):
            continue  # malformed row; skip rather than lose the whole capture
    return lines


def collect_stall_enrichment(silence_secs: float) -> list[str]:
    """Return crash-dump enrichment lines for a loop stall in progress.

    Called from the watchdog's daemon thread while the event loop is wedged;
    must be cheap, allocation-light, and must never raise (the watchdog also
    guards the call, but degrade gracefully here too).
    """
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    header = (
        f"=== STALL ENRICHMENT @ {stamp} — loop silent {silence_secs:.1f}s; "
        "captured by watchdog daemon thread before dump-then-exit ==="
    )
    lines = [header]
    if not sys.platform.startswith("linux"):
        lines.append("(socket capture unavailable: non-Linux platform)")
        return lines
    try:
        inodes = socket_inodes("/proc", os.getpid())
        socket_lines = _established_lines("/proc/net/tcp", inodes)
        socket_lines += _established_lines("/proc/net/tcp6", inodes)
        if socket_lines:
            lines.extend(socket_lines)
            lines.append(
                f"({len(socket_lines)} established non-loopback TCP connection(s); "
                "a large rx_queue marks an inbound flood on that socket)"
            )
        else:
            lines.append("(no established non-loopback TCP connections)")
    except Exception as exc:  # noqa: BLE001 — enrichment must never hurt the watchdog
        lines.append(f"(socket capture failed: {exc!r})")
    return lines
