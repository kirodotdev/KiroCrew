"""``.github/scripts/pr_size.py`` and the two workflows that run it.

PR Size ships in shadow: ``PR_SIZE_ENFORCED`` is False and the check never fails. The
red path is real and is tested here with the switch forced on, so flipping the
constant later is a one-line change whose behaviour is already pinned.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "pr_size.py"
WORKFLOWS = ROOT / ".github" / "workflows"

ps = load_skill_script("pr_size_under_test", SCRIPT)

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
BOT = {"login": "github-actions[bot]"}
HEAD = "a" * 40


def _marker(actor: str = "owner1", files: int = 90) -> str:
    return ps.render_marker(actor, files, HEAD, "123", "2026-10-20T10:00:00Z")


def _approval_comment(files: int = 90, login: str = "github-actions[bot]") -> dict[str, Any]:
    return {
        "user": {"login": login},
        "body": _marker(files=files) + "\n## PR size approved\n",
        "html_url": "https://github.com/o/r/pull/1#issuecomment-1",
    }


class FakeGh:
    """Stands in for ``gh api``: answers by path, counts calls, can fail the first N."""

    def __init__(
        self, pull: dict[str, Any] | None, comments: list[Any] | None = None, fail: int = 0
    ):
        self.pull = pull
        self.comments = comments or []
        self.fail = fail
        self.calls: list[list[str]] = []
        self.slept: list[float] = []

    def __call__(self, args: list[str]) -> tuple[int, str, str]:
        self.calls.append(args)
        if self.fail > 0:
            self.fail -= 1
            return 1, "", "HTTP 502 bad gateway"
        path = args[1]
        if "/comments" in path:
            # `gh api --paginate` prints one array per page, back to back.
            half = len(self.comments) // 2
            return 0, json.dumps(self.comments[:half]) + json.dumps(self.comments[half:]), ""
        if self.pull is None:
            return 1, "", "HTTP 404"
        return 0, json.dumps(self.pull), ""


def _pull(files: int, created: str = "2026-10-21T08:00:00Z") -> dict[str, Any]:
    return {"changed_files": files, "created_at": created}


def _check(gh: FakeGh, enforced: bool, summary: str | None = None) -> int:
    return ps.run_check(
        "o/r",
        "1",
        now=NOW,
        enforced=enforced,
        runner=gh,
        sleep=gh.slept.append,
        summary_path=summary,
    )


# ---------------------------------------------------------------- constants


def test_ships_in_shadow() -> None:
    """Flipping this is the rollout's second step; the test changes in that PR."""
    assert ps.PR_SIZE_ENFORCED is False


def test_thresholds_are_the_ones_the_rfc_accepted() -> None:
    assert (ps.FILES_HINT, ps.FILES_STOP, ps.HOURS_AGING) == (50, 80, 168)
    assert (ps.GROWTH_PERCENT, ps.GROWTH_FILES) == (25, 10)
    assert (ps.REASON_MIN, ps.REASON_MAX) == (12, 500)


# ------------------------------------------------------------- the comment


def test_parse_approve_reads_the_reason_from_the_first_line() -> None:
    reason, refusal = ps.parse_approve("/pr-size approve: mechanical rename across 90 files\nmore")
    assert (reason, refusal) == ("mechanical rename across 90 files", "")


@pytest.mark.parametrize(
    "body",
    [
        "/pr-size approve",
        "/pr-size approve:",
        "/pr-size approve: too short",
        "/pr-size approved: a long enough reason here",
        "please /pr-size approve: a long enough reason here",
        "\n/pr-size approve: reason on the second line only",
        "/pr-size approve: " + "x" * 501,
    ],
)
def test_parse_approve_refuses_what_is_not_the_command(body: str) -> None:
    reason, refusal = ps.parse_approve(body)
    assert reason is None
    assert refusal


def test_parse_approve_accepts_the_length_bounds_and_crlf() -> None:
    assert ps.parse_approve("/pr-size approve: " + "x" * 12)[0] == "x" * 12
    assert ps.parse_approve("/pr-size approve: " + "x" * 500)[0] == "x" * 500
    assert (
        ps.parse_approve("/pr-size approve: a reason of length\r\nnext")[0] == "a reason of length"
    )


