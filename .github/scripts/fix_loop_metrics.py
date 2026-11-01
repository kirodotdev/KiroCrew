#!/usr/bin/env python3
"""Deterministic half of the weekly fix-loop analysis.

Computes every NUMBER the weekly report carries, so the model half only
classifies and narrates (same contract as ship-report.yml: counts are
deterministic, prose is the model's). Five metric groups over a trailing
window of the current branch's history:

  volume    -- commits, fix commits, fix share
  szz       -- per fix: blame the removed lines at <fix>^, dominant culprit
               commit -> squash trailer '(#N)' -> culprit PR; classes
               traced_to_pr / no_pr / add_only; bug age buckets + median
  deferrals -- open `deferred-finding` issues: tracked share (assignee +
               'Due: YYYY-MM-DD' body line or milestone due), overdue count
  overrides -- `/ai-review override` records the handler posted in the window:
               total, count per class, how many the PR author recorded about
               their own PR, `deferred` records with no issue, and every
               `owner-decision` (listed). A record from before classes existed
               counts as `unclassified`, the old command as `legacy`.
  harvest   -- merged fix-titled PRs in the window carrying a
               '## Pattern harvest' section. Scoped to PRs merged into the
               DEFAULT branch whose own hygiene check RAN under the gate: a
               release-branch PR ran that branch's workflow version and a PR
               whose hygiene check never ran was never asked, so neither is a
               non-adopter. Both are counted separately, leaving `escapes` to
               mean what it says.

Two more groups measure whether the review-loop changes worked (RFC
docs/request-for-change/rfc-review-loop-and-escaped-bug-reduction.md, D8):

  pr_counters -- size flags (D6). Read from the merged-PR list the harvest
               already fetches: no API call of its own.
  ci_waste   -- the CI-waste census (M1 to M3). OPT-IN (`--ci-waste true`), a
               HARD cap on API calls (`--ci-waste-max-calls`), newest N merged
               PRs only (`--ci-waste-sample`). Details and the call budget:
               docs/ci/ci-and-reviews.md, "The weekly fix-loop report".

Needs `git` (full-depth checkout) and `gh` (GH_TOKEN) on PATH. Emits JSON to
--out and a markdown table to --summary. Pure stdlib by design: the weekly
action must not grow a dependency install step.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import statistics
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

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
        # ONE blame per file, every removed hunk as its own -L. Blame walks the
        # file's history once per invocation regardless of how many lines -L
        # asks for, so the per-hunk form paid that walk again for every hunk of
        # the same file (256 hunks across 67 files in a 20-fix sample: 2.6x the
        # wall time for byte-identical culprits). -M/-C stay: dropping copy
        # detection changes the dominant culprit on ~10% of fixes.
        out = git(
            "blame",
            "-l",
            "-w",
            "-M",
            "-C",
            *(f"-L{a},{b}" for a, b in spans),
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


def classify_fixes(shas: list[str]) -> list[tuple[str, str | None, int | None]]:
    """classify_fix over every sha, in input order, a few fixes at a time.

    Each fix is an independent chain of git subprocesses, so the work is
    embarrassingly parallel and the GIL is irrelevant. The pool is bounded by
    the CPU count: blame is CPU-bound in git itself, so more workers than cores
    only adds contention. Order is preserved so the output is deterministic
    regardless of which fix finishes first.
    """
    workers = max(1, min(8, os.cpu_count() or 1))
    if workers == 1 or len(shas) < 2:
        return [classify_fix(s) for s in shas]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(classify_fix, shas))


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


# The override record, as `ai-review-human-override.yml` writes it: line 1 is the
# marker every lane clears on, line 2 the class record. Transcribed from
# `_review_contract.OVERRIDE_MARKER_RE` / `OVERRIDE_CLASS_RE` because this script
# is stdlib-only and cannot import the skill; a test pins the two together.
OVERRIDE_BOT = "github-actions[bot]"
OVERRIDE_MARKER_RE = re.compile(
    r"\A<!-- ai-review-human-override target=([a-z0-9-]+) head=([0-9a-fA-F]{7,40})"
    r" actor=(\S+) source=([0-9]+) -->"
)
OVERRIDE_CLASS_RE = re.compile(
    r"\A[^\n]*\n<!-- ai-review-override-record class=([a-z-]+) issue=([0-9]+|none)"
    r" author_is_actor=(true|false) -->"
)
OWNER_DECISION_LISTED = 25


def json_values(text: str) -> list:
    """Every top-level JSON value in ``text``, flattened one level when it is a list.

    `gh api --paginate` prints one JSON array per page; depending on the gh
    version they arrive as one merged array or back to back. Both parse.
    """
    decoder = json.JSONDecoder()
    values: list = []
    pos = 0
    while pos < len(text):
        while pos < len(text) and text[pos].isspace():
            pos += 1
        if pos >= len(text):
            break
        value, pos = decoder.raw_decode(text, pos)
        values.extend(value if isinstance(value, list) else [value])
    return values


def override_record(comment: dict) -> dict | None:
    """The class record of a bot-authored override comment, else None.

    Authority is the bot authorship, as in `parse_override_record`: the same
    bytes from a person's account are not a record and are not counted.
    """
    if (comment.get("user") or {}).get("login") != OVERRIDE_BOT:
        return None
    body = comment.get("body") or ""
    marker = OVERRIDE_MARKER_RE.match(body)
    if not marker:
        return None
    cls = OVERRIDE_CLASS_RE.match(body)
    if not cls:
        return {
            "class": "unclassified",
            "issue": "",
            "author_is_actor": None,
            "actor": marker.group(3),
        }
    return {
        "class": cls.group(1),
        "issue": "" if cls.group(2) == "none" else cls.group(2),
        "author_is_actor": cls.group(3) == "true",
        "actor": marker.group(3),
    }


def override_metrics(repo: str, since_iso: str) -> dict:
    """Count the override records the handler posted since ``since_iso``.

    One repository-wide read of issue comments (every PR comment is an issue
    comment), filtered on bot authorship and the marker. A failed read raises,
    like every other group: a false zero here would read as "nobody overrode".
    """
    path = f"repos/{repo}/issues/comments?since={since_iso}&per_page=100"
    out = run(["gh", "api", path, "--paginate"])
    comments = json_values(out) if out.strip() else []
    by_class: dict[str, int] = {}
    total = self_authored = deferred_without_issue = 0
    owner_decisions: list[dict] = []
    for comment in comments:
        # `since` selects on the last UPDATE; the window is on creation.
        if (comment.get("created_at") or "") < since_iso:
            continue
        record = override_record(comment)
        if record is None:
            continue
        total += 1
        by_class[record["class"]] = by_class.get(record["class"], 0) + 1
        if record["author_is_actor"]:
            self_authored += 1
        if record["class"] == "deferred" and not record["issue"]:
            deferred_without_issue += 1
        if record["class"] == "owner-decision":
            owner_decisions.append(
                {
                    "pr": "#" + (comment.get("issue_url") or "").rsplit("/", 1)[-1],
                    "actor": record["actor"],
                    "url": comment.get("html_url") or "",
                }
            )
    return {
        "overrides_total": total,
        "overrides_by_class": dict(sorted(by_class.items())),
        "overrides_self_authored": self_authored,
        "overrides_deferred_without_issue": deferred_without_issue,
        "owner_decisions": owner_decisions,
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


def _local_pr_evidence(since_iso: str) -> bool | None:
    """Whether the checkout's own history shows PR-merged commits since *since_iso*.

    Read-only and cheap: a squash merge leaves a '(#N)' subject trailer and a
    merge commit answers `--merges`; either is proof PRs merged in the window.
    Returns None when git cannot answer authoritatively (shallow checkout, git
    unavailable), so the caller falls back to a coarser rule.

    Deliberately scoped to the checked-out branch: the scheduled run checks
    out the default branch, which is also the harvest's own scope, and
    scanning every ref would let a feature branch's merge commits manufacture
    evidence against a legitimately quiet window. Known edge: `--since`
    filters on committer date, so a backport cherry-picked onto the checked-
    out branch keeps its '(#N)' subject with a fresh date and reads as
    evidence -- a dispatch run against a release branch can fail loud on a
    quiet window, which is the accepted cost of never publishing a false
    zero on the branch the metric is actually about.
    """
    try:
        if run(["git", "rev-parse", "--is-shallow-repository"]).strip() != "false":
            return None
        subjects = run(["git", "log", f"--since={since_iso}", "--format=%s"])
        if any(PR_TRAILER_RE.search(s) for s in subjects.splitlines()):
            return True
        return bool(run(["git", "log", "--merges", f"--since={since_iso}", "--oneline"]).strip())
    except (OSError, RuntimeError):
        return None


# The fields of the merged-PR search. The D8 counters (`pr_counters`) read size,
# open time and author from the SAME list the harvest already pays for, so they
# add scalar fields to an existing GraphQL query and no call of their own.
PR_LIST_FIELDS = "number,title,body,mergedAt,createdAt,changedFiles,author,baseRefName,headRefOid"


def merged_prs_since(repo: str, since_iso: str) -> list[dict]:
    """Every PR merged at or after since_iso, one search per calendar day.

    GitHub search returns at most 1000 items per query and the repo merges
    ~950 PRs a week, so a single `merged:>=<date>` search overflows on any
    window past seven days (and will on a busy seven). Per-day chunks stay far
    under the cap; each is still checked, because a day that overflows would
    silently truncate the denominator this metric exists to make trustworthy.
    """
    start = datetime.strptime(since_iso[:10], "%Y-%m-%d").date()
    end = datetime.now(timezone.utc).date()
    prs: list[dict] = []
    seen: set[str] = set()
    day = start
    while day <= end:
        chunk = gh_json(
            "pr",
            "list",
            "--repo",
            repo,
            "--state",
            "merged",
            "--search",
            f"merged:{day.isoformat()}",
            "--limit",
            "1000",
            "--json",
            PR_LIST_FIELDS,
        )
        if len(chunk) >= 1000:
            raise RuntimeError(
                f"gh pr list hit its 1000-item cap for merged:{day}; the harvest "
                "denominator would be truncated. Chunk finer before publishing."
            )
        for p in chunk:
            # A PR merged near midnight UTC can be returned by two adjacent
            # day searches; identity is the PR number, head sha as fallback.
            key = str(p.get("number") or p.get("headRefOid") or "")
            if key and key not in seen:
                seen.add(key)
                prs.append(p)
        day += timedelta(days=1)
    window = [p for p in prs if (p.get("mergedAt") or "") >= since_iso]
    if not window:
        # An empty window is legitimate only when the checkout agrees. The
        # search surface omits nodes the token cannot read instead of
        # erroring, so a permissions gap looks exactly like a quiet week --
        # and this metric's whole contract is to never publish that.
        evidence = _local_pr_evidence(since_iso)
        if evidence:
            raise RuntimeError(
                f"gh pr list returned zero merged PRs since {since_iso}, yet the "
                "checkout's own history shows commits that merged through PRs. "
                "The search surface omits nodes the token cannot read instead "
                "of erroring, so the probable cause is a token without "
                "`pull-requests: read` -- failing loud rather than publishing "
                "a false zero."
            )
        if evidence is None and (end - start).days >= 1:
            raise RuntimeError(
                f"gh pr list returned zero merged PRs since {since_iso} over a "
                "window of at least one full day, and git cannot confirm the "
                "window is empty (shallow checkout or git unavailable). Either "
                "the window is genuinely quiet or the token lacks "
                "`pull-requests: read`; re-run on a full-depth checkout to tell "
                "the two apart rather than publishing a possible false zero."
            )
    return window


def harvest_metrics(repo: str, since_iso: str, prs: list[dict]) -> dict:
    """since_iso is the exact UTC timestamp of the window start; *prs* is the
    window's merged-PR list from `merged_prs_since`, fetched once by `main` and
    shared with the D8 counters and the census. Its per-day gh searches use the
    date as a coarse pre-filter and mergedAt enforces the precise boundary,
    keeping this window identical to the git-history window."""
    fix_prs = [p for p in prs if FIX_SUBJECT_RE.match(p.get("title") or "")]

    # Two exclusions, both answering "was this PR ever asked for a section?".
    # A PR merged into a release branch ran that branch's workflow version, which
    # has no harvest step; a PR whose hygiene check never ran under the gate was
    # likewise never asked. Counting either as a non-adopter reports a hole that
    # does not exist: a release backport would read as an escape. They are
    # reported separately so a REAL escape still stands out.
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


# ---------------------------------------------------------------------------
# D8 measures (docs/request-for-change/rfc-review-loop-and-escaped-bug-reduction.md,
# section 4.8). Two groups, kept apart from the groups above so a change to one
# cannot move the other:
#
#   pr_counters -- size counters read from the merged-PR list the harvest
#                  already fetched. NO API CALL of their own.
#   ci_waste    -- the CI-waste census (M1 to M3), opt-in, with a hard cap on API
#                  calls.
#
# Every rate is published beside its raw counts: a measure that turns into a
# target drifts, and the counts are what let a reader see the denominator move.
# ---------------------------------------------------------------------------

# D6 thresholds (RFC Appendix F). Files and hours open only: lines are the wrong
# signal there. A PR is "flagged" at 80 files or 168 h open.
SIZE_HINT_FILES = 50
SIZE_FLAG_FILES = 80
AGE_FLAG_HOURS = 168

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\[bot\])?$")


def ratio_pct(part: int, whole: int) -> float | None:
    """One-decimal percentage, or None for an empty denominator (never 0.0%)."""
    return round(100 * part / whole, 1) if whole else None


def parse_utc(stamp: object) -> datetime | None:
    """A GitHub timestamp as an aware UTC datetime, or None when absent or odd."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def author_login(pr: dict) -> str:
    return str((pr.get("author") or {}).get("login") or "").lower()


