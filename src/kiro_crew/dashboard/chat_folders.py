"""Folder management — CRUD, pin, assignment, icon generation."""

from __future__ import annotations

import asyncio
import logging
import os
import unicodedata
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_persistence import save_slot_off_loop
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.executors import subprocess_executor
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

_folder_icon_lock = asyncio.Lock()


def _is_single_emoji(s: str) -> bool:
    """True if `s` is exactly one emoji grapheme (no letters/digits/text).

    Accepts simple emoji, variation-selector / skin-tone modified emoji, ZWJ
    sequences (families, professions), and two-codepoint flag pairs. Rejects
    empty strings, plain text, and multiple emoji.
    """
    if not s or len(s) > 16:
        return False
    modifiers = {0xFE0F, 0x200D}  # variation selector-16, zero-width joiner

    def _emoji_char(c: str) -> bool:
        o = ord(c)
        return (
            unicodedata.category(c).startswith("So")  # symbol, other
            or o > 0x1F000                            # supplementary emoji planes
            or o in modifiers
            or 0x1F3FB <= o <= 0x1F3FF                 # skin-tone modifiers
            or 0x1F1E6 <= o <= 0x1F1FF                 # regional indicators (flags)
        )

    if not all(_emoji_char(c) for c in s):
        return False
    # Count grapheme clusters; must be exactly one.
    cps = [ord(c) for c in s]
    n = len(cps)
    clusters = 0
    i = 0
    while i < n:
        if 0x1F1E6 <= cps[i] <= 0x1F1FF:  # flag = pair of regional indicators
            clusters += 1
            i += 2 if (i + 1 < n and 0x1F1E6 <= cps[i + 1] <= 0x1F1FF) else 1
        else:
            clusters += 1  # base emoji, then absorb modifiers / ZWJ-joined emoji
            i += 1
            while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                i += 1
            while i < n and cps[i] == 0x200D:  # ZWJ joins the following emoji
                i += 2 if i + 1 < n else 1
                while i < n and (cps[i] == 0xFE0F or 0x1F3FB <= cps[i] <= 0x1F3FF):
                    i += 1
        if clusters > 1:
            return False
    return clusters == 1


_FOLDER_ICON_MODEL = "claude-haiku-4.5"


async def generate_emoji_for_name(state: DashboardState, name: str) -> str:
    """Ask the cheapest model for ONE emoji representing a folder ``name``.

    Shared by chat folders and artifact-library folders. Serialized via a
    module-level lock so concurrent folder creations don't interleave streams
    on the shared BACKGROUND_KEY session. Returns ``""`` on any failure or
    when the reply isn't exactly one emoji grapheme.
    """

    prompt = (
        f"Reply with exactly ONE emoji that best represents a project folder named \"{name}\". "
        "No text, no explanation, just the single emoji character."
    )

    # Folder icon is a trivial single-emoji task — run on the cheapest model via
    # the shared background one-liner helper (best-effort, 30s bound, denials
    # SEL-logged). The lock serializes icon generation across folders.
    async with _folder_icon_lock:
        try:
            text = await run_bg_oneliner(
                state.sessions,
                prompt,
                model=_FOLDER_ICON_MODEL,
                sel_source="chat_folders",
                timeout=30,
            )
        except Exception:  # noqa: BLE001 — best-effort background task
            text = ""
    icon = text.strip()
    icon, _ = redact_exfiltration_urls(icon)
    icon, _ = redact_credentials(icon)
    # Validate: must be exactly one emoji (guard against stray LLM text).
    return icon if _is_single_emoji(icon) else ""


async def _generate_folder_icon(state: DashboardState, folder: dict) -> None:
    """Background task: ask LLM for a single emoji for the folder name."""
    icon = await generate_emoji_for_name(state, folder["name"])
    if not icon:
        return

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, bool]:
        # Re-find under the store lock: the folder may have been deleted while
        # the model call was in flight.
        target = next((f for f in folders if f["id"] == folder["id"]), None)
        if target is None:
            return False, False
        target["icon"] = icon
        return True, True

    if await state.mutate_folders(_apply):
        state.push_slots_update()


