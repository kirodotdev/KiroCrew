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
import functools
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from aiohttp import web

from kiro_crew import work_ledger
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.dashboard.handlers.work_ledger import (
    _WIDEST_EVENT_TEXT,
    _WIDEST_HEX_ID,
    _WIDEST_STAMP,
    _acting_unit,
    _audit,
    _board_header,
    _board_lock,
    _drain_before_cancelling,
    _entry_probe_with_baseline,
    _entry_with_baseline,
    _mark_recorded,
    _refuse_if_dirty,
    _refuse_unrecorded,
)
from kiro_crew.validation import (
    WORK_REPORT_REMOTE_DISPATCH_SCHEMA,
    ValidationError,
    validate_tool_args,
)
from kiro_crew.work_vocab import WORK_REASON_STATUSES

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

#: Peer events asked for per pull. Only the peer item (the worker's latest
#: fields) is filed, so one event is the least the route accepts.
PULL_EVENT_LIMIT = 1

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
    item = await settled_item(board, item_id.strip())
    if item is None:
        raise err(404, work_ledger.CODE_UNKNOWN_ITEM, "no such item on this conductor's ledger")
    if item.is_terminal:
        raise err(409, work_ledger.CODE_ITEM_CLOSED, f"item {item.item_id} is {item.state}")
    if item.worker_session_key:
        raise err(409, "already_bound", f"item {item.item_id} is already bound")
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
    status, payload = await routes.peer_call(mgr, instance_id, "POST", "api/chat/slots", body=body)
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


def _response_code(resp: web.Response) -> str:
    import json

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
    crew_state = _crew_state(mgr, crew)
    if mgr is None or crew_state != "connected":
        await _mark_down(board, item.item_id, crew_state, "proxy_peer_not_connected")
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
    unit, refusal = _acting_unit(state, board, "work_ledger_read.ingest", item.item_id)
    async with _board_lock(board):
        # Read under the lock, so two pulls never write over each other's record.
        record = await asyncio.to_thread(_stored, board, item.item_id)
        record.crew_state = "connected"
        record.crew_down_since = None
        for name, value in observed.items():
            setattr(record, name, value)
        if status == 404:
            # No such slot, or no item on it: nothing to ingest. The slot list
            # above answers whether the session is gone.
            record.error = "crew_work_item_not_found"
        elif not 200 <= status < 300 or not isinstance(payload, dict):
            record.error = "crew_peer_refused"
        elif refusal is not None or unit is None:
            record.error = _response_code(refusal) if refusal is not None else "crew_log_off"
        else:
            record.error = await _ingest_report_locked(
                state, board, unit, item.item_id, crew, slot, payload
            )
        record.ingested_at = record.attempted_at = _now()
        await asyncio.to_thread(_store, board, item.item_id, record)


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


def _snapshot_report(item: work_ledger.WorkItem, payload: dict[str, Any]) -> dict[str, Any] | None:
    """The peer worker's LATEST report as report fields plus its stamp, or ``None``.

    Worker fields are latest-value (RFC §2): the hub files the peer item's current
    ``status``, ``summary``, ``artifacts``, ``pr`` and ``reason``, stamped with the
    peer's ``last_report_at``, and never the event history. A snapshot older than
    the hub's own ``last_report_at`` is not news. Statuses the worker passed through
    between two hub reads are not kept.
    """
    peer = payload.get("work_item")
    if not isinstance(peer, dict) or peer.get("status") is None:
        return None
    stamp = peer.get("last_report_at")
    if not isinstance(stamp, str) or not stamp or _later(item.last_report_at, stamp):
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


async def _ingest_report_locked(
    state: "DashboardState",
    board: str,
    unit: str,
    item_id: str,
    crew: str,
    slot: str,
    payload: dict[str, Any],
) -> str | None:
    """File the peer worker's latest report on *item_id*. HOLD THE BOARD LOCK.

    ``None`` or an error code. The report is filed unless the hub item already
    holds exactly those worker fields and that stamp, so a repeat read files
    nothing and a changed snapshot is filed once. Nothing else is remembered
    between reads: the item is the record.
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
    row = _snapshot_report(current, payload)
    if row is None:
        return None
    cleaned = _clean_report(row)
    if cleaned is None:
        return "crew_report_malformed"
    stamp = cleaned.get("reported_at")
    if not isinstance(stamp, str) or work_ledger.parse_stamp(stamp) is None:
        # ``apply_remote_report`` refuses the same stamp; refusing it here names why.
        return "crew_report_malformed"
    if _already_filed(current, cleaned):
        return None
    return await _drain_before_cancelling(_file_one(state, board, unit, item_id, slot, cleaned))


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
    return None


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


def remote_row_fields(board: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    """The extra row fields for a remote *item*; ``{}`` for a local one. BLOCKING."""
    if not item.worker_instance_id:
        return {}
    record = work_ledger.read_remote_state(board, item.item_id)
    record = record or work_ledger.RemoteState()
    return {
        "worker_instance_id": item.worker_instance_id,
        "crew_state": record.crew_state,
        "crew_down_since": record.crew_down_since,
        "pending_approval": record.pending_approval,
        "remote_error": record.error,
    }


__all__ = [
    "CODE_PEER_LACKS_WORK_ITEMS",
    "REMOTE_ROW_FIELDS",
    "create_bound",
    "forward_decide",
    "holds_peer_slot",
    "ingest_board",
    "remote_row_fields",
]
