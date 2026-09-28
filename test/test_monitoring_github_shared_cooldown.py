"""GitHub monitors wait out the ``github:api`` schedule the dependency
coordinator already holds instead of spending another refused call.

A task that hit GitHub's rate limit parks on one per-scope schedule; a monitor
probe from the same host and token would only be refused again and would push
the reset further out on a secondary limit. So a probe consults the shared
``retry_at`` first and answers ``RATE_LIMITED`` locally while it is ahead of
now. A monitor never joins the schedule (it has no task row).

That local answer is a THIRD tick outcome, and the second half of this suite is
what keeps it separable from the two the observation type carries: it spends none
of the watch's finite provider-error budget, because that budget counts refusals
GitHub gave THIS watch, and it clears no streak either, so a provider that is
genuinely failing still retires the watch on its own count.
"""

from __future__ import annotations

import subprocess
from copy import deepcopy

import pytest

from kiro_crew.monitoring import github_pull_request as pr_mod
from kiro_crew.monitoring import github_workflow_run as wf_mod
from kiro_crew.monitoring.models import (
    MonitorDecision,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    ProviderErrorKind,
)
from kiro_crew.monitoring.shadow import run_shadow_probe
from kiro_crew.taskq import dependency as taskq_dependency
from kiro_crew.taskq.adapters.github import SCOPE_API
from kiro_crew.taskq.dependency import DependencyCoordinator, DependencySignal


@pytest.fixture
def coordinator():
    clock = {"now": 1_000.0}
    coord = DependencyCoordinator(None, clock=lambda: clock["now"])
    taskq_dependency.register_coordinator(coord)
    yield coord, clock
    taskq_dependency.register_coordinator(None)


def _rate_limited(retry_at: float) -> DependencySignal:
    return DependencySignal(
        kind="rate_limited", dependency_scope=SCOPE_API, source="github", retry_at=retry_at
    )


def _runner_that_must_not_run(*_a, **_k):
    raise AssertionError("gh was invoked during a shared cooldown")


def _ok_runner(stdout: str):
    def run(argv, **_k):
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    return run


class TestPullRequestMonitor:
    def test_probe_is_skipped_while_the_scope_cools(self, coordinator, monkeypatch) -> None:
        coord, clock = coordinator
        coord.report("subagent:1", _rate_limited(retry_at=clock["now"] + 600))
        monkeypatch.setattr(pr_mod.time, "time", lambda: clock["now"] + 1)
        provider = pr_mod.GitHubPullRequestProvider(
            resolver=lambda: "gh", runner=_runner_that_must_not_run
        )
        (result,) = provider.probe(["https://github.com/o/r/pull/1"]).values()
        obs = result.observation
        assert obs.status is MonitorObservationStatus.PROVIDER_ERROR
        assert obs.provider_error is ProviderErrorKind.RATE_LIMITED
        assert obs.reason_code == pr_mod.REASON_SHARED_COOLDOWN
        assert "github:api" in obs.summary

    def test_probe_runs_once_the_schedule_is_due(self, coordinator, monkeypatch) -> None:
        coord, clock = coordinator
        coord.report("subagent:1", _rate_limited(retry_at=clock["now"] + 600))
        monkeypatch.setattr(pr_mod.time, "time", lambda: clock["now"] + 601)
        calls: list[list[str]] = []

        def run(argv, **_k):
            calls.append(list(argv))
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 500: boom")

        provider = pr_mod.GitHubPullRequestProvider(resolver=lambda: "gh", runner=run)
        (result,) = provider.probe(["https://github.com/o/r/pull/1"]).values()
        assert calls, "gh must run once the shared cooldown has passed"
        assert result.observation.reason_code != pr_mod.REASON_SHARED_COOLDOWN

    def test_no_coordinator_means_no_cooldown(self, monkeypatch) -> None:
        taskq_dependency.register_coordinator(None)
        assert pr_mod._shared_cooldown(0.0) is None


class TestWorkflowRunMonitor:
    def test_probe_is_skipped_while_the_scope_cools(self, coordinator, monkeypatch) -> None:
        coord, clock = coordinator
        coord.report("subagent:1", _rate_limited(retry_at=clock["now"] + 600))
        monkeypatch.setattr(wf_mod.time, "time", lambda: clock["now"] + 1)
        provider = wf_mod.GitHubWorkflowRunProvider(
            resolver=lambda: "gh", runner=_runner_that_must_not_run
        )
        (result,) = provider.probe(["https://github.com/o/r/actions/runs/5"]).values()
        obs = result.observation
        assert obs.provider_error is ProviderErrorKind.RATE_LIMITED
        assert obs.reason_code == wf_mod.REASON_SHARED_COOLDOWN

    def test_other_scopes_do_not_gate_github(self, coordinator, monkeypatch) -> None:
        coord, clock = coordinator
        coord.report(
            "subagent:2",
            DependencySignal(
                kind="rate_limited",
                dependency_scope="http:example.com",
                source="http",
                retry_at=clock["now"] + 600,
            ),
        )
        monkeypatch.setattr(wf_mod.time, "time", lambda: clock["now"] + 1)
        assert wf_mod._shared_cooldown(clock["now"] + 1) is None


