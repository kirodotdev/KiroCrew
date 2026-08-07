"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    default_project_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.channel_slots import channel_slot_name, note_slot_closed
from kiro_crew.dashboard.chat_folders import _unhide_folder
from kiro_crew.dashboard.chat_orchestrator import _stage_loop
from kiro_crew.dashboard.chat_persistence import (
    _attach_variants,
    get_reasoning_effort_values,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_runner import (
    _context_usage_payload,
    _run_chat,
    _start_next_queued_turn,
)
from kiro_crew.dashboard.chat_title import _maybe_auto_title
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SYNTHETIC_RECOVERY_KIND,
    _build_stream_chunk,
    _edit_queued_by_id,
    _emit_agent_assignment,
    _history_key_for,
    _normalize_model,
    _prepare_messages,
    _redact_for_display,
    _redact_meta,
    _redact_meta_for_role,
    _remove_queued_by_id,
    _sync_dashboard_slots,
    effective_session_key,
    slot_history_key,
)
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import (
    _MAX_PENDING_CONTEXT,
    DashboardState,
    _ChatSlot,
    _mark_permission_resolved,
    _normalize_slot_key,
    parse_cls_meta,
)
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.history import carry_provenance
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMProvider
from kiro_crew.safety_override import safety_override
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    ARTIFACT_SLUG_RE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    normalize_theme_consent_sha,
    validate_tool_args,
)

logger = logging.getLogger(__name__)


def _sweep_stale_permissions(slot: "_ChatSlot") -> None:
    """Mark unresolved permissions from prior turns as stale.

    Called once at turn-start, before the new user message is appended.
    Safe: if we're starting a new turn, any prior unresolved permission
    is definitionally orphaned — the LLM that requested it is gone.

    Note: if the same slot is open in multiple tabs, an in-flight pending
    approval in tab A may be marked stale by a turn-start in tab B. The
    failure mode is benign (user re-clicks approve); single-tab use is
    unaffected.
    """
    for msg in slot.messages:
        if msg.get("role") != "permission":
            continue
        try:
            cls = json.loads(msg.get("cls", "{}"))
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls, dict):
            # Valid JSON but not an object (e.g. [], "x", 123, null) — cannot
            # carry a "resolved" key; skip rather than raise TypeError and
            # abort the whole sweep. Mirrors parse_cls_meta() in state.py.
            continue
        if "resolved" in cls:
            continue
        cls["resolved"] = "stale"
        msg["cls"] = json.dumps(cls)
        slot._dirty = True
        sel().log_api_access(
            caller="gateway",
            operation="permission.resolve_stale",
            outcome="allowed",
            source="turn_start_sweep",
            resources=cls.get("request_id", ""),
        )


async def api_chat(request: web.Request) -> web.StreamResponse:
    """POST /api/chat — send message to a slot, stream response via SSE."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    message = body.get("message", "").strip()
    agent = body.get("agent", "")
    slot_name = body.get("slot")
    color_theme = body.get("color_theme", "")
    user_meta = body.get("meta")  # knowledge/files/pastes metadata from frontend
    if not isinstance(user_meta, dict):
        user_meta = None
    theme_consent = body.get("theme_consent") is True
    # Content-bound persona consent: the sha256 hex the user
    # granted in the consent modal. Injection is gated on this matching the
    # persona text read from disk server-side; the legacy boolean above is
    # still parsed (backward-compatible bodies + logging) but does not grant
    # injection by itself. Normalize + full-match to 64 lowercase hex here so a
    # malformed value (non-ASCII "é", wrong length, non-str) becomes None
    # (absent) rather than reaching hmac.compare_digest and crashing the turn
    # with a TypeError.
    theme_consent_sha = normalize_theme_consent_sha(body.get("theme_consent_sha"))
    if not isinstance(color_theme, str) or not (
        color_theme == "" or color_theme.startswith("custom-")
    ):
        color_theme = ""
    if not isinstance(agent, str) or not (agent == "" or _AGENT_NAME_RE.match(agent)):
        _emit_agent_assignment(str(slot_name or ""), str(agent), outcome="denied_invalid")
        return web.json_response({"error": "invalid agent name"}, status=400)
    if not isinstance(slot_name, str) and slot_name is not None:
        slot_name = None  # coerce non-string slot to auto-generate

    # Honor memory_mode from the body when auto-creating a slot (e.g. AgentRock
    # skill dispatch defaults to "temporary"). Only validated values are passed
    # through; anything else is dropped so get_or_create_slot uses its default.
    # If the slot already exists, get_or_create_slot raises on a memory_mode
    # mismatch, matching POST /api/chat/slots semantics.
    requested_memory_mode = body.get("memory_mode")
    if requested_memory_mode not in ("persistent", "incognito", "temporary"):
        requested_memory_mode = None

    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            memory_mode=requested_memory_mode,
        )
    except ValueError as exc:
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="chat_send",
            outcome="denied",
            source="memory_mode_mismatch",
            resources=f"slot={slot_name}",
            error=str(exc),
        )
        return web.json_response({"error": str(exc)}, status=409)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps can only access slots they own. Dashboard users (empty request_app)
    # can access everything.
    request_app = request.get("app", "")
    if request_app:
        if not slot._app:
            # Unscoped slot created by dashboard — apps cannot access it.
            sel().log_api_access(
                caller=request_app,
                operation="chat_send",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app cannot access unscoped slots",
            )
            return web.json_response({"error": "not found"}, status=404)
        elif request_app != slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="chat_send",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "not found"}, status=404)

    if slot.agent not in (None, ""):
        # Slot already has an agent — only reject explicit mismatches (non-empty different agent).
        # Empty agent in request means "use existing" (e.g. follow-up messages from frontend).
        if agent and slot.agent != agent:
            _emit_agent_assignment(slot.key, agent or "", outcome="denied_mismatch")
            return web.json_response({"error": "slot agent mismatch"}, status=409)
        else:
            logger.debug("agent match for slot=%s agent=%s", slot.key, agent)
    elif agent:
        # Slot has no agent — set it if not running
        if slot.running:
            _emit_agent_assignment(slot.key, agent, outcome="denied_running")
            return web.json_response(
                {"error": "cannot set agent on running slot"},
                status=409,
            )
        slot.agent = agent
        _emit_agent_assignment(slot.key, agent)
    else:
        # No agent on slot, no agent in request — nothing to enforce.
        pass

    if "color_theme" in body:
        slot.color_theme = color_theme
        slot.theme_consent = theme_consent
        slot.theme_consent_sha = theme_consent_sha

    if slot.running or slot._in_stage_execution:
        # Mid-turn steer: inject into the RUNNING turn instead of queueing for
        # the next turn. Gated on an explicit `steer` flag + a live, steer-capable
        # inner AcpClient that _run_chat published on the slot. Fire-and-forget —
        # the inline steer card materializes when kiro-cli echoes steering_consumed
        # (EVENT_STEER_CONSUMED). If steer is requested but unavailable (no live
        # client / unsupported backend / RPC error), fall through to the queue
        # path so the user's text is NEVER silently dropped.
        #
        # ``slot._in_stage_execution`` extends this to autopilot: during a multi-stage
        # plan ``slot.running`` briefly reads False between stages (each stage's
        # _run_chat closes its own turn), so a mid-plan message would otherwise
        # start a concurrent turn. The orchestrating flag keeps it on the queue
        # path (steer is unavailable between stages, so it falls through to the
        # queue below and is held until the plan ends).
        if body.get("steer") and message:
            _client = slot._acp_client
            if _client is not None and getattr(_client, "supports_steer", False):
                # Register as pending BEFORE the await: _client.steer() suspends
                # on stdin.drain(), and if the turn's finally runs during that
                # suspension it must already see this steer to requeue it
                # (append-after-await would land on an idle slot and orphan the
                # message). The force-stop
                # clear() likewise races correctly: a hard kill during the
                # await discards the entry, so a late write can't resurrect it.
                slot._pending_steers.append(message)
                try:
                    steered = await _client.steer(message)
                except Exception as exc:  # best-effort — fall through to queue
                    logger.warning("steer failed for slot %s: %s", slot.key, exc)
                    steered = False
                if not steered:
                    # Unwind the optimistic registration so the queue fallback
                    # below doesn't double-deliver. If the entry is already
                    # gone, the turn's finally requeued it (or a hard kill
                    # discarded it) during the await — either way the message
                    # is accounted for, so skip the fallback.
                    try:
                        slot._pending_steers.remove(message)
                    except ValueError:
                        return web.json_response({"ok": True, "queued": True})
                if steered:
                    _ts = datetime.now(timezone.utc).isoformat()
                    # Cut the in-flight text segment at the steer boundary
                    # BEFORE persisting the user message, so the transcript
                    # reads [assistant(pre-steer), user(steer), …] — the same
                    # order the client rendered live. Without this the whole
                    # segment lands BELOW the steer bubble at end-of-turn and
                    # the chat_done refresh visibly reorders the reply (and the
                    # pre-steer chunk entries are stranded in slot.messages —
                    # _flush_segment's trailing-run walk stops at this user
                    # message). Best-effort: a cut failure must never lose the
                    # steer itself.
                    _cut = slot._steer_segment_cut
                    if _cut is not None:
                        try:
                            _cut()
                        except Exception:
                            logger.warning(
                                "steer segment cut failed for slot %s",
                                slot.key,
                                exc_info=True,
                            )
                    # Sanitize: same chain as the queue path.
                    _sanitized, _ = redact_exfiltration_urls(message)
                    _sanitized, _ = redact_credentials(_sanitized)
                    _redacted = _redact_for_display(_sanitized)
                    # Persist the steered message so it survives page reload
                    # (dirty-flush picks it up on next save cycle). Store the
                    # sanitized form — raw content must never reach an external
                    # surface (security-controls).
                    slot.append(
                        "user",
                        _sanitized,
                        "msg msg-u",
                        ts=_ts,
                        meta={"steer": True},
                    )
                    state.broadcast_ws(
                        "steer_push",
                        {
                            "slot": slot.key,
                            "content": _redacted,
                            "ts": _ts,
                        },
                    )
                    return web.json_response({"ok": True, "steered": True})
            # steer requested but unavailable → fall through to queue below.
        # Queue the message — return JSON immediately (no SSE needed).
        # The existing SSE reader will pick up queued messages as _run_chat
        # processes the queue in its finally block.
        if message:
            qid = slot.queue_append(message)
            _c, _ = redact_exfiltration_urls(message)
            _c, _ = redact_credentials(_c)
            _redacted = _redact_for_display(_c)
            state.broadcast_ws(
                "queue_push",
                {
                    "slot": slot.key,
                    "content": _redacted,
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "queue_id": qid,
                },
            )
        return web.json_response({"ok": True, "queued": True})

    if not message:
        return web.json_response({"error": "message is required"}, status=400)

    # Queue a message typed while background sub-agents are still running for
    # this slot. The slot.running queue path above covers the mid-turn case;
    # this covers the idle case (spawn_run is fire-and-forget, so the main slot
    # goes idle while children run). Without the hold, this message would start a
    # main turn immediately and interleave with the [Subagent completion event]
    # injections. Queue it instead (reusing the slot queue) — the queue drain
    # releases it after the last sub-agent finishes (see chat_runner _hold_users).
    # Opt-out: if the user explicitly chose steer mode, honour it — start a new
    # turn immediately so the message is processed without waiting for children.
    if (
        not body.get("steer")
        and state.subagents is not None
        and state.subagents.running_agents_for(f"dashboard:{slot.key}")
    ):
        qid = slot.queue_append(message)
        _c, _ = redact_exfiltration_urls(message)
        _c, _ = redact_credentials(_c)
        _redacted = _redact_for_display(_c)
        state.broadcast_ws(
            "queue_push",
            {
                "slot": slot.key,
                "content": _redacted,
                "ts": datetime.now(timezone.utc).isoformat(),
                "queue_id": qid,
            },
        )
        return web.json_response({"ok": True, "queued": True})

    # WS mode: return JSON immediately, chunks delivered via WebSocket
    ws_mode = request.query.get("ws") == "1"

    slot._has_reader = not ws_mode  # Only block SSE broadcast if HTTP SSE reader
    slot._file_changes = []  # Reset file-change accumulator for the new turn
    # ── Sweep orphaned permissions from prior turns ──
    _sweep_stale_permissions(slot)

    # No per-message browse marker: Browser Mode is a capability, not a per-turn
    # gate. When it is on the `browser_*` MCP tools are registered and present in
    # the agent's tool list; when it is off they are not. The agent itself decides
    # whether to operate a browser or read with web_fetch (the system prompt and
    # the kirocrew-commands / web-browse skills tell it how), so the backend
    # injects nothing here.
    slot.append("user", message, "msg msg-u", meta=_redact_meta(user_meta) if user_meta else None)

    # Note: untitled slots display as "New Session…" via _ChatSlot.display_title
    # (serialization layer), so there's no bare chat-N flash to patch here. The
    # LLM titling is kicked off below, before _run_chat.

    # ── AutoNudge: user input cancels any pending nudge timer (user wins). ──
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _autonudge = _autonudge_get()
        if _autonudge is not None:
            _autonudge.notify_user_input(slot.key)
    except Exception:
        logger.warning("autonudge.notify_user_input failed", exc_info=True)

    # ── Orchestrator "Go All" detection ─────────────────────────────
    # Deny-by-default trust boundary (item 5): a turn tagged
    # origin="widget" was pre-filled into the composer by an LLM-emitted
    # <mcwidget> postMessage. Even though the frontend now requires a human
    # gesture to send it, the message TEXT is still attacker-controlled — an
    # injected widget can pre-fill "go all" and socially engineer the user
    # into pressing Enter. "go"/"go all" is the only chat-text-reachable
    # privilege escalation (it flips the orchestrator into unattended
    # per-stage auto-approval via slot._auto_run + _stage_loop), so we refuse
    # to honour it for widget-origin turns and let the text fall through to a
    # normal, fully-gated _run_chat turn instead. Mode changes and tool
    # approvals live on separate endpoints a widget iframe cannot reach.
    _widget_origin = bool(user_meta) and user_meta.get("origin") == "widget"
    if (
        getattr(slot, "mode", "") == "orchestrator"
        and message.strip().lower() in ("go", "go all")
        and _widget_origin
    ):
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="auto_run_denied",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go_typed_widget_origin",
                outcome="denied",
                resources=f"slot={slot.key}",
                error="orchestrator go/go-all refused for widget-origin turn",
            )
        )
        logger.warning(
            "Refused orchestrator auto-run escalation for widget-origin turn on slot %s",
            slot.key,
        )
    elif getattr(slot, "mode", "") == "orchestrator" and message.strip().lower() in (
        "go",
        "go all",
    ):
        _is_auto = message.strip().lower() == "go all"
        if _is_auto:
            slot._auto_run = True
            logger.info("Auto-run enabled for slot %s", slot.key)
            sel().log(
                SecurityEvent(
                    event_id=uuid.uuid4().hex,
                    timestamp=datetime.now(tz=timezone.utc).isoformat(),
                    event_type="auto_run_enabled",
                    caller_identity=f"dashboard:{slot.key}",
                    agent=getattr(slot, "agent", ""),
                    source="dashboard",
                    operation="go_all_typed",
                    outcome="approved",
                    resources=f"slot={slot.key}",
                )
            )
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="stage_approved",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="go_typed",
                outcome="approved",
                resources=f"slot={slot.key}",
            )
        )
        # Use Python-controlled stage loop instead of _run_chat
        task = asyncio.create_task(_stage_loop(state, slot, auto_run=_is_auto))
        slot.task = task
        slot._recovery_retrigger_count = 0
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)
        state.push_slots_update()
        # All output delivered via WebSocket — return JSON like api_chat_plan_action
        return web.json_response({"ok": True, "slot": slot.key})

    # ── Orchestrator stop detection ─────────────────────────────────
    _stop_words = {"stop", "cancel", "abort"}
    tracker = slot._orch_tracker
    if (
        tracker is not None
        and tracker.has_escalated
        and not tracker.stopped
        and message.strip().lower().split()[0] in _stop_words
    ):
        tracker.stop()
        slot._auto_run = False
        # Cancel running agents for this slot
        if state.subagents:
            session_key = f"dashboard:{slot.key}"
            mgr = state.subagents
            for a in mgr.running_agents_for(session_key):
                t = mgr._tasks.get(a["id"])
                if t and not t.done():
                    t.cancel()
        stop_msg = "🛑 [SYSTEM] Orchestration stopped by user."
        slot.append("assistant", stop_msg, "msg msg-a")
        state.broadcast_ws(
            "chat_message", {"slot": slot.key, "role": "assistant", "content": stop_msg}
        )
        state.broadcast_ws("chat_done", {"slot": slot.key})
        return web.json_response({"ok": True, "stopped": True})

    # ── Reset rounds after user guidance (not a stop) ───────────────
    if tracker is not None and tracker.has_escalated:
        tracker.reset_after_guidance()
        logger.info("Rounds reset after user guidance for slot %s", slot.key)

    # Drain stale pending messages from previous turns that completed
    # after their SSE reader disconnected. Must happen BEFORE _run_chat
    # so we don't discard the new turn's output.
    slot.drain()

    # Kick off LLM titling now, from the first user message, so the title lands
    # *during* the first turn instead of waiting for the whole response to
    # finish (chat_done). Runs on an isolated background kiro-cli session
    # concurrent with the turn. No-ops once titled / in-flight; the instant
    # 60-char provisional stays as the fallback if the LLM SKIPs or errors.
    if not slot._titled and not slot._title_in_flight:
        _tt = asyncio.create_task(_maybe_auto_title(state, slot))
        state._background_tasks.add(_tt)
        _tt.add_done_callback(state._background_tasks.discard)

    # Edition message observer (CPP seam). Fire-and-forget, fail-safe: a
    # companion uses this to auto-ingest doc links pasted into chat. The public
    # Default is a no-op. Guarded so an observer error never blocks the turn;
    # deferred context read via the sel.py pattern (no platform import at load).
    try:
        from kiro_crew.platform.context import current_context, safe_context_call

        safe_context_call(
            lambda: current_context().dashboard.on_user_message(request.app, message),
            fallback=None,
            log_message="dashboard.on_user_message observer failed",
        )
    except Exception:
        logger.debug("on_user_message observer raised; ignoring", exc_info=True)

    task = spawn_guarded_turn(state, slot, _run_chat(state, slot, message))
    slot.task = task
    slot._recovery_retrigger_count = 0
    state.push_slots_update()

    if ws_mode:
        return web.json_response({"ok": True, "slot": slot.key})

    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    try:
        while True:
            pending = slot.drain()
            for msg in pending:
                if msg["cls"] == "done":
                    await resp.write(b"data: [DONE]\n\n")
                    slot._has_reader = False
                    return resp
                chunk = _build_stream_chunk(msg)
                await resp.write(f"data: {chunk}\n\n".encode())
            try:
                await asyncio.wait_for(slot.event.wait(), timeout=30)
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, ClientConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        slot.drain()
        slot._has_reader = False
    return resp


async def api_chat_slots(request: web.Request) -> web.Response:
    """GET /api/chat/slots — list all chat slots."""
    state: DashboardState = request.app["state"]
    # Credential-backed check status is owner-only. Non-owner and app-token
    # callers receive source links but neither cached status nor provider work.
    from kiro_crew.dashboard.handlers.source_providers import (
        ensure_gitlab_hosts_loaded,
        is_owner_dashboard_request,
        schedule_check_refresh,
    )

    # Same warm-up as the WebSocket connect path: slot source-link extraction is
    # synchronous and cannot load the self-managed GitLab allowlist itself, so a
    # cold direct GET would omit every configured self-hosted MR link.
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug("GitLab allowlist warm-up failed; chips may lag one round", exc_info=True)

    include_check_status = is_owner_dashboard_request(request)
    payloads = state.serialize_slots(include_check_status=include_check_status)
    if include_check_status:
        # Issue links carry no check status — skip them so the scheduler never
        # hands an issue URL to the pull-request-only chip fetch.
        urls = [
            link["url"]
            for payload in payloads
            for link in payload.get("source_links", [])
            if link.get("kind", "change") == "change"
        ]
        if urls:
            schedule_check_refresh(urls, state.push_slots_update)
    return web.json_response(payloads)


def _finite_number(value: Any) -> float | None:
    """Return *value* as a float when it is a real, finite number, else None.

    The context fields are cosmetic, but they ride on the response that carries
    the whole conversation, so anything unserializable reaching `json_response`
    would turn a display nicety into a 500 that blanks the transcript. A
    provider is free to return whatever its accessors return; this is the gate
    that keeps a non-numeric one from ever being emitted.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _context_reading(
    pct: Any, used: Any, window: Any, *, stale: bool
) -> dict[str, Any]:
    """Assemble the context fields from a (pct, used, window) triple.

    ``pct`` is the PRIMARY signal and the only one the bar needs: kiro-cli
    commonly reports ``contextUsagePercentage`` with no ``usage_update``, so a
    resident session routinely knows it is 11% full while knowing neither token
    count. Gating on the window would no-op the whole feature in that case.
    Token counts are optional enrichment for the tooltip's absolute numbers,
    and the frontend already falls back to a model-derived window without them.

    A ``stale`` reading omits ``used`` entirely rather than shipping a count no
    process measured. The tooltip renders an absent ``used`` as a ``~``
    approximation derived from pct, so honesty costs nothing — and leaving the
    count on the wire would make every other consumer of this endpoint render a
    never-measured figure as measured unless it knew to drop it.

    Returns ``{}`` when there is nothing worth showing — no usable pct, and no
    window either. A 0% reading with no tokens is indistinguishable from a
    fresh session that has never had a turn, and both render an empty bar
    anyway, so it is reported as "no reading" rather than as a measurement.
    """
    pct_num = _finite_number(pct)
    window_num = _finite_number(window)
    used_num = _finite_number(used)
    if pct_num is None:
        return {}
    fields: dict[str, Any] = {"context_pct": pct_num, "context_stale": stale}
    if window_num:
        fields["context_window_tokens"] = int(window_num)
        if used_num and not stale:
            fields["context_used_tokens"] = int(used_num)
    if not pct_num and "context_window_tokens" not in fields:
        return {}
    return fields


