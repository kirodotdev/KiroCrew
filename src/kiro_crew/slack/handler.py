"""Message handler — streams LLM responses to Slack with tool approval UI.

Routes incoming Slack messages through hooks, cron command interception,
and the LLM provider.  Supports interactive tool approval via Block Kit
buttons.

Session privacy modes
---------------------
Temporary (blank-slate): no memory reads, no memory writes, no persistence.
    The session starts with zero context and discards everything on close.
Incognito: memory reads allowed but writes blocked; persists an ephemeral
    conversation log that is discarded on close.

Both modes live in :mod:`kiro_crew.messaging.privacy_mode`, keyed by session key,
so a second channel inherits the same machinery; the names in this module are
thin Slack-facing wrappers over it.  Use :func:`_is_slack_restricted` to check
whether a Slack session should skip memory writes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

from kiro_crew import name_grant
from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import (
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
)
from kiro_crew.agent_discovery import project_agent_files, project_agent_name
from kiro_crew.config.loader import (
    ACTIVATION_REVIEW,
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    update_config_locked,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.context import (
    ContextBuilder,
    build_cancelled_turn_preamble,
    compress_thread_history,
    window_for_provider_client,
)
from kiro_crew.cron import CronService
from kiro_crew.dashboard.chat_utils import (
    expire_slack_options,
    mint_options_token,
    options_control_is_stale,
    remember_slack_options,
    run_config_write,
)
from kiro_crew.dashboard.state import append_and_surface
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import (
    HOOK_REPLY,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    event_is_spawn_run,
    safe_read_file_bytes,
    validate_file_path,
)
from kiro_crew.llm_helpers import (
    record_interaction_event,
    save_conversation_turn_off_loop,
)
from kiro_crew.messaging import auto_title, privacy_mode
from kiro_crew.messaging.commands import (
    compact_unsupported_backend,
    compact_unsupported_reply,
    cron_command_reply,
    spawn_command_reply,
    task_command_reply,
)
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import canonical_key
from kiro_crew.messaging.renderer import credential_redaction_notice
from kiro_crew.messaging.session_trust import _trusted_sessions as _shared_trusted_sessions
from kiro_crew.messaging.session_trust import add_trusted_session as _add_trusted_session
from kiro_crew.messaging.session_trust import clear_trusted_sessions, is_session_trusted
from kiro_crew.platform import current_context
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
    EVENT_TOOL_CALL,
    LLMEvent,
    LLMProvider,
)
from kiro_crew.safety_override import (
    SafetyOverride,
    apply_config_duration,
    describe_grant_lifetime,
    describe_new_grant,
    grant_declared_yolo,
    safety_override,
)
from kiro_crew.security import (
    CREDENTIAL_REDACTION_TAGS,
    StreamRedactor,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session import SessionClosingError, SessionManager
from kiro_crew.session_map import SessionMap
from kiro_crew.slack.blocks import build_working_blocks, deprecation_warning_block
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import (
    SLACK_MSG_LIMIT,
    TRUNCATION_NOTICE,
    _convert_tables,
    extract_options,
    render_one_for_slack,
    split_message,
    strip_thinking_tags,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.sessions_view import (
    _SESSIONS_DEFAULT_LIMIT,
    _build_sessions_blocks,
    _collect_recent_sessions_off_loop,
)
from kiro_crew.stats import Stats
from kiro_crew.subagent import SubagentManager
from kiro_crew.task import Task
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.voice_reply import DEFAULT_PROVIDER, VALID_PROVIDERS
from kiro_crew.voice_reply import is_available as _tts_available
from kiro_crew.voice_reply import validate_length_scale as _validate_length_scale
from kiro_crew.voice_reply import voice_reply as _voice_reply_fn

logger = logging.getLogger(__name__)

# Mapping of bang commands to their /kirocrew slash equivalents.
_BANG_TO_SLASH: dict[str, str] = {
    "!yolo": "/kirocrew yolo",
    "!stop": "/kirocrew stop",
    "!voice": "/kirocrew voice",
    "!agent": "/kirocrew agent",
    "!dashboard": "/kirocrew dashboard",
    "!ta": "/kirocrew agent",
    # "!allowlist" removed — multi-user access disabled for security
    "!channel": "/kirocrew channel",
    "!link-to-dashboard": "/kirocrew link-to-dashboard",
    "!restart": "/kirocrew restart",
}

# Approval modes (UX-level, not provider-specific)
APPROVAL_AUTO = "auto"
APPROVAL_INTERACTIVE = "interactive"


def _should_auto_approve_spawn(context_builder, event) -> bool:
    """Check if a spawn_run tool call should be auto-approved.

    Takes the PERMISSION EVENT, not the title: the title is model-authored
    (a shell command's title can be forged to ``spawn_run``), so the check
    keys on ``event_is_spawn_run``'s canonical identity.
    """
    return bool(
        context_builder
        and context_builder.hooks
        and context_builder.hooks.auto_approve_subagent_spawn
        and event_is_spawn_run(event)
    )


# Min interval between Slack message edits (avoid rate limits)
_EDIT_INTERVAL = 1.0

# Timeout for user to click approve/reject before auto-rejecting
_APPROVAL_TIMEOUT = 120.0

# Slack Block Kit section text limit (3000 chars max); leave room for
# markdown fences (``` ... ```) that wrap the tool input.
_SLACK_SECTION_TEXT_LIMIT = 2900

# Truncation marker appended when tool_input exceeds the limit
_TRUNCATION_MARKER = "\n… [truncated]"

# Slack UX strings
_THINKING = "_Thinking…_"
_THINKING_PLACEHOLDER = "💭 _Thinking…_"
_CURSOR = " ▍"
_NO_RESPONSE = "_No response._"
_STATUS_WORKING = "is working on your request"

# Max chars of reasoning to surface inline in Slack before truncating. Keeps
# the 💭 Thinking block from becoming a wall of text; the full
# reasoning remains available in the dashboard Activity panel.
_THINKING_PREVIEW_LIMIT = 600


def _condense_thinking(mrkdwn: str, *, limit: int = _THINKING_PREVIEW_LIMIT) -> str:
    """Render reasoning as a subdued, truncated Slack blockquote.

    Keeps the reasoning visible but prevents a wall of text: truncates to
    ``limit`` chars on a whitespace boundary and renders each line as a
    blockquote so it appears indented/muted relative to the answer.

    Args:
        mrkdwn: Reasoning text, already converted to Slack mrkdwn and redacted.
        limit: Soft character cap before truncation.

    Returns:
        A Slack-mrkdwn string headed by ``💭 *Thinking*``.
    """
    text = mrkdwn.strip()
    truncated = False
    if len(text) > limit:
        # Break on the last whitespace (space, newline, tab) in the window so
        # reasoning whose only break is a newline still cuts cleanly instead of
        # falling through to the hard cut.
        boundaries = list(re.finditer(r"\s", text[:limit]))
        cut = (
            boundaries[-1].start() if boundaries and boundaries[-1].start() >= limit // 2 else limit
        )
        text = text[:cut].rstrip()
        truncated = True
    quoted = "\n".join(f"> {ln}" if ln.strip() else ">" for ln in text.splitlines())
    suffix = "\n> _…full reasoning in dashboard Activity_" if truncated else ""
    return f"💭 *Thinking*\n{quoted}{suffix}"


# Pending approvals: keyed by f"{channel}:{approval_msg_ts}"
# Module-level dict — safe because gateway runs in a single asyncio event loop.
_pending_approvals: dict[str, _PendingApproval] = {}

# ── Phase-aware reaction constants ──────────────────────────────────────

_DEFAULT_PHASE_EMOJIS: dict[str, str] = {
    "queued": "eyes",
    "thinking": "thinking_face",
    "coding": "man_technologist",
    "browsing": "globe_with_meridians",
    "tool": "wrench",
    "done": "lobster",
    "error": "scream",
}


def _build_phase_emojis(
    overrides: dict[str, str | None] | None = None,
) -> tuple[dict[str, str | None], list[str]]:
    """Return ``(phase_emoji_dict, unknown_keys)`` with optional overrides applied.

    A phase value may be ``None`` to suppress that phase entirely (no emoji
    will be added or swapped in for it).  Stall emojis and transitions from
    other phases are unaffected.

    Unknown keys are collected and returned so callers can surface them
    to the user (e.g. startup warning) rather than silently dropping them.
    """
    result: dict[str, str | None] = dict(_DEFAULT_PHASE_EMOJIS)
    unknown: list[str] = []
    for key, value in (overrides or {}).items():
        if key in _DEFAULT_PHASE_EMOJIS:
            result[key] = value
        else:
            unknown.append(key)
    return result, unknown


try:
    _overrides = KiroCrewConfig.load().slack.reactions
except Exception:
    logger.warning("Failed to load reaction overrides from config; using defaults", exc_info=True)
    _overrides = {}
_PHASE_EMOJIS, _unknown_phases = _build_phase_emojis(_overrides)
del _overrides
if _unknown_phases:
    logger.warning(
        "Ignoring unknown slack.reactions keys: %s (valid: %s)",
        ", ".join(repr(k) for k in _unknown_phases),
        ", ".join(sorted(_DEFAULT_PHASE_EMOJIS)),
    )
del _unknown_phases


async def _add_phase_reaction(slack: SlackClientOps, channel: str, ts: str, phase: str) -> None:
    """Add the reaction for *phase* if the user hasn't suppressed it.

    Used by one-shot emoji-ack sites outside ``StatusReactionController``
    (e.g. ``!command`` handlers).  Honours ``slack.reactions`` ``null``
    suppression sentinels.
    """
    emoji = _PHASE_EMOJIS.get(phase)
    if emoji is None:
        return
    await slack.add_reaction(channel, ts, emoji)


_STALL_EMOJI_SOFT = "yawning_face"
_STALL_EMOJI_HARD = "fearful"

_STALL_SOFT_SECS = 15.0
_STALL_HARD_SECS = 45.0
_PHASE_DEBOUNCE_SECS = 0.7

_TERMINAL_PHASES = frozenset({"done", "error"})
_IMMEDIATE_PHASES = frozenset({"queued"})

_CODING_TOOLS: frozenset[str] = frozenset(
    {"Bash", "Write", "Edit", "Read", "Glob", "Grep", "NotebookEdit"}
)
_WEB_TOOLS: frozenset[str] = frozenset({"WebFetch", "WebSearch", "Browser"})

_CODING_KINDS: frozenset[str] = frozenset(t.lower() for t in _CODING_TOOLS)
_WEB_KINDS: frozenset[str] = frozenset(t.lower() for t in _WEB_TOOLS)


def _tool_to_phase(tool_name: str, tool_kind: str = "") -> str:
    """Map a tool name/kind to a reaction phase."""
    kind_lower = tool_kind.lower()
    if kind_lower:
        if kind_lower in _CODING_KINDS:
            return "coding"
        if kind_lower in _WEB_KINDS:
            return "browsing"
    # Extract base tool name for MCP tools (mcp__example-mcp__Bash → Bash)
    base = tool_name.split("__")[-1] if "__" in tool_name else tool_name
    if base in _CODING_TOOLS:
        return "coding"
    if base in _WEB_TOOLS:
        return "browsing"
    return "tool"


class StatusReactionController:
    """Phase-aware Slack reaction controller with debounce and stall detection.

    Provides richer emoji feedback than the old binary eyes/lobster pair.
    Intermediate phases are debounced so rapid tool transitions don't spam
    the Slack API.  A stall watchdog adds yawning/fearful reactions when
    the agent appears stuck.
    """

    def __init__(
        self, slack: SlackClientOps, channel: str, ts: str, *, enabled: bool = True
    ) -> None:
        self._enabled = enabled
        self._slack = slack
        self._channel = channel
        self._ts = ts
        self._loop = asyncio.get_running_loop()

        self._current_emoji: str | None = None
        self._pending_phase: str | None = None
        self._debounce_handle: asyncio.TimerHandle | None = None
        self._stall_soft_handle: asyncio.TimerHandle | None = None
        self._stall_hard_handle: asyncio.TimerHandle | None = None
        self._stall_emoji: str | None = None
        self._stall_paused = False
        self._finalized = False

    # ── public API ──────────────────────────────────────────────────

    def set_phase(self, phase: str) -> None:
        """Request a phase transition (may be debounced)."""
        if self._finalized or not self._enabled:
            return

        if phase in _TERMINAL_PHASES:
            self.finalize(error=(phase == "error"))
            return

        if phase in _IMMEDIATE_PHASES:
            self._cancel_debounce()
            emoji = _PHASE_EMOJIS.get(phase, phase)
            asyncio.ensure_future(self._swap_emoji(emoji))
            self._reset_stall_watchdog()
            return

        # Intermediate phase — debounce
        self._pending_phase = phase
        self._cancel_debounce()
        self._debounce_handle = self._loop.call_later(_PHASE_DEBOUNCE_SECS, self._fire_debounce)

    def on_progress(self) -> None:
        """Reset stall watchdog — call on any LLM/tool activity."""
        if not self._finalized and not self._stall_paused and self._enabled:
            self._reset_stall_watchdog()

    def pause_stall_watchdog(self) -> None:
        """Pause stall detection (e.g. waiting for user approval)."""
        self._stall_paused = True
        self._cancel_stall_timers()

    def resume_stall_watchdog(self) -> None:
        """Resume stall detection after a pause."""
        self._stall_paused = False
        if not self._finalized and self._enabled:
            self._reset_stall_watchdog()

    def finalize(self, error: bool = False) -> None:
        """Swap to terminal emoji. Idempotent."""
        if self._finalized or not self._enabled:
            return
        self._finalized = True
        self._cancel_debounce()
        self._cancel_stall_timers()
        # Clean up stall emoji before setting terminal
        asyncio.ensure_future(self._do_finalize(error))

    # ── internal ────────────────────────────────────────────────────

    async def _do_finalize(self, error: bool) -> None:
        if self._stall_emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
            self._stall_emoji = None
        terminal = _PHASE_EMOJIS["error" if error else "done"]
        await self._swap_emoji(terminal)

    def _fire_debounce(self) -> None:
        """Timer callback — bridge to async."""
        asyncio.ensure_future(self._apply_pending())

    async def _apply_pending(self) -> None:
        if self._finalized or self._pending_phase is None:
            return
        emoji = _PHASE_EMOJIS.get(self._pending_phase, self._pending_phase)
        self._pending_phase = None
        await self._swap_emoji(emoji)
        self._reset_stall_watchdog()

    async def _swap_emoji(self, new_emoji: str | None) -> None:
        """Remove old reaction and add new one (skip if same).

        ``new_emoji=None`` means the phase is suppressed by config: remove
        any previously-applied reaction but do not add a replacement.
        """
        if new_emoji == self._current_emoji:
            return
        old = self._current_emoji
        self._current_emoji = new_emoji
        if old:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, old)
            except Exception:
                pass
        if new_emoji is None:
            return
        try:
            await self._slack.add_reaction(self._channel, self._ts, new_emoji)
        except Exception:
            pass

    def _cancel_debounce(self) -> None:
        if self._debounce_handle is not None:
            self._debounce_handle.cancel()
            self._debounce_handle = None

    def _cancel_stall_timers(self) -> None:
        if self._stall_soft_handle is not None:
            self._stall_soft_handle.cancel()
            self._stall_soft_handle = None
        if self._stall_hard_handle is not None:
            self._stall_hard_handle.cancel()
            self._stall_hard_handle = None

    def _reset_stall_watchdog(self) -> None:
        if not self._enabled:
            return
        self._cancel_stall_timers()
        # Remove existing stall emoji
        if self._stall_emoji:
            emoji_to_remove = self._stall_emoji
            self._stall_emoji = None
            asyncio.ensure_future(self._remove_stall_emoji(emoji_to_remove))
        if self._stall_paused or self._finalized:
            return
        self._stall_soft_handle = self._loop.call_later(_STALL_SOFT_SECS, self._on_stall_soft)
        self._stall_hard_handle = self._loop.call_later(_STALL_HARD_SECS, self._on_stall_hard)

    async def _remove_stall_emoji(self, emoji: str) -> None:
        try:
            await self._slack.remove_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass

    def _on_stall_soft(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_SOFT))

    def _on_stall_hard(self) -> None:
        asyncio.ensure_future(self._add_stall_emoji(_STALL_EMOJI_HARD))

    async def _add_stall_emoji(self, emoji: str) -> None:
        if self._finalized:
            return
        # Remove previous stall emoji if upgrading
        if self._stall_emoji and self._stall_emoji != emoji:
            try:
                await self._slack.remove_reaction(self._channel, self._ts, self._stall_emoji)
            except Exception:
                pass
        self._stall_emoji = emoji
        try:
            await self._slack.add_reaction(self._channel, self._ts, emoji)
        except Exception:
            pass


# Trust/YOLO state
# trust: auto-approve tools for a specific session (via Trust button)
# yolo: auto-approve all tools globally for all sessions (via !yolo on command, owner-only)
#: Re-exported from the shared per-session trust set so Slack and every channel
#: read ONE grant. Kept under this name because interactions.py, the dashboard's
#: approval-mode reset and the Slack suites all reach it here.
_trusted_sessions = _shared_trusted_sessions
# Deprecated alias kept for import compatibility. `!yolo on` is an AD-HOC
# grant, so it now uses the SAME duration as the dashboard picker and the API
# (agent.yolo_duration, default 6h) — a per-surface TTL made the behavior
# unpredictable without buying security. Read the live value, never this.
_YOLO_TTL_SECS = SafetyOverride._ADHOC_TTL_DEFAULT


# Allowed user IDs for Slack access (set by gateway at startup).
# Falls back to single KIROCREW_OWNER_ID for backward compatibility.
_allowed_users: set[str] = set()


# ── Voice reply state ──
@dataclass
class _VoiceConfig:
    """Per-session and global voice reply settings."""

    sessions: set[str] = None  # type: ignore[assignment]  # threads with voice on
    global_enabled: bool = False
    auto_speak: bool = False
    voices: dict[str, str] = None  # type: ignore[assignment]
    engines: dict[str, str] = None  # type: ignore[assignment]
    rates: dict[str, str] = None  # type: ignore[assignment]
    pitches: dict[str, str] = None  # type: ignore[assignment]
    default_voice: str = "Ruth"
    default_engine: str = "generative"
    default_rate: str = "100%"
    default_pitch: str = "+0%"
    aws_profile: str = ""
    region: str = ""
    # TTS provider. Defaults to the LOCAL provider (Piper), matching
    # ``voice_reply.DEFAULT_PROVIDER``. It used to default to "polly" here,
    # which meant enabling voice reply without naming a provider silently sent
    # text to a paid AWS service under whatever the ambient credential chain
    # resolved to. Sourced from the single constant so the two cannot drift
    # again.
    provider: str = DEFAULT_PROVIDER
    # Piper-specific (ignored when provider="polly"):
    piper_binary: str = ""
    piper_model: str = ""
    piper_model_config: str = ""
    piper_length_scale: float = 1.0
    # If True, a message carrying voice input (a transcribed voice memo)
    # automatically receives a voice reply, even without `!voice on`. The
    # config-load default follows ``enabled`` (see ``set_orch_cfg``); the
    # in-memory default below is False so an unconfigured ``_VoiceConfig``
    # behaves the same as a default-config user (``enabled=false``).
    auto_reply_to_voice: bool = False

    def __post_init__(self) -> None:
        self.sessions = self.sessions or set()
        self.voices = self.voices or {}
        self.engines = self.engines or {}
        self.rates = self.rates or {}
        self.pitches = self.pitches or {}


_vc = _VoiceConfig()

# Primary owner ID — for owner-only commands like !agent.
_owner_id: str = ""

# Tracked channel IDs for member_joined_channel monitoring.
_tracking_channels: set[str] = set()
_open_channels: set[str] = set()

# Live reference to the orchestrator's config — set by events.py, reloaded
# after !channel writes so activation changes take effect immediately.
_orch_cfg: KiroCrewConfig | None = None

# Dashboard state reference for pushing refresh events (set by gateway).
_dashboard_state: object | None = None


_cached_default_agent: str | None = None  # None = not yet loaded from disk

# Per-thread agent overrides: session_key → agent name.
# Set via !ta command (thread-agent).
_thread_agents: dict[str, str] = {}

# Per-thread project directory overrides: session_key → absolute path.
# Set via !project command.
_thread_projects: dict[str, str] = {}

# Guard set for _hydrate_thread_overrides to avoid repeated I/O per session.
_hydrated_sessions: set[str] = set()

# The privacy-mode machinery lives in ``messaging.privacy_mode`` so a second
# channel gets the same trackers, the same durable flag and the same audit rather
# than a second copy of them. The names below are the Slack-facing spellings the
# ~45 enforcement sites in this package (and the dashboard) already import; each
# is a thin wrapper. The two LRU dicts are ALIASES of the shared objects, not
# copies — a caller (or a test fixture) that mutates one is mutating the tracker
# the shared module reads.
_thread_temporary = privacy_mode._temporary
_thread_incognito = privacy_mode._incognito

_mark_temporary = privacy_mode.mark_temporary
_mark_incognito = privacy_mode.mark_incognito
is_thread_temporary = privacy_mode.is_temporary
is_thread_incognito = privacy_mode.is_incognito

_RESTRICTED_WRITE_MSG = "Memory writes are not allowed in this session mode."

_INCOGNITO_TOKEN_RE = privacy_mode.INCOGNITO_TOKEN_RE
_TEMPORARY_TOKEN_RE = privacy_mode.TEMPORARY_TOKEN_RE


def _is_slack_restricted(session_key: str) -> bool:
    """Return True if this Slack session should skip memory writes.

    The predicate itself is namespace-agnostic (see
    :func:`kiro_crew.messaging.privacy_mode.is_restricted`); the Slack spelling
    survives because this package's enforcement sites are named for it.
    """
    return privacy_mode.is_restricted(session_key)


def _conv_state_map(sessions: object) -> "SessionMap | None":
    """Return the SessionManager's canonical SessionMap, or None.

    Thin wrapper over :func:`kiro_crew.messaging.privacy_mode.conv_state_map`,
    which documents why requiring the real class (rather than any attribute) is
    load-bearing for a test double.
    """
    sm = privacy_mode.conv_state_map(sessions)
    return sm if isinstance(sm, SessionMap) else None


def _hydrate_conv_flags(sessions: object, session_key: str) -> None:
    """Restore persisted temporary/incognito flags into the in-memory caches.

    Called once per session in ``handle_message`` so a thread marked temporary
    or incognito stays so across a gateway restart (the in-memory LRU is rebuilt
    from the durable ``SessionMap`` entry).
    """
    privacy_mode.hydrate(sessions, session_key)


def _strip_incognito_token(text: str) -> tuple[str, bool]:
    """Remove standalone ``!incognito`` token from *text*."""
    return privacy_mode.strip_token(text, privacy_mode.MODE_INCOGNITO)


def _strip_temporary_token(text: str) -> tuple[str, bool]:
    """Remove standalone ``!temporary`` token from *text*.

    Returns ``(cleaned_text, found)`` where *found* is True if the token
    was present.  The cleaned text has the token removed and excess
    whitespace collapsed.
    """
    return privacy_mode.strip_token(text, privacy_mode.MODE_TEMPORARY)


async def _apply_privacy_mode(
    mode: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> None:
    """Mark a session as *mode* and notify the user (idempotent).

    Everything platform-shaped is a callback into this module, which is what lets
    the shared applier own the ordering (mark before any await, then the durable
    flag, then the audit, then the notice).
    """

    async def _notify(message: str) -> None:
        await slack.post_message(channel, message, reply_ts)

    async def _on_applied(_mode: str) -> None:
        # Register thread so follow-up messages pass the in_active_thread
        # gate in mention/observe channels without needing another @mention.
        # reply_ts is the bare Slack thread_ts; session_key may be namespaced.
        sessions.set_slack_link(session_key, reply_ts, channel)

    await privacy_mode.apply_mode(
        mode,
        session_key,
        source="slack",
        caller=user_id,
        resources=f"{channel}:{session_key}",
        sessions=sessions,
        notify=_notify,
        on_applied=_on_applied,
    )


async def _apply_temporary_modifier(
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> None:
    """Mark a session as temporary and notify the user (idempotent)."""
    await _apply_privacy_mode(
        privacy_mode.MODE_TEMPORARY, session_key, user_id, channel, slack, sessions, reply_ts
    )


async def _apply_incognito_modifier(
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> None:
    """Mark a session as incognito and notify the user (idempotent)."""
    await _apply_privacy_mode(
        privacy_mode.MODE_INCOGNITO, session_key, user_id, channel, slack, sessions, reply_ts
    )


async def maybe_apply_privacy_modifiers(
    text: str,
    cmd_text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    reply_ts: str,
) -> tuple[str, str, bool]:
    """Strip and apply the ``!temporary`` / ``!incognito`` privacy modifiers.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so the privacy controls behave identically
    on both (and the modifier token never leaks into the LLM prompt).

    Returns ``(text, cmd_text, only_modifier)``:
    - *text* — the LLM-facing message with the modifier token(s) removed.
    - *cmd_text* — the mention-stripped command text with the token removed
      (the native path reuses it for its subsequent ``!compact``/``!bang``
      checks; the transport path ignores it).
    - *only_modifier* — True when the message was nothing but the modifier(s);
      the caller MUST then return without starting an LLM turn.

    Slack's TWO texts are why this drives ``privacy_mode``'s primitives rather
    than its single-text ``strip_and_apply``: only *cmd_text* decides whether the
    message was nothing BUT a modifier, while *text* is what reaches the model.
    Ordering (temporary, then incognito) and the early return as soon as nothing
    remains match the shipped behaviour.
    """
    for mode, pattern in (
        (privacy_mode.MODE_TEMPORARY, _TEMPORARY_TOKEN_RE),
        (privacy_mode.MODE_INCOGNITO, _INCOGNITO_TOKEN_RE),
    ):
        cmd_stripped, had_mode = privacy_mode.strip_token(cmd_text, mode)
        if not had_mode:
            continue
        await _apply_privacy_mode(mode, session_key, user_id, channel, slack, sessions, reply_ts)
        cmd_text = cmd_stripped
        text = pattern.sub("", text)
        text = " ".join(text.split()) or text  # collapse whitespace
        if not cmd_text:
            # Message was *only* the modifier(s), with no remaining content.
            return text, cmd_text, True

    return text, cmd_text, False


# Auto-titling lives in ``messaging.auto_title`` so both Slack paths and a second
# channel share ONE claim tracker: two turns that resolved to the same session key
# cannot then title it twice. The names below are the Slack-facing spellings this
# package's call sites already use; ``_titled_threads`` is an ALIAS of the shared
# tracker, not a copy.
_titled_threads = auto_title._titled
_mark_titled = auto_title.mark_titled


# Background tasks kept alive to prevent GC mid-execution.
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


def cancel_background_tasks() -> None:
    """Cancel pending background tasks during gateway shutdown."""
    for t in _background_tasks:
        t.cancel()
    _background_tasks.clear()


def track_background_task(task: "asyncio.Task[Any]") -> None:
    """Hold a strong reference to *task* until it finishes.

    Both halves matter: without the reference the loop may collect a running task
    mid-flight, and without the registration :func:`cancel_background_tasks`
    cannot stop it at shutdown. The transport dispatcher shares this set so a
    fire-and-forget turn it starts is torn down with the gateway too.
    """
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Review mode: stores draft text keyed by "channel|thread_ts|uuid" for button/modal
# handlers. Each entry includes the *requester* user_id so handlers can authorize the
# requester (in addition to bot owner) to act on their own drafts.
# Bounded with TTL to prevent memory leaks from abandoned drafts.
_REVIEW_PLACEHOLDER_TS = "review_placeholder"
_REVIEW_DRAFT_TTL = 3600  # 1 hour
_REVIEW_DRAFT_MAX = 1024
# key → (draft, requester_user_id, timestamp)
_review_drafts: dict[str, tuple[str, str, float]] = {}


def _review_drafts_get(key: str) -> tuple[str, str]:
    """Get (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.get(key)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        _review_drafts.pop(key, None)
        return "", ""
    return draft, requester


def _review_drafts_set(key: str, draft: str, requester_user_id: str) -> None:
    """Store a draft with TTL + requester id, evicting oldest if at capacity."""
    now = time.monotonic()
    # Evict expired entries
    expired = [k for k, (_, _, ts) in _review_drafts.items() if now - ts > _REVIEW_DRAFT_TTL]
    for k in expired:
        _review_drafts.pop(k, None)
    # Evict oldest if still at capacity
    if len(_review_drafts) >= _REVIEW_DRAFT_MAX:
        oldest_key = min(_review_drafts, key=lambda k: _review_drafts[k][2])
        _review_drafts.pop(oldest_key, None)
    _review_drafts[key] = (draft, requester_user_id, now)


def _review_drafts_pop(key: str) -> tuple[str, str]:
    """Pop (draft, requester_user_id), returning ("","") if missing or expired."""
    entry = _review_drafts.pop(key, None)
    if entry is None:
        return "", ""
    draft, requester, ts = entry
    if time.monotonic() - ts > _REVIEW_DRAFT_TTL:
        return "", ""
    return draft, requester


def _get_default_agent() -> str:
    """Read persisted default agent, cached to avoid disk I/O on every message."""
    global _cached_default_agent
    if _cached_default_agent is None:
        _cached_default_agent = KiroCrewConfig.load().agent.default_agent
    return _cached_default_agent


def _hydrate_thread_overrides(session_key: str, conversation_log: ConversationLog | None) -> None:
    """Populate in-memory caches from conversation log metadata if not already set."""
    if session_key in _hydrated_sessions:
        return
    _hydrated_sessions.add(session_key)
    if not conversation_log:
        return
    try:
        meta = conversation_log.get_metadata(session_key)
    except Exception:
        logger.debug("Failed to hydrate thread overrides for %s", session_key, exc_info=True)
        return
    if meta.get("agent"):
        _thread_agents[session_key] = meta["agent"]
    if meta.get("project"):
        # Defense-in-depth: re-validate the persisted path at this input
        # boundary. Conversation-log metadata is normally written through the
        # guarded !project handler, but if it is ever corrupted or tampered
        # with, a sensitive credential path (~/.aws, ~/.ssh, …) must never be
        # loaded into the in-memory cache.
        if not is_sensitive_path(meta["project"]):
            _thread_projects[session_key] = meta["project"]
        else:
            logger.warning(
                "Ignoring sensitive project path from thread metadata for %s",
                session_key,
            )


def _get_agent_for_session(session_key: str) -> str:
    """Return agent for a session: thread override first, then global default."""
    return _thread_agents.get(session_key) or _get_default_agent()


def _discover_project_agents(project_dir: str | None) -> list[Path]:
    """Return agent JSON files from <project_dir>/.kiro/ and .kiro/agents/.

    Delegates to :func:`agent_discovery.project_agent_files`, the one implementation
    now shared with the dashboard picker, ``spawn_run`` validation and per-turn agent
    resolution. ``include_legacy=True`` is passed HERE and only here: Slack's
    ``*.agent-spec.json`` convention predates ``.kiro/agents/`` and is kept for
    continuity, but kiro-cli cannot activate such a name, so no dispatch surface may
    offer it.
    """
    return project_agent_files(project_dir, include_legacy=True)


def _resolve_agent_name(name: str, project_dir: str | None = None) -> str | None:
    """Resolve an agent name to its internal name via suffix matching.

    Searches project-local .kiro/ first (if project_dir set), then ~/.kiro/agents/.
    Returns the resolved name, or None if not found.
    """
    # Project-local agents take priority — kiro-cli resolves --agent against its
    # cwd before the user-level dir, so a project agent is the one that would run.
    # Prefilter on the FILENAME first: this runs on the event loop, and reading
    # every spec to compare its declared name stalls Slack and the gateway on a
    # checkout with many agents or slow storage. At most the one matching file is
    # read, to return the name it declares.
    for spec in _discover_project_agents(project_dir):
        stem = spec.stem.removesuffix(".agent-spec")
        if stem != name and spec.stem != name:
            continue
        return project_agent_name(spec)

    agents_dir = kiro_agents_dir()
    jsons = (
        sorted(agents_dir.glob("*.json"), key=lambda f: (len(f.stem), f.stem))
        if agents_dir.is_dir()
        else []
    )
    match = next(
        (f for f in jsons if f.stem == name or f.stem.endswith(f"-{name}")),
        None,
    )
    if not match:
        # Fallback: search companion-backend cc-plugins agents
        cc_match = _resolve_cc_agent_name(name)
        return cc_match
    safe = validate_file_path(str(match))
    if not safe:
        return None
    try:
        return json.loads(Path(safe).read_text(encoding="utf-8")).get("name", match.stem)
    except (json.JSONDecodeError, OSError):
        return match.stem


# Frontmatter ``name:`` matcher for cc-plugins agent specs. Pre-compiled at
# module level rather than per-iteration inside the agent-file walk below.
_CC_AGENT_NAME_RE = re.compile(r'^name:\s*["\']?([^"\'\n]+)', re.MULTILINE)


def _iter_cc_agent_names(cc_plugins_dir: Path | None = None) -> Iterator[str]:
    """Yield the ``name:`` from each ``~/.aim/cc-plugins/*/agents/*.md`` agent.

    Single source of truth for walking the cc-plugins agent set: reads each
    Markdown file, parses its YAML ``---`` frontmatter, and yields the declared
    agent name (quotes/whitespace stripped). Files that are unreadable, lack
    frontmatter, or omit ``name:`` are skipped. Iterated in sorted path order
    for deterministic output.
    """
    cc_dir = cc_plugins_dir or (Path.home() / ".aim" / "cc-plugins")
    if not cc_dir.is_dir():
        return
    for md_file in sorted(cc_dir.glob("*/agents/*.md")):
        try:
            raw = safe_read_file_bytes(str(md_file))
            if raw is None:
                continue
            content = raw.decode("utf-8")
            if not content.startswith("---"):
                continue
            frontmatter = content[3 : content.index("---", 3)]
            name_match = _CC_AGENT_NAME_RE.search(frontmatter)
            if not name_match:
                continue
            agent_name = name_match.group(1).strip().strip("\"'")
            if agent_name:
                yield agent_name
        except Exception:
            continue


def _resolve_cc_agent_name(name: str, cc_plugins_dir: Path | None = None) -> str | None:
    """Return *name* if a cc-plugins agent declares it, else None."""
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name == name:
            return agent_name
    return None


def _list_all_agent_names(cc_plugins_dir: Path | None = None) -> str:
    """Return a comma-separated list of all available agent names.

    Merges ``~/.kiro/agents/*.json`` (by stem) with the cc-plugins agents from
    :func:`_iter_cc_agent_names`. The internal ``kirocrew-lite`` variant is
    hidden. Returns ``"(none found)"`` when empty.

    Note: this listing is unioned across both agent sources, but *activation*
    is not. cc-plugins (companion-backend) agents only actually load when
    ``agent.provider=claude_code``; under the kiro-cli provider a ``!ta`` to a
    cc-plugins name resolves and is recorded, but the next kiro session looks
    for ``~/.kiro/agents/<name>.json`` and falls back if it is absent. Switch
    the provider to ``claude_code`` to run cc-plugins agents.
    """
    names: list[str] = []
    agents_dir = kiro_agents_dir()
    if agents_dir.is_dir():
        # Hide the internal kirocrew-lite variant from BOTH sources — a
        # ~/.kiro/agents/kirocrew-lite.json would otherwise leak into the list.
        names.extend(f.stem for f in sorted(agents_dir.glob("*.json")) if f.stem != "kirocrew-lite")
    seen = set(names)
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name not in seen and agent_name != "kirocrew-lite":
            names.append(agent_name)
            seen.add(agent_name)
    return ", ".join(names) if names else "(none found)"


def _set_default_agent(name: str) -> None:
    """Persist default agent to config (shared with dashboard)."""
    global _cached_default_agent
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")

    def _apply(data: dict) -> dict:
        data.setdefault("agent", {})["default_agent"] = name
        return data

    try:
        # Locked read-modify-write: holds the sidecar advisory lock so a
        # concurrent config writer (dashboard PATCH, CLI, the boot-time meta
        # refresh) cannot land between this read and write and get reverted.
        update_config_locked(path, mutate=_apply)
    except ConfigReadError as e:
        # Fail closed: writing back a {} baseline would drop every other setting.
        raise ValueError(f"Failed to read config: {e}") from e
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e
    _cached_default_agent = name


def _persist_channel_config(
    channel_id: str,
    activation: str | None = None,
    agent: str | None = None,
) -> None:
    """Update a single channel's config in config.json (merge, not overwrite)."""
    path = config_path()
    if is_sensitive_path(str(path)):
        raise ValueError(f"Refusing to write to sensitive path: {path}")

    def _apply(data: dict) -> dict:
        slack_data = data.setdefault("slack", {})
        channels = slack_data.setdefault("channels", {})
        ch = channels.setdefault(channel_id, {})
        if activation is not None:
            ch["activation"] = activation
        if agent is not None:
            ch["agent"] = agent
        return data

    try:
        # Locked read-modify-write (see _set_default_agent): without the
        # sidecar lock, a `!channel always` racing any other config writer
        # could be silently reverted by the loser's stale snapshot.
        update_config_locked(path, mutate=_apply)
    except ConfigReadError as e:
        # Fail closed: writing back a {} baseline would drop every other setting.
        raise ValueError(f"Failed to read config: {e}") from e
    except OSError as e:
        raise ValueError(f"Failed to write config: {e}") from e


class _PendingApproval:
    __slots__ = ("provider", "request_id", "session_key", "future")

    def __init__(self, provider: LLMProvider, request_id: str | int, session_key: str = "") -> None:
        self.provider = provider
        self.request_id = request_id
        self.session_key = session_key
        self.future: asyncio.Future[str] = asyncio.get_running_loop().create_future()


class _LinkedApproval:
    """A tool-approval prompt posted to Slack on behalf of a *linked dashboard
    slot*.

    Unlike :class:`_PendingApproval`, this entry does NOT own the ACP backend
    answer. For a Slack-linked dashboard session the consumer that actually
    calls ``approve_tool`` / ``reject_tool`` is the dashboard's ``_run_chat``
    loop, which is parked on the slot's approval *future*. A Slack button click
    here must therefore ONLY resolve that future (via
    ``state.resolve_approval``); the dashboard loop then answers the backend
    exactly once. Calling ``approve_tool`` from here too would answer the
    JSON-RPC request twice.
    """

    __slots__ = ("request_id", "session_key")

    def __init__(self, request_id: str | int, session_key: str) -> None:
        self.request_id = request_id
        self.session_key = session_key


# Linked-slot approvals: keyed by f"{channel}:{approval_msg_ts}", parallel to
# _pending_approvals. Kept separate so the click handler can tell a Slack-native
# approval (answer the backend) from a dashboard-linked one (resolve the slot
# future only).
_linked_approvals: dict[str, _LinkedApproval] = {}


_OUTCOME_APPROVED = "approved"
_OUTCOME_REJECTED = "rejected"

# Block Kit action IDs
_ACTION_APPROVE = "approve_tool"
_ACTION_TRUST = "trust_tool"
_ACTION_REJECT = "reject_tool"


def set_allowed_users(user_ids: set[str]) -> None:
    """Set the allowed user IDs for Slack access (called by gateway)."""
    global _allowed_users
    _allowed_users = user_ids


def set_owner_id(owner_id: str) -> None:
    """Set the primary owner ID for owner-only commands (called by gateway)."""
    global _owner_id
    _owner_id = owner_id


def set_yolo_mode(enabled: bool) -> None:
    """Set YOLO mode at startup from config (called by gateway).

    ``dangerouslySkipPermissions`` is a standing instruction, so the grant does not
    expire — see ``safety_override.grant_declared_yolo``. A headless
    ``--slack-only`` gateway never runs the dashboard startup path, so the same
    helper is called here or YOLO would still lapse for exactly the users
    driving the agent from another channel.
    """
    apply_config_duration()
    if enabled:
        grant_declared_yolo()


def set_orch_cfg(cfg: KiroCrewConfig) -> None:
    """Store a live reference to the orchestrator's config (called by events.py)."""
    global _orch_cfg
    _orch_cfg = cfg
    load_voice_reply_config(cfg)


def load_voice_reply_config(cfg: "KiroCrewConfig | None" = None) -> None:
    """Populate the live voice state (``_vc``) from config's ``voice_reply``.

    Callable without a Slack orchestrator: ``set_orch_cfg`` runs only on the
    Slack startup path, so the dashboard app builders call this directly at
    boot. Without that call a dashboard-only gateway (no Slack tokens) never
    restores persisted voice settings — every restart silently resets TTS to
    disabled while the dashboard's settings PUT keeps reporting success.
    """
    # Load voice_reply defaults from config
    _vr: dict = cfg.raw.get("voice_reply", {}) if (cfg is not None and hasattr(cfg, "raw")) else {}
    if not _vr:
        try:
            with open(config_path()) as f:
                _vr = json.load(f).get("voice_reply", {})
        except Exception:
            _vr = {}
    _enabled = bool(_vr.get("enabled", False))
    if _enabled:
        _vc.global_enabled = True
    _vc.auto_speak = bool(_vr.get("auto_speak", False))
    _vc.default_voice = _vr.get("voice_id", "Ruth")
    _vc.default_engine = _vr.get("engine", "generative")
    _vc.default_rate = _vr.get("rate", "100%")
    _vc.default_pitch = _vr.get("pitch", "+0%")
    _vc.aws_profile = _vr.get("aws_profile", "")
    _vc.region = _vr.get("region", "")
    # ``auto_reply_to_voice`` defaults to ``enabled``'s value: users with
    # explicit ``enabled=false`` keep the existing zero-voice behavior, and
    # users who turn voice on globally also get symmetric voice-in/voice-out
    # without needing to set a second flag.
    _vc.auto_reply_to_voice = bool(_vr.get("auto_reply_to_voice", _enabled))
    # Validate provider on load — a typo (e.g. "ploly") would otherwise pass
    # through and only fail at synthesis time, after the user has already sent
    # a voice memo expecting a voice reply.
    #
    # Both the absent-key default and the invalid-value fallback resolve to the
    # LOCAL provider. They previously resolved to "polly", so a config that
    # enabled voice reply without naming a provider — or that named one with a
    # typo — reached a paid AWS service with no operator decision behind it.
    # Falling back to local is also the safer half of the pair: a wrong local
    # provider costs nothing and degrades to a "TTS isn't configured" notice.
    _provider = _vr.get("provider", DEFAULT_PROVIDER)
    if _provider not in VALID_PROVIDERS:
        logger.warning(
            "voice_reply.provider %r not in %s, defaulting to %r",
            _provider,
            sorted(VALID_PROVIDERS),
            DEFAULT_PROVIDER,
        )
        _provider = DEFAULT_PROVIDER
    _vc.provider = _provider
    _vc.piper_binary = _vr.get("piper_binary", "")
    _vc.piper_model = _vr.get("piper_model", "")
    _vc.piper_model_config = _vr.get("piper_model_config", "")
    # Coerce to finite/positive — a config.json with inf/NaN (JSON accepts both)
    # would otherwise reach synthesis and be re-serialized as non-RFC JSON,
    # breaking the dashboard's config GET.
    _vc.piper_length_scale = _validate_length_scale(_vr.get("piper_length_scale", 1.0))


def set_dashboard_state(state: object) -> None:
    """Store dashboard state reference for push_refresh (called by gateway)."""
    global _dashboard_state
    _dashboard_state = state


def get_dashboard_state() -> object | None:
    """The live dashboard state, or None when running without a dashboard.

    An accessor rather than a direct read of the global: the gateway installs
    the state AFTER import, so a caller that imported the name would capture
    None forever.
    """
    return _dashboard_state


def get_orch_cfg() -> "KiroCrewConfig | None":
    """The orchestrator's live config, or None before the gateway installs it.

    Same reason as :func:`get_dashboard_state` -- the value is set post-import.
    """
    return _orch_cfg


def _reload_orch_cfg() -> None:
    """Reload in-memory config after !channel writes so changes take effect immediately."""
    if _orch_cfg is not None:
        fresh = KiroCrewConfig.load()
        _orch_cfg.slack_channels = fresh.slack_channels
        _orch_cfg.slack_dm_activation = fresh.slack_dm_activation


def is_owner(user_id: str) -> bool:
    """Check if *user_id* is the primary owner (with W/U prefix cross-match)."""
    if not _owner_id or not user_id:
        return False
    if user_id == _owner_id:
        return True
    return user_id.replace("W", "U", 1) == _owner_id or user_id.replace("U", "W", 1) == _owner_id


def disable_yolo() -> None:
    """Disable YOLO mode (global auto-approve)."""
    if not safety_override().is_active():
        return
    safety_override().deactivate("slack")
    # Through the shared revoke, which undoes BOTH halves of each grant. Dropping
    # only the in-memory mapping leaves every granted session's approval_policy at
    # "auto", and a subagent reads that policy rather than the mapping, so a later
    # spawn would inherit a trust this call just revoked.
    clear_trusted_sessions()
    logger.info("YOLO mode OFF")


def enable_yolo_with_ttl(ttl_secs: int) -> None:
    """Enable YOLO mode with a specific TTL."""
    safety_override().activate("slack", ttl=ttl_secs)
    logger.info("YOLO mode ON (expires in %ds)", ttl_secs)


def is_yolo_mode() -> bool:
    """Return whether YOLO mode is currently active."""
    return safety_override().is_active()


def is_slack_session_trusted(session_key: str) -> bool:
    """Return whether *session_key* has been granted per-session Trust.

    Per-session trust auto-approves all subsequent tools for THIS session only
    (distinct from global YOLO). Populated by the Trust button on both the
    native and messaging-transport approval prompts.
    """
    return is_session_trusted(session_key)


def add_trusted_session(session_key: str, sessions: "SessionManager | None" = None) -> None:
    """Grant per-session Trust for *session_key* (mirrors native trust_tool).

    Adds the session to the in-memory trust set and, when a SessionManager is
    supplied, sets its approval policy to ``auto`` so spawned subagents inherit
    the trust (they read the parent's approval policy, not the in-memory set).
    """
    _add_trusted_session(session_key, sessions)


def is_allowed_user(user_id: str) -> bool:
    """Check if user_id is the owner.

    Multi-user access is disabled for security — only the owner
    (KIROCREW_OWNER_ID) is authorized to interact via Slack.
    """
    if not user_id:
        return False
    return is_owner(user_id)


def set_tracking_channels(channel_ids: set[str]) -> None:
    """Set the tracked channel IDs (called by gateway/interactions)."""
    global _tracking_channels
    _tracking_channels = channel_ids


def set_open_channels(channel_ids: set[str]) -> None:
    """Set channel IDs where all users are authorized (no allowlist needed)."""
    global _open_channels
    _open_channels = channel_ids


def is_open_channel(channel_id: str) -> bool:
    """Open channels are disabled — multi-user access is blocked for security."""
    return False


def is_tracked_channel(channel_id: str) -> bool:
    """Check if *channel_id* is in the tracking set."""
    return bool(channel_id and channel_id in _tracking_channels)


@dataclass
class MessageContext:
    """Service references needed to process a Slack message.

    Groups the 8 service/config parameters that ``handle_message`` needs.
    """

    sessions: SessionManager
    approval_mode: str = APPROVAL_AUTO
    context_builder: ContextBuilder | None = None
    cron_service: CronService | None = None
    conversation_log: ConversationLog | None = None
    consolidator: HistoryConsolidator | None = None
    subagent_manager: SubagentManager | None = None
    task_runner: TaskRunner | None = None


async def _safe_voice_reply(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    text: str,
    voice_id: str = "Ruth",
    engine: str = "generative",
    rate: str = "100%",
    pitch: str = "+0%",
) -> None:
    """Fire-and-forget voice reply.  Never raises."""
    try:
        await _voice_reply_fn(
            slack,
            channel,
            thread_ts,
            text,
            provider=_vc.provider,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
            piper_binary=_vc.piper_binary,
            piper_model=_vc.piper_model,
            piper_model_config=_vc.piper_model_config,
            length_scale=_vc.piper_length_scale,
        )
    except Exception:
        logger.debug("Voice reply failed", exc_info=True)


async def _handle_slash_command(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
) -> str | None:
    """Dispatch owner-only ``!commands``.  Returns a string (even empty) if handled, None if not."""

    cmd = cmd_text.split()[0].lower()

    # ── Deprecation warning for all bang commands ──
    slash_equiv = _BANG_TO_SLASH.get(cmd)
    if slash_equiv:
        logger.warning("Deprecated bang command %s used — suggest %s", cmd, slash_equiv)
        warn_block = deprecation_warning_block(cmd, slash_equiv)
        await slack.post_blocks(channel, [warn_block], f"{cmd} is deprecated", reply_ts)

    # ── !yolo on / !yolo off / !yolo renew ──
    if cmd == "!yolo":
        parts = cmd_text.split()
        yolo_active = is_yolo_mode()
        if len(parts) >= 2 and parts[1].lower() == "off":
            if yolo_active:
                disable_yolo()
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_off",
                )
                await slack.post_message(channel, "🔒 YOLO mode disabled.", reply_ts)
            else:
                await slack.post_message(channel, "YOLO mode is already off.", reply_ts)
        elif len(parts) >= 2 and parts[1].lower() == "on":
            if not yolo_active:
                _result = safety_override().activate("slack")
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_on",
                )
                await slack.post_message(
                    channel,
                    f"🔓 YOLO mode enabled ({describe_new_grant(_result.ttl)}).",
                    reply_ts,
                )
            else:
                await slack.post_message(
                    channel, f"YOLO mode is already on ({describe_grant_lifetime()}).", reply_ts
                )
        elif len(parts) >= 2 and parts[1].lower() == "renew":
            # renew() audits fail-closed with a synchronous SEL write; keep
            # that filesystem I/O off the event loop.
            result = await asyncio.to_thread(safety_override().renew, "slack")
            if result.renewed:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="renewed",
                    source="slack",
                    resources="yolo_renew",
                )
                await slack.post_message(
                    channel,
                    f"🔓 YOLO mode renewed (auto-expires in {result.ttl // 60}min).",
                    reply_ts,
                )
            else:
                await slack.post_message(
                    channel, "YOLO mode is not active. Use `!yolo on` to activate.", reply_ts
                )
        else:
            if yolo_active:
                status = f"ON 🔓 ({describe_grant_lifetime()})"
            else:
                status = "OFF 🔒"
            await slack.post_message(
                channel,
                f"YOLO mode: *{status}*. Use `!yolo on` / `!yolo off` / `!yolo renew`.",
                reply_ts,
            )
        return ""

    # ── !stop — defensive fallback (normally intercepted in events.py
    #    _route_message before handle_message is called) ──
    if cmd == "!stop":
        has_session = sessions.has_session(session_key)
        if not has_session:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!stop",
                tool_kind="command",
                outcome="no_session",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, "Nothing running.", reply_ts)
            return ""

        # Post ephemeral "Stopping…" block with Kill Now button
        from kiro_crew.slack.blocks import build_stopping_blocks

        await slack.post_ephemeral(
            channel,
            user_id,
            "Stopping…",
            blocks=build_stopping_blocks(session_key),
            thread_ts=reply_ts,
        )

        async def _on_soft() -> None:
            await slack.post_message(channel, "⏹ Execution stopped.", reply_ts)

        async def _on_hard() -> None:
            await slack.post_message(channel, "⛔ Execution stopped — session reset.", reply_ts)

        outcome = await sessions.stop_turn(session_key, on_soft=_on_soft, on_hard=_on_hard)
        # If stop_turn returned "idle" (no active turn), neither callback
        # fired — dismiss the stale "Stopping…" ephemeral explicitly.
        if outcome == "idle":
            await slack.post_message(channel, "Nothing running.", reply_ts)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!stop",
            tool_kind="command",
            outcome=outcome,
            metadata={"user": user_id, "channel": channel},
        )
        return ""

    # ── !voice on/off/global/<name> | engine/speed/pitch controls ──
    if cmd == "!voice":
        from kiro_crew.voice_reply import VALID_ENGINES, _validate_pitch, _validate_rate

        parts = cmd_text.split()
        arg = parts[1].lower() if len(parts) >= 2 else ""
        val = parts[2] if len(parts) >= 3 else ""
        if arg == "on":
            _vc.sessions.add(session_key)
            v = _vc.voices.get(session_key, _vc.default_voice)
            e = _vc.engines.get(session_key, _vc.default_engine)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_on",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, f"\U0001f50a Voice ON — *{v}* ({e})", reply_ts)
        elif arg == "off":
            _vc.sessions.discard(session_key)
            for d in (_vc.voices, _vc.engines, _vc.rates, _vc.pitches):
                d.pop(session_key, None)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_off",
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, "\U0001f507 Voice OFF.", reply_ts)
        elif arg == "global":
            _vc.global_enabled = not _vc.global_enabled
            state = "ON \U0001f50a" if _vc.global_enabled else "OFF \U0001f507"
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!voice",
                tool_kind="command",
                outcome="voice_global_" + ("on" if _vc.global_enabled else "off"),
                metadata={"user": user_id, "channel": channel},
            )
            await slack.post_message(channel, f"Voice global: *{state}*", reply_ts)
        elif arg == "engine" and val:
            eng = val.lower()
            if eng not in VALID_ENGINES:
                await slack.post_message(
                    channel,
                    f"\u274c Invalid engine. Use: {', '.join(sorted(VALID_ENGINES))}",
                    reply_ts,
                )
            else:
                _vc.engines[session_key] = eng
                _vc.sessions.add(session_key)
                await slack.post_message(channel, f"\U0001f50a Engine set to *{eng}*.", reply_ts)
        elif arg == "speed" and val:
            validated = _validate_rate(val)
            _vc.rates[session_key] = validated
            _vc.sessions.add(session_key)
            await slack.post_message(channel, f"\U0001f50a Speed set to *{validated}*.", reply_ts)
        elif arg == "pitch" and val:
            validated = _validate_pitch(val)
            _vc.pitches[session_key] = validated
            _vc.sessions.add(session_key)
            await slack.post_message(channel, f"\U0001f50a Pitch set to *{validated}*.", reply_ts)
        elif arg and arg not in ("engine", "speed", "pitch"):
            voice_name = parts[1]  # preserve original case
            _vc.sessions.add(session_key)
            _vc.voices[session_key] = voice_name
            await slack.post_message(channel, f"\U0001f50a Voice set to *{voice_name}*.", reply_ts)
        else:
            on = session_key in _vc.sessions or _vc.global_enabled
            v = _vc.voices.get(session_key, _vc.default_voice)
            e = _vc.engines.get(session_key, _vc.default_engine)
            r = _vc.rates.get(session_key, _vc.default_rate)
            p = _vc.pitches.get(session_key, _vc.default_pitch)
            await slack.post_message(
                channel,
                f"\U0001f50a Voice: *{'ON' if on else 'OFF'}*\n"
                f"\u2022 Voice: *{v}* | Engine: *{e}*\n"
                f"\u2022 Speed: *{r}* | Pitch: *{p}*\n"
                "`!voice <name>` `!voice engine <neural|generative|long-form>` "
                "`!voice speed <80%>` `!voice pitch <+10%>`",
                reply_ts,
            )
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !agent <name> / !agent off — always global ──
    if cmd == "!agent":
        parts = cmd_text.split()
        if len(parts) == 1:
            name = _get_default_agent() or "kirocrew"
            await slack.post_message(
                channel,
                f"Current agent: *{name}*. Usage: `!agent <name>` or `!agent off`",
                reply_ts,
            )
            return ""
        if len(parts) != 2:
            await slack.post_message(channel, "Usage: `!agent <name>` or `!agent off`", reply_ts)
            return ""
        agent_name = parts[1]
        if agent_name.lower() in ("default", "off"):
            try:
                await run_config_write(_set_default_agent, "")
            except ValueError as e:
                await slack.post_message(channel, f"❌ {e}", reply_ts)
                return ""
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!agent",
                tool_kind="command",
                outcome="agent_reset",
                metadata={"user": user_id, "channel": channel},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "🔄 Reset to default agent.", reply_ts)
            await _add_phase_reaction(slack, channel, msg_ts, "done")
            return ""
        resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
        if not resolved:
            names = _list_all_agent_names()
            await slack.post_message(
                channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
            )
            return ""
        try:
            await run_config_write(_set_default_agent, resolved)
        except ValueError as e:
            await slack.post_message(channel, f"❌ {e}", reply_ts)
            return ""
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!agent",
            tool_kind="command",
            outcome="agent_switch",
            metadata={"agent": resolved, "user": user_id, "channel": channel},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, f"🔄 Switched to agent: *{resolved}*", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !dashboard [duration] ──
    if cmd == "!dashboard":
        from kiro_crew.dashboard.token_auth import parse_duration
        from kiro_crew.slack.allowlist import send_dashboard_link

        parts = cmd_text.split()
        ttl = 3600
        if len(parts) >= 2:
            parsed = parse_duration(parts[1])
            if parsed is None:
                await slack.post_message(
                    channel,
                    "Usage: `!dashboard [<N>h|<N>m]` — e.g. `!dashboard 2h`, `!dashboard 30m`",
                    reply_ts,
                )
                return ""
            ttl = parsed

        url = await send_dashboard_link(slack, user_id, ttl)
        if url:
            await slack.post_message(channel, "🔗 Dashboard link sent via DM.", reply_ts)
        else:
            await slack.post_message(channel, "❌ Failed to send dashboard link.", reply_ts)
        return ""

    # ── !link-to-dashboard -- import Slack thread into dashboard ──
    if cmd == "!link-to-dashboard":
        if not is_allowed_user(user_id):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="denied",
                metadata={"user_id": user_id, "channel": channel, "reason": "not_allowed_user"},
            )
            await slack.post_message(channel, "Not authorized.", reply_ts)
            return ""
        if not _dashboard_state or not hasattr(_dashboard_state, "get_or_create_slot"):
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"user_id": user_id, "channel": channel, "reason": "no_dashboard"},
            )
            await slack.post_message(channel, "Dashboard not available.", reply_ts)
            return ""
        if reply_ts == msg_ts:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"user_id": user_id, "channel": channel, "reason": "not_in_thread"},
            )
            await slack.post_message(
                channel, "Use this command inside a thread to import it.", reply_ts
            )
            return ""
        # Fetch thread history and import to dashboard
        from kiro_crew.slack.interactions import _import_thread_to_slot

        slot = await _import_thread_to_slot(slack, _dashboard_state, channel, reply_ts)
        if not slot:
            sel().log_tool_invocation(
                session_key="",
                agent="kirocrew",
                source="slack",
                tool_name="link_to_dashboard",
                tool_kind="command",
                outcome="failure",
                metadata={"channel": channel, "thread_ts": reply_ts, "reason": "empty_thread"},
            )
            await slack.post_message(channel, "Could not fetch thread history.", reply_ts)
            return ""
        sel().log_tool_invocation(
            session_key=slot.key,
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="success",
            metadata={
                "slot": slot.key,
                "channel": channel,
                "thread_ts": reply_ts,
                "msg_count": len(slot.messages),
            },
        )
        await slack.post_message(
            channel,
            f"Imported {len(slot.messages)} messages to dashboard session *{slot.key}*. Thread is now linked.",
            reply_ts,
        )
        return ""

    # ── !ta <name> / !ta off — thread-scoped agent ──
    if cmd == "!ta":
        parts = cmd_text.split()
        if len(parts) < 2:
            current = _thread_agents.get(session_key, "")
            if current:
                await slack.post_message(
                    channel,
                    f"Thread agent: *{current}*. `!ta off` to reset.",
                    reply_ts,
                )
            else:
                await slack.post_message(
                    channel,
                    "No thread agent set. Usage: `!ta <name>` or `!ta off`",
                    reply_ts,
                )
            return ""
        agent_name = parts[1]
        if agent_name.lower() in ("default", "off"):
            _thread_agents.pop(session_key, None)
            if conversation_log:
                try:
                    await asyncio.to_thread(
                        conversation_log.update_metadata, session_key, {"agent": ""}
                    )
                except Exception:
                    logger.debug("Failed to clear agent in conversation log", exc_info=True)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!ta",
                tool_kind="command",
                outcome="agent_reset",
                metadata={"user": user_id, "channel": channel, "scope": "thread"},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "🔄 Thread agent reset.", reply_ts)
            await _add_phase_reaction(slack, channel, msg_ts, "done")
            return ""
        resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
        if not resolved:
            names = _list_all_agent_names()
            await slack.post_message(
                channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
            )
            return ""
        _thread_agents[session_key] = resolved
        if conversation_log:
            try:
                await asyncio.to_thread(
                    conversation_log.update_metadata, session_key, {"agent": resolved}
                )
            except Exception:
                logger.debug("Failed to persist agent to conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!ta",
            tool_kind="command",
            outcome="agent_switch",
            metadata={"agent": resolved, "user": user_id, "channel": channel, "scope": "thread"},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, f"🔄 Thread agent: *{resolved}*", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""

    # ── !project <path> / !project off — thread-scoped agent-discovery dir ──
    # NOTE: this only scopes which project-local .kiro agents are discoverable
    # for !ta in this thread; it does NOT change the agent's working directory
    # (cwd). Provider cwd plumbing is out of scope for this CR.
    if cmd == "!project":
        parts = cmd_text.split(maxsplit=1)
        if len(parts) < 2:
            current = _thread_projects.get(session_key, "")
            msg = (
                f"Thread agent-discovery project: `{current}`"
                if current
                else "No project set. Usage: `!project <path>` or `!project off`\n"
                "Scopes which project-local `.kiro` agents `!ta` can find — "
                "does not change the working directory."
            )
            await slack.post_message(channel, msg, reply_ts)
            return ""
        raw_path = parts[1].strip()
        if raw_path.lower() in ("off", "clear", "reset"):
            _thread_projects.pop(session_key, None)
            if conversation_log:
                try:
                    await asyncio.to_thread(
                        conversation_log.update_metadata, session_key, {"project": ""}
                    )
                except Exception:
                    logger.debug("Failed to clear project in conversation log", exc_info=True)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_cleared",
                metadata={"user": user_id, "channel": channel},
            )
            await sessions.remove(session_key)
            await slack.post_message(channel, "Thread project cleared.", reply_ts)
            return ""
        resolved = os.path.realpath(os.path.expanduser(raw_path))
        if is_sensitive_path(resolved):
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_denied_sensitive",
                metadata={"user": user_id, "channel": channel, "project": resolved},
            )
            await slack.post_message(
                channel, "Cannot use sensitive path as project directory.", reply_ts
            )
            return ""
        if not os.path.isdir(resolved):
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="!project",
                tool_kind="command",
                outcome="project_denied_invalid",
                metadata={"user": user_id, "channel": channel, "project": resolved},
            )
            await slack.post_message(channel, f"Not a directory: `{resolved}`", reply_ts)
            return ""
        _thread_projects[session_key] = resolved
        if conversation_log:
            try:
                await asyncio.to_thread(
                    conversation_log.update_metadata, session_key, {"project": resolved}
                )
            except Exception:
                logger.debug("Failed to persist project to conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!project",
            tool_kind="command",
            outcome="project_set",
            metadata={"user": user_id, "channel": channel, "project": resolved},
        )
        await sessions.remove(session_key)
        # Discover project-local agents
        project_agents = _discover_project_agents(resolved)
        agent_info = ""
        if project_agents:
            names = ", ".join(
                f"`{s.stem.replace('.agent-spec', '') if '.agent-spec' in s.name else s.stem}`"
                for s in project_agents
            )
            agent_info = f"\nAgents found: {names} — use `!ta <name>` to switch"
        await slack.post_message(
            channel,
            f"Thread agent-discovery project: `{resolved}` "
            f"(scopes `!ta` agent lookup, not the working directory){agent_info}",
            reply_ts,
        )
        return ""

    # ── !allowlist — multi-user access disabled ──
    if cmd == "!allowlist":
        await slack.post_message(
            channel,
            "⛔ Multi-user access is disabled for security. Only the owner can use Kiro Crew via Slack.",
            reply_ts,
        )
        return ""

    # ── !channel always|mention|observe|off / !channel agent <name> (owner-only) ──
    if cmd == "!channel":
        if not is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.channel_config",
                outcome="denied",
                source="slack",
                resources=channel,
                error="not owner",
            )
            await slack.post_message(channel, "⛔ Only the bot owner can use `!channel`.", reply_ts)
            return ""
        from kiro_crew.config.loader import _VALID_ACTIVATIONS

        parts = cmd_text.split()
        if len(parts) == 1:
            cfg = KiroCrewConfig.load()
            ch_cfg = cfg.channel_config(channel)
            agent_info = f", agent=*{ch_cfg.agent}*" if ch_cfg.agent else ""
            await slack.post_message(
                channel,
                f"Channel `{channel}` activation: *{ch_cfg.activation}*{agent_info}\n"
                f"Usage: `!channel always|mention|observe|off` or `!channel agent <name|off>`",
                reply_ts,
            )
            return ""

        subcmd = parts[1].lower()

        # !channel agent <name|off>
        if subcmd == "agent":
            if len(parts) < 3:
                await slack.post_message(
                    channel, "Usage: `!channel agent <name>` or `!channel agent off`", reply_ts
                )
                return ""
            agent_name = parts[2]
            if agent_name.lower() == "off":
                agent_name = ""
            else:
                resolved = _resolve_agent_name(agent_name, _thread_projects.get(session_key))
                if not resolved:
                    names = _list_all_agent_names()
                    await slack.post_message(
                        channel,
                        f"Unknown agent `{agent_name}`. Available: {names}",
                        reply_ts,
                    )
                    return ""
                agent_name = resolved
            await run_config_write(_persist_channel_config, channel, agent=agent_name)
            _reload_orch_cfg()
            sel().log_api_access(
                caller=user_id,
                operation="slack.channel_agent",
                outcome="allowed",
                source="slack",
                resources=f"{channel}:{agent_name or 'default'}",
            )
            label = f"*{agent_name}*" if agent_name else "default"
            await slack.post_message(channel, f"Agent for this channel: {label}", reply_ts)
            return ""

        # !channel always|mention|observe|off
        if subcmd not in _VALID_ACTIVATIONS:
            await slack.post_message(
                channel,
                f"Invalid mode `{subcmd}`. Use: `always`, `mention`, `observe`, or `off`.",
                reply_ts,
            )
            return ""

        await run_config_write(_persist_channel_config, channel, activation=subcmd)
        _reload_orch_cfg()
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_activation",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:{subcmd}",
        )
        await slack.post_message(channel, f"Channel activation set to *{subcmd}*.", reply_ts)
        return ""

    # ── !title — set/generate Slack thread title ──
    if cmd == "!title":
        parts = cmd_text.split()
        title_text = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
        if title_text:
            title_text, _ = redact_exfiltration_urls(title_text)
            title_text, _ = redact_credentials(title_text)
            await slack.set_thread_title(channel, session_key, title_text[:80])
            _mark_titled(session_key, "manual")
            if conversation_log and not _is_slack_restricted(session_key):
                try:
                    await asyncio.to_thread(
                        conversation_log.set_title, session_key, title_text[:80]
                    )
                except Exception:
                    logger.debug(
                        "Failed to set conversation log title for %s", session_key, exc_info=True
                    )
            sel().log_api_access(
                caller=user_id,
                operation="slack.thread_title",
                outcome="allowed",
                source="slack",
                resources=f"{channel}:{session_key}",
            )
            await _add_phase_reaction(slack, channel, msg_ts, "done")
        else:
            await slack.post_message(
                channel, "Usage: `!title <text>` — set a title for this thread.", reply_ts
            )
        return ""

    # Catch-all: unrecognized ! command — post error instead of falling through to LLM
    await slack.post_message(
        channel,
        f"❌ Unknown command `{cmd}`. Type `/kirocrew help` for available commands.",
        reply_ts,
    )
    return ""


