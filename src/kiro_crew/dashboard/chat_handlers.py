"""HTTP API handlers for dashboard chat endpoints."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import re
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from aiohttp.client_exceptions import ClientConnectionResetError

from kiro_crew import model_registry
from kiro_crew.acp.client import AcpModelUnavailable
from kiro_crew.agent_discovery import cached_project_agent_names, warm_project_agent_names
from kiro_crew.config.loader import (
    KiroCrewConfig,
    _workspace_name_for_dir,
    config_dir,
    default_project_dir,
    resolve_agent_bindings,
)
from kiro_crew.dashboard.channel_slots import channel_slot_name, note_slot_closed
from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag
from kiro_crew.dashboard.chat_folders import _unhide_folder
from kiro_crew.dashboard.chat_orchestrator import _stage_loop
from kiro_crew.dashboard.chat_persistence import (
    _FLUSH_SNAPSHOT_RETRIES,
    _TRANSIENT_ROLES,
    COLOR_HEX_RE,
    _attach_variants,
    _rehydrate_slot_title,
    get_reasoning_effort_values,
    save_slot_off_loop,
)
from kiro_crew.dashboard.chat_runner import (
    _context_usage_payload,
    _run_chat,
    _start_next_queued_turn,
    context_entry_expired,
    schedule_eager_spawn,
)
from kiro_crew.dashboard.chat_summary import generate_session_summary
from kiro_crew.dashboard.chat_title import _maybe_auto_title
from kiro_crew.dashboard.chat_utils import (
    _MANUAL_CONTINUE_MSG,
    _MANUAL_RESUME_MSG,
    SYNTHETIC_RECOVERY_KIND,
    _build_stream_chunk,
    _collapse_wire_rows,
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
from kiro_crew.dashboard.state import (
    DashboardState,
    _ChatSlot,
    _mark_permission_resolved,
    _normalize_slot_key,
    is_stop_event_row,
    is_turn_interrupted,
    parse_cls_meta,
    request_slot_origin,
)
from kiro_crew.dashboard.system_notices import SESSION_RELOAD_KIND, is_system_notice
from kiro_crew.dashboard.turn_dispatch import spawn_guarded_turn
from kiro_crew.history import carry_provenance, is_incognito_transcript
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
from kiro_crew.session_summary import count_user_turns_in_records
from kiro_crew.validation import (
    _AGENT_NAME_RE,
    ARTIFACT_SLUG_RE,
    SUGGEST_FOLLOWUP_SCHEMA,
    ValidationError,
    normalize_theme_consent_sha,
    validate_tool_args,
)

if TYPE_CHECKING:  # circular at runtime: autonudge -> dashboard.chat -> chat_handlers
    from kiro_crew.autonudge import NudgeLoop

logger = logging.getLogger(__name__)

# Feed notice appended by api_chat_slot_reload. A constant, not LLM-derived
# text, so it needs no redaction pass.
_SESSION_RELOAD_NOTICE = (
    "Reloading session: relaunching the agent process with a freshly loaded "
    "agent spec, environment, and MCP servers. The conversation is preserved."
)

# Approval modes that grant auto-approval to the SLOT they name, as opposed to
# the process-global YOLO grant. A tuple, not a set: membership is tested against
# a request-supplied value, and tuple `in` compares by equality rather than
# hashing, so a non-string body value answers False instead of raising.
_SLOT_SCOPED_TRUST_MODES = ("trust", "trust_reads")


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

    # Honor mode from the body when auto-creating a slot, mirroring memory_mode
    # above: an app whose worker slot lives only in gateway memory (e.g. Design
    # Critique) repeats mode on send(), so a slot recreated here after a
    # gateway restart keeps its non-"" surface and stays out of the chat
    # sidebar's surface allowlist. Only creation-allowlisted values pass;
    # anything else is dropped so get_or_create_slot uses its default. Unlike
    # memory_mode there is no mismatch error: get_or_create_slot ignores mode
    # for an already-existing slot.
    requested_mode = body.get("mode")
    if not isinstance(requested_mode, str) or requested_mode not in _CREATABLE_MODES:
        requested_mode = ""
    if requested_mode == "crew" and slot_name:
        # Same boundary as api_chat_slot_create: a caller-supplied name whose
        # normalized key cannot host a crew store must not become a crew slot
        # (its first crew message would 500). Dropped rather than refused —
        # auto-create is a convenience path, not the crew entry point.
        from kiro_crew.crew_chat import is_crew_capable_slot_key

        if not is_crew_capable_slot_key(_normalize_slot_key(slot_name)):
            requested_mode = ""

    try:
        slot = state.get_or_create_slot(
            slot_name,
            app=request.get("app", ""),
            origin=request_slot_origin(request.get("app", "")),
            mode=requested_mode,
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
    else:
        # FIX 1: a dashboard user (no app token) typed into this slot, so a
        # human demonstrably has it open. That restores the full 2h approval
        # window even on an app-owned tab — the deny-fast window is for slots
        # nobody is watching. Only a caller with an EMPTY request_app reaches
        # here, so an app cannot forge attendance for its own worker.
        slot._human_seen = True

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

    if not message:
        # One guard, above every dispatch branch. An empty wire text reaches
        # here only from programmatic callers (app tokens, curl, integrations)
        # — the dashboard composer always inlines staged files into the
        # message text. Such a send may still carry attachments in `meta`:
        # nothing downstream queues or broadcasts it, so any success receipt
        # would report work that was silently dropped. Refusing here keeps
        # every branch below (steer/queue, crew, subagent-hold, new turn)
        # unable to bypass the check — the guard used to sit below the busy
        # branch, which is exactly how the false `queued: true` receipt
        # happened. `message_required` is the backend-owned code already used
        # for this refusal (handlers/messaging.py).
        return web.json_response(
            {"error": "message is required", "code": "message_required"}, status=400
        )

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
        # processes the queue in its finally block. The message is non-empty
        # here (hoisted guard above the busy branch), so `queued: true`
        # always reports a real enqueue.
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

    # ── Crew Mode dispatch (RFC orchestrator-chat-sessions) ─────────
    # MUST precede the hold-users gate below: crew topics ARE background
    # sub-agents, so the hold would swallow every message the moment one
    # topic runs — killing the mode's whole point (parallel ingress). Crew
    # messages are durable queue entries, not turns; the CrewOrchestrator
    # acks instantly and routes them to topic sub-sessions.
    if getattr(slot, "mode", "") == "crew":
        _crew = getattr(state, "crew", None)
        if _crew is None:
            return web.json_response(
                {"error": "crew mode unavailable", "code": "crew_unavailable"}, status=503
            )
        # Do NOT append the user message here. `ingest` shows it only after the
        # queue entry is durable: a visible message with no queue entry (process
        # exit during a cold-store build) is a request that can never resume.
        _refusal = await _crew.ingest(
            slot, message,
            user_meta=_redact_meta(user_meta) if user_meta else None,
        )
        if _refusal:
            # Crew declined this ingress (app-owned session). Answering 200 told
            # a programmatic caller its message was accepted for work that will
            # never run — the transcript note it posts is not visible to an API
            # caller, so the refusal has to reach the status line too.
            return web.json_response(
                {"error": "crew mode is not available for this session",
                 "code": _refusal},
                status=409,
            )
        return web.json_response({"ok": True, "slot": slot.key, "crew": True})

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

    # No per-message browse marker: browsing is a capability, not a per-turn
    # gate. The agent drives a browser by running `playwright-cli` shell
    # commands, so the capability is simply whether that binary is on PATH. The
    # agent itself decides whether to operate a browser or read with web_fetch
    # (the system prompt and the kirocrew-commands / web-browse skills tell it
    # how), so the backend injects nothing here.
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

    # Auto-tag: derive a tag from the session's project directory (deterministic,
    # no LLM). Fire-and-forget, same pattern as auto-title.
    if not getattr(slot, "_auto_tagged", False):
        _at = asyncio.create_task(maybe_auto_tag(state, slot))
        state._background_tasks.add(_at)
        _at.add_done_callback(state._background_tasks.discard)

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

    # FIX 2: an unattended app-owned turn runs under the background concurrency
    # cap; run_background_turn passes an attended slot straight through, so the
    # interactive path is unchanged (no semaphore is even created).
    task = spawn_guarded_turn(
        state, slot, state.run_background_turn(slot, _run_chat(state, slot, message))
    )
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

    # Declare this reader as the owner of `slot._pending` for as long as it is
    # draining. A turn-end chunk release must not run while an SSE reader still
    # has undelivered tokens queued, and `_has_reader` alone cannot carry that:
    # the `done` branch below clears it before this scope ends.
    with slot.pending_consumer():
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


async def api_chat_slot_source_links(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/source-links — every PR/issue link, unbudgeted.

    The slots payload caps chips per kind, so the sidebar's "+N" overflow chip
    has nothing on the client to expand into. This is the lazy read behind that
    expand, kept off the slots broadcast on purpose: widening the budget would
    put up to ``_MAX_SOURCE_LINKS_PER_SLOT`` links per slot on the wire for every
    row nobody expanded, on every push.
    """
    # circular import: source_providers imports chat state helpers, so a
    # top-level import would close a cycle (same pattern as api_chat_slots'
    # owner-only check-status gate above).
    from kiro_crew.dashboard.handlers.source_providers import (
        ensure_gitlab_hosts_loaded,
        is_owner_dashboard_request,
    )

    state: DashboardState = request.app["state"]
    slot = state._slots.get(request.match_info["slot"])
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2): deny-by-default for app tokens. An app
    # token scoped to /api/chat/slots/* would otherwise name any slot the list
    # endpoint reveals and read every pull request and issue URL a dashboard or
    # foreign-app session ever mentioned. Same indistinguishable 404 as the send
    # path -- SAME error code too, so the response cannot be used to probe which
    # foreign slots exist.
    request_app = request.get("app", "")
    if request_app and request_app != slot._app:
        sel().log_api_access(
            caller=request_app,
            operation="chat_source_links",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key}",
            error=(
                "app cannot access unscoped slots"
                if not slot._app
                else "app does not own this slot"
            ),
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    if request_app:
        # The ALLOW is a permission decision too, and an audit trail that records
        # only refusals cannot answer which app actually read a slot's links.
        # Dashboard callers are deliberately not logged here: they are the owner,
        # and every sidebar expand would otherwise write an event.
        sel().log_api_access(
            caller=request_app,
            operation="chat_source_links",
            outcome="allowed",
            source="app_isolation",
            resources=f"slot={slot.key}",
        )

    # Same warm-up as GET /api/chat/slots: link extraction is synchronous and
    # cannot load the self-managed GitLab allowlist itself, so a cold expand
    # would drop every self-hosted MR link from the revealed set.
    try:
        await ensure_gitlab_hosts_loaded()
    except Exception:
        logger.debug(
            "GitLab allowlist warm-up failed; expanded chips may lag one round", exc_info=True
        )

    # Cached status only, owner-gated exactly like the list endpoint. No
    # schedule_check_refresh here: that pushes a `slots` update, which by
    # definition cannot carry links outside the budget, so the provider work
    # would produce a result this response can never show.
    return web.json_response(
        slot.source_links_payload(include_check_status=is_owner_dashboard_request(request))
    )


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


def _context_reading(pct: Any, used: Any, window: Any, *, stale: bool) -> dict[str, Any]:
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
            (provider.context_used_tokens() if hasattr(provider, "context_used_tokens") else 0),
            (provider.context_window_tokens() if hasattr(provider, "context_window_tokens") else 0),
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


