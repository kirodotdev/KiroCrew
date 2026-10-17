"""Direct controls over a spec's artifacts: approval, one task, label, archive.

These act on the documents and index entry without asking the agent. Each is
pinned to the creation the client rendered, and the two that race the agent's own
writes (approval and running a task) serialize on the directory turn lock.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from aiohttp import web

from ..parsers import (
    _APPROVABLE_PHASES,
    _MAX_FIELD,
    _SHA256_RE,
    _parse_tasks,
    _sha256_text,
    _task_prompt,
)
from ..repository import (
    _DELETING,
    _aload_index,
    _audit,
    _mutate_index,
    _read_spec_text,
    _slot_key,
    _touch_spec,
)
from ..runtime import _dispatch_turn, _ensure_worker_slot
from .execution_state import _effective_status
from .request_identity import _STALE_CLIENT_ERROR, _pinned_entry, _read_json, _require_auth
from .turn_guard import _agent_is_writing, _turn_lock


async def _handle_approve(request: web.Request) -> web.Response:
    """Serialize approval recording with every turn that can change the document."""
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    index = await _aload_index()
    meta = index.get(name)
    if not isinstance(meta, dict) or meta.get(_DELETING):
        return await _approve_locked(request)
    async with _turn_lock(str(meta.get("spec_dir", ""))):
        return await _approve_locked(request)


async def _approve_locked(request: web.Request) -> web.Response:
    """Record a human approval of one phase, against the version approved.

    Records rather than enforces, and the distinction is deliberate. The agent
    writes through its own file tools, so this API cannot enforce phase order
    without owning that filesystem access. The record still preserves who approved
    which exact text.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    phase = str(body.get("phase", "")).strip()
    if phase not in _APPROVABLE_PHASES:
        return web.json_response(
            {"code": "invalid_phase", "error": f"phase must be one of {list(_APPROVABLE_PHASES)}"},
            status=400,
        )
    claimed_hash = str(body.get("hash", "") or "")
    if not _SHA256_RE.match(claimed_hash):
        return web.json_response(
            {"code": "invalid_hash", "error": "hash must be a sha256 hex digest"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    spec_dir = Path(str(fresh.get("spec_dir", "")))
    captured_slot_key = str(fresh.get("slot_key", ""))
    fname = phase + ".md"

    def _current_hash() -> str:
        text = _read_spec_text(spec_dir, fname)
        return _sha256_text(text) if text is not None else ""

    actual = await asyncio.to_thread(_current_hash)
    if actual != claimed_hash:
        # Approving a version you have not seen records nothing meaningful, so the
        # client is sent back to re-read rather than having its claim trusted.
        return web.json_response(
            {
                "code": "doc_changed",
                "error": f"{fname} changed since you reviewed it — reload before approving",
                "current_hash": actual,
            },
            status=409,
        )
    user = str(request.get("user") or "")
    record = {"hash": claimed_hash, "at": time.time(), "user": user[:_MAX_FIELD]}

    def _record(index: dict) -> bool:
        meta = index.get(name)
        if meta is None or meta.get(_DELETING):
            return False
        if str(meta.get("spec_dir", "")) != str(spec_dir):
            return False
        if captured_slot_key and str(meta.get("slot_key", "")) != captured_slot_key:
            return False
        # Merged INSIDE the lock rather than by reading the dict out, editing it and
        # stamping it back: the read-modify-write would drop a second phase's
        # approval that landed in between, and this is the one field where losing a
        # record silently defeats the point of having it.
        existing = meta.get("approvals")
        approvals = dict(existing) if isinstance(existing, dict) else {}
        approvals[phase] = record
        meta["approvals"] = approvals
        meta["updated_at"] = time.time()
        return True

    if not await _mutate_index(_record):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_phase_approve", f"{name}/{phase}")
    return web.json_response({"ok": True, "phase": phase, "hash": claimed_hash})


async def _handle_run_task(request: web.Request) -> web.Response:
    """Run ONE task from tasks.md as a single turn.

    The whole-list handoff arms an autonudge loop over every unchecked task, which
    is the only granularity the app had: there was no way to run one task, and no
    way to see which task a run was on. This dispatches a single scoped turn and
    stops, and progress stays derived from the file's checkboxes.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    raw_index = body.get("index")
    if not isinstance(raw_index, int) or isinstance(raw_index, bool) or raw_index < 0:
        return web.json_response(
            {"code": "invalid_index", "error": "index must be a non-negative integer"}, status=400
        )
    claimed_hash = str(body.get("hash", "") or "")
    if not _SHA256_RE.match(claimed_hash):
        return web.json_response(
            {"code": "invalid_hash", "error": "hash must be a sha256 hex digest"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    state = request.app.get("state")
    # An autonudge loop already working the whole list would collide with a
    # single-task turn: both write the same files and both check boxes off.
    if (
        await _effective_status(name, fresh, state.get_slot(_slot_key(name)) if state else None)
        == "executing"
    ):
        return web.json_response(
            {
                "code": "already_executing",
                "error": "this spec is already building — pause it first",
            },
            status=409,
        )
    if _agent_is_writing(request, name):
        return web.json_response(
            {
                "code": "agent_running",
                "error": "the agent is busy right now — wait for the turn to finish",
            },
            status=409,
        )
    spec_dir = Path(str(fresh.get("spec_dir", "")))

    def _task_snapshot() -> tuple[dict | None, str]:
        tasks = _parse_tasks(_read_spec_text(spec_dir, "tasks.md") or "")
        if raw_index >= len(tasks):
            return None, "task_not_found"
        candidate = tasks[raw_index]
        # Position AND text must both still match. The agent rewrites tasks.md
        # between polls, so an index alone is a moving target and a click on
        # "task 3" could otherwise dispatch whatever ended up third.
        if candidate["hash"] != claimed_hash:
            return None, "task_changed"
        if candidate["done"]:
            return None, "task_done"
        return candidate, ""

    def _task_conflict(code: str) -> web.Response:
        errors = {
            "task_not_found": "that task is no longer in the list — reload",
            "task_changed": "that task changed since the list was rendered — reload and pick it again",
            "task_done": "that task is already checked off",
        }
        return web.json_response({"code": code, "error": errors[code]}, status=409)

    task, task_error = await asyncio.to_thread(_task_snapshot)
    if task_error:
        return _task_conflict(task_error)
    # Hold the same per-spec lock that Execute uses to claim execution and Delete
    # uses to reserve teardown BEFORE materializing the worker slot. If Delete
    # captured "no slot" while _ensure_worker_slot awaited and this request then
    # restored one, Delete's identity-pinned teardown would deliberately leave the
    # new slot behind as an orphan. Re-pin first under the lock; after that Delete
    # either already owns the entry and no slot is created, or waits until the task
    # publishes its slot/turn and can capture that exact runtime.
    async with _turn_lock(str(spec_dir)):
        before_slot = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
        )
        if before_slot is None:
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        current_slot = state.get_slot(_slot_key(name)) if state else None
        if await _effective_status(name, before_slot, current_slot) == "executing":
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building — pause it first",
                },
                status=409,
            )
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        slot = await _ensure_worker_slot(state, name, before_slot)
        if slot is None:
            return web.json_response(
                {
                    "code": "slot_owned_by_another_app",
                    "error": "this spec's chat session is owned by another app",
                },
                status=409,
            )
        # Slot setup awaits, so re-pin the creation before using the materialized
        # slot. Delete cannot cross the lock, while other identity mutations still
        # fail this check.
        final_fresh = await _touch_spec(
            name,
            expect_spec_dir=str(spec_dir),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
        )
        if final_fresh is None:
            return web.json_response(
                {"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409
            )
        if await _effective_status(name, final_fresh, slot) == "executing":
            return web.json_response(
                {
                    "code": "already_executing",
                    "error": "this spec is already building — pause it first",
                },
                status=409,
            )
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        # Slot setup and status reconciliation both await. The IDE can edit
        # tasks.md during either window, so the earlier snapshot is not safe
        # to dispatch. Execute and Delete cannot cross this final awaited reread,
        # and _dispatch_turn publishes slot.task synchronously before the lock is
        # released.
        task, task_error = await asyncio.to_thread(_task_snapshot)
        if task_error:
            return _task_conflict(task_error)
        assert task is not None
        if _agent_is_writing(request, name):
            return web.json_response(
                {
                    "code": "agent_running",
                    "error": "the agent is busy right now — wait for the turn to finish",
                },
                status=409,
            )
        _dispatch_turn(
            state,
            slot,
            _task_prompt(
                name,
                spec_dir,
                str(final_fresh.get("working_dir", "")),
                task["text"],
                task["index"],
            ),
        )
    _audit("spec_task_run", f"{name}#{raw_index}")
    return web.json_response({"ok": True, "index": raw_index})


async def _handle_title(request: web.Request) -> web.Response:
    """Set a spec's display label.

    A rename, but of the LABEL only -- and that limit is the design, not a
    shortcut. The name is simultaneously the on-disk directory under
    ``.kiro/specs/``, the ``spec/<name>`` git branch, and the chat slot key, and
    ``_owns_slot_key`` requires the key to ENCODE the indexed name. So renaming the
    identity would move a directory the IDE and CLI also read, rewrite a branch
    that may already have commits, and orphan the spec's transcript, which is the
    very thing delete-and-recreate loses. A label fixes what users actually hit --
    a spec misnamed at the New Spec screen -- and costs none of that.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    if "title" not in body:
        return web.json_response({"code": "title_required", "error": "title required"}, status=400)
    title = str(body.get("title") or "").strip()[:120]
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    # "" clears the label and the UI falls back to the name, so an empty title is
    # a reset rather than an error.
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(fresh.get("spec_dir", "")),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
            title=title,
        )
        is None
    ):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_title", name)
    return web.json_response({"ok": True, "title": title})


