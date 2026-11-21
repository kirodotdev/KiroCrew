#!/usr/bin/env python3
"""Miss ledger for diff-based test selection: shadow verdicts joined with full-run failures.

Reads only: `gh` (completed pull_request `CI` runs, each run's verdict artifact,
its Linux `Backend Tests` jobs and their logs, the open main-red issue). Appends
to a JSONL ledger, writes proposed tier rates as JSON and a markdown summary. It
never edits the checked-in rates file: a person moves the proposal in by PR.

A verdict is the scorer's JSON for one PR run (`schema` 1: `verdict`, `tiers`,
`would_skip`), read from the run's `--artifact`, or from `<--verdict-dir>/<run
id>.json` when it was scored offline. A failing test file the verdict lists in
`would_skip` is a MISS.

Only a run that proves what it ran is an observation: every Linux backend shard
ended in success or failure, ran the full suite (the reduced and leaf scopes
skip the `FULL_SUITE_STEP` step), and every failed shard names a failing test.
Any other run is recorded as excluded, with the reason, and fits nothing.

Ledger rows, one JSON object per line, keyed on run id and attempt. A pass
appends only what the ledger does not hold; a rerun appends a fresh observation
of the whole run, and the report reads each run's newest attempt only:

    {"kind": "run", "run", "attempt", "source", "fork", "pr", "head", "date",
     "verdict", "tiers": {tier: files}}
    {"kind": "failure", <the same seven>, "file", "tier", "miss", "rerun_green", "main_red"}
    {"kind": "skip", <the same seven>, "reason"}

Rows are joined and deduplicated only on values every later pass reads the same:
the run id and attempt, the run's creation `date`, and `source`, which is `<head
repository id>:<head branch>` as the run recorded them. A run keeps both after
its PR merges and after the branch is deleted or the repository renamed. `pr` is
the number GitHub lists only while the PR is open; it is kept for the summary
and never compared. Two PRs opened one after the other from one branch share a
`source`, so a failure they have in common is not marked `shared`: that keeps a
rate up, never down. A run whose head repository no longer exists has no
`source` and is excluded.

A failure is not caused by the diff when the shard that failed it ended green on
a rerun (`rerun_green`), the watcher's open main-red issue named its file when
the row was recorded and the run is no older than the first red run on main
(`main_red`), or the same file failed under another `source` within
`--window-days` (`shared`, decided over the ledger at report time). Those rows
stay in the ledger and are reported apart. An issue that closed before the pass
ran clears nothing, so its failures count as caused by the diff.

Each tier's rate is its diff-caused failing files over the files `select`
verdicts placed in it, with the upper bound of the two-sided 95% Wilson
interval. A test file the Linux suite never collects still counts as a trial. A

Trust: what a run from another repository (a fork) says is never evidence. Its
verdict and its logs are written by code the fork controls, so its rows are
recorded and its misses are listed, but the fit, the `shared` evidence, the days
of data and the withheld-rates check read same-repository rows only. No fork row
can change a number this script proposes. A same-repository run is trusted as
far as its author, who can already push to the repository. The main-red issue is
trusted only when the watcher wrote it.

Every row is checked with `valid_row` before it is written, the same check a
later pass applies when it reads the ledger; a run whose rows fail it is
recorded as excluded. The ledger is replaced through a temporary file, so an
interrupted pass leaves the old ledger whole. A
failing file a `select` verdict gave no tier cannot be fitted: the summary lists
it first and no rates are written.

Windows, on unless `--no-windows`: a run with a verdict is read again for its
`Backend Tests (Windows)` shards, under the same full-suite rule, and push runs
to main and `release/**` are listed for them alone. The Linux shards need not
have run the full suite (once Linux selection is enforced they will not), only
ended in a pass or a fail naming each failing test; a file a selected Linux run
skipped then counts as not failing on Linux, which keeps a Windows rate up,
never down. A file that failed on Windows and not on Linux in the same run is a
Windows-only failure,
labelled `os`, `timing` or `unclear` from its messages; outside the verdict's
`windows.run` while `windows.verdict` is `select`, it is a Windows MISS:

    {"kind": "windows_run", <the same seven>, "os": "windows", "verdict", "trial", "files"}
    {"kind": "windows_failure", <the same seven>, "os": "windows", "file", "in_run",
     "label", "miss", "rerun_green", "main_red"}
    {"kind": "windows_skip", <the same seven>, "os": "windows", "reason"}

`trial` is `push` for a push run, else the block's `source` (`shadow` when it
names none). `shared` reads Linux and Windows failures alike; every Windows
number is reported apart from the Linux ones, ending in one JSON status line
(`windows_status`) that turns Windows selection off.

Exit codes: 0 the proposal was written; 2 nothing was recorded (a ledger line of
the wrong shape, or `gh` failed before the first row); 3 the rates are withheld.
1 is left to the interpreter, so an error this script did not foresee is never
read as one of those.

Verdicts, logs and the ledger are data: parsed as JSON, checked field by field,
size-capped, and every table cell is cut to a fixed character set.
"""

