"""Behavioural tests for the commit-author credit path in add-contributor.yml.

The workflow credits three sets of people, from TWO independent paginated
GraphQL sweeps: the author of each merged pull request and the reporters of the
issues those PRs closed (one sweep over merged PRs), and the linked authors and
co-authors of the commits that landed via a merged PR (a separate commit-history
sweep). The commit-author path is what closes the superseded-PR attribution gap
-- when a maintainer lands someone's work as a replacement PR (commits
cherry-picked or preserved via a ``Co-authored-by:`` trailer), the replacement
PR's own author is the maintainer, and only the commit-author path names the
original contributor.

Two design constraints are locked in here because both were blocking review
findings:

* the commit sweep must NOT be a connection nested inside the merged-PR query
  (GraphQL cost scales with the product of nested connections and would blow the
  API's cost budget on an all-merged-PR sweep); and
* it must be SCOPED to commits associated with a merged PR (a raw branch-history
  walk would credit internal authors who committed directly and never consented
  to public recognition, which the README promises against).

The logic lives in ``jq`` programs embedded in the collect ``run:`` block, which
no other test touches. These tests extract those programs from the workflow YAML
and run them for real against synthetic GraphQL node data, so the filters
(merged-PR scope, linked user only, bot exclusion, three-way union dedup) are
verified rather than assumed.

Skipped where the POSIX toolchain the scripts need (bash, jq) is unavailable,
which is the case on the Windows leg of the matrix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "add-contributor.yml"

pytestmark = pytest.mark.skipif(
    not WORKFLOW.exists()
    or os.name == "nt"
    or shutil.which("bash") is None
    or shutil.which("jq") is None,
    reason="requires the workflow file plus a POSIX bash and jq",
)


def _collect_step_script() -> str:
    """The ``run:`` script of the 'Collect merged-PR authors' step."""
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    for step in doc["jobs"]["add"]["steps"]:
        if step.get("name", "").startswith("Collect merged-PR authors"):
            return step["run"]
    raise AssertionError("Collect merged-PR authors step not found in add-contributor.yml")


SCRIPT = _collect_step_script()


def _simple_jq_program(source_file: str, dest_file: str) -> str:
    """Extract a single-stage ``jq -r '<prog>' /tmp/<src> | sort -u >/tmp/<dst>``.

    Used for the PR-author and issue-reporter programs; anchors on ``jq -r '``
    and stops at the first closing ``'`` (the jq programs contain no single
    quote) so the same-shaped calls do not bleed into one another.
    """
    m = re.search(
        r"jq -r '(?P<prog>[^']*)' /tmp/"
        + re.escape(source_file)
        + r" \| sort -u >/tmp/"
        + re.escape(dest_file),
        SCRIPT,
        re.DOTALL,
    )
    assert m, f"no jq program /tmp/{source_file} -> /tmp/{dest_file} found in the collect step"
    return m.group("prog")


def _commit_jq_program() -> str:
    """Extract the commit-author jq program (reads /tmp/commits.jsonl).

    Its pipeline is ``jq -r '<prog>' /tmp/commits.jsonl | sort -u | grep ...``,
    so stop the capture at ``' \\n`` (the closing quote before the trailing
    backslash-newline continuation), not at ``| sort -u >``.
    """
    m = re.search(
        r"jq -r '(?P<prog>[^']*)' \\\n\s*/tmp/commits\.jsonl",
        SCRIPT,
        re.DOTALL,
    )
    assert m, "commit-author jq program (over /tmp/commits.jsonl) not found in the collect step"
    return m.group("prog")


def _run_jq(program: str, nodes: list[dict]) -> list[str]:
    stdin = "\n".join(json.dumps(node) for node in nodes) + "\n"
    proc = subprocess.run(
        ["jq", "-r", program],
        input=stdin,
        capture_output=True,
        # jq emits UTF-8; pin it so the decode does not fall back to the locale
        # encoding (the Windows ANSI code page under CI's subprocess-encoding gate).
        encoding="utf-8",
        check=True,
    )
    return sorted({lg for lg in proc.stdout.splitlines() if lg})


def _commit_logins(nodes: list[dict]) -> list[str]:
    """Run the commit-author jq exactly as the step does.

    Bot exclusion is NOT on this path: it is left to the single existing
    mechanism (`.github/contributors-optout.txt`, applied by the script for
    every source). ``test_automation_accounts_are_in_the_optout_list`` covers
    that the service accounts are listed there.
    """
    return _run_jq(_commit_jq_program(), nodes)


def _user(login: str) -> dict:
    return {"user": {"login": login}}


def _pr_node() -> dict:
    return {
        "author": {"login": "maintainer", "__typename": "User"},
        "closingIssuesReferences": {
            "nodes": [{"author": {"login": "reporter", "__typename": "User"}}]
        },
    }


def _merged(login_lists: list[list[str]]) -> list[dict]:
    """Commit-history nodes associated with a MERGED PR."""
    return [
        {
            "associatedPullRequests": {"nodes": [{"merged": True}]},
            "authors": {"nodes": [_user(lg) for lg in lgs]},
        }
        for lgs in login_lists
    ]


def test_commit_authors_are_not_nested_in_the_pr_query():
    """Commit authors must come from a SEPARATE history sweep, not a connection
    nested inside pullRequests (nesting multiplies GraphQL cost per page)."""
    flat = " ".join(SCRIPT.split())
    assert "pullRequests(states: MERGED" in flat
    pr_query = flat.split("pullRequests(states: MERGED", 1)[1].split("--jq", 1)[0]
    assert "commits(" not in pr_query, "commit authors must not be nested in the PR query"
    assert "defaultBranchRef" in flat
    assert "history(first:" in flat.replace("history(first: ", "history(first:")
    assert "authors(first:" in flat.replace("authors(first: ", "authors(first:")


def test_commit_sweep_is_scoped_to_merged_prs():
    """The history walk must filter on associatedPullRequests merged, so a raw
    branch commit (direct push / non-merged) never credits a non-consenting
    internal author."""
    flat = " ".join(SCRIPT.split())
    assert "associatedPullRequests(first:" in flat.replace(
        "associatedPullRequests(first: ", "associatedPullRequests(first:"
    )
    prog = _commit_jq_program()
    assert "associatedPullRequests" in prog and "merged == true" in prog
    # A commit NOT associated with a merged PR is excluded.
    direct_push = [
        {
            "associatedPullRequests": {"nodes": [{"merged": False}]},
            "authors": {"nodes": [_user("internal-direct-pusher")]},
        },
        {  # no associated PR at all
            "associatedPullRequests": {"nodes": []},
            "authors": {"nodes": [_user("another-direct-pusher")]},
        },
    ]
    assert _commit_logins(direct_push) == []


def test_commit_authors_credit_linked_users_and_coauthors():
    nodes = _merged([["original-contributor", "preserved-coauthor"]])
    assert _commit_logins(nodes) == ["original-contributor", "preserved-coauthor"]


def test_commit_unlinked_email_is_skipped():
    nodes = [
        {
            "associatedPullRequests": {"nodes": [{"merged": True}]},
            "authors": {"nodes": [{"user": None}, _user("linked-one")]},
        }
    ]
    assert _commit_logins(nodes) == ["linked-one"]


def test_automation_accounts_are_in_the_optout_list():
    # GitActor.user is typed User (e.g. kiro-agent's account type is User), so a
    # __typename test on this path would be inert. Rather than a second inline
    # list, the service accounts are excluded through the ONE existing mechanism
    # the script applies to every source: .github/contributors-optout.txt.
    optout = (
        Path(__file__).resolve().parents[1] / ".github" / "contributors-optout.txt"
    ).read_text(encoding="utf-8")
    listed = {
        line.split("#", 1)[0].strip().lower()
        for line in optout.splitlines()
        if line.split("#", 1)[0].strip()
    }
    for account in ("kiro-agent", "github-actions", "dependabot", "renovate"):
        assert account in listed, f"{account} must be in contributors-optout.txt"
    # And the workflow must NOT carry a second, divergent bot list of its own.
    assert "/tmp/bot_logins.txt" not in SCRIPT, "no duplicate inline bot list in the workflow"


def test_pr_author_jq_isolated_from_commit_authors():
    logins = _run_jq(_simple_jq_program("merged.jsonl", "pr_logins.txt"), [_pr_node()])
    assert logins == ["maintainer"]


def test_issue_reporter_jq():
    logins = _run_jq(_simple_jq_program("merged.jsonl", "issue_logins.txt"), [_pr_node()])
    assert logins == ["reporter"]


def test_union_credits_all_three_sets_deduped():
    pr = _run_jq(_simple_jq_program("merged.jsonl", "pr_logins.txt"), [_pr_node()])
    commit = _commit_logins(_merged([["original-contributor", "preserved-coauthor"]]))
    issue = _run_jq(_simple_jq_program("merged.jsonl", "issue_logins.txt"), [_pr_node()])
    union = sorted(set(pr) | set(commit) | set(issue))
    assert union == [
        "maintainer",
        "original-contributor",
        "preserved-coauthor",
        "reporter",
    ]
    assert re.search(
        r"sort -u /tmp/pr_logins\.txt /tmp/commit_logins\.txt /tmp/issue_logins\.txt >/tmp/logins\.txt",
        SCRIPT,
    )


def test_commit_only_contributor_is_still_credited():
    """A contributor who is neither the PR author nor an issue reporter -- only a
    merged-PR commit co-author -- is the whole point: they must reach the union."""
    commit = _commit_logins(_merged([["original-contributor", "preserved-coauthor"]]))
    assert "original-contributor" in commit
    assert "preserved-coauthor" in commit
