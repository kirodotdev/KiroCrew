"""Continue a terminal kiro-cli session as a Kiro Crew chat: ``POST /api/kiro-cli/sessions/{sid}/continue``.

The History pane's Kiro CLI tab lists the sessions a person ran in a terminal
(:mod:`kiro_crew.kiro_cli_sessions`). Continuing one COPIES it: the session's
``<sid>.json`` + ``<sid>.jsonl`` are snapshotted, carried as a version-2 transfer
bundle's Layer B, and installed by the same code a session sent from another
instance lands through (:func:`~kiro_crew.dashboard.session_transfer.install_validated_bundle`).
That installer writes the context under a FRESH sid and joins it to a NEW slot,
so:

* the terminal's files are never written, moved or deleted, and
  ``kiro-cli chat --resume-id <sid>`` still resumes the original;
* continuing the same session twice makes two independent chats, each with its
  own copy, exactly as a repeated transfer does. Nothing is refused for having
  been continued before, because nothing about the original changed.

The new chat's project is the session's working directory when that directory
still exists and passes the same checks the project picker applies (not a
sensitive path, not overlapping the Crew data home); otherwise the chat opens
without one and the answer says why, rather than failing the copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from aiohttp import web

from kiro_crew import kiro_cli_sessions
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
from kiro_crew.dashboard.session_transfer import (
    BUNDLE_VERSION,
    _memory_admission,
    _MemoryWaitTimedOut,
    _NeverFits,
    _resolve_layer_b_sid,
    _validate_bundle,
    install_validated_bundle,
)
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, DashboardState
from kiro_crew.sandbox import voice_runtime_workspace_conflict
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.kiro_cli_continue")

#: The ``origin`` the bundle carries: the installer titles the chat
#: "<title> (from kiro-cli)" and files it under Imported / from kiro-cli.
ORIGIN = "kiro-cli"

#: HTTP status for each refusal :func:`kiro_cli_sessions.snapshot_session` raises.
_SNAPSHOT_STATUS = {
    "kiro_cli_session_invalid_id": 404,
    "kiro_cli_session_not_found": 404,
    "kiro_cli_session_no_transcript": 422,
    "kiro_cli_session_unreadable": 422,
    "kiro_cli_session_empty": 422,
    "kiro_cli_session_busy": 409,
}


def _error(message: str, code: str, status: int) -> web.Response:
    return web.json_response({"error": message, "code": code}, status=status)


def _owned_sids(state: DashboardState) -> frozenset[str]:
    """Kiro-cli ids the session map joins to Crew sessions. **Blocking** (map lock)."""
    from kiro_crew.dashboard.handlers.sessions_parts.history_reads import (
        _crew_owned_kiro_cli_sids,
    )

    return _crew_owned_kiro_cli_sids(state)


def _eligible_meta(state: DashboardState, sid: str) -> kiro_cli_sessions.CliSessionMeta | None:
    """*sid*'s envelope when the History list would show it, else ``None``. **Blocking.**"""
    paths = kiro_cli_sessions.session_paths(sid)
    meta = kiro_cli_sessions.read_envelope_meta(paths[0]) if paths else None
    if meta is None or meta.session_id != sid:
        return None
    if not kiro_cli_sessions.is_terminal_session(
        meta,
        owned_sids=_owned_sids(state),
        crew_roots=kiro_cli_sessions.crew_working_roots(),
    ):
        return None
    return meta


def _pair_bytes(sid: str) -> int:
    """The pair's size on disk, for the memory admission. **Blocking.**"""
    paths = kiro_cli_sessions.session_paths(sid)
    total = 0
    for path in paths or ():
        try:
            total += path.lstat().st_size
        except OSError:
            continue
    return total


def project_for_cwd(cwd: str) -> tuple[str, str]:
    """``(project, "")`` when *cwd* can be the chat's project, else ``("", reason)``.

    **Blocking**: realpath and the voice-runtime overlap check touch the disk.
    The same checks ``POST /api/chat/slots/{slot}/project`` applies to a folder
    the person picks: an absolute existing directory, not a sensitive path, and
    not one that contains or sits inside the Crew data home.
    """
    if not cwd or not os.path.isabs(cwd):
        return "", "the session has no working folder"
    resolved = os.path.realpath(cwd)
    if not os.path.isdir(resolved):
        return "", "the session's folder no longer exists"
    if is_sensitive_path(resolved):
        return "", "the session's folder is a protected location"
    conflict = voice_runtime_workspace_conflict(resolved)
    if conflict is not None:
        return "", conflict
    return resolved, ""


