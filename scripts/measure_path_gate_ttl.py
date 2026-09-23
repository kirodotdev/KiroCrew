#!/usr/bin/env python3
"""Measure the sensitive-path gate under GIL contention and derive its TTL law.

WHY THIS EXISTS
---------------
``kiro_crew.security.paths`` caches the protected-anchor set for
``_HOME_TARGETS_TTL_SECS``.  That constant was chosen against an IDLE
interpreter, where one rebuild costs about 2ms, so a 0.1s expiry spends about
2% of the wall clock rebuilding.  Under contention the SAME rebuild costs
hundreds of times more -- it is ~130 ``os.path.realpath`` calls, each releasing
and re-acquiring the GIL, and every re-acquisition can wait a full
``sys.getswitchinterval()`` behind a CPU-bound sibling thread -- while the
expiry does not move.  The amortised share of the wall clock spent rebuilding
therefore grows with load until the rebuild misses its budget and the gate
refuses ordinary files (issue #10255).

The fix makes the expiry track the measured rebuild cost:

    ttl = clamp(last_rebuild_secs * K, _HOME_TARGETS_TTL_SECS, cap)

This script is how ``K`` and ``cap`` are chosen from measurement rather than
taste, and how the choice can be re-checked on another host.

WHAT IT MEASURES
----------------
``rebuild``  The cost of one uncached anchor rebuild -- the work the cache
             exists to avoid -- with N sibling threads holding the GIL.

``ksweep``   The derivation.  ``K`` is not a speed knob, it is the amortised
             share of the wall clock the gate may spend rebuilding: holding
             ``ttl = rebuild * K`` means one rebuild per ``K`` rebuild costs,
             i.e. a ``1/K`` share, at every load level.  Each cell drives the
             SHIPPED law with one candidate ``K``, so every build's own measured
             cost selects its own expiry exactly as in production, and the
             baseline cell is the pre-#10255 fixed expiry reached through the
             module's own ratio knob set to 0.

             Cells are ranked on REFUSALS first, then on how many rebuilds they
             paid.  Deliberately not on latency: once a stall opens a per-prefix
             cooldown, every later call is refused without touching the
             filesystem, so the worst policies answer fastest.  The chosen ``K``
             is the smallest one that refuses nothing and rebuilds fewest times,
             because a larger one only widens the window in which a symlink
             swapped deep inside the crew home is still answered from the cache.

HOW TO RUN
----------
From a checkout, with an interpreter that can import ``kiro_crew``::

    KIROCREW_HOME="$SOME_SCRATCH_DIR" \\
      PYTHONPATH=src python scripts/measure_path_gate_ttl.py ksweep \\
      > ksweep.log 2>&1 </dev/null

The rebuild cost alone, which is the quickest way to see the mechanism::

    ... measure_path_gate_ttl.py rebuild --hogs 0,1,2,4,8

``--json PATH`` additionally writes the raw rows, so a PR table can be rebuilt
without re-running the measurement.

WHAT THE LAW CANNOT DO, AND WHY THE SWEEP SHOWS IT
-------------------------------------------------
An expiry controls how OFTEN the rebuild is paid, never what one costs.  Past a
certain load a SINGLE cold rebuild already exceeds
``_PATH_RESOLVE_REBUILD_TIMEOUT_SECS``, and there every policy refuses alike --
the first refusal arrives before any cache exists to serve.  The sweep reports
those load levels as having no clean ``K``.  Keep them in the grid: they are the
measured boundary of what this law fixes, and what a resolver outside the GIL's
reach would have to fix instead.

COST AND SAFETY
---------------
The sibling threads are pure-Python loops.  They contend for the GIL rather
than for cores, so the whole run costs roughly one core no matter how many are
asked for, and nothing is written outside ``KIROCREW_HOME``.  Point
``KIROCREW_HOME`` at a scratch directory: the gate anchors its target set on
that variable, and the run should not depend on the operator's real crew home.
A ``ksweep`` takes several minutes, because the contended cells deliberately
wait out the refusals they are there to count.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from kiro_crew import security
from kiro_crew.security import paths as gate

#: Load levels the sweep walks.  Chosen to bracket the transition: an idle
#: interpreter, the single sibling the raised rebuild budget was sized for, and
#: the handful a gateway with a few busy sessions actually runs.
DEFAULT_HOGS = (0, 1, 2, 4, 8)

#: Candidate cost ratios the derivation walks.  ``K`` is the reciprocal of the
#: share of the wall clock the gate may spend rebuilding, so this spans a 10%
#: share down to a 0.5% one.
DEFAULT_KS = (10.0, 25.0, 50.0, 100.0, 200.0)


@dataclass
class RebuildRow:
    """One ``rebuild`` cell: the cost of the work the cache avoids."""

    hogs: int
    samples: int
    median_ms: float
    max_ms: float


@dataclass
class WalkRow:
    """One ``walk`` cell: what the gate does for a caller at this load and policy."""

    hogs: int
    #: The ``K`` in force, or ``None`` for the fixed-expiry baseline.
    cost_ratio: float | None
    calls: int
    #: Ordinary project files the gate answered True for. Each one is the
    #: reported defect: a stall refusal, or a per-prefix cooldown refusal that a
    #: stall opened. This is the metric that decides a policy; latency is
    #: secondary, because a cooldown answers instantly and therefore looks fast.
    refusals: int
    rebuilds: int
    #: Latency over the calls that were NOT refused, so a cooldown's instant
    #: refusals cannot flatter the number.
    median_served_ms: float
    max_served_ms: float
    wall_secs: float
    #: What the law actually selected during the cell, smallest and largest.
    ttl_min_secs: float
    ttl_max_secs: float
    #: Share of the cell's wall clock spent inside anchor rebuilds, using this
    #: load level's measured rebuild cost. The law aims to hold this at ``1 / K``,
    #: so it is how the model is checked rather than assumed.
    rebuild_share: float


class _GilLoad:
    """N sibling threads running pure Python, as a context manager.

    Pure Python rather than a C-level loop on purpose: it releases the GIL every
    switch interval, which is the ordinary case -- a session compaction, a large
    tool-input scan, a dashboard burst.  A long C-level regex holds the GIL for
    its whole match and is strictly worse, so the numbers here are the mild end
    of the range, not the tail.
    """

    def __init__(self, count: int) -> None:
        self._count = count
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def __enter__(self) -> _GilLoad:
        for index in range(self._count):
            thread = threading.Thread(target=self._spin, name=f"hog-{index}", daemon=True)
            thread.start()
            self._threads.append(thread)
        if self._count:
            # Let every sibling reach its loop, so the measured call competes
            # with the full asked-for load rather than with a ramp.
            time.sleep(0.05)
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5.0)

    def _spin(self) -> None:
        while not self._stop.is_set():
            sum(index * index for index in range(20_000))


def _reset_gate_state() -> None:
    """Clear everything a previous cell could carry into this one.

    The anchor cache, the per-prefix stall cooldowns, the load-arm probe counts
    and the per-thread wait allowance all persist for seconds by design, and a
    contended cell fills all four.  Leaving them would make the next cell read
    faster than it is: a prefix already in cooldown refuses without touching the
    filesystem at all.
    """
    gate._home_targets_cache.clear()
    gate._path_resolve_degraded.clear()
    gate._path_resolve_load_probes.clear()
    gate._path_resolve_thread_waits.clear()
    gate._path_resolve_wedged.clear()


def _anchor_inputs() -> tuple[list[str], gate._ResolvedRoots]:
    """The exact arguments the gate's own rebuild is called with."""
    roots = gate._resolve_root_anchors(str(Path.home()))
    return list(gate._SENSITIVE_HOME_DIRS), roots


