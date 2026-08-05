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

#: Serializes the whole prepare -> provider-create -> record sequence of a filing.
#:
#: `_TASKS_LOCK` cannot do this job: it is a `threading.Lock` held only across local
#: file IO and explicitly never across an await, and the provider call in the middle
#: IS an await (a network round trip for a tracker provider). So two review tabs
#: filing the same task both passed their `_prepare_filing` read, both created an item
#: externally, and the second `_record_filing` overwrote the first's `filed_ref` — two
#: tracker items for one action item, with one reference lost and no way to find it.
#:
#: An asyncio lock, because what is being serialized is event-loop interleaving across
#: an await rather than concurrent worker threads. The terminal-`pushed` re-check
#: inside it is the other half: the lock orders the two filings, and the re-read is
#: what makes the SECOND one a no-op instead of a duplicate.
_FILING_LOCK = asyncio.Lock()

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
    # Bound to a local so the type check narrows once — two separate `raw.get("labels")`
    # calls leave mypy unable to see that the second is the value the first tested.
    raw_labels = raw.get("labels")
    labels = raw_labels if isinstance(raw_labels, list) else []
    return {
        # Redacted like every sibling field, and for the same reason: `tasks.json`
        # is AGENT-written, so the id is untrusted text that reaches the dashboard
        # in the API response. It was the one field here that skipped the pass,
        # which is exactly how a credential-shaped id would have crossed the
        # boundary while `description` beside it was scrubbed. Redact BEFORE the
        # truncation, so a marker cannot be sliced in half into something the
        # scanner no longer recognises.
        "id": redact(str(raw.get("id") or f"t{uuid.uuid4().hex[:8]}"))[:64],
        "description": description[:_MAX_DESCRIPTION],
        "assignee": redact(str(raw.get("assignee") or "").strip())[:200],
        "priority": priority if priority in k.TASK_PRIORITIES else k.DEFAULT_TASK_PRIORITY,
        "status": raw.get("status") if raw.get("status") in k.TASK_STATES else "open",
        "context": redact(str(raw.get("context") or "").strip())[:_MAX_CONTEXT],
        # `isinstance(..., list)` FIRST, not just truthiness. `tasks.json` is
        # agent-written, so `labels` is whatever the model emitted:
        #
        #   * `"labels": 1` is not iterable, so the comprehension raised TypeError —
        #     and `read_normalized` runs on every outputs poll, so one such record
        #     made every poll answer 500 for the rest of the meeting;
        #   * `"labels": "urgent"` IS iterable, and silently became
        #     `["u", "r", "g", "e", "n", "t"]` — six junk labels rather than one,
        #     which is the quieter half of the same bug.
        #
        # A non-list is dropped rather than coerced, matching how `_normalize_task`
        # already treats every other malformed field.
        "labels": [
            redact(str(lab).strip())[:100]
            for lab in labels
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
    # Ids are DE-DUPLICATED here, because `tasks.json` is agent-written and nothing
    # stops the model emitting `t1` twice. Every route keys on the id, so a duplicate
    # made them act on the wrong rows: `_patch_task` edited only the first match
    # while `_drop_task` deleted BOTH, and filing recorded the ref against one of
    # them arbitrarily. The user sees two rows and can address neither reliably.
    #
    # Renamed rather than dropped: the second row is a real task the extractor found,
    # so discarding it would lose meeting content to fix a bookkeeping problem. The
    # suffix is derived from the position, so the same file always normalizes to the
    # same ids — a client that just read the list can still act on what it saw.
    seen: set[str] = set()
    for index, raw in enumerate(doc.get("tasks", [])[:_MAX_TASKS]):
        task = _normalize_task(raw)
        if task is None:
            continue
        if task["id"] in seen:
            task["id"] = f"{task['id']}-{index}"[:64]
        seen.add(task["id"])
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
    # Under `_FILING_LOCK`, not just `_TASKS_LOCK`.
    #
    # A filing is prepare -> provider-create -> record, and the middle step is an
    # AWAIT. `_TASKS_LOCK` is a threading lock held only across local file IO, so it
    # cannot span that gap: a delete landing inside it removed the task while the
    # provider had already created the external item, and `_record_filing` then found
    # no matching id, broke out of its loop, wrote the list unchanged and reported
    # SUCCESS. The tracker item existed with nothing referencing it — an orphan the
    # user cannot find from either side, and the response said the filing worked.
    #
    # Deleting is the other half of the same critical section the filing already
    # serializes, so it takes the same lock. Ordering rather than rejecting: whichever
    # request wins runs to completion, so a delete before the provider call cancels the
    # filing (no external item), and one after it removes a task that is genuinely
    # filed — both coherent, unlike the interleaving.
    async with _FILING_LOCK:
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
                store.write_tasks(meeting_id, tasks, root)
                return tasks
        # The task is GONE and the external item already exists — do not write and do
        # not pretend this succeeded.
        #
        # `_FILING_LOCK` now covers deletion, so reaching here means the task went away
        # by some path that lock does not order (the extractor agent rewriting
        # `tasks.json`, a hand-edit, a future route). The reference is the only record
        # of what was created, so it is LOGGED rather than dropped: without it the
        # tracker item is unfindable from either side. Falling through to
        # `write_tasks` here would also have rewritten the list a concurrent deleter
        # had just written, undoing its work.
        logger.warning(
            "meetings: filed task %s of meeting %s vanished before the filing was "
            "recorded; the external item exists at %s and is now unreferenced",
            task_id,
            meeting_id,
            ref.to_dict().get("url") or ref.to_dict().get("id") or "<unknown>",
        )
        raise BadRequest(
            "the task was deleted while it was being filed; "
            "the external item was created and is not linked here",
            status=409,
            code="task_vanished_while_filing",
        )


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

    # One critical section from the read that decides whether to file, through the
    # provider call, to the record. See `_FILING_LOCK`: the provider call is an await,
    # so without this two tabs both passed the read and both created an item.
    async with _FILING_LOCK:
        return await _file_task_locked(meeting_id, task_id, root)


async def _file_task_locked(meeting_id: str, task_id: str, root: Any) -> web.Response:
    """The filing itself. Caller holds :data:`_FILING_LOCK`."""
    tasks, target, config, meta = await asyncio.to_thread(
        _prepare_filing, meeting_id, task_id, root
    )
    if target is None:
        return web.json_response({"error": "task not found", "code": "task_not_found"}, status=404)
    # ALREADY FILED — the second of two concurrent filings lands here, and so does a
    # double-click or a retry of a request whose response was lost. Answering success
    # with the existing ref is deliberate: the caller's intent ("this task should be
    # filed") is satisfied, and the alternative — creating a second tracker item — is
    # the bug. Read from the fresh list, so it reflects the first filing's write.
    if target.get("review_status") == k.REVIEW_PUSHED:
        audit("meetings.task_file", f"duplicate:{task_id}", outcome="ok")
        return web.json_response(
            {"ok": True, "ref": target.get("filed_ref") or {}, "tasks": tasks}
        )

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

    ``pushed`` is TERMINAL and is never overwritten here. It is not a third value
    this endpoint may set — it is the record that the task was actually filed with an
    external provider, written by :func:`_record_filing` together with the
    ``filed_ref`` that identifies the created item.

    The freshly-read status is what decides, not the caller's snapshot, so the check
    covers the race as well as the plain case: File and then Archive All (or a
    delayed archive from a second tab) had the archive land after the provider call
    returned, replacing ``pushed`` with ``archived``. The task then dropped out of the
    filed set and could be filed a SECOND time — a duplicate item in the tracker for
    one action item, with the original's ``filed_ref`` gone.

    Answers success rather than a conflict: the caller asked for a review disposition
    on a task whose disposition is already settled more strongly, so there is nothing
    for them to resolve and the response still carries the true list.
    """
    with _TASKS_LOCK:
        tasks = read_normalized(meeting_id, root)
        found = False
        for index, task in enumerate(tasks):
            if task["id"] == task_id:
                found = True
                if task.get("review_status") == k.REVIEW_PUSHED:
                    # Already filed: keep the record and report the real list.
                    return tasks
                tasks[index] = {**task, "review_status": state}
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
