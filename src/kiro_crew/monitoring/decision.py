"""Pure decision policy for structured monitors."""

from __future__ import annotations

from kiro_crew.monitoring import models

# A skip that made no API call is not evidence about the target, and this module
# is the third place that has to know it: the two counters refuse to charge it
# and the retirement PREDICTION here has to refuse to spend it.
from kiro_crew.monitoring.github_provider_errors import is_unattempted_probe
from kiro_crew.monitoring.models import (
    MONITOR_STATE_VERSION,
    MONITOR_STOP_AGENT_TURN_BUDGET,
    MONITOR_STOP_PROVIDER_ERROR_BUDGET,
    MONITOR_STOP_RUNTIME_BUDGET,
    MONITOR_STOP_TOKEN_BUDGET,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorState,
    MonitorVerdict,
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
) -> MonitorVerdict:
    """Return the only controller effect permitted for an observation.

    The effect is returned inside a :class:`MonitorVerdict` so it arrives with
    the observations it was rendered against. Every path here judges exactly one
    observation, so the verdict names that one; a probe reporting several
    independent conditions fills the same tuple with several entries without
    changing this signature or any caller.

    A structured monitor watches one subject, so a wake-worthy change coalesces
    over TIME rather than across simultaneous signals: successive changes to the
    subject share one window, aged from when the window opened. This updates the
    window fields on *state* as part of deciding, and the caller persists that
    same state, so the window rides on the state object rather than on any file.

    The window's floor and the re-alert interval are the module-level
    ``DEFAULT_MONITOR_*`` constants, read through :mod:`models` so a test can
    patch them. They are not parameters here: both production callers pass the
    defaults, so a per-call surface would vary only under test.
    """
    return MonitorVerdict(
        decision=_decide_effect(state, observation, now=now),
        entries=(observation,),
    )


