"""Messaging handlers — spawn, notifications, send-message, slack profile."""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, cast

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.browser.command_bus import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_DRAIN_WAIT_MS,
    NoPanelError,
    QueueFullError,
    get_command_bus,
)
from kiro_crew.browser_cli import install as browser_cli_install
from kiro_crew.browser_cli import token as browser_cli_token
from kiro_crew.browser_cli import view as browser_cli_view
from kiro_crew.config import loader as _loader
from kiro_crew.config.loader import (
    IMESSAGE_SERVICES,
    TELEGRAM_ACTIVATIONS,
    KiroCrewConfig,
    config_path,
)
from kiro_crew.constants import CHANNEL_SEND_NAMESPACES
from kiro_crew.cron import CronStoreBusy, CronStoreUnreadable
from kiro_crew.dashboard.channel_folders import (
    LIVE_RELOAD_FIELDS,
    clean_session_folder,
    ensure_channel_folder,
    stored_folder_name,
)
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    _remove_queued_by_id,
    dashboard_slot_key,
    effective_session_key,
    mint_options_token,
    remember_slack_options,
    slack_options_owner_key,
)
from kiro_crew.dashboard.handlers._shared import (
    _pip_install_channel_available,
    pip_extra_install_command,
    read_bounded_json,
)
from kiro_crew.dashboard.origin import is_direct_local_request, is_proxied_request
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    DashboardState,
)
from kiro_crew.dashboard.token_auth import caller_names_a_missing_slot
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.link import SLACK_NAMESPACE, ChannelLink
from kiro_crew.messaging.renderer import (
    chunk_for_transport,
    chunk_text,
    display_safe_for,
    format_overflow,
)
from kiro_crew.messaging.transport import delivery_confirmed
from kiro_crew.notifications.bus import (
    NotificationPayload,
    NotificationValidationError,
)
from kiro_crew.platform.governance_profiles import HOST_SESSION_KEY
from kiro_crew.platform_compat import IS_MACOS
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.format import build_options_blocks, extract_options
from kiro_crew.slack.outbound import OPTIONS_FALLBACK_TEXT, PostedOptions
from kiro_crew.spawn_warm import warm_project_agents_for_spawn
from kiro_crew.subagent import effort_applied_note, effort_drop_reason
from kiro_crew.subagent_persistence import _agent_dir, read_state
from kiro_crew.validation import (
    _EMOJI_NAME_RE,
    CHANNEL_ID_RE,
    CHANNEL_MAX_LEN,
    CRON_SESSION_RE,
    SLACK_THREAD_TS_RE,
    SPAWN_RUN_SCHEMA,
    ValidationError,
    validate_tool_args,
)

#: Seconds to wait for Slack when verifying a pasted token at save time.
_TOKEN_VERIFY_TIMEOUT = 8

#: A Slack message timestamp is `<10-digit epoch>.<6-digit sequence>` -- 17
#: characters. The cap is what BOUNDS the value: these routes forward `ts` to
#: Slack and write it into a SEL audit line, and the allowlist check that
#: rejects an untracked channel runs AFTER that line is written.
_SLACK_TS_MAX_LEN = 30


def _is_slack_ts(value: object) -> bool:
    """True for a Slack message timestamp that is safe to forward.

    Uses the shared ``SLACK_THREAD_TS_RE`` rather than an inline
    ``^\\d+\\.\\d+$``: ``\\d`` is Unicode-aware, so a string of Arabic-Indic
    numerals satisfied the old check and was forwarded verbatim. The shared
    pattern spells the class ``[0-9]`` to avoid precisely that.
    """
    return (
        isinstance(value, str)
        and len(value) <= _SLACK_TS_MAX_LEN
        and bool(SLACK_THREAD_TS_RE.match(value))
    )


#: Public field name -> .env credential key for the two Slack secrets.
_SLACK_SECRET_FIELDS = {
    "bot_token": "SLACK_BOT_TOKEN",
    "app_token": "SLACK_APP_TOKEN",
}

#: Transports ``send_message``'s ``channel_type`` may name, which is also the set
#: its channel ``session`` values may name. Reads the shared
#: ``CHANNEL_SEND_NAMESPACES`` rather than subtracting the two non-targets here:
#: that subtraction was spelled at three separate readers, which is the same drift
#: shape that left a Webex owner DM unreachable while this module's own leg already
#: served it (#6514). The exclusions and their reasons are documented at the
#: definition — ``slack`` has its own client and streaming path and is deliberately
#: absent from ``state.channel_transports`` (``session="slack"`` is its spelling),
#: and ``unified`` is a session-key bucket rather than a transport.
_SEND_MESSAGE_CHANNEL_TYPES: frozenset[str] = frozenset(CHANNEL_SEND_NAMESPACES)
logger = logging.getLogger(__name__)


def _read_text_or_none(path: Path) -> str | None:
    """Read ``path`` as UTF-8, or return None if it does not exist.

    Pure synchronous filesystem I/O — call via ``asyncio.to_thread`` from an
    async handler so the stat + read never block the gateway event loop. Used to
    snapshot config.json before a credential write so a failed .env commit can
    roll the metadata back to a consistent pair.
    """
    return path.read_text(encoding="utf-8") if path.exists() else None


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Subagents ──

#: Generic ``code`` for a spawn rejection that mints no identifier of its own.
#: Spelled once for the two handlers that answer with it (``api_spawn`` and
#: ``api_spawn_continue``), so the pair cannot drift apart. A rejection a client
#: acts on differently gets its OWN code at the decision instead -- see
#: ``subagent.AGENT_NOT_FOUND_CODE``.
_SPAWN_REJECTED_CODE = "spawn_rejected"


async def api_spawn(request: web.Request) -> web.Response:
    """POST /api/spawn — spawn a subagent.

    Invariant: every error returned after ``state.subagents.spawn`` is called
    must include ``counted: true``. The manager counts submissions on entry;
    omitting the flag would make ``spawn_run`` reconcile the member again and
    could close a batch wave early.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        cleaned = validate_tool_args(
            {
                "task": body.get("task", ""),
                "agent": body.get("agent", ""),
                "max_turns": body.get("max_turns", 0),
                "cwd": body.get("cwd", ""),
                "model": body.get("model", ""),
                "reasoning_effort": body.get("reasoning_effort", ""),
                "include_memory": body.get("include_memory", True),
                "include_lessons": body.get("include_lessons", True),
                "include_project": body.get("include_project", True),
            },
            SPAWN_RUN_SCHEMA,
        )
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    task = (cleaned.get("task") or "").strip()
    if not task:
        return web.json_response({"error": "task is required"}, status=400)
    parent_session = body.get("parent_session", "")
    # approval_mode and silent are HTTP API parameters passed by the SDK,
    # NOT MCP tool arguments from the LLM.  The LLM's spawn_run tool
    # (mcp_core.py) does not expose these params — they are added by the
    # SDK's spawn() method for app-level control.  Validated inline here
    # rather than in SPAWN_RUN_SCHEMA because they are transport-layer
    # params, not tool-schema params.
    #
    # Security: this endpoint requires X-Internal-Secret (internal_paths
    # in server.py), so only local MCP server processes can call it.
    approval_mode = body.get("approval_mode", "")
    if approval_mode not in ("", "auto"):
        return web.json_response({"error": "approval_mode must be '' or 'auto'"}, status=400)
    silent = body.get("silent", False)
    if not isinstance(silent, bool):
        silent = str(silent).lower() in ("true", "1", "yes")
    # keep=True marks the run's session as a continuable conversation
    # (spawn_continue can dispatch follow-up turns into it). Transport-layer
    # param like silent/approval_mode.
    keep = body.get("keep", False)
    if not isinstance(keep, bool):
        keep = str(keep).lower() in ("true", "1", "yes")
    agent = cleaned.get("agent") or ""
    max_turns = cleaned.get("max_turns") or 0
    cwd = cleaned.get("cwd") or ""
    model = cleaned.get("model") or ""
    reasoning_effort = cleaned.get("reasoning_effort") or ""
    # Batch/wave identity (transport-layer params from spawn_run MCP, like
    # approval_mode/silent above): validated inline, bounded, never LLM-schema.
    batch_id = str(body.get("batch_id", "") or "")[:32]
    if batch_id and not batch_id.isalnum():
        return web.json_response({"error": "batch_id must be alphanumeric"}, status=400)
    try:
        batch_total = max(0, min(int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    # The async moment preceding the synchronous spawn(): warm here so the
    # on-loop, cache-only agent validation inside spawn() is a hit.
    if agent:
        await warm_project_agents_for_spawn(state, cwd)
    info = state.subagents.spawn(
        task,
        parent_session_key=parent_session,
        agent=agent,
        max_turns=max_turns,
        cwd=cwd,
        model=model or None,
        reasoning_effort=reasoning_effort,
        approval_mode=approval_mode or None,
        silent=silent,
        batch_id=batch_id,
        batch_total=batch_total,
        keep=keep,
        include_memory=cleaned.get("include_memory", True) is not False,
        include_lessons=cleaned.get("include_lessons", True) is not False,
        include_project=cleaned.get("include_project", True) is not False,
    )
    if not info:
        # Reached mgr.spawn (submission COUNTED at the top of spawn()) but
        # refused for capacity — tell the client so it does NOT reconcile
        # this member as a lost submission (double-count would close the
        # wave early).
        return web.json_response(
            {"error": f"capacity reached ({state.subagents.max_concurrent})", "counted": True},
            status=429,
        )
    if info.done and info.error:
        # Rejected INSIDE mgr.spawn: already counted as submitted and (for
        # batch members) announced through the completion consumer
        # (_announce_rejection). "counted" tells spawn_run's client-side
        # reconcile to skip this member.
        #
        # ``code`` is what the client switches on; ``error`` is advisory prose
        # (RFC 9457 3.1.3). Only the unknown-agent refusal mints its own
        # identifier today, because it is the only rejection a client treats
        # differently — spawn_run stops re-posting a name already refused. Every
        # other rejection reports the generic code, matching the sibling
        # /continue handler below.
        return web.json_response(
            {
                "error": info.error,
                "code": info.error_code or _SPAWN_REJECTED_CODE,
                "counted": True,
            },
            status=400,
        )
    resp: dict[str, object] = {"id": info.id, "task": task, "status": "spawned"}
    # Server-side effort verdict: only this side knows the model the factory's
    # effort gate will see (explicit per-call value, else the subagent role
    # pin, else the session chain for the effective agent — a crew's pin, else
    # a non-sentinel global). Additive, optional key — reporting only, never
    # changes whether the spawn happened.
    if reasoning_effort:
        # Mirror _run_inner's agent inheritance so the verdict judges the same
        # agent the session will actually use.
        verdict_agent = agent or (
            state.sessions.get_agent(parent_session) if parent_session else ""
        )

        def _effort_verdict() -> tuple[str, str]:
            d = effort_drop_reason(model, reasoning_effort, verdict_agent)
            if d:
                return d, ""
            return "", effort_applied_note(model, reasoning_effort, verdict_agent)

        # The resolvers read config and glob ~/.kiro/agents — file I/O that
        # must not run on the gateway event loop (the same reason
        # get_or_create runs _session_model in an executor).
        drop, applied = await asyncio.to_thread(_effort_verdict)
        if drop:
            resp["effort_dropped"] = drop
        elif applied:
            resp["effort_applied"] = applied
    if keep:
        # The conversation id is the FIRST run's id: spawn_continue targets it.
        resp["conversation"] = info.id
    return web.json_response(resp)


async def api_spawn_continue(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/continue — follow-up turn on a conversation.

    ``agent_id`` is the conversation id (the first keep=True run's id). Mints
    a NEW run on the same underlying session (resumed via session/load), so
    the follow-up executes with the conversation's accumulated context.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    conv_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    task = str(body.get("task", "") or "").strip()
    if not task:
        return web.json_response({"error": "task is required", "code": "task_required"}, status=400)
    parent_session = str(body.get("parent_session", "") or "")
    agent = str(body.get("agent", "") or "")
    model = str(body.get("model", "") or "")
    try:
        max_turns = max(0, min(int(body.get("max_turns", 0) or 0), 1000))
    except (TypeError, ValueError):
        max_turns = 0
    # The run's own cwd, resolved OFF the event loop: a continuation has to run
    # where the run ran (a project-local agent does not resolve against the pool
    # project), but reading state.json and probing the path are blocking calls and
    # `continue_conversation` is synchronous. Doing it here keeps the gateway
    # responsive even when the recorded path lives on a stalled mount.
    resumed_cwd = await asyncio.to_thread(state.subagents.recorded_cwd, conv_id)
    info = state.subagents.continue_conversation(
        conv_id,
        task,
        parent_session_key=parent_session,
        agent=agent,
        model=model or None,
        max_turns=max_turns,
        cwd=resumed_cwd,
    )
    if not info:
        return web.json_response(
            {
                "error": f"capacity reached ({state.subagents.max_concurrent})",
                "code": "capacity_reached",
            },
            status=429,
        )
    if info.done and info.error:
        if info.error.startswith("conversation_busy"):
            return web.json_response({"error": info.error, "code": "conversation_busy"}, status=409)
        if info.error.startswith("conversation_gone"):
            return web.json_response({"error": info.error, "code": "conversation_gone"}, status=404)
        return web.json_response({"error": info.error, "code": _SPAWN_REJECTED_CODE}, status=400)
    return web.json_response({"id": info.id, "conversation": conv_id, "status": "spawned"})


async def api_spawn_steer(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/steer — inject into a RUNNING run's turn.

    Body: ``{message, mode?}``. ``mode="interrupt"`` (default) injects into
    the running turn; ``mode="follow_up"`` queues the message for delivery as
    a continuation AFTER the run's current turn completes (never interrupts).
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    agent_id = request.match_info["agent_id"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    message = str(body.get("message", "") or "").strip()
    if not message:
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )
    mode = str(body.get("mode", "") or "interrupt").strip()
    if mode not in ("interrupt", "follow_up"):
        return web.json_response(
            {"error": "mode must be 'interrupt' or 'follow_up'", "code": "invalid_mode"},
            status=400,
        )
    if mode == "follow_up":
        ok, detail = await state.subagents.follow_up_run(agent_id, message)
    else:
        ok, detail = await state.subagents.steer_run(agent_id, message)
    if not ok:
        if detail == "not_found":
            return web.json_response({"error": detail, "code": "not_found"}, status=404)
        if detail.startswith("not_running"):
            return web.json_response({"error": detail, "code": "not_running"}, status=409)
        if detail.startswith("session_starting"):
            # Transient: the run is alive but its session has not registered
            # yet (#1113). 503 + Retry-After tells clients to retry, unlike
            # the terminal 502 steer_failed.
            return web.json_response(
                {"error": detail, "code": "session_starting"},
                status=503,
                headers={"Retry-After": "5"},
            )
        return web.json_response({"error": detail, "code": "steer_failed"}, status=502)
    return web.json_response(
        {"id": agent_id, "status": "follow_up_queued" if mode == "follow_up" else "steered"}
    )


async def api_spawn_release(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/release — end a continuable conversation.

    Deletes the persisted session mapping and the on-disk session files.
    Refuses while a run is in flight on the conversation.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    conv_id = request.match_info["agent_id"]
    ok, detail = state.subagents.release_conversation(conv_id)
    if not ok:
        if detail.startswith("conversation_busy"):
            return web.json_response({"error": detail, "code": "conversation_busy"}, status=409)
        return web.json_response({"error": detail, "code": "conversation_gone"}, status=404)
    return web.json_response({"conversation": conv_id, "status": "released"})


async def api_spawn_lost(request: web.Request) -> web.Response:
    """POST /api/spawn/lost — reconcile a batch member whose spawn POST failed.

    Called by ``spawn_run`` (mcp_core) when a member was explicitly rejected
    BEFORE ``mgr.spawn`` ran (validation 400 / 503), so the response carried
    no ``counted`` flag. Every sibling's ``batch_total`` already counts the
    lost member, so without this reconcile the wave's ``submitted < expected``
    forever and held digest results strand until restart (Opus MEDIUM + Design
    Review CONCERN 1).

    Transport failures are excluded because the gateway may have accepted the
    member before its response failed; reconciling that member as lost could
    close the wave early. The stuck-wave sweep safely handles truly lost
    transport submissions after its grace period.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    batch_id = str(body.get("batch_id", "") or "")[:32]
    if not batch_id or not batch_id.isalnum():
        return web.json_response({"error": "valid batch_id required"}, status=400)
    try:
        batch_total = max(0, min(int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    reason = str(body.get("reason", "") or "spawn submission failed")[:300]
    parent_session = str(body.get("parent_session", "") or "")
    state.subagents.record_lost_submission(
        batch_id, batch_total, reason, parent_session_key=parent_session
    )
    return web.json_response({"status": "reconciled", "batch_id": batch_id})


async def api_spawn_mark_collected(request: web.Request) -> web.Response:
    """POST /api/spawn/mark-collected — suppress injection for blocking tool.

    Called by the spawn_sub_agents MCP tool after it has polled and collected
    results inline.  Records the agent IDs on the parent slot so that the
    subsequent _subagent_done callback skips the _run_chat injection (the model
    already processed these results as a tool-call return value).  Without this,
    each completion event triggers a redundant LLM turn whose response shadows
    any [OPTIONS:] buttons the synthesis message rendered.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    ids = body.get("ids")
    if not ids or not isinstance(ids, list):
        return web.json_response(
            {"error": "'ids' array required", "code": "ids_required"}, status=400
        )
    parent_session = str(body.get("parent_session", "") or "")
    slot_name = dashboard_slot_key(parent_session)
    if not slot_name:
        return web.json_response({"status": "no_slot"})
    slot = state.get_slot(slot_name)
    if not slot:
        return web.json_response({"status": "no_slot"})
    # Record the IDs (bounded to 200 to prevent unbounded growth)
    for aid in ids[:200]:
        if isinstance(aid, str) and aid:
            slot._subagents_inline_collected.add(aid)
    return web.json_response({"status": "ok", "marked": len(ids)})


def _redact(text: str) -> str:
    """Two-pass redaction for LLM-derived content on external surfaces."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


_SPAWN_STATUS_MAX_LINES = 2000  # cap lines returned per spawn_status page
_SPAWN_STATUS_MAX_GREP_LEN = 500


def _spawn_result_view(text: str, offset: int, limit: int, grep: str) -> tuple[str, dict]:
    """Apply optional grep (regex line filter) then offset/limit line slicing.

    Line-oriented, like reading code: *offset* is a 0-based start line and *limit*
    caps returned lines (0 = to end, hard-capped at ``_SPAWN_STATUS_MAX_LINES``).
    When *grep* is set, lines are filtered by a case-insensitive regex first, then
    offset/limit apply to the matches. Returns ``(view_text, meta)``; on a bad
    regex ``meta['grep_error']`` is set and *view_text* is empty. Pure CPU — run
    via ``asyncio.to_thread`` so a pathological regex never stalls the loop.
    """
    lines = text.splitlines()
    total = len(lines)
    if grep:
        try:
            pat = re.compile(grep[:_SPAWN_STATUS_MAX_GREP_LEN], re.IGNORECASE)
        except re.error as exc:
            return "", {"grep_error": f"invalid grep regex: {exc}"}
        lines = [ln for ln in lines if pat.search(ln)]
    meta: dict = {"total_lines": total}
    if grep:
        meta["matched_lines"] = len(lines)
    start = min(max(0, offset), len(lines))
    span = _SPAWN_STATUS_MAX_LINES if limit <= 0 else min(limit, _SPAWN_STATUS_MAX_LINES)
    end = min(len(lines), start + span)
    meta["offset"] = start
    meta["returned_lines"] = end - start
    meta["has_more"] = end < len(lines)
    return "\n".join(lines[start:end]), meta


async def _apply_result_view(request: web.Request, text: str) -> tuple[str, dict]:
    """Read offset/limit/grep query params and apply :func:`_spawn_result_view`.

    Returns ``(text, {})`` unchanged when no paging/filter params are present, so
    the default ``spawn_status`` contract (full transcript) is preserved. Only a
    paged/filtered request pays the split+regex cost, offloaded to a thread.
    """

    def _q_int(name: str) -> int:
        try:
            return max(0, int(request.query.get(name, 0)))
        except (TypeError, ValueError):
            return 0

    offset = _q_int("offset")
    limit = _q_int("limit")
    grep = (request.query.get("grep") or "").strip()[:_SPAWN_STATUS_MAX_GREP_LEN]
    if not (grep or offset > 0 or limit > 0):
        return text, {}
    return await asyncio.to_thread(_spawn_result_view, text, offset, limit, grep)


async def api_spawn_status(request: web.Request) -> web.Response:
    """GET /api/spawn/{id} — poll subagent status."""
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    agent_id = request.match_info["agent_id"]
    info = state.subagents.get(agent_id)
    if not info:
        # Fall back to persistence layer (orphaned/recovered agents)
        try:
            disk_state = read_state(agent_id)
            if disk_state:
                disk_data: dict[str, object] = {
                    "id": agent_id,
                    "task": _redact(disk_state.get("task", "")),
                    "done": True,
                    "started": disk_state.get("started"),
                }
                result_path = _agent_dir(agent_id) / "result.txt"
                result = ""
                if result_path.exists() and not is_sensitive_path(str(result_path)):
                    try:
                        result = await asyncio.to_thread(
                            result_path.read_text, encoding="utf-8", errors="replace"
                        )
                    except OSError:
                        pass
                # _redact() defined at line 82 of this file; calls both
                # redact_exfiltration_urls() and redact_credentials() per security guidelines.
                view, view_meta = await _apply_result_view(request, result)
                if view_meta:
                    disk_data["result_meta"] = view_meta
                disk_data["result"] = _redact(view) if view else "_No result._"
                # Check for tombstone
                tombstone_path = _agent_dir(agent_id) / "tombstone.json"
                if tombstone_path.exists() and not is_sensitive_path(str(tombstone_path)):
                    try:
                        raw = await asyncio.to_thread(tombstone_path.read_text, encoding="utf-8")
                        ts = json.loads(raw)
                        disk_data["error"] = _redact(f"Orphaned: {ts.get('cause', 'unknown')}")
                    except (OSError, ValueError):
                        disk_data["error"] = "Orphaned (unknown cause)"
                else:
                    disk_data["error"] = ""
                return web.json_response(disk_data)
        except Exception:
            logger.debug("Persistence fallback failed for %s", agent_id, exc_info=True)
        return web.json_response({"error": "not found"}, status=404)
    data = {"id": info.id, "task": _redact(info.task), "done": info.done}  # type: dict[str, object]
    data["started"] = info.started
    if info.done:
        # Read full result from disk (info.result is truncated to 3000 chars)
        result = info.result
        if info.result_path and not is_sensitive_path(info.result_path):
            try:
                result = await asyncio.to_thread(
                    Path(info.result_path).read_text,
                    encoding="utf-8",
                    errors="replace",
                )
            except OSError:
                pass
        view, view_meta = await _apply_result_view(request, result)
        data["result"] = _redact(view)
        if view_meta:
            data["result_meta"] = view_meta
        data["error"] = _redact(info.error) if info.error else ""
    else:
        data["turns"] = info.turns
        data["last_tool"] = _redact(info.last_tool)
        data["elapsed"] = round(time.time() - info.started)
        # Same predicate, same present-only-while-true convention as
        # api_spawn_list. This endpoint is the one a blocking `kirocrew spawn
        # run` polls every 2s (cli_commands.py), so leaving it out is what kept
        # the CLI reproduction of #6484 silent: the caller sat on "waiting for
        # result..." while the answer ("a prompt is waiting for you") was only
        # discoverable from a separate `spawn list` or a log grep.
        if _awaiting_spawn_approval(info):
            data["awaiting_approval"] = True
    return web.json_response(data)


def _awaiting_spawn_approval(info: object) -> bool:
    """True only while a run is parked on the SPAWN-approval gate.

    ``_awaiting_approval`` alone is NOT sufficient: ``run.py`` sets the same
    flag for TOOL approvals raised INSIDE a running subagent (three sites), so
    reading it bare would report a run at turn 5 waiting on a tool prompt as
    though it were waiting to START -- rendering "waiting for spawn approval"
    and telling a caller to approve it "to start this run" that already
    started. ``_exec_started`` is the permanent discriminator: it is stamped
    once when execution begins, so ``None`` means the run never entered
    execution, which for a registered run is only reachable via the spawn gate.

    Both read paths go through this ONE predicate rather than repeating the
    pair, because the two handlers build their payloads independently and a
    drift between them is invisible to a behavioural test.

    ``subagent_manager/terminal.py`` computes the same pair for the reap message
    (#7325). Deliberately not extracted onto ``SubagentInfo``: that read is a
    plain attribute read on a live run inside the manager package, whereas this
    one must survive the info doubles the handlers are tested with (below), and
    unifying them would mean editing a reap path this change does not otherwise
    touch. The duplication is two lines and both sites name each other.

    ``getattr`` with a strict ``is True`` / ``is None``: these handlers are
    exercised with lightweight info doubles (SimpleNamespace / MagicMock) that
    carry only the fields a case cares about, so a bare attribute read raises
    and a truthy Mock would otherwise advertise a wait that isn't happening.
    """
    return (
        getattr(info, "_awaiting_approval", False) is True
        and getattr(info, "_exec_started", None) is None
    )


async def api_spawn_list(request: web.Request) -> web.Response:
    """GET /api/spawn — list all subagents."""
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"agents": []})
    agents = []
    for info in state.subagents.all_agents:
        entry: dict[str, object] = {
            "id": info.id,
            "task": _redact(info.task),
            "done": info.done,
            "parent": info.parent_session_key,
            "agent": info.agent,
            "started": info.started,
        }
        if info.done:
            entry["result"] = _redact(info.result)
            entry["error"] = _redact(info.error) if info.error else ""
            entry["stopped"] = info.user_stopped
            entry["outcome"] = info.outcome
        else:
            entry["turns"] = info.turns
            entry["last_tool"] = _redact(info.last_tool)
            entry["elapsed"] = round(time.time() - info.started)
            # Present only while the run is parked on its spawn-approval
            # prompt, so the default payload is unchanged. Without it a run
            # waiting for a human is byte-identical to one that is executing --
            # which is how #6484 presented: `kirocrew spawn list` showed the
            # same hourglass for a run that had no child process and was only
            # ever waiting to be approved.
            if _awaiting_spawn_approval(info):
                entry["awaiting_approval"] = True
        # Present only when a group was actually withheld, so the default
        # (everything on) payload is unchanged.
        withheld = [
            group
            for group, on in (
                ("memory", info.include_memory),
                ("lessons", info.include_lessons),
                ("project", info.include_project),
            )
            if not on
        ]
        if withheld:
            entry["context_withheld"] = withheld
        agents.append(entry)
    return web.json_response({"agents": agents})


async def api_spawn_retry(request: web.Request) -> web.Response:
    """POST /api/spawn/{agent_id}/retry — re-spawn a FAILED subagent's task.

    Backs the chip's "Retry failed (N)" batch control. Only terminal failed
    agents are retryable (never running ones — that would double the work —
    and never user-stopped ones — the user killed that work on purpose).
    Spawns a fresh agent with the original task/agent/parent (new id; the old
    terminal card stays for history). Batch identity is NOT carried over: the
    retry is a standalone spawn, so a wave's digest accounting (already
    completed) is never reopened.
    """
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"error": "subagents not available"}, status=503)
    agent_id = request.match_info["agent_id"]
    if agent_id.startswith("native:"):
        return web.json_response(
            {"error": "native subagents run inside the parent turn and cannot be retried"},
            status=400,
        )
    old = state.subagents.get(agent_id)
    if not old:
        return web.json_response({"error": "not found"}, status=404)
    if not old.done:
        return web.json_response({"error": "agent is still running"}, status=409)
    if old.outcome != "failed":
        return web.json_response(
            {"error": f"only failed agents can be retried (outcome={old.outcome})"},
            status=409,
        )
    # Same validated warm as the primary spawn handler. old.cwd was validated
    # at the ORIGINAL spawn, but the allowlist may have changed since (and a
    # gateway restart leaves the cache cold), so it is re-checked against the
    # current config before any discovery read.
    if old.agent:
        await warm_project_agents_for_spawn(state, old.cwd or "")
    info = state.subagents.spawn(
        old._raw_task or old.task,
        parent_session_key=old.parent_session_key,
        agent=old.agent,
        max_turns=old.max_turns,
        cwd=old.cwd,
        model=old.model or None,
        # Like model and the context groups: a retry must run at the SAME
        # effort as the run it replaces, or it is a different experiment.
        reasoning_effort=old.reasoning_effort,
        approval_mode=old.approval_mode or None,
        silent=old.silent,
        # A retry must see the SAME context scope as the run it replaces —
        # otherwise the retried agent is a different experiment.
        include_memory=old.include_memory,
        include_lessons=old.include_lessons,
        include_project=old.include_project,
    )
    if not info:
        return web.json_response(
            {"error": f"capacity reached ({state.subagents.max_concurrent})"}, status=429
        )
    if info.done and info.error:
        return web.json_response({"error": info.error}, status=400)
    return web.json_response({"id": info.id, "retried_from": agent_id, "status": "spawned"})


