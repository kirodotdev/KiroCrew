"""Ledger API — shared todo/scratch lists attachable to chat sessions.

Routes (registered in server.py, cookie-auth via the standard middleware):
    GET    /api/ledgers                       list (meta + progress + pinned_by)
    POST   /api/ledgers                       create {title?}
    GET    /api/ledgers/{id}                  full ledger incl. content
    PUT    /api/ledgers/{id}                  rename and/or CAS content write
    DELETE /api/ledgers/{id}                  delete + unpin everywhere
    POST   /api/ledgers/{id}/toggle           atomic checkbox flip
    PATCH  /api/chat/slots/{slot}/ledger      pin/unpin a slot's default ledger

Store calls are synchronous filesystem work — every one runs off the event
loop via ``run_in_executor``. Mutations are serialized behind a module lock
so the registry read-modify-write (and the CAS check) is atomic across
concurrent requests.
"""

from __future__ import annotations

import asyncio
import logging

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.ledgers import (
    MAX_CONTENT_LEN,
    MAX_TITLE_LEN,
    LedgerConflictError,
    LedgerNotFoundError,
    LedgerStore,
)
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_write_lock = asyncio.Lock()


def _store() -> LedgerStore:
    return LedgerStore()


def _pinned_by(state: DashboardState, ledger_id: str) -> list[str]:
    """Slot keys currently pinning this ledger (in-memory, event-loop safe)."""
    return [key for key, slot in state._slots.items() if slot.ledger_id == ledger_id]


async def _run(fn, *args):  # type: ignore[no-untyped-def]
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(subprocess_executor(), fn, *args)


def _conflict_response(exc: LedgerConflictError) -> web.Response:
    return web.json_response(
        {"error": "version_conflict", "current": exc.current}, status=409
    )


async def api_ledgers_list(request: web.Request) -> web.Response:
    """GET /api/ledgers — all ledgers with checklist progress and pins."""
    state: DashboardState = request.app["state"]
    ledgers = await _run(_store().list)
    for meta in ledgers:
        meta["pinned_by"] = _pinned_by(state, meta["id"])
    return web.json_response(ledgers)


