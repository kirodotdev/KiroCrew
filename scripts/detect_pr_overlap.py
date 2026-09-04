#!/usr/bin/env python3
"""Detect open pull requests that overlap a given PR on the same issue.

This is the shared detection engine behind two advisory, visibility-only
workflows (`.github/workflows/pr-duplicate-detect.yml`):

  * ON OPEN (proposal 2): when a newly opened / synchronized PR shares an issue
    and touches at least one of the same files as another OPEN PR, that new PR
    gets a single heads-up comment naming the earlier PR(s), so duplicated
    effort is caught before a reviewer reads two solutions to the same problem.
  * ON MERGE (proposal 1): when a PR is merged, any OPEN PR against the same
    issue whose overlap with the merged PR is STRONG (an exact changed-file
    match, or the shared files cover at least STRONG_OVERLAP_FRACTION of the
    merged PR's files) is pointed at the merge and closed as superseded. A
    weaker single-file overlap is left to the advisory comment rather than
    auto-closed, so distinct companion work is not closed.

Both triggers ask the same question about one PR: *which OTHER open PRs target
the same issue AND touch at least one of the same files?* That question is this
script; the workflows only decide what to do with the answer (comment vs.
comment-and-close). Keeping the logic here -- not inline in YAML -- makes the
set-intersection and issue-linkage rules unit-testable via importlib against
canned JSON, with no network (mirrors test/test_update_contributors.py).

--------------------------------------------------------------------------------
Matching rule (falsifiable, deliberately narrow)
--------------------------------------------------------------------------------
A candidate PR is reported as overlapping the subject PR when BOTH hold:

  1. Same issue: the candidate references at least one issue that the subject
     also references (set intersection of referenced issue numbers is
     non-empty).
  2. Intersecting files: the candidate's changed-file set and the subject's
     changed-file set have a non-empty intersection (they touch >= 1 common
     path).

INTERSECTION, not exact-set-equality, is the trigger. Proposal 2 phrases this
as "changed-file set matches" and proposal 1 as "intersects"; a strict exact
match would miss the common real case where two PRs edit the same file plus
different neighbours, which is still duplicated effort on that file. An exact
set match is reported as a stronger `exact` signal on each overlap so a caller
MAY phrase its message more firmly, but it is NOT required to flag.

Each overlap also carries a `coverage` (the fraction of the subject PR's files
the overlap shares) and a `strong` flag (exact match, or coverage >=
STRONG_OVERLAP_FRACTION). The on-open advisory flags on ANY overlap; the
on-merge auto-close acts ONLY on a `strong` overlap, so a merge does not close
an open PR that merely shares one file for the same issue.

Both conditions are required. Same issue alone is too noisy -- a single issue
legitimately hosts stacked or companion PRs that touch different files.
Intersecting files alone is too noisy -- unrelated PRs routinely edit a shared
file. Requiring both keeps the signal to "these PRs are solving the same issue
by editing the same files", which is the duplicated effort the proposals target.

The subject PR is NEVER matched against itself (its own number is excluded from
the candidate set).

--------------------------------------------------------------------------------
Referenced issue(s)
--------------------------------------------------------------------------------
An issue is "referenced" by a PR when either holds:

  * GitHub's own linkage (`closingIssuesReferences`, the same connection
    add-contributor.yml reads) lists it. This is authoritative but is populated
    from a "Fixes/Closes/Resolves #N" declaration, and for an UNMERGED PR the
    connection may not be populated yet.
  * The PR body contains a closing keyword + issue reference
    ("Fixes #N", "Closes #N", "Resolves #N", case-insensitive, GitHub's own
    keyword set). This body parse is the fallback that covers an open PR whose
    connection has not populated.

The union of both is the PR's referenced-issue set.

--------------------------------------------------------------------------------
I/O
--------------------------------------------------------------------------------
The engine is pure: it takes fully-materialized PR records and returns overlaps.
All `gh`/GitHub I/O lives in the ``--fetch`` CLI path so the core stays
network-free and unit-testable.

Usage:
    # Pure mode: analyze a pre-fetched bundle read as JSON from stdin, print the
    # overlap result as JSON to stdout. This is the network-free path the tests
    # (and, if pre-fetched, a workflow) use.
    #   {"subject": <pr-record>, "candidates": [<pr-record>, ...]}
    # where a <pr-record> is:
    #   {"number": int, "body": str, "state": "OPEN"|"CLOSED"|"MERGED",
    #    "files": [str, ...], "closingIssues": [int, ...]}
    python3 scripts/detect_pr_overlap.py < bundle.json

    # Fetch mode: materialize the subject PR and the open-PR candidate set from
    # GitHub via `gh`, then run the same analysis. Used by the workflows. The
    # open-PR candidate set is materialized in a SINGLE `gh pr list` call (number
    # + body + state + files + closing-issue linkage), not one call per PR, so
    # the API cost stays bounded on a busy repo.
    python3 scripts/detect_pr_overlap.py --fetch --repo owner/name --pr 123

    # Self-test (no repository or network needed):
    python3 scripts/detect_pr_overlap.py --test

Exit codes:
    0  analysis completed (overlaps may be empty -- that is a normal answer)
    1  malformed input
    2  environment error (a `gh` call failed in --fetch mode)
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from typing import Any

# GitHub's closing keywords, matched case-insensitively against the PR body as
# the fallback for an unmerged PR whose closingIssuesReferences connection is
# not yet populated. Same-repo references only ("#N"); a cross-repo
# "owner/repo#N" is intentionally ignored because this repo's overlap question
# is scoped to its own issues.
_CLOSING_KEYWORDS = ("close", "closes", "closed", "fix", "fixes", "fixed", "resolve", "resolves",
                     "resolved")
_CLOSES_RE = re.compile(
    r"\b(?:" + "|".join(_CLOSING_KEYWORDS) + r")\b\s*:?\s+#(\d+)\b",
    re.IGNORECASE,
)

# The on-merge lane auto-closes an overlapping open PR only on a STRONG signal,
# not on a single shared file. An overlap is strong when it is an exact
# changed-file match OR the shared files cover at least this fraction of the
# subject PR's files (on the on-merge path the subject is the merged PR, so this
# is the fraction of the merged PR's change the open PR duplicates). Weaker
# overlaps still surface in the advisory on-open comment but do NOT trigger an
# auto-close, so genuinely-distinct companion work sharing one file is not
# closed. Every overlap still carries its `coverage`, so a caller may apply its
# own bar; this constant is only the on-merge close threshold.
STRONG_OVERLAP_FRACTION = 0.5


def referenced_issues(pr: dict[str, Any]) -> set[int]:
    """Return the set of issue numbers a PR references.

    Union of GitHub's ``closingIssues`` linkage and a closing-keyword parse of
    the PR body (the fallback for an unmerged PR whose linkage is unpopulated).
    """
    issues: set[int] = set()
    for num in pr.get("closingIssues") or []:
        try:
            issues.add(int(num))
        except (TypeError, ValueError):
            continue
    body = pr.get("body") or ""
    for match in _CLOSES_RE.finditer(body):
        issues.add(int(match.group(1)))
    return issues


def changed_files(pr: dict[str, Any]) -> set[str]:
    """Return the PR's changed-file path set."""
    return {str(path) for path in (pr.get("files") or []) if path}


