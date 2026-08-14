"""Agent routes — list, toggle, mute, dispatch text, reset the circuit breaker.

``GET  …/agents``             configured meeting agents
``POST …/{id}/agents``        enable/disable an agent mid-meeting
``POST …/{id}/mute``          mute/unmute an agent (persisted in session.json)
``POST …/{id}/dispatch``      broadcast a transcription or typed line to agents
``POST …/{id}/reset``         reset failed agents' circuit breakers
``GET  …/status``             the live meeting's dispatcher status
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import session as sess
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    ACTIVE,
    DISPATCH_LOCK,
    START_LOCK,
    BadRequest,
    audit,
    data_root,
    field_bool,
    field_str,
    json_body,
)
from kiro_crew.security import redact

logger = logging.getLogger("kirocrew.app.meetings")


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


async def handle_get_agents(request: web.Request) -> web.Response:
    """The configured meeting agents (plus the always-on task extractor)."""
    config = await asyncio.to_thread(store.read_config, data_root(request))
    return web.json_response(
        {
            "agents": config.get("meeting_agents") or [],
            "task_extractor_id": k.TASK_EXTRACTOR_ID,
        }
    )


async def handle_status(request: web.Request) -> web.Response:
    """The live dispatcher status, or an all-idle shape when nothing is running."""
    session = ACTIVE.get()
    if session is None:
        return web.json_response(
            {
                "active_meeting": None,
                "muted_agents": [],
                "agents": {},
                "agents_paused": False,
                "expired": False,
            }
        )
    return web.json_response(session.status())


def _read_toggle_state(
    meeting_id: str, agent_id: str, enable: bool, root: Any
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None, str]:
    """Read everything a toggle needs and seed the agent's output file. BLOCKING.

    Returns ``(config, agent_def, meta, output_filename, meeting_dir)``; a ``None``
    ``agent_def`` or ``meta`` is the caller's 404, in that order (the order the
    handler checked them inline).

    Runs on a worker thread, never the event loop: a config read, a metadata read,
    an ``ensure_agent_files`` pass that creates and seeds a file, and a
    ``meeting_dir`` containment check that resolves the path on disk.

    Grouped into ONE hop so the agent definition, the metadata and the seeded file
    are a single consistent snapshot — and because the alternative is four separate
    hops in a handler that already has to await an agent dispatch afterwards.
    """
    config = store.read_config(root)
    agent_def = next(
        (a for a in (config.get("meeting_agents") or []) if a.get("id") == agent_id), None
    )
    if agent_def is None:
        return config, None, None, None, ""
    meta = store.read_meeting_meta(meeting_id, root)
    if meta is None:
        return config, agent_def, None, None, ""
    if not enable:
        return config, agent_def, meta, None, ""
    store.ensure_agent_files(meeting_id, [agent_def], meta.get("title", "Meeting Notes"), root)
    return (
        config,
        agent_def,
        meta,
        store.agent_output_filename(agent_def),
        str(store.meeting_dir(meeting_id, root)),
    )


async def handle_toggle_agent(request: web.Request) -> web.Response:
    """Enable or disable one agent mid-meeting."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    root = data_root(request)
    agent_id = store.safe_agent_id(field_str(body, "agent_id", required=True, max_len=64))
    enable = field_bool(body, "enable", default=True)

    # Enabling an agent creates its output file before committing the metadata.
    # Keep that complete transaction ordered with meeting deletion so the file
    # cannot recreate a directory after DELETE has returned 204.
    async with START_LOCK:
        # Agent initialization is slow but must finish before transcript can enter
        # the new queue; otherwise the first line can arrive before the agent knows
        # which output file it owns.
        async with DISPATCH_LOCK:
            return await _toggle_agent_locked(meeting_id, agent_id, enable, root)


