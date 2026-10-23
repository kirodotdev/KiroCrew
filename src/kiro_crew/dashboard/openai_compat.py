"""OpenAI-compatible /v1/chat/completions endpoint.

Translates OpenAI API format into KiroCrew's slot-based chat system,
allowing any OpenAI SDK client to talk to KiroCrew agents by setting
`model` to the agent name (e.g. "router", "lite").

Limitations:
- ``usage`` fields are hardcoded to zero; KiroCrew does not track token
  counts at the slot layer. Clients relying on usage for billing/rate-
  limiting should use their own tokenizer on the response content.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from aiohttp import web

from kiro_crew import members as members_mod
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.context import _neutralize_structural_markers
from kiro_crew.dashboard.chat_handlers import write_sse_or_abort
from kiro_crew.dashboard.chat_runner import TURN_FAILED_META, _run_chat
from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND, effective_session_key
from kiro_crew.dashboard.kiro_readiness import (
    backend_signs_in_via_kiro_cli,
    live_session_signs_in_via_kiro_cli,
    reject_if_kiro_unverified,
    selected_backend,
)
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    RELAY_ARCHIVE_ERROR,
    is_relay_archive,
)
from kiro_crew.dashboard.slot_ownership import deny_app_slot_session_access
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key, load_window_off_loop
from kiro_crew.dashboard.turn_dispatch import chat_turn_timeout_secs
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE, is_registered_agent_name

logger = logging.getLogger(__name__)

_OPENAI_OBJECT_CHAT = "chat.completion"
_OPENAI_OBJECT_CHUNK = "chat.completion.chunk"
_MAX_MESSAGES = 200
_MAX_PROMPT_BYTES = 100 * 1024  # 100 KiB
_REDACT_MARGIN = 256  # hold back chars >= max redactable pattern length
_UNSUPPORTED_ROLES = frozenset(("tool", "function"))


def _make_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


# The dashed fences this module emits to separate context from the current turn.
# Scrubbed from CALLER content only, and deliberately NOT added to
# ``context._STRUCTURAL_MARKER_RES``: ``ContextBuilder.build_message`` neutralizes
# the whole turn with that global set, so a global entry would strip the fences
# added below and collapse the separation it is meant to create.
_CALLER_FENCE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"[-]{3,}\s*CONTEXT\s*ENTRY\s*(?:BEGIN|END)\s*[-]{3,}", re.IGNORECASE),
    re.compile(r"[-]{3,}\s*USER\s*MESSAGE\s*(?:BEGIN|END)\s*[-]{3,}", re.IGNORECASE),
)
_FENCE_NEUTRALIZED = "[marker-removed]"


def _scrub_caller_fences(text: str) -> str:
    """Remove this module's own framing fences from caller-supplied content."""
    for pattern in _CALLER_FENCE_RES:
        text = pattern.sub(_FENCE_NEUTRALIZED, text)
    return text


def _flatten_messages(messages: list[dict[str, Any]]) -> str:
    """Flatten OpenAI messages array into a single prompt string.

    Preserves the last user message as primary. System and prior messages
    are prepended as context block.

    Every caller-supplied ``content`` is scrubbed of the bracket boundary markers
    (via :func:`_neutralize_structural_markers`) and of this module's own dashed
    fences (via :func:`_scrub_caller_fences`). Collapsing distinct role channels
    into one string means the role labels and the fences below become the only
    signal of where caller content starts and stops, so content that replicates
    one could otherwise close its own region and forge a ``[SYSTEM]`` block the
    agent treats as authoritative.
    """
    if not messages:
        return ""
    last_user = ""
    system_parts: list[str] = []
    context_parts: list[str] = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content") or ""
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "text"
            )
        elif not isinstance(content, str):
            # A scalar (or null) content is off-spec but must not 500: coerce
            # before the scrubbers, which are string-only.
            content = "" if content is None else str(content)
        content = _scrub_caller_fences(_neutralize_structural_markers(content))
        if role == "user":
            if last_user:
                context_parts.append(f"[Previous user message] {last_user}")
            last_user = content
        elif role == "system":
            system_parts.append(f"[SYSTEM] {content}")
        elif role == "assistant":
            context_parts.append(f"[Previous assistant response] {content}")
    context_parts = system_parts + context_parts

    if not last_user:
        return ""

    if context_parts and len(messages) > 1:
        ctx = "\n".join(context_parts)
        return (
            f"--- CONTEXT ENTRY BEGIN ---\n{ctx}\n--- CONTEXT ENTRY END ---\n\n"
            f"--- USER MESSAGE BEGIN ---\n{last_user}\n--- USER MESSAGE END ---"
        )
    return last_user