async def _context_snapshot_fields(state: "DashboardState", slot: "_ChatSlot") -> dict[str, Any]:
    """Context-meter fields for a slot-detail response, or ``{}`` when unknown.

    The meter is fed by turn-scoped ``context_usage`` WS frames, so opening a
    session that has not had a turn *in this tab's lifetime* renders an empty
    bar. This is the open-path source that seeds it.

    Two tiers, in order:

    1. **Live session** — the provider is still resident in the pool, so its
       ``last_prompt_stats`` are authoritative.
    2. **Cold session** — the ACP process expired (idle timeout) or the gateway
       restarted, so the stats are gone. Falls back to the snapshot recorded by
       ``DashboardState.broadcast_context_usage`` and marks it
       ``context_stale``. Resume replays the same transcript via ACP
       ``session/load``, so the pre-shutdown reading approximates the next
       turn's — and that turn overwrites it with measured truth.

    A snapshot taken under a DIFFERENT model is discarded rather than shown:
    its pct and counts are denominated in the old model's window, so rendering
    them against the new one would misreport usage. Dropping them lets the
    frontend fall back to its model-derived window at 0%.

    Never raises: every failure degrades to ``{}`` (an empty bar) rather than
    failing the request the transcript arrives on.
    """
    try:
        return await _context_snapshot_fields_inner(state, slot)
    except Exception:
        logger.debug("context snapshot fields failed for slot %s", slot.key, exc_info=True)
        return {}


async def _context_snapshot_fields_inner(
    state: "DashboardState", slot: "_ChatSlot"
) -> dict[str, Any]:
    provider = state.sessions.get_provider(effective_session_key(slot))
    if provider is not None:
        return _context_reading(
            provider.context_usage_pct(),
            (
                provider.context_used_tokens()
                if hasattr(provider, "context_used_tokens")
                else 0
            ),
            (
                provider.context_window_tokens()
                if hasattr(provider, "context_window_tokens")
                else 0
            ),
            stale=False,
        )
    # Readings from a previous process live in a file, so the first read is
    # disk IO — off the event loop, since this handler serves every chat open.
    await asyncio.to_thread(state.ensure_context_snapshots_loaded)
    snapshot = state.context_snapshot_for(slot.key)
    if snapshot is None:
        return {}
    if snapshot.get("model", "") != slot.model:
        return {}
    return _context_reading(
        snapshot.get("pct"),
        snapshot.get("used_tokens"),
        snapshot.get("window_tokens"),
        stale=True,
    )


async def api_chat_slot_detail(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot} — message history for a slot.

    Query params:
      - ``limit``: max messages to return (optional; if omitted, returns ALL messages from disk)
      - ``before``: return messages before this index (legacy pagination, still supported)

    By default (no limit), reads the full chained history from disk across
    gateway restarts. Pagination params are retained for backwards compatibility.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    limit_raw = request.query.get("limit")
    before = request.query.get("before")

    # No limit → load ALL messages (chained across gateway restarts).
    # In-memory slot.messages is authoritative for the current session.
    # _disk_older_count gates whether to read disk AND provides the stable
    # slice boundary (set at restore/resume, never drifts with new messages).
    if limit_raw is None and before is None:
        mem_msgs = list(slot.messages)
        if slot._disk_older_count > 0 and state.conversation_log:
            history_key = slot_history_key(slot)
            try:
                disk_msgs = state.conversation_log.read_messages_chained(history_key)
            except Exception:
                logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
                disk_msgs = []
            older = disk_msgs[: slot._disk_older_count] if disk_msgs else []
            messages = older + mem_msgs
        else:
            messages = mem_msgs
        total = len(messages)
        has_more = False
    else:
        # Legacy pagination path (retained for programmatic callers).
        # Always reads from chained disk history; no in-memory offset math.
        limit = min(int(limit_raw or "200"), 500)
        history_key = slot_history_key(slot)
        try:
            all_msgs = (
                state.conversation_log.read_messages_chained(history_key)
                if state.conversation_log
                else []
            )
        except Exception:
            logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
            all_msgs = []
        # Append any un-flushed in-memory tail messages beyond what's on disk.
        # Use _disk_older_count to isolate current-session disk count, since
        # chained disk includes older sessions that inflate disk_len.
        mem_len = len(slot.messages)
        disk_len = len(all_msgs)
        current_session_disk = max(0, disk_len - slot._disk_older_count)
        unflushed = mem_len - current_session_disk
        if unflushed > 0:
            all_msgs = list(all_msgs) + list(slot.messages[-unflushed:])
        total = len(all_msgs)
        if before is not None:
            end = max(0, min(int(before), total))
        else:
            end = total
        start = max(0, end - limit)
        messages = all_msgs[start:end]
        has_more = start > 0

    prepared = _prepare_messages(messages, slot.running)

    return web.json_response(
        {
            "key": slot.key,
            # Redacted at emit like every sibling path (_ChatSlot.to_dict does the
            # same for the sidebar payload). Titles can be LLM-generated or set by
            # a rename, so they are content, not configuration.
            "title": _redact_for_display(slot.display_title),
            "running": slot.running,
            "stopping": slot._stopping,
            "messages": prepared,
            "queue": [
                {"id": q["id"], "content": _redact_for_display(q["content"])} for q in slot._queue
            ],
            "total": total,
            "has_more": has_more,
            # Seeds the context meter on open. Turn-scoped WS frames alone leave
            # it empty for a session reopened in a new tab; omitted entirely
            # (not zeroed) when genuinely unknown, so the frontend can tell
            # "no reading" from "0% used".
            **(await _context_snapshot_fields(state, slot)),
        }
    )