async def api_spawn_delete(request: web.Request) -> web.Response:
    """DELETE /api/spawn/{agent_id} — cancel a running subagent or remove a finished one."""
    state: DashboardState = request.app["state"]
    agent_id = request.match_info["agent_id"]
    # Handle native kiro-cli subagents (native:* IDs not in SubagentManager)
    if agent_id.startswith("native:") and hasattr(state, "_native_cards"):
        card_info = getattr(state, "_native_cards", {}).get(agent_id)
        if card_info:
            # Can't actually kill the kiro-cli internal sub-agent, but we can
            # close the Activity card so it stops showing "Starting..."
            state._native_cards.pop(agent_id, None)
            # Persist the stop on the slot-owned tracker record so WS replay
            # (native_subagent_snapshots) reconstructs this card as STOPPED for
            # reconnecting clients — not as still-running or completed.
            try:
                _slot = state.get_slot(card_info["slot"])
                _rec = (
                    _slot._native_subagent_tracker.get(card_info.get("session_id", ""))
                    if _slot is not None
                    else None
                )
                if _rec is not None and not _rec.get("done"):
                    _rec["done"] = True
                    _rec["done_at"] = time.time()
                    _rec["elapsed"] = time.time() - card_info.get("started", time.time())
                    _rec["error"] = None
                    _rec["stopped"] = True
                    _rec["outcome"] = "stopped"
                    _rec["result"] = "(cancelled)"
            except Exception:
                logger.debug("native cancel: tracker update failed for %s", agent_id, exc_info=True)
            # User-initiated cancellation is an auditable action (parity with
            # the managed path, which audits inside SubagentManager.cancel()).
            try:
                _sel().log_tool_invocation(
                    session_key=card_info["slot"],
                    source="subagent",
                    tool_name="cancel_native_subagent",
                    outcome="cancelled_by_user",
                    metadata={"card_id": agent_id},
                )
            except Exception:
                logger.debug("SEL audit failed for native cancel %s", agent_id, exc_info=True)
            state.broadcast_ws(
                "subagent_done",
                {
                    "id": agent_id,
                    "slot": card_info["slot"],
                    "elapsed": time.time() - card_info.get("started", time.time()),
                    "error": None,
                    "stopped": True,
                    "task": "",
                    "agent": "",
                    "result": "(cancelled)",
                },
            )
            return web.json_response({"ok": True, "cancelled": True})
        return web.json_response({"error": "not found"}, status=404)
    if not state.subagents or agent_id not in state.subagents._agents:
        return web.json_response({"error": "not found"}, status=404)
    cancelled = await state.subagents.cancel(agent_id)
    if not cancelled:
        # Already done — just remove from list
        state.subagents._agents.pop(agent_id, None)
        state.subagents._tasks.pop(agent_id, None)
    return web.json_response({"ok": True, "cancelled": cancelled})


async def api_spawn_stop_all(request: web.Request) -> web.Response:
    """POST /api/spawn/stop-all — stop one chat's running and queued subagents."""
    state: DashboardState = request.app["state"]
    request_app = request.get("app", "")
    if "app" not in request or request_app:
        _sel().log_api_access(
            caller=request_app or "unknown",
            operation="spawn.stop_all",
            outcome="denied",
            source="app_isolation",
            resources="dashboard-only bulk cancellation",
            error="app tokens cannot stop dashboard subagent waves",
        )
        return web.json_response(
            {"error": "app token not allowed", "code": "app_token_forbidden"}, status=403
        )
    if not state.subagents:
        return web.json_response(
            {"error": "subagents not available", "code": "subagents_unavailable"},
            status=503,
        )
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    slot_name = body.get("slot") if isinstance(body, dict) else None
    if not isinstance(slot_name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,256}", slot_name):
        return web.json_response(
            {"error": "valid slot is required", "code": "invalid_slot"}, status=400
        )
    slot = state.get_slot(slot_name)
    if slot is None:
        return web.json_response({"error": "slot not found", "code": "slot_not_found"}, status=404)
    running, queued = await state.subagents.cancel_for_parent(effective_session_key(slot))
    return web.json_response(
        {"ok": True, "stopped": running + queued, "running": running, "queued": queued}
    )


async def api_spawn_clear(request: web.Request) -> web.Response:
    """DELETE /api/spawn — clear all completed subagents."""
    state: DashboardState = request.app["state"]
    if not state.subagents:
        return web.json_response({"ok": True})
    done_ids = [a.id for a in state.subagents.all_agents if a.done]
    for aid in done_ids:
        state.subagents._agents.pop(aid, None)
        state.subagents._tasks.pop(aid, None)
    return web.json_response({"ok": True, "cleared": len(done_ids)})


# ── Sessions / Notifications ──


async def api_notifications(request: web.Request) -> web.Response:
    state: DashboardState = request.app["state"]
    return web.json_response(
        {"notifications": state._notification_log, "unread": state._unread_count}
    )


