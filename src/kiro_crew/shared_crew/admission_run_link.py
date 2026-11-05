"""Linking an admitted background run to its run id before the run can start.

A task run is registered and persisted by ``task_runner.start_background``
(or set ``running`` by a retry or a resume) before the handler's
``attach_child`` records RUNNING. The task runner resumes a run it finds
``running`` after a gateway stop, so a stop in that window must not leave an
ADMITTED record that reconciliation proves "never started" and cancels.

So the run id is chosen first and written on the still-ADMITTED record. On
restart a linked record of an operation with a registered probe is held and
settled by that probe, like a RUNNING one, instead of being cancelled while the
task runner runs the work.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import TYPE_CHECKING

from kiro_crew.shared_crew.admission import (
    InvalidTransition,
    OperationKind,
    OperationState,
    StaleRevision,
)

if TYPE_CHECKING:
    from kiro_crew.shared_crew.admission import AcceptedWork
    from kiro_crew.shared_crew.admission_gate import Admission, AdmissionService

S = OperationState

#: States a record is in before its start attaches.
UNSTARTED = (S.QUEUED, S.ADMITTED)
#: Non-terminal states reconciliation holds and resolves through runtime proof:
#: a started record, or a linked one whose run may have started.
HELD_ON_RESTART = (S.RUNNING, S.CANCELLING, *UNSTARTED)


def linked(work: AcceptedWork, probes: Mapping[OperationKind, object]) -> bool:
    """True for an unstarted record that names its run id and whose run can be probed."""
    return (
        work.state in UNSTARTED
        and work.operation in probes
        and bool(work.extra.get("execution_id"))
    )


async def link(service: AdmissionService, work_id: str, execution_id: str) -> bool:
    """Record *execution_id* on *work_id*'s unstarted record.

    False when the work stopped waiting to start, or the store refused the
    write (the work is then ended fail-closed); the run must not start.
    """
    store = service._store
    if store is None:
        return True
    live = service._live.get(work_id)
    if live is None or live.work.state not in UNSTARTED:
        return False
    try:
        live.work = await store.annotate(live.work, {"execution_id": execution_id})
    except (StaleRevision, InvalidTransition):
        return False
    except asyncio.CancelledError:
        # The write may still land on the store's thread: the ticker retries
        # the terminal write against whatever revision it finds.
        service._fail_closed(live, "link")
        raise
    except Exception:
        service._fail_closed(live, "link")
        return False
    if live.cancel_reason is not None and live.work.state in UNSTARTED:
        # A cancel raced the write and lost its compare-and-set to it.
        try:
            await service._end(live, S.CANCELLED, reason=live.cancel_reason)
        except asyncio.CancelledError:
            service._fail_closed(live, "link")
            raise
    return live.work.state in UNSTARTED and work_id in service._live


async def link_run(admission: Admission, run_id: str) -> bool:
    """Link *run_id* to *admission* before its run starts.

    True when there is nothing to link (admission is off); False when the
    record could not take the id, so the run must not start.
    """
    work = admission.work
    if work is None or admission.passthrough or admission.service is None:
        return True
    return await link(admission.service, work.work_id, run_id)


__all__ = ["HELD_ON_RESTART", "UNSTARTED", "link", "link_run", "linked"]
