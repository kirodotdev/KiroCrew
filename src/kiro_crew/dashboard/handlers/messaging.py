"""Messaging handlers — spawn, notifications, send-message, slack profile."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from kiro_crew import platform_compat
from kiro_crew.browser.auth import ensure as browser_auth_ensure
from kiro_crew.browser.command_bus import (
    DEFAULT_COMMAND_TIMEOUT_MS,
    DEFAULT_DRAIN_WAIT_MS,
    NoPanelError,
    QueueFullError,
    get_command_bus,
)
from kiro_crew.browser.screencast import BROWSER_FRAME_EVENT, build_frame_payload
from kiro_crew.browser.setup import (
    BROWSER_ENGINES,
    BROWSER_FIRST_USE_NOTE,
    browser_mode_enabled,
    deregister_playwright_proxy,
    ensure_playwright_installed,
    generate_playwright_config,
    get_browser_engine,
    get_extension_token,
    has_playwright_extension,
    is_playwright_installed,
    register_playwright_proxy,
    set_browser_engine,
    set_browser_mode_enabled,
)
from kiro_crew.cron import CronStoreBusy
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.chat_utils import _remove_queued_by_id, dashboard_slot_key
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.origin import is_direct_local_request, is_loopback
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    DashboardState,
)
from kiro_crew.notifications.bus import (
    NotificationPayload,
    NotificationValidationError,
)
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
from kiro_crew.session_pid_sig import verify_session_pid
from kiro_crew.slack.format import build_options_blocks, extract_options
from kiro_crew.subagent_persistence import _agent_dir, read_state
from kiro_crew.validation import (
    _EMOJI_NAME_RE,
    CHANNEL_ID_RE,
    CRON_SESSION_RE,
    SPAWN_RUN_SCHEMA,
    ValidationError,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Subagents ──


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
    # Batch/wave identity (transport-layer params from spawn_run MCP, like
    # approval_mode/silent above): validated inline, bounded, never LLM-schema.
    batch_id = str(body.get("batch_id", "") or "")[:32]
    if batch_id and not batch_id.isalnum():
        return web.json_response({"error": "batch_id must be alphanumeric"}, status=400)
    try:
        batch_total = max(0, min(int(body.get("batch_total", 0) or 0), 1000))
    except (TypeError, ValueError):
        batch_total = 0
    info = state.subagents.spawn(
        task,
        parent_session_key=parent_session,
        agent=agent,
        max_turns=max_turns,
        cwd=cwd,
        model=model or None,
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
        return web.json_response({"error": info.error, "counted": True}, status=400)
    resp: dict[str, object] = {"id": info.id, "task": task, "status": "spawned"}
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
        return web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400
        )
    task = str(body.get("task", "") or "").strip()
    if not task:
        return web.json_response(
            {"error": "task is required", "code": "task_required"}, status=400
        )
    parent_session = str(body.get("parent_session", "") or "")
    agent = str(body.get("agent", "") or "")
    model = str(body.get("model", "") or "")
    try:
        max_turns = max(0, min(int(body.get("max_turns", 0) or 0), 1000))
    except (TypeError, ValueError):
        max_turns = 0
    info = state.subagents.continue_conversation(
        conv_id,
        task,
        parent_session_key=parent_session,
        agent=agent,
        model=model or None,
        max_turns=max_turns,
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
            return web.json_response(
                {"error": info.error, "code": "conversation_busy"}, status=409
            )
        if info.error.startswith("conversation_gone"):
            return web.json_response(
                {"error": info.error, "code": "conversation_gone"}, status=404
            )
        return web.json_response(
            {"error": info.error, "code": "spawn_rejected"}, status=400
        )
    return web.json_response(
        {"id": info.id, "conversation": conv_id, "status": "spawned"}
    )


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
        return web.json_response(
            {"error": "invalid JSON", "code": "invalid_json"}, status=400
        )
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
            return web.json_response(
                {"error": detail, "code": "not_found"}, status=404
            )
        if detail.startswith("not_running"):
            return web.json_response(
                {"error": detail, "code": "not_running"}, status=409
            )
        if detail.startswith("session_starting"):
            # Transient: the run is alive but its session has not registered
            # yet (#1113). 503 + Retry-After tells clients to retry, unlike
            # the terminal 502 steer_failed.
            return web.json_response(
                {"error": detail, "code": "session_starting"},
                status=503,
                headers={"Retry-After": "5"},
            )
        return web.json_response(
            {"error": detail, "code": "steer_failed"}, status=502
        )
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
            return web.json_response(
                {"error": detail, "code": "conversation_busy"}, status=409
            )
        return web.json_response(
            {"error": detail, "code": "conversation_gone"}, status=404
        )
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
        return web.json_response({"error": "'ids' array required", "code": "ids_required"}, status=400)
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
    return web.json_response(data)


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
    info = state.subagents.spawn(
        old._raw_task or old.task,
        parent_session_key=old.parent_session_key,
        agent=old.agent,
        max_turns=old.max_turns,
        cwd=old.cwd,
        model=old.model or None,
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
            except CronStoreBusy:
                # Store transiently contended — the notification-level unack
                # below still succeeds; the acked-item trim is best-effort.
                logger.warning("unack_job skipped: cron store busy (job %s)", n["job_id"])
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
    # token-verified ``app:<name>`` source is enforced. The middleware sets
    # ``request["app"]`` only on app-token auth; the internal-secret path
    # (the MCP tool) never does.
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


async def api_send_message(request: web.Request) -> web.Response:
    """POST /api/send-message — send a message to Slack and/or dashboard."""
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
        if not isinstance(thread_ts, str) or not re.match(r"^\d+\.\d+$", thread_ts):
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
    # This covers all downstream paths (session injection, fallback, Slack).
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
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
            {"error": "user not in allowlist — configure allowed_users in config.json"}, status=403
        )

    sent_slack = False
    slack_ts: str | None = None
    sent_session = False
    target_session = body.get("session")
    job_name = None
    slack_attempted = False
    slack_error = ""
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
        #     _rehydrate_slot_from_history restores it from disk, tab reappears
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
        # Validate the cron session format before trusting it to escalate
        # routing from notification-only to owner Slack DM — a malformed or
        # injected value must not abuse that upgrade.
        is_cron_caller = bool(CRON_SESSION_RE.match(caller_session))
        send_to_slack = (
            target_session == "slack" or bool(target_channel) or bool(target_user) or is_cron_caller
        )
        if target_session == "slack":
            target_session = None
        if target_session:
            slot_key, job_name = _resolve_session_target(state, target_session, caller_session)
            if slot_key:
                # Resolve the origin slot. get_slot is the hot path (fast,
                # O(1) dict lookup). On miss, _rehydrate_slot_from_history
                # restores from disk if the session exists and isn't closed.
                # Truly-gone sessions (never persisted, deleted, or closed)
                # return None and delivery falls through to the Slack DM
                # path below — no phantom empty tab is ever created.
                slot = state.get_slot(slot_key)
                was_loaded = slot is not None
                if slot is None:
                    slot = _rehydrate_slot_from_history(state, slot_key)
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
                    if slot.running:
                        if len(slot._queue) >= 50:
                            evicted = slot.queue_pop(0)
                            logger.warning(
                                "Queue full for slot %s — evicting oldest message", slot_key
                            )
                            _remove_queued_by_id(slot.messages, evicted["id"])
                        qid = slot.queue_append(wrapped)
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

                        slot.append("inject", wrapped, inject_cls)
                        task = spawn_guarded_turn(state, slot, _run_chat(state, slot, wrapped))
                        slot.task = task
                        state.push_slots_update()
                    sent_session = True
        # Fall back to normal delivery if no session target or session is gone
        if not sent_session:
            if target_session and job_name:
                safe_name, _ = redact_exfiltration_urls(job_name)
                safe_name, _ = redact_credentials(safe_name)
                title = f"⏰ {safe_name}"
                text += "\n\n_(session closed — delivered as notification)_"
            state.notify("agent", title, text)
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
                                    await state.slack_client.post_blocks(
                                        channel,
                                        build_options_blocks(options),
                                        text,
                                        thread_ts=thread_ts,
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
            base_res = (
                f"target_channel={target_channel} target_user={target_user}"
                if (target_channel or target_user)
                else ("session=origin" if sent_session else "fallback=owner_dm")
            )
            _sel().log_tool_invocation(
                session_key="dashboard",
                tool_name="send_message",
                outcome=(
                    "completed" if sent_slack or sent_session or not slack_attempted else "error"
                ),
                downstream_service=(
                    "session" if sent_session else ("slack" if sent_slack else "dashboard")
                ),
                resources=base_res + thread_hint,
            )
        except Exception:
            logger.warning("SEL logging failed for send_message", exc_info=True)
    if slack_attempted and not sent_slack:
        safe_error, _ = redact_credentials(slack_error)
        safe_error, _ = redact_exfiltration_urls(safe_error)
        return web.json_response(
            {"ok": False, "error": f"Slack delivery failed: {safe_error}", "slack": False},
            status=502,
        )
    # A: report the actual delivery channel so callers (and the read-back
    # steering) can distinguish a real Slack post from a notification-only
    # send. "ok: true" alone previously masked notification-only outcomes.
    if sent_session:
        delivered_to = "session"
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
    if not channel or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)

    ts = body.get("ts", "")
    if action in ("add", "remove"):
        if not isinstance(ts, str) or not re.match(r"^\d+\.\d+$", ts):
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
    if not channel or not CHANNEL_ID_RE.match(channel):
        return web.json_response({"error": "invalid channel ID format"}, status=400)
    ts = body.get("ts", "")
    if not isinstance(ts, str) or not re.match(r"^\d+\.\d+$", ts):
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
    import time  # noqa: F811

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


async def api_browser_event(request: web.Request) -> web.Response:
    """POST /api/browser-event — receive browser activity events from MCP and broadcast via WS."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    event_type = body.get("event", "")
    if not event_type:
        return web.json_response({"error": "event is required"}, status=400)
    # Broadcast to all connected WS clients
    payload = {"type": "browser_event", "event": event_type, "ts": time.time()}
    # Forward all extra fields from the body, redacting string values
    for k, v in body.items():
        if k not in ("type", "event", "ts"):
            if isinstance(v, str):
                v, _ = redact_credentials(v)
                v, _ = redact_exfiltration_urls(v)
            payload[k] = v
    state.broadcast_ws("browser_event", payload)
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_event",
        outcome="completed",
        downstream_service="browser",
    )
    return web.json_response({"ok": True})


