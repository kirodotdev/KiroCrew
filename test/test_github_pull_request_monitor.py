"""GitHub pull-request monitor provider and canonicalization contracts.

This file pins target parsing and its width bounds, the allowlisted canonical observation
and its fingerprint, check-identity redaction and the check-rollup collapse: when a newer
run supersedes a cancelled row and when same-named rows stay distinct. The readiness
classification matrix, review-thread evidence, head revisions and terminal lifecycles live
in ``test_github_pull_request_monitor_readiness.py``; provider failure categories, the
separate primary and check reads, a target-alias refusal and the shadow probe's persistence
live in ``test_github_pull_request_monitor_errors_and_shadow.py``; batched reads, the plural
probe boundary, the REST fallback on a GraphQL rate limit and the PR comment digest live in
``test_github_pull_request_monitor_batched_reads.py``. The response fixtures and fake
runners these files share live in ``github_pull_request_monitor_helpers.py``.
"""

from __future__ import annotations

import json

import pytest
from github_pull_request_monitor_helpers import (
    _HEAD,
    _check_run,
    _primary,
    _probe_one,
    _provider,
    _threads,
)

from kiro_crew.github_runner import GITHUB_MAX_OWNER_CHARS, GITHUB_MAX_REPO_CHARS
from kiro_crew.monitoring import github_pull_request
from kiro_crew.monitoring.github_pull_request import (
    GitHubPullRequestTarget,
    parse_github_pull_request_target,
)
from kiro_crew.monitoring.models import MonitorObservationStatus


def test_blank_check_label_keeps_the_provider_state_under_an_opaque_identity() -> None:
    """A missing display label must not turn a successful check into unknown."""
    provider, _runner = _provider(
        _primary(
            statusCheckRollup=[
                {
                    "__typename": "CheckRun",
                    "name": "",
                    "workflowName": "CI",
                    "workflowDefinitionId": 7,
                    "workflowRunEvent": "pull_request",
                    "status": "COMPLETED",
                    "conclusion": "SUCCESS",
                }
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    passed = result.canonical["checks"]["passed"]
    assert len(passed) == 1
    assert passed[0].startswith("github_check:")
    assert result.canonical["checks"]["unknown"] == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "https://github.com/owner/repo/pull/123",
            GitHubPullRequestTarget("github.com", "owner", "repo", 123),
        ),
        (
            "https://www.github.com/Owner-1/repo_name/pull/7",
            GitHubPullRequestTarget("github.com", "Owner-1", "repo_name", 7),
        ),
    ],
)
def test_pull_request_target_normalizes_only_public_github_urls(
    raw: str,
    expected: GitHubPullRequestTarget,
) -> None:
    """Changing public-host normalization or typed identity breaks this contract."""
    assert parse_github_pull_request_target(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "owner/repo#123",
        "git@github.com:owner/repo.git",
        "http://github.com/owner/repo/pull/123",
        "https://github.example.com/owner/repo/pull/123",
        "https://enterprise.github.com/owner/repo/pull/123",
        "https://github.com/owner/repo",
        "https://github.com/owner/repo/issues/123",
        "https://github.com/owner/repo/pull/0",
        "https://github.com/owner/repo/pull/-1",
        "https://github.com/owner/repo/pull/not-a-number",
        "https://github.com/owner/repo/pull/123/files",
        "https://github.com/owner/repo/pull/123/",
        "https://github.com/owner//repo/pull/123",
        "https://github.com/owner/repo/pull/123?diff=split",
        "https://github.com/owner/repo/pull/123#discussion",
        "https://user@github.com/owner/repo/pull/123",
        "https://github.com:443/owner/repo/pull/123",
        "https://github.com:notaport/owner/repo/pull/123",
        "https://github.com/../repo/pull/123",
        "https://github.com/owner/repo;touch/pull/123",
    ],
)
def test_pull_request_target_rejects_noncanonical_or_untrusted_input(raw: str) -> None:
    """Weakening target validation would let input select a host or command shape."""
    with pytest.raises(ValueError, match="GitHub pull request"):
        parse_github_pull_request_target(raw)


