"""Shared utility functions for dashboard chat modules.

Redaction, model normalization, queue operations, stream chunk building,
persona injection, and other helpers used across chat_*.py modules.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMEvent

from kiro_crew.dashboard.state import (
    BUSY_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_PREFIX,
    EMPTY_RESPONSE_RECOVERY_PREFIX,
    MANUAL_RESUME_RECOVERY_PREFIX,
    POSTTOKEN_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    DashboardState,
    _ChatSlot,
    _normalize_slot_key,
    parse_cls_meta,
)
from kiro_crew.hooks import safe_read_file
from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced
from kiro_crew.validation import (
    MAX_TOOL_NAME_LEN,
    THEME_CONSENT_SHA_RE,
    sanitize_string,
)

logger = logging.getLogger(__name__)

# Per-turn compaction-failure backoff. See
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

    Failure backoff: the per-turn
    EVENT_COMPACTION_STATUS path has no cooldown of its own — kiro-cli can
    re-attempt (and re-fail) auto-compaction every single turn while context
    stays over threshold, which would append a near-identical
    "Compaction failed: unknown error" notice each time with no backoff. A
    per-slot consecutive-failure streak and a short cooldown avoid that:
    the first couple of failures are shown as-is (so the user sees it's
    happening), then subsequent failures within the cooldown window are
    suppressed from the chat (still logged server-side via
    AcpClient._handle_compaction_status) until the cooldown elapses, at which
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
        # Reset the context meter — the provider dropped its stale counts when
        # the completed status arrived (AcpPromptStats.reset_after_compaction),
        # and `reset` tells the frontend to delete its stored token counts too
        # (same contract as the threshold auto-compact path in
        # DashboardState.wire_session_compact_callback). Without this the bar
        # kept showing the pre-compaction usage until the next turn.
        state.broadcast_context_usage(slot.key, {"slot": slot.key, "pct": 0.0, "reset": True})
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
    """Canonical history key for a dashboard chat slot.

    Takes a SLOT KEY, never a session key: the ``dashboard:`` prefix it adds is
    unconditional, so feeding it a channel session key yields the nonexistent
    ``dashboard:slack:<ts>``. Slots whose conversation lives on a channel must
    go through :func:`effective_session_key` instead.
    """
    if slot_key.startswith("dashboard:"):
        return slot_key
    while slot_key.startswith("dashboard_"):
        slot_key = slot_key[len("dashboard_"):]
    return f"dashboard:{slot_key}"


def dashboard_slot_key(session_key: str) -> str:
    """The dashboard slot name displaying *session_key*, or ``""`` if none.

    Answers "which tab shows this conversation?" — the question that
    ``session_key.startswith("dashboard:")`` plus a prefix strip approximates,
    and gets wrong for a channel-born conversation, whose session key is the
    channel's own even while its tab is open.

    Use it wherever dashboard behaviour is gated on the user having a tab to
    receive it: routing a notice, addressing a card, honouring a dashboard-only
    directive.
    """
    if session_key.startswith("cron:"):
        # A cron-born tab is named ``cron-<job_id>`` (see cron_inject.py), which
        # is NOT the session key folded: ``_normalize_slot_key`` turns
        # ``cron:<id>`` into ``cron_<id>`` (underscore), a slot that has never
        # existed. Consumers that trusted the fold — sub-agent completion
        # injection, compaction/recycle notices — silently missed the open cron
        # tab ("parent slot cron_<id> gone, notification only"), so agent
        # results reached the bell icon but never the conversation.
        #
        # Per-run execution keys carry extra segments — ``cron:<job_id>:<run_id>``
        # for stateless jobs, ``cron:<job_id>:<agent>`` for agent sequences —
        # while the surface registry only ever holds the slot's linked key
        # (``cron:<job_id>``), so the surface gate is checked against both
        # spellings. Whichever matched, the displaying tab is the job's own.
        job_id = session_key.removeprefix("cron:").split(":", 1)[0]
        if not (
            has_dashboard_surface(session_key) or has_dashboard_surface(f"cron:{job_id}")
        ):
            return ""
        return _normalize_slot_key(f"cron-{job_id}")
    if not has_dashboard_surface(session_key):
        return ""
    return _normalize_slot_key(session_key)


def subagent_event_slot(parent_session_key: str) -> str:
    """The ``slot`` value a per-slot WS event must carry for *parent_session_key*.

    The frontend routes ``subagent_*`` / ``batch_finished`` frames by EXACT
    match between the frame's ``slot`` and the tab's slot key, so a bare
    ``removeprefix("dashboard:")`` breaks every non-dashboard parent: a
    cron-born tab is named ``cron-<id>`` while its session key is
    ``cron:<id>``, and a channel-born tab is named by its transcript stem
    (``slack_<ts>``) while its session key stays ``slack:<ts>``. Frames tagged
    with those raw keys route to a slot no tab reads — the Subagents panel
    showed "No subagents running" for the entire life of every agent spawned
    from such a session.

    :func:`dashboard_slot_key` owns the real mapping; fall back to the old
    prefix-strip when it answers ``""`` (no open tab — nothing routes anywhere
    either way, but keeping the raw key preserves the historical payload for
    external WS consumers and log lines).
    """
    return dashboard_slot_key(parent_session_key) or parent_session_key.removeprefix("dashboard:")


def slot_transcript_key(slot_key: str) -> str:
    """Transcript key for a slot known only by NAME, with no slot object yet.

    Used by the restore paths, which build slots *from* disk and so cannot ask a
    slot for its own key. A channel-born slot's name is its transcript's
    filename stem (``slack_1785370133.085469``), and ``history._safe_key`` folds
    both that stem and the live ``slack:1785370133.085469`` onto the same
    ``.jsonl`` — so the stem already addresses the channel transcript and needs
    no translation.

    This resolves the FILE only. The session key cannot be recovered this way
    (``_safe_key`` folds every ``:`` to ``_``, so ``discord_a_b_c`` is ambiguous)
    and is read back from the persisted ``linked_session_key`` instead.
    """
    if is_channel_session_key(slot_key):
        return slot_key
    return _history_key_for(slot_key)


def slot_history_key(slot: _ChatSlot) -> str:
    """The TRANSCRIPT key for *slot* — the file its conversation is stored in.

    Differs from :func:`effective_session_key` in exactly one case, and that
    case is a real one: a channel-born slot the dashboard could not bind.
    ``surface_channel_session`` deliberately surfaces such a slot **unbound**
    when ``channel_key_for_stem`` cannot resolve its key (the session map was
    pruned, or the thread predates it), because guessing would route replies to
    a session the channel never reads. For that slot ``linked_session_key`` is
    empty, so ``effective_session_key`` falls back to ``_history_key_for``,
    which prefixes ``dashboard:`` and names a file NO restore path reads —
    while every read path resolves the same slot through
    :func:`slot_transcript_key` and gets the channel transcript. Reads and
    writes then address different files: a close flag, a fork, or a backfill
    lands on (or is looked for in) a phantom transcript.

    Resolving the fallback through :func:`slot_transcript_key` puts both back on
    one file. Deliberately does NOT change the slot's SESSION identity — an
    unbound channel slot keeps running under ``dashboard:<name>``, so approval
    policy and restricted-key bookkeeping keyed on that prefix stay intact.

    Gated on the slot's ``channel_origin`` provenance, NOT on its name's shape.
    A name is not provenance: ``POST /api/chat/slots`` accepts a client-supplied
    slot name, so keying off the ``slack_<ts>`` shape alone would let a fresh
    dashboard conversation write itself into an existing thread's transcript and
    merge two unrelated histories. Only the paths that adopt an EXISTING channel
    conversation (``surface_channel_session``, the restore, a History resume)
    set the flag.

    Use this wherever a slot is turned into a transcript path; use
    :func:`effective_session_key` where a slot is turned into a session.
    """
    linked = getattr(slot, "linked_session_key", "")
    if linked:
        return linked
    if getattr(slot, "channel_origin", False):
        return slot_transcript_key(slot.key)
    return _history_key_for(slot.key)


def effective_session_key(slot: _ChatSlot) -> str:
    """The session key for *slot* — the session its turns run on.

    A channel-born slot carries the real channel key (``slack:<ts>``) in
    ``linked_session_key``, so its turns run on the channel's own session and
    its transcript IS the channel transcript: ``history._safe_key`` folds
    ``slack:<ts>`` and the ``slack_<ts>`` filename stem onto the same
    ``.jsonl``, so one key addresses both the live session and the file the
    channel side appends to. Everything else derives from the slot key.

    Use this anywhere a slot's SESSION is addressed — resolving the session its
    turns run on, mirroring its links. For the slot's TRANSCRIPT use
    :func:`slot_history_key`, which resolves the unbound-channel-slot case onto
    the file the read paths actually use. Reserve :func:`_history_key_for` for
    the cases that genuinely start from a slot key with no slot in hand.
    """
    return getattr(slot, "linked_session_key", "") or _history_key_for(slot.key)


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


def _maybe_inject_persona(
    message: str,
    color_theme: str,
    is_new: bool,
    theme_consent_sha: str | None = None,
) -> str:
    """Append a theme persona to *message* on the first turn, when an installed
    theme (value ``custom-<slug>``) ships a validated ``persona.md``.

    ALL personas come from installed packs and are gated on **content-bound**
    consent: the caller threads ``theme_consent_sha`` (the sha256 hex the user
    granted in the consent modal, from the frontend), and the pack's persona is
    injected only when it equals sha256 of the persona text actually read from
    disk *now*. A stale hash (e.g. a reinstall rewrote ``persona.md``) or a
    missing hash fails closed, so a never-consented persona can never be
    injected. The legacy boolean ``theme_consent`` request field does not grant
    injection on its own -- consent is content-bound. There is
    no built-in / unconditional persona path."""
    if not is_new:
        return message
    # Installed themes may carry a persona.md (validated at install, §6.5).
    # Persona activation for INSTALLED packs is content-bound: the sha256 the
    # user consented to must equal the hash of the persona text we read now
    # (fail closed on None/mismatch/non-str). This closes the reinstall-swap
    # gap where a client-asserted boolean would inject a never-consented
    # persona after persona.md changed. The THEME_CONSENT_SHA_RE full-match is
    # also a hard guard that only pure 64-hex ASCII ever reaches
    # hmac.compare_digest below (which raises TypeError on non-ASCII).
    if (
        color_theme.startswith("custom-")
        and isinstance(theme_consent_sha, str)
        and THEME_CONSENT_SHA_RE.fullmatch(theme_consent_sha)
    ):
        text = _installed_theme_persona(color_theme[len("custom-"):])
        if text:
            actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if hmac.compare_digest(actual, theme_consent_sha):
                return message + f"\n[THEME PERSONA]\n{text}\n[END THEME PERSONA]\n\n"
    return message


def _installed_theme_persona(slug: str) -> str:
    """Read an installed theme's ``persona.md`` (bounded), or '' if none.

    Defense-in-depth: re-validate the slug (no traversal) even though install
    already did, and cap the length at the install-time bound. A lazy import of
    ``config_dir`` avoids a circular import with the handlers package.
    """
    if not slug or not all(("a" <= c <= "z") or ("0" <= c <= "9") or c == "-" for c in slug):
        return ""
    try:
        from kiro_crew.config.loader import config_dir

        p = config_dir() / "themes" / slug / "persona.md"
        if not p.is_file() or p.is_symlink():
            return ""
        text = safe_read_file(str(p))
        return text[:2000] if text else ""
    except Exception:
        logger.warning("Installed theme persona load failed", exc_info=True)
        return ""


def _maybe_consolidate(state, slot) -> None:
    """Run memory consolidation unless session is restricted."""
    if state.consolidator and not slot.is_restricted:
        state.consolidator.maybe_consolidate(effective_session_key(slot))
    elif state.consolidator and slot.is_restricted:
        sel().log_api_access(
            caller=f"dashboard:{slot.key}", operation="consolidate",
            outcome="denied", source="dashboard",
            resources="restricted_session_block",
        )


def _sync_dashboard_slots(state: "DashboardState") -> None:
    """Publish the open slots' session keys to SessionManager and the surface registry.

    SessionManager uses the set to reap orphaned sessions; the surface registry
    lets layers with no dashboard import ask whether a session has an open tab
    (see :mod:`kiro_crew.session_surface`). A channel-born slot contributes its
    channel key, which is what both consumers must match against.
    """
    keys = {effective_session_key(s) for s in state._slots.values()}
    state.sessions.set_active_dashboard_slots(keys)
    set_dashboard_surfaced(keys)


def _redact_value(v):  # type: ignore[no-untyped-def]
    """Recursively redact any value (str, dict, list, or passthrough)."""
    if isinstance(v, str):
        v, _ = redact_exfiltration_urls(v)
        v, _ = redact_credentials(v)
        return v
    if isinstance(v, dict):
        return _redact_meta(v)
    if isinstance(v, list):
        # Snapshot for the same reason as _redact_meta — the flush thread reads
        # containers the event loop is still appending to.
        return [_redact_value(i) for i in list(v)]
    return v


def _redact_meta(meta: dict) -> dict:
    """Recursively redact string values in meta dict.

    Iterates a SNAPSHOT of the dict, never the live object. ``_redact_meta`` is
    reached from ``_save_slot_to_history``, which runs in the flush executor
    thread while the event loop is still mutating that same message's meta
    (streaming tool calls, growing file-change lists). Iterating ``meta.items()``
    directly therefore raised ``RuntimeError: dictionary changed size during
    iteration``, which propagated out of ``_save_slot_to_history`` and aborted
    the whole slot's save — the transcript for that flush was lost.

    A shallow copy per level suffices: the copy's key set is stable, and nested
    containers get their own snapshot from the recursive call.
    """
    return {k: _redact_value(v) for k, v in list(meta.items())}


def _redact_meta_for_role(role: str, meta: dict) -> dict:
    """Redact meta, but preserve role-specific user-actionable external URLs (e.g. mcp_oauth).

    Lives here (the display-redaction module) rather than in chat_persistence
    because it is called on the EMIT path — see _prepare_messages. The
    dependency runs chat_persistence -> chat_utils, so keeping it here lets both
    the save path and the emit path share one implementation without a cycle.
    """
    if role == "mcp_oauth":
        out: dict = {}
        for k, v in list(meta.items()):
            if k == "oauth_url" and isinstance(v, str):
                # Two gates, and deliberately NOT a third:
                #   1. http(s)-only — a tampered history line can't smuggle a
                #      javascript:/data: URL into <a href>.
                #   2. URL must not embed an actual credential — a legit OAuth
                #      consent URL never carries credential patterns; presence of
                #      one means it's tampered/bogus.
                #
                # The generic EXFIL heuristic is deliberately NOT applied, matching
                # `_oauth_url_contains_credential` (chat_runner.py), whose docstring
                # says it omits the long-query heuristic because that heuristic
                # "would reject every real OAuth URL". test/oauth_url_corpus.py is
                # the contract: real provider URLs routinely exceed 200 query chars
                # and carry a 43-char base64url `code_challenge`, so the exfil
                # heuristic fires on all of them.
                #
                # This function runs on the EMIT path (_prepare_messages), which
                # serves the slot-detail endpoint that the frontend refetches on
                # `chat_done`, on WS reconnect, and on switchSlot. Blanking the URL
                # here therefore hits a PRE-TERMINAL banner: renderMcpOAuthMessage
                # returns null when `oauth_url` is empty and neither completed nor
                # failed is set, so the Authorize banner would silently vanish and
                # the user could never authorize the server. Keeping the two gates
                # aligned is what prevents that.
                lower = v.lower()
                safe_scheme = lower.startswith("https://") or lower.startswith("http://")
                _, hit_cred = redact_credentials(v)
                out[k] = v if (safe_scheme and not hit_cred) else ""
            else:
                out[k] = _redact_value(v)
        return out
    return _redact_meta(meta)


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


# Runner-injected synthetic recovery instructions (defined here — the shared
# utils layer — so BOTH the runner's turn logic and the queue/merge predicates
# below classify them from one source of truth; chat_runner re-exports them).
# The connection-loss and post-transient continuations resume interrupted turns;
# the empty-response nudge breaks the repeated-empty-generation pattern. All are
# orchestration, not user speech.
#
# Each carries a bracketed marker line, matching the recovery prefixes in
# state.py. The marker is what the dashboard matches to fold the row into a
# one-line RecoveryCard instead of printing the machine-facing prose as a
# full-width bubble; it also labels the injection for the model, which reads
# these the same way it reads the refusal/stall continuations.
_CONN_RECOVER_MSG = (
    f"{CONN_RECOVERY_PREFIX}\n"
    "Your previous turn was interrupted by a lost backend connection and has "
    "been automatically recovered. This was NOT a user action — do not treat "
    "it as a cancellation or interruption by the user. The work already done "
    "above is preserved in the conversation. Continue from where it stopped "
    "and finish the request — do not restart it or repeat steps or tools that "
    "already completed successfully."
)
_BUSY_RECOVER_MSG = (
    f"{BUSY_RECOVERY_PREFIX}\n"
    "Your previous turn was interrupted because the backend session was still "
    "busy, so the session was reset and the turn automatically recovered. This "
    "was NOT a user action — do not treat it as a cancellation or interruption "
    "by the user. The work already done above is preserved in the "
    "conversation. Continue from where it stopped and finish the request — do "
    "not restart it or repeat steps or tools that already completed "
    "successfully."
)
_POSTTOKEN_RECOVER_MSG = (
    f"{POSTTOKEN_RECOVERY_PREFIX}\n"
    "The previous response was interrupted partway through by a transient "
    "backend error. The work already done above (including any completed tool "
    "results) is preserved in the conversation. Continue from where it stopped "
    "to finish the original request — do NOT restart from scratch and do NOT "
    "re-run steps or tools that already completed successfully."
)
_EMPTY_AUTO_CONTINUE_MSG = (
    f"{EMPTY_RESPONSE_RECOVERY_PREFIX}\n"
    "Your previous turn produced no output (the model returned an empty "
    "response twice). Continue working on the pending request from the "
    "conversation above and respond now — do NOT restart from scratch and do "
    "NOT re-run steps or tools that already completed successfully."
)
_SYNTHETIC_RECOVERY_MSGS = (
    _CONN_RECOVER_MSG,
    _BUSY_RECOVER_MSG,
    _POSTTOKEN_RECOVER_MSG,
    _EMPTY_AUTO_CONTINUE_MSG,
)
# Injected when the USER presses Continue on an interrupted turn. Worded to be
# TRUE in both interruption shapes, which is why the endpoint needs no branch:
# a turn that streamed partway and one that produced nothing at all read this
# same text correctly. It must not assert that completed work exists above —
# _POSTTOKEN_RECOVER_MSG does ("The work already done above ... is preserved"),
# and after a gateway restart mid-first-turn that is simply false, which would
# point the model at progress it cannot find.
_MANUAL_RESUME_MSG = (
    f"{MANUAL_RESUME_RECOVERY_PREFIX}\n"
    "The previous turn was interrupted before it finished (a dropped "
    "connection, a restart, or a backend error) and the user has asked you to "
    "carry on. Look at the conversation above, work out what was already "
    "completed, and finish the user's most recent request from there. Do NOT "
    "re-run steps or tools that already completed successfully, and do NOT "
    "assume any particular progress was made — if nothing was done yet, simply "
    "start the request now."
)
# Injected when the user presses Continue on a slot whose last turn ended
# NORMALLY. Continue is offered on any idle slot with a transcript (a killed
# gateway writes no error row, so an interrupted turn can be shape-identical to
# a clean one — see ``_is_interrupted``), which means the button must also have
# something true to say when nothing was actually cut short. Sharing
# ``MANUAL_RESUME_RECOVERY_PREFIX`` is deliberate: to the user the two are one
# button, so they must fold into the same RecoveryCard.
#
# The closing sentence is load-bearing. Without an explicit licence to say "this
# is done", a model handed a bare "keep going" on a finished thread invents
# follow-up work to justify the turn.
_MANUAL_CONTINUE_MSG = (
    f"{MANUAL_RESUME_RECOVERY_PREFIX}\n"
    "The user pressed Continue without typing a new instruction. Look at the "
    "conversation above and carry on with their most recent request: take the "
    "next step that was still outstanding, or finish anything left half-done. "
    "Do NOT re-run steps or tools that already completed successfully. If the "
    "request is genuinely complete, say so in one line instead of inventing "
    "further work."
)


class ResetCause(str, Enum):
    """Why a turn's session had to be reset, which selects the continuation the
    requeue carries — and so the row the transcript renders.

    A closed set rather than a boolean or a caller-supplied string: every reset
    site must state its cause, and a site added later cannot silently inherit
    another cause's user-facing label.

    ``str`` mixin (not ``StrEnum``) for Py3.10 compat, matching ``KindSupport``.
    """

    CONNECTION_LOST = "connection_lost"
    SESSION_BUSY = "session_busy"


#: The continuation each cause resumes with once the turn has emitted output.
_CONTINUATION_BY_CAUSE = {
    ResetCause.CONNECTION_LOST: _CONN_RECOVER_MSG,
    ResetCause.SESSION_BUSY: _BUSY_RECOVER_MSG,
}


def build_recovery_requeue(
    message: str, turn_emitted: bool, cause: ResetCause, *, message_is_synthetic: bool
) -> tuple[str, RecoveryPayload]:
    """Choose the prompt for a reset-and-requeue recovery, and label its provenance.

    Once output or a tool call has been emitted, replaying the original request
    can repeat side effects. A continuation instead resumes from restored
    conversation state. Before any output, the original request is safe and is
    still required for the model to begin the work.

    That decision is the same for every cause, but the continuation is not:
    ``cause`` is required because the marker it carries is what the transcript
    renders, and a session that was merely busy must not be reported as a lost
    connection.

    The text and its label are returned together because choosing them apart is how
    they drifted. Replaying ``message`` unchanged only means "the user's own words"
    when this turn was not itself a recovery: a second consecutive failure before any
    output re-queues the runner's previous continuation, so ``turn_emitted`` alone
    cannot say whose words these are. ``message_is_synthetic`` carries that from the
    queue entry that produced the turn, and is required for the same reason ``cause``
    is — a requeue site added later must not silently inherit "the user said this".
    """
    if turn_emitted:
        return _CONTINUATION_BY_CAUSE[cause], RecoveryPayload.CONTINUATION
    return message, payload_for_replay(message_is_synthetic)


def is_system_injection(content: str) -> bool:
    """True when a queued message is a system injection (sub-agent completion
    or cron notification) rather than a plain user message.

    Single source of truth for the predicate that decides which queued
    messages keep draining during a sub-agent run (`_dequeue_next_system_message`),
    which break a user-message merge (`_dequeue_next_message`), and which must
    not consume the session-reset notice (chat_runner drain loop).

    Both sub-agent shapes count: the per-agent event and the wave digest, whose
    prefix is a sibling of the per-agent one rather than an extension of it.
    """
    return content.startswith(SUBAGENT_COMPLETION_PREFIXES) or content.startswith(
        CRON_NOTIFY_PREFIX
    )


#: Structural queue-entry kind for runner-injected recovery instructions.
SYNTHETIC_RECOVERY_KIND = "synthetic_recovery"


def is_synthetic_recovery_item(item: dict) -> bool:
    """True when a queue ENTRY is a runner-injected synthetic recovery
    instruction (post-transient CONTINUE / empty-response nudge).

    Classification is structural — the ``kind`` tag set at ``queue_insert``
    time — never content equality: metadata survives any queue transformation
    (merge, prefixing, truncation) and cannot collide with a user pasting the
    transcript-visible recovery text verbatim (which must classify as a plain
    user message)."""
    return item.get("kind") == SYNTHETIC_RECOVERY_KIND


class RecoveryPayload(str, Enum):
    """Whether a recovery entry's TEXT is runner-authored or the user's own words.

    ``build_recovery_requeue`` already draws this line — a continuation once the
    turn emitted output, the original request before that — but both re-queue
    under ``SYNTHETIC_RECOVERY_KIND``, because both must render as an inject row
    rather than a second user bubble. The kind therefore cannot also answer
    whether the text may be mirrored to a linked thread as user speech.

    ``str`` mixin (not ``StrEnum``) for Py3.10 compat, matching ``ResetCause``.
    """

    CONTINUATION = "continuation"
    ORIGINAL = "original"


def payload_for_replay(message_is_synthetic: bool) -> RecoveryPayload:
    """The payload tag for a requeue that replays the incoming ``message`` verbatim.

    Asks the only question such a site has: were these the user's words, or the
    runner's? Branching on ``turn_emitted`` instead was wrong — a recovery turn that
    dies before emitting replays the runner's own continuation, and labelling that
    ORIGINAL mirrors internal orchestration to a linked thread as user speech.
    """
    return RecoveryPayload.CONTINUATION if message_is_synthetic else RecoveryPayload.ORIGINAL


def is_synthetic_payload_item(item: dict) -> bool:
    """True when a queue ENTRY's text was written by the runner, not the user.

    Separate question from :func:`is_synthetic_recovery_item`, which answers where
    the entry came from. An untagged entry falls back to the kind because the two
    errors are not symmetric: mirroring runner text as if the user typed it
    misattributes machine orchestration, while suppressing a mirror only loses an
    echo of something the user can already see.
    """
    payload = item.get("payload")
    if payload:
        return payload == RecoveryPayload.CONTINUATION
    return is_synthetic_recovery_item(item)


def is_system_injection_item(item: dict) -> bool:
    """Item-aware system-injection predicate for queue-entry consumers.

    Synthetic recovery instructions are orchestration, not user speech: they
    must BREAK a user-message merge (folding one into a "[N queued messages
    merged]" turn would flip it back into user-authored, persisted,
    channel-mirrored history), keep draining during sub-agent runs, and never
    consume the session-reset notice — same treatment as sub-agent completion
    and cron injections."""
    return is_synthetic_recovery_item(item) or is_system_injection(item["content"])


def _dequeue_next_message(slot, merge_enabled: bool) -> tuple:
    """Drain the queue: merge non-cron messages or pop the first one."""
    if merge_enabled and len(slot._queue) > 1:
        to_merge: list[dict] = []
        for item in list(slot._queue):
            if is_system_injection_item(item):
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
        if is_system_injection_item(item):
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
            # Gate is `!= "user"`, NOT `not in ("user", "system")`. This is the
            # display-time redaction boundary for everything the slot detail
            # endpoint returns — including the frozen-prefix lines read straight
            # off disk — so it must cover every non-user role. The load path does
            # not redact on load, and `system` content is written to disk
            # unredacted (see _build_message_entry's gate), so excluding it here
            # would emit raw stored bytes.
            # User-authored content stays raw: the user typed it and is the only
            # one who sees it back.
            if role != "user" and text:
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
                msg_out["meta"] = _redact_meta_for_role(role, meta)
            elif isinstance(msg_out.get("meta"), dict):
                # Redact the STORED meta too. Without this branch the stored dict
                # passes through by reference (dict(m) is shallow), so it would
                # reach the client exactly as loaded. This is the only guard on
                # meta for the slot-detail response (the load path does not
                # redact meta).
                msg_out["meta"] = _redact_meta_for_role(role, msg_out["meta"])
            out.append(msg_out)
    if chunk_text:
        redacted_chunk, _ = redact_exfiltration_urls(chunk_text)
        redacted_chunk, _ = redact_credentials(redacted_chunk)
        out.append({"role": "streaming", "content": redacted_chunk, "cls": "msg msg-a"})
    return out
