#!/usr/bin/env python3
"""Main-red watch: what the push runs of main before this one say about a red run.

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

A watcher run that the API rate limit ended, or that a newer one replaced in
the queue, records nothing. So every red run that names a test asks which red
runs before it no watcher recorded, and replays them under the watcher's own
rules:

    unfinished RUNS.json --run-number N --prev-body BODY
                             print `<id> TAB <url>`, oldest first, of the red
                             verdict runs directly before run number N that the
                             record in BODY (the open issue's, or empty) does
                             not hold: back to the first green run, to the run
                             the record holds, or MAX_UNFINISHED runs
    replay RUNS.json PASTDIR --run-number N --prev-body BODY --out-body OUT
                             read `PASTDIR/<id>/` (per-job files) for each of
                             those runs and write to OUT the record the watcher
                             would have left; print what was counted. Exit 1,
                             writing nothing, when no run adds to the record

A replayed run counts as it would have live: with a record open, when it names
a failing test; with none, when a test is red in two of its jobs, or its jobs
share a test file with the verdict run before it.

Nothing is stored between runs: the record is the open issue's, and each run
is read again from the API.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

VERDICTS = ("success", "failure", "timed_out")
MAX_FILES_SHOWN = 3
MAX_UNFINISHED = 5
_FULL_SHA = re.compile(r"[0-9a-f]{40}")


def _sibling() -> Any:
    path = Path(__file__).resolve().with_name("main_red_nodeids.py")
    spec = importlib.util.spec_from_file_location("main_red_nodeids", str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


watch = _sibling()


def verdict_runs(listing: Any, run_number: int) -> list[dict[str, Any]]:
    """Push runs of main before ``run_number`` that reached a verdict, newest first.

    ``listing`` is the body of the workflow-runs API. A cancelled run is a
    pending one that a later push replaced, so it says nothing about the tests.
    """
    rows = listing.get("workflow_runs") if isinstance(listing, dict) else None
    found: list[dict[str, Any]] = []
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
        found.append(row)
    return sorted(found, key=lambda row: int(row["run_number"]), reverse=True)


def previous_verdict(listing: Any, run_number: int) -> dict[str, Any] | None:
    """The newest run before ``run_number`` that reached a verdict, or ``None``."""
    earlier = verdict_runs(listing, run_number)
    return earlier[0] if earlier else None


def unfinished(
    listing: Any, run_number: int, state: dict[str, Any] | None
) -> tuple[list[dict[str, Any]], bool]:
    """Red verdict runs directly before ``run_number`` that ``state`` does not hold.

    Oldest first. The walk back ends at a green run (a streak starts after
    it), at the last run the record holds, or after ``MAX_UNFINISHED`` runs; the
    flag says the cap ended it.
    """
    last = int(state["last_run_id"]) if state is not None else -1
    found: list[dict[str, Any]] = []
    for row in verdict_runs(listing, run_number):
        if row["conclusion"] == "success" or int(row["id"]) <= last:
            break
        if len(found) == MAX_UNFINISHED:
            return found[::-1], True
        found.append(row)
    return found[::-1], False


def run_url(repo: str, row: dict[str, Any]) -> str:
    return f"https://github.com/{repo}/actions/runs/{row['id']}"


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


def replay(
    state: dict[str, Any] | None,
    runs: list[tuple[dict[str, Any], list[dict[str, str]]]],
    repo: str,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """The record after ``runs`` (oldest first, each with its per-job failures).

    Returns it with the runs that added to it. With no record open, a run adds
    one only under the rules by which a live run opens an issue.
    """
    counted: list[dict[str, Any]] = []
    before: list[dict[str, str]] | None = None
    for row, per_job in runs:
        failures = watch.merge_failures(per_job)
        opens = watch.repeated(per_job) or (before is not None and same_files(per_job, before))
        if failures and (state is not None or opens):
            sha = str(row.get("head_sha") or "")
            after = watch.next_state(state, failures, sha, int(row["id"]), run_url(repo, row))
            if after is not None:
                state = after
                counted.append(row)
        before = per_job
    return state, counted


def cmd_previous_verdict(args: argparse.Namespace) -> int:
    try:
        listing = json.loads(Path(args.runs).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return 1
    run = previous_verdict(listing, args.run_number)
    if run is None:
        return 1
    print(f"{run['id']}\t{run['conclusion']}\t{run_url(args.repo, run)}")
    return 0


def _unfinished(args: argparse.Namespace) -> tuple[Any, list[dict[str, Any]], bool]:
    """The record in ``--prev-body`` and the runs before this one it does not hold."""
    listing = json.loads(Path(args.runs).read_text(encoding="utf-8", errors="replace"))
    state = watch.parse_state(watch._read_text(args.prev_body))
    rows, capped = unfinished(listing, args.run_number, state)
    return state, rows, capped


def cmd_unfinished(args: argparse.Namespace) -> int:
    try:
        _, rows, _ = _unfinished(args)
    except (OSError, ValueError):
        return 1
    for row in rows:
        print(f"{row['id']}\t{run_url(args.repo, row)}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        state, rows, capped = _unfinished(args)
    except (OSError, ValueError):
        return 1
    if not all(_FULL_SHA.fullmatch(str(row.get("head_sha") or "")) for row in rows):
        return 1
    runs = [(row, watch._per_job(str(Path(args.pastdir) / str(row["id"])))) for row in rows]
    state, counted = replay(state, runs, args.repo)
    if state is None or not counted:
        return 1
    body = f"<!-- {watch.STATE_MARKER} {watch.dump_state(state)} -->\n"
    if watch.parse_state(body) is None:
        return 1
    Path(args.out_body).write_text(body, encoding="utf-8")
    urls = ", ".join(run_url(args.repo, row) for row in counted)
    cap = f" Only the {MAX_UNFINISHED} runs before this one were read." if capped else ""
    print(
        f"{len(counted)} earlier red run(s) of main that no watcher run recorded"
        f" are counted now ({urls}); first red is {state['first_sha']}.{cap}"
    )
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
    for name, run in (("unfinished", cmd_unfinished), ("replay", cmd_replay)):
        past = sub.add_parser(name)
        past.add_argument("runs")
        if name == "replay":
            past.add_argument("pastdir")
            past.add_argument("--out-body", required=True)
        past.add_argument("--run-number", type=int, required=True)
        past.add_argument("--prev-body", required=True)
        past.add_argument("--repo", required=True)
        past.set_defaults(run=run)
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