class TestTargetSegmentsAreBoundedInWidth:
    """``GitHubPullRequestTarget`` bounds the width of its two path segments as well
    as their charset. A charset with no quantifier admits a segment of any size, and
    the segments it admits are what this adapter joins into the ``owner/repo`` slug it
    hands the provider. Widths below are derived from the published limits, never
    restated here.
    """

    def test_segments_at_githubs_own_limits_are_accepted(self) -> None:
        owner = "o" * GITHUB_MAX_OWNER_CHARS
        repo = "r" * GITHUB_MAX_REPO_CHARS

        target = parse_github_pull_request_target(f"https://github.com/{owner}/{repo}/pull/1")

        assert len(target.owner) == GITHUB_MAX_OWNER_CHARS
        assert len(target.repo) == GITHUB_MAX_REPO_CHARS

    @pytest.mark.parametrize("over", [1, 5000])
    def test_an_owner_wider_than_githubs_login_limit_is_refused(self, over: int) -> None:
        owner = "o" * (GITHUB_MAX_OWNER_CHARS + over)

        with pytest.raises(ValueError, match="GitHub pull request"):
            parse_github_pull_request_target(f"https://github.com/{owner}/repo/pull/1")

    @pytest.mark.parametrize("over", [1, 5000])
    def test_a_repository_wider_than_githubs_name_limit_is_refused(self, over: int) -> None:
        repo = "r" * (GITHUB_MAX_REPO_CHARS + over)

        with pytest.raises(ValueError, match="GitHub pull request"):
            parse_github_pull_request_target(f"https://github.com/owner/{repo}/pull/1")

    def test_the_typed_identity_refuses_an_oversized_segment_constructed_directly(
        self,
    ) -> None:
        """The dataclass is the guard, not the URL parser: an in-process caller that
        builds one straight from two strings meets the same bound."""
        with pytest.raises(ValueError, match="GitHub pull request"):
            GitHubPullRequestTarget("github.com", "o" * (GITHUB_MAX_OWNER_CHARS + 1), "repo", 1)


def test_clean_pull_request_has_allowlisted_canonical_observation_and_fingerprint() -> None:
    """Adding provider payload fields or omitting a readiness fact breaks persistence."""
    provider, runner = _provider(_primary(), _threads())

    result = _probe_one(provider)

    assert result.canonical == {
        "blocking_review": "none",
        "checks": {
            "failed": [],
            "passed": ["CI / test", "lint"],
            "pending": [],
            "unknown": [],
        },
        "checks_complete": True,
        "draft": False,
        "head_revision": _HEAD,
        "kind": "github_pull_request",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "review_threads_complete": True,
        "state": "open",
        "target": "github.com/owner/repo#123",
        "unresolved_review_threads": 0,
    }
    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert result.observation.reason_code == "review_ready"
    assert result.observation.fingerprint == (
        "fe6dc90df56bdd1b5f40dc900d3c8af145899e64fbeeac3c86f296cd481d63c5"
    )
    primary_argv, primary_kwargs = runner.calls[0]
    assert primary_argv[:3] == ["/trusted/bin/gh", "api", "graphql"]
    assert primary_argv[3] == "-f"
    assert primary_argv[4] == (
        "query=query($o0:String!,$r0:String!,$n0:Int!)"
        "{s0:repository(owner:$o0,name:$r0){pullRequest(number:$n0){"
        "number state isDraft headRefOid mergeable mergeStateStatus reviewDecision}}}"
    )
    # Owner, repository and number reach GitHub as bound variables, so no part of a
    # subject is ever interpolated into the document above.
    assert primary_argv[5:] == ["-f", "o0=owner", "-f", "r0=repo", "-F", "n0=123"]
    assert primary_kwargs["audit_caller"] == "core:monitor"
    assert primary_kwargs["pin_host"] == "github.com"