def _resolve_browse_session_key(host_pid: Any) -> str:
    """Resolve the authoritative session key for a browse frame from the posting
    proxy's ``host_pid``, walking process ancestors and verifying each one's
    gateway-signed ``session_pid_<pid>.txt`` sidecar.

    Warm-pool ``kiro-cli`` workers are pre-spawned before a slot is assigned, so
    ``KIROCREW_SESSION_KEY`` is never in their env and the Playwright proxy's
    frozen-env key (``session_key`` in the POST body) is empty. The reliable
    source is the signed pid->key mapping the gateway publishes on session claim
    — the same per-turn mechanism every managed MCP tool resolves (see
    ``kiro_crew.mcp_core._resolve_session_key``). The proxy's immediate parent
    (``kiro-cli-chat``) has no PID file, so we walk up to the ``kiro-cli`` worker
    that does. ``verify_session_pid`` requires the HMAC sidecar (agents cannot
    forge it) and binds the pid into the MAC, so a wrong/forged pid can't cross a
    session boundary. Returns ``""`` when no ancestor has a verifiable mapping.
    """
    try:
        pid = int(host_pid)
    except (TypeError, ValueError):
        return ""
    seen: set[int] = set()
    steps = 0
    # Bounded walk: guards against a cycle (seen) or a pathological chain (steps).
    while pid > 1 and pid not in seen and steps < 40:
        seen.add(pid)
        steps += 1
        key = verify_session_pid(pid)
        if key:
            return key
        try:
            pid = platform_compat.get_ppid(pid)
        except Exception:
            break
    return ""


