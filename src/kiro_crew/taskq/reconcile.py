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

An unleased ``retry_wait`` row is claimable and normally its dispatcher's, so
it is examined for ONE thing: a deliberate stop its artifacts prove, which
settles it ``cancelled``. Such a row is a run whose own terminal write was
lost -- a Stop whose cancel was dropped -- and left alone it stays in its
parent's pending count, so the attached-children guard never clears. Every
other verdict leaves the row as it is: ``retry_wait -> done`` is closed on
purpose, a run that began and recorded no ending is the dispatcher's to
re-run, and a ``failed`` ending is not written either, because a row parked by
a wait wake or a park can be one the dispatcher was meant to retry, and
neither of those writes a claim the pass could place the failure against.

That pass asks the probe about the row's CURRENT attempt only, the one its
latest ``claimed`` event began, named by that claim's generation (``attempt``,
:class:`ArtifactProbe`). A claim reads no artifact, so a row whose ending was
lost can be claimed and run again over the folder its earlier attempt ended
in; that earlier ending says nothing about the attempt now parked, and
settling the row on it would end live work. An ending that does not name the
attempt it belongs to settles nothing, and neither does a row with no claim.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Protocol

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
    """``probe(record) -> "done" | "failed" | "cancelled" | "started" | None``.

    None means the artifacts say nothing about how the run ended. The
    ``retry_wait`` pass also passes ``attempt``, the generation of the row's
    latest claim: the probe answers for an ending recorded by THAT attempt
    only, and None for an earlier attempt's ending or one that names no
    attempt. A probe that does not take ``attempt`` raises there, which the
    pass records and settles nothing on.
    """

    def __call__(self, rec: TaskRecord, /, attempt: int | None = None) -> str | None: ...


#: The event ``TaskStore.claim`` appends; its ``generation`` names the attempt
#: the claim began.
_CLAIMED_EVENT = "claimed"

#: A probe verdict that is not a task state: the run's own artifacts show it
#: began and record no ending. It decides one thing, that an ``admitted`` row
#: whose start mark was lost in a crash is not "claimed but never started".
STARTED = "started"

#: Kinds the running gateway can re-dispatch from a stored row today.
DEFAULT_RECOVERY_ADAPTERS: frozenset[str] = frozenset({KIND_SUBAGENT})

#: Rows per read of the ``retry_wait`` pass. The pass reads every page before it
#: writes, so a row it settles cannot shift a later page past a row unread.
_PARKED_PAGE = 500


@dataclass
class ReconcileReport:
    examined: int = 0
    #: unleased ``retry_wait`` rows read for an ending (not in ``examined``).
    parked_examined: int = 0
    settled_done: int = 0
    settled_failed: int = 0
    settled_cancelled: int = 0
    requeued: int = 0
    recovering: int = 0
    unknown_side_effect: int = 0
    awaiting_adapter: int = 0
    #: rows ended ``failed`` because their lost-owner recoveries were spent.
    lost_owner_limit: int = 0
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
    after the dispatcher has started leaves the live rows alone. An unleased
    ``retry_wait`` row is cancelled only on a stop *artifact_probe* proves for
    its current attempt, which a live run has not recorded; with no
    *artifact_probe* those rows are not read at all.
    """
    probe = artifact_probe or _no_probe
    known = frozenset(adapters)
    ts = store.now() if now is None else now
    report = ReconcileReport()
    for rec in store.active_rows(exclude_owner=store.incarnation):
        report.examined += 1
        try:
            _settle_one(store, rec, probe, known, ts, report)
        except TaskStoreUnavailable as exc:
            report.errors.append(f"{rec.id}: {exc}")
    if artifact_probe is not None:
        parked = _unleased_parked_rows(store, report)
        for rec, attempt in _with_latest_attempts(store, parked, report):
            report.parked_examined += 1
            try:
                _settle_parked(store, rec, artifact_probe, attempt, report)
            except TaskStoreUnavailable as exc:
                report.errors.append(f"{rec.id}: {exc}")
    if report.examined or report.parked_examined:
        logger.info(
            "taskq reconcile: examined=%d parked_examined=%d done=%d failed=%d "
            "cancelled=%d requeued=%d recovering=%d unknown_side_effect=%d "
            "awaiting_adapter=%d errors=%d",
            report.examined,
            report.parked_examined,
            report.settled_done,
            report.settled_failed,
            report.settled_cancelled,
            report.requeued,
            report.recovering,
            report.unknown_side_effect,
            report.awaiting_adapter,
            len(report.errors),
        )
    return report


def _verdict(
    rec: TaskRecord, probe: ArtifactProbe, report: ReconcileReport, **kw: Any
) -> str | None:
    """The probe's verdict on *rec* (called with *kw*); ``None`` when the probe raised."""
    try:
        return probe(rec, **kw)
    except Exception as exc:  # noqa: BLE001 - a probe that cannot answer says nothing
        # The probe is the caller's callable and ``None`` is already its "the
        # artifacts say nothing" answer, so an exception is that answer for THIS
        # row: settling it by class is safe (``unknown`` never re-runs), while
        # unwinding would abandon every row behind it with no later sweep.
        report.errors.append(f"{rec.id}: artifact probe failed: {exc}")
        return None


