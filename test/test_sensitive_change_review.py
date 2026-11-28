"""Unit tests for .github/scripts/sensitive_change_review.py.

The gate turns red when a sensitive path changes and no trusted human approved
the CURRENT head with a filled-in reasoning template. These tests pin each way
an approval can fail to count, and the workflow wiring that keeps the checker
out of the PR's own hands.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "sensitive_change_review.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sensitive-change-review.yml"
COMMENT_WORKFLOW = ROOT / ".github" / "workflows" / "sensitive-change-review-comment.yml"
APPROVAL_WORKFLOW = ROOT / ".github" / "workflows" / "sensitive-change-review-approval.yml"
HEAD = "a" * 40
OLD = "b" * 40

GOOD_BODY = """Looks right.

## Sensitive change review
- What rule changed: the git push floor now also blocks --mirror.
- Before -> after (what was allowed/blocked, now): --mirror was allowed, now it is blocked.
- Worst case if wrong (breaks commands / leaks secret / locks user out): a normal push could be blocked.
- Evidence checked (which sandbox result proves it): macOS Seatbelt run in the PR body, both cases.
- No regression:
  - Tools still work (which tools/commands were run, result): read, write, git status, git push all passed.
  - Backward compatible (old config / old data / old callers still work, how checked): old denied_commands.json loads unchanged.
  - Existing tests (which suites passed on this head): test_security and test_sandbox passed.