def _folder_history_counts(state: DashboardState) -> dict[str, int]:
    """Count on-disk (history) sessions filed in each folder, keyed by folder_id.

    Authoritative per-folder archived-session count computed from the full
    session list, NOT the paginated client history window. The sidebar uses it
    to decide whether an empty folder can be hidden (it has an archived session
    that can revive it) or must be deleted instead (nothing could revive it).
    """
    counts: dict[str, int] = {}
    if not state.conversation_log:
        return counts
    for session in state.conversation_log.list_sessions():
        fid = session.get("folder_id")
        if fid:
            counts[fid] = counts.get(fid, 0) + 1
    return counts


def _folders_with_history_counts(state: DashboardState) -> list[dict]:
    """Folders enriched with a computed, non-persisted `history_count` field."""
    counts = _folder_history_counts(state)
    return [{**f, "history_count": counts.get(f["id"], 0)} for f in state._folders]


async def _unhide_folder(state: DashboardState, folder_id: str) -> None:
    """Clear a folder's `hidden` flag when a session re-engages it.

    Model-B semantics: reviving or moving a session into a folder un-hides it so
    it stays visible until the user hides it again. Persists on change (and only
    on change); the caller is responsible for pushing the slots update.
    """
    if not folder_id:
        return

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        for f in folders:
            if f["id"] == folder_id and f.get("hidden"):
                f["hidden"] = False
                return True, None
        return False, None

    await state.mutate_folders(_apply)


async def api_chat_folders(request: web.Request) -> web.Response:
    """GET /api/chat/folders — list all project folders (with archived-session counts)."""
    state: DashboardState = request.app["state"]
    # _folders_with_history_counts walks the on-disk session list (a synchronous
    # filesystem scan) that is user-triggered (every GET) and scales with the
    # archived-session count. Offload it to keep the event loop responsive, using
    # subprocess_executor (the pool for potentially-slow work) rather than
    # maintenance_executor, whose fast periodic sweeps — the orphan reaper — must
    # stay responsive and could otherwise be starved by frequent polling.
    loop = asyncio.get_running_loop()
    folders = await loop.run_in_executor(
        subprocess_executor(), _folders_with_history_counts, state
    )
    return web.json_response(folders)


def _validate_project_dir(raw: str) -> tuple[str, str | None]:
    """Validate and normalize project_dir. Returns (resolved_path, error_msg)."""
    if not raw:
        return "", None
    if not os.path.isabs(raw) and not raw.startswith("~"):
        return "", "project_dir must be an absolute path"
    resolved = os.path.realpath(os.path.expanduser(raw))
    if is_sensitive_path(resolved):
        sel().log_api_access(
            caller="dashboard", operation="chat.folder_project_dir",
            outcome="denied", resources=resolved, error="sensitive path",
        )
        return "", "project_dir refers to a sensitive path"
    if not os.path.isdir(resolved):
        return "", "project_dir must be an existing directory"
    return resolved, None


def _is_descendant(folders: list[dict], *, ancestor_id: str, folder_id: str) -> bool:
    """True if `folder_id` is `ancestor_id` or lies anywhere under it.

    Walks parent_id links upward from `folder_id` with a visited-set guard
    so pre-existing corrupt cycles in folders.json can't hang the request.
    """
    by_id = {f["id"]: f for f in folders}
    seen: set[str] = set()
    cur: str | None = folder_id
    while cur and cur not in seen:
        if cur == ancestor_id:
            return True
        seen.add(cur)
        node = by_id.get(cur)
        cur = str(node.get("parent_id") or "") if node else None
    return False