def _redact(text: str) -> str:
    """Apply defense-in-depth redaction to LLM output."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _auth_required_error(message: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Translate a confirmed ACP auth error row into the OpenAI envelope."""
    content = message.get("content")
    if not isinstance(content, str) or not content:
        content = "Authentication is required for the selected backend."
    return {
        "error": {
            "message": _redact(content),
            "type": "authentication_error",
            "code": AUTH_REQUIRED_KIND,
        }
    }


def _reject_unowned_app_slot(
    request: web.Request,
    state: DashboardState,
    slot: Any | None,
    *,
    slot_id: str,
    freshly_created: bool = False,
) -> web.Response | None:
    """The app-isolation 403 for *slot*, or ``None`` when the caller may use it.

    One predicate for every refusal point on this route: the slot claim plus the
    session and transcript it is linked to (``deny_app_slot_session_access``),
    so an app that owns a channel-linked slot is refused exactly as for a slot
    another app owns. Run before the readiness peek, so a foreign app learns
    neither a slot's existence nor its session's sign-in state, and again on the
    slot the request actually obtains after each await.
    """
    request_app = request.get("app", "") or ""
    if not request_app:
        return None
    slot_app = getattr(slot, "_app", "") if slot is not None else ""
    slot_key = getattr(slot, "key", "") or slot_id or "ephemeral"
    if slot is None:
        sel().log_api_access(
            caller=request_app,
            operation="openai_compat.chat",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot_key}",
            error="app cannot access unscoped slots",
        )
    elif deny_app_slot_session_access(request_app, slot, slot_key, "openai_compat.chat") is None:
        return None

    if freshly_created and slot is not None:
        state._slots.pop(slot.key, None)
    if not slot_app:
        message = "app cannot reach unscoped slots"
    elif request_app != slot_app:
        message = "slot owned by another app"
    else:
        message = "app cannot reach this slot"
    return web.json_response(
        {
            "error": {
                "message": message,
                "type": "forbidden",
                "code": "app_token_forbidden",
            },
            # Top-level duplicate is the dashboard/i18n contract
            # (test_error_code_contract reads the top-level dict).
            "code": "app_token_forbidden",
        },
        status=403,
    )