async def api_notification_delete(request: web.Request) -> web.Response:
    """DELETE /api/notifications — delete a single notification by timestamp."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    ok = await state.delete_notification(ts)
    return web.json_response({"ok": ok})


async def api_notifications_clear(request: web.Request) -> web.Response:
    """POST /api/notifications/clear — clear all notifications."""
    state: DashboardState = request.app["state"]
    await state.clear_notifications()
    return web.json_response({"ok": True})


async def api_notification_ack(request: web.Request) -> web.Response:
    """POST /api/notifications/ack — mark a single notification as read."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    ok = await state.ack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notification_unack(request: web.Request) -> web.Response:
    """POST /api/notifications/unack — mark a single notification as unread."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ts = body.get("ts", "")
    if not ts:
        return web.json_response({"error": "ts is required"}, status=400)
    # If this is a cron notification, also remove the last acked item from the job
    for n in state._notification_log:
        if n.get("ts") == ts and n.get("kind") == "cron" and n.get("job_id"):
            try:
                await state.crons.unack_job_async(n["job_id"])
            except (CronStoreBusy, CronStoreUnreadable) as exc:
                # Store transiently contended, or refusing writes outright — the
                # notification-level unack below still succeeds; the acked-item
                # trim is best-effort. Unreadable degrades here rather than
                # surfacing a 409: the person unacking a notification asked
                # nothing of the cron store, so failing their request for a
                # bookkeeping trim they did not request would be the wrong
                # trade. Escaping instead becomes a 500 on a request that does
                # not depend on the store at all.
                logger.warning("unack_job skipped: %s (job %s)", type(exc).__name__, n["job_id"])
            break
    ok = await state.unack_notification(ts)
    return web.json_response({"ok": ok})


async def api_notifications_ack_all(request: web.Request) -> web.Response:
    """POST /api/notifications/ack-all — mark all notifications as read."""
    state: DashboardState = request.app["state"]
    for n in state._notification_log:
        n["acked"] = True
    # Same ordered executor as every other notification-file mutation: a
    # rewrite submitted after a queued delivery append can never be
    # overtaken by it, and durability is awaited before responding.
    await state._rewrite_notifications_async()
    state.broadcast_ws("notification_ack", {"ts": "*"})
    return web.json_response({"ok": True})


async def api_notification_channels(request: web.Request) -> web.Response:
    """GET /api/notifications/channels — registered channels + user settings.

    Returns every channel the bus knows about, grouped by source (``system``
    or the owning app name), each with its default priority, the user's
    stored settings, and whether it is protected (approval cannot be muted).
    Channels with stored settings but no live registration (e.g. app
    currently disabled) are included so mutes remain visible and editable.
    """
    from kiro_crew.notifications.settings import PROTECTED_CHANNELS

    state: DashboardState = request.app["state"]
    registered = state.notification_bus.channels()
    stored = state.notification_channel_settings.all_settings()
    channels = []
    for channel in sorted(set(registered) | set(stored)):
        source = channel.split(".", 1)[0]
        channels.append(
            {
                "channel": channel,
                "source": source,
                "registered": channel in registered,
                "default_priority": registered.get(channel),
                "protected": channel in PROTECTED_CHANNELS,
                "settings": stored.get(channel, {}),
            }
        )
    return web.json_response({"channels": channels})


async def api_notification_channel_settings(request: web.Request) -> web.Response:
    """PUT /api/notifications/channels/settings — update one channel's settings.

    Body: ``{"channel": str, "muted"?: bool, "priority"?: str|null}`` —
    ``priority: null`` clears the override. Protected channels reject mute
    and priority-lowering with 400.
    """
    from kiro_crew.notifications.settings import ChannelSettingsError

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if not isinstance(body, dict):
        # Valid-but-non-object JSON ([], null, "str") would AttributeError on
        # body.get below -- an unintended 500 instead of a validation 400.
        return web.json_response({"error": "body must be a JSON object"}, status=400)
    channel = body.get("channel")
    if not isinstance(channel, str) or not channel.strip():
        return web.json_response({"error": "channel is required"}, status=400)
    channel = channel.strip()
    if len(channel) > 256:
        return web.json_response({"error": "channel name too long"}, status=400)
    muted = body.get("muted")
    if muted is not None and not isinstance(muted, bool):
        return web.json_response({"error": "muted must be a boolean"}, status=400)
    has_priority = "priority" in body
    priority = body.get("priority")
    if has_priority and priority is not None and not isinstance(priority, str):
        return web.json_response({"error": "priority must be a string or null"}, status=400)
    try:
        # update() persists via atomic_write (blocking file I/O) -- keep it
        # off the event loop. ChannelSettings serializes internally with its
        # own lock, so concurrent updates from worker threads are safe.
        entry = await asyncio.to_thread(
            state.notification_channel_settings.update,
            channel,
            muted=muted,
            priority=priority if has_priority and priority is not None else None,
            clear_priority=has_priority and priority is None,
        )
    except ChannelSettingsError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    state.broadcast_ws("notification_channel_settings", {"channel": channel, "settings": entry})
    return web.json_response({"ok": True, "channel": channel, "settings": entry})


async def api_notification_agent_push(request: web.Request) -> web.Response:
    """POST /api/notifications/agent — send_notification MCP tool (RFC Phase 5).

    Agent sessions publish schema-v2 notifications through the system.agent
    channel. Body: ``{"title": str, "body"?: str, "priority"?: str,
    "url"?: str, "group_key"?: str, "actions"?: [{id,label,url?}]}``.
    ``source``/``channel`` are server-fixed (never body-supplied), and the
    full payload validation applies — internal-path urls, action caps,
    length caps. Durability mirrors the app push: a 200 awaits the persist.
    """
    state: DashboardState = request.app["state"]
    # App tokens must never reach this endpoint (GPT 5.6 round 16): an app's
    # declared ``permissions.api`` uses prefix-boundary matching, so an app
    # allowed ``/api/notifications`` is also admitted to this child route by
    # the auth middleware. This publish path is MCP/internal-secret only —
    # it publishes ``source="system"`` (channel system.agent), so an app
    # reaching it could impersonate system notifications and bypass its app
    # rate limits / declared-channel checks. Apps publish through
    # POST /api/notifications where their
    # token-verified ``app:<name>`` source is enforced. The middleware publishes
    # ``request["app"]`` on app-token auth and, since issue #3690, also on the
    # internal-secret path whenever the calling session resolves to an app — so
    # this check now bites for an app agent arriving over MCP too, which it
    # could not before.
    if request.get("app"):
        # Permission denial on a security boundary — audited before the
        # response (backend-security-controls: every denial emits SEL).
        _sel().log_api_access(
            caller=f"app:{request.get('app')}",
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="app tokens forbidden on the agent publish path",
        )
        return web.json_response({"error": "forbidden for app tokens"}, status=403)
    # MCP/internal-secret ONLY (GPT 5.6 round 19): the strict-internal
    # middleware also admits loopback dashboard-COOKIE callers to this
    # route, and a browser-credentialed caller publishing source="system"
    # would bypass MCP governance. The middleware sets
    # request["internal_auth"] only on the
    # validated X-Internal-Secret path — exactly the transport the
    # send_notification tool uses.
    if not request.get("internal_auth"):
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "internal-secret authentication required"}, status=403)
    # A caller whose own slot is GONE cannot be attributed (issue #3690). The
    # app-token check above refuses an app by name, but a tab closed while this
    # call was in flight takes the ``_app`` that check reads with it, so an
    # app-owned session going through that race would publish source="system"
    # here as though it were the person. Absence of an app claim is only
    # trustworthy for a caller that never had a slot (a Slack thread, a cron the
    # person owns); a ``dashboard:`` key names one, so a missing slot is a
    # failure to attribute rather than proof of the dashboard user. Refused HERE
    # rather than in the middleware: a popped slot no longer says whose tab it
    # was, so refusing centrally would also refuse the person's own in-flight
    # calls on every internal route. This route refuses because of what it
    # publishes -- ``source="system"`` on the system.agent channel.
    if caller_names_a_missing_slot(
        getattr(state, "_slots", None), request.headers.get("X-Session-Key", "")
    ):
        _sel().log_api_access(
            caller=str(request.headers.get("X-Session-Key") or ""),
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error="calling session's slot is gone; cannot attribute a system publish",
        )
        return web.json_response(
            {"error": "calling session not found", "code": "caller_session_missing"},
            status=403,
        )
    # Bound the body BEFORE decoding, mirroring the app push endpoint: without
    # this the strict-internal route inherits the server-wide client_max_size,
    # and a large JSON object would be buffered and decoded on the event-loop
    # thread. Shared helper so the cap and the 413/400
    # contract cannot drift from the app push endpoint.
    body, _cap_err = await read_bounded_json(request)
    if _cap_err is not None:
        return _cap_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    # Type-check optional fields BEFORE payload construction: the bus
    # validator assumes str/list shapes, so a non-string url or non-list
    # actions would raise AttributeError/TypeError past the
    # NotificationValidationError catch -- a 500 where the contract says 400.
    for field_name in ("title", "body", "priority", "url", "group_key"):
        value = body.get(field_name)
        if value is not None and not isinstance(value, str):
            return web.json_response({"error": f"{field_name} must be a string"}, status=400)
    actions = body.get("actions")
    if actions is not None and not isinstance(actions, list):
        return web.json_response({"error": "actions must be a list"}, status=400)
    payload = NotificationPayload(
        source="system",
        channel="system.agent",
        kind="agent",
        title=body.get("title") or "",
        body=body.get("body") or "",
        priority=body.get("priority"),
        url=body.get("url"),
        group_key=body.get("group_key"),
        actions=actions,
    )
    try:
        note = state.notification_bus.push(payload)
    except NotificationValidationError as exc:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="denied",
            source="notifications_api",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=400)
    except Exception:
        logger.exception("agent notification delivery failed")
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="delivery failed",
        )
        return web.json_response({"error": "notification delivery failed"}, status=500)
    # Same durability guarantee as the app push endpoint: only acknowledge
    # once the persist job has succeeded.
    persist = state.last_notification_persist
    if persist is not None and not await persist:
        _sel().log_api_access(
            caller="agent",
            operation="notification_agent_push",
            outcome="error",
            source="notifications_api",
            error="persistence failed",
        )
        return web.json_response({"error": "failed to persist notification"}, status=500)
    _sel().log_api_access(
        caller="agent",
        operation="notification_agent_push",
        outcome="success",
        source="notifications_api",
    )
    return web.json_response({"ok": True, "note": note})


_MAX_BLOCKS = 50  # Slack Block Kit limit
_MAX_WALK_DEPTH = 10  # defense-in-depth against deeply nested LLM output


def _redact_all(value: str) -> str:
    """Both outbound redactors as one callable, in the canonical order.

    ``redact_for_display`` re-runs its redactor over each normalised form, so it
    needs the pair behind a single call rather than two sequential passes.
    """
    value, _ = redact_exfiltration_urls(value)
    value, _ = redact_credentials(value)
    return value


def _sanitize_blocks(
    blocks: list[dict],
    *redactors: Any,
) -> list[dict]:
    """Walk Block Kit blocks and sanitize all strings (both keys and values).

    Block Kit structural keys (type, text, mrkdwn, etc.) pass through
    sanitizers unchanged since they don't match hostile patterns.
    """
    from copy import deepcopy  # noqa: F811

    def _redact_str(s: str) -> str:
        for fn in redactors:
            s, _ = fn(s)
        return s

    def _walk(obj: Any, depth: int = 0) -> Any:
        if depth > _MAX_WALK_DEPTH:
            if isinstance(obj, str):
                return _redact_str(obj)
            if isinstance(obj, (dict, list)):
                return {} if isinstance(obj, dict) else []
            return obj  # scalars (int, bool, None) are safe
        if isinstance(obj, str):
            return _redact_str(obj)
        if isinstance(obj, dict):
            return {_redact_str(k): _walk(v, depth + 1) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_walk(item, depth + 1) for item in obj]
        return obj

    return _walk(deepcopy(blocks[:_MAX_BLOCKS]))


def _resolve_session_target(
    state: DashboardState, target: str, caller_session: str
) -> tuple[str, str] | tuple[None, None]:
    """Resolve a session target to a dashboard slot key and job name.

    ``target="origin"`` looks up the cron job that owns *caller_session*
    and returns ``(session_key, job_name)``.
    Returns ``(None, None)`` if the origin session can't be resolved
    (non-"origin" target, non-cron caller, unknown job, or cron with no
    originating session_key — e.g. one created from the dashboard UI).

    Note: ``target="slack"`` is NOT handled here — it is intercepted in
    ``api_send_message`` and converted to an explicit fall-through to the
    Slack DM path, so it never reaches this resolver.
    """
    if target != "origin":
        return None, None  # only "origin" is allowed — reject arbitrary slot keys
    # caller_session is "cron:{job_id}" or "cron:{job_id}:{run_id}" (stateless)
    if not caller_session.startswith("cron:"):
        return None, None
    cron_id = caller_session.removeprefix("cron:").split(":")[0]
    jobs = state.crons.list_jobs(include_disabled=True)
    job = next((j for j in jobs if j.id == cron_id), None)
    if not job or not job.session_key:
        return None, None
    # session_key is e.g. "dashboard:chat-3-1712793600" but slot names
    # don't have the "dashboard:" prefix
    slot_key = job.session_key.removeprefix("dashboard:")
    return slot_key, job.name


#: ``session`` values on ``/api/send-message`` that name a delivery MODE rather
#: than a chat channel. Any other value is looked up in the registered channel
#: transports, so a channel becomes reachable here by registering its transport
#: instead of by editing this module. Slack is reserved because it is delivered
#: by its own client, which is not in ``channel_transports``.
_RESERVED_SESSION_TARGETS = frozenset({"origin", SLACK_NAMESPACE})

#: The shape every registered ``channel_type`` has. A ``session`` value that does
#: not match is not looked up as a channel at all: the value is agent-authored and
#: reaches an error body and the SEL ``resources`` field, so bounding its length
#: and alphabet here keeps an unrecognized value from carrying newlines or
#: kilobytes into the audit trail. It still degrades to the dashboard
#: notification, which is what an unknown ``session`` has always done.
_CHANNEL_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: The ``configured_targets()`` prefix every transport gives a DIRECT
#: conversation (``user:<identity>``). A ``thread:`` or room target is a
#: different audience and is never the owner's DM.
_DM_TARGET_PREFIX = "user:"

#: Request fields that only exist in Slack's protocol. Combined with a channel
#: ``session`` they are refused rather than dropped: a caller that asked for a
#: threaded reply or a named Slack channel cannot observe the drop, and would
#: read a private DM as a successful post to a shared channel.
_SLACK_ONLY_BODY_FIELDS = (
    "channel",
    "user",
    "blocks",
    "thread_ts",
    "reply_broadcast",
    "unfurl_links",
    "unfurl_media",
)


def _owner_dm_target(transport: Any) -> str:
    """The configured owner-DM target id on *transport*, or ``""``.

    ``configured_targets()`` is the channel-neutral allowlist the dashboard's own
    target picker reads, so a proactive DM can only be addressed at something the
    user configured for that channel. Two entries are skipped: a target the
    transport marks unavailable (WeCom may only reply to an inbound message), and
    a thread or room target, which is a wider audience than a DM.

    No channel carries a separate "owner" field the way Slack's ``owner_id`` does,
    so an owner can only be inferred, and this REFUSES to infer one from an
    ambiguous list: a target is returned only when the channel advertises exactly
    ONE available direct target. Picking the first of several would send private
    agent output to whichever allow-listed person happened to sort first, which is
    the wrong human rather than a smaller audience, and fanning out to all of them
    would deliver a message the agent decided to send once N times. With no single
    answer the caller degrades to the dashboard notification, which reaches the
    operator without guessing who they are.
    """
    try:
        targets = list(transport.configured_targets())
    except Exception:
        logger.warning("send_message: could not enumerate channel targets", exc_info=True)
        return ""
    direct = [
        str(getattr(target, "target_id", "") or "")
        for target in targets
        if str(getattr(target, "target_id", "") or "").startswith(_DM_TARGET_PREFIX)
        and getattr(target, "available", False)
    ]
    if len(direct) == 1:
        return direct[0]
    if direct:
        logger.info(
            "send_message: %d configured DM targets and no owner field, so no single "
            "recipient can be inferred; degrading to the dashboard notification",
            len(direct),
        )
    return ""


async def _deliver_channel_dm(
    state: DashboardState, channel_type: str, text: str, *, caller_session: str
) -> tuple[bool, str, str]:
    """Deliver *text* as a DM on *channel_type*. Returns ``(sent, code, detail)``.

    Channel-neutral by construction: the transport is looked up by name, the
    destination comes from its own configured-target allowlist, and delivery runs
    through the shared cross-surface send ladder
    (``chat_runner._resolve_channel_target``), the same fail-closed, SEL-audited
    ``channels`` governance gate the mirror-link endpoint passes. A channel added
    later needs no code here.

    An empty *code* with ``sent=False`` is the SOFT miss: the channel is not
    connected, or the user configured no reachable DM target. It degrades to the
    dashboard notification the caller already got, exactly as an absent Slack
    client does. A non-empty *code* is a refusal or a failed delivery, and the
    caller must answer non-2xx: reporting success for a message the user will
    never see is the failure this contract exists to prevent.
    """
    transport = state.get_channel_transport(channel_type)
    if transport is None:
        logger.info("send_message: channel %s is not connected", channel_type)
        return False, "", ""
    target_id = _owner_dm_target(transport)
    if not target_id:
        logger.info("send_message: channel %s advertises no DM target", channel_type)
        return False, "", ""
    # Imported here, not at module scope: chat_runner imports from
    # kiro_crew.dashboard.handlers, so a top-level import closes that cycle. Only
    # this one is deferred -- `HOST_SESSION_KEY` is a constant and is imported at
    # module scope, where the top-level-imports rule wants it.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    # A cron carries its own validated session key; an out-of-band send owns no
    # session, and the host sentinel is what operators bind host-side governance
    # to (an empty key classifies as unknown and matches no profile at all).
    session_key = caller_session or HOST_SESSION_KEY
    # Vet BEFORE resolving the target: resolution is itself a visible side effect
    # on some channels (Discord opens a DM channel over REST), so a denied
    # channel must never reach it. Offloaded because the governance evaluation
    # reads profile files.
    # The principal is supplied rather than derived: this addresses a
    # ``configured_targets()`` entry, so the link carries a ``user:<id>`` target id
    # and ``session_key`` is a host sentinel naming nobody. The id came off the
    # transport's own allow-list via ``_owner_dm_target``, which is the authoritative
    # answer the recipient check would otherwise be unable to reach.
    governed = await asyncio.to_thread(
        functools.partial(
            _resolve_channel_target,
            state,
            session_key,
            ChannelLink(channel_type=channel_type, channel_id=target_id),
            principal=target_id.removeprefix(_DM_TARGET_PREFIX),
        )
    )
    if governed is None:
        return False, "channel_not_permitted", f"{channel_type} is not permitted"
    _, live_transport = governed
    try:
        resolved = await live_transport.resolve_configured_target(target_id)
    except Exception as exc:
        logger.exception("send_message: %s target resolution failed", channel_type)
        return False, "channel_delivery_failed", str(exc)
    if resolved is None:
        # The advertised target stopped resolving between enumeration and use
        # (revoked from the allow-list, or no longer a private thread). Refuse
        # rather than fall back to a wider audience.
        return False, "channel_delivery_failed", f"{channel_type} DM target is unavailable"
    conversation_id, thread_id = resolved
    # Chunked at the transport's own declared ceiling, as a mirrored turn is:
    # Discord rejects a message over its cap outright, so an unchunked long
    # report would arrive as a delivery failure instead of a message. At least one
    # unit always comes back, because the route rejects an empty body. One
    # governance decision covers the whole send -- this is a single message the
    # transport happens to split, not the sequence of independent egress actions
    # the mirror backfill re-vets per unit.
    #
    # ``chunk_for_transport``, not ``chunk_text``: a byte-capped channel (Webex)
    # is reachable here, and its char declaration is only the 4x-pessimistic floor
    # a caller that can measure bytes does not need. The same helper the two
    # cross-surface mirror legs use, so one channel cannot be chunked against a
    # unit it does not have.
    #
    # ``display_safe`` is the display-form floor at the egress rather than only at
    # the caller that happens to exist today: this leg passes no renderer, and a
    # renderer is where a turn gets that floor. `api_send_message` already applies
    # it, so on that path this is a second, idempotent application; what it buys is
    # that a future caller of this helper cannot reach a channel without it. The
    # neutral sink rather than a bare redactor pair, because the leg is
    # channel-NEUTRAL and Slack/Discord both parse broadcast-mention grammars.
    units = chunk_for_transport(
        display_safe_for(text, live_transport.capabilities), live_transport.capabilities
    )
    try:
        for unit in units:
            # Fail on the first UNCONFIRMED unit rather than pressing on: the
            # remaining chunks of a message whose head never landed would arrive as
            # an orphaned fragment. `delivery_confirmed` owns which of the two id
            # conventions this transport follows.
            sent = await live_transport.send_message(conversation_id, unit, thread_id=thread_id)
            if not delivery_confirmed(live_transport.capabilities, sent):
                logger.warning(
                    "send_message: %s returned no message id; treating as undelivered",
                    channel_type,
                )
                return False, "channel_delivery_failed", "the channel returned no message id"
    except Exception as exc:
        logger.exception("send_message: %s delivery failed", channel_type)
        return False, "channel_delivery_failed", str(exc)
    return True, "", ""


def _channel_delivery_key(state: DashboardState, caller_session: str, declared_session: str) -> str:
    """The session whose channel conversation a proactive send should reach.

    Two sources, in order, and the request BODY's own idea of who it is talking to
    is not one of them:

    * a **cron** caller (``caller_session`` has already matched
      ``CRON_SESSION_RE``) names its job, and the job's stored ``session_key`` is
      gateway-owned state — so the conversation is chosen by the scheduler rather
      than by whoever posted the request.
    * any other caller is identified by the ``X-Session-Key`` header, which
      ``token_auth._verify_unix_peer`` kernel-attests against the peer's own
      process ancestry on the AF_UNIX socket and denies on mismatch. A body field
      carries no such check, so naming another session's key there would post
      into a conversation the caller does not own.

    Unlike :func:`_resolve_session_target` this returns the job's session key
    VERBATIM. That function wants a dashboard slot name and strips the
    ``dashboard:`` prefix to get one; channel links are keyed by the full session
    key, so stripping it here would lose a dashboard session's outbound mirror.

    Returns ``""`` when neither source answers, which fails the send closed.
    """
    if caller_session.startswith("cron:"):
        cron_id = caller_session.removeprefix("cron:").split(":")[0]
        jobs = state.crons.list_jobs(include_disabled=True)
        job = next((j for j in jobs if j.id == cron_id), None)
        if job is None:
            return ""
        return job.session_key or ""
    return declared_session


async def _deliver_to_channel(
    state: DashboardState, session_key: str, text: str, *, channel_type: str = ""
) -> bool:
    """Governed proactive send to the channel conversation behind *session_key*.

    Rides the same cross-surface ladder as the auto-compact notice and the
    inbound-unbind notice (``chat_runner._resolve_channel_target``) rather than
    reaching for a transport directly, so the send is capability-checked,
    governance-vetted under the ``channels`` scope and SEL-audited exactly like
    every other outbound notice. Slack is not reachable through it by design —
    that transport is not registered in ``state.channel_transports``.

    *channel_type*, when given, is the transport the caller NAMED. The resolved
    link must match it: a session can only have one channel link, so a mismatch
    means the caller asked for a conversation this session does not have, and
    posting to the link it does have would deliver to an audience nobody asked
    for. Empty accepts whatever the link names.

    Fails closed and returns ``False`` — never falls through to another
    destination — for every reason a send can be refused: no link, a link on
    another transport, a governance denial, an unregistered transport, one that
    cannot send proactively, or a transport error. Each is audited, because a
    proactive message that reached nobody is exactly what the caller must not
    read as success.
    """
    # Lazy: chat_runner imports this package at module scope (MAX_PROMPT_BYTES,
    # _find_prompt), so a top-level import here would close the cycle.
    from kiro_crew.dashboard.chat_runner import _resolve_channel_target

    def _audit(outcome: str, reason: str) -> None:
        try:
            _sel().log_tool_invocation(
                session_key=session_key or "dashboard",
                tool_name="send_message",
                outcome=outcome,
                downstream_service=channel_type or "channel",
                resources=f"channel_type={channel_type} reason={reason}",
            )
        except Exception:
            logger.warning("SEL logging failed for channel send", exc_info=True)

    if not session_key or not text:
        _audit("denied", "no_session_key" if not session_key else "empty_text")
        return False
    # Own inbound conversation first, then the outbound mirror: a channel-born
    # session has the former, a dashboard session linked to a channel has the
    # latter, and only one of the two is ever set for a given session.
    link = state.sessions.get_origin_link(session_key) or state.sessions.get_mirror_link(
        session_key
    )
    if link is None:
        _audit("denied", "no_channel_link")
        return False
    if channel_type and link.channel_type != channel_type:
        _audit("denied", f"link_is_{link.channel_type}")
        return False
    try:
        # Off-loop: the ladder's governance gate walks the profile directory,
        # which is unbounded on slow storage.
        target = await asyncio.to_thread(_resolve_channel_target, state, session_key, link)
    except Exception:
        # Includes PlatformCompositionError, which _resolve_channel_target
        # re-raises. Refusing the send is the fail-closed answer either way, and
        # the audit line is what keeps a broken ceiling from reading as a
        # routine skip.
        logger.warning("channel send: target resolution failed for %s", session_key, exc_info=True)
        _audit("error", "resolve_failed")
        return False
    if target is None:
        # Governance denial, no registered transport, or one that cannot send
        # proactively. The ladder logs which; all three are a refusal here.
        _audit("denied", "not_permitted_or_unregistered")
        return False
    resolved, transport = target
    if not resolved.channel_id:
        _audit("denied", "no_conversation_id")
        return False
    # ``display_safe_for`` is the SHARED outbound display sink (redact against the
    # rendered form, then defang mentions ONLY where the platform parses one).
    # Routing through it rather than re-running the two byte-level scanners is what
    # keeps this from becoming a second, differently-sanitised copy of the same
    # egress boundary — and the capability-aware variant rather than the flat
    # ``display_safe`` because this leg is channel-NEUTRAL: Webex reaches it, has no
    # broadcast grammar, and its allow-list IS email addresses, so a blanket defang
    # would insert a ZWSP into every address the agent prints.
    #
    # CHUNKED against the transport's own cap, like the sibling leg above. A
    # transport caps by SLICING (Telegram's `_cap_text` at 4096), so handing it a
    # longer message loses the tail and still answers with a message id -- a
    # delivery this function would then audit as complete. Chunking is what makes
    # the confirmation mean the whole message. The two legs keep separate loops on
    # purpose: this one splits plain text, while the gateway's splits markdown with
    # fence sealing, and collapsing them would silently retune one of the two.
    parts = chunk_text(
        display_safe_for(text, transport.capabilities), transport.capabilities.max_message_chars
    )
    for part in parts:
        try:
            # "No exception" is not delivery on its own, and auditing it as such
            # would report a success for a message the user never saw -- the one
            # outcome this helper's contract exists to prevent.
            # `delivery_confirmed` owns which id convention this transport follows.
            delivered = await transport.send_message(
                resolved.channel_id,
                part,
                thread_id=resolved.thread_id,
            )
        except Exception:
            logger.warning("channel send: delivery failed for %s", session_key, exc_info=True)
            _audit("error", "transport_error")
            return False
        if not delivery_confirmed(transport.capabilities, delivered):
            logger.warning(
                "channel send: %s returned no message id for %s", channel_type, session_key
            )
            _audit("error", "empty_message_id")
            return False
    _audit("completed", "delivered")
    return True


def _coerce_like(value: Any, stored: Any) -> Any:
    """*stored* rendered in *value*'s own type, for a no-op comparison.

    config.json can legitimately hold a ``null`` or a string where a field is a
    bool or an int (a hand-edited file, or a key written before the field gained
    its type), and an untyped ``!=`` against that reports a change on every save —
    which makes ``restart_required`` permanently true and tells the operator to
    restart for nothing. Coercing to the staged value's type is what keeps a
    genuine no-op reading as one. An unrecognised type is returned unchanged, so
    the comparison degrades to the untyped one rather than guessing.
    """
    if isinstance(value, bool):
        return bool(stored)
    if isinstance(value, int):
        try:
            return int(stored or 0)
        except (TypeError, ValueError):
            return stored
    if isinstance(value, str):
        return str(stored or "")
    if isinstance(value, list):
        try:
            return list(stored or [])
        except TypeError:
            # A hand-edited scalar where a list belongs (``"allowed_room_ids": 5``).
            # Returned unchanged so the comparison degrades to the untyped one and
            # reports a change, exactly as the int branch above does — the
            # alternative is `list(5)` raising out of the handler as a 500 that
            # persists nothing, on a request that may not even mention this field.
            return stored
    return stored


async def _send_to_channel_target(
    state: DashboardState,
    channel_type: str,
    target_id: str,
    text: str,
    *,
    caller_session: str = "",
) -> web.Response:  # noqa: C901
    """Deliver *text* to an opaque configured target on a registered transport.

    Four gates, all fail-closed, in the order their evidence is cheapest:

    1. **A registered transport.** Expressed as membership in the registry, never
       as ``channel_type != "slack"``: a negation hands every channel added later
       whatever this path grants, in the permissive direction.
    2. **``supports_proactive_send``.** A channel whose reply is bound to an
       inbound token (WeCom) cannot originate a message at all, and saying so is
       better than a confusing platform error.
    3. **Governance.** The same ``channels``-scope chokepoint the mirror leg uses,
       fail-closed, so a profile that narrows after startup stops sends too.
    4. **The transport's own allow-list**, re-applied by
       ``resolve_configured_target``. The opaque id travelled through the browser
       or the model, and the config may have narrowed since it was minted.

    Every non-2xx body carries a machine-readable ``code``: backend strings have
    no i18n catalog path, so the caller needs something stable to branch on.
    """
    transports = getattr(state, "channel_transports", None) or {}
    transport = transports.get(channel_type)
    if transport is None:
        return web.json_response(
            {"error": f"channel {channel_type} is not connected", "code": "channel_not_connected"},
            status=404,
        )
    if not getattr(transport.capabilities, "supports_proactive_send", False):
        return web.json_response(
            {
                "error": f"channel {channel_type} cannot start a conversation",
                "code": "channel_no_proactive_send",
            },
            status=400,
        )
    # Vet under the CALLER's identity, not the destination's. The ``channels``
    # scope resolves against the surface that ORIGINATED the send, so a cron
    # profile permitting only Slack must deny a Webex target; a key synthesized
    # from ``channel_type`` would resolve the DESTINATION channel's own profile
    # instead, making every per-surface operator binding inert on this leg while
    # the sibling ``_deliver_channel_dm`` honours it. Empty ``caller_session``
    # is a non-cron caller (a browser operator, or a direct call): the host
    # sentinel is what operators bind host-side governance to.
    session_key = caller_session or HOST_SESSION_KEY
    # Offloaded: the governance evaluation stats and reads the profile files (and
    # writes a SEL record either way), which is filesystem latency on the shared
    # gateway loop. The sibling ``_deliver_channel_dm`` already runs its own vet
    # through ``asyncio.to_thread`` for exactly this reason.
    gov = await asyncio.to_thread(_vet_channel_send, channel_type, session_key)
    if gov:
        return web.json_response({"error": gov, "code": "channel_denied"}, status=403)
    resolved = await transport.resolve_configured_target(target_id)
    if resolved is None:
        # Audited like the governance denial above: this is a permission decision
        # on an egress chokepoint, and the caller may be the model. A refusal that
        # leaves no record is the one an operator cannot review — someone probing
        # target ids would look identical to normal traffic.
        _sel().log_api_access(
            caller=session_key,
            operation="channel.send_message",
            outcome="denied",
            source="dashboard",
            resources=f"channel={channel_type} reason=target_not_configured",
        )
        return web.json_response(
            {"error": "target is not configured for this channel", "code": "target_not_allowed"},
            status=403,
        )
    conversation_id, thread_id = resolved
    # The DISPLAY sink, not a byte-level redactor pair. This text can come from
    # the model, and a credential split by markdown delimiters
    # (``AKIA**IOSF**ODNN7EXAMPLE``) survives a byte scan and is reassembled whole
    # by the platform's own renderer. ``display_safe`` canonicalizes to the
    # displayed form before scanning, and defangs broadcast-mention grammars —
    # correct here because this leg is channel-NEUTRAL and Slack/Discord do have
    # them.
    parts = chunk_for_transport(
        display_safe_for(text, transport.capabilities), transport.capabilities
    )
    try:
        for index, part in enumerate(parts):
            # Most transports report a failed send by RETURNING a falsy id rather
            # than raising, so reading only exceptions would answer 200 "ok" for a
            # message that never arrived — worse than an error, because the caller
            # (including the LLM, which cannot see the room) records it as delivered
            # and moves on. But two transports carry no id at all (WeCom's proactive
            # command, Feishu's reply) and raise on failure instead, so there the
            # empty string is the SUCCESS value. ``delivery_confirmed`` owns which
            # convention each transport follows, from its own declared
            # ``returns_message_id`` — the alternative is this leg reporting every
            # delivered message on those two as lost.
            sent = await transport.send_message(conversation_id, part, thread_id=thread_id)
            if not delivery_confirmed(transport.capabilities, sent):
                raise _ChannelSendFailed(f"part {index + 1} of {len(parts)} was not accepted")
    except Exception as exc:
        logger.warning("channel send failed for %s: %s", channel_type, exc, exc_info=True)
        _sel().log_api_access(
            caller=session_key,
            operation="channel.send_message",
            outcome="error",
            source="dashboard",
            resources=f"channel={channel_type} parts={len(parts)}",
        )
        return web.json_response(
            {"error": "delivery failed", "code": "channel_delivery_failed"}, status=502
        )
    _sel().log_api_access(
        caller=session_key,
        operation="channel.send_message",
        outcome="allowed",
        source="dashboard",
        resources=f"channel={channel_type} parts={len(parts)}",
    )
    return web.json_response({"ok": True, "delivered_to": channel_type, "parts": len(parts)})


class _ChannelSendFailed(Exception):
    """A transport declined a part of a channel-addressed send.

    Raised so the falsy-return path and the raising path converge on one handler:
    the endpoint must not answer 200 for a message the channel never accepted.
    """


def _vet_channel_send(channel_type: str, caller_session: str) -> str:
    """Governance for a channel-addressed send; ``""`` when permitted.

    Fail-closed, and audited by ``vet_and_audit`` on both grant and denial: this
    is an egress chokepoint on a network surface, so a degraded governance
    evaluation must DENY rather than degrade to permit.
    """
    try:
        from kiro_crew.platform.governance_profiles import vet_and_audit

        decision = vet_and_audit(
            "channels",
            channel_type,
            session_key=caller_session,
            tool_name="channel.send_message",
            fail_closed=True,
        )
        if not getattr(decision, "permitted", False):
            return f"channel {channel_type} is denied by the active governance profile"
    except Exception:
        logger.warning("channel send governance check failed", exc_info=True)
        return "governance evaluation unavailable"
    return ""


async def api_send_message(request: web.Request) -> web.Response:
    """POST /api/send-message — send a message to a chat surface and/or dashboard.

    ``session`` picks the surface: ``"origin"`` injects into the dashboard session
    that spawned the calling cron, ``"slack"`` adds a Slack DM, and any other
    value names a registered channel transport (``"discord"``) and delivers a DM
    to that channel's configured owner. The Slack-only options are refused, not
    ignored, when combined with a channel session (see ``_SLACK_ONLY_BODY_FIELDS``).
    """
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811
    from kiro_crew.slack.handler import is_allowed_user, is_tracked_channel  # noqa: F811
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    text = body.get("text", "").strip()
    if not text:
        return web.json_response({"error": "text required"}, status=400)
    title = body.get("title", "Agent Message")
    blocks = body.get("blocks")
    if blocks and not isinstance(blocks, list):
        return web.json_response({"error": "blocks must be an array"}, status=400)

    # ── Channel-addressed leg ──
    # Handled before the Slack-shaped validation below, because a Webex room id is
    # not a Slack channel id and must not have to satisfy CHANNEL_ID_RE. The target
    # is an OPAQUE ConfiguredChannelTarget id, never a raw platform conversation
    # id: this endpoint is reachable by the LLM, and re-resolving an opaque id
    # through the transport is what re-applies that channel's own allow-list at the
    # side-effect boundary.
    #
    # ``target_id`` is what selects THIS leg. ``channel_type`` alone means the
    # non-Slack conversation the SESSION already belongs to (``_deliver_to_channel``
    # further down); ``channel_type`` + ``target_id`` names an explicit configured
    # destination on that transport, which is this one. So the field pair reads as
    # "which transport, and — if given — which destination on it", and a
    # ``target_id`` with no transport to resolve it against is the only
    # under-specified combination.
    addressed_channel = str(body.get("channel_type", "") or "").strip().lower()
    target_id = str(body.get("target_id", "") or "").strip()
    if target_id:
        if not addressed_channel:
            return web.json_response(
                {
                    "error": "target_id requires channel_type",
                    "code": "channel_target_incomplete",
                },
                status=400,
            )
        # This leg is the explicit address, so it returns before the Slack-shaped
        # validation below ever runs. Refuse a Slack-only field or a routing
        # ``session`` travelling with it rather than dropping them: the caller
        # (a browser, or the model) cannot observe a drop, and would read a
        # private DM as a threaded post to a named Slack channel. Same posture,
        # and the same ``code`` shape, as the channel-session refusal further
        # down; ``presence`` not truthiness, because ``unfurl_links=False`` is
        # still a Slack option the caller asked for.
        stray = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        stray_session = body.get("session")
        if isinstance(stray_session, str) and stray_session:
            stray.append("session")
        if stray:
            return web.json_response(
                {
                    "error": (
                        "channel_type/target_id addresses the destination directly and "
                        f"cannot be combined with: {', '.join(stray)}"
                    ),
                    "code": "slack_field_with_channel_target",
                },
                status=400,
            )
        # Vet on the CALLER's REAL identity so the fail-closed ``channels`` re-vet
        # inside the leg resolves the caller's own profile rather than a
        # permissive ``HOST_SESSION_KEY`` default. Filtering to ``cron:`` here
        # discarded every non-cron caller's identity, and the MCP-side channel vet
        # fails OPEN on an evaluation error, so a non-cron session whose own
        # profile denies the transport could reach a host-permitted target.
        #
        # The identity is honoured ONLY on the proven-internal transport:
        # ``internal_auth`` is set solely after a constant-time ``X-Internal-Secret``
        # match — the path the MCP gateway and cron use, where ``caller_session``
        # is derived from the verified session key, not a tool arg. This route is
        # on ``_STRICT_INTERNAL_API_PATHS`` today (no browser reaches it), so the
        # gate also guards a future reclassification. Absent (a direct operator
        # send naming no session) degrades to the host sentinel inside the leg.
        addressed_caller = body.get("caller_session", "") if request.get("internal_auth") else ""
        return await _send_to_channel_target(
            state, addressed_channel, target_id, text, caller_session=addressed_caller
        )

    target_channel = body.get("channel", "").strip()
    target_user = body.get("user", "").strip()
    unfurl_links = body.get("unfurl_links")
    unfurl_media = body.get("unfurl_media")
    if (unfurl_links is not None and not isinstance(unfurl_links, bool)) or (
        unfurl_media is not None and not isinstance(unfurl_media, bool)
    ):
        return web.json_response(
            {"error": "unfurl_links and unfurl_media must be booleans"}, status=400
        )

    thread_ts = body.get("thread_ts")
    if thread_ts is not None:
        if not _is_slack_ts(thread_ts):
            return web.json_response(
                {"error": "thread_ts must be a Slack timestamp string like '1712793600.123456'"},
                status=400,
            )
    reply_broadcast = body.get("reply_broadcast")
    if reply_broadcast is not None and not isinstance(reply_broadcast, bool):
        return web.json_response({"error": "reply_broadcast must be a boolean"}, status=400)
    if reply_broadcast and not thread_ts:
        return web.json_response({"error": "reply_broadcast requires thread_ts"}, status=400)

    # Fail fast: mutual exclusion before any redaction/regex work (#4)
    if target_channel and target_user:
        return web.json_response({"error": "specify channel or user, not both"}, status=400)

    # A ``session`` naming a channel transport takes the routing over, so a
    # Slack-only option travelling with it has no destination. Refuse here,
    # before any delivery, rather than posting a message whose thread/layout/
    # audience request was silently discarded.
    raw_session = body.get("session")
    session_name = raw_session if isinstance(raw_session, str) else ""
    channel_target = (
        session_name
        if session_name not in _RESERVED_SESSION_TARGETS and _CHANNEL_TYPE_RE.match(session_name)
        else ""
    )
    if channel_target:
        slack_only = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        if slack_only:
            return web.json_response(
                {
                    "error": (
                        f"session '{channel_target}' does not accept the Slack-only "
                        f"field(s): {', '.join(slack_only)}"
                    ),
                    "code": "slack_only_field_with_channel_session",
                },
                status=400,
            )

    channel_type = body.get("channel_type") or ""
    if not isinstance(channel_type, str):
        return web.json_response(
            {"error": "channel_type must be a string", "code": "channel_type_not_a_string"},
            status=400,
        )
    channel_type = channel_type.strip()
    if channel_type:
        # Refused, never resolved by precedence: with two destinations named,
        # either order silently drops one and the caller cannot tell which. The
        # field list is the shared ``_SLACK_ONLY_BODY_FIELDS`` rather than a
        # hand-rolled three, so a Slack option added there is refused here too.
        conflicts = [f for f in _SLACK_ONLY_BODY_FIELDS if body.get(f) is not None]
        if session_name == SLACK_NAMESPACE:
            conflicts.append('session="slack"')
        if conflicts:
            return web.json_response(
                {
                    "error": (
                        f"channel_type cannot be combined with {', '.join(conflicts)} — "
                        "those route to Slack only"
                    ),
                    "code": "channel_type_conflicts_slack_routing",
                },
                status=400,
            )
        if channel_type == SLACK_NAMESPACE:
            return web.json_response(
                {
                    "error": 'channel_type "slack" is not supported — use session="slack"',
                    "code": "channel_type_slack_unsupported",
                },
                status=400,
            )
        if channel_type not in _SEND_MESSAGE_CHANNEL_TYPES:
            return web.json_response(
                {
                    "error": (
                        f"unknown channel_type {channel_type!r} — expected one of "
                        f"{', '.join(sorted(_SEND_MESSAGE_CHANNEL_TYPES))}"
                    ),
                    "code": "channel_type_unknown",
                },
                status=400,
            )
    # Two DESTINATIONS named, so it is refused rather than resolved by branch
    # order: whichever won, the caller would be told the send succeeded to a place
    # they did not ask for. ``session="slack"`` is caught by the conflicts list
    # above for the same reason.
    #
    # ``session="origin"`` is deliberately NOT caught here, and is not a third
    # destination: it is a MODE meaning "inject where this came from", which is why
    # it sits in ``_RESERVED_SESSION_TARGETS`` rather than resolving to a channel.
    # Combined with channel_type it is the fallback ladder the cron path needs -- a
    # job whose origin slot has died still reaches its user on the channel -- and
    # the response reports ``delivered_to`` as the surface that actually took the
    # message, so a caller is never told the channel received something it did not.
    if channel_type and channel_target:
        return web.json_response(
            {
                "error": (
                    f"channel_type '{channel_type}' cannot be combined with "
                    f"session '{channel_target}' — each names a different destination"
                ),
                "code": "channel_type_conflicts_channel_session",
            },
            status=400,
        )

    # Validate format first, then redact (#2)
    if target_channel and not CHANNEL_ID_RE.match(target_channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    if target_user and not USER_ID_RE.match(target_user):
        return web.json_response({"error": "invalid user ID format"}, status=400)

    # Redact after format validation
    if target_channel:
        target_channel, _ = redact_exfiltration_urls(target_channel)
        target_channel, _ = redact_credentials(target_channel)
    if target_user:
        target_user, _ = redact_exfiltration_urls(target_user)
        target_user, _ = redact_credentials(target_user)

    # Sanitize LLM-generated content before any external surface.
    # This covers all downstream paths (session injection, fallback, Slack,
    # and every channel transport), which is why the DISPLAY-form floor belongs
    # here rather than at each delivery site: a proactive body is posted as-is by
    # the channel legs below, so the literal-form scan alone let a
    # markdown-collapse credential (`AKIA**IOSFODNN7EXAMPLE**`, which the client
    # renders whole) through on a path that never passes a renderer -- the one
    # place every turn egress applies this floor.
    text, _ = redact_for_display(text, _redact_all)
    title, _ = redact_for_display(title, _redact_all)
    if blocks:
        blocks = _sanitize_blocks(blocks, redact_exfiltration_urls, redact_credentials)

    # render [OPTIONS: ...] tags as interactive buttons on the
    # plain-text path (when the caller did not supply explicit blocks — those
    # own their own layout). Strip the tag from the text used for both the
    # dashboard notification and the Slack post; an actions block is appended
    # after the message when options are present.
    options: list[str] = []
    if not blocks:
        text, options = extract_options(text)

    # --- Authorization gates (before any side effects) ---
    if target_channel and not is_tracked_channel(target_channel):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="send_message",
            outcome="denied",
            downstream_service="slack",
            resources=f"target_channel={target_channel}",
        )
        return web.json_response(
            {
                "error": f"channel {target_channel} not in tracked channels. "
                "Add it to config.json: "
                f'{{"slack": {{"tracking_channels": [{{"channel_id": "{target_channel}"}}]}}}}. '
                "Then restart the gateway."
            },
            status=403,
        )

    if target_user and not is_allowed_user(target_user):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="send_message",
            outcome="denied",
            downstream_service="slack",
            resources=f"target_user={target_user}",
        )
        return web.json_response(
            {
                "error": "user not in allowlist — configure allowed_users in config.json",
                "code": "user_not_in_allowlist",
            },
            status=403,
        )

    sent_slack = False
    slack_ts: str | None = None
    sent_session = False
    # A channel target is not a session-injection target: _resolve_session_target
    # accepts only "origin", and the delivery below keys off channel_target.
    target_session = "" if channel_target else session_name
    job_name = None
    slack_attempted = False
    slack_error = ""
    sent_channel = False
    channel_code = ""
    channel_detail = ""
    try:
        # ───────────────────────────────────────────────────────────────────
        # send_message delivery contract
        # ───────────────────────────────────────────────────────────────────
        # For cron jobs, the intended behavior is:
        #
        #   1. Try the origin dashboard session first (the chat that created
        #      this cron). Inject the message there so the session agent can
        #      react to it (not just display it). When injection succeeds,
        #      the message appears in the chat UI directly — no extra bell
        #      notification needed.
        #   2. Fall through to owner Slack DM if origin is unreachable.
        #   3. Dashboard notification (bell icon + notifications.jsonl) fires
        #      ONLY on the fallback path, so no-Slack setups still surface
        #      messages that couldn't reach their origin. The invariant is
        #      "never silently dropped", not "always notified".
        #
        # "Origin reachable" = one of:
        #   - Hot: slot in state._slots (user has the tab open) → fast path
        #   - Cold: slot not loaded but JSONL exists without closed=true →
        #     rehydrate_slot_from_history_async restores it from disk, tab reappears
        #
        # "Origin unreachable" = any of:
        #   - User clicked ✕ on the tab (closed=true in JSONL metadata) —
        #     respect the close, do NOT resurrect the tab
        #   - JSONL file deleted entirely (history.delete_session)
        #   - Cron created from dashboard UI without an originating chat
        #     (job.session_key is empty — api_crons_create never sets it)
        #   - Cron's caller_session doesn't match any known job
        #
        # session param values (enforced by _resolve_session_target):
        #   - "origin": route to originating dashboard session
        #   - "slack":  Slack DM + notification
        #   - omitted:  dashboard notification only (default)
        # ───────────────────────────────────────────────────────────────────
        # B: cron-originated sends deliver to the owner Slack DM by default —
        # the documented "cron → Slack DM + dashboard" behavior — even on a
        # bare send with no explicit session/channel/user. For session=origin
        # this only takes effect as the fallback when the origin slot is
        # unreachable (see the contract above). Non-cron bare sends remain
        # dashboard-notification-only.
        caller_session = body.get("caller_session", "")
        # The header, not the body, is what identifies a non-cron caller to the
        # channel path: token_auth kernel-attests it against the AF_UNIX peer's
        # own process ancestry. See _channel_delivery_key.
        declared_session = request.headers.get("X-Session-Key", "")
        # Validate the cron session format before trusting it to escalate
        # routing from notification-only to owner Slack DM — a malformed or
        # injected value must not abuse that upgrade.
        is_cron_caller = bool(CRON_SESSION_RE.match(caller_session))
        # A channel session takes the routing over, INCLUDING the cron default:
        # a cron asking for a Discord DM did not ask for a Slack one as well.
        send_to_slack = not channel_target and (
            target_session == "slack" or bool(target_channel) or bool(target_user) or is_cron_caller
        )
        # A channel_type send names ONE destination, so Slack is not its
        # fallback: a failed channel delivery falling through to the owner DM
        # would post the message to an audience the caller never named, and the
        # 502 below is what tells the caller nothing was delivered. This is also
        # why the MCP tool vets ONLY channel_type's transport under the
        # ``channels`` scope — Slack is not a destination of such a call.
        if channel_type:
            send_to_slack = False
        if target_session == "slack":
            target_session = ""
        if target_session:
            slot_key, job_name = _resolve_session_target(state, target_session, caller_session)
            if slot_key:
                # Resolve the origin slot. get_slot is the hot path (fast,
                # O(1) dict lookup). On miss, rehydrate_slot_from_history_async
                # restores from disk if the session exists and isn't closed,
                # with the transcript read on a worker thread so a large store
                # does not stall the loop for every other request.
                # Truly-gone sessions (never persisted, deleted, or closed)
                # return None and delivery falls through to the Slack DM
                # path below — no phantom empty tab is ever created.
                slot = state.get_slot(slot_key)
                was_loaded = slot is not None
                if slot is None:
                    slot = await rehydrate_slot_from_history_async(state, slot_key)
                logger.info(
                    "send_message session=origin resolved slot_key=%s job=%s was_loaded=%s rehydrated=%s",
                    slot_key,
                    job_name,
                    was_loaded,
                    (slot is not None and not was_loaded),
                )
                if slot:
                    label = job_name or "cron"
                    label, _ = redact_exfiltration_urls(label)
                    label, _ = redact_credentials(label)
                    # text and title already redacted above (L2538-2542)
                    # Text wrapper kept for LLM context and queue detection;
                    # cronLabel in cls JSON provides structured data for frontend.
                    wrapped = f'{CRON_NOTIFY_PREFIX}"{label}"]\n{text}\n{CRON_NOTIFY_END}'
                    inject_cls = json.dumps({"cronLabel": label})
                    # Queue while a turn is live OR a multi-stage plan is mid-flight.
                    # During stage execution slot.task is None between stages (see
                    # chat_orchestrator), so slot.running alone reads False in that
                    # window and would let this injection start a concurrent turn that
                    # clobbers the plan. _in_stage_execution closes it — same predicate
                    # the user-typed path uses (chat_handlers._api_chat).
                    if slot.running or slot._in_stage_execution:
                        if len(slot._queue) >= 50:
                            evicted = slot.queue_pop(0)
                            logger.warning(
                                "Queue full for slot %s — evicting oldest message", slot_key
                            )
                            _remove_queued_by_id(slot.messages, evicted["id"])
                        qid = slot.queue_append(wrapped, kind=CRON_NOTIFICATION_KIND)
                        _cls = json.loads(inject_cls)
                        _cls["queue_id"] = qid
                        slot.append("queued", wrapped, json.dumps(_cls))
                        state.push_slots_update()
                    else:
                        # circular import: chat_runner imports from
                        # kiro_crew.dashboard.handlers (for MAX_PROMPT_BYTES,
                        # _find_prompt, _list_aim_prompts), so we can't import
                        # it at module top-level without a cycle.
                        from kiro_crew.dashboard.chat_runner import _run_chat
                        from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn

                        # `cls` is not persisted for role `inject`, so the label
                        # must also ride in `meta`, which is — otherwise the row
                        # loses its identity on the next rehydrate.
                        slot.append(
                            "inject",
                            wrapped,
                            inject_cls,
                            meta={"injectKind": "cron", "cronLabel": label},
                        )
                        task = spawn_guarded_turn(
                            state,
                            slot,
                            _run_chat(
                                state,
                                slot,
                                wrapped,
                                _directive_user_origin=False,
                            ),
                        )
                        slot.task = task
                        state.push_slots_update()
                    sent_session = True
        # Fall back to normal delivery if no session target or session is gone
        if not sent_session:
            # Snapshot before the suffix below: that sentence describes the BELL's
            # delivery, and a channel post is a real delivery, not the
            # notification fallback it announces. A channel-born cron reaches
            # here with job_name set on every run (its job.session_key names a
            # channel session, never a dashboard slot), so this is the normal
            # path for one, not an edge case.
            channel_text = text
            if target_session and job_name:
                safe_name, _ = redact_exfiltration_urls(job_name)
                safe_name, _ = redact_credentials(safe_name)
                title = f"⏰ {safe_name}"
                text += "\n\n_(session closed — delivered as notification)_"
            state.notify("agent", title, text)
            # No widget on either channel path, so a parsed [OPTIONS:] trailer is
            # re-attached as a numbered list rather than dropped: the user still
            # learns the choices exist and can answer by typing one. Built from the
            # snapshot, so the notification-fallback sentence never rides along, and
            # hoisted above both legs so neither mechanism drops it.
            if options:
                channel_text = f"{channel_text}\n\n{format_overflow(options, 0)}"
            if channel_target:
                sent_channel, channel_code, channel_detail = await _deliver_channel_dm(
                    state,
                    channel_target,
                    channel_text,
                    # Vet on the CALLER's real identity so the fail-closed
                    # ``channels`` re-vet inside the leg resolves that caller's own
                    # profile rather than the permissive ``HOST_SESSION_KEY``
                    # default. Filtering to ``cron:`` here discarded every non-cron
                    # caller's identity, and the MCP-side channel vet fails OPEN on
                    # an evaluation error, so a non-cron session whose own profile
                    # denies the transport could reach a host-permitted target --
                    # the same hole the ``channel_type``/``target_id`` leg above
                    # already closed, on the leg that was not migrated with it.
                    #
                    # The two arms take their identity from where each is trustworthy,
                    # matching ``_channel_delivery_key`` on this same path: a cron's
                    # body key is format-validated above before it may escalate
                    # routing, and a non-cron caller is identified by the
                    # ``X-Session-Key`` header, which ``token_auth._verify_unix_peer``
                    # kernel-attests against the peer's own process ancestry -- never
                    # the body, which a tool arg could name. So the governance vet and
                    # the delivery key resolve the SAME principal. Absent (a direct
                    # operator send naming no session) still degrades to the host
                    # sentinel inside the leg, unchanged.
                    caller_session=caller_session if is_cron_caller else declared_session,
                )
            if channel_type:
                sent_channel = await _deliver_to_channel(
                    state,
                    _channel_delivery_key(state, caller_session, declared_session),
                    channel_text,
                    channel_type=channel_type,
                )
            # A separate ``if``, not an ``elif``: ``send_to_slack`` is the single
            # predicate that decides Slack delivery, so it must be false when a
            # channel session took the routing over rather than merely
            # unreachable behind another branch.
            if send_to_slack and state.slack_client:
                try:
                    if target_channel:
                        channel = target_channel
                    elif target_user:
                        channel = await state.slack_client.open_dm(target_user)
                    elif state.owner_id:
                        channel = await state.slack_client.open_dm(state.owner_id)
                    else:
                        channel = ""

                    if channel:
                        slack_attempted = True
                        if blocks:
                            slack_ts = await state.slack_client.post_blocks(
                                channel,
                                blocks,
                                text,
                                thread_ts=thread_ts,
                                unfurl_links=unfurl_links,
                                unfurl_media=unfurl_media,
                                reply_broadcast=reply_broadcast,
                            )
                        else:
                            slack_ts = await state.slack_client.post_message(
                                channel,
                                text,
                                thread_ts=thread_ts,
                                unfurl_links=unfurl_links,
                                unfurl_media=unfurl_media,
                                reply_broadcast=reply_broadcast,
                            )
                            if options:
                                try:
                                    # Asker is the thread's owner: an out-of-band
                                    # post has no running session of its own, so
                                    # the conversation that receives the reply is
                                    # the right subject.
                                    _sm_o = slack_options_owner_key(state, thread_ts or "")
                                    _sm_t = (
                                        await asyncio.to_thread(mint_options_token, state, _sm_o)
                                        if _sm_o
                                        else None
                                    )
                                    option_blocks = build_options_blocks(
                                        options, staleness_token=_sm_t
                                    )
                                    # Fallback text is the SAFE stub, not the
                                    # message body. Slack parses entities in a
                                    # message's top-level `text` -- which is what
                                    # notifications render -- so an agent-authored
                                    # body containing `<!channel>` would ping the
                                    # whole channel, and the expiry would ping it
                                    # AGAIN every time it replays this text on its
                                    # edit. Nothing is lost: the body was already
                                    # posted as its own message just above, so here
                                    # it was pure duplication. This is the same stub
                                    # the other three posting paths use.
                                    option_ts = await state.slack_client.post_blocks(
                                        channel,
                                        option_blocks,
                                        OPTIONS_FALLBACK_TEXT,
                                        thread_ts=thread_ts,
                                    )
                                    # A thread IS a conversation, so bind the
                                    # control to whichever session owns that
                                    # thread — a dashboard session mirroring into
                                    # it, or the Slack-born one. Without a thread
                                    # there is no conversation to supersede it, so
                                    # nothing is recorded.
                                    if thread_ts and option_ts:
                                        remember_slack_options(
                                            state,
                                            slack_options_owner_key(state, str(thread_ts)),
                                            PostedOptions(
                                                channel=channel,
                                                ts=option_ts,
                                                choices=tuple(options),
                                                blocks=tuple(option_blocks),
                                            ),
                                        )
                                except Exception:
                                    logger.debug(
                                        "send_message: failed to post OPTIONS blocks",
                                        exc_info=True,
                                    )
                        sent_slack = True
                except Exception as exc:
                    slack_attempted = True
                    slack_error = str(exc)
                    logger.exception("send_message: Slack delivery failed")
    finally:
        try:
            thread_hint = " threaded=1" if thread_ts else ""
            if reply_broadcast:
                thread_hint += " broadcast=1"
            if target_channel or target_user:
                base_res = f"target_channel={target_channel} target_user={target_user}"
            elif sent_session:
                base_res = "session=origin"
            elif channel_target or channel_type:
                base_res = f"channel_type={channel_target or channel_type}"
            else:
                base_res = "fallback=owner_dm"
            if sent_session:
                downstream_service = "session"
            elif sent_channel:
                # The channel type itself, so a reader of the audit learns WHICH
                # surface took the message and a channel added later needs no new
                # vocabulary here.
                downstream_service = channel_target or channel_type
            elif sent_slack:
                downstream_service = "slack"
            else:
                downstream_service = "dashboard"
            # A refused or failed channel delivery is an error for the same reason a
            # failed Slack post is: the caller asked for that surface. channel_code is
            # empty when the channel was merely absent, which is the documented
            # degradation to a notification rather than a failure. The channel_type leg
            # has no soft miss, so any non-delivery is its 502 -- but a satisfied
            # session injection is not one, which is the same condition that guard
            # uses, so this row and the HTTP status cannot disagree.
            failed = (
                (slack_attempted and not sent_slack)
                or bool(channel_code)
                or bool(channel_type and not sent_channel and not sent_session)
            )
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="send_message",
                outcome="error" if failed else "completed",
                downstream_service=downstream_service,
                resources=base_res + thread_hint,
                error=channel_code,
            )
        except Exception:
            logger.warning("SEL logging failed for send_message", exc_info=True)
    if channel_code:
        safe_detail, _ = redact_credentials(channel_detail)
        safe_detail, _ = redact_exfiltration_urls(safe_detail)
        detail = f"{channel_target} delivery failed: {safe_detail}"
        # Both responses are spelled out inline, with a literal status and a
        # literal body, rather than sharing a hoisted dict or computing the
        # status. `test_error_code_contract` ratchets BOTH of those shapes for the
        # same reason: a computed status and a body a static scan cannot read are
        # each a way to slip an uncoded error response past the gate, so the one
        # form that stays checkable is the verbose one. A governance refusal is
        # the caller's own permission problem; anything else is downstream.
        if channel_code == "channel_not_permitted":
            return web.json_response(
                {"ok": False, "error": detail, "code": channel_code}, status=403
            )
        return web.json_response({"ok": False, "error": detail, "code": channel_code}, status=502)
    if slack_attempted and not sent_slack:
        safe_error, _ = redact_credentials(slack_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response(
            {"ok": False, "error": f"Slack delivery failed: {safe_error}", "slack": False},
            status=502,
        )
    # A named channel that was not reached is a failure, not a notification-only
    # success: the caller asked for a specific conversation, Slack was suppressed
    # as its fallback, and the bell is not a substitute for the surface the user
    # is actually reading. _deliver_to_channel has already audited which of the
    # refusals it was.
    if channel_type and not sent_channel and not sent_session:
        return web.json_response(
            {
                "ok": False,
                "error": (
                    f"channel delivery to {channel_type} failed — the message was "
                    "not posted to the conversation"
                ),
                "code": "channel_delivery_failed",
            },
            status=502,
        )
    # Report the actual delivery channel so callers (and the read-back
    # steering) can distinguish a real Slack post from a notification-only
    # send; "ok: true" alone masks that difference.
    if sent_session:
        delivered_to = "session"
    elif sent_channel:
        # The channel type itself, so a caller reads WHICH surface took the message
        # and a channel added later needs no new vocabulary here.
        delivered_to = channel_target or channel_type
    elif sent_slack:
        delivered_to = "slack"
    else:
        delivered_to = "notification"
    resp_body: dict[str, Any] = {
        "ok": True,
        "slack": sent_slack,
        "session": sent_session,
        "delivered_to": delivered_to,
    }
    if slack_ts:
        resp_body["ts"] = slack_ts
    return web.json_response(resp_body)


async def api_slack_pins(request: web.Request) -> web.Response:
    """POST /api/slack/pins — pin/unpin/list pins on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. The gateway
    holds the token in ``state.slack_client``; this route enforces the same
    tracked-channel allowlist and SEL audit logging as the other Slack routes.

    Body: {"channel": "C...", "action": "add"|"remove"|"list", "ts": "..."}
    (``ts`` required for add/remove, ignored for list).
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("add", "remove", "list"):
        return web.json_response({"error": "action must be 'add', 'remove', or 'list'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)

    ts = body.get("ts", "")
    if action in ("add", "remove"):
        if not _is_slack_ts(ts):
            return web.json_response(
                {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
                status=400,
            )

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        result: dict[str, Any] = {"ok": True}
        if action == "add":
            await slack.add_pin(channel, ts)
        elif action == "remove":
            await slack.remove_pin(channel, ts)
        else:
            # Pinned messages may contain content originally posted by
            # LLM-controlled agents; redact each text field before returning
            # it to the caller (same output contract as send_message).
            pins = await slack.list_pins(channel)
            for pin in pins:
                safe_text, _ = redact_credentials(pin.get("text", ""))
                safe_text, _ = redact_exfiltration_urls(safe_text)
                pin["text"] = safe_text
            result["pins"] = pins
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(result)
    except Exception as e:
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_pins",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


async def api_slack_reactions(request: web.Request) -> web.Response:
    """POST /api/slack/reactions — add/remove an emoji reaction on a tracked channel.

    Server-side proxy so callers never need the Slack bot token. Mirrors the
    pins route: tracked-channel allowlist + SEL audit + server-held token.

    Body: {"channel": "C...", "ts": "...", "emoji": "white_check_mark",
           "action": "add"|"remove"}
    """
    # circular import: slack.handler imports from dashboard.* at module load
    from kiro_crew.slack.handler import is_tracked_channel  # noqa: F811

    state: DashboardState = request.app["state"]
    slack = state.slack_client
    if not slack:
        return web.json_response({"ok": True, "skipped": "no_slack"})
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    action = body.get("action", "")
    if action not in ("add", "remove"):
        return web.json_response({"error": "action must be 'add' or 'remove'"}, status=400)
    channel = body.get("channel", "")
    if not isinstance(channel, str):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    channel = channel.strip()
    if not channel or len(channel) > CHANNEL_MAX_LEN or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    ts = body.get("ts", "")
    if not _is_slack_ts(ts):
        return web.json_response(
            {"error": "ts must be a Slack timestamp string like '1712793600.123456'"},
            status=400,
        )
    emoji = body.get("emoji", "")
    if not isinstance(emoji, str):
        return web.json_response({"error": "invalid emoji name"}, status=400)
    emoji = emoji.strip()
    if not emoji or not _EMOJI_NAME_RE.match(emoji):
        return web.json_response({"error": "invalid emoji name"}, status=400)

    if not is_tracked_channel(channel):
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="denied",
            downstream_service="slack",
            resources=f"channel={channel} action={action}",
        )
        return web.json_response(
            {"error": f"channel {channel} not in tracked channels"}, status=403
        )

    try:
        if action == "add":
            await slack.add_reaction(channel, ts, emoji, raise_on_error=True)
        else:
            await slack.remove_reaction(channel, ts, emoji, raise_on_error=True)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="completed",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
        )
        return web.json_response({"ok": True})
    except Exception as e:
        safe_error, _ = redact_credentials(str(e))
        safe_error, _ = redact_exfiltration_urls(safe_error)
        _sel().log_tool_invocation(
            session_key="api",
            source="api",
            tool_name="slack_reactions",
            tool_kind="slack",
            outcome="error",
            downstream_service="slack",
            resources=f"channel={channel} action={action} emoji={emoji}",
            error=safe_error,
        )
        return web.json_response({"error": safe_error}, status=502)


async def api_delete_message(request: web.Request) -> web.Response:
    """POST /api/delete-message — delete a bot-authored Slack message."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    channel = body.get("channel", "").strip()
    ts = body.get("ts", "").strip()
    if not channel or not ts:
        return web.json_response({"error": "channel and ts required"}, status=400)
    slack = state.slack_client
    if not slack:
        return web.json_response({"error": "Slack not connected"}, status=503)
    try:
        await slack.delete_message(channel, ts)
    except Exception as e:
        safe_error = str(e).split("\n")[0][:200]
        safe_error, _ = redact_credentials(safe_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response({"error": f"Delete failed: {safe_error}"}, status=502)
    return web.json_response({"ok": True})


def _missing_scope_message(needed: str) -> str:
    """Build an actionable missing_scope message, naming the scope(s) when known."""
    # Slack's ``needed`` field may name several comma-separated scopes.
    scopes = [s.strip() for s in needed.split(",") if s.strip()] if needed else []
    if scopes:
        joined = ", ".join(scopes)
        noun = "OAuth scope" if len(scopes) == 1 else "OAuth scopes"
        scope_clause = f"the {joined} {noun}"
        add_clause = f"add {joined} to"
    else:
        scope_clause = "an OAuth scope"
        add_clause = "add the required scope to"
    return (
        f"This Slack action requires {scope_clause}, which is not granted to this app. "
        "Reinstall the app after granting the required permissions in the Slack Dashboard. "
        f"Alternatively, {add_clause} the app manifest and recreate the app by following "
        "the steps in docs/guides/slack-setup.md."
    )


async def api_slack_profile(request: web.Request) -> web.Response:
    """POST /api/slack-profile — read a Slack user's profile."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls  # noqa: F811
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    raw_user = body.get("user", "")
    if not isinstance(raw_user, str):
        return web.json_response({"error": "user must be a string"}, status=400)
    user_id = raw_user.strip()
    if not user_id:
        return web.json_response({"error": "user required"}, status=400)
    # Validate format first, then redact (#2)
    if not USER_ID_RE.match(user_id):
        return web.json_response({"error": "invalid user ID format"}, status=400)
    user_id, _ = redact_exfiltration_urls(user_id)
    user_id, _ = redact_credentials(user_id)

    # Authorization first (deny-by-default) — reject before any side effects
    from kiro_crew.slack.handler import is_allowed_user  # noqa: F811

    if not is_allowed_user(user_id):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "user not in allowlist"}, status=403)

    if not state.slack_client:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id} reason=slack_not_connected",
        )
        return web.json_response({"error": "Slack not connected"}, status=503)

    # Rate limiting: max 5 profile lookups per minute (#5)
    # Only counts authorized requests — unauthorized 403s don't consume slots
    now = time.monotonic()
    history: list[float] = getattr(state, "_profile_lookup_times", [])
    history = [t for t in history if now - t < 60]
    if len(history) >= 5:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="denied",
            downstream_service="slack",
            resources=f"user={user_id} reason=rate_limit",
        )
        return web.json_response(
            {"error": "rate limit exceeded — max 5 profile lookups per minute"}, status=429
        )
    history.append(now)
    state._profile_lookup_times = history  # type: ignore[attr-defined]

    try:
        profile = await state.slack_client.get_user_profile(user_id)
    except Exception as exc:
        from slack_sdk.errors import SlackApiError  # noqa: F811

        if isinstance(exc, SlackApiError):
            response = exc.response  # type: ignore[attr-defined]
            slack_error = str(response.get("error", "") or "") if response else ""
            if slack_error == "missing_scope":
                needed = str(response.get("needed", "") or "") if response else ""
                logger.warning(
                    "slack-profile: missing_scope (needed=%s) for %s", needed or "?", user_id
                )
                _sel().log_tool_invocation(
                    session_key="dashboard",
                    tool_name="read_slack_profile",
                    outcome="error",
                    downstream_service="slack",
                    resources=f"user={user_id} reason=missing_scope needed={needed}",
                )
                needed, _ = redact_credentials(needed)
                needed, _ = redact_exfiltration_urls(needed)
                return web.json_response({"error": _missing_scope_message(needed)}, status=403)
        logger.exception("slack-profile: failed for %s", user_id)
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="read_slack_profile",
            outcome="error",
            downstream_service="slack",
            resources=f"user={user_id}",
        )
        return web.json_response({"error": "Slack API error"}, status=502)

    # Redact free-form profile fields that could contain prompt-injection
    for key in list(profile):
        val = profile[key]
        if isinstance(val, str) and key not in ("id",):
            val, _ = redact_exfiltration_urls(val)
            val, _ = redact_credentials(val)
            profile[key] = val

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="read_slack_profile",
        outcome="completed",
        downstream_service="slack",
        resources=f"user={user_id}",
    )
    return web.json_response({"profile": profile})


