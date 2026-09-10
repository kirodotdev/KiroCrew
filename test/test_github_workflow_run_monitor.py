"""Contract for the GitHub Actions workflow-run monitored kind.

This kind is the acceptance test for the monitoring substrate: a second, genuinely
different subject added as a probe plus a registry entry. These tests pin the
subject-specific behaviour -- the terminal-state mapping and the URL identity --
and drive the kind end to end through the SHARED persistence-only path
(``run_shadow_probe``) to show the shared engine carries it with no change.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from kiro_crew.monitoring.github_workflow_run import (
    GitHubWorkflowRunProvider,
    parse_github_workflow_run_target,
)
from kiro_crew.monitoring.models import (
    MonitorObservationStatus,
    MonitorProbeResult,
    ProviderErrorKind,
)

_URL = "https://github.com/owner/repo/actions/runs/123456"


def _completed(fake_runner_stdout: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=0, stdout=json.dumps(fake_runner_stdout), stderr=""
    )


def _run_json(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "databaseId": 123456,
        "status": "completed",
        "conclusion": "success",
        "headSha": "a" * 40,
        "workflowName": "CI",
        "event": "push",
    }
    payload.update(changes)
    return payload


def _probe_once(fake_runner_stdout: dict[str, object]) -> MonitorProbeResult:
    def runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(fake_runner_stdout)

    provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=runner)
    results = provider.probe((_URL,))
    return results[_URL]


class TestTheRunUrlIsTypedIdentity:
    def test_a_canonical_run_url_parses(self) -> None:
        target = parse_github_workflow_run_target(_URL)
        assert target.owner == "owner"
        assert target.repo == "repo"
        assert target.run_id == 123456
        assert target.repo_slug == "owner/repo"
        assert target.identity == "github.com/owner/repo/actions/runs/123456"

    def test_www_host_is_accepted(self) -> None:
        target = parse_github_workflow_run_target(
            "https://www.github.com/owner/repo/actions/runs/9"
        )
        assert target.run_id == 9

    @pytest.mark.parametrize(
        "bad",
        [
            "https://github.com/owner/repo/pull/5",
            "https://github.com/owner/repo/actions/runs/123456/attempts/2",
            "https://github.com/owner/repo/actions/runs/123456/job/9",
            "https://github.com/owner/repo/actions/runs/0",
            "https://github.com/owner/repo/actions/runs/007",
            "http://github.com/owner/repo/actions/runs/1",
            "https://gitlab.com/owner/repo/actions/runs/1",
            "https://github.com/owner/repo/actions/runs/1?x=1",
            "https://github.com/owner/repo/actions/runs/notanumber",
            "",
        ],
    )
    def test_non_canonical_or_foreign_urls_are_refused(self, bad: str) -> None:
        with pytest.raises(ValueError):
            parse_github_workflow_run_target(bad)


class TestTheTerminalStateMapping:
    """A run's own vocabulary -- success / failure / cancelled / timed_out -- mapped
    to the engine's generic status. This is the mapping the acceptance test pins."""

    @pytest.mark.parametrize(
        ("status", "conclusion", "expected_status", "expected_reason"),
        [
            ("completed", "success", MonitorObservationStatus.SUCCESS, "run_succeeded"),
            ("completed", "neutral", MonitorObservationStatus.SUCCESS, "run_succeeded"),
            ("completed", "skipped", MonitorObservationStatus.SUCCESS, "run_succeeded"),
            ("completed", "stale", MonitorObservationStatus.SUCCESS, "run_succeeded"),
            ("completed", "failure", MonitorObservationStatus.BLOCKED, "run_failed"),
            ("completed", "timed_out", MonitorObservationStatus.BLOCKED, "run_failed"),
            ("completed", "startup_failure", MonitorObservationStatus.BLOCKED, "run_failed"),
            ("completed", "action_required", MonitorObservationStatus.BLOCKED, "run_failed"),
            ("completed", "cancelled", MonitorObservationStatus.BLOCKED, "run_cancelled"),
            (
                "completed",
                "some_new_conclusion",
                MonitorObservationStatus.BLOCKED,
                "run_conclusion_unknown",
            ),
            ("queued", "", MonitorObservationStatus.PENDING, "run_in_progress"),
            ("in_progress", "", MonitorObservationStatus.PENDING, "run_in_progress"),
            ("waiting", "", MonitorObservationStatus.PENDING, "run_in_progress"),
            ("requested", "", MonitorObservationStatus.PENDING, "run_in_progress"),
            ("some_new_status", "", MonitorObservationStatus.PENDING, "run_in_progress"),
        ],
    )
    def test_a_conclusion_maps_to_the_engine_status_and_this_kinds_reason(
        self,
        status: str,
        conclusion: str,
        expected_status: MonitorObservationStatus,
        expected_reason: str,
    ) -> None:
        result = _probe_once(_run_json(status=status, conclusion=conclusion))
        assert result.observation.status is expected_status
        assert result.observation.reason_code == expected_reason

    def test_the_conclusion_is_uppercased_by_gh_and_still_maps(self) -> None:
        result = _probe_once(_run_json(status="COMPLETED", conclusion="SUCCESS"))
        assert result.observation.status is MonitorObservationStatus.SUCCESS

    def test_a_null_conclusion_on_a_running_run_is_pending(self) -> None:
        result = _probe_once(_run_json(status="in_progress", conclusion=None))
        assert result.observation.status is MonitorObservationStatus.PENDING

    def test_the_canonical_names_this_kind_and_carries_no_pull_request_words(self) -> None:
        result = _probe_once(_run_json())
        assert result.canonical["kind"] == "github_workflow_run"
        assert result.canonical["conclusion"] == "success"
        assert result.canonical["status"] == "completed"
        assert result.canonical["workflow_name"] == "CI"
        assert "mergeability" not in result.canonical
        assert "review_decision" not in result.canonical


