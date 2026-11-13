"""Reconcile-first boot: settle every row a previous incarnation still owned.

A gateway that restarts finds rows in ``admitted``/``starting``/``running``/
``waiting_*``/``recovering`` whose owner is gone. Before the
dispatcher takes a single new row, each of those is examined once:

* an artifact probe says the work finished (a result file, a tombstone) -> the
  matching terminal state, so a completed run whose final ``done`` write was
  lost is not run twice;
* otherwise, by idempotency class: ``none`` and ``idempotent_key`` become
  ``recovering`` with a backoff, which the dispatcher re-claims and rebuilds a
  runtime for, at most ``model.RECOVERY_MAX_ATTEMPTS`` times per row; the next
  lost owner ends the row ``failed`` with the reason, so a run that takes its
  gateway down cannot restart it on every boot; ``unknown`` becomes
  ``unknown_side_effect``, because an external operation may have happened and
  only the entry adapter can query for it.
* a kind with no registered recovery adapter keeps its state, loses its lease,
  and gets an ``awaiting_adapter`` event -- nothing is invented for it.

``cancelled`` and the other terminal states are never touched: a cancel that
landed before the crash stays a cancel, whatever the artifacts say.

A ``queued`` row is claimed by nobody and is normally its dispatcher's to pick
up, so it is examined for TWO markers only, each the artifact an ending
published before the row's own terminal write landed leaves behind when that
write was lost before the crash: a REFUSAL marker (``REFUSED``, a spawn refused
before its row was ever claimed), settled ``failed``, and a QUEUED-STOP marker
(``STOPPED``, a run stopped before it started), settled ``cancelled`` -- rather
than started as if the ending had never happened. Any
other ending the artifacts record for a ``queued`` row is NOT acted on: a row
can come back to ``queued`` after an earlier attempt (a dependency wake of a
parked ``retry_wait`` row, a lost-owner requeue), and that attempt's ending
on disk is not this row's. Such a row, and one whose artifacts say nothing,
is left exactly as it was.

The one exception is a STOP of a ``retry_wait`` row's CURRENT attempt, settled
``cancelled``: a run whose own cancel was lost after its start was given back,
which otherwise stays in its parent's pending count forever. The attempt is
named by the generation of the row's latest ``claimed`` event, and the probe is
asked again with it (``ArtifactProbe``'s ``attempt``): the run folder is the row
id's, so a stop an earlier attempt recorded, or one that names no attempt,
settles nothing.
"""

from __future__ import annotations

import functools
import logging
from dataclasses import dataclass, field
from typing import Iterable, Protocol

from .model import (
    ADMITTED,
    CANCELLED,
    DONE,
    FAILED,
    KIND_SUBAGENT,
    QUEUED,
    RECOVERING,
    RECOVERY_MAX_ATTEMPTS,
    RETRY_WAIT,
    SIDE_EFFECT_UNKNOWN,
    UNKNOWN_SIDE_EFFECT,
    TaskRecord,
    recovery_backoff_secs,
    recovery_exhausted,
)
from .store import TaskStore, TaskStoreUnavailable

logger = logging.getLogger(__name__)


class ArtifactProbe(Protocol):
    """``probe(record) -> "done" | "failed" | "cancelled" | "refused" | "stopped" | "started" | None``.

    None means the artifacts say nothing about how the run ended. With
    ``attempt`` (the generation of the row's latest claim) the probe answers
    only for an ending that attempt recorded, and None for any other.
    """

    def __call__(self, rec: TaskRecord, /, attempt: int | None = None) -> str | None: ...


#: A probe verdict that is not a task state: the run's own artifacts show it
#: began and record no ending. It decides one thing, that an ``admitted`` row
#: whose start mark was lost in a crash is not "claimed but never started".
STARTED = "started"

#: A probe verdict that is not a task state: the run was REFUSED before it
#: registered (``write_refusal_tombstone``, cause ``refused``) and its row ends
#: ``failed``. It is one of the two endings the queued pass acts on (``STOPPED``
#: is the other): a plain ``failed`` or ``done`` on a ``queued`` row may be an
#: earlier attempt's, a refusal marker can only be this row's own.
REFUSED = "refused"