from __future__ import annotations

import argparse
import functools
import heapq
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_main_red_skip as red_skip  # noqa: E402  (path set immediately above)
import ci_test_map_shadow as shadow  # noqa: E402

# ci.yml's `backend-test` job runs this step only when `steps.scope.outputs.reduced`
# is 'false'; `test_the_full_suite_step_is_the_one_ci_gates_on_scope` pins that.
FULL_SUITE_STEP = "Prewarm assertion rewrites"
VERDICT_SCHEMA = 1
MAX_VERDICT_BYTES = 8 * 1024 * 1024
# GitHub lists at most 1,000 runs for one query; `--since` narrows the query.
MAX_RUN_PAGES = 10
Z95 = 1.959964
NO_VERDICT = "no verdict artifact"
BAD_VERDICT = "invalid verdict"
NO_SOURCE = "its head repository no longer exists"
BAD_ROW = "its rows would not read back from the ledger"
# Linux exclusions decided before any job is read: the run has no Windows half either.
_NO_WINDOWS_HALF = (NO_VERDICT, BAD_VERDICT, NO_SOURCE, BAD_ROW)
EXIT_NOT_RECORDED = 2
EXIT_WITHHELD = 3
# ci.yml's `backend-test-windows` matrix job, `Backend Tests (Windows) (3)`.
WINDOWS_BACKEND_JOB = re.compile(r"Backend Tests \(Windows\)(?: \(\d+\))?")
LANES = {"Linux": shadow.LINUX_BACKEND_JOB, "Windows": WINDOWS_BACKEND_JOB}
NO_WINDOWS_BLOCK = "the verdict has no windows block"
# A push runs the full Windows suite and selects nothing: a trial that cannot miss.
PUSH_BLOCK = {"verdict": "full", "run": frozenset(), "source": "push"}
TRIAL_SOURCES = ("shadow", "sample", "label", "push")
WINDOWS_LABELS = ("os", "timing", "unclear")
# Windows selection stays on while fewer than WINDOWS_MISS_BAR diff-caused
# Windows misses fall in the last WINDOWS_STATUS_DAYS days, and only with at
# least WINDOWS_MIN_TRIALS full-Windows trials there to say so.
WINDOWS_STATUS_DAYS = 14
WINDOWS_MISS_BAR = 2
WINDOWS_MIN_TRIALS = 20
_PUSH_BRANCH = re.compile(r"main|release/.+")
_OS_FAILURE = re.compile(
    r"WinError|PermissionError|\b[A-Za-z]:[\\/]|\w\\{1,2}[\w.-]+\\|\\r\\n|\bCRLF\b|\.cmd\b"
    r"|has no attribute '(?:getuid|geteuid|getgid|getegid|getpgid|setsid|setpgrp|fork"
    r"|killpg|chown|uname|sync|getloadavg|SIGKILL|SIGHUP|SIGALRM)'",
    re.IGNORECASE,
)
_TIMING_FAILURE = re.compile(
    r"timeout|timed out|node down|not properly terminated|worker .{0,40}crashed", re.IGNORECASE
)

_TIER = re.compile(r"[A-Za-z0-9_-]{1,20}")
_FILE = re.compile(r"[A-Za-z0-9_./-]{1,300}\.py")
_SHA = re.compile(r"[0-9a-f]{40}")
_DAY = re.compile(r"\d{4}-\d\d-\d\d")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
_ARTIFACT = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_NOT_CELL = re.compile(r"[^A-Za-z0-9 _./:#-]")

Row = dict[str, Any]


def cell(value: object) -> str:
    """One markdown table cell: a fixed character set, so no cell can add markup."""
    return _NOT_CELL.sub("?", str(value))[:120]


def _files(value: object) -> set[str] | None:
    if not isinstance(value, list):
        return None
    if not all(isinstance(item, str) and _FILE.fullmatch(item) for item in value):
        return None
    return set(value)


def parse_verdict(text: str) -> Row | None:
    """A verdict's `verdict`, `tiers` and `would_skip`, None unless each has its fixed shape."""
    try:
        doc = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or doc.get("schema") != VERDICT_SCHEMA:
        return None
    raw, skip = doc.get("tiers"), _files(doc.get("would_skip"))
    if doc.get("verdict") not in ("select", "full") or not isinstance(raw, dict) or skip is None:
        return None
    tiers: dict[str, set[str]] = {}
    for tier, listed in raw.items():
        files = _files(listed)
        if not isinstance(tier, str) or not _TIER.fullmatch(tier) or files is None:
            return None
        tiers[tier] = files
    windows = doc.get("windows")
    if windows is not None:
        windows = parse_windows(windows)
        if windows is None:
            return None
    return {"verdict": doc["verdict"], "tiers": tiers, "would_skip": skip, "windows": windows}


