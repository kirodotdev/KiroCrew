"""Meeting lifecycle routes — init, start, pause/resume, stop, list, outputs.

``POST …/{id}/init``          create the meeting folder + seed files (idempotent)
``POST …/{id}/start``         activate: seed outputs, spawn agent sessions
``POST …/{id}/status``        move between active / paused / reviewing
``POST …/{id}/stop``          flush agents, mark ended
``GET  …/meetings``           list every meeting with metadata on disk
``GET  …/{id}``               one meeting's metadata
``DELETE …/{id}``             permanently remove an inactive meeting
``GET  …/{id}/outputs``       batch-read every agent output + tasks.json
``POST …/{id}/attachments``   add/remove context attachments
"""

from __future__ import annotations

import asyncio
import logging
from http import HTTPStatus
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    ACTIVE,
    START_LOCK,
    BadRequest,
    audit,
    data_root,
    field_str,
    field_str_list,
    hooks_of,
    json_body,
    sessions_of,
)
from kiro_crew.security import redact

logger = logging.getLogger("kirocrew.app.meetings")

# An attachment is a small, fixed-shape record; anything else is dropped rather
# than stored, so a malformed entry can never reach an agent prompt.
_ATTACHMENT_TYPES = ("file", "url")


def _meeting_id(request: web.Request) -> str:
    """The validated, filesystem-safe meeting id from the URL."""
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _clean_attachment(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    kind = raw.get("type")
    if kind not in _ATTACHMENT_TYPES:
        return None
    label = redact(str(raw.get("label") or "").strip())[:200]
    if kind == "url":
        url = str(raw.get("url") or "").strip()
        # Only http(s) — a file:// or javascript: "attachment" would be handed to
        # an agent as something to open.
        if not url.lower().startswith(("http://", "https://")) or len(url) > 2000:
            return None
        return {"type": "url", "url": redact(url), "label": label or url[:80]}
    path = str(raw.get("path") or "").strip()
    if not path or len(path) > 1000:
        return None
    return {"type": "file", "path": redact(path), "label": label or path.rsplit("/", 1)[-1]}


def _init_meeting(
    meeting_id: str, title: str, body: dict[str, Any], root: Any
) -> dict[str, Any]:
    """Create the meeting folder, metadata, tasks file, and agent outputs. BLOCKING.

    Runs on a worker thread, never the event loop: this is half a dozen filesystem
    operations (two directory creations, a JSON read, up to two atomic writes, and
    one seeded output file per enabled agent), and it is the first call the
    dashboard makes when a user opens a meeting. Inline, each of those syscalls
    stalls the gateway's single loop — the user's chat turn and the liveness
    heartbeat included.

    Grouped into ONE hop rather than a ``to_thread`` per store call so the
    metadata read and the writes derived from it cannot have another request
    interleaved between them.

    ``agents_enabled`` is validated HERE, after the folder work, because that is
    where the handler validated it inline — a malformed value must still 400 at the
    same point in the sequence rather than before the meeting folder exists.
    """
    store.ensure_data_dirs(root)
    mdir = store.meeting_dir(meeting_id, root)
    mdir.mkdir(parents=True, exist_ok=True)

    # Under the metadata lock: read-modify-write, on a worker thread, so a
    # concurrent request would otherwise interleave (see `store.meta_transaction`).
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            meta = store.new_meeting_meta(meeting_id, redact(title))
            store.write_meeting_meta(meeting_id, meta, root)

    if not store.tasks_path(meeting_id, root).is_file():
        store.write_tasks(meeting_id, [], root)

    config = store.read_config(root)
    agents_enabled = field_str_list(body, "agents_enabled") or meta.get("agents_enabled")
    enabled = sess.get_enabled_agents(config, agents_enabled)
    store.ensure_agent_files(meeting_id, enabled, meta.get("title", "Meeting Notes"), root)
    return meta


async def handle_meeting_init(request: web.Request) -> web.Response:
    """Create the meeting folder, metadata, tasks file, and agent outputs."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    title = field_str(body, "title", default="Meeting", max_len=k.MAX_TITLE_LEN)

    meta = await asyncio.to_thread(
        _init_meeting, meeting_id, title, body, data_root(request)
    )
    return web.json_response({"ok": True, "meeting_id": meeting_id, "meta": meta})


async def handle_get_meeting(request: web.Request) -> web.Response:
    """One meeting's metadata (the frontend's poll target)."""
    meeting_id = _meeting_id(request)
    meta = await asyncio.to_thread(store.read_meeting_meta, meeting_id, data_root(request))
    if meta is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)
    live = ACTIVE.get(meeting_id)
    return web.json_response(
        {
            "meta": meta,
            "live": live.status() if live is not None else None,
        }
    )


async def handle_list_meetings(request: web.Request) -> web.Response:
    # `list_meetings` globs `*/session.json` under the meetings root and JSON-parses
    # every hit, so it grows with the user's meeting history — off the loop.
    meetings = await asyncio.to_thread(store.list_meetings, data_root(request))
    return web.json_response({"meetings": meetings})


async def handle_delete_meeting(request: web.Request) -> web.Response:
    """Permanently remove an inactive meeting and every app-owned output."""
    meeting_id = _meeting_id(request)
    root = data_root(request)

    # Share the lifecycle lock with start/stop so a delete cannot pass the live
    # check and then race a start that begins writing into the same directory.
    async with START_LOCK:
        if ACTIVE.get(meeting_id) is not None:
            audit(
                "meetings.delete",
                meeting_id,
                outcome="denied",
                error="meeting is active",
            )
            return web.json_response(
                {
                    "error": "end the meeting before deleting it",
                    "code": "meeting_active",
                },
                status=HTTPStatus.CONFLICT,
            )
        deleted = await asyncio.to_thread(store.delete_meeting, meeting_id, root)

    if not deleted:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"},
            status=HTTPStatus.NOT_FOUND,
        )
    audit("meetings.delete", meeting_id, outcome="ok")
    return web.Response(status=HTTPStatus.NO_CONTENT)


def _begin_meeting(
    meeting_id: str,
    agents_enabled: list[str] | None,
    title: str,
    preset: str,
    muted: list[str],
    root: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Mark a meeting active on disk and read the config back. BLOCKING.

    Runs on a worker thread, never the event loop: ``start_meeting_meta`` alone is
    a config read, a metadata read, a metadata write and one seeded output file per
    enabled agent, and this handler then rewrites the metadata and re-reads the
    config.

    Grouped into ONE hop so the read-modify-write of the metadata (status,
    ``preset``, ``muted_agents``) happens without another request interleaving, and
    so the config the live session is built from is the one that was on disk at
    that moment.
    """
    with store.meta_transaction():
        meta = sess.start_meeting_meta(meeting_id, agents_enabled, title, root)
        if preset:
            meta["preset"] = preset
        meta["muted_agents"] = muted
        store.write_meeting_meta(meeting_id, meta, root)
    return meta, store.read_config(root)


