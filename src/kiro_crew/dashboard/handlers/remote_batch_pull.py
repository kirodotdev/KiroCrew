"""The hub's half of the batch pull: one call per crew per patrol tick.

:func:`pull_crew` asks one crew, in one ``POST api/chat/work-items/pull``, for
every item *board* has bound there (``remote_work_items_pull``). Per item it sends
the cursor the crew log already holds: the peer event ids filed on that binding
(``remote_ingest_peer.read_folded_peer``) and the binding's newest filed peer stamp.
Nothing new is stored for it. The reply says, per item, the slot's booleans and,
only when there is news, the copy and its events from the hub's cursor on. Under
``held`` it names each live worker it holds for the board that the pull did not
name, which ``remote_reclaim`` binds or closes; the pull lists the slots it is
already closing as ``held_skip``, so successive pulls reach every such worker.

A crew that does not serve the route (an older version, a plain 404) or answers
anything this module cannot read gives ``None``, and the pull reads that crew's
items one by one as before (``remote_ingest._ingest_item``).
shortcut: an older crew is asked again on every tick (one extra 404); remember the
answer per crew if that ever matters.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest_peer as peer_ingest
from kiro_crew.dashboard.handlers import remote_work_items_pull

logger = logging.getLogger(__name__)

#: The peer route.
BATCH_PATH = "api/chat/work-items/pull"

#: Filed ids sent per item: the peer route's own cap.
MAX_AFTER_IDS = remote_work_items_pull.MAX_AFTER_IDS

_FLAGS = ("live", "running", "pending_approval", "trust_active", "found", "more")


@dataclass(frozen=True)
class CrewBatch:
    """One crew's batch reply, checked."""

    #: item id -> ``{live, running, pending_approval, trust_active, found, more}``
    #: plus ``change`` (``{work_item, events}``) when there is news.
    rows: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: item id -> ``{slot, closed}`` for each live worker the crew holds for this
    #: board that the pull did not name (``remote_reclaim``); ``closed`` when the
    #: crew's copy of the item is closed.
    held: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Held workers past :data:`MAX_HELD_ITEMS` this reply did not carry or this
    #: hub did not keep: a later pull reclaims them.
    held_omitted: int = 0


#: Most held rows kept from one reply: the peer route's own cap.
MAX_HELD_ITEMS = remote_work_items_pull.MAX_HELD_ITEMS


def _parse_held(raw: Any, omitted: Any) -> tuple[dict[str, dict[str, Any]], int]:
    """``(held rows, omitted)``: the reply's ``held`` rows that name a usable item id
    and slot, at most :data:`MAX_HELD_ITEMS`, and how many workers beyond them the
    crew holds (its ``held_omitted`` plus the rows over the cap). ``({}, 0)`` from a
    crew that predates them."""
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest

    held: dict[str, dict[str, Any]] = {}
    over = omitted if type(omitted) is int and 0 < omitted <= 2**31 else 0
    if not isinstance(raw, dict):
        return held, over
    for item_id, row in raw.items():
        if len(held) >= MAX_HELD_ITEMS:
            over += 1
            continue
        try:
            work_ledger._require_item_id(item_id)
        except work_ledger.WorkLedgerError:
            continue
        slot = row.get("slot") if isinstance(row, dict) else None
        if remote_ingest._routes()._slot_ok(slot):
            # ``closed``: the crew's copy is closed, so the worker is only ever closed.
            held[item_id] = {"slot": slot, "closed": row.get("closed") is True}
    return held, over


#: Most slots sent as ``held_skip``: the peer route's own cap.
MAX_HELD_SKIP = remote_work_items_pull.MAX_HELD_SKIP

#: ``(crew, slot) -> board`` whose close last ran for a key of the close backoff
#: (``remote_ingest._CLOSE_RETRY``), so a pull's ``held_skip`` names only its own
#: board's closes. In memory, like that backoff; a close that ends drops its key.
CLOSE_BOARD: dict[tuple[str, str], str] = {}