async def api_browser_frame(request: web.Request) -> web.Response:
    """POST /api/browser/frame — receive a browse screenshot and rebroadcast it.

    The Playwright MCP proxy POSTs each screenshot it already captured (loopback
    only) as ``{"data": "<base64>", "format": "jpeg", ...}``; we normalize it and
    broadcast a ``browser_frame`` WS event for the BrowserLiveView panel. No CDP
    debug port is involved — this rides the proxy's existing capture path.

    Loopback-gated: the proxy runs on the same host, and frames carry a live view
    of the (authenticated) browse session, so off-host posts are refused.
    """
    if not is_loopback(request.remote or ""):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_frame",
            outcome="denied",
            downstream_service="browser",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only"}, status=403)
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_frame",
            outcome="invalid_input",
            downstream_service="browser",
            resources="invalid-json",
        )
        return web.json_response({"error": "invalid JSON"}, status=400)
    payload = build_frame_payload(body if isinstance(body, dict) else {})
    if payload is None:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_frame",
            outcome="invalid_input",
            downstream_service="browser",
            resources="no-frame-data",
        )
        return web.json_response({"error": "no frame data"}, status=400)
    # Stamp the AUTHORITATIVE session key resolved from the posting proxy's host
    # pid (gateway-signed session_pid sidecar), overriding the proxy's frozen-env
    # key which is empty under the warm pool. This is what lets the client-side
    # panel (scoped by frameSessionKey === sessionKey) render the mirror and
    # keeps a background session's frames from surfacing in the wrong panel. When
    # no ancestor has a verifiable mapping we leave the proxy-provided fallback
    # (empty on warm pool → client drops it, same as before — never worse).
    resolved_key = await asyncio.to_thread(
        _resolve_browse_session_key,
        body.get("host_pid") if isinstance(body, dict) else None,
    )
    if resolved_key:
        # verify_session_pid returns the FULL namespaced session key
        # (e.g. "dashboard:chat-70-<ts>"), but the client panel filters frames
        # by `frame.session_key === activeSlot`, where activeSlot is the BARE
        # slot key ("chat-70-<ts>"). Without stripping the "dashboard:" prefix
        # every frame is dropped on the mismatch and the mirror never renders.
        # (Same normalization as the Slack slot-key resolution below.)
        payload["session_key"] = resolved_key.removeprefix("dashboard:")
    state.broadcast_ws(BROWSER_FRAME_EVENT, payload)
    # Label the audit event by frame origin so the proxy's active pump frames are
    # distinguishable from agent-initiated screenshots. Bounded to a known set so
    # the SEL field can't carry arbitrary caller-supplied text.
    frame_source = body.get("source") if isinstance(body, dict) else None
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_frame",
        outcome="completed",
        downstream_service="browser",
        source=frame_source if frame_source in ("agent", "pump") else "agent",
    )
    # Report the live WS-client count so the proxy's active pump can back off
    # (stop self-issuing screenshots) when no dashboard is actually watching.
    return web.json_response({"ok": True, "subscribers": state.ws_client_count()})


async def api_browser_pump_audit(request: web.Request) -> web.Response:
    """POST /api/browser/pump-audit — audit a proxy active-pump screenshot injection.

    The active pump (``mcp_playwright_proxy``) injects its own
    ``browser_take_screenshot`` into the Playwright subprocess to keep the live
    mirror current between agent screenshots. That proxy is a stdlib-only stdio
    subprocess and cannot reach ``sel.py``, so it reports each injection here and
    the gateway emits the SEL tool-invocation event on its behalf — keeping
    proxy-internal tool calls auditable. Loopback-gated; the ``X-Internal-Secret``
    is enforced by the token_auth middleware (this path is in ``internal_paths``).
    """
    if not is_loopback(request.remote or ""):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_take_screenshot",
            outcome="denied",
            downstream_service="browser",
            source="pump",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only"}, status=403)
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_take_screenshot",
        outcome="injected",
        downstream_service="browser",
        source="pump",
    )
    return web.json_response({"ok": True})


