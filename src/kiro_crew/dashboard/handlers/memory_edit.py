"""Owner-directed memory selection, preview and atomic editing."""

from __future__ import annotations

import asyncio
import secrets

from aiohttp import web

from kiro_crew import memory_edit
from kiro_crew._sqlite_compat import sqlite3

from ._shared import (
    _admin_store,
    _audit,
    _redact_memory_field,
    _store_unavailable,
    read_bounded_json,
    require_owner_dashboard_request,
    vector_memory_for_store,
)
from .memory import _memory_write_gate


def _secret(state) -> bytes:
    # One process-local key invalidates previews on gateway restart. It holds no
    # selected content and is never shared with model or browser credentials.
    key = getattr(state, "_memory_edit_preview_key", None)
    if key is None:
        key = secrets.token_bytes(32)
        state._memory_edit_preview_key = key
    return key


def _error(exc: memory_edit.MemoryEditError) -> web.Response:
    return web.json_response({"error": str(exc), "code": exc.code}, status=exc.status)


async def api_memory_records(request: web.Request) -> web.Response:
    refusal = await require_owner_dashboard_request(request, "memory.records")
    if refusal is not None:
        return refusal
    name, refusal = await _admin_store(request, "memory.records")
    if refusal is not None:
        return refusal
    try:
        # Authentication retains its token in the query; it is not a record filter.
        if set(request.query) - {"store", "q", "kind", "limit", "offset", "token"}:
            raise memory_edit.MemoryEditError("Invalid memory filter.")
        tier = await vector_memory_for_store(request.app["state"], name)
        if tier is None:
            return _store_unavailable(name)
        try:
            limit, offset = int(request.query.get("limit", "50")), int(
                request.query.get("offset", "0")
            )
        except ValueError:
            raise memory_edit.MemoryEditError("Page size and offset must be integers.") from None
        result = await asyncio.to_thread(
            memory_edit.list_records,
            tier,
            {key: request.query[key] for key in ("q", "kind") if key in request.query},
            limit=limit,
            offset=offset,
        )
    except memory_edit.MemoryEditError as exc:
        return _error(exc)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    return web.json_response(_redact_memory_field(result))


async def api_memory_records_refresh(request: web.Request) -> web.Response:
    refusal = await require_owner_dashboard_request(request, "memory.records.refresh")
    if refusal is not None:
        return refusal
    body, refusal = await read_bounded_json(request, max_bytes=512 * 1024)
    if refusal is not None:
        return refusal
    assert body is not None
    name, refusal = await _admin_store(request, "memory.records.refresh", body)
    if refusal is not None:
        return refusal
    try:
        tier = await vector_memory_for_store(request.app["state"], name)
        if tier is None:
            return _store_unavailable(name)
        result = await asyncio.to_thread(
            memory_edit.refresh_records,
            tier,
            body.get("items"),
            selection=body.get("selection"),
        )
    except memory_edit.MemoryEditError as exc:
        return _error(exc)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    return web.json_response(_redact_memory_field(result))


async def api_memory_record_history(request: web.Request) -> web.Response:
    refusal = await require_owner_dashboard_request(request, "memory.records.history")
    if refusal is not None:
        return refusal
    name, refusal = await _admin_store(request, "memory.records.history")
    if refusal is not None:
        return refusal
    try:
        tier = await vector_memory_for_store(request.app["state"], name)
        if tier is None:
            return _store_unavailable(name)
        try:
            limit, offset = int(request.query.get("limit", "25")), int(
                request.query.get("offset", "0")
            )
        except ValueError:
            raise memory_edit.MemoryEditError("Page size and offset must be integers.") from None
        result = await asyncio.to_thread(
            memory_edit.record_history,
            tier,
            {"kind": request.query.get("kind"), "id": request.query.get("id")},
            limit=limit,
            offset=offset,
        )
    except memory_edit.MemoryEditError as exc:
        return _error(exc)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    return web.json_response(_redact_memory_field(result))


async def _bulk(request: web.Request, *, apply: bool) -> web.Response:
    operation = "memory.bulk.apply" if apply else "memory.bulk.preview"
    refusal = await require_owner_dashboard_request(request, operation)
    if refusal is not None:
        return refusal
    refusal = await _memory_write_gate(request.app["state"], request, operation)
    if refusal is not None:
        return refusal
    body, refusal = await read_bounded_json(request, max_bytes=512 * 1024)
    if refusal is not None:
        return refusal
    assert body is not None
    name, refusal = await _admin_store(request, operation, body)
    if refusal is not None:
        return refusal
    try:
        state = request.app["state"]
        tier = await vector_memory_for_store(state, name)
        if tier is None:
            return _store_unavailable(name)
        secret = _secret(state)
        if apply:
            result = await asyncio.to_thread(
                memory_edit.apply_edit, tier, name, secret, body.get("preview_id")
            )
        else:
            result = await asyncio.to_thread(memory_edit.preview_edit, tier, name, secret, body)
    except memory_edit.MemoryEditError as exc:
        return _error(exc)
    except (OSError, sqlite3.Error):
        return _store_unavailable(name)
    if apply:
        await _audit(
            request, operation, "success", f"{name or 'default'}:{result['changed_count']}"
        )
    return web.json_response(_redact_memory_field(result))


async def api_memory_bulk_preview(request: web.Request) -> web.Response:
    return await _bulk(request, apply=False)


async def api_memory_bulk_apply(request: web.Request) -> web.Response:
    return await _bulk(request, apply=True)