def test_parse_approve_strips_control_characters() -> None:
    reason, _ = ps.parse_approve("/pr-size approve: line\x1b[31m with escapes ok")
    assert reason is not None
    assert "\x1b" not in reason


# --------------------------------------------------------------- authorize


@pytest.mark.parametrize("permission", ["write", "maintain", "admin"])
def test_a_writer_who_is_not_the_author_may_approve(permission: str) -> None:
    assert ps.authorize(permission, "owner1", "author1", 90) == (True, "")


@pytest.mark.parametrize("permission", ["", "read", "triage", "none"])
def test_a_non_writer_may_not(permission: str) -> None:
    ok, why = ps.authorize(permission, "owner1", "author1", 90)
    assert not ok
    assert "writer" in why


def test_the_author_may_approve_only_as_admin() -> None:
    ok, why = ps.authorize("write", "Author1", "author1", 90)
    assert not ok
    assert "author" in why
    assert ps.authorize("maintain", "author1", "author1", 90)[0] is False
    assert ps.authorize("admin", "author1", "author1", 90) == (True, "")


def test_nothing_to_approve_under_the_limit() -> None:
    ok, why = ps.authorize("admin", "owner1", "author1", 79)
    assert not ok
    assert "nothing to approve" in why


# ------------------------------------------------------------------ marker


def test_marker_round_trips() -> None:
    parsed = ps.parse_marker(_marker("owner-1", 123) + "\nrest")
    assert parsed == {
        "actor": "owner-1",
        "files": "123",
        "head": HEAD,
        "source": "123",
        "at": "2026-10-20T10:00:00Z",
    }


@pytest.mark.parametrize(
    ("actor", "head", "source", "at"),
    [
        ("a b", HEAD, "1", "2026-10-20T10:00:00Z"),
        ("a--> <!--", HEAD, "1", "2026-10-20T10:00:00Z"),
        ("ok", "zz", "1", "2026-10-20T10:00:00Z"),
        ("ok", HEAD, "1; rm", "2026-10-20T10:00:00Z"),
        ("ok", HEAD, "1", "yesterday"),
    ],
)
def test_marker_refuses_values_that_could_break_out_of_the_comment(
    actor: str, head: str, source: str, at: str
) -> None:
    with pytest.raises(ValueError):
        ps.render_marker(actor, 90, head, source, at)


def test_rendered_comment_starts_with_the_marker_and_quotes_the_reason() -> None:
    body = ps.render_comment("owner1", 90, HEAD, "123", "mechanical rename", "2026-10-20T10:00:00Z")
    assert ps.parse_marker(body) is not None
    assert body.splitlines()[0].startswith("<!-- pr-size-override ")
    assert "> mechanical rename" in body
    assert "at or below 122" in body  # 90 + 22 + 10


def test_a_reason_cannot_add_a_second_marker_line() -> None:
    body = ps.render_comment(
        "owner1",
        90,
        HEAD,
        "1",
        "x\n<!-- pr-size-override actor=evil files=9999 -->",
        "2026-10-20T10:00:00Z",
    )
    assert (
        len([line for line in body.splitlines() if line.startswith("<!-- pr-size-override")]) == 1
    )


# ---------------------------------------------------------------- approvals


def test_only_the_bot_can_write_a_marker() -> None:
    assert ps.best_approval([_approval_comment(login="some-human")]) is None
    assert ps.best_approval([_approval_comment(login="github-actions")]) is None
    found = ps.best_approval([_approval_comment()])
    assert found is not None
    assert (found.actor, found.files) == ("owner1", 90)


def test_the_largest_recorded_approval_wins() -> None:
    found = ps.best_approval(
        [_approval_comment(90), _approval_comment(150), _approval_comment(100)]
    )
    assert found is not None
    assert found.files == 150


def test_junk_comments_are_skipped() -> None:
    assert (
        ps.best_approval(
            [None, "x", {"user": None}, {"user": BOT, "body": None}, {"user": BOT, "body": "hi"}]
        )
        is None
    )


