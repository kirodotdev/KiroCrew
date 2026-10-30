"""A workflow's written ``permissions:`` must cover the calls its scripts make.

An explicit ``permissions:`` block turns every scope it does not list into ``none``.
A handler that reads an issue with the workflow token and has no ``issues: read``
gets a 403 on every call; if it retries and fails closed, the outage looks like a
flaky API. The required scopes are DERIVED from the calls in the scripts
(``workflow_token_scopes``), never hand-listed, so a new call that needs a new
scope fails here instead of in production.

The second class here is who a comment-triggered workflow believes: on an
``edited`` event the comment's author is not necessarily the person who made the
edit, so a workflow that acts on ``comment.user`` must also compare the event
sender.
"""

from __future__ import annotations

import re

import pytest
from workflow_token_scopes import (
    WORKFLOWS,
    Need,
    effective_permissions,
    grants,
    job_needs,
    load_workflow,
    missing_scopes,
)

ALL_WORKFLOWS = sorted(p.name for p in WORKFLOWS.glob("*.yml"))

# Call shapes the scanner reports as `unknown`, each judged by hand. Anything not
# listed fails, so a new shape must be taught to the scanner or acknowledged here
# with the reason it needs no scope beyond the grant.
ACKNOWLEDGED_UNKNOWN = {
    # Two GraphQL reads of merged pull requests and default-branch history; the
    # workflow grants contents and pull-requests, which cover both.
    ("add-contributor.yml", "gh api graphql"),
}


def _scoped_jobs(name: str):
    workflow = load_workflow(WORKFLOWS / name)
    for job_name, job in (workflow.get("jobs") or {}).items():
        if isinstance(job, dict) and effective_permissions(workflow, job) is not None:
            yield workflow, job_name, job


class TestEveryWorkflowGrantsTheScopesItsCallsNeed:
    @pytest.mark.parametrize("name", ALL_WORKFLOWS)
    def test_no_call_needs_a_scope_the_block_leaves_out(self, name: str) -> None:
        missing = missing_scopes(name, load_workflow(WORKFLOWS / name))

        assert not missing, "\n".join(
            f"{m.where}: `{m.call}` needs {'/'.join(m.scopes)}: {m.level}" for m in missing
        )

    @pytest.mark.parametrize("name", ALL_WORKFLOWS)
    def test_no_call_shape_goes_unread(self, name: str) -> None:
        unread = [
            f"{name}:{job_name}: {call}"
            for workflow, job_name, _job in _scoped_jobs(name)
            for call in job_needs(name, workflow, job_name).unknown
            if (name, call) not in ACKNOWLEDGED_UNKNOWN
        ]

        assert not unread, unread


class TestTheOverrideHandlerReadsItsIssue:
    """Class `deferred` reads a release issue."""

    NAME = "ai-review-human-override.yml"

    def _record(self):
        workflow = load_workflow(WORKFLOWS / self.NAME)
        return workflow, workflow["jobs"]["record"]

    def test_the_scanner_sees_the_issue_read_in_the_script(self) -> None:
        workflow, _job = self._record()

        reads = [
            n
            for n in job_needs(self.NAME, workflow, "record").needs
            if n.scopes == ("issues",) and n.level == "read"
        ]

        assert reads, "the issue read in the Validate step was not derived"
        assert any("issues/$release_issue" in n.call for n in reads)

    def test_the_grant_holds_issues_read(self) -> None:
        workflow, job = self._record()
        granted = effective_permissions(workflow, job)

        assert granted is not None
        assert granted.get("issues") == "read"

    def test_the_grant_holds_every_scope_the_handler_calls_need(self) -> None:
        workflow, job = self._record()
        granted = effective_permissions(workflow, job)
        needs = job_needs(self.NAME, workflow, "record").needs

        assert granted is not None
        assert needs
        assert [n.call for n in needs if not grants(granted, n)] == []

    def test_the_handler_asks_for_no_more_than_it_uses(self) -> None:
        # Least privilege: a scope the derived calls never use should not be
        # held. `checks: write` and `contents: read` are used by the re-run and
        # the module load; `id-token` and `contents: write` are not granted.
        workflow, job = self._record()
        granted = effective_permissions(workflow, job)
        needs = job_needs(self.NAME, workflow, "record").needs
        used = {scope for n in needs for scope in n.scopes}

        assert granted is not None
        assert "id-token" not in granted
        assert granted.get("contents") != "write"
        for scope in granted:
            assert scope in used or scope == "checks", scope

    def test_without_the_scope_the_derived_check_fails(self) -> None:
        # The check has teeth: the block this PR first shipped, minus issues,
        # is reported against the issue read.
        workflow, job = self._record()
        shipped = {k: v for k, v in effective_permissions(workflow, job).items() if k != "issues"}
        needs = job_needs(self.NAME, workflow, "record").needs

        failing = [n.call for n in needs if not grants(shipped, n)]

        assert failing == ["gh api GET repos/$REPO/issues/$release_issue"]


