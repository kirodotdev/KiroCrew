"""``POST /api/chat/manual-order``: the person's own order for every Chats session.

The order lives in ``pinned_session_order`` beside the pinned one (its own file
in the same fenced ``pinned-order`` directory) and reaches every browser on the
slot rows as ``manual_rank`` and ``manual_rev``. The sidebar draws unpinned rows
in it while ``dashboard.session_sort`` is ``manual``.

Only the person's own browser writes it. :func:`_write_manual_order` is the one
function that does, and it refuses every other caller: an agent or crew member
(they reach the gateway with ``X-Internal-Secret``, so their origin is never
``dashboard``) and an app (its token carries an app claim). No MCP tool writes
it, so an agent has no sanctioned path to it either.
"""

from __future__ import annotations

import asyncio
import logging
import time
import weakref
from typing import Any, TypeGuard

from aiohttp import web

from kiro_crew.config.loader import config_dir
from kiro_crew.dashboard import pinned_session_order as pso
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.token_auth import folder_principal, request_origin
from kiro_crew.loop_lock import LoopBoundLock
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_OPERATION = "chat.manual_order"

#: Longest ``created`` token a move may echo: an ISO-8601 timestamp with offset
#: is 32 characters, so this leaves room without leaving it open (the pinned
#: route's bound).
_MAX_EXPECTED_CREATED_CHARS = 64

#: Byte ceiling for a manual-order body: one list of keys at the store's bounds
#: (a seed's ``keys`` or a move's ``seat``), each key quoted and separated,
#: plus the moved key, its anchor, its ``created`` token and the field names.
_MAX_MANUAL_ORDER_BODY_BYTES = (
    pso.MAX_PINNED_ORDER_KEYS * (pso.MAX_PINNED_ORDER_KEY_CHARS + 4)
    + 2 * (pso.MAX_PINNED_ORDER_KEY_CHARS + 4)
    + _MAX_EXPECTED_CREATED_CHARS
    + 256
)

_LOCKS: "weakref.WeakKeyDictionary[Any, LoopBoundLock]" = weakref.WeakKeyDictionary()


def _lock(state: Any) -> LoopBoundLock:
    """One write at a time per gateway, so each write reads the order the last one left."""
    lock = _LOCKS.get(state)
    if lock is None:
        lock = _LOCKS[state] = LoopBoundLock()
    return lock


def _from_the_persons_browser(state: DashboardState, request: web.Request) -> bool:
    """Whether *request* is the person's own browser, the order's only writer.

    The origin is :func:`token_auth.request_origin`, the rule the folder routes'
    ``_audit_origin`` wraps: ``dashboard`` only for a request with no
    ``X-Internal-Secret``. Agents and crew members reach the gateway through the
    MCP loopback helpers, which always send that secret, so their origin is
    ``mcp``. A request without it must carry a dashboard token signed with the
    gateway's key, which the agent file tools and the OS sandbox fence. An app
    token is a dashboard token too, but carries an app claim, which
    :func:`folder_principal` names.
    """
    source, _caller = request_origin(request, what="manual order write", log=logger)
    return source == "dashboard" and not folder_principal(state, request)


def _refusal(state: DashboardState, request: web.Request) -> web.Response:
    source, caller = request_origin(request, what="manual order write", log=logger)
    principal = folder_principal(state, request)
    sel().log_api_access(
        caller=principal or caller,
        operation=_OPERATION,
        outcome="denied",
        source=source if not principal else "app_isolation",
        error="only the person's own browser may set the manual order",
    )
    return web.json_response(
        {
            "error": "only the person's own browser may set the manual session order",
            "code": "manual_order_person_only",
        },
        status=403,
    )