async def api_completions(request: web.Request) -> web.StreamResponse:
    """POST /v1/chat/completions — OpenAI-compatible chat endpoint."""
    state: DashboardState = request.app["state"]

    try:
        body = await request.json()
    except Exception:
        return web.json_response(
            {"error": {"message": "invalid JSON", "type": "invalid_request_error"}},
            status=400,
        )

    model = body.get("model")
    messages = body.get("messages", [])
    stream = body.get("stream", False)

    # --- Input validation ---
    # model is required and must be a non-empty string (OpenAI contract)
    if not isinstance(model, str) or not model:
        return web.json_response(
            {
                "error": {
                    "message": "model must be a non-empty string",
                    "type": "invalid_request_error",
                }
            },
            status=400,
        )

    if not isinstance(messages, list) or not messages:
        return web.json_response(
            {
                "error": {
                    "message": "messages must be a non-empty array",
                    "type": "invalid_request_error",
                }
            },
            status=400,
        )
    if len(messages) > _MAX_MESSAGES:
        return web.json_response(
            {
                "error": {
                    "message": f"too many messages (max {_MAX_MESSAGES})",
                    "type": "invalid_request_error",
                }
            },
            status=400,
        )
    for msg in messages:
        if not isinstance(msg, dict):
            return web.json_response(
                {
                    "error": {
                        "message": "each message must be an object",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        # Reject unsupported roles loudly (tool/function)
        role = msg.get("role", "user")
        if role in _UNSUPPORTED_ROLES:
            return web.json_response(
                {
                    "error": {
                        "message": f"role {role!r} not supported",
                        "type": "invalid_request_error",
                    }
                },
                status=400,
            )
        # Reject non-text multimodal content
        content = msg.get("content")
        if isinstance(content, list):
            non_text = [p for p in content if isinstance(p, dict) and p.get("type") != "text"]
            if non_text:
                return web.json_response(
                    {
                        "error": {
                            "message": "multimodal content not supported",
                            "type": "invalid_request_error",
                        }
                    },
                    status=400,
                )

    agent = model

    prompt = _flatten_messages(messages)
    if not prompt:
        return web.json_response(
            {"error": {"message": "no user message found", "type": "invalid_request_error"}},
            status=400,
        )
    if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
        return web.json_response(
            {"error": {"message": "prompt too large", "type": "invalid_request_error"}},
            status=413,
        )

    # id field targets an existing slot/conversation; omit for ephemeral
    slot_id = body.get("id", "")
    if slot_id and not isinstance(slot_id, str):
        return web.json_response(
            {"error": {"message": "id must be a string", "type": "invalid_request_error"}},
            status=400,
        )
    normalized_slot_id = _normalize_slot_key(slot_id) if slot_id else ""
    if request.get("app", "") and normalized_slot_id.casefold().startswith(
        members_mod.DM_SLOT_KEY_PREFIX
    ):
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="openai_compat.chat",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={normalized_slot_id}",
            error="app cannot access member slots",
        )
        return web.json_response(
            {
                "error": {"message": "not found", "type": "invalid_request_error"},
                "code": "not_found",
            },
            status=404,
        )
    existing_member_slot = None
    if slot_id:
        existing = state._slots.get(normalized_slot_id)
        if existing and existing.mode == members_mod.DM_SLOT_MODE:
            existing_member_slot = existing
    if existing_member_slot is not None and not members_mod.is_dispatchable_member_name(
        existing_member_slot.agent
    ):
        sel().log_api_access(
            caller=request.remote or "",
            operation="openai_compat.chat",
            outcome="denied",
            source="member_pin",
            resources=f"slot={existing_member_slot.key}",
            error="stored member pin is not dispatchable",
        )
        return web.json_response(
            {
                "error": {
                    "message": "this thread's crew name cannot be dispatched",
                    "type": "invalid_request_error",
                    "code": "member_pin_mismatch",
                },
                "code": "member_pin_mismatch",
            },
            status=409,
        )
    if slot_id and not _AGENT_NAME_RE.fullmatch(slot_id) and existing_member_slot is None:
        return web.json_response(
            {"error": {"message": "invalid id (slot name)", "type": "invalid_request_error"}},
            status=400,
        )
    member_pin_match = members_mod.member_pin_matches(
        getattr(existing_member_slot, "mode", None),
        getattr(existing_member_slot, "agent", None),
        agent,
    )
    if (
        not is_registered_agent_name(agent)
        and not member_pin_match
        # A configured free-form member name is a valid ``model``; an off-grammar
        # string that is not a member is refused before any slot is created.
        and not await asyncio.to_thread(members_mod.is_configured_dispatchable_member, agent)
    ):
        return web.json_response(
            {"error": {"message": "invalid model/agent name", "type": "invalid_request_error"}},
            status=400,
        )
    # App ownership precedes the live-session readiness peek below. Otherwise an
    # app that does not own a slot could tell a live kiro-backed session (503)
    # from a foreign or absent one (the ordinary 403), reading state it may not
    # see. An app is refused for an ABSENT target here too: a slot this request
    # would create is unscoped, which the post-creation check refuses anyway,
    # and answering it after readiness would let an app tell an existing
    # unscoped slot (403) from a missing one (503) while kiro-cli is signed out.
    # The post-creation check further down repeats this on the slot the request
    # actually obtains.
    request_app = request.get("app", "") or ""
    live_slot = state._slots.get(normalized_slot_id) if slot_id else None
    if request_app:
        refused = _reject_unowned_app_slot(request, state, live_slot, slot_id=slot_id)
        if refused is not None:
            return refused

    # This endpoint has no transcript the caller reads, so a kiro-backed turn is
    # gated on the kiro-cli latch BEFORE it starts. The gate applies only to a
    # harness that signs in through kiro-cli. An `id` naming a slot with a LIVE
    # session continues that session, which keeps the harness it started on
    # across a PATCH of agent.acp_backend, so its own verdict wins. A missing or
    # not-yet-live slot gets a session built by the INSTALLED provider factory,
    # so the verdict reads that factory's backend (`state=`), resolved for the
    # slot's session key so a member thread is judged by its member backend.
    # Resolved ONCE: the streaming path reads the same verdict to decide whether
    # its SSE headers may go out before the turn produces anything. On a backend
    # the gate stands aside for, a turn that fails is translated by the
    # collectors below (401 for a sign-in failure, 500 otherwise), never an
    # empty 200.
    live_key = effective_session_key(live_slot) if live_slot is not None else None
    signs_in_via_kiro_cli = (
        await live_session_signs_in_via_kiro_cli(state, live_key) if live_key is not None else None
    )
    if signs_in_via_kiro_cli is None:
        signs_in_via_kiro_cli = backend_signs_in_via_kiro_cli(
            await selected_backend(live_key, state=state)
        )
    blocked = await reject_if_kiro_unverified(request, signs_in_via_kiro_cli=signs_in_via_kiro_cli)
    if blocked is not None:
        return web.json_response(
            {
                "error": {
                    "message": "Kiro CLI setup or sign-in is required before starting a session.",
                    "type": "service_unavailable_error",
                    "code": "kiro_prerequisite_required",
                }
            },
            status=503,
        )

    completion_id = _make_id()

    if slot_id:
        # A thread restored as a sidebar row loads its window here, with the
        # read off the loop, before the turn below appends to it; the slot is
        # looked up again after the await.
        if (_pending := state._slots.get(_normalize_slot_key(slot_id))) is not None:
            await load_window_off_loop(_pending)
        # Membership must be checked on the canonical (filename-charset) key —
        # get_or_create_slot folds unsafe chars, so a raw slot_id may map to an
        # existing slot even when the raw string is absent from _slots.
        freshly_created = _normalize_slot_key(slot_id) not in state._slots
        try:
            slot = state.get_or_create_slot(slot_id)
        except ValueError as exc:
            # A slot another app now holds at this key must answer the ownership
            # refusal, not describe itself through the constructor's reason.
            current = state._slots.get(_normalize_slot_key(slot_id))
            if current is not None:
                refused = _reject_unowned_app_slot(request, state, current, slot_id=slot_id)
                if refused is not None:
                    return refused
            # The constructor's refusals (member-* key reservation,
            # memory-mode mismatch) map to a 409 in the OpenAI error shape —
            # the same translation the send and slot-create paths perform.
            return web.json_response(
                {
                    "error": {
                        "message": str(exc),
                        "type": "invalid_request_error",
                        "code": "member_slot_reserved",
                    },
                    # The OpenAI wire shape nests code inside `error`; the
                    # top-level duplicate is the dashboard/i18n contract
                    # (test_error_code_contract reads the top-level dict).
                    "code": "member_slot_reserved",
                },
                status=409,
            )
        # The readiness await above can outlast a close and a same-name create by
        # another app, so judge the slot actually obtained before any of its own
        # 409s can describe it.
        refused = _reject_unowned_app_slot(
            request,
            state,
            slot,
            slot_id=slot_id,
            freshly_created=bool(slot_id and freshly_created),
        )
        if refused is not None:
            return refused
        # A relay archive is read-only. Reaching the local dispatch chokepoint
        # (`_run_chat`) would append the prompt and emit a WS-only `chat_done`,
        # leaving this HTTP caller waiting on a turn that never runs. Refuse
        # BEFORE any mutation. A freshly-created slot is always local, so this
        # only rejects an existing archive.
        if is_relay_archive(slot):
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id}",
                error="relay archive is read-only",
            )
            return web.json_response(
                {
                    "error": {
                        "message": RELAY_ARCHIVE_ERROR,
                        "type": "invalid_request_error",
                        "code": RELAY_ARCHIVE_CODE,
                    },
                    "code": RELAY_ARCHIVE_CODE,
                },
                status=409,
            )
        # Busy check — prevent concurrent writes to the same slot.
        if slot.running is True:
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id}",
                error="slot busy",
            )
            return web.json_response(
                {
                    "error": {
                        "message": f"slot {slot_id!r} is busy",
                        "type": "slot_busy",
                        "code": "slot_busy",
                    },
                    "code": "slot_busy",
                },
                status=409,
            )
        # Member DM threads are pinned to their crew — the specific refusal
        # (with its machine-readable code) must fire BEFORE the generic
        # mismatch below, or a member mismatch surfaces as an ordinary
        # conflict and the pin is invisible to the caller.
        if slot.mode == "member" and agent and agent != slot.agent:
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="member_pin",
                resources=f"slot={slot_id} agent={agent}",
                error=f"member thread pinned to {slot.agent}",
            )
            return web.json_response(
                {
                    "error": {
                        "message": "member thread agent is pinned",
                        "type": "invalid_request_error",
                        "code": "member_thread_agent_pinned",
                    },
                    # Top-level duplicate: the dashboard/i18n error-code
                    # contract reads the top-level dict; OpenAI clients read
                    # error.code.
                    "code": "member_thread_agent_pinned",
                },
                status=409,
            )
        if slot.mode == "member":
            _member_cfg = await asyncio.to_thread(KiroCrewConfig.load)
            if slot.agent not in _member_cfg.agents:
                sel().log_api_access(
                    caller=request.remote or "",
                    operation="openai_compat.chat",
                    outcome="denied",
                    source="member_pin",
                    resources=f"slot={slot_id}",
                    error=f"registry no longer names {slot.agent}",
                )
                return web.json_response(
                    {
                        "error": {
                            "message": "this thread's crew no longer exists",
                            "type": "invalid_request_error",
                            "code": "member_pin_mismatch",
                        },
                        "code": "member_pin_mismatch",
                    },
                    status=409,
                )
            if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX):
                _send_binding = await asyncio.to_thread(
                    members_mod.read_dm_binding_for_slot, slot.key
                )
                refused = _reject_unowned_app_slot(
                    request,
                    state,
                    slot,
                    slot_id=slot_id,
                    freshly_created=bool(slot_id and freshly_created),
                )
                if refused is not None:
                    return refused
                if _send_binding is None or _send_binding.get("member", "") != slot.agent:
                    sel().log_api_access(
                        caller=request.remote or "",
                        operation="openai_compat.chat",
                        outcome="denied",
                        source="member_pin",
                        resources=f"slot={slot_id}",
                        error="member binding missing or mismatched",
                    )
                    return web.json_response(
                        {
                            "error": {
                                "message": "this thread's binding is missing or no longer matches",
                                "type": "invalid_request_error",
                                "code": "member_binding_missing",
                            },
                            "code": "member_binding_missing",
                        },
                        status=409,
                    )
        # Agent mismatch — deny when slot has an agent and caller supplies a different one
        if slot.agent and slot.agent != agent:
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id} agent={agent}",
                error=f"slot agent mismatch (slot has {slot.agent})",
            )
            return web.json_response(
                {"error": {"message": "slot agent mismatch", "type": "conflict"}},
                status=409,
            )
    else:
        # App-scoped callers cannot create ephemeral slots (they'd always be
        # unscoped, triggering the 403 below). Reject early to avoid leaking
        # a slot that will immediately be discarded.
        request_app = request.get("app", "")
        if request_app:
            sel().log_api_access(
                caller=request_app,
                operation="openai_compat.chat",
                outcome="denied",
                source="app_isolation",
                resources="ephemeral",
                error="app cannot create ephemeral unscoped slots",
            )
            return web.json_response(
                {"error": {"message": "app cannot reach unscoped slots", "type": "forbidden"}},
                status=403,
            )
        slot_name = f"oai-{completion_id}"
        slot = state.get_or_create_slot(slot_name)
        # One request's slot, popped when it returns: nobody views its card.
        slot._dashboard_card_exempt = True

    # App-Kit ownership enforcement — mirror chat_handlers.api_chat
    # Non-app callers (dashboard, CLI) have no app identity and legitimately
    # skip this check — they access all slots, same as /api/chat. This is
    # safe because mixed_internal_paths already gates access via X-Internal-Secret.
    # Note: app_middleware only runs for app-scoped paths; for mixed_internal_paths
    # callers (dashboard, CLI, curl), request["app"] is unset — treat as non-app.
    is_dashboard_caller = request_app == ""
    refused = _reject_unowned_app_slot(
        request, state, slot, slot_id=slot_id, freshly_created=bool(slot_id and freshly_created)
    )
    if refused is not None:
        return refused

    # Drain stale pending from prior turns whose reader disconnected
    slot.drain()

    if agent:
        if slot.mode == "member" and agent != slot.agent:
            # Member DM threads are pinned to their crew. Only an EXISTING slot
            # can be in member mode (a slot this request just created carries
            # the caller's own mode), so no freshly_created cleanup applies.
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="member_pin",
                resources=f"slot={slot.key} agent={agent}",
                error=f"member thread pinned to {slot.agent}",
            )
            return web.json_response(
                {
                    "error": {
                        "message": "member thread agent is pinned",
                        "type": "invalid_request_error",
                        "code": "member_thread_agent_pinned",
                    },
                    # Top-level duplicate: the dashboard/i18n error-code
                    # contract reads the top-level dict; OpenAI clients read
                    # error.code.
                    "code": "member_thread_agent_pinned",
                },
                status=409,
            )
        slot.agent = agent
    slot.append("user", prompt, "msg msg-u")

    # SEL audit for tool invocation visibility
    sel().log_api_access(
        caller=request.remote or "",
        operation="openai_compat.chat",
        outcome="allowed",
        source="openai_compat",
        resources=f"slot={slot.key} agent={agent}",
    )

    # Both response shapes below consume `slot._pending` as their delivery
    # queue, and neither sets `_has_reader` (that flag also suppresses the
    # global message broadcast, which an app-owned slot still wants). Claim the
    # queue BEFORE the turn is dispatched: the first `await` inside the response
    # helpers lets the turn run, so a scope opened there would leave a window in
    # which a turn-end release could discard tokens this reader owes its client.
    with slot.pending_consumer():
        # Launch the chat, bounded by the standard chat-turn ceiling. A fixed
        # 300s cap here would race COMPACT_WAIT_TIMEOUT_SECS: a /compact prompt
        # phase plus the full compaction wait always exceeds it, so the outer
        # cancel would surface as an HTTP 500 instead of the graceful
        # compaction-timeout result.
        task = asyncio.create_task(
            asyncio.wait_for(
                _run_chat(
                    state,
                    slot,
                    prompt,
                    _directive_user_origin=is_dashboard_caller,
                    # Named for the same reason ``api_chat`` names it: the actor
                    # resolver's fallback is ``user``, so a dispatch that OBSERVED
                    # an app and stayed silent records a person who never typed
                    # anything -- and every consumer that asks "is a human
                    # watching this turn" then gets the wrong answer. ``""`` is the
                    # parameter's own default and reads as "not named", so a
                    # dashboard caller is unchanged.
                    _turn_actor="app" if request_app else "",
                ),
                timeout=chat_turn_timeout_secs(),
            )
        )
        slot.task = task
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

        created = int(time.time())
        ephemeral = not slot_id

        if stream:
            # A kiro-backed turn was gated above, so its SSE headers go out at
            # once; a turn on a harness the gate stood aside for may fail before
            # any output, so its headers wait until there is something to send
            # and that failure can still answer a plain HTTP error.
            return await _stream_response(
                request,
                state,
                slot,
                completion_id,
                model,
                created,
                ephemeral,
                defer_prepare=not signs_in_via_kiro_cli,
            )
        else:
            return await _blocking_response(state, slot, completion_id, model, created, ephemeral)