#: A probe verdict that is not a task state: the run was STOPPED before it
#: started (``write_queued_stop_tombstone``, cause ``queued_stop``) and its row
#: ends ``cancelled``. Like ``REFUSED`` it can only be this row's own marker, so
#: the queued pass acts on it too.
STOPPED = "stopped"

#: The two markers the queued pass acts on, each with the state it settles the row in.
_QUEUED_MARKER_STATES = {REFUSED: FAILED, STOPPED: CANCELLED}

#: Kinds the running gateway can re-dispatch from a stored row today.
DEFAULT_RECOVERY_ADAPTERS: frozenset[str] = frozenset({KIND_SUBAGENT})


@dataclass
class ReconcileReport:
    examined: int = 0
    #: ``queued`` rows read for an artifact-recorded ending (not in ``examined``).
    queued_examined: int = 0
    settled_done: int = 0
    settled_failed: int = 0
    settled_cancelled: int = 0
    requeued: int = 0
    recovering: int = 0
    unknown_side_effect: int = 0
    awaiting_adapter: int = 0
    #: rows ended ``failed`` because their lost-owner recoveries were spent.
    lost_owner_limit: int = 0
    #: ``queued`` rows whose artifact-recorded ending the store refused to
    #: write, held out of dispatch until it lands (``TaskStore.hold_until_settled``).
    held: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return (
            self.settled_done
            + self.settled_failed
            + self.settled_cancelled
            + self.requeued
            + self.recovering
            + self.unknown_side_effect
            + self.lost_owner_limit
        )


def _no_probe(_: TaskRecord, /, attempt: int | None = None) -> str | None:
    return None


def reconcile_on_boot(
    store: TaskStore,
    *,
    artifact_probe: ArtifactProbe | None = None,
    adapters: Iterable[str] = DEFAULT_RECOVERY_ADAPTERS,
    now: float | None = None,
) -> ReconcileReport:
    """Settle the rows an earlier incarnation left active. Idempotent.

    Only rows NOT leased by this incarnation are examined, so calling it again
    after the dispatcher has started leaves the live rows alone.
    """
    probe = artifact_probe or _no_probe
    known = frozenset(adapters)
    ts = store.now() if now is None else now
    report = ReconcileReport()
    # The queued pass runs FIRST: a row the active pass requeues from
    # ``admitted`` was examined by it, and must not be read a second time here.
    if not _queued_pass(store, probe, report):
        # A read the store refused leaves rows unexamined that may owe the
        # terminal write ``hold_until_settled`` exists for, and a hold needs the
        # row's id. So the whole dispatch is fenced: every dispatch read re-runs
        # this pass first and publishes nothing until it has run to completion
        # (rows settled by the pages that did land are terminal and not re-read).
        store.fence_dispatch(functools.partial(_queued_pass, store, probe, report))
    for rec in store.active_rows(exclude_owner=store.incarnation):
        report.examined += 1
        try:
            _settle_one(store, rec, probe, known, ts, report)
        except TaskStoreUnavailable as exc:
            report.errors.append(f"{rec.id}: {exc}")
    if report.examined or report.changed or report.errors:
        logger.info(
            "taskq reconcile: examined=%d queued_examined=%d done=%d failed=%d cancelled=%d "
            "requeued=%d recovering=%d unknown_side_effect=%d awaiting_adapter=%d held=%d "
            "errors=%d",
            report.examined,
            report.queued_examined,
            report.settled_done,
            report.settled_failed,
            report.settled_cancelled,
            report.requeued,
            report.recovering,
            report.unknown_side_effect,
            report.awaiting_adapter,
            report.held,
            len(report.errors),
        )
    return report


