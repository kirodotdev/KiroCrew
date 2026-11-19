"""The hub half of remote dispatch, wave 4 (rfc-conductor-remote-dispatch §2, §3, §5).

A conductor on this gateway binds an item to a worker session on another crew with
``session_create(crew=, work_item=)``; the peer stores the item in its
``hub:<conductor key>`` mailbox (wave 2). This module is everything the hub does with
that item afterwards, and the hub only ever PULLS: the peer never reaches back.

* :func:`create_bound` -- the ``work_item`` half of a crew create: the item must be
  the caller's own, open and unbound; the peer must echo ``work_item_id``, or its
  slot is closed and the create refused ``crew_peer_lacks_work_items``; then a
  recorded ``bind`` stores the pair ``worker_session_key`` (the peer's slot key) +
  ``worker_instance_id`` (the crew).
* :func:`ingest_board` -- on ``work_ledger_read``: for each open remote item, read
  the peer slot list (``running``, ``pending_approval``, ``closed`` = missing from a
  successful read) and the item's peer copy (``GET api/chat/slots/{s}/work-item``),
  then file every peer report newer than the item's ``last_report_at`` as a normal
  recorded report, stamped with the PEER's time, so a repeat read adds nothing.
* :func:`forward_decide` -- after a recorded ``decide`` on a remote item, PATCH the
  peer copy. A failure is reported; the local write stands.
* :func:`remote_row_fields` -- the read row's ``crew_state``, ``crew_down_since``,
  ``pending_approval`` and ``remote_error`` for a remote item.

Every peer call goes through the wave 1 ``peer_call`` (path fence, byte cap,
``_redact_peer_payload``). Fail closed: an unreachable or incompatible crew keeps
the last ingested state and records why; nothing here invents a status.

All of it sits behind ``work_ledger.remote_dispatch``. With the switch off nothing
here is called, and an item without ``worker_instance_id`` is never touched.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import json
import logging
import sys as _sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from kiro_crew import _facade_mirror, platform_compat, work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers import remote_batch_pull
from kiro_crew.dashboard.handlers import remote_ingest_peer as peer_ingest
from kiro_crew.dashboard.handlers.remote_ingest_report import (
    _ingest_report_locked,
    _is_remote_open,
    _later,
    _report_floor,
    _snapshot_key,
    _stamp,
    _unrecorded_hold,
)
from kiro_crew.dashboard.handlers.work_ledger import (
    _acting_unit,
    _board_lock,
    _conductor_unit,
    _drain_before_cancelling,
    _refuse_404,
    _refuse_409,
    _refuse_503,
    _refuse_if_dirty,
)

# This module is the remote-dispatch hub's facade: callers and tests import and patch
# every name through it, as ``kiro_crew.dashboard.handlers.remote_ingest.<name>``. A read
# of a name an owner holds is served from that owner, and a write -- ``monkeypatch.setattr``,
# ``mock.patch`` -- reaches every module holding it (:mod:`kiro_crew._facade_mirror`).
# Owners never import this module. It keeps the bind, the pull, the retire and the decide
# forward; new work goes to the owner of its responsibility:
#
#   ``remote_ingest_report`` -- a pulled peer snapshot read, cleaned and filed as a report

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Peer events a pull asks for first. When that tail holds none of the filed peer
#: event ids and is not the peer's whole log, the pull asks again for the whole log
#: (``work_ledger.MAX_EVENTS_PER_ITEM``), so every report since the last pull is
#: reached (``remote_ingest_peer``).
PULL_EVENT_LIMIT = 50

#: Most remote items one read pulls; the rest keep their last state until the next.
MAX_ITEMS_PER_READ = 32

#: Seconds one item's pull may take before it is given up for this read.
PULL_TIMEOUT_SECS = 4.0

#: Seconds all of one read's pulls may take together; the rest wait for the next
#: read. Well under the 10 s the ``work_ledger_read`` tool waits for its answer
#: (``mcp_core._get``), so a slow crew costs the conductor stale remote rows,
#: never its whole board.
READ_DEADLINE_SECS = 6.0

#: Boards a read is pulling for right now. One pull per board at a time, so two
#: overlapping reads can never file snapshots out of the order they were fetched:
#: a read that finds its board in here serves the stored state instead.
_PULLING: set[str] = set()

#: The create refusal for a peer whose reply did not echo ``work_item_id``.
CODE_PEER_LACKS_WORK_ITEMS = "crew_peer_lacks_work_items"


def _now() -> str:
    return work_ledger._now_iso()


def _routes() -> Any:
    from kiro_crew.dashboard.handlers import remote_dispatch_routes

    return remote_dispatch_routes


async def settled_item(board: str, item_id: str) -> work_ledger.WorkItem | None:
    """*item_id* as the record holds it, read under the board lock. ``None`` when
    absent, unreadable or the cache is flagged dirty.

    A write commits to the cache and then appends to the crew log under that
    lock, undoing the commit when the append fails; a read outside it could
    publish a value the hub then drops. Every read whose value leaves this
    gateway, or decides whether something does, goes through here.
    """
    async with _board_lock(board):
        if await _refuse_if_dirty(board, board, "remote_dispatch.read"):
            return None
        try:
            return await asyncio.to_thread(work_ledger.read_work_item, board, item_id)
        except work_ledger.WorkLedgerError:
            return None


# ── the conductor's Trust (RFC §4) ────────────────────────────────────────


def conductor_trusts(state: "DashboardState", board: str) -> bool:
    """Whether *board*'s conductor slot is in Trust or YOLO right now.

    The rule a local child follows: a Trust a person set on the slot carries
    (``trust_carries``; a Crew Mode card trust does not), and a YOLO grant that
    reaches the slot does. A scoped grant the slot holds is never passed on.
    """
    from kiro_crew.dashboard.approval_posture import trust_carries

    get = getattr(state, "get_slot", None)
    slot = get(board) if callable(get) else None
    if slot is None:
        return False
    if trust_carries(slot):
        return True
    return bool(state.is_yolo_active() and state.yolo_reaches(slot))


async def revoke_peer_trust(mgr: Any, instance_id: str, slot: str) -> bool:
    """PATCH ``trust: false`` to the peer worker *slot*. True when the peer took it.

    Best effort: a peer that cannot be reached keeps the grant only until its TTL,
    and the next pull that sees ``trust_active`` asks again.
    """
    routes = _routes()
    try:
        status, _payload = await routes.peer_call(
            mgr,
            instance_id,
            "PATCH",
            f"api/chat/slots/{quote(slot, safe='')}/work-item",
            body={"trust": False},
        )
    except routes.CrewCallError:
        return False
    return 200 <= status < 300


async def revoke_if_closed(state: "DashboardState", board: str, item_id: str) -> None:
    """End a closed remote item's peer grant. Never raises.

    A pull skips a closed item, so the pull's own revoke never reaches it.
    """
    item = await settled_item(board, item_id)
    if item is None or not item.is_terminal or not item.worker_instance_id:
        return
    mgr = getattr(state, "instances_manager", None)
    if mgr is None or not item.worker_session_key:
        return
    try:
        # Bounded like a decide forward, so a stalled peer cannot outlast the
        # caller's request and turn a committed close into a reported failure.
        await asyncio.wait_for(
            revoke_peer_trust(mgr, item.worker_instance_id, item.worker_session_key),
            DECIDE_DEADLINE_SECS,
        )
    except Exception:  # noqa: BLE001 - the recorded write already stands
        logger.warning("remote trust revoke failed for %s", item_id, exc_info=True)


#: The close reason for a close in a turn the owner sent, and for any other turn.
CLOSE_BY_OWNER = "owner_closed"
CLOSE_BY_WAKE = "item_closed"


async def close_remote_item(
    state: "DashboardState", board: str, item_id: str, *, owner_turn: bool
) -> None:
    """After a write closed a remote item: end its grant and leave its slot to close. Never raises.

    1. Record a ``retire`` with a close reason (local writes only): the binding moves into
       ``retired_bindings`` as ``retired``. That entry is the durable pending
       close: it survives a hub restart, the close sweep finds it
       (``boards_with_pending_close``), the owner's ``session_close(crew=)`` may
       still name the slot (``holds_retired_slot``), and the row counts it in
       ``close_pending``.
    2. ``PATCH trust: false`` at once, to the pair the item held, whether or not
       the retire landed.
    3. Start its close now (:func:`_schedule_closes`). A close in a turn the owner
       sent (*owner_turn*, :data:`CLOSE_BY_OWNER`) DELETEs the slot once its crew
       reads connected. Any other turn (:data:`CLOSE_BY_WAKE`) never DELETEs a live
       worker: the slot is closed only when the crew lists it not running and its
       copy of the item last reported ``done``; until then each sweep past the
       backoff looks again.
    """
    item = await settled_item(board, item_id)
    if (
        item is None
        or not item.is_terminal
        or not item.worker_instance_id
        or not item.worker_session_key
    ):
        return
    crew, slot = item.worker_instance_id, item.worker_session_key
    retired: work_ledger.WorkItem | None = None
    try:
        # Recorded before the PATCH, so a stalled or cancelled PATCH never leaves
        # the close without its pending entry. Local writes only.
        reason = CLOSE_BY_OWNER if owner_turn else CLOSE_BY_WAKE
        resp = await _record_retire(state, board, item, reason)
        if _landed(resp):
            retired = await settled_item(board, item_id)
        else:
            logger.warning(
                "closed remote item %s kept its binding (%s)",
                item_id,
                _response_code(resp) if resp is not None else "not_recorded",
            )
    except Exception:  # noqa: BLE001 - the recorded close already stands
        logger.warning("pending close not recorded for %s", item_id, exc_info=True)
    mgr = getattr(state, "instances_manager", None)
    if mgr is not None:
        try:
            # Bounded like a decide forward, so a stalled peer cannot outlast the
            # caller's request and turn a committed close into a reported failure.
            await asyncio.wait_for(revoke_peer_trust(mgr, crew, slot), DECIDE_DEADLINE_SECS)
        except Exception:  # noqa: BLE001 - the sweep's close sends it again
            logger.warning("remote trust revoke failed for %s", item_id, exc_info=True)
    if retired is not None:
        _schedule_closes(state, board, [retired])


# ── the bound create ──────────────────────────────────────────────────────


async def create_bound(
    state: "DashboardState",
    mgr: Any,
    board: str,
    instance_id: str,
    item_id: object,
    create: dict[str, Any],
) -> tuple[str, Any, work_ledger.WorkItem]:
    """Create a peer slot born bound to *board*'s item. ``(slot, payload, item)`` or raise.

    *create* is the peer create body wave 1 built (``memory_mode``, ``agent``,
    ``model``); this adds ``work_item``. The bind is recorded on this hub only after
    the peer echoed the item, and a bind that does not record closes the peer slot,
    so neither side keeps a worker the other does not know.
    """
    routes = _routes()
    err = routes.CrewCallError
    if not isinstance(item_id, str) or not item_id.strip():
        raise err(400, "work_item_invalid", "work_item must be an item id")
    unit, refusal = _acting_unit(state, board, "session_create.crew", item_id)
    if refusal is not None or unit is None:
        code = _response_code(refusal) if refusal is not None else ""
        raise err(409, code or "crew_log_off", "this conductor has no crew log unit to record into")
    item = await open_unbound_item(board, item_id)
    body = dict(create)
    body["work_item"] = {
        "conductor_key": board,
        "item_id": item.item_id,
        # Prose leaves this machine scrubbed, as a crew create's title does. The
        # acceptance is the bar the worker is judged by and goes as stored.
        "title": routes._outbound(item.title),
        "acceptance": item.acceptance,
        "decision": routes._outbound(item.decision),
    }
    if conductor_trusts(state, board):
        # Sent only when set, so a Normal conductor's create reads as before.
        body["work_item"]["trust"] = True
    status, payload = await routes.peer_call(mgr, instance_id, "POST", "api/chat/slots", body=body)
    if (
        status == 400
        and "trust" in body["work_item"]
        and isinstance(payload, dict)
        and payload.get("field") == "trust"
    ):
        # A peer that predates the field refuses it; its worker asks, as it always has.
        body["work_item"].pop("trust")
        status, payload = await routes.peer_call(
            mgr, instance_id, "POST", "api/chat/slots", body=body
        )
    if status == 503 and isinstance(payload, dict) and payload.get("code") == routes.CREW_BUSY:
        raise routes.CrewBusyError(payload)
    if (
        status == 409
        and isinstance(payload, dict)
        and payload.get("code") == work_ledger.CODE_DUPLICATE_ITEM
    ):
        # The peer already holds a worker for this item and made nothing here;
        # placement skips this crew on it.
        from kiro_crew.dashboard.handlers.remote_placement import DuplicateItemError

        raise DuplicateItemError()
    if not 200 <= status < 300:
        raise routes._peer_error(status, payload, "could not create the crew session")
    slot = payload.get("key") if isinstance(payload, dict) else None
    if not routes._slot_ok(slot):
        raise err(
            502, "crew_reply_malformed", "the crew's create reply named no usable session key"
        )
    assert isinstance(slot, str)
    if payload.get("work_item_id") != item.item_id:
        # A peer that ignored ``work_item`` made a plain session: closed at once, so
        # a worker never runs unbound (RFC §5, version skew).
        await _close_peer_slot(mgr, instance_id, slot)
        raise err(
            409,
            CODE_PEER_LACKS_WORK_ITEMS,
            "that crew created a session without binding the work item; it was closed",
        )
    from kiro_crew.dashboard.handlers.work_ledger import _record_conductor_write

    try:
        resp = await _record_conductor_write(
            state,
            board,
            unit,
            "bind",
            {
                "action": "bind",
                "item_id": item.item_id,
                "worker_session_key": slot,
                "worker_instance_id": instance_id,
            },
            operation="session_create.crew",
        )
    except Exception:
        await _close_peer_slot(mgr, instance_id, slot)
        raise
    if resp.status != 200:
        await _close_peer_slot(mgr, instance_id, slot)
        code = _response_code(resp) or "bind_failed"
        raise err(409 if resp.status < 500 else 503, code, "the bind did not record; closed")
    # A decide recorded while the create was in flight found the item unbound and
    # forwarded nothing, so the peer may hold an older decision than the hub. The
    # comparison runs on the SETTLED item, inside the forward's own locks.
    outcome = await forward_decide(state, board, item.item_id, unless=item.decision)
    bound = await settled_item(board, item.item_id)
    if not outcome.get("ok") and isinstance(payload, dict):
        payload = {**payload, "decision_error": str(outcome.get("code") or "")}
    return slot, payload, bound or item


async def open_unbound_item(board: str, item_id: str) -> work_ledger.WorkItem:
    """*board*'s item *item_id* when it is open and unbound, else raise the typed refusal."""
    err = _routes().CrewCallError
    item = await settled_item(board, item_id.strip())
    if item is None:
        raise err(404, work_ledger.CODE_UNKNOWN_ITEM, "no such item on this conductor's ledger")
    if item.is_terminal:
        raise err(409, work_ledger.CODE_ITEM_CLOSED, f"item {item.item_id} is {item.state}")
    if item.worker_session_key:
        raise err(409, "already_bound", f"item {item.item_id} is already bound")
    if item.retired_bindings:
        record = await asyncio.to_thread(work_ledger.read_remote_state, board, item.item_id)
        if record is not None and record.binding is not None:
            # The retire's fresh record has not stored yet (wave 8): fenced.
            raise err(
                409,
                CODE_RETIRE_PENDING,
                f"item {item.item_id} was retired and its old record is not yet cleared; "
                "retry after the next work_ledger_read",
            )
    return item


