"""Task routes — read/edit the extracted task list and file reviewed tasks.

``GET    …/{id}/tasks``           the meeting's extracted tasks
``POST   …/{id}/tasks``           add a task by hand
``PATCH  …/{id}/tasks``           edit one task's fields
``DELETE …/{id}/tasks``           remove a task
``POST   …/{id}/tasks/file``      file a task through the task provider
``GET    …/task-providers``       registered providers (for the settings picker)

Filing goes through the :mod:`..providers.tasks` seam — the shipped provider is
the local KiroCrew ledger. Upstream instead composed a natural-language prompt
naming a company-internal tracker and handed it to a dedicated agent; that agent
and its internal MCP servers are gone.

Every store read and write here is BLOCKING and runs on a worker thread via
``asyncio.to_thread``; the module-level ``_`` helpers are the grouped bodies those
threads execute. Each read-modify-write of ``tasks.json`` is inside ONE helper AND
under ``_TASKS_LOCK``: one thread hop keeps the read and the write together, and the
lock keeps two hops from interleaving. Both are needed — worker threads run
concurrently, and "Archive all" issues one request per task, so without the lock the
last write wins and every other update is discarded while all of them report success.

``handle_file_task`` is the one helper whose write must follow an ``await`` (only a
successful provider call may be recorded), so it cannot share a hop. It therefore
RE-READS under the lock in :func:`_record_filing` and applies only that task's two
fields, instead of writing the snapshot it captured before the await — which would
have reverted anything the extractor agent or another request changed meanwhile.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
import uuid
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.providers import tasks as taskprov
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    audit,
    data_root,
    field_str,
    field_str_list,
    json_body,
)
from kiro_crew.executors import subprocess_executor
from kiro_crew.security import redact

logger = logging.getLogger("kirocrew.app.meetings")

#: Serializes every read-modify-write of a meeting's ``tasks.json``.
#:
#: Each helper below reads the whole list, changes one entry, and writes the list
#: back — and they run on worker threads (``asyncio.to_thread``), so two requests
#: genuinely execute at once. "Archive all" fires one POST per task, which is the
#: easy way to hit it: without a lock the last write wins and every concurrent
#: update but one is discarded, while all of them report success. ``atomic_write``
#: never helped here — the WRITE was atomic, the read-modify-write around it was not.
#:
#: Module level, because a handler has no instance to hang a lock on. One lock for
#: all meetings rather than one per id: the critical section is a small file read
#: plus a write, so the contention is negligible next to the bookkeeping a per-id
#: registry would need. Held only across local file IO, never across an await.
_TASKS_LOCK = threading.Lock()

_MAX_TASKS = 500
_MAX_DESCRIPTION = 2000
_MAX_CONTEXT = 4000
_MAX_REF_FIELD_LEN = 500


#: A filed-task reference URL is rendered as an ``href`` by the dashboard, so only
#: absolute http(s) is accepted. A ``javascript:`` value written into
#: ``tasks.json`` by an agent would otherwise execute on the dashboard origin the
#: moment the user clicked the filed-task link.
_LINKABLE_URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _normalize_filed_ref(raw: Any) -> dict[str, str] | None:
    """Coerce a filed-task reference, dropping an unsafe or unusable URL.

    ``tasks.json`` is agent-written, so every field here is untrusted: the id is
    length-capped and redacted like the rest of the record, and the url is kept
    ONLY when it is absolute http(s). A rejected url leaves the id intact, which
    is what the UI falls back to rendering as plain text.
    """
    if not isinstance(raw, dict):
        return None
    ref: dict[str, str] = {}
    ref_id = redact(str(raw.get("id") or "").strip())[:_MAX_REF_FIELD_LEN]
    if ref_id:
        ref["id"] = ref_id
    url = str(raw.get("url") or "").strip()[:_MAX_REF_FIELD_LEN]
    if url and _LINKABLE_URL_RE.match(url):
        ref["url"] = redact(url)
    return ref or None


def _normalize_task(raw: Any) -> dict[str, Any] | None:
    """Coerce one task record into the app's schema, or drop it.

    Applied on every read AND write: ``tasks.json`` is written by an LLM agent,
    so the file's shape is untrusted input even though the app owns the path.
    """
    if not isinstance(raw, dict):
        return None
    description = redact(str(raw.get("description") or raw.get("text") or "").strip())
    if not description:
        return None
    priority = raw.get("priority")
    review = raw.get("review_status")
    return {
        "id": str(raw.get("id") or f"t{uuid.uuid4().hex[:8]}")[:64],
        "description": description[:_MAX_DESCRIPTION],
        "assignee": redact(str(raw.get("assignee") or "").strip())[:200],
        "priority": priority if priority in k.TASK_PRIORITIES else k.DEFAULT_TASK_PRIORITY,
        "status": raw.get("status") if raw.get("status") in k.TASK_STATES else "open",
        "context": redact(str(raw.get("context") or "").strip())[:_MAX_CONTEXT],
        "labels": [
            redact(str(lab).strip())[:100]
            for lab in (raw.get("labels") or [])
            if isinstance(lab, str) and str(lab).strip()
        ][:20],
        "review_status": review if review in k.VALID_REVIEW_STATES else k.REVIEW_PENDING,
        "filed_ref": _normalize_filed_ref(raw.get("filed_ref")),
    }


def read_normalized(meeting_id: str, root: Any) -> list[dict[str, Any]]:
    """Every task from ``tasks.json``, coerced and redacted. BLOCKING.

    Public because the meeting-lifecycle ``/outputs`` poll returns the task list
    too: it MUST come through here rather than straight off
    ``store.read_tasks``, or agent-written text reaches the dashboard unredacted.
    """
    doc = store.read_tasks(meeting_id, root)
    out: list[dict[str, Any]] = []
    for raw in doc.get("tasks", [])[:_MAX_TASKS]:
        task = _normalize_task(raw)
        if task is not None:
            out.append(task)
    return out


async def handle_get_tasks(request: web.Request) -> web.Response:
    meeting_id = _meeting_id(request)
    tasks = await asyncio.to_thread(read_normalized, meeting_id, data_root(request))
    return web.json_response({"tasks": tasks})


def _append_task(meeting_id: str, body: dict[str, Any], root: Any) -> dict[str, Any]:
    """Validate, append, and persist one hand-added task. BLOCKING.

    Runs on a worker thread, never the event loop: reads and re-normalizes the whole
    of ``tasks.json`` (up to ``_MAX_TASKS`` records, each with several ``redact()``
    passes) and then writes it back atomically.

    Grouped into ONE hop because the written list is the list just read plus the new
    record: splitting the read from the write would let two concurrent adds each
    write a list missing the other's task. The ``field_*`` calls stay INSIDE, after
    the read, so the cap check still precedes the remaining field validation exactly
    as it did inline; a ``BadRequest`` raised here propagates through the await into
    ``_common.guarded``.
    """
    with _TASKS_LOCK:
        description = field_str(body, "description", required=True, max_len=_MAX_DESCRIPTION)
        tasks = read_normalized(meeting_id, root)
        if len(tasks) >= _MAX_TASKS:
            raise BadRequest(f"a meeting is limited to {_MAX_TASKS} tasks")
        task = _normalize_task(
            {
                "id": f"t{int(time.time() * 1000)}",
                "description": description,
                "assignee": field_str(body, "assignee", max_len=200),
                "priority": body.get("priority"),
                "context": field_str(body, "context", max_len=_MAX_CONTEXT),
                "labels": field_str_list(body, "labels", max_items=20, max_len=100) or [],
            }
        )
        if task is None:  # pragma: no cover — description is validated above
            raise BadRequest("description is required")
        tasks.append(task)
        store.write_tasks(meeting_id, tasks, root)
        return {"ok": True, "task": task, "tasks": tasks}


async def handle_add_task(request: web.Request) -> web.Response:
    """Add a task by hand (the sidebar's quick-add)."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    payload = await asyncio.to_thread(_append_task, meeting_id, body, data_root(request))
    return web.json_response(payload)


def _patch_task(
    meeting_id: str, task_id: str, fields: dict[str, Any], root: Any
) -> dict[str, Any] | None:
    """Merge *fields* into one task and persist. None when the id is unknown. BLOCKING.

    Runs on a worker thread, never the event loop: a full read + re-normalize of
    ``tasks.json`` followed by an atomic write.

    Grouped into ONE hop because this is a read-modify-write of the whole task list
    — the write is the read's list with one element replaced, so splitting them
    would discard any task added concurrently.
    """
    with _TASKS_LOCK:
        tasks = read_normalized(meeting_id, root)
        updated: dict[str, Any] | None = None
        for index, task in enumerate(tasks):
            if task["id"] != task_id:
                continue
            merged = {**task, **fields, "id": task_id}
            normalized = _normalize_task(merged)
            if normalized is None:
                raise BadRequest("description cannot be empty")
            tasks[index] = normalized
            updated = normalized
            break
        if updated is None:
            return None
        store.write_tasks(meeting_id, tasks, root)
        return {"ok": True, "task": updated, "tasks": tasks}


async def handle_update_task(request: web.Request) -> web.Response:
    """Patch one task's editable fields."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    task_id = field_str(body, "id", required=True, max_len=64)
    fields = body.get("fields")
    if not isinstance(fields, dict):
        raise BadRequest("fields must be a JSON object")

    payload = await asyncio.to_thread(
        _patch_task, meeting_id, task_id, fields, data_root(request)
    )
    if payload is None:
        return web.json_response({"error": "task not found", "code": "task_not_found"}, status=404)
    return web.json_response(payload)


def _drop_task(meeting_id: str, task_id: str, root: Any) -> list[dict[str, Any]] | None:
    """Remove one task and persist the rest. None when the id is unknown. BLOCKING.

    Runs on a worker thread, never the event loop, and grouped into ONE hop for the
    same read-modify-write reason as :func:`_patch_task`.
    """
    with _TASKS_LOCK:
        tasks = read_normalized(meeting_id, root)
        remaining = [t for t in tasks if t["id"] != task_id]
        if len(remaining) == len(tasks):
            return None
        store.write_tasks(meeting_id, remaining, root)
        return remaining


async def handle_delete_task(request: web.Request) -> web.Response:
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    task_id = field_str(body, "id", required=True, max_len=64)
    remaining = await asyncio.to_thread(
        _drop_task, meeting_id, task_id, data_root(request)
    )
    if remaining is None:
        return web.json_response({"error": "task not found", "code": "task_not_found"}, status=404)
    return web.json_response({"ok": True, "tasks": remaining})


async def handle_task_providers(request: web.Request) -> web.Response:
    config = await asyncio.to_thread(store.read_config, data_root(request))
    return web.json_response(
        {
            "providers": taskprov.available_task_providers(),
            "active": config.get("task_provider", k.DEFAULT_TASK_PROVIDER),
        }
    )


def _prepare_filing(
    meeting_id: str, task_id: str, root: Any
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, dict[str, Any], dict[str, Any]]:
    """Read everything the filing needs: tasks, the target, config, meta. BLOCKING.

    Returns ``(tasks, target, config, meta)``; a ``None`` target is the caller's 404.

    Runs on a worker thread, never the event loop: a full read + re-normalize of
    ``tasks.json``, a config read, and a metadata read.

    Grouped into ONE hop so the provider is resolved from the same config snapshot
    that the meeting title is read alongside, and because the alternative is three
    sequential hops before a handler that must then await the provider call.
    """
    tasks = read_normalized(meeting_id, root)
    target = next((t for t in tasks if t["id"] == task_id), None)
    if target is None:
        return tasks, None, {}, {}
    return (
        tasks,
        target,
        store.read_config(root),
        store.read_meeting_meta(meeting_id, root) or {},
    )


def _record_filing(
    meeting_id: str, task_id: str, ref: taskprov.TaskRef, root: Any
) -> list[dict[str, Any]]:
    """Mark one task filed, against a FRESH read of the list. BLOCKING.

    Called after the provider call has already succeeded, so it re-reads under
    :data:`_TASKS_LOCK` and applies only this task's two fields — never the caller's
    pre-await snapshot, which would silently revert concurrent edits.
    """
    with _TASKS_LOCK:
        tasks = read_normalized(meeting_id, root)
        for index, task in enumerate(tasks):
            if task["id"] == task_id:
                tasks[index] = {
                    **task,
                    "review_status": k.REVIEW_PUSHED,
                    "filed_ref": ref.to_dict(),
                }
                break
        store.write_tasks(meeting_id, tasks, root)
        return tasks


async def handle_file_task(request: web.Request) -> web.Response:
    """File one reviewed task through the configured task provider.

    The provider call is synchronous (the local ledger writes a file; an edition
    provider may talk to a tracker over the network), so it runs on the
    subprocess executor rather than the gateway's event loop.
    """
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    root = data_root(request)
    task_id = field_str(body, "id", required=True, max_len=64)

    tasks, target, config, meta = await asyncio.to_thread(
        _prepare_filing, meeting_id, task_id, root
    )
    if target is None:
        return web.json_response({"error": "task not found", "code": "task_not_found"}, status=404)

    provider = taskprov.get_task_provider(str(config.get("task_provider") or ""), root)
    draft = taskprov.TaskDraft(
        description=target["description"],
        meeting_id=meeting_id,
        meeting_title=str(meta.get("title") or ""),
        assignee=target["assignee"],
        priority=target["priority"],
        context=target["context"],
        labels=list(target["labels"]),
    ).sanitized()

    try:
        ref = await asyncio.get_running_loop().run_in_executor(
            subprocess_executor(), provider.create, draft
        )
    except Exception as exc:
        logger.warning("meetings: task provider %s failed", provider.provider_id, exc_info=True)
        audit(
            "meetings.task_file",
            f"{provider.provider_id}:{task_id}",
            outcome="error",
            error=type(exc).__name__,
        )
        return web.json_response(
            {
                "ok": False,
                "error": f"could not file the task ({type(exc).__name__})",
                "code": "task_file_failed",
            }, status=502
        )

    # RE-READ under the lock rather than writing the list captured before the
    # provider call. That call is an `await` the write must follow (only a
    # successful filing may be recorded), so this is the one helper whose read and
    # write cannot share a hop — and writing the pre-await snapshot would roll back
    # every task the extractor agent or another request changed in between, while
    # reporting success. Re-reading and applying only THIS task's fields narrows the
    # lost update to nothing: the filing is recorded, everyone else's edits survive.
    tasks = await asyncio.to_thread(_record_filing, meeting_id, task_id, ref, root)
    audit("meetings.task_file", f"{provider.provider_id}:{ref.id}", outcome="ok")
    return web.json_response({"ok": True, "ref": ref.to_dict(), "tasks": tasks})


def _set_review_state(
    meeting_id: str, task_id: str, state: str, root: Any
) -> list[dict[str, Any]] | None:
    """Set one task's review state and persist. None when the id is unknown. BLOCKING.

    Runs on a worker thread, never the event loop, and grouped into ONE hop for the
    same read-modify-write reason as :func:`_patch_task`.
    """
    with _TASKS_LOCK:
        tasks = read_normalized(meeting_id, root)
        found = False
        for index, task in enumerate(tasks):
            if task["id"] == task_id:
                tasks[index] = {**task, "review_status": state}
                found = True
                break
        if not found:
            return None
        store.write_tasks(meeting_id, tasks, root)
        return tasks


async def handle_review_task(request: web.Request) -> web.Response:
    """Set a task's review state (pending / archived)."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    task_id = field_str(body, "id", required=True, max_len=64)
    state = field_str(body, "review_status", required=True, max_len=32)
    if state not in (k.REVIEW_PENDING, k.REVIEW_ARCHIVED):
        raise BadRequest("review_status must be 'pending' or 'archived'")

    tasks = await asyncio.to_thread(
        _set_review_state, meeting_id, task_id, state, data_root(request)
    )
    if tasks is None:
        return web.json_response({"error": "task not found", "code": "task_not_found"}, status=404)
    return web.json_response({"ok": True, "tasks": tasks})
