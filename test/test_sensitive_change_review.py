"""Unit tests for .github/scripts/sensitive_change_review.py.

The gate turns red when a sensitive path changes and no trusted human approved
the CURRENT head with a filled-in reasoning template. These tests pin each way
an approval can fail to count, and the workflow wiring that keeps the checker
out of the PR's own hands.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "sensitive_change_review.py"
WORKFLOW = ROOT / ".github" / "workflows" / "sensitive-change-review.yml"
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
    assert "No approval" in v.messages[0]


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


def test_failure_summary_says_how_to_approve(mod):
    # Operators pasted the template as a PR comment, or approved their own PR.
    # The summary must name the real action: a non-author Approve review.
    text = mod._summary(_eval(mod, _pr(), SENSITIVE, []))
    assert "comment does NOT count" in text
    assert "other than the PR author" in text
    assert '"Approve"' in text
    assert "new push resets" in text


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

    def test_reruns_on_review_events(self, wf):
        on = wf.get("on") or wf[True]
        assert set(on["pull_request_review"]["types"]) == {"submitted", "edited", "dismissed"}
        assert {"synchronize", "edited"} <= set(on["pull_request"]["types"])

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
