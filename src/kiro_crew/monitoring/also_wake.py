"""The other pull requests a structured watch also wakes on (``also_wake_on``).

An extra only ever wakes the session, once per end state (merged, closed, retired
as unreadable). It never ends the watch, never writes the target's observation,
coalescing or stall fields, and never stamps the target's re-alert map or its
one-shot ``last_wake_reason_code``. Its wake rides the target's claim and
completion path keyed on ``last_wake_fingerprint``, with ``also_wake_claim``
naming the extra so a retry renders the same message.

Pure: no IO and no clock, like :mod:`kiro_crew.monitoring.decision`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from kiro_crew.monitoring.github_provider_errors import is_unattempted_probe
from kiro_crew.monitoring.models import (
    ALSO_WAKE_END_REASON_CODES,
    ALSO_WAKE_KINDS,
    ALSO_WAKE_OPEN,
    MAX_MONITOR_ALSO_WAKE_ON,
    MonitorDecision,
    MonitorDispatchResult,
    MonitorObservationStatus,
    MonitorProbeResult,
    MonitorState,
    also_wake_fingerprint,
    fresh_also_wake_entry,
)

ALSO_WAKE_KIND_REFUSAL = (
    "also_wake_on is available only on a github_pull_request watch; nothing was changed"
)

#: Target decisions that leave the watch running with no wake of its own, so an
#: extra's undelivered end state may take this tick's one wake.
_ROOM_FOR_EXTRA_WAKE = frozenset(
    {MonitorDecision.NO_CHANGE, MonitorDecision.RECORD_ONLY, MonitorDecision.RETRY_PROVIDER}
)


def normalize_also_wake_on(kind: str, target: str, entries: Sequence[str]) -> list[str]:
    """Validate *entries* by *kind*'s target rules; drop the target and repeats.

    Raises ``ValueError`` for another kind or an entry that is not a pull request
    of this kind.
    """
    if kind not in ALSO_WAKE_KINDS:
        raise ValueError(ALSO_WAKE_KIND_REFUSAL)
    from kiro_crew.monitoring.targets import normalize_pull_request_target

    normalized: list[str] = []
    for raw in entries:
        extra = normalize_pull_request_target(kind, str(raw), gitlab_hosts=())
        if extra != target and extra not in normalized:
            normalized.append(extra)
    if len(normalized) > MAX_MONITOR_ALSO_WAKE_ON:
        raise ValueError(f"also_wake_on names at most {MAX_MONITOR_ALSO_WAKE_ON} pull requests")
    return normalized


def replace_also_wake_on(state: MonitorState, extras: Sequence[str]) -> bool:
    """Replace the list; a kept entry keeps its record, a new one starts open.

    Returns whether the list changed.
    """
    changed = list(extras) != state.also_wake_on
    state.also_wake_state = {
        extra: state.also_wake_state.get(extra) or fresh_also_wake_entry() for extra in extras
    }
    state.also_wake_on = list(extras)
    return changed


def open_also_wake_targets(state: MonitorState) -> tuple[str, ...]:
    """The extras still read on each tick, in configured order."""
    return tuple(
        extra
        for extra in state.also_wake_on
        if state.also_wake_state.get(extra, {}).get("state") == ALSO_WAKE_OPEN
    )


def apply_also_wake_results(
    state: MonitorState,
    results: Mapping[str, MonitorProbeResult],
    *,
    now: float,
) -> None:
    """Fold one tick's reads of the open extras into their records.

    Only the primary (lifecycle) read counts: an attempted failure adds one to the
    extra's own streak, a good read resets it, an unattempted skip moves nothing,
    and a supplemental failure is ignored. At the watch's ``max_provider_errors``
    the extra is retired as unreadable.
    """
    for extra, result in results.items():
        entry = state.also_wake_state.get(extra)
        if entry is None or entry.get("state") != ALSO_WAKE_OPEN:
            continue
        observation = result.observation
        if observation.status is MonitorObservationStatus.PROVIDER_ERROR:
            if is_unattempted_probe(observation):
                continue
            prior = entry.get("read_errors")
            errors = (prior if isinstance(prior, int) else 0) + 1
            entry["read_errors"] = errors
            if errors >= state.budgets.max_provider_errors:
                entry.update(state="unreadable", at=now)
            continue
        entry["read_errors"] = 0
        lifecycle = result.canonical.get("state")
        if lifecycle in ("merged", "closed"):
            entry.update(state=lifecycle, at=now)


def also_wake_waiting(state: MonitorState) -> bool:
    """Whether an extra is still open or owes an undelivered wake."""
    return any(
        entry.get("state") == ALSO_WAKE_OPEN or not entry.get("wake_claimed")
        for entry in state.also_wake_state.values()
    )


def claim_also_wake(state: MonitorState, decision: MonitorDecision) -> bool:
    """Claim this tick's wake for the first extra owing one, if the target left room.

    Writes the shared claim fields (``last_wake_fingerprint``, ``wake_in_flight``,
    ``wake_delivery``, ``last_decision``) and ``also_wake_claim``; never
    ``last_wake_reason_code`` or the re-alert map.
    """
    if decision not in _ROOM_FOR_EXTRA_WAKE or state.wake_in_flight:
        return False
    for extra in state.also_wake_on:
        entry = state.also_wake_state[extra]
        end_state = str(entry.get("state"))
        if end_state == ALSO_WAKE_OPEN or entry.get("wake_claimed"):
            continue
        entry["wake_claimed"] = True
        state.also_wake_claim = {
            "target": extra,
            "state": end_state,
            "reason_code": ALSO_WAKE_END_REASON_CODES[end_state],
        }
        state.last_wake_fingerprint = also_wake_fingerprint(extra, end_state)
        state.wake_in_flight = True
        state.wake_delivery = None
        state.last_decision = MonitorDecision.WAKE_ACTIONABLE
        return True
    return False


def honoured_also_wake_claim(state: MonitorState) -> dict[str, object] | None:
    """The extra claim the in-flight wake carries, or ``None`` for a target wake."""
    claim = state.also_wake_claim
    if (
        claim
        and state.wake_in_flight
        and also_wake_fingerprint(str(claim.get("target")), str(claim.get("state")))
        == state.last_wake_fingerprint
    ):
        return claim
    return None


def release_undelivered_also_wake(state: MonitorState) -> None:
    """Drop the claim of a wake that is ending; one never dispatched stays owed."""
    claim = state.also_wake_claim
    entry = state.also_wake_state.get(str(claim.get("target"))) if claim else None
    if (
        entry is not None
        and entry.get("state") == claim.get("state")
        and state.wake_delivery is not MonitorDispatchResult.DISPATCHED
    ):
        entry["wake_claimed"] = False
    state.also_wake_claim = {}