class TestTheDeferralCheckStillCoversItsReads:
    def test_the_check_reads_the_issue_and_the_module_it_runs(self) -> None:
        workflow = load_workflow(WORKFLOWS / "disposition-deferral-check.yml")
        job = workflow["jobs"]["validate-deferral"]
        granted = effective_permissions(workflow, job)
        needs = job_needs("disposition-deferral-check.yml", workflow, "validate-deferral").needs

        assert granted is not None
        assert {(n.scopes, n.level) for n in needs} >= {
            (("issues",), "read"),
            (("contents",), "read"),
        }
        assert [n.call for n in needs if not grants(granted, n)] == []


class TestTheReviewPublishersCoverTheirWrites:
    @pytest.mark.parametrize(
        ("name", "job_name"),
        [
            ("codex-review.yml", "codex-review"),
            ("security-scope-review.yml", "generate"),
            ("security-scope-review.yml", "publish"),
            ("fork-security-scope-review.yml", "generate"),
            ("fork-security-scope-review.yml", "publish"),
            ("pr-size-override.yml", "record"),
            ("sensitive-change-review-comment.yml", "rerun"),
        ],
    )
    def test_the_job_holds_what_its_calls_derive(self, name: str, job_name: str) -> None:
        workflow = load_workflow(WORKFLOWS / name)
        granted = effective_permissions(workflow, workflow["jobs"][job_name])
        needs = job_needs(name, workflow, job_name).needs

        assert granted is not None
        assert needs, f"{name}:{job_name} makes no derived call; the scanner lost it"
        assert [n.call for n in needs if not grants(granted, n)] == []


