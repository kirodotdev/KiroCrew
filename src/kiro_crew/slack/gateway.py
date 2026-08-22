"""Slack Socket Mode gateway orchestrator for KiroCrew.

Manages the lifecycle of all runtime services: session manager, cron
scheduler, context builder, heartbeat, subagents, task runner, dashboard,
and the Slack Socket Mode connection.

Event routing, interactive button handling, and allowlist management
live in sibling modules:

- ``events``        — Socket Mode event dispatch + dedup
- ``interactions``  — Block Kit button routing
- ``allowlist``     — tracking-channel join prompts + config persistence
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import importlib
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import socket
import sys
import threading
import time
import uuid
import webbrowser
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient

import kiro_crew
import kiro_crew.crash_guard as crash_guard
from kiro_crew import agent_scratch, beacon, dep_sync, platform_compat, shutdown_event
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.agents_janitor import sweep_agents_dir
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.autonudge import enabled as autonudge_enabled
from kiro_crew.autonudge import (
    is_channel_key,
    runtime_budget_exceeded,
)
from kiro_crew.beacon import distribution
from kiro_crew.channel_history import ChannelHistory
from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import KiroCrewConfig
from kiro_crew.config.loader import (
    CRED_DISCORD_BOT_TOKEN,
    CRED_MICROSOFT_APP_ID,
    CRED_MICROSOFT_APP_PASSWORD,
    CRED_MICROSOFT_APP_TENANT_ID,
    CRED_OWNER_ID,
    CRED_SLACK_APP_TOKEN,
    CRED_SLACK_BOT_TOKEN,
    CRED_TELEGRAM_BOT_TOKEN,
    CRED_WEBEX_BOT_TOKEN,
    CRED_WECOM_BOT_ID,
    CRED_WECOM_SECRET,
    CRED_WEIXIN_TOKEN,
    _session_work_dir,
    build_provider_factory,
    config_dir,
    data_home,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.constants import DATA_WARNING, SUBAGENT_COMPLETION_META_KEY
from kiro_crew.context import ContextBuilder
from kiro_crew.context_management import summarize_result
from kiro_crew.cron import (
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    CronJob,
    CronService,
    CronStoreBusy,
    build_cron_session_context,
    effective_wake_budget,
)
from kiro_crew.cron_script import run_command_sandboxed, run_script_sandboxed
from kiro_crew.dashboard import cautious_boot, start_dashboard
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_runner import _resolve_channel_target, _run_chat
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    dashboard_slot_key,
    mint_options_token,
    remember_slack_options,
    subagent_event_slot,
)
from kiro_crew.dashboard.cron_inject import (
    context_meter_reading,
    inject_cron_result_to_dashboard,
)
from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES
from kiro_crew.dashboard.handlers.autonudge import render_nudge_message
from kiro_crew.dashboard.handlers.messaging import _rehydrate_slot_from_history
from kiro_crew.dashboard.handlers.updates import remediation_command as _remediation_command
from kiro_crew.dashboard.handlers.usage import (
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
)
from kiro_crew.dashboard.origin import (
    build_dashboard_url,
    format_dashboard_urls,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.stale_asset_watchdog import run_stale_asset_watchdog
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    DashboardState,
)
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn
from kiro_crew.embeddings import (
    embedding_model_is_custom,
    get_shared_embedder,
    make_sync_embed_fn,
    model_file_present,
    reconcile_store_embedding_space,
    start_background_model_download,
    store_embedding_space_is_stale,
)
from kiro_crew.executors import (
    CronQueueTimeout,
    cron_gate_budget,
    maintenance_executor,
    run_in_cron_gate_pool,
    run_in_cron_pool,
    run_in_embed_pool,
    subprocess_executor,
)
from kiro_crew.frontend import build_frontend_async
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS
from kiro_crew.heartbeat import (
    HEARTBEAT_TASK_TIMEOUT_SECS,
    HeartbeatService,
    is_keep_response,
    strip_keep_sentinel,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HookManager, HooksConfig, hooks_config_from_config_dict
from kiro_crew.learn import LessonStore
from kiro_crew.llm_helpers import (
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    acp_error_is_transient,
    provider_last_turn_usage,
    save_conversation_turn_off_loop,
    stream_and_collect,
    transient_retry_delay,
)
from kiro_crew.mcp_cron import vet_job_at_fire_time
from kiro_crew.mcp_gateway import is_gateway_supported
from kiro_crew.mcp_gateway.manager import (
    GatewayManager,
    GatewaySpec,
)
from kiro_crew.mcp_gateway.resolve_once import prefetch as resolve_prefetch
from kiro_crew.mcp_gateway.rewriter import (
    default_socket_path,
    resolve_overlay_dir,
    rewrite_agents,
)
from kiro_crew.memory import MemoryStore
from kiro_crew.messaging import registry
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import (
    CHANNEL_SESSION_NAMESPACES,
    CHAT_TYPE_DIRECT,
    DM_SCOPE_UNIFIED,
    SLACK_NAMESPACE,
    ChannelLink,
    channel_namespace_of,
    parse_session_key,
)
from kiro_crew.messaging.renderer import chunk_text
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.platform import boot_platform
from kiro_crew.platform.context import (
    PlatformCompositionError,
    current_context,
    redact_via_context,
    safe_context_call,
)
from kiro_crew.platform.governance_profiles import (
    HOST_SESSION_KEY,
    audit_governance_degraded,
    governance_permits,
)
from kiro_crew.platform.update_capability import (
    CHECK_SUCCEEDED,
    CHECK_UNCHECKED,
    EXTERNALLY_MANAGED_STAMPS,
)
from kiro_crew.providers.base import LLMEvent
from kiro_crew.safety_override import safety_override
from kiro_crew.sandbox import ensure_agents_slice_limits, warm_backend
from kiro_crew.security import redact, redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.service.common import restart_command_hint
from kiro_crew.session import HEARTBEAT_KEY, SessionManager
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.format import (
    build_cron_ack_block,
    build_options_blocks,
    escape_mrkdwn,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.handler import (
    _get_agent_for_session,
    build_timing_footer,
    is_thread_incognito,
    is_thread_temporary,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.retry import open_dm_with_retry
from kiro_crew.slack.scope_probe import warn_unreadable_tracked_channels
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    DIGEST_HOLD_SECS,
    INJECTION_TIMEOUT,
    SubagentInfo,
    SubagentManager,
    ToolApprovalCallback,
    resolve_max_subagents,
)
from kiro_crew.subagent_completion_meta import (
    OUTCOME_FAILED,
    OUTCOME_OK,
    OUTCOME_STOPPED,
    single_completion_meta,
    wave_chunk_meta,
    wave_final_meta,
)
from kiro_crew.taskrunner import TaskRunner

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.imessage.client import IMessageClient
    from kiro_crew.messaging.registry import ChannelDescriptor
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.subagent_scale import SubagentEventCoalescer
    from kiro_crew.task_models import Task
    from kiro_crew.teams.client import TeamsClient
    from kiro_crew.telegram.client import TelegramClient
    from kiro_crew.webex.client import WebexClient
    from kiro_crew.wecom.client import WeComClient
    from kiro_crew.weixin.client import WeixinClient


async def _persist_turn_row(
    client: Any,
    session_key: str,
    *,
    provider: str,
    surface: str,
    agent_fallback: Callable[[], str],
    t0: float,
) -> None:
    """Persist one per-turn usage row for a background dispatch surface.

    Extracted so the heartbeat and monitor surfaces — each with a success and a
    timeout twin that were byte-identical copies — share one implementation
    instead of cloning the block a fourth (and fifth, sixth…) time (issue
    #1086, following the usage-row wiring from issue #647). Best-effort: a
    persistence failure is logged at debug and never propagates into the
    background loop, since a dropped analytics row must not abort a live turn.

    ``agent_fallback`` is a zero-arg callable, invoked INSIDE the try/except and
    only when ``read_effective_agent`` yields nothing — preserving the original
    short-circuit (``read_effective_agent(client) or _get_agent_for_session(key)``)
    so a cold-cache ``KiroCrewConfig.load()`` neither runs on every turn nor
    escapes the best-effort guard.

    NOTE: ``test_turn_duration_recorded.py`` counts ``persist_token_record_async``
    call sites per file and requires every one to pass ``elapsed_ms``. This
    helper is the single heartbeat/monitor call site; the two cron sites persist
    directly (they carry a ``model`` argument). Adding a new surface that
    bypasses this helper changes the count and fails that guard by design.
    """
    try:
        _used, _window = read_context_tokens(client)
        await persist_token_record_async(
            session_key,
            "",
            provider_last_turn_usage(client),
            provider=provider,
            surface=surface,
            agent=read_effective_agent(client) or agent_fallback(),
            context_used=_used,
            context_window=_window,
            elapsed_ms=int((time.monotonic() - t0) * 1000),
            model_source=client,
        )
    except Exception:
        logger.debug("usage row (%s) persist failed", surface, exc_info=True)


# Chunked wave-digest size: every multi-task wave delivers its completed
# results to the parent in digest CHUNKS of this many members (queue-style —
# each chunk is one injection turn), with a final partial chunk when the wave
# closes. A 60-agent wave = 6 digest turns spread across the wave's runtime
# instead of 60 per-agent turns (the parent-context flood at scale) or one
# straggler-gated mega-digest at the very end. Single-task spawns have no
# batch identity and keep the plain per-agent injection.
# Tunable via KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE. Guarded parse: a malformed
# value must never crash gateway import — fall back to the default and clamp
# to a sane positive range.


def _digest_chunk_size() -> int:
    try:
        return max(1, min(int(os.environ.get("KIROCREW_SUBAGENT_DIGEST_CHUNK_SIZE", "10")), 1000))
    except (TypeError, ValueError):
        return 10


SUBAGENT_DIGEST_CHUNK_SIZE = _digest_chunk_size()


def _injection_slot_busy(slot: Any) -> bool:
    """True when *slot* already owns a turn a new injection must wait behind.

    ``slot.running`` alone is not enough. A just-dispatched injection parks in
    ``bounded_chat_turn``'s off-loop timeout resolution before ``_run_chat``
    starts, and only the live ``slot.task`` — assigned synchronously at
    dispatch — records that claim. A slot whose ``running`` is not derived
    from ``task`` (test doubles, duck-typed slots) reads such a window as
    idle, so a later digest chunk takes the idle branch: it appends in
    whichever order the dispatch hops resolve (not FIFO under CPU load) and
    assigns ``slot.task`` over the earlier chunk's still-pending task instead
    of awaiting it. Consulting the claim directly keeps chunk delivery FIFO
    regardless of how ``running`` is implemented or when the hop resolves.
    """
    task = slot.task
    return bool(slot.running) or (task is not None and not task.done())


# Whole-callback transient retries for the cron LLM path (session acquire /
# client creation / context assembly), mirroring the subagent path's budget.
# In-stream transient errors are retried separately by stream_and_collect.
_CRON_TRANSIENT_RETRIES = 2

# Continuation prompt for the one-shot post-token resume below. Reuses the
# subagent path's constant verbatim (gateway.py already imports from
# kiro_crew.subagent) instead of adding a third hand-maintained copy next to
# the dashboard's _POSTTOKEN_RECOVER_MSG — see the PARITY NOTE in
# dashboard/chat_runner.py. The cron result is delivered once at the end of
# the turn, so the preserved partial is concatenated with the continuation
# instead of being re-shown to a live viewer.
_CRON_POSTTOKEN_CONTINUE_MSG = _TRANSIENT_CONTINUE_MSG

logger = logging.getLogger(__name__)

# Full chat turn timeout — tool calls, multi-step reasoning, spawning.
# More generous than INJECTION_TIMEOUT (default 900s, tunable via
# KIROCREW_INJECTION_TIMEOUT) which only covers a single injected continuation turn.

# Max retries for injecting subagent results into parent sessions.
_MAX_INJECT_ATTEMPTS = 2

# Per-turn hard deadline for an unattended AutoNudge turn in a channel session
# (Slack/Discord babysit loops). Mirrors HEARTBEAT_TASK_TIMEOUT_SECS / cron's
# _JOB_TIMEOUT_SECS: no human is present, so the turn MUST be bounded.
_NUDGE_TURN_TIMEOUT = 1800.0  # 30 min

# Budget for awaiting the in-flight run-marker write during shutdown. Bounded
# so a stalled write can never eat into GRACEFUL_SHUTDOWN_SECS (which saves
# active slots) — the marker is best-effort, the slot save is not.
_MARKER_WRITE_WAIT_SECS = 5.0

# Approval sources that run UNATTENDED (no human responder). These deny-fast on a
# short window instead of burning the full 2h human-approval window. Subagent
# approvals are NOT background: they route to the dashboard where the spawning
# human is present (via the parent slot), so they keep the long interactive window.
_BACKGROUND_APPROVAL_SOURCES = frozenset({"cron", "heartbeat", "taskrunner", "autonudge", ""})

# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000


def _heartbeat_slack_parts(title: str, result_text: str) -> list[str]:
    """Render a heartbeat completion into postable Slack parts.

    Shared by all four heartbeat delivery branches so they cannot drift. Two
    things it fixes relative to the per-branch f-string it replaces:

    - **It splits.** Those branches posted one unsplit message, and Slack
      rejects anything past ~40,000 characters outright -- so a long heartbeat
      result was silently lost rather than truncated.
    - **It redacts around the transform.** ``_deliver_result`` redacts
      ``result_text`` at its head, but ``to_slack_mrkdwn`` strips ANSI escapes
      and that strip can reassemble a credential the escapes had broken up.
      Redacting again after conversion is what closes that.

    The ``💓 *title*`` caption goes through ``header=``, which redacts it without
    converting (it is already Slack mrkdwn) and charges it against the limit.
    """
    return render_for_slack(result_text, header=f"💓 *{title}*\n\n")


# Volatile patterns stripped before hashing cron results for dedup.
_VOLATILE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"  # ISO timestamps
    r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",  # UUIDs
    re.IGNORECASE,
)
_EPOCH_RE = re.compile(r"\b\d{10,13}\b")
_EPOCH_WINDOW_SECS = 300  # strip epoch values within ±5 min of now
_SUCCESS_REMINDER_SECS = 86400  # post "still succeeding w/ same result" reminder every 24h
_FAILURE_REMINDER_SECS = 3600  # re-alert still-failing cron every 1h (louder than success dedup)
# Cap on the failure reason carried into a user-facing alert. Matches the cap
# _apply_gate_verdict already puts on job.last_error, so the bell and the cron
# row cannot disagree about how much of a long traceback the user is shown.
_CRON_FAILURE_DETAIL_CAP = 500


# Tool-name prefixes treated as read-only by the --approval reads flag.
# Matched against the leading verb token of an event.title (e.g. "Read foo.txt"
# -> "read"). Conservative list — anything not on it falls through to the
# standard approval flow.
_READ_ONLY_TOOL_PREFIXES = (
    "read",
    "list",
    "get",
    "search",
    "find",
    "describe",
    "show",
    "view",
    "fetch",
    "query",
    "grep",
    "ls",
    "cat",
    "head",
    "tail",
)

# Tokens that disqualify a tool from auto-approval even if its leading
# verb is in _READ_ONLY_TOOL_PREFIXES. After splitting the title on
# whitespace/punctuation/underscore/dash, any resulting token that exactly
# matches one of these entries causes rejection. Catches compound names
# a third-party MCP author might pick (e.g. read_or_write, find_and_replace,
# get_or_create) where the read prefix masks a write capability. Fail
# closed on ambiguity.
_WRITE_INDICATORS = (
    "write",
    "delete",
    "create",
    "destroy",
    "remove",
    "update",
    "modify",
    "replace",
    "set",
    "put",
    "post",
    "exec",
    "execute",
    "run",
    "rm",
    "rmdir",
    "drop",
    "patch",
    "send",
    "publish",
    "save",
    "edit",
    "kill",
    "terminate",
)


def _is_read_only_tool(event_title: str) -> bool:
    """Return True if event_title looks like a read-only tool invocation.

    Used by --approval reads to auto-approve a conservative set of read
    verbs while still gating writes. Two-stage check:

    1. Leading token (before any whitespace/punctuation) must be in
       _READ_ONLY_TOOL_PREFIXES.
    2. After splitting the title on whitespace/punctuation/underscore/dash,
       no resulting token may exactly match one in _WRITE_INDICATORS — catches
       compound names like read_or_write, find_and_replace, get_or_create.
       Exact token equality, not substring containment: ``setter`` does not
       match ``set``.

    Fails closed on ambiguity.
    """
    if not event_title:
        return False
    lowered = event_title.strip().lower()
    if not lowered:
        return False
    # Tokenize on whitespace, underscores, dashes, and common punctuation
    # so compound names like read_or_write break into ["read", "or", "write"].
    tokens = [t for t in re.split(r"[\s_\-:()/.,]+", lowered) if t]
    if not tokens:
        return False
    leading = tokens[0]
    if leading not in _READ_ONLY_TOOL_PREFIXES:
        return False
    # Reject if any token (other than the leading verb itself) is a known
    # write indicator. Catches read_or_write, find_and_replace, etc.
    if any(token in _WRITE_INDICATORS for token in tokens):
        return False
    return True


# ── Heartbeat tool allowlist ──
#
# Heartbeat sessions run unattended on a timer.  Tool approval cannot prompt
# a human, so we maintain a strict explicit allowlist of read-only /
# observation tools that auto-approve.  Anything outside the list is rejected
# with a SEL audit event so operators can see what got blocked and tune the
# list.
#
# The allowlist is **name-based and exact-match only** (no verb/heuristic
# fallback).  Heartbeat polls untrusted external content (CR comments, ticket
# bodies) where prompt-injection could try to coax the agent into write
# actions; a verb-based fallback could be widened by a clever name like
# ``get_all_credentials`` or ``list_env_secrets`` from a malicious MCP server
# or injected payload.  Exact-match enforcement is auditable and cannot be
# widened that way.
#
# When a legitimate new read tool needs to run in heartbeat, operators
# observe the SEL ``denied`` events for it and explicitly add the name to
# this set.  This is deny-by-default per the security-controls guideline.
HEARTBEAT_SAFE_TOOLS = frozenset(
    {
        # Local / built-in read tools
        "Read",
        "Grep",
        "Glob",
        # Workspace exploration
        "WorkspaceSearch",
        # KiroCrew-core reads (no side effects)
        "learn_list",
        "cron_list",
        "spawn_list",
        "spawn_status",
        "artifact_list",
        "artifact_get",
        "artifact_versions",
        "local_knowledge_search",
    }
)


_HEARTBEAT_STATUS_PREFIXES = ("Running: ",)


def _is_heartbeat_safe_tool(event_title: str) -> bool:
    """Return True if *event_title* is safe to auto-approve in a heartbeat task.

    Strict exact-name match against ``HEARTBEAT_SAFE_TOOLS``.  No verb-based
    fallback — heartbeat polls untrusted external content (CR comments,
    ticket bodies) where prompt-injection could try to widen approval via a
    clever read-shaped tool name (``get_all_credentials``,
    ``list_env_secrets``, etc.).  Per security-controls deny-by-default:
    reject unless positively confirmed.

    Title normalization (applied before the set lookup):

    1. Strip leading status prefix (e.g. ``Running: ``).
    2. Strip ACP ``mcp__<server>__<Tool>`` prefix.
    3. Strip runtime ``@<server>/<Tool>`` prefix — kiro-cli titles arrive as
       ``Running: @example-mcp/SomeTool`` at the gateway.

    Only the **bare tool name** is tested against the frozenset.

    Returns False on empty / whitespace-only / unrecognised names.
    """
    if not event_title:
        return False
    name = event_title.strip()
    if not name:
        return False
    # Strip leading status prefix: "Running: @example-mcp/Tool" → "@example-mcp/Tool"
    for prefix in _HEARTBEAT_STATUS_PREFIXES:
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    # Preserve the server-QUALIFIED form (before the prefix is stripped) so the
    # edition allowlist can match on the full identity and avoid bare-name
    # collisions — normalized to the "@server/Tool" spelling regardless of which
    # wire form arrived ("mcp__server__Tool" or "@server/Tool").
    qualified = ""
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            qualified = f"@{parts[1]}/{parts[2]}"
    elif name.startswith("@") and "/" in name:
        qualified = name
    # Strip MCP server prefix: "mcp__example-mcp__ToolName" → "ToolName"
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            name = parts[2]
    # Strip @server/Tool prefix: "@example-mcp/SomeTool" → "SomeTool"
    if name.startswith("@") and "/" in name:
        name = name.rsplit("/", 1)[-1]
    if name in HEARTBEAT_SAFE_TOOLS:
        return True
    # Edition-contributed additions. Deferred context read via the sel.py pattern
    # so this module never imports the platform package at load time; fails closed
    # to the core set on any error.
    #
    # SECURITY — match ONLY the server-qualified "@server/Tool" identity, never a
    # bare tool name: a bare-name allowlist entry would let a DIFFERENT (or
    # compromised) MCP server expose a destructive tool with the same bare name
    # as an allowlisted read-only one, and an injected heartbeat could get it
    # auto-approved. So a title with no resolvable server (``qualified == ""``)
    # can never match an edition entry, and an edition entry that is itself a
    # bare name simply never matches any qualified title. This keeps the
    # deny-by-default boundary intact; the companion MUST pin "@server/Tool".
    if not qualified:
        return False
    empty: frozenset[str] = frozenset()
    extra: frozenset[str] = safe_context_call(
        lambda: current_context().slack_gate.heartbeat_safe_tools(),
        fallback=empty,
        log_message="heartbeat_safe_tools lookup failed; using core set only",
    )
    return qualified in extra


# Prepended to every heartbeat task_text before ``ctx_builder.build_message``.
# Inline injection survives context compaction and webhook-restored sessions
# where skill / system-prompt copies of the same instruction can drift out of
# effective context.
_HEARTBEAT_KEEP_INJECTION = (
    "[HEARTBEAT TASK — you MUST include the keyword HEARTBEAT_KEEP "
    "in your response if this task is NOT complete. Omit the "
    "keyword only when the task is fully complete.]\n\n"
)


def _build_heartbeat_hooks(user_hooks: HookManager) -> HookManager:
    """Return a HookManager scoped for heartbeat use.

    The interactive user's ``auto_approve_tools`` (e.g. ``*``, ``Write*``)
    must NEVER widen the heartbeat allowlist — ``llm_helpers._resolve_permission``
    consults ``hooks.on_tool_call()`` BEFORE the ``on_tool_approval`` callback,
    so a user-config auto-approve would bypass ``_heartbeat_approval``
    entirely (per code review).

    The heartbeat-scoped hooks keep:
      - sensitive-path deny (always-on, structural — not from user config)
      - the user's ``auto_deny_tools`` (denies are safe; users can only
        narrow, not widen, what runs in heartbeat)

    They drop:
      - ``auto_approve_tools`` (set to empty so ``HEARTBEAT_SAFE_TOOLS`` is
        the sole approval authority)
      - ``auto_replies`` / ``transforms`` / ``context_rules`` (chat-only)

    The result: every tool call in a heartbeat session takes the
    ``on_tool_approval`` branch, where ``_heartbeat_approval`` enforces
    strict allowlist + SEL audit.
    """
    user_cfg = user_hooks._config  # noqa: SLF001 — internal hooks state by design
    scoped = HooksConfig(
        auto_approve_tools=[],
        auto_deny_tools=list(user_cfg.auto_deny_tools),
        # Denied-command opt-out state carries over: denies can only narrow what
        # runs in a heartbeat session, never widen it, so the effective built-in
        # ruleset (and user-added denies) must apply here too.
        denied_commands_disabled_ids=list(user_cfg.denied_commands_disabled_ids),
        denied_commands_disable_all=user_cfg.denied_commands_disable_all,
        denied_commands_user_added=list(user_cfg.denied_commands_user_added),
    )
    return HookManager(scoped)


class _GateTally:
    """Tool-gate outcomes accumulated over one cron run.

    A cron whose every tool call was refused still gets prose back from the
    model, so the reply text alone cannot separate "did the work" from "was
    blocked at every step". Counting both arms is what makes that verdict
    available once the turn ends. A multi-agent run tallies across the whole
    sequence, because status and history are per-run, not per-agent.

    Only an unconditional security block counts as a refusal here. A governance
    denial and an unattended-approval timeout also arrive unapproved, but they
    describe the policy state or an absent approver rather than a defect in the
    job — and a job's failure counter drives auto-pause, which is durable.
    """

    def __init__(self) -> None:
        self.refused: list[str] = []
        self.approved = 0
        self.unresolved = 0

    def note(self, title: str, approved: bool, security_blocked: bool) -> None:
        if approved:
            self.approved += 1
        elif security_blocked:
            self.refused.append(title)
        else:
            self.unresolved += 1

    @property
    def all_blocked(self) -> bool:
        """Every tool the turn attempted was security-blocked, and none ran.

        ``unresolved`` must be zero, not merely uncounted: a governance denial
        or an approval timeout alongside a security block leaves the run's real
        capability unknown — that tool might have succeeded with a looser policy
        or a present approver — so the run does not evidence a job that cannot
        work. Treating it as evidence would auto-pause a healthy job.
        """
        return bool(self.refused) and self.approved == 0 and self.unresolved == 0


def _apply_gate_verdict(job: CronJob, tally: _GateTally) -> bool:
    """Record a finished cron run's success or failure from its gate outcomes.

    Shared by both cron agent paths so their verdicts cannot drift. Mutating
    ``last_status`` is how the non-raising cron paths signal failure:
    ``CronScheduler._execute`` keeps an explicit "error" rather than
    overwriting it with "ok".

    Returns whether this call counted a failure, because a run must increment
    ``consecutive_failures`` **at most once**. On the single-agent path the
    delivery work that follows (dashboard broadcast, Slack post) runs inside a
    ``try`` whose handler counts too, so a blocked turn whose delivery then
    failed would otherwise reach the auto-pause threshold in three runs rather
    than five — pausing on arithmetic instead of on evidence.
    """
    if tally.all_blocked:
        # Nothing the model attempted was permitted, so the run accomplished
        # nothing however plausible its reply reads. A success resets
        # consecutive_failures and clears auto_paused, so recording one here
        # would keep a structurally-failing job firing on its schedule forever.
        job.last_status = "error"
        _named = ", ".join(t or "<untitled tool>" for t in tally.refused[:3])
        job.last_error = redact(
            f"all {len(tally.refused)} tool call(s) blocked by the security gate: " + _named
        )[:500]
        job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Cron '%s': auto-paused after %d consecutive failures",
                job.name,
                job.consecutive_failures,
            )
        return True
    # Clear failure dedup on any success, regardless of whether the success
    # result itself is a dup. A successful run means the job recovered — next
    # failure should always alert fresh. record_success() owns the reset now, so
    # every kind's success path gets it rather than only this one.
    job.record_success()
    return False


async def _await_cron_fire_time_gate(
    job: CronJob, *, tool_name: str, tool_kind: str
) -> tuple[str | None, bool]:
    """Await the fire-time governance gate, bounded, returning ``(reason, starved)``.

    The gate used to be awaited as a bare ``run_in_executor`` on the shared
    governance pool, with no timeout of its own, INSIDE the wake deadline
    ``_execute_with_timeout`` has already armed.  Two things followed, and a
    review lane raised both:

    * that pool is paced by REMOTE senders, so an inbound burst put an unbounded
      FIFO backlog ahead of a cron gate; and
    * a message job carries no ``_pool_queue_allowance``, so the whole backlog
      was charged to its execution budget.  When the wake deadline expired
      first, ``_execute_with_timeout`` caught the ``TimeoutError`` and returned
      normally, so ``_merge_job_result`` saw an ordinary finished run -- and a
      ``delete_after_run`` job was consumed by a run that never dispatched.

    The gate now runs on its own pool and its TOTAL wait -- queue plus execution,
    which is why the budget is split across those phases rather than given to
    each -- is bounded below the wake budget, so starvation surfaces as
    ``CronQueueTimeout`` BEFORE the deadline can fire.  That is what makes the
    retention marker reachable: ``starved`` is reported to the caller and
    ``run_never_started`` is set here, which ``cron.py``'s delete site already
    honours.  The marker is deliberately not
    ``fire_time_denied`` -- that flag also parks an at-job disabled and records
    the event as a policy denial, and pool capacity is neither.

    ``record_failure()`` is deliberately NOT called, matching both the deny path
    and the command/script starvation handlers: a fleet-capacity state must not
    auto-pause a job that never ran a line.
    """
    budget = cron_gate_budget(effective_wake_budget(job))
    # Default to RETAIN for exactly the duration of the await.  The marker used to
    # be set only INSIDE the handler below -- that is, only when the await raised
    # something that handler catches.  A recoverable event-loop stall can carry
    # wall clock past the gate's own bounds AND the wake deadline, and the
    # ``asyncio.wait_for`` in ``_execute_with_timeout`` then cancels this coroutine
    # AT the await: no handler runs, the marker stays False, that timeout is caught
    # and returns normally, and ``_merge_job_result`` consumes a
    # ``delete_after_run`` job that never dispatched.  Sizing the internal bounds
    # correctly cannot prevent it, because nothing inside the call is scheduled to
    # notice.  ``CancelledError`` is a ``BaseException`` on both interpreters in
    # this matrix, so it escapes the ``except Exception`` below and the marker
    # survives -- which is the fix.
    job.run_never_started = True
    try:
        reason = await run_in_cron_gate_pool(vet_job_at_fire_time, job, timeout=budget)
    except CronQueueTimeout as exc:
        # Scoped exactly as _run_job_isolated's own result-less clear
        # (cron.py:2852). For an agent/message job ``last_result`` is the
        # cross-run dedup context build_cron_session_context prepends as "do
        # NOT repeat", and a run starved here produced no result to replace it
        # -- clearing it made the NEXT run repeat content it had already sent.
        # Command and script jobs still clear: the prompt built for them is
        # discarded, so a carried value could only show a previous run's output
        # beside this run's status. The message fire-time deny path below never
        # clears either, so all three sites now agree.
        if job.command or job.script:
            job.clear_carried_result()
        job.last_status = "error"
        # Distinct from the pool-starvation text so the two are not conflated:
        # this run never even reached its own dispatch decision.
        job.last_error = f"fire-time gate {exc}"
        # Already True from above; kept so this handler still reads correctly on
        # its own and a later reordering cannot silently drop the retention.
        job.run_never_started = True
        try:
            sel().log_tool_invocation(
                session_key=f"cron:{job.id}",
                tool_name=tool_name,
                tool_kind=tool_kind,
                outcome="error",
                error=job.last_error,
            )
        except Exception:
            logger.debug("SEL logging failed in cron fire-time gate starvation path", exc_info=True)
        return None, True
    except Exception:
        # The gate reached its own WORK and failed there -- a failed dispatch
        # DECISION, not a run that never started.  Clearing preserves the very
        # distinction :class:`CronGateWorkTimeout` was introduced to make.
        job.run_never_started = False
        raise
    # A verdict came back, so this run reached its dispatch decision.  Clearing is
    # not optional: hold the marker past a verdict and a HEALTHY one-shot is
    # retained instead, so it fires again or never leaves the queue -- the same
    # data-integrity failure pointing the other way.  A DENY clears it too, because
    # its retention is owned by ``fire_time_denied``, whose readers also park an
    # at-job disabled; conflating them would park a job for a policy decision that
    # was never made.
    job.run_never_started = False
    return reason, False


class CronClaimTimeDenied(Exception):
    """Governance refused the job when the worker CLAIMED its execution.

    Distinct from the fire-time deny, which happens before the execution is
    submitted at all.  Deliberately a plain ``Exception``: it must not be caught
    by the :class:`CronQueueTimeout` clause (whose retention semantics are for
    runs that never got a worker) nor by ``asyncio.TimeoutError``, and it must
    be handled BEFORE the generic ``except Exception`` arm, which calls
    ``record_failure()`` and would feed a policy decision into the auto-pause
    counter.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CronClaimAbandoned(Exception):
    """The awaiter gave up before the worker reached the payload, so it must not run.

    Deliberately NOT a :class:`CronClaimTimeDenied` subclass -- an abandoned call
    is a deadline, not a policy decision, and recording it as a denial would
    misreport it.  Deliberately NOT a :class:`CronQueueTimeout` or
    ``asyncio.TimeoutError`` subclass either: by the time this is raised the
    awaiter has already left, so nothing catches it and it exists to say in the
    logs which of the two timeout shapes happened.
    """


class _ClaimHandoff:
    """Serialises a worker starting its payload against its awaiter giving up.

    :func:`run_in_cron_pool` reaches its execution phase only once a worker has
    CLAIMED the call, and a thread cannot be interrupted -- so when that phase
    times out the submitted callable keeps running.  That was tolerable while
    the claimed thread was already inside the sandbox, whose own ``timeout``
    bounds it.  With the claim-time vet the vet runs FIRST, so the deadline can
    land while the payload has not started, and it would then start after the
    caller's ``finally`` released the overlap guard -- running alongside the
    next fire.  ``run_in_cron_pool``'s own docstring names that harm for the
    queue phase ("reporting a queue timeout here would release the caller's
    overlap guard while the command runs, letting the next fire duplicate its
    side effects"); this closes the same hole for a deadline landing mid-vet.

    The lock is what makes the outcome DETERMINISTIC rather than a race.
    Exactly one of :meth:`claim` and :meth:`abandon` observes an unset flag, so
    a payload either starts -- and is reported as still running, which is the
    pre-existing claimed-and-running case -- or never starts at all.  Without
    it ``claim`` could read an unset flag that ``abandon`` sets an instant
    later, and the refusal would silently not happen.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._abandoned = False
        self._started = False

    def abandon(self) -> bool:
        """Record that the awaiter gave up; True if the payload had already started.

        Idempotent, because the ``finally`` that releases the overlap guard runs
        on every exit path including the ones that already called this.
        """
        with self._lock:
            self._abandoned = True
            return self._started

    def claim(self) -> bool:
        """Ask permission to start the payload.  False means refuse."""
        with self._lock:
            if self._abandoned:
                return False
            self._started = True
            return True


class CronVetOverran(Exception):
    """The claim-time vet spent more than the allowance the deadline carries for it.

    Starting the payload anyway is the harm: the remaining budget no longer
    covers the subprocess bound plus
    :data:`~kiro_crew.cron._SUBPROC_CLEANUP_ALLOWANCE_SECS`, so the deadline
    would fire with the subprocess already running -- and a thread cannot be
    interrupted, so the overlap guard would clear while it runs on and the next
    fire would duplicate its side effects.  Refusing before the payload starts
    trades a reported missed run for a silent duplicate execution.

    Deliberately NOT a :class:`CronClaimTimeDenied` subclass -- an overrun is a
    budget fact, not a policy decision, and recording it as a denial would park
    an at-job disabled for a decision never made.  Deliberately NOT a
    :class:`CronQueueTimeout` subclass either: that arm reports "never got a
    worker slot", which a vet that ran for its full bound plainly did.
    """

    def __init__(self, elapsed: float, bound: float) -> None:
        super().__init__(f"claim-time vet took {elapsed:.2f}s of a {bound:.2f}s allowance")
        self.elapsed = elapsed
        self.bound = bound


def claim_vet_bound(job: CronJob) -> float:
    """Seconds the claim-time vet may spend before its payload must be refused.

    The same bound the FIRE-time gate is held to for the same
    ``vet_job_at_fire_time`` work, so the two do not drift, and the number
    ``kiro_crew.cron._vet_allowance`` adds to the run deadline.  Read from
    :func:`cron_gate_budget` rather than kept as a literal, which is what makes
    the widened backstop below a guarantee instead of a hope.
    """
    return cron_gate_budget(effective_wake_budget(job))


def _claim_backstop(job: CronJob, subprocess_bound: int) -> float:
    """The inner ``run_in_cron_pool`` bound: subprocess + teardown + vet.

    One budget covers all three serially, so each needs a term.  ``+ 5`` used to
    be written here as a literal duplicate of
    :data:`~kiro_crew.cron._SUBPROC_CLEANUP_ALLOWANCE_SECS`; reading the constant
    keeps the teardown margin single-sourced, and adding
    :func:`claim_vet_bound` stops the vet spending the teardown's share of it.
    """
    return subprocess_bound + _SUBPROC_CLEANUP_ALLOWANCE_SECS + claim_vet_bound(job)


def _vet_at_claim_then(
    handoff: _ClaimHandoff, job: CronJob, fn: Callable[..., Any], *args: Any
) -> Any:
    """Re-vet inside the worker, immediately before the execution it authorises.

    The fire-time gate authorises a run and then the execution is submitted to
    the cron pool, whose queue wait is deliberately NOT charged to the job's
    deadline (that uncharging is this change's sibling and the point of the
    surrounding work).  So the authorisation and the use it authorises are
    separated by a wait bounded only by ``_CRON_QUEUE_WAIT_SECS`` -- and both
    inputs to the decision can change inside it:

    * a script's BODY, because ``run_script_sandboxed``'s launcher re-reads the
      file in the sandboxed child (``open`` + ``compile`` + ``exec``), so the
      bytes that run are whatever is on disk when the worker gets there, not
      the bytes the gate scanned;
    * the governance POLICY, which applies to ``command`` jobs too even though
      a command's text is already captured in ``job.command``.

    Running the vet here closes that window to nil: the queue wait now happens
    BEFORE the decision, and the decision holds at the moment of use.

    This is deliberately an ADDITIONAL vet, not a moved one.  Keeping the
    fire-time gate means a denial is still refused early and cheaply, without
    occupying a cron worker for the queue's duration, and it leaves the gate's
    starvation/retention plumbing (``gate_starved`` ->
    ``run_never_started``) untouched.  The cost is one extra governance
    evaluation, and for scripts one extra capped body read, per EXECUTED run.
    That work runs inside a worker this job already holds, so unlike gating on
    this pool it puts no policy check behind other jobs' queue -- the property
    the governance-pool split at the call sites protects.  It does count against
    the ``+5s`` backstop the call sites arm, which is why the vet must stay
    short and bounded.

    Consequence for the audit trail, by design: an EXECUTED command/script run
    now leaves TWO ``governance_decision`` events per gate (gate time and claim
    time) rather than one.  They are genuinely distinct decisions -- the second
    is the one that authorised the bytes that ran -- and
    ``vet_job_at_fire_time`` already audits every decision "in its own right so
    the SEL trail shows every permission decision that authorized this
    execution".  A reader counting events per run should expect the pair.

    Because the vet now runs BEFORE the payload inside the same budget, the
    caller's deadline can land while the vet is still going -- with the payload
    not yet started.  ``handoff`` is what stops that call dispatching anyway
    once the caller has given up and released its overlap guard; see
    :class:`_ClaimHandoff`.  The check sits immediately before the dispatch and
    nowhere earlier on purpose: the vet itself takes time, so a check made
    before it would be stale by the time it mattered.

    The vet is also BOUNDED here rather than merely asked to "stay short".  The
    caller's backstop carries an allowance for it (:func:`_claim_backstop`), and
    an allowance is only a guarantee if the thing it covers cannot exceed it --
    so a vet that overruns refuses its payload instead of starting one whose
    remaining margin no longer covers the subprocess and its teardown.  Measured
    on ``monotonic`` so a clock adjustment cannot make an overrun look fine.
    """
    started_at = time.monotonic()
    reason = vet_job_at_fire_time(job)
    if reason:
        raise CronClaimTimeDenied(reason)
    elapsed = time.monotonic() - started_at
    bound = claim_vet_bound(job)
    if elapsed > bound:
        raise CronVetOverran(elapsed, bound)
    if not handoff.claim():
        raise CronClaimAbandoned(
            f"cron '{job.name}': awaiter gave up during the claim-time vet; "
            "payload refused rather than run beside the next fire"
        )
    return fn(*args)


async def _cron_stream_with_posttoken_resume(
    client: Any, message: str, *, job_name: str, **stream_kwargs: Any
) -> tuple[str, float | None]:
    """Run a cron agent turn, resuming ONCE after a post-token transient error.

    Closes the seam between the two existing transient-retry layers:
    stream_and_collect's in-stream retry stops once tokens have streamed
    (re-running would duplicate the already-emitted output), and
    _cron_callback's whole-callback retry stops once the prompt is dispatched
    (tools may have run). A transient backend error raised AFTER the first
    token therefore failed the whole cycle even though the live session still
    holds the interrupted turn's context.

    Recovery mirrors the dashboard's post-token CONTINUE re-prompt
    (chat_runner's ``_posttoken_retry_used`` branch) and the subagent's
    ``_stream_with_transient_retry`` post-activity arm: the streamed partial is
    preserved, the SAME live session is re-prompted with a continuation
    instruction (never the original message, so completed work is not re-run),
    and the returned result is partial + continuation. The allowance is a
    strict one-shot per turn (``_resume_used``, same style as
    ``_posttoken_retry_used``): a transient error during the continuation
    propagates unchanged, so the unrecovered path records the error exactly as
    before.

    Returns ``(text, carried_credits)``. ``carried_credits`` is ``None`` when
    the turn completed without a resume; on a resumed turn it is the credits
    the INTERRUPTED prompt accumulated — snapshotted before the continuation
    prompt's ``AcpPromptStats.carry_over()`` zeroes the per-turn counter — so
    the caller's usage row can bill both prompts instead of only the
    continuation.

    Eligibility deliberately reuses ``acp_error_is_transient`` — the one
    authoritative classifier — so auth/validation failures and every other
    non-transient error propagate untouched. With NO tokens streamed the error
    also propagates untouched: that window is stream_and_collect's own retry's
    job, and by the time it raises here its budget is spent.

    Inherited tradeoffs, stated for the record (both are the mirrored owner
    decisions from chat_runner/subagent, extended here to the cron surface):

    - A side-effecting tool that was IN FLIGHT (dispatched, no completion)
      when the transient hit may be legitimately re-issued by the continuation
      turn — the CONTINUE instruction forbids re-running *completed* tools
      only. On an ``approval_mode == "auto"`` job that re-issue meets no gate
      and no human, an unattended posture narrower than the live-viewer
      surface the tradeoff was originally accepted for. Accepted: the window
      is rare (mid-flight tool AND a transient), and failing the whole cycle
      fast was exactly the behaviour this fix exists to remove.
    - The resume adds at most one bounded prompt plus one backoff sleep to the
      cycle's worst case, inside the same per-wake ``asyncio.wait_for``
      deadline. A deadline firing mid-continuation degrades exactly as a
      deadline mid-turn does today (``CancelledError`` is not caught here), so
      no remaining-budget plumbing is added for a one-shot.

    ``parts`` observes chunks across stream_and_collect's internal attempts.
    Its transient retry only fires while no text has streamed, and — a stated
    ASSUMPTION about the provider, not an enforced invariant — a prompt-busy
    error is only raised at prompt submission, before this turn's stream emits
    chunks. Under that assumption the accumulated text never contains chunks
    from an abandoned attempt.
    """
    parts: list[str] = []
    preserved = ""
    carried_credits: float | None = None
    _resume_used = False  # one-shot, same style as slot._posttoken_retry_used
    msg = message
    while True:
        try:
            text = await stream_and_collect(
                client,
                msg,
                on_chunk=parts.append,
                # The continuation call owns NO further transient budget: its
                # in-stream retry re-sends the prompt whenever no text has
                # streamed, so a mutating tool completed by the continuation
                # followed by a pre-text transient would be re-run by the
                # inner replay — amplifying the one-shot. The first call keeps
                # the default (existing pre-token behaviour, unchanged).
                retry_transient=not _resume_used,
                **stream_kwargs,
            )
            return preserved + text, carried_credits
        except Exception as exc:
            partial = "".join(parts)
            if _resume_used or not partial or not acp_error_is_transient(exc):
                raise
            _resume_used = True
            preserved = partial
            parts.clear()
            # Snapshot the interrupted prompt's billing NOW: sending the
            # continuation runs AcpPromptStats.carry_over(), which zeroes the
            # per-turn credit counter, and the caller's single post-turn read
            # would otherwise bill only the continuation.
            carried_credits = provider_last_turn_usage(client).credits
            _delay = transient_retry_delay(1)
            logger.warning(
                "Cron '%s': transient backend error after %d chars streamed — "
                "one-shot CONTINUE re-prompt of live session in %.1fs: %s",
                job_name,
                len(preserved),
                _delay,
                exc,
            )
            await asyncio.sleep(_delay)
            msg = _CRON_POSTTOKEN_CONTINUE_MSG


def _result_hash(text: str) -> str:
    """Normalize volatile data and return a 16-hex-char SHA-256 prefix.

    Strips ISO timestamps, UUIDs, and any 10–13 digit number that looks
    like an epoch timestamp (within ±5 minutes of now).  Non-epoch numeric
    IDs (account IDs, build IDs) are likely preserved because they would
    likely fall outside the time window.

    Truncated to 64 bits — sufficient for 1:1 comparison against a single
    previous hash (collision probability ~1/2^64 per comparison).
    """
    now = time.time()
    lo = now - _EPOCH_WINDOW_SECS
    hi = now + _EPOCH_WINDOW_SECS

    def _strip_epoch(m: re.Match) -> str:
        v = int(m.group())
        # 13 digits → millis, convert to seconds for comparison
        ts = v / 1000 if v > 9_999_999_999 else v
        return "" if lo <= ts <= hi else m.group()

    text = _VOLATILE_RE.sub("", text)
    text = _EPOCH_RE.sub(_strip_epoch, text)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _channel_transport_permitted(member: str) -> bool:
    """Return True only if the ``channels`` scope POSITIVELY permits *member*.

    Gates each transport's STARTUP on the same ``channels`` ScopedMap the two
    OUTBOUND chokepoints consult: outbound-send
    (``mcp_core._vet_channel_governance``) and outbound cross-surface mirroring
    (``dashboard.chat_runner._resolve_mirror_target``).  So one ``channels``
    policy governs a transport consistently at connect and on every outbound
    path — this gate is the connect-time member.  INBOUND receive is gated
    separately and per-message by ``messaging.identity.channel_inbound_permitted``
    (called at the top of each dispatcher's ``handle_message``), so a deny added
    after connect stops dispatch without a restart.  *member* is the transport's
    ``channel_type`` (``slack`` / ``wecom`` / ``telegram`` / ``discord`` /
    ``webex``) — the IDENTICAL member id the outbound + inbound gates use, so one
    allowlist covers them all.  **Slack is NOT exempt**: it is gated in
    ``_connect_slack`` (which also drops the socket client on a deny so nothing
    can reconnect it), while the other four are gated in
    ``_start_channel_transports``.

    HOST-side resolution mirrors the canonical
    ``apps.manager._app_activation_denied`` template:

    * ``session_key=HOST_SESSION_KEY`` — starting a transport is an operator/host
      action, so it is governed by the policy ceiling AND any ``bind: {type:
      surface, id: host}`` profile.  An empty key would classify to surface
      ``unknown`` and silently ignore a host profile (and historically
      mis-classified to ``slack``); the same fix ``apps/manager`` and
      ``slack/enterprise.py`` apply.
    * Both the DENY and the ALLOW decision are audited here via
      ``sel().log_governance_decision`` — ``governance_permits`` audits only its
      own degrade, not a normal permit/deny, so the caller owns both.

    Default-build invariant: with no policy governing ``channels`` (the standard
    open-source case) ``governance_permits`` returns a permitting Decision, so
    every ENABLED transport starts exactly as before — byte-identical behavior.

    Connect-time + inbound: this gate is the CONNECT-time member. A separate
    per-message inbound gate (``messaging.identity.channel_inbound_permitted``,
    called at the top of each dispatcher's ``handle_message``) rechecks the same
    ``channels`` policy on every inbound message, so a host-profile deny added
    AFTER a transport connected stops dispatching without a restart. Together they
    cover connect + inbound; the outbound chokepoints cover sends.

    Audit + error posture (exact):

    * GOVERNED allow (a policy/profile governs ``channels`` → ``rule !=
      "default"``): **audit-or-deny**. The allow SEL is written with
      ``critical=True`` (synchronous + raising), so a persistence failure
      (unwritable SEL / full disk) propagates to the outer ``except`` and DENIES
      the start — a policy-governed transport never connects unaudited. This is
      why ``critical`` is required: the default background writer SWALLOWS disk
      failures, so a best-effort allow-audit would let the transport connect even
      when its audit record never landed.
    * UNGOVERNED allow (no policy governs ``channels`` — the default OSS build →
      ``rule == "default"`` / ``_PERMIT_NOT_GOVERNED``): **best-effort**
      (``critical=False``). OSS transport availability must never depend on SEL
      disk health when the operator configured no governance at all.
    * DENY: best-effort audit (the transport is not starting either way).
    * ERROR: **fail-closed**. A transport is an externally-reachable network
      surface, so it starts ONLY on a positive permit. We pass
      ``governance_permits(fail_closed=True)`` so an internal
      governance-evaluation error yields a DENYING Decision, and the outer
      ``except Exception`` ALSO denies (``return False`` + a ``failed_closed=True``
      degrade audit) — deny-by-default on any error, never an unaudited connect.
      This deliberately DIVERGES from ``apps.manager`` /
      ``mcp_core._vet_channel_governance`` (which fail open) because they gate
      in-process actions, not a network-reachable listener.  A
      ``PlatformCompositionError`` still propagates (a broken CPP composition must
      abort, not silently deny).
    """
    try:
        # A bare member id queries the ``channels`` ScopedMap ``members`` ruleset.
        # session_key=HOST_SESSION_KEY: honour a surface:host profile (empty key
        # → "unknown" would silently ignore it), matching apps/manager.
        # fail_closed=True: an internal governance error DENIES (network surface).
        decision = governance_permits(
            "channels", member, session_key=HOST_SESSION_KEY, fail_closed=True
        )
        if not getattr(decision, "permitted", False):
            logger.warning(
                "%s transport not started: denied by the channels governance policy (%s).",
                member,
                getattr(decision, "reason", "") or "denied",
            )
            # governance_permits does NOT audit a normal deny — the caller must.
            try:
                sel().log_governance_decision(
                    session_key=HOST_SESSION_KEY,
                    tool_name=f"start_transport:{member}",
                    scope="channels",
                    item=member,
                    outcome="denied",
                    rule=getattr(decision, "rule", ""),
                    layer=getattr(decision, "layer", ""),
                    reason=getattr(decision, "reason", ""),
                )
            except Exception:
                logger.debug("transport-start deny audit failed", exc_info=True)
            return False
        # Audit the ALLOWED decision too (a connect to an externally-reachable
        # surface is worth a positive audit trail). The disposition splits on
        # whether the ``channels`` scope was actually GOVERNED for this member:
        #   * GOVERNED allow (a policy AND/OR profile governs ``channels``):
        #     audit-or-deny. Pass critical=True so the SEL write is
        #     synchronous+raising; a persistence failure (unwritable SEL, full
        #     disk) propagates to the outer except and DENIES the start — never
        #     connect a policy-governed transport unaudited. (A background enqueue
        #     would swallow the disk failure, so critical is required to make
        #     audit-or-deny real, not just cover a synchronous raise.)
        #   * UNGOVERNED allow (no policy/profile governs ``channels``):
        #     best-effort (critical=False). OSS transport availability must NOT
        #     depend on SEL disk health when the operator has configured no
        #     governance for this scope.
        # Detect "governed" via the Decision's LAYER, not its rule. ``resolve()``
        # returns rule="rule2-intersect" for EVERY permit — including the case
        # where a policy exists but does not govern ``channels`` — so a rule-based
        # check would mis-treat that ungoverned case as governed. ``layer`` names
        # WHICH level actually carried the decision:
        #   * no policy at all   → governance_permits early-returns layer="" ;
        #   * policy, but channels ungoverned → resolve() sets layer="default" ;
        #   * channels governed  → layer is "policy" / "profile" / "both".
        # So "governed" is exactly layer ∈ {policy, profile, both}.
        governed = getattr(decision, "layer", "") in ("policy", "profile", "both")
        try:
            sel().log_governance_decision(
                session_key=HOST_SESSION_KEY,
                tool_name=f"start_transport:{member}",
                scope="channels",
                item=member,
                outcome="allowed",
                rule=getattr(decision, "rule", ""),
                layer=getattr(decision, "layer", ""),
                reason=getattr(decision, "reason", ""),
                critical=governed,
            )
        except PlatformCompositionError:
            raise
        except Exception:
            if governed:
                # audit-or-deny: a GOVERNED transport must never connect
                # unaudited. Re-raise so the outer fail-closed branch denies the
                # start (critical=True already forced a synchronous+raising write,
                # so this is a real persistence failure, not a swallowed enqueue).
                raise
            # UNGOVERNED allow: best-effort. An SEL ill-health (e.g. corrupt HMAC
            # key during sel() init/redaction) must NOT deny an ungoverned
            # transport — OSS availability does not depend on SEL disk health when
            # the operator configured no governance for this scope. Log and start.
            logger.warning(
                "%s transport: ungoverned allow could not be audited (best-effort); "
                "starting anyway",
                member,
                exc_info=True,
            )
        return True
    except PlatformCompositionError:
        raise
    except Exception:
        # Fail CLOSED (deliberate divergence from apps/manager + mcp_core, which
        # fail open): a transport is an externally-reachable network surface, so
        # an unexpected governance error must DENY the connect, not permit it.
        # Record the failed-closed degrade; wrap it so a late-import failure
        # cannot raise out of this branch and mask the deny.
        try:
            audit_governance_degraded(
                "start_transport",
                session_key=HOST_SESSION_KEY,
                scope="channels",
                failed_closed=True,
            )
        except Exception:
            logger.debug("transport-start governance degrade audit unavailable", exc_info=True)
        logger.warning(
            "%s transport not started: channels governance check errored; "
            "failing closed (deny-by-default for a network-exposed surface).",
            member,
            exc_info=True,
        )
        return False


class GatewayOrchestrator:
    """Manages the lifecycle of all gateway services.

    Responsibilities are intentionally narrow — event routing and
    interactive handling are delegated to :mod:`events` and
    :mod:`interactions` respectively.
    """

    #: The stub set the broker's last start ATTEMPT was made with, which is not
    #: the configured one: a stub change is recorded for the next gateway start
    #: and deliberately not applied in place. Anything that restarts the broker
    #: for an unrelated reason re-emits THIS set, or it silently applies a change
    #: the operator was told is pending.
    #:
    #: Written on the attempt rather than on success, because a start that fails
    #: leaves the broker down and something still has to know which set to bring
    #: up when a later restart retries it. Recording only successes would turn a
    #: transient start failure into a permanently absent broker.
    #:
    #: Declared on the class so it is total for every construction path,
    #: including the ``__new__`` fixtures that never run ``__init__`` -- a
    #: partially built orchestrator reading it must get "nothing attempted", not
    #: AttributeError.
    _mcp_stub_servers_started: frozenset[str] = frozenset()

    def __init__(
        self,
        cfg: KiroCrewConfig,
        *,
        no_dashboard: bool = False,
        no_crons: bool = False,
        no_open: bool = False,
        port_override: str | None = None,
        json_ready: bool = False,
        approval_mode: str | None = None,
        test_mode: bool = False,
    ) -> None:
        # NOTE: test_heartbeat_prompt_deliver.py creates instances via __new__
        # (bypassing __init__). Update that fixture if new attributes are added.
        self._cfg = cfg
        self._no_dashboard = no_dashboard
        self._no_crons = no_crons
        self._no_open = no_open
        self._port_override = port_override
        self._json_ready = json_ready
        self._approval_mode = approval_mode
        self._test_mode = test_mode
        creds = cfg.load_credentials()
        self._app_token = creds.get(CRED_SLACK_APP_TOKEN, "")
        self._bot_token = creds.get(CRED_SLACK_BOT_TOKEN, "")
        self._owner_id = creds.get(CRED_OWNER_ID, "")
        # Multi-user access is disabled — only owner is authorized.
        # Prune stale allowed_users entries from config and warn.
        stale = {u["slack_id"] for u in cfg.slack.allowed_users} - (
            {self._owner_id} if self._owner_id else set()
        )
        if stale:
            logger.warning(
                "Pruning %d stale allowlist entries (multi-user disabled): %s",
                len(stale),
                stale,
            )
        self._allowed_users: set[str] = {self._owner_id} if self._owner_id else set()
        self._tracking_channels: set[str] = {
            c["channel_id"] for c in cfg.slack.tracking_channels if c.get("channel_id")
        }
        self._open_channels: set[str] = set(cfg.slack.open_channels)
        self._slack_enabled = bool(self._app_token and self._bot_token)
        self._wecom_bot_id = creds.get(CRED_WECOM_BOT_ID, "")
        self._wecom_secret = creds.get(CRED_WECOM_SECRET, "")
        self._wecom_enabled = bool(cfg.wecom.enabled and self._wecom_bot_id and self._wecom_secret)
        # Telegram — the TELEGRAM_BOT_TOKEN credential (env/.env) overrides
        # cfg.telegram.bot_token; all other settings come from the typed
        # cfg.telegram dataclass (no ad-hoc config.json re-parse).
        self._telegram_bot_token = creds.get(CRED_TELEGRAM_BOT_TOKEN, "") or cfg.telegram.bot_token
        # telegram.accounts is deprecated and inert, and while it is set the channel
        # stays OFF rather than falling back to the top-level token. A config that
        # named accounts served ONLY those accounts — the top-level bot_token and
        # allowed_user_ids were shadowed — so serving them now would reopen a bot
        # the operator had stopped, under an allow-list they may have narrowed when
        # they migrated. Staying off preserves what the accounts block already did
        # and leaves re-enabling an explicit edit.
        self._telegram_enabled = bool(
            cfg.telegram.enabled and self._telegram_bot_token and not cfg.telegram.accounts
        )
        if cfg.telegram.accounts:
            logger.warning(
                "telegram.accounts is no longer served (%d account(s): %s) — multi-bot "
                "operation is withdrawn until a bot is a governable unit, and the "
                "Telegram channel stays OFF while telegram.accounts is set (these "
                "entries already shadowed the top-level token, so falling back to it "
                "would start a bot you had stopped). Remove the accounts block and put "
                "the one token you want served in telegram.bot_token; the entries are "
                "preserved in config until you do.",
                len(cfg.telegram.accounts),
                ", ".join(sorted(cfg.telegram.accounts)),
            )
        self._telegram_allowed_user_ids: list[int] = list(cfg.telegram.allowed_user_ids)
        # Forum-topic gate (fail closed): serve supergroup forum Topics only when
        # allow_forum is set AND the supergroup's chat_id is allow-listed.
        self._telegram_allow_forum: bool = bool(cfg.telegram.allow_forum)
        self._telegram_allowed_forum_chat_ids: list[int] = list(cfg.telegram.allowed_forum_chat_ids)
        self._telegram_client: "TelegramClient | None" = None
        # Weixin (iLink personal WeChat) — the WEIXIN_TOKEN credential (env/.env)
        # overrides cfg.weixin.token. token + account_id come from the Settings
        # QR flow; deny-by-default DM policy from the typed cfg.weixin dataclass.
        self._weixin_token = creds.get(CRED_WEIXIN_TOKEN, "") or cfg.weixin.token
        self._weixin_account_id: str = cfg.weixin.account_id
        self._weixin_base_url: str = cfg.weixin.base_url
        self._weixin_dm_policy: str = cfg.weixin.dm_policy
        self._weixin_allowed_user_ids: list[str] = list(cfg.weixin.allowed_user_ids)
        self._weixin_enabled = bool(
            cfg.weixin.enabled and self._weixin_token and self._weixin_account_id
        )
        self._weixin_client: "WeixinClient | None" = None
        # Discord — the DISCORD_BOT_TOKEN credential (env/.env) overrides
        # cfg.discord.bot_token; all other settings come from the typed
        # cfg.discord dataclass (mirrors the Telegram block above).
        self._discord_bot_token = creds.get(CRED_DISCORD_BOT_TOKEN, "") or cfg.discord.bot_token
        self._discord_enabled = bool(cfg.discord.enabled and self._discord_bot_token)
        self._discord_allowed_user_ids: list[str] = [str(u) for u in cfg.discord.allowed_user_ids]
        self._discord_allowed_thread_ids: list[str] = [
            str(t) for t in cfg.discord.allowed_thread_ids
        ]
        self._discord_allowed_channel_ids: list[str] = [
            str(c) for c in cfg.discord.allowed_channel_ids
        ]
        self._discord_auto_thread = bool(cfg.discord.auto_thread)
        self._discord_client: "DiscordClient | None" = None
        # Webex — the WEBEX_BOT_TOKEN credential (env/.env) overrides
        # cfg.webex.bot_token; all other settings come from the typed
        # cfg.webex dataclass (no ad-hoc config.json re-parse).
        self._webex_bot_token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
        self._webex_enabled = bool(cfg.webex.enabled and self._webex_bot_token)
        self._webex_allowed_emails: list[str] = list(cfg.webex.allowed_emails)
        self._webex_client: "WebexClient | None" = None
        # iMessage — no credential exists to hoist: the transport is the user's
        # own signed-in Messages.app, so enablement is the config flag alone.
        # Everything else is read from the typed cfg.imessage dataclass at start.
        self._imessage_enabled = bool(cfg.imessage.enabled)
        self._imessage_client: "IMessageClient | None" = None
        # Teams — the MICROSOFT_APP_ID / MICROSOFT_APP_PASSWORD / _TENANT_ID
        # credentials (env/.env) override the typed cfg.teams fields; all other
        # settings come from the typed cfg.teams dataclass.
        self._teams_app_id = creds.get(CRED_MICROSOFT_APP_ID, "") or cfg.teams.app_id
        self._teams_app_password = (
            creds.get(CRED_MICROSOFT_APP_PASSWORD, "") or cfg.teams.app_password
        )
        self._teams_tenant_id = creds.get(CRED_MICROSOFT_APP_TENANT_ID, "") or cfg.teams.tenant_id
        self._teams_enabled = bool(
            cfg.teams.enabled and self._teams_app_id and self._teams_app_password
        )
        self._teams_allowed_emails: list[str] = list(cfg.teams.allowed_emails)
        self._teams_client: "TeamsClient | None" = None
        self.slack_command = cfg.slack.command

        # Services (initialized in start())
        self.slack: RealSlackClient | None = None
        self.sessions: SessionManager | None = None
        self.ctx_builder: ContextBuilder | None = None
        self.conv_log: ConversationLog | None = None
        self.consolidator: HistoryConsolidator | None = None
        self.cron_svc: CronService | None = None
        self.heartbeat_svc: HeartbeatService | None = None
        # Secretary runtime service removed (Amazon-internal). Attribute stays
        # as an inert None so other modules referencing it degrade gracefully.
        self.secretary_svc: object | None = None
        self.subagent_mgr: SubagentManager | None = None
        self._subagent_coalescer_inst: "SubagentEventCoalescer | None" = None
        # Wave accounting for the completion digest (batch_id -> progress).
        self._batch_progress: dict[str, dict] = {}
        self._cron_injecting: dict[str, int] = {}  # parent_key → pending injection count
        self._running_script_ids: set[str] = (
            set()
        )  # job IDs with in-flight script/command execution
        self.task_runner: TaskRunner | None = None
        self.channel_history: ChannelHistory | None = None
        self.dashboard_state: DashboardState | None = None
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
        self._marker_write_task: "asyncio.Task[None] | None" = None
        # Set by the shutdown path when the marker write is still in flight:
        # tells the writer thread to self-clear after publishing, closing the
        # write-after-clear race without any event-loop dependency.
        self._marker_clear_pending = threading.Event()
        self._dashboard_runner: web.AppRunner | None = None
        self._handler_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._session_tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        self._pending_queue: dict[str, list] = {}
        self._socket_client: WSSocketModeClient | None = None
        self._wecom_client: "WeComClient | None" = None  # set by maybe_start_wecom
        # Registry-owned live channel handles ({channel_type: client}). The
        # per-channel _<type>_client attributes are legacy mirrors kept in sync
        # by messaging.registry.start_channels until the config-schema PR
        # retires them; shutdown closes through THIS dict.
        self._channel_handles: dict[str, object] = {}
        self._model_download_task: "asyncio.Task[bool] | None" = None
        self._auto_migrate_task: "asyncio.Task[None] | None" = None
        # Boot-time update check, started fire-and-forget after the signal
        # handlers are installed (see start()). Cancelled on shutdown so a
        # stalled git fetch cannot hold the process open.
        self._update_check_task: "asyncio.Task[None] | None" = None
        self._mcp_gateway_manager: GatewayManager | None = None
        # Detached pre-resolve pass for npm-launcher MCP targets. Held so the
        # loop keeps a strong reference (a bare create_task is only weakly held)
        # and so broker shutdown can cancel an install still in flight.
        self._mcp_resolve_prefetch: asyncio.Task[None] | None = None
        # The rewriter's ``KIROCREW_MCP_TARGET_*`` mapping from the last broker
        # start, kept so an explicit refresh resolves the same launches the
        # daemon is actually serving rather than a freshly re-derived guess.
        self._mcp_target_env: dict[str, str] = {}
        # Resolved here, in sync construction, because config_dir() does file IO
        # and must never be called from an async path (issue #1057). The store
        # lives beside the rest of the data home for the life of the process.

        self._mcp_resolve_home: str = str(config_dir())

    def _count_in_flight_work(self) -> int:
        """Count in-flight backend tasks that an abrupt restart would lose.

        Used by the stale-asset watchdog to drain before shutting down: an
        update prune only breaks static-asset serving, not live ACP turns, so
        letting active turns finish avoids the "❌ lost to gateway restart /
        no result captured" orphaning. Counts active provider turns (dashboard
        chat + task-runner sessions) plus in-flight Slack session turns.

        Defensive: any failure to introspect a surface is treated as idle, so
        a broken accessor can never wedge shutdown.
        """
        count = 0
        state = self.dashboard_state
        if state is not None:
            try:
                for provider in state.sessions.active_providers():
                    checker = getattr(provider, "has_active_turn", None)
                    if not callable(checker):
                        continue
                    try:
                        if checker():
                            count += 1
                    except Exception:
                        # A provider that can't report turn state must not
                        # block shutdown — treat it as idle.
                        pass
            except Exception:
                logger.debug("in-flight count: active_providers() failed", exc_info=True)
        # In-flight Slack session turns (one task per active thread turn).
        for task in list(self._session_tasks.values()):
            if not task.done():
                count += 1
        return count

    # ------------------------------------------------------------------
    # Tool approval callback (shared by cron, heartbeat, subagent, task)
    # ------------------------------------------------------------------

    def _interactive_approval(
        self,
        source: str,
        slot_resolver: Callable[[str], str] | None = None,
        nudge_key: str = "",
    ) -> ToolApprovalCallback:
        """Return an approval callback that races dashboard vs Slack DM.

        Uses the same rich Block Kit message as the main-agent approval flow
        so users see full command text, security redactions, and Trust-session
        controls for background agents too.

        ``nudge_key`` names the monitoring loop whose cycle this callback serves,
        when one does. A channel-bound loop's turns are approved here rather than
        through the dashboard runner, so without it an unanswered prompt on this
        path records no evidence and such a loop keeps waking, being declined and
        spending its cycle cap -- while the expiry notice still promises a stop.
        """

        is_background = source in _BACKGROUND_APPROVAL_SOURCES

        async def _approve(event: LLMEvent, parent_session_key: str = "") -> bool:
            request_id = str(event.request_id)
            # Low-fidelity CHILD request: the structured security context is
            # absent, so every field a shortcut below would judge (title,
            # read-only classification, trust patterns) is agent-authored.
            # Such a request may ONLY be approved by the human prompt at the
            # end of this callback — every non-human auto-approve shortcut
            # (auto_approve_sources, --approval yolo/reads, YOLO override,
            # slot trust) is skipped for it. Strict ``is True``: real events
            # (AcpEvent) return a genuine bool; anything else (e.g. a mock
            # or a foreign event type) must not accidentally enter the
            # restricted path on a truthy non-bool.
            _child_lf = getattr(event, "child_low_fidelity", False) is True
            # Background callers pass the authoritative parent session key. Prefer it
            # over a request-ID resolver because tool permission IDs are opaque UUIDs,
            # unlike spawn approvals (``spawn:<agent_id>``). Treating a tool request ID
            # as an agent ID loses the dashboard slot and hides the approval prompt.
            # ``dashboard_slot_key`` answers "which tab shows this conversation?", so a
            # channel-born session gets its prompt in the tab it is open in too.
            parent_slot = dashboard_slot_key(parent_session_key)

            # NO heuristic fallback. A background caller (cron / taskrunner /
            # autonudge) that supplies neither an authoritative parent session
            # nor a ``slot_resolver`` has no owning conversation, and there is
            # no way to guess one. Borrowing "the first slot that is running"
            # hijacked an unrelated chat and was wrong in three directions at
            # once:
            #   * the prompt surfaced in a conversation that never raised it,
            #     with a truncated label and no provenance;
            #   * the Trust control resolved against that innocent slot, so
            #     trusting a cron's command granted blanket auto-approval to
            #     the borrowed session (and did nothing for the cron);
            #   * conversely, a borrowed slot that already had trust enabled
            #     silently auto-approved the background command below —
            #     privilege the cron was never granted.
            # Unowned approvals now carry slot="" and are surfaced ONLY on the
            # global approvals surface (notification feed / /api/approvals).
            if parent_slot:
                approval_slot = parent_slot
            elif slot_resolver:
                try:
                    approval_slot = slot_resolver(request_id) or ""
                except Exception:
                    logger.warning("slot_resolver failed for %s", request_id, exc_info=True)
                    approval_slot = ""
            else:
                approval_slot = ""

            # Per-source auto-approve (e.g. cron, taskrunner, subagent)
            if source in self._cfg.hooks.get("auto_approve_sources", []):
                if _child_lf:
                    # The operator explicitly configured this source to run
                    # UNATTENDED — nobody is watching the interactive window,
                    # so parking a low-fidelity child request there would
                    # stall the run for the full approval timeout and then
                    # deny anyway. Fail closed fast instead (an approve is
                    # still never allowed on agent-authored context).
                    logger.warning(
                        "Fast-denying low-fidelity child request under "
                        "auto-approve source %s (unattended; title is "
                        "agent-authored)",
                        source,
                    )
                    return False
                logger.info("Auto-approving tool %s from source %s", event.title, source)
                return True

            # CLI --approval flag override (composable test mode).
            # 'yolo' auto-approves all; 'reads' auto-approves read-only tools;
            # 'interactive' falls through to the standard flow.
            if self._approval_mode in ("yolo", "reads") and not _child_lf:
                approve = self._approval_mode == "yolo" or (
                    self._approval_mode == "reads" and _is_read_only_tool(event.title or "")
                )
                if approve:
                    # Emit a SEL audit event so the audit trail records WHICH
                    # mode auto-approved the tool. Downstream sites already
                    # log the invocation itself; this captures the decision.
                    try:
                        _safe = redact(event.title or "")
                        sel().log_api_access(
                            caller=f"cli:approval={self._approval_mode}",
                            operation=f"{source}.cli_approval_auto_approve",
                            outcome="ok",
                            resources=_safe,
                        )
                    except Exception:
                        logger.warning(
                            "SEL audit failed for cli --approval auto-approve", exc_info=True
                        )
                    return True

            # Check both YOLO sources: Slack handler (!yolo on) and dashboard UI
            if safety_override().is_active() and not _child_lf:
                return True

            if self.dashboard_state:
                # Check if the parent slot is trusted (not all slots).
                # The parent comes from the authoritative parent session key or
                # an explicit slot_resolver -- never from a guess. When a
                # slot_resolver exists but returns falsy we do NOT fall back to
                # the all-slots rule: if the explicit resolver cannot find the
                # parent, widening trust scope would be unsound.

                def _sel_log(
                    *, caller: str, operation: str, outcome: str, resources: str = ""
                ) -> None:
                    try:
                        sel().log_api_access(
                            caller=caller,
                            operation=operation,
                            outcome=outcome,
                            resources=resources,
                        )
                    except Exception:
                        logger.warning("SEL audit failed for trust check", exc_info=True)

                _safe_title = redact(event.title)

                _parent_slot_key = approval_slot or None

                if _parent_slot_key:
                    _ps = (self.dashboard_state._slots or {}).get(_parent_slot_key)
                    if _ps and _ps._trust and _child_lf:
                        # Slot IS trusted; the fidelity gate is what blocks
                        # the auto-approve. A distinct audit reason — an
                        # auditor reading "not_trusted" for a trusted slot
                        # would reconstruct the wrong cause.
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_blocked_low_fidelity_child",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                    elif _ps and _ps._trust:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_auto_approve",
                            outcome="ok",
                            resources=_safe_title,
                        )
                        return True
                    elif _ps:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_not_trusted",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                    else:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.scoped_trust_slot_not_found",
                            outcome="not_auto_approved",
                            resources=_safe_title,
                        )
                elif not slot_resolver:
                    # No owning slot and no resolver at all. There is NO
                    # implicit trust path here: an unowned background command
                    # always prompts.
                    #
                    # An "all open conversations are trusted" rule does not
                    # narrow this enough to be safe: for a single-user dashboard
                    # with one trusted chat open -- the typical state -- `all()`
                    # is trivially satisfied, so a cron's command would be
                    # silently auto-approved with no prompt: privilege the job
                    # was never granted, justified by trust the user granted to a
                    # conversation the job has nothing to do with.
                    #
                    # Session trust means "auto-approve tools for THIS chat
                    # session". An unattended job is not this session, so no
                    # amount of session trust should speak for it. Operators who
                    # do want a source to run unprompted have the explicit
                    # opt-in above (``hooks.auto_approve_sources``), which is
                    # consent for that source rather than a side effect of
                    # trusting a chat.
                    _sel_log(
                        caller=f"source:{source}",
                        operation=f"{source}.unowned_no_implicit_trust",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )
                else:
                    # Resolver existed but failed -- fall through to interactive approval
                    _sel_log(
                        caller=f"source:{source}",
                        operation=f"{source}.scoped_trust_fallthrough",
                        outcome="not_auto_approved",
                        resources=_safe_title,
                    )

            # Post approval buttons to Slack DM if available
            if self.slack and self._owner_id:
                try:
                    # Resolve parent thread context for threaded approval prompts
                    thread_ts: str | None = None
                    channel: str | None = None
                    if parent_session_key and self.sessions:
                        channel = self.sessions.get_channel(parent_session_key)
                        thread_ts = self.sessions.get_thread(parent_session_key)
                        if not thread_ts and channel:
                            # Slack ts format: "{epoch_seconds}.{microseconds}" — pure digits + one dot
                            if re.fullmatch(r"\d+\.\d+", parent_session_key):
                                thread_ts = parent_session_key
                    is_dm = not channel
                    if not channel:
                        channel = await self.slack.open_dm(self._owner_id)
                        thread_ts = None
                    from kiro_crew.slack.handler import (
                        _build_approval_blocks,
                        _pending_approvals,
                        _PendingApproval,
                    )

                    blocks = _build_approval_blocks(event, is_dm=is_dm, source=source)
                    title_safe, _ = redact_exfiltration_urls(event.title)
                    title_safe, _ = redact_credentials(title_safe)
                    fallback = f"🔐 [{source}] Approve: {title_safe}?"
                    approval_ts = await self.slack.post_blocks(
                        channel, blocks, fallback, thread_ts  # type: ignore[arg-type]
                    )

                    # Create a pending approval that the interactive handler can resolve.
                    # Use a dummy provider — the actual approve/reject is handled by
                    # returning True/False from this callback.
                    pending = _PendingApproval(
                        provider=None,  # type: ignore[arg-type]
                        request_id=request_id,
                        session_key=parent_session_key,
                    )
                    key = f"{channel}:{approval_ts}"
                    _pending_approvals[key] = pending

                    # Also request via dashboard if available
                    dashboard_future = None
                    if self.dashboard_state:
                        dashboard_future = asyncio.ensure_future(
                            self.dashboard_state.request_approval(
                                request_id,
                                source,
                                event.title,
                                tool_input=event.tool_input,
                                tool_purpose=event.tool_purpose,
                                slot=approval_slot,
                                is_background=is_background,
                            )
                        )

                        # When dashboard resolves, also resolve the Slack future
                        def _on_dashboard_done(fut: asyncio.Future) -> None:  # type: ignore[type-arg]
                            if fut.cancelled() or fut.exception():
                                return
                            result = "approved" if fut.result() else "rejected"
                            if not pending.future.done():
                                pending.future.set_result(result)

                        dashboard_future.add_done_callback(_on_dashboard_done)

                    # Wait for either Slack or dashboard approval. Background
                    # sources (no human present) deny-fast on a short window
                    # instead of burning the full 2h human window.
                    approval_timeout = (
                        DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
                        if is_background
                        else DashboardState._APPROVAL_TIMEOUT
                    )
                    try:
                        outcome = await asyncio.wait_for(pending.future, timeout=approval_timeout)
                    except asyncio.TimeoutError:
                        outcome = "rejected"
                        # Nobody answered on either surface -- this branch also
                        # cancels the dashboard future below, so it is the single
                        # authoritative "unanswered" point for a channel-bound
                        # loop's cycle. Record it so the loop stops on its next
                        # wake instead of spending the rest of its cap.
                        if nudge_key:
                            try:
                                svc = self.autonudge_svc
                                if svc is not None:
                                    svc.notify_approval_stalled(nudge_key)
                            except Exception:
                                logger.debug(
                                    "autonudge.notify_approval_stalled failed", exc_info=True
                                )
                    finally:
                        _pending_approvals.pop(key, None)
                        # Resolve dashboard approval if Slack responded first
                        if self.dashboard_state:
                            self.dashboard_state.resolve_approval(request_id, outcome == "approved")
                        if dashboard_future and not dashboard_future.done():
                            dashboard_future.cancel()

                    # Clean up Slack message
                    try:
                        status = "✅ Approved" if outcome == "approved" else "🚫 Rejected"
                        await self.slack.update_message(
                            channel, approval_ts, text=f"🔐 *{title_safe}* — {status}"
                        )
                    except Exception:
                        pass

                    return outcome == "approved"
                except Exception:
                    logger.debug("Slack approval failed, falling back to dashboard", exc_info=True)

            # Fallback: dashboard only
            if self.dashboard_state:
                return await self.dashboard_state.request_approval(
                    request_id,
                    source,
                    event.title,
                    tool_input=event.tool_input,
                    tool_purpose=event.tool_purpose,
                    slot=approval_slot,
                    is_background=is_background,
                )
            if _child_lf:
                # No human surface answered and none of the (skipped)
                # shortcuts may speak for an agent-authored request:
                # fail closed.
                return False
            return True  # no UI → auto-approve

        return _approve

    # ------------------------------------------------------------------
    # Heartbeat tool approval — strict allowlist, no UI prompt
    # ------------------------------------------------------------------
    async def _heartbeat_approval(self, event: LLMEvent, _parent_session_key: str = "") -> bool:
        """Tool-approval callback for heartbeat sessions.

        Heartbeat runs unattended on a timer — there is no human to click an
        approval button.  We auto-approve only tools whose name is in
        ``HEARTBEAT_SAFE_TOOLS`` (strict exact-match) and reject everything
        else with a SEL audit event.

        This is the "Option A" mitigation for the heartbeat security review
        on blanket ``AUTO_APPROVE`` was rejected because polled
        external content (CR comments, ticket bodies) is untrusted; a strict
        name-based allowlist gives heartbeat the tool access it needs while
        keeping the write surface closed to deny-by-default.

        Both approve and deny outcomes emit SEL audit events
        (``log_tool_invocation``) so operators can audit every permission
        decision made on behalf of an unattended heartbeat session.
        """
        title = (event.title or "").strip()
        # Tool titles are LLM-originated input. Redact before any external
        # surface — SEL audit AND dashboard-visible logger warnings —
        # per the security-controls "never trust LLM output" guideline.
        safe_title = redact_exfiltration_urls(redact_credentials(title)[0])[0]

        def _audit(outcome: str, *, critical: bool = False, **metadata: str) -> None:
            """Emit a SEL ``log_tool_invocation`` event.

            With ``critical=True`` the write is synchronous and raises on
            failure — callers must decide whether the underlying permission
            decision can proceed without an audit trail. The approve path
            passes ``critical=True`` and treats SEL failure as fatal
            (deny-by-default, preserve audit invariant). The deny path
            tolerates SEL failure because the tool is rejected regardless.
            """
            sel().log_tool_invocation(
                session_key=HEARTBEAT_KEY,
                source="heartbeat",
                agent="kirocrew-heartbeat",
                tool_name=safe_title or "<unknown>",
                tool_kind=event.tool_kind,
                outcome=outcome,
                request_id=event.request_id,
                metadata=metadata or None,
                critical=critical,
            )

        if _is_heartbeat_safe_tool(title):
            # Fail-closed: if SEL is down we cannot record the auto-approve
            # decision, and unattended sessions must not run tools without
            # an auditable permission record. Deny rather than approve
            # silently (security-controls deny-by-default). critical=True
            # forces a synchronous SEL write so a filesystem failure reaches
            # this except instead of being swallowed by the async writer.
            # Offloaded to a worker thread: the critical write does blocking
            # file IO + a Condition.wait() drain, which must not run on the
            # gateway event loop (no-blocking-call-on-event-loop). The
            # exception still propagates through await, preserving fail-closed.
            try:
                await asyncio.to_thread(
                    _audit, "auto_approved", critical=True, reason="in_heartbeat_safe_tools"
                )
            except Exception:
                logger.warning(
                    "SEL audit failed on heartbeat approve path — "
                    "denying tool to preserve audit-or-deny invariant",
                    exc_info=True,
                )
                return False
            return True

        # Reject + audit. Logged via the same SEL channel as the interactive
        # approval path so operators can see what got blocked and decide
        # whether to extend HEARTBEAT_SAFE_TOOLS. SEL failure here is
        # tolerated because the tool is denied regardless — the safety
        # property the audit protects (no unaudited tool runs) is preserved.
        try:
            _audit("denied", reason="not_in_heartbeat_safe_tools")
        except Exception:
            logger.warning(
                "SEL audit failed on heartbeat deny path — " "tool was still rejected",
                exc_info=True,
            )
        logger.warning(
            "Heartbeat blocked tool call: %s (not in HEARTBEAT_SAFE_TOOLS)",
            safe_title or "<unknown>",
        )
        return False

    # Required packages that must be importable (import_name, pip_spec).
    # pip_spec may include version constraints matching setup.cfg.
    _REQUIRED_DEPS = [
        ("snowballstemmer", "snowballstemmer>=1.0"),
        # PyYAML (import name ``yaml``) is imported by cc_agent on every CLI
        # path. It installs cleanly from public PyPI, so list it here as a
        # backstop: if it is ever missing (e.g. a partial install), the startup
        # self-heal repairs it instead of every command crashing at import.
        ("yaml", "PyYAML>=6,<7"),
    ]

    @staticmethod
    def _is_brazil_install(proj: str) -> bool:
        """Return True if *proj* was installed via Brazil, False for venv/pip."""
        method_file = Path(proj) / ".install-method"
        if method_file.is_file():
            return method_file.read_text().strip() == "brazil"
        return bool(
            shutil.which("brazil-build") and (Path(proj).parent.parent / ".brazil").is_dir()
        )

    # Budgets for the two startup subprocesses below. Class attributes (not
    # literals) so tests can shrink them to exercise the timeout/kill paths
    # without waiting out the real budgets.
    _DEP_INSTALL_TIMEOUT_SECS: float = 300.0
    _KIRO_CLI_VERSION_TIMEOUT_SECS: float = 5.0
    # Bound on the post-kill reap: a build-backend grandchild that survived
    # the kill can hold the stdout/stderr pipes open, making an unbounded
    # ``communicate()`` wait forever and hang boot.
    _STARTUP_CHILD_REAP_SECS: float = 5.0

    @staticmethod
    async def _kill_startup_child(proc: "asyncio.subprocess.Process") -> None:
        """Best-effort kill of a startup child and its descendants.

        ``proc.kill()`` signals only the child's own PID; pip's build-backend
        grandchildren survive it, keep writing into site-packages, and hold
        the pipe write ends open. The tree kill covers them: process-group on
        POSIX (the child is spawned with ``start_new_session``) and
        ``taskkill /T`` on Windows. Async on purpose — the Windows branch
        spawns ``taskkill`` (up to 5s), and ``kill_process_tree_async``
        offloads it so the kill itself cannot stall the loop this fix exists
        to protect (POSIX dispatches inline; ``killpg`` is non-blocking).
        Falls back to a plain kill when the tree kill is refused
        (already-dead child, non-int mocked PID in tests).
        """
        sig = getattr(signal, "SIGKILL", signal.SIGTERM)  # no SIGKILL on Windows
        try:
            await platform_compat.kill_process_tree_async(proc.pid, sig)
        except Exception:
            with contextlib.suppress(Exception):
                proc.kill()

    @classmethod
    async def _reap_startup_child(cls, proc: "asyncio.subprocess.Process") -> None:
        """Bounded reap after a kill — never lets a wedged pipe hang boot.

        Best-effort by design: past the bound, boot proceeds and the OS reaps
        the zombie eventually. ``suppress(Exception)`` deliberately does not
        swallow ``CancelledError`` (a ``BaseException``).
        """
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.communicate(), timeout=cls._STARTUP_CHILD_REAP_SECS)

    async def _check_missing_deps(self) -> None:
        """Auto-repair missing pip deps for venv installs.

        After auto-update, old code may have pulled new source via git reset
        but skipped ``pip install``. This catches the gap on next startup.

        Async on purpose: the install can legitimately take minutes, and a
        synchronous ``subprocess.run`` here would block the event loop for the
        whole budget (see the module invariant — the loop runs callbacks one at
        a time, so nothing else, including the loop-stall heartbeat once it is
        armed, runs while a callback blocks). The child runs via
        ``asyncio.create_subprocess_exec`` — the same pattern the auto-update
        path in this file already uses — so the loop keeps servicing callbacks
        while pip works.
        """
        missing = [pip for mod, pip in self._REQUIRED_DEPS if importlib.util.find_spec(mod) is None]
        if not missing:
            return

        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if not proj or self._is_brazil_install(proj):
            return

        logger.warning("Missing deps %s — installing directly", missing)
        print(f"👻 Installing missing dependencies: {', '.join(missing)}")
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--quiet",
            *missing,
            cwd=proj,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group (POSIX; no-op on Windows) so a timeout kill
            # reaches pip's build-backend grandchildren, not just pip itself.
            start_new_session=platform_compat.IS_POSIX,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self._DEP_INSTALL_TIMEOUT_SECS
            )
        except (TimeoutError, asyncio.TimeoutError):
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            print("❌ pip install timed out — run manually: kirocrew update")
            logger.error("Dep repair timed out after %.0fs", self._DEP_INSTALL_TIMEOUT_SECS)
            return
        except asyncio.CancelledError:
            # Gateway shutdown / Ctrl-C mid-install: leaving pip running would
            # race the NEXT boot's install of the same distributions — the
            # half-installed state this repair path exists to fix.
            await self._kill_startup_child(proc)
            raise
        if proc.returncode == 0:
            # Invalidate import caches so the new packages are found
            importlib.invalidate_caches()
            print("✅ Dependencies installed")
        else:
            print("❌ pip install failed — run manually: kirocrew update")
            # pip stderr can echo an index URL with embedded credentials
            # (https://user:token@internal-index/...), so redact before the
            # volume cap: truncating first can bisect a token, and half a token
            # no longer matches the redactors' patterns.
            dep_err = (stderr or b"").decode(errors="replace")
            dep_err, _ = redact_exfiltration_urls(dep_err)
            dep_err, _ = redact_credentials(dep_err)
            logger.error("Dep repair failed: %s", dep_err[:500])

    # ------------------------------------------------------------------
    # Service initialisation
    # ------------------------------------------------------------------

    async def _auto_open_dashboard(self, dashboard_url: str) -> None:
        """Open the dashboard in the operator's browser, best effort.

        Offloaded to the subprocess executor — ``webbrowser.open()`` can block
        indefinitely on a wedged ``/usr/bin/open``, which would starve the
        default thread pool if this used ``asyncio.to_thread()``. The subprocess
        executor is a dedicated pool for exactly this class of hang.

        Runs as a background task rather than inline on the boot path so a slow
        browser launch overlaps the MCP probe instead of adding to it. The URL
        has already been printed by the time this is called, so a failure here
        costs the operator a click, not the address.
        """
        try:
            await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    webbrowser.open,
                    dashboard_url,
                ),
                timeout=5.0,
            )
        except (TimeoutError, asyncio.TimeoutError):
            logger.debug("webbrowser.open timed out — skipping")
            print(
                "👻 Browser was slow to open — skipping auto-open.\n"
                "   Dashboard is running. Open this URL manually:\n"
                f"   {dashboard_url}\n"
                "   Or run: kirocrew token"
            )

    async def _warn_if_kiro_cli_outdated(self) -> None:
        """Warn when kiro-cli is too old for ``--agent`` (requires >= 1.26).

        Never raises: an absent, hung, or unparseable kiro-cli must not break
        boot. Off the loop via an async subprocess so a slow binary cannot
        stall every other callback for the 5s budget, and a timeout is logged
        (not silently swallowed) so a wedged kiro-cli that costs 5s on every
        boot is diagnosable from gateway.log.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                "kiro-cli",
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except Exception:
            return  # binary missing/unspawnable — same silence as before
        try:
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._KIRO_CLI_VERSION_TIMEOUT_SECS
            )
        except (TimeoutError, asyncio.TimeoutError):
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            logger.warning(
                "kiro-cli --version timed out after %.0fs",
                self._KIRO_CLI_VERSION_TIMEOUT_SECS,
            )
            return
        except asyncio.CancelledError:
            await self._kill_startup_child(proc)
            raise
        except Exception:
            # Transport/pipe errors must not abort boot (this helper's
            # contract is "never raises" — the pre-#3051 code swallowed them
            # via a blanket except). Kill+reap best-effort and continue.
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            logger.debug("kiro-cli --version probe failed", exc_info=True)
            return
        try:
            if proc.returncode == 0:
                # e.g. "kiro-cli 1.25.0" -> (1, 25, 0)
                parts = out.decode(errors="replace").strip().split()[-1].split(".")
                major, minor = int(parts[0]), int(parts[1])
                if (major, minor) < (1, 26):
                    print(
                        f"⚠️  kiro-cli {major}.{minor} is outdated (1.26+ required). "
                        "Update kiro-cli, or use the default claude-agent-acp backend."
                    )
        except Exception:
            pass  # unparseable version output — same silence as before

    async def _init_services(self) -> None:
        """Initialize memory, skills, hooks, context, history, sessions.

        Async so the blocking pieces named by issue #3051 — the kiro-cli
        version probe, the pip dep repair, ``VectorMemoryStore.init()`` and
        ``memory.rebuild_index()`` — run off the event loop. Object
        CONSTRUCTION deliberately stays on the loop thread:
        ``SessionManager.__init__`` creates asyncio primitives (locks,
        semaphores, queues), so hopping the whole method into a worker thread
        would trade a blocking bug for a thread-affinity one. (Other sync
        filesystem steps — e.g. agent-config install — remain on the loop;
        they are bounded small-file work, not usage-scaled. The builtin-skills
        sync verifies user-owned trees before replacing them, so it runs as a
        tracked background task in a worker thread and never gates readiness.)
        """
        if not self._slack_enabled:
            logger.info("Slack not configured — starting without the Slack gateway")

        # Check kiro-cli version (--agent requires >= 1.26)
        await self._warn_if_kiro_cli_outdated()

        # Auto-repair missing pip deps (handles chicken-and-egg after auto-update)
        try:
            await self._check_missing_deps()
        except Exception:
            logger.warning("Dep check failed", exc_info=True)

        # Auto-install agent config so MCP servers are always up to date
        try:
            from kiro_crew.agent import rebuild_agent_config  # circular import

            path = rebuild_agent_config()
            logger.info("Agent config installed: %s", path)

            # Deliver shim + one-time stale-MCP purge automatically — the
            # desktop app launches the gateway but never runs `kirocrew setup`.
            #
            # Routed through the CPP seam (``AgentRuntime.run_first_run_setup``)
            # rather than importing ``agent.run_first_run_setup`` directly, so
            # first-run setup is genuinely extensible: an edition composes an
            # adapter that adds its own one-time provisioning on top. The
            # ``DefaultAgentRuntime`` delegates to exactly the same
            # ``agent.run_first_run_setup()`` this line used to call, so the
            # standalone build is behaviorally identical (asserted in
            # test_cpp_wiring_standalone).
            #
            # ``safe_context_call`` keeps a transient adapter error from breaking
            # startup (``fallback=None`` matches the seam's ``-> None`` contract),
            # matching the pre-existing best-effort posture of this block.
            # The fail-closed guarantee for a non-standalone host is already
            # discharged EARLIER, by ``boot_platform`` in ``run_gateway``: it
            # aborts before the orchestrator is built, so a companion that cannot
            # compose never reaches this line. (Note the enclosing
            # ``except Exception`` would itself absorb a PlatformCompositionError
            # raised here — which is why boot, not this call site, is where that
            # invariant is enforced.)
            safe_context_call(
                lambda: current_context().agent_runtime.run_first_run_setup(),
                fallback=None,
                log_message="agent_runtime.run_first_run_setup failed",
            )
        except Exception:
            # Boot deliberately continues: a gateway with no agent spec still
            # serves the dashboard, and one command repairs it. But this is NOT a
            # warning-level event — without a spec on disk kiro-cli answers every
            # session/set_mode with "Mode '<name>' not found", so EVERY chat turn
            # and every background turn fails for the life of the install. Log at
            # ERROR and print the remedy, mirroring _check_missing_deps: the
            # desktop app launches the gateway with no terminal in sight, so the
            # log is the only durable record and the print is what a
            # console-launched operator actually sees.
            logger.error("Agent config install failed", exc_info=True)
            print(
                "ERROR: agent config install failed — chat sessions cannot start. "
                "Repair with: kirocrew setup --agent-only --clean"
            )

        # Verify what actually landed on disk, whether or not the block above
        # raised. An exception is only one of the ways to end up with no spec (see
        # missing_required_agent_specs for the two silent ones), and the failure
        # mode is identical in all of them: every turn dies at session/set_mode.
        # Deliberately outside the try above so a raising install still gets
        # verified, and best-effort itself so a stat error cannot break boot.
        try:
            from kiro_crew.agent import missing_required_agent_specs  # circular import

            missing = missing_required_agent_specs()
            if missing:
                logger.error(
                    "Agent specs missing after install: %s (in %s) — every chat turn "
                    "will fail at session/set_mode with \"Mode '<name>' not found\". "
                    "Repair with: kirocrew setup --agent-only --clean",
                    ", ".join(missing),
                    kiro_agents_dir(),
                )
                print(
                    f"ERROR: agent specs missing after install: {', '.join(missing)} — "
                    "chat sessions cannot start. "
                    "Repair with: kirocrew setup --agent-only --clean"
                )
        except Exception:
            logger.debug("Agent spec verification failed", exc_info=True)

        self.slack = RealSlackClient(self._bot_token) if self._slack_enabled else None
        factory = build_provider_factory(self._cfg)

        # Memory, skills, hooks, lessons
        memory = MemoryStore()
        memory.init()

        # Vector memory (structured semantic store)
        from kiro_crew.vector_memory import VectorMemoryStore

        self.vector_memory = VectorMemoryStore(
            confidence_threshold=self._cfg.memory.semantic_confidence_threshold,
            extra_prefixes=self._cfg.memory.semantic_keys or None,
            episodic_limit=self._cfg.memory.episodic_max_results,
            embedding_dim=self._cfg.memory.embedding_dim,
            decay_rates=self._cfg.memory.decay_rates or None,
        )
        # Off-loop: init() connects sqlite and runs schema migrations, which
        # scale with store size (VectorMemoryStore's own docs say async
        # contexts should wrap it in asyncio.to_thread).
        await asyncio.to_thread(self.vector_memory.init)
        memory.vector_store = self.vector_memory

        # Bind-fast: construct the loader WITHOUT syncing (it reads whatever
        # is on disk now) and run the builtin sync as a tracked background
        # task in a worker thread. The sync verifies user-owned trees before
        # it may replace them, so its cost scales with what users put in the
        # skills dir — it must gate neither the event loop nor the dashboard
        # socket. Listings pick the synced skills up as soon as it completes.
        skills = SkillsLoader(install_builtins=False)

        async def _sync_builtin_skills() -> None:
            try:
                await asyncio.to_thread(skills.sync_builtins)
            except Exception:
                logger.warning("builtin-skill sync failed", exc_info=True)

        _skills_sync_task = asyncio.create_task(_sync_builtin_skills())
        self._background_tasks.add(_skills_sync_task)
        _skills_sync_task.add_done_callback(self._background_tasks.discard)
        # Opt-out state comes from the keystone denied_commands.json (agent-
        # unwritable), not config.json's hooks section.
        hooks = HookManager(hooks_config_from_config_dict(self._cfg.hooks))
        lessons = LessonStore()
        self.ctx_builder = ContextBuilder(
            memory=memory,
            skills=skills,
            hooks=hooks,
            lessons=lessons,
            bot_name=self._cfg.agent.bot_name,
        )

        # Conversation history
        self.conv_log = ConversationLog()
        self.conv_log.init()
        self.ctx_builder.conversation_log = self.conv_log

        # Session manager
        self.sessions = SessionManager(
            self._cfg, provider_factory=factory
        )  # type: ignore[arg-type]

        # History consolidator
        self.consolidator = HistoryConsolidator(
            log=self.conv_log,
            memory=memory,
            sessions=self.sessions,
            lesson_store=lessons,
            history_idle_secs=self._cfg.memory.history_idle_hours * 3600,
            vector_store=self.vector_memory,
            migrated=self._cfg.memory.migrated,
            skills_loader=skills,
            auto_skills_enabled=self._cfg.skills.auto_create_from_sessions,
            auto_refine_enabled=self._cfg.skills.auto_refine_on_deviation,
            auto_min_tool_calls=self._cfg.skills.auto_min_tool_calls,
            auto_similarity_threshold=self._cfg.skills.auto_similarity_threshold,
            approval_required=self._cfg.skills.approval_required,
            max_auto_skills=self._cfg.skills.max_auto_skills,
            stale_after_days=self._cfg.skills.stale_after_days,
            archive_after_days=self._cfg.skills.archive_after_days,
            generate_scripts=self._cfg.skills.generate_scripts,
            judge_model=self._cfg.skills.judge_model,
        )

        # Trigger skill extraction when sessions expire (idle/orphan)
        self.sessions.on_session_expire = self.consolidator.consolidate_session

        # Channel history buffer. data_home(), not config_dir(): this method is
        # async and config_dir() re-runs start-of-process maintenance (mkdir,
        # breadcrumb refresh, archive sweep) on every call — #1057.
        self.channel_history = ChannelHistory(
            observe_max_entries=self._cfg.observe_max_messages,
            observe_ttl_secs=int(self._cfg.observe_ttl_hours * 3600),
            history_dir=data_home() / "history",
        )
        self.ctx_builder.channel_history = self.channel_history

        # Register observe-mode channels for deeper history buffer
        from kiro_crew.config.loader import ACTIVATION_OBSERVE

        for ch_id, ch_cfg in self._cfg.slack_channels.items():
            if ch_cfg.activation == ACTIVATION_OBSERVE:
                self.channel_history.set_observe(ch_id)

        # FTS index. Off-loop: rebuild_index globs every history *.md and
        # rewrites the index, so it scales with usage.
        indexed = await asyncio.to_thread(memory.rebuild_index)
        logger.info("FTS index built: %d files", indexed)

    async def _open_dm_with_retry(
        self, user_id: str, job_name: str, max_attempts: int = 3
    ) -> str | None:
        """Retry open_dm to handle transient Slack API errors (shared impl)."""
        if self.slack is None:
            return None
        return await open_dm_with_retry(
            self.slack,
            user_id,
            context=f"Cron '{job_name}'",
            max_attempts=max_attempts,
        )

    def _remember_options(
        self,
        session_key: str,
        channel: str,
        ts: str,
        choices: list[str],
        blocks: list[dict],
        text: str,
    ) -> None:
        """Record an OPTIONS control posted into *session_key*'s Slack thread.

        Lets that session's next turn strike the control through, so a delivery
        which ended on a question stops inviting an answer once the conversation
        has moved past it. Best-effort: losing the record only means the control
        is left live.
        """
        if not ts or not choices:
            return
        try:

            remember_slack_options(
                self.dashboard_state,
                session_key,
                PostedOptions(
                    channel=channel,
                    ts=ts,
                    choices=tuple(choices),
                    blocks=tuple(blocks),
                    text=text,
                ),
            )
        except Exception:
            logger.debug("Failed to record OPTIONS control for %s", session_key, exc_info=True)

    async def _deliver_cron_response(
        self, parent_key: str, text: str, *, silent: bool = False
    ) -> bool:
        """Deliver a cron session's post-subagent response to Slack.

        When a cron session spawns subagents via ``spawn_run``, the agent's
        synthesized response was only appended to the dashboard notification
        body and never posted to Slack — making subagent delegation useless in
        cron contexts. This routes that response to the channel/thread the cron
        originally posted in (stored on the session at delivery time), falling
        back to the owner's DM. No-op when silent, when Slack is unavailable,
        or when no channel can be resolved.
        """
        if silent or self.slack is None or not text.strip():
            return False
        assert self.sessions is not None
        channel = self.sessions.get_channel(parent_key)
        thread_ts = self.sessions.get_thread(parent_key)
        if not channel and self._owner_id:
            channel = await self._open_dm_with_retry(self._owner_id, parent_key)
            thread_ts = None  # a thread_ts from another channel is invalid in a DM
        if not channel:
            logger.warning("Cron %s: no channel resolved for subagent response", parent_key)
            return False
        # render [OPTIONS: ...] tags as interactive buttons, matching
        # the interactive-handler / subagent-completion / dashboard-mirror paths.
        # Extracted from the raw text: the tag is a plain-text marker, so pulling
        # it off before conversion is what makes the controls independent of what
        # conversion (and its 39,000-char self-truncation) does to the tail.
        text, options = extract_options(text)
        # render_for_slack IS the redaction boundary here -- it normalises ANSI
        # first so a credential broken up by escapes cannot be reassembled by the
        # strip inside to_slack_mrkdwn, and redacts again after conversion.
        for part in render_for_slack(text, limit=_CRON_MSG_LIMIT):
            await self.slack.post_message(channel, part, thread_ts)
        if options:
            try:
                # Tokened like every other producer. An untokened control has no
                # asker to pin, so a click on it falls back to resolving the
                # thread -- which is exactly the reroute the pin exists to stop.
                _cron_token = await asyncio.to_thread(
                    mint_options_token, self.dashboard_state, parent_key
                )
                option_blocks = build_options_blocks(options, staleness_token=_cron_token)
                option_ts = await self.slack.post_blocks(
                    channel,
                    option_blocks,
                    "Options",
                    thread_ts,
                )
                self._remember_options(
                    parent_key, channel, option_ts, options, option_blocks, "Options"
                )
            except Exception:
                logger.debug("Cron %s: failed to post OPTIONS blocks", parent_key, exc_info=True)
        return True

    def _channel_reply_link(self, parent_key: str) -> tuple[ChannelLink, bool] | None:
        """Resolve the non-Slack channel conversation behind *parent_key*.

        Returns ``(link, needs_dm_resolution)``, or None when no safe target
        exists. Resolution ladder, most explicit first:

        1. the session's **origin link** — the conversation's real send target,
           recorded by a transport's inbound dispatch (e.g. Discord);
        2. a non-Slack **mirror link** (e.g. a Telegram ``/link`` binding),
           which also carries a forum Topic thread id;
        3. the session's stored channel value — a *session-attribution* id,
           not a postable conversation, so it is accepted ONLY when it names a
           direct (1:1) peer: for a canonical channel key the stored
           ``"{namespace}:{user_id}"`` must match the key's own namespace and
           the key's chat_type must be direct; for a ``unified:`` DM bucket
           (direct-only by construction — forum routes never collapse into it)
           the stored namespace must be a registered non-Slack channel. The id
           is the peer's USER id, so the caller must resolve the postable
           conversation through ``transport.resolve_configured_target``
           (``needs_dm_resolution=True``). Group/forum sessions never take
           this rung: their stored value carries the sender's user id, and
           sending there would leak the conversation into a private DM.

        Returns None for Slack, dashboard, and unrecognized keys so callers
        keep their existing delivery for those.
        """
        namespace = channel_namespace_of(parent_key)
        if not namespace or namespace == SLACK_NAMESPACE or self.sessions is None:
            return None
        for getter in (self.sessions.get_origin_link, self.sessions.get_mirror_link):
            try:
                link = getter(parent_key)
            except Exception:
                link = None
            if link is not None and link.channel_id and link.channel_type != SLACK_NAMESPACE:
                return link, False
        try:
            stored = self.sessions.get_channel(parent_key)
        except Exception:
            stored = None
        if not stored:
            return None
        channel_type, sep, peer_id = stored.partition(":")
        if not (sep and channel_type and peer_id) or channel_type == SLACK_NAMESPACE:
            return None
        if namespace == DM_SCOPE_UNIFIED:
            # A unified bucket carries no chat_type of its own; validate the
            # stored namespace against the registered channel set instead.
            if channel_type not in CHANNEL_SESSION_NAMESPACES or channel_type == DM_SCOPE_UNIFIED:
                return None
        else:
            parsed = parse_session_key(parent_key)
            if parsed is None or parsed.chat_type != CHAT_TYPE_DIRECT or channel_type != namespace:
                return None
        return ChannelLink(channel_type, channel_id=peer_id), True

    async def _deliver_channel_reply(
        self,
        parent_key: str,
        text: str,
        *,
        resolved_link: tuple[ChannelLink, bool] | None = None,
    ) -> bool:
        """Deliver a subagent-completion reply to a non-Slack channel conversation.

        The transport leg of completion routing: resolves the conversation
        behind *parent_key*, vets the egress through the shared governed
        cross-surface ladder (``_resolve_channel_target`` — SEL-audited,
        fail-closed, capability-gated on ``supports_proactive_send``), then
        redacts, chunks, and sends via the registered ``MessagingTransport``.
        A link derived from the stored channel value carries the peer's USER
        id, so the postable conversation is resolved through
        ``transport.resolve_configured_target`` first.

        ``resolved_link`` lets the caller pass a target snapshotted BEFORE an
        injection retry loop — a timeout-path ``sessions.reset()`` evicts the
        in-memory origin link, so resolving only here would lose it.

        Returns True when the reply reached the channel; False degrades the
        caller to dashboard-notification-only. Never raises: a delivery
        failure must not break completion handling for the other paths.
        """
        if not text.strip() or self.dashboard_state is None:
            return False
        if resolved_link is None:
            resolved_link = self._channel_reply_link(parent_key)
        if resolved_link is None:
            return False
        link, needs_dm_resolution = resolved_link
        try:
            # Off-loop: the ladder's governance gate walks the profile
            # directory (iterdir + stat, with a possible reload), which is
            # unbounded on slow or networked storage.
            target = await asyncio.to_thread(
                _resolve_channel_target, self.dashboard_state, parent_key, link
            )
        except Exception:
            logger.exception("Subagent reply: channel target resolution failed for %s", parent_key)
            return False
        if target is None:
            return False
        resolved, transport = target
        try:
            conversation_id = resolved.channel_id
            if needs_dm_resolution:
                # The stored value is the direct peer's user id, not a postable
                # conversation. resolve_configured_target("user:<id>") is the
                # transport contract for exactly this: it enforces the
                # transport's allow-list and returns the real send target
                # (learned conversation on Teams, DM-channel creation on
                # Discord, identity on Telegram) — or None when the peer has
                # no reachable conversation, which fails closed here.
                dm_target = await transport.resolve_configured_target(f"user:{resolved.channel_id}")
                # Audit the allow-list decision (allowed/denied) BEFORE
                # branching, matching chat_mirror's configured-target resolve:
                # a peer the resolver rejects is an authorization outcome and
                # must land in the SEL trail, not just degrade silently.
                sel().log_api_access(
                    caller="subagent",
                    operation="subagent.reply_target_resolve",
                    outcome="allowed" if dm_target else "denied",
                    source="gateway",
                    resources=f"{parent_key} -> {link.channel_type}:user:{resolved.channel_id}",
                )
                if not dm_target or not dm_target[0]:
                    return False
                conversation_id = dm_target[0]
            # Redact through the canonical egress shim so a loaded companion's
            # extra credential/token regexes apply, then split on the
            # channel's max message length, mirroring the cross-surface
            # mirror leg.
            safe_text = redact_via_context(text)
            parts = chunk_text(safe_text, transport.capabilities.max_message_chars)
            for part in parts:
                await transport.send_message(conversation_id, part, thread_id=resolved.thread_id)
        except Exception:
            logger.warning(
                "Subagent reply: %s delivery failed for %s",
                link.channel_type,
                parent_key,
                exc_info=True,
            )
            return False
        logger.info(
            "Subagent reply → %s:%s (%d part(s))",
            link.channel_type,
            conversation_id,
            len(parts),
        )
        return True

    def _cron_job_is_silent(self, parent_key: str) -> bool:
        """Return True if *parent_key* maps to a cron job marked silent.

        ``_deliver_cron_response`` routes a cron
        session's post-subagent-completion turn to Slack, gated on
        ``info.silent`` — the *sub-agent's* flag. That flag is never set from
        the parent cron's ``silent`` setting (``spawn`` defaults it False and
        the spawn queue tuple doesn't carry it), so a silent cron's subagent
        completions still reached Slack. The cron job's own ``silent`` flag is
        the source of truth, so resolve it here. ``parent_key`` is
        ``cron:{job_id}`` or ``cron:{job_id}:{run_id}``.
        """
        if not parent_key.startswith("cron:") or self.cron_svc is None:
            return False
        parts = parent_key.split(":", 2)
        if len(parts) < 2:
            return False
        job = self.cron_svc.get_job(parts[1])
        return bool(job and job.silent)

    async def _init_cron(self) -> None:
        """Initialize and start the cron service."""

        async def _deliver_script_result(
            job: CronJob, message: str, *, remove: bool = False
        ) -> None:
            """Deliver a script cron result to the originating session. Optionally remove the job."""
            delivered = False
            try:
                if message and not job.silent and self.dashboard_state and job.session_key:
                    slot_key = job.session_key.removeprefix("dashboard:")
                    slot = self.dashboard_state.get_slot(slot_key)
                    if slot is None:
                        slot = _rehydrate_slot_from_history(self.dashboard_state, slot_key)
                    label = redact(job.name)
                    if slot:
                        wrapped = f'[Cron notification: "{label}"]\n{message}\n[/Cron notification]'
                        inject_cls = json.dumps({"cronLabel": label})
                        if slot.running:
                            qid = slot.queue_append(wrapped, kind=CRON_NOTIFICATION_KIND)
                            _cls = json.loads(inject_cls)
                            _cls["queue_id"] = qid
                            slot.append("queued", wrapped, json.dumps(_cls))
                        else:
                            # `cls` is not persisted for role `inject`, so the label
                            # must also ride in `meta`, which is — otherwise the row
                            # loses its identity on the next rehydrate.
                            slot.append(
                                "inject",
                                wrapped,
                                inject_cls,
                                meta={"injectKind": "cron", "cronLabel": label},
                            )
                            task = spawn_guarded_turn(
                                self.dashboard_state,
                                slot,
                                _run_chat(self.dashboard_state, slot, wrapped),
                            )
                            slot.task = task
                        self.dashboard_state.push_slots_update()
                    else:
                        self.dashboard_state.notify(
                            "cron", f"⚡ {label}", message, meta={"job_id": job.id}
                        )
                elif message and not job.silent and self.dashboard_state:
                    label = redact(job.name)
                    self.dashboard_state.notify(
                        "cron", f"⚡ {label}", message, meta={"job_id": job.id}
                    )
                delivered = True
            except Exception as notify_exc:
                logger.warning("Cron '%s' delivery failed: %s", job.name, notify_exc)
            if remove and delivered and self.cron_svc:
                try:
                    await self.cron_svc.remove_job_async(job.id)
                except CronStoreBusy:
                    # No caller to retry this fire-and-forget removal, so hand
                    # it to the service's deferred-removal queue: the job is
                    # disabled in memory immediately (can't re-fire) and the
                    # next timer tick drains it from disk under the store lock.
                    self.cron_svc.defer_removal(job.id)
                    logger.warning(
                        "Cron '%s': store busy, queued one-shot removal for " "the next timer tick",
                        job.name,
                    )

        async def _alert_cron_failure(job: CronJob, detail: str, *, denied: bool = False) -> None:
            """Tell the user WHY a script/command cron run failed or was denied.

            The script and command paths signal failure by mutating the job
            (``last_status="error"`` + ``last_error``) and returning normally, so
            they never reached the message path's failure alert below — the reason
            existed only in the gateway log and in a dashboard field nobody is
            watching when the notification they expected simply never arrives. A
            job whose every run dies on a startup-time ``RuntimeError`` therefore
            looked idle rather than broken.

            Deliberately NOT a delivery of the run's *result*: this is a bell plus
            a DM carrying the reason, never an injected turn like
            :func:`_deliver_script_result`. A job failing on its own schedule must
            not spend a model turn per failure, and an injected turn is exactly how
            a failing cron would amplify itself.

            Contract, so the two failure surfaces cannot drift:

            * ``record_failure()`` is NOT called here. Every call site already
              counted the run (or deliberately did not, for a policy denial), and
              the counter has one owner per run — see :func:`_apply_gate_verdict`.
              Callers therefore alert AFTER counting, so ``consecutive_failures``
              reads true if a future body wants it.
            * Dedup reuses the SAME ``last_failure_hash`` / ``last_failure_at``
              fields as the message path. A job is exactly one kind, so the two
              writers never interleave on one job, and a run that fails
              identically every minute alerts once per
              ``_FAILURE_REMINDER_SECS`` instead of once per fire.
            * Never raises. Every call site is inside an ``except`` block whose
              exception is the real story; an alert that failed must not replace
              it. Cancellation still propagates (``CancelledError`` is a
              ``BaseException``).
            """
            try:
                if job.silent:
                    # Silent jobs still execute and still count toward auto-pause;
                    # only the user-facing surfaces are suppressed.
                    return
                text = redact(detail or "")
                text, _ = redact_exfiltration_urls(text)
                text, _ = redact_credentials(text)
                text = text.strip()[:_CRON_FAILURE_DETAIL_CAP] or "no reason reported"
                # job.name is user-controlled and the reason can carry subprocess
                # output, so both are scrubbed once, ahead of either surface and
                # of either branch below.
                label = redact(job.name)
                label, _ = redact_exfiltration_urls(label)
                label, _ = redact_credentials(label)
                mark = "⛔" if denied else "❌"
                headline = "Blocked by policy" if denied else "Run failed"
                # Denials and failures hash apart so a policy denial does not read
                # as a dup of a same-worded crash (and vice versa).
                fh = _result_hash(f"{'denied' if denied else 'failed'}:{text}")
                if (
                    fh == job.last_failure_hash
                    and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS
                ):
                    logger.info(
                        "Cron '%s': duplicate failure alert suppressed (%s)",
                        job.name,
                        "denied" if denied else "failed",
                    )
                    # Same split the message path draws: the LOCAL bell still
                    # rings (marked suppressed, so a user watching the feed sees
                    # the job is still down) and only the Slack DM is withheld.
                    try:
                        if self.dashboard_state:
                            self.dashboard_state.notify(
                                "cron",
                                f"🔇 Cron: {label} (repeat)",
                                f"{mark} Still failing (suppressed — same reason):\n{text}",
                                meta={"job_id": job.id, "failure_hash": fh},
                            )
                    except Exception:
                        logger.debug(
                            "Dashboard notify failed in cron run-failure suppress path",
                            exc_info=True,
                        )
                    return
                try:
                    if self.dashboard_state:
                        self.dashboard_state.notify(
                            "cron",
                            f"Cron: {label}",
                            f"{mark} {headline}:\n{text}",
                            meta={"job_id": job.id, "failure_hash": fh},
                        )
                except Exception:
                    logger.debug(
                        "Dashboard notify failed in cron run-failure alert path", exc_info=True
                    )
                slack_failed = False  # real delivery exceptions only
                if self.slack:
                    # Name the machine for the same reason the message path does:
                    # a laptop and a cloud desktop can both run Kiro Crew, and the
                    # DM is the only place the user learns which one failed.
                    host = socket.gethostname().split(".")[0]
                    # Slack PARSES entity markup in a message's text, and both
                    # halves of this one are attacker-shaped: the job name is
                    # user-authored and the reason carries subprocess output. A job
                    # named `<!channel>` would notify every member of the channel
                    # the moment it failed. Escape at the Slack-facing sink only --
                    # the dashboard body above is not a mrkdwn sink, and escaping
                    # there would show a literal `&lt;`.
                    safe_label = escape_mrkdwn(label)
                    # Escaping does not neutralize a fence: three backticks inside
                    # the reason would close this one early and hand the remainder
                    # to the mrkdwn parser as markup.
                    safe_text = escape_mrkdwn(text).replace("```", "'''")
                    msg = (
                        f"⏰ *Cron: {safe_label}* {mark} "
                        f"_{headline} on {escape_mrkdwn(host)}_\n```{safe_text}```"
                    )
                    msg, _ = redact_exfiltration_urls(msg)
                    msg, _ = redact_credentials(msg)
                    try:
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            await self.slack.post_message(channel, msg)
                        else:
                            logger.warning(
                                "Cron '%s': no channel resolved for run-failure alert", job.name
                            )
                    except Exception:
                        slack_failed = True
                        logger.error(
                            "Cron '%s': Slack run-failure alert delivery failed",
                            job.name,
                            exc_info=True,
                        )
                # Advance dedup only once the reason actually reached someone.
                # "No channel available" counts as delivered (the bell rang), the
                # same skip-vs-failure split the message path draws — otherwise a
                # Slack-less install re-notifies the dashboard on every fire.
                if not slack_failed:
                    job.last_failure_hash = fh
                    job.last_failure_at = time.time()
                try:
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_run_failure_alert",
                        outcome="denied" if denied else "alerted",
                        downstream_service="slack" if self.slack else "none",
                    )
                except Exception:
                    logger.debug("SEL logging failed in cron run-failure alert path", exc_info=True)
            except Exception:
                logger.warning("Cron '%s': run-failure alert failed", job.name, exc_info=True)

        async def _cron_callback(job: CronJob) -> str | None:
            # True once ANY prompt has been handed to the provider this
            # invocation. The whole-callback transient retry below is only
            # safe BEFORE dispatch: after it, tools may have run, so a
            # resubmit risks duplicate side effects (in-stream transient
            # errors are stream_and_collect's own retry's job).
            _prompt_dispatched = False
            # helper picks stable vs ephemeral session key and
            # decides whether to prepend last_result, based on job.persistent_session.
            # Off-loop: this does a config load, a variables-store read and a JSON
            # parse, and the cron callback runs on the gateway's event loop.
            session_key, msg = await asyncio.to_thread(build_cron_session_context, job)

            # ── Concurrent execution guard ──
            if (job.script or job.command) and job.id in self._running_script_ids:
                logger.info("Cron '%s': previous execution still running, skipping", job.name)
                return None

            # ── Command mode: direct shell execution (sandboxed) ──
            if job.command:
                self._running_script_ids.add(job.id)
                # Bound to the overlap guard's own lifetime, and created HERE rather
                # than at the submit below so the ``finally`` that releases the guard
                # can always reach it -- including on the fire-time deny paths that
                # return before anything is submitted.
                handoff = _ClaimHandoff()
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="invoked",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command invoked path", exc_info=True
                        )
                    # Re-run governance at fire time, not just at cron_add authoring
                    # time. A job vetted when it was scheduled can outlive a later
                    # policy tightening: the mcp_cron _vet_* gates only run once, at
                    # authoring, so a ceiling change has no effect on an
                    # already-scheduled job until someone notices and re-authors it.
                    # Denial here does not delete the job, so a later policy
                    # loosening lets it resume on its own. vet_job_at_fire_time is
                    # the shared gate for all three job kinds (command/script/message).
                    # Off-loop: the script variant of this gate reads the script
                    # file from disk, and governance profile resolution can touch
                    # the filesystem too — neither may block the event loop.
                    #
                    # On the GOVERNANCE pool, deliberately NOT the cron pool. This
                    # await sits inside the deadline _execute_with_timeout arms
                    # BEFORE the callback runs, so whatever this gate waits for is
                    # charged to the job's own execution budget. The cron pool is
                    # bounded at _MAX_CRON_WORKERS and its workers are held for a
                    # whole job's DURATION, so gating there puts a short policy
                    # check behind however many long-running command/script jobs
                    # currently occupy it -- and a job whose budget is spent that
                    # way is killed having run no code, reported as an overrun,
                    # and (if delete_after_run) deleted without ever dispatching.
                    # The governance pool holds only short, bounded policy work,
                    # which is what makes the residual wait here proportionate.
                    #
                    # The alternative -- widening every job's deadline by the pool
                    # allowance instead -- is the wrong lever twice over: it would
                    # delay the wedged-delivery backstop by that allowance for
                    # runs that never queue, and it would leave THIS wait
                    # unbounded and still misreported, merely later.
                    gate_reason, gate_starved = await _await_cron_fire_time_gate(
                        job, tool_name="cron_command_exec", tool_kind="cron_command"
                    )
                    if gate_starved:
                        return None
                    if gate_reason:
                        # Deliberately NOT record_failure(): a governance denial
                        # is a policy state, not a job defect. Counting it would
                        # auto-pause the job after _AUTO_PAUSE_THRESHOLD fires,
                        # and a paused job never fires again — breaking the
                        # documented resume-on-policy-loosening semantic.
                        # A denial is result-less: without this the run shows a
                        # PREVIOUS run's output beside this run's error status.
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = redact(gate_reason)
                        job.fire_time_denied = True
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name="cron_command_exec",
                                tool_kind="cron_command",
                                outcome="denied",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron command fire-time deny path",
                                exc_info=True,
                            )
                        await _alert_cron_failure(job, gate_reason, denied=True)
                        return None
                    cmd_timeout = job.timeout or 300
                    # Queue wait is NOT charged to cmd_timeout: see
                    # run_in_cron_pool.  The timeout here is a backstop only --
                    # run_command_sandboxed already enforces cmd_timeout on the
                    # subprocess itself -- but it must stay, or a wedged worker
                    # leaves this entry un-failed forever.
                    #
                    # Submitted through _vet_at_claim_then for the same reason as
                    # the script site: the command TEXT cannot be substituted
                    # (it is already captured in job.command), but the governance
                    # POLICY it was vetted against can tighten during the queue
                    # wait, and the gate above ran before that wait.
                    result = await run_in_cron_pool(
                        _vet_at_claim_then,
                        handoff,
                        job,
                        run_command_sandboxed,
                        job.command,
                        cmd_timeout,
                        job.id,
                        timeout=_claim_backstop(job, cmd_timeout),
                    )
                    if result.get("status") == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    output = result.get("output", "")
                    if not output.strip():
                        if result.get("status") == "ok":
                            # Cleared, not marked: last_status already says the run
                            # succeeded, so last_result carries produced text only.
                            job.clear_carried_result()
                            job.last_status = "ok"
                            job.last_error = ""
                            job.record_success()
                        else:
                            # Cleared so displays fall back to last_error below.
                            job.clear_carried_result()
                            job.last_status = "error"
                            job.last_error = (
                                f"non-ok status with no output (status={result.get('status')})"
                            )
                            job.record_failure()
                            await _alert_cron_failure(job, job.last_error)
                        return None  # no output = no delivery
                    job.set_run_result(redact(output))
                    job.last_error = ""
                    if result.get("status") == "ok":
                        job.last_status = "ok"
                        job.record_success()
                    else:
                        job.last_status = "error"
                        job.last_error = f"command failed (exit_code={result.get('exit_code')})"
                        job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome=job.last_status,
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command result path", exc_info=True
                        )
                    if job.last_status == "error":
                        # A non-zero exit DOES produce output, and that output is
                        # the reason — carry it, not just the exit code.
                        await _alert_cron_failure(job, f"{job.last_error}\n{output}")
                    return job.last_result
                except CronQueueTimeout as exc:
                    # Pool starvation, not a broken command: every worker was
                    # busy for the whole budget so this never started.  Say so,
                    # or the next saturation reads as N independent failures.
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    # Retention-only marker: a one-shot must not be consumed by a
                    # run it never had.  NOT fire_time_denied -- that would also
                    # park an at-job disabled and call this a policy denial.
                    job.run_never_started = True
                    # Deliberately NOT record_failure(): starvation is a fleet
                    # state, not a job defect, exactly as a fire-time governance
                    # denial is a policy state.  Counting it would auto-pause the
                    # job after _AUTO_PAUSE_THRESHOLD starved wakes, and a paused
                    # job never fires again -- so a pool that recovers would leave
                    # a perfectly healthy job disabled and its work unscheduled.
                    # The distinct error text above is what makes the saturation
                    # legible; the counter is for runs that actually ran.
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command queue-timeout path",
                            exc_info=True,
                        )
                    return None
                except asyncio.TimeoutError:
                    # Retain the one-shot when the payload never started. This arm
                    # is the CLAIM BACKSTOP expiring, and it fires for two
                    # different runs: a slow claim-time vet that burned the bound
                    # before ``claim()`` was ever granted, and a payload that DID
                    # start and then overran. Only the first is a never-started
                    # run, and ``abandon()`` is the only thing that can tell them
                    # apart -- it returns whether the payload had started, and
                    # ``claim()`` refuses once it has been called, so a False here
                    # can never become True later. Without this the marker stayed
                    # unset and ``_merge_job_result`` consumed a
                    # ``delete_after_run`` job that dispatched nothing.
                    #
                    # Deliberately scoped to THIS arm rather than to the shared
                    # ``finally`` below, which also runs on the fire-time deny
                    # path: a deny reaches it with the payload equally unstarted,
                    # but its retention is owned by ``fire_time_denied``, whose
                    # readers park an at-job disabled. Setting this marker there
                    # would park a job for a policy decision never made -- the
                    # opposite silent failure. ``abandon()`` is documented
                    # idempotent, so calling it here and again in the ``finally``
                    # is safe.
                    started = handoff.abandon()
                    job.run_never_started = not started
                    job.clear_carried_result()
                    job.last_error = f"timeout ({cmd_timeout + 5}s)"
                    job.last_status = "error"
                    # Count only a run that DISPATCHED. The same reasoning the
                    # starvation, gate-deny and vet-overrun arms above already
                    # apply: a backstop that expired before ``claim()`` means no
                    # line of this job ran, so it is a fleet state rather than a
                    # job defect, and counting it auto-pauses at
                    # _AUTO_PAUSE_THRESHOLD -- a paused job never fires again, so
                    # repeated wedged wakes would permanently disable a healthy
                    # job and leave its work unscheduled. ``cron.py``'s
                    # _execute_with_timeout guard already refuses to count a
                    # never-started run, but this arm calls record_failure()
                    # DIRECTLY and so never reaches it. A genuine overrun (the
                    # payload started, then ran long) still counts, which is what
                    # keeps this a discriminator rather than a deletion.
                    if started:
                        job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="timeout",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command timeout path", exc_info=True
                        )
                    await _alert_cron_failure(job, f"command {job.last_error}")
                    return None
                except CronClaimTimeDenied as exc:
                    # Governance refused this run when the worker claimed it --
                    # the policy tightened during the queue wait.  Same
                    # disposition as the fire-time deny above, deliberately:
                    # result-less, keeps the job, and NOT record_failure(),
                    # because a policy state must not feed the auto-pause
                    # counter.  This clause exists so the run cannot reach the
                    # generic arm below, which does count it.
                    job.clear_carried_result()
                    job.last_status = "error"
                    job.last_error = redact(exc.reason)
                    job.fire_time_denied = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="denied",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command claim-time deny path",
                            exc_info=True,
                        )
                    return None
                except CronVetOverran as exc:
                    # The vet outran the allowance the deadline carries for it, so
                    # the payload was REFUSED rather than started with a margin
                    # that no longer covers its own bound plus teardown.  Nothing
                    # ran, so this is retention-shaped like starvation: mark the
                    # run never-started and deliberately do NOT record_failure() --
                    # a slow governance read is a fleet state, not a job defect,
                    # and counting it would auto-pause a healthy job at
                    # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
                    # decision was made, and that flag also parks an at-job.
                    logger.warning("Cron '%s': %s; payload refused", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    job.run_never_started = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron command vet-overrun path", exc_info=True
                        )
                    return None
                except asyncio.CancelledError:
                    # The wake deadline cancelled this callback outright, so none of
                    # the arms above ran: CancelledError is a BaseException, which
                    # the ``except Exception`` below deliberately does not catch.
                    # Without this the run reaches cron.py's delete site with
                    # neither retention flag set, so a ``delete_after_run`` one-shot
                    # is consumed having never executed.
                    #
                    # ``abandon()`` is the discriminator, exactly as the claim
                    # backstop above uses it: it reports whether the payload had
                    # already started, and ``claim()`` refuses once abandoned, so a
                    # False can never later become True.  Assigning ``not started``
                    # rather than setting True unconditionally is what keeps the
                    # opposite failure closed -- a payload that DID run reports True,
                    # so the marker stays clear and the one-shot is still consumed
                    # instead of firing a second time.
                    #
                    # Deliberately NOT in the shared ``finally`` below, which the
                    # fire-time deny path also reaches with the payload equally
                    # unstarted: retention there is owned by ``fire_time_denied``,
                    # and setting this marker would park an at-job disabled for a
                    # policy decision that was never made.
                    job.run_never_started = not handoff.abandon()
                    raise
                except Exception as exc:
                    logger.exception("Command cron '%s' failed: %s", job.name, exc)
                    job.clear_carried_result()
                    err_str = redact(str(exc))
                    job.last_error = err_str[:200]
                    job.last_status = "error"
                    job.record_failure()
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_command_exec",
                            tool_kind="cron_command",
                            outcome="error",
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron command error path", exc_info=True)
                    await _alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
                    return None
                finally:
                    # Abandon BEFORE releasing the overlap guard, and do it here
                    # rather than in the timeout arm because this ``finally`` is the
                    # single place the guard is released -- so it also covers any
                    # exit no ``except`` arm above handles.  A no-op on the success
                    # path: the payload already ran.  ``abandon()`` is idempotent,
                    # so the cancellation arm above having already called it changes
                    # nothing observed here.
                    handoff.abandon()
                    self._running_script_ids.discard(job.id)

            # ── Code-based script execution (deterministic, no LLM) ──
            if job.script:
                self._running_script_ids.add(job.id)
                # Bound to the overlap guard's own lifetime, and created HERE rather
                # than at the submit below so the ``finally`` that releases the guard
                # can always reach it -- including on the fire-time deny paths that
                # return before anything is submitted.
                handoff = _ClaimHandoff()
                try:
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="invoked",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script invoked path", exc_info=True
                        )
                    # Fire-time governance gate — mirrors the command path above.
                    # vet_job_at_fire_time re-runs the capabilities.cron gate AND
                    # re-scans the script BODY on the freshly re-resolved path
                    # (which also validates the path, as the bare
                    # resolve_script_path call here previously did), so a policy
                    # tightened after scheduling — or a script file edited on disk
                    # after authoring — denies this run. The job is kept: a later
                    # policy loosening lets it resume on its own.
                    # Off-loop: reads the script body from disk (up to the scan
                    # cap) — must not block the event loop on a wedged FS.
                    # Governance pool, not the cron pool: see the command site.
                    gate_reason, gate_starved = await _await_cron_fire_time_gate(
                        job, tool_name="cron_script_exec", tool_kind="cron_script"
                    )
                    if gate_starved:
                        return None
                    if gate_reason:
                        # No record_failure() — see the command-path deny above:
                        # a policy denial must not feed the auto-pause counter.
                        # A denial is result-less: without this the run shows a
                        # PREVIOUS run's output beside this run's error status.
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = redact(gate_reason)
                        job.fire_time_denied = True
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="denied",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script fire-time deny path",
                                exc_info=True,
                            )
                        await _alert_cron_failure(job, gate_reason, denied=True)
                        return None
                    # Run in sandboxed subprocess via wrap_argv()
                    script_timeout = job.timeout or 30
                    # Queue wait is NOT charged to script_timeout: see
                    # run_in_cron_pool.  The timeout here is a backstop only --
                    # run_script_sandboxed already enforces script_timeout on
                    # the subprocess itself -- but it must stay, or a wedged
                    # worker leaves this entry un-failed forever.
                    #
                    # Submitted through _vet_at_claim_then, not bare: the gate
                    # above ran before the queue wait, and the launcher re-reads
                    # the body from disk in the child, so the gate's scan alone
                    # authorises bytes that may no longer be there.  The re-vet
                    # runs inside the worker, after the wait, so the decision
                    # holds at the moment of use.  It also now shares the
                    # backstop below, which is why it must stay short.
                    result = await run_in_cron_pool(
                        _vet_at_claim_then,
                        handoff,
                        job,
                        run_script_sandboxed,
                        job.script,
                        job.id,
                        job.message,
                        script_timeout,
                        timeout=_claim_backstop(job, script_timeout),
                    )
                    status = result.get("status", "error")
                    if status == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    if status == "ok":
                        job.clear_carried_result()
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="ok",
                            )
                        except Exception:
                            logger.debug("SEL logging failed in cron script ok path", exc_info=True)
                        return "ok"
                    elif status == "skip":
                        # A completed Skip is a successful run that chose no-op —
                        # the same "success" outcome as the ok/done/report
                        # siblings above. Unlike them it deliberately does NOT
                        # call job.record_success() here: CronScheduler._execute
                        # is the backstop that resets consecutive_failures (and
                        # lifts auto-pause) on every non-error return — Skip
                        # included, since this branch returns None without
                        # setting last_status="error" — and its reset is guarded
                        # by the _cancelled_jobs cancel-race check. Resetting in
                        # this branch would bypass that guard and could re-enable
                        # a job cancelled mid-tick.
                        # Result-less like the deny paths: a Skip that carried the
                        # previous run's output read as though it had produced it.
                        job.clear_carried_result()
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="skip",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script skip path", exc_info=True
                            )
                        return None
                    elif status == "done":
                        msg = result.get("message", "")
                        script_msg = redact(msg) if msg else ""
                        job.set_run_result(script_msg)
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Deliver Done message and remove job
                        await _deliver_script_result(job, script_msg, remove=True)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="done",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script done path", exc_info=True
                            )
                        return script_msg or "done"
                    elif status == "report":
                        msg = result.get("message", "")
                        script_msg = redact(msg) if msg else ""
                        job.set_run_result(script_msg)
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Deliver Report message (keep job running)
                        await _deliver_script_result(job, script_msg)
                        try:
                            sel().log_tool_invocation(
                                session_key=f"cron:{job.id}",
                                tool_name=job.script,
                                tool_kind="cron_script",
                                outcome="report",
                            )
                        except Exception:
                            logger.debug(
                                "SEL logging failed in cron script report path", exc_info=True
                            )
                        return script_msg or "report"
                    else:
                        err = result.get("error", "unknown error")
                        raise RuntimeError(err)
                except CronQueueTimeout as exc:
                    # Pool starvation, not a broken script: every worker was
                    # busy for the whole budget so this never ran a line.  The
                    # distinct text is what makes the next saturation legible
                    # instead of looking like N scripts that each overran.
                    logger.warning("Script cron '%s' never got a worker slot: %s", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    # Retention-only marker: see the command path.
                    job.run_never_started = True
                    # Deliberately NOT record_failure(): see the command path.
                    # Starvation means the script never ran a line, so counting it
                    # would auto-pause a healthy job after _AUTO_PAUSE_THRESHOLD
                    # starved wakes and leave it disabled once the pool recovered.
                    # The auto-pause log that used to sit here went with it: this
                    # path can no longer reach the threshold.
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script queue-timeout path",
                            exc_info=True,
                        )
                    return None
                except asyncio.TimeoutError:
                    # See the command path above: this is the claim backstop, and
                    # only ``abandon()`` distinguishes a vet that burned the bound
                    # before ``claim()`` from a payload that started and overran.
                    # Scoped to this arm, not the shared ``finally``, so the
                    # fire-time deny path keeps its retention in
                    # ``fire_time_denied`` instead of parking an at-job disabled.
                    started = handoff.abandon()
                    job.run_never_started = not started
                    logger.warning(
                        "Script cron '%s' timed out after %ds", job.name, script_timeout + 5
                    )
                    job.clear_carried_result()
                    job.last_error = f"timeout ({script_timeout + 5}s)"
                    job.last_status = "error"
                    # See the command path: count only a run that DISPATCHED, so a
                    # backstop that expired before ``claim()`` cannot auto-pause a
                    # job that never ran a line. A genuine overrun still counts.
                    if started:
                        job.record_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Script cron '%s' auto-paused after %d consecutive errors",
                            job.name,
                            job.consecutive_failures,
                        )
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=f"timeout ({script_timeout + 5}s)",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script timeout path", exc_info=True
                        )
                    await _alert_cron_failure(job, f"script {job.last_error}")
                    return None
                except CronClaimTimeDenied as exc:
                    # Governance refused this run when the worker claimed it --
                    # the body on disk, or the policy, changed during the queue
                    # wait.  Same disposition as the fire-time deny above,
                    # deliberately: result-less, keeps the job, and NOT
                    # record_failure(), because a policy state must not feed the
                    # auto-pause counter.  This clause exists so the run cannot
                    # reach the generic arm below, which does count it.
                    job.clear_carried_result()
                    job.last_status = "error"
                    job.last_error = redact(exc.reason)
                    job.fire_time_denied = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="denied",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script claim-time deny path",
                            exc_info=True,
                        )
                    return None
                except CronVetOverran as exc:
                    # The vet outran the allowance the deadline carries for it, so
                    # the payload was REFUSED rather than started with a margin
                    # that no longer covers its own bound plus teardown.  Nothing
                    # ran, so this is retention-shaped like starvation: mark the
                    # run never-started and deliberately do NOT record_failure() --
                    # a slow governance read is a fleet state, not a job defect,
                    # and counting it would auto-pause a healthy job at
                    # _AUTO_PAUSE_THRESHOLD.  NOT fire_time_denied: no policy
                    # decision was made, and that flag also parks an at-job.
                    logger.warning("Cron '%s': %s; payload refused", job.name, exc)
                    job.clear_carried_result()
                    job.last_error = str(exc)
                    job.last_status = "error"
                    job.run_never_started = True
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=str(exc),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron script vet-overrun path", exc_info=True
                        )
                    return None
                except asyncio.CancelledError:
                    # See the command path above: a wake deadline cancelling this
                    # callback runs no ``except`` arm, because CancelledError is a
                    # BaseException, so without this the delete site consumes a
                    # one-shot that never executed.  ``abandon()`` reports whether
                    # the payload had started, so ``not started`` retains only the
                    # run that dispatched nothing and leaves a completed run
                    # deletable.  Not in the shared ``finally``, whose deny path
                    # retention belongs to ``fire_time_denied``.
                    job.run_never_started = not handoff.abandon()
                    raise
                except Exception as exc:
                    logger.exception("Script cron '%s' failed: %s", job.name, exc)
                    job.clear_carried_result()
                    err_str = redact(str(exc))
                    job.last_error = err_str
                    job.last_status = "error"
                    job.record_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Script cron '%s' auto-paused after %d consecutive errors",
                            job.name,
                            job.consecutive_failures,
                        )
                    try:
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name=job.script,
                            tool_kind="cron_script",
                            outcome="error",
                            error=err_str,
                        )
                    except Exception:
                        logger.debug("SEL logging failed in cron script error path", exc_info=True)
                    # The reason a script cron dies is often environmental (a
                    # startup RuntimeError, a missing dependency) and identical on
                    # every fire, so it read as an idle job rather than a broken
                    # one until this alert carried the reason out.
                    await _alert_cron_failure(job, f"{type(exc).__name__}: {exc}")
                    return None
                finally:
                    # Abandon BEFORE releasing the overlap guard, and do it here
                    # rather than in the timeout arm because this ``finally`` is the
                    # single place the guard is released -- so it also covers any
                    # exit no ``except`` arm above handles.  A no-op on the success
                    # path: the payload already ran.  ``abandon()`` is idempotent,
                    # so the cancellation arm above having already called it changes
                    # nothing observed here.
                    handoff.abandon()
                    self._running_script_ids.discard(job.id)

            # ── Fire-time governance gate: message (LLM) jobs ──
            # Command and script jobs are gated inside their blocks above; a job
            # reaching this point dispatches an LLM turn. Message jobs previously
            # had NO fire-time capabilities.cron check at all, so disabling the
            # cron capability after scheduling never affected them. Same deny
            # semantics as the other kinds: mark the run failed, keep the job.
            # Off-loop for the same reason as the command/script sites above, and
            # on the GOVERNANCE pool for the same reason: a message job gets no
            # pool allowance on either deadline, so gating it on the cron pool
            # charged a saturated pool's queue directly to its execution budget.
            gate_reason, gate_starved = await _await_cron_fire_time_gate(
                job, tool_name="cron_message_dispatch", tool_kind="cron_message"
            )
            if gate_starved:
                return None
            if gate_reason:
                # No record_failure() — see the command-path deny above: a
                # policy denial must not feed the auto-pause counter.
                job.last_status = "error"
                job.last_error = redact(gate_reason)
                job.fire_time_denied = True
                try:
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_message_dispatch",
                        tool_kind="cron_message",
                        outcome="denied",
                    )
                except Exception:
                    logger.debug(
                        "SEL logging failed in cron message fire-time deny path", exc_info=True
                    )
                await _alert_cron_failure(job, gate_reason, denied=True)
                return None

            def _cron_extra_env() -> dict[str, str] | None:
                """job.env plus KIROCREW_APPROVAL_MODE when the job runs auto.

                SubagentManager.spawn's own auto-approve fallback for a cron's
                spawn_run subagents (parent_trusted, i.e.
                sessions.get_approval_policy(parent_session_key)=="auto")
                depends on parent_session resolving back to this cron's session
                key -- an identity-plumbing path that can fail silently and
                leave the spawn stuck on the interactive approval path a cron
                has no responder for. Injecting the mode directly as an env var
                the spawned kiro-cli process inherits (mirroring
                KIROCREW_SESSION_KEY/KIROCREW_CHANNEL_ID) lets the spawn_run MCP
                tool (mcp_core.py) read and forward its own approval_mode
                explicitly, independent of whether parent_session resolution
                succeeds.

                KIROCREW_APPROVAL_MODE is a RESERVED control var: it is stripped
                from the app/user-controlled ``job.env`` on every path and only
                re-injected here when the job's VALIDATED ``approval_mode`` is
                "auto". Otherwise an app manifest could set it directly in
                ``job.env`` and have an interactive cron's spawn_run subagents
                silently auto-approved -- an authorization bypass.
                """
                env = {k: v for k, v in (job.env or {}).items() if k != "KIROCREW_APPROVAL_MODE"}
                if job.approval_mode == "auto":
                    env["KIROCREW_APPROVAL_MODE"] = "auto"
                return env or None

            async def _acquire_with_model_fallback(
                key: str, agent_id: str | None
            ) -> "tuple[LLMProvider, bool, bool, bool]":
                """get_or_create honoring job.model; if that model is
                unavailable, retry once with the registry default.
                Returns (client, is_new, resumed, downgraded)."""
                assert self.sessions is not None
                try:
                    client, is_new, resumed = await self.sessions.get_or_create(
                        key,
                        agent=agent_id,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        model=job.model or None,
                        extra_env=_cron_extra_env(),
                    )
                    return client, is_new, resumed, False
                except Exception as model_exc:
                    if not job.model:
                        raise
                    # Only fall back when the failure plausibly implicates the
                    # pinned model; unrelated session-creation errors (provider
                    # spawn, missing factory, transient I/O) must propagate so
                    # they are not misreported as a model downgrade.
                    _err = str(model_exc).lower()
                    if "model" not in _err and job.model.lower() not in _err:
                        raise
                    logger.warning(
                        "Cron '%s': model %r unavailable (%s); retrying with default",
                        job.name,
                        job.model,
                        model_exc,
                    )
                    client, is_new, resumed = await self.sessions.get_or_create(
                        key,
                        agent=agent_id,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        extra_env=_cron_extra_env(),
                    )
                    return client, is_new, resumed, True

            def _annotate_model_downgrade(text: str) -> str:
                # job.model is LLM-controllable via MCP; redact before it
                # reaches Slack/dashboard through last_result.
                safe_model = redact_credentials(redact_exfiltration_urls(job.model)[0])[0]
                return f"⚠️ Model '{safe_model}' unavailable; ran with default.\n\n" + text

            # ── Sequential agent execution ──
            # When agent_sequence has multiple agents, run them sequentially
            # with per-agent session keys and per-job env vars.
            agents = job.agent_sequence if job.agent_sequence else []
            if len(agents) > 1:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                result_text = "_No response._"
                _seq_downgraded = False
                # Run-scoped: a sequence where one agent got a tool through has
                # done work, even if a later agent was blocked outright.
                _gate = _GateTally()
                for agent in agents:
                    agent_session_key = f"cron:{job.id}:{agent}"
                    if self.cron_svc is not None:
                        self.cron_svc.register_active_session_key(job.id, agent_session_key)
                    _acq = False
                    try:
                        client, is_new, _resumed, _downgraded = await _acquire_with_model_fallback(
                            agent_session_key, agent
                        )
                        _seq_downgraded = _seq_downgraded or _downgraded
                        _acq = True
                        # Publish this turn's session identity so managed MCP
                        # tools resolve their parent session. The cron path was
                        # the ONE turn-running surface that skipped this (every
                        # other surface publishes — see messaging.identity), and
                        # under session sharing the runtime env carries no
                        # KIROCREW_SESSION_KEY and macOS sets no
                        # KIROCREW_HOST_PID, so the ancestor PID-walk over the
                        # per-turn pidfile mapping is the only identity source
                        # left. Without the publish, spawn_run resolved an
                        # empty parent ("notification only (parent=)") unless an
                        # unrelated surface happened to be mid-turn.
                        await publish_turn_identity(self.sessions, agent_session_key)
                        # Off-loop, and the ARGUMENT has to be too: this is evaluated
                        # before run_in_embed_pool is even called, so leaving it inline
                        # ran a config load, a variables-store read and a JSON parse on
                        # the event loop for every sequence member.
                        #
                        # Expanded PER MEMBER: agent_sequence takes precedence over
                        # agent_id, so the single expansion computed above from
                        # agent_id resolves the wrong crew here (the DEFAULT crew for
                        # a job that sets only a sequence).
                        #
                        # Through build_cron_session_context, NOT the bare expander:
                        # only this path prepends the last_result carry-over, so
                        # calling the expander directly dropped the prior-run context
                        # and the do-not-repeat instruction from every run after the
                        # first.
                        _member_msg = (
                            await asyncio.to_thread(build_cron_session_context, job, agent)
                        )[1]
                        # Off-loop: build_message embeds the episodic query.
                        full_message, _ = await run_in_embed_pool(
                            self.ctx_builder.build_message,
                            _member_msg,
                            True,
                            interactive=False,
                            agent=agent,
                            crew=agent,
                            # ``msg`` has already had crew variables expanded by
                            # build_cron_session_context, so trigger matching gets
                            # the author's own text instead: a variable VALUE must
                            # never be able to pull in a skill body.
                            trigger_text=job.message,
                        )
                        # Wall clock for the cron agent turn: acp never assigns
                        # TurnUsage.duration_ms, so the row falls back to this.
                        # Brackets only the model turn — session acquisition and
                        # the episodic-query embed above are setup, not the turn.
                        _turn_t0 = time.monotonic()
                        _prompt_dispatched = True
                        result_text, _carried_credits = await _cron_stream_with_posttoken_resume(
                            client,
                            full_message,
                            job_name=job.name,
                            approval_policy=(
                                ToolApprovalPolicy.AUTO_APPROVE
                                if job.approval_mode == "auto"
                                else ToolApprovalPolicy.HOOK_BASED
                            ),
                            hooks=self.ctx_builder.hooks,
                            on_tool_approval=(
                                None
                                if job.approval_mode == "auto"
                                else self._interactive_approval("cron")
                            ),
                            on_tool_gate=_gate.note,
                        )
                        if not result_text:
                            result_text = "_No response._"
                        logger.info("Cron '%s': agent '%s' completed", job.name, agent)

                        # ── Per-turn usage row: background spend. ──
                        try:

                            _used, _window = read_context_tokens(client)
                            _turn_usage = provider_last_turn_usage(client)
                            if _carried_credits:
                                # A resumed turn's post-turn read sees only the
                                # continuation prompt; bill the interrupted
                                # prompt's snapshotted credits too.
                                _turn_usage.credits += _carried_credits
                            await persist_token_record_async(
                                agent_session_key,
                                # Blank on a downgrade: the configured model was
                                # unavailable and the default ran instead, so the
                                # requested id would attribute spend to a model
                                # that never executed. Blank defers to
                                # model_source, which reports what actually ran.
                                "" if _seq_downgraded else (job.model or ""),
                                _turn_usage,
                                provider=(
                                    self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                                ),
                                surface="cron",
                                agent=read_effective_agent(client) or agent or "",
                                context_used=_used,
                                context_window=_window,
                                elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                                model_source=client,
                            )
                        except Exception:
                            logger.debug("usage row (cron seq) persist failed", exc_info=True)
                    finally:
                        if _acq:
                            self.sessions.release(agent_session_key)
                            # Mirror the single-agent finally below: defer the
                            # reset when this agent's sub-agents are still
                            # running, QUEUED behind the concurrency/stagger
                            # gate, or mid-injection — _subagent_done resets
                            # after the last one. Now that this path publishes
                            # turn identity, a non-final agent's spawn_run
                            # resolves a REAL parent key, so an unconditional
                            # reset here would tear down the session a pending
                            # completion is about to inject into (cold-starting
                            # a context-free replacement) and the completion's
                            # own cleanup would clear the reaper registration
                            # for the NEXT agent's still-in-flight turn.
                            _has_pending = bool(
                                self.subagent_mgr
                                and self.subagent_mgr.has_pending_work_for(agent_session_key)
                            )
                            _has_injecting = self._cron_injecting.get(agent_session_key, 0) > 0
                            if _has_pending or _has_injecting:
                                logger.info(
                                    "Cron '%s': deferring reset of %s, subagents pending",
                                    job.name,
                                    agent_session_key,
                                )
                            else:
                                await self.sessions.reset(agent_session_key)
                                if self.cron_svc is not None:
                                    self.cron_svc.clear_active_session_key(job.id)
                if _seq_downgraded:
                    result_text = _annotate_model_downgrade(result_text)
                job.set_run_result(result_text)
                # This path owns the same verdict as the single-agent one, so a
                # multi-agent job's failure counter moves in both directions —
                # which is what keeps auto-pause both reachable and clearable.
                _apply_gate_verdict(job, _gate)
                return result_text

            # ── Single-agent path (existing behavior) ──
            # Tell the reaper which key to target if this run hangs.
            if self.cron_svc is not None:
                self.cron_svc.register_active_session_key(job.id, session_key)

            _acquired = False
            _model_downgraded = False
            # Set when the gate verdict below already counted this run, so the
            # exception handler does not count it a second time.
            _gate_counted = False
            try:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                client, is_new, _resumed, _model_downgraded = await _acquire_with_model_fallback(
                    session_key, job.agent_id or None
                )
                _acquired = True
                # Same identity publish as the sequential site above — the
                # single-agent cron turn must publish its pidfile mapping or
                # spawn_run's parent resolution has no source to walk to.
                await publish_turn_identity(self.sessions, session_key)
                if job.acked_items:
                    msg += (
                        "\n\n[User has seen and acknowledged ALL of the following — "
                        "do NOT repeat the same content]\n"
                        + "\n".join(f"- {a}" for a in job.acked_items)
                    )
                _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                # Off-loop: build_message embeds the episodic query.
                full_message, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    msg,
                    True,
                    interactive=False,
                    agent=job.agent_id or None,
                    # Expanded upstream; triggers see the authored text. See the
                    # sequential site above.
                    trigger_text=job.message,
                    provider_type=_provider,
                    minimal_context=job.minimal_context,
                )

                # Wall clock for the cron agent turn — see the sequential site
                # above. acp reports no duration, so this is the row's fallback.
                _turn_t0 = time.monotonic()
                _gate = _GateTally()
                _prompt_dispatched = True
                result_text, _carried_credits = await _cron_stream_with_posttoken_resume(
                    client,
                    full_message,
                    job_name=job.name,
                    approval_policy=(
                        ToolApprovalPolicy.AUTO_APPROVE
                        if job.approval_mode == "auto"
                        else ToolApprovalPolicy.HOOK_BASED
                    ),
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=(
                        None if job.approval_mode == "auto" else self._interactive_approval("cron")
                    ),
                    on_tool_gate=_gate.note,
                )

                if not result_text:
                    result_text = "_No response._"

                if _model_downgraded:
                    result_text = _annotate_model_downgrade(result_text)

                job.set_run_result(result_text)

                # Context-meter reading for the dashboard slot, captured NOW:
                # the finally block below resets this session, so the open
                # path can never read the provider live. Routed through
                # broadcast_context_usage by inject_cron_result_to_dashboard.
                _ctx_reading = context_meter_reading(client)

                # ── Per-turn usage row: attribute background spend. ──
                # Best-effort; must never fail the cron turn.
                try:

                    _used, _window = read_context_tokens(client)
                    _turn_usage = provider_last_turn_usage(client)
                    if _carried_credits:
                        # See the sequential site above: bill the interrupted
                        # prompt's snapshotted credits alongside the
                        # continuation's on a resumed turn.
                        _turn_usage.credits += _carried_credits
                    await persist_token_record_async(
                        session_key,
                        # Blank on a downgrade — see the sequential site above.
                        "" if _model_downgraded else (job.model or ""),
                        _turn_usage,
                        provider=_provider,
                        surface="cron",
                        agent=read_effective_agent(client) or job.agent_id or "",
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=client,
                    )
                except Exception:
                    logger.debug("usage row (cron) persist failed", exc_info=True)

                # ── Error deduplication ──
                # Suppress Slack for repeated identical results to avoid spam.
                rh = _result_hash(result_text)

                _gate_counted = _apply_gate_verdict(job, _gate)

                if rh == job.last_posted_hash:
                    job.consecutive_dupes += 1
                    # Time-based reminder: re-post after 24h so persistent identical
                    # results don't go unnoticed indefinitely.
                    if time.time() - job.last_posted_at >= _SUCCESS_REMINDER_SECS:
                        # NB: consecutive_dupes is captured here before the reset
                        # at the post-delivery state update further below.
                        result_text = (
                            f"⚠️ Cron '{job.name}' has produced the same result"
                            f" {job.consecutive_dupes} times in a row:\n\n{result_text}"
                        )
                    else:
                        logger.info(
                            "Cron '%s': duplicate result #%d — suppressing Slack",
                            job.name,
                            job.consecutive_dupes,
                        )
                        if self.dashboard_state:
                            redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                            redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                            title = f"🔇 Cron: {job.name} (dup #{job.consecutive_dupes})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                redacted_for_dash,
                                meta={"job_id": job.id},
                            )

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                        # Still inject into dashboard slot even when Slack is suppressed
                        if (
                            self.dashboard_state
                            and job.persistent_session
                            and not job.hide_in_chat
                            and self.dashboard_state.has_slot(f"cron-{job.id}")
                        ):
                            inject_cron_result_to_dashboard(
                                self.dashboard_state,
                                job,
                                result_text,
                                context_reading=_ctx_reading,
                            )
                        return result_text

                if job.silent:
                    logger.info("Cron job '%s' silent — suppressing auto-delivery", job.name)

                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_silent_suppress",
                        outcome="suppressed",
                        downstream_service="none",
                    )
                    # Still inject into dashboard slot even when silent
                    if (
                        self.dashboard_state
                        and job.persistent_session
                        and not job.hide_in_chat
                        and self.dashboard_state.has_slot(f"cron-{job.id}")
                    ):
                        inject_cron_result_to_dashboard(
                            self.dashboard_state,
                            job,
                            result_text,
                            context_reading=_ctx_reading,
                        )
                    return result_text

                if self.dashboard_state:
                    # Inject into slot BEFORE notification so has_slot() is true for notify_meta.
                    # hide_in_chat=True keeps the cron out of the active session list — the
                    # result still reaches Slack/bell below, and the run stays visible in the
                    # History tab via the cron execution-history store (CronHistoryStore, written
                    # unconditionally by the executor and surfaced at GET /api/crons/{id}/history).
                    # NOTE: the cron:{id} dashboard conversation_log is written ONLY by
                    # inject_cron_result_to_dashboard (gated off here for hidden crons), so it is
                    # intentionally empty for a hidden cron — it exists solely to feed a dashboard
                    # follow-up turn, which a no-slot cron never has. Do NOT rely on cron:{id} for
                    # hidden-cron result persistence; get_history() is the source of truth.
                    # This is the only slot *creator* site (get_or_create_slot); the dedup/silent
                    # paths above only re-inject into an already-existing slot via has_slot(), so
                    # they self-no-op when hide_in_chat is True.
                    if job.persistent_session and not job.hide_in_chat:
                        history = (
                            await asyncio.to_thread(
                                self.dashboard_state.conversation_log.read_messages,
                                f"cron:{job.id}",
                            )
                            if self.dashboard_state.conversation_log
                            else []
                        )
                        inject_cron_result_to_dashboard(
                            self.dashboard_state,
                            job,
                            result_text,
                            history=history,
                            context_reading=_ctx_reading,
                        )
                    redacted_for_dash, _ = redact_exfiltration_urls(result_text)
                    redacted_for_dash, _ = redact_credentials(redacted_for_dash)
                    safe_name, _ = redact_exfiltration_urls(job.name)
                    safe_name, _ = redact_credentials(safe_name)
                    notify_meta: dict[str, str] = {"job_id": job.id}
                    # Gate the slot linkage on not hide_in_chat for parity with the
                    # three inject sites above. Without this, a job flipped to
                    # hide_in_chat=True that still owns an older cron-{id} slot would
                    # keep emitting meta.slot (has_slot stays True) → the notification
                    # CTA shows "Continue session" pointing at a slot no longer
                    # receiving results. Gating here forces the no-slot "View last
                    # result" CTA, which lazily rebuilds from CronHistoryStore.
                    if (
                        job.persistent_session
                        and not job.hide_in_chat
                        and self.dashboard_state.has_slot(f"cron-{job.id}")
                    ):
                        notify_meta["slot"] = f"cron-{job.id}"
                    self.dashboard_state.notify(
                        "cron",
                        f"Cron: {safe_name}",
                        redacted_for_dash,
                        meta=notify_meta,
                    )
                if self.slack:
                    try:
                        # Retry only open_dm (transient Slack API errors).
                        # Delivery (post_blocks/post_message) is NOT retried to avoid duplicates.
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            # The caption is redacted-but-not-converted by
                            # render_for_slack's header= seam, which also charges
                            # it against the limit. Doing it there rather than
                            # here is the point: a cron name is LLM-authored (the
                            # agent can create crons via cron_add), and the
                            # hand-rolled version of this had already forgotten to
                            # redact it once.
                            parts = render_for_slack(
                                result_text,
                                limit=_CRON_MSG_LIMIT,
                                header=f"⏰ *Cron: {job.name}*\n\n",
                            )
                            # First part as Block Kit message with ack button
                            blocks: list[dict] = [
                                {
                                    "type": "section",
                                    "text": {"type": "mrkdwn", "text": parts[0]},
                                },
                            ] + build_cron_ack_block(job.id)
                            parent_ts = await self.slack.post_blocks(
                                channel, blocks, parts[0], job.thread_ts
                            )
                            thread_root = job.thread_ts or parent_ts
                            # Store thread_ts so subagents can route replies here
                            if thread_root and self.sessions:
                                await self.sessions.set_thread(session_key, thread_root)
                                await self.sessions.set_channel(session_key, channel)
                            # Overflow parts as threaded follow-up messages
                            for part in parts[1:]:
                                await self.slack.post_message(channel, part, thread_root)
                            # Dedup state: only advance after confirmed delivery.
                            job.last_posted_hash = rh
                            job.consecutive_dupes = 0
                            job.last_posted_at = time.time()
                        else:
                            logger.warning(
                                "Cron '%s': no channel resolved, skipping notification", job.name
                            )
                    except Exception as slack_exc:
                        logger.error(
                            "Cron job '%s': Slack delivery failed (job succeeded)",
                            job.name,
                            exc_info=True,
                        )
                        if self.dashboard_state:
                            exc_msg, _ = redact_exfiltration_urls(str(slack_exc))
                            exc_msg, _ = redact_credentials(exc_msg)
                            self.dashboard_state.notify(
                                "cron",
                                f"Cron: {job.name}",
                                f"⚠️ Job completed but Slack delivery failed: {exc_msg}",
                                meta={"job_id": job.id},
                            )
                # Session cleanup happens in finally block
                return result_text
            except Exception as exc:
                # Attempt one retry for ACP process death before any dedup / alert.
                exc_msg = str(exc).lower()
                if (
                    isinstance(exc, AcpError)
                    and ("not running" in exc_msg or "process exited" in exc_msg)
                    and not getattr(job, "_acp_retried", False)
                    and self.sessions is not None
                ):
                    logger.warning(
                        "Cron '%s': ACP process died, resetting session and retrying",
                        job.name,
                    )
                    job._acp_retried = True  # type: ignore[attr-defined]
                    try:
                        if _acquired:
                            self.sessions.release(session_key)
                            _acquired = False
                        await self.sessions.reset(session_key)
                        return await _cron_callback(job)
                    except Exception:
                        pass  # retry failed — fall through to dedup + alert
                    finally:
                        job._acp_retried = False  # type: ignore[attr-defined]
                # ── Transient backend errors: retry the whole callback with ──
                # backoff instead of counting a failure. stream_and_collect's
                # in-stream retry only covers errors raised INSIDE the prompt
                # stream; a throttle/5xx during session acquire, client
                # creation, or context assembly propagates here and — before
                # this branch existed — went straight to record_failure(),
                # marching consecutive_failures toward auto-pause (threshold
                # 5) on pure infrastructure weather. The subagent path has
                # retried these 3x with backoff since it existed; this brings
                # the cron path to the same semantics (Phase 0, Finding 1:
                # five throttled wakes would silently auto-pause a healthy
                # perpetual agent).
                #
                # Guarded by the same recursion marker pattern as the ACP
                # retry: the attempt counter lives on the job for the duration
                # of the outermost invocation only, and the recursive call
                # re-enters the full callback so a retry that succeeds runs
                # the complete delivery path.
                if acp_error_is_transient(exc) and not _prompt_dispatched:
                    _t_attempt = getattr(job, "_transient_attempts", 0)
                    if _t_attempt < _CRON_TRANSIENT_RETRIES:
                        job._transient_attempts = _t_attempt + 1  # type: ignore[attr-defined]
                        _delay = transient_retry_delay(_t_attempt + 1)
                        logger.warning(
                            "Cron '%s': transient backend error (attempt %d/%d), "
                            "retrying in %.1fs: %s",
                            job.name,
                            _t_attempt + 1,
                            _CRON_TRANSIENT_RETRIES,
                            _delay,
                            exc,
                        )
                        # The backoff sleep AND the recursive call live inside
                        # the counter-owning try/finally: a wake-budget
                        # cancellation (asyncio.wait_for) landing in the sleep
                        # would otherwise strand the just-consumed attempt on
                        # the in-memory job, and later wakes would start with
                        # fewer (or zero) retries.
                        try:
                            try:
                                if _acquired and self.sessions is not None:
                                    self.sessions.release(session_key)
                                    _acquired = False
                            except Exception:
                                logger.debug("release before transient retry failed", exc_info=True)
                            await asyncio.sleep(_delay)
                            return await _cron_callback(job)
                        finally:
                            # Outermost frame owns the counter: clear it once
                            # the retry chain unwinds — success, failure, or
                            # cancellation.
                            if _t_attempt == 0:
                                job._transient_attempts = 0  # type: ignore[attr-defined]
                    # Retries exhausted — fall through to dedup + alert +
                    # record_failure: a persistent outage should still count.
                logger.exception("Cron job '%s' failed", job.name)
                # During an in-flight ACP retry (inner recursive _cron_callback
                # call), suppress all notify/slack/dedup work — the outer
                # invocation is authoritative and will handle notification
                # for the retry's final failure. Without this guard, the
                # inner call emits its own dashboard notify + Slack alert
                # and advances dedup state, duplicating the outer handler.
                if getattr(job, "_acp_retried", False):
                    raise
                # ── Failure dedup: suppress repeated identical crash notifications ──
                exc_summary = f"{type(exc).__name__}: {exc}"
                exc_summary, _ = redact_exfiltration_urls(exc_summary)
                exc_summary, _ = redact_credentials(exc_summary)
                fh = _result_hash(exc_summary)
                is_dup = fh == job.last_failure_hash
                if is_dup and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS:
                    # record_failure() is the counter's sole owner: a suppressed
                    # duplicate is still a failed run, so it must count toward
                    # the auto-pause threshold like every other failure path —
                    # unless the gate verdict already counted THIS run, in which
                    # case counting again would pause on arithmetic rather than
                    # on five distinct failures.
                    if not _gate_counted:
                        job.record_failure()
                    if job.auto_paused:
                        logger.warning(
                            "Cron '%s' auto-paused after %d consecutive failures",
                            job.name,
                            job.consecutive_failures,
                        )
                    logger.info(
                        "Cron '%s': duplicate failure #%d — suppressing Slack",
                        job.name,
                        job.consecutive_failures,
                    )
                    # Dashboard notify is best-effort — never mask the original
                    # exception if notification itself fails.
                    try:
                        if self.dashboard_state and not job.silent:
                            title = f"🔇 Cron: {job.name} (dup failure #{job.consecutive_failures})"
                            title, _ = redact_exfiltration_urls(title)
                            title, _ = redact_credentials(title)
                            self.dashboard_state.notify(
                                "cron",
                                title,
                                f"❌ Job failed (suppressed — same error):\n{exc_summary}",
                                meta={"job_id": job.id, "failure_hash": fh},
                            )
                    except Exception:
                        logger.debug(
                            "Dashboard notify failed in cron failure suppress path", exc_info=True
                        )
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_dedup_suppress",
                            outcome="suppressed",
                            downstream_service="none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure suppress path",
                            exc_info=True,
                        )
                    raise
                # First failure (or fresh failure after reminder window) — alert.
                # Dashboard notify is best-effort — never mask the original
                # exception if notification itself fails.
                try:
                    if self.dashboard_state and not job.silent:
                        alert_title = f"Cron: {job.name}"
                        alert_title, _ = redact_exfiltration_urls(alert_title)
                        alert_title, _ = redact_credentials(alert_title)
                        # Carry the reason, matching the suppressed-duplicate body
                        # below: without it the FIRST alert — the one the user
                        # actually reads — was the least informative of the two.
                        self.dashboard_state.notify(
                            "cron",
                            alert_title,
                            f"❌ Job failed:\n{exc_summary[:_CRON_FAILURE_DETAIL_CAP]}",
                            meta={"job_id": job.id, "failure_hash": fh},
                        )
                except Exception:
                    logger.debug(
                        "Dashboard notify failed in cron failure alert path", exc_info=True
                    )
                # Include the machine hostname so multi-gateway setups (e.g. a
                # laptop + a cloud desktop both running KiroCrew) can tell which
                # machine's session failed. This is framework-level: the ❌ DM
                # can fire before any prompt logic runs (e.g. a session-startup
                # credential failure), so the machine name must come from here,
                # not from inside the cron prompt.
                host = socket.gethostname().split(".")[0]
                # Slack PARSES entity markup here, and job.name is user-authored:
                # a job named `<!channel>` notifies the whole channel on failure.
                # Same sink and same source as the script/command alert above, so
                # it gets the same escaping rather than being left as the one
                # unescaped Slack interpolation of a cron name.
                safe_name = escape_mrkdwn(job.name)
                # Carry the reason here too. The dashboard body above and the
                # script/command DM both do, so leaving this one at "check logs"
                # made the DM the only failure surface that still withheld what
                # the caller already knows. Escaped and fence-neutralized for the
                # same reasons as the script/command alert.
                safe_reason = escape_mrkdwn(exc_summary[:_CRON_FAILURE_DETAIL_CAP]).replace(
                    "```", "'''"
                )
                if is_dup:
                    # +1: this run's failure is recorded below, after the
                    # awaited Slack attempt, so the display count must include
                    # it explicitly.
                    fail_msg = (
                        f"⏰ *Cron: {safe_name}* ❌ _Job still failing on {escape_mrkdwn(host)}"
                        f" ({job.consecutive_failures + 1} consecutive failures)"
                        f" — check logs._\n```{safe_reason}```"
                    )
                else:
                    fail_msg = (
                        f"⏰ *Cron: {safe_name}* ❌ "
                        f"_Job failed on {escape_mrkdwn(host)} — check logs._\n"
                        f"```{safe_reason}```"
                    )
                # Never trust interpolated content (job.name is user-controlled):
                # scrub exfiltration URLs + credentials before it reaches Slack,
                # mirroring the dashboard alert_title redaction above.
                fail_msg, _ = redact_exfiltration_urls(fail_msg)
                fail_msg, _ = redact_credentials(fail_msg)
                # Silent jobs still execute but suppress notifications (UI bells
                # AND Slack DMs). The failure is still logged at warning level
                # and counted toward auto-pause above — we just skip
                # user-facing noise.
                slack_failed = False  # track real delivery exceptions only
                if self.slack and not job.silent:

                    try:
                        channel = job.channel
                        if not channel and (job.created_by or self._owner_id):
                            channel = await self._open_dm_with_retry(
                                job.created_by or self._owner_id, job.name
                            )
                        if channel:
                            # fail_msg already redacted at construction above.
                            await self.slack.post_message(channel, fail_msg)
                        else:
                            logger.warning(
                                "Cron '%s': no channel resolved for error notification", job.name
                            )

                    except Exception:
                        slack_failed = True
                        logger.error(
                            "Cron job '%s': Slack failure-notification delivery failed",
                            job.name,
                            exc_info=True,
                        )
                # record_failure() is the counter's sole owner: it continues an
                # accumulation another writer (gate verdict, timeout) already
                # built up instead of restarting at 1, and it is deliberately
                # NOT gated on the alert's Slack delivery above — the run
                # failed either way, and a job whose failure alerts also fail
                # must still reach the auto-pause threshold. It runs AFTER the
                # awaited Slack attempt so a timeout cancelling this handler
                # mid-alert cannot leave the run counted here AND again by the
                # timeout handler. For the same reason it defers when the gate
                # verdict already counted THIS run.
                if not _gate_counted:
                    job.record_failure()
                if job.auto_paused:
                    logger.warning(
                        "Cron '%s' auto-paused after %d consecutive failures",
                        job.name,
                        job.consecutive_failures,
                    )
                # Advance dedup state unless Slack delivery raised. "No channel
                # available" is treated as a skip (not a failure), so dedup still
                # advances — otherwise every identical failure re-notifies the
                # dashboard, which is what dedup is supposed to prevent. Only the
                # dedup fields are gated here; the failure count was already
                # recorded above.
                if not slack_failed:
                    job.last_failure_hash = fh
                    job.last_failure_at = time.time()
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:

                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_alert",
                            outcome="suppressed" if job.silent else "alerted",
                            downstream_service=(
                                "slack" if (self.slack and not job.silent) else "none"
                            ),
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure alert path",
                            exc_info=True,
                        )
                raise
            finally:
                assert self.sessions is not None
                if _acquired:
                    self.sessions.release(session_key)
                    # Defer session reset if subagents are still running,
                    # queued behind the concurrency/stagger gate, or
                    # mid-injection — _subagent_done will reset after the last one.
                    has_pending = bool(
                        self.subagent_mgr and self.subagent_mgr.has_pending_work_for(session_key)
                    )
                    has_injecting = self._cron_injecting.get(session_key, 0) > 0
                    if has_pending or has_injecting:
                        logger.info("Cron '%s': deferring reset, subagents pending", job.name)
                        # leave the active-session registration in place so
                        # the reaper can still target the ephemeral key if the deferred
                        # reset hangs. _subagent_done will clear it after the real reset.
                    else:
                        await self.sessions.reset(session_key)
                        # reset done → reaper no longer needs this key.
                        if self.cron_svc is not None:
                            self.cron_svc.clear_active_session_key(job.id)
                # Restore per-job env vars (single-agent path) — now handled via extra_env passthrough

        self.cron_svc = await CronService.create(base_dir=data_home(), on_job=_cron_callback)
        if self.dashboard_state:
            self.cron_svc.set_refresh_callback(self.dashboard_state.push_refresh)
        if self._no_crons:
            logger.info("Cron scheduler disabled (--no-crons)")
        else:
            # CronService.create has loaded durable jobs but has not armed a
            # timer yet. Remove jobs owned by disabled or execution-denied apps
            # at this boundary; if cleanup cannot complete, leave the entire
            # scheduler stopped rather than risk firing a denied command.
            from kiro_crew.apps.bridges import reconcile_app_crons_for_execution

            try:
                await reconcile_app_crons_for_execution(self.cron_svc)
            except Exception:
                logger.exception(
                    "App cron execution reconciliation failed; refusing to arm "
                    "the cron scheduler"
                )
                return
            await self.cron_svc.start()
            if self.sessions:
                self.cron_svc.start_reaper(self.sessions)
            else:
                logger.warning("Cron reaper not started: sessions not available")

    async def _init_heartbeat(self) -> None:
        """Initialize and start the heartbeat service."""
        memory = self.ctx_builder.memory if self.ctx_builder else MemoryStore()

        # Heartbeat-scoped hooks: drops the user's ``auto_approve_tools`` so
        # ``HEARTBEAT_SAFE_TOOLS`` is the sole approval authority for any
        # tool call in a heartbeat session.  REBUILT per run (below) from the
        # live primary manager: denied-command opt-out state is mutable at
        # runtime (Settings > Security hot-reloads ``ctx_builder.hooks``), so a
        # once-at-init snapshot would let a heartbeat session keep enforcing a
        # just-disabled rule — or skip a just-added one — until restart.
        assert self.ctx_builder is not None

        async def _heartbeat_task(task_text: str, deliver: str) -> str | None:
            assert self.sessions is not None
            assert self.ctx_builder is not None
            session_key = HEARTBEAT_KEY
            # Re-derive the heartbeat-scoped hooks from the CURRENT primary
            # manager each cycle so live denied-command changes take effect
            # without a gateway restart (cross-surface consistency).
            heartbeat_hooks = _build_heartbeat_hooks(self.ctx_builder.hooks)
            _acquired = False
            try:
                # Use the dedicated ``kirocrew-heartbeat`` agent — minimal
                # MCP surface (kirocrew-core only on public installs) so cycle
                # cold-starts stay cheap.  Tool calls are still gated at
                # runtime by ``_heartbeat_approval`` against
                # ``HEARTBEAT_SAFE_TOOLS``.
                client, is_new, _resumed = await self.sessions.get_or_create(
                    session_key,
                    agent="kirocrew-heartbeat",
                )
                _acquired = True

                # Prepend an unmissable HEARTBEAT_KEEP reminder to every task
                # text before message build.  This survives context
                # compaction and webhook-restored sessions where skill /
                # system-prompt copies of the same instruction can drift out
                # of effective context.
                injected = _HEARTBEAT_KEEP_INJECTION + task_text
                # Off-loop: build_message embeds the episodic query.
                full_message, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message, injected, is_new
                )

                # A heartbeat turn runs unattended. Bound it with a hard deadline
                # (mirrors cron's _execute_with_timeout) as defense in depth so
                # any unexpected hang in stream_and_collect cannot freeze the
                # whole heartbeat subsystem. ``_heartbeat_approval`` already
                # rejects non-allowlisted tools immediately (no human-approval
                # wait), so the timeout is the second line of defense.
                #
                # ``hooks=heartbeat_hooks`` (NOT the interactive user hooks):
                # the user's ``auto_approve_tools`` MUST NOT widen the heartbeat
                # allowlist — ``llm_helpers._resolve_permission`` consults
                # ``hooks.on_tool_call()`` BEFORE ``on_tool_approval``.
                #
                # Clock started outside wait_for so BOTH the success path and the
                # TimeoutError branch below can report the real elapsed time.
                _turn_t0 = time.monotonic()
                result_text = await asyncio.wait_for(
                    stream_and_collect(
                        client,
                        full_message,
                        approval_policy=ToolApprovalPolicy.HOOK_BASED,
                        hooks=heartbeat_hooks,
                        on_tool_approval=self._heartbeat_approval,
                    ),
                    timeout=HEARTBEAT_TASK_TIMEOUT_SECS,
                )

                if not result_text:
                    result_text = "_No response._"

                # ── Per-turn usage row: attribute heartbeat spend. ──
                await _persist_turn_row(
                    client,
                    session_key,
                    provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                    surface="heartbeat",
                    agent_fallback=lambda: "kirocrew-heartbeat",
                    t0=_turn_t0,
                )
            except asyncio.TimeoutError:
                # Tear down the in-flight turn so the underlying claude-agent-acp
                # process/turn doesn't linger holding the heartbeat session.
                # Per-task reset is safe here because asyncio.wait_for has
                # already cancelled the in-flight stream_and_collect, so any
                # concurrent heartbeat task using the same key was already
                # blocked on the per-key semaphore (held until our finally
                # releases) — they pick up the freshly-recreated session.
                logger.warning(
                    "Heartbeat task timed out after %ds, resetting session: %s",
                    HEARTBEAT_TASK_TIMEOUT_SECS,
                    task_text[:80],
                )
                # ── Timeout spend is REAL spend (issue #874 follow-up). ──
                # Before this, a timed-out heartbeat wrote no row at all, so
                # every cancelled turn silently dropped whatever it had already
                # cost. Record it here, BEFORE the session reset below tears the
                # client down and takes its last-turn usage with it.
                #
                # No new schema field: the record has never carried a
                # success/failure outcome for ANY surface, so a timeout row is
                # no less honest than any other row. The duration recorded is
                # the real elapsed time, which for a timeout is ~the ceiling.
                await _persist_turn_row(
                    client,
                    session_key,
                    provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                    surface="heartbeat",
                    agent_fallback=lambda: "kirocrew-heartbeat",
                    t0=_turn_t0,
                )
                try:
                    await self.sessions.reset(session_key)
                except Exception:
                    logger.warning("Heartbeat: session reset after timeout failed", exc_info=True)
                # Produce a graceful incomplete result rather than crashing the loop.
                result_text = (
                    f"_Heartbeat task timed out after {HEARTBEAT_TASK_TIMEOUT_SECS}s "
                    "and was cancelled._"
                )
            except Exception:
                logger.exception("Heartbeat task failed: %s", task_text[:80])
                raise
            finally:
                if _acquired:
                    # Release the per-session semaphore so the next task in
                    # this cycle (asyncio.gather'd) can acquire the SAME
                    # warm session.  Cycle-end teardown is handled by
                    # ``_recycle_heartbeat`` (called once after gather
                    # completes) — see ``HeartbeatService._process_heartbeat_file``.
                    self.sessions.release(session_key)

            result_safe, _ = redact_exfiltration_urls(result_text)
            result_safe, _ = redact_credentials(result_safe)
            display_text = strip_keep_sentinel(result_safe)
            # Only notify when task is complete — suppress delivery for
            # incomplete tasks (HEARTBEAT_KEEP) to avoid spamming every cycle.
            if is_keep_response(result_safe):
                logger.info("Heartbeat task incomplete, suppressing delivery: %s", task_text[:80])
            else:
                task_safe, _ = redact_exfiltration_urls(task_text[:100])
                task_safe, _ = redact_credentials(task_safe)
                await self._deliver_result(
                    "💓 Heartbeat",
                    task_safe,
                    display_text,
                    deliver,
                )
            return result_safe

        async def _on_cycle_end() -> None:
            """Recycle the heartbeat session ONCE per cycle, not per task.

            Multi-task heartbeat cycles run concurrently via
            ``asyncio.gather`` and share ``HEARTBEAT_KEY``.  A per-task
            ``reset()`` would tear down the session under sibling tasks
            still in flight (per code review).
            ``recycle_heartbeat`` is unconditional: heartbeat promises
            "fresh context each cycle", and each entry is re-read from
            HEARTBEAT.md every cycle, so carrying a transcript forward only
            costs input tokens. Nobody waits on a heartbeat tick, so the
            per-cycle MCP cold-start is unobserved.
            """
            assert self.sessions is not None
            try:
                await self.sessions.recycle_heartbeat()
            except Exception:
                logger.warning("Heartbeat: cycle-end recycle failed", exc_info=True)

        self.heartbeat_svc = HeartbeatService(
            memory=memory,
            on_task=_heartbeat_task,
            consolidator=self.consolidator,
            on_cycle_end=_on_cycle_end,
        )
        await self.heartbeat_svc.start()

    async def _fire_slack_nudge(self, loop: NudgeLoop) -> bool:
        """Drive one unattended nudge turn in a Slack thread session.

        Mirrors the subagent-completion Slack injection: acquire the session,
        run the turn with auto-approval, post the reply into the originating
        thread, persist for dashboard replay. Returns True when the turn ran;
        False on skip (busy/unroutable/error) — the AutoNudge service re-arms
        with backoff on False.
        """
        key = loop.slot_key
        if self.sessions is None or self.slack is None:
            return False
        if self.sessions.is_busy(key):
            logger.info("AutoNudge skip: slack session %s busy (loop %s)", key, loop.id)
            return False
        channel = self.sessions.get_channel(key)
        thread_ts = self.sessions.get_thread(key)
        if not thread_ts and key.startswith("slack:"):
            # Canonical keys embed the thread root ts.
            thread_ts = key.split(":", 1)[1]
        if not channel:
            logger.warning(
                "AutoNudge: slack session %s unroutable — removing loop %s", key, loop.id
            )
            if self.autonudge_svc:
                await self.autonudge_svc.remove(loop.id)
            return False
        # Off-loop: render_nudge_message resolves variables, which does a config
        # load plus a store read and JSON parse; this fires on the event loop.
        msg_body = await asyncio.to_thread(
            render_nudge_message, loop.message, loop.stop_sentinel_path, loop.agent
        )
        tagged = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{msg_body}"
        # Fail closed: an unattended turn MUST run under the HookManager
        # PreToolUse governance gate (mirrors cron's default approval path).
        # Without ctx_builder there are no hooks to enforce the gate — skip.
        if self.ctx_builder is None or self.ctx_builder.hooks is None:
            logger.warning(
                "AutoNudge: no hook manager available — refusing unattended "
                "slack nudge turn for %s (loop %s)",
                key,
                loop.id,
            )
            return False
        response: str | None = None
        _acquired = False
        try:
            client, is_new, _resumed = await self.sessions.get_or_create(key)
            _acquired = True
            _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
            full_msg, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                tagged,
                is_new,
                key,
                provider_type=_provider,
                # ``tagged`` wraps a body render_nudge_message already expanded, so
                # triggers match the loop's authored instruction instead.
                trigger_text=loop.message,
                # The loop's armed crew, so the system prompt resolves the same
                # crew's variables the body was rendered with.
                crew=loop.agent or None,
            )
            # Clock started outside wait_for so BOTH the success path and the
            # TimeoutError branch below can report the real elapsed time. acp
            # never assigns TurnUsage.duration_ms, so the row needs this.
            _turn_t0 = time.monotonic()
            response = await asyncio.wait_for(
                stream_and_collect(
                    client,
                    full_msg,
                    retry_transient=False,
                    # Same governance contract as unattended cron turns: the
                    # HookManager PreToolUse gate decides tool approvals, and
                    # anything it can't decide goes to the deny-fast
                    # background-approval window (source "autonudge").
                    approval_policy=ToolApprovalPolicy.HOOK_BASED,
                    hooks=self.ctx_builder.hooks,
                    on_tool_approval=self._interactive_approval("autonudge", nudge_key=key),
                ),
                timeout=_NUDGE_TURN_TIMEOUT,
            )

            # ── Per-turn usage row: attribute monitor spend. ──
            await _persist_turn_row(
                client,
                key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="monitor",
                agent_fallback=lambda: _get_agent_for_session(key),
                t0=_turn_t0,
            )
        except asyncio.TimeoutError:
            # ── Timeout spend is REAL spend (issue #874 follow-up). ──
            # A timed-out nudge turn previously fell through to the generic
            # handler below and wrote no row at all, silently dropping whatever
            # the cancelled turn had already cost. Record it, then bail as
            # before. Runs before the `finally` cancels/releases the session.
            #
            # No new schema field: the record has never carried a
            # success/failure outcome for ANY surface, so a timeout row is no
            # less honest than any other row.
            logger.warning(
                "AutoNudge: slack nudge turn timed out after %ss for %s (loop %s)",
                _NUDGE_TURN_TIMEOUT,
                key,
                loop.id,
            )
            await _persist_turn_row(
                client,
                key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="monitor",
                agent_fallback=lambda: _get_agent_for_session(key),
                t0=_turn_t0,
            )
            return False
        except Exception:
            logger.exception("AutoNudge: slack nudge turn failed for %s (loop %s)", key, loop.id)
            return False
        finally:
            if _acquired:
                try:
                    await self.sessions.cancel_current(key)
                except Exception:
                    logger.debug("AutoNudge: cancel_current failed for %s", key, exc_info=True)
                try:
                    self.sessions.release(key)
                except Exception:
                    logger.exception("AutoNudge: failed to release session %s", key)
        # Post the response into the originating thread (best-effort — the
        # turn itself already ran, so failures here don't fail the cycle).
        try:
            if response:
                for part in render_for_slack(response):
                    await self.slack.post_message(channel, part, thread_ts)
        except Exception:
            logger.exception("AutoNudge: slack posting failed for %s (turn ran)", key)
        # Persist for dashboard replay (mirrors subagent Slack injection).
        if self.conv_log and not (is_thread_temporary(key) or is_thread_incognito(key)):
            try:
                safe_nudge, _ = redact_exfiltration_urls(tagged)
                safe_nudge, _ = redact_credentials(safe_nudge)
                safe_response, _ = redact_exfiltration_urls(response or "")
                safe_response, _ = redact_credentials(safe_response)
                await save_conversation_turn_off_loop(
                    self.conv_log,
                    key,
                    safe_nudge,
                    safe_response,
                    source_thread=key,
                    source_user="autonudge",
                    agent=_get_agent_for_session(key),
                )
            except Exception:
                logger.warning("AutoNudge: failed to persist nudge turn for %s", key, exc_info=True)
        return True

    async def _fire_discord_nudge(self, loop: NudgeLoop) -> bool:
        """Drive one unattended nudge turn in a Discord DM session.

        Synthesizes an ``InboundMessage`` and routes it through the Discord
        dispatcher — the exact path a real DM takes — so busy/steer/queue
        handling, rendering, chunking, and persistence behave like a user
        turn. ``interpret_commands=False`` keeps the nudge from being parsed
        as a ``!command``.
        """
        key = loop.slot_key
        transports = getattr(self.dashboard_state, "channel_transports", None) or {}
        transport = transports.get("discord")
        dispatcher = transport.dispatcher if transport is not None else None
        if transport is None or dispatcher is None:
            logger.info("AutoNudge skip: discord transport not running (loop %s)", loop.id)
            return False
        # Key shape: discord:{agent}:direct:{user_id}[:genN]
        parts = key.split(":")
        if len(parts) < 4 or parts[2] != "direct":
            logger.warning("AutoNudge: unsupported discord key %s — removing loop %s", key, loop.id)
            if self.autonudge_svc:
                await self.autonudge_svc.remove(loop.id)
            return False
        user_id = parts[3]
        # Defense-in-depth: re-check the inbound allowlist at fire time (the
        # create endpoint enforces it too, but the allowlist can shrink after
        # a loop was created). Synthetic injection bypasses transport.receive,
        # so authorization is this caller's responsibility — mirrors
        # on_interaction's _authorized re-check. Uses the dispatcher's public
        # injection surface; a missing method raises loudly instead of
        # silently retiring the loop.
        if not dispatcher.is_authorized(user_id):
            logger.warning(
                "AutoNudge: discord user %s not authorized — removing loop %s",
                user_id,
                loop.id,
            )
            if self.autonudge_svc:
                await self.autonudge_svc.remove(loop.id)
            return False
        # Generation guard: the dispatcher derives the CURRENT key for this
        # user (dm_scope + `!new` generation). If it no longer matches the
        # loop's stored key, the monitored conversation is gone — a synthetic
        # turn would run in a fresh session with none of the loop's context,
        # and autonudge_stop from that new session could never find this
        # loop. Retire it instead of firing into the wrong generation.
        try:
            current_key = dispatcher.current_session_key(user_id)
        except Exception:
            current_key = key
        if current_key != key:
            logger.info(
                "AutoNudge: discord session rotated (%s -> %s) — removing loop %s",
                key,
                current_key,
                loop.id,
            )
            if self.autonudge_svc:
                await self.autonudge_svc.remove(loop.id)
            return False
        sessions = getattr(dispatcher, "sessions", None)
        if sessions is not None and sessions.is_busy(key):
            logger.info("AutoNudge skip: discord session %s busy (loop %s)", key, loop.id)
            return False
        # Off-loop: render_nudge_message resolves variables, which does a config
        # load plus a store read and JSON parse; this fires on the event loop.
        msg_body = await asyncio.to_thread(
            render_nudge_message, loop.message, loop.stop_sentinel_path, loop.agent
        )
        tagged = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{msg_body}"
        try:
            conversation_id = await transport.resolve_conversation(user_id)
            synthetic = InboundMessage(
                channel_type="discord",
                user_id=user_id,
                conversation_id=conversation_id,
                text=tagged,
                # ``tagged`` wraps a body whose {{name}} tokens render_nudge_message
                # already resolved using the loop's armed crew. Skill selection must
                # read the loop's own instruction instead, or a variable's VALUE
                # could pull in a skill the author never referenced.
                trigger_text=loop.message,
            )
            await asyncio.wait_for(
                dispatcher.handle_message(synthetic, interpret_commands=False),
                timeout=_NUDGE_TURN_TIMEOUT,
            )
            return True
        except Exception:
            logger.exception("AutoNudge: discord nudge failed for %s (loop %s)", key, loop.id)
            return False

    async def _fire_dashboard_nudge(self, loop: NudgeLoop) -> bool:
        """Drive one nudge turn in a dashboard chat slot.

        Sibling of :meth:`_fire_slack_nudge` / :meth:`_fire_discord_nudge`; a
        named method rather than an inline branch so the slot-resolution
        contract below is directly testable.

        Returns True if the nudge was dispatched, False if skipped (dashboard
        not ready, session genuinely gone, or a turn still active). The service
        only counts dispatched cycles toward ``max_cycles``.
        """
        # Guard (not assert): stripped under -O; also _init_autonudge() can
        # run before _init_dashboard(), and _init_dashboard is skipped
        # entirely in --no-dashboard mode. Mirrors _observer's guard.
        if self.dashboard_state is None:
            logger.warning("AutoNudge: dashboard not ready — skipping fire for loop %s", loop.id)
            return False
        # Slot resolution mirrors the cron→origin delivery contract in
        # dashboard/handlers/messaging.py: get_slot() is the hot path, and a
        # miss falls back to restoring the session from its persisted history
        # rather than assuming it is gone. A miss is NOT evidence of a dead
        # session — the in-memory registry is empty for any tab the user has
        # navigated away from, and it is empty for EVERY slot immediately
        # after a gateway restart (AutoNudgeService.start() re-arms timers
        # before the dashboard has restored its slots). Removing the loop here
        # deleted a live babysit loop on nothing more than a cold cache, which
        # silently abandoned the PR it was watching.
        #
        # Rehydration deliberately does NOT resurrect a session the user
        # dismissed with ✕ — that is the documented "respect the close" rule.
        # It is now enforced where the user acts: api_chat_slot_delete removes
        # this loop as part of the close, so a loop that is still armed was
        # never user-dismissed. Only a genuinely unreachable session retires
        # the loop.
        #
        # FIX 3: hence adopt_closed=True. ``closed`` in the metadata is written
        # by TWO producers, and only one of them is the user: idle archival
        # (POST /api/chat/slots/cleanup, default 3 days) also marks a slot
        # closed. An unattended worker is idle by nature between cycles, so it
        # was archived, became unreachable to this exact call, and the loop was
        # REMOVED below — terminally, with no way back. Adopting the closed
        # session is what makes archival survivable; the companion change in
        # api_chat_slots_cleanup exempts loop-owning slots so it should not
        # happen in the first place, and this is the backstop for a slot
        # archived before that landed (or by any other automatic closer).
        slot = self.dashboard_state.get_slot(loop.slot_key)
        if slot is None:
            # Rehydration reads the session's persisted transcript and replays
            # its window. Real sessions reach tens of MB, so the reads must not
            # run on the event loop: the gateway serves every request, turn and
            # the stall-watchdog heartbeat on one thread, and a fire here is a
            # timer callback. The async form hoists ONLY the reads to a worker
            # thread and builds the slot back on the loop, because slot
            # construction broadcasts through asyncio primitives that are not
            # thread-safe.
            slot = await rehydrate_slot_from_history_async(
                self.dashboard_state, loop.slot_key, adopt_closed=True
            )
            if slot is None:
                logger.warning(
                    "AutoNudge: session %s unreachable (no history or deleted) "
                    "— removing loop %s",
                    loop.slot_key,
                    loop.id,
                )
                await self.autonudge_svc.remove(loop.id)  # type: ignore[union-attr]
                return False
            logger.info(
                "AutoNudge: rehydrated session %s from history for loop %s",
                loop.slot_key,
                loop.id,
            )
        # Off-loop: render_nudge_message resolves variables, which does a config
        # load plus a store read and JSON parse; this fires on the event loop.
        msg = await asyncio.to_thread(
            render_nudge_message, loop.message, loop.stop_sentinel_path, loop.agent
        )
        tagged = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{msg}"
        from kiro_crew.dashboard.chat import (
            _run_chat,  # circular import: gateway -> dashboard.chat -> gateway (chat dispatch references GatewayOrchestrator)
        )

        if slot.running or slot._in_stage_execution:
            # Turn still active, OR a multi-stage plan is mid-flight (slot.task is
            # None between stages, so slot.running alone misses that window and the
            # nudge would start a concurrent turn that clobbers the plan) — drop this
            # nudge. Next idle-timer tick will schedule again once the turn/plan ends.
            # Queueing would stack identical 3KB+ nudges and blow up the context
            # window. Returning False keeps cycle_count accurate (only delivered
            # nudges count toward max_cycles).
            logger.info(
                "AutoNudge skip: slot %s is running (loop %s cycle %d)",
                slot.key,
                loop.id,
                loop.cycle_count,
            )
            return False
        # Show nudge as a distinct "nudge" role message in the slot history.
        # The structured meta lets the dashboard render a compact cycle chip
        # instead of echoing the whole instruction payload as a chat bubble.
        # The tag stays in ``content`` because that is what the model reads,
        # and the body is deliberately NOT duplicated into meta — the client
        # derives it from content, so a multi-KB payload is stored and
        # broadcast once rather than twice.
        slot.append(
            "nudge",
            tagged,
            "msg msg-nudge",
            meta={
                "nudge": {
                    "cycle": loop.cycle_count + 1,
                    "loop_id": loop.id,
                }
            },
        )
        # FIX 2: an unattended app-owned nudge turn runs under the background
        # concurrency cap. This is the fleet's hot path — N armed loops fire
        # independently and would otherwise put N turns on the runtime at once.
        # An attended slot (any user session with a monitor loop) is passed
        # straight through, so babysit loops on human sessions are unaffected.
        task = spawn_guarded_turn(
            self.dashboard_state,
            slot,
            self.dashboard_state.run_background_turn(
                slot, _run_chat(self.dashboard_state, slot, tagged, trigger_text=loop.message)
            ),
        )
        # Mirror dashboard /api/chat/send path so slot.running == True and sidebar
        # shows the "turn active" three-dots indicator immediately.
        slot.task = task
        self._session_tasks[slot.key] = task
        self.dashboard_state.push_slots_update()
        return True

    async def _init_autonudge(self) -> None:
        """Initialize and start the auto-nudge service (feature-flagged)."""
        if not autonudge_enabled():
            logger.info("AutoNudge disabled via feature flag")
            return

        async def _fire(loop: NudgeLoop) -> bool:
            """Inject nudge message into the bound session.

            Routes by binding-key namespace: ``slack:``/``discord:`` keys run
            an unattended turn in the channel session; bare keys are dashboard
            chat slots (original path).

            Returns True if the nudge was actually dispatched, False if skipped
            (slot missing, dashboard not ready, or turn still active). The
            service uses this to avoid counting skipped cycles toward
            max_cycles.
            """
            if is_channel_key(loop.slot_key):
                if loop.slot_key.startswith("slack:"):
                    return await self._fire_slack_nudge(loop)
                if loop.slot_key.startswith("discord:"):
                    return await self._fire_discord_nudge(loop)
                logger.warning(
                    "AutoNudge: unsupported channel key %s — removing loop %s",
                    loop.slot_key,
                    loop.id,
                )
                await self.autonudge_svc.remove(loop.id)  # type: ignore[union-attr]
                return False
            return await self._fire_dashboard_nudge(loop)

        def _observer(event: str, loop: NudgeLoop | None) -> None:
            if event == "expired" and loop is not None:
                self._notify_nudge_expired(loop)
            if self.dashboard_state and loop is not None:
                self.dashboard_state.broadcast_ws(
                    "autonudge_state",
                    {
                        "event": event,
                        "slot": loop.slot_key,
                        "loop": {
                            "id": loop.id,
                            "slot_key": loop.slot_key,
                            "message": loop.message,
                            "idle_secs": loop.idle_secs,
                            "max_cycles": loop.max_cycles,
                            "max_runtime_secs": loop.max_runtime_secs,
                            "cycle_count": loop.cycle_count,
                            "active": loop.active,
                            "last_fire_ts": loop.last_fire_ts,
                        },
                    },
                )

        self.autonudge_svc = AutoNudgeService(base_dir=data_home(), on_fire=_fire)
        self.autonudge_svc.subscribe(_observer)
        await self.autonudge_svc.start()

    def _notify_nudge_expired(self, loop: NudgeLoop) -> None:
        """Notify the user that a monitoring loop stopped at a terminal bound.

        Reaching ``max_cycles`` or spending ``max_runtime_secs`` is a runaway
        backstop, not a finish line: the loop stopped with its goal possibly
        unmet. Without this the only signals were a log line and an
        ``active=False`` state change that looks identical to a manual Stop, so
        a loop that ran out of cycles was indistinguishable from the agent
        stopping on its own — the most confusing failure mode of the babysit
        feature. The wording distinguishes WHICH bound fired via the same
        ``runtime_budget_exceeded`` predicate ``_timer`` enforces with; when
        both are exhausted the cycle cap wins, matching the enforcement order.
        A loop stopped because it could not obtain tool approval is a third
        case naming a different remedy — restore the authorization, do not
        raise a bound — so reporting it as a cap would send the operator to
        change a setting that was never the problem. It is read from the
        persisted reason and ranked below the two bounds, again matching
        ``_timer``, which tests it last.

        Best-effort by construction: ``notify()`` never raises (it swallows
        validation errors and logs), and the whole call is wrapped anyway
        because this runs inside ``_emit``'s observer loop, where an exception
        would be caught and logged but would also skip the WS broadcast that
        follows it.
        """
        if not self.dashboard_state:
            return
        try:
            key = loop.slot_key
            # Channel-bound loops get NO synthesized meta: _notif_meta's generic
            # ``chan:ts`` split would read the NAMESPACE as the channel id,
            # producing a dead link (and a Slack URL for a Discord loop).
            # Dashboard loops bind on the BARE slot key, so re-qualify those to
            # get a working jump-to-source slot link.
            meta = None if is_channel_key(key) else self._notif_meta(f"dashboard:{key}")
            capped_out = loop.max_cycles and loop.cycle_count >= loop.max_cycles
            if not capped_out and runtime_budget_exceeded(loop):
                title = "Monitoring loop spent its time budget"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because "
                    f"its {loop.max_runtime_secs}s wall-clock budget ran out "
                    "without it reporting done, so its goal may still be "
                    "unmet. Restart it from the goal popover, or ask the agent "
                    "to raise the budget (monitor_update)."
                )
            elif not capped_out and loop.stopped_reason == APPROVAL_STALL_REASON:
                title = "Monitoring loop stopped — it could not get tool approval"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because a "
                    "tool it needed went unanswered at the approval prompt, so "
                    "further cycles would wake, be declined and accomplish "
                    "nothing. A prompt you were merely away for counts too: if "
                    "approval is available now, just restart the loop from the "
                    "goal popover; otherwise re-enable auto-approve first. For "
                    "runs meant to go unattended overnight, Settings → "
                    "agent.yolo_duration has an 'until_shutdown' option that "
                    "has no timed expiry."
                )
            else:
                title = "Monitoring loop hit its cycle cap"
                body = (
                    f"The loop stopped after {loop.cycle_count} of "
                    f"{loop.max_cycles} cycles without reporting done, so its "
                    "goal may still be unmet. Reopen the goal popover to raise "
                    "the cap or restart it."
                )
            self.dashboard_state.notify("agent", title, body, meta=meta)
        except Exception:
            logger.debug("AutoNudge expiry notification failed", exc_info=True)

    @staticmethod
    def _defer_queued_delivery(
        slot: Any, announce: str, info: SubagentInfo, *, flush_only: bool
    ) -> None:
        """Owe a queued completion's delivery tombstones to the queue drain.

        The retention window for ``result.txt`` (``agent.subagent_result_ttl_secs``)
        exists so the parent can read the full transcript AFTER the completion
        event reaches it. Writing the ``delivered`` tombstone when the announce is
        merely QUEUED starts that clock while the event is still waiting for a
        turn, so a long-running turn ahead of it lets the reaper prune every file
        the queued announce points at (issue #4839).

        So the ids are handed to the slot keyed on the announce ITSELF,
        ``_delivery_queued`` tells the run loop to skip its own ``mark_delivered``,
        and the drain (``chat_runner._start_next_queued_turn``) settles them once a
        turn has consumed that announce -- including a retry, because a failure
        before the model consumed the prompt re-queues the same text under a newly
        minted queue id, which a debt keyed on the original id could never match. A
        wave digest carries its held members' ids too: ``_digest_settle_ids`` is
        transferred (not copied), so the run loop's ``_settle_digest_holds`` becomes
        a no-op rather than a second writer.

        Best-effort in one direction only: if the slot cannot take the ids (a
        stubbed slot in tests), nothing is transferred and the previous
        immediate-tombstone behaviour stands — better a short window than a
        folder no one ever tombstones.
        """
        # A flush-only record is synthetic (no run, no folder of its own). Only a
        # COMPLETED member owes a delivered mark: ``info.outcome`` is the codebase's
        # canonical three-way classification precisely because the ``error``-
        # nullability idiom reports a user-stopped agent as completed, and a
        # stopped or failed run already carries its own tombstone whose 7-day
        # post-mortem window a "delivered" write would shorten to the result TTL.
        owed: list[str] = [] if (flush_only or info.outcome != "completed") else [info.id]
        held = getattr(info, "_digest_settle_ids", None)
        if isinstance(held, list):
            owed.extend(str(h) for h in held)
        try:
            slot.note_pending_subagent_delivery(announce, owed)
        except Exception:
            logger.debug(
                "Subagent %s: could not defer queued delivery marks", info.id, exc_info=True
            )
            return
        info._delivery_queued = True
        if isinstance(held, list):
            info._digest_settle_ids = []

    @staticmethod
    def _notif_meta(parent_key: str | None) -> dict[str, str] | None:
        """Build notification meta with slot or slack_link for jump-to-source."""
        if not parent_key:
            return None
        # A jump-to-source slot beats a channel deep link whenever a tab is
        # open, including for a channel-born conversation whose key is the
        # channel's own.
        slot = dashboard_slot_key(parent_key)
        if slot:
            return {"slot": slot}
        if ":" in parent_key and not parent_key.startswith(("cron:", "subagent:", "hook:")):
            chan, ts = parent_key.split(":", 1)
            return {
                "slack_link": f"https://amzn-aws.slack.com/archives/{chan}/p{ts.replace('.', '')}"
            }
        return None

    async def _persist_slot_title(self, slot: "_ChatSlot") -> None:
        """Persist a dashboard slot's title so it survives a gateway restart.

        Best-effort and off the event loop (``set_title`` does a synchronous
        read + rewrite): a slow or failed write must never break heartbeat
        delivery. Mirrors the auto-research worker-slot titling path.
        """
        conv_log = getattr(self.dashboard_state, "conversation_log", None)
        if conv_log is None:
            return
        # Lazy import avoids a circular dependency (dashboard.chat_utils → gateway).
        from kiro_crew.dashboard.chat_utils import slot_history_key

        try:
            await asyncio.to_thread(conv_log.set_title, slot_history_key(slot), slot.title)
        except Exception:
            logger.warning(
                "Heartbeat: failed to persist slot title for %s", slot.key, exc_info=True
            )

    async def _deliver_result(
        self,
        title: str,
        task_summary: str,
        result_text: str,
        deliver: str,
    ) -> None:
        """Route a background result to the right surface.

        ``deliver`` values:
        - ``prompt:dashboard:<slot>`` → send as user prompt to dashboard slot (triggers agent turn)
        - ``dashboard:<slot>`` → inject into existing dashboard chat slot
        - ``dashboard``        → create new dashboard chat slot
        - ``slack:<chan>:<ts>`` → reply to Slack thread
        - ``slack``            → new Slack DM only (no dashboard notification)
        - ``silent``           → log only
        - ``""`` (empty)       → routed per ``heartbeat.default_deliver`` config:
          ``slack`` (default) = Slack DM (if available) + dashboard notification;
          ``dashboard`` = dashboard slot + bell only (no Slack)
        """
        result_text, _ = redact_exfiltration_urls(result_text)
        result_text, _ = redact_credentials(result_text)
        task_summary, _ = redact_exfiltration_urls(task_summary)
        task_summary, _ = redact_credentials(task_summary)
        title, _ = redact_exfiltration_urls(title)
        title, _ = redact_credentials(title)

        # Tagless heartbeat completions route per the configured default
        # (heartbeat.default_deliver, default "slack" = backward compatible).
        # "dashboard" -> dashboard slot + bell only (no Slack); "slack" -> leave
        # empty so the default Slack-DM + dashboard branch below runs. An explicit
        # per-task <!-- deliver:... --> tag makes deliver non-empty and bypasses this.
        if not deliver:
            try:
                if KiroCrewConfig.load().heartbeat.default_deliver == "dashboard":
                    deliver = "dashboard"
            except Exception:
                logger.debug("heartbeat default_deliver lookup failed", exc_info=True)
        body = f"{task_summary}\n\n{result_text}"

        # ── silent: log only ──
        if deliver == "silent":
            logger.info("%s (silent): %s", title, task_summary)
            return

        # ── prompt:dashboard:<slot> → send as user prompt to slot (triggers agent turn) ──
        if deliver.startswith("prompt:dashboard:"):
            slot_name = deliver.removeprefix("prompt:dashboard:")
            if not slot_name:
                logger.debug("Heartbeat prompt:dashboard: missing slot name, skipping")
                return
            if self.dashboard_state:
                slot = self.dashboard_state.resolve_slot(slot_name)
                if slot:
                    # Truncate the variable-size *content* separately so the title/prefix
                    # can never be sliced at a multi-byte boundary. errors='ignore'
                    # (not 'replace') keeps the final byte size <= limit — U+FFFD
                    # would be 3 bytes and push past the cap.
                    prefix = f"{title}\n\n"
                    prefix_bytes = len(prefix.encode("utf-8"))
                    content_budget = max(0, MAX_PROMPT_BYTES - prefix_bytes)
                    content_bytes = result_text.encode("utf-8")
                    if len(content_bytes) > content_budget:
                        truncated = content_bytes[:content_budget].decode("utf-8", errors="ignore")
                        logger.warning(
                            "Heartbeat prompt truncated to %d bytes for slot %s",
                            MAX_PROMPT_BYTES,
                            slot_name,
                        )
                        prompt = prefix + truncated
                    else:
                        prompt = prefix + result_text
                    # Lazy import avoids circular dependency (chat → gateway)
                    from kiro_crew.dashboard.chat import _run_chat

                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_prompt_deliver",
                        outcome="approved",
                        source="gateway",
                        resources=f"requested={slot_name},resolved={slot.key}",
                    )
                    ran = slot.enqueue_or_run_prompt(prompt, _run_chat, self.dashboard_state)
                    if ran:
                        # Only push UI updates when the prompt actually started —
                        # queued prompts produce no visible change until dequeued.
                        self.dashboard_state.push_slots_update()
                        self.dashboard_state.notify(
                            "heartbeat", title, body, meta={"slot": slot.key}
                        )
                    else:
                        logger.info(
                            "Heartbeat prompt queued for busy slot %s (queue depth=%d)",
                            slot.key,
                            slot.queue_depth,
                        )
                else:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_prompt_deliver",
                        outcome="not_found",
                        source="gateway",
                        resources=f"requested={slot_name}",
                    )
                    logger.warning("Heartbeat prompt target slot %s not found", slot_name)
            else:
                logger.debug("prompt:dashboard:%s ignored — no dashboard_state", slot_name)
            return

        # ── dashboard:<slot> → inject into specific slot ──
        if deliver.startswith("dashboard:"):
            slot_name = deliver.removeprefix("dashboard:")
            if self.dashboard_state:
                slot = self.dashboard_state.resolve_slot(slot_name)
                if slot:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_inject_deliver",
                        outcome="approved",
                        source="gateway",
                        resources=f"requested={slot_name},resolved={slot.key}",
                    )
                    slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
                    self.dashboard_state.push_slots_update()
                    self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
                else:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_inject_deliver",
                        outcome="not_found",
                        source="gateway",
                        resources=f"requested={slot_name}",
                    )
                    logger.warning("Heartbeat deliver target slot %s not found", slot_name)
            else:
                logger.debug("dashboard:%s ignored — no dashboard_state", slot_name)
            return

        # ── dashboard (no slot) → new slot ──
        if deliver == "dashboard":
            if self.dashboard_state:
                slot = self.dashboard_state.get_or_create_slot()
                # Heartbeat delivery appends only an assistant message, so the
                # interactive LLM auto-titler never fires for this slot
                # (_maybe_auto_title gates on user_count >= 1) and it would be
                # stuck on the "New Session…" placeholder forever. Seed a
                # meaningful title from the (already-redacted) task summary,
                # mirroring the cron/auto-research slot pattern: set the title,
                # lock _titled so display_title returns it, and persist it so it
                # survives a gateway restart (best-effort, off the event loop).
                seed = " ".join(task_summary.split())
                slot.title = f"💓 {seed}"[:80] if seed else title
                slot._titled = True
                await self._persist_slot_title(slot)
                slot.append("assistant", f"{title}\n\n{result_text}", "msg msg-a")
                self.dashboard_state.push_slot_title(slot.key, slot.title)
                self.dashboard_state.push_slots_update()
                self.dashboard_state.notify("heartbeat", title, body, meta={"slot": slot.key})
            return

        # ── slack (no thread) → new Slack DM only ──
        if deliver == "slack":
            if self.slack and self._owner_id:
                try:
                    channel = await self.slack.open_dm(self._owner_id)
                    if channel:
                        for post in _heartbeat_slack_parts(title, result_text):
                            await self.slack.post_message(channel, post)
                except Exception:
                    logger.exception("Heartbeat Slack delivery failed")
            return

        # ── slack:<channel>:<thread_ts> → reply to thread ──
        if deliver.startswith("slack:"):
            parts = deliver.split(":", 2)
            try:
                if self.slack and len(parts) == 3:
                    chan, ts = parts[1], parts[2]
                    for post in _heartbeat_slack_parts(title, result_text):
                        await self.slack.post_message(chan, post, ts)
                elif self.slack and self._owner_id:
                    chan = await self.slack.open_dm(self._owner_id)
                    if chan:
                        for post in _heartbeat_slack_parts(title, result_text):
                            await self.slack.post_message(chan, post)
            except Exception:
                logger.exception("Heartbeat Slack delivery failed")
            if self.dashboard_state:
                self.dashboard_state.notify("heartbeat", title, body)
            return

        # ── default: Slack DM + dashboard notification ──
        if self.slack and self._owner_id:
            try:
                channel = await self.slack.open_dm(self._owner_id)
                if channel:
                    for post in _heartbeat_slack_parts(title, result_text):
                        await self.slack.post_message(channel, post)
            except Exception:
                logger.exception("Heartbeat Slack delivery failed")
        if self.dashboard_state:
            self.dashboard_state.notify("heartbeat", title, body)

    def _init_mcp_discovery(self) -> None:
        """Log configured MCP servers at startup.

        The actual config merge is handled by rebuild_agent_config() which
        runs earlier in __init__. This just logs what's configured for
        debugging visibility.
        """
        try:
            from kiro_crew.mcp_discovery import list_servers  # circular import

            servers = list_servers()
            if servers:
                srv_names = [s.name for s in servers]
                logger.info("Configured MCP servers: %s", ", ".join(srv_names))
            else:
                logger.info("No MCP servers configured")
        except Exception:
            logger.debug("MCP server listing failed", exc_info=True)

    def _subagent_coalescer(self) -> "SubagentEventCoalescer":
        """Lazily construct the scale coalescer (needs dashboard_state +
        subagent_mgr, both wired after __init__)."""
        if self._subagent_coalescer_inst is None:
            from kiro_crew.subagent_scale import SubagentEventCoalescer

            _state = self.dashboard_state

            def _bcast_all(t: str, d: dict) -> None:
                if _state:
                    _state.broadcast_ws(t, d)

            def _bcast_subs(t: str, d: dict) -> None:
                if _state:
                    _state.broadcast_ws_subagent_subscribers(t, d)

            self._subagent_coalescer_inst = SubagentEventCoalescer(
                _bcast_all,
                _bcast_subs,
                lambda: self.subagent_mgr.running_count if self.subagent_mgr else 0,
            )
        return self._subagent_coalescer_inst

    def _init_subagents(self) -> None:
        """Initialize the subagent manager."""

        # Per-slot WS events route by EXACT slot-key match in the frontend —
        # `subagent_event_slot` maps a parent session key to the tab that
        # displays it (cron-born tabs are `cron-<id>`, channel-born tabs their
        # transcript stem), falling back to the legacy prefix-strip when no
        # tab is open. A raw `removeprefix("dashboard:")` here left the
        # Subagents panel permanently empty for cron/channel-born sessions.
        _event_slot = subagent_event_slot

        async def _broadcast_subagent_status(info: SubagentInfo, event: str) -> None:
            """Broadcast subagent status change via WS for per-slot tracking."""
            if not self.dashboard_state:
                return
            try:
                slot = _event_slot(info.parent_session_key)
                agents = (
                    self.subagent_mgr.running_agents_for(info.parent_session_key)
                    if self.subagent_mgr
                    else []
                )
                running = len(agents)
                payload = {
                    "running": running,
                    "id": info.id,
                    "event": event,
                    "slot": slot,
                    "agents": agents,
                }
                logger.info(
                    "📡 subagent_status WS: event=%s slot=%s running=%d agents=%d",
                    event,
                    slot,
                    running,
                    len(agents),
                )
                self.dashboard_state.broadcast_ws("subagent_status", payload)
            except Exception:
                logger.info("Failed to broadcast subagent %s status", info.id, exc_info=True)

        def _retrigger_recovery(slot: "_ChatSlot", parent_key: str) -> None:
            """Drain queued failures into a new recovery _run_chat turn.

            Called from _on_done callbacks after resetting the guard, so
            failures that arrived while the previous recovery was running
            get processed without waiting for user input.
            """
            if slot._recovery_chat_triggered or not slot._pending_subagent_failures:
                return
            if not self.dashboard_state:
                return
            _max_retrigger = 3
            if slot._recovery_retrigger_count >= _max_retrigger:
                logger.warning(
                    "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                    _max_retrigger,
                    parent_key,
                    len(slot._pending_subagent_failures),
                )
                slot._pending_subagent_failures.clear()
                return
            slot._recovery_retrigger_count += 1
            slot._recovery_chat_triggered = True
            # Bound here rather than at module scope: this reads ``_run_chat`` from
            # ``dashboard.chat``, a different module than the top-level
            # ``chat_runner`` import, and resolving it per call is what lets a test
            # patch ``dashboard.chat._run_chat`` and have this path observe it.
            from kiro_crew.dashboard.chat import _run_chat

            failures = slot._pending_subagent_failures[:]
            slot._pending_subagent_failures.clear()
            msg = "\n\n".join(failures)
            msg, _ = redact_exfiltration_urls(msg)
            msg, _ = redact_credentials(msg)
            slot.append("user", msg, "msg msg-u auto-go")
            logger.info(
                "Re-triggering recovery _run_chat for %s (%d queued failures)",
                parent_key,
                len(failures),
            )

            def _done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                if t.cancelled():
                    logger.warning("Re-triggered recovery cancelled for %s", parent_key)
                    slot._recovery_chat_triggered = False
                    return
                elif t.exception():
                    logger.error(
                        "Re-triggered recovery failed for %s",
                        parent_key,
                        exc_info=t.exception(),
                    )
                slot._recovery_chat_triggered = False
                if slot._pending_subagent_failures:
                    _retrigger_recovery(slot, parent_key)

            _task = asyncio.create_task(
                bounded_chat_turn(_run_chat(self.dashboard_state, slot, msg)),
            )
            slot.task = _task
            self._background_tasks.add(_task)
            _task.add_done_callback(self._background_tasks.discard)
            _task.add_done_callback(_done)

        async def _subagent_done(info: SubagentInfo) -> None:
            async def _inject_with_retry(
                client,
                msg: str,
                parent_key: str,
                label: str,
            ) -> str | None:
                """Retry stream_and_collect up to 3 times on AcpError.

                Cancels any orphaned prompt between attempts so the next
                retry doesn't hit 'Prompt already in progress'.
                """
                for attempt in range(3):
                    try:
                        return await stream_and_collect(client, msg, retry_transient=False)
                    except PromptBusyExhaustedError:
                        # Provider is dead after exhausting prompt-busy retries.
                        # Reset session + notify, same as TimeoutError path.
                        logger.error(
                            "Subagent %s: provider dead after prompt-busy retries (%s)",
                            info.id,
                            label,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to reset %s after busy exhaustion",
                                parent_key,
                                exc_info=True,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="provider dead after prompt-busy retries",
                            )
                        return None
                    except AcpProcessDied:
                        logger.warning(
                            "Subagent %s: ACP process died during %s injection",
                            info.id,
                            label,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to reset %s after process death",
                                parent_key,
                                exc_info=True,
                            )
                        if self.subagent_mgr:
                            self.subagent_mgr.notify_injection_failed(
                                info,
                                reason="ACP process died",
                            )
                        return None
                    except AcpError:
                        if attempt == 2:
                            raise
                        logger.warning(
                            "Subagent %s %s injection attempt %d failed, retrying",
                            info.id,
                            label,
                            attempt + 1,
                        )
                        try:
                            assert self.sessions is not None
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for %s",
                                info.id,
                                exc_info=True,
                            )
                        await asyncio.sleep(2**attempt)
                return None  # unreachable, but satisfies type checker

            # A synthetic flush-only record is NOT a wave member (see
            # SubagentManager.force_digest_flush): it exists only to force the
            # wave's pending digest chunk out when its hold deadline expired.
            # Every per-member side effect below must be skipped for it — a
            # terminal WS event, orchestration accounting or a done/ok counter
            # bump would invent an agent that never ran.
            _flush_only = getattr(info, "_digest_flush_only", False) is True

            if not _flush_only:
                await _broadcast_subagent_status(info, "done")
            # Three-way outcome: a user stop is neutral — neither a success nor
            # a failure. The record contract keeps ``error`` unset for stops, so
            # every consumer below must branch on ``user_stopped`` explicitly
            # rather than inferring success from an empty error.
            if info.user_stopped:
                status, emoji, single_outcome = "stopped by user", "⏹", OUTCOME_STOPPED
            elif info.error:
                status, emoji, single_outcome = "failed", "❌", OUTCOME_FAILED
            else:
                status, emoji, single_outcome = "completed", "✅", OUTCOME_OK
            title = f"Subagent `{info.id}` {emoji}"

            # ── Orchestration guard: track failures (only in orchestrator mode) ──
            parent_key = info.parent_session_key
            guard_msg = ""
            try:
                _is_orchestrator = False
                _slot = None
                # Stage limits are a property of the tab the orchestrator runs
                # in, not of where its conversation started.
                _parent_slot_name = dashboard_slot_key(parent_key)
                if self.dashboard_state and _parent_slot_name:
                    _slot = self.dashboard_state.get_slot(_parent_slot_name)
                    _is_orchestrator = (
                        _slot is not None and getattr(_slot, "mode", "") == "orchestrator"
                    )
                if _slot is not None and _is_orchestrator:
                    from kiro_crew.context_management import (
                        MAX_STAGE_ESCALATIONS,
                        MAX_STAGE_ROUNDS,
                        OrchestrationTracker,
                    )

                    if not getattr(_slot, "_orch_tracker", None):
                        _slot._orch_tracker = OrchestrationTracker()
                    tracker = _slot._orch_tracker
                    if tracker.stopped:
                        logger.info("Orchestration stopped, ignoring subagent result %s", info.id)
                        return
                    task_key = info.task[:80]
                    if _flush_only:
                        # No task ran — nothing to record. Recording it as a
                        # success would credit a stage round to a timer.
                        pass
                    elif info.user_stopped:
                        # User stop: neither success nor failure. Recording it
                        # as success would let orchestration/synthesis advance
                        # on work the user explicitly killed and permanently
                        # skew success stats; recording it as failure would
                        # trigger retry-guidance guards for a deliberate act.
                        pass
                    elif info.error:
                        if tracker.record_failure(task_key):
                            guard_msg = (
                                f"\n\n⚠️ [SYSTEM] Task '{task_key}' has failed "
                                f"{tracker.failure_count(task_key)} times. "
                                "You MUST ask the user for guidance before retrying."
                            )
                    else:
                        tracker.record_success(task_key)
                    # Track spawn rounds — count each completed batch as a round
                    pending = (
                        self.subagent_mgr.running_agents_for(parent_key)
                        if self.subagent_mgr
                        else []
                    )
                    if not pending and not _flush_only:
                        # All agents done → one round completed
                        stage = tracker.current_stage
                        if tracker.record_round(stage):
                            if tracker.is_force_failed(stage):
                                guard_msg += (
                                    f"\n\n🛑 [SYSTEM] Stage {stage} has failed after "
                                    f"{MAX_STAGE_ESCALATIONS} escalations ({tracker.round_count(stage)} rounds). "
                                    "You MUST stop this stage and report the failure to the user. "
                                    "Do NOT retry or spawn more agents."
                                )
                            else:
                                guard_msg += (
                                    f"\n\n⚠️ [SYSTEM] Stage {stage} has used "
                                    f"{tracker.round_count(stage)}/{MAX_STAGE_ROUNDS} spawn rounds. "
                                    "You MUST ask the user for guidance before spawning more."
                                )
            except Exception:
                logger.warning("Orchestration guard failed for %s", info.id, exc_info=True)
            # Chat mode: inline info.result (subagent.py already trimmed it to
            # agent.completion_keep + completion_keep_chars) when it fits. When the
            # completion copy dropped content (result_truncated) or in orchestrator
            # mode, emit a summary + result_path pointer so the parent reads the full
            # transcript on demand (read / grep / spawn_status) instead of re-running
            # the subagent.
            result_path = info.result_path or ""
            if info.user_stopped:
                _partial = info.result or ""
                detail = (
                    "Stopped by the user before completing. Do NOT treat this as "
                    "a finished result or retry it unprompted."
                    + (f"\n\nPartial output:\n{_partial}" if _partial else "")
                )
            elif info.error:
                detail = f"Error: {info.error}"
            elif result_path and (info.result_truncated or _is_orchestrator):
                detail = summarize_result(info.result, result_path)
            else:
                detail = info.result or "_No response._"
            detail, _ = redact_exfiltration_urls(detail)
            detail, _ = redact_credentials(detail)
            task_text, _ = redact_exfiltration_urls(info.task)
            task_text, _ = redact_credentials(task_text)
            task_text = task_text[:100]
            body = f"{task_text}\n\n{detail}"
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)

            announce = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}`"
                f"{f' ({info.agent})' if info.agent else ''}"
                f" {status} {emoji}\n"
                f"Task: {task_text}\n\n"
                f"{detail}"
                f"{guard_msg}"
            )
            # Structured header facts for the dashboard card, stamped on the row
            # so a reword of the prose above cannot silently break rendering
            # (#1792). The card reads this; the frontend regexes are a fallback.
            # Reassigned for the wave-digest shapes below when this member's
            # completion is folded into a batch chunk instead of injected alone.
            sub_meta = single_completion_meta(
                agent_id=info.id,
                outcome=single_outcome,
                agent_name=info.agent or "",
                task=task_text,
            )

            parent_key = info.parent_session_key

            if _flush_only:
                # The synthetic record has no result of its own. Its title/body
                # are only used by the "parent slot gone → notification only"
                # fallback, so make them describe the WAVE, not a phantom agent.
                title = "Wave results (partial)"
                body = task_text

            # ── Batch accounting + wave digest (scale plumbing) ──
            # Every batch member is accounted here (the single completion
            # consumer for all terminal paths). Waves larger than the digest
            # threshold deliver ONE consolidated injection turn when the wave
            # finishes, instead of N per-agent turns — at 60-100 agents the
            # per-agent turns are the parent-context flood (N full LLM turns)
            # and bury the 2 failures among 58 successes.
            # (Type guards: test doubles pass MagicMock infos whose attrs are
            # truthy mocks — only real str/int batch identity participates.)
            _batch_id = getattr(info, "batch_id", "")
            _batch_total = getattr(info, "batch_total", 0)
            if not isinstance(_batch_id, str):
                _batch_id = ""
            if not isinstance(_batch_total, int):
                _batch_total = 0
            if _flush_only and not _batch_id:
                # A flush-only record without wave identity has nothing to
                # release and MUST NOT fall through to the per-agent routing
                # below — that would inject a completion turn for an agent that
                # never ran.
                return
            if _batch_id:
                if _flush_only:
                    # Nothing to release (wave already closed / already flushed
                    # by a completion that raced this sweep) → no-op.
                    _bp = self._batch_progress.get(_batch_id)
                    if _bp is None or _bp["done"] <= _bp["flushed"]:
                        return
                    bp = _bp
                    # A forced flush never closes the wave: the sweep only fires
                    # while members are still outstanding, so the wave-close
                    # digest (counts + release guidance) is still to come.
                    _last = False
                    _oc = ""
                else:
                    bp = self._batch_progress.setdefault(
                        _batch_id,
                        {
                            "total": _batch_total,
                            "done": 0,
                            "ok": 0,
                            "err": 0,
                            "stopped": 0,
                            "fail_lines": [],
                            "ok_lines": [],
                            "guard_msgs": [],
                            "held_ok_ids": [],
                            # Members whose delivery is currently held, so the
                            # hold-deadline sweep's timestamps can be cleared
                            # when their chunk finally fires.
                            "held_infos": [],
                            # Chunked delivery bookkeeping: "flushed" = members whose
                            # results have already been delivered in a prior chunk;
                            # "chunks" = digest chunks emitted so far.
                            "flushed": 0,
                            "chunks": 0,
                        },
                    )
            if _batch_id and not _flush_only:
                bp["done"] += 1
                # Fold EVERY member's orchestration escalation into the wave
                # digest — held members return before the announce is sent, so
                # without accumulation only the last member's guard_msg would
                # survive and a mid-wave "you MUST ask the user" ceiling would
                # be silently dropped.
                if guard_msg:
                    bp["guard_msgs"].append(guard_msg)
                _oc = info.outcome
                if _oc == "stopped":
                    bp["stopped"] += 1
                elif _oc == "failed":
                    bp["err"] += 1
                else:
                    bp["ok"] += 1
                # Exception-first digest content: failures/stops carry detail,
                # successes are one pointer line (full output stays on disk).
                if _oc == "completed":
                    bp["ok_lines"].append(
                        f"— `{info.id}` ✅ {task_text[:80]}"
                        + (f"\n  → {result_path}" if result_path else "")
                    )
                else:
                    bp["fail_lines"].append(
                        f"— `{info.id}` {status} {emoji} · {task_text[:80]}\n"
                        f"  {detail[:400]}{'…' if len(detail) > 400 else ''}"
                    )
                _last = bp["total"] > 0 and bp["done"] >= bp["total"]
                if not _last:
                    # Robustness: a wave member that failed AT SPAWN never
                    # reaches this consumer, so done can never hit total.
                    # Completion is decided by THIS batch's outstanding
                    # members only (running OR still queued behind the
                    # stagger gate) — an unrelated agent under the same
                    # parent must neither hold the digest hostage nor
                    # release it early.
                    try:
                        _last = bool(
                            self.subagent_mgr
                            and not self.subagent_mgr.batch_members_pending(_batch_id)
                        )
                    except Exception:
                        _last = False
                if _last:
                    self._batch_progress.pop(_batch_id, None)
                    # Prune per-wave bookkeeping for ALL wave sizes (bounds
                    # _seen_batches / _batch_submitted growth).
                    try:
                        if self.subagent_mgr:
                            self.subagent_mgr.finalize_batch(_batch_id)
                    except Exception:
                        logger.debug("finalize_batch failed", exc_info=True)
                    try:
                        if self.dashboard_state:
                            self.dashboard_state.broadcast_ws(
                                "batch_finished",
                                {
                                    "batch_id": _batch_id,
                                    "slot": _event_slot(parent_key),
                                    "total": bp["total"],
                                    "ok": bp["ok"],
                                    "err": bp["err"],
                                    "stopped": bp["stopped"],
                                },
                            )
                    except Exception:
                        logger.debug("batch_finished broadcast failed", exc_info=True)
            if _batch_id:
                if _flush_only and bp["total"] <= 1:
                    # Single-member wave: nothing is ever held, and falling
                    # through would route the phantom per-agent announce.
                    return
                if bp["total"] > 1:
                    # ── Chunked wave delivery ── completed results feed the
                    # parent queue-style: every SUBAGENT_DIGEST_CHUNK_SIZE
                    # completions flush ONE digest chunk (an injection turn);
                    # the final member flushes the remaining partial chunk.
                    # This bounds each digest's size AND gives the parent
                    # incremental signal — one straggler no longer withholds
                    # every sibling's result for its entire runtime (Design
                    # Review CONCERN 1).
                    #
                    # The count trigger alone cannot deliver that incremental
                    # signal for a wave smaller than the chunk size (issue
                    # #2215): _pending can never reach it, so wave close is the
                    # only flush. _flush_only is the LATENCY trigger the count
                    # lacks — the reaper's hold-deadline sweep forces the
                    # pending chunk out once results have been held too long.
                    _pending = bp["done"] - bp["flushed"]
                    _flush = _last or _flush_only or _pending >= SUBAGENT_DIGEST_CHUNK_SIZE
                    if not _flush:
                        # Held for the next chunk — the terminal WS event,
                        # tracker accounting, and stats above already ran;
                        # only the per-agent injection turn is suppressed.
                        # Restart safety: flag the member so the run loop SKIPS
                        # mark_delivered — its result is not in the parent's
                        # context yet, and a "delivered" tombstone would hide it
                        # from orphan reconciliation. If the gateway restarts
                        # mid-chunk, the orphan path finds these undelivered
                        # results and delivers a recovery digest; in normal
                        # operation they are marked delivered when their chunk
                        # flushes.
                        info._digest_held = True
                        # Hold clock for the reaper's hold-deadline sweep. Kept
                        # separate from _digest_held (the restart-safety flag the
                        # run loop reads) so the sweep never mutates that
                        # contract; cleared when this member's chunk fires.
                        info._digest_held_at = time.time()
                        bp.setdefault("held_infos", []).append(info)
                        if _oc == "completed":
                            bp["held_ok_ids"].append(info.id)
                        logger.info(
                            "Subagent %s: completion held for digest chunk (%d/%d done)",
                            info.id,
                            bp["done"],
                            bp["total"],
                        )
                        return
                    # Chunk fires now. Do NOT settle the held members'
                    # delivery tombstones here — composition precedes routing,
                    # and marking "delivered" before the chunk is handed off
                    # would re-open the restart-loss window (GPT 5.6 HIGH).
                    # Stash the ids on the flushing member: the run loop
                    # settles them only after _on_done (which includes the
                    # routing below) returns without raising.
                    info._digest_settle_ids = list(bp.get("held_ok_ids", []))
                    # These members are no longer held: stop the hold clock so
                    # the reaper's deadline sweep does not force a second flush
                    # for results this chunk already carries.
                    for _held in bp.get("held_infos", []):
                        _held._digest_held_at = 0.0
                    bp["held_infos"] = []
                    _failures = bp["fail_lines"]
                    _oks = bp["ok_lines"]
                    _digest_body = "\n".join(_failures + _oks)
                    if len(_digest_body) > 60_000:
                        _digest_body = _digest_body[:60_000] + "\n…(digest truncated)"
                    # Deduped union of this chunk's members' escalation
                    # guards — not just the flushing member's.
                    _guards = "".join(dict.fromkeys(bp.get("guard_msgs", [])))
                    bp["chunks"] += 1
                    bp["flushed"] = bp["done"]
                    _chunk_k = bp["chunks"]
                    # Total chunks: full chunks + one final partial. Completion
                    # order fills chunks to exactly CHUNK_SIZE, so this is
                    # ceil(total / chunk_size) — but a NON-final chunk always
                    # has at least the wave-close chunk still to come, so it can
                    # never honestly label itself k/k. Without the +1 a
                    # deadline-forced flush on a small wave would announce
                    # "1/1 — 1 still running", telling the parent the wave is
                    # fully delivered while a member is outstanding.
                    _chunk_j = max(
                        _chunk_k if _last else _chunk_k + 1,
                        -(-bp["total"] // SUBAGENT_DIGEST_CHUNK_SIZE),
                    )
                    _footer = (
                        "Failures are listed first. Full outputs are on disk — "
                        "read the result paths on demand; do NOT re-run "
                        "completed agents."
                    )
                    if _last:
                        # Final chunk: release the spawn-discipline gate.
                        announce = (
                            f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                            f"Batch results {_chunk_k}/{_chunk_j} — wave finished: "
                            f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                            f"{bp['stopped']} ⏹ of {bp['total']} agents. "
                            f"All results delivered.\n"
                            f"This run is complete. Finish processing all "
                            f"results before spawning any follow-up "
                            f"sub-agents.\n"
                            f"{_footer}\n\n{_digest_body}{_guards}"
                        )
                        # This member's completion is delivered as the wave-close
                        # digest, not a per-agent row — stamp the digest's facts
                        # (tallies + chunk index) in place of the single-agent
                        # meta built above.
                        sub_meta = wave_final_meta(
                            chunk=_chunk_k,
                            chunks=_chunk_j,
                            ok=bp["ok"],
                            failed=bp["err"],
                            stopped=bp["stopped"],
                            total=bp["total"],
                        )
                    else:
                        # Non-final chunk: spawn-discipline guidance — the
                        # parent wakes mid-wave, so it must not start new
                        # spawns that would interleave with the batches still
                        # arriving from this run.
                        _remaining = max(0, bp["total"] - bp["done"])
                        # A deadline-forced flush is a STRAGGLER release, not a
                        # full chunk: say so, or the parent reads "still
                        # running" as normal progress and keeps waiting rather
                        # than deciding whether the remainder is worth waiting
                        # for (issue #2215).
                        _why = (
                            f"The results below were finished and held for "
                            f"{int(DIGEST_HOLD_SECS)}s+ while the remaining "
                            f"agent(s) ran, so they are being delivered early "
                            f"as a PARTIAL result set. Synthesize what you can "
                            f"now; if a remaining agent never reports, work "
                            f"with what you have or tell the user.\n"
                            if _flush_only
                            else ""
                        )
                        announce = (
                            f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                            f"Batch results {_chunk_k}/{_chunk_j} — "
                            f"{bp['done']} of {bp['total']} delivered, "
                            f"{_remaining} still running.\n"
                            f"{_why}"
                            f"Process these results now, but do NOT spawn new "
                            f"sub-agents yet — more result batches from this "
                            f"run are still arriving, and spawning now will "
                            f"interleave with them.\n"
                            f"{_footer}\n\n{_digest_body}{_guards}"
                        )
                        # Mid-wave chunk: progress facts (delivered/running), no
                        # tallies — mirrors the CHUNK regex the frontend demotes.
                        sub_meta = wave_chunk_meta(
                            chunk=_chunk_k,
                            chunks=_chunk_j,
                            delivered=bp["done"],
                            total=bp["total"],
                            running=_remaining,
                        )
                        # Reset per-chunk buffers for the next chunk. (On the
                        # final chunk bp was already popped from
                        # _batch_progress above — nothing to reset.)
                        bp["fail_lines"] = []
                        bp["ok_lines"] = []
                        bp["guard_msgs"] = []
                        bp["held_ok_ids"] = []

            # ── Route completion back to the originating session ──
            # Tab open        → that tab (a channel-born tab mirrors on to its channel)
            # Channel, no tab → channel thread + dashboard notification
            # Cron/no parent  → dashboard notification only

            # Crew-mode ownership (RFC orchestrator-chat-sessions): runs
            # dispatched by the CrewOrchestrator deliver through its
            # forward/attribution pipeline, never the default injection —
            # placed after batch accounting so wave bookkeeping stays intact.
            # isinstance (not truthiness): dashboard_state may be a test double
            # whose .crew is an auto-created attribute; only a real
            # CrewOrchestrator owns runs.
            _crew = getattr(self.dashboard_state, "crew", None) if self.dashboard_state else None
            # Imported HERE, not at module scope: this module is on the gateway's
            # boot path and `--no-dashboard` must not pay for a dashboard-only
            # subsystem before it is ready to serve. By the time a subagent
            # completes with a live `.crew`, `crew_chat` is already imported, so
            # this costs a sys.modules hit. `_crew is None` short-circuits first,
            # which is the whole API-only case.
            if _crew is not None:
                from kiro_crew.crew_chat import CrewOrchestrator
            if _crew is not None and isinstance(_crew, CrewOrchestrator) and _crew.owns(info.id):
                try:
                    await _crew.on_subagent_done(info)
                    return
                except Exception:
                    # Do NOT swallow-and-return: fall through to the default
                    # injection path so the result still reaches the user
                    # (a crew-store write failure must not silently discard
                    # the completion — GPT review finding on 76d35e37).
                    logger.warning(
                        "crew: completion delivery failed for %s — falling back to default injection",
                        info.id,
                        exc_info=True,
                    )

            _slot_name = dashboard_slot_key(parent_key)
            if _slot_name and self.dashboard_state:
                # Route the result through _run_chat for full streaming, tool
                # call visibility, and proper lifecycle. A channel-born tab
                # runs on the channel's own session, so the turn's mirror
                # carries the reply back to the thread — the raw-injection path
                # below is for parents with no tab to stream into.
                _injection_slot = self.dashboard_state.get_slot(_slot_name)

                # Redact LLM-generated output before any external surface
                announce, _ = redact_exfiltration_urls(announce)
                announce, _ = redact_credentials(announce)
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)

                if _injection_slot:

                    # ── Fix 2 (B1): arm a one-shot post-fan-out synthesis turn ──
                    # When this is the LAST outstanding sub-agent for the parent
                    # (chat mode only), flag the slot so that once every completion
                    # has been processed and the queue drains, _run_chat fires ONE
                    # dedicated synthesis turn (see chat_runner drain/idle branch).
                    # Ordering guarantees running_agents_for == [] here on the last
                    # agent (info.done set + _running_count decremented first).
                    if not _is_orchestrator:
                        try:
                            _still_running = (
                                self.subagent_mgr.running_agents_for(parent_key)
                                if self.subagent_mgr
                                else None
                            )
                        except Exception:
                            _still_running = None  # error → don't arm (fail safe)
                        if _still_running == []:
                            _injection_slot._pending_synthesis = True

                    # ── Skip injection for blocking-tool-collected results ──
                    # spawn_sub_agents (blocking MCP tool) already delivered
                    # this result inline as a tool-call return value. Injecting
                    # it again would trigger a redundant _run_chat turn whose
                    # assistant response shadows any [OPTIONS:] buttons from the
                    # synthesis message. Mark delivered and return.
                    # NOTE: This check is placed BEFORE the inflight counter and
                    # busy-wait because at this point the blocking tool's
                    # mark-collected POST has already landed (the tool returns
                    # before its turn ends, and _subagent_done fires only after
                    # the agent's terminal report, which is after the tool has
                    # finished). However, if the slot is busy (turn still
                    # running) we must wait first, then re-check — see the
                    # second check after the busy-wait below.
                    if info.id in _injection_slot._subagents_inline_collected:
                        _injection_slot._subagents_inline_collected.discard(info.id)
                        # Disarm synthesis — the blocking tool already delivered
                        # all results and the model synthesized inline.
                        if not _injection_slot._subagents_inline_collected:
                            _injection_slot._pending_synthesis = False
                        logger.info(
                            "Subagent %s: skipping injection (already collected inline by spawn_sub_agents)",
                            info.id,
                        )
                        return

                    # Fix 2 (B1) race guard: count this completion as an
                    # in-flight delivery from entry until it is handed off (turn
                    # launched or queued). The synthesis fire-gate in chat_runner
                    # requires this count to be zero, so a concurrently-finishing
                    # sibling that is still awaiting the current turn (busy path)
                    # can't let an earlier turn fire synthesis before this result
                    # is delivered. try/finally so a CancelledError can't leak it.
                    _injection_slot._subagent_deliveries_inflight += 1
                    try:
                        if _injection_slot_busy(_injection_slot):
                            # Slot is busy (or an injection is dispatched but
                            # not yet started) — wait for that task to finish,
                            # then inject. No visible queue card.
                            _current = _injection_slot.task
                            if _current is not None:
                                try:
                                    await asyncio.wait_for(
                                        asyncio.shield(_current),
                                        timeout=INJECTION_TIMEOUT,
                                    )
                                except asyncio.TimeoutError:
                                    pass  # Timed out waiting — slot still busy, will be queued below
                                except asyncio.CancelledError:
                                    raise  # Don't swallow cancellation of this coroutine
                                except Exception:
                                    pass  # Task failed — slot is now idle

                            # Re-check: another injection may have claimed the slot
                            # during the await above.
                            if _injection_slot_busy(_injection_slot):
                                # Check inline-collected before queuing — if the
                                # blocking tool already handled this result, don't
                                # queue it for a later redundant turn.
                                if info.id in _injection_slot._subagents_inline_collected:
                                    _injection_slot._subagents_inline_collected.discard(info.id)
                                    if not _injection_slot._subagents_inline_collected:
                                        _injection_slot._pending_synthesis = False
                                    logger.info(
                                        "Subagent %s: skipping queue " "(already collected inline)",
                                        info.id,
                                    )
                                    return
                                logger.info(
                                    "Subagent %s: slot %s claimed by another injection, queuing",
                                    info.id,
                                    _slot_name,
                                )
                                # Bounded by the configured turn ceiling
                                # (chat_turn_timeout_secs, 7200s default):
                                # _run_chat's finally block drains slot._queue
                                # on any exit path.
                                # Carry the structured completion facts so the
                                # drained row is a card without re-parsing the
                                # prose (#1792); _start_next_queued_turn reads them.
                                _injection_slot.queue_append(
                                    announce,
                                    kind=SUBAGENT_COMPLETION_KIND,
                                    meta={SUBAGENT_COMPLETION_META_KEY: sub_meta},
                                )
                                # Queuing is not delivery. The announce promises
                                # result paths the parent can read on demand, but
                                # it will not be in the parent's context until a
                                # turn drains it — a wait bounded only by the turn
                                # ceiling, so longer than the retention TTL. Owe
                                # the delivery tombstones to that drain instead of
                                # writing them now, or the reaper prunes
                                # result.txt while the promise is still queued
                                # and the parent is handed dead paths (#4839).
                                self._defer_queued_delivery(
                                    _injection_slot, announce, info, flush_only=_flush_only
                                )
                                self.dashboard_state.push_slots_update()
                                logger.info("Subagent %s → queued in %s", info.id, _slot_name)
                                return

                        # Slot is idle — re-check inline-collected (the
                        # blocking tool's mark-collected POST has now landed,
                        # since the tool returns before its owning turn ends).
                        if info.id in _injection_slot._subagents_inline_collected:
                            _injection_slot._subagents_inline_collected.discard(info.id)
                            if not _injection_slot._subagents_inline_collected:
                                _injection_slot._pending_synthesis = False
                            logger.info(
                                "Subagent %s: skipping injection after wait "
                                "(already collected inline by spawn_sub_agents)",
                                info.id,
                            )
                            return

                        # Slot is idle — start _run_chat.
                        _task = asyncio.create_task(
                            bounded_chat_turn(
                                _run_chat(self.dashboard_state, _injection_slot, announce)
                            )
                        )
                        _injection_slot.task = _task
                        self.dashboard_state._background_tasks.add(_task)
                        _task.add_done_callback(self.dashboard_state._background_tasks.discard)

                        def _on_inject_done(t: asyncio.Task) -> None:  # type: ignore[type-arg]
                            if _injection_slot.task is t:
                                _injection_slot.task = None
                            if not t.cancelled() and t.exception():
                                logger.error(
                                    "Subagent injection _run_chat failed: %s", t.exception()
                                )
                                if self.subagent_mgr:
                                    _reason = str(t.exception())
                                    _reason, _ = redact_exfiltration_urls(_reason)
                                    _reason, _ = redact_credentials(_reason)
                                    self.subagent_mgr.notify_injection_failed(
                                        info,
                                        reason=_reason,
                                    )

                        _task.add_done_callback(_on_inject_done)
                        self.dashboard_state.push_slots_update()
                        logger.info("Subagent %s → _run_chat in %s", info.id, _slot_name)
                    finally:
                        _injection_slot._subagent_deliveries_inflight -= 1
                else:
                    logger.info(
                        "Subagent %s: parent slot %s gone, notification only",
                        info.id,
                        _slot_name,
                    )
                    # Only notify when slot is gone — active slots already show
                    # results in the Activity panel and chat.
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                return

            if parent_key and not parent_key.startswith(("cron:", "subagent:")):
                # Channel session — inject silently into the parent's ACP
                # session, then deliver only the synthesized reply to the
                # conversation. Retry up to _MAX_INJECT_ATTEMPTS times on
                # timeout. Slack keeps its dedicated rich posting; every other
                # channel namespace (Telegram, Discord, …) delivers through
                # the governed transport ladder — its stored channel value is
                # not a Slack channel id, so posting it through the Slack
                # client can never reach the user.
                assert self.sessions is not None
                _namespace = channel_namespace_of(parent_key)
                _via_transport = bool(_namespace) and _namespace != SLACK_NAMESPACE
                _inject_label = _namespace if _via_transport else "Slack"
                # Snapshot the delivery target BEFORE the injection retry
                # loop: the timeout path's sessions.reset() evicts the
                # session's in-memory origin link, so resolving after a retry
                # would lose a Discord thread/forum target and drop the reply.
                _reply_link = self._channel_reply_link(parent_key) if _via_transport else None
                _injected = False
                _inject_failure_reasons: list[str] = []
                _sleep_before_retry = False
                for _attempt in range(1, _MAX_INJECT_ATTEMPTS + 1):
                    if _sleep_before_retry:
                        await asyncio.sleep(2)
                        _sleep_before_retry = False
                    _acquired = False
                    _footer_client = None
                    try:
                        logger.debug(
                            "Subagent %s: %s injection attempt %d/%d into %s",
                            info.id,
                            _inject_label,
                            _attempt,
                            _MAX_INJECT_ATTEMPTS,
                            parent_key,
                        )
                        client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                        _acquired = True
                        _footer_client = client
                        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                        if self.ctx_builder:
                            msg, _ = await run_in_embed_pool(
                                self.ctx_builder.build_message,
                                announce,
                                is_new,
                                parent_key,
                                provider_type=_provider,
                            )
                        else:
                            msg = announce
                        response = await asyncio.wait_for(
                            _inject_with_retry(client, msg, parent_key, _inject_label),
                            timeout=INJECTION_TIMEOUT,
                        )
                        _injected = True  # LLM processed result; channel posting is best-effort

                        # Persisted at the per-attempt level, NOT inside the
                        # Slack branch below: a channel parent (Discord,
                        # Telegram) delivers via the transport ladder and skips
                        # that branch entirely, so persisting there dropped
                        # every non-Slack subagent turn from replay. It still
                        # runs BEFORE the Slack control is posted, so the
                        # control's staleness token names this turn rather than
                        # the one before it.
                        # Persist BEFORE the control below invites
                        # an answer to this turn: the token names
                        # this session's last written row, so an
                        # unwritten turn stamps the control with the
                        # PREVIOUS turn's position and the first
                        # click reads as already superseded.
                        if self.conv_log and not (
                            is_thread_temporary(parent_key) or is_thread_incognito(parent_key)
                        ):
                            try:
                                # Defense-in-depth: `announce` is composed from
                                # already-redacted parts plus identifiers such as
                                # `info.agent`; we re-redact before persisting to the
                                # dashboard replay (an external surface), mirroring the
                                # dashboard branch. `response` is fresh LLM output from
                                # stream_and_collect and is NOT yet redacted, so its
                                # redaction here is strictly required.
                                safe_announce, _ = redact_exfiltration_urls(announce)
                                safe_announce, _ = redact_credentials(safe_announce)
                                safe_response, _ = redact_exfiltration_urls(response or "")
                                safe_response, _ = redact_credentials(safe_response)
                                await save_conversation_turn_off_loop(
                                    self.conv_log,
                                    parent_key,
                                    safe_announce,
                                    safe_response,
                                    source_thread=parent_key,
                                    source_user="subagent",
                                    agent=_get_agent_for_session(parent_key),
                                )
                            except Exception:
                                logger.warning(
                                    "Failed to persist subagent turn for %s",
                                    parent_key,
                                    exc_info=True,
                                )
                        # Deliver only the LLM's synthesized response to the
                        # parent's own conversation. Non-Slack channels go
                        # through the governed transport ladder; on failure
                        # the dashboard notification below still fires.
                        if _via_transport and response:
                            await self._deliver_channel_reply(
                                parent_key, response, resolved_link=_reply_link
                            )
                        # Post only the LLM's synthesized response to Slack
                        try:
                            if response and not _via_transport and self.slack and self._owner_id:
                                channel = (
                                    self.sessions.get_channel(parent_key) if self.sessions else None
                                ) or await self.slack.open_dm(self._owner_id)
                                if channel:
                                    # OPTIONS off the RAW text, then render: the
                                    # tag is plain text, so extracting it after
                                    # conversion made the controls hostage to
                                    # to_slack_mrkdwn's 39,000-char truncation.
                                    reply_text, options = extract_options(response)
                                    for part in render_for_slack(reply_text):
                                        await self.slack.post_message(channel, part, parent_key)
                                    try:
                                        elapsed = (
                                            info.elapsed
                                            if info.elapsed > 0
                                            else (time.monotonic() - info.started)
                                        )
                                        footer_blocks, footer_text = build_timing_footer(
                                            elapsed,
                                            _footer_client,
                                        )
                                        if options:
                                            _sub_token = await asyncio.to_thread(
                                                mint_options_token,
                                                self.dashboard_state,
                                                parent_key,
                                            )
                                            footer_blocks.extend(
                                                build_options_blocks(
                                                    options, staleness_token=_sub_token
                                                )
                                            )
                                        _footer_ts = await self.slack.post_blocks(
                                            channel,
                                            footer_blocks,
                                            footer_text,
                                            parent_key,
                                        )
                                        self._remember_options(
                                            parent_key,
                                            channel,
                                            _footer_ts,
                                            options,
                                            footer_blocks,
                                            footer_text,
                                        )
                                    except Exception:
                                        logger.debug(
                                            "Failed to post timing footer for %s",
                                            parent_key,
                                            exc_info=True,
                                        )
                        except Exception:
                            logger.exception(
                                "Subagent %s: Slack posting failed (injection succeeded)",
                                info.id,
                            )

                        # Persist the subagent completion turn to the conversation
                        # log so the dashboard replay shows it. Without this, Slack
                        # subagent injections are visible in the thread but missing
                        # from the dashboard session history.

                        logger.info(
                            "Subagent %s → %s session %s", info.id, _inject_label, parent_key
                        )
                        break
                    except asyncio.TimeoutError:
                        _inject_failure_reasons.append(
                            f"attempt {_attempt} timed out after {int(INJECTION_TIMEOUT)}s"
                        )
                        logger.warning(
                            "Subagent %s: %s injection attempt %d/%d timed out after %.0fs",
                            info.id,
                            _inject_label,
                            _attempt,
                            _MAX_INJECT_ATTEMPTS,
                            INJECTION_TIMEOUT,
                        )
                        if _acquired:
                            try:
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after channel injection timeout",
                                    parent_key,
                                    exc_info=True,
                                )
                        if _attempt < _MAX_INJECT_ATTEMPTS:
                            _sleep_before_retry = True
                    except Exception as exc:
                        _inject_failure_reasons.append(f"attempt {_attempt} failed: {exc}")
                        logger.exception("Subagent %s %s injection failed", info.id, _inject_label)
                        break
                    finally:
                        if _acquired:
                            try:
                                await self.sessions.cancel_current(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to cancel parent prompt for %s",
                                    info.id,
                                    exc_info=True,
                                )
                            try:
                                self.sessions.release(parent_key)
                            except Exception:
                                logger.exception("Failed to release session %s", parent_key)

                if not _injected:
                    _last_failure_reason = "; ".join(_inject_failure_reasons)
                    _last_failure_reason, _ = redact_exfiltration_urls(_last_failure_reason)
                    _last_failure_reason, _ = redact_credentials(_last_failure_reason)
                    logger.error(
                        "Subagent %s: all %d %s injection attempts failed: %s",
                        info.id,
                        _MAX_INJECT_ATTEMPTS,
                        _inject_label,
                        _last_failure_reason,
                    )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=_last_failure_reason,
                        )
                # Dashboard notification
                if self.dashboard_state:
                    self.dashboard_state.notify(
                        "subagent",
                        title,
                        body,
                        meta=self._notif_meta(parent_key),
                    )
                return

            # Cron parent — inject result back into the cron session.
            # Track pending injections to avoid resetting the session while
            # other subagents are queued behind the per-session semaphore.
            if parent_key.startswith("cron:"):
                self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 0) + 1
                assert self.sessions is not None
                acquired = False
                cron_response: str | None = None
                try:
                    client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                    acquired = True
                    _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                    if self.ctx_builder:
                        msg, _ = await run_in_embed_pool(
                            self.ctx_builder.build_message,
                            announce,
                            is_new,
                            parent_key,
                            provider_type=_provider,
                        )
                    else:
                        msg = announce
                    cron_response = await asyncio.wait_for(
                        _inject_with_retry(client, msg, parent_key, "cron"),
                        timeout=INJECTION_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Subagent %s: cron injection timed out after %.0fs",
                        info.id,
                        INJECTION_TIMEOUT,
                    )
                    try:
                        await self.sessions.reset(parent_key)
                    except Exception:
                        logger.debug(
                            "Failed to reset %s after cron injection timeout",
                            parent_key,
                            exc_info=True,
                        )
                    if self.subagent_mgr:
                        self.subagent_mgr.notify_injection_failed(
                            info,
                            reason=f"injection timed out after {int(INJECTION_TIMEOUT)}s",
                        )
                except Exception:
                    logger.exception("Subagent %s cron injection failed", info.id)
                finally:
                    if acquired:
                        try:
                            await self.sessions.cancel_current(parent_key)
                        except Exception:
                            logger.debug(
                                "Failed to cancel parent prompt for cron %s", info.id, exc_info=True
                            )
                        try:
                            self.sessions.release(parent_key)
                        except Exception:
                            logger.exception("Failed to release session %s", parent_key)
                    self._cron_injecting[parent_key] = self._cron_injecting.get(parent_key, 1) - 1
                    if self._cron_injecting[parent_key] <= 0:
                        self._cron_injecting.pop(parent_key, None)
                if cron_response:
                    cron_response, _ = redact_exfiltration_urls(cron_response)
                    cron_response, _ = redact_credentials(cron_response)
                    body = f"{body}\n\n{cron_response}"
                    logger.info("Subagent %s → cron session %s", info.id, parent_key)
                    # also deliver the synthesized response to Slack.
                    # honor the parent cron job's silent flag too —
                    # info.silent is never set from the cron's silent setting for
                    # spawn_run sub-agents, so a silent cron would otherwise still
                    # post every subagent-completion turn to Slack.
                    try:
                        await self._deliver_cron_response(
                            parent_key,
                            cron_response,
                            silent=info.silent or self._cron_job_is_silent(parent_key),
                        )
                    except Exception:
                        logger.exception(
                            "Subagent %s: failed to deliver cron response to Slack",
                            info.id,
                        )
                # Reset only when no subagents running or QUEUED AND no
                # injections pending. Queued spawns (behind the concurrency /
                # stagger gate) have no SubagentInfo in `running` yet — a
                # sibling completing while the rest of the wave is still
                # queued must not reset the parent out from under them.
                still_running = self.subagent_mgr and (
                    any(
                        a.parent_session_key == parent_key and a.id != info.id
                        for a in self.subagent_mgr.running
                    )
                    or self.subagent_mgr.queued_count_for(parent_key) > 0
                )
                still_injecting = self._cron_injecting.get(parent_key, 0) > 0
                if not still_running and not still_injecting:
                    try:
                        await self.sessions.reset(parent_key)
                        logger.info(
                            "Cron session %s: last subagent done, session reset", parent_key
                        )
                        # reset succeeded → reaper no longer needs the
                        # registered ephemeral key. Clear inside try so a failed
                        # reset leaves the key registered (ephemeral session may
                        # still be alive — reaper must be able to target it).
                        # parent_key is "cron:{job_id}" (persistent) or
                        # "cron:{job_id}:{run_id}" (ephemeral); job_id is the
                        # second colon-separated segment in both cases. Clear
                        # ONLY when the registration still points at the session
                        # just reset: an agent-sequence job re-registers the
                        # NEXT agent's key (cron:{job_id}:{agent}) while a prior
                        # agent's deferred reset is still pending, and an
                        # unconditional clear here would strip the reaper's
                        # handle on that still-in-flight turn.
                        cron_svc = getattr(self, "cron_svc", None)
                        if cron_svc is not None:
                            parts = parent_key.split(":", 2)
                            if (
                                len(parts) >= 2
                                and cron_svc.get_active_session_key(parts[1]) == parent_key
                            ):
                                cron_svc.clear_active_session_key(parts[1])
                    except Exception:
                        logger.exception(
                            "Cron session %s: reset failed after last subagent", parent_key
                        )

            # Dashboard notification
            if self.dashboard_state and not info.silent:
                self.dashboard_state.notify(
                    "subagent",
                    title,
                    body,
                    meta=self._notif_meta(parent_key),
                )
            if not parent_key.startswith("cron:"):
                logger.info("Subagent %s → notification only (parent=%s)", info.id, parent_key)

        assert self.sessions is not None
        assert self.ctx_builder is not None

        def _is_yolo() -> bool:
            return safety_override().is_active()

        def _spawn_slot_resolver(request_id: str) -> str:
            """Resolve slot from spawn request_id (spawn:{agent_id})."""
            agent_id = request_id.removeprefix("spawn:")
            info = self.subagent_mgr.get(agent_id) if self.subagent_mgr is not None else None
            slot = _event_slot(info.parent_session_key) if info and info.parent_session_key else ""
            logger.info(
                "_spawn_slot_resolver: rid=%s agent_id=%s info=%s slot=%s",
                request_id,
                agent_id,
                info is not None,
                slot,
            )
            return slot

        _approve_subagent = self._interactive_approval(
            "subagent", slot_resolver=_spawn_slot_resolver
        )

        async def _spawn_approve(
            request_id: str, description: str, parent_session_key: str = ""
        ) -> bool:
            event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
            return await _approve_subagent(event, parent_session_key)

        # Debounced slots push: keep slots[].subagents_running live for every
        # SSE consumer (composer busy affordance, Board "working" lane, and
        # external readers of the slots stream). Without this, the field is
        # only fresh on a full GET — serialize_slots() computes it at call
        # time but nothing pushed on sub-agent lifecycle transitions. The
        # 0.2s coalesce window collapses batch spawns into one push. Covers
        # the reaper too: _force_reap fires subagent_done through the same
        # on_event path.
        _slots_push_pending = False

        def _flush_slots_push() -> None:
            nonlocal _slots_push_pending
            _slots_push_pending = False
            if self.dashboard_state:
                self.dashboard_state.push_slots_update()

        def _schedule_slots_push() -> None:
            nonlocal _slots_push_pending
            if _slots_push_pending:
                return
            _slots_push_pending = True
            asyncio.get_running_loop().call_later(0.2, _flush_slots_push)

        async def _subagent_event(etype: str, info: SubagentInfo, extra: dict) -> None:
            if not self.dashboard_state:
                return
            slot_name = _event_slot(info.parent_session_key)
            base = {"id": info.id, "slot": slot_name}
            # Batch identity rides every frame when present so the UI can
            # group/aggregate a wave without a lookup table. (Type guard:
            # test doubles pass MagicMock infos.)
            _ebid = getattr(info, "batch_id", "")
            if isinstance(_ebid, str) and _ebid:
                base["batch_id"] = _ebid
            if etype == "subagent_injection_failed":
                # Show error in UI + queue for LLM context on next turn.
                slot = self.dashboard_state.get_slot(slot_name)
                if slot:
                    task_preview, _ = redact_exfiltration_urls((info.task or "")[:100])
                    task_preview, _ = redact_credentials(task_preview)
                    error_text, _ = redact_exfiltration_urls(extra.get("error", "timed out"))
                    error_text, _ = redact_credentials(error_text)
                    slot.append(
                        "assistant",
                        f"{SUBAGENT_COMPLETION_PREFIX}\n"
                        f"Agent `{info.id}` ❌\n"
                        f"Task: {task_preview}\n\n"
                        f"Error: {error_text}\n"
                        f"⚠️ Result delivery timed out — the subagent finished but "
                        f"its result could not be injected into this session.",
                        "msg msg-a",
                        meta={
                            SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                                agent_id=info.id,
                                outcome=OUTCOME_FAILED,
                                agent_name=info.agent or "",
                                task=task_preview,
                            )
                        },
                    )
                    # Queue failure for LLM context drain
                    failure_msg = extra.get("failure_msg", "")
                    if failure_msg:
                        failure_msg, _ = redact_exfiltration_urls(failure_msg)
                        failure_msg, _ = redact_credentials(failure_msg)
                        slot._pending_subagent_failures.append(failure_msg)
                    self.dashboard_state.push_slots_update()
                    logger.warning(
                        "Injected timeout error for subagent %s into slot %s", info.id, slot_name
                    )
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
            elif etype == "subagent_chunk":
                # Heavy data — only to subscribed clients. At scale (>threshold
                # active agents) the coalescer absorbs it into the ~1s
                # subagent_batch_chunks frame instead of a per-event frame.
                if self._subagent_coalescer().handle(etype, {**base, **extra}):
                    return
                self.dashboard_state.broadcast_ws_subagent_subscribers(etype, {**base, **extra})
            else:
                # Lightweight status events — broadcast to all. High-frequency
                # deltas (tool/stalled/retrying) coalesce at scale into ONE
                # subagent_batch_update frame per tick; lifecycle events
                # (spawn/done/recovering/batch_*) always pass through.
                if self._subagent_coalescer().handle(etype, {**base, **extra}):
                    return
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
                # subagents_running flips truth value exactly at spawn/done —
                # push (debounced) so slots-stream consumers stay live.
                if etype in ("subagent_spawn", "subagent_done"):
                    _schedule_slots_push()

        async def _orphan_notify(parent_session: str, msg: str, meta: dict | None = None) -> bool:
            """Inject an orphan notification into the parent dashboard slot.

            Mirrors the subagent_injection_failed delivery: visible transcript
            card + queue into ``slot._pending_subagent_failures`` so the LLM
            drains it (as a digest with any other pending failures) on its next
            turn. Returns False when the slot no longer exists so the manager
            falls through to the owner-DM path. ``msg`` is redacted by the
            manager before delivery; re-redact defensively anyway.

            ``meta`` carries the structured completion facts (#1792) so the
            orphan row renders as a card without re-parsing its prose header.
            """
            # Deliver to whichever tab shows the parent conversation, including a
            # channel-born one; only a parent with no tab wants the owner DM.
            slot_name = dashboard_slot_key(parent_session)
            if not self.dashboard_state or not slot_name:
                return False
            slot = self.dashboard_state.get_slot(slot_name)
            if not slot:
                return False
            safe_msg, _ = redact_exfiltration_urls(msg)
            safe_msg, _ = redact_credentials(safe_msg)
            slot.append(
                "assistant",
                safe_msg,
                "msg msg-a",
                meta={SUBAGENT_COMPLETION_META_KEY: meta} if meta else None,
            )
            slot._pending_subagent_failures.append(safe_msg)
            self.dashboard_state.push_slots_update()
            logger.info("Orphan notification injected into slot %s", slot_name)
            return True

        async def _orphan_dm(msg: str) -> bool:
            """Owner-DM fallback for orphan notifications (bell + Slack DM)."""
            safe_msg, _ = redact_exfiltration_urls(msg)
            safe_msg, _ = redact_credentials(safe_msg)
            delivered = False
            if self.dashboard_state:
                try:
                    self.dashboard_state.notify(
                        "subagent", "Sub-agent orphaned by restart", safe_msg
                    )
                    delivered = True
                except Exception:
                    logger.debug("Orphan bell notification failed", exc_info=True)
            try:
                if self.slack and self._owner_id:
                    ch = await self.slack.open_dm(self._owner_id)
                    if ch:
                        await self.slack.post_message(ch, safe_msg)
                        delivered = True
            except Exception as exc:
                logger.warning("Failed to send orphan notification to Slack DM: %s", exc)
            return delivered

        self.subagent_mgr = SubagentManager(
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
            on_done=_subagent_done,
            max_concurrent=resolve_max_subagents(self._cfg),
            default_turn_limit=self._cfg.agent.subagent_max_turns,
            default_timeout=self._cfg.agent.subagent_timeout_secs,
            stall_idle_secs=self._cfg.agent.subagent_stall_idle_secs,
            on_tool_approval=_approve_subagent,
            on_spawn_approval=_spawn_approve,
            is_yolo=_is_yolo,
            on_event=_subagent_event,
            on_orphan_notify=_orphan_notify,
            on_orphan_dm=_orphan_dm,
            completion_keep=self._cfg.agent.completion_keep,
            completion_keep_chars=self._cfg.agent.completion_keep_chars,
        )
        self.subagent_mgr.start_reaper()

    def _init_crew(self) -> None:
        """Attach the Crew Mode control plane (engineered pipeline;
        decision-only agent) to dashboard_state so api_chat can route
        crew-slot messages to it. MUST run after _init_dashboard() —
        dashboard_state is None until then (GPT review finding on
        faf5a127: attaching from _init_subagents silently skipped crew
        setup in every real gateway boot)."""
        if self.dashboard_state is None:
            return
        try:
            # Deferred import: `gateway` is on the boot path and this subsystem is
            # dashboard-only, so `--no-dashboard` must not pay for it. This method
            # is already dashboard-gated by the return above.
            from kiro_crew.crew_chat import CrewOrchestrator

            self.dashboard_state.crew = CrewOrchestrator(
                state=self.dashboard_state,
                sessions=self.sessions,
                subagents=self.subagent_mgr,
                cfg=self._cfg,
            )
            # Attaching is not resuming. Without this, a request acknowledged
            # before a restart stayed pending with nothing scheduled to act on
            # it — the user saw the ack and then silence forever.
            self.dashboard_state.crew.resume_persisted_slots()
        except Exception:
            logger.warning("CrewOrchestrator init failed — crew mode disabled", exc_info=True)

    def _init_task_runner(self) -> None:
        """Initialize the task runner."""

        async def _task_notify(title: str, body: str, task_id: str = "") -> None:
            if self.dashboard_state:
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)
                title, _ = redact_exfiltration_urls(title)
                title, _ = redact_credentials(title)
                meta = {"task_id": task_id} if task_id else None
                self.dashboard_state.notify("taskrunner", title, body, meta=meta)
                self.dashboard_state.push_refresh("taskrunner")
            # Send approval-related notifications to Slack DM so user knows even when away.
            # Match on specific title patterns from task_executor, not broad keywords
            # (avoids false positives like "Investigating gateway error").
            if "requires approval" in title.lower() or "denied" in title.lower():
                try:
                    if self.slack and self._owner_id:
                        safe_t = redact_credentials(redact_exfiltration_urls(title)[0])[0]
                        safe_b = redact_credentials(redact_exfiltration_urls(body)[0])[0]
                        ch = await self.slack.open_dm(self._owner_id)
                        if ch:
                            await self.slack.post_message(ch, f"*{safe_t}*\n{safe_b}")
                except Exception as exc:
                    logger.warning("Failed to send approval notification to Slack DM: %s", exc)

        assert self.sessions is not None
        self.task_runner = TaskRunner(
            sessions=self.sessions,
            context_builder=self.ctx_builder,
            on_notify=_task_notify,
            work_dir=_session_work_dir("taskrunner:main"),
            conversation_log=self.conv_log,
            consolidator=self.consolidator,
            lesson_store=LessonStore(),
            max_parallel_steps=self._cfg.taskrunner.max_parallel_steps,
            workspace_dir=self._cfg.taskrunner.workspace_dir,
        )
        self.task_runner._on_tool_approval = self._interactive_approval("taskrunner")

        # Task-level approval handler: blocks until user approves via dashboard UI
        async def _task_approval(task: "Task") -> bool:
            if not self.dashboard_state:
                logger.warning("No dashboard state — denying task %d approval", task.index)
                sel().log_api_access(
                    caller="taskrunner",
                    operation="task.force_approval",
                    outcome="denied",
                    source="gateway",
                    resources=f"task-{task.index}",
                    error="no dashboard state available",
                )
                return False
            clean_title, _ = redact_exfiltration_urls(task.title or "")
            clean_title, _ = redact_credentials(clean_title)
            approval_id = f"task-gate-{task.index}-{uuid.uuid4().hex[:8]}"
            result = await self.dashboard_state.request_approval(
                approval_id=approval_id,
                source="taskrunner",
                tool=f"Task {task.index}: {clean_title}",
                tool_purpose="Task requires manual approval before execution",
            )
            sel().log_api_access(
                caller="taskrunner",
                operation="task.force_approval",
                outcome="approved" if result else "denied",
                source="dashboard",
                resources=f"task-{task.index}",
            )
            return result

        self.task_runner._on_approval = _task_approval

    async def _init_dashboard(self) -> None:
        """Start the dashboard web server."""
        assert self.sessions is not None
        assert self.cron_svc is not None

        configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
        # --port override (literal int or "auto" for ephemeral)
        if self._port_override == "auto":
            dashboard_port = 0
        elif self._port_override is not None:
            dashboard_port = int(self._port_override)
        self._dashboard_port = dashboard_port
        self._configured_host = configured_host
        self._local_only = is_local_only(configured_host, self._slack_enabled)
        self._dashboard_runner, self.dashboard_state = await start_dashboard(
            sessions=self.sessions,
            crons=self.cron_svc,
            lessons=LessonStore(),
            port=dashboard_port,
            subagents=self.subagent_mgr,
            context_builder=self.ctx_builder,
            conversation_log=self.conv_log,
            consolidator=self.consolidator,
            task_runner=self.task_runner,
            slack_connected=self._slack_enabled,
            local_only=self._local_only,
            configured_host=configured_host,
            dashboard_url=self._cfg.dashboard.url,
            slack_client=self.slack,
            owner_id=self._owner_id,
            assume_kiro_ready=self._test_mode,
        )
        # When --port auto was requested, read the OS-assigned ephemeral port
        # back from the runner so subsequent URL building and the READY line
        # use the real bound port.
        if dashboard_port == 0 and self._dashboard_runner is not None:
            addresses = self._dashboard_runner.addresses
            if addresses:
                self._dashboard_port = addresses[0][1]
        if self.slack and self.dashboard_state:
            self.dashboard_state.slack_client = self.slack
        if self.dashboard_state:
            self.dashboard_state.no_crons = self._no_crons  # dashboard mode

    async def _init_api_server(self) -> None:
        """Start a minimal API-only HTTP server for MCP tool transport."""
        from kiro_crew.dashboard import start_api_server

        assert self.sessions is not None
        assert self.cron_svc is not None
        configured_host, dashboard_port = parse_dashboard_url(self._cfg.dashboard.url)
        # --port override (literal int or "auto" for ephemeral)
        if self._port_override == "auto":
            dashboard_port = 0
        elif self._port_override is not None:
            dashboard_port = int(self._port_override)
        self._dashboard_port = dashboard_port
        self._configured_host = configured_host
        self._local_only = is_local_only(configured_host, self._slack_enabled)
        self._dashboard_runner, self.dashboard_state = await start_api_server(
            sessions=self.sessions,
            crons=self.cron_svc,
            lessons=LessonStore(),
            port=dashboard_port,
            subagents=self.subagent_mgr,
            task_runner=self.task_runner,
            slack_client=self.slack,
            owner_id=self._owner_id,
            local_only=self._local_only,
            configured_host=configured_host,
            assume_kiro_ready=self._test_mode,
            conversation_log=self.conv_log,
        )
        if dashboard_port == 0 and self._dashboard_runner is not None:
            addresses = self._dashboard_runner.addresses
            if addresses:
                self._dashboard_port = addresses[0][1]
        if self.dashboard_state:
            self.dashboard_state.no_crons = self._no_crons  # API-only mode

    async def _start_embeddings(self) -> None:
        """Wire in-process embeddings and kick background model download.

        The embed_fn_factory is wired unconditionally so that _try_embed()
        lazily rebinds embed_fn once the model file lands — no gateway
        restart required. If the model is already present (common case after
        first boot), embed_fn is bound immediately.
        """
        self.vector_memory.embed_fn_factory = make_sync_embed_fn
        if model_file_present():
            self.vector_memory.embed_fn = make_sync_embed_fn()
            logger.info("In-process embeddings ready (model already present)")
        elif embedding_model_is_custom():
            # No download will fix this — the operator has to correct the path.
            # resolve_custom_model() already logged the specific reason.
            logger.warning(
                "Custom embedding model is not usable — memory falls back to keyword "
                "search. Run 'kirocrew doctor' for the reason."
            )
        else:
            logger.info(
                "Embedding model not yet present — downloading in background; "
                "memory falls back to keyword search until ready"
            )
        self._model_download_task = start_background_model_download()

    async def _auto_migrate_memory(self) -> None:
        """Migrate legacy markdown memory into the vector store, then backfill.

        Runs once at boot as a fire-and-forget background task. Two idempotent
        phases, all blocking work offloaded to the maintenance executor so the
        event loop is never stalled:

          1. Migrate (gated on ``memory.migrated`` being False): parse legacy
             markdown/lessons via ``migrate_from_markdown``, flip
             ``memory.migrated`` to True (even for a fresh install with zero
             legacy entries, so everyone lands in vector-only mode), sync the
             live consolidator, and acknowledge via an audit event + log line.
          2. Re-embed sweep (independent of phase 1): embed any episodic rows
             written without a vector (migrated before the model landed) and
             rebuild the FAISS index. Self-healing across boots. Gated on a cheap
             non-loading probe FIRST — nothing pending and a current vector space
             means the sweep returns without loading the embedding model at all,
             so a steady-state boot never pays its ~1GB RSS. Only once there is
             work does it wait on model readiness.

        Never raises: any failure is logged and leaves ``migrated`` unchanged so
        the next boot retries. Boot survives regardless.
        """
        from kiro_crew.memory import legacy_memory_present

        # Every dereference lives inside the try so the "never raises" contract
        # above holds even on a boot where ``_init_services`` never ran (or was
        # stubbed): this is a fire-and-forget task, so an escaping exception is
        # only surfaced later as an unretrieved-task error, far from its cause.
        loop = asyncio.get_running_loop()
        try:
            store = getattr(self, "vector_memory", None)
            if store is None:
                logger.debug("auto-migrate skipped: vector memory not initialised")
                return
            # Reconcile BEFORE phase 1 when the backend is ALREADY usable. A ready
            # backend makes migration write real vectors, and write_episodic's
            # FAISS dedup search would then query an index built at the previous
            # model's dimensionality — faiss raises on the mismatch, which aborts
            # migration AND phase 2, so the store would never reconcile, on every
            # boot. No waiting here on purpose: when the backend is NOT ready,
            # migration writes NULL vectors and skips the FAISS search entirely,
            # so there is nothing to reconcile ahead of, and waiting would delay
            # first-boot migration behind the model download.
            if get_shared_embedder().is_ready():
                await loop.run_in_executor(
                    maintenance_executor(), reconcile_store_embedding_space, store
                )
            # ── Phase 1: migrate ──
            if not self._cfg.memory.migrated:
                # Bind embed_fn so migration writes real vectors when the model
                # is already present; otherwise rows are written NULL and the
                # sweep below (or a later boot) backfills them.
                if store.embed_fn is None and model_file_present():
                    store.embed_fn = make_sync_embed_fn()

                had_legacy = await loop.run_in_executor(
                    maintenance_executor(), legacy_memory_present
                )
                counts = {"semantic": 0, "episodic": 0, "skipped": 0}
                if had_legacy:
                    counts = await loop.run_in_executor(
                        maintenance_executor(), store.migrate_from_markdown
                    )
                # Flip the flag for everyone (fresh installs included) so the
                # app enters vector-only mode and stops writing markdown.
                await self._set_memory_migrated(True)
                self._cfg.memory.migrated = True
                if self.consolidator is not None:
                    self.consolidator._migrated = True
                summary = (
                    f"semantic={counts['semantic']} episodic={counts['episodic']} "
                    f"skipped={counts['skipped']}"
                )
                try:
                    store._log_event("migration", "system", "auto_migrate", None, summary, "auto")
                except Exception:
                    logger.debug("auto-migrate audit log failed", exc_info=True)
                logger.info("Auto-migrated legacy memory: %s", summary)

            # ── Phase 2: re-embed sweep ──
            # Wait (non-blocking to boot — we are our own task) for the model, so
            # rows written NULL during phase 1 get vectors.
            if not model_file_present() and self._model_download_task is not None:
                try:
                    await self._model_download_task
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("model download task errored", exc_info=True)
            # Gate on EMBEDDER READINESS, not on the bundled GGUF being on disk.
            # model_file_present() is a proxy that only means anything for the
            # file-backed llama.cpp backend: a backend installed via
            # register_embedding_backend() (remote endpoint, ONNX, ...) can be
            # ready with no local file at all, and gating on the file left it
            # outside this block entirely — so its foreign vectors were never
            # reconciled. Readiness is the property actually required here.

            def _wait_then_backfill() -> int:
                # Probe for work BEFORE touching the embedder. wait_ready() below
                # calls _kick_background_load(), which mmaps the ~700MB GGUF and
                # allocates its KV/compute buffers — the single largest chunk of
                # gateway RSS. A boot with nothing to embed used to pay all of it
                # for a sweep that then embedded zero rows. Both probes here are
                # non-loading: has_pending_embeddings() is three LIMIT-1 SELECTs,
                # and store_embedding_space_is_stale() compares signatures built
                # from model_id/dim, which are set when the backend is CONSTRUCTED.
                # Deliberately NOT reconcile_store_embedding_space(): that one is
                # destructive and refuses to clear against an unready backend, so
                # it is the wrong tool for a question asked before the load.
                has_pending = getattr(store, "has_pending_embeddings", None)
                # A store without the probe (a stub, a foreign implementation)
                # keeps the old behaviour rather than silently losing its sweep.
                pending = has_pending() if callable(has_pending) else True
                if not pending and not store_embedding_space_is_stale(store):
                    logger.debug(
                        "Re-embed sweep: no rows pending and the stored vector space "
                        "is current — leaving the embedding model unloaded"
                    )
                    return 0
                embedder = get_shared_embedder()
                # wait_ready() is on the llama.cpp backend but not the
                # EmbeddingBackend ABC (a swapped-in backend may not support
                # blocking-wait); fall back to is_ready() when absent.
                wait_ready = getattr(embedder, "wait_ready", None)
                ready = wait_ready(timeout=120) if callable(wait_ready) else embedder.is_ready()
                if not ready:
                    logger.info(
                        "Embedding model not ready within timeout; deferring "
                        "re-embed sweep to a later boot"
                    )
                    return 0
                if store.embed_fn is None:
                    store.embed_fn = make_sync_embed_fn()
                # Reconcile BEFORE the sweep: a model change clears stale vectors
                # to NULL and the same sweep re-embeds them in one pass. Routed
                # through the shared chokepoint so every process that opens a
                # store reconciles identically (see reconcile_store_embedding_space).
                reconcile_store_embedding_space(store)
                return store.backfill_missing_embeddings()

            await loop.run_in_executor(maintenance_executor(), _wait_then_backfill)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Auto-migration failed; will retry next boot", exc_info=True)

    async def _set_memory_migrated(self, value: bool) -> None:
        """Persist ``memory.migrated`` to config.json (config-lock guarded)."""
        from kiro_crew.dashboard.handlers.memory import _set_migrated

        await _set_migrated(value)

    # ------------------------------------------------------------------
    # MCP Gateway
    # ------------------------------------------------------------------

    async def _init_mcp_gateway(self, stub_servers: frozenset[str] | None = None) -> None:
        """Start the MCP gateway sidecar and populate the agent-JSON overlay.

        Runs iff at least one server gets a stub
        (``mcp_gateway.stub_servers``). Routing is what interposes a stub, and
        the stub is what carries both the render/callback path and any sharing —
        so nothing stubbed means there is nothing for a broker to serve. Sharing
        (``mcp_gateway.enabled``) is deliberately NOT part of this condition: it
        decides how a stubbed server's backend is acquired, and on its own routes
        nothing. Any failure downgrades to today's per-session MCP path — the
        stub's graceful fallback keeps kiro-cli sessions working even when the
        broker is unreachable.

        ``stub_servers`` overrides the configured set. A caller restarting the
        broker for an unrelated reason passes the set already being served, so a
        stub change recorded for the next gateway start is not applied early as a
        side effect of that unrelated restart.
        """
        cfg_gw = self._cfg.mcp_gateway
        stubs = frozenset(cfg_gw.stub_servers) if stub_servers is None else stub_servers
        if not stubs:
            return
        self._mcp_stub_servers_started = stubs
        # Runs on every platform the transport layer covers -- an AF_UNIX socket
        # on POSIX, a named pipe on Windows. Stub delivery is ACP session/new
        # injection, not a bind-mount, so no mount namespace is needed anywhere.
        if not is_gateway_supported():
            return

        overlay_dir = resolve_overlay_dir(cfg_gw.overlay_dir)
        socket_path = Path(cfg_gw.socket_path) if cfg_gw.socket_path else default_socket_path()
        agents_source_dir = kiro_agents_dir()
        workspace_default = _session_work_dir(None)

        try:
            # rewrite_agents() walks ~/.kiro/agents, parses every JSON spec and
            # rewrites the overlay — pure-sync file I/O.  Offload to the bounded
            # maintenance pool so it can't block the event loop when triggered
            # post-startup.
            _rewrite_result, target_env = await asyncio.get_running_loop().run_in_executor(
                maintenance_executor(),
                functools.partial(
                    rewrite_agents,
                    source_dir=agents_source_dir,
                    overlay_dir=overlay_dir,
                    socket_path=socket_path,
                    work_dir=workspace_default,
                    sandbox_mode=self._cfg.agent.sandbox,
                    approval_mode=self._cfg.agent.approval_mode,
                    stub_servers=stubs,
                    pooling_enabled=cfg_gw.enabled,
                ),
            )
        except Exception:
            logger.exception("mcp-gateway rewriter failed — falling back")
            return

        manager = GatewayManager(
            GatewaySpec(
                socket_path=socket_path,
                idle_timeout_secs=cfg_gw.idle_timeout_secs,
                max_backends=cfg_gw.max_backends,
                mcp_target_env=target_env,
                prewarm_count=cfg_gw.prewarm_count,
            )
        )
        # Pre-resolve npm-launcher targets in the background. An npx spec asks the
        # registry what it means on every launch; once resolved, the daemon execs
        # the installed tree instead, so session start does no resolution and
        # needs no network. Fired detached and never awaited: a launch that beats
        # the prefetch just uses today's path, so blocking startup on installs
        # would trade the stall we are removing for one at boot.
        self._mcp_target_env = dict(target_env)
        self._mcp_resolve_prefetch = asyncio.create_task(
            self._mcp_resolve_prefetch_loop(dict(target_env))
        )
        if await manager.start():
            self._mcp_gateway_manager = manager
            # Report the stub set and the sharing decision. There is one
            # trigger now (something is stubbed), so the useful line is WHAT it
            # serves: "N routed" beside a live daemon explains itself, and the
            # sharing suffix stops "sharing: off" next to a running broker from
            # reading as a contradiction.
            #
            # Counts ``stubs``, not the configured list. The two differ whenever a
            # stub change is recorded for the next gateway start, and this line is
            # read during exactly that diagnosis ("why is my stub not live?") --
            # reporting the configured count there would answer it wrongly.
            logger.info(
                "mcp-gateway: broker ready (socket=%s) for %d stubbed server(s), "
                "backend sharing %s",
                socket_path,
                len(stubs),
                "on" if cfg_gw.enabled else "off",
            )

    #: Floor on the gap between timed pre-resolve passes. ``refresh_hours = 0``
    #: legitimately means "always stale" for the freshness check, but it must not
    #: turn the timer into a spin loop that reinstalls continuously.
    _MCP_RESOLVE_MIN_SLEEP_SECS = 300.0

    async def _mcp_resolve_prefetch_loop(self, target_env: dict[str, str]) -> None:
        """Run the pre-resolve pass on the configured cadence until cancelled.

        A single startup pass is not enough to make
        ``resolve_once_refresh_hours`` mean what it says. Staleness is consulted
        only when a pass runs, and ``resolved_launch`` ignores it by design, so
        on a gateway that stays up for weeks -- the normal case -- an unpinned
        ``@latest`` spec would freeze at whatever it resolved to on boot and
        silently stop picking up upstream fixes. The window needs something to
        tick it.

        Sleeps for the refresh window itself between passes: each pass already
        skips anything not yet stale, so the cadence only has to be fine enough
        that "expires after N hours" is honoured within about one window.

        Never returns normally -- ``_stop_mcp_broker`` cancels it, which is the
        only exit. The window is re-read every iteration so a config reload takes
        effect on the next pass without a broker restart.
        """
        while True:
            await self._prefetch_mcp_resolutions(target_env)
            window = float(self._cfg.mcp_gateway.resolve_once_refresh_hours) * 3600.0
            await asyncio.sleep(max(window, self._MCP_RESOLVE_MIN_SLEEP_SECS))

    async def _prefetch_mcp_resolutions(
        self, target_env: dict[str, str], *, force: bool = False
    ) -> dict[str, str]:
        """Pre-resolve npm-launcher MCP targets so launches skip dependency resolution.

        ``force`` bypasses the freshness window -- that is the operator asking to
        go to the registry now, rather than asking whether it is time to.

        Errors are logged and swallowed: a resolution that does not land leaves
        the server launching exactly the way it does today.
        """

        refresh_secs = float(self._cfg.mcp_gateway.resolve_once_refresh_hours) * 3600.0
        try:
            outcomes = await resolve_prefetch(
                self._mcp_resolve_home, target_env, refresh_secs=refresh_secs, force=force
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("mcp-gateway: pre-resolve pass failed")
            return {}
        ready = sorted(pkg for pkg, state in outcomes.items() if state == "ready")
        if ready:
            logger.info(
                "mcp-gateway: %d npm target(s) pre-resolved; their launches now "
                "skip dependency resolution (%s)",
                len(ready),
                ", ".join(ready),
            )
        return outcomes

    async def _refresh_mcp_resolutions(self) -> dict:
        """Dashboard callback: re-resolve every npm-launcher MCP target now.

        This is the explicit half of the freshness policy. The timed pass asks
        "is it time to check upstream?"; pressing this says "check upstream",
        so it forces past the window even for a pinned spec.

        Awaited rather than detached, because the caller is a person waiting for
        an answer -- unlike the startup pass, whose whole point is not to block.
        Reports which packages are now ready so the UI can say what happened
        instead of only that something was attempted.
        """

        self._cfg = KiroCrewConfig.load()
        target_env = dict(self._mcp_target_env)
        if not target_env:
            # No broker start has computed a target set, so there is nothing this
            # could refresh. Say so rather than reporting an empty success.
            return {"ok": False, "reason": "no_targets", "resolved": {}}
        outcomes = await self._prefetch_mcp_resolutions(target_env, force=True)
        return {
            "ok": True,
            "resolved": outcomes,
            "ready": sorted(pkg for pkg, state in outcomes.items() if state == "ready"),
        }

    async def _stop_mcp_broker(self) -> None:
        """Stop the MCP gateway broker if running and clear the handle."""
        task = self._mcp_resolve_prefetch
        self._mcp_resolve_prefetch = None
        if task is not None and not task.done():
            # An install in flight has nothing left to serve once the broker is
            # gone, and leaving it running would race the next pass.
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        mgr = self._mcp_gateway_manager
        self._mcp_gateway_manager = None
        if mgr is not None:
            try:
                await mgr.shutdown()
            except Exception:
                logger.exception("mcp-gateway: broker shutdown failed")

    async def _apply_mcp_gateway_enabled(self, enabled: bool) -> dict:
        """Dashboard callback: apply the persisted ``mcp_gateway.enabled``
        flag in-process (start/stop the broker), no gateway restart.

        Reloads config so it acts on the value the handler just wrote.
        Returns ``{enabled, running, ping_ok}``.

        The flag governs backend SHARING, not the broker's existence: a routed
        server needs its stub either way. So turning sharing off restarts the
        broker rather than stopping it whenever something is still routed — a
        plain stop would take away the render and callback paths of servers the
        operator never unstubbed. The restart is required, not incidental: the
        rewriter reads the sharing flag when the broker starts, so re-running it
        is what re-emits every stub WITHOUT ``--poolable`` and actually stops the
        sharing the operator just turned off.
        The restart re-emits the stub set the broker is ALREADY serving, not the
        configured one. A stub change is recorded for the next gateway start, so
        consuming it here would apply it early as a side effect of an unrelated
        sharing edit -- the operator was told that change is waiting, and the
        broker cycle that carried it would also cancel the in-flight tool calls
        of every session attached to the old daemon.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        self._cfg = KiroCrewConfig.load()
        serving = self._mcp_stub_servers_started
        if self._mcp_gateway_manager is not None:
            await self._stop_mcp_broker()
        if serving:
            await self._init_mcp_gateway(stub_servers=serving)
        mgr = self._mcp_gateway_manager
        if self.dashboard_state is not None:
            self.dashboard_state._mcp_gateway_manager = mgr
        # Rebuild the provider factory so new sessions resolve the overlay
        # path from the CURRENT config, not the value captured at boot.
        # refresh_defaults() rebuilds the factory and drains the warm pool
        # without killing live sessions — the correct semantics since a
        # running session has already sent session/new and cannot be
        # retrofitted.
        #
        # Skipped while the configured set disagrees with what is being served,
        # because the factory's overlay decision is all-or-nothing on the
        # CONFIGURED list: ``config.loader`` passes ``mcp_gateway_overlay`` only
        # ``if _gw.stub_servers`` and otherwise passes None, which drops the
        # gateway out of the path entirely. So refreshing right after the last
        # server was unstubbed would hand new sessions no overlay at all -- they
        # would bypass the broker that is still serving that stub, which is the
        # pending change taking effect early on an unrelated sharing edit. When
        # the two agree the refresh is a no-op for the overlay, so the guard only
        # ever suppresses the disagreeing case.
        if self.sessions is not None and frozenset(self._cfg.mcp_gateway.stub_servers) == serving:
            await self.sessions.refresh_defaults()
        if mgr is None:
            return {"enabled": enabled, "running": False, "ping_ok": False}
        running = bool(mgr.is_running)
        ping_ok = bool(running and await mgr.ping())
        return {"enabled": enabled, "running": running, "ping_ok": ping_ok}

    async def _apply_mcp_stub(self) -> dict:
        """Dashboard callback: record a stub change for the NEXT gateway start.

        Deliberately leaves the running broker alone, because there is nothing
        useful to do to it. A session's MCP toolset is fixed at ``session/new``,
        so no running session can adopt a new stub set however the broker is
        cycled; the change only ever matters to sessions created later, and the
        next start builds their routing from this config.

        Restarting to shorten that wait actively destroys work. The drain gives
        in-flight tool calls ``DRAIN_SECS`` to finish and then cancels them, and
        the stub does not re-handshake afterwards -- ``handshake()`` has a single
        call site at startup, and the post-``initialize`` path deliberately does
        not fall back to a per-session exec (kiro-cli never re-sends
        ``initialize``, so an exec'd server would reject every later call). An
        attached session therefore loses those servers for the rest of its life.

        Rewriting the agent specs without restarting is worse still: a new
        session would route a server through the stub while the running daemon
        has no target for it, and an unknown target is a TERMINAL rejection in
        ``stub.py``, not a fallback. The spec rewrite and the daemon's routing
        environment are built together at startup and must stay that way, so
        this callback persists intent only and reports that a restart is needed.
        """
        from kiro_crew.config.loader import KiroCrewConfig

        self._cfg = KiroCrewConfig.load()
        return {
            "applied": False,
            "restart_required": True,
            "stub_servers": sorted(self._cfg.mcp_gateway.stub_servers),
        }

    def _wire_mcp_gateway_dashboard(self) -> None:
        """Publish the broker + apply callbacks onto DashboardState.

        _init_mcp_gateway runs at boot before dashboard_state exists, so
        the manager and the enable/poolable callbacks are attached here
        (post dashboard init). The /api/mcp-gateway/* handlers read these
        off ``request.app['state']``.
        """
        if self.dashboard_state is None:
            return
        self.dashboard_state._mcp_gateway_manager = self._mcp_gateway_manager
        self.dashboard_state._mcp_gateway_apply = self._apply_mcp_gateway_enabled
        self.dashboard_state._mcp_gateway_apply_stub = self._apply_mcp_stub
        self.dashboard_state._mcp_resolve_refresh = self._refresh_mcp_resolutions

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Graceful cleanup of all services."""
        # Disarm the loop-stall watchdog FIRST, before any of the teardown below.
        # close_all()/cancel_all() deliberately kill every kiro-cli child, which
        # is exactly the os.waitpid reaping burst that can wedge the loop for
        # >exit_after seconds. If the armed faulthandler dump-then-exit timer is
        # still live, that wedge would _exit(1) the process mid-shutdown — a clean
        # quit would look like a crash. The watchdog's own on_cleanup hook only
        # runs inside _dashboard_runner.cleanup(), which is gathered concurrently
        # with the reaping burst (too late), so we stop it explicitly here and
        # cancel the heartbeat that keeps re-arming it.
        if self.dashboard_state:
            wd = getattr(self.dashboard_state, "_loop_watchdog", None)
            if wd is not None:
                wd.stop()
            hb = getattr(self.dashboard_state, "_loop_heartbeat", None)
            if hb is not None:
                hb.cancel()

        # Save all active chat slots to history before shutdown
        if self.dashboard_state:
            from kiro_crew.dashboard.chat import save_all_slots_to_history

            # save_all_slots_to_history does synchronous per-slot file I/O that
            # takes the per-session cross-process lock; on the event loop a
            # contended session would raise HistoryLockTimeout (and a wedged
            # disk would block the loop). Offload to the bounded
            # subprocess_executor with a deadline so a slot's final save is
            # attempted off-loop and cannot stall the shutdown path.
            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(),
                        save_all_slots_to_history,
                        self.dashboard_state,
                    ),
                    timeout=5.0,
                )
            except Exception:
                logger.debug("Dashboard slot save before shutdown failed", exc_info=True)
            self.dashboard_state.file_indexes.stop_all()

        # Cancel in-flight handler tasks
        for t in list(self._handler_tasks):
            t.cancel()
        if self._handler_tasks:
            await asyncio.gather(*self._handler_tasks, return_exceptions=True)

        # Stop services
        if self.cron_svc:
            await self.cron_svc.stop()
        if self.heartbeat_svc:
            self.heartbeat_svc.stop()

        # Stop the pooled MCP gateway broker + its backends. gatewayd is
        # spawned with start_new_session (and no PR_SET_PDEATHSIG), so on a
        # clean KiroCrew exit it and its pooled MCP subprocesses would
        # otherwise leak orphaned until the next start's flock adoption.
        await self._stop_mcp_broker()

        # Kill all ACP processes and close connections
        cleanup_tasks: list = []
        if self.subagent_mgr:
            cleanup_tasks.append(self.subagent_mgr.cancel_all())
        if self.sessions:
            cleanup_tasks.append(self.sessions.close_all())
        if self._dashboard_runner:
            # Close WS connections first so handlers exit promptly
            if self.dashboard_state:
                await self.dashboard_state.close_all_ws()
            cleanup_tasks.append(self._dashboard_runner.cleanup())
        if self._socket_client:
            cleanup_tasks.append(asyncio.wait_for(self._socket_client.close(), timeout=1.0))
        cleanup_tasks.extend(registry.shutdown_tasks(self._channel_handles, timeout=2.0))
        # Cancel background model download if still in flight
        if self._model_download_task is not None and not self._model_download_task.done():
            self._model_download_task.cancel()
        # Cancel background auto-migration if still in flight
        if self._auto_migrate_task is not None and not self._auto_migrate_task.done():
            self._auto_migrate_task.cancel()
        # Cancel the boot update check if still in flight — its git subprocesses
        # can take ~70s to time out and nothing downstream needs the result.
        if self._update_check_task is not None and not self._update_check_task.done():
            self._update_check_task.cancel()

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    async def _check_for_updates(self) -> None:
        """Blocking update check — auto-applies if enabled, otherwise notifies.

        Delegates to the resolved :class:`~kiro_crew.platform.update_provider.CommandProvider`
        when security_policy.json's ``updates`` block defines the update commands
        (the enterprise escape hatch). When no policy-defined provider is active,
        ``resolve_provider`` returns ``None`` and we fall through to the existing
        layout-aware logic (backward compatible: no policy = existing behavior).
        """
        provider = None
        try:
            from kiro_crew.platform.update_provider import resolve_provider

            provider = await asyncio.get_running_loop().run_in_executor(None, resolve_provider)
        except Exception:
            # ONLY resolution is tolerated here. If reading the policy fails we
            # cannot know an operator selected a provider, so the built-in
            # behaviour is the honest default.
            logger.debug("Provider resolution failed, using legacy path", exc_info=True)

        # A policy-defined provider (enterprise escape hatch) OWNS the update from
        # here on. Its failures must NOT fall through to the legacy updater: doing
        # so would run the built-in git/CDN update on a host whose administrator
        # selected a different package manager, which is the bypass this seam
        # exists to prevent. `_check_for_updates_via_provider` reports its own
        # failures and leaves the install alone.
        if provider is not None:
            try:
                await self._check_for_updates_via_provider(provider)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Contained, not swallowed and not retried elsewhere: the update
                # check runs on the gateway's boot path, so an exception must not
                # escape into it, and it must not reach the legacy updater either
                # (that would run the built-in update the operator excluded).
                logger.exception("Policy-defined update provider failed")
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed",
                        "Update provider failed — see logs; run manually: kirocrew update",
                    )
            return

        # Legacy path: existing behavior for builtin/git auto-detected installs.
        await self._check_for_updates_legacy()

    def _publish_provider_update_state(self, result: object) -> None:
        """Mirror a provider's verdict into the dashboard's authoritative status.

        The SSE snapshot renders the update badge from
        ``dashboard/handlers/updates.py::_update_info["update_available"]``, which
        only the LEGACY check writes. A provider carries its own
        :class:`UpdateCheckResult`, so notifying without this leaves the badge
        reading a stale (usually null) value and the operator never sees that a
        policy-defined update is waiting.

        Written in the capability contract's vocabulary, and ``check_status`` is
        stamped alongside the verdict: under that contract "up to date" means
        ``check_status == "succeeded" and update_available is False``, so writing
        the verdict without the status would leave a provider's real answer
        indistinguishable from a check that never ran.
        """
        from kiro_crew.dashboard.handlers.updates import _update_info

        _update_info["update_available"] = bool(getattr(result, "available", False))
        remote = str(getattr(result, "remote_version", "") or "")
        if remote:
            _update_info["latest_version"] = remote
        _update_info["check_status"] = CHECK_SUCCEEDED

    async def _check_for_updates_via_provider(self, provider: object) -> None:
        """Provider-delegated update check and apply."""
        from kiro_crew import __version__ as _running_version
        from kiro_crew.config import KiroCrewConfig
        from kiro_crew.platform.update_governance import min_version, update_required
        from kiro_crew.platform.update_provider import UpdateProvider

        assert isinstance(provider, UpdateProvider)

        result = await provider.check()

        # The mandatory floor is an enterprise ceiling and is evaluated FIRST,
        # before any check-error early return: a host below min_version must
        # still be updated even when the provider's check could not complete
        # (a timed-out or misconfigured command must not strand the host below
        # the policy floor).
        if update_required(_running_version):
            # Guard against an infinite update→restart loop: only apply when the
            # check found a NEWER build available. If the floor is pinned above
            # the highest installable build (a policy typo, or a floor set ahead
            # of the current release), applying would reinstall the same version,
            # restart, and re-enter this branch forever. When no newer build is
            # available we notify and stop — the git path's no-new-commits early
            # return is the equivalent guard.
            if not result.available:
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s, "
                    "but no newer build is available to apply — notifying, not looping",
                    _running_version,
                    min_version(),
                )
                self._publish_provider_update_state(result)
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
                return
            logger.warning(
                "Version compliance: running %s is below the policy minimum %s — "
                "applying mandatory update via provider (overrides auto_update)",
                _running_version,
                min_version(),
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("pulling", "Applying mandatory update…")
            success = await provider.apply()
            if success:
                await self._restart_after_update()
            else:
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed", "Update apply failed — run manually: kirocrew update"
                    )
            return

        # Below the mandatory floor: a check error is a non-answer, not a
        # verdict — report it and stop rather than treating it as "up to date".
        if result.error:
            logger.info("Update check did not complete (%s)", result.error)
            return

        if result.available:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            if cfg.auto_update:
                logger.info("Auto-update enabled — applying update via provider")
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress("pulling", "Downloading update…")
                success = await provider.apply()
                if success:
                    await self._restart_after_update()
                else:
                    if self.dashboard_state:
                        self.dashboard_state.push_update_progress(
                            "failed", "Update apply failed — run manually: kirocrew update"
                        )
            else:
                self._publish_provider_update_state(result)
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
        else:
            print("👻 Already on latest version")

    async def _restart_after_update(self) -> None:
        """Save state and restart the process after a successful update apply."""
        logger.info("Update applied, restarting gateway")
        if self.dashboard_state:
            self.dashboard_state.push_update_progress("restarting", "Restarting server…")
            from kiro_crew.dashboard.chat import save_all_slots_to_history

            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(),
                        save_all_slots_to_history,
                        self.dashboard_state,
                    ),
                    timeout=5.0,
                )
            except Exception:
                logger.debug(
                    "Dashboard slot save before update restart failed",
                    exc_info=True,
                )
        if self.sessions:
            await self.sessions.close_all()
        os.execv(sys.executable, [sys.executable, "-m", "kiro_crew"] + sys.argv[1:])

    async def _check_for_updates_legacy(self) -> None:
        """Legacy update check — the existing layout-aware logic."""
        try:
            from kiro_crew import __version__ as _running_version
            from kiro_crew.dashboard.handlers import _do_update_check, _update_info

            await _do_update_check()
            # Snapshot: the branches below read several keys with awaits
            # between them, and a dashboard-triggered check running
            # concurrently replaces the cache wholesale.
            info = dict(_update_info)
            from kiro_crew.platform.update_governance import min_version, update_required

            # A policy-pinned minimum version makes the update MANDATORY: it
            # overrides the user's auto_update=False, because user config sits
            # under the enterprise ceiling and an operator opting out must not
            # hold a fleet on a build the policy forbids.
            #
            # Checked BEFORE the `available` branch, and deliberately independent
            # of it: the mandate is about whether THIS host satisfies the floor,
            # not about whether a newer build was advertised. `_auto_apply_update`
            # still applies the source pin and its own no-new-commits early
            # return, so this cannot bypass the ceiling or loop.
            if update_required(_running_version):
                # A mandatory floor is handled by layout, because "apply" means
                # different things per install shape:
                #   * git checkout (`can_apply`) -> git fetch + reset applies.
                #   * wheel/cli.sh (no `can_apply`, but carries an installer
                #     command in `remediation`) -> the installer can apply it, so
                #     a floor does drive it; a floor above the newest build
                #     notifies instead of reinstalling the same bytes forever.
                #   * externally managed (dmg/appimage/deb/rpm/docker: no
                #     `can_apply` and no command) -> its own updater owns this; the
                #     backend must not drive a git reset on a non-git tree nor show
                #     an inapplicable CLI-update badge.
                if info.get("can_apply"):
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s — "
                        "applying a mandatory update (overrides auto_update)",
                        _running_version,
                        min_version(),
                    )
                    await self._auto_apply_update()
                    return
                # A wheel install cannot apply in-process, but the installer can,
                # and a policy floor outranks auto_update. Restricted to the
                # `wheel` stamp: a `source` install carries the same installer
                # command, yet re-running it there builds a SEPARATE managed venv
                # while this interpreter re-execs unchanged — an endless
                # update->restart loop. The stamp is baked at build time, so
                # reading it costs no I/O on the event loop.
                if _remediation_command(info) and distribution() == "wheel":
                    # Only apply when a NEWER build is available; otherwise the
                    # installer reinstalls the same below-floor version and the
                    # execv-restart re-enters this branch forever (the git path's
                    # no-new-commits guard is the equivalent). A floor pinned
                    # above the latest build must notify, not loop.
                    if not info.get("update_available"):
                        logger.warning(
                            "Version compliance: running %s is below the policy minimum %s, "
                            "but no newer build is available — notifying, not looping",
                            _running_version,
                            min_version(),
                        )
                        if self.dashboard_state:
                            self.dashboard_state.push_refresh("update_available")
                        return
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s — "
                        "applying mandatory update via installer (overrides auto_update)",
                        _running_version,
                        min_version(),
                    )
                    await self._auto_apply_wheel_update()
                    return
                # Everything below cannot apply here, so the operator has to act.
                # Two of the three cases light the badge; the third deliberately
                # does not, because a dmg/appimage/deb/rpm/docker install cannot
                # act on a CLI-update badge and its own updater owns the upgrade.
                #
                # Where the badge IS lit, `check_status` and `error_code` are left
                # exactly as the check left them. Stamping them "succeeded" would
                # erase the only evidence that the check path itself is broken,
                # which is worse than a payload carrying two independent facts: an
                # update is mandated (a LOCAL determination against the policy pin,
                # which does not need the feed) and the feed check did not complete.
                if _remediation_command(info):
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but this install (%s) updates by re-running the installer — "
                        "run `kirocrew update`",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                    )
                    _badge = True
                elif info.get("check_status") in ("unchecked", "checking"):
                    # The check no-ops while another one is in flight, so the cache
                    # can hold no verdict here — and with no verdict there is no
                    # `managed_by` either. The baked distribution stamp answers the
                    # one question the badge needs and costs no I/O, so an
                    # externally managed install is not handed a CLI-update badge it
                    # cannot act on.
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, but no "
                        "check has reached a verdict yet — the next cycle decides which surface "
                        "owns the upgrade",
                        _running_version,
                        min_version(),
                    )
                    _badge = distribution() not in EXTERNALLY_MANAGED_STAMPS
                else:
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, but "
                        "this install (%s) is updated by its own updater — not applying from "
                        "the backend",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                    )
                    _badge = False
                if _badge:
                    _update_info["update_available"] = True
                    if self.dashboard_state:
                        self.dashboard_state.push_refresh("update_available")
                return

            if info.get("update_available"):
                logger.info("Updates available from remote")
                from kiro_crew.config import KiroCrewConfig

                cfg = KiroCrewConfig.load()
                # `_auto_apply_update` replaces code with git fetch + reset, so it
                # can only serve a GIT CHECKOUT (`can_apply`). A wheel install
                # replaces itself by re-running the installer, which the branch
                # below drives instead; without that half of the guard the wheel
                # path in `_do_update_check` would drive a git reset in a tree
                # that has no `.git`.
                #
                # `version_newer` is the other half, and it is not redundant:
                # `update_available` is true on commit distance alone, which for a
                # source checkout means any upstream commit — acting on that would
                # `git reset --hard` a developer's tree within 12 hours of one,
                # where before it only happened at a release. Commit distance
                # without a version bump lights the badge below instead, and the
                # dashboard's own apply path (`git pull`, dirty tree refused) is
                # the non-destructive way in.
                if cfg.auto_update and info.get("can_apply") and info.get("version_newer"):
                    logger.info("Auto-update enabled — applying update")
                    await self._auto_apply_update()
                elif cfg.auto_update and _remediation_command(info) and distribution() == "wheel":
                    # Only a managed WHEEL install can be safely self-updated by
                    # re-running the cli.sh installer: it replaces the same venv
                    # the running interpreter lives in. A "source" install (cloud
                    # tarball / EC2) carries the same installer command but the
                    # installer would create a SEPARATE managed venv while this
                    # source interpreter re-execs unchanged — an infinite
                    # update→restart loop. Those notify instead.
                    logger.info("Auto-update enabled for wheel install — running installer")
                    await self._auto_apply_wheel_update()
                else:
                    if cfg.auto_update:
                        logger.warning(
                            "Auto-update is on, but this install (%s) updates by "
                            "re-running the installer, not by git — notifying instead",
                            info.get("managed_by") or "unknown",
                        )
                    if self.dashboard_state:
                        self.dashboard_state.push_refresh("update_available")
            elif info.get("error_code"):
                # A check that could not run is NOT "already on latest" — saying so
                # is the exact false reassurance the honesty pair in
                # `handlers/updates.py` exists to prevent.
                logger.info("Update check did not complete (%s)", info.get("error_code"))
            elif info.get("check_status") == CHECK_SUCCEEDED:
                print("👻 Already on latest version")
            else:
                # DEFERRED (a desktop bundle whose own updater owns this), or a
                # check that never ran. Neither carries an `error_code`, so keying
                # only on that would fall through to the reassurance above and
                # claim a verdict nothing produced.
                logger.info(
                    "No update verdict to report (check_status=%s)",
                    info.get("check_status") or CHECK_UNCHECKED,
                )
        except Exception:
            logger.debug("Update check failed", exc_info=True)

    async def _auto_apply_update(self) -> None:
        """Auto-apply: fetch, reset to remote, rebuild frontend, pip install, restart.

        Uses ``git fetch`` + ``git reset --hard`` instead of ``git pull``
        so local tracked-file edits never cause merge conflicts.
        Untracked files (task specs, notes) are untouched by reset.

        The public OSS flow is the same one used by ``kirocrew update`` and the
        dashboard update endpoint: git reset to origin → build + stage the
        in-tree ``website/`` frontend → ``pip install -e .`` → ``os.execv``
        restart. The optional ``kiro-cli`` backend is updated only when present.
        """
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if not proj:
            return
        try:
            # Detect current branch
            branch_proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            branch_out, _ = await asyncio.wait_for(branch_proc.communicate(), timeout=10)
            if branch_proc.returncode != 0:
                logger.error("Auto-update: could not determine current branch")
                return
            branch = branch_out.strip().decode() if branch_out else ""
            if not branch or branch == "HEAD":
                branch = "mainline"

            # Only auto-update on mainline — beta/feature branches need manual update
            if branch != "mainline":
                logger.debug("Auto-update: skipping — on branch %s, not mainline", branch)
                return

            # Source pin, checked before the fetch. This is the most privileged
            # update path in the product — no auth, no click, `git reset --hard`
            # + pip + execv on boot — so a blocked host must not touch its tree.
            from kiro_crew.platform.update_governance import (
                resolve_remote_url,
                update_blocked_reason,
            )

            blocked = await asyncio.get_running_loop().run_in_executor(
                None, lambda: update_blocked_reason(resolve_remote_url(proj, remote="origin"))
            )
            if blocked:
                logger.warning("Auto-update refused: %s", blocked)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            if self.dashboard_state:
                self.dashboard_state.push_update_progress("pulling", "Fetching latest changes…")

            fetch = await asyncio.create_subprocess_exec(
                "git",
                "fetch",
                "origin",
                branch,
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(fetch.communicate(), timeout=60)

            if fetch.returncode != 0:
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Check if there are actually new commits
            diff_proc = await asyncio.create_subprocess_exec(
                "git",
                "diff",
                "HEAD",
                f"origin/{branch}",
                "--quiet",
                cwd=proj,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(diff_proc.wait(), timeout=10)
            if diff_proc.returncode == 0:
                # No diff — already up to date
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Warn if local tracked-file edits will be discarded
            status_proc = await asyncio.create_subprocess_exec(
                "git",
                "status",
                "--porcelain",
                cwd=proj,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            status_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=10)
            if status_out and status_out.strip():
                tracked = [
                    ln
                    for ln in status_out.decode(errors="replace").splitlines()
                    if not ln.startswith("??")
                ]
                if tracked:
                    logger.warning("Auto-update: discarding local tracked-file changes in %s", proj)

            # Hard reset to remote — discards local tracked-file edits,
            # untracked files (task specs, notes) are preserved.
            reset = await asyncio.create_subprocess_exec(
                "git",
                "reset",
                "--hard",
                f"origin/{branch}",
                cwd=proj,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(reset.wait(), timeout=10)
            if reset.returncode != 0:
                logger.error("Auto-update: git reset --hard failed (rc=%d)", reset.returncode)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return
            logger.info("Auto-update: reset to origin/%s, rebuilding", branch)

            # Update the optional kiro-cli backend if present.
            if shutil.which("kiro-cli"):
                try:
                    kiro_update = await asyncio.create_subprocess_exec(
                        "kiro-cli",
                        "update",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    await asyncio.wait_for(kiro_update.wait(), timeout=120)
                except Exception:
                    logger.debug("Auto-update: kiro-cli update failed (non-fatal)")

            # Build + stage the in-tree website/ frontend so the dashboard
            # serves the latest bundle. Graceful no-op if no website/ or npm.
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("building", "Building frontend…")
            await build_frontend_async(
                proj,
                push_progress=(
                    self.dashboard_state.push_update_progress if self.dashboard_state else None
                ),
            )

            if self.dashboard_state:
                self.dashboard_state.push_update_progress("building", "Rebuilding package…")
            # Install the reset revision's Python deps / entry points. The gateway
            # is normally started through the console script pip would have to
            # rewrite, which Windows locks, so dep_sync picks the reinstall only
            # where it can actually run and substitutes a dependency-only sync
            # where it cannot — a reinstall that dies on the locked script has
            # already deleted the editable .pth.
            pip_messages: list[tuple[str, bool]] = []
            # The bound lives on the pip subprocess (dep_sync's `timeout`), not on
            # an `asyncio.wait_for` around the executor. Cancelling a wait_for does
            # NOT cancel the thread it is waiting on: expiry would report failure
            # while a live pip kept writing to the venv and permanently occupied a
            # subprocess_executor thread. Passing the deadline down means the child
            # is actually killed and the thread is released.
            pip_rc = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                functools.partial(
                    dep_sync.sync_or_reinstall,
                    Path(proj),
                    Path(sys.executable),
                    lambda message, error: pip_messages.append((message, error)),
                    timeout=600,
                ),
            )
            if pip_rc != 0:
                # Same reasoning as the dep-repair path: redact first, cap last.
                err_text = "; ".join(m for m, _ in pip_messages)
                err_text, _ = redact_exfiltration_urls(err_text)
                err_text, _ = redact_credentials(err_text)
                err_text = err_text[:500]
                logger.error(
                    "Auto-update: dependency install failed (rc=%d): %s",
                    pip_rc,
                    err_text,
                )
                if pip_rc == dep_sync.REFUSED:
                    # REFUSED means the sync stopped BEFORE touching the venv --
                    # most importantly when that venv serves a different
                    # checkout. The core-dep repair below would then install into
                    # exactly the venv the guard just protected, so a refusal ends
                    # the auto-update here without even repairing: the messages
                    # above name the remedy, and nothing was changed.
                    if self.dashboard_state:
                        self.dashboard_state.push_update_progress("error", err_text)
                    return
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "building",
                        "Dependency install hit an error — repairing core deps…",
                    )
                # The source tree is already on the new version (git reset ran
                # first), so booting without the core deps crashes every
                # command (e.g. cc_agent's `import yaml`). Install the core
                # public deps directly so the gateway still boots and can
                # self-heal — this can't fully fail the way `pip install -e .`
                # can, because these resolve from public PyPI with no
                # internal-index dependency.
                core_deps = [pip for _mod, pip in self._REQUIRED_DEPS]
                fallback = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    *core_deps,
                    cwd=proj,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                _fb_out, fb_err = await asyncio.wait_for(fallback.communicate(), timeout=300)
                if fallback.returncode == 0:
                    logger.info(
                        "Auto-update: core deps repaired after pip failure (%s)",
                        ", ".join(core_deps),
                    )
                else:
                    logger.error(
                        "Auto-update: core dep repair also failed (rc=%d): %s",
                        fallback.returncode,
                        fb_err.decode(errors="replace")[:300],
                    )
                # Repair or not, do NOT restart after a sync that did not come back
                # clean. The tree is already on the new revision (the reset ran
                # first), and every nonzero result names something the restart
                # cannot fix by itself:
                #
                #   - dependencies still unsatisfied  -> the process this restart
                #     brings up dies at import and takes the running gateway with
                #     it, and the repair only covers the CORE deps, not whatever
                #     the revision actually added.
                #   - console script repointed or removed by the revision -> the
                #     wrapper on disk still dispatches to the old target, and no
                #     dependency install rewrites it. This restart uses
                #     `-m kiro_crew` so it would survive, but the next restart
                #     through the service manager runs `kirocrew` and does not.
                #
                # Staying up on already-imported modules is strictly better than
                # either: the operator keeps a working gateway to finish the
                # install from, and is told so now rather than at the next restart.
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "error",
                        "Update stopped before restart: the dependency sync did "
                        "not complete cleanly. The gateway is still running on the "
                        "previously loaded code — finish the install from a "
                        "terminal, then restart.",
                    )
                return

            logger.info("Auto-update: rebuild complete, restarting")
            # Re-read version from rebuilt package
            importlib.reload(kiro_crew)
            new_ver = kiro_crew.__version__
            print(f"👻 New version {new_ver} available — auto-updating and restarting…")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress("restarting", "Restarting server…")
                from kiro_crew.dashboard.chat import save_all_slots_to_history

                # Offload the synchronous per-slot save (per-session lock + disk
                # I/O) to the bounded subprocess_executor with a deadline so a
                # contended/wedged session can't stall the auto-update restart
                # (mirrors the shutdown save above).
                try:
                    await asyncio.wait_for(
                        asyncio.get_running_loop().run_in_executor(
                            subprocess_executor(),
                            save_all_slots_to_history,
                            self.dashboard_state,
                        ),
                        timeout=5.0,
                    )
                except Exception:
                    logger.debug(
                        "Dashboard slot save before auto-update restart failed",
                        exc_info=True,
                    )
            if self.sessions:
                await self.sessions.close_all()
            # Use -m kiro_crew rather than sys.argv[0] so the restart resolves
            # the freshly reinstalled entry point regardless of how the
            # original process was launched.
            os.execv(sys.executable, [sys.executable, "-m", "kiro_crew"] + sys.argv[1:])
        except Exception:
            logger.warning("Auto-update failed", exc_info=True)
            if self.dashboard_state:
                # Surface the platform-correct manual restart command so a failed
                # auto-restart doesn't leave the user guessing.
                self.dashboard_state.push_update_progress(
                    "failed", f"Restart failed — run: {restart_command_hint()}"
                )

    async def _auto_apply_wheel_update(self) -> None:
        """Auto-apply a wheel/cli.sh update by re-running the signed installer.

        The installer (``cli.sh``) handles the full security chain: RSA-SHA256
        signature verification of the manifest against a pinned public key,
        SHA-256 checksum of the downloaded wheel, and channel assertion. This
        method simply invokes it as a subprocess, then restarts the gateway via
        ``os.execv`` so the new code takes effect.

        Preconditions (checked by the caller):
        * ``auto_update`` is True in config, or a policy floor mandates the update.
        * The capability's ``remediation`` carries the installer command (the feed
          check succeeded and composed it locally from validated inputs).
        * The install is NOT a git checkout (no ``can_apply``) and NOT externally
          managed (not a desktop app or container).

        The command is composed by
        :func:`kiro_crew.platform.update_layout.wheel_update_command` from a
        validated channel name and a scheme-pinned artifact base URL
        (``--proto '=https'``), never from feed data. A successful run replaces the
        venv in-place; a failure leaves the existing install intact (cli.sh writes
        to a temp dir and atomically replaces via ``ln -sf``).
        """
        from kiro_crew.dashboard.handlers import _update_info

        # Read the command through the SAME accessor the caller selected this
        # branch with. Reading a bare `_update_info["update_command"]` here is
        # what made this method a silent no-op: the capability contract carries the
        # command inside `remediation`, so the old key is never populated, the
        # branch was still entered, and a mandated update logged a warning instead
        # of applying.
        update_cmd = _remediation_command(_update_info)
        if not update_cmd:
            logger.warning("Auto-update (wheel): no installer command in the capability")
            return

        # Platform guard: cli.sh is POSIX shell. Windows wheel installs do not
        # exist in practice (install.ps1 makes Windows a thin client to a Linux
        # gateway), but guard anyway.
        if sys.platform == "win32":
            logger.warning("Auto-update (wheel): not supported on Windows")
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return

        # Source pin: the CDN bases that compose the installer command must
        # satisfy the policy source pin, same check the git path applies to
        # its remote. A pinned fleet's wheel installs cannot bypass the ceiling.
        from kiro_crew.platform.update_governance import update_blocked_reason
        from kiro_crew.platform.update_layout import cdn_bases, cdn_bases_are_safe

        # The installer command embeds the CDN bases and is handed to a shell, and
        # KIROCREW_CDN_BASE is operator-set: a metacharacter could close the URL
        # and append a second command, and an http:// override would make the
        # piped installer interceptable. Same gate `kirocrew update` applies.
        if not cdn_bases_are_safe():
            logger.error(
                "Auto-update (wheel): CDN base contains disallowed characters "
                "or is not HTTPS — refusing to run"
            )
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return

        feed_base, artifact_base = cdn_bases()
        blocked = update_blocked_reason(feed_base)
        if not blocked:
            blocked = update_blocked_reason(artifact_base)
        if blocked:
            logger.warning("Auto-update (wheel) refused: %s", blocked)
            if self.dashboard_state:
                self.dashboard_state.push_refresh("update_available")
            return

        if self.dashboard_state:
            self.dashboard_state.push_update_progress("pulling", "Downloading update from CDN…")

        logger.info("Auto-update (wheel): running installer")
        # Resolve sh through the trusted system dirs, not the gateway's PATH
        # (which can lead with an agent-writable venv/bin), so a planted shim
        # cannot hijack the installer spawn. Fail CLOSED if no trusted shell:
        # a bare-name fallback would reopen the very hole this closes.
        from kiro_crew.platform.update_provider import (
            _kill_and_reap,
            _read_bounded_output,
            _trusted_path_env,
        )
        from kiro_crew.platform_compat import trusted_system_bin

        _sh = trusted_system_bin("sh")
        if not _sh:
            logger.error("Auto-update (wheel): no trusted shell found — refusing to run")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed", "No trusted shell — run manually: kirocrew update"
                )
            return
        # Pinning the shell is only half of it: the installer line is
        # ``curl … | sh``, so the child resolves `curl` (and its own inner shell)
        # through the inherited PATH. Narrow the child's PATH to trusted system
        # dirs, and fail CLOSED when there is none rather than handing over an
        # agent-influenceable lookup.
        _env = _trusted_path_env()
        if _env is None:
            logger.error("Auto-update (wheel): no trusted PATH — refusing to run")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed", "No trusted PATH — run manually: kirocrew update"
                )
            return
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                _sh,
                "-c",
                update_cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_env,
                # Root, not the gateway's cwd, which can be an agent-writable
                # checkout a relative command word would resolve inside.
                cwd="/",
                # Own session: the installer line is a pipeline, so the whole
                # tree must be one killable group that cannot signal back into
                # the gateway's own group.
                start_new_session=platform_compat.IS_POSIX,
            )
            # Bounded: the installer's stdout is chatter and only a capped
            # stderr is logged, so a verbose CDN script cannot exhaust the
            # gateway's memory buffering it.
            stdout, stderr = await _read_bounded_output(proc, timeout=300, want_stdout=False)
        except asyncio.CancelledError:
            # Shutdown (SIGTERM) cancels this task. Without this branch the
            # installer keeps mutating the installation after the gateway exits,
            # leaving a half-replaced venv nobody is supervising. Kill the whole
            # TREE (the line is a pipeline) and reap under a bound, then re-raise
            # so cancellation still propagates. ``proc`` is None when the
            # cancellation landed during the spawn itself.
            if proc is not None:
                await _kill_and_reap(proc)
            logger.warning("Auto-update (wheel): cancelled — installer child killed")
            raise
        except asyncio.TimeoutError:
            # Terminate the whole tree and reap under a bound so nothing keeps
            # modifying the installation after we return.
            if proc is not None:
                await _kill_and_reap(proc)
            logger.error("Auto-update (wheel): installer timed out (5 min)")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed", "Installer timed out — run manually: kirocrew update"
                )
            return
        except OSError:
            # OSError, not just FileNotFoundError: fd or process exhaustion
            # raises a different OSError, and this runs on the boot path.
            logger.exception("Auto-update (wheel): could not start the installer")
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed", "'sh' not available — run manually: kirocrew update"
                )
            return

        if proc.returncode != 0:
            from kiro_crew.security import redact_credentials, redact_exfiltration_urls

            # Redact BEFORE truncating. Slicing first can cut a credential in
            # half, and half a token no longer matches the redactors' patterns
            # (an AWS key needs its full 20 chars to match), so the surviving
            # fragment would reach gateway.log and /api/logs verbatim. The
            # 500-char cap is for log volume, so it belongs last.
            err_text = (stderr or b"").decode(errors="replace")
            err_text, _ = redact_exfiltration_urls(err_text)
            err_text, _ = redact_credentials(err_text)
            err_text = err_text[:500]
            logger.error(
                "Auto-update (wheel): installer failed (rc=%d): %s",
                proc.returncode,
                err_text,
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "failed",
                    f"Installer failed (exit {proc.returncode}) — " "run manually: kirocrew update",
                )
            return

        logger.info("Auto-update (wheel): installer succeeded, restarting gateway")
        if self.dashboard_state:
            self.dashboard_state.push_update_progress("restarting", "Restarting server…")
            from kiro_crew.dashboard.chat import save_all_slots_to_history

            try:
                await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        subprocess_executor(),
                        save_all_slots_to_history,
                        self.dashboard_state,
                    ),
                    timeout=5.0,
                )
            except Exception:
                logger.debug(
                    "Dashboard slot save before wheel auto-update restart failed",
                    exc_info=True,
                )
        if self.sessions:
            await self.sessions.close_all()
        # Restart into the freshly-installed version.
        os.execv(sys.executable, [sys.executable, "-m", "kiro_crew"] + sys.argv[1:])

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    async def _connect_slack(self) -> bool:
        """Connect the Slack socket-mode client. Non-fatal on failure.

        Returns ``True`` if connected, ``False`` if Slack is disabled or the
        connect failed. A failure (network/proxy/timeout — e.g. a stale
        ``HTTPS_PROXY`` in the environment) must NOT crash the gateway: the
        dashboard, cron, and task runner keep running in dashboard-only mode.

        ponytail: no background retry of the initial connect — Slack DM stays
        disabled until the next gateway restart.

        Slack is a GOVERNED transport like every other channel: a ``channels``
        policy that denies ``slack`` must stop it from CONNECTING, not merely drop
        its inbound messages. The check runs off the loop (it walks the
        ProfileStore) and, on a deny, the socket client is dropped so nothing can
        later reconnect it. Default build (no ``channels`` policy) permits, so the
        connect path is byte-identical to today.
        """
        if not self._socket_client:
            return False
        loop = asyncio.get_running_loop()
        slack_permitted = await loop.run_in_executor(
            maintenance_executor(), _channel_transport_permitted, "slack"
        )
        if not slack_permitted:
            logger.info("slack transport not started: denied by channels governance policy")
            self._socket_client = None
            return False
        try:
            await self._socket_client.connect()
            print("👻 Kiro Crew gateway connected to Slack")
            return True
        except Exception as exc:
            # Keep a short reason for status surfaces (settings badge). Slack
            # API errors carry a stable code like "invalid_auth"; anything
            # else (network/proxy) falls back to the exception class name.
            reason = ""
            resp = getattr(exc, "response", None)
            if resp is not None:
                try:
                    reason = str(resp.get("error", "") or "")
                except Exception:
                    reason = ""
            self._slack_connect_error = (reason or type(exc).__name__)[:120]
            logger.warning(
                "Slack socket-mode connect failed — continuing in "
                "dashboard-only mode (Slack DM disabled this session)",
                exc_info=True,
            )
            print(
                "⚠️  Slack connect failed — running dashboard-only "
                "(check network/proxy; details in gateway.log)"
            )
            return False

    def _write_marker_worker(self, run_marker: Any, port: int) -> None:
        """Write the run marker; self-clear if shutdown flagged a clear.

        ``run_marker`` is the :mod:`kiro_crew.instances.run_marker` module,
        passed in by ``run()`` (which already imports it lazily) so this
        worker adds no import of its own. Runs on a ``to_thread`` worker.
        If graceful shutdown timed out waiting for this write, it sets
        ``_marker_clear_pending`` BEFORE clearing the marker itself — so
        whichever order the write and the shutdown-side clear land in, this
        thread re-clears its own late write. The clear lives in the same
        thread as the write (not an event-loop callback) because
        ``os._exit`` can beat any callback still queued on the loop.
        """
        try:
            run_marker.write_marker(port)
        finally:
            if self._marker_clear_pending.is_set():
                try:
                    run_marker.clear_marker(port)
                except Exception:
                    logger.debug("Late run-marker self-clear skipped", exc_info=True)

    async def run(self) -> None:
        """Start all services and block until shutdown signal."""
        # ── Crash guard (D1/D2 of Lorikeets-3929) ──
        # Install the asyncio exception handler on the running loop.
        # atexit + excepthook were already installed in cli.py before asyncio.run().
        crash_guard.install_loop_handler(asyncio.get_running_loop())

        # Log process identity to the gateway log (D3 of Lorikeets-3929)
        logger.info(
            "=== GATEWAY PID=%d STARTED AT %s ===",
            os.getpid(),
            datetime.now(timezone.utc).isoformat(),
        )

        # Raise FD limit — each kiro-cli session uses ~6 FDs (3 pipes)
        # plus MCP server subprocesses. Default macOS limit (256) is too low.
        # No-op on Windows (no per-process descriptor rlimit).
        platform_compat.raise_nofile_soft_limit(10240)

        # Refuse to boot when the data home cannot persist state. Every save
        # path (chat history, cron history, session PIDs) needs file creation +
        # advisory locking in the data home; when either is broken (e.g. a
        # seccomp filter inherited from a sandboxed parent turns flock/mkstemp
        # into ENOSYS) the gateway would still serve traffic while silently
        # dropping every write — and the very next call below would crash with
        # a raw traceback anyway. Failing here is loud, early, and actionable.
        # Off-loop: the probe does real filesystem I/O (mkstemp + flock), which
        # on a stalled filesystem would otherwise wedge the event loop.
        def _probe_persistence() -> str | None:
            return platform_compat.probe_file_persistence(data_home())

        try:
            persistence_error = await asyncio.to_thread(_probe_persistence)
        except RuntimeError as exc:
            # asyncio.to_thread could not get a worker thread (executor
            # exhaustion/shutdown). A process that cannot spawn one thread at
            # boot cannot run session pools either — route through the clean
            # preflight exit below instead of dying with a raw traceback.
            persistence_error = f"cannot run the persistence preflight: {exc}"
        if persistence_error is not None:
            logger.critical(
                "Persistence preflight failed — refusing to start: %s",
                persistence_error,
            )
            print(
                f"❌ Cannot persist state: {persistence_error}",
                file=sys.stderr,
            )
            raise SystemExit(1)

        # Clean up orphaned kiro-cli processes from previous runs
        from kiro_crew.session import cleanup_orphaned_sessions

        cleanup_orphaned_sessions()

        # Fill the sandbox probe cache BEFORE any on-loop spawn path can reach
        # detect_backend(). Waiting (off-loop) rather than firing-and-forgetting
        # is what makes that guarantee hold: a fire-and-forget prewarm leaves the
        # very next caller racing the warm thread and reading a cold-cache
        # transient as "no sandbox backend on this host".
        try:
            await asyncio.to_thread(warm_backend)
        except RuntimeError:
            logger.warning("sandbox warm_backend skipped (thread exhaustion); cache stays cold")

        # ── Initialise all services ──
        from kiro_crew.slack.events import SeenCache, init_socket_mode
        from kiro_crew.slack.interactions import init as init_interactions

        # Cautious boot: decide ONCE — off-loop — whether the previous instance
        # left a recent loop-stall crash dump. If it did, the pause_before()
        # calls below (and in start_dashboard) stagger the startup battery so
        # a host that is possibly still under the same memory pressure is not
        # hit with everything at once. Fails open: any error means normal boot.
        await cautious_boot.initialize()

        seen = SeenCache()
        await self._init_services()

        # Wire in-process embeddings (always-on) and kick background model download
        await self._start_embeddings()

        # Auto-migrate legacy markdown memory to the vector store in the
        # background (fire-and-forget) — never blocks boot. Idempotent: gated on
        # memory.migrated for the migrate phase; the re-embed sweep probes for
        # pending rows with plain SQL and returns without loading the embedding
        # model when there is nothing to embed.
        self._auto_migrate_task = asyncio.create_task(self._auto_migrate_memory())
        self._background_tasks.add(self._auto_migrate_task)
        self._auto_migrate_task.add_done_callback(self._background_tasks.discard)

        # Start MCP gateway sidecar before any ACP session can spawn.  The
        # rewriter writes the agent-JSON overlay first so kiro-cli picks up
        # the broker-wired MCP entries the moment a session starts.  No-op
        # when ``mcp_gateway.enabled`` is False.
        await cautious_boot.pause_before("MCP gateway sidecar")
        await self._init_mcp_gateway()

        # Arming the cron scheduler fires any overdue jobs immediately, so
        # under cautious boot this pause also defers the post-restart cron
        # catch-up burst out of the app/MCP launch window.
        await cautious_boot.pause_before("cron scheduler")
        await self._init_cron()
        await self._init_heartbeat()
        self._init_mcp_discovery()
        self._init_subagents()
        self._init_task_runner()
        if not self._no_dashboard:
            await self._init_dashboard()
            self._init_crew()
        else:
            await self._init_api_server()
        # Record this gateway's own kirocrew launcher, keyed by the port it
        # serves, so a remote token-mint execs THIS install's venv instead of
        # a stale ~/.local/bin/kirocrew that may point at an uninstalled
        # worktree. See kiro_crew.instances.run_marker. Written for headless
        # API-only gateways too: the marker's filename is what lets a client
        # or MCP child discover a non-default port when neither KIROCREW_PORT
        # nor dashboard.url names one. Dispatched as a tracked background
        # task, never awaited: write_marker does atomic file writes plus a
        # prune scan over prior runs' markers, so on a slow filesystem an
        # await here would gate READY on file-count-scaled maintenance. The
        # marker is best-effort discovery metadata — nothing at boot depends
        # on it, and write_marker never raises. The guard keeps startup alive
        # even when dashboard init was skipped and no port was ever resolved.
        try:
            from kiro_crew.instances import run_marker

            if self._dashboard_port:
                _marker_task = asyncio.create_task(
                    asyncio.to_thread(self._write_marker_worker, run_marker, self._dashboard_port)
                )
                self._marker_write_task = _marker_task
                self._background_tasks.add(_marker_task)
                _marker_task.add_done_callback(self._background_tasks.discard)
        except Exception:
            logger.debug("Gateway run-marker write skipped", exc_info=True)

        # Publish the MCP-gateway broker + apply callbacks onto
        # DashboardState now that it exists (the broker started earlier).
        self._wire_mcp_gateway_dashboard()

        # Emit machine-readable READY line for test harnesses (--json-ready).
        # Printed BEFORE bg_session and other startup chatter so the harness
        # can read it deterministically with a single readline() in the
        # KIROCREW_READY: prefix matcher.
        if self._json_ready:
            ready_token = generate_token(
                self._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
            )
            ready_payload = {
                "port": self._dashboard_port,
                "token": ready_token,
                "pid": os.getpid(),
                "home": str(data_home()),
            }
            print(f"KIROCREW_READY:{json.dumps(ready_payload)}", flush=True)

        # AutoNudge must run after dashboard init — _fire callback dereferences
        # self.dashboard_state. In --no-dashboard mode the guard inside _fire
        # early-returns so persisted loops are harmless until a dashboard
        # process takes over.
        await self._init_autonudge()

        # Wire up event routing and interactive handlers
        init_interactions(self)
        init_socket_mode(self, seen)

        await self._start_channel_transports()

        # ── Signal handlers ──
        # Installed BEFORE the update check (below) is started. The check runs
        # five sequential git subprocesses whose timeouts sum to ~70s, so while
        # it was inline-awaited here a stalled network left the gateway with no
        # SIGINT/SIGTERM handler for over a minute: Ctrl-C did nothing and the
        # process looked wedged. Handlers first means the boot is interruptible
        # from this point on regardless of what the check does.
        loop = asyncio.get_running_loop()
        _shutting_down = False

        def _on_signal(*_args: object) -> None:
            nonlocal _shutting_down
            if _shutting_down:
                print("\n👻 Force exit!")
                cleanup_orphaned_sessions()
                os._exit(0)
            _shutting_down = True
            shutdown_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _on_signal)
            except (RuntimeError, ValueError):
                # Not in main thread (e.g. pytest-xdist worker) — skip.
                pass
            except NotImplementedError:
                # Windows ProactorEventLoop does not support add_signal_handler.
                # Fall back to signal.signal for SIGINT so shutdown_event still
                # gets set; SIGTERM is not meaningfully deliverable on Windows.
                if sig == signal.SIGINT:

                    def _sigint_fallback(*_a: object) -> None:
                        try:
                            loop.call_soon_threadsafe(_on_signal)
                        except RuntimeError:
                            _on_signal()  # loop already closed

                    try:
                        signal.signal(sig, _sigint_fallback)
                    except (ValueError, OSError):
                        pass  # not in main thread

        # Update check — fire-and-forget, NOT awaited. It runs five sequential
        # git subprocesses (fetch/rev-parse/...) whose timeouts sum to ~70s, and
        # nothing later in boot depends on its result: it only flips
        # _update_info / pushes a dashboard refresh, or applies an auto-update
        # that restarts the process. Awaiting it delayed the dashboard URL by up
        # to ~70s on a stalled network. handlers_system.api_status treats the
        # same check as fire-and-forget for the same reason. Registered in
        # _background_tasks so the task is not GC'd mid-flight and is reaped on
        # shutdown with the rest.
        print("👻 Checking for updates…")
        self._update_check_task = asyncio.create_task(self._check_for_updates())
        self._background_tasks.add(self._update_check_task)
        self._update_check_task.add_done_callback(self._background_tasks.discard)

        # ── Announce the dashboard URL — deliberately NOT behind the probe ──
        # The HTTP port is already listening (bound by _init_dashboard above), and
        # nothing about building, formatting or printing a URL depends on MCP
        # state. The ordering constraint documented below covers ONLY session
        # spawn. Printing here instead of after the probe removes up to
        # ~mcp_probe_timeout_secs+15 of "no URL on screen" from every boot, and
        # all of it from the timed-out path.
        dashboard_url = ""
        if not self._no_dashboard:
            try:
                host = resolve_dashboard_host(self._local_only, self._configured_host)
                _cfg_url = self._cfg.dashboard.url
                if _cfg_url and "://" in _cfg_url:
                    base_url = _cfg_url.rstrip("/")
                else:
                    base_url = f"http://{host}:{self._dashboard_port}"
                startup_token = generate_token(
                    self._owner_id or "local-startup", ttl_seconds=MAX_SESSION_TTL_SECS
                )
                dashboard_url = build_dashboard_url(
                    base_url, startup_token, local_only=self._local_only
                )
                for line in format_dashboard_urls(
                    dashboard_url,
                    port=self._dashboard_port,
                    local_only=self._local_only,
                    has_custom_host=bool(self._configured_host),
                ):
                    print(line)

                # Auto-open dashboard — skip on headless remote sessions
                _is_ssh = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_CLIENT"))
                _has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
                _skip_open = _is_ssh and not _has_display and sys.platform != "darwin"
                if self._no_open or not self._cfg.dashboard.auto_open_browser:
                    pass  # suppressed via --no-open flag or config
                elif _skip_open:
                    print("👻 Headless remote session — skipping browser auto-open")
                else:
                    # Runs as a task so a slow browser launch overlaps the MCP
                    # probe instead of delaying it. Tracked so it is not GC'd
                    # mid-flight and is reaped on shutdown with the rest.
                    _open_task = asyncio.create_task(self._auto_open_dashboard(dashboard_url))
                    self._background_tasks.add(_open_task)
                    _open_task.add_done_callback(self._background_tasks.discard)
            except Exception:
                # Announcing the URL is BEST EFFORT and must never abort boot.
                # This block used to sit inside a fire-and-forget task, so a
                # failure here could not take the gateway down; moving it onto
                # the boot path has to preserve that. The dashboard is already
                # listening either way — the operator loses a printed line, not
                # the service, and `kirocrew token` still produces a URL.
                logger.warning("Dashboard URL announcement failed", exc_info=True)

        # Wait for MCP probe to finish before warming sessions —
        # kiro-cli reads MCP config at spawn time, so sessions must
        # start AFTER the probe has synced all servers to mcp.json.
        from kiro_crew.dashboard.handlers import _bg_mcp_probe

        print("👻 Probing MCP servers…")
        # self._cfg is the config this boot already loaded — re-reading it here
        # would pay a deepcopy plus a full nested-dataclass rebuild for one scalar.
        _probe_t = self._cfg.dashboard.mcp_probe_timeout_secs + 15
        try:
            await asyncio.wait_for(_bg_mcp_probe(), timeout=_probe_t)
        except asyncio.TimeoutError:
            print("👻 MCP probe timed out — continuing without full probe")

        # ── Start background session (this IS gated on the probe) ──
        async def _start_bg_session() -> None:
            try:
                assert self.sessions is not None
                await self.sessions.start_pool(blocking=False)
                logger.info("Background session starting")
            except Exception:
                logger.warning("Background session start failed", exc_info=True)

        asyncio.create_task(_start_bg_session())

        # Stale-asset watchdog: detects when an update prunes the running
        # install's static assets and triggers graceful shutdown so the
        # supervisor can restart a fresh process. It first drains in-flight
        # backend turns (count_in_flight) so active work isn't killed
        # mid-prompt by the restart.
        _watchdog = asyncio.create_task(
            run_stale_asset_watchdog(shutdown_event, count_in_flight=self._count_in_flight_work)
        )
        self._background_tasks.add(_watchdog)
        _watchdog.add_done_callback(self._background_tasks.discard)

        print("👻 Kiro Crew gateway starting…")
        print(f"\n{DATA_WARNING}\n")

        connected = await self._connect_slack()
        # Record the real socket outcome so status surfaces (e.g. the Slack
        # settings badge) can distinguish "connected" from "tokens present
        # but connect failed" — slack_client alone only proves the latter.
        if self.dashboard_state:
            self.dashboard_state.slack_socket_connected = connected
            self.dashboard_state.slack_connect_error = getattr(self, "_slack_connect_error", "")

        # Deferred tracked-channel capability probe (fire-and-forget, never
        # awaited — boot latency is unaffected). A Slack install created before
        # the manifest gained groups:history keeps its old grant, so a tracked
        # private channel delivers no events and nothing logs; the probe turns
        # that silent-dead state into a warning + dashboard notification.
        if connected and self.slack is not None and self._tracking_channels:
            _scope_task = asyncio.create_task(
                warn_unreadable_tracked_channels(
                    self.slack,
                    set(self._tracking_channels),
                    notify=self.dashboard_state.notify if self.dashboard_state else None,
                )
            )
            self._background_tasks.add(_scope_task)
            _scope_task.add_done_callback(self._background_tasks.discard)

        # Block until shutdown
        await shutdown_event.wait()
        print("👻 Shutting down…")

        # Drop this gateway's run-marker BEFORE _shutdown() releases the
        # listener: once the port is free a replacement gateway can bind it
        # and publish its own marker + credential, which this clear would
        # then delete (clear_marker is unconditional — consumers verify
        # ownership on read, but deleting the successor's credential 403s
        # its clients). The clear itself is best-effort; a stale marker is
        # harmless — the next startup overwrites it. The wait for the
        # in-flight write is bounded so a stalled write cannot eat into the
        # graceful-shutdown deadline below (which saves active slots). On
        # timeout the detached writer thread may still republish the marker
        # after the clear, so _marker_clear_pending is set FIRST: the
        # writer thread (see _write_marker_worker) then re-clears its own
        # late write in the same thread, with no event-loop callback that
        # os._exit could beat. TimeoutError and a failed write are both
        # caught HERE (not by the outer except) so they still fall through
        # to the clear.
        try:
            from kiro_crew.instances import run_marker

            if self._dashboard_port:
                if self._marker_write_task is not None:
                    try:
                        await asyncio.wait_for(
                            self._marker_write_task,
                            timeout=_MARKER_WRITE_WAIT_SECS,
                        )
                    except asyncio.TimeoutError:
                        self._marker_clear_pending.set()
                        logger.warning(
                            "Run-marker write did not finish within %ss; "
                            "clearing marker without waiting",
                            _MARKER_WRITE_WAIT_SECS,
                        )
                    except Exception:
                        logger.debug(
                            "Run-marker write failed; clearing anyway",
                            exc_info=True,
                        )
                run_marker.clear_marker(self._dashboard_port)
        except Exception:
            logger.debug("Gateway run-marker clear skipped", exc_info=True)

        try:
            await asyncio.wait_for(self._shutdown(), timeout=GRACEFUL_SHUTDOWN_SECS)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Graceful shutdown timed out — force exiting")

        print("👻 Goodbye!")
        # Kill any kiro-cli processes that survived graceful shutdown
        cleanup_orphaned_sessions()
        os._exit(0)

    async def _start_channel_transports(
        self, descriptors: "tuple[ChannelDescriptor, ...] | None" = None
    ) -> None:
        """Start each non-Slack transport, gated on the ``channels`` scope.

        Registry-driven (PR ③ of the channel-plugin RFC): the roster comes from
        :func:`kiro_crew.channels.builtin_channel_descriptors` and the loop
        lives in :mod:`kiro_crew.messaging.registry` — adding a channel no
        longer edits this method. ``descriptors`` is injectable for tests.

        Every transport is a guarded no-op unless enabled + credentialed (its
        own ``maybe_start_*``), and is ADDITIONALLY gated on the ``channels``
        governance scope: a policy that denies the transport member keeps it from
        connecting at all, and its client stays ``None``. The member ids are
        IDENTICAL to the outbound chokepoints — outbound-send (``mcp_core``) and
        outbound cross-surface mirroring (``chat_runner``) — so one ``channels``
        allowlist governs a transport at connect time and on every outbound path
        (inbound receive is gated per-message by
        ``messaging.identity.channel_inbound_permitted``).

        Default-build invariant: with no policy governing ``channels`` (the
        standard OSS build) the gate permits, so every transport starts exactly
        as before — byte-identical behavior. Slack is a registry member too
        (``start=None``) but is gated in ``_connect_slack`` rather than here,
        because it owns its own socket-client lifecycle (a deny must drop that
        client, not just skip a start call).

        Loop hygiene: ``_channel_transport_permitted`` reaches
        ``ProfileStore._ensure_fresh``, which stats/reads the profile files off
        disk — blocking I/O. This method runs on the gateway event loop (inside
        ``run()``), so the governance decisions are computed together in an
        executor BEFORE any transport is started; only the actual factory
        awaits stay on the loop.

        Enabled-only eval: the gate is queried ONLY for a transport whose
        ``_<member>_enabled`` is set (config-enabled + credentialed). A transport
        that is off never starts regardless of policy, so evaluating it would only
        emit a spurious deny-SEL for a channel that was never going to connect.
        A member not evaluated defaults to not-permitted (it is off anyway), so
        the no-policy default is unchanged: every ENABLED transport still resolves
        to permit and starts exactly as before.
        """
        if descriptors is None:
            descriptors = builtin_channel_descriptors()
        boot = registry.bootable(descriptors)
        enabled = {
            d.channel_type: bool(getattr(self, f"_{d.channel_type}_enabled", False)) for d in boot
        }
        loop = asyncio.get_running_loop()
        permitted = await loop.run_in_executor(
            maintenance_executor(),
            lambda: {
                m: (_channel_transport_permitted(m) if enabled[m] else False) for m in enabled
            },
        )
        self._channel_handles = await registry.start_channels(self, descriptors, permitted)


# Strong reference to the background slice-limit apply task: the event loop
# holds tasks weakly, so a fire-and-forget create_task with no reference can
# be garbage-collected mid-flight.
_SLICE_LIMITS_TASK: "asyncio.Task[None] | None" = None

# Strong ref to the fire-and-forget agents-dir janitor sweep launched at boot
# (the loop holds tasks weakly, so without this it could be GC'd mid-flight).
_AGENTS_JANITOR_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the liveness-keyed agent-scratch sweep loop (#5063).
_AGENT_SCRATCH_SWEEP_TASK: "asyncio.Task[None] | None" = None


async def run_gateway(
    cfg: KiroCrewConfig,
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_open: bool = False,
    port_override: str | None = None,
    json_ready: bool = False,
    approval_mode: str | None = None,
    test_mode: bool = False,
) -> None:
    """Start the Slack Socket Mode gateway (blocks until shutdown).

    If Slack credentials are missing, starts in **dashboard-only** mode:
    all services (chat, cron, subagents, task runner) are available via
    the web dashboard, but Slack connectivity is disabled.
    """
    # ── Platform context boot (CPP seam) ──
    # Resolve + install the PlatformContext ONCE before any service spins up.
    # Idempotent: a no-op when ``cli.main`` already booted in this process.
    # Standalone composes the all-defaults context (identical to today); a
    # non-standalone profile that cannot compose its companion fails closed.
    boot_platform(cfg)

    # ── Aggregate cgroup ceiling for all agent scopes ──
    # The per-spawn scope wrapper (sandbox.cgroup_scope_argv) bounds ONE spawn
    # tree; this bounds ALL of them together by putting MemoryMax/TasksMax on
    # their shared parent slice. Scheduled as a contained background task —
    # never awaited on the boot path, so a slow user manager (the systemctl
    # call carries a 15s timeout) cannot delay dashboard binding. The module
    # global keeps a strong reference (the loop holds tasks weakly). Skipped
    # in test_mode: the offline E2E gate must not mutate the developer's real
    # user manager. Failure is non-fatal — the function logs and the
    # per-scope ceilings still apply.
    global _SLICE_LIMITS_TASK
    if not test_mode:

        async def _apply_slice_limits() -> None:
            try:
                await asyncio.to_thread(ensure_agents_slice_limits)
            except Exception:
                logging.getLogger(__name__).warning(
                    "aggregate cgroup ceiling apply failed", exc_info=True
                )

        _SLICE_LIMITS_TASK = asyncio.create_task(_apply_slice_limits(), name="agents-slice-limits")

    # ── Agents-dir janitor (fire-and-forget) ──
    # Sweep aged orphaned atomic-write temps + stale backups from the shared
    # kiro agents directory (see kiro_crew.agents_janitor). Scheduled as a
    # contained background task and never awaited, so a slow or failing sweep
    # cannot delay dashboard binding or crash boot: the coroutine offloads the
    # blocking filesystem work to a thread and swallows every error. The module
    # global keeps a strong reference (the loop holds tasks weakly). Skipped in
    # test_mode so the offline E2E gate never touches the developer's real
    # agents dir.
    global _AGENTS_JANITOR_TASK
    if not test_mode:

        async def _run_agents_janitor() -> None:
            try:

                def _sweep_in_thread() -> None:
                    # kiro_agents_dir() resolved INSIDE the worker thread, not
                    # in this coroutine body: the resolver walks env +
                    # Path.home() + .resolve(), which is blocking filesystem
                    # work — on an unavailable network home it can stall
                    # indefinitely, and this coroutine runs on the event loop
                    # during boot, between bind and serve.
                    sweep_agents_dir(
                        kiro_agents_dir(),
                        sweep_backups=cfg.agent.sweep_agents_backups,
                    )

                await asyncio.to_thread(_sweep_in_thread)
            except Exception:
                logging.getLogger(__name__).debug(
                    "agents-dir janitor sweep failed at boot", exc_info=True
                )

        _AGENTS_JANITOR_TASK = asyncio.create_task(_run_agents_janitor(), name="agents-dir-janitor")

    # ── Agent scratch sweep (fire-and-forget, boot + hourly) ──
    # Reclaim per-process agent scratch dirs whose owner process is dead
    # (see kiro_crew.agent_scratch). Liveness-keyed, never age-keyed, so a
    # long-lived session's in-flight work is never deleted under it -- and
    # because agent processes can OUTLIVE a gateway restart, the sweep checks
    # each recorded owner pid instead of clearing wholesale at boot. Hourly
    # repeats catch processes that die while the gateway stays up (no
    # per-teardown hook: the positive liveness signal covers every death
    # path by construction). Same containment posture as the janitor above:
    # offloaded, fail-open, skipped in test_mode.
    global _AGENT_SCRATCH_SWEEP_TASK
    if not test_mode:

        async def _run_agent_scratch_sweep() -> None:
            while True:
                # Sleep FIRST: gateway boot must not pick up file-count-scaled
                # maintenance (no-new-work-on-gateway-boot-path); the first
                # sweep runs an hour in, and nothing here is boot-urgent --
                # scratch reclamation has no correctness deadline.
                await asyncio.sleep(3600)
                try:
                    await asyncio.to_thread(agent_scratch.sweep_dead_scratch)
                except Exception:
                    logging.getLogger(__name__).debug("agent-scratch sweep failed", exc_info=True)

        _AGENT_SCRATCH_SWEEP_TASK = asyncio.create_task(
            _run_agent_scratch_sweep(), name="agent-scratch-sweep"
        )

    # ── Anonymous usage beacon (at most one HTTP GET per day) ──
    # Detached daemon thread, NOT awaited: ``beacon.send`` is blocking urllib
    # with a 5s timeout, and boot must never wait on the network (nor pin
    # interpreter exit — hence daemon=True, matching the model-download helper's
    # reasoning in embeddings.py). Errors are swallowed inside ``send``; a failed
    # heartbeat is invisible by design. ``test_mode`` skips it entirely so the
    # offline E2E gate can never make an outbound request.
    #
    # ``acked`` withholds the FIRST heartbeat until the user has actually been
    # shown the disclosure and its opt-out. Boot runs before the dashboard has
    # ever rendered, so on a fresh install this thread would otherwise ping
    # before the user could possibly decline, making the opt-out an offer
    # arriving after the fact. Established installs are unaffected: the gate
    # applies only while `is_first_send()` holds.
    if not test_mode:
        with contextlib.suppress(Exception):
            threading.Thread(
                target=beacon.send,
                args=(cfg.telemetry.beacon_endpoint, kiro_crew.__version__),
                kwargs={
                    "enabled": cfg.telemetry.beacon_enabled,
                    "acked": cfg.dashboard.privacy_acked,
                },
                name="kirocrew-beacon",
                daemon=True,
            ).start()

    orchestrator = GatewayOrchestrator(
        cfg,
        no_dashboard=no_dashboard,
        no_crons=no_crons,
        no_open=no_open,
        port_override=port_override,
        json_ready=json_ready,
        approval_mode=approval_mode,
        test_mode=test_mode,
    )
    await orchestrator.run()
