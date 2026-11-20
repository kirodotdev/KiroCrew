#!/usr/bin/env python3
"""Main-red watch: is a failure seen in one job the same one the previous run saw?

A test red in one job of one run is as often a flake as a break, so
`main-red-watch.yml` opens no issue for it. A break that shows on one leg only
(one operating system, one shard) is red in one job of every run, and stayed
unfiled however long it lasted. When a run is red in one job only and no issue
is open, the workflow reads the previous push run of `CI` on `main` that
reached a verdict and asks this script whether both runs are red on the same
test file.

    previous-verdict RUNS.json --run-number N
                             print `<id> TAB <conclusion> TAB <url>` of the newest
                             run before run number N that ended `success`,
                             `failure` or `timed_out`; exit 1 when there is none
    same-files DIR PREVDIR   exit 0 and print the test files red in both
                             directories of per-job files; else exit 1

The unit is the test file, not the node id: a break often fails a different
test of one file on each run (another parameter, another method of one class),
and a node id that differs would hide it.

Nothing is stored between runs. The previous run is read again each time, and
only on a run that is red in one job with no issue open.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

VERDICTS = ("success", "failure", "timed_out")
MAX_FILES_SHOWN = 3


def _sibling() -> Any:
    path = Path(__file__).resolve().with_name("main_red_nodeids.py")
    spec = importlib.util.spec_from_file_location("main_red_nodeids", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = _sibling()


def previous_verdict(listing: Any, run_number: int) -> dict[str, Any] | None:
    """The newest run before ``run_number`` that reached a verdict, or ``None``.

    ``listing`` is the body of the workflow-runs API. A cancelled run is a
    pending one that a later push replaced, so it says nothing about the tests.
    """
    rows = listing.get("workflow_runs") if isinstance(listing, dict) else None
    best: dict[str, Any] | None = None
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        number, run_id = row.get("run_number"), row.get("id")
        if not isinstance(number, int) or not isinstance(run_id, int) or number >= run_number:
            continue
        if row.get("conclusion") not in VERDICTS:
            continue
        if row.get("event") != "push" or row.get("head_branch") != "main":
            continue
        if best is None or number > best["run_number"]:
            best = row
    return best


def test_file(nodeid: str) -> str:
    """`test/a.py::TestX::test_y[p]` is of the file `test/a.py`."""
    return nodeid.split("::", 1)[0]


def same_files(current: list[dict[str, str]], previous: list[dict[str, str]]) -> list[str]:
    """Test files with a failing test in both runs, in the current run's order."""
    before = {test_file(nodeid) for failures in previous for nodeid in failures}
    seen: list[str] = []
    for failures in current:
        for nodeid in failures:
            name = test_file(nodeid)
            if name in before and name not in seen:
                seen.append(name)
    return seen


def cmd_previous_verdict(args: argparse.Namespace) -> int:
    try:
        listing = json.loads(Path(args.runs).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return 1
    run = previous_verdict(listing, args.run_number)
    if run is None:
        return 1
    url = f"https://github.com/{args.repo}/actions/runs/{run['id']}"
    print(f"{run['id']}\t{run['conclusion']}\t{url}")
    return 0


def cmd_same_files(args: argparse.Namespace) -> int:
    files = same_files(watch._per_job(args.logdir), watch._per_job(args.prevdir))
    if not files:
        return 1
    shown = ", ".join(watch.clean(name, 200) for name in files[:MAX_FILES_SHOWN])
    more = f" and {len(files) - MAX_FILES_SHOWN} more" if len(files) > MAX_FILES_SHOWN else ""
    print(shown + more)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    verdict = sub.add_parser("previous-verdict")
    verdict.add_argument("runs")
    verdict.add_argument("--run-number", type=int, required=True)
    verdict.add_argument("--repo", required=True)
    verdict.set_defaults(run=cmd_previous_verdict)
    same = sub.add_parser("same-files")
    same.add_argument("logdir")
    same.add_argument("prevdir")
    same.set_defaults(run=cmd_same_files)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.run(args))


if __name__ == "__main__":
    sys.exit(main())