async def _toggle_agent_locked(
    meeting_id: str, agent_id: str, enable: bool, root: Any
) -> web.Response:
    """Apply one agent toggle while the caller holds ``START_LOCK``."""
    config, agent_def, meta, fname, mdir = await asyncio.to_thread(
        _read_toggle_state, meeting_id, agent_id, enable, root
    )
    if agent_def is None:
        return web.json_response({"error": "unknown agent", "code": "agent_not_found"}, status=404)
    if meta is None:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )

    # An ABSENT `agents_enabled` means "the configured defaults", not "none" — the
    # two are different values to `get_enabled_agents`, which treats `None` as
    # defaults and `[]` as an explicit empty roster. Starting from `[]` here made
    # the first toggle on a fresh meeting destroy the roster: disabling ONE default
    # agent persisted `[]`, so the meeting then started with no agents at all and
    # silently produced no notes and no diagrams.
    #
    # An explicit `[]` already on the meeting is preserved as-is (the `is None`
    # check, not a truthiness test) — the user having turned everything off is a
    # real state, and re-seeding it from the defaults would fight them.
    stored = meta.get("agents_enabled")
    if stored is None:
        enabled_list = [str(a["id"]) for a in sess.get_enabled_agents(config) if a.get("id")]
    else:
        enabled_list = [str(x) for x in stored]
    session = ACTIVE.get(meeting_id)

    if enable:
        if agent_id not in enabled_list:
            enabled_list.append(agent_id)
        if fname:
            outputs = dict(meta.get("outputs") or {})
            outputs[agent_id] = fname
            meta["outputs"] = outputs
        if session is not None:
            session.add_agent(agent_id, agent_def.get("agent") or "")
            if fname:
                message = (
                    sess.build_init_message(
                        agent_def,
                        meta,
                        f"{mdir}/{fname}",
                        sess.build_cross_reference(
                            mdir, sess.get_enabled_agents(config, enabled_list)
                        ),
                    )
                    + "\n\nYou are joining mid-meeting. Wait for transcription."
                )
                await sess._safe_dispatch(session, agent_id, message, agent_def.get("agent") or "")
    else:
        enabled_list = [x for x in enabled_list if x != agent_id]
        if session is not None:
            # Keep the queue (its pending lines still belong in the output) but
            # stop feeding it — that is what "muted" means here.
            session.muted_agents.add(agent_id)

    # A second hop rather than one grouped read-modify-write: the agent dispatch
    # above is an `await` the write must follow (the agent has to see its file
    # before it is told about it), so read and write cannot share a thread.
    #
    # So this RE-READS under the metadata lock and applies only the two fields this
    # request owns, instead of writing the `meta` snapshot captured before the
    # await. Writing the snapshot would roll back anything another request changed
    # meanwhile — the same lost-update `handle_file_task` avoids the same way.
    enabled_list = await asyncio.to_thread(
        _commit_toggle, meeting_id, agent_id, enable, fname or "", enabled_list, root
    )
    return web.json_response(
        {"ok": True, "agent_id": agent_id, "enabled": enable, "agents_enabled": enabled_list}
    )


def _commit_toggle(
    meeting_id: str,
    agent_id: str,
    enable: bool,
    fname: str,
    default_ids: list[str],
    root: Any,
) -> list[str]:
    """Apply ONE agent's toggle against a fresh read. BLOCKING.

    Returns the resulting roster.

    Takes the OPERATION (which agent, on or off) rather than a pre-computed list.
    An earlier version re-read the metadata but then wrote the list its caller had
    derived before the dispatch await — so two rapid toggles both computed from the
    same stale roster and the later commit silently discarded the earlier one. The
    re-read only helped for OTHER fields; the field being changed still lost.

    Recomputing inside :func:`store.meta_transaction` is what makes concurrent
    toggles of different agents both survive. *default_ids* seeds an absent
    ``agents_enabled`` with the configured defaults, since absent means "the
    defaults" and not "none" — an explicit ``[]`` is preserved.
    """
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return []
        stored = meta.get("agents_enabled")
        roster = [str(x) for x in stored] if stored is not None else list(default_ids)
        if enable:
            if agent_id not in roster:
                roster.append(agent_id)
            if fname:
                outputs = dict(meta.get("outputs") or {})
                outputs[agent_id] = fname
                meta["outputs"] = outputs
        else:
            roster = [x for x in roster if x != agent_id]
        meta["agents_enabled"] = roster
        store.write_meeting_meta(meeting_id, meta, root)
        return roster


def _apply_mute(meeting_id: str, agent_id: str, muted: bool, root: Any) -> dict[str, Any] | None:
    """Persist one agent's mute state, or return None when the meeting is gone. BLOCKING.

    Runs on a worker thread, never the event loop: a metadata read plus an atomic
    metadata write.

    Grouped so the read-modify-write of ``muted_agents`` is ONE hop — the written
    set is derived from the set just read, so splitting them would let two
    concurrent mute toggles each discard the other.
    """
    # One critical section: the new set is derived from the list just read, so a
    # concurrent mute would otherwise be dropped by whichever write landed last
    # (see `store.meta_transaction`).
    with store.meta_transaction():
        meta = store.read_meeting_meta(meeting_id, root)
        if meta is None:
            return None
        muted_set = {str(x) for x in (meta.get("muted_agents") or [])}
        if muted:
            muted_set.add(agent_id)
        else:
            muted_set.discard(agent_id)
        meta["muted_agents"] = sorted(muted_set)
        store.write_meeting_meta(meeting_id, meta, root)
    return meta


