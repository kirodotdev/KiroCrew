"""Explicit member-memory recall and owner-directed, selected-item inheritance.

Recall derives authority from a recorded session. Copying is an owner action:
neither an agent nor a source document may choose another member's memory.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web

from kiro_crew._sqlite_compat import sqlite3
from kiro_crew.executors import run_in_embed_pool

from ._shared import (
    _admin_store,
    _audit,
    _blocks_reads_session,
    _redact_memory_field,
    _store_unavailable,
    read_bounded_json,
    requesting_slot_project,
    require_owner_dashboard_request,
    resolve_lesson_memory_store,
    resolve_requested_memory_store,
    vector_memory_for_store,
)
from .cron import _is_temporary_transcript, _recognize_session

MAX_SEED_ITEMS = 50
MAX_RECALL_QUERY = 2000


def _error(message: str, code: str, status: int = 400) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


async def _private_tier(request: web.Request, name: str) -> tuple[Any, web.Response | None]:
    if not name:
        return None, _error("Select a member's private V2 memory.", "member_memory_required")
    try:
        tier = await vector_memory_for_store(request.app["state"], name)
    except (ValueError, OSError, sqlite3.Error):
        return None, _store_unavailable(name)
    if tier is None:
        return None, _store_unavailable(name)
    if tier.algorithm_version != "v2":
        return None, _error(
            "This memory is V1. Create private memory for the member first; no migration was performed.",
            "member_memory_required",
            409,
        )
    return tier, None


async def api_memory_recall(request: web.Request) -> web.Response:
    """GET /api/memory/recall: explicit recall from the caller's V1 or V2 store."""
    state = request.app["state"]
    if "store" in request.query:
        name, refusal = await resolve_requested_memory_store(request, state, "memory.recall")
    else:
        # Authenticate BEFORE consulting the session header. A regular browser
        # token cannot borrow a member identity by inventing X-Session-Key.
        if request.get("internal_auth") is not True:
            refusal = await require_owner_dashboard_request(request, "memory.recall")
            if refusal is not None:
                return refusal
        key = request.headers.get("X-Session-Key", "")
        refusal = await _recognize_session(
            state, key, "memory.recall", blocks_persisted_mode=_is_temporary_transcript
        )
        if refusal is not None:
            return refusal
        if _blocks_reads_session(state, request):
            return _error(
                "Memory reads are disabled for this session.", "memory_reads_disabled", 403
            )
        try:
            name, refusal = await resolve_lesson_memory_store(request, state, "memory.recall")
        except (ValueError, OSError):
            return _error("The member's recorded memory is unavailable.", "store_unavailable", 503)
    if refusal is not None:
        return refusal
    query = request.query.get("q", "").strip()
    if not query or len(query) > MAX_RECALL_QUERY:
        return _error("A query of 1–2000 characters is required.", "invalid_memory_query")
    try:
        tier = await vector_memory_for_store(state, name)
        if tier is None:
            return _store_unavailable(name)
        project = requesting_slot_project(state, request.headers.get("X-Session-Key", ""))
        result = await run_in_embed_pool(
            tier.recall, query, cap=3000, project_dir=str(project) if project else None
        )
    except (ValueError, OSError, sqlite3.Error):
        return _store_unavailable(name)
    from kiro_crew.memory_recall import recall_json

    return web.json_response(
        _redact_memory_field({"store": name, **result}),
        dumps=lambda payload: recall_json(payload, ensure_ascii=False, context_cap=3000),
    )


async def api_memory_seed(request: web.Request) -> web.Response:
    """POST /api/memory/seed copies only explicitly selected items, without overwrite."""
    refusal = await require_owner_dashboard_request(request, "memory.seed")
    if refusal is not None:
        return refusal
    body, refusal = await read_bounded_json(request, max_bytes=16384)
    if refusal is not None:
        return refusal
    assert body is not None
    items = body.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_SEED_ITEMS:
        return _error("Select between 1 and 50 memory items.", "invalid_seed_items")
    selections: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            return _error("Each selection requires a kind and an item id.", "invalid_seed_items")
        kind, identity = item.get("kind"), item.get("id")
        if (
            not isinstance(kind, str)
            or kind not in {"fact", "directive", "episode"}
            or not isinstance(identity, str)
        ):
            return _error("Each selection requires a valid kind and item id.", "invalid_seed_items")
        if not identity or len(identity) > 200 or (kind, identity) in selections:
            return _error(
                "Item ids must be unique, nonempty and at most 200 characters.",
                "invalid_seed_items",
            )
        selections.append((kind, identity))
    target, refusal = await _admin_store(request, "memory.seed", body)
    if refusal is not None:
        return refusal
    if not isinstance(body.get("source_store"), str) or not body["source_store"].strip():
        return _error("Choose the source memory explicitly.", "invalid_seed_source")
    source, refusal = await _admin_store(
        request, "memory.seed.source", {"store": body["source_store"]}
    )
    if refusal is not None:
        return refusal
    if source == target:
        return _error("Choose a different source memory.", "same_memory_store")
    target_tier, refusal = await _private_tier(request, target)
    if refusal is not None:
        return refusal
    try:
        source_tier = await vector_memory_for_store(request.app["state"], source)
    except (ValueError, OSError, sqlite3.Error):
        return _store_unavailable(source)
    if source_tier is None:
        return _store_unavailable(source)

    def copy_selected() -> list[dict[str, Any]] | None:
        # Read and validate the entire selection before writing any item. A stale
        # selection is a conflict, never a request to copy an empty replacement.
        rows = []
        for kind, identity in selections:
            row = (
                source_tier._get_episodic(identity)
                if kind == "episode"
                else source_tier.get_semantic(identity)
            )
            if row is None or row.get("is_deleted"):
                return None
            if kind != "episode" and (identity.startswith("lesson.") != (kind == "directive")):
                return None
            rows.append(dict(row))
        outcomes = []
        stopped = False
        for (kind, identity), row in zip(selections, rows):
            details = {"kind": kind, "source_id": identity}
            if stopped:
                outcomes.append(
                    {
                        **details,
                        "id": identity,
                        "outcome": "not_attempted",
                        "reason": "Not attempted because an earlier copy could not be confirmed.",
                    }
                )
                continue
            try:
                result = target_tier.seed_item_if_absent(
                    row, source_store=source or "default", source_id=identity, kind=kind
                )
            except Exception as exc:
                # Each store helper commits its own item. A later failure cannot
                # erase earlier success, and the failing commit itself may be
                # uncertain. Return every selection with its honest outcome.
                stopped = True
                result = {
                    "id": identity,
                    "outcome": "unconfirmed",
                    "reason": (
                        f"Copy could not be confirmed: {str(exc)[:240]}. "
                        "Refresh this item before retrying."
                    ),
                }
            outcomes.append({**details, **result})
        return outcomes

    try:
        results = await asyncio.to_thread(copy_selected)
    except (ValueError, OSError, sqlite3.Error, RuntimeError):
        return _store_unavailable(target)
    if results is None:
        return _error(
            "A selected memory changed or is no longer available. Refresh the source.",
            "seed_source_changed",
            409,
        )
    partial = any(result["outcome"] in {"unconfirmed", "not_attempted"} for result in results)
    await _audit(request, "memory.seed", "partial" if partial else "ok", target)
    return web.json_response(
        _redact_memory_field(
            {
                "store": target,
                "source_store": source or "default",
                "results": results,
                "partial": partial,
            }
        )
    )
