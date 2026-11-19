"""The peer's batch read for one hub conductor: ``POST /api/chat/work-items/pull``.

Where ``GET api/chat/slots/{slot}/work-item`` answers one item and the slot list
answers the slots, this route answers all of one hub conductor's items on this
crew in one call:

* ``items`` -- each item the hub named, with its slot's ``live`` / ``running`` /
  ``pending_approval`` / ``trust_active``, and its peer copy and events only when
  something is new since the hub's cursor (``changes``). Nothing new, no copy: a
  tick with no news carries only booleans.

The cursor is the hub's, folded from its crew log: per item it sends the peer event
ids it filed on that binding (newest last) and the newest peer stamp it filed. This
crew stores nothing for it. A copy is unchanged when its newest report's id is one
the hub sent, its ``last_report_at`` is the stamp the hub sent, and its worker fields
digest to the hub's (``remote_ingest_peer.worker_digest``). Otherwise the reply
holds the events from the newest report the hub filed onward, or the whole log
when it holds none of them, which is what the per-slot read with the store's cap
would hold for the hub's own filing (``remote_ingest_peer.new_reports``).

``POST /api/chat/work-items/wait`` takes the same body and holds the request until
one of those items has news or ``wait_secs`` runs out, answering only
``{"news": bool}`` (:func:`api_chat_work_items_wait`); the hub then pulls.

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


def _unchanged(
    copy: work_ledger.WorkItem,
    fields: dict[str, Any],
    newest: str | None,
    after: tuple[str, ...],
    stamp: str | None,
    digest: str | None,
) -> bool:
    """Whether the hub's cursor already covers *copy*: its newest report id is one
    the hub sent, its stamp is the hub's, and its worker fields digest the same."""
    return (
        copy.last_report_at == stamp
        and (newest is None or newest in after)
        and digest == remote_ingest_peer.worker_digest(fields)
    )


def _reported_past(
    copy: work_ledger.WorkItem, newest: str | None, after: tuple[str, ...], stamp: str | None
) -> bool:
    """Whether *copy* holds a report the hub has not filed.

    With filed ids, only ids decide: news is a newest report id the hub did not
    send, so a peer clock that steps back never makes a filed report look new. With
    none (a first report, or a binding from before peer ids), the stamp decides.

    The digest is left out on purpose. The hub's item can keep fields no peer
    report carries (a ``pr`` from a retired worker, a redacted summary), so a
    digest that never matches would make every wait answer at once.
    shortcut: a report that keeps its id (same second, status and summary; only
    reason, artifacts or pr changed), or a same-second report on an id-less
    binding, is not news here and reaches the hub on the patrol pull.
    """
    if after:
        return newest is not None and newest not in after
    return copy.last_report_at != stamp


def _news(board: str, hints: list[_Hint]) -> bool:
    """Whether any named item's copy holds a worker report past the hub's cursor
    (:func:`_reported_past`). BLOCKING; hold the board lock. A missing copy, or one
    another slot holds, is no news: no report can come from it."""
    for item_id, slot, after, stamp, _digest in hints:
        copy = work_ledger.read_work_item(board, item_id)
        if copy is None or copy.worker_session_key != ledger_key(slot):
            continue
        events = work_ledger.read_events(board, item_id, limit=work_ledger.MAX_EVENTS_PER_ITEM)
        _tail, newest = _since(events, after)
        if _reported_past(copy, newest, after, stamp):
            return True
    return False


def _read(board: str, hints: list[_Hint], flags: dict[str, dict[str, bool]]) -> dict[str, Any]:
    """The reply body. BLOCKING; hold the board lock. *flags* are each named item's
    slot booleans, read on the event loop (:func:`_flags`)."""
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
        if _unchanged(copy, fields, newest, after, stamp, digest):
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
    return {"items": items}


def _redacted(doc: dict[str, Any]) -> Any:
    """*doc* with every key and string redacted. BLOCKING."""
    return _redact_deep(doc)


async def _checked(
    request: web.Request, operation: str
) -> tuple[str, list[_Hint], dict[str, Any]] | web.Response:
    """``(mailbox board, hints, body)`` of an owner's batch request, or the refusal.
    Raises 404 with the preview off, as if the route did not exist."""
    if not await _preview_on():
        raise web.HTTPNotFound()
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
    return mailbox.mailbox_key(conductor), hints, body


