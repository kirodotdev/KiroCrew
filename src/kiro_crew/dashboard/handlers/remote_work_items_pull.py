"""The peer's batch read for one hub conductor: ``POST /api/chat/work-items/pull``.

Where ``GET api/chat/slots/{slot}/work-item`` answers one item and the slot list
answers the slots, this route answers all of one hub conductor's items on this
crew in one call:

* ``items`` -- each item the hub named, with its slot's ``live`` / ``running`` /
  ``pending_approval`` / ``trust_active``, and its peer copy and events only when
  something is new since the hub's cursor (``changes``). Nothing new, no copy: a
  tick with no news carries only booleans.
* ``workers`` -- every OPEN copy of that conductor's items on this crew the hub did
  not name, as ``(item_id, key)``, with ``workers_complete`` false when the list is
  cut at :data:`MAX_WORKERS`, so a hub whose create reply was lost can find the
  worker it never bound (``remote_reclaim``).

The cursor is the hub's, folded from its crew log: per item it sends the peer event
ids it filed on that binding (newest last) and the newest peer stamp it filed. This
crew stores nothing for it. A copy is unchanged when its newest report's id is one
the hub sent, its ``last_report_at`` is the stamp the hub sent, and its worker fields
digest to the hub's (``remote_ingest_peer.worker_digest``). Otherwise the reply
holds the events from the newest report the hub filed onward, or the whole log
when it holds none of them, which is what the per-slot read with the store's cap
would hold for the hub's own filing (``remote_ingest_peer.new_reports``).

Behind the ``work_ledger.remote_dispatch`` preview (a plain 404 with it off, as the
per-slot routes), owner token only, and redacted on the way out. Reads only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from aiohttp import web

from kiro_crew import remote_dispatch_mailbox as mailbox
from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import remote_ingest_peer
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)
from kiro_crew.dashboard.handlers.remote_work_item import (
    _caller,
    _preview_on,
    remote_trust_active,
)
from kiro_crew.dashboard.handlers.work_ledger import (
    _audit,
    _board_lock,
    _refuse_400,
    _refuse_if_dirty,
)
from kiro_crew.dashboard.handlers.work_ledger_board import _redact_deep
from kiro_crew.session_ledger import ledger_key

logger = logging.getLogger(__name__)

#: Most items one pull may name: the hub's own per-read cap.
MAX_PULL_ITEMS = 32

#: Most filed peer ids the hub sends per item. Only the tail matters: the peer
#: coalesces only its newest ``progress``.
MAX_AFTER_IDS = 16

#: Most unnamed open copies listed in ``workers``.
MAX_WORKERS = 64

#: Bytes of ``changes`` one reply carries before the rest are only flagged
#: ``more``, well under the hub's 8 MiB reply cap; the hub reads those one by one.
MAX_CHANGES_BYTES = 4 * 1024 * 1024

_CODE = "invalid_value"


_Hint = tuple[str, str, tuple[str, ...], str | None, str | None]


def _hint(item_id: Any, raw: Any) -> _Hint | None:
    """``(item_id, slot, after, stamp, digest)`` for one named item, or ``None`` when
    malformed."""
    if not isinstance(item_id, str) or not isinstance(raw, dict):
        return None
    try:
        checked = work_ledger._require_item_id(item_id)
    except work_ledger.WorkLedgerError:
        return None
    slot, after, stamp = raw.get("slot"), raw.get("after", []), raw.get("stamp")
    digest = raw.get("digest")
    if not isinstance(slot, str) or not 0 < len(slot) <= work_ledger.MAX_PEER_SLOT_CHARS:
        return None
    if not isinstance(after, list) or len(after) > MAX_AFTER_IDS:
        return None
    if not all(work_ledger.is_peer_event_id(i) for i in after):
        return None
    if stamp is not None and not (isinstance(stamp, str) and len(stamp) <= 64):
        return None
    if digest is not None and not (isinstance(digest, str) and len(digest) <= 64):
        return None
    return checked, slot, tuple(after), stamp, digest


def _slot_flags(state: Any, slot_key: str) -> dict[str, bool]:
    """``live`` / ``running`` / ``pending_approval`` for *slot_key*, as the slot list
    rows say them (``slot_projection``)."""
    slot = state._slots.get(slot_key)
    if slot is None:
        return {"live": False, "running": False, "pending_approval": False}
    futures = getattr(slot, "_approval_futures", {}) or {}
    coordinator = getattr(slot, "_coordinator_approvals", None)
    pending = any(not f.done() for f in futures.values()) or bool(
        coordinator(slot.key) if callable(coordinator) else []
    )
    running = bool(getattr(slot, "turn_running", False)) or (
        getattr(slot, "linked_turn_running", False) is True
    )
    return {"live": True, "running": running, "pending_approval": pending}


def _flags(state: Any, hints: list[_Hint]) -> dict[str, dict[str, bool]]:
    """Each named item's slot booleans and ``trust_active``. On the event loop: the
    slot table and its approval futures are the loop's to change."""
    return {
        h[0]: {**_slot_flags(state, h[1]), "trust_active": remote_trust_active(h[1])} for h in hints
    }


def _copy(item: work_ledger.WorkItem) -> dict[str, Any]:
    """The worker-owned fields of a copy, as the per-slot read gives them."""
    return {
        "item_id": item.item_id,
        "state": item.state,
        "status": item.status,
        "reason": item.reason,
        "summary": item.summary,
        "artifacts": item.artifacts,
        "pr": item.pr,
        "last_report_at": item.last_report_at,
    }


