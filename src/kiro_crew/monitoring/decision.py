"""Pure decision policy for structured monitors."""

from __future__ import annotations

import hashlib
import json

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
    MONITOR_STOP_VERDICT_STALL,
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

#: The one decision a stall streak counts. A tick reaches it when the subject
#: settled and the engine then did NOTHING about it, which is the only shape
#: "the watch stopped learning" can take.
#:
#: Every other decision means the watch was working, so it zeroes the streak
#: rather than being skipped over. ``WAKE_ACTIONABLE`` acted. ``RECORD_ONLY`` and
#: ``RETRY_PROVIDER`` are DELIBERATE DEFERRALS -- a change held inside the
#: coalescing floor, or incomplete supplemental evidence waiting on a retry --
#: and counting a deferral is the same mistake as counting a ``PENDING`` tick: it
#: retires a watch that was waiting on purpose. At the supported 15s cadence a
#: held change produces a byte-identical ``RECORD_ONLY`` every tick and reaches
#: twelve of them 180s into a 240s window, so counting them retired the watch
#: before the window could release the wake it was folding.
#:
#: The three ``STOP_*`` decisions have already ended the watch, and zeroing on
#: them is what keeps a merge or a close from carrying a stall's reason.
_STALL_COUNTED_DECISION = MonitorDecision.NO_CHANGE

#: Observation statuses that CONCLUDE something about the subject, which is what
#: makes a tick countable toward a stall. Read off the domain's own
#: classification rather than restated as a second notion of settled: PENDING is
#: exactly the not-concluded class and PROVIDER_ERROR is not evidence about the
#: subject at all.
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

    A watch that keeps reaching the same conclusion is stuck, so the last step
    counts consecutive ticks that settled the subject and did nothing about it,
    and stops the watch once enough of them land to clear both its tick ceiling
    and its wall-clock floor. That step runs after the effect is chosen because
    the verdict it digests does not exist before then.
    """
    entries = (observation,)
    return MonitorVerdict(
        decision=_fold_stall_streak(
            state,
            _decide_effect(state, observation, now=now),
            entries,
            now=now,
        ),
        entries=entries,
    )


def _verdict_digest(
    decision: MonitorDecision,
    entries: tuple[MonitorObservation, ...],
) -> str:
    """Derive one comparable digest for a whole verdict.

    DERIVED on every tick from the verdict in hand, never persisted next to a
    second copy of what it summarizes -- layer 3's rule about subject
    fingerprints, applied one level up to the verdict. What ``MonitorState``
    keeps is the digest of an EARLIER verdict, which is history no other field
    holds, so nothing exists for it to disagree with.

    The decision is inside the digest rather than only the entries, and that is
    what makes "with no progress" collapse into verdict identity: every kind of
    progress this layer can observe moves either an entry (a new fingerprint, a
    moved head, a different status or reason) or the decision itself (a wake, a
    record, a retry). A tick that progressed the watch cannot digest the same.

    ``summary`` is deliberately out. It is operator prose rendered from the
    canonical facts the fingerprint already hashes, so it carries no fact of its
    own -- and prose that ever embedded a clock or a countdown would make this
    digest differ on every tick, leaving a stop nothing could trigger.
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

    Reuses the classification the domain already produces instead of inventing a
    second notion of settled. ``PENDING`` IS the not-concluded class --
    ``classify_pull_request_facts`` returns it for ``checks_incomplete``,
    ``checks_pending``, ``review_threads_incomplete`` and every other in-flight
    reason -- so counting a pending tick toward a stall would retire a watch for
    being early, which is the failure this exclusion exists to prevent. The
    price is a subject parked in PENDING forever, a permanently draft pull
    request being the clear case: it is retired by the runtime budget instead of
    by this, which is late but bounded and correctly labelled.

    ``PROVIDER_ERROR`` is excluded for the other reason: it is no evidence about
    the subject at all, and it already has a budget of its own.

    A verdict with NO entries concludes nothing either -- the refusal paths that
    build one never ran a probe.
    """
    return bool(entries) and all(entry.status in _SETTLED_STATUSES for entry in entries)


def _stall_tripped(state: MonitorState, *, now: float) -> bool:
    """Whether the open streak has both reached the count and covered the floor.

    THE single place the stall condition is evaluated. :func:`_fold_stall_streak`
    decides the stop with it and :func:`monitor_stall_reason` names the stop with
    it, and two spellings of one question are how a stop comes to be labelled one
    thing while it happened for another.

    Elapsed time is measured, never translated from a tick count. A negative
    interval -- a clock that went backwards -- reads as not yet, which is the safe
    direction: the watch keeps going.
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

    Positioned AFTER classification because its input IS the classification: no
    digest exists until the verdict does. The fixed evaluation order puts the
    budget check before classification for the mirror-image reason -- the
    budget's operands are already known, so an expensive classification must not
    be what exhausts it -- and applying that same rationale here yields the
    latest slot rather than the earliest. Every earlier step therefore keeps its
    own answer: a recorded terminal outcome still short-circuits, a spent budget
    still reports itself as a budget, and dedupe, coalescing and the floor still
    choose the decision this reads.

    Two outcomes only, and that is what makes the streak trustworthy: a COUNTED
    tick extends it, and **every other tick zeroes it**. An unsettled tick is
    included in that, and it has to be. The trip reads a clock, so a streak left
    standing through a long pending stretch would let time alone satisfy the floor
    and hand the next merge or close a stall's reason. Zeroing means the condition
    can only be true immediately after the counted tick that reached it -- the tick
    that returned the stop -- which is the invariant
    :func:`monitor_stall_reason` relies on to ignore the decision. The price is
    that a subject whose checks flap never accumulates a streak and is retired by
    its runtime budget instead: a later stop with an honest reason, which beats an
    earlier one with a false reason.
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
        # itself the first of it, so zeroing here would need one extra identical
        # tick before the stop and make the count mean something other than the
        # number of identical conclusions.
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


