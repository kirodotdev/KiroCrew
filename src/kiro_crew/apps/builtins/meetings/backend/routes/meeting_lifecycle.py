"""Meeting lifecycle routes — init, start, pause/resume, stop, list, outputs.

``POST …/{id}/init``          create the meeting folder + seed files (idempotent)
``POST …/{id}/start``         activate: seed outputs, spawn agent sessions
``POST …/{id}/status``        move between active / paused / reviewing
``POST …/{id}/stop``          flush agents, mark ended
``GET  …/meetings``           list every meeting with metadata on disk
``GET  …/{id}``               one meeting's metadata
``DELETE …/{id}``             permanently remove an inactive meeting
``GET  …/{id}/transcript``     finalized speech and typed broadcasts
``GET  …/{id}/outputs``       batch-read every agent output + tasks.json
``PUT  …/{id}/outputs``       save the user's edit of one agent's output (sidecar)
``DELETE …/{id}/outputs``     revert to what the agent itself last wrote
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
from kiro_crew.apps.builtins.meetings.backend.domain import translate
from kiro_crew.apps.builtins.meetings.backend.routes import tasks as task_routes
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    ACTIVE,
    DISPATCH_LOCK,
    START_LOCK,
    BadRequest,
    audit,
    data_root,
    field_str,
    field_str_list,
    hooks_of,
    json_body,
    query_int,
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


def _init_meeting(meeting_id: str, title: str, body: dict[str, Any], root: Any) -> dict[str, Any]:
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
    # `[]` means "no agents" and absent means "use the defaults", so fall back
    # on None rather than falsiness — `or` would collapse the two.
    body_agents = field_str_list(body, "agents_enabled")
    agents_enabled = body_agents if body_agents is not None else meta.get("agents_enabled")
    enabled = sess.get_enabled_agents(config, agents_enabled)
    store.ensure_agent_files(meeting_id, enabled, meta.get("title", "Meeting Notes"), root)
    return meta


#: Public name for the blocking init, so the calendar poller (which pre-creates a
#: meeting the same way the dashboard does) reuses this transaction rather than
#: keeping a second copy of the folder/metadata/tasks/outputs sequence.
init_meeting_blocking = _init_meeting


async def handle_meeting_init(request: web.Request) -> web.Response:
    """Create the meeting folder, metadata, tasks file, and agent outputs."""
    meeting_id = _meeting_id(request)
    body = await json_body(request, required=False)
    title = field_str(body, "title", default="Meeting", max_len=k.MAX_TITLE_LEN)

    # Initialization creates the same directory tree that deletion removes. Keep
    # the whole worker-thread transaction under the lifecycle lock so a meeting
    # cannot be recreated after a concurrent delete has returned 204.
    async with START_LOCK:
        meta = await asyncio.to_thread(_init_meeting, meeting_id, title, body, data_root(request))
    return web.json_response({"ok": True, "meeting_id": meeting_id, "meta": meta})


async def handle_get_meeting(request: web.Request) -> web.Response:
    """One meeting's metadata (the frontend's poll target)."""
    meeting_id = _meeting_id(request)
    meta = await asyncio.to_thread(store.read_meeting_meta, meeting_id, data_root(request))
    if meta is None:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )
    live = ACTIVE.get(meeting_id)
    live_payload = None
    if live is not None:
        live_payload = live.status()
        # Whether a dispatch sent NOW would be fanned out DIRECTLY, from the same
        # holder flag ``get_for_dispatch`` reads. The status is persisted ``active``
        # before ``init_agents`` runs, so status alone overstates readiness for the
        # whole initialization window (~tens of seconds). Plain attribute read on
        # the single-threaded loop, same as the ``ACTIVE.get`` above; the value is a
        # snapshot and may change by the next poll, which is exactly what a poll is
        # for.
        live_payload["accepting_dispatches"] = ACTIVE.accepting_dispatches
        # And whether it would be HELD rather than refused (issue #4610). The
        # frontend polls this endpoint to decide when to open the microphone, and
        # "would speech land?" is now these two ORed: during initialization the
        # answer is yes-by-holding. Reported separately rather than folded into the
        # flag above, because that one is also the gate ``get_for_dispatch`` reads —
        # making it true here would send lines to agents that are not ready.
        live_payload["buffering_dispatches"] = ACTIVE.buffering_dispatches
    return web.json_response(
        {
            "meta": meta,
            "live": live_payload,
        }
    )


async def handle_list_meetings(request: web.Request) -> web.Response:
    # `list_meetings` globs `*/session.json` under the meetings root and JSON-parses
    # every hit, so it grows with the user's meeting history — off the loop.
    meetings = await asyncio.to_thread(store.list_meetings, data_root(request))
    return web.json_response({"meetings": meetings})


def _collect_transcript(
    meeting_id: str, cursor: int, root: Any
) -> tuple[bool, list[dict[str, str]], int]:
    """Read meeting existence and its transcript off the event loop. BLOCKING."""
    exists = store.read_meeting_meta(meeting_id, root) is not None
    if not exists:
        return False, [], 0
    segments, next_cursor = store.read_transcript_page(meeting_id, cursor, root)
    return True, segments, next_cursor


async def handle_get_transcript(request: web.Request) -> web.Response:
    """Return finalized transcript segments from an optional byte cursor."""
    meeting_id = _meeting_id(request)
    cursor = query_int(
        request,
        "cursor",
        default=0,
        low=0,
        high=k.MAX_TRANSCRIPT_BYTES,
    )
    exists, segments, next_cursor = await asyncio.to_thread(
        _collect_transcript, meeting_id, cursor, data_root(request)
    )
    if not exists:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )
    return web.json_response({"segments": segments, "next_cursor": next_cursor})


def _delete_meeting(meeting_id: str, root: Any) -> bool:
    """Remove a meeting while excluding every task mutation. BLOCKING."""
    with task_routes.task_mutation_transaction():
        return store.delete_meeting(meeting_id, root)


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
                status=409,
            )
        # A filing spans a provider call and the local task update. Let it finish
        # before deleting so the provider cannot create an external item after its
        # source meeting has disappeared; a filing that starts later sees 404.
        async with task_routes.task_filing_transaction():
            deleted = await asyncio.to_thread(_delete_meeting, meeting_id, root)

    if not deleted:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"},
            status=404,
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
                {"error": "another meeting is already active", "code": "meeting_already_active"},
                status=409,
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
            # Threaded through for the translation worker's writes, which are the
            # only ones a live session makes on its own rather than via a handler.
            root=root,
        )
        session.muted_agents = set(muted)
        # Drain the OUTGOING session before this one replaces it. `set()` cancels the
        # previous session's queues, so starting a second meeting while an earlier
        # (typically expired) one still held a half-batch discarded that transcript —
        # the same loss as the teardown paths, reached by a different route. Awaiting is
        # possible here because this is an `async def`; the previous justification for
        # the non-awaiting `set()` did not survive checking.
        # Close the outgoing session's ingress before draining it. Dispatch uses a
        # separate short-lived lock, so later speech is rejected promptly while an
        # agent takes time to finish its queued turn.
        async with DISPATCH_LOCK:
            ACTIVE.suspend_dispatches(ACTIVE.get())
        outgoing = await ACTIVE.drain_and_clear()
        async with DISPATCH_LOCK:
            ACTIVE.set(session)
            # Agent initialization may await several model turns. Keep DIRECT
            # fan-out closed until every enabled agent knows its output contract —
            # but HOLD what is said meanwhile instead of refusing it.
            #
            # Refusing was measured at ~46s of a real meeting (issue #4610): the
            # speaker opens with the agenda, every line 409s, and the notes and
            # tasks begin partway through the first topic with nothing to show a
            # turn was lost. The hold is bounded and drains in arrival order right
            # after `init_agents` returns, below.
            ACTIVE.suspend_dispatches(session, buffer_speech=True)

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
        async with DISPATCH_LOCK:
            ACTIVE.resume_dispatches(session)
            # Drain under the SAME acquisition that reopened ingress. A live
            # dispatch needs this lock too, so nothing spoken after the reopen can
            # overtake speech that was held while it was shut — releasing between
            # the two would let the meeting's opening land after its second topic.
            buffered, dropped = session.drain_init_buffer()
        if buffered or dropped:
            logger.info(
                "meetings: %r delivered %d line(s) held during agent init, %d dropped",
                meeting_id,
                buffered,
                dropped,
            )
        if dropped:
            # Off the lock (this is disk IO) but still inside START_LOCK. Recorded
            # so the human transcript states the loss too: the agents were told by
            # `drain_init_buffer`, and a gap only one reader can see is the silent
            # truncation the bound exists to avoid.
            await asyncio.to_thread(
                store.append_transcript,
                meeting_id,
                k.SYSTEM_INIT_BUFFER_OVERFLOW.format(count=dropped, limit=k.MAX_INIT_BUFFER_LINES),
                k.TRANSCRIPT_SOURCE_SYSTEM,
                root,
            )

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

    # The admission lock first waits for an in-flight durable append/fan-out. It is
    # released before agent flushes, so a slow agent cannot hold every later
    # request in this endpoint's queue.
    async with START_LOCK:
        async with DISPATCH_LOCK:
            meta = await asyncio.to_thread(_apply_status, meeting_id, status, root)
            if meta is None:
                return web.json_response(
                    {"error": "meeting not found", "code": "meeting_not_found"},
                    status=404,
                )

            session = ACTIVE.get(meeting_id)
            if session is not None and status in (k.STATUS_REVIEWING, k.STATUS_ENDED):
                ACTIVE.suspend_dispatches(session)
            elif session is not None and status == k.STATUS_ACTIVE:
                ACTIVE.resume_dispatches(session)
        if session is not None and status in (
            k.STATUS_PAUSED,
            k.STATUS_REVIEWING,
            k.STATUS_ENDED,
        ):
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
        async with DISPATCH_LOCK:
            session = ACTIVE.get(meeting_id)
            ACTIVE.suspend_dispatches(session)
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


def _is_editable(agent_def: dict[str, Any]) -> bool:
    """Whether this agent's output is one the user may edit.

    See :data:`constants.EDITABLE_WIDGET_TYPE`. Used by the read overlay AND the
    write gate, so "editable" means the same thing in both directions.
    """
    return str(agent_def.get("widget_type") or k.DEFAULT_WIDGET_TYPE) == k.EDITABLE_WIDGET_TYPE


def _collect_outputs(
    meeting_id: str, root: Any
) -> tuple[dict[str, str], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Read every agent's EFFECTIVE output, the edit metadata, and the tasks. BLOCKING.

    Runs on a worker thread, never the event loop: the note-taker is prompted to
    rewrite its WHOLE file after each transcription batch, so these reads are
    unbounded, and `redact()` over a megabyte of notes measures in the tens of
    milliseconds. The dashboard polls this every few seconds for the length of a
    meeting, so doing it inline would stall every other task on the loop —
    including the liveness heartbeat — on a repeating timer.

    A user EDIT of an agent's output takes precedence over the generated text, so
    ``outputs`` is what the meeting actually shows and the client needs no merge
    step. ``edits`` carries only the ``stale`` metadata, because
    the content is already in ``outputs`` and sending it twice would double the
    poll for the app's largest field.

    **The generated half is redacted and the edited half is not**, which looks
    inconsistent and is not:

    * agent output is model-generated prose the user has never vetted, so it is
      scrubbed on every read (unchanged from before this feature);
    * an edit is owner-authored text, not untrusted model output. The editor can
      accept arbitrary pasted text, including text that merely resembles a
      credential, so it must be stored and returned byte-for-byte. Re-scrubbing
      it would silently modify the user's document.

    Tasks come from `tasks.json`, which an agent writes, so they go through the task
    module's own normalizer (which redacts every field and drops a malformed
    record) rather than being forwarded raw.
    """
    config = store.read_config(root)
    agents = config.get("meeting_agents") or []
    outputs = {
        agent_id: redact(content)
        for agent_id, content in store.read_agent_outputs(meeting_id, agents, root).items()
    }
    # Only EDITABLE agents are consulted, using the same predicate the write gate
    # does. Filtering here as well as there is what keeps a sidecar written while an
    # agent was markdown from being served after its widget_type is changed to html —
    # at which point the user's markdown would be handed to the iframe renderer.
    edits = store.read_agent_edits(meeting_id, [a for a in agents if _is_editable(a)], root)
    for agent_id, edit in edits.items():
        outputs[agent_id] = str(edit.pop("content", ""))
    return outputs, edits, task_routes.read_normalized(meeting_id, root)