def held_skip(crew: str, board: str) -> list[str]:
    """The slots on *crew* whose close for *board* is in flight or backing off on
    this hub, which the crew leaves out of ``held`` so its cap reaches the others.

    Read from the close backoff this process already keeps, so nothing is stored
    for it; a hub restart sends none and the first pull starts over. A close that
    ends drops its key, so each slot named is one the crew may still hold live,
    and :data:`MAX_HELD_SKIP` is the crew's live-slot ceiling: none is cut off.
    """
    # circular import: remote_ingest imports this module at load
    from kiro_crew.dashboard.handlers import remote_ingest

    slots = sorted(
        key[1]
        for key in list(remote_ingest._CLOSE_RETRY)
        if key[0] == crew and CLOSE_BOARD.get(key) == board and not remote_ingest._close_due(key)
    )
    return slots[:MAX_HELD_SKIP]


async def _hint(board: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    """The cursor sent for *item*: its slot, filed peer ids and floor stamp."""
    from kiro_crew.dashboard.handlers import remote_ingest

    hint: dict[str, Any] = {"slot": str(item.worker_session_key)}
    try:
        ids: tuple[str, ...] = (
            await asyncio.to_thread(peer_ingest.read_folded_peer, board, item.item_id)
        ).ids
    except Exception:  # noqa: BLE001 - no ids: the crew sends its whole log, and
        # the filing reads the fold again and says why it cannot file
        ids = ()
    if ids:
        hint["after"] = list(ids[-MAX_AFTER_IDS:])
    record = await asyncio.to_thread(remote_ingest._stored, board, item.item_id)
    hint["stamp"] = remote_ingest._report_floor(record, item)
    hint["digest"] = peer_ingest.worker_digest(
        {
            "status": item.status,
            "summary": item.summary,
            "reason": item.reason,
            "artifacts": item.artifacts,
            "pr": item.pr,
        }
    )
    return hint


def _parse(payload: Any, named: set[str]) -> CrewBatch | None:
    """*payload* as a :class:`CrewBatch`, or ``None`` when it is not a batch reply."""
    if not isinstance(payload, dict):
        return None
    items = payload.get("items")
    if not isinstance(items, dict):
        return None
    rows: dict[str, dict[str, Any]] = {}
    for item_id, raw in items.items():
        if item_id not in named or not isinstance(raw, dict):
            continue
        row: dict[str, Any] = {name: raw.get(name) is True for name in _FLAGS}
        change = raw.get("change")
        if row["found"] and isinstance(change, dict):
            if not isinstance(change.get("work_item"), dict) or not isinstance(
                change.get("events"), list
            ):
                continue
            row["change"] = {"work_item": change["work_item"], "events": change["events"]}
        rows[item_id] = row
    held, omitted = _parse_held(payload.get("held"), payload.get("held_omitted"))
    return CrewBatch(rows, held, omitted)


async def pull_crew(
    mgr: Any, board: str, crew: str, items: list[work_ledger.WorkItem]
) -> CrewBatch | None:
    """One batch read of *items*, all bound to *crew*.

    ``None`` when the crew does not serve the route or the reply is unreadable; a
    tunnel or proxy failure raises ``CrewCallError`` as ``peer_call`` does.
    """
    from kiro_crew.dashboard.handlers import remote_ingest

    routes = remote_ingest._routes()
    hints = {item.item_id: await _hint(board, item) for item in items}
    body: dict[str, Any] = {"conductor_key": board, "items": hints}
    skip = held_skip(crew, board)
    if skip:
        # Sent only when set; a crew that predates it reads no other key either.
        body["held_skip"] = skip
    status, payload = await routes.peer_call(mgr, crew, "POST", BATCH_PATH, body=body)
    if not 200 <= status < 300:
        return None
    return _parse(payload, set(hints))


def payload_of(row: dict[str, Any]) -> dict[str, Any] | None:
    """The per-slot read's payload for a row with news, else ``None``."""
    change = row.get("change")
    if not isinstance(change, dict):
        return None
    return {**change, "trust_active": row["trust_active"]}


def listed_of(slot: str, row: dict[str, Any]) -> dict[str, dict[str, bool]]:
    """The slot list's answer for *slot* alone, as ``remote_ingest._slot_list`` shapes it."""
    if not row["live"]:
        return {}
    return {
        slot: {
            "running": row["running"],
            "pending_approval": row["pending_approval"],
            "idle": not row["running"] and not row["pending_approval"],
        }
    }


#: A batch row with no news: nothing to file, every answer still fresh.
UNCHANGED: dict[str, Any] = {}


async def _crew_ready(state: Any, mgr: Any, board: str, crew: str) -> bool:
    """Whether *crew* may be batch-read now: connected, a unit to file into, its
    version matching. Anything else is left to the per-item read, which says why."""
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    if mgr is None or ri._crew_state(mgr, crew) != "connected":
        return False
    _unit, unit_error = await ri._board_unit(state, board)
    if unit_error is not None:
        return False
    try:
        await asyncio.wait_for(ri._routes().check_version(mgr, crew), ri.PULL_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 - the per-item read checks again and records it
        return False
    return True


async def pull_batches(
    state: Any,
    mgr: Any,
    board: str,
    remote: list[work_ledger.WorkItem],
    deadline: float,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, dict[str, Any]]]]:
    """One batch read per crew: ``({item id: row}, {crew: held rows})``.

    An item a crew did not answer, or one flagged ``more``, is read on its own.
    """
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    loop = asyncio.get_running_loop()
    by_crew: dict[str, list[work_ledger.WorkItem]] = {}
    for item in remote:
        by_crew.setdefault(str(item.worker_instance_id), []).append(item)
    # Every crew at once, each cut off at one item's pull budget, so a stalled
    # crew costs the read that budget once and never the rows another crew sent.
    budget = min(ri.PULL_TIMEOUT_SECS, max(deadline - loop.time(), 0.0))

    async def one(crew: str, items: list[work_ledger.WorkItem]) -> CrewBatch | None:
        try:
            return await asyncio.wait_for(_ready_then_pull(state, mgr, board, crew, items), budget)
        except Exception:  # noqa: BLE001 - the per-item read tries this crew again
            logger.info("batch pull from one crew failed", exc_info=True)
            return None

    crews = list(by_crew)
    batches = await asyncio.gather(*(one(crew, by_crew[crew]) for crew in crews))
    rows: dict[str, dict[str, Any]] = {}
    held: dict[str, dict[str, dict[str, Any]]] = {}
    for crew, batch in zip(crews, batches):
        if batch is None:
            continue
        rows.update({k: v for k, v in batch.rows.items() if not v["more"]})
        if batch.held:
            held[crew] = batch.held
        if batch.held_omitted:
            logger.info(
                "crew %s holds %d more unbound workers for %s than one pull reclaims",
                crew,
                batch.held_omitted,
                board,
            )
    return rows, held


