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
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_utils import AUTH_REQUIRED_KIND, effective_session_key
from kiro_crew.dashboard.kiro_readiness import (
    backend_signs_in_via_kiro_cli,
    live_session_signs_in_via_kiro_cli,
    reject_if_kiro_unverified,
    selected_backend,
)
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
from kiro_crew.dashboard.turn_dispatch import chat_turn_timeout_secs
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.validation import _AGENT_NAME_RE

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
    """Return the app-isolation refusal before readiness state is inspected."""
    # Local for the same import-cycle reason as chat_regenerate's ownership gate.
    from kiro_crew.dashboard.chat_handlers import _check_slot_app_ownership

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
    elif _check_slot_app_ownership(slot, slot_key, request_app, "openai_compat.chat") is None:
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

    # model maps to agent name — validate
    agent = model
    if not _AGENT_NAME_RE.match(agent):
        return web.json_response(
            {"error": {"message": "invalid model/agent name", "type": "invalid_request_error"}},
            status=400,
        )

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
    if slot_id and not _AGENT_NAME_RE.match(slot_id):
        return web.json_response(
            {"error": {"message": "invalid id (slot name)", "type": "invalid_request_error"}},
            status=400,
        )
    # App tokens get ONE uniform answer for the whole member-* space,
    # BEFORE any existence check -- including the readiness gate's live-session
    # peek below, which would otherwise answer 503 for a member slot holding a
    # live kiro session and 404 for one that does not: an app can never own a member slot, so
    # the reservation 409 for a missing key next to the ownership 404
    # for an existing one would let an app enumerate member threads.
    if (
        slot_id
        and request.get("app", "")
        and _normalize_slot_key(slot_id).startswith(members_mod.DM_SLOT_KEY_PREFIX)
    ):
        sel().log_api_access(
            caller=request.get("app", ""),
            operation="openai_compat.chat",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot_id}",
            error="app cannot access member slots",
        )
        return web.json_response(
            {
                "error": {"message": "not found", "type": "invalid_request_error"},
                "code": "not_found",
            },
            status=404,
        )
    # App ownership precedes the live-session readiness peek. Otherwise a caller
    # that does not own a slot can distinguish its live kiro backend (503) from a
    # foreign or absent backend (the ordinary 403), probing state it may not read.
    # The later check repeats this after the readiness await to close a slot-replace
    # race; app isolation is cheap and must bracket the state-dependent operation.
    live_slot = state._slots.get(_normalize_slot_key(slot_id)) if slot_id else None
    if request.get("app", ""):
        ownership_error = _reject_unowned_app_slot(request, state, live_slot, slot_id=slot_id)
        if ownership_error is not None:
            return ownership_error

    # An `id` naming a slot with a LIVE session continues that session, which
    # keeps the harness it started on across a PATCH of agent.acp_backend
    # (the same rule regenerate applies), so the configured default is the wrong
    # backend to gate on for it. A missing or not-yet-live slot gets a fresh
    # session on the configured default, which is what a `None` verdict reads --
    # the default for THAT slot's session key, since an existing member thread
    # is routed to `agent.member_acp_backend` rather than the gateway field.
    # Resolved ONCE here (the regenerate pattern): the gate takes this snapshot
    # instead of reading config again, and the streaming path reads the same
    # verdict to decide whether the SSE headers may go out eagerly.
    live_key = effective_session_key(live_slot) if live_slot is not None else None
    signs_in_via_kiro_cli = (
        live_session_signs_in_via_kiro_cli(state, live_key) if live_key is not None else None
    )
    if signs_in_via_kiro_cli is None:
        signs_in_via_kiro_cli = backend_signs_in_via_kiro_cli(await selected_backend(live_key))
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
        # Membership must be checked on the canonical (filename-charset) key —
        # get_or_create_slot folds unsafe chars, so a raw slot_id may map to an
        # existing slot even when the raw string is absent from _slots.
        freshly_created = _normalize_slot_key(slot_id) not in state._slots
        try:
            slot = state.get_or_create_slot(slot_id)
        except ValueError as exc:
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
        # A remote-bound slot runs its turn on a connected peer and streams the
        # reply over the dashboard WebSocket; this endpoint has no such channel —
        # its collectors read only local `chunk`/`assistant` rows. Reaching the
        # local dispatch chokepoint (`_run_chat`, keyed on `executor == "remote"`)
        # would append the prompt and emit a WS-only `chat_done`, leaving this HTTP
        # caller waiting forever on a turn the peer never received and history
        # holding an unsent turn. Refuse BEFORE any mutation — keyed on
        # `executor` (not `is_remote`) so a half-open binding is refused too,
        # matching the chokepoint and the `api_chat` incomplete-binding guard. A
        # freshly-created slot is always local, so this only rejects an existing
        # remote-bound target.
        if getattr(slot, "executor", "") == "remote":
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id}",
                error="remote-bound slot not supported on OpenAI-compat endpoint",
            )
            return web.json_response(
                {
                    "error": {
                        "message": (
                            "this session is bound to a remote crew; the "
                            "OpenAI-compatible endpoint cannot relay remote turns"
                        ),
                        "type": "invalid_request_error",
                        "code": "remote_slot_unsupported",
                    },
                    "code": "remote_slot_unsupported",
                },
                status=409,
            )
        # Busy check — prevent concurrent writes to the same slot. ``running``
        # includes the outer Autopilot controller while no child turn occupies
        # ``slot.task``; the pending marker keeps the same isolation after an
        # authentication pause has ended that controller but before Stage N is
        # settled and captured.
        if slot.running is True:
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id}",
                error="slot busy",
            )
            if (
                slot.stage_boundary.stage is not None
                and not slot.turn_running
                and not slot._plan_cancelled
            ):
                return web.json_response(
                    {
                        "error": {
                            "message": (
                                f"slot {slot_id!r} is paused at an Autopilot stage gate; "
                                "continue from the dashboard (Go)"
                            ),
                            "type": "slot_busy",
                            "code": "stage_gate_paused",
                        },
                        "code": "stage_gate_paused",
                    },
                    status=409,
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
            # Registry-drift fail-closed, mirroring the chat_send path: a
            # deleted crew's thread must not dispatch — the resolver would
            # fall back to the default agent and reply under the deleted
            # member's identity (a caller sending the matching stale agent
            # name passes the pin check above but still hits this).
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
            # Binding-drift fail-closed, also mirroring chat_send: a live
            # member slot whose dm.json was deleted or corrupted must refuse
            # the send — dispatching would persist a transcript that restore
            # skips and thread-open refuses (orphaned the moment the slot
            # dies). Same rare-send thread-IO budget as the registry check.
            if slot.key.startswith(members_mod.DM_SLOT_KEY_PREFIX):
                _send_binding = await asyncio.to_thread(
                    members_mod.read_dm_binding_for_slot, slot.key
                )
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

    # App-Kit ownership enforcement — mirror chat_handlers.api_chat
    # Non-app callers (dashboard, CLI) have no app identity and legitimately
    # skip this check — they access all slots, same as /api/chat. This is
    # safe because mixed_internal_paths already gates access via X-Internal-Secret.
    # Note: app_middleware only runs for app-scoped paths; for mixed_internal_paths
    # callers (dashboard, CLI, curl), request["app"] is unset — treat as non-app.
    # Same helper as the pre-readiness check above, re-run on the slot
    # get_or_create_slot actually handed back: the readiness await in between is
    # where a slot-replace race could swap the object the first check judged.
    request_app = request.get("app", "") or ""
    is_dashboard_caller = request_app == ""
    ownership_error = _reject_unowned_app_slot(
        request, state, slot, slot_id=slot_id, freshly_created=bool(slot_id and freshly_created)
    )
    if ownership_error is not None:
        return ownership_error

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
            # once; a foreign harness's sign-in failure surfaces only through the
            # turn itself, so its headers wait until there is output to send.
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
    """Stream SSE in OpenAI format, preserving HTTP errors before output starts.

    With *defer_prepare* the response is prepared on the first write, so a turn
    that fails ``AcpAuthRequired`` before any output still answers a plain HTTP
    401 -- the only route a foreign harness's sign-in failure has to an SDK
    client, since the readiness gate stands aside for those backends. A turn the
    gate already vouched for (a kiro-backed session) prepares eagerly, as the
    endpoint always did, so its SSE headers reach the client at once instead of
    with the first token or the 30s keepalive.
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
        prepared = await _prepared_response()
        await prepared.write(data)

    if not defer_prepare:
        await _prepared_response()

    try:
        _redact_buffer = ""
        _last_emitted_len = 0
        terminal_error: dict[str, Any] | None = None
        while True:
            pending = slot.drain()
            for msg in pending:
                if msg.get("role") == "error":
                    terminal_error = msg
                    continue
                if msg.get("cls") == "done":
                    if (
                        terminal_error is not None
                        and getattr(slot, "_last_turn_auth_required", False) is True
                    ):
                        auth_error = _auth_required_error(terminal_error)
                        if resp is None:
                            return web.json_response(
                                # Dict literal, not the variable: the error-code
                                # ratchet only reads transparent bodies, and the
                                # top-level code is the dashboard/i18n contract.
                                {"error": auth_error["error"], "code": AUTH_REQUIRED_KIND},
                                status=401,
                            )
                        await resp.write(f"data: {json.dumps(auth_error)}\n\n".encode())
                        await resp.write(b"data: [DONE]\n\n")
                        return resp
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
                    err_data = {"error": {"message": "internal error", "type": "server_error"}}
                    await _write(f"data: {json.dumps(err_data)}\n\n".encode())
                    await _write(b"data: [DONE]\n\n")
                    return await _prepared_response()

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
    terminal_error: dict[str, Any] | None = None

    try:
        while True:
            pending = slot.drain()
            for msg in pending:
                if msg.get("role") == "error":
                    terminal_error = msg
                    continue
                if msg.get("cls") == "done":
                    if (
                        terminal_error is not None
                        and getattr(slot, "_last_turn_auth_required", False) is True
                    ):
                        auth_error = _auth_required_error(terminal_error)
                        return web.json_response(
                            # Dict literal for the error-code ratchet; top-level
                            # code is the dashboard/i18n contract.
                            {"error": auth_error["error"], "code": AUTH_REQUIRED_KIND},
                            status=401,
                        )
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
                        {"error": {"message": "internal error", "type": "server_error"}},
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