def _probe_verdict(
    rec: TaskRecord, probe: ArtifactProbe, report: ReconcileReport, attempt: int | None = None
) -> str | None:
    try:
        return probe(rec) if attempt is None else probe(rec, attempt=attempt)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot answer says nothing
        # The probe is the caller's callable and ``None`` is already its "the
        # artifacts say nothing" answer, so an exception is that answer for THIS
        # row: settling it by class is safe (``unknown`` never re-runs), while
        # unwinding would abandon every row behind it with no later sweep.
        report.errors.append(f"{rec.id}: artifact probe failed: {exc}")
        return None


def _queued_pass(store: TaskStore, probe: ArtifactProbe, report: ReconcileReport) -> bool:
    """Settle every ``queued`` row its artifacts say ended; False when a read failed.

    Walks the backlog one dispatch-window page at a time, keyed on the last row
    of the previous page, so a boot over a large backlog never holds more rows
    than the dispatcher would; a page shorter than the window is the last. A
    page the store refuses ends the pass with the error on the report.
    """
    after: tuple[float, str] | None = None
    while True:
        try:
            page = store.queued_rows(after=after)
        except TaskStoreUnavailable as exc:
            report.errors.append(f"queued pass: {exc}")
            return False
        for rec in page:
            report.queued_examined += 1
            _settle_queued(store, rec, probe, report)
        if len(page) < store.window:
            return True
        after = (page[-1].created_at, page[-1].id)


def _settle_queued(
    store: TaskStore, rec: TaskRecord, probe: ArtifactProbe, report: ReconcileReport
) -> None:
    """Settle a ``queued`` row when its artifacts carry a refusal or queued-stop marker.

    Only ``REFUSED`` and ``STOPPED`` are acted on. A ``queued`` row can be one an earlier attempt
    already ran -- a ``retry_wait`` row a dependency wake made claimable again,
    an ``admitted`` row a lost-owner requeue put back -- and that attempt's
    ``done``/``failed``/``cancelled`` on disk is not this row's ending. A
    refusal marker is written only for a spawn refused before its row was
    claimed, and a queued-stop marker only for a run stopped before it started,
    so either can only be this row's own.
    """
    verdict = _probe_verdict(rec, probe, report)
    if verdict == CANCELLED and rec.state == RETRY_WAIT:
        _cancel_parked_stop(store, rec, probe, report)
        return
    ending = _QUEUED_MARKER_STATES.get(verdict or "")
    if ending is None:
        return
    try:
        _settle_terminal(store, rec, verdict, report)
    except TaskStoreUnavailable as exc:
        report.errors.append(f"{rec.id}: {exc}")
        # The artifacts say this row was refused (or stopped) and the store
        # refused to record it. Left as it is, the row is a dispatchable
        # ``queued`` row the moment the store answers again -- a run its caller
        # was told had ended. The store keeps it out of every claim and retries
        # the write at the dispatch read, so the ending is persisted before the
        # row can be published.
        store.hold_until_settled(rec.id, ending)
        report.held += 1


def _cancel_parked_stop(
    store: TaskStore, rec: TaskRecord, probe: ArtifactProbe, report: ReconcileReport
) -> None:
    """Cancel a ``retry_wait`` row when its CURRENT attempt recorded a stop.

    The attempt is the generation of the row's latest ``claimed`` event; a row
    with none is left alone. The cancel is fenced on the state and generation
    read, so a row claimed since is not touched. A store that refuses a read or
    the write leaves the row as it was, named in ``report.errors``.
    """
    try:
        claim = store.latest_events([rec.id], ["claimed"]).get((rec.id, "claimed"))
        attempt = claim.data.get("generation") if claim is not None else None
        if type(attempt) is not int:
            return
        if _probe_verdict(rec, probe, report, attempt) != CANCELLED:
            return
        if (
            store.cancel(
                rec.id,
                reason="reconciled: tombstone",
                only_from=frozenset({RETRY_WAIT}),
                generation=rec.generation,
            )
            is not None
        ):
            report.settled_cancelled += 1
    except TaskStoreUnavailable as exc:
        report.errors.append(f"{rec.id}: {exc}")