def measure_rebuild(hogs: int, samples: int) -> RebuildRow:
    """Time ``samples`` uncached anchor rebuilds with ``hogs`` siblings running.

    Timed on a WORKER thread, not the main thread, because that is where the
    gate runs it: the pool worker is the thread whose GIL re-acquisitions the
    siblings delay.
    """
    home_dirs, roots = _anchor_inputs()
    durations: list[float] = []

    def _run() -> None:
        # One discarded warm-up: the first build also pays the import-time and
        # page-cache costs the cache would never pay again.
        gate._home_dir_targets_uncached(home_dirs, roots)
        for _ in range(samples):
            started = time.monotonic()
            gate._home_dir_targets_uncached(home_dirs, roots)
            durations.append((time.monotonic() - started) * 1000.0)

    with _GilLoad(hogs):
        worker = threading.Thread(target=_run, name="rebuild-probe")
        worker.start()
        worker.join()

    return RebuildRow(
        hogs=hogs,
        samples=len(durations),
        median_ms=round(statistics.median(durations), 1) if durations else 0.0,
        max_ms=round(max(durations), 1) if durations else 0.0,
    )


def _candidate_files(limit: int) -> list[str]:
    """Ordinary project files to gate, which no correct gate ever refuses."""
    root = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "security"
    files = sorted(str(path) for path in root.rglob("*.py"))
    if not files:  # pragma: no cover - a checkout always has these
        raise SystemExit(f"no candidate files under {root}")
    while len(files) < limit:
        files = files + files
    return files[:limit]