def _response_code(resp: web.Response) -> str:
    try:
        body = json.loads(resp.body) if isinstance(resp.body, bytes | str) else {}
    except ValueError:
        return ""
    code = body.get("code") if isinstance(body, dict) else ""
    return code if isinstance(code, str) else ""


async def _close_peer_slot(mgr: Any, instance_id: str, slot: str) -> None:
    """Best-effort close of a peer slot this create must not leave behind."""
    routes = _routes()
    try:
        await routes.peer_call(mgr, instance_id, "DELETE", f"api/chat/slots/{quote(slot, safe='')}")
    except routes.CrewCallError as exc:
        logger.warning("could not close crew slot on %s (%s)", instance_id, exc.code)


async def holds_peer_slot(board: str, instance_id: str, slot: str) -> bool:
    """Whether an OPEN item on *board* is bound to *slot* on *instance_id*.

    The target fence for the item pair, which survives a hub restart where the
    in-memory dispatch record does not. Read under the board lock, so a bind or
    close still being recorded never opens the fence.
    """
    async with _board_lock(board):
        if await _refuse_if_dirty(board, board, "remote_dispatch.fence"):
            return False
        try:
            items = await asyncio.to_thread(work_ledger.list_work_items, board)
        except (work_ledger.WorkLedgerError, OSError):
            return False
    return any(
        item.worker_instance_id == instance_id
        and item.worker_session_key == slot
        and not item.is_terminal
        for item in items
    )


