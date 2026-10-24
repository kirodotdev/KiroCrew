"""Provider failure categories, the separate primary and check reads, and the shadow probe.

Pins the fixed, non-leaking category each CLI failure, setup error and transport exception
maps to; a primary read that never selects the check rollup, with the facts the separate
reads derive (an empty rollup, outdated threads, bounded check buckets, a draft); a target
alias refused before URL parsing; and what ``run_shadow_probe`` persists, refuses and stops
on (an actionable result with no wake, a failed write, the budget gate, a terminal subject).
The canonical observation and the check-rollup collapse live in
``test_github_pull_request_monitor.py``, readiness classification in
``test_github_pull_request_monitor_readiness.py``, batched reads and the REST fallback in
``test_github_pull_request_monitor_batched_reads.py`` and the shared fixtures in
``github_pull_request_monitor_helpers.py``.
"""

from __future__ import annotations

import errno
import subprocess
from collections.abc import Sequence
from copy import deepcopy

import pytest
from github_pull_request_monitor_helpers import (
    _HEAD,
    _check_run,
    _comments,
    _completed,
    _CompletedRunner,
    _core_and_rollup,
    _envelope,
    _pr_node,
    _primary,
    _probe_one,
    _provider,
    _rollup_node,
    _threads,
)

from kiro_crew.github_runner import SetupError
from kiro_crew.monitoring.github_pull_request import (
    GitHubPullRequestProvider,
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


def test_probe_isolates_checks_from_the_primary_field_set() -> None:
    """Missing Checks scope must not erase readable lifecycle and review facts.

    The load-bearing primary read must not SELECT the rollup at all. On a shared
    document a Checks permission failure nulls the field it names, so a primary
    read that selected the rollup would hand its own lifecycle and review facts to
    that failure -- which is why the two reads are separate requests.
    """
    core, rollup = _core_and_rollup()
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
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
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
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
    assert len(runner.calls) == 5


def test_more_than_one_hundred_passing_checks_still_yields_a_complete_board() -> None:
    rows = [
        {"__typename": "StatusContext", "context": f"ci-{index}", "state": "SUCCESS"}
        for index in range(112)
    ]
    core, rollup = _core_and_rollup(
        statusCheckRollup=rows,
        reviewDecision="REVIEW_REQUIRED",
    )
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
    provider = GitHubPullRequestProvider(resolver=lambda: "/trusted/bin/gh", runner=runner)

    result = _probe_one(provider)

    assert len(result.canonical["checks"]["passed"]) == 100
    assert result.canonical["checks"]["unknown"] == []
    assert result.canonical["checks_complete"] is True
    assert result.canonical["checks_passed_overflow"] is True
    assert result.observation.status is MonitorObservationStatus.PENDING
    assert result.observation.reason_code == "review_required"
    assert [condition.key for condition in result.observation.conditions] == ["checks_complete"]


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
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
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
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
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
    runner = _CompletedRunner(
        [_completed(core), _completed(rollup), _completed(_threads()), _completed(_comments())]
    )
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