def measure_walk(
    hogs: int,
    duration: float,
    *,
    cost_ratio: float | None,
    cap_secs: float,
    rebuild_secs: float | None = None,
) -> WalkRow:
    """Drive the real gate for ``duration`` seconds under one expiry policy.

    ``cost_ratio`` of ``None`` selects the pre-#10255 behaviour through the
    ratio knob set to 0, so the baseline
    row is the shipped fixed expiry rather than an imitation of it.  Otherwise
    the SHIPPED law runs with that ratio and cap, and every build's own measured
    cost sets its own expiry -- which is the point: a TTL pinned to one value
    cannot represent a law whose whole job is to select a different value per
    build.
    """
    files = _candidate_files(64)
    rebuilds = 0
    ttls: list[float] = []
    real_rebuild = gate._home_dir_targets_uncached
    real_ttl = gate._home_targets_ttl

    def _counting_rebuild(*args: object, **kwargs: object) -> set[str]:
        nonlocal rebuilds
        rebuilds += 1
        return real_rebuild(*args, **kwargs)  # type: ignore[arg-type]

    def _recording_ttl(cost: float, **kwargs: object) -> float:
        # Forward whatever the cache passes rather than naming the keywords: this
        # wrapper's job is to OBSERVE the shipped law, so a signature that has to
        # be kept in step with it would turn every future parameter into a crash
        # here (and a crash mid-sweep reads as a measurement, not as a bug).
        chosen = real_ttl(cost, **kwargs)  # type: ignore[arg-type]
        ttls.append(chosen)
        return chosen

    served: list[float] = []
    refusals = 0
    calls = 0

    def _drive() -> None:
        nonlocal refusals, calls
        deadline = time.monotonic() + duration
        index = 0
        while time.monotonic() < deadline:
            path = files[index % len(files)]
            index += 1
            started = time.monotonic()
            verdict = security.is_sensitive_path(path)
            elapsed_ms = (time.monotonic() - started) * 1000.0
            calls += 1
            if verdict:
                # An ordinary source file under the checkout. A True here is the
                # defect: a stall refusal, or a cooldown refusal a stall opened.
                refusals += 1
            else:
                served.append(elapsed_ms)

    _reset_gate_state()
    previous = (
        gate._HOME_TARGETS_TTL_COST_RATIO,
        gate._HOME_TARGETS_TTL_MAX_SECS,
    )
    # A ratio of 0 is the fixed-expiry baseline, which is also the shipped revert.
    gate._HOME_TARGETS_TTL_COST_RATIO = 0.0 if cost_ratio is None else cost_ratio
    gate._HOME_TARGETS_TTL_MAX_SECS = cap_secs
    gate._home_dir_targets_uncached = _counting_rebuild  # type: ignore[assignment]
    gate._home_targets_ttl = _recording_ttl  # type: ignore[assignment]
    wall_started = time.monotonic()
    try:
        with _GilLoad(hogs):
            worker = threading.Thread(target=_drive, name="gate-probe")
            worker.start()
            worker.join()
    finally:
        wall_secs = time.monotonic() - wall_started
        gate._home_dir_targets_uncached = real_rebuild  # type: ignore[assignment]
        gate._home_targets_ttl = real_ttl  # type: ignore[assignment]
        (
            gate._HOME_TARGETS_TTL_COST_RATIO,
            gate._HOME_TARGETS_TTL_MAX_SECS,
        ) = previous
        _reset_gate_state()

    share = 0.0
    if rebuild_secs and wall_secs:
        share = min(1.0, rebuilds * rebuild_secs / wall_secs)
    return WalkRow(
        hogs=hogs,
        cost_ratio=cost_ratio,
        calls=calls,
        refusals=refusals,
        rebuilds=rebuilds,
        median_served_ms=round(statistics.median(served), 2) if served else 0.0,
        max_served_ms=round(max(served), 1) if served else 0.0,
        wall_secs=round(wall_secs, 1),
        ttl_min_secs=round(min(ttls), 3) if ttls else 0.0,
        ttl_max_secs=round(max(ttls), 3) if ttls else 0.0,
        rebuild_share=round(share, 3),
    )