async def api_browser_command(request: web.Request) -> web.Response:
    """POST /api/browser/command — run one op against the native browser panel.

    Called by the Playwright MCP proxy. Body:
    ``{"op": str, "host_pid": int, "session_key"?: str, "args"?: object, "timeout_ms"?: int}``.
    The session is resolved from ``host_pid`` (signed session_pid sidecar, same as
    ``api_browser_frame``); ``session_key`` is only a fallback for per-session
    spawns. Enqueues the op on the command bus and awaits the native panel's
    result.

    Responses:
    - 200 ``{"id", "ok": true, "result": <any>}`` — op ran and succeeded;
    - 200 ``{"id", "ok": false, "error": str}`` — op ran but failed;
    - 503 ``{"error": "no-native-panel", "code": "no_native_panel"}`` — no Electron poller registered for
      ``session_key`` (returned FAST, no wait, so the proxy falls back to
      Playwright);
    - 504 ``{"error": "timeout", "code": "timeout"}`` — the panel did not answer in time.

    Loopback-gated like ``api_browser_frame``, AND requires proven
    ``X-Internal-Secret`` auth. Membership in the strict-internal path set is NOT
    sufficient on its own: that middleware still admits a loopback dashboard
    *cookie* caller, so a browser-credentialed page could otherwise drive,
    intercept or forge native-browser operations. ``request["internal_auth"]`` is
    set only on the validated ``X-Internal-Secret`` path — exactly the transport
    the MCP proxy and the Electron main process use.
    """
    if not is_loopback(request.remote or "") or request.get("internal_auth") is not True:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="denied",
            downstream_service="browser",
            resources="non-loopback",
        )
        return web.json_response({"error": "loopback only", "code": "loopback_only"}, status=403)
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="invalid_input",
            downstream_service="browser",
            resources="invalid-json",
        )
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    fallback_key = body.get("session_key")
    op = body.get("op")
    args = body.get("args")
    timeout_ms = body.get("timeout_ms")
    if not isinstance(op, str) or not op:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="invalid_input",
            downstream_service="browser",
            resources="missing op",
        )
        return web.json_response({"error": "op required", "code": "op_required"}, status=400)
    if args is not None and not isinstance(args, dict):
        return web.json_response({"error": "args must be an object", "code": "args_must_be_object"}, status=400)
    if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
        timeout_ms = DEFAULT_COMMAND_TIMEOUT_MS
    # Resolve the AUTHORITATIVE session key from the posting proxy's host pid
    # (gateway-signed session_pid sidecar), overriding the proxy's frozen-env key
    # which is EMPTY under the warm pool -- the same resolution api_browser_frame
    # does. Strip the "dashboard:" prefix so the key matches the BARE slot key the
    # Electron panel registers via command-drain and dispatches on (see
    # api_browser_frame for the identical normalization). The proxy-provided
    # session_key is only a fallback for per-session spawns whose pid does not
    # resolve.
    resolved_key = await asyncio.to_thread(
        _resolve_browse_session_key,
        body.get("host_pid"),
    )
    if resolved_key:
        session_key = resolved_key.removeprefix("dashboard:")
    elif isinstance(fallback_key, str):
        session_key = fallback_key
    else:
        session_key = ""
    if not session_key:
        # No identifiable session -> no panel we could address. Answer like the
        # no-panel case (503) so the proxy falls back to Playwright, NOT 400: a
        # 400 surfaces a hard MCP error to the agent instead of the graceful
        # mirror path, and a warm-pool worker on a remote/non-Electron host (no
        # sidecar to resolve) legitimately reaches here on every browser_* call.
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="no_panel",
            downstream_service="browser",
            resources=op,
        )
        return web.json_response({"error": "no-native-panel", "code": "no_native_panel"}, status=503)
    bus = get_command_bus()
    try:
        outcome = await bus.submit(session_key, op, args or {}, timeout_ms=timeout_ms)
    except NoPanelError:
        # Fast path: no live native panel. The proxy falls back to Playwright.
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="no_panel",
            downstream_service="browser",
            resources=op,
        )
        return web.json_response({"error": "no-native-panel", "code": "no_native_panel"}, status=503)
    except QueueFullError:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="queue_full",
            downstream_service="browser",
            resources=op,
        )
        return web.json_response({"error": "queue-full", "code": "queue_full"}, status=429)
    except asyncio.TimeoutError:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command",
            outcome="timeout",
            downstream_service="browser",
            resources=op,
        )
        return web.json_response({"error": "timeout", "code": "timeout"}, status=504)
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_command",
        outcome="completed" if outcome.get("ok") else "failed",
        downstream_service="browser",
        resources=op,
    )
    response: dict[str, Any] = {"id": outcome.get("id"), "ok": bool(outcome.get("ok"))}
    if outcome.get("ok"):
        response["result"] = outcome.get("result")
    else:
        response["error"] = outcome.get("error") or "error"
    return web.json_response(response)


async def api_browser_command_drain(request: web.Request) -> web.Response:
    """POST /api/browser/command-drain — long-poll for a queued browser command.

    Called by the Electron main process. Body:
    ``{"session_keys": [str, ...], "wait_ms"?: int}``.

    SIDE EFFECT: registers ``session_keys`` as having a live native panel (TTL
    ~2x ``wait_ms``); this registration is what ``/api/browser/command`` checks
    to decide whether to 503.

    Responses:
    - 200 ``{"id", "session_key", "op", "args"}`` — a command is available;
    - 204 empty — nothing arrived within ``wait_ms``.

    Loopback-gated exactly like ``api_browser_frame``.
    """
    if not is_loopback(request.remote or "") or request.get("internal_auth") is not True:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command_drain",
            outcome="denied",
            downstream_service="browser",
            resources="non-loopback",
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
        return web.json_response({"error": "session_keys must be a list of strings", "code": "session_keys_invalid"}, status=400)
    wait_ms = body.get("wait_ms")
    if not isinstance(wait_ms, int) or isinstance(wait_ms, bool) or wait_ms <= 0:
        wait_ms = DEFAULT_DRAIN_WAIT_MS
    bus = get_command_bus()
    command = await bus.drain(session_keys, wait_ms=wait_ms)
    if command is None:
        return web.Response(status=204)
    return web.json_response(command)


async def api_browser_command_result(request: web.Request) -> web.Response:
    """POST /api/browser/command-result — post a native browser command's result.

    Called by the Electron main process. Body:
    ``{"id": str, "ok": bool, "result"?: <any>, "error"?: str}``.

    Responses:
    - 200 ``{"ok": true}`` — the result was matched to a waiting command;
    - 404 ``{"error": "unknown-command", "code": "unknown_command"}`` — the id already timed out or never
      existed.

    Loopback-gated exactly like ``api_browser_frame``.
    """
    if not is_loopback(request.remote or "") or request.get("internal_auth") is not True:
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_command_result",
            outcome="denied",
            downstream_service="browser",
            resources="non-loopback",
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
        return web.json_response({"error": "unknown-command", "code": "unknown_command"}, status=404)
    return web.json_response({"ok": True})