async def _handle_archive(request: web.Request) -> web.Response:
    """Move a spec out of the working set, or bring it back.

    The non-destructive counterpart to delete: documents, transcript and index
    entry all stay, so an archived spec is recoverable by definition. Delete was
    the only lifecycle operation besides create, which meant tidying up a finished
    spec and destroying it were the same act.
    """
    if denied := _require_auth(request):
        return denied
    name = request.match_info["name"]
    body = await _read_json(request)
    if isinstance(body, web.Response):
        return body
    archived = body.get("archived")
    if not isinstance(archived, bool):
        return web.json_response(
            {"code": "archived_required", "error": "archived must be a boolean"}, status=400
        )
    fresh = await _pinned_entry(request, name, body)
    if isinstance(fresh, web.Response):
        return fresh
    state = request.app.get("state")
    if (
        archived
        and await _effective_status(name, fresh, state.get_slot(_slot_key(name)) if state else None)
        == "executing"
    ):
        # Archiving a running spec would hide a loop that keeps editing files, so
        # the user would have no surface left to stop it from.
        return web.json_response(
            {"code": "spec_executing", "error": "pause this spec before archiving it"}, status=409
        )
    if (
        await _touch_spec(
            name,
            expect_spec_dir=str(fresh.get("spec_dir", "")),
            expect_slot_key=str(fresh.get("slot_key", "")) or None,
            archived=archived,
        )
        is None
    ):
        return web.json_response({"code": "stale_client", "error": _STALE_CLIENT_ERROR}, status=409)
    _audit("spec_archive" if archived else "spec_unarchive", name)
    return web.json_response({"ok": True, "archived": archived})