async def handle_start_meeting(request: web.Request) -> web.Response:
    """Activate a meeting: seed outputs, build the live session, init agents."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    root = data_root(request)
    title = field_str(body, "title", default="", max_len=k.MAX_TITLE_LEN)
    preset = field_str(body, "preset", default="", max_len=120)
    agents_enabled = field_str_list(body, "agents_enabled")
    muted = field_str_list(body, "muted_agents") or []
    is_restart = bool(body.get("restart") is True)

    # One critical section from the "is another meeting active?" read through to the
    # install. Both the metadata IO and the drain below are awaits, so two starts
    # interleaving in that gap would BOTH pass the check and the second would replace
    # the first — whose transcript then fails to dispatch with a confusing 409.
    async with START_LOCK:
        existing = ACTIVE.get()
        if existing is not None and existing.meeting_id != meeting_id and not existing.expired:
            audit("meetings.start", meeting_id, outcome="denied", error="another meeting is active")
            return web.json_response(
                {"error": "another meeting is already active", "code": "meeting_already_active"}, status=409
            )

        meta, config = await asyncio.to_thread(
            _begin_meeting, meeting_id, agents_enabled, redact(title), preset, muted, root
        )
        session = sess.MeetingSession(
            meeting_id=meeting_id,
            sessions=sessions_of(request),
            hooks=hooks_of(request),
            agents_enabled=agents_enabled,
            config=config,
        )
        session.muted_agents = set(muted)
        # Drain the OUTGOING session before this one replaces it. `set()` cancels the
        # previous session's queues, so starting a second meeting while an earlier
        # (typically expired) one still held a half-batch discarded that transcript —
        # the same loss as the teardown paths, reached by a different route. Awaiting is
        # possible here because this is an `async def`; the previous justification for
        # the non-awaiting `set()` did not survive checking.
        outgoing = await ACTIVE.drain_and_clear()
        ACTIVE.set(session)

        # A replacement of a DIFFERENT meeting is a teardown of that meeting, so its
        # metadata needs the same terminal status every other teardown writes.
        #
        # Only an EXPIRED one can be here — the guard above 409s otherwise — and it
        # is gone for good: its session was just dropped, and reopening it would show
        # `active` with nothing installed, so its transcript dispatches would 409 into
        # the void. Two meetings persisting as `active` at once also breaks the
        # single-active-meeting invariant the list view reads.
        if outgoing is not None and outgoing.meeting_id != meeting_id:
            await asyncio.to_thread(sess.end_meeting_meta, outgoing.meeting_id, root)

        # ALWAYS initialize, restart or not, THEN send the restart notice.
        #
        # The restart branch used to skip `init_agents` entirely, on the assumption
        # that a restarted meeting's agents still remember their instructions. They
        # may not: the slots are ordinary kiro sessions and can have been reclaimed
        # (session cleanup, a gateway restart, an idle sweep) between stop and
        # restart. A fresh session then received only "continue appending to your
        # output" — an instruction that names no output — so it had no `OUTPUT_FILE`
        # and the notes and tasks silently stopped updating for the rest of the
        # meeting.
        #
        # Re-initializing a session that DOES remember is harmless: the init message
        # is idempotent by construction (it re-states the path and says "the file
        # already exists — overwrite it directly"), and `init_agents` writes no
        # files, so nothing already captured is lost. Ordering the notice last means
        # "disregard the previous 'Meeting ended' message" arrives after the
        # instructions it qualifies.
        #
        # INSIDE `START_LOCK`, which now also covers `handle_stop_meeting`. Agent
        # initialization is a long sequence of awaited dispatches, and it ran
        # unlocked: a stale Close in another tab could tear the session down midway,
        # so the remaining agents were initialized into a session no longer installed
        # while this request still answered `active` — a meeting the UI showed as
        # running, with no live session and an `ended` status on disk.
        #
        # The lock is what makes stop WAIT for a start to finish rather than
        # interleave with it. The cost is that a stop arriving during initialization
        # is delayed until the agents are ready, which is the correct order anyway:
        # the finalize notice stop broadcasts is only meaningful to agents that have
        # been told what they are doing.
        await sess.init_agents(session, meta, root)
        if is_restart:
            await sess.broadcast_system(session, k.SYSTEM_MEETING_RESTARTED)

    audit("meetings.start", meeting_id, outcome="ok")
    return web.json_response(
        {
            "ok": True,
            "status": k.STATUS_ACTIVE,
            "agents": sorted(meta.get("outputs", {}).keys()),
            "meta": meta,
        }
    )


def _apply_status(meeting_id: str, status: str, root: Any) -> dict[str, Any] | None:
    """Set a meeting's status on disk, or return None when it does not exist. BLOCKING.

    Runs on a worker thread, never the event loop: a metadata read plus an atomic
    metadata write.

    Grouped so the read-modify-write is a single hop — splitting it would let a
    concurrent status change land between the read and the write and be discarded.

    The TRANSITION is validated here, inside the lock, against the status actually
    on disk — not at the handler against a value read earlier. Checking outside
    would leave the same race the lock exists to close: two requests could each see
    a legal transition from the same starting status and the second would apply an
    illegal one.

    Raises :class:`BadRequest` for a transition the lifecycle does not allow. The
    dashboard greys out those buttons, but that is an affordance and not
    enforcement — the endpoint accepted any valid status name, so an authenticated
    ``POST status=idle`` against an ACTIVE meeting persisted "idle" while the live
    session stayed installed: transcription stopped feeding a meeting the UI still
    showed as running, and starting another answered 409 because ``ACTIVE`` was
    still held.
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return None
        current = str(meta.get("status") or k.STATUS_IDLE)
        # Same-status is always allowed: an idempotent retry of a request whose
        # response was lost must not fail.
        if status != current and status not in k.ALLOWED_TRANSITIONS.get(current, ()):
            raise BadRequest(
                f"a {current} meeting cannot move to {status}",
                status=HTTPStatus.CONFLICT,
                code="invalid_transition",
            )
        meta["status"] = status
        if status == k.STATUS_ENDED:
            meta["ended_at"] = store.utc_now_iso()
        store.write_meeting_meta(meeting_id, meta, root)
    return meta