async def api_chat_folder_create(request: web.Request) -> web.Response:
    """POST /api/chat/folders — create a project folder."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    name = (body.get("name") or "").strip()[:100]
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    parent_id = str(body.get("parent_id") or "")
    if parent_id and not any(f["id"] == parent_id for f in state._folders):
        return web.json_response({"error": "parent folder not found"}, status=400)
    project_dir = str(body.get("project_dir") or "").strip()
    project_dir, err = _validate_project_dir(project_dir)
    if err:
        return web.json_response({"error": err}, status=400)
    # `icon` and `default_agent` were previously PATCH-only, so the create modal
    # had to follow every creation with an update. Accepting them here makes a
    # fully-configured folder one request, and — for `icon` — avoids a visible
    # flash of the auto-generated emoji before the user's choice lands.
    default_agent = str(body.get("default_agent") or "").strip()
    icon = str(body.get("icon") or "").strip()
    if icon and not _is_single_emoji(icon):
        # `code` is the contract, `error` is advisory prose (RFC 9457 3.1.3) —
        # the dashboard renders `error` verbatim into a localized UI, so a new
        # error response without an id is untranslatable by construction.
        return web.json_response(
            {"error": "icon must be a single emoji", "code": "icon_not_single_emoji"},
            status=400,
        )
    folder = {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "order": len(state._folders),
        "collapsed": False,
        "hidden": False,
        "parent_id": parent_id,
        "project_dir": project_dir,
        "default_agent": default_agent,
    }
    if icon:
        folder["icon"] = icon[:16]

    def _append(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        folder["order"] = len(folders)  # recount under the lock
        folders.append(folder)
        return True, None

    await state.mutate_folders(_append)
    state.push_slots_update()
    # Generate icon in background — don't block the response. Skipped when the
    # caller chose an icon: _generate_folder_icon writes unconditionally on
    # success, so running it anyway would silently clobber that choice a few
    # seconds after creation (the same conflict PATCH rejects outright).
    if not icon:
        task = asyncio.ensure_future(_generate_folder_icon(state, folder))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_create",
        outcome="allowed", source="dashboard", resources=str(folder["id"]),
    )
    return web.json_response(folder, status=201)


async def api_chat_folder_update(request: web.Request) -> web.Response:
    """PATCH /api/chat/folders/{id} — rename or reorder a folder."""
    state: DashboardState = request.app["state"]
    fid = request.match_info["id"]
    folder = next((f for f in state._folders if f["id"] == fid), None)
    if not folder:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Validate ALL submitted fields into a pending-changes dict BEFORE mutating
    # ``folder`` — otherwise an early field (e.g. name) is persisted while a later
    # field (e.g. an invalid/cyclic parent_id) returns 400, leaving the rejected
    # request's partial mutation live for the next successful save.
    changes: dict[str, object] = {}
    if "name" in body:
        new_name = str(body["name"]).strip()[:100]
        if not new_name:
            return web.json_response({"error": "name required"}, status=400)
        changes["name"] = new_name
    if "collapsed" in body:
        changes["collapsed"] = bool(body["collapsed"])
    if "hidden" in body:
        changes["hidden"] = bool(body["hidden"])
    if "order" in body:
        changes["order"] = int(body["order"])
    if "default_agent" in body:
        val = body["default_agent"]
        changes["default_agent"] = str(val).strip() if val is not None else ""
    reparenting = "parent_id" in body
    new_parent = ""
    if reparenting:
        # Re-parent: move this folder into another folder, or to the top
        # level ("" / null). Reject self-parenting and cycles (the new
        # parent must not be the folder itself or any of its descendants).
        #
        # Self-parenting is state-independent, so it is decided here. The other
        # two conditions depend on the CURRENT tree, and this check runs before
        # the store lock is taken — so it is only a fast reject. The
        # authoritative parent-exists / cycle test is repeated inside ``_apply``
        # under the lock: two opposite reparents (A into B, B into A) can both
        # pass here against the same pre-state and would otherwise both apply,
        # persisting a cycle that makes both folders unreachable in the tree.
        new_parent = str(body["parent_id"] or "")
        if new_parent:
            if new_parent == fid:
                return web.json_response({"error": "folder cannot be its own parent"}, status=400)
            if not any(f["id"] == new_parent for f in state._folders):
                return web.json_response({"error": "parent folder not found"}, status=400)
            if _is_descendant(state._folders, ancestor_id=fid, folder_id=new_parent):
                return web.json_response(
                    {"error": "cannot move a folder into its own descendant"},
                    status=400,
                )
        changes["parent_id"] = new_parent
    if "project_dir" in body:
        pd, err = _validate_project_dir(str(body["project_dir"] or "").strip())
        if err:
            return web.json_response({"error": err}, status=400)
        changes["project_dir"] = pd
    if "icon" in body and body.get("regenerate_icon"):
        # Mutually exclusive: a manual icon would be saved and returned, then the
        # background regeneration would silently overwrite it. Reject the
        # ambiguous request so the conflict is explicit to the caller.
        return web.json_response(
            {"error": "Cannot set icon and regenerate_icon in the same request"},
            status=400,
        )
    if "icon" in body:
        # User-chosen emoji. None or empty string clears to the default;
        # otherwise it must be exactly one emoji (no text / multiple emoji).
        raw_icon = body["icon"]
        icon_val = str(raw_icon).strip() if raw_icon is not None else ""
        if icon_val and not _is_single_emoji(icon_val):
            return web.json_response({"error": "icon must be a single emoji"}, status=400)
        changes["icon"] = icon_val[:16]
    # All fields validated — apply atomically under the store lock, re-finding
    # the folder there so a concurrent delete cannot resurrect it, and
    # re-deciding the tree-shape rules there so two concurrent reparents cannot
    # each validate against the pre-state and persist a cycle between them.

    def _apply(folders: list[dict[str, Any]]) -> tuple[bool, str]:
        target = next((f for f in folders if f["id"] == fid), None)
        if target is None:
            return False, "not_found"
        if reparenting and new_parent:
            if not any(f["id"] == new_parent for f in folders):
                return False, "parent_not_found"
            if _is_descendant(folders, ancestor_id=fid, folder_id=new_parent):
                return False, "cycle"
        target.update(changes)
        return True, ""

    err = await state.mutate_folders(_apply)
    if err == "not_found":
        # Deleted between the validation above and acquiring the store lock.
        return web.json_response(
            {"error": "not found", "code": "folder_not_found"}, status=404
        )
    if err == "parent_not_found":
        # The parent was deleted while this request waited for the lock.
        return web.json_response(
            {"error": "parent folder not found", "code": "folder_parent_not_found"},
            status=400,
        )
    if err == "cycle":
        # A concurrent reparent moved the target under this folder while this
        # request waited for the lock; applying it now would persist a cycle.
        return web.json_response(
            {
                "error": "cannot move a folder into its own descendant",
                "code": "folder_cycle",
            },
            status=409,
        )
    if body.get("regenerate_icon"):
        # "Reset to auto" — re-run the LLM emoji generator in the background.
        # _generate_folder_icon saves + pushes a slots update on success.
        task = asyncio.create_task(_generate_folder_icon(state, folder))
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_update",
        outcome="allowed", source="dashboard", resources=fid,
    )
    return web.json_response(folder)


async def api_chat_folder_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/folders/{id} — delete a folder, ungroup its slots."""

    state: DashboardState = request.app["state"]
    fid = request.match_info["id"]
    if not any(f["id"] == fid for f in state._folders):
        return web.json_response({"error": "not found"}, status=404)

    def _remove(folders: list[dict[str, Any]]) -> tuple[bool, None]:
        for f in folders:
            if f.get("parent_id") == fid:
                f["parent_id"] = ""
        # In place, not a rebind: mutate_folders snapshots the list object it
        # was given, and other holders of state._folders must see the removal.
        folders[:] = [f for f in folders if f["id"] != fid]
        return True, None

    await state.mutate_folders(_remove)
    for slot in state._slots.values():
        if slot.folder_id == fid:
            slot.folder_id = ""
            await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.folder_delete",
        outcome="allowed", source="dashboard", resources=fid,
    )
    return web.json_response({"ok": True})