def _deny_non_owner_browser_request(request: web.Request, operation: str) -> web.Response | None:
    """Require the dashboard owner on browser MUTATION endpoints. 403 or None.

    The caller must be the configured owner (``is_owner_dashboard_request``):
    app tokens, non-owner dashboard users, and callers with no identity are all
    refused. This is the same predicate used for MCP-app calls, source-provider
    mutations, and agent-question endpoints — one definition of "owner" across
    the dashboard.

    The mutations guarded here are security-sensitive:

    * installing the CLI globally adds an executable host capability, so a
      non-owner must not be able to mutate the machine to provide it;
    * the attach token is a stored credential that silences the browser's own
      per-attach approval prompt — the last human checkpoint before a program
      drives the user's logged-in session;
    * a browser download writes to the machine;
    * the view endpoints return an unauthenticated dashboard URL, i.e. control
      of a logged-in browser.

    Reads (``api_browser_install_get``) stay open: knowing whether browsing
    exists is not a capability. Mirrors the ComputerUse keystone precedent.
    """
    # Deferred import: source_providers imports chat state helpers from this
    # module's sibling, so a top-level import would close a cycle.
    from kiro_crew.dashboard.handlers._shared import _owner_denial_response
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
    )

    if is_owner_dashboard_request(request):
        # A permission DECISION is audited whichever way it goes. Recording only
        # refusals leaves the log unable to answer "who armed browsing on this
        # host", which is the question an investigation actually asks: the
        # damaging path here is an ALLOWED install or token write, not a blocked
        # one.
        _sel().log_api_access(
            caller=str(request.get("user") or "owner"),
            operation=operation,
            outcome="allowed",
            source="browser_api",
            resources=request.path,
        )
        return None
    # Derive a meaningful caller identity for the SEL record: app tokens
    # audit as "app:<name>"; dashboard users audit as their subject; callers
    # with no identity audit as "anonymous".
    app_name = request.get("app", "")
    if app_name:
        caller = f"app:{app_name}"
    else:
        caller = str(request.get("user") or "anonymous")
    # Permission denial on a security boundary — audited before the response
    # (backend-security-controls: every denial emits SEL).
    _sel().log_api_access(
        caller=caller,
        operation=operation,
        outcome="denied",
        source="browser_api",
        resources=request.path,
        error="browser mutations require the dashboard owner",
    )
    # Deny decision made above; only the response label changes for a signed
    # pre-owner bootstrap subject (see stale_owner_session_response).
    return _owner_denial_response(request, "dashboard user required", "dashboard_user_required")


async def api_browser_token_put(request: web.Request) -> web.Response:
    """PUT /api/browser/token -- set or clear the optional attach token.

    The response reports only WHETHER a token is set. A value that exists to reach a
    child process's environment has no reason to travel back out, and echoing it
    would put it in dashboard traffic and browser memory for no gain.

    Re-publishes the environment on the way out: a child reads the environment it
    was handed, so a token written without this would not reach any shell until the
    next gateway start.
    """
    denied = _deny_non_owner_browser_request(request, "browser_token_set")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    # Valid-but-non-object JSON ([], null, "str") would AttributeError on
    # body.get below -- an unintended 500 instead of a validation 400. Same
    # guard, same reason, as the other body-reading handlers in this file.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    value = body.get("token")
    if not isinstance(value, str):
        return web.json_response({"error": "token must be a string"}, status=400)
    await asyncio.to_thread(browser_cli_token.set_token, value)
    # Re-project onto the gateway's own environment, and DELETE the key when the
    # token was cleared: the agent inherits this env, so writing the file alone
    # would leave a revoked token live for every later invocation.
    #
    # Residual, stated rather than implied: a child process that ALREADY started
    # holds the value it inherited at spawn time, so an agent session running
    # right now keeps using the old token until it restarts. Revoking that would
    # mean killing live sessions on a settings write, which is a worse trade than
    # the window it closes.
    if browser_cli_token.has_token():
        os.environ.update(browser_cli_token.cli_env_overrides())
    else:
        os.environ.pop(browser_cli_token.TOKEN_ENV, None)
    return web.json_response({"ok": True, "token": browser_cli_token.has_token()})


async def api_browser_command(request: web.Request) -> web.Response:
    """POST /api/browser/command -- run one op against the native browser panel.

    Called by the ``browser`` MCP tool. Body:
    ``{"op": str, "session_key": str, "args"?: object, "timeout_ms"?: int}``.
    ``session_key`` is the BARE slot key the Electron panel registers under (the
    tool resolves and namespace-strips it; the ``dashboard:``-namespaced form
    travels in the ``X-Session-Key`` header for the peer check). Enqueues the op
    on the command bus and awaits the native panel's result.

    Responses:
    - 200 ``{"id", "ok": true, "result": <any>}`` -- op ran and succeeded;
    - 200 ``{"id", "ok": false, "error": str}`` -- op ran but failed;
    - 503 ``{"code": "no_native_panel"}`` -- no Electron poller for this session,
      returned FAST so the tool falls back to playwright-cli;
    - 429 ``{"code": "queue_full"}`` / 504 ``{"code": "timeout"}``.

    Proven ``X-Internal-Secret`` only (``request["internal_auth"] is True``),
    mirroring ``api_computer_use_invoke``: the middleware sets that flag only on
    the validated-secret path, so a dashboard COOKIE caller -- even a
    browser-credentialed page on a ``local_only=False`` deployment where strict
    paths reclassify as mixed -- is rejected here. We do NOT additionally require
    a loopback ``request.remote``: the tool reaches the gateway over the AF_UNIX
    internal-API socket (0700 data home + kernel peer check), where
    ``request.remote`` is empty, so a loopback re-assert would 403 every op.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    op = body.get("op")
    args = body.get("args")
    timeout_ms = body.get("timeout_ms")
    session_key = body.get("session_key")
    if not isinstance(op, str) or not op:
        return web.json_response({"error": "op required", "code": "op_required"}, status=400)
    if args is not None and not isinstance(args, dict):
        return web.json_response(
            {"error": "args must be an object", "code": "args_must_be_object"}, status=400
        )
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        timeout_ms = DEFAULT_COMMAND_TIMEOUT_MS
    if not isinstance(session_key, str) or not session_key:
        # No addressable panel -> answer like the no-panel case (503) so the tool
        # falls back to playwright-cli rather than surfacing a hard error.
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    bus = get_command_bus()
    logger.debug("browser-cmdbus: submit op=%s session=%s", op, session_key)
    try:
        outcome = await bus.submit(session_key, op, args or {}, timeout_ms=timeout_ms)
    except NoPanelError:
        logger.debug(
            "browser-cmdbus: no native panel registered for session=%s -> 503 (client falls back to playwright-cli)",
            session_key,
        )
        return web.json_response(
            {"error": "no-native-panel", "code": "no_native_panel"}, status=503
        )
    except QueueFullError:
        return web.json_response({"error": "queue-full", "code": "queue_full"}, status=429)
    except asyncio.TimeoutError:
        return web.json_response({"error": "timeout", "code": "timeout"}, status=504)
    response: dict[str, Any] = {"id": outcome.get("id"), "ok": bool(outcome.get("ok"))}
    if outcome.get("ok"):
        response["result"] = outcome.get("result")
    else:
        response["error"] = outcome.get("error") or "error"
    logger.debug(
        "browser-cmdbus: op=%s completed ok=%s session=%s", op, bool(outcome.get("ok")), session_key
    )
    return web.json_response(response)


async def api_browser_command_drain(request: web.Request) -> web.Response:
    """POST /api/browser/command-drain -- long-poll for a queued browser command.

    Called by the Electron main process. Body:
    ``{"session_keys": [str, ...], "wait_ms"?: int}``.

    SIDE EFFECT: registers ``session_keys`` as having a live native panel for a
    fixed liveness window (independent of ``wait_ms``) AND marks a native host
    present for the same window; the registration is what ``/api/browser/command``
    checks to decide whether to 503, and host-presence is what lets it briefly
    wait for a cold-starting panel instead. ``wait_ms == 0`` with empty
    ``session_keys`` is the Electron idle heartbeat: it refreshes host-presence
    and returns 204 at once.

    Responses: 200 ``{"id", "session_key", "op", "args"}`` or 204 (nothing yet).
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_drain",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    session_keys = body.get("session_keys")
    if not isinstance(session_keys, list) or not all(isinstance(k, str) for k in session_keys):
        return web.json_response(
            {"error": "session_keys must be a list of strings", "code": "session_keys_invalid"},
            status=400,
        )
    wait_ms = body.get("wait_ms")
    # ``wait_ms == 0`` is a valid heartbeat: refresh host-presence and return 204
    # at once. Only a missing, negative, or non-int value takes the long wait.
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or wait_ms < 0:
        wait_ms = DEFAULT_DRAIN_WAIT_MS
    bus = get_command_bus()
    command = await bus.drain(session_keys, wait_ms=wait_ms)
    if command is None:
        return web.Response(status=204)
    return web.json_response(command)


