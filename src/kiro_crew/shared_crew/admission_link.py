"""Linking an admitted child to its run id before the child can run.

A child spawn's subagent row becomes dispatchable inside the spawn call, before
``attach_child`` records RUNNING. So the run id is minted first and written on
the still-ADMITTED record, the same way ``admission_run_link`` links a task run.
A gateway that stops between the spawn and the attach then finds the execution
on restart and settles the record through the child probe, instead of
cancelling a record whose child the task queue still runs.

The run's own key (``subagent:<id>``) is bound to the admitted budget at the
same moment. A start the subagent queue releases later (a queue drain, an
approval) reads that binding (``containment.run_principal``), so it lands in the
admitted person's slice even before the attach and even with no parent session.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from kiro_crew.shared_crew import admission_run_link as _run_link
from kiro_crew.shared_crew.admission_run_link import HELD_ON_RESTART, UNSTARTED
from kiro_crew.shared_crew.scheduler import WorkClass

if TYPE_CHECKING:
    from kiro_crew.shared_crew.admission import AcceptedWork, OperationKind
    from kiro_crew.shared_crew.admission_gate import Admission, AdmissionService

logger = logging.getLogger(__name__)

#: Seconds between reconcile attempts while recovered subagent dispatch is held.
DISPATCH_RETRY_SECONDS = 30.0


def linked(
    work: AcceptedWork, probes: Mapping[OperationKind, object], work_class: WorkClass
) -> bool:
    """True for an unstarted record whose run may have started.

    That is a child that already names its run id, or a probed run that
    ``admission_run_link`` linked.
    """
    child = (
        work_class is WorkClass.CHILD
        and work.state in UNSTARTED
        and bool(work.extra.get("execution_id"))
    )
    return child or _run_link.linked(work, probes)


async def link(service: AdmissionService, work_id: str, execution_id: str) -> bool:
    """Record *execution_id* on *work_id*'s unstarted record and bind its run key.

    False when the work stopped waiting to start, or the store refused the
    write (the work is then ended fail-closed); the spawn must not go ahead.
    """
    if service._store is None:
        return True
    if not await _run_link.link(service, work_id, execution_id):
        return False
    live = service._live[work_id]
    service.bind_session(
        f"subagent:{execution_id}", live.work.budget_key, live.root_work_id or work_id
    )
    return True


async def link_spawn(admission: Admission, subagents: object) -> str | None:
    """Mint the child's run id and link it to *admission* before the spawn.

    Returns the id to pass to the spawn as ``_preassigned_id``; ``""`` when
    there is nothing to link (admission is off, or the manager mints no ids);
    None when the record could not take it, so the spawn must not start.
    """
    work = admission.work
    if work is None or admission.passthrough or admission.service is None:
        return ""
    service = admission.service
    mint = getattr(subagents, "_mint_agent_id", None)
    if not callable(mint):
        return ""
    run_id = str(mint())
    return run_id if await link(service, work.work_id, run_id) else None


async def restore(service: AdmissionService, work: AcceptedWork, work_class: WorkClass) -> bool:
    """After a restart, bind a held child's run key again and ready its slice.

    The binding lives in memory only. A recovered queue dispatch of the run
    reads it (``containment.run_principal``) to land in the admitted person's
    slice, which a parentless child has no other way to find. Not a child: no-op.

    False only under hard limits when the person's slice was not applied: a
    live admission refuses then (``containment_unavailable``), so the recovered
    dispatch must wait for the slice too. Otherwise the instance slice's
    ceilings still apply and the slice is best effort.
    """
    run_id = str(work.extra.get("execution_id") or "")
    if work_class is not WorkClass.CHILD or not run_id:
        return True
    root = str(work.extra.get("root_work_id") or "") or work.work_id
    service.bind_session(f"subagent:{run_id}", work.budget_key, root)
    unit = ""
    try:
        unit = await service.prepare_principal_slice(work.budget_key)
    except Exception:  # noqa: BLE001 -- judged below, like an unapplied slice
        logger.debug("shared crew: preparing %s's slice failed", work.budget_key, exc_info=True)
    return bool(unit) or not service._limits.hard_limits


async def _recovered(service: AdmissionService) -> list[str]:
    """The surviving linked children not yet held, bound and (hard limits) sliced.

    ``ensure_ready`` logs and skips a row whose reconcile raised (a store write
    lost to a lock) and still marks itself ready, so each such child is
    reconciled again here, under the same lock, before its dispatch may open.
    Empty once every one is recovered.
    """
    from kiro_crew.shared_crew import admission_gate as _gate

    if service._store is None or not service.enabled:
        return []
    rows = await service._store.list(states=set(_gate.S) - _gate.TERMINAL, limit=100000)
    stuck: list[str] = []
    for work in rows:
        if work.operation not in _gate.CHILD_OPERATIONS or not work.extra.get("execution_id"):
            continue
        if work.work_id not in service._live:
            async with service._lock:
                # Re-read under the lock: a tick may have settled the listed row.
                current = await service._store.get(work.work_id)
                if current is None or current.state in _gate.TERMINAL:
                    continue
                if work.work_id not in service._live:
                    try:
                        await service._reconcile_one(current)
                    except Exception:
                        logger.debug(
                            "shared crew: reconciling %s failed", work.work_id, exc_info=True
                        )
                        stuck.append(work.work_id)
                        continue
            live = service._live.get(work.work_id)
            if live is None or live.work.state in _gate.TERMINAL:
                continue  # settled by its reconcile; nothing will dispatch it as this record
            work = live.work
        if not await restore(service, work, WorkClass.CHILD):
            stuck.append(work.work_id)
    return stuck


#: Attempts between repeated warnings while recovered dispatch stays held.
_WARN_EVERY = 20


async def _reconciled(*, attempt: int) -> bool:
    from kiro_crew.shared_crew import admission_gate

    loud = attempt % _WARN_EVERY == 0
    try:
        service = await admission_gate.current_service()
        await service.ensure_ready()
        stuck = await _recovered(service)
    except Exception:
        log = logger.warning if loud else logger.debug
        log("shared crew: admission not reconciled; subagent dispatch held", exc_info=True)
        return False
    if stuck and loud:
        logger.warning(
            "shared crew: subagent dispatch held until these recovered children are"
            " reconciled with their slice: %s",
            ", ".join(stuck),
        )
    return not stuck


async def release_after_reconcile(release: Callable[[], None]) -> asyncio.Task[None] | None:
    """Open recovered subagent dispatch only once fair admission has reconciled.

    Reconciliation binds a recovered child's run key to its person again
    (:func:`restore`), so its dispatch lands in their slice, not the instance
    slice. With admission off it does nothing and *release* runs at once. A
    failed reconcile keeps the queue held, fail-closed like the admission it
    protects, and retries every :data:`DISPATCH_RETRY_SECONDS`; the returned
    task is that retry, which calls *release* once a reconcile succeeds.
    """
    if await _reconciled(attempt=0):
        release()
        return None

    async def _retry() -> None:
        attempt = 0
        while True:
            await asyncio.sleep(DISPATCH_RETRY_SECONDS)
            attempt += 1
            if await _reconciled(attempt=attempt):
                logger.info("shared crew: admission reconciled; subagent dispatch opened")
                release()
                return

    return asyncio.create_task(_retry(), name="shared-crew-dispatch-reconcile")


__all__ = [
    "DISPATCH_RETRY_SECONDS",
    "HELD_ON_RESTART",
    "UNSTARTED",
    "link",
    "link_spawn",
    "linked",
    "release_after_reconcile",
    "restore",
]
