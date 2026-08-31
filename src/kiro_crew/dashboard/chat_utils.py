"""Shared utility functions for dashboard chat modules.

Redaction, model normalization, queue operations, stream chunk building,
persona injection, and other helpers used across chat_*.py modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMEvent
    from kiro_crew.slack.outbound import PostedOptions

from kiro_crew.context_blocks import attributable_user_chars
from kiro_crew.dashboard.state import (
    BUSY_RECOVERY_PREFIX,
    COMPACTION_RECOVERY_PREFIX,
    CONN_RECOVERY_PREFIX,
    CRON_NOTIFY_PREFIX,
    EMPTY_RESPONSE_RECOVERY_PREFIX,
    MANUAL_RESUME_RECOVERY_PREFIX,
    POSTTOKEN_RECOVERY_PREFIX,
    PROMISE_ONLY_RECOVERY_PREFIX,
    SUBAGENT_COMPLETION_PREFIXES,
    DashboardState,
    _ChatSlot,
    _normalize_slot_key,
    append_and_surface,
    parse_cls_meta,
)
from kiro_crew.history import transcript_sort_key
from kiro_crew.hooks import safe_read_file
from kiro_crew.messaging.link import canonical_key, is_channel_session_key
from kiro_crew.quick_prompts import QUICK_PROMPTS
from kiro_crew.security import (
    oauth_url_contains_credential,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.session_surface import has_dashboard_surface, set_dashboard_surfaced
from kiro_crew.slack.outbound import (
    decode_options_token,
    encode_options_token,
    expire_options,
    mark_options_terminal,
    options_edit_lock,
)
from kiro_crew.validation import (
    MAX_TOOL_NAME_LEN,
    THEME_CONSENT_SHA_RE,
    sanitize_string,
)

logger = logging.getLogger(__name__)


async def run_config_write(fn, /, *args, **kwargs):
    """Run a blocking ``config.json`` writer under BOTH config locks.

    Every ``config.json`` read-modify-write must serialize against two writer
    generations at once: the sidecar advisory flock that ``update_config_locked``
    takes (covering CLI / boot-refresh / other-process writers), and the
    loop-side :func:`_get_config_lock` asyncio lock that the dashboard's legacy
    handlers still rely on *alone* (bare ``read_config_for_update`` +
    ``write_config_atomically``). A writer that holds only one of the two can
    interleave with the other family and silently revert its settings from a
    stale snapshot. The memory-settings PUT was such a writer and is not one
    any more: ``handlers/memory.py`` routes every config mutation through this
    helper.

    This helper is the one async entry point that holds both: the asyncio lock
    is acquired on the event loop, then ``fn`` (a sync callable that itself
    routes through ``update_config_locked``) runs in a worker thread so the
    flock wait never blocks the loop. Mirrors the boot-time meta-stamp refresh
    in ``server.py``, which established the pattern.
    """
    from kiro_crew.dashboard.handlers.agents import _get_config_lock  # lazy: import cycle

    async with _get_config_lock():
        # The worker is SHIELDED and drained before the lock is released. A
        # thread cannot be cancelled, so cancelling this coroutine -- a gateway
        # shutdown cancelling the boot migration, a client disconnecting mid-PUT
        # -- would otherwise unwind the `async with` while ``fn`` is still inside
        # its read-modify-write. The next writer would then enter the critical
        # section against a file the previous one is still rewriting, and land a
        # config derived from a snapshot taken before it: the earlier caller's
        # settings silently revert.
        #
        # The invariant is NOT "drain once after a cancellation": it is that once
        # a cancellation has been observed, the lock cannot leave this block until
        # the worker is done. A single drain does not give that -- awaiting the
        # drain is itself a suspension point, so a SECOND cancel (a graceful
        # shutdown escalating after its timeout, which is exactly when a config
        # write is most likely to be in flight) unwinds the `async with` with the
        # thread still writing. Hence the loop: every cancellation is absorbed,
        # and only a finished future ends it.
        #
        # Draining cannot change WHETHER the write happens -- the thread runs to
        # completion either way -- so this only decides whether the lock outlives
        # it. The cancellation is re-raised once, never swallowed.
        fut = asyncio.ensure_future(asyncio.to_thread(fn, *args, **kwargs))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(fut)
            except asyncio.CancelledError:
                cancelled = True
                continue
            break
        if cancelled:
            raise asyncio.CancelledError
        return result


# Per-turn compaction-failure backoff. See
# _broadcast_compaction_result for the full rationale. Kept small: this is a
# UX/spam guard, not a correctness gate — the underlying compaction attempt
# still runs (or fails) on kiro-cli's own schedule every turn; we only
# control how often we *tell the user about it*.
_COMPACTION_NOTICE_SHOW_FIRST_N = 2
_COMPACTION_FAIL_COOLDOWN_SECS = 60.0


def history_corpus_unreadable(code: str = "history_corpus_unreadable") -> Any:
    """The one response for "this session's history corpus could not be read".

    Three handlers independently wrapped a corpus read in `try/except` and, on
    failure, substituted their own local encoding of "there is nothing there" --
    `rotated = []`, `_rotated_head = []`, `all_msgs = []`. Each substitution turns
    a read failure into a SUCCESSFUL response that reports a shorter transcript,
    and this corpus is an index space, so the shortening also shifts every index
    above the missing rows. A reader cannot see it and has nothing to retry, and a
    fork taken at a rendered row lands on a different message.

    Three separate hand-written recoveries is why the same defect was found three
    times in three places. This exists so the recovery is one decision rather than
    a choice each call site re-makes: on a corpus read failure, call this.

    `code` names the surface (the fork path answers `fork_corpus_unreadable`) so a
    client can tell which affordance to retry.
    """
    return web.json_response(
        {
            "error": "this session's history could not be read; please retry",
            "code": code,
        },
        status=503,
    )


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
            text = (
                encoded[:limit].decode("utf-8", errors="ignore")
                + f"\n… [truncated at {limit:,} bytes]"
            )
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _build_stream_chunk(msg: dict, *, include_row_meta: bool = False) -> str:
    """Build a JSON SSE chunk from a slot message, with meta redaction for permissions.

    ``include_row_meta`` carries the row's own durable ``meta`` dict (tool
    input/output, call identity) into the record. Off by default so the ordinary
    SSE / OpenAI-compat stream keeps its "only permission rows carry meta"
    contract; the RELAY drain turns it on, because a relay reader (``_apply_row``)
    rebuilds the local row from this record and would otherwise lose that tool
    correlation permanently on a local refresh (GPT #7693).
    """
    try:
        meta = parse_cls_meta(msg.get("cls", "")) if msg.get("role") == "permission" else None
    except Exception:
        logger.warning("Failed to parse cls meta for permission message", exc_info=True)
        meta = None
    if meta is None and include_row_meta:
        row_meta = msg.get("meta")
        if isinstance(row_meta, dict):
            meta = row_meta
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
        {
            "type": msg.get("role", ""),
            "content": content,
            "ts": msg.get("ts", ""),
            "cls": cls_val,
            **({"meta": meta} if meta else {}),
        }
    )


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
        "/workflow",
    }
)

# Commands that exist in kiro-cli's interactive TUI but cannot work in the
# dashboard (they drive a local terminal: quitting it, pasting from its
# clipboard, opening an editor, or toggling checkpoint modes the dashboard's
# own session model already covers via tabs and /side). Blocked commands are
# rejected before session acquisition AND excluded from the
# GET /api/slash-commands suggestion payload, so every surface hides them at
# once — advertising a command that only yields a warning teaches a gesture
# that does not work.
_BLOCKED_SLASH_COMMANDS = frozenset(
    {"/quit", "/exit", "/q", "/chat", "/paste", "/reply", "/editor", "/tangent"}
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
    "/workflow": "List or run a saved workflow by name",
}


def parse_workflow_command(message: str) -> tuple[str, str] | None:
    """Parse ``/workflow <slug> [input]`` without interpreting the input."""
    parts = message.strip().split(None, 2)
    if not parts or parts[0] != "/workflow":
        return None
    workflow_ref = parts[1] if len(parts) > 1 else ""
    input_text = parts[2] if len(parts) > 2 else ""
    return workflow_ref, input_text


def user_text_span(
    offset: int,
    typed_len: int,
    *,
    quick_prompt: bool,
    prompt_expanded: bool,
) -> tuple[int, int]:
    """WHERE the user's typed text sits in the message handed to ``build_message``.

    Deliberately NOT the same question as how much of the turn is ATTRIBUTABLE to
    the user, which is :func:`attributable_user_chars`. Conflating the two is a live
    trap: a quick-prompt turn credits the user zero characters, so deriving the
    span from the attributable count hands ``build_message`` an EMPTY slice — and
    since that slice is what the quick-prompt matcher reads, the token silently
    stops expanding altogether.

    So a quick prompt reports its REAL typed span (the matcher has to see the
    token; ``build_message`` zeroes the attribution itself once it has expanded),
    while an ``@prompt`` turn — already replaced before this point, so its typed
    text is gone from the message — reports the empty span the attribution rule
    asks for.
    """
    length = (
        typed_len
        if quick_prompt
        else attributable_user_chars(typed_len, prompt_expanded=prompt_expanded)
    )
    return offset, offset + length


# A cross-surface dependent: the option-marker splitter `splitRecommendation` refuses to
# strip a marker off a `/`-leading label, so a sigil added here needs adding there too.
def is_harness_slash_command(first_word: str, *, cc_provider: bool) -> bool:
    """Whether *first_word* should be forwarded to the harness as a command.

    Two rules, and the second exists because of a trap. A member of
    :data:`_SLASH_COMMANDS` is a command on every provider. Under ``claude_code``
    the harness owns its own command set, so ANY leading slash is forwarded —
    except a quick-prompt token, which is not a command at all but a macro
    :meth:`ContextBuilder.build_message` expands into an instruction. Forwarding one
    would hand the harness a command it has no definition for, and the token would
    silently do nothing on that provider while working everywhere else.
    """
    if first_word in _SLASH_COMMANDS:
        return True
    if not (cc_provider and first_word.startswith("/")):
        return False
    return first_word.lower() not in QUICK_PROMPTS


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
            "slot": slot.key,
            "tool": title,
            "kind": kind,
            "auto": True,
            "tool_call_id": tcid,
            "purpose": _redact_tool_field(event.tool_purpose, limit=_MAX_TOOL_PURPOSE),
            "input_preview": _redact_tool_field(event.tool_input),
        },
    )
    return title


def _append_compaction_notice(state: DashboardState, slot: _ChatSlot, msg_text: str) -> None:
    """Append a compaction status notice as an assistant message and broadcast it.

    The notice is tagged ``kind="compaction"`` so the dashboard can tell it apart
    from a real assistant turn. Follow-up ``[OPTIONS:]`` buttons are derived by
    scanning backward for the last assistant message; without this marker the
    scan stops on this option-less notice and hides the buttons of the turn it
    follows (see ChatPage ``deriveFollowUpOptions``). ``meta.kind`` survives a
    history reload; the top-level ``kind`` covers the live websocket path.

    This is the single chokepoint for emitting a compaction notice — every
    compaction path (auto-compaction status events and the ``/compact`` slash
    command, the kiro and claude backends alike) must route
    through here so the tag is never accidentally dropped.

    Defense-in-depth: callers already redact, but since this chokepoint posts to
    an external surface (the dashboard websocket) the redaction is reapplied here
    so a future caller passing unredacted LLM-derived text (e.g. a compaction
    summary) can never leak a credential/exfil URL. Both passes are idempotent.
    """
    msg_text, _ = redact_credentials(msg_text)
    msg_text, _ = redact_exfiltration_urls(msg_text)
    meta = {"kind": "compaction"}
    append_and_surface(
        state, slot, "assistant", msg_text, "msg msg-a", meta=meta, extra={"kind": "compaction"}
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
        slot_key = slot_key[len("dashboard_") :]
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
        if not (has_dashboard_surface(session_key) or has_dashboard_surface(f"cron:{job_id}")):
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


def subagents_attached(
    state: DashboardState, slot: _ChatSlot, session_key: str, operation: str
) -> bool:
    """Whether sub-agent children are attached to *session_key*.

    True means an action that tears the session down, or dispatches into it,
    would discard a child's work. Every such caller shares THIS predicate: a
    second copy is how the probes diverge, and both callers must fail toward
    keeping a child's work.

    Three probes, none optional:

    * ``running_agents_for`` on the true session key. QUEUED children count too:
      a spawn that hit the concurrency/stagger gate is deliberately absent from
      ``_agents`` (see ``SubagentInfo.queued``), yet it WILL start on its own.
    * IN-FLIGHT RESULT DELIVERY: the last child can finish — emptying both
      probes — while its ``[Subagent completion event]`` injection is still
      landing, and that injection needs both the transcript order and the
      session it reports to.
    * Fail closed on a None running-probe: that is the probe FAILING, not a slot
      with no children, and mistaking the two is exactly the hazard this guard
      exists to prevent.

    A state with no ``subagents`` registry answers False — there is no runtime
    for a child to be attached to.
    """
    subs = getattr(state, "subagents", None)
    if subs is None:
        return False
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
    return bool(running is None or running or queued or inflight)


def slack_options_slot(state: DashboardState, session_key: str) -> _ChatSlot | None:
    """The slot holding *session_key*'s Slack OPTIONS state, if one exists.

    Deliberately not routed through :func:`dashboard_slot_key`, which answers
    "is a tab open?". A slot can hold OPTIONS state with no tab currently open,
    and one lookup reaches both flavours of slot: a channel-born slot
    (``slack_<ts>``) and a dashboard slot mirroring out to Slack
    (``chat-<n>-<epoch>``) both live in the same registry.

    Returns None rather than raising for any state object that cannot answer the
    question. OPTIONS bookkeeping is best-effort cleanup and must never be able
    to abort the turn that triggered it.

    The key is required to be a real ``str``: ``_normalize_slot_key`` strips a
    repeated ``dashboard_`` prefix with an unbounded ``while``, which only
    terminates for a genuine string. Handing it anything whose ``startswith``
    is always truthy spins forever, allocating as it goes -- so a non-string
    key is refused here rather than normalized.
    """
    if not isinstance(session_key, str):
        return None
    getter = getattr(state, "get_slot", None)
    if not callable(getter):
        return None
    try:
        slot = getter(_normalize_slot_key(session_key))
        if slot is not None:
            return slot
        # The fold is FILENAME-shaped, so any slot whose name is not its session
        # key folded is unreachable through it. A cron slot is named
        # ``cron-<id>`` while its session key is ``cron:<id>``, which folds to
        # ``cron_<id>`` and matches nothing — so a persistent cron's OPTIONS
        # control was never tracked at all, and the follow-up turn had nothing
        # to expire, leaving it clickable into a superseded question.
        #
        # Such a slot still knows its own identity (``linked_session_key``), so
        # ask the slots rather than guessing at more spellings. Only on a miss,
        # so the common path stays a single dict lookup.
        for candidate in (getattr(state, "_slots", None) or {}).values():
            if effective_session_key(candidate) == session_key:
                return candidate
        return None
    except Exception:
        logger.debug("Slack OPTIONS slot lookup failed", exc_info=True)
        return None


def slack_options_linked_slot(state: DashboardState | None, thread_ts: str) -> _ChatSlot | None:
    """The dashboard slot that owns *thread_ts*, if a session mirrors into it.

    Prefers the thread -> slot reverse index, then falls back to scanning slots
    for a matching ``_slack_thread_ts``. The fallback exists because the index is
    written by one helper that a caller can forget: relying on it alone made this
    resolver silently return nothing for a freshly-linked thread.
    """
    if not thread_ts or state is None:
        return None
    linked = getattr(state, "get_linked_slot", None)
    if callable(linked):
        try:
            slot = linked(thread_ts)
        except Exception:
            slot = None
        if slot is not None:
            return slot
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return None
    for slot in slots.values():
        if getattr(slot, "_slack_linked", False) and (
            getattr(slot, "_slack_thread_ts", "") == thread_ts
        ):
            return slot
    return None


def _persisted_thread_owner(state: DashboardState | None, thread_ts: str) -> str:
    """The session key the PERSISTED thread index maps *thread_ts* to.

    Distinct from :func:`slack_options_linked_slot`, which only knows the
    dashboard SLOT index. A cron thread is linked with ``cron:<id>`` and has no
    slot at all, so the slot index cannot see it — yet that is the key a control
    on such a thread is recorded under, because the record sites resolve the owner
    through this same index. Leaving it out of the ownership helpers made the
    record and the forget disagree: the control was filed under ``cron:<id>`` and
    then never cleared, so a later expiry found it and overwrote the selection.

    Returns "" when unknown. The ``isinstance`` check is deliberate: the index is
    typed ``str | None``, and anything else means the caller handed us a stub.
    """
    if state is None or not thread_ts:
        return ""
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return ""
    try:
        owner = sessions.get_session_for_thread(thread_ts)
    except Exception:
        logger.debug("Could not resolve the persisted owner of %s", thread_ts, exc_info=True)
        return ""
    return owner if isinstance(owner, str) and owner else ""


def slack_options_owner_key(state: DashboardState | None, thread_ts: str) -> str:
    """The single session key that owns the conversation living in *thread_ts*.

    Use this when RECORDING a control — it has to land on the one session whose
    next turn should spend it. Use :func:`slack_options_session_keys` when
    CLEARING, where covering every candidate is correct.

    The slot index is consulted first and the persisted thread index second. Where
    both know the thread they agree (``link_slack`` writes both), so the order only
    matters for a thread ONE of them can see — and a cron-linked thread is visible
    only to the persisted one.
    """
    slot = slack_options_linked_slot(state, thread_ts)
    if slot is not None:
        mirrored = effective_session_key(slot)
        if mirrored:
            return mirrored
    persisted = _persisted_thread_owner(state, thread_ts)
    if persisted:
        return persisted
    return canonical_key(thread_ts) if thread_ts else ""


def slack_options_session_keys(state: DashboardState | None, thread_ts: str) -> list[str]:
    """Every session key under which *thread_ts*'s OPTIONS control may be recorded.

    One Slack thread belongs to one conversation, but that conversation is
    addressed by several different keys depending on which side owns it: a
    Slack-born session is ``slack:<ts>``, a dashboard session mirroring out to the
    thread is ``dashboard:<slot>``, and a persistent cron is ``cron:<id>``. A
    caller holding only the thread timestamp cannot tell which, so return every
    candidate — they name the same conversation, so acting on all of them is
    correct rather than merely safe.

    Missing the cron spelling here is what let a selection leave its record
    behind: the forget cleared the keys it could guess, the ``cron:<id>`` record
    survived, and the next expiry edited over the user's answer.
    """
    if not thread_ts:
        return []
    keys = [canonical_key(thread_ts)]
    slot = slack_options_linked_slot(state, thread_ts)
    if slot is not None:
        mirrored = effective_session_key(slot)
        if mirrored and mirrored not in keys:
            keys.append(mirrored)
    persisted = _persisted_thread_owner(state, thread_ts)
    if persisted and persisted not in keys:
        keys.append(persisted)
    return keys


def options_records(state: DashboardState | None, session_key: str) -> tuple[PostedOptions, ...]:
    """Every OPTIONS control still outstanding for *session_key*.

    The store is keyed by SESSION KEY, on ``DashboardState``, not held on the
    slot. A plain Slack thread frequently has no dashboard slot, and a slot-held
    record was simply dropped for those sessions — so nothing tracked the control,
    no later turn could expire it, and the stale click this whole lifecycle exists
    to prevent stayed possible (#1694). Keying by session key makes the slotless
    case ordinary instead of special, and it cannot go stale when a slot appears
    or disappears mid-conversation.
    """
    if state is None or not session_key:
        return ()
    store = getattr(state, "_slack_options_by_key", None)
    if not isinstance(store, dict):
        return ()
    return store.get(canonical_key(session_key), ())


def set_options_records(
    state: DashboardState | None, session_key: str, records: tuple[PostedOptions, ...]
) -> None:
    """Replace *session_key*'s outstanding controls, dropping the key when empty.

    Pruning on empty is the ONLY bound, and it is the right one: an entry exists
    exactly as long as a question is still unanswered, and it leaves the moment the
    lifecycle completes — the expiry settles it, a click forgets it, or an unlink
    clears it.

    Deliberately NOT capped with eviction. A cap sounds prudent and is actively
    harmful here: evicting a record for a control that is still clickable means no
    later turn can retire it, which is precisely the untracked control this whole
    lifecycle exists to eliminate — so a bound would reintroduce the defect at
    scale, silently, on the busiest instances. The footprint is also no worse than
    what it replaced: records used to hang off ``_ChatSlot``, and slots are
    themselves unbounded in number, so this holds strictly fewer entries (only
    conversations with a live unanswered question) than the store it came from.
    """
    if state is None or not session_key:
        return
    store = getattr(state, "_slack_options_by_key", None)
    if not isinstance(store, dict):
        return
    key = canonical_key(session_key)
    if records:
        store[key] = records
    else:
        store.pop(key, None)


def remember_slack_options(
    state: DashboardState | None,
    session_key: str,
    posted: PostedOptions | None,
) -> None:
    """Record the live OPTIONS control just posted for *session_key*.

    APPENDS rather than replaces. A turn can post more than one OPTIONS message,
    and the same slot is reachable from several posting paths, so overwriting
    would leave the earlier control on screen with nothing tracking it — a click
    on it would then answer a question the conversation has already passed.
    Every outstanding record is kept so expiry can drain all of them.

    A no-op when there is no control or no dashboard state. Note there is NO
    slot requirement: the store is keyed by session key precisely so a plain
    Slack thread without a slot still gets its control tracked (#1694).
    """
    if posted is None or state is None or not session_key:
        return
    current = options_records(state, session_key)
    # Same message posted twice (a retry, or two paths recording one post)
    # must not queue two edits for one control.
    if posted not in current:
        set_options_records(state, session_key, (*current, posted))


def forget_slack_options(
    state: DashboardState | None, session_key: str, ts: str | None = None
) -> None:
    """Drop the recorded control for *session_key* without editing Slack.

    For when something else has already spent the control — a Send click
    re-renders the message with the user's selection, and striking every choice
    through afterwards would erase the choice they made.

    Pass *ts* to drop ONLY the control posted as that message. A click spends one
    control, not every control outstanding in the conversation: dropping them all
    would leave any other one on screen with nothing tracking it, so a later click
    on it would answer a superseded question. Omitting *ts* clears all of them,
    which is right when the whole conversation is going away (an unlink).
    """
    if state is None or not session_key:
        return
    if ts is None:
        set_options_records(state, session_key, ())
        return
    set_options_records(
        state,
        session_key,
        tuple(p for p in options_records(state, session_key) if p.ts != ts),
    )


def slack_options_owner_keys_snapshot(
    state: DashboardState | None, thread_ts: str
) -> tuple[str, ...]:
    """The keys *thread_ts*'s control could be recorded under, captured NOW.

    A caller that is about to await Slack has to take this BEFORE the await and
    forget against it afterwards. Recomputing after the fact reads the keys of
    whoever owns the thread THEN: a relink landing during a submit's edit moves the
    thread to another session, so the recomputed list names the new owner, the
    previous owner's record survives the click, and that session's next turn edits
    straight over the selection the user just made.
    """
    return tuple(slack_options_session_keys(state, thread_ts))


def mint_options_token(
    state: DashboardState | None,
    asker_key: str,
    row_ts: str | None = None,
) -> str | None:
    """The staleness token to post with a control asked by *asker_key*.

    Pairs the asking conversation with how far it had got when the question was
    asked.

    *asker_key* is supplied by the caller rather than resolved from the thread.
    The caller knows which session ran the turn; resolving the thread's owner here
    would name whoever owns it at MINT time, and a link landing between the turn
    starting and its footer going out would stamp the control with a conversation
    that never asked the question.

    *row_ts* likewise comes from the caller when it already holds the value. That
    keeps this free of I/O: reading the tail off disk takes the transcript's
    cross-process flock, so on a contended session it is not a bounded cost and
    has no business on the event loop. Passing the row the caller already has in
    memory is the same value -- a replayed row preserves its ``ts`` verbatim.

    Falls back to a disk read only when the caller has nothing, and that path is
    BLOCKING: run it in a thread. ``None`` means the control posts untokened,
    which the check reads as "cannot prove staleness" and honours.
    """
    try:
        if not asker_key:
            return None
        if not row_ts:
            log = getattr(state, "conversation_log", None)
            if log is None:
                return None
            row_ts = log.last_row_ts(asker_key)
        if not row_ts:
            return None
        return encode_options_token(asker_key, row_ts)
    except Exception:
        logger.debug("Could not mint an OPTIONS staleness token", exc_info=True)
        return None


async def options_control_is_stale(
    state: DashboardState | None, block_id: str | None, thread_ts: str
) -> bool:
    """Whether the control carrying *block_id* is answering a superseded question.

    The whole check: the token says which conversation asked and where that
    conversation stood at the time; the transcript on disk says where it stands
    now. A conversation that has moved on has superseded its own question.

    Nothing in gateway memory takes part, which is what makes this survive a
    restart -- the token is in the Slack message and the comparand is a persisted
    transcript row, so neither half is lost when the process dies. The counter a
    previous design compared against could NOT be used here: it is rebuilt from a
    windowed replay of the transcript on startup, so it climbs back through values
    it has already issued and reads a pre-restart token as current.

    ABSTAINS to False -- honour the click -- whenever staleness cannot be PROVEN:
    no token, a token this build cannot parse, an unreadable transcript, or an
    unparseable timestamp. Refusing a legitimate answer is worse than accepting a
    late one, and a control posted before this check existed carries no token at
    all.
    """
    token = decode_options_token(block_id)
    if token is None:
        return False
    asker_key, minted_ts = token
    try:
        # ONE comparison: has the conversation that ASKED moved on?
        #
        # Deliberately no ownership check. It would answer the wrong question now
        # that an accepted click carries its destination: the answer reaches the
        # conversation that asked it whatever the thread's ownership has since
        # done, so a thread changing hands does not make a still-pending question
        # unanswerable -- and refusing on that basis would reject a click the user
        # was legitimately shown. Handover is not supersession; only the asker's
        # own transcript advancing is.
        log = getattr(state, "conversation_log", None)
        if log is None:
            return False
        current_ts = await asyncio.to_thread(log.last_row_ts, asker_key)
        if not current_ts:
            return False
        return transcript_sort_key(current_ts) > transcript_sort_key(minted_ts)
    except Exception:
        logger.debug("Could not judge whether an OPTIONS control is stale", exc_info=True)
        return False


def forget_slack_options_for_thread(
    state: DashboardState | None,
    thread_ts: str,
    ts: str | None = None,
    keys: tuple[str, ...] | None = None,
) -> None:
    """Drop the recorded control for the conversation living in *thread_ts*.

    For callers that hold a Slack thread timestamp rather than a session key —
    the interaction handlers, which see a click on a message and not the session
    behind it. Clears every key the thread's conversation can be recorded under,
    so a control posted by the dashboard mirror is forgotten too.

    *ts* scopes it to the ONE control posted as that message, which is what a
    click spends. Without it every outstanding control in the conversation is
    dropped, leaving any other one clickable with nothing tracking it.

    Pass *keys* from :func:`slack_options_owner_keys_snapshot` when an await sits
    between reading ownership and clearing it — a relink during that window would
    otherwise leave the previous owner's record behind. Omitting *keys* resolves
    now, which is right only for a caller that has not awaited.
    """
    for key in keys if keys is not None else slack_options_session_keys(state, thread_ts):
        forget_slack_options(state, key, ts)


async def expire_slack_options(
    state: DashboardState | None, session_key: str, ts: str | None = None
) -> None:
    """Spend the OPTIONS control left from *session_key*'s previous turn.

    Called as a new turn begins, whichever surface it arrives on, so a control
    the conversation has moved past stops inviting a click that would answer a
    superseded question.

    Records stay TRACKED across the Slack edit and are only ever REMOVED
    afterwards, never re-added. A write-back that re-adds cannot tell "still
    outstanding" from "deliberately removed while I was awaiting": a click landing
    mid-await calls :func:`forget_slack_options`, and re-adding would resurrect
    the control it just answered, so every later turn would re-edit the message
    and overwrite the user's selected summary. Removing only what settled leaves
    a concurrent forget authoritative. A record whose edit failed *transiently* is
    simply never removed, so it is still retried later — dropping it would leave a
    live control on screen with nothing tracking it, the exact stale click this
    whole lifecycle exists to prevent. A failure that will never succeed (deleted
    message, a channel we are not in) counts as settled, so it cannot be retried
    on every later turn forever.

    Two concurrent expiries can therefore both edit the same control. That is
    deliberate: both write byte-identical spent blocks, so the cost is a wasted
    API call, whereas resurrecting an answered control corrupts what the user
    sees.

    Drains EVERY outstanding control, not just the newest: a turn can leave more
    than one on screen, and any one left untracked stays clickable into a
    superseded question.

    Pass *ts* to spend ONLY the control posted as that message. A caller that is
    cleaning up after ITSELF has to narrow this way: a concurrent turn can record
    its own fresh control in the same slot while this caller is still awaiting
    Slack, and a session-wide drain would strike that newer question through too
    — leaving the question the conversation is actually waiting on unanswerable.
    Omitting *ts* drains all of them, which is what a NEW turn wants (it
    supersedes everything before it) and what an unlink wants (the whole
    conversation is going away).
    """
    if state is None or not session_key:
        return
    outstanding = options_records(state, session_key)
    if not outstanding:
        return
    if ts is not None:
        outstanding = tuple(posted for posted in outstanding if posted.ts == ts)
        if not outstanding:
            # Already spent by whoever else tracked it; nothing of ours to edit.
            return
    slack = getattr(state, "slack_client", None)
    if slack is None:
        # Nothing was spent, so nothing may be dropped: with no client the
        # controls are still live on screen and must stay tracked.
        return
    settled: list[PostedOptions] = []
    for posted in outstanding:
        # Serialize against the Send handler's edit to this SAME message, and
        # re-read the record INSIDE the lock. A click that won the race has
        # already rewritten the message with the user's selection and dropped the
        # record, so finding it gone means "do not edit" -- without the re-read,
        # a late expiry would erase the answer the user just gave. The lock makes
        # that check trustworthy; the check is what makes the lock useful.
        async with options_edit_lock(posted.channel, posted.ts):
            if posted not in options_records(state, session_key):
                continue
            if await expire_options(slack, posted):
                # Retire the control for clicks too, not just for our records. A
                # Send click queued behind this expiry would otherwise find the
                # answer claim unheld, take it, and dispatch an answer to the
                # question we just struck through. Marked while we still hold the
                # lock, so the queued click cannot slip between the edit and this.
                mark_options_terminal(posted.channel, posted.ts)
                settled.append(posted)
    if settled:
        # Remove by identity against the CURRENT records, not by reassigning a
        # remembered tuple: a turn that finished while we awaited Slack may have
        # recorded its own control here, and it must survive.
        set_options_records(
            state,
            session_key,
            tuple(p for p in options_records(state, session_key) if p not in settled),
        )


def slack_mirror_is_paused(state: Any, session_key: str) -> bool:
    """True when a turn must NOT be mirrored to the session's linked thread.

    A pause retains the thread binding, so every inbound resolver still sees the
    link and a reply still reaches the session that owns the thread. This is the
    one predicate that tells outbound egress apart from that: consult it before
    SENDING, never before routing. Gating a routing decision on it would fork a
    new session out of a reply the user expected to continue this one.

    Scope is deliberately turn mirroring — the user echo, its tool stream, the
    assistant reply, an auth-required error and the linked approval prompt.
    Deliveries that merely reuse the thread as an ADDRESS (a cron result, a
    subagent completion, a requested file, an auto-nudge tick) are NOT gated: an
    absent link makes those fall back to the owner's DM, and one of them deletes
    the auto-nudge loop outright, so treating paused as no-link there would
    reroute messages the user asked for and destroy a live monitor.

    The channel compaction notice is also not gated, and belongs with the
    address-based deliveries above rather than with turn output: it reports that
    the session's own history was compacted, which stays true whether or not the
    conversation is currently connected. (An earlier version of this note claimed
    it "cannot reach a paused link" because an origin had no disconnect control.
    Origin rows now DO carry one, so that reasoning is void — the exclusion
    stands on the delivery's kind, not on the row's affordances.)

    Strict ``is True`` rather than truthiness, and fails OPEN: ``sessions`` is a
    bare ``MagicMock`` across much of the suite and returns a truthy child for
    any unstubbed accessor, so truthiness here would silence every linked thread
    in the test suite. Failing open leaves a muted thread noisy at worst; failing
    closed would make a live thread silently dead.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_slack_paused(session_key) is True
    except Exception:
        logger.debug("slack pause lookup failed for %s", session_key, exc_info=True)
        return False


def mirror_is_paused(state: Any, session_key: str, *, origin: bool = False) -> bool:
    """True when a turn must NOT be mirrored to one of the session's non-Slack deliveries.

    The channel-neutral twin of :func:`slack_mirror_is_paused`, and what a
    dashboard disconnect suppresses for a non-Slack channel.

    ``origin`` names WHICH delivery is being asked about, because a session can
    hold two at once — the conversation it was born in and an explicit mirror —
    and they mute independently. Callers that resolve a single outbound target
    pass the flag matching the row the user acted on; see
    :meth:`SessionMap.set_mirror_paused`.

    The scope is narrower than Slack's because the hazard Slack has does not
    exist here: no cron result, subagent completion, requested file or auto-nudge
    tick reads a mirror binding at all — those address a channel explicitly — so
    gating this cannot reroute a delivery to the owner's DM or destroy a monitor
    loop. It covers the two sites that carry turn output: the user echo and the
    assistant reply.

    Same ``is True`` / fail-open contract as the Slack gate, for the same
    MagicMock reason. A muted binding must stay visible to
    ``find_mirror_sessions``, to the resume-conflict check and to both clear
    paths, or in-channel ``!unlink`` and conflict detection break.
    """
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return False
    try:
        return sessions.is_mirror_paused(session_key, origin=origin) is True
    except Exception:
        logger.debug("mirror pause lookup failed for %s", session_key, exc_info=True)
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
        text = _installed_theme_persona(color_theme[len("custom-") :])
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
            caller=f"dashboard:{slot.key}",
            operation="consolidate",
            outcome="denied",
            source="dashboard",
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
                # The exfiltration gate is parameter-aware: standard high-entropy
                # OAuth values are exempt only at exact code-owned endpoints, while
                # fixed/encoded credentials, heavy percent encoding, and unknown
                # params remain fail-closed.
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
                out[k] = v if (safe_scheme and not oauth_url_contains_credential(v)) else ""
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
_ACTIVITY_NO_REPLY_CONTINUE_MSG = (
    f"{EMPTY_RESPONSE_RECOVERY_PREFIX}\n"
    "Your previous turn did work — it streamed text, called tools, or reasoned "
    "— but ended without a closing reply, so the request looks unanswered. "
    "Everything above already happened and its results are in the conversation: "
    "answer now from what is there. Do NOT restart the request, and do NOT "
    "re-run any tool or step that already completed."
)
#: Shares :data:`EMPTY_RESPONSE_RECOVERY_PREFIX` with
#: :data:`_EMPTY_AUTO_CONTINUE_MSG` rather than minting a marker of its own. The
#: marker is what ``RecoveryCard.tsx`` classifies a transcript row by, and both
#: bodies are the same event to a reader ("the turn ended without an answer, and
#: the runner continued it once"); a second marker would need a card row and a
#: locale pair in twelve catalogs to say nothing new. The BODIES must differ,
#: because this one is read by the MODEL: telling a turn that ran tools that it
#: "produced no output" invites it to redo work whose side effects already
#: landed, which is the failure this whole path exists to prevent.
_PROMISE_ONLY_CONTINUE_MSG = (
    f"{PROMISE_ONLY_RECOVERY_PREFIX}\n"
    "Your previous turn ended right after you said you would perform an action "
    "immediately (for example, opening a PR or running a tool), but the turn "
    "yielded before that action was carried out, so nothing actually happened. "
    "Carry out that action now by making the tool call you announced. But if you "
    "were actually waiting on the user's approval or confirmation, or on a "
    "condition that is not yet satisfied, do NOT perform the action — say what you "
    "are waiting for instead. If it turns out you cannot, say what is blocking it "
    "and what you need instead — do NOT just restate the intention. Do NOT re-run "
    "any tool that already completed successfully above."
)
_COMPACTION_CONTINUE_MSG = (
    f"{COMPACTION_RECOVERY_PREFIX}\n"
    "The conversation above was summarized mid-turn because the context window "
    "filled up, and your previous turn then ended without finishing the request. "
    "The summary is authoritative — earlier messages are gone, so work from what "
    "remains above. Continue the pending request now and respond. Do NOT restart "
    "from scratch, and do NOT re-run any tool or step that already completed "
    "successfully — the summary records what was done. If the summary left you "
    "without something you need, say what is missing instead of guessing."
)
_SYNTHETIC_RECOVERY_MSGS = (
    _CONN_RECOVER_MSG,
    _BUSY_RECOVER_MSG,
    _POSTTOKEN_RECOVER_MSG,
    _EMPTY_AUTO_CONTINUE_MSG,
    _ACTIVITY_NO_REPLY_CONTINUE_MSG,
    _PROMISE_ONLY_CONTINUE_MSG,
    _COMPACTION_CONTINUE_MSG,
)

# High-confidence "I will do it right now" endings. Kept deliberately NARROW: a
# broad natural-language detector risks false positives, duplicate writes, and
# continuation loops (see #2686 fix direction), so this matches only a terminal
# first-person commitment to an IMMEDIATE action, with an explicit now/right-away
# marker. "I'll explain that now: ..." is NOT caught, because the detector also
# requires that the promise be the LAST thing in the text (nothing substantive
# follows it) — an announcement followed by the actual content is a normal answer.
# The immediacy markers are deliberately RESTRICTIVE: only true "right now"
# adverbs. `next` and `go ahead and` were removed after the #2696 AI review found
# they matched "I'll do that next week" and permission-seeking closers — `next`
# is a sequencer, not an immediacy signal. The bare `going to` alternative was
# also removed (#2696 GPT round): with no first-person subject it matched
# third-person statements like "The deployment is going to start now", firing an
# unrelated continuation. Only the subject-bound `i'm going to` form remains.
_PROMISE_NOW_RE = re.compile(
    r"\b(?:i(?:'|’)?ll|i\s+will|let\s+me|i(?:'|’)?m\s+going\s+to)\b"
    r"[^.!?\n]*?"
    r"\b(?:now|right\s+away|right\s+now|immediately)\b"
    r"[^.!?\n]*[.!?]?\s*$",
    re.IGNORECASE,
)
# A trailing sentence that is ONLY a promise (no colon-introduced content, no
# code fence, no list) — used to confirm the promise is terminal, not a preamble.
_PROMISE_HAS_FOLLOWING_CONTENT_RE = re.compile(r":\s*\S|```|\n\s*[-*\d]")
# Permission-seeking / no-action closers that a naive immediacy match misfires on
# (found by the #2696 AI review). These are the OPPOSITE of a promise-to-act: the
# turn is correctly yielding to the user or explicitly declining to act. If any
# appears in the final segment, it is never a promise-only turn. "let me know ...
# now"/"...next" reads as immediate to the regex but is a hand-off; "for now" /
# "as-is" / "stop here" are explicit non-actions.
_NO_ACTION_CLOSER_RE = re.compile(
    r"\blet\s+me\s+know\b"
    r"|\bfor\s+now\b"
    r"|\bfor\s+the\s+moment\b"
    r"|\bas[-\s]is\b"
    r"|\bstop\s+here\b"
    r"|\bleave\s+(?:it|that|this)\b",
    re.IGNORECASE,
)
# An APPROVAL-GATED closer keeps the decision with the user, even when the
# sentence otherwise reads as a promise-to-act ("If that looks good, I'll push
# it now.", "Just say the word and I'll open the PR right away."). The current
# no-action list does not cover these because they contain both a real
# commitment ("I'll ... now") and a conditional opener; auto-continuing them
# dispatches an unattended action the user was still being asked to approve.
# Found by the #2696 UX review. Bias, like the negation gate, is toward reject
# on ambiguous conditionals — a false reject just lands the turn normally
# (pre-fix behaviour, safe); a false accept executes a possibly-irreversible
# side effect (push, merge, delete) without consent.
_APPROVAL_GATED_RE = re.compile(
    # ANY conditional `if` opener, not just specific pronouns: "If CI passes, I'll
    # delete it now" is as gated as "If you approve ...". Bias toward reject is
    # safe here (a false reject just lands normally); the #2696 GPT round widened
    # this from the pronoun list after "If CI passes ..." slipped through.
    r"\bif\b" r"|\bjust\s+say\s+the\s+word\b" r"|\bwant\s+me\s+to\b" r"|\bshall\s+i\b"
    # Consent DEFERRAL: the action is gated on the user's approval/confirmation,
    # even when the sentence reads as "I'll ... now" ("I'll wait for your approval
    # before I delete it right now"). The earlier list only caught "with your
    # approval" and missed the far more common "wait for / for / pending your
    # approval", "your go-ahead/sign-off/confirmation", "before you approve", and
    # "you to confirm" forms — auto-continuing any of them dispatches an action the
    # model explicitly said it would hold for consent (#2696 GPT round, blocking).
    # The forms are kept PRECISE (a possessive consent-noun, or a deferral verb
    # bound to you/your) rather than bare "pending"/"await"/"before you", so a
    # genuine promise like "merge the pending PR now" is not falsely rejected —
    # closing the consent CLASS without the over-broad reject the design review
    # warned about. Reject-bias is still safe (a false reject just lands).
    r"|\byour\s+(?:approval|go[-\s]?ahead|sign[-\s]?off|confirmation|permission|ok(?:ay)?|blessing)\b"
    r"|\bwait(?:ing)?\s+for\s+(?:you|your|approval|confirmation|sign[-\s]?off|permission|the\s+go[-\s]?ahead)\b"
    r"|\bbefore\s+you\s+(?:approve|confirm|decide|sign\s+off|review|say|weigh\s+in|ok(?:ay)?)\b"
    r"|\byou\s+to\s+(?:approve|confirm|decide|sign\s+off|review|weigh\s+in|say|tell)\b"
    # Temporal/conditional gates in ANY form ("once tests are green", "when the
    # build passes", "after CI", "until you confirm"), not only "... you ...".
    # Closes the conditional CLASS in one rule rather than enumerating each noun;
    # reject-bias is safe.
    r"|\b(?:once|when|after|as\s+soon\s+as|until)\b"
    # Remaining subordinating-conditional conjunctions ("Unless you object, I'll
    # merge now", "Assuming you're fine, I'll push now"). The risky ones are bound
    # to a following pronoun/complementizer so a benign adjective ("the provided
    # config", "the given file") is NOT falsely rejected (#2696 GPT/design rounds).
    r"|\bunless\b"
    r"|\bassuming\s+(?:you|that|we|it|the|your)\b"
    r"|\bprovided\s+(?:that|you)\b"
    r"|\bgiven\s+(?:that|you)\b"
    r"|\b(?:as|so)\s+long\s+as\b",
    # NOTE (residual risk, #2696 design review): this is a deny-list, and the
    # DANGEROUS direction for the consent class is the false ACCEPT (auto-continuing
    # an action the user was still being asked to approve), which enumeration cannot
    # fully close — a novel conditional phrasing will always slip through. Two
    # mitigations bound the blast radius: (1) `_PROMISE_ONLY_CONTINUE_MSG` itself
    # instructs the model NOT to act if it was waiting on approval/a condition (a
    # semantic backstop independent of this regex), and (2) recovery only INJECTS a
    # continuation — the tool call still flows through the normal approval path, so
    # only auto-approve/yolo turns an escape into an unattended side effect. The
    # accept-list inversion (fire only on a proven-unconditional commitment) is the
    # real long-term fix but is a behaviour-changing redesign, out of scope here.
    # A cheaper structural bound worth considering: skip promise-only recovery (or
    # downgrade it to the notice-only arm) under auto-approve/yolo, so a detector
    # false-accept can never become an unattended side effect.
    re.IGNORECASE,
)
# A NEGATED commitment before the immediacy marker ("I'm not going to open the PR
# now", "I won't do that now", "I can't right now") is the OPPOSITE of a promise
# to act, but the bare `going to`/`i'll` alternatives above still match it (found
# by the #2696 GPT review). Reject when a negated-commitment form is followed,
# within the same sentence, by an immediacy marker. Bias is deliberately toward
# REJECTING on ambiguous negation: a false reject just lands the turn normally
# (pre-fix behaviour, safe), whereas a false accept injects an unwanted action.
_NEGATED_PROMISE_RE = re.compile(
    r"\b(?:not\s+going\s+to|never\s+going\s+to|won(?:'|’)?t|will\s+not"
    r"|i(?:'|’)?ll\s+not|can(?:'|’)?t|cannot|not\s+able\s+to|unable\s+to"
    r"|do(?:es)?n(?:'|’)?t|do(?:es)?\s+not|no\s+longer)\b"
    r"[^.!?\n]*?"
    r"\b(?:now|right\s+away|right\s+now|immediately)\b",
    re.IGNORECASE,
)
# The promise gate (`_PROMISE_NOW_RE`) is TERMINAL-ANCHORED (`...$`): any promise
# it matches lives entirely in the final sentence. The three reject gates must be
# scoped to that SAME sentence, not `.search()` the whole segment — otherwise an
# everyday `if`/`when`/`after`/`let me know`/negation in an EARLIER sentence
# ("When you asked about X, I fixed it. I'll open the PR now.") vetoes a genuine
# terminal promise, landing the exact #2686 symptom unrecovered (asymmetric-scope
# false negative, #2696 design review). Splitting on sentence + newline boundaries
# keeps the reject-bias but only where the promise can actually be.
_SENTENCE_BOUNDARY_RE = re.compile(r"[.!?\n]+")


def _terminal_sentence(text: str) -> str:
    """Return the last sentence of ``text`` — the span the terminal promise gate
    matches. Reject gates are scoped to this so a subordinate clause earlier in a
    multi-sentence closer cannot veto a promise that sits only at the end."""
    parts = [p.strip() for p in _SENTENCE_BOUNDARY_RE.split(text) if p.strip()]
    return parts[-1] if parts else text


def is_promise_only_terminal(final_segment_text: str) -> bool:
    """True when the turn's FINAL segment is a promise to act immediately, with no
    action taken and nothing substantive following the promise.

    ``final_segment_text`` MUST be the assistant text emitted AFTER the last tool
    call of the turn (the runner resets its segment buffer at each tool boundary,
    so its final ``assistant_text`` already is exactly that). Evaluating only the
    post-last-tool text is what keeps an already-executed action from being
    replayed: a turn that DID call the tool and then summarised has a final
    segment that is a summary, not a promise, so it does not match.

    Narrow by construction (three gates, all required):
      1. there is a first-person commitment to an immediate action
         (``I'll ... now`` / ``let me ... now`` / ``going to ... right away``);
      2. that promise sits at the END of the text (``...now.`` is the last
         sentence), so ``I'll explain now: <the explanation>`` does NOT match;
      3. no colon-introduced content, code fence, or list follows — those mean
         the "promise" was actually a lead-in to delivered content.
    Returns False on empty/whitespace so a genuinely empty turn stays on the
    dedicated empty-response path."""
    text = (final_segment_text or "").strip()
    if not text:
        return False
    # The three reject gates are scoped to the terminal sentence (where a matched
    # promise must live), NOT the whole segment — see `_SENTENCE_BOUNDARY_RE`.
    terminal = _terminal_sentence(text)
    # Gate 0: a permission-seeking or explicit-no-action closer is the OPPOSITE of
    # a promise-to-act, even when it contains an immediacy word ("let me know what
    # you'd like to do next", "I'll leave that as-is for now"). Reject up front so
    # the guard never overrides a user decision point with an unsolicited action.
    if _NO_ACTION_CLOSER_RE.search(terminal):
        return False
    # Gate 0b: a negated commitment ("I'm not going to ... now", "I won't ... now")
    # is an explicit NON-action, not an unfulfilled promise. Reject it before the
    # immediacy match, which would otherwise fire on the bare commitment token.
    if _NEGATED_PROMISE_RE.search(terminal):
        return False
    # Gate 0c: an approval-gated closer ("If that looks good, I'll push it now")
    # leaves the decision with the user; auto-continuing it dispatches the
    # action the user was still being asked to approve.
    if _APPROVAL_GATED_RE.search(terminal):
        return False
    # Gate 3: a promise that introduces content (colon + text, a fence, a list)
    # is a preamble to a real answer, not an unfulfilled action. Scanned over the
    # WHOLE text on purpose: the content that unmasks the promise as a preamble
    # follows it (a colon tail on the same line, or a fence/list on later lines),
    # so it is outside the terminal sentence by construction.
    if _PROMISE_HAS_FOLLOWING_CONTENT_RE.search(text):
        return False
    # Gates 1 + 2: an immediate-action commitment that is the terminal sentence.
    return bool(_PROMISE_NOW_RE.search(text))


# Fenced code blocks (``` or ~~~, any length ≥3) and inline code spans (a
# backtick run closed by an equal-length run on the same line) are QUOTED
# content: a user pasting a leak transcript, or the model explaining tool
# syntax, legitimately shows an invoke block inside them. Both are stripped
# before the leak scan so quoted machine syntax never triggers the notice.
# Only PAIRED delimiters are removed — an unpaired fence or tick run inside a
# genuinely leaked payload leaves the surrounding invoke tags visible (failure
# direction: a missed notice, never a suppressed one).
#
# LINEAR BY CONSTRUCTION, deliberately. The scan runs on the event loop at
# every turn completion over model-authored text, so it must stay linear on
# ADVERSARIAL input too: a variable-length backreference pattern like
# ``(`{3,}|~{3,}).*?(?P=fence)`` backtracks over every possible delimiter
# split, and a few thousand consecutive backticks stall the loop for tens of
# seconds — long enough for the liveness watchdog to kill the gateway. So the
# delimiters are tokenized with a fixed-alternative regex (no backreferences)
# and paired in one forward pass.
_CODE_DELIM_RE = re.compile(r"`+|~{3,}")


def _strip_quoted_code(text: str) -> str:
    """*text* with paired code fences and inline code spans removed, linearly.

    One forward pass over the delimiter tokens:

    * A run of ≥3 backticks or ≥3 tildes OPENS a fence; the next run of the
      same character AT LEAST AS LONG (CommonMark's closer rule) CLOSES it,
      and everything between is dropped — so a four-backtick quote can carry
      triple-backtick content without being cut short. Delimiters of the
      other kind, and shorter runs of the same kind, inside an open fence are
      content.
    * Outside a fence, a backtick run of length L opens an inline span; the
      next run of exactly length L on the SAME line closes it. A newline
      discards all pending span opens (spans cannot contain newlines), and a
      pending open that never closes is emitted as literal text.

    Every token is visited once and every output chunk is appended/discarded
    at most once, so the pass is linear whatever the input shape.
    """
    out: list[str] = []
    # Pending inline-span opens: run length -> index in ``out`` where the
    # opening run was appended. Cleared on every newline and on fence entry.
    span_open_at: dict[int, int] = {}
    fence_char = ""  # non-empty while inside an open fence
    fence_open_len = 0
    fence_open_out_idx = 0
    pos = 0
    for tok in _CODE_DELIM_RE.finditer(text):
        seg = text[pos : tok.start()]
        delim = tok.group()
        pos = tok.end()
        if fence_char:
            # Inside a fence: append tentatively — the content is dropped only
            # when a closer actually arrives, so an UNPAIRED fence leaves every
            # byte in place (fail toward detection, never suppression).
            out.append(seg)
            if delim[0] == fence_char and len(delim) >= fence_open_len:
                del out[fence_open_out_idx:]  # drop the fence and its content
                fence_char = ""
            else:
                out.append(delim)
            continue
        out.append(seg)
        if "\n" in seg:
            span_open_at.clear()
        if len(delim) >= 3:
            # Fence opener (either character). Tentatively append; the closer
            # (if any) truncates back to this index.
            fence_open_out_idx = len(out)
            out.append(delim)
            fence_char = delim[0]
            fence_open_len = len(delim)
            span_open_at.clear()
            continue
        opened = span_open_at.pop(len(delim), None)
        if opened is not None:
            del out[opened:]  # drop the span, its delimiters, and its content
            # Pending opens recorded INSIDE the just-dropped span went with it;
            # keeping their indices would point past the truncated output.
            span_open_at = {ln: i for ln, i in span_open_at.items() if i < opened}
        else:
            span_open_at[len(delim)] = len(out)
            out.append(delim)
    # An unpaired fence needs no special handling here: its tentatively-
    # appended delimiter is already in ``out`` as literal text.
    out.append(text[pos:])
    return "".join(out)


# The leaked block's signature: an invoke open tag with a name attribute
# (optionally namespace-prefixed the way some harnesses emit it). MACHINE-SHAPED
# on purpose — unlike the promise-only detector this is not a natural-language
# inference, so the false-positive surface is quoted syntax (handled by the
# stripping above), not phrasing.
_LEAKED_INVOKE_OPEN_RE = re.compile(
    r"<(?:[A-Za-z][\w.-]*:)?invoke\s+name\s*=\s*[\"'][^\"'<>]+[\"']"
)
# Corroboration: the block must also carry a parameter tag or its own close tag,
# so a lone stray open tag in prose (a truncated quote, a typo'd example) does
# not read as a full leaked invocation.
_LEAKED_INVOKE_BODY_RE = re.compile(r"<(?:[A-Za-z][\w.-]*:)?(?:parameter\b|/invoke\s*>)")


def has_leaked_tool_call(text: str) -> bool:
    """True when *text* contains a tool invocation emitted as PROSE — an
    unquoted invoke-block open tag with a parameter or close tag (issue #6112:
    the model writes the call into the text channel instead of executing it,
    the turn ends with zero tool calls, and the session silently stalls).

    Quoted syntax is excluded structurally: fenced code blocks and inline code
    spans are stripped before the scan, so a pasted bug report or an explained
    example never matches. Callers decide what to DO with a match, and the two
    turn shapes differ: :func:`should_notice_leaked_tool_call` requires
    ``turn_tool_calls == 0`` because it un-lands the turn, which would
    misdescribe a turn whose earlier calls had side effects, while
    :func:`should_notice_mixed_turn_leak` handles that shape notice-only. A
    tool-heavy turn IS a leak — it is just not un-landable.
    """
    if not text:
        return False
    stripped = _strip_quoted_code(text)
    opener = _LEAKED_INVOKE_OPEN_RE.search(stripped)
    if not opener:
        return False
    # Corroboration must FOLLOW the opener: a parameter/close tag sitting
    # before a lone opener is unrelated markup, not this invocation's body.
    return bool(_LEAKED_INVOKE_BODY_RE.search(stripped, opener.end()))


def should_notice_leaked_tool_call(
    *,
    stop_reason: str,
    end_turn_reason: str,
    final_segment_text: str,
    prompt_depth: int,
    is_cancelled: bool,
    refusal_reasons: list,
    turn_tool_calls: int = 0,
    in_stage_execution: bool = False,
) -> bool:
    """Decide whether to surface the leaked-tool-call NOTICE (issue #6112).

    The defect: the model emits an invoke block into its TEXT channel instead
    of executing it (observed when the target is a deferred MCP tool whose
    schema is not yet bound, and with large nested arguments), the turn then
    ends with zero tool calls, and the session idles — in a monitor/autonudge
    loop this is a silent stall the user only discovers by noticing nothing
    happened.

    NOTICE-ONLY, deliberately. An injected "re-issue that call" continuation
    would carry runtime authority into a session where the call can execute
    with no human gate — slot trust, global yolo, OR a static agent tool
    allowlist all auto-approve it, and the last of these is invisible at this
    layer, so no downgrade condition can be written here that fails closed.
    The leaked block is also not proof of the model's own intent: untrusted
    external content (a pasted issue, a fetched page quoting a leak) can carry
    an unfenced block the model merely reproduces. So the turn is marked
    un-landed and the user gets a visible card naming what happened; nothing
    is queued and nothing can execute. An unattended loop loses one cycle and
    retries on its own schedule — visibly, which is the half of #6112 this
    layer can fix honestly.

    Gates: the turn ended NORMALLY (cancel/refusal/error paths own their own
    reporting), made ZERO tool calls — not because a tool-heavy turn is a
    different shape (it is the same leak) but because THIS path un-lands the
    turn, and a turn whose earlier calls had real side effects must not be
    marked unacted; that shape is noticed without un-landing by
    :func:`should_notice_mixed_turn_leak` — is top-level, is NOT a
    stage-execution turn (the orchestrator's stage loop reads the turn result
    for stage accounting, and un-landing a stage turn from here would let the
    loop record an unfinished stage as complete — same exclusion as the
    promise-only guard), and its final segment carries the machine-shaped
    leak (:func:`has_leaked_tool_call`). No one-shot budget: nothing is
    re-queued, so there is no loop to bound, and every leaked turn deserves
    its own visible mark.
    """
    if is_cancelled or refusal_reasons:
        return False
    if turn_tool_calls != 0:
        return False
    if in_stage_execution:
        return False
    if stop_reason != end_turn_reason:
        return False
    if prompt_depth != 0:
        return False
    return has_leaked_tool_call(final_segment_text)


def should_notice_mixed_turn_leak(
    *,
    stop_reason: str,
    end_turn_reason: str,
    final_segment_text: str,
    prompt_depth: int,
    turn_tool_calls: int,
) -> bool:
    """Decide whether to surface the MIXED-TURN leak notice.

    The shape :func:`should_notice_leaked_tool_call` deliberately declines: the
    turn dispatched tool calls and THEN leaked a final one as text. Its
    ``turn_tool_calls == 0`` gate exists to protect UN-LANDING — earlier calls
    in the turn may already have taken effect, so marking it unacted could
    misdescribe it — and that reasoning is untouched here. This predicate
    governs only the user-visible NOTICE, which carries no such hazard: nothing
    is queued, nothing is un-landed, nothing executes.

    Why the notice is worth a card on a shape that lands normally: the mixed
    turn hides the gap BETTER than the zero-call one, not worse. The model
    reads state, announces the write, leaks the write, and the reply lands
    looking like a completed action — some of the work may already have
    happened, so there is no stall to notice and no missing output to explain.
    Before this the only trace was a gateway log line, which the person in the
    chat does not read.

    Conditions are EXACTLY the ones the runner already used for its diagnostic
    log, so the set of diagnosed turns does not move — only their visibility:
    at least one dispatched tool call, a NORMAL end-turn (which excludes the
    cancelled stop reason), top-level, and a final segment carrying the
    machine-shaped leak (:func:`has_leaked_tool_call`).

    Deliberately NOT gated on ``in_stage_execution``, unlike its sibling: that
    exclusion exists so the orchestrator's stage loop cannot read an unfinished
    stage as complete, and a notice-only card changes no turn result the loop
    reads. One mismatch follows and is accepted: the card's guidance ("check
    what landed before re-sending") addresses a human driving the chat, not the
    stage loop, so on a stage-execution turn it offers advice its reader cannot
    act on — harmless, and better than hiding the leak on those turns.

    The card says the earlier calls were ATTEMPTED rather than ran, and never
    "nothing was run" as the sibling does, because ``turn_tool_calls`` counts
    dispatches at the tool-call event, before approval — a refused call
    increments it too. The count bounds what may have landed rather than
    asserting any of it did, and a reader who took "nothing was run" literally
    here would redo work that already took effect.

    ``turn_tool_calls`` is REQUIRED, unlike the sibling's defaulted parameter:
    zero is that one's firing shape but this one's refusing shape, so a default
    here would silently disable the predicate rather than exercise it.
    """
    if turn_tool_calls <= 0:
        return False
    if stop_reason != end_turn_reason:
        return False
    if prompt_depth != 0:
        return False
    return has_leaked_tool_call(final_segment_text)


#: Normalised, CLOSED stop-reason vocabulary for the empty-turn diagnostic. The
#: raw wire value is never logged: a backend is free to invent a reason string,
#: and an unbounded value in a diagnostic is both a cardinality hazard and a
#: place model- or user-derived text could appear.
#:
#: The three literals are spelled here rather than imported from
#: ``kiro_crew.acp.types``, following the precedent in ``metrics/turns.py``:
#: ``scripts/check_agent_sdk_boundary.py`` baselines this file at ONE ACP edge
#: and a baselined file may not grow its count. Duplicating a wire constant is
#: only safe with a guard, so ``test_dashboard_chat.py`` pins each against the
#: ACP constant it mirrors — the test tree is outside the gate's scope, so the
#: pin can import what this module may not.
_STOP_END_TURN = "end_turn"
_STOP_CANCELLED_REASON = "cancelled"
_STOP_REFUSAL = "refusal"

#: A terminal event arrived carrying NO stop reason at all. Deliberately its own
#: value rather than folded onto ``end_turn``: ``metrics.turns.turn_outcome``
#: reads absence as a clean turn (correct for latency accounting, where the acp
#: path leaves it unset on every normal completion), but here the two are the
#: whole question — "the provider said the turn ended and produced nothing" is a
#: model-side event, while "the provider never said why it stopped" is a
#: transport-side one, and the incident that motivated these diagnostics could
#: not tell them apart.
STOP_REASON_ABSENT = "absent"
#: A terminal stop reason outside the closed set above.
STOP_REASON_OTHER = "other"

#: Causes for a turn that reached the empty-response verdict. Closed set,
#: low-cardinality, content-free — safe for a log line and for a metric
#: attribute if one is ever added.
EMPTY_CAUSE_NO_TERMINAL = "no_terminal_event"
EMPTY_CAUSE_SYNTHETIC = "synthetic_completion"
EMPTY_CAUSE_VISIBLE_PARTIAL = "visible_partial"
EMPTY_CAUSE_TOOL_ONLY = "tool_only"
EMPTY_CAUSE_THINKING_ONLY = "thinking_only"
EMPTY_CAUSE_PROVIDER_EMPTY = "provider_empty"
EMPTY_CAUSE_OTHER = "other"

#: Which rung of the empty-response ladder claimed the turn.
EMPTY_RUNG_REPLAY = "replay"
EMPTY_RUNG_CONTINUE = "continue"
EMPTY_RUNG_GIVE_UP = "give_up"


def normalize_stop_reason(stop_reason: str | None) -> str:
    """Map a raw terminal stop reason onto the closed diagnostic vocabulary.

    ``None`` and ``""`` both answer :data:`STOP_REASON_ABSENT` — an omitted
    reason, which is a distinct observation from a clean ``end_turn`` and must
    not be laundered into one. Anything unrecognised answers
    :data:`STOP_REASON_OTHER`, so no raw backend string is ever logged.
    """
    raw = stop_reason or ""
    if not raw:
        return STOP_REASON_ABSENT
    if raw in (_STOP_END_TURN, _STOP_CANCELLED_REASON, _STOP_REFUSAL):
        return raw
    if raw.startswith("error:"):
        return "error"
    return STOP_REASON_OTHER


@dataclass(frozen=True)
class EmptyTurnActivity:
    """What a turn DID, in booleans only, as observed at the empty-response verdict.

    Every field is a bool or a value from a closed set. There are deliberately no
    counts, no durations, no token or credit numbers, no paths, no ids, and no
    text: this object exists to be written to a log line, and the incident it was
    built for is one where the interesting facts (did a tool run? did the user
    already read something?) are exactly the facts a privacy-safe diagnostic can
    carry. A count would answer no question the bool does not, and token counts
    and costs are billing data that has no business in a warning.

    ``billed`` is likewise a bool: whether the provider reported ANY billing
    dimension for the turn (``llm_helpers.usage_has_billing``). It separates the
    two shapes of "nothing came back" that look identical from the runner — a
    turn the provider generated and charged for, versus one it never ran.
    """

    #: A terminal ``EVENT_COMPLETE`` arrived. False means the stream ended
    #: without one, and every other field describes a turn nobody closed.
    saw_terminal: bool = False
    #: The terminal was SYNTHESIZED by the provider layer (watchdog, timeout,
    #: cancel-unacked) rather than reported by the backend. Retained past the
    #: event arm because the verdict below is reached long after it.
    terminal_synthetic: bool = False
    #: Normalised terminal stop reason — see :func:`normalize_stop_reason`.
    stop_reason: str = STOP_REASON_ABSENT
    #: At least one assistant text chunk streamed this turn.
    saw_text: bool = False
    #: A visible assistant segment was FLUSHED and persisted at a tool boundary,
    #: so the user has already read text this turn even though the final segment
    #: is empty. This is the incident's own shape.
    flushed_visible: bool = False
    #: At least one tool call was dispatched.
    had_tools: bool = False
    #: At least one thinking chunk arrived.
    had_thinking: bool = False
    #: The provider reported some billing dimension for the turn.
    billed: bool = False

    @property
    def productive(self) -> bool:
        """True when the turn did work that can carry state or side effects.

        This is the load-bearing predicate: a turn that is productive must NEVER
        have its originating message replayed verbatim, because the replay
        re-executes tool calls that already completed and re-derives an answer
        the user has already read. Text that merely STREAMED is not enough on its
        own — an un-flushed partial segment is still in ``assistant_text`` and is
        handled by the answer branch — so the three triggers are a flushed
        visible segment, a dispatched tool call, and thinking, each of which
        leaves the conversation in a state a replay would corrupt or duplicate.
        """
        return self.flushed_visible or self.had_tools or self.had_thinking


def classify_empty_turn(activity: EmptyTurnActivity) -> str:
    """Name the cause of an empty-response verdict, from the closed cause set.

    Ordered most-specific first, and the order is the point: the causes overlap
    (a synthesized terminal usually also has tool activity), so a flat set of
    predicates would report whichever the code happened to check first. The
    ranking is by what an operator must act on.

    1. :data:`EMPTY_CAUSE_NO_TERMINAL` — nobody closed the turn, so no other
       field can be trusted to describe a complete picture.
    2. :data:`EMPTY_CAUSE_SYNTHETIC` — the provider layer closed it, so the
       emptiness is ours, not the model's.
    3. :data:`EMPTY_CAUSE_VISIBLE_PARTIAL` — the user read an answer that a tool
       boundary flushed away; the turn is not empty in any sense the user would
       recognise.
    4. :data:`EMPTY_CAUSE_TOOL_ONLY` / :data:`EMPTY_CAUSE_THINKING_ONLY` — work
       happened with nothing said.
    5. :data:`EMPTY_CAUSE_PROVIDER_EMPTY` — a clean ``end_turn`` with no
       activity at all. The genuine provider-side empty, and the only cause for
       which replaying the original message is the right recovery.
    6. :data:`EMPTY_CAUSE_OTHER` — a closed terminal with no activity and no
       clean ``end_turn``, most importantly an OMITTED stop reason. Distinct
       from ``provider_empty`` on purpose: the incident's third attempt looked
       identical to a provider empty in the log and was not diagnosable.
    """
    if not activity.saw_terminal:
        return EMPTY_CAUSE_NO_TERMINAL
    if activity.terminal_synthetic:
        return EMPTY_CAUSE_SYNTHETIC
    if activity.flushed_visible:
        return EMPTY_CAUSE_VISIBLE_PARTIAL
    if activity.had_tools:
        return EMPTY_CAUSE_TOOL_ONLY
    if activity.had_thinking:
        return EMPTY_CAUSE_THINKING_ONLY
    if activity.stop_reason == _STOP_END_TURN:
        return EMPTY_CAUSE_PROVIDER_EMPTY
    return EMPTY_CAUSE_OTHER


def should_recover_promise_only(
    *,
    stop_reason: str,
    end_turn_reason: str,
    produced_visible_output: bool,
    final_segment_text: str,
    prompt_depth: int,
    promise_only_retries: int,
    is_cancelled: bool,
    refusal_reasons: list,
    turn_tool_calls: int = 0,
    in_stage_execution: bool = False,
    stop_in_progress: bool = False,
    stop_generation_unchanged: bool = True,
    queue_empty: bool = True,
    no_pending_steers: bool = True,
) -> bool:
    """Decide whether to inject ONE promise-only continuation.

    All must hold (each guards a failure mode the #2686 fix direction names):
      * NO Stop is in progress (``stop_in_progress`` is the runner's
        ``_should_suppress_requeue``). A soft Stop pressed while the promise
        streamed can lose the cancel race and arrive here as a normal
        ``end_turn``; re-queueing then would dispatch the very action the user
        tried to stop. Every sibling recovery path gates on this, so this one
        must too (#2696 GPT review, blocking);
      * NO Stop was pressed at ANY point during this turn
        (``stop_generation_unchanged``: the monotonic ``slot._stop_generation``
        snapshotted at turn start still matches). A Stop that pressed AND
        resolved back to idle during the turn is invisible to ``stop_in_progress``
        but the user still cancelled; do not re-dispatch the announced action
        (#2696 GPT review round 2, blocking);
      * NO user follow-up is queued (``queue_empty``). ``queue_insert(0, ...)``
        would jump the continuation ahead of a user-typed "don't do that" or
        clarifying message; the user's queued input must process FIRST — respect
        it by falling through to a normal landing instead of overriding it
        (#2696 GPT review round 2, blocking);
      * NO mid-turn steer is pending (``no_pending_steers``). A steer lands in
        ``slot._pending_steers``, a SEPARATE channel from ``_queue``; it is only
        degraded into a queue card in ``_run_chat``'s ``finally``, which runs
        AFTER this guard. So a "don't delete" steer is invisible to
        ``queue_empty`` here, and firing recovery would schedule the announced
        action despite the just-arrived revocation. Abort when any user input
        exists, in either channel (#2696 GPT review round 3, blocking);
      * the turn ended NORMALLY (``end_turn``), not cancelled/refused/errored —
        those have their own paths and must stay unchanged;
      * it produced visible output (a promise IS visible output) and is not the
        empty-response case (that path owns ``not produced_visible_output``);
      * the turn made NO tool calls (``turn_tool_calls == 0``). The segment-buffer
        reset at each tool boundary was the ORIGINAL "never replay an executed
        action" proxy, but it is not airtight: a turn that completed a side-
        effecting tool (e.g. ``send_message``) and then emitted trailing
        promise-shaped text ("I'll send that now") would still match the detector,
        and the continuation would REISSUE the completed action (duplicate external
        message). The promise-only bug is by definition a turn that announced an
        action and made NO tool call, so requiring a zero tool-call count closes the
        replay hole directly. A turn that ran a read then promised a further action
        is excluded too — a false negative, which is the safe direction (#2696 GPT
        review, blocking);
      * the final segment is a terminal promise-to-act
        (:func:`is_promise_only_terminal`). Because the runner resets its segment
        buffer at every tool boundary, ``final_segment_text`` is exactly the text
        AFTER the last tool call — so a turn that executed a tool and then
        summarised has a summary here, not a promise; the ``turn_tool_calls`` gate
        above is the airtight backstop for the same guarantee;
      * this is NOT a stage-execution turn (``in_stage_execution``). A turn run by
        the orchestrator's stage loop must not spawn async recovery: the loop
        records the stage complete and advances before the continuation finishes,
        corrupting stage attribution (#2696 GPT review, blocking);
      * this is a top-level turn (``prompt_depth == 0``) and the one-shot budget
        is unspent (``promise_only_retries < 1``) — bounded to a single attempt,
        never a loop.
    Model-agnostic: nothing here keys on a model id.

    Language scope: the terminal-promise detector (``is_promise_only_terminal``)
    matches English commitment/immediacy tokens only; a non-English promise-only
    turn falls through and lands normally (pre-fix behaviour). Failure bias is
    safe (false negative, not false positive)."""
    if stop_in_progress or not stop_generation_unchanged or not queue_empty:
        return False
    if not no_pending_steers:
        return False
    if is_cancelled or refusal_reasons:
        return False
    if turn_tool_calls != 0:
        return False
    if in_stage_execution:
        return False
    if stop_reason != end_turn_reason:
        return False
    if not produced_visible_output:
        return False
    if prompt_depth != 0 or promise_only_retries >= 1:
        return False
    return is_promise_only_terminal(final_segment_text)


def should_continue_after_compaction(
    *,
    compaction_started: bool,
    compaction_settled: bool,
    user_requested_compaction: bool,
    final_segment_text: str,
    stop_reason: str,
    end_turn_reason: str,
    prompt_depth: int,
    compaction_continue_retries: int,
    is_cancelled: bool,
    refusal_reasons: list,
    in_stage_execution: bool = False,
    stop_in_progress: bool = False,
    stop_generation_unchanged: bool = True,
    queue_empty: bool = True,
    no_pending_steers: bool = True,
) -> bool:
    """Decide whether to inject ONE post-compaction continuation.

    The scenario: the backend hit its context ceiling PART WAY THROUGH a turn,
    summarized the conversation in place, and then ended the turn without
    finishing the work it was doing. The compaction succeeded, so nothing
    reports an error and the turn lands as a clean ``end_turn`` with a settled
    footer — the request is simply abandoned, and the chat appears to stop dead.
    kiro-cli re-sends the pending request itself after compacting; the Claude
    backend does not, so the recovery has to live here.

    Unlike :func:`should_recover_promise_only` this needs no text detector and
    is deliberately NOT gated on ``turn_tool_calls == 0``: a mid-turn compaction
    is a hard fact reported by the backend, not a guess about what prose meant,
    and the turns that overflow the window are precisely the long tool-heavy
    ones. Re-dispatch safety comes from the continuation's own wording, which
    tells the model the summary records completed work and not to re-run it.

    All must hold:
      * a compaction STARTED and COMPLETED inside this turn
        (``compaction_started``/``compaction_settled``) — the turn really was
        interrupted by one, rather than merely following one. ``compaction_settled``
        means the COMPLETED terminal specifically, not any terminal: a compaction
        that FAILED also ends, and continuing after one would assert a summary
        exists when the context was never summarized;
      * the user did NOT ask for it (``user_requested_compaction`` is False for
        anything but an explicit ``/compact``). A deliberate ``/compact`` has
        done exactly what was asked and must land quietly; auto-continuing it
        would turn a housekeeping command into an unrequested turn;
      * the turn produced NO answer of its own after the compaction
        (``final_segment_text`` is blank). A backend that DID resume and finish
        leaves text here, so this is also what keeps the fix from double-
        prompting a backend that self-heals. A compaction NOTICE does not count
        as such an answer: the Claude adapter ships it as ordinary assistant text
        and nothing deletes the chunk, so the ACP layer flags it
        (``AcpEvent.control_notice``) and the caller SUBTRACTS the flagged chunks
        from the segment text it passes here. Subtracting at this one call rather
        than suppressing the chunk upstream is deliberate: the notice must still
        stream and persist, so the text path stays untouched and only this gate —
        the one reader that asks "did the turn answer?" — discounts it;
      * the turn ended NORMALLY (``end_turn``), was not cancelled, and carried
        no tool refusals — those have their own paths;
      * no Stop is in progress, no Stop was pressed at any point in the turn,
        no user follow-up is queued, and no mid-turn steer is pending. Same four
        user-intent gates every sibling recovery path uses, for the same reason:
        a queued continuation must never jump ahead of, or act against, input
        the user has already given;
      * this is NOT a stage-execution turn, it IS top-level
        (``prompt_depth == 0``), and the one-shot budget is unspent
        (``compaction_continue_retries < 1``) — one attempt, never a loop. The
        bound matters more here than elsewhere: if the continuation itself
        overflows the window again, an unbounded version would compact and
        continue forever.
    Model-agnostic and backend-agnostic: nothing here keys on a model id or a
    backend name — it reads the normalized compaction status every backend's
    adapter now produces.
    """
    if not compaction_started or not compaction_settled:
        return False
    if user_requested_compaction:
        return False
    if stop_in_progress or not stop_generation_unchanged or not queue_empty:
        return False
    if not no_pending_steers:
        return False
    if is_cancelled or refusal_reasons:
        return False
    if in_stage_execution:
        return False
    if stop_reason != end_turn_reason:
        return False
    if final_segment_text.strip():
        return False
    return prompt_depth == 0 and compaction_continue_retries < 1


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

    .. deprecated::
        Content-only classification is spoofable — a user typing the prefix
        text would be misclassified. Prefer :func:`is_system_injection_item`
        which checks the structural ``kind`` tag first. This function is kept
        only as a backwards-compatibility fallback for queue items enqueued
        before the kind tag was introduced.

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

#: Row-level kind for the `error` notice appended when a recovery has ALREADY
#: been queued, so the frontend can tell a pending retry from a terminal failure.
TRANSIENT_RETRY_KIND = "transient_retry"

#: Structural queue-entry kinds for system injections.  Classification by kind
#: tag — set at enqueue time — is unforgeable: a user typing the same prefix
#: text will not have the kind tag and will correctly classify as plain input.
SUBAGENT_COMPLETION_KIND = "subagent_completion"
CRON_NOTIFICATION_KIND = "cron_notification"

#: All system-injection kinds (for set-membership checks).
_SYSTEM_INJECTION_KINDS = frozenset(
    (SUBAGENT_COMPLETION_KIND, CRON_NOTIFICATION_KIND, SYNTHETIC_RECOVERY_KIND)
)


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

    Prefers the **structural** ``kind`` tag (set at enqueue time, unforgeable)
    over content-prefix inspection. Content fallback is removed to fully close
    the spoofing gap — classification is exclusively by kind tag.

    Synthetic recovery instructions are orchestration, not user speech: they
    must BREAK a user-message merge (folding one into a "[N queued messages
    merged]" turn would flip it back into user-authored, persisted,
    channel-mirrored history), keep draining during sub-agent runs, and never
    consume the session-reset notice — same treatment as sub-agent completion
    and cron injections."""
    kind = item.get("kind", "")
    if kind in _SYSTEM_INJECTION_KINDS:
        return True
    return False


def _dequeue_next_message(slot, merge_enabled: bool) -> tuple:
    """Drain the queue: merge non-cron messages or pop the first one."""
    if merge_enabled and len(slot._queue) > 1:
        to_merge: list[dict] = []
        for item in list(slot._queue):
            if is_system_injection_item(item):
                break
            to_merge.append(item)
        if len(to_merge) > 1:
            del slot._queue[: len(to_merge)]
            merged = "\n\n".join(item["content"] for item in to_merge)
            return f"[{len(to_merge)} queued messages merged]\n\n{merged}", to_merge
    item = slot.queue_pop(0)
    return item["content"], [item]


def _dequeue_next_system_message(slot, *, exclude_cron: bool = False) -> tuple:
    """Pop the first queued sub-agent-completion or cron injection, leaving
    plain user messages queued.

    Implements the (always-on) queue-during-subagents behavior: while background
    sub-agents run for a slot, a tangential user message is held (not drained)
    so it does not start a main turn mid-run, while system injections that must
    keep flowing (sub-agent completions, cron notifications) are still drained.
    Returns ``(content, [item])`` for the drained item, or ``(None, [])`` when
    only held (user) messages remain queued.

    ``exclude_cron`` additionally holds cron notifications. A multi-stage plan
    runs each stage as its own ``_run_chat`` whose tail-drain fires while
    ``_in_stage_execution`` is still set; without this a cron notification
    queued during the plan is pulled BETWEEN stages and starts a turn that
    scatters the plan's output. Sub-agent completions and synthetic recovery
    still flow (a stage may legitimately spawn sub-agents or re-queue a
    continuation) -- only the external cron injection waits for the plan to end.
    """
    for i, item in enumerate(slot._queue):
        if is_system_injection_item(item):
            if exclude_cron and item.get("kind") == CRON_NOTIFICATION_KIND:
                continue
            popped = slot.queue_pop(i)
            return popped["content"], [popped]
    return None, []


def _collapse_wire_rows(messages: list[dict]) -> list[dict]:
    """Reduce wire-only rows so that one row means one displayed message.

    ``chunk`` and ``done`` are wire-only roles that are never persisted.
    ``chunk`` is appended once per streamed delta, so a text segment still in
    flight occupies hundreds of rows that render as a single message, and
    ``done`` is a turn terminator that renders as nothing at all. A caller that
    bounds by row count is therefore counting stream progress and terminators
    rather than messages, and a bound taken before this reduction is spent on
    rows the response will not contain.

    Runs of ``chunk`` fold into one ``chunk`` row; ``done`` rows drop. This is
    the canonical first pass for :func:`_prepare_messages`, so dropping a
    terminator here rather than letting it split a run defines the render
    behaviour too. No redaction is applied and no other role is rewritten,
    which is what lets this run ahead of a slice without changing what the
    slice renders as.

    Input dicts are never mutated: a merged row is a fresh dict, because these
    rows are shared with the live window the event loop appends to.
    """

    def _merged(run: list[dict]) -> dict:
        if len(run) == 1:
            return run[0]
        # One join across the run, not a new string per delta: a long reply is
        # hundreds of deltas, and pairwise concatenation copies the text
        # accumulated so far every time, which is quadratic in the reply size.
        return {**run[0], "content": "".join(m.get("content", "") for m in run)}

    out: list[dict] = []
    run: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "chunk":
            run.append(m)
            continue
        if role == "done":
            continue
        if run:
            out.append(_merged(run))
            run = []
        out.append(m)
    if run:
        out.append(_merged(run))
    return out


# One opaque id for THIS gateway process, minted at import and never written to
# disk. That is the entire mechanism behind the gate below: an `mcp_oauth` banner
# records the generation that minted it, and a generation that is not this one is
# provably gone. Every ACP child is a subprocess of the gateway, so the loopback
# listener and the PKCE verifier that made the banner's URL redeemable died with it.
#
def _live_child_instance(state: "DashboardState", slot: "_ChatSlot") -> str:
    """Process-instance identity of the live ACP child serving *slot*, or ``""``.

    The verdict `_prepare_messages` needs to judge an open MCP OAuth banner: a
    banner's Authorize link is redeemable only while the child process that
    minted it is alive, because that process holds the loopback callback
    listener and the PKCE verifier (see `_expire_dead_child_oauth_meta`).
    Resolved here — on the event loop, where the session pool is in scope —
    because `_prepare_messages` itself is synchronous, may run in a worker
    thread, and has no ACP access.

    Resolves the ACTIVE TURN's session key first and the slot's routing only
    as a fallback, for the reason `_cancel_target` documents: a running turn
    owns a stable identity, while ``linked_session_key`` is mutable underneath
    it (a cron injection can rebind a live slot mid-turn). A banner minted
    during that turn belongs to the turn's own child; re-deriving the key from
    the routing would compare it against a different or absent provider and
    withdraw a link that still works.

    ``""`` covers every no-live-child case in one answer: no session in the
    pool (idle sweep, RSS recycle, reset, gateway restart), a session whose
    process has exited on its own, and a provider not backed by a process at
    all. Never raises: a probe failure answers ``""``, which withdraws a
    possibly-dead button rather than failing the read it rides on.
    """
    try:
        key = getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)
        provider = state.sessions.get_provider(key)
        if provider is None or not provider.is_process_alive():
            return ""
        return str(getattr(provider, "process_instance", "") or "")
    except Exception:
        logger.debug("live-child probe failed for slot %s", slot.key, exc_info=True)
        return ""


def _broadcast_expired_oauth_banners(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Push the read-time gate's current verdict on *slot*'s OAuth banners to open tabs.

    The gate in `_prepare_messages` runs only when a client FETCHES, so an
    already-open tab keeps rendering a dead Authorize link until its next
    refetch. This closes that window for the teardowns the dashboard can see:
    call it after any step that may have ended the slot's child (a session
    reset, an agent switch, a conversation discard, a watchdog recycle) and it
    re-broadcasts the withdrawal frame for every open banner the gate now
    judges dead.

    Verdict-driven, not ordering-driven: it resolves the live child at call
    time and applies the SAME predicate as the read gate, so it needs no
    pre-teardown snapshot and no outcome gating. Called after a teardown that
    failed or was declined, the child is still alive, the stamps still match,
    and nothing is broadcast; a successor session's banner names the successor
    child and is never swept. That is what lets a caller invoke it
    unconditionally where the old write-time retirement needed a doomed-set
    snapshot and a confirmed-teardown gate.

    Deliberately does NOT rewrite the stored rows — the read gate remains the
    one liveness mechanism and this is presentation freshness only. The frame
    carries ``oauth_url: ""`` (not an absent key) because the client merges
    incoming meta over the row it already has, and pre-upgrade JS keeps
    rendering a link whose key was merely omitted. Best-effort: teardowns the
    dashboard never observes (the idle sweep, a child exiting on its own) are
    still caught by the read gate on the tab's next refetch.
    """
    # Candidates first, pool second: almost every slot holds no open banner,
    # and the live-child probe reads the session pool — a side effect the
    # common case must not pay (and one that would burn a pool read at every
    # teardown site this helper rides).
    candidates = [
        message
        for message in slot.messages
        if message.get("role") == "mcp_oauth"
        for meta in (message.get("meta") or {},)
        if meta.get("oauth_url")
        and not (meta.get("completed") or meta.get("failed") or meta.get("superseded"))
    ]
    if not candidates:
        return
    live_child = _live_child_instance(state, slot)
    for message in candidates:
        meta = message.get("meta") or {}
        verdict = _expire_dead_child_oauth_meta("mcp_oauth", meta, live_child)
        if verdict is meta:
            # The gate returns the input unchanged when nothing applies — the
            # banner is live, already terminal, or has no link to withdraw.
            continue
        new_meta = _redact_meta_for_role("mcp_oauth", verdict)
        new_meta.pop("oauth_url", None)
        # Meta only, no content override: the read gate rewrites nothing but
        # meta, so a refetch serves the stored content with `expired` set and
        # the client renders the expired branch from the meta alone. A content
        # override here would diverge from what the next fetch says.
        state.broadcast_ws(
            "chat_message_update",
            {
                "slot": slot.key,
                "ts": message.get("ts", ""),
                "mid": str(meta.get("mid") or ""),
                "meta": {**new_meta, "oauth_url": ""},
            },
        )


def _expire_dead_child_oauth_meta(role: str, meta: dict, live_child: str) -> dict:
    """Present an ``mcp_oauth`` banner whose minting child is gone as terminal.

    A read-time gate, and it has to be read-time. The entity whose death
    invalidates an Authorize link is the ACP child process that minted it — that
    process holds the loopback callback listener and the PKCE verifier the code
    is exchanged with. A child can die without any code of ours running at a
    site that knows about banners: a hard gateway restart, the idle sweep, the
    RSS recycle, or the child simply exiting. Revalidating on every read is the
    same discipline ``connections.mint.expire_dead_holder`` already applies to
    the mint feed, instead of trying to enumerate every way a flow can end.

    ``live_child`` is the process-instance identity of the child CURRENTLY
    serving the slot, resolved by the caller (empty when there is no live
    child). It is a per-spawn token, never the ACP session id: a resume carries
    the same session id onto a NEW process, so a session-id comparison would
    fail open exactly when the minting process is gone.

    Withdraws the link only when ALL of these hold:

    * the row still carries an ``oauth_url`` — there is a live-looking button to
      take away. A row without one has nothing to withdraw and is left
      untouched, which is what keeps the two rejected-URL banners and an
      already-retired row alone.
    * the row is not already terminal. ``completed`` / ``failed`` /
      ``superseded`` are authoritative outcomes recorded by the process that
      observed them, and a later read must not reinterpret them.
    * the row's ``child`` stamp does not name the live child.

    A row carrying NO ``child`` stamp is treated as stale, and that is a
    deduction rather than a guess: the stamp is written by the same build that
    reads it, so an unstamped row was persisted by an older build — and running
    this build means this process replaced the one that hosted that row's
    child. The effect is that the fix also retires banners that went stale
    before it shipped, including rows carrying only the older gateway-``gen``
    stamp.

    Returns the input dict unchanged when nothing applies, so the caller can use
    the result unconditionally; only the withdrawing path copies.
    """
    if role != "mcp_oauth":
        return meta
    if not meta.get("oauth_url"):
        return meta
    if meta.get("completed") or meta.get("failed") or meta.get("superseded"):
        return meta
    child = meta.get("child")
    if child and live_child and child == live_child:
        return meta
    out = dict(meta)
    out.pop("oauth_url", None)
    out["expired"] = True
    return out


def _prepare_messages(messages: list[dict], running: bool, *, live_child: str) -> list[dict]:
    """Prepare messages for API response.

    ``live_child`` is the process-instance identity of the ACP child currently
    serving the slot ("" when none is alive) — see
    :func:`_expire_dead_child_oauth_meta`. REQUIRED and keyword-only on
    purpose: a caller that forgot it would silently withdraw every live OAuth
    banner it renders, and no test would see the omission. Resolve it with
    :func:`_live_child_instance` on the event loop, as close to the render as
    possible (a verdict sampled long before use can name a child that has
    since died, serving one stale read).
    """
    out: list[dict] = []
    for m in _collapse_wire_rows(messages):
        role = m.get("role", "")
        if role == "chunk":
            text = m.get("content", "")
            if text:
                text, _ = redact_exfiltration_urls(text)
                text, _ = redact_credentials(text)
                out.append({"role": "streaming", "content": text, "cls": "msg msg-a"})
            continue
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
            # Snapshot for the same reason as _redact_meta — this runs in a
            # worker thread (slot-detail render offload) while the event
            # loop may still be appending variants to the live list.
            msg_out["variants"] = [
                {
                    **v,
                    "content": redact_credentials(
                        redact_exfiltration_urls(v.get("content", ""))[0]
                    )[0],
                }
                for v in list(msg_out["variants"])
                if isinstance(v, dict)
            ]
        meta = parse_cls_meta(m.get("cls", ""))
        if meta is not None:
            msg_out["meta"] = _expire_dead_child_oauth_meta(
                role, _redact_meta_for_role(role, meta), live_child
            )
        elif isinstance(msg_out.get("meta"), dict):
            # Redact the STORED meta too. Without this branch the stored dict
            # passes through by reference (dict(m) is shallow), so it would
            # reach the client exactly as loaded. This is the only guard on
            # meta for the slot-detail response (the load path does not
            # redact meta).
            msg_out["meta"] = _expire_dead_child_oauth_meta(
                role, _redact_meta_for_role(role, msg_out["meta"]), live_child
            )
        out.append(msg_out)
    return out
