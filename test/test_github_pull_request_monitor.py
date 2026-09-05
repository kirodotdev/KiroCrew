"""GitHub pull-request monitor provider and canonicalization contracts."""

from __future__ import annotations

import errno
import json
import subprocess
from collections.abc import Sequence
from copy import deepcopy

import pytest

from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.github_pull_request import (
    GitHubPullRequestProvider,
    GitHubPullRequestTarget,
    parse_github_pull_request_target,
)
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorDecision,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    ProviderErrorKind,
    monitor_state_to_dict,
)
from kiro_crew.monitoring.shadow import ShadowWakeDeliveryRefused, run_shadow_probe

_HEAD = "0123456789abcdef0123456789abcdef01234567"


def _primary(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "number": 123,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": _HEAD,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "startedAt": "2026-08-22T00:00:00Z",
                "completedAt": "2026-08-22T00:01:00Z",
            },
            {
                "__typename": "StatusContext",
                "context": "lint",
                "state": "SUCCESS",
                "targetUrl": "https://github.com/owner/repo/statuses/sha",
            },
        ],
    }
    payload.update(changes)
    return payload


def _threads(
    nodes: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    normalized_nodes: list[object] = []
    source_nodes = list(nodes) if nodes is not None else [{"isResolved": True}] * 2
    for node in source_nodes:
        if isinstance(node, dict) and "isOutdated" not in node:
            node = {**node, "isOutdated": False}
        normalized_nodes.append(node)
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {
                            "hasNextPage": has_next,
                            "endCursor": cursor,
                        },
                        "nodes": normalized_nodes,
                    }
                }
            }
        }
    }