def is_fix_pr(pr: dict) -> bool:
    return bool(FIX_SUBJECT_RE.match(pr.get("title") or ""))


def size_counters(prs: list[dict]) -> dict:
    """D6: how many merged PRs the size rule would have flagged.

    Computed from changed files and created-to-merged hours, so it needs no
    comment read. The owner approvals (`/pr-size approve`) are comments and are
    NOT counted here.
    """
    hint = stop = aged = flagged = unknown = 0
    for pr in prs:
        files = pr.get("changedFiles")
        if not isinstance(files, int):
            unknown += 1
            files = 0
        created, merged = parse_utc(pr.get("createdAt")), parse_utc(pr.get("mergedAt"))
        hours = (merged - created).total_seconds() / 3600 if created and merged else 0.0
        hint += files >= SIZE_HINT_FILES
        stop += files >= SIZE_FLAG_FILES
        aged += hours >= AGE_FLAG_HOURS
        flagged += files >= SIZE_FLAG_FILES or hours >= AGE_FLAG_HOURS
    return {
        "files_ge_hint": hint,
        "files_ge_flag": stop,
        "open_ge_flag_hours": aged,
        "flagged": flagged,
        "flagged_pct": ratio_pct(flagged, len(prs)),
        "size_unknown": unknown,
    }


