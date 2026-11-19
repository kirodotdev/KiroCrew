#!/usr/bin/env python3
"""Miss ledger for diff-based test selection: shadow verdicts joined with full-run failures.

Reads only: `gh` (PR `CI` runs, their verdict artifacts, the Linux `Backend Tests`
job logs, open `main-red` issues). Appends to a JSONL ledger, writes proposed tier
rates as JSON and prints a markdown summary. It never edits the checked-in rates
file: a person moves the proposal in by PR.

A verdict (one JSON object per run, the artifact's `--verdict-file`, or
`<--verdict-dir>/<run id>.json` when scored offline):

    {"pr": 7, "head": "<sha>", "tiers": {"<tier>": ["test/test_a.py", ...]},
     "skippable": ["<tier>", ...]}

Ledger rows, keyed on the run id and attempt so a second pass adds nothing it
already has; a later attempt of a recorded run appends a fresh observation of the
whole run, and the report reads only each run's latest one:

    {"kind": "run", "run", "attempt", "pr", "head", "date", "tier_counts": {"<tier>": n}}
    {"kind": "failure", "run", "attempt", "pr", "head", "date", "file", "tier", "miss",
     "rerun_green"}

A failure is not diff-caused when a rerun's Linux `Backend Tests` jobs all
concluded `success` (`rerun_green`; a rerun that is still red clears nothing),
an open `main-red` issue names the file (`main_red`), or the same file failed on
another PR within `--window-days` (`shared`). `main_red` and `shared` are decided
over the whole ledger each time the report is written, from the issues open then.
Those rows stay in the ledger and are reported apart.

Each tier's rate is its diff-caused failing files over the files it held across
runs, proposed at the upper 95% Wilson bound. A failing file the verdict gave no
tier cannot be fitted: the summary lists it first, and no rates file is written
(exit 1) while any such row is in the ledger.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_test_map_shadow as shadow  # noqa: E402  (path set immediately above)

Z95 = 1.959964


def tier_of(verdict: dict[str, Any], test_file: str) -> str | None:
    for tier, files in verdict.get("tiers", {}).items():
        if test_file in files:
            return str(tier)
    return None


def run_rows(sample: dict[str, Any], verdict: dict[str, Any]) -> list[dict[str, Any]]:
    """One run's ledger rows.

    `sample["attempts"]` lists each attempt's failing test files, oldest first, and
    `sample["last_green"]` says whether the last attempt's backend jobs all succeeded.
    """
    base = {k: sample[k] for k in ("run", "attempt", "pr", "head", "date")}
    rows: list[dict[str, Any]] = [
        {
            "kind": "run",
            **base,
            "tier_counts": {t: len(files) for t, files in verdict.get("tiers", {}).items()},
        }
    ]
    attempts: list[list[str]] = sample["attempts"]
    last = set(attempts[-1]) if attempts else set()
    went_green = len(attempts) > 1 and bool(sample["last_green"])
    skippable = set(verdict.get("skippable", ()))
    for test_file in sorted({f for attempt in attempts for f in attempt}):
        tier = tier_of(verdict, test_file)
        rows.append(
            {
                "kind": "failure",
                **base,
                "file": test_file,
                "tier": tier,
                "miss": tier in skippable,
                "rerun_green": went_green and test_file not in last,
            }
        )
    return rows


def latest(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only each run's newest attempt: a rerun supersedes what was recorded before it."""
    newest: dict[int, int] = {}
    for row in rows:
        newest[row["run"]] = max(newest.get(row["run"], 0), row["attempt"])
    return [r for r in rows if r["attempt"] == newest[r["run"]]]


def recorded_attempts(rows: list[dict[str, Any]]) -> dict[int, int]:
    return {r["run"]: r["attempt"] for r in latest(rows)}


def _day(row: dict[str, Any]) -> datetime:
    return datetime.fromisoformat(str(row["date"]).replace("Z", "+00:00"))


def mark_shared(
    rows: list[dict[str, Any]], window_days: float, main_red_text: str = ""
) -> list[dict[str, Any]]:
    """Failure rows with `shared`, `main_red` and `diff_caused` set, over the whole ledger."""
    failures = [r for r in rows if r["kind"] == "failure"]
    out = []
    for row in failures:
        shared = any(
            other["file"] == row["file"]
            and other["pr"] != row["pr"]
            and abs((_day(other) - _day(row)).total_seconds()) <= window_days * 86400
            for other in failures
        )
        main_red = row["file"] in main_red_text
        caused = not (shared or row["rerun_green"] or main_red)
        out.append({**row, "shared": shared, "main_red": main_red, "diff_caused": caused})
    return out