async def api_browser_command_result(request: web.Request) -> web.Response:
    """POST /api/browser/command-result -- post a native browser command's result.

    Called by the Electron main process. Body:
    ``{"id": str, "ok": bool, "result"?: <any>, "error"?: str}``.

    Responses: 200 ``{"ok": true}``, or 404 ``{"code": "unknown_command"}`` when
    the id already timed out or never existed.
    """
    if request.get("internal_auth") is not True:
        _sel().log_api_access(
            caller=str(request.get("user") or request.remote or ""),
            operation="browser_command_result",
            outcome="denied",
            source="browser",
            error="internal-secret authentication required (cookie callers forbidden)",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    command_id = body.get("id")
    if not isinstance(command_id, str) or not command_id:
        return web.json_response({"error": "id required", "code": "id_required"}, status=400)
    ok = bool(body.get("ok"))
    result = body.get("result")
    error = body.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    bus = get_command_bus()
    matched = await bus.complete(command_id, ok, result=result, error=error)
    if not matched:
        return web.json_response(
            {"error": "unknown-command", "code": "unknown_command"}, status=404
        )
    return web.json_response({"ok": True})


async def api_browser_install_get(request: web.Request) -> web.Response:
    """GET /api/browser/install -- whether browsing is available, and why not.

    Reports `installing` separately from the detection fields so the card can show
    progress for an install already in flight, including one started by a different
    dashboard tab: the job lives on the gateway, not in a page.
    """
    state: DashboardState = request.app["state"]
    payload = dict(await asyncio.to_thread(browser_cli_install.detect))
    task = getattr(state, "_browser_install_task", None)
    payload["installing"] = bool(task and not task.done())
    payload["token"] = browser_cli_token.has_token()
    payload["last_error"] = getattr(state, "_browser_install_error", None)
    return web.json_response(payload)


async def api_browser_install_start(request: web.Request) -> web.Response:
    """POST /api/browser/install -- install the Playwright CLI in the background.

    Returns immediately with the same shape as the GET. The install downloads a
    browser, which takes long enough that holding the request open would read as a
    hung dashboard, so progress is observed by re-reading rather than awaited here.

    Concurrent clicks are folded into the one running job: npm and the browser
    installer are not safe to run twice over the same target at once.
    """
    denied = _deny_non_owner_browser_request(request, "browser_cli_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    task = getattr(state, "_browser_install_task", None)
    if not (task and not task.done()):

        async def _run() -> None:
            state._browser_install_error = None
            try:
                result = await asyncio.to_thread(browser_cli_install.install)
                # The LAST step, not the first failed one. Two reasons, both of
                # them cases this string is the only cure for:
                #   * A step can fail and be RECOVERED -- a refused
                #     ``--with-deps`` is retried without the flag
                #     (browser_cli.os_deps) and its failed attempt stays in
                #     ``steps`` so the operator can see what was tried. Reporting
                #     "any failed step" would raise a permanent banner quoting a
                #     sudo refusal on a host where browsing works.
                #   * When the install really did fail, the FIRST failed step may
                #     be that same recovered one, which would mask the step that
                #     actually decided the outcome and drop the remedy it carries.
                # ``install`` returns ``ok`` from its last step and every earlier
                # gate returns on a real failure, so the last step is always the
                # decisive one.
                steps = result.get("steps") or []
                failed = [] if result.get("ok") or not steps else steps[-1:]
                if failed:
                    first = failed[0]
                    # `stderr`, not `error`: install steps only ever carry
                    # `stderr` (see browser_cli.install._step), so reading
                    # `error` discarded the npm / download output and left the
                    # operator with a bare "failed" -- which cannot tell a
                    # registry auth error apart from a blocked download, the two
                    # cases the panel renders this string to explain.
                    # Redacted before it reaches the panel. npm failures routinely
                    # quote the command's own environment back at you: a registry
                    # line carrying `_authToken=`, or a proxy URL with inline
                    # credentials. This string is rendered verbatim in Settings and
                    # is the thing an operator screenshots into a bug report, so it
                    # goes through the same two-pass redaction as every other
                    # external surface.
                    detail = first.get("stderr") or first.get("error") or "failed"
                    state._browser_install_error = _redact(
                        f"{first.get('name', 'install')}: {str(detail).strip()}"
                    )[:2000]
            except Exception as exc:  # noqa: BLE001 - surfaced to the operator
                state._browser_install_error = _redact(str(exc))[:2000]

        state._browser_install_task = asyncio.create_task(_run())
    return await api_browser_install_get(request)


async def api_browser_engine_install(request: web.Request) -> web.Response:
    """POST /api/browser/engine -- download one engine's browser build.

    Body: ``{"engine": "chromium" | "firefox" | "webkit"}``.

    Shares the ONE ``_browser_install_task`` slot with the CLI install rather than
    taking its own: both drive the same browser installer, which is not safe to run
    twice over the same cache at once, and sharing the slot means the panel's single
    "installing" flag stays true for whichever download is in flight.
    """
    denied = _deny_non_owner_browser_request(request, "browser_engine_install")
    if denied is not None:
        return denied
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - a malformed body is a client error, not a crash
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # `(body or {})` absorbs [] and null but NOT a non-empty non-dict ([1],
    # "abc", 5), which would AttributeError into a 500. Check the type instead
    # of leaning on falsiness.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_json"}, status=400
        )
    engine = str(body.get("engine", "")).strip()
    # Validated here as well as in install_browser: rejecting at the boundary
    # keeps an unknown value out of the background task entirely, so the operator
    # gets a 400 instead of an error they have to go re-read the status to find.
    if engine not in browser_cli_install.BROWSER_ENGINES:
        return web.json_response({"error": "unknown engine", "code": "unknown_engine"}, status=400)
    task = getattr(state, "_browser_install_task", None)
    if task and not task.done():
        # 409, NOT a folded success. Folding is right for the CLI install, which
        # has one target: a second click means the same work. Engines are three
        # DISTINCT targets sharing one slot, so answering 200 while a different
        # engine installs makes the panel show WebKit downloading when Firefox
        # actually is. Refuse and say why.
        return web.json_response(
            {"error": "an install is already running", "code": "install_already_running"},
            status=409,
        )

    async def _run() -> None:
        state._browser_install_error = None
        try:
            result = await asyncio.to_thread(browser_cli_install.install_browser, engine)
            # The decisive step, not the first failed one: see the CLI install
            # path above for why a recovered attempt must neither raise a banner
            # nor mask the step that actually decided the outcome.
            steps = result.get("steps") or []
            failed = [] if result.get("ok") or not steps else steps[-1:]
            if failed:
                first = failed[0]
                state._browser_install_error = _redact(
                    f"{first.get('name', 'install-browser')}: "
                    f"{first.get('stderr') or first.get('error') or 'failed'}"
                )[:2000]
        except Exception as exc:  # noqa: BLE001 - surfaced to the operator
            state._browser_install_error = _redact(str(exc))[:2000]

    state._browser_install_task = asyncio.create_task(_run())
    return await api_browser_install_get(request)


async def api_browser_view_get(request: web.Request) -> web.Response:
    """GET /api/browser/view -- where the Playwright CLI dashboard is served.

    Reports without starting anything, so polling the panel never launches a
    browser dashboard the operator did not ask for.

    App-token denied like the install and token routes: the reply carries the
    dashboard URL, and that URL is served WITHOUT authentication, so handing it
    to an app is handing over control of a logged-in browser. Read-only on this
    gateway is not read-only on the browser.
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_status")
    if denied is not None:
        return denied
    return web.json_response(await asyncio.to_thread(browser_cli_view.status))


async def api_browser_view_start(request: web.Request) -> web.Response:
    """POST /api/browser/view/start -- ensure the CLI dashboard is serving.

    Idempotent, and off-loaded to a thread because starting the dashboard waits on
    a child process becoming healthy, which would otherwise stall the event loop
    and with it every other dashboard request. Honors
    ``dashboard.browser_view_port`` when set, so a remote-gateway operator can
    keep the port inside their tunnel's forwarded set.

    App-token denied: this both LAUNCHES a browser process and returns the
    unauthenticated dashboard URL, so it is the stronger half of the same hole
    the GET carries.
    """
    denied = _deny_non_owner_browser_request(request, "browser_view_start")
    if denied is not None:
        return denied

    def _start_view() -> None:
        # Config read stays in the worker thread with the child-process wait:
        # both are blocking I/O that must not run on the event loop.
        pinned = KiroCrewConfig.load().dashboard.browser_view_port
        browser_cli_view.ensure_running(pinned or None)

    await asyncio.to_thread(_start_view)
    return web.json_response(await asyncio.to_thread(browser_cli_view.status))


async def _validate_slack_token(key: str, token: str) -> str | None:
    """Check a pasted token against Slack before it is stored.

    Bot tokens are checked with ``auth.test``; app-level tokens with
    ``apps.connections.open`` (the same call the gateway makes at startup, so
    a token that passes here will connect at boot). Returns ``None`` when
    Slack accepts the token, or Slack's error code (e.g. ``invalid_auth``)
    when it rejects it. Network failures propagate to the caller, which
    treats them as "unverifiable" rather than invalid — saves must not be
    blocked by being offline.
    """
    from slack_sdk.errors import SlackApiError
    from slack_sdk.web.async_client import AsyncWebClient

    client = AsyncWebClient(token=token, timeout=_TOKEN_VERIFY_TIMEOUT)
    try:
        if key == "SLACK_APP_TOKEN":
            await client.apps_connections_open(app_token=token)
        else:
            await client.auth_test()
        return None
    except SlackApiError as exc:
        try:
            return str(exc.response.get("error", "") or "rejected")[:60]
        except Exception:
            return "rejected"


def _mask_secret(val: str) -> str:
    """Return a masked preview keeping the token prefix + last 4 chars.

    e.g. "xoxb-1234-abcd…wxyz" → "xoxb-••••wxyz". Empty string for no value.
    """
    if not val:
        return ""
    prefix = f"{val.split('-', 1)[0]}-" if "-" in val else ""
    tail = val[-4:] if len(val) >= 4 else ""
    return f"{prefix}••••{tail}"


def _clean_id_list(raw: object, is_valid: Callable[[str], bool], label: str) -> list[str]:
    """Validate and normalize a list of ID strings, dropping blanks.

    Raises ``ValueError`` (message safe to surface) when *raw* is not a list or
    an entry fails *is_valid*. Shared by the channel / enterprise-org fields.
    """
    if not isinstance(raw, list):
        raise ValueError(f"{label}s must be a list")
    out: list[str] = []
    for item in raw:
        s = str(item).strip()
        if not s:
            continue
        if not is_valid(s):
            raise ValueError(f"invalid {label}: {s}")
        out.append(s)
    return out


async def _write_env_off_loop(updates: dict[str, str | None]) -> None:
    """Run the blocking ``.env`` write on a worker, drained under the config lock.

    Every caller holds ``_get_config_lock()`` across this, and a thread cannot be
    cancelled -- so a bare ``await asyncio.to_thread(...)`` lets a cancelled
    request (a client disconnecting mid-save, gateway shutdown) unwind the
    ``async with`` while the worker is still rewriting ``.env``. The next channel
    save then enters the critical section against a file still being replaced and
    writes it back from lines it read before the first write landed, discarding
    whichever credential the other save was persisting.

    Shielding and draining puts the lock release after the worker instead. It
    cannot change WHETHER the write happens -- the thread runs to completion
    either way -- so the only thing it decides is whether the lock outlives it.
    The cancellation is re-raised, never swallowed.

    All six channel saves go through here. The offload itself is already in
    place on every one of them; the bare offload is what leaves the hole, so
    covering a subset would leave the same window open in the rest.
    """
    fut = asyncio.ensure_future(asyncio.to_thread(_write_env_updates, updates))
    try:
        await asyncio.shield(fut)
    except asyncio.CancelledError:
        await asyncio.wait([fut])
        raise


def _write_env_updates(updates: dict[str, str | None]) -> None:
    """Update select keys in config_dir/.env, preserving comments and order.

    A value of ``None`` deletes the key; new keys are appended. The write goes
    through :func:`kiro_crew.atomic_write.atomic_write` with
    ``restrict_to_owner=True``, which locks the unique temp file down to its
    owner BEFORE any content byte is written: POSIX mode bits protect nothing
    on Windows (``fchmod_safe`` is a documented no-op there), so a lockdown
    applied after the write would leave the tokens readable under the
    directory-inherited DACL for the whole write — and indefinitely if the
    lockdown fails. ``restrict_on_error="warn"`` keeps this writer's contract:
    a host where the lockdown cannot be applied must not abort a token save
    that already succeeded.

    CALLER CONTRACT: blocking, and every caller is an async request handler
    holding ``_get_config_lock()``, so each one must reach this through
    ``_write_env_off_loop`` rather than ``asyncio.to_thread`` directly --
    the drain there is what keeps the lock from being released mid-write.
    Offload the WHOLE call, never a part of it: the read-modify-write is one
    transaction, and a suspension point between the read and the rename would
    let a concurrent writer's keys be dropped by a write derived from lines
    nobody re-read. ``test/test_channel_env_write_off_loop.py`` pins both
    properties for all six channels.
    """
    ep = _loader.env_path()
    # Ensure the parent exists before opening the lock file (the .env itself may
    # not exist yet — e.g. first credential save into a fresh config dir).
    ep.parent.mkdir(parents=True, exist_ok=True)
    # Serialize this read-modify-write against the OTHER cross-process .env
    # writers (the `kirocrew secrets import` migrator and the WeChat/Weixin QR
    # handler) on the SAME advisory lock, derived from the shared helper so all
    # writers provably use one lock file. Without this, a channel/token save
    # here could interleave with the importer's rewrite (read stale bytes here,
    # or clobber the importer's commit), losing a freshly saved token or leaving
    # a `secret://` reference pointing at the wrong value. The lock wraps the
    # entire read → transform → atomic-rename sequence.
    # Lazy import: `secrets.migrate` is the CLI migration module and must stay
    # OFF the gateway boot path (messaging.py is imported at startup). Importing
    # it inside the writer keeps it off the module-import path so gateway
    # readiness is not delayed by loading the migration module.
    from kiro_crew.secrets.migrate import _env_lock_path

    lock_path = _env_lock_path(ep)
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    if not platform_compat.try_acquire_lock(lock_fd, exclusive=True):
        os.close(lock_fd)
        raise OSError(
            f"{ep} is locked by another process (a secrets import or another "
            "credential save is in progress); no update performed. Retry once "
            "the other operation finishes."
        )
    try:
        _write_env_updates_locked(ep, updates)
    finally:
        platform_compat.release_lock(lock_fd)
        os.close(lock_fd)


def _write_env_updates_locked(ep: "Path", updates: dict[str, str | None]) -> None:
    """The read-modify-atomic-rewrite of .env, run under the .env lock held by
    the caller (:func:`_write_env_updates`)."""

    lines = ep.read_text(encoding="utf-8").splitlines() if ep.exists() else []
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s and not s.startswith("#") and "=" in s:
            k = s.split("=", 1)[0].strip()
            if k in updates:
                seen.add(k)
                new_val = updates[k]
                if new_val is None:
                    continue
                out.append(f"{k}={new_val}")
                continue
        out.append(line)
    for k, new_val in updates.items():
        if k not in seen and new_val:
            out.append(f"{k}={new_val}")
    content = "\n".join(out) + ("\n" if out else "")
    atomic_write(ep, content, restrict_to_owner=True, restrict_on_error="warn")


async def api_slack_manifest(request: web.Request) -> web.Response:
    """GET /api/slack/manifest — rendered Slack app manifest + create URL.

    Mirrors ``kirocrew manifest --url`` so the settings UI can offer one-click
    Slack app creation without the CLI: the bundled template gets the user's
    alias substituted, and the comment-stripped YAML is URL-encoded into
    Slack's new-app deep link. Serves only the public template — no secrets.
    """
    from kiro_crew import slack_manifest

    # Default to a non-identifying alias: $USER is a host account name and
    # should not be volunteered to every authenticated client.
    alias = request.query.get("alias", "").strip() or "kirocrew"
    if not slack_manifest.valid_alias(alias):
        return web.json_response({"error": "invalid alias"}, status=400)
    try:
        rendered = slack_manifest.render(alias)
        create_url = slack_manifest.deep_link(alias)
    except FileNotFoundError:
        return web.json_response({"error": "manifest template missing"}, status=500)
    return web.json_response(
        {
            "alias": alias,
            "manifest": rendered,
            "create_url": create_url,
        }
    )


async def api_slack_config_get(request: web.Request) -> web.Response:
    """GET /api/slack/config — read Slack config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_OWNER_ID,
        CRED_SLACK_APP_TOKEN,
        CRED_SLACK_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    bot = creds.get(CRED_SLACK_BOT_TOKEN, "")
    app = creds.get(CRED_SLACK_APP_TOKEN, "")
    owner = creds.get(CRED_OWNER_ID, "")
    slack = cfg.slack
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the socket-mode connect succeeded this session —
            # NOT merely "tokens were present at boot" (see DashboardState).
            "connected": bool(getattr(state, "slack_socket_connected", False)),
            # Short reason from the failed connect attempt ("invalid_auth",
            # a network error class name, or "" when connected / untried).
            "connect_error": str(getattr(state, "slack_connect_error", ""))[:120],
            "configured": bool(bot and app and owner),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(bot),
            "app_token_set": bool(app),
            "bot_token_preview": _mask_secret(bot),
            "app_token_preview": _mask_secret(app),
            "owner_id": owner,
            "command": slack.command,
            # allowed_users / open_channels are deliberately NOT exposed: the
            # runtime enforces owner-only access in this build (is_allowed_user
            # ignores both), so surfacing editors would create access rules
            # that are never honored. Re-add when multi-user Slack lands.
            "allowed_enterprise_ids": list(slack.allowed_enterprise_ids),
            "reactions_enabled": slack.reactions_enabled,
            "show_thinking": slack.show_thinking,
            "session_folder": slack.session_folder,
        }
    )


async def api_slack_config_save(request: web.Request) -> web.Response:
    """PUT /api/slack/config — persist Slack secrets (.env) + config (config.json).

    Token/owner changes need a gateway restart to reconnect Slack (creds are
    read at gateway startup); the response returns ``restart_required`` so the
    UI can surface a hint. Config-only changes take effect on the next message
    or restart.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Discord and Telegram saves) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        return await _slack_config_save_locked(request)


async def _slack_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Slack save; caller holds ``_get_config_lock()``."""
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_OWNER_ID,
        config_path,
    )
    from kiro_crew.validation import USER_ID_RE  # noqa: F811

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="slack.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

    # Remote sessions are read-only: like /reveal, config writes are accepted
    # only from the machine running the gateway, so a remote or tunneled
    # session (even with a valid dashboard token) cannot alter Slack access
    # or plant new tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state
    # (e.g. a token persisted while a bad channel ID 400s). ──

    # Secrets → .env (empty/omitted token = leave unchanged; explicit clear via
    # *_clear flag to avoid accidentally wiping a token on save).
    env_updates: dict[str, str | None] = {}
    for field_name, key in _SLACK_SECRET_FIELDS.items():
        clear_flag = body.get(f"{field_name}_clear")
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{field_name}_clear must be a boolean")
        if clear_flag is True:
            env_updates[key] = None
            continue
        raw = body.get(field_name)
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{key}="):  # strip an accidentally pasted env line
                tok = tok[len(key) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny(f"{field_name} must not contain whitespace")
                env_updates[key] = tok

    if "owner_id" in body:
        owner = str(body.get("owner_id", "")).strip()
        if owner and not USER_ID_RE.match(owner):
            return _deny("owner_id must be a Slack member ID (starts with U or W)")
        # Only stage a real change: the UI sends the field on every save, and
        # staging an unchanged value would flag restart_required on every
        # config-only save.
        current_owner = os.environ.get(CRED_OWNER_ID, "").strip()
        if owner != current_owner:
            env_updates[CRED_OWNER_ID] = owner or None

    # Config → config.json under "slack" (staged, applied only after Phase 1).
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("slack"), dict):
        data["slack"] = {}
    slack_cfg = data["slack"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "command" in body:
        cmd = str(body.get("command", "")).strip().lstrip("/").strip()
        if cmd and (len(cmd) > 32 or not all(c.isalnum() or c in "-_" for c in cmd)):
            return _deny("command must be alphanumeric/-/_ and at most 32 chars")
        # Empty input resets to the default rather than silently keeping the
        # old value — previously the slash command could be set but never
        # cleared. Stage only on actual change: the UI sends the field on
        # every save, and command is boot-read, so staging an unchanged value
        # would flag restart_required on every save.
        new_cmd = cmd or "kirocrew"
        if new_cmd != slack_cfg.get("command", "kirocrew"):
            staged["command"] = new_cmd
            applied.append("command")

    if "allowed_enterprise_ids" in body:
        try:
            new_ents = _clean_id_list(
                body.get("allowed_enterprise_ids"),
                lambda v: bool(re.fullmatch(r"[ET][A-Z0-9]+", v)),
                "enterprise ID",
            )
        except ValueError as exc:
            return _deny(str(exc))
        # Boot-read field: stage only on actual change (see command above).
        if new_ents != slack_cfg.get("allowed_enterprise_ids", []):
            staged["allowed_enterprise_ids"] = new_ents
            applied.append("allowed_enterprise_ids")

    for key in ("reactions_enabled", "show_thinking"):
        if key in body:
            val = body.get(key)
            if not isinstance(val, bool):
                return _deny(f"{key} must be a boolean")
            staged[key] = val
            applied.append(key)

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(slack_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # ── Phase 1.5: verify newly pasted tokens against Slack before storing.
    # A token Slack rejects (invalid_auth etc.) fails the save right here,
    # where the user can act on it — instead of being stored and silently
    # failing at the next gateway startup. Network failure is NOT a rejection:
    # the save proceeds with a warning so being offline never blocks config.
    verify_warning = ""
    for field_name, key in _SLACK_SECRET_FIELDS.items():
        pending_tok = env_updates.get(key)
        if not pending_tok:
            continue  # cleared or unchanged — nothing to verify
        try:
            slack_err = await _validate_slack_token(key, pending_tok)
        except Exception:
            verify_warning = "Slack was unreachable, so the token was saved without verification."
            continue
        if slack_err:
            return _deny(f"{field_name} rejected by Slack ({slack_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        await _write_env_off_loop(env_updates)
        # Keep the live process environment in sync with the new .env state.
        # load_credentials() lets os.environ win over .env, so without this a
        # replaced/cleared token would keep being reported as installed by GET
        # until restart, and spawned children would inherit the stale value.
        # The Slack socket connection itself still reconnects only on restart,
        # which restart_required below surfaces to the UI.
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val
    if staged:
        slack_cfg.update(staged)
        # Shield + drain so a cancellation arriving mid-write cannot release
        # the config lock while the worker thread is still replacing the file.
        # Without this a later save can interleave writes under the lock.
        _cfg_write_task_sl: asyncio.Task[None] = asyncio.ensure_future(
            asyncio.to_thread(_atomic_json_write, path, data)
        )
        try:
            await asyncio.shield(_cfg_write_task_sl)
        except asyncio.CancelledError:
            await asyncio.gather(_cfg_write_task_sl, return_exceptions=True)
            raise

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(slack_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "slack",
                _folder_name,
                relabel="session_folder" in staged,
            )

    _sel().log_api_access(
        caller=caller,
        operation="slack.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # command and enterprise IDs are read once at gateway startup; reactions
    # and show_thinking are re-read per message, so only the former (plus any
    # secret/owner change) need a restart to take effect.
    boot_read = {"command", "allowed_enterprise_ids"}
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(boot_read & staged.keys()),
            "verify_warning": verify_warning,
        }
    )


def _threshold_pct_rejection(body: dict[str, Any], key: str) -> tuple[str, str] | None:
    """``(code, message)`` when *body[key]* is present and not a valid percentage.

    ``None`` when the key is absent or the value is in range. One home for the check
    across every channel that exposes context-window nudge thresholds, because
    ``isinstance(True, int)`` is True in Python: each site has to exclude bool
    explicitly or a JSON ``true`` reads as 1%, and a per-channel copy of that subtlety
    is a per-channel chance to omit it. The bounds come from the config loader, so a
    value this accepts is one the loader will not silently clamp.
    """
    if key not in body:
        return None
    from kiro_crew.config.loader import THRESHOLD_PCT_MAX, THRESHOLD_PCT_MIN

    pct = body.get(key)
    if isinstance(pct, int) and not isinstance(pct, bool):
        if THRESHOLD_PCT_MIN <= pct <= THRESHOLD_PCT_MAX:
            return None
    return (
        f"{key}_invalid",
        f"{key} must be an integer between {THRESHOLD_PCT_MIN} and {THRESHOLD_PCT_MAX}",
    )


# ── Discord configuration API ──
# The bot token lives in config_dir/.env as DISCORD_BOT_TOKEN (0600), with
# config.json's discord.bot_token as a legacy fallback. Non-secret config
# (enabled, allowed_user_ids, allowed_thread_ids, soft_threshold_pct) lives
# in config.json under
# the "discord" key. GET returns a masked preview + presence boolean; raw
# token values are write-only (reset at the Developer Portal if ever needed).

#: Discord fields the dispatcher re-reads per turn, so changing one takes
#: effect on the next message rather than at the next restart.
_DISCORD_LIVE_FIELDS = frozenset({"reactions_enabled", "show_thinking"})

#: Loose shape check for Discord bot tokens: three dot-separated base64url
#: segments (e.g. "MTA5...aBc.GhIjKl.MnOpQrStUvWxYz0123456789_-").
_DISCORD_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{20,}$")


async def _validate_discord_token(token: str) -> str | None:
    """Check a pasted bot token against Discord before it is stored.

    Uses ``GET /users/@me`` — the cheapest authenticated REST call. Returns
    ``None`` when Discord accepts the token, or Discord's error message when
    it rejects it. Network failures propagate to the caller, which treats
    them as "unverifiable" rather than invalid — saves must not be blocked by
    being offline.
    """
    import aiohttp  # noqa: F811

    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            desc = ""
            try:
                data = await resp.json(content_type=None)
                if isinstance(data, dict):
                    desc = str(data.get("message", "") or "")
            except Exception:
                pass
            return (desc or f"HTTP {resp.status}")[:60]


async def api_discord_config_get(request: web.Request) -> web.Response:
    """GET /api/discord/config — read Discord config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_DISCORD_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or cfg.discord.bot_token
    dc = cfg.discord
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the Gateway WebSocket transport actually started
            # this session — NOT merely "a token was present at boot".
            "connected": bool(getattr(state, "discord_connected", False)),
            "connect_error": str(getattr(state, "discord_connect_error", ""))[:120],
            # allowed_user_ids is part of "configured": the transport fails
            # closed and rejects every message while the allowlist is empty.
            "configured": bool(token and dc.enabled and dc.allowed_user_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": bool(dc.enabled),
            "allowed_user_ids": [str(u) for u in dc.allowed_user_ids],
            "allowed_thread_ids": [str(t) for t in dc.allowed_thread_ids],
            "allowed_channel_ids": [str(c) for c in dc.allowed_channel_ids],
            "auto_thread": bool(dc.auto_thread),
            "reactions_enabled": bool(dc.reactions_enabled),
            "show_thinking": bool(dc.show_thinking),
            "soft_threshold_pct": int(dc.soft_threshold_pct),
            "session_folder": dc.session_folder,
        }
    )


async def api_discord_config_save(request: web.Request) -> web.Response:
    """PUT /api/discord/config — persist Discord secret (.env) + config (config.json).

    Every Discord field is read once at gateway startup (token, enabled flag,
    allowlist are consumed in the orchestrator's constructor), so any actual
    change returns ``restart_required`` for the UI hint.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Slack and Telegram saves) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        return await _discord_config_save_locked(request)


async def _discord_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Discord save; caller holds ``_get_config_lock()``."""
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_DISCORD_BOT_TOKEN,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="discord.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        # ``code`` is optional: most rejections in this handler are prose-only, and a
        # machine-readable code is added per field as one is retrofitted. The dashboard
        # renders ``error`` verbatim into a localized UI, so prose alone is
        # untranslatable by construction (RFC 9457 3.1.3).
        payload: dict[str, Any] = {"error": msg}
        if code:
            payload["code"] = code
        return web.json_response(payload, status=status)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with
    # a valid dashboard token) cannot alter Discord access or plant tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("bot_token_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("bot_token_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_DISCORD_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_DISCORD_BOT_TOKEN}="):  # accidental env line
                tok = tok[len(CRED_DISCORD_BOT_TOKEN) + 1 :].strip()
            if tok.startswith("Bot "):  # accidental Authorization-header prefix
                tok = tok[4:].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                if not _DISCORD_TOKEN_RE.match(tok):
                    return _deny(
                        "bot_token must be the bot token from the Discord "
                        "Developer Portal (Bot page → Reset Token)"
                    )
                env_updates[CRED_DISCORD_BOT_TOKEN] = tok

    # Config → config.json under "discord" (staged, applied only after Phase 1).
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("discord"), dict):
        data["discord"] = {}
    dc_cfg = data["discord"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(dc_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        new_ids: list[str] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            # Discord user IDs are numeric snowflakes (17-20 digits today;
            # accept any all-digit string to stay future-proof).
            if not s.isdigit():
                return _deny(f"invalid Discord user ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(u) for u in dc_cfg.get("allowed_user_ids", [])]:
            staged["allowed_user_ids"] = new_ids
            applied.append("allowed_user_ids")

    if "allowed_thread_ids" in body:
        raw_ids = body.get("allowed_thread_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_thread_ids must be a list")
        new_ids = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Discord thread ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(t) for t in dc_cfg.get("allowed_thread_ids", [])]:
            staged["allowed_thread_ids"] = new_ids
            applied.append("allowed_thread_ids")

    if "allowed_channel_ids" in body:
        raw_ids = body.get("allowed_channel_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_channel_ids must be a list")
        new_ids = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Discord channel ID: {s} (numeric IDs only)")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != [str(c) for c in dc_cfg.get("allowed_channel_ids", [])]:
            staged["allowed_channel_ids"] = new_ids
            applied.append("allowed_channel_ids")

    if "auto_thread" in body:
        val = body.get("auto_thread")
        if not isinstance(val, bool):
            return _deny("auto_thread must be a boolean")
        if val != bool(dc_cfg.get("auto_thread", True)):
            staged["auto_thread"] = val
            applied.append("auto_thread")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(dc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    # Both render toggles are read per turn by the dispatcher, not at boot, so a
    # change takes effect on the next message. `_DISCORD_LIVE_FIELDS` below keeps
    # `restart_required` honest about that; promising a restart the user does not
    # need is how a settings page trains people to restart for everything.
    for toggle in ("reactions_enabled", "show_thinking"):
        if toggle in body:
            val = body.get(toggle)
            if not isinstance(val, bool):
                return _deny(f"{toggle} must be a boolean")
            if val != bool(dc_cfg.get(toggle, toggle == "reactions_enabled")):
                staged[toggle] = val
                applied.append(toggle)

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(dc_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # Whenever the .env token is set or cleared, also drop the legacy
    # config.json ``discord.bot_token`` fallback. The gateway (and GET above)
    # fall back to that field when .env is empty, so leaving it behind would
    # resurrect a removed credential on the next restart — an explicit clear
    # must actually revoke access, and a replacement must not shadow-keep the
    # old token. It also sits in agent-readable ``config.json``, so the copy is
    # worth strictly less than the .env one it shadows. Staged here (write
    # happens only in Phase 2), matching the Telegram and Webex saves.
    legacy_token_removed = False
    if CRED_DISCORD_BOT_TOKEN in env_updates and dc_cfg.get("bot_token"):
        dc_cfg.pop("bot_token", None)
        legacy_token_removed = True
        applied.append("legacy_bot_token_removed")

    # ── Phase 1.5: verify a newly pasted token against Discord before storing.
    # A token Discord rejects fails the save right here, where the user can
    # act on it. Network failure is NOT a rejection: the save proceeds with a
    # warning so being offline never blocks config.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_DISCORD_BOT_TOKEN)
    if pending_tok:
        try:
            dc_err = await _validate_discord_token(pending_tok)
        except Exception:
            verify_warning = "Discord was unreachable, so the token was saved without verification."
        else:
            if dc_err:
                return _deny(f"bot_token rejected by Discord ({dc_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. Order
    # matters for crash safety: config.json — which carries the legacy
    # ``bot_token`` fallback removal — is persisted FIRST, so there is no
    # failure window in which .env was already cleared but the legacy fallback
    # survives to silently resurrect the revoked credential on restart. The
    # inverse failure mode (config written, then a crash before the .env
    # update) is benign and visible: the .env token remains exactly as GET
    # reports it, and re-running the save completes the operation. ──
    if staged or legacy_token_removed:
        dc_cfg.update(staged)
        # Off-loop: the atomic write (temp file + fsync + replace) must not
        # block the gateway event loop.
        await asyncio.to_thread(_atomic_json_write, path, data)

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(dc_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "discord",
                _folder_name,
                relabel="session_folder" in staged,
            )
    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        await _write_env_off_loop(env_updates)
        # Keep the live process environment in sync with the new .env state
        # (load_credentials() lets os.environ win over .env — see the Slack
        # save handler for the full rationale).
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="discord.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # Token, enabled and the allow-lists are boot-read: they are consumed in the
    # orchestrator's constructor and the dispatcher is built at boot. The two
    # render toggles are not, because the dispatcher re-reads them per turn.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates)
            or bool(staged.keys() - LIVE_RELOAD_FIELDS - _DISCORD_LIVE_FIELDS),
            "verify_warning": verify_warning,
        }
    )


# ── Telegram configuration API ──
# The bot token lives in config_dir/.env as TELEGRAM_BOT_TOKEN (0600), with
# config.json's telegram.bot_token as a legacy fallback. Non-secret config
# (enabled, allowed_user_ids, soft_threshold_pct) lives in config.json under
# the "telegram" key. GET returns a masked preview + presence boolean; raw
# token values are write-only (rotate at @BotFather if ever needed).

#: Loose shape check for Telegram bot tokens: "<bot_id>:<secret>" from
#: @BotFather (e.g. "110201543:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw").
_TELEGRAM_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]{10,}$")


async def _validate_telegram_token(token: str) -> str | None:
    """Check a pasted bot token against Telegram before it is stored.

    Uses ``getMe`` — the cheapest authenticated Bot API call. Returns ``None``
    when Telegram accepts the token, or Telegram's error description (e.g.
    ``Unauthorized``) when it rejects it. Network failures propagate to the
    caller, which treats them as "unverifiable" rather than invalid — saves
    must not be blocked by being offline.
    """
    import aiohttp  # noqa: F811

    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
            data = await resp.json(content_type=None)
            if isinstance(data, dict) and data.get("ok"):
                return None
            desc = ""
            if isinstance(data, dict):
                desc = str(data.get("description", "") or "")
            return (desc or "rejected")[:60]


async def api_telegram_config_get(request: web.Request) -> web.Response:
    """GET /api/telegram/config — read Telegram config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_TELEGRAM_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_TELEGRAM_BOT_TOKEN, "") or cfg.telegram.bot_token
    tg = cfg.telegram
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the long-polling transport actually started this
            # session — NOT merely "a token was present at boot".
            "connected": bool(getattr(state, "telegram_connected", False)),
            "connect_error": str(getattr(state, "telegram_connect_error", ""))[:120],
            # allowed_user_ids is part of "configured": the transport fails
            # closed and rejects every message while the allowlist is empty.
            "configured": bool(token and tg.enabled and tg.allowed_user_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": bool(tg.enabled),
            # Serialized as strings for the tag editor UI; the save path
            # accepts digit strings and stores canonical ints.
            "allowed_user_ids": [str(u) for u in tg.allowed_user_ids],
            "soft_threshold_pct": int(tg.soft_threshold_pct),
            "show_thinking": bool(tg.show_thinking),
            "voice_replies": bool(tg.voice_replies),
            "session_folder": tg.session_folder,
            # Forum per-topic config. chat_ids are serialized as strings for
            # the tag editor UI; they are NEGATIVE (e.g. "-1001234567890"),
            # so the save path accepts a leading minus (not a digits-only check).
            "allow_forum": bool(tg.allow_forum),
            "allowed_forum_chat_ids": [str(c) for c in tg.allowed_forum_chat_ids],
            "forum_activation": tg.forum_activation,
        }
    )