async def api_kiro_cli_session_continue(request: web.Request) -> web.Response:
    """POST /api/kiro-cli/sessions/{sid}/continue — copy a terminal session into a new chat.

    Owner-only, like the History reads beside it. ``sid`` must be a kiro-cli
    session id the History list would show; anything else answers 404. Answers
    the installer's own body (``key``, ``title``, ``resume_mode`` ...) plus
    ``project`` and, when the session's folder could not be used,
    ``project_skipped`` with the reason.
    """
    refusal = await require_owner_dashboard_request(request, "kiro_cli_session_continue")
    if refusal is not None:
        return refusal
    state: DashboardState = request.app["state"]
    sid = request.match_info.get("sid", "")
    if not kiro_cli_sessions.valid_session_id(sid):
        return _error("kiro-cli session not found", "kiro_cli_session_not_found", 404)
    if state.live_slot_count() >= MAX_LIVE_SLOTS:
        return _error(f"slot cap reached ({MAX_LIVE_SLOTS})", "transfer_slot_cap", 429)
    meta = await asyncio.to_thread(_eligible_meta, state, sid)
    if meta is None:
        return _error("kiro-cli session not found", "kiro_cli_session_not_found", 404)

    doc_bytes = await asyncio.to_thread(_pair_bytes, sid)
    try:
        # The pair is read whole (the installer takes the event log as text),
        # so a large one waits for memory exactly as a large arriving bundle does.
        async with _memory_admission(doc_bytes):
            try:
                snapshot = await asyncio.to_thread(kiro_cli_sessions.snapshot_session, sid)
            except kiro_cli_sessions.SnapshotRefused as refused:
                return _error(str(refused), refused.code, _SNAPSHOT_STATUS.get(refused.code, 422))
            state_doc = snapshot.envelope.get("session_state") or {}
            agent = state_doc.get("agent_name") if isinstance(state_doc, dict) else ""
            bundle, err = await asyncio.to_thread(
                _validate_bundle,
                {
                    "bundle_version": BUNDLE_VERSION,
                    "origin": ORIGIN,
                    "title": snapshot.meta.title,
                    "agent": agent if isinstance(agent, str) else "",
                    "messages": snapshot.messages,
                    "layer_b": {"envelope": snapshot.envelope, "events": snapshot.events},
                },
            )
            del snapshot
            if err is not None:
                return err
            project, skipped = await asyncio.to_thread(project_for_cwd, meta.cwd)
            response = await install_validated_bundle(request, bundle, project=project)
    except _NeverFits:
        return _error(
            "this session is too large to copy on this machine; resume it in the terminal",
            "kiro_cli_session_too_large",
            413,
        )
    except _MemoryWaitTimedOut:
        return _error(
            "this machine is short of memory right now; please retry",
            "transfer_expansion_busy",
            429,
        )
    sel().log_api_access(
        caller="dashboard",
        operation="kiro_cli.session_continue",
        outcome="allowed" if response.status == 200 else "error",
        source="dashboard",
        resources=f"sid={sid},status={response.status},project={'yes' if project else 'no'}",
    )
    if response.status != 200 or not isinstance(response, web.Response) or response.body is None:
        return response
    body = json.loads(response.body)  # type: ignore[arg-type]
    # The copy's own sid, read from the join the installer just made (on the
    # loop: the lookup self-prunes the map), so the list can keep recognising the
    # copy as Crew's after a reset drops that join.
    slot = state._slots.get(body.get("key", ""))
    copy_sid = (
        _resolve_layer_b_sid(getattr(state, "sessions", None), effective_session_key(slot))
        if slot
        else ""
    )
    if copy_sid:
        await asyncio.to_thread(kiro_cli_sessions.record_continued, copy_sid)
    body["project"] = project
    if skipped:
        body["project_skipped"] = skipped
    return web.json_response(body)