def wilson_upper(failures: int, trials: int, z: float = Z95) -> float:
    if trials <= 0:
        return 1.0
    p = failures / trials
    n = trials
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return min(1.0, (centre + margin) / (1 + z * z / n))


def refit(
    rows: list[dict[str, Any]], window_days: float, main_red_text: str = ""
) -> dict[str, Any]:
    """Per tier: files held, diff-caused and other failures, and the proposed rate.

    `unscored` counts the failing files the verdict gave no tier: a fit with any is incomplete.
    """
    rows = latest(rows)
    marked = mark_shared(rows, window_days, main_red_text)
    tiers: dict[str, dict[str, Any]] = {}

    def entry(tier: str) -> dict[str, Any]:
        return tiers.setdefault(tier, {"trials": 0, "failures": 0, "not_diff_caused": 0})

    for row in rows:
        if row["kind"] == "run":
            for tier, count in row["tier_counts"].items():
                entry(tier)["trials"] += count
    for row in marked:
        if row["tier"] is not None:
            entry(row["tier"])["failures" if row["diff_caused"] else "not_diff_caused"] += 1
    for counts in tiers.values():
        counts["point"] = round(counts["failures"] / counts["trials"], 6) if counts["trials"] else 0
        counts["rate"] = round(wilson_upper(counts["failures"], counts["trials"]), 6)
    return {
        "bound": "wilson-upper-95",
        "tiers": dict(sorted(tiers.items())),
        "unscored": sum(row["tier"] is None for row in marked),
    }


def days_of_data(rows: list[dict[str, Any]]) -> float:
    days = [_day(r) for r in rows if r["kind"] == "run"]
    return round((max(days) - min(days)).total_seconds() / 86400, 1) if days else 0.0


def summarize(
    rows: list[dict[str, Any]], rates: dict[str, Any], window_days: float, main_red_text: str = ""
) -> str:
    rows = latest(rows)
    failures = mark_shared(rows, window_days, main_red_text)
    runs = sum(r["kind"] == "run" for r in rows)
    lines = [f"Runs: {runs}. Days of data: {days_of_data(rows)}.", ""]
    unscored = [r for r in failures if r["tier"] is None]
    if unscored:
        lines += [
            f"**Failing files the verdict gave no tier: {len(unscored)}. "
            "No rates are proposed until each verdict covers them.**",
            "",
            "| PR | Head | File |",
            "|---|---|---|",
            *(f"| {r['pr']} | {r['head'][:12]} | {r['file']} |" for r in unscored),
            "",
        ]
    lines += ["| Tier | Files held | Diff-caused | Not diff-caused | Point | Rate (upper 95%) |"]
    lines += ["|---|---|---|---|---|---|"]
    for tier, c in rates["tiers"].items():
        lines.append(
            f"| {tier} | {c['trials']} | {c['failures']} | {c['not_diff_caused']} "
            f"| {c['point']:.4f} | {c['rate']:.4f} |"
        )
    for title, caused in (("Misses", True), ("Misses not caused by the diff", False)):
        picked = [r for r in failures if r["miss"] and r["diff_caused"] is caused]
        lines += ["", f"{title}: {len(picked)}."]
        if picked:
            lines += [
                "",
                "| PR | Head | File | Tier | Why not diff-caused |",
                "|---|---|---|---|---|",
            ]
            for r in picked:
                why = ", ".join(k for k in ("shared", "rerun_green", "main_red") if r[k])
                lines.append(
                    f"| {r['pr']} | {r['head'][:12]} | {r['file']} | {r['tier']} | {why} |"
                )
    return "\n".join(lines)


def read_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def append_runs(
    ledger: list[dict[str, Any]],
    samples: list[tuple[dict[str, Any], dict[str, Any]]],
) -> list[dict[str, Any]]:
    """The rows to append: every sample whose run and attempt the ledger does not hold yet."""
    seen = {(r["run"], r["attempt"]) for r in ledger}
    new: list[dict[str, Any]] = []
    for sample, verdict in samples:
        key = (sample["run"], sample["attempt"])
        if key not in seen:
            seen.add(key)
            new += run_rows(sample, verdict)
    return new