def _filter_options_brackets(text: str, bracket_hold: str, stream_buffer: str) -> tuple[str, str]:
    """Filter ``[OPTIONS: ...]`` tags from streaming text character-by-character.

    Returns the updated *(bracket_hold, stream_buffer)* tuple.
    """
    for ch in text:
        if bracket_hold or ch == "[":
            bracket_hold += ch
            if ch == "]":
                if bracket_hold.startswith("[OPTIONS:"):
                    bracket_hold = ""
                else:
                    stream_buffer += bracket_hold
                    bracket_hold = ""
        else:
            stream_buffer += ch
    return bracket_hold, stream_buffer


def build_timing_footer(
    elapsed: float,
    client: LLMProvider | None = None,
) -> tuple[list[dict], str]:
    """Build the timing/context footer blocks for a Slack response.

    Returns ``(blocks, fallback_text)`` suitable for ``post_blocks``.
    """
    if elapsed < 60:
        duration = f"{int(elapsed)}s"
    else:
        mins, secs = divmod(int(elapsed), 60)
        duration = f"{mins}m {secs}s"
    footer_text = f"Finished in {duration}"
    if client is not None:
        try:
            ctx_pct = round(client.context_usage_pct())
            if ctx_pct >= 70:
                ctx_icon = "🔴"
            elif ctx_pct >= 50:
                ctx_icon = "🟠"
            elif ctx_pct >= 30:
                ctx_icon = "🟡"
            else:
                ctx_icon = "🟢"
            footer_text = f"Finished in {duration} · {ctx_icon} ctx {ctx_pct}%"
        except Exception:
            logger.debug("Failed to retrieve context usage", exc_info=True)
    blocks: list[dict] = [
        {"type": "context", "elements": [{"type": "mrkdwn", "text": footer_text}]}
    ]
    return blocks, footer_text