def parse_windows(block: object) -> Row | None:
    """A verdict's `windows` block: `verdict`, `run` (may be absent under `full`), `source`."""
    if not isinstance(block, dict) or block.get("verdict") not in ("select", "full"):
        return None
    listed = block.get("run", [] if block["verdict"] == "full" else None)
    run, source = _files(listed), block.get("source", "shadow")
    if run is None or source not in TRIAL_SOURCES or source == "push":
        return None
    return {"verdict": block["verdict"], "run": run, "source": source}


def _capped_text(path: Path) -> str:
    """The file's text, or "" (never a valid verdict) for a link, a non-file or an oversized one."""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_VERDICT_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def verdict_text(run_id: int, args: argparse.Namespace, root: Path) -> str | None:
    """The run's verdict JSON text, None when the run has none. Raises when `gh` fails."""
    if args.verdict_dir:
        path = Path(args.verdict_dir) / f"{run_id}.json"
        return _capped_text(path) if path.exists() else None
    listed = shadow._gh_json(
        [
            "api",
            f"repos/{shadow.REPO}/actions/runs/{run_id}/artifacts?name={args.artifact}",
            "--jq",
            ".artifacts",
        ],
        root,
    )
    if not any(isinstance(a, dict) and not a.get("expired") for a in listed):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        shadow._run(
            ["gh", "run", "download", str(run_id), "-R", shadow.REPO]
            + ["-n", args.artifact, "-D", tmp],
            root,
        )
        found = sorted(Path(tmp).rglob("*.json"))
        return _capped_text(found[0]) if len(found) == 1 else ""


def list_runs(root: Path, since: str | None, event: str = "pull_request") -> Iterator[Row]:
    """Completed `event` `CI` runs, newest first, a page at a time until the listing ends."""
    created = f"&created=%3E%3D{since}" if since else ""
    for page in range(1, MAX_RUN_PAGES + 1):
        runs = shadow._gh_json(
            [
                "api",
                f"repos/{shadow.REPO}/actions/workflows/ci.yml/runs?event={event}"
                f"&status=completed&per_page=100&page={page}{created}",
                "--jq",
                ".workflow_runs",
            ],
            root,
        )
        if not runs:
            return
        yield from runs


def ran_full_suite(job: Row) -> bool:
    steps = job.get("steps") or []
    return any(
        str(step.get("name", "")).startswith(FULL_SUITE_STEP)
        and step.get("conclusion") not in (None, "skipped")
        for step in steps
    )


Listing = Callable[[int], list[Row] | None]


def read_run(
    run: Row,
    root: Path,
    parse: Callable[[str], dict[str, str]],
    listing: Listing,
    lane: str = "Linux",
    full: bool = True,
) -> dict[str, bool] | str | None:
    """`{failing file: its shard ended green on a rerun}` for a full-suite run of one lane.

    `listing` gives an attempt's jobs. A string says why the run is not an
    observation; None means `gh` could not read it, so a later pass tries again.
    `full=False` drops the full-suite rule alone: what a selected run failed.
    """
    final: dict[str, Row] = {}
    named: dict[int, set[str]] = {}
    failed: dict[str, set[str]] = {}
    for attempt in range(1, int(run.get("run_attempt") or 1) + 1):
        jobs = listing(attempt)
        if jobs is None:
            return None
        for job in jobs:
            if not LANES[lane].fullmatch(str(job.get("name"))):
                continue
            final[job["name"]] = job
            if job["conclusion"] != "failure":
                continue
            if job["id"] not in named:
                failures = shadow.job_failures(job, root, parse)
                if failures is None:
                    return None
                named[job["id"]] = set(shadow.failing_test_files(failures))
            failed.setdefault(job["name"], set()).update(named[job["id"]])
    if not final:
        return f"no {lane} backend shard ran"
    for job in final.values():
        if job["conclusion"] not in ("success", "failure"):
            return "a backend shard neither passed nor failed"
        if full and not ran_full_suite(job):
            return "a backend shard did not run the full suite"
        if job["conclusion"] == "failure" and not named[job["id"]]:
            return "a failed backend shard names no failing test"
    failing: dict[str, bool] = {}
    for name, files in failed.items():
        green = final[name]["conclusion"] == "success"
        for test_file in files:
            failing[test_file] = failing.get(test_file, True) and green
    return failing


def main_red_files(root: Path) -> tuple[int, set[str]]:
    """`(first red run on main, the test files named)` from the watcher's open main-red issue.

    Read with the watcher's own `pick_issue` and `parse_state`. With no such issue
    the set is empty.
    """
    issues = shadow._gh_json(
        ["api", f"repos/{shadow.REPO}/issues?state=open&labels={red_skip.LABEL}&per_page=100"],
        root,
    )
    watch = shadow.watch_module()
    issue = watch.pick_issue(red_skip.as_watcher_rows(issues if isinstance(issues, list) else []))
    state = watch.parse_state(str(issue.get("body") or "")) if issue else None
    if not state:
        return 0, set()
    return int(state["first_run_id"]), set(shadow.failing_test_files(state["failures"]))