async def api_browser_auth_retry(request: web.Request) -> web.Response:
    """POST /api/browser-auth-retry — retry browser auth."""
    state: DashboardState = request.app["state"]
    try:
        result = await asyncio.to_thread(browser_auth_ensure)
        state.broadcast_browser_event("auth_retry", result)
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_auth_retry",
            outcome="completed",
            downstream_service="browser",
            resources="auth_retry",
        )
        return web.json_response(result)
    except Exception as exc:
        logger.warning("browser-auth-retry failed: %s", exc, exc_info=True)
        _sel().log_tool_invocation(
            session_key="dashboard",
            tool_name="browser_auth_retry",
            outcome="error",
            downstream_service="browser",
            resources=f"error={exc}",
        )
        return web.json_response({"error": str(exc)}, status=500)


async def api_browser_config_get(request: web.Request) -> web.Response:
    """GET /api/browser/config — browser mode, engine, extension mode, token."""
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_config_get",
        outcome="completed",
        downstream_service="browser",
    )
    return web.json_response(
        {
            "enabled": browser_mode_enabled(),
            "engine": get_browser_engine(),
            "engines": list(BROWSER_ENGINES),
            "extension_mode": has_playwright_extension(),
            "token": get_extension_token() is not None,
            "installed": is_playwright_installed(),
        }
    )


async def api_browser_config_save(request: web.Request) -> web.Response:
    """PUT /api/browser/config — save browser mode, engine, extension, token.

    On a fresh enable this also downloads ``@playwright/mcp`` and the selected
    engine's browser binary (bootstrapping Node if needed). The install runs off
    the event loop and its result is reported in the body — a failed install
    never 500s, so the persisted preference and an actionable ``code`` reach the
    UI instead of a blank error.
    """
    # Enabling Browser Mode is a keystone-level authorization (registration mounts
    # the browser_* tools, and in attach mode drives the operator's real logged-in
    # browser). An APP TOKEN must not be able to self-grant it — an app token
    # yields a truthy request["user"] too, so gate on the empty app identity,
    # mirroring the computer-use keystone save. Every denial emits a SEL event.
    if request.get("app"):
        _sel().log_api_access(
            caller=f"app:{request.get('app')}",
            operation="browser_config_save",
            outcome="denied",
            source="browser_config_api",
            error="app tokens may not enable Browser Mode",
        )
        return web.json_response(
            {"ok": False, "code": "dashboard_user_required"},
            status=403,
        )

    body = await request.json()

    extension_mode = body.get("extension_mode", False)
    token = body.get("token", "")
    # Strict boolean: a truthy non-bool (``"false"``, ``1``, ``"off"``) must NOT
    # enable a security capability. Only a real JSON ``true`` enables Browser Mode.
    enabled = body.get("enabled", False) is True

    engine = body.get("engine", get_browser_engine())
    if engine not in BROWSER_ENGINES:
        return web.json_response(
            {"ok": False, "code": "invalid_engine", "engine": engine},
            status=400,
        )

    # Persist preferences + regenerate the engine config UNDER the in-process
    # config lock, then release it before the long installer and the proxy
    # register/deregister. The lock's job is narrow: make the durable engine and
    # ``playwright-config.json`` move as one unit so two Settings tabs saving
    # different engines can't interleave and leave the persisted engine disagreeing
    # with the config the launcher reads (worker A persists firefox, worker B
    # persists webkit, then A's slower generate_playwright_config lands last and
    # writes a firefox config under the webkit preference — wrong browser). It is
    # the same repo-wide config lock the messaging and MCP writers take.
    #
    # It is deliberately NOT held across register/deregister: those serialize on
    # their OWN inter-process ``mcp.lock`` file lock, and holding an asyncio lock
    # across that blocking wait would couple the two locks — a wedged ``mcp.lock``
    # would then freeze every config.json writer (Slack, MCP sync, computer-use)
    # dashboard-wide. Registration reads only the enable + extension flags (never
    # the engine or config.json), so releasing the config lock first cannot make
    # the proxy entry disagree with the persisted engine.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # noqa: F811

    async with _get_config_lock():
        # Read the current enable BEFORE mutating so the session reset below fires
        # only on a real transition (inside the lock, so it cannot race the write).
        enabled_before = browser_mode_enabled()
        await asyncio.to_thread(
            _persist_browser_preferences,
            enabled=enabled,
            engine=engine,
            extension_mode=extension_mode,
            token=token,
        )

    return await _browser_config_finalize(
        request,
        enabled=enabled,
        engine=engine,
        extension_mode=extension_mode,
        enabled_before=enabled_before,
    )


def _persist_browser_preferences(
    *, enabled: bool, engine: str, extension_mode: Any, token: str
) -> None:
    """Write the durable browser preferences + engine config (holds the config lock).

    Synchronous file writes only, dispatched via ``asyncio.to_thread`` by the
    caller inside ``_get_config_lock()``. Persisting the enable/engine flags first
    means they survive even if the later install fails — the enable state lives in
    a data-home flag, not per-session React state, which is what makes it durable
    across restart.
    """
    from kiro_crew.config.loader import data_home  # noqa: F811

    kirocrew_dir = data_home()
    kirocrew_dir.mkdir(parents=True, exist_ok=True)
    flag_file = kirocrew_dir / "playwright-extension-mode"
    token_file = kirocrew_dir / "playwright-extension-token"

    set_browser_mode_enabled(enabled)
    set_browser_engine(engine)

    if extension_mode:
        flag_file.touch()
        if token:
            fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
    else:
        flag_file.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)

    # Regenerate the launched-browser config so the persisted engine actually
    # takes effect (the proxy launches `--config <playwright-config.json>`, whose
    # ``browserName`` is the ONLY place the engine reaches Playwright). Also
    # creates the file for a dashboard-only user who never ran the CLI setup, so
    # `--config` never points at a missing path. Kept in the SAME locked unit as
    # set_browser_engine so the pair is atomic against a concurrent save.
    if enabled:
        generate_playwright_config(engine)


