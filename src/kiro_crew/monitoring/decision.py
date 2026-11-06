"""Pure decision policy for structured monitors."""

from __future__ import annotations

import hashlib
import json

from kiro_crew.metrics import monitor_loops as monitor_loop_metrics
from kiro_crew.monitoring import models

# A skip that made no API call is not evidence about the target: the counters
# refuse to charge it, and the retirement PREDICTION here must refuse to spend it.
from kiro_crew.monitoring.github_provider_errors import is_unattempted_probe
from kiro_crew.monitoring.models import (
    MONITOR_REVISION_KEY_SPACE,
    MONITOR_STATE_VERSION,
    MONITOR_STOP_AGENT_TURN_BUDGET,
    MONITOR_STOP_PROVIDER_ERROR_BUDGET,
    MONITOR_STOP_RUNTIME_BUDGET,
    MONITOR_STOP_TOKEN_BUDGET,
    MONITOR_STOP_VERDICT_STALL,
    MonitorBudgets,
    MonitorDecision,
    MonitorObservation,
    MonitorObservationStatus,
    MonitorOutcome,
    MonitorSeverity,
    MonitorState,
    MonitorVerdict,
    ProviderErrorKind,
    monitor_condition_dedupe_key,
    monitor_dedupe_key_resets_on_revision,
)

_RETRYABLE_PROVIDER_ERRORS = frozenset(
    {ProviderErrorKind.TRANSIENT, ProviderErrorKind.RATE_LIMITED}
)

#: The one decision a stall streak counts: the subject settled and the engine did
#: NOTHING about it, the only shape "the watch stopped learning" can take.
#:
#: Every other decision zeroes the streak. ``RECORD_ONLY`` and ``RETRY_PROVIDER``
#: are deliberate deferrals (a change held inside the coalescing floor, or
#: supplemental evidence waiting on a retry); at a 15s cadence a held change emits
#: twelve identical ``RECORD_ONLY`` ticks 180s into a 240s window, so counting them
#: would retire the watch before the window releases its wake. Zeroing on the
#: ``STOP_*`` decisions keeps a merge or a close from carrying a stall's reason.
_STALL_COUNTED_DECISION = MonitorDecision.NO_CHANGE

#: Observation statuses that CONCLUDE something about the subject, so a tick can
#: count toward a stall. PENDING has not concluded, and PROVIDER_ERROR is not
#: evidence about the subject at all.
_SETTLED_STATUSES = frozenset(
    {
        MonitorObservationStatus.ACTIONABLE,
        MonitorObservationStatus.SUCCESS,
        MonitorObservationStatus.BLOCKED,
    }
)


