"""HTTP routes of Mastermind (``kiro_crew.mastermind``).

Two audiences:

* ``/api/mastermind/zoom`` and ``/api/mastermind/date`` answer the
  ``mastermind_zoom`` / ``mastermind_date`` MCP tools. They are MCP-only, listed
  in ``server._STRICT_INTERNAL_API_PATHS``, and answer only the Mastermind
  session itself: the caller's ``X-Session-Key``, vetted by
  ``_recognize_session``, must be the session the Mastermind slot runs on.
* Everything else is Settings > Mastermind, for the dashboard owner alone
  (``require_owner_dashboard_request``): the switch, the memory's state, its
  browser, export, clear and remove.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.handlers.cron import _recognize_session
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import is_incognito_transcript
from kiro_crew.mastermind.slot import MASTERMIND_SLOT_KEY, enabled, is_mastermind_turn, set_enabled

_TITLE = "Mastermind"


def _service(state: DashboardState) -> Any:
    from kiro_crew.mastermind.service import service_for

    return service_for(state)


def _int_arg(request: web.Request, name: str, default: int | None = None) -> int | None:
    raw = request.query.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return None


# The MCP tools' routes


async def _refuse_unless_mastermind(request: web.Request, operation: str) -> web.Response | None:
    state: DashboardState = request.app["state"]
    sk = request.headers.get("X-Session-Key", "")
    refusal = await _recognize_session(
        state, sk, operation, blocks_persisted_mode=is_incognito_transcript
    )
    if refusal is not None:
        return refusal
    from kiro_crew.dashboard.chat_utils_slots import effective_session_key

    slot = state._slots.get(MASTERMIND_SLOT_KEY)
    if slot is None or not is_mastermind_turn(state, slot) or sk != effective_session_key(slot):
        return web.json_response(
            {
                "error": "Mastermind's memory is reachable only from the Mastermind session.",
                "code": "not_mastermind_session",
            },
            status=403,
        )
    return None


async def api_mastermind_zoom(request: web.Request) -> web.Response:
    """GET /api/mastermind/zoom?id=&n=&at= — a line opened, or a message whole."""
    refusal = await _refuse_unless_mastermind(request, "mastermind_zoom")
    if refusal is not None:
        return refusal
    return await _zoom(request)


async def api_mastermind_date(request: web.Request) -> web.Response:
    """GET /api/mastermind/date?id= — when message id was written."""
    refusal = await _refuse_unless_mastermind(request, "mastermind_date")
    if refusal is not None:
        return refusal
    msg_id = _int_arg(request, "id")
    if msg_id is None:
        return web.json_response(
            {"error": "id must be an integer", "code": "id_not_integer"}, status=400
        )
    text = await _service(request.app["state"]).worker.run(lambda t: t.date(msg_id))
    return web.json_response({"text": text})


async def _zoom(request: web.Request) -> web.Response:
    msg_id, n, at = _int_arg(request, "id"), _int_arg(request, "n", 1), _int_arg(request, "at", 0)
    if msg_id is None or n is None or at is None or at < 0:
        return web.json_response(
            {"error": "id, n and at must be integers", "code": "zoom_args_not_integers"},
            status=400,
        )
    text = await _service(request.app["state"]).worker.run(lambda t: t.zoom(msg_id, n, at))
    return web.json_response({"text": text})


# Settings > Mastermind


async def api_mastermind_get(request: web.Request) -> web.Response:
    """GET /api/mastermind (and /status) — the switch and the memory's state."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.read")) is not None:
        return denied
    state: DashboardState = request.app["state"]
    on = await asyncio.to_thread(enabled, state)
    from kiro_crew.mastermind.service import summary_model

    body: dict[str, Any] = {
        "enabled": on,
        "slot_exists": MASTERMIND_SLOT_KEY in state._slots,
        "summary_model": await asyncio.to_thread(summary_model),
    }
    body.update(await _service(state).status())
    return web.json_response(body)


async def api_mastermind_set_enabled(request: web.Request) -> web.Response:
    """POST /api/mastermind/enabled {enabled} — turn Mastermind on or off.

    Turning it on makes and pins the Mastermind session if it is missing; turning
    it off keeps the session and its memory, as an ordinary session."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.enable")) is not None:
        return denied
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    on = body.get("enabled") if isinstance(body, dict) else None
    if not isinstance(on, bool):
        return web.json_response(
            {"error": "enabled must be a boolean", "code": "enabled_not_bool"}, status=400
        )
    state: DashboardState = request.app["state"]
    await asyncio.to_thread(set_enabled, state, on)
    if on:
        await _ensure_slot(state)
    state.push_slots_update()
    return web.json_response({"enabled": on})


async def _ensure_slot(state: DashboardState) -> None:
    from kiro_crew.dashboard.chat_persistence import save_slot_off_loop

    slot = state.get_or_create_slot(MASTERMIND_SLOT_KEY)
    if slot.title != _TITLE or not slot.pinned:
        slot.title = _TITLE
        slot._titled = True
        slot._title_origin = "user"
        slot.pinned = True
        await save_slot_off_loop(state, slot, force=True)


async def api_mastermind_view(request: web.Request) -> web.Response:
    """GET /api/mastermind/view — the memory view's lines, oldest first."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.read")) is not None:
        return denied
    count, lines = await _service(request.app["state"]).worker.run(lambda t: (t.count, t.lines()))
    return web.json_response({"count": count, "lines": lines})


async def api_mastermind_browse(request: web.Request) -> web.Response:
    """GET /api/mastermind/browse?id=&n=&at= — zoom, for the memory browser."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.read")) is not None:
        return denied
    return await _zoom(request)


async def api_mastermind_export(request: web.Request) -> web.Response:
    """GET /api/mastermind/export — every message and summary, as a JSON file."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.export")) is not None:
        return denied
    data = await _service(request.app["state"]).worker.run(lambda t: t.export())
    return web.Response(
        text=json.dumps(data, ensure_ascii=False),
        content_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="mastermind-memory.json"'},
    )


async def api_mastermind_clear(request: web.Request) -> web.Response:
    """POST /api/mastermind/clear — forget every message and summary. The
    session's visible transcript is kept."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.clear")) is not None:
        return denied
    await _service(request.app["state"]).worker.run(lambda t: t.clear())
    return web.json_response({"ok": True})


async def api_mastermind_remove(request: web.Request) -> web.Response:
    """POST /api/mastermind/remove {forget} — turn Mastermind off and close its
    session; ``forget`` also clears its memory."""
    if (denied := await require_owner_dashboard_request(request, "mastermind.remove")) is not None:
        return denied
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        body = {}
    forget = bool(body.get("forget")) if isinstance(body, dict) else False
    state: DashboardState = request.app["state"]
    await asyncio.to_thread(set_enabled, state, False)
    slot = state._slots.get(MASTERMIND_SLOT_KEY)
    if slot is not None:
        from kiro_crew.dashboard.chat_handlers import SlotCloseError, close_slot

        try:
            await close_slot(state, slot, MASTERMIND_SLOT_KEY)
        except SlotCloseError as exc:
            return web.json_response({"error": exc.message, "code": exc.code}, status=500)
    if forget:
        await _service(state).worker.run(lambda t: t.clear())
    return web.json_response({"ok": True})