_WALK_HEADERS = [
    "hogs",
    "K",
    "ttl low s",
    "ttl high s",
    "calls",
    "REFUSED",
    "rebuilds",
    "p50 served ms",
    "max served ms",
    "rebuild share",
]


def _walk_cells(row: WalkRow) -> list[str]:
    return [
        str(row.hogs),
        "fixed" if row.cost_ratio is None else f"{row.cost_ratio:.0f}",
        f"{row.ttl_min_secs}",
        f"{row.ttl_max_secs}",
        str(row.calls),
        str(row.refusals),
        str(row.rebuilds),
        f"{row.median_served_ms}",
        f"{row.max_served_ms}",
        f"{row.rebuild_share}",
    ]


def _pick_cost_ratio(
    rows: list[WalkRow], max_demanded_ttl: float, rebuild_tolerance: float
) -> WalkRow | None:
    """The smallest ``K`` that refuses nothing, at an expiry worth demanding.

    Three filters, in order, because each rules out a different kind of bad
    answer:

    1. REFUSALS must be zero.  Not latency: once a stall opens a per-prefix
       cooldown every later call is refused WITHOUT touching the filesystem, so
       the worst policies answer fastest.
    2. The expiry the row DEMANDS must stay within *max_demanded_ttl*.  This is
       the one policy input the measurement cannot supply: a larger ``K`` always
       pays fewer rebuilds, so without a bound on the stale window "fewest
       rebuilds" degenerates into "largest ``K``" and the answer is minutes of
       staleness bought for a rebuild count already at its floor.
    3. Among what is left, the smallest ``K`` whose rebuild count is within
       *rebuild_tolerance* times the fewest -- the diminishing-returns point.

    Rebuild count rather than a latency median because it is the quantity the law
    controls and it is an integer, so it does not need the sample size a stable
    median would.
    """
    clean = [
        row
        for row in rows
        if row.refusals == 0 and row.cost_ratio is not None and row.ttl_max_secs <= max_demanded_ttl
    ]
    if not clean:
        return None
    fewest = min(row.rebuilds for row in clean)
    within = [row for row in clean if row.rebuilds <= max(1.0, fewest * rebuild_tolerance)]
    return min(within, key=lambda row: row.cost_ratio or 0.0)