def find_overlaps(subject: dict[str, Any], candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the OPEN candidate PRs that overlap ``subject`` on issue + files.

    A candidate overlaps when it is OPEN, is not the subject itself, shares at
    least one referenced issue, and shares at least one changed file. The result
    is sorted by PR number ascending so the earliest-opened overlaps are named
    first (their number is the lowest), which is the natural "you may be
    duplicating this earlier PR" ordering.
    """
    subject_number = subject.get("number")
    subject_issues = referenced_issues(subject)
    subject_files = changed_files(subject)
    overlaps: list[dict[str, Any]] = []
    if not subject_issues or not subject_files:
        # No issue link or no changed files -> nothing to correlate on.
        return overlaps
    for cand in candidates:
        if cand.get("number") == subject_number:
            continue  # never match the subject against itself
        if (cand.get("state") or "").upper() != "OPEN":
            continue  # only OTHER OPEN PRs are candidates
        shared_issues = subject_issues & referenced_issues(cand)
        if not shared_issues:
            continue
        shared_files = subject_files & changed_files(cand)
        if not shared_files:
            continue
        cand_files = changed_files(cand)
        exact = cand_files == subject_files and bool(cand_files)
        # Fraction of the SUBJECT's files that this candidate also touches. On
        # the on-merge path the subject is the merged PR, so this is "how much of
        # the merged PR's change this open PR covers" -- the signal the on-merge
        # close gate uses (see STRONG_OVERLAP_FRACTION).
        coverage = len(shared_files) / len(subject_files) if subject_files else 0.0
        overlaps.append(
            {
                "number": cand.get("number"),
                "issues": sorted(shared_issues),
                "files": sorted(shared_files),
                "file_count": len(shared_files),
                # Candidate PR's labels, passed through so a caller (the on-merge
                # lane) can honour a label-gated opt-out without a per-PR call.
                "labels": [str(lbl) for lbl in (cand.get("labels") or []) if lbl],
                # Exact set match is a stronger signal a caller MAY phrase more
                # firmly; it is not required to flag (intersection is the rule).
                "exact": exact,
                # Fraction of the subject's files covered by this overlap.
                "coverage": round(coverage, 4),
                # A "strong" overlap is one where the merge plausibly supersedes
                # the open PR's work: an exact changed-file match, or the shared
                # files cover at least STRONG_OVERLAP_FRACTION of the subject's
                # files. The on-merge lane closes ONLY on a strong overlap and
                # leaves weaker (single-file) overlaps to the advisory comment,
                # so it does not auto-close legitimately-distinct companion work.
                "strong": exact or coverage >= STRONG_OVERLAP_FRACTION,
            }
        )
    overlaps.sort(key=lambda o: (o["number"] is None, o["number"]))
    return overlaps


def analyze(bundle: dict[str, Any]) -> dict[str, Any]:
    """Run the overlap analysis over a {"subject", "candidates"} bundle."""
    subject = bundle.get("subject") or {}
    candidates = bundle.get("candidates") or []
    overlaps = find_overlaps(subject, candidates)
    return {
        "subject": subject.get("number"),
        "issues": sorted(referenced_issues(subject)),
        "files": sorted(changed_files(subject)),
        "overlaps": overlaps,
        "has_overlap": bool(overlaps),
        # True when at least one overlap is strong enough for the on-merge lane
        # to close it (see STRONG_OVERLAP_FRACTION). The on-open advisory uses
        # has_overlap; the on-merge close uses this.
        "has_strong_overlap": any(o["strong"] for o in overlaps),
    }


# --------------------------------------------------------------------------------
# Fetch mode: the only code that touches the network, via `gh`. Kept out of the
# pure core so the analysis stays unit-testable with canned JSON.
# --------------------------------------------------------------------------------


def _gh_json(args: list[str]) -> Any:
    """Run a read-only `gh` command and parse its JSON stdout."""
    proc = subprocess.run(  # noqa: S603 - fixed argv built from validated inputs
        ["gh", *args],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {proc.stderr.strip()}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None


def _fetch_pr_record(repo: str, number: int) -> dict[str, Any]:
    """Materialize one PR into an engine record via `gh`."""
    view = _gh_json(
        ["pr", "view", str(number), "--repo", repo, "--json", "number,body,state,files,labels"]
    )
    files = [f.get("path") for f in (view.get("files") or []) if f.get("path")]
    labels = [lbl.get("name") for lbl in (view.get("labels") or []) if lbl.get("name")]
    owner, name = repo.split("/", 1)
    graph = _gh_json(
        [
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"number={number}",
            "-f",
            "query="
            "query($owner:String!,$name:String!,$number:Int!){"
            "repository(owner:$owner,name:$name){"
            "pullRequest(number:$number){"
            "closingIssuesReferences(first:50){nodes{number}}}}}",
        ]
    )
    closing = []
    try:
        nodes = graph["data"]["repository"]["pullRequest"]["closingIssuesReferences"]["nodes"]
        closing = [n["number"] for n in nodes if n.get("number") is not None]
    except (KeyError, TypeError):
        closing = []
    return {
        "number": view.get("number"),
        "body": view.get("body") or "",
        "state": view.get("state") or "OPEN",
        "files": files,
        "closingIssues": closing,
        "labels": labels,
    }


def _record_from_list_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Normalize one `gh pr list --json ...` entry into an engine record.

    `gh pr list` returns `files` as objects with a `path`,
    `closingIssuesReferences` as objects with a `number`, and `labels` as
    objects with a `name`, the same shapes `_fetch_pr_record` normalizes from
    the per-PR calls.
    """
    files = [f.get("path") for f in (entry.get("files") or []) if f.get("path")]
    closing = [
        n.get("number")
        for n in (entry.get("closingIssuesReferences") or [])
        if n.get("number") is not None
    ]
    labels = [lbl.get("name") for lbl in (entry.get("labels") or []) if lbl.get("name")]
    return {
        "number": entry.get("number"),
        "body": entry.get("body") or "",
        "state": entry.get("state") or "OPEN",
        "files": files,
        "closingIssues": closing,
        "labels": labels,
    }


def _fetch_bundle(repo: str, number: int, list_limit: int) -> dict[str, Any]:
    """Build the {"subject", "candidates"} bundle from GitHub.

    Candidates are the OTHER open PRs. To keep the API cost bounded on a busy
    repo, this issues ONE `gh pr list` that materializes every open PR's number,
    body, state, changed files and closing-issue linkage in a single call,
    rather than a `pr view` + a GraphQL call per open PR (which scaled as
    ~2x the open-PR count on every open/reopen/synchronize event). The subject
    PR is taken from that same list when it is open; on the on-merge path it is
    no longer open, so it alone is materialized with the per-PR reader.
    """
    open_prs = _gh_json(
        ["pr", "list", "--repo", repo, "--state", "open", "--limit", str(list_limit),
         "--json", "number,body,state,files,closingIssuesReferences,labels"]
    ) or []

    subject: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] = []
    for entry in open_prs:
        record = _record_from_list_entry(entry)
        if record["number"] == number:
            subject = record  # the subject is open -> reuse the list record
            continue
        candidates.append(record)

    if subject is None:
        # On-merge (or a subject not returned by the open list): materialize the
        # single subject PR directly. This is one PR, not the whole open set.
        subject = _fetch_pr_record(repo, number)

    return {"subject": subject, "candidates": candidates}