async def api_chat_slot_create(request: web.Request) -> web.Response:
    """POST /api/chat/slots — create a new chat slot."""
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = body.get("name")
    agent = body.get("agent", "")
    model = body.get("model", "")
    # Folder membership at BIRTH. Assigning it afterwards (client PATCH) is
    # visibly too late: get_or_create_slot broadcasts the new slot before this
    # handler returns, so the dashboard renders it at the top level for a frame
    # or two and it then jumps into the folder. Validated exactly as
    # PATCH /api/chat/slots/{slot}/folder validates it.
    folder_id = str(body.get("folder_id") or "")
    if folder_id and not any(f["id"] == folder_id for f in state._folders):
        return web.json_response(
            {"error": "folder not found", "code": "folder_not_found"}, status=400
        )

    # Resolve workspace from agent bindings
    workspace = "default"
    cfg = None
    try:
        cfg = KiroCrewConfig.load()
    except Exception:
        # Infra failure loading config must not block slot creation outright, so
        # validation below is skipped rather than failing closed.
        logger.warning("Failed to load config for slot create", exc_info=True)
    # Normalize an agent nothing will dispatch to the one that WILL answer.
    # Otherwise the name is stored verbatim and resolve_agent_bindings silently
    # falls back to the default agent: the sidebar advertises the requested agent
    # while a different one answers, with none of its tools. Storing the real
    # agent keeps the slot honest, and a caller that requires a specific binding
    # (an app panel verifying the returned agent) can see the mismatch instead of
    # discovering it turns later.
    if cfg is not None and agent:
        try:
            bindings = resolve_agent_bindings(cfg, agent)
            workspace = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            if not bindings.requested_resolved:
                # Log only — the requested binding is the user's intent and is
                # stored VERBATIM. Rewriting it to whatever currently answers was
                # destructive: the resolution behind that decision can be
                # momentarily stale while the overwrite is permanent, so a valid
                # binding could be silently rebound to the default forever, where a
                # verbatim name recovers as soon as it resolves. Surfacing the
                # effective agent to the UI is a separate, non-destructive change.
                logger.info(
                    "Slot %s requested agent %r, which currently resolves to %r",
                    name,
                    agent,
                    bindings.resolved_alias or "(default)",
                )
        except Exception:
            logger.warning("Failed to resolve bindings for slot create", exc_info=True)

    # Coalesce every push inside into ONE broadcast at exit, so the first frame
    # any client sees already carries the folder, title, artifact binding and
    # project. Otherwise each of those is a separate post-create correction the
    # UI renders as a jump.
    with state.suspend_slots_push():
        try:
            memory_mode = body.get("memory_mode", "persistent")
            if memory_mode not in ("persistent", "incognito", "temporary"):
                return web.json_response({"error": "invalid memory_mode"}, status=400)
            slot = state.get_or_create_slot(
                name,
                agent=agent,
                workspace=workspace,
                model=model,
                mode=body.get("mode", ""),
                memory_mode=memory_mode,
                ephemeral=body.get("ephemeral"),
                app=request.get("app", ""),
            )
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        if slot.is_restricted:
            logger.info("Slot %s created with memory_mode=%s", slot.key, slot.memory_mode)
        # App ownership check (App Kit §5.2), same deny-by-default rule as
        # api_chat_send. It matters HERE because `name` can address an
        # ALREADY-EXISTING slot: get_or_create_slot returns that slot without
        # consulting ownership, and everything below mutates it (folder, title,
        # artifact binding). Without this an app token could refile or retitle
        # another app's — or the dashboard's — session. A slot this request just
        # created carries `_app == request_app`, so the new-slot path is
        # unaffected; a dashboard caller (empty app) keeps full access.
        request_app = request.get("app", "")
        if request_app and slot._app != request_app:
            sel().log_api_access(
                caller=request_app,
                operation="chat_slot_create",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error=(
                    "app cannot access unscoped slots"
                    if not slot._app
                    else "app does not own this slot"
                ),
            )
            # One code for BOTH reasons on purpose: a distinct code per reason
            # would turn this 404 into an existence oracle for slots the caller
            # may not know about. The prose stays in `error` for logs.
            return web.json_response(
                {"error": "not found", "code": "slot_not_found"}, status=404
            )
        # Pin title if explicitly provided (prevents auto-title from overwriting)
        title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            slot.title = title
            slot._titled = True
        # Bind to an artifact if provided (companion chat). Validate
        # against the artifact slug grammar so an injection-shaped value can never
        # land on the slot; anything invalid is silently dropped. Uniqueness (≤1
        # active bound session per slug) is a frontend-flow convention, not
        # enforced here.
        artifact_slug = body.get("artifact") if isinstance(body, dict) else None
        if isinstance(artifact_slug, str) and ARTIFACT_SLUG_RE.match(artifact_slug):
            slot._artifact = artifact_slug
        # Default project to workspace directory so file search works out of the box
        if not slot.project:
            cfg_proj = cfg.dashboard.default_project if cfg else ""
            if isinstance(cfg_proj, str) and cfg_proj:
                resolved = os.path.realpath(os.path.expanduser(cfg_proj))
                if os.path.isdir(resolved) and not is_sensitive_path(resolved):
                    cfg_proj = resolved
                else:
                    cfg_proj = ""
            else:
                cfg_proj = ""
            slot.project = cfg_proj or default_project_dir(workspace)
        # File the slot before the coalesced broadcast, so its first appearance
        # in every client is already inside the folder.
        if folder_id:
            # Mirror PATCH /api/chat/slots/{slot}/folder: a CHANGED folder must
            # re-inject the [FOLDER] breadcrumb on the next turn. `is_new` alone
            # is not enough — `name` can address an already-used slot, whose
            # turn is `is_new=False`, so moving it would otherwise leave the
            # model believing the session is still in its old folder.
            # Harmless on the new-slot path: that turn is `is_new`, so the
            # breadcrumb fires regardless and the flag is consumed there.
            if folder_id != slot.folder_id:
                slot._folder_changed = True
            slot.folder_id = folder_id
            _unhide_folder(state, folder_id)
        _sync_dashboard_slots(state)
        # Guarantee a frame. get_or_create_slot pushes for a NEW slot, but
        # returns an existing named slot without pushing — and this handler is
        # now the only thing that files a slot (the client sends no follow-up
        # PATCH to supply that push). Without this, re-creating an
        # existing slot name with a different folder_id would move it for the
        # requester while every other connected client kept the stale
        # placement. Inside the suspension this only marks a push owed, so the
        # new-slot path still emits exactly ONE coalesced frame.
        state.push_slots_update()
    # Persist OUTSIDE the suspension. save_slot_off_loop deliberately takes the
    # patient cross-process history lock, which another holder (a workflow or
    # cron appending to the same session) can hold for a while — and the
    # suspension is process-wide, so awaiting it inside would stall every
    # client's slot updates behind one session's file lock. The in-memory slot
    # is the source of truth and was already broadcast at block exit; a failed
    # write re-arms the periodic flush (best_effort).
    if folder_id:
        await save_slot_off_loop(state, slot, force=True)
    return web.json_response(state.serialize_slot(slot))


def _reject_pending_approvals(slot: _ChatSlot) -> None:
    """Reject all pending approval futures so the chat runner unblocks.

    When a stop/interrupt is triggered while the agent is waiting for tool
    approval, the chat runner is suspended on the approval future. Without
    resolving it, the stream generator stays paused, _turn_done never fires,
    and the cooperative cancel times out — forcing a hard kill.

    Resolving the future is not enough on its own: the ``permission`` message
    the UI renders the approval bar from must ALSO be marked resolved.
    Otherwise the future is gone while the message still reads pending, so the
    bar survives a history reload and every button on it answers
    ``404 no pending approval`` — an approval card the user cannot action.
    """
    for aid, fut in list(slot._approval_futures.items()):
        if not fut.done():
            fut.set_result("rejected")
            if _mark_permission_resolved(slot.messages, aid, "rejected"):
                slot._dirty = True
            sel().log_tool_invocation(
                session_key=effective_session_key(slot),
                agent=getattr(slot, "agent", "") or "kirocrew",
                source="dashboard",
                tool_name=f"approval_reject:{aid}",
                tool_kind="permission",
                outcome="rejected_on_stop",
            )


def _unblock_pending_waits(state: DashboardState, slot: _ChatSlot) -> None:
    """Unblock EVERY thing a stop/interrupt could leave the runner waiting on.

    Two independent blocking waits exist per slot and both must be released or
    the cooperative cancel times out into a hard kill:

    * pending tool approvals (:func:`_reject_pending_approvals`)
    * pending agent questions from the ``ask_question`` tool
      (:meth:`DashboardState.cancel_questions_for_slot`) — the blocked HTTP
      request holds an MCP worker, so resolving the future is what lets that
      socket close and the tool call return.

    They are combined here deliberately: a new blocking wait added later must
    be released from every stop path, and three separate call sites each
    needing their own second line is how one of them gets missed.
    """
    _reject_pending_approvals(slot)
    cancelled = state.cancel_questions_for_slot(slot.key)
    if cancelled:
        logger.info(
            "Stop: cancelled %d pending question(s) on slot %s", cancelled, slot.key
        )


async def _reset_slot_session(
    state: DashboardState, slot: _ChatSlot, session_key: str
) -> None:
    """Reset a slot's agent session, releasing anything blocked on the old one.

    The switch handlers (agent, model, bulk model, reasoning effort, workspace)
    reset the session so the next message starts under the new setting. That
    tears down the agent process — but a pending ``ask_question`` lives in
    dashboard state, not in the session, so without this it survives the reset:
    the card stays on screen inviting an answer, and the blocked HTTP request
    holds an MCP worker until its own timeout with no agent left to receive the
    answer it eventually returns.

    Routing every reset through one helper rather than adding a second call at
    each site is deliberate, and is the same reasoning as
    :func:`_unblock_pending_waits`: five call sites each having to remember an
    extra line is how one of them gets missed.
    """
    _unblock_pending_waits(state, slot)
    await state.sessions.reset(session_key)


def _resolve_stop_event(slot: _ChatSlot, outcome: str) -> None:
    """Update the in-flight stop_event message in place with final state."""
    stop_id = slot._stop_event_id
    logger.debug("_resolve_stop_event: outcome=%s stop_id=%r", outcome, stop_id)
    if not stop_id:
        return
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    final_state = "stopped" if outcome == "soft" else "stop_failed_reset"
    found = False
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        cls_data["state"] = final_state
        cls_data["outcome"] = outcome
        cls_data["ts_end"] = now_ts
        serialized = json.dumps(cls_data)
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        found = True
        # Re-broadcast updated stop_event so frontend StopEventCard
        # transitions from "stopping" → "stopped"/"stop_failed_reset".
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-broadcast failed", exc_info=True)
        break
    if not found:
        logger.debug("_resolve_stop_event: no matching message for stop_id=%s", stop_id)
    slot._stop_event_id = None


def _make_stop_resolver(
    state: DashboardState, slot: _ChatSlot, outcome: str, card_id: str | None
) -> Callable[[], Awaitable[None]]:
    """Build the stop_turn on_soft/on_hard callback that settles the stop card.

    Key the guard on `_stop_event_id`, not on `_stop_state`. The card id is
    already the idempotency token: `_resolve_stop_event` no-ops when it is None
    and clears it once it has settled the card, so a state gate buys nothing
    there. What the state gate did buy was a bug. A turn tearing down
    concurrently drives `_stop_state` back to "idle" (`_finish_queue_cycle` in
    chat_runner.py, through the `_stopping` setter in state.py), and that
    teardown races the escalation. When teardown won, the hard callback bailed,
    `_resolve_stop_event` never ran, and the card pulsed at "stopping" for the
    rest of the session instead of settling to "stop_failed_reset".

    Precedence needs its own non-racy marker. A cooperative ack that arrives
    after the user escalated must not relabel a hard kill as a clean stop, and
    `_stop_state` cannot carry that fact because the same teardown resets it to
    "idle" from `killing` just as readily as from `soft_pending`. Reading it
    here would reproduce the bug one dimension over: teardown erases the
    escalation, the late soft callback sees a neutral state, and the card
    settles as "stopped" for a session that was killed. So the escalation path
    sets `slot._stop_escalated_card_id`, which teardown never touches, and only
    the soft callback defers on it. `hard` is terminal and nothing outranks it.
    The marker holds an id rather than a flag so it cannot leak onto a later
    card: a bare boolean left set would make the NEXT card's cooperative ack
    defer to a hard callback that never fires, stranding that card at
    "stopping", which is the failure this change exists to remove.

    Bind to `card_id`, the specific card this callback was created for, and not
    to whatever card happens to be in flight when it fires. `stop_turn` awaits
    these callbacks, so one can still be pending when teardown resets the stop
    posture, a new turn starts, and a second stop opens a NEW card. Reading
    `slot._stop_event_id` at call time would then settle that newer card with
    this older outcome and clear its posture, so the newer stop's own callback
    would find nothing left to settle. Callers pass the id they just assigned.

    `card_id` may be None, for a stop that escalated before any card existed.
    Such a callback still releases the stop posture; it simply has no card to
    label. Only a mismatching non-None current id means "someone else owns
    this", so only that case returns without touching the slot.
    """

    async def _resolve() -> None:
        logger.debug(
            "stop resolver (%s): card_id=%r current=%r stop_state=%r escalated=%r",
            outcome,
            card_id,
            slot._stop_event_id,
            slot._stop_state,
            slot._stop_escalated_card_id,
        )
        # Bail only when a DIFFERENT card is genuinely in flight, because that
        # card belongs to a later stop that owns the posture. Do not bail merely
        # because this attempt has no card: settling a card and releasing the
        # stop posture are separate jobs, and the posture must be released even
        # when there was never a card to settle. A stop can reach a callback
        # with `card_id` None: `api_chat_slot_interrupt` claims
        # `_stop_state = "soft_pending"` before it awaits the request body and
        # only then opens its card, so a concurrent `/stop` escalates against a
        # slot that has none yet. Skipping the reset there strands `_stop_state`
        # at "killing", which permanently suppresses re-queue
        # (`_should_suppress_requeue`) and rejects every later interrupt. That
        # wedges the slot, which is worse than the mislabel this guard prevents.
        if slot._stop_event_id is not None and slot._stop_event_id != card_id:
            return
        # `card_id is None` cannot mean "escalated": the marker holds a real
        # card id, so comparing None to None would defer a callback that no
        # hard kill will ever follow, and the posture would never be released.
        if outcome == "soft" and card_id is not None and slot._stop_escalated_card_id == card_id:
            logger.debug("stop resolver (soft): escalated to hard kill, deferring to hard")
            return
        # No-ops when there is no card, which is exactly the case above.
        _resolve_stop_event(slot, outcome)
        slot._stop_state = "idle"
        if card_id is not None and slot._stop_escalated_card_id == card_id:
            slot._stop_escalated_card_id = None
        state.push_slots_update()

    return _resolve


async def api_chat_slot_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/stop — cooperative stop with kill fallback.

    First press: soft cancel (cooperative). Second press (?force=true):
    hard kill. Inserts a stop_event message into the slot transcript.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    force = request.query.get("force", "").lower() == "true"

    # Escalation path: a second stop press while a cooperative cancel is
    # already pending hard-kills. We escalate on ANY second press — not only
    # when the client computed force=true — because the client derives force
    # from the WS-echoed stop_state, which may lag behind the actual state on a
    # slow connection. The backend's own _stop_state is the authoritative
    # "already soft_pending" signal, so a second press always means "kill it".
    if slot._stop_state == "soft_pending":
        slot._stop_state = "killing"
        # Survives turn teardown, which resets _stop_state to "idle". Without
        # it a cooperative ack from the first press could still land and label
        # this hard kill a clean stop. Scoped to this card so it cannot defer
        # a later card's ack.
        slot._stop_escalated_card_id = slot._stop_event_id
        slot._queue.clear()
        # Hard kill = "discard everything": drop unconsumed steers too, so the
        # end-of-turn requeue (chat_runner finally) has nothing to resurrect.
        # Mirrors the queue clear above; a soft stop preserves both.
        slot._pending_steers.clear()
        state.push_slots_update()
        logger.info("Stop (force): hard-killing session for slot %s", name)

        # Escalation reuses the card the first press opened, so bind to it.
        _on_hard_force = _make_stop_resolver(state, slot, "hard", slot._stop_event_id)

        # Unblock chat runner if it's suspended waiting for tool approval or on
        # a pending ask_question card.
        _unblock_pending_waits(state, slot)
        await state.sessions.stop_turn(_history_key_for(name), force=True, on_hard=_on_hard_force)
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="hard",
            # Record what the client requested (force flag) vs. the escalation
            # the backend actually performed (always a hard kill here).
            metadata={"slot": name, "force": force, "escalated": True},
        )
        return web.json_response({"ok": True})

    # Already stopping or not running — no-op (idempotent repeat press guard)
    if slot._stop_state != "idle" or not slot.running:
        if not slot.running:
            logger.info("Stop: slot %s not running, ignoring", name)
            _info = "not running"
        else:
            _info = "stop already in progress"
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": _info},
        )
        return web.json_response({"ok": True, "info": _info})

    # First press: soft stop
    slot._stop_state = "soft_pending"
    # NOTE: Do NOT clear the queue here — stop should only cancel the
    # currently running turn, leaving queued messages intact for the user
    # to process or dismiss individually.
    _was_auto = slot._auto_run
    slot._auto_run = False
    if _was_auto:
        sel().log(
            SecurityEvent(
                event_id=uuid.uuid4().hex,
                timestamp=datetime.now(tz=timezone.utc).isoformat(),
                event_type="auto_run_stopped",
                caller_identity=f"dashboard:{slot.key}",
                agent=getattr(slot, "agent", ""),
                source="dashboard",
                operation="stop",
                outcome="stopped",
                resources=f"slot={slot.key}",
            )
        )

    # Defensive stale-card sweep: resolve any orphaned stop card from a prior attempt
    if slot._stop_event_id:
        _resolve_stop_event(slot, "soft")

    # Insert stop_event message into transcript
    stop_id = f"stop-{uuid.uuid4().hex}"
    slot._stop_event_id = stop_id
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    stop_data = {
        "kind": "stop_event",
        "id": stop_id,
        "state": "stopping",
        "outcome": None,
        "ts_start": now_ts,
    }
    # cls must be JSON-encoded so parse_cls_meta() populates meta on the wire.
    # content mirrors the data for backward-compat with any consumer that only
    # reads content.
    stop_msg = json.dumps(stop_data)
    slot.append("system", stop_msg, stop_msg)
    state.push_slots_update()
    logger.info("Stop: cooperative cancel for slot %s (queue=%d)", name, len(slot._queue))

    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        _history_key_for(name), force=False, preserve_queue=True, on_soft=_on_soft, on_hard=_on_hard
    )
    # Resolve orphaned card when provider reports no active turn
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "force": False},
    )
    return web.json_response({"ok": True})