_SERVER_ERROR = {"error": {"message": "internal error", "type": "server_error"}}


def _turn_failed(msg: dict[str, Any]) -> bool:
    """Whether *msg* is the error row of a turn that failed before it started.

    That turn's cycle still ends with a ``done`` row, which on its own reads as
    an empty successful reply."""
    meta = msg.get("meta")
    return isinstance(meta, dict) and bool(meta.get(TURN_FAILED_META))


def _terminal_failure(
    slot: Any, *, failed: bool, error_row: dict[str, Any] | None, produced: bool
) -> str | None:
    """How a turn that just reached ``done`` failed, or ``None`` when it did not.

    ``"auth"`` when the turn ended ``AcpAuthRequired`` (the runner sets
    ``_last_turn_auth_required``); ``"server"`` when it failed before it started
    (``TURN_FAILED_META``) or appended an ``error`` row and produced no reply.
    The second case is every other ACP failure -- a foreign harness that dies at
    startup with stderr the auth vocabulary does not recognise lands here -- so
    the caller gets an error instead of an empty 200.
    """
    if error_row is not None and getattr(slot, "_last_turn_auth_required", False) is True:
        return "auth"
    if failed or (error_row is not None and not produced):
        return "server"
    return None


async def _stream_server_error(
    request: web.Request, resp: web.StreamResponse
) -> web.StreamResponse:
    """End an SSE completion with the server-error frame and ``[DONE]``."""
    await write_sse_or_abort(request, resp, f"data: {json.dumps(_SERVER_ERROR)}\n\n".encode())
    await write_sse_or_abort(request, resp, b"data: [DONE]\n\n")
    return resp


