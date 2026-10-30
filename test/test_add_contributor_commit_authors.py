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
import tempfile
from pathlib import Path

import pytest
import yaml
from git_maintenance_helpers import git_maintenance_off_entries

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

    Its pipeline is ``jq -rn --rawfile oids /tmp/pr_commit_oids.txt '<prog>' \\
    /tmp/commits.jsonl | sort -u >...``, so stop the capture at ``' \\n`` (the
    closing quote before the trailing backslash-newline continuation).
    """
    m = re.search(
        r"jq -rn --rawfile oids /tmp/pr_commit_oids\.txt '(?P<prog>[^']*)' \\\n\s*/tmp/commits\.jsonl",
        SCRIPT,
        re.DOTALL,
    )
    assert m, "commit-author jq program (over /tmp/commits.jsonl) not found in the collect step"
    return m.group("prog")


def _merge_oid_program() -> str:
    """The jq program that lists the merged PRs' merge-commit oids."""
    m = re.search(
        r"jq -r '(?P<prog>[^']*)' /tmp/merged\.jsonl \| sort -u >/tmp/merge_oids\.txt", SCRIPT
    )
    assert m, "merge-commit oid jq program not found in the collect step"
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


def _commit_logins(nodes: list[dict], landed: list[str] | None = None) -> list[str]:
    """Run the commit-author jq exactly as the step does.

    ``landed`` is the oid set that landed via a merged PR (the step's
    /tmp/pr_commit_oids.txt); by default every node's oid counts as landed.

    Bot exclusion is NOT on this path: it is left to the single existing
    mechanism (`.github/contributors-optout.txt`, applied by the script for
    every source). ``test_automation_accounts_are_in_the_optout_list`` covers
    that the service accounts are listed there.
    """
    if landed is None:
        landed = [n["oid"] for n in nodes]
    with tempfile.TemporaryDirectory() as d:
        oids = Path(d) / "pr_commit_oids.txt"
        oids.write_text("".join(f"{o}\n" for o in landed), encoding="utf-8")
        stdin = "\n".join(json.dumps(node) for node in nodes) + "\n"
        proc = subprocess.run(
            ["jq", "-rn", "--rawfile", "oids", str(oids), _commit_jq_program()],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
    return sorted({lg for lg in proc.stdout.splitlines() if lg})


def _user(login: str) -> dict:
    return {"user": {"login": login}}


def _pr_node() -> dict:
    return {
        "mergeCommit": {"oid": "c0ffee"},
        "author": {"login": "maintainer", "__typename": "User"},
        "closingIssuesReferences": {
            "nodes": [{"author": {"login": "reporter", "__typename": "User"}}]
        },
    }


def _merged(login_lists: list[list[str]]) -> list[dict]:
    """Commit-history nodes; ``_commit_logins`` treats them as landed by default."""
    return [
        {
            "oid": f"oid{i}",
            "authors": {"nodes": [_user(lg) for lg in lgs]},
        }
        for i, lgs in enumerate(login_lists)
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
    """Only commits whose oid landed via a merged PR are credited, so a raw
    branch commit (direct push / non-merged) never credits a non-consenting
    internal author."""
    nodes = [
        {"oid": "landed", "authors": {"nodes": [_user("pr-contributor")]}},
        {"oid": "direct", "authors": {"nodes": [_user("internal-direct-pusher")]}},
    ]
    assert _commit_logins(nodes, landed=["landed"]) == ["pr-contributor"]
    assert _commit_logins(nodes, landed=[]) == []


def test_commit_sweep_asks_no_per_commit_pr_lookup():
    """The per-commit associatedPullRequests lookup cost ~50 ms a commit and
    pushed the job past its time limit; the merged-PR set now comes from the
    PR sweep's mergeCommit oids plus local git history."""
    flat = " ".join(SCRIPT.split())
    history_query = flat.split("history(first:", 1)[1].split("--jq", 1)[0]
    assert "associatedPullRequests" not in history_query
    pr_query = flat.split("pullRequests(states: MERGED", 1)[1].split("--jq", 1)[0]
    assert "mergeCommit { oid }" in pr_query
    assert _run_jq(_merge_oid_program(), [_pr_node(), {"mergeCommit": None}]) == ["c0ffee"]
    assert 'git rev-list "$oid^1..$oid"' in SCRIPT


def test_merge_commit_merge_credits_the_merged_branch_commits(tmp_path):
    """A PR merged with a merge commit lands its own commits too; the step adds
    every commit the merge brought in, and only those."""
    if shutil.which("git") is None:
        pytest.skip("requires git")
    block = re.search(
        r"(git rev-list --merges HEAD .*?\} \| sort -u >/tmp/pr_commit_oids\.txt)",
        SCRIPT,
        re.DOTALL,
    )
    assert block, "pr_commit_oids block not found in the collect step"
    repo = tmp_path / "repo"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    env.update(git_maintenance_off_entries(env))

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env, capture_output=True, encoding="utf-8", check=True
        ).stdout.strip()

    repo.mkdir()
    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    git("checkout", "-q", "-b", "topic")
    git("commit", "-q", "--allow-empty", "-m", "topic 1")
    topic1 = git("rev-parse", "HEAD")
    git("commit", "-q", "--allow-empty", "-m", "topic 2")
    topic2 = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")
    git("commit", "-q", "--allow-empty", "-m", "direct push")
    direct = git("rev-parse", "HEAD")
    git("merge", "-q", "--no-ff", "-m", "merge topic", "topic")
    merge = git("rev-parse", "HEAD")

    work = tmp_path / "tmp"
    work.mkdir()
    (work / "merge_oids.txt").write_text(merge + "\n", encoding="utf-8")
    script = "set -euo pipefail\n" + block.group(1).replace("/tmp/", f"{work}/")
    subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True)
    landed = set((work / "pr_commit_oids.txt").read_text(encoding="utf-8").split())
    assert landed == {merge, topic1, topic2}
    assert direct not in landed


