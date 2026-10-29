"""Stop a removed person's work: queued work ends, running work is cancelled now.

:func:`stop_principals` walks the admission service's live work and cancels each
non-terminal root item charged to one of the given principals, through
:meth:`AdmissionService.cancel` with :attr:`ReasonCode.ACCESS_REMOVED`, the same
path the work list's Cancel button takes:

* queued or admitted work ends at once as ``cancelled`` and never starts;
* running work moves to ``cancelling`` and its turn is cancelled. The slot is
  released only when the runtime has stopped and the work settles, never here.

Work that already finished is not touched. A merged queued message follows the
turn it was merged into, which may be someone else's, so it is left alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.shared_crew.admission import TERMINAL, InvalidTransition
from kiro_crew.shared_crew.admission import OperationState as S
from kiro_crew.shared_crew.admission import ReasonCode, StaleRevision

if TYPE_CHECKING:
    from kiro_crew.shared_crew.admission_gate import AdmissionService

logger = logging.getLogger(__name__)

_SKIP = frozenset({S.CANCELLING, S.RECONCILING}) | TERMINAL


async def stop_principals(
    service: AdmissionService, principal_ids: frozenset[str]
) -> dict[str, int]:
    """Cancel every live item charged to *principal_ids*; ``{"queued": n, "running": n}``."""
    counts = {"queued": 0, "running": 0}
    if not principal_ids:
        return counts
    for work_id, live in list(service._live.items()):
        work = live.work
        if work.budget_key not in principal_ids or work.state in _SKIP or live.merged_into:
            continue
        was_running = work.state is S.RUNNING
        for _attempt in range(2):
            current = service.live_work(work_id)
            if current is None or current.state in _SKIP:
                break
            try:
                await service.cancel(
                    work_id, expected_revision=current.revision, reason=ReasonCode.ACCESS_REMOVED
                )
            except StaleRevision:
                continue
            except (InvalidTransition, KeyError):
                break
            counts["running" if was_running else "queued"] += 1
            break
    return counts


__all__ = ["stop_principals"]
