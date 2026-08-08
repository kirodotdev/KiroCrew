"""Session tags — dynamic vocabulary CRUD, slot assignment, and sidebar columns.

Tags are user-defined labels (id/name/color/status) attached to dashboard chat
slots. Sessions can carry multiple tags. The sidebar renders as a horizontal
strip of user-configurable columns; each column filters the session list by a
set of tags (any/all/none mode). Dragging a session card between columns that
carry a single status tag as filter reassigns the card's status tag.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
import weakref
from typing import Any, Callable, TypeVar

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_NAME_MAX = 60
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
_DEFAULT_COLOR = "#6b7280"
_VALID_MODES = {"any", "all", "none"}

_T = TypeVar("_T")


# Per-state tag-write lock. Serializes ALL mutations to state._tags + disk
# persistence so concurrent creates/updates/deletes cannot race the atomic
# fsync/os.replace sequence. Keyed weakly so a discarded state's lock is
# collectable (mirrors _RECONCILE_LOCKS in channel_slots.py).
_TAGS_WRITE_LOCKS: weakref.WeakKeyDictionary[Any, asyncio.Lock] = weakref.WeakKeyDictionary()


def _tags_write_lock(state: Any) -> asyncio.Lock:
    """Return (lazily create) the per-state asyncio.Lock for tag writes."""
    lock = _TAGS_WRITE_LOCKS.get(state)
    if lock is None:
        lock = asyncio.Lock()
        _TAGS_WRITE_LOCKS[state] = lock
    return lock


# Public accessor — used by the tags/boards HTTP handlers in this module and by
# chat_auto_tag.maybe_auto_tag to hold the lock across an entire
# resolve→merge→persist critical section.
tags_write_lock = _tags_write_lock


async def _mutate_tags_locked(state: DashboardState, mutate: Callable[[], _T]) -> _T:
    """Serialize a tag mutation + persistence under a shared async lock.

    ``mutate`` is a sync callable that modifies ``state._tags`` in place and
    returns a result. After it runs, an immutable snapshot is atomically written
    to disk inside a worker thread (mirrors DashboardState._atomic_write_json).
    The lock is NON-REENTRANT — callers must never nest.

    Raises on persist failure (caller must catch to surface HTTP 5xx).
    The in-memory state is rolled back to the pre-mutate snapshot on failure.
    """
    async with _tags_write_lock(state):
        pre_snapshot = [dict(t) for t in state._tags]
        result = mutate()
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Roll back to pre-mutate state.
            state._tags = pre_snapshot
            raise
        return result


def _write_tags_snapshot(state: DashboardState, snapshot: list[dict]) -> None:
    """Persist a pre-captured tag snapshot (runs in worker thread).

    Delegates to ``state.save_tags_snapshot`` so the file location resolves
    through ``kiro_crew.dashboard.state.config_dir`` exactly like
    ``save_tags`` (and stays patchable in tests)."""
    state.save_tags_snapshot(snapshot)


async def persist_tags_snapshot_unlocked(state: DashboardState) -> None:
    """Persist the current state._tags to disk without acquiring the lock.

    Callers MUST hold `tags_write_lock(state)` themselves. Exported for use in
    cross-module critical sections (e.g. chat_auto_tag.maybe_auto_tag).
    """
    snapshot = [dict(t) for t in state._tags]
    await asyncio.to_thread(_write_tags_snapshot, state, snapshot)


def _valid_color(value: str) -> str:
    return value if _COLOR_RE.match(value) else _DEFAULT_COLOR


def _tag_by_id(state: DashboardState, tag_id: str) -> dict | None:
    return next((t for t in state._tags if t.get("id") == tag_id), None)


def create_tag_definition(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
) -> dict:
    """Create a new tag definition (sync, no lock).

    Shared by the async locked callers. Mutates state._tags in place.
    Returns the created tag dict.
    """
    clean_name = name.strip()[:_NAME_MAX]
    if not clean_name:
        raise ValueError("tag name must not be empty")
    clean_color = _valid_color(str(color or _DEFAULT_COLOR))
    tag = {
        "id": uuid.uuid4().hex[:12],
        "name": clean_name,
        "color": clean_color,
        "order": len(state._tags),
        "status": bool(status),
    }
    state._tags.append(tag)
    return tag


async def create_tag_definition_off_loop(
    state: DashboardState,
    name: str,
    color: str | None = None,
    *,
    status: bool = False,
) -> dict:
    """Async wrapper: creates a tag definition under the shared write lock.

    Acquires the per-state tag-write lock (shared with update/delete) so
    concurrent callers creating the same missing tag name produce exactly one
    definition. The sync fsync write runs in a worker thread while the lock is
    held.

    Raises on persist failure so the caller can surface HTTP 5xx.
    """
    async with _tags_write_lock(state):
        # Re-check under lock: another caller may have just created it.
        lower = name.strip().lower()
        existing = next(
            (t for t in state._tags if (t.get("name") or "").lower() == lower),
            None,
        )
        if existing:
            return existing
        tag = create_tag_definition(state, name, color, status=status)
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Roll back in-memory mutation.
            state._tags = [t for t in state._tags if t.get("id") != tag["id"]]
            raise
        return tag


# ── Tag vocabulary ─────────────────────────────────────────────────────────


async def api_chat_tags(request: web.Request) -> web.Response:
    """GET /api/chat/tags — list all tag definitions."""
    state: DashboardState = request.app["state"]
    return web.json_response(sorted(state._tags, key=lambda t: t.get("order", 0)))


async def api_chat_tag_create(request: web.Request) -> web.Response:
    """POST /api/chat/tags — create a new tag."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # Redact credential / exfiltration patterns from the user-supplied name
    # BEFORE truncation — truncating first can slice a credential that
    # straddles the length cut into a fragment the scanners no longer
    # recognize, persisting a raw prefix. Same boundary pattern as other
    # LLM-authored sinks.
    name = (body.get("name") or "").strip()
    name, _ = redact_exfiltration_urls(name)
    name, _ = redact_credentials(name)
    name = name.strip()[:_NAME_MAX]
    if not name:
        return web.json_response({"error": "name required", "code": "name_required"}, status=400)
    color = str(body.get("color") or _DEFAULT_COLOR)
    status = bool(body.get("status", False))
    try:
        tag = await create_tag_definition_off_loop(state, name, color, status=status)
    except Exception:
        logger.warning("tag create failed to persist: %s", name)
        return web.json_response({"error": "persist failed", "code": "persist_failed"}, status=500)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_create",
        outcome="allowed",
        source="dashboard",
        resources=str(tag["id"]),
    )
    return web.json_response(tag, status=201)