async def _stream_response(
    request: web.Request,
    state: DashboardState,
    slot: Any,
    completion_id: str,
    model: str,
    created: int,
    ephemeral: bool,
    *,
    defer_prepare: bool = False,
) -> web.StreamResponse:
    """Stream SSE in OpenAI format, keeping HTTP errors possible until output starts.

    With *defer_prepare* the response is prepared on the first write, so a turn
    that fails before any output still answers a plain HTTP 401 (sign-in) or 500.
    That is the only route a foreign harness's failure has to an SDK client,
    since the readiness gate stands aside for those backends. A turn the gate
    already vouched for (a kiro-backed session) prepares eagerly, so its SSE
    headers reach the client at once instead of with the first token or the 30s
    keepalive.
    """
    resp: web.StreamResponse | None = None

    async def _prepared_response() -> web.StreamResponse:
        nonlocal resp
        if resp is None:
            resp = web.StreamResponse()
            resp.content_type = "text/event-stream"
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["X-Accel-Buffering"] = "no"
            await resp.prepare(request)
        return resp

    async def _write(data: bytes) -> None:
        await write_sse_or_abort(request, await _prepared_response(), data)

    async def _server_error() -> web.StreamResponse:
        if resp is None:
            return web.json_response(
                {
                    "error": {
                        "message": "internal error",
                        "type": "server_error",
                        "code": "server_error",
                    },
                    "code": "server_error",
                },
                status=500,
            )
        return await _stream_server_error(request, resp)

    if not defer_prepare:
        await _prepared_response()

    try:
        _redact_buffer = ""
        _last_emitted_len = 0
        failed = False
        error_row: dict[str, Any] | None = None
        while True:
            pending = slot.drain()
            for msg in pending:
                failed = failed or _turn_failed(msg)
                if msg.get("role") == "error":
                    error_row = msg
                    continue
                if msg.get("cls") == "done":
                    failure = _terminal_failure(
                        slot, failed=failed, error_row=error_row, produced=bool(_redact_buffer)
                    )
                    if failure == "auth" and error_row is not None:
                        auth_error = _auth_required_error(error_row)
                        if resp is None:
                            return web.json_response(
                                # Dict literal, not the variable: the error-code
                                # ratchet only reads transparent bodies, and the
                                # top-level code is the dashboard/i18n contract.
                                {"error": auth_error["error"], "code": AUTH_REQUIRED_KIND},
                                status=401,
                            )
                        await _write(f"data: {json.dumps(auth_error)}\n\n".encode())
                        await _write(b"data: [DONE]\n\n")
                        return resp
                    if failure == "server":
                        return await _server_error()
                    # Flush remaining buffer
                    if _redact_buffer:
                        final = _redact(_redact_buffer)
                        remainder = final[_last_emitted_len:]
                        if remainder:
                            chunk = {
                                "id": completion_id,
                                "object": _OPENAI_OBJECT_CHUNK,
                                "created": created,
                                "model": model,
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"role": "assistant", "content": remainder},
                                        "finish_reason": None,
                                    }
                                ],
                            }
                            await _write(f"data: {json.dumps(chunk)}\n\n".encode())
                    chunk = {
                        "id": completion_id,
                        "object": _OPENAI_OBJECT_CHUNK,
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    await _write(f"data: {json.dumps(chunk)}\n\n".encode())
                    await _write(b"data: [DONE]\n\n")
                    return await _prepared_response()

                # Stream assistant and chunk roles (token-level streaming)
                if msg.get("role") not in ("assistant", "chunk"):
                    continue
                content = msg.get("content") or ""
                if not content:
                    continue

                # Buffered redaction: accumulate, redact full buffer, emit safe prefix
                _redact_buffer += content
                redacted = _redact(_redact_buffer)
                safe_end = max(_last_emitted_len, len(redacted) - _REDACT_MARGIN)
                delta = redacted[_last_emitted_len:safe_end]
                _last_emitted_len = safe_end
                if not delta:
                    continue

                chunk = {
                    "id": completion_id,
                    "object": _OPENAI_OBJECT_CHUNK,
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": delta},
                            "finish_reason": None,
                        }
                    ],
                }
                await _write(f"data: {json.dumps(chunk)}\n\n".encode())

            # Detect task failure — prevents infinite loop
            if slot.task and slot.task.done():
                try:
                    slot.task.result()
                except BaseException as exc:
                    logger.warning("chat task failed: %s", exc)
                    return await _server_error()

            try:
                await asyncio.wait_for(slot.event.wait(), timeout=30)
            except asyncio.TimeoutError:
                await _write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if ephemeral:
            state._slots.pop(slot.key, None)
            if slot.task and not slot.task.done():
                slot.task.cancel()
    return await _prepared_response()


