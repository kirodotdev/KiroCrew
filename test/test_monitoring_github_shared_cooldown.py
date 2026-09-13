"""GitHub monitors wait out the ``github:api`` schedule the dependency
coordinator already holds instead of spending another refused call.

A task that hit GitHub's rate limit parks on one per-scope schedule; a monitor
probe from the same host and token would only be refused again and would push
the reset further out on a secondary limit. So a probe consults the shared
``retry_at`` first and answers ``RATE_LIMITED`` locally while it is ahead of
now. A monitor never joins the schedule (it has no task row).
"""

from __future__ import annotations

import subprocess

import pytest

from kiro_crew.monitoring import github_pull_request as pr_mod
from kiro_crew.monitoring import github_workflow_run as wf_mod
from kiro_crew.monitoring.models import MonitorObservationStatus, ProviderErrorKind
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
