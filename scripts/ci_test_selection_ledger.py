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

Exit codes: 0 the proposal was written; 2 nothing was recorded (a ledger line of
the wrong shape, or `gh` failed before the first row); 3 the rates are withheld.
1 is left to the interpreter, so an error this script did not foresee is never
read as one of those.

Verdicts, logs and the ledger are data: parsed as JSON, checked field by field,
size-capped, and every table cell is cut to a fixed character set.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator
from datetime import datetime
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
EXIT_NOT_RECORDED = 2
EXIT_WITHHELD = 3

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
    return {"verdict": doc["verdict"], "tiers": tiers, "would_skip": skip}


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


def list_runs(root: Path, since: str | None) -> Iterator[Row]:
    """Completed pull_request `CI` runs, newest first, a page at a time until the listing ends."""
    created = f"&created=%3E%3D{since}" if since else ""
    for page in range(1, MAX_RUN_PAGES + 1):
        runs = shadow._gh_json(
            [
                "api",
                f"repos/{shadow.REPO}/actions/workflows/ci.yml/runs?event=pull_request"
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


def read_run(
    run: Row, root: Path, parse: Callable[[str], dict[str, str]]
) -> dict[str, bool] | str | None:
    """`{failing file: its shard ended green on a rerun}` for a full-suite run.

    A string says why the run is not an observation; None means `gh` could not
    read it, so a later pass tries again.
    """
    final: dict[str, Row] = {}
    named: dict[int, set[str]] = {}
    failed: dict[str, set[str]] = {}
    for attempt in range(1, int(run.get("run_attempt") or 1) + 1):
        jobs = shadow.backend_jobs(run["id"], attempt, root)
        if jobs is None:
            return None
        for job in jobs:
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
        return "no Linux backend shard ran"
    for job in final.values():
        if job["conclusion"] not in ("success", "failure"):
            return "a backend shard neither passed nor failed"
        if not ran_full_suite(job):
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


def examine(
    run: Row, args: argparse.Namespace, root: Path, parse: Callable[[str], dict[str, str]]
) -> tuple[Row | None, dict[str, bool] | str | None]:
    """`(verdict, what the run showed)`: a string excludes the run, None leaves it for later."""
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
    return verdict, read_run(run, root, parse)


def collect(args: argparse.Namespace, recorded: dict[int, int], root: Path) -> list[Row]:
    """Rows for up to `--limit` run attempts the ledger does not hold, newest run first.

    Every row returned passes `valid_row`, so the ledger never holds a line a
    later pass refuses.
    """
    parse = shadow._nodeid_parser()
    red_since, red_files = main_red_files(root)
    rows: list[Row] = []
    taken = 0
    for run in list_runs(root, args.since):
        if taken >= args.limit:
            break
        key = run_key(_dict(run))
        excluded = {"kind": "skip", **key, "reason": BAD_ROW}
        if not valid_row(excluded):
            print(f"run {cell(key['run'])}: its listing is unusable; not recorded", file=sys.stderr)
            continue
        if key["attempt"] <= recorded.get(key["run"], 0):
            continue
        verdict, seen = examine(run, args, root, parse)
        if seen == NO_VERDICT and args.verdict_dir:
            continue  # not scored offline: nothing was fetched, so nothing is recorded
        if seen is None:
            print(f"run {key['run']}: gh could not read it; a later pass will", file=sys.stderr)
            continue
        recorded[key["run"]] = key["attempt"]
        taken += 1
        if isinstance(seen, str) or verdict is None:
            fresh = [{**excluded, "reason": seen}]
        else:
            named = red_files if key["run"] >= red_since else set()
            fresh = run_rows(key, verdict, seen, named)
        rows += fresh if all(valid_row(row) for row in fresh) else [excluded]
    return rows


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
    if not (isinstance(source, str) or (source is None and kind == "skip")):
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
    if kind == "skip":
        return isinstance(row.get("reason"), str)
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
    by_file: dict[str, list[Row]] = {}
    for row in rows:
        if row["kind"] == "failure" and not row["fork"]:
            by_file.setdefault(row["file"], []).append(row)
    out = []
    for row in rows:
        if row["kind"] != "failure" or row["run"] not in select:
            continue
        shared = any(
            other["source"] != row["source"]
            and abs((_when(other) - _when(row)).total_seconds()) <= window_days * 86400
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
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger)
    try:
        ledger = read_ledger(ledger_path)
        new = collect(args, recorded_attempts(ledger), Path(args.root)) if args.limit > 0 else []
    except (ValueError, subprocess.CalledProcessError) as error:
        print(f"nothing recorded: {error}", file=sys.stderr)
        return EXIT_NOT_RECORDED
    append_rows(ledger_path, new)
    rows = current(ledger + new, args.since)
    failures = classify(rows, args.window_days)
    tiers = fit(rows, failures)
    text = summarize(rows, failures, tiers)
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