async def handle_meeting_status(request: web.Request) -> web.Response:
    """Move a meeting between active / paused / reviewing."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    root = data_root(request)
    status = field_str(body, "status", default="", max_len=32)
    if status not in k.VALID_STATUSES:
        raise BadRequest(f"status must be one of {', '.join(k.VALID_STATUSES)}")

    meta = await asyncio.to_thread(_apply_status, meeting_id, status, root)
    if meta is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)

    session = ACTIVE.get(meeting_id)
    if session is not None and status in (k.STATUS_PAUSED, k.STATUS_REVIEWING, k.STATUS_ENDED):
        # A paused/reviewing meeting stops receiving transcription, so flush what
        # is queued rather than leaving a half-batch to expire with the session.
        await session.flush_all()
    if session is not None and status == k.STATUS_ENDED:
        # Already flushed above for this status, but use the draining teardown so a
        # future edit to the branch above cannot silently reintroduce the loss.
        await ACTIVE.drain_and_clear()

    return web.json_response({"ok": True, "status": status})


async def handle_stop_meeting(request: web.Request) -> web.Response:
    """End a meeting: flush every agent, send the finalize notice, mark ended.

    Takes ``START_LOCK``, so a stop cannot interleave with a start. Without it, a
    stale Close in one tab tore down a session another tab was still initializing:
    the remaining agents were initialized into a session no longer installed, and the
    start still answered `active` for a meeting with `ended` on disk and nothing live.

    Both directions matter, which is why the lock is shared rather than a second one:
    a stop landing mid-start waits for the agents to be ready (the finalize notice
    only means something to an initialized agent), and a start landing mid-stop waits
    for the teardown to complete rather than installing a session the stop then drops.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    async with START_LOCK:
        session = ACTIVE.get(meeting_id)
        if session is not None:
            await sess.broadcast_system(session, k.SYSTEM_MEETING_ENDED)
            # The finalize notice is itself enqueued, so the teardown MUST drain or
            # the very notice just broadcast would never reach the agents.
            await ACTIVE.drain_and_clear()
        # `end_meeting_meta` is itself a metadata read-modify-write; one hop keeps it
        # atomic with respect to the loop as well as off it.
        meta = await asyncio.to_thread(sess.end_meeting_meta, meeting_id, root)
    audit("meetings.stop", meeting_id, outcome="ok")
    return web.json_response({"ok": True, "status": k.STATUS_ENDED, "meta": meta})