def _append_footer_actions(
    footer_blocks: list[dict],
    options: list[str] | None,
    thread_ts: str | None,
    linked_session_key: str | None,
    dashboard_state: object | None,
    staleness_token: str | None = None,
) -> list[dict]:
    """Append OPTIONS checkboxes and/or Link to Dashboard button to footer blocks.

    *staleness_token* must be minted by the caller, which is async and can do the
    transcript read off the event loop. Absent it the control posts untokened and
    clicks on it are honoured unconditionally.
    """
    if options:
        from kiro_crew.slack.format import build_options_blocks

        footer_blocks.extend(build_options_blocks(options, staleness_token=staleness_token))
    if thread_ts and not linked_session_key and dashboard_state:
        from kiro_crew.slack.format import build_link_dashboard_button

        if footer_blocks and footer_blocks[-1].get("type") == "actions":
            footer_blocks[-1]["elements"].append(build_link_dashboard_button())
        else:
            footer_blocks.append({"type": "actions", "elements": [build_link_dashboard_button()]})
    return footer_blocks


async def _handle_compact_command(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
) -> None:
    """Trigger in-place ACP ``/compact`` on the current thread's session."""
    # Atomically take the turn semaphore for the WHOLE compaction, or refuse.
    # Slack dispatches each message as its own task (asyncio.create_task), so a
    # bare get_provider() + compact() would race a normal turn that holds the
    # session and interleave two prompts on one stdio channel — corrupting
    # session state (the reason Discord/Telegram guard the same way). Because
    # /compact routes through session/prompt, that collision surfaces
    # as "turn already active" and the except path would destroy a healthy
    # session; try_acquire() serializes against the in-flight turn and the
    # finally always releases.
    if not await sessions.try_acquire(session_key):
        if sessions.has_session(session_key):
            await slack.post_message(
                channel,
                "⏳ Still working on your last message — try `!compact` once it finishes.",
                reply_ts,
            )
        else:
            await slack.post_message(channel, "No active session to compact.", reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="no_session",
            )
        return
    try:
        provider = sessions.get_provider(session_key)
        if not provider:
            await slack.post_message(channel, "No active session to compact.", reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="no_session",
            )
            return

        # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
        # backend that cannot serve a manual /compact treats the prompt as
        # ordinary text and never answers, so dispatching would strand the
        # 120s wait below. Informational, never an error.
        unsupported = compact_unsupported_backend(provider)
        if unsupported:
            await slack.post_message(channel, compact_unsupported_reply(unsupported), reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="auto_managed_backend",
                metadata={"backend": unsupported},
            )
            return

        _t0 = time.monotonic()

        # --- Phase 1: Pre-compaction UI (cosmetic — log failures, don't abort) ---
        try:
            await slack.add_reaction(channel, msg_ts, "recycle")
            await slack.post_message(channel, "🔄 Compacting context…", reply_ts)
        except Exception:
            logger.debug("Pre-compact UI failed for %s", session_key, exc_info=True)

        # --- Phase 2: Actual compaction (failures warrant error + session teardown) ---
        result_text: str | None = None
        outcome = "unknown"
        try:
            # Compaction runs over the prompt transport:
            # provider.compact() drives /compact via session/prompt (the
            # commands/execute path does NOT run compaction — it returns with
            # no status). Bound compact()'s prompt turn here,
            # then let wait_for_compaction() own its OWN deadline for a status
            # emitted async after end_turn — it must NOT be nested inside
            # another timeout, or the graceful "timed out" branch is
            # unreachable and a slow-but-healthy session gets destroyed.
            await asyncio.wait_for(provider.compact(), timeout=120)
            cr = await provider.wait_for_compaction()
            if cr["type"] == "completed":
                # ``summary`` is model-facing compacted context, not a
                # user-facing receipt. Never publish its orchestration text.
                result_text = "✅ Context compacted."
                outcome = "completed"
            elif cr["type"] == "failed":
                error = cr.get("summary", "")
                result_text = f"❌ Compaction failed: {error}" if error else "❌ Compaction failed."
                outcome = "failed"
            else:
                result_text = "⚠️ Compaction timed out."
                outcome = "timeout"
        except Exception:
            logger.warning("Compact command failed for %s", session_key, exc_info=True)
            try:
                await slack.post_message(channel, "❌ Compaction failed unexpectedly.", reply_ts)
            except Exception:
                logger.debug("Failed to post compact error for %s", session_key, exc_info=True)
            # Drop the wedged native conversation, NOT the session's channel
            # identity: the map entry carries the thread linkage that
            # ``get_session_for_thread`` routes every later reply through, so a
            # full ``destroy`` would fork this thread into a fresh session with
            # none of its context. Housekeeping never unlinks (see
            # ``SessionMap.prune`` and ``SessionManager._recycle_held``).
            try:
                await sessions.discard_conversation(session_key)
            except Exception:
                logger.warning(
                    "Failed to discard conversation %s after compact failure",
                    session_key,
                    exc_info=True,
                )
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="failed",
                error="exception",
            )
            try:
                await slack.remove_reaction(channel, msg_ts, "recycle")
                await _add_phase_reaction(slack, channel, msg_ts, "done")
            except Exception:
                pass
            return

        # --- Phase 3: Post-compaction reporting (log failures, don't mislead) ---
        try:
            result_text, _ = redact_exfiltration_urls(result_text)
            result_text, _ = redact_credentials(result_text)
            await slack.post_message(channel, result_text, reply_ts)

            elapsed = time.monotonic() - _t0
            footer_blocks, footer_text = build_timing_footer(elapsed)
            await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
        except Exception:
            logger.debug("Post-compact reporting failed for %s", session_key, exc_info=True)

        try:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome=outcome,
            )
        except Exception:
            logger.debug("Failed to log compact outcome for %s", session_key, exc_info=True)
        try:
            await slack.remove_reaction(channel, msg_ts, "recycle")
            await _add_phase_reaction(slack, channel, msg_ts, "done")
        except Exception:
            pass
    finally:
        sessions.release(session_key)