async def _ready_then_pull(
    state: Any, mgr: Any, board: str, crew: str, items: list[work_ledger.WorkItem]
) -> CrewBatch | None:
    if not await _crew_ready(state, mgr, board, crew):
        return None
    return await pull_crew(mgr, board, crew, items)


async def ingest_batched(
    state: Any,
    mgr: Any,
    board: str,
    item: work_ledger.WorkItem,
    row: dict[str, Any],
) -> None:
    """File one item from its crew's batch reply, as ``remote_ingest._ingest_item`` files a
    per-slot read: no further peer call but a trust revoke."""
    from kiro_crew.dashboard.handlers import remote_ingest as ri

    if await ri._reset_if_stale(board, item):
        return
    unit, unit_error = await ri._board_unit(state, board)
    if unit_error is not None:
        await ri._mark_error(board, item.item_id, unit_error)
        return
    slot = str(item.worker_session_key)
    payload = payload_of(row)
    await ri._apply_pull(
        state,
        mgr,
        board,
        item,
        unit,
        status=200 if row["found"] else 404,
        payload=payload if payload is not None else UNCHANGED,
        limit=work_ledger.MAX_EVENTS_PER_ITEM,
        listed=listed_of(slot, row),
        trust_active=row["trust_active"],
    )


__all__ = [
    "BATCH_PATH",
    "CrewBatch",
    "listed_of",
    "UNCHANGED",
    "ingest_batched",
    "payload_of",
    "pull_batches",
    "pull_crew",
]
