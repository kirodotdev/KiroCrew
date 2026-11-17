"""A person's steer into a Crew Mode worker, from the send that carries it to the
ledger event that records it.

The send path asks :func:`_person_crew_worker_root` whether the dashboard owner typed
into a worker and stamps the request (``_CREW_PERSON_STEER``); a queued steer carries
its marker on the queue entry (:func:`mark_crew_steer`), and one written into a
running turn is held (:func:`hold_crew_steer`) until the turn reads it, requeues it
or drops it.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import logger


#: Request key the send path stamps when the person typed into a Crew Mode worker:
#: ``(root, worker, message)``, read by ``chat_send.api_chat`` once the send succeeded.
_CREW_PERSON_STEER = "kirocrew_crew_person_steer"

#: Queue-entry key naming ``(root, worker)`` on a person's queued send into a
#: Crew Mode worker; the drain records the steer when it delivers that entry.
CREW_STEER_QUEUE_KEY = "_crew_person_steer"


def mark_crew_steer(entry: dict[str, Any], root: str, worker: str) -> None:
    """Mark queue *entry* as the person's steer into *worker* of Crew Mode *root*.

    In memory only, like a held steer: an entry a restart restores comes off a
    writable file, so it is still delivered but never recorded as the person's.
    """
    entry[CREW_STEER_QUEUE_KEY] = (root, worker)


#: A person's steer written into a crew member's running turn and not yet read by
#: it: slot key -> steer text -> ``(root, worker)``. Recorded on the ledger when the
#: turn reads it (:func:`crew_steer_consumed`), moved onto the queue entry when the
#: turn's end requeues it (:func:`crew_steer_requeued`), and dropped when it is taken
#: back or discarded (:func:`crew_steer_discarded`), so a withdrawn steer is never
#: on the ledger. In memory only: a restart loses the record, never adds one.
_HELD_CREW_STEERS: dict[str, dict[str, tuple[str, str]]] = {}


def hold_crew_steer(slot_key: str, message: str, root: str, worker: str) -> bool:
    """Hold *message*'s record until the running turn reads it (see above).

    ``True`` only when this call made the hold. An earlier unread steer of the same
    text keeps its hold, and the caller must not drop it when its own attempt fails
    (the duplicate is refused, but the first one is still read and recorded).
    """
    held = _HELD_CREW_STEERS.setdefault(slot_key, {})
    if message in held:
        return False
    held[message] = (root, worker)
    return True


def _take_held(slot_key: str, message: str) -> tuple[str, str] | None:
    held = _HELD_CREW_STEERS.get(slot_key)
    if not held:
        return None
    marker = held.pop(message, None)
    if not held:
        _HELD_CREW_STEERS.pop(slot_key, None)
    return marker


def crew_steer_consumed(state: Any, slot: Any, message: str) -> None:
    """The running turn read *message*: record the held steer, off the turn's path."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is None:
        return
    try:
        record_crew_steer_later(state, marker[0], marker[1], message)
    except Exception:
        logger.warning("crew steer: a read steer was not recorded", exc_info=True)


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


def crew_steer_requeued(slot: Any, message: str, entry: dict[str, Any] | None) -> None:
    """*message* went back into the queue: the drain records it when it delivers it."""
    marker = _take_held(getattr(slot, "key", ""), message)
    if marker is not None and entry is not None:
        mark_crew_steer(entry, *marker)


def crew_steer_discarded(slot: Any, message: str) -> None:
    """*message* was taken back or thrown away unread: it is never recorded."""
    _take_held(getattr(slot, "key", ""), message)


def _person_crew_worker_root(request: web.Request, state: Any, slot: Any, app: str) -> str:
    """The Crew Mode root *slot* works for when the dashboard owner typed into it, else ``""``.

    Only the person's own send counts: no app token, the owner's dashboard request,
    and a slot the slot list stamps as some root's worker. ``""`` for every send
    while ``crew_mode.APPROVAL_ROUTING_LIVE`` is off (the Crew panel part turns it
    on), so a send typed into a crew member's own composer is an ordinary send until
    then. No I/O.
    """
    if app:
        return ""
    try:
        from kiro_crew import crew_mode

        if not crew_mode.APPROVAL_ROUTING_LIVE:
            return ""
        from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

        if not is_owner_dashboard_request(request):
            return ""
        from kiro_crew.dashboard.session_control_parts.crew_filing import _frame_parents

        # The lineage the slot frame reads: a session its conductor released is a
        # plain session again, and an adopted one belongs to its new conductor.
        crew_root, _is_root = crew_mode.live_stamps(
            state._slots, _frame_parents(state), settled=crew_mode.lineage_is_settled()
        ).get(slot.key, ("", False))
        return crew_root
    except Exception:
        logger.debug("crew steer: could not tell whether this send is to a worker", exc_info=True)
        return ""
