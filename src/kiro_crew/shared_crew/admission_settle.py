"""Terminal writes the task store refused, for :class:`AdmissionService`.

When a store write fails, the work ends in memory at once (its waiters wake and
its child is stopped), but it keeps its slot and stays tracked until the
terminal transition is durable. Each ticker pass retries the write, so the work
API and the capacity count never disagree with the store for longer than the
store is unwritable. :mod:`admission_gate` keeps ``_fail_closed`` and ``tick``
as the call sites; the service is passed in.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, Mapping

from kiro_crew.shared_crew.admission import (
    TERMINAL,
    InvalidTransition,
    OperationState,
    ReasonCode,
    StaleRevision,
    is_legal_transition,
)

if TYPE_CHECKING:
    from kiro_crew.shared_crew.admission_gate import AdmissionService, _Live

logger = logging.getLogger(__name__)

S = OperationState


def mark_unpersisted(
    live: _Live,
    target: OperationState,
    reason: ReasonCode | None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Record that *live* must still reach *target* in the store.

    ``live.work`` becomes the in-memory terminal record; the last durable record
    is kept for the retry. A second failure keeps the first durable record.
    """
    if live.unpersisted is None:
        live.unpersisted = (live.work, target, reason, dict(extra or {}))
    else:
        durable, _old_target, _old_reason, old_extra = live.unpersisted
        live.unpersisted = (durable, target, reason, {**old_extra, **dict(extra or {})})
    live.work = replace(
        live.work,
        state=target,
        reason=reason,
        extra={**live.work.extra, **dict(extra or {})},
    )


def _reachable(durable_state: OperationState, target: OperationState) -> OperationState:
    """*target* when the transition table allows it, else the nearest end that is."""
    if is_legal_transition(durable_state, target):
        return target
    for fallback in (S.FAILED, S.CANCELLED):
        if is_legal_transition(durable_state, fallback):
            return fallback
    return target


async def persist_pending(service: AdmissionService) -> int:
    """Retry every unpersisted terminal write; release what lands. Returns that count."""
    store = service._store
    if store is None:
        return 0
    landed = 0
    for live in list(service._live.values()):
        pending = live.unpersisted
        if pending is None:
            continue
        durable, target, reason, extra = pending
        try:
            if durable.state in TERMINAL:
                written = durable
            else:
                written = await store.advance(
                    durable,
                    _reachable(durable.state, target),
                    reason=reason,
                    runtime=service._stopped_ref(live),
                    extra=extra or None,
                )
        except (StaleRevision, InvalidTransition):
            try:
                current = await store.get(durable.work_id)
            except Exception:
                logger.debug("shared crew: re-read of %s failed", durable.work_id, exc_info=True)
                continue
            if current is None or current.state not in TERMINAL:
                if current is not None:
                    live.unpersisted = (current, target, reason, extra)
                continue
            written = current
        except Exception:
            logger.debug("shared crew: terminal write for %s still failing", durable.work_id)
            continue
        live.unpersisted = None
        live.work = written
        landed += 1
        if live.merged_into:
            service._live.pop(written.work_id, None)
        else:
            service._maybe_release(live)
    return landed


async def confirm_stopped(service: AdmissionService) -> int:
    """Release work whose failed write stopped a child, once that child has ended.

    The slot is held while the stop is still tearing the runtime down, so
    replacement work cannot run beyond the outstanding-child limit.
    """
    released = 0
    for live in list(service._live.values()):
        exec_id = live.stopping_child
        if not exec_id or live.unpersisted is not None:
            continue
        probe = service._probe_for(live)
        if probe is not None:
            try:
                state = await asyncio.to_thread(probe, exec_id)
            except Exception:
                logger.debug("shared crew: stop probe for %s raised", exec_id, exc_info=True)
                continue
            if state is None or state not in TERMINAL:
                continue
        live.stopping_child = ""
        released += 1
        service._maybe_release(live)
    return released


__all__ = ["confirm_stopped", "mark_unpersisted", "persist_pending"]
