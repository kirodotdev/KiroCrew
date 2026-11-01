"""Parked and merged messages, and late starts, for :class:`AdmissionService`.

A message sent to a busy session is parked work; a session drain may merge
several into one turn. These helpers decide when such a turn must not run
(a person stopped one of its messages), end every merged message with the
turn's outcome, and stop an execution whose admitted work ended while its start
was still in flight. :mod:`admission_gate` keeps the service's own methods as
the call sites; the service is passed in. A caller cancelled before its work
started ends that work here, so it never keeps a slot.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING

from kiro_crew.shared_crew import admission_settle as _settle
from kiro_crew.shared_crew.admission import (
    TERMINAL,
    InvalidTransition,
    OperationState,
    ReasonCode,
    StaleRevision,
)
from kiro_crew.shared_crew.scheduler import Decision

if TYPE_CHECKING:
    from kiro_crew.shared_crew.admission_gate import Admission, AdmissionService, _Live

logger = logging.getLogger(__name__)

S = OperationState

#: ``extra`` key naming the work a merged queued message ran inside.
MERGED_INTO_KEY = "merged_into"
#: Reasons that mean a person stopped the work: its text must never run.
PERSON_STOPS = frozenset({ReasonCode.CANCELLED_BY_USER, ReasonCode.ACCESS_REMOVED})


async def dropped_as_changed(service: AdmissionService, lives: list[_Live], key: tuple) -> bool:
    """End parked *lives* whose drained turn is not the (digest, budget) *key* they were parked as.

    An edit or a merge changes the body or the person: that turn is admitted
    afresh, so the size limit and the charge are taken on what really runs.
    """
    first = lives[0].work
    if not key or (first.request_digest, first.budget_key) == key:
        return False
    for live in lives:
        await service._end(live, S.CANCELLED, reason=ReasonCode.CANCELLED_BY_USER)
    return True


async def cancelled_parked(service: AdmissionService, work_ids: list[str]) -> Admission | None:
    """Refuse a merged prompt containing any work a person's stop ended.

    That is work its sender cancelled and work whose sender was removed: the
    text of either must never run, alone or merged into someone else's turn.
    The refusal carries the work's own reason.
    """
    from kiro_crew.shared_crew.admission_gate import Admission

    store = service._store
    if store is None:
        return None
    lives = [service._live.get(work_id) for work_id in work_ids]
    works = [
        live.work if live is not None else await store.get(work_id)
        for work_id, live in zip(work_ids, lives)
    ]
    for live, work in zip(lives, works):
        if work is None:
            continue
        why = live.cancel_reason if live is not None else None
        if why not in PERSON_STOPS:
            why = work.reason
        if why in PERSON_STOPS:
            assert why is not None
            return Admission(
                service,
                Decision.REJECTED.value,
                work=work,
                reason=why,
                capacity=service.capacity(work.budget_key),
            )
    return None


async def settle_followers(service: AdmissionService, live: _Live) -> None:
    """End every message merged into *live*'s turn with *live*'s own outcome."""
    store = service._store
    assert store is not None
    target = live.work.state
    for work_id in live.followers:
        follower = service._live.get(work_id)
        if follower is None or follower.work.state in TERMINAL:
            continue
        extra = {MERGED_INTO_KEY: live.work.work_id}
        try:
            if target in (S.COMPLETED, S.FAILED):
                for step in (S.ADMITTED, S.RUNNING):
                    if follower.work.state is not step:
                        follower.work = await store.advance(follower.work, step)
                follower.work = await store.advance(
                    follower.work,
                    target,
                    reason=live.work.reason,
                    runtime=service._stopped_ref(follower),
                    extra=extra,
                )
            else:
                end = S.EXPIRED if target is S.EXPIRED else S.CANCELLED
                follower.work = await store.advance(
                    follower.work, end, reason=live.work.reason, extra=extra
                )
        except (StaleRevision, InvalidTransition):
            current = await store.get(work_id)
            if current is not None:
                follower.work = current
        except Exception:
            logger.warning("shared crew: settling merged %s failed", work_id, exc_info=True)
            # Kept tracked until the ticker lands the terminal write.
            _settle.mark_unpersisted(follower, target, live.work.reason, extra)
            follower.self_stopped = True
            follower.ended.set()
            continue
        follower.self_stopped = True
        follower.ended.set()
        service._live.pop(work_id, None)


def stop_late_start(service: AdmissionService, work_id: str, execution_id: str) -> None:
    """Stop an execution whose admitted work ended before it attached."""
    ended = service._ended_unattached.pop(work_id, None)
    if ended is None:
        return
    work, hook = ended
    if hook is None or not execution_id:
        return
    logger.info("shared crew: %s ended before its start attached; stopping it", work_id)
    try:
        hook(replace(work, extra={**work.extra, "execution_id": execution_id}))
    except Exception:  # noqa: BLE001 -- logged; the record is already terminal
        logger.warning("shared crew: stopping late start %s failed", work_id, exc_info=True)


async def end_unstarted(service: AdmissionService, live: _Live, exc: BaseException) -> None:
    """End work that never started, so its slot is freed: cancelled, or failed on any error."""
    if live.work.state in TERMINAL:
        return
    if isinstance(exc, asyncio.CancelledError):
        await service._end(live, S.CANCELLED, reason=ReasonCode.CANCELLED_BY_USER)
    elif isinstance(exc, Exception):
        service._fail_closed(live, "start")


async def cancel_disabled_queue(service: AdmissionService) -> None:
    """Wake queued callers with the same refusal as pre-start policy revalidation."""
    for live in list(service._live.values()):
        if live.work.state is S.QUEUED and not live.merged_into:
            await service._end(live, S.CANCELLED, reason=ReasonCode.POLICY_CHANGED)


async def sweep_parked(service: AdmissionService, at: float) -> int:
    """End parked messages past their expiry whose queue entry is gone.

    A parked message waits on its session's own turn, so its expiry is a
    check-in, not a deadline: while its queue entry still exists it waits
    on. With no way to ask (no probe) it expires like queued work.
    """
    from kiro_crew.shared_crew.admission_gate import RECONCILE_DETAIL_KEY

    ended = 0
    for ticket in service._scheduler.parked_tickets():
        if ticket.expires_at > at:
            continue
        live = service._live.get(ticket.work_id)
        if live is None:
            service._scheduler.cancel_queued(ticket.work_id)
            continue
        present: bool | None = None
        if service._parked_present is not None:
            try:
                present = bool(service._parked_present(ticket.work_id))
            except Exception:  # noqa: BLE001 -- unknown keeps the default expiry
                present = None
        if present:
            ticket.expires_at = at + service._limits.queue_expiry_seconds
            continue
        if present is False:
            await service._end(
                live, S.CANCELLED, reason=None, extra={RECONCILE_DETAIL_KEY: "queue_entry_gone"}
            )
        else:
            await service._end(live, S.EXPIRED, reason=ReasonCode.QUEUE_EXPIRED)
        ended += 1
    return ended


__all__ = [
    "MERGED_INTO_KEY",
    "PERSON_STOPS",
    "cancelled_parked",
    "end_unstarted",
    "settle_followers",
    "stop_late_start",
]