async def api_chat_slot_summary(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot}/summary — intent summary for the panel.

    Read-only: it never triggers generation. Summaries are produced at turn end
    by the background pass, deliberately, so that opening the panel cannot spend
    tokens and repeated opening cannot turn into a refresh loop.

    Responses:
      - 200 with ``{enabled, generated_at, stale, intents, constraints, ...}``
      - 200 with ``intents: []`` and ``enabled: false`` when the feature is off,
        so the panel can render an explanatory empty state rather than an error
      - 404 ``slot_not_found`` for an unknown slot, or for a slot an app caller
        does not own (App Kit §5.2 isolation; 404 not 403 for anti-enumeration)
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # App ownership check (App Kit §5.2), mirroring api_chat_slot_delete: a
    # summary is derived conversation content, so a slot merely existing must
    # not make it readable. Dashboard users carry an explicit empty request_app
    # and are unaffected; an app token may only read summaries for slots it
    # created, never for unscoped slots.
    request_app = request.get("app", "")
    if request_app and (not slot._app or slot._app != request_app):
        sel().log_api_access(
            caller=request_app,
            operation="slot_summary_read",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own this slot",
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    enabled = bool(cfg.session_summary.enabled)

    payload: dict | None = None
    stale = False
    log = state.conversation_log
    # Gate the cache read on the flag as well: turning the feature off has to
    # stop serving summaries, not just stop producing them, or a sidecar written
    # during an earlier opt-in keeps being returned after opt-out.
    if enabled and log is not None:
        history_key = slot_history_key(slot)
        payload, stale = await asyncio.to_thread(log.read_intent_summary, history_key)

    body: dict = {
        "enabled": enabled,
        "stale": stale,
        "intents": (payload or {}).get("intents", []),
        "constraints": (payload or {}).get("constraints", []),
        "generated_at": (payload or {}).get("generated_at"),
        "user_turns": (payload or {}).get("user_turns"),
        "last_activity": (payload or {}).get("last_activity"),
        "generate_state": _generate_state(cfg, slot),
    }
    return web.json_response(body)


def _generate_state(cfg: KiroCrewConfig, slot: Any) -> str:
    """Which on-demand affordance the panel should offer for *slot*.

    Three values, because the panel has three honest things to say and a bool
    could only carry two: ``ready`` (offer the button), ``too_few_turns`` (say so
    plainly and offer nothing -- a click could only fail), and ``unavailable``
    (the feature is off, a pass is already running, or the session is incognito
    and must never leave a durable artifact). Collapsing the last two into
    "not enough messages" would print a reason that is simply untrue for an
    incognito session.

    The turn count is an ESTIMATE from the slot's IN-MEMORY messages, not a
    transcript read: this runs on every panel mount and tab switch, and reading a
    thousand-message session from disk to answer a yes/no question is waste. A
    restored slot keeps only a window of its transcript, and the window is NOT a
    safe proxy for the whole session -- a tail made mostly of assistant replies
    and injected automation messages can hold fewer than the minimum genuine user
    turns while the file holds dozens. So `too_few_turns` is only claimed when the
    window IS the whole session (`_disk_older_count == 0`); a truncated window
    reports `ready` and lets the POST's disk-backed count decide.

    The authoritative gate lives in the generator and reads disk; if this estimate
    is wrong the POST refuses and says why, so the cost is a refused click, never
    a wasted call.

    A turn in flight is deliberately NOT one of these values, even though the
    generator refuses one. This field is only refreshed when a summary is written,
    so a state that begins and ends mid-turn would arrive stale and stay stale: a
    turn that ends without producing a summary (stopped, or gated by cadence)
    pushes no event, and the panel would sit on a dead verdict until it remounted.
    The panel already holds a live per-slot turn signal, so it owns that
    presentation and this field stays limited to what only the server knows.
    """
    if not cfg.session_summary.enabled:
        return "unavailable"
    if getattr(slot, "_summary_in_flight", False):
        return "unavailable"
    if is_incognito_transcript(getattr(slot, "memory_mode", "")):
        return "unavailable"
    turns = count_user_turns_in_records(getattr(slot, "messages", []) or [])
    if turns < cfg.session_summary.min_user_turns and not getattr(
        slot, "_disk_older_count", 0
    ):
        return "too_few_turns"
    return "ready"


async def api_chat_slot_summary_generate(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/summary — summarize this session on request.

    The companion to the read-only GET. Generation stays off the read path so
    that opening the panel can never spend tokens; this route exists because the
    turn-end trigger alone leaves every session that predates the feature -- or
    that simply has not been touched since it was switched on -- permanently
    empty, with nothing a person can do about it from the panel.

    Explicit consent is the whole justification for the spend, so there is no
    batch form: one request summarizes one session.

    Responses:
      - 200 with the same body as the GET, once a summary exists
      - 409 ``summary_disabled`` / ``summary_in_flight`` / ``summary_unavailable``
        when no summary could be produced, so the panel can say which
      - 404 ``slot_not_found`` for an unknown slot, or one an app does not own
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    # Same App Kit §5.2 isolation as the GET: generating is strictly more
    # privileged than reading, so it can never be the laxer of the two.
    request_app = request.get("app", "")
    if request_app and (not slot._app or slot._app != request_app):
        sel().log_api_access(
            caller=request_app,
            operation="slot_summary_generate",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own this slot",
        )
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)

    cfg = await asyncio.to_thread(KiroCrewConfig.load)
    if not cfg.session_summary.enabled:
        return web.json_response(
            {"error": "session summaries are switched off", "code": "summary_disabled"},
            status=409,
        )
    log = state.conversation_log
    if log is None:
        return web.json_response(
            {"error": "no conversation log", "code": "summary_unavailable"},
            status=409,
        )
    # Reported separately from the generic failure because it is the one the
    # panel can explain as "already working" rather than "could not".
    if getattr(slot, "_summary_in_flight", False):
        return web.json_response(
            {"error": "a summary is already being written", "code": "summary_in_flight"},
            status=409,
        )
    # Likewise distinct: a turn in flight is a wait-and-retry, not a refusal. The
    # generator would decline anyway; saying so here keeps the panel from
    # reporting a transient state as a failure.
    if getattr(slot, "running", False):
        return web.json_response(
            {"error": "this session has a turn in progress", "code": "summary_turn_running"},
            status=409,
        )

    await generate_session_summary(state, slot, cfg=cfg, force=True)

    # Read back rather than trusting the return value: a forced pass returns
    # False both when it produced nothing AND when the cached summary was
    # already current, and those are opposite outcomes for the panel.
    history_key = slot_history_key(slot)
    payload, stale = await asyncio.to_thread(log.read_intent_summary, history_key)
    if payload is None:
        return web.json_response(
            {"error": "could not summarize this session", "code": "summary_unavailable"},
            status=409,
        )
    return web.json_response(
        {
            "enabled": True,
            "stale": stale,
            "intents": payload.get("intents", []),
            "constraints": payload.get("constraints", []),
            "generated_at": payload.get("generated_at"),
            "user_turns": payload.get("user_turns"),
            "last_activity": payload.get("last_activity"),
            "generate_state": _generate_state(cfg, slot),
        }
    )


def _load_redacted(body: str) -> str:
    """Apply the transcript redaction pair, in the one order the repo uses."""
    redacted, _ = redact_exfiltration_urls(body)
    redacted, _ = redact_credentials(redacted)
    return redacted


def _same_persisted_body(
    disk_body: str, window_body: str, role: str, disk_ts: str = "", window_ts: str = ""
) -> bool:
    """True when *disk_body* is the persisted form of *window_body*.

    A persisted row can differ from its window copy by exactly the redaction
    transform, and EITHER side can be the redacted one, which is why the compare
    applies it symmetrically. A restore redacts on load while keeping ``ts``
    verbatim (``chat_persistence.py:708-709``), so the window holds the redacted
    text; a save redacts every non-user role on the way out
    (``chat_persistence.py:1282-1284``) while the window keeps it verbatim
    (``state.py:2107``), so in a session that was never restored the DISK holds the
    redacted text instead. Redacting one side only cannot converge on that second
    pair — redacting an already-redacted body just reproduces it — so the row reads
    as un-flushed and the persisted suffix is appended twice.

    Applying the transform is also what separates a persisted row from a foreign
    row that merely shares a ``ts``: on a coarse clock two writers flooring off the
    same previous row both emit ``previous + 1µs`` (``history.py:1179-1219``), so
    matching on ``ts`` alone treats an unrelated row as the window's own and drops
    an un-flushed message from a response the client uses as a replacement.

    Two bodies can redact to the same text while being different messages, so the
    redaction-equivalent branch ALSO requires the stamps to match. That costs the
    legitimate case nothing: this branch only ever fires for a row and its own
    persisted copy, which differ by the transform precisely because one side was
    redacted, and both the save and the load copy ``ts`` verbatim
    (``chat_persistence.py:1288`` and ``:714``). A foreign row whose credential
    merely redacts to the same text carries its own writer's stamp, so it no longer
    consumes the window row.

    The requirement is on this branch ALONE, which is why it does not reintroduce
    the duplication above. A durable injection is byte-identical to its window row,
    so it returns at the plain-equality check and never reaches here — and that pair
    genuinely does carry different stamps, because the two writers mint
    independently.
    """
    if disk_body == window_body:
        return True
    if role == "user":
        return False
    if disk_ts != window_ts:
        return False
    return _load_redacted(disk_body) == _load_redacted(window_body)


#: Window rows a bounded read must NOT hand back. ``_TRANSIENT_ROLES`` documents
#: itself as being about a window-region DISK line
#: (``chat_persistence.py:1320-1322``) and ``chat_persistence.py:1571`` uses it that
#: way. A bounded read answers a different question — which WINDOW rows does the
#: client still need — and three of those roles are still needed. ``permission``:
#: a pending approval is actionable and the client reads it out of the transcript,
#: so dropping it hides the approval bar while the server is still waiting.
#: ``chunk``/``streaming``: ``_prepare_messages`` does not discard a chunk run, it
#: collapses one into a single ``streaming`` row, and that is the only way in-flight
#: assistant text reaches this endpoint — the client filters raw ``chunk`` itself.
#: ``done`` is discarded by ``_prepare_messages`` regardless, and ``queued`` stays
#: listed because the client rebuilds those bubbles from the payload's ``queue``.
_UNOWED_WINDOW_ROLES = _TRANSIENT_ROLES - {"permission", "chunk", "streaming"}


def _is_answered_permission(m: dict) -> bool:
    """True for a ``permission`` row whose approval has already been answered.

    A permission row is never persisted, so it is always owed and therefore always
    lands in the tail — i.e. after every row that DID reach disk. For a pending
    approval that is the right place: it is the newest row, and nothing can follow
    it because the agent is blocked waiting on it. An answered one is history, and
    the agent has since produced turns that ARE on disk, so putting it in the tail
    moves it after them and the rendered order no longer matches what happened.

    The decision is written into the row's ``cls`` JSON in place
    (``state.py`` ``_mark_permission_resolved``), which is also the only place the
    stale-sweep and the slot resolver read it, so ``cls`` is the single source of
    truth here. Truthiness rather than key presence mirrors the client's own
    ``!meta.resolved`` test (``chatSlice.ts`` ``selectSlotPendingApproval``), so an
    empty decision still counts as pending and an actionable approval is never lost.
    """
    if m.get("role") != "permission":
        return False
    meta = parse_cls_meta(m.get("cls", "")) or {}
    return bool(meta.get("resolved"))


def _snapshot_slot_window(slot: "_ChatSlot") -> tuple[int, list[dict]]:
    """Capture ``(_disk_older_count, window)`` as one internally consistent pair.

    Call this on the EVENT LOOP where possible. The two reads have no ``await``
    between them, so no loop-scheduled writer can land in the middle — and the
    finalization that motivates this, ``chat_runner._flush_segment``, is a plain
    ``def`` that is never handed to ``to_thread``, so it cannot interleave with a
    loop capture. It assigns ``slot.messages = head`` and only then appends the
    finalized assistant row, so a reader that lands between those two statements
    sees a transient chunk-free window missing that row. A worker thread CAN land
    there, which is why capturing inside the threaded scan is the weaker option.

    From a thread the pair can still tear, so retry: a front trim bumps
    ``_disk_older_count`` (state.py:2191-2200) between the reads, and a PRE-trim
    window paired with a POST-trim count shortens ``window_disk``, hides the
    trimmed rows' ids and re-appends rows the disk read already returned. A trim
    is the only mutation that changes the window/count relationship, so read the
    count, copy the window, then confirm the count is unchanged. ``slot._lock``
    is an ``asyncio.Lock`` and cannot be acquired from a thread, so this mirrors
    the bounded re-read ``_save_slot_to_history`` uses for the same race
    (chat_persistence.py:1711-1722).
    """
    for _ in range(_FLUSH_SNAPSHOT_RETRIES):
        disk_older_count = slot._disk_older_count
        window = list(slot.messages)
        if slot._disk_older_count == disk_older_count:
            break
    else:
        disk_older_count = slot._disk_older_count
        window = list(slot.messages)
    return disk_older_count, window


def _append_unflushed_tail(
    slot: "_ChatSlot",
    all_msgs: list[dict],
    *,
    snapshot: tuple[int, list[dict]] | None = None,
) -> list[dict]:
    """Append window messages that are not yet on disk to a chained disk read.

    ``all_msgs`` is a disk read, so it omits transient roles while the window
    retains them, and it spans any older sessions a chained read walks. Sizing the
    tail by subtracting the two lengths therefore mixes units AND measures the
    whole file: it both re-appends rows the disk read already returned and lets a
    row from another writer consume a turn that is still owed.

    Takes the window itself rather than a caller-supplied count, so a caller cannot
    pass a length captured before an ``await``; the window can grow while a threaded
    disk read is in flight. ``snapshot`` is the one safe way to supply it: a
    ``(disk_older_count, window)`` PAIR from ``_snapshot_slot_window`` captured on
    the event loop AFTER the disk read, which is consistent by construction and
    cannot observe a mid-finalization window. Passing no snapshot falls back to
    capturing inside this thread, which is weaker — see that helper.

    Prefer message identity. A save copies each window row's ``meta.mid`` to disk,
    so a window row whose id appears in the disk read is persisted. A durable
    injector passes the window row's own id to ``ConversationLog.append``, which
    persists it in the same ``meta.mid`` shape — that copy carrying the id is the
    point: it IS the window row's flushed form and must match. A writer that passes
    no id persists no ``meta``, so its rows cannot be mistaken for a flushed window
    row.

    A disk read holding no ids at all needs a different boundary: a session
    persisted before ids existed, or rows a durable injector appended without
    going through a save. Walk the window and the disk read forward TOGETHER and
    stop at the first row that is not accounted for. A row from another writer no
    longer ENDS the run, which is what sizing the boundary as
    ``len(all_msgs) - slot._disk_older_count`` did — that measures the whole file,
    so a foreign append walked one row too far and dropped the owed turn. Both
    estimators the slot already carries are wrong here for opposite reasons: that
    subtraction over-counts, and ``_disk_window_len`` is not advanced by an
    injector, so it under-counts and would re-append a persisted row.

    The window is matched against the disk region as an ordered SUBSEQUENCE: a row
    that does not match the window row under consideration is SKIPPED rather than
    treated as the end of the window. The save is non-destructive against a
    cross-process append and merges the preserved rows back in TIME order
    (``_interleave_foreign_lines``), so the region can read
    ``[window, foreign, window]`` and an unmatched row means "not mine", not "end of
    window". Ending the run there leaves every persisted row after it in the tail,
    which appends an already-persisted suffix a second time.

    Skipping cannot pass over a row that should have matched: both sequences are
    chronological — the save's merge preserves each side's internal order — so a
    later window row's persisted copy cannot precede the current row's. It is also
    bounded: the disk cursor only ever moves forward, so the scans total
    O(window + region), and the first window row with no match anywhere in the
    remaining region ends the walk, which is the genuine end of the flushed prefix.

    A row matches on role plus content, compared through the redaction transform
    on both sides (``_same_persisted_body``). A shared ``ts`` is never SUFFICIENT —
    on a coarse clock two writers flooring off the same previous row both emit
    ``previous + 1µs`` (``history.py:1179-1219``), so accepting it alone drops an
    un-flushed message — but it is REQUIRED on the redaction-equivalent branch,
    where the only legitimate pair is a row and its own copy and the stamp is
    carried through verbatim.

    Ids are counted over the on-disk WINDOW REGION only,
    ``all_msgs[slot._disk_older_count:]``. The rows before that are the frozen prefix
    — on-disk rows older than the window, so none of them is in ``slot.messages``.
    Counting them would let an occurrence that exists only in the prefix fund a match
    for a window row that was never flushed, and the boundary would then walk past it.
    The fallback below already starts its disk cursor at the same offset.

    Id matching is selected only when EVERY row in that region carries a valid id, not
    merely when some row does. The dual-write injectors stamp both copies with one id
    (``slot.append`` mints it for the window copy and ``append_if_absent`` persists it
    on the durable copy), but the region can still legitimately hold a MIX: transcripts
    written before ids existed, and callers that pass no id. Choosing id matching on the
    strength of one id-carrying
    row then applies it to a row that structurally cannot match, which reads as
    un-flushed and appends the injection a second time. A mixed region belongs on the
    ordered path, which compares the fields both writers do record.

    Ids are matched as a MULTISET, one disk occurrence consumed per window row, not
    as a set. ``meta`` on an inbound message is caller-supplied and an id is minted
    only when one is *absent*, so a caller can post the same id twice. A set then
    matches EVERY window row carrying that id, so the boundary walks past a row that
    was never persisted and the response omits it — the silent-loss direction. One
    disk row is enough for that; two disk rows sharing an id are not required.
    Consuming an occurrence bounds the match to as many rows as really reached disk,
    and the earliest window row is the persisted one because flushes follow window
    order.

    Only string ids are matched. A truthy non-string ``mid`` survives to disk for the
    same caller-supplied reason and would raise ``TypeError`` if hashed.

    The id path selects the owed rows by MEMBERSHIP rather than by a prefix
    boundary. A boundary assumes every persisted row precedes every un-flushed one.
    When it does not, a later match moves the boundary past an un-flushed row and
    the response omits it — a drop, which is worse than the duplication this
    function exists to prevent. Ending the walk at the first miss is not the
    remedy either: a transient row is dropped by the save and so can never match,
    and stopping there re-appends every persisted row after it. Rows the client does
    not need are skipped outright (``_UNOWED_WINDOW_ROLES``), so selecting by
    membership cannot surface one the boundary happened to exclude; a pending
    ``permission`` row is deliberately not among them. Because an id in
    the disk window region proves that row reached disk, the owed set is simply the
    rows whose id did not, kept in window order. Where the persisted rows really
    are a prefix this returns the same answer, so it is a strict generalisation.
    """
    if snapshot is None:
        snapshot = _snapshot_slot_window(slot)
    disk_older_count, window = snapshot
    window_disk = all_msgs[disk_older_count:]
    disk_mid_positions: dict[str, list[int]] = {}
    every_row_has_an_id = bool(window_disk)
    for i, m in enumerate(window_disk):
        meta = m.get("meta")
        mid = meta.get("mid") if isinstance(meta, dict) else None
        if isinstance(mid, str) and mid:
            disk_mid_positions.setdefault(mid, []).append(i)
        else:
            every_row_has_an_id = False
    tail: list[dict]
    if every_row_has_an_id:
        # Membership, not a prefix boundary: see the docstring for why neither a
        # boundary nor a break-on-miss is correct here.
        #
        # Owed rows are MERGED at their window position, not concatenated after the
        # whole disk slice. Window order is authoritative and a persisted row can
        # sit LATER in it than an owed one: _flush_segment pulls a stop_event out of
        # the trailing chunk run and re-appends it AFTER the finalized assistant row
        # (chat_runner.py:2686-2687), so a stop that reached disk during streaming
        # follows a reply that is still owed. Appending owed rows last renders that
        # pair inverted -- stop before the reply it belongs to.
        #
        # Every row here carries an id, so the position is derivable without the
        # body matching the other arm needs. Persisted rows keep their disk order
        # and none is dropped; each owed row is only INSERTED before the disk row of
        # the next window row that reached disk, so this is additive.
        owed_before: dict[int, list[dict]] = {}
        pending: list[dict] = []
        for m in window:
            if m.get("role", "assistant") in _UNOWED_WINDOW_ROLES:
                continue
            if _is_answered_permission(m):
                continue
            meta = m.get("meta")
            mid = meta.get("mid") if isinstance(meta, dict) else None
            positions = disk_mid_positions.get(mid) if isinstance(mid, str) else None
            if positions:
                at = positions.pop(0)
                if pending:
                    owed_before.setdefault(at, []).extend(pending)
                    pending = []
                continue
            pending.append(m)
        if not owed_before and not pending:
            return all_msgs
        merged: list[dict] = list(all_msgs[:disk_older_count])
        for i, m in enumerate(window_disk):
            merged.extend(owed_before.get(i, ()))
            merged.append(m)
        merged.extend(pending)
        return merged
    else:
        start = 0
        d = min(disk_older_count, len(all_msgs))
        # An owed row is one the disk slice does not already carry, and this arm walks
        # the WHOLE window so that a single owed row cannot strand the rows behind it.
        # There are two ways to be owed, and both route to ``owed_rows``:
        #
        #   1. A TRANSIENT role. A disk read omits transient roles entirely, so such a
        #      row can NEVER be matched and is ALWAYS owed. Only ``_UNOWED_WINDOW_ROLES``
        #      (``done``/``queued``) and an already-answered ``permission`` are genuinely
        #      not owed.
        #   2. A non-transient row the forward scan does not find on disk. This used to
        #      ``break`` the loop outright, which left ``start`` pointing AT the unmatched
        #      row, so ``window[start:]`` re-emitted every LATER window row -- including
        #      rows already on disk. With a stop_event flushed before reply finalization
        #      (``_flush_segment`` re-appends the stop AFTER the finalized assistant row,
        #      see the note at the top of this function) the window reads
        #      ``[... unflushed reply, flushed stop]``: the reply missed, the loop broke,
        #      and the persisted stop came back a second time and out of order -- the
        #      very duplication this function exists to remove.
        #
        # The sibling id-carrying arm above already has the right rule, so mirror it
        # rather than inventing a second one: an unmatched row is held, a later match
        # flushes what is held at ITS disk position, and leftovers stay in the tail.
        # That keeps owed rows in window order instead of after the whole disk slice.
        #
        # Nothing is emitted twice: ``start`` only advances on a match, and a match flushes
        # ``owed_rows`` first, so every flushed row had an index below ``start``. Whatever
        # is left over sits at or after ``start`` and is carried by the trailing slice --
        # but that slice needs the unowed/answered exclusions applied to it as well, for
        # the reason recorded at the slice itself.
        owed_at: dict[int, list[dict]] = {}
        owed_rows: list[dict] = []
        for i, m in enumerate(window):
            role = m.get("role", "assistant")
            if role in _TRANSIENT_ROLES:
                if role not in _UNOWED_WINDOW_ROLES and not _is_answered_permission(m):
                    owed_rows.append(m)
                continue
            body = m.get("content", "")
            probe = d
            while probe < len(all_msgs):
                row = all_msgs[probe]
                if row.get("role", "assistant") == role and _same_persisted_body(
                    row.get("content", ""),
                    body,
                    role,
                    row.get("ts", ""),
                    m.get("ts", ""),
                ):
                    break
                probe += 1
            if probe >= len(all_msgs):
                owed_rows.append(m)
                continue
            if owed_rows:
                owed_at.setdefault(probe, []).extend(owed_rows)
                owed_rows = []
            d = probe + 1
            start = i + 1
        # ``start`` does NOT advance past an unowed row: such a row takes the
        # ``_TRANSIENT_ROLES`` branch above, is correctly kept out of ``owed_rows`` by the
        # guard there, and then ``continue``s -- skipping ``start = i + 1``. So a raw
        # ``window[start:]`` re-admits any unowed row that TRAILS the last match, and the
        # exclusion the guard performed is undone. The sibling arm does not have this hole
        # because it applies both exclusions at the TOP of its loop, so its leftovers can
        # never hold one. Apply the same two exclusions here, which is what actually
        # mirrors it.
        #
        # Two symptoms, one cause. A trailing ``done`` reaches the bounded response and
        # ``_prepare_messages`` then drops it while rendering (``chat_utils.py``), so a
        # page whose only row is that ``done`` renders EMPTY and replaces the transcript.
        # A trailing answered ``permission`` is instead re-ordered after every persisted
        # row -- the misordering ``_is_answered_permission`` exists to prevent.
        #
        # ``chunk``/``streaming`` and a still-PENDING ``permission`` are genuinely owed and
        # MUST survive this filter; narrowing it further would be the opposite defect.
        tail = [
            m
            for m in window[start:]
            if m.get("role", "assistant") not in _UNOWED_WINDOW_ROLES
            and not _is_answered_permission(m)
        ]
        if not owed_at and not tail:
            return all_msgs
        merged_idless: list[dict] = []
        for idx, row in enumerate(all_msgs):
            merged_idless.extend(owed_at.get(idx, ()))
            merged_idless.append(row)
        merged_idless.extend(tail)
        return merged_idless


async def api_chat_slot_detail(request: web.Request) -> web.Response:
    """GET /api/chat/slots/{slot} — message history for a slot.

    Query params:
      - ``limit``: max messages to return (optional; if omitted, returns ALL messages from disk).
        Clamped to 1..500. A value below 1 is rejected rather than clamped up, because
        no caller asking for 0 wanted exactly one message.
      - ``before``: return messages before this index (legacy pagination, still supported).
        ``before=0`` is valid and yields an empty page.

    Either param being a non-integer is a 400; both used to raise out of the
    handler and surface as a 500.

    By default (no limit), reads the full chained history from disk across
    gateway restarts. Pagination params are retained for backwards compatibility.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_detail")
    if denied is not None:
        return denied

    limit_raw = request.query.get("limit")
    before_raw = request.query.get("before")

    # Both params arrive as strings and were converted at their point of use, so a
    # non-integer escaped as a ValueError and the client saw a 500 for what is
    # plainly a bad request. The branch below still keys off the RAW values, so
    # routing is unchanged.
    try:
        limit = min(int(limit_raw or "200"), 500)
        before = int(before_raw) if before_raw is not None else None
    except ValueError:
        return web.json_response(
            {"error": "limit and before must be integers", "code": "invalid_query_params"},
            status=400,
        )
    # Clamped above but not below, limit=0 made `start == end`: an empty page
    # reporting has_more true, which paginates forever.
    if limit < 1:
        return web.json_response(
            {"error": "limit must be >= 1", "code": "limit_out_of_range"}, status=400
        )

    # No limit → load ALL messages (chained across gateway restarts).
    # In-memory slot.messages is authoritative for the current session.
    # _disk_older_count gates whether to read disk AND provides the stable
    # slice boundary (set at restore/resume, never drifts with new messages).
    if limit_raw is None and before_raw is None:
        mem_msgs = list(slot.messages)
        if slot._disk_older_count > 0 and state.conversation_log:
            history_key = slot_history_key(slot)
            try:
                disk_msgs = await asyncio.to_thread(
                    state.conversation_log.read_messages_chained, history_key
                )
            except Exception:
                logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
                disk_msgs = []
            older = disk_msgs[: slot._disk_older_count] if disk_msgs else []
            # Re-read the tail after the await: that suspension point lets a message
            # land mid-read, and the client replaces its list with this response.
            messages = older + list(slot.messages)
        elif state.conversation_log:
            # _disk_older_count == 0: the window is supposed to be the whole
            # session. But disk can grow beyond the window (a concurrent writer,
            # a foreign append, or a persistence race). Detect and include any
            # rows the in-memory window is missing (#4373).
            # Safety: skip when the slot has unflushed rows or pending rewrites.
            _slot_idle = (
                len(mem_msgs) <= getattr(slot, "_disk_window_len", 0)
                and not getattr(slot, "_pending_rewrite", False)
                and not getattr(slot, "_dirty_flag", False)
            )
            if _slot_idle:
                history_key = slot_history_key(slot)
                try:
                    disk_msgs = await asyncio.to_thread(
                        state.conversation_log.read_messages_chained, history_key
                    )
                except Exception:
                    logger.warning(
                        "read_messages_chained failed for %s", history_key, exc_info=True
                    )
                    disk_msgs = []
                # Re-read after the await to capture anything that arrived mid-read.
                current_mem = list(slot.messages)
                # Post-await re-check: slot may have gained unflushed rows.
                _slot_idle = (
                    len(current_mem) <= getattr(slot, "_disk_window_len", 0)
                    and not getattr(slot, "_pending_rewrite", False)
                    and not getattr(slot, "_dirty_flag", False)
                )
                if _slot_idle and len(disk_msgs) > len(current_mem):
                    # Validate alignment: if rotation shifted offsets, the disk
                    # prefix no longer matches memory — skip reconciliation to
                    # avoid appending the wrong slice (#4373 fix, GPT finding 2).
                    _aligned = True
                    if current_mem and disk_msgs:
                        # Spot-check last memory row against its expected disk position.
                        last_mem = current_mem[-1]
                        disk_at = disk_msgs[len(current_mem) - 1] if len(current_mem) <= len(disk_msgs) else None
                        if disk_at and (
                            last_mem.get("ts", "") != disk_at.get("ts", "")
                            or last_mem.get("role") != disk_at.get("role")
                        ):
                            _aligned = False
                    if not _aligned:
                        messages = current_mem
                    else:
                        # Disk has rows the window does not — reconcile by appending
                        # the missing tail to the slot and returning the union.
                        fresh = disk_msgs[len(current_mem):]
                        for msg in fresh:
                            role = msg.get("role", "assistant")
                            cls = msg.get("cls") or (
                                "msg msg-u" if role == "user" else "msg msg-a"
                            )
                            content = msg.get("content", "")
                            if role != "user":
                                content, _ = redact_exfiltration_urls(content)
                                content, _ = redact_credentials(content)
                            slot.append(
                                role,
                                content,
                                cls,
                                ts=msg.get("ts", ""),
                                broadcast=False,
                                meta=(
                                    _redact_meta_for_role(role, msg["meta"])
                                    if isinstance(msg.get("meta"), dict)
                                    else None
                                ),
                            )
                            carry_provenance(slot.messages[-1], msg)
                            _attach_variants(slot, msg)
                        # Replayed rows came from disk — drain the replay
                        # frames and mark the window persisted (not dirty) so a
                        # fork/SSE drain or the next save does not duplicate them.
                        slot.drain()
                        slot._resumed_count = len(slot.messages)
                        slot._disk_window_len = len(slot.messages)
                        slot._dirty = False
                        # Use the full disk corpus (which includes the prefix
                        # plus the reconciled tail) rather than slot.messages,
                        # because slot.append may have trimmed the head under
                        # _MAX_SLOT_MESSAGES — returning slot.messages alone
                        # would lose older rows without signaling has_more.
                        messages = disk_msgs
                else:
                    messages = current_mem
            else:
                messages = mem_msgs
        else:
            messages = mem_msgs
        total = len(messages)
        has_more = False
        # This branch returns the whole corpus, so there is no older page to ask
        # for. Sent anyway so the field is present on every response shape.
        next_before = 0
    else:
        # Legacy pagination path (retained for programmatic callers).
        # Always reads from chained disk history; no in-memory offset math.
        history_key = slot_history_key(slot)
        try:
            all_msgs = (
                await asyncio.to_thread(
                    state.conversation_log.read_messages_chained, history_key
                )
                if state.conversation_log
                else []
            )
        except Exception:
            logger.warning("read_messages_chained failed for %s", history_key, exc_info=True)
            all_msgs = []
        # Append any un-flushed in-memory tail messages beyond what's on disk.
        # Snapshot on the LOOP, after the disk read: the two reads inside the helper
        # have no await between them, so a synchronous finalization cannot be caught
        # half-done the way a worker thread can catch it.
        tail_snapshot = _snapshot_slot_window(slot)
        all_msgs = await asyncio.to_thread(
            _append_unflushed_tail, slot, all_msgs, snapshot=tail_snapshot
        )
        # One row must mean one displayed message BEFORE `limit` is applied. The
        # owed rows above can include `chunk`/`streaming`, and a segment still
        # streaming is hundreds of rows that render as one message, so slicing
        # first spends the caller's budget on rows the response will not carry
        # and returns a mid-sentence fragment.
        #
        # Reduce the whole corpus, not a trailing slice: the helper places owed
        # rows at the disk index they belong to, so they are not a contiguous
        # suffix and a slice-scoped fold would miss the interleaved ones. In a
        # thread for the same reason the append is -- whole-corpus work does not
        # belong on the event loop.
        #
        # `done` is already excluded upstream (`_UNOWED_WINDOW_ROLES`), so on
        # this path the reduction's remaining job is folding the chunk runs.
        all_msgs = await asyncio.to_thread(_collapse_wire_rows, all_msgs)
        total = len(all_msgs)
        if before is not None:
            end = max(0, min(before, total))
        else:
            end = total
        start = max(0, end - limit)
        messages = all_msgs[start:end]
        has_more = start > 0
        # The cursor the client should send next, in the RAW index space this
        # slice was taken in. The client cannot derive it from the response:
        # `_prepare_messages` drops `done`, so the returned row count is not
        # the span consumed here.
        next_before = start

    # Snapshot every slot field the response needs BEFORE leaving the event
    # loop: the render below runs in a worker thread, and it must not read
    # attributes the loop keeps mutating mid-turn. `messages` is already a
    # fresh top-level list in both branches above; the message dicts inside it
    # are shared with live mutation, which _prepare_messages tolerates by the
    # same snapshot discipline the flush-thread save path relies on.
    key = slot.key
    running = slot.running
    stopping = slot._stopping
    display_title = slot.display_title
    queue_snapshot = [{"id": q["id"], "content": q["content"]} for q in slot._queue]
    context_fields = await _context_snapshot_fields(state, slot)

    def _render() -> str:
        # Off-loop on purpose. _prepare_messages applies a regex-heavy
        # redaction battery to the ENTIRE history; on a multi-MB session that
        # blocked the event loop past the loop-stall watchdog's exit budget
        # and hard-exited the gateway. json.dumps of the same payload is a
        # second loop-blocking cost, so it lives in the thread too.
        prepared = _prepare_messages(messages, running)
        return json.dumps(
            {
                "key": key,
                # Redacted at emit like every sibling path (_ChatSlot.to_dict
                # does the same for the sidebar payload). Titles can be
                # LLM-generated or set by a rename, so they are content, not
                # configuration.
                "title": _redact_for_display(display_title),
                "running": running,
                "stopping": stopping,
                "messages": prepared,
                "queue": [
                    {"id": q["id"], "content": _redact_for_display(q["content"])}
                    for q in queue_snapshot
                ],
                "total": total,
                "has_more": has_more,
                "next_before": next_before,
                # Seeds the context meter on open. Turn-scoped WS frames alone
                # leave it empty for a session reopened in a new tab; omitted
                # entirely (not zeroed) when genuinely unknown, so the frontend
                # can tell "no reading" from "0% used".
                **context_fields,
            }
        )

    # Per-slot single-flight: concurrent refetches of the same slot (WS
    # reconnect + switchSlot + chat_done all refetch) queue here instead of
    # each burning a worker thread on the same multi-MB redaction pass.
    async with slot._detail_render_lock:
        body = await asyncio.to_thread(_render)
    return web.Response(text=body, content_type="application/json")


# Modes a slot may be CREATED with. A deliberate superset of the mode-SWITCH
# allowlist (chat_folders._VALID_MODES) and the fork override allowlist
# (chat_fork): "design-critique" is an app-worker mode assigned at birth by the
# Design Critique app's openSlot() — the custom mode keeps its throwaway dc-*
# slots off the chat sidebar, which renders only "", "orchestrator" and "crew"
# (ChatPage.tsx filteredSlots). Switching an existing session INTO an app-worker
# mode, or forking one with it as an override, is not a real flow, so those two
# allowlists deliberately stay narrower — do not "sync" them to this one.
_CREATABLE_MODES = ("", "orchestrator", "crew", "design-critique")


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
            _mode = body.get("mode", "")
            if _mode not in _CREATABLE_MODES:
                return web.json_response(
                    {"error": "invalid mode", "code": "invalid_mode"}, status=400
                )
            # Same boundary as the mode-switch endpoint: a slot whose name folds
            # to nothing but dots has no crew store, so accepting `mode="crew"`
            # here would hand back a tab that 500s on its first message. Only a
            # CALLER-SUPPLIED name can be that: an omitted name is generated by
            # `get_or_create_slot` and is always storable. Checked on the
            # NORMALIZED form, which is the key the store is built from — the raw
            # body name is not what `CrewStore` ever sees.
            if _mode == "crew" and name:
                # Deferred: this module is imported when the dashboard package is,
                # which the gateway does on its boot path, and crew is a
                # dashboard-only subsystem. Only a crew request pays for it.
                from kiro_crew.crew_chat import is_crew_capable_slot_key
            if (
                _mode == "crew"
                and name
                and not is_crew_capable_slot_key(_normalize_slot_key(str(name)))
            ):
                return web.json_response(
                    {"error": "this session name cannot run crew mode",
                     "code": "crew_unsupported_slot"},
                    status=400,
                )
            slot = state.get_or_create_slot(
                name,
                agent=agent,
                workspace=workspace,
                model=model,
                mode=_mode,
                memory_mode=memory_mode,
                ephemeral=body.get("ephemeral"),
                app=request.get("app", ""),
                origin=request_slot_origin(request.get("app", "")),
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
            return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
        # Pin title if explicitly provided (prevents auto-title from overwriting)
        title = (body.get("title") or "").strip()[:200] if isinstance(body, dict) else ""
        if title:
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)
            slot.title = title
            slot._titled = True
            # A pinned title is caller-explicit: record origin "user" so the
            # background title refresh never rewrites it (this endpoint can
            # address an ALREADY-auto-titled slot whose origin would otherwise
            # stay "auto"), and bump the epoch so an in-flight background
            # attempt stands down instead of clobbering the pin.
            slot._title_origin = "user"
            slot._title_epoch += 1
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
            previous_folder = slot.folder_id
            previous_changed = slot._folder_changed
            if folder_id != slot.folder_id:
                slot._folder_changed = True
            slot.folder_id = folder_id
            # Existence is only reliable inside the store lock. If the folder
            # went away, abandon THIS assignment and leave the slot as it was —
            # `name` can address an already-used slot, so clearing outright would
            # unfile a conversation that was sitting in a perfectly good folder
            # of its own. This is a chat turn, so declining the move beats
            # failing the turn.
            if not await _unhide_folder(state, folder_id):
                slot.folder_id = previous_folder
                slot._folder_changed = previous_changed
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
    # A pinned title must persist too (not just a folder move): without the
    # write, a restart rehydrates the previous title with a refreshable "auto"
    # origin and the background refresh may rewrite the pin.
    if folder_id or title:
        await save_slot_off_loop(state, slot, force=True)
    # Speculative session creation: overlap the ACP handshake with the user's
    # think-time before their first message. No-op unless session.eager_spawn.
    schedule_eager_spawn(state, slot)
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
        logger.info("Stop: cancelled %d pending question(s) on slot %s", cancelled, slot.key)


def _subagents_attached_response(
    state: DashboardState, slot: _ChatSlot, session_key: str, operation: str
) -> web.Response | None:
    """409 while sub-agent children are attached to *session_key*, else None.

    One guard for every endpoint whose action cannot coexist with children —
    dispatching a new turn (continue) interleaves with their writes, and a
    session teardown (reload) kills the shared runtime they run on. Two copies
    of this block is how the probes diverge, and this one fails toward
    discarding a child's work.

    Three probes, none optional:

    * `running_agents_for` on the true session key. QUEUED children count too:
      a spawn that hit the concurrency/stagger gate is deliberately absent
      from `_agents` (see `SubagentInfo.queued`), yet it WILL start on its own.
    * IN-FLIGHT RESULT DELIVERY: the last child can finish — emptying both
      probes — while its `[Subagent completion event]` injection is still
      landing, and that injection needs both the transcript order and the
      session it reports to. The runner's own synthesis gate pairs the same
      conditions at both its call sites (chat_runner).
    * Fail closed on a None running-probe: that is the probe FAILING, not a
      slot with no children, and mistaking the two is exactly the hazard this
      guard exists to prevent. Mirrors the stage gate in chat_orchestrator.
    """
    subs = getattr(state, "subagents", None)
    if subs is None:
        return None
    running = subs.running_agents_for(session_key)
    queued = 0
    if running is not None:
        try:
            queued = subs._queued_depth(session_key)
        except Exception:
            # An unreadable queue is unknown children, not zero children.
            logger.debug("%s: queued-depth probe failed", operation, exc_info=True)
            queued = 1
    inflight = getattr(slot, "_subagent_deliveries_inflight", 0)
    if running is None or running or queued or inflight:
        return web.json_response(
            {"error": "sub-agents are running", "code": "slot_subagents_running"},
            status=409,
        )
    return None


async def _reset_slot_session(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    *,
    skip_if_busy: bool = False,
) -> bool:
    """Reset a slot's agent session, releasing anything blocked on the old one.

    The switch handlers (agent, model, bulk model, reasoning effort, workspace)
    and the reload endpoint reset the session so the next message starts under
    the new setting. That tears down the agent process — but a pending
    ``ask_question`` lives in dashboard state, not in the session, so without
    this it survives the reset: the card stays on screen inviting an answer,
    and the blocked HTTP request holds an MCP worker until its own timeout with
    no agent left to receive the answer it eventually returns.

    Routing every reset through one helper rather than adding a second call at
    each site is deliberate, and is the same reasoning as
    :func:`_unblock_pending_waits`: six call sites each having to remember an
    extra line is how one of them gets missed.

    ``skip_if_busy`` forwards to :meth:`SessionManager.reset`, which evaluates
    busyness atomically with the session pop; False means the reset was
    declined or there was no live session to tear down. The unblock still runs
    first even then: a wait can only be pending from a turn old enough to have
    completed an LLM round-trip, and such a turn is visible to any caller's
    has_active_turn() fast path — so a decline here implies a turn that started
    microseconds ago, which cannot have posted a card yet.
    """
    _unblock_pending_waits(state, slot)
    return await state.sessions.reset(session_key, skip_if_busy=skip_if_busy)


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


def _slot_not_found() -> web.Response:
    """The one 404 every cancel-route refusal returns.

    A denial and a genuinely missing slot MUST be byte-identical, or an app can
    tell "this slot is not mine" from "this slot does not exist" and enumerate
    foreign slot names. Single-sourced so the two cannot diverge; the shape
    matches ``api_chat_slot_continue``.
    """
    return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)


def _cancel_target(slot: _ChatSlot) -> str:
    """The session a cancel on *slot* must address.

    Never ``_history_key_for(name)``: every slot carrying a
    ``linked_session_key`` — a cron-born tab (``cron:<job_id>``), a channel-born
    tab (``slack:<ts>``), a workflow-born tab — runs its turns under that key,
    while the dashboard-prefixed spelling names a session that never existed.
    ``SessionManager.stop_turn`` then finds nothing and returns ``"idle"``, the
    handler settles the card as "stopped", and the turn keeps streaming, so Stop
    is a silent no-op that reports success once per press.

    Routing alone is not enough either. A running turn owns a stable identity:
    ``_run_chat`` captures the key it acquires and keeps using that one for the
    whole turn, while
    ``linked_session_key`` remains mutable underneath it — a cron injection
    binds an already-live slot with no ``running`` gate. Re-deriving the key at
    cancel time therefore names wherever the slot routes the NEXT turn, which
    after a mid-turn rebind is not the turn the operator is trying to stop.

    Falls back to the routing when no turn is in flight (nothing to have
    captured an identity), which is also what a slot restored from disk answers
    — the field is runtime-only and empty after a restart.
    """
    return getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)


def _app_cancel_denied(
    request: web.Request, slot: _ChatSlot, operation: str, target_key: str
) -> web.Response | None:
    """Whether *request* may cancel *target_key*, as an indistinguishable 404.

    Two conditions for an app token, because slot ownership does NOT imply
    ownership of the session the cancel would land on:

    1. the app owns the slot (App Kit §5.2, deny-by-default), and
    2. the session about to be cancelled is still the slot's own dashboard
       session, not one the app has no claim on.

    Condition 2 is load-bearing. ``get_or_create_slot`` takes ``app`` and, for a
    name shaped like a channel session stem, resolves ``linked_session_key``
    from the session map in the same call — so an app that names a live channel
    thread ends up owning a slot bound to a conversation it has no claim on.
    Ownership alone would then authorize cancelling that channel's turn, turning
    a slot binding into capability escalation.

    It tests *target_key* — the key the caller will actually cancel — rather
    than re-reading the slot, so authorization and action cannot disagree. That
    is not only a TOCTOU guard: for a turn that started on the app's own session
    and was rebound mid-flight, re-reading would DENY the app its own running
    turn, because the routing now points somewhere it does not own.

    A dashboard caller has no app scope and may cancel either kind.

    Shared by the cancel routes so /stop and /interrupt cannot drift onto two
    policies.
    """
    request_app = request.get("app", "")
    if not request_app:
        return None

    if request_app != slot._app:
        reason = (
            "app cannot access unscoped slots" if not slot._app else "app does not own this slot"
        )
    elif target_key != _history_key_for(slot.key):
        reason = "app does not own the session this slot is linked to"
    else:
        return None

    sel().log_api_access(
        caller=request_app,
        operation=operation,
        outcome="denied",
        source="app_isolation",
        resources=f"slot={slot.key}",
        error=reason,
    )
    return _slot_not_found()


async def api_chat_slot_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/stop — cooperative stop with kill fallback.

    First press: soft cancel (cooperative). Second press (?force=true):
    hard kill. Inserts a stop_event message into the slot transcript.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return _slot_not_found()
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_stop")
    if denied is not None:
        return denied
    # Before ANY side effect — the escalation branch below clears the queue and
    # drops pending steers before it reaches stop_turn, so a guard placed later
    # would still let a foreign caller mutate the slot.
    # One target, resolved once: the session the in-flight turn actually runs
    # on. Authorization and every stop_turn below are handed this same value, so
    # they cannot disagree — neither across the request-body await in
    # /interrupt, nor across two presses of Stop while a rebind lands between
    # them.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_stop", cancel_key)
    if denied is not None:
        return denied
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
        # Stop addresses the SESSION, so it resolves through
        # effective_session_key: a channel-linked slot's turns run under its
        # linked_session_key (slack:<ts>), and handing stop_turn the
        # dashboard:<slot> key names a session no running turn owns — the stop
        # reports success and cancels nothing. The SEL record below stays on the
        # slot-derived key, which identifies the tab the operator pressed.
        await state.sessions.stop_turn(cancel_key, force=True, on_hard=_on_hard_force)
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
        cancel_key,
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

    NOT readiness-gated, and that is deliberate — see
    ``kiro_readiness.reject_if_kiro_unverified``. Continue is an ordinary send: it
    queues one synthetic message and lets the runner dispatch it, mutating nothing
    durable up front, so the ACP attempt is its authority and a signed-out install
    reports ``AcpAuthRequired`` in the transcript. Gating it instead put the
    button behind a latch that is refreshed by re-probing ``kiro-cli``, and a
    probe that merely TIMES OUT reads as signed-out: on a host where that probe is
    slow the press was refused with a 503 forever while typing the same request by
    hand worked. The unequal treatment of two paths that dispatch the same turn is
    the bug; the transcript's own error card is the report either way.
    """
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
                "app cannot access unscoped slots"
                if not slot._app
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
        # Children guard — see _subagents_attached_response for the three
        # probes and why each is load-bearing. `effective_session_key`, never
        # `f"dashboard:{slot.key}"`: a channel-born slot's children register
        # under the channel key, and the dashboard-prefixed form silently
        # matches nothing — `_history_key_for`'s own docstring says as much.
        denied_409 = _subagents_attached_response(
            state, slot, effective_session_key(slot), "continue"
        )
        if denied_409 is not None:
            return denied_409
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
        if is_system_notice(m.get("role"), m.get("meta")):
            continue
        if m.get("role") in ("user", "assistant") and m.get("content"):
            return True
    return False


def _is_stop_event(m: dict) -> bool:
    """True when *m* is the card recorded because the user pressed Stop.

    Thin alias over ``state.is_stop_event_row`` — the predicate lives there
    (next to ``parse_cls_meta``, its one dependency) so the slot-summary
    builder can share it without importing this handler module.
    """
    return is_stop_event_row(m)


def _is_interrupted(slot: _ChatSlot) -> bool:
    """True when the transcript shows a turn that ended without a reply.

    Thin adapter over ``state.is_turn_interrupted``, which owns the scan and
    its contract (see its docstring). Shared with the slot-summary builder so
    the Continue endpoint, the composer's Resume gate, and the sidebar's
    ``interrupted`` field can never disagree about what an interruption is.
    """
    return is_turn_interrupted(slot.messages)


async def api_chat_slot_end_wait(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/end-wait — ask the sleeping `wait` tool to
    return early. Body: ``{"wait_id": "..."}``.

    Cooperative, and deliberately NOT a cancel. The tool sleeps in a separate
    MCP subprocess that runs no listener, so there is nothing to signal: the
    request is parked on the slot and collected by the tool on its next
    keepalive poll (see WAIT_PING_SECS — bounded at 5s). The turn then continues
    with a normal tool result, which is the whole point of not routing this
    through /stop: /stop can only end a wait as collateral of killing the
    session, losing in-flight results and paying a respawn.

    ``wait_id`` is required and must match the sleep currently in flight. That
    rejects the two races a slot-scoped flag would have accepted: a click landing
    after the wait already elapsed, and a click from a stale tab still showing a
    previous wait's countdown.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_end_wait")
    if denied is not None:
        return denied
    try:
        body = await request.json() if request.content_length else {}
    except Exception:
        body = {}
    # `request.json()` happily returns a list or a scalar for well-formed JSON
    # that simply is not an object, and `.get` on one of those raises past the
    # except above into a 500. Normalize the shape, not just the parse.
    if not isinstance(body, dict):
        body = {}
    wait_id = str(body.get("wait_id") or "").strip()
    if not wait_id:
        return web.json_response(
            {"error": "wait_id required", "code": "wait_id_required"}, status=400
        )
    current = slot._wait_state or {}
    if current.get("wait_id") != wait_id:
        return web.json_response(
            {"error": "no such wait in flight", "code": "wait_not_in_flight"}, status=409
        )
    slot._end_wait_request = wait_id
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_end_wait",
        tool_kind="command",
        outcome="success",
        metadata={"slot": name, "wait_id": wait_id},
    )
    return web.json_response({"ok": True})


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
        return _slot_not_found()
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_interrupt")
    if denied is not None:
        return denied
    # Before the _stop_state claim and the queue promotion below, both of which
    # mutate the slot ahead of stop_turn.
    # Resolved once, before the request-body await below, and used for both the
    # guard and the cancel — see api_chat_slot_stop for the same rule.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_interrupt", cancel_key)
    if denied is not None:
        return denied
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
        # Wire-side field is `queue_id`; stored items carry `id` (the key
        # queue_append/queue_insert write and every *_by_id helper matches).
        # The previous inline loop compared item.get("queue_id"), which is
        # None on every production item — a silent no-op that made the
        # "run this next" click land on whatever happened to be at the
        # front of the queue instead of the selected message.
        slot.queue_promote_by_id(queue_id)

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
        cancel_key,
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_queue_cancel")
    if denied is not None:
        return denied
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_queue_edit")
    if denied is not None:
        return denied
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_queue_reorder")
    if denied is not None:
        return denied
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


class _NudgeRetireFailed(Exception):
    """A slot close could not retire the slot's auto-nudge loop.

    Carries the loop so the caller can put it back in MEMORY, which is the point:
    the failure happens between ``remove()``'s in-memory drop and its registry
    write, so memory and disk disagree until one of them is corrected. Restoring
    memory re-agrees with the still-armed disk, leaving the session open and
    still driven rather than open and abandoned.
    """

    def __init__(self, loop: "NudgeLoop | None") -> None:
        super().__init__("autonudge loop removal on slot close failed")
        self.loop = loop


async def _retire_slot_nudge_loop(name: str) -> "NudgeLoop | None":
    """Retire *name*'s auto-nudge loop and return it (None if it had none).

    Retire this slot's loop at the moment the user dismissed the tab. "Respect
    the close" used to be an emergent property of the fire path's rehydrate miss
    — and that is precisely the miss the fire path must now adopt through (see
    ``_fire_dashboard_nudge``'s ``adopt_closed``), or idle archival kills loops
    terminally. Making the user's ✕ the explicit retirement keeps the rule intact
    without relying on a cache miss to enforce it.

    MUST be called BEFORE the close path's first await. Two reasons, both of
    which resurrect a session the user closed:

    * The loop's timer can EXPIRE during an await of the close (the turn-cancel
      wait, the history persist, the session teardown). The slot is already out
      of ``state._slots`` by then, so the fire path takes its rehydrate branch
      and restores the transcript with ``adopt_closed=True`` — the very
      transcript the persist is marking closed.
    * Cancelling ``slot.task`` runs ``_run_chat``'s finally, which re-arms the
      timer through ``notify_turn_complete``. Disarming without removing is
      therefore not enough: the clock comes straight back mid-close.

    ``remove()`` is what makes this atomic: its uncontended lock acquire does not
    yield, so the loop leaves the registry and its timer is cancelled before the
    coroutine can suspend, and ``notify_turn_complete`` then finds nothing to
    re-arm.

    The returned loop is the only remaining record of it — the persist-failure
    path uses it to put the clock back (see :func:`_restore_slot_nudge_loop`).

    A removal that FAILS raises :exc:`_NudgeRetireFailed` rather than logging and
    carrying on. ``remove()`` drops the loop from memory first and only then
    writes the registry, so a write that raises leaves memory retired while the
    DISK still lists the loop. Swallowing that let the close finish and persist
    the slot as closed, and the next start read the surviving record back: the
    fire path answers the missing slot with ``adopt_closed=True``, so the loop
    rebuilt the dismissed session and ran an unattended turn in it. Locating a
    session the user closed is exactly the outcome this function exists to
    prevent, so the close must not proceed on a half-applied retirement.
    """
    loop = None
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        svc = _autonudge_get()
        if svc is None:
            return None
        loop = svc.get_by_slot(name)
        if loop is None:
            return None
    except Exception:
        # Only the LOOKUP is tolerated: no service and no loop both legitimately
        # mean "nothing to retire", and neither can leave state half-applied.
        logger.warning("autonudge loop lookup on slot close failed", exc_info=True)
        return None
    try:
        await svc.remove(loop.id)
    except Exception as exc:
        logger.warning("autonudge loop removal on slot close failed", exc_info=True)
        raise _NudgeRetireFailed(loop) from exc
    return loop


async def _restore_slot_nudge_loop(loop: "NudgeLoop | None") -> None:
    """Give a session its clock back after a close that failed to persist.

    The close retires the loop before persisting, so a persist that raises would
    otherwise leave the restored session live with nothing driving it — an
    unattended babysit abandoned by a disk error, with no trace but a log line.

    The replacement carries the REMAINING budget, never a fresh one. ``add()``
    mints a new id and a new ``created_ts``, so the spent allowance is subtracted
    here instead: a failed close must not buy unattended cycles the user never
    granted. A loop whose cycle cap or wall-clock budget is already spent is not
    restored at all (it was one tick from terminal), and neither is a paused one
    — reviving that would override an explicit stop.
    """
    if loop is None or not loop.active:
        return
    try:
        from kiro_crew import autonudge  # circular: autonudge -> dashboard.chat -> chat_handlers

        svc = autonudge.get_instance()
        if svc is None:
            return
        cycles_left = loop.max_cycles
        if loop.max_cycles:
            cycles_left = loop.max_cycles - loop.cycle_count
            if cycles_left <= 0:
                return
        runtime_left = loop.max_runtime_secs
        if loop.max_runtime_secs and loop.created_ts:
            if autonudge.runtime_budget_exceeded(loop):
                return
            # >=1: a budget of 0 means UNLIMITED, so a spent-to-the-second
            # remainder must not round into "no budget at all".
            runtime_left = max(1, int(loop.max_runtime_secs - (time.time() - loop.created_ts)))
        await svc.add(
            loop.slot_key,
            loop.message,
            idle_secs=loop.idle_secs,
            max_cycles=cycles_left,
            stop_sentinel_path=loop.stop_sentinel_path,
            max_runtime_secs=runtime_left,
        )
    except Exception:
        # Same wedged disk that failed the persist most likely fails this write
        # too. The 500 already tells the caller the close did not happen; the
        # retired loop is visible as gone in the dashboard, not silently dead.
        logger.warning("autonudge loop restore after failed slot close failed", exc_info=True)


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

    # Synchronous tombstone, BEFORE any await: a channel-slot reconcile pass
    # whose snapshot predates this close reads these after its last await, so
    # it cannot re-surface the tab this handler is dismissing (see
    # channel_slots._RECENT_CLOSES). The returned instant is persisted as
    # closed_at below — the save runs after the cancellation awaits, and
    # stamping save time would make channel activity landing in that window
    # compare as older than the close.
    closed_at = note_slot_closed(state, name)
    # Retire the auto-nudge loop BEFORE the awaits below, so no nudge can expire
    # into the session being closed and resurrect it. See
    # _retire_slot_nudge_loop for why disarming alone does not hold.
    try:
        retired_loop = await _retire_slot_nudge_loop(name)
    except _NudgeRetireFailed as exc:
        # The loop could not be retired, so the close CANNOT proceed: persisting
        # the slot as closed while the registry still lists the loop is what lets
        # the next start rebuild this session and nudge it. Put the in-memory
        # loop back so memory agrees with the armed disk, and report the failure
        # the same way a failed history save does — the tab stays open and driven,
        # which is a state the user can see and retry, unlike a closed tab that
        # quietly wakes up later.
        await _restore_slot_nudge_loop(exc.loop)
        logger.error("Failed to retire nudge loop for slot %s, close aborted", name)
        _sync_dashboard_slots(state)
        state.push_slots_update()
        return web.json_response(
            {"error": "failed to retire nudge loop", "code": "nudge_retire_failed"},
            status=500,
        )
    # Remove from the registry only AFTER the loop is retired, because the ORDER
    # is what decides whether a nudge landing in between is harmless or fatal.
    # Retiring takes the AutoNudge lock, so it awaits; a timer expiring inside
    # that await used to find the slot already gone from `_slots`, and the fire
    # path's response to a missing slot is `rehydrate_slot_from_history_async(...,
    # adopt_closed=True)` — it rebuilds the session and adopts it DESPITE the
    # closed flag (deliberately, so idle-archived workers survive). So popping
    # first turned "the user dismissed this tab" into "the tab comes back".
    #
    # With the loop retired first there is no timer left to fire, so the removal
    # below cannot be undone. A nudge that fires BEFORE the retire begins still
    # runs a turn, but that is the ordinary race with the ✕ click itself and it
    # resurrects nothing.
    # Tell the app BEFORE anything durable happens. For a crew this hook is the
    # write that pauses the worker, so it has to succeed for the dismissal to
    # mean anything — and it must be undoable if it does not. Sequenced here, a
    # failure costs nothing: the slot is still in `_slots`, history still says
    # open, and the only thing to put back is the loop. Sequenced after the
    # persist (where it used to live) there was nothing to abort INTO — the close
    # was already committed, so a lost pause left a live auto-approved crew whose
    # watchdog relaunched the tab, with only a log line to say so.
    #
    # Stopping the worker first is also the right order on its own terms: quiet
    # the thing, then dismantle its surface. The reverse opens exactly the window
    # this hook exists to close.
    #
    # Deliberately NOT in the bulk idle-archive path below: that one closes a slot
    # for quietness, and an app worker stopped by idleness alone is a silent
    # failure. Which call site fires IS the signal.
    if slot._app:
        from kiro_crew.apps.teardown import (
            notify_slot_closed,  # circular: apps.teardown -> apps.bridges -> dashboard
        )

        if not await notify_slot_closed(slot._app, name):
            # The app could not record the dismissal. Refuse the close rather
            # than leave a worker running behind a tab the user believes is gone.
            await _restore_slot_nudge_loop(retired_loop)
            logger.error(
                "Slot-close hook for app %r failed on %r, close aborted", slot._app, name
            )
            _sync_dashboard_slots(state)
            state.push_slots_update()
            return web.json_response(
                {"error": "failed to notify the app", "code": "app_close_hook_failed"},
                status=500,
            )
    state._slots.pop(name, None)
    # Release any blocking wait before cancelling the task: a pending
    # ask_question holds an MCP worker on a blocked HTTP request, and the slot
    # is going away, so nobody will ever answer its card.
    _unblock_pending_waits(state, slot)
    # Cancel any pending speculative session creation. Without this, an
    # eager task mid-debounce or mid-handshake outlives the slot; combined
    # with the task's own post-create liveness re-check this closes both
    # halves of the delete/recreate race.
    _eager = getattr(slot, "_eager_spawn_task", None)
    if _eager is not None and not _eager.done():
        _eager.cancel()
    # A pending resume-prefetch TTL timer is deliberately NOT cancelled here:
    # its removal is conditional (no-ops once the slot is gone or the session
    # was claimed), while a cancel landing mid-removal would interrupt
    # provider.shutdown() after the registry entry was already popped and
    # leak the process holding kiro-cli's native session lock.
    if slot.running and slot.task is not None:
        slot.task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(slot.task), timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    try:
        await save_slot_off_loop(state, slot, closed=True, closed_at=closed_at, best_effort=False)
    except Exception:
        # Save failed — restore slot so data isn't lost
        logger.error("Failed to save slot %s to history, restoring", name, exc_info=True)
        state._slots[name] = slot
        # The close did not happen, so the loop retired for it must come back —
        # a restored session with no clock is an abandoned unattended worker.
        await _restore_slot_nudge_loop(retired_loop)
        # ...and the app's record of the dismissal has to come back too. The
        # notification above already SUCCEEDED, which for a crew means the worker is
        # durably paused; without this the failed close would still have stopped it,
        # so the user gets an error AND a silently disabled worker. Unwound in
        # reverse order of commitment, which is the only arrangement that leaves no
        # pair of the three stores disagreeing.
        if slot._app:
            from kiro_crew.apps.teardown import (
                notify_slot_close_undone,  # circular: apps.teardown -> apps.bridges
            )

            if not await notify_slot_close_undone(slot._app, name):
                logger.error(
                    "Could not take back the dismissal for app %r on %r; it may still "
                    "consider this slot closed",
                    slot._app,
                    name,
                )
        _sync_dashboard_slots(state)
        state.push_slots_update()
        return web.json_response(
            {"error": "failed to save history", "code": "history_save_failed"}, status=500
        )
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
        # Durable, so no rollback can retract this frame — a client pruning its
        # per-slot cards on it can never be pruning a slot that comes back.
        state.push_slots_update()
    # The app was already told, and compensated if the persist above failed — see
    # the notify block before the pop and the rollback in the except branch.
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
    # FIX 3: slots owning an ARMED auto-nudge loop are exempt from idle archival.
    # Archiving one marked it closed, and the nudge fire path then could not
    # reach it and REMOVED the loop — terminally. An unattended worker is idle
    # by nature between cycles (a 6h CI wait looks exactly like abandonment), so
    # the 3-day idle heuristic reliably shot the longest-running loops. Resolved
    # once, outside the per-slot loop, so a large registry costs one pass.
    _looped: set[str] = set()
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_handlers
        )

        _svc = _autonudge_get()
        if _svc is not None:
            for _lp in _svc.list_all():
                if not _lp.active:
                    continue
                _looped.add(_lp.slot_key)
                # A channel-born loop is bound under its channel session key
                # (slack:<ts>) while its tab is named with the folded form
                # (slack_<ts>) — match both or the exemption misses the tab.
                _looped.add(_normalize_slot_key(_lp.slot_key))
    except Exception:
        # Fail CLOSED for the loops: if the registry cannot be read we do not
        # know which slots are protected, so archive nothing this pass rather
        # than risk destroying a loop. Cleanup is a convenience; the loop is not.
        logger.warning("Cleanup: auto-nudge registry unreadable; skipping this pass", exc_info=True)
        return web.json_response(
            {"ok": True, "archived": 0, "keys": [], "failed": [], "skipped": "autonudge_unknown"}
        )
    stale_keys: list[str] = []
    active_is_stale = False
    for name in list(state._slots):
        slot = state._slots.get(name)
        if slot is None or slot.pinned:
            continue
        if name in _looped:
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
        # Cancel BEFORE the flush, mirroring the single-tab close at :3271-3276.
        # The flush promotes a held note's context half into ``_pending_context``,
        # and the save below is an await a still-running turn resumes across: it
        # drains and CLEARS that queue, then is cancelled, so the context reaches
        # nobody. Bounded and shielded; a task outliving the timeout still leaves
        # ``running`` true, so the collect branch below hands it to the one
        # batched wait rather than serialising a hung turn's full teardown here.
        _turn_killed = False
        if removed.running and removed.task is not None:
            removed.task.cancel()
            _turn_killed = True
            try:
                await asyncio.wait_for(asyncio.shield(removed.task), timeout=2.0)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass
        removed.flush_deferred_notes()
        try:
            await save_slot_off_loop(
                state, removed, closed=True, closed_at=closed_at, best_effort=False
            )
        except Exception:
            logger.error("Cleanup: failed to archive slot %s", name, exc_info=True)
            state._slots[name] = removed
            # Restoring the slot does not undo the cancel above, and ``running`` is
            # derived from the task, so a cancel that already completed reads False:
            # the tab returns looking idle and dispatchable with that turn's output
            # silently gone. Report it as an error row instead, and drop the dead
            # task so nothing downstream treats it as this slot's live turn. A task
            # that outlived the shielded wait is still running, so the restore loses
            # nothing there and this stays quiet.
            if _turn_killed and removed.task is not None and removed.task.done():
                removed.task = None
                removed.append(
                    "error",
                    "⚠️ Archiving this tab failed after its running turn was "
                    "cancelled. The tab was kept, but that turn did not finish "
                    "-- re-send to continue.",
                    "msg msg-err",
                )
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
    if not failed:
        cleanup_outcome = "ok"
    elif archived:
        cleanup_outcome = "partial"
    else:
        cleanup_outcome = "error"
    sel().log_api_access(
        caller="dashboard",
        operation="chat.slots_cleanup",
        outcome=cleanup_outcome,
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_agent")
    if denied is not None:
        return denied
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
            # Resolve WITH the slot's project scope (warmed off-loop first) so a
            # project agent counts as resolved rather than falling back.
            await warm_project_agent_names(slot.project or None)
            bindings = resolve_agent_bindings(cfg, agent_name, slot.project or None)
            ws_name = _workspace_name_for_dir(cfg, bindings.workspace_dir)
            slot.workspace = ws_name
            workspace = ws_name
            # A project-scope agent exists only inside slot.project: kiro-cli
            # resolves --agent against $PWD/.kiro/agents, so resetting the
            # project here would make the very agent just selected unresolvable
            # on the next turn (slot advertises it, default answers — the
            # silent-substitution bug #1684 exists to remove). Aliases keep the
            # reset: their project comes from their own workspace bindings.
            is_project_agent = agent_name not in cfg.agents and agent_name in (
                cached_project_agent_names(slot.project or None) or frozenset()
            )
            if not is_project_agent:
                slot.project = default_project_dir(workspace)
    except Exception:
        logger.warning("Failed to resolve agent bindings for %r", agent_name, exc_info=True)

    # Reset session so next message uses the new agent
    logger.info("Slot %s agent switched to %r, resetting session", name, agent_name or "kirocrew")
    await _reset_slot_session(state, slot, _history_key_for(name))
    # The reset destroyed any eagerly created session; picking an agent is
    # itself a strong first-message intent signal (it also resets the
    # project), so re-arm the speculative spawn for the new bindings.
    schedule_eager_spawn(state, slot)
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


def _model_rejected_reason(model_name: str, provider: str | None = None) -> str | None:
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

    *provider* lets a caller that has already loaded the config supply it, so
    this adds no read of its own: ``KiroCrewConfig.load()`` deep-copies the
    validated dict even on a cache hit, and on a miss it reads and validates
    files — work that must not land on the event loop under a held lock. Omit it
    and the provider is resolved here, preserving the original behaviour.
    """
    if not model_name or model_name == "auto":
        return None
    if provider is None:
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_model")
    if denied is not None:
        return denied
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
        return web.json_response({"error": str(exc), "code": "model_unavailable"}, status=400)
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_reasoning_effort")
    if denied is not None:
        return denied
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


async def api_chat_slot_reload(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/reload -- relaunch the slot's agent process.

    A live agent process mounts its MCP servers and builds its tool table once,
    at session-init time; config that changes afterwards (a newly added MCP
    server, an env or agent-spec fix) never reaches it. Reload is the in-place
    remedy: tear the process down exactly like the agent/workspace switch
    handlers do, then eagerly re-arm the resume spawn, so the relaunched
    process re-reads its agent spec and environment and re-initializes MCP
    servers via session/load -- with the conversation preserved.

    Refused with 409 while a turn is in flight (killing an in-flight ACP
    process orphans the streaming prompt: resume refusals, empty responses)
    and while sub-agent children are attached (their shared runtime is torn
    down with the parent session -- see ``SessionManager.reset`` -- so a
    reload under a working child silently discards its work). The
    has_active_turn() check is a best-effort fast path; the authoritative
    guard is the reset's skip_if_busy, which evaluates busyness atomically
    with the session pop (see _reset_slot_session for why the unblock half of
    the chokepoint is safe even when the guard declines).
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response(
            {"error": "not found", "code": "slot_not_found"}, status=404
        )
    # The session the reload will tear down. ``effective_session_key``, never
    # ``_history_key_for``: a channel- or cron-born slot runs its turns under
    # its linked key, and the dashboard-prefixed spelling names a session that
    # never existed -- the reset would "succeed" against nothing while the
    # live process kept its stale config.
    session_key = effective_session_key(slot)
    # App isolation, same policy as the cancel routes: reload is a teardown,
    # so an app token must own both the slot and the session the teardown
    # lands on, and a denial is indistinguishable from a missing slot.
    denied = _app_cancel_denied(request, slot, "chat.slot_reload", session_key)
    if denied is not None:
        return denied
    provider = state.sessions.get_provider(session_key)
    if provider is not None and provider.has_active_turn():
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
        )
    # Children guard, shared with api_chat_slot_continue: RUNNING children die
    # with the parent runtime, and _subagents_attached_response documents why
    # queued children and in-flight deliveries count too.
    denied_409 = _subagents_attached_response(state, slot, session_key, "reload")
    if denied_409 is not None:
        return denied_409
    reloaded = await _reset_slot_session(state, slot, session_key, skip_if_busy=True)
    if not reloaded:
        provider = state.sessions.get_provider(session_key)
        if provider is not None and provider.has_active_turn():
            return web.json_response(
                {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
            )
        if provider is not None:
            # A turn slipped into the guard window and already FINISHED: the
            # declined reset left a live idle session untouched, and falling
            # through would report success while the stale process survives --
            # the silent failure this endpoint exists to prevent. Retry once;
            # a second decline means another turn is genuinely racing, which
            # is the turn-in-flight case.
            reloaded = await _reset_slot_session(
                state, slot, session_key, skip_if_busy=True
            )
            if not reloaded:
                return web.json_response(
                    {"error": "a turn is in flight", "code": "turn_in_flight"},
                    status=409,
                )
    logger.info("Slot %s session reloaded (had_live_session=%s)", name, reloaded)
    # Feed notice: the visible confirmation (and the durable record) that the
    # relaunch happened. Tagged so the last-real-message scans skip it on both
    # sides (is_system_notice here, isSystemNoticeKind on the frontend).
    # append() itself broadcasts the row -- with the per-row ``mid`` identity
    # clients dedupe on -- so an explicit broadcast here would deliver the
    # notice twice.
    slot.append(
        "assistant", _SESSION_RELOAD_NOTICE, "msg msg-a",
        meta={"kind": SESSION_RELOAD_KIND},
    )
    # Respawn + session/load now rather than on the next message, so the fresh
    # process (and its rebuilt toolset) is ready when the user comes back.
    schedule_eager_spawn(state, slot, allow_resume=True)
    state.push_slots_update()
    return web.json_response({"ok": True})


async def api_chat_slot_workspace(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/workspace — set workspace for a chat slot."""
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return web.json_response({"error": "not found"}, status=404)
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_workspace")
    if denied is not None:
        return denied
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
    denied = _deny_cross_app_slot_access(request, slot, name, "slot_project")
    if denied is not None:
        return denied
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
        # Speculatively re-create the session rooted at the new project so the
        # cwd change is paid during think-time. The eager task consumes the
        # deferred reset itself, but only when no turn is running — the
        # same killpg constraint that deferred the reset applies to it.
        schedule_eager_spawn(state, slot)
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


def _deny_cross_app_slot_access(
    request: web.Request, slot, name: str, operation: str
) -> web.Response | None:
    """Deny app tokens acting on slots they don't own (App Kit §5.2).

    Returns a 404 response if the caller is an app that doesn't own this slot,
    or None to proceed. Dashboard users (empty request_app) always pass.
    Anti-enumeration: uses 404 not 403 (CWE-204).
    """
    request_app = request.get("app", "")
    if not request_app:
        return None  # Dashboard user -- no restriction
    if slot._app and request_app == slot._app:
        return None  # App owns this slot
    reason = (
        "app does not own this slot" if slot._app else "app cannot access unscoped slots"
    )
    try:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error=reason,
        )
    except Exception:
        pass
    return web.json_response({"error": "not found", "code": "slot_not_found"}, status=404)


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
    from kiro_crew.dashboard.handlers.source_providers import (
        is_owner_dashboard_request,
        stale_owner_session_response,
    )

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
        # Deny decision made above; only the response label changes for a
        # signed pre-owner bootstrap subject (see stale_owner_session_response).
        stale = stale_owner_session_response(request)
        if stale is not None:
            return stale
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


