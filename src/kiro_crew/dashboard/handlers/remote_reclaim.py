"""Reclaim the peer worker a lost create reply leaves behind (remote dispatch M1).

A crew makes a bound worker in this order: slot, mailbox binding, Trust, reply. When
the reply is lost in transit, or the hub dies before it records the bind, the crew
holds a worker (with its Trust) for an item no hub binding names, and refuses the
item ``duplicate_item`` from then on.

So a create first records the crew it is sent to (``RemoteState.create_tried``,
written before the POST, so a hub killed mid-create keeps it) and clears it once the
bind records or the crew certainly made nothing. The patrol pull then asks each such
crew, in the same batch call it reads reports with
(``remote_work_items_pull``'s ``workers``), which open copy it holds per item, and for
each copy of an item whose hub binding is not that worker:

* the item is open and unbound: record the ``bind``, so the hub owns the worker
  again (the conductor seeds it with ``session_send``);
* the item is closed and unbound: record it ``retired`` for closing
  (``item_closed``), so H1's close path ends its Trust and DELETEs it only once its
  crew lists it idle and its copy last reported ``done``; the owner's
  ``session_close(crew=)`` may close it any time. Never a DELETE from here;
* the item is bound to another worker: end the copy's Trust (``PATCH trust:false``)
  and leave it. shortcut: the copy stays until its TTL or the owner closes it;
  recording it for closing needs a retire of a pair that is not the binding.

A create in flight on that item is never reclaimed under it (:data:`_CREATING`), and
a crew that lists no copy clears the mark only once the create has had
:data:`CREATE_GRACE_SECS` to land, so a hub that restarted mid-create keeps it.
The reclaim runs after the pull filed its reports, within its own
:data:`RECLAIM_BUDGET_SECS`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers.work_ledger import _board_lock, _drain_before_cancelling

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: ``(board, item id)`` -> creates of it between their tried mark and their bind
#: (or failure). The pull never reclaims a copy of such an item: the create's own
#: reply may still bind it, and its failed bind closes it.
_CREATING: dict[tuple[str, str], int] = {}

#: Seconds after a create was marked before a crew that lists no copy of the item
#: clears the mark. A create can still be landing on the peer (its POST in flight,
#: the slot being prepared before the bind) when a hub that restarted mid-create
#: reads the crew, and in-memory :data:`_CREATING` is gone then; absence proves
#: nothing until every create sent before it has ended.
#: shortcut: a fixed bound well past the tool's 30 s wait and the peer's create;
#: tie it to a peer-reported create deadline if one is ever exposed.
CREATE_GRACE_SECS = 15 * 60.0

#: Seconds the reclaim may take after a pull filed its reports. The pull's own
#: deadline (``remote_ingest.READ_DEADLINE_SECS``) is spent by then; this keeps
#: the whole read inside the 10 s ``work_ledger_read`` and patrol budgets.
RECLAIM_BUDGET_SECS = 3.0

#: ``(board, crew)`` -> the item a reclaim on that crew last started on. The next
#: sweep starts after it, so a copy that keeps outrunning the budget never keeps
#: the ones after it from being reached. In memory only, like the close backoff.
_RECLAIM_NEXT: dict[tuple[str, str], str] = {}

#: board -> the crew a reclaim last started on, the same rotation across crews, so
#: one slow crew never keeps the crews after it from being reached.
_RECLAIM_NEXT_CREW: dict[str, str] = {}


def _stored(board: str, item_id: str) -> work_ledger.RemoteState:
    return work_ledger.read_remote_state(board, item_id) or work_ledger.RemoteState()


#: The refusal for a create on one more crew while every mark slot is held.
CODE_RECLAIM_PENDING = "crew_reclaim_pending"


async def _set_tried(
    board: str, item_id: str, crew: str, *, on: bool, guarded: bool = False
) -> bool | None:
    """Add or drop *crew* in *item_id*'s ``create_tried``; whether the record stored.

    Adding answers ``None`` when *crew* is new and the list is full: a mark is never
    dropped to make room, since it is the only record of a worker to reclaim. The
    read, change and write run as one drained step under the board lock, so a
    cancelled caller never releases the lock while its write is still landing."""
    return await _drain_before_cancelling(
        _set_tried_locked(board, item_id, crew, on=on, guarded=guarded)
    )


async def _set_tried_locked(
    board: str, item_id: str, crew: str, *, on: bool, guarded: bool
) -> bool | None:
    async with _board_lock(board):
        if guarded and (board, item_id) in _CREATING:
            # A create of the item began since the caller looked: its mark is live.
            return True
        record = await asyncio.to_thread(_stored, board, item_id)
        crews = [c for c in record.create_tried if c != crew]
        if on and crew not in record.create_tried and len(crews) >= work_ledger.MAX_CREATE_TRIED:
            return None
        if on:
            crews.append(crew)
            record.create_tried_at = work_ledger._now_iso()
        elif crews == record.create_tried:
            return True
        record.create_tried = crews[-work_ledger.MAX_CREATE_TRIED :]
        if not record.create_tried:
            record.create_tried_at = None
        try:
            await asyncio.to_thread(work_ledger.write_remote_state, board, item_id, record)
        except (work_ledger.WorkLedgerError, OSError):
            logger.warning("create mark write failed for %s", item_id, exc_info=True)
            return False
    return True


async def clear_tried(board: str, item_id: str, crew: str, *, guarded: bool = False) -> None:
    """Drop *crew* from *item_id*'s ``create_tried``. Never raises.

    *guarded* (every clear but a create's own): checked again under the board lock,
    so a create of the item that began after the caller looked keeps its mark."""
    try:
        await _set_tried(board, item_id, crew, on=False, guarded=guarded)
    except Exception:  # noqa: BLE001 - a stale mark costs one more roster read
        logger.warning("create mark clear failed for %s", item_id, exc_info=True)


class CreateMark:
    """One create's mark. ``settle()`` only on an answer that says what the crew
    holds for the item, whatever any other create did: the bind recorded (the crew
    admits a bind only when it holds no other copy), or ``crew_peer_lacks_work_items``
    (the crew binds no work item at all). A refusal (busy, a 4xx, a failed bind, a
    lost reply) says nothing of another create on the same crew, so it never clears
    the mark; the reclaim does, once it finds the copy or its absence."""

    def __init__(self) -> None:
        self.clear = False

    def settle(self) -> None:
        self.clear = True


@asynccontextmanager
async def creating(board: str, item_id: str, crew: str) -> AsyncIterator[CreateMark]:
    """Hold a create of *item_id* on *crew*: marked tried before, cleared after when it
    settled and no other create of the item is still open. Raises the create refusal
    when the mark cannot store."""
    from kiro_crew.dashboard.handlers import remote_dispatch_routes as routes

    key = (board, item_id)
    _CREATING[key] = _CREATING.get(key, 0) + 1
    mark = CreateMark()
    try:
        stored = await _set_tried(board, item_id, crew, on=True)
        if stored is None:
            raise routes.CrewCallError(
                409,
                CODE_RECLAIM_PENDING,
                f"item {item_id} has unresolved creates on {work_ledger.MAX_CREATE_TRIED} "
                "crews; the next work_ledger_read reclaims or clears them, then retry",
            )
        if not stored:
            raise routes.CrewCallError(
                503, "ledger_write_failed", "could not record the create; try again"
            )
        yield mark
    finally:
        left = _CREATING.get(key, 1) - 1
        if left > 0:
            _CREATING[key] = left
        else:
            _CREATING.pop(key, None)
        # Only the last create of the item to end may clear: another one still open
        # shares the mark and may yet lose its reply.
        if mark.clear and left <= 0:
            await clear_tried(board, item_id, crew)


async def _bound_here(board: str, item_id: str, crew: str) -> bool:
    """Whether *item_id* is bound on *crew* now (so its mark waits on no create)."""
    from kiro_crew.dashboard.handlers import remote_ingest

    item = await remote_ingest.settled_item(board, item_id)
    return item is not None and item.worker_instance_id == crew


def _held(item: work_ledger.WorkItem, crew: str, key: str) -> bool:
    """Whether *item* already holds *key* on *crew*, bound or retired."""
    if item.worker_instance_id == crew and item.worker_session_key == key:
        return True
    return any(
        b["worker_instance_id"] == crew and b["worker_session_key"] == key
        for b in item.retired_bindings
    )


def _past_grace(board: str, item_id: str) -> bool:
    """Whether every create marked on *item_id* has had :data:`CREATE_GRACE_SECS`
    to land. A missing or unreadable stamp has not. BLOCKING."""
    from datetime import datetime, timezone

    stamp = _stored(board, item_id).create_tried_at
    moment = work_ledger.parse_stamp(stamp) if stamp else None
    if moment is None:
        return False
    return (datetime.now(timezone.utc) - moment).total_seconds() >= CREATE_GRACE_SECS


async def reclaim_all(
    state: "DashboardState",
    board: str,
    batches: dict[str, Any],
    tried: dict[str, list[str]],
) -> None:
    """Reclaim on every crew a pull's batch read answered (or said it serves no batch
    route, which clears that crew's marks), within
    :data:`RECLAIM_BUDGET_SECS`. Never raises. Each write it makes runs to the end
    (drained), so the cut-off only leaves the rest for the next pull."""
    try:
        await asyncio.wait_for(_reclaim_each(state, board, batches, tried), RECLAIM_BUDGET_SECS)
    except Exception:  # noqa: BLE001 - a timeout or fault leaves the marks for next time
        logger.info("remote reclaim stopped early", exc_info=True)


async def _reclaim_each(
    state: "DashboardState", board: str, batches: dict[str, Any], tried: dict[str, list[str]]
) -> None:
    order = sorted(batches)
    last = _RECLAIM_NEXT_CREW.get(board)
    start = next((n + 1 for n, c in enumerate(order) if last is not None and c == last), 0)
    for crew in order[start:] + order[:start]:
        # Recorded before the await, so a sweep cut off on this crew starts after it.
        _RECLAIM_NEXT_CREW[board] = crew
        batch = batches[crew]
        if batch.unsupported:
            # A crew that serves no batch route lists no copies, so nothing can be
            # reclaimed from it here: its marks only kept the board pulled.
            for item_id, crews in tried.items():
                if crew in crews:
                    await clear_tried(board, item_id, crew, guarded=True)
            continue
        await reclaim_crew(state, board, crew, batch.workers, tried, complete=batch.complete)


async def reclaim_crew(
    state: "DashboardState",
    board: str,
    crew: str,
    workers: list[tuple[str, str]],
    tried: dict[str, list[str]],
    *,
    complete: bool = True,
) -> None:
    """Reclaim every copy *crew* listed in *workers* that no binding names. Never raises.

    *tried* is the board's ``create_tried`` (:func:`work_ledger.creates_to_reclaim`);
    each of its items that named *crew* has the mark cleared once this crew's list
    is handled, the copy reclaimed or none found. A list cut at the crew's cap
    (*complete* False) proves no copy absent, so it clears no mark on absence, and
    neither does a full list inside the mark's grace (:data:`CREATE_GRACE_SECS`).
    """
    listed = dict(workers)
    order = sorted(set(listed) | {i for i, crews in tried.items() if crew in crews})
    last = _RECLAIM_NEXT.get((board, crew))
    start = next((n + 1 for n, i in enumerate(order) if last is not None and i == last), 0)
    for item_id in order[start:] + order[:start]:
        if (board, item_id) in _CREATING:
            continue
        # Recorded before the await, so a sweep cut off on this copy starts after it.
        _RECLAIM_NEXT[(board, crew)] = item_id
        try:
            key = listed.get(item_id)
            if key is None and not complete:
                continue
            marked = crew in tried.get(item_id, ())
            # A marked crew that lists no copy, for an item not bound there, proves
            # absence only past the grace; that is checked again under the lock.
            absent = key is None and marked and not await _bound_here(board, item_id, crew)
            if absent and not await asyncio.to_thread(_past_grace, board, item_id):
                continue
            done = await _reclaim_one(state, board, crew, item_id, key, marked=marked)
        except Exception:  # noqa: BLE001 - one copy must never stop the rest
            logger.warning("remote reclaim failed for %s", item_id, exc_info=True)
            continue
        if done and marked:
            await _clear_reclaimed(board, item_id, crew, absent=absent)


async def _clear_reclaimed(board: str, item_id: str, crew: str, *, absent: bool) -> None:
    """Clear a reclaimed mark, guarded under the board lock; a clear on absence also
    needs the grace to have passed at that moment."""

    async def _locked() -> None:
        async with _board_lock(board):
            if (board, item_id) in _CREATING:
                return
            if absent and not await asyncio.to_thread(_past_grace, board, item_id):
                return
            record = await asyncio.to_thread(_stored, board, item_id)
            if crew not in record.create_tried:
                return
            record.create_tried = [c for c in record.create_tried if c != crew]
            if not record.create_tried:
                record.create_tried_at = None
            await asyncio.to_thread(work_ledger.write_remote_state, board, item_id, record)

    try:
        await _drain_before_cancelling(_locked())
    except Exception:  # noqa: BLE001 - a stale mark costs one more roster read
        logger.warning("create mark clear failed for %s", item_id, exc_info=True)


async def _reclaim_one(
    state: "DashboardState",
    board: str,
    crew: str,
    item_id: str,
    key: str | None,
    *,
    marked: bool = True,
) -> bool:
    """Reclaim one copy. Whether this item is handled for *crew* (its mark may go).

    Only a crew marked for the item (*marked*) may have its copy bound or retired:
    a copy the hub never sent a create for gets at most its Trust ended."""
    from kiro_crew.dashboard.handlers import remote_ingest

    if key is None:
        # Not listed: the crew holds no open copy of it, or holds the item's own
        # bound worker (asked for by name, so never listed). A worker this reclaim
        # bound keeps its mark until it holds the hub's current decision.
        item = await remote_ingest.settled_item(board, item_id)
        if item is not None and item.worker_instance_id == crew and not item.is_terminal:
            return await _forwarded(state, board, item_id)
        return True
    if not remote_ingest._routes()._slot_ok(key):
        return True
    item = await remote_ingest.settled_item(board, item_id)
    if item is None:
        # Unknown here or unreadable: a dirty board answers None too, so keep the mark.
        return False
    if _held(item, crew, key):
        return True
    mgr = getattr(state, "instances_manager", None)
    if marked and not item.worker_session_key:
        reply = await _record(state, board, item, crew, key)
        if reply is None or reply.status != 200:
            if item.is_terminal and mgr is not None:
                # Not recorded (a full retired history, say): the closed item's
                # orphan still loses its Trust now; the mark stays for a retry.
                await remote_ingest.revoke_peer_trust(mgr, crew, key)
            return False
        if not item.is_terminal:
            # A decide recorded while the item was unbound reached no copy.
            return await _forwarded(state, board, item_id)
        if item.is_terminal:
            retired = await remote_ingest.settled_item(board, item_id)
            if retired is not None:
                # H1's close: trust off now, DELETE only when idle and done.
                remote_ingest._schedule_closes(state, board, [retired])
        logger.info("reclaimed crew worker %s for %s", key, item_id)
        return True
    if mgr is not None and await remote_ingest.revoke_peer_trust(mgr, crew, key):
        return True
    return False


async def _forwarded(state: "DashboardState", board: str, item_id: str) -> bool:
    """Send the item's settled decision to its copy; whether the copy took it."""
    from kiro_crew.dashboard.handlers import remote_ingest

    outcome = await remote_ingest.forward_decide(state, board, item_id)
    return bool(outcome.get("ok"))


async def _record(
    state: "DashboardState",
    board: str,
    item: work_ledger.WorkItem,
    crew: str,
    key: str,
) -> Any:
    """Record the bind (open item) or the close-retire (closed item) of *key* on *crew*."""
    from kiro_crew.dashboard.handlers import remote_ingest
    from kiro_crew.dashboard.handlers.work_ledger import _record_conductor_write

    unit, unit_error = await remote_ingest._board_unit(state, board)
    if unit_error is not None or not unit:
        return None
    if item.is_terminal:
        action, extra, operation = (
            "retire",
            {"reason": remote_ingest.CLOSE_BY_WAKE},
            "remote_dispatch.reclaim_close",
        )
    else:
        action, extra, operation = "bind", {}, "remote_dispatch.reclaim_bind"
    return await remote_ingest._drain_before_cancelling(
        _record_conductor_write(
            state,
            board,
            unit,
            action,
            {
                "action": action,
                "item_id": item.item_id,
                "worker_session_key": key,
                "worker_instance_id": crew,
                **extra,
            },
            operation=operation,
        )
    )


__all__ = ["CreateMark", "clear_tried", "creating", "reclaim_all", "reclaim_crew"]