async def api_chat_slot_continue(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/continue — hand the thread back to the agent.

    Two callers, one mechanism: picking up a turn that was cut short, and asking
    a slot that finished cleanly to carry on. They are one endpoint because they
    are indistinguishable from the transcript — a force-quit runs no ``finally``,
    so a killed turn leaves no error row behind and reads exactly like a
    completed one. ``_has_conversation`` authorizes; ``_is_interrupted`` only
    chooses which of the two continuation bodies the model receives.

    Runs the same synthetic-continuation machinery the runner already uses for
    its own post-transient recovery: queue the continuation at the head, then let
    ``_start_next_queued_turn`` land it as an ``inject`` row and dispatch the
    turn. No bespoke dispatch path, and the row folds into the existing recovery
    card instead of printing machine prose as a user bubble.

    The frontend decides whether to OFFER this (it has the transcript, `running`
    and the queue locally, so it needs no server field for that). This endpoint
    re-checks under ``slot._lock`` because the client's view is a WS snapshot and
    therefore lagging: a press landing in the instant a turn starts, or a second
    browser tab acting on a stale cache, would otherwise dispatch a duplicate
    turn against one slot — real tokens, real tool calls, real repo writes. Every
    other dispatch route guards the same way (see ``api_chat_slot_regenerate``).
    """
    blocked = await reject_if_kiro_unverified(request)
    if blocked is not None:
        return blocked
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens, mirroring
    # api_chat. Without it an app token holding /api/chat could resume ANY
    # interrupted slot — including a dashboard user's — and that is not a read: it
    # dispatches an agent turn that runs tools and writes to the repo. Same
    # indistinguishable 404 as the send path, so the response cannot be used to
    # probe which foreign slots exist.
    request_app = request.get("app", "")
    if request_app and request_app != slot._app:
        sel().log_api_access(
            caller=request_app,
            operation="chat_continue",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error=(
                "app cannot access unscoped slots" if not slot._app
                else "app does not own this slot"
            ),
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    async with slot._lock:
        if slot.running:
            return web.json_response(
                {"error": "slot is running", "code": "slot_running"}, status=409
            )
        if slot._in_stage_execution:
            # An autopilot plan reads `running` False BETWEEN stages while it is
            # still mid-plan, so `running` alone would let a Continue dispatch
            # concurrently with the next stage — two turns interleaving tool calls
            # and repository writes on one slot.
            return web.json_response(
                {"error": "slot is orchestrating", "code": "slot_orchestrating"}, status=409
            )
        if slot._stopping or slot._stop_state != "idle":
            return web.json_response(
                {"error": "a stop is in progress", "code": "slot_stopping"}, status=409
            )
        if slot.queue_depth:
            # The runner is about to pick the thread back up on its own; adding a
            # continuation would double-fire.
            return web.json_response(
                {"error": "queued messages pending", "code": "slot_queue_pending"}, status=409
            )
        if any(not f.done() for f in slot._approval_futures.values()):
            return web.json_response(
                {"error": "approval pending", "code": "slot_approval_pending"}, status=409
            )
        # Background sub-agents are still running (or waiting to start) for this
        # slot. `slot.running` is False here — the parent turn ENDS while its
        # children keep going — so nothing above catches this, and the widened
        # gate below makes it the common shape rather than the rare one (before
        # this endpoint accepted a settled transcript, a parent that finished
        # cleanly after `spawn_run` was refused only incidentally, by
        # `_is_interrupted`).
        #
        # It has to be refused HERE rather than left to the queue: a synthetic
        # recovery entry satisfies `is_system_injection_item`, so
        # `_dequeue_next_system_message` drains it straight through the
        # `hold_users` gate that exists to stop exactly this (chat_runner) — the
        # hold only holds plain USER messages. A parent turn would start and
        # interleave tool calls and repository writes with its own children's
        # completion injections. `api_chat` queues instead of dispatching for the
        # same reason; Continue has nowhere to queue to, so it refuses.
        #
        # Two things this must NOT get wrong, both of which look like a working
        # guard right up until they lose a file write:
        #
        # * `effective_session_key`, never `f"dashboard:{slot.key}"`. A slot born
        #   on a channel carries the channel key (`slack:<ts>`) and its children
        #   register under THAT, so the dashboard-prefixed form silently matches
        #   nothing — `_history_key_for`'s own docstring says as much.
        # * QUEUED children count. A spawn that hit the concurrency/stagger gate
        #   is deliberately absent from `_agents` (see `SubagentInfo.queued`), so
        #   `running_agents_for` cannot see it, yet it WILL start on its own and
        #   write concurrently with the turn this endpoint would dispatch.
        # * IN-FLIGHT RESULT DELIVERY counts too. The last child can finish —
        #   emptying both probes — while its `[Subagent completion event]`
        #   injection is still landing. Starting a turn in that window interleaves
        #   with the injection and corrupts transcript order. This is why the
        #   runner's own synthesis gate pairs the two conditions at BOTH its call
        #   sites (`chat_runner.py:2273` and `:2305`): `running_agents_for(...)`
        #   alone is not "no children are touching this slot".
        subs = getattr(state, "subagents", None)
        if subs is not None:
            child_key = effective_session_key(slot)
            running = subs.running_agents_for(child_key)
            # Fail closed on None: that is the probe FAILING, not a slot with no
            # children, and mistaking the two dispatches the interleaved turn this
            # guard exists to prevent. Mirrors the stage gate in chat_orchestrator.
            queued = 0
            if running is not None:
                try:
                    queued = subs._queued_depth(child_key)
                except Exception:
                    # An unreadable queue is unknown children, not zero children.
                    logger.debug("continue: queued-depth probe failed", exc_info=True)
                    queued = 1
            inflight = getattr(slot, "_subagent_deliveries_inflight", 0)
            if running is None or running or queued or inflight:
                return web.json_response(
                    {"error": "sub-agents are running", "code": "slot_subagents_running"},
                    status=409,
                )
        if not _has_conversation(slot):
            return web.json_response(
                {"error": "nothing to continue", "code": "slot_empty"}, status=409
            )

        # _is_interrupted no longer AUTHORIZES the continue — it only picks which
        # body to inject. Both are true statements about their own case, and
        # getting this wrong is not cosmetic: telling a model that finished
        # cleanly that it was "interrupted before it finished" sends it looking
        # for half-done work that does not exist.
        resume = _MANUAL_RESUME_MSG if _is_interrupted(slot) else _MANUAL_CONTINUE_MSG
        slot.queue_insert(0, resume, kind=SYNTHETIC_RECOVERY_KIND)

    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_continue",
        tool_kind="command",
        outcome="ok",
        metadata={"slot": name},
    )
    started = await _start_next_queued_turn(state, slot)
    if not started:
        # Lost a race for the queue entry (a concurrent dequeue consumed it).
        # The turn is running either way, so this is not an error for the caller.
        logger.info("continue: queue entry consumed by a concurrent dequeue (slot %s)", name)
    state.push_slots_update()
    return web.json_response({"ok": True, "slot": slot.key})


def _has_conversation(slot: _ChatSlot) -> bool:
    """True when the transcript holds a real turn to continue FROM.

    The authorization check behind Continue. It is deliberately weak — anything
    a person could look at and say "carry on with that" qualifies — because a
    hard-killed gateway writes no error row, so an interrupted turn is often
    shape-identical to a completed one and no predicate can separate them. The
    button is therefore offered on any idle slot with a transcript, and this
    guard only refuses the one case with nothing to reason about at all: an empty
    slot (or one holding only scaffolding rows such as a compaction notice),
    where a continuation would reach the model with no conversation under it.

    Rows are walked with the same skip rules as ``_is_interrupted`` so the two
    cannot disagree about what counts as the conversation's floor.
    """
    for m in slot.messages:
        meta = m.get("meta") or {}
        if m.get("role") == "assistant" and meta.get("kind") == "compaction":
            continue
        if m.get("role") in ("user", "assistant") and m.get("content"):
            return True
    return False


def _is_stop_event(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Three carriers, and the in-memory one is the easy miss: the stop is appended
    as ``slot.append("system", stop_msg, stop_msg)`` with **no** ``meta=`` kwarg,
    so ``_ChatSlot.append`` never creates a ``meta`` key and the discriminator
    exists ONLY inside the JSON-encoded ``cls``/``content``. ``parse_cls_meta()``
    is what unpacks it, and it runs on the way OUT to a client
    (``_prepare_messages`` / ``_broadcast_chat_message``) — which is why the
    frontend sees ``meta.kind`` while this module, reading the live window, does
    not. Checking only ``kind``/``meta.kind`` here therefore matched a restored
    row but never a freshly-stopped one, silently diverging from the frontend
    mirror in exactly the case the two must agree on.

    Mirrors ``isStopEvent`` in ``website/src/store/chatSlice.ts``.
    """
    if m.get("kind") == "stop_event":
        return True
    meta = m.get("meta") or {}
    if meta.get("kind") == "stop_event":
        return True
    # Live window: the discriminator is still JSON inside `cls`.
    parsed = parse_cls_meta(m.get("cls") or "")
    return bool(parsed and parsed.get("kind") == "stop_event")


def _is_interrupted(slot: _ChatSlot) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Two shapes qualify: the last conversational row is the USER's (nothing came
    back at all — a gateway restart mid-turn leaves exactly this), or it is the
    ASSISTANT's but an error row follows it (the turn streamed partway then died,
    which is otherwise shape-identical to a clean completion).

    One shape is explicitly excluded: a trailing ``stop_event``. The user pressing
    Stop is a deliberate ending, not an interruption, and stopping before the
    reply emitted any text produces the same ``[user, ...]`` tail as a crash.

    Still selects the wording injected for the model (``_MANUAL_RESUME_MSG`` vs
    ``_MANUAL_CONTINUE_MSG``), and on the dashboard it now also gates whether the
    composer offers the control at all — see the ``continuable && interrupted``
    composition in ``website/src/pages/ChatPage.tsx``. A False result means "as
    far as the transcript shows, the last turn finished or was ended on purpose",
    NOT "there is nothing to do": a force-quit runs no ``finally``, so the error
    row that would have proved an interruption was never written.

    Deliberately does not distinguish "produced some output" from "produced
    none": ``_MANUAL_RESUME_MSG`` is worded to hold in both cases, so the
    distinction would buy a branch and nothing else.
    """
    saw_trailing_error = False
    for m in reversed(slot.messages):
        role = m.get("role")
        meta = m.get("meta") or {}
        # A deliberate Stop ENDS the turn; it does not interrupt it. Tested
        # before the user/assistant branch because stopping before the reply
        # emitted any text leaves ``[user, stop_event]`` -- shape-identical to
        # "the gateway died before anything came back". See ``_is_stop_event``
        # for why the discriminator has to be resolved from three carriers.
        # Only the NEWEST turn's terminator reaches here -- an older stop card
        # is never scanned, because a later user/assistant row returns first.
        if _is_stop_event(m):
            return False
        if role == "assistant" and meta.get("kind") == "compaction":
            continue
        if role in ("user", "assistant") and m.get("content"):
            return True if role == "user" else saw_trailing_error
        if role == "error":
            saw_trailing_error = True
    return False


async def api_chat_slot_interrupt(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/interrupt — interrupt current turn and
    immediately process the next queued message.

    Unlike /stop which clears the queue, this preserves it so the dequeue
    loop in chat_runner's finally block picks up the next message.
    Optionally accepts {"queue_id": "..."} to promote a specific queued
    message to the front before stopping.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    if not slot.running:
        return web.json_response({"ok": True, "info": "not running"})
    # Idempotent guard: interrupt already in progress. State alone decides —
    # do NOT also require _stop_event_id: after the early soft_pending claim
    # below, a concurrent request can arrive before the stop card is created
    # (event id still None), and a compound condition would let it through.
    if slot._stop_state != "idle":
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop already in progress"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})
    if not slot._queue:
        return web.json_response({"error": "queue empty, use /stop instead"}, status=400)

    # Claim the stop slot synchronously BEFORE the await below: the
    # idempotency guard above is check-then-act, and a concurrent /interrupt
    # arriving during `await request.json()` would otherwise still see
    # _stop_state == "idle" and slip past the guard (double stop_turn +
    # double SEL audit for one logical press). /stop is race-safe because it
    # has no await between guard and claim; this makes /interrupt match.
    slot._stop_state = "soft_pending"
    slot._auto_run = False

    # Optionally promote a specific queue item to front
    try:
        body = await request.json() if request.content_length else {}
    except Exception:
        slot._stop_state = "idle"
        raise
    queue_id = body.get("queue_id")
    if queue_id:
        for i, item in enumerate(slot._queue):
            if item.get("queue_id") == queue_id:
                slot._queue.insert(0, slot._queue.pop(i))
                break

    # Stop current turn but preserve the queue so dequeue loop fires
    # (soft_pending already claimed above, before the request-body await)

    # Defensive stale-card sweep
    if slot._stop_event_id:
        _resolve_stop_event(slot, "soft")

    # Insert stop_event for UI feedback
    stop_id = f"stop-{uuid.uuid4().hex}"
    slot._stop_event_id = stop_id
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    stop_data = {
        "kind": "stop_event",
        "id": stop_id,
        "state": "interrupting",
        "outcome": None,
        "ts_start": now_ts,
    }
    stop_msg = json.dumps(stop_data)
    slot.append("system", stop_msg, stop_msg)
    state.push_slots_update()

    # Built after the card exists so each resolver is bound to this card.
    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        _history_key_for(name),
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # Resolve orphaned card when provider reports no active turn
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_interrupt",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "queue_id": queue_id},
    )
    return web.json_response({"ok": True, "outcome": outcome})


async def api_chat_slot_queue_cancel(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot}/queue/{queue_id} — cancel a queued message.

    Removes the message from the backend queue and broadcasts a
    ``queue_cancel`` WebSocket event so the frontend can move the
    text back to the input box.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    content = slot.queue_remove_by_id(queue_id)
    if content is None:
        return web.json_response({"error": "queue item not found"}, status=404)
    _remove_queued_by_id(slot.messages, queue_id)
    slot.invalidate_source_links()
    _redacted = _redact_for_display(content)
    state.broadcast_ws("queue_cancel", {"slot": name, "queue_id": queue_id, "content": _redacted})
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_cancel",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_edit(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/queue/{queue_id} — edit a queued message.

    Accepts ``{"content": "new text"}`` and replaces the content of the
    matching queue item in place (order preserved).  Broadcasts a
    ``queue_edit`` WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    queue_id = request.match_info["queue_id"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    content = body.get("content")
    if not isinstance(content, str) or not content.strip():
        return web.json_response({"error": "content must be a non-empty string"}, status=400)
    if not slot.queue_edit_by_id(queue_id, content):
        return web.json_response({"error": "queue item not found"}, status=404)
    _edit_queued_by_id(slot.messages, queue_id, content)
    slot.invalidate_source_links()
    _redacted = _redact_for_display(content)
    state.broadcast_ws("queue_edit", {"slot": name, "queue_id": queue_id, "content": _redacted})
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_edit",
        tool_kind="permission",
        outcome="allowed",
        metadata={"queue_id": queue_id, "slot": name},
    )
    return web.json_response({"ok": True, "content": _redacted})


async def api_chat_slot_queue_reorder(request: web.Request) -> web.Response:
    """PUT /api/chat/slots/{slot}/queue/order — reorder queued messages.

    Accepts ``{"order": ["qid1", "qid2", ...]}`` and rearranges the slot's
    ``_queue`` to match the given id sequence.  Broadcasts a ``queue_reorder``
    WebSocket event so all connected clients update in sync.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    order = body.get("order")
    if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
        return web.json_response({"error": "order must be a list of queue id strings"}, status=400)
    # Build lookup of current queue items by id
    by_id = {item["id"]: item for item in slot._queue}
    # Validate all ids exist
    missing = [qid for qid in order if qid not in by_id]
    if missing:
        return web.json_response({"error": f"unknown queue ids: {missing}"}, status=400)
    # Reorder: place requested ids first in given order, then any remaining
    reordered = [by_id[qid] for qid in order if qid in by_id]
    remaining = [item for item in slot._queue if item["id"] not in set(order)]
    slot._queue[:] = reordered + remaining
    # Reorder the queued messages in the messages list to match
    queued_msgs = [m for m in slot.messages if m.get("role") == "queued"]
    other_msgs = [m for m in slot.messages if m.get("role") != "queued"]
    queued_by_id: dict[str | None, dict] = {}
    for m in queued_msgs:
        try:
            cls = json.loads(m.get("cls", "{}"))
            queued_by_id[cls.get("queue_id")] = m
        except (json.JSONDecodeError, TypeError):
            pass
    reordered_msgs = [queued_by_id[qid] for qid in order if qid in queued_by_id]
    remaining_msgs = [m for m in queued_msgs if m not in reordered_msgs]
    slot.messages[:] = other_msgs + reordered_msgs + remaining_msgs
    slot.invalidate_source_links()
    state.broadcast_ws(
        "queue_reorder", {"slot": name, "order": [item["id"] for item in slot._queue]}
    )
    state.push_slots_update()
    sel().log_tool_invocation(
        session_key=f"dashboard:{name}",
        agent="kirocrew",
        source="dashboard",
        tool_name="queue_reorder",
        tool_kind="permission",
        outcome="allowed",
        metadata={"slot": name, "order_len": len(order)},
    )
    return web.json_response({"ok": True})


