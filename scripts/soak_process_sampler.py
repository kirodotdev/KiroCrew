#!/usr/bin/env python3
"""Sample one gateway instance's process population on an interval, as TSV.

The soak gate asks a question no single measurement can answer: over hours, does
this instance's process count or memory grow without bound? So this script takes
one reading per interval and appends one tab-separated row, which is the shape
that goes straight into a plot or a ``sort``/``awk`` one-liner without parsing.

One row per sample, columns in this order::

    ts  runtimes  owned_alive  owned_dead  unowned_alive  stubs  scopes  rss_mb  registry

``ts``
    Seconds since the epoch, integer, so a series sorts and subtracts directly.
``runtimes``
    Live agent-runtime ROOTS -- one per session backend. The number that should
    track how many sessions are open.
``owned_alive`` / ``owned_dead`` / ``unowned_alive``
    The three reconciliation populations (see ``e2e.process_inventory``).
    ``unowned_alive`` climbing over a soak is the leak; ``owned_dead`` climbing
    is the registry failing to forget.
``stubs``
    Live managed MCP stub processes.
``scopes``
    Scope units under the instance's slice. A scope that outlives its processes
    still holds a cgroup, so this can grow while pid counts look flat.
``rss_mb``
    Summed resident set size of every live process in the instance's slice, in
    MiB. Summed rather than sampled from the gateway alone because the growth
    this gate looks for is in the agent tree, not the supervisor.
``registry``
    Total registry entries. Grows monotonically if nothing is ever forgotten,
    which is a leak of bookkeeping even when no process survives.

Usage::

    scripts/soak_process_sampler.py --home <data home> --interval 600 --out soak.tsv

Read-only. It opens ``/proc`` and the cgroup tree and appends to its output
file; it never signals a process, writes into the instance's home, or asks
systemd for anything. Safe to run against a live gateway for as long as the
soak lasts.

``--once`` takes a single sample and exits, which is what a smoke check or a
cron wrapper wants. Without it the script samples forever and flushes after
every row, so a soak that is killed mid-run keeps every row it already wrote.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# The sampler ships beside the test helper that owns the reconciliation, and
# imports it rather than restating it: two implementations of "who owns this
# pid" would drift, and the soak numbers would stop matching the gate's.
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT / "test"))

from e2e.process_inventory import (  # noqa: E402  (path set above)
    SESSION_PID_FILE,
    Inventory,
    assert_slice_contract,
    inventory,
    pid_alive,
    read_registry,
    start_token,
)

COLUMNS = (
    "ts",
    "runtimes",
    "owned_alive",
    "owned_dead",
    "unowned_alive",
    "stubs",
    "scopes",
    "rss_mb",
    "registry",
)

#: Default gap between samples. Ten minutes: long enough that a 12-hour soak is
#: ~72 rows rather than thousands, short enough to place a step change.
DEFAULT_INTERVAL_SECS = 600.0


def rss_mb(pids: tuple[int, ...], *, proc_root: Path = Path("/proc")) -> float:
    """Summed resident set size of *pids*, in MiB.

    Read from ``statm`` field 2 (resident pages) times the page size. A pid that
    exits mid-walk contributes nothing rather than raising: the sample is a
    snapshot of a moving population, and a missing reading is one process, not a
    failed sample.
    """
    page = os.sysconf("SC_PAGE_SIZE")
    total = 0
    for pid in pids:
        try:
            fields = (proc_root / str(pid) / "statm").read_text(encoding="utf-8").split()
        except OSError:
            continue
        if len(fields) < 2 or not fields[1].isdigit():
            continue
        total += int(fields[1]) * page
    return round(total / (1024 * 1024), 1)


def runtime_roots(home: Path) -> int:
    """Live agent-runtime roots, as the registry's session file records them.

    A recorded pid whose current start identity differs from the recorded one is
    a STRANGER that inherited the number, not a live runtime. Counting it would
    make the soak series report runtimes that do not exist, and on a long soak
    that is exactly the direction that hides a leak: the count looks healthy
    while the real population drifts.
    """
    live = 0
    for entry in read_registry(home):
        if entry.source != SESSION_PID_FILE or not pid_alive(entry.pid):
            continue
        if entry.token is not None:
            current = start_token(entry.pid)
            if current is not None and current != entry.token:
                continue
        live += 1
    return live


def sample(home: Path, *, ignore: frozenset[int]) -> tuple[Inventory, dict[str, object]]:
    """One reading. Returns the inventory and the row to write."""
    inv = inventory(home, ignore_pids=ignore)
    counts = inv.counts()
    row: dict[str, object] = {
        "ts": int(time.time()),
        "runtimes": runtime_roots(home),
        "owned_alive": counts["owned_alive"],
        "owned_dead": counts["owned_dead"],
        "unowned_alive": counts["unowned_alive"],
        "stubs": counts["stubs"],
        "scopes": counts["scopes"],
        "rss_mb": rss_mb(inv.live_pids),
        "registry": counts["registry_entries"],
    }
    return inv, row


def format_row(row: dict[str, object]) -> str:
    return "\t".join(str(row[name]) for name in COLUMNS)


def default_home() -> Path | None:
    """The data home the PRODUCT would resolve, or ``None`` if it cannot be asked.

    Resolved through ``kiro_crew.config.paths.peek_data_home`` rather than
    rebuilt from a literal. The top-level ``~/.kirocrew`` path is the LEGACY home;
    the current default nests under kiro-cli's base, so a hand-rolled default
    would point a stock run at a directory nothing writes to and report an empty
    machine forever -- the same false-clean this harness exists to prevent.

    ``peek_data_home`` is the read-only resolver: it reports the home without
    creating it, so a sampler pointed at the wrong place fails the existence
    check below instead of silently creating an empty directory to measure.
    """
    try:
        from kiro_crew.config.paths import peek_data_home
    except ImportError:
        return None
    try:
        return Path(peek_data_home())
    except Exception:
        return None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__ or "", add_help=True)
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help=(
            "the gateway instance's data home. Defaults to KIROCREW_HOME, else the "
            "home the product itself resolves. Required when neither can be resolved."
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL_SECS,
        help=f"seconds between samples (default: {DEFAULT_INTERVAL_SECS:.0f})",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="append rows to this TSV file as well as stdout",
    )
    parser.add_argument("--once", action="store_true", help="take one sample and exit")
    parser.add_argument(
        "--ignore-pid",
        type=int,
        action="append",
        default=[],
        help="a pid that is not agent work (the gateway itself); repeatable",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="also print the full inventory for each sample to stderr",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # Everything this samples is Linux-only: cgroup v2 for slice membership,
    # /proc for liveness and RSS, and a page size from sysconf. Refuse by name
    # rather than crashing part-way through the first sample on a host that has
    # none of them.
    if sys.platform != "linux":
        print(
            f"process sampling needs cgroup v2 and /proc; this host is {sys.platform}",
            file=sys.stderr,
        )
        return 2
    home = args.home
    if home is None:
        env_home = os.environ.get("KIROCREW_HOME")
        home = Path(env_home) if env_home else default_home()
    if home is None:
        print(
            "could not resolve a data home: pass --home, or set KIROCREW_HOME, or run "
            "where kiro_crew is importable",
            file=sys.stderr,
        )
        return 2
    home = home.expanduser()
    if not home.is_dir():
        print(f"data home does not exist: {home}", file=sys.stderr)
        return 2
    if args.interval <= 0:
        print("--interval must be positive", file=sys.stderr)
        return 2
    # The one detector for a sampler that would read all-zero forever. The slice
    # name is derived from the data home, and the product does not resolve its
    # DEFAULT home while this module resolves what it is given, so on a host with
    # a symlinked home component the two digests can differ -- and every
    # population would then read zero for the whole soak with nothing to say why.
    try:
        assert_slice_contract(home, _REPO_ROOT / "src")
    except AssertionError as exc:
        print(f"refusing to sample: {exc}", file=sys.stderr)
        return 2

    ignore = frozenset(set(args.ignore_pid) | {os.getpid()})
    handle = None
    if args.out is not None:
        fresh = not args.out.exists() or args.out.stat().st_size == 0
        handle = args.out.open("a", encoding="utf-8")
        if fresh:
            handle.write("\t".join(COLUMNS) + "\n")
            handle.flush()
    # The header goes to stdout every run, so a piped sample is self-describing
    # even when no file is kept.
    print("\t".join(COLUMNS), flush=True)
    try:
        while True:
            inv, row = sample(home, ignore=ignore)
            line = format_row(row)
            print(line, flush=True)
            if args.verbose:
                print(inv.render(), file=sys.stderr, flush=True)
            if handle is not None:
                handle.write(line + "\n")
                # Flushed per row: a soak that is killed at hour 11 must keep
                # every row it already took.
                handle.flush()
            if args.once:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        if handle is not None:
            handle.close()


if __name__ == "__main__":
    raise SystemExit(main())