async def maybe_handle_keyword_command(
    text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
    *,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    cron_service: CronService | None = None,
    handle_sessions: bool = True,
    channel_agent: str | None = None,
) -> bool:
    """Intercept the path-independent keyword commands.

    These are plain (non-``!``) keyword commands that must behave identically
    on both the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path: ``sessions``, ``spawn <task>``,
    ``run <spec>`` and natural-language ``cron`` wakeups.

    Returns ``True`` when the message was handled as a keyword command — the
    caller MUST then ``return`` without starting an LLM turn. Returns ``False``
    when the message is not a keyword command and normal routing continues.

    ``!``-bang commands are intentionally NOT handled here; they stay in
    ``handle_message`` (owner/allowed gating, mention stripping, modifiers) and
    are being deprecated in favour of slash commands. Slash commands are
    already path-independent (handled upstream of the native-vs-transport gate),
    so they need no porting.

    *handle_sessions* lets the native path opt out of the ``sessions`` branch
    (it keeps its own earlier, position-sensitive ``sessions`` block so that
    ``!temporary``/``!incognito`` modifier rewrites cannot turn a modified
    message into a bare ``sessions`` match). The transport path has no such
    modifier machinery, so it uses the default and handles all four commands.
    """
    # Resolve the agent so the command-intercept persists record the real agent
    # name in session metadata (thread override, then channel override, then
    # global default), matching handle_message's main path.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
    # ── Sessions keyword: list recent sessions (owner/allowed only) ──
    if handle_sessions and text.strip().lower() == "sessions":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_sessions_command(
                text.strip(),
                slack,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                conversation_log,
                sessions=sessions,
            )
        else:
            # Deny-by-default: unauthorized callers must be audited (so the
            # security pipeline can see attempted access) and given an
            # explicit denial — silent return masks the access attempt.
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="denied",
                source="slack",
                resources=channel,
                error="unauthorized caller",
            )
            await slack.post_message(channel, "_Permission denied._", reply_ts)
        return True

    # ── Subagent spawn: "spawn <task>" (before cron to avoid NL overlap) ──
    if subagent_manager:
        spawn_reply = _handle_spawn_command(text, subagent_manager, session_key)
        if spawn_reply:
            await slack.post_message(channel, spawn_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                # Offloaded via the shared choke point -- see
                # save_conversation_turn_off_loop for why every async caller must.
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    spawn_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Task runner: "run <spec-path>" ──
    if task_runner:
        run_reply = await _handle_run_command(
            text, task_runner, slack, channel, reply_ts, session_key=session_key
        )
        if run_reply:
            await slack.post_message(channel, run_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    run_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Natural language cron: intercept wakeup patterns ──
    if cron_service:
        cron_reply = await _handle_cron_command(
            text, cron_service, channel, reply_ts, user_id=user_id
        )
        if cron_reply:
            await slack.post_message(channel, cron_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    cron_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    return False


async def maybe_route_linked_thread(
    text: str,
    session_key: str,
    user_id: str,
    channel: str,
    slack: SlackClientOps,
    reply_ts: str,
    target_slot: Any | None = None,
    route_pinned: bool = False,
    interpret_commands: bool = True,
) -> bool:
    """Route a Slack message to a linked dashboard slot, if one is linked.

    Shared by the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path so a thread linked via
    ``/kirocrew link-to-dashboard`` behaves identically on both.

    Returns ``True`` when the caller MUST return without further handling —
    either the message was routed into the linked dashboard slot, or an
    unauthorized user was denied. Returns ``False`` when normal routing should
    continue: no dashboard state, no linked slot, or a ``!``-bang command that is
    being read as a command (which is intentionally allowed to fall through).

    *interpret_commands* is ``False`` when the text is MODEL-AUTHORED — an OPTIONS
    click sends a label the model wrote. Such a label is turn content whatever it
    looks like, so the bang fallthrough is suppressed: letting it through would
    persist the turn under the dashboard key while the linked slot's transcript
    never sees it, so the click would resolve to a different slot than an ordinary
    message from the same thread.

    *route_pinned* makes *target_slot* authoritative instead of resolving the
    thread's CURRENT owner. An OPTIONS answer is accepted against the
    conversation that asked the question, but the dispatch runs as a separate
    task -- so re-resolving here would let a link, relink or unlink landing in
    between deliver that answer into a different conversation. Pinning is
    tri-state on purpose: a pinned ``None`` means "this answer belongs to no
    slot", so a thread linked AFTER acceptance cannot capture a native answer
    either.
    """
    if not (_dashboard_state and hasattr(_dashboard_state, "get_linked_slot")):
        return False
    if route_pinned:
        _linked_slot = target_slot
    else:
        # The dashboard _slack_to_slot map is keyed by the bare Slack thread_ts
        # (reply_ts), NOT the namespaced session key — look up with reply_ts so
        # canonical ``slack:<ts>`` session keys still hit linked slots. session_key
        # is kept for the SEL logging below.
        _linked_slot = _dashboard_state.get_linked_slot(reply_ts)
    if not _linked_slot:
        return False

    # Auth check FIRST — deny all messages from unauthorized users.
    if not is_allowed_user(user_id):
        logger.warning("Unauthorized user %s in linked thread %s", user_id, session_key)
        sel().log_tool_invocation(
            session_key=session_key,
            agent="kirocrew",
            source="slack",
            tool_name="linked_thread_intercept",
            tool_kind="permission",
            outcome="denied",
            metadata={"user_id": user_id, "reason": "not_allowed_user"},
        )
        await slack.post_message(channel, "Not authorized.", reply_ts)
        return True

    # Fall through only when this text is READ as a command: a model-authored label would
    # otherwise persist under the dashboard key with the linked transcript never updated.
    _first_word = text.strip().split(maxsplit=1)[0] if text.strip() else ""
    if interpret_commands and _first_word in _BANG_TO_SLASH:
        return False

    _linked_slot_key = _linked_slot.key
    # Redact for UI display only — LLM receives original text so it can process
    # user intent fully (redaction strips URLs/creds that may be relevant
    # context). The LLM's own output is redacted before display.
    _safe_text, _ = redact_exfiltration_urls(text)
    _safe_text, _ = redact_credentials(_safe_text)
    # Nothing rendered this Slack-typed row optimistically in the dashboard, so
    # broadcast_user=True: append delivers the ONE identity-carrying frame
    # (the old manual frame here carried no ``meta.mid``, so a client receiving
    # the row through a second door rendered a duplicate — #5981 family).
    append_and_surface(
        _dashboard_state, _linked_slot, "user", _safe_text, "msg msg-u", broadcast_user=True  # type: ignore[arg-type]
    )
    if not _linked_slot.running:
        from kiro_crew.dashboard.chat import _run_chat

        _chat_task = asyncio.create_task(
            _run_chat(
                _dashboard_state,  # type: ignore[arg-type]
                _linked_slot,
                text,
                _directive_user_origin=True,
            )
        )
        _linked_slot.task = _chat_task
        _dashboard_state._background_tasks.add(_chat_task)  # type: ignore[attr-defined]
        _chat_task.add_done_callback(_dashboard_state._background_tasks.discard)  # type: ignore[attr-defined]
    else:
        # circular import: session_control pulls in dashboard modules at module level.
        from kiro_crew.dashboard.session_control import containment_meta

        # Stamp the admission-time containment (#5911). A linked slot records
        # linked=True here, so its own channel's queued messages keep draining;
        # only a constraint that appears AFTER this enqueue drops the entry.
        _linked_slot.queue_append(
            text,
            meta=containment_meta(_dashboard_state, _linked_slot),  # type: ignore[arg-type]
            directive_user_origin=True,
        )
    _dashboard_state.push_slots_update()  # type: ignore[attr-defined]
    sel().log_tool_invocation(
        session_key=session_key,
        agent="kirocrew",
        source="slack",
        tool_name="linked_thread_intercept",
        tool_kind="permission",
        outcome="allowed",
        metadata={"user_id": user_id, "slot": _linked_slot_key},
    )
    logger.info("Routed linked Slack message to dashboard slot %s", _linked_slot_key)
    return True


async def handle_message(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    text: str,
    thread_ts: str | None,
    msg_ts: str,
    user_id: str,
    team_id: str = "",
    approval_mode: str = APPROVAL_AUTO,
    context_builder: ContextBuilder | None = None,
    cron_service: CronService | None = None,
    conversation_log: ConversationLog | None = None,
    consolidator: HistoryConsolidator | None = None,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    channel_agent: str | None = None,
    user_display_name: str | None = None,
    action_context: str | None = None,
    target_slot_name: str | None = None,
    route_pinned: bool = False,
    asker_key: str | None = None,
    from_trusted_bot: bool = False,
    channel_activation: str | None = None,
    had_voice_input: bool = False,
    interpret_commands: bool = True,
) -> None:
    """Route a Slack message through ACP with streaming and tool approval.

    NOTE: ``from_trusted_bot`` is consumed only in the error path (echo-loop
    suppression). Early-reply paths (hook auto-reply, !status, !sessions) still
    post to Slack unconditionally — safe today because trusted bots send
    structured commands (``[TASK:id]``, ``[ACK:id]``) that don't match those
    patterns. Extend if that assumption changes.

    This function accepts individual parameters for backward compatibility.
    New callers can use ``MessageContext`` to group the service parameters.

    *channel_agent* overrides the default agent for this channel (set via
    per-channel config in ``slack.channels``).

    *interpret_commands* is ``False`` when the text is MODEL-AUTHORED rather than
    typed by the user — an OPTIONS click sends a label the model wrote, so a leading
    command token there is ordinary turn content and never permission to run
    ``!yolo``, ``!agent`` or any future command. Mirrors the Telegram and Discord
    dispatchers, which pass the same flag on their callback paths.
    """
    Stats().inc_message_received()
    _t0 = time.monotonic()
    # reply_ts is the true Slack thread timestamp (used for posting replies and
    # as the key of thread-indexed maps like SessionMap._thread_to_session and
    # dashboard _slack_to_slot). session_key is the namespaced form used for
    # everything session-scoped (registry, conversation log, thread overrides).
    # Deriving the canonical form HERE keeps the key stable across messages:
    # otherwise the first message would run under the bare thread_ts while the
    # second is rewritten to ``slack:<ts>`` by the linked-thread routing below
    # (the self-link canonicalizes), splitting the live session, the
    # conversation log, and the per-thread override maps across two keys.
    reply_ts = thread_ts or msg_ts
    session_key = canonical_key(reply_ts)

    # ONE decision, read by every command branch below and forwarded to the linked router.
    # A new branch that forgets it is visible against the single name, not a token list.
    interpret_as_command = interpret_commands

    # Inbound channels-governance gate (off-loop). Slack is a governed transport
    # like the others: a ``channels`` policy that denies ``slack`` stops inbound
    # dispatch on the very next message without a restart (the ProfileStore
    # hot-reloads by mtime). Default OSS build (no policy) permits, so behavior is
    # unchanged. Silently drop on deny — matching how an unauthorized user is
    # ignored — before any hook/command/turn processing.
    if not await channel_inbound_permitted("slack"):
        logger.info("slack inbound dropped: denied by channels governance policy")
        return

    _hydrate_thread_overrides(session_key, conversation_log)
    _hydrate_conv_flags(sessions, session_key)

    # Resolve agent early so ALL persist paths (hook auto-reply, command
    # intercepts, review-mode drafts, main LLM path) can forward it.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None

    # ── Linked thread intercept: route to dashboard slot if linked ──
    # Resolved from the NAME captured when the answer was accepted, not from the
    # thread's current owner: the name survives a link change, a live slot object
    # would not tell us whether it is still the right destination. A pinned name
    # that no longer resolves falls through to normal handling rather than
    # inventing a target.
    _target_slot = None
    if route_pinned and target_slot_name and _dashboard_state:
        _target_slot = getattr(_dashboard_state, "_slots", {}).get(target_slot_name)

    if await maybe_route_linked_thread(
        text,
        session_key,
        user_id,
        channel,
        slack,
        reply_ts,
        target_slot=_target_slot,
        route_pinned=route_pinned,
        interpret_commands=interpret_as_command,
    ):
        return

    logger.info(
        "🔍 handle_message: thread_ts=%s msg_ts=%s → session_key=%s channel=%s",
        thread_ts,
        msg_ts,
        session_key,
        channel,
    )

    # ── Hook: check for auto-reply before touching ACP ──
    if context_builder:
        hook_result = context_builder.hooks.on_message(text)
        if hook_result.action == HOOK_REPLY:
            await slack.post_message(channel, hook_result.text, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    hook_result.text,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return

    # ── Status keyword: reply with stats summary ──
    if interpret_as_command and text.strip().lower() == "status":
        # Identity status via the active PlatformContext (Default == OSS no-op
        # stub returning ""; an enterprise companion returns the real SSO line).
        sso_line = await current_context().identity.status_line(prefix=" · sso")
        await slack.post_message(channel, Stats().summary() + sso_line, reply_ts)
        return

    # ── Sessions keyword: list recent sessions ──
    if interpret_as_command and text.strip().lower() == "sessions":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_sessions_command(
                text.strip(),
                slack,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                conversation_log,
                sessions=sessions,
            )
        else:
            # Deny-by-default: unauthorized callers must be audited (so the
            # security pipeline can see attempted access) and given an
            # explicit denial — silent return masks the access attempt.
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="denied",
                source="slack",
                resources=channel,
                error="unauthorized caller",
            )
            await slack.post_message(channel, "_Permission denied._", reply_ts)
        return

    # ── Compact keyword: trigger in-place context compaction ──

    _cmd_text = re.sub(r"^<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text.strip())

    # ── !temporary / !incognito privacy modifiers (shared with transport) ──
    if interpret_as_command:
        text, _cmd_text, _only_modifier = await maybe_apply_privacy_modifiers(
            text, _cmd_text, session_key, user_id, channel, slack, sessions, reply_ts
        )
        if _only_modifier:
            return

    if interpret_as_command and _cmd_text.strip().lower() == "!compact":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.compact_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_compact_command(slack, sessions, channel, reply_ts, msg_ts, session_key)
            return
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="denied",
                error=f"unauthorized user {user_id}",
            )
            await slack.post_message(channel, "⛔ Not authorized to compact.", reply_ts)
            return  # deny-by-default: do not fall through

    # ── Owner commands: all "!" prefixed messages are reserved for owner ──
    # Strip leading bot mention from app_mention events so the ! prefix is exposed.
    # DM:       "!agent foo"                    → "!agent foo"       (no-op)
    # @mention: "<@UBOT|kirocrew> !agent foo"   → "!agent foo"      (strip prefix)
    if interpret_as_command and _cmd_text.startswith("!"):
        # !dashboard and !stop are available to any allowed user
        _cmd_word = _cmd_text.split()[0]
        if _cmd_word in ("!dashboard", "!stop", "!title"):
            if is_owner(user_id) or is_allowed_user(user_id):
                reply = await _handle_slash_command(
                    _cmd_text,
                    slack,
                    sessions,
                    channel,
                    reply_ts,
                    msg_ts,
                    session_key,
                    user_id,
                    conversation_log=conversation_log,
                )
                if reply is not None:
                    return
            else:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.allowed_command",
                    outcome="denied",
                    source="slack",
                    resources=_cmd_word,
                    error="unauthorized sender",
                )
                await slack.post_message(channel, "⛔ Not authorized.", reply_ts)
                return
        # All other ! commands are owner-only
        elif not is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.owner_command",
                outcome="denied",
                source="slack",
                resources=_cmd_word,
                error="unauthorized sender",
            )
            await slack.post_message(channel, "⛔ Owner-only command.", reply_ts)
            return
        else:
            reply = await _handle_slash_command(
                _cmd_text,
                slack,
                sessions,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                user_id,
                conversation_log=conversation_log,
            )
            if reply is not None:
                return

    # ── Path-independent keyword commands: spawn/run/cron ──
    # ``sessions`` is deliberately excluded here (handle_sessions=False): the
    # native path keeps its own earlier ``sessions`` block above so that the
    # ``!temporary``/``!incognito`` modifier rewrites can't turn a modified
    # message into a bare ``sessions`` match. The transport path (which has no
    # modifier machinery) handles all four via the same helper.
    if interpret_as_command and await maybe_handle_keyword_command(
        text,
        slack,
        sessions,
        channel,
        reply_ts,
        msg_ts,
        session_key,
        user_id,
        conversation_log,
        subagent_manager=subagent_manager,
        task_runner=task_runner,
        cron_service=cron_service,
        handle_sessions=False,
        channel_agent=channel_agent,
    ):
        return

    # A new turn supersedes whatever question the previous one ended on, so any
    # OPTIONS control still live in this thread stops being answerable.
    #
    # Placed HERE, below every short-circuit above, because only a message that
    # actually starts a turn supersedes anything. ``status``, a permission
    # denial, a modifier-only message, a hook's canned reply and the keyword
    # commands all answer and return WITHOUT running the agent, so the
    # conversation has not moved and the pending question is still the one being
    # waited on. Expiring for those spends a LIVE control and leaves valid
    # choices unanswerable — the exact inverse of the stale click this lifecycle
    # exists to prevent. The denial case matters most: an unauthorized caller in
    # the thread must not be able to destroy the owner's pending question.
    # Keeping this at one point below the short-circuits, rather than guarding
    # each of them, means a shortcut added later inherits the right behaviour.
    #
    # Resolve the OWNING session, not the ``slack:<ts>`` key derived above: the
    # control is recorded under whichever session owns the thread, and for a
    # dashboard-linked thread that is its ``dashboard:chat-N`` key — the same
    # distinction the linked-thread lookup relies on. Expiring under the wrong
    # key silently no-ops and leaves the control clickable.
    await expire_slack_options(
        cast("DashboardState | None", get_dashboard_state()),
        sessions.get_session_for_thread(reply_ts) or session_key,
    )

    status_ctrl = StatusReactionController(
        slack,
        channel,
        msg_ts,
        enabled=KiroCrewConfig.load().slack.reactions_enabled,
    )
    status_ctrl.set_phase("queued")
    _had_error = False
    _stop_reason = ""

    # Set assistant thread status while we wait for the LLM to respond.
    # Defer start_stream until the first text chunk arrives so the user
    # sees the status indicator instead of a blank bot message.
    await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)

    # Post inline stop button (only in threaded conversations to avoid breaking tests)
    _working_ts: str | None = None
    if thread_ts:

        _working_ts = await slack.post_blocks(
            channel, build_working_blocks(session_key), "Working…", reply_ts
        )

    use_slack_stream = False
    stream_ts: str | None = None
    thinking_ts: str | None = None  # 💭 reasoning placeholder, posted above the answer
    _show_thinking = KiroCrewConfig.load().slack.show_thinking
    _stream_had_redaction = False  # True when per-chunk redaction modified a streamed chunk
    # Rolling-buffer redactor for the live Slack wire: withholds the trailing
    # credential-class run so a credential split across streaming chunks can't
    # reach Slack unredacted (issue 3). The final message is posted from the
    # complete, fully-redacted `accumulated`, so the held tail is superseded at
    # stop_stream — no data loss.
    _sred = StreamRedactor()
    accumulated = ""
    thinking_accumulated = ""
    stream_buffer = ""  # unsent chunks for streaming API (buffered between rate-limited appends)
    bracket_hold = ""  # text held back from '[' until ']' to filter [OPTIONS: ...]
    last_edit = 0.0
    _task_counter = 0  # incrementing task ID for task cards
    _active_task_id = ""  # current in-progress task
    _active_task_title = ""  # display title (purpose or tool name)
    _tool_start_time = 0.0  # monotonic time when current tool started
    _tool_timer_task: asyncio.Task | None = None  # periodic elapsed-time updater
    _status_dirty = False  # True when status needs reset to base on next text chunk
    _tool_gap = False

    async def _rotate_stream() -> str | None:
        """Stop the dead stream and start a fresh one. Returns new ts or None."""
        nonlocal stream_ts, use_slack_stream
        if stream_ts:
            await slack.stop_stream(channel, stream_ts)
        new_ts = await slack.start_stream(
            channel, reply_ts, team_id=team_id or None, user_id=user_id or None
        )
        if new_ts:
            stream_ts = new_ts
            logger.info("Stream rotated: new ts=%s", new_ts)
        else:
            use_slack_stream = False
            logger.warning("Stream rotation failed — falling back to chat.update")
        return new_ts

    async def _append_stream(text: str) -> bool:
        """Append text to stream, rotating on failure.

        Streams through the rolling redactor (``_sred``) so a credential split
        across streaming chunks can't reach Slack unredacted (issue 3): only the
        confirmed-safe prefix is sent now; the trailing (possible-partial-
        credential) run is withheld until the next append. The final message is
        posted from the complete, fully-redacted ``accumulated`` at stop_stream,
        so the withheld tail is superseded — never lost.
        """
        nonlocal _stream_had_redaction
        if not stream_ts:
            return True
        if channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress streaming text in review mode
        safe = _sred.feed(text)  # redacts the confirmed-safe prefix internally
        if not safe:
            return True  # whole delta withheld (partial credential) — nothing to send yet
        if "[REDACTED" in safe:
            _stream_had_redaction = True
        ok = await slack.append_stream(channel, stream_ts, safe)
        if not ok and use_slack_stream:
            if await _rotate_stream():
                assert stream_ts is not None
                return await slack.append_stream(channel, stream_ts, safe)
        return ok

    async def _append_task(task_id: str, title: str, status: str, details: str = "") -> bool:
        """Append task card to stream, rotating on failure."""
        if not stream_ts:
            return False
        if channel_activation == ACTIVATION_REVIEW:
            return True  # Suppress task cards in review mode
        ok = await slack.append_task(channel, stream_ts, task_id, title, status, details=details)
        if not ok and use_slack_stream:
            if await _rotate_stream():
                assert stream_ts is not None
                return await slack.append_task(
                    channel, stream_ts, task_id, title, status, details=details
                )
        return ok

    async def _tool_elapsed_updater() -> None:
        """Periodically update the active task card with elapsed time (every 30s)."""
        # reads _active_task_id/_active_task_title/_tool_start_time from the
        # enclosing scope (no rebind here, so no nonlocal needed)
        while True:
            await asyncio.sleep(30)
            if _active_task_id and _tool_start_time and use_slack_stream:
                elapsed = time.monotonic() - _tool_start_time
                mins, secs = divmod(int(elapsed), 60)
                time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
                # Elapsed goes in the TITLE (Slack replaces title on same
                # task_id) — NOT details, which Slack APPENDS, causing the
                # "⏱ 30s ⏱ 1m 0s ⏱ 1m 30s" accumulation bug.
                await _append_task(
                    _active_task_id,
                    f"{_active_task_title}  ⏱ {time_str}",
                    "in_progress",
                )

    def _start_tool_timer() -> None:
        """Start the 30s elapsed-time updater for the current tool."""
        nonlocal _tool_timer_task, _tool_start_time
        _cancel_tool_timer()
        _tool_start_time = time.monotonic()
        _tool_timer_task = asyncio.ensure_future(_tool_elapsed_updater())

    def _cancel_tool_timer() -> None:
        """Cancel the tool elapsed-time updater."""
        nonlocal _tool_timer_task
        if _tool_timer_task and not _tool_timer_task.done():
            _tool_timer_task.cancel()
        _tool_timer_task = None

    def _tool_elapsed_str() -> str:
        """Return formatted elapsed time for the current tool, or empty string."""
        if not _tool_start_time:
            return ""
        elapsed = time.monotonic() - _tool_start_time
        if elapsed < 1:
            return ""
        mins, secs = divmod(elapsed, 60)
        if mins:
            return f"⏱ {int(mins)}m {secs:.1f}s"
        return f"⏱ {secs:.1f}s"

    async def _ensure_stream_started() -> None:
        """Lazy-start the stream on first event. Falls back to chat.update."""
        nonlocal stream_ts, use_slack_stream, thinking_ts
        if stream_ts is not None:
            return
        if channel_activation == ACTIVATION_REVIEW:
            # No visible message — only thread status indicator is shown
            stream_ts = _REVIEW_PLACEHOLDER_TS
            use_slack_stream = False
            return
        # Reserve the 💭 reasoning slot ABOVE the answer *before* the response
        # message is created. This must run regardless of which
        # event arrived first: if a text/tool event precedes the first
        # reasoning chunk, posting the placeholder here is the only way to keep
        # reasoning above the answer (the reasoning-chunk branch never got the
        # chance). Guarded on thinking_ts is None so we never double-post when
        # the reasoning branch already claimed the slot. An empty placeholder
        # (no reasoning this turn) is cleaned up at end of turn.
        if _show_thinking and thinking_ts is None:
            try:
                thinking_ts = await slack.post_message(channel, _THINKING_PLACEHOLDER, reply_ts)
            except Exception:
                logger.debug("Failed to reserve thinking slot", exc_info=True)
        stream_ts = await slack.start_stream(
            channel, reply_ts, team_id=team_id or None, user_id=user_id or None
        )
        use_slack_stream = stream_ts is not None
        if not use_slack_stream:
            stream_ts = await slack.post_message(channel, _THINKING, reply_ts)
        assert stream_ts is not None

    task = Task(id=msg_ts)
    _acquired = False

    # ── Bidirectional sync: check if this Slack thread is linked to a dashboard session ──
    # The thread index is keyed by the bare Slack thread_ts (reply_ts), NOT the
    # namespaced session key. A self-linked Slack thread resolves to our own
    # canonical key (no-op rewrite); a dashboard-linked thread resolves to its
    # ``dashboard:chat-N`` key.
    # Read the thread's owner ONCE and keep it truthful. Three separate decisions
    # below consume it -- whether to re-route this turn, whether to CLAIM the
    # thread, and whether to mirror into a dashboard slot -- and a pinned answer
    # needs a different answer for each. Falsifying this single value to steer all
    # three is what made the pin land wrong three times running.
    thread_owner_key = sessions.get_session_for_thread(reply_ts)
    # Mirror/footer value: a pinned answer belongs to the conversation that ASKED,
    # not to whoever owns the thread now, so it mirrors nowhere. (A pinned asker
    # that *does* hold a slot never reaches here -- maybe_route_linked_thread
    # already delivered the turn into that slot and returned.)
    linked_session_key = None if route_pinned else thread_owner_key
    if route_pinned:
        # A pinned answer names its own conversation, so the thread's CURRENT
        # owner has no say -- rewriting the key here is what let a pinned answer
        # land in whoever took the thread over in the meantime.
        #
        # Suppressing that rewrite is only half of it. A pinned asker that holds no
        # slot -- a cron or native conversation -- would otherwise be left running
        # under the bare Slack thread key, which for a cron asker is a DIFFERENT
        # conversation: the answer would open a new session and take the thread
        # mapping with it. So the asker becomes the session key outright.
        if asker_key:
            session_key = asker_key
            # Same reason as the linked-thread reroute below: overrides are keyed
            # BY SESSION and the hydration at entry ran for the PREVIOUS key, so
            # without this the agent re-resolution reads a key nobody hydrated and
            # falls through to the channel or default agent -- discarding a binding
            # the asker's own metadata records correctly. The pinned path needs it
            # exactly as much: `asker_key` is a different conversation, which is
            # the whole reason it is substituted here.
            _hydrate_thread_overrides(session_key, conversation_log)
    elif thread_owner_key and thread_owner_key != session_key:
        logger.info(
            "🔗 Slack thread %s linked to dashboard session %s — routing there",
            session_key,
            thread_owner_key,
        )
        session_key = thread_owner_key
        # Thread overrides are keyed BY SESSION, and the hydration at entry ran
        # for the previous key. Re-hydrate for the new owner in the same breath
        # as the reroute -- otherwise the agent re-resolution below reads a key
        # that was never hydrated and falls through to the channel or default
        # agent, discarding a binding the session's own metadata records
        # correctly. Same shape as transport_dispatch._resolve_thread_owner.
        # The helper guards repeated I/O per session, so this is cheap.
        _hydrate_thread_overrides(session_key, conversation_log)

    client: LLMProvider | None = None
    try:
        task.start()
        # Re-resolve _agent against (possibly linked) session_key for the main
        # LLM path — linked dashboard sessions may carry a different thread agent.
        _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
        client, is_new, resumed = await sessions.get_or_create(
            session_key, agent=_agent, channel_id=channel
        )
        _acquired = True
        # Expire AGAIN now the turn is serialized — see the same call in
        # transport_dispatch. The pass earlier in this function runs before
        # `get_or_create` waits its turn, so two messages arriving together both
        # clear the OLD control and neither clears the NEW one the first turn
        # posts on its way out, leaving live buttons for a superseded question.
        await expire_slack_options(
            cast("DashboardState | None", get_dashboard_state()),
            sessions.get_session_for_thread(reply_ts) or session_key,
        )
        if is_new:
            await sessions.set_channel(session_key, channel)
        if thread_owner_key is None and not route_pinned:
            # Self-link: thread index maps the bare Slack thread_ts to this
            # session's canonical key. reply_ts (not session_key) is the true
            # Slack timestamp — storing the namespaced key as slack_thread_ts
            # would corrupt reply routing.
            #
            # A PINNED answer never claims the thread, however empty the index
            # looks. Pinning exists so an accepted click cannot mutate thread
            # routing: a cron or native asker claiming the thread here would
            # evict its real owner, and every later human reply would land in
            # the cron conversation instead.
            sessions.set_slack_link(session_key, reply_ts, channel)
        logger.info(
            "🔍 session state: key=%s is_new=%s resumed=%s",
            session_key,
            is_new,
            resumed,
        )

        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
        await publish_turn_identity(sessions, session_key)

        # Build message with context injection
        compressed: str | None = None
        # Scale the injected-context budget to the live model's context window
        # (200K model ⇒ one-fifth the memory/lessons/history chars of a 1M
        # model, same window share). Derived from the resolved session client;
        # Auto/unknown ⇒ None ⇒ the 1M reference (unchanged default).
        _model_window = window_for_provider_client(client)
        # is_new = new kiro-cli/dashboard process, NOT new conversation.
        # The Slack thread persists across processes, so we compress its
        # history to bootstrap the fresh session's context window.
        if is_new and not resumed and context_builder and context_builder.conversation_log:
            compressed = await compress_thread_history(
                context_builder.conversation_log,
                session_key,
                text,
                sessions,
                model_window=_model_window,
            )

        # After a soft-cancel, kiro-cli drops the cancelled turn from its
        # conversation log — but the user+assistant text is persisted to our
        # local conversation_log. Re-inject just the cancelled turn as a
        # preamble so the LLM remembers what was interrupted. Flag lives on
        # the session (set by SessionManager.stop_turn), consumed one-shot.
        # Use getattr for prev_turn_cancelled so test doubles (AsyncMock)
        # don't raise AttributeError on coroutine-returning mock chains.
        _session = getattr(sessions, "_sessions", {}).get(session_key)
        if (
            _session is not None
            and getattr(_session, "prev_turn_cancelled", False)
            and context_builder
            and context_builder.conversation_log
        ):
            _session.prev_turn_cancelled = False
            _preamble = build_cancelled_turn_preamble(context_builder.conversation_log, session_key)
            if _preamble:
                text = _preamble + "\n\n" + text

        # Fetch thread parent message when starting a new session in an
        # existing thread (e.g. replying to a cron thread).  Gives the LLM
        # context about what started the thread without requiring manual
        # batch_get_thread_replies.
        thread_parent_text: str | None = None
        if is_new and not resumed and thread_ts and context_builder:
            if not compressed:
                thread_parent_text = await slack.fetch_message(channel, thread_ts)
            if thread_parent_text:
                thread_parent_text = redact(thread_parent_text)
                if len(thread_parent_text) > 3000:
                    thread_parent_text = (
                        thread_parent_text[:3000]
                        + "\n[truncated — use batch_get_thread_replies for full text]"
                    )

        if context_builder:
            # Thread-scoped temporary mode: blocks memory reads.
            _slack_blocks_reads = is_thread_temporary(session_key)

            # Fallback thread metadata: when thread_parent_text is unavailable
            # (e.g. fetch_message failed), try conversations.replies to get parent info.
            # Note: requires channels:history (public) or groups:history (private). Both
            # ship in the manifest, but installs created before groups:history was added
            # need a reinstall to gain it. Gracefully degrades — if scope is missing,
            # thread context is simply skipped.
            _thread_meta: str | None = None
            if (
                is_new
                and not resumed
                and thread_ts
                and not thread_parent_text
                and not compressed
                and context_builder
            ):
                replies = await slack.fetch_thread_replies(
                    channel, thread_ts, limit=1, warn_on_pagination=False
                )
                if replies:
                    parent = replies[0]
                    reply_count = parent.get("reply_count", 0)
                    parent_text = redact(parent.get("text", ""))
                    if parent_text:
                        if len(parent_text) > 500:
                            parent_text = parent_text[:500] + "…[truncated]"
                        if reply_count > 0:
                            _thread_meta = (
                                f'[Thread has {reply_count} replies. Parent message: "{parent_text}"]\n'
                                "Use batch_get_thread_replies to read the full thread if needed.\n"
                            )
                        else:
                            _thread_meta = f'[Parent message: "{parent_text}"]\n'
                else:
                    logger.info(
                        "Thread fallback returned no replies for %s/%s (missing scope?)",
                        channel,
                        thread_ts,
                    )

            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                context_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel,
                thread_ts=thread_ts or msg_ts,
                agent=_agent,
                resumed=resumed,
                user_display_name=user_display_name,
                compressed_history=compressed,
                action_context=action_context,
                thread_parent_text=thread_parent_text,
                thread_meta=_thread_meta,
                blocks_reads=_slack_blocks_reads,
                model_window=_model_window,
                runtime_source="slack",
            )
        else:
            full_message = text

        # ── Early cancellation check: bail before expensive LLM call ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled before LLM call — skipping", msg_ts)
            await slack.set_thread_status(channel, reply_ts, "")
            return

        # Lease-dispatch race gate: the session lease was taken by
        # get_or_create above, but the turn only opens on the first stream
        # iteration below. If a gateway restart moved the SessionManager into the
        # closing state during the async prep between, dispatching now would open
        # a turn ABSENT from the shutdown drain snapshot → killed mid-turn with
        # its native lock held (empty-response bug). Re-check SYNCHRONOUSLY here
        # (no await between this check and the async-for) so the _closing read
        # and the stream's turn registration are one atomic span, strictly
        # ordered w.r.t. close_all's _closing set. Abort if closing (the outer
        # finally releases the lease).
        try:
            sessions.begin_turn(session_key)
        except SessionClosingError:
            logger.info("Aborting Slack dispatch for %s — gateway shutting down", session_key)
            await slack.set_thread_status(channel, reply_ts, "")
            return

        async for event in client.stream(full_message):
            if event.kind == EVENT_TEXT_CHUNK:
                if _tool_gap and accumulated and accumulated[-1:] not in ("\n", " "):
                    first = event.text[:1]
                    if first and first not in ("\n", " "):
                        event.text = "\n\n" + event.text
                event.text, _exfil_w = redact_exfiltration_urls(event.text)
                event.text, _cred_w = redact_credentials(event.text)
                if _exfil_w or _cred_w:
                    _stream_had_redaction = True

                if event.text:
                    _tool_gap = False
                status_ctrl.set_phase("thinking")
                status_ctrl.on_progress()
                accumulated += event.text

                if _status_dirty and use_slack_stream:
                    await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)
                    _status_dirty = False

                # ── Bracket hold-back: filter [OPTIONS: ...] from stream ──
                # When inside a bracket, accumulate into bracket_hold.
                # On ']', release if not OPTIONS, suppress if it is.
                if use_slack_stream:
                    bracket_hold, stream_buffer = _filter_options_brackets(
                        event.text, bracket_hold, stream_buffer
                    )
                else:
                    stream_buffer += event.text

                await _ensure_stream_started()

                now = time.monotonic()
                if now - last_edit >= _EDIT_INTERVAL:
                    if use_slack_stream:
                        if stream_buffer:
                            stream_buffer, _ = strip_thinking_tags(
                                stream_buffer, strip_whitespace=False
                            )
                            await _append_stream(stream_buffer)
                            stream_buffer = ""
                    else:
                        assert stream_ts is not None
                        if channel_activation != ACTIVATION_REVIEW:
                            await _safe_update(
                                slack, channel, stream_ts, redact(accumulated) + _CURSOR
                            )
                    last_edit = now

            elif event.kind == EVENT_THINKING_CHUNK:
                status_ctrl.set_phase("thinking")
                status_ctrl.on_progress()
                thinking_accumulated += event.text
                # Claim the 💭 slot as soon as reasoning starts so it appears
                # promptly during a long thinking phase (early feedback). This
                # is an optimization for the common reasoning-first case; the
                # ordering guarantee itself lives in _ensure_stream_started,
                # which reserves the slot before the answer message whenever it
                # hasn't been claimed yet (handles text/tool-first turns).
                if (
                    _show_thinking
                    and thinking_ts is None
                    and stream_ts is None
                    and channel_activation != ACTIVATION_REVIEW
                ):
                    try:
                        thinking_ts = await slack.post_message(
                            channel, _THINKING_PLACEHOLDER, reply_ts
                        )
                    except Exception:
                        logger.debug("Failed to post thinking placeholder", exc_info=True)

            elif event.kind == EVENT_TOOL_CALL:
                _tool_gap = True
                # Check tool hooks. NOTE: EVENT_TOOL_CALL is informational —
                # the tool has already been auto-approved by the provider and
                # is executing; this branch cannot reject_tool(). The real
                # enforceable gate is EVENT_PERMISSION_REQUEST below. So we do
                # NOT arm deny-by-default here (is_shell omitted): a shell tool
                # with an unrecoverable command would otherwise render a
                # misleading "blocked" message while the tool actually runs.
                # A genuine deny-list / sensitive-path match still surfaces a
                # (best-effort, non-enforcing) warning + audit.
                if context_builder:
                    tool_result = context_builder.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        command=event.shell_command,
                    )
                    if tool_result.action == TOOL_DENY:
                        # event.title is LLM-authored (select_tool_title prefers
                        # the model's description) — never post it to Slack raw.
                        _flagged_title, _ = redact_exfiltration_urls(event.title)
                        _flagged_title, _ = redact_credentials(_flagged_title)
                        accumulated += (
                            f"\n⚠️ _Tool `{_flagged_title}` flagged by security "
                            f"hooks (already executing; cannot be stopped here)._"
                        )
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="flagged_unenforceable",
                            error="hook_deny",
                        )
                        continue

                sel().log_tool_invocation(
                    session_key=session_key,
                    source="slack",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="invoked",
                )

                tool_name = event.title.removeprefix("Running: ")
                tool_name, _ = redact_exfiltration_urls(tool_name)
                tool_name, _ = redact_credentials(tool_name)
                tool_kind = event.tool_kind or ""
                status_ctrl.set_phase(_tool_to_phase(tool_name, tool_kind))
                status_ctrl.on_progress()
                tool_detail = event.tool_purpose or tool_kind
                tool_status = f"\n🫆 `{tool_name}`\n"
                await _ensure_stream_started()
                if use_slack_stream:
                    await slack.set_thread_status(channel, reply_ts, f"is using {tool_name}")
                    _status_dirty = True
                if use_slack_stream:
                    # Flush any buffered text before the tool status
                    if stream_buffer:
                        stream_buffer, _ = strip_thinking_tags(
                            stream_buffer, strip_whitespace=False
                        )
                        await _append_stream(stream_buffer)
                        stream_buffer = ""
                    # Mark previous task complete, start new one
                    if _active_task_id:
                        _elapsed = _tool_elapsed_str()
                        _cancel_tool_timer()
                        _ct = (
                            f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
                        )
                        await _append_task(_active_task_id, _ct, "complete")
                    _task_counter += 1
                    _active_task_id = f"tool_{_task_counter}"
                    _active_task_title = event.tool_purpose or tool_name
                    _active_task_title, _ = redact_exfiltration_urls(_active_task_title)
                    _active_task_title, _ = redact_credentials(_active_task_title)
                    await _append_task(
                        _active_task_id,
                        title=_active_task_title,
                        status="in_progress",
                        details=tool_name if tool_detail else "",
                    )
                    _start_tool_timer()
                else:
                    accumulated += tool_status
                    assert stream_ts is not None
                    if channel_activation != ACTIVATION_REVIEW:
                        await _safe_update(slack, channel, stream_ts, redact(accumulated) + _CURSOR)
                last_edit = time.monotonic()

                # wait tool blocks MCP for up to 30min — finalize the
                # streaming message now so Slack doesn't show an error.
                # _ensure_stream_started() will open a new message when
                # the next text chunk arrives after wait returns.
                if tool_name == "wait" and use_slack_stream and stream_ts:
                    if _active_task_id:
                        _elapsed = _tool_elapsed_str()
                        _cancel_tool_timer()
                        _ct = (
                            f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
                        )
                        await _append_task(_active_task_id, _ct, "complete")
                        _active_task_id = ""
                    await slack.stop_stream(channel, stream_ts)
                    stream_ts = None
                    accumulated = ""

            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Check tool hooks for auto-approve
                if context_builder:
                    tool_result = context_builder.hooks.on_tool_call(
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        tool_kind=event.tool_kind,
                        raw_params=event.raw_tool_params,
                        command=event.shell_command,
                        is_shell=event.is_shell,
                    )
                    if tool_result.action == TOOL_AUTO_APPROVE:
                        # The hook granted this by NAME (its `auto_approve_tools`
                        # globs, or the read-only allowlist). Honour it only
                        # while each program name in the command still resolves
                        # to the program it appears to name; a shadowed,
                        # agent-tree or unidentified resolution DOWNGRADES to
                        # the remaining rungs below (spawn hook, approval mode,
                        # trust/YOLO, the interactive buttons) — never a hard
                        # block.
                        _ng_refusal = await name_grant.refusal_for_event(event)
                        if _ng_refusal is None:
                            await client.approve_tool(event.request_id)
                            Stats().inc_tool_auto_approved()
                            sel().log_tool_invocation(
                                session_key=session_key,
                                source="slack",
                                tool_name=event.title,
                                tool_kind=event.tool_kind,
                                outcome="auto_approved",
                                request_id=event.request_id,
                                metadata={"reason": "hook_auto_approve"},
                            )
                            continue
                        logger.warning(
                            "declining a hook auto-approve: %s; the request "
                            "falls through to the Slack handler's normal "
                            "approval ladder",
                            _ng_refusal.log_text,
                        )
                        name_grant.log_decline(
                            source="slack",
                            session_key=session_key,
                            event=event,
                            refusal=_ng_refusal,
                            tier="hook_auto_approve",
                            sel_factory=sel,
                        )
                    if tool_result.action == TOOL_DENY:
                        await client.reject_tool(event.request_id)
                        Stats().inc_tool_denial()
                        # event.title is LLM-authored — redact before posting.
                        _blocked_title, _ = redact_exfiltration_urls(event.title)
                        _blocked_title, _ = redact_credentials(_blocked_title)
                        accumulated += f"\n🚫 _Tool `{_blocked_title}` blocked by hooks._"
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error="hook_deny",
                        )
                        continue

                # auto_approve_subagent_spawn → auto-approve spawn_run tool calls
                if _should_auto_approve_spawn(context_builder, event):
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "auto_approve_subagent_spawn"},
                    )
                    continue

                if approval_mode == APPROVAL_AUTO:
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "approval_mode_auto"},
                    )
                    continue

                # Trust mode (per-session) or YOLO mode (owner-only global) → auto-approve
                _yolo_now = is_yolo_mode()
                if _yolo_now or session_key in _trusted_sessions:
                    await client.approve_tool(event.request_id)
                    Stats().inc_tool_auto_approved()
                    logger.info(
                        "Auto-approved %s (%s)",
                        event.title,
                        "yolo" if _yolo_now else "trust",
                    )
                    sel().log_tool_invocation(
                        session_key=session_key,
                        source="slack",
                        tool_name=event.title,
                        tool_kind=event.tool_kind,
                        outcome="auto_approved",
                        request_id=event.request_id,
                        metadata={"reason": "yolo" if _yolo_now else "trust"},
                    )
                    continue

                logger.info("Permission request: tool=%s req_id=%s", event.title, event.request_id)
                status_ctrl.pause_stall_watchdog()
                task.await_approval()
                # The stream-prep Slack calls below run BEFORE _request_approval
                # answers the permission. If any raises (rate-limit, network),
                # the ACP permission request would be orphaned and the
                # subprocess would wedge — reject it before propagating so the
                # turn unblocks. _request_approval guards its own post failure.
                try:
                    await _ensure_stream_started()
                    if use_slack_stream:
                        await slack.set_thread_status(channel, reply_ts, "Waiting for approval…")
                        _status_dirty = True
                        # Flush buffered text before approval pause
                        if stream_buffer:
                            stream_buffer, _ = strip_thinking_tags(
                                stream_buffer, strip_whitespace=False
                            )
                            await _append_stream(stream_buffer)
                            stream_buffer = ""
                except Exception:
                    await _reject_orphaned_tool(client, event.request_id)
                    raise

                outcome = await _request_approval(
                    slack,
                    client,
                    channel,
                    reply_ts,
                    event,
                    session_key,
                    is_dm=channel.startswith("D"),
                )
                task.resume()
                status_ctrl.resume_stall_watchdog()
                sel().log_tool_invocation(
                    session_key=session_key,
                    source="slack",
                    tool_name=event.title,
                    tool_kind=event.tool_kind,
                    outcome="approved" if outcome != _OUTCOME_REJECTED else "rejected",
                    request_id=event.request_id,
                    metadata={"reason": "interactive"},
                )
                if outcome == _OUTCOME_REJECTED:
                    if use_slack_stream and _active_task_id:
                        _cancel_tool_timer()
                        assert stream_ts is not None
                        await _append_task(_active_task_id, _active_task_title, "error")
                        _active_task_id = ""
                    if not use_slack_stream:
                        accumulated += "\n🚫 _Tool use rejected._"
                    break

            elif event.kind == EVENT_COMPLETE:
                status_ctrl.on_progress()
                _stop_reason = event.stop_reason
                if (
                    _stop_reason
                    and _stop_reason != STOP_REASON_END_TURN
                    and _stop_reason != STOP_REASON_CANCELLED
                    # Expected terminal state after a failed auto-compaction;
                    # handled below with a session reset, so not "unexpected".
                    and _stop_reason != STOP_REASON_COMPACTION_FAILED
                ):
                    logger.warning(
                        "Unexpected stop_reason %r for %s — treating as normal completion",
                        _stop_reason,
                        session_key,
                    )
                break

        if _stop_reason == STOP_REASON_CANCELLED:
            logger.info("Turn cancelled by user for %s", session_key)
            task.complete()
        else:
            task.complete()
            sessions.record_success(session_key)
            Stats().inc_message_success()
            # Per-interaction telemetry (PlatformContext seam) — shared helper so
            # the payload shape and model reflection cannot drift across surfaces.
            record_interaction_event(client, session_key, "slack")

        if _stop_reason == STOP_REASON_COMPACTION_FAILED:
            # The completion was synthetic — the backend abandoned the turn
            # after a failed auto-compaction and never sent end_turn, so it
            # still counts the prompt as in progress. Reset now (mirrors the
            # dashboard runner's needs_session_reset) or the NEXT message
            # collides with "prompt already in progress" and burns the busy
            # recovery path. No re-queue: the compaction notice already told
            # the user. The context-usage probe is skipped — compaction just
            # failed and the session was torn down.
            try:
                await sessions.reset(session_key)
            except Exception:
                logger.debug(
                    "Failed to reset session %s after compaction failure",
                    session_key,
                    exc_info=True,
                )
        else:
            # Check context usage — fires background compaction at configured threshold, never blocks
            sessions.check_context_usage(session_key, client)

    except AcpTimeoutError as e:
        _had_error = True
        accumulated = e.partial_output or "⏱️ Request timed out. Please try again."
        task.fail("timeout")
        await sessions.record_failure(session_key)
        Stats().inc_timeout()
        Stats().inc_message_failed()
    except AcpProcessDied:
        _had_error = True
        accumulated = accumulated or "💀 Agent process died. Please try again."
        task.fail("process_died")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpPromptBusy as e:
        _had_error = True
        # Session is wedged mid-prompt — reset the provider so the next
        # message cold-starts cleanly instead of hitting the same wall.
        try:
            await sessions.reset(session_key)
        except Exception:
            logger.debug("Failed to reset session %s after prompt-busy", session_key, exc_info=True)
        accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpError as e:
        _had_error = True
        accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except Exception:
        _had_error = True
        logger.exception("Unexpected error handling message")
        accumulated = accumulated or "🔧 Something went wrong. Please try again."
        task.fail("unexpected")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    finally:
        if _acquired:
            sessions.release(session_key)
        status_ctrl.finalize(error=_had_error)
        await asyncio.sleep(0)  # let finalize fire

    # ── Cancelled check: suppress response if message was deleted mid-flight ──
    if sessions.is_cancelled(session_key, msg_ts):
        logger.info("Message %s cancelled (deleted) — suppressing response", msg_ts)
        await slack.set_thread_status(channel, reply_ts, "")
        if stream_ts:
            try:
                await slack.delete_message(channel, stream_ts)
            except Exception:
                logger.debug("Failed to delete cancelled stream", exc_info=True)
        if thinking_ts:
            try:
                await slack.delete_message(channel, thinking_ts)
            except Exception:
                logger.debug("Failed to delete thinking placeholder", exc_info=True)
        if _working_ts:
            try:
                await slack.delete_message(channel, _working_ts)
            except Exception:
                pass
        return

    # Clear assistant thread status (skip in review mode — keep indicator until button press)
    if channel_activation != ACTIVATION_REVIEW:
        await slack.set_thread_status(channel, reply_ts, "")

    # Remove inline stop button
    if _working_ts:
        try:
            await slack.delete_message(channel, _working_ts)
        except Exception:
            pass

    # Suppress error replies for trusted bot messages to prevent echo loops
    if from_trusted_bot and _had_error:
        logger.info("Suppressing error reply to trusted bot message to prevent echo loop")
        if thinking_ts:
            try:
                await slack.delete_message(channel, thinking_ts)
            except Exception:
                logger.debug("Failed to delete thinking placeholder", exc_info=True)
        if conversation_log and not _is_slack_restricted(session_key):
            await save_conversation_turn_off_loop(
                conversation_log,
                session_key,
                text,
                "[suppressed: trusted bot error]",
                source_thread=session_key,
                source_user=user_id,
                agent=_agent,
            )
        return

    # Strip any inline <thinking> tags that leaked into the text
    if accumulated:
        accumulated, inline_thinking = strip_thinking_tags(accumulated)
        accumulated = accumulated.strip()
        if inline_thinking:
            thinking_accumulated += ("\n\n" if thinking_accumulated else "") + inline_thinking

    actually_streamed = use_slack_stream and bool(stream_ts)
    # render_one_for_slack normalises ANSI and redacts BEFORE converting, then
    # again after. Converting first (as this did) let to_slack_mrkdwn's ANSI strip
    # reassemble a credential the escapes had broken up, and let its 39,000-char
    # self-truncation cut one in half before the regex below could match it.
    # keep_tables is forced here because Slack's rich streaming renderer draws
    # tables itself when the stream actually started.
    #
    # _render_redacted carries whether that internal redaction fired. It is
    # load-bearing, not informational: the answer has ALREADY been posted
    # incrementally, and the only thing that replaces the visible copy is the
    # final-update condition below. The outer passes cannot supply that signal
    # any more, because by the time they run the render has already cleaned the
    # text and they find nothing left to redact.
    # Extract the OPTIONS tag from the RAW accumulated text, BEFORE rendering.
    # It is a plain-text marker at the very end of the turn, so rendering first
    # makes the controls hostage to the render's size ceiling: a >39,000-char
    # answer ending in [OPTIONS: ...] is truncated, the tag goes with the tail,
    # and the buttons silently never appear. Matches the ordering used by the
    # cron, subagent-completion and dashboard-mirror paths.
    _body_text, options = extract_options(accumulated) if accumulated else ("", [])

    _render = render_one_for_slack(_body_text, keep_tables=actually_streamed)
    final_text = _render.text or _NO_RESPONSE
    _render_redacted = _render.redacted

    # Second pass at the boundary: the decorator seam below can still introduce
    # text, and these warning lists drive the final chat_update decision.
    final_text, exfil_warnings = redact_exfiltration_urls(final_text)
    for w in exfil_warnings:
        logger.warning("Exfiltration URL redacted in response: %s", w)
    final_text, cred_warnings = redact_credentials(final_text)
    for w in cred_warnings:
        logger.warning("Credential redacted in response: %s", w)

    clean_text = final_text

    # Outbound-reply decorator seam (Default: identity, OSS-identical). The model
    # has finished speaking, so this is the outbound half of an active
    # conversation — an edition may refresh its Slack auth window's activity clock
    # and append a "<5 min left" expiry footer here. The public DefaultDashboard-
    # Contributor returns the text unchanged. Fail-safe: a raising decorator falls
    # back to the undecorated text so it can never break the reply.
    from kiro_crew.platform import current_context, safe_context_call

    _pre_decorate = clean_text
    clean_text = safe_context_call(
        lambda: current_context().dashboard.decorate_reply(
            clean_text, channel=channel, user_id=user_id
        ),
        fallback=clean_text,
        log_message="dashboard.decorate_reply failed; sending undecorated reply",
    )
    # Re-run the redaction passes on any text the decorator INTRODUCED. Redaction
    # above (3493-3498) ran before decoration, so a decorator that appends a URL or
    # a credential-shaped token would otherwise reach Slack unscanned (link-preview
    # exfiltration / credential disclosure). Only re-scan when the decorator changed
    # the text (the common Default path is a no-op identity, so this is skipped).
    if clean_text != _pre_decorate:
        clean_text, _exfil_after = redact_exfiltration_urls(clean_text)
        if _exfil_after:
            logger.warning(
                "Redacted %d exfiltration URL(s) introduced by reply decorator", len(_exfil_after)
            )
        clean_text, _cred_after = redact_credentials(clean_text)
        if _cred_after:
            # Log only the COUNT — the per-warning strings embed a truncated
            # prefix of the matched credential (redact_credentials returns
            # "Redacted credential pattern: <first 20 chars>..."), so logging
            # them verbatim would defeat the redaction we just performed.
            logger.warning(
                "Redacted %d credential pattern(s) introduced by reply decorator", len(_cred_after)
            )

    # Per-turn tally of redaction placeholders in the text actually SENT, so the
    # user learns their pasteable text was rewritten. Read from the TAG in
    # `clean_text` rather than from `cred_warnings`, which only reaches the log:
    # on the streaming path that list is empty here because each chunk was already
    # redacted upstream, so re-redacting `clean_text` reports nothing. Counting the
    # artifact answers the question the user has -- "is what I am about to copy
    # still what the assistant wrote?" -- and stays correct wherever the
    # substitution happened (per-chunk, the StreamRedactor wire pass, the final
    # render, or the post-decorator scan). Sum every tag the redactor can emit
    # (`CREDENTIAL_REDACTION_TAGS`) so an encoded-credential-only reply is not
    # missed.
    #
    # The thinking block (redacted separately below) adds to this SAME tally so a
    # single warning covers the turn if either the answer or the thinking was
    # rewritten -- one turn, one notice, never two identical warnings.
    _cred_redactions = sum(clean_text.count(tag) for tag in CREDENTIAL_REDACTION_TAGS)

    # ── Review mode: ephemeral draft instead of public post ──
    if channel_activation == ACTIVATION_REVIEW:
        from kiro_crew.slack.blocks import review_draft_blocks

        # Stop streaming, delete placeholder, set status indicator
        if stream_ts and stream_ts != _REVIEW_PLACEHOLDER_TS:
            if use_slack_stream:
                try:
                    await slack.stop_stream(channel, stream_ts)
                except Exception:
                    pass
            try:
                await slack.delete_message(channel, stream_ts)
            except Exception:
                logger.debug("Failed to delete stream msg in review mode", exc_info=True)
        await slack.set_thread_status(channel, reply_ts, "Awaiting review…")
        # Post ephemeral draft with approve/edit/cancel buttons
        draft = clean_text or _NO_RESPONSE
        draft_key = f"{channel}|{reply_ts}|{uuid.uuid4().hex[:8]}"
        blocks = review_draft_blocks(draft, draft_key)
        await slack.post_ephemeral(
            channel, user_id, draft, blocks=blocks, thread_ts=reply_ts if thread_ts else None
        )
        # Store draft for button handlers (requester can act on their own draft)
        _review_drafts_set(draft_key, draft, user_id)
        logger.info("Review mode: ephemeral draft sent to %s in %s", user_id, channel)
        # Persist conversation (draft counts as a turn)
        if conversation_log and not _is_slack_restricted(session_key):
            await save_conversation_turn_off_loop(
                conversation_log,
                session_key,
                text,
                accumulated,
                source_thread=session_key,
                source_user=user_id,
                agent=_agent,
            )
        return

    if use_slack_stream and stream_ts:
        # Mark last task complete
        if _active_task_id:
            _elapsed = _tool_elapsed_str()
            _cancel_tool_timer()
            _ct = f"{_active_task_title}  {_elapsed}" if _elapsed else _active_task_title
            await _append_task(_active_task_id, _ct, "complete")
        # Flush remaining buffer (bracket_hold excluded — it's either
        # a suppressed OPTIONS tag or an unclosed bracket we drop)
        if stream_buffer:
            stream_buffer, _ = strip_thinking_tags(stream_buffer, strip_whitespace=False)
            await _append_stream(stream_buffer)
        await slack.stop_stream(channel, stream_ts, clean_text or _NO_RESPONSE)

    if use_slack_stream and stream_ts:
        # Rich AI renderer is now locked in by stop_stream above.
        # Only overwrite via chat_update when redaction modified the text —
        # either per-chunk during streaming (_stream_had_redaction), inside the
        # final render (_render_redacted), or caught by the post-decorator scan
        # (exfil_warnings/cred_warnings). The security invariant requires the
        # final visible message reflect the redacted accumulated text; all other
        # cases leave the rich render intact.
        #
        # _render_redacted is the one that catches an ANSI-obfuscated credential:
        # the per-chunk StreamRedactor sees raw chunks and does not strip escapes,
        # so it can miss one that only becomes matchable after normalisation —
        # and the post-decorator scan sees text the render has already cleaned.
        if _stream_had_redaction or _render_redacted or exfil_warnings or cred_warnings:
            fallback_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
            await _safe_final_update(
                slack, channel, stream_ts, fallback_text or _NO_RESPONSE, reply_ts
            )
    elif stream_ts:
        # Legacy fallback path (chat.startStream unavailable): replace the
        # "Thinking…" placeholder with the clean accumulated text.
        final_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
        await _safe_final_update(slack, channel, stream_ts, final_text or _NO_RESPONSE, reply_ts)
    else:
        # No stream was started (e.g. no text chunks) — post the final text directly
        await slack.post_message(channel, clean_text or _NO_RESPONSE, reply_ts)

    # Render reasoning as a condensed, subdued blockquote. When a
    # placeholder was posted above the answer, update it in place so the thread
    # reads reasoning → answer. Otherwise (the stream started before any
    # reasoning arrived) fall back to a post after the answer.
    if thinking_accumulated and _show_thinking:
        # thinking_accumulated is built from raw event text and, unlike the answer
        # stream, has no StreamRedactor upstream -- so this render is its ONLY
        # redaction. Ordering matters most here for that reason.
        thinking_mrkdwn = render_one_for_slack(thinking_accumulated).text
        thinking_mrkdwn, exfil_warnings = redact_exfiltration_urls(thinking_mrkdwn)
        for w in exfil_warnings:
            logger.warning("Exfiltration URL redacted in thinking: %s", w)
        thinking_mrkdwn, cred_warnings = redact_credentials(thinking_mrkdwn)
        for w in cred_warnings:
            logger.warning("Credential redacted in thinking: %s", w)
        # Fold thinking redactions into the SAME per-turn tally as the answer so
        # a single warning covers the turn (see the tally comment above the
        # review-mode branch). Count the fully redacted text before it is
        # condensed -- condensing can truncate, which would drop a placeholder
        # from the count even though the credential was still rewritten.
        _cred_redactions += sum(thinking_mrkdwn.count(tag) for tag in CREDENTIAL_REDACTION_TAGS)
        thinking_block = _condense_thinking(thinking_mrkdwn)
        if thinking_ts:
            try:
                await slack.update_message(channel, thinking_ts, thinking_block)
            except Exception:
                logger.warning("Failed to update thinking message", exc_info=True)
        else:
            for part in split_message(thinking_block):
                try:
                    await slack.post_message(channel, part, reply_ts)
                except Exception:
                    logger.warning("Failed to post thinking message", exc_info=True)
    elif thinking_ts:
        # Placeholder was posted but no reasoning was captured — remove it so
        # the thread isn't left with a dangling "💭 Thinking…".
        try:
            await slack.delete_message(channel, thinking_ts)
        except Exception:
            logger.debug("Failed to delete empty thinking placeholder", exc_info=True)

    # One notice per turn, AFTER the answer (and thinking) have been posted, so it
    # reads below the text it describes. Posted as a SEPARATE threaded message
    # rather than folded into the answer: Slack has already committed the rich
    # answer via stop_stream/chat_update above and the answer text must stay
    # exactly as redacted (never relaxed, never annotated inline). Best-effort --
    # a failed notice must not turn a delivered answer into a failed turn.
    if _cred_redactions > 0:
        try:
            await slack.post_message(
                channel, credential_redaction_notice(_cred_redactions), reply_ts
            )
        except Exception:
            logger.warning("Failed to post credential redaction notice", exc_info=True)

    # Persist the turn BEFORE posting anything that invites an answer to it.
    # The control below carries a staleness token derived from this session's last
    # persisted transcript row, so posting it while this turn is still unwritten
    # would stamp it with the PREVIOUS turn's position -- and these two rows
    # landing straight afterwards would read as the conversation having moved on,
    # refusing the very click the control was posted for.
    #
    # Durability-before-invitation is also right on its own terms: a question
    # about a turn that has no record is not answerable after a restart.
    _skip_writes = _is_slack_restricted(session_key)
    _turn_row_ts: str | None = None
    if conversation_log and not _skip_writes:
        # The per-turn hot path: two appends every turn, so this is where the
        # ~12ms of loop time was paid most often.
        _turn_row_ts = await save_conversation_turn_off_loop(
            conversation_log,
            session_key,
            text,
            accumulated,
            source_thread=session_key,
            source_user=user_id,
            agent=_agent,
        )

    # ── Timing footer ──
    elapsed = time.monotonic() - _t0
    footer_blocks, footer_text = build_timing_footer(elapsed, client)
    # Gated on `options` alone. A top-level Slack message has no ``thread_ts``, so
    # gating on it left every root-thread control untokened -- unprotected on
    # exactly the path a restart strands. ``reply_ts`` is the thread this control
    # actually lands in (``thread_ts or msg_ts``), and ``session_key`` is the
    # conversation that ran this turn: resolving the asker from the thread instead
    # would name whoever owns it at mint time, so a link landing mid-turn would
    # stamp the control with a session that never asked the question.
    #
    # The position comes from the row this turn WROTE, not from re-reading the
    # tail. The session permit is released well above here, so a queued second
    # turn can persist in between; a re-read would then hand this control the
    # NEWER turn's position and a click on it -- by then obsolete -- would read as
    # current and be accepted. Minting from our own row also means no I/O and no
    # await here at all. No row (restricted session, or no log) means no provable
    # position, so the control posts untokened and its clicks are honoured.
    _options_token = (
        mint_options_token(
            cast("DashboardState | None", _dashboard_state),
            session_key,
            _turn_row_ts,
        )
        if options and _turn_row_ts
        else None
    )
    footer_blocks = _append_footer_actions(
        footer_blocks,
        options,
        thread_ts,
        linked_session_key,
        _dashboard_state,
        _options_token,
    )
    _footer_ts = await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
    if options and _footer_ts:
        # Remember this turn's OPTIONS control so the next turn can strike it
        # through once the conversation has moved past the question it asked.
        #
        # Resolved ONCE and reused by the cleanup below. The record and the
        # expiry have to agree on the owner key or they can never pair up: a
        # thread linked to a dashboard mid-turn changes owner, so recording under
        # the key this turn started with files the control where the next turn's
        # expiry will not look. Reading it twice would reopen the same split if a
        # link landed in between.
        _options_owner = sessions.get_session_for_thread(reply_ts) or session_key
        try:

            remember_slack_options(
                cast("DashboardState | None", get_dashboard_state()),
                _options_owner,
                PostedOptions(
                    channel=channel,
                    ts=_footer_ts,
                    choices=tuple(options),
                    blocks=tuple(footer_blocks),
                    text=footer_text,
                ),
            )
        except Exception:
            logger.debug("Failed to record OPTIONS control", exc_info=True)

        # The conversation can move on while post_blocks is in flight -- a queued
        # message can acquire the permit this turn already released and run a whole
        # turn underneath us. The control we just posted would then be asking a
        # question nobody is on any more.
        #
        # Judged by the SAME predicate the click paths use, against the token that
        # went out on the control. That is the whole point of minting it: the
        # question "has this conversation moved past this control" has one answer,
        # computed one way, whether it is asked here or when a click arrives.
        #
        # Cosmetic. A click on a superseded control is refused on its own terms, so
        # failing to strike it through leaves the thread untidy, not unsafe.
        _superseded = _options_token is not None and await options_control_is_stale(
            cast("DashboardState | None", get_dashboard_state()),
            _options_token,
            reply_ts,
        )
        if _superseded:
            try:
                # Narrowed to OUR footer's ts, never a session-wide drain: the
                # very turn that superseded us can finish while we were awaiting
                # post_blocks and record its OWN live control on this session, and
                # draining the slot would strike that newer question through --
                # silencing the one the conversation is now waiting on.
                await expire_slack_options(
                    cast("DashboardState | None", get_dashboard_state()),
                    _options_owner,
                    ts=_footer_ts,
                )
            except Exception:
                logger.debug(
                    "Failed to expire OPTIONS control superseded mid-post",
                    exc_info=True,
                )

    # ── Voice reply (fire-and-forget, non-blocking) ──
    # Triggers when: (a) user has opted in globally or per-thread via !voice,
    # or (b) this message carried transcribed voice input and
    # auto_reply_to_voice is enabled (symmetric voice conversation).
    #
    # ``auto_reply_to_voice`` defaults to ``enabled``'s value at config load
    # (see ``set_orch_cfg``) so users with explicit ``enabled=false`` retain
    # zero-voice behavior, and globally-enabled users automatically get
    # symmetric voice-in/voice-out. Users who want voice ONLY in response to
    # voice memos can set ``auto_reply_to_voice=true`` while leaving
    # ``enabled=false``. See docs/reference/kiro-cli/chat/voice.md.
    voice_auto_reply = had_voice_input and _vc.auto_reply_to_voice
    if _vc.global_enabled or session_key in _vc.sessions or voice_auto_reply:
        if len(accumulated) >= 50:
            _tts_ok = _tts_available(
                provider=_vc.provider,
                piper_binary=_vc.piper_binary,
                piper_model=_vc.piper_model,
            )
            if not _tts_ok:
                # Voice reply requested via any opt-in path (global, per-thread,
                # or voice-auto-reply) but the configured TTS backend isn't
                # available. Post a one-shot ephemeral so the user knows the
                # response fell back to text only — silent fallback is worse
                # UX for users who explicitly opted in.
                if _vc.provider == "piper":
                    hint = (
                        "Install piper (`pip install piper-tts` in a Python "
                        "3.11 venv) and set `voice_reply.piper_model` to your "
                        "voice .onnx file."
                    )
                else:
                    hint = "Run `ada credentials update` and ensure `aws` CLI " "is on PATH."
                if voice_auto_reply:
                    intro = "🔇 Received your voice memo. Replying as text — "
                else:
                    intro = "🔇 Voice reply requested but "
                try:
                    await slack.post_ephemeral(
                        channel,
                        user_id,
                        f"{intro}TTS (provider={_vc.provider}) isn't " f"configured. {hint}",
                    )
                except Exception:
                    logger.debug("Failed to post TTS-unavailable ephemeral", exc_info=True)
            else:
                _vid = _vc.voices.get(session_key, _vc.default_voice)
                _eng = _vc.engines.get(session_key, _vc.default_engine)
                _rate = _vc.rates.get(session_key, _vc.default_rate)
                _pitch = _vc.pitches.get(session_key, _vc.default_pitch)
                asyncio.create_task(
                    _safe_voice_reply(
                        slack,
                        channel,
                        reply_ts,
                        final_text,
                        voice_id=_vid,
                        engine=_eng,
                        rate=_rate,
                        pitch=_pitch,
                    )
                )

    # ── Update task banner with final state ──
    # History was persisted earlier, above the OPTIONS control, so that the
    # control's staleness token names this turn rather than the one before it.
    if conversation_log and not _skip_writes:
        if consolidator and _stop_reason != STOP_REASON_CANCELLED:
            consolidator.maybe_consolidate(session_key)

    # ── Bidirectional sync: mirror to dashboard if routed to a dashboard session ──
    if linked_session_key and _dashboard_state and accumulated and not _skip_writes:
        try:
            ds = _dashboard_state
            slot_name = linked_session_key.removeprefix("dashboard:")
            slot = getattr(ds, "_slots", {}).get(slot_name)
            if slot:
                slot.append("user", text, "msg msg-u")
                slot.append("assistant", accumulated, "msg msg-a")
                if slot._on_message:
                    slot._on_message(
                        slot.key, {"role": "user", "content": text, "cls": "msg msg-u"}
                    )
                    slot._on_message(
                        slot.key, {"role": "assistant", "content": accumulated, "cls": "msg msg-a"}
                    )
                ds.push_slots_update()  # type: ignore[attr-defined]
        except Exception:
            logger.debug("Failed to mirror Slack message to dashboard", exc_info=True)
    # ── Auto-title Slack thread (fire-and-forget) ──
    # Claim-early-unclaim-on-failure pattern: ``try_claim`` checks and marks in one
    # synchronous step, so concurrent messages (and the transport path, which
    # claims through the same shared tracker) cannot both fire a task. If the
    # background task fails or returns SKIP, it unclaims the key so the next
    # message retries. A message arriving between claim and unclaim is
    # intentionally skipped (no duplicate).
    if not _had_error and not _skip_writes and auto_title.try_claim(session_key):
        track_background_task(
            asyncio.create_task(
                _maybe_auto_title_slack(
                    slack, sessions, channel, session_key, conversation_log, text, accumulated
                )
            )
        )


