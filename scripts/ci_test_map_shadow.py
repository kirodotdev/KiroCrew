#!/usr/bin/env python3
"""Shadow report for the test-map selector: how much would it run, and is it safe?

Reads only: `gh` (pull requests, CI runs, job logs) and local git. Writes one
JSON line per sample to `--out` and prints a markdown summary. Nothing here
changes a test any lane runs.

    merged --limit N   the last N merged PRs: the selector's verdict on each
                       squash commit's own diff, and the share of test node ids
                       it would run (a full-suite fall-back counts as 1.0)
    red --limit N      the last N red `CI` pull_request runs whose Linux
                       `Backend Tests` jobs name failing tests: would every
                       failing test FILE have been selected for the PR's diff?

A red run is measured on GitHub's merge ref, so a test that failed because main
was broken (or a flake) is not caused by the PR's diff; such a miss is still
counted as a miss, and the row says whether the same test file also failed on a
different PR in the sample (`shared`), which is how a broken main shows up.

One map (the newest) judges every sample, including diffs older than the map:
the report measures the method, not a historical replay of each night's map.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import statistics
import subprocess
import sys
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import ci_test_map as ctm  # noqa: E402  (path set immediately above)

REPO = "kirodotdev/KiroCrew"
# ci.yml's Linux matrix job, `Backend Tests (3.12, 4)`; not the Windows or IPv6 lanes.
LINUX_BACKEND_JOB = re.compile(r"Backend Tests \(3\.\d+, \d+\)")
TIMEOUT = 120
_NO_TIER = "\0no-guard-tier"


def watch_module() -> Any:
    """`main_red_nodeids`, the module main-red-watch.yml already uses."""
    path = REPO_ROOT / ".github" / "scripts" / "main_red_nodeids.py"
    spec = importlib.util.spec_from_file_location("main_red_nodeids", path)
    if spec is None or spec.loader is None:
        raise ImportError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nodeid_parser() -> Callable[[str], dict[str, str]]:
    parse: Callable[[str], dict[str, str]] = watch_module().parse_failures
    return parse


def _run(argv: list[str], root: Path) -> str:
    return subprocess.run(
        argv,
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=TIMEOUT,
    ).stdout


def _gh_json(args: list[str], root: Path) -> Any:
    return json.loads(_run(["gh", *args], root))


def failing_test_files(failures: dict[str, str]) -> list[str]:
    """Node ids -> the distinct test files they live in."""
    return sorted({nodeid.split("::", 1)[0] for nodeid in failures})


def judge(
    name_status: str, test_map: dict[str, Any], tier: list[str], failing: list[str] | None = None
) -> dict[str, Any]:
    """One sample's verdict: full or not, share, and (for a red run) what was missed.

    `map_share` and `missed_by_map` leave the guard tier out, to show what the map
    alone picks and what only the tier caught.
    """
    changes = ctm.parse_name_status(name_status)
    result = ctm.select(changes, test_map, tier)
    # The same selection with a stand-in tier, which select needs non-empty: keeps a
    # changed test file that is also in the tier, drops only what the tier added.
    by_map = ctm.select(changes, test_map, [_NO_TIER])
    by_map = by_map._replace(tests=[t for t in by_map.tests if t != _NO_TIER])
    row: dict[str, Any] = {
        "full": result.full,
        "share": round(ctm.share(result, test_map), 4),
        "map_share": round(ctm.share(by_map, test_map), 4),
        "selected_files": len(result.tests),
        "reasons": result.reasons[:5],
    }
    if failing is not None:
        row["failing"] = failing
        row["missed"] = [] if result.full else [f for f in failing if f not in result.tests]
        row["missed_by_map"] = [] if result.full else [f for f in failing if f not in by_map.tests]
    return row


def merged_samples(limit: int, root: Path) -> list[dict[str, Any]]:
    prs = _gh_json(
        [
            "pr",
            "list",
            "-R",
            REPO,
            "--state",
            "merged",
            "--base",
            "main",
            "--limit",
            str(limit),
            "--json",
            "number,mergeCommit,title",
        ],
        root,
    )
    rows = []
    for pr in prs:
        sha = (pr.get("mergeCommit") or {}).get("oid")
        if sha:
            rows.append(
                {"pr": pr["number"], "title": pr["title"][:70], "base": f"{sha}^", "head": sha}
            )
    return rows


def red_samples(
    limit: int, root: Path, scan: int = 300, cache: Path | None = None
) -> list[dict[str, Any]]:
    """The newest red pull_request CI runs whose Linux backend jobs name failing tests.

    `cache` keeps each run's verdict (`run-<id>.json`) so a re-run reads no log twice.
    """
    parse = _nodeid_parser()
    runs = []
    for page in range(1, scan // 100 + 1):
        try:
            runs += _gh_json(
                [
                    "api",
                    f"repos/{REPO}/actions/workflows/ci.yml/runs?event=pull_request"
                    f"&status=failure&per_page=100&page={page}",
                    "--jq",
                    ".workflow_runs",
                ],
                root,
            )
        except subprocess.CalledProcessError:
            if not runs:
                raise
            break  # a later page failed: sample what the earlier pages listed
    rows: list[dict[str, Any]] = []

    def one(run: dict[str, Any]) -> dict[str, Any] | None:
        # A rerun keeps the run id and bumps run_attempt: key on both.
        cached = cache / f"run-{run['id']}-{run.get('run_attempt', 1)}.json" if cache else None
        if cached and cached.is_file():
            return json.loads(cached.read_text(encoding="utf-8"))
        row = _red_row(run, root, parse)
        if cached and row is not None:  # unreadable runs stay uncached, so a later pass retries
            cached.write_text(json.dumps(row), encoding="utf-8")
        return row

    # Eight runs at a time: a run costs one jobs call plus one log per red Linux
    # shard, and most red runs are not test failures at all (a lint lane, a 429).
    with ThreadPoolExecutor(max_workers=8) as pool:
        for start in range(0, len(runs), 8):
            if len(rows) >= limit:
                break
            rows += [row for row in pool.map(one, runs[start : start + 8]) if row]
    return rows[:limit]


def backend_jobs(run_id: int, attempt: int, root: Path) -> list[dict[str, Any]] | None:
    """The Linux `Backend Tests` jobs of one run attempt, None when they cannot all be read."""
    jobs = run_jobs(run_id, attempt, root)
    return None if jobs is None else [j for j in jobs if LINUX_BACKEND_JOB.fullmatch(j["name"])]


def run_jobs(run_id: int, attempt: int, root: Path) -> list[dict[str, Any]] | None:
    """Every job of one run attempt, None when they cannot all be read."""
    jobs: list[dict[str, Any]] = []
    page = 1
    while True:
        try:
            doc = _gh_json(
                [
                    "api",
                    f"repos/{REPO}/actions/runs/{run_id}/attempts/{attempt}"
                    f"/jobs?per_page=100&page={page}",
                ],
                root,
            )
        except subprocess.CalledProcessError:
            return None  # transient API error
        jobs += doc["jobs"]
        if not doc["jobs"] or len(jobs) >= doc["total_count"]:
            break
        page += 1
    return jobs


def job_failures(
    job: dict[str, Any], root: Path, parse: Callable[[str], dict[str, str]]
) -> dict[str, str] | None:
    """The failing node ids one job's log names, None when the log cannot be read."""
    try:
        log = _run(
            [
                "gh",
                "api",
                "--allow-escape-sequences",
                f"repos/{REPO}/actions/jobs/{job['id']}/logs",
            ],
            root,
        )
    except subprocess.CalledProcessError:
        return None
    return parse(log)