# --------------------------------------------------------------------------------
# Self-test: exercises the pure core with canned data, no repo or network.
# --------------------------------------------------------------------------------


def _run_self_test() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"{'ok  ' if cond else 'FAIL'} {name}")
        if not cond:
            failures.append(name)

    subject = {"number": 10, "body": "Fixes #1", "state": "OPEN", "files": ["a.py", "b.py"],
               "closingIssues": [1]}
    same_issue_intersect = {"number": 5, "body": "Closes #1", "state": "OPEN",
                            "files": ["a.py", "c.py"], "closingIssues": []}
    disjoint_files = {"number": 6, "body": "", "state": "OPEN", "files": ["z.py"],
                      "closingIssues": [1]}
    different_issue = {"number": 7, "body": "Fixes #2", "state": "OPEN", "files": ["a.py"],
                       "closingIssues": [2]}
    closed_pr = {"number": 8, "body": "Fixes #1", "state": "CLOSED", "files": ["a.py"],
                 "closingIssues": [1]}
    exact_match = {"number": 4, "body": "", "state": "OPEN", "files": ["b.py", "a.py"],
                   "closingIssues": [1]}

    res = analyze({"subject": subject, "candidates": [same_issue_intersect, disjoint_files,
                                                      different_issue, closed_pr]})
    nums = [o["number"] for o in res["overlaps"]]
    check("same_issue_intersect_flagged", 5 in nums)
    check("disjoint_files_not_flagged", 6 not in nums)
    check("different_issue_not_flagged", 7 not in nums)
    check("closed_pr_not_flagged", 8 not in nums)
    check("has_overlap_true", res["has_overlap"] is True)

    only = next((o for o in res["overlaps"] if o["number"] == 5), None)
    check("overlap_names_shared_file", only is not None and only["files"] == ["a.py"])
    check("overlap_reports_file_count", only is not None and only["file_count"] == 1)
    check("intersect_not_exact", only is not None and only["exact"] is False)
    # #5 shares a.py out of the subject's {a.py, b.py} -> coverage 0.5 -> strong.
    check("overlap_reports_coverage", only is not None and only["coverage"] == 0.5)
    check("half_coverage_is_strong", only is not None and only["strong"] is True)

    # Body-keyword fallback finds the issue when closingIssues is empty.
    check("body_fallback_issue_parsed", referenced_issues(same_issue_intersect) == {1})

    # Exact set match is reported as a stronger signal.
    res2 = analyze({"subject": subject, "candidates": [exact_match]})
    exact = next((o for o in res2["overlaps"] if o["number"] == 4), None)
    check("exact_set_match_flagged", exact is not None)
    check("exact_flag_set", exact is not None and exact["exact"] is True)
    check("exact_is_strong", exact is not None and exact["strong"] is True)

    # A weak overlap: the merged subject touches many files, the candidate
    # shares only one -> below STRONG_OVERLAP_FRACTION -> flagged (advisory) but
    # NOT strong, so the on-merge lane would not auto-close it.
    wide_subject = {"number": 30, "body": "Fixes #9", "state": "MERGED",
                    "files": ["a.py", "b.py", "c.py", "d.py"], "closingIssues": [9]}
    one_file = {"number": 31, "body": "Fixes #9", "state": "OPEN", "files": ["a.py", "e.py"],
                "closingIssues": [9]}
    res5 = analyze({"subject": wide_subject, "candidates": [one_file]})
    weak = next((o for o in res5["overlaps"] if o["number"] == 31), None)
    check("weak_overlap_flagged", weak is not None)
    check("weak_overlap_not_strong", weak is not None and weak["strong"] is False)
    check("weak_overlap_no_strong_overlap", res5["has_strong_overlap"] is False)
    check("weak_overlap_has_overlap", res5["has_overlap"] is True)

    # Self never matches self even if present in the candidate list.
    res3 = analyze({"subject": subject, "candidates": [subject]})
    check("self_never_matched", res3["overlaps"] == [])

    # A subject with no issue reference correlates on nothing.
    no_issue = {"number": 20, "body": "no link", "state": "OPEN", "files": ["a.py"],
                "closingIssues": []}
    res4 = analyze({"subject": no_issue, "candidates": [same_issue_intersect]})
    check("no_issue_reference_no_overlap", res4["overlaps"] == [])

    if failures:
        print(f"\n{len(failures)} check(s) FAILED: {failures}")
        return 1
    print("\nall checks passed")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--test", action="store_true", help="run the network-free self-test")
    parser.add_argument("--fetch", action="store_true",
                        help="materialize the bundle from GitHub via gh")
    parser.add_argument("--repo", help="owner/name (required with --fetch)")
    parser.add_argument("--pr", type=int, help="subject PR number (required with --fetch)")
    parser.add_argument("--limit", type=int, default=200,
                        help="max open PRs to materialize as candidates in --fetch mode "
                             "(one `gh pr list` call, not one call per PR)")
    args = parser.parse_args(argv[1:])

    if args.test:
        return _run_self_test()

    if args.fetch:
        if not args.repo or args.pr is None:
            sys.stderr.write("--fetch requires --repo and --pr\n")
            return 1
        try:
            bundle = _fetch_bundle(args.repo, args.pr, args.limit)
        except (RuntimeError, ValueError) as exc:
            sys.stderr.write(f"{exc}\n")
            return 2
    else:
        try:
            bundle = json.load(sys.stdin)
        except json.JSONDecodeError as exc:
            sys.stderr.write(f"malformed JSON on stdin: {exc}\n")
            return 1
        if not isinstance(bundle, dict):
            sys.stderr.write("stdin must be a JSON object with 'subject' and 'candidates'\n")
            return 1

    json.dump(analyze(bundle), sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