def open_remote_bindings() -> dict[str, int]:
    """Open remote bindings per crew across every ledger here: wave 5's reservations.

    The store seam for :mod:`remote_placement`, which does not import the store.
    BLOCKING.
    """
    return work_ledger.open_remote_bindings()


# ── ingest on read ────────────────────────────────────────────────────────


def _crew_state(mgr: Any, instance_id: str) -> str:
    try:
        status = mgr.status(instance_id) if mgr is not None else None
    except Exception:  # noqa: BLE001 - a status read must not fail a ledger read
        status = None
    value = getattr(getattr(status, "state", None), "value", None)
    if value in work_ledger.CREW_STATES:
        return str(value)
    return "disconnected" if mgr is not None else "unknown"


def _stored(board: str, item_id: str) -> work_ledger.RemoteState:
    return work_ledger.read_remote_state(board, item_id) or work_ledger.RemoteState()


def _store(board: str, item_id: str, record: work_ledger.RemoteState) -> bool:
    """Write *record*; whether it landed. A failure is logged, never raised."""
    try:
        work_ledger.write_remote_state(board, item_id, record)
    except (work_ledger.WorkLedgerError, OSError):
        logger.warning("remote state write failed for %s", item_id, exc_info=True)
        return False
    return True


async def _mark_down(
    board: str, item_id: str, crew_state: str, error: str
) -> work_ledger.RemoteState:
    """Keep the last ingested answers; say the crew is down and since when.

    Every write of a remote record holds the board lock, so two pulls never
    write over each other's record.
    """
    async with _board_lock(board):
        record = await asyncio.to_thread(_stored, board, item_id)
        if record.crew_state == "connected" or not record.crew_down_since:
            record.crew_down_since = _now()
        record.crew_state = crew_state if crew_state != "connected" else "error"
        record.error = error
        record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item_id, record)
    return record


async def _mark_connecting(board: str, item_id: str) -> None:
    """The crew's tunnel is reconnecting: not down, so no outage is counted."""
    async with _board_lock(board):
        record = await asyncio.to_thread(_stored, board, item_id)
        record.crew_state = "connecting"
        record.crew_down_since = None
        record.error = "crew_connecting"
        record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item_id, record)


async def _reset_if_stale(board: str, item: work_ledger.WorkItem) -> bool:
    """Replace a record that names another binding than *item*'s. Whether it was stale.

    A stale record is one a retire could not replace (wave 8): until a fresh one
    stores, nothing is ingested on it and no replacement may bind. Never raises.
    """
    async with _board_lock(board):
        record = await asyncio.to_thread(_stored, board, item.item_id)
        if record.binding is None or record.binding == work_ledger.binding_id(item):
            return False
        await asyncio.to_thread(_store, board, item.item_id, _fresh_record())
    return True


def _fresh_record() -> work_ledger.RemoteState:
    """The record a new binding starts from: no floor, no outage, no owner."""
    return work_ledger.RemoteState(report_floor_known=True)


async def _mark_error(board: str, item_id: str, error: str) -> None:
    """The crew answered but nothing was ingested: keep the answers, record why."""
    async with _board_lock(board):
        record = await asyncio.to_thread(_stored, board, item_id)
        record.error = error
        record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item_id, record)


async def ingest_board(state: "DashboardState", board: str) -> None:
    """Pull every open remote item on *board* and file what is new. Never raises.

    Called by ``work_ledger_read`` with the switch on, before the read takes its
    board lock. Each crew is read once, in one batch call (``remote_batch_pull``);
    a crew without that route has its slot list read once and each item's peer
    copy once.
    """
    # The roster is read SETTLED, as every value that drives a peer call is: a
    # bind still being recorded must not start a pull.
    async with _board_lock(board):
        if await _refuse_if_dirty(board, board, "work_ledger_read.ingest"):
            return
        try:
            items = await asyncio.to_thread(work_ledger.list_work_items, board)
        except (work_ledger.WorkLedgerError, OSError):
            return
    open_remote = [item for item in items if _is_remote_open(item)]
    # Wave 8: a retire whose fresh record did not store leaves the item fenced;
    # each read tries again (local only, no peer call).
    for item in items:
        if item.retired_bindings and not item.worker_session_key and not item.is_terminal:
            await _reset_if_stale(board, item)
    # Wave 8: close each retired peer slot whose crew is back (RFC §5b step 3).
    _schedule_closes(state, board, items)
    # Tried longest ago first, never tried first of all: a read cut off by its
    # deadline then leaves a different tail each time, so a slow crew cannot keep
    # a healthy crew's items from ever being reached.
    last_pull = await asyncio.to_thread(
        lambda: {
            item.item_id: (
                work_ledger.read_remote_state(board, item.item_id) or _NEVER
            ).attempted_at
            for item in open_remote
        }
    )
    open_remote.sort(key=lambda item: _stamp(last_pull.get(item.item_id)) or _EPOCH)
    remote = open_remote[:MAX_ITEMS_PER_READ]
    if not remote:
        return
    if board in _PULLING:
        return
    _PULLING.add(board)
    try:
        await _pull_all(state, board, remote)
        # Wave 8: retire what has been unreachable too long (RFC §5b).
        await _retire_overdue(state, board, remote, open_remote)
    finally:
        _PULLING.discard(board)