def _dict(value: object) -> Row:
    return value if isinstance(value, dict) else {}


def run_key(run: Row) -> Row:
    """The fields every row of one run attempt carries, as GitHub gave them.

    `source` is None with no head repository. Nothing here is checked: `collect`
    passes the result through `valid_row` before it uses it.
    """
    head = _dict(run.get("head_repository"))
    prs = run.get("pull_requests")
    number = _dict(prs[0]).get("number") if isinstance(prs, list) and prs else None
    return {
        "run": run.get("id"),
        "attempt": run.get("run_attempt", 1),
        "source": f"{head['id']}:{run.get('head_branch')}" if _is_int(head.get("id")) else None,
        "fork": head.get("id") != _dict(run.get("repository")).get("id"),
        "pr": number if _is_int(number) else None,
        "head": run.get("head_sha"),
        "date": run.get("created_at"),
    }


def run_rows(key: Row, verdict: Row, failing: dict[str, bool], main_red: set[str]) -> list[Row]:
    tiers: dict[str, set[str]] = verdict["tiers"]
    rows: list[Row] = [
        {
            "kind": "run",
            **key,
            "verdict": verdict["verdict"],
            "tiers": {tier: len(files) for tier, files in sorted(tiers.items())},
        }
    ]
    for test_file in sorted(failing):
        rows.append(
            {
                "kind": "failure",
                **key,
                "file": test_file,
                "tier": next((t for t in sorted(tiers) if test_file in tiers[t]), None),
                "miss": test_file in verdict["would_skip"],
                "rerun_green": failing[test_file],
                "main_red": test_file in main_red,
            }
        )
    return rows


def windows_label(messages: list[str]) -> str:
    """`os` when a message is a Windows trait, `timing` when one is a timeout or a worker crash."""
    if any(_OS_FAILURE.search(message) for message in messages):
        return "os"
    if any(_TIMING_FAILURE.search(message) for message in messages):
        return "timing"
    return "unclear"


def _noting(
    parse: Callable[[str], dict[str, str]], notes: dict[str, list[str]]
) -> Callable[[str], dict[str, str]]:
    """`parse`, keeping each failing file's messages in `notes` as it goes."""

    def noted(log: str) -> dict[str, str]:
        found = parse(log)
        for nodeid, message in found.items():
            notes.setdefault(nodeid.split("::", 1)[0], []).append(message)
        return found

    return noted


def windows_rows(
    run: Row,
    key: Row,
    block: Row | None,
    linux: dict[str, bool] | str,
    read: tuple[Path, Callable[[str], dict[str, str]], Listing],
    main_red: set[str],
) -> list[Row] | None:
    """The Windows rows of one run attempt; None when `gh` could not read its Windows shards."""
    base = {**key, "os": "windows"}
    if isinstance(linux, str):
        return [{"kind": "windows_skip", **base, "reason": f"Linux: {linux}"}]
    if block is None:
        return [{"kind": "windows_skip", **base, "reason": NO_WINDOWS_BLOCK}]
    root, parse, listing = read
    notes: dict[str, list[str]] = {}
    seen = read_run(run, root, _noting(parse, notes), listing, "Windows")
    if seen is None:
        return None
    if isinstance(seen, str):
        return [{"kind": "windows_skip", **base, "reason": seen}]
    rows: list[Row] = [
        {
            "kind": "windows_run",
            **base,
            "verdict": block["verdict"],
            "trial": block["source"],
            "files": len(block["run"]),
        }
    ]
    for test_file in sorted(set(seen) - set(linux)):
        in_run = test_file in block["run"]
        rows.append(
            {
                "kind": "windows_failure",
                **base,
                "file": test_file,
                "in_run": in_run,
                "label": windows_label(notes.get(test_file, [])),
                "miss": block["verdict"] == "select" and not in_run,
                "rerun_green": seen[test_file],
                "main_red": test_file in main_red,
            }
        )
    return rows


def examine(
    run: Row,
    args: argparse.Namespace,
    root: Path,
    parse: Callable[[str], dict[str, str]],
    listing: Listing,
) -> tuple[Row | None, dict[str, bool] | str | None]:
    """`(verdict, what the run showed)`: a string excludes the run, None leaves it for later."""
    if run.get("event") == "push":
        return {"windows": PUSH_BLOCK}, read_run(run, root, parse, listing)
    try:
        text = verdict_text(run["id"], args, root)
    except subprocess.CalledProcessError:
        return None, None
    if text is None:
        return None, NO_VERDICT
    verdict = parse_verdict(text)
    if verdict is None:
        return None, BAD_VERDICT
    if run_key(run)["source"] is None:
        return None, NO_SOURCE
    return verdict, read_run(run, root, parse, listing)


