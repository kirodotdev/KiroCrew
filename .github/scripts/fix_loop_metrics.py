#!/usr/bin/env python3
"""Deterministic half of the weekly fix-loop analysis.

Computes every NUMBER the weekly report carries, so the model half only
classifies and narrates (same contract as ship-report.yml: counts are
deterministic, prose is the model's). Four metric groups over a trailing
window of the current branch's history:

  volume    -- commits, fix commits, fix share
  szz       -- per fix: blame the removed lines at <fix>^, dominant culprit
               commit -> squash trailer '(#N)' -> culprit PR; classes
               traced_to_pr / no_pr / add_only; bug age buckets + median
  deferrals -- open `deferred-finding` issues: tracked share (assignee +
               'Due: YYYY-MM-DD' body line or milestone due), overdue count
  harvest   -- merged fix-titled PRs in the window carrying a
               '## Pattern harvest' section. Scoped to PRs merged into the
               DEFAULT branch whose own hygiene check RAN under the gate: a
               release-branch PR ran that branch's workflow version and a PR
               whose hygiene check never ran was never asked, so neither is a
               non-adopter. Both are counted separately, leaving `escapes` to
               mean what it says.

Needs `git` (full-depth checkout) and `gh` (GH_TOKEN) on PATH. Emits JSON to
--out and a markdown table to --summary. Pure stdlib by design: the weekly
action must not grow a dependency install step.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import subprocess
import sys
from datetime import datetime, timezone

FIX_SUBJECT_RE = re.compile(r"^(fix|revert)([(!:]|\b)", re.I)
# Trailing '(#N)' of a squash-merge subject; the LAST number is the PR.
PR_TRAILER_RE = re.compile(r"\(#(\d+)\)\s*$")
# '-a,b' side of a unified hunk header; b omitted means 1, b=0 means pure add.
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+", re.MULTILINE)
DIFF_FILE_RE = re.compile(r"^--- a/(.+)$", re.MULTILINE)
BLAME_SHA_RE = re.compile(r"^([0-9a-f]{40})", re.MULTILINE)
DUE_RE = re.compile(r"^ *Due: *(\d{4}-\d{2}-\d{2})", re.MULTILINE)
AGE_BUCKETS = (("d0_1", 1), ("d1_3", 3), ("d3_7", 7), ("d7_30", 30), ("d30_plus", 10**9))
# Generated/vendored paths whose blame says nothing about authorship of a bug.
DIFF_EXCLUDES = [":!*.lock", ":!*.svg", ":!*.snap"]


def run(argv: list[str], *, check: bool = True) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"{argv[0]} failed ({proc.returncode}): {proc.stderr.strip()[:400]}")
    return proc.stdout


def git(*args: str, check: bool = True) -> str:
    return run(["git", *args], check=check)


def removed_ranges(fix_sha: str) -> dict[str, list[tuple[int, int]]]:
    """Per-file line ranges the fix removed from its parent."""
    out = git("show", "-U0", "--format=", fix_sha, "--", ".", *DIFF_EXCLUDES, check=False)
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in out.splitlines():
        m = DIFF_FILE_RE.match(line)
        if m:
            current = None if m.group(1) == "dev/null" else m.group(1)
            continue
        h = HUNK_RE.match(line)
        if h and current:
            start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) is not None else 1
            if count > 0:
                ranges.setdefault(current, []).append((start, start + count - 1))
    return ranges


def dominant_culprit(fix_sha: str, ranges: dict[str, list[tuple[int, int]]]) -> str | None:
    votes: dict[str, int] = {}
    for path, spans in ranges.items():
        for a, b in spans:
            out = git(
                "blame",
                "-l",
                "-w",
                "-M",
                "-C",
                f"-L{a},{b}",
                f"{fix_sha}^",
                "--",
                path,
                check=False,
            )
            for sha in BLAME_SHA_RE.findall(out):
                votes[sha] = votes.get(sha, 0) + 1
    if not votes:
        return None
    return max(votes, key=lambda s: votes[s])


def classify_fix(fix_sha: str) -> tuple[str, str | None, int | None]:
    """-> (class, culprit_pr, age_days). class: traced_to_pr | no_pr | add_only."""
    ranges = removed_ranges(fix_sha)
    if not ranges:
        return "add_only", None, None
    culprit = dominant_culprit(fix_sha, ranges)
    if culprit is None:
        return "add_only", None, None
    subject = git("log", "-1", "--pretty=%s", culprit).strip()
    fix_ts = int(git("log", "-1", "--pretty=%ct", fix_sha).strip())
    culprit_ts = int(git("log", "-1", "--pretty=%ct", culprit).strip())
    age_days = max(0, (fix_ts - culprit_ts) // 86400)
    m = PR_TRAILER_RE.search(subject)
    if m:
        return "traced_to_pr", m.group(1), age_days
    return "no_pr", None, age_days


def bucket(age_days: int) -> str:
    for name, ceiling in AGE_BUCKETS:
        if age_days < ceiling:
            return name
    return AGE_BUCKETS[-1][0]


def gh_json(*args: str) -> list | dict:
    """A failed gh call or unparseable payload RAISES: silently returning []
    would publish false zeros for deferral/harvest metrics, which is worse
    than no report (the caller's step fails and the run goes red honestly)."""
    out = run(["gh", *args])
    if not out.strip():
        return []
    return json.loads(out)


def deferral_metrics(repo: str) -> dict:
    issues = gh_json(
        "api", f"repos/{repo}/issues?state=open&labels=deferred-finding&per_page=100", "--paginate"
    )
    if isinstance(issues, dict):
        issues = [issues]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    total = len(issues)
    tracked = overdue = 0
    for it in issues:
        due_m = DUE_RE.search(it.get("body") or "")
        # GitHub's milestone object carries due_on with value null when unset
        # (key present, so a .get default never applies) -- guard before slicing.
        due = due_m.group(1) if due_m else ((it.get("milestone") or {}).get("due_on") or "")[:10]
        has_owner = bool(it.get("assignees"))
        if due and has_owner:
            tracked += 1
        if due and due < today:
            overdue += 1
    return {
        "open": total,
        "tracked": tracked,
        "tracked_pct": round(100 * tracked / total, 1) if total else None,
        "overdue": overdue,
    }


def default_branch(repo: str) -> str:
    """The repo's own default branch, so the harvest denominator is not a literal.

    Derived rather than hardcoded because it decides which PRs the gate is even
    expected to have run on: a release-branch PR runs the workflow version that
    lives on THAT branch.
    """
    data = gh_json("repo", "view", repo, "--json", "defaultBranchRef")
    name = ((data or {}).get("defaultBranchRef") or {}).get("name") or ""
    if not name:
        raise RuntimeError(f"could not resolve the default branch of {repo}")
    return str(name)


# When the Pattern harvest requirement began applying. Used to date the PR's own
# hygiene RUN, never the PR itself -- see gate_ran_on.
HARVEST_GATE_ACTIVE_ISO = "2026-08-31T08:01:43Z"

# The workflow that carries the hygiene gate, as its filename on disk.
GATE_WORKFLOW = "code-review.yml"


def gate_ran_on(repo: str, head_sha: str) -> bool:
    """Whether the harvest gate actually evaluated this PR.

    Applicability is derived from the RUN that carried the gate, not from when the
    PR was opened and not from when a check attempt started -- both are proxies
    that fail in opposite directions:

    * The PR's creation time excludes a PR opened before the rollout and pushed to
      afterwards. That PR DID get a gate-bearing run, so if it merged with no
      section it escaped, and excluding it publishes a false zero.
    * A check attempt's start time includes a PRE-gate run that was re-run (or
      queued) after activation. A re-run re-executes the ORIGINAL workflow
      version, so the gate was never in it, and including it manufactures an
      escape.

    A workflow run's ``created_at`` is the invariant both proxies miss: it is set
    once when the run is created and a re-run updates ``run_started_at`` instead.
    A `pull_request` run uses the workflow from the merge ref, so a run created
    after the gate merged necessarily carried the harvest step.

    False when no such run exists on the head -- a PR whose workflows are still
    awaiting maintainer approval was never asked.

    One API call per candidate PR (~100 a week). A failed call raises, per this
    module's contract: a report that silently drops a PR understates its own
    denominator.
    """
    if not head_sha:
        return False
    payload = gh_json(
        "api",
        f"repos/{repo}/actions/workflows/{GATE_WORKFLOW}/runs" f"?head_sha={head_sha}&per_page=100",
    )
    runs = payload.get("workflow_runs", []) if isinstance(payload, dict) else []
    return any(
        str(r.get("created_at") or "") >= HARVEST_GATE_ACTIVE_ISO
        for r in runs
        if r.get("created_at")
    )


# What the PR Hygiene gate accepts, transcribed from its own two `grep -qiE`
# patterns in `.github/workflows/code-review.yml`. They MUST stay identical: the
# gate is case-insensitive and space-tolerant, so a body reading
# `### pattern harvest` passes it -- and a stricter reading here would publish
# that PR as an escape, corrupting the one number this metric exists to make
# trustworthy. `test_fix_loop_discipline.py` extracts the gate's patterns and
# fails when these drift from them.
#
# Both are required, matching the gate: the heading alone is not a harvest, and a
# PR that merged without satisfying what the gate demands IS an escape.
HARVEST_HEADING_RE = re.compile(r"^##+ *Pattern harvest", re.MULTILINE | re.IGNORECASE)
HARVEST_ANSWER_RE = re.compile(
    r"^ *(Rule candidate|Not generalizable) *:", re.MULTILINE | re.IGNORECASE
)


def has_harvest_section(body: str) -> bool:
    """Whether *body* satisfies the same contract the hygiene gate enforces."""
    return bool(HARVEST_HEADING_RE.search(body) and HARVEST_ANSWER_RE.search(body))


def harvest_metrics(repo: str, since_iso: str) -> dict:
    """since_iso is the exact UTC timestamp of the window start; the gh search
    uses its date as a coarse pre-filter and mergedAt enforces the precise
    boundary, keeping this window identical to the git-history window."""
    prs = gh_json(
        "pr",
        "list",
        "--repo",
        repo,
        "--state",
        "merged",
        "--search",
        f"merged:>={since_iso[:10]}",
        "--limit",
        "1000",
        "--json",
        "title,body,mergedAt,baseRefName,headRefOid",
    )
    if len(prs) >= 1000:
        raise RuntimeError(
            "gh pr list hit its 1000-item cap; the harvest denominator would be "
            "truncated. Narrow the window or paginate before publishing."
        )
    prs = [p for p in prs if (p.get("mergedAt") or "") >= since_iso]
    fix_prs = [p for p in prs if FIX_SUBJECT_RE.match(p.get("title") or "")]

    # Two exclusions, both answering "was this PR ever asked for a section?".
    # A PR merged into a release branch ran that branch's workflow version, which
    # has no harvest step; a PR whose hygiene check never ran under the gate was
    # likewise never asked. Counting either as a non-adopter reports a hole that
    # does not exist -- the first audit of this mechanism misread the first case
    # and called two release backports escapes. They are reported separately so a
    # REAL escape still stands out.
    base = default_branch(repo)
    on_default = [p for p in fix_prs if (p.get("baseRefName") or "") == base]
    other_base = len(fix_prs) - len(on_default)
    in_scope = [p for p in on_default if gate_ran_on(repo, str(p.get("headRefOid") or ""))]
    never_asked = len(on_default) - len(in_scope)
    with_section = sum(1 for p in in_scope if has_harvest_section(p.get("body") or ""))
    return {
        "merged_fix_prs": len(in_scope),
        "with_harvest_section": with_section,
        "adoption_pct": round(100 * with_section / len(in_scope), 1) if in_scope else None,
        # A non-zero count here is the signal to investigate: the gate ran on
        # these PRs, it is required on this branch, and they merged anyway.
        "escapes": len(in_scope) - with_section,
        "excluded_other_base": other_base,
        "excluded_gate_never_ran": never_asked,
        "default_branch": base,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", required=True, help="git --since expression, e.g. '7 days ago'")
    ap.add_argument(
        "--since-iso", required=True, help="exact UTC window start, e.g. 2026-08-23T17:00:00Z"
    )
    ap.add_argument("--repo", required=True, help="owner/name for gh calls")
    ap.add_argument("--max-fixes", type=int, default=600, help="SZZ cap per run")
    ap.add_argument("--out", required=True, help="JSON output path")
    ap.add_argument("--summary", required=True, help="markdown table output path")
    ap.add_argument(
        "--bodies-out",
        help="optional path: dump the ANALYZED fix commits (hash, subject, full "
        "body) so a later model step can classify exactly the set these metrics "
        "count, without needing git access of its own",
    )
    args = ap.parse_args()

    total = int(
        git("rev-list", "--count", "--no-merges", f"--since={args.since}", "HEAD").strip() or 0
    )
    # Subject-only fix detection: a full-message --grep also matches merge
    # bodies and commits merely MENTIONING a fix, double-counting volume and
    # feeding non-fixes to SZZ.
    log = git("log", "--no-merges", f"--since={args.since}", "--pretty=%H\t%s")
    fixes = [
        line.split("\t", 1)[0]
        for line in log.splitlines()
        if FIX_SUBJECT_RE.match(line.split("\t", 1)[1] if "\t" in line else "")
    ]
    capped = fixes[: args.max_fixes]

    if args.bodies_out:
        with open(args.bodies_out, "w", encoding="utf-8") as f:
            for sha in capped:
                subject = git("log", "-1", "--pretty=%s", sha).strip()
                body = git("log", "-1", "--pretty=%b", sha).rstrip()
                f.write(f"=== {sha[:7]} {subject}\n{body}\n\n")

    classes = {"traced_to_pr": 0, "no_pr": 0, "add_only": 0}
    ages: list[int] = []
    age_counts = {name: 0 for name, _ in AGE_BUCKETS}
    culprit_votes: dict[str, int] = {}
    for sha in capped:
        cls, culprit_pr, age = classify_fix(sha)
        classes[cls] += 1
        if age is not None:
            ages.append(age)
            age_counts[bucket(age)] += 1
        if culprit_pr:
            culprit_votes[culprit_pr] = culprit_votes.get(culprit_pr, 0) + 1

    top_culprits = sorted(culprit_votes.items(), key=lambda kv: -kv[1])[:10]
    result = {
        "window_since": args.since,
        "volume": {
            "commits": total,
            "fix_commits": len(fixes),
            "fix_pct": round(100 * len(fixes) / total, 1) if total else None,
            "szz_analyzed": len(capped),
            "szz_capped": len(capped) < len(fixes),
        },
        "szz": {
            "classes": classes,
            "age_buckets": age_counts,
            "median_age_days": statistics.median(ages) if ages else None,
        },
        "top_culprit_prs": [{"pr": f"#{n}", "fixes": c} for n, c in top_culprits],
        "deferrals": deferral_metrics(args.repo),
        "harvest": harvest_metrics(args.repo, args.since_iso),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    vol, szz, dfr, hrv = result["volume"], result["szz"], result["deferrals"], result["harvest"]

    def pct(value: object) -> str:
        """Render a percentage, or `n/a` when the denominator was zero.

        A `None` percentage formatted straight into the table published `None%`,
        which reads as a measurement rather than as an empty denominator.
        """
        return "n/a" if value is None else f"{value}%"

    lines = [
        "| Metric | Value |",
        "|---|---|",
        f"| Commits in window | {vol['commits']} |",
        f"| Fix commits | {vol['fix_commits']} ({pct(vol['fix_pct'])}) |",
        f"| SZZ coverage | {vol['szz_analyzed']}/{vol['fix_commits']}"
        f"{' (CAPPED — trends only, not totals)' if vol['szz_capped'] else ' (full)'} |",
        f"| SZZ: introduced by reviewed PR | {szz['classes']['traced_to_pr']} |",
        f"| SZZ: no-PR code | {szz['classes']['no_pr']} |",
        f"| SZZ: pure addition (omission) | {szz['classes']['add_only']} |",
        f"| Median bug age (days) | {szz['median_age_days']} |",
        f"| Deferred findings open / tracked / overdue | "
        f"{dfr['open']} / {dfr['tracked']} ({pct(dfr['tracked_pct'])}) / {dfr['overdue']} |",
        f"| Harvest-section adoption on fix PRs | "
        f"{hrv['with_harvest_section']}/{hrv['merged_fix_prs']} ({pct(hrv['adoption_pct'])}) |",
        # Escapes are the actionable half: on the default branch the gate is
        # required, so a gap means it did not run and wants investigating.
        f"| Harvest escapes (default-branch fix PRs with no section) | {hrv['escapes']} |",
        f"| Excluded: another base / gate never ran | "
        f"{hrv['excluded_other_base']} / {hrv['excluded_gate_never_ran']} |",
    ]
    with open(args.summary, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"metrics written: {args.out} ({len(capped)} fixes SZZ-traced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
