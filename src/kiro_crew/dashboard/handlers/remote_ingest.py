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
  then file every peer report newer than the binding's last filed one, in order, as
  normal recorded reports stamped with the PEER's time, so a repeat read adds nothing.
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
import functools
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from kiro_crew import platform_compat, work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard import trace_binding
from kiro_crew.dashboard.handlers.work_ledger import (
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _acting_unit,
    _audit,
    _board_header,
    _board_lock,
    _conductor_unit,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_recorded,
    _refuse_if_dirty,
    _refuse_unrecorded,
)
from kiro_crew.tracing import work as trace_work
from kiro_crew.validation import (
    WORK_REPORT_REMOTE_DISPATCH_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_vocab import WORK_REASON_STATUSES

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Peer events asked for per pull: the peer route's own default tail. The reports
#: in it the hub has not filed yet are filed in order before the latest snapshot,
#: so a ``question`` followed by a ``progress`` between two pulls still wakes.
PULL_EVENT_LIMIT = 50

#: Most earlier reports one pull files before the snapshot. The probe reads an
#: eight-event tail (``probes.work_ledger._EVENT_TAIL``), so these plus the
#: snapshot stay inside it and each one can still wake the conductor.
#: shortcut: a worker that reports more than this between two pulls loses the
#: oldest; raise it with the probe's tail if pulls ever get that far apart.
MAX_EARLIER_REPORTS = 6

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


def _is_remote_open(item: work_ledger.WorkItem) -> bool:
    return bool(item.worker_instance_id) and not item.is_terminal and bool(item.worker_session_key)


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


def _already_filed(item: work_ledger.WorkItem, cleaned: dict[str, Any]) -> bool:
    """Whether *item* already holds exactly this report as its newest one, every
    worker field alike.

    The item is the authoritative record (rebuilt from the crew log), so this is
    the whole dedupe: a repeat read of an unchanged peer files nothing.
    """
    return (
        item.last_report_at == cleaned.get("reported_at")
        and item.status == cleaned.get("status")
        and item.summary == (cleaned.get("summary") or "")
        and item.reason == cleaned.get("reason")
        and item.artifacts == (cleaned.get("artifacts") or {})
        # A report without a pr leaves the item's pr as it was.
        and (cleaned.get("pr") is None or item.pr == cleaned.get("pr"))
    )


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
    board lock. Each crew's slot list is read once; each item's peer copy once.
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
    state: "DashboardState", board: str, remote: list[work_ledger.WorkItem]
) -> None:
    mgr = getattr(state, "instances_manager", None)
    slot_lists: dict[str, dict[str, dict[str, bool]] | None] = {}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + READ_DEADLINE_SECS
    for item in remote:
        left = deadline - loop.time()
        if left <= 0:
            # Out of time for this read: these items keep their last state.
            break
        try:
            await asyncio.wait_for(
                _ingest_item(state, mgr, board, item, slot_lists), min(PULL_TIMEOUT_SECS, left)
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
    """``{slot key: {running, pending_approval}}`` for a crew, or ``None`` when unread.

    Only keys and two booleans are taken from the rows, so no peer text reaches
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
    try:
        status, payload = await routes.peer_call(
            mgr,
            crew,
            "GET",
            f"api/chat/slots/{quote(slot, safe='')}/work-item",
            params={"limit": str(PULL_EVENT_LIMIT)},
        )
    except routes.CrewCallError as exc:
        if exc.status == 503:
            await _mark_down(board, item.item_id, "disconnected", exc.code)
        else:
            await _mark_error(board, item.item_id, exc.code)
        return
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
        elif _unrecorded_hold(record, payload):
            # This snapshot's filing failed to reach the crew log moments ago: keep the
            # error and wait out the window rather than audit the same failure again.
            record.error = "crew_log_unrecorded"
        else:
            floor = _report_floor(record, item)
            record.error = await _ingest_report_locked(
                state, board, unit, item.item_id, crew, slot, payload, floor, record
            )
            failed = record.error == "crew_log_unrecorded"
            record.unrecorded_key = _snapshot_key(payload) if failed else None
            record.unrecorded_at = _now() if failed else None
            peer = payload.get("work_item")
            peer_stamp = peer.get("last_report_at") if isinstance(peer, dict) else None
            if record.error is None and isinstance(peer_stamp, str) and _later(peer_stamp, floor):
                record.report_floor, record.report_floor_known = peer_stamp, True
            elif (
                record.error == CODE_CURSOR_UNSTORED or record.error in _RETRY_LATER
            ) and not record.report_floor_known:
                # An earlier report may still be unfiled: pin this pull's floor, or the
                # next one falls back to the snapshot's stamp and skips it.
                record.report_floor, record.report_floor_known = floor, True
        record.ingested_at = record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item.item_id, record)
    # A conductor that left Trust ends the grant its worker still holds (RFC §4).
    # Asked only when the peer says it holds one, so an older peer gets no new field.
    if (
        status == 200
        and isinstance(payload, dict)
        and payload.get("trust_active") is True
        and not conductor_trusts(state, board)
    ):
        await revoke_peer_trust(mgr, crew, slot)


#: Seconds before a pull retries a peer stamp whose filing the crew log refused.
UNRECORDED_RETRY_SECS = 600.0


#: The peer item fields a snapshot's retry key covers: its stamp and every worker field.
_SNAPSHOT_FIELDS = ("last_report_at", "status", "summary", "reason", "artifacts", "pr")


def _snapshot_key(payload: dict[str, Any]) -> str | None:
    """A short digest of the peer worker's latest snapshot, or ``None`` with no stamp.

    Two reports in the same second share a stamp, so the stamp alone cannot tell a
    changed snapshot from a repeat; the digest covers every worker field too.
    """
    peer = payload.get("work_item")
    if not isinstance(peer, dict):
        return None
    stamp = peer.get("last_report_at")
    if not isinstance(stamp, str) or not stamp:
        return None
    fields = {name: peer.get(name) for name in _SNAPSHOT_FIELDS}
    raw = json.dumps(fields, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


def _unrecorded_hold(record: work_ledger.RemoteState, payload: dict[str, Any]) -> bool:
    """Whether this pull skips filing: the same peer snapshot failed to record within
    :data:`UNRECORDED_RETRY_SECS`. A changed snapshot is always filed."""
    key = _snapshot_key(payload)
    if not key or key != record.unrecorded_key or not record.unrecorded_at:
        return False
    since = _stamp(record.unrecorded_at)
    if since is None:
        return False
    age = (datetime.now(timezone.utc) - since).total_seconds()
    return 0 <= age < UNRECORDED_RETRY_SECS


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


def _stamp(value: Any) -> Any:
    return work_ledger.parse_stamp(value) if isinstance(value, str) and value else None


def _later(a: str | None, b: str | None) -> bool:
    """Whether stamp *a* is strictly after *b*; an absent *b* is before everything."""
    moment = _stamp(a)
    if moment is None:
        return False
    floor = _stamp(b)
    return floor is None or moment > floor


def _report_floor(record: work_ledger.RemoteState, item: work_ledger.WorkItem) -> str | None:
    """The current binding's report floor: a peer snapshot no newer is not news.

    The floor belongs to the binding (crew + slot), never to the item. A record
    that holds it answers it. Without one, an item that was never retired falls
    back to its ``last_report_at`` (a record from before the per-binding floor);
    a retired item has no floor, since its ``last_report_at`` is a retired
    worker's stamp from another crew's clock. Only the current pair is ever
    pulled, so no floor lets a retired worker's report through.
    """
    if record.report_floor_known:
        return record.report_floor
    return None if item.retired_bindings else item.last_report_at


def _snapshot_report(
    item: work_ledger.WorkItem, payload: dict[str, Any], floor: str | None = None
) -> dict[str, Any] | None:
    """The peer worker's LATEST report as report fields plus its stamp, or ``None``.

    The peer item holds the worker's current ``status``, ``summary``, ``artifacts``,
    ``pr`` and ``reason``, stamped with the peer's ``last_report_at``; it is filed
    last, after the earlier reports :func:`_earlier_reports` finds in the event
    tail. A snapshot older than the binding's floor is not news.
    """
    peer = payload.get("work_item")
    if not isinstance(peer, dict) or peer.get("status") is None:
        return None
    stamp = peer.get("last_report_at")
    if not isinstance(stamp, str) or not stamp or _later(floor, stamp):
        return None
    row: dict[str, Any] = {
        "status": peer.get("status"),
        "summary": peer.get("summary") if isinstance(peer.get("summary"), str) else "",
        "reported_at": stamp,
    }
    for name in ("artifacts", "pr"):
        if peer.get(name) is not None:
            row[name] = peer.get(name)
    if peer.get("reason") is not None and peer.get("status") in WORK_REASON_STATUSES:
        # A reason rides only on blocked / question, as a local report's does.
        row["reason"] = peer.get("reason")
    return row


def _clean_report(row: dict[str, Any]) -> dict[str, Any] | None:
    """*row* through the report route's own schema and redaction, or ``None``."""
    fields = {k: row[k] for k in ("status", "summary", "artifacts", "pr", "reason") if k in row}
    if not fields.get("artifacts"):
        fields.pop("artifacts", None)
    try:
        # The peer's own report schema with the preview on, which ingest always is:
        # a ``reply`` the worker could file must be one the hub can file too.
        cleaned = validate_tool_args(fields, WORK_REPORT_REMOTE_DISPATCH_SCHEMA)
        cleaned = crew_log_emit.safe_work_fields(cleaned)
    except (ValidationError, crew_log_emit.WorkFieldError):
        return None
    cleaned["reported_at"] = row.get("reported_at")
    return cleaned


#: Longest peer event id a cursor holds. A cursor is ``<before>/<id>``, two ids in
#: the record's 64-character text cap; a real id is 16 hex characters
#: (``work_vocab.work_event_id``).
_MAX_EVENT_ID_CHARS = 31


def _cursor_at(reports: list[dict[str, Any]], index: int) -> str:
    """The cursor for "filed up to ``reports[index]``": its id, after the id of the
    report before it when there is one.

    The peer coalesces two progress reports in a row (``_coalesce_progress``), so
    the newest filed report can vanish from the next tail, replaced in place by the
    progress after it. The report before it stays, and says where to resume; an
    empty one says no report came before it in the tail.
    """
    before = reports[index - 1]["_id"] if index > 0 else ""
    return f"{before}/{reports[index]['_id']}"


def _tail_reports(payload: dict[str, Any], stamp: str) -> list[dict[str, Any]]:
    """The peer tail's report events, oldest first, each cleaned as a report row.

    Only what passes the report route's own schema and redaction is kept, with its
    event id under ``_id``; an event of another kind, a wrong type, an unparsable
    stamp or an id no cursor can hold is dropped. A row stamped after *stamp* (the
    snapshot's) is kept only to be recognised, never filed: every earlier report
    precedes the snapshot, and the snapshot's own event may be stamped a second
    after it (two clock reads). Only the newest :data:`PULL_EVENT_LIMIT` entries
    are read, whatever the peer sent.
    """
    events = payload.get("events")
    limit = _stamp(stamp)
    if not isinstance(events, list) or limit is None:
        return []
    out: list[dict[str, Any]] = []
    for event in events[-PULL_EVENT_LIMIT:]:
        if not isinstance(event, dict) or event.get("kind") != "report":
            continue
        ts, text, status = event.get("ts"), event.get("text"), event.get("status")
        event_id = event.get("id")
        if not all(isinstance(value, str) for value in (ts, text, status)):
            continue
        if not isinstance(event_id, str) or not 0 < len(event_id) <= _MAX_EVENT_ID_CHARS:
            continue
        if "/" in event_id:
            continue
        moment = _stamp(ts)
        if moment is None:
            continue
        cleaned = _clean_report({"status": status, "summary": text, "reported_at": ts})
        if cleaned is None:
            continue
        cleaned["_id"] = event_id
        cleaned["_after_snapshot"] = moment > limit
        out.append(cleaned)
    return out


def _earlier_reports(
    payload: dict[str, Any],
    snapshot: dict[str, Any],
    floor: str | None,
    cursor: str | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """``(earlier, newest)``: the peer reports to file before *snapshot*, oldest
    first, each with its ``_cursor``, and the cursor past the newest report.

    The peer's events carry a report's status and summary only, so these are filed
    without reason, artifacts or pr; the snapshot after them restores the latest.

    *cursor* (``RemoteState.report_event_id``, :func:`_cursor_at`) names the newest
    report the last pull got past; everything after it is new. When the tail no
    longer holds it but holds the report before it, that report coalesced into the
    progress after it, so everything after the report before it is new. With
    neither (no cursor, or the tail rolled past both), every report stamped after
    *floor* is new. The newest report, when it says what the snapshot says, is the
    snapshot's own event and is filed only as the snapshot.
    """
    stamp = snapshot.get("reported_at")
    reports = _tail_reports(payload, stamp) if isinstance(stamp, str) else []
    if not reports:
        return [], None
    for index in range(len(reports)):
        reports[index]["_cursor"] = _cursor_at(reports, index)
    ids = [row["_id"] for row in reports]
    before, mark, last_id = (cursor or "").rpartition("/")
    events = payload.get("events")
    whole_log = isinstance(events, list) and len(events) < PULL_EVENT_LIMIT
    if last_id and last_id in ids:
        fresh = reports[ids.index(last_id) + 1 :]
    elif before and before in ids:
        fresh = reports[ids.index(before) + 1 :]
    elif mark and not before and whole_log:
        # The filed report was the first in its tail and coalesced away, and this
        # tail is the peer's whole log: nothing in it was filed.
        fresh = reports
    else:
        fresh = [row for row in reports if _later(row["reported_at"], floor)]
    last = fresh[-1] if fresh else None
    if (
        last is not None
        and last["status"] == snapshot.get("status")
        and (last.get("summary") or "") == (snapshot.get("summary") or "")
    ):
        fresh = fresh[:-1]
    earlier = [row for row in fresh if not row["_after_snapshot"]]
    return earlier[-MAX_EARLIER_REPORTS:], reports[-1]["_cursor"]


async def _ingest_report_locked(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    crew: str,
    slot: str,
    payload: dict[str, Any],
    floor: str | None,
    record: work_ledger.RemoteState,
) -> str | None:
    """File the peer worker's reports since the last pull on *item_id*. HOLD THE BOARD LOCK.

    *floor* is the current binding's newest filed peer stamp, and *record* its
    remote record, whose ``report_event_id`` cursor this moves.

    ``None`` or an error code. The earlier reports after the cursor are filed
    first, in order (:func:`_earlier_reports`), then the snapshot, unless no
    earlier one was filed and the hub item already holds exactly those worker
    fields and that stamp, so a repeat read files nothing. The cursor is stored
    BEFORE each filing, in the same drained step, and a cursor that does not store
    files no further earlier report (the snapshot still files): each earlier report
    is filed at most once, even across a cut-off pull or a remote record that will
    not write. An earlier report the crew log did not record, or whose ledger write
    failed (:data:`_RETRY_LATER`), gets its cursor back
    and stops the earlier reports, so a later pull refiles it. Any other earlier
    report that fails to file is passed over, never retried, so it cannot hold back
    the latest; the pull records its code. The snapshot needs no cursor to be retried: the hub item is
    its dedupe.
    """
    if await _refuse_if_dirty(board, board, "work_ledger_read.ingest") is not None:
        return work_ledger.CODE_CACHE_DIRTY
    current = await asyncio.to_thread(work_ledger.read_work_item, board, item_id)
    if (
        current is None
        or not _is_remote_open(current)
        or current.worker_session_key != slot
        or current.worker_instance_id != crew
    ):
        # The item is not bound to the crew and slot this pull read.
        return None
    row = _snapshot_report(current, payload, floor)
    if row is None:
        return None
    cleaned = _clean_report(row)
    if cleaned is None:
        return "crew_report_malformed"
    stamp = cleaned.get("reported_at")
    if not isinstance(stamp, str) or work_ledger.parse_stamp(stamp) is None:
        # ``apply_remote_report`` refuses the same stamp; refusing it here names why.
        return "crew_report_malformed"
    earlier, newest = _earlier_reports(payload, cleaned, floor, record.report_event_id)

    async def _move_then_file(report: dict[str, Any] | None, cursor: str | None) -> str | None:
        before = record.report_event_id
        if cursor is not None and cursor != before:
            record.report_event_id = cursor
            if not await asyncio.to_thread(_store, board, item_id, record):
                record.report_event_id = before
                if report is not cleaned:
                    return CODE_CURSOR_UNSTORED
                # The snapshot's dedupe is the hub item, not the cursor: it still files.
        if report is None:
            return None
        try:
            error = await _file_one(state, board, unit, item_id, slot, report)
        except Exception:
            # Not filed (a read or write that raised): give the cursor back, as below.
            record.report_event_id = before
            await asyncio.to_thread(_store, board, item_id, record)
            raise
        if error in _RETRY_LATER:
            # The write may succeed later: give the cursor back so a later pull refiles
            # it, the snapshot's own event included.
            record.report_event_id = before
            await asyncio.to_thread(_store, board, item_id, record)
        return error

    refused: str | None = None
    stopped = False
    filed_earlier = False
    for report in earlier:
        # One report that cannot file (too large, a refused write) must not stall
        # the ones after it: it is passed over, and its code is what the pull records.
        error = await _drain_before_cancelling(_move_then_file(report, report["_cursor"]))
        if error == CODE_CURSOR_UNSTORED or error in _RETRY_LATER:
            # Not filed and not passed over: no more earlier reports this pull. The
            # snapshot still files below.
            refused, stopped = error, True
            break
        filed_earlier = filed_earlier or error is None
        refused = refused or error
    snapshot: dict[str, Any] | None = cleaned
    if not filed_earlier and _already_filed(current, cleaned):
        snapshot = None
    # An earlier report that stopped the loop is still unfiled: keep the cursor
    # before it, so the next pull replays it instead of skipping to the newest.
    cursor = None if stopped else newest
    error = await _drain_before_cancelling(_move_then_file(snapshot, cursor))
    return error or refused


#: A pull that could not store its cursor, and so filed nothing more.
CODE_CURSOR_UNSTORED = "crew_cursor_unstored"

#: A filing the crew log did not record; the ledger write was undone.
CODE_LOG_UNRECORDED = "crew_log_unrecorded"

#: Filing failures that may clear on their own: the report keeps its place.
_RETRY_LATER = (CODE_LOG_UNRECORDED, "ledger_write_failed")


async def _file_one(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    slot: str,
    cleaned: dict[str, Any],
) -> str | None:
    """One ingested report: probe, commit, record (board copy). HOLD THE BOARD LOCK."""
    probe_item, probe_board = await asyncio.to_thread(
        work_ledger.read_work_item, board, item_id
    ), await asyncio.to_thread(work_ledger.read_conductor, board)
    probe, invalid = _entry_probe_with_baseline(
        board,
        probe_item,
        probe_board,
        actor="worker",
        by=slot,
        action="report",
        item_id=item_id,
        generation=_WIDEST_HEX_ID,
        round=getattr(probe_item, "round", 0),
        status=cleaned.get("status"),
        reason=cleaned.get("reason"),
        summary=cleaned.get("summary"),
        artifacts=cleaned.get("artifacts") or {},
        pr=work_ledger.MAX_PR,
        last_report_at=_WIDEST_STAMP,
        event=_WIDEST_EVENT_TEXT,
        event_kind="report",
        event_id=_WIDEST_HEX_ID,
        event_ts=_WIDEST_STAMP,
        board_copy=True,
    )
    if invalid is not None or probe is None or not crew_log_emit.work_entry_fits(probe):
        return "work_entry_too_large"
    snapshot = await asyncio.to_thread(
        functools.partial(work_ledger.snapshot_for_write, board, item_id=item_id)
    )
    try:
        result = await asyncio.to_thread(
            functools.partial(
                work_ledger.apply_remote_report,
                board,
                item_id,
                status=cleaned.get("status"),
                summary=cleaned.get("summary"),
                artifacts=cleaned.get("artifacts"),
                pr=cleaned.get("pr"),
                reason=cleaned.get("reason"),
                reported_at=cleaned.get("reported_at"),
            )
        )
    except work_ledger.WorkLedgerError as exc:
        return exc.code or "crew_report_refused"
    except OSError:
        return "ledger_write_failed"
    item, event = result["item"], result["event"]
    header = await asyncio.to_thread(work_ledger.read_conductor, board)
    established, header = await _board_header(board, header)
    landed = established and await asyncio.to_thread(
        crew_log_emit.on_work_recorded,
        unit,
        {
            **_entry_with_baseline(
                board,
                item,
                header,
                actor="worker",
                by=slot,
                action="report",
                item_id=item.item_id,
                generation=getattr(header, "generation", None) or None,
                round=item.round,
                status=item.status,
                reason=item.reason,
                summary=item.summary,
                artifacts=item.artifacts,
                pr=item.pr,
                last_report_at=item.last_report_at,
                event=event.text,
                event_kind="report",
                event_id=event.id or None,
                event_ts=event.ts or None,
            ),
            "board_copy": True,
        },
    )
    if not landed:
        await _refuse_unrecorded(board, "work_ledger_read.ingest", item.item_id, board, snapshot)
        return "crew_log_unrecorded"
    await asyncio.to_thread(_mark_recorded, board, item.item_id)
    _audit(board, "work_ledger_read.ingest", "ok", resources=f"{item_id} status={item.status}")
    # A peer's worker reported: no turn here made it, so it sits under its item.
    await trace_work.recorded(
        "",
        board,
        "report",
        actor="worker",
        item=item,
        header=header,
        event_id=event.id,
        root_kind=lambda: trace_binding.root_kind(state, board),
    )
    return None


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
            if _monotonic() - first >= minutes * 60 and await _record_retire(state, board, item):
                _DOWN_SEEN.pop(key, None)
        except Exception:  # noqa: BLE001 - a retire must never fail the read
            logger.warning("remote retire failed for %s", item.item_id, exc_info=True)
    for key in [k for k in _DOWN_SEEN if k[0] == board and k not in live_keys]:
        _DOWN_SEEN.pop(key, None)


async def _record_retire(state: "DashboardState", board: str, item: work_ledger.WorkItem) -> bool:
    """Record one ``retire`` write for *item*'s current binding. Whether it landed."""
    slot, crew = str(item.worker_session_key), str(item.worker_instance_id)
    # The board's durable crew-log unit, as a pull files reports into: the
    # background puller retires with no conductor runtime alive.
    unit, unit_error = await _board_unit(state, board)
    if unit_error is not None or not unit:
        return False
    # The record must name this binding BEFORE the retire commits, so a fresh
    # record that then fails to store leaves it recognisably stale (fenced)
    # rather than silently carried into the replacement. No stamp, no retire.
    async with _board_lock(board):
        record = await asyncio.to_thread(_stored, board, item.item_id)
        if record.binding is None:
            record.binding = work_ledger.binding_id(item)
            if not await asyncio.to_thread(_store, board, item.item_id, record):
                return False
    # The write and the steps after it run as one, finished even when this read
    # is cancelled (the puller's per-board budget): a retire that committed always
    # leaves the dispatch record and the remote record behind it.
    return bool(
        await _drain_before_cancelling(_commit_retire(state, board, unit, item, slot, crew))
    )


async def _commit_retire(
    state: "DashboardState",
    board: str,
    unit: str,
    item: work_ledger.WorkItem,
    slot: str,
    crew: str,
) -> bool:
    """Record the retire, then drop the dispatch entry and store a fresh record."""
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
                    "reason": RETIRE_CREW_UNREACHABLE,
                },
                operation="work_ledger_read.retire",
            )
            landed = resp.status == 200
            if not landed:
                logger.info(
                    "remote retire of %s not recorded (%s)", item.item_id, _response_code(resp)
                )
                return False
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
    return True