async def handle_mute_agent(request: web.Request) -> web.Response:
    """Mute or unmute an agent. Persisted so it survives a reload."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    root = data_root(request)
    agent_id = store.safe_agent_id(field_str(body, "agent_id", required=True, max_len=64))
    muted = field_bool(body, "muted", default=True)

    meta = await asyncio.to_thread(_apply_mute, meeting_id, agent_id, muted, root)
    if meta is None:
        return web.json_response(
            {"error": "meeting not found", "code": "meeting_not_found"}, status=404
        )

    session = ACTIVE.get(meeting_id)
    if session is not None:
        if muted:
            session.muted_agents.add(agent_id)
        else:
            session.muted_agents.discard(agent_id)

    return web.json_response({"ok": True, "muted_agents": meta["muted_agents"]})


async def handle_dispatch_text(request: web.Request) -> web.Response:
    """Broadcast one line to every unmuted agent.

    Two producers: the browser's speech-to-text stream (final segments) and the
    user typing into the broadcast bar. Both arrive as untrusted text, so the
    line is redacted before it enters an agent's context — the transcript is
    also what the dashboard echoes back, and redacting once at the entry point
    keeps every downstream copy clean.
    """
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    text = field_str(body, "text", required=True, max_len=k.MAX_TRANSCRIPT_CHARS)
    is_chat = field_bool(body, "chat", default=False)

    # The transcript append creates its parent directory when needed. Keep the
    # live-session check, append, and fan-out in the lifecycle transaction so a
    # concurrent stop followed by deletion cannot remove the meeting while this
    # request is awaiting disk IO and then have the append recreate an orphan.
    expired_session: sess.MeetingSession | None = None
    async with DISPATCH_LOCK:
        session = ACTIVE.get_for_dispatch(meeting_id)
        if session is None:
            return web.json_response(
                {"error": "no active meeting", "code": "no_active_meeting"}, status=409
            )
        if session.expired:
            # Close admission before the slow drain. A later request can then fail
            # promptly instead of waiting behind agent IO, while the lifecycle lock
            # below still serializes the actual teardown with start/stop/delete.
            ACTIVE.suspend_dispatches(session)
            expired_session = session
        else:
            transcript_text = redact(text)
            source = k.TRANSCRIPT_SOURCE_TYPED if is_chat else k.TRANSCRIPT_SOURCE_SPEECH
            segment = await asyncio.to_thread(
                store.append_transcript,
                meeting_id,
                transcript_text,
                source,
                data_root(request),
            )
            if segment is None:
                raise BadRequest(
                    "meeting transcript is too large",
                    status=413,
                    code="transcript_too_large",
                )

            line = f"{k.CHAT_PREFIX} {transcript_text}" if is_chat else transcript_text
            accepted = session.broadcast(line)

    if expired_session is not None:
        async with START_LOCK:
            if ACTIVE.get(meeting_id) is expired_session:
                # Drain, not cancel: a long meeting whose next line arrives after the
                # session lapsed still has whatever was queued when it went quiet.
                await ACTIVE.drain_and_clear()
                await asyncio.to_thread(sess.end_meeting_meta, meeting_id, data_root(request))
        return web.json_response(
            {
                "error": "meeting session expired",
                "code": "meeting_session_expired",
            },
            status=410,
        )
    return web.json_response({"ok": True, "dispatched": accepted, "text": line, "segment": segment})


async def handle_reset_agents(request: web.Request) -> web.Response:
    """Reset failed agents' circuit breakers and retry their queues."""
    meeting_id = _meeting_id(request)
    session = ACTIVE.get(meeting_id)
    if session is None:
        return web.json_response(
            {"error": "no active meeting", "code": "no_active_meeting"}, status=409
        )
    resumed = session.resume_all()
    audit("meetings.reset_agents", meeting_id, outcome="ok")
    return web.json_response(
        {
            "ok": True,
            "resumed": resumed,
            "queued": {name: len(q.queue) for name, q in session.agents.items()},
        }
    )


async def handle_agent_message(request: web.Request) -> web.Response:
    """Send a direct message to one agent (the per-panel chat input)."""
    meeting_id = _meeting_id(request)
    body = await json_body(request)
    agent_id = store.safe_agent_id(field_str(body, "agent_id", required=True, max_len=64))
    text = field_str(body, "text", required=True, max_len=k.MAX_TRANSCRIPT_CHARS)

    session = ACTIVE.get(meeting_id)
    if session is None:
        return web.json_response(
            {"error": "no active meeting", "code": "no_active_meeting"}, status=409
        )
    queue = session.agents.get(agent_id)
    if queue is None:
        raise BadRequest("agent is not part of this meeting", status=404)
    queue.enqueue(f"{k.CHAT_PREFIX} {redact(text)}")
    await queue.flush_now()
    return web.json_response({"ok": True, "agent_id": agent_id})
