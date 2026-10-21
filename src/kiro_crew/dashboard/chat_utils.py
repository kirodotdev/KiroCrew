"""Shared utility functions for dashboard chat modules.

Redaction, model normalization, queue operations, stream chunk building,
persona injection, and other helpers used across chat_*.py modules.
"""

from __future__ import annotations

import importlib as _importlib
import json
import logging
import sys as _sys
import time
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMEvent

from kiro_crew import _facade_mirror
from kiro_crew.context_blocks import attributable_user_chars
from kiro_crew.dashboard.chat_utils_display import redact_display_content
from kiro_crew.dashboard.state import (
    DashboardState,
    _ChatSlot,
    append_and_surface,
    parse_cls_meta,
)
from kiro_crew.external_text import redact_external_text
from kiro_crew.quick_prompts import QUICK_PROMPTS
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import SecurityEvent, sel
from kiro_crew.validation import (
    MAX_TOOL_NAME_LEN,
    sanitize_string,
)

logger = logging.getLogger("kiro_crew.dashboard.chat_utils")


#: ``meta["notice"]`` on the three `error` rows the transient-5xx ladder appends
#: (chat_runner ``acp_error_is_transient`` branches). The row's CONTENT is the
#: English fallback below, read verbatim by non-dashboard consumers (channel
#: mirrors, SSE, an older frontend); the dashboard ignores it and renders
#: localized copy keyed on this token instead
#: (``website/src/pages/chat/transientNotice.ts``). A structured token rather
#: than prose-matching so the wording can change on either side without the
#: other silently falling back to raw English -- the drift class
#: ``test_recovery_marker_parity.py`` exists for. Both sides are still
#: hand-synced (no shared schema), so ``test_transient_notice_parity.py`` pins
#: these values against the frontend table.
TRANSIENT_NOTICE_META_KEY = "notice"
TRANSIENT_NOTICE_RETRYING = "transient_retrying"
TRANSIENT_NOTICE_RESUMING = "transient_resuming"
TRANSIENT_NOTICE_GIVE_UP = "transient_give_up"

#: English fallback text for the rows above. Plain language on purpose: the
#: failure is an upstream model-backend 5xx the gateway is already retrying
#: against, and neither "backend" nor "hiccup" tells a reader that.
TRANSIENT_RETRYING_TEXT = "⟳ Connection unstable — retrying…"
TRANSIENT_RESUMING_TEXT = "⟳ Connection unstable — resuming…"
TRANSIENT_GIVE_UP_TEXT = "⟳ Connection unstable — please try again."


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


#: Wire frame pushed onto ``slot._pending`` at a turn's end, before the queue
#: drain or the cycle's end writes anything (``chat_runner._mark_turn_end``); a
#: recovery the ending turn queued for itself is the same turn and gets none.
#: ``done`` marks the end of the whole queue cycle; this marks the end of one
#: turn, which is where an app's stream on a user's session stops
#: (``chat_handlers.api_chat``). Never a row: other readers skip it.
TURN_END_WIRE_CLS = "turn_end"


def _build_stream_chunk(msg: dict) -> str:
    """Build a JSON SSE chunk from a slot message, with meta redaction for permissions."""
    try:
        meta = parse_cls_meta(msg.get("cls", "")) if msg.get("role") == "permission" else None
    except Exception:
        logger.warning("Failed to parse cls meta for permission message", exc_info=True)
        meta = None
    if meta:
        meta = _redact_deep(meta)
    # One shared guard for content: redact recursively and hold the
    # wire-string shape (see redact_display_content). This stream has no
    # user-content carve-out — it redacts every role, same as its previous
    # inline redactor pair.
    content = redact_display_content(msg.get("content", ""))
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

# Blocked on the kiro path ONLY: the KiroACP harness does not implement /todos
# and rejects it with an "unknown variant" error, while Claude Code has its own.
_KIRO_ONLY_BLOCKED_SLASH_COMMANDS = frozenset({"/todos"})

# Commands that exist in kiro-cli's interactive TUI but cannot work in the
# dashboard (they drive a local terminal: quitting it, pasting from its
# clipboard, opening an editor, or toggling checkpoint modes the dashboard's
# own session model already covers via tabs and /side). Blocked commands are
# rejected before session acquisition AND excluded from the
# GET /api/slash-commands suggestion payload, so every surface hides them at
# once — advertising a command that only yields a warning teaches a gesture
# that does not work.
_BLOCKED_SLASH_COMMANDS = (
    frozenset({"/quit", "/exit", "/q", "/chat", "/paste", "/reply", "/editor", "/tangent"})
    | _KIRO_ONLY_BLOCKED_SLASH_COMMANDS
)