# ── Slack thread auto-title ─────────────────────────────────────────────
#
# The turn, the claim tracker, the tool-free stream, the prompt and the
# title-cleaning rules all live in ``messaging.auto_title``. Slack supplies the
# one thing that is genuinely per-channel — renaming the Slack thread itself.

_get_auto_title_lock = auto_title.get_lock
_build_title_prompt = auto_title.build_title_prompt


async def _maybe_auto_title_slack(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    user_text: str,
    assistant_text: str,
) -> None:
    """Generate and set a Slack thread title after the first response."""

    async def _set_thread_title(title: str) -> None:
        await slack.set_thread_title(channel, session_key, title)

    await auto_title.maybe_auto_title(
        sessions,
        conversation_log,
        session_key,
        user_text,
        assistant_text,
        source="slack",
        resources=f"{channel}:{session_key}",
        set_channel_title=_set_thread_title,
    )


async def _reject_orphaned_tool(provider: LLMProvider, request_id: "str | int") -> None:
    """Reject a pending ACP permission request that we can no longer surface.

    Both the pre-approval stream-prep and the approval-prompt post happen BEFORE
    the permission is answered; if either raises, the ACP request would be left
    unanswered and the agent subprocess wedges forever (every later turn blocks
    behind it). Callers invoke this on failure, then re-raise. Swallows any
    reject failure (best-effort) so the original error still propagates.
    """
    try:
        await provider.reject_tool(request_id)
    except Exception:
        logger.warning("Failed to reject orphaned tool %s", request_id, exc_info=True)


