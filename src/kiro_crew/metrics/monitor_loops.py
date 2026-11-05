"""What auto-nudge loops and structured monitors actually DO.

``kirocrew.inventory.monitor_loops.active`` says how many loops exist right now.
It cannot say whether any of them did useful work: a host with four live loops
that tick quietly forever and a host with four loops delivering a turn every
interval read identically. These four counters close that gap.

=========================================  ===================================
counter                                    one increment per
=========================================  ===================================
``kirocrew.autonudge.loops.started``       loop that went from inactive to active
``kirocrew.autonudge.ticks``               tick that reached a decision
``kirocrew.autonudge.loops.stopped``       loop that went inactive or vanished
``kirocrew.monitor.decisions``             ``decide_monitor`` policy evaluation
=========================================  ===================================

The first number to read is the ratio of ``quiet`` to ``delivered`` ticks: it
says whether the probe gate is earning its keep or whether loops are spending a
model turn on every interval.

**Starts and stops come from one diff, so they stay paired by construction.**
A loop can be paused and resumed many times, and each active period ends in a
stop, so ``loops.started`` counts every inactive-to-active transition, not only
the first arm. Both counters are taken at the store's one commit point
(``autonudge_service/store.py``), which compares the loops active in the last
committed store with the rows just written: an id active before and not now is
a stop, an id active now and not before is a start. No mutation names either
counter, so the arm, a resume, a reconciler's revival, a restore after a failed
session close, a replacement rollback and any activation path added later are
all counted the same way, after their write has committed.

**A restart does not re-count a start.** ``_load`` seeds that baseline from the
rows active on disk, so a gateway that boots with nine persisted loops adds
nothing. A row the load resumes from the torn-deactivation shape was inactive
on disk, so it counts once, at the first commit that writes it active, and its
later stop is paired. A row the load could not parse is rewritten verbatim but
never served, and is not counted.

**A stop is counted exactly once per active-to-inactive transition, however it
was reached.** The source is the store's one commit point, which diffs the loops
that were active in the last committed store against the rows just written
(``autonudge_stop_log.stop_records``). A second ``autonudge_stop`` on an
already-stopped loop produces no record and no increment, and the dozen stop
paths -- the cycle cap, a spent runtime budget, an unanswered approval, a user
delete, a closed session, an agent's own call -- cannot double-count between
them. A loop that is resumed and stopped again is stopped twice, and was
started twice.

**What ``ticks`` observes, and what it does not.** It observes the PROMPT
delivery tick: a plain loop, and a prompt loop whose probe gate watches a
subject. A structured monitor record is driven by its own typed controller
(``firing.py`` hands it to ``_on_monitor_tick`` and returns), which never reaches
either tick emit site, so such a record contributes NOTHING to ``ticks`` -- its
per-tick verdict is :data:`MONITOR_DECISIONS` instead, which is the controller's
own decision and the only honest reading of what that path decided. The
consequence worth knowing before building a dashboard on these series: within
``ticks``, ``kind=monitor`` means a probe-gated PROMPT loop, which is a narrower
population than ``kind=monitor`` on :data:`LOOPS_STARTED` and
:data:`LOOPS_STOPPED` (both of which also count structured records). So a
per-loop rate must not be taken as ``ticks`` over ``loops.started`` for
``kind=monitor``; the quiet-to-delivered RATIO inside ``ticks`` is read against
its own population and is unaffected.

**No free text leaves here.** A stored stop reason is model-authored or
user-authored on some paths and is composed at the call site on others
(``campaign_<status>``), so :data:`STOP_REASONS` maps the stored string to a
closed value and anything unrecognised becomes :data:`OTHER`. The same applies to
the monitor kind, which is checked against the registry of monitored kinds. No
loop id, slot key, target URL, session key or detail string is ever an attribute.

**Every closed set here is built from the thing that produces its values, never
retyped beside it.** :data:`STOP_REASONS` is assembled from the stop-reason
constants and :class:`~kiro_crew.monitoring.models.MonitorOutcome`;
:data:`MONITOR_DECISION_VALUES` from
:class:`~kiro_crew.monitoring.models.MonitorDecision`. A hand-written copy of
either could only ever catch a new member by reporting it as :data:`OTHER` while
reading as though someone had checked -- which is exactly what a retyped
stop-reason table did to one outcome before this was derived.

Every function here is best-effort: a telemetry failure is swallowed after a
debug log, because the paths these sit on are a store commit, a timer tick and a
pure decision policy. Recording is an in-memory add -- no disk write, no network
call, no log line.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from kiro_crew import autonudge_stop_log
from kiro_crew.autonudge_service import model as loop_model
from kiro_crew.monitoring import models as monitor_models
from kiro_crew.monitoring.models import MonitorDecision, MonitorOutcome
from kiro_crew.monitoring.registry import monitor_kind

logger = logging.getLogger(__name__)

#: One increment per inactive-to-active transition the store's commit point
#: observed. ``kind`` only.
LOOPS_STARTED = "kirocrew.autonudge.loops.started"

#: One increment per tick that reached a decision about spending a turn.
TICKS = "kirocrew.autonudge.ticks"

#: One increment per loop the store's commit point observed stopping.
LOOPS_STOPPED = "kirocrew.autonudge.loops.stopped"

#: One increment per ``monitoring/decision.py::decide_monitor`` call -- per
#: POLICY EVALUATION, which is narrower than every verdict a caller acts on; see
#: :func:`record_monitor_decision`.
MONITOR_DECISIONS = "kirocrew.monitor.decisions"

#: The value every closed enum here falls back to, per the metrics RFC's rule
#: that an unknown attribute value maps to ``other`` rather than travelling as
#: itself. Shared by all four attributes that can meet one.
OTHER = "other"

# ---------------------------------------------------------------------------
# kind -- what sort of automation this is
# ---------------------------------------------------------------------------

#: A prompt loop: it delivers its message as a turn on its slot.
KIND_LOOP = "loop"
#: A loop carrying monitor state -- a structured controller record, or a prompt
#: loop whose probe gate watches a subject. Both are "this automation is driven
#: by an observation", which is the distinction worth a label; the finer split is
#: already visible in :data:`MONITOR_DECISIONS`, which only structured records
#: reach. On :data:`TICKS` this value covers the probe-gated prompt loops ALONE,
#: for the reason the module docstring gives at length.
KIND_MONITOR = "monitor"

#: Both values ``kind`` can carry on the three ``autonudge.*`` counters.
LOOP_KINDS = frozenset({KIND_LOOP, KIND_MONITOR})

# ---------------------------------------------------------------------------
# outcome -- what one tick decided
# ---------------------------------------------------------------------------

#: The fire callback reported that a turn landed on the slot.
OUTCOME_DELIVERED = "delivered"
#: The probe gate read the subject, found nothing new, and spent no turn. The
#: whole saving the gate exists for.
OUTCOME_QUIET = "quiet"
#: The fire callback declined -- the slot was mid-turn, the session was busy, the
#: nudge was skipped. No turn was spent and the tick is owed back.
OUTCOME_REFUSED = "refused"
#: The fire callback RAISED. Distinguished from ``refused`` because a refusal is
#: the system working as designed and an exception is a defect.
OUTCOME_FAILED = "failed"

#: Every value ``outcome`` can carry.
TICK_OUTCOMES = frozenset({OUTCOME_DELIVERED, OUTCOME_QUIET, OUTCOME_REFUSED, OUTCOME_FAILED})

# ---------------------------------------------------------------------------
# ran -- how long the loop lived, as a bucket
# ---------------------------------------------------------------------------

#: Bucket edges, stated here and in ``docs/system-specs/modules/metrics.md``.
#: A counter with a closed bucket attribute rather than a histogram, so the
#: distribution survives a backend that keeps no explicit buckets.
_ONE_HOUR_SECS = 3600
_ONE_DAY_SECS = 86400

RAN_LT_1H = "lt_1h"
RAN_1H_24H = "1h_24h"
RAN_GT_24H = "gt_24h"

#: Every value ``ran`` can carry. :data:`OTHER` is a row whose creation time was
#: missing or unreadable, so no runtime can be derived -- reported as unknown
#: rather than folded into ``lt_1h``, which would claim a short life it does not
#: have evidence for.
RAN_BUCKETS = frozenset({RAN_LT_1H, RAN_1H_24H, RAN_GT_24H, OTHER})

# ---------------------------------------------------------------------------
# reason -- why the loop stopped
# ---------------------------------------------------------------------------

#: The reported value shared by the two spellings of one event: a legacy row
#: records ``approval_stalled`` and a structured record records
#: ``approval_stall``, and splitting them would make an operator add two series
#: to get one answer.
_APPROVAL_STALL = "approval_stall"

#: Every stored stop reason whose reported value IS its stored spelling, taken
#: from the constants that write it. Imported rather than retyped: the module's
#: own argument against a hand-written copy of an enum applies here too, and a
#: retyped table had already lost ``MonitorOutcome.TARGET_UNAVAILABLE``.
_PASSED_THROUGH_REASONS = (
    # The legacy loop reasons (``autonudge_service/model.py``).
    loop_model.CYCLE_CAP_REASON,
    # A spent runtime budget is reported under its stored spelling, the string
    # the WARNING line and the stop file carry, so one name finds it in all three.
    loop_model.RUNTIME_BUDGET_REASON,
    loop_model.STOP_SENTINEL_REASON,
    loop_model.MONITOR_TERMINAL_REASON,
    loop_model.AUTONUDGE_STOP_REASON,
    loop_model.SESSION_START_FAILURE_REASON,
    loop_model.CONSECUTIVE_FAILURE_REASON,
    loop_model.STRUCTURAL_TERMINAL_REASON,
    loop_model.MODEL_REFUSED_REASON,
    loop_model.SENTINEL_DROPPED_REASON,
    loop_model.MANUAL_STOP_REASON,
    # The structured monitor stop reasons (``monitoring/models.py``). Each budget
    # stays its own value: they name resources an operator raises separately.
    monitor_models.MONITOR_STOP_RUNTIME_BUDGET,
    monitor_models.MONITOR_STOP_AGENT_TURN_BUDGET,
    monitor_models.MONITOR_STOP_TOKEN_BUDGET,
    monitor_models.MONITOR_STOP_PROVIDER_ERROR_BUDGET,
    monitor_models.MONITOR_STOP_VERDICT_STALL,
    monitor_models.MONITOR_STOP_INVALID_RECORD,
    monitor_models.MONITOR_STOP_UNSUPPORTED_VERSION,
    monitor_models.MONITOR_STOP_COMPLETION_UNAVAILABLE,
    monitor_models.MONITOR_STOP_SESSION_UNAVAILABLE,
    monitor_models.MONITOR_STOP_USER,
    monitor_models.MONITOR_STOP_SESSION_CLOSE,
    # ``autonudge_stop_log``'s own defaults: the row was gone with no note, or it
    # went inactive carrying nothing.
    autonudge_stop_log.REMOVED_REASON,
    autonudge_stop_log.UNSPECIFIED_REASON,
)

#: Stored stop reason -> the closed value it is reported as.
#:
#: BUILT from the constants and the enum that write those strings, never retyped
#: beside them: the legacy loop reasons, the structured monitor stop reasons, the
#: terminal :class:`~kiro_crew.monitoring.models.MonitorOutcome` a monitor row
#: falls back to when it carries no stop reason of its own, and the two defaults
#: ``autonudge_stop_log`` supplies. A reason added to any of those sources is
#: reported under its own name without an edit here; a retyped table silently
#: reported the one it had missed as :data:`OTHER`.
#:
#: The one rename is the exception and is spelled out, because a reported value
#: that differs from the stored string is exactly the thing a reader must be able
#: to find in one place.
STOP_REASONS: dict[str, str] = {
    **{reason: reason for reason in _PASSED_THROUGH_REASONS},
    # A monitor row with no stop reason falls back to its terminal outcome, so
    # every member of that enum is a reason this counter can meet.
    **{outcome.value: outcome.value for outcome in MonitorOutcome},
    loop_model.APPROVAL_STALL_REASON: _APPROVAL_STALL,
    monitor_models.MONITOR_STOP_APPROVAL_STALL: _APPROVAL_STALL,
}

#: Every value ``reason`` can carry.
STOP_REASON_VALUES = frozenset(STOP_REASONS.values()) | {OTHER}

# ---------------------------------------------------------------------------
# ended_by -- who or what ended the loop, a fixed grouping of ``reason``
# ---------------------------------------------------------------------------

#: Every value ``ended_by`` can carry.
ENDED_BY_VALUES: tuple[str, ...] = (
    "agent_stop",
    "user_stop",
    "budget",
    "subject_done",
    "stalled",
    "error",
    OTHER,
)

#: Each :data:`STOP_REASON_VALUES` member -> the ``ended_by`` group it belongs to.
#: A function of ``reason``, so the attribute adds no series. Written out rather
#: than derived, because the grouping is a judgement about what each reason means;
#: ``test_monitor_loop_counters.py`` asserts the keys are exactly
#: :data:`STOP_REASON_VALUES`, so a new stop reason cannot ship unmapped.
ENDED_BY: dict[str, str] = {
    # The agent declared the goal met.
    "autonudge_stop": "agent_stop",
    "stop_sentinel": "agent_stop",
    # A person stopped it, or removed what it ran on.
    "user_stop": "user_stop",
    "manual": "user_stop",
    "session_close": "user_stop",
    "removed": "user_stop",
    # A cap or budget ran out.
    "cycle_cap": "budget",
    "runtime_budget": "budget",
    "agent_turn_budget": "budget",
    "token_budget": "budget",
    "provider_error_budget": "budget",
    "budget": "budget",
    # The watched subject finished.
    "success": "subject_done",
    "monitor_terminal": "subject_done",
    # It waited on something that never came.
    "approval_stall": "stalled",
    "verdict_stall": "stalled",
    "blocked": "stalled",
    # It could not keep running.
    "session_start_failures": "error",
    "consecutive_failures": "error",
    "structural_terminal": "error",
    "model_refused": "error",
    "sentinel_dropped": "error",
    "invalid_monitor_record": "error",
    "unsupported_monitor_version": "error",
    "completion_evidence_unavailable": "error",
    "session_unavailable": "error",
    "target_unavailable": "error",
    # No reason recorded, or one not recognised.
    "unspecified": OTHER,
    OTHER: OTHER,
}


def ended_by(reason: str) -> str:
    """The :data:`ENDED_BY_VALUES` group a closed stop *reason* belongs to."""
    return ENDED_BY.get(reason, OTHER)


# ---------------------------------------------------------------------------
# decision -- what the monitor controller decided
# ---------------------------------------------------------------------------

#: Every value ``decision`` can carry, DERIVED from the enum that produces them
#: (``monitoring.models.MonitorDecision``) rather than restated here. A second
#: hand-written copy of a seven-member enum is a list that drifts: the only thing
#: it could catch is a new decision, and what it would do is report that decision
#: as ``other`` while reading as though someone had checked.
MONITOR_DECISION_VALUES = frozenset(member.value for member in MonitorDecision)


def loop_kind(loop: Any) -> str:
    """``monitor`` when *loop* carries monitor state, else ``loop``.

    The same derivation ``autonudge_stop_log._summary`` uses for its stop
    records, so one loop carries one label across the started, tick and stopped
    counters for its whole life. Which loops REACH each counter still differs:
    see the module docstring on what ``ticks`` observes.
    """
    return KIND_MONITOR if getattr(loop, "monitor", None) is not None else KIND_LOOP


def ran_bucket(ran_secs: object) -> str:
    """Which :data:`RAN_BUCKETS` member *ran_secs* falls in.

    A negative runtime is possible -- the stop records derive it from a wall
    clock the host may have stepped backwards -- and is reported as ``lt_1h``,
    the bucket it is nearest. A missing or non-numeric value is :data:`OTHER`.
    """
    if isinstance(ran_secs, bool) or not isinstance(ran_secs, (int, float)):
        return OTHER
    if ran_secs < _ONE_HOUR_SECS:
        return RAN_LT_1H
    if ran_secs < _ONE_DAY_SECS:
        return RAN_1H_24H
    return RAN_GT_24H


def stop_reason(stored: object) -> str:
    """The closed :data:`STOP_REASON_VALUES` member *stored* maps to.

    Never the stored string itself: a reason is composed at some call sites
    (``campaign_<status>``) and is unbounded by construction, so an unrecognised
    one is :data:`OTHER`.
    """
    if not isinstance(stored, str):
        return OTHER
    return STOP_REASONS.get(stored.strip(), OTHER)


#: The terminal outcomes a structured monitor row records next to its stop
#: reason. Built from the enum, so the fallback below stays a closed set.
_MONITOR_OUTCOME_VALUES = frozenset(outcome.value for outcome in MonitorOutcome)


def monitor_outcomes(rows: Iterable[Any]) -> dict[str, str]:
    """Loop id -> terminal :class:`MonitorOutcome` value, for monitor rows that carry one.

    A structured monitor that stops on a provider verdict stores the
    observation's own reason code (``pull_request_merged``,
    ``pull_request_closed``, ``monitor_blocked``) as its stop reason. Those codes
    are per-provider and not in :data:`STOP_REASONS`, so the counter falls back to
    the outcome the same commit wrote. A value outside the enum is dropped here.
    """
    out: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, Mapping) or not row.get("id"):
            continue
        monitor = row.get("monitor")
        outcome = monitor.get("outcome") if isinstance(monitor, Mapping) else None
        if isinstance(outcome, str) and outcome in _MONITOR_OUTCOME_VALUES:
            out[str(row["id"])] = outcome
    return out


def monitor_kind_label(kind: object) -> str:
    """*kind* when it is a REGISTERED monitored kind, else :data:`OTHER`.

    ``monitoring/registry.py`` is the catalogue every boundary that gates a kind
    already reads, so asking it is what keeps this attribute closed without
    restating the list here. A kind arriving from a store row written by a newer
    gateway has no entry and is reported as ``other``.
    """
    return kind if isinstance(kind, str) and monitor_kind(kind) is not None else OTHER


def _emit(name: str, attrs: dict[str, str | int | bool | float]) -> None:
    """Add 1 to *name*; never raises.

    Imported lazily inside the call because ``metrics.provider`` reads the config
    and the modules emitting these sit under it -- the same cycle every existing
    emit site avoids the same way.
    """
    try:
        from kiro_crew.metrics.events import emit_counter

        emit_counter(name, attrs)
    except Exception:  # telemetry must never break the instrumented path
        logger.debug("monitor-loop counter emit failed for %s", name, exc_info=True)


def record_loop_started(summary: Mapping[str, Any]) -> None:
    """One loop went from inactive to active, from its commit-point summary.

    Called only by the store's commit point
    (``autonudge_service/store.py::LoopStore._record_starts_committed``), for an
    id active in the rows just written and absent from the last committed
    baseline -- the same diff, read the other way, that yields a stop record. No
    mutation calls this, so an activation path added later is counted without
    knowing the counter exists.

    Takes the ``autonudge_stop_log`` summary rather than a loop for the reason
    :func:`record_loop_stopped` takes a record: it is what the commit point has,
    and its ``kind`` is the one the stop record carries, so a start and its stop
    share one label by construction.
    """
    kind = summary.get("kind")
    _emit(LOOPS_STARTED, {"kind": kind if kind in LOOP_KINDS else OTHER})


def record_tick(loop: Any, outcome: str) -> None:
    """One tick reached *outcome*. Exactly one call per tick that decided.

    A tick that returns before deciding anything about a turn -- a loop whose
    bound just ran out, a stand-down, a hold for a person, a terminal verdict
    that retired the watch -- records nothing here: it stopped the loop or
    deferred it, which :data:`LOOPS_STOPPED` and the loop's own re-arm already
    describe. Counting those as ticks would make the quiet-to-delivered ratio --
    the number this instrument exists to serve -- read against a denominator of
    events that were never candidates for a turn, and a terminal one would be
    reported twice over, once here and once as a stop.

    A structured monitor record reaches neither call site at all; see the module
    docstring for what that means for ``kind`` on this counter.
    """
    _emit(
        TICKS,
        {
            "kind": loop_kind(loop),
            "outcome": outcome if outcome in TICK_OUTCOMES else OTHER,
        },
    )


def record_loop_stopped(record: Mapping[str, Any], outcome: str | None = None) -> None:
    """One loop stopped, from one ``autonudge_stop_log`` stop record.

    Takes the record rather than a loop because the record is what the commit
    point has: by the time a stop is observable the row may be GONE from the
    store, and the summary captured when it was last seen active is the only
    description of it left. It already carries the three things this needs --
    ``kind``, the stored ``reason``, and ``ran_secs`` -- and nothing else from it
    is read.

    *outcome* is the row's terminal :class:`MonitorOutcome` value, when the row
    is still present and carries one (see :func:`monitor_outcomes`). It is used
    only when the stored reason is not in :data:`STOP_REASONS`, so a PR monitor
    that stopped on ``pull_request_merged`` is reported as ``success`` rather
    than :data:`OTHER`.

    ``ended_by`` is the fixed grouping :data:`ENDED_BY` of the reported reason.
    """
    kind = record.get("kind")
    reason = stop_reason(record.get("reason"))
    if reason == OTHER and outcome in _MONITOR_OUTCOME_VALUES:
        reason = stop_reason(outcome)
    _emit(
        LOOPS_STOPPED,
        {
            "kind": kind if kind in LOOP_KINDS else OTHER,
            "reason": reason,
            "ran": ran_bucket(record.get("ran_secs")),
            "ended_by": ended_by(reason),
        },
    )


def record_monitor_decision(decision: object, kind: object) -> None:
    """One ``decide_monitor`` verdict, labelled by decision and monitored kind.

    This counts POLICY EVALUATIONS, not every verdict a caller acts on.
    ``monitor_records.apply_monitor_probe`` short-circuits some ticks to a
    verdict of its own -- a record with an outcome already written, a reading it
    refuses -- without consulting the policy at all, and those are deliberately
    not counted here: the population is "the policy was asked, and this is what
    it answered", which is what makes a rate over the decisions readable.
    """
    value = getattr(decision, "value", decision)
    label = value if isinstance(value, str) and value in MONITOR_DECISION_VALUES else OTHER
    _emit(
        MONITOR_DECISIONS,
        {"decision": label, "kind": monitor_kind_label(kind)},
    )