def test_reordered_and_volatile_provider_values_keep_the_fingerprint_stable() -> None:
    """Ordering, URLs, request ids, bodies, logs, and timestamps are never durable facts."""
    first_provider, _ = _provider(_primary(), _threads())
    noisy_checks = list(reversed(_primary()["statusCheckRollup"]))
    noisy_checks[0] = {
        **noisy_checks[0],
        "targetUrl": "https://github.com/owner/repo/statuses/different",
        "requestId": "request-2",
    }
    noisy_checks[1] = {
        **noisy_checks[1],
        "workflowRunCreatedAt": "2030-01-01T00:00:00Z",
        "completedAt": "2030-01-01T00:01:00Z",
        "detailsUrl": "https://github.com/owner/repo/actions/runs/999",
        "logText": "credential-like provider output",
    }
    noisy_primary = _primary(
        statusCheckRollup=noisy_checks,
        title="volatile title",
        body="volatile body",
        url="https://github.com/owner/repo/pull/123",
        requestId="request-1",
        updatedAt="2030-01-01T00:00:00Z",
    )
    second_provider, _ = _provider(
        noisy_primary,
        _threads(nodes=[{"isResolved": True}, {"isResolved": True}]),
    )

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

    assert second.canonical == first.canonical
    assert second.observation.fingerprint == first.observation.fingerprint
    serialized = json.dumps(second.canonical, sort_keys=True)
    for forbidden in (
        "request-1",
        "request-2",
        "volatile title",
        "volatile body",
        "credential-like",
        "2030-01-01",
        "https://",
    ):
        assert forbidden not in serialized


def test_check_identity_is_redacted_before_it_enters_canonical_state() -> None:
    """A provider-controlled check label cannot turn monitor state into a secret sink."""
    token = "ghp_abcdefghijklmnopqrstuvwxyz1234567890ABCD"
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(),
                    "workflowName": f"CI {token}",
                    "name": "https://internal.example.test/run?id=secret",
                }
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    serialized = json.dumps(result.canonical, sort_keys=True)
    assert token not in serialized
    assert "internal.example.test" not in serialized
    assert "id=secret" not in serialized


def test_whitespace_only_check_retains_state_under_opaque_identity() -> None:
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    "__typename": "StatusContext",
                    "context": " \t ",
                    "state": "SUCCESS",
                    "targetUrl": "https://github.com/owner/repo/statuses/sha",
                }
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert result.observation.reason_code == "review_ready"
    assert result.canonical["checks"]["passed"]


def test_the_rollup_selection_asks_for_every_field_the_collapse_reads() -> None:
    """The fixtures answer with these fields whether the query requests them or not.

    ``_provider`` replies from canned rows, so every collapse test above stays green
    even if the selection stops asking GitHub for ``startedAt`` or for the run's
    ``databaseId``. In production an absent field reads as an unorderable row, and
    an unorderable row is always kept -- so the collapse would quietly stop
    collapsing, the phantom wake would come back, and no behavioural test would go
    red. This is the one test that fails when the query and the collapse disagree.
    """
    selection = github_pull_request._ROLLUP_SELECTION
    fragment = selection.split("... on CheckRun{", 1)[1].split("... on StatusContext", 1)[0]

    assert "workflowRun{databaseId" in fragment, (
        "supersession is ordered on the monotonic RUN ID, so it has to come back on "
        "every row; it is also the only place _flat_check_row reads it"
    )
    assert "databaseId event" in fragment, (
        "identity includes the RUN's triggering event, since one workflow file can "
        "declare several triggers and each produces its own run on one commit"
    )
    assert "workflow{databaseId" in fragment, (
        "identity is the workflow DEFINITION, not its display name, so the "
        "definition's id must come back too, or every row is exempt from the collapse"
    )
    assert "checkSuite{conclusion" in fragment, (
        "displacement is proven by the RUN's conclusion, read from the check suite "
        "because WorkflowRun exposes none; without it no row can ever be collapsed"
    )