async def handle_get_outputs(request: web.Request) -> web.Response:
    """Batch-read every configured agent's effective output plus the task list."""
    meeting_id = _meeting_id(request)
    root = data_root(request)
    outputs, edits, tasks = await asyncio.to_thread(_collect_outputs, meeting_id, root)
    return web.json_response({"outputs": outputs, "edits": edits, "tasks": tasks})


def _editable_agent(agent_id: str, root: Any) -> dict[str, Any]:
    """The agent definition *agent_id* names, if its output can be edited. BLOCKING.

    Raises rather than returning None, because the two failures are distinct answers
    the dashboard acts on differently: an unknown agent is a 404, and an agent whose
    output is not prose is a 409 (see :data:`constants.EDITABLE_WIDGET_TYPE`).
    """
    config = store.read_config(root)
    agent_def = next(
        (a for a in (config.get("meeting_agents") or []) if a.get("id") == agent_id), None
    )
    if agent_def is None:
        raise BadRequest("unknown agent", status=404, code="agent_not_found")
    if not _is_editable(agent_def):
        raise BadRequest(
            "only a markdown agent's output can be edited",
            status=409,
            code="agent_output_not_editable",
        )
    return agent_def


def _save_edit(meeting_id: str, agent_id: str, content: str, root: Any) -> None:
    """Validate the agent, then persist the edit. BLOCKING.

    The meeting existence check and the write share the metadata transaction with
    deletion. Without it, a PUT for an unknown meeting created an orphan ``edits/``
    directory, and a concurrent delete could remove ``session.json`` just before the
    write recreated that same orphan. The agent definition is one authorization
    snapshot; the read overlay re-applies the editable predicate on every poll.
    """
    with store.meta_transaction():
        if store.read_meeting_meta(meeting_id, root) is None:
            raise BadRequest("meeting not found", status=404, code="meeting_not_found")
        store.write_agent_edit(meeting_id, _editable_agent(agent_id, root), content, root)


