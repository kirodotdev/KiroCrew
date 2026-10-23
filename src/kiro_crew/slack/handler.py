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
import contextlib
import json  # noqa: F401 - read by the owners
import logging
import os  # noqa: F401 - read by the owners
import re
import time
import uuid  # noqa: F401 - read by the owners
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState

from kiro_crew import name_grant, runtime_death
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import (
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
)
from kiro_crew.agent_discovery import (
    SensitiveAgentSpecPathError,
    agent_spec_stems,
    project_agent_files,
    project_agent_name,
    read_agent_spec_strict,
)
from kiro_crew.agent_sdk import CONTEXT_EVENT_COMPACTION
from kiro_crew.agent_spec_format import is_markdown_spec, iter_agent_spec_files
from kiro_crew.agent_switch_command import (  # noqa: F401 - read by the owners
    agent_switch_target,
)
from kiro_crew.config.loader import (  # noqa: F401 - read by the owners
    ACTIVATION_REVIEW,
    KIRO_CLI_BUILTIN_AGENTS,
    ConfigReadError,
    KiroCrewConfig,
    config_path,
    read_config_text,
    update_config_locked,
)
from kiro_crew.config.paths import kiro_agents_dir, peek_data_home
from kiro_crew.constants import (  # noqa: F401 - read by the owners
    DENY_CAUSE_APPROVAL_TIMEOUT,
    DENY_CAUSE_POLICY,
    STEER_NOTICE_BOUND_SECS,
    is_control_tag_tail,
    strip_control_comments,
)
from kiro_crew.context import (
    ContextBuilder,
    build_cancelled_turn_preamble,
    build_session_replay,
    session_store_for_turn,
    window_for_provider_client,
)
from kiro_crew.cron import CronService
from kiro_crew.dashboard.chat_utils import (  # noqa: F401 - read by the owners
    effective_session_key,
    expire_slack_options,
    mint_options_token,
    options_control_is_stale,
    remember_slack_options,
    run_config_write,
)
from kiro_crew.dashboard.state import (  # noqa: F401 - read by the owners
    append_and_surface,
    load_window_off_loop,
)
from kiro_crew.dashboard.turn_dispatch import tool_approval_timeout_secs
from kiro_crew.deny_notice import steer_refusal_notice
from kiro_crew.executors import run_in_embed_pool, run_in_tool_gate_pool
from kiro_crew.history import (  # noqa: F401 - read by the owners
    HUMAN_TURN_META_KEY,
    ConversationLog,
    HistoryConsolidator,
)
from kiro_crew.hooks import (
    HOOK_REPLY,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    ToolHookResult,
    event_is_spawn_run,
    hook_gate_kwargs,
    safe_read_file_bytes,
)
from kiro_crew.llm_helpers import (
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    SESSION_NOT_FOUND_RETRY_NOTICE,
    acp_error_is_session_not_found,
    acp_error_needs_fresh_credentials,
    credential_give_up_text,
    record_interaction_event,
    save_conversation_turn_off_loop,
)
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import auto_title, privacy_mode, turn_ceiling
from kiro_crew.messaging.commands import (  # noqa: F401 - read by the owners
    compact_unsupported_backend,
    compact_unsupported_reply,
    cron_command_reply,
    note_user_stop,
    spawn_command_reply,
    task_command_reply,
)
from kiro_crew.messaging.dispatch import (
    admit_inbound_callback,
    await_replay_gap,
    consume_reinjection,
    rearm_reinjection,
    rollback_skill_bodies,
    session_stop_generation,
    stop_reason_landed,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import canonical_key
from kiro_crew.messaging.renderer import count_redaction_tags, redaction_notice
from kiro_crew.messaging.session_trust import _trusted_sessions as _shared_trusted_sessions
from kiro_crew.messaging.session_trust import (  # noqa: F401 - read by the owners
    add_trusted_session as _add_trusted_session,
)
from kiro_crew.messaging.session_trust import (  # noqa: F401 - read by the owners
    clear_trusted_sessions,
    is_session_trusted,
)
from kiro_crew.messaging.turn_ceiling import TurnCeilingExceeded
from kiro_crew.permission_floor import OUTCOME_REJECTED_TRANSPORT_FLOOR
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
from kiro_crew.safety_override import (  # noqa: F401 - read by the owners
    SafetyOverride,
    apply_config_duration,
    describe_grant_lifetime,
    describe_new_grant,
    grant_declared_yolo,
    safety_override,
    yolo_policy_permits,
)
from kiro_crew.security import (  # noqa: F401 - read by the owners
    StreamRedactor,
    is_sensitive_path,
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel
from kiro_crew.session import (
    _CIRCUIT_BREAKER_THRESHOLD,
    SessionClosingError,
    SessionManager,
)
from kiro_crew.session_lifecycle import (  # noqa: F401 - read by the owners
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.slack.blocks import (  # noqa: F401 - read by the owners
    build_working_blocks,
    deprecation_warning_block,
)
from kiro_crew.slack.channel_crew import resolve_channel_crew  # noqa: F401 - read by the owners
from kiro_crew.slack.client import SlackClientOps
from kiro_crew.slack.format import (  # noqa: F401 - read by the owners
    SLACK_MSG_LIMIT,
    TRUNCATION_NOTICE,
    _convert_tables,
    extract_options,
    is_wait_identity,
    render_one_for_slack,
    split_message,
    strip_thinking_tags,
)
from kiro_crew.slack.handler_runtime import access as _owner_access
from kiro_crew.slack.handler_runtime import agent_names as _owner_agent_names
from kiro_crew.slack.handler_runtime import approvals as _owner_approvals
from kiro_crew.slack.handler_runtime import commands as _owner_commands
from kiro_crew.slack.handler_runtime import delivery as _owner_delivery
from kiro_crew.slack.handler_runtime import dispatch as _owner_dispatch
from kiro_crew.slack.handler_runtime import finalize as _owner_finalize
from kiro_crew.slack.handler_runtime import inbound as _owner_inbound
from kiro_crew.slack.handler_runtime import permission_ladder as _owner_permission_ladder
from kiro_crew.slack.handler_runtime import reactions as _owner_reactions
from kiro_crew.slack.handler_runtime import stream as _owner_stream
from kiro_crew.slack.handler_runtime import turn as _owner_turn
from kiro_crew.slack.handler_runtime import turn_context as _owner_turn_context
from kiro_crew.slack.handler_runtime import voice as _owner_voice
from kiro_crew.slack.handler_runtime.access import (  # noqa: F401
    _reload_orch_cfg,
    add_trusted_session,
    adopt_slack_config,
    cancel_background_tasks,
    copy_slack_fields,
    disable_yolo,
    enable_yolo_with_ttl,
    get_dashboard_state,
    get_orch_cfg,
    is_allowed_user,
    is_open_channel,
    is_owner,
    is_slack_session_trusted,
    is_tracked_channel,
    is_yolo_mode,
    set_allowed_users,
    set_dashboard_state,
    set_open_channels,
    set_orch_cfg,
    set_owner_id,
    set_tracking_channels,
    set_yolo_mode,
    slack_cfg,
    track_background_task,
)
from kiro_crew.slack.handler_runtime.agent_names import (  # noqa: F401
    _discover_project_agents,
    _list_all_agent_names,
    _resolve_agent_name,
    _resolve_cc_agent_name,
)
from kiro_crew.slack.handler_runtime.approvals import (  # noqa: F401
    _build_approval_blocks,
    _grant_late_trust,
    _grant_linked_trust,
    _linked_slots_for,
    _linked_trust_grantable,
    _LinkedApproval,
    _LinkedApprovalEvent,
    _PendingApproval,
    _resolve_linked_click,
    post_linked_approval,
    resolve_linked_approval,
)
from kiro_crew.slack.handler_runtime.commands import (  # noqa: F401
    _bang_agent,
    _bang_allowlist,
    _bang_channel,
    _bang_dashboard,
    _bang_link_to_dashboard,
    _bang_project,
    _bang_stop,
    _bang_thread_agent,
    _bang_title,
    _bang_voice,
    _bang_yolo,
    _handle_compact_command,
    _handle_cron_command,
    _handle_run_command,
    _handle_slash_command,
    _handle_spawn_command,
    _is_sessions_keyword,
    _route_bang_command,
)
from kiro_crew.slack.handler_runtime.delivery import (  # noqa: F401
    _deliver_answer,
)
from kiro_crew.slack.handler_runtime.dispatch import (  # noqa: F401
    _dispatch_before_turn,
    _handle_sessions_command,
    maybe_handle_keyword_command,
)
from kiro_crew.slack.handler_runtime.finalize import (  # noqa: F401
    _append_footer_actions,
    _maybe_auto_title_slack,
    _mirror_to_dashboard,
    _post_review_draft,
    _review_drafts_get,
    _review_drafts_pop,
    _review_drafts_set,
    build_timing_footer,
)
from kiro_crew.slack.handler_runtime.inbound import (  # noqa: F401
    _apply_privacy_mode,
    _get_agent_for_session,
    _get_default_agent,
    _hydrate_conv_flags,
    _hydrate_conv_header,
    _hydrate_thread_overrides,
    _is_slack_restricted,
    _persist_channel_config,
    _read_thread_overrides,
    _set_default_agent,
    adopt_default_agent,
    maybe_apply_privacy_modifiers,
    maybe_route_linked_thread,
)
from kiro_crew.slack.handler_runtime.permission_ladder import (  # noqa: F401
    _answer_permission_request,
    _reject_orphaned_tool,
    _request_approval,
    _screen_tool_call,
    _should_auto_approve_spawn,
    _steer_host_deny,
    handle_interaction,
)
from kiro_crew.slack.handler_runtime.reactions import (  # noqa: F401
    StatusReactionController,
    _add_phase_reaction,
    _tool_to_phase,
    phase_emojis,
    refresh_phase_emojis,
)
from kiro_crew.slack.handler_runtime.stream import (  # noqa: F401
    _add_to_rich_text,
    _AnswerStream,
    _at_tag_line_start,
    _blocks_with_run,
    _comment_hold_is_protocol,
    _filter_options_brackets,
    _resolve_comment_hold,
    _safe_final_update,
    _safe_update,
    _ts_before,
)
from kiro_crew.slack.handler_runtime.turn import (  # noqa: F401
    _run_turn,
    _SlackTurn,
)
from kiro_crew.slack.handler_runtime.turn_context import (  # noqa: F401
    _thread_context,
    _thread_meta_fallback,
)
from kiro_crew.slack.handler_runtime.voice import (  # noqa: F401
    _reply_by_voice,
    _safe_voice_reply,
    _str_or_default,
    load_voice_reply_config,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.sessions_view import (  # noqa: F401 - read by the owners
    SESSIONS_INCLUDE_ENDED_ARGS,
    _build_sessions_blocks,
    _collect_recent_sessions_off_loop,
    _message_surface_limit,
    sessions_include_ended,
)
from kiro_crew.slack.thread_parent import (  # noqa: F401 - read by the owners
    fetch_thread_parent,
    has_prior_turns,
    is_slack_born,
    parent_prompt_text,
    record_thread_parent,
)
from kiro_crew.slack.thread_replies import (  # noqa: F401 - read by the owners
    ThreadReplies,
    has_noted_turn,
    note_turn,
    replies_since_last_turn,
)
from kiro_crew.start_priority import StartPriority
from kiro_crew.stats import Stats
from kiro_crew.subagent import SubagentManager
from kiro_crew.task import Task
from kiro_crew.taskrunner import TaskRunner
from kiro_crew.tool_gate_busy import hook_gate_busy
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    DEFAULT_PROVIDER,
    PROVIDER_POLLY,
    PROVIDER_SYSTEM,
)
from kiro_crew.voice_reply import is_available as _tts_available  # noqa: F401 - read by the owners
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    resolve_configured_provider,
    validated_config_string,
    validated_language_code,
)
from kiro_crew.voice_reply import voice_reply as _voice_reply_fn  # noqa: F401 - read by the owners

logger = logging.getLogger(__name__)


def _display_redactor(text: str) -> str:
    """Both outbound redactors as one callable, in the canonical order.

    The twin of the renderer's ``_redact_all``: exfiltration URLs then
    credentials. Passed to ``redact_for_display`` so a fallback egress on this
    path is scanned against what Slack renders, matching the answer path rather
    than a weaker literal-only scrub.
    """
    text, _ = redact_exfiltration_urls(text)
    return redact_credentials(text)[0]


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


# Min interval between Slack message edits (avoid rate limits)
_EDIT_INTERVAL = 1.0

# Approve/reject window: agent.tool_approval_timeout_secs via the shared
# resolver, unless a test pins _APPROVAL_TIMEOUT.
_APPROVAL_TIMEOUT: float | None = None
# Upper bound on the best-effort in-band deny notice steered into the running
# turn before an expired approval prompt is rejected. The shared constant, so
# this arm, the dashboard chat runner and the messaging TurnDriver cannot drift:
# an unbounded await on a backpressured ACP stdin could stall the reject that
# unblocks the turn. Module-level so a test can shorten it.
_STEER_NOTICE_BOUND_SECS = STEER_NOTICE_BOUND_SECS

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
#: First chunk of a REPLACEMENT stream opened by ``_AnswerStream.rotate``
#: (``handler_runtime/stream.py``). A rotation abandons the message the reader is
#: already watching and continues the same answer in a new one, so without this the
#: thread reads as a stalled reply followed by an unexplained second reply. Slack
#: appends stream chunks and never replaces them, so the text already shown stays in the
#: abandoned message — this line is what tells the reader the two belong together.
_STREAM_CONTINUED = "_(continued)_\n\n"

#: Appended to a stream that lost real answer text Slack would not accept. A
#: refused append always attempts a rotation, so a for-good loss reaches finalize
#: with the turn's text spread over more than one message: overwriting the message
#: the reader is looking at would duplicate what the abandoned one already shows,
#: and characters lost before a wait boundary are in nothing the turn still holds.
#: The loss is disclosed rather than restated, because a reader who is told can
#: ask again, while a reader who is told nothing reads a complete-looking answer
#: with a hole in it.
#:
#: Shared by both stream paths so the two disclose a loss in the same words. It
#: lives here because ``renderer`` imports from this module, not the reverse.
DELIVERY_DEBT_NOTICE = (
    "\n\n_[Part of this reply did not reach Slack and could not be restored here. "
    "Ask for it again to see the missing text.]_"
)

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
# Strong references to teardown-time orphan-reject tasks (the CancelledError
# arm of _request_approval): asyncio holds tasks weakly, and these are created
# exactly while the loop is unwinding.
_orphan_rejects: "set[asyncio.Task[bool]]" = set()

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


# Import-time, so it must not CREATE anything: ``KiroCrewConfig.load()`` resolves
# ``config_dir()``, which mkdirs the data home, and this module is imported by
# every test collector and by read-only tools. With no ``config.json`` on disk
# there are no overrides to read, so peek first and load only when the file --
# and therefore the directory -- already exists.
try:
    if (peek_data_home() / "config.json").is_file():
        _overrides = KiroCrewConfig.load().slack.reactions
    else:
        _overrides = {}
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
    # TTS provider. Defaults to the LOCAL provider, matching
    # ``voice_reply.DEFAULT_PROVIDER``. Defaulting to "polly" here would mean
    # enabling voice reply without naming a provider silently sends text to a
    # paid AWS service under whatever the ambient credential chain resolves to.
    # Sourced from the single constant so the two cannot drift.
    provider: str = DEFAULT_PROVIDER
    # Built-in-engine voice. Empty means the OS default voice, which is the
    # right answer whenever the host language matches the reply language.
    system_voice: str = ""
    # BCP-47 language for spoken replies, handed to edition TTS providers. Empty
    # means each provider's own default language.
    language_code: str = ""
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

# Retries granted to a Slack turn abandoned after a TRANSIENT compaction failure
# (a throttled or 5xx'd summarization call). Per message: the replay is a nested
# ``handle_message`` call carrying the attempt number, so the budget travels
# with the message and needs no per-thread state. Same count as the dashboard's
# _COMPACTION_FAILED_RETRIES and for the same reason: a throttle still firing
# after two session resets is not clearing, and every attempt costs the
# summarization call again.
_COMPACTION_FAILED_RETRIES = 2

# Posted to the thread when the abandoned message is about to be replayed. Sent
# directly, never through the turn's own reply path: the abandoned attempt
# persists nothing and mirrors nothing, so the conversation log records the
# message once, with the reply the replay produces.
_COMPACTION_RETRY_NOTICE = "⟳ Compaction failed — retrying…"

# Terminals the session handle synthesizes for a turn the backend never
# finished: its own turn ceiling ("timeout") and a stale turn its probe
# confirmed wedged. The runtime still reads alive after either one, so the turn
# is failed, the thread is told, and the session is reset so the next message
# starts a fresh runtime.
_UNFINISHED_STOP_REASONS = frozenset({"timeout", STOP_REASON_STALE_RECOVER})
_TIMEOUT_REPLY = "⏱️ Request timed out. Please try again."


@dataclass(frozen=True)
class _CompactionReplay:
    """Why a ``handle_message`` call is running: it is replay ``attempt`` of a
    message whose previous attempt was abandoned after a transient compaction
    failure, or after a live backend lost the session (``session_reloaded``).

    ``stop_gen_at_entry`` is the session manager's user-Stop count when the
    FIRST attempt acquired its session, carried unchanged across attempts. The
    replay compares against it right before it opens a prompt: any Stop issued
    since -- on any surface, including one that landed while the key had no
    live session between the reset and this attempt's acquire, which the caller
    keeps recordable with ``open_replay_gap`` -- means the user does not want
    this message run, and the replay ends without a turn.
    """

    attempt: int
    stop_gen_at_entry: int
    # Set on the one replay that follows a lost backend session; a second loss
    # inside it ends on a clear error instead of replaying again.
    session_reloaded: bool = False


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


# Auto-titling lives in ``messaging.auto_title`` so both Slack paths and a second
# channel share ONE claim tracker: two turns that resolved to the same session key
# cannot then title it twice. The names below are the Slack-facing spellings this
# package's call sites already use; ``_titled_threads`` is an ALIAS of the shared
# tracker, not a copy.
_titled_threads = auto_title._titled
_mark_titled = auto_title.mark_titled


# Background tasks kept alive to prevent GC mid-execution.
_background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]


# Review mode: stores draft text keyed by "channel|thread_ts|uuid" for button/modal
# handlers. Each entry includes the *requester* user_id so handlers can authorize the
# requester (in addition to bot owner) to act on their own drafts.
# Bounded with TTL to prevent memory leaks from abandoned drafts.
_REVIEW_PLACEHOLDER_TS = "review_placeholder"
_REVIEW_DRAFT_TTL = 3600  # 1 hour
_REVIEW_DRAFT_MAX = 1024
# key → (draft, requester_user_id, timestamp)
_review_drafts: dict[str, tuple[str, str, float]] = {}


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


#: The Slack-owned attributes of :class:`KiroCrewConfig` that a reload copies
#: onto the shared config object. Sections are replaced whole (the dataclass
#: instance from the new load), so a read of ``slack_cfg().slack.<field>`` sees
#: the loader's own coercion of the new value, never a raw copy.
#: ``slack_enterprise_ids`` is absent because it is a derived property over
#: ``slack.allowed_enterprise_ids`` and follows the section automatically.
_SLACK_OWNED_FIELDS: tuple[str, ...] = (
    "slack",
    "messaging",
    "slack_channels",
    "slack_dm_activation",
    "observe_max_messages",
    "observe_ttl_hours",
)


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


#: Longest span the comment hold keeps before giving up on it. Sized for the
#: three tag families stacked once each at the grammar's own bounds (each
#: line: opener, 16 whitespace, 256 body, closer, 16 trailing whitespace and
#: its newline -- under 300 bytes), so every tail the grammar admits fits; a
#: hold past it is not one and is released as prose rather than withheld to
#: end of turn. Also the bound on the per-byte re-judgement: each byte costs
#: one anchored pass over the hold, so this cap is what keeps a stream of
#: nothing but tags linear.
_COMMENT_HOLD_MAX = 1024


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
    spool_text: str | None = None,
    spool_attachments: int = 0,
    _compaction_replay: _CompactionReplay | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> None:
    """Route a Slack message through ACP with streaming and tool approval.

    ``start_priority`` orders a cold start: the Slack event and interaction paths
    pass FOREGROUND for a person's message (rule: ``kiro_crew.start_priority``).

    NOTE: ``from_trusted_bot`` is consumed only in the error path (echo-loop
    suppression). Early-reply paths (hook auto-reply, !status, !sessions) still
    post to Slack unconditionally — safe today because trusted bots send
    structured commands (``[TASK:id]``, ``[ACK:id]``) that don't match those
    patterns. Extend if that assumption changes.

    This function accepts individual parameters for backward compatibility.
    New callers can use ``MessageContext`` to group the service parameters.

    *channel_agent* overrides the default agent for this channel (set via
    per-channel config in ``slack.channels``).

    *spool_text* / *spool_attachments* are what the user sent before voice-memo
    transcription and file ingestion rewrote *text*. The shutdown spool records
    these, never the ingested *text*, which inlines turn-owned temp paths (dead
    after a restart) and extracted file contents. ``None`` means *text* was not
    ingested (button and interaction re-dispatches), so it is spooled as is.

    *_compaction_replay* is set only by this function's own turn, when it re-runs a
    message whose previous attempt was abandoned after a transient compaction
    failure (see the ``STOP_REASON_COMPACTION_FAILED`` branch). Every other
    argument is passed through unchanged, so the replay resolves the same
    session, keeps the same activation and pinning, and can still read the
    attachment files the original text refers to -- their cleanup runs when the
    OUTER call's task ends, after this nested call has returned.
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

    # Inbound channels-governance gate (off-loop). Slack is a governed transport
    # like the others: a ``channels`` policy that denies ``slack`` stops inbound
    # dispatch on the very next message without a restart (the ProfileStore
    # hot-reloads by mtime). Default OSS build (no policy) permits, so behavior is
    # unchanged. Silently drop on deny — matching how an unauthorized user is
    # ignored — before any hook/command/turn processing.
    if not await channel_inbound_permitted("slack"):
        logger.info("slack inbound dropped: denied by channels governance policy")
        return

    await _hydrate_thread_overrides(session_key, conversation_log)
    await _hydrate_conv_header(sessions, session_key)
    _hydrate_conv_flags(sessions, session_key)

    if not await admit_inbound_callback(
        sessions,
        channel_type="slack",
        route=InboundRoute(
            conversation_id=channel,
            text=text if spool_text is None else spool_text,
            user_id=user_id,
            thread_id=reply_ts,
            message_id=msg_ts,
            attachments_dropped=spool_attachments,
        ),
        restricted=_is_slack_restricted(session_key),
    ):
        return

    # One turn's inputs and the values its phases hand on (``handler_runtime/turn.py``).
    # The phases run in order, each saying whether the next one runs: the pre-turn
    # dispatch answers a message that needs no model, ``_run_turn`` holds the session
    # and streams the answer, and ``_deliver_answer`` posts it and books the verdict.
    turn = _SlackTurn(
        slack=slack,
        sessions=sessions,
        channel=channel,
        text=text,
        thread_ts=thread_ts,
        msg_ts=msg_ts,
        user_id=user_id,
        team_id=team_id,
        approval_mode=approval_mode,
        context_builder=context_builder,
        cron_service=cron_service,
        conversation_log=conversation_log,
        consolidator=consolidator,
        subagent_manager=subagent_manager,
        task_runner=task_runner,
        channel_agent=channel_agent,
        user_display_name=user_display_name,
        action_context=action_context,
        target_slot_name=target_slot_name,
        route_pinned=route_pinned,
        asker_key=asker_key,
        from_trusted_bot=from_trusted_bot,
        channel_activation=channel_activation,
        had_voice_input=had_voice_input,
        spool_text=spool_text,
        spool_attachments=spool_attachments,
        _compaction_replay=_compaction_replay,
        start_priority=start_priority,
        _t0=_t0,
        reply_ts=reply_ts,
        session_key=session_key,
    )
    if await _dispatch_before_turn(turn) and await _run_turn(turn):
        await _deliver_answer(turn)


# ── Slack thread auto-title ─────────────────────────────────────────────
#
# The turn, the claim tracker, the tool-free stream, the prompt and the
# title-cleaning rules all live in ``messaging.auto_title``. Slack supplies the
# one thing that is genuinely per-channel — renaming the Slack thread itself.

_get_auto_title_lock = auto_title.get_lock
_build_title_prompt = auto_title.build_title_prompt


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.slack.handler.<name>`` reaches it wherever it lives; see
# :mod:`kiro_crew.slack.handler_runtime`. Run once, after this body has bound every name.
_facade_compose(
    globals(),
    (
        _owner_access,
        _owner_agent_names,
        _owner_approvals,
        _owner_commands,
        _owner_delivery,
        _owner_dispatch,
        _owner_finalize,
        _owner_inbound,
        _owner_permission_ladder,
        _owner_reactions,
        _owner_stream,
        _owner_turn,
        _owner_turn_context,
        _owner_voice,
    ),
)