def _decide_effect(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> MonitorDecision:
    """Select the effect alone.

    Budget checks lead because a spent bound must never buy one additional
    unattended turn. Provider failures are classified without a model. An
    actionable change passes through the coalescing window before it may wake
    the owning session.
    """
    if state.version != MONITOR_STATE_VERSION:
        return MonitorDecision.STOP_BLOCKED
    terminal = terminal_decision_for_outcome(state.outcome)
    if terminal is not None:
        return terminal
    if monitor_budget_reason(state, now=now):
        return MonitorDecision.STOP_BUDGET
    if observation.status is MonitorObservationStatus.PROVIDER_ERROR:
        return _provider_error_decision(state, observation, state.budgets)
    if observation.status is MonitorObservationStatus.ACTIONABLE:
        if _dedup_fingerprint(state, observation, now=now) == state.last_wake_fingerprint:
            if observation.supplemental_provider_error is not None:
                return _supplemental_provider_error_decision(state, state.budgets)
            return MonitorDecision.NO_CHANGE
        return _coalesce_actionable(state, observation, now=now)
    # Any non-actionable outcome settles the subject, so no window stays open.
    _close_window(state, now=now)
    if observation.supplemental_provider_error is not None:
        return _supplemental_provider_error_decision(state, state.budgets)
    if observation.status is MonitorObservationStatus.SUCCESS:
        if observation.head_changed:
            return MonitorDecision.WAKE_ACTIONABLE
        return MonitorDecision.STOP_SUCCESS
    if observation.fingerprint == state.last_fingerprint:
        return MonitorDecision.NO_CHANGE
    if observation.status is MonitorObservationStatus.PENDING:
        return MonitorDecision.RECORD_ONLY
    return MonitorDecision.STOP_BLOCKED


#: Fed to the actionable dedup comparison in place of a fingerprint whose
#: re-alert period has elapsed. It cannot equal any provider fingerprint or the
#: empty string, so the guard declines to suppress and re-assertion falls through
#: to the coalescing path, which re-wakes and restamps the alert time.
_REALERT_ELAPSED = "\x00realert-elapsed"


def _dedup_fingerprint(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> str:
    """The value the actionable dedup guard compares against last_wake_fingerprint.

    Returns the raw fingerprint while its last alert is inside the re-alert
    interval, so an unchanged actionable subject stays suppressed exactly as
    before. Once the interval has elapsed the value differs, so the same guard,
    on its own unchanged rule, stops suppressing and the subject re-asserts. The
    period is measured from the last ALERT, not from when the state was first
    seen: a wake restamps the alert time, so a genuine wake near a period
    boundary does not trip a second wake one interval later.
    """
    fingerprint = observation.fingerprint
    if fingerprint != state.last_wake_fingerprint:
        return fingerprint
    if _realert_ready(state, fingerprint, now=now):
        return _REALERT_ELAPSED
    return fingerprint


def _coalesce_actionable(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> MonitorDecision:
    """Decide one actionable change through the coalescing window.

    The first actionable change wakes immediately and opens the window. A
    structured monitor's probe interval is user-set (15 to 86400 seconds,
    default 300) and typically exceeds the floor, so holding the first change
    for the floor adds a probe interval of latency at the default and groups
    nothing -- two probes are already further apart than the floor. The floor
    earns its keep only when the interval is materially shorter than it, so it
    holds the SUBSEQUENT change: a second, different actionable fingerprint
    arriving while the window is still open waits out the floor, which folds a
    burst of rapid changes into one wake. A change already inside its re-alert
    interval stays masked; a head change opens a fresh window because a new
    commit is a different subject state.
    """
    _prune_alerted(state, now=now)
    fingerprint = observation.fingerprint

    if not _realert_ready(state, fingerprint, now=now):
        return MonitorDecision.NO_CHANGE

    # Past the re-alert mask. A fingerprint that already owns the open window is
    # an unresolved change re-asserting on its interval: re-wake so it is
    # re-reported rather than told once. The caller stamps the alert time.
    if state.coalesce_fingerprint == fingerprint and not observation.head_changed:
        state.coalesce_opened_at = now
        return MonitorDecision.WAKE_ACTIONABLE

    window_open = bool(state.coalesce_fingerprint) and not observation.head_changed
    if not window_open:
        # First actionable change (or the first after a head change): wake now
        # and open the window so a rapid follow-up change is what the floor holds.
        state.coalesce_fingerprint = fingerprint
        state.coalesce_opened_at = now
        return MonitorDecision.WAKE_ACTIONABLE

    age = now - state.coalesce_opened_at
    # A follow-up change wakes once the window has aged past the floor; before
    # that it is recorded and held, folding a burst into one wake.
    if age >= models.DEFAULT_MONITOR_COALESCE_SECS:
        state.coalesce_fingerprint = fingerprint
        state.coalesce_opened_at = now
        return MonitorDecision.WAKE_ACTIONABLE
    # A follow-up change still inside the floor: record it and keep waiting.
    return MonitorDecision.RECORD_ONLY


def _realert_ready(state: MonitorState, fingerprint: str, *, now: float) -> bool:
    """Whether *fingerprint* may wake again, given the re-alert interval.

    A future timestamp reads as stale rather than as permanent suppression, so a
    clock rollback cannot silence the subject forever.
    """
    last = state.coalesce_alerted.get(fingerprint)
    if not isinstance(last, (int, float)):
        return True
    elapsed = now - float(last)
    return not (0 <= elapsed < models.DEFAULT_MONITOR_REALERT_SECS)


def _close_window(state: MonitorState, *, now: float) -> None:
    """Close any open window and prune the re-alert map.

    The prune is unconditional because the re-alert map lives on a durable
    per-loop record: an entry past its interval suppresses nothing, so dropping
    it frees state that otherwise grows across every restart.
    """
    state.coalesce_fingerprint = ""
    state.coalesce_opened_at = 0.0
    _prune_alerted(state, now=now)


def _prune_alerted(state: MonitorState, *, now: float) -> None:
    """Drop re-alert entries older than the interval, or with an unusable time."""
    realert_secs = models.DEFAULT_MONITOR_REALERT_SECS
    stale = [
        fingerprint
        for fingerprint, last in state.coalesce_alerted.items()
        if not isinstance(last, (int, float)) or now - float(last) >= realert_secs
    ]
    for fingerprint in stale:
        state.coalesce_alerted.pop(fingerprint, None)


def terminal_decision_for_outcome(outcome: MonitorOutcome | None) -> MonitorDecision | None:
    """Return the decision a recorded terminal outcome forces, or None if live.

    Both :func:`decide_monitor` and the persistence-only shadow path
    short-circuit here so a stopped monitor is never re-probed. This is NOT the
    verdict the delivery controller reports: ``autonudge``'s
    ``apply_monitor_probe`` refuses a monitor with a recorded outcome before
    :func:`decide_monitor` runs, flattening every terminal outcome to
    ``STOP_BLOCKED``.
    """
    if outcome is MonitorOutcome.SUCCESS:
        return MonitorDecision.STOP_SUCCESS
    if outcome is MonitorOutcome.BUDGET:
        return MonitorDecision.STOP_BUDGET
    if outcome is not None:
        return MonitorDecision.STOP_BLOCKED
    return None


def monitor_budget_reason(state: MonitorState, *, now: float) -> str:
    """Return the first exhausted hard bound in stable policy order."""
    budgets = state.budgets
    if now - state.created_ts >= budgets.max_runtime_secs:
        return MONITOR_STOP_RUNTIME_BUDGET
    if state.agent_turns >= budgets.max_agent_turns:
        return MONITOR_STOP_AGENT_TURN_BUDGET
    if state.total_tokens >= budgets.max_tokens:
        return MONITOR_STOP_TOKEN_BUDGET
    if state.provider_error_count >= budgets.max_provider_errors:
        return MONITOR_STOP_PROVIDER_ERROR_BUDGET
    return ""


def _provider_error_decision(
    state: MonitorState,
    observation: MonitorObservation,
    budgets: MonitorBudgets,
) -> MonitorDecision:
    error = observation.provider_error
    if error not in _RETRYABLE_PROVIDER_ERRORS:
        return MonitorDecision.STOP_BLOCKED
    if is_unattempted_probe(observation):
        # The third outcome again, one layer up. Both counting sites already
        # refuse to charge a shared-cooldown skip (``shadow.apply_monitor_probe``
        # and the production site in ``autonudge``), and the ``+ 1`` below is the
        # SAME budget spoken about in the future tense -- so a skip reaching it
        # retires the watch on an error nobody will ever count. A watch two real
        # errors into a budget of three is then retired by an unrelated scope's
        # cooldown, having made no API call of its own.
        #
        # The already-spent half stays above in ``monitor_budget_reason``: a
        # budget the watch really has exhausted still stops it, and only the
        # prediction is corrected here.
        return MonitorDecision.RETRY_PROVIDER
    if state.consecutive_provider_errors + 1 >= budgets.max_provider_errors:
        return MonitorDecision.STOP_BLOCKED
    return MonitorDecision.RETRY_PROVIDER


def _supplemental_provider_error_decision(
    state: MonitorState,
    budgets: MonitorBudgets,
) -> MonitorDecision:
    """Retry incomplete secondary evidence before retiring the readable target."""
    if state.consecutive_provider_errors + 1 >= budgets.max_provider_errors:
        return MonitorDecision.STOP_BLOCKED
    return MonitorDecision.RETRY_PROVIDER