async def _pull_all(
    state: "DashboardState",
    board: str,
    remote: list[work_ledger.WorkItem],
) -> None:
    mgr = getattr(state, "instances_manager", None)
    slot_lists: dict[str, dict[str, dict[str, bool]] | None] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READ_DEADLINE_SECS
    rows = await remote_batch_pull.pull_batches(state, mgr, board, remote, deadline)
    # Rows a crew already answered need no peer call: filed first, so a stalled
    # crew's per-item fallback never spends the deadline they were fetched within.
    for item in sorted(remote, key=lambda i: i.item_id not in rows):
        left = deadline - loop.time()
        if left <= 0:
            # Out of time for this read: these items keep their last state.
            break
        row = rows.get(item.item_id)
        try:
            await asyncio.wait_for(
                (
                    _ingest_item(state, mgr, board, item, slot_lists)
                    if row is None
                    else remote_batch_pull.ingest_batched(state, mgr, board, item, row)
                ),
                min(PULL_TIMEOUT_SECS, left),
            )
        except asyncio.TimeoutError:
            # Slow, not shown down: the crew's state stays what it was.
            await _mark_error(board, item.item_id, "crew_pull_timeout")
        except Exception:  # noqa: BLE001 - a pull must never fail the read
            logger.warning("remote ingest failed for %s", item.item_id, exc_info=True)
            await _mark_error(board, item.item_id, "crew_pull_failed")


async def _slot_list(
    state: "DashboardState", instance_id: str
) -> dict[str, dict[str, bool]] | None:
    """``{slot key: {running, pending_approval, idle}}`` for a crew, or ``None`` when unread.

    Only keys and three booleans are taken from the rows, so no peer text reaches
    the conductor from this read. A truncated list cannot prove a slot gone, so it
    answers ``None`` for the close question as an unreadable one does.
    """
    from kiro_crew.dashboard.handlers_instances_peer import PeerSlotsUnavailable, read_peer_slots

    try:
        peer = await read_peer_slots(state, instance_id)
    except PeerSlotsUnavailable:
        return None
    if peer.over_cap:
        return None
    out: dict[str, dict[str, bool]] = {}
    for row in peer.rows:
        key = row.get("key") if isinstance(row, dict) else None
        if isinstance(key, str) and key:
            out[key] = {
                "running": row.get("running") is True,
                "pending_approval": row.get("pending_approval") is True,
                # The close gate's own answer: idle only when the crew SAYS the slot
                # is not running, so a row missing the field is not idle.
                "idle": row.get("running") is False and row.get("pending_approval") is not True,
            }
    return out


async def _ingest_item(
    state: "DashboardState",
    mgr: Any,
    board: str,
    item: work_ledger.WorkItem,
    slot_lists: dict[str, dict[str, dict[str, bool]] | None],
) -> None:
    routes = _routes()
    crew = str(item.worker_instance_id)
    slot = str(item.worker_session_key)
    if await _reset_if_stale(board, item):
        # The record still described a retired binding: nothing is ingested on
        # it, and this read only replaced it. The next read pulls.
        return
    crew_state = _crew_state(mgr, crew)
    if mgr is not None and crew_state == "connecting":
        # Reconnecting counts as up for the retire clock (wave 8).
        await _mark_connecting(board, item.item_id)
        return
    if mgr is None or crew_state != "connected":
        await _mark_down(board, item.item_id, crew_state, "proxy_peer_not_connected")
        return
    # The unit the board's copy is filed into, as for a local worker's report: the
    # conductor's live unit, else its slot's newest one, so a pull with no live
    # conductor turn (the background puller) still files. Resolved before any peer
    # call, so a board with nowhere to file costs the crew nothing.
    unit, unit_error = await _board_unit(state, board)
    if unit_error is not None:
        await _mark_error(board, item.item_id, unit_error)
        return
    try:
        await routes.check_version(mgr, crew)
    except routes.CrewCallError as exc:
        if exc.code == "proxy_peer_not_connected":
            await _mark_down(board, item.item_id, "disconnected", exc.code)
        else:
            await _mark_error(board, item.item_id, exc.code)
        return
    if crew not in slot_lists:
        slot_lists[crew] = await _slot_list(state, crew)
    listed = slot_lists[crew]
    # Only for the paging choice; the filing reads the fold again under the lock.
    filed_ids = await _filed_ids_hint(board, item.item_id)
    limit = PULL_EVENT_LIMIT
    try:
        status, payload = await _read_peer_item(routes, mgr, crew, slot, limit)
        if 200 <= status < 300 and peer_ingest.needs_page(payload, filed_ids, limit):
            limit = work_ledger.MAX_EVENTS_PER_ITEM
            status, payload = await _read_peer_item(routes, mgr, crew, slot, limit)
    except routes.CrewCallError as exc:
        # A failed read changes nothing that says where ingest stands; the next
        # pull reads again.
        if exc.status == 503:
            await _mark_down(board, item.item_id, "disconnected", exc.code)
        else:
            await _mark_error(board, item.item_id, exc.code)
        return
    await _apply_pull(
        state,
        mgr,
        board,
        item,
        unit,
        status=status,
        payload=payload,
        limit=limit,
        listed=listed,
        trust_active=payload.get("trust_active") if isinstance(payload, dict) else None,
    )


async def _apply_pull(
    state: "DashboardState",
    mgr: Any,
    board: str,
    item: work_ledger.WorkItem,
    unit: str,
    *,
    status: int,
    payload: Any,
    limit: int,
    listed: dict[str, dict[str, bool]] | None,
    trust_active: Any,
) -> None:
    """Store what one read of *item*'s peer copy said and file its news.

    *payload* ``remote_batch_pull.UNCHANGED`` is a batch row with no news: the answers are
    stored fresh and nothing is filed.
    """
    crew = str(item.worker_instance_id)
    slot = str(item.worker_session_key)
    observed: dict[str, bool] = {}
    if listed is not None:
        row = listed.get(slot)
        observed = {
            "closed": row is None,
            "running": bool(row and row["running"]),
            "pending_approval": bool(row and row["pending_approval"]),
        }
    async with _board_lock(board):
        # Read under the lock, so two pulls never write over each other's record.
        record = await asyncio.to_thread(_stored, board, item.item_id)
        if record.binding not in (None, work_ledger.binding_id(item)):
            # Went stale since the guard above: file nothing on it.
            return
        # A retire that landed during the peer call left a fresh record with no
        # binding; writing this pull's pair into it would fence the item.
        if not await _still_bound(board, item):
            return
        record.binding = work_ledger.binding_id(item)
        record.crew_state = "connected"
        record.crew_down_since = None
        for name, value in observed.items():
            setattr(record, name, value)
        # Whether this pull read the crew's roster: without it ``closed`` is the last
        # known answer, not a fresh one, so the probe's person-wait hold must not
        # rest on it (``remote_pull_fresh``).
        record.roster_read = listed is not None
        if status == 404:
            # No such slot, or no item on it: nothing to ingest. The slot list
            # above answers whether the session is gone.
            record.error = "crew_work_item_not_found"
        elif not 200 <= status < 300 or not isinstance(payload, dict):
            record.error = "crew_peer_refused"
        elif payload is remote_batch_pull.UNCHANGED:
            record.error = None
        elif _unrecorded_hold(record, payload):
            # This snapshot's filing failed to reach the crew log moments ago: keep the
            # error and wait out the window rather than audit the same failure again.
            record.error = "crew_log_unrecorded"
        else:
            floor = _report_floor(record, item)
            record.error = await _ingest_report_locked(
                state,
                board,
                unit,
                item.item_id,
                crew,
                slot,
                payload,
                floor,
                limit,
                record_floor_known=record.report_floor_known,
            )
            failed = record.error == "crew_log_unrecorded"
            record.unrecorded_key = _snapshot_key(payload) if failed else None
            record.unrecorded_at = _now() if failed else None
            peer = payload.get("work_item")
            peer_stamp = peer.get("last_report_at") if isinstance(peer, dict) else None
            if record.error is None and isinstance(peer_stamp, str) and _later(peer_stamp, floor):
                record.report_floor, record.report_floor_known = peer_stamp, True
        record.ingested_at = record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item.item_id, record)
    # A conductor that left Trust ends the grant its worker still holds (RFC §4).
    # Asked only when the peer says it holds one, so an older peer gets no new field.
    if status == 200 and trust_active is True and not conductor_trusts(state, board):
        await revoke_peer_trust(mgr, crew, slot)


