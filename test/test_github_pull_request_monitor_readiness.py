"""How the GitHub probe classifies a pull request's readiness facts into a typed outcome.

Pins the classification matrix and the precedence of a known actionable fact over pending or
unknown ones, review-thread evidence that spans pages or is capped, partial or malformed, a
supplemental read that fails or raises beside a readable primary read (with the primary
facts the shadow probe persists then), a changed or missing head revision and a terminal
lifecycle that reads no review threads. The canonical observation and the check-rollup
collapse live in ``test_github_pull_request_monitor.py``; provider failure categories and
the shadow probe's persistence live in
``test_github_pull_request_monitor_errors_and_shadow.py``; batched reads, the REST fallback
and the comment digest live in ``test_github_pull_request_monitor_batched_reads.py``; the
shared fixtures live in ``github_pull_request_monitor_helpers.py``.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from copy import deepcopy

import pytest
from github_pull_request_monitor_helpers import (
    _HEAD,
    _check_run,
    _comments,
    _completed,
    _core_and_rollup,
    _envelope,
    _pr_node,
    _primary,
    _probe_one,
    _provider,
    _threads,
)

from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProvider
from kiro_crew.monitoring.models import (
    MonitorDecision,
    MonitorObservationStatus,
    MonitorState,
    ProviderErrorKind,
    monitor_state_to_dict,
)
from kiro_crew.monitoring.shadow import run_shadow_probe


@pytest.mark.parametrize(
    ("primary_changes", "thread_nodes", "status", "reason"),
    [
        (
            {"statusCheckRollup": [_check_run(status="IN_PROGRESS", conclusion="")]},
            None,
            MonitorObservationStatus.PENDING,
            "checks_pending",
        ),
        (
            {"statusCheckRollup": [_check_run(conclusion="FROBNICATED")]},
            None,
            MonitorObservationStatus.PENDING,
            "checks_unknown",
        ),
        (
            {"statusCheckRollup": [_check_run(conclusion="FAILURE")]},
            None,
            MonitorObservationStatus.ACTIONABLE,
            "checks_failed",
        ),
        (
            {"reviewDecision": "CHANGES_REQUESTED"},
            None,
            MonitorObservationStatus.ACTIONABLE,
            "changes_requested",
        ),
        (
            {},
            [{"isResolved": True}, {"isResolved": False}],
            MonitorObservationStatus.ACTIONABLE,
            "unresolved_review_threads",
        ),
        (
            {"mergeable": "CONFLICTING", "mergeStateStatus": "DIRTY"},
            None,
            MonitorObservationStatus.ACTIONABLE,
            "merge_conflict",
        ),
        (
            {"mergeStateStatus": "BEHIND"},
            None,
            MonitorObservationStatus.ACTIONABLE,
            "branch_behind",
        ),
        (
            {"mergeStateStatus": "BLOCKED"},
            None,
            MonitorObservationStatus.PENDING,
            "mergeability_pending",
        ),
        (
            {
                "mergeStateStatus": "BLOCKED",
                "statusCheckRollup": [_check_run(status="IN_PROGRESS", conclusion="")],
            },
            None,
            MonitorObservationStatus.PENDING,
            "checks_pending",
        ),
        (
            {
                "mergeStateStatus": "BLOCKED",
                "reviewDecision": "REVIEW_REQUIRED",
            },
            None,
            MonitorObservationStatus.PENDING,
            "review_required",
        ),
        (
            {"isDraft": True},
            None,
            MonitorObservationStatus.PENDING,
            "pull_request_draft",
        ),
        (
            {"mergeable": "UNKNOWN", "mergeStateStatus": "UNKNOWN"},
            None,
            MonitorObservationStatus.PENDING,
            "mergeability_pending",
        ),
        (
            {"reviewDecision": "UNKNOWN"},
            None,
            MonitorObservationStatus.PENDING,
            "review_state_unknown",
        ),
        (
            {"reviewDecision": "REVIEW_REQUIRED"},
            None,
            MonitorObservationStatus.PENDING,
            "review_required",
        ),
        (
            {"state": "CLOSED"},
            None,
            MonitorObservationStatus.BLOCKED,
            "pull_request_closed",
        ),
        (
            {"state": "MERGED"},
            None,
            MonitorObservationStatus.SUCCESS,
            "pull_request_merged",
        ),
    ],
)
def test_pull_request_classification_matrix(
    primary_changes: dict[str, object],
    thread_nodes: list[dict[str, object]] | None,
    status: MonitorObservationStatus,
    reason: str,
) -> None:
    """Changing one readiness fact must select its conservative typed outcome."""
    provider, _ = _provider(_primary(**primary_changes), _threads(nodes=thread_nodes))

    result = _probe_one(provider)

    assert result.observation.status is status
    assert result.observation.reason_code == reason


@pytest.mark.parametrize(
    ("primary_changes", "thread_nodes", "reason"),
    [
        (
            {
                "statusCheckRollup": [
                    _check_run(conclusion="FAILURE"),
                    {
                        "__typename": "StatusContext",
                        "context": "deploy",
                        "state": "PENDING",
                    },
                ]
            },
            None,
            "checks_failed",
        ),
        (
            {
                "reviewDecision": "CHANGES_REQUESTED",
                "statusCheckRollup": [_check_run(conclusion="FROBNICATED")],
            },
            None,
            "changes_requested",
        ),
        (
            {"statusCheckRollup": [_check_run(status="IN_PROGRESS", conclusion="")]},
            [{"isResolved": False}],
            "unresolved_review_threads",
        ),
        (
            {
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "statusCheckRollup": [_check_run(status="IN_PROGRESS", conclusion="")],
            },
            None,
            "merge_conflict",
        ),
    ],
)
def test_known_actionable_fact_precedes_simultaneous_pending_or_unknown_fact(
    primary_changes: dict[str, object],
    thread_nodes: list[dict[str, object]] | None,
    reason: str,
) -> None:
    """Known work must wake the owner even when unrelated provider facts are unsettled."""
    provider, _ = _provider(_primary(**primary_changes), _threads(nodes=thread_nodes))

    result = _probe_one(provider)

    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == reason


def test_actionable_fingerprint_ignores_unrelated_pending_check_churn() -> None:
    """Renaming an unsettled check cannot issue a second wake for the same known failure."""

    def mixed_checks(pending_name: str) -> list[dict[str, object]]:
        return [
            _check_run(conclusion="FAILURE"),
            {
                "__typename": "StatusContext",
                "context": pending_name,
                "state": "PENDING",
            },
        ]

    first_provider, _ = _provider(
        _primary(statusCheckRollup=mixed_checks("deploy")),
        _threads(),
    )
    second_provider, _ = _provider(
        _primary(statusCheckRollup=mixed_checks("publish")),
        _threads(),
    )

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

    assert first.canonical != second.canonical
    assert first.observation.status is MonitorObservationStatus.ACTIONABLE
    assert second.observation.status is MonitorObservationStatus.ACTIONABLE
    assert first.observation.fingerprint == second.observation.fingerprint


@pytest.mark.parametrize(
    ("merge_state", "mergeability", "status"),
    [
        ("CLEAN", "mergeable", MonitorObservationStatus.SUCCESS),
        ("HAS_HOOKS", "mergeable", MonitorObservationStatus.SUCCESS),
        ("UNSTABLE", "mergeable", MonitorObservationStatus.SUCCESS),
        ("", "pending", MonitorObservationStatus.PENDING),
        ("FUTURE_STATE", "pending", MonitorObservationStatus.PENDING),
    ],
)
def test_mergeability_only_accepts_known_settled_merge_states(
    merge_state: str,
    mergeability: str,
    status: MonitorObservationStatus,
) -> None:
    """An empty or future provider enum cannot fall through to review-ready success."""
    provider, _ = _provider(_primary(mergeStateStatus=merge_state), _threads())

    result = _probe_one(provider)

    assert result.canonical["mergeability"] == mergeability
    assert result.observation.status is status


def test_review_threads_paginate_and_fold_order_independently() -> None:
    """Stopping after one page can falsely report no blocking review threads."""
    first_provider, first_runner = _provider(
        _primary(),
        _threads([{"isResolved": True}], has_next=True, cursor="cursor-1"),
        _threads([{"isResolved": False}], has_next=False),
    )
    second_provider, _ = _provider(
        _primary(),
        _threads([{"isResolved": False}], has_next=True, cursor="cursor-2"),
        _threads([{"isResolved": True}], has_next=False),
    )

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

    assert first.canonical["unresolved_review_threads"] == 1
    assert first.canonical["review_threads_complete"] is True
    assert first.observation.fingerprint == second.observation.fingerprint
    assert len(first_runner.calls) == 5
    assert "c0=cursor-1" in first_runner.calls[3][0]


def test_review_thread_string_variables_use_raw_graphql_fields() -> None:
    """Numeric-looking GitHub names and cursors must remain GraphQL strings."""
    provider, runner = _provider(
        _primary(),
        _threads(has_next=True, cursor="false"),
        _threads(),
    )

    _probe_one(provider, "https://github.com/123/true/pull/123")

    first_page = runner.calls[2][0]
    second_page = runner.calls[3][0]
    assert ["-f", "o0=123"] == first_page[5:7]
    assert ["-f", "r0=true"] == first_page[7:9]
    assert ["-F", "n0=123"] == first_page[9:11]
    assert ["-f", "c0=false"] == second_page[5:7]


def test_review_thread_page_cap_is_pending_instead_of_success() -> None:
    """An eleventh page cannot be silently treated as an empty complete tail."""
    pages = [
        _threads(
            [{"isResolved": True}],
            has_next=True,
            cursor=f"cursor-{page}",
        )
        for page in range(1, 11)
    ]
    provider, runner = _provider(_primary(), *pages)

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_threads_incomplete"
    assert result.observation.supplemental_provider_error is None
    assert len(runner.calls) == 13


def test_review_thread_missing_next_cursor_is_pending_instead_of_success() -> None:
    """A partial pagination envelope is not evidence that the unseen tail is empty."""
    provider, runner = _provider(
        _primary(),
        _threads([{"isResolved": True}], has_next=True, cursor=None),
    )

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_threads_incomplete"
    assert len(runner.calls) == 4


def test_review_thread_graphql_errors_make_partial_data_pending() -> None:
    """GraphQL can return usable-looking data alongside errors; it is still incomplete."""
    partial = _threads([{"isResolved": True}])
    partial["errors"] = [{"message": "provider-controlled detail"}]
    provider, _ = _provider(_primary(), partial)

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_threads_incomplete"
    assert "provider-controlled detail" not in repr(result)


def test_review_thread_graphql_errors_without_usable_data_preserve_primary_facts() -> None:
    """An error-only supplemental envelope cannot erase a valid primary observation."""
    provider, _ = _provider(
        _primary(),
        {"data": None, "errors": [{"message": "provider-controlled detail"}]},
    )

    result = _probe_one(
        provider,
        "https://github.com/owner/repo/pull/123",
        previous_observation={"head_revision": "previous-head"},
    )

    assert result.response is not None
    assert result.canonical["head_revision"] == _HEAD
    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT
    assert result.observation.reason_code == "review_threads_incomplete"
    assert "provider-controlled detail" not in repr(result)


def test_generic_blocked_state_preserves_supplemental_provider_error() -> None:
    """GitHub's generic BLOCKED state is uncertainty, not a known blocker."""
    provider, _ = _provider(
        _primary(mergeStateStatus="BLOCKED"),
        {"data": None, "errors": [{"message": "provider-controlled detail"}]},
    )

    result = _probe_one(
        provider,
        "https://github.com/owner/repo/pull/123",
        previous_observation={"head_revision": "previous-head"},
    )

    assert result.response is not None
    assert result.canonical["mergeability"] == "blocked"
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT
    assert result.observation.reason_code == "review_threads_incomplete"