async def _blocking_response(
    state: DashboardState,
    slot: Any,
    completion_id: str,
    model: str,
    created: int,
    ephemeral: bool,
) -> web.Response:
    """Wait for full completion and return a single JSON response.

    Note: ``usage`` is hardcoded to zero — KiroCrew does not expose token
    counts at the slot layer.
    """
    collected: list[str] = []
    failed = False
    error_row: dict[str, Any] | None = None

    try:
        while True:
            pending = slot.drain()
            for msg in pending:
                failed = failed or _turn_failed(msg)
                if msg.get("role") == "error":
                    error_row = msg
                    continue
                failure = (
                    _terminal_failure(
                        slot, failed=failed, error_row=error_row, produced=any(collected)
                    )
                    if msg.get("cls") == "done"
                    else None
                )
                if failure == "auth" and error_row is not None:
                    auth_error = _auth_required_error(error_row)
                    return web.json_response(
                        # Dict literal for the error-code ratchet; top-level
                        # code is the dashboard/i18n contract.
                        {"error": auth_error["error"], "code": AUTH_REQUIRED_KIND},
                        status=401,
                    )
                if failure == "server":
                    return web.json_response(
                        {
                            "error": {
                                "message": "internal error",
                                "type": "server_error",
                                "code": "server_error",
                            },
                            "code": "server_error",
                        },
                        status=500,
                    )
                if msg.get("cls") == "done":
                    content = _redact("".join(collected))
                    return web.json_response(
                        {
                            "id": completion_id,
                            "object": _OPENAI_OBJECT_CHAT,
                            "created": created,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "message": {"role": "assistant", "content": content},
                                    "finish_reason": "stop",
                                }
                            ],
                            "usage": {
                                "prompt_tokens": 0,
                                "completion_tokens": 0,
                                "total_tokens": 0,
                            },
                        }
                    )
                if msg.get("role") == "chunk":
                    collected.append(msg.get("content", ""))
                elif msg.get("role") == "assistant" and not collected:
                    collected.append(msg.get("content", ""))

            # Detect task failure — prevents infinite loop
            if slot.task and slot.task.done():
                try:
                    slot.task.result()
                except BaseException as exc:
                    logger.warning("chat task failed: %s", exc)
                    return web.json_response(
                        {
                            "error": {
                                "message": "internal error",
                                "type": "server_error",
                                "code": "server_error",
                            },
                            "code": "server_error",
                        },
                        status=500,
                    )

            try:
                await asyncio.wait_for(slot.event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
    finally:
        if ephemeral:
            state._slots.pop(slot.key, None)
            if slot.task and not slot.task.done():
                slot.task.cancel()
