"""GitHub pull-request monitor provider and canonicalization contracts."""

from __future__ import annotations

import errno
import json
import subprocess
from collections.abc import Mapping, Sequence
from copy import deepcopy
from unittest import mock

import pytest

from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring import github_pull_request
from kiro_crew.monitoring.decision import decide_monitor
from kiro_crew.monitoring.github_pull_request import (
    _MAX_SUBJECTS_PER_QUERY,
    GitHubPullRequestProbeResult,
    GitHubPullRequestProvider,
    GitHubPullRequestTarget,
    parse_github_pull_request_target,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_CADENCE_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MONITOR_STOP_VERDICT_STALL,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorProbe,
    MonitorProbeResult,
    MonitorState,
    ProviderErrorKind,
    monitor_state_to_dict,
)
from kiro_crew.monitoring.shadow import ShadowWakeDeliveryRefused, run_shadow_probe

_HEAD = "0123456789abcdef0123456789abcdef01234567"


def _probe_one(
    provider: GitHubPullRequestProvider,
    target: str = "https://github.com/owner/repo/pull/123",
    *,
    previous_observation: object = None,
) -> object:
    """Probe ONE subject across the plural boundary and return its result.

    These tests are about what the GitHub probe derives from a response, not
    about the boundary's arity -- that is covered on its own in
    ``TestPluralProbeBoundary``. Wrapping the one-element call once keeps every
    assertion below reading as it did.
    """
    previous = None if previous_observation is None else {target: previous_observation}
    return provider.probe((target,), previous_observations=previous)[target]


def _primary(**changes: object) -> dict[str, object]:
    """One pull request in the fixtures' own flat vocabulary.

    Flat because a test reads and overrides it far more often than the wire shape
    it becomes: ``_envelope`` and ``_rollup_node`` are the single translators that
    turn this into what GitHub actually answers, so no test hand-writes the
    nesting and no fixture can drift from the selection the provider asks for.
    """
    payload: dict[str, object] = {
        "number": 123,
        "state": "OPEN",
        "isDraft": False,
        "headRefOid": _HEAD,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            _check_run(),
            {"__typename": "StatusContext", "context": "lint", "state": "SUCCESS"},
        ],
    }
    payload.update(changes)
    return payload


def _pr_node(payload: Mapping[str, object]) -> dict[str, object]:
    """The primary read's node: the same fields, minus the rollup it never selects."""
    return {key: value for key, value in payload.items() if key != "statusCheckRollup"}


def _wire_check_row(row: object) -> object:
    """Nest a flat fixture row the way GitHub returns it.

    A ``CheckRun``'s workflow name is reached through its check suite on the wire,
    so the flat ``workflowName`` a test writes is moved there rather than sent as a
    field GitHub never returns. Any other row is passed through untouched, which is
    what keeps the malformed-row tests testing malformed rows.
    """
    if not isinstance(row, dict) or row.get("__typename") != "CheckRun":
        return row
    nested = {key: value for key, value in row.items() if key != "workflowName"}
    if "workflowName" in row:
        nested["checkSuite"] = {"workflowRun": {"workflow": {"name": row["workflowName"]}}}
    return nested


def _rollup_node(
    payload: Mapping[str, object],
    *,
    commit_oid: str | None = None,
    total: int | None = None,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """The supplemental check read's node, carrying the head it describes."""
    rows = payload.get("statusCheckRollup")
    head = payload.get("headRefOid")
    rollup: object = None
    if rows is not None:
        rollup = {
            "contexts": {
                "totalCount": (
                    (len(rows) if isinstance(rows, list) else 0) if total is None else total
                ),
                "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                "nodes": [_wire_check_row(row) for row in rows] if isinstance(rows, list) else rows,
            }
        }
    return {
        "headRefOid": head,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "oid": head if commit_oid is None else commit_oid,
                        "statusCheckRollup": rollup,
                    }
                }
            ]
        },
    }