@pytest.mark.asyncio
async def test_shadow_graphql_error_without_data_persists_new_primary_facts() -> None:
    """Readable primary facts remain durable while supplemental evidence retries."""
    # An empty board keeps the observation free of the named pending signals, so
    # this test isolates the supplemental-error retry path rather than a wake.
    provider, _ = _provider(
        _primary(statusCheckRollup=[]),
        {"data": None, "errors": [{"message": "provider-controlled detail"}]},
    )
    previous = {"head_revision": "previous-head", "safe": "fact"}
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation=deepcopy(previous),
        last_fingerprint="safe-fingerprint",
    )
    snapshots: list[dict[str, object]] = []

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(monitor_state_to_dict(updated)))

    decision = await run_shadow_probe(state, provider, persist, now=1_100.0)

    assert decision.decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_observation["head_revision"] == _HEAD
    assert state.last_observation["review_threads_complete"] is False
    assert state.last_fingerprint != "safe-fingerprint"
    assert state.provider_error_count == 1
    assert state.consecutive_provider_errors == 1
    assert "provider-controlled detail" not in repr(snapshots)


def test_review_thread_graphql_errors_preserve_observed_unresolved_nodes() -> None:
    """Partial GraphQL failure cannot erase a blocker returned in the same payload."""
    partial = _threads([{"isResolved": False}])
    partial["errors"] = [{"message": "partial review evidence"}]
    provider, _ = _provider(_primary(), partial)

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.canonical["unresolved_review_threads"] == 1
    assert result.canonical["blocking_review"] == "unresolved_threads"
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "unresolved_review_threads"