async def _reconcile_slot_window(
    state: DashboardState, slot: "_ChatSlot"
) -> None:
    """Detect and reconcile stale in-memory window from disk.

    A live slot's window can fall behind disk when messages are written to the
    session file by a path that does not (or cannot) also push into the
    in-memory window — e.g. a concurrent subagent flush, a channel-origin
    append, or a persistence race during heavy traffic.

    This function compares the slot's believed disk coverage
    (``_disk_older_count + len(messages)``) against the actual on-disk message
    count. If disk has grown beyond what the slot accounts for, the missing
    tail is read and appended to the in-memory window, making the next detail
    or resume response self-healing on refresh.

    Safety: skips reconciliation when the slot has unflushed in-memory rows
    beyond what the last save persisted (``len(messages) > _disk_window_len``),
    because a concurrent flush could persist those rows between the
    ``represented`` snapshot and the disk read, leading to a duplicate
    append. Re-validates after the await to guard against appends that landed
    during the disk read.
    """
    if not state.conversation_log:
        return
    # Safety gate: do not reconcile a slot that has in-memory rows the last
    # flush has not yet persisted, or that is mid-rewind, or that has unsaved
    # in-place edits (dirty) — clearing dirty at the end would erase the edit.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    history_key = slot_history_key(slot)
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    try:
        disk_msgs = await asyncio.to_thread(
            state.conversation_log.read_messages_chained, history_key
        )
    except Exception:
        logger.warning(
            "reconcile: read_messages_chained failed for %s", history_key, exc_info=True
        )
        return
    disk_total = len(disk_msgs)
    if disk_total <= represented:
        return
    # Post-await safety: the slot may have received appends (and a flush) while
    # we were reading disk. Re-check and recompute represented to avoid
    # duplicating rows that arrived during the await.
    if len(slot.messages) > getattr(slot, "_disk_window_len", 0):
        return
    if getattr(slot, "_pending_rewrite", False):
        return
    if getattr(slot, "_dirty_flag", False):
        return
    represented = (getattr(slot, "_disk_older_count", 0) or 0) + len(slot.messages)
    if disk_total <= represented:
        return
    # Validate alignment: if transcript rotation shifted offsets, the disk
    # prefix no longer matches memory — abort to avoid appending wrong rows.
    # The slot's window starts at disk offset _disk_older_count, so we compare
    # the last memory row against its expected position on disk.
    disk_older = getattr(slot, "_disk_older_count", 0) or 0
    if slot.messages and (disk_older + len(slot.messages)) <= len(disk_msgs):
        last_mem = slot.messages[-1]
        expected_pos = disk_older + len(slot.messages) - 1
        disk_at = disk_msgs[expected_pos]
        if (
            last_mem.get("ts", "") != disk_at.get("ts", "")
            or last_mem.get("role") != disk_at.get("role")
        ):
            logger.info(
                "reconcile: slot %s alignment mismatch at offset %d — skipping "
                "(possible transcript rotation)",
                slot.key,
                expected_pos,
            )
            return
    # Disk has rows the slot does not know about — append the tail.
    fresh = disk_msgs[represented:]
    logger.info(
        "reconcile: slot %s has %d messages in memory + %d older on disk = %d represented, "
        "but disk has %d; appending %d missing rows",
        slot.key,
        len(slot.messages),
        getattr(slot, "_disk_older_count", 0) or 0,
        represented,
        disk_total,
        len(fresh),
    )
    for msg in fresh:
        role = msg.get("role", "assistant")
        cls = msg.get("cls") or ("msg msg-u" if role == "user" else "msg msg-a")
        content = msg.get("content", "")
        if role != "user":
            content, _ = redact_exfiltration_urls(content)
            content, _ = redact_credentials(content)
        slot.append(
            role,
            content,
            cls,
            ts=msg.get("ts", ""),
            broadcast=False,
            meta=(
                _redact_meta_for_role(role, msg["meta"])
                if isinstance(msg.get("meta"), dict)
                else None
            ),
        )
        carry_provenance(slot.messages[-1], msg)
        _attach_variants(slot, msg)
    # The appended rows came from the file, so drain the replay frames and
    # mark the window as persisted (not dirty) — the next save must not
    # re-serialize them, and a fork/SSE drain must not treat them as new.
    slot.drain()
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False


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