async def api_telegram_config_save(request: web.Request) -> web.Response:
    """PUT /api/telegram/config — persist Telegram secret (.env) + config (config.json).

    Every Telegram field is read once at gateway startup (token, enabled flag,
    allowlist are consumed in the orchestrator's constructor), so any actual
    change returns ``restart_required`` for the UI hint.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` (also used by the MCP, memory, and agent
    handlers) — this handler read-modify-writes the shared ``.env`` /
    ``config.json`` stores, so interleaving with ANY other config writer
    (including the Slack save) would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        return await _telegram_config_save_locked(request)


async def _telegram_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Telegram save; caller holds ``_get_config_lock()``."""
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_TELEGRAM_BOT_TOKEN,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        """Refuse with *msg*, and with a machine-readable *code* when one is given.

        ``code`` is optional so the existing denials keep their exact bodies, and is
        added per field as one is retrofitted. It exists because backend-owned
        strings have no i18n catalog path: the dashboard renders ``error`` verbatim
        into a localized UI, so prose alone is untranslatable by construction (RFC
        9457 3.1.3) and a caller reacting to a specific refusal would have to match
        on that prose. New denials should supply one.
        """
        _sel().log_api_access(
            caller=caller,
            operation="telegram.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        payload: dict[str, Any] = {"error": msg}
        if code:
            payload["code"] = code
        return web.json_response(payload, status=status)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with
    # a valid dashboard token) cannot alter Telegram access or plant tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("bot_token_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("bot_token_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_TELEGRAM_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_TELEGRAM_BOT_TOKEN}="):  # accidental env line
                tok = tok[len(CRED_TELEGRAM_BOT_TOKEN) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                if not _TELEGRAM_TOKEN_RE.match(tok):
                    return _deny("bot_token must look like <bot_id>:<secret> from @BotFather")
                env_updates[CRED_TELEGRAM_BOT_TOKEN] = tok

    # Config → config.json under "telegram" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop (chat, heartbeats). Reading under _get_config_lock() keeps
    # the snapshot current relative to every other config writer.
    path = config_path()

    def _read_config() -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    try:
        data = await asyncio.to_thread(_read_config)
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("telegram"), dict):
        data["telegram"] = {}
    tg_cfg = data["telegram"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(tg_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        new_ids: list[int] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not s.isdigit():
                return _deny(f"invalid Telegram user ID: {s} (numeric IDs only)")
            uid = int(s)
            if uid not in new_ids:
                new_ids.append(uid)
        if new_ids != list(tg_cfg.get("allowed_user_ids", [])):
            staged["allowed_user_ids"] = new_ids
            applied.append("allowed_user_ids")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(tg_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "show_thinking" in body:
        val = body.get("show_thinking")
        # Strict bool, like every other toggle on this handler: a truthy string
        # would silently enable a per-turn extra message nobody asked for.
        if not isinstance(val, bool):
            return _deny("show_thinking must be a boolean")
        if val != bool(tg_cfg.get("show_thinking", False)):
            staged["show_thinking"] = val
            applied.append("show_thinking")

    if "voice_replies" in body:
        val = body.get("voice_replies")
        # Strict bool for the same reason as show_thinking: a truthy string would
        # silently start uploading synthesized audio, one extra message per turn.
        if not isinstance(val, bool):
            return _deny("voice_replies must be a boolean")
        if val != bool(tg_cfg.get("voice_replies", False)):
            staged["voice_replies"] = val
            applied.append("voice_replies")

    if "forum_activation" in body:
        val = body.get("forum_activation")
        # Validated against the closed set HERE rather than left to the loader's
        # degrade-to-always: the loader's fallback exists for a config file edited
        # by hand, and silently storing an unusable value the operator picked in a
        # dropdown would report success for a setting that never took effect.
        if not isinstance(val, str) or val not in TELEGRAM_ACTIVATIONS:
            return _deny(
                "forum_activation must be one of " + ", ".join(sorted(TELEGRAM_ACTIVATIONS)),
                code="invalid_forum_activation",
            )
        if val != str(tg_cfg.get("forum_activation", "always") or "always"):
            staged["forum_activation"] = val
            applied.append("forum_activation")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(tg_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    if "allow_forum" in body:
        val = body.get("allow_forum")
        if not isinstance(val, bool):
            return _deny("allow_forum must be a boolean")
        if val != bool(tg_cfg.get("allow_forum", False)):
            staged["allow_forum"] = val
            applied.append("allow_forum")

    if "allowed_forum_chat_ids" in body:
        raw_chat_ids = body.get("allowed_forum_chat_ids")
        if not isinstance(raw_chat_ids, list):
            return _deny("allowed_forum_chat_ids must be a list")
        new_chat_ids: list[int] = []
        for item in raw_chat_ids:
            s = str(item).strip()
            if not s:
                continue
            # Forum supergroup chat_ids are NEGATIVE (e.g. -1001234567890),
            # so accept an optional leading minus — the digits-only check used
            # for allowed_user_ids would wrongly reject every group id here.
            digits = s[1:] if s.startswith("-") else s
            if not digits.isdigit():
                return _deny(f"invalid Telegram chat ID: {s} (integer IDs only)")
            cid = int(s)
            if cid not in new_chat_ids:
                new_chat_ids.append(cid)
        if new_chat_ids != list(tg_cfg.get("allowed_forum_chat_ids", [])):
            staged["allowed_forum_chat_ids"] = new_chat_ids
            applied.append("allowed_forum_chat_ids")

    # Whenever the .env token is set or cleared, also drop the legacy
    # config.json ``telegram.bot_token`` fallback. The gateway (and GET above)
    # fall back to that field when .env is empty, so leaving it behind would
    # resurrect a removed credential on the next restart — an explicit clear
    # must actually revoke access, and a replacement must not shadow-keep the
    # old token. Staged here (write happens only in Phase 2).
    legacy_token_removed = False
    if CRED_TELEGRAM_BOT_TOKEN in env_updates and tg_cfg.get("bot_token"):
        tg_cfg.pop("bot_token", None)
        legacy_token_removed = True
        applied.append("legacy_bot_token_removed")

    # ── Phase 1.5: verify a newly pasted token against Telegram before storing.
    # A token Telegram rejects fails the save right here, where the user can
    # act on it. Network failure is NOT a rejection: the save proceeds with a
    # warning so being offline never blocks config.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_TELEGRAM_BOT_TOKEN)
    if pending_tok:
        try:
            tg_err = await _validate_telegram_token(pending_tok)
        except Exception:
            verify_warning = (
                "Telegram was unreachable, so the token was saved without verification."
            )
        else:
            if tg_err:
                return _deny(f"bot_token rejected by Telegram ({tg_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. Order
    # matters for crash safety: config.json — which carries the legacy
    # ``bot_token`` fallback removal — is persisted FIRST, so there is no
    # failure window in which .env was already cleared but the legacy
    # fallback survives to silently resurrect the revoked credential on
    # restart. The inverse failure mode (config written, then a crash before
    # the .env update) is benign and visible: the .env token remains exactly
    # as GET reports it, and re-running the save completes the operation. ──
    if staged or legacy_token_removed:
        tg_cfg.update(staged)
        # Off-loop: the atomic write (temp file + fsync + replace) must not
        # block the gateway event loop.
        await asyncio.to_thread(_atomic_json_write, path, data)

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(tg_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "telegram",
                _folder_name,
                relabel="session_folder" in staged,
            )
    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        await _write_env_off_loop(env_updates)
        # Keep the live process environment in sync with the new .env state
        # (load_credentials() lets os.environ win over .env — see the Slack
        # save handler for the full rationale).
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="telegram.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # All Telegram fields are boot-read: token/enabled/allowlist are consumed
    # in the orchestrator's constructor and the dispatcher is built at boot.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(staged.keys() - LIVE_RELOAD_FIELDS),
            "verify_warning": verify_warning,
        }
    )


# ── Webex configuration API ──
# Mirrors the Slack config API above: the bot token lives in config_dir/.env
# (0600, WEBEX_BOT_TOKEN); non-secret config (enabled, allowed_emails) lives
# in config.json under the "webex" key. GET returns a masked preview +
# presence boolean; raw token values are write-only.


def _is_valid_webex_email(v: str) -> bool:
    """Loose email shape check using linear string ops (no regex).

    CodeQL flags ``[^@\\s]+@[^@\\s]+\\.[^@\\s]+`` as polynomially backtracking
    on adversarial input; exactly-one-``@``, non-empty local part, a dot in
    the domain (not at its edges), and no whitespace covers the same shape in
    O(n) without a regex engine.
    """
    if not v or len(v) > 254:
        return False
    if any(ch.isspace() for ch in v):
        return False
    local, sep, domain = v.partition("@")
    if not sep or not local or "@" in domain:
        return False
    return "." in domain[1:-1]


#: Seconds to wait for Webex when verifying a pasted token at save time.
_WEBEX_VERIFY_TIMEOUT = 8


async def _validate_webex_token(token: str) -> str | None:
    """Check a pasted bot token against Webex before it is stored.

    ``GET /people/me`` is the cheapest authenticated call (the same identity
    call the client makes at connect time). Returns ``None`` when Webex
    accepts the token, or a short error string when it rejects it (401/403).
    Network failures propagate to the caller, which treats them as
    "unverifiable" rather than invalid — saves must not be blocked by being
    offline. Mirrors ``_validate_slack_token``.
    """
    import aiohttp

    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://webexapis.com/v1/people/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=_WEBEX_VERIFY_TIMEOUT),
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            if resp.status in (401, 403):
                return f"invalid_token (http {resp.status})"
            # 5xx / 429 are Webex-side trouble, not a bad token.
            raise RuntimeError(f"webex verify http {resp.status}")


async def api_teams_activity(request: web.Request) -> web.Response:
    """POST /api/messaging/teams — Bot Framework inbound webhook (late-bound).

    The route is registered at app-build time (aiohttp freezes routes at
    startup), but the handler that validates the JWT + drives the turn is the
    ``TeamsClient.on_activity`` built by ``maybe_start_teams`` once credentials
    are present. Until then (channel disabled/uncredentialed) we return 503.

    This route is exempt from the dashboard cookie gate and from the CSRF Origin
    check, for POST only (``token_auth._BYPASS_EXACT_METHODS`` /
    ``CSRF_EXEMPT_EXACT_METHODS``); the delegated handler performs Bot Framework
    JWT validation itself before doing anything with the payload. Both
    exemptions make this the one route in the product an unauthenticated caller
    can reach, so the two hardening steps below run before delegating.
    """
    # Deferred imports: Teams is an optional, default-off channel, and importing
    # its client also probes for the optional PyJWT. Neither belongs on the
    # gateway boot path, which imports this module.
    from kiro_crew import webhooks  # noqa: F811
    from kiro_crew.teams.client import (  # noqa: F811
        TEAMS_ACTIVITY_REQUEST_KEY,
        TEAMS_MAX_ACTIVITY_BYTES,
    )

    state: DashboardState = request.app["state"]
    handler = getattr(state, "teams_on_activity", None)
    if handler is None:
        return web.Response(status=503, text="Teams channel not enabled")

    # Failed-auth throttle, the same per-source abuse damper /api/hooks/agent
    # uses. An anonymous POST here is free to the sender but not to us: an
    # unknown JWT ``kid`` sends the validator to the Bot Framework JWKS endpoint,
    # so a flood buys a remote fetch plus an audit write per request. Slowed
    # rather than answered at line rate; the real boundary remains the JWT.
    #
    # 429 rather than the 200 the in-flight shed uses: a rate limit is exactly
    # what the Connector's retry-with-backoff is for.
    #
    # SKIPPED when a proxy terminated the connection, and that is the whole point
    # of the check rather than a convenience. ``request.remote`` is then the
    # proxy's address for EVERY caller, so the real Connector and an anonymous
    # flood share one counter -- and in two of the three topologies
    # ``docs/teams-integration.md`` documents (a reverse proxy, a dev tunnel) that
    # is the normal deployment. Throttling there converts ~10 bogus POSTs into a
    # 300s outage for the channel, renewably, because a 429 is never an accepted
    # activity and so never clears the count. Trusting a forwarded header instead
    # would make a spoofable field the identity, which is its own decision;
    # bounding only the direct-bind topology is the honest half.
    source = request.remote or "unknown"
    throttled_source = "" if is_proxied_request(request) else source
    if throttled_source and webhooks.auth_throttle_blocked(throttled_source):
        # Off the loop: the first ``sel()`` of a process CONSTRUCTS the log
        # (trust-dir creation, key validation — blocking file IO), and this
        # route can be the first request a fresh gateway ever serves.
        await asyncio.to_thread(
            lambda: _sel().log_api_access(
                caller=source,
                operation="teams.activity",
                outcome="denied",
                source="teams",
                error="auth failures throttled",
            )
        )
        return web.json_response(
            {"error": "too many failed attempts", "code": "auth_throttled"}, status=429
        )

    # Bounded body read. It happens HERE, not in the client, for two reasons: the
    # cap is a property of the exposed route, and ``teams/client.py`` keeps its
    # no-dashboard-imports property. ``on_activity`` reads the parsed dict from
    # the request mapping, so the body is parsed exactly once and never past the
    # cap.
    body, cap_error = await read_bounded_json(request, max_bytes=TEAMS_MAX_ACTIVITY_BYTES)
    if cap_error is not None and cap_error.status == 413:
        return cap_error
    if body is not None:
        request[TEAMS_ACTIVITY_REQUEST_KEY] = body
    # A body that is not a JSON object is deliberately NOT answered here. The
    # ordering guarantee is that the token check precedes any verdict derived
    # from body CONTENT, so the stash is left unset and ``on_activity`` answers
    # 400 after its JWT gate. An over-cap body is different: refusing on SIZE
    # reveals nothing about the payload and must happen before any buffering.

    response = await handler(request)
    # 401 is ``on_activity``'s only authentication refusal (invalid or
    # unverifiable bearer); anything it accepts clears the source, exactly as the
    # hooks webhook does after a valid token authenticates. Bookkeeping is skipped
    # for a proxied request for the same reason the check above is: the counter
    # would be shared across every caller.
    if throttled_source:
        if response.status == 401:
            webhooks.record_auth_failure(throttled_source)
        elif response.status < 400:
            webhooks.record_auth_success(throttled_source)
    return response


async def api_teams_config_get(request: web.Request) -> web.Response:
    """GET /api/teams/config — read Teams channel status + config summary."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        KiroCrewConfig,
    )

    # Deferred for the same reason as in api_teams_activity: importing the client
    # probes for the optional PyJWT, which must not happen on the boot path.
    from kiro_crew.teams.client import HAS_JWT  # noqa: F811

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    tc = cfg.teams
    # Credential resolution mirrors the boot path (slack/gateway.py) exactly: the
    # env credential wins over config.json for all three values, so the panel
    # reports the identity the channel will actually run as.
    app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or tc.app_id
    app_password = creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or tc.app_password
    tenant_id = creds.get(CRED_MICROSOFT_APP_TENANT_ID, "") or tc.tenant_id
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only once the outbound app credentials validated this
            # session (kept truthful by TeamsClient.on_state_change).
            "connected": bool(getattr(state, "teams_connected", False)),
            "connect_error": str(getattr(state, "teams_connect_error", ""))[:120],
            "configured": bool(app_id and app_password and tc.enabled and tc.allowed_emails),
            "read_only": not is_direct_local_request(request),
            "app_id_set": bool(app_id),
            # Presence only, deliberately with no masked preview: an Azure client
            # secret carries no vendor prefix, so the shared prefix-preserving
            # mask (_mask_secret keeps everything before the first "-") would
            # reveal real secret bytes rather than a type marker.
            "app_password_set": bool(app_password),
            # PyJWT ships in the optional `kirocrew[teams]` extra and the channel
            # REFUSES to start without it (teams/gateway.py), because inbound JWT
            # validation is impossible. Reported so an operator sees the reason
            # instead of a channel that silently never starts.
            "jwt_available": bool(HAS_JWT),
            "enabled": tc.enabled,
            # Non-secret: blank means a multi-tenant bot, a value means
            # single-tenant. The operator needs it to tell those apart.
            "tenant_id": tenant_id,
            "allowed_emails": list(tc.allowed_emails),
            "soft_threshold_pct": int(tc.soft_threshold_pct),
            "hard_threshold_pct": int(tc.hard_threshold_pct),
            "session_folder": tc.session_folder,
        }
    )


def _is_valid_teams_principal(v: str) -> bool:
    """Accept an allow-list entry that is either an email/UPN or an AAD object
    id. Both are non-empty, whitespace-free, and length-bounded; keeping the
    check shape-only (no regex) mirrors the Webex email helper and lets object
    ids (GUIDs) through, since Teams activities key on those."""
    if not v or len(v) > 254:
        return False
    return not any(ch.isspace() for ch in v)


#: Azure AD OAuth error codes that mean "these credentials are wrong" rather than
#: "Azure is having trouble". Anything else is treated as unverifiable.
_TEAMS_CREDENTIAL_REJECT_STATUSES = frozenset({400, 401, 403})


async def _validate_teams_app_credentials(
    app_id: str, app_password: str, tenant_id: str
) -> str | None:
    """Check Azure Bot app credentials against Azure AD before they are stored.

    The client-credentials token exchange is the same call ``TeamsClient.connect``
    makes to prime its outbound token, so it is both the cheapest credential check
    available and exactly the one the channel performs at boot. Returns ``None``
    when Azure issues a token, or a short error code (``invalid_client``,
    ``unauthorized_client``, …) when it refuses the credentials. Azure-side
    trouble (5xx/429) and network failures propagate to the caller, which treats
    them as "unverifiable" rather than invalid — saves must not be blocked by
    being offline. Mirrors ``_validate_webex_token`` / ``_validate_discord_token``.

    Only the OAuth ``error`` code is surfaced, never ``error_description``: the
    description carries tenant ids, app ids, and a correlation id, none of which
    belong in a dashboard error string.
    """
    import aiohttp  # noqa: F811

    from kiro_crew.teams.client import (  # noqa: F811
        _TOKEN_SCOPE,
        _TOKEN_URL_TMPL,
        TEAMS_MULTITENANT_AUTHORITY,
    )

    # A blank tenant_id means a multi-tenant bot, whose token is issued by the Bot
    # Framework authority rather than a directory tenant. The default comes from
    # teams.client so the pre-store check and the running channel cannot disagree
    # about which authority a blank tenant means.
    authority = tenant_id.strip() or TEAMS_MULTITENANT_AUTHORITY
    timeout = aiohttp.ClientTimeout(total=_TOKEN_VERIFY_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(
            _TOKEN_URL_TMPL.format(tenant=authority),
            data={
                "grant_type": "client_credentials",
                "client_id": app_id,
                "client_secret": app_password,
                "scope": _TOKEN_SCOPE,
            },
        ) as resp:
            if 200 <= resp.status < 300:
                return None
            if resp.status in _TEAMS_CREDENTIAL_REJECT_STATUSES:
                desc = ""
                try:
                    data = await resp.json(content_type=None)
                    if isinstance(data, dict):
                        desc = str(data.get("error", "") or "")
                except Exception:
                    pass
                return (desc or f"HTTP {resp.status}")[:60]
            raise RuntimeError(f"teams credential verify http {resp.status}")


async def api_teams_config_save(request: web.Request) -> web.Response:
    """PUT /api/teams/config — persist the Teams secret (.env) + config (json).

    The app password (secret) is written ONLY to config_dir/.env
    (``MICROSOFT_APP_PASSWORD``, 0600); non-secret config (enabled, app_id,
    tenant_id, allowed_emails, thresholds) lives in config.json under the "teams"
    key. Remote sessions are read-only. Every field except ``session_folder`` is
    read at gateway startup, so an actual change returns ``restart_required``.
    """
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        CRED_MICROSOFT_APP_TENANT_ID,
        _threshold_pct,
        config_path,
        read_env_file_credential,
    )

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="teams.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(msg: str, status: int = 400) -> web.Response:
        _audit_denial(msg)
        return web.json_response({"error": msg}, status=status)

    def _reject(code: str, msg: str) -> web.Response:
        """Reject with a machine-readable ``code``; ``msg`` is advisory prose.

        A sibling of ``_deny`` rather than an extra parameter on it: the status is
        a literal here so the error-code contract gate can see the response, and
        the dashboard renders ``error`` verbatim into a localized UI, so prose
        alone would be untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only: a remote/tunneled session cannot alter
    # channel access or plant the Azure Bot secret.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate + stage (no partial writes). The secret goes to .env
    # only — never config.json — so the agent-readable config never holds it.
    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("app_password_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("app_password_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_MICROSOFT_APP_PASSWORD] = None
    else:
        raw = body.get("app_password")
        if isinstance(raw, str):
            secret = raw.strip()
            if secret.startswith(f"{CRED_MICROSOFT_APP_PASSWORD}="):
                secret = secret[len(CRED_MICROSOFT_APP_PASSWORD) + 1 :].strip()
            if secret:
                if any(ch.isspace() for ch in secret):
                    return _deny("app_password must not contain whitespace")
                env_updates[CRED_MICROSOFT_APP_PASSWORD] = secret

    staged: dict[str, object] = {}
    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        staged["enabled"] = val
    for str_key in ("app_id", "tenant_id"):
        if str_key in body:
            val = body.get(str_key)
            if not isinstance(val, str):
                return _deny(f"{str_key} must be a string")
            v = val.strip()
            if any(ch.isspace() for ch in v):
                return _deny(f"{str_key} must not contain whitespace")
            staged[str_key] = v
    if "allowed_emails" in body:
        try:
            new_ids = _clean_id_list(
                body.get("allowed_emails"), _is_valid_teams_principal, "principal"
            )
        except ValueError as exc:
            return _deny(str(exc))
        staged["allowed_emails"] = new_ids

    # Context-window nudge thresholds, on the shared validator.
    for pct_key in ("soft_threshold_pct", "hard_threshold_pct"):
        bad_pct = _threshold_pct_rejection(body, pct_key)
        if bad_pct is not None:
            return _reject(*bad_pct)
        if pct_key in body:
            staged[pct_key] = int(body[pct_key])

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))

    # ── Phase 1.5: verify the app credentials against Azure AD before storing.
    # Runs whenever any part of the credential triple changes: a mistyped App ID
    # or tenant is as fatal as a bad secret, and all three are checked by the one
    # token exchange. Rejection fails the save here, where the operator can act on
    # it, and writes nothing. A network failure is NOT a rejection — the save
    # proceeds with a warning so being offline never blocks config (mirrors the
    # Webex and Discord saves).
    #
    # The fallbacks come from a config snapshot taken OUTSIDE the config lock and
    # are advisory only: they decide what to verify, never what to write. The
    # authoritative read-modify-write is still Phase 2 under the lock. Resolution
    # order mirrors the boot path (slack/gateway.py) — the env credential wins —
    # so the check exercises the credentials the channel will actually use.
    verify_warning = ""
    #: The exact (app_id, password, tenant) Azure accepted, or None when nothing was
    #: verified. Re-confirmed under the config lock before anything is written.
    verified_triple: tuple[str, str, str] | None = None
    credential_touched = (
        CRED_MICROSOFT_APP_PASSWORD in env_updates or "app_id" in staged or "tenant_id" in staged
    )
    if credential_touched:
        # Read credentials directly from .env (+ os.environ override) rather
        # than via KiroCrewConfig.load(), which triggers _load_resolved() ->
        # cfg.save(), an unconditional migration write-back that purges
        # teams.app_password from config.json as a side-effect.  That
        # write-back races Phase 2's write-order invariant (SET: .env first,
        # then config purge) by clearing the legacy credential before the .env
        # write has succeeded.  read_env_file_credential touches only the .env
        # file and never writes config.json.
        _p15_cfg = config_path()
        _raw_teams_15: dict = {}
        try:
            # Offload read_text + json.loads to a thread so a slow filesystem
            # cannot stall the async event loop (Finding 1).
            def _read_config_15() -> dict:
                return json.loads(_p15_cfg.read_text(encoding="utf-8")) if _p15_cfg.exists() else {}

            _rd15 = await asyncio.to_thread(_read_config_15)
            # Guard against a malformed config.json where "teams" is not a dict
            # (e.g. someone hand-edited it to a list).  .get() on a list raises
            # AttributeError; the isinstance check degrades gracefully (Finding 3).
            _t15 = _rd15.get("teams")
            _raw_teams_15 = _t15 if isinstance(_t15, dict) else {}
        except Exception:
            pass
        _c_pw = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_PASSWORD)
        _c_app_id = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_ID)
        _c_tenant = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_TENANT_ID)
        # ENV-first, matching load_credentials() semantics: os.environ overrides
        # the .env file (Finding 2).  A pending in-flight update still wins as
        # the outermost layer (see env_updates.get() below).
        _c_pw = os.environ.get(CRED_MICROSOFT_APP_PASSWORD, "") or _c_pw
        _c_app_id = os.environ.get(CRED_MICROSOFT_APP_ID, "") or _c_app_id
        _c_tenant = os.environ.get(CRED_MICROSOFT_APP_TENANT_ID, "") or _c_tenant
        # ENV-first: env_updates wins, then .env/os.environ (_c_pw).  When the
        # password lives ONLY in legacy config.json (not in .env or os.environ,
        # so _c_pw is empty), fall back to the raw legacy value so verification
        # and Phase-2's purge-write see the existing credential and preserve it.
        _c_pw_effective = _c_pw or _raw_teams_15.get("app_password", "")
        eff_password = env_updates.get(CRED_MICROSOFT_APP_PASSWORD, _c_pw_effective)
        eff_app_id = _c_app_id or str(staged.get("app_id", _raw_teams_15.get("app_id", "")))
        eff_tenant = _c_tenant or str(staged.get("tenant_id", _raw_teams_15.get("tenant_id", "")))
        # Nothing to verify while half the pair is missing (e.g. the operator is
        # clearing the secret, or is filling the form in two saves).
        if eff_app_id and eff_password:
            try:
                teams_err = await _validate_teams_app_credentials(
                    eff_app_id, eff_password, eff_tenant
                )
            except Exception:
                verify_warning = (
                    "Azure was unreachable, so the credentials were saved " "without verification."
                )
            else:
                if teams_err:
                    return _reject(
                        "credentials_rejected",
                        f"credentials rejected by Azure ({teams_err})",
                    )
                verified_triple = (eff_app_id, eff_password, eff_tenant)

    # ── Phase 2: commit under the repo-wide config lock (read fresh, merge only
    # the teams section, write atomic) so a concurrent save is never clobbered.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return _deny("config.json is corrupt", status=500)
        if not isinstance(data.get("teams"), dict):
            data["teams"] = {}
        teams_cfg = data["teams"]

        if verified_triple is not None:
            # Re-derive the effective triple under the lock and refuse if what is about
            # to be STORED is not what Azure accepted. Optimistic, deliberately: the
            # verification is a network round trip and the config lock serializes EVERY
            # writer in the process, so holding it across that call would stall unrelated
            # saves and a hung endpoint would wedge them until the timeout. Verifying
            # outside and confirming inside costs one extra .env read and keeps the
            # invariant that nothing unverified is stored.
            #
            # The race it closes: two concurrent saves, one changing the app id and one
            # the secret. Each verifies a triple containing the OTHER's old value and
            # passes; the serialized commits then merge into a stored triple neither one
            # checked, and the channel is dead at the next restart with a green "Saved."
            # Read credentials directly from .env (same rationale as Phase 1.5:
            # avoid KiroCrewConfig.load() which would trigger migration write-back).
            _f_pw = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_PASSWORD)
            _f_app_id = await asyncio.to_thread(read_env_file_credential, CRED_MICROSOFT_APP_ID)
            _f_tenant = await asyncio.to_thread(
                read_env_file_credential, CRED_MICROSOFT_APP_TENANT_ID
            )
            # ENV-first, matching load_credentials() semantics (Finding 2).
            _f_pw = os.environ.get(CRED_MICROSOFT_APP_PASSWORD, "") or _f_pw
            _f_app_id = os.environ.get(CRED_MICROSOFT_APP_ID, "") or _f_app_id
            _f_tenant = os.environ.get(CRED_MICROSOFT_APP_TENANT_ID, "") or _f_tenant
            now_password = env_updates.get(
                CRED_MICROSOFT_APP_PASSWORD,
                _f_pw or str(teams_cfg.get("app_password", "")),
            )
            now_app_id = _f_app_id or str(staged.get("app_id", teams_cfg.get("app_id", "")))
            now_tenant = _f_tenant or str(staged.get("tenant_id", teams_cfg.get("tenant_id", "")))
            if (now_app_id, now_password, now_tenant) != verified_triple:
                return _reject(
                    "config_changed",
                    "the Teams credentials changed while these were being verified; "
                    "nothing was saved — reload and try again",
                )

        # Threshold ordering is checked against the EFFECTIVE pair (the staged
        # value, else what is stored, else the shipped default), because a request
        # may send only one half. Still commit-last: nothing is written yet.
        # Stored values go through the loader's own coercion so a hand-edited
        # config.json is read here exactly as the next load will read it.
        eff_soft = _threshold_pct(
            staged.get("soft_threshold_pct", teams_cfg.get("soft_threshold_pct")), 80
        )
        eff_hard = _threshold_pct(
            staged.get("hard_threshold_pct", teams_cfg.get("hard_threshold_pct")), 95
        )
        if eff_hard < eff_soft:
            # The loader would silently pull soft down to hard
            # (_normalize_threshold_pair), so an inverted pair is not a crash but
            # is never what the operator meant: the soft nudge would be
            # unreachable. Refuse it here instead of storing a value that reads
            # back different.
            return _reject(
                "threshold_pct_inverted",
                "hard_threshold_pct must be >= soft_threshold_pct",
            )

        changes: dict[str, object] = {}
        if "enabled" in staged and staged["enabled"] != bool(teams_cfg.get("enabled", False)):
            changes["enabled"] = staged["enabled"]
        for str_key in ("app_id", "tenant_id"):
            if str_key in staged and staged[str_key] != teams_cfg.get(str_key, ""):
                changes[str_key] = staged[str_key]
        for pct_key, pct_default in (("soft_threshold_pct", 80), ("hard_threshold_pct", 95)):
            if pct_key in staged and staged[pct_key] != _threshold_pct(
                teams_cfg.get(pct_key), pct_default
            ):
                changes[pct_key] = staged[pct_key]
        if "allowed_emails" in staged and staged["allowed_emails"] != teams_cfg.get(
            "allowed_emails", []
        ):
            changes["allowed_emails"] = staged["allowed_emails"]
        if "session_folder" in staged and staged["session_folder"] != str(
            teams_cfg.get("session_folder", "") or ""
        ):
            changes["session_folder"] = staged["session_folder"]
        applied = list(changes.keys())
        # The secret is env-only; if a legacy plaintext app_password ever landed
        # in config.json, purge it when the credential is safely held elsewhere:
        # either it is being written to .env in this same save, or it already
        # exists in .env / os.environ (so purging the config copy is safe).
        # Do NOT purge when the password lives ONLY in legacy config.json (no .env
        # entry, no env_update) — that would erase the sole credential copy and
        # produce a dead pair at the next restart (Finding 1).
        # _c_pw is only populated inside ``if credential_touched`` (Phase 1.5).
        # For metadata-only saves (credential_touched=False) fall back to a
        # synchronous os.environ check — load_credentials() seeds os.environ from
        # .env at startup, so the key is present when .env holds the credential.
        _pw_in_env_or_environ = locals().get("_c_pw") or os.environ.get(
            CRED_MICROSOFT_APP_PASSWORD, ""
        )
        _pw_safe_in_env = bool(
            CRED_MICROSOFT_APP_PASSWORD in env_updates  # being written this save
            or _pw_in_env_or_environ  # already in .env / os.environ
        )
        if teams_cfg.get("app_password") and _pw_safe_in_env:
            changes["app_password"] = ""
            applied.append("app_password_purged")

        _cfg_snapshot: str | None = None
        if changes:
            teams_cfg.update(changes)
            # Snapshot the on-disk config BEFORE writing the new metadata, so
            # that if the subsequent .env credential write fails we can roll
            # the metadata back.  Restoring config on .env failure keeps the
            # pair consistent (old credential + old meta).
            _cfg_snapshot = await asyncio.to_thread(_read_text_or_none, path)
            _atomic_json_write(path, data)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(teams_cfg.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "teams",
                    _folder_name,
                    relabel="session_folder" in changes,
                )
        if env_updates:
            # Off-loop: the .env write is blocking file IO (lock, temp write,
            # owner-only lockdown, replace) that would stall the gateway loop
            # if run inline.
            #
            # Cancellation guard: _write_env_off_loop shields + drains its
            # worker, so a CancelledError from it means the .env write has
            # already finished (either succeeded or failed). Roll config back
            # ONLY when the write actually failed; if it succeeded, the pair
            # is consistent and rolling back would create a mismatch.
            _env_write_task: asyncio.Task[None] = asyncio.ensure_future(
                _write_env_off_loop(env_updates)
            )
            try:
                await asyncio.shield(_env_write_task)
            except asyncio.CancelledError:
                # Drain to completion WITHOUT propagating, so we can inspect the
                # outcome and roll back before re-raising (a second shield() would
                # re-raise CancelledError before the rollback ran).
                await asyncio.gather(_env_write_task, return_exceptions=True)
                _env_exc = _env_write_task.exception() if not _env_write_task.cancelled() else None
                if _env_exc is not None:
                    # .env write failed — roll config back for consistency.
                    if changes:
                        if _cfg_snapshot is None:
                            await asyncio.to_thread(path.unlink, missing_ok=True)
                        else:
                            await asyncio.to_thread(
                                _atomic_json_write,
                                path,
                                json.loads(_cfg_snapshot),
                            )
                raise
            except BaseException:
                # Genuine .env write failure — roll the config metadata back so
                # a failed write cannot leave the NEW metadata paired with the
                # OLD credential on disk.
                if changes:
                    if _cfg_snapshot is None:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    else:
                        await asyncio.to_thread(
                            _atomic_json_write,
                            path,
                            json.loads(_cfg_snapshot),
                        )
                raise
            for key, new_val in env_updates.items():
                if new_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="teams.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(set(applied) - LIVE_RELOAD_FIELDS),
            "verify_warning": verify_warning,
        }
    )