def pr_counters(prs: list[dict]) -> dict:
    """Counters over the window's merged PRs that need no API call (see above).

    Only D6's size rule is counted today. Adoption counters for D3 (sibling
    sweep), D4 (`Proof:` line) and D7 (`Gate proof:`, M4, M5) arrive with the
    change that first writes each line: counting a format nothing emits yet
    would publish a known zero and fix the format before it is decided.
    """
    return {
        "merged_prs": len(prs),
        "fix_prs": sum(1 for p in prs if is_fix_pr(p)),
        "size": size_counters(prs),
    }


# --- The CI-waste census (M1 to M3) ----------------------------------------
#
# The logic of the study's `waste.py` (design F section 1), run over the newest
# merged PRs of the window. A push is AVOIDABLE when every red job on it is a job
# with a local command (`LOCAL_COMMAND_JOBS`) and the author pushed again later:
# the whole push re-ran everything, so all of it is waste.
#
# Two differences from the study, both stated in docs/ci/ci-and-reviews.md:
#   * The study dropped 27 of 219 such pushes whose loop record named main,
#     flaky, infra or rebase. No loop records exist here, so the live M1/M2 are
#     the 219-push reading (about 14% above the published 192-push baseline).
#   * Job-minutes are the sum of check-run durations (completed_at - started_at)
#     of a head's GitHub Actions jobs: not billed minutes, no per-OS multiplier.

