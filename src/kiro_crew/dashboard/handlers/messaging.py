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

from kiro_crew.browser.auth import ensure as browser_auth_ensure
from kiro_crew.browser.screencast import BROWSER_FRAME_EVENT, build_frame_payload
from kiro_crew.browser.setup import (
    get_extension_token,
    has_playwright_extension,
    patch_mcp_extension,
    patch_mcp_headless,
)
from kiro_crew.constants import CHAT_TURN_TIMEOUT
from kiro_crew.dashboard.chat_persistence import _rehydrate_slot_from_history
from kiro_crew.dashboard.chat_utils import _remove_queued_by_id
from kiro_crew.dashboard.origin import is_direct_local_request, is_loopback
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_END,
    CRON_NOTIFY_PREFIX,
    DashboardState,
)
from kiro_crew.security import is_sensitive_path, redact_credentials, redact_exfiltration_urls
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
    """POST /api/spawn — spawn a subagent."""
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
    agent = cleaned.get("agent") or ""
    max_turns = cleaned.get("max_turns") or 0
    cwd = cleaned.get("cwd") or ""
    model = cleaned.get("model") or ""
    info = state.subagents.spawn(
        task,
        parent_session_key=parent_session,
        agent=agent,
        max_turns=max_turns,
        cwd=cwd,
        model=model or None,
        approval_mode=approval_mode or None,
        silent=silent,
    )
    if not info:
        return web.json_response(
            {"error": f"capacity reached ({state.subagents.max_concurrent})"}, status=429
        )
    if info.done and info.error:
        return web.json_response({"error": info.error}, status=400)
    return web.json_response({"id": info.id, "task": task, "status": "spawned"})


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
        else:
            entry["turns"] = info.turns
            entry["last_tool"] = _redact(info.last_tool)
            entry["elapsed"] = round(time.time() - info.started)
        agents.append(entry)
    return web.json_response({"agents": agents})


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
                    "error": "Cancelled by user",
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
            state.crons.unack_job(n["job_id"])
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

    # Mesh-2603: render [OPTIONS: ...] tags as interactive buttons on the
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

                        slot.append("inject", wrapped, inject_cls)
                        task = asyncio.create_task(
                            asyncio.wait_for(
                                _run_chat(state, slot, wrapped),
                                timeout=CHAT_TURN_TIMEOUT,
                            )
                        )
                        slot.task = task
                        state._background_tasks.add(task)
                        task.add_done_callback(state._background_tasks.discard)
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
    except Exception:
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


async def api_browser_frame(request: web.Request) -> web.Response:
    """POST /api/browser/frame — receive a browse screenshot and rebroadcast it.

    The Playwright MCP proxy POSTs each screenshot it already captured (loopback
    only) as ``{"data": "<base64>", "format": "jpeg", ...}``; we normalize it and
    broadcast a ``browser_frame`` WS event for the BrowserLiveView panel. No CDP
    debug port is involved — this rides the proxy's existing capture path.

    Loopback-gated: the proxy runs on the same host, and frames carry a live view
    of the (Midway-authenticated) browse session, so off-host posts are refused.
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
    """GET /api/browser/config — get browser extension mode and token status."""
    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_config_get",
        outcome="completed",
        downstream_service="browser",
    )
    return web.json_response(
        {
            "extension_mode": has_playwright_extension(),
            "token": get_extension_token() is not None,
        }
    )


async def api_browser_config_save(request: web.Request) -> web.Response:
    """PUT /api/browser/config — save browser extension mode and token."""
    body = await request.json()
    kirocrew_dir = Path.home() / ".kirocrew"
    kirocrew_dir.mkdir(parents=True, exist_ok=True)
    flag_file = kirocrew_dir / "playwright-extension-mode"
    token_file = kirocrew_dir / "playwright-extension-token"

    extension_mode = body.get("extension_mode", False)
    token = body.get("token", "")

    if extension_mode:
        flag_file.touch()
        if token:
            fd = os.open(str(token_file), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(token)
            patch_mcp_extension(token)
    else:
        flag_file.unlink(missing_ok=True)
        token_file.unlink(missing_ok=True)
        patch_mcp_headless()

    _sel().log_tool_invocation(
        session_key="dashboard",
        tool_name="browser_config_save",
        outcome="completed",
        downstream_service="browser",
        resources=f"extension_mode={extension_mode}",
    )
    return web.json_response({"ok": True})


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
    """
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
# (enabled, allowed_user_ids, soft_threshold_pct) lives in config.json under
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
            "soft_threshold_pct": int(dc.soft_threshold_pct),
        }
    )


async def api_discord_config_save(request: web.Request) -> web.Response:
    """PUT /api/discord/config — persist Discord secret (.env) + config (config.json).

    Every Discord field is read once at gateway startup (token, enabled flag,
    allowlist are consumed in the orchestrator's constructor), so any actual
    change returns ``restart_required`` for the UI hint.
    """
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