def runs_to_read(root: Path, args: argparse.Namespace) -> Iterator[Row]:
    """The pull_request runs and (with Windows on) the push runs to main and `release/**`.

    Merged newest first, so a backlog of one event never keeps the other out of a pass.
    """
    prs = list_runs(root, args.since)
    if not args.windows:
        yield from prs
        return
    pushes = (
        run
        for run in list_runs(root, args.since, "push")
        if _PUSH_BRANCH.fullmatch(str(_dict(run).get("head_branch")))
    )
    yield from heapq.merge(
        prs, pushes, key=lambda run: str(_dict(run).get("created_at")), reverse=True
    )


def recorded_lanes(rows: list[Row]) -> tuple[dict[int, int], dict[int, int]]:
    """`(Linux, Windows)`: per run, the newest attempt each lane's rows hold.

    A Linux exclusion no verdict could pass leaves no Windows half to collect.
    """
    linux: dict[int, int] = {}
    windows: dict[int, int] = {}
    for row in rows:
        lanes = [windows] if _on_windows(row) else [linux]
        if row["kind"] == "skip" and row["reason"] in _NO_WINDOWS_HALF:
            lanes.append(windows)
        for lane in lanes:
            lane[row["run"]] = max(lane.get(row["run"], 0), row["attempt"])
    return linux, windows


def collect(
    args: argparse.Namespace, recorded: tuple[dict[int, int], dict[int, int]], root: Path
) -> list[Row]:
    """Rows for up to `--limit` run attempts a lane of the ledger lacks, newest run first.

    Only the lanes the ledger lacks are written: a Windows half is collected for
    an attempt whose Linux half an earlier pass recorded, without repeating it.
    Every row returned passes `valid_row`, so the ledger never holds a line a
    later pass refuses.
    """
    parse = shadow._nodeid_parser()
    red_since, red_files = main_red_files(root)
    rows: list[Row] = []
    taken = 0
    on_linux, on_windows = recorded
    for run in runs_to_read(root, args):
        if taken >= args.limit:
            break
        key = run_key(_dict(run))
        push = _dict(run).get("event") == "push"
        win_excluded = {"kind": "windows_skip", **key, "os": "windows", "reason": BAD_ROW}
        excluded = win_excluded if push else {"kind": "skip", **key, "reason": BAD_ROW}
        if not valid_row(excluded):
            print(f"run {cell(key['run'])}: its listing is unusable; not recorded", file=sys.stderr)
            continue
        need_linux = not push and key["attempt"] > on_linux.get(key["run"], 0)
        need_windows = args.windows and key["attempt"] > on_windows.get(key["run"], 0)
        if not (need_linux or need_windows):
            continue
        listing = functools.cache(functools.partial(shadow.run_jobs, key["run"], root=root))
        verdict, seen = examine(run, args, root, parse, listing)
        if seen == NO_VERDICT and args.verdict_dir:
            continue  # not scored offline: nothing was fetched, so nothing is recorded
        named = red_files if key["run"] >= red_since else set()
        fresh: list[Row] | None = []
        if isinstance(seen, str) and not push:
            fresh = [{**excluded, "reason": seen}]
        elif isinstance(seen, dict) and verdict is not None and not push:
            fresh = run_rows(key, verdict, seen, named)
        if args.windows and seen is not None and verdict is not None:
            # A Linux run that selected its tests still says which files failed on Linux.
            linux = (
                read_run(run, root, parse, listing, full=False) if isinstance(seen, str) else seen
            )
            more = None
            if linux is not None:
                more = windows_rows(
                    run, key, verdict["windows"], linux, (root, parse, listing), named
                )
            fresh = None if more is None or fresh is None else fresh + more
        if seen is None or fresh is None:
            print(f"run {key['run']}: gh could not read it; a later pass will", file=sys.stderr)
            continue
        fresh = [r for r in fresh if (need_windows if _on_windows(r) else need_linux)]
        if not need_linux and not any(_on_windows(r) for r in fresh):
            # The Windows half alone was wanted and nothing let it be read.
            fresh = [{**win_excluded, "reason": seen if isinstance(seen, str) else BAD_ROW}]
        on_linux[key["run"]] = on_windows[key["run"]] = key["attempt"]
        taken += 1
        rows += (
            fresh
            if all(valid_row(row) for row in fresh)
            else [excluded if need_linux else win_excluded]
        )
    return rows


def _on_windows(row: Row) -> bool:
    return str(row["kind"]).startswith("windows_")


def append_rows(path: Path, rows: list[Row]) -> None:
    """Add `rows` to the ledger by replacing it, so no reader ever sees a half-written line."""
    if not rows:
        return
    old = path.read_bytes() if path.is_file() else b""
    if old and not old.endswith(b"\n"):
        old += b"\n"
    text = "".join(json.dumps(row, allow_nan=False) + "\n" for row in rows)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(old + text.encode("utf-8"))
    os.replace(partial, path)