async def _read_peer_item(
    routes: Any, mgr: Any, crew: str, slot: str, limit: int
) -> tuple[int, Any]:
    """The peer's ``GET work-item`` with its newest *limit* events. Raises
    ``CrewCallError`` as ``peer_call`` does."""
    status, payload = await routes.peer_call(
        mgr,
        crew,
        "GET",
        f"api/chat/slots/{quote(slot, safe='')}/work-item",
        params={"limit": str(limit)},
    )
    return status, payload


async def _filed_ids_hint(board: str, item_id: str) -> tuple[str, ...] | None:
    """The peer event ids the fold holds as filed, or ``None`` when it cannot be read."""
    try:
        return (await asyncio.to_thread(peer_ingest.read_folded_peer, board, item_id)).ids
    except Exception:  # noqa: BLE001 - the filing reads it again and says why
        return None


async def _board_unit(state: "DashboardState", board: str) -> tuple[str, str | None]:
    """``(unit, None)`` for the unit *board*'s copy of a report is filed into, else
    ``("", code)``. No audit row: a pull is not a caller's write being refused."""
    if not crew_log_emit.enabled():
        return "", "crew_log_off"
    unit = await asyncio.to_thread(_conductor_unit, state, board)
    return (unit, None) if unit else ("", "crew_log_unit_unknown")


#: A record for an item never pulled.
_NEVER = work_ledger.RemoteState()

#: Before every real stamp, for sorting never-pulled items first.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


# ── retire and redispatch (wave 8, RFC §5b) ──────────────────────────────


#: The retire reason this module writes.
RETIRE_CREW_UNREACHABLE = "crew_unreachable"

#: The refusal for a bind while a retire's fresh record has not stored yet.
CODE_RETIRE_PENDING = "crew_retire_pending"

#: The refusal for any verb but close on a retired binding's slot.
CODE_TARGET_RETIRED = "crew_target_retired"


def _retire_minutes() -> float:
    from kiro_crew.dashboard.remote_dispatch_headroom import remote_dispatch_settings

    return remote_dispatch_settings().retire_minutes


#: The clock the retire timer reads: it stands still while the host is suspended
#: (Windows included), and a hub restart starts it afresh, so neither counts as
#: outage.
_monotonic = platform_compat.awake_now

#: ``(board, item id, binding) -> monotonic time`` this process first saw that
#: binding's crew down, through the last read that still saw it down. In memory
#: only: a stored ``crew_down_since`` from before this process started is never
#: trusted, so the clock restarts from the first down seen after start.
_DOWN_SEEN: dict[tuple[str, str, str], float] = {}


def _record_down(record: work_ledger.RemoteState) -> bool:
    return record.crew_state not in ("connected", "connecting") and bool(record.crew_down_since)


async def _retire_overdue(
    state: "DashboardState",
    board: str,
    remote: list[work_ledger.WorkItem],
    open_remote: list[work_ledger.WorkItem],
) -> None:
    """Retire each binding whose crew this process has seen down past the timeout.

    Never raises. The clock (:data:`_DOWN_SEEN`) starts at the first read in this
    process that sees the crew down, both live and in the record, and stops at any
    read that sees it ``connected`` or ``connecting``. The item is left open and
    unbound, its pair moved to ``retired_bindings``, so ingest reads nothing more
    from that slot: a late report from it is never applied. The redispatch itself
    is the conductor's ``session_create(work_item=)`` on a turn the owner sent;
    the read row says ``redispatch_due``.
    """
    minutes = await asyncio.to_thread(_retire_minutes)
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        return
    # A clock is kept while its binding is open anywhere on the board, not only
    # in this read's slice of *remote*.
    live_keys = {
        (board, item.item_id, pair)
        for item in open_remote
        if (pair := work_ledger.binding_id(item)) is not None
    }
    # A crew that reads up clears every clock it holds, whether or not this read's
    # slice of *remote* reached the item (in-memory tunnel state, no peer call).
    up = {
        str(item.worker_instance_id)
        for item in open_remote
        if item.worker_instance_id
        and _crew_state(mgr, str(item.worker_instance_id)) in ("connected", "connecting")
    }
    for key in [k for k in _DOWN_SEEN if k[2].split("/", 1)[0] in up]:
        _DOWN_SEEN.pop(key, None)
    for item in remote:
        pair = work_ledger.binding_id(item)
        if pair is None:
            continue
        key = (board, item.item_id, pair)
        try:
            if _crew_state(mgr, str(item.worker_instance_id)) in ("connected", "connecting"):
                _DOWN_SEEN.pop(key, None)
                continue
            record = await asyncio.to_thread(_stored, board, item.item_id)
            if record.binding not in (None, pair) or not _record_down(record):
                _DOWN_SEEN.pop(key, None)
                continue
            first = _DOWN_SEEN.setdefault(key, _monotonic())
            if _monotonic() - first >= minutes * 60 and _landed(
                await _record_retire(state, board, item)
            ):
                _DOWN_SEEN.pop(key, None)
        except Exception:  # noqa: BLE001 - a retire must never fail the read
            logger.warning("remote retire failed for %s", item.item_id, exc_info=True)
    for key in [k for k in _DOWN_SEEN if k[0] == board and k not in live_keys]:
        _DOWN_SEEN.pop(key, None)


def _landed(resp: web.Response | None) -> bool:
    return resp is not None and resp.status == 200


#: The refusal for a retire whose item left that binding since it was read.
CODE_RETIRE_BINDING_MOVED = "retire_binding_moved"


async def _still_bound(board: str, item: work_ledger.WorkItem) -> bool:
    """Whether *board*'s copy of *item* still holds *item*'s pair. Hold the board lock.

    Every write that stamps a pair into the remote record checks this first: the
    item it holds was read before the lock, and a retire may have landed since.
    """
    current = await asyncio.to_thread(work_ledger.read_work_item, board, item.item_id)
    return current is not None and work_ledger.binding_id(current) == work_ledger.binding_id(item)


