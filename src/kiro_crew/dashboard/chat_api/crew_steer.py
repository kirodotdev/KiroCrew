"""The background ledger record of a person's steer into a Crew Mode worker.

The send path (``chat_send.api_chat``), the side panel's steer route, the queue
drain and a running turn that read a held steer all record the steer through
:func:`record_crew_steer_later`, so the send answers before the board lock and the
crew-log writer let the record land, and the records for one worker land in send
order.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import logger


#: WS frame (owners only) telling the worker's composer that a delivered steer was
#: not recorded on the ledger: ``{"slot": worker, "reason": ledger_error}``. The
#: same reason a synchronous receipt's ``ledger_error`` carries.
CREW_STEER_UNRECORDED_FRAME = "crew_steer_unrecorded"

#: worker slot key -> ``[lock, records owed]``. Every steer record for one worker
#: takes that worker's lock, in the order the sends were accepted: tasks start in
#: creation order and ``asyncio.Lock`` wakes its waiters first-in first-out. The
#: count includes tasks created but not started yet (they are not lock waiters
#: yet), so the entry is dropped only when no record is owed at all.
_STEER_RECORD_LOCKS: dict[str, list[Any]] = {}


def record_crew_steer_later(
    state: Any,
    root: str,
    worker: str,
    message: str,
    *,
    rows: list[dict[str, Any]] | None = None,
) -> None:
    """Record the person's delivered steer on the ledger in the background.

    The record waits on the board lock and the crew-log writer's commit, which a
    busy writer can hold for tens of seconds, so the send answers
    ``ledger_pending`` and never waits for it. Records for one worker land in the
    order this is called. A failure is logged and pushed to the owner's tabs
    (:data:`CREW_STEER_UNRECORDED_FRAME`). Must be called on the event loop, with
    no await between the accepted send and this call.
    """
    held = _STEER_RECORD_LOCKS.get(worker)
    if held is None:
        held = _STEER_RECORD_LOCKS[worker] = [asyncio.Lock(), 0]
    held[1] += 1
    try:
        task = asyncio.get_running_loop().create_task(
            _record_crew_steer(state, root, worker, message, held[0], rows)
        )
    except BaseException:
        _release_steer_record(worker, held)
        raise
    # A done callback, not a ``finally``: it also runs for a task cancelled before
    # its first step, whose body never runs.
    task.add_done_callback(lambda _t: _release_steer_record(worker, held))
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def _release_steer_record(worker: str, held: list[Any]) -> None:
    """One owed record of *worker* finished: drop its lock entry when none is owed.

    No await here, so nothing can queue on the lock between the check and the delete.
    """
    held[1] -= 1
    if held[1] <= 0 and not held[0].locked() and _STEER_RECORD_LOCKS.get(worker) is held:
        del _STEER_RECORD_LOCKS[worker]


async def _record_crew_steer(
    state: Any,
    root: str,
    worker: str,
    message: str,
    lock: asyncio.Lock,
    rows: list[dict[str, Any]] | None,
) -> None:
    """The background record. Never raises into the loop, except cancellation."""
    why: Any = None
    try:
        from kiro_crew.dashboard.handlers import crew_workers

        async with lock:
            recorded = await crew_workers.person_steer(state, root, worker, message, rows=rows)
        why = recorded.get("ledger_error") if isinstance(recorded, dict) else "not_recorded"
        if why:
            logger.info("crew steer: steer into %s (root %s) not recorded: %s", worker, root, why)
    except asyncio.CancelledError:
        logger.debug("crew steer: record into %s cancelled", worker, exc_info=True)
        raise
    except Exception:
        logger.warning("crew steer: the steer into %s was not recorded", worker, exc_info=True)
        why = "not_recorded"
    if why:
        try:
            state.broadcast_ws_owners(
                CREW_STEER_UNRECORDED_FRAME, {"slot": worker, "reason": str(why)}
            )
        except Exception:
            logger.warning("crew steer: could not tell the composer", exc_info=True)