class _LinkedApprovalEvent:
    """Minimal event shim for :func:`_build_approval_blocks`.

    The dashboard's permission event (``AcpEvent``) and the Slack-native
    ``LLMEvent`` have different shapes, so adapt the few fields the block
    builder reads: ``request_id``, ``title``, ``tool_input``, ``tool_purpose``.
    """

    __slots__ = ("request_id", "title", "tool_input", "tool_purpose")

    def __init__(self, request_id: str | int, title: str, tool_input: str = "") -> None:
        self.request_id = request_id
        self.title = title
        self.tool_input = tool_input
        self.tool_purpose = ""


async def post_linked_approval(
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    request_id: str | int,
    session_key: str,
    title: str,
    tool_input: str = "",
) -> str | None:
    """Mirror a dashboard tool-approval prompt into a linked Slack thread.

    Posts Approve / Reject buttons threaded under ``thread_ts`` and registers a
    :class:`_LinkedApproval` keyed by ``channel:ts`` so a button click resolves
    the dashboard slot's approval future (see :func:`handle_interaction`).

    Returns the Slack message ts on success, or ``None`` if the post failed.
    The caller (dashboard ``_run_chat``) treats ``None`` as "delivery failed"
    and surfaces it rather than silently parking on an unanswerable prompt.

    Trust is intentionally omitted (``is_dm=False``): trust for a linked slot is
    a dashboard-side mode, not wired through this path. Approve / Reject are
    sufficient to guarantee the prompt is answerable from Slack.
    """
    # title / tool_input are LLM-generated (the tool-use request). Slack is an
    # external surface, so scrub them the same way every other outbound LLM
    # string is scrubbed before posting — the dashboard path already redacts
    # these via perm_meta, but this Slack mirror must do its own redaction.
    title, _ = redact_exfiltration_urls(title)
    title, _ = redact_credentials(title)
    tool_input, _ = redact_exfiltration_urls(tool_input)
    tool_input, _ = redact_credentials(tool_input)
    event = _LinkedApprovalEvent(request_id, title, tool_input)
    # _build_approval_blocks is typed for AcpEvent but only reads the four
    # attributes the shim provides (request_id/title/tool_input/tool_purpose).
    blocks = _build_approval_blocks(event, is_dm=False)  # type: ignore[arg-type]
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        logger.warning(
            "Failed to post linked approval prompt to Slack (session=%s req=%s)",
            session_key,
            request_id,
            exc_info=True,
        )
        return None
    _linked_approvals[f"{channel}:{approval_ts}"] = _LinkedApproval(request_id, session_key)
    return approval_ts