async def api_webex_config_get(request: web.Request) -> web.Response:
    """GET /api/webex/config — read Webex config + masked secret status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WEBEX_BOT_TOKEN,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the device WebSocket is actually connected +
            # authorized this session — NOT merely "a token was present at
            # boot" or "the transport registered". Kept truthful by the
            # client's on_state_change observer (see maybe_start_webex).
            "connected": bool(getattr(state, "webex_connected", False)),
            # Short reason from the most recent connection failure ("" when
            # connected / untried).
            "connect_error": str(getattr(state, "webex_connect_error", ""))[:120],
            "configured": bool(token and cfg.webex.enabled and cfg.webex.allowed_emails),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "bot_token_set": bool(token),
            "bot_token_preview": _mask_secret(token),
            "enabled": cfg.webex.enabled,
            "allowed_emails": list(cfg.webex.allowed_emails),
            "allow_group_rooms": bool(cfg.webex.allow_group_rooms),
            "allowed_room_ids": list(cfg.webex.allowed_room_ids),
            "reply_in_thread": bool(cfg.webex.reply_in_thread),
            "soft_threshold_pct": int(cfg.webex.soft_threshold_pct),
            "hard_threshold_pct": int(cfg.webex.hard_threshold_pct),
            "session_folder": cfg.webex.session_folder,
        }
    )


async def api_webex_config_save(request: web.Request) -> web.Response:
    """PUT /api/webex/config — persist the Webex token (.env) + config (config.json).

    The whole Webex channel config is read at gateway startup, so every
    change returns ``restart_required`` for the UI hint.
    """
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WEBEX_BOT_TOKEN,
        WebexConfig,
        _normalize_threshold_pair,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="webex.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

    # Remote sessions are read-only (same gate as the Slack config API): a
    # remote or tunneled session cannot alter channel access or plant tokens.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes (no partial writes). ──
    env_updates: dict[str, str | None] = {}
    clear_flag = body.get("bot_token_clear")
    if clear_flag is not None and not isinstance(clear_flag, bool):
        return _deny("bot_token_clear must be a boolean")
    if clear_flag is True:
        env_updates[CRED_WEBEX_BOT_TOKEN] = None
    else:
        raw = body.get("bot_token")
        if isinstance(raw, str):
            tok = raw.strip()
            if tok.startswith(f"{CRED_WEBEX_BOT_TOKEN}="):  # accidentally pasted env line
                tok = tok[len(CRED_WEBEX_BOT_TOKEN) + 1 :].strip()
            if tok:
                if any(ch.isspace() for ch in tok):
                    return _deny("bot_token must not contain whitespace")
                env_updates[CRED_WEBEX_BOT_TOKEN] = tok

    # ── Phase 1 (continued): validate the config fields from the request
    # alone — the current config.json is NOT read here. The authoritative
    # read-modify-write happens entirely under the config lock in Phase 2,
    # so a concurrent save by another handler can never be clobbered by a
    # stale full-file snapshot.
    staged: dict[str, object] = {}

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        staged["enabled"] = val

    if "allowed_emails" in body:
        try:
            new_emails = _clean_id_list(body.get("allowed_emails"), _is_valid_webex_email, "email")
        except ValueError as exc:
            return _deny(str(exc))
        staged["allowed_emails"] = new_emails

    for flag in ("allow_group_rooms", "reply_in_thread"):
        if flag in body:
            val = body.get(flag)
            if not isinstance(val, bool):
                return _deny(f"{flag} must be a boolean")
            staged[flag] = val

    if "allowed_room_ids" in body:
        rooms = body.get("allowed_room_ids")
        if not isinstance(rooms, list) or not all(isinstance(r, str) for r in rooms):
            return _deny("allowed_room_ids must be a list of strings")
        # De-duplicated, order preserved, blanks dropped. Not otherwise validated:
        # a Webex room id is an opaque base64 blob whose shape is the platform's to
        # define, and a format guess here would reject a legitimate id from a
        # cluster this code has never seen.
        seen: set[str] = set()
        cleaned: list[str] = []
        for raw in rooms:
            room = raw.strip()
            if room and room not in seen:
                seen.add(room)
                cleaned.append(room)
        staged["allowed_room_ids"] = cleaned

    # Range-validated here; CLAMPED in Phase 2, where the locked fresh read
    # supplies the counterpart. Reading the config here instead would be both a
    # torn read and a side-effecting one: ``KiroCrewConfig.load()`` normalizes and
    # writes the file back, which materializes every default into config.json and
    # makes the next no-op save report a change.
    for name in ("soft_threshold_pct", "hard_threshold_pct"):
        if name in body:
            pct = body.get(name)
            if not isinstance(pct, int) or isinstance(pct, bool) or not (1 <= pct <= 100):
                return _deny(f"{name} must be an integer between 1 and 100")
            staged[name] = pct

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))

    # ── Phase 1.5: verify a newly pasted token against Webex before storing.
    # Network failure is NOT a rejection: the save proceeds with a warning so
    # being offline never blocks config. Mirrors the Slack token verification.
    verify_warning = ""
    pending_tok = env_updates.get(CRED_WEBEX_BOT_TOKEN)
    if pending_tok:
        try:
            webex_err = await _validate_webex_token(pending_tok)
        except Exception:
            verify_warning = "Webex was unreachable, so the token was saved without verification."
        else:
            if webex_err:
                return _deny(f"bot_token rejected by Webex ({webex_err})")

    # ── Phase 2: commit. All validation passed, so writes are safe. The
    # read-modify-write of config.json happens ENTIRELY under the repo-wide
    # config lock (read fresh, merge only the webex section, write atomic),
    # so a concurrent save by another settings handler is never overwritten
    # by a stale snapshot taken before the lock.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            return _deny("config.json is corrupt", status=500)
        if not isinstance(data.get("webex"), dict):
            data["webex"] = {}
        webex_cfg = data["webex"]

        # Reduce staged fields to actual changes against the fresh read so
        # restart_required stays truthful on no-op saves.
        #
        # Generic over ``staged`` rather than one branch per field. A hand-written
        # branch list silently DROPS any field added to Phase 1 without a matching
        # branch here — the whole write is ``webex_cfg.update(changes)``, so a
        # missing branch means the value validates, reports success, and is never
        # persisted. The dataclass supplies each field's default, so the
        # comparison is against the same value a fresh config would read.
        defaults = WebexConfig()
        # Clamp the thresholds as a PAIR through the same helper the config
        # dataclass uses, so a soft value above the hard one cannot make the soft
        # nudge unreachable -- ``_maybe_notice`` tests ``pct >= hard`` first. Done
        # here because clamping needs the counterpart, and only this fresh read
        # under the config lock has an untorn view of it.
        if "soft_threshold_pct" in staged or "hard_threshold_pct" in staged:

            def _pct(name: str) -> int:
                """This request's value for *name*, or the STORED counterpart.

                The stored side is coerced defensively: ``config.json`` is a file
                an operator can hand-edit, so a non-numeric counterpart would make
                saving the OTHER threshold raise out of the handler as a 500 and
                persist nothing — a value this request never mentioned breaking a
                value it did. A malformed stored number falls back to the dataclass
                default, which is the same thing the loader does with it.
                """
                if name in staged:
                    return int(cast(int, staged[name]))
                try:
                    return int(webex_cfg.get(name, getattr(defaults, name)))
                except (TypeError, ValueError):
                    return int(getattr(defaults, name))

            soft, hard = _normalize_threshold_pair(
                _pct("soft_threshold_pct"), _pct("hard_threshold_pct")
            )
            if "soft_threshold_pct" in staged:
                staged["soft_threshold_pct"] = soft
            if "hard_threshold_pct" in staged:
                staged["hard_threshold_pct"] = hard

        changes: dict[str, object] = {}
        for key, value in staged.items():
            stored = webex_cfg.get(key, getattr(defaults, key, None))
            if value != _coerce_like(value, stored):
                changes[key] = value
        applied = list(changes.keys())
        # Any token set/clear also purges the legacy config.json
        # ``webex.bot_token`` fallback so a stale plaintext copy can't shadow
        # (or outlive) the .env credential. The config write commits BEFORE
        # the .env write — if we crash between the two, the legacy copy is
        # already gone rather than resurrected.
        if CRED_WEBEX_BOT_TOKEN in env_updates and webex_cfg.get("bot_token"):
            changes["bot_token"] = ""
            applied.append("bot_token_purged")

        _cfg_snapshot: str | None = None
        if changes:
            webex_cfg.update(changes)
            # Snapshot the on-disk config BEFORE writing the new metadata, so
            # that if the subsequent .env credential write fails we can roll
            # the metadata back.  Restoring config on .env failure keeps the
            # pair consistent (old token + old meta).
            _cfg_snapshot = await asyncio.to_thread(_read_text_or_none, path)
            _atomic_json_write(path, data)

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(webex_cfg.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "webex",
                    _folder_name,
                    relabel="session_folder" in changes,
                )
        if env_updates:
            # Off-loop: the .env write is blocking file IO (lock, temp write,
            # owner-only lockdown, replace) and must not block the event loop.
            #
            # Cancellation guard: see Teams save for the full rationale. Only
            # roll config back when the .env write actually failed, not when
            # cancellation arrived after the write already committed.
            _env_write_task_wx: asyncio.Task[None] = asyncio.ensure_future(
                _write_env_off_loop(env_updates)
            )
            try:
                await asyncio.shield(_env_write_task_wx)
            except asyncio.CancelledError:
                await asyncio.gather(_env_write_task_wx, return_exceptions=True)
                _env_exc_wx = (
                    _env_write_task_wx.exception() if not _env_write_task_wx.cancelled() else None
                )
                if _env_exc_wx is not None:
                    if changes:
                        if _cfg_snapshot is None:
                            await asyncio.to_thread(path.unlink, missing_ok=True)
                        else:
                            await asyncio.to_thread(
                                _atomic_json_write, path, json.loads(_cfg_snapshot)
                            )
                raise
            except BaseException:
                # Roll config back so a failed .env write cannot leave the
                # NEW metadata paired with the OLD token on disk.
                if changes:
                    if _cfg_snapshot is None:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    else:
                        await asyncio.to_thread(_atomic_json_write, path, json.loads(_cfg_snapshot))
                raise
            # Keep the live process environment in sync (see the Slack save path).
            for key, new_val in env_updates.items():
                if new_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="webex.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire Webex channel config is read once at gateway startup.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(set(applied) - LIVE_RELOAD_FIELDS),
            "verify_warning": verify_warning,
        }
    )


# ── iMessage configuration API ──
# The only channel with NO credential to manage: the transport is the operator's
# own signed-in Messages.app, reached through a local bridge process, so there is
# nothing to mask, verify against a vendor, or write to .env. Everything lives in
# config.json under the "imessage" key, and the whole section is read once at
# gateway startup (only session_folder reloads live).


def _is_valid_imessage_handle(v: str) -> bool:
    """Accept an Apple ID email or a phone-shaped handle.

    Linear string ops, no regex: the same polynomial-backtracking concern that
    shaped ``_is_valid_webex_email`` applies to any pattern run over an
    operator-supplied list.

    A phone handle may carry the punctuation people actually type, spaces
    included -- ``normalize_handle`` strips it before any comparison, so
    rejecting ``+1 (555) 123-4567`` here would refuse a handle the transport
    treats as identical to the digits-only form.
    """
    if not v or len(v) > 254:
        return False
    if "@" in v:
        return _is_valid_webex_email(v)
    body = v[1:] if v.startswith("+") else v
    digits = [ch for ch in body if ch.isdigit()]
    # Anything outside digits and dialling punctuation is rejected, so a stray
    # identifier cannot be smuggled in as a "phone" and silently authorized.
    if any(not (ch.isdigit() or ch in "()-. ") for ch in body):
        return False
    return 4 <= len(digits) <= 18


def _clean_imessage_path(raw: object, label: str) -> str:
    """Validate an operator-supplied filesystem path for the bridge.

    The value becomes ``argv[0]`` (or a ``--db-path`` argument) of a spawned
    child. It is passed to ``create_subprocess_exec``, never a shell, so quoting
    is not the risk -- but a newline or NUL would corrupt the argv and is
    rejected rather than silently truncated.
    """
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValueError(f"{label} must be a string")
    value = raw.strip()
    if any(ch in value for ch in ("\n", "\r", "\x00")):
        raise ValueError(f"{label} must not contain line breaks")
    if len(value) > 4096:
        raise ValueError(f"{label} is too long")
    return value


async def api_imessage_config_get(request: web.Request) -> web.Response:
    """GET /api/imessage/config — read the iMessage channel config."""
    cfg = KiroCrewConfig.load()
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the bridge's watch is actually live this session —
            # NOT merely "enabled in config". Kept truthful by the client's
            # on_state_change observer (see maybe_start_imessage).
            "connected": bool(getattr(state, "imessage_connected", False)),
            "connect_error": str(getattr(state, "imessage_connect_error", ""))[:120],
            "configured": bool(cfg.imessage.enabled and cfg.imessage.allowed_handles),
            # The UI explains the macOS-only requirement instead of leaving the
            # operator to infer it from a channel that silently never connects.
            "supported": bool(IS_MACOS),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            "enabled": cfg.imessage.enabled,
            # No bridge path is exposed: the executable is resolved in code,
            # never from agent-writable config. See imessage.bridge_path.
            "db_path": cfg.imessage.db_path,
            "allowed_handles": list(cfg.imessage.allowed_handles),
            "service": cfg.imessage.service,
            "session_folder": cfg.imessage.session_folder,
        }
    )


async def api_imessage_config_save(request: web.Request) -> web.Response:
    """PUT /api/imessage/config — persist the iMessage config (config.json)."""
    # `_atomic_json_write` stays function-local, and NOT for the rule's
    # circular-import reason -- there is no cycle here (verified by importing
    # both orders). It is imported this way at seven sites in this module, six of
    # them pre-existing, so hoisting only this one would turn those six into F811
    # redefinitions of a module-scope name and drag six unrelated call sites into
    # this PR. Hoisting all seven belongs in its own change.
    from kiro_crew.agent import _atomic_json_write  # noqa: F811

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="imessage.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(code: str, msg: str) -> web.Response:
        """Reject a bad request. ``code`` is the contract, ``msg`` is advisory.

        The status is a literal rather than a parameter so this lands in the
        error-code gate's CHECKED bucket: a computed status puts a response in
        the unverifiable ``dynamic_status`` escape hatch, which the gate caps
        precisely because hoisting a status out of view looks like refactoring.
        The dashboard renders ``error`` verbatim into a localized UI, so prose
        alone would be untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only (same gate as every other channel config
    # API): a remote or tunneled session cannot widen who may reach the agent.
    if not is_direct_local_request(request):
        message = "read-only from remote sessions (local machine only)"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "remote_read_only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid_json", "invalid JSON")
    if not isinstance(body, dict):
        return _deny("body_not_object", "body must be an object")

    # ── Phase 1: validate everything and stage changes (no partial writes).
    # The current config.json is NOT read here; the authoritative
    # read-modify-write happens entirely under the config lock in Phase 2.
    staged: dict[str, object] = {}

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled_not_bool", "enabled must be a boolean")
        staged["enabled"] = val

    if "allowed_handles" in body:
        try:
            staged["allowed_handles"] = _clean_id_list(
                body.get("allowed_handles"), _is_valid_imessage_handle, "handle"
            )
        except ValueError as exc:
            return _deny("invalid_handle", str(exc))

    if "service" in body:
        val = body.get("service")
        if not isinstance(val, str) or val.strip().lower() not in IMESSAGE_SERVICES:
            return _deny("invalid_service", "service must be one of: imessage, sms, auto")
        staged["service"] = val.strip().lower()

    for key in ("db_path",):
        if key in body:
            try:
                staged[key] = _clean_imessage_path(body.get(key), key)
            except ValueError as exc:
                return _deny("invalid_path", str(exc))

    if "session_folder" in body:
        try:
            staged["session_folder"] = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny("invalid_session_folder", str(exc))

    # ── Phase 2: commit. The read-modify-write of config.json happens ENTIRELY
    # under the repo-wide config lock (read fresh, merge only the imessage
    # section, write atomic), so a concurrent save by another settings handler
    # is never overwritten by a stale snapshot taken before the lock.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    applied: list[str] = []
    async with _get_config_lock():
        path = config_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception:
            message = "config.json is corrupt"
            _audit_denial(message)
            return web.json_response({"error": message, "code": "config_corrupt"}, status=500)
        if not isinstance(data.get("imessage"), dict):
            data["imessage"] = {}
        imessage_cfg = data["imessage"]

        # Reduce staged fields to actual changes against the fresh read so
        # restart_required stays truthful on no-op saves.
        changes: dict[str, object] = {}
        if "enabled" in staged and staged["enabled"] != bool(imessage_cfg.get("enabled", False)):
            changes["enabled"] = staged["enabled"]
        if "allowed_handles" in staged and staged["allowed_handles"] != imessage_cfg.get(
            "allowed_handles", []
        ):
            changes["allowed_handles"] = staged["allowed_handles"]
        for key, default in (("service", "imessage"), ("db_path", "")):
            if key in staged and staged[key] != str(imessage_cfg.get(key, default) or default):
                changes[key] = staged[key]
        if "session_folder" in staged and staged["session_folder"] != str(
            imessage_cfg.get("session_folder", "") or ""
        ):
            changes["session_folder"] = staged["session_folder"]
        applied = list(changes.keys())

        if changes:
            imessage_cfg.update(changes)
            # Shield + drain so a cancellation arriving mid-write cannot
            # release the config lock while the worker thread is still
            # replacing the file (interleaved-write race, Finding 3).
            _cfg_write_task_im: asyncio.Task[None] = asyncio.ensure_future(
                asyncio.to_thread(_atomic_json_write, path, data)
            )
            try:
                await asyncio.shield(_cfg_write_task_im)
            except asyncio.CancelledError:
                await asyncio.gather(_cfg_write_task_im, return_exceptions=True)
                raise

        # Create the configured session folder now, on this user-initiated save,
        # so the reconcile path never has to write the folder store. Best-effort:
        # a failure leaves conversations unfiled until the next save.
        _folder_name = stored_folder_name(imessage_cfg.get("session_folder"))
        if _folder_name:
            _state = request.app.get("state")
            if _state is not None:
                await ensure_channel_folder(
                    _state,
                    "imessage",
                    _folder_name,
                    relabel="session_folder" in changes,
                )

    _sel().log_api_access(
        caller=caller,
        operation="imessage.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied),
    )
    # The entire iMessage channel config is read once at gateway startup.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(set(applied) - LIVE_RELOAD_FIELDS),
            "verify_warning": "",
        }
    )


# ── WeCom (企业微信) configuration API ──
# Mirrors the Telegram config API above with one structural difference: WeCom
# uses TWO credentials (WECOM_BOT_ID + WECOM_SECRET, both in config_dir/.env,
# 0600) instead of a single bot token. Non-secret config (enabled,
# allowed_users, soft_threshold_pct) lives in config.json under the "wecom"
# key. GET returns masked previews + presence booleans; raw values are
# write-only. The UI maps WECOM_SECRET onto the shared panel's primary secret
# ("bot_token") and WECOM_BOT_ID onto its second credential field ("bot_id").


def _is_valid_wecom_userid(v: str) -> bool:
    """WeCom userid shape check (linear string ops, no regex).

    WeCom userids are 1-64 chars: ASCII letters, digits, and ``.-_@`` — the
    same charset the WeCom admin console accepts. ASCII-only on purpose:
    ``str.isalnum()`` alone would admit Unicode letters/digits, which can
    never match a real WeCom userid and would sit in the allow-list looking
    authoritative. Fail closed on anything else (whitespace, display names,
    zero-width blobs).
    """
    if not v or len(v) > 64:
        return False
    return all((ch.isascii() and ch.isalnum()) or ch in "._-@" for ch in v)


