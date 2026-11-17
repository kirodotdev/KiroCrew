#!/usr/bin/env python3
"""Count why the sensitive-path gate refuses plain files under CPU load, by arm.

WHY THIS EXISTS
---------------
The gate resolves symlinks in a child interpreter (``kiro_crew.subprocess_pool``)
within a wall-clock budget, and refuses fail-closed when the budget runs out.  The
child's ``realpath`` takes microseconds and holds no GIL of the gateway's, yet
under CPU load the gate refused ordinary files.  The refusals come from the
CALLER side of ``_run_resolution_bounded``: the calling thread's own wait for the
GIL counts against its deadline.  Each refusal arm logs its own DEBUG line, so
counting those lines tells the arms apart; the bed's WARNING-level logs cannot.

This is a measurement, not a test, because its counts depend on how fast the
host is.  The suite pins each arm with a deterministic double instead
(``test/test_security_path_gate_under_load.py``).

WHAT IT DOES
------------
Starts ``--busy`` threads that spin in pure Python, then ``--callers`` threads
that each ask ``sensitive_path_refusal`` about one plain file ``--calls`` times,
all off the event loop (the shape of the memory-read pool and the tool gate).
``--on-loop`` makes each caller ask from inside its own running event loop
instead.  It prints one JSON object: the outcome counts and the arm counts,
the pool size, and the resident set of one resolver child in KiB.

The arms, by the DEBUG line each one logs:

``allowance``    the calling thread's cumulative wait allowance was spent
``lease``        no resolver child was free within the budget
``load_arm``     a child took the request and was not in the filesystem when
                 the budget ran out; not charged, up to three per prefix per 10 s
``clamped``      the budget or grace was cut short by the allowance
``charged``      the "stalled mount?" warning: a prefix cooldown opened
``cooldown``     a refusal under an open cooldown ("timed out under")

HOW TO RUN
----------
From a checkout, with an interpreter that can import ``kiro_crew``.  HOME and
KIROCREW_HOME are pointed at a temporary directory before ``kiro_crew`` is
imported, so the run never reads the operator's data home::

    PYTHONPATH=src python scripts/measure_path_gate_under_load.py --busy 32 \\
      --callers 16 --calls 20 --workers 8

To compare against another revision, run this file with ``PYTHONPATH`` pointing
at that revision's ``src``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import tempfile
import threading
import time
from collections import Counter

_ARMS = {
    "allowance": "cumulative wait allowance exhausted",
    "lease": "resolver pool was saturated",
    "load_arm": "timed out under load",
    "clamped": "clamped by the calling thread",
    "charged": "stalled mount?",
}


class _ArmCounter(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.counts: Counter[str] = Counter()
        self.samples: Counter[str] = Counter()
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("resolver child syscall="):
            with self._lock:
                self.samples[message.split()[2].split("=", 1)[1]] += 1
        for arm, needle in _ARMS.items():
            if needle in message:
                with self._lock:
                    self.counts[arm] += 1


def _rss_kib(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as status:
            for line in status:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        return None
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--busy", type=int, default=32, help="spinning threads")
    parser.add_argument("--callers", type=int, default=16, help="threads asking the gate")
    parser.add_argument("--calls", type=int, default=20, help="checks per caller")
    parser.add_argument("--workers", type=int, default=None, help="resolver children")
    parser.add_argument("--on-loop", action="store_true", help="ask from a running loop")
    args = parser.parse_args()

    scratch = tempfile.TemporaryDirectory(prefix="kc-path-gate-load-")
    home = os.path.join(scratch.name, "home")
    os.makedirs(os.path.join(home, ".kiro", "crew"))
    os.environ["HOME"] = home
    os.environ["USERPROFILE"] = home
    os.environ["KIROCREW_HOME"] = os.path.join(home, ".kiro", "crew")
    if args.workers is not None:
        os.environ["KIROCREW_PATH_RESOLVE_WORKERS"] = str(args.workers)

    import kiro_crew.executors as ex
    from kiro_crew import security

    counter = _ArmCounter()
    gate_logger = logging.getLogger("kiro_crew.security.paths")
    gate_logger.addHandler(counter)
    gate_logger.setLevel(logging.DEBUG)
    gate_logger.propagate = False

    target = os.path.join(scratch.name, "ws", "notes.md")
    os.makedirs(os.path.dirname(target))
    with open(target, "w", encoding="utf-8") as handle:
        handle.write("hello")

    pool = ex.path_resolve_executor()
    deadline = time.monotonic() + 30.0
    while not all(child.proc is not None for child in pool._children):
        if time.monotonic() > deadline:
            print("resolver children never started", file=sys.stderr)
            return 2
        time.sleep(0.05)
    rss = _rss_kib(pool._children[0].proc.pid)

    stop = threading.Event()

    def spin() -> None:
        while not stop.is_set():
            sum(range(2000))

    outcomes: Counter[str] = Counter()
    outcomes_lock = threading.Lock()

    def ask() -> None:
        for _ in range(args.calls):
            reason = security.sensitive_path_refusal(target)
            if reason is None:
                key = "ok"
            elif "timed out under" in reason:
                key = "cooldown"
            else:
                key = "refused"
            with outcomes_lock:
                outcomes[key] += 1

    def caller() -> None:
        if args.on_loop:

            async def _ask() -> None:
                ask()

            asyncio.run(_ask())
        else:
            ask()

    spinners = [threading.Thread(target=spin, daemon=True) for _ in range(args.busy)]
    for thread in spinners:
        thread.start()
    started = time.monotonic()
    callers = [threading.Thread(target=caller) for _ in range(args.callers)]
    for thread in callers:
        thread.start()
    for thread in callers:
        thread.join(timeout=600.0)
    elapsed = time.monotonic() - started
    stop.set()
    for thread in spinners:
        thread.join(timeout=10.0)

    result = {
        "busy": args.busy,
        "callers": args.callers,
        "calls": args.calls,
        "on_loop": args.on_loop,
        "workers": len(pool._children),
        "child_rss_kib": rss,
        "elapsed_s": round(elapsed, 2),
        "outcomes": dict(outcomes),
        "arms": {arm: counter.counts.get(arm, 0) for arm in _ARMS},
        "sampled_syscalls": dict(counter.samples),
    }
    print(json.dumps(result, sort_keys=True))
    ex.shutdown_maintenance_executor()
    scratch.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