async def api_chat_work_items_pull(request: web.Request) -> web.Response:
    """POST /api/chat/work-items/pull -- one hub conductor's items on this crew."""
    operation = "chat_work_items.pull"
    checked = await _checked(request, operation)
    if isinstance(checked, web.Response):
        return checked
    board, hints, _body = checked
    state = request.app["state"]
    async with _board_lock(board):
        dirty = await _refuse_if_dirty(board, _caller(request), operation)
        if dirty is not None:
            return dirty
        try:
            doc = await asyncio.to_thread(_read, board, hints, _flags(state, hints))
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


#: Longest a hub wait is held, in seconds. Well under every idle limit on the way:
#: the hub carrier's read idle (``DEFAULT_PROXY_READ_IDLE_TIMEOUT_SECS``, 120 s)
#: and this server's keep-alive (``slowloris.KEEPALIVE_TIMEOUT``, 75 s).
MAX_WAIT_SECS = 25.0

#: Most hub waits this crew holds open at once; one more is refused 429 and the
#: hub backs off. A hub holds one per conductor board.
MAX_WAITERS = 32


async def _news_now(board: str, hints: list[_Hint], caller: str, operation: str) -> Any:
    """``True`` / ``False`` for news on *board* now, or the refusal to answer with."""
    async with _board_lock(board):
        dirty = await _refuse_if_dirty(board, caller, operation)
        if dirty is not None:
            return dirty
        try:
            return await asyncio.to_thread(_news, board, hints)
        except (OSError, work_ledger.WorkLedgerError):
            logger.warning("remote work items wait: mailbox read failed", exc_info=True)
            return web.json_response(
                {"error": "mailbox read failed; try again", "code": "ledger_read_failed"},
                status=503,
            )


async def api_chat_work_items_wait(request: web.Request) -> web.Response:
    """POST /api/chat/work-items/wait -- hold until one of the hub's items has news.

    The body is the pull's (``conductor_key``, ``items`` with their cursors) plus
    ``wait_secs``, capped at :data:`MAX_WAIT_SECS`. Answers ``{"news": true}`` as soon
    as a worker report on this conductor's mailbox moves any named item past its
    cursor, else ``{"news": false}`` once the wait is up. It carries no copy and no
    event: the hub then reads the batch pull, as on its patrol tick. Never holds the
    board lock while it waits; each report on the board
    (``remote_ingest_peer.note_report``) re-reads once, so a quiet board costs two
    reads per wait. Same gate as the pull: preview on, owner only, reads only.
    """
    operation = "chat_work_items.wait"
    checked = await _checked(request, operation)
    if isinstance(checked, web.Response):
        return checked
    board, hints, body = checked
    wait = body.get("wait_secs", 0)
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not wait >= 0:
        return _refuse_400(_CODE, "wait_secs must be a number of seconds", field="wait_secs")
    if remote_ingest_peer.waiting_count() >= MAX_WAITERS:
        return web.json_response(
            {"error": "too many waits held; try later", "code": "too_many_waits"}, status=429
        )
    caller = _caller(request)
    loop = asyncio.get_running_loop()
    # Capped before the conversion: a huge integer would overflow float().
    deadline = loop.time() + float(min(wait, MAX_WAIT_SECS))
    with remote_ingest_peer.report_waiter(board) as reported:
        while True:
            # Cleared before the read, so a report landing during it ends the
            # next wait at once. After the wait runs out, one last read.
            reported.clear()
            news = await _news_now(board, hints, caller, operation)
            if isinstance(news, web.Response):
                return news
            left = deadline - loop.time()
            if news or left <= 0:
                break
            try:
                await asyncio.wait_for(reported.wait(), left)
            except asyncio.TimeoutError:
                pass
    if news:
        # Only a wait that found news is audited: a quiet one says nothing and comes
        # back every MAX_WAIT_SECS, which would bury the log the pull's row keeps.
        _audit(caller, operation, "ok", resources=f"{len(hints)} items")
    return web.json_response({"news": bool(news)})


__all__ = ["api_chat_work_items_pull", "api_chat_work_items_wait"]
