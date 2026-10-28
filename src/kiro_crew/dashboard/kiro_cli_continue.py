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
passes the checks a folder's project directory must (``chat_folders_paths``:
absolute, existing, not a sensitive path, not overlapping the Crew data home);
otherwise the chat opens
without one and the answer says why, rather than failing the copy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import kiro_cli_sessions
from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, kiro_cli_offers_kas
from kiro_crew.agent_sdk.tool_search import resume_takes_tool_search_replay
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat_folders_paths import (
    _folder_project_overlap_denied,
    _validate_project_dir,
)
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
from kiro_crew.dashboard.session_transfer_intake import _measure_stream
from kiro_crew.dashboard.state import MAX_LIVE_SLOTS, DashboardState
from kiro_crew.jsonl_util import open_regular_nofollow
from kiro_crew.sel import sel

logger = logging.getLogger("kiro_crew.dashboard.kiro_cli_continue")

#: The ``origin`` the bundle carries: the installer titles the chat
#: "<title> (from kiro-cli)" and files it in a top-level "from kiro-cli" folder.
#: Not under Imported: the session was copied from this machine's own store, it
#: did not arrive from somewhere else.
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


#: Bytes read at a time while counting the event log's records.
_COUNT_CHUNK_BYTES = 1024 * 1024


def _measure_pair(sid: str) -> tuple[int, int, int, int]:
    """What copying *sid*'s pair costs in memory, as ``(size, records, values,
    factor)`` for :func:`_memory_admission`. **Blocking IO, thread-safe.**

    Each file is measured the way an arriving bundle is
    (:func:`_measure_stream`: its size, the JSON values it can hold and the
    width its text decodes to), a chunk at a time, never read whole. A bundle's
    parse holds one decoded document; the snapshot holds more at once: the raw
    bytes (1x), the decoded event log (at most the parse factor) and the record
    being parsed beside them (the parse factor again, for one record as large as
    the whole log). So the factor admitted is ``2 * factor + 1``. Records stand
    in for messages: each visible message is one record, so the per-message
    allowance is never short. A file that is not a regular one (a FIFO, a link
    to ``/dev/zero``) is never read: every file is opened through
    :func:`kiro_crew.jsonl_util.open_regular_nofollow`, and one it refuses is
    skipped here and refused by the snapshot.
    """
    size = records = values = 0
    factor = 1
    paths = kiro_cli_sessions.session_paths(sid)
    for path in paths or ():
        try:
            with open_regular_nofollow(path, max_bytes=sys.maxsize) as f:
                file_size, _markers, file_values, file_factor = _measure_stream(
                    f, os.fstat(f.fileno()).st_size
                )
        except OSError:
            continue
        size += file_size
        values += file_values
        factor = max(factor, file_factor)
    if paths is not None:
        records = _count_records(paths[1])
    return size, records, values, 2 * factor + 1


def _count_records(path: Path) -> int:
    """Newlines in the event log at *path*, a chunk at a time. **Blocking IO.**

    Opened through :func:`kiro_crew.jsonl_util.open_regular_nofollow`, so a FIFO or a link
    to ``/dev/zero`` planted under the sid answers 0 instead of hanging, and
    read no further than the size it had when opened, so a writer appending
    meanwhile cannot keep the count running."""
    count = 0
    try:
        with open_regular_nofollow(path, max_bytes=sys.maxsize) as f:
            left = os.fstat(f.fileno()).st_size
            while left > 0 and (chunk := f.read(min(_COUNT_CHUNK_BYTES, left))):
                left -= len(chunk)
                count += chunk.count(b"\n")
    except OSError:
        return 0
    return count + 1


#: The reason codes Continue gives for a transcript-only copy. Fixed and
#: allowlisted: the configured values are never echoed back, so a hand-edited
#: ``agent.acp_backend`` reaches no response, error or log line.
#:
#: The chat runs on a backend other than kiro-cli.
CONTEXT_NEEDS_KIRO_CLI = "kiro_cli_backend_required"
#: The chat runs on kiro-cli, but with Tool Search on its first turn starts a
#: fresh session and replays the transcript instead of loading the copy.
CONTEXT_TOOL_SEARCH_REPLAYS = "tool_search_replays_transcript"
#: This host's kiro-cli serves its v3 engine, so a Kiro CLI choice runs as KAS
#: (the a-kiro-cli-choice-runs-on-kas entry in docs/decisions) and no setting
#: gives a chat the v2 harness that alone can load the copy.
CONTEXT_UNAVAILABLE_HERE = "kiro_cli_state_unavailable"

#: The shape of the key every Continue chat runs under: a direct dashboard chat
#: with no channel identity. Only its namespace is read, to ask the provider's
#: own resume rule the question it will answer on the chat's first turn.
_CONTINUE_CHAT_KEY = "dashboard:"