- How to undo: revert this one commit.
"""


@pytest.fixture(scope="module")
def mod():
    return load_skill_script("sensitive_change_review", SCRIPT)


SAME = {"full_name": "o/r"}


def _pr(author="alice", head_repo=SAME):
    return {
        "head": {"sha": HEAD, "repo": head_repo},
        "base": {"repo": SAME},
        "user": {"login": author},
    }


def _review(body=GOOD_BODY, login="bob", state="APPROVED", commit=HEAD, typ="User"):
    return {
        "state": state,
        "body": body,
        "commit_id": commit,
        "user": {"login": login, "type": typ},
    }


def _writers(*logins):
    return lambda login: "write" if login in logins else "read"


def _eval(mod, pr, files, reviews, perm=None):
    return mod.evaluate(pr, files, reviews, perm or _writers("bob", "carol"))


SENSITIVE = ["src/kiro_crew/sandbox.py", "README.md"]


def test_no_sensitive_path_passes_without_review(mod):
    assert _eval(mod, _pr(), ["README.md", "src/kiro_crew/cli.py"], []).ok


@pytest.mark.parametrize(
    "path",
    [
        "src/kiro_crew/sandbox.py",
        "src/kiro_crew/sandbox_seatbelt.py",
        "src/kiro_crew/security/redaction.py",
        "src/kiro_crew/secrets/vault.py",
        "src/kiro_crew/log_redaction.py",
        "src/kiro_crew/apps/scrub_sdk.py",
        "src/kiro_crew/hooks.py",
        "src/kiro_crew/hook_runtime/denied_commands.py",
        "src/kiro_crew/deny_guidance.py",
        "src/kiro_crew/platform/security_authority.py",
        "src/kiro_crew/agent_sdk/tool_gate.py",
        "src/kiro_crew/acp_tool_gate.py",
        "src/kiro_crew/security_posture.py",
        "src/kiro_crew/computer_use/gate.py",
        "src/kiro_crew/dashboard/handlers/credential_redaction.py",
        ".github/scripts/sensitive_change_review.py",
        ".github/workflows/sensitive-change-review.yml",
        ".github/workflows/sensitive-change-review-comment.yml",
    ],
)
def test_sensitive_globs_match(mod, path):
    assert mod.sensitive_files([path]) == [path]


def test_sensitive_globs_name_real_files(mod):
    # A glob that matches nothing tracked protects nothing.
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    tracked = out.splitlines()
    for glob in mod.SENSITIVE_GLOBS:
        assert any(mod.fnmatch.fnmatchcase(t, glob) for t in tracked), glob


def test_sensitive_globs_cover_denial_differential_paths(mod):
    # The command floor's own trigger list must stay a subset of this gate.
    wf = (ROOT / ".github/workflows/denial-differential.yml").read_text(encoding="utf-8")
    block = wf.split("paths:", 1)[1].split("\n\n", 1)[0]
    paths = [
        ln.strip().lstrip("-").strip().strip('"')
        for ln in block.splitlines()
        if ln.strip().startswith("-")
    ]
    assert paths, "no paths parsed from denial-differential.yml"
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True, encoding="utf-8"
    ).stdout
    tracked = out.splitlines()
    for pattern in paths:
        if pattern in (".github/workflows/denial-differential.yml", "scripts/deny_diff.py"):
            continue  # the differential's own CI tooling, not a runtime floor path
        glob = pattern.replace("**", "*")
        hits = [f for f in tracked if mod.fnmatch.fnmatchcase(f, glob)]
        assert hits, pattern
        assert mod.sensitive_files(hits) == hits, pattern


def test_complete_approval_on_head_passes(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review()])
    assert v.ok, v.messages
    assert v.sensitive == ["src/kiro_crew/sandbox.py"]


def test_no_review_fails(mod):
    v = _eval(mod, _pr(), SENSITIVE, [])
    assert not v.ok
    assert "No reasoning template" in v.messages[0]


def test_bare_approval_fails(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review(body="")])
    assert not v.ok
    assert "heading" in v.messages[0]


def test_approval_on_old_commit_fails(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review(commit=OLD)])
    assert not v.ok
    assert "older commit" in v.messages[0]


def test_bot_approval_fails(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review(login="kiro[bot]", typ="Bot")])
    assert not v.ok
    assert "bots" in v.messages[0]


def test_self_approval_fails(mod):
    v = _eval(mod, _pr(author="bob"), SENSITIVE, [_review(login="bob")])
    assert not v.ok
    assert "author" in v.messages[0]


def test_read_only_reviewer_fails(mod):
    # A read-only collaborator still reads as COLLABORATOR in author_association,
    # so the gate asks the permission API instead.
    v = _eval(mod, _pr(), SENSITIVE, [_review()], perm=_writers())
    assert not v.ok
    assert "write access" in v.messages[0]


def test_admin_reviewer_passes(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review()], perm=lambda _login: "admin")
    assert v.ok


def test_renamed_sensitive_file_counts_by_old_name(mod):
    rows = [{"filename": "src/kiro_crew/moved.py", "previous_filename": "src/kiro_crew/sandbox.py"}]
    paths = mod.changed_paths(rows)
    assert mod.sensitive_files(paths) == ["src/kiro_crew/sandbox.py"]


def test_file_list_must_be_complete(mod):
    rows = [{"filename": "a.py"}]
    assert mod.file_list_problem({"changed_files": 1}, rows) is None
    assert "reports 2" in mod.file_list_problem({"changed_files": 2}, rows)
    assert "reports None" in mod.file_list_problem({}, rows)
    big = [{"filename": f"f{i}"} for i in range(mod.FILES_API_CAP)]
    assert "3000+" in mod.file_list_problem({"changed_files": mod.FILES_API_CAP}, big)


def test_later_changes_requested_revokes_approval(mod):
    reviews = [_review(), _review(state="CHANGES_REQUESTED", body="wait")]
    assert not _eval(mod, _pr(), SENSITIVE, reviews).ok


def test_later_comment_does_not_revoke_approval(mod):
    reviews = [_review(), _review(state="COMMENTED", body="nit")]
    assert _eval(mod, _pr(), SENSITIVE, reviews).ok


def test_one_good_approval_among_bad_ones_passes(mod):
    reviews = [_review(login="carol", body="LGTM"), _review(login="bob")]
    assert _eval(mod, _pr(), SENSITIVE, reviews).ok


@pytest.mark.parametrize(
    "label", ["Tools still work", "Backward compatible", "Existing tests", "How to undo"]
)
def test_missing_field_fails(mod, label):
    body = "\n".join(line for line in GOOD_BODY.splitlines() if label not in line)
    problems = mod.template_problems(body)
    assert any(label in p and "missing" in p for p in problems), problems


@pytest.mark.parametrize("value", ["", "N/A", "n/a", "none", "LGTM", "fine", "looks ok"])
def test_empty_or_short_value_fails(mod, value):
    body = GOOD_BODY.replace("revert this one commit.", value)
    problems = mod.template_problems(body)
    assert any("How to undo" in p for p in problems), problems


def test_na_with_reason_passes(mod):
    body = GOOD_BODY.replace(
        "old denied_commands.json loads unchanged.",
        "N/A because no config or data format changed.",
    )
    assert mod.template_problems(body) == []


def test_value_on_continuation_line_counts(mod):
    body = GOOD_BODY.replace(
        "- How to undo: revert this one commit.",
        "- How to undo:\n  revert this one commit and redeploy.",
    )
    assert mod.template_problems(body) == []


def test_heading_is_case_insensitive(mod):
    body = GOOD_BODY.replace("## Sensitive change review", "## SENSITIVE CHANGE REVIEW")
    assert mod.template_problems(body) == []


def test_failure_summary_carries_the_template(mod):
    v = _eval(mod, _pr(), SENSITIVE, [])
    text = mod._summary(v)
    assert mod.HEADING in text
    for label in mod.REQUIRED_FIELDS:
        assert label in text


def test_failure_summary_says_how_to_clear(mod):
    # The summary names both real actions: the author's comment, or a
    # reviewer's Approve review. It pre-fills the head SHA to copy.
    text = mod._summary(_eval(mod, _pr(), SENSITIVE, []))
    assert "PR author posts the template" in text
    assert '"Approve"' in text
    assert "new push resets" in text
    assert f"- Head commit: {HEAD}" in text


# -- PR author's template comment ------------------------------------------------


def _comment(body=None, login="alice", typ="User"):
    if body is None:
        body = GOOD_BODY.replace(
            "## Sensitive change review\n", f"## Sensitive change review\n- Head commit: {HEAD}\n"
        )
    return {"body": body, "user": {"login": login, "type": typ}}


def _eval_c(mod, comments, reviews=(), perm=None):
    return mod.evaluate(
        _pr(), SENSITIVE, list(reviews), perm or _writers("alice", "bob"), comments=comments
    )


def test_author_comment_with_head_passes(mod):
    v = _eval_c(mod, [_comment()])
    assert v.ok, v.messages
    assert "@alice posted" in v.messages[0]


def test_author_comment_short_sha_passes(mod):
    body = GOOD_BODY + f"\nCovers {HEAD[:7]}.\n"
    assert _eval_c(mod, [_comment(body=body)]).ok


def test_author_comment_without_head_fails(mod):
    v = _eval_c(mod, [_comment(body=GOOD_BODY)])
    assert not v.ok
    assert "does not name the current head" in " ".join(v.messages)


def test_author_comment_for_old_head_fails(mod):
    body = GOOD_BODY + f"\n- Head commit: {OLD}\n"
    assert not _eval_c(mod, [_comment(body=body)]).ok


def test_author_comment_incomplete_template_fails(mod):
    body = f"## Sensitive change review\n- Head commit: {HEAD}\n- What rule changed: x\n"
    v = _eval_c(mod, [_comment(body=body)])
    assert not v.ok
    assert "missing field" in " ".join(v.messages)


def test_other_users_comment_does_not_count(mod):
    assert not _eval_c(mod, [_comment(login="bob")]).ok


def test_bot_comment_does_not_count(mod):
    pr = _pr(author="github-actions[bot]")
    c = _comment(login="github-actions[bot]", typ="Bot")
    v = mod.evaluate(pr, SENSITIVE, [], lambda _l: "write", comments=[c])
    assert not v.ok


def test_author_without_write_fails(mod):
    v = _eval_c(mod, [_comment()], perm=_writers("bob"))
    assert not v.ok
    assert "no write access" in " ".join(v.messages)


def test_newer_author_comment_wins_over_stale_one(mod):
    stale = _comment(body=GOOD_BODY + f"\n- Head commit: {OLD}\n")
    assert _eval_c(mod, [stale, _comment()]).ok


def test_fork_author_comment_still_refused(mod):
    v = mod.evaluate(
        _pr(head_repo={"full_name": "x/r"}), SENSITIVE, [], _writers("alice"), comments=[_comment()]
    )
    assert not v.ok and v.fork


def test_reviewer_path_still_passes_without_comment(mod):
    assert _eval_c(mod, [], reviews=[_review()]).ok


def test_main_passes_on_author_comment(mod, monkeypatch):
    pr = dict(_pr(), changed_files=1)

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "src/kiro_crew/sandbox.py"}]
        if path.endswith("/reviews?per_page=100"):
            return []
        if path.endswith("/comments?per_page=100"):
            return [_comment()]
        if path.endswith("/permission"):
            return {"permission": "write"}
        return pr

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_write", lambda *a: None)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 0


def test_main_comment_read_error_fails_closed(mod, monkeypatch, capsys):
    pr = dict(_pr(), changed_files=1)

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "src/kiro_crew/sandbox.py"}]
        if path.endswith("/reviews?per_page=100"):
            return []
        if path.endswith("/comments?per_page=100"):
            raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 502")
        return pr

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 2


def test_event_head_must_match_api_head(mod):
    # Finding 1: an API that lags a push must not let an older approval turn
    # the newer commit green.
    assert mod.head_problem(HEAD, HEAD) is None
    assert "differs" in mod.head_problem(HEAD, OLD)
    assert "no head SHA" in mod.head_problem("", HEAD)


def test_judges_the_given_head_not_the_api_head(mod):
    # The API still says OLD, but the run is for HEAD: an approval of OLD fails.
    pr = _pr()
    pr["head"] = dict(pr["head"], sha=OLD)
    v = mod.evaluate(pr, SENSITIVE, [_review(commit=OLD)], _writers("bob"), head=HEAD)
    assert not v.ok
    assert "older commit" in v.messages[0]


def test_later_bare_approval_keeps_complete_one(mod):
    # Finding 2: a second bare "Approve" on the same head must not hide the
    # complete template approval before it.
    reviews = [_review(), _review(body="")]
    v = _eval(mod, _pr(), SENSITIVE, reviews)
    assert v.ok, v.messages


def test_changes_requested_withdraws_all_earlier_approvals(mod):
    reviews = [_review(), _review(body=""), _review(state="CHANGES_REQUESTED", body="wait")]
    assert not _eval(mod, _pr(), SENSITIVE, reviews).ok


def test_approval_after_changes_requested_counts(mod):
    reviews = [_review(state="CHANGES_REQUESTED", body="wait"), _review()]
    assert _eval(mod, _pr(), SENSITIVE, reviews).ok


def test_several_incomplete_approvals_say_so(mod):
    v = _eval(mod, _pr(), SENSITIVE, [_review(body=""), _review(body="LGTM")])
    assert not v.ok
    assert "2 approvals checked" in v.messages[0]


def test_permission_read_error_propagates(mod):
    # Finding 3: a failed permission read must surface, not read as "none".
    def broken(_login):
        raise mod.PermissionReadError("GET .../permission failed: HTTP 403")

    with pytest.raises(mod.PermissionReadError):
        _eval(mod, _pr(), SENSITIVE, [_review()], perm=broken)


def test_bare_approval_skips_permission_read(mod):
    calls = []

    def perm(login):
        calls.append(login)
        return "write"

    _eval(mod, _pr(), SENSITIVE, [_review(body="")], perm=perm)
    assert calls == []


def _main_with(mod, monkeypatch, permission_result):
    """Run main() with a fake `gh api` whose permission call returns or raises."""
    pr = dict(_pr(), changed_files=1)
    rows = [{"filename": "src/kiro_crew/sandbox.py"}]

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return rows
        if path.endswith("/reviews?per_page=100"):
            return [_review()]
        if path.endswith("/comments?per_page=100"):
            return []
        if path.endswith("/permission"):
            if isinstance(permission_result, Exception):
                raise permission_result
            return permission_result
        return pr

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_write", lambda *a: None)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return mod.main()


def test_main_passes_with_write_permission(mod, monkeypatch):
    assert _main_with(mod, monkeypatch, {"permission": "write"}) == 0


def test_main_404_is_no_access(mod, monkeypatch, capsys):
    err = subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)")
    assert _main_with(mod, monkeypatch, err) == 1
    assert "HTTP 404" in capsys.readouterr().out


def test_main_other_permission_error_is_read_error(mod, monkeypatch, capsys):
    err = subprocess.CalledProcessError(1, ["gh"], stderr="gh: Resource not accessible (HTTP 403)")
    assert _main_with(mod, monkeypatch, err) == 2
    assert "HTTP 403" in capsys.readouterr().out


def test_main_fails_when_event_head_lags(mod, monkeypatch):
    pr_old = dict(_pr(), changed_files=1)
    pr_old["head"] = dict(pr_old["head"], sha=OLD)

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "src/kiro_crew/sandbox.py"}]
        if path.endswith("/reviews?per_page=100"):
            return [_review(commit=OLD)]
        if path.endswith("/permission"):
            return {"permission": "write"}
        return pr_old

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 1


@pytest.mark.parametrize(
    "line",
    [
        "- **What rule changed:** the git push floor now also blocks --mirror.",
        "- **What rule changed**: the git push floor now also blocks --mirror.",
        "- _What rule changed:_ the git push floor now also blocks --mirror.",
    ],
)
def test_emphasized_label_counts(mod, line):
    # Finding 4: bold or italic labels are the same field.
    body = GOOD_BODY.replace(
        "- What rule changed: the git push floor now also blocks --mirror.", line
    )
    assert mod.template_problems(body) == []


def test_emphasized_empty_value_still_fails(mod):
    body = GOOD_BODY.replace("- How to undo: revert this one commit.", "- **How to undo:** **N/A**")
    assert any("How to undo" in p for p in mod.template_problems(body))


class TestWorkflow:
    @pytest.fixture(scope="class")
    def wf(self):
        return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_runs_the_default_branch_workflow(self, wf):
        # Any `pull_request` or `pull_request_review` run reads this file from
        # the PR head, so a PR could rewrite the check it is judged by.
        on = wf.get("on") or wf[True]
        assert set(on) == {"pull_request_target"}
        assert {"synchronize", "edited"} <= set(on["pull_request_target"]["types"])

    def test_token_can_only_comment(self, wf):
        # Write is for the sticky comment; nothing else.
        assert wf["permissions"] == {"contents": "read", "pull-requests": "write"}

    def test_runs_the_default_branch_checker_first(self, wf):
        (job,) = wf["jobs"].values()
        base = job["steps"][0]["with"]
        assert base["ref"] == "${{ github.event.repository.default_branch }}"
        run = job["steps"][-1]["run"]
        assert 'script="base/.github/scripts/sensitive_change_review.py"' in run
        # The PR's own copy is never checked out or run.
        assert "head/" not in run
        assert all("head" not in str(step.get("with", {}).get("ref", "")) for step in job["steps"])

    def test_passes_the_event_head_sha(self, wf):
        (job,) = wf["jobs"].values()
        env = job["steps"][-1]["env"]
        assert env["EVENT_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"

    def test_no_waiver_label(self, wf):
        assert "labels" not in WORKFLOW.read_text(encoding="utf-8")


class TestCommentWorkflow:
    @pytest.fixture(scope="class")
    def wf(self):
        return yaml.safe_load(COMMENT_WORKFLOW.read_text(encoding="utf-8"))

    def test_reruns_on_a_comment_or_when_called_for_a_review(self, wf):
        on = wf.get("on") or wf[True]
        assert set(on) == {"issue_comment", "workflow_call"}
        (job,) = wf["jobs"].values()
        assert "github.event_name == 'pull_request_review'" in job["if"]

    def test_token_can_only_rerun(self, wf):
        assert wf["permissions"] == {
            "actions": "write",
            "checks": "read",
            "contents": "read",
            "pull-requests": "read",
        }

    def test_only_the_pr_authors_template_comment_runs(self, wf):
        (job,) = wf["jobs"].values()
        cond = job["if"]
        assert "github.event.issue.pull_request != null" in cond
        assert "github.event.comment.user.login == github.event.issue.user.login" in cond
        assert "## Sensitive change review" in cond

    def test_hosted_runner_and_no_checkout(self, wf):
        # Still a GitHub-hosted Ubuntu VM, not the CodeBuild fleet: both
        # branches of the expression carry no fleet credentials and are
        # dispatched by GitHub, so an arbitrary commenter's event cannot reach
        # AWS. A personal fork cannot reach the larger-runner definition, so it
        # falls back to the shared hosted pool.
        (job,) = wf["jobs"].values()
        assert job["runs-on"] == (
            "${{ github.repository == 'kirodotdev/KiroCrew' && 'kirocrew-linux-4-core' "
            "|| 'ubuntu-latest' }}"
        )
        assert all("uses" not in step for step in job["steps"])

    def test_reruns_the_check_workflow(self, wf):
        (job,) = wf["jobs"].values()
        run = job["steps"][-1]["run"]
        assert "actions/workflows/sensitive-change-review.yml/runs" in run
        # Only that run executed the default branch's copy of the check.
        assert '.event == "pull_request_target"' in run
        assert "/rerun" in run

    def test_other_comments_cannot_cancel_the_authors_rerun(self, wf):
        # A workflow-level group runs before the job `if`, so a skipped run
        # from someone else's comment would cancel the author's re-run.
        assert "concurrency" not in wf
        (job,) = wf["jobs"].values()
        assert job["concurrency"]["cancel-in-progress"] is False


class TestApprovalWorkflow:
    @pytest.fixture(scope="class")
    def wf(self):
        return yaml.safe_load(APPROVAL_WORKFLOW.read_text(encoding="utf-8"))

    def test_a_review_calls_the_rerun(self, wf):
        on = wf.get("on") or wf[True]
        assert set(on) == {"pull_request_review"}
        assert set(on["pull_request_review"]["types"]) == {"submitted", "edited", "dismissed"}
        (job,) = wf["jobs"].values()
        assert job["uses"] == "./.github/workflows/sensitive-change-review-comment.yml"
        assert wf["permissions"] == {
            "actions": "write",
            "checks": "read",
            "contents": "read",
            "pull-requests": "read",
        }


# -- Fork PRs -----------------------------------------------------------------


def test_fork_with_sensitive_path_fails_even_when_approved(mod):
    v = _eval(mod, _pr(head_repo={"full_name": "someone/r"}), SENSITIVE, [_review()])
    assert not v.ok and v.fork
    assert "maintainer takes it over" in v.messages[0]


def test_deleted_fork_counts_as_fork(mod):
    assert _eval(mod, _pr(head_repo=None), SENSITIVE, [_review()]).fork


def test_fork_without_sensitive_path_passes(mod):
    assert _eval(mod, _pr(head_repo={"full_name": "someone/r"}), ["README.md"], []).ok


def test_fork_summary_has_no_template(mod):
    v = _eval(mod, _pr(head_repo={"full_name": "someone/r"}), SENSITIVE, [])
    assert mod.HEADING not in mod._summary(v)


# -- Sticky comment -------------------------------------------------------------


def _sticky_run(mod, monkeypatch, pr, comments):
    writes = []

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "src/kiro_crew/sandbox.py"}]
        if path.endswith("/reviews?per_page=100"):
            return []
        if path.endswith("/comments?per_page=100"):
            return comments
        return dict(pr, changed_files=1)

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_write", lambda method, path, body: writes.append((method, path)))
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 1
    return writes


def test_sticky_comment_is_created(mod, monkeypatch):
    assert _sticky_run(mod, monkeypatch, _pr(), []) == [("POST", "repos/o/r/issues/1/comments")]


def test_sticky_comment_is_updated_in_place(mod, monkeypatch):
    own = {"id": 7, "user": {"login": mod.COMMENT_AUTHOR}, "body": mod.COMMENT_MARKER + " old"}
    writes = _sticky_run(mod, monkeypatch, _pr(), [own])
    assert writes == [("PATCH", "repos/o/r/issues/comments/7")]


def test_marker_from_another_user_is_not_edited(mod, monkeypatch):
    planted = {"id": 9, "user": {"login": "mallory"}, "body": mod.COMMENT_MARKER}
    writes = _sticky_run(mod, monkeypatch, _pr(), [planted])
    assert writes == [("POST", "repos/o/r/issues/1/comments")]


def test_fork_gets_no_comment(mod, monkeypatch):
    assert _sticky_run(mod, monkeypatch, _pr(head_repo={"full_name": "x/r"}), []) == []


def test_comment_failure_does_not_change_verdict(mod, monkeypatch, capsys):
    def boom(method, path, body):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="HTTP 403")

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "src/kiro_crew/sandbox.py"}]
        if path.endswith("/reviews?per_page=100"):
            return [_review()]
        if path.endswith("/comments?per_page=100"):
            return []
        if path.endswith("/permission"):
            return {"permission": "write"}
        return dict(_pr(), changed_files=1)

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_write", boom)
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 0
    assert "Could not update the sticky comment" in capsys.readouterr().out


def _clean_run(mod, monkeypatch, comments):
    writes = []

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [{"filename": "README.md"}]
        if path.endswith("/reviews?per_page=100"):
            return []
        if path.endswith("/comments?per_page=100"):
            return comments
        return dict(_pr(), changed_files=1)

    monkeypatch.setattr(mod, "_gh_json", fake)
    monkeypatch.setattr(mod, "_gh_write", lambda method, path, body: writes.append((method, path)))
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 0
    return writes


def test_stale_sticky_is_refreshed_when_sensitive_file_reverted(mod, monkeypatch):
    own = {"id": 7, "user": {"login": mod.COMMENT_AUTHOR}, "body": mod.COMMENT_MARKER + " old"}
    assert _clean_run(mod, monkeypatch, [own]) == [("PATCH", "repos/o/r/issues/comments/7")]


def test_no_sticky_is_created_without_sensitive_file(mod, monkeypatch):
    assert _clean_run(mod, monkeypatch, []) == []


# -- Transient API reads ---------------------------------------------------------


def _err(stderr):
    return subprocess.CalledProcessError(1, ["gh", "api"], stderr=stderr)


@pytest.fixture
def gh_calls(mod, monkeypatch):
    """Script `gh api` answers in order: an exception is raised, a str is stdout."""
    state = {"answers": [], "calls": 0, "sleeps": []}

    def run(cmd, **kwargs):
        state["calls"] += 1
        answer = state["answers"].pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(cmd, 0, stdout=answer, stderr="")

    monkeypatch.setattr(mod.subprocess, "run", run)
    # raising=False: the retry tests must fail, not error, with the fix reverted.
    monkeypatch.setattr(mod, "_sleep", state["sleeps"].append, raising=False)
    return state


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Server Error (HTTP 502)",
        "gh: Service Unavailable (HTTP 503)",
        "gh: API rate limit exceeded for installation. (HTTP 403)",
        "gh: Too Many Requests (HTTP 429)",
        'Get "https://api.github.com/...": read: connection reset by peer',
    ],
)
def test_transient_read_failure_is_retried(mod, gh_calls, stderr):
    gh_calls["answers"] = [_err(stderr), '{"ok": 1}']
    assert mod._gh_json("repos/o/r/pulls/1") == {"ok": 1}
    assert gh_calls["calls"] == 2
    assert gh_calls["sleeps"] == [mod.READ_RETRY_DELAYS[0]]


@pytest.mark.parametrize(
    "stderr",
    ["gh: Not Found (HTTP 404)", "gh: Resource not accessible by integration (HTTP 403)"],
)
def test_definite_api_answer_is_not_retried(mod, gh_calls, stderr):
    # A 404 is how permission_of learns "not a collaborator"; waiting on it
    # would only slow the check down.
    gh_calls["answers"] = [_err(stderr)]
    with pytest.raises(subprocess.CalledProcessError):
        mod._gh_json("repos/o/r/collaborators/x/permission")
    assert gh_calls["calls"] == 1
    assert gh_calls["sleeps"] == []


def test_bad_json_is_not_retried(mod, gh_calls):
    gh_calls["answers"] = ["not json"]
    with pytest.raises(json.JSONDecodeError):
        mod._gh_json("repos/o/r/pulls/1")
    assert gh_calls["calls"] == 1


def test_read_that_keeps_failing_still_fails_closed(mod, gh_calls, monkeypatch, capsys):
    attempts = len(mod.READ_RETRY_DELAYS) + 1
    gh_calls["answers"] = [_err("gh: Bad Gateway (HTTP 502)\nmore detail")] * attempts
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 2
    assert gh_calls["calls"] == attempts
    assert gh_calls["sleeps"] == list(mod.READ_RETRY_DELAYS)
    out = capsys.readouterr().out
    assert (
        "::error::Could not read the PR from the API "
        "(CalledProcessError: gh: Bad Gateway (HTTP 502)); re-run this job."
    ) in out
    assert "more detail" not in out


def test_one_failed_pr_read_no_longer_fails_the_check(mod, gh_calls, monkeypatch):
    pr = dict(_pr(head_repo={"full_name": "someone/r"}), changed_files=1)
    gh_calls["answers"] = [
        _err("gh: Server Error (HTTP 502)"),
        json.dumps(pr),
        json.dumps([[{"filename": "README.md"}]]),
        json.dumps([[]]),
        json.dumps([[]]),
    ]
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert mod.main() == 0
    assert gh_calls["answers"] == []


def test_a_write_sends_its_body_from_a_file_not_stdin(mod, monkeypatch) -> None:
    # The job's rate-limit wrapper re-runs a rate-limited call; a body fed on
    # stdin would be empty on that retry.
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        seen["stdin"] = kwargs.get("input")
        with open(cmd[-1], encoding="utf-8") as payload:
            seen["body"] = json.load(payload)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    mod._gh_write("POST", "repos/o/r/issues/1/comments", "hello")

    assert seen == {"stdin": None, "body": {"body": "hello"}}


# -- Carrying a record across a push that leaves the sensitive diff alone ------

SANDBOX_PATCH = "@@ -10,6 +10,7 @@ def floor():\n     keep\n+    block('--mirror')\n     keep\n"


def _row(name="src/kiro_crew/sandbox.py", patch=SANDBOX_PATCH, **extra):
    lines = patch.split("\n") if isinstance(patch, str) else []
    row = {
        "filename": name,
        "status": "modified",
        "patch": patch,
        "additions": sum(ln.startswith("+") for ln in lines),
        "deletions": sum(ln.startswith("-") for ln in lines),
    }
    return dict(row, **extra)


def _regular(paths):
    return {path: "100644" for path in paths}


def _diffs(mod, **by_head):
    """patches_of over fixed compare rows per head; an exception is raised.

    A head maps to its rows, or to (rows, modes) to set the modes it reads.
    """

    def patches_of(sha):
        answer = by_head[sha]
        if isinstance(answer, BaseException):
            raise answer
        rows, modes = answer if isinstance(answer, tuple) else (answer, None)
        return mod.sensitive_patches(rows, (lambda _paths: modes) if modes else _regular)

    return patches_of


def _old_comment():
    return _comment(body=_comment()["body"].replace(HEAD, OLD))


def _eval_carry(mod, patches_of, comments=(), reviews=(), pr=None, perm=None):
    return mod.evaluate(
        pr or _pr(),
        SENSITIVE,
        list(reviews),
        perm or _writers("alice", "bob"),
        comments=list(comments),
        patches_of=patches_of,
    )


def test_identical_sensitive_diff_carries_the_authors_comment(mod):
    rows = [
        _row(),
        {"filename": "README.md", "status": "modified", "patch": "@@ -1 +1 @@\n-a\n+b\n"},
    ]
    readme_moved = [_row(), {"filename": "README.md", "status": "modified", "patch": "@@ x"}]
    v = _eval_carry(mod, _diffs(mod, **{OLD: rows, HEAD: readme_moved}), comments=[_old_comment()])
    assert v.ok, v.messages
    assert f"@alice's template comment for {OLD[:12]} carries to {HEAD[:12]}" in v.messages[0]
    assert "byte-identical" in v.messages[0]


def test_identical_sensitive_diff_carries_a_template_approval(mod):
    v = _eval_carry(
        mod, _diffs(mod, **{OLD: [_row()], HEAD: [_row()]}), reviews=[_review(commit=OLD)]
    )
    assert v.ok, v.messages
    assert f"@bob's template approval for {OLD[:12]}" in v.messages[0]


def test_one_changed_byte_does_not_carry(mod):
    changed = SANDBOX_PATCH.replace("--mirror", "--mirrOr")
    v = _eval_carry(
        mod, _diffs(mod, **{OLD: [_row()], HEAD: [_row(patch=changed)]}), comments=[_old_comment()]
    )
    assert not v.ok
    assert "the diff of `src/kiro_crew/sandbox.py` changed" in " ".join(v.messages)


def test_moved_hunk_does_not_carry(mod):
    # A patch-id would match this; the exact patch text does not.
    moved = SANDBOX_PATCH.replace("-10,6 +10,7", "-40,6 +40,7")
    v = _eval_carry(
        mod, _diffs(mod, **{OLD: [_row()], HEAD: [_row(patch=moved)]}), comments=[_old_comment()]
    )
    assert not v.ok


def test_added_sensitive_file_does_not_carry(mod):
    files = ["src/kiro_crew/sandbox.py", "src/kiro_crew/hooks.py"]
    after = [_row(), _row(name="src/kiro_crew/hooks.py")]
    v = mod.evaluate(
        _pr(),
        files,
        [],
        _writers("alice"),
        comments=[_old_comment()],
        patches_of=_diffs(mod, **{OLD: [_row()], HEAD: after}),
    )
    assert not v.ok
    assert "set of sensitive files changed (`src/kiro_crew/hooks.py`)" in " ".join(v.messages)


def test_removed_sensitive_file_does_not_carry(mod):
    before = [_row(), _row(name="src/kiro_crew/hooks.py")]
    v = _eval_carry(mod, _diffs(mod, **{OLD: before, HEAD: [_row()]}), comments=[_old_comment()])
    assert not v.ok
    assert "set of sensitive files changed" in " ".join(v.messages)


def test_fork_is_refused_even_when_the_diff_is_identical(mod):
    v = _eval_carry(
        mod,
        _diffs(mod, **{OLD: [_row()], HEAD: [_row()]}),
        comments=[_old_comment()],
        pr=_pr(head_repo={"full_name": "x/r"}),
    )
    assert not v.ok and v.fork


def test_dismissed_approval_does_not_carry(mod):
    reviews = [_review(commit=OLD), _review(commit=OLD, state="DISMISSED", body="")]
    v = _eval_carry(mod, _diffs(mod, **{OLD: [_row()], HEAD: [_row()]}), reviews=reviews)
    assert not v.ok


def test_incomplete_earlier_record_does_not_carry(mod):
    v = _eval_carry(
        mod, _diffs(mod, **{OLD: [_row()], HEAD: [_row()]}), reviews=[_review(commit=OLD, body="")]
    )
    assert not v.ok


def test_compare_failure_fails_closed(mod):
    broken = mod.CarryUnavailable("GET compare failed: HTTP 404")
    v = _eval_carry(mod, _diffs(mod, **{OLD: broken, HEAD: [_row()]}), comments=[_old_comment()])
    assert not v.ok
    assert "could not compare the two heads (GET compare failed: HTTP 404)" in " ".join(v.messages)


def test_compare_that_disagrees_with_the_file_list_fails_closed(mod):
    v = _eval_carry(mod, _diffs(mod, **{OLD: [], HEAD: []}), comments=[_old_comment()])
    assert not v.ok
    assert "disagrees with the PR's file list" in " ".join(v.messages)


@pytest.mark.parametrize(
    "missing",
    [
        {"patch": None},
        {"patch": ""},
        {"filename": "", "previous_filename": "src/kiro_crew/hooks.py"},
    ],
)
def test_sensitive_file_without_patch_text_cannot_be_compared(mod, missing):
    with pytest.raises(mod.CarryUnavailable):
        mod.sensitive_patches([dict(_row(), **missing)], _regular)


def test_compare_at_the_file_cap_cannot_be_compared(mod):
    rows = [{"filename": f"f{i}", "patch": "x"} for i in range(mod.COMPARE_FILES_CAP)]
    with pytest.raises(mod.CarryUnavailable):
        mod.sensitive_patches(rows, _regular)


def test_non_sensitive_rows_need_no_patch(mod):
    assert mod.sensitive_patches([_row(), {"filename": "logo.png"}], _regular) == {
        "src/kiro_crew/sandbox.py": ("", "modified", "100644", SANDBOX_PATCH)
    }


@pytest.mark.parametrize(
    "line, want",
    [
        (f"- Head commit: {OLD}", OLD),
        (f"- **Head commit:** `{OLD}`", OLD),
        (f"- Head commit: {OLD[:12]}", ""),
        (f"- Head commit: {OLD} and {HEAD}", ""),
        ("- Head commit:", ""),
    ],
)
def test_named_head_needs_one_full_sha(mod, line, want):
    assert (
        mod.named_head(f"## Sensitive change review\n{line}\n- What rule changed: x y z\n") == want
    )


def test_carry_compares_at_most_the_newest_heads(mod):
    heads = [f"{i:x}" * 40 for i in range(1, mod.CARRY_MAX_HEADS + 3)]
    comments = [_comment(body=_comment()["body"].replace(HEAD, h)) for h in heads]
    asked = []

    def patches_of(sha):
        asked.append(sha)
        raise mod.CarryUnavailable("unreadable")

    assert not _eval_carry(mod, patches_of, comments=comments).ok
    assert len(set(asked)) == mod.CARRY_MAX_HEADS
    assert set(asked) == set(reversed(heads[-mod.CARRY_MAX_HEADS :]))


def test_carry_is_off_without_a_compare(mod):
    assert not _eval_carry(mod, None, comments=[_old_comment()]).ok


def _carry_main(mod, monkeypatch, compare, summary_path, base_ref="main", modes_at=None):
    pr = dict(_pr(), changed_files=1, base={"repo": SAME, "ref": base_ref})
    asked = []
    writes = []

    def fake(path, paginate=False):
        if path.endswith("/files?per_page=100"):
            return [_row()]
        if path.endswith("/reviews?per_page=100"):
            return []
        if path.endswith("/comments?per_page=100"):
            return [_old_comment()]
        if path.endswith("/permission"):
            return {"permission": "write"}
        if "/compare/" in path:
            asked.append(path)
            return compare(path)
        return pr

    monkeypatch.setattr(mod, "_gh_json", fake)
    modes_at = modes_at or (lambda _sha, paths: _regular(paths))
    monkeypatch.setattr(mod, "tree_modes", lambda _repo, sha, paths: modes_at(sha, paths))
    monkeypatch.setattr(mod, "_gh_write", lambda method, p, body: writes.append(body))
    monkeypatch.setenv("REPO", "o/r")
    monkeypatch.setenv("PR", "1")
    monkeypatch.setenv("EVENT_HEAD_SHA", HEAD)
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_path))
    return mod.main(), asked, writes


def test_main_carries_and_says_which_head(mod, monkeypatch, tmp_path):
    code, asked, writes = _carry_main(
        mod, monkeypatch, lambda _p: {"files": [_row()]}, tmp_path / "summary.md"
    )
    assert code == 0
    assert asked == [
        f"repos/o/r/compare/main...{OLD}?per_page=1",
        f"repos/o/r/compare/main...{HEAD}?per_page=1",
    ]
    said = f"@alice's template comment for {OLD[:12]} carries to {HEAD[:12]}"
    assert said in (tmp_path / "summary.md").read_text(encoding="utf-8")
    assert said in writes[0]


def test_main_compare_read_error_fails_closed(mod, monkeypatch, tmp_path):
    def compare(_path):
        raise subprocess.CalledProcessError(1, ["gh"], stderr="gh: Not Found (HTTP 404)")

    code, _asked, writes = _carry_main(mod, monkeypatch, compare, tmp_path / "summary.md")
    assert code == 1
    assert "could not compare the two heads" in writes[0]
    assert "HTTP 404" in writes[0]


def test_failure_summary_says_a_plain_rebase_keeps_the_record(mod):
    text = mod._summary(_eval(mod, _pr(), SENSITIVE, []))
    assert "keeps the record for the earlier head" in text


# -- What a compare row does not show: mode, status, cut-short patches ---------


def test_changed_mode_alone_does_not_carry(mod):
    # A compare row shows no mode, so an exec-bit flip on an already edited
    # file leaves the patch text identical.
    sandbox = "src/kiro_crew/sandbox.py"
    v = _eval_carry(
        mod,
        _diffs(mod, **{OLD: [_row()], HEAD: ([_row()], {sandbox: "100755"})}),
        comments=[_old_comment()],
    )
    assert not v.ok
    assert f"the mode of `{sandbox}` changed (100644 -> 100755)" in " ".join(v.messages)


@pytest.mark.parametrize("mode", ["120000", "160000", "40000", ""])
def test_symlink_submodule_or_unread_mode_cannot_be_compared(mod, mode):
    modes = {"src/kiro_crew/sandbox.py": mode} if mode else {}
    with pytest.raises(mod.CarryUnavailable, match="not a regular file"):
        mod.sensitive_patches([_row()], lambda _paths: modes)


def test_removed_file_reads_no_mode(mod):
    asked = []

    def modes_of(paths):
        asked.append(paths)
        return {}

    patches = mod.sensitive_patches([_row(status="removed")], modes_of)
    assert patches["src/kiro_crew/sandbox.py"][2] == ""
    assert asked == []


@pytest.mark.parametrize("status", ["changed", "copied", "unchanged", ""])
def test_status_the_patch_cannot_show_cannot_be_compared(mod, status):
    with pytest.raises(mod.CarryUnavailable, match="not compared"):
        mod.sensitive_patches([_row(status=status)], _regular)


def test_cut_short_patch_cannot_be_compared(mod):
    with pytest.raises(mod.CarryUnavailable, match="disagrees with its line counts"):
        mod.sensitive_patches([_row(additions=40)], _regular)


def test_row_that_is_not_an_object_cannot_be_compared(mod):
    with pytest.raises(mod.CarryUnavailable, match="not an object"):
        mod.sensitive_patches([_row(), "src/kiro_crew/sandbox.py"], _regular)


@pytest.mark.parametrize(
    "after",
    [
        {"status": "renamed", "previous_filename": "src/kiro_crew/sandbox_old.py"},
        {"status": "added"},
    ],
)
def test_rename_source_or_status_change_does_not_carry(mod, after):
    before = _row(status="renamed", previous_filename="src/kiro_crew/sandbox_wrap.py")
    files = ["src/kiro_crew/sandbox.py", "src/kiro_crew/sandbox_old.py"]
    v = mod.evaluate(
        _pr(),
        files if after.get("previous_filename") else files[:1],
        [],
        _writers("alice"),
        comments=[_old_comment()],
        patches_of=_diffs(mod, **{OLD: [before], HEAD: [_row(**after)]}),
    )
    assert not v.ok
    assert "the diff of `src/kiro_crew/sandbox.py` changed" in " ".join(v.messages)


def _tree_answer(entries):
    return json.dumps({"data": {"repository": {"object": {"entries": entries}}}})


def test_tree_modes_reads_each_parent_tree_once(mod, gh_calls):
    gh_calls["answers"] = [
        _tree_answer([{"name": "a.py", "mode": 0o100644}, {"name": "b.py", "mode": 0o100755}]),
        _tree_answer([{"name": "c.py", "mode": 0o120000}]),
    ]
    modes = mod.tree_modes("o/r", HEAD, ["src/a.py", "src/b.py", "src/x/c.py"])
    assert modes == {"src/a.py": "100644", "src/b.py": "100755", "src/x/c.py": "120000"}
    assert gh_calls["calls"] == 2


@pytest.mark.parametrize(
    "answer",
    [
        json.dumps({"data": {"repository": {"object": None}}}),
        json.dumps({"errors": [{"message": "Something went wrong"}]}),
        _tree_answer([{"name": "other.py", "mode": 0o100644}]),
        _tree_answer([{"name": "a.py", "mode": "100644"}]),
        "not json",
        _err("gh: Not Found (HTTP 404)"),
    ],
)
def test_tree_modes_fails_closed(mod, gh_calls, answer):
    gh_calls["answers"] = [answer]
    with pytest.raises(mod.CarryUnavailable):
        mod.tree_modes("o/r", HEAD, ["src/a.py"])


def test_main_changed_mode_fails_closed(mod, monkeypatch, tmp_path):
    def modes_at(sha, paths):
        return {p: "100755" if sha == HEAD else "100644" for p in paths}

    code, _asked, writes = _carry_main(
        mod, monkeypatch, lambda _p: {"files": [_row()]}, tmp_path / "s.md", modes_at=modes_at
    )
    assert code == 1
    assert "the mode of `src/kiro_crew/sandbox.py` changed" in writes[0]


def test_main_without_a_base_ref_fails_closed(mod, monkeypatch, tmp_path):
    code, asked, writes = _carry_main(
        mod, monkeypatch, lambda _p: {"files": [_row()]}, tmp_path / "s.md", base_ref=""
    )
    assert code == 1
    assert asked == []
    assert "the PR names no base branch" in writes[0]


@pytest.mark.parametrize("answer", [[], "text", {"files": None}, {"files": {"a": 1}}])
def test_main_malformed_compare_fails_closed(mod, monkeypatch, tmp_path, answer):
    code, _asked, writes = _carry_main(mod, monkeypatch, lambda _p: answer, tmp_path / "s.md")
    assert code == 1
    assert "lists no files" in writes[0]
