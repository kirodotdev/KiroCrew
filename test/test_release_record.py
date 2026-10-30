"""``.github/scripts/release_record.py``: the override class grammar and the deferral issue test."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from ai_review_workflows_helpers import _bash, _child_env
from skill_script_helpers import load_skill_script

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / ".github" / "scripts" / "release_record.py"
module = load_skill_script("release_record_under_test", SCRIPT)

SHA = "a" * 40
BEFORE_CUTOVER = date(2026, 11, 1)
LONG = "the finding does not apply to this change"
RUN_URL = "https://github.com/example/repo/actions/runs/123456"
COMMENT_URL = "https://github.com/example/repo/pull/7#issuecomment-987654"
ISSUE_COMMENT_URL = "https://github.com/example/repo/issues/7#issuecomment-987654"


def cmd(cls: str, reason: str = LONG, issue: str = "", lane: str = "gpt", sha: str = SHA) -> str:
    return f"/ai-review override {lane} {sha} {cls}{' #' + issue if issue else ''}: {reason}"


class TestEveryClassHasItsRequirement:
    @pytest.mark.parametrize(
        ("cls", "issue", "reason"),
        [
            ("false-positive", "", LONG),
            ("not-applicable", "", LONG),
            ("pre-existing", "12", LONG),
            ("pre-existing", "", "it already fails on main since 3a7bbaa85 as well"),
            ("evidence-supplied", "", "the screenshot is at https://example.com/shot.png ok"),
            ("infra-failure", "", f"the lane crashed, see {RUN_URL} for the failure"),
            ("reissue", "", f"same ruling as {COMMENT_URL} for the new head"),
            ("reissue", "", "same ruling as the record with source=555 for the new head"),
            ("deferred", "1234", LONG),
            ("owner-decision", "", f"the owner accepted this here {COMMENT_URL} today"),
            ("owner-decision", "", f"the owner accepted this here {ISSUE_COMMENT_URL} today"),
        ],
    )
    def test_a_met_requirement_is_accepted(self, cls: str, issue: str, reason: str) -> None:
        parsed = module.parse_command(cmd(cls, reason, issue), BEFORE_CUTOVER)

        assert parsed.ok, parsed.notice
        assert (parsed.cls, parsed.issue, parsed.reason) == (cls, issue, reason)
        assert parsed.legacy is False and parsed.warning == ""

    @pytest.mark.parametrize(
        ("cls", "issue", "reason", "needs"),
        [
            # `pre-existing` with neither an issue nor a sha. "defaced" is hex letters only.
            ("pre-existing", "", "this was defaced long before this change landed", "commit sha"),
            ("evidence-supplied", "", LONG, "URL"),
            ("infra-failure", "", "the lane crashed with a timeout, nothing more", "run's URL"),
            # A link that is not a workflow run is not the run.
            ("infra-failure", "", "see https://example.com/log for what failed", "run's URL"),
            ("reissue", "", LONG, "comment link"),
            ("deferred", "", LONG, "follow-up issue"),
            ("owner-decision", "", LONG, "decision comment"),
            # A bare `source=<id>` is enough for a `reissue` and is not a decision comment.
            ("owner-decision", "", "the owner agreed, see the record source=555 for it", "link"),
            # A bare comment id, a link to something that is not a comment, and a review thread.
            ("owner-decision", "", "the owner agreed in issuecomment-987654 on the PR", "link"),
            (
                "owner-decision",
                "",
                "the owner agreed, see https://example.com/pull/7 for it",
                "link",
            ),
            (
                "owner-decision",
                "",
                f"the owner agreed {COMMENT_URL.split('#')[0]}#discussion_r1",
                "link",
            ),
        ],
    )
    def test_a_missing_requirement_is_refused_naming_it(
        self, cls: str, issue: str, reason: str, needs: str
    ) -> None:
        parsed = module.parse_command(cmd(cls, reason, issue), BEFORE_CUTOVER)

        assert not parsed.ok
        assert needs in parsed.notice, parsed.notice
        assert f"`{cls}`" in parsed.notice

    @pytest.mark.parametrize("cls", ["false-positive", "not-applicable", "reissue"])
    def test_an_issue_number_is_refused_where_it_means_nothing(self, cls: str) -> None:
        parsed = module.parse_command(cmd(cls, f"{LONG} {COMMENT_URL}", "5"), BEFORE_CUTOVER)

        assert not parsed.ok
        assert "does not take an issue number" in parsed.notice

    def test_the_class_table_is_the_documented_one(self) -> None:
        assert module.CLASSES == (
            "false-positive",
            "not-applicable",
            "pre-existing",
            "evidence-supplied",
            "infra-failure",
            "reissue",
            "deferred",
            "owner-decision",
        )
        for cls in module.CLASSES:
            assert f"`{cls}`" in module.USAGE


class TestReasonBounds:
    def test_the_reason_runs_from_20_to_500_characters(self) -> None:
        assert not module.parse_command(cmd("false-positive", "x" * 19), BEFORE_CUTOVER).ok
        assert module.parse_command(cmd("false-positive", "x" * 20), BEFORE_CUTOVER).ok
        assert module.parse_command(cmd("false-positive", "x" * 500), BEFORE_CUTOVER).ok
        over = module.parse_command(cmd("false-positive", "x" * 501), BEFORE_CUTOVER)
        assert not over.ok and "20 to 500" in over.notice

    def test_the_length_is_counted_after_trimming(self) -> None:
        body = f"/ai-review override gpt {SHA} false-positive:    {'x' * 19}   "

        assert not module.parse_command(body, BEFORE_CUTOVER).ok


class TestShape:
    @pytest.mark.parametrize(
        "body",
        [
            "",
            "/ai-review override",
            f"/ai-review override gpt {SHA}",
            f"/ai-review override nope {SHA} false-positive: {LONG}",
            f"/ai-review override gpt abc12 false-positive: {LONG}",
            f"/ai-review override gpt {SHA} False-Positive: {LONG}",
            f"/ai-review override gpt {SHA} false-positive #x: {LONG}",
            f"/ai-review override gpt {SHA} deferred #12 #13: {LONG}",
            f" /ai-review override gpt {SHA} false-positive: {LONG}",
        ],
    )
    def test_a_malformed_command_is_refused_with_the_usage(self, body: str) -> None:
        parsed = module.parse_command(body, BEFORE_CUTOVER)

        assert not parsed.ok
        assert "AI-review override not recorded" in parsed.notice
        assert "docs/ci/ci-and-reviews.md#risk-release-records" in parsed.notice

    def test_an_unknown_class_is_named_in_the_refusal(self) -> None:
        parsed = module.parse_command(cmd("accepted-risk"), BEFORE_CUTOVER)

        assert not parsed.ok and "`accepted-risk` is not a release class" in parsed.notice

    def test_only_the_first_line_is_the_command(self) -> None:
        body = cmd("false-positive") + "\nsecond line is prose, not a reason"
        parsed = module.parse_command(body, BEFORE_CUTOVER)

        assert parsed.ok and parsed.reason == LONG

    def test_a_carriage_return_does_not_reach_the_reason(self) -> None:
        parsed = module.parse_command(cmd("false-positive") + "\r\nmore", BEFORE_CUTOVER)

        assert parsed.ok and parsed.reason == LONG

    def test_the_sha_is_lowercased_and_every_lane_is_accepted(self) -> None:
        for lane in module.LANES:
            parsed = module.parse_command(cmd("false-positive", lane=lane, sha="ABCDEF1"), None)
            assert parsed.ok and parsed.lane == lane and parsed.sha == "abcdef1"

    def test_shell_text_in_the_reason_is_data(self) -> None:
        reason = "$(touch /tmp/pwned) `id` ${HOME} \"quoted\" and 'single' text"
        parsed = module.parse_command(cmd("false-positive", reason), BEFORE_CUTOVER)

        assert parsed.ok and parsed.reason == reason


class TestOldGrammarIsAcceptedUntilTheCutover:
    OLD = f"/ai-review override gpt {SHA}: short reason"

    def test_it_is_accepted_with_a_warning_before_the_date(self) -> None:
        parsed = module.parse_command(self.OLD, BEFORE_CUTOVER)

        assert parsed.ok and parsed.legacy and parsed.cls == "legacy"
        assert parsed.reason == "short reason"
        assert module.LEGACY_CUTOVER.isoformat() in parsed.warning
        assert "Name a class" in parsed.warning

    def test_it_is_refused_from_the_date(self) -> None:
        on_the_day = module.parse_command(self.OLD, module.LEGACY_CUTOVER)
        day_before = module.parse_command(
            self.OLD, date.fromordinal(module.LEGACY_CUTOVER.toordinal() - 1)
        )

        assert not on_the_day.ok and "retired" in on_the_day.notice
        assert day_before.ok

    def test_the_old_command_keeps_its_old_reason_rule(self) -> None:
        assert not module.parse_command(f"/ai-review override gpt {SHA}: " + "x" * 501, None).ok

    def test_the_cutover_is_six_weeks_or_more_after_this_code_was_written(self) -> None:
        assert module.LEGACY_CUTOVER >= date(2026, 12, 2)


class TestNoWorkflowAdvertisesTheOldCommand:
    """The old command stops working on ``LEGACY_CUTOVER``. A message that still shows it sends
    the reader to a command the handler refuses, so every emitter shows the classed form."""

    # `/ai-review override <lane> <sha>:` with no class between the sha and the colon.
    CLASSLESS = re.compile(r"/ai-review override (?:<[^>\n]+>|[^\s:<]+) (?:<[^>\n]+>|[^\s:<]+):")
    SCANNED = (
        ".github/workflows/*.yml",
        ".github/review-prompts/*.md",
        ".github/PULL_REQUEST_TEMPLATE.md",
    )

    def _files(self) -> list[Path]:
        return sorted(f for pattern in self.SCANNED for f in ROOT.glob(pattern))

    def test_the_scan_sees_the_emitters(self) -> None:
        names = {f.name for f in self._files()}

        assert {"codex-review.yml", "security-scope-review.yml"} <= names
        assert {"fork-security-scope-review.yml", "claude-review.yml"} <= names

    def test_no_file_shows_the_command_without_a_class(self) -> None:
        found = [
            f"{f.relative_to(ROOT).as_posix()}:{number}: {match.group(0)}"
            for f in self._files()
            for number, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            for match in self.CLASSLESS.finditer(line)
        ]

        assert not found, "\n".join(found)

    def test_no_ready_to_paste_command_files_a_real_finding_as_not_applicable(self) -> None:
        # `not-applicable` means "the rule does not cover this change". A command a workflow
        # drafts for a reader to paste would record every accepted risk as that.
        found = [
            f"{f.relative_to(ROOT).as_posix()}:{number}"
            for f in self._files()
            for number, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1)
            if "/ai-review override" in line and "not-applicable" in line
        ]

        assert not found, "\n".join(found)

    @pytest.mark.parametrize(
        "shown",
        [
            "/ai-review override gpt $HEAD: <reason>",
            "/ai-review override scope <sha>: <reason>",
            f"/ai-review override gpt {SHA}: because",
            "/ai-review override first-principles <head sha>: why",
        ],
    )
    def test_the_scan_catches_the_old_shape(self, shown: str) -> None:
        assert self.CLASSLESS.search(shown)

    @pytest.mark.parametrize(
        "shown",
        [
            "/ai-review override gpt $HEAD <class>: <reason>",
            "/ai-review override scope <sha> false-positive: <reason>",
            "/ai-review override gpt",
        ],
    )
    def test_the_scan_passes_the_new_shape(self, shown: str) -> None:
        assert not self.CLASSLESS.search(shown)


class TestIssueProblems:
    GOOD = {
        "state": "open",
        "labels": [{"name": "deferred-finding"}],
        "assignees": [{"login": "owner"}],
        "body": "Due: 2031-01-01",
        "milestone": None,
    }

    def test_a_tracked_deferral_has_no_problem(self) -> None:
        assert module.issue_problems(self.GOOD) == []

    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            ({"state": "closed"}, ["closed"]),
            ({"labels": []}, ["no deferred-finding label"]),
            ({"labels": [{"name": "bug"}]}, ["no deferred-finding label"]),
            ({"assignees": []}, ["no assignee (owner)"]),
            ({"body": "no date here"}, ["no due date"]),
            ({"body": None}, ["no due date"]),
            ({"body": "see Due: 2031-01-01 in prose"}, ["no due date"]),
            ({"body": "Due: next week"}, ["no due date"]),
        ],
    )
    def test_each_test_fails_on_its_own(self, change: dict, expected: list[str]) -> None:
        problems = module.issue_problems({**self.GOOD, **change})

        assert len(problems) == len(expected)
        for problem, want in zip(problems, expected, strict=True):
            assert problem.startswith(want), problems

    def test_a_milestone_due_date_stands_in_for_the_body_line(self) -> None:
        issue = {**self.GOOD, "body": "", "milestone": {"due_on": "2031-01-01T00:00:00Z"}}

        assert module.issue_problems(issue) == []

    def test_every_problem_is_reported_at_once(self) -> None:
        assert len(module.issue_problems({"state": "closed"})) == 4

    def test_a_pull_request_is_not_an_issue(self) -> None:
        assert module.issue_problems({**self.GOOD, "pull_request": {"url": "x"}}) == [
            "is a pull request, not an issue"
        ]


class TestRenderRecord:
    def test_the_line_is_the_documented_shape(self) -> None:
        assert (
            module.render_record("deferred", "1234", False)
            == "<!-- ai-review-override-record class=deferred issue=1234 author_is_actor=false -->"
        )
        assert module.render_record("false-positive", "", True).endswith(
            "issue=none author_is_actor=true -->"
        )

    @pytest.mark.parametrize(
        ("cls", "issue"),
        [
            ("bogus", ""),
            ("deferred", "1 --> <!--"),
            ("x y", ""),
        ],
    )
    def test_a_value_outside_its_closed_set_is_refused(self, cls: str, issue: str) -> None:
        with pytest.raises(ValueError):
            module.render_record(cls, issue, False)

    def test_the_legacy_class_renders(self) -> None:
        assert "class=legacy" in module.render_record("legacy", "", False)


class TestCommandLine:
    def _run(self, *args: str, stdin: str = "", **env: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            env={**os.environ, **env},
        )

    def test_parse_reads_the_body_from_the_environment(self) -> None:
        proc = self._run("parse", COMMENT_BODY=cmd("deferred", issue="77"))

        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout)
        assert (out["ok"], out["class"], out["issue"], out["lane"]) == (
            True,
            "deferred",
            "77",
            "gpt",
        )

    def test_parse_honours_the_injected_date(self) -> None:
        old = f"/ai-review override gpt {SHA}: old style"
        proc = self._run("parse", COMMENT_BODY=old, RELEASE_RECORD_TODAY="2030-01-01")

        assert json.loads(proc.stdout)["ok"] is False

    def test_issue_problems_prints_one_problem_per_line(self) -> None:
        proc = self._run("issue-problems", stdin=json.dumps({"state": "closed"}))

        assert proc.returncode == 0
        assert proc.stdout.splitlines()[0] == "closed"
        assert len(proc.stdout.splitlines()) == 4

    @pytest.mark.parametrize("stdin", ["not json", "[]", ""])
    def test_issue_problems_refuses_input_that_is_not_an_issue(self, stdin: str) -> None:
        assert self._run("issue-problems", stdin=stdin).returncode == 2

    def test_render_reads_its_values_from_the_environment(self) -> None:
        proc = self._run("render", CLASS="deferred", ISSUE="5", AUTHOR_IS_ACTOR="true")

        assert proc.stdout.strip() == (
            "<!-- ai-review-override-record class=deferred issue=5 author_is_actor=true -->"
        )

    def test_render_refuses_a_value_outside_its_set(self) -> None:
        assert self._run("render", CLASS="nope", ISSUE="", AUTHOR_IS_ACTOR="").returncode == 2

    def test_an_unknown_subcommand_is_a_usage_error(self) -> None:
        assert self._run("frobnicate").returncode == 2


@pytest.mark.skipif(_bash() is None or shutil.which("jq") is None, reason="needs bash and jq")
class TestDeferralCheckAgreesWithIssueProblems:
    """The deferral check calls ``issue_problems``; it must keep telling the author what it always did.

    ``REFERENCE`` is the bash the check carried before it called the module, frozen. Every fixture
    runs through it and through ``issue_problems``, so the two stay one rule.
    """

    REFERENCE = r"""