def decide_monitor(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> MonitorVerdict:
    """Return the only controller effect permitted for an observation.

    The effect is returned inside a :class:`MonitorVerdict` with the
    observations it was rendered against; a probe reporting several conditions
    fills the same tuple without changing this signature.

    A wake-worthy change coalesces over TIME: successive changes to the one
    subject share one window, aged from when it opened. The window fields are
    updated on *state*, which the caller persists. The floor and re-alert
    interval are the ``DEFAULT_MONITOR_*`` constants, read through :mod:`models`
    so a test can patch them.

    The last step counts consecutive ticks that settled the subject and did
    nothing, and stops the watch once they clear both the tick ceiling and the
    wall-clock floor. It runs after the effect is chosen because it digests
    the verdict.
    """
    entries = (observation,)
    decision = _fold_stall_streak(
        state,
        _decide_effect(state, observation, now=now),
        entries,
        now=now,
    )
    # Counted after the fold, so the metric is the verdict the caller acts on.
    # Only the decision value and the kind travel, never the target or a
    # fingerprint. The emit is an in-memory add that swallows its own failures,
    # so this policy stays pure.
    monitor_loop_metrics.record_monitor_decision(decision, state.kind)
    return MonitorVerdict(decision=decision, entries=entries)


def _verdict_digest(
    decision: MonitorDecision,
    entries: tuple[MonitorObservation, ...],
) -> str:
    """Derive one comparable digest for a whole verdict.

    Derived on every tick, never persisted beside what it summarizes;
    ``MonitorState`` keeps only the digest of an EARLIER verdict.

    The decision is hashed with the entries, so any progress (a new fingerprint,
    a moved head, a different status or reason, a wake, a record, a retry)
    changes the digest. ``summary`` is left out: it is prose rendered from facts
    the fingerprint already hashes, and a clock or countdown in it would change
    the digest every tick, so a stall could never trigger.
    """
    payload = {
        "decision": decision.value,
        "entries": [
            {
                "fingerprint": entry.fingerprint,
                "head_changed": entry.head_changed,
                "provider_error": (
                    entry.provider_error.value if entry.provider_error is not None else None
                ),
                "reason_code": entry.reason_code,
                "status": entry.status.value,
                "supplemental_provider_error": (
                    entry.supplemental_provider_error.value
                    if entry.supplemental_provider_error is not None
                    else None
                ),
            }
            for entry in entries
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _verdict_is_settled(entries: tuple[MonitorObservation, ...]) -> bool:
    """Whether this tick concluded anything about the subject.

    ``PENDING`` is the not-concluded class (``classify_pull_request_facts``
    returns it for ``checks_incomplete``, ``checks_pending``,
    ``review_threads_incomplete`` and other in-flight reasons), so counting it
    would retire a watch for being early. A subject parked in PENDING forever,
    such as a permanent draft, is retired by the runtime budget instead.
    ``PROVIDER_ERROR`` is no evidence about the subject and has its own budget.
    A verdict with NO entries never ran a probe, so it concludes nothing.
    """
    return bool(entries) and all(entry.status in _SETTLED_STATUSES for entry in entries)


def _stall_tripped(state: MonitorState, *, now: float) -> bool:
    """Whether the open streak has both reached the count and covered the floor.

    The single place the stall condition is evaluated: :func:`_fold_stall_streak`
    decides the stop with it and :func:`monitor_stall_reason` names it, so the two
    cannot disagree. Elapsed time is measured, never derived from a tick count; a
    clock that went backwards reads as not yet, so the watch keeps going.
    """
    if state.stall_streak < models.DEFAULT_MONITOR_STALL_TICKS or not state.stall_started_at:
        return False
    return now - state.stall_started_at >= models.DEFAULT_MONITOR_STALL_MIN_SECS


def _fold_stall_streak(
    state: MonitorState,
    decision: MonitorDecision,
    entries: tuple[MonitorObservation, ...],
    *,
    now: float,
) -> MonitorDecision:
    """Count identical settled verdicts, and stop the watch once too many land.

    Runs AFTER classification because its input is the verdict, so every
    earlier step keeps its own answer: a terminal outcome still short-circuits,
    a spent budget still reports as a budget, and dedupe, coalescing and the
    floor still choose the decision read here.

    A COUNTED tick extends the streak and **every other tick zeroes it**, an
    unsettled one included. The trip reads a clock, so a streak left standing
    through a long pending stretch would let time alone satisfy the floor and
    give the next merge or close a stall's reason. Zeroing keeps the condition
    true only right after the counted tick that returned the stop, which
    :func:`monitor_stall_reason` relies on. A subject whose checks flap never
    builds a streak and is retired by its runtime budget, with an honest reason.
    """
    if decision is not _STALL_COUNTED_DECISION or not _verdict_is_settled(entries):
        state.stall_digest = ""
        state.stall_streak = 0
        state.stall_started_at = 0.0
        return decision
    digest = _verdict_digest(decision, entries)
    if digest == state.stall_digest and state.stall_started_at:
        state.stall_streak += 1
    else:
        # A different counted verdict starts a new streak AT ONE: this tick is
        # its first, so the count equals the number of identical conclusions.
        state.stall_digest = digest
        state.stall_streak = 1
        state.stall_started_at = now
    if _stall_tripped(state, now=now):
        return MonitorDecision.STOP_BLOCKED
    return decision


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
    # A new head retires revision-scoped memory whatever this tick reports, so
    # the reset sits ABOVE the status branch. A force-push is usually first seen
    # as a PENDING tick, which records and returns; a reset in the actionable path
    # would leave the old head's mask to suppress the new head's first real
    # failure for a whole re-alert interval. The model refuses a failed read that
    # claims a head change, so that case cannot reach here.
    if observation.head_changed:
        _reset_revision_memory(state)
    if observation.status is MonitorObservationStatus.ACTIONABLE:
        return _coalesce_actionable(state, observation, now=now)
    # Any non-actionable outcome settles the subject, so no window stays open.
    _close_window(state, now=now)
    if observation.supplemental_provider_error is not None:
        return _supplemental_provider_error_decision(
            state, state.budgets, observation.supplemental_provider_error
        )
    if observation.status is MonitorObservationStatus.SUCCESS:
        if observation.head_changed:
            return MonitorDecision.WAKE_ACTIONABLE
        return MonitorDecision.STOP_SUCCESS
    if observation.fingerprint == state.last_fingerprint:
        return MonitorDecision.NO_CHANGE
    if observation.status is MonitorObservationStatus.PENDING:
        return MonitorDecision.RECORD_ONLY
    return MonitorDecision.STOP_BLOCKED


def _window_entries(
    observation: MonitorObservation,
) -> tuple[tuple[str, MonitorSeverity], ...]:
    """The dedupe keys and urgency claims this observation puts in the window.

    A probe that names its conditions gets one entry per condition. A probe that
    names none gets ONE revision-scoped entry keyed by the subject fingerprint,
    so a zero-condition adapter is a legal shape.

    The key is built here, not through a synthetic :class:`MonitorCondition`,
    because a fingerprint is not a semantic key: one longer than the condition
    key limit would raise inside a tick.
    """
    if observation.conditions:
        return tuple(
            (monitor_condition_dedupe_key(condition), condition.severity)
            for condition in observation.conditions
        )
    return ((MONITOR_REVISION_KEY_SPACE + observation.fingerprint, MonitorSeverity.WAKE),)


def _reset_revision_memory(state: MonitorState) -> None:
    """Drop every revision-scoped window and mask, keeping the sticky half.

    A revision-scoped condition describes something a new head made obsolete;
    a SUBJECT-scoped one survives, since a force-push does not make a review
    comment new and must not replay every comment ever seen.

    Scoped by KEY SPACE, not by the conditions in hand: a revision-scoped
    condition can go unreported for a tick and come back, and its mask would
    then survive the reset and suppress a real wake.
    """
    for record in (state.coalesce_windows, state.coalesce_alerted):
        for key in [key for key in record if monitor_dedupe_key_resets_on_revision(key)]:
            record.pop(key, None)


def _coalesce_actionable(
    state: MonitorState,
    observation: MonitorObservation,
    *,
    now: float,
) -> MonitorDecision:
    """Decide one actionable tick through the per-condition coalescing window.

    Per-condition dedupe first: a condition inside its re-alert interval is
    masked, and a tick whose every condition is masked has nothing to deliver.
    Per-condition masks can tell a resolved condition from a surviving one.

    Then the window. The first actionable change wakes immediately and opens it:
    the probe interval (15 to 86400 seconds, default 300) usually exceeds the
    floor, so holding it would add latency and group nothing. A SUBSEQUENT
    change waits out the floor, folding a burst into one wake, aged from the
    OLDEST open window so no condition is held past the floor.

    An ``IMMEDIATE`` condition bypasses the floor because waiting observes
    nothing (a conflicted pull request dispatches no checks). The mask still
    applies, so a persisting urgent condition wakes once per re-alert interval.
    """
    entries = _window_entries(observation)
    # Read BEFORE the prune: a condition whose mask has just elapsed is exactly
    # the one the prune drops, so asking afterwards cannot tell a condition that
    # already woke from one that has only ever been held.
    alerted_before = {key for key, _ in entries if key in state.coalesce_alerted}
    _prune_alerted(state, now=now)
    unmasked = [(key, sev) for key, sev in entries if _realert_ready(state, key, now=now)]
    if not unmasked:
        # Every condition is inside its re-alert interval, so this wake could
        # tell the owner nothing the last one did not.
        if observation.supplemental_provider_error is not None:
            return _supplemental_provider_error_decision(
                state, state.budgets, observation.supplemental_provider_error
            )
        return MonitorDecision.NO_CHANGE

    window_was_open = bool(state.coalesce_windows)
    # A condition that already woke and whose mask has since elapsed is an
    # unresolved change re-asserting on its interval: re-report it rather than
    # telling the owner once. A condition merely HELD in the window has never
    # woken, so it is not re-asserting and the floor still applies to it.
    reasserting = any(key in alerted_before for key, _ in unmasked)
    immediate = any(sev is MonitorSeverity.IMMEDIATE for _, sev in unmasked)
    for key, _ in unmasked:
        state.coalesce_windows.setdefault(key, now)

    if immediate or reasserting or not window_was_open:
        return _wake(state, unmasked, now=now)
    oldest = min(state.coalesce_windows.values())
    if now - oldest >= models.DEFAULT_MONITOR_COALESCE_SECS:
        return _wake(state, unmasked, now=now)
    # A follow-up change still inside the floor: record it and keep waiting.
    return MonitorDecision.RECORD_ONLY


def _wake(
    state: MonitorState,
    unmasked: list[tuple[str, MonitorSeverity]],
    *,
    now: float,
) -> MonitorDecision:
    """Deliver the unmasked conditions and re-open the window on them alone.

    The window that remains IS the set of conditions this wake delivers, which is
    what :func:`stamp_monitor_alerted` reads to decide what to mask. Re-opening
    at *now* rather than clearing is what keeps a rapid follow-up folded: an
    empty window would read as a first change and wake again at once.

    A masked condition's window is dropped here rather than carried, because its
    own age is measured from the wake that masked it, and a window left standing
    from that wake would satisfy the floor for every later condition until it
    aged out.
    """
    state.coalesce_windows.clear()
    for key, _ in unmasked:
        state.coalesce_windows[key] = now
    return MonitorDecision.WAKE_ACTIONABLE


def stamp_monitor_alerted(state: MonitorState, *, now: float) -> None:
    """Record that a wake was DECIDED for the conditions it delivers.

    Called by whichever driver persists the staged state, next to the write, so
    the stamp cannot outlive it: the re-alert interval is measured from here and
    :func:`decide_monitor` only READS the map.

    The conditions to mask are exactly the window :func:`_wake` left open, so
    there is no second list of them for this to disagree with. Masking the whole
    tick's conditions instead would extend the mask on a condition this wake did
    not deliver, silencing it for a full interval it never earned.
    """
    for key in state.coalesce_windows:
        state.coalesce_alerted[key] = now


def _realert_ready(state: MonitorState, key: str, *, now: float) -> bool:
    """Whether the condition under *key* may wake again, given the interval.

    A future timestamp reads as stale rather than as permanent suppression, so a
    clock rollback cannot silence a condition forever.
    """
    last = state.coalesce_alerted.get(key)
    if not isinstance(last, (int, float)):
        return True
    elapsed = now - float(last)
    return not (0 <= elapsed < models.DEFAULT_MONITOR_REALERT_SECS)


def _close_window(state: MonitorState, *, now: float) -> None:
    """Close every open window and prune the re-alert map.

    The prune is unconditional because the re-alert map lives on a durable
    per-loop record: an entry past its interval suppresses nothing, so dropping
    it frees state that otherwise grows across every restart.
    """
    state.coalesce_windows.clear()
    _prune_alerted(state, now=now)


def _prune_alerted(state: MonitorState, *, now: float) -> None:
    """Drop window and re-alert entries past the interval, or with a bad time.

    Both maps are bounded by the same interval. A window older than the interval
    cannot change any decision -- it is far past the floor, so the next actionable
    tick wakes whether the entry is there or not -- and dropping it is what keeps
    a subject whose conditions churn from accumulating one window per distinct
    condition it ever reported.
    """
    realert_secs = models.DEFAULT_MONITOR_REALERT_SECS
    for record in (state.coalesce_alerted, state.coalesce_windows):
        stale = [
            key
            for key, last in record.items()
            if not isinstance(last, (int, float)) or now - float(last) >= realert_secs
        ]
        for key in stale:
            record.pop(key, None)


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
    """Return the first exhausted hard bound in stable policy order.

    A ``max_agent_turns`` of zero means unlimited wakes, so the turn bound is
    skipped rather than compared. Comparing it would make the bound true of a
    monitor that has run no turns at all, retiring every watch on its first wake.
    """
    budgets = state.budgets
    if now - state.created_ts >= budgets.max_runtime_secs:
        return MONITOR_STOP_RUNTIME_BUDGET
    if budgets.max_agent_turns > 0 and state.agent_turns >= budgets.max_agent_turns:
        return MONITOR_STOP_AGENT_TURN_BUDGET
    if state.total_tokens >= budgets.max_tokens:
        return MONITOR_STOP_TOKEN_BUDGET
    if state.provider_error_count >= budgets.max_provider_errors:
        return MONITOR_STOP_PROVIDER_ERROR_BUDGET
    return ""


def monitor_stall_reason(state: MonitorState, *, now: float) -> str:
    """Return the stall reason when the streak has tripped, else the empty string.

    Like :func:`monitor_budget_reason`, the ENGINE owns the reason a stop it
    decided is filed under, and a driver asks instead of guessing. Otherwise
    both writers of ``stopped_reason`` would take the observation's reason code
    and file a stall as, say, ``checks_failed``.

    **It reads the streak alone and never consults the decision.** Both callers
    compose the reason in an arm they also reach for ``STOP_SUCCESS``, yet a
    merged subject can never be filed as ``SUCCESS`` with a stall's reason. An
    invariant kept in three places, not a guard here, makes that unreachable:

    1. :func:`_fold_stall_streak` is the only code that RAISES the streak, and it
       raises it only on a COUNTED tick. **Every other tick zeroes the streak**,
       an unsettled one included, so the condition is true only right after the
       counted tick that reached it, and that call returns ``STOP_BLOCKED``.
    2. Both callers stage that stop's terminal ``outcome`` onto the SAME state
       object they persist, and both apply it as one unit
       (``autonudge._persist_staged_monitor_locked``,
       ``shadow._persist_and_publish``). Neither a record on disk nor a live loop
       can therefore hold a tripped streak while ``outcome`` is still ``None``.
    3. With an outcome recorded, :func:`decide_monitor` is never reached again:
       ``autonudge.apply_monitor_probe`` refuses before it, and
       :func:`terminal_decision_for_outcome` short-circuits the shadow path. The
       one path that clears an outcome,
       ``autonudge.restore_monitor_after_failed_session_close``, is fenced to
       ``MonitorOutcome.SESSION_CLOSE`` -- which a stall never records. A
       retarget does reopen a subject, and its reset block zeroes the fields.

    Reorder the fold, let a non-counted tick leave the streak standing, count a
    decision other than ``NO_CHANGE``, or unfence that rollback, and the reason
    composition in both callers needs a decision guard it does not have.
    ``test_monitor_controller_realert_stall`` pins the invariant, so breaking
    one of the three reddens a test that names this docstring.

    ``now`` is passed in, as everywhere in this module, so the reason cannot
    disagree with the fold that decided the stop on the same tick.
    """
    return MONITOR_STOP_VERDICT_STALL if _stall_tripped(state, now=now) else ""


def _provider_error_decision(
    state: MonitorState,
    observation: MonitorObservation,
    budgets: MonitorBudgets,
) -> MonitorDecision:
    error = observation.provider_error
    if error not in _RETRYABLE_PROVIDER_ERRORS:
        return MonitorDecision.STOP_BLOCKED
    if is_unattempted_probe(observation):
        # Both counting sites (``shadow.apply_monitor_probe`` and ``autonudge``)
        # refuse to charge a shared-cooldown skip, and the ``+ 1`` below
        # predicts that same budget, so a skip must not reach it: otherwise
        # another scope's cooldown retires a watch that made no API call. A
        # budget really spent still stops the watch in ``monitor_budget_reason``.
        return MonitorDecision.RETRY_PROVIDER
    if state.consecutive_provider_errors + 1 >= budgets.max_provider_errors:
        return MonitorDecision.STOP_BLOCKED
    return MonitorDecision.RETRY_PROVIDER


def _supplemental_provider_error_decision(
    state: MonitorState,
    budgets: MonitorBudgets,
    error: ProviderErrorKind,
) -> MonitorDecision:
    """Retry incomplete secondary evidence before retiring the readable target.

    Gated on the same retryable set as the primary error path: a permanent
    secondary failure (auth, not-found, setup) stops immediately instead of
    burning retries until the provider-error budget runs out.
    """
    if error not in _RETRYABLE_PROVIDER_ERRORS:
        return MonitorDecision.STOP_BLOCKED
    if state.consecutive_provider_errors + 1 >= budgets.max_provider_errors:
        return MonitorDecision.STOP_BLOCKED
    return MonitorDecision.RETRY_PROVIDER