def test_an_attempt_a_newer_run_replaced_is_not_reported_as_a_live_failure() -> None:
    """The phantom wake this mark exists to stop.

    ``cancel-in-progress`` leaves the cancelled attempt in the rollup beside the
    run that replaced it, and ``CANCELLED`` maps to ``failed``, so counting every
    row reports a blocker that is not live and wakes the session on a failure
    nothing can fix. Supersession is decided by the workflow RUN: these two rows
    are one dispatch retried, and only the newer run is live. Order must not
    matter either -- the rollup is not returned in start-time order, so a reader
    that leaned on position would mark the wrong way on the same board.

    The displaced row is declassified, not deleted: it appears under ``superseded``,
    which is what lets the report name the row a suppressed wake was suppressed for.
    """
    superseded = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    replacement = {
        **superseded,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    first_provider, _ = _provider(
        _primary(statusCheckRollup=[superseded, replacement]),
        _threads(),
    )
    second_provider, _ = _provider(
        _primary(statusCheckRollup=[replacement, superseded]),
        _threads(),
    )

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

    assert first.canonical["checks"] == {
        "failed": [],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
        "superseded": ["CI / test"],
    }
    assert first.observation.status is MonitorObservationStatus.SUCCESS
    assert first.observation.reason_code == "review_ready"
    assert first.observation.fingerprint == second.observation.fingerprint


def test_the_row_cap_is_spent_after_the_mark_so_a_cut_successor_revives_no_phantom() -> None:
    """A successor beyond the row cap still declassifies the row it replaced.

    The cap bounds how many rows are reported, not how many are read to decide which
    run is newest. Spending it first can cut the successor while keeping the attempt
    it replaced, and that kept attempt then wins its own key and is reported as a live
    ``failed`` row -- the phantom wake the mark exists to remove, reappearing on
    exactly the boards big enough to hit the cap.
    """
    superseded = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
    }
    replacement = {
        **superseded,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
    }
    filler = [
        {"__typename": "StatusContext", "context": f"filler-{index:03d}", "state": "SUCCESS"}
        for index in range(github_pull_request._MAX_CHECK_ROWS - 1)
    ]
    provider, _ = _provider(
        _primary(statusCheckRollup=[superseded, *filler, replacement]),
        _threads(),
    )

    result = _probe_one(provider)

    checks = result.canonical["checks"]
    assert isinstance(checks, dict)
    assert checks["failed"] == []
    assert checks["superseded"] == ["CI / test"]
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "checks_incomplete"


def test_an_oversized_displaced_bucket_does_not_report_the_board_unmeasured() -> None:
    """Displaced rows carry no verdict, so their own size is not a completeness claim.

    Every live row is still measured when a head accumulates more displaced rows than
    one bucket holds, and reporting the board incomplete there would hold a green
    board pending on rows that decide nothing. They are also all handed on, so the one
    cut happens where the projection can announce it.
    """
    live = github_pull_request.GitHubCheck("CI / test", "passed")
    displaced = tuple(
        github_pull_request.GitHubCheck(f"CI / old-{index:03d}", "superseded")
        for index in range(101)
    )

    bounded, complete = github_pull_request._bounded_checks((live, *displaced))

    assert complete is True
    assert sum(1 for check in bounded if check.state == "superseded") == 101
    assert live in bounded


def test_two_rows_of_one_workflow_run_are_both_live_and_neither_is_marked() -> None:
    """Two rows of ONE run are concurrent, so start time must not collapse them.

    A workflow can publish a check run through the Checks API under the same
    display name as its own Actions job, and both rows are live at once. Ordering
    them by ``startedAt`` and keeping the newest would let the later row erase the
    earlier row's failure, which is the one outcome this collapse may never
    produce. Measured on a live fork lane: a description check published FAILURE two
    seconds after its own job reported SUCCESS, on a head whose ``PR Readiness``
    status was itself FAILURE.
    """
    job = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 42,
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    published_later = {
        **job,
        "conclusion": "FAILURE",
        "workflowRunCreatedAt": "2026-08-21T00:00:02Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[job, published_later]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["CI / test"],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "checks_failed"


def test_duplicate_failed_check_rows_preserve_multiplicity_in_the_fingerprint() -> None:
    """A second same-labelled blocker must change the durable observation."""
    first_provider, _ = _provider(
        _primary(statusCheckRollup=[_check_run(conclusion="FAILURE")]),
        _threads(),
    )
    second_provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                _check_run(conclusion="FAILURE"),
                {
                    **_check_run(conclusion="FAILURE"),
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/200/job/202",
                },
            ]
        ),
        _threads(),
    )

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

    assert first.canonical["checks"]["failed"] == ["CI / test"]
    assert second.canonical["checks"]["failed"] == ["CI / test", "CI / test"]
    assert first.observation.fingerprint != second.observation.fingerprint