def resolve_linked_approval(channel: str, approval_ts: str) -> None:
    """Drop a linked-approval registry entry (after the dashboard resolved it)."""
    _linked_approvals.pop(f"{channel}:{approval_ts}", None)


async def _request_approval(
    slack: SlackClientOps,
    provider: LLMProvider,
    channel: str,
    thread_ts: str,
    event: LLMEvent,
    session_key: str = "",
    is_dm: bool = True,
) -> str:
    """Post approval buttons, wait for click, return 'approved' or 'rejected'."""
    blocks = _build_approval_blocks(event, is_dm=is_dm)
    # If posting the approval prompt fails, the ACP permission request would
    # otherwise be left unanswered — the subprocess blocks forever and every
    # later turn wedges behind it. Reject the tool before re-raising so the
    # turn unblocks and the caller's error path can run.
    try:
        approval_ts = await slack.post_blocks(
            channel, blocks, "Manual approval required", thread_ts
        )
    except Exception:
        await _reject_orphaned_tool(provider, event.request_id)
        raise

    key = f"{channel}:{approval_ts}"
    pending = _PendingApproval(provider, event.request_id, session_key)
    _pending_approvals[key] = pending

    try:
        outcome = await asyncio.wait_for(pending.future, timeout=_APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        outcome = _OUTCOME_REJECTED
        await provider.reject_tool(event.request_id)
        Stats().inc_tool_denial()
    finally:
        _pending_approvals.pop(key, None)

    try:
        await slack.delete_message(channel, approval_ts)
    except Exception:
        status = "✅ Approved" if outcome == _OUTCOME_APPROVED else "🚫 Rejected"
        title_safe, _ = redact_exfiltration_urls(event.title)
        title_safe, _ = redact_credentials(title_safe)
        await _safe_update(slack, channel, approval_ts, f"🔐 *{title_safe}* — {status}")

    return outcome


async def handle_interaction(
    channel: str,
    msg_ts: str,
    action_id: str,
    user_id: str = "",
    thread_ts: str = "",
    slack: SlackClientOps | None = None,
    sessions: SessionManager | None = None,
) -> str | None:
    """Handle a Block Kit button click for tool approval.

    Supports four actions:
    - approve_tool: approve this one tool call
    - trust_tool: auto-approve all tools for this session (thread)
    - reject_tool: reject this tool call

    Security: rejects non-owner clicks. Trust requires DM channel
    (verified via conversations.info by the gateway caller).
    """

    # Deny-by-default: reject unless positively confirmed as allowed
    if not user_id or not is_allowed_user(user_id):
        logger.warning(
            "Rejecting interactive action from unauthorized user %s (action=%s)", user_id, action_id
        )
        sel().log_api_access(
            caller=user_id or "unknown",
            operation="slack.interactive.approval",
            outcome="denied",
            source="slack",
            resources=action_id,
            error="unauthorized user",
        )
        return None

    key = f"{channel}:{msg_ts}"

    # Linked-dashboard-slot approval: the dashboard's _run_chat owns the ACP
    # answer (it is parked on the slot's approval future). Resolve ONLY that
    # future here via state.resolve_approval — do NOT call approve_tool/reject
    # (that would answer the JSON-RPC request twice). Trust is not offered on
    # this path, so treat anything that isn't an explicit reject as approve.
    linked_entry = _linked_approvals.get(key)
    if linked_entry is not None:
        approved = action_id != _ACTION_REJECT
        resolved = False
        if _dashboard_state is not None and hasattr(_dashboard_state, "resolve_approval"):
            try:
                resolved = bool(
                    _dashboard_state.resolve_approval(str(linked_entry.request_id), approved)  # type: ignore[attr-defined]
                )
            except Exception:
                logger.warning(
                    "Failed to resolve linked approval (req=%s)",
                    linked_entry.request_id,
                    exc_info=True,
                )
        _linked_approvals.pop(key, None)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval_linked",
            outcome="allowed" if approved else "denied",
            source="slack",
            resources=linked_entry.session_key,
            error="" if resolved else "future_not_found",
        )
        if approved:
            Stats().inc_tool_approval()
        else:
            Stats().inc_tool_denial()
        return _ACTION_APPROVE if approved else _ACTION_REJECT

    pending = _pending_approvals.get(key)
    if not pending:
        # Approval already resolved (approved/rejected/timed out).
        # For trust clicks, still set trust using the thread as session key.
        # Replicate session_key derivation from handle_message: thread_ts,
        # then check for linked dashboard session override.
        if action_id == _ACTION_TRUST and thread_ts:
            if not is_allowed_user(user_id):
                logger.warning("Rejecting late trust click from non-allowed user %s", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="unauthorized user",
                )
                return None
            # Verify clicking user owns this thread (prevents privilege escalation)
            if not slack:
                logger.warning(
                    "Rejecting late trust click: cannot verify thread ownership (no slack client)"
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="no_slack_client",
                )
                return None
            try:
                msgs = await slack.fetch_thread_replies(channel, thread_ts, limit=1)
                thread_owner = msgs[0].get("user", "") if msgs else ""
            except Exception:
                logger.warning("Failed to verify thread ownership for %s", thread_ts, exc_info=True)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="thread_ownership_check_failed",
                )
                return None
            if not thread_owner or thread_owner != user_id:
                logger.warning("Rejecting late trust click: user %s is not thread owner", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="not_thread_owner",
                )
                return None
            # Imported at call time on purpose: tests patch
            # ``kiro_crew.session.SessionMap`` to drive the fail-closed path, and
            # only a call-time rebind observes that patch.
            from kiro_crew.session import SessionMap

            session_key = thread_ts
            try:
                linked = SessionMap().get_session_for_thread(thread_ts)
                if linked:
                    session_key = linked
            except Exception:
                logger.warning(
                    "SessionMap lookup failed for thread %s; refusing to grant trust",
                    thread_ts,
                    exc_info=True,
                )
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_late",
                    outcome="denied",
                    source="slack",
                    error="session_map_lookup_failed",
                )
                return None
            # Through the shared grant, which owns BOTH halves: the in-memory
            # mapping the driver reads and the parent approval_policy a subagent
            # reads (see subagent.py). Poking the container directly is what let a
            # revoke clear one half and leave the other, so the two are no longer
            # separable at a call site.
            add_trusted_session(session_key, sessions)
            logger.info("Trust mode ON (late click) for session %s", session_key)
            sel().log_api_access(
                caller=user_id,
                operation="slack.interactive.trust_late",
                outcome="allowed",
                source="slack",
                resources=session_key,
            )
            return _ACTION_TRUST
        else:
            logger.warning("No pending approval for %s", key)
            sel().log_api_access(
                caller=user_id or "unknown",
                operation="slack.interactive.approval",
                outcome="denied",
                source="slack",
                resources=key,
                error="no_pending_approval",
            )
        return None

    if action_id in (_ACTION_APPROVE, _ACTION_TRUST):
        # Set trust state BEFORE approving (so subsequent tools auto-approve)
        if action_id == _ACTION_TRUST:
            if not is_allowed_user(user_id):
                logger.error("Rejecting trust escalation from non-allowed user %s", user_id)
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.interactive.trust_denied",
                    outcome="denied",
                    source="slack",
                    resources=pending.session_key or "",
                    error="non-allowed user",
                )
                if not pending.future.done():
                    pending.future.set_result(_OUTCOME_REJECTED)
                del _pending_approvals[key]
                return _ACTION_REJECT
            elif pending.session_key:
                add_trusted_session(pending.session_key, sessions)
                logger.info("Trust mode ON for session %s", pending.session_key)
            else:
                logger.warning(
                    "No session_key on pending approval %s; approving without trust", key
                )
        if pending.provider:
            await pending.provider.approve_tool(pending.request_id)
        if not pending.future.done():
            pending.future.set_result(_OUTCOME_APPROVED)
        Stats().inc_tool_approval()
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval",
            outcome="allowed",
            source="slack",
            resources=action_id,
        )
    else:
        if pending.provider:
            await pending.provider.reject_tool(pending.request_id)
        if not pending.future.done():
            pending.future.set_result(_OUTCOME_REJECTED)
        sel().log_api_access(
            caller=user_id,
            operation="slack.interactive.approval",
            outcome="denied",
            source="slack",
            resources=action_id,
        )

    del _pending_approvals[key]
    return action_id