async def api_chat_slot_delete(request: web.Request) -> web.Response:
    """DELETE /api/chat/slots/{slot} — stop and remove a UI slot.

    Kills the per-tab kiro-cli session and saves history.  The session
    will be recreated from the warm pool if the tab is resumed later.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)

    # App ownership check (App Kit §5.2): app can only delete slots it created.
    # Unscoped slots (empty _app) cannot be deleted by app tokens.
    # Dashboard users (empty request_app) can delete anything.
    request_app = request.get("app", "")
    if request_app and slot._app != request_app:
        sel().log_api_access(
            caller=request_app,
            operation="slot_delete",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own this slot",
        )
        return web.json_response({"error": "not found"}, status=404)
    if request_app and not slot._app:
        sel().log_api_access(
            caller=request_app,
            operation="slot_delete",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app cannot delete unscoped slots",
        )
        # 404 (not 403): a foreign/unscoped slot is indistinguishable from a
        # missing one — anti-enumeration (CWE-204); true reason logged via SEL.
        return web.json_response({"error": "not found"}, status=404)

    # Remove from dict before async operations
    state._slots.pop(name, None)
    # Synchronous tombstone, BEFORE any await: a channel-slot reconcile pass
    # whose snapshot predates this close reads these after its last await, so
    # it cannot re-surface the tab this handler is dismissing (see
    # channel_slots._RECENT_CLOSES). The returned instant is persisted as
    # closed_at below — the save runs after the cancellation awaits, and
    # stamping save time would make channel activity landing in that window
    # compare as older than the close.
    closed_at = note_slot_closed(state, name)
    # Release any blocking wait before cancelling the task: a pending
    # ask_question holds an MCP worker on a blocked HTTP request, and the slot
    # is going away, so nobody will ever answer its card.
    _unblock_pending_waits(state, slot)
    if slot.running and slot.task is not None:
        slot.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(slot.task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    try:
        await save_slot_off_loop(
            state, slot, closed=True, closed_at=closed_at, best_effort=False
        )
    except Exception:
        # Save failed — restore slot so data isn't lost
        logger.error("Failed to save slot %s to history, restoring", name, exc_info=True)
        state._slots[name] = slot
        _sync_dashboard_slots(state)
        state.push_slots_update()
        return web.json_response({"error": "failed to save history"}, status=500)
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
    # Kill the per-tab session to free resources
    await state.sessions.remove(_history_key_for(name))
    _sync_dashboard_slots(state)
    state.push_slots_update()
    state.push_refresh("history")
    return web.json_response({"ok": True})


async def api_chat_slots_cleanup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/cleanup — bulk-archive inactive sessions to history.

    Body: ``{"max_inactive_days": 3, "active_slot": "chat-1-123"}``
    Skips the active slot and pinned sessions.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    max_days = 3
    try:
        max_days = max(1, int(body.get("max_inactive_days", 3)))
    except (ValueError, TypeError):
        pass
    active_slot = body.get("active_slot", "")
    dry_run = body.get("dry_run", False)
    request_app = request.get("app", "")
    cutoff = time.time() - max_days * 86400
    stale_keys: list[str] = []
    active_is_stale = False
    for name in list(state._slots):
        slot = state._slots.get(name)
        if slot is None or slot.pinned:
            continue
        # App Kit ownership isolation: app callers can only archive
        # their own slots. Dashboard users (empty request_app) pass
        # through and can archive anything.
        if request_app:
            if slot._app != request_app:
                continue
        last_activity = 0.0
        if slot.messages:
            for m in reversed(slot.messages):
                ts = m.get("ts", "")
                if not ts:
                    continue
                try:
                    dt = datetime.fromisoformat(ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    last_activity = dt.timestamp()
                except (ValueError, TypeError):
                    continue
                break
        if not last_activity:
            try:
                dt = datetime.fromisoformat(slot.created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                last_activity = dt.timestamp()
            except Exception:
                last_activity = 0.0
        if not last_activity:
            continue  # unknown activity — don't archive
        if last_activity >= cutoff:
            continue
        if name == active_slot:
            active_is_stale = True
            continue
        stale_keys.append(name)
    # Dry-run: return the exact list without archiving
    if dry_run:
        sel().log_api_access(
            caller="dashboard",
            operation="chat.cleanup_dry_run",
            outcome="allowed",
            source="dashboard",
            resources=f"count={len(stale_keys)} threshold={max_days}d",
        )
        return web.json_response(
            {
                "ok": True,
                "dry_run": True,
                "keys": stale_keys,
                "count": len(stale_keys),
                "active_is_stale": active_is_stale,
            }
        )
    archived: list[str] = []
    failed: list[str] = []
    _tasks_to_cancel: list[asyncio.Task] = []
    for name in stale_keys:
        removed = state._slots.pop(name, None)
        if not removed:
            continue
        # Same tombstone as the single-tab close: the archive pass must not
        # race a concurrent channel reconcile into resurrecting the slot. Its
        # instant is persisted as closed_at for the same teardown-window
        # reason as the single-tab path.
        closed_at = note_slot_closed(state, name)
        try:
            await save_slot_off_loop(
                state, removed, closed=True, closed_at=closed_at, best_effort=False
            )
        except Exception:
            logger.error("Cleanup: failed to archive slot %s", name, exc_info=True)
            state._slots[name] = removed
            failed.append(name)
            continue
        else:
            state._restricted_keys.discard(f"dashboard:{name}")
        # Session cleanup is best-effort — history is already written
        try:
            await state.sessions.remove(_history_key_for(name))
        except Exception:
            logger.warning("Cleanup: session remove failed for %s", name, exc_info=True)
        archived.append(name)
        # Collect running tasks for concurrent cancellation after the loop
        if removed.running and removed.task is not None:
            removed.task.cancel()
            _tasks_to_cancel.append(removed.task)
    # Await all cancelled tasks concurrently with a single bounded timeout
    if _tasks_to_cancel:
        await asyncio.wait(_tasks_to_cancel, timeout=5.0)
    if archived:
        _sync_dashboard_slots(state)
        state.push_slots_update()
        state.push_refresh("history")
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slots_cleanup",
        outcome="ok" if not failed else ("partial" if archived else "error"),
        source="dashboard",
        resources=f"archived={len(archived)} failed={len(failed)} threshold={max_days}d keys={','.join(archived[:10])}",
    )
    return web.json_response(
        {"ok": True, "archived": len(archived), "keys": archived, "failed": failed}
    )


async def api_chat_slot_agent(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/agent — set agent for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    agent_name = body.get("agent", "")
    if agent_name and not _AGENT_NAME_RE.match(agent_name):
        return web.json_response({"error": "invalid agent name"}, status=400)

    # Stored verbatim — never rewritten to whatever currently answers. See the
    # same reasoning in api_chat_slot_create.
    slot.agent = agent_name

    # Resolve workspace from agent bindings
    workspace = "default"
    try:
        cfg = KiroCrewConfig.load()
        if agent_name:
            # Resolve by the name being STORED, which is exactly the name dispatch
            # will resolve later (`chat_runner` -> resolve_agent_bindings(
            # slot.agent)). Looking it up as an alias first and taking THAT
            # alias's workspace disagreed with dispatch whenever the two differ:
            # a name that is merely some alias's `kiro_agent` target, or a
            # materialized app agent, dispatches with the DEFAULT bindings while
            # the slot had recorded the alias's workspace. A materialized agent
            # previously matched nothing here at all, so the slot kept the
            # PREVIOUS agent's project — latent until app agents could dispatch.
            bindings = resolve_agent_bindings(cfg, agent_name)
            ws_name = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            slot.workspace = ws_name
            workspace = ws_name
            slot.project = default_project_dir(workspace)
    except Exception:
        logger.warning("Failed to resolve agent bindings for %r", agent_name, exc_info=True)

    # Reset session so next message uses the new agent
    logger.info("Slot %s agent switched to %r, resetting session", name, agent_name or "kirocrew")
    await _reset_slot_session(state, slot, _history_key_for(name))
    # Persist the new agent so the session resumes under the correct agent
    # after a gateway restart.  Written after reset succeeds so we never
    # advertise an agent we couldn't actually switch to.
    if state.conversation_log:
        try:
            # update_metadata enters _locked (flock + os.close); those are
            # blocking-on-loop-prohibited, so offload to a worker thread rather
            # than run them on the event loop (a wedged peer must never freeze
            # chat/WS/heartbeat).
            await asyncio.to_thread(
                state.conversation_log.update_metadata,
                _history_key_for(name),
                {"agent": agent_name},
            )
        except Exception:
            logger.warning("Failed to persist agent for slot %s", name, exc_info=True)
    state.push_slots_update()
    return web.json_response({"ok": True, "agent": agent_name, "workspace": workspace})


def _model_rejected_reason(model_name: str) -> str | None:
    """Reason to reject ``model_name`` for the active provider, or None to allow.

    The dashboard model dropdown falls back to canonical registry keys (e.g.
    ``fable-5-1m``) when /api/models is unavailable (gateway restart / kiro-cli
    cold-start timeout). Those keys are DISPLAY identifiers the ACP CLI rejects
    as model ids (-32603 "model not available") — persisting one into
    ``slot.model`` breaks the next turn. This guard is defense-in-depth behind
    the frontend's auto-only fallback: a stale client, a direct API
    call, or the openai-compat path can never persist a canonical key. ``auto``
    and ``""`` (provider default) always pass; for the ``claude_code`` provider
    canonical keys ARE the wire format, so they pass there too.
    """
    if not model_name or model_name == "auto":
        return None
    try:
        provider = KiroCrewConfig.load().agent.provider
    except Exception:  # pragma: no cover - config load is resilient
        provider = ""
    if provider == "claude_code":
        return None
    if model_registry.is_canonical_key(model_name):
        return (
            f"{model_name!r} is a display-only model identifier the "
            f"{provider or 'active'} provider does not accept; "
            f"select a listed model or 'auto'."
        )
    return None


def _wire_model_id(provider: AcpProvider, model_name: str) -> str:
    """Translate a canonical model key into the id THIS backend accepts.

    ``slot.model`` holds a canonical/wire value while ``session/set_model`` only
    accepts the backend's own ids — two namespaces. Mirrors the normalisation the
    warm-pool post-claim switch does in ``SessionManager``: kiro wants the bare
    dotted id via ``to_acp_id`` (which translates canonical keys and passes
    kiro's own ids through unchanged), the claude backend wants the
    ``global.anthropic.*`` id.

    Returns "" when the change cannot be expressed as a ``set_model`` on this
    backend, which tells the caller to fall back to a session reset.
    """
    # The dashboard sends "" for Auto, but the literal "auto" also passes the
    # guard (stale clients / direct API calls), so both mean "provider default".
    is_default = model_name in ("", "auto")
    if provider.is_claude_backend:
        # The claude backend has no id meaning "let the server choose", so
        # returning to default needs a reset.
        return "" if is_default else model_registry.to_provider_id(model_name, "claude_code")
    if is_default:
        # kiro DOES express Auto as a real model id — but only switch to it when
        # this session's backend actually advertised it.
        advertised = {m.get("modelId", "") for m in provider.available_models()}
        return "auto" if "auto" in advertised else ""
    return model_registry.to_acp_id(model_name)


async def _reapply_effort_after_live_switch(
    name: str, slot: _ChatSlot, provider: AcpProvider
) -> bool:
    """Re-apply the slot's reasoning effort to the model we just switched to.

    The kiro effort overlay is written before every (re)spawn, so a cold start
    picks the level up for free. An in-place switch never respawns, so without
    this the new model would run at its own default while the UI still reports
    the slot's level. Pushes it live through the same provider calls
    ``api_chat_slot_reasoning_effort`` uses.

    Returns False to ask the caller for a reset, which re-applies effort through
    the provider factory instead.
    """
    if not provider.supports_effort():
        # The new model has no effort selector. slot.reasoning_effort stays
        # persisted for when the user switches back to a capable model — same
        # "persisted no-op" the effort endpoint applies.
        return True
    try:
        if slot.reasoning_effort:
            return bool(await provider.change_effort(slot.reasoning_effort))
        # No slot override: re-resolve so a workspace default reaches the new
        # model, matching what a respawn's overlay would have written. A False
        # return is benign HERE, unlike in the effort endpoint: it means there
        # was no default to push, and since the user never set a level for THIS
        # model there is nothing stale on the session to undo either.
        await provider.clear_effort()
        return True
    except Exception as exc:
        logger.warning(
            "Effort re-apply after live model switch failed for slot %s: %s: %s"
            " — falling back to reset",
            name,
            type(exc).__name__,
            exc,
        )
        return False


async def _try_live_model_switch(
    name: str, slot: _ChatSlot, provider: LLMProvider | None, model_name: str
) -> bool:
    """Apply a model change to the LIVE session instead of tearing it down.

    ``session/set_model`` switches the model on a running kiro-cli session.
    Verified against kiro-cli 2.15.1: acked synchronously, carries the existing
    conversation across the switch (including across vendors), sticks over
    subsequent turns, and switches back. That makes a session reset
    unnecessary for an idle slot — and the reset is expensive twice over, since
    it kills the whole process tree now AND forces the next message to
    cold-start and replay a compressed transcript.

    Returns True when the live session owns *model_name*. False means the caller
    must fall back to a reset — including when there is no live session at all,
    where the reset is an O(1) no-op teardown but still routes through
    ``_reset_slot_session``'s pending-wait cleanup.
    """
    if not isinstance(provider, AcpProvider):
        return False
    if provider.has_active_turn():
        # Same hazard api_chat_slot_reasoning_effort documents: awaiting a
        # response mid-turn races the streaming prompt loop on stdout for the
        # non-multiplexed client. The UI disables the model button while a turn
        # runs, so this is defensive — take the old reset path.
        return False
    wire = _wire_model_id(provider, model_name)
    if not wire:
        return False
    try:
        await provider.client.set_model(wire)
    except AcpModelUnavailable:
        # NOT a "the call didn't land" failure, so the reset fallback below is
        # the wrong recovery: it would tear down the live conversation and then
        # cold-start on a DIFFERENT model while the caller reported success.
        # Propagate so the handler answers 4xx and the slot keeps its old model.
        raise
    except Exception as exc:
        logger.warning(
            "Live set_model(%s) failed for slot %s: %s: %s — falling back to reset",
            wire,
            name,
            type(exc).__name__,
            exc,
        )
        return False
    if not await _reapply_effort_after_live_switch(name, slot, provider):
        return False
    logger.info("Slot %s model switched live to %r (session preserved)", name, wire)
    return True


def _broadcast_context_reset(state: "DashboardState", slot_key: str, provider: Any) -> None:
    """Push one ``context_usage`` event so the meter updates on a model switch.

    Without this the frontend keeps the previous model's stored ``{used,
    window}`` until the next turn emits an event. ``reset: true`` tells the
    ``sseContextUsage`` reducer it may REPLACE or DELETE the stored token entry
    (a frame WITHOUT ``reset`` never deletes, so the backend sets ``reset``
    whenever it has no real counts to send). With a live provider the payload
    carries the freshly rebased stats from ``set_model``; without one (the
    session-reset path) it carries no tokens, so the reducer deletes the entry
    and the UI falls back to its own model-derived window for the slot's new
    model. Best-effort: a broadcast failure must not fail the switch.
    """
    try:
        if provider is not None:
            payload = _context_usage_payload(slot_key, provider)
        else:
            payload = {"slot": slot_key, "pct": 0.0}
        payload["reset"] = True
        state.broadcast_context_usage(slot_key, payload)
    except Exception:
        logger.exception("Failed to broadcast context_usage reset for slot %s", slot_key)


async def api_chat_slot_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/model — set model for a chat slot.

    Prefers an in-place ``session/set_model`` on the running session and only
    resets when that is impossible (no ACP provider, a turn in flight, an
    unrepresentable target, or the live call failing).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    model_name = _normalize_model(body.get("model", ""))
    reason = _model_rejected_reason(model_name)
    if reason:
        logger.warning("Slot %s model rejected: %s", name, reason)
        return web.json_response({"error": reason}, status=400)
    if slot.model == model_name:
        return web.json_response({"ok": True, "model": model_name})
    session_key = _history_key_for(name)
    provider = state.sessions.get_provider(session_key)
    prior_model = slot.model
    slot.model = model_name
    try:
        went_live = await _try_live_model_switch(name, slot, provider, model_name)
    except AcpModelUnavailable as exc:
        # The live session refused the pick as unavailable to this account. Roll
        # the slot back so the picker keeps showing what is actually running, and
        # answer 4xx — deliberately NOT the reset fallback below, which would
        # destroy the conversation and cold-start on a different model while
        # reporting success. Only the session that owns the advertised list gets
        # to make this call, so there is no pre-emptive gate here to go stale.
        slot.model = prior_model
        logger.warning("Slot %s model rejected: %s", name, exc)
        return web.json_response(
            {"error": str(exc), "code": "model_unavailable"}, status=400
        )
    if went_live:
        _broadcast_context_reset(state, slot.key, provider)
    else:
        logger.info("Slot %s model switched to %r, resetting session", name, model_name or "auto")
        await _reset_slot_session(state, slot, session_key)
        _broadcast_context_reset(state, slot.key, None)
    state.push_slots_update()
    return web.json_response({"ok": True, "model": model_name})


async def api_chat_slots_model(request: web.Request) -> web.Response:
    """POST /api/chat/slots/model — set the model for ALL chat slots (bulk).

    Body: {"model": "<name>" | "", "skip_running": bool (default True)}.
    "" selects the provider/auto default. Applies the model to every slot
    whose model differs, resetting each affected slot's session — a model
    switch always resets, same as ``api_chat_slot_model``. Slots mid-turn are
    skipped when ``skip_running`` is true to avoid the model-switch-mid-stream
    duplicate-content bug; pass ``skip_running: false`` to force
    every slot. Returns the slot keys that were switched / skipped / unchanged /
    failed; a per-slot reset failure is isolated (that slot is reported in
    ``failed`` and keeps its old model) rather than aborting the whole switch.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    model_name = _normalize_model(body.get("model", ""))
    reason = _model_rejected_reason(model_name)
    if reason:
        return web.json_response({"error": reason}, status=400)
    skip_running = body.get("skip_running", True)
    if not isinstance(skip_running, bool):
        return web.json_response({"error": "skip_running must be a boolean"}, status=400)
    # Deny-by-default (security-controls): the auth middleware always sets
    # request["app"] on every authenticated path (empty string for dashboard
    # users, app name for app tokens). An ABSENT key means the middleware did
    # not run -- refuse rather than fall through to all-slot access.
    if "app" not in request:
        return web.json_response({"error": "unauthorized"}, status=403)
    request_app = request["app"]
    # Dashboard users are identified by the middleware's EXPLICIT "" assignment.
    # Compare with == "" (not truthiness) so an unexpected falsy value (None, 0)
    # fails closed into the per-slot ownership check instead of bypassing it.
    is_dashboard_user = request_app == ""

    switched: list[str] = []
    skipped_running: list[str] = []
    unchanged: list[str] = []
    failed: list[str] = []
    # Snapshot the slot keys up front: sessions.reset awaits, so iterating the
    # live dict directly would risk a concurrent-modification surprise.
    for name, slot in list(state._slots.items()):
        # App Kit ownership isolation: app callers can only switch their own
        # slots (mirrors api_chat_slots_cleanup). Only an explicit dashboard
        # user bypasses the ownership check.
        if not is_dashboard_user and slot._app != request_app:
            continue
        if slot.model == model_name:
            unchanged.append(name)
            continue
        if skip_running and slot.running:
            skipped_running.append(name)
            continue
        # Reset before flipping the model and isolate per-slot failures: if the
        # reset raises, leave slot.model untouched so the slot is never left on
        # the new model with stale history (the model/history inconsistency), and a
        # single failure doesn't abort the whole bulk switch.
        try:
            await _reset_slot_session(state, slot, _history_key_for(name))
        except Exception:
            logger.error("Bulk model switch: session reset failed for %s", name, exc_info=True)
            failed.append(name)
            continue
        slot.model = model_name
        _broadcast_context_reset(state, slot.key, None)
        switched.append(name)

    if switched:
        logger.info(
            "Bulk model switch to %r: %d switched, %d skipped-running, %d unchanged, %d failed",
            model_name or "auto",
            len(switched),
            len(skipped_running),
            len(unchanged),
            len(failed),
        )
        # Guard the push on real progress so partial switches still broadcast
        # even when a later slot's reset failed.
        state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "model": model_name,
            "switched": switched,
            "skipped_running": skipped_running,
            "unchanged": unchanged,
            "failed": failed,
        }
    )