async def _browser_config_finalize(
    request: web.Request,
    *,
    enabled: bool,
    engine: str,
    extension_mode: Any,
    enabled_before: bool,
) -> web.Response:
    """Install, (de)register the proxy, and reset sessions — no config lock held."""
    # Download @playwright/mcp + the engine browser on enable. Run the installer
    # whenever Browser Mode is on, NOT gated on launcher resolvability: `npx`
    # being on PATH means the package can be fetched, not that the OS/arch browser
    # binary is on disk, so gating on it would skip the one step that downloads
    # the browser. The installer itself skips the npm install when a launcher
    # already resolves and `playwright install` is an idempotent fast no-op when
    # the browser is present, so a re-save stays cheap. Blocking (subprocess +
    # network), so it runs off the event loop.
    #
    # ``ensure_playwright_installed`` is contracted never to raise, but enabling
    # Browser Mode must NEVER 500 or dump a raw install error at the user, so this
    # is belt-and-suspenders: any unexpected exception becomes a calm advisory in
    # the payload (Browser Mode stays on; the browser downloads on first use).
    install_result: dict[str, Any] | None = None
    if enabled:
        try:
            install_result = await asyncio.to_thread(ensure_playwright_installed, engine)
        except Exception:
            logger.exception("browser provisioning raised unexpectedly; deferring to first use")
            install_result = {
                "ok": True,
                "step": "browser-deferred",
                "detail": BROWSER_FIRST_USE_NOTE,
                "engine": engine,
            }

    # Tool availability is the gate (there is no per-message marker): enabling
    # REGISTERS the proxy so the browser_* tools appear in the agent's tool list;
    # disabling DEREGISTERS it so they disappear and "off" actually prevents
    # browser operation. Both go through the setup helpers, which hold the shared
    # mcp.json lock (so a concurrent app-bridge or dashboard MCP write is not
    # clobbered), refuse to touch a user-authored non-proxy entry under the
    # canonical key, and create/rewrite the file safely. Blocking (file lock +
    # disk I/O), so off the event loop.
    #
    # The preferences above are already persisted, so an mcp.json-level failure is
    # reported in the payload rather than raised — a 500 here would tell the user
    # nothing was saved when the flag/engine files were in fact written.
    try:
        if enabled:
            _, mcp_status = await asyncio.to_thread(register_playwright_proxy)
        else:
            _, mcp_status = await asyncio.to_thread(deregister_playwright_proxy)
    except OSError as exc:
        logger.warning("browser config: MCP registration failed: %s", exc)
        mcp_status = "registration-failed"

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_config_save",
        outcome="completed",
        downstream_service="browser",
        resources=(
            f"enabled={enabled} engine={engine} extension_mode={extension_mode} "
            f"mcp={mcp_status}"
        ),
    )

    # Flipping the enable changes the agent's tool surface (register mounts the
    # browser_* tools, deregister removes them), and kiro-cli caches ``tools/list``
    # for the LIFETIME of a session — ACP has no ``tools/list_changed`` push. Reset
    # active sessions on the transition, the same primitive ``POST /api/mcp/sync``
    # and the computer-use keystone use. Without this, DISABLING leaves the live
    # session holding browser tools (the security-relevant direction: settings say
    # off while browsing still works), and enabling shows "0 browser tools" until
    # some later cold session. Only on a real change: a re-save with the same value
    # must not tear down the user's session.
    sessions_reset = 0
    if enabled != enabled_before:
        from kiro_crew.dashboard.handlers.sessions import _reset_all_sessions

        try:
            sessions_reset = await _reset_all_sessions(request)
        except Exception:
            # The preferences already landed and were audited; a reset failure must
            # not report the SAVE as failed. Worst case is the prior behavior — the
            # new tool surface applies on the next cold session.
            logger.exception("browser config saved, but session reset failed")

    # ``mcp_status`` is "kept-user-entry" when the caller's own hand-authored
    # Playwright server was left in place — the preferences were still saved,
    # but KiroCrew's proxy was deliberately NOT written over their config.
    payload: dict[str, Any] = {
        "ok": True,
        "mcp_status": mcp_status,
        "enabled": enabled,
        "engine": engine,
        "sessions_reset": sessions_reset,
    }
    if install_result is not None:
        payload["install"] = install_result
    return web.json_response(payload)


# ── Slack configuration API ──
# Secrets (bot/app token, owner id) live in config_dir/.env (0600). Non-secret
# config (slash command, allowlists, behavior toggles) lives in config.json
# under the "slack" key. GET returns masked previews + presence booleans.
# Raw token values are write-only: no API path returns them (rotate at
# api.slack.com or read .env on the machine itself if ever needed).

#: Public field name → .env credential key for the two Slack secrets.
_SLACK_SECRET_FIELDS = {
    "bot_token": "SLACK_BOT_TOKEN",
    "app_token": "SLACK_APP_TOKEN",
}

#: Seconds to wait for Slack when verifying a pasted token at save time.
_TOKEN_VERIFY_TIMEOUT = 8


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