async def api_chat_tag_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tags/{id} — rename / recolor / reorder.

    Resolution of the tag dict happens INSIDE the write lock so a concurrent
    DELETE cannot remove the tag between lookup and mutation (preventing a
    silent lost update on a detached dict).
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    # Early unlocked check for fast 404 on obviously invalid ids (avoids
    # JSON parse + lock contention for non-existent tags).
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    if "name" in body:
        new_name = str(body["name"]).strip()[:_NAME_MAX]
        if not new_name:
            return web.json_response(
                {"error": "name required", "code": "name_required"}, status=400
            )

    async with _tags_write_lock(state):
        # Re-resolve under lock — a concurrent DELETE may have removed it.
        tag = _tag_by_id(state, tid)
        if not tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        pre_snapshot = [dict(t) for t in state._tags]

        if "name" in body:
            # Redact BEFORE truncation (same reasoning as tag create).
            safe_name = str(body["name"]).strip()
            safe_name, _ = redact_exfiltration_urls(safe_name)
            safe_name, _ = redact_credentials(safe_name)
            safe_name = safe_name.strip()[:_NAME_MAX]
            if not safe_name:
                # Parity with create: never persist an empty name (e.g. the
                # whole input was a redacted credential).
                return web.json_response(
                    {"error": "name required", "code": "name_required"}, status=400
                )
            tag["name"] = safe_name
        if "color" in body:
            tag["color"] = _valid_color(str(body["color"]))
        if "order" in body:
            try:
                tag["order"] = int(body["order"])
            except (TypeError, ValueError, OverflowError):
                pass
        if "status" in body:
            tag["status"] = bool(body["status"])

        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            state._tags = pre_snapshot
            logger.warning("tag update failed to persist: %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
        updated = tag

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_update",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response(updated)


async def api_chat_tag_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tags/{id} — delete a tag; strip it from all slots.

    Holds the per-state tag-write lock across the ENTIRE sequence so a
    concurrent tag_session directive cannot resolve the tag between the
    vocabulary removal and the slot strip.

    CRASH-ATOMIC ordering: the vocabulary (``tags.json``) is persisted FIRST.
    Once that single write commits, the deletion is durable — a crash at any
    later point leaves only dangling tag ids on slots/boards, which are
    harmless and pruned on the next load (see ``_prune_unknown_tag_ids``).
    If the vocabulary write fails, nothing else has been touched, so the
    in-memory removal is simply rolled back and 500 returned. No multi-write
    compensation is needed in either direction.
    """
    state: DashboardState = request.app["state"]
    tid = request.match_info["id"]
    if not _tag_by_id(state, tid):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)

    async with _tags_write_lock(state):
        # Re-check under lock — another concurrent delete may have won.
        removed_tag = _tag_by_id(state, tid)
        if not removed_tag:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)

        # ── Single durable commit: remove from vocabulary and persist ────
        state._tags = [t for t in state._tags if t.get("id") != tid]
        snapshot = [dict(t) for t in state._tags]
        try:
            await asyncio.to_thread(_write_tags_snapshot, state, snapshot)
        except Exception:
            # Nothing else has been written — restore memory and abort.
            state._tags.append(removed_tag)
            logger.warning("tag delete: vocab persist failed for %s", tid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )

        # ── Best-effort cleanup: strip the (now nonexistent) id ──────────
        # Failures here are tolerable: a dangling id on disk is pruned on
        # the next load; mark the slot dirty so the periodic flush retries.
        for slot in state._slots.values():
            if tid in slot.tags:
                slot.tags = [t for t in slot.tags if t != tid]
                try:
                    await save_slot_off_loop(state, slot, force=True, best_effort=False)
                except Exception:
                    slot._dirty = True
                    logger.warning(
                        "tag delete: slot strip persist failed for %s; "
                        "marked dirty for periodic-flush retry",
                        getattr(slot, "key", "?"),
                        exc_info=True,
                    )

        # Strip from sidebar columns (flat list of column dicts).
        changed_boards = False
        for col in state._tag_boards:
            tag_ids = col.get("tag_ids") or []
            filtered = [t for t in tag_ids if t != tid]
            if len(filtered) != len(tag_ids):
                col["tag_ids"] = filtered
                changed_boards = True
        if changed_boards:
            try:
                boards_snapshot = [dict(c) for c in state._tag_boards]
                await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snapshot)
            except Exception:
                # Dangling board reference — pruned on next load.
                logger.warning("tag delete: board strip persist failed for %s", tid, exc_info=True)

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_delete",
        outcome="allowed",
        source="dashboard",
        resources=tid,
    )
    return web.json_response({"ok": True})


# ── Slot tag assignment ────────────────────────────────────────────────────


async def api_chat_slot_tags(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/tags — replace the slot's tag list.

    Holds the tags write lock across validate→assign→persist so a concurrent
    DELETE cannot remove a tag id between validation and slot persistence
    (preventing dangling references).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    raw_ids = body.get("tags")
    if not isinstance(raw_ids, list):
        return web.json_response(
            {"error": "tags must be an array", "code": "tags_not_array"}, status=400
        )

    async with _tags_write_lock(state):
        valid_ids = {t.get("id") for t in state._tags}
        new_tags: list[str] = []
        for tid in raw_ids:
            if isinstance(tid, str) and tid in valid_ids and tid not in new_tags:
                new_tags.append(tid)
        slot.tags = new_tags
        await save_slot_off_loop(state, slot, force=True)

    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_tags",
        outcome="allowed",
        source="dashboard",
        resources=name,
    )
    return web.json_response({"ok": True, "tags": slot.tags})


# ── Sidebar columns (Trello-style filtered lanes) ──────────────────────────


def _normalize_column(
    state: DashboardState, raw: Any, *, existing: dict | None = None
) -> dict | None:
    """Validate + coerce a column payload. Returns None if invalid."""
    if not isinstance(raw, dict):
        return None
    valid_ids = {t.get("id") for t in state._tags}
    cleaned: dict[str, Any] = dict(existing or {})
    if "tag_ids" in raw:
        tag_ids = raw.get("tag_ids") or []
        if not isinstance(tag_ids, list):
            return None
        cleaned["tag_ids"] = [str(t) for t in tag_ids if isinstance(t, str) and t in valid_ids]
    if "mode" in raw:
        mode = str(raw.get("mode") or "any")
        if mode not in _VALID_MODES:
            return None
        cleaned["mode"] = mode
    if "name" in raw:
        cleaned["name"] = str(raw.get("name") or "").strip()[:_NAME_MAX]
    if "order" in raw:
        order_val = raw.get("order")
        if order_val is not None:
            try:
                cleaned["order"] = int(order_val)
            except (TypeError, ValueError):
                pass
    if "include_untagged" in raw:
        cleaned["include_untagged"] = bool(raw.get("include_untagged"))
    cleaned.setdefault("mode", "any")
    cleaned.setdefault("tag_ids", [])
    cleaned.setdefault("name", "")
    cleaned.setdefault("order", 0)
    cleaned.setdefault("include_untagged", False)
    return cleaned


async def api_chat_tag_columns(request: web.Request) -> web.Response:
    """GET /api/chat/tag-columns — list sidebar column layout."""
    state: DashboardState = request.app["state"]
    return web.json_response(sorted(state._tag_boards, key=lambda c: c.get("order", 0)))


async def api_chat_tag_column_create(request: web.Request) -> web.Response:
    """POST /api/chat/tag-columns — append a new sidebar column."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    async with _tags_write_lock(state):
        column = _normalize_column(state, {**body, "order": len(state._tag_boards)})
        if column is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        column["id"] = uuid.uuid4().hex[:12]
        state._tag_boards.append(column)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards = [c for c in state._tag_boards if c.get("id") != column["id"]]
            logger.warning("tag column create failed to persist: %s", column["id"])
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_create",
        outcome="allowed",
        source="dashboard",
        resources=str(column["id"]),
    )
    return web.json_response(column, status=201)