async def _record_retire(
    state: "DashboardState",
    board: str,
    item: work_ledger.WorkItem,
    reason: str = RETIRE_CREW_UNREACHABLE,
    *,
    code_channel: bool = False,
    caller_sk: str = "",
) -> web.Response | None:
    """Record one ``retire`` write for *item*'s current binding.

    The write's own reply, or ``None`` when it was never tried (no crew-log unit,
    or the record could not name the binding first).
    """
    slot, crew = str(item.worker_session_key), str(item.worker_instance_id)
    # The board's durable crew-log unit, as a pull files reports into: the
    # background puller retires with no conductor runtime alive.
    unit, unit_error = await _board_unit(state, board)
    if unit_error is not None or not unit:
        return None
    # The record must name this binding BEFORE the retire commits, so a fresh
    # record that then fails to store leaves it recognisably stale (fenced)
    # rather than silently carried into the replacement. No stamp, no retire.
    async with _board_lock(board):
        # *item* was read before this lock: another retire may have landed since
        # and left a fresh record, which this stale pair must not fence.
        if not await _still_bound(board, item):
            return _refuse_409(
                CODE_RETIRE_BINDING_MOVED,
                f"item {item.item_id} is no longer bound to that crew session; "
                "read the ledger again",
            )
        record = await asyncio.to_thread(_stored, board, item.item_id)
        if record.binding is None:
            record.binding = work_ledger.binding_id(item)
            if not await asyncio.to_thread(_store, board, item.item_id, record):
                return None
    # The write and the steps after it run as one, finished even when this read
    # is cancelled (the puller's per-board budget): a retire that committed always
    # leaves the dispatch record and the remote record behind it.
    return await _drain_before_cancelling(
        _commit_retire(state, board, unit, item, slot, crew, reason, code_channel, caller_sk)
    )


async def _commit_retire(
    state: "DashboardState",
    board: str,
    unit: str,
    item: work_ledger.WorkItem,
    slot: str,
    crew: str,
    reason: str,
    code_channel: bool,
    caller_sk: str,
) -> web.Response:
    """Record the retire, then drop the dispatch entry and store a fresh record.

    *code_channel* and *caller_sk* are the record route's, for a conductor's own
    retire (the reply's published-fields projection); the hub's retire has neither.
    """
    from kiro_crew.dashboard.handlers.work_ledger import _record_conductor_write

    # The slot leaves the in-memory dispatch record BEFORE the retire commits, so
    # no send / read / stop reaches it through the cache while the write is still
    # settling; until then only the item pair admits it, read under the board
    # lock, which waits for the write and then refuses a retired slot. If the
    # retire does not land (refused, failed or cancelled), every caller that held
    # the slot gets it back: the worker is still bound and still counts toward its
    # caller's crew-session cap. No peer call.
    routes = _routes()
    # Evict -> write -> restore holds the cap lock, as a create's cap check and
    # record do, so a create never counts the slot as freed mid-retire.
    async with routes.cap_lock(state):
        holders = [c for c, held in routes._record(state).items() if (crew, slot) in held]
        routes.forget_dispatched_slot(state, crew, slot)
        landed = False
        try:
            resp = await _record_conductor_write(
                state,
                board,
                unit,
                "retire",
                {
                    "action": "retire",
                    "item_id": item.item_id,
                    "worker_session_key": slot,
                    "worker_instance_id": crew,
                    "reason": reason,
                },
                operation=(
                    "work_ledger_read.retire"
                    if reason == RETIRE_CREW_UNREACHABLE
                    else (
                        "work_ledger_record.close"
                        if reason in work_ledger.CLOSE_RETIRE_REASONS
                        else "work_ledger_record.retire"
                    )
                ),
                code_channel=code_channel,
                caller_sk=caller_sk,
            )
            landed = resp.status == 200
            if not landed:
                logger.info(
                    "remote retire of %s not recorded (%s)", item.item_id, _response_code(resp)
                )
                return resp
        finally:
            if not landed:
                for caller in holders:
                    routes._record_dispatch(state, caller, crew, slot)
    # The record described the retired crew; a new binding starts its own, with
    # no report floor: the replacement's stamps come from another clock. A store
    # that fails leaves the old record naming the retired pair, which fences the
    # item (no ingest, no bind) until a later read stores it (_reset_if_stale).
    async with _board_lock(board):
        await asyncio.to_thread(_store, board, item.item_id, _fresh_record())
    return resp


#: The refusal for a conductor retire of an item bound to no session.
CODE_RETIRE_NOT_BOUND = "retire_not_bound"

#: The refusal for a conductor retire of an item bound to a LOCAL session.
CODE_RETIRE_NOT_REMOTE = "retire_not_remote"


async def retire_by_conductor(
    state: "DashboardState",
    board: str,
    item_id: str,
    reason: str,
    *,
    code_channel: bool = False,
    caller_sk: str = "",
) -> web.Response:
    """The owning conductor's ``work_ledger_record action=retire`` (RFC §5b).

    *board* is the caller's own, so an item on any other board reads as unknown:
    only its owner may retire it. The same write as the hub's own retire
    (:func:`_record_retire`), with the conductor's *reason*: the item stays open
    with its history and the worker's last branch, placement skips the crew
    (:func:`retired_crews`), and the old slot is closed now if its crew is
    connected, else by the first pull that finds it back.
    """
    item = await settled_item(board, item_id)
    if item is None:
        return _refuse_404(work_ledger.CODE_UNKNOWN_ITEM, f"no item {item_id} on your ledger")
    if item.is_terminal:
        return _refuse_409(work_ledger.CODE_ITEM_CLOSED, f"item {item_id} is {item.state}")
    if not item.worker_session_key:
        return _refuse_409(CODE_RETIRE_NOT_BOUND, f"item {item_id} is bound to no session")
    if not item.worker_instance_id:
        return _refuse_409(
            CODE_RETIRE_NOT_REMOTE,
            f"item {item_id} is bound to a local session; retire moves only a crew-bound "
            "item. Close it, or create a new item for another session.",
        )
    resp = await _record_retire(
        state, board, item, reason, code_channel=code_channel, caller_sk=caller_sk
    )
    if resp is None:
        return _refuse_503("retire_not_recorded", "the retire was not recorded; try again")
    if _landed(resp):
        retired = await settled_item(board, item_id)
        if retired is not None:
            _schedule_closes(state, board, [retired])
    return resp


#: Seconds a failed close waits before the next sweep or read tries it again,
#: doubled after each failure up to :data:`CLOSE_RETRY_MAX_SECS`.
CLOSE_RETRY_SECS = 60.0

#: The longest wait between two tries of one close.
CLOSE_RETRY_MAX_SECS = 3600.0

#: ``(crew, slot)`` -> ``(failures, not_before)`` for each retired slot this
#: process has tried to close and not yet closed. ``not_before`` is on
#: :data:`_monotonic`'s clock, and ``inf`` while a try is in flight. A close that
#: fails leaves the entry ``retired`` (fenced, close-only) and is tried again by
#: the first sweep or read past ``not_before``; there is no timer of its own.
#: shortcut: in memory only, so a hub restart tries every slot at once; persist
#: the backoff if restarts ever come faster than the wait.
_CLOSE_RETRY: dict[tuple[str, str], tuple[int, float]] = {}