def _write_env_updates(updates: dict[str, str | None]) -> None:
    """Update select keys in config_dir/.env, preserving comments and order.

    A value of ``None`` deletes the key; new keys are appended. The write is
    atomic (0600 temp file in the same dir, then rename) so a crash can never
    truncate .env and lose other credentials, and there is no world-readable
    window between create and chmod.
    """
    import tempfile  # noqa: F811

    from kiro_crew.config.loader import env_path  # noqa: F811
    from kiro_crew.platform_compat import fchmod_safe, restrict_to_owner

    ep = env_path()
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
    ep.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(out) + ("\n" if out else "")
    # mkstemp creates the file with mode 0600 and O_EXCL; rename is atomic on
    # the same filesystem. fchmod is belt-and-suspenders in case of odd umask.
    fd, tmp_name = tempfile.mkstemp(dir=str(ep.parent), prefix=".env.", suffix=".tmp")
    try:
        # Portable perms: os.fchmod is POSIX-only (absent on Windows, where a
        # raw call would raise AttributeError and 500 every token save).
        # fchmod_safe applies 0600 on POSIX and no-ops on Windows;
        # restrict_to_owner then locks the completed file down on both.
        fchmod_safe(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_name, ep)
        try:
            restrict_to_owner(ep)
        except OSError:
            logger.warning("could not restrict .env permissions", exc_info=True)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def api_slack_manifest(request: web.Request) -> web.Response:
    """GET /api/slack/manifest — rendered Slack app manifest + create URL.

    Mirrors ``kirocrew manifest --url`` so the settings UI can offer one-click
    Slack app creation without the CLI: the bundled template gets the user's
    alias substituted, and the comment-stripped YAML is URL-encoded into
    Slack's new-app deep link. Serves only the public template — no secrets.
    """
    import re  # noqa: F811
    from importlib.resources import files as _pkg_files
    from urllib.parse import quote

    # Default to a non-identifying alias: $USER is a host account name and
    # should not be volunteered to every authenticated client.
    alias = request.query.get("alias", "").strip() or "kirocrew"
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", alias):
        return web.json_response({"error": "invalid alias"}, status=400)
    try:
        template = _pkg_files("kiro_crew").joinpath("slack-manifest.yaml").read_text("utf-8")
    except FileNotFoundError:
        return web.json_response({"error": "manifest template missing"}, status=500)
    rendered = template.replace("{{ALIAS}}", alias)
    # Strip comment lines to keep the deep link short (same as the CLI).
    lines = [ln for ln in rendered.splitlines() if not ln.lstrip().startswith("#")]
    encoded = quote("\n".join(lines).strip() + "\n", safe="")
    return web.json_response(
        {
            "alias": alias,
            "manifest": rendered,
            "create_url": f"https://api.slack.com/apps?new_app=1&manifest_yaml={encoded}",
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
        _write_env_updates(env_updates)
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
        _atomic_json_write(path, data)

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


# ── Discord configuration API ──
# The bot token lives in config_dir/.env as DISCORD_BOT_TOKEN (0600), with
# config.json's discord.bot_token as a legacy fallback. Non-secret config
# (enabled, allowed_user_ids, allowed_thread_ids, soft_threshold_pct) lives
# in config.json under
# the "discord" key. GET returns a masked preview + presence boolean; raw
# token values are write-only (reset at the Developer Portal if ever needed).

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
            "soft_threshold_pct": int(dc.soft_threshold_pct),
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

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="discord.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

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

    if "soft_threshold_pct" in body:
        pct = body.get("soft_threshold_pct")
        if not isinstance(pct, int) or isinstance(pct, bool) or not (1 <= pct <= 100):
            return _deny("soft_threshold_pct must be an integer between 1 and 100")
        if pct != int(dc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

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

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    if env_updates:
        _write_env_updates(env_updates)
        # Keep the live process environment in sync with the new .env state
        # (load_credentials() lets os.environ win over .env — see the Slack
        # save handler for the full rationale).
        for key, new_val in env_updates.items():
            if new_val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = new_val
    if staged:
        dc_cfg.update(staged)
        _atomic_json_write(path, data)

    _sel().log_api_access(
        caller=caller,
        operation="discord.config.update",
        outcome="ok",
        source="dashboard",
        resources=",".join(applied + list(env_updates.keys())),
    )
    # All Discord fields are boot-read: token/enabled/allowlist are consumed
    # in the orchestrator's constructor and the dispatcher is built at boot.
    return web.json_response(
        {
            "ok": True,
            "restart_required": bool(env_updates) or bool(staged),
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
            # Forum per-topic config. chat_ids are serialized as strings for
            # the tag editor UI; they are NEGATIVE (e.g. "-1001234567890"),
            # so the save path accepts a leading minus (not a digits-only check).
            "allow_forum": bool(tg.allow_forum),
            "allowed_forum_chat_ids": [str(c) for c in tg.allowed_forum_chat_ids],
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

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="telegram.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

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

    if "soft_threshold_pct" in body:
        pct = body.get("soft_threshold_pct")
        if not isinstance(pct, int) or isinstance(pct, bool) or not (1 <= pct <= 100):
            return _deny("soft_threshold_pct must be an integer between 1 and 100")
        if pct != int(tg_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

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
    if env_updates:
        # Off-loop: on Windows the owner-only lockdown shells out to icacls,
        # which must not block the event loop.
        await asyncio.to_thread(_write_env_updates, env_updates)
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
            "restart_required": bool(env_updates) or bool(staged),
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

    This route is exempt from the dashboard cookie gate for POST only (see
    token_auth ``_BYPASS_EXACT_METHODS``); the delegated handler performs Bot
    Framework JWT validation itself before doing anything with the payload.
    """
    state: DashboardState = request.app["state"]
    handler = getattr(state, "teams_on_activity", None)
    if handler is None:
        return web.Response(status=503, text="Teams channel not enabled")
    return await handler(request)


async def api_teams_config_get(request: web.Request) -> web.Response:
    """GET /api/teams/config — read Teams channel status + config summary."""
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_ID,
        CRED_MICROSOFT_APP_PASSWORD,
        KiroCrewConfig,
    )

    cfg = KiroCrewConfig.load()
    creds = cfg.load_credentials()
    app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or cfg.teams.app_id
    app_password = creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or cfg.teams.app_password
    state: DashboardState = request.app["state"]
    return web.json_response(
        {
            # True only once the outbound app credentials validated this
            # session (kept truthful by TeamsClient.on_state_change).
            "connected": bool(getattr(state, "teams_connected", False)),
            "connect_error": str(getattr(state, "teams_connect_error", ""))[:120],
            "configured": bool(
                app_id and app_password and cfg.teams.enabled and cfg.teams.allowed_emails
            ),
            "read_only": not is_direct_local_request(request),
            "app_id_set": bool(app_id),
            "app_password_set": bool(app_password),
            "enabled": cfg.teams.enabled,
            "tenant_id": cfg.teams.tenant_id,
            "allowed_emails": list(cfg.teams.allowed_emails),
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


async def api_teams_config_save(request: web.Request) -> web.Response:
    """PUT /api/teams/config — persist the Teams secret (.env) + config (json).

    The app password (secret) is written ONLY to config_dir/.env
    (``MICROSOFT_APP_PASSWORD``, 0600); non-secret config (enabled, app_id,
    tenant_id, allowed_emails) lives in config.json under the "teams" key.
    Remote sessions are read-only. The whole channel config is read at gateway
    startup, so every change returns ``restart_required``.
    """
    from kiro_crew.agent import _atomic_json_write  # noqa: F811
    from kiro_crew.config.loader import (  # noqa: F811
        CRED_MICROSOFT_APP_PASSWORD,
        config_path,
    )

    caller = request.get("user", "dashboard")

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="teams.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

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

        changes: dict[str, object] = {}
        if "enabled" in staged and staged["enabled"] != bool(teams_cfg.get("enabled", False)):
            changes["enabled"] = staged["enabled"]
        for str_key in ("app_id", "tenant_id"):
            if str_key in staged and staged[str_key] != teams_cfg.get(str_key, ""):
                changes[str_key] = staged[str_key]
        if "allowed_emails" in staged and staged["allowed_emails"] != teams_cfg.get(
            "allowed_emails", []
        ):
            changes["allowed_emails"] = staged["allowed_emails"]
        applied = list(changes.keys())
        # The secret is env-only; if a legacy plaintext app_password ever landed
        # in config.json, purge it so it can't shadow or outlive the .env value.
        if teams_cfg.get("app_password"):
            changes["app_password"] = ""
            applied.append("app_password_purged")

        if changes:
            teams_cfg.update(changes)
            _atomic_json_write(path, data)
        if env_updates:
            # Off-loop: restrict_to_owner spawns whoami/icacls subprocesses on
            # Windows, which would stall the gateway loop if run inline.
            await asyncio.to_thread(_write_env_updates, env_updates)
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
            "restart_required": bool(env_updates) or bool(applied),
            "verify_warning": "",
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
        changes: dict[str, object] = {}
        if "enabled" in staged and staged["enabled"] != bool(webex_cfg.get("enabled", False)):
            changes["enabled"] = staged["enabled"]
        if "allowed_emails" in staged and staged["allowed_emails"] != webex_cfg.get(
            "allowed_emails", []
        ):
            changes["allowed_emails"] = staged["allowed_emails"]
        applied = list(changes.keys())
        # Any token set/clear also purges the legacy config.json
        # ``webex.bot_token`` fallback so a stale plaintext copy can't shadow
        # (or outlive) the .env credential. The config write commits BEFORE
        # the .env write — if we crash between the two, the legacy copy is
        # already gone rather than resurrected.
        if CRED_WEBEX_BOT_TOKEN in env_updates and webex_cfg.get("bot_token"):
            changes["bot_token"] = ""
            applied.append("bot_token_purged")

        if changes:
            webex_cfg.update(changes)
            _atomic_json_write(path, data)
        if env_updates:
            _write_env_updates(env_updates)
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
            "restart_required": bool(env_updates) or bool(applied),
            "verify_warning": verify_warning,
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

    def _deny(msg: str, status: int = 400) -> web.Response:
        _sel().log_api_access(
            caller=caller,
            operation="wecom.config.update",
            outcome="denied",
            source="dashboard",
            error=msg,
        )
        return web.json_response({"error": msg}, status=status)

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

    if "soft_threshold_pct" in body:
        pct = body.get("soft_threshold_pct")
        if not isinstance(pct, int) or isinstance(pct, bool) or not (1 <= pct <= 100):
            return _deny("soft_threshold_pct must be an integer between 1 and 100")
        if pct != int(wc_cfg.get("soft_threshold_pct", 80)):
            staged["soft_threshold_pct"] = pct
            applied.append("soft_threshold_pct")

    # No Phase 1.5 credential verification: validating WeCom credentials
    # requires opening the AI-bot WebSocket long-connection (no cheap REST
    # "whoami" like Telegram's getMe), so credentials are stored as given and
    # the status badge reports the truth after the next gateway restart.

    # ── Phase 2: commit. All validation passed, so writes are safe. ──
    if staged:
        wc_cfg.update(staged)
        # Off-loop: the atomic write (temp file + fsync + replace) must not
        # block the gateway event loop.
        await asyncio.to_thread(_atomic_json_write, path, data)
    if env_updates:
        # Off-loop: on Windows the owner-only lockdown shells out to icacls,
        # which must not block the event loop.
        await asyncio.to_thread(_write_env_updates, env_updates)
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
            "restart_required": bool(env_updates) or bool(staged),
            "verify_warning": "",
        }
    )
