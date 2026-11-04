"""Chat bookmark handlers: list, add and remove the links kept with one chat session.

The store and its rules live in :mod:`kiro_crew.dashboard.chat_bookmarks_store`.
Every change is announced with a ``bookmarks_changed`` frame carrying only the
slot key, to dashboard-owner connections, the same shape ``pins_changed`` uses.
"""

from __future__ import annotations

import logging

from aiohttp import web

from kiro_crew.dashboard.chat_bookmarks_store import BookmarkError
from kiro_crew.dashboard.chat_pins import _authorize_app_slot
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.slot_ownership import slot_not_found
from kiro_crew.dashboard.state import DashboardState

logger = logging.getLogger(__name__)

_ERROR_STATUS = {"bookmark_limit_reached": 409}


def _error(code: str, message: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _persist_failed() -> web.Response:
    logger.warning("chat_bookmarks: failed to persist", exc_info=True)
    return _error("persist_failed", "failed to persist bookmarks", 500)


def notify_bookmarks_changed(state: DashboardState, slot_key: str) -> None:
    state.broadcast_ws_owners("bookmarks_changed", {"slot_key": slot_key})


async def api_chat_bookmarks_list(request: web.Request) -> web.Response:
    """GET /api/chat/bookmarks?slot=<slot_key> — the session's bookmarks, oldest first."""
    state: DashboardState = request.app["state"]
    slot_key = request.query.get("slot", "")
    if not slot_key:
        return _error("missing_query_params", "slot query param required")
    denied = _authorize_app_slot(request, state, slot_key, "chat.bookmarks_list")
    if denied is not None:
        return denied
    try:
        bookmarks = await state.chat_bookmarks.list_for(slot_key)
    except OSError:
        logger.warning("chat_bookmarks: failed to read", exc_info=True)
        return _error("read_failed", "failed to read bookmarks", 500)
    return web.json_response({"bookmarks": bookmarks})


async def api_chat_bookmarks_create(request: web.Request) -> web.Response:
    """POST /api/chat/bookmarks — bookmark a URL on a session."""
    state: DashboardState = request.app["state"]
    body, body_error = await read_bounded_json(request)
    if body_error is not None:
        return body_error
    assert body is not None
    slot_key = body.get("slot_key")
    url = body.get("url")
    title = body.get("title", "")
    if not isinstance(slot_key, str) or not slot_key.strip():
        return _error("missing_required_fields", "slot_key is required")
    if not isinstance(url, str) or not isinstance(title, str):
        return _error("invalid_field_type", "url and title must be strings")
    slot_key = slot_key.strip()
    denied = _authorize_app_slot(request, state, slot_key, "chat.bookmarks_create")
    if denied is not None:
        return denied
    if state.get_slot(slot_key) is None:
        return slot_not_found()
    try:
        record, created = await state.chat_bookmarks.add(slot_key, url, title, origin="user")
    except BookmarkError as exc:
        return _error(exc.code, str(exc), _ERROR_STATUS.get(exc.code, 400))
    except OSError:
        return _persist_failed()
    notify_bookmarks_changed(state, slot_key)
    return web.json_response(record, status=201 if created else 200)


async def api_chat_bookmarks_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/bookmarks/{id} — remove one bookmark."""
    state: DashboardState = request.app["state"]
    bookmark_id = request.match_info["id"]
    slot_key = await state.chat_bookmarks.slot_of(bookmark_id)
    if slot_key is None:
        return _error("bookmark_not_found", "not found", 404)
    denied = _authorize_app_slot(
        request, state, slot_key, "chat.bookmarks_delete", missing_code="bookmark_not_found"
    )
    if denied is not None:
        return denied
    try:
        removed = await state.chat_bookmarks.remove(slot_key, bookmark_id=bookmark_id)
    except OSError:
        return _persist_failed()
    if not removed:
        return _error("bookmark_not_found", "not found", 404)
    notify_bookmarks_changed(state, slot_key)
    return web.json_response({"ok": True})
