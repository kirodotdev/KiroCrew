"""Pure decision policy for structured monitors."""

from __future__ import annotations

from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    ProviderErrorKind,
)

_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {ProviderErrorKind.TRANSIENT, ProviderErrorKind.RATE_LIMITED}
)


def decide_monitor(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> MonitorDecision:
    """Return the only controller effect permitted for an observation.

    Budget checks lead because a spent bound must never buy one additional
    unattended turn. Provider failures are classified without a model. For a
    normal observation, only a new actionable fingerprint may wake the owning
    session.
    """
    if state.version != MONITOR_STATE_VERSION:
        return MonitorDecision.STOP_BLOCKED
    terminal = _terminal_decision(state.outcome)
    if terminal is not None:
        return terminal
    if _budget_exhausted(state, state.budgets, now=now):
        return MonitorDecision.STOP_BUDGET
    if observation.status is MonitorObservationStatus.PROVIDER_ERROR:
        return _provider_error_decision(state, observation, state.budgets)
    if observation.status is MonitorObservationStatus.ACTIONABLE:
        if observation.fingerprint == state.last_wake_fingerprint:
            return MonitorDecision.NO_CHANGE
        return MonitorDecision.WAKE_ACTIONABLE
    if observation.fingerprint == state.last_fingerprint:
        return MonitorDecision.NO_CHANGE
    if observation.status is MonitorObservationStatus.PENDING:
        return MonitorDecision.RECORD_ONLY
    if observation.status is MonitorObservationStatus.SUCCESS:
        return MonitorDecision.STOP_SUCCESS
    return MonitorDecision.STOP_BLOCKED


def _terminal_decision(outcome: MonitorOutcome | None) -> MonitorDecision | None:
    if outcome is MonitorOutcome.SUCCESS:
        return MonitorDecision.STOP_SUCCESS
    if outcome is MonitorOutcome.BUDGET:
        return MonitorDecision.STOP_BUDGET
    if outcome is not None:
        return MonitorDecision.STOP_BLOCKED
    return None


def _budget_exhausted(state: MonitorState, budgets: MonitorBudgets, *, now: float) -> bool:
    return (
        now - state.created_ts >= budgets.max_runtime_secs
        or state.agent_turns >= budgets.max_agent_turns
        or state.total_tokens >= budgets.max_tokens
    )


def _provider_error_decision(
    state: MonitorState,
    observation: MonitorObservation,
    budgets: MonitorBudgets,
) -> MonitorDecision:
    error = observation.provider_error
    if error not in _RETRYABLE_PROVIDER_ERRORS:
        return MonitorDecision.STOP_BLOCKED
    if state.consecutive_provider_errors + 1 >= budgets.max_provider_errors:
        return MonitorDecision.STOP_BLOCKED
    return MonitorDecision.RETRY_PROVIDER
