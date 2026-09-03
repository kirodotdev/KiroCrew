#!/usr/bin/env python3
"""Detect open pull requests that overlap a given PR on the same issue.

This is the shared detection engine behind two advisory, visibility-only
workflows (`.github/workflows/pr-duplicate-detect.yml`):

  * ON OPEN (proposal 2): when a newly opened / synchronized PR shares an issue
    and touches at least one of the same files as another OPEN PR, that new PR
    gets a single heads-up comment naming the earlier PR(s), so duplicated
    effort is caught before a reviewer reads two solutions to the same problem.
  * ON MERGE (proposal 1): when a PR is merged, any OPEN PR against the same
    issue whose changed-file set intersects the merged PR's is pointed at the
    merge and closed as superseded.

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
    # GitHub via `gh`, then run the same analysis. Used by the workflows.
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
        overlaps.append(
            {
                "number": cand.get("number"),
                "issues": sorted(shared_issues),
                "files": sorted(shared_files),
                "file_count": len(shared_files),
                # Exact set match is a stronger signal a caller MAY phrase more
                # firmly; it is not required to flag (intersection is the rule).
                "exact": cand_files == subject_files and bool(cand_files),
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
        ["pr", "view", str(number), "--repo", repo, "--json", "number,body,state,files"]
    )
    files = [f.get("path") for f in (view.get("files") or []) if f.get("path")]
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
    }


def _fetch_bundle(repo: str, number: int, list_limit: int) -> dict[str, Any]:
    """Build the {"subject", "candidates"} bundle from GitHub.

    Candidates are every OTHER open PR, each materialized to a full record. This
    is the read-only I/O the workflow needs; the analysis stays pure.
    """
    subject = _fetch_pr_record(repo, number)
    open_prs = _gh_json(
        ["pr", "list", "--repo", repo, "--state", "open", "--limit", str(list_limit),
         "--json", "number"]
    ) or []
    candidates: list[dict[str, Any]] = []
    for entry in open_prs:
        cand_num = entry.get("number")
        if cand_num is None or cand_num == number:
            continue
        candidates.append(_fetch_pr_record(repo, cand_num))
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

    # Body-keyword fallback finds the issue when closingIssues is empty.
    check("body_fallback_issue_parsed", referenced_issues(same_issue_intersect) == {1})

    # Exact set match is reported as a stronger signal.
    res2 = analyze({"subject": subject, "candidates": [exact_match]})
    exact = next((o for o in res2["overlaps"] if o["number"] == 4), None)
    check("exact_set_match_flagged", exact is not None)
    check("exact_flag_set", exact is not None and exact["exact"] is True)

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
    parser.add_argument("--limit", type=int, default=900,
                        help="max open PRs to consider as candidates in --fetch mode")
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
