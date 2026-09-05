"""Persistence-only monitor probing with no action-delivery dependency."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import fields
from typing import Protocol

from kiro_crew.monitoring.decision import decide_monitor, monitor_budget_reason
from kiro_crew.monitoring.github_pull_request import GitHubPullRequestProbeResult
from kiro_crew.monitoring.models import (
    MonitorDecision,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    ProviderErrorKind,
)

ShadowStatePersistence = Callable[[MonitorState], Awaitable[None]]


class ShadowWakeDeliveryRefused(RuntimeError):
    """Raised when a caller asks the persistence-only path to wake a session."""


class GitHubShadowProvider(Protocol):
    """External probe boundary required by the shadow controller."""

    def probe(
        self,
        raw_target: str,
        *,
        previous_observation: Mapping[str, object] | None = None,
    ) -> GitHubPullRequestProbeResult: ...


async def run_shadow_probe(
    state: MonitorState,
    provider: GitHubShadowProvider,
    persist: ShadowStatePersistence,
    *,
    now: float,
    wake_delivery: bool = False,
) -> MonitorDecision:
    """Probe and persist one decision without acquiring a delivery capability."""
    if wake_delivery:
        raise ShadowWakeDeliveryRefused("wake delivery is unavailable in shadow mode")
    if state.kind != "github_pull_request" or state.objective != "review_ready":
        raise ValueError("shadow mode supports only github_pull_request review_ready")
    if isinstance(now, bool) or not isinstance(now, (int, float)) or now < 0:
        raise ValueError("now must be a finite non-negative number")
    try:
        now_is_finite = math.isfinite(now)
    except OverflowError as exc:
        raise ValueError("now must be a finite non-negative number") from exc
    if not now_is_finite:
        raise ValueError("now must be a finite non-negative number")
    if not callable(persist):
        raise ValueError("persist must be callable")

    terminal = _decision_for_outcome(state.outcome)
    if terminal is not None:
        return terminal
    budget_reason = monitor_budget_reason(state, now=now)
    if budget_reason:
        staged = deepcopy(state)
        staged.last_decision = MonitorDecision.STOP_BUDGET
        staged.outcome = MonitorOutcome.BUDGET
        staged.stopped_reason = budget_reason
        staged.stopped_at = now
        staged.next_probe_at = 0.0
        await _persist_and_publish(state, staged, persist)
        return MonitorDecision.STOP_BUDGET

    result = await asyncio.to_thread(
        provider.probe,
        state.target,
        previous_observation=deepcopy(state.last_observation),
    )
    staged = deepcopy(state)
    decision = decide_monitor(staged, result.observation, now=now)
    staged.probe_count += 1
    staged.last_probe_at = now
    staged.last_decision = decision
    observation = result.observation
    staged.last_observation_status = observation.status
    staged.last_observation_reason_code = observation.reason_code
    provider_error = observation.provider_error or observation.supplemental_provider_error
    if provider_error is not None:
        staged.provider_error_count += 1
        staged.consecutive_provider_errors += 1
        staged.last_provider_error = provider_error
    else:
        staged.consecutive_provider_errors = 0
        staged.last_provider_error = None
    if observation.status is not MonitorObservationStatus.PROVIDER_ERROR:
        staged.last_observation = deepcopy(result.canonical)
        staged.last_fingerprint = observation.fingerprint
        staged.last_observed_at = now
    if decision in {
        MonitorDecision.STOP_SUCCESS,
        MonitorDecision.STOP_BLOCKED,
        MonitorDecision.STOP_BUDGET,
    }:
        staged.outcome = _terminal_outcome(decision, observation.provider_error)
        staged.stopped_reason = observation.reason_code or decision.value
        staged.stopped_at = now
        staged.next_probe_at = 0.0
    else:
        staged.next_probe_at = now + staged.cadence_secs
    await _persist_and_publish(state, staged, persist)
    return decision


async def _persist_and_publish(
    state: MonitorState,
    staged: MonitorState,
    persist: ShadowStatePersistence,
) -> None:
    """Publish only after the staged replacement is durable."""
    await persist(staged)
    for state_field in fields(MonitorState):
        setattr(state, state_field.name, deepcopy(getattr(staged, state_field.name)))


def _terminal_outcome(
    decision: MonitorDecision,
    provider_error: ProviderErrorKind | None,
) -> MonitorOutcome:
    if decision is MonitorDecision.STOP_SUCCESS:
        return MonitorOutcome.SUCCESS
    if decision is MonitorDecision.STOP_BUDGET:
        return MonitorOutcome.BUDGET
    if provider_error is ProviderErrorKind.NOT_FOUND:
        return MonitorOutcome.TARGET_UNAVAILABLE
    return MonitorOutcome.BLOCKED


def _decision_for_outcome(outcome: MonitorOutcome | None) -> MonitorDecision | None:
    if outcome is MonitorOutcome.SUCCESS:
        return MonitorDecision.STOP_SUCCESS
    if outcome is MonitorOutcome.BUDGET:
        return MonitorDecision.STOP_BUDGET
    if outcome is not None:
        return MonitorDecision.STOP_BLOCKED
    return None