# Jobs whose check has a local command (waste.py T1_EXACT). The names are CI's;
# a rename in a workflow silently shrinks M1, so `test_fix_loop_metrics_d8.py`
# pins every name here to a `name:` in .github/workflows.
LOCAL_COMMAND_JOBS = frozenset(
    {
        "Backend Lint & Type Check (3.12)",
        "Backend Lint & Type Check",
        "Frontend Lint & Type Check",
        # ci.yml's baselined ratchets, off lint-gate's path. Each step runs a
        # script a contributor runs locally, so a push red only here is as
        # avoidable as one red only in a lint job.
        "Backend Ratchets & Boundaries",
        "Frontend Ratchets & i18n",
        # fast-gate.yml's fourteen stdlib gates run as steps of this one job,
        # so a red on any of them reaches the census under this single name.
        "Source Gates",
        "Static Ratchet Gate",
        "CloudFormation Lint",
        "PR Hygiene",
        "Bundle Size Gate",
    }
)
# Jobs that go red only because an upstream job did (waste.py DERIVED).
DERIVED_JOBS = frozenset(
    {
        "Await Fast Gate",
        "PR Readiness",
        "Publish readiness signal",
        "Coverage Gate",
        "Frontend Coverage Merge",
        "Coverage Combine",
    }
)
Fetch = Callable[..., Any]  # gh_json's shape: fetch("api", "<path>") -> parsed JSON
# Census defaults. The workflow's own defaults must equal these (pinned by
# test_fix_loop_metrics_d8.py); the cap is the number the docs promise.
CENSUS_DEFAULT_SAMPLE = 100
CENSUS_DEFAULT_MAX_CALLS = 1000
CENSUS_DEFAULT_RESERVE = 200
FORCE_PUSH_EVENT = "head_ref_force_pushed"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
API_PAGE_SIZE = 100
# Pages read per endpoint per head or PR. A head with more than this many check
# runs is counted and reported as truncated, never silently cut.
API_MAX_PAGES = 3
ACTIONS_APP_SLUG = "github-actions"
# A job that timed out is red too: it must stop a lint-only push counting as avoidable.
RED_CONCLUSIONS = frozenset({"failure", "timed_out"})
TOP_FAILED_JOBS = 5