async def api_chat_slot_folder(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/folder — assign slot to a folder."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response({"error": "folder not found"}, status=400)
    if folder_id != slot.folder_id:
        slot._folder_changed = True  # re-inject [FOLDER] breadcrumb on next turn
    slot.folder_id = folder_id
    await _unhide_folder(state, folder_id)
    await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_folder",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "folder_id": slot.folder_id})


async def api_chat_slot_pin(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/pin — toggle pinned state."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    slot.pinned = bool(body.get("pinned", False))
    await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_pin",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "pinned": slot.pinned})


_VALID_MODES = ("", "orchestrator")


async def api_chat_slot_mode(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/mode — switch session mode."""

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = body.get("mode", "")
    if mode not in _VALID_MODES:
        return web.json_response({"error": "invalid mode"}, status=400)
    if slot.running:
        sel().log_api_access(
            caller="dashboard", operation="chat.slot_mode",
            outcome="denied", source="dashboard", resources=name,
        )
        return web.json_response(
            {"error": "cannot switch mode while session is running"}, status=409
        )
    slot.mode = mode
    # Clear orchestrator auto-run flag when leaving orchestrator mode to
    # prevent stale "Go All" state from triggering on re-entry.
    if mode != "orchestrator" and getattr(slot, "_auto_run", False):
        slot._auto_run = False
    await save_slot_off_loop(state, slot, force=True)
    state.push_slots_update()
    sel().log_api_access(
        caller="dashboard", operation="chat.slot_mode",
        outcome="allowed", source="dashboard", resources=name,
    )
    return web.json_response({"ok": True, "mode": slot.mode})