def _threads_node(
    nodes: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """The supplemental review-thread read's node for one subject."""
    normalized_nodes: list[object] = []
    source_nodes = list(nodes) if nodes is not None else [{"isResolved": True}] * 2
    for node in source_nodes:
        if isinstance(node, dict) and "isOutdated" not in node:
            node = {**node, "isOutdated": False}
        normalized_nodes.append(node)
    return {
        "reviewThreads": {
            "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": normalized_nodes,
        }
    }


def _envelope(*nodes: object, errors: Sequence[object] | None = None) -> dict[str, object]:
    """One batched GraphQL response, one alias per subject in the order asked.

    A ``None`` node is GitHub answering that the subject is not there, which is
    the shape a real partial failure has: the readable aliases stay populated
    beside it.
    """
    payload: dict[str, object] = {
        "data": {
            f"s{index}": (None if node is None else {"pullRequest": node})
            for index, node in enumerate(nodes)
        }
    }
    if errors is not None:
        payload["errors"] = list(errors)
    return payload


def _threads(
    nodes: Sequence[object] | None = None,
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    """One subject's review-thread read, as a whole response."""
    return _envelope(_threads_node(nodes, has_next=has_next, cursor=cursor))


def _alias_error(
    index: int, *, type_name: str = "NOT_FOUND", message: str = ""
) -> dict[str, object]:
    """One reported error naming exactly the subject at *index* in the batch."""
    return {"type": type_name, "path": [f"s{index}", "pullRequest"], "message": message}


def _target_on_another_host(
    target: GitHubPullRequestTarget, host: str = "github.example.com"
) -> GitHubPullRequestTarget:
    """The same subject on a second host, which the target type refuses to build.

    ``GitHubPullRequestTarget`` validates its host on construction and accepts only
    ``github.com``, so a second host cannot be reached through the type or through
    ``parse_github_pull_request_target``. The provider still checks that a chunk
    names one host, and the state that check exists for has to be assembled around
    the validator rather than through it. Every other field stays as the real
    parser produced it.
    """
    twin = deepcopy(target)
    object.__setattr__(twin, "host", host)
    return twin


class _FakeRunner:
    def __init__(self, payloads: Sequence[dict[str, object]]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(self, argv: Sequence[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        payload = self._payloads.pop(0)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")


def _reads(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """The responses one subject's primary and check reads produce, in order.

    A terminal pull request issues neither supplemental request, so it contributes
    the primary response alone -- the same rule the provider enforces.
    """
    responses = [_envelope(_pr_node(payload))]
    if payload.get("state") not in {"MERGED", "CLOSED"}:
        responses.append(_envelope(_rollup_node(payload)))
    return responses


def _batched_reads(*payloads: Mapping[str, object]) -> list[dict[str, object]]:
    """The responses one batched tick reads for several subjects.

    One document per evidence kind, each carrying one alias per subject in the
    order they were asked. A per-subject SEQUENCE of single-alias responses would
    also let a batched probe finish -- every alias past the first reads as absent --
    so a batch test built that way passes while proving nothing.

    Only live subjects appear in the supplemental documents, because only live
    subjects are asked about.
    """
    live = [p for p in payloads if p.get("state") not in {"MERGED", "CLOSED"}]
    responses = [_envelope(*(_pr_node(payload) for payload in payloads))]
    if live:
        responses.append(_envelope(*(_rollup_node(payload) for payload in live)))
        responses.append(_envelope(*(_threads_node() for _ in live)))
    return responses


def _provider(*payloads: dict[str, object]) -> tuple[GitHubPullRequestProvider, _FakeRunner]:
    """Wire a provider to canned responses.

    A payload in the fixtures' flat vocabulary (``_primary()``) is expanded into
    the reads it produces; anything else is a whole response handed over verbatim,
    which is how a test supplies its own review-thread pages or an error envelope.
    A live subject with no supplemental response of its own gets the default one,
    so a test that only cares about primary facts does not have to write it.
    """
    expanded: list[dict[str, object]] = []
    supplemental_supplied = False
    live = False
    for payload in payloads:
        if "state" in payload and "statusCheckRollup" in payload:
            body = deepcopy(payload)
            expanded.extend(_reads(body))
            live = live or body.get("state") not in {"MERGED", "CLOSED"}
        else:
            supplemental_supplied = True
            expanded.append(deepcopy(payload))
    if live and not supplemental_supplied:
        expanded.append(_envelope(_threads_node()))
    runner = _FakeRunner(expanded)
    return (
        GitHubPullRequestProvider(
            resolver=lambda: "/trusted/bin/gh",
            runner=runner,
        ),
        runner,
    )


def test_blank_check_label_keeps_the_provider_state_under_an_opaque_identity() -> None:
    """A missing display label must not turn a successful check into unknown."""
    provider, _runner = _provider(
        _primary(
            statusCheckRollup=[
                {
                    "__typename": "CheckRun",
                    "name": "",
                    "workflowName": "CI",
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

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

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

    first = _probe_one(first_provider)
    second = _probe_one(second_provider)

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

    result = _probe_one(provider)

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
                    "startedAt": "2026-08-23T00:00:00Z",
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


def _check_run(*, status: str = "COMPLETED", conclusion: str = "SUCCESS") -> dict[str, object]:
    """One check run, carrying only the fields the rollup selection asks for."""
    return {
        "__typename": "CheckRun",
        "name": "test",
        "workflowName": "CI",
        "status": status,
        "conclusion": conclusion,
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
    assert len(first_runner.calls) == 4
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
    assert len(runner.calls) == 12


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
    assert len(runner.calls) == 3


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
        runner=lambda *_args, **_kwargs: next(results),
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
        step = next(steps)
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
        step = next(steps)
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
        runner=lambda *_args, **_kwargs: next(results),
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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    assert decision.decision is MonitorDecision.WAKE_ACTIONABLE
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

    assert decision.decision is MonitorDecision.RETRY_PROVIDER
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
        def probe(self, subjects: object, **kwargs: object) -> object:
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
    """The primary and supplemental check responses for one subject, in order."""
    payload = _primary(**changes)
    return _envelope(_pr_node(payload)), _envelope(_rollup_node(payload))


def test_probe_isolates_checks_from_the_primary_field_set() -> None:
    """Missing Checks scope must not erase readable lifecycle and review facts.

    The load-bearing primary read must not SELECT the rollup at all. On a shared
    document a Checks permission failure nulls the field it names, so a primary
    read that selected the rollup would hand its own lifecycle and review facts to
    that failure -- which is why the two reads are separate requests.
    """
    core, rollup = _core_and_rollup()
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = _probe_one(provider)

    assert result.observation.status is MonitorObservationStatus.SUCCESS
    primary_document = runner.calls[0][4]
    checks_document = runner.calls[1][4]
    assert "statusCheckRollup" not in primary_document
    assert "commits(" not in primary_document
    assert "statusCheckRollup" in checks_document
    assert "headRefOid" in checks_document
    assert result.canonical["checks_complete"] is True


def test_null_check_rollup_is_an_empty_complete_check_set() -> None:
    core = _envelope(_pr_node(_primary()))
    rollup = _envelope(_rollup_node(_primary(statusCheckRollup=None)))
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = _probe_one(provider)

    assert result.canonical["checks"] == {
        "failed": [],
        "passed": [],
        "pending": [],
        "unknown": [],
    }
    assert result.canonical["checks_complete"] is True
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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

    assert result.canonical["checks"]["failed"] == ["ci"]
    assert result.canonical["checks"]["passed"] == ["CI / test"]
    assert result.observation.reason_code == "checks_failed"


def test_actionable_fingerprint_changes_when_unresolved_thread_count_changes() -> None:
    first_core, first_rollup = _core_and_rollup()
    second_core, second_rollup = _core_and_rollup()
    first_provider = GitHubPullRequestProvider(
        resolver=lambda: "/trusted/bin/gh",
        runner=_CompletedRunner(
            [
                _completed(first_core),
                _completed(first_rollup),
                _completed(_threads(nodes=[{"isResolved": False, "isOutdated": False}])),
            ]
        ),
    )
    first = _probe_one(first_provider)
    second_provider = GitHubPullRequestProvider(
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
    )
    second = _probe_one(second_provider)

    assert first.observation.fingerprint != second.observation.fingerprint


def test_draft_state_prevents_failed_checks_from_requesting_a_turn() -> None:
    core, rollup = _core_and_rollup(
        isDraft=True,
        statusCheckRollup=[_check_run(conclusion="FAILURE")],
    )
    runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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

    result = _probe_one(provider)

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
        def probe(self, subjects: object, **kwargs: object) -> object:
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

    assert decision.decision is MonitorDecision.STOP_BUDGET
    assert decision.entries == (), "a gate that precedes the probe observed nothing"
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

    assert decision.decision is MonitorDecision.STOP_SUCCESS
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
        def probe(self, subjects: object, **kwargs: object) -> object:
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

    assert decision.decision is MonitorDecision.STOP_SUCCESS
    assert decision.entries == (), "a recorded outcome is replayed, not re-observed"
    assert probes == 0
    assert persists == 0
    assert state.outcome is MonitorOutcome.SUCCESS


class TestBatchedReads:
    """Subjects sharing a host and a credential share each request.

    The spec's rule is that a probe which CAN batch MUST, and GitHub's GraphQL API
    answers for many pull requests in one document. So the cost of a tick is what
    these tests are about: a per-subject loop and a batch are indistinguishable from
    the results alone, and only the request count tells them apart.
    """

    @staticmethod
    def _urls(count: int, *, repo: str = "repo") -> tuple[str, ...]:
        return tuple(
            f"https://github.com/owner/{repo}/pull/{number}" for number in range(1, count + 1)
        )

    def test_one_unreadable_subject_leaves_every_other_verdict_intact(self) -> None:
        """The rule that matters: a partial failure degrades only what it covers.

        GitHub answers a partial failure by populating the readable aliases and
        naming the rest in ``errors[].path`` -- and ``gh`` exits NON-ZERO while
        still writing that payload. A probe that read the exit code as the verdict
        would fail all five subjects for one bad one, which is why this asserts the
        four survivors' facts rather than only the failure.
        """
        urls = self._urls(5)
        readable = [_primary(number=number) for number in (1, 2, 4, 5)]
        primary = _envelope(
            _pr_node(readable[0]),
            _pr_node(readable[1]),
            None,
            _pr_node(readable[2]),
            _pr_node(readable[3]),
            errors=[_alias_error(2)],
        )
        live = _envelope(*(_rollup_node(payload) for payload in readable))
        threads = _envelope(*(_threads_node() for _ in readable))
        runner = _CompletedRunner(
            [
                _completed(primary, returncode=1, stderr="gh: Could not resolve to a PullRequest"),
                _completed(live),
                _completed(threads),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(urls)

        assert set(results) == set(urls)
        unreadable = results[urls[2]]
        assert unreadable.observation.status is MonitorObservationStatus.PROVIDER_ERROR
        assert unreadable.observation.provider_error is ProviderErrorKind.NOT_FOUND
        assert unreadable.observation.reason_code == "provider_not_found"
        assert unreadable.canonical == {}
        for index in (0, 1, 3, 4):
            survivor = results[urls[index]]
            assert survivor.observation.provider_error is None
            assert survivor.observation.status is MonitorObservationStatus.SUCCESS
            assert survivor.observation.reason_code == "review_ready"
            assert survivor.canonical["head_revision"] == _HEAD
            assert survivor.canonical["checks"]["passed"] == ["CI / test", "lint"]
            assert survivor.canonical["checks_complete"] is True
            assert survivor.canonical["review_threads_complete"] is True
        # The unreadable subject is also dropped from the supplemental documents,
        # so it cannot consume an alias the survivors' evidence is read from.
        assert runner.calls[1][4].count("repository(") == 4

    def test_a_tick_costs_three_requests_whatever_the_subject_count_is(self) -> None:
        """Ten subjects were thirty invocations before this; the count is the point."""
        payloads = [_primary(number=number) for number in range(1, 11)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(10))

        assert len(results) == 10
        assert all(result.observation.provider_error is None for result in results.values())
        assert len(runner.calls) == 3
        assert all(call[1] == "api" and call[2] == "graphql" for call in runner.calls)

    def test_every_subject_gets_its_own_alias_and_bound_variables(self) -> None:
        """No part of a subject reaches the document text.

        The names below are chosen so they cannot occur in GraphQL syntax: finding
        either one inside the document would mean a subject was interpolated into
        it rather than bound as a variable.
        """
        payloads = [_primary(number=1), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        provider.probe(
            (
                "https://github.com/owner-alpha/repo-alpha/pull/1",
                "https://github.com/owner-beta/repo-beta/pull/2",
            )
        )

        document = runner.calls[0][4]
        assert "s0:repository(owner:$o0,name:$r0)" in document
        assert "s1:repository(owner:$o1,name:$r1)" in document
        for interpolated in ("owner-alpha", "repo-alpha", "owner-beta", "repo-beta"):
            assert interpolated not in document
        assert runner.calls[0][5:] == [
            "-f",
            "o0=owner-alpha",
            "-f",
            "r0=repo-alpha",
            "-F",
            "n0=1",
            "-f",
            "o1=owner-beta",
            "-f",
            "r1=repo-beta",
            "-F",
            "n1=2",
        ]

    def test_subjects_in_different_repositories_share_one_query(self) -> None:
        """One call is one batch, so two repositories on one host do not split it."""
        payloads = [_primary(number=1), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(
            (
                "https://github.com/owner/first/pull/1",
                "https://github.com/other/second/pull/2",
            )
        )

        assert len(runner.calls) == 3
        assert all(result.observation.provider_error is None for result in results.values())
        assert runner.calls[0][5:11] == ["-f", "o0=owner", "-f", "r0=first", "-F", "n0=1"]
        assert runner.calls[0][11:] == ["-f", "o1=other", "-f", "r1=second", "-F", "n1=2"]

    def test_more_subjects_than_the_document_bound_are_split_not_dropped(self) -> None:
        """A document that grows without a bound is a request that times out."""
        count = _MAX_SUBJECTS_PER_QUERY + 1
        payloads = [_primary(number=number) for number in range(1, count + 1)]
        first_chunk = payloads[:_MAX_SUBJECTS_PER_QUERY]
        second_chunk = payloads[_MAX_SUBJECTS_PER_QUERY:]
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(*first_chunk) + _batched_reads(*second_chunk)
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(count))

        assert len(results) == count
        assert all(result.observation.provider_error is None for result in results.values())
        assert len(runner.calls) == 6
        assert runner.calls[0][4].count("repository(") == _MAX_SUBJECTS_PER_QUERY
        assert runner.calls[3][4].count("repository(") == 1

    def test_an_error_naming_no_subject_is_charged_to_all_of_them(self) -> None:
        """A document-level failure means none of them was read.

        Dropping an unattributable error would report a verdict from a response
        that carried none, so it fails the whole batch rather than silently
        passing.
        """
        primary = _envelope(
            None,
            None,
            errors=[{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}],
        )
        runner = _CompletedRunner([_completed(primary, returncode=1, stderr="gh: rate limit")])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        assert len(results) == 2
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.RATE_LIMITED
            assert result.observation.reason_code == "provider_rate_limited"
        assert len(runner.calls) == 1

    def test_one_document_advances_subjects_on_different_pages(self) -> None:
        """Each subject carries its own cursor, so a batch is not held to one page."""
        payloads = [_primary(number=1), _primary(number=2)]
        first_page = _envelope(
            _threads_node([{"isResolved": False}], has_next=True, cursor="cursor-1"),
            _threads_node([{"isResolved": True}]),
        )
        # Only the subject that advertised another page is asked again.
        second_page = _envelope(_threads_node([{"isResolved": False}]))
        runner = _CompletedRunner(
            [
                _completed(_envelope(*(_pr_node(payload) for payload in payloads))),
                _completed(_envelope(*(_rollup_node(payload) for payload in payloads))),
                _completed(first_page),
                _completed(second_page),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        paginated = results["https://github.com/owner/repo/pull/1"]
        settled = results["https://github.com/owner/repo/pull/2"]
        assert paginated.canonical["unresolved_review_threads"] == 2
        assert paginated.canonical["review_threads_complete"] is True
        assert settled.canonical["unresolved_review_threads"] == 0
        assert settled.canonical["review_threads_complete"] is True
        assert len(runner.calls) == 4
        assert "c0=cursor-1" in runner.calls[3]
        assert runner.calls[3][4].count("repository(") == 1

    def test_a_terminal_subject_is_left_out_of_the_supplemental_documents(self) -> None:
        """A merged subject issues neither supplemental request, batched or not."""
        payloads = [_primary(number=1, state="MERGED"), _primary(number=2)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(self._urls(2))

        merged = results["https://github.com/owner/repo/pull/1"]
        assert merged.observation.status is MonitorObservationStatus.SUCCESS
        assert merged.observation.reason_code == "pull_request_merged"
        assert results["https://github.com/owner/repo/pull/2"].observation.provider_error is None
        assert len(runner.calls) == 3
        assert runner.calls[0][4].count("repository(") == 2
        assert runner.calls[1][4].count("repository(") == 1
        assert runner.calls[2][4].count("repository(") == 1

    def test_an_unparseable_subject_never_reaches_a_query(self) -> None:
        """A subject that is not a pull-request URL cannot be asked about at all."""
        payloads = [_primary(number=1)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        results = provider.probe(
            ("https://github.com/owner/repo/pull/1", "https://example.com/not-a-pull-request")
        )

        assert results["https://example.com/not-a-pull-request"].observation.reason_code == (
            "provider_malformed_response"
        )
        assert results["https://github.com/owner/repo/pull/1"].observation.provider_error is None
        assert runner.calls[0][4].count("repository(") == 1

    def test_a_repeated_subject_is_asked_about_once(self) -> None:
        """The result mapping is keyed by subject, so a duplicate cannot cost a read."""
        payloads = [_primary(number=1)]
        runner = _CompletedRunner([_completed(payload) for payload in _batched_reads(*payloads)])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        url = "https://github.com/owner/repo/pull/1"

        results = provider.probe((url, url))

        assert list(results) == [url]
        assert runner.calls[0][4].count("repository(") == 1

    def test_refused_credentials_audit_the_query_once_not_each_subject(self) -> None:
        """The query that was not allowed to run is what the refusal records."""
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        denials: list[str] = []

        with mock.patch.object(
            github_pull_request,
            "audit_provider_cli_denied",
            side_effect=denials.append,
        ):
            results = provider.probe(self._urls(3), use_owner_credentials=False)

        assert denials == ["gh"]
        assert len(results) == 3
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.AUTHORIZATION
            assert result.observation.reason_code == "provider_authorization"
        assert runner.calls == []

    def test_a_chunk_naming_one_host_reports_it_and_two_hosts_report_nothing(self) -> None:
        """The check that lets a chunk become a query, exercised on both answers."""
        here = parse_github_pull_request_target("https://github.com/owner/repo/pull/1")
        elsewhere = _target_on_another_host(here)

        one = github_pull_request._shared_host(
            (
                github_pull_request._BatchSubject("a", here),
                github_pull_request._BatchSubject("b", here),
            )
        )
        two = github_pull_request._shared_host(
            (
                github_pull_request._BatchSubject("a", here),
                github_pull_request._BatchSubject("b", elsewhere),
            )
        )

        assert one == "github.com"
        assert two is None

    def test_a_chunk_naming_two_hosts_is_refused_rather_than_pinned_to_one(self) -> None:
        """A query carries one token, so it must never span two hosts.

        The refusal is the whole query, because the query is what carries the
        identity -- reading the second host's subject under the first host's token
        is the harm. Nothing can reach this state today, which is why the check
        exists rather than a per-host grouping pass: a check can be exercised, and
        this is where it is.
        """
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        here = parse_github_pull_request_target("https://github.com/owner/repo/pull/1")
        elsewhere = _target_on_another_host(
            parse_github_pull_request_target("https://github.com/owner/repo/pull/2")
        )
        admitted = {"https://github.com/owner/repo/pull/1": here, "elsewhere": elsewhere}

        with mock.patch.object(
            github_pull_request,
            "parse_github_pull_request_target",
            side_effect=lambda raw: admitted[raw],
        ):
            results = provider.probe(tuple(admitted))

        assert runner.calls == []
        assert set(results) == set(admitted)
        for result in results.values():
            assert result.observation.provider_error is ProviderErrorKind.SETUP
            assert result.observation.reason_code == "provider_setup"


class TestRollupDescribesTheHeadItWasAskedAbout:
    """A rollup for another commit is incomplete evidence, never another head's checks.

    One document reports the head field and the commit the rollup hangs off, so a
    disagreement between them is visible here where the previous per-call read could
    only compare the head field across two responses.
    """

    URL = "https://github.com/owner/repo/pull/123"

    def test_a_rollup_hanging_off_another_commit_is_incomplete(self) -> None:
        payload = _primary(statusCheckRollup=[_check_run(conclusion="FAILURE")])
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(payload))),
                _completed(_envelope(_rollup_node(payload, commit_oid="f" * 40))),
                _completed(_threads()),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider, self.URL)

        assert result.response is not None
        assert result.canonical["head_revision"] == _HEAD
        assert result.canonical["checks"]["failed"] == []
        assert result.canonical["checks_complete"] is False
        assert result.observation.status is MonitorObservationStatus.PENDING
        assert result.observation.supplemental_provider_error is ProviderErrorKind.TRANSIENT

    def test_an_unreported_head_is_judged_on_the_head_field_alone(self) -> None:
        """A subject whose head GitHub did not report keeps the earlier comparison.

        With no head to compare against, the rollup's commit says nothing about
        whether the page is current, so it cannot be what makes the evidence
        incomplete.
        """
        payload = _primary(headRefOid="", statusCheckRollup=[_check_run(conclusion="FAILURE")])
        runner = _CompletedRunner(
            [
                _completed(_envelope(_pr_node(payload))),
                _completed(_envelope(_rollup_node(payload, commit_oid="f" * 40))),
                _completed(_threads()),
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider, self.URL)

        assert result.canonical["head_revision"] == ""
        assert result.canonical["checks"]["failed"] == ["CI / test"]
        assert result.canonical["checks_complete"] is True
        assert result.observation.supplemental_provider_error is None


class TestPluralProbeBoundary:
    """The probe boundary's arity and keying, independent of what GitHub derives.

    These are the tests that would have to change if the signature were made
    plural later instead of now, which is why it is plural now.
    """

    def test_several_subjects_yield_one_result_each_keyed_as_passed(self) -> None:
        # The response's own number is validated against the requested target, so
        # each subject needs an alias that answers for ITS pull request.
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(
                    _primary(number=1),
                    _primary(number=2, statusCheckRollup=[_check_run(conclusion="FAILURE")]),
                )
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        first_url = "https://github.com/owner/repo/pull/1"
        second_url = "https://github.com/owner/repo/pull/2"

        results = provider.probe((first_url, second_url))

        assert set(results) == {first_url, second_url}
        # Both are real observations, not one verdict beside an absent alias.
        assert results[first_url].observation.provider_error is None
        assert results[second_url].observation.provider_error is None
        assert results[first_url].canonical["checks"]["failed"] == []
        assert results[second_url].canonical["checks"]["failed"] == ["CI / test"]
        assert (
            results[first_url].observation.fingerprint
            != results[second_url].observation.fingerprint
        )

    def test_the_mapping_is_keyed_by_the_subject_as_passed_not_a_derived_identity(
        self,
    ) -> None:
        """A caller can only look up what it asked for.

        The canonical facts carry GitHub's own normalized identity
        (``github.com/owner/repo#123``), which is NOT the URL the caller handed
        over. Keying by the derived form would make the mapping unreadable to the
        caller that built the request.
        """
        core, rollup = _core_and_rollup()
        runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        url = "https://github.com/owner/repo/pull/123"

        results = provider.probe((url,))

        assert list(results) == [url]
        assert results[url].canonical["target"] != url

    def test_each_subject_sees_only_its_own_previous_observation(self) -> None:
        """A head carried against the wrong subject would fake a changed head."""
        runner = _CompletedRunner(
            [
                _completed(payload)
                for payload in _batched_reads(_primary(number=1), _primary(number=2))
            ]
        )
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)
        changed = "https://github.com/owner/repo/pull/1"
        unchanged = "https://github.com/owner/repo/pull/2"

        results = provider.probe(
            (changed, unchanged),
            previous_observations={changed: {"head_revision": "a-different-head"}},
        )

        assert results[changed].observation.head_changed is True
        assert results[unchanged].observation.head_changed is False

    def test_no_subjects_probes_nothing(self) -> None:
        runner = _CompletedRunner([])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        assert provider.probe(()) == {}
        assert runner.calls == []

    def test_a_kind_that_is_not_github_satisfies_the_same_boundary(self) -> None:
        """The shared boundary names no host, so a second kind can satisfy it.

        This implementation touches no GitHub type and is accepted by the shared
        protocol. A boundary whose return type named one kind's own result would
        reject it whatever it returned, which is what keeps this test honest
        about the abstraction rather than about GitHub.
        """

        class _CalendarProbe:
            def probe(
                self,
                subjects: Sequence[str],
                *,
                previous_observations: object = None,
            ) -> dict[str, MonitorProbeResult]:
                return {
                    subject: MonitorProbeResult(
                        canonical={"kind": "calendar", "target": subject, "slots_free": 0},
                        observation=MonitorObservation(
                            f"calendar-{subject}",
                            MonitorObservationStatus.PENDING,
                            reason_code="calendar_pending",
                        ),
                    )
                    for subject in subjects
                }

        probe: MonitorProbe = _CalendarProbe()
        results = probe.probe(("team-standup",))

        assert set(results) == {"team-standup"}
        assert results["team-standup"].canonical["kind"] == "calendar"
        assert results["team-standup"].observation.status is MonitorObservationStatus.PENDING

    def test_the_github_provider_satisfies_the_shared_boundary(self) -> None:
        probe: MonitorProbe = GitHubPullRequestProvider(
            resolver=lambda: "/trusted/bin/gh",
            runner=_CompletedRunner([]),
        )

        assert probe.probe(()) == {}

    def test_the_github_result_is_an_implementation_of_the_shared_result(self) -> None:
        """So a caller typed to the shared record can hold this kind's result."""
        core, rollup = _core_and_rollup()
        runner = _CompletedRunner([_completed(core), _completed(rollup), _completed(_threads())])
        provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

        result = _probe_one(provider)

        assert isinstance(result, MonitorProbeResult)


@pytest.mark.asyncio
async def test_shadow_fails_closed_on_a_subset_answer_like_the_controller_does() -> None:
    """Both boundary consumers must agree on what an unusable answer means.

    A plural boundary lets a provider answer for a subset of what it was asked.
    Guarding that in one consumer and letting the other raise on the same input
    would make the hazard's meaning depend on which path observed it.
    """
    snapshots: list[MonitorState] = []

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {}

    async def persist(updated: MonitorState) -> None:
        snapshots.append(deepcopy(updated))

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    verdict = await run_shadow_probe(state, Provider(), persist, now=1_100.0)

    assert verdict.decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_provider_error is ProviderErrorKind.TRANSIENT
    assert len(snapshots) == 1


@pytest.mark.asyncio
async def test_shadow_fails_closed_on_an_untyped_result() -> None:
    """An untyped value would fail inside the decision engine, not at the boundary."""

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {subject: "not a probe result" for subject in subjects}  # type: ignore[union-attr]

    async def persist(updated: MonitorState) -> None:
        return None

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
    )

    verdict = await run_shadow_probe(state, Provider(), persist, now=1_100.0)

    assert verdict.decision is MonitorDecision.RETRY_PROVIDER
    assert state.last_provider_error is ProviderErrorKind.TRANSIENT


@pytest.mark.asyncio
async def test_shadow_records_a_stalled_watch_as_a_stall_not_as_the_subject() -> None:
    """The second writer of ``stopped_reason`` needs the same precedence.

    ``run_shadow_probe`` otherwise takes the reason from the observation, so a
    watch retired for repeating itself would persist as ``checks_failed`` on this
    path while the delivering path called it a stall. One rule, both writers.
    """
    red = MonitorProbeResult(
        canonical={"head_revision": "abc123"},
        observation=MonitorObservation(
            "red-1",
            MonitorObservationStatus.ACTIONABLE,
            reason_code="checks_failed",
            summary="One check is failing.",
        ),
    )

    class Provider:
        def probe(self, subjects: object, **kwargs: object) -> dict[str, object]:
            return {subject: red for subject in subjects}  # type: ignore[union-attr]

    async def persist(updated: MonitorState) -> None:
        return None

    state = MonitorState(
        kind="github_pull_request",
        target="https://github.com/owner/repo/pull/123",
        objective="review_ready",
        created_ts=1_000.0,
        # Already alerted and inside the re-alert interval, so every tick decides
        # NO_CHANGE and the verdict never moves.
        last_fingerprint="red-1",
        last_wake_fingerprint="red-1",
        coalesce_alerted={"red-1": 1_000.0},
        budgets=MonitorBudgets(max_runtime_secs=10_000_000),
    )

    for tick in range(DEFAULT_MONITOR_STALL_TICKS):
        verdict = await run_shadow_probe(
            state, Provider(), persist, now=1_100.0 + tick * DEFAULT_MONITOR_CADENCE_SECS
        )

    assert verdict.decision is MonitorDecision.STOP_BLOCKED
    assert state.outcome is MonitorOutcome.BLOCKED
    assert state.stopped_reason == MONITOR_STOP_VERDICT_STALL
    assert state.stopped_reason != "checks_failed"


def test_the_github_result_requires_its_response_explicitly() -> None:
    """No default: a caller that forgets the typed response should not compile past it."""
    with pytest.raises(TypeError):
        GitHubPullRequestProbeResult(  # type: ignore[call-arg]
            canonical={},
            observation=MonitorObservation("fp", MonitorObservationStatus.PENDING),
        )
