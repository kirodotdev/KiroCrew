"""The conductor's read of its own board: ``GET /api/work-ledger``.

The query options and filters, the compact row, each item's event tail, and the flags
a row derives at read time -- orphaned, stale, acceptance concrete, unpatrolled and
pending overdue -- from the dashboard's own slot table and the wake gate's resolvers;
and whether a slot is a worker still holding an open item.

Served through :mod:`kiro_crew.dashboard.handlers.work_ledger`, which callers and tests
import and patch; this module never imports it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew import conductor_patrol, ledger_wake, session_ledger, work_ledger
from kiro_crew.dashboard.handlers.work_ledger_caller import (
    _caller_key,
    _contained_channel_caller,
    _own_ledger,
)
from kiro_crew.dashboard.handlers.work_ledger_recording import _board_lock, _refuse_if_dirty
from kiro_crew.dashboard.handlers.work_ledger_refusals import (
    _audit,
    _refuse_400,
    _refuse_403,
    _validation_code,
)
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import WORK_LEDGER_READ_SCHEMA, ValidationError, validate_tool_args

logger = logging.getLogger("kiro_crew.dashboard.handlers.work_ledger")


# ── conductor half ────────────────────────────────────────────────────────


async def api_work_ledger_get(request: web.Request) -> web.Response:
    """GET /api/work-ledger — the ledger this session owns.

    The conductor record, every item with all its fields, the derived
    ``orphaned`` / ``stale`` / ``acceptance_concrete`` flags, each item's newest
    events, and a ready-to-pipe ``accept_batch`` built from ``acceptance`` ALONE —
    never from a worker's claimed ``pr``, which is surfaced beside the item instead.

    ``accept_batch`` holds only the items whose bar is concrete, so an item still
    carrying a ``"TBD"`` pull request number is absent from it; ``acceptance_concrete``
    on the item row is why. Each entry carries the item's ``status`` so the conductor
    can apply its own "``done`` only" filter without a second lookup — the filter stays
    the conductor's to apply.

    With no query the answer is the whole board. Every parameter NARROWS it:
    ``events=<n>`` shortens each tail, ``item_id`` / ``state`` / ``since`` select
    rows (``accept_batch`` stays built from the WHOLE board, because its contract
    is the whole bar), and ``compact=true`` returns the status columns alone, the
    derived flags among them, with no events, acceptance or batch. The route does
    not size the reply: the MCP tool that carries it to a model fits it to that
    runtime's cut (``mcp_work._fit_ledger``), measured on the text it delivers.
    """
    key, refusal = await _caller_key(request, "work_ledger_read")
    if refusal is not None:
        return refusal
    assert key is not None
    options, bad = _read_options(request)
    if bad is not None:
        return bad
    assert options is not None
    compact = bool(options.get("compact", False))
    tail = int(options.get("events", _MAX_EVENT_TAIL))
    remote_on = await asyncio.to_thread(_remote_dispatch_on)
    if remote_on:
        # Remote dispatch (preview): pull each open remote item's peer copy and file
        # what is new BEFORE the snapshot below, outside its lock (the pull is network).
        from kiro_crew.dashboard.handlers import remote_ingest

        await remote_ingest.ingest_board(request.app["state"], key)
    # Under the board lock for the same reason the brief read is: a write's
    # cache commit and its record append are two steps, and a read between them
    # would publish a mutation the record may yet roll back. The header and the
    # items are read under one hold, so the response is one snapshot.
    async with _board_lock(key):
        record, lrefusal = await _own_ledger(key, "work_ledger_read")
        if lrefusal is not None:
            return lrefusal
        dirty = await _refuse_if_dirty(key, key, "work_ledger_read")
        if dirty is not None:
            return dirty
        items = await asyncio.to_thread(work_ledger.list_work_items, key)
        shown = [item for item in items if _passes_filters(item, options)]
        event_tails: dict[str, list[work_ledger.WorkEvent]] = {}
        if not compact:
            for item in shown:
                event_tails[item.item_id] = await asyncio.to_thread(
                    _tail_events, key, item.item_id, tail
                )
    assert record is not None

    state: DashboardState = request.app["state"]
    # Liveness is read straight off the dashboard's own slot table rather than
    # over HTTP: this handler runs in the process that owns it. ``orphaned`` asks
    # whether the CONDUCTOR's slot is still open and ``stale`` whether the
    # WORKER's is — the conjunction with the staleness window is what keeps a
    # worker in a thirty-minute build from being flagged. Derived for the compact
    # read too: a patrol that reads only the status columns must still see a dead
    # worker.
    conductor_alive = _slot_open(state, key)
    # One read for the whole board: an open item whose conductor holds no active
    # work-ledger watch has nobody reading its reports, and the flag puts that in
    # front of the conductor's next turn.
    patrolled = conductor_patrol.has_active_patrol(state, key)
    undelivered = await asyncio.to_thread(
        lambda: {item.item_id: work_ledger.read_undelivered(key, item) for item in shown}
    )
    remote_live = await asyncio.to_thread(work_ledger.remote_liveness_map, key, shown)
    rows: list[dict[str, Any]] = []
    for item in shown:
        row = _row_with_flags(
            state,
            item,
            conductor_alive=conductor_alive,
            patrolled=patrolled,
            report_undelivered=undelivered.get(item.item_id),
            board=key,
            remote_live=remote_live.get(item.item_id),
        )
        if item.worker_instance_id:
            row.update(await asyncio.to_thread(_remote_row_fields, key, item))
        if compact:
            compact_row = {name: row[name] for name in _COMPACT_ROW_FIELDS}
            if item.worker_instance_id:
                from kiro_crew.dashboard.handlers.remote_ingest import REMOTE_ROW_FIELDS

                compact_row.update({name: row[name] for name in REMOTE_ROW_FIELDS})
            rows.append(compact_row)
            continue
        events = event_tails[item.item_id]
        row["events"] = [event.to_dict() for event in events]
        rows.append(row)

    if why := _contained_channel_caller(request, request.headers.get("X-Session-Key", "")):
        # Same post-await re-check as ``work_brief``. This payload is larger: every
        # item's acceptance bar plus worker-authored prose for the whole fleet.
        _audit(
            request.headers.get("X-Session-Key", "") or "anonymous",
            "work_ledger_read",
            "denied",
            resources="channel_agent_block_post_read",
        )
        return _refuse_403(
            "channel_session",
            "This session stopped being a private surface while the ledger was being "
            f"read, so it is no longer one to return it to: {why}.",
        )
    _audit(key, "work_ledger_read", "ok", resources=f"{len(rows)} item(s)")
    # ``goal_items_used`` and ``cap_exceeded`` are derived, never stored: the
    # number the conductor's "items used: N of <item_cap>" line reports, and
    # whether that line has gone past its cap, both read off the same header.
    # Advisory: the compact read carries them so a patrol that reads only the
    # status columns still sees a goal spending past what the user expected.
    # ``max_depth`` is the conducting depth cap in force right now (never stored),
    # so a conductor can see how many levels may still dispatch below its own.
    # Read off the loop: it consults the config file.
    depth_cap = await asyncio.to_thread(work_ledger.max_depth)
    conductor = dict(
        record.to_dict(),
        goal_items_used=record.goal_items_used,
        cap_exceeded=record.cap_exceeded,
        max_depth=depth_cap,
    )
    payload: dict[str, Any] = {"conductor": conductor, "items": rows}
    if compact:
        payload["compact"] = True
    else:
        # The whole board, never the filtered rows.
        payload["accept_batch"] = work_ledger.accept_batch(items)
    return web.json_response(payload)


def _row_with_flags(
    state: DashboardState,
    item: work_ledger.WorkItem,
    *,
    conductor_alive: bool,
    patrolled: bool,
    report_undelivered: dict[str, str] | None = None,
    board: str = "",
    remote_live: tuple[bool, bool] | None = None,
) -> dict[str, Any]:
    """One item as a read row, with the flags the store derives at read time.

    A function rather than four lines inlined per caller because the read route and
    the wake snapshot both publish these flags and must publish the SAME ones: a
    snapshot that said a worker was alive while the route called it stale would be a
    second opinion on the one question a conductor acts on.

    ``report_undelivered`` is ``{"tool", "at"}`` while the worker's last
    ``work_report`` / ``work_brief`` call failed before reaching the ledger (the
    harness could not resolve it) and none has got through since, else ``None``
    (:func:`kiro_crew.work_ledger.read_undelivered`, read by the caller
    off the event loop). It is what tells "couldn't send its report" apart from a
    worker that went quiet.

    Liveness is read straight off the dashboard's own slot table rather than over
    HTTP: every caller runs in the process that owns it. ``orphaned`` asks whether
    the CONDUCTOR's slot is still open and ``stale`` whether the WORKER's is -- the
    conjunction with the staleness window is what keeps a worker in a thirty-minute
    build from being flagged.

    ``acceptance_concrete`` says why an item is (or is not) in ``accept_batch``.
    Without it a conductor sees an item it dispatched simply missing from the batch
    and has no way to tell "bar not filled in yet" from "the read dropped it".

    ``unpatrolled`` marks an open item whose conductor holds no active work-ledger
    watch -- nobody is reading its reports -- and ``patrolled`` is that one read for
    the whole board, passed in rather than re-read per item.

    ``pending_overdue`` marks an item whose acceptance has read ``pending`` past the
    budget, so the conductor stops polling it and asks the user.
    """
    row = item.to_dict()
    row["orphaned"] = work_ledger.is_orphaned(item, conductor_slot_exists=conductor_alive)
    running, closed, looping = work_ledger.item_liveness(
        board,
        item,
        running=lambda k: _slot_running(state, k),
        closed=lambda k: _slot_closed(state, k),
        looping=_slot_looping,
        remote=remote_live if item.worker_instance_id else None,
    )
    row["stale"] = work_ledger.is_stale(
        item, worker_running=running, worker_closed=closed, worker_looping=looping
    )
    row["acceptance_concrete"] = work_ledger.is_acceptance_concrete(item.acceptance)
    row["unpatrolled"] = item.state == "open" and not patrolled
    row["pending_overdue"] = work_ledger.is_pending_overdue(item)
    row["report_undelivered"] = report_undelivered
    return row


#: The fields a ``compact=true`` row carries: what a patrol cycle reads to decide
#: who moves next, the derived flags included, and nothing that is a
#: document in its own right (``acceptance``, ``artifacts``, the events).
_COMPACT_ROW_FIELDS: tuple[str, ...] = (
    "item_id",
    "title",
    "state",
    "created_at",
    "status",
    "reason",
    "summary",
    "decision",
    "verdict",
    "pr",
    "worker_session_key",
    "last_report_at",
    "orphaned",
    "stale",
    "acceptance_concrete",
    "unpatrolled",
    "pending_overdue",
    "report_undelivered",
)

#: Query-string spellings of ``compact``. ``true`` / ``false`` is what the tool
#: layer sends.
_QUERY_TRUE = frozenset({"1", "true", "yes", "on"})
_QUERY_FALSE = frozenset({"0", "false", "no", "off"})


def _read_options(
    request: web.Request,
) -> tuple[dict[str, Any], None] | tuple[None, web.Response]:
    """The read's query parameters, validated against ``WORK_LEDGER_READ_SCHEMA``.

    A query string carries only text, so ``events`` and ``compact`` are coerced to
    the schema's types first; the rest is the schema's own check, the same one the
    tool layer applies. An unknown or empty parameter is refused rather than
    ignored: a filter that silently did not apply would hand back the whole board
    as if it were the narrowed one.
    """
    raw: dict[str, Any] = {}
    for name in request.query:
        value = request.query.get(name, "")
        if value == "":
            return None, _refuse_400(
                work_ledger.CODE_INVALID_VALUE, f"{name}: expected a value", name
            )
        if name == "events":
            try:
                raw[name] = int(value)
            except ValueError:
                return None, _refuse_400(
                    work_ledger.CODE_INVALID_VALUE, "events: expected an integer", "events"
                )
        elif name == "compact":
            lowered = value.strip().lower()
            if lowered not in _QUERY_TRUE | _QUERY_FALSE:
                return None, _refuse_400(
                    work_ledger.CODE_INVALID_VALUE, "compact: expected true or false", "compact"
                )
            raw[name] = lowered in _QUERY_TRUE
        else:
            raw[name] = value
    try:
        cleaned = validate_tool_args(raw, WORK_LEDGER_READ_SCHEMA)
    except ValidationError as exc:
        # An unknown key is a wrong VALUE for the query; the store-code mapping
        # reads the field name and would call a stray ``status=`` an
        # ``invalid_status``.
        unknown = "unknown field" in (exc.message or "")
        code = work_ledger.CODE_INVALID_VALUE if unknown else _validation_code(exc)
        return None, _refuse_400(code, str(exc), exc.field)
    since = cleaned.get("since")
    if since is not None:
        moment = work_ledger.parse_stamp(since)
        if moment is None:
            return None, _refuse_400(
                work_ledger.CODE_INVALID_VALUE, "since: expected an ISO-8601 timestamp", "since"
            )
        cleaned["since"] = moment
    return cleaned, None


def _passes_filters(item: work_ledger.WorkItem, options: dict[str, Any]) -> bool:
    """Whether *item* survives the read's ``item_id`` / ``state`` / ``since`` filters.

    ``since`` is judged on the newest of ``created_at``, ``last_report_at`` and
    ``closed_at``, so it answers "what moved since my last cycle" from the item's
    own fields, without reading any event log.
    """
    item_id = options.get("item_id")
    if item_id is not None and item.item_id != item_id:
        return False
    state = options.get("state")
    if state is not None and item.state != state:
        return False
    since = options.get("since")
    if since is not None:
        stamps = [
            parsed
            for parsed in (
                work_ledger.parse_stamp(value)
                for value in (item.created_at, item.last_report_at, item.closed_at)
                if value
            )
            if parsed is not None
        ]
        if not stamps or max(stamps) < since:
            return False
    return True


#: Events returned per item. The log is append-only and capped at 200 per item,
#: so an unbounded slice would eventually be the largest thing in a conductor's
#: context — the opposite of what a patrol cycle needs. Newest kept, oldest
#: dropped, which is the slice ``read_events``' own ``limit`` already applies.
_MAX_EVENT_TAIL = 20


def _remote_dispatch_on() -> bool:
    """``work_ledger.remote_dispatch``, read on every call. Blocking; fails closed."""
    from kiro_crew.remote_dispatch import remote_dispatch_enabled

    return remote_dispatch_enabled()


def _remote_row_fields(key: str, item: work_ledger.WorkItem) -> dict[str, Any]:
    from kiro_crew.dashboard.handlers.remote_ingest import remote_row_fields

    return remote_row_fields(key, item)


def _tail_events(key: str, item_id: str, limit: int) -> list[work_ledger.WorkEvent]:
    """The newest *limit* events for one item. ``limit`` is keyword-only downstream."""
    return work_ledger.read_events(key, item_id, limit=limit)


def _slot_running(state: DashboardState, key: str) -> bool:
    """Whether *key*'s slot has a TURN IN FLIGHT — not merely an open tab.

    ``stale`` is the conjunction "quiet past the window AND not running AND the
    worker's last word still left the move with it", and the running half means the
    worker is doing something (a thirty-minute build), not that its session exists. An idle worker whose tab is still open but which
    stopped without reporting is exactly the case the flag exists to surface, and
    testing slot EXISTENCE here would never flag it. ``orphaned`` keeps the
    existence test, because a conductor's absence is what that flag means.
    """
    slot = _find_slot(state, key)
    return bool(getattr(slot, "running", False)) if slot is not None else False


def _slot_closed(state: DashboardState, key: str) -> bool:
    """Whether *key*'s slot is GONE, read the way the work-ledger wake gate reads it.

    ``is_stale`` skips its window for a worker that reported and then closed, and the
    badge must agree with the gate that wakes the conductor: one resolver,
    :func:`ledger_wake.worker_closed`, answers both. It counts a slot being built, an
    unrestored key and any key during a restore as open, so the badge cannot report a
    live worker as gone, and anything unreadable answers False.
    """
    return ledger_wake.worker_closed(state, key)


def _slot_looping(key: str) -> bool:
    """Whether *key*'s session holds an active loop, read the way the wake gate reads it.

    One resolver, :func:`ledger_wake.worker_looping`, answers both the badge and the
    stall wake, so a worker waiting inside its own monitor loop reads the same on
    both. No auto-nudge service, or anything unreadable, answers False.
    """
    from kiro_crew.autonudge import get_instance

    return ledger_wake.worker_looping(get_instance(), key)


def worker_holds_open_item(slot_key: str) -> bool:
    """Whether *slot_key* is a worker bound to an OPEN item that names it back. BLOCKING.

    For the slot-retention paths (the open-tab restore budget and the idle
    sweep), which must not leave a worker without its tab: the wake gate reads a
    worker with no slot as closed. The key is the slot's own name, resolved by
    the server, never by a caller. Both spellings are asked, because a binding
    is filed under the ledger's folded key. Any read that fails answers True: an
    unknown binding must not cost a worker its tab.
    """
    try:
        for candidate in dict.fromkeys((slot_key, session_ledger.ledger_key(slot_key))):
            binding = work_ledger.read_binding(candidate, strict=True)
            if binding is None:
                continue
            item = work_ledger.read_work_item(*binding, strict=True)
            if item is not None and not item.is_terminal and item.worker_session_key == candidate:
                return True
    except Exception:  # noqa: BLE001 - unreadable binding means keep the tab
        logger.debug("work-ledger binding read failed for %s", slot_key, exc_info=True)
        return True
    return False


def _find_slot(state: DashboardState, key: str):
    if not key:
        return None
    for candidate in (key, f"dashboard_{key}"):
        try:
            slot = state.get_slot(candidate)
        except Exception:  # pragma: no cover - a slot-table read must not fail a read
            logger.debug("slot lookup failed for %s", candidate, exc_info=True)
            continue
        if slot is not None:
            return slot
    return None


def _slot_open(state: DashboardState, key: str) -> bool:
    """Whether *key* still names an open slot. A blank key is never alive."""
    if not key:
        return False
    try:
        return state.get_slot(key) is not None or state.get_slot(f"dashboard_{key}") is not None
    except Exception:  # pragma: no cover - a slot-table read must not fail a read
        logger.debug("slot liveness read failed for %s", key, exc_info=True)
        return False