async def api_chat_tag_column_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/tag-columns/{id} — rename / retag / reorder."""
    state: DashboardState = request.app["state"]
    cid = request.match_info["id"]
    column = next((c for c in state._tag_boards if c.get("id") == cid), None)
    if not column:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    async with _tags_write_lock(state):
        # Re-check under lock (column may have been deleted concurrently).
        column = next((c for c in state._tag_boards if c.get("id") == cid), None)
        if not column:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        merged = _normalize_column(state, body, existing=column)
        if merged is None:
            return web.json_response(
                {"error": "invalid column payload", "code": "invalid_column_payload"}, status=400
            )
        original = dict(column)
        column.update(merged)
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            column.clear()
            column.update(original)
            logger.warning("tag column update failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_update",
        outcome="allowed",
        source="dashboard",
        resources=cid,
    )
    return web.json_response(column)


async def api_chat_tag_column_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/tag-columns/{id} — remove a column."""
    state: DashboardState = request.app["state"]
    cid = request.match_info["id"]
    if not any(c.get("id") == cid for c in state._tag_boards):
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    async with _tags_write_lock(state):
        # Re-check under lock.
        removed = [c for c in state._tag_boards if c.get("id") == cid]
        if not removed:
            return web.json_response({"error": "not found", "code": "not_found"}, status=404)
        state._tag_boards = [c for c in state._tag_boards if c.get("id") != cid]
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            state._tag_boards.extend(removed)
            logger.warning("tag column delete failed to persist: %s", cid)
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_column_delete",
        outcome="allowed",
        source="dashboard",
        resources=cid,
    )
    return web.json_response({"ok": True})