def _settle_terminal(
    store: TaskStore, rec: TaskRecord, verdict: str | None, report: ReconcileReport
) -> bool:
    """Settle *rec* into the terminal its artifacts recorded; False when they recorded none."""
    if verdict == DONE:
        if store.transition(rec.id, DONE, detail={"reconciled": "artifact"}):
            report.settled_done += 1
        return True
    if verdict in (FAILED, REFUSED):
        detail = "refusal" if verdict == REFUSED else "artifact"
        if store.transition(rec.id, FAILED, detail={"reconciled": detail}):
            report.settled_failed += 1
        return True
    if verdict in (CANCELLED, STOPPED):
        if store.cancel(rec.id, reason="reconciled: tombstone") is not None:
            report.settled_cancelled += 1
        return True
    return False


def _settle_one(
    store: TaskStore,
    rec: TaskRecord,
    probe: ArtifactProbe,
    adapters: frozenset[str],
    now: float,
    report: ReconcileReport,
) -> None:
    verdict = _probe_verdict(rec, probe, report)
    try:
        settled = _settle_terminal(store, rec, verdict, report)
    except TaskStoreUnavailable:
        ending = _QUEUED_MARKER_STATES.get(verdict or "")
        if ending is not None:
            # As in the queued pass: an ending its caller was told of must not
            # leave a claimable (``recovering``) row to be run again.
            store.hold_until_settled(rec.id, ending)
            report.held += 1
        raise
    if settled:
        return
    if rec.state == ADMITTED and not (
        verdict == STARTED and rec.side_effect_class == SIDE_EFFECT_UNKNOWN
    ):
        # Claimed but never started: no runtime, no side effect. Back to the
        # queue whatever the class, exactly as an expired admit wait does. A run
        # the artifacts show STARTED (its start mark posted and lost in the
        # crash) did run: with an ``unknown`` class it falls through and is
        # parked below like any other started run of that class. A class that
        # makes a fresh run safe is still requeued, since ``admitted`` has no
        # edge to ``recovering``.
        if store.transition(rec.id, QUEUED, detail={"reconciled": "lost_owner"}):
            report.requeued += 1
        return
    if rec.kind not in adapters:
        # Nothing here knows how to rebuild this kind; say so on the row and
        # drop the dead owner's lease so a future adapter can claim it.
        if rec.lease_owner is not None:
            store.append_event(rec.id, "awaiting_adapter", {"kind": rec.kind, "state": rec.state})
            store.release_lease(rec.id)
        report.awaiting_adapter += 1
        return
    if rec.side_effect_class == SIDE_EFFECT_UNKNOWN:
        if store.transition(rec.id, UNKNOWN_SIDE_EFFECT, detail={"reconciled": "lost_owner"}):
            report.unknown_side_effect += 1
        return
    if rec.state == RECOVERING:
        # Already recovering; with a dead owner's lease, drop it so the row is
        # claimable. With no lease it is already waiting for the dispatcher.
        if rec.lease_owner is not None and store.release_lease(rec.id):
            report.recovering += 1
        return
    recoveries = store.lost_owner_recoveries(rec.id)
    if recovery_exhausted(recoveries):
        # Its owner died during every recovery this row was granted: a run that
        # takes its gateway down would otherwise restart it on every boot.
        reason = (
            f"the gateway stopped during this task {recoveries + 1} times; "
            f"not re-dispatched after {RECOVERY_MAX_ATTEMPTS} recoveries"
        )
        if store.transition(
            rec.id,
            FAILED,
            detail={
                "reconciled": "lost_owner_limit",
                "lost_owner_recoveries": recoveries,
                "error": reason,
            },
        ):
            report.lost_owner_limit += 1
            logger.warning("taskq reconcile: %s failed: %s", rec.id, reason)
        return
    backoff = recovery_backoff_secs(rec.attempts)
    if store.transition(
        rec.id,
        RECOVERING,
        next_run_at=now + backoff,
        detail={"reconciled": "lost_owner", "backoff_secs": backoff},
    ):
        report.recovering += 1
