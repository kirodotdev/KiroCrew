"""Fakes and builders the monitor controller test files share.

``test_monitor_controller.py`` and its ``_claim_settlement``, ``_answers_and_envelope``
and ``_realert_stall`` siblings import what they use from here: ``_Provider`` answers
every subject with one fixed probe result and records the previous observation and
owner-credential permission each probe was given; ``_result`` builds a typed GitHub
pull-request probe result for a status; and ``_armed`` arms a ``github_pull_request``
monitor on a fresh ``AutoNudgeService`` and wires a ``MonitorController`` to a
``_Provider`` that returns the given result.
"""

from __future__ import annotations

from copy import deepcopy

from kiro_crew.autonudge import AutoNudgeService
from kiro_crew.monitoring.controller import MonitorController
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorBudgets,
    MonitorObservation,
    MonitorObservationStatus,
    ProviderErrorKind,
)


class _Provider:
    def __init__(self, result: GitHubPullRequestProbeResult) -> None:
        self.result = result
        self.previous: list[dict[str, object]] = []
        self.owner_credential_permissions: list[bool] = []

    def probe(
        self,
        subjects,
        *,
        previous_observations=None,
        use_owner_credentials: bool = True,
    ):
        previous = previous_observations or {}
        for subject in subjects:
            self.previous.append(deepcopy(previous.get(subject) or {}))
            self.owner_credential_permissions.append(use_owner_credentials)
        return {subject: self.result for subject in subjects}


def _result(
    status: MonitorObservationStatus,
    fingerprint: str = "fp-1",
    *,
    supplemental_error: ProviderErrorKind | None = None,
):
    canonical = {
        "kind": "github_pull_request",
        "target": "github.com/acme/widgets#7",
        "state": "open",
        "draft": False,
        "head_revision": "abc123",
        "mergeability": "mergeable",
        "review_decision": "approved",
        "blocking_review": "none",
        "unresolved_review_threads": 0,
        "review_threads_complete": True,
        "checks": {"failed": [], "passed": ["ci"], "pending": [], "unknown": []},
        "checks_complete": True,
    }
    error = (
        ProviderErrorKind.TRANSIENT if status is MonitorObservationStatus.PROVIDER_ERROR else None
    )
    return GitHubPullRequestProbeResult(
        response=None,
        canonical={} if error else canonical,
        observation=MonitorObservation(
            "" if error else fingerprint,
            status,
            provider_error=error,
            supplemental_provider_error=supplemental_error,
            reason_code="provider_transient" if error else "review_ready",
            summary="Provider retry scheduled." if error else "Pull request is ready.",
        ),
    )


async def _armed(tmp_path, *, result, dispatch, clock=lambda: 120.0):
    service = AutoNudgeService(base_dir=tmp_path)
    loop = await service.add_monitor(
        slot_key="chat-1",
        kind="github_pull_request",
        target="https://github.com/acme/widgets/pull/7",
        objective="review_ready",
        cadence_secs=60,
        budgets=MonitorBudgets(max_runtime_secs=600),
        wake_instructions="Check the failed gate.",
        now=100.0,
    )
    controller = MonitorController(
        service,
        dispatch,
        providers={"github_pull_request": _Provider(result)},
        clock=clock,
    )
    return service, loop, controller