class TestTheScannerItself:
    def _wf(self, run: str, perms: dict | None = None, job_perms: dict | None = None):
        job: dict = {
            "steps": [{"name": "s", "env": {"GH_TOKEN": "${{ github.token }}"}, "run": run}]
        }
        if job_perms is not None:
            job["permissions"] = job_perms
        workflow: dict = {"jobs": {"j": job}}
        if perms is not None:
            workflow["permissions"] = perms
        return workflow

    def test_a_missing_read_is_found(self) -> None:
        wf = self._wf('gh api "repos/$REPO/issues/$N"', {"contents": "read"})

        assert [m.scopes for m in missing_scopes("w.yml", wf)] == [("issues",)]

    def test_a_write_is_not_satisfied_by_a_read(self) -> None:
        wf = self._wf(
            'gh api --method POST "repos/$REPO/issues/$N/comments" -f body=x',
            {"pull-requests": "read", "issues": "read"},
        )

        assert len(missing_scopes("w.yml", wf)) == 1

    def test_either_issues_or_pull_requests_write_covers_a_comment(self) -> None:
        for scope in ("issues", "pull-requests"):
            wf = self._wf(
                'gh api --method POST "repos/$REPO/issues/$N/comments" -f body=x',
                {scope: "write"},
            )
            assert missing_scopes("w.yml", wf) == []

    def test_a_job_block_replaces_the_workflow_block(self) -> None:
        wf = self._wf('gh api "repos/$REPO/issues/$N"', {"issues": "read"}, {"contents": "read"})

        assert len(missing_scopes("w.yml", wf)) == 1

    def test_no_block_means_the_repository_default_and_is_not_judged(self) -> None:
        assert missing_scopes("w.yml", self._wf('gh api "repos/$REPO/issues/$N"')) == []

    def test_a_token_minted_elsewhere_is_not_governed_by_the_block(self) -> None:
        wf = self._wf('gh api "repos/$REPO/issues/$N"', {"contents": "read"})
        wf["jobs"]["j"]["steps"][0]["env"]["GH_TOKEN"] = "${{ steps.app.outputs.token }}"

        assert missing_scopes("w.yml", wf) == []

    def test_prose_that_names_a_gh_command_is_not_a_call(self) -> None:
        run = 'echo "re-run `gh pr edit` or gh api repos/x/issues/1"\n# gh run rerun\n'

        assert missing_scopes("w.yml", self._wf(run, {"contents": "read"})) == []

    def test_a_continued_line_is_one_call(self) -> None:
        run = 'gh api --method POST \\\n  "repos/$REPO/actions/runs/1/rerun"'

        assert [m.scopes for m in missing_scopes("w.yml", self._wf(run, {"contents": "read"}))] == [
            ("actions",)
        ]

    def test_a_call_inside_a_command_substitution_is_found(self) -> None:
        run = 'if x="$(gh api "repos/$REPO/pulls/$PR" 2>err)"; then :; fi'

        assert [m.scopes for m in missing_scopes("w.yml", self._wf(run, {"contents": "read"}))] == [
            ("pull-requests",)
        ]

    def test_an_unrecognised_shape_is_reported_not_passed(self) -> None:
        from workflow_token_scopes import job_needs as needs_of

        wf = self._wf("gh frobnicate now", {"contents": "read"})

        assert needs_of("w.yml", wf, "j").unknown == ["gh frobnicate now"]

    def test_a_github_script_call_is_derived(self) -> None:
        wf = {
            "permissions": {"contents": "read"},
            "jobs": {
                "j": {
                    "steps": [
                        {
                            "uses": "actions/github-script@v7",
                            "with": {"script": "await github.rest.issues.createComment({})"},
                        }
                    ]
                }
            },
        }

        assert [m.level for m in missing_scopes("w.yml", wf)] == ["write"]

    def test_need_ranks_levels(self) -> None:
        assert grants({"issues": "write"}, Need(("issues",), "read", "c", "w"))
        assert not grants({"issues": "read"}, Need(("issues",), "write", "c", "w"))
        assert not grants({}, Need(("issues",), "read", "c", "w"))


def _trigger_types(workflow: dict, event: str) -> list[str]:
    # PyYAML reads the bare key `on` as the boolean True.
    triggers = workflow.get("on", workflow.get(True)) or {}
    if not isinstance(triggers, dict) or event not in triggers:
        return []
    return list((triggers[event] or {}).get("types") or [])


COMMENT_EVENTS = ("issue_comment", "pull_request_review_comment", "pull_request_review")


def _edit_triggered_workflows() -> list[str]:
    names = []
    for name in ALL_WORKFLOWS:
        workflow = load_workflow(WORKFLOWS / name)
        if any("edited" in _trigger_types(workflow, event) for event in COMMENT_EVENTS):
            names.append(name)
    return names


class TestACommentIsBelievedOnlyWhenItsSenderMadeIt:
    def test_the_sweep_finds_the_edit_triggered_workflows(self) -> None:
        assert "ai-review-human-override.yml" in _edit_triggered_workflows()
        assert "sensitive-change-review-comment.yml" in _edit_triggered_workflows()

    @pytest.mark.parametrize("name", _edit_triggered_workflows())
    def test_a_workflow_that_trusts_the_comment_author_compares_the_sender(self, name: str) -> None:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        # `github.event.comment.user.login` / `review.user.login` is the
        # AUTHOR of the thing edited; `github.event.sender.login` is who acted.
        trusts_author = re.search(r"github\.event\.(?:comment|review)\.user\.login", text)

        if trusts_author:
            assert "github.event.sender.login" in text, (
                f"{name} acts on the comment author on an `edited` event without "
                "comparing the event sender"
            )

    def test_the_author_comment_rerun_ignores_an_edit_by_someone_else(self) -> None:
        workflow = load_workflow(WORKFLOWS / "sensitive-change-review-comment.yml")
        condition = " ".join(str(workflow["jobs"]["rerun"]["if"]).split())

        assert "github.event.sender.login == github.event.comment.user.login" in condition
        assert "github.event.comment.user.login == github.event.issue.user.login" in condition