def _collect_outputs(meeting_id: str, root: Any) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Read every configured agent's output and the task list. BLOCKING.

    Runs on a worker thread, never the event loop: the note-taker is prompted to
    rewrite its WHOLE file after each transcription batch, so these reads are
    unbounded, and `redact()` over a megabyte of notes measures in the tens of
    milliseconds. The dashboard polls this every few seconds for the length of a
    meeting, so doing it inline would stall every other task on the loop —
    including the liveness heartbeat — on a repeating timer.

    Both halves are redacted. The outputs are model-generated prose; the tasks
    come from `tasks.json`, which an agent writes, so they go through the task
    module's own normalizer (which redacts every field and drops a malformed
    record) rather than being forwarded raw.
    """
    config = store.read_config(root)
    agents = config.get("meeting_agents") or []
    outputs = {
        agent_id: redact(content)
        for agent_id, content in store.read_agent_outputs(meeting_id, agents, root).items()
    }
    return outputs, task_routes.read_normalized(meeting_id, root)


async def handle_get_outputs(request: web.Request) -> web.Response:
    """Batch-read every configured agent's output plus the task list."""
    meeting_id = _meeting_id(request)
    root = data_root(request)
    outputs, tasks = await asyncio.to_thread(_collect_outputs, meeting_id, root)
    return web.json_response({"outputs": outputs, "tasks": tasks})