class TestProviderErrors:
    def test_a_404_is_not_found(self) -> None:
        def runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["gh"], returncode=1, stdout="", stderr="HTTP 404: Not Found"
            )

        provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=runner)
        result = provider.probe((_URL,))[_URL]
        assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR
        assert result.observation.provider_error is ProviderErrorKind.NOT_FOUND

    def test_a_rate_limit_is_retryable(self) -> None:
        def runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(
                args=["gh"], returncode=1, stdout="", stderr="API rate limit exceeded"
            )

        provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=runner)
        result = provider.probe((_URL,))[_URL]
        assert result.observation.provider_error is ProviderErrorKind.RATE_LIMITED

    def test_a_timeout_is_transient(self) -> None:
        def runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise subprocess.TimeoutExpired(cmd="gh", timeout=30.0)

        provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=runner)
        result = provider.probe((_URL,))[_URL]
        assert result.observation.provider_error is ProviderErrorKind.TRANSIENT

    def test_a_malformed_response_is_transient(self) -> None:
        result = _probe_once({"databaseId": 123456, "status": "completed"})  # missing fields
        assert result.observation.provider_error is ProviderErrorKind.TRANSIENT
        assert result.observation.reason_code == "provider_malformed_response"

    def test_a_databaseid_mismatch_is_rejected(self) -> None:
        result = _probe_once(_run_json(databaseId=999))
        assert result.observation.status is MonitorObservationStatus.PROVIDER_ERROR