def transcript_only_reason() -> str:
    """Why a new chat could not load the copied kiro-cli session, or ``""`` when
    it can. **Blocking** (reads the config).

    Two conditions both have to hold for the chat's first turn to
    ``session/load`` the copy:

    * it runs on the kiro-cli backend. A new chat runs on ``agent.acp_backend``,
      already resolved to a runtime id when the config loads. Any other backend
      (KAS, the default, among them) labels its turns with its own provider, so
      the session-map join the installer makes would be dropped as a provider
      switch on the first turn;
    * the provider would not replace that load with a fresh session plus a
      transcript replay. It does so for every direct dashboard chat on
      kiro-cli while ``agent.tool_search`` is on (the default), because a loaded
      session comes back without Tool Search's activated tool schemas. The
      question goes to that rule itself,
      :func:`~kiro_crew.agent_sdk.tool_search.resume_takes_tool_search_replay`,
      so this answer cannot drift from the one the first turn acts on.

    Either way the chat would quietly replay the transcript while Continue
    reported full context. Unreadable config answers
    :data:`CONTEXT_NEEDS_KIRO_CLI`: transcript-only is the outcome that is never
    wrong.

    The reason says what, if anything, would change it. On KAS while this host's
    kiro-cli serves its v3 engine, a stored Kiro CLI choice itself loads as KAS
    (``config.loader_builders._read_backend_choice``), so no setting reaches the
    v2 harness: :data:`CONTEXT_UNAVAILABLE_HERE`, whose text names no setting.
    """
    try:
        agent_cfg = KiroCrewConfig.load().agent
        backend = agent_cfg.acp_backend or ACP_BACKEND_KIRO
        tool_search = agent_cfg.tool_search
    except Exception:
        logger.warning("kiro-cli continue: config unreadable; copying transcript-only")
        return CONTEXT_NEEDS_KIRO_CLI
    if backend == ACP_BACKEND_KIRO:
        if resume_takes_tool_search_replay(
            tool_search=tool_search,
            backend=backend,
            channel_id=None,
            session_key=_CONTINUE_CHAT_KEY,
        ):
            return CONTEXT_TOOL_SEARCH_REPLAYS
        return ""
    if backend == ACP_BACKEND_KAS and kiro_cli_offers_kas():
        return CONTEXT_UNAVAILABLE_HERE
    return CONTEXT_NEEDS_KIRO_CLI


def _project_for_cwd(cwd: str) -> tuple[str, str]:
    """``(project, "")`` for a usable *cwd*, else ``("", reason)``. **Blocking.**

    The folder-project validator pair (``_validate_project_dir`` then
    ``_folder_project_overlap_denied``), which also audits a refused sensitive
    path, so this route accepts exactly the folders a folder's project may name.
    A session with no folder has nothing to refuse and opens without a project.
    """
    resolved, error = _validate_project_dir(cwd)
    if error is not None:
        return "", error
    if not resolved:
        return "", ""
    conflict = _folder_project_overlap_denied(resolved)
    if conflict is not None:
        return "", conflict
    return resolved, ""


async def api_kiro_cli_session_continue(request: web.Request) -> web.Response:
    """POST /api/kiro-cli/sessions/{sid}/continue — copy a terminal session into a new chat.

    Owner-only, like the History reads beside it. ``sid`` must be a kiro-cli
    session id the History list would show; anything else answers 404. Answers
    the installer's own body (``key``, ``title``, ``resume_mode`` ...) plus
    ``project`` and, when the session's folder could not be used,
    ``project_skipped`` with the reason. When the new chat could not load the
    copied session (see :func:`transcript_only_reason`) the copy is
    transcript-only: ``resume_mode`` is ``transcript`` and ``context_skipped``
    names why, as :data:`CONTEXT_NEEDS_KIRO_CLI`,
    :data:`CONTEXT_TOOL_SEARCH_REPLAYS` or :data:`CONTEXT_UNAVAILABLE_HERE`.
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

    doc_bytes, records, values, factor = await asyncio.to_thread(_measure_pair, sid)
    skipped_context = CONTEXT_NEEDS_KIRO_CLI
    try:
        # The pair is read whole (the installer takes the event log as text),
        # so a large one waits for memory exactly as a large arriving bundle does,
        # measured the same way, and the snapshot reads no more than was measured.
        async with _memory_admission(doc_bytes, records, values, factor):
            try:
                snapshot = await asyncio.to_thread(
                    kiro_cli_sessions.snapshot_session, sid, doc_bytes
                )
            except kiro_cli_sessions.SnapshotRefused as refused:
                return _error(str(refused), refused.code, _SNAPSHOT_STATUS.get(refused.code, 422))
            state_doc = snapshot.envelope.get("session_state") or {}
            agent = state_doc.get("agent_name") if isinstance(state_doc, dict) else ""
            skipped_context = await asyncio.to_thread(transcript_only_reason)
            doc: dict[str, Any] = {
                "bundle_version": BUNDLE_VERSION,
                "origin": ORIGIN,
                "title": snapshot.meta.title,
                "agent": agent if isinstance(agent, str) else "",
                "messages": snapshot.messages,
            }
            if not skipped_context:
                doc["layer_b"] = {"envelope": snapshot.envelope, "events": snapshot.events}
            else:
                # The session HAD native context this chat cannot load: say so,
                # so the installer marks the copy transcript-only.
                doc["layer_b_skipped"] = True
            bundle, err = await asyncio.to_thread(_validate_bundle, doc)
            del snapshot
            if err is not None:
                return err
            project, skipped = await asyncio.to_thread(_project_for_cwd, meta.cwd)
            response = await install_validated_bundle(
                request, bundle, project=project, top_level_folder=True
            )
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
        resources=(
            f"sid={sid},status={response.status},project={'yes' if project else 'no'},"
            f"context={skipped_context or 'native'}"
        ),
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
    if skipped_context:
        body["resume_mode"] = "transcript"
        body["context_skipped"] = skipped_context
    return web.json_response(body)
