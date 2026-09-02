"""Bridge an editor's ACP session onto a real Kiro Crew turn.

``acp_server.server`` is deliberately ignorant of the gateway: it takes a
``PromptHandler``. This module supplies that handler, so an editor-driven turn
goes through the same machinery a dashboard turn does — context assembly
(memory, lessons, skills, history), the shared session registry, and the
PreToolUse hook gate.

It integrates at the ``LLMProvider`` seam (``SessionManager.get_or_create`` ->
``provider.stream``) rather than reusing ``dashboard.chat_runner._run_chat``.
``_run_chat`` is ~1500 lines of transport-specific concerns — slot queues,
WebSocket broadcast, Slack mirroring, auto-titling, native subagent cards — none
of which an editor needs, and all of which assume a dashboard slot exists.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Protocol

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    EVENT_TOOL_RESULT,
    KIRO_TOOL_TODO_LIST,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
)
from kiro_crew.acp_server.locations import extract_tool_locations
from kiro_crew.acp_server.server import PromptHandler, PromptRequest, SessionSink
from kiro_crew.hooks import HOOK_REPLY, TOOL_DENY
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

logger = logging.getLogger(__name__)

# Session keys are namespaced so an editor session can never collide with a
# dashboard slot ("dashboard:<slot>") or a Slack thread key.
SESSION_PREFIX = "acp"

# Tool statuses in the ACP wire vocabulary.
STATUS_PENDING = "pending"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"


class GatewayServices(Protocol):
    """The gateway surface this bridge needs.

    Narrowed to two attributes so the handler is unit-testable with a stub and
    does not bind to the whole ``DashboardState``.
    """

    sessions: Any
    context_builder: Any


def redact(text: str) -> str:
    """Strip credentials and exfiltration URLs from model-originated text.

    Everything crossing into the editor came from kiro-cli and is untrusted per
    ``AUTOSDE.yaml``; the ACP transport is an external surface like the WebSocket
    broadcast, so the same redaction applies.
    """
    if not text:
        return text
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def diff_content(raw: Any) -> list[dict[str, Any]] | None:
    """Build an ACP ``diff`` content block from a tool's raw params, if possible.

    This is what makes the editor render an inline diff with accept/reject rather
    than an opaque tool card. ``_dispatch`` flattens the agent's structured diff
    into unified-diff *text* for the dashboard, so the structured old/new is
    recovered here from the tool's raw params instead. Returns None when the tool
    is not a file edit, in which case the editor falls back to a plain approval.
    """
    if not isinstance(raw, dict):
        return None
    path = raw.get("path") or raw.get("file_path") or raw.get("filePath")
    if not isinstance(path, str) or not path:
        return None
    old = raw.get("oldStr") or raw.get("old_str") or raw.get("oldText")
    new = raw.get("newStr") or raw.get("new_str") or raw.get("newText")
    if new is None and isinstance(raw.get("file_text"), str):
        # Whole-file create/overwrite: no prior text to show.
        old, new = "", raw["file_text"]
    if not isinstance(new, str):
        return None
    return [
        {
            "type": "diff",
            "path": path,
            "oldText": redact(old if isinstance(old, str) else ""),
            "newText": redact(new),
        }
    ]


def make_prompt_handler(
    services: GatewayServices,
    *,
    agent: str | None = None,
    session_prefix: str = SESSION_PREFIX,
) -> PromptHandler:
    """Build a ``PromptHandler`` that runs turns through the gateway."""

    async def handle_prompt(request: PromptRequest, sink: SessionSink) -> str:
        session_key = f"{session_prefix}:{request.session_id}"
        provider, is_new, resumed = await services.sessions.get_or_create(session_key, agent=agent)
        # get_or_create acquires the per-session semaphore; failing to release it
        # wedges the session for the life of the gateway.
        try:
            message = await _build_message(
                services, request.text, is_new, session_key, agent=agent, resumed=resumed
            )
            if message is None:
                return STOP_REASON_END_TURN  # a hook already answered
            if isinstance(message, _HookReply):
                await sink.send_text(redact(message.text))
                return STOP_REASON_END_TURN
            return await _stream_turn(
                services,
                provider,
                message,
                sink,
                session_key=session_key,
                agent=agent or "",
            )
        finally:
            services.sessions.release(session_key)

    return handle_prompt


class _HookReply:
    """A message hook answered the turn itself; do not call the model."""

    def __init__(self, text: str) -> None:
        self.text = text


async def _build_message(
    services: GatewayServices,
    text: str,
    is_new: bool,
    session_key: str,
    *,
    agent: str | None,
    resumed: bool,
) -> Any:
    """Assemble the turn text via ContextBuilder, or a hook's short-circuit reply."""
    try:
        built, hook = await asyncio.to_thread(
            services.context_builder.build_message,
            text,
            is_new,
            session_key,
            agent=agent,
            resumed=resumed,
        )
    except Exception:
        # Context assembly is best-effort: a failure must not lose the user's
        # message, so fall back to the raw prompt rather than aborting the turn.
        logger.exception("context assembly failed for %s; sending raw prompt", session_key)
        return text
    if hook is not None and getattr(hook, "action", "") == HOOK_REPLY:
        return _HookReply(getattr(hook, "text", ""))
    return built or text