class BudgetExhausted(Exception):
    """The census asked for one more API call than its cap allows."""


class CallBudget:
    """A hard cap on API calls. `spend` is called BEFORE each call and raises when
    the cap is reached, so `used` can never exceed `cap`."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.used = 0

    def spend(self) -> None:
        if self.used >= self.cap:
            raise BudgetExhausted(f"API call cap of {self.cap} reached")
        self.used += 1


def summarise_head(sha: str, check_runs: list[dict]) -> dict | None:
    """One pushed head, from its check runs: when it started, what it cost, what
    was red. None when the head has no GitHub Actions job (nothing ran on it)."""
    jobs = [
        c
        for c in check_runs
        if isinstance(c, dict) and (c.get("app") or {}).get("slug") == ACTIONS_APP_SLUG
    ]
    if not jobs:
        return None
    seconds = 0.0
    starts: list[float] = []
    failed: list[tuple[str, float]] = []
    for job in jobs:
        began, ended = parse_utc(job.get("started_at")), parse_utc(job.get("completed_at"))
        duration = max(0.0, (ended - began).total_seconds()) if began and ended else 0.0
        if began:
            starts.append(began.timestamp())
        seconds += duration
        if job.get("conclusion") in RED_CONCLUSIONS:
            failed.append((str(job.get("name") or "?"), duration))
    red = [(n, d) for n, d in failed if n not in DERIVED_JOBS]
    return {
        "sha": sha,
        "start": min(starts) if starts else None,
        "seconds": seconds,
        "red_local": [(n, d) for n, d in red if n in LOCAL_COMMAND_JOBS],
        "red_other": [n for n, _ in red if n not in LOCAL_COMMAND_JOBS],
    }


def is_static_only(head: dict) -> bool:
    """Every red job (derived ones aside) has a local command, and there is one."""
    return bool(head["red_local"]) and not head["red_other"]


def waste_for_pr(heads: list[dict]) -> dict:
    """The census record of one PR from its observed heads.

    A head is *superseded* when the PR has a later-starting head, i.e. the author
    had to push again. `avoidable` = static-only red AND superseded. M3 looks at
    the earliest-starting head only.
    """
    timed = sorted((h for h in heads if h["start"] is not None), key=lambda h: h["start"])
    last_start = timed[-1]["start"] if timed else None
    avoidable = [
        h for h in timed if is_static_only(h) and last_start is not None and h["start"] < last_start
    ]
    return {
        "pushes": len(heads),
        "avoidable": len(avoidable),
        "avoidable_seconds": sum(h["seconds"] for h in avoidable),
        "avoidable_floor_seconds": sum(d for h in avoidable for _, d in h["red_local"]),
        "first_push_red_local": bool(timed and timed[0]["red_local"]),
        "failed_jobs": [n for h in avoidable for n, _ in h["red_local"]],
    }


def mean_ci95_per_100(counts: list[int]) -> tuple[float, float] | None:
    """95% interval of the mean count per PR, scaled to 100 PRs; None under 2 PRs.

    Plain normal approximation. Design F measured mean 0.097 and sd 0.386 per PR,
    so 200 PRs give about +-55%: the interval is published to say so.
    """
    n = len(counts)
    if n < 2:
        return None
    mean = sum(counts) / n
    sd = math.sqrt(sum((c - mean) ** 2 for c in counts) / (n - 1))
    half = 1.96 * sd / math.sqrt(n)
    return round(max(0.0, mean - half) * 100, 1), round((mean + half) * 100, 1)


def aggregate_waste(records: list[dict]) -> dict:
    """M1 to M3 over the PRs whose census finished (every head read)."""
    n = len(records)
    with_push = [r for r in records if r["pushes"]]
    avoidable = sum(r["avoidable"] for r in records)
    seconds = sum(r["avoidable_seconds"] for r in records)
    floor = sum(r["avoidable_floor_seconds"] for r in records)
    first_red = sum(1 for r in with_push if r["first_push_red_local"])
    jobs: dict[str, int] = {}
    for r in records:
        for name in r["failed_jobs"]:
            jobs[name] = jobs.get(name, 0) + 1
    top = sorted(jobs.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP_FAILED_JOBS]
    return {
        "prs": n,
        "prs_with_push": len(with_push),
        "pushes": sum(r["pushes"] for r in records),
        "avoidable_pushes": avoidable,
        "prs_with_avoidable": sum(1 for r in records if r["avoidable"]),
        "m1_per_100_prs": round(100 * avoidable / n, 1) if n else None,
        "m1_ci95": mean_ci95_per_100([r["avoidable"] for r in records]),
        "avoidable_job_minutes": round(seconds / 60, 1),
        "avoidable_floor_job_minutes": round(floor / 60, 1),
        "m2_job_minutes_per_pr": round(seconds / 60 / n, 1) if n else None,
        "m3_first_push_red_prs": first_red,
        "m3_pct": ratio_pct(first_red, len(with_push)),
        "failed_local_jobs": dict(top),
    }


def pushed_heads(final_head: object, commits: list[dict], events: list[dict]) -> list[str]:
    """Every head sha a PR is known to have had, de-duplicated, in discovery order.

    PR commits cover plain pushes. Force pushes (the agent flow amends and
    force-pushes) leave only the event `head_ref_force_pushed`, whose commit_id
    is the head it created. The final head comes from the PR list. Anything that
    is not a 40-hex sha is dropped: the value goes into an API path.
    """
    found: list[str] = []

    def add(sha: object) -> None:
        if isinstance(sha, str) and SHA_RE.match(sha.lower()) and sha.lower() not in found:
            found.append(sha.lower())

    for c in commits:
        add(c.get("sha") if isinstance(c, dict) else None)
    for e in events:
        if isinstance(e, dict) and e.get("event") == FORCE_PUSH_EVENT:
            add(e.get("commit_id"))
    add(final_head)
    return found


def _pages(
    budget: CallBudget, fetch: Fetch, path: str, key: str | None = None
) -> tuple[list[dict], int]:
    """Read up to API_MAX_PAGES pages of *path*; every page is one budgeted call.

    -> (items, total). With *key* (check-runs) `total` is the server's
    total_count, so a caller can tell the read was cut; for a plain list it is
    just the item count.
    """
    items: list[dict] = []
    total = 0
    for page in range(1, API_MAX_PAGES + 1):
        budget.spend()
        payload = fetch("api", f"{path}?per_page={API_PAGE_SIZE}&page={page}")
        chunk = payload.get(key, []) if key and isinstance(payload, dict) else payload
        chunk = chunk if isinstance(chunk, list) else []
        if key and isinstance(payload, dict):
            total = int(payload.get("total_count") or 0)
        items.extend(c for c in chunk if isinstance(c, dict))
        if len(chunk) < API_PAGE_SIZE or (key and len(items) >= total):
            break
    return items, (total if key else len(items))


def census_one_pr(repo: str, pr: dict, budget: CallBudget, fetch: Fetch) -> tuple[dict, dict]:
    """Read one PR's heads and their check runs -> (record, notes).

    Two calls find the heads, then 1-3 per head. A head GitHub no longer serves
    (404/422) is skipped and counted, as is one with no GitHub Actions job (a
    force-pushed head whose runs are gone). Both make the push count a lower
    bound, which the report states. Any other failure raises, per this module's
    contract.
    """
    number = int(pr["number"])
    commits, _ = _pages(budget, fetch, f"repos/{repo}/pulls/{number}/commits")
    events, _ = _pages(budget, fetch, f"repos/{repo}/issues/{number}/events")
    heads: list[dict] = []
    notes = {"heads_unreadable": 0, "heads_truncated": 0, "heads_without_jobs": 0}
    for sha in pushed_heads(pr.get("headRefOid"), commits, events):
        try:
            runs, total = _pages(
                budget, fetch, f"repos/{repo}/commits/{sha}/check-runs", "check_runs"
            )
        except RuntimeError as exc:
            if "HTTP 404" in str(exc) or "HTTP 422" in str(exc):
                notes["heads_unreadable"] += 1
                continue
            raise
        if total > len(runs):
            notes["heads_truncated"] += 1
        summary = summarise_head(sha, runs)
        if summary:
            heads.append(summary)
        else:
            notes["heads_without_jobs"] += 1
    return waste_for_pr(heads), notes


def rate_limit_remaining(fetch: Fetch) -> int | None:
    """Remaining REST calls of this token, or None when the probe cannot say.

    The probe is a guard, not a number the report publishes, so a failure here
    degrades to the configured cap instead of failing the run.
    """
    try:
        payload = fetch("api", "rate_limit")
        return int(payload["resources"]["core"]["remaining"])
    except (RuntimeError, KeyError, TypeError, ValueError):
        return None


def ci_waste_census(
    repo: str,
    prs: list[dict],
    *,
    sample: int,
    max_calls: int,
    reserve: int,
    agent_logins: frozenset[str] = frozenset(),
    fetch: Fetch | None = None,
) -> dict:
    """M1 to M3 over the newest *sample* merged PRs, within *max_calls* API calls.

    The cap is hard: `CallBudget` refuses the call that would pass it, and the PR
    being read at that moment is dropped whole, so a half-read PR never enters a
    rate. The effective cap is also held below the token's remaining hourly
    allowance minus *reserve*, because GITHUB_TOKEN's pool is shared with
    pr-readiness.yml. Spent calls are reported either way.
    """
    fetch = fetch or gh_json
    budget = CallBudget(max_calls)
    budget.spend()  # the rate-limit probe is a call too
    remaining = rate_limit_remaining(fetch)
    configured = budget.cap
    if remaining is not None:
        budget.cap = min(budget.cap, max(budget.used, remaining - reserve))
    newest = sorted(prs, key=lambda p: p.get("mergedAt") or "", reverse=True)[:sample]
    done: list[tuple[dict, dict]] = []
    totals = {"heads_unreadable": 0, "heads_truncated": 0, "heads_without_jobs": 0}
    truncated = False
    for pr in newest:
        try:
            record, notes = census_one_pr(repo, pr, budget, fetch)
        except BudgetExhausted:
            truncated = True
            break
        record["author"] = author_login(pr)
        done.append((record, notes))
        for key in totals:
            totals[key] += notes[key]
    records = [r for r, _ in done]
    agents = [r for r in records if agent_logins and r["author"] in agent_logins]
    return {
        "enabled": True,
        "sample_requested": sample,
        "prs_in_window": len(prs),
        "prs_sampled": len(records),
        "truncated_by_budget": truncated,
        "calls_used": budget.used,
        "calls_cap_configured": configured,
        "calls_cap_effective": budget.cap,
        "rate_limit_remaining_before": remaining,
        "rate_limit_reserve": reserve,
        **totals,
        "all_authors": aggregate_waste(records),
        "agents": aggregate_waste(agents) if agent_logins else None,
    }


def d8_table_rows(counters: dict, waste: dict) -> list[str]:
    """Markdown rows for the summary table. Every rate sits beside its counts."""

    def num(value: object) -> str:
        return "n/a" if value is None else str(value)

    def pct(value: object) -> str:
        return "n/a" if value is None else f"{value}%"

    size = counters["size"]
    fixes = counters["fix_prs"]
    rows = [
        f"| Merged PRs in window / fix or revert titles | {counters['merged_prs']} / {fixes} |",
        f"| Size: flagged (>= {SIZE_FLAG_FILES} files or >= {AGE_FLAG_HOURS} h open) | "
        f"{size['flagged']} ({pct(size['flagged_pct'])}); >= {SIZE_HINT_FILES} files: "
        f"{size['files_ge_hint']}, >= {SIZE_FLAG_FILES} files: {size['files_ge_flag']}, "
        f">= {AGE_FLAG_HOURS} h: {size['open_ge_flag_hours']} |",
    ]
    if not waste.get("enabled"):
        rows.append("| CI-waste census (M1 to M3) | off (opt-in; see docs/ci/ci-and-reviews.md) |")
        return rows
    rows.append(
        f"| CI-waste census: PRs read / requested / in window | {waste['prs_sampled']} / "
        f"{waste['sample_requested']} / {waste['prs_in_window']} |"
    )
    rows.append(
        f"| CI-waste census: API calls used / cap "
        f"{'(CUT SHORT by the cap)' if waste['truncated_by_budget'] else '(finished)'} | "
        f"{waste['calls_used']} / {waste['calls_cap_effective']} "
        f"(configured {waste['calls_cap_configured']}) |"
    )
    rows.append(
        f"| CI-waste census: heads with no jobs / unreadable / listing cut (push counts are "
        f"lower bounds) | {waste['heads_without_jobs']} / {waste['heads_unreadable']} / "
        f"{waste['heads_truncated']} |"
    )
    for label, group in (("all authors", waste["all_authors"]), ("agent logins", waste["agents"])):
        if not group:
            continue
        ci = group["m1_ci95"]
        interval = f"; 95% interval {ci[0]} to {ci[1]}" if ci else ""
        rows += [
            f"| M1 Avoidable pushes per 100 merged PRs ({label}) | {num(group['m1_per_100_prs'])} "
            f"({group['avoidable_pushes']} pushes in {group['prs_with_avoidable']} of "
            f"{group['prs']} PRs; {group['pushes']} pushes seen{interval}) |",
            f"| M2 Avoidable job-minutes per merged PR ({label}) | {num(group['m2_job_minutes_per_pr'])} "
            f"({group['avoidable_job_minutes']} min over {group['prs']} PRs; failed jobs alone "
            f"{group['avoidable_floor_job_minutes']} min) |",
            f"| M3 PRs whose first push is red on a local-command job ({label}) | "
            f"{pct(group['m3_pct'])} ({group['m3_first_push_red_prs']}/{group['prs_with_push']}) |",
        ]
    return rows


def cli_bool(text: str) -> bool:
    """`true`/`false` only: a typo must not switch an API-spending step on or off."""
    value = text.strip().lower()
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError(f"expected true or false, got {text!r}")
    return value == "true"


def cli_int(minimum: int) -> Callable[[str], int]:
    def parse(text: str) -> int:
        try:
            value = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"expected an integer, got {text!r}") from None
        if value < minimum:
            raise argparse.ArgumentTypeError(f"must be at least {minimum}, got {value}")
        return value

    return parse


def cli_logins(text: str) -> frozenset[str]:
    """Comma-separated GitHub logins, lowercase. Empty means none configured."""
    logins = [part.strip() for part in text.split(",") if part.strip()]
    for login in logins:
        if not LOGIN_RE.match(login):
            raise argparse.ArgumentTypeError(f"not a GitHub login: {login!r}")
    return frozenset(login.lower() for login in logins)


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
    ap.add_argument(
        "--ci-waste",
        type=cli_bool,
        default=False,
        help="true to run the CI-waste census (M1 to M3). Opt-in: it spends API calls "
        "up to --ci-waste-max-calls. Default false.",
    )
    ap.add_argument(
        "--ci-waste-sample",
        type=cli_int(1),
        default=CENSUS_DEFAULT_SAMPLE,
        help=f"census the newest N merged PRs of the window (default {CENSUS_DEFAULT_SAMPLE})",
    )
    ap.add_argument(
        "--ci-waste-max-calls",
        type=cli_int(1),
        default=CENSUS_DEFAULT_MAX_CALLS,
        help=f"HARD cap on census API calls per run (default {CENSUS_DEFAULT_MAX_CALLS})",
    )
    ap.add_argument(
        "--agent-logins",
        type=cli_logins,
        default=frozenset(),
        help="comma-separated GitHub logins that are agents. Unset means the census "
        "rates cover all authors only; nothing is guessed",
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
    for cls, culprit_pr, age in classify_fixes(capped):
        classes[cls] += 1
        if age is not None:
            ages.append(age)
            age_counts[bucket(age)] += 1
        if culprit_pr:
            culprit_votes[culprit_pr] = culprit_votes.get(culprit_pr, 0) + 1

    top_culprits = sorted(culprit_votes.items(), key=lambda kv: -kv[1])[:10]
    deferrals = deferral_metrics(args.repo)
    # ONE set of per-day searches serves the harvest, the D8 counters and the
    # census: the counters are only "free" if nothing fetches the list twice.
    merged = merged_prs_since(args.repo, args.since_iso)
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
        "deferrals": deferrals,
        "overrides": override_metrics(args.repo, args.since_iso),
        "harvest": harvest_metrics(args.repo, args.since_iso, merged),
        "pr_counters": pr_counters(merged),
        "ci_waste": (
            ci_waste_census(
                args.repo,
                merged,
                sample=args.ci_waste_sample,
                max_calls=args.ci_waste_max_calls,
                reserve=CENSUS_DEFAULT_RESERVE,
                agent_logins=args.agent_logins,
            )
            if args.ci_waste
            else {"enabled": False}
        ),
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    vol, szz, dfr, hrv = result["volume"], result["szz"], result["deferrals"], result["harvest"]
    ovr = result["overrides"]
    by_class = ", ".join(f"{k} {v}" for k, v in ovr["overrides_by_class"].items()) or "none"
    owner = ovr["owner_decisions"]
    owner_cell = (
        "; ".join(f"{d['pr']} by @{d['actor']}" for d in owner[:OWNER_DECISION_LISTED])
        + (
            f"; and {len(owner) - OWNER_DECISION_LISTED} more"
            if len(owner) > OWNER_DECISION_LISTED
            else ""
        )
        if owner
        else "none"
    )

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
        f"| Overrides recorded / by the PR author on their own PR | "
        f"{ovr['overrides_total']} / {ovr['overrides_self_authored']} |",
        f"| Overrides by class | {by_class} |",
        f"| Deferred overrides with no issue | {ovr['overrides_deferred_without_issue']} |",
        f"| Owner decisions | {len(owner)}: {owner_cell} |",
        f"| Harvest-section adoption on fix PRs | "
        f"{hrv['with_harvest_section']}/{hrv['merged_fix_prs']} ({pct(hrv['adoption_pct'])}) |",
        # Escapes are the actionable half: on the default branch the gate is
        # required, so a gap means it did not run and wants investigating.
        f"| Harvest escapes (default-branch fix PRs with no section) | {hrv['escapes']} |",
        f"| Excluded: another base / gate never ran | "
        f"{hrv['excluded_other_base']} / {hrv['excluded_gate_never_ran']} |",
        *d8_table_rows(result["pr_counters"], result["ci_waste"]),
    ]
    with open(args.summary, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"metrics written: {args.out} ({len(capped)} fixes SZZ-traced)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