#: ``(crew, slot)`` retired slots this process already tried to close. One try
#: each, never a retry loop: a close that fails leaves the entry ``retired``
#: (fenced, close-only), and the owner's ``session_close(crew=)`` still closes it.
_CLOSE_TRIED: set[tuple[str, str]] = set()

#: The close tasks in flight, held so none is collected before it ends.
_CLOSE_TASKS: set["asyncio.Task[None]"] = set()


def _schedule_closes(
    state: "DashboardState", board: str, items: list[work_ledger.WorkItem]
) -> None:
    """Start one close per retired slot whose crew reads connected now. Never raises.

    Each close runs as its own task, so a slow crew never delays this read or
    holds back another close.
    """
    mgr = getattr(state, "instances_manager", None)
    if mgr is None:
        return
    for item in items:
        for entry in item.retired_bindings:
            if entry["state"] != "retired":
                continue
            crew, slot = entry["worker_instance_id"], entry["worker_session_key"]
            if (crew, slot) in _CLOSE_TRIED or _crew_state(mgr, crew) != "connected":
                continue
            _CLOSE_TRIED.add((crew, slot))
            task = asyncio.ensure_future(_close_retired_slot(state, mgr, board, crew, slot))
            _CLOSE_TASKS.add(task)
            task.add_done_callback(_CLOSE_TASKS.discard)