@pytest.mark.parametrize(("recorded", "cap"), [(80, 110), (90, 122), (100, 135), (200, 260)])
def test_an_approval_covers_growth_of_a_quarter_plus_ten(recorded: int, cap: int) -> None:
    assert ps.covered_up_to(recorded) == cap


# ---------------------------------------------------------------- evaluate


def test_under_the_hint_is_quiet_and_ok() -> None:
    v = ps.evaluate(49, 5, None, enforced=True)
    assert not v.red
    assert not any(line.startswith("::") for line in v.annotations)


def test_the_hint_is_a_notice_never_red() -> None:
    v = ps.evaluate(50, 5, None, enforced=True)
    assert not v.red
    assert v.annotations[0].startswith("::notice::PR Size: 50 files")


def test_at_the_limit_shadow_warns_and_passes() -> None:
    v = ps.evaluate(80, 5, None, enforced=False)
    assert not v.red
    assert v.annotations[0].startswith("::warning::PR Size (shadow, not enforced): 80 files")
    assert "This would be red" in v.annotations[0]
    assert "/pr-size approve" in v.annotations[0]


def test_at_the_limit_enforced_is_red_without_an_approval() -> None:
    v = ps.evaluate(80, 5, None, enforced=True)
    assert v.red
    assert v.annotations[0].startswith("::error::PR Size: 80 files")


def test_just_under_the_limit_is_not_red_even_when_enforced() -> None:
    assert not ps.evaluate(79, 5, None, enforced=True).red


def test_an_approval_turns_the_enforced_check_green_and_stays_visible() -> None:
    approval = ps.Approval("owner1", 90, "https://example.invalid/c")
    v = ps.evaluate(90, 5, approval, enforced=True)
    assert not v.red
    assert v.annotations[0].startswith(
        "::warning::PR Size: 90 files is over the limit of 80; approved by @owner1"
    )


def test_growth_past_the_cover_needs_a_new_decision() -> None:
    approval = ps.Approval("owner1", 90, "")
    assert not ps.evaluate(122, 5, approval, enforced=True).red
    v = ps.evaluate(123, 5, approval, enforced=True)
    assert v.red
    assert "covers up to 122" in v.annotations[0]


def test_age_is_a_notice_and_never_red() -> None:
    v = ps.evaluate(10, 400, None, enforced=True)
    assert not v.red
    assert "::notice::PR Size: open 400 h (7+ days): merge, split or close" in v.annotations[0]
    assert (
        not ps.evaluate(10, 167, None, enforced=True)
        .annotations[0]
        .startswith("::notice::PR Size: open")
    )


# --------------------------------------------------------------- run_check