def _apply_attachments(
    meeting_id: str, body: dict[str, Any], root: Any
) -> list[dict[str, Any]] | None:
    """Add or remove attachments on a meeting's metadata. BLOCKING.

    Runs on a worker thread, never the event loop: a metadata read plus an atomic
    metadata write.

    Grouped so the read-modify-write is ONE hop — the new list is derived from the
    list that was just read, so splitting the read from the write would let two
    concurrent adds each drop the other's attachment.

    Body validation stays inside this helper, after the read, so a request naming a
    meeting that does not exist still answers 404 before a malformed body answers
    400 (the order the handler had inline). ``BadRequest`` raised here propagates
    out of the ``to_thread`` await into ``_common.guarded`` unchanged.
    """
    # The attachment list is derived from the list just read, so the read and the
    # write must be ONE critical section: two concurrent adds each appended to the
    # same snapshot and the second write dropped the first attachment, with both
    # requests reporting success. The `field_*` validation stays inside, after the
    # read, so a 404 still precedes a 400 as it did before.
    with store.meta_transaction():
        return _apply_attachments_locked(meeting_id, body, root)


def _apply_attachments_locked(
    meeting_id: str, body: dict[str, Any], root: Any
) -> list[dict[str, Any]] | None:
    """The read-modify-write itself. Caller holds ``store.meta_transaction()``."""
    meta = store.read_meeting_meta(meeting_id, root)
    if meta is None:
        return None

    action = field_str(body, "action", default="add", max_len=16)
    attachments: list[dict[str, Any]] = list(meta.get("attachments") or [])

    if action == "add":
        raw_items = body.get("attachments")
        if not isinstance(raw_items, list):
            raise BadRequest("attachments must be a list")
        for raw in raw_items[: k.MAX_ATTACHMENTS]:
            cleaned = _clean_attachment(raw)
            if cleaned is not None and len(attachments) < k.MAX_ATTACHMENTS:
                attachments.append(cleaned)
    elif action == "remove":
        index = body.get("index")
        if not isinstance(index, int) or isinstance(index, bool):
            raise BadRequest("index must be an integer")
        if 0 <= index < len(attachments):
            attachments.pop(index)
    else:
        raise BadRequest("action must be 'add' or 'remove'")

    meta["attachments"] = attachments
    store.write_meeting_meta(meeting_id, meta, root)
    return attachments


async def handle_attachments(request: web.Request) -> web.Response:
    """Add or remove meeting context attachments."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    attachments = await asyncio.to_thread(
        _apply_attachments, meeting_id, body, data_root(request)
    )
    if attachments is None:
        return web.json_response({"error": "meeting not found", "code": "meeting_not_found"}, status=404)
    return web.json_response({"ok": True, "attachments": attachments})