def test_commits_outside_the_oid_set_get_the_exact_pr_lookup(tmp_path):
    """The earlier commits of a rebase merge are not merge-commit oids; they are
    asked about one by one (in batches), and only a merged-PR answer counts."""
    m = re.search(
        r"(jq -r '\.oid' /tmp/commits\.jsonl .*?-o /tmp/pr_commit_oids\.txt)\n", SCRIPT, re.DOTALL
    )
    assert m, "leftover-commit lookup block not found in the collect step"
    rebased_early, direct, squash = "a" * 40, "b" * 40, "c" * 40
    work = tmp_path / "tmp"
    work.mkdir()
    (work / "commits.jsonl").write_text(
        "".join(json.dumps({"oid": o}) + "\n" for o in (rebased_early, direct, squash, "not-hex")),
        encoding="utf-8",
    )
    (work / "pr_commit_oids.txt").write_text(squash + "\n", encoding="utf-8")
    # Stand-in for `gh api graphql ... --jq <filter>`: answer for the aliases in
    # the query, then apply the step's own --jq filter with the real jq.
    response = {
        "data": {
            "repository": {
                "c1": {
                    "oid": rebased_early,
                    "associatedPullRequests": {"nodes": [{"merged": True}]},
                },
                "c2": {"oid": direct, "associatedPullRequests": {"nodes": []}},
            }
        }
    }
    (work / "response.json").write_text(json.dumps(response), encoding="utf-8")
    queries = work / "queries.txt"
    fake_gh = (
        "gh() { local filter=''; while [ $# -gt 0 ]; do "
        f'case "$1" in --jq) filter="$2"; shift 2;; -f) printf "%s\\n" "$2" >>{queries}; shift 2;; *) shift;; esac; done; '
        f'jq -r "$filter" {work}/response.json; }}\n'
    )
    sweep_fn = re.search(r"(sweep\(\) \{.*?\n\s*\})\n", SCRIPT, re.DOTALL).group(1)
    script = (
        "set -euo pipefail\nREPO=o/r\n"
        + fake_gh
        + sweep_fn
        + "\n"
        + m.group(1).replace("/tmp/", f"{work}/")
    )
    subprocess.run(["bash", "-c", script], check=True, capture_output=True)
    landed = set((work / "pr_commit_oids.txt").read_text(encoding="utf-8").split())
    assert landed == {squash, rebased_early}
    asked = queries.read_text(encoding="utf-8")
    assert rebased_early in asked and direct in asked
    assert squash not in asked and "not-hex" not in asked


def test_sweep_retries_and_truncates_a_partial_attempt(tmp_path):
    """A 5xx page does not fail the whole run: the sweep retries, and a
    failed attempt's partial output never survives into the next."""
    fn = re.search(r"(sweep\(\) \{.*?\n\s*\})\n", SCRIPT, re.DOTALL)
    assert fn, "sweep() not found in the collect step"
    counter = tmp_path / "n"
    out = tmp_path / "out.jsonl"
    flaky = (
        f"n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n >{counter}; "
        'echo "partial-$n"; [ "$n" -ge 2 ]'
    )
    script = fn.group(1) + f"\nsleep() {{ :; }}\nsweep {out} bash -c '{flaky}'\n"
    subprocess.run(["bash", "-c", "set -euo pipefail\n" + script], check=True, capture_output=True)
    assert out.read_text(encoding="utf-8") == "partial-2\n"


def test_commit_authors_credit_linked_users_and_coauthors():
    nodes = _merged([["original-contributor", "preserved-coauthor"]])
    assert _commit_logins(nodes) == ["original-contributor", "preserved-coauthor"]


def test_commit_unlinked_email_is_skipped():
    nodes = [
        {
            "oid": "x",
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