# ── the skip is a tick that observed nothing, not a refusal to charge ────────


def _refusing_runner(stderr: str):
    """A ``gh`` that GitHub itself refused, so the watch is charged for it."""

    def run(argv, **_k):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr=stderr)

    return run


_RATE_LIMIT_STDERR = "HTTP 403: API rate limit exceeded for user ID 1"


def _watch() -> MonitorState:
    """A healthy watch with facts already observed and no errors charged."""
    return MonitorState(
        kind="github_workflow_run",
        target="https://github.com/o/r/actions/runs/5",
        objective="run_complete",
        created_ts=1_000.0,
        cadence_secs=15,
        last_observation={"run_id": 5, "status": "in_progress"},
        last_fingerprint="a-healthy-fingerprint",
    )


async def _tick(state: MonitorState, provider, now: float) -> MonitorDecision:
    async def persist(updated: MonitorState) -> None:
        deepcopy(updated)

    return (await run_shadow_probe(state, provider, persist, now=now)).decision


@pytest.mark.asyncio
async def test_three_cooldown_skips_do_not_retire_a_healthy_watch(coordinator, monkeypatch) -> None:
    """An unrelated task's rate limit must not spend another watch's budget.

    ``max_provider_errors`` defaults low and ``provider_error_count`` is never
    refunded, so charging a skip retires a watch after as many ticks as the budget
    allows -- here with no request having reached GitHub at all.
    """
    coord, clock = coordinator
    coord.report("subagent:someone-elses-work", _rate_limited(retry_at=clock["now"] + 3_600))
    monkeypatch.setattr(wf_mod.time, "time", lambda: clock["now"] + 1)
    provider = wf_mod.GitHubWorkflowRunProvider(
        resolver=lambda: "gh", runner=_runner_that_must_not_run
    )
    state = _watch()
    budget = state.budgets.max_provider_errors

    for tick in range(budget + 1):
        decision = await _tick(state, provider, now=1_001.0 + tick)
        assert decision is MonitorDecision.RETRY_PROVIDER, tick

    assert state.outcome is None and state.stopped_reason == ""
    assert (state.provider_error_count, state.consecutive_provider_errors) == (0, 0)
    assert state.last_observation_reason_code == wf_mod.REASON_SHARED_COOLDOWN
    # The subject's known facts survive a tick that observed nothing.
    assert state.last_observation == {"run_id": 5, "status": "in_progress"}
    assert state.last_fingerprint == "a-healthy-fingerprint"


@pytest.mark.asyncio
async def test_a_skip_neither_charges_nor_clears_a_real_refusal_streak(
    coordinator, monkeypatch
) -> None:
    """The third outcome is separable from both: no charge, and no reset either.

    Resetting would be the opposite defect -- a provider failing on every other
    tick, with a cooldown between, would keep its streak at one for ever and blind
    the watch permanently.
    """
    coord, clock = coordinator
    monkeypatch.setattr(wf_mod.time, "time", lambda: clock["now"] + 1)
    state = _watch()

    refused = wf_mod.GitHubWorkflowRunProvider(
        resolver=lambda: "gh", runner=_refusing_runner(_RATE_LIMIT_STDERR)
    )
    await _tick(state, refused, now=1_001.0)
    assert (state.provider_error_count, state.consecutive_provider_errors) == (1, 1)
    assert state.last_observation_reason_code == "provider_rate_limited"

    coord.report("subagent:someone-elses-work", _rate_limited(retry_at=clock["now"] + 3_600))
    skipping = wf_mod.GitHubWorkflowRunProvider(
        resolver=lambda: "gh", runner=_runner_that_must_not_run
    )
    await _tick(state, skipping, now=1_002.0)

    assert (state.provider_error_count, state.consecutive_provider_errors) == (1, 1)
    assert state.last_provider_error is ProviderErrorKind.RATE_LIMITED


@pytest.mark.asyncio
async def test_a_refused_provider_still_retires_the_watch_on_schedule(monkeypatch) -> None:
    """The guard against the opposite error: a real outage must still end the watch.

    Nothing here is in cooldown, so every tick is a refusal GitHub gave, and the
    budget is what stops the watch rather than a skip that never asked.
    """
    monkeypatch.setattr(wf_mod.time, "time", lambda: 1_001.0)
    provider = wf_mod.GitHubWorkflowRunProvider(
        resolver=lambda: "gh", runner=_refusing_runner(_RATE_LIMIT_STDERR)
    )
    state = _watch()
    budget = state.budgets.max_provider_errors
    decisions = [await _tick(state, provider, now=1_001.0 + tick) for tick in range(budget)]

    assert decisions[-1] is MonitorDecision.STOP_BLOCKED
    assert state.outcome is MonitorOutcome.BLOCKED
    assert state.provider_error_count == budget
    assert state.stopped_reason == "provider_rate_limited"