def test_review_thread_request_failure_preserves_primary_failed_check() -> None:
    """A secondary request failure cannot discard a blocker from the primary read."""
    failed_check = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "workflowDefinitionId": 7,
        "workflowRunEvent": "pull_request",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
    }
    core, rollup = _core_and_rollup(statusCheckRollup=[failed_check])
    results = iter(
        (
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(core), stderr=""),
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(rollup), stderr=""),
            subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="provider failure"),
        )
    )
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=lambda *_args, **_kwargs: next(results, _completed(_comments())),
    )

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.canonical["checks"]["failed"] == ["CI / test"]
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "checks_failed"


def test_raised_check_timeout_preserves_primary_review_blocker() -> None:
    """A raised supplemental timeout cannot replace readable primary review facts."""
    steps = iter(
        (
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(
                    _envelope(_pr_node(_primary(reviewDecision="CHANGES_REQUESTED")))
                ),
                stderr="",
            ),
            subprocess.TimeoutExpired(["gh"], 30),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(_threads()),
                stderr="",
            ),
        )
    )

    def runner(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        step = next(steps, _completed(_comments()))
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, subprocess.CompletedProcess)
        return step

    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=runner,
    )

    result = _probe_one(provider)

    assert result.response is not None
    assert result.canonical["checks_complete"] is False
    assert result.canonical["review_threads_complete"] is True
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "changes_requested"
    assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT


