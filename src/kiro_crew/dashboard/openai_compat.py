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

from kiro_crew.context import _neutralize_structural_markers
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.kiro_readiness import reject_if_kiro_unverified
from kiro_crew.dashboard.state import DashboardState, _normalize_slot_key
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


async def api_completions(request: web.Request) -> web.StreamResponse:
    """POST /v1/chat/completions — OpenAI-compatible chat endpoint."""
    # Unlike the dashboard, this endpoint has no transcript the caller reads: the
    # collectors below pick up only `chunk`/`assistant` roles, so the `error` card
    # an AcpAuthRequired turn appends is invisible and the request would return
    # HTTP 200 with empty content — an SDK client cannot tell that apart from a
    # model that legitimately said nothing. Fail closed until this endpoint
    # translates AcpAuthRequired into an OpenAI-shaped error.
    blocked = await reject_if_kiro_unverified(request)
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
    completion_id = _make_id()

    if slot_id:
        # Membership must be checked on the canonical (filename-charset) key —
        # get_or_create_slot folds unsafe chars, so a raw slot_id may map to an
        # existing slot even when the raw string is absent from _slots.
        freshly_created = _normalize_slot_key(slot_id) not in state._slots
        slot = state.get_or_create_slot(slot_id)
        # Busy check — prevent concurrent writes to same slot
        if slot.task is not None and not slot.task.done():
            sel().log_api_access(
                caller=request.remote or "",
                operation="openai_compat.chat",
                outcome="denied",
                source="openai_compat",
                resources=f"slot={slot_id}",
                error="slot busy",
            )
            return web.json_response(
                {"error": {"message": f"slot {slot_id!r} is busy", "type": "slot_busy"}},
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
    request_app = request.get("app", "") or ""
    is_dashboard_caller = request_app == ""
    if not is_dashboard_caller:
        if not slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="openai_compat.chat",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app cannot access unscoped slots",
            )
            if slot_id and freshly_created:
                state._slots.pop(slot.key, None)
            return web.json_response(
                {"error": {"message": "app cannot reach unscoped slots", "type": "forbidden"}},
                status=403,
            )
        if request_app != slot._app:
            sel().log_api_access(
                caller=request_app,
                operation="openai_compat.chat",
                outcome="denied",
                source="app_isolation",
                resources=f"slot={slot.key}",
                error="app does not own this slot",
            )
            if slot_id and freshly_created:
                state._slots.pop(slot.key, None)
            return web.json_response(
                {"error": {"message": "slot owned by another app", "type": "forbidden"}},
                status=403,
            )

    # Drain stale pending from prior turns whose reader disconnected
    slot.drain()

    if agent:
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

    # Launch the chat
    task = asyncio.create_task(asyncio.wait_for(_run_chat(state, slot, prompt), timeout=300))
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)

    created = int(time.time())
    ephemeral = not slot_id

    if stream:
        return await _stream_response(
            request, state, slot, completion_id, model, created, ephemeral
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
) -> web.StreamResponse:
    """Stream SSE in OpenAI format."""
    resp = web.StreamResponse()
    resp.content_type = "text/event-stream"
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Accel-Buffering"] = "no"
    await resp.prepare(request)

    try:
        _redact_buffer = ""
        _last_emitted_len = 0
        while True:
            pending = slot.drain()
            for msg in pending:
                if msg.get("cls") == "done":
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
                            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    chunk = {
                        "id": completion_id,
                        "object": _OPENAI_OBJECT_CHUNK,
                        "created": created,
                        "model": model,
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    }
                    await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    await resp.write(b"data: [DONE]\n\n")
                    return resp

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
                await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())

            # Detect task failure — prevents infinite loop
            if slot.task and slot.task.done():
                try:
                    slot.task.result()
                except BaseException as exc:
                    logger.warning("chat task failed: %s", exc)
                    err_data = {"error": {"message": "internal error", "type": "server_error"}}
                    await resp.write(f"data: {json.dumps(err_data)}\n\n".encode())
                    await resp.write(b"data: [DONE]\n\n")
                    return resp

            try:
                await asyncio.wait_for(slot.event.wait(), timeout=30)
            except asyncio.TimeoutError:
                await resp.write(b": keepalive\n\n")
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    finally:
        if ephemeral:
            state._slots.pop(slot.key, None)
            if slot.task and not slot.task.done():
                slot.task.cancel()
    return resp


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

    try:
        while True:
            pending = slot.drain()
            for msg in pending:
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