async def _live_slot_resume_response(
    state, request: web.Request, history_key: str, name: str
) -> web.Response | None:
    """Answer a resume that a live slot already satisfies, else return None.

    Returns 404 when the caller's app does not own the slot, otherwise the
    dedup early-return. Called on BOTH sides of the threaded transcript read:
    that await lets a concurrent resume publish the slot in between, and
    ``get_or_create_slot`` would then hand it back having never applied this
    ownership gate for the second caller's app.
    """
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
        # Reconcile: if disk grew beyond what the in-memory window covers,
        # append the missing tail so a page refresh self-heals (#4373).
        await _reconcile_slot_window(state, existing)
        # Reduce the wire-only rows before bounding, for the same reason the
        # detail handler does: a segment still streaming is hundreds of `chunk`
        # rows that render as one message, so a raw 200-row bound over the live
        # window can be filled entirely by one unfinished reply -- and it then
        # returns only that window's slice of the reply, dropping the text
        # ahead of it. Reducing first makes the bound, `total` and the cursor
        # below all count displayed messages.
        #
        # It also puts the cursor's two terms in the same unit: persisted rows
        # carry no wire-only role, so `_disk_older_count` is already a message
        # count, while a raw window length is not.
        #
        # O(window) on the event loop, and the window is capped -- the
        # `_prepare_messages` redaction pass on the next line is the larger
        # cost at this call site either way.
        window = _collapse_wire_rows(existing.messages)
        total = len(window)
        recent = window[-200:] if total > 200 else window
        prepared = _prepare_messages(recent, existing.running)
        # Raw index this window starts at: the frozen on-disk prefix plus the
        # in-memory rows it skipped. has_more is derived from the same number so
        # the flag cannot contradict the cursor -- counting only the in-memory
        # window said "no more" for a slot with a prefix, and the client drops a
        # cursor it was told not to use.
        next_before = (getattr(existing, "_disk_older_count", 0) or 0) + (total - len(recent))
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
                "has_more": next_before > 0,
                "next_before": next_before,
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
    return None


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
    resume_resp = await _live_slot_resume_response(state, request, history_key, name)
    if resume_resp is not None:
        return resume_resp

    # Boundary for the compare-and-clear below, captured BEFORE the metadata read
    # it is compared against. Everything from here to the ``clear_closed`` call is
    # a window in which this session can be closed by somebody else -- including
    # deleted, recreated and closed again -- and the clear must not erase a
    # ``closed`` that landed inside it. Anchored to this read specifically, since
    # ``meta["closed"]`` is the snapshot the clear acts on.
    resume_started_at = time.time()
    # Read the history metadata BEFORE creating the slot: this endpoint RESUMES a
    # persisted conversation, so its origin is a property of that conversation,
    # not of whoever is resuming it. Deriving it from the request would label a
    # resumed CRON slot as USER, and `slots:user` would then hand the dashboard
    # user's private cron output to any app holding that scope. An absent
    # persisted origin stays empty (get_or_create_slot then derives APP for an
    # app token, otherwise leaves it untagged, which is invisible to cross-slot
    # scopes) rather than claiming USER on a conversation we cannot attribute.
    meta = state.conversation_log.get_metadata(history_key)

    # Read the transcript BEFORE publishing the slot: this await would otherwise
    # expose an empty slot by name, and a concurrent append would land ahead of it.
    all_messages = await asyncio.to_thread(
        state.conversation_log.read_messages_chained, history_key
    )

    # Every remaining await in this handler runs BEFORE the slot is published: one
    # after it would expose an empty slot, and a concurrent append there is ordered
    # ahead of the history the hydrate loop restores further down. They are placed
    # ahead of the re-check too, so nothing can suspend between it and the publish.
    folder_unhidden = True
    # Record WHICH folder that verdict is about. Hoisting this call above the
    # publish is what keeps the window closed, but it also moved it onto the
    # PRE-read ``meta``, while the hydrate below binds ``folder_id`` from the
    # snapshot re-read after the last await. A channel reconciliation landing
    # during the transcript read makes those two ids differ, and an existence
    # verdict earned by the OLD folder says nothing about the NEW one.
    folder_checked_id = ""
    if meta.get("folder_id"):
        folder_checked_id = meta["folder_id"]
        folder_unhidden = await _unhide_folder(state, folder_checked_id)
    if meta.get("closed"):
        # Clear the closed flag so the session restores on the next gateway restart.
        # Offloaded because clear_closed takes the per-session cross-process lock,
        # which fails fast on the loop under contention. Best-effort: resume anyway.
        #
        # COMPARE-AND-CLEAR, not an unconditional clear. We are acting on the
        # ``meta`` snapshot above, and by the time this call takes the lock the
        # session may have been closed again by someone else -- or deleted,
        # recreated and closed, in which case the flag we would drop belongs to a
        # DIFFERENT conversation that the identity re-check below is about to
        # refuse with a 409. Clearing it anyway reopens a replacement the user
        # closed. ``only_if_closed_before`` moves the comparison inside the store's
        # own lock, so there is no window between the check and the write; a close
        # instant at or after our boundary leaves the flag standing.
        try:
            await asyncio.to_thread(
                state.conversation_log.clear_closed,
                history_key,
                only_if_closed_before=resume_started_at,
            )
        except Exception:
            logger.warning("Failed to clear closed flag for %s", history_key, exc_info=True)

    # Re-check after the await: a concurrent resume can publish the slot while we
    # are suspended, and the publish below would skip the ownership gate above.
    resume_resp = await _live_slot_resume_response(state, request, history_key, name)
    if resume_resp is not None:
        return resume_resp

    # Re-check DELETION in the same window and for the same reason. The transcript
    # loaded above can be permanently deleted while we are suspended, and
    # ``delete_session`` leaves NO tombstone -- its own docstring notes that once
    # the delete releases the lock "a concurrent writer can recreate the session".
    # So publishing a slot from content we already hold rewrites, on its next
    # flush, a file the user permanently deleted.
    #
    # ``get_metadata_status``, never ``get_metadata``: the latter returns ``{}`` for
    # BOTH "deleted" and "unreadable", and reading an unreadable metadata line as a
    # deletion would discard a LIVE session -- its docstring says to prefer this
    # wherever an empty result triggers something destructive.
    #
    # Synchronous, like the ``get_metadata`` above it, so this adds no suspension
    # point between the re-checks and the publish -- the property the comment on
    # the awaits above depends on.
    post_read_meta, meta_readable = state.conversation_log.get_metadata_status(history_key)
    # Did this session exist when we looked? Both re-checks below need that, and
    # ``all_messages`` alone is the wrong witness: a METADATA-ONLY session -- a
    # metadata line with no messages, which ``update_metadata`` creates on upsert --
    # has an empty transcript, so gating on it silently disabled both guards for
    # exactly the sessions least able to survive it. The pre-read ``meta`` is the
    # right witness, and it costs nothing: it is already read synchronously above,
    # so consulting it adds no suspension point.
    #
    # A UNION rather than a swap, so the witness is never narrower than it was: a
    # transcript we managed to read is also evidence of prior existence, even where
    # the metadata line was unreadable at pre-read time and ``meta`` came back empty.
    #
    # This is ONE term used by BOTH arms deliberately. They previously carried the
    # same predicate separately, which is how the empty-transcript hole reached two
    # sites at once; a single binding means a future change cannot fix one and leave
    # the other behind.
    session_existed = bool(meta or all_messages)
    # Resuming a session that never existed stays untouched: no metadata and no
    # transcript leaves this false, so an absent key is treated as a new
    # conversation rather than a deletion. A legitimately empty session that is
    # still PRESENT is protected by the other terms instead -- ``post_read_meta``
    # is non-empty below, and the identity arm needs two DIFFERING stamps.
    if meta_readable and not post_read_meta and session_existed:
        logger.info(
            "chat resume: session %s was deleted during the transcript read; "
            "refusing to publish a slot that would resurrect it",
            history_key,
        )
        return web.json_response(
            {
                "error": "the session was deleted while it was being resumed",
                "code": "resume_session_deleted",
            },
            status=409,
        )
    # IDENTITY, not merely existence. The arm above fires on metadata being
    # ABSENT, which the delete-then-RECREATE interleaving does not produce: the
    # delete leaves no tombstone, so a writer that recreates the session inside
    # this same window leaves ``post_read_meta`` a NON-EMPTY dict belonging to the
    # NEW conversation. Existence reads that as "still here" and publishes a slot
    # holding the OLD transcript, whose next flush overwrites a session the user
    # is actively using -- the opposite error to the one above, and worse, because
    # the data destroyed is live rather than already-deleted.
    #
    # ``created_at`` is the discriminator because every path that MINTS a metadata
    # line stamps it (``append`` when the file does not exist,
    # ``_update_metadata_locked`` when the line is missing) while
    # ``_rewrite_session_locked`` carries it through verbatim. So a rewrite,
    # compaction or rename does NOT move it and is not refused here; a differing
    # value means this is a different file than the one we read.
    #
    # ABSENT on either side means we cannot compare, and we FALL THROUGH rather
    # than refuse. Refusing would reject legitimate resumes of any transcript
    # whose metadata predates the field -- a visible break for real users -- to
    # close a narrow race. It also neuters the one false positive available here:
    # ``_rewrite_session_locked`` mints a fresh ``created_at`` only when the
    # original lacked one, which is exactly the case this skips. The residual is
    # that a recreate of such a transcript stays undetected; the durable fix for
    # that is a tombstone in ``history.delete_session``, which is out of scope.
    pre_identity = meta.get("created_at")
    post_identity = post_read_meta.get("created_at")
    if (
        meta_readable
        and post_read_meta
        and session_existed
        and pre_identity
        and post_identity
        and pre_identity != post_identity
    ):
        logger.info(
            "chat resume: session %s was deleted and recreated during the "
            "transcript read; refusing to publish a slot whose flush would "
            "overwrite the replacement",
            history_key,
        )
        # Same code as the plain-delete arm: from the resumer's point of view the
        # session it asked for was deleted. That it was then recreated does not
        # change what happened to the conversation being resumed, and one code
        # keeps the client contract single-valued.
        return web.json_response(
            {
                "error": "the session was deleted while it was being resumed",
                "code": "resume_session_deleted",
            },
            status=409,
        )

    slot = state.get_or_create_slot(
        name,
        app=request.get("app", ""),
        # Resuming an existing channel transcript from History is an adoption of
        # that conversation, so the tab is channel-origin even when the session
        # map can no longer name its session.
        channel_origin=is_channel_session_key(history_key),
        origin=str(meta.get("origin", "")),
    )
    # PERSISTED METADATA IS AUTHORITATIVE for the title. The sidebar's resume
    # call always sends a ``title`` (see website/src/api/client.ts
    # resumeChatSlot: ``title: title || key``), and that value is client
    # chrome — often a STALE echo of an older name (a notification deep link,
    # a sidebar row rendered before a background refresh landed). Classifying
    # request titles (echo vs override) is unwinnable against staleness: a
    # stale echo is indistinguishable from a deliberate override. So the
    # request title is used ONLY when no persisted title exists; otherwise the
    # persisted title and its provenance are restored exactly like the
    # chat_persistence loaders (resume is the THIRD hydration path).
    meta = state.conversation_log.get_metadata(history_key)
    raw_persisted_title = meta.get("title")
    # Accept the persisted title only when it is a string: a legacy or
    # hand-corrupted JSONL could carry a non-string here, and redacting it
    # would raise TypeError and 500 the resume. Non-string == absent.
    persisted_title = raw_persisted_title if isinstance(raw_persisted_title, str) else ""
    title = body.get("title", "")
    if persisted_title:
        _rehydrate_slot_title(
            slot,
            persisted_title,
            titled=True,
            metadata=meta,
        )
    elif title:
        # Never-titled session with a caller-supplied name: apply it, with
        # conservative "user" provenance (unknown origin — the background
        # refresh must never rewrite it) and an epoch bump so any in-flight
        # background attempt stands down.
        slot.title = title
        slot._titled = True
        slot._title_origin = "user"
        slot._title_epoch += 1
    # else: untitled on disk and no caller name — leave the slot untitled
    # (mirrors _rehydrate_slot_from_history: ``_titled = bool(meta title)``),
    # so the auto-titler can still name it on the next turn.
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
    if meta.get("channel_folder_filed"):
        # Resuming from History must carry the filing marker forward, or the
        # next save of this slot drops it and the conversation is re-filed.
        slot._channel_folder_filed = True
    if meta.get("folder_id"):
        slot.folder_id = meta["folder_id"]
        # Re-engaging a hidden empty folder (Model B) un-hides it so it stays
        # visible until the user hides it again. A folder deleted since this
        # session was last saved leaves the stored id dangling; drop it so the
        # resumed session is plainly unfiled instead of pointing at nothing.
        #
        # Only when the verdict is ABOUT this folder. ``_unhide_folder`` reports
        # existence from inside the folder-store lock precisely because a check
        # made outside it can go stale, so re-deriving one here against
        # ``state._folders`` is the race its own docstring warns about; and it
        # cannot simply be re-run, because a second await here would reopen the
        # publish-to-hydrate window this ordering exists to close. Holding no
        # verdict for a newly filed id, we KEEP it: a dangling id is visible and
        # self-corrects on the next folder operation, whereas erasing a live
        # filing is silent and indistinguishable from the user unfiling the
        # session -- and the dirty-slot flush would then persist that erasure.
        if not folder_unhidden and meta["folder_id"] == folder_checked_id:
            slot.folder_id = ""
    if meta.get("pinned"):
        slot.pinned = True
    if meta.get("color_index") is not None:
        slot.color_index = meta["color_index"]
    _ch = meta.get("color_hex")
    if isinstance(_ch, str) and COLOR_HEX_RE.match(_ch):
        slot.color_hex = _ch.lower()
    if meta.get("color_theme"):
        slot.color_theme = meta["color_theme"]
        slot.theme_consent = meta.get("theme_consent") is True
        # Restore from history metadata: re-run the same fail-closed normalizer
        # so a tampered/legacy JSONL can't seed a malformed sha that later
        # crashes the compare.
        slot.theme_consent_sha = normalize_theme_consent_sha(meta.get("theme_consent_sha"))
    # Restore tags + the auto-tag once-flag (mirrors the persistence loaders).
    # Without the flag, resuming a session whose auto-tag the user removed
    # would re-run maybe_auto_tag on the next message and silently re-add it.
    raw_tags = meta.get("tags")
    if isinstance(raw_tags, list):
        slot.tags = [str(t) for t in raw_tags if isinstance(t, str) and t]
        # Prune ids missing from the vocabulary (crash-atomic delete leaves
        # dangling ids on disk; see api_chat_tag_delete). FAIL-OPEN only when
        # the vocabulary is UNKNOWN (tags.json parse/I/O failure) — pruning
        # then would wipe every assignment. A legitimately-empty vocabulary
        # is authoritative and must prune dangling ids.
        if getattr(state, "_tags_authoritative", True):
            known = {t.get("id") for t in state._tags}
            slot.tags = [t for t in slot.tags if t in known]
    if meta.get("auto_tagged"):
        slot._auto_tagged = True
    mm = meta.get("memory_mode", "persistent")
    slot.memory_mode = mm
    if mm != "persistent":
        state._restricted_keys.add(f"dashboard:{name}")
    else:
        state._restricted_keys.discard(f"dashboard:{name}")
    if meta.get("forked_from") is not None:
        slot.forked_from = meta["forked_from"]
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
            # `total` is the full on-disk length here, so this already is the
            # raw index the next older page starts from.
            "next_before": total - len(recent),
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
    denied = deny_non_dashboard_caller(request, "chat_mode")
    if denied is not None:
        return denied
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    mode = body.get("mode", "normal")
    slot_key = body.get("slot") or None

    # The safety override (YOLO) is PROCESS-GLOBAL while an approval mode is
    # per-slot, so revoking it on behalf of a request that named ONE slot drops
    # every OTHER slot out of YOLO too. That is how a programmatic per-slot
    # `trust` — the call an automation makes when it creates a session — silently
    # ends an operator's live grant minutes after they enabled it.
    #
    # A slot-scoped `trust`/`trust_reads` therefore leaves the grant alone: it
    # asks for auto-approval on one slot and cannot be answered by withdrawing
    # authority elsewhere. Everything else still revokes, so `normal` remains the
    # off-switch at any scope and the dashboard picker (which always names its own
    # slot) keeps working.
    #
    # A grant DECLARED in owner-only config is exempt from the narrowing: it has
    # no TTL, and selecting another approval mode is the one action documented to
    # end it. Identity is the grant's source, never its permanence — an
    # `until_shutdown` ad-hoc pick is equally permanent and must stay protected.
    slot_scoped_trust = slot_key is not None and mode in _SLOT_SCOPED_TRUST_MODES
    if mode != "yolo" and (not slot_scoped_trust or safety_override().is_declared):
        safety_override().deactivate("dashboard")

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
                    # ``slot`` keys the frame for the slot-scoped WS gate — an
                    # app token cannot receive its own resolution without it.
                    state.broadcast_ws(
                        "approval_resolved",
                        {"id": aid, "approved": True, "slot": slot.key},
                    )
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
    denied = deny_non_dashboard_caller(request, "chat_slot_approve")
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
            "approval_resolved",
            {
                "id": request_id,
                "approved": resolved != "rejected",
                # Keys the frame for the slot-scoped WS gate (see
                # ws_event_scope._SLOT_SCOPED_EVENTS).
                "slot": owner.key,
            },
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
    """PATCH /api/chat/slots/{slot}/color — set session color.

    Accepts ``color_index`` (int 0..MAX_COLOR_INDEX or null, resolved
    client-side against the viewer's generated palette) and/or ``color_hex``
    (``#rrggbb`` or null, a theme-independent custom color). The two are
    mutually exclusive: setting a non-null value for one clears the other, so
    a slot can never carry both and clients need no precedence rule. Keys are
    ``in body``-gated so an old client sending only ``color_index`` cannot
    silently null an existing hex.
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
    has_ci = "color_index" in body
    has_ch = "color_hex" in body
    ci = body.get("color_index")
    if ci is not None and (
        isinstance(ci, bool) or not isinstance(ci, int) or ci < 0 or ci > MAX_COLOR_INDEX
    ):
        return web.json_response(
            {"error": f"color_index must be a non-negative integer <= {MAX_COLOR_INDEX} or null"},
            status=400,
        )
    ch = body.get("color_hex")
    if ch is not None and (not isinstance(ch, str) or not COLOR_HEX_RE.match(ch)):
        return web.json_response(
            {"error": "color_hex must be #RRGGBB or null", "code": "invalid_color_hex"},
            status=400,
        )
    if has_ci:
        slot.color_index = ci
        if ci is not None:
            slot.color_hex = None
    if has_ch:
        slot.color_hex = ch.lower() if isinstance(ch, str) else None
        if ch is not None:
            slot.color_index = None
    slot._dirty = True
    state.push_slots_update()
    return web.json_response(
        {"ok": True, "color_index": slot.color_index, "color_hex": slot.color_hex}
    )


_MAX_CONTEXT_PER_SOURCE = 10
_MAX_CONTEXT_CONTENT = 40000
# Default expiry for a note's context half: if the user never sends a follow-up
# within 24h, the stale entry is dropped at drain rather than attaching itself to
# some far-future unrelated message. The visible transcript line has no maxAge.
_NOTE_CONTEXT_MAX_AGE = 86400
# Bounds the visible lines a caller can park on one in-flight turn. Matches the
# per-source context cap so neither half of /note outlives the other by much.
_MAX_DEFERRED_NOTES = 10

# Distinguishes "key absent" from an explicit JSON null, which `body.get("maxAge")`
# alone cannot: both yield None, so the two cannot mean different things without it.
_UNSET = object()

# Source label bounds. The label is interpolated into the
# ``[Background context from "{source}"]`` prompt frame at drain, so disallow
# control chars and newlines to keep a crafted label from breaking out of the
# frame line, and cap the length. Defense-in-depth: the real free-form surface
# is ``content``, not ``source``.
_MAX_SOURCE_LEN = 64
_SOURCE_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _validate_content(content: object) -> web.Response | None:
    """Shared content validation for /context and /note.

    Validating at the request boundary in ONE place is what keeps the two entry
    points from drifting. Returns a 400 response on a bad value, else None.
    """
    if not isinstance(content, str):
        return web.json_response(
            {"error": "content must be a string", "code": "invalid_content"},
            status=400,
        )
    if not content:
        return web.json_response(
            {"error": "content is required", "code": "empty_content"},
            status=400,
        )
    if len(content) > _MAX_CONTEXT_CONTENT:
        return web.json_response(
            {
                "error": f"content exceeds {_MAX_CONTEXT_CONTENT} char limit",
                "code": "content_too_long",
            },
            status=400,
        )
    return None


def _normalize_source(source: object) -> str:
    """Trim a caller source to its stored form: a stripped str.

    Non-str / None / blank collapse to ``""`` and the caller then applies its own
    default. Shared by validation and the /note default so a whitespace-only
    label cannot produce a blank drain frame, and so a padded label shares one
    per-source cap bucket with its trimmed form.
    """
    if not isinstance(source, str):
        return ""
    return source.strip()


def _validate_source(source: object) -> web.Response | None:
    """Shared source-label validation.

    Returns a 400 response on a bad value, else None. An empty, absent, or
    whitespace-only source is allowed here; the caller defaults it.
    """
    if source is not None and not isinstance(source, str):
        return web.json_response(
            {"error": "source must be a string", "code": "source_not_a_string"},
            status=400,
        )
    # Checked BEFORE the strip, which would otherwise silently drop a leading or
    # trailing tab/newline the documented contract says is a 400.
    if isinstance(source, str) and _SOURCE_CTRL_RE.search(source):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    normalized = _normalize_source(source)
    if normalized == "":
        return None
    if len(normalized) > _MAX_SOURCE_LEN:
        return web.json_response(
            {"error": f"source exceeds {_MAX_SOURCE_LEN} char limit", "code": "source_too_long"},
            status=400,
        )
    if _SOURCE_CTRL_RE.search(normalized):
        return web.json_response(
            {
                "error": "source must not contain control characters or newlines",
                "code": "invalid_source",
            },
            status=400,
        )
    return None


def _validate_max_age(max_age: object) -> web.Response | None:
    """Shared maxAge validation. Returns a 400 response on a bad value, else None.

    ``drain_pending_context`` computes ``injected_at + max_age``, so a
    non-numeric value raises a TypeError on the user's NEXT send -- far from the
    request that introduced it. Rejecting it here turns that into a 400 at the
    boundary. Both callers validate UNCONDITIONALLY, not only when an entry is
    actually enqueued, so a visible-only note with a malformed maxAge is a 400
    rather than a silent ignore.

    ``bool`` is rejected because ``isinstance(True, int)`` is True but a boolean
    TTL is a caller bug. ``None`` is allowed, and both callers reach it from an
    omitted key as well as an explicit null -- they tell those apart themselves.
    """
    if max_age is None:
        return None
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
        return web.json_response(
            {"error": "maxAge must be a number (seconds) or omitted", "code": "invalid_max_age"},
            status=400,
        )
    # NaN and Infinity are floats that slip past the <= 0 check (NaN <= 0 is
    # False) and then make injected_at + max_age non-comparable at drain, so the
    # entry would never expire. Reject them at the boundary.
    # An arbitrary-precision int passes the isinstance check above, then
    # OverflowErrors inside isfinite's float conversion — same 400, not a 500.
    try:
        finite = math.isfinite(max_age)
    except OverflowError:
        finite = False
    if not finite:
        return web.json_response(
            {"error": "maxAge must be a finite number", "code": "non_finite_number"},
            status=400,
        )
    if max_age <= 0:
        return web.json_response(
            {"error": "maxAge must be positive", "code": "value_out_of_range"},
            status=400,
        )
    return None


def _check_slot_app_ownership(
    slot: _ChatSlot, name: str, request_app: str, operation: str
) -> web.Response | None:
    """App ownership gate (App Kit §5.2). Returns a 404 response if denied, else None.

    Apps can only touch slots they own; dashboard users (empty ``request_app``)
    can touch everything. The denial is a 404 rather than a 403 so a non-owning
    app token cannot use the status code to probe which slots exist -- the SEL
    event still records the real reason.

    Owning the slot is not sufficient, because it does not imply owning the
    session the write lands on. ``get_or_create_slot`` sets ``_app`` from its
    caller and, for a name shaped like a channel session stem, resolves
    ``linked_session_key`` from the session map in the same call -- so an app
    that names a live channel thread ends up owning a slot bound to a
    conversation it has no claim on. Both callers of
    this gate write into that session: the visible row lands in the channel's
    own transcript and the queued half drains into its next turn. Ownership
    alone would turn a slot binding into capability escalation, which is the
    same second condition ``_app_cancel_denied`` already applies to /stop.

    That session check cannot fire for an UNBOUND channel slot, and the write
    does not follow the session there. When ``surface_channel_session`` cannot
    resolve a thread's key it surfaces the slot with ``linked_session_key``
    empty and ``channel_origin`` set, so ``effective_session_key`` falls back to
    ``_history_key_for`` and the condition above compares a value against
    itself. ``slot_history_key`` does not: it resolves a ``channel_origin`` slot
    through ``slot_transcript_key``, onto the channel's own transcript. So the
    visible row lands in a foreign conversation while the slot's session
    identity stays local and every session-shaped check passes. The third
    condition therefore tests the TRANSCRIPT key -- the thing the write actually
    addresses -- which is the discipline ``_app_cancel_denied`` states at
    :2299-2301: authorize the key the caller will really act on, so
    authorization and action cannot disagree.

    The denials are single-sourced through :func:`_slot_not_found` so the four
    cannot drift apart -- byte-identity is the property being defended.
    """
    if not request_app:
        return None
    if not slot._app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app cannot access unscoped slots",
        )
        return _slot_not_found()
    if request_app != slot._app:
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own this slot",
        )
        return _slot_not_found()
    if effective_session_key(slot) != _history_key_for(slot.key):
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own the session this slot is linked to",
        )
        return _slot_not_found()
    if slot_history_key(slot) != _history_key_for(slot.key):
        sel().log_api_access(
            caller=request_app,
            operation=operation,
            outcome="denied",
            source="app_isolation",
            resources=f"slot={name}",
            error="app does not own the transcript this slot writes to",
        )
        return _slot_not_found()
    return None


def _reauthorize_after_await(
    state: DashboardState, slot: _ChatSlot, name: str, request_app: str, operation: str
) -> web.Response | None:
    """Re-authorize *slot* after an await, immediately before touching it.

    The ownership gate necessarily runs before the request body is read, and
    that ``await`` is a window rather than a formality: ``linked_session_key``
    is rebound on ALREADY-LIVE slots with no ``running`` gate -- a cron
    completion (``cron_inject.py:96``), a workflow injection
    (``workflow_inject.py:156``) -- so a slow caller can be authorized against
    its own session and land on somebody else's conversation. The same identity
    check ``_app_cancel_denied`` makes for /stop, moved to the point of use.

    Requires the same slot OBJECT, not just the same name: a delete and
    re-create under one name would pass an ownership re-check while being a
    different conversation. Callers must run this before the first read of slot
    state too, since ``running`` and the hold queue belong to whichever
    conversation the slot now routes to.
    """
    if state._slots.get(name) is not slot:
        if request_app:
            sel().log_api_access(
                caller=request_app,
                operation=operation,
                outcome="denied",
                source="app_isolation",
                resources=f"slot={name}",
                error="slot was replaced while the request body was read",
            )
        return _slot_not_found()
    return _check_slot_app_ownership(slot, name, request_app, operation)


def _source_cap_reached(slot: _ChatSlot, source: str) -> bool:
    """True if ``source`` already holds the max pending context entries.

    An empty source is uncapped (it shares no bucket). Shared by
    ``_enqueue_pending_context`` and the /note handler, which uses it to keep the
    visible transcript line independent of the context-queue cap.

    Expired entries do not count. They are dropped by ``drain_pending_context``
    but stay in the list until the next drain, so counting them would let ten
    already-dead notes lock a source out of fresh context indefinitely -- and the
    caller is told nothing, because the note still returns 200 with
    ``contextSkipped``. The same predicate decides both, so a count and a drain
    cannot disagree about which entries are live.

    Entries HELD for the deferred-note flush count as well. They are not in the
    queue yet, so a cap that read the queue alone admitted every one of them:
    ten same-source notes posted during one turn each saw a clear cap, and the
    flush then promoted all ten at once, past the per-source ceiling and into
    the FIFO eviction that drops other sources' context.
    """
    if not source:
        return False
    now = time.time()
    held = [n["context"] for n in slot._deferred_notes if n.get("context") is not None]
    pending = sum(
        1
        for e in (*slot._pending_context, *held)
        if e.get("source") == source and not context_entry_expired(e, now)
    )
    return pending >= _MAX_CONTEXT_PER_SOURCE


def _enqueue_pending_context(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> web.Response | None:
    """Build, cap, and append a ``_pending_context`` entry.

    Returns a 4xx response on a bad request (429 per-source cap, 400 invalid
    ``max_age``) WITHOUT mutating the queue, else None on success. The entry is
    consumed on the next user-initiated message via ``drain_pending_context``.

    ``max_age`` is the resolved seconds-to-live, or None for no expiry. HTTP
    callers already validate it via ``_validate_max_age``; the same guard runs
    again here so a direct (non-HTTP) caller cannot slip a non-numeric TTL
    through to the drain.

    """
    entry, err = _build_pending_context_entry(slot, content, source, ephemeral, max_age)
    if err is not None:
        return err
    assert entry is not None
    slot.append_pending_context(entry)
    return None


def _build_pending_context_entry(
    slot: _ChatSlot,
    content: str,
    source: str,
    ephemeral: bool,
    max_age: int | float | None,
) -> tuple[dict[str, object] | None, web.Response | None]:
    """Validate and build one context entry WITHOUT touching the queue.

    Returns ``(entry, None)`` or ``(None, 4xx response)``. Split from the append
    so /note can run every rejection synchronously -- the caller still gets its
    400 or 429 on the POST -- while HOLDING the entry until the running turn
    ends. Queueing it at the POST instead would hand it to the turn already in
    flight, since that turn drains the queue after its task is assigned.
    """
    bad_age = _validate_max_age(max_age)
    if bad_age is not None:
        return None, bad_age
    if _source_cap_reached(slot, source):
        return None, web.json_response(
            {
                "error": f"source {source!r} has {_MAX_CONTEXT_PER_SOURCE} pending entries",
                "code": "capacity_reached",
            },
            status=429,
        )
    entry: dict[str, object] = {
        "content": content,
        "source": source,
        "ephemeral": ephemeral,
        "injectedAt": time.time(),
    }
    if max_age is not None:
        entry["maxAge"] = max_age
    return entry, None


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
        # Same body as the ownership denial below: two shapes would let an app
        # token tell "not mine" from "does not exist" and enumerate slot names.
        return _slot_not_found()

    request_app = request.get("app", "")
    denied = _check_slot_app_ownership(slot, name, request_app, "context_inject")
    if denied is not None:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON"}, status=400)
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Same window as /note: authorized before the body read, so re-decide against
    # the slot as it is now, ahead of the only write.
    stale = _reauthorize_after_await(state, slot, name, request_app, "context_inject")
    if stale is not None:
        return stale

    # Normalize the source the same way /note does, so a whitespace-padded label
    # renders a clean drain frame and shares one cap bucket with its trimmed
    # form. /context keeps empty-source-uncapped and applies no default label: a
    # sourceless context injection is intentionally bucket-free.
    err = _enqueue_pending_context(
        slot,
        content,
        _normalize_source(body.get("source")),
        body.get("ephemeral", True),
        body.get("maxAge"),
    )
    if err is not None:
        return err

    # SEL audit logging
    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="context_inject",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    return web.json_response({"ok": True, "pending": len(slot._pending_context)})


async def api_chat_slot_note(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/note — visible transcript line + silent next-turn context.

    A background actor (a cron, an app) uses this to drop a short, DECLARATIVE
    note into a chat that is both (a) visible in the transcript right away and
    (b) known to the agent if the user later asks about it -- WITHOUT firing an
    LLM turn.

    A plain transcript append is not enough on its own: a live provider holds its
    own in-memory conversation state and a normal send forwards only the new user
    message, so a row written via ``slot.append()`` alone is never seen by the
    model. The channel that IS seen is ``_pending_context``, which is drained and
    prepended to the next user message. So the endpoint does two writes against
    the same slot:

    1. visible line -- ``slot.append(role="inject", cls="reconcile-note")`` so it
       renders in the transcript and persists.
    2. context entry -- a ``_pending_context`` entry (the same channel
       ``/context`` uses) drained onto the user's next manual message exactly
       once, then cleared.

    Both writes always happen. A context-only write is ``POST /context``, which
    already exists; there is no visible-only mode, because no caller wanted one.

    A session reset in between can replay the transcript row into the new
    session, so the model may see the note twice in one prompt. The queued copy
    is kept regardless: the replay is char-budget bounded, so dropping it would
    lose an older note the replay had already trimmed away.

    Notes are meant to be declarative -- state what happened, never ask. An
    interrogative note rides along as background context and may get answered on
    the next unrelated turn. The context half defaults to a 24h ``maxAge`` so a
    never-followed-up note self-expires; the visible line is permanent.

    Body::

        {
            "content": "...",         // required, declarative, non-empty string
            "source": "board-sync",   // optional frame label + per-source cap bucket;
                                      //   <=64 chars, no control chars; empty -> "note"
            "maxAge": 86400,          // optional seconds; omitted -> 24h default.
                                      //   Explicit null -> no expiry, as on /context.
            "ephemeral": true         // optional, default true (passed to the context entry)
        }

    Returns ``{"ok", "appended", "visibleDeferred", "contextSkipped", "pending"}``.
    If the source's per-source context cap is already full the visible line is
    still written and ``contextSkipped`` is true: the cap protects the context
    queue, not the transcript, so the call is NOT 429'd.

    When a turn is already running BOTH halves are held and written at that
    turn's end, so ``appended`` is false and ``visibleDeferred`` is true. Its
    order is preserved and it is not dropped while this gateway stays up -- the
    hold is in memory, so a 200 means accepted for this gateway lifetime, not
    durable delivery.
    Appending mid-turn would take the row the replay path skips and cause the
    user's own request to be replayed; queueing the context mid-turn would let
    the turn already in flight drain it, so the note would shape the request it
    was written after and the next turn would find nothing. Every rejection
    still happens on the POST. ``pending`` counts held entries too, so it always
    reports what the model will receive. Holding more than
    ``_MAX_DEFERRED_NOTES`` on one turn is a 429 ``deferred_notes_full``.
    """

    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        # Byte-identical to the ownership denial below, or an app token could tell
        # "not mine" from "does not exist" and enumerate foreign slot names.
        return _slot_not_found()

    request_app = request.get("app", "")
    denied = _check_slot_app_ownership(slot, name, request_app, "note_post")
    if denied is not None:
        return denied

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)
    # A scalar or array parses cleanly and then makes `.get` raise past the
    # except above into a 500, so the SHAPE needs its own rejection.
    if not isinstance(body, dict):
        return web.json_response(
            {"error": "body must be a JSON object", "code": "invalid_body"}, status=400
        )

    content = body.get("content", "")
    bad = (
        _validate_content(content)
        or _validate_source(body.get("source"))
        or _validate_max_age(body.get("maxAge"))
    )
    if bad is not None:
        return bad

    # Default an empty, absent, or whitespace-only source to "note" so the drain
    # frame reads [Background context from "note"] rather than empty quotes.
    source = _normalize_source(body.get("source")) or "note"

    # Ownership was decided before the body read. Re-decide it here, against the
    # slot as it is NOW, because that await is long enough for a rebind.
    stale = _reauthorize_after_await(state, slot, name, request_app, "note_post")
    if stale is not None:
        return stale

    # A turn in flight owns the tail of the transcript: the replay path skips
    # exactly one recall-eligible row to drop the current-turn user message, and
    # an `inject` row appended now would take that slot and get skipped in its
    # place, replaying the user's request twice. So the visible line is HELD and
    # written at the turn's end, which is why `appended` is reported separately.
    # This is decided BEFORE either write: a note rejected for a full hold must
    # not leave its context half behind to reach the next turn anyway.
    deferred = slot.running or slot._in_stage_execution
    if deferred and len(slot._deferred_notes) >= _MAX_DEFERRED_NOTES:
        return web.json_response(
            {
                "error": f"slot already holds {_MAX_DEFERRED_NOTES} deferred notes",
                "code": "deferred_notes_full",
            },
            status=429,
        )

    # The per-source cap protects the context QUEUE, not the transcript. So when
    # the context half is capped we still write the VISIBLE line -- the audit
    # record the caller came for -- and report contextSkipped=true, rather than
    # 429-ing the whole request and losing the visible note too. This matters
    # most for the default source="note" bucket, which every sourceless caller
    # shares. An omitted maxAge takes this endpoint's 24h default; an explicit
    # null means no expiry, the same as it does on /context.
    context_skipped = False
    context_entry: dict[str, object] | None = None
    if _source_cap_reached(slot, source):
        context_skipped = True
    else:
        max_age = body.get("maxAge", _UNSET)
        if max_age is _UNSET:
            max_age = _NOTE_CONTEXT_MAX_AGE
        context_entry, err = _build_pending_context_entry(
            slot, content, source, body.get("ephemeral", True), max_age
        )
        if err is not None:
            return err
        assert context_entry is not None
        # A held note's context is queued by the flush, not here. The drain runs
        # inside the turn and after its task is assigned, so an entry queued now
        # is read by the turn already running -- the note would shape the request
        # it was written after, and the next turn would find nothing.
        if not deferred:
            # Both immediate halves resolve their destination LATE, so each
            # records the session it was authorized against -- same reason the
            # deferred arm below does, and checked at those later seams.
            context_entry["noteSession"] = effective_session_key(slot)
            slot.append_pending_context(context_entry)

    # Caller-controlled content reaching the visible transcript (SSE plus the
    # on-disk JSONL). Redact at this sink so a secret or exfil URL cannot land
    # in user-visible history. The context half stays raw: that is the
    # trusted-caller boundary inherited from /context. Order matters -- exfil
    # URLs first, since that pass collapses the whole URL.
    visible_content, _ = redact_exfiltration_urls(content)
    visible_content, _ = redact_credentials(visible_content)
    if deferred:
        slot._deferred_notes.append(
            {
                "content": visible_content,
                "cls": "reconcile-note",
                "context": context_entry,
                # The session this note was authorized against. The gate above
                # only admits a slot that still routes to its own session, but
                # an unbound slot can acquire a foreign binding while the note
                # is held, and the flush resolves its target late.
                "session": effective_session_key(slot),
            }
        )
    else:
        slot.append(
            role="inject",
            content=visible_content,
            cls="reconcile-note",
            broadcast=True,
            meta={"noteSession": effective_session_key(slot)},
        )

    sel().log_api_access(
        caller=request_app or request.get("user", "dashboard"),
        operation="note_post",
        outcome="ok",
        source="app_kit",
        resources=f"slot={name}",
    )

    # A hold is delivered only if the slot still routes to the same session at
    # flush; a rebind during the hold drops it. An IMMEDIATE note is equally
    # conditional while the slot is UNBOUND, because both halves resolve their
    # destination late and every binding site claims an EMPTY binding
    # (``if not slot.linked_session_key``) -- so an already-bound slot cannot be
    # re-claimed and its immediate note is genuinely unconditional.
    delivery_conditional = deferred or not slot.linked_session_key
    return web.json_response(
        {
            "ok": True,
            "appended": not deferred,
            "visibleDeferred": deferred,
            "deliveryConditional": delivery_conditional,
            "contextSkipped": context_skipped,
            "pending": len(slot._pending_context) + slot.deferred_context_count(),
        }
    )