def _build_approval_blocks(event: LLMEvent, is_dm: bool = True, source: str = "") -> list[dict]:
    """Build Block Kit blocks for tool approval prompt.

    Args:
        event: The permission-request event from the LLM provider.
        is_dm: True when posting to a DM (adds Trust button).
        source: Optional label for background agents (e.g. "subagent",
            "cron").  Prefixed to the header so users can tell main-agent
            approvals apart from background ones.

    Shows the full command text (from tool_input) in a code block so users
    can see exactly what will run before approving.  Falls back to the
    truncated title when tool_input is unavailable.

    In DMs: Approve / Trust / Reject
    In group channels: Approve / Reject only (Trust excluded
    to limit blast radius — it escalates permissions for the session).
    YOLO is owner-only via ``!yolo on`` command — no button.
    """
    # Slack Block Kit requires button `value` to be a string. ACP backends
    # (e.g. claude-agent-acp) issue integer JSON-RPC request ids, so coerce —
    # an int value makes Slack reject the whole post with `invalid_blocks`.
    # The interactive handler matches on channel:msg_ts and acts on the stored
    # `_PendingApproval.request_id`, so the button value itself is display-only.
    req_value = str(event.request_id)
    buttons: list[dict] = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Approve"},
            "style": "primary",
            "action_id": _ACTION_APPROVE,
            "value": req_value,
        },
    ]
    if is_dm:
        buttons.append(
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Trust session"},
                "action_id": _ACTION_TRUST,
                "value": req_value,
            },
        )
    buttons.append(
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "Reject"},
            "style": "danger",
            "action_id": _ACTION_REJECT,
            "value": req_value,
        },
    )

    blocks: list[dict] = []

    tag = f"[{source}] " if source else ""
    title_safe, _ = redact_exfiltration_urls(event.title)
    title_safe, _ = redact_credentials(title_safe)
    footer = f":lock: {tag}*{title_safe}*"
    if event.tool_purpose:
        purpose, _ = redact_exfiltration_urls(event.tool_purpose)
        purpose, _ = redact_credentials(purpose)
        footer += f" — {purpose}"

    # When full tool_input is available, show a simple header and the
    # complete command in a code block below.
    # When tool_input is missing, fall back to the truncated title.
    if event.tool_input:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🔐 *{tag}Tool approval requested:*"},
            },
        )
        # Security: scan for exfiltration URLs and credentials before posting
        sanitized, _ = redact_exfiltration_urls(event.tool_input)
        sanitized, _ = redact_credentials(sanitized)
        # Truncate with marker if exceeds Slack limit
        if len(sanitized) > _SLACK_SECTION_TEXT_LIMIT:
            detail = (
                sanitized[: _SLACK_SECTION_TEXT_LIMIT - len(_TRUNCATION_MARKER)]
                + _TRUNCATION_MARKER
            )
        else:
            detail = sanitized
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```{detail}```"},
            },
        )

    blocks.append({"type": "actions", "elements": buttons})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": footer}]})
    return blocks


def _handle_spawn_command(text: str, manager: SubagentManager, session_key: str = "") -> str | None:
    """Intercept spawn/bg keyword commands. Returns reply or None."""
    return spawn_command_reply(text, manager, session_key)


async def _handle_cron_command(
    text: str, cron_service: CronService, channel: str, thread_ts: str, user_id: str = ""
) -> str | None:
    """Handle cron keyword commands. Returns reply or None.

    Async so the store mutators (remove/pause/resume) run through the
    event-loop-safe ``*_async`` variants instead of parking the Slack gateway
    loop on the store lock; a contended store yields a "busy, retry" reply
    rather than a stall.

    ``user_id`` is the Slack caller, threaded through so the destructive
    branches can attribute their SEL audit events to the human who issued
    the command (per-caller identity, matching the dashboard/MCP/CLI paths).
    """
    # ``source``/``caller`` carry #5428's attribution into the shared remove-all
    # audit: the hoist moved the audit's home, not its contract.
    return await cron_command_reply(text, cron_service, source="slack", caller=user_id)


async def _handle_run_command(
    text: str,
    runner: TaskRunner,
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    *,
    session_key: str = "",
) -> str | None:
    """Intercept 'run <path>' keyword commands. Returns reply or None.

    ``slack`` / ``channel`` / ``thread_ts`` are unused and were already unused
    before the reply text was hoisted; they stay because this is the positional
    shape ``maybe_handle_keyword_command`` and several suites call.

    ``session_key`` is what lets a task that later blocks on an approval report back
    to the conversation the operator is watching, instead of only to the owner DM.
    Keyword-only with a default so the ~25 existing positional call sites are
    unchanged; omitting it reproduces the old owner-DM-only behaviour exactly.
    """
    return await task_command_reply(text, runner, session_key=session_key)


async def _handle_sessions_command(
    cmd_text: str,
    slack: SlackClientOps,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    *,
    sessions: SessionManager | None = None,
) -> None:
    """Handle the ``sessions`` keyword in DMs.

    Delegates to
    :func:`kiro_crew.slack.sessions_view._collect_recent_sessions_off_loop`
    and :func:`kiro_crew.slack.sessions_view._build_sessions_blocks` so the
    keyword, the ``/<command> sessions`` slash command, and the App Home Tab
    all render the same Block Kit content with the same Resume button wiring.
    """
    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline.
    # Mirrors the slash and Home Tab error-path patterns.
    try:
        rows = await _collect_recent_sessions_off_loop(sessions, limit=_SESSIONS_DEFAULT_LIMIT)
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=session_key,
            operation="slack.sessions_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("sessions keyword: collector failed for session_key %s", session_key)
        await slack.post_message(channel, "_Sessions unavailable._", reply_ts)
        return

    sel().log_api_access(
        caller=session_key,
        operation="slack.sessions_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await slack.post_message(channel, "_No recent sessions._", reply_ts)
        return

    blocks = _build_sessions_blocks(rows)
    await slack.post_blocks(channel, blocks, "Recent sessions:", reply_ts)


async def _safe_update(slack: SlackClientOps, channel: str, ts: str, text: str) -> None:
    """Update a Slack message, truncating if too long.

    Used for progressive streaming edits — truncation is fine here since
    the final message uses _safe_final_update which splits instead.
    """
    text, _ = redact_exfiltration_urls(text)
    if len(text) > SLACK_MSG_LIMIT:
        text = text[:SLACK_MSG_LIMIT] + TRUNCATION_NOTICE
    try:
        await slack.update_message(channel, ts, text)
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)


async def _safe_final_update(
    slack: SlackClientOps, channel: str, ts: str, text: str, thread_ts: str | None = None
) -> None:
    """Final message update — splits into multiple messages if too long."""
    text, _ = redact_exfiltration_urls(text)
    parts = split_message(text)
    # First part updates the existing streaming message
    try:
        await slack.update_message(channel, ts, parts[0])
    except Exception:
        logger.debug("Failed to update message %s", ts, exc_info=True)
    # Overflow parts posted as follow-up messages in the same thread
    for part in parts[1:]:
        try:
            await slack.post_message(channel, part, thread_ts)
        except Exception:
            logger.debug("Failed to post continuation message", exc_info=True)