def _unleased_parked_rows(store: TaskStore, report: ReconcileReport) -> list[TaskRecord]:
    """Every ``retry_wait`` row with no lease, read in full before any write."""
    rows: list[TaskRecord] = []
    offset = 0
    while True:
        try:
            page = store.list_rows(state=RETRY_WAIT, limit=_PARKED_PAGE, offset=offset)
        except TaskStoreUnavailable as exc:
            report.errors.append(f"retry_wait scan: {exc}")
            break
        rows.extend(r for r in page if r.lease_owner is None)
        if len(page) < _PARKED_PAGE:
            break
        offset += len(page)
    return rows


def _with_latest_attempts(
    store: TaskStore, rows: list[TaskRecord], report: ReconcileReport
) -> list[tuple[TaskRecord, int]]:
    """Each row with the attempt its latest ``claimed`` event began (its generation).

    Read a page of rows per query. A row with no such claim, or one that names
    no generation, is left out: without the attempt an ending cannot be matched
    to it. A page whose claims cannot be read is left out too, and named in
    ``report.errors``.
    """
    out: list[tuple[TaskRecord, int]] = []
    for start in range(0, len(rows), _PARKED_PAGE):
        page = rows[start : start + _PARKED_PAGE]
        try:
            claims = store.latest_events([r.id for r in page], [_CLAIMED_EVENT])
        except TaskStoreUnavailable as exc:
            report.errors.extend(f"{r.id}: claim history unreadable: {exc}" for r in page)
            continue
        for rec in page:
            claim = claims.get((rec.id, _CLAIMED_EVENT))
            attempt = claim.data.get("generation") if claim is not None else None
            if type(attempt) is int and attempt > 0:
                out.append((rec, attempt))
    return out


def _settle_parked(
    store: TaskStore,
    rec: TaskRecord,
    probe: ArtifactProbe,
    attempt: int,
    report: ReconcileReport,
) -> None:
    """Cancel an unleased ``retry_wait`` row whose current attempt's artifacts prove a stop.

    ``cancelled`` is the one ending written, fenced on the state and generation
    this pass read. ``done`` has no edge from ``retry_wait``, ``failed`` is left
    to the dispatcher (see the module docstring), and ``started`` or no verdict
    leaves the row claimable for it.
    """
    verdict = _verdict(rec, probe, report, attempt=attempt)
    if verdict == CANCELLED:
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


def _settle_one(
    store: TaskStore,
    rec: TaskRecord,
    probe: ArtifactProbe,
    adapters: frozenset[str],
    now: float,
    report: ReconcileReport,
) -> None:
    verdict = _verdict(rec, probe, report)
    if verdict == DONE:
        if store.transition(rec.id, DONE, detail={"reconciled": "artifact"}):
            report.settled_done += 1
        return
    if verdict == FAILED:
        if store.transition(rec.id, FAILED, detail={"reconciled": "artifact"}):
            report.settled_failed += 1
        return
    if verdict == CANCELLED:
        if store.cancel(rec.id, reason="reconciled: tombstone") is not None:
            report.settled_cancelled += 1
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