def _red_row(
    run: dict[str, Any], root: Path, parse: Callable[[str], dict[str, str]]
) -> dict[str, Any] | None:
    """One run's sample, `{}` when no Linux backend job named a failing test, None if unreadable."""
    prs = run.get("pull_requests") or []
    jobs = backend_jobs(run["id"], run.get("run_attempt", 1), root)
    if jobs is None:
        return None
    failures: dict[str, str] = {}
    for job in jobs:
        if job["conclusion"] == "failure":
            named = job_failures(job, root, parse)
            if named is None:
                # One unreadable shard log makes the sample incomplete: a failure
                # in that shard would be uncounted. Not cached, so a later pass retries.
                return None
            failures.update(named)
    if not failures:
        return {}
    print(f"red run {run['id']}: {len(failures)} failing node ids", file=sys.stderr)
    return {
        "run": run["id"],
        "pr": prs[0]["number"] if prs else None,
        "head": run["head_sha"],
        "failing": failing_test_files(failures),
    }


def _ensure_commit(sha: str, root: Path) -> bool:
    try:
        _run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], root)
        return True
    except subprocess.CalledProcessError:
        pass
    try:
        _run(["git", "fetch", "-q", "--no-tags", "origin", sha], root)
        return True
    except subprocess.CalledProcessError:
        return False


