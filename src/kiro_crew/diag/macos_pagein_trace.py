"""Standalone, read-only macOS page-in / memory-pressure sampler.

Why this exists: the loop-stall watchdog's macOS paging deferral
(:func:`kiro_crew.dashboard.loop_watchdog.read_loop_pageins_under_pressure`)
acts on a rise in the process's ``ri_pageins`` count while the host is under
memory pressure. That signal is process-wide and the compressor undercounts it,
so before trusting it we want to SEE what ``ri_pageins`` + the kernel pressure
level actually do across a real stall on this machine. But the stall often kills
the gateway, taking any in-process telemetry with it.

So this sampler runs OUTSIDE the gateway, as its own process. It only READS two
kernel structs on a cadence -- ``proc_pid_rusage`` (for ``ri_pageins`` and
``phys_footprint``) and ``kern.memorystatus_vm_pressure_level`` -- and appends one
JSON line per sample to a durable log under the data home's ``logs/`` directory
(``$KIROCREW_HOME`` or ``~/.kiro/crew``). It
allocates almost nothing and spawns nothing, so it cannot itself add memory
pressure, and because the file is fsync'd per line, a hard kill of the watched
gateway loses at most the in-flight sample. On the next launch the JSONL is the
post-mortem of what paging and pressure looked like across the stall.

Usage::

    python -m kiro_crew.diag.macos_pagein_trace            # sample self every 5s
    python -m kiro_crew.diag.macos_pagein_trace --pid 6584 # watch the gateway pid
    python -m kiro_crew.diag.macos_pagein_trace --interval 1 --count 600

Nothing here imports the gateway or the event loop, so it stays runnable even
when the gateway is wedged or dead.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import signal
import sys
import time
import types
from pathlib import Path

from kiro_crew import platform_compat


def default_output_path() -> Path:
    """Durable per-user log path that survives a gateway crash and a logout.

    Writes under the data home's ``logs/`` directory (``$KIROCREW_HOME`` or
    ``~/.kiro/crew``), the same place the gateway keeps its crash dumps. The
    trace then sits beside the dumps it explains and is not cleared on reboot
    or by a tmp reaper, unlike a runtime or scratch directory.
    """
    env = os.environ.get("KIROCREW_HOME", "").strip()
    data_home = Path(env) if env else Path.home() / ".kiro" / "crew"
    return data_home / "logs" / "pagein-trace.jsonl"


def sample(pid: int) -> dict[str, object]:
    """One read-only snapshot of *pid*'s paging + the host pressure level.

    Reads only; allocates only the small result dict. ``None`` fields mean the
    kernel read was unavailable (not on macOS, libproc missing, or a failed
    call), never a fabricated zero.
    """
    level = platform_compat.memory_pressure_level()
    return {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "mono": time.monotonic(),
        "pid": pid,
        "ri_pageins": platform_compat.proc_pageins_for_pid(pid),
        "phys_footprint_bytes": platform_compat.proc_phys_footprint_bytes_for_pid(pid),
        "pressure_level": level,
        "pressure_name": platform_compat.memory_pressure_name(level) or None,
    }


def _append_jsonl(path: Path, record: dict[str, object]) -> None:
    """Append one JSON line and fsync it, so a hard kill loses at most this line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)


def run(pid: int, interval: float, count: int | None, out: Path) -> int:
    """Sample on a cadence until *count* samples (or forever) / a stop signal.

    Returns a process exit code. Never raises on a single failed read -- it
    records the ``None`` fields and keeps sampling, because the whole point is to
    survive the window around a crash.
    """
    stop = {"now": False}

    def _handle(_signum: int, _frame: "types.FrameType | None") -> None:
        stop["now"] = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    print(f"pagein-trace: pid={pid} interval={interval}s -> {out}", file=sys.stderr)
    taken = 0
    while not stop["now"] and (count is None or taken < count):
        try:
            _append_jsonl(out, sample(pid))
        except OSError as exc:  # a write failure must not kill the sampler
            print(f"pagein-trace: write failed: {exc}", file=sys.stderr)
        taken += 1
        if count is not None and taken >= count:
            break
        # Interruptible sleep so a stop signal ends the run promptly.
        slept = 0.0
        while slept < interval and not stop["now"]:
            time.sleep(min(0.5, interval - slept))
            slept += 0.5
    print(f"pagein-trace: stopped after {taken} sample(s)", file=sys.stderr)
    return 0


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kiro_crew.diag.macos_pagein_trace",
        description="Read-only macOS ri_pageins + memory-pressure sampler (survives a crash).",
    )
    parser.add_argument(
        "--pid", type=int, default=os.getpid(), help="pid to sample (default: self)"
    )
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between samples")
    parser.add_argument(
        "--count", type=int, default=None, help="stop after N samples (default: forever)"
    )
    parser.add_argument("--out", type=Path, default=None, help="JSONL output path")
    args = parser.parse_args(argv)

    if not platform_compat.IS_MACOS:
        print("pagein-trace: macOS only (ri_pageins is a Darwin counter)", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("pagein-trace: --interval must be positive", file=sys.stderr)
        return 2

    out = args.out or default_output_path()
    return run(args.pid, args.interval, args.count, out)


if __name__ == "__main__":
    raise SystemExit(main())