def _attempt_green(run_id: int, attempt: int) -> bool:
    """Whether every Linux backend job of this attempt concluded `success`; unreadable is not."""
    try:
        jobs = shadow._gh_json(
            [
                "api",
                f"repos/{shadow.REPO}/actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100",
                "--jq",
                ".jobs",
            ],
            REPO_ROOT,
        )
    except subprocess.CalledProcessError:
        return False
    backend = [j for j in jobs if shadow.LINUX_BACKEND_JOB.fullmatch(j["name"])]
    return bool(backend) and all(j["conclusion"] == "success" for j in backend)


def _verdict(run_id: int, args: argparse.Namespace) -> dict[str, Any] | None:
    if args.verdict_dir:
        path = Path(args.verdict_dir) / f"{run_id}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    with tempfile.TemporaryDirectory() as tmp:
        try:
            shadow._run(
                ["gh", "run", "download", str(run_id), "-R", shadow.REPO]
                + ["-n", args.artifact, "-D", tmp],
                REPO_ROOT,
            )
        except subprocess.CalledProcessError:
            return None  # no verdict artifact on this run
        path = Path(tmp) / args.verdict_file
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def fetch_samples(
    limit: int, args: argparse.Namespace, recorded: dict[int, int]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The newest completed PR `CI` runs with a verdict, each attempt's failing files read.

    `recorded` maps a run id to the attempt the ledger holds: only a newer attempt is read.
    """
    parse = shadow._nodeid_parser()
    runs = shadow._gh_json(
        [
            "api",
            f"repos/{shadow.REPO}/actions/workflows/ci.yml/runs?event=pull_request"
            "&status=completed&per_page=100",
            "--jq",
            ".workflow_runs",
        ],
        REPO_ROOT,
    )
    samples: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for run in runs:
        if len(samples) >= limit:
            break
        last = int(run.get("run_attempt", 1))
        if last <= recorded.get(run["id"], 0):
            continue
        verdict = _verdict(run["id"], args)
        if verdict is None:
            continue
        attempts = []
        for attempt in range(1, last + 1):
            row = shadow._red_row({**run, "run_attempt": attempt}, REPO_ROOT, parse)
            if row is None:
                break  # an unreadable attempt: leave the run for a later pass
            attempts.append(row.get("failing", []))
        else:
            prs = run.get("pull_requests") or []
            sample = {
                "run": run["id"],
                "attempt": last,
                "last_green": last > 1 and _attempt_green(run["id"], last),
                "pr": verdict.get("pr", prs[0]["number"] if prs else None),
                "head": run["head_sha"],
                "date": run["created_at"],
                "attempts": attempts,
            }
            samples.append((sample, verdict))
    return samples


def main_red_text() -> str:
    issues = shadow._gh_json(
        ["issue", "list", "-R", shadow.REPO, "--label", "main-red", "--state", "open"]
        + ["--json", "title,body", "--limit", "100"],
        REPO_ROOT,
    )
    return "\n".join(f"{i.get('title', '')}\n{i.get('body', '')}" for i in issues)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--ledger", required=True, help="JSONL ledger to read and append to")
    parser.add_argument("--rates-out", required=True, help="proposed tier rates (JSON)")
    parser.add_argument("--summary-out", help="markdown summary (default: stdout only)")
    parser.add_argument("--limit", type=int, default=50, help="new runs to read")
    parser.add_argument("--window-days", type=float, default=3.0)
    parser.add_argument("--artifact", default="test-selection-verdict")
    parser.add_argument("--verdict-file", default="verdict.json")
    parser.add_argument("--verdict-dir", help="offline-scored verdicts, <run id>.json")
    args = parser.parse_args(argv)
    ledger_path = Path(args.ledger)
    ledger = read_ledger(ledger_path)
    if args.limit:
        samples = fetch_samples(args.limit, args, recorded_attempts(ledger))
        new = append_runs(ledger, samples)
        with open(ledger_path, "a", encoding="utf-8") as handle:
            handle.writelines(json.dumps(row) + "\n" for row in new)
        ledger += new
    red = main_red_text()
    rates = refit(ledger, args.window_days, red)
    text = summarize(ledger, rates, args.window_days, red)
    if args.summary_out:
        Path(args.summary_out).write_text(text + "\n", encoding="utf-8")
    print(text)
    if rates["unscored"]:
        print("no rates written: some failing files have no tier", file=sys.stderr)
        return 1
    Path(args.rates_out).write_text(json.dumps(rates, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