def _when(row: Row) -> datetime:
    return datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_row(row: object) -> bool:
    """Whether one parsed ledger line has the shape this script writes."""
    if not isinstance(row, dict) or not _is_int(row.get("run")) or not _is_int(row.get("attempt")):
        return False
    if row["attempt"] < 1:
        return False
    kind, source = row.get("kind"), row.get("source")
    if not (isinstance(source, str) or (source is None and kind in ("skip", "windows_skip"))):
        return False
    if str(kind).startswith("windows_") and row.get("os") != "windows":
        return False
    if not (row.get("pr") is None or _is_int(row.get("pr"))) or not isinstance(
        row.get("fork"), bool
    ):
        return False
    if not isinstance(row.get("head"), str) or not _SHA.fullmatch(row["head"]):
        return False
    if not isinstance(row.get("date"), str) or not _STAMP.fullmatch(row["date"]):
        return False
    try:
        _when(row)
    except ValueError:
        return False
    if kind in ("skip", "windows_skip"):
        return isinstance(row.get("reason"), str)
    if kind == "windows_run":
        return (
            row.get("verdict") in ("select", "full")
            and row.get("trial") in TRIAL_SOURCES
            and _is_int(row.get("files"))
            and row["files"] >= 0
        )
    if kind == "windows_failure":
        return (
            isinstance(row.get("file"), str)
            and _FILE.fullmatch(row["file"]) is not None
            and row.get("label") in WINDOWS_LABELS
            and all(
                isinstance(row.get(k), bool) for k in ("in_run", "miss", "rerun_green", "main_red")
            )
        )
    if kind == "run":
        tiers = row.get("tiers")
        return (
            row.get("verdict") in ("select", "full")
            and isinstance(tiers, dict)
            and all(isinstance(t, str) and _TIER.fullmatch(t) for t in tiers)
            and all(_is_int(n) and n >= 0 for n in tiers.values())
        )
    if kind == "failure":
        tier = row.get("tier")
        return (
            isinstance(row.get("file"), str)
            and _FILE.fullmatch(row["file"]) is not None
            and (tier is None or (isinstance(tier, str) and _TIER.fullmatch(tier) is not None))
            and all(isinstance(row.get(k), bool) for k in ("miss", "rerun_green", "main_red"))
        )
    return False