# Slash-command descriptions surfaced by the dashboard API
# (GET /api/slash-commands): the English text for API clients and the fallback
# the frontend shows for a command it has no catalog key for. A command with a
# ``slashCommandMenu.desc_*`` key in the frontend's i18n catalog renders that
# catalog text instead (SlashCommandMenu.tsx ``commandDescription``), so the two
# can differ. Keys are slash-prefixed command names. Covers every
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


def _tool_identity_fields(event: "LLMEvent") -> dict[str, str]:
    """``tool_name`` / ``mcp_server`` for a tool_call frame, present only when known.

    Read from the trusted ``_meta.kiro`` identity the dispatcher extracted, never
    from the title. Omitted rather than sent empty so a consumer merging a
    refinement field-by-field keeps what the initial frame supplied, and so a
    frame from a backend that sends no ``_meta`` carries nothing to misread. The
    dashboard's title derivation treats both as optional and falls back to
    parsing the title, so an absent pair never breaks a row.
    """
    out: dict[str, str] = {}
    name = getattr(event, "tool_name", "")
    server = getattr(event, "mcp_server_name", "")
    # Only a real string is an identity; any other value (an event shape that
    # lacks the attribute, a stand-in object) is treated as unknown.
    if isinstance(name, str) and name:
        out["tool_name"] = _redact_tool_field(name, limit=200)
    if isinstance(server, str) and server:
        out["mcp_server"] = _redact_tool_field(server, limit=200)
    return out


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
            **_tool_identity_fields(event),
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
    append_and_surface(state, slot, "assistant", msg_text, "msg msg-a", meta=meta)


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
    safe_agent = redact_external_text(agent)
    sel().log(
        SecurityEvent(
            event_id=uuid.uuid4().hex,
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            event_type="agent_assignment",
            caller_identity=f"dashboard:{slot_key}",
            agent=safe_agent,
            source="dashboard",
            operation="slot_agent_set",
            outcome=outcome,
            resources=f"slot={slot_key}",
        )
    )


def _validate_tool_name(tool_name: str, *, is_shell: bool = False, canonical_name: str = "") -> str:
    """Validate and sanitize tool display names for hook matching.

    ``is_shell`` is the provider-agnostic signal (set at the provider boundary)
    that this tool call is a shell/exec command, whose display title is the full
    command line and legitimately exceeds the length cap. Keying the exemption
    on this flag rather than a hardcoded set of provider tool_kind literals
    (e.g. "execute"/"Bash") stops the cap from silently re-breaking long shell
    commands on every engine migration or tool rename.

    ``canonical_name`` is the adapter-authored tool identity that travelled
    beside the title (``AcpEvent.tool_name``, read from the harness's own
    ``_meta`` channel, never from the title or the model's ``description``).
    The length cap protects the case where the title IS the only identity a
    hook can match on; when a canonical identity is present the title is
    content (a ``read`` title embeds the paths it reads, exactly as a shell
    title embeds its command line), so the cap is skipped for it as it is for
    ``is_shell``. Sanitisation and the empty check apply regardless: only the
    length predicate is relaxed. A backend that publishes no identity leaves
    ``canonical_name`` empty and keeps the loud refusal.
    """
    sanitized = sanitize_string(tool_name)
    if not sanitized:
        raise ValueError("Tool name cannot be empty")
    if not is_shell and not canonical_name and len(sanitized) > MAX_TOOL_NAME_LEN:
        raise ValueError(f"Tool name exceeds max length {MAX_TOOL_NAME_LEN}")
    return sanitized