async def ensure_manual_order_loaded(state: DashboardState) -> bool:
    """Read the stored manual order if it has not landed yet; ``True`` once loaded.

    A write that lands first wins: the read is dropped when the order is
    already loaded by the time it returns. A read that holds keys publishes
    them, since slot rows sent before it carried no ``manual_rank``. Returns
    ``False`` when the file could not be read, and the caller writes nothing.
    """
    order = state.manual_session_order
    if order.loaded:
        return True
    path = pso.manual_store_path(config_dir())
    try:
        keys, stored = await asyncio.to_thread(pso.load_stored, path)
    except OSError:
        logger.warning("could not read %s", pso.MANUAL_ORDER_FILE, exc_info=True)
        return False
    if order.loaded:
        return True
    order.adopt(keys, stored, time.time_ns() // 1000)
    if keys:
        state.push_slots_update()
    return True


async def load_manual_order_after_listen(state: DashboardState) -> None:
    """Read the manual order once the gateway serves; rows carry no rank until it lands."""
    try:
        await ensure_manual_order_loaded(state)
    except Exception:  # noqa: BLE001 -- a display preference must not crash startup
        logger.warning("could not load the manual session order", exc_info=True)


async def _write_manual_order(
    state: DashboardState, keys: list[str] | None, *, request: web.Request
) -> bool | None:
    """Store *keys* as the manual order, or clear it for ``None``; the ONE writer.

    Returns ``None`` without touching disk or memory for any caller that is not
    the person's own browser (see :func:`_from_the_persons_browser`), so every
    route that ever writes the order inherits the rule. Memory changes only
    after the file write succeeds, so the order on screen is the one a restart
    loads. Returns ``False`` (logged) when the write fails.
    """
    if not _from_the_persons_browser(state, request):
        return None
    path = pso.manual_store_path(config_dir())
    try:
        if keys is None:
            await asyncio.to_thread(pso.clear_stored, path)
        else:
            await asyncio.to_thread(pso.save, path, keys)
    except OSError:
        logger.warning("could not write the manual session order", exc_info=True)
        return False
    state.manual_session_order.adopt(keys or [], keys is not None, time.time_ns() // 1000)
    return True


def _valid_key(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and 0 < len(value) <= pso.MAX_PINNED_ORDER_KEY_CHARS


def _bad(message: str, code: str) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=400)


def _key_list(raw: object, field: str) -> tuple[list[str] | None, web.Response | None]:
    """*raw* as a list of distinct slot keys within the store's bounds, or a 400."""
    if not isinstance(raw, list) or not all(_valid_key(key) for key in raw):
        return None, _bad(f"{field} must be an array of session keys", f"{field}_invalid")
    if len(raw) > pso.MAX_PINNED_ORDER_KEYS:
        return None, _bad(f"too many keys in {field}", f"{field}_too_many")
    if len(set(raw)) != len(raw):
        return None, _bad(f"a key appears more than once in {field}", f"{field}_duplicate")
    return list(raw), None


def _conflict(state: DashboardState, message: str, code: str) -> web.Response:
    """A 409 carrying the stored order, so the browser can draw it without a re-read."""
    order = state.manual_session_order
    return web.json_response(
        {"error": message, "code": code, "order": list(order.keys), "rev": order.rev},
        status=409,
    )


async def api_chat_manual_order(request: web.Request) -> web.Response:
    """POST /api/chat/manual-order -- seed, move one session in, or clear the manual order.

    Three bodies, one write each:

    * ``{"op": "seed", "keys": [...]}`` -- the order on the person's screen,
      stored only while nothing is saved (``only_if_unset``); otherwise a 409
      ``manual_order_exists`` carrying the stored order. The first drag and
      picking Manual with nothing saved both seed.
    * ``{"op": "move", "key": K, "after": A}`` (or ``"before": B``) -- moves
      ONE session next to another; ``"after": null`` puts it at the top.
      ``"seat"`` lists the unranked sessions the caller showed above the ranked
      ones, in that order, so the anchor has a place when it is one of them.
      ``"expected_created"`` is K's ``created``, refusing a key that was closed
      and recreated since the caller read it.
    * ``{"op": "clear", "rev": R}`` -- removes the order, only while it is still
      at revision ``R`` (the undo of a first drag); otherwise a 409
      ``manual_order_changed``, so an undo never wipes a move made elsewhere.

    Every answer that writes carries ``{"order", "rev"}``.
    """
    state: DashboardState = request.app["state"]
    if not _from_the_persons_browser(state, request):
        return _refusal(state, request)
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_MANUAL_ORDER_BODY_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    op = body.get("op")
    if op not in ("seed", "move", "clear"):
        return _bad("op must be seed, move or clear", "op_invalid")
    keys: list[str] = []
    seat: list[str] = []
    key = ""
    anchor: str | None = None
    after = True
    rev = 0
    expected_created = ""
    if op == "seed":
        parsed, err = _key_list(body.get("keys"), "keys")
        if err is not None:
            return err
        keys = parsed or []
    elif op == "move":
        raw_key = body.get("key")
        if not _valid_key(raw_key):
            return _bad("key must be a session key", "key_invalid")
        key = raw_key
        has_after, has_before = "after" in body, "before" in body
        if has_after == has_before:
            return _bad("give exactly one of after or before", "anchor_invalid")
        anchor = body.get("after") if has_after else body.get("before")
        after = has_after
        if not (_valid_key(anchor) or (has_after and anchor is None)) or anchor == key:
            return _bad("the anchor must be another session key", "anchor_invalid")
        parsed, err = _key_list(body.get("seat", []), "seat")
        if err is not None:
            return err
        seat = parsed or []
        raw_created = body.get("expected_created", "")
        if not isinstance(raw_created, str) or len(raw_created) > _MAX_EXPECTED_CREATED_CHARS:
            return _bad("expected_created must be a string", "expected_created_invalid")
        expected_created = raw_created
    else:
        raw_rev = body.get("rev")
        if not isinstance(raw_rev, int) or isinstance(raw_rev, bool):
            return _bad("rev must be an integer", "rev_invalid")
        rev = raw_rev
    async with _lock(state):
        if not await ensure_manual_order_loaded(state):
            return web.json_response(
                {"error": "the manual order could not be read", "code": "manual_order_unreadable"},
                status=503,
            )
        order = state.manual_session_order
        live = list(state._slots)
        next_keys: list[str] | None
        if op == "seed":
            if order.stored:
                return _conflict(state, "a manual order is already stored", "manual_order_exists")
            next_keys = pso.after_manual_seed(keys, live)
        elif op == "move":
            slot = state._slots.get(key)
            if slot is None or (expected_created and slot.created_at != expected_created):
                return web.json_response(
                    {"error": "the moved session is gone or was replaced", "code": "session_gone"},
                    status=409,
                )
            next_keys = pso.after_manual_move(
                list(order.keys), key, anchor, after=after, seat=seat, live=live
            )
            if next_keys is None:
                return _conflict(state, "the session to move next to has no place", "anchor_gone")
        else:
            if rev != order.rev:
                return _conflict(state, "the manual order changed since", "manual_order_changed")
            next_keys = None
        saved = await _write_manual_order(state, next_keys, request=request)
        if saved is None:
            return _refusal(state, request)
        if not saved:
            return web.json_response(
                {
                    "error": "the gateway could not write its order file",
                    "code": "manual_order_write_failed",
                },
                status=500,
            )
        answer = {"ok": True, "order": list(order.keys), "rev": order.rev}
    state.push_slots_update()
    source, caller = request_origin(request, what="manual order write", log=logger)
    sel().log_api_access(
        caller=caller,
        operation=_OPERATION,
        outcome="allowed",
        source=source,
        resources=f"{op}:{key}" if op == "move" else op,
    )
    return web.json_response(answer)