class TestTheKindIsRegisteredAsData:
    def test_the_registry_knows_the_kind_and_its_objective(self) -> None:
        from kiro_crew.monitoring.registry import (
            GITHUB_WORKFLOW_RUN,
            RUN_COMPLETE,
            kind_supports_objective,
            kind_supports_shadow,
            monitor_kind,
        )

        entry = monitor_kind(GITHUB_WORKFLOW_RUN)
        assert entry is not None
        assert entry.objectives == frozenset({RUN_COMPLETE})
        assert kind_supports_objective(GITHUB_WORKFLOW_RUN, RUN_COMPLETE)
        assert kind_supports_shadow(GITHUB_WORKFLOW_RUN)

    def test_the_objective_is_this_kinds_own_not_review_ready(self) -> None:
        from kiro_crew.monitoring.registry import (
            GITHUB_WORKFLOW_RUN,
            REVIEW_READY,
            RUN_COMPLETE,
            kind_supports_objective,
        )

        assert not kind_supports_objective(GITHUB_WORKFLOW_RUN, REVIEW_READY)
        from kiro_crew.monitoring.registry import GITHUB_PULL_REQUEST

        assert not kind_supports_objective(GITHUB_PULL_REQUEST, RUN_COMPLETE)

    def test_the_kind_is_not_publicly_armable(self) -> None:
        """A workflow run cannot be inferred from a message by URL, so it is not
        namable by a caller. This also keeps the public armable sets pinned, which
        is what lets the existing engine tests pass unchanged."""
        from kiro_crew.monitoring.registry import (
            GITHUB_WORKFLOW_RUN,
            publicly_armable_kinds,
            publicly_armable_objectives,
        )

        assert GITHUB_WORKFLOW_RUN not in publicly_armable_kinds()
        assert "run_complete" not in publicly_armable_objectives()


@pytest.mark.asyncio
async def test_the_shared_shadow_path_drives_the_kind_to_a_terminal_outcome() -> None:
    """End to end through the SHARED engine: a failed run stops the monitor blocked,
    a successful run stops it success -- with the workflow-run provider injected and
    no change to decision.py, models.py, or the probe protocol."""
    from kiro_crew.monitoring.models import (
        MonitorDecision,
        MonitorOutcome,
        MonitorState,
    )
    from kiro_crew.monitoring.registry import GITHUB_WORKFLOW_RUN, RUN_COMPLETE
    from kiro_crew.monitoring.shadow import run_shadow_probe

    persisted: list[MonitorState] = []

    async def persist(updated: MonitorState) -> None:
        persisted.append(updated)

    def failing_runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(_run_json(conclusion="failure"))

    provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=failing_runner)
    state = MonitorState(
        kind=GITHUB_WORKFLOW_RUN,
        target=_URL,
        objective=RUN_COMPLETE,
        created_ts=1_000.0,
    )
    verdict = await run_shadow_probe(state, provider, persist, now=1_100.0)
    assert verdict.decision is MonitorDecision.STOP_BLOCKED
    assert state.outcome is MonitorOutcome.BLOCKED
    assert state.stopped_reason == "run_failed"
    assert persisted and persisted[-1].outcome is MonitorOutcome.BLOCKED

    def succeeding_runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(_run_json(conclusion="success"))

    ok_provider = GitHubWorkflowRunProvider(
        resolver=lambda: "/usr/bin/gh", runner=succeeding_runner
    )
    ok_state = MonitorState(
        kind=GITHUB_WORKFLOW_RUN,
        target=_URL,
        objective=RUN_COMPLETE,
        created_ts=1_000.0,
    )
    ok_verdict = await run_shadow_probe(ok_state, ok_provider, persist, now=1_100.0)
    assert ok_verdict.decision is MonitorDecision.STOP_SUCCESS
    assert ok_state.outcome is MonitorOutcome.SUCCESS
    assert ok_state.stopped_reason == "run_succeeded"


@pytest.mark.asyncio
async def test_a_running_run_records_and_reschedules_without_stopping() -> None:
    from kiro_crew.monitoring.models import MonitorDecision, MonitorState
    from kiro_crew.monitoring.registry import GITHUB_WORKFLOW_RUN, RUN_COMPLETE
    from kiro_crew.monitoring.shadow import run_shadow_probe

    async def persist(updated: MonitorState) -> None:
        return None

    def running_runner(argv: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(_run_json(status="in_progress", conclusion=None))

    provider = GitHubWorkflowRunProvider(resolver=lambda: "/usr/bin/gh", runner=running_runner)
    state = MonitorState(
        kind=GITHUB_WORKFLOW_RUN,
        target=_URL,
        objective=RUN_COMPLETE,
        created_ts=1_000.0,
        cadence_secs=300,
    )
    verdict = await run_shadow_probe(state, provider, persist, now=1_100.0)
    assert verdict.decision is MonitorDecision.RECORD_ONLY
    assert state.outcome is None
    assert state.next_probe_at == 1_400.0