async def api_chat_slot_reasoning_effort(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reasoning-effort — set reasoning effort.

    Body: {"reasoning_effort": "" | "low" | "medium" | "high" | "xhigh" | "max"}.
    "" = provider default (e.g. CC falls back to its opus heuristic, kiro to
    the model's default).

    Works for both ACP backends (claude-agent-acp and kiro-cli) via the
    provider's ``change_effort`` — which pushes the level live to the running
    session (claude: session/set_config_option, kiro: /effort + cli.json
    overlay). Effort is Opus/Sonnet-only; on a non-capable model this is a
    persisted no-op (no live apply, no session reset).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    effort = body.get("reasoning_effort", "")
    valid_efforts = get_reasoning_effort_values()
    if not isinstance(effort, str) or effort not in valid_efforts:
        return web.json_response(
            {
                "error": f"reasoning_effort must be one of: {', '.join(sorted(valid_efforts - {''}))}"
            },
            status=400,
        )
    if slot.reasoning_effort == effort:
        return web.json_response({"ok": True, "reasoning_effort": effort})
    slot.reasoning_effort = effort
    logger.info("Slot %s reasoning_effort switched to %r", name, effort or "default")

    session_key = _history_key_for(name)
    provider = state.sessions.get_provider(session_key)
    _updated_live = False
    if isinstance(provider, AcpProvider) and provider.supports_effort():
        # Guard against racing the in-flight prompt read loop: a live
        # change_effort issues session/set_config_option and its response wait
        # would call stdout.readline() concurrently with the streaming
        # _prompt_loop → dropped/misrouted frame or a stuck turn. The override
        # is already persisted on the slot, so defer the live push to the next
        # turn instead of pushing now or resetting (effort is a cheap knob).
        if provider.has_active_turn():
            logger.info("Slot %s deferred live effort push: turn active", name)
            state.push_slots_update()
            return web.json_response({"ok": True, "reasoning_effort": effort, "deferred": True})
        # change_effort handles both backends and persists the per-model
        # override + overlay. "" clears the override → fall back to model
        # default (kiro: /effort with model default; claude: leave as-is).
        try:
            if effort:
                _updated_live = await provider.change_effort(effort)
            else:
                _updated_live = await provider.clear_effort()
        except Exception as exc:
            logger.warning(
                "change_effort(%s) failed for slot %s: %s: %s — falling back to reset",
                effort,
                name,
                type(exc).__name__,
                exc,
            )
    elif isinstance(provider, AcpProvider):
        # Model does not support effort — persist the slot value for when the
        # user switches to a capable model, but do not touch the live session.
        _updated_live = True
        logger.info("Slot %s effort persisted (model not effort-capable)", name)

    if not _updated_live:
        # No live session (or live update failed): reset so the next cold
        # start picks up the new effort via the provider factory/overlay.
        await _reset_slot_session(state, slot, session_key)
    state.push_slots_update()
    return web.json_response({"ok": True, "reasoning_effort": effort})


async def api_chat_slot_workspace(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/workspace — set workspace for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ws_name = body.get("workspace", "default")
    # Block workspace change after conversation has started
    if slot.total_messages > 0:
        return web.json_response(
            {
                "error": "Cannot change workspace after messages have been sent. Open a new session instead."
            },
            status=409,
        )
    slot.workspace = ws_name
    slot.project = default_project_dir(ws_name)
    logger.info("Slot %s workspace switched to %r, resetting session", name, ws_name)
    await _reset_slot_session(state, slot, _history_key_for(name))
    state.push_slots_update()
    return web.json_response({"ok": True, "workspace": ws_name})


async def api_chat_slot_project(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/project — set project directory for file search scoping."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    project = body.get("project", "")
    if not isinstance(project, str):
        return web.json_response({"error": "project must be a string"}, status=400)
    project = project.strip()
    if project:
        project = os.path.realpath(os.path.expanduser(project))
        if not os.path.isdir(project):
            return web.json_response({"error": "Not a directory"}, status=400)
        if is_sensitive_path(project):
            sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="chat_slot_project",
                outcome="denied",
                resources=f"slot={name} project={project}",
                error="sensitive path",
            )
            return web.json_response({"error": "Access denied"}, status=403)
    old_project = slot.project
    slot.project = project
    logger.info("Slot %s project set to %r", name, project)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="chat_slot_project",
        outcome="allowed",
        resources=f"slot={name} project={project}",
    )
    # Track recent projects
    if project:
        try:
            await asyncio.to_thread(_save_recent_project, project)
        except Exception:
            logger.warning("Failed to save recent project", exc_info=True)
    # Reset the session so the next message cold-starts with the new CWD and
    # picks up project-level .kiro/steering/**/*.md (mirrors api_chat_slot_agent).
    # Only on an actual change — avoids a needless cold start on a no-op set.
    #
    # Deferred via a flag because this endpoint is reachable over loopback HTTP
    # from inside the kiro-cli process group (the set_project MCP tool); an
    # inline reset would killpg() the caller. Consumed in chat_runner.
    if project != old_project:
        slot._pending_reset_history_key = _history_key_for(name)
    state.push_slots_update()
    return web.json_response({"ok": True, "project": project})


# Fields carried per follow-up item on the wire. Kept explicit so a future
# schema addition has to be added here deliberately rather than leaking
# whatever the model happened to send into the broadcast payload.
_FOLLOWUP_TEXT_FIELDS = ("title", "description", "prompt")


def _redact_followup_item(item: dict) -> dict:
    """Return a display-safe copy of one follow-up item.

    Every string is LLM-authored and renders in the dashboard DOM, so it goes
    through the same credential + exfiltration-URL redaction as chat content
    (mirrors the AskUserQuestion path in chat_runner). ``branch`` is omitted
    when absent so the frontend can fall back to deriving one from the title.
    """
    out: dict[str, str] = {}
    for key in _FOLLOWUP_TEXT_FIELDS:
        text = str(item.get(key) or "")
        text, _ = redact_exfiltration_urls(text)
        text, _ = redact_credentials(text)
        out[key] = text
    branch = item.get("branch")
    if isinstance(branch, str) and branch:
        # `branch` is LLM-authored too, and it travels further than the text
        # fields: into a git ref, a directory name, SEL records and logs. Run the
        # same redactors, and if either one CHANGES it, drop the field rather than
        # ship a mangled ref — the frontend then derives a branch from the title.
        scrubbed, _ = redact_exfiltration_urls(branch)
        scrubbed, _ = redact_credentials(scrubbed)
        if scrubbed == branch:
            out["branch"] = branch
    return out


def deny_non_dashboard_caller(request: web.Request, operation: str) -> web.Response | None:
    """403 unless this is the dashboard OWNER's own request, else None.

    Deny-by-default, matching ``api_chat_slots_model``'s reasoning: the auth
    middleware sets ``request["app"]`` on every authenticated path (``""`` for
    dashboard users, the app name for app tokens), so an ABSENT key means the
    middleware did not run and must refuse rather than fall through.

    An app claim of ``""`` is necessary but NOT sufficient. Both surfaces guarded
    here act on owner-scoped resources — the card renders in the owner's composer
    and the worktree allow-list is built from every slot's project — so identity
    is checked with ``is_owner_dashboard_request``, the same predicate the source
    provider mutations use: the caller must match the configured ``owner_id``, or
    be a signed local bootstrap subject when no owner is configured (the
    standalone-local case, where the browser's own token is minted for
    ``local-app``). A dashboard token issued for a different subject would
    otherwise mutate repositories it does not own.

    ONE exception, and it is the path every MCP call arrives on: a request that
    presented a valid ``X-Internal-Secret`` from loopback is granted by the
    middleware WITHOUT an app claim (there is no app identity to set), so it
    carries ``request["internal_auth"] is True`` instead. Refusing that would
    403 ``suggest_followup`` outright — the tool could never raise a card.
    """
    if request.get("internal_auth") is True:
        return None
    # Imported here, not at module scope: source_providers imports chat state
    # helpers, so a top-level import would close a cycle (same pattern as
    # api_chat_slots' owner-only check-status gate above).
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    if not is_owner_dashboard_request(request):
        try:
            sel().log_api_access(
                caller=str(request.get("user") or "anonymous"),
                operation=operation,
                outcome="denied",
                source="dashboard",
                error="not the dashboard owner",
            )
        except Exception:  # pragma: no cover - audit is best-effort
            logger.debug("SEL audit failed for %s denial", operation, exc_info=True)
        return web.json_response({"error": "forbidden"}, status=403)
    return None


async def api_chat_slot_followup(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/followup — show an agent-authored follow-up card.

    Backs the ``suggest_followup`` MCP tool. Reachable over loopback HTTP from
    inside the kiro-cli process group, so the payload is re-validated here
    against the same schema the MCP layer used: this endpoint is a trust
    boundary in its own right, not merely a relay.

    The card is ephemeral (broadcast-only, held in frontend state) and one card
    per slot: a second call replaces an unacted-on card rather than stacking.
    """
    state: DashboardState = request.app["state"]
    denied = deny_non_dashboard_caller(request, "chat_slot_followup")
    if denied is not None:
        return denied
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "invalid JSON"}, status=400)
    try:
        cleaned = validate_tool_args(body, SUGGEST_FOLLOWUP_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    items = [_redact_followup_item(item) for item in cleaned.get("items") or []]
    if not items:
        return web.json_response({"error": "items must not be empty"}, status=400)
    # The card is delivered by broadcast only — nothing is stored server-side —
    # so with no WS client attached the suggestions are dropped on the floor.
    # Report the number of sends that COMPLETED instead of an unconditional
    # success, so the MCP tool can tell the model to restate the follow-ups in
    # its reply text rather than being assured they were shown and steered into
    # silence.
    #
    # This send is AWAITED: a socket count is taken before any send runs, so an
    # owner window that disconnects in that window produced a failed send already
    # reported as delivered.
    #
    # OWNER clients only: an app token can open /api/ws, and an all-clients
    # broadcast would hand it another user's complete handoff prompts.
    try:
        clients = int(
            await state.deliver_ws_owners(
                "followup_card",
                {"slot": slot.key, "items": items, "ts": time.time()},
            )
        )
    except Exception:  # pragma: no cover - defensive: delivery must not 500
        logger.debug("Follow-up card delivery failed", exc_info=True)
        clients = 0
    logger.info(
        "Slot %s follow-up card broadcast with %d item(s) to %d client(s)",
        name,
        len(items),
        clients,
    )
    resp: dict[str, Any] = {"ok": True, "count": len(items), "delivered": clients}
    if not getattr(slot, "project", ""):
        # Parity with session_directive_apply._suggest_followup: the card's
        # worktree button renders disabled for an unscoped slot, and the caller
        # (the MCP relay, and through it the model) must hear that from the
        # delivery path — the tool description alone cannot know this slot.
        resp["warning"] = (
            "this session has no project directory, so the card's 'Start in "
            "new worktree' button is disabled; steer the user to 'Add to this "
            "session' or to scoping a project first"
        )
    return web.json_response(resp)


_MAX_RECENT_PROJECTS = 100


def _recent_projects_path() -> Path:
    return config_dir() / "recent_projects.json"


def _save_recent_project(path: str) -> None:
    """Prepend path to recent projects list (deduped, capped)."""

    fp = _recent_projects_path()
    fp.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
    except (json.JSONDecodeError, OSError):
        existing = []
    if not isinstance(existing, list):
        existing = []
    existing = [p for p in existing if p != path]
    existing.insert(0, path)
    existing = existing[:_MAX_RECENT_PROJECTS]
    fd, tmp = tempfile.mkstemp(dir=fp.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(json.dumps(existing))
        os.replace(tmp, fp)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


async def api_recent_projects(request: web.Request) -> web.Response:
    """GET /api/recent-projects — list recently used project directories."""

    def _read_recent_projects() -> list[str]:
        fp = _recent_projects_path()
        try:
            dirs = json.loads(fp.read_text(encoding="utf-8")) if fp.is_file() else []
        except Exception:
            dirs = []
        if not isinstance(dirs, list):
            dirs = []
        return [
            d for d in dirs if isinstance(d, str) and os.path.isdir(d) and not is_sensitive_path(d)
        ]

    dirs = await asyncio.to_thread(_read_recent_projects)
    sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="recent_projects",
        outcome="allowed",
        resources=f"count={len(dirs)}",
    )
    return web.json_response({"dirs": dirs})


def _resume_session_identity(state: DashboardState, history_key: str) -> str:
    """The session a transcript runs under, spelled as a slot spells its own.

    Counterpart to :func:`effective_session_key`, for the caller that holds a
    history key and no slot. A channel-born transcript's session is the
    channel's own, read from the session map because ``history._safe_key``
    folds every ``:`` to ``_`` irreversibly — ``discord_a_b_c`` cannot be
    unfolded by guessing, and a guess would name a session the channel never
    reads. An unmapped channel key falls back to the dashboard spelling, the
    same "leave it unbound" outcome the restore path takes.
    """
    if is_channel_session_key(history_key) and state.sessions:
        real_key = state.sessions.channel_key_for_stem(channel_slot_name(history_key))
        if isinstance(real_key, str) and is_channel_session_key(real_key):
            return real_key
    return _history_key_for(history_key)


async def api_chat_slot_resume(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/resume — load a history session into a slot."""
    state: DashboardState = request.app["state"]
    # Fold the requested name with the function that keys the slot table, so
    # every spelling of one slot resolves to that slot: a caller may hold a
    # filename stem, a session key (a notification deep link carries the
    # conversation's own ``slack:<ts>``), or a display-style name. A partial
    # fold leaves the lookup below missing an open tab and falls through to the
    # create path, which re-reads the transcript into the slot it should have
    # returned.
    name = _normalize_slot_key(request.match_info["slot"])
    if not state.conversation_log:
        return web.json_response({"error": "no conversation log"}, status=400)
    try:
        body = await request.json()
    except Exception:
        body = {}
    history_key = body.get("key", name)

    # If slot already exists (active session), just return it — no duplicate.
    # Check both by slot name AND by canonical session key to prevent two
    # slots sharing the same kiro-cli process.
    #
    # INVARIANT: both sides of this comparison derive identity through the same
    # rule. A slot answers with ``effective_session_key``, which for a
    # channel-born tab is the channel's own key — so the requested key resolves
    # the same way, via the session map. Two rules in play and a channel
    # transcript matches nothing here: it gets a second tab, so one conversation
    # shows as two sidebar rows backed by two kiro-cli processes.
    canonical = _resume_session_identity(state, history_key)
    existing = state._slots.get(name)
    if not existing:
        for slot in state._slots.values():
            if effective_session_key(slot) == canonical:
                existing = slot
                break
    if existing:
        # App ownership check (App Kit §5.2)
        request_app = request.get("app", "")
        if request_app:
            if not existing._app:
                sel().log_api_access(
                    caller=request_app,
                    operation="slot_resume",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={existing.key}",
                    error="app cannot access unscoped slots",
                )
                return web.json_response({"error": "not found"}, status=404)
            elif request_app != existing._app:
                sel().log_api_access(
                    caller=request_app,
                    operation="slot_resume",
                    outcome="denied",
                    source="app_isolation",
                    resources=f"slot={existing.key}",
                    error="app does not own this slot",
                )
                return web.json_response({"error": "not found"}, status=404)
        total = len(existing.messages)
        recent = existing.messages[-200:] if total > 200 else existing.messages
        prepared = _prepare_messages(recent, existing.running)
        return web.json_response(
            {
                "ok": True,
                "key": existing.key,
                "messages": prepared,
                "queue": [
                    {"id": q["id"], "content": _redact_for_display(q["content"])}
                    for q in existing._queue
                ],
                "total": total,
                "has_more": total > 200,
                "memory_mode": existing.memory_mode,
                # Return the slot's mode (and its `surface` alias) so the
                # frontend can render the recovered slot in the correct mode
                # (e.g. autopilot/"orchestrator") immediately, without waiting
                # for the racy SSE slots push to arrive (resumed autopilot
                # sessions came back as plain chat until SSE reconciled).
                "mode": existing.mode,
                "surface": existing.mode,
            }
        )

    slot = state.get_or_create_slot(
        name,
        app=request.get("app", ""),
        # Resuming an existing channel transcript from History is an adoption of
        # that conversation, so the tab is channel-origin even when the session
        # map can no longer name its session.
        channel_origin=is_channel_session_key(history_key),
    )
    title = body.get("title", "")
    if title:
        slot.title = title
        slot._titled = True
    else:
        sessions = state.conversation_log.list_sessions()
        for s in sessions:
            if s.get("key") == history_key:
                slot.title = s.get("title", history_key)
                slot._titled = True
                break
    # Restore original created_at from history metadata
    meta = state.conversation_log.get_metadata(history_key)
    if meta.get("created_at"):
        slot.created_at = meta["created_at"]
    if meta.get("agent"):
        slot.agent = meta["agent"]
    if meta.get("workspace"):
        slot.workspace = meta["workspace"]
    if meta.get("project"):
        slot.project = meta["project"]
    if meta.get("mode"):
        slot.mode = meta["mode"]
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
        # Re-engaging a hidden empty folder (Model B) un-hides it so it stays
        # visible until the user hides it again.
        _unhide_folder(state, meta["folder_id"])
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    if meta.get("color_theme"):
        slot.color_theme = meta["color_theme"]
        slot.theme_consent = meta.get("theme_consent") is True
        # Restore from history metadata: re-run the same fail-closed normalizer
        # so a tampered/legacy JSONL can't seed a malformed sha that later
        # crashes the compare.
        slot.theme_consent_sha = normalize_theme_consent_sha(meta.get("theme_consent_sha"))
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{name}")
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
    # Clear closed flag so session restores on next gateway restart
    if meta.get("closed"):
        # Offload to a worker thread: clear_closed takes the per-session
        # cross-process lock (so it can't race an append / rewrite and lose
        # data), and on the event loop that lock fails fast under contention —
        # the patient off-loop acquire path avoids both a loop-blocking disk
        # write and a dropped edit. Best-effort: resume proceeds regardless.
        try:
            await asyncio.to_thread(state.conversation_log.clear_closed, history_key)
        except Exception:
            logger.warning("Failed to clear closed flag for %s", history_key, exc_info=True)
    all_messages = state.conversation_log.read_messages_chained(history_key)
    disk_total = len(all_messages)
    max_resume = 500
    messages = all_messages[-max_resume:] if disk_total > max_resume else all_messages
    # Stable count of messages older than what we loaded into memory
    slot._disk_older_count = max(0, disk_total - len(messages))
    for m in messages:
        role = m.get("role", "assistant")
        cls = "msg msg-u" if role == "user" else "msg msg-a"
        content = m.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=m.get("ts", ""),
            meta=(
                _redact_meta_for_role(role, m["meta"]) if isinstance(m.get("meta"), dict) else None
            ),
        )
        # See the equivalent call in _rehydrate_slot_from_history: resume loads
        # the window that the next save re-serializes.
        carry_provenance(slot.messages[-1], m)
        _attach_variants(slot, m)
    slot.drain()
    slot._resumed_count = len(slot.messages)
    # Loaded window is the on-disk window region; older lines (in
    # _disk_older_count above) are the frozen prefix saves never rewrite,
    # so older on-disk turns are preserved.
    slot._disk_window_len = len(slot.messages)
    total = disk_total
    recent = slot.messages[-200:] if len(slot.messages) > 200 else slot.messages
    _sync_dashboard_slots(state)
    state.push_slots_update()
    return web.json_response(
        {
            "ok": True,
            "key": slot.key,
            "messages": _prepare_messages(recent, slot.running),
            "queue": [
                {"id": q["id"], "content": _redact_for_display(q["content"])} for q in slot._queue
            ],
            "total": total,
            "has_more": total > len(recent),
            "memory_mode": slot.memory_mode,
            "mode": slot.mode,
            "surface": slot.mode,
        }
    )


async def api_chat_mode(request: web.Request) -> web.Response:
    """POST /api/chat/mode — set global tool approval mode.

    Modes:
      - ``normal``: reset to interactive (ask for each tool)
      - ``trust``: auto-approve tools for active slot
      - ``yolo``: auto-approve all tools everywhere

    Unlike the per-tool approve endpoint, this doesn't require a
    pending approval — it preemptively sets the mode for future tools.
    """
    state: DashboardState = request.app["state"]
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = body.get("mode", "normal")
    slot_key = body.get("slot") or None

    if mode == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:yolo",
                outcome="enabled",
                resources=",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for YOLO mode activation", exc_info=True)
    elif mode == "trust_reads":
        safety_override().deactivate("dashboard")
        if slot_key and slot_key in state._slots:
            state._slots[slot_key]._trust = False
            state._slots[slot_key]._trust_reads = True
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "")
        else:
            for slot in state._slots.values():
                slot._trust = False
                slot._trust_reads = True
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:trust_reads",
                outcome="enabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for trust_reads mode activation", exc_info=True)
    elif mode == "trust":
        safety_override().deactivate("dashboard")
        mgr = getattr(state, "channel_manager", None)
        if slot_key is not None:
            if slot_key not in state._slots:
                return web.json_response({"ok": False, "error": "unknown slot"}, status=400)
            state._slots[slot_key]._trust = True
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "auto")
            linked_ch = getattr(state._slots[slot_key], "_slack_channel", None)
            if mgr and linked_ch and linked_ch in mgr._channels:
                mgr._channels[linked_ch].trusted = True
                mgr._channels[linked_ch]._save()
        else:
            for slot in state._slots.values():
                slot._trust = True
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "auto")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = True
                    ch._save()
        _trusted_chs = [cid for cid, ch in mgr._channels.items() if ch.trusted] if mgr else []
        try:
            _res = slot_key or ",".join(s.key for s in state._slots.values())
            if _trusted_chs:
                _res += "|channels:" + ",".join(_trusted_chs)
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:trust",
                outcome="enabled",
                resources=_res,
            )
        except Exception:
            logger.warning("SEL audit failed for trust mode activation", exc_info=True)
    else:  # normal
        safety_override().deactivate("dashboard")
        mgr = getattr(state, "channel_manager", None)
        if slot_key is not None:
            if slot_key not in state._slots:
                return web.json_response({"ok": False, "error": "unknown slot"}, status=400)
            state._slots[slot_key]._trust = False
            state._slots[slot_key]._trust_reads = False
            state.sessions.set_approval_policy(f"dashboard:{slot_key}", "")
            linked_ch = getattr(state._slots[slot_key], "_slack_channel", None)
            if mgr and linked_ch and linked_ch in mgr._channels:
                mgr._channels[linked_ch].trusted = False
                mgr._channels[linked_ch]._save()
        else:
            for slot in state._slots.values():
                slot._trust = False
                slot._trust_reads = False
                state.sessions.set_approval_policy(f"dashboard:{slot.key}", "")
            if mgr:
                for ch in mgr._channels.values():
                    ch.trusted = False
                    ch._save()
        try:
            sel().log_api_access(
                caller="dashboard:mode",
                operation="mode_change:normal",
                outcome="disabled",
                resources=slot_key or ",".join(s.key for s in state._slots.values()),
            )
        except Exception:
            logger.warning("SEL audit failed for normal mode activation", exc_info=True)

    # If any slot has a pending approval and mode is trust/yolo, auto-approve it
    if mode in ("trust", "yolo"):
        for slot in state._slots.values():
            for aid, fut in list(slot._approval_futures.items()):
                if not fut.done():
                    fut.set_result("approved")
                    # Persist resolved state into the permission message. The
                    # periodic flush skips non-dirty slots, so the mark must
                    # flag the slot or the write can be lost on restart.
                    if _mark_permission_resolved(slot.messages, aid, mode):
                        slot._dirty = True
                    state.broadcast_ws("approval_resolved", {"id": aid, "approved": True})
                    try:
                        sel().log_api_access(
                            caller=f"dashboard:{slot.key}",
                            operation=f"tool_approval:bulk_{mode}",
                            outcome="approved",
                            resources=aid,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
        # Also auto-approve all pending background approvals (cron/subagent/taskrunner)
        for aid in list(state._approval_futures):
            fut = state._approval_futures[aid]
            if not fut.done():
                state.resolve_approval(aid, True)
                try:
                    sel().log_api_access(
                        caller="dashboard:background",
                        operation=f"tool_approval:bulk_{mode}",
                        outcome="approved",
                        resources=aid,
                    )
                except Exception:
                    logger.warning("SEL audit failed for bulk approval %s", aid, exc_info=True)
        # Auto-approve pending channel approvals
        mgr = getattr(state, "channel_manager", None)
        if mgr:
            for ch in mgr._channels.values():
                for agent in ch.members.values():
                    fut = agent._approval_future
                    if fut and not fut.done():
                        fut.set_result("approved")
                        try:
                            sel().log_api_access(
                                caller=f"channel:{ch.id}:{agent.agent_name}",
                                operation=f"tool_approval:bulk_{mode}",
                                outcome="approved",
                                resources=getattr(fut, "_approval_id", "unknown"),
                            )
                        except Exception:
                            logger.warning(
                                "SEL audit failed for channel bulk approval", exc_info=True
                            )

    # Propagate trust/yolo to session approval policies so subagents inherit.
    for slot in state._slots.values():
        policy = "auto" if slot._trust or safety_override().is_active() else ""
        state.sessions.set_approval_policy(f"dashboard:{slot.key}", policy)

    state.push_slots_update()
    return web.json_response({"ok": True, "mode": mode})


def _get_pattern_from_pending(slot: _ChatSlot, request_id: str, field: str) -> str:
    """Extract a pattern field from the permission message matching request_id."""
    if not request_id:
        return ""
    for msg in reversed(slot.messages):
        if msg.get("role") == "permission" and msg.get("cls"):
            try:
                meta = json.loads(msg["cls"])
                if not isinstance(meta, dict):
                    continue
                if meta.get("request_id") == request_id:
                    return meta.get(field, "")
            except (json.JSONDecodeError, TypeError):
                continue
    return ""


async def api_chat_slot_approve(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/approve — resolve a pending tool approval."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    action = body.get("action", "rejected")
    original_action = action
    request_id = body.get("request_id", "")
    # Locate the slot that OWNS the pending approval future. It is usually the
    # addressed slot, but under session-sharing or a rehydrated/replaced slot the
    # future can live on a different slot object under a different key. All
    # slot-scoped side-effects (trust flags, trusted patterns, approval policy)
    # and the resolved outcome MUST land on the OWNER slot — the one whose
    # session loop consumes the future and gates subsequent tools — or the trust
    # opt-in silently fails on the running session while the UI reports success.
    owner = slot
    if request_id:
        fut = slot._approval_futures.get(request_id)
        if not fut or fut.done():
            # The future can live on a DIFFERENT slot object only under
            # session-sharing / rehydration — i.e. a slot that resolves to the
            # SAME session identity as the addressed one. ACP request_ids are
            # connection-scoped and can collide across unrelated sessions, so a
            # bare id-match scan could approve (and, for trust, auto-approve) an
            # unrelated slot's pending tool. Guard the scan on session identity:
            # only a candidate whose effective session key equals the addressed
            # slot's is a legitimate owner.
            want_session = effective_session_key(slot)
            for s in state._slots.values():
                cand = s._approval_futures.get(request_id)
                if not cand or cand.done():
                    continue
                cand_session = s.linked_session_key or _history_key_for(s.key)
                if cand_session != want_session:
                    continue
                owner, fut = s, cand
                break
    else:
        pending = [(k, f) for k, f in slot._approval_futures.items() if not f.done()]
        if len(pending) == 1:
            request_id, fut = pending[0]
        else:
            fut = None
    # Trust: auto-approve remaining tools for this slot. The approval policy MUST
    # be keyed by the OWNER's EFFECTIVE session key — a linked cron/workflow slot
    # runs under ``linked_session_key``, not ``dashboard:{key}``, so writing the
    # raw slot key would leave the running session on its old policy and the trust
    # decision would silently not take (mirrors the _run_chat session-key derivation).
    if action == "trust":
        owner._trust = True
        owner_session = owner.linked_session_key or _history_key_for(owner.key)
        state.sessions.set_approval_policy(owner_session, "auto")
        action = "approved"
    # Trust-reads: auto-approve read-only bash commands for this slot
    # Defer setting _trust_reads until after the approval future is consumed
    # to prevent the frontend from seeing trust_reads=true while still pending.
    elif action == "trust_reads":
        action = "approved_trust_reads"
    # Trust-command: trust this exact command/tool (session-scoped)
    elif action == "trust_command":
        pattern = body.get("pattern", "")
        if not pattern:
            pattern = _get_pattern_from_pending(owner, request_id, "full_command")
        if pattern:
            owner._trusted_patterns.add(pattern)
        action = "approved"
    # Trust-base: trust the base command glob e.g. "ls *" (session-scoped)
    # For multi-command titles ("cat,wc"), adds patterns for each binary.
    elif action == "trust_base":
        pattern = body.get("pattern", "")
        if not pattern:
            base = _get_pattern_from_pending(owner, request_id, "base_command")
            pattern = ",".join(f"{b} *" for b in base.split(",") if b) if base else ""
        for p in pattern.split(","):
            p = p.strip()
            if p:
                owner._trusted_patterns.add(p)
                # Also trust the bare command (no args) since "ls *" doesn't match "ls"
                if p.endswith(" *"):
                    bare = p[:-2]
                    if bare:
                        owner._trusted_patterns.add(bare)
        action = "approved"
    # YOLO: auto-approve all tools globally (all slots)
    elif action == "yolo":
        result = await asyncio.to_thread(safety_override().activate, "dashboard")
        if not result.active:
            return web.json_response(
                {"ok": False, "error": "safety override activation refused"},
                status=503,
            )
        for s in state._slots.values():
            # Same effective-session-key rule as the single-slot trust above: a
            # linked cron/workflow slot runs under its linked_session_key.
            s_session = s.linked_session_key or _history_key_for(s.key)
            state.sessions.set_approval_policy(s_session, "auto")
        action = "approved"
    resolved = action if action in ("approved", "approved_trust_reads") else "rejected"
    if not fut or fut.done():
        # Distinguish ambiguous (multiple pending) from truly empty
        if not request_id and slot._approval_futures:
            pending_ids = [k for k, f in slot._approval_futures.items() if not f.done()]
            if len(pending_ids) > 1:
                return web.json_response(
                    {
                        "error": "multiple approvals pending, specify request_id",
                        "pending": pending_ids,
                    },
                    status=400,
                )
        # No slot owns this future — fall back to the STATE-LEVEL-ONLY resolver so
        # a background approval (cron/subagent/gateway) is still dismissed instead
        # of 404-ing. MUST be resolve_state_approval, NOT resolve_approval: the
        # latter re-scans every slot's futures by bare id-match, which would let a
        # request-id collision resolve an unrelated slot's pending tool — exactly
        # the cross-slot approval the session-identity owner scan above prevents.
        # State-level futures have no per-slot trust semantics, so the bool
        # coercion loses nothing.
        if request_id and state.resolve_state_approval(request_id, resolved != "rejected"):
            return web.json_response({"ok": True})
        return web.json_response({"error": "no pending approval"}, status=404)
    fut.set_result(resolved)
    # Persist resolved state into the permission message so it survives tab
    # switches — on the owner slot, whose messages hold the permission card.
    # Flagging the slot dirty is required for it to survive a RESTART too: the
    # periodic flush skips non-dirty slots.
    if request_id:
        if _mark_permission_resolved(
            owner.messages,
            request_id,
            original_action if original_action in ("trust", "trust_reads") else resolved,
        ):
            owner._dirty = True
    # Broadcast first to ensure frontend is unblocked
    if request_id:
        state.broadcast_ws(
            "approval_resolved", {"id": request_id, "approved": resolved != "rejected"}
        )
    state.push_slots_update()
    # SEL audit (best-effort — must not block the UI-unblocking path above)
    try:
        sel().log_api_access(
            caller=f"dashboard:{name}",
            operation=f"tool_approval:{original_action}",
            outcome=resolved,
            resources=request_id,
        )
    except Exception:
        logger.warning("SEL audit failed for approval %s", request_id, exc_info=True)
    return web.json_response({"ok": True})


MAX_COLOR_INDEX = 20


async def api_chat_slot_color(request: web.Request) -> web.Response:
    """PATCH /api/chat/slots/{slot}/color — set session color."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    ci = body.get("color_index")
    if ci is not None and (
        isinstance(ci, bool) or not isinstance(ci, int) or ci < 0 or ci > MAX_COLOR_INDEX
    ):
        return web.json_response(
            {"error": f"color_index must be a non-negative integer <= {MAX_COLOR_INDEX} or null"},
            status=400,
        )
    slot.color_index = ci
    slot._dirty = True
    state.push_slots_update()
    return web.json_response({"ok": True, "color_index": ci})


_MAX_CONTEXT_PER_SOURCE = 10


async def api_chat_slot_context(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/context — inject silent background context.

    Adds a ContextEntry to the slot's ``_pending_context`` queue.
    The content is consumed on the next user-initiated message via
    ``ctx_builder.build_message()`` and prepended to the LLM prompt.

    No LLM turn is triggered, no WS event is broadcast, and no visible
    message is appended to the slot's chat history.

    Body::

        {
            "content": "...",
            "source": "watch-check",   // optional
            "ephemeral": true,         // optional, default true
            "maxAge": 300              // optional, seconds
        }
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "slot not found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens.
    # Apps can only access slots they own. Dashboard users (empty request_app)
    # can access everything.
    request_app = request.get("app", "")
    if request_app:
        if not slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="context_inject",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app cannot access unscoped slots",
            )
            return web.json_response({"error": "not found"}, status=404)
        elif request_app != slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="context_inject",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="app does not own this slot",
            )
            return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)

    content = body.get("content", "")
    if not content:
        return web.json_response({"error": "content is required"}, status=400)

    # Content size limit (40,000 chars — same as message limit)
    max_context_content = 40000
    if len(content) > max_context_content:
        return web.json_response(
            {"error": f"content exceeds {max_context_content} char limit"}, status=400
        )

    entry: dict[str, object] = {
        "content": content,
        "source": body.get("source", ""),
        "ephemeral": body.get("ephemeral", True),
        "injectedAt": time.time(),
    }
    max_age = body.get("maxAge")
    if max_age is not None:
        entry["maxAge"] = max_age

    # Per-source cap: prevent one app from evicting all others' context
    source = body.get("source", "")
    if source:
        source_count = sum(1 for e in slot._pending_context if e.get("source") == source)
        if source_count >= _MAX_CONTEXT_PER_SOURCE:
            return web.json_response(
                {"error": f"source {source!r} has {_MAX_CONTEXT_PER_SOURCE} pending entries"},
                status=429,
            )

    # FIFO eviction: cap pending queue at the shared ceiling
    while len(slot._pending_context) >= _MAX_PENDING_CONTEXT:
        slot._pending_context.pop(0)

    slot._pending_context.append(entry)  # type: ignore[arg-type]

    # SEL audit logging
    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="context_inject",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    return web.json_response({"ok": True, "pending": len(slot._pending_context)})