def test_raised_review_setup_error_preserves_primary_review_blocker() -> None:
    """A raised supplemental setup failure remains typed without erasing primary facts."""
    core, rollup = _core_and_rollup(reviewDecision="CHANGES_REQUESTED")
    steps = iter(
        (
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(core), stderr=""),
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(rollup), stderr=""),
            SetupError("supplemental audit unavailable"),
        )
    )

    def runner(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        step = next(steps, _completed(_comments()))
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, subprocess.CompletedProcess)
        return step

    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=runner,
    )

    result = _probe_one(provider)

    assert result.response is not None
    assert result.canonical["checks_complete"] is True
    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "changes_requested"
    assert result.observation.supplemental_provider_error is ProviderErrorKind.SETUP


def test_later_review_thread_request_failure_preserves_observed_blocker() -> None:
    """A failed later page cannot erase an unresolved thread already returned."""
    core, rollup = _core_and_rollup()
    results = iter(
        (
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(core), stderr=""),
            subprocess.CompletedProcess(["gh"], 0, stdout=json.dumps(rollup), stderr=""),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(
                    _threads(
                        [{"isResolved": False}],
                        has_next=True,
                        cursor="cursor-1",
                    )
                ),
                stderr="",
            ),
            subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="provider failure"),
        )
    )
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=lambda *_args, **_kwargs: next(results, _completed(_comments())),
    )

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.canonical["unresolved_review_threads"] == 1
    assert result.canonical["blocking_review"] == "unresolved_threads"
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "unresolved_review_threads"


def test_malformed_review_thread_node_cannot_hide_a_later_blocker() -> None:
    """Malformed evidence makes the page incomplete without discarding valid blockers."""
    provider, _ = _provider(
        _primary(),
        _threads([None, {"isResolved": False}]),
    )

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.canonical["unresolved_review_threads"] == 1
    assert result.canonical["blocking_review"] == "unresolved_threads"
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "unresolved_review_threads"


@pytest.mark.parametrize(
    ("primary_changes", "thread_nodes", "reason", "blocking_review"),
    [
        (
            {"reviewDecision": "CHANGES_REQUESTED"},
            [{"isResolved": True}],
            "changes_requested",
            "changes_requested",
        ),
        (
            {},
            [{"isResolved": False}],
            "unresolved_review_threads",
            "unresolved_threads",
        ),
    ],
)
def test_known_review_blocker_precedes_incomplete_thread_evidence(
    primary_changes: dict[str, object],
    thread_nodes: list[dict[str, object]],
    reason: str,
    blocking_review: str,
) -> None:
    """A partial unseen tail cannot mask blocking review evidence already observed."""
    nodes = [*thread_nodes]
    partial = _threads(nodes)
    if blocking_review == "unresolved_threads":
        nodes.append({})
        partial = _threads(nodes)
    else:
        partial["errors"] = [{"message": "partial review evidence"}]
    provider, _ = _provider(_primary(**primary_changes), partial)

    result = _probe_one(provider)

    assert result.canonical["review_threads_complete"] is False
    assert result.canonical["blocking_review"] == blocking_review
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == reason