async def api_chat_tag_columns_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/tag-columns/order — reorder columns by id list."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    ids = body.get("ids")
    if not isinstance(ids, list):
        return web.json_response(
            {"error": "ids must be an array", "code": "ids_not_array"}, status=400
        )
    async with _tags_write_lock(state):
        original_order = [(col.get("id"), col.get("order", 0)) for col in state._tag_boards]
        order_map = {str(cid): i for i, cid in enumerate(ids)}
        # Push columns not present in the reorder payload past the explicit
        # ordering so they don't collide with the new sequential indices.
        next_order = len(order_map)
        for col in state._tag_boards:
            cid = col.get("id")
            if cid in order_map:
                col["order"] = order_map[cid]
            else:
                col["order"] = next_order
                next_order += 1
        state._tag_boards.sort(key=lambda c: c.get("order", 0))
        boards_snap = [dict(c) for c in state._tag_boards]
        try:
            await asyncio.to_thread(state.save_tag_boards_snapshot, boards_snap)
        except Exception:
            # Restore original order.
            for col in state._tag_boards:
                for cid, order in original_order:
                    if col.get("id") == cid:
                        col["order"] = order
                        break
            state._tag_boards.sort(key=lambda c: c.get("order", 0))
            logger.warning("tag columns reorder failed to persist")
            return web.json_response(
                {"error": "persist failed", "code": "persist_failed"}, status=500
            )
    sel().log_api_access(
        caller="dashboard",
        operation="chat.tag_columns_reorder",
        outcome="allowed",
        source="dashboard",
        resources=",".join(str(x) for x in ids[:10]),
    )
    return web.json_response({"ok": True})