class _FakeRunner:
    def __init__(self, payloads: Sequence[dict[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        payload = self._payloads.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")


def _provider(*payloads: dict[str, object]) -> tuple[GitHubPullRequestProvider, _FakeRunner]:
    expanded: list[dict[str, object]] = []
    for payload in payloads:
        if payload.get("state") == "OPEN" and "statusCheckRollup" in payload:
            primary = deepcopy(payload)
            expanded.append(primary)
            expanded.append(
                {
                    "headRefOid": primary["headRefOid"],
                    "statusCheckRollup": primary.pop("statusCheckRollup"),
                }
            )
        else:
            expanded.append(payload)
    runner = _FakeRunner(expanded)
    return (
        GitHubPullRequestProvider(
            resolver=lambda: "/trusted/bin/gh",
            runner=runner,
        ),
        runner,
    )


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


def test_clean_pull_request_has_allowlisted_canonical_observation_and_fingerprint() -> None:
    """Adding provider payload fields or omitting a readiness fact breaks persistence."""
    provider, runner = _provider(_primary(), _threads())

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
    assert primary_argv == [
        "/trusted/bin/gh",
        "pr",
        "view",
        "https://github.com/owner/repo/pull/123",
        "--json",
        ("number,state,isDraft,headRefOid,mergeable,mergeStateStatus," "reviewDecision"),
    ]
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
        "startedAt": "2030-01-01T00:00:00Z",
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

    first = first_provider.probe("https://github.com/owner/repo/pull/123")
    second = second_provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

    serialized = json.dumps(result.canonical, sort_keys=True)
    assert token not in serialized
    assert "internal.example.test" not in serialized
    assert "id=secret" not in serialized


def test_whitespace_only_check_degrades_to_unknown_without_failing_the_probe() -> None:
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

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "checks_unknown"
    assert result.canonical["checks"]["unknown"]


def test_same_label_check_runs_remain_independent_without_order_affecting_fingerprint() -> None:
    """Display labels cannot prove that distinct workflow runs supersede each other."""
    older_failure = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "status": "COMPLETED",
        "conclusion": "FAILURE",
        "startedAt": "2026-08-21T00:00:00Z",
        "completedAt": "2026-08-21T00:01:00Z",
        "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/201",
    }
    newer_success = {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "status": "COMPLETED",
        "conclusion": "SUCCESS",
        "startedAt": "2026-08-22T00:00:00Z",
        "completedAt": "2026-08-22T00:01:00Z",
        "detailsUrl": "https://github.com/owner/repo/actions/runs/200/job/202",
    }
    first_provider, _ = _provider(
        _primary(statusCheckRollup=[older_failure, newer_success]),
        _threads(),
    )
    second_provider, _ = _provider(
        _primary(statusCheckRollup=[newer_success, older_failure]),
        _threads(),
    )

    first = first_provider.probe("https://github.com/owner/repo/pull/123")
    second = second_provider.probe("https://github.com/owner/repo/pull/123")

    assert first.canonical["checks"] == {
        "failed": ["CI / test"],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
    }
    assert first.observation.status is MonitorObservationStatus.ACTIONABLE
    assert first.observation.reason_code == "checks_failed"
    assert first.observation.fingerprint == second.observation.fingerprint


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

    first = first_provider.probe("https://github.com/owner/repo/pull/123")
    second = second_provider.probe("https://github.com/owner/repo/pull/123")

    assert first.canonical["checks"]["failed"] == ["CI / test"]
    assert second.canonical["checks"]["failed"] == ["CI / test", "CI / test"]
    assert first.observation.fingerprint != second.observation.fingerprint


def test_distinct_workflow_dispatches_with_same_labels_remain_independent() -> None:
    """A new run id cannot identify which workflow definition produced a check."""
    provider, _ = _provider(
        _primary(
            statusCheckRollup=[
                {
                    **_check_run(conclusion="FAILURE"),
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/201",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "startedAt": "2026-08-23T00:00:00Z",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/200/job/301",
                },
            ]
        ),
        _threads(),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["checks"] == {
        "failed": ["CI / test"],
        "passed": ["CI / test"],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
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
                    "startedAt": "2026-08-23T00:00:00Z",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/200/job/301",
                },
            ]
        ),
        _threads(),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
                    "startedAt": "2026-08-21T00:00:00Z",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/202",
                    "startedAt": "2026-08-22T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
                    "startedAt": "2026-08-23T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
                    "startedAt": "2026-08-21T00:00:00Z",
                },
                {
                    **_check_run(conclusion="SUCCESS"),
                    "workflowName": "",
                    "detailsUrl": "https://github.com/owner/repo/actions/runs/100/job/202",
                    "startedAt": "2026-08-22T00:00:00Z",
                },
            ]
        ),
        _threads(),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["checks"] == {
        "failed": [],
        "passed": ["CI / test"],
        "pending": ["CI / test"],
        "unknown": [],
    }
    assert result.observation.reason_code == "checks_pending"


def _check_run(*, status: str = "COMPLETED", conclusion: str = "SUCCESS") -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "status": status,
        "conclusion": conclusion,
        "startedAt": "2026-08-22T00:00:00Z",
        "completedAt": "2026-08-22T00:01:00Z",
    }


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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    first = first_provider.probe("https://github.com/owner/repo/pull/123")
    second = second_provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    first = first_provider.probe("https://github.com/owner/repo/pull/123")
    second = second_provider.probe("https://github.com/owner/repo/pull/123")

    assert first.canonical["unresolved_review_threads"] == 1
    assert first.canonical["review_threads_complete"] is True
    assert first.observation.fingerprint == second.observation.fingerprint
    assert len(first_runner.calls) == 4
    assert "cursor=cursor-1" in first_runner.calls[3][0]


def test_review_thread_string_variables_use_raw_graphql_fields() -> None:
    """Numeric-looking GitHub names and cursors must remain GraphQL strings."""
    provider, runner = _provider(
        _primary(),
        _threads(has_next=True, cursor="false"),
        _threads(),
    )

    provider.probe("https://github.com/123/true/pull/123")

    first_page = runner.calls[2][0]
    second_page = runner.calls[3][0]
    assert ["-f", "owner=123"] == first_page[5:7]
    assert ["-f", "repo=true"] == first_page[7:9]
    assert ["-F", "number=123"] == first_page[9:11]
    assert ["-f", "cursor=false"] == second_page[-2:]


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

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_threads_incomplete"
    assert result.observation.supplemental_provider_error is None
    assert len(runner.calls) == 12