def _since(
    events: list[work_ledger.WorkEvent], after: tuple[str, ...]
) -> tuple[list[work_ledger.WorkEvent], str | None]:
    """``(events from the newest report the hub filed onward, newest report id)``.

    The whole log when it holds no report the hub filed.
    """
    filed = set(after)
    reports = [i for i, e in enumerate(events) if e.kind == "report" and e.id]
    newest = events[reports[-1]].id if reports else None
    at = next((i for i in reversed(reports) if events[i].id in filed), None)
    return (events[at:] if at is not None else events), newest


def _read(
    board: str,
    hints: list[_Hint],
    flags: dict[str, dict[str, bool]],
    live: frozenset[str] | None = frozenset(),
) -> dict[str, Any]:
    """The reply body. BLOCKING; hold the board lock. *flags* are each named item's
    slot booleans, read on the event loop (:func:`_flags`). *live* is the ledger keys
    of this crew's live slots, read there too, or ``None`` before the startup restore
    has run: only a copy whose worker is live is listed, and before the restore no
    list is complete, since absence proves nothing yet."""
    items: dict[str, Any] = {}
    budget = MAX_CHANGES_BYTES
    for item_id, slot, after, stamp, digest in hints:
        row: dict[str, Any] = {**flags[item_id], "found": False}
        copy = work_ledger.read_work_item(board, item_id)
        if copy is None or copy.worker_session_key != ledger_key(slot):
            # No copy, or one another slot holds: the per-slot read's 404.
            items[item_id] = row
            continue
        row["found"] = True
        events = work_ledger.read_events(board, item_id, limit=work_ledger.MAX_EVENTS_PER_ITEM)
        tail, newest = _since(events, after)
        fields = _copy(copy)
        if (
            copy.last_report_at == stamp
            and (newest is None or newest in after)
            and digest == remote_ingest_peer.worker_digest(fields)
        ):
            items[item_id] = row
            continue
        change = {"work_item": fields, "events": [e.to_dict() for e in tail]}
        size = len(json.dumps(change, default=str))
        if size > budget:
            row["more"] = True
        else:
            budget -= size
            row["change"] = change
        items[item_id] = row
    named = {h[0] for h in hints}
    workers: list[dict[str, str]] = []
    complete = live is not None
    for copy in work_ledger.list_work_items(board):
        if copy.item_id in named or copy.is_terminal or not copy.worker_session_key:
            continue
        if live is None or copy.worker_session_key not in live:
            # No live worker (yet): a copy the peer's own stranded-copy cleanup
            # frees, never one to bind.
            continue
        if len(workers) >= MAX_WORKERS:
            # Cut at the cap: the hub must not read a copy missing here as absent.
            complete = False
            break
        workers.append({"item_id": copy.item_id, "key": copy.worker_session_key})
    return {"items": items, "workers": workers, "workers_complete": complete}


def _redacted(doc: dict[str, Any]) -> Any:
    """*doc* with every key and string redacted. BLOCKING."""
    return _redact_deep(doc)


async def api_chat_work_items_pull(request: web.Request) -> web.Response:
    """POST /api/chat/work-items/pull -- one hub conductor's items on this crew."""
    if not await _preview_on():
        raise web.HTTPNotFound()
    operation = "chat_work_items.pull"
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    conductor = body.get("conductor_key")
    raw_items = body.get("items", {})
    if (
        not isinstance(conductor, str)
        or not 0 < len(conductor) <= mailbox.MAX_CONDUCTOR_KEY_CHARS
        or not work_ledger._slot_key_is_shaped(conductor)
    ):
        return _refuse_400(_CODE, "conductor_key must be a slot key", field="conductor_key")
    if not isinstance(raw_items, dict) or len(raw_items) > MAX_PULL_ITEMS:
        return _refuse_400(
            _CODE, f"items must be an object of at most {MAX_PULL_ITEMS}", field="items"
        )
    hints = []
    for item_id, raw in raw_items.items():
        hint = _hint(item_id, raw)
        if hint is None:
            return _refuse_400(_CODE, f"item {str(item_id)[:64]!r} is malformed", field="items")
        hints.append(hint)
    board = mailbox.mailbox_key(conductor)
    state = request.app["state"]
    async with _board_lock(board):
        dirty = await _refuse_if_dirty(board, _caller(request), operation)
        if dirty is not None:
            return dirty
        try:
            live = (
                frozenset(ledger_key(k) for k in state._slots)
                if getattr(state, "open_slots_restored", False)
                else None
            )
            doc = await asyncio.to_thread(_read, board, hints, _flags(state, hints), live)
        except (OSError, work_ledger.WorkLedgerError):
            logger.warning("remote work items pull: mailbox read failed", exc_info=True)
            return web.json_response(
                {"error": "mailbox read failed; try again", "code": "ledger_read_failed"},
                status=503,
            )
    _audit(_caller(request), operation, "ok", resources=f"{len(hints)} items")
    # ``_redact_deep`` runs every key and string through ``redact_via_context``,
    # as the per-slot read does.
    return web.json_response(await asyncio.to_thread(_redacted, doc))


__all__ = ["api_chat_work_items_pull"]