def _drop_edit(meeting_id: str, agent_id: str, root: Any) -> bool:
    """Validate the meeting and agent, then delete its edit sidecar. BLOCKING."""
    with store.meta_transaction():
        if store.read_meeting_meta(meeting_id, root) is None:
            raise BadRequest("meeting not found", status=404, code="meeting_not_found")
        return store.revert_agent_edit(meeting_id, _editable_agent(agent_id, root), root)


async def handle_put_output(request: web.Request) -> web.Response:
    """Save the user's edit of one agent's output — the editable minutes.

    The edit lands in a SIDECAR, never in the agent's file (see the block comment
    above ``store.agent_edits_dir``). So the agent keeps writing its own document
    throughout, the next outputs poll's ``stale`` flag is how the user learns it
    has, and ``DELETE`` restores the generated text by deleting one file.

    Not redacted (the asymmetry ``_collect_outputs`` spells out), and validated by
    hand rather than with ``field_str``, for two reasons: ``field_str`` treats a
    non-string as MISSING (so a malformed body would answer 200 having replaced the
    minutes with ``""``) and it ``strip()``s, which would eat the trailing newline
    of every markdown document it touched.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    # A whole document, not a short field — hence the raised cap. See
    # `constants.MAX_MINUTES_BODY_BYTES` for why the default 256 KiB is wrong here.
    body = await json_body(request, max_bytes=k.MAX_MINUTES_BODY_BYTES)
    agent_id = store.safe_agent_id(field_str(body, "agent_id", required=True, max_len=64))
    content = body.get("content")
    if not isinstance(content, str):
        raise BadRequest("content must be a string")
    if len(content) > k.MAX_MINUTES_CHARS:
        raise BadRequest(f"content must be at most {k.MAX_MINUTES_CHARS} characters", status=413)
    # JSON's ``\udXXX`` escapes can decode into UNPAIRED surrogates, which are
    # valid Python str but not encodable UTF-8 — the sidecar write would then
    # raise mid-request and the user would see a 500 for a malformed input.
    # Reject it here as the client error it is.
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise BadRequest(
            "content contains unpaired surrogate characters", code="content_not_unicode"
        )

    await asyncio.to_thread(_save_edit, meeting_id, agent_id, content, root)
    audit("meetings.edit_output", f"{meeting_id} agent:{agent_id}", outcome="ok")
    return web.json_response({"ok": True, "agent_id": agent_id})


async def handle_delete_output(request: web.Request) -> web.Response:
    """Revert one agent's output to what the agent itself last wrote.

    ``reverted: false`` for an agent with no edit is a success, not a 404: the
    request asked for "no edit on this agent" and that is the state afterwards.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    body = await json_body(request)
    agent_id = store.safe_agent_id(field_str(body, "agent_id", required=True, max_len=64))

    reverted = await asyncio.to_thread(_drop_edit, meeting_id, agent_id, root)
    audit("meetings.revert_output", f"{meeting_id} agent:{agent_id}", outcome="ok")
    return web.json_response({"ok": True, "agent_id": agent_id, "reverted": reverted})