def test_review_thread_missing_next_cursor_is_pending_instead_of_success() -> None:
    """A partial pagination envelope is not evidence that the unseen tail is empty."""
    provider, runner = _provider(
        _primary(),
        _threads([{"isResolved": True}], has_next=True, cursor=None),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_threads_incomplete"
    assert len(runner.calls) == 3


def test_review_thread_graphql_errors_make_partial_data_pending() -> None:
    """GraphQL can return usable-looking data alongside errors; it is still incomplete."""
    partial = _threads([{"isResolved": True}])
    partial["errors"] = [{"message": "provider-controlled detail"}]
    provider, _ = _provider(_primary(), partial)

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe(
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

    result = provider.probe(
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
    provider, _ = _provider(
        _primary(),
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

    assert decision is MonitorDecision.RETRY_PROVIDER
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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
        "status": "COMPLETED",
        "conclusion": "FAILURE",
    }
    results = iter(
        (
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(_primary(statusCheckRollup=[failed_check])),
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps({"headRefOid": _HEAD, "statusCheckRollup": [failed_check]}),
                stderr="",
            ),
            subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="provider failure"),
        )
    )
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=lambda *_args, **_kwargs: next(results),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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
                stdout=json.dumps(_primary(reviewDecision="CHANGES_REQUESTED")),
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
        step = next(steps)
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, subprocess.CompletedProcess)
        return step

    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=runner,
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.response is not None
    assert result.canonical["checks_complete"] is False
    assert result.canonical["review_threads_complete"] is True
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "changes_requested"
    assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT


