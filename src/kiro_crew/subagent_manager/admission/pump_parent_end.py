"""How the pump's claimer ends a claimed row whose parent conversation ended.

A parent-end teardown refuses to cancel a row that is claimed and not started, and
leaves it to its claimer (``claim_and_start``). These are the claimer's half: the
teardown mark and the run folder's ``parent_end`` tombstone that say the parent ended,
the generation-fenced cancel that ends the row instead of starting it, and the tombstone
a claimer writes when that cancel cannot reach the store.

A function here whose first parameter is ``self`` is a ``_PumpMixin`` method: the class
binds it under the same name in ``admission/pump.py``. None is an ``*_impl``, so each
runs on this module's own globals and imports what it uses inside its body.

The manager fields this module uses. Writes: none. Reads: ``_teardown_cancelled_ids``.
"""

from __future__ import annotations

import logging as _logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq

    from .pump import _PumpMixin
    from .types import ClaimPoint

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


def _parent_ended(self: _PumpMixin, agent_id: str) -> bool:
    """Whether a parent-end teardown has marked *agent_id*.

    The teardown marks every id it selects before its first await and then
    refuses to cancel a row that is claimed but not started, leaving it to
    the claimer. This is the claimer's half: a marked row is not started.
    """
    return agent_id in getattr(self._manager, "_teardown_cancelled_ids", ())


async def _parent_end_recorded(self: _PumpMixin, agent_id: str) -> bool:
    """Whether *agent_id*'s run folder holds a ``parent_end`` tombstone.

    Read off the loop. An unreadable folder answers False, which leaves the
    teardown mark as the only record, as it is without the tombstone.
    """
    import asyncio as _asyncio

    from kiro_crew.subagent_persistence import read_tombstone

    try:
        tombstone = await _asyncio.to_thread(read_tombstone, agent_id)
    except Exception:
        _glue_logger.debug("taskq: reading the tombstone of %s failed", agent_id, exc_info=True)
        return False
    return isinstance(tombstone, dict) and tombstone.get("cause") == "parent_end"


async def _record_parent_end(self: _PumpMixin, point: ClaimPoint) -> None:
    """Record on the run folder that a retained claim's parent conversation ended.

    Written after the claim is retained. The boot reconcile reads it and
    cancels the row, and the retained-claim retry reads it and cancels instead
    of starting, whether or not the teardown mark is still held.
    """
    from kiro_crew.subagent_manager.cancellation import record_parent_end

    await record_parent_end(
        point.agent_id,
        point.parent_session_key,
        "parent conversation ended; the claimer's cancel could not reach the store",
    )


async def _cancel_torn_down_claim(
    self: _PumpMixin, store: "_taskq.TaskStore", agent_id: str, generation: int
) -> bool:
    """Cancel the claimed row of an ended parent under the claim's generation.

    ``store.cancel`` keeps the generation fence, so a row that moved under
    another owner is left alone. Returns False only when the store could not
    be reached. The caller then retains the claim and records the parent end
    on the run folder (:meth:`_record_parent_end`): left ``admitted`` or
    ``starting`` with only the in-memory teardown mark, the boot reconcile
    would requeue or recover the row and start it, and a retry after the mark
    aged out would start it too.
    """
    import asyncio as _asyncio

    from kiro_crew import taskq as _taskq

    # Shielded and awaited to its answer, like the claim and the start write:
    # a claimer cancelled here must not leave a taken start nothing ends.
    job = _asyncio.ensure_future(
        store.run(
            store.cancel,
            agent_id,
            reason="parent_end",
            only_from=frozenset({_taskq.ADMITTED, _taskq.STARTING}),
            generation=generation,
        )
    )
    try:
        await _asyncio.shield(job)
    except _asyncio.CancelledError:
        try:
            await job
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning(
                "taskq: cancelled parent-end cancel of %s failed", agent_id, exc_info=True
            )
        raise
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning(
            "taskq: cancelling the claimed row of %s after its parent ended failed; "
            "retaining generation %d",
            agent_id,
            generation,
            exc_info=True,
        )
        return False
    _glue_logger.info("taskq: %s not started -- its parent conversation ended", agent_id)
    return True