async def api_ledger_create(request: web.Request) -> web.Response:
    """POST /api/ledgers — create a ledger."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    title = str(body.get("title") or "").strip()[:MAX_TITLE_LEN]
    async with _write_lock:
        meta = await _run(_store().create, title)
    meta["pinned_by"] = []
    state.push_ledger_update(meta["id"], meta["title"], meta["version"], meta["updated_at"])
    sel().log_api_access(
        caller="dashboard", operation="ledger_create",
        outcome="allowed", source="dashboard", resources=str(meta["id"]),
    )
    return web.json_response(meta, status=201)


async def api_ledger_get(request: web.Request) -> web.Response:
    """GET /api/ledgers/{id} — full ledger including content."""
    state: DashboardState = request.app["state"]
    lid = request.match_info["id"]
    try:
        meta = await _run(_store().get, lid)
    except LedgerNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    meta["pinned_by"] = _pinned_by(state, lid)
    return web.json_response(meta)


async def api_ledger_update(request: web.Request) -> web.Response:
    """PUT /api/ledgers/{id} — rename and/or CAS content write."""
    state: DashboardState = request.app["state"]
    lid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = body.get("content")
    base_version = body.get("base_version")
    title = body.get("title")
    if content is None and title is None:
        return web.json_response({"error": "nothing to update"}, status=400)
    if content is not None:
        if not isinstance(content, str):
            return web.json_response({"error": "content must be a string"}, status=400)
        if len(content) > MAX_CONTENT_LEN:
            return web.json_response(
                {"error": f"content exceeds {MAX_CONTENT_LEN} chars"}, status=400
            )
        if not isinstance(base_version, int):
            return web.json_response(
                {"error": "base_version (int) required with content"}, status=400
            )
    if title is not None and not str(title).strip():
        return web.json_response({"error": "title must be non-empty"}, status=400)
    try:
        async with _write_lock:
            meta = await _run(
                lambda: _store().update(
                    lid,
                    content=content,
                    base_version=base_version,
                    title=str(title)[:MAX_TITLE_LEN] if title is not None else None,
                )
            )
    except LedgerNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except LedgerConflictError as exc:
        sel().log_api_access(
            caller="dashboard", operation="ledger_update",
            outcome="denied", source="dashboard", resources=lid, error="version conflict",
        )
        return _conflict_response(exc)
    state.push_ledger_update(meta["id"], meta["title"], meta["version"], meta["updated_at"])
    sel().log_api_access(
        caller="dashboard", operation="ledger_update",
        outcome="allowed", source="dashboard", resources=lid,
    )
    return web.json_response(meta)


async def api_ledger_toggle(request: web.Request) -> web.Response:
    """POST /api/ledgers/{id}/toggle — atomic checkbox flip (line CAS)."""
    state: DashboardState = request.app["state"]
    lid = request.match_info["id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    line = body.get("line")
    expected = body.get("expected")
    if not isinstance(line, int) or line < 0:
        return web.json_response({"error": "line must be a non-negative int"}, status=400)
    if not isinstance(expected, str) or len(expected) > 4000:
        return web.json_response({"error": "expected must be a string"}, status=400)
    try:
        async with _write_lock:
            result = await _run(lambda: _store().toggle(lid, line, expected))
    except LedgerNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    except LedgerConflictError as exc:
        return _conflict_response(exc)
    meta = await _run(_store().get, lid)
    state.push_ledger_update(lid, meta["title"], meta["version"], meta["updated_at"])
    sel().log_api_access(
        caller="dashboard", operation="ledger_toggle",
        outcome="allowed", source="dashboard", resources=lid,
    )
    return web.json_response(result)


async def api_ledger_delete(request: web.Request) -> web.Response:
    """DELETE /api/ledgers/{id} — delete the ledger, unpin from all slots."""
    state: DashboardState = request.app["state"]
    lid = request.match_info["id"]
    try:
        async with _write_lock:
            await _run(_store().delete, lid)
    except LedgerNotFoundError:
        return web.json_response({"error": "not found"}, status=404)
    for slot in state._slots.values():
        if slot.ledger_id == lid:
            slot.ledger_id = ""
            await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    state.push_ledger_update(lid, "", 0, 0.0, deleted=True)
    sel().log_api_access(
        caller="dashboard", operation="ledger_delete",
        outcome="allowed", source="dashboard", resources=lid,
    )
    return web.json_response({"ok": True})


async def api_chat_slot_ledger(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/ledger — pin/unpin the slot's ledger."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ledger_id = str(body.get("ledger_id") or "")
    if ledger_id:
        try:
            meta = await _run(_store().get, ledger_id)
        except LedgerNotFoundError:
            return web.json_response({"error": "ledger not found"}, status=400)
    slot.ledger_id = ledger_id
    await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    if ledger_id:
        state.push_ledger_update(
            ledger_id, meta["title"], meta["version"], meta["updated_at"]
        )
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_ledger",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "ledger_id": slot.ledger_id})


def setup_ledger_routes(app: web.Application) -> None:
    """Register the ledger CRUD + slot-pin routes (see module docstring)."""
    app.router.add_get("/api/ledgers", api_ledgers_list)
    app.router.add_post("/api/ledgers", api_ledger_create)
    app.router.add_get("/api/ledgers/{id}", api_ledger_get)
    app.router.add_put("/api/ledgers/{id}", api_ledger_update)
    app.router.add_delete("/api/ledgers/{id}", api_ledger_delete)
    app.router.add_post("/api/ledgers/{id}/toggle", api_ledger_toggle)
    app.router.add_patch("/api/chat/slots/{slot}/ledger", api_chat_slot_ledger)