async def _stream_turn(
    services: GatewayServices,
    provider: Any,
    message: str,
    sink: SessionSink,
    *,
    session_key: str,
    agent: str,
) -> str:
    """Map one provider event stream onto the editor's session."""
    hidden_tool_ids: set[str] = set()
    async for event in provider.stream(message):
        if sink.cancelled:
            return STOP_REASON_CANCELLED

        kind = event.kind
        if kind == EVENT_TEXT_CHUNK:
            await sink.send_text(redact(event.text))
        elif kind == EVENT_THINKING_CHUNK:
            await sink.send_thought(redact(event.text))
        elif kind == EVENT_TOOL_CALL:
            if event.tool_name == KIRO_TOOL_TODO_LIST:
                if event.tool_call_id:
                    hidden_tool_ids.add(event.tool_call_id)
                continue
            await sink.send_tool_call(
                event.tool_call_id,
                redact(event.title),
                event.tool_kind,
                status=STATUS_PENDING,
                content=diff_content(event.raw_tool_params),
                locations=extract_tool_locations(event.tool_name, event.raw_tool_params),
            )
        elif kind in (EVENT_TOOL_CALL_UPDATE, EVENT_TOOL_RESULT):
            if event.tool_call_id in hidden_tool_ids:
                continue
            await sink.send_tool_call_update(
                event.tool_call_id,
                status=STATUS_COMPLETED if event.tool_final else STATUS_PENDING,
                content=_text_content(event.tool_output),
                locations=extract_tool_locations(event.tool_name, event.raw_tool_params),
            )
        elif kind == EVENT_PERMISSION_REQUEST:
            await _gate_tool(
                services,
                provider,
                event,
                sink,
                session_key=session_key,
                agent=agent,
            )
        elif kind == EVENT_COMPLETE:
            return event.stop_reason or STOP_REASON_END_TURN
    return STOP_REASON_END_TURN


def _text_content(text: str) -> list[dict[str, Any]] | None:
    if not text:
        return None
    return [{"type": "content", "content": {"type": "text", "text": redact(text)}}]


async def _gate_tool(
    services: GatewayServices,
    provider: Any,
    event: Any,
    sink: SessionSink,
    *,
    session_key: str,
    agent: str,
) -> None:
    """Decide one tool call: hooks first, the editor only if hooks allow.

    Kiro Crew — not the editor — owns trust scope. The PreToolUse gate
    (``auto_deny_tools``, sensitive-path and sensitive-command checks) is the
    reason per-call ``session/request_permission`` exists, so a hook DENY is
    final and is NOT overridable by an editor approval. Consulting the editor
    first would let a click bypass the gate entirely.
    """
    hook = services.context_builder.hooks.on_tool_call(
        event.title,
        session_key=session_key,
        agent=agent,
        tool_kind=event.tool_kind,
        raw_params=event.raw_tool_params,
        command=event.shell_command,
        is_shell=event.is_shell,
        mcp_server_name=event.mcp_server_name,
        mcp_tool_name=event.tool_name,
        resolved_agent=agent,
    )
    if getattr(hook, "action", "") == TOOL_DENY:
        logger.info("hook denied tool %r: %s", event.title, getattr(hook, "reason", ""))
        await provider.reject_tool(event.request_id)
        # Tell the editor why the tool never ran; otherwise the card just stalls.
        await sink.send_tool_call_update(
            event.tool_call_id,
            status=STATUS_FAILED,
            content=_text_content(f"Denied by Kiro Crew policy: {getattr(hook, 'reason', '')}"),
        )
        return

    tool_call: dict[str, Any] = {
        "toolCallId": event.tool_call_id,
        "title": redact(event.title),
        "kind": event.tool_kind,
    }
    content = diff_content(event.raw_tool_params) or _tool_input_content(event.tool_input)
    if content is not None:
        tool_call["content"] = content
    locations = extract_tool_locations(event.tool_name, event.raw_tool_params)
    if locations:
        tool_call["locations"] = locations

    if await sink.request_permission(tool_call):
        await provider.approve_tool(event.request_id)
    else:
        await provider.reject_tool(event.request_id)


def _tool_input_content(tool_input: str) -> list[dict[str, Any]] | None:
    """Show the tool's input when there is no diff to render."""
    if not tool_input:
        return None
    return [{"type": "content", "content": {"type": "text", "text": redact(tool_input)}}]