def monitor_stall_reason(state: MonitorState, *, now: float) -> str:
    """Return the stall reason when the streak has tripped, else the empty string.

    Sibling of :func:`monitor_budget_reason`, and for the same reason: the ENGINE
    owns the reason a stop it decided is filed under, and a driver asks instead
    of guessing. Both writers of ``stopped_reason`` otherwise take it from the
    observation's own reason code, which would file a stall under whatever the
    subject happened to look like -- ``checks_failed`` for a watch that stopped
    because it stopped learning anything. Getting that wrong is the anti-pattern
    of a stop that bills for a cost while reading like a conclusion, so the
    reason is the whole point of the field, not decoration on it.

    **It reads the streak alone and never consults the decision.** Both callers
    compose the reason in an arm they also reach for ``STOP_SUCCESS``, so on the
    face of it a merged subject could be filed as ``SUCCESS`` carrying a stall's
    reason -- the same anti-pattern running backwards. That is unreachable, and
    it is unreachable because of an invariant kept in three separate places
    rather than by a guard here:

    1. :func:`_fold_stall_streak` is the only code that RAISES the streak, and it
       raises it only on a COUNTED tick -- one that settled the subject and then
       did nothing about it. **Every other tick zeroes the streak**, an unsettled
       one included, so the condition can only be true immediately after the
       counted tick that reached it, and that call returns ``STOP_BLOCKED``. The
       zeroing is what makes the clock safe to read here: without it a streak left
       standing through a long pending stretch would let time alone satisfy the
       floor, and the next merge or close would carry a stall's reason.
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
    composition in both callers needs a decision guard it does not have today.
    ``test_monitor_controller`` pins the invariant rather than guarding its
    consequence, so breaking one of the three reddens a test that names this
    docstring.

    ``now`` is a parameter rather than read here, for the reason the whole module
    takes its clock as a value: the sibling ``monitor_budget_reason`` already does,
    and both callers have the tick's ``now`` in hand at the point they compose the
    reason. A function that read the clock itself could disagree with the fold
    that decided the stop microseconds earlier.
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