async def _close_retired_slot(
    state: "DashboardState", mgr: Any, board: str, crew: str, slot: str
) -> None:
    """Close one retired peer slot, then mark its entry ``destroyed``. Never raises.

    A 2xx, or a 404 saying the slot is gone, counts as closed. Anything else (a
    timeout, a refusal, a version skew) leaves the entry ``retired``: fail closed.
    """
    routes = _routes()

    async def _delete() -> tuple[int, Any]:
        await routes.check_version(mgr, crew)
        return await routes.peer_call(mgr, crew, "DELETE", f"api/chat/slots/{quote(slot, safe='')}")

    try:
        status, payload = await asyncio.wait_for(_delete(), PULL_TIMEOUT_SECS)
    except Exception:  # noqa: BLE001 - one failed close stays fenced, nothing more
        logger.info("retired slot %s on %s not closed", slot, crew, exc_info=True)
        return
    if 200 <= status < 300 or slot_gone(status, payload):
        await mark_destroyed(state, board, crew, slot)


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
)


#: The fields a row adds for an item that holds retired bindings (wave 8).
RETIRED_ROW_FIELDS: tuple[str, ...] = ("redispatch_due", "retired_crews")


def remote_row_fields(board: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    """The extra row fields for a remote *item*; ``{}`` for a local one. BLOCKING.

    An item that holds retired bindings adds ``redispatch_due`` (open and unbound:
    the conductor places it again with ``session_create(work_item=)``, which skips
    ``retired_crews``) whether or not it is bound again.
    """
    retired: dict[str, Any] = {}
    if item.retired_bindings:
        retired = {
            "redispatch_due": not item.is_terminal and not item.worker_session_key,
            "retired_crews": sorted(retired_crews(item)),
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
    }


__all__ = [
    "CODE_PEER_LACKS_WORK_ITEMS",
    "CODE_RETIRE_PENDING",
    "CODE_TARGET_RETIRED",
    "REMOTE_ROW_FIELDS",
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
