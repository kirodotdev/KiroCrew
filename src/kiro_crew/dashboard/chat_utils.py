"""Shared utility functions for dashboard chat modules.

Redaction, model normalization, queue operations, stream chunk building,
persona injection, and other helpers used across chat_*.py modules.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMEvent

from kiro_crew.agent import _BUNDLED_CFG_DIR, _shipped_prompt
from kiro_crew.dashboard.state import (
    CRON_NOTIFY_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    DashboardState,
    _ChatSlot,
    parse_cls_meta,
)
from kiro_crew.hooks import safe_read_file
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.validation import MAX_TOOL_NAME_LEN, sanitize_string

logger = logging.getLogger(__name__)

# Per-turn compaction-failure backoff (Mesh compaction-spam fix). See
# _broadcast_compaction_result for the full rationale. Kept small: this is a
# UX/spam guard, not a correctness gate — the underlying compaction attempt
# still runs (or fails) on kiro-cli's own schedule every turn; we only
# control how often we *tell the user about it*.
_COMPACTION_NOTICE_SHOW_FIRST_N = 2
_COMPACTION_FAIL_COOLDOWN_SECS = 60.0


def _redact_deep(obj):
    """Recursively redact all string values in a nested structure."""
    if isinstance(obj, str):
        obj, _ = redact_exfiltration_urls(obj)
        obj, _ = redact_credentials(obj)
        return obj
    if isinstance(obj, dict):
        return {k: _redact_deep(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact_deep(v) for v in obj]
    return obj


# 1 MB safety cap on persisted/broadcast tool fields. The inline detail panel
# is the only place users see what an agent is about to run, so we keep this
# generous — well past every realistic tool input. Anything above 1 MB is
# almost certainly runaway log spam; truncate with a visible sentinel so the
# user can tell the value was capped.
_MAX_TOOL_FIELD = 1_000_000
_MAX_TOOL_PURPOSE = 8_000  # purpose is a short label — no scenario for more


def _redact_tool_field(text: str | None, *, limit: int = _MAX_TOOL_FIELD) -> str:
    """Redact + apply 1 MB safety cap to a tool input/output field. Used for
    both the persisted message meta and the live WS broadcast so the live UI
    and the post-reload UI see the same content."""
    if not text:
        return ""
    if len(text) * 4 > limit:
        encoded = text.encode("utf-8")
        if len(encoded) > limit:
            # errors="ignore" cleanly drops a partial trailing multi-byte
            # sequence at the cut point.
            text = encoded[:limit].decode("utf-8", errors="ignore") + f"\n… [truncated at {limit:,} bytes]"
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _build_stream_chunk(msg: dict) -> str:
    """Build a JSON SSE chunk from a slot message, with meta redaction for permissions."""
    try:
        meta = parse_cls_meta(msg.get("cls", "")) if msg.get("role") == "permission" else None
    except Exception:
        logger.warning("Failed to parse cls meta for permission message", exc_info=True)
        meta = None
    if meta:
        meta = _redact_deep(meta)
    content = msg.get("content", "")
    if isinstance(content, str):
        content, _ = redact_exfiltration_urls(content)
        content, _ = redact_credentials(content)
    else:
        content = _redact_deep(content)
    cls_val = msg.get("cls", "")
    if isinstance(cls_val, str):
        cls_val, _ = redact_exfiltration_urls(cls_val)
        cls_val, _ = redact_credentials(cls_val)
    else:
        cls_val = _redact_deep(cls_val)
    return json.dumps(
        {"type": msg.get("role", ""), "content": content, "ts": msg.get("ts", ""),
         "cls": cls_val,
         **({"meta": meta} if meta else {})}
    )


def _extract_bash_command(tool_input: str) -> str:
    """Extract the command string from execute_bash tool_input (JSON or raw)."""
    try:
        data = json.loads(tool_input)
        if isinstance(data, dict):
            return data.get("command", "")
    except (json.JSONDecodeError, TypeError):
        pass
    return tool_input


# Deprecated -1m model aliases → base model (Anthropic 1M GA, April 2026)
_DEPRECATED_MODEL_MAP = {
    "claude-opus-4.6-1m": "claude-opus-4.6",
    "claude-sonnet-4.6-1m": "claude-sonnet-4.6",
}


def _normalize_model(name: str) -> str:
    """Map deprecated model names to their replacements."""
    return _DEPRECATED_MODEL_MAP.get(name, name)


def is_deprecated_model(name: str) -> bool:
    """Check if a model name is deprecated (public API for cross-module use)."""
    return name in _DEPRECATED_MODEL_MAP


# kiro-cli slash command root words
_SLASH_COMMANDS = frozenset(
    {
        "/agent",
        "/changelog",
        "/chat",
        "/clear",
        "/code",
        "/compact",
        "/context",
        "/editor",
        "/exit",
        "/experiment",
        "/goal",
        "/help",
        "/hooks",
        "/issue",
        "/logdump",
        "/mcp",
        "/model",
        "/paste",
        "/prompts",
        "/q",
        "/quit",
        "/reply",
        "/side",
        "/tangent",
        "/todos",
        "/tools",
        "/usage",
    }
)

_BLOCKED_SLASH_COMMANDS = frozenset(
    {"/quit", "/exit", "/q", "/chat", "/paste", "/reply", "/editor"}
)

# Single source of truth for slash-command descriptions surfaced by the
# dashboard API (GET /api/slash-commands) and mirrored by the frontend
# autocomplete fallback. Keys are slash-prefixed command names. Covers every
# command in _SLASH_COMMANDS plus the claude_code-only /init, /review, and
# /security-review so no command renders a blank description in either path.
SLASH_COMMAND_DESCRIPTIONS: dict[str, str] = {
    "/agent": "Switch or manage the active agent",
    "/changelog": "Show the release changelog",
    "/chat": "Save or load a chat session",
    "/clear": "Clear conversation history",
    "/code": "Open code intelligence tools",
    "/compact": "Compact conversation to free context",
    "/context": "Manage context files and token usage",
    "/editor": "Compose your prompt in an external editor",
    "/exit": "Exit the chat session",
    "/experiment": "Toggle experimental features",
    "/goal": "Set a standing goal the agent works toward across turns",
    "/help": "Show available commands",
    "/hooks": "View configured context hooks",
    "/init": "Initialize project context",
    "/issue": "Report an issue or bug",
    "/logdump": "Dump session logs to a file",
    "/mcp": "Show configured MCP servers",
    "/model": "Show or switch the current model",
    "/paste": "Paste an image from the clipboard",
    "/prompts": "List or invoke saved prompts & agent SOPs",
    "/q": "Quit the chat session",
    "/quit": "Quit the chat session",
    "/reply": "Reply to the last assistant message",
    "/review": "Review code changes",
    "/security-review": "Run a security review",
    "/side": "Open a side conversation panel",
    "/tangent": "Start a tangent conversation",
    "/todos": "Show or manage the task list",
    "/tools": "Show available tools",
    "/usage": "Show billing and usage information",
    "/workflows": "List and manage dynamic workflow runs",
}


def _broadcast_auto_tool(state: DashboardState, slot: _ChatSlot, event: "LLMEvent") -> str:
    """Broadcast an auto-approved tool call via WS with redacted title. Returns redacted title."""
    title, _ = redact_exfiltration_urls(event.title)
    title, _ = redact_credentials(title)
    kind, _ = redact_exfiltration_urls(event.tool_kind)
    kind, _ = redact_credentials(kind)
    tcid, _ = redact_exfiltration_urls(event.tool_call_id or "")
    tcid, _ = redact_credentials(tcid)
    state.broadcast_ws(
        "tool_call",
        {
            "slot": slot.key, "tool": title, "kind": kind, "auto": True, "tool_call_id": tcid,
            "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
            "input_preview": _redact_tool_field(event.tool_input),
        },
    )
    return title


def _append_compaction_notice(
    state: DashboardState, slot: _ChatSlot, msg_text: str
) -> None:
    """Append a compaction status notice as an assistant message and broadcast it.

    The notice is tagged ``kind="compaction"`` so the dashboard can tell it apart
    from a real assistant turn. Follow-up ``[OPTIONS:]`` buttons are derived by
    scanning backward for the last assistant message; without this marker the
    scan stops on this option-less notice and hides the buttons of the turn it
    follows (see ChatPage ``deriveFollowUpOptions``). ``meta.kind`` survives a
    history reload; the top-level ``kind`` covers the live websocket path.

    This is the single chokepoint for emitting a compaction notice — every
    compaction path (auto-compaction status events and the ``/compact`` slash
    command, the kiro backend and the dormant claude seam alike) must route
    through here so the tag is never accidentally dropped.

    Defense-in-depth: callers already redact, but since this chokepoint posts to
    an external surface (the dashboard websocket) the redaction is reapplied here
    so a future caller passing unredacted LLM-derived text (e.g. a compaction
    summary) can never leak a credential/exfil URL. Both passes are idempotent.
    """
    msg_text, _ = redact_credentials(msg_text)
    msg_text, _ = redact_exfiltration_urls(msg_text)
    meta = {"kind": "compaction"}
    slot.append("assistant", msg_text, "msg msg-a", meta=meta)
    state.broadcast_ws(
        "chat_message",
        {
            "slot": slot.key,
            "role": "assistant",
            "content": msg_text,
            "kind": "compaction",
            "meta": meta,
        },
    )


def _broadcast_compaction_result(
    state: DashboardState, slot: _ChatSlot, event: "LLMEvent"
) -> str | None:
    """Broadcast compaction completed/failed to the slot. Returns message text or None.

    Failure backoff (Mesh compaction-spam fix): the per-turn
    EVENT_COMPACTION_STATUS path has no cooldown of its own — kiro-cli can
    re-attempt (and re-fail) auto-compaction every single turn while context
    stays over threshold, previously appending a near-identical
    "Compaction failed: unknown error" notice each time with no backoff. We
    now track a per-slot consecutive-failure streak and a short cooldown:
    the first couple of failures are shown as-is (so the user sees it's
    happening), then subsequent failures within the cooldown window are
    suppressed from the chat (still logged server-side via
    AcpClient._log_compaction_status) until the cooldown elapses, at which
    point a single collapsed notice reports the streak length instead of
    repeating the same line indefinitely.
    """
    status_type = event.text
    if status_type == "completed":
        slot._compaction_fail_streak = 0
        slot._compaction_fail_cooldown_until = 0.0
        summary, _ = redact_credentials(event.title)
        summary, _ = redact_exfiltration_urls(summary)
        msg_text = (
            f"✅ Conversation compacted: {summary}" if summary else "✅ Conversation compacted."
        )
    elif status_type == "failed":
        now = time.monotonic()
        slot._compaction_fail_streak += 1
        streak = slot._compaction_fail_streak

        if streak > _COMPACTION_NOTICE_SHOW_FIRST_N and now < slot._compaction_fail_cooldown_until:
            # Suppress: still within cooldown after we already told the user
            # once/twice. Nothing new to say — don't spam identical notices.
            return None

        error, _ = redact_credentials(event.title or "unknown error")
        error, _ = redact_exfiltration_urls(error)
        if streak <= _COMPACTION_NOTICE_SHOW_FIRST_N:
            msg_text = f"❌ Compaction failed: {error}"
        else:
            # Cooldown just elapsed after 1+ suppressed repeats — collapse
            # into one message instead of resuming per-turn spam.
            msg_text = (
                f"❌ Compaction has failed {streak}x in a row "
                f"({error}) — this conversation may be too large to "
                "auto-compact. Consider `/compact` manually or starting a "
                "new chat if this persists."
            )
        slot._compaction_fail_cooldown_until = now + _COMPACTION_FAIL_COOLDOWN_SECS
    else:
        return None
    _append_compaction_notice(state, slot, msg_text)
    return msg_text


def _emit_agent_assignment(slot_key: str, agent: str, outcome: str = "applied") -> None:
    """Emit a SEL audit event when an agent is set, changed, or rejected on a slot."""
    sel().log(
        SecurityEvent(
            event_id=uuid.uuid4().hex,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type="agent_assignment",
            caller_identity=f"dashboard:{slot_key}",
            agent=agent,
            source="dashboard",
            operation="slot_agent_set",
            outcome=outcome,
            resources=f"slot={slot_key}",
        )
    )


def _validate_tool_name(tool_name: str, *, is_shell: bool = False) -> str:
    """Validate and sanitize tool display names for hook matching.

    ``is_shell`` is the provider-agnostic signal (set at the provider boundary)
    that this tool call is a shell/exec command, whose display title is the full
    command line and legitimately exceeds the length cap. Keying the exemption
    on this flag rather than a hardcoded set of provider tool_kind literals
    (e.g. "execute"/"Bash") stops the cap from silently re-breaking long shell
    commands on every engine migration or tool rename.
    """
    sanitized = sanitize_string(tool_name)
    if not sanitized:
        raise ValueError("Tool name cannot be empty")
    if not is_shell and len(sanitized) > MAX_TOOL_NAME_LEN:
        raise ValueError(f"Tool name exceeds max length {MAX_TOOL_NAME_LEN}")
    return sanitized


def _history_key_for(slot_key: str) -> str:
    """Canonical history key for a dashboard chat slot."""
    if slot_key.startswith("dashboard:"):
        return slot_key
    while slot_key.startswith("dashboard_"):
        slot_key = slot_key[len("dashboard_"):]
    return f"dashboard:{slot_key}"


_INCOGNITO_PREFIX = (
    "[INCOGNITO SESSION] This is an ephemeral session. "
    "Do NOT call learn_add or any memory-writing tool. "
    "learn_remove and cron tools are allowed (active user actions). "
    "If the user asks to save a lesson, respond: "
    "'⚠️ Incognito mode — lessons are not saved in this session.'\n\n"
)

_TEMPORARY_PREFIX = (
    "[TEMPORARY SESSION] This is a blank-slate ephemeral session. "
    "The user has explicitly chosen ephemeral mode. "
    "There are NO memory reads or writes — no preferences, no history, "
    "no lessons, no episodic memory, no projects. "
    "Do NOT reference prior conversations or stored preferences. "
    "Do NOT call learn_add, learn_list, or any memory tool. "
    "Treat this as a completely fresh conversation with no prior context.\n\n"
)


def _apply_incognito_prefix(slot, message: str) -> str:
    """Prepend incognito/temporary instruction for non-persistent sessions."""
    if slot.memory_mode == "temporary":
        return _TEMPORARY_PREFIX + message
    if slot.memory_mode == "incognito":
        return _INCOGNITO_PREFIX + message
    return message


# Theme persona registry: color_theme slug -> (display tag, persona filename).
# Adding a persona-backed theme is a single entry here plus dropping the
# matching config/persona-<slug>.md (auto-packaged via the config/persona-*.md
# glob in setup.cfg). No new loader function or branching required.
_THEME_PERSONAS: dict[str, tuple[str, str]] = {
    "lumon": ("LUMON PERSONA", "persona-lumon.md"),
    "lcars": ("LCARS PERSONA", "persona-lcars.md"),
    "bikini-bottom": ("KAREN PERSONA", "persona-bikini-bottom.md"),
    "knight-rider": ("KITT PERSONA", "persona-knight-rider.md"),
}


def _maybe_inject_persona(message: str, color_theme: str, is_new: bool) -> str:
    """Append a theme persona to *message* on the first turn, when the theme
    declares one in ``_THEME_PERSONAS``."""
    if not is_new:
        return message
    spec = _THEME_PERSONAS.get(color_theme)
    if spec is None:
        return message
    tag, filename = spec
    try:
        text = _cached_persona(filename)
        if text:
            return message + f"\n[{tag}]\n{text}\n[END {tag}]\n\n"
        return message
    except Exception:
        logger.warning("Persona injection failed", exc_info=True)
        return message


@functools.lru_cache(maxsize=8)
def _cached_persona(filename: str) -> str:
    """Load and cache a shipped persona file by name (config/<filename>)."""

    # Defense-in-depth: this helper is importable and LRU-cached, so reject any
    # filename that could escape the config dir (path traversal / absolute path)
    # even though current callers only pass hardcoded _THEME_PERSONAS values.
    if "/" in filename or "\\" in filename or ".." in filename:
        raise ValueError(f"Invalid persona filename: {filename}")

    _p = _shipped_prompt().parent / filename
    if not _p.is_file():
        _p = _BUNDLED_CFG_DIR / filename
    return safe_read_file(str(_p))


def _maybe_consolidate(state, slot) -> None:
    """Run memory consolidation unless session is restricted."""
    if state.consolidator and not slot.is_restricted:
        state.consolidator.maybe_consolidate(_history_key_for(slot.key))
    elif state.consolidator and slot.is_restricted:
        sel().log_api_access(
            caller=f"dashboard:{slot.key}", operation="consolidate",
            outcome="denied", source="dashboard",
            resources="restricted_session_block",
        )


def _sync_dashboard_slots(state: "DashboardState") -> None:
    """Push current slot keys to SessionManager so orphaned sessions get reaped."""
    state.sessions.set_active_dashboard_slots(
        {_history_key_for(k) for k in state._slots}
    )


def _redact_for_display(text: str) -> str:
    """Apply all redaction passes for dashboard/WS display."""
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _remove_queued_by_id(messages: list[dict], queue_id: str) -> bool:
    """Remove a 'queued' placeholder by queue_id stored in cls JSON."""
    for i, m in enumerate(messages):
        if m.get("role") != "queued":
            continue
        try:
            cls = json.loads(m.get("cls", "{}"))
            if cls.get("queue_id") == queue_id:
                del messages[i]
                return True
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def _edit_queued_by_id(messages: list[dict], queue_id: str, content: str) -> bool:
    """Update the content of a 'queued' placeholder by queue_id stored in cls JSON."""
    for m in messages:
        if m.get("role") != "queued":
            continue
        try:
            cls = json.loads(m.get("cls", "{}"))
            if cls.get("queue_id") == queue_id:
                m["content"] = content
                return True
        except (json.JSONDecodeError, TypeError):
            pass
    return False


def is_system_injection(content: str) -> bool:
    """True when a queued message is a system injection (sub-agent completion
    or cron notification) rather than a plain user message.

    Single source of truth for the predicate that decides which queued
    messages keep draining during a sub-agent run (`_dequeue_next_system_message`),
    which break a user-message merge (`_dequeue_next_message`), and which must
    not consume the session-reset notice (chat_runner drain loop).
    """
    return content.startswith(SUBAGENT_COMPLETION_PREFIX) or content.startswith(
        CRON_NOTIFY_PREFIX
    )


def _dequeue_next_message(slot, merge_enabled: bool) -> tuple:
    """Drain the queue: merge non-cron messages or pop the first one."""
    if merge_enabled and len(slot._queue) > 1:
        to_merge: list[dict] = []
        for item in list(slot._queue):
            if is_system_injection(item["content"]):
                break
            to_merge.append(item)
        if len(to_merge) > 1:
            del slot._queue[:len(to_merge)]
            merged = "\n\n".join(item["content"] for item in to_merge)
            return f"[{len(to_merge)} queued messages merged]\n\n{merged}", to_merge
    item = slot.queue_pop(0)
    return item["content"], [item]


def _dequeue_next_system_message(slot) -> tuple:
    """Pop the first queued sub-agent-completion or cron injection, leaving
    plain user messages queued.

    Implements the (always-on) queue-during-subagents behavior: while background
    sub-agents run for a slot, a tangential user message is held (not drained)
    so it does not start a main turn mid-run, while system injections that must
    keep flowing (sub-agent completions, cron notifications) are still drained.
    Returns ``(content, [item])`` for the drained item, or ``(None, [])`` when
    only held (user) messages remain queued.
    """
    for i, item in enumerate(slot._queue):
        if is_system_injection(item["content"]):
            popped = slot.queue_pop(i)
            return popped["content"], [popped]
    return None, []


def _prepare_messages(messages: list[dict], running: bool) -> list[dict]:
    """Prepare messages for API response."""
    out: list[dict] = []
    chunk_text = ""
    for m in messages:
        role = m.get("role", "")
        if role == "chunk":
            chunk_text += m.get("content", "")
        elif role == "done":
            continue
        else:
            if chunk_text:
                redacted_chunk, _ = redact_exfiltration_urls(chunk_text)
                redacted_chunk, _ = redact_credentials(redacted_chunk)
                out.append({"role": "streaming", "content": redacted_chunk, "cls": "msg msg-a"})
                chunk_text = ""
            text = m.get("content", "")
            if role not in ("user", "system") and text:
                text, _ = redact_exfiltration_urls(text)
                text, _ = redact_credentials(text)
                m = {**m, "content": text}
            msg_out = dict(m)
            if msg_out.get("variants"):
                msg_out["variants"] = [
                    {**v, "content": redact_credentials(redact_exfiltration_urls(v.get("content", ""))[0])[0]}
                    for v in msg_out["variants"] if isinstance(v, dict)
                ]
            meta = parse_cls_meta(m.get("cls", ""))
            if meta is not None:
                msg_out["meta"] = meta
            out.append(msg_out)
    if chunk_text:
        redacted_chunk, _ = redact_exfiltration_urls(chunk_text)
        redacted_chunk, _ = redact_credentials(redacted_chunk)
        out.append({"role": "streaming", "content": redacted_chunk, "cls": "msg msg-a"})
    return out