# ── Drag-drop: move a session between columns (reassigns status tags) ──────


async def api_chat_slot_drop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/drop — move a session into a column.

    Destination rule: if the target column's tag_ids contains exactly one
    *status* tag, strip every status tag from the slot and add that one.
    Non-status tags are preserved. Any other configuration is a no-op so users
    can have filter-only columns without accidental data loss.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "not_found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    column_id = str(body.get("column_id") or "")
    column = next((c for c in state._tag_boards if c.get("id") == column_id), None)
    if not column:
        return web.json_response(
            {"error": "column not found", "code": "column_not_found"}, status=404
        )
    tag_index = {t["id"]: t for t in state._tags}
    col_tags = [tag_index[t] for t in column.get("tag_ids") or [] if t in tag_index]
    status_tags = [t for t in col_tags if t.get("status")]
    if len(status_tags) != 1:
        # Column doesn't carry exactly one status tag — there's no
        # unambiguous status to assign on drop, so this is a visual no-op
        # (covers both the unfiltered and the multi-status filter cases).
        # Audit the rejected drop so the SEL trail captures every attempt.
        sel().log_api_access(
            caller="dashboard",
            operation="chat.slot_drop",
            outcome="rejected",
            source="dashboard",
            resources=f"{name}->{column_id}",
            error="column is not a status lane",
        )
        return web.json_response(
            {"ok": False, "reason": "column is not a status lane", "tags": slot.tags}
        )
    target_id = status_tags[0]["id"]
    kept = [t for t in slot.tags if t in tag_index and not tag_index[t].get("status")]
    slot.tags = kept + [target_id]
    await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slot_drop",
        outcome="allowed",
        source="dashboard",
        resources=f"{name}->{column_id}",
    )
    return web.json_response({"ok": True, "tags": slot.tags})