def _as_float(value: object) -> float:
    """Read a number back out of a derivation entry.

    The entries are built as JSON-shaped ``dict[str, object]`` so they can be
    written with ``--json`` unchanged, which loses the element types on the way
    back out.
    """
    assert isinstance(value, (int, float)), value
    return float(value)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    if not rows:
        print("(no rows)")
        return
    widths = [max(len(headers[i]), *(len(row[i]) for row in rows)) for i in range(len(headers))]
    print("  ".join(header.ljust(widths[i]) for i, header in enumerate(headers)))
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def _provenance() -> dict[str, object]:
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "switch_interval_secs": sys.getswitchinterval(),
        "shipped_ttl_floor_secs": gate._HOME_TARGETS_TTL_SECS,
        "shipped_cost_ratio": gate._HOME_TARGETS_TTL_COST_RATIO,
        "shipped_ttl_cap_secs": gate._HOME_TARGETS_TTL_MAX_SECS,
        "rebuild_budget_secs": gate._PATH_RESOLVE_REBUILD_TIMEOUT_SECS,
        "candidate_budget_secs": gate._PATH_RESOLVE_TIMEOUT_SECS,
        "kirocrew_home": os.environ.get("KIROCREW_HOME", "<unset>"),
    }


def _cmd_rebuild(args: argparse.Namespace) -> dict[str, object]:
    rows = [measure_rebuild(hogs, args.samples) for hogs in args.hogs]
    _print_table(
        ["hogs", "rebuild median ms", "rebuild max ms", "samples"],
        [[str(row.hogs), f"{row.median_ms}", f"{row.max_ms}", str(row.samples)] for row in rows],
    )
    return {"rebuild": [asdict(row) for row in rows]}


def _cmd_ksweep(args: argparse.Namespace) -> dict[str, object]:
    rebuilds = {hogs: measure_rebuild(hogs, args.samples) for hogs in args.hogs}
    print("== rebuild cost: the work the cache avoids ==")
    _print_table(
        ["hogs", "rebuild median ms", "rebuild max ms", "samples"],
        [
            [str(row.hogs), f"{row.median_ms}", f"{row.max_ms}", str(row.samples)]
            for row in rebuilds.values()
        ],
    )

    rows: list[WalkRow] = []
    per_level: dict[int, list[WalkRow]] = {}
    baselines: dict[int, WalkRow] = {}
    for hogs in args.hogs:
        rebuild_secs = rebuilds[hogs].median_ms / 1000.0
        baselines[hogs] = measure_walk(
            hogs,
            args.duration,
            cost_ratio=None,
            cap_secs=args.cap_probe,
            rebuild_secs=rebuild_secs,
        )
        level = [
            measure_walk(
                hogs,
                args.duration,
                cost_ratio=ratio,
                cap_secs=args.cap_probe,
                rebuild_secs=rebuild_secs,
            )
            for ratio in args.ks
        ]
        per_level[hogs] = level
        rows.append(baselines[hogs])
        rows.extend(level)

    print()
    print("== the gate under each expiry policy ==")
    print(
        f"(K 'fixed' is the pre-#10255 behaviour, reached through "
        f"a ratio of 0; cap held at {args.cap_probe}s so the "
        f"rows show what each K ASKS for)"
    )
    _print_table(_WALK_HEADERS, [_walk_cells(row) for row in rows])

    print()
    print("== derivation ==")
    table: list[list[str]] = []
    derivation: list[dict[str, object]] = []
    for hogs in args.hogs:
        rebuild = rebuilds[hogs]
        baseline = baselines[hogs]
        chosen = _pick_cost_ratio(per_level[hogs], args.max_demanded_ttl, args.rebuild_tolerance)
        entry: dict[str, object] = {
            "hogs": hogs,
            "rebuild_median_ms": rebuild.median_ms,
            "baseline_rebuilds": baseline.rebuilds,
            "baseline_refusals": baseline.refusals,
        }
        if chosen is None:
            table.append(
                [
                    str(hogs),
                    f"{rebuild.median_ms}",
                    f"{baseline.rebuilds}",
                    f"{baseline.refusals}",
                    "no clean K",
                    "-",
                    "-",
                    "-",
                ]
            )
            entry["chosen_k"] = None
            derivation.append(entry)
            continue
        table.append(
            [
                str(hogs),
                f"{rebuild.median_ms}",
                f"{baseline.rebuilds}",
                f"{baseline.refusals}",
                f"{chosen.cost_ratio:.0f}" if chosen.cost_ratio else "-",
                f"{chosen.ttl_max_secs}",
                f"{chosen.rebuilds}",
                f"{chosen.refusals}",
            ]
        )
        entry.update(
            {
                "chosen_k": chosen.cost_ratio,
                "chosen_ttl_high_secs": chosen.ttl_max_secs,
                "chosen_rebuilds": chosen.rebuilds,
                "chosen_refusals": chosen.refusals,
            }
        )
        derivation.append(entry)
    _print_table(
        [
            "hogs",
            "rebuild median ms",
            "fixed: rebuilds",
            "fixed: REFUSED",
            "chosen K",
            "its ttl s",
            "K: rebuilds",
            "K: REFUSED",
        ],
        table,
    )

    ks = [_as_float(entry["chosen_k"]) for entry in derivation if entry.get("chosen_k")]
    ttls = [
        _as_float(entry["chosen_ttl_high_secs"])
        for entry in derivation
        if entry.get("chosen_ttl_high_secs")
    ]
    print()
    if ks:
        print(f"smallest K that serves every measurable load level: {max(ks):.0f}")
        print(f"largest expiry that K asked for: {max(ttls)}s -- the cap has to reach it")
    else:
        print("no load level produced a clean K; the rebuild itself is over budget")
    unfixable = [entry["hogs"] for entry in derivation if not entry.get("chosen_k")]
    if unfixable:
        print(
            "load levels NO expiry can fix (one cold rebuild is already at or over "
            f"the {gate._PATH_RESOLVE_REBUILD_TIMEOUT_SECS}s rebuild budget, so the "
            f"first refusal happens before any cache exists): {unfixable}"
        )
    return {
        "rebuild": [asdict(row) for row in rebuilds.values()],
        "walk": [asdict(row) for row in rows],
        "derivation": derivation,
    }


