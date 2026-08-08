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
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMEvent

from kiro_crew.dashboard.state import (
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
from kiro_crew.messaging.link import SLACK_NAMESPACE, is_channel_session_key
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
    if not has_dashboard_surface(session_key):
        return ""
    return _normalize_slot_key(session_key)


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


def mirror_is_paused(state: Any, session_key: str, channel_type: str = "") -> bool:
    """True when a turn must NOT be mirrored to the named channel binding.

    The channel-neutral twin of ``slack_mirror_is_paused``, and per BINDING: a
    session can hold several, so muting Discord must leave a Telegram sibling
    delivering. With no ``channel_type`` it answers for the session as a whole,
    which the storage layer defines as "every binding is muted" — never "any" —
    for exactly that reason.

    The scope is genuinely narrower than Slack's because the hazard Slack has does
    not exist here: no cron result, sub-agent completion, requested file or
    auto-nudge tick reads a mirror binding at all — those address a channel
    explicitly — so gating this predicate cannot reroute a delivery to the owner's
    DM or destroy a monitor loop. It covers the two sites that carry turn output:
    the user echo and the assistant reply.

    Consult it before SENDING, never before routing. A muted binding must stay
    visible to ``find_mirror_sessions``, to the resume-conflict check and to both
    clear paths, or in-channel ``!unlink`` and conflict detection break.

    Strict ``is True`` for the same reason as the Slack gate: ``sessions`` is a
    bare ``MagicMock`` across much of the suite and returns a truthy child for
    any unstubbed accessor, so truthiness here would mute every mirror in the
    test suite. Failing open leaves a muted channel noisy at worst; failing
    closed would make a live one silently dead.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_mirror_paused(session_key, channel_type) is True
    except TypeError:
        # Older/stubbed SessionManagers predate the per-binding argument.
        return sessions.is_mirror_paused(session_key) is True
    except Exception:
        logger.debug("mirror pause lookup failed for %s", session_key, exc_info=True)
        return False


def slack_mirror_is_paused(state: Any, session_key: str) -> bool:
    """True when a turn must NOT be mirrored to the session's linked thread.

    Pause retains the thread binding, so every inbound resolver still sees the
    link and a reply still reaches the session that owns the thread. This is the
    one predicate that tells outbound egress apart from that: consult it before
    sending, never before routing.

    Scope is deliberately turn mirroring -- the user message echo, its tool
    stream, the assistant reply, the compaction notice, and the linked approval
    prompt. Deliveries that merely *reuse* the thread as an address (a cron
    result, a subagent completion, a requested file, an auto-nudge tick) are NOT
    gated: an empty link makes those fall back to the owner's DM, and one of them
    deletes the auto-nudge loop outright, so reading paused as no-link there
    would reroute messages the user asked for and destroy a live monitor.

    Strict ``is True`` rather than truthiness: ``sessions`` is a bare
    ``MagicMock`` in much of the suite and returns a truthy child for any
    unstubbed accessor. Failing open leaves a paused thread noisy at worst;
    failing closed would make a live thread silently dead.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_slack_paused(session_key) is True
    except Exception:
        logger.debug("slack pause lookup failed for %s", session_key, exc_info=True)
        return False


def slack_turn_egress_denied(state: Any, session_key: str) -> bool:
    """Has channel governance stopped allowing Slack for THIS send?

    The companion to ``slack_mirror_is_paused``, asked at the same three places
    and about the same question — should this turn's output reach Slack — but
    answering the policy half rather than the user half.

    The mirror path re-resolves governance per send inside
    ``_resolve_channel_target``, which deliberately skips Slack because Slack has
    its own client and streaming path rather than a registered transport. That
    left the Slack leg deciding egress on the policy as it stood when the link was
    created: a link made (or RECONNECTED) while Slack was allowed kept posting
    transcript content after the policy denied it. The backfill drain already
    re-checks before every post; this closes the same gap on the turn path.

    Returns True to DENY. Fail-closed on a broken evaluation, matching the
    "channels"-scope gates elsewhere: this is an egress chokepoint on a network
    surface, so a degraded answer must not read as permission.

    Note the asymmetry with the pause lookup above, which fails OPEN. That one
    guards against a bare ``MagicMock`` making every thread look muted; this one
    guards a network send, where the safe direction is the other way. A policy
    that cannot be evaluated is not a policy that permits.
    """
    try:
        from kiro_crew.platform.context import PlatformCompositionError
        from kiro_crew.platform.governance_profiles import vet_and_audit
    except Exception:  # pragma: no cover - import-time composition failure
        logger.debug("slack egress: governance module unavailable", exc_info=True)
        return True
    try:
        decision = vet_and_audit(
            "channels",
            SLACK_NAMESPACE,
            session_key=session_key,
            tool_name="chat.slack_turn",
            fail_closed=True,
        )
    except PlatformCompositionError:
        # An invalid governance ceiling is re-raised by design elsewhere; here the
        # caller is a delivery leg with nowhere to surface it, so it denies.
        logger.warning("slack egress denied: governance ceiling is invalid")
        return True
    except Exception:
        logger.debug("slack egress: governance check failed", exc_info=True)
        return True
    if not getattr(decision, "permitted", False):
        logger.info("slack egress denied by channels governance policy")
        return True
    return False


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
# The post-transient CONTINUE resumes an interrupted turn; the empty-response
# nudge breaks the repeated-empty-generation pattern. Both are orchestration,
# not user speech.
#
# Each carries a bracketed marker line, matching the three recovery prefixes in
# state.py. The marker is what the dashboard matches to fold the row into a
# one-line RecoveryCard instead of printing the machine-facing prose as a
# full-width bubble; it also labels the injection for the model, which reads
# these the same way it reads the refusal/stall continuations.
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
_SYNTHETIC_RECOVERY_MSGS = (_POSTTOKEN_RECOVER_MSG, _EMPTY_AUTO_CONTINUE_MSG)
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