async def api_wecom_config_get(request: web.Request) -> web.Response:
    """GET /api/wecom/config — read WeCom config + masked credential status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    bot_id = creds.get(CRED_WECOM_BOT_ID, "")
    secret = creds.get(CRED_WECOM_SECRET, "")
    wc = cfg.wecom
    userids = [
        str(u.get("userid")) for u in wc.allowed_users if isinstance(u, dict) and u.get("userid")
    ]
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only when the WS transport actually started this session —
            # NOT merely "credentials were present at boot".
            "connected": bool(getattr(state, "wecom_connected", False)),
            "connect_error": str(getattr(state, "wecom_connect_error", ""))[:120],
            # allowed_users is part of "configured" unless allow-all is on:
            # the transport fails closed and rejects every message while the
            # allow-list is empty (the owner fallback still needs a userid
            # entry to match on).
            "configured": bool(
                bot_id and secret and wc.enabled and (userids or wc.allow_all_users)
            ),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            # Primary secret slot of the shared panel = WECOM_SECRET.
            "bot_token_set": bool(secret),
            "bot_token_preview": _mask_secret(secret),
            # Second credential slot = WECOM_BOT_ID.
            "bot_id_set": bool(bot_id),
            "bot_id_preview": _mask_secret(bot_id),
            "enabled": bool(wc.enabled),
            # Explicit opt-in: every org member may DM the bot (allow-list
            # bypassed). Never inferred from an empty allow-list.
            "allow_all_users": bool(wc.allow_all_users),
            # Projected for the tag editor UI; the save path re-attaches the
            # stored display names to surviving entries.
            "allowed_user_ids": userids,
            "soft_threshold_pct": int(wc.soft_threshold_pct),
            "session_folder": wc.session_folder,
        }
    )


async def api_wecom_config_save(request: web.Request) -> web.Response:
    """PUT /api/wecom/config — persist WeCom secrets (.env) + config (config.json).

    Every WeCom field is read once at gateway startup (credentials, enabled
    flag, and allow-list are consumed when ``maybe_start_wecom`` builds the
    transport), so any actual change returns ``restart_required``.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` — this handler read-modify-writes the shared
    ``.env`` / ``config.json`` stores, so interleaving with ANY other config
    writer would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        return await _wecom_config_save_locked(request)


async def _wecom_config_save_locked(request: web.Request) -> web.Response:
    """Body of the WeCom save; caller holds ``_get_config_lock()``."""
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_WECOM_BOT_ID,
        CRED_WECOM_SECRET,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400, *, code: str = "") -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="wecom.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        # ``code`` is optional: most rejections in this handler are prose-only, and a
        # machine-readable code is added per field as one is retrofitted. The dashboard
        # renders ``error`` verbatim into a localized UI, so prose alone is
        # untranslatable by construction (RFC 9457 3.1.3).
        payload: dict[str, Any] = {"error": msg}
        if code:
            payload["code"] = code
        return web.json_response(payload, status=status)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with
    # a valid dashboard token) cannot alter WeCom access or plant credentials.
    if not is_direct_local_request(request):
        return _deny("read-only from remote sessions (local machine only)", status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON")
    if not isinstance(body, dict):
        return _deny("body must be an object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    # Two independent credential slots, each with the same set/clear contract
    # as the single-token channels (clear wins over a simultaneously-sent value).
    for field_key, clear_key, cred_key, label in (
        ("bot_token", "bot_token_clear", CRED_WECOM_SECRET, "bot secret"),
        ("bot_id", "bot_id_clear", CRED_WECOM_BOT_ID, "bot ID"),
    ):
        clear_flag = body.get(clear_key)
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{clear_key} must be a boolean")
        if clear_flag is True:
            env_updates[cred_key] = None
            continue
        raw = body.get(field_key)
        if isinstance(raw, str):
            cred_val = raw.strip()
            if cred_val.startswith(f"{cred_key}="):  # accidental env line paste
                cred_val = cred_val[len(cred_key) + 1 :].strip()
            if cred_val:
                if any(ch.isspace() for ch in cred_val):
                    return _deny(f"{label} must not contain whitespace")
                if len(cred_val) > 256:
                    return _deny(f"{label} is implausibly long")
                env_updates[cred_key] = cred_val

    # Config → config.json under "wecom" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop. Reading under _get_config_lock() keeps the snapshot current
    # relative to every other config writer.
    path = config_path()

    def _read_config() -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    try:
        data = await asyncio.to_thread(_read_config)
    except Exception:
        return _deny("config.json is corrupt", status=500)
    if not isinstance(data.get("wecom"), dict):
        # Back-compat: seed from a COPY of the legacy "wechat" section (the
        # config key was renamed) so an existing install's allow-list /
        # thresholds / ws_url survive the first dashboard save instead of
        # being reset. Copy so the legacy block is never mutated in place.
        legacy = data.get("wechat")
        data["wecom"] = dict(legacy) if isinstance(legacy, dict) else {}
    wc_cfg = data["wecom"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    if "enabled" in body:
        val = body.get("enabled")
        if not isinstance(val, bool):
            return _deny("enabled must be a boolean")
        if val != bool(wc_cfg.get("enabled", False)):
            staged["enabled"] = val
            applied.append("enabled")

    if "allow_all_users" in body:
        val = body.get("allow_all_users")
        if not isinstance(val, bool):
            return _deny("allow_all_users must be a boolean")
        if val != bool(wc_cfg.get("allow_all_users", False)):
            staged["allow_all_users"] = val
            applied.append("allow_all_users")

    if "allowed_user_ids" in body:
        raw_ids = body.get("allowed_user_ids")
        if not isinstance(raw_ids, list):
            return _deny("allowed_user_ids must be a list")
        # Preserve stored display names for entries that survive the edit —
        # the UI round-trips only userids, but ``{userid, name}`` is the
        # canonical config shape consumed by the transport allow-list.
        existing = {
            str(u.get("userid")): u
            for u in wc_cfg.get("allowed_users", [])
            if isinstance(u, dict) and u.get("userid")
        }
        new_users: list[dict] = []
        seen: set[str] = set()
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not _is_valid_wecom_userid(s):
                return _deny(f"invalid WeCom userid: {s}")
            if s in seen:
                continue
            seen.add(s)
            new_users.append(existing.get(s) or {"userid": s, "name": ""})
        if new_users != list(wc_cfg.get("allowed_users", [])):
            staged["allowed_users"] = new_users
            applied.append("allowed_users")

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != int(wc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc))
        if new_folder != str(wc_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # No Phase 1.5 credential verification: validating WeCom credentials
    # requires opening the AI-bot WebSocket long-connection (no cheap REST
    # "whoami" like Telegram's getMe), so credentials are stored as given and
    # the status badge reports the truth after the next gateway restart.

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    _cfg_snapshot: str | None = None
    if staged:
        wc_cfg.update(staged)
        # Snapshot the on-disk config BEFORE writing the new metadata, so
        # that if the subsequent .env credential write fails we can roll
        # the metadata back.  Restoring config on .env failure keeps the
        # pair consistent (old credentials + old meta).
        _cfg_snapshot = await asyncio.to_thread(_read_text_or_none, path)
        # Off-loop: the atomic write (temp file + fsync + replace) must not
        # block the gateway event loop.
        await asyncio.to_thread(_atomic_json_write, path, data)

    # Create the configured session folder now, on this user-initiated save,
    # so the reconcile path never has to write the folder store. Best-effort:
    # a failure leaves conversations unfiled until the next save.
    _folder_name = stored_folder_name(wc_cfg.get("session_folder"))
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "wecom",
                _folder_name,
                relabel="session_folder" in staged,
            )
    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        #
        # Cancellation guard: see Teams save for the full rationale. Only
        # roll config back when the .env write actually failed, not when
        # cancellation arrived after the write already committed.
        _env_write_task_wc: asyncio.Task[None] = asyncio.ensure_future(
            _write_env_off_loop(env_updates)
        )
        try:
            await asyncio.shield(_env_write_task_wc)
        except asyncio.CancelledError:
            await asyncio.gather(_env_write_task_wc, return_exceptions=True)
            _env_exc_wc = (
                _env_write_task_wc.exception() if not _env_write_task_wc.cancelled() else None
            )
            if _env_exc_wc is not None:
                if staged:
                    if _cfg_snapshot is None:
                        await asyncio.to_thread(path.unlink, missing_ok=True)
                    else:
                        await asyncio.to_thread(_atomic_json_write, path, json.loads(_cfg_snapshot))
            raise
        except BaseException:
            # Roll config back so a failed .env write cannot leave the NEW
            # metadata paired with the OLD credentials on disk.
            if staged:
                if _cfg_snapshot is None:
                    await asyncio.to_thread(path.unlink, missing_ok=True)
                else:
                    await asyncio.to_thread(_atomic_json_write, path, json.loads(_cfg_snapshot))
            raise
        # Keep the live process environment in sync with the new .env state
        # (load_credentials() lets os.environ win over .env — see the Slack
        # save handler for the full rationale).
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val

    _sel().log_api_access(
        caller=caller,
        operation="wecom.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire WeCom channel config is read once at gateway startup.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(staged.keys() - LIVE_RELOAD_FIELDS),
            "verify_warning": "",
        }
    )


# ── Feishu (飞书/Lark) configuration API ──
# Same two-credential shape as WeCom: FEISHU_APP_ID + FEISHU_APP_SECRET live in
# config_dir/.env (0600), non-secret config (enabled, allowed_open_ids,
# allow_group, allowed_group_ids, soft_threshold_pct, session_folder) lives in
# config.json under the "feishu" key. GET returns masked previews + presence
# booleans; raw values are write-only. The UI maps FEISHU_APP_SECRET onto the
# shared panel's primary secret ("bot_token") and FEISHU_APP_ID onto its second
# credential field ("bot_id").
#
# Group access is a SEPARATE axis from the DM allow-list here (unlike WeCom's
# allow-all switch): allow_group gates group chats at all, and allowed_group_ids
# names which ones. Both fail closed — allow_group with an empty list serves no
# group, which is why the panel shows a hint rather than silently doing nothing.


# Channels whose SDK ships as an optional extra rather than in core. Maps the
# channel to (import name, extra name) so the panel can report BOTH whether the
# SDK is importable by this gateway process and the exact command that installs
# it into this interpreter. Teams (PyJWT) and WhatsApp (neonize) have the same
# shape and are one entry each when their panels adopt the card.
_CHANNEL_SDK_EXTRA: dict[str, tuple[str, str]] = {
    "feishu": ("lark_oapi", "feishu"),
}


def _channel_sdk_status(channel: str) -> tuple[bool, bool, str]:
    """``(installed, install_supported, install_command)`` for *channel*'s extra.

    Computed HERE rather than read off the connection badge, because the badge
    cannot answer it in the case that matters. ``maybe_start_feishu`` returns at
    its first line when the channel is disabled, and the ``ImportError`` branch
    that records the missing SDK sits after that return — so a user who has not
    yet flipped the enable toggle gets no hint at all, and one who has must
    restart the gateway before the hint appears. This endpoint answers either
    way and without a restart.

    ``install_command`` is empty when it would be useless or actively wrong: the
    SDK is already importable, or no install channel exists in this
    build/interpreter (see :func:`_pip_install_channel_available` — on the
    bundled desktop interpreter a pip install writes into the code-signed bundle
    and is discarded on the next app update, so naming the command there is bad
    advice rather than merely unhelpful).

    Blocking: ``find_spec`` and the PEP 668 marker check both touch the
    filesystem, so call it from a worker thread on an async path.
    """
    entry = _CHANNEL_SDK_EXTRA.get(channel)
    if entry is None:
        # No optional extra for this channel: nothing is ever missing, so the
        # panel renders no card.
        return True, False, ""
    import_name, extra = entry
    if importlib.util.find_spec(import_name) is not None:
        return True, True, ""
    if not _pip_install_channel_available():
        return False, False, ""
    return False, True, pip_extra_install_command(extra)


def _is_valid_feishu_id(v: str, prefix: str) -> bool:
    """Feishu opaque-id shape check (linear string ops, no regex).

    Feishu ids are a fixed prefix (``ou_`` for a user open_id, ``oc_`` for a
    group chat_id) followed by an opaque ASCII alphanumeric body. Only the
    prefix and the charset are asserted, never a length equality: the body
    length is not contractual and a stricter check would reject valid ids from a
    future tenant. ASCII-only on purpose — ``str.isalnum()`` alone admits
    Unicode digits, which can never match a real Feishu id and would sit in the
    allow-list looking authoritative. Fail closed on anything else (whitespace,
    display names, a pasted @-mention, zero-width blobs).
    """
    if not v.startswith(prefix) or len(v) > 128:
        return False
    body = v[len(prefix) :]
    if not body:
        return False
    return all(ch.isascii() and ch.isalnum() for ch in body)


async def api_feishu_config_get(request: web.Request) -> web.Response:
    """GET /api/feishu/config — read Feishu config + masked credential status."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        KiroCrewConfig,
    )

    # Off-loop: both calls are synchronous filesystem reads (config.json, then
    # .env) and the settings panel polls this endpoint every 15s, so on slow or
    # contended storage they would stall every other task on the gateway loop
    # rather than just this request. Read as ONE unit of work: the credential
    # read is a method on the config object, and splitting them into two hops
    # would let the two files be read either side of a concurrent save.
    def _read() -> "tuple[KiroCrewConfig, dict, tuple[bool, bool, str]]":
        loaded = KiroCrewConfig.load()
        # The SDK probe joins this same unit of work: find_spec and the PEP 668
        # marker are filesystem reads, and the panel polls this endpoint every
        # 15s, so giving them their own thread hop would double the cost of a
        # poll for no isolation benefit.
        return loaded, loaded.load_credentials(), _channel_sdk_status("feishu")

    cfg, creds, (sdk_installed, sdk_supported, sdk_command) = await asyncio.to_thread(_read)
    app_id = creds.get(CRED_FEISHU_APP_ID, "")
    app_secret = creds.get(CRED_FEISHU_APP_SECRET, "")
    fs = cfg.feishu
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only while the WS receiver thread is alive this session —
            # NOT merely "credentials were present at boot". A refused app ends
            # that thread within seconds, which flips this back to false.
            "connected": bool(getattr(state, "feishu_connected", False)),
            "connect_error": str(getattr(state, "feishu_connect_error", ""))[:120],
            # allowed_open_ids is part of "configured": the transport fails
            # closed and rejects every DM while the allow-list is empty, so a
            # credentialed + enabled channel with no ids is not yet usable.
            "configured": bool(app_id and app_secret and fs.enabled and fs.allowed_open_ids),
            # Remote sessions get a read-only view: config edits (PUT) are
            # loopback-only, so the UI disables all inputs and hides Save.
            "read_only": not is_direct_local_request(request),
            # Primary secret slot of the shared panel = FEISHU_APP_SECRET.
            "bot_token_set": bool(app_secret),
            "bot_token_preview": _mask_secret(app_secret),
            # Second credential slot = FEISHU_APP_ID.
            "bot_id_set": bool(app_id),
            "bot_id_preview": _mask_secret(app_id),
            "enabled": bool(fs.enabled),
            "allowed_user_ids": list(fs.allowed_open_ids),
            "allow_group": bool(fs.allow_group),
            "allowed_group_ids": list(fs.allowed_group_ids),
            "soft_threshold_pct": int(fs.soft_threshold_pct),
            "session_folder": fs.session_folder,
            # The channel needs lark-oapi, which ships as the optional [feishu]
            # extra. False means the gateway process cannot import it and the
            # channel will be skipped at boot no matter how complete the rest of
            # this config is.
            "sdk_installed": sdk_installed,
            # False in the three environments where a pip install cannot work
            # (bundled desktop interpreter, no pip module, PEP 668
            # externally-managed): the panel shows an unsupported notice instead
            # of a command that would silently achieve nothing.
            "sdk_install_supported": sdk_supported,
            # Names THIS gateway's interpreter, because installing into the
            # wrong environment is the actual failure mode. Empty when the SDK is
            # present or no install channel exists.
            "sdk_install_command": sdk_command,
        }
    )


async def api_feishu_config_save(request: web.Request) -> web.Response:
    """PUT /api/feishu/config — persist Feishu secrets (.env) + config (config.json).

    Every Feishu field is read once at gateway startup (credentials, enabled
    flag, and both allow-lists are consumed when ``maybe_start_feishu`` builds
    the transport), so any actual change returns ``restart_required``.

    Serialized with every other config.json writer via the repository-wide
    ``_get_config_lock()`` — this handler read-modify-writes the shared
    ``.env`` / ``config.json`` stores, so interleaving with ANY other config
    writer would silently lose writes.
    """
    # circular import: agents imports from dashboard.handlers at module load
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        return await _feishu_config_save_locked(request)


async def _feishu_config_save_locked(request: web.Request) -> web.Response:
    """Body of the Feishu save; caller holds ``_get_config_lock()``."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_FEISHU_APP_ID,
        CRED_FEISHU_APP_SECRET,
        ConfigReadError,
        config_path,
        update_config_locked,
    )

    caller = request.get("user", "dashboard")

    def _audit_denial(msg: str) -> None:
        _sel().log_api_access(
            caller=caller,
            operation="feishu.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )

    def _deny(msg: str, *, code: str) -> web.Response:
        """Reject a bad request. ``code`` is the contract, ``msg`` is advisory.

        400 is a literal rather than a parameter, and the 403/500 replies below
        are written out at their own call sites for the same reason: a computed
        status puts a response in the error-code gate's unverifiable
        ``dynamic_status`` bucket, which the gate caps precisely because hoisting
        a status out of view looks like refactoring. The dashboard renders
        ``error`` verbatim into a localized UI, so prose alone would be
        untranslatable by construction (RFC 9457 3.1.3).
        """
        _audit_denial(msg)
        return web.json_response({"error": msg, "code": code}, status=400)

    # Remote sessions are read-only: config writes are accepted only from the
    # machine running the gateway, so a remote or tunneled session (even with a
    # valid dashboard token) cannot widen Feishu access or plant credentials.
    if not is_direct_local_request(request):
        message = "read-only from remote sessions (local machine only)"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "remote_read_only"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return _deny("invalid JSON", code="invalid_json")
    if not isinstance(body, dict):
        return _deny("body must be an object", code="body_not_object")

    # ── Phase 1: validate everything and stage changes. No writes happen until
    # all validation passes, so a rejected field never leaves partial state. ──

    env_updates: dict[str, str | None] = {}
    # Two independent credential slots, each with the same set/clear contract
    # as the single-token channels (clear wins over a simultaneously-sent value).
    for field_key, clear_key, cred_key, label in (
        ("bot_token", "bot_token_clear", CRED_FEISHU_APP_SECRET, "app secret"),
        ("bot_id", "bot_id_clear", CRED_FEISHU_APP_ID, "app ID"),
    ):
        clear_flag = body.get(clear_key)
        if clear_flag is not None and not isinstance(clear_flag, bool):
            return _deny(f"{clear_key} must be a boolean", code="clear_flag_not_bool")
        if clear_flag is True:
            env_updates[cred_key] = None
            continue
        raw = body.get(field_key)
        if isinstance(raw, str):
            cred_val = raw.strip()
            if cred_val.startswith(f"{cred_key}="):  # accidental env line paste
                cred_val = cred_val[len(cred_key) + 1 :].strip()
            if cred_val:
                if any(ch.isspace() for ch in cred_val):
                    return _deny(
                        f"{label} must not contain whitespace", code="credential_whitespace"
                    )
                if len(cred_val) > 256:
                    return _deny(f"{label} is implausibly long", code="credential_too_long")
                env_updates[cred_key] = cred_val

    # Config → config.json under "feishu" (staged, applied only after Phase 1).
    # Off-loop read: a large or slow config.json must not stall the gateway
    # event loop. Reading under _get_config_lock() keeps the snapshot current
    # relative to every other config writer.
    path = config_path()

    def _read_config() -> dict:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def _corrupt_config() -> web.Response:
        message = "config.json is corrupt"
        _audit_denial(message)
        return web.json_response({"error": message, "code": "config_corrupt"}, status=500)

    try:
        data = await asyncio.to_thread(_read_config)
    except Exception:
        return _corrupt_config()
    # A readable file whose TOP LEVEL is not an object (a hand-edited `[]`) is the
    # same class of problem as an unreadable one, and gets the same answer: the
    # alternative is `data.get` raising AttributeError into a 500 with a stack
    # trace and no indication of what to fix.
    if not isinstance(data, dict):
        return _corrupt_config()
    if not isinstance(data.get("feishu"), dict):
        data["feishu"] = {}
    fs_cfg = data["feishu"]
    staged: dict[str, object] = {}
    applied: list[str] = []

    def _stored_list(key: str) -> list:
        """Stored value only when it really is a list, else empty.

        ``dict.get(key, default)`` substitutes the default only for an ABSENT key:
        a hand-edited ``"allowed_open_ids": null`` returns None and would reach
        ``list(None)``, raising into a 500 from the save that is the way to repair
        the file. Mirrors the loader, which already coerces this shape for the
        runtime.
        """
        value = fs_cfg.get(key)
        return value if isinstance(value, list) else []

    def _stored_int(key: str, default: int) -> int:
        """Stored value only when it really is an int, else the default.

        ``bool`` is excluded deliberately: it is an ``int`` subclass, so a stored
        ``true`` would otherwise compare as the threshold 1.
        """
        value = fs_cfg.get(key)
        return value if isinstance(value, int) and not isinstance(value, bool) else default

    for flag_key in ("enabled", "allow_group"):
        if flag_key in body:
            val = body.get(flag_key)
            if not isinstance(val, bool):
                return _deny(f"{flag_key} must be a boolean", code="flag_not_bool")
            if val != bool(fs_cfg.get(flag_key, False)):
                staged[flag_key] = val
                applied.append(flag_key)

    # Both id lists share one validator, differing only in prefix. The wire name
    # for the DM list is the shared panel's ``allowed_user_ids``; on disk it is
    # ``allowed_open_ids``, which is what the transport reads.
    for wire_key, cfg_key, prefix, what in (
        ("allowed_user_ids", "allowed_open_ids", "ou_", "Feishu open_id"),
        ("allowed_group_ids", "allowed_group_ids", "oc_", "Feishu group chat_id"),
    ):
        if wire_key not in body:
            continue
        raw_ids = body.get(wire_key)
        if not isinstance(raw_ids, list):
            return _deny(f"{wire_key} must be a list", code="ids_not_list")
        new_ids: list[str] = []
        for item in raw_ids:
            s = str(item).strip()
            if not s:
                continue
            if not _is_valid_feishu_id(s, prefix):
                return _deny(f"invalid {what}: {s}", code="invalid_id")
            if s not in new_ids:
                new_ids.append(s)
        if new_ids != _stored_list(cfg_key):
            staged[cfg_key] = new_ids
            applied.append(cfg_key)

    bad_pct = _threshold_pct_rejection(body, "soft_threshold_pct")
    if bad_pct is not None:
        return _deny(bad_pct[1], code=bad_pct[0])
    if "soft_threshold_pct" in body:
        pct = int(body["soft_threshold_pct"])
        if pct != _stored_int("soft_threshold_pct", 80):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    if "session_folder" in body:
        try:
            new_folder = clean_session_folder(body.get("session_folder"))
        except ValueError as exc:
            return _deny(str(exc), code="invalid_session_folder")
        if new_folder != str(fs_cfg.get("session_folder", "") or ""):
            staged["session_folder"] = new_folder
            applied.append("session_folder")

    # No Phase 1.5 credential verification: a REST tenant-token probe would have
    # to pick a domain (open.feishu.cn vs open.larksuite.com) and would report a
    # false failure for whichever tenant it guessed wrong. Credentials are stored
    # as given, and the badge reports receiver liveness after the next restart —
    # a refused app ends the receiver thread within seconds, so a wrong secret
    # surfaces as "not connected" with a reason rather than silence.

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    #
    # Through ``update_config_locked``, not ``write_config_atomically``: it holds an
    # advisory lock on the sidecar ``<path>.lock`` for the entire read-modify-write,
    # so a concurrent ``kirocrew config set`` in ANOTHER PROCESS cannot land between
    # our read and our write. ``_get_config_lock()`` (held by the caller) only
    # serializes writers inside this one, and loader.py names that combination the
    # required path for a new config.json mutation.
    #
    # ``staged`` is applied to the config as re-read INSIDE the lock rather than to
    # the snapshot taken during validation, so a concurrent edit to an unrelated
    # section is preserved instead of being replaced by our older copy.
    prior_feishu: dict | None = None

    def _apply_staged(fresh: dict) -> dict:
        nonlocal prior_feishu
        section = fresh.get("feishu")
        # Captured HERE because this is the only point at which the pre-mutation
        # state is known to be current.
        prior_feishu = dict(section) if isinstance(section, dict) else None
        if not isinstance(section, dict):
            section = {}
            fresh["feishu"] = section
        section.update(staged)
        return fresh

    def _restore_feishu(fresh: dict) -> dict:
        """Undo the keys THIS request wrote, and only where we still own them.

        Two narrower than the obvious form, both deliberate. Rewriting the file
        from a whole-file snapshot would revert whatever a concurrent writer
        landed; restoring the whole ``feishu`` SECTION would still discard a
        concurrent ``kirocrew config set feishu.*`` that arrived between our write
        and this rollback. So the comparison is per key: a key whose stored value
        is no longer the value we wrote has been changed by someone else since,
        and reverting it would destroy their edit to undo ours.
        """
        section = fresh.get("feishu")
        if not isinstance(section, dict):
            # Nothing of ours left to undo (the section is gone or was replaced
            # wholesale by another writer).
            return fresh
        before = prior_feishu if isinstance(prior_feishu, dict) else {}
        for key, written in staged.items():
            if section.get(key) != written:
                continue  # not ours any more
            if key in before:
                section[key] = before[key]
            else:
                section.pop(key, None)
        # Drop a section that only ever existed because we created it, so a failed
        # first-time save leaves no empty scaffold behind.
        if prior_feishu is None and not section:
            fresh.pop("feishu", None)
        return fresh

    async def _rollback_config() -> None:
        try:
            await asyncio.to_thread(
                functools.partial(update_config_locked, path, mutate=_restore_feishu)
            )
        except Exception:
            # A rollback that cannot run must not mask the original failure the
            # caller is already raising; the mismatch is logged instead.
            logger.exception("Feishu config rollback failed; config may lead .env")

    if staged:
        # Off-loop: the locked read-modify-write does file IO and may block on a
        # concurrent holder of the lock, neither of which belongs on the loop.
        try:
            await asyncio.to_thread(
                functools.partial(update_config_locked, path, mutate=_apply_staged)
            )
        except ConfigReadError:
            return _corrupt_config()

    if env_updates:
        # Off-loop: the .env write is blocking file IO (lock, temp write,
        # owner-only lockdown, replace) and must not block the event loop.
        #
        # Cancellation guard: see the WeCom save for the full rationale. Only
        # roll config back when the .env write actually failed, not when
        # cancellation arrived after the write already committed.
        _env_write_task_fs: asyncio.Task[None] = asyncio.ensure_future(
            _write_env_off_loop(env_updates)
        )
        try:
            await asyncio.shield(_env_write_task_fs)
        except asyncio.CancelledError:
            await asyncio.gather(_env_write_task_fs, return_exceptions=True)
            _env_exc_fs = (
                _env_write_task_fs.exception() if not _env_write_task_fs.cancelled() else None
            )
            if _env_exc_fs is not None and staged:
                await _rollback_config()
            raise
        except BaseException:
            # Roll config back so a failed .env write cannot leave the NEW
            # metadata paired with the OLD credentials on disk.
            if staged:
                await _rollback_config()
            raise
        # Keep the live process environment in sync with the new .env state
        # (load_credentials() lets os.environ win over .env — see the Slack save
        # handler for the full rationale).
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val

    # Create the configured session folder now, on this user-initiated save, so
    # the reconcile path never has to write the folder store. Best-effort: a
    # failure leaves conversations unfiled until the next save.
    #
    # AFTER the credential write, not before: the .env write can fail (or be
    # cancelled) and the config write above is rolled back when it does, but a
    # folder that has already been created, renamed or unhidden is NOT rolled
    # back. Reconciling here means a save that reported failure leaves no durable
    # folder change behind.
    # The staged value when we changed it, else what was already stored: `fs_cfg`
    # is the VALIDATION snapshot and is no longer mutated in place, since the
    # authoritative update now happens inside the lock.
    _effective_folder = staged.get("session_folder", fs_cfg.get("session_folder"))
    _folder_name = stored_folder_name(_effective_folder)
    if _folder_name:
        _state = request.app.get("state")
        if _state is not None:
            await ensure_channel_folder(
                _state,
                "feishu",
                _folder_name,
                relabel="session_folder" in staged,
            )

    _sel().log_api_access(
        caller=caller,
        operation="feishu.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # The entire Feishu channel config is read once at gateway startup.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(staged.keys() - LIVE_RELOAD_FIELDS),
            "verify_warning": "",
        }
    )