def test_shadow_check_passes_a_wide_pr_and_says_it_would_be_red(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _check(FakeGh(_pull(120)), enforced=False) == 0
    assert "shadow, not enforced" in capsys.readouterr().out


def test_enforced_check_is_red_for_a_wide_pr_without_approval(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _check(FakeGh(_pull(120)), enforced=True) == 1
    assert "::error::PR Size: 120 files" in capsys.readouterr().out


def test_enforced_check_is_green_with_a_bot_approval_on_either_page() -> None:
    noise = [{"user": {"login": "x"}, "body": "hi"}] * 3
    for comments in ([_approval_comment(100)] + noise, noise + [_approval_comment(100)]):
        assert _check(FakeGh(_pull(120), comments), enforced=True) == 0


def test_enforced_check_ignores_a_marker_typed_by_a_human() -> None:
    forged = [_approval_comment(500, login="author1")]
    assert _check(FakeGh(_pull(120), forged), enforced=True) == 1


def test_comments_are_read_only_at_the_limit() -> None:
    gh = FakeGh(_pull(60))
    assert _check(gh, enforced=True) == 0
    assert [call[1] for call in gh.calls] == ["repos/o/r/pulls/1"]


def test_one_pull_read_when_under_the_limit_and_one_comment_listing_over_it() -> None:
    gh = FakeGh(_pull(90), [_approval_comment(90)])
    _check(gh, enforced=True)
    assert [call[1] for call in gh.calls] == [
        "repos/o/r/pulls/1",
        "repos/o/r/issues/1/comments?per_page=100",
    ]
    assert gh.calls[1][2:] == ["--paginate"]


def test_a_transient_read_failure_is_retried() -> None:
    gh = FakeGh(_pull(10), fail=2)
    assert _check(gh, enforced=True) == 0
    assert gh.slept == [1, 2]


def test_an_unreadable_pull_passes_in_shadow_and_fails_closed_when_enforced(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _check(FakeGh(_pull(10), fail=3), enforced=False) == 0
    assert "::warning::PR Size could not measure" in capsys.readouterr().out
    assert _check(FakeGh(_pull(10), fail=3), enforced=True) == 1
    assert "::error::PR Size could not measure" in capsys.readouterr().out


def test_a_pull_with_no_file_count_is_a_read_failure_not_a_pass() -> None:
    assert _check(FakeGh({"created_at": "2026-10-21T08:00:00Z"}), enforced=True) == 1


def test_a_bad_repo_or_number_is_refused_not_interpolated(
    capsys: pytest.CaptureFixture[str],
) -> None:
    gh = FakeGh(_pull(10))
    code = ps.run_check("o/r; rm -rf /", "1", now=NOW, enforced=True, runner=gh)
    assert code == 1
    assert gh.calls == []
    assert ps.run_check("o/r", "1/../x", now=NOW, enforced=False, runner=gh) == 0


def test_the_step_summary_is_appended(tmp_path: Path) -> None:
    summary = tmp_path / "summary.md"
    summary.write_text("earlier\n", encoding="utf-8")
    _check(FakeGh(_pull(90)), enforced=False, summary=str(summary))
    text = summary.read_text(encoding="utf-8")
    assert text.startswith("earlier\n### PR Size")
    assert "| changed files | 90 |" in text
    assert "enforced | no (shadow)" in text


def test_paginated_output_decodes_to_one_list() -> None:
    assert ps.decode_stream('[{"a":1}]\n[{"a":2},{"a":3}]\n[]') == [{"a": 1}, {"a": 2}, {"a": 3}]


def test_hours_open_counts_whole_hours() -> None:
    created = ps.parse_time("2026-10-14T12:00:00Z")
    assert ps.hours_open(created, NOW) == 168
    assert ps.hours_open(created, created) == 0
    assert ps.hours_open(NOW, created) == 0


# ------------------------------------------------------------- command line


def _cli(*args: str, **env: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-B", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PATH": os.environ.get("PATH", ""), **env},
    )


def test_cli_parse_approve_prints_the_reason_or_the_refusal() -> None:
    ok = _cli("parse-approve", COMMENT_BODY="/pr-size approve: a mechanical rename")
    assert (ok.returncode, ok.stdout.strip()) == (0, "a mechanical rename")
    bad = _cli("parse-approve", COMMENT_BODY="/pr-size approve: no")
    assert bad.returncode == 3
    assert "Use `/pr-size approve:" in bad.stdout


def test_cli_authorize_exit_codes() -> None:
    ok = _cli(
        "authorize", "--permission", "write", "--actor", "o", "--author", "a", "--files", "90"
    )
    assert (ok.returncode, ok.stdout) == (0, "")
    no = _cli("authorize", "--permission", "read", "--actor", "o", "--author", "a", "--files", "90")
    assert no.returncode == 3
    assert "writer" in no.stdout


def test_cli_render_comment_reads_the_reason_from_the_environment() -> None:
    out = _cli(
        "render-comment",
        "--actor",
        "o",
        "--files",
        "90",
        "--head",
        HEAD,
        "--source",
        "5",
        REASON="why not",
    )
    assert out.returncode == 0
    assert ps.parse_marker(out.stdout) is not None
    assert "> why not" in out.stdout
    bad = _cli("render-comment", "--actor", "o x", "--files", "90", "--head", HEAD, "--source", "5")
    assert bad.returncode == 2


def test_cli_check_in_shadow_never_fails_without_a_token() -> None:
    """No gh credentials here: the read fails, and shadow passes with a warning."""
    out = _cli("check", REPO="o/r", PR="1", PATH="/nonexistent")
    assert out.returncode == 0
    assert "::warning::PR Size could not measure" in out.stdout


# ---------------------------------------------------------------- workflows


def _workflow(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def _steps(name: str) -> list[dict[str, Any]]:
    (job,) = _workflow(name)["jobs"].values()
    return job["steps"]


def _triggers(workflow: dict[str, Any]) -> Any:
    return workflow.get(True, workflow.get("on"))


def test_the_check_is_named_pr_size_and_reads_only() -> None:
    wf = _workflow("pr-size.yml")
    (job,) = wf["jobs"].values()
    assert job["name"] == "PR Size"
    assert job["runs-on"] == "ubuntu-latest"
    assert wf["permissions"] == {"contents": "read", "pull-requests": "read"}
    assert set(_triggers(wf)) == {"pull_request"}
    assert "labeled" not in _triggers(wf)["pull_request"]["types"]


def test_the_check_runs_the_script_from_the_default_branch_not_the_pr() -> None:
    checkout = _steps("pr-size.yml")[0]
    assert checkout["with"]["ref"] == "${{ github.event.repository.default_branch }}"
    assert checkout["with"]["persist-credentials"] is False
    run = _steps("pr-size.yml")[1]["run"]
    assert "python3 base/.github/scripts/pr_size.py check" in run


def test_no_untrusted_expression_sits_inside_a_run_block() -> None:
    for name in ("pr-size.yml", "pr-size-override.yml"):
        for step in _steps(name):
            assert "${{" not in step.get("run", ""), f"{name}: {step.get('name')}"


def test_the_comment_body_reaches_the_script_only_through_the_environment() -> None:
    record = next(s for s in _steps("pr-size-override.yml") if s.get("id") == "record")
    assert record["env"]["COMMENT_BODY"] == "${{ github.event.comment.body }}"
    assert 'python3 "$script" parse-approve' in record["run"]
    assert "COMMENT_BODY" not in record["run"]


def test_the_handler_is_comment_triggered_and_gated_on_the_command() -> None:
    wf = _workflow("pr-size-override.yml")
    assert set(_triggers(wf)) == {"issue_comment"}
    (job,) = wf["jobs"].values()
    assert "github.event.issue.pull_request != null" in job["if"]
    assert "startsWith(github.event.comment.body, '/pr-size approve')" in job["if"]
    assert job["runs-on"] == "ubuntu-latest"
    assert wf["permissions"] == {"actions": "write", "contents": "read", "pull-requests": "write"}


def test_the_handler_reads_the_permission_and_fails_closed_like_the_ai_review_override() -> None:
    run = next(s for s in _steps("pr-size-override.yml") if s.get("id") == "record")["run"]
    assert 'gh api "repos/$REPO/collaborators/$ACTOR/permission"' in run
    assert "HTTP 404|Not Found" in run
    assert "for attempt in 1 2 3" in run
    assert "authorize" in run
    reference = (WORKFLOWS / "ai-review-human-override.yml").read_text(encoding="utf-8")
    assert 'gh api "repos/$REPO/collaborators/$ACTOR/permission"' in reference


def test_the_handler_reruns_the_pr_size_workflow_and_never_fails_after_recording() -> None:
    rerun = next(s for s in _steps("pr-size-override.yml") if s.get("name") == "Re-run PR Size")
    assert rerun["if"] == "steps.record.outputs.head != ''"
    assert "workflows/pr-size.yml/runs" in rerun["run"]
    assert "exit 1" not in rerun["run"]


def test_the_bootstrap_guard_is_a_shadow_only_allowance() -> None:
    run = _steps("pr-size.yml")[1]["run"]
    guard = "if [ ! -f base/.github/scripts/pr_size.py ]; then"
    if ps.PR_SIZE_ENFORCED:
        assert guard not in run, "enforcing means a missing script is a red check, not a pass"
    else:
        assert guard in run


def test_pr_scope_stays_advisory() -> None:
    """D6 adds a separate check; the one that judges 'are these changes related' is untouched."""
    assert "exit 1" not in (WORKFLOWS / "pr-scope.yml").read_text(encoding="utf-8")