#: Retire reasons whose slot is closed as soon as its crew reads connected: the
#: hub's own timeout, the conductor's owner-turn retire and an owner-turn close.
#: Any other reason, an unknown or missing one included, closes only an idle slot
#: whose worker reported ``done`` (fail closed).
_CLOSE_AT_ONCE_REASONS: frozenset[str] = frozenset(
    {RETIRE_CREW_UNREACHABLE, CLOSE_BY_OWNER, *work_ledger.WORK_CONDUCTOR_RETIRE_REASONS}
)

#: The close tasks in flight, held so none is collected before it ends.
_CLOSE_TASKS: set["asyncio.Task[None]"] = set()


def _close_due(key: tuple[str, str]) -> bool:
    """Whether *key* is neither in flight nor inside its backoff window."""
    tried = _CLOSE_RETRY.get(key)
    return tried is None or _monotonic() >= tried[1]


def _schedule_closes(
    state: "DashboardState", board: str, items: list[work_ledger.WorkItem]
) -> None:
    """Start one close per due retired slot whose crew reads connected now. Never raises.

    Each close runs as its own task, so a slow crew never delays this read or
    holds back another close. A crew that is not connected costs no try.
    """
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        return
    for item in items:
        for entry in item.retired_bindings:
            if entry["state"] != "retired":
                continue
            key = (entry["worker_instance_id"], entry["worker_session_key"])
            if not _close_due(key) or _crew_state(mgr, key[0]) != "connected":
                continue
            failures = _CLOSE_RETRY.get(key, (0, 0.0))[0]
            _CLOSE_RETRY[key] = (failures, float("inf"))
            task = asyncio.ensure_future(
                _close_retired_slot(
                    state,
                    mgr,
                    board,
                    *key,
                    only_if_done=entry.get("reason") not in _CLOSE_AT_ONCE_REASONS,
                )
            )
            _CLOSE_TASKS.add(task)
            task.add_done_callback(_CLOSE_TASKS.discard)


async def _close_retired_slot(
    state: "DashboardState",
    mgr: Any,
    board: str,
    crew: str,
    slot: str,
    *,
    only_if_done: bool = False,
) -> None:
    """End one retired peer slot's Trust, close it, then mark it ``destroyed``. Never raises.

    The ``trust: false`` PATCH goes first and is best effort, so a worker whose
    DELETE then fails holds no grant while it waits for the retry. A 2xx, or a
    404 saying the slot is gone, counts as closed. Anything else (a timeout, a
    refusal, a version skew, a ``destroy`` that did not record) leaves the entry
    ``retired`` (fail closed) and backs the next try off.

    *only_if_done* (an item closed outside an owner turn, :data:`CLOSE_BY_WAKE`, or
    any reason not in :data:`_CLOSE_AT_ONCE_REASONS`):
    the DELETE is sent only when :func:`_slot_idle_and_done` says so; otherwise
    the entry stays ``retired`` and backs off like a failed close.
    """
    routes = _routes()
    key = (crew, slot)
    closed = False
    try:
        try:
            await asyncio.wait_for(routes.check_version(mgr, crew), PULL_TIMEOUT_SECS)
            try:
                await asyncio.wait_for(revoke_peer_trust(mgr, crew, slot), PULL_TIMEOUT_SECS)
            except Exception:  # noqa: BLE001 - the DELETE below still ends the grant
                logger.info("retired slot %s on %s kept its trust", slot, crew, exc_info=True)
            if only_if_done and not await asyncio.wait_for(
                _slot_idle_and_done(state, mgr, crew, slot), PULL_TIMEOUT_SECS
            ):
                logger.info("closed item's slot %s on %s is not idle and done yet", slot, crew)
                return
            status, payload = await asyncio.wait_for(
                routes.peer_call(mgr, crew, "DELETE", f"api/chat/slots/{quote(slot, safe='')}"),
                PULL_TIMEOUT_SECS,
            )
        except Exception:  # noqa: BLE001 - a failed close stays fenced and backs off
            logger.info("retired slot %s on %s not closed", slot, crew, exc_info=True)
            return
        if 200 <= status < 300 or slot_gone(status, payload):
            closed = await mark_destroyed(state, board, crew, slot)
    finally:
        if closed:
            _CLOSE_RETRY.pop(key, None)
        else:
            failures = _CLOSE_RETRY.get(key, (0, 0.0))[0] + 1
            delay = min(CLOSE_RETRY_SECS * 2 ** min(failures - 1, 16), CLOSE_RETRY_MAX_SECS)
            _CLOSE_RETRY[key] = (failures, _monotonic() + delay)


async def _slot_idle_and_done(state: "DashboardState", mgr: Any, crew: str, slot: str) -> bool:
    """Whether a closed item's peer slot may be closed outside an owner turn. Fail closed.

    True when a full read of the crew's slot list shows *slot* neither running nor
    waiting on an approval and the peer's copy of the item last reported ``done``,
    or when that full read does not list the slot (gone: the DELETE only
    confirms it). Any unread or partial answer is ``False``.
    """
    listed = await _slot_list(state, crew)
    if listed is None:
        return False
    row = listed.get(slot)
    if row is None:
        return True
    if not row["idle"]:
        return False
    try:
        status, payload = await _read_peer_item(_routes(), mgr, crew, slot, 1)
    except _routes().CrewCallError:
        return False
    peer = payload.get("work_item") if 200 <= status < 300 and isinstance(payload, dict) else None
    return isinstance(peer, dict) and peer.get("status") == "done"


def slot_gone(status: int, payload: Any) -> bool:
    """Whether a close reply says the crew holds no such slot."""
    return status == 404 and isinstance(payload, dict) and payload.get("code") == "slot_not_found"


async def mark_destroyed(state: "DashboardState", board: str, instance_id: str, slot: str) -> bool:
    """Record ``destroy`` on the item holding *slot* on *instance_id* as ``retired``.

    Called once the peer confirmed that slot closed or gone. Whether it landed;
    any failure leaves the entry ``retired`` (close-only). Never raises.
    """
    from kiro_crew.dashboard.handlers.work_ledger import _record_conductor_write

    try:
        items = await asyncio.to_thread(work_ledger.list_work_items, board)
        owner = next(
            (
                item
                for item in items
                if any(
                    b["state"] == "retired"
                    and b["worker_instance_id"] == instance_id
                    and b["worker_session_key"] == slot
                    for b in item.retired_bindings
                )
            ),
            None,
        )
        if owner is None:
            return False
        unit, unit_error = await _board_unit(state, board)
        if unit_error is not None or not unit:
            return False
        resp = await _record_conductor_write(
            state,
            board,
            unit,
            "destroy",
            {
                "action": "destroy",
                "item_id": owner.item_id,
                "worker_session_key": slot,
                "worker_instance_id": instance_id,
            },
            operation="remote_dispatch.destroy",
        )
    except Exception:  # noqa: BLE001 - the close itself already happened
        logger.warning("could not mark %s on %s destroyed", slot, instance_id, exc_info=True)
        return False
    return resp.status == 200


