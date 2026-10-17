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

The watchdog adds **where the loop thread is** beside them
(:func:`describe_loop_thread`), read while the stall is still in progress.
A loop blocked inside a native callback has no Python frame of its own:
faulthandler's dump of it ends at asyncio's ``Handle._run`` and cannot say
what that handle was running.  The capture names the handle's callback, so
an ``os.close``, an ``os.waitpid`` or a C-extension method scheduled
straight onto the loop is identified by name rather than by elimination.
The full stack is not repeated here: faulthandler's dump and the diag
recorder's ``loop_stall`` event already carry it.

The socket table is Linux-``/proc`` based (no subprocesses, no psutil — the
gateway venv does not ship psutil, and spawning a subprocess from a
watchdog thread in a sick process is riskier than reading procfs).  Socket
inodes come from the existing :func:`kiro_crew.acp.liveness.socket_inodes`
rather than a second spelling of that walk.  On non-Linux platforms or any
parse failure it degrades to a short note; the loop thread's position comes
from the interpreter and is captured on every platform.  Neither capture
may raise into the watchdog.
"""

from __future__ import annotations

import asyncio.events
import functools
import ipaddress
import os
import struct
import sys
import time
import types
from pathlib import Path

from kiro_crew.acp.liveness import socket_inodes
from kiro_crew.diag.threads import _redact_frame

__all__ = ["collect_stall_enrichment", "describe_loop_thread"]

# /proc/net/tcp{,6} socket-state code for ESTABLISHED.
_TCP_ESTABLISHED = "01"

#: The code object every asyncio ``Handle`` (and ``TimerHandle``) callback
#: runs under.  An identity check against it cannot match a same-named
#: function anywhere else.
_HANDLE_RUN_CODE = asyncio.events.Handle._run.__code__


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


def _callable_name(callback: object) -> tuple[str, bool]:
    """Return ``(dotted name, is_native)`` for a loop callback.

    Built from attributes alone, never from ``repr``: the ``repr`` of a bound
    method or a ``functools.partial`` runs the owner's and the arguments'
    ``__repr__``, which is arbitrary code executed on the watchdog thread while
    the loop thread is stopped inside those same objects.  ``is_native`` is
    true when the callable is not a Python function, which is exactly the
    case faulthandler cannot name.
    """
    for _ in range(8):
        if not isinstance(callback, functools.partial):
            break
        callback = callback.func
    function = getattr(callback, "__func__", callback)
    native = not isinstance(function, types.FunctionType)
    name = getattr(callback, "__qualname__", None) or getattr(callback, "__name__", None)
    if not isinstance(name, str):
        name = type(callback).__qualname__
    module = getattr(callback, "__module__", None)
    if not isinstance(module, str):
        owner = getattr(callback, "__self__", None)
        if isinstance(owner, types.ModuleType):
            module = owner.__name__
        elif owner is not None:
            module = type(owner).__module__
    return (f"{module}.{name}" if isinstance(module, str) else name), native


def describe_loop_thread(thread_id: int | None) -> list[str]:
    """Return lines naming where the event-loop thread is right now.

    *thread_id* is the loop thread's ``threading.get_ident()``, recorded by the
    watchdog's heartbeat; ``None`` (no heartbeat yet) yields no lines.  The
    first line is the loop thread's innermost Python frame from
    :func:`sys._current_frames`, formatted and scrubbed by the frame redactor
    the diag recorder's stall capture uses.  When that frame is asyncio's
    ``Handle._run``, a second line names the callback the handle is running
    and says whether it is native: a native callback contributes no frame of
    its own, so this line is the only record of what the loop is inside.
    Called from the watchdog's daemon thread; never raises.
    """
    if thread_id is None:
        return []
    try:
        frame = sys._current_frames().get(thread_id)
    except Exception as exc:  # noqa: BLE001 — enrichment must never hurt the watchdog
        return [f"(loop thread capture failed: {exc!r})"]
    if frame is None:
        return [f"(loop thread {thread_id:#x} has no Python frame)"]
    try:
        code = frame.f_code
        where = _redact_frame(code.co_filename, frame.f_lineno or 0, code.co_name)
        lines = [f"loop thread {thread_id:#x} is at {where}"]
        if code is _HANDLE_RUN_CODE:
            handle = frame.f_locals.get("self")
            callback = getattr(handle, "_callback", None)
            if callback is not None:
                name, native = _callable_name(callback)
                kind = (
                    "a native callable with no Python frame of its own"
                    if native
                    else "a Python callable"
                )
                lines.append(f"  asyncio Handle._run is running {name} ({kind})")
    except Exception as exc:  # noqa: BLE001 — a frame read must never hurt the watchdog
        return [f"(loop thread frame read failed: {exc!r})"]
    return lines
