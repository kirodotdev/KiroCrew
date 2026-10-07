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
from kiro_crew.acp.client import AcpError, AcpProcessDied, AcpPromptBusy, AcpTimeoutError
from kiro_crew.acp.types import (
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
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
from kiro_crew.dashboard.state import append_and_surface  # noqa: F401 - read by the owners
from kiro_crew.deny_notice import steer_refusal_notice
from kiro_crew.executors import run_in_embed_pool, run_in_tool_gate_pool
from kiro_crew.history import (  # noqa: F401 - read by the owners
    HUMAN_TURN_META_KEY,
    ConversationLog,
    HistoryConsolidator,
)
from kiro_crew.hooks import (
    HOOK_REPLY,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    event_is_spawn_run,
    hook_gate_kwargs,
    safe_read_file_bytes,
)
from kiro_crew.llm_helpers import (
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    SESSION_NOT_FOUND_RETRY_NOTICE,
    acp_error_is_session_not_found,
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
from kiro_crew.slack import handler_runtime as _handler_runtime
from kiro_crew.slack.blocks import (  # noqa: F401 - read by the owners
    build_working_blocks,
    deprecation_warning_block,
)
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
from kiro_crew.slack.handler_runtime import approvals as _owner_approvals
from kiro_crew.slack.handler_runtime import commands as _owner_commands
from kiro_crew.slack.handler_runtime import finalize as _owner_finalize
from kiro_crew.slack.handler_runtime import inbound as _owner_inbound
from kiro_crew.slack.handler_runtime import reactions as _owner_reactions
from kiro_crew.slack.handler_runtime import stream as _owner_stream
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
    _hydrate_thread_overrides,
    _is_slack_restricted,
    _persist_channel_config,
    _read_thread_overrides,
    _set_default_agent,
    maybe_apply_privacy_modifiers,
    maybe_route_linked_thread,
)
from kiro_crew.slack.handler_runtime.reactions import (  # noqa: F401
    StatusReactionController,
    _add_phase_reaction,
    _tool_to_phase,
    phase_emojis,
    refresh_phase_emojis,
)
from kiro_crew.slack.handler_runtime.stream import (  # noqa: F401
    _AnswerStream,
    _at_tag_line_start,
    _comment_hold_is_protocol,
    _filter_options_brackets,
    _resolve_comment_hold,
    _safe_final_update,
    _safe_update,
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
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    DEFAULT_PROVIDER,
    PROVIDER_PIPER,
    PROVIDER_SYSTEM,
)
from kiro_crew.voice_reply import is_available as _tts_available  # noqa: F401 - read by the owners
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    resolve_configured_provider,
)
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    validate_length_scale as _validate_length_scale,
)
from kiro_crew.voice_reply import (  # noqa: F401 - read by the owners
    validated_config_string,
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
    # Piper-specific (ignored by the other providers):
    piper_binary: str = ""
    piper_model: str = ""
    piper_model_config: str = ""
    piper_length_scale: float = 1.0
    # Built-in-engine voice. Empty means the OS default voice, which is the
    # right answer whenever the host language matches the reply language.
    system_voice: str = ""
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


def _discover_project_agents(
    project_dir: str | None, *, operation: str = "slack_project_agents"
) -> list[Path]:
    """Return agent JSON files from <project_dir>/.kiro/ and .kiro/agents/.

    Delegates to :func:`agent_discovery.project_agent_files`, the one implementation
    now shared with the dashboard picker, ``spawn_run`` validation and per-turn agent
    resolution. ``include_legacy=True`` is passed HERE and only here: Slack's
    ``*.agent-spec.json`` convention predates ``.kiro/agents/`` and is kept for
    continuity, but kiro-cli cannot activate such a name, so no dispatch surface may
    offer it.

    *operation* names the Slack request whose scan this is, so a sensitive-project-dir
    denial is attributed to the listing or the name resolution rather than to this
    shared helper. The channel is fixed: every route here is Slack.
    """
    return project_agent_files(
        project_dir, include_legacy=True, operation=operation, source="slack"
    )


def _resolve_agent_name(name: str, project_dir: str | None = None) -> str | None:
    """Resolve an agent name to its internal name via suffix matching.

    Searches project-local .kiro/ first (if project_dir set), then ~/.kiro/agents/.
    Returns the resolved name, or None if not found.
    """
    # Project-local agents take priority — kiro-cli resolves --agent against its
    # cwd before the user-level dir, so a project agent is the one that would run.
    # Prefilter on the FILENAME first: the async callers hand this to a thread,
    # but reading every spec to compare its declared name would still make a
    # checkout with many agents or slow storage slow to answer. At most the one
    # matching file is read, to return the name it declares.
    for spec in _discover_project_agents(project_dir, operation="slack_resolve_agent"):
        stem = spec.stem.removesuffix(".agent-spec")
        if stem != name and spec.stem != name:
            continue
        return project_agent_name(spec)

    agents_dir = kiro_agents_dir()
    specs = (
        sorted(iter_agent_spec_files(agents_dir), key=lambda f: (len(f.stem), f.stem))
        if agents_dir.is_dir()
        else []
    )
    match = next(
        (f for f in specs if f.stem == name or f.stem.endswith(f"-{name}")),
        None,
    )
    if not match:
        # kiro-cli's own agents ship inside its binary, so no spec file names
        # them; they resolve as themselves (see KIRO_CLI_BUILTIN_AGENTS).
        if name in KIRO_CLI_BUILTIN_AGENTS:
            return name
        # Fallback: search companion-backend cc-plugins agents
        cc_match = _resolve_cc_agent_name(name)
        return cc_match
    try:
        # The hardened reader resolves the path, vets the target and opens it
        # with no reparse in ONE step, so a symlink swapped in between a check
        # and the read is refused rather than followed.
        data = read_agent_spec_strict(match, operation="slack_resolve_agent", source="slack")
    except SensitiveAgentSpecPathError:
        # A file whose target the path gate refuses is no agent at all, as it
        # was when the path check ran here.
        return None
    except (ValueError, OSError):
        # ValueError covers bad JSON, bad frontmatter and a non-UTF-8 read. A
        # broken JSON spec still occupies its name, as it always has; a
        # markdown file that does not parse is not a spec at all (a README,
        # notes), the same rule the listing applies, so it does not resolve.
        return None if is_markdown_spec(match) else match.stem
    if not isinstance(data, dict):
        return None if is_markdown_spec(match) else match.stem
    declared = data.get("name")
    return declared if isinstance(declared, str) and declared else match.stem


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

    Merges the ``~/.kiro/agents`` spec stems (see
    :func:`kiro_crew.agent_discovery.agent_spec_stems`) with the cc-plugins
    agents from :func:`_iter_cc_agent_names`. The internal ``kirocrew-lite``
    variant is hidden. Returns ``"(none found)"`` when empty. Reads every
    markdown candidate to decide whether it is a spec, so the async callers
    run it in a thread rather than on the event loop.

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
        names.extend(
            stem
            for stem in agent_spec_stems(agents_dir, operation="slack_list_agents", source="slack")
            if stem != "kirocrew-lite"
        )
    seen = set(names)
    for agent_name in _iter_cc_agent_names(cc_plugins_dir):
        if agent_name not in seen and agent_name != "kirocrew-lite":
            names.append(agent_name)
            seen.add(agent_name)
    return ", ".join(names) if names else "(none found)"


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
    if handle_sessions and _is_sessions_keyword(text):
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
        spawn_reply = await _handle_spawn_command(text, subagent_manager, session_key)
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

    *_compaction_replay* is set only by this function itself, when it re-runs a
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
    _hydrate_conv_flags(sessions, session_key)

    if not await admit_inbound_callback(
        sessions,
        channel_type="slack",
        route=InboundRoute(
            conversation_id=channel,
            text=text,
            user_id=user_id,
            thread_id=reply_ts,
            message_id=msg_ts,
        ),
        restricted=_is_slack_restricted(session_key),
    ):
        return

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
                # After the reply is posted but BEFORE the record is written:
                # an older message in this thread may be between its reset and
                # its compaction replay, and the transcript must show that turn
                # first, as the thread does.
                await await_replay_gap(sessions, session_key)
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
    if text.strip().lower() == "status":
        # Identity status via the active PlatformContext (Default == OSS no-op
        # stub returning ""; an enterprise companion returns the real SSO line).
        sso_line = await current_context().identity.status_line(prefix=" · sso")
        await slack.post_message(channel, Stats().summary() + sso_line, reply_ts)
        return

    # ── Sessions keyword: list recent sessions ──
    if _is_sessions_keyword(text):
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

    # Strip leading bot mention from app_mention events so the ! prefix is exposed.
    # DM:       "!agent foo"                    → "!agent foo"       (no-op)
    # @mention: "<@UBOT|kirocrew> !agent foo"   → "!agent foo"      (strip prefix)
    _cmd_text = re.sub(r"^<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text.strip())

    # ── !temporary / !incognito privacy modifiers (shared with transport) ──
    text, _cmd_text, _only_modifier = await maybe_apply_privacy_modifiers(
        text, _cmd_text, session_key, user_id, channel, slack, sessions, reply_ts
    )
    if _only_modifier:
        return

    # ── !compact and the owner / allowed-user ``!`` commands ──
    # The routing lives in handler_runtime/commands.py (``_route_bang_command``); a
    # message it answers (run, denied, or replied to) never reaches the model.
    if await _route_bang_command(
        _cmd_text,
        slack,
        sessions,
        channel,
        reply_ts,
        msg_ts,
        session_key,
        user_id,
        conversation_log,
    ):
        return

    # ── Path-independent keyword commands: spawn/run/cron ──
    # ``sessions`` is deliberately excluded here (handle_sessions=False): the
    # native path keeps its own earlier ``sessions`` block above so that the
    # ``!temporary``/``!incognito`` modifier rewrites can't turn a modified
    # message into a bare ``sessions`` match. The transport path (which has no
    # modifier machinery) handles all four via the same helper.
    if await maybe_handle_keyword_command(
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
    # Whether the stream delivered an EVENT_COMPLETE; ``_stop_reason`` alone
    # cannot say (it is "" both before any completion and for one that carries
    # no reason), and the re-injection bookkeeping needs the difference.
    _completion_observed = False
    # Set at clean model completion; success accounting is booked only after the
    # answer-carrying delivery below actually posts, so this records "the model
    # finished" separately from "the reader received the answer".
    _turn_completed_ok = False
    _replayed = False  # set when a transient-compaction replay took over this message
    # Set as the last statement of the turn body. A raise that skips it -- a
    # cancellation landing in the post-compaction reset, after the model already
    # completed -- never reaches the delivery region, so the ``finally`` must not
    # defer the release to a ``_release_permit`` that will never run.
    _body_completed = False

    # Set assistant thread status while we wait for the LLM to respond.
    # Defer start_stream until the first text chunk arrives so the user
    # sees the status indicator instead of a blank bot message.
    await slack.set_thread_status(channel, reply_ts, _STATUS_WORKING)

    # Post inline stop button (only in threaded conversations to avoid breaking tests)
    _working_ts: str | None = None
    if thread_ts:
        # Best-effort: a failed stop-button post must not drop the user's message.
        try:
            _working_ts = await slack.post_blocks(
                channel, build_working_blocks(session_key), "Working…", reply_ts
            )
        except Exception:
            logger.warning("Failed to post inline stop button", exc_info=True)

    # The Slack wire this turn's answer streams to, and every flag the end of the turn
    # reads off it (``handler_runtime/stream.py``).
    answer = _AnswerStream(
        slack,
        channel,
        reply_ts,
        team_id=team_id,
        user_id=user_id,
        channel_activation=channel_activation,
        show_thinking=KiroCrewConfig.load().slack.show_thinking,
        status=status_ctrl,
    )

    task = Task(id=msg_ts)
    _acquired = False

    # ── Bidirectional sync: check if this Slack thread is linked to a dashboard session ──
    # The thread index is keyed by the bare Slack thread_ts (reply_ts), NOT the
    # namespaced session key. A self-linked Slack thread resolves to our own
    # canonical key (no-op rewrite); a dashboard-linked thread resolves to its
    # ``dashboard:chat-N`` key.
    # Keep the thread's owner truthful. Three separate decisions
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
            await _hydrate_thread_overrides(session_key, conversation_log)

    client: LLMProvider | None = None
    # Post-compaction re-injection bookkeeping for the finally: whether this
    # turn consumed the one-shot flag, whether it landed (recorded success), and
    # whether the backend reported that it compacted the session.
    _needs_reinjection = False
    _turn_landed = False
    _turn_compacted = False
    # This turn's thread-replies read; its watermark moves in the finally.
    _thread_replies: ThreadReplies | None = None

    async def _replay_abandoned(notice: str, replay: _CompactionReplay) -> None:
        """Re-run this message on the successor of the session just reset.

        The caller opened the replay gap and reset the session; this closes the
        gap once the replay has settled. The replay is a nested call of this
        function with every argument unchanged, which is what makes it Slack's
        own replay: it resolves the same session, keeps the same activation and
        pinning, runs while this task still owns the attachment temp files, and
        needs no queue drain from whoever dispatched the original -- the
        interaction paths dispatch ``handle_message`` without one.
        """
        nonlocal _acquired, _replayed, _needs_reinjection, _turn_compacted, _working_ts
        # This attempt's permit died with the session the reset popped; the
        # successor's belongs to the replay, whose own ``finally`` releases it,
        # so this frame must not release again.
        _acquired = False
        _replayed = True
        # The reset popped the session, so the replay cold-starts a NEW one whose
        # first prompt carries the full session-start context anyway; re-arming
        # the one-shot flag for this abandoned prompt, or for a compaction the
        # popped session reported, would only make the turn after the replay
        # inject it twice.
        _needs_reinjection = False
        _turn_compacted = False
        # This attempt is over: stop its reaction ladder and stall watchdog now
        # (idempotent, so the ``finally`` re-call is a no-op), and take down what
        # it posted -- the Working block and a reasoning placeholder that a
        # thinking-only attempt left above where the answer would have gone. The
        # nested call posts its own.
        status_ctrl.finalize(error=False)
        for _ts in (_working_ts, answer.thinking_ts):
            if _ts:
                try:
                    await slack.delete_message(channel, _ts)
                except Exception:
                    pass
        _working_ts = None
        answer.thinking_ts = None
        # Visible, not persisted: the abandoned attempt records nothing, so the
        # conversation log carries this message exactly once, with the reply the
        # replay produces.
        try:
            await slack.post_message(channel, notice, reply_ts)
        except Exception:
            logger.debug("Failed to post the replay notice", exc_info=True)
        try:
            await handle_message(
                slack,
                sessions,
                channel,
                _user_text,
                thread_ts,
                msg_ts,
                user_id,
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
                _compaction_replay=replay,
                start_priority=start_priority,
            )
        finally:
            # The replay has settled and released its permit (or never got
            # there); a waiter admitted now finds an idle session.
            sessions.close_replay_gap(session_key)

    try:
        task.start()
        while True:
            candidate_key = session_key
            if not route_pinned:
                thread_owner_key = sessions.get_session_for_thread(reply_ts)
                candidate_key = thread_owner_key or canonical_key(reply_ts)
                if candidate_key != session_key:
                    await _hydrate_thread_overrides(candidate_key, conversation_log)
                    if sessions.get_session_for_thread(reply_ts) != thread_owner_key:
                        continue
            # Both private identity hydration and store resolution can yield to
            # a link/unlink. Commit the route only after those reads agree with
            # the current owner; a pinned answer always keeps its asker instead.
            memory_error = None
            try:
                _memory_store = await session_store_for_turn(context_builder, candidate_key)
            except UnknownMemoryStore as exc:
                memory_error = exc
            if not route_pinned:
                if sessions.get_session_for_thread(reply_ts) != thread_owner_key:
                    continue
                if candidate_key != session_key:
                    logger.info(
                        "🔗 Slack thread %s linked to dashboard session %s — routing there",
                        session_key,
                        candidate_key,
                    )
                session_key = candidate_key
                linked_session_key = thread_owner_key
                _hydrate_conv_flags(sessions, session_key)
            if memory_error is not None:
                raise memory_error
            break
        # Re-resolve _agent against (possibly linked) session_key for the main
        # LLM path — linked dashboard sessions may carry a different thread agent.
        _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
        client, is_new, resumed = await sessions.get_or_create(
            session_key, agent=_agent, channel_id=channel, start_priority=start_priority
        )
        _acquired = True
        if _compaction_replay is not None:
            # The gap the outer attempt opened stays open until this replay has
            # settled and released its permit (the outer's ``finally`` closes it):
            # a message admitted now would park on this session's semaphore,
            # which a further retry's reset would pop from under it.
            _stop_gen_at_entry = _compaction_replay.stop_gen_at_entry
        else:
            # The user's Stop count for this key at turn start; a replay of
            # this message re-reads it before opening its prompt, so a Stop
            # issued anywhere in between -- on any surface -- keeps the
            # abandoned message dropped.
            _stop_gen_at_entry = session_stop_generation(sessions, session_key)
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
        # The Slack thread persists across processes, so we replay its history
        # to bootstrap the fresh session. Same lossless tail-first replay the
        # dashboard uses: a process death is not a context overflow, so code
        # and tool output must come back verbatim, not as an LLM summary.
        if is_new and not resumed and context_builder and context_builder.conversation_log:
            compressed = await asyncio.to_thread(
                build_session_replay,
                context_builder.conversation_log,
                session_key,
                model_window=_model_window,
            )

        # The user's message as it arrived. The block below may fold a
        # cancelled-turn preamble into ``text`` for the model; a
        # transient-compaction replay must re-run THIS value, so the nested call
        # derives its own preamble (its own gate, its own one-shot flag) and
        # persists what the user actually typed, not a preamble a previous
        # attempt prepended.
        _user_text = text
        # After a soft-cancel, kiro-cli drops the cancelled turn from its
        # conversation log — but the user+assistant text is persisted to our
        # local conversation_log. Re-inject just the cancelled turn as a
        # preamble so the LLM remembers what was interrupted. Flag lives on
        # the session (set by SessionManager.stop_turn), consumed one-shot.
        # Use getattr for prev_turn_cancelled so test doubles (AsyncMock)
        # don't raise AttributeError on coroutine-returning mock chains.
        _user_text_range = (0, len(text))
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
                offset = len(_preamble) + 2
                _user_text_range = (offset, offset + len(text))
                text = _preamble + "\n\n" + text

        # The thread's first message for a fresh session, and the replies since this
        # conversation's last turn (``handler_runtime/turn_context.py``).
        thread_parent_text, _thread_replies = await _thread_context(
            slack,
            channel,
            thread_ts,
            msg_ts,
            session_key,
            is_new=is_new,
            resumed=resumed,
            compressed=compressed,
            context_builder=context_builder,
            conversation_log=conversation_log,
            agent=_agent,
        )

        if context_builder:
            # Thread-scoped temporary mode: blocks memory reads.
            _slack_blocks_reads = is_thread_temporary(session_key)

            _thread_meta = await _thread_meta_fallback(
                slack,
                channel,
                thread_ts,
                is_new=is_new,
                resumed=resumed,
                thread_parent_text=thread_parent_text,
                compressed=compressed,
                context_builder=context_builder,
            )

            # This conversation's own silo, resolved from the session's RECORDED
            # binding and never from ``_agent`` -- on Slack that value is a kiro
            # agent name, a namespace disjoint from ``cfg.agents``, so deriving a
            # store from it answers ``default`` for exactly the crew that
            # configured otherwise. A thread taken over from a crew-bound
            # dashboard session carries that crew's key here, which is what stops
            # the takeover from reading the operator's own memory instead.
            #
            # The private tier was prepared before provider acquisition. Missing
            # or unreadable member memory refuses the turn with its own error.
            # A compaction drops session-start context. Read-and-clear the
            # one-shot flag so this turn re-injects that context exactly once;
            # the finally re-arms it if this turn never lands.
            _needs_reinjection = consume_reinjection(sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                context_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel,
                thread_ts=thread_ts or msg_ts,
                agent=_agent,
                memory_store=_memory_store,
                resumed=resumed,
                needs_reinjection=_needs_reinjection,
                user_display_name=user_display_name,
                compressed_history=compressed,
                action_context=action_context,
                thread_parent_text=thread_parent_text,
                thread_meta=_thread_meta,
                thread_replies_text=_thread_replies.text if _thread_replies else None,
                blocks_reads=_slack_blocks_reads,
                model_window=_model_window,
                runtime_source="slack",
                user_text_range=_user_text_range,
                context_provider=client,
            )
        else:
            full_message = text

        # ── Early cancellation check: bail before expensive LLM call ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled before LLM call — skipping", msg_ts)
            await slack.set_thread_status(channel, reply_ts, "")
            return
        # A replay must not run a message the user has stopped since its first
        # attempt began. Same shape and same placement as the check above: the
        # last look before the prompt opens.
        if (
            _compaction_replay is not None
            and session_stop_generation(sessions, session_key) != _stop_gen_at_entry
        ):
            logger.info("Message %s stopped before its compaction replay — skipping", msg_ts)
            await slack.set_thread_status(channel, reply_ts, "")
            if _working_ts:
                try:
                    await slack.delete_message(channel, _working_ts)
                except Exception:
                    pass
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
            turn_ceiling.gate(session_key, lambda: sessions.begin_turn(session_key))()
        except TurnCeilingExceeded as exc:
            # At the conversation's turn ceiling, so no turn opened. This route
            # streams without a TurnDriver, so the notice is posted directly
            # rather than rendered; without it the pause would be the same
            # silence the per-message echo guard already leaves.
            #
            # The "Working…" block is deleted on the way out, as the
            # compaction-replay exit above does: it carries a live Stop button for
            # a turn that never opened, and this route returns before the normal
            # path that would clean it up. The latch does not clear on its own, so
            # leaving it would strand one per refused message.
            logger.warning("Slack turn ceiling reached for %s -- conversation paused", session_key)
            if exc.announce:
                try:
                    await slack.post_message(channel, str(exc), reply_ts or None)
                except Exception:
                    logger.debug("turn-ceiling notice post failed", exc_info=True)
            await slack.set_thread_status(channel, reply_ts, "")
            if _working_ts:
                try:
                    await slack.delete_message(channel, _working_ts)
                except Exception:
                    pass
            return
        except SessionClosingError:
            logger.info("Aborting Slack dispatch for %s — gateway shutting down", session_key)
            await slack.set_thread_status(channel, reply_ts, "")
            return

        async for event in client.stream(full_message):
            if event.kind == EVENT_TEXT_CHUNK:
                await answer.on_text(event)

            elif event.kind == EVENT_THINKING_CHUNK:
                await answer.on_thinking(event)

            elif event.kind == EVENT_TOOL_CALL:
                answer.tool_gap = True
                # Check tool hooks. NOTE: EVENT_TOOL_CALL is informational —
                # the tool has already been auto-approved by the provider and
                # is executing; this branch cannot reject_tool(). The real
                # enforceable gate is EVENT_PERMISSION_REQUEST below. So we do
                # NOT arm deny-by-default here (is_shell omitted): a shell tool
                # with an unrecoverable command would otherwise render a
                # misleading "blocked" message while the tool actually runs.
                # For the same reason this site deliberately does NOT use
                # ``hook_gate_kwargs`` (the shared extraction every enforcing
                # permission-request site threads): the params/diff-path tiers
                # it would arm can also deny a call that is already executing,
                # and this warning must never claim to have blocked one. The
                # structural test in test_hooks.py names this site as the one
                # informational exception. A genuine deny-list / sensitive-path
                # match still surfaces a (best-effort, non-enforcing) warning +
                # audit.
                if context_builder:
                    tool_result = await run_in_tool_gate_pool(
                        context_builder.hooks.on_tool_call,
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        command=event.shell_command,
                        mcp_server_name=event.mcp_server_name,
                        mcp_tool_name=event.tool_name,
                        mcp_identity_trusted=event.mcp_identity_trusted,
                    )
                    if tool_result.action == TOOL_DENY:
                        # event.title is LLM-authored (select_tool_title prefers
                        # the model's description) — never post it to Slack raw.
                        _flagged_title, _ = redact_exfiltration_urls(event.title)
                        _flagged_title, _ = redact_credentials(_flagged_title)
                        answer.accumulated += (
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
                await answer.on_tool_call(event)

            elif event.kind == EVENT_PERMISSION_REQUEST:
                # Check tool hooks for auto-approve
                if context_builder:
                    tool_result = await run_in_tool_gate_pool(
                        context_builder.hooks.on_tool_call,
                        event.title,
                        session_key=session_key,
                        agent=_agent or "",
                        **hook_gate_kwargs(event),
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
                            approval_sent = await client.approve_tool(event.request_id)
                            if approval_sent is False:
                                sel().log_tool_invocation(
                                    session_key=session_key,
                                    source="slack",
                                    tool_name=event.title,
                                    tool_kind=event.tool_kind,
                                    outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                                    request_id=event.request_id,
                                )
                                continue
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
                        # Audit FIRST, then steer, then reject: the steer and
                        # the reject both await the ACP pipe, and a backend that
                        # stops reading stdin cancels this coroutine at the
                        # turn deadline -- an SEL row sequenced after them
                        # never runs (the chat runner's audit-first rule).
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome="denied",
                            request_id=event.request_id,
                            error="hook_deny",
                        )
                        # A hook deny is a HOST verdict on the call, not the
                        # person's: tell the model so in-band before the reject
                        # hands it kiro-cli's "User denied tool execution".
                        await _steer_host_deny(
                            client,
                            event,
                            tool_result.reason,
                            cause=DENY_CAUSE_POLICY,
                            audited=True,
                        )
                        await client.reject_tool(event.request_id)
                        Stats().inc_tool_denial()
                        # event.title is LLM-authored — redact before posting.
                        _blocked_title, _ = redact_exfiltration_urls(event.title)
                        _blocked_title, _ = redact_credentials(_blocked_title)
                        answer.accumulated += f"\n🚫 _Tool `{_blocked_title}` blocked by hooks._"
                        continue

                # auto_approve_subagent_spawn → auto-approve spawn_run tool calls
                if _should_auto_approve_spawn(context_builder, event):
                    approval_sent = await client.approve_tool(event.request_id)
                    if approval_sent is False:
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                            request_id=event.request_id,
                        )
                        continue
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
                    approval_sent = await client.approve_tool(event.request_id)
                    if approval_sent is False:
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                            request_id=event.request_id,
                        )
                        continue
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
                    approval_sent = await client.approve_tool(event.request_id)
                    if approval_sent is False:
                        sel().log_tool_invocation(
                            session_key=session_key,
                            source="slack",
                            tool_name=event.title,
                            tool_kind=event.tool_kind,
                            outcome=OUTCOME_REJECTED_TRANSPORT_FLOOR,
                            request_id=event.request_id,
                        )
                        continue
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
                    await answer.prepare_for_approval()
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
                    await answer.on_tool_rejected()
                    break

            elif event.kind == CONTEXT_EVENT_COMPACTION:
                # The backend compacted the session on its own: the session-start
                # context is gone from its window, and the finally arms the flag.
                if event.text == "completed":
                    _turn_compacted = True

            elif event.kind == EVENT_COMPLETE:
                status_ctrl.on_progress()
                _stop_reason = event.stop_reason
                _completion_observed = True
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
            # Success accounting is deferred to after the answer-carrying delivery
            # below (past the ``finally``), not recorded here. On the no-stream
            # paths the answer is not sent yet at this point, so booking a success
            # here would credit a turn whose only message can still fail to post.
            # This flag marks a clean model completion; the delivery block below
            # decides success vs failure once the answer is actually out.
            _turn_completed_ok = True
            # Re-injection is restored only when the observed completion did not
            # land. Account success separately after confirmed answer delivery.
            _turn_landed = stop_reason_landed(
                (_stop_reason or "") if _completion_observed else None
            )

        if _stop_reason == STOP_REASON_COMPACTION_FAILED:
            # The completion was synthetic — the backend abandoned the turn
            # after a failed auto-compaction and never sent end_turn, so it
            # still counts the prompt as in progress. Reset now (mirrors the
            # dashboard runner's needs_session_reset) or the NEXT message
            # collides with "prompt already in progress" and burns the busy
            # recovery path. The context-usage probe is skipped — compaction
            # just failed and the session was torn down.
            #
            # Opened BEFORE the reset pops the session: from that pop until the
            # replay acquires its successor, a Stop would find no session and
            # go unrecorded -- the window the replay's pre-prompt check exists
            # for -- and a newer message for this key would claim the successor
            # first and run ahead of the replay; the open gap makes any other
            # task's claim wait. Closed when the replay has settled (the
            # ``finally`` around the nested call), or right below when no
            # replay is attempted.
            sessions.open_replay_gap(session_key)
            _reset_ok = True
            try:
                await sessions.reset(session_key)
            except Exception:
                _reset_ok = False
                logger.debug(
                    "Failed to reset session %s after compaction failure",
                    session_key,
                    exc_info=True,
                )
            # Whether the abandoned message is replayed depends on WHY
            # compaction failed, which is the verdict the ACP layer records. A
            # compaction that overflowed the window fails again identically, so
            # replaying it only burns the budget — the case the unconditional
            # give-up was written for. A throttled or 5xx'd summarization call
            # has nothing wrong with it, and dropping the message for it ends
            # the turn on a backend hiccup the next attempt would clear.
            _attempt = _compaction_replay.attempt if _compaction_replay is not None else 0
            if (
                _reset_ok
                # Compared against True rather than read for truthiness: the
                # retry must require a real verdict, so a provider that never
                # set the attribute (or exposes an auto-created stand-in for
                # it) cannot be read as "transient" by accident.
                and getattr(client, "last_compaction_transient", False) is True
                # Verbatim replay is only safe before anything landed in the
                # thread — text or a tool card. Once output or a tool call has
                # landed, re-sending could repeat a side effect, so an emitted
                # turn keeps the give-up behaviour.
                and not answer.accumulated
                and answer.task_counter == 0
                and _attempt < _COMPACTION_FAILED_RETRIES
            ):
                logger.info(
                    "Transient compaction failure in %s (attempt %d/%d) — "
                    "replaying the abandoned message",
                    session_key,
                    _attempt + 1,
                    _COMPACTION_FAILED_RETRIES,
                )
                await _replay_abandoned(
                    _COMPACTION_RETRY_NOTICE,
                    _CompactionReplay(
                        attempt=_attempt + 1,
                        stop_gen_at_entry=_stop_gen_at_entry,
                        session_reloaded=(
                            _compaction_replay is not None and _compaction_replay.session_reloaded
                        ),
                    ),
                )
            else:
                sessions.close_replay_gap(session_key)
        else:
            # Check context usage — fires background compaction at configured
            # threshold, never blocks. This runs AFTER ``_turn_completed_ok`` is
            # set (the model already finished cleanly), so a raise here must NOT
            # reach the turn-failure ``except`` chain below: that chain books a
            # raw ``record_failure`` while ``_turn_completed_ok`` stays True, so
            # the delivery-verdict block would then book a SECOND time (the
            # ``_verdict_booked = not _turn_completed_ok`` guard is defeated) —
            # double-booking the turn on an ordinary transient probe error. The
            # probe is advisory, so swallow its failure here and let the completed
            # turn proceed to its single delivery-time verdict.
            try:
                sessions.check_context_usage(session_key, client)
            except Exception:
                logger.warning(
                    "check_context_usage failed for %s — skipping (advisory)",
                    session_key,
                    exc_info=True,
                )
        _body_completed = True

    except AcpTimeoutError as e:
        _had_error = True
        answer.accumulated = e.partial_output or "⏱️ Request timed out. Please try again."
        task.fail("timeout")
        await sessions.record_failure(session_key)
        Stats().inc_timeout()
        Stats().inc_message_failed()
    except AcpProcessDied:
        _had_error = True
        answer.accumulated = answer.accumulated or "💀 Agent process died. Please try again."
        task.fail("process_died")
        # The circuit breaker counts a session's OWN consecutive failures, and
        # trips into a reset. A process this session was sharing dying is not
        # this session's failure, and counting it there is how N co-tenants each
        # marched their own breaker toward tripping over one process event. The
        # death was classified once where it was detected; this reads that record.
        # A single-tenant runtime is charged exactly as before.
        if runtime_death.caused_by_this_session(client):
            await sessions.record_failure(session_key)
        else:
            # Bounded, like every other exemption: the breaker is what resets a
            # session whose runtime keeps dying, so an unbounded skip would leave
            # a session on a permanently dying shared process never recovering.
            # The streak is counted against that runtime rather than the session.
            #
            # At the limit the substitute bound PERFORMS the actuator rather than
            # adding one charge to the counter it stood in for. Charging instead
            # would deliver twice the bound it claims: the exemption spends the
            # first `_CIRCUIT_BREAKER_THRESHOLD` deaths, and a counter still at
            # zero then needs that many charges again, so a session on a
            # permanently dying shared runtime would lose about twice as many
            # turns as one that was never exempted. `record_failure` trips into
            # exactly this reset, so calling it here is the same recovery at the
            # limit the breaker would have reached -- and it leaves the session's
            # own failure count untouched, which is the whole point: the session
            # never misbehaved.
            _shared_streak = runtime_death.note_shared_death(session_key)
            if _shared_streak >= _CIRCUIT_BREAKER_THRESHOLD:
                logger.warning(
                    "session %s: the runtime it shares has died %d times running — "
                    "resetting it now, the same recovery the breaker performs",
                    session_key,
                    _shared_streak,
                )
                try:
                    await sessions.reset(session_key)
                    # The reset IS the hand-over, so the streak is spent: clear it
                    # or the next shared death hands over again and every death
                    # from here on performs the actuator, which is the unexempted
                    # behaviour the bound exists to replace. Cleared only once the
                    # reset has returned -- a reset that raised transferred
                    # nothing, and keeping the streak is what makes the next death
                    # retry it.
                    runtime_death.clear_shared_deaths(session_key)
                except Exception:
                    logger.warning(
                        "session %s: reset after a shared runtime's deaths failed",
                        session_key,
                        exc_info=True,
                    )
            else:
                logger.warning(
                    "session %s lost a turn to a SHARED runtime's death (%d running) — "
                    "not counting it toward the circuit breaker",
                    session_key,
                    _shared_streak,
                )
        Stats().inc_message_failed()
    except AcpPromptBusy as e:
        _had_error = True
        # Session is wedged mid-prompt — reset the provider so the next
        # message cold-starts cleanly instead of hitting the same wall.
        try:
            await sessions.reset(session_key)
        except Exception:
            logger.debug("Failed to reset session %s after prompt-busy", session_key, exc_info=True)
        answer.accumulated = f"❌ {e}"
        task.fail(str(e))
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    except AcpError as e:
        _had_error = True
        # Only once this attempt holds a claim: the binding to reset is the one
        # it claimed, and the Stop count the replay checks was read with it.
        _session_lost = acp_error_is_session_not_found(e) and _acquired
        _reset_ok = False
        if _session_lost:
            # A live backend holds no session under the mapped id, so every later
            # message on this binding would draw the same answer. The reset keeps
            # the session-map entry: the next claim spawns a fresh runtime that
            # session/loads the SAME id. The gap is opened first for the same
            # reason as the compaction replay above.
            sessions.open_replay_gap(session_key)
            _reset_ok = True
            try:
                await sessions.reset(session_key)
            except Exception:
                _reset_ok = False
                logger.debug(
                    "Failed to reset session %s after the backend lost it",
                    session_key,
                    exc_info=True,
                )
        _already_reloaded = _compaction_replay is not None and _compaction_replay.session_reloaded
        if (
            _session_lost
            and _reset_ok
            and not _already_reloaded
            # Verbatim replay only before anything landed in the thread, the
            # same bar the compaction replay holds: a tool may have run.
            and not answer.accumulated
            and answer.task_counter == 0
        ):
            logger.warning(
                "Backend lost the session for %s — re-loading it and replaying the message once",
                session_key,
            )
            _had_error = False
            task.complete()
            await _replay_abandoned(
                SESSION_NOT_FOUND_RETRY_NOTICE,
                _CompactionReplay(
                    attempt=_compaction_replay.attempt if _compaction_replay is not None else 0,
                    stop_gen_at_entry=_stop_gen_at_entry,
                    session_reloaded=True,
                ),
            )
        else:
            if _session_lost:
                sessions.close_replay_gap(session_key)
            if _session_lost and (answer.accumulated or answer.task_counter):
                answer.accumulated = f"⟳ {SESSION_NOT_FOUND_NOT_REPLAYED_TEXT}"
            elif _session_lost:
                answer.accumulated = (
                    f"❌ {SESSION_NOT_FOUND_GIVE_UP_TEXT} Send your message again, "
                    "or start a new thread if this keeps happening."
                )
            else:
                answer.accumulated = f"❌ {e}"
            task.fail(str(e))
            await sessions.record_failure(session_key)
            Stats().inc_message_failed()
    except UnknownMemoryStore as exc:
        _had_error = True
        answer.accumulated = redact_local_paths(redact(str(exc)))[0][:1000]
        task.fail("memory_unavailable")
        Stats().inc_message_failed()
    except Exception:
        _had_error = True
        logger.exception("Unexpected error handling message")
        answer.accumulated = answer.accumulated or "🔧 Something went wrong. Please try again."
        task.fail("unexpected")
        await sessions.record_failure(session_key)
        Stats().inc_message_failed()
    finally:
        # A turn that consumed the post-compaction flag but never landed (an
        # error arm, a cancel) discarded the prompt carrying the re-injected
        # context, and a backend that compacted the session during the turn
        # dropped it; either way the flag is set so the next turn re-injects it.
        rearm_reinjection(
            sessions,
            session_key,
            consumed=_needs_reinjection,
            landed=_turn_landed,
            compacted=_turn_compacted,
        )
        # The replies watermark moves only past a turn that landed after a good
        # read; a cancelled or failed turn discarded the prompt that carried them.
        if _turn_landed and _thread_replies is not None and _thread_replies.read_ok:
            note_turn(session_key, thread_ts or msg_ts, msg_ts)
        rollback_skill_bodies(context_builder, session_key, landed=_turn_landed)
        # The permit is held past this ``finally`` when the turn reached a clean
        # model completion, because success/failure accounting is booked only
        # after the answer-carrying delivery below and mutates per-session breaker
        # state (``consecutive_failures``). Releasing here would open a window in
        # which the next queued turn for the same folded key acquires the permit
        # and mutates that same state while this turn is still deciding its own
        # verdict, corrupting the breaker. On every error path accounting already
        # ran inside the ``except`` blocks above, so the permit is released now.
        # A raise that left the body after the model completed (``_body_completed``
        # still False) is released now too: nothing below this ``finally`` runs.
        _release_deferred = _acquired and _turn_completed_ok and _body_completed
        if _acquired and not _release_deferred:
            sessions.release(session_key)
            _acquired = False
        # A replay gap this attempt opened must not outlive it: a cancellation
        # landing in the reset (``!stop`` cancels the handler task) skips every
        # close inside the try, and a gap left open makes every later claim on
        # this key wait forever. Idempotent, and ordered after the release so a
        # waiter admitted now finds an idle session; when the release is
        # deferred past this ``finally`` the gap is deferred with it and
        # ``_release_permit`` closes both. Probed with ``getattr`` because this
        # line runs on EVERY turn, including the many focused session-manager
        # doubles across the suite that predate the method.
        if not _release_deferred:
            _close_gap = getattr(sessions, "close_replay_gap", None)
            if callable(_close_gap):
                _close_gap(session_key)
        status_ctrl.finalize(error=_had_error)

    # Release the retained permit once delivery and accounting have run. Called
    # explicitly right after accounting so a queued turn can proceed while the
    # post-answer decorations run, and again from the structural ``finally``
    # below so that ANY exit from the delivery/accounting region — a return, or a
    # raise from a Slack send or a task cancellation — still releases. Idempotent:
    # guarded on ``_acquired`` so the second call is a no-op.
    def _release_permit() -> None:
        nonlocal _acquired
        if _acquired:
            sessions.release(session_key)
            _acquired = False
            # The gap opened for a replay is held for as long as the permit is,
            # so the deferred-release path closes it here, right after the
            # release, and the ``finally`` above closes it on every other exit.
            _close_gap = getattr(sessions, "close_replay_gap", None)
            if callable(_close_gap):
                _close_gap(session_key)

    if _replayed:
        # The nested call posted the reply, persisted the turn, booked its own
        # verdict and cleared the thread status; this abandoned attempt has
        # nothing of its own to show and holds no permit (the reset popped the
        # session whose permit it held), so nothing is released here.
        return

    # Structural release guarantee: delivery and accounting below can raise
    # on ANY step (a Slack send such as post_ephemeral, a conversation-log
    # write, or a CancelledError from stop/shutdown landing after the verdict
    # is decided). The permit is held across this whole region, so the release
    # must be in a finally rather than at hand-listed exit points — an
    # unlisted raise would otherwise strand the per-session semaphore with no
    # timeout and no other releaser, wedging every later turn on the folded
    # key. _release_permit() is idempotent, so the explicit releases inside
    # (before the post-answer decorations) remain correct and this finally is
    # a no-op once they have run.
    try:
        # ALL verdict state and the verdict helpers are bound BEFORE the first
        # suspension point below. Both ``finally`` blocks (the release finally
        # here and the decorations-tail finally) read ``_options_verdict_deferred``
        # and ``_verdict_booked`` and call these helpers, so a cancellation
        # delivered at the very first ``await`` must find them already bound. A
        # cleanup block may only read state that was bound before the first point
        # control can leave the body -- a finally that reads a local bound after a
        # yield is a landmine.
        _options_verdict_deferred = False  # set at the verdict step; read by both finallys
        _verdict_booked = not _turn_completed_ok  # model errors already booked
        _title_pin_held: auto_title.RecordPin | None = None  # set under the permit
        # Bound before the first suspension point so the release finally can read
        # it on a cancellation landing at any await. Recomputed at the delivery
        # step below; the default False is correct for a cancellation BEFORE
        # delivery (nothing reached the reader, so the finally books no success).
        _answer_reached = False
        # Whether this turn carries an [OPTIONS] control. Bound early (default
        # False) so the release finally can tell an OPTIONS turn apart even on a
        # cancellation that lands BEFORE ``options`` is computed at the delivery
        # step: on such a turn the choices ride only in the footer and the verdict
        # is owned by the footer site, so the finally must never book success for
        # it. ``_options_verdict_deferred`` cannot serve this role because it is
        # set only at 4710, AFTER the ``stop_stream`` await where a cancellation
        # can occur.
        _options_present = False

        def _book_success() -> None:
            nonlocal _verdict_booked
            if _verdict_booked:
                return
            _verdict_booked = True
            sessions.record_success(session_key)
            # Reset with the counter it substitutes for: record_success clears
            # consecutive_failures, so a completed turn must clear the shared-death
            # streak too. Otherwise the streak is a LIFETIME total and the bound
            # stays permanently tripped, silently ending the exemption.
            runtime_death.clear_shared_deaths(session_key)
            Stats().inc_message_success()
            if client is not None:
                record_interaction_event(client, session_key, "slack")

        async def _book_failure() -> None:
            nonlocal _verdict_booked, _had_error
            if _verdict_booked:
                return
            _verdict_booked = True
            _had_error = True
            await sessions.record_failure(session_key)
            Stats().inc_message_failed()

        # ``asyncio.sleep(0)`` lets ``status_ctrl.finalize`` fire. It lives INSIDE
        # this try, AFTER the verdict state above, so a cancellation landing on
        # this first yield reaches the release finally with every local it reads
        # already bound, rather than propagating with the permit held.
        await asyncio.sleep(0)

        # ── Cancelled check: suppress response if message was deleted mid-flight ──
        if sessions.is_cancelled(session_key, msg_ts):
            logger.info("Message %s cancelled (deleted) — suppressing response", msg_ts)
            # A clean model completion whose reply the user then deleted is a
            # success, not a failure: the model did its work and the suppression
            # is a user action, not a delivery fault. Book it before returning so
            # this cancellation exit is not a verdict hole.
            if _turn_completed_ok:
                _book_success()
            await slack.set_thread_status(channel, reply_ts, "")
            if answer.stream_ts:
                try:
                    await slack.delete_message(channel, answer.stream_ts)
                except Exception:
                    logger.debug("Failed to delete cancelled stream", exc_info=True)
            if answer.thinking_ts:
                try:
                    await slack.delete_message(channel, answer.thinking_ts)
                except Exception:
                    logger.debug("Failed to delete thinking placeholder", exc_info=True)
            if _working_ts:
                try:
                    await slack.delete_message(channel, _working_ts)
                except Exception:
                    pass
            _release_permit()
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
            if answer.thinking_ts:
                try:
                    await slack.delete_message(channel, answer.thinking_ts)
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
            _release_permit()
            return

        # Strip any inline <thinking> tags that leaked into the text
        _untrimmed = ""
        if answer.accumulated:
            answer.accumulated, inline_thinking = strip_thinking_tags(answer.accumulated)
            # Trailing control-tag lines are peeled BEFORE the whitespace trim: the
            # trim would erase the indentation that marks a quoted, 4-space-indented
            # tag as code, and the tail grammar would then read it as protocol.
            _untrimmed = strip_control_comments(answer.accumulated)
            answer.accumulated = _untrimmed.strip()
            if inline_thinking:
                answer.thinking_accumulated += (
                    "\n\n" if answer.thinking_accumulated else ""
                ) + inline_thinking

        actually_streamed = answer.use_slack_stream and bool(answer.stream_ts)
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
        # Extract the OPTIONS tag from the RAW answer.accumulated text, BEFORE rendering.
        # It is a plain-text marker at the very end of the turn, so rendering first
        # makes the controls hostage to the render's size ceiling: a >39,000-char
        # answer ending in [OPTIONS: ...] is truncated, the tag goes with the tail,
        # and the buttons silently never appear. Matches the ordering used by the
        # cron, subagent-completion and dashboard-mirror paths.
        # From the UNTRIMMED text, for the same reason as above: a tag line that sat
        # before the OPTIONS trailer is protocol only with its own indent in view.
        _body_text, options = extract_options(_untrimmed) if answer.accumulated else ("", [])
        _body_text = strip_control_comments(_body_text).strip()
        _options_present = bool(options)

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
        from kiro_crew.platform import safe_context_call

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
                    "Redacted %d exfiltration URL(s) introduced by reply decorator",
                    len(_exfil_after),
                )
            clean_text, _cred_after = redact_credentials(clean_text)
            if _cred_after:
                # Log only the COUNT — the per-warning strings embed a truncated
                # prefix of the matched credential (redact_credentials returns
                # "Redacted credential pattern: <first 20 chars>..."), so logging
                # them verbatim would defeat the redaction we just performed.
                logger.warning(
                    "Redacted %d credential pattern(s) introduced by reply decorator",
                    len(_cred_after),
                )

        # Per-turn tally of redaction placeholders in the text actually SENT, so the
        # user learns their pasteable text was rewritten. Read from the TAG in
        # `clean_text` rather than from `cred_warnings`, which only reaches the log:
        # on the streaming path that list is empty here because each chunk was already
        # redacted upstream, so re-redacting `clean_text` reports nothing. Counting the
        # artifact answers the question the user has -- "is what I am about to copy
        # still what the assistant wrote?" -- and stays correct wherever the
        # substitution happened (per-chunk, the StreamRedactor wire pass, the final
        # render, or the post-decorator scan). The shared ``count_redaction_tags``
        # sums every tag the redactor can emit so an encoded-credential-only reply
        # is not missed, and counts the exfiltration-URL tag by its prefix, because
        # that tag interpolates the redacted domain and has no constant form to
        # equality-compare. Kept as separate counts because the notice is worded
        # by kind: the remedies differ (re-enter the secret vs re-check the URL).
        #
        # The thinking block (redacted separately below) adds to this SAME tally so a
        # single warning covers the turn if either the answer or the thinking was
        # rewritten -- one turn, one notice, never two identical warnings.
        _cred_redactions, _url_redactions = count_redaction_tags(clean_text)

        # ── Review mode: ephemeral draft instead of public post ──
        if channel_activation == ACTIVATION_REVIEW:
            if not await _post_review_draft(
                answer, slack, channel, reply_ts, thread_ts, user_id, session_key, clean_text
            ):
                if _turn_completed_ok:
                    await _book_failure()
                _release_permit()
                return
            # Persist conversation (draft counts as a turn). Best-effort: the draft
            # already reached the reader, so a persistence failure must not turn a
            # delivered draft into a failed turn — book success first, then persist.
            if _turn_completed_ok:
                _book_success()
            if conversation_log and not _is_slack_restricted(session_key):
                try:
                    await save_conversation_turn_off_loop(
                        conversation_log,
                        session_key,
                        text,
                        answer.accumulated,
                        source_thread=session_key,
                        source_user=user_id,
                        agent=_agent,
                    )
                except Exception:
                    logger.warning(
                        "Slack review-draft persist failed for %s", session_key, exc_info=True
                    )
            _release_permit()
            return

        # ── Answer delivery, then exactly one verdict ──────────────────────────
        # THE turn invariant, in one place. Read it before touching accounting:
        #
        #   answer-reached = the reader has the answer. The evidence differs by
        #     path, on purpose:
        #       * Streaming: the answer is delivered incrementally as it arrives,
        #         and a refused append is recoverable delivery-debt (a designed
        #         follow-up), not a turn failure -- so a stream that was used is
        #         answer-reached.
        #       * No-stream: the answer is delivered ONLY by the final send, so
        #         answer-reached requires that send to RETURN (not raise). A
        #         truthy placeholder handle is not evidence -- the send itself is.
        #       * No answer text to send (a tool-only turn): nothing to deliver,
        #         answer-reached by definition.
        #
        #   send classification (explicit, not implied by which try block a call
        #     sits in):
        #       answer-carrying -> stream appends; the no-stream fallback send
        #         (``_safe_final_update`` / ``post_message``); and the timing
        #         footer WHEN it carries an [OPTIONS] control, because the trailer
        #         was stripped from the answer and the choices ride only in the
        #         footer.
        #       decoration -> the ``stop_stream`` seal (text is already on screen),
        #         the redaction overwrite on an already-delivered stream, thinking
        #         posts, the credential notice, and a footer with no options.
        #
        #   verdict -> exactly one per turn, on every exit including exceptions and
        #     cancellation: a failed answer-carrying send books a failure, a
        #     reached answer books a success, a failed decoration send books
        #     nothing and logs. ``_verdict_booked`` (defined above, shared with the
        #     review path) guarantees the "exactly one" so no path double-books and
        #     none books zero.
        #
        # A ``clean_text`` of the ``_NO_RESPONSE`` placeholder is NOT answer text
        # to deliver: a reasoning-only / tool-only turn renders empty and picks up
        # the ``_No response._`` sentinel upstream (4429), so treating it as real
        # answer text would demand a confirmed stream append that legitimately
        # never happens, and the wholly-refused-stream predicate below would book
        # such an ordinary turn a FAILURE — driving the consecutive-failure breaker
        # toward a session-resetting trip. There is nothing to deliver, so the
        # (placeholder) answer reaches trivially.
        _answer_text_to_send = bool(clean_text) and clean_text != _NO_RESPONSE
        _answer_reached = not _answer_text_to_send

        try:
            if answer.use_slack_stream and answer.stream_ts:
                await answer.finish(_untrimmed)
                # On the streaming path the answer is delivered incrementally as
                # it arrives, and a refused append on a stream that DID land text
                # is recoverable delivery-debt (a designed follow-up), NOT a turn
                # failure: the model produced a complete answer and the drop is
                # transient. So a stream that delivered at least one real-text
                # append counts as answer-reached. But a stream on which EVERY
                # append was refused (a Slack outage for the whole turn) delivered
                # nothing to the reader, and booking that as a success would count
                # an answer that never arrived -- exactly the before-delivery
                # accounting this change exists to remove. ``answer.delivered``
                # rises only on a confirmed real-text append, so it separates the
                # two: used stream -> reached, wholly-refused stream -> failure.
                # A turn with no answer text to send (``_answer_text_to_send``
                # False: empty body or the ``_NO_RESPONSE`` placeholder) opened the
                # stream for reasoning/tools but has nothing to deliver, so no
                # append lands and ``answer.delivered`` is legitimately False;
                # clobbering ``_answer_reached`` to False there would book an
                # ordinary reasoning/tool-only turn a failure. Only apply the
                # wholly-refused-stream predicate when there WAS answer text.
                if _answer_text_to_send:
                    _answer_reached = answer.delivered
                await answer.seal(
                    clean_text, redacted=bool(_render_redacted or exfil_warnings or cred_warnings)
                )
            elif answer.stream_ts:
                # Legacy fallback (chat.startStream unavailable): the "Thinking…"
                # placeholder is replaced with the clean text. Answer-carrying —
                # nothing streamed — so a primary-send failure raises and books a
                # failure; a send that returns is confirmed delivery.
                final_text = _convert_tables(clean_text) if clean_text else _NO_RESPONSE
                await _safe_final_update(
                    slack,
                    channel,
                    answer.stream_ts,
                    final_text or _NO_RESPONSE,
                    reply_ts,
                    raise_on_primary_failure=True,
                )
                _answer_reached = True
            else:
                # No stream and no placeholder — post the answer directly.
                # Answer-carrying: a raise means the reader got nothing; a return
                # is confirmed delivery.
                await slack.post_message(channel, clean_text or _NO_RESPONSE, reply_ts)
                _answer_reached = True
        except Exception:
            # An answer-carrying send failed: the reader received no answer.
            logger.exception("Slack answer delivery failed for %s", session_key)
            await _book_failure()
            try:
                await slack.post_message(
                    channel,
                    "🔧 Something went wrong delivering the reply. Please try again.",
                    reply_ts,
                )
            except Exception:
                logger.debug("Failed to post delivery-failure notice", exc_info=True)
            # The answer did not land; skip the decorations and release the permit.
            _release_permit()
            return

        # Exactly one verdict, from the predicate: a reached answer on a clean
        # completion is a success; a clean completion whose answer did NOT reach
        # the reader (every append refused, no fallback) is a failure. A turn that
        # already booked a failure in the except blocks above (a model-level
        # error) is left as-is by the idempotent guard.
        #
        # OPTIONS exception: when the reply carries an [OPTIONS] control the
        # choices were stripped from the answer body and ride ONLY in the footer,
        # so that footer (or its fallback) is itself answer-carrying. For such a
        # turn the verdict and the permit release are DEFERRED to the footer site
        # below, which books success only once one of the two OPTIONS deliveries
        # returns — and books a failure if both fail. The permit stays held across
        # the intervening decorations (fast, best-effort) so the deferred verdict
        # is still written under it.
        # Pin the record for the naming turn while the permit is still held. Every
        # release below is followed by Slack round-trips before the auto-title block,
        # and a queued turn that takes the released permit can delete this key's
        # record and re-mint it in that span. A pin read down there reads the
        # REPLACEMENT, the guard matches it, and the title generated from this turn
        # names a conversation it never ran in. While the permit is held no other
        # turn for this key runs, so the identity read here is the record this turn
        # is about. The same cheap ``is_titled`` peek the block below uses gates it,
        # so an already-named conversation pays no thread hop.
        #
        # A key whose record has not landed yet pins ABSENT here and is re-pinned
        # below once this turn's own row is written: with no record there is nothing
        # a replacement can be mistaken for, and the first exchange stays nameable.
        if (
            not _had_error
            and not _is_slack_restricted(session_key)
            and not auto_title.is_titled(session_key)
        ):
            _title_pin_held = await auto_title.pin_record(conversation_log, session_key)
        _options_verdict_deferred = bool(_turn_completed_ok and options and _answer_reached)
        if not _options_verdict_deferred:
            if _turn_completed_ok:
                if _answer_reached:
                    _book_success()
                else:
                    await _book_failure()
            # Accounting is done; release the retained permit now. The decorations
            # below touch no verdict state on this path.
            _release_permit()
    finally:
        # F2/F1: a cancellation (BaseException, uncaught by ``except Exception``)
        # raised AFTER the answer reached the reader — at the first
        # ``await asyncio.sleep(0)`` on a stream that already delivered, or inside
        # the best-effort ``stop_stream`` await — but before the verdict step,
        # propagates straight here leaving the turn with no verdict booked though
        # delivery succeeded. Book the decided success so that window is not a
        # verdict hole (a missing success-reset that leaves the consecutive-
        # failure counter stale). The delivered signal is ``_answer_reached OR
        # answer.delivered``: ``_answer_reached`` is recomputed only at the
        # delivery step (past ``sleep(0)``), but ``answer.delivered`` rises at the
        # first confirmed real-text append — before that first await — so it makes
        # a fully-streamed turn cancelled at the ``sleep(0)`` yield book its
        # success too, closing the whole cancellation-at-any-await class rather
        # than one await at a time. Idempotent via ``_verdict_booked``; gated on
        # ``not _options_present`` because on an OPTIONS turn the choices ride only
        # in the footer whose verdict is owned by the footer site, so a
        # cancellation before the footer delivers must NOT book success here.
        if (_answer_reached or answer.delivered) and not _verdict_booked and not _options_present:
            _book_success()
        # Structural release for every non-deferred exit. When the OPTIONS verdict
        # is deferred the permit is intentionally still held here and released at
        # the footer site; _release_permit stays idempotent so this is a no-op in
        # every already-released case.
        if not _options_verdict_deferred:
            _release_permit()

    # Structural release for the deferred-OPTIONS case: when the verdict was
    # deferred to the footer below, the permit is still held across these
    # decorations, so a raise or cancellation in any of them must still release
    # it. _release_permit() is idempotent, so for every already-released turn
    # this finally is a no-op.
    try:
        # Render reasoning as a condensed, subdued blockquote. When a
        # placeholder was posted above the answer, update it in place so the thread
        # reads reasoning → answer. Otherwise (the stream started before any
        # reasoning arrived) fall back to a post after the answer.
        if answer.thinking_accumulated and answer.show_thinking:
            # answer.thinking_accumulated is built from raw event text and, unlike the answer
            # stream, has no StreamRedactor upstream -- so this render is its ONLY
            # redaction. Ordering matters most here for that reason.
            thinking_mrkdwn = render_one_for_slack(answer.thinking_accumulated).text
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
            _thinking_creds, _thinking_urls = count_redaction_tags(thinking_mrkdwn)
            _cred_redactions += _thinking_creds
            _url_redactions += _thinking_urls
            thinking_block = _condense_thinking(thinking_mrkdwn)
            if answer.thinking_ts:
                try:
                    await slack.update_message(channel, answer.thinking_ts, thinking_block)
                except Exception:
                    logger.warning("Failed to update thinking message", exc_info=True)
            else:
                for part in split_message(thinking_block):
                    try:
                        await slack.post_message(channel, part, reply_ts)
                    except Exception:
                        logger.warning("Failed to post thinking message", exc_info=True)
        elif answer.thinking_ts:
            # Placeholder was posted but no reasoning was captured — remove it so
            # the thread isn't left with a dangling "💭 Thinking…".
            try:
                await slack.delete_message(channel, answer.thinking_ts)
            except Exception:
                logger.debug("Failed to delete empty thinking placeholder", exc_info=True)

        # One notice per turn, AFTER the answer (and thinking) have been posted, so it
        # reads below the text it describes. Posted as a SEPARATE threaded message
        # rather than folded into the answer: Slack has already committed the rich
        # answer via stop_stream/chat_update above and the answer text must stay
        # exactly as redacted (never relaxed, never annotated inline). Best-effort --
        # a failed notice must not turn a delivered answer into a failed turn.
        if _cred_redactions > 0 or _url_redactions > 0:
            try:
                await slack.post_message(
                    channel, redaction_notice(_cred_redactions, _url_redactions), reply_ts
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
                answer.accumulated,
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
        # The footer is decoration EXCEPT when it carries an [OPTIONS] control:
        # the trailer was stripped from the answer, so the choices ride ONLY here,
        # which makes the footer (or its fallback) answer-carrying. For such a turn
        # the verdict was deferred to this site: success is booked only once one of
        # the two OPTIONS deliveries returns, and a failure is booked if BOTH fail
        # — the choices never reached the reader, so it is not a success. The
        # fallback text is model-authored, so it passes the SAME display-safe
        # redaction the answer path uses (never a second scrubber). A footer with
        # no options is pure decoration; its failure just logs.
        _footer_ts: str | None = None
        _options_delivered = False
        try:
            _footer_ts = await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
            _options_delivered = True
        except Exception:
            logger.warning("Slack footer post_blocks failed for %s", session_key, exc_info=True)
            if options:
                try:
                    # The choices are model-authored, so the fallback carries the
                    # SAME display-safe obligation as the answer it stands in for:
                    # scan against what Slack RENDERS, not only the literal bytes.
                    # A bare exfil+credential scan misses ``[AKIA](url)REST`` and
                    # ``<!channel>`` obfuscated behind markup that Slack collapses
                    # on screen -- the primary OPTIONS blocks escape every choice
                    # and the renderer's fallback twin runs this same canonical
                    # scrub, so this path routes through it too rather than a
                    # weaker literal-only pass.
                    _fallback = redact_for_display(
                        "*Options:*\n" + "\n".join(f"• {o}" for o in options),
                        _display_redactor,
                    )[0]
                    await slack.post_message(channel, _fallback, reply_ts)
                    _options_delivered = True
                except Exception:
                    logger.warning(
                        "Slack options fallback post failed for %s", session_key, exc_info=True
                    )
        if _options_verdict_deferred:
            # The OPTIONS payload is answer-carrying: book the deferred verdict from
            # whether the choices reached the reader, then release the retained
            # permit. Idempotent helpers, so this is the single verdict for the turn.
            if _options_delivered:
                _book_success()
            else:
                await _book_failure()
            _release_permit()
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
        await _reply_by_voice(
            slack,
            channel,
            reply_ts,
            user_id,
            session_key,
            answer.accumulated,
            final_text,
            had_voice_input,
        )

        # ── Update task banner with final state ──
        # History was persisted earlier, above the OPTIONS control, so that the
        # control's staleness token names this turn rather than the one before it.
        if conversation_log and not _skip_writes:
            if consolidator and _stop_reason != STOP_REASON_CANCELLED:
                consolidator.maybe_consolidate(session_key)

        # ── Bidirectional sync: mirror to dashboard if routed to a dashboard session ──
        if linked_session_key and _dashboard_state and answer.accumulated and not _skip_writes:
            _mirror_to_dashboard(linked_session_key, text, answer.accumulated)
        # ── Auto-title Slack thread (fire-and-forget) ──
        # Claim-early-unclaim-on-failure pattern: ``try_claim`` checks and marks in one
        # synchronous step, so concurrent messages (and the transport path, which
        # claims through the same shared tracker) cannot both fire a task. If the
        # background task fails or returns SKIP, it unclaims the key so the next
        # message retries. A message arriving between claim and unclaim is
        # intentionally skipped (no duplicate).
        if not _had_error and not _skip_writes and not auto_title.is_titled(session_key):
            # The ``is_titled`` peek above is a cheap synchronous membership test on
            # the same tracker ``try_claim`` checks below: once a key is claimed or
            # titled the claim cannot be taken again, so without the peek the pin's
            # thread hop would be paid and then discarded on every later message of
            # every already-named conversation.
            #
            # Pin BEFORE claiming, and both before the task is scheduled. The pin
            # read suspends on a thread, so claiming first would leave the claim
            # held across that await with nothing scheduled yet to release it: a
            # cancellation there (``!stop``) would strand it, and the claim is
            # process-wide, so this key could not be auto-titled again until the
            # gateway restarts. The pin still precedes ``create_task``, which is
            # what closes the scheduling-tick window -- see ``pin_record``.
            #
            # The pin itself is the one taken under the permit, well above here:
            # reading it at this point would sit after the release and after the
            # Slack round-trips in between, which is the window a replacement
            # record slips through. ABSENT is the one state worth re-reading, and
            # only because a key with no record has no replacement to confuse:
            # this turn's own row has landed by now, so the re-read is what makes a
            # brand-new conversation nameable from its first exchange.
            _title_pin = _title_pin_held
            if _title_pin is None or _title_pin.state == auto_title.RECORD_ABSENT:
                _title_pin = await auto_title.pin_record(conversation_log, session_key)
            if auto_title.try_claim(session_key):
                track_background_task(
                    asyncio.create_task(
                        _maybe_auto_title_slack(
                            slack,
                            sessions,
                            channel,
                            session_key,
                            conversation_log,
                            text,
                            answer.accumulated,
                            pin=_title_pin,
                        )
                    )
                )
    finally:
        # If the verdict was deferred to the footer and this tail is torn down
        # (a raise or cancellation in a decoration) before the footer books it,
        # the choices never reached the reader, so book the failure here rather
        # than exit with no verdict. Idempotent: a no-op once the footer booked.
        if _options_verdict_deferred and not _verdict_booked:
            await _book_failure()
        _release_permit()


# ── Slack thread auto-title ─────────────────────────────────────────────
#
# The turn, the claim tracker, the tool-free stream, the prompt and the
# title-cleaning rules all live in ``messaging.auto_title``. Slack supplies the
# one thing that is genuinely per-channel — renaming the Slack thread itself.

_get_auto_title_lock = auto_title.get_lock
_build_title_prompt = auto_title.build_title_prompt


async def _reject_orphaned_tool(
    provider: LLMProvider, request_id: "str | int", *, audit: bool = True
) -> bool:
    """Reject a pending ACP permission request that we can no longer surface.

    Both the pre-approval stream-prep and the approval-prompt post happen BEFORE
    the permission is answered; if either raises, the ACP request would be left
    unanswered and the agent subprocess wedges forever (every later turn blocks
    behind it). Callers invoke this on failure, then re-raise. Swallows any
    reject failure, and audit failure after a successful rejection, so the
    original error still propagates. ``audit=False`` is for a caller whose
    decision already has its SEL row (the audit-first deny sites): the wire
    still gets answered, but the ledger is append-only and a second row for
    one decision would be a duplicate nothing reconciles.
    """
    try:
        await provider.reject_tool(request_id)
    except Exception:
        logger.warning("Failed to reject orphaned tool %s", request_id, exc_info=True)
        return False
    # The fallback arms re-raise past the normal permission audit, so record
    # the denial here: a rejection that reached the wire but never reached the
    # audit trail is a silent gap in a security control.
    if not audit:
        return True
    try:
        sel().log_tool_invocation(
            session_key="",
            source="slack",
            tool_name="",
            outcome="rejected",
            request_id=request_id,
            metadata={"reason": "orphaned_fallback_reject"},
        )
    except Exception:
        logger.warning("Failed to audit orphaned tool %s", request_id, exc_info=True)
    return True


async def _steer_host_deny(
    provider: Any, event: Any, reason: str, *, cause: str, audited: bool
) -> None:
    """Tell the model, in-band, that the HOST denied this call -- not the person.

    A rejected permission reaches the model as kiro-cli's fixed "User denied
    tool execution", so without this it reads a refusal that never happened.
    Awaited immediately BEFORE a host-deny ``reject_tool`` in this module: while
    the permission request is unanswered the turn is provably in flight, which
    is what gets the notice queued rather than dropped (``kiro_crew.deny_notice``).
    The Slack handler has two host denies -- a hook ``deny`` on the message
    path (``DENY_CAUSE_POLICY``, the hook's reason) and the approval prompt
    expiring unanswered (``DENY_CAUSE_APPROVAL_TIMEOUT``). *cause* is REQUIRED
    because the wrong noun sends the model the wrong way. The two genuine USER
    rejections (a Deny click in ``handle_interaction``) and the teardown-only
    ``_reject_orphaned_tool`` must NOT call this: there kiro-cli's wording is
    the truth, and "this was NOT a user action" would be a lie.
    ``test_messaging_deny_notice`` walks the file to keep both halves honest.

    *reason* may echo agent-authored text (a hook's reason quotes the matched
    path), so it is redacted here; the shared helper redacts the title.
    Best-effort by construction: ``steer_refusal_notice`` probes the capability
    and swallows every failure, so a backend without a steer channel behaves
    exactly as before and the caller's reject always runs.

    Cancellation mid-steer (teardown) must still answer the wire: a stranded
    ``session/request_permission`` blocks the subprocess forever and wedges
    every later turn behind it. The reject is scheduled as a strongly referenced
    referenced task and awaited through ``asyncio.shield`` so it is stepped
    while this coroutine unwinds; ``_reject_orphaned_tool`` retrieves its
    exception so teardown stays quiet. *audited* is REQUIRED and says whether
    the caller wrote the decision's SEL row BEFORE this await (the hook deny
    does) or writes it after the wire (the approval-timeout arm, whose
    caller audits both outcomes once the request is answered). The orphan
    reject audits only in the second case: the SEL ledger is append-only,
    and a decision already on it must not gain a second row nothing
    reconciles.
    """
    safe_reason, _ = redact_exfiltration_urls(reason or "")
    safe_reason, _ = redact_credentials(safe_reason)
    try:
        await steer_refusal_notice(
            provider,
            str(getattr(event, "title", "") or ""),
            safe_reason,
            cause=cause,
            bound_secs=_STEER_NOTICE_BOUND_SECS,
        )
    except asyncio.CancelledError:
        reject = asyncio.ensure_future(
            _reject_orphaned_tool(provider, event.request_id, audit=not audited)
        )
        _orphan_rejects.add(reject)
        reject.add_done_callback(_orphan_rejects.discard)
        with contextlib.suppress(BaseException):
            if await asyncio.shield(reject):
                Stats().inc_tool_denial()
        raise


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
        # shield: on timeout, wait_for would otherwise CANCEL the future, and a
        # click that claimed the entry just before the deadline could then
        # never deliver its real outcome (its set_result guards on done()).
        outcome = await asyncio.wait_for(asyncio.shield(pending.future), timeout=_APPROVAL_TIMEOUT)
    except asyncio.TimeoutError:
        outcome = _OUTCOME_REJECTED
        # Claim the decision BEFORE awaiting anything: while the entry stays
        # registered, a Slack click landing inside the steer window would take
        # the live-approval branch and answer the same permission request a
        # second time. The pop's result says who won: a click that claimed the
        # entry first is answering (or already answered) the request itself, so
        # steering "expired unanswered" then would hand the model a false
        # cause. The finally pop is idempotent, and a late click hits the
        # already-resolved path.
        claimed = _pending_approvals.pop(key, None) is not None
        # Steer FIRST, reject SECOND: while the permission request is still
        # unanswered the turn is provably in flight, so the notice is queued
        # rather than dropped, and the model learns the denial was an expired
        # prompt instead of concluding a human refused the call, matching the
        # dashboard chat runner's host-decline arms. On Slack the driver stops
        # rendering after a rejection, so this corrects the model-side
        # transcript attribution only; the notice's continue-guidance has no
        # Slack consumer. Best-effort: _steer_host_deny (capability probe,
        # redaction, build, bounded send -- the same shared helper the
        # messaging TurnDriver uses) swallows every failure, so the reject
        # below still runs; a cancellation mid-steer schedules the orphan
        # reject itself before re-raising, so teardown still answers the wire.
        if claimed:
            await _steer_host_deny(
                provider,
                event,
                "the Slack approval prompt went unanswered for "
                f"{max(1, round(_APPROVAL_TIMEOUT))}s",
                cause=DENY_CAUSE_APPROVAL_TIMEOUT,
                # The caller audits this outcome after the wire is answered;
                # a cancellation here would skip that row, so the orphan
                # reject writes it.
                audited=False,
            )
        if claimed:
            # Only the claim winner answers the wire. A lost claim means a
            # click is answering (or answered) this request itself; a second
            # answer would hit the ACP client's popped-options fallback, whose
            # cancelled outcome cancels the WHOLE turn. The click's own wire
            # failure cannot strand the request either: handle_interaction
            # answers the wire itself when its approve/reject raises.
            await provider.reject_tool(event.request_id)
            Stats().inc_tool_denial()
        else:
            # A click beat the deadline and owns the answer. The future was
            # shielded from the timeout's cancellation, so it still carries the
            # click's REAL decision: await it until the click resolves it, and
            # report THAT. No bound and no fabricated fallback: the click is the
            # sole responder and every way it can end resolves this future --
            # its approve/reject completes (set_result in handle_interaction),
            # its write raises (handle_interaction self-answers the wire, then
            # set_result), or the backend stops reading stdin for good, which
            # the ACP client's tool-stall watchdog turns into a transport close
            # that raises out of the parked write and lands on the same path.
            # Returning "rejected" on a timer instead would close this stream
            # over a tool the person approved and that still executes.
            outcome = await asyncio.shield(pending.future)
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
    # (that would answer the JSON-RPC request twice). Anything that isn't an
    # explicit reject approves THIS call; a Trust click additionally widens the
    # session, and only a widening that actually took counts as an approval.
    linked_entry = _linked_approvals.get(key)
    if linked_entry is not None:
        return _resolve_linked_click(linked_entry, key, action_id, user_id, sessions)

    # Claim-before-await, symmetric with the timeout arm: popping here (not
    # at the end) means a timeout firing while this click awaits the wire
    # sees a lost claim and stays entirely off it — only the claim winner may
    # answer, because a second answer to the same request id lands in the ACP
    # client's popped-options cancelled-outcome fallback and cancels the whole
    # turn. It also means the timeout arm's own pop cannot leave this path
    # deleting a missing key.
    pending = _pending_approvals.pop(key, None)
    if not pending:
        # Approval already resolved (approved/rejected/timed out).
        # For trust clicks, still set trust using the thread as session key.
        # Replicate session_key derivation from handle_message: thread_ts,
        # then check for linked dashboard session override.
        if action_id == _ACTION_TRUST and thread_ts:
            return await _grant_late_trust(channel, thread_ts, user_id, slack, sessions)
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

    # Everything below runs with the entry CLAIMED: the pop above means no
    # later claimer exists, and _request_approval's lost-claim arm is awaiting
    # ``pending.future`` unbounded on the promise that every way this click can
    # end resolves it. The wire calls kept that promise through their own
    # fallback arms, but the synchronous bookkeeping between the claim and
    # ``set_result`` (trust grant, audit, stats) could raise and return with
    # the wire unanswered and the future unresolved — parking that waiter
    # permanently. One guard over the WHOLE claimed region keeps the promise
    # on every exit; it subsumes the two per-wire-call fallback arms it
    # replaces. approve_tool pops the recorded options before sending, so the
    # guard's fallback reject can land as a cancelled outcome (ends the turn's
    # remaining tool calls) — still strictly better than a wedged subprocess.
    floor_refused = False
    try:
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
                    return _ACTION_REJECT
                elif pending.session_key:
                    add_trusted_session(pending.session_key, sessions)
                    logger.info("Trust mode ON for session %s", pending.session_key)
                else:
                    logger.warning(
                        "No session_key on pending approval %s; approving without trust", key
                    )
            approval_sent = True
            if pending.provider:
                approval_sent = await pending.provider.approve_tool(pending.request_id)
            if not pending.future.done():
                pending.future.set_result(
                    _OUTCOME_APPROVED if approval_sent is not False else _OUTCOME_REJECTED
                )
            if approval_sent is not False:
                Stats().inc_tool_approval()
            else:
                # The transport's gate refused the call: the card must not
                # be relabelled as approved.
                floor_refused = True
            sel().log_api_access(
                caller=user_id,
                operation="slack.interactive.approval",
                outcome=(
                    "allowed" if approval_sent is not False else OUTCOME_REJECTED_TRANSPORT_FLOOR
                ),
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
    except BaseException:
        # An unresolved future is the signal the exit was abnormal: both arms
        # resolve it immediately after their wire call, so reaching here with
        # it pending means the wire may be unanswered and the lost-claim
        # waiter is still parked. Answer best-effort and release the waiter
        # before propagating; _reject_orphaned_tool swallows its own failure,
        # so the original error still surfaces.
        if not pending.future.done():
            if pending.provider:
                await _reject_orphaned_tool(pending.provider, pending.request_id)
            pending.future.set_result(_OUTCOME_REJECTED)
        raise

    return _ACTION_REJECT if floor_refused else action_id


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

    *cmd_text* is the message as typed; ``sessions all`` / ``sessions ended``
    asks for rows the user has dismissed with End, which are otherwise left out.
    """
    include_ended = sessions_include_ended(cmd_text)
    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline.
    # Mirrors the slash and Home Tab error-path patterns.
    try:
        rows = await _collect_recent_sessions_off_loop(
            sessions,
            limit=_message_surface_limit(slack_cfg().slack.sessions_limit),
            include_ended=include_ended,
        )
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


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.slack.handler.<name>`` reaches it wherever it lives; see
# :mod:`kiro_crew.slack.handler_runtime`. Run once, after this body has bound every name.
_handler_runtime.compose(
    globals(),
    (
        _owner_access,
        _owner_approvals,
        _owner_commands,
        _owner_finalize,
        _owner_inbound,
        _owner_reactions,
        _owner_stream,
        _owner_turn_context,
        _owner_voice,
    ),
)