state="$(printf '%s' "$ISSUE" | jq -r '.state // ""')"
has_label="$(printf '%s' "$ISSUE" | jq -r '[.labels[].name] | index("deferred-finding") != null')"
has_assignee="$(printf '%s' "$ISSUE" | jq -r '(.assignees | length) > 0')"
body_due="$(printf '%s' "$ISSUE" | jq -r '.body // ""' | grep -cE '^ *Due: *[0-9]{4}-[0-9]{2}-[0-9]{2}' || true)"
milestone_due="$(printf '%s' "$ISSUE" | jq -r '.milestone.due_on // ""')"
[ "$state" != "open" ] && echo "closed"
[ "$has_label" != "true" ] && echo "no deferred-finding label"
[ "$has_assignee" != "true" ] && echo "no assignee (owner)"
if [ "${body_due:-0}" -eq 0 ] && [ -z "$milestone_due" ]; then
  echo "no due date"
fi
true
"""

    GOOD = TestIssueProblems.GOOD
    FIXTURES = [
        GOOD,
        {**GOOD, "state": "closed"},
        {**GOOD, "labels": []},
        {**GOOD, "labels": [{"name": "bug"}, {"name": "deferred-finding"}]},
        {**GOOD, "assignees": []},
        {**GOOD, "body": None},
        {**GOOD, "body": ""},
        {**GOOD, "body": "intro\nDue: 2031-01-01\nmore"},
        {**GOOD, "body": "intro\n  Due: 2031-01-01"},
        {**GOOD, "body": "Due:2031-01-01"},
        {**GOOD, "body": "x Due: 2031-01-01"},
        {**GOOD, "body": "Due: 31-01-2031"},
        {**GOOD, "body": "", "milestone": {"due_on": "2031-01-01T00:00:00Z"}},
        {**GOOD, "body": "", "milestone": {"due_on": None}},
        {"state": "open", "labels": [], "assignees": [], "body": "", "milestone": None},
    ]

    @pytest.mark.parametrize("issue", FIXTURES, ids=range(len(FIXTURES)))
    def test_bash_and_python_name_the_same_problems(self, issue: dict) -> None:
        # `_bash()`, not `shutil.which`: on Windows the latter finds the WSL launcher.
        bash = _bash()
        proc = subprocess.run(
            [bash, "-c", self.REFERENCE],
            env=_child_env({**os.environ, "ISSUE": json.dumps(issue)}),
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        reference = proc.stdout.splitlines()
        problems = module.issue_problems(issue)

        assert len(problems) == len(reference), (problems, reference)
        for got, want in zip(problems, reference, strict=True):
            assert got.startswith(want), (problems, reference)