def test_a_row_whose_run_was_not_identified_is_never_treated_as_superseded() -> None:
    """An unidentified run cannot lose, because it may BE the run that would win.

    A row carrying no run id could belong to the newer run itself, in which case it
    is concurrent with it rather than replaced by it. Letting a dated run supersede
    it would drop a live failure on the strength of a field the response simply did
    not carry, so an unidentified run is exempt whatever its start time says.
    """
    unidentified = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    dated_run = {
        **unidentified,
        "workflowRunId": 200,
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[unidentified, dated_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_two_workflows_sharing_a_display_name_do_not_supersede_each_other() -> None:
    """A display name is not an identity, so it must not decide supersession.

    GitHub permits two workflow files to carry the same ``name:``, and each can
    publish a check of the same name. Those are two independent workflows: neither
    replaces the other, and dropping the earlier one because the other started
    later hides a live failure. Only the workflow DEFINITION id separates them, so
    identity is keyed on it rather than on the label.
    """
    failing_workflow = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 11,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    other_workflow_same_name = {
        **failing_workflow,
        "workflowDefinitionId": 22,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[failing_workflow, other_workflow_same_name]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "the failing workflow's row is live; only its own newer run may replace it"
    assert result.canonical["checks"]["passed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_a_row_whose_workflow_definition_was_not_identified_is_never_collapsed() -> None:
    """The collapse never runs on an id the response did not supply.

    ``Workflow.databaseId`` is a nullable ``Int`` in the schema even though
    ``WorkflowRun.workflow`` is non-null, so a row can name its run while leaving
    its workflow definition unidentified. Without that id two rows cannot be shown
    to be the same check, and the only thing left to key on is the display name,
    which is what this collapse refuses to trust.
    """
    unidentified_workflow = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    later_run = {
        **unidentified_workflow,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[unidentified_workflow, later_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_a_fail_fast_cancelled_row_in_a_live_run_is_never_collapsed() -> None:
    """A row reaches CANCELLED inside runs that were never displaced.

    ``fail-fast`` cancels a matrix job's siblings the moment one of them fails, a job
    is cancelled when something in its ``needs`` fails, and an operator can cancel a
    single job. In every one of those the row is ``COMPLETED``+``CANCELLED`` while its
    run concluded ``FAILURE`` and is entirely live. Reading the row's own cancellation
    as displacement would drop it, and since the fold re-runs identically on every
    poll the loss is silent and never self-corrects: the monitor would report
    ``review_ready`` on a run that failed. Displacement is a property of the run, so
    it is read from the run.
    """
    fail_fast_cancelled = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "FAILURE",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    later_live_run = {
        **fail_fast_cancelled,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[fail_fast_cancelled, later_live_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "the run concluded FAILURE, so its cancelled row was not displaced"
    assert result.observation.reason_code == "checks_failed"
    assert result.observation.status is not MonitorObservationStatus.SUCCESS


def test_a_row_that_reached_a_verdict_inside_a_cancelled_run_keeps_it() -> None:
    """A cancelled run can still hold a row that decided before the cancel landed.

    The run's cancellation proves the row was displaced; it does not prove the row is
    empty. A job that already finished FAILURE keeps that verdict while its run is
    cancelled around it, and dropping it would discard a real failure. So the row's
    own ``COMPLETED``+``CANCELLED`` is required too, and only a row cancelled inside a
    cancelled run is dropped.
    """
    decided_in_cancelled_run = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    replacement = {
        **decided_in_cancelled_run,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[decided_in_cancelled_run, replacement]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "a row that reached FAILURE keeps it even though its run was cancelled"
    assert result.observation.reason_code == "checks_failed"


def test_a_run_whose_conclusion_was_not_supplied_is_never_treated_as_displaced() -> None:
    """Absent run conclusion is not displacement proof, so the row stays.

    ``CheckSuite.conclusion`` is null while a run is still going and can be withheld
    like any other field. Evidence the host did not supply is not evidence that a row
    was replaced, so the row is kept and its verdict counted. Over-report rather than
    hide a live failure.
    """
    no_run_conclusion = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    later_run = {
        **no_run_conclusion,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[no_run_conclusion, later_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "no run conclusion was supplied, so nothing proves this row was replaced"
    assert result.observation.reason_code == "checks_failed"


def test_a_newest_run_without_a_conclusion_still_displaces_an_older_cancelled_row() -> None:
    """An absent run conclusion exempts a row from REMOVAL, never from winning.

    The winner is chosen on run id alone, so a newest row whose run conclusion the
    host withheld still supersedes the round before it. Only a row's OWN removal
    needs displacement proof, which is why the older cancelled run is the one that
    goes. The sibling test above pins the other half: such a row is itself kept.
    """
    displaced = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    newest_without_run_conclusion = {
        key: value for key, value in displaced.items() if key != "workflowRunConclusion"
    }
    newest_without_run_conclusion.update(
        {
            "workflowRunId": 200,
            "conclusion": "SUCCESS",
            "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
        }
    )
    provider, _ = _provider(
        _primary(statusCheckRollup=[displaced, newest_without_run_conclusion]),
        _threads(),
    )

    result = _probe_one(provider)

    assert (
        result.canonical["checks"]["failed"] == []
    ), "run 200 won on its id alone, so the cancelled round it replaced must not count"


def test_a_boolean_id_is_refused_rather_than_grouped_with_the_integer_one() -> None:
    """``True`` is an ``int`` subclass, so accepting it would group it with ``1``.

    ``True == 1`` and ``hash(True) == hash(1)``, so a boolean workflow-definition id
    lands on the same dictionary key as the integer ``1`` and the two rows are read
    as one check. They are not: a boolean is not an id the host assigned, and letting
    it group drops the cancelled row's live verdict on the strength of a value that
    never identified anything. Refusing it leaves the row exempt and its verdict
    counted, which over-reports rather than hides.
    """
    boolean_definition = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": True,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    integer_definition_later_run = {
        **boolean_definition,
        "workflowDefinitionId": 1,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[boolean_definition, integer_definition_later_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "a boolean id identifies nothing, so its row cannot be superseded by id 1"
    assert result.canonical["checks"]["passed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_a_run_still_in_flight_is_never_treated_as_cancelled() -> None:
    """Only a COMPLETED cancellation proves displacement; an in-flight row is live.

    Displacement is established by the concurrency group having cancelled a run, and
    that is a terminal fact: the run finished, as CANCELLED, carrying no verdict of
    its own. A row still in flight has reached no such state, so its ``conclusion``
    cannot license dropping it. Dropping it would delete a lane that is still
    running and report the round settled, which is the inverse of this collapse's
    purpose -- it exists to stop the monitor acting on a state that is not live, not
    to declare an unfinished one absent.
    """
    still_running = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "IN_PROGRESS",
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    later_run = {
        **still_running,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[still_running, later_run]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["pending"] == [
        "CI / test"
    ], "an in-flight run has not been cancelled, so it stays in the rollup"
    assert result.canonical["checks"]["failed"] == []
    assert result.observation.reason_code == "checks_pending"


def test_an_unidentified_run_does_not_decide_which_identified_run_is_newest() -> None:
    """An unidentified run can neither lose nor win.

    It cannot lose, because it may be the very run that would replace the others.
    For the same reason it cannot win: letting its start time set the winner would
    make every identified run look superseded, including the newest one, and that
    drops a live failure while reporting the round clean. Both halves are one rule,
    so both need a test.
    """
    older_pass = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
    }
    newest_identified_failure = {
        **older_pass,
        "workflowRunId": 200,
        "conclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
    }
    later_but_unidentified = {
        key: value for key, value in older_pass.items() if key != "workflowRunId"
    }
    later_but_unidentified["workflowRunCreatedAt"] = "2026-08-23T00:00:00Z"
    provider, _ = _provider(
        _primary(statusCheckRollup=[older_pass, newest_identified_failure, later_but_unidentified]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["CI / test"], (
        "run 200 is the newest run anyone can identify, so its failure is live "
        "however late the unidentified row claims to have started"
    )
    assert result.observation.reason_code == "checks_failed"


def test_a_replaced_run_that_completed_is_kept_because_only_cancellation_proves_it() -> None:
    """Being older is not proof of replacement; being cancelled is.

    The rollup carries no lineage: nothing in it says one run replaced another. A
    higher run id proves only that a run started later, and a later run of the same
    workflow can be an independent dispatch -- one workflow file may fire on several
    events, and the event field is coarser than the action, so ``synchronize`` and
    ``edited`` runs share it. Inferring replacement from recency therefore drops rows
    that were never replaced.

    Cancellation is different. A run the concurrency group cancelled was displaced by
    the run that cancelled it, and a cancelled row carries no verdict of its own, so
    dropping it cannot lose a failure. A row that COMPLETED holds a real verdict and
    is kept however old its run is, even though that leaves the phantom this change
    exists to remove when the replaced round completed rather than being cancelled.
    Over-report rather than hide a live failure.
    """
    older_completed_failure = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "status": "COMPLETED",
        "conclusion": "FAILURE",
    }
    newer_pass = {
        **older_completed_failure,
        "workflowRunId": 200,
        "conclusion": "SUCCESS",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[older_completed_failure, newer_pass]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["CI / test"], (
        "a completed verdict is not displaced by a later run starting; only the "
        "cancellation of its own run proves that"
    )
    assert result.observation.reason_code == "checks_failed"


def test_two_runs_created_in_the_same_second_are_ordered_by_run_id() -> None:
    """A timestamp with one-second granularity cannot order two runs of one identity.

    ``WorkflowRun.createdAt`` resolves to the second, and two runs of one workflow on
    one head routinely share it -- a workflow firing on both ``synchronize`` and
    ``edited`` produces exactly that, both under the ``pull_request`` event, so they
    share this mark's identity. Ordering on that timestamp leaves them tied, neither
    row is marked, and the bucket resolves to the cancelled one: a lane whose newest
    run succeeded reads as a blocking failure. Run ids increase monotonically, so
    they order the pair on their own. This repository's readiness aggregate reached
    the same conclusion and records it at `.github/workflows/pr-readiness.yml`.

    Both rows carry the SAME ``workflowRunCreatedAt`` deliberately: it is what makes a
    mutation back to timestamp ordering fail this test.
    """
    superseded_twin = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunConclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T10:00:00Z",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
    }
    newer_same_second = {
        **superseded_twin,
        "workflowRunId": 200,
        "workflowRunConclusion": "SUCCESS",
        "conclusion": "SUCCESS",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[superseded_twin, newer_same_second]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": [],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
        "superseded": ["CI / test"],
    }
    assert result.observation.reason_code == "review_ready"


def test_two_triggers_of_one_workflow_do_not_supersede_each_other() -> None:
    """Runs of one workflow from different events are independent, not attempts.

    A workflow declaring ``on: [push, pull_request]`` produces two runs of the same
    definition on one commit, and each reports its own check of the same name. They
    are concurrent dispatches, so neither replaces the other, and keeping only the
    later one drops a live failure. What makes two runs an attempt and its
    replacement is sharing the TRIGGER as well as the definition.
    """
    push_run_failed = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunId": 100,
        "workflowRunEvent": "push",
        "workflowRunConclusion": "CANCELLED",
        "workflowRunCreatedAt": "2026-08-21T10:00:00Z",
        "status": "COMPLETED",
        "conclusion": "CANCELLED",
    }
    pull_request_run_passed = {
        **push_run_failed,
        "workflowRunId": 200,
        "workflowRunEvent": "pull_request",
        "workflowRunConclusion": "SUCCESS",
        "workflowRunCreatedAt": "2026-08-21T10:05:00Z",
        "conclusion": "SUCCESS",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[push_run_failed, pull_request_run_passed]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == [
        "CI / test"
    ], "the push run's failure is live; only a later PUSH run may replace it"
    assert result.observation.reason_code == "checks_failed"


def test_a_queued_job_does_not_make_its_older_run_the_newer_one() -> None:
    """Order runs by when the RUN was created, not by when its job got a runner.

    A check row's ``startedAt`` is when that job started, which waits on runner
    availability. So an older run's job can start after a newer run's, and ordering
    by it picks the older run as the winner and drops the newer run's rows. Here the
    newer run is the one that failed, so that inversion hides a live failure and
    reports the round clean. ``WorkflowRun.createdAt`` is run-level and cannot be
    reordered by queueing.
    """
    older_run_queued_late = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "workflowRunId": 100,
        "workflowRunCreatedAt": "2026-08-21T10:00:00Z",
        "startedAt": "2026-08-21T10:20:00Z",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
    }
    newer_run_started_at_once = {
        **older_run_queued_late,
        "workflowRunId": 200,
        "workflowRunCreatedAt": "2026-08-21T10:05:00Z",
        "startedAt": "2026-08-21T10:06:00Z",
        "conclusion": "FAILURE",
    }
    provider, _ = _provider(
        _primary(statusCheckRollup=[older_run_queued_late, newer_run_started_at_once]),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["CI / test"], (
        "run 200 was created later, so its failure is the live one however long "
        "run 100's job sat waiting for a runner"
    )
    assert result.observation.reason_code == "checks_failed"


def test_independent_workflows_with_same_check_name_remain_distinct() -> None:
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(conclusion="FAILURE"),
                    "workflowName": "Backend",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/201",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "workflowName": "Frontend",
                    "workflowRunCreatedAt": "2026-08-23T00:00:00Z",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/200/job/301",
                },
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["Backend / test"],
        "passed": ["Frontend / test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE


def test_independent_same_workflow_jobs_with_same_name_remain_distinct() -> None:
    """Display names cannot prove that two check runs are rerun attempts."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(conclusion="FAILURE"),
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/201",
                    "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/202",
                    "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["CI / test"],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE


def test_distinct_raw_check_identities_cannot_collapse_during_redaction() -> None:
    """Sanitization must not let one provider check hide another check's failure."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(conclusion="FAILURE"),
                    "name": "https://failure.example.test/run",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "name": "https://success.example.test/run",
                    "workflowRunCreatedAt": "2026-08-23T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "checks_failed"
    assert result.canonical["checks"]["failed"] == ["CI / [provider-url]"]
    assert result.canonical["checks"]["passed"] == ["CI / [provider-url]"]


def test_same_workflow_checks_without_dispatch_identity_remain_independent() -> None:
    """A display-name match alone cannot prove that one job supersedes another."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                _check_run(conclusion="SUCCESS"),
                _check_run(conclusion="FAILURE"),
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["CI / test"],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE


def test_same_named_workflowless_check_runs_remain_distinct() -> None:
    """Rows without workflow identity cannot safely be treated as one rerun chain."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(conclusion="FAILURE"),
                    "workflowName": "",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/201",
                    "workflowRunCreatedAt": "2026-08-21T00:00:00Z",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "workflowName": "",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/202",
                    "workflowRunCreatedAt": "2026-08-22T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["test"],
        "passed": ["test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE


def test_status_context_failure_cannot_be_hidden_by_same_named_check_run() -> None:
    """Status contexts and check runs use distinct provider namespaces."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    "__typename": "StatusContext",
                    "context": "test",
                    "state": "FAILURE",
                    "targetUrl": "https://github.com/owner/repo/statuses/sha",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "workflowName": "",
                },
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": ["test"],
        "passed": ["test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE


def test_same_dispatch_queued_attempt_cannot_hide_an_older_completion() -> None:
    """Ambiguous same-dispatch attempts remain independent and fail closed."""
    queued = _check_run(status="QUEUED", conclusion="")
    queued["startedAt"] = None
    queued["detailsUrl"] = "https://github.com/owner/repo/actions/runs/100/job/202"
    completed = _check_run(conclusion="SUCCESS")
    completed["detailsUrl"] = "https://github.com/owner/repo/actions/runs/100/job/201"
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                completed,
                queued,
            ]
        ),
        _threads(),
    )

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": [],
        "passed": ["CI / test"],
        "pending": ["CI / test"],
        "unknown": [],
    }
    assert result.observation.reason_code == "checks_pending"