async def holds_retired_slot(board: str, instance_id: str, slot: str) -> bool | None:
    """Whether an item on *board* holds *slot* on *instance_id* as a retired binding.

    Such a slot keeps only the ``session_close(crew=)`` right (RFC §5b). ``None``
    when the board cannot be read (flagged dirty, unreadable); only an explicit
    ``True`` admits a close, so an unreadable history admits nothing.
    """
    async with _board_lock(board):
        if await _refuse_if_dirty(board, board, "remote_dispatch.fence"):
            return None
        try:
            items = await asyncio.to_thread(work_ledger.list_work_items, board)
        except (work_ledger.WorkLedgerError, OSError):
            return None
    return any(
        b["state"] == "retired"
        and b["worker_instance_id"] == instance_id
        and b["worker_session_key"] == slot
        for item in items
        for b in item.retired_bindings
    )


def retired_crews(item: work_ledger.WorkItem) -> set[str]:
    """The crews *item* was retired from, which placement skips."""
    return {b["worker_instance_id"] for b in item.retired_bindings}


# ── decide forwards ───────────────────────────────────────────────────────


#: One forward at a time per remote item, so two overlapping decides cannot land
#: on the peer out of order. Entries are dropped when nobody holds or waits.
_FORWARD_LOCKS: dict[tuple[str, str], asyncio.Lock] = {}

#: Seconds one decide forward may take in all: the wait for the item's forward
#: lock, the settled read and the PATCH together. Well under the 30 s the
#: ``work_ledger_record`` tool waits for its answer (``mcp_core._post``), so a
#: peer that stays connected but stalls costs the reply a ``crew_decide_timeout``
#: outcome, never the recorded decide itself.
DECIDE_DEADLINE_SECS = 10.0


async def forward_decide(
    state: "DashboardState", board: str, item_id: str, *, unless: str | None = None
) -> dict[str, Any]:
    """PATCH the item's LATEST committed decision to its peer copy.

    Serialized per item, and the decision is read inside that hold, so the peer
    ends on the decision the hub ends on whatever order two decides finish in.
    ``{"ok": True}`` or ``{"ok": False, "code": ...}``. With *unless*, a settled
    decision equal to it is already the peer's, so nothing is sent. Bounded by
    :data:`DECIDE_DEADLINE_SECS` in all; past it the answer is
    ``crew_decide_timeout`` and the next decide carries the decision again.
    """
    try:
        return await asyncio.wait_for(
            _forward_decide_held(state, board, item_id, unless=unless), DECIDE_DEADLINE_SECS
        )
    except TimeoutError:
        return {"ok": False, "code": "crew_decide_timeout"}


async def _forward_decide_held(
    state: "DashboardState", board: str, item_id: str, *, unless: str | None
) -> dict[str, Any]:
    key = (board, item_id)
    lock = _FORWARD_LOCKS.setdefault(key, asyncio.Lock())
    try:
        async with lock:
            # The SETTLED decision: a write commits to the cache and then appends
            # to the crew log under the board lock, and undoes the commit when the
            # append fails, so a read between the two could publish a decision the
            # hub then drops. The lock is released before the peer call.
            async with _board_lock(board):
                if await _refuse_if_dirty(board, board, "work_ledger_record.remote_decide"):
                    return {"ok": False, "code": work_ledger.CODE_CACHE_DIRTY}
                item = await asyncio.to_thread(work_ledger.read_work_item, board, item_id)
            if item is None or not item.worker_instance_id:
                return {"ok": False, "code": work_ledger.CODE_UNKNOWN_ITEM}
            if unless is not None and item.decision == unless:
                return {"ok": True}
            return await _forward_one(state, item)
    finally:
        if not lock.locked() and not getattr(lock, "_waiters", None):
            _FORWARD_LOCKS.pop(key, None)


async def _forward_one(state: "DashboardState", item: work_ledger.WorkItem) -> dict[str, Any]:
    routes = _routes()
    crew = str(item.worker_instance_id or "")
    slot = str(item.worker_session_key or "")
    mgr = getattr(state, "instances_manager", None)
    if mgr is None or not crew or not slot:
        return {"ok": False, "code": "instances_manager_unavailable"}
    try:
        await routes.check_version(mgr, crew)
        status, payload = await routes.peer_call(
            mgr,
            crew,
            "PATCH",
            f"api/chat/slots/{quote(slot, safe='')}/work-item",
            body={"decision": routes._outbound(item.decision)},
        )
    except routes.CrewCallError as exc:
        return {"ok": False, "code": exc.code}
    if not 200 <= status < 300:
        code = payload.get("code") if isinstance(payload, dict) else ""
        return {
            "ok": False,
            "code": str(code)[:64] if isinstance(code, str) and code else "crew_peer_refused",
        }
    return {"ok": True}


# ── read rows ─────────────────────────────────────────────────────────────


#: The fields a remote row adds, compact rows included.
REMOTE_ROW_FIELDS: tuple[str, ...] = (
    "worker_instance_id",
    "crew_state",
    "crew_down_since",
    "pending_approval",
    "remote_error",
    "remote_reports_skipped",
)


#: The fields a row adds for an item that holds retired bindings (wave 8).
RETIRED_ROW_FIELDS: tuple[str, ...] = ("redispatch_due", "retired_crews", "close_pending")


def remote_row_fields(board: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    """The extra row fields for a remote *item*; ``{}`` for a local one. BLOCKING.

    An item that holds retired bindings adds ``redispatch_due`` (open and unbound:
    the conductor places it again with ``session_create(work_item=)``, which skips
    ``retired_crews``) whether or not it is bound again, and ``close_pending``, the
    retired peer slots not yet closed.
    """
    retired: dict[str, Any] = {}
    if item.retired_bindings:
        retired = {
            "redispatch_due": not item.is_terminal and not item.worker_session_key,
            "retired_crews": sorted(retired_crews(item)),
            "close_pending": sum(b["state"] == "retired" for b in item.retired_bindings),
        }
    if not item.worker_instance_id:
        return retired
    record = work_ledger.read_remote_state(board, item.item_id)
    record = record or work_ledger.RemoteState()
    return {
        **retired,
        "worker_instance_id": item.worker_instance_id,
        "crew_state": record.crew_state,
        "crew_down_since": record.crew_down_since,
        "pending_approval": record.pending_approval,
        "remote_error": record.error,
        # Places in the peer's reports the hub could not file, from the crew log.
        "remote_reports_skipped": peer_ingest.folded_skipped(board, item.item_id),
    }


#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = ("kiro_crew.dashboard.handlers.remote_ingest_report",)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.handlers.remote_ingest_report import (  # noqa: F401
        _RETRY_LATER,
        _SNAPSHOT_FIELDS,
        CODE_LOG_UNRECORDED,
        UNRECORDED_RETRY_SECS,
        _already_filed,
        _clean_report,
        _file_one,
        _peer_fields,
        _peer_report_fields,
        _snapshot_report,
    )


__all__ = [
    "CODE_PEER_LACKS_WORK_ITEMS",
    "CODE_RETIRE_PENDING",
    "CODE_TARGET_RETIRED",
    "CLOSE_BY_OWNER",
    "CLOSE_BY_WAKE",
    "REMOTE_ROW_FIELDS",
    "close_remote_item",
    "RETIRED_ROW_FIELDS",
    "create_bound",
    "forward_decide",
    "holds_peer_slot",
    "holds_retired_slot",
    "mark_destroyed",
    "ingest_board",
    "remote_row_fields",
    "retired_crews",
]