# This module is the facade of the dashboard chat helpers: every name is reached through it
# as ``kiro_crew.dashboard.chat_utils.<name>``. A read is served by the owner that binds the
# name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every owner holding
# it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The code above stays
# here: the stream chunks, tool and compaction events and slash commands a turn emits, the
# deprecated model aliases, and the transient-notice tokens the frontend table is pinned to.
# New work goes to the owner of its responsibility:
#
#   ``chat_utils_slots`` -- a slot's session, history and transcript keys, and its memory mode
#   ``chat_utils_display`` -- display redaction and preparing stored rows for the wire
#   ``chat_utils_turn_text`` -- what a turn's final text claims: promise-only, leaked tool calls
#   ``chat_utils_injected_rows`` -- the rows the runner injects, their kinds, the next message
#   ``chat_utils_turn_recovery`` -- how a turn ended and whether the runner continues it
#   ``chat_utils_awaits`` -- cancellation-safe awaits for handlers that must finish a write
#   ``chat_utils_completion`` -- the end of a turn: sub-agent probes, the done payload
#   ``chat_utils_slack_options`` -- Slack option controls and paused mirrors
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.dashboard.chat_utils_awaits",
    "kiro_crew.dashboard.chat_utils_slots",
    "kiro_crew.dashboard.chat_utils_display",
    "kiro_crew.dashboard.chat_utils_turn_text",
    "kiro_crew.dashboard.chat_utils_injected_rows",
    "kiro_crew.dashboard.chat_utils_turn_recovery",
    "kiro_crew.dashboard.chat_utils_completion",
    "kiro_crew.dashboard.chat_utils_slack_options",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.dashboard.chat_utils_awaits import (  # noqa: F401
        drained_to_thread,
        run_config_write,
        run_to_completion,
    )
    from kiro_crew.dashboard.chat_utils_completion import (  # noqa: F401
        _CHUNK_GENERATION,
        SYNTHESIS_CLEAR,
        SYNTHESIS_HELD,
        SYNTHESIS_UNKNOWN,
        _attached_verdict,
        _queued_depth_off_loop,
        chat_done_payload,
        chunk_generation,
        subagents_attached,
        subagents_attached_async,
        synthesis_fire_verdict,
        wire_session_subagent_probe,
    )
    from kiro_crew.dashboard.chat_utils_display import (  # noqa: F401
        _DISPLAY_CLEAN_GENERATION,
        _DISPLAY_PREVIEW_CACHE_MAX_BYTES,
        _DISPLAY_PREVIEW_CACHE_MAX_ENTRIES,
        _DISPLAY_REDACTION_CACHE_MAX_BYTES,
        _DISPLAY_REDACTION_CACHE_MAX_ENTRIES,
        _DISPLAY_REDACTION_SALT,
        _DISPLAY_REDACTION_STRINGS_PER_ROW,
        _NO_BASE,
        REDACTION_RECORD_FIELDS,
        SLOT_DETAIL_MAX_LIMIT,
        UNKNOWN_ROW_FIELDS_KEY,
        _broadcast_expired_oauth_banners,
        _clean_verdict_key,
        _clear_display_redaction_cache,
        _collapse_wire_rows,
        _cut_preview,
        _display_clean_add,
        _display_clean_old,
        _display_clean_seen,
        _display_clean_young,
        _display_preview_cache,
        _display_preview_cache_bytes,
        _display_preview_cache_info,
        _display_redaction_cache,
        _display_redaction_cache_bytes,
        _display_redaction_cache_info,
        _display_redaction_cache_key,
        _display_redaction_cache_lock,
        _display_scope_changes_battery,
        _DisplayPreviewKey,
        _DisplayRedactionKey,
        _expire_dead_child_oauth_meta,
        _live_child_instance,
        _prepare_messages,
        _prepare_messages_scoped,
        _redact_for_display,
        _redact_meta,
        _redact_meta_for_role,
        _redact_value,
        _redacted_preview,
        _remember_preview,
        _remember_redacted,
        _variant_for_emit,
        adopt_variant_text,
        drop_records_without_placeholders,
        serialize_wire_content,
        variant_from_row,
        with_allowed_links_restored,
        with_bounded_redaction_records,
        without_unknown_row_fields,
    )
    from kiro_crew.dashboard.chat_utils_injected_rows import (  # noqa: F401
        _ACTIVITY_NO_REPLY_CONTINUE_MSG,
        _BUSY_RECOVER_MSG,
        _COMPACTION_CONTINUE_MSG,
        _CONN_RECOVER_MSG,
        _EMPTY_AUTO_CONTINUE_MSG,
        _POSTTOKEN_RECOVER_MSG,
        _PROMISE_ONLY_CONTINUE_MSG,
        _SYNTHETIC_RECOVERY_MSGS,
        _SYSTEM_INJECTION_KINDS,
        APP_AGENT_REFUSED_KIND,
        APP_MESSAGE_END,
        APP_MESSAGE_PREFIX,
        AUTH_REQUIRED_KIND,
        AUTOMATION_NOTICE_KIND,
        CONTEXT_OVERFLOW_KIND,
        CRON_NOTIFICATION_KIND,
        EMPTY_TURN_NOTICE_KIND,
        FALSE_TOOL_BLOCKER_REPLAY_KIND,
        IMAGE_REPAIR_KIND,
        KIRO_CLI_MISSING_KIND,
        MCP_APP_MESSAGE_KIND,
        MODEL_UNENTITLED_KIND,
        SESSION_START_FAILED_KIND,
        SPEC_HOOKS_NOTICE_KIND,
        STEER_INTENT_META,
        STEER_POSSIBLY_DELIVERED_META,
        STEER_POSSIBLY_DELIVERED_NOTE,
        SUBAGENT_COMPLETION_KIND,
        SUBAGENT_DELIVERY_KINDS,
        SYNTHETIC_RECOVERY_KIND,
        TRANSIENT_RETRY_KIND,
        USAGE_LIMIT_KIND,
        RecoveryPayload,
        _dequeue_next_message,
        _dequeue_next_system_message,
        _edit_queued_by_id,
        _remove_queued_by_id,
        _stamped_turn_actor,
        app_inject_row,
        carries_attachments,
        is_synthetic_payload_item,
        is_synthetic_recovery_item,
        is_system_injection,
        is_system_injection_item,
        payload_for_replay,
        releases_past_subagent_hold,
    )
    from kiro_crew.dashboard.chat_utils_slack_options import (  # noqa: F401
        _persisted_thread_owner,
        expire_slack_options,
        forget_slack_options,
        forget_slack_options_for_thread,
        mint_options_token,
        mirror_is_paused,
        options_control_is_stale,
        options_records,
        remember_slack_options,
        set_options_records,
        slack_mirror_is_paused,
        slack_options_linked_slot,
        slack_options_owner_key,
        slack_options_owner_keys_snapshot,
        slack_options_session_keys,
        slack_options_slot,
    )
    from kiro_crew.dashboard.chat_utils_slots import (  # noqa: F401
        _INCOGNITO_PREFIX,
        _TEMPORARY_PREFIX,
        ReplacementTightening,
        _apply_incognito_prefix,
        _history_key_for,
        _installed_theme_persona,
        _maybe_consolidate,
        _maybe_inject_persona,
        _resettle_restricted_key,
        _sync_dashboard_slots,
        apply_pending_slot_memory_mode,
        dashboard_slot_key,
        effective_session_key,
        reject_if_slot_under_construction,
        replacement_shares_transcript,
        restore_replacement_if_handover_did_not_land,
        session_key_for,
        slot_history_key,
        slot_transcript_key,
        subagent_event_slot,
        tighten_live_slot_memory_mode,
        tighten_replacement_to_restricted_original,
    )
    from kiro_crew.dashboard.chat_utils_turn_recovery import (  # noqa: F401
        _CONTINUATION_BY_CAUSE,
        _FALSE_CURRENT_TOOL_BLOCKER_NEAR_MISS_RE,
        _FALSE_CURRENT_TOOL_BLOCKER_RE,
        _MANUAL_CONTINUE_MSG,
        _MANUAL_RESUME_MSG,
        _READ_ONLY_PREPARATION_TOOLS,
        _REFUSAL_FALLBACK_RESUME_MSG,
        _STOP_CANCELLED_REASON,
        _STOP_END_TURN,
        _STOP_REFUSAL,
        EMPTY_CAUSE_NO_TERMINAL,
        EMPTY_CAUSE_OTHER,
        EMPTY_CAUSE_PROVIDER_EMPTY,
        EMPTY_CAUSE_SYNTHETIC,
        EMPTY_CAUSE_THINKING_ONLY,
        EMPTY_CAUSE_TOOL_ONLY,
        EMPTY_CAUSE_VISIBLE_PARTIAL,
        EMPTY_RUNG_CONTINUE,
        EMPTY_RUNG_GIVE_UP,
        EMPTY_RUNG_REPLAY,
        STOP_REASON_ABSENT,
        STOP_REASON_OTHER,
        EmptyTurnActivity,
        ResetCause,
        build_recovery_requeue,
        classify_empty_turn,
        is_false_current_tool_blocker,
        is_false_current_tool_blocker_near_miss,
        normalize_stop_reason,
        should_continue_after_compaction,
        should_recover_promise_only,
        tool_calls_are_read_only_preparation,
    )
    from kiro_crew.dashboard.chat_utils_turn_text import (  # noqa: F401
        _APPROVAL_GATED_RE,
        _CODE_DELIM_RE,
        _LEAKED_INVOKE_BODY_RE,
        _LEAKED_INVOKE_OPEN_RE,
        _NEGATED_PROMISE_RE,
        _NO_ACTION_CLOSER_RE,
        _PROMISE_HAS_FOLLOWING_CONTENT_RE,
        _PROMISE_NOW_RE,
        _SENTENCE_BOUNDARY_RE,
        _UNFINISHED_PROGRESS_RE,
        _strip_quoted_code,
        _terminal_sentence,
        has_leaked_tool_call,
        has_unfinished_progress_claim,
        is_promise_only_terminal,
        open_construct_at_end,
        should_notice_compaction_dropped_leak,
        should_notice_leaked_tool_call,
        should_notice_mixed_turn_leak,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew.dashboard import display_verdicts as _display_verdicts  # noqa: F401
    from kiro_crew.dashboard.display_battery import (  # noqa: F401
        display_battery,
    )
    from kiro_crew.dashboard.slot_queue_repository import (  # noqa: F401
        ATTACHMENT_META_KEYS,
    )
    from kiro_crew.dashboard.state import (  # noqa: F401
        BUSY_RECOVERY_PREFIX,
        COMPACTION_RECOVERY_PREFIX,
        CONN_RECOVERY_PREFIX,
        CRON_NOTIFY_PREFIX,
        EMPTY_RESPONSE_RECOVERY_PREFIX,
        MANUAL_RESUME_RECOVERY_PREFIX,
        POSTTOKEN_RECOVERY_PREFIX,
        PROMISE_ONLY_RECOVERY_PREFIX,
        REFUSAL_FALLBACK_RECOVERY_PREFIX,
        SUBAGENT_COMPLETION_PREFIXES,
        _normalize_slot_key,
    )
    from kiro_crew.execution_context import (  # noqa: F401
        ExecutionContext,
        canonical_memory_mode,
        read_live_session_execution,
        rollback_live_session_tightening,
        stricter_memory_mode,
        tighten_live_session_execution,
    )
    from kiro_crew.history import (  # noqa: F401
        is_incognito_transcript,
        transcript_lock_stems,
        transcript_sort_key,
        transcript_stems,
    )
    from kiro_crew.hooks import (  # noqa: F401
        _HOST_READ_ONLY_BUILTIN_TOOLS,
        safe_read_file,
    )
    from kiro_crew.memory_stores import (  # noqa: F401
        UnknownMemoryStore,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        canonical_key,
        is_channel_session_key,
    )
    from kiro_crew.messaging.renderer import (  # noqa: F401
        split_options_trailer,
    )
    from kiro_crew.messaging.split import (  # noqa: F401
        FENCE_CLOSE,
        FENCE_OPEN,
        iter_fence_lines,
    )
    from kiro_crew.security import (  # noqa: F401
        CREDENTIAL_REDACTION_TAGS,
        EXFILTRATION_REDACTION_TAG_PREFIX,
        _exempt_exact_hosts,
        bounded_blocked_links,
        oauth_url_contains_credential,
    )
    from kiro_crew.security.credential_sources import (  # noqa: F401
        bounded_credential_records,
    )
    from kiro_crew.security.exfil import (  # noqa: F401
        current_scoped_exempt_hosts,
        restore_allowed_links,
        scoped_exempt_hosts,
    )
    from kiro_crew.security.redaction import (  # noqa: F401
        _token_param_exempt_prefixes,
    )
    from kiro_crew.security.redaction_allow import (  # noqa: F401
        allowed_hosts_for,
    )
    from kiro_crew.session_surface import (  # noqa: F401
        has_dashboard_surface,
        set_dashboard_surfaced,
    )
    from kiro_crew.slack.outbound import (  # noqa: F401
        decode_options_token,
        encode_options_token,
        expire_options,
        mark_options_terminal,
        options_edit_lock,
    )
    from kiro_crew.validation import (  # noqa: F401
        THEME_CONSENT_SHA_RE,
    )
    from kiro_crew.widget_parse import (  # noqa: F401
        parse_widgets,
    )