def _read_translations_since(meeting_id: str, since: int, root: Any) -> dict[str, Any]:
    """Translated lines with ``n >= since``, plus the cursor to ask for next. BLOCKING."""
    doc = store.read_translations(meeting_id, root)
    lines = [
        line
        for line in doc.get("lines", [])
        if isinstance(line, dict) and int(line.get("n", -1)) >= since
    ]
    language = str(doc.get("language", "") or "")
    return {
        "language": language,
        # Resolved here rather than in the frontend: the accepted languages and
        # their endonyms are published by the backend (see GET /config), so a
        # second copy in the client would be the thing that drifts.
        "language_label": translate.language_label(language) if language else "",
        "lines": lines,
        "next_n": int(doc.get("next_n", 0)),
    }


async def handle_get_translations(request: web.Request) -> web.Response:
    """Live-translation lines for a meeting, newer than a client cursor.

    A cursor rather than the whole document: a long meeting accumulates hundreds
    of lines and the panel polls while it is open, so resending everything each
    time would grow linearly for no benefit. ``next_n`` is what the client sends
    back as ``since``.

    Separate from ``…/outputs`` on purpose. Outputs is polled for every meeting;
    this is polled only while the panel is open, and translation is off by default.
    """
    meeting_id = _meeting_id(request)
    root = data_root(request)
    since = query_int(request, "since", default=0, low=0, high=10_000_000)
    payload = await asyncio.to_thread(_read_translations_since, meeting_id, since, root)
    live = ACTIVE.get(meeting_id)
    queue = live.translations if live is not None else None
    payload["pending"] = queue.pending if queue is not None else 0
    payload["dropped"] = queue.dropped if queue is not None else 0
    return web.json_response(payload)


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
    attachments = await asyncio.to_thread(_apply_attachments, meeting_id, body, data_root(request))
    if attachments is None:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )
    return web.json_response({"ok": True, "attachments": attachments})
