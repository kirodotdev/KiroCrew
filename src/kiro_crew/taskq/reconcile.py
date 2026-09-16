"""Reconcile-first boot: settle every row a previous incarnation still owned.

A gateway that restarts finds rows in ``admitted``/``starting``/``running``/
``waiting_*``/``recovering`` whose owner is gone. Before the
dispatcher takes a single new row, each of those is examined once:

* an artifact probe says the work finished (a result file, a tombstone) -> the
  matching terminal state, so a completed run whose final ``done`` write was
  lost is not run twice;
* otherwise, by idempotency class: ``none`` and ``idempotent_key`` become
  ``recovering`` with a backoff, which the dispatcher re-claims and rebuilds a
  runtime for; ``unknown`` becomes ``unknown_side_effect``, because an external
  operation may have happened and only the entry adapter can query for it.
* a kind with no registered recovery adapter keeps its state, loses its lease,
  and gets an ``awaiting_adapter`` event -- nothing is invented for it.

``cancelled`` and the other terminal states are never touched: a cancel that
landed before the crash stays a cancel, whatever the artifacts say.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .model import (
    ADMITTED,
    CANCELLED,
    DONE,
    FAILED,
    KIND_SUBAGENT,
    QUEUED,
    RECOVERING,
    SIDE_EFFECT_UNKNOWN,
    UNKNOWN_SIDE_EFFECT,
    TaskRecord,
    recovery_backoff_secs,
)
from .store import TaskStore, TaskStoreUnavailable

logger = logging.getLogger(__name__)

#: ``artifact_probe(record) -> "done" | "failed" | "cancelled" | None``.
#: None means the artifacts say nothing about how the run ended.
ArtifactProbe = Callable[[TaskRecord], "str | None"]

#: Kinds the running gateway can re-dispatch from a stored row today.
DEFAULT_RECOVERY_ADAPTERS: frozenset[str] = frozenset({KIND_SUBAGENT})


@dataclass
class ReconcileReport:
    examined: int = 0
    settled_done: int = 0
    settled_failed: int = 0
    settled_cancelled: int = 0
    requeued: int = 0
    recovering: int = 0
    unknown_side_effect: int = 0
    awaiting_adapter: int = 0
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
        )


def _no_probe(_: TaskRecord) -> str | None:
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
    for rec in store.active_rows(exclude_owner=store.incarnation):
        report.examined += 1
        try:
            _settle_one(store, rec, probe, known, ts, report)
        except TaskStoreUnavailable as exc:
            report.errors.append(f"{rec.id}: {exc}")
    if report.examined:
        logger.info(
            "taskq reconcile: examined=%d done=%d failed=%d cancelled=%d requeued=%d "
            "recovering=%d unknown_side_effect=%d awaiting_adapter=%d errors=%d",
            report.examined,
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


def _settle_one(
    store: TaskStore,
    rec: TaskRecord,
    probe: ArtifactProbe,
    adapters: frozenset[str],
    now: float,
    report: ReconcileReport,
) -> None:
    verdict = probe(rec)
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
    if rec.state == ADMITTED:
        # Claimed but never started: no runtime, no side effect. Back to the
        # queue whatever the class, exactly as an expired admit wait does.
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
    backoff = recovery_backoff_secs(rec.attempts)
    if store.transition(
        rec.id,
        RECOVERING,
        next_run_at=now + backoff,
        detail={"reconciled": "lost_owner", "backoff_secs": backoff},
    ):
        report.recovering += 1