def test_incomplete_review_fingerprint_distinguishes_known_blockers() -> None:
    """Distinct known review work must not deduplicate behind one unknown fingerprint."""
    changes = _threads([{"isResolved": True}])
    changes["errors"] = [{"message": "partial review evidence"}]
    unresolved = _threads([{"isResolved": False}, {}])
    changes_provider, _ = _provider(
        _primary(reviewDecision="CHANGES_REQUESTED"),
        changes,
    )
    unresolved_provider, _ = _provider(_primary(), unresolved)

    first = _probe_one(changes_provider)
    second = _probe_one(unresolved_provider)

    assert first.observation.fingerprint != second.observation.fingerprint


def test_changed_head_is_explicitly_actionable_even_when_new_facts_are_green() -> None:
    """A green new revision must not terminate before the owner can inspect it."""
    previous_provider, _ = _provider(_primary(), _threads())
    previous = _probe_one(previous_provider)
    new_head = "fedcba9876543210fedcba9876543210fedcba98"
    current_provider, _ = _provider(_primary(headRefOid=new_head), _threads())

    current = _probe_one(
        current_provider,
        "https://github.com/owner/repo/pull/123",
        previous_observation=previous.canonical,
    )
    state = MonitorState(
        kind="github_pull_request",
        target="github.com/owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation=deepcopy(previous.canonical),
        last_fingerprint=previous.observation.fingerprint,
    )

    assert current.observation.head_changed is True
    assert current.observation.status is MonitorObservationStatus.SUCCESS
    assert current.observation.fingerprint != previous.observation.fingerprint
    assert decide_monitor(state, current.observation, now=1_001.0).decision is (
        MonitorDecision.WAKE_ACTIONABLE
    )


def test_missing_current_head_is_pending_without_a_changed_head_wake() -> None:
    """A missing current SHA is provider uncertainty, not evidence of a new revision."""
    provider, _ = _provider(_primary(headRefOid=""), _threads())
    state = MonitorState(
        kind="github_pull_request",
        target="github.com/owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={"head_revision": _HEAD},
        last_fingerprint="previous-fingerprint",
    )

    result = _probe_one(
        provider,
        "https://github.com/owner/repo/pull/123",
        previous_observation=state.last_observation,
    )

    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.head_changed is False
    assert (
        decide_monitor(state, result.observation, now=1_001.0).decision
        is MonitorDecision.RECORD_ONLY
    )


@pytest.mark.parametrize(
    ("provider_state", "expected"),
    [
        ("MERGED", MonitorDecision.STOP_SUCCESS),
        ("CLOSED", MonitorDecision.STOP_BLOCKED),
    ],
)
def test_terminal_pull_request_state_precedes_a_head_revision_change(
    provider_state: str,
    expected: MonitorDecision,
) -> None:
    """A terminal lifecycle cannot reopen merely because its final head is new."""
    provider, _ = _provider(_primary(state=provider_state), _threads())
    state = MonitorState(
        kind="github_pull_request",
        target="github.com/owner/repo#123",
        objective="review_ready",
        created_ts=1_000.0,
        last_observation={"head_revision": "previous-head"},
    )

    result = _probe_one(
        provider,
        "https://github.com/owner/repo/pull/123",
        previous_observation=state.last_observation,
    )

    assert result.observation.head_changed is False
    assert decide_monitor(state, result.observation, now=1_001.0).decision is expected


@pytest.mark.parametrize(
    ("provider_state", "status", "reason"),
    [
        ("MERGED", MonitorObservationStatus.SUCCESS, "pull_request_merged"),
        ("CLOSED", MonitorObservationStatus.BLOCKED, "pull_request_closed"),
    ],
)
def test_terminal_lifecycle_does_not_query_review_threads(
    provider_state: str,
    status: MonitorObservationStatus,
    reason: str,
) -> None:
    """Secondary GraphQL failure cannot override an already-terminal primary lifecycle."""
    calls = 0

    def runner(argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(_envelope(_pr_node(_primary(state=provider_state)))),
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="HTTP 503: secondary query unavailable",
        )

    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = _probe_one(provider)

    assert result.observation.status is status
    assert result.observation.reason_code == reason
    assert calls == 1


def test_boolean_pull_request_number_is_a_malformed_primary_response() -> None:
    """Python boolean equality must not let a non-integer provider number pass validation."""
    provider, runner = _provider(_primary(number=True))

    result = _probe_one(provider, "https://github.com/owner/repo/pull/1")

    assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
    assert result.observation.reason_code == "provider_malformed_response"
    assert len(runner.calls) == 1