def _csv_ints(raw: str) -> list[int]:
    return [int(part) for part in raw.split(",") if part.strip()]


def _csv_floats(raw: str) -> list[float]:
    return [float(part) for part in raw.split(",") if part.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or "")
    parser.add_argument("mode", choices=("rebuild", "ksweep"))
    parser.add_argument(
        "--hogs",
        type=_csv_ints,
        default=list(DEFAULT_HOGS),
        help="comma-separated sibling-thread counts (default: %(default)s)",
    )
    parser.add_argument(
        "--k",
        "--ks",
        dest="ks",
        type=_csv_floats,
        default=list(DEFAULT_KS),
        help="comma-separated cost ratios to walk (default: %(default)s)",
    )
    parser.add_argument(
        "--cap-probe",
        dest="cap_probe",
        type=float,
        default=600.0,
        help="cap the law runs under during the sweep; keep it far above any "
        "candidate cap so each row shows what its K asks for rather than what a "
        "cap would allow",
    )
    parser.add_argument(
        "--max-demanded-ttl",
        dest="max_demanded_ttl",
        type=float,
        default=60.0,
        help="reject a K that asks for an expiry longer than this; the one policy "
        "input the measurement cannot supply (default: %(default)s)",
    )
    parser.add_argument(
        "--rebuild-tolerance",
        dest="rebuild_tolerance",
        type=float,
        default=2.0,
        help="a K qualifies when its rebuild count is within this multiple of the "
        "fewest at the same load level (default: %(default)s)",
    )
    parser.add_argument("--duration", type=float, default=15.0, help="walk seconds per cell")
    parser.add_argument("--samples", type=int, default=9, help="timed rebuilds per cell")
    parser.add_argument("--json", dest="json_path", help="also write raw rows here")
    args = parser.parse_args(argv)

    provenance = _provenance()
    print("== provenance ==")
    for key, value in provenance.items():
        print(f"{key}: {value}")
    print()

    handler = {"rebuild": _cmd_rebuild, "ksweep": _cmd_ksweep}[args.mode]
    payload = handler(args)

    if args.json_path:
        Path(args.json_path).write_text(
            json.dumps({"provenance": provenance, **payload}, indent=2) + "\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