def evaluate(
    rows: list[dict[str, Any]], test_map: dict[str, Any], root: Path
) -> list[dict[str, Any]]:
    tier = ctm.guard_tier(root)
    out = []
    for row in rows:
        if not _ensure_commit(row["head"], root):
            continue
        base = (
            row.get("base") or _run(["git", "merge-base", "origin/main", row["head"]], root).strip()
        )
        name_status = ctm.git_name_status(base, row["head"], root)
        out.append({**row, **judge(name_status, test_map, tier, row.get("failing"))})
    for row in out:
        if row.get("missed"):
            others = {
                r.get("pr") or r.get("run")
                for r in out
                if set(r.get("failing", ())) & set(row["missed"])
            }
            row["shared"] = len(others) > 1
        elif "missed" in row:
            row["shared"] = False
    return out


def summarize(
    merged: list[dict[str, Any]], red: list[dict[str, Any]], test_map: dict[str, Any]
) -> str:
    lines = [
        f"Map: commit `{test_map.get('commit', '')[:10]}`, {len(test_map['files'])} measured files, "
        f"{len(test_map['tests'])} test files, {sum(test_map['tests'].values())} test node ids."
    ]
    if merged:
        shares = sorted(r["share"] for r in merged)
        reduced = sorted(r["share"] for r in merged if not r["full"])
        by_map = sorted(r["map_share"] for r in merged if not r["full"])
        lines += [
            "",
            f"Merged PRs: {len(merged)}. Reduced (not full suite): {len(reduced)}. "
            f"Median selected share: {statistics.median(shares):.1%}. "
            f"Mean: {sum(shares) / len(shares):.1%}. "
            f"PRs at <=10%: {sum(s <= 0.10 for s in shares)}.",
        ]
        if reduced:
            lines.append(
                f"Reduced PRs only: median share {statistics.median(reduced):.1%} with the guard tier, "
                f"{statistics.median(by_map):.1%} from the map alone."
            )
        reasons = Counter(r["reasons"][0].split(":")[-1].strip() for r in merged if r["full"])
        lines.append(
            "Full-suite fall-back reasons (first per PR): "
            + ", ".join(f"{k} {v}" for k, v in reasons.most_common())
        )
    if red:
        caught = [r for r in red if not r["missed"]]
        reduced_red = [r for r in red if not r["full"]]
        reduced_caught = [r for r in reduced_red if not r["missed"]]
        lines += [
            "",
            f"Red PR pushes: {len(red)}. Every failing test file selected: {len(caught)} "
            f"({len(caught) / len(red):.0%}). Of the {len(reduced_red)} reduced runs, "
            f"{len(reduced_caught)} caught every failing file "
            f"({sum(not r['missed_by_map'] for r in reduced_red)} from the map alone); "
            f"misses also failing on another PR (broken main or flake): "
            f"{sum(r['shared'] for r in reduced_red if r['missed'])}.",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--map", required=True)
    parser.add_argument("--merged", type=int, default=0, help="last N merged PRs")
    parser.add_argument("--red", type=int, default=0, help="last N red PR runs with failing tests")
    parser.add_argument("--out", required=True, help="JSONL rows to write")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--cache", help="directory that keeps each red run's parsed verdict")
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    test_map = ctm.load_map(Path(args.map))
    merged = evaluate(merged_samples(args.merged, root), test_map, root) if args.merged else []
    cache = Path(args.cache) if args.cache else None
    if cache:
        cache.mkdir(parents=True, exist_ok=True)
    red = evaluate(red_samples(args.red, root, cache=cache), test_map, root) if args.red else []
    with open(args.out, "w", encoding="utf-8") as handle:
        for row in [
            *({"kind": "merged", **r} for r in merged),
            *({"kind": "red", **r} for r in red),
        ]:
            handle.write(json.dumps(row) + "\n")
    print(summarize(merged, red, test_map))
    return 0


if __name__ == "__main__":
    sys.exit(main())