def test_raised_review_setup_error_preserves_primary_review_blocker() -> None:
    """A raised supplemental setup failure remains typed without erasing primary facts."""
    primary = _primary(reviewDecision="CHANGES_REQUESTED")
    steps = iter(
        (
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(primary),
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(
                    {
                        "headRefOid": primary["headRefOid"],
                        "statusCheckRollup": primary["statusCheckRollup"],
                    }
                ),
                stderr="",
            ),
            SetupError("supplemental audit unavailable"),
        )
    )

    def runner(argv: Sequence[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        step = next(steps)
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, subprocess.CompletedProcess)
        return step

    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=runner,
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.response is not None
    assert result.canonical["checks_complete"] is True
    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.reason_code == "changes_requested"
    assert result.observation.supplemental_provider_error is ProviderErrorKind.SETUP


def test_later_review_thread_request_failure_preserves_observed_blocker() -> None:
    """A failed later page cannot erase an unresolved thread already returned."""
    results = iter(
        (
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(_primary()),
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["gh"],
                0,
                stdout=json.dumps(
                    {
                        "headRefOid": _HEAD,
                        "statusCheckRollup": _primary()["statusCheckRollup"],
                    }
                ),
                stderr="",
            ),
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
        runner=lambda *_args, **_kwargs: next(results),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    result = provider.probe("https://github.com/owner/repo/pull/123")

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

    first = changes_provider.probe("https://github.com/owner/repo/pull/123")
    second = unresolved_provider.probe("https://github.com/owner/repo/pull/123")

    assert first.observation.fingerprint != second.observation.fingerprint


def test_changed_head_is_explicitly_actionable_even_when_new_facts_are_green() -> None:
    """A green new revision must not terminate before the owner can inspect it."""
    previous_provider, _ = _provider(_primary(), _threads())
    previous = previous_provider.probe("https://github.com/owner/repo/pull/123")
    new_head = "fedcba9876543210fedcba9876543210fedcba98"
    current_provider, _ = _provider(_primary(headRefOid=new_head), _threads())

    current = current_provider.probe(
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
    assert decide_monitor(state, current.observation, now=1_001.0) is (
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

    result = provider.probe(
        "https://github.com/owner/repo/pull/123",
        previous_observation=state.last_observation,
    )

    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.head_changed is False
    assert decide_monitor(state, result.observation, now=1_001.0) is MonitorDecision.RECORD_ONLY


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

    result = provider.probe(
        "https://github.com/owner/repo/pull/123",
        previous_observation=state.last_observation,
    )

    assert result.observation.head_changed is False
    assert decide_monitor(state, result.observation, now=1_001.0) is expected


class _FailureRunner:
    def __init__(
        self,
        *,
        returncode: int = 1,
        stderr: str = "",
        error: BaseException | None = None,
    ) -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.error = error

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if self.error is not None:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, stdout="", stderr=self.stderr)


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
                stdout=json.dumps(_primary(state=provider_state)),
                stderr="",
            )
        return subprocess.CompletedProcess(
            argv,
            1,
            stdout="",
            stderr="HTTP 503: secondary query unavailable",
        )

    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.status is status
    assert result.observation.reason_code == reason
    assert calls == 1


def test_boolean_pull_request_number_is_a_malformed_primary_response() -> None:
    """Python boolean equality must not let a non-integer provider number pass validation."""
    provider, runner = _provider(_primary(number=True))

    result = provider.probe("https://github.com/owner/repo/pull/1")

    assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
    assert result.observation.reason_code == "provider_malformed_response"
    assert len(runner.calls) == 1


@pytest.mark.parametrize(
    ("stderr", "kind", "reason"),
    [
        ("HTTP 401: Bad credentials", ProviderErrorKind.AUTHENTICATION, "provider_authentication"),
        (
            "HTTP 403: secondary rate limit exceeded",
            ProviderErrorKind.RATE_LIMITED,
            "provider_rate_limited",
        ),
        (
            "HTTP 403: resource not accessible",
            ProviderErrorKind.AUTHORIZATION,
            "provider_authorization",
        ),
        ("HTTP 404: Not Found", ProviderErrorKind.NOT_FOUND, "provider_not_found"),
        ("HTTP 429: too many requests", ProviderErrorKind.RATE_LIMITED, "provider_rate_limited"),
        ("HTTP 503: unavailable", ProviderErrorKind.TRANSIENT, "provider_transient"),
        ("dial tcp: connection refused", ProviderErrorKind.TRANSIENT, "provider_transient"),
        ("Could not resolve host: github.com", ProviderErrorKind.TRANSIENT, "provider_transient"),
        (
            "not logged into any GitHub hosts; run gh auth login",
            ProviderErrorKind.AUTHENTICATION,
            "provider_authentication",
        ),
        (
            "Could not resolve to a Repository with the name 'owner/repo'",
            ProviderErrorKind.NOT_FOUND,
            "provider_not_found",
        ),
    ],
)
def test_provider_cli_failures_map_to_fixed_nonleaking_categories(
    stderr: str,
    kind: ProviderErrorKind,
    reason: str,
) -> None:
    """Raw stderr cannot become durable or loggable monitor state."""
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_FailureRunner(stderr=stderr),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.response is None
    assert result.canonical == {}
    assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
    assert result.observation.provider_error is kind
    assert result.observation.reason_code == reason
    assert result.observation.fingerprint == ""
    assert stderr not in repr(result)


@pytest.mark.parametrize(
    ("resolver", "runner", "kind"),
    [
        (
            lambda: (_ for _ in ()).throw(SetupError("untrusted /tmp/gh")),
            _FailureRunner(),
            ProviderErrorKind.SETUP,
        ),
        (
            lambda: "/trusted/bin/gh",
            _FailureRunner(error=subprocess.TimeoutExpired(["gh"], 30)),
            ProviderErrorKind.TRANSIENT,
        ),
        (
            lambda: "/trusted/bin/gh",
            _FailureRunner(error=FileNotFoundError("/private/path/gh")),
            ProviderErrorKind.SETUP,
        ),
        (
            lambda: "/trusted/bin/gh",
            _FailureRunner(error=PermissionError("/private/path/gh")),
            ProviderErrorKind.SETUP,
        ),
    ],
)
def test_provider_setup_and_transport_exceptions_have_typed_categories(
    resolver: object,
    runner: _FailureRunner,
    kind: ProviderErrorKind,
) -> None:
    """Local setup is terminal while network timeouts remain retryable."""
    provider = GitHubPullRequestProvider(resolver=resolver, runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.provider_error is kind
    assert result.observation.reason_code in {"provider_setup", "provider_transient"}
    assert "/tmp/gh" not in repr(result)
    assert "/private/path/gh" not in repr(result)


@pytest.mark.parametrize(
    "payloads",
    [
        ({"number": 123},),
    ],
)
def test_malformed_provider_payload_is_a_retryable_nonleaking_error(
    payloads: tuple[dict[str, object], ...],
) -> None:
    """Partial JSON is provider uncertainty, not evidence of readiness."""
    provider, _ = _provider(*payloads)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.response is None
    assert result.canonical == {}
    assert result.observation.provider_error is ProviderErrorKind.TRANSIENT
    assert result.observation.reason_code == "provider_malformed_response"


def test_secret_bearing_stderr_never_reaches_result_or_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Credentials, paths, URLs, and provider text are discarded after classification."""
    raw = (
        "HTTP 403 token ghp_abcdefghijklmnopqrstuvwxyz123456 "
        "/home/user/private https://internal.example.test/request?id=secret"
    )
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_FailureRunner(stderr=raw),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    combined = repr(result) + caplog.text
    for forbidden in ("ghp_", "/home/user", "internal.example.test", "request?id"):
        assert forbidden not in combined


@pytest.mark.asyncio
async def test_shadow_probe_persists_observation_decision_and_metrics_without_a_wake() -> None:
    """An actionable shadow result records evidence but cannot claim or charge a turn."""
    provider, _ = _provider(
        _primary(statusCheckRollup=[_check_run(conclusion="FAILURE")]),
        _threads(),
    )
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )
    snapshots: list[dict[str, object]] = []

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(monitor_state_to_dict(updated)))

    decision = await run_shadow_probe(state, provider, persist, now=1_100.0)

    assert decision is MonitorDecision.WAKE_ACTIONABLE
    assert state.last_decision is MonitorDecision.WAKE_ACTIONABLE
    assert state.probe_count == 1
    assert state.provider_error_count == 0
    assert state.last_probe_at == 1_100.0
    assert state.last_observed_at == 1_100.0
    assert state.next_probe_at == 1_100.0 + state.cadence_secs
    assert state.last_observation["checks"] == {
        "failed": ["CI / test"],
        "passed": [],
        "pending": [],
        "unknown": [],
    }
    assert state.last_observation_status is MonitorObservationStatus.ACTIONABLE
    assert state.last_observation_reason_code == "checks_failed"
    assert state.last_fingerprint
    assert state.last_wake_fingerprint == ""
    assert state.wake_in_flight is False
    assert state.agent_turns == 0
    assert state.input_tokens == state.output_tokens == 0
    assert len(snapshots) == 1
    assert snapshots[0]["last_decision"] == MonitorDecision.WAKE_ACTIONABLE


@pytest.mark.asyncio
async def test_shadow_provider_error_persists_only_fixed_error_metrics() -> None:
    """A retryable failure advances probe metrics without replacing the last good facts."""
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_FailureRunner(stderr="HTTP 429 token ghp_abcdefghijklmnopqrstuvwxyz123456"),
    )
    previous = {"head_revision": _HEAD, "safe": "fact"}
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

    assert decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_observation == previous
    assert state.last_fingerprint == "safe-fingerprint"
    assert state.last_observation_status is MonitorObservationStatus.PROVIDER_ERROR
    assert state.last_observation_reason_code == "provider_rate_limited"
    assert state.probe_count == 1
    assert state.provider_error_count == 1
    assert state.consecutive_provider_errors == 1
    assert state.last_provider_error is ProviderErrorKind.RATE_LIMITED
    assert state.last_decision is MonitorDecision.RETRY_PROVIDER
    assert "ghp_" not in repr(snapshots)


@pytest.mark.asyncio
async def test_shadow_probe_leaves_live_state_unchanged_when_persistence_fails() -> None:
    """A failed durable write must leave the same observation eligible for retry."""
    provider, _ = _provider(_primary(), _threads())
    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        last_fingerprint="previous-fingerprint",
    )
    before = deepcopy(state)

    async def persist(updated: MonitorState) -> None:
        raise OSError("disk full")

    with pytest.raises(OSError, match="disk full"):
        await run_shadow_probe(state, provider, persist, now=1_100.0)

    assert state == before


@pytest.mark.asyncio
async def test_shadow_mode_refuses_wake_delivery_before_probe_or_persistence() -> None:
    """Turning shadow mode into a dispatcher path must require a later controller change."""
    probes = 0
    persists = 0

    class Provider:
        def probe(self, raw_target: str, **kwargs: object) -> object:
            nonlocal probes
            probes += 1
            raise AssertionError("shadow refusal must happen before the provider boundary")

    async def persist(updated: MonitorState) -> None:
        nonlocal persists
        persists += 1

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    with pytest.raises(ShadowWakeDeliveryRefused, match="shadow mode"):
        await run_shadow_probe(
            state,
            Provider(),
            persist,
            now=1_100.0,
            wake_delivery=True,
        )

    assert probes == 0
    assert persists == 0
    assert state.probe_count == 0
    assert state.last_wake_fingerprint == ""
    assert state.wake_in_flight is False


class _CompletedRunner:
    def __init__(self, results: Sequence[subprocess.CompletedProcess[str]]) -> None:
        self._results = list(results)
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(argv))
        return self._results.pop(0)


def _completed(
    payload: dict[str, object] | None = None,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["gh"],
        returncode,
        stdout=json.dumps(payload) if payload is not None else "",
        stderr=stderr,
    )


def _core_and_rollup(**changes: object) -> tuple[dict[str, object], dict[str, object]]:
    core = _primary(**changes)
    rollup = {
        "headRefOid": core["headRefOid"],
        "statusCheckRollup": core.pop("statusCheckRollup"),
    }
    return core, rollup


def test_probe_isolates_checks_from_the_primary_field_set() -> None:
    """Missing Checks scope must not erase readable lifecycle and review facts."""
    core, rollup = _core_and_rollup()
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert "statusCheckRollup" not in runner.calls[0][-1]
    assert runner.calls[1][-1] == "statusCheckRollup,headRefOid"
    assert result.canonical["checks_complete"] is True


def test_null_check_rollup_is_an_empty_complete_check_set() -> None:
    core, rollup = _core_and_rollup()
    rollup["statusCheckRollup"] = None
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["checks"] == {
        "failed": [],
        "passed": [],
        "pending": [],
        "unknown": [],
    }
    assert result.observation.status is MonitorObservationStatus.SUCCESS


def test_supplemental_check_permission_failure_preserves_primary_facts() -> None:
    core, _rollup = _core_and_rollup()
    runner = _CompletedRunner(
        [
            _completed(core),
            _completed(returncode=1, stderr="HTTP 403: Resource not accessible"),
            _completed(_threads()),
        ]
    )
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.response is not None
    assert result.canonical["review_decision"] == "approved"
    assert result.canonical["checks_complete"] is False
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "checks_incomplete"
    assert result.observation.supplemental_provider_error is ProviderErrorKind.AUTHORIZATION


def test_nonzero_graphql_with_usable_nodes_preserves_the_blocker_and_error() -> None:
    core, rollup = _core_and_rollup()
    partial = _threads(nodes=[{"isResolved": False, "isOutdated": False}])
    partial["errors"] = [{"message": "partial"}]
    runner = _CompletedRunner(
        [
            _completed(core),
            _completed(rollup),
            _completed(partial, returncode=1, stderr="GraphQL: partial response"),
        ]
    )
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["unresolved_review_threads"] == 1
    assert result.canonical["review_threads_complete"] is False
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT


def test_outdated_unresolved_review_thread_is_not_a_current_blocker() -> None:
    core, rollup = _core_and_rollup()
    runner = _CompletedRunner(
        [
            _completed(core),
            _completed(rollup),
            _completed(_threads(nodes=[{"isResolved": False, "isOutdated": True}])),
        ]
    )
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["unresolved_review_threads"] == 0
    assert result.observation.status is MonitorObservationStatus.SUCCESS
    assert "isOutdated" in runner.calls[2][runner.calls[2].index("-f") + 1]


def test_repeated_review_thread_cursor_is_incomplete_instead_of_looping() -> None:
    core, rollup = _core_and_rollup()
    repeated = _threads(has_next=True, cursor="same-cursor")
    runner = _CompletedRunner(
        [
            _completed(core),
            _completed(rollup),
            _completed(repeated),
            _completed(repeated),
        ]
    )
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["review_threads_complete"] is False
    assert result.observation.reason_code == "review_threads_incomplete"
    assert len(runner.calls) == 4


def test_check_identity_and_bucket_sizes_are_bounded_before_persistence() -> None:
    core, rollup = _core_and_rollup(
        statusCheckRollup=[
            {
                **_check_run(conclusion="FAILURE"),
                "name": f"failure-{index}-\n\u202e\u200b" + "x" * 400,
            }
            for index in range(101)
        ]
    )
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    failed = result.canonical["checks"]["failed"]
    assert len(failed) == 100
    assert all(
        len(identity) <= 200
        and "\n" not in identity
        and "\u202e" not in identity
        and "\u200b" not in identity
        for identity in failed
    )
    assert result.canonical["checks_complete"] is False
    assert result.observation.status is MonitorObservationStatus.ACTIONABLE
    assert result.observation.supplemental_provider_error is None


def test_status_context_failure_outranks_duplicate_success_and_stale_is_nonblocking() -> None:
    core, rollup = _core_and_rollup(
        statusCheckRollup=[
            {"__typename": "StatusContext", "context": "ci", "state": "SUCCESS"},
            {"__typename": "StatusContext", "context": "ci", "state": "FAILURE"},
            _check_run(conclusion="STALE"),
        ]
    )
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.canonical["checks"]["failed"] == ["ci"]
    assert result.canonical["checks"]["passed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_actionable_fingerprint_changes_when_unresolved_thread_count_changes() -> None:
    first_core, first_rollup = _core_and_rollup()
    second_core, second_rollup = _core_and_rollup()
    first = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_CompletedRunner(
            [
                _completed(first_core),
                _completed(first_rollup),
                _completed(_threads(nodes=[{"isResolved": False, "isOutdated": False}])),
            ]
        ),
    ).probe("https://github.com/owner/repo/pull/123")
    second = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_CompletedRunner(
            [
                _completed(second_core),
                _completed(second_rollup),
                _completed(
                    _threads(
                        nodes=[
                            {"isResolved": False, "isOutdated": False},
                            {"isResolved": False, "isOutdated": False},
                        ]
                    )
                ),
            ]
        ),
    ).probe("https://github.com/owner/repo/pull/123")

    assert first.observation.fingerprint != second.observation.fingerprint


def test_draft_state_prevents_failed_checks_from_requesting_a_turn() -> None:
    core, rollup = _core_and_rollup(
        isDraft=True,
        statusCheckRollup=[_check_run(conclusion="FAILURE")],
    )
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "pull_request_draft"


@pytest.mark.parametrize(
    "error",
    [
        OSError(errno.EMFILE, "too many open files"),
        BlockingIOError(errno.EAGAIN, "temporarily unavailable"),
        ConnectionResetError(errno.ECONNRESET, "connection reset"),
    ],
)
def test_transient_spawn_os_errors_remain_retryable(error: OSError) -> None:
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_FailureRunner(error=error),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.provider_error is ProviderErrorKind.TRANSIENT
    assert result.observation.reason_code == "provider_transient"


@pytest.mark.parametrize(
    ("stderr", "kind"),
    [
        ("HTTP 503: unavailable for owner/authentication", ProviderErrorKind.TRANSIENT),
        ("HTTP 502 bad gateway for acme/permission", ProviderErrorKind.TRANSIENT),
        (
            "GraphQL: Could not resolve to a PullRequest with the number of 999999.",
            ProviderErrorKind.NOT_FOUND,
        ),
        ("Resource protected by SAML enforcement", ProviderErrorKind.AUTHORIZATION),
    ],
)
def test_cli_error_classification_uses_structured_status_before_provider_text(
    stderr: str,
    kind: ProviderErrorKind,
) -> None:
    provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_FailureRunner(stderr=stderr),
    )

    result = provider.probe("https://github.com/owner/repo/pull/123")

    assert result.observation.provider_error is kind


@pytest.mark.parametrize(
    "raw",
    [
        "https://github.com/owner/repo/pull/12\r\n3",
        "https://github.com/owner/repo/pull/1\n23",
        "https://github.com/owner/repo/pull/123;touch",
        "https://github.com/owner/repo/pull/0123",
        "https://github.com/owner/repo/pull/" + "9" * 400,
    ],
)
def test_target_rejects_noncanonical_aliases_before_url_parsing(raw: str) -> None:
    with pytest.raises(ValueError, match="GitHub pull request"):
        parse_github_pull_request_target(raw)


@pytest.mark.asyncio
async def test_shadow_budget_gate_stops_before_provider_execution() -> None:
    probes = 0
    snapshots: list[MonitorState] = []

    class Provider:
        def probe(self, raw_target: str, **kwargs: object) -> object:
            nonlocal probes
            probes += 1
            raise AssertionError("budget gate must precede provider execution")

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(updated))

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        budgets=MonitorBudgets(max_runtime_secs=100),
    )

    decision = await run_shadow_probe(state, Provider(), persist, now=1_100.0)

    assert decision is MonitorDecision.STOP_BUDGET
    assert probes == 0
    assert len(snapshots) == 1
    assert state.outcome is MonitorOutcome.BUDGET
    assert state.stopped_reason == "runtime_budget"
    assert state.stopped_at == 1_100.0
    assert state.next_probe_at == 0.0


@pytest.mark.asyncio
async def test_shadow_rejects_unrepresentable_time_as_a_validation_error() -> None:
    async def persist(_updated: MonitorState) -> None:
        raise AssertionError("invalid time must fail before persistence")

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    with pytest.raises(ValueError, match="finite non-negative"):
        await run_shadow_probe(
            state,
            object(),
            persist,
            now=10**400,
        )


@pytest.mark.asyncio
async def test_shadow_terminal_probe_persists_terminal_outcome_without_rearming() -> None:
    provider, _ = _provider(_primary(state="MERGED"))
    snapshots: list[MonitorState] = []

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(updated))

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    decision = await run_shadow_probe(state, provider, persist, now=1_100.0)

    assert decision is MonitorDecision.STOP_SUCCESS
    assert state.outcome is MonitorOutcome.SUCCESS
    assert state.stopped_reason == "pull_request_merged"
    assert state.stopped_at == 1_100.0
    assert state.next_probe_at == 0.0
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_shadow_terminal_state_never_probes_again_or_changes_outcome() -> None:
    probes = 0
    persists = 0

    class Provider:
        def probe(self, raw_target: str, **kwargs: object) -> object:
            nonlocal probes
            probes += 1
            raise AssertionError("terminal monitor must not probe")

    async def persist(updated: MonitorState) -> None:
        nonlocal persists
        persists += 1

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        outcome=MonitorOutcome.SUCCESS,
        stopped_reason="review_ready",
        stopped_at=1_050.0,
    )

    decision = await run_shadow_probe(state, Provider(), persist, now=10_000.0)

    assert decision is MonitorDecision.STOP_SUCCESS
    assert probes == 0
    assert persists == 0
    assert state.outcome is MonitorOutcome.SUCCESS