def read_ledger(path: Path) -> list[Row]:
    rows: list[Row] = []
    if not path.is_file():
        return rows
    with open(path, encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except (ValueError, RecursionError):
                row = None
            if not valid_row(row):
                raise ValueError(f"{path}:{number}: not a ledger row")
            rows.append(row)
    return rows


def recorded_attempts(rows: list[Row]) -> dict[int, int]:
    newest: dict[int, int] = {}
    for row in rows:
        newest[row["run"]] = max(newest.get(row["run"], 0), row["attempt"])
    return newest


def current(rows: list[Row], since: str | None) -> list[Row]:
    """Each run's newest attempt only, and only runs created on or after `since`."""
    newest = recorded_attempts(rows)
    return [
        row
        for row in rows
        if row["attempt"] == newest[row["run"]] and (not since or str(row["date"])[:10] >= since)
    ]


def classify(rows: list[Row], window_days: float) -> list[Row]:
    """The failure rows of `select` runs, each with `shared` and `diff_caused` set.

    Only a same-repository failure is evidence that a file fails elsewhere; under
    a `full` verdict it has no tier to fit, but it is evidence all the same.
    """
    select = {row["run"] for row in rows if row["kind"] == "run" and row["verdict"] == "select"}
    picked = [r for r in rows if r["kind"] == "failure" and r["run"] in select]
    return _marked(picked, rows, ("failure",), window_days)


def classify_windows(rows: list[Row], window_days: float) -> list[Row]:
    """The Windows-only failure rows, marked as `classify` marks Linux ones.

    A same-repository failure of the file on either OS under another `source` is
    evidence it fails elsewhere.
    """
    picked = [r for r in rows if r["kind"] == "windows_failure"]
    return _marked(picked, rows, ("failure", "windows_failure"), window_days)


def _marked(picked: list[Row], rows: list[Row], kinds: tuple[str, ...], days: float) -> list[Row]:
    by_file: dict[str, list[Row]] = {}
    for row in rows:
        if row["kind"] in kinds and not row["fork"]:
            by_file.setdefault(row["file"], []).append(row)
    out = []
    for row in picked:
        shared = any(
            other["source"] != row["source"]
            and abs((_when(other) - _when(row)).total_seconds()) <= days * 86400
            for other in by_file.get(row["file"], ())
        )
        caused = not (shared or row["rerun_green"] or row["main_red"])
        out.append({**row, "shared": shared, "diff_caused": caused})
    return out


def untiered(failures: list[Row]) -> list[Row]:
    """Same-repository failing files a `select` verdict gave no tier: they cannot be fitted."""
    return [row for row in failures if row["tier"] is None and not row["fork"]]


def wilson_upper(failures: int, trials: int) -> float:
    """The upper end of the two-sided 95% Wilson interval.

    1.0 with no trial, and for a count the interval is not defined on: as many
    failures as trials, or more, which only a ledger this script did not write holds.
    """
    if trials <= 0 or failures >= trials:
        return 1.0
    p = failures / trials
    centre = p + Z95 * Z95 / (2 * trials)
    margin = Z95 * math.sqrt(p * (1 - p) / trials + Z95 * Z95 / (4 * trials * trials))
    return min(1.0, (centre + margin) / (1 + Z95 * Z95 / trials))


def fit(rows: list[Row], failures: list[Row]) -> dict[str, Row]:
    """Per tier, from same-repository runs: files held, failures, the rate and its upper bound.

    The bound is rounded up, so rounding never makes a tier look safer than the data.
    """
    tiers: dict[str, Row] = {}

    def entry(tier: str) -> Row:
        return tiers.setdefault(tier, {"trials": 0, "failures": 0, "other": 0})

    for row in rows:
        if row["kind"] == "run" and row["verdict"] == "select" and not row["fork"]:
            for tier, count in row["tiers"].items():
                entry(tier)["trials"] += count
    for row in failures:
        if row["tier"] is not None and not row["fork"]:
            entry(row["tier"])["failures" if row["diff_caused"] else "other"] += 1
    for counts in tiers.values():
        below = counts["failures"] < counts["trials"]
        counts["rate"] = round(counts["failures"] / counts["trials"], 8) if below else 1.0
        counts["upper95"] = (
            math.ceil(wilson_upper(counts["failures"], counts["trials"]) * 1e8) / 1e8
        )
    return dict(sorted(tiers.items()))


def proposed_rates(tiers: dict[str, Row]) -> Row:
    """The fit in the scorer's rates shape: `{"schema": 1, "tiers": {tier: {rate, upper95}}}`."""
    return {
        "schema": VERDICT_SCHEMA,
        "tiers": {t: {"rate": c["rate"], "upper95": c["upper95"]} for t, c in tiers.items()},
    }


def _table(head: list[str], body: list[list[object]]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return lines + ["| " + " | ".join(cell(value) for value in line) + " |" for line in body]


def _where(row: Row) -> list[object]:
    pr = f"#{row['pr']}" if _is_int(row["pr"]) else row["source"]
    return [pr, row["head"][:12], row["run"], row["file"]]


def summarize(rows: list[Row], failures: list[Row], tiers: dict[str, Row]) -> str:
    runs = [row for row in rows if row["kind"] == "run"]
    select = [row for row in runs if row["verdict"] == "select"]
    fitted = [row for row in select if not row["fork"]]
    days = [_when(row) for row in fitted]
    span = (max(days) - min(days)).total_seconds() / 86400 if days else 0.0
    skipped = Counter(row["reason"] for row in rows if row["kind"] == "skip")
    lines = [
        f"Runs fitted: {len(fitted)} with a `select` verdict; {len(runs) - len(select)} more "
        f"had a `full` verdict. Days of data: {span:.1f}. "
        f"Fork runs, reported but not fitted: {len(select) - len(fitted)}.",
        f"Runs excluded: {sum(skipped.values())}"
        + "".join(f"; {cell(reason)}: {n}" for reason, n in sorted(skipped.items()))
        + ".",
        "",
    ]
    unfitted = untiered(failures)
    if unfitted:
        lines += [
            f"**Failing files a `select` verdict gave no tier: {len(unfitted)}. "
            "No rates are proposed while the ledger holds one.**",
            "",
            *_table(["PR", "Head", "Run", "File"], [_where(row) for row in unfitted]),
            "",
        ]
    lines += _table(
        ["Tier", "Files held", "Diff-caused failures", "Other failures", "Rate", "Upper 95%"],
        [
            [t, c["trials"], c["failures"], c["other"], f"{c['rate']:.8f}", f"{c['upper95']:.8f}"]
            for t, c in tiers.items()
        ],
    )
    misses = [row for row in failures if row["miss"]]
    groups = [
        ("Misses caused by the diff", [r for r in misses if r["diff_caused"] and not r["fork"]]),
        ("Misses not caused by it", [r for r in misses if not r["diff_caused"] and not r["fork"]]),
        ("Misses on fork runs, reported and not fitted", [r for r in misses if r["fork"]]),
    ]
    for title, picked in groups:
        lines += ["", f"{title}: {len(picked)}."]
        if picked:
            body = [
                [
                    *_where(row),
                    row["tier"],
                    ", ".join(k for k in ("rerun_green", "main_red", "shared") if row[k]),
                ]
                for row in picked
            ]
            lines += ["", *_table(["PR", "Head", "Run", "File", "Tier", "Not diff-caused"], body)]
    return "\n".join(lines)


def windows_status(rows: list[Row], failures: list[Row], now: datetime) -> Row:
    """The auto-off line: same-repository trials and diff-caused misses in the last window."""

    def recent(row: Row) -> bool:
        return (now - _when(row)).total_seconds() <= WINDOWS_STATUS_DAYS * 86400

    trials = sum(1 for r in rows if r["kind"] == "windows_run" and not r["fork"] and recent(r))
    misses = sum(
        1 for r in failures if r["miss"] and r["diff_caused"] and not r["fork"] and recent(r)
    )
    return {
        "windows_selection_ok": misses < WINDOWS_MISS_BAR and trials >= WINDOWS_MIN_TRIALS,
        "window_days": WINDOWS_STATUS_DAYS,
        "diff_caused_windows_misses": misses,
        "trials": trials,
        "bar": WINDOWS_MISS_BAR,
    }


def summarize_windows(rows: list[Row], failures: list[Row], status: Row) -> str:
    """The Windows section: its own trials, misses and days, never mixed into a Linux number."""
    trials = [row for row in rows if row["kind"] == "windows_run" and not row["fork"]]
    days = [_when(row) for row in trials]
    span = (max(days) - min(days)).total_seconds() / 86400 if days else 0.0
    sources = Counter(row["trial"] for row in trials)
    skipped = Counter(row["reason"] for row in rows if row["kind"] == "windows_skip")
    labels = Counter(row["label"] for row in failures if not row["fork"])
    lines = [
        "## Windows",
        "",
        f"Windows trials: {len(trials)}"
        + "".join(f"; {source}: {sources[source]}" for source in TRIAL_SOURCES)
        + f". Days of data: {span:.1f}.",
        f"Windows runs excluded: {sum(skipped.values())}"
        + "".join(f"; {cell(reason)}: {n}" for reason, n in sorted(skipped.items()))
        + ".",
        f"Windows-only failures: {sum(labels.values())}"
        + "".join(f"; {label}: {labels[label]}" for label in WINDOWS_LABELS)
        + ".",
    ]
    misses = [row for row in failures if row["miss"]]
    groups = [
        ("Windows misses caused by the diff", [r for r in misses if r["diff_caused"]]),
        ("Windows misses not caused by it", [r for r in misses if not r["diff_caused"]]),
    ]
    for title, picked in groups:
        same = [row for row in picked if not row["fork"]]
        lines += [
            "",
            f"{title}: {len(same)}; on fork runs, not counted: {len(picked) - len(same)}.",
        ]
        if picked:
            body = [
                [
                    *_where(row),
                    row["label"],
                    "fork" if row["fork"] else "",
                    ", ".join(k for k in ("rerun_green", "main_red", "shared") if row[k]),
                ]
                for row in picked
            ]
            head = ["PR", "Head", "Run", "File", "Label", "Fork", "Not diff-caused"]
            lines += ["", *_table(head, body)]
    return "\n".join(lines + ["", json.dumps(status, sort_keys=True)])


def _day(value: str) -> str:
    if not _DAY.fullmatch(value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    return value


def _artifact(value: str) -> str:
    if not _ARTIFACT.fullmatch(value):
        raise argparse.ArgumentTypeError("expected an artifact name of letters, digits, . _ -")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n", 1)[0])
    parser.add_argument("--ledger", required=True, help="JSONL ledger to read and append to")
    parser.add_argument("--rates-out", required=True, help="where the proposed tier rates go")
    parser.add_argument("--summary-out", help="where the markdown summary goes (always printed)")
    parser.add_argument("--limit", type=int, default=50, help="run attempts to record; 0 reports")
    parser.add_argument("--since", type=_day, help="only runs created on or after YYYY-MM-DD")
    parser.add_argument("--window-days", type=float, default=3.0, help="the `shared` window")
    parser.add_argument("--artifact", type=_artifact, default="test-selection-verdict")
    parser.add_argument("--verdict-dir", help="verdicts scored offline, <run id>.json each")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument(
        "--no-windows", dest="windows", action="store_false", help="read no Windows shard"
    )
    parser.add_argument("--windows-status-out", help="where the Windows auto-off JSON goes")
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger)
    try:
        ledger = read_ledger(ledger_path)
        new = collect(args, recorded_lanes(ledger), Path(args.root)) if args.limit > 0 else []
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"nothing recorded: {error}", file=sys.stderr)
        return EXIT_NOT_RECORDED
    append_rows(ledger_path, new)
    rows = current(ledger + new, args.since)
    failures = classify(rows, args.window_days)
    tiers = fit(rows, failures)
    text = summarize(rows, failures, tiers)
    if args.windows:
        on_windows = classify_windows(rows, args.window_days)
        status = windows_status(rows, on_windows, datetime.now(timezone.utc))
        text += "\n\n" + summarize_windows(rows, on_windows, status)
        if args.windows_status_out:
            Path(args.windows_status_out).write_text(json.dumps(status) + "\n", encoding="utf-8")
    if args.summary_out:
        Path(args.summary_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if untiered(failures):
        print("no rates written: a failing file has no tier", file=sys.stderr)
        return EXIT_WITHHELD
    Path(args.rates_out).write_text(
        json.dumps(proposed_rates(tiers), indent=2) + "\n", encoding="utf-8"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
