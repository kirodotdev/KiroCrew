"""Slack Socket Mode gateway orchestrator for KiroCrew.

Manages the lifecycle of all runtime services: session manager, cron
scheduler, context builder, heartbeat, subagents, task runner, dashboard,
and the Slack Socket Mode connection.

Event routing, interactive button handling, and allowlist management
live in sibling modules:

- ``events``        — Socket Mode event dispatch + dedup
- ``interactions``  — Block Kit button routing
- ``allowlist``     — tracking-channel join prompts + config persistence

The orchestrator's own responsibilities are composed from private owners in
``gateway_runtime``: tool-approval policy, cron dispatch and run tallies,
delivery routing, the channel lifecycle, the MCP broker, the memory lifecycle
and admission. This module stays their only import path and patch surface,
and their functions run on its globals: ``# noqa: F401`` marks an import
statement binding a name no function left here reads, which an owner reads
or the module's name surface keeps. What stays here, and why, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""

from __future__ import annotations

# Imports marked ``# noqa: F401`` are the gateway_runtime owners' globals; removing one fails
# test_every_global_an_owner_function_reads_is_bound_on_the_facade, not a call at runtime.
import asyncio
import contextlib
import functools
import hashlib  # noqa: F401
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
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
from slack_sdk.socket_mode.websockets import SocketModeClient as WSSocketModeClient

import kiro_crew
import kiro_crew.crash_guard as crash_guard
from kiro_crew import (
    agent_scratch,
    autonudge_selfarm,
    beacon,
    dep_sync,
    name_grant,
    platform_compat,
    runtime_death,
    session_work_dir,
    shutdown_event,
    work_root,
)
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.agent_sdk import AgentTurnUsage
from kiro_crew.agents_janitor import sweep_agents_dir
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    MONITOR_TERMINAL_REASON,
    STRUCTURAL_TERMINAL_REASON,
    AutoNudgeService,
    NudgeLoop,
)
from kiro_crew.autonudge import enabled as autonudge_enabled
from kiro_crew.autonudge import (
    is_channel_key,
    is_structured_monitor_loop,
    nudge_cycle_header,
    runtime_budget_exceeded,
    terminal_notification_delivery_matches,
)
from kiro_crew.beacon import distribution
from kiro_crew.channel_history import ChannelHistory
from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.config.live import ConfigChange, ConfigDeferred, Subscription  # noqa: F401
from kiro_crew.config.loader import (
    CRED_DISCORD_BOT_TOKEN,
    CRED_FEISHU_APP_ID,
    CRED_FEISHU_APP_SECRET,
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
    workspace_root,
)
from kiro_crew.config.paths import kiro_agents_dir
from kiro_crew.constants import SUBAGENT_COMPLETION_META_KEY, strip_control_comments
from kiro_crew.context import ContextBuilder, session_store_for_turn
from kiro_crew.context_management import summarize_result
from kiro_crew.cron import (  # noqa: F401
    _AUTO_PAUSE_THRESHOLD,
    _SUBPROC_CLEANUP_ALLOWANCE_SECS,
    CronJob,
    CronService,
    CronStoreBusy,
    CronStoreUnreadable,
    agent_sequence_dispatches,
    build_cron_session_context,
    effective_wake_budget,
)
from kiro_crew.cron_script import delivery_fingerprint, run_command_sandboxed, run_script_sandboxed
from kiro_crew.dashboard import cautious_boot, start_dashboard
from kiro_crew.dashboard.chat_persistence import rehydrate_slot_from_history_async
from kiro_crew.dashboard.chat_runner import (
    _arm_queued_delivery_settlement,
    _auto_approve_reason,
    _resolve_channel_target,
    _run_chat,
    _slot_is_trusted,
)
from kiro_crew.dashboard.chat_utils import (  # noqa: F401
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    dashboard_slot_key,
    mint_options_token,
    remember_slack_options,
    subagent_event_slot,
)
from kiro_crew.dashboard.cron_inject import (  # noqa: F401
    context_meter_reading,
    ensure_cron_slot,
    inject_cron_result_to_dashboard,
    prefetch_cron_history,
)
from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES
from kiro_crew.dashboard.handlers.autonudge import (
    _redact_monitor_value,
    compose_nudge_body,
    render_nudge_message,
)
from kiro_crew.dashboard.handlers.updates import _update_info, record_auto_update_effect
from kiro_crew.dashboard.handlers.updates import remediation_command as _remediation_command
from kiro_crew.dashboard.handlers.usage import (
    persist_token_record_async,
    read_context_tokens,
    read_effective_agent,
)
from kiro_crew.dashboard.listener_guard import listener_guard_exit_code
from kiro_crew.dashboard.origin import (
    build_dashboard_url,
    format_dashboard_urls,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.stale_asset_watchdog import (
    run_stale_asset_watchdog,
    shutdown_exit_code,
)
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    DashboardState,
    stage_boundary_for,
)
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn
from kiro_crew.embeddings import (  # noqa: F401
    embedding_model_is_custom,
    get_shared_embedder,
    make_sync_embed_fn,
    model_file_present,
    peek_ready_shared_embedder,
    reconcile_store_embedding_space,
    reembed_progress,
    start_background_model_download,
    store_embedding_space_is_stale,
)
from kiro_crew.executors import (  # noqa: F401
    CronQueueTimeout,
    configure_default_executor,
    cron_gate_budget,
    embed_executor,
    maintenance_executor,
    run_in_cron_gate_pool,
    run_in_cron_pool,
    run_in_embed_pool,
    subprocess_executor,
)
from kiro_crew.frontend import build_frontend_async
from kiro_crew.gateway_restart import resolve_restart_launcher
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS
from kiro_crew.heartbeat import (
    HEARTBEAT_TASK_TIMEOUT_SECS,
    HeartbeatService,
    is_keep_response,
    strip_keep_sentinel,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import HookManager, HooksConfig, hooks_config_from_config_dict  # noqa: F401
from kiro_crew.kiro_cli import PATH_ONLY_INSTALL_NOTE, is_bundled_kiro_cli, pin_kiro_cli
from kiro_crew.learn import LessonStore
from kiro_crew.llm_helpers import (  # noqa: F401
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    acp_error_is_transient,
    annotate_model_fallback,
    append_fallback_story,
    configured_fallback_chain,
    provider_fallback_active,
    provider_last_turn_usage,
    provider_model_pin_partial,
    provider_model_pin_refused,
    save_conversation_turn_off_loop,
    stream_and_collect,
    transient_retry_delay,
)
from kiro_crew.mcp_cron import vet_job_at_fire_time  # noqa: F401
from kiro_crew.mcp_gateway import is_gateway_supported  # noqa: F401
from kiro_crew.mcp_gateway.launch_approval import (  # noqa: F401
    LaunchApprovals,
    filter_target_env,
    load_approvals,
    save_pass,
)
from kiro_crew.mcp_gateway.launch_resolve import rewrite_kwargs  # noqa: F401
from kiro_crew.mcp_gateway.manager import (  # noqa: F401
    GatewayManager,
    GatewaySpec,
)
from kiro_crew.mcp_gateway.resolve_once import prefetch as resolve_prefetch  # noqa: F401
from kiro_crew.mcp_gateway.rewriter import rewrite_agents  # noqa: F401
from kiro_crew.mcp_hot_reload import parse_kiro_cli_version
from kiro_crew.memory import MemoryStore
from kiro_crew.messaging import (
    APPROVAL_INTERACTIVE,
    TurnDriver,
    inbound_spool,
    registry,
    turn_ceiling,
)
from kiro_crew.messaging.dispatch import (
    build_directive_consumer,
    build_tool_gate,
    consume_reinjection,
    rearm_reinjection,
    stop_reason_landed,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (  # noqa: F401
    CHANNEL_SESSION_NAMESPACES,
    CHAT_TYPE_DIRECT,
    DM_SCOPE_UNIFIED,
    SLACK_NAMESPACE,
    ChannelLink,
    channel_namespace_of,
    parse_session_key,
)
from kiro_crew.messaging.renderer import (  # noqa: F401
    SilentRenderer,
    chunk_for_transport,
    display_safe,
)
from kiro_crew.messaging.spawn_approval_delivery import deliver_spawn_approval
from kiro_crew.messaging.transport import InboundMessage, delivery_confirmed
from kiro_crew.monitoring.completion import (
    MonitorCompletionHook,
    disposition_for_stop_reason,
    is_monitor_completion_evidence,
)
from kiro_crew.monitoring.models import (
    DEFAULT_MONITOR_STALL_MIN_SECS,
    DEFAULT_MONITOR_STALL_TICKS,
    MONITOR_STOP_APPROVAL_STALL,
    MONITOR_STOP_COMPLETION_UNAVAILABLE,
    MONITOR_STOP_INVALID_RECORD,
    MONITOR_STOP_SESSION_UNAVAILABLE,
    MONITOR_STOP_UNSUPPORTED_VERSION,
    MONITOR_STOP_VERDICT_STALL,
    MonitorActionDisposition,
    MonitorDispatchResult,
    MonitorOutcome,
    monitor_state_public_dict,
)
from kiro_crew.notifications.bus import MONITOR_CHANNEL
from kiro_crew.platform import boot_platform
from kiro_crew.platform.context import (  # noqa: F401
    PlatformCompositionError,
    current_context,
    redact_log_via_context,
    redact_via_context,
    safe_context_call,
)
from kiro_crew.platform.governance_profiles import (  # noqa: F401
    HOST_SESSION_KEY,
    audit_governance_degraded,
    governance_permits,
    vet_and_audit,
)
from kiro_crew.platform.update_capability import (
    AUTO_EFFECT_INSTALL,
    AUTO_EFFECT_MANDATORY,
    AUTO_EFFECT_NOTIFY,
    AUTO_ROUTE_GIT,
    AUTO_ROUTE_WHEEL,
    CHECK_SUCCEEDED,
    CHECK_UNCHECKED,
    EXTERNALLY_MANAGED_STAMPS,
    MANAGED_BY_COMMAND,
    AutoUpdateEffect,
    auto_update_effect,
)
from kiro_crew.platform.update_governance import (
    commits_ahead,
    git_command_env,
    hidden_worktree_edits,
    is_primary_branch,
    loggable_path,
    repo_exec_config_reason,
    resolve_remote_url,
    tracks_upstream,
    update_blocked_reason,
)
from kiro_crew.providers.base import LLMEvent
from kiro_crew.safety_override import flush_breadcrumb_writes, safety_override
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    create_subprocess_limited,
    ensure_agents_slice_limits,
    sandboxed_spawn_argv,
    sandboxed_spawn_argv_async,
    warm_backend,
)
from kiro_crew.security import (
    redact,
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.service.common import restart_command_hint
from kiro_crew.session import (
    HEARTBEAT_KEY,
    SessionBusyError,
    SessionClosingError,
    SessionManager,
)
from kiro_crew.skills import SkillsLoader
from kiro_crew.slack import gateway_runtime as _runtime
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.format import (
    build_cron_ack_block,
    build_options_blocks,
    escape_mrkdwn,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.gateway_runtime import admission as _admission
from kiro_crew.slack.gateway_runtime import channel_lifecycle as _channel_lifecycle
from kiro_crew.slack.gateway_runtime import cron_dispatch as _cron_dispatch
from kiro_crew.slack.gateway_runtime import cron_verdict as _cron_verdict
from kiro_crew.slack.gateway_runtime import delivery as _delivery
from kiro_crew.slack.gateway_runtime import mcp_broker as _mcp_broker
from kiro_crew.slack.gateway_runtime import memory_lifecycle as _memory_lifecycle
from kiro_crew.slack.gateway_runtime import tool_policy as _tool_policy
from kiro_crew.slack.gateway_runtime.channel_lifecycle import (  # noqa: F401
    _channel_transport_permitted,
    _push_observe_limits,
)
from kiro_crew.slack.gateway_runtime.cron_dispatch import (  # noqa: F401
    _CRON_POSTTOKEN_CONTINUE_MSG,
    _CRON_RESERVED_ENV_KEYS,
    _CRON_TRANSIENT_RETRIES,
    CronClaimAbandoned,
    CronClaimTimeDenied,
    CronVetOverran,
    _annotate_model_fallback,
    _await_cron_fire_time_gate,
    _claim_backstop,
    _ClaimHandoff,
    _cron_stream_with_posttoken_resume,
    _defer_cron_before_dispatch,
    _pre_create_cron_slot,
    _vet_at_claim_then,
    claim_vet_bound,
    cron_job_env_without_reserved,
)
from kiro_crew.slack.gateway_runtime.cron_verdict import (  # noqa: F401
    _CRON_FAILURE_DETAIL_CAP,
    _EPOCH_RE,
    _EPOCH_WINDOW_SECS,
    _FAILURE_REMINDER_SECS,
    _NO_RESPONSE,
    _SUCCESS_REMINDER_SECS,
    _VOLATILE_RE,
    _annotate_partial_block,
    _GateTally,
    _result_hash,
)
from kiro_crew.slack.gateway_runtime.tool_policy import (  # noqa: F401
    _BACKGROUND_APPROVAL_SOURCES,
    _HEARTBEAT_STATUS_PREFIXES,
    _READ_ONLY_TOOL_PREFIXES,
    _WRITE_INDICATORS,
    HEARTBEAT_SAFE_TOOLS,
    _bare_tool_name,
    _build_heartbeat_hooks,
    _is_heartbeat_safe_tool,
    _is_read_only_tool,
)
from kiro_crew.slack.handler import (
    _get_agent_for_session,
    build_timing_footer,
    is_thread_incognito,
    is_thread_temporary,
    is_tracked_channel,
)
from kiro_crew.slack.outbound import PostedOptions  # noqa: F401
from kiro_crew.slack.retry import open_dm_with_retry  # noqa: F401
from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels
from kiro_crew.slack.transport import SlackTransport  # noqa: F401
from kiro_crew.subagent import (  # noqa: F401
    _TRANSIENT_CONTINUE_MSG,
    DIGEST_HOLD_SECS,
    INJECTION_TIMEOUT,
    SpawnApprovalUnreachable,
    SubagentDelivery,
    SubagentInfo,
    SubagentManager,
    ToolApprovalCallback,
    _injection_notice_outcome,
    format_subagent_usage,
    resolve_max_subagents,
    stage_boundary_owner_for_run,
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
from kiro_crew.tunnel import set_publish_disabled
from kiro_crew.validation import CHANNEL_ID_RE
from kiro_crew.wecom.gateway import warn_if_channel_uncredentialed

if TYPE_CHECKING:
    from kiro_crew.adaptive.controller import AdaptiveController
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.imessage.client import IMessageClient
    from kiro_crew.memory_startup import MemoryStartup
    from kiro_crew.messaging.registry import ChannelDescriptor
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.subagent_scale import SubagentEventCoalescer
    from kiro_crew.task_models import Task
    from kiro_crew.teams.client import TeamsClient
    from kiro_crew.telegram.client import TelegramClient
    from kiro_crew.webex.client import WebexClient
    from kiro_crew.wecom.client import WeComClient
    from kiro_crew.weixin.client import WeixinClient
    from kiro_crew.whatsapp.client import WhatsAppClient


async def _persist_turn_row(
    client: Any,
    session_key: str,
    *,
    provider: str,
    surface: str,
    agent_fallback: Callable[[], str],
    t0: float,
    usage: AgentTurnUsage | None = None,
) -> None:
    """Persist one per-turn usage row for a background dispatch surface.

    The heartbeat and monitor surfaces — each with a success and a timeout twin —
    share this one implementation rather than each carrying its own copy of the
    block. Best-effort: a persistence failure is logged at debug and never
    propagates into the background loop, since a dropped analytics row must not
    abort a live turn.

    ``agent_fallback`` is a zero-arg callable, invoked INSIDE the try/except and
    only when ``read_effective_agent`` yields nothing — the short-circuit
    (``read_effective_agent(client) or _get_agent_for_session(key)``) keeps a
    cold-cache ``KiroCrewConfig.load()`` from running on every turn and from
    escaping the best-effort guard.

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
            provider_last_turn_usage(client) if usage is None else usage,
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


async def _subagent_work_pending(manager: Any, parent_session_key: str) -> bool:
    """Whether *parent_session_key* still has sub-agents running or QUEUED.

    Asked through ``SubagentManager.has_pending_work_for_async``, whose store
    ``count_pending`` runs on the task store's writer thread: the synchronous
    entry takes the SQLite connection on this loop, and a wait there freezes
    every session's turn behind it. A manager double without the async sibling
    is asked synchronously -- the pre-queue behaviour those doubles model, and
    the same probe ``dashboard.handlers.messaging._spawn_on_loop`` makes for
    ``spawn_async``.
    """
    import inspect

    entry = getattr(manager, "has_pending_work_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(parent_session_key))
    return bool(manager.has_pending_work_for(parent_session_key))


async def _subagent_queued_count(manager: Any, parent_session_key: str) -> int:
    """This parent's QUEUED spawn count, read off-loop like
    :func:`_subagent_work_pending` (which also counts the running ones)."""
    import inspect

    entry = getattr(manager, "queued_count_for_async", None)
    if inspect.iscoroutinefunction(entry):
        return int(await entry(parent_session_key))
    return int(manager.queued_count_for(parent_session_key))


async def _subagent_batch_pending(manager: Any, batch_id: str) -> bool:
    """Whether any member of *batch_id* is still outstanding, read off-loop for
    :func:`_subagent_work_pending`'s reason: the store half of
    ``batch_members_pending`` is a ``fetch_pending_by_batch`` on the connection
    this loop would otherwise block on."""
    import inspect

    entry = getattr(manager, "batch_members_pending_async", None)
    if inspect.iscoroutinefunction(entry):
        return bool(await entry(batch_id))
    return bool(manager.batch_members_pending(batch_id))


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


def _delivery_result(
    wake_message: str | None,
    result: MonitorDispatchResult,
) -> bool | MonitorDispatchResult:
    """Keep legacy bool semantics while structured delivery stays typed."""
    if wake_message is not None:
        return result
    return result is MonitorDispatchResult.DISPATCHED


@dataclass(frozen=True)
class _DmDispatchAdapter:
    """What ONE dispatcher-routed DM channel supplies to the shared fire path.

    A channel whose nudge is delivered by synthesizing an inbound message and
    handing it to that channel's real dispatcher differs from its siblings in
    five places and nowhere else. Everything around those five -- the guard
    ladder, the envelope, the turn timeout, when a loop is retired, and how a
    result is spelled -- is the same reasoning for every such channel, so it
    lives once in :meth:`GatewayOrchestrator._fire_dm_nudge` instead of being
    re-derived per channel.

    ``channel`` keys ``dashboard_state.channel_transports`` and names the
    channel in logs. ``supports_monitor`` says whether the channel can carry a
    structured monitor wake: a channel without one is only ever asked for a
    plain nudge, so it never sees a ``wake_message``. ``authorize`` is the
    fire-time allow-list re-check, which each channel spells against a
    different object. ``resolve_conversation`` answers where the synthetic turn
    lands. ``build_inbound`` mints that channel's own inbound type.

    The three callables take the transport and dispatcher rather than closing
    over them because a channel's transport is resolved per fire, not once at
    construction: a gateway can start, stop and restart one channel while a
    loop stays armed across all of it.
    """

    channel: str
    supports_monitor: bool
    authorize: Callable[[Any, Any, str], bool]
    resolve_conversation: Callable[[Any, Any, str, str], Awaitable[str]]
    build_inbound: Callable[[str, str, str], Any]


# Budget for awaiting the in-flight run-marker write during shutdown. Bounded
# so a stalled write can never eat into GRACEFUL_SHUTDOWN_SECS (which saves
# active slots) — the marker is best-effort, the slot save is not.
_MARKER_WRITE_WAIT_SECS = 5.0


# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000


def _live_session_work_dirs(sessions: Any) -> list[str]:
    """The work directories of every provider the session registry holds now."""
    if sessions is None:
        return []
    return [
        path
        for path in (str(getattr(p, "cwd", "") or "") for p in sessions.active_providers())
        if path
    ]


def _sweep_predecessor_session_work_dirs(live_work_dirs: list[str]) -> int:
    """Sweep the run directories a dead predecessor of this data home left, off-loop.

    The evidence is this home's own: its pid ledger (``retained_gateway_pids``,
    read under the ledger's lock) and *live_work_dirs* from its session
    registry. An unreadable ledger raises, and the callers then sweep nothing.
    """
    from kiro_crew.session_pid import retained_gateway_pids

    return session_work_dir.sweep_predecessor_work_dirs(
        workspace_root(),
        retained_gateway_pids=retained_gateway_pids(),
        live_work_dirs=live_work_dirs,
    )


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


# Prepended to every heartbeat task_text before ``ctx_builder.build_message``.
# Inline injection survives context compaction and webhook-restored sessions
# where skill / system-prompt copies of the same instruction can drift out of
# effective context.
_HEARTBEAT_KEEP_INJECTION = (
    "[HEARTBEAT TASK — you MUST include the keyword HEARTBEAT_KEEP "
    "in your response if this task is NOT complete. Omit the "
    "keyword only when the task is fully complete.]\n\n"
)


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

    Three outcomes, not two. A run with every call refused is a failure and
    is counted. A run with SOME call refused keeps ``last_status = "error"``
    with the refusal as its reason but is counted in neither direction: the
    approved calls prove the job can work, so it must not march toward
    auto-pause, and the lost call means it did not succeed, so it must not
    read as ``ok`` and must not reset a failure streak or the failure-alert
    dedup the way a success does. Only a run with no refusal at all records a
    success.
    """
    if tally.all_blocked:
        # Nothing the model attempted was permitted, so the run accomplished
        # nothing however plausible its reply reads. A success resets
        # consecutive_failures and clears auto_paused, so recording one here
        # would keep a structurally-failing job firing on its schedule forever.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        job.record_failure()
        if job.auto_paused:
            logger.warning(
                "Cron '%s': auto-paused after %d consecutive failures",
                job.name,
                job.consecutive_failures,
            )
        return True
    if tally.partially_blocked:
        # The run did work AND lost work. "error" is the one non-ok status the
        # row, cron_list and the history record understand, and the reason
        # names the refused call. Neither counter moves: not record_failure(),
        # because the approved calls are evidence the job can work; not
        # record_success(), because that would reset a failure streak and the
        # failure-alert dedup on a run that did not fully succeed.
        job.last_status = "error"
        job.last_error = tally.refusal_summary()
        return False
    # Clear failure dedup on any success, regardless of whether the success
    # result itself is a dup. A successful run means the job recovered — next
    # failure should always alert fresh. record_success() owns the reset now, so
    # every kind's success path gets it rather than only this one.
    job.record_success()
    # Same reason record_success resets consecutive_failures: a run that
    # worked proves the job can work, so the shared-death streak that
    # substitutes for that counter is reset with it. Without this the
    # streak is a LIFETIME total, so after enough deaths the bound is
    # permanently tripped and the exemption silently stops applying.
    runtime_death.clear_shared_deaths(f"cron:{job.id}")
    return False


#: Budget for pinning kiro-cli's path before an unattended spawn. The lookup is
#: a handful of `stat` calls, but they are under the home directory and
#: `_warn_if_kiro_cli_outdated` awaits them BEFORE `_init_dashboard` binds its
#: socket — so on an unresponsive network-mounted home an unbounded lookup would
#: keep the gateway from ever coming up. Overrunning the budget refuses the
#: spawn, exactly as an absent binary does.
_KIRO_CLI_RESOLVE_TIMEOUT_SECS = 5.0


async def _pinned_kiro_cli(purpose: str) -> str | None:
    """kiro-cli's absolute path for an unattended spawn, or ``None`` to refuse.

    Neither unattended spawn may exec a bare argv0: the gateway's inherited
    ``PATH`` can lead with an agent-writable directory (a worktree venv's
    ``bin``), and whatever that names would decide the payload. So the candidate
    set is the fixed known install directories plus the operator's own
    ``KIROCREW_KIRO_BIN``, with the inherited ``PATH`` excluded.

    That set does not cover every install: a system-wide one outside
    ``known_kiro_cli_dirs`` — a root-owned ``/usr/local/bin`` on Linux — is
    refused here while sessions keep launching it off ``PATH``. Refusing is the
    right default for a spawn with no operator present, but being SILENT about
    it is not: the resulting host never auto-updates and never warns it is
    outdated, with nothing in the log to say why. Hence the warning naming the
    override, and hence its condition — an install the pin declined is worth a
    line, a backend that simply is not installed is not.

    The sync half is :func:`kiro_crew.kiro_cli.pin_kiro_cli`, shared with the
    CLI's update command and the diagnostics bundle; this wrapper adds only
    what an unattended path on the event loop needs. Off the loop and bounded:
    see :data:`_KIRO_CLI_RESOLVE_TIMEOUT_SECS`.
    """

    try:
        pinned, unpinned_exists = await asyncio.wait_for(
            asyncio.to_thread(pin_kiro_cli),
            timeout=_KIRO_CLI_RESOLVE_TIMEOUT_SECS,
        )
    except (TimeoutError, asyncio.TimeoutError):
        logger.warning(
            "kiro-cli: path lookup exceeded %.0fs (unresponsive home?), skipping %s",
            _KIRO_CLI_RESOLVE_TIMEOUT_SECS,
            purpose,
        )
        return None
    if pinned is None and unpinned_exists:
        logger.warning("%s is skipped: %s.", purpose, PATH_ONLY_INSTALL_NOTE)
    return pinned


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

    # Declared for the type checker only (no value, so no class attribute): each
    # is first assigned in an owner function, which mypy does not read as an
    # attribute declaration the way it reads an assignment in a method.
    _slack_connect_error: str
    _adaptive_start_sub: Subscription | None

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
        # One hoist per channel, in roster order. Each reads the credential
        # store + that channel's config section into the ``_<channel>_*``
        # attributes its ``maybe_start_*`` factory consumes. Kept as methods so
        # ``restart_channel`` can re-run exactly one of them against a fresh
        # config when a connection parameter changes.
        self._hoist_wecom(cfg, creds)
        self._hoist_telegram(cfg, creds)
        self._hoist_weixin(cfg, creds)
        self._hoist_whatsapp(cfg, creds)
        self._hoist_feishu(cfg, creds)
        self._hoist_discord(cfg, creds)
        self._hoist_webex(cfg, creds)
        self._hoist_imessage(cfg, creds)
        self._hoist_teams(cfg, creds)
        self.slack_command = cfg.slack.command
        # Serializes restart_channel: two config writes landing within one
        # poll interval must close-then-start the same channel in order.
        self._channel_restart_lock = asyncio.Lock()
        # Set once _start_channel_transports has run; the restart applier defers
        # before that, because the boot loop is about to start every channel
        # from the hoist above and a concurrent start would double it.
        self._channel_transports_started = False
        self._channel_restart_tasks: set[asyncio.Task[None]] = set()
        self._channel_restart_gen: dict[str, int] = {}
        self._config_subs: list[Subscription] = []
        self._register_config_appliers()

        # Services (initialized in start())
        self.slack: RealSlackClient | None = None
        self.sessions: SessionManager | None = None
        self.ctx_builder: ContextBuilder | None = None
        self.conv_log: ConversationLog | None = None
        self.consolidator: HistoryConsolidator | None = None
        self.cron_svc: CronService | None = None
        self._cron_reconciled = False
        self._cron_armed = False
        self.heartbeat_svc: HeartbeatService | None = None
        # Declared here, not just assigned in `_init_autonudge`: that method
        # returns early when `KIROCREW_AUTONUDGE=0`, BEFORE its only assignment,
        # so with the flag off the attribute never existed at all -- and the
        # seven `if self.autonudge_svc:` sites in the loop-CRUD handlers below
        # would raise AttributeError rather than read a default.
        self.autonudge_svc: AutoNudgeService | None = None
        # Secretary runtime service removed (Amazon-internal). Attribute stays
        # as an inert None so other modules referencing it degrade gracefully.
        self.secretary_svc: object | None = None
        self.subagent_mgr: SubagentManager | None = None
        # Adaptive concurrency controller: started by _start_adaptive_controller
        # after dashboard and task-store readiness; stopped in shutdown before
        # the manager is cancelled so no actuator fires into a closing manager.
        self._adaptive_controller: AdaptiveController | None = None
        self._subagent_coalescer_inst: "SubagentEventCoalescer | None" = None
        # Wave accounting for the completion digest (batch_id -> progress).
        self._batch_progress: dict[str, dict] = {}
        self._cron_injecting: dict[str, int] = {}  # parent_key → pending injection count
        self._running_script_ids: set[str] = (
            set()
        )  # job IDs with in-flight script/command execution
        self.task_runner: TaskRunner | None = None
        # Runner admission over the task queue, attached by _wire_runner_admission
        self._runner_admission: Any = None
        # Whether the consumers' orphan-adoption sweep has already been handed
        # out over a store that existed. The first wiring pass can run while the
        # store is still opening off the loop, and adopting nothing is not the
        # same as having adopted: _runner_admission_store_ready re-attaches then,
        # and this keeps that from becoming a SECOND sweep over the same rows.
        self._runner_admission_adopted = False
        self.channel_history: ChannelHistory | None = None
        self.dashboard_state: DashboardState | None = None
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks
        # Approval-state persistence is scheduled while the MCP broker starts,
        # but its task waits for the dashboard/API readiness boundary.
        self._mcp_launch_approval_ready = asyncio.Event()
        self._memory_startup: MemoryStartup | None = None
        self._memory_startup_task: asyncio.Task | None = None
        self._memory_repair_task: asyncio.Task | None = None
        self._memory_repair_stop = threading.Event()
        self._memory_repair_cursor = ""
        # Dedicated ownership for the repair's dep_sync/pip process tree. The
        # general set only prevents task GC; shutdown must cancel and await this
        # task so _check_console_script can kill and reap its child group.
        self._console_script_repair_task: "asyncio.Task[None] | None" = None
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
        # by messaging.registry.start_channels until the config schema retires
        # them; shutdown closes through THIS dict.
        self._channel_handles: dict[str, object] = {}
        # Detached boot task that replays the durable inbound spool.
        # Held on the instance so the task is not garbage-collected mid-flight.
        self._inbound_replay_task: "asyncio.Task[None] | None" = None
        self._model_download_task: "asyncio.Task[bool] | None" = None
        self._feature_video_task: "asyncio.Task[bool] | None" = None
        self._auto_migrate_task: "asyncio.Task[None] | None" = None
        # Recurring update coordinator, started after signal handlers are
        # installed. Cancelled on shutdown so stalled network or installer work
        # cannot hold the process open.
        self._update_check_task: "asyncio.Task[None] | None" = None
        self._update_apply_deferred = False
        self._pending_update_respawn: Callable[[], str] | None = None
        self._mandatory_update_deferred_at: float | None = None
        self._mandatory_update_deferred_key: str | None = None
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
        # and must never be called from an async path. The store
        # lives beside the rest of the data home for the life of the process.

        self._mcp_resolve_home: str = str(config_dir())

    def _in_flight_work_counts(self) -> tuple[int, int]:
        """Return ``(turns, background)`` that a restart would interrupt.

        Unknown provider state is classified as background so a mandatory
        cooperative turn drain never cancels work whose ownership is unclear.
        """
        turns = 0
        background = 0
        state = self.dashboard_state
        if state is not None:
            try:
                for provider in state.sessions.active_providers():
                    checker = getattr(provider, "has_active_turn", None)
                    if not callable(checker):
                        background += 1
                        continue
                    try:
                        if checker():
                            turns += 1
                    except Exception:
                        logger.warning("in-flight count: provider turn state failed", exc_info=True)
                        background += 1
            except Exception:
                logger.warning("in-flight count: active_providers() failed", exc_info=True)
                background += 1

            slots = getattr(state, "_slots", None)
            if isinstance(slots, dict):
                seen_slot_tasks: set[int] = set()
                for slot in list(slots.values()):
                    task = getattr(slot, "task", None)
                    if task is not None:
                        marker = id(task)
                        if marker in seen_slot_tasks:
                            continue
                        seen_slot_tasks.add(marker)
                        try:
                            if not task.done():
                                turns += 1
                        except Exception:
                            logger.warning(
                                "in-flight count: dashboard slot task failed", exc_info=True
                            )
                            background += 1
                    elif bool(getattr(slot, "_in_stage_execution", False)):
                        # The Python stage loop remains live between stage turns
                        # while slot.task is intentionally momentarily None.
                        background += 1

            workflows = getattr(state, "workflow_service", None)
            if workflows is not None:
                try:
                    background += sum(
                        1
                        for run in workflows.list_runs()
                        if isinstance(run, dict) and run.get("status") == "running"
                    )
                except Exception:
                    logger.warning("in-flight count: workflow listing failed", exc_info=True)
                    background += 1

        for task in list(self._session_tasks.values()):
            if not task.done():
                turns += 1

        sessions = self.sessions
        if sessions is not None:
            try:
                background += max(0, int(sessions.inbound_callback_count))
            except Exception:
                logger.warning("in-flight count: inbound callback state failed", exc_info=True)
                background += 1
        try:
            background += max(0, int(inbound_spool.pending_refusal_write_count()))
        except Exception:
            logger.warning("in-flight count: refusal write state failed", exc_info=True)
            background += 1

        seen_handler_tasks: set[int] = set()
        handler_owners: list[object] = [
            self,
            *getattr(self, "_channel_handles", {}).values(),
        ]
        handler_owners.extend(
            peer
            for owner in list(handler_owners)
            if (peer := getattr(owner, "_peer", None)) is not None
        )
        for owner in handler_owners:
            tasks = getattr(owner, "_handler_tasks", None)
            if not isinstance(tasks, (set, list, tuple)):
                continue
            for task in list(tasks):
                marker = id(task)
                if marker in seen_handler_tasks:
                    continue
                seen_handler_tasks.add(marker)
                try:
                    if not task.done():
                        background += 1
                except Exception:
                    logger.warning("in-flight count: channel handler state failed", exc_info=True)
                    background += 1

        subagents = self.subagent_mgr
        if subagents is not None:
            try:
                background += max(0, int(subagents.pending_work_count))
            except Exception:
                logger.warning("in-flight count: subagent state failed", exc_info=True)
                background += 1

        background += len(self._running_script_ids)

        runner = self.task_runner
        if runner is not None:
            try:
                if runner.running:
                    background += 1
            except Exception:
                logger.warning("in-flight count: TaskRunner state failed", exc_info=True)
                background += 1
        return turns, background

    def _count_in_flight_work(self) -> int:
        """Count all gateway work that an automatic restart would interrupt."""
        turns, background = self._in_flight_work_counts()
        return turns + background

    def _live_update_handler_tasks(self) -> list[asyncio.Task[Any]]:
        """Return deduplicated channel handler tasks the restart must preserve."""
        current = asyncio.current_task()
        seen: set[int] = set()
        pending: list[asyncio.Task[Any]] = []
        state = self.dashboard_state
        slots = getattr(state, "_slots", None) if state is not None else None
        if isinstance(slots, dict):
            for slot in list(slots.values()):
                task = getattr(slot, "task", None)
                if task is None or task is current:
                    continue
                marker = id(task)
                if marker in seen:
                    continue
                seen.add(marker)
                if not task.done():
                    pending.append(task)
        background_tasks = getattr(state, "_background_tasks", None) if state is not None else None
        if isinstance(background_tasks, (set, list, tuple)):
            for task in list(background_tasks):
                if task is current or not callable(getattr(task, "get_name", None)):
                    continue
                if not task.get_name().startswith("dashboard-stage:"):
                    continue
                marker = id(task)
                if marker in seen:
                    continue
                seen.add(marker)
                if not task.done():
                    pending.append(task)

        owners: list[object] = [self, *getattr(self, "_channel_handles", {}).values()]
        owners.extend(
            peer for owner in list(owners) if (peer := getattr(owner, "_peer", None)) is not None
        )
        for owner in owners:
            tasks = getattr(owner, "_handler_tasks", None)
            if not isinstance(tasks, (set, list, tuple)):
                continue
            for task in list(tasks):
                marker = id(task)
                if marker in seen or task is current:
                    continue
                seen.add(marker)
                if not task.done():
                    pending.append(task)
        return pending

    async def _drain_update_callback_work(self, *, timeout: float | None) -> bool:
        """Wait without cancellation until callbacks and refusal writes are durable."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        while True:
            handlers = self._live_update_handler_tasks()
            writes = list(inbound_spool.pending_refusal_writes())
            reservations = 0
            if self.sessions is not None:
                try:
                    reservations = max(0, int(self.sessions.inbound_callback_count))
                except Exception:
                    logger.warning("update restart: callback count unavailable", exc_info=True)
                    return False
            if not handlers and not writes and reservations <= 0:
                # Let callbacks already queued on the loop either reserve or
                # observe the closed gate, then recheck. The caller commits the
                # synchronous restart fence immediately after this returns.
                await asyncio.sleep(0)
                handlers = self._live_update_handler_tasks()
                writes = list(inbound_spool.pending_refusal_writes())
                reservations = (
                    max(0, int(self.sessions.inbound_callback_count))
                    if self.sessions is not None
                    else 0
                )
                if not handlers and not writes and reservations <= 0:
                    return True
            remaining = None if deadline is None else deadline - loop.time()
            if remaining is not None and remaining <= 0:
                return False
            awaitables = [*handlers, *writes]
            if awaitables:
                _done, still_pending = await asyncio.wait(awaitables, timeout=remaining)
                if still_pending and deadline is not None and loop.time() >= deadline:
                    return False
            else:
                await asyncio.sleep(0.01 if remaining is None else min(0.01, remaining))

    def _live_internal_secret(self) -> str:
        """This gateway's LIVE internal-API secret, for script-cron ``notify()``.

        The dashboard mints the secret at startup and the auth middleware
        compares against that same in-memory value (``app["local_secret"]``).
        A script cron's child authenticates ``/api/send-message`` with the
        secret it is handed at spawn, so the in-process scheduler hands it THIS
        value rather than one re-derived from the environment or a per-port
        file — a stale ``KIROCREW_INTERNAL_SECRET`` or ``.secret`` file
        otherwise wins the derivation and 403s every ``notify()``.

        Returns ``""`` when no dashboard has started (``--no-dashboard`` /
        API-only), so ``run_script_sandboxed`` falls back to env/file
        derivation. Read-only and in-process only: the value is never logged or
        returned over HTTP.
        """
        runner = self._dashboard_runner
        if runner is None:
            return ""
        try:
            return runner.app.get("local_secret", "") or ""
        except Exception:
            return ""

    _UPDATE_BUSY_RETRY_SECS = 300.0
    _MANDATORY_UPDATE_MAX_DEFER_SECS = 600.0
    #: How long the pre-restart drain waits for callbacks and refusal writes to
    #: become durable before giving up and deferring the restart.
    #:
    #: Named rather than inlined at the call site so a test that is about the
    #: fetch/reset/venv SEQUENCE can shorten it. When nothing makes the drain
    #: condition true, ``_drain_update_callback_work`` polls at 10ms to the
    #: deadline, so an inlined 30.0 cost eleven such tests thirty seconds EACH --
    #: ~330s of pure sleeping per full suite run, asserted on by none of them.
    #: The value itself stays pinned by the test that IS about it
    #: (``test_restart_fences_then_closes_and_final_drains`` asserts
    #: ``drain:30.0``), so shortening it elsewhere cannot hide a change here.
    _UPDATE_DRAIN_TIMEOUT_SECS = 30.0

    async def _prepare_auto_update_apply(
        self,
        *,
        mandatory: bool,
        mandatory_key: str = "",
    ) -> bool:
        """Pause admission and reach an idle boundary before automatic apply."""
        if not mandatory:
            self._mandatory_update_deferred_at = None
            self._mandatory_update_deferred_key = None
        sessions = self.sessions
        if sessions is None:
            return True
        try:
            paused = await sessions.pause_turn_admission_for_update()
        except Exception:
            logger.exception("Auto-update could not pause turn admission")
            self._update_apply_deferred = True
            return False
        if not paused:
            self._update_apply_deferred = True
            return False

        turn_busy, background_busy = self._in_flight_work_counts()
        busy = turn_busy + background_busy
        if busy <= 0:
            self._mandatory_update_deferred_at = None
            self._mandatory_update_deferred_key = None
            return True

        loop = asyncio.get_running_loop()
        now = loop.time()
        if mandatory:
            target_key = mandatory_key or "mandatory"
            if self._mandatory_update_deferred_key != target_key:
                self._mandatory_update_deferred_key = target_key
                self._mandatory_update_deferred_at = now
            elif self._mandatory_update_deferred_at is None:
                self._mandatory_update_deferred_at = now
        mandatory_due = bool(
            mandatory
            and self._mandatory_update_deferred_at is not None
            and now - self._mandatory_update_deferred_at >= self._MANDATORY_UPDATE_MAX_DEFER_SECS
        )
        if mandatory_due:
            logger.warning(
                "Mandatory update remains deferred after its grace period: "
                "%d active turn(s), %d background workload(s)",
                turn_busy,
                background_busy,
            )

        await sessions.resume_turn_admission_after_update()
        self._schedule_inbound_replay()
        self._update_apply_deferred = True
        logger.info("Auto-update deferred: %d in-flight turn(s)", busy)
        if self.dashboard_state:
            self.dashboard_state.push_refresh("update_available")
        return False

    async def _finish_auto_update_apply(self) -> None:
        """Reopen admission when apply returns instead of replacing the process."""
        sessions = self.sessions
        if sessions is None:
            return
        try:
            await sessions.resume_turn_admission_after_update()
        except Exception:
            logger.exception("Auto-update could not resume turn admission")
        else:
            self._schedule_inbound_replay()

    # ------------------------------------------------------------------
    # Tool approval callback (shared by cron, heartbeat, subagent, task)
    # ------------------------------------------------------------------

    def _dashboard_client_attached(self) -> bool:
        """Report whether an attached dashboard client could answer a prompt NOW.

        Asked at exactly one place: the dashboard-only fallback in
        ``_interactive_approval``. Reaching it means Slack has already had its
        turn — either no owner DM was configured, or posting the prompt to it
        raised and the callback fell through — so the question left is only about
        the dashboard, and a Slack term here would report a surface that
        demonstrably did NOT receive the prompt.

        Counts DASHBOARD-USER sockets, not every ``/api/ws`` registration. An app
        token registers as a client too, and the broadcast chokepoint sends it an
        owner-surface frame only if its manifest declared that event -- so an open
        app UI is not somebody who can answer the prompt, for the same reason a
        configured-but-failed Slack DM is not.

        Deliberately narrow, because a false "attached" merely restores today's
        behaviour while a false "detached" refuses a spawn a human WOULD have
        approved:

        * an unreadable client count is treated as attached;
        * no ``dashboard_state`` at all is genuinely no surface, but the caller
          reaches its own no-UI branch before asking, so that answer is never
          used to refuse anything.

        A relay reader is not a false positive here: it consumes the SSE stream
        (``dashboard/remote_mirror``), never registers on ``/api/ws``, and so is
        not in ``_ws_clients`` at all.
        """
        if self.dashboard_state is None:
            return False
        try:
            return int(self.dashboard_state.dashboard_user_ws_count()) > 0
        except Exception:
            logger.debug(
                "dashboard_user_ws_count failed; treating the dashboard as attached",
                exc_info=True,
            )
            return True

    def _interactive_approval(
        self,
        source: str,
        slot_resolver: Callable[[str], str] | None = None,
        nudge_key: str = "",
        *,
        raise_when_unreachable: bool = False,
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

        ``raise_when_unreachable`` opts this callback into raising
        ``SpawnApprovalUnreachable`` instead of parking on a prompt no surface
        received. Only the spawn gate sets it, because only the spawn gate has a
        terminal path that can report the refusal to the calling agent; a mid-run
        tool approval parks on the prompt instead.
        """

        is_background = source in _BACKGROUND_APPROVAL_SOURCES

        async def _approve(event: LLMEvent, parent_session_key: str = "") -> bool:
            request_id = str(event.request_id)
            # Low-fidelity CHILD request: the structured security context is
            # absent, so every field a content-matching shortcut below would
            # judge (title, read-only classification, trust patterns) is
            # agent-authored. Unless its canonical MCP identity is verified
            # (``_child_grant_eligible`` below), such a request may ONLY be
            # approved by the human prompt at the end of this callback —
            # every non-human auto-approve shortcut (auto_approve_sources,
            # --approval yolo/reads, YOLO override, slot trust) is skipped
            # for it. Strict ``is True``: real events
            # (AcpEvent) return a genuine bool; anything else (e.g. a mock
            # or a foreign event type) must not accidentally enter the
            # restricted path on a truthy non-bool.
            _child_lf = getattr(event, "child_low_fidelity", False) is True
            # Hoisted grant-eligibility — see
            # AcpEvent.child_unconditional_grant_eligible for which shortcuts
            # below may honor it (per-source auto-approve, --approval yolo,
            # the YOLO override, slot trust) and which must not (the 'reads'
            # mode MATCHES the agent-authored title). The outer
            # ``not _child_lf`` short-circuit keeps a foreign event type or
            # mock — which never entered the restricted path via the strict
            # ``_child_lf`` probe — eligible without consulting an attribute
            # it may not have; the property is only reached for a genuinely
            # low-fidelity event, with the same strict ``is True`` rationale
            # as ``_child_lf``.
            _child_grant_eligible = (not _child_lf) or (
                getattr(event, "child_unconditional_grant_eligible", False) is True
            )
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
                if not _child_grant_eligible:
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
            # 'yolo' is an UNCONDITIONAL grant (consumes no event data) so a
            # verified-identity child qualifies; 'reads' classifies the
            # agent-authored TITLE, so it requires the composite fidelity.
            if self._approval_mode in ("yolo", "reads") and _child_grant_eligible:
                approve = self._approval_mode == "yolo" or (
                    self._approval_mode == "reads"
                    and not _child_lf
                    and _is_read_only_tool(event.title or "")
                )
                if approve and self._approval_mode == "reads":
                    # 'reads' is a NAME-shaped grant: it classifies the title,
                    # and the shell resolves the command's program names again
                    # through a PATH that can lead with agent-writable
                    # directories — the same tier the dashboard's trust-reads
                    # rung verifies. A refused name falls through to the
                    # interactive prompt below (never a hard block), so a
                    # PATH-shadowed program cannot ride the reads grant on an
                    # unattended cron/autonudge turn. 'yolo' is unconditional
                    # (consumes no event data) and stays unverified by design.
                    _ng_refusal = await name_grant.refusal_for_event(event)
                    if _ng_refusal is not None:
                        logger.warning(
                            "declining a reads-mode auto-approve: %s; the "
                            "request falls through to the interactive prompt",
                            _ng_refusal.log_text,
                        )
                        name_grant.log_decline(
                            source="background",
                            session_key=parent_session_key,
                            event=event,
                            refusal=_ng_refusal,
                            tier="cli_approval_reads",
                            metadata={"caller_source": source},
                            sel_factory=sel,
                        )
                        approve = False
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
            if safety_override().is_active() and _child_grant_eligible:
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
                    # The same verdict the slot's own tool approvals take: the
                    # human's session flag OR a live SafetyOverride scoped grant,
                    # re-checked here per request and never renewed from here.
                    _ps_trusted = bool(_ps) and _slot_is_trusted(_ps)
                    _ps_via_scope = (
                        _ps_trusted and _auto_approve_reason(_ps, False) == "trust_scope"
                    )
                    if _ps_trusted and not _child_grant_eligible:
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
                    elif _ps_via_scope:
                        _sel_log(
                            caller=f"slot:{_parent_slot_key}",
                            operation=f"{source}.trust_scope_auto_approve",
                            outcome="ok",
                            resources=f"scope:{getattr(_ps, '_trust_scope', '')} {_safe_title}",
                        )
                        return True
                    elif _ps_trusted:
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
                # The single park-with-nobody-attached point. Every non-human
                # shortcut above has already been evaluated and skipped, and the
                # Slack branch either was not taken or fell through after failing
                # to post — so "nobody received this prompt" is now the only
                # remaining reading, which is what makes the check sound here and
                # nowhere else.
                if raise_when_unreachable and not self._dashboard_client_attached():
                    raise SpawnApprovalUnreachable("no dashboard client is connected")
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
        safe_title = redact(title)

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
            *platform_compat.isolated_python_argv(
                "-m",
                "pip",
                "install",
                "--quiet",
                *missing,
            ),
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
            logger.error("Dep repair failed: %s", dep_err[-500:])

    async def _check_console_script(self) -> None:
        """Repair a venv whose ``kirocrew`` console script went missing.

        ``_check_missing_deps`` catches a git-reset-without-pip-install that left
        an import missing, but not the failure mode where an interrupted venv
        rebuild (e.g. a Python-version bump that reran ``python -m venv`` + a
        killed ``pip install -e``) leaves a venv with a working interpreter but
        no ``kirocrew`` entry point — the gateway then dies later with an
        exit-127 "binary not found". This closes that gap at startup: if the
        recorded pip install has no executable console script, run the same
        in-place editable reinstall ``dep_sync`` uses, which is the one operation
        that rewrites the entry point.

        Scoped to pip installs of a real project dir: a Brazil install owns its
        own entry point, and an empty ``KIROCREW_PROJECT_DIR`` means there is no
        checkout to reinstall from.
        """
        proj = os.environ.get("KIROCREW_PROJECT_DIR", "")
        if not proj or self._is_brazil_install(proj):
            return
        method_file = Path(proj) / ".install-method"
        if not (method_file.is_file() and method_file.read_text().strip() == "pip"):
            return
        # The venv interpreter under the project, platform-aware (Scripts on
        # Windows, bin on POSIX), shared with dep_sync's ownership exception.
        venv_py = dep_sync.project_venv_python(Path(proj))
        script = dep_sync.console_script_path(venv_py)
        if script.exists() and os.access(script, os.X_OK):
            return

        logger.warning(
            "kirocrew console script missing/not executable at %s — reinstalling", script
        )
        print("👻 Repairing kirocrew install (console script missing)…")
        # Run the stdlib-only module by absolute file path: the target venv may
        # not currently contain an importable kiro_crew package. A dedicated
        # child session lets cancellation own pip and its build descendants.
        dep_sync_file = dep_sync.__file__
        if dep_sync_file is None:
            raise RuntimeError("dep_sync module has no source path")
        # Route through the sandbox chokepoint: this child EXECUTES
        # ``venv_py`` (dep_sync probes the target interpreter via
        # ``installed_package_origin``), and that interpreter lives in the
        # project checkout, so its bytes are not ours to trust. The sandbox
        # gives the whole repair subtree filesystem isolation plus a
        # credential-scrubbed env, and ``create_subprocess_limited`` adds the
        # kernel resource ceiling. ``mode="strict"`` hides the credential dirs
        # AND ``.ssh`` while the untrusted interpreter runs -- the tightest
        # tier, chosen because the child executes bytes we do not trust. It
        # still leaves the project and its venv writable (pip must rewrite the
        # entry point) and the network open (pip must reach the index);
        # ``scrub_env`` is mode-independent, so ``PIP_INDEX_URL`` / proxy / SSL
        # vars survive. ``strip_python_env`` stops an inherited PYTHONPATH from
        # satisfying the child's import probe from outside the venv. No
        # ``extra_writable_dirs``: the project is already writable, and a
        # carve-out outside the sealed runtime parent would be refused anyway.
        try:
            argv, env, cleanup = await sandboxed_spawn_argv_async(
                platform_compat.isolated_python_argv(
                    str(Path(dep_sync_file).resolve()),
                    "--repair-missing-package",
                    str(proj),
                    str(venv_py),
                    force_isolation=True,
                ),
                mode="strict",
                env=os.environ.copy(),
                strip_python_env=True,
                _prepare=sandboxed_spawn_argv,
            )
        except SandboxUnavailableError as exc:
            # Fail CLOSED. Running an untrusted interpreter unsandboxed is the
            # exposure this routing exists to remove, so a host with no sandbox
            # backend does not get the repair -- it gets a named reason instead
            # of a silent no-op, leaving the pre-existing manual path. The typed
            # ``kind``/``detail`` are logged rather than an inferred English
            # guess: this PR exists to make this failure class diagnosable.
            print("❌ kirocrew reinstall skipped (no sandbox available) — run: kirocrew update")
            logger.error(
                "Console-script repair skipped: sandbox unavailable (kind=%s): %s — "
                "refusing to run the project venv interpreter unsandboxed",
                exc.kind,
                exc.detail,
            )
            return
        try:
            proc = await create_subprocess_limited(
                *argv,
                cwd=proj,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._DEP_INSTALL_TIMEOUT_SECS
                )
            except (TimeoutError, asyncio.TimeoutError):
                await self._kill_startup_child(proc)
                await self._reap_startup_child(proc)
                print("❌ kirocrew reinstall timed out — run manually: kirocrew update")
                logger.error(
                    "Console-script reinstall timed out after %.0fs",
                    self._DEP_INSTALL_TIMEOUT_SECS,
                )
                return
            except asyncio.CancelledError:
                await self._kill_startup_child(proc)
                await self._reap_startup_child(proc)
                raise
        finally:
            if cleanup:
                Path(cleanup).unlink(missing_ok=True)

        if proc.returncode != 0:
            detail = b"\n".join(part for part in (stdout, stderr) if part).decode(
                "utf-8", errors="replace"
            )
            detail, _ = redact_exfiltration_urls(detail)
            detail, _ = redact_credentials(detail)
            print("❌ kirocrew reinstall failed — run manually: kirocrew update")
            logger.error("Console-script reinstall failed: %s", detail[:500])
        else:
            print("✅ kirocrew console script restored")

    def _schedule_console_script_repair(self) -> asyncio.Task[None]:
        """Run the console-script repair after the HTTP socket has bound.

        The healthy path is a few filesystem probes, but repair can spend the
        full pip timeout in its owned child process. Keep a strong reference so
        the task is observable and shutdown cancellation reaches that child.
        """
        existing = self._console_script_repair_task
        if existing is not None and not existing.done():
            return existing

        async def _repair() -> None:
            try:
                await self._check_console_script()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Console-script check failed", exc_info=True)

        task = asyncio.create_task(_repair())
        self._console_script_repair_task = task
        self._background_tasks.add(task)

        def _clear(done: asyncio.Task[None]) -> None:
            self._background_tasks.discard(done)
            if self._console_script_repair_task is done:
                self._console_script_repair_task = None

        task.add_done_callback(_clear)
        return task

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

        Pinned the same way the auto-update pins it, via `_pinned_kiro_cli`:
        this probe runs unattended at boot, so a shim planted on `PATH` would
        execute here regardless of the `--version` argument. A binary the pin
        refuses has no version worth warning about — and the pin logs why.
        """
        kiro_cli_bin = await _pinned_kiro_cli("the kiro-cli version check")
        if kiro_cli_bin is None:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                kiro_cli_bin,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                # Own process group (POSIX; no-op on Windows) so the tree kill
                # in `_kill_startup_child` below reaches any descendants, not
                # just the direct child: without a session of its own the group
                # signal has nothing to address beyond the child's PID, so the
                # kill+reap arms below could not reach a descendant.
                start_new_session=platform_compat.IS_POSIX,
            )
        except Exception:
            return  # binary missing/unspawnable — stay silent
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
            # contract is "never raises", so a blanket except swallows them).
            # Kill+reap best-effort and continue.
            await self._kill_startup_child(proc)
            await self._reap_startup_child(proc)
            logger.debug("kiro-cli --version probe failed", exc_info=True)
            return
        try:
            if proc.returncode == 0:
                version = parse_kiro_cli_version(out.decode(errors="replace"))
                if version is not None and version[:2] < (1, 26):
                    major, minor = version[0], version[1]
                    print(
                        f"⚠️  kiro-cli {major}.{minor} is outdated (1.26+ required). "
                        "Update kiro-cli, or use the default claude-agent-acp backend."
                    )
        except Exception:
            pass  # unparseable version output — stay silent

    async def _init_services(self) -> None:
        """Initialize memory, skills, hooks, context, history, sessions.

        Async so the kiro-cli version probe and pip dep repair run off the
        event loop. Memory restore, database initialization and FTS rebuilding
        belong to the tracked worker started after dashboard readiness. Object
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

            # Off-loop, with the fork refresh deferred: the rebuild gates
            # readiness, and per-fork work scales with fork count.
            # rebuild_agent_config owns the deferral (skip + background
            # schedule together), so this caller cannot get half the pair.
            path = await asyncio.to_thread(rebuild_agent_config, refresh_forks="defer")
            logger.info("Agent config installed: %s", path)

            # Deliver shim + one-time stale-MCP purge automatically — the
            # desktop app launches the gateway but never runs `kirocrew setup`.
            #
            # Routed through the CPP seam (``AgentRuntime.run_first_run_setup``)
            # rather than importing ``agent.run_first_run_setup`` directly, so
            # first-run setup is genuinely extensible: an edition composes an
            # adapter that adds its own one-time provisioning on top. The
            # ``DefaultAgentRuntime`` delegates to exactly the same
            # ``agent.run_first_run_setup()`` a direct import would call, so the
            # standalone build is behaviorally identical (asserted in
            # test_cpp_wiring_standalone).
            #
            # ``safe_context_call`` keeps a transient adapter error from breaking
            # startup (``fallback=None`` matches the seam's ``-> None`` contract),
            # matching the best-effort posture of this block.
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

        # Only in-memory wiring belongs on the boot path. The closed barrier
        # refuses memory consumers until the preparation worker has restored and
        # initialized every shared tier. No old store is opened in the meantime.
        from kiro_crew.memory_startup import MemoryStartup

        self._memory_startup = MemoryStartup.begin()

        # Any telemetry.* write rebuilds the metrics recorder, so a field other than
        # `enabled` (retention, export interval, OTLP endpoint) is not frozen at
        # first use.
        try:
            from kiro_crew.dashboard.chat_persistence import (
                watch_config as _watch_entry_cache_config,
            )
            from kiro_crew.metrics.provider import watch_config as _watch_telemetry_config

            _watch_telemetry_config()
            # Entry-cache bounds are memoised on the chat save path; the watcher
            # drops the memo so a new bound applies to the next flush.
            _watch_entry_cache_config()
        except Exception:
            logger.debug("service config watchers not fully registered", exc_info=True)

        # Memory, skills, hooks, lessons
        memory = MemoryStore()

        # Vector memory (structured semantic store)
        from kiro_crew.vector_memory import VectorMemoryStore

        self.vector_memory = VectorMemoryStore(
            embedding_dim=self._cfg.memory.embedding_dim, config=self._cfg
        )
        # Preserve one object for context, consolidation and the dashboard;
        # its database opens only in the authorized preparation worker.
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
        # Follow config.json's hooks section live. The heartbeat-scoped manager is
        # re-derived from this one per cycle, so it inherits the reload without
        # subscribing (and must not, see HookManager.watch_config).
        hooks.watch_config()
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

        # Same expiry paths, the other direction: a parent with a completion
        # injection in flight must not be expired under it. This counter is the
        # only witness in the window between committing the injected turn and
        # acquiring the session, which is why the three reset sites in this file
        # consult it too.
        self.sessions.set_injection_probe(
            lambda key: self._cron_injecting.get(key, 0) > 0,
        )

        # Channel history buffer. data_home(), not config_dir(): this method is
        # async and config_dir() re-runs start-of-process maintenance (mkdir,
        # breadcrumb refresh, archive sweep) on every call.
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

        # FTS rebuild is part of deferred memory initialization: it scans user
        # history and must complete before memory is released to consumers.

    # gateway_runtime/delivery.py
    _open_dm_with_retry = _delivery._open_dm_with_retry

    _record_cron_delivery = _delivery._record_cron_delivery

    _remember_options = _delivery._remember_options

    async def _deliver_cron_response(
        self, parent_key: str, text: str, *, silent: bool = False
    ) -> bool:
        """Deliver a cron session's post-subagent response to its own channel.

        When a cron session spawns subagents via ``spawn_run``, the agent's
        synthesized response would otherwise only be appended to the dashboard
        notification body, making subagent delegation invisible in cron
        contexts. Two legs carry it, in this order:

        1. the channel that scheduled the job, resolved from the job's origin
           session key (:meth:`_cron_origin_key`) and delivered through the
           governed transport ladder;
        2. Slack, the channel/thread the cron originally posted in (stored on
           the session at delivery time), falling back to the owner's DM.

        The channel leg is tried FIRST and, when it delivers, it is the only leg:
        a job belongs to the conversation that scheduled it, so adding a Slack
        owner DM on top would notify one operator twice for one response. Slack
        remains the delivery for a Slack-origin, dashboard-origin or
        origin-less job, which is every job an install carries today. No-op when
        silent or when the text is blank. Returns True when a leg delivered; a
        False leaves the caller's dashboard notification as the only surface.
        """
        if silent or not text.strip():
            return False
        assert self.sessions is not None
        # Resolved before the Slack leg so a Slack-less install still delivers.
        delivered = await self._deliver_cron_to_channel(
            self._cron_origin_key(parent_key), text, actor_key=parent_key
        )
        # One surface per response. The channel that scheduled the job is the one
        # its owner is watching, so a Slack owner DM on top of it is a duplicate
        # rather than a second audience.
        if self.slack is None or delivered:
            return delivered
        channel = self.sessions.get_channel(parent_key)
        thread_ts = self.sessions.get_thread(parent_key)
        if not channel and self._owner_id:
            channel = await self._open_dm_with_retry(self._owner_id, parent_key)
            thread_ts = None  # a thread_ts from another channel is invalid in a DM
        if not channel:
            logger.warning("Cron %s: no channel resolved for subagent response", parent_key)
            # Not False: the channel leg above may already have delivered, and
            # reporting a drop would let the caller log one that did not happen.
            return delivered
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

    # gateway_runtime/delivery.py
    _channel_reply_link = _delivery._channel_reply_link

    async def _deliver_channel_reply(
        self,
        parent_key: str,
        text: str,
        *,
        resolved_link: tuple[ChannelLink, bool] | None = None,
        caller: str = "subagent",
    ) -> bool:
        """Deliver unattended output to the non-Slack channel conversation behind a session.

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

        ``caller`` names the producer on the SEL trail and in the logs. Two
        surfaces share this leg (subagent completions and cron runs), and an
        allow-list decision recorded against the wrong one is an audit trail
        that points at a principal which never made the send.

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
            logger.exception(
                "%s reply: channel target resolution failed for %s", caller, parent_key
            )
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
                    caller=caller,
                    operation=f"{caller}.reply_target_resolve",
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
            #
            # Wrapped in the DISPLAY-form floor, because this is the chokepoint
            # every proactive channel egress passes (cron results, cron failure
            # and crash alerts, subagent completions) and NONE of them passes a
            # renderer -- the renderers are where a turn gets that floor. A
            # literal-only scan here would let a markdown-collapse credential
            # (`AKIA**...**`, which the client renders whole) reach the channel,
            # and every caller would inherit that gap rather than each one
            # carrying the floor itself.
            # ``redact_via_context`` stays the redactor rather than the neutral
            # ``display_safe``: it is context-aware, and the shared sink's default
            # pair would silently drop that.
            #
            # Trailing control-tag lines are stripped FIRST: this is the
            # proactive egress chokepoint for cron results and subagent
            # completions authored under dashboard rules, and Slack renders
            # HTML comments literally. Strip-then-redact matches display_safe.
            safe_text, _ = redact_for_display(strip_control_comments(text), redact_via_context)
            # ``chunk_for_transport``: the transport's OWN unit (bytes for a
            # byte-capped channel like Webex, chars otherwise) and fence-safe on
            # both paths. A blind slice through a code block leaves part two with
            # no opener, so every line reads as prose and a channel's dialect
            # converter rewrites the `**`, `#` and `- ` INSIDE the code -- a
            # sub-agent's diff or log dump is exactly that shape. The shared
            # splitter seals each chunk with a synthetic closer and reopens the
            # next with the original opener line.
            # ``redactor=redact_via_context``: the seam check must grade with the
            # SAME context-aware pair the display pass above uses, not the shared
            # sink's default -- a companion-contributed credential split across a
            # transport seam is invisible to the narrower default pair and would
            # ship whole across two adjacent messages.
            parts = await asyncio.to_thread(
                chunk_for_transport,
                safe_text,
                transport.capabilities,
                redactor=redact_via_context,
            )
            for part in parts:
                # Stop on the first UNCONFIRMED part rather than pressing on: the
                # remaining chunks of a message whose head never landed would arrive
                # as an orphaned fragment. `delivery_confirmed` owns which of the two
                # id conventions this transport follows.
                sent = await transport.send_message(
                    conversation_id, part, thread_id=resolved.thread_id
                )
                if not delivery_confirmed(transport.capabilities, sent):
                    logger.warning(
                        "%s reply: %s returned no message id for %s; treating as undelivered",
                        caller,
                        link.channel_type,
                        parent_key,
                    )
                    return False
        except Exception:
            logger.warning(
                "%s reply: %s delivery failed for %s",
                caller,
                link.channel_type,
                parent_key,
                exc_info=True,
            )
            return False
        logger.info(
            "%s reply → %s:%s (%d part(s))",
            caller,
            link.channel_type,
            conversation_id,
            len(parts),
        )
        return True

    # gateway_runtime/delivery.py
    _cron_origin_key = _delivery._cron_origin_key

    _deliver_cron_to_channel = _delivery._deliver_cron_to_channel

    # ── One spelling of the cron failure-alert mechanism ───────────────────
    #
    # Two call sites alert on a failed cron run: the script/command helper
    # (`_alert_cron_failure`) and the message path's own `except` block. They
    # legitimately differ in control flow (one re-raises), in who owns
    # `record_failure()`, and in wording. What they must NOT differ in is the
    # mechanism below -- the dedup window, the Slack-sink hardening, the
    # one-surface delivery rule, and when the dedup anchor advances.
    #
    # A docstring promising the two "cannot drift" is prose, not a mechanism:
    # nothing in it stops one surface's DM saying only "check logs" while the
    # other already carries the reason, and no test catches that. These four
    # helpers ARE the mechanism, so a change lands on both surfaces or on
    # neither.

    def _failure_alert_is_duplicate(self, job: CronJob, failure_hash: str) -> bool:
        """Whether this failure repeats the last one inside the reminder window.

        A job that fails identically every minute alerts once per
        ``_FAILURE_REMINDER_SECS`` rather than once per fire. Both surfaces read
        the SAME ``last_failure_hash`` / ``last_failure_at`` pair; a job is
        exactly one kind, so the two writers never interleave on one job.
        """
        return (
            failure_hash == job.last_failure_hash
            and time.time() - job.last_failure_at < _FAILURE_REMINDER_SECS
        )

    def _slack_safe_fenced(self, text: str) -> str:
        """Make *text* safe to interpolate into a Slack mrkdwn code fence.

        Two hazards, one of which escaping alone does not cover. Slack PARSES
        entity markup, and both halves of a failure alert are attacker-shaped --
        the job name is user-authored and the reason carries subprocess output, so
        a job named ``<!channel>`` would notify a whole channel the moment it
        failed. And three backticks inside the reason would CLOSE the fence early
        and hand the remainder to the parser as markup, which escaping does not
        prevent.

        Slack-facing sinks only. The dashboard bell is not a mrkdwn sink and
        escaping there would render a literal ``&lt;``.
        """
        return escape_mrkdwn(text).replace("```", "'''")

    async def _deliver_failure_alert(
        self,
        job: CronJob,
        *,
        mrkdwn: str,
        plain: str,
        actor_key: str,
        silent: bool = False,
    ) -> tuple[bool, bool, bool]:
        """Deliver a failure alert on exactly ONE surface.

        Returns ``(channel_delivered, slack_delivered, slack_failed)``.
        ``slack_failed`` names a real delivery exception, never an unresolved
        channel -- the caller's dedup decision turns on that split.

        The one-surface rule: when the conversation that scheduled the job will
        hear about the failure, the owner DM would be a second alert for one
        event. An explicit ``job.channel`` is a destination the user pinned and
        still wins, so the channel leg is skipped for it -- without that guard the
        channel leg reports a delivery, stands the Slack leg down, and the alert
        lands on the origin conversation instead of the destination the user
        named.

        *mrkdwn* and *plain* are composed by the caller and are deliberately two
        strings: Slack's markup is not another transport's dialect, so a shared
        string would show ``&lt;`` and stray backticks to a channel reader.

        Never raises. Both callers are inside an ``except`` whose exception is the
        real story, and one of them re-raises it.
        """
        channel_delivered = False
        slack_delivered = False
        slack_failed = False
        if not silent and not job.channel:
            try:
                channel_delivered = await self._deliver_cron_to_channel(
                    job.session_key, plain, actor_key=actor_key
                )
            except Exception:
                logger.error(
                    "Cron '%s': channel failure-alert delivery failed",
                    job.name,
                    exc_info=True,
                )
        if self.slack and not silent and not channel_delivered:
            try:
                channel = job.channel
                if not channel and (job.created_by or self._owner_id):
                    channel = await self._open_dm_with_retry(
                        job.created_by or self._owner_id, job.name
                    )
                if channel:
                    await self.slack.post_message(channel, mrkdwn)
                    slack_delivered = True
                else:
                    logger.warning("Cron '%s': no channel resolved for failure alert", job.name)
            except Exception:
                slack_failed = True
                logger.error(
                    "Cron '%s': Slack failure-alert delivery failed",
                    job.name,
                    exc_info=True,
                )
        return channel_delivered, slack_delivered, slack_failed

    def _advance_failure_dedup(
        self,
        job: CronJob,
        failure_hash: str,
        *,
        channel_delivered: bool,
        slack_failed: bool,
    ) -> None:
        """Advance the dedup anchor once the reason actually reached someone.

        "No channel available" counts as delivered -- the bell rang -- so a
        Slack-less install does not re-notify the dashboard on every fire. A
        confirmed channel delivery counts for the same reason: the reason reached
        the user even when the Slack leg threw. Only a REAL Slack exception holds
        the anchor back, so the next identical failure tries again.

        Only the dedup fields move here. ``record_failure()`` has its own owner
        per run and is deliberately not touched.
        """
        if channel_delivered or not slack_failed:
            job.last_failure_hash = failure_hash
            job.last_failure_at = time.time()

    # gateway_runtime/delivery.py
    _cron_job_is_silent = _delivery._cron_job_is_silent

    async def _init_cron(self, *, arm: bool = True) -> None:
        """Initialize the cron service, optionally arming it immediately."""

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
                        # Async form: the sync one parses the whole transcript on
                        # the loop, which stalls every other session's frames on
                        # a large store.
                        slot = await rehydrate_slot_from_history_async(
                            self.dashboard_state, slot_key
                        )
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
                                _run_chat(
                                    self.dashboard_state,
                                    slot,
                                    wrapped,
                                    _directive_user_origin=False,
                                    # Structural provenance for the session
                                    # crew log: the queued twin above carries
                                    # CRON_NOTIFICATION_KIND, and this branch is
                                    # the same injector dispatching directly.
                                    _turn_actor="cron",
                                ),
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
                    await self.cron_svc.remove_job_async(
                        job.id,
                        actor="cron",
                        source="cron",
                        one_shot_path="cron_gateway",
                    )
                except (CronStoreBusy, CronStoreUnreadable):
                    # No caller to retry this fire-and-forget removal, so hand
                    # it to the service's deferred-removal queue: the job is
                    # disabled in memory immediately (can't re-fire) and the
                    # next timer tick drains it from disk under the store lock.
                    # No audit here — whichever path lands the removal on disk
                    # emits cron.remove: the deferred drain, or the run-merge
                    # consume when a delete_after_run job's merge gets there
                    # first.
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
                if self._failure_alert_is_duplicate(job, fh):
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
                # Name the machine for the same reason the message path does: a
                # laptop and a cloud desktop can both run Kiro Crew, and the
                # alert is the only place the user learns which one failed. Read
                # once, ahead of both delivery legs, so they cannot disagree.
                host = socket.gethostname().split(".")[0]
                # Slack PARSES entity markup and a fence can be closed early by
                # the reason's own backticks; `_slack_safe_fenced` is the one
                # spelling of that hardening. `label` and `text` are already
                # scrubbed above, and the transport leg redacts again at egress.
                safe_label = self._slack_safe_fenced(label)
                safe_text = self._slack_safe_fenced(text)
                msg = (
                    f"⏰ *Cron: {safe_label}* {mark} "
                    f"_{headline} on {escape_mrkdwn(host)}_\n```{safe_text}```"
                )
                msg, _ = redact_exfiltration_urls(msg)
                msg, _ = redact_credentials(msg)
                # Plain twin for a non-Slack transport: mrkdwn is not another
                # channel's dialect, so a shared string would show `&lt;` and
                # stray backticks there.
                plain = f"⏰ Cron: {label} {mark} {headline} on {host}\n{text}"
                # Silent jobs returned above, so this leg is never suppressed here.
                channel_delivered, slack_delivered, slack_failed = (
                    await self._deliver_failure_alert(
                        job,
                        mrkdwn=msg,
                        plain=plain,
                        actor_key=f"cron:{job.id}",
                    )
                )
                self._advance_failure_dedup(
                    job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
                )
                try:
                    # Name every surface the alert actually left on, so the trail
                    # does not read "none" for a run answered on Discord.
                    surfaces = ["slack"] if slack_delivered else []
                    if channel_delivered:
                        surfaces.append(channel_namespace_of(job.session_key))
                    sel().log_tool_invocation(
                        session_key=f"cron:{job.id}",
                        tool_name="cron_run_failure_alert",
                        outcome="denied" if denied else "alerted",
                        downstream_service=",".join(surfaces) or "none",
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
            session_key, msg = build_cron_session_context(job)

            from kiro_crew.cron import resolve_cron_memory
            from kiro_crew.execution_context import execution_for_store, execution_from_record

            # Snapshot the job before yielding; reloading a cron cannot rebind it.
            cron_agents = list(job.agent_sequence)
            # ── Default-agent fallback (LLM jobs) ──
            # A job with no explicit agent_id must run the configured default
            # agent (config.agent.default_agent), matching the chat transports'
            # fallback (transport_dispatch: `self.agent or cfg.agent.default_agent`).
            # Without it the empty selector reaches dispatch as AcpClient's
            # "kirocrew" floor -- an agent whose config carries none of the
            # default agent's MCP servers, so agent-less cron sessions silently
            # run without the expected toolset.
            #
            # Resolved ABOVE the branch and applied BELOW it, to the dispatched
            # agent only -- never to the captured execution identity -- and only
            # where the resolved agent is still the bare "kirocrew" floor. All
            # three halves are load-bearing:
            #   * Above the branch, because every record created since the
            #     execution-context migration carries `execution_context` and so
            #     takes the other arm. Resolving inside `else:` reaches only
            #     pre-migration jobs and leaves the reported symptom in place for
            #     everything created since.
            #   * Not into the capture, because `cron_execution` is what
            #     `bind_session_execution` publishes under the session key, and a
            #     persistent_session job (the default) reuses the stable key
            #     `cron:{job.id}` on every fire. That binder is called
            #     positionally, so `replace_existing` is False, and it refuses a
            #     candidate that differs from what is already recorded. A job that
            #     has already run holds `template_id="kirocrew"`; folding the
            #     default into the captured identity changes that field, so every
            #     later fire raises "session already belongs to another execution"
            #     before get_or_create -- not transient, caught nowhere in this
            #     callback, one recorded failure per fire until the job
            #     auto-pauses. Leaving the captured identity alone is also what
            #     keeps the resolved name out of the durable store.
            #   * Floor-gated, because a captured `template_id` that names a real
            #     template is a deliberate selection and must survive; see the
            #     gate below.
            # job.agent_id is never mutated: the snapshot is deliberately immune
            # to later mutation ("captured selectors, never the scheduler's
            # mutable job"), and a declared agent_sequence owns its own dispatch
            # and is left untouched. agent_sequence_dispatches is the ONE spelling
            # of "the sequence owns dispatch": a lone entry is dormant and
            # dispatch falls through to agent_id, which must still get the default.
            # Typed deliberately: a malformed or non-str configured default must
            # not reach the dispatched agent kwarg, so it degrades to the existing
            # "kirocrew" floor instead of failing the dispatch.
            _default_agent = ""
            # True once the dispatched agent came from ``agent.default_agent``.
            # That field is the TEMPLATE namespace (config/sections.py, free-form
            # str, no alias validation) -- unlike the alias-namespaced default,
            # which has its own `default_agent_not_alias` guard. The collapse in
            # _resolve_cron_agent treats ANY name that keys ``cfg.agents`` as a
            # crew alias, so without this flag a default that merely shares a
            # name with a crew member would hand an agent-less cron that crew's
            # workspace and capability MCP servers -- a silent capability and
            # identity widening, announced at DEBUG only.
            _default_substituted = False
            _agentless_job = not (job.agent_id or "").strip() and not agent_sequence_dispatches(
                job.agent_sequence
            )
            if _agentless_job:
                try:
                    # Prefer the last APPLIED reload over the boot snapshot.
                    # agent.default_agent carries no restart=True mark, so the
                    # config watcher adopts a dashboard/CLI change live, while
                    # self._cfg is rebuilt only by unrelated MCP handlers. A
                    # boot-only read would keep dispatching the "kirocrew" floor
                    # after an operator sets a default -- the exact symptom this
                    # hunk removes -- until a gateway restart or an incidental
                    # reload. Same one-liner the sibling resolver below uses.
                    _cfg_now = live.snapshot() or self._cfg
                    _configured = _cfg_now.agent.default_agent
                    if isinstance(_configured, str):
                        _default_agent = _configured.strip()
                except Exception:
                    _default_agent = ""
            if job.execution_context is not None:
                cron_execution = execution_from_record({"execution_context": job.execution_context})
            else:
                legacy_selection = CronJob(
                    id=job.id,
                    name=job.name,
                    message=job.message,
                    member_id=job.member_id,
                    memory_store=job.memory_store,
                    agent_id=job.agent_id,
                )

                def resolve_legacy_execution():
                    # Both calls can load configuration. The worker sees only
                    # captured selectors, never the scheduler's mutable job.
                    resolve_cron_memory(legacy_selection, validate_memory_files=False)
                    return execution_for_store(
                        legacy_selection.memory_store,
                        template_id=legacy_selection.agent_id or "kirocrew",
                    )

                cron_execution = await asyncio.to_thread(resolve_legacy_execution)
            cron_memory_store, cron_agent = (
                cron_execution.store.legacy_name,
                cron_execution.template_id,
            )
            # The configured default lands HERE and nowhere else: cron_agent is
            # read only by _resolve_cron_agent and the `agent=` kwargs below, none
            # of which any durable record compares.
            #
            # Gated on the resolved agent still being the bare "kirocrew" floor,
            # which is the whole of what this change replaces. A record whose
            # captured template_id names a REAL template keeps it: a schedule
            # created from a template chat with no `agent` argument names its
            # template ONLY there, `agent_id` staying empty: bind_cron_memory
            # (cron_service/identity.py:143-152) captures the CREATOR session's
            # execution, and overwrites template_id only `if job.agent_id`. So
            # an agent_id-only gate reads that job as agent-less. Overriding it
            # would re-point the job onto the default on every fire while its
            # captured execution and memory store stayed the original template's,
            # and dispatched_agents_from_disk -- the delete guard's ONE walk of
            # the dispatch-mirroring rule -- still reported the old agent: guard
            # and dispatch would drift, which is the same silent wrong-agent class
            # this change removes, reintroduced on the other arm. Compared after
            # .strip() so a whitespace-only capture counts as the floor, which is
            # where whitespace agent_id handling lives now that the capture itself
            # is left byte-for-byte alone.
            # Also gated on the capture carrying no member_id. A cron created
            # from a crew member's chat is captured by resolve_member_execution
            # as a MEMBER execution whose template_id is
            # `agent.kiro_agent or "kirocrew"` -- so a member whose agent names
            # no kiro_agent captures the floor spelling with agent_id empty, and
            # a floor-only gate reads it as "selected nothing". Substituting
            # there makes build_message inject the substituted template's system
            # prompt and documents into that member's envelope while
            # _resolve_cron_agent still dispatches the member's own kiro_agent,
            # and attributes the usage row to the substituted name. Requiring
            # member_id to be unset narrows the override to a capture that
            # really did select nothing.
            #
            # member_id ALONE does not narrow it far enough. A member predating
            # the identity migration persists no member_id at all, and
            # `with_template` states the rule that admits it: such a record "is
            # named by ``selection_kind == \"member\"`` and ``selection_name``
            # alone" (execution_context.py). ExecutionContext.__post_init__
            # constrains selection_kind to exactly "member" or "template", so
            # the namespace is the authoritative answer to "did this capture
            # select a member?" and member_id is only its persisted form.
            # Requiring BOTH keeps a legacy member on an unmigrated store out of
            # the override, which a member_id-only gate admitted.
            #
            # And BOTH capture gates together are still not enough, because on
            # the `job.execution_context is None` arm cron_execution is
            # RECONSTRUCTED, not loaded: resolve_legacy_execution calls
            # execution_for_store, which for a V1 store returns
            # `ExecutionContext(None, MemoryStoreRef(name), "template", ...)` --
            # hardcoding a null member_id and the "template" namespace whatever
            # the schedule actually selected. A member cron predating the
            # migration therefore passes both capture gates while job.member_id
            # still names the member, and resolve_cron_memory deliberately keeps
            # that record dispatching rather than raising
            # (cron_service/identity.py: "Older V1 schedules may still carry the
            # historical member selector beside their explicit legacy store"),
            # with nothing backfilling execution_context on load. So the JOB is
            # asked as well: on that arm it is the only surviving evidence of the
            # selection. A truthy non-str also declines, which is the safe
            # direction -- declining leaves the pre-change floor behaviour.
            if (
                _default_agent
                and (cron_agent or "").strip() in ("", "kirocrew")
                and not job.member_id
                and not cron_execution.member_id
                and cron_execution.selection_kind != "member"
            ):
                # ── Spawn-allowlist divergence gate ──
                # The substitution reaches the dispatched `agent=` only, while
                # bind_session_execution publishes the UNSUBSTITUTED capture
                # under `cron:{job.id}` -- which it must, for the auto-pause
                # reason above. Both spawn entry points derive the parent's
                # declaration from that record alone: read_session_execution(
                # parent) -> parent_spawn_allowlists(execution.template_id), in
                # dashboard/handlers/messaging.py and the off-loop twin in
                # subagent.py. The floor spelling resolves to `()`, which
                # _vet_parent_available_agents admits as "no declaration to
                # honour" -- so a toolsSettings.subagent.availableAgents list on
                # the substituted template is never consulted and a child it
                # forbids starts.
                #
                # Honouring it properly means a NON-DURABLE parent execution the
                # spawn gate reads, in two files this change does not touch and
                # whose reviewers are scoped to spawn admission, not cron. So
                # this change narrows itself instead: a default that declares an
                # allowlist is not substituted, and the job keeps the floor it
                # already had before this change. Nothing an operator has today
                # regresses; the capability simply waits for that CR.
                #
                # DENY BY DEFAULT. A non-empty tuple declines, and so does the
                # reader's `None`. Only `()` -- "no readable spec declares this
                # name, and every spec file parsed" -- permits the substitution.
                #
                # An earlier revision substituted on `None`, arguing the spawn
                # gate would refuse anyway because it asks about the CAPTURE's
                # floor spelling. That premise is false: `agent.py` generates and
                # installs `kirocrew.json` into the agents directory and
                # `config/defaults.json` declares `"name": "kirocrew"`, so a
                # readable spec DOES declare the floor name -- and
                # `parent_spawn_allowlists` answers `None` only when NO readable
                # spec declares the name while some file could not be read. For
                # the floor it therefore returns `()`, "no declaration to
                # honour", and the gate ADMITS. `None` is exactly the state where
                # a restrictive declaration may be sitting in the spec file the
                # hardened reader refused, in a shared user-writable directory,
                # so letting a falsy value skip the check is the deny-by-default
                # violation this clause exists to avoid.
                from kiro_crew.subagent import parent_spawn_allowlists as _spawn_allowlists

                _declared = await asyncio.to_thread(_spawn_allowlists, _default_agent)
                if _declared or _declared is None:
                    logger.warning(
                        "Cron '%s': default agent %r %s, and the captured "
                        "execution's floor template cannot carry that declaration to "
                        "the spawn gate; keeping the floor for this fire rather than "
                        "dispatching a restricted agent whose allowlist would not be "
                        "honoured",
                        job.name,
                        _default_agent,
                        (
                            "declares toolsSettings.subagent.availableAgents"
                            if _declared
                            else "has an unreadable agent spec, so its "
                            "toolsSettings.subagent.availableAgents is unknown"
                        ),
                    )
                else:
                    cron_agent = _default_agent
                    _default_substituted = True

            def _resolve_cron_agent(
                alias: str | None,
                *,
                template_namespace: bool = False,
            ) -> "tuple[str | None, str | None, str | None]":
                """Resolve a cron agent alias to (kiro_agent, cwd, crew_alias).

                A cron bound to a Slack channel carries that channel's agent
                ALIAS (e.g. ``in-3d``) in ``job.agent_id`` / ``agent_sequence``.
                kiro-cli only accepts a materialized agent MODE, not a Kiro Crew
                alias, so dispatching the alias verbatim fails closed with
                "Agent mode 'in-3d' is not available … its ~/.kiro/agents/
                in-3d.json is likely missing". The dashboard chat path already
                collapses the alias the right way — see
                ``chat_runner._allocation_kwargs``, which passes
                ``agent=<kiro_agent>`` alongside ``crew_agent=<alias>`` so
                ``prepare_runtime`` still resolves the member identity from the
                alias. The cron path was the one turn-running surface that
                skipped it.

                Mirror that here: dispatch the alias's ``kiro_agent`` (usually
                ``kirocrew``) as ``agent``, run in the agent's workspace ``cwd``,
                AND return the ``crew_alias`` so the caller can pass it as
                ``crew_agent=`` — without which ``resolve_crew_identity`` sees
                only the bare kiro template name (not a ``config.agents`` key),
                returns ``""``, and every member-capability gate, the crew's
                pinned model / reasoning-effort, and its watchdog windows are
                silently skipped. Returns (None, None, None) on any miss so the
                caller falls back to the raw value unchanged (behavior-preserving
                for a job whose agent is already a real mode or is unset).
                """
                if not alias:
                    return None, None, None
                # A template-namespace name is NOT a crew alias, even when it
                # happens to key ``cfg.agents``. ``agent.default_agent`` is a
                # free-form str in the template namespace with no alias
                # validation (config/sections.py), so an operator naming a crew
                # member there is ordinary input -- and the collapse below keys
                # only on membership in ``cfg.agents``, then returns the alias as
                # ``crew_agent``, which ``resolve_crew_identity`` honours
                # verbatim. That would give an agent-less cron the named crew's
                # workspace, pinned model and capability MCP servers, silently
                # apart from a DEBUG line. Return the EXPLICIT no-crew spelling
                # rather than the miss tuple the other early returns use: `""`
                # is what `resolve_crew_identity` documents as opting out of the
                # fallback ("an explicit crew_agent wins verbatim -- including
                # \"\""), while `None` is the fallback-ENABLED spelling, so a
                # `None` here would still let `if agent and agent in
                # config.agents` reselect the colliding crew (loader.py) and the
                # bypass would be a no-op. `kiro_agent`/`cwd` stay None so the
                # name dispatches as the TEMPLATE it was configured as, in the
                # default workspace.
                # Consequence to expect, and the intended one: a default that is
                # really only a crew alias now fails closed and loudly with
                # "Agent mode '<name>' is not available", instead of quietly
                # inheriting that crew's identity.
                if template_namespace:
                    return None, None, ""
                try:
                    from kiro_crew.config.loader import (
                        resolve_agent_bindings,
                        workspace_dir_from_entry,
                    )

                    cfg = getattr(self, "_cfg", None)
                    # Prefer the last APPLIED reload over the boot snapshot: an
                    # agent created at runtime (dashboard/CLI) writes cfg.agents
                    # in live.snapshot() but not in the boot self._cfg, so a
                    # boot-only read would miss a hot-added alias and dispatch it
                    # raw (failing closed) on every fire until a gateway restart.
                    # Same one-liner the watchdog/mcp reads use elsewhere here.
                    cfg = live.snapshot() or cfg
                    if cfg is None:
                        return None, None, None
                    agents = getattr(cfg, "agents", None) or {}
                    # Resolve by IDENTITY, not by name, for a member execution.
                    # The memory store is captured at authoring (cron_execution),
                    # but the runtime/workspace/crew_agent are resolved live here;
                    # if a same-name alias was deleted and recreated, a by-name
                    # lookup would bind the NEW member's runtime while the store
                    # stays the retired member's silo — a cross-identity memory
                    # leak (the memory-store-seam execution_context.py guards).
                    # member_config_for_id pins resolution to the captured
                    # member_id, so the alias whose bindings we read is the same
                    # member the store belongs to; a mismatch (recreated/renamed)
                    # raises and we fall back to the raw value unchanged.
                    resolved_alias = alias
                    captured_member = getattr(cron_execution, "member_id", None)
                    if captured_member:
                        try:
                            from kiro_crew.execution_context import member_config_for_id

                            resolved_alias, _ = member_config_for_id(cfg, captured_member)
                        except Exception:
                            logger.debug(
                                "cron member identity %r not resolvable in live cfg; "
                                "leaving agent %r unchanged",
                                captured_member,
                                alias,
                                exc_info=True,
                            )
                            return None, None, None
                    # For a non-member (template/legacy) execution there is no
                    # identity to pin to: ONLY collapse a real Kiro Crew alias.
                    # resolve_agent_bindings falls back to the default agent for
                    # an unknown name, so resolving unconditionally would rewrite
                    # a legitimate kiro mode (e.g. 'kirocrew-lite') into the
                    # default. A name that is not an alias is either a real mode
                    # or unset — leave it untouched.
                    elif alias not in agents:
                        return None, None, None
                    # validate_memory_files=False: we only need the alias's
                    # kiro_agent + workspace mapping here, and this runs on the
                    # gateway event loop. The default (True) does a synchronous
                    # store dir stat + SQLite identity read, which would stall
                    # the loop on every cron fire; the session's own
                    # execution-context resolution validates the store later.
                    bindings = resolve_agent_bindings(
                        cfg, resolved_alias, validate_memory_files=False
                    )
                    kiro_agent = bindings.kiro_agent or None
                    # Anchor the workspace dir by the one placement rule. The
                    # resolved bindings.workspace_dir is the RAW configured value
                    # (e.g. the shipped relative default "workspace"); handing
                    # that to the provider as a cwd would resolve it against the
                    # gateway PROCESS directory, not the data home. Resolve the
                    # alias's workspace entry through workspace_dir_from_entry so
                    # a relative dir anchors under config_dir() and an absolute
                    # dir is honored as-is.
                    agent_cfg = agents.get(resolved_alias)
                    ws_name = getattr(agent_cfg, "workspace", None) if agent_cfg else None
                    ws_entry = None
                    if ws_name:
                        ws_entry = getattr(cfg, "workspaces", {}).get(ws_name)
                    # workspace_dir_from_entry(None) is the BASE workspace
                    # directory under config_dir() — the documented answer for an
                    # unmapped/empty workspace name (loader.py's workspace_dir_for
                    # rule). Deliberately NOT cfg.default_workspace's dir: that
                    # rule forbids an unmapped name hopping to whatever absolute
                    # dir the default declares, so a missing mapping anchors to
                    # the data home rather than escaping it (or defaulting to the
                    # gateway process cwd via a None).
                    ws_dir = workspace_dir_from_entry(ws_entry)
                    cwd = str(ws_dir) if ws_dir else None
                    # Carry the identity-resolved alias back as crew_alias:
                    # prepare_runtime needs it to resolve the member identity (the
                    # kiro_agent name alone is not a config.agents key), and it is
                    # the alias pinned to the captured member_id above.
                    return kiro_agent, cwd, resolved_alias
                except Exception:
                    logger.debug("cron agent resolve failed for %r", alias, exc_info=True)
                    return None, None, None

            # ── Concurrent execution guard ──
            if (job.script or job.command) and job.id in self._running_script_ids:
                logger.info("Cron '%s': previous execution still running, skipping", job.name)
                return None

            # ── Command mode: direct shell execution (sandboxed) ──
            if job.command:
                if getattr(self.sessions, "admission_closed", False) is True:
                    logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
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
                        job.secret_env,
                        job.secret_env_pin,
                        timeout=_claim_backstop(job, cmd_timeout),
                    )
                    if result.get("status") == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    if result.get("status") == "skipped":
                        # Overlapping wake refused because this job is already
                        # spawning or running. Not a job defect, so no failure
                        # counting and no delivery -- counting it would strike a
                        # job for a transient scheduling overlap.
                        #
                        # But returning None alone is NOT neutral: _execute treats
                        # any non-"error" last_status as success, so it would set
                        # last_status="ok" AND call record_success(), fabricating a
                        # run that never happened and refilling the auto-pause
                        # budget. The cancelled branch above escapes that only
                        # because _execute additionally checks self._cancelled_jobs
                        # membership, and a refused overlap is not in that set.
                        #
                        # So use the established deliberately-neutral shape of the
                        # starvation and fire-time-denial paths: last_status="error"
                        # to skip the success branch, run_never_started=True as the
                        # retention marker, and DELIBERATELY NOT record_failure() --
                        # a strike is only ever counted by that explicit call, never
                        # by last_status, so this spends no budget and refills none.
                        logger.info("Cron '%s': overlapping run refused, skipping", job.name)
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = "Another run of this job was already starting or running"
                        job.run_never_started = True
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
                            # Same reason record_success resets consecutive_failures: a run that
                            # worked proves the job can work, so the shared-death streak that
                            # substitutes for that counter is reset with it. Without this the
                            # streak is a LIFETIME total, so after enough deaths the bound is
                            # permanently tripped and the exemption silently stops applying.
                            runtime_death.clear_shared_deaths(f"cron:{job.id}")
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
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
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
                if getattr(self.sessions, "admission_closed", False) is True:
                    logger.info("Cron '%s': gateway admission is closed, deferring", job.name)
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
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
                    # (which also validates the path, making a bare
                    # resolve_script_path call here redundant), so a policy
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
                    # holds at the moment of use.  It shares the backstop below,
                    # which is why it must stay short.
                    result = await run_in_cron_pool(
                        _vet_at_claim_then,
                        handoff,
                        job,
                        run_script_sandboxed,
                        job.script,
                        job.id,
                        job.message,
                        script_timeout,
                        job.secret_env,
                        job.secret_env_pin,
                        delivery_fingerprint(
                            job.session_key,
                            job.silent,
                            job.channel or "",
                            job.thread_ts or "",
                        ),
                        self._live_internal_secret,
                        timeout=_claim_backstop(job, script_timeout),
                    )
                    status = result.get("status", "error")
                    if status == "cancelled":
                        # User-initiated cancel: CronService.cancel() owns the
                        # bookkeeping/history — no failure counting, no delivery.
                        return None
                    if status == "skipped":
                        # Overlapping wake refused because this job is already
                        # spawning or running -- no failure counting, no delivery.
                        # See the command path for why returning None alone would
                        # be recorded as a SUCCESS: last_status="error" skips
                        # _execute's success branch, run_never_started=True is the
                        # retention marker, and record_failure() is deliberately
                        # NOT called so the overlap costs no auto-pause strike.
                        logger.info("Cron '%s': overlapping run refused, skipping", job.name)
                        job.clear_carried_result()
                        job.last_status = "error"
                        job.last_error = "Another run of this job was already starting or running"
                        job.run_never_started = True
                        return None
                    if status == "ok":
                        job.clear_carried_result()
                        job.last_error = ""
                        job.last_status = "ok"
                        job.record_success()
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
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
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
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
                        # Same reason record_success resets consecutive_failures: a run that
                        # worked proves the job can work, so the shared-death streak that
                        # substitutes for that counter is reset with it. Without this the
                        # streak is a LIFETIME total, so after enough deaths the bound is
                        # permanently tripped and the exemption silently stops applying.
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
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
                    # No auto-pause log here either: this path cannot reach the
                    # threshold.
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
            # reaching this point dispatches an LLM turn, so it is gated here too:
            # without this check, disabling the cron capability after scheduling
            # would leave message jobs firing. Same deny semantics as the other
            # kinds: mark the run failed, keep the job.
            # Off-loop for the same reason as the command/script sites above, and
            # on the GOVERNANCE pool for the same reason: a message job gets no
            # pool allowance on either deadline, so gating it on the cron pool
            # would charge a saturated pool's queue directly to its execution
            # budget.
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

            # ── First-run tab pre-create ──
            # The result injection at the end of this callback is the other
            # creator site for the job's dashboard tab, and on its own it leaves
            # a NEW job's first run without one: session-control caller identity
            # (caller_slot_key walks slot links for cron:{id}) refuses every verb
            # with caller_unidentified, and the dashboard-surface registry has no
            # row for sub-agent/widget/question/approval routing. Bind the tab up
            # front — eligibility (persistent_session and not hide_in_chat) lives
            # inside the helper, so script/command jobs never reach it (they
            # return above) and ineligible message jobs are untouched. Placed
            # AFTER the fire-time gate: a denied run dispatches nothing a tab
            # could serve. Visible consequence, accepted deliberately: a first
            # run that starts and then fails leaves an empty tab. Pre-creating
            # anyway is the choice, because gating the tab on the run reaching
            # injection would reopen the very hole this closes — identity must
            # exist DURING the run. Guarded wrapper, not a bare await: a tab we
            # FAIL to mint must not kill — or, via the run_never_started
            # lifecycle, CONSUME — the one-shot run it was meant to serve (see
            # _pre_create_cron_slot for the retention-marker contract).
            if self.dashboard_state:
                await _pre_create_cron_slot(self.dashboard_state, job)

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

                The two governance policy-path vars are reserved for the same
                reason, and the strip set is ``_CRON_RESERVED_ENV_KEYS``.
                """
                env = cron_job_env_without_reserved(job.env)
                if job.approval_mode == "auto":
                    env["KIROCREW_APPROVAL_MODE"] = "auto"
                return env or None

            async def _acquire_with_model_fallback(
                key: str,
                agent_id: str | None,
                cwd: str | None = None,
                crew_agent: str | None = None,
            ) -> "tuple[LLMProvider, bool, bool, bool]":
                """get_or_create honoring job.model; if that model is
                unavailable, retry once with the registry default.
                Returns (client, is_new, resumed, downgraded).

                ``agent_id`` is the RESOLVED kiro agent mode (an alias must be
                collapsed via _resolve_cron_agent before this call), ``cwd``
                is that agent's workspace so the session runs in the right tree,
                and ``crew_agent`` is the original alias so prepare_runtime
                resolves the member identity (its capability gates, model /
                reasoning-effort pins, and watchdog windows).
                """

                assert self.sessions is not None
                from kiro_crew.execution_context import bind_session_execution

                await asyncio.to_thread(bind_session_execution, key, cron_execution)
                modes = getattr(self.ctx_builder, "_session_memory_modes", None)
                if isinstance(modes, dict):
                    # A separately scheduled run is durable work, not a child
                    # conversation. Only this trusted dispatch admits its key.
                    from kiro_crew.messaging.privacy_mode import strictest
                    from kiro_crew.subagent_persistence import bind_session_memory_mode
                    from kiro_crew.workflows.registry import _await_owned

                    publication = asyncio.create_task(
                        asyncio.to_thread(bind_session_memory_mode, key, cron_execution.memory_mode)
                    )
                    admitted_mode = await _await_owned(publication)
                    modes[key] = (
                        strictest((admitted_mode, modes.get(key, "persistent"))) or "persistent"
                    )
                try:
                    client, is_new, resumed = await self.sessions.get_or_create(
                        key,
                        agent=agent_id,
                        crew_agent=crew_agent,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        model=job.model or None,
                        extra_env=_cron_extra_env(),
                        cwd=cwd,
                    )
                    # A config-option backend refuses a pin without raising and
                    # stays on its default: the same downgrade as the except
                    # below, so report it the same way.
                    return (
                        client,
                        is_new,
                        resumed,
                        bool(job.model) and provider_model_pin_refused(client),
                    )
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
                        crew_agent=crew_agent,
                        channel_id=job.channel,
                        approval_policy=job.approval_mode,
                        extra_env=_cron_extra_env(),
                        cwd=cwd,
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
            agents = cron_agents
            if agent_sequence_dispatches(agents):
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
                    # Post-compaction re-injection bookkeeping for the finally:
                    # consumed the one-shot flag / turn landed. The stream
                    # returns text only, so the completion's stop reason is
                    # captured through on_complete (last completion wins: a
                    # post-token resume's continuation is the turn that ended).
                    _seq_reinjection = False
                    _seq_landed = False
                    # None until a completion is observed: a stream that ends
                    # without one is not landed.
                    _seq_stop: dict[str, str | None] = {"reason": None}

                    def _seq_note_complete(
                        ev: Any, _box: dict[str, str | None] = _seq_stop
                    ) -> None:
                        _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

                    try:
                        # Collapse an alias (e.g. a channel-bound agent) to its
                        # real kiro mode + workspace; keep the session key on the
                        # ORIGINAL alias so per-agent keys stay stable.
                        _seq_kagent, _seq_cwd, _seq_crew = _resolve_cron_agent(agent)
                        client, is_new, _resumed, _downgraded = await _acquire_with_model_fallback(
                            agent_session_key, _seq_kagent or agent, _seq_cwd, _seq_crew
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
                        # A compaction drops session-start context. Read-and-clear
                        # the one-shot flag so this turn re-injects it exactly
                        # once; the finally re-arms it if the turn never lands.
                        _seq_reinjection = consume_reinjection(self.sessions, agent_session_key)
                        # Off-loop: build_message embeds the episodic query.
                        full_message, _ = await run_in_embed_pool(
                            self.ctx_builder.build_message,
                            msg,
                            is_new,
                            agent_session_key,
                            interactive=False,
                            agent=agent,
                            memory_store=cron_memory_store or None,
                            execution_context=cron_execution,
                            context_provider=client,
                            resumed=_resumed,
                            needs_reinjection=_seq_reinjection,
                            minimal_context=job.minimal_context,
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
                            on_complete=_seq_note_complete,
                            fallback_models=configured_fallback_chain(),
                        )
                        # The prompt reached the model and the turn completed, so
                        # the finally must NOT restore the re-injection flag --
                        # only for a succeeded stop reason.
                        _seq_landed = stop_reason_landed(_seq_stop["reason"])
                        if not result_text:
                            result_text = _gate.empty_reply_placeholder()
                        result_text = _annotate_model_fallback(result_text, client)
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
                                # A half-applied pair pin bills the bare
                                # model that ran, not the suffixed pin.
                                (
                                    ""
                                    if (_seq_downgraded or provider_fallback_active(client))
                                    else (
                                        (job.model and provider_model_pin_partial(client))
                                        or job.model
                                        or ""
                                    )
                                ),
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
                        # Before the reset below: a turn that consumed the
                        # post-compaction flag but never landed puts it back so
                        # a session that survives (deferred reset) re-injects.
                        rearm_reinjection(
                            self.sessions,
                            agent_session_key,
                            consumed=_seq_reinjection,
                            landed=_seq_landed,
                        )
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
                                and await _subagent_work_pending(
                                    self.subagent_mgr, agent_session_key
                                )
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
                                    self.cron_svc.clear_active_session_key(
                                        job.id, agent_session_key
                                    )
                if _seq_downgraded:
                    result_text = _annotate_model_downgrade(result_text)
                result_text = _annotate_partial_block(result_text, _gate)
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
            # The provider THIS run acquired, held for the failure handler's
            # attribution question. Captured here rather than looked up when the
            # failure is handled, because by then the runtime is gone: the
            # ACP-death arm resets the session before it retries, and the retry
            # frame's own finally resets again on unwind -- so a lookup by key
            # answers None, or worse the REPLACEMENT provider, and either one
            # reads as "this job's own fault".
            _run_provider: object | None = None
            # Post-compaction re-injection bookkeeping for the finally: consumed
            # the one-shot flag / turn landed. Stop reason captured through
            # on_complete, as on the sequential path above.
            _needs_reinjection = False
            _turn_landed = False
            _turn_stop: dict[str, str | None] = {"reason": None}

            def _note_complete(ev: Any, _box: dict[str, str | None] = _turn_stop) -> None:
                _box["reason"] = str(getattr(ev, "stop_reason", "") or "")

            try:
                assert self.sessions is not None
                assert self.ctx_builder is not None
                # Collapse an alias (channel-bound agent) to its real kiro mode
                # + workspace before dispatch; falls back to the raw value when
                # it is already a real mode or unset.
                _single_kagent, _single_cwd, _single_crew = _resolve_cron_agent(
                    cron_agent or None, template_namespace=_default_substituted
                )
                _dispatch_agent = _single_kagent or cron_agent or None
                # ── Retained-session agent guard (substituted default only) ──
                # The cron key is the stable f"cron:{job.id}", and the run's
                # `finally` DEFERS the reset while sub-agents are pending or an
                # injection is in flight, so a session can outlive its fire. The
                # claim path then returns that live `session.provider` by key
                # WITHOUT comparing the `agent` kwarg (session_allocation.py's
                # `existing is not None and not recycling` arm), so a default
                # narrowed between fires would build the NEW agent's context and
                # run it on the OLD agent's process -- keeping MCP servers, a
                # workspace and a pinned model the operator has just taken away.
                #
                # This window is opened by THIS change and by nothing else:
                # before it an agent-less cron dispatched the constant
                # "kirocrew" floor on every fire, so no two fires could differ.
                # Hence the gate on `_agentless_job` -- the predicate that
                # defines the window -- and NOT on `_default_substituted`, which
                # failed open in exactly the case this guard exists for: fire N
                # substitutes and defers its reset, the operator then CLEARS
                # `agent.default_agent`, and fire N+1 resolves an empty default,
                # so that flag is False, the guard is skipped, and the fire runs
                # the FLOOR's context on the process still holding the previous
                # default's runtime. Every other reason for declining the
                # substitution below reaches the same hole. Keying on the
                # mismatch itself costs nothing elsewhere: a job pinning its own
                # agent_id dispatches one name forever, and a dispatching
                # sequence runs on per-agent keys (f"cron:{job.id}:{agent}") that
                # already separate the sessions -- both are excluded by this
                # predicate, so no fire this change does not touch is deferred.
                #
                # DEFER, never reset: a retained session implies pending
                # sub-agent work -- the no-pending case already reset in the
                # prior fire's `finally` -- so resetting to get a correct agent
                # would destroy work in flight, which is strictly worse than
                # skipping one fire. The retention is temporary by construction
                # (`_subagent_done` resets after the last one lands) and the
                # reaper still targets this key if that reset hangs, so the
                # deferral cannot become permanent. `_defer_cron_before_dispatch`
                # is the established spelling: the run counts as neither success
                # nor failure, so a skipped fire cannot walk a job toward
                # auto-pause, and `run_never_started` keeps a delete_after_run
                # job from being consumed by a fire that ran no line.
                #
                # The active-session-key registration above is deliberately
                # LEFT in place: a session really is still live under this key,
                # which is the same reason the deferred-reset arm leaves it.
                #
                # POSITIVE EVIDENCE ONLY. A non-empty `str` from the reader that
                # differs from the name about to be dispatched is a mismatch; a
                # manager without the reader, an unreadable answer, a non-`str`,
                # or "" (no session under the key) all leave this dispatch
                # exactly as it was. Fail-closed here means refusing the unsafe
                # REUSE, not treating an unanswerable question as a mismatch --
                # deferring on a silent reader would stall every agent-less cron
                # on such a host forever, which is a much larger failure than
                # the widening being closed. `session.agent` is stored as the
                # `agent` kwarg verbatim (`agent=agent or ""`), so the two sides
                # of this comparison are the same spelling by construction.
                if _agentless_job:
                    _live_agent = None
                    try:
                        _reader = getattr(self.sessions, "_get_session_agent", None)
                        if callable(_reader):
                            _live_agent = _reader(session_key)
                    except Exception:
                        logger.debug(
                            "cron '%s': live session agent unreadable", job.name, exc_info=True
                        )
                        _live_agent = None
                    if (
                        isinstance(_live_agent, str)
                        and _live_agent
                        and _live_agent != (_dispatch_agent or "")
                    ):
                        logger.info(
                            "Cron '%s': session retained under agent %r but this fire "
                            "dispatches %r; deferring rather than reusing its runtime",
                            job.name,
                            _live_agent,
                            _dispatch_agent or "",
                        )
                        _defer_cron_before_dispatch(
                            job,
                            f"session retained under agent {_live_agent!r} with work "
                            f"pending; this fire dispatches "
                            f"{_dispatch_agent or ''!r} and will not reuse it",
                        )
                        return None
                client, is_new, _resumed, _model_downgraded = await _acquire_with_model_fallback(
                    session_key, _dispatch_agent, _single_cwd, _single_crew
                )
                _acquired = True
                _run_provider = client
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
                # A compaction drops session-start context. Read-and-clear the
                # one-shot flag so this turn re-injects it exactly once; the
                # finally re-arms it if the turn never lands.
                _needs_reinjection = consume_reinjection(self.sessions, session_key)
                # Off-loop: build_message embeds the episodic query.
                full_message, _ = await run_in_embed_pool(
                    self.ctx_builder.build_message,
                    msg,
                    is_new,
                    session_key,
                    interactive=False,
                    agent=cron_agent or None,
                    memory_store=cron_memory_store or None,
                    execution_context=cron_execution,
                    context_provider=client,
                    resumed=_resumed,
                    needs_reinjection=_needs_reinjection,
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
                    on_complete=_note_complete,
                    fallback_models=configured_fallback_chain(),
                )

                # The prompt reached the model and the turn completed, so the
                # finally must NOT restore the re-injection flag -- only for a
                # succeeded stop reason.
                _turn_landed = stop_reason_landed(_turn_stop["reason"])

                if not result_text:
                    result_text = _gate.empty_reply_placeholder()

                if _model_downgraded:
                    result_text = _annotate_model_downgrade(result_text)
                result_text = _annotate_model_fallback(result_text, client)
                # Before set_run_result and the dedup hash below, so the stored
                # result, the slot, the bell and every transport carry the
                # refusal, and a run that lost a call never hashes equal to
                # one that did not.
                result_text = _annotate_partial_block(result_text, _gate)

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
                        # Blank on a downgrade or an active fallback — see the
                        # sequential site above / provider_fallback_active. A
                        # half-applied pair pin bills the bare model that ran.
                        (
                            ""
                            if (_model_downgraded or provider_fallback_active(client))
                            else (
                                (job.model and provider_model_pin_partial(client))
                                or job.model
                                or ""
                            )
                        ),
                        _turn_usage,
                        provider=_provider,
                        surface="cron",
                        agent=read_effective_agent(client) or cron_agent or "",
                        context_used=_used,
                        context_window=_window,
                        elapsed_ms=int((time.monotonic() - _turn_t0) * 1000),
                        model_source=client,
                    )
                except Exception:
                    logger.debug("usage row (cron) persist failed", exc_info=True)

                # ── Error deduplication ──
                # Suppress repeated identical results to avoid spam. This is
                # delivery-agnostic in both directions: the anchor it reads is
                # advanced by _record_cron_delivery on EVERY confirmed surface,
                # so the early return below suppresses the channel post too,
                # not only Slack's. Deliberate for a channel-delivered cron:
                # identical output is equally noisy in a Telegram chat, and the
                # 24h reminder plus the "same result N times in a row" caption
                # are what keep a persistently-identical job from going
                # unnoticed.
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
                            "Cron '%s': duplicate result #%d — suppressing delivery",
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
                                history=await prefetch_cron_history(self.dashboard_state, job.id),
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
                            history=await prefetch_cron_history(self.dashboard_state, job.id),
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
                # A job belongs to ONE surface: the conversation that scheduled
                # it. Delivering to both would notify an operator twice for one
                # run, which is how notifications become noise people stop
                # reading. So the channel leg is attempted FIRST and Slack stands
                # down only on a CONFIRMED delivery: a predicate saying a channel
                # *would* take it is not the same claim, and standing Slack down
                # on that loses the result outright when the channel send is
                # refused by governance or fails on the wire. An explicit
                # `job.channel` is a destination the user pinned, so it wins over
                # both. Every job that exists today has no channel origin, so
                # this is inert for current installs.
                channel_delivered = False
                if not job.channel:
                    try:
                        channel_delivered = await self._deliver_cron_to_channel(
                            job.session_key,
                            f"⏰ Cron: {job.name}\n\n{result_text}",
                            actor_key=session_key,
                        )
                    except Exception:
                        # The job SUCCEEDED. Letting a delivery error reach the
                        # outer handler would record a failure and march the job
                        # toward auto-pause on a messaging fault. Slack still
                        # runs below, because nothing was delivered here.
                        logger.error(
                            "Cron job '%s': channel delivery failed (job succeeded)",
                            job.name,
                            exc_info=True,
                        )
                if channel_delivered:
                    # Same dedup contract as the Slack leg: the hash advances
                    # once the result reached someone. Without this a Slack-less
                    # install never advances it, so the suppression branch can
                    # never fire and an unchanged result is re-delivered forever.
                    self._record_cron_delivery(job, rh)
                    # No reply-anchor write to mirror the Slack branch's
                    # set_thread/set_channel below, deliberately. Those two record
                    # where a Slack cron post LANDED so a later subagent completion
                    # under ``cron:{id}`` can be threaded onto it; the channel leg
                    # learns nothing equivalent at send time -- its conversation was
                    # already resolved FROM the creating session's own durable
                    # origin/mirror link, which every later delivery re-reads.
                    # Routing a cron's subagent completions back to the creating
                    # channel needs a ``cron:{id}`` -> creating-key edge instead,
                    # which is its own change; half of it here would look like
                    # parity without being it.
                if self.slack and not channel_delivered:
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
                            self._record_cron_delivery(job, rh)
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
                if isinstance(exc, SessionClosingError) and not _prompt_dispatched:
                    logger.info(
                        "Cron '%s': gateway admission closed before dispatch, deferring",
                        job.name,
                    )
                    _defer_cron_before_dispatch(job, "gateway admission is closed")
                    return None
                # Attempt one retry for ACP process death before any dedup / alert.
                exc_msg = str(exc).lower()
                # Set once the ACP retry below enters its attempt. If that
                # attempt fails it may have dispatched, so the transient ladder
                # must not run again. A failed reset leaves it unset.
                _acp_retry_attempted = False
                if (
                    # Match the death by type: the pipe-broken raise sites in
                    # acp/client.py word it "pipe broken", which no substring
                    # below covers. The typed arm is held to before dispatch,
                    # where no tool can have run yet. Once the retry attempt
                    # has started, this frame skips its transient ladder, so
                    # no prompt is sent again after that attempt.
                    # The substrings keep their old reach.
                    (
                        (isinstance(exc, AcpProcessDied) and not _prompt_dispatched)
                        or (
                            isinstance(exc, AcpError)
                            and ("not running" in exc_msg or "process exited" in exc_msg)
                        )
                    )
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
                        # The reset destroyed the live session that carried the
                        # consumed re-injection flag, and the retry starts the
                        # replacement cold (session-start context comes with a
                        # fresh session). Hand the bookkeeping to the retry, as
                        # the transient arm does, or the finally below would
                        # re-arm the REPLACEMENT after the retry lands and keeps
                        # it (deferred reset for pending sub-agents) -- one
                        # duplicated re-injection on its next turn.
                        _needs_reinjection = False
                        _acp_retry_attempted = True
                        return await _cron_callback(job)
                    except Exception:
                        pass  # retry failed — fall through to dedup + alert
                    finally:
                        job._acp_retried = False  # type: ignore[attr-defined]
                # ── Transient backend errors: retry the whole callback with ──
                # backoff instead of counting a failure. stream_and_collect's
                # in-stream retry only covers errors raised INSIDE the prompt
                # stream; a throttle/5xx during session acquire, client
                # creation, or context assembly propagates here, and without
                # this branch would go straight to record_failure(), marching
                # consecutive_failures toward auto-pause (threshold 5) on pure
                # infrastructure weather -- five throttled wakes silently
                # auto-pausing a healthy perpetual agent. The subagent path
                # retries these 3x with backoff; the cron path carries the same
                # semantics.
                #
                # Guarded by the same recursion marker pattern as the ACP
                # retry: the attempt counter lives on the job for the duration
                # of the outermost invocation only, and the recursive call
                # re-enters the full callback so a retry that succeeds runs
                # the complete delivery path.
                if (
                    acp_error_is_transient(exc)
                    and not _prompt_dispatched
                    and not _acp_retry_attempted
                ):
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
                        # The callback only INCREMENTS `_transient_attempts`
                        # (above). Reading it into the persisted telemetry and
                        # clearing it for the next run both belong to
                        # `CronService._execute`, which owns every other
                        # per-run stamp (`last_run_ts` first among them): a
                        # write from inside this frame would land BEFORE that
                        # method stamps this run's `last_run_ts`, binding the
                        # count to the previous run. Its read-and-clear is in a
                        # `finally`, so a wake-budget cancellation landing in
                        # this sleep cannot strand the just-consumed attempt on
                        # the in-memory job either.
                        try:
                            if _acquired and self.sessions is not None:
                                self.sessions.release(session_key)
                                _acquired = False
                        except Exception:
                            logger.debug("release before transient retry failed", exc_info=True)
                        # The retry re-enters this callback on the SAME live
                        # session (no reset on this arm), so give it back the
                        # re-injection this attempt consumed and never delivered
                        # -- and hand the bookkeeping to the retry, or the
                        # finally below would re-arm a second time after it lands.
                        rearm_reinjection(
                            self.sessions,
                            session_key,
                            consumed=_needs_reinjection,
                            landed=_turn_landed,
                        )
                        _needs_reinjection = False
                        await asyncio.sleep(_delay)
                        return await _cron_callback(job)
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
                # Was this run's failure this JOB's, or its runtime's? A cron
                # runtime hosts the job's own sub-agents, so a death there is a
                # process event several accounts witness -- and five of them
                # auto-pause a job that has done nothing wrong, which is the
                # threshold this guard protects. Read the record the death wrote
                # once, through the provider the manager still holds for this key
                # (never a pid, which a session must not be able to name).
                # Unattributable, or a runtime this job was alone on: counted
                # exactly as before.
                _job_owns_failure = runtime_death.caused_by_this_session(_run_provider)
                # Set only when the substitute bound reaches its limit below, and
                # consumed only beside record_failure(), so the three steps of the
                # hand-over stay contiguous.
                _hand_over_streak = False

                def _charge_failure() -> None:
                    """Charge this run's failure, performing any pending hand-over first.

                    The two are one indivisible move. The hand-over raises the
                    job's counter to one below the threshold so that the charge
                    below lands ON it, and forgets the streak whose value it just
                    transferred -- and none of those three steps may be separated
                    from the others by an await. ``cancel()`` is a plain
                    ``task.cancel()`` which, unlike the wake-budget timeout,
                    charges nothing on its way out, so a torn move would leave the
                    counter and the streak disagreeing with no writer left to
                    reconcile them.
                    """
                    if _hand_over_streak:
                        job.consecutive_failures = max(
                            job.consecutive_failures, _AUTO_PAUSE_THRESHOLD - 1
                        )
                        runtime_death.clear_shared_deaths(f"cron:{job.id}")
                    job.record_failure()

                if not _job_owns_failure:
                    # Not charging is not the same as never charging. A job whose
                    # shared runtime dies every run would otherwise re-fire on
                    # every tick forever, because record_failure() is the only
                    # actuator auto-pause has. So the exemption is bounded by the
                    # job's own threshold, counted against the shared runtime
                    # rather than against the job -- the same substitute bound the
                    # chat runner and the taskrunner use.
                    # Keyed to the JOB, never to this run's session key. The
                    # counter this substitutes for -- job.consecutive_failures --
                    # lives on the job, while a non-persistent job's session key
                    # is `cron:{id}:{uuid}`, fresh every run: a streak keyed there
                    # would never reach 2, so the bound would never fire and the
                    # exemption would be unbounded for exactly the jobs that most
                    # need it. A substitute bound is keyed to whatever owns the
                    # counter it replaces.
                    _shared_streak = runtime_death.note_shared_death(f"cron:{job.id}")
                    if _shared_streak >= _AUTO_PAUSE_THRESHOLD:
                        # At the limit the substitute bound HANDS OVER its
                        # accumulated value to the counter it stood in for,
                        # instead of adding a single charge to a counter still at
                        # zero. Adding one would deliver twice the bound this
                        # claims: the exemption spends the first
                        # _AUTO_PAUSE_THRESHOLD deaths, and the job's own counter
                        # would then need that many charges again, so a job whose
                        # shared runtime dies every run keeps firing for about
                        # twice as many runs as a job that was never exempted.
                        # The hand-over leaves the one charge below to land ON the
                        # threshold, so record_failure() stays the sole owner of
                        # both the counter and the pause, and the alert's
                        # displayed count is the number that actually paused it.
                        #
                        # The transfer itself is NOT performed here. It is a
                        # three-step move -- raise the counter, forget the streak,
                        # charge the failure -- and every await in this handler
                        # precedes the counter for the reason the comment beside
                        # record_failure() gives. Splitting the move across those
                        # awaits leaves a window: `cancel()` is a plain
                        # ``task.cancel()`` that charges nothing, so a cancellation
                        # during the failure alert would leave the counter at
                        # threshold-1 with the streak already erased, and the next
                        # failure would pause a job that had not reached the
                        # threshold. So the decision is recorded now and the move
                        # happens beside record_failure(), where the three steps
                        # are contiguous and cannot be torn apart.
                        _hand_over_streak = True
                        logger.warning(
                            "Cron '%s': the runtime it shares has died %d times running — "
                            "handing the streak to the job's own counter so it pauses now",
                            job.name,
                            _shared_streak,
                        )
                        _job_owns_failure = True
                    else:
                        logger.warning(
                            "Cron '%s': the runtime it shares died (%d running) — recording the "
                            "error but not counting it toward auto-pause",
                            job.name,
                            _shared_streak,
                        )
                # ── Failure dedup: suppress repeated identical crash notifications ──
                # A chain-exhaustion failure carries the fallback story on the
                # exception (llm_helpers.FALLBACK_STORY_ATTR); append it so the
                # alert names the whole walk, not just the last candidate's
                # error.
                # Redact over the FULL error text BEFORE any truncation — a cap
                # applied first can cut a credential at the boundary, leaving a
                # fragment the redaction regexes no longer match. The story is
                # redacted+capped centrally in fallback_story_of.
                _exc_text = f"{type(exc).__name__}: {exc}"
                _exc_text, _ = redact_exfiltration_urls(_exc_text)
                _exc_text, _ = redact_credentials(_exc_text)
                # Delivery detail: the budget trims the ERROR part to leave the
                # story room (a verbose backend error must not evict the walk),
                # floored at half the cap — an oversized story trims its own
                # tail past that point instead of evicting the error.
                exc_detail = append_fallback_story(_exc_text, exc, budget=_CRON_FAILURE_DETAIL_CAP)
                # Dedup hashes the STORY-FREE error text: the walk can differ
                # between two occurrences of the same backend error (a
                # candidate momentarily unadvertised changes `walked`; a
                # skipped walk has no story at all), and a hash keyed on it
                # would miss the duplicate and re-page the user.
                fh = _result_hash(_exc_text)
                # Kept separately from the suppression gate below: `is_dup` still
                # selects the "still failing" wording on an alert that DOES go out
                # because the reminder window has expired.
                is_dup = fh == job.last_failure_hash
                if self._failure_alert_is_duplicate(job, fh):
                    # record_failure() is the counter's sole owner: a suppressed
                    # duplicate is still a failed run, so it must count toward
                    # the auto-pause threshold like every other failure path —
                    # unless the gate verdict already counted THIS run, in which
                    # case counting again would pause on arithmetic rather than
                    # on five distinct failures, or the failure belonged to a
                    # shared runtime rather than to this job.
                    if not _gate_counted and _job_owns_failure:
                        _charge_failure()
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
                                f"❌ Job failed (suppressed — same error):\n{exc_detail}",
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
                            f"❌ Job failed:\n{exc_detail}",
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
                # Same sink and same source as the script/command alert, so it
                # goes through the one shared spelling of that hardening.
                safe_name = self._slack_safe_fenced(job.name)
                # Carry the reason here too. The dashboard body above and the
                # script/command DM both do, so leaving this one at "check logs"
                # made the DM the only failure surface that still withheld what
                # the caller already knows.
                safe_reason = self._slack_safe_fenced(exc_detail)
                # +1 only when the charge below will actually land. This run's
                # failure is recorded after the awaited Slack attempt, so the
                # display count has to add it explicitly -- but it is now gated on
                # _job_owns_failure, and an exempted run that still claimed
                # "N+1 consecutive failures" would report a number the counter
                # driving auto-pause never reached.
                _display_bump = 1 if (_job_owns_failure and not _gate_counted) else 0
                if is_dup:
                    fail_msg = (
                        f"⏰ *Cron: {safe_name}* ❌ _Job still failing on {escape_mrkdwn(host)}"
                        f" ({job.consecutive_failures + _display_bump} consecutive failures)"
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
                # Channel-neutral twin of ``fail_msg``: the same sentence with no
                # mrkdwn. Composed separately rather than reusing the escaped
                # form because Slack's markup is not another channel's dialect --
                # a fence and a ``&lt;`` would reach that reader literally -- and
                # separately from ``render_for_slack`` is how the success leg
                # composes its own channel string too.
                if is_dup:
                    channel_fail_msg = (
                        f"⏰ Cron: {job.name} ❌ Job still failing on {host}"
                        f" ({job.consecutive_failures + _display_bump} consecutive failures)"
                        f" — check logs.\n{exc_detail}"
                    )
                else:
                    channel_fail_msg = (
                        f"⏰ Cron: {job.name} ❌ Job failed on {host} — check logs.\n"
                        f"{exc_detail}"
                    )
                channel_fail_msg, _ = redact_exfiltration_urls(channel_fail_msg)
                channel_fail_msg, _ = redact_credentials(channel_fail_msg)
                # Silent jobs still execute but suppress notifications (UI bells
                # AND Slack DMs). The failure is still logged at warning level
                # and counted toward auto-pause above — we just skip
                # user-facing noise.
                # One spelling of the one-surface rule and both delivery legs,
                # shared with the script/command alert. `channel_fail_msg` rather
                # than `fail_msg` for the channel leg: that string is mrkdwn, no
                # other transport parses it, and the channel form also carries the
                # repeat-failure wording and both egress redaction passes.
                #
                # Placed before record_failure() deliberately: every await in this
                # handler must precede the counter, so a cancellation mid-alert
                # cannot leave the run counted twice.
                channel_delivered, slack_delivered, slack_failed = (
                    await self._deliver_failure_alert(
                        job,
                        mrkdwn=fail_msg,
                        plain=channel_fail_msg,
                        actor_key=session_key,
                        silent=job.silent,
                    )
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
                # verdict already counted THIS run, or when the failure belonged
                # to a runtime this job was sharing rather than to the job.
                if not _gate_counted and _job_owns_failure:
                    _charge_failure()
                if job.auto_paused:
                    logger.warning(
                        "Cron '%s' auto-paused after %d consecutive failures",
                        job.name,
                        job.consecutive_failures,
                    )
                # One spelling of the advance rule, shared with the
                # script/command alert: the anchor moves once the reason reached
                # someone, and only a REAL Slack exception holds it back.
                self._advance_failure_dedup(
                    job, fh, channel_delivered=channel_delivered, slack_failed=slack_failed
                )
                # The SEL record was nested INSIDE the advance condition before
                # this refactor, so a Slack exception suppressed the audit line as
                # well as the anchor. Preserved verbatim rather than quietly
                # widened -- whether the audit should be unconditional (the
                # script/command alert logs it either way) is a behaviour question,
                # not a consolidation one. Restating the condition is what makes
                # that gating visible instead of implied by indentation.
                if channel_delivered or not slack_failed:
                    # SEL logging is best-effort — never mask the original
                    # exception if audit logging itself fails.
                    try:
                        # Name every surface the alert actually left on, so the
                        # trail does not read "none" for a crash sent to Discord.
                        surfaces = ["slack"] if slack_delivered else []
                        if channel_delivered:
                            surfaces.append(channel_namespace_of(job.session_key))
                        sel().log_tool_invocation(
                            session_key=f"cron:{job.id}",
                            tool_name="cron_failure_alert",
                            outcome="suppressed" if job.silent else "alerted",
                            downstream_service=",".join(surfaces) or "none",
                        )
                    except Exception:
                        logger.debug(
                            "SEL logging failed in cron failure alert path",
                            exc_info=True,
                        )
                raise
            finally:
                assert self.sessions is not None
                # Before the reset below: a turn that consumed the post-compaction
                # flag but never landed puts it back so a session that survives
                # (deferred reset) re-injects on its next turn.
                rearm_reinjection(
                    self.sessions, session_key, consumed=_needs_reinjection, landed=_turn_landed
                )
                if _acquired:
                    self.sessions.release(session_key)
                    # Defer session reset if subagents are still running,
                    # queued behind the concurrency/stagger gate, or
                    # mid-injection — _subagent_done will reset after the last one.
                    has_pending = bool(
                        self.subagent_mgr
                        and await _subagent_work_pending(self.subagent_mgr, session_key)
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
                            self.cron_svc.clear_active_session_key(job.id, session_key)
                # Per-job env vars (single-agent path) travel via extra_env passthrough

        self._cron_reconciled = False
        self._cron_armed = False
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
            self._cron_reconciled = True
            if arm:
                await self._start_cron_after_memory_ready()

    async def _start_cron_after_memory_ready(self) -> None:
        """Arm overdue jobs only after the memory preparation fence completes."""
        if self._no_crons or self.cron_svc is None or not self._cron_reconciled:
            return
        if self._cron_armed:
            return
        startup = getattr(self, "_memory_startup", None)
        if startup is not None and (startup.stopped or not startup.ready):
            raise RuntimeError("Cron scheduler cannot start before memory preparation completes")
        await self.cron_svc.start()
        self._cron_armed = True
        if self.sessions:
            self.cron_svc.start_reaper(self.sessions)
        else:
            logger.warning("Cron reaper not started: sessions not available")

    async def _init_heartbeat(self) -> None:
        """Initialize and start the heartbeat service."""
        startup = getattr(self, "_memory_startup", None)
        if startup is not None and (startup.stopped or not startup.ready):
            raise RuntimeError("Heartbeat service cannot start before memory preparation completes")
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
                        fallback_models=configured_fallback_chain(),
                    ),
                    timeout=HEARTBEAT_TASK_TIMEOUT_SECS,
                )

                if not result_text:
                    result_text = "_No response._"
                result_text = _annotate_model_fallback(result_text, client)

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
                # ── Timeout spend is REAL spend. ──
                # A cancelled turn has already cost whatever it cost, so a
                # timed-out heartbeat writes a row rather than dropping that
                # silently. Record it here, BEFORE the session reset below tears
                # the client down and takes its last-turn usage with it.
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
                # Gate-side LOG line, so it takes the context spelling: a host with
                # a companion loaded must not have this text scanned with the
                # weaker OSS pass. Truncation comes AFTER redaction — the
                # invariant ``redact_log_via_context`` hands to its callers by
                # contract: slicing first would leave a credential as an
                # unmatchable fragment. The sibling below stays on
                # ``redact_and_truncate`` because it feeds a DELIVERY, not a log,
                # and the two want different failure modes on a non-composable
                # host.
                logger.info(
                    "Heartbeat task incomplete, suppressing delivery: %s",
                    redact_log_via_context(task_text)[:80],
                )
            else:
                task_safe = redact_and_truncate(task_text, 100)
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

    async def _fire_slack_nudge(
        self, loop: NudgeLoop, wake_message: str | None = None
    ) -> bool | MonitorDispatchResult:
        """Drive one unattended nudge turn in a Slack thread session.

        Mirrors the subagent-completion Slack injection: acquire the session,
        run the turn with auto-approval, post the reply into the originating
        thread, persist for dashboard replay. Returns True when the turn ran;
        False on skip (busy/unroutable/error) — the AutoNudge service re-arms
        with backoff on False.
        """
        key = loop.slot_key
        if self.sessions is None or self.slack is None:
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        if wake_message is not None and not await channel_inbound_permitted("slack"):
            logger.warning(
                "AutoNudge: Slack inbound policy denied structured wake for loop %s",
                loop.id,
            )
            return MonitorDispatchResult.UNAVAILABLE
        if self.sessions.is_busy(key):
            logger.info("AutoNudge skip: slack session %s busy (loop %s)", key, loop.id)
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        channel = self.sessions.get_channel(key)
        thread_ts = self.sessions.get_thread(key)
        if not thread_ts and key.startswith("slack:"):
            # Canonical keys embed the thread root ts.
            thread_ts = key.split(":", 1)[1]
        if not channel:
            logger.warning(
                "AutoNudge: slack session %s unroutable — removing loop %s", key, loop.id
            )
            if self.autonudge_svc and wake_message is None:
                await self.autonudge_svc.remove(loop.id, stop_reason="slack_unroutable")
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        if wake_message is None:
            # Snapshot message, sentinel AND config generation TOGETHER, before
            # the compose_nudge_body() await, so a concurrent PATCH during that
            # suspension cannot pair the old message with a new generation (which
            # would make the malformed verdict match the reconfigured loop and
            # wrongly stop it). The fence below compares this captured generation.
            _fired_message = loop.message
            _fired_sentinel = loop.stop_sentinel_path
            _fired_generation = loop.config_generation
            msg_body = await compose_nudge_body(_fired_message, _fired_sentinel, loop.slot_key)
            tagged = f"{nudge_cycle_header(loop)}\n{msg_body}"
        else:
            tagged = wake_message
            _fired_generation = loop.config_generation
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
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        response: str | None = None
        _acquired = False
        _turn_started = False
        _driver_completion_hook: MonitorCompletionHook | None = None
        _completion_hook: MonitorCompletionHook | None = None
        _raw_dispositions: list[MonitorActionDisposition] = []
        _completion_reported = False
        try:
            _memory_store = await session_store_for_turn(self.ctx_builder, key)
            if wake_message is None:
                client, is_new, _resumed = await self.sessions.get_or_create(key)
            else:
                client, is_new, _resumed = await self.sessions.get_or_create(
                    key, wait_if_busy=False
                )
            _acquired = True
            _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
            # An auto-nudge cycle continues the NUDGED session's own conversation,
            # so it reads that session's silo — resolved from its recorded binding,
            # the same key its consolidations are filed under. Without it a
            # crew-bound conversation gets nudged with the operator's own memory in
            # the prompt, and the reply it produces is then filed into the crew's
            # store as if the crew had said it.
            full_msg, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                tagged,
                is_new,
                key,
                memory_store=_memory_store,
                provider_type=_provider,
                context_provider=client,
                resumed=_resumed,
            )
            _completion_hook = self._monitor_completion_hook(loop)
            if wake_message is not None and _completion_hook is None:
                return MonitorDispatchResult.UNAVAILABLE
            # Clock started outside wait_for so BOTH the success path and the
            # TimeoutError branch below can report the real elapsed time. acp
            # never assigns TurnUsage.duration_ms, so the row needs this.
            _turn_t0 = time.monotonic()
            _turn_started = True
            if wake_message is None:

                def _capture_raw_completion(event: LLMEvent) -> None:
                    if is_monitor_completion_evidence(
                        event.stop_reason,
                        synthetic=event.synthetic_completion,
                    ):
                        _raw_dispositions.append(disposition_for_stop_reason(event.stop_reason))

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
                        on_complete=_capture_raw_completion,
                    ),
                    timeout=_NUDGE_TURN_TIMEOUT,
                )
            else:

                async def _capture_completion(completion: Any) -> None:
                    _raw_dispositions.append(completion.disposition)

                approval = self._interactive_approval("autonudge", nudge_key=key)
                sessions = self.sessions
                assert sessions is not None
                driver = TurnDriver(
                    client,
                    SilentRenderer(channel_type="slack"),
                    approval_mode=APPROVAL_INTERACTIVE,
                    decider=approval,
                    tool_gate=build_tool_gate(
                        self.ctx_builder,
                        session_key=key,
                        agent=_get_agent_for_session(key) or "",
                    ),
                    directive_consumer=build_directive_consumer(
                        session_key=key,
                        sessions=self.sessions,
                    ),
                    monitor_completion=(
                        (
                            _driver_completion_hook := MonitorCompletionHook(
                                _completion_hook.monitor_id,
                                _completion_hook.fingerprint,
                                _capture_completion,
                                authorization_callback=_completion_hook.authorization_callback,
                                acceptance_callback=_completion_hook.mark_accepted,
                            )
                        )
                        if _completion_hook is not None
                        else None
                    ),
                    closing_gate=lambda: sessions.begin_turn(key),
                )
                response = await asyncio.wait_for(
                    driver.run(full_msg),
                    timeout=_NUDGE_TURN_TIMEOUT,
                )
                if _driver_completion_hook is None or not _driver_completion_hook.accepted:
                    return MonitorDispatchResult.UNAVAILABLE
                assert _completion_hook is not None
                _completion_hook.mark_accepted()
            _turn_usage = provider_last_turn_usage(client)

            if _raw_dispositions:
                await self._report_monitor_completion(
                    loop,
                    _raw_dispositions[-1],
                    _turn_usage,
                    hook=_completion_hook,
                )
                _completion_reported = True
            # ── Per-turn usage row: attribute monitor spend. ──
            await _persist_turn_row(
                client,
                key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="monitor",
                agent_fallback=lambda: _get_agent_for_session(key),
                t0=_turn_t0,
                usage=_turn_usage,
            )
        except SessionBusyError:
            logger.info(
                "AutoNudge skip: slack session %s won by another turn (loop %s)",
                key,
                loop.id,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        except SessionClosingError:
            logger.info(
                "AutoNudge: refusing Slack monitor turn for %s during shutdown",
                key,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        except asyncio.CancelledError:
            if _turn_started and _raw_dispositions and not _completion_reported:
                _turn_usage = provider_last_turn_usage(client)
                await self._report_monitor_completion(
                    loop,
                    _raw_dispositions[-1],
                    _turn_usage,
                    hook=_completion_hook,
                )
            raise
        except asyncio.TimeoutError:
            # ── Timeout spend is REAL spend. ──
            # A timed-out nudge turn would otherwise fall through to the generic
            # handler below and write no row at all, silently dropping whatever
            # the cancelled turn had already cost. Record it, then bail. Runs
            # before the `finally` cancels/releases the session.
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
            _turn_usage = provider_last_turn_usage(client)
            if _raw_dispositions:
                await self._report_monitor_completion(
                    loop,
                    _raw_dispositions[-1],
                    _turn_usage,
                    hook=_completion_hook,
                )
            await _persist_turn_row(
                client,
                key,
                provider=(self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"),
                surface="monitor",
                agent_fallback=lambda: _get_agent_for_session(key),
                t0=_turn_t0,
                usage=_turn_usage,
            )
            if wake_message is None:
                return False
            if _driver_completion_hook is not None and _driver_completion_hook.accepted:
                return MonitorDispatchResult.DISPATCHED
            return MonitorDispatchResult.BUSY
        except Exception as exc:
            logger.exception("AutoNudge: slack nudge turn failed for %s (loop %s)", key, loop.id)
            await self._stop_message_loop_if_structural_terminal(
                loop, exc, wake_message, _fired_generation
            )
            if (
                wake_message is not None
                and _driver_completion_hook is not None
                and _driver_completion_hook.accepted
            ):
                assert _completion_hook is not None
                _completion_hook.mark_accepted()
                return MonitorDispatchResult.DISPATCHED
            if wake_message is None:
                return False
            return MonitorDispatchResult.BUSY
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
        return _delivery_result(wake_message, MonitorDispatchResult.DISPATCHED)

    async def _fire_dm_nudge(
        self,
        loop: NudgeLoop,
        adapter: _DmDispatchAdapter,
        wake_message: str | None = None,
    ) -> bool | MonitorDispatchResult:
        """Drive one unattended nudge turn in a dispatcher-routed DM session.

        Shared by every channel that delivers a nudge the way a real DM arrives:
        synthesize that channel's inbound type and hand it to the channel's own
        dispatcher, so busy/steer/queue handling, rendering, chunking and
        persistence behave like a user turn. ``interpret_commands=False`` keeps
        the nudge text from being read as a command.

        Four guards run before anything is delivered, and they are this caller's
        responsibility rather than the transport's precisely because a synthetic
        injection never passes through ``transport.receive``:

        1. The channel's transport and dispatcher are running. A gateway can
           have the channel disabled or still starting, which is a SKIP: the
           loop is fine and the next cycle may find the transport up.
        2. The binding key has the direct-message shape
           ``<channel>:{agent}:direct:{principal}[:genN]``. Any other shape
           cannot name a principal, so the loop can never fire and is retired.
        3. The principal is still on the inbound allow-list. The create
           endpoint checks it too, but an allow-list can SHRINK after a loop is
           armed, so the check is repeated here at fire time.
        4. The session generation still matches. A ``new``-style command mints a
           fresh key; firing into the rotated one would run in a session with
           none of the loop's context, and a stop issued from there could never
           find this loop. Retire instead of firing into the wrong generation.

        Then a busy session is a SKIP rather than a queue, so a cycle is not
        counted while the human's own turn is running.

        A retirement only happens for a plain nudge (``wake_message is None``).
        A structured monitor wake reports ``UNAVAILABLE`` and leaves the
        monitor's own lifecycle to decide, since the controller owns that
        record. Every outcome returns through :func:`_delivery_result`, so a
        caller asking for a plain nudge reads a bool and a caller carrying a
        wake reads the typed result.
        """
        key = loop.slot_key
        channel = adapter.channel
        if wake_message is not None and not adapter.supports_monitor:
            # Fail closed BEFORE delivering. A channel with no structured
            # dispatch cannot report whether the wake landed, so delivering it
            # and then answering UNAVAILABLE would show the text to the reader
            # while the controller treats the wake as undelivered and sends it
            # again. Refusing first keeps the two in agreement.
            logger.info(
                "AutoNudge: %s carries no monitor dispatch, refusing wake for loop %s",
                channel,
                loop.id,
            )
            return MonitorDispatchResult.UNAVAILABLE
        transports = getattr(self.dashboard_state, "channel_transports", None) or {}
        transport = transports.get(channel)
        dispatcher = transport.dispatcher if transport is not None else None
        if transport is None or dispatcher is None:
            logger.info(
                "AutoNudge skip: %s transport not running (loop %s)",
                channel,
                loop.id,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

        async def _retire(
            stop_reason: str, reason: str, *args: Any
        ) -> bool | MonitorDispatchResult:
            logger.warning("AutoNudge: " + reason, *args)
            if self.autonudge_svc and wake_message is None:
                await self.autonudge_svc.remove(loop.id, stop_reason=stop_reason)
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)

        parts = key.split(":")
        if len(parts) < 4 or parts[2] != "direct":
            return await _retire(
                "unsupported_key", "unsupported %s key %s, removing loop %s", channel, key, loop.id
            )
        principal = parts[3]
        if not adapter.authorize(transport, dispatcher, principal):
            return await _retire(
                "user_not_authorized", "%s user not authorized, removing loop %s", channel, loop.id
            )
        try:
            current_key = dispatcher.current_session_key(principal)
        except Exception:
            # A dispatcher that cannot answer is not evidence of rotation, so
            # treat the key as current and let the busy check and the dispatch
            # itself decide. Failing closed here would retire a healthy loop on
            # a transient lookup error.
            current_key = key
        if current_key != key:
            logger.info(
                "AutoNudge: %s session rotated (%s -> %s), removing loop %s",
                channel,
                key,
                current_key,
                loop.id,
            )
            if self.autonudge_svc and wake_message is None:
                await self.autonudge_svc.remove(loop.id, stop_reason="session_rotated")
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        sessions = getattr(dispatcher, "sessions", None)
        if sessions is not None and sessions.is_busy(key):
            logger.info(
                "AutoNudge skip: %s session %s busy (loop %s)",
                channel,
                key,
                loop.id,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

        if wake_message is None:
            msg_body = await compose_nudge_body(
                loop.message, loop.stop_sentinel_path, loop.slot_key
            )
            tagged = f"{nudge_cycle_header(loop)}\n{msg_body}"
        else:
            tagged = wake_message

        try:
            conversation_id = await adapter.resolve_conversation(
                transport, sessions, key, principal
            )
        except Exception:
            logger.exception(
                "AutoNudge: %s conversation lookup failed for %s (loop %s)",
                channel,
                key,
                loop.id,
            )
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)

        completion_hook: MonitorCompletionHook | None = None
        try:
            synthetic = adapter.build_inbound(principal, conversation_id, tagged)
            dispatch_kwargs: dict[str, Any] = {"interpret_commands": False}
            if adapter.supports_monitor:
                completion_hook = self._monitor_completion_hook(loop)
                if wake_message is not None and completion_hook is None:
                    return MonitorDispatchResult.UNAVAILABLE
                if completion_hook is not None:
                    dispatch_kwargs["monitor_completion"] = completion_hook
                    dispatch_kwargs["monitor_session_key"] = key
            # This turn is GENERATED, not received, so it does not count against
            # the conversation's turn ceiling: the loop already carries its own
            # cycle cap and runtime budget, and spending the conversation's budget
            # on it would latch the conversation and then refuse the human's next
            # message. Marked here rather than passed down because this is the one
            # place that knows, and the channels' dispatch signatures in between
            # have no business carrying it.
            with turn_ceiling.generated_turn():
                dispatch_result = await asyncio.wait_for(
                    dispatcher.handle_message(synthetic, **dispatch_kwargs),
                    timeout=_NUDGE_TURN_TIMEOUT,
                )
            if wake_message is not None:
                return (
                    dispatch_result
                    if isinstance(dispatch_result, MonitorDispatchResult)
                    else MonitorDispatchResult.UNAVAILABLE
                )
            if completion_hook is not None and isinstance(dispatch_result, MonitorDispatchResult):
                return dispatch_result is MonitorDispatchResult.DISPATCHED
            return True
        except Exception:
            logger.exception(
                "AutoNudge: %s nudge failed for %s (loop %s)",
                channel,
                key,
                loop.id,
            )
            if wake_message is None:
                return False
            if completion_hook is not None and completion_hook.accepted:
                return MonitorDispatchResult.DISPATCHED
            return MonitorDispatchResult.UNAVAILABLE

    async def _fire_discord_nudge(
        self, loop: NudgeLoop, wake_message: str | None = None
    ) -> bool | MonitorDispatchResult:
        """Drive one unattended nudge turn in a Discord DM session.

        The guard ladder and the delivery live in :meth:`_fire_dm_nudge`; this
        supplies only what is specific to Discord. Authorization is asked of the
        DISPATCHER here, which is the object holding Discord's inbound
        allow-list, and it mirrors the re-check ``on_interaction`` performs.
        """
        return await self._fire_dm_nudge(
            loop,
            _DmDispatchAdapter(
                channel="discord",
                supports_monitor=True,
                authorize=lambda _transport, dispatcher, principal: bool(
                    dispatcher.is_authorized(principal)
                ),
                resolve_conversation=(
                    lambda transport, _sessions, _key, principal: transport.resolve_conversation(
                        principal
                    )
                ),
                build_inbound=lambda principal, conversation_id, text: InboundMessage(
                    channel_type="discord",
                    user_id=principal,
                    conversation_id=conversation_id,
                    text=text,
                ),
            ),
            wake_message,
        )

    async def _fire_webex_nudge(self, loop: NudgeLoop) -> bool:
        """Drive one unattended nudge turn in a Webex DM session.

        The guard ladder and the delivery live in :meth:`_fire_dm_nudge`; this
        supplies only what is specific to Webex. Two of those three pieces carry
        a reason worth keeping beside them.

        Authorization is asked of the TRANSPORT, not the dispatcher, because the
        Webex allow-list is held there.

        The room is read from the persisted origin link when there is one.
        Webex's ``resolve_conversation`` answers with the EMAIL, which its send
        path maps onto ``toPersonEmail``, so it delivers correctly but is a
        SECOND spelling of the same room. An origin bind is matched by VALUE
        (see ``_origin_mirror_link``), so a nudge that writes the link in that
        other spelling makes a later ``/unlink`` miss the binding. The persisted
        link therefore wins, and the email is the first-turn fallback, where no
        binding exists to disagree with yet.

        Webex carries no structured-monitor dispatch, so ``supports_monitor`` is
        False and this path is only ever asked for a plain nudge.
        """

        async def _resolve(transport: Any, sessions: Any, key: str, principal: str) -> str:
            existing = sessions.get_origin_link(key) if sessions is not None else None
            room = getattr(existing, "channel_id", "")
            if room:
                return str(room)
            return str(await transport.resolve_conversation(principal))

        def _build(principal: str, conversation_id: str, text: str) -> Any:
            # Imported HERE, not at module scope: this file is on the gateway
            # boot path, and it deliberately keeps every channel client behind
            # TYPE_CHECKING so enabling one channel does not cost every launch
            # the import of all of them. Reached only when a Webex loop fires.
            from kiro_crew.webex.client import WebexInbound
            from kiro_crew.webex.transport import ROOM_DIRECT

            return WebexInbound(
                person_email=principal,
                room_id=conversation_id,
                text=text,
                room_type=ROOM_DIRECT,
            )

        result = await self._fire_dm_nudge(
            loop,
            _DmDispatchAdapter(
                channel="webex",
                supports_monitor=False,
                authorize=lambda transport, _dispatcher, principal: bool(
                    transport.is_authorized(principal)
                ),
                resolve_conversation=_resolve,
                build_inbound=_build,
            ),
        )
        # A plain nudge always normalizes to a bool through _delivery_result.
        assert isinstance(result, bool)
        return result

    async def _stop_message_loop_if_structural_terminal(
        self,
        loop: NudgeLoop,
        exc: BaseException,
        wake_message: str | None,
        fired_generation: int,
    ) -> bool:
        """Stop a MESSAGE loop whose fired turn raised a structural rejection.

        A malformed-request rejection ("Improperly formed request") is
        deterministic in the payload's SHAPE, so re-firing the identical nudge
        context can only reproduce it -- an undelivered cycle that the service
        would otherwise re-arm with backoff, forever, since undelivered cycles
        never reach ``max_cycles``. This is the CHANNEL-adapter counterpart to
        the dashboard fire path's pre-dispatch guard: a channel adapter runs its
        turn inline and holds the exception directly, so it reads the verdict off
        the exception (``AcpError.structural_terminal``) rather than through the
        slot flag the dashboard path relays. Scoped to message loops
        (``wake_message is None``): a structured monitor wake carries its own
        actionable context, not this repeated prompt, and keeps its own dispatch
        contract. Returns True when it stopped the loop. getattr-guarded: only
        AcpError carries the attribute.

        ``fired_generation`` is ``loop.config_generation`` captured at fire time.
        The stop is applied through ``AutoNudgeService.update(expected_generation
        =...)``, which compares it against the loop's CURRENT generation UNDER
        THE SERVICE LOCK: an inline channel turn can be slow, and a concurrent
        ``PATCH /api/autonudge/{id}`` can change the instruction (advancing the
        generation) WHILE the turn runs -- possibly back to the same text
        (A->B->A). The atomic fence refuses the stale stop with no TOCTOU window;
        a value compare on ``message`` could not tell a re-committed A apart.
        """
        if wake_message is not None:
            return False
        if not getattr(exc, "structural_terminal", False):
            return False
        if self.autonudge_svc is None:
            return False
        stopped_loop = await self.autonudge_svc.update(
            loop.id,
            active=False,
            stopped_reason=STRUCTURAL_TERMINAL_REASON,
            expected_generation=fired_generation,
        )
        if stopped_loop is None or stopped_loop.active:
            # The fence refused: the loop's config generation advanced under the
            # in-flight turn, so this malformed verdict belongs to an OLD
            # instruction and must not deactivate the reconfigured loop.
            logger.info(
                "AutoNudge: loop %s on %s not stopped — its config generation "
                "advanced while the malformed turn ran, so the rejection does "
                "not apply to the current instruction",
                loop.id,
                loop.slot_key,
            )
            return False
        logger.warning(
            "AutoNudge: loop %s on %s stopped — its delivered turn was rejected "
            "as structurally malformed, so re-firing the same context cannot "
            "help; the loop stays inactive and a later directive (after a fresh "
            "conversation) may re-arm it",
            loop.id,
            loop.slot_key,
        )
        return True

    async def _fire_dashboard_nudge(
        self, loop: NudgeLoop, wake_message: str | None = None
    ) -> bool | MonitorDispatchResult:
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
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
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
                if wake_message is None:
                    await self.autonudge_svc.remove(  # type: ignore[union-attr]
                        loop.id, stop_reason="session_unreachable"
                    )
                return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
            logger.info(
                "AutoNudge: rehydrated session %s from history for loop %s",
                loop.slot_key,
                loop.id,
            )
        # STRUCTURAL-TERMINAL GUARD (message loops only). If the slot's LAST
        # delivered turn ended on a malformed-request rejection, the backend
        # refused the payload's SHAPE, deterministically -- re-injecting the same
        # nudge context can only reproduce it. Firing again would spend cycle
        # after cycle (the reported cycles 13, 14, ...) on an identical doomed
        # turn, so STOP the loop instead. The verdict is scoped to the loop id
        # (``_last_turn_structural_terminal_loop_id``) AND applied under an ATOMIC
        # (id, generation) fence in AutoNudgeService.update(expected_generation=):
        # the loop's config generation captured at fire time
        # (``_last_turn_structural_terminal_loop_gen``) must still match under the
        # service lock, or the completion is a STALE result of an OLD instruction
        # (the A->B->A race) and the stop is refused there with no TOCTOU window.
        # The loop stays INACTIVE when stopped; the stop is REPLACEABLE
        # (STRUCTURAL_TERMINAL_REASON), which does not re-arm on its own -- it
        # only PERMITS a later directive to re-arm the loop, and any such re-arm
        # advances the generation so this verdict cannot follow it. The slot's
        # verdict is cleared at the start of every genuine new turn (chat_runner).
        # A structured monitor WAKE (``wake_message is not None``) carries its own
        # actionable context and is out of scope here. Tested with ``is True``
        # (not truthiness) so a bare MagicMock slot's truthy attribute cannot
        # trip it; getattr keeps minimal slot doubles safe.
        if (
            wake_message is None
            and getattr(slot, "_last_turn_structural_terminal", False) is True
            and getattr(slot, "_last_turn_structural_terminal_loop_id", "") == loop.id
            and self.autonudge_svc is not None
        ):
            _expected_gen = int(getattr(slot, "_last_turn_structural_terminal_loop_gen", 0) or 0)
            stopped_loop = await self.autonudge_svc.update(  # type: ignore[union-attr]
                loop.id,
                active=False,
                stopped_reason=STRUCTURAL_TERMINAL_REASON,
                expected_generation=_expected_gen,
            )
            # The fence answers three cases, and only ONE may dispatch:
            #   * inactive loop  -> stopped (verdict applied): return UNAVAILABLE.
            #   * None            -> update refused the mutation because the loop
            #     is quiescing/removed under maintenance (``_acquire_mutation_lock``
            #     returns None), NOT a live target: return UNAVAILABLE, matching
            #     the sibling ``_stop_message_loop_if_structural_terminal`` seam,
            #     which treats None as not-a-live-loop.
            #   * still-active loop -> the fence refused because the config
            #     generation advanced under the turn (stale completion): fall
            #     through and dispatch the reconfigured loop.
            # So dispatch happens ONLY for a non-None ACTIVE loop; a None must not
            # be conflated with "refused, still firing".
            if stopped_loop is None or not stopped_loop.active:
                logger.warning(
                    "AutoNudge: loop %s on slot %s not dispatched — its last "
                    "delivered turn was rejected as structurally malformed and "
                    "the loop is stopped or is not a live target; re-firing "
                    "the same context cannot help, and a later directive (after "
                    "a fresh conversation) may re-arm it",
                    loop.id,
                    loop.slot_key,
                )
                return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
            logger.info(
                "AutoNudge: loop %s structural stop skipped — config generation "
                "advanced under the fired turn, so the verdict is stale",
                loop.id,
            )
        if wake_message is None:
            # Snapshot message, sentinel AND config generation TOGETHER, before
            # the compose_nudge_body() await: a concurrent PATCH during that
            # suspension could otherwise pair the OLD message with the NEW
            # generation, so the malformed verdict would match the reconfigured
            # loop's generation and wrongly stop it. The generation recorded with
            # the verdict must be the one that goes with the message the turn
            # actually runs.
            _fired_message = loop.message
            _fired_sentinel = loop.stop_sentinel_path
            _fired_generation = loop.config_generation
            msg = await compose_nudge_body(_fired_message, _fired_sentinel, loop.slot_key)
            tagged = f"{nudge_cycle_header(loop)}\n{msg}"
        else:
            tagged = wake_message
        # ONE STRING, TWO CONSUMERS, and only an opt-in ``banner`` splits them.
        # ``tagged`` is the PROMPT and is never shortened — re-delivering the
        # whole instruction every cycle is the guarantee the nudge exists to
        # provide. ``visible`` is the transcript row, which a reader consults
        # only to learn that a cycle happened. Without a banner it IS ``tagged``,
        # so an existing loop's row is byte-identical to today's.
        #
        # A banner deliberately skips ``compose_nudge_body``: that composer
        # prefixes the work-ledger snapshot, which the model wants and a display
        # line does not. ``render_nudge_message`` still applies, so
        # ``{{STOP_FILE}}`` resolves in a banner as it does in a message.
        #
        # A banner is a MESSAGE-loop concept: a monitor wake (``wake_message``)
        # shows its own actionable-wake row, so the banner only splits the row
        # on the ``wake_message is None`` arm.
        #
        # ``isinstance`` rather than a bare falsiness test: ``banner: str`` is a
        # plain dataclass annotation, unenforced at runtime, and ``_load`` builds
        # a loop straight from parsed JSON — so a store carrying ``"banner": 5``
        # yields ``loop.banner == 5`` and ``.strip()`` on it would raise
        # ``AttributeError``, killing the fire and (since the service re-arms an
        # undelivered cycle) rearming the loop forever. A whitespace-only banner
        # is truthy too and its blank row is worse than the verbose one, so both
        # fall through to ``tagged``.
        banner = loop.banner.strip() if isinstance(loop.banner, str) else ""
        if banner and wake_message is None:
            # Credential redaction lives at the banner's single owner — the
            # authorized write paths (incl. /goal via ``normalize_banner``) and
            # ``_load`` for a hand-edited store — so ``loop.banner`` is already
            # scrubbed here and every egress (this row, ``GET /api/autonudge``,
            # the WS broadcast) serves the same scrubbed value. No per-fire,
            # per-field scrub at this sink.
            shown = render_nudge_message(banner, loop.stop_sentinel_path)
            visible = f"[auto-nudge cycle {loop.cycle_count + 1}]\n{shown}"
        else:
            visible = tagged
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
            return _delivery_result(wake_message, MonitorDispatchResult.BUSY)
        # Crew/member slot boundary, for EVERY dashboard loop -- prompt loops
        # included, not only the structured/gated ones the completion hook
        # covers below. Such a slot accepts a wake only from a loop its own
        # turn armed, proven by the persisted bit AND the keystone-gated trust
        # record together (see ``_dashboard_mode_admits``). The hook path
        # re-checks right before provider entry for the TOCTOU window; this is
        # the gate that applies when there is no hook at all.
        if not await self._dashboard_mode_admits(loop, slot):
            await self._audit_fire_refused(loop, slot)
            return _delivery_result(wake_message, MonitorDispatchResult.UNAVAILABLE)
        # Show nudge as a distinct "nudge" role message in the slot history.
        # The structured meta lets the dashboard render a compact cycle chip
        # instead of echoing the whole instruction payload as a chat bubble.
        # The tag stays in ``content`` because that is what the model reads,
        # and the body is deliberately NOT duplicated into meta — the client
        # derives it from content, so a multi-KB payload is stored and
        # broadcast once rather than twice. ``visible`` rather than ``tagged``
        # in the appended row: identical unless the loop opted into a ``banner``,
        # in which case this transcript row is the only thing shortened while the
        # full ``tagged`` prompt still reaches ``_run_chat``.
        nudge_meta: dict[str, Any] = {
            "nudge": {
                "cycle": loop.cycle_count + 1,
                "loop_id": loop.id,
            }
        }
        if wake_message is not None and loop.monitor is not None:
            nudge_meta["monitor"] = {
                "id": loop.id,
                "fingerprint": loop.monitor.last_wake_fingerprint,
                "classification": (
                    loop.monitor.last_decision.value
                    if loop.monitor.last_decision is not None
                    else "actionable"
                ),
            }
        completion_hook = self._monitor_completion_hook(loop)
        if wake_message is not None and completion_hook is None:
            return MonitorDispatchResult.UNAVAILABLE
        dashboard_state = self.dashboard_state
        turn_slot = slot
        assert dashboard_state is not None and turn_slot is not None

        def _append_nudge() -> None:
            turn_slot.append(
                "nudge",
                visible,
                "msg msg-nudge",
                meta=nudge_meta,
            )

        if completion_hook is None:
            _append_nudge()
        # FIX 2: an unattended app-owned nudge turn runs under the background
        # concurrency cap. This is the fleet's hot path — N armed loops fire
        # independently and would otherwise put N turns on the runtime at once.
        # An attended slot (any user session with a monitor loop) is passed
        # straight through, so babysit loops on human sessions are unaffected.
        admission: asyncio.Future[MonitorDispatchResult] | None = None
        if completion_hook is not None:
            admission = asyncio.get_running_loop().create_future()

        def _settle_admission(result: MonitorDispatchResult) -> None:
            if admission is not None and not admission.done():
                admission.set_result(result)

        if completion_hook is not None:
            base_hook = completion_hook

            async def _authorize_dashboard_turn(monitor_id: str, fingerprint: str) -> bool:
                current_slot = dashboard_state.get_slot(loop.slot_key)
                # A crew/member slot refuses a wake armed from OUTSIDE the
                # session; a loop the slot's OWN turn armed is the member keeping
                # itself awake and must fire. Same rule as ``autonudge_authz``.
                # TWO sources must agree, because the loop store is agent-
                # writable and this is the one bit that relaxes a session
                # boundary: the persisted ``self_armed`` must be the boolean True
                # (``is True`` -- a forged string is truthy; ``_load`` normalises
                # too) AND the keystone-gated trust record the authorizer wrote
                # at arm time (``autonudge_selfarm``, which agent file tools
                # cannot reach) must name this loop on this slot. A forged
                # boolean in the store has no trust entry and refuses.
                mode_refused = not await self._dashboard_mode_admits(loop, turn_slot)
                if mode_refused:
                    await self._audit_fire_refused(loop, turn_slot)
                if (
                    current_slot is not turn_slot
                    or bool(getattr(turn_slot, "is_closing", False))
                    or mode_refused
                    or str(getattr(turn_slot, "memory_mode", "persistent")) != "persistent"
                ):
                    _settle_admission(MonitorDispatchResult.UNAVAILABLE)
                    return False
                callback = base_hook.authorization_callback
                authorized = True if callback is None else await callback(monitor_id, fingerprint)
                if not authorized:
                    _settle_admission(MonitorDispatchResult.UNAVAILABLE)
                return authorized

            def _accept_dashboard_turn() -> None:
                callback = base_hook.acceptance_callback
                if callback is not None:
                    callback()
                _append_nudge()
                _settle_admission(MonitorDispatchResult.DISPATCHED)

            completion_hook = MonitorCompletionHook(
                base_hook.monitor_id,
                base_hook.fingerprint,
                base_hook.callback,
                authorization_callback=_authorize_dashboard_turn,
                acceptance_callback=_accept_dashboard_turn,
            )

        run_kwargs: dict[str, Any] = {}
        # This turn IS the loop's delivered wake on its own slot. The directive
        # consumer treats that as self-arm provenance alongside a human-started
        # turn, so a member re-arming or revising its loop from inside a cycle
        # is admitted; a cron, app or sub-agent turn on the same slot never
        # carries this mark. On a crew/member slot the wake only exists because
        # ``_dashboard_mode_admits`` already proved the loop self-armed.
        run_kwargs["_directive_self_wake"] = True
        # Scope the structural-terminal verdict this turn may record to THIS loop
        # and the CONFIG GENERATION it fires under (the snapshot captured with the
        # message above, before compose_nudge_body's await), so the stop is
        # applied via an atomic (id, generation) fence and a stale completion
        # cannot deactivate a loop whose config advanced under the turn.
        run_kwargs["_directive_loop_id"] = loop.id
        run_kwargs["_directive_loop_gen"] = _fired_generation if wake_message is None else 0
        if completion_hook is not None:
            run_kwargs["monitor_completion"] = completion_hook
            # Structured monitor turns own a single durable budgeted turn.
            # Nested depth disables dashboard recovery paths that would enqueue
            # an additional provider turn outside that accounting boundary.
            run_kwargs["_prompt_depth"] = 1

        async def _run_dashboard_turn() -> None:
            # Unattended slots can wait behind the background-turn semaphore.
            # Reject a revoked structured claim before entering ``_run_chat``:
            # prompt-submit hooks run during its setup and must not observe a
            # monitor the user stopped while this turn waited for admission.
            # The runner retains its own final recheck immediately before
            # provider entry to cover revocation during that setup.
            if completion_hook is not None and not await completion_hook.authorize():
                return
            await _run_chat(
                dashboard_state,
                turn_slot,
                tagged,
                _directive_user_origin=False,
                **run_kwargs,
            )

        async def _run_background_turn() -> None:
            try:
                await dashboard_state.run_background_turn(turn_slot, _run_dashboard_turn())
            except TimeoutError:
                _settle_admission(MonitorDispatchResult.BUSY)
            except asyncio.CancelledError:
                _settle_admission(MonitorDispatchResult.BUSY)
                raise
            except Exception:
                _settle_admission(MonitorDispatchResult.UNAVAILABLE)
                raise

        if admission is not None:
            turn_coro = _run_background_turn()
        else:
            turn_coro = dashboard_state.run_background_turn(
                turn_slot,
                _run_dashboard_turn(),
            )
        task = spawn_guarded_turn(dashboard_state, turn_slot, turn_coro)
        # Mirror dashboard /api/chat/send path so slot.running == True and sidebar
        # shows the "turn active" three-dots indicator immediately.
        slot.task = task
        self._session_tasks[slot.key] = task
        self.dashboard_state.push_slots_update()
        if admission is not None:

            def _settle_unstarted_admission(_task: asyncio.Task[Any]) -> None:
                _settle_admission(MonitorDispatchResult.BUSY)

            task.add_done_callback(_settle_unstarted_admission)
            return _delivery_result(wake_message, await admission)
        return _delivery_result(wake_message, MonitorDispatchResult.DISPATCHED)

    @staticmethod
    async def _dashboard_mode_admits(loop: NudgeLoop, slot: Any) -> bool:
        """Whether *slot*'s mode admits a wake from *loop*.

        Any mode but crew/member admits. A crew/member slot refuses a wake armed
        from OUTSIDE the session; a loop the slot's OWN turn armed is the
        member keeping itself awake and must fire. Same rule as
        ``autonudge_authz``. TWO sources must agree, because the loop store is
        agent-writable and this is the one bit that relaxes a session boundary:
        the persisted ``self_armed`` must be the boolean True (``is True`` -- a
        forged string is truthy; ``_load`` normalises too) AND the
        keystone-gated trust record the authorizer wrote at arm time
        (``autonudge_selfarm``, unreachable by agent file tools) must name this
        loop on this slot. A forged boolean in the store has no trust entry and
        refuses. The record read is file IO, so it is offloaded.
        """
        if str(getattr(slot, "mode", "")) not in {"crew", "member"}:
            return True
        if getattr(loop, "self_armed", False) is not True:
            return False
        return bool(
            await asyncio.to_thread(autonudge_selfarm.is_recorded_self_arm, loop.id, loop.slot_key)
        )

    @staticmethod
    async def _audit_fire_refused(loop: NudgeLoop, slot: Any) -> None:
        """SEL-record a fire-time refusal at the crew/member boundary.

        A permission decision that keeps an unattended turn OUT of a session is
        as audit-worthy as the arm that let one in (backend-security-controls):
        without it an operator reading the trail sees a loop armed and never
        fired, with nothing saying why. Best-effort and offloaded -- the
        refusal stands whether or not the write lands.
        """
        mode = str(getattr(slot, "mode", ""))
        try:
            await asyncio.to_thread(
                lambda: sel().log_tool_invocation(
                    session_key=loop.slot_key,
                    source="autonudge",
                    tool_name="monitor_fire",
                    outcome="denied",
                    error=f"{mode}-mode session refuses a wake it did not arm itself",
                    metadata={
                        "loop_id": loop.id,
                        "self_armed_bit": getattr(loop, "self_armed", False) is True,
                    },
                )
            )
        except Exception:  # noqa: BLE001 - auditing must never break the fire path
            logger.warning("fire-refusal SEL audit failed for loop %s", loop.id, exc_info=True)

    def _monitor_completion_hook(self, loop: NudgeLoop) -> MonitorCompletionHook | None:
        """Bind a structured loop's in-flight identity to controller accounting."""
        state = getattr(loop, "monitor", None)
        if state is None:
            return None
        service = self.autonudge_svc
        if service is None or not state.wake_in_flight or not state.last_wake_fingerprint:
            return None
        mark_accepted = getattr(service, "mark_monitor_turn_accepted", None)
        return MonitorCompletionHook(
            loop.id,
            state.last_wake_fingerprint,
            service.record_monitor_turn_completion,
            authorization_callback=getattr(service, "monitor_dispatch_is_authorized", None),
            acceptance_callback=(
                (lambda: mark_accepted(loop.id, state.last_wake_fingerprint))
                if mark_accepted is not None
                else None
            ),
        )

    async def _report_monitor_completion(
        self,
        loop: NudgeLoop,
        disposition: MonitorActionDisposition,
        usage: AgentTurnUsage | None,
        *,
        hook: MonitorCompletionHook | None = None,
    ) -> None:
        """Best-effort monitor accounting that cannot change delivery outcome."""
        if hook is None:
            hook = self._monitor_completion_hook(loop)
        if hook is None:
            return
        try:
            await hook.complete(disposition, usage)
        except Exception:
            logger.warning(
                "monitor turn completion callback failed for loop %s",
                loop.id,
                exc_info=True,
            )

    async def _init_autonudge(self) -> None:
        """Initialize and start the auto-nudge service (feature-flagged)."""
        if not autonudge_enabled():
            logger.info("AutoNudge disabled via feature flag")
            return

        # Keep the disabled gateway boot path free of controller and provider
        # imports. Provider adapters load credentials and client dependencies
        # that a gateway with automation disabled never uses.
        from kiro_crew.monitoring.controller import MonitorController

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
                    result = await self._fire_slack_nudge(loop)
                    assert isinstance(result, bool)
                    return result
                if loop.slot_key.startswith("discord:"):
                    result = await self._fire_discord_nudge(loop)
                    assert isinstance(result, bool)
                    return result
                if loop.slot_key.startswith("webex:"):
                    return await self._fire_webex_nudge(loop)
                logger.warning(
                    "AutoNudge: unsupported channel key %s — removing loop %s",
                    loop.slot_key,
                    loop.id,
                )
                await self.autonudge_svc.remove(  # type: ignore[union-attr]
                    loop.id, stop_reason="unsupported_channel"
                )
                return False
            result = await self._fire_dashboard_nudge(loop)
            assert isinstance(result, bool)
            return result

        async def _fire_monitor(loop: NudgeLoop, envelope: str) -> MonitorDispatchResult:
            """Route one controller-owned envelope without legacy decoration."""
            if loop.slot_key.startswith("slack:"):
                result = await self._fire_slack_nudge(loop, envelope)
                if not isinstance(result, MonitorDispatchResult):
                    logger.error("Slack monitor dispatcher returned an untyped result")
                    return MonitorDispatchResult.UNAVAILABLE
                return result
            if loop.slot_key.startswith("discord:"):
                result = await self._fire_discord_nudge(loop, envelope)
                if not isinstance(result, MonitorDispatchResult):
                    logger.error("Discord monitor dispatcher returned an untyped result")
                    return MonitorDispatchResult.UNAVAILABLE
                return result
            if is_channel_key(loop.slot_key):
                return MonitorDispatchResult.UNAVAILABLE
            result = await self._fire_dashboard_nudge(loop, envelope)
            if not isinstance(result, MonitorDispatchResult):
                logger.error("Dashboard monitor dispatcher returned an untyped result")
                return MonitorDispatchResult.UNAVAILABLE
            return result

        controller: MonitorController | None = None
        notified_monitor_terminals: set[tuple[str, MonitorOutcome, float]] = set()

        async def _record_terminal_notification_delivery(
            monitor_id: str,
            outcome: MonitorOutcome,
            stopped_at: float,
        ) -> None:
            try:
                assert self.autonudge_svc is not None
                await self.autonudge_svc.mark_terminal_notification_delivered(
                    monitor_id,
                    outcome,
                    stopped_at,
                )
            except Exception:
                # Delivery already happened. Leaving the marker unset makes a
                # later restart retry the notice instead of losing it forever.
                logger.warning(
                    "AutoNudge: could not persist terminal notification delivery for %s",
                    monitor_id,
                    exc_info=True,
                )

        async def _deliver_terminal_notification(
            loop: NudgeLoop,
            terminal_key: tuple[str, MonitorOutcome, float],
        ) -> None:
            notification_persisted = False
            try:
                if not self._notify_nudge_expired(loop):
                    return
                assert self.dashboard_state is not None
                # notify() synchronously installs the append future before it
                # returns. Capture that exact future before yielding so another
                # notification cannot replace the state-wide pointer underneath
                # this terminal generation.
                persisted = self.dashboard_state.last_notification_persist
                if persisted is not None and not await persisted:
                    logger.warning(
                        "AutoNudge: terminal notification persistence failed for %s",
                        terminal_key[0],
                    )
                    return
                notification_persisted = True
            except Exception:
                logger.warning(
                    "AutoNudge: terminal notification persistence raised for %s",
                    terminal_key[0],
                    exc_info=True,
                )
                return
            finally:
                # The observer may emit the same terminal generation again.
                # Release its in-process claim until a durable notice exists.
                if not notification_persisted:
                    notified_monitor_terminals.discard(terminal_key)
            await _record_terminal_notification_delivery(*terminal_key)

        def _schedule_terminal_notification(
            loop: NudgeLoop,
            terminal_key: tuple[str, MonitorOutcome, float],
        ) -> None:
            task = asyncio.create_task(_deliver_terminal_notification(loop, terminal_key))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        async def _monitor_tick(loop: NudgeLoop) -> None:
            if controller is not None:
                await controller.tick(loop, now=time.time())

        def _observer(event: str, loop: NudgeLoop | None) -> None:
            if event == "expired" and loop is not None:
                self._notify_nudge_expired(loop)
            elif (
                loop is not None
                and is_structured_monitor_loop(loop)
                and loop.monitor is not None
                and not loop.active
            ):
                state = loop.monitor
                if state.outcome in {
                    MonitorOutcome.SUCCESS,
                    MonitorOutcome.BLOCKED,
                    MonitorOutcome.BUDGET,
                    MonitorOutcome.TARGET_UNAVAILABLE,
                }:
                    terminal_key = (loop.id, state.outcome, state.stopped_at)
                    if terminal_key not in notified_monitor_terminals:
                        notified_monitor_terminals.add(terminal_key)
                        _schedule_terminal_notification(loop, terminal_key)
            if self.dashboard_state and loop is not None:
                loop_payload: dict[str, Any] = {
                    "id": loop.id,
                    "slot_key": loop.slot_key,
                    "message": loop.message,
                    "idle_secs": loop.idle_secs,
                    "max_cycles": loop.max_cycles,
                    "max_runtime_secs": loop.max_runtime_secs,
                    "cycle_count": loop.cycle_count,
                    "active": loop.active,
                    "last_fire_ts": loop.last_fire_ts,
                    # On EVERY loop, not only a structured monitor's: the goal
                    # popover words a paused loop by ``stopped_reason`` and reads
                    # its countdown off ``next_due_ts``, and the dashboard caches
                    # the frame over the REST read, so a frame without them blanks
                    # both the moment it lands. Both are public on the ungated
                    # REST list already.
                    "next_due_ts": loop.next_due_ts,
                    "stopped_reason": loop.stopped_reason,
                }
                if is_structured_monitor_loop(loop):
                    assert loop.monitor is not None
                    loop_payload["monitor"] = _redact_monitor_value(
                        monitor_state_public_dict(loop.monitor)
                    )
                broadcast = (
                    self.dashboard_state.broadcast_ws_owners
                    if is_structured_monitor_loop(loop)
                    else self.dashboard_state.broadcast_ws
                )
                _frame = {
                    "event": event,
                    "slot": loop.slot_key,
                    "loop": loop_payload,
                }

                def _publish(frame: dict = _frame) -> None:
                    broadcast("autonudge_state", frame)

                # Per-member event log: a member's DM-slot patrol started or
                # stopped. The log -- not the loop -- is what the Crew Members
                # drawer reads for a patrol's stop REASON across a restart:
                # `reconcile_members_at_startup` closes a log that still reads
                # `armed` with no live loop as reason='interrupted', so a
                # transition published BEFORE its append landed lets a crash in
                # that window replace the real reason (`runtime_budget`, say)
                # permanently, with nothing able to recover it. The append
                # therefore runs FIRST and the publish follows it, which is what
                # `persist-before-you-publish` requires of a state a record owns.
                #
                # Still queued on the ordered executor whether or not this is the
                # serving thread: one queue is what orders a rapid start/stop, and
                # an inline write from a non-loop caller could land ahead of an
                # append already queued. So the publish is scheduled back onto the
                # loop from that one worker, because `broadcast_ws` is loop-affine
                # and a synchronous durability barrier on the observer would stall
                # every concurrent session.
                _publish_deferred = False
                # Whether a DURABLE transition applies to this event at all. It is
                # not the same question as `_publish_deferred`, and conflating the
                # two is what published unpersisted state: a false
                # `_publish_deferred` means "no worker will publish", which covers
                # both "there was nothing to persist" (this path may publish) and
                # "the append was REFUSED" (it must not).
                _ledger_owns_frame = False
                try:

                    from kiro_crew import eventlog_hooks
                    from kiro_crew.eventlog.types import PATROL_STARTED, PATROL_STOPPED

                    _pslug = eventlog_hooks.member_slug_for_slot(loop.slot_key)
                    _etype2: str | None = None
                    _edata: dict = {}
                    if event == "added":
                        _etype2, _edata = PATROL_STARTED, {"slot_key": loop.slot_key}
                    elif event in ("removed", "expired"):
                        _reason = getattr(loop, "stopped_reason", None) or event
                        _etype2, _edata = (
                            PATROL_STOPPED,
                            {"slot_key": loop.slot_key, "reason": _reason},
                        )
                    if _pslug is not None and _etype2 is not None:
                        _ledger_owns_frame = True
                        _pslug_s: str = _pslug
                        _etype_s: str = _etype2
                        try:
                            _loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
                        except RuntimeError:
                            # A non-loop caller: it was already publishing from
                            # its own thread before this, so keep that shape
                            # rather than drop the append to preserve it.
                            _loop = None

                        def _emit_patrol(
                            slug: str = _pslug_s, etype: str = _etype_s, data: dict = _edata
                        ) -> None:
                            landed = False
                            try:
                                landed = bool(eventlog_hooks.emit(slug, None, etype, data))
                            except Exception:
                                logger.debug("patrol event-log emit failed", exc_info=True)
                            if not landed:
                                # NOT published. The frame would assert a transition
                                # the ledger does not hold, and the ledger is what
                                # the drawer reads for the stop reason after a
                                # restart -- so publishing here is the
                                # report-success-on-a-failed-write shape that
                                # `persist-before-you-publish` forbids. The refusal
                                # is not silent either: the log still reads `armed`,
                                # and `reconcile_members_at_startup` closes exactly
                                # that state with an explicitly terminal
                                # `interrupted`, which is the state the rule asks a
                                # failed transition to leave behind.
                                logger.debug(
                                    "patrol %s for %s was not persisted; frame withheld",
                                    etype,
                                    slug,
                                )
                                return
                            # Published only AFTER the append landed, and from the
                            # loop, because `broadcast_ws` is loop-affine.
                            try:
                                if _loop is not None:
                                    _loop.call_soon_threadsafe(_publish)
                                else:
                                    _publish()
                            except Exception:
                                logger.debug("patrol publish failed", exc_info=True)

                        _publish_deferred = eventlog_hooks.submit(_emit_patrol)
                except Exception:
                    logger.debug("patrol event-log hook failed", exc_info=True)
                if not _publish_deferred and not _ledger_owns_frame:
                    # Publish directly ONLY when this event has no durable meaning --
                    # not a member slot, or a type the log does not record -- so there
                    # is no ledger state for the frame to contradict.
                    #
                    # A refused queue deliberately falls through here without
                    # publishing. `_publish_deferred` alone cannot tell a refusal
                    # from "nothing to persist", and publishing on refusal asserts a
                    # transition the ledger never received, which is what
                    # `persist-before-you-publish` forbids. The state is not lost:
                    # the log still reads `armed` and `reconcile_members_at_startup`
                    # closes exactly that with an explicitly terminal `interrupted`.
                    _publish()

        async def _collect_judge_evidence(loop: NudgeLoop) -> tuple[list[dict], int, dict]:
            """The wake judge's evidence for one tick: new worker rows, plus the probe.

            A closure rather than a method on the service, for the reason ``_fire`` and
            ``_monitor_owner_session_id`` are: authorizing a transcript read needs
            ``dashboard_state``, which ``AutoNudgeService`` does not hold.

            Creator-only by REUSE, not by a second check. ``read_messages`` calls
            ``authorize_target`` before it returns a row, so a target this loop's owner
            may not read raises and is counted as dropped. Nothing here decides who may
            read what.

            ``since`` is the loop's own per-target cursor, so each tick sees only what
            arrived after the last one, and the cursor advances only on a read that
            actually returned -- a refusal leaves it where it was rather than skipping
            the rows it would have served.
            """
            from kiro_crew import autonudge_judge as _judge
            from kiro_crew.dashboard import session_control as _sc

            state = self.dashboard_state
            if state is None:
                # No cursors either, and an empty map is the truthful third value: with
                # no dashboard state nothing was read, so nothing advanced.
                return [], 0, {}

            async def _read_session(target: str, since: int) -> tuple[list[dict], int]:
                # Off the loop: this authorizes, may write a SEL row, and reads slot
                # state. Raises on refusal, which the collector counts as a drop.
                #
                # The limit MUST match what the collector retains. ``next_since``
                # follows the returned window, and ``session_evidence`` keeps only the
                # last ``MAX_ROWS_PER_TARGET`` rows, so a wider page advances the
                # cursor across rows that are then discarded and never read again: a
                # 20-row burst loses its oldest 8, which is where an actionable line
                # sits when a worker posted several since the last tick. Reading
                # exactly what is retained turns that loss into a later tick.
                payload = await asyncio.to_thread(
                    _sc.read_messages,
                    state,
                    caller_session_key=loop.slot_key,
                    target=target,
                    limit=_judge.MAX_ROWS_PER_TARGET,
                    since=since or None,
                )
                rows = payload.get("messages") or []
                cursor = payload.get("next_since")
                return list(rows), int(cursor) if isinstance(cursor, int) else since

            async def _read_pr(target: str) -> dict | None:
                # The reading the fetcher ALREADY made this tick, never a fresh fetch:
                # re-asking the forge would spend a subprocess to learn what the
                # monitor record already holds, and the judge's job is the owner's own
                # prose criterion read against those facts.
                monitor = loop.monitor
                observed = getattr(monitor, "last_observation", None) if monitor else None
                if monitor is None or not isinstance(observed, dict):
                    return None
                if _judge.pr_target_is_unread(observed):
                    # No reading, or one whose own status says it is short. A partial
                    # reading counts as unread: a quiet drawn from the half that was
                    # read would be a quiet about the wrong half.
                    logger.debug(
                        "AutoNudge: no whole pull-request reading for loop %s -- counting "
                        "the target as unread",
                        loop.id,
                    )
                    return None
                # A loop holds ONE monitor, so this returns the same observation for
                # every subject it is asked about. The brief's targets are the owner's
                # strings and may name a DIFFERENT pull request, which would label the
                # row with that name while carrying the watched subject's state.
                if not _judge.pr_observation_is_about(
                    target,
                    monitor_kind=str(getattr(monitor, "kind", "") or ""),
                    monitor_target=str(getattr(monitor, "target", "") or ""),
                    observation=observed,
                ):
                    logger.debug(
                        "AutoNudge: a judge brief named a pull request this loop does not watch"
                    )
                    return None
                # Bodies are merged into a COPY. They live in memory for this tick
                # only, and the record the copy is taken from carries who said
                # something and when, never what -- so filling them in place is
                # exactly how review prose would reach the disk.
                stashed, stash_dropped = _judge.take_pr_bodies(loop.id)
                payload = _judge.payload_for_judge(observed, stashed, stash_dropped)
                if payload is None:
                    logger.debug(
                        "AutoNudge: loop %s lost its stashed remark bodies -- counting "
                        "the target as unread rather than judging prose the judge never got",
                        loop.id,
                    )
                    return None
                # ``last_observed_at`` is a SIBLING field of the fact object, not a key
                # inside it, so the collector cannot age the reading without being
                # handed it.
                at = getattr(monitor, "last_observed_at", 0.0)
                if isinstance(at, (int, float)) and not isinstance(at, bool) and at > 0:
                    payload["observed_at"] = float(at)
                return payload

            targets = _judge.parse_targets(_judge.spec_of(loop), loop.message)
            # Pruned to the targets this tick actually reads, not merely copied. The
            # collector only ever ADDS a key, the targets come from ``loop.message``, and
            # ``asdict`` persists whatever the map holds, so without this a retarget
            # leaves the departed target's cursor in the store for the life of the loop.
            #
            # Pruning at the write is what bounds retention in the process doing the
            # writing. The load cap is not that bound: it keeps an arbitrary 16, so it
            # can discard the cursor of a target still being read, and a lost cursor
            # replays rows the judge already screened. With the population pruned to the
            # current targets, the two bounds that disagree -- 16 cursors against 8
            # targets -- collapse into the smaller one and the cap never binds.
            wanted = set(targets)
            cursors = {t: c for t, c in loop.judge_cursors.items() if t in wanted}
            evidence, dropped = await _judge.collect_evidence(
                targets,
                read_session=_read_session,
                read_pr=_read_pr,
                cursors=cursors,
            )
            # RETURNED, not assigned onto the loop. The advanced positions are a
            # consequence of a reading that has not been judged yet, and the judge await
            # that follows is cancellable -- a user typing cancels exactly that task --
            # so publishing here moves the cursors for a verdict that never commits. The
            # next tick then reads nothing new, answers quiet, and the wake the skipped
            # row had earned is gone. The caller owns the one point where a verdict is
            # committed, so the caller publishes them.
            return evidence, dropped, cursors

        async def _emit_judge_notice(loop: NudgeLoop, line: str) -> None:
            """Write ONE ``notice`` row on the owning session for a judge verdict.

            The same surface a refused arm uses (``_surface_arm_refusal``): a row of
            its own, because that is what reaches whoever is watching the session
            without costing a turn. A quiet verdict is exactly the case that needs
            it: without a row, a loop that judged and stayed quiet looks identical
            to a loop that died.

            The row is scrubbed before it is persisted or broadcast, like every
            other transcript egress. It carries probabilities and counts, never
            evidence text: the state stays in the request, and the transcript gets
            the verdict.

            Notice rows are NOT evidence. The session collector admits assistant
            rows only, so a judge can never read its own previous notice back as
            new evidence about the session it is watching.
            """
            state = self.dashboard_state
            if state is None:
                return
            # ``get_slot`` is the accessor; ``state.sessions`` is the SessionManager and
            # holds sessions rather than chat slots. A channel-bound loop has no slot
            # window at all, which is why this returns rather than inventing one: the
            # verdict is still on the loop record and in the decisions log.
            slot = state.get_slot(loop.slot_key)
            if slot is None:
                return
            from kiro_crew.dashboard.state import append_and_surface

            text, _ = redact_exfiltration_urls(line)
            text, _ = redact_credentials(text)
            await asyncio.to_thread(append_and_surface, state, slot, "notice", text, "msg msg-info")

        def _worker_slot_running(session_key: str) -> bool:
            """Bind the wake gate's liveness question to this gateway's slot table.

            The logic is ``ledger_wake.worker_running``, not a copy of it here: a
            closure inside this constructor is unreachable from a test, and the one
            hop the probe cannot make for itself is exactly the hop that should not
            be the untested one. This keeps only the binding, which is what a
            gateway is for.

            Imported HERE rather than at module scope, matching
            ``_monitor_owner_session_id`` below. ``ledger_wake`` reaches the
            work-ledger store, and every gateway boots whether or not any conductor
            has ever opened a ledger, so a module-level import would put an optional
            subsystem on the startup path of every install to serve a callable that
            only runs once a work-ledger watch ticks.
            """
            from kiro_crew import ledger_wake

            return ledger_wake.worker_running(self.dashboard_state, session_key)

        def _worker_slot_closed(session_key: str) -> bool:
            """Bind the wake gate's "is this worker gone" question to the slot table.

            A second binding rather than a second return value from the one above,
            because the two answers are read at different strengths: a running worker
            suppresses a stall wake, a closed one causes it without waiting out the
            window. The logic is ``ledger_wake.worker_closed``, for the reason its
            sibling gives -- a closure in this constructor is unreachable from a test.
            """
            from kiro_crew import ledger_wake

            return ledger_wake.worker_closed(self.dashboard_state, session_key)

        self.autonudge_svc = AutoNudgeService(
            base_dir=data_home(),
            on_fire=_fire,
            on_monitor_tick=_monitor_tick,
            collect_judge_evidence=_collect_judge_evidence,
            emit_judge_notice=_emit_judge_notice,
            worker_running=_worker_slot_running,
            worker_closed=_worker_slot_closed,
        )

        def _monitor_owner_session_id(loop: NudgeLoop) -> str:
            """The crew log unit the loop's owner slot is serving on, or ``""``.

            The same resolver the session ledger uses: an exact registry read plus
            an attribute read, no disk and no session state mutated by asking. A
            slot with no live session answers ``""`` and the controller records
            nothing for it -- a probe runs without a model turn and must not start
            a session to file its result.
            """
            if self.dashboard_state is None:
                return ""
            from kiro_crew.crew_log.resolve import unit_for_session_key

            return unit_for_session_key(self.dashboard_state.sessions, loop.slot_key)

        controller = MonitorController(
            self.autonudge_svc,
            _fire_monitor,
            owner_session_id=_monitor_owner_session_id,
        )
        # Timers can complete while start() awaits store repair. Install the
        # observer first so that transition cannot fall between startup and
        # terminal replay.
        self.autonudge_svc.subscribe(_observer)
        await self.autonudge_svc.start()
        # A persisted terminal transition and its notification are separate
        # durable steps. Replay any notice not proven delivered; marking only
        # after delivery gives this boundary at-least-once crash semantics.
        for loop in self.autonudge_svc.list_all():
            if (
                is_structured_monitor_loop(loop)
                and loop.monitor is not None
                and not loop.active
                and loop.monitor.outcome
                in {
                    MonitorOutcome.SUCCESS,
                    MonitorOutcome.BLOCKED,
                    MonitorOutcome.BUDGET,
                    MonitorOutcome.TARGET_UNAVAILABLE,
                }
            ):
                terminal_key = (loop.id, loop.monitor.outcome, loop.monitor.stopped_at)
                if terminal_key not in notified_monitor_terminals:
                    notified_monitor_terminals.add(terminal_key)
                    if not terminal_notification_delivery_matches(
                        loop,
                        loop.monitor.outcome,
                        loop.monitor.stopped_at,
                    ):
                        _schedule_terminal_notification(loop, terminal_key)

    def _notify_nudge_expired(self, loop: NudgeLoop) -> bool:
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
            return False
        try:
            key = loop.slot_key
            # Channel-bound loops get NO synthesized meta: _notif_meta's generic
            # ``chan:ts`` split would read the NAMESPACE as the channel id,
            # producing a dead link (and a Slack URL for a Discord loop).
            # Dashboard loops bind on the BARE slot key, so re-qualify those to
            # get a working jump-to-source slot link.
            meta = None if is_channel_key(key) else self._notif_meta(f"dashboard:{key}")
            if is_structured_monitor_loop(loop):
                monitor = loop.monitor
                assert monitor is not None
                outcome = monitor.outcome
                reason = monitor.stopped_reason
                if outcome is MonitorOutcome.SUCCESS and reason == "pull_request_merged":
                    title = "Pull request monitor finished — pull request merged"
                    body = "The pull request was merged. No action needed for this watch."
                elif outcome is MonitorOutcome.SUCCESS:
                    title = "Pull request monitor finished — review readiness reached"
                    body = "The pull request is ready for review."
                elif outcome is MonitorOutcome.BUDGET:
                    title = "Pull request monitor spent its budget"
                    body = (
                        "The monitor stopped at its configured budget "
                        "before the pull request became review-ready. "
                        "Start a new watch with a larger budget to keep monitoring."
                    )
                elif outcome is MonitorOutcome.TARGET_UNAVAILABLE:
                    title = "Pull request monitor could not deliver an action"
                    body = (
                        "This monitor's conversation could not accept work. "
                        "Start a new watch from an active conversation."
                    )
                elif reason == "pull_request_closed":
                    title = "Pull request monitor stopped — pull request closed unmerged"
                    body = (
                        "The pull request was closed without merging. Decide whether "
                        "to reopen it or abandon this watch. Restart the monitor if you reopen it."
                    )
                elif reason == MONITOR_STOP_VERDICT_STALL:
                    # NOT the terminal-blocker map below, whose copy sends the reader to
                    # repair something. But it must not claim the opposite either: the
                    # DOMINANT way a watch reaches twelve identical ticks is an
                    # already-alerted red sitting inside its re-alert interval, so
                    # "nothing is wrong" would be false exactly when it matters. Name the
                    # verdict it kept reaching and let the reader judge it. The minutes
                    # are derived so the sentence cannot outlive the floor.
                    last_seen = monitor.last_observation_reason_code or "no change"
                    title = "Pull request monitor stopped — its verdict stopped moving"
                    body = (
                        f"The monitor reached the same conclusion {DEFAULT_MONITOR_STALL_TICKS} "
                        f"checks in a row, over at least {DEFAULT_MONITOR_STALL_MIN_SECS // 60} "
                        f"minutes. Last seen: {last_seen}. It stopped because nothing was "
                        "moving, not because of a new problem -- act on that verdict yourself, "
                        "or start a new watch if you expect it to change."
                    )
                else:
                    title = "Pull request monitor stopped on a terminal blocker"
                    body = {
                        "provider_authentication": (
                            "The provider rejected the monitor's credentials. "
                            "Restore its credentials before restarting."
                        ),
                        "provider_authorization": (
                            "The monitor no longer has permission to read this pull request. "
                            "Restore provider access before restarting."
                        ),
                        "provider_setup": (
                            "The monitor's provider setup is unavailable. "
                            "Repair the provider configuration before restarting."
                        ),
                        MONITOR_STOP_APPROVAL_STALL: (
                            "The monitor could not get tool approval. "
                            "Restore approval access before restarting."
                        ),
                        MONITOR_STOP_COMPLETION_UNAVAILABLE: (
                            "The monitor could not confirm completion of its last action. "
                            "Check the conversation before restarting to avoid repeating work."
                        ),
                        MONITOR_STOP_SESSION_UNAVAILABLE: (
                            "This monitor's conversation is unavailable. "
                            "Start a new watch from an active conversation."
                        ),
                        MONITOR_STOP_UNSUPPORTED_VERSION: (
                            "This watch was created by a newer Kiro Crew version. "
                            "Update Kiro Crew to inspect or restart it."
                        ),
                        MONITOR_STOP_INVALID_RECORD: (
                            "The saved monitor record is invalid. "
                            "Inspect its details before starting a new watch."
                        ),
                    }.get(
                        reason,
                        "The monitor stopped before review readiness. Open its details "
                        "in the dashboard and resolve the reported problem before restarting.",
                    )
                body = f"{body}\n\n{monitor.target}"
                body, _ = redact_exfiltration_urls(body)
                body, _ = redact_credentials(body)
                self.dashboard_state.notify(
                    "agent", title, body, meta=meta, channel=MONITOR_CHANNEL
                )
                return True
            capped_out = loop.max_cycles and loop.cycle_count >= loop.max_cycles
            # Every branch below except the terminal one explains why the loop stopped
            # SHORT of its goal. A terminal subject is not short of anything, so it
            # outranks all of them -- expressed ONCE here rather than as a guard added to
            # each branch after a reviewer finds it, which is how the cap and then the
            # wall-clock budget each came to preempt it in turn.
            #
            # An OWED terminal turn is terminal news too, and it is the third way this
            # same precedence has been lost. A CHANNEL-bound loop deliberately does not
            # settle on observation -- it learns its watch finished from a delivered turn,
            # so the probe records the owed turn in ``terminal_pending`` and leaves the
            # loop active with no ``outcome`` and no ``MONITOR_TERMINAL_REASON``. If that
            # final turn is refused (a busy thread, the ordinary case) and the retry finds
            # a bound spent, ``_timer`` deactivates on the bound before the settlement
            # that would have promoted the debt ever runs. Reading ``stopped_reason``
            # alone then contradicts a fact already durably on disk, and announces a watch
            # that SUCCEEDED with the same signal as one that ran out of cycles.
            #
            # Scope: the debt is consulted for the WORDING only. The bound that actually
            # stopped the loop keeps its own ``stopped_reason`` untouched -- so the spent
            # cap stays observable, and every consumer of that literal (notably the
            # monitor_update revival affordance, which revives a ``cycle_cap`` loop when
            # the cap is raised) sees that literal unchanged.
            owed = ""
            if loop.monitor:
                owed = str(getattr(loop.monitor, "terminal_pending", "") or "")
            terminal = loop.stopped_reason == MONITOR_TERMINAL_REASON or bool(owed)
            if not terminal and not capped_out and runtime_budget_exceeded(loop):
                title = "Monitoring loop spent its time budget"
                body = (
                    f"The loop stopped after {loop.cycle_count} cycles because "
                    f"its {loop.max_runtime_secs}s wall-clock budget ran out "
                    "without it reporting done, so its goal may still be "
                    "unmet. Restart it from the goal popover, or ask the agent "
                    "to raise the budget (monitor_update)."
                )
            elif not terminal and not capped_out and loop.stopped_reason == APPROVAL_STALL_REASON:
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
            elif terminal:
                settled = getattr(loop.monitor, "outcome", None) if loop.monitor else None
                decided = getattr(settled, "value", settled) or owed
                if decided == "success":
                    title = "Monitoring loop finished — what it was watching is done"
                    body = (
                        f"The loop stopped after {loop.cycle_count} cycles because "
                        "the pull request it was watching was merged, so there is "
                        "nothing left to observe. No action needed; arm a new loop "
                        "if you want to watch something else."
                    )
                elif decided == "blocked":
                    title = "Monitoring loop stopped — its subject was closed unmerged"
                    body = (
                        f"The loop stopped after {loop.cycle_count} cycles because "
                        "the pull request it was watching was closed WITHOUT being "
                        "merged. Nothing is left to observe, but the work is not "
                        "finished: decide whether to reopen it or abandon it, then "
                        "arm a new loop if you reopen."
                    )
                else:
                    title = "Monitoring loop finished — it reported done"
                    body = (
                        f"The loop stopped after {loop.cycle_count} cycles because its "
                        "goal reported completion. Open the session for the final details."
                    )
            else:
                title = "Monitoring loop hit its cycle cap"
                body = (
                    f"The loop stopped after {loop.cycle_count} of "
                    f"{loop.max_cycles} cycles without reporting done, so its "
                    "goal may still be unmet. Reopen the goal popover to raise "
                    "the cap or restart it."
                )
            self.dashboard_state.notify("agent", title, body, meta=meta, channel=MONITOR_CHANNEL)
            return True
        except Exception:
            logger.debug("AutoNudge expiry notification failed", exc_info=True)
            return False

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
        the queued announce points at.

        So the ids are handed to the slot keyed on the announce ITSELF,
        ``_delivery_queued`` tells the run loop to skip its own ``mark_delivered``,
        and the drain (``chat_runner._start_next_queued_turn``) settles them once a
        turn has consumed that announce -- including a retry, because a failure
        before the model consumed the prompt re-queues the same text under a newly
        minted queue id, which a debt keyed on the original id could never match. A
        wave digest carries its held members' snapshots too:
        ``_digest_settle_deliveries`` is
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
        owed: list[SubagentDelivery] = (
            []
            if (flush_only or info.outcome != "completed")
            else [SubagentDelivery(info.id, info.elapsed, info.credits)]
        )
        held = getattr(info, "_digest_settle_deliveries", None)
        if isinstance(held, list):
            owed.extend(held)
        try:
            slot.note_pending_subagent_delivery(announce, owed)
        except Exception:
            logger.debug(
                "Subagent %s: could not defer queued delivery marks", info.id, exc_info=True
            )
            return
        info._delivery_queued = True
        if isinstance(held, list):
            info._digest_settle_deliveries = []

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
        - ``slack:<chan>``     → new message in that Slack channel (later parts of a
          split report thread under the first one)
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

        # ── slack:<channel>[:<thread_ts>] → thread reply, else a new channel post ──
        if deliver.startswith("slack:"):
            parts = deliver.split(":", 2)
            try:
                # A channel id with no thread ts is a valid target: post there as a
                # new message. ``parts[1]`` must be non-empty though -- a truncated
                # tag ("slack:") would otherwise post to channel "", and the
                # resulting SlackApiError is swallowed by the except below, so the
                # report would go nowhere. That case keeps the owner-DM leg.
                # The ``slack:`` prefix guarantees a colon, so ``parts[1]`` exists.
                chan = parts[1]
                ts: str | None = parts[2] if len(parts) == 3 else None
                # The deliver tag is agent-writable: HEARTBEAT.md is not fenced by
                # ``security.paths._CREW_SECRET_LEAVES`` and prompt.md tells the
                # agent to append entries, so ``chan`` is untrusted input. An
                # unattended post reaches a NEW audience, which is what
                # ``api_send_message``'s tracked-channel allowlist governs.
                allowed = bool(chan and CHANNEL_ID_RE.match(chan) and is_tracked_channel(chan))
                owner_dm: str | None = None
                if self.slack and not allowed and self._owner_id:
                    # Resolved only off the allowed path, where it serves twice: as
                    # the fallback target, and to admit a ``slack:D...:<ts>``
                    # origin-thread reply -- the owner's own DM channel is never a
                    # tracked channel, so it cannot clear the check above.
                    owner_dm = await self.slack.open_dm(self._owner_id)
                    if chan and owner_dm and chan == owner_dm:
                        allowed = True
                # EVERY denied routing decision is audited, including the one with
                # an EMPTY channel (`deliver="slack:"`, which the agent can write).
                # Gating the log on `chan` meant that exact input produced a denial
                # with no SEL record at all -- the one shape that leaves no trace is
                # the one an agent can author, so it is the last that should be
                # exempt. `channel=` reads as "none supplied" in the record.
                if self.slack and not allowed:
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_channel_deliver",
                        outcome="denied",
                        source="gateway",
                        resources=f"channel={chan},thread_ts={ts or ''}",
                    )
                    logger.warning(
                        "Heartbeat deliver target %r is not a tracked channel — "
                        "falling back to the owner DM",
                        chan,
                    )
                if self.slack and allowed:
                    # An unattended post to an operator-named channel is egress to
                    # a non-owner audience, so it lands on the audit trail like the
                    # sibling dashboard-deliver branches.
                    sel().log_api_access(
                        caller="heartbeat",
                        operation="heartbeat_channel_deliver",
                        outcome="approved",
                        source="gateway",
                        resources=f"channel={chan},thread_ts={ts or ''}",
                    )
                    for post in _heartbeat_slack_parts(title, result_text):
                        # A split report threads under its own first part rather
                        # than posting N top-level messages into the channel.
                        posted_ts = await self.slack.post_message(chan, post, ts)
                        ts = ts or posted_ts
                elif self.slack and owner_dm:
                    for post in _heartbeat_slack_parts(title, result_text):
                        await self.slack.post_message(owner_dm, post)
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

    # gateway_runtime/mcp_broker.py
    _init_mcp_discovery = _mcp_broker._init_mcp_discovery

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
        from kiro_crew.recovery.ladder import configure_default_ladder

        # The process recovery ladder is snapshotted from config HERE because
        # this step is unconditional and runs before the task runner, the
        # dashboard and any chat slot — i.e. before any layer can decide a
        # retry, and outside the event-loop failure branch that would otherwise
        # be the first to want a delay. `agent.recovery_backoff_*` are
        # restart=True, so one snapshot is the whole contract.
        configure_default_ladder(self._cfg)

        # Per-slot WS events route by EXACT slot-key match in the frontend —
        # `subagent_event_slot` maps a parent session key to the tab that
        # displays it (cron-born tabs are `cron-<id>`, channel-born tabs their
        # transcript stem), falling back to the legacy prefix-strip when no
        # tab is open. A raw `removeprefix("dashboard:")` here left the
        # Subagents panel permanently empty for cron/channel-born sessions.
        _event_slot = subagent_event_slot

        async def _broadcast_subagent_status(
            info: SubagentInfo,
            event: str,
            selected_slot_name: str = "",
        ) -> None:
            """Broadcast subagent status change via WS for per-slot tracking."""
            if not self.dashboard_state:
                return
            try:
                slot = selected_slot_name or _event_slot(info.parent_session_key)
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
            if stage_boundary_for(slot).recovery_retrigger_count >= _max_retrigger:
                logger.warning(
                    "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                    _max_retrigger,
                    parent_key,
                    len(slot._pending_subagent_failures),
                )
                slot._pending_subagent_failures.clear()
                return
            stage_boundary_for(slot).recovery_retrigger_count += 1
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
                bounded_chat_turn(
                    _run_chat(
                        self.dashboard_state,
                        slot,
                        msg,
                        _directive_user_origin=False,
                        # The text is the re-injected sub-agent completion or
                        # failure this recovery drains, so the sub-agent is what
                        # caused the turn.
                        _turn_actor="subagent",
                    )
                ),
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
                        # Same ownership question as the AcpProcessDied arm below:
                        # the provider being dead is a PROCESS fact, and on a
                        # shared runtime the process that died was carrying the
                        # parent and its co-tenants too. Resetting the parent for
                        # a death it did not cause takes their sessions with it.
                        if runtime_death.caused_by_this_session(client):
                            try:
                                assert self.sessions is not None
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after busy exhaustion",
                                    parent_key,
                                    exc_info=True,
                                )
                        else:
                            logger.warning(
                                "Subagent %s: the SHARED runtime died — leaving parent %s "
                                "to re-acquire on its own next turn",
                                info.id,
                                parent_key,
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
                        # The parent is reset only when the death was the
                        # PARENT's runtime ending. A sub-agent runs on its
                        # parent's process, so this handler also catches the
                        # child's own death -- and resetting the parent for that
                        # tears down a healthy conversation, plus every other
                        # tenant's session with it, over a child that failed.
                        # A single-tenant runtime is reset exactly as before.
                        if runtime_death.caused_by_this_session(client):
                            try:
                                assert self.sessions is not None
                                await self.sessions.reset(parent_key)
                            except Exception:
                                logger.debug(
                                    "Failed to reset %s after process death",
                                    parent_key,
                                    exc_info=True,
                                )
                        else:
                            logger.warning(
                                "Subagent %s: the SHARED runtime died — leaving parent %s "
                                "to re-acquire on its own next turn",
                                info.id,
                                parent_key,
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
            parent_key = info.parent_session_key
            _parent_slot_name = dashboard_slot_key(parent_key)
            _boundary_owner = stage_boundary_owner_for_run(info)

            def _boundary_completion_cancelled() -> bool:
                return getattr(info, "_stage_boundary_cancelled", False) is True

            _injection_slot = None
            if self.dashboard_state and _parent_slot_name:
                from kiro_crew.dashboard.handlers.messaging import (
                    _stage_boundary_slot_for_parent,
                )

                _injection_slot = _stage_boundary_slot_for_parent(
                    self.dashboard_state,
                    parent_key,
                    boundary_owner=_boundary_owner,
                )
                if _injection_slot is None and not _boundary_owner:
                    _injection_slot = self.dashboard_state.get_slot(_parent_slot_name)
            _completion_key = getattr(_injection_slot, "key", "")
            _injection_slot_name = (
                _completion_key
                if isinstance(_completion_key, str) and _completion_key
                else _event_slot(parent_key)
            )

            if not _flush_only:
                await _broadcast_subagent_status(info, "done", _injection_slot_name)
                # Wake anything waiting on this parent's wave (the autopilot
                # stage loop) BEFORE the injection below, which can take
                # minutes: ``info.done`` is already True by here — the terminal
                # report sets it ahead of this announce — so the waiter's own
                # re-read of the running set sees the same state a poll would
                # have, just without the wait. Pulsing after the injection would
                # reintroduce exactly the latency this removes.
                if self.subagent_mgr:
                    self.subagent_mgr.signal_completion(info.parent_session_key)
            # Three-way outcome: a user stop is neutral — neither a success nor
            # a failure. The record contract keeps ``error`` unset for stops, so
            # every consumer below must branch on ``user_stopped`` explicitly
            # rather than inferring success from an empty error.
            if info.user_stopped:
                # The stop's own origin when the record carries one (a
                # parent-end verb, a stage cancel), so the announce does not
                # credit the user with a stop they never pressed.
                status, emoji, single_outcome = (
                    getattr(info, "_stop_origin", "") or "stopped by user",
                    "⏹",
                    OUTCOME_STOPPED,
                )
            elif info.error:
                status, emoji, single_outcome = "failed", "❌", OUTCOME_FAILED
            else:
                status, emoji, single_outcome = "completed", "✅", OUTCOME_OK
            title = f"Subagent `{info.id}` {emoji}"

            # Chat mode: inline info.result (subagent.py already trimmed it to
            # agent.completion_keep + completion_keep_chars) when it fits. When the
            # completion copy dropped content (result_truncated), emit a summary +
            # result_path pointer so the parent reads the full
            # transcript on demand (read / grep / spawn_status) instead of re-running
            # the subagent.
            result_path = info.result_path or ""
            if info.user_stopped:
                _partial = info.result or ""
                # Same origin as the status line above: a parent end or a stage
                # cancel must not read as the user's own Stop in the digest text.
                _origin = getattr(info, "_stop_origin", "") or "stopped by user"
                _who = (
                    "Stopped by the user"
                    if _origin == "stopped by user"
                    else f"Stopped ({_origin})"
                )
                detail = (
                    f"{_who} before completing. Do NOT treat this as "
                    "a finished result or retry it unprompted."
                    + (f"\n\nPartial output:\n{_partial}" if _partial else "")
                )
            elif info.error:
                detail = f"Error: {info.error}"
                # A run that ended on a stall / cancel / transport death keeps
                # what it streamed as a flagged PARTIAL (``info.partial``, set by
                # the stop-reason classifier in subagent_manager/run.py); deliver
                # it so the parent continues from it instead of re-submitting the
                # whole task.
                if info.partial and info.result:
                    detail += (
                        "\n\nPartial output (the run did NOT finish — do not treat "
                        f"this as a completed result):\n{info.result}"
                    )
            elif result_path and info.result_truncated:
                detail = summarize_result(info.result, result_path)
            else:
                detail = info.result or "_No response._"
            detail, _ = redact_exfiltration_urls(detail)
            detail, _ = redact_credentials(detail)
            task_text, _ = redact_exfiltration_urls(info.task)
            task_text, _ = redact_credentials(task_text)
            task_text = task_text[:100]
            usage = format_subagent_usage(info.credits, info.elapsed)
            body = f"{task_text}\n\nUsage: {usage}\n\n{detail}"
            title, _ = redact_exfiltration_urls(title)
            title, _ = redact_credentials(title)

            announce = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}`"
                f"{f' ({info.agent})' if info.agent else ''}"
                f" {status} {emoji}\n"
                f"Task: {task_text}\n\n"
                f"Usage: {usage}\n\n"
                f"{detail}"
            )
            # Structured header facts for the dashboard card, stamped on the row
            # so a reword of the prose above cannot silently break rendering.
            # The card reads this; the frontend regexes are a fallback.
            # Reassigned for the wave-digest shapes below when this member's
            # completion is folded into a batch chunk instead of injected alone.
            sub_meta = single_completion_meta(
                agent_id=info.id,
                outcome=single_outcome,
                agent_name=info.agent or "",
                task=task_text,
                requested_model=info.requested_model or info.model or "",
                resolved_model=info.resolved_model or "",
            )

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
                            "held_ok_deliveries": [],
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
                _oc = info.outcome
                if _oc == "stopped":
                    bp["stopped"] += 1
                elif _oc == "failed":
                    bp["err"] += 1
                else:
                    bp["ok"] += 1
                # Per-member model provenance in the PARENT-READ digest text: the
                # announce body the parent LLM consumes is built from
                # ok_lines/fail_lines, so surface each member's served
                # model inline there — rather than in a structured meta field
                # with no consumer.
                #
                # Print the SERVED model id only (no "(requested …)" qualifier):
                # `_res_model != _req_model` is NOT how the card decides a
                # downgrade — `isModelDowngrade` folds auto/default to "no pin"
                # and treats alias-vs-canonical / routing-prefix pairs as the
                # same model, so a raw inequality would print a false downgrade
                # on every member of a normal wave (default agent.model is
                # "auto"). Until this uses the same fold (or the registry
                # fold), show only the served id, and show nothing when there is
                # no served model — matching what the card renders in that case.
                # The value is caller-influenceable (spawn_run.model), so redact
                # it through the display context before it enters the digest
                # text broadcast to the dashboard/channels: credential-shaped
                # input must not reach metadata.
                _res_model = info.resolved_model or ""
                if _res_model:
                    _res_model, _ = redact_for_display(_res_model, redact_via_context)
                _model_tag = f" · model {_res_model}" if _res_model else ""
                # Exception-first digest content: failures/stops carry detail,
                # successes are one pointer line (full output stays on disk).
                if _oc == "completed":
                    bp["ok_lines"].append(
                        f"— `{info.id}` ✅ {task_text[:80]}{_model_tag} · {usage}"
                        + (f"\n  → {result_path}" if result_path else "")
                    )
                else:
                    bp["fail_lines"].append(
                        f"— `{info.id}` {status} {emoji} · {task_text[:80]}{_model_tag}\n"
                        f"  Usage: {usage}\n"
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
                            and not await _subagent_batch_pending(self.subagent_mgr, _batch_id)
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
                                    "slot": _injection_slot_name,
                                    "total": bp["total"],
                                    "ok": bp["ok"],
                                    "err": bp["err"],
                                    "stopped": bp["stopped"],
                                },
                            )
                    except Exception:
                        logger.debug("batch_finished broadcast failed", exc_info=True)
            if _boundary_completion_cancelled():
                logger.info(
                    "Subagent %s completion discarded after stage authority revocation",
                    info.id,
                )
                return
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
                    # incremental signal — one straggler does not withhold
                    # every sibling's result for its entire runtime.
                    #
                    # The count trigger alone cannot deliver that incremental
                    # signal for a wave smaller than the chunk size: _pending can
                    # never reach it, so wave close is the only flush.
                    # _flush_only is the LATENCY trigger the count
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
                            bp["held_ok_deliveries"].append(
                                SubagentDelivery(info.id, info.elapsed, info.credits)
                            )
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
                    # would re-open the restart-loss window.
                    # Stash the ids on the flushing member: the run loop
                    # settles them only after _on_done (which includes the
                    # routing below) returns without raising.
                    info._digest_settle_deliveries = list(bp.get("held_ok_deliveries", []))
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
                        #
                        # The tally counts this wave's DIRECT members only.
                        # ``wave_has_live_nested_spawns`` reports whether a
                        # member of this wave has itself spawned work that is
                        # still running (its own independent batch, so not in
                        # this total). When it is, the completion wording is
                        # scoped to the direct members and states that their
                        # nested work reports on its own; the unconditional
                        # "This run is complete / All results delivered" claim
                        # is reserved for a wave with no live nested work. The
                        # check is read-only and never withholds the digest.
                        try:
                            _nested_live = bool(
                                self.subagent_mgr
                                and self.subagent_mgr.wave_has_live_nested_spawns(_batch_id)
                            )
                        except Exception:
                            _nested_live = False
                        if _nested_live:
                            _completion_line = (
                                f"These {bp['total']} sub-agents finished: "
                                f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                                f"{bp['stopped']} ⏹. Their results are below. "
                                f"NOTE: a sub-agent in this wave spawned further "
                                f"work that is still running; that nested work "
                                f"is tracked as its own wave and reports "
                                f"separately when it finishes — this digest does "
                                f"NOT cover it.\n"
                                f"Finish processing these results before "
                                f"spawning any follow-up sub-agents.\n"
                            )
                        else:
                            _completion_line = (
                                f"wave finished: "
                                f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                                f"{bp['stopped']} ⏹ of {bp['total']} agents. "
                                f"All results delivered.\n"
                                f"This run is complete. Finish processing all "
                                f"results before spawning any follow-up "
                                f"sub-agents.\n"
                            )
                        announce = (
                            f"{SUBAGENT_BATCH_COMPLETION_PREFIX}\n"
                            f"Batch results {_chunk_k}/{_chunk_j} — "
                            f"{_completion_line}"
                            f"{_footer}\n\n{_digest_body}"
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
                        # for.
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
                            f"{_footer}\n\n{_digest_body}"
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
                        bp["held_ok_deliveries"] = []

            # ── Route completion back to the originating session ──
            # Tab open        → that tab (a channel-born tab mirrors on to its channel)
            # Channel, no tab → channel thread + dashboard notification
            # Cron/no parent  → dashboard notification only

            _slot_name = _injection_slot_name
            if _parent_slot_name and self.dashboard_state:
                # Route the result through _run_chat for full streaming, tool
                # call visibility, and proper lifecycle. A channel-born tab
                # runs on the channel's own session, so the turn's mirror
                # carries the reply back to the thread — the raw-injection path
                # below is for parents with no tab to stream into.
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
                        if getattr(_injection_slot, "_in_stage_execution", False) is True:
                            # The Python stage controller owns this boundary. It
                            # waits for terminal reports, then drains completion
                            # entries in order before capturing or advancing.
                            # Launching here would race that capture; waiting on
                            # slot.task can wait on the controller itself.
                            _injection_slot.queue_append(
                                announce,
                                kind=SUBAGENT_COMPLETION_KIND,
                                meta=stage_boundary_for(_injection_slot).tag_meta(
                                    {SUBAGENT_COMPLETION_META_KEY: sub_meta},
                                    owner=stage_boundary_owner_for_run(info),
                                ),
                            )
                            self._defer_queued_delivery(
                                _injection_slot,
                                announce,
                                info,
                                flush_only=_flush_only,
                            )
                            self.dashboard_state.push_slots_update()
                            logger.info(
                                "Subagent %s → queued for Autopilot stage in %s",
                                info.id,
                                _slot_name,
                            )
                            return
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

                            if _boundary_completion_cancelled():
                                logger.info(
                                    "Subagent %s completion discarded after stage "
                                    "authority revocation",
                                    info.id,
                                )
                                return
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
                                # (chat_turn_timeout_secs, 14400s default):
                                # _run_chat's finally block drains slot._queue
                                # on any exit path.
                                # Carry the structured completion facts so the
                                # drained row is a card without re-parsing the
                                # prose; _start_next_queued_turn reads them.
                                _injection_slot.queue_append(
                                    announce,
                                    kind=SUBAGENT_COMPLETION_KIND,
                                    meta=stage_boundary_for(_injection_slot).tag_meta(
                                        {SUBAGENT_COMPLETION_META_KEY: sub_meta},
                                        owner=stage_boundary_owner_for_run(info),
                                    ),
                                )
                                # Queuing is not delivery. The announce promises
                                # result paths the parent can read on demand, but
                                # it will not be in the parent's context until a
                                # turn drains it — a wait bounded only by the turn
                                # ceiling, so longer than the retention TTL. Owe
                                # the delivery tombstones to that drain instead of
                                # writing them now, or the reaper prunes
                                # result.txt while the promise is still queued
                                # and the parent is handed dead paths.
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
                        #
                        # This branch hands the digest off ASYNCHRONOUSLY: the
                        # turn is a task, and `_on_done` returns to
                        # `_report_terminal` while it is still pending — so a
                        # bare return here is a local routing success, not
                        # evidence the parent received anything. Owe
                        # the delivery bookkeeping to the turn's CONSUMPTION
                        # instead, through the same `_defer_queued_delivery`
                        # the queue branch uses: it records the debt (the
                        # completed member's own tombstone AND any held wave
                        # siblings) in the slot's content-keyed ledger, keyed
                        # on this announce, and flags `_delivery_queued` — so
                        # the run loop's `mark_delivered` and its digest-hold
                        # settle both become no-ops for this route, and the
                        # two settle paths cannot both fire.
                        #
                        # The task's own OUTCOME is deliberately not the
                        # signal: `_run_chat` returns NORMALLY on a signed-out
                        # CLI, a dead provider, exhausted retries and a first
                        # empty response — several of them after re-queueing
                        # the announce itself — so "the task finished cleanly"
                        # says nothing about delivery. Consumption does, and a
                        # failure before it re-queues the announce, whose drain
                        # claims this same content-keyed debt on the replay. An
                        # unconfirmed hand-off leaves the debt parked on
                        # purpose: a duplicate announce after a restart is
                        # visible to the parent and recoverable, a lost result
                        # is neither.
                        #
                        # Computed BEFORE the transfer (which detaches the held
                        # ids); stays False when there is nothing to owe — a
                        # failed or stopped solo member settles through its own
                        # failure tombstone, not this ledger.
                        _owes_delivery = bool(info._digest_settle_deliveries) or (
                            not _flush_only and info.outcome == "completed"
                        )
                        self._defer_queued_delivery(
                            _injection_slot, announce, info, flush_only=_flush_only
                        )
                        _consumed: list[bool] = [False]

                        def _note_consumed(consumed: bool = True) -> None:
                            # False is a retraction: the first empty response
                            # re-queues this exact announce verbatim, so the
                            # delivery that counts has not happened yet.
                            _consumed[0] = consumed

                        _run_kwargs: dict[str, Any] = {}
                        if _owes_delivery:
                            _run_kwargs["_on_consumed"] = _note_consumed
                        _task = asyncio.create_task(
                            bounded_chat_turn(
                                _run_chat(
                                    self.dashboard_state,
                                    _injection_slot,
                                    announce,
                                    _directive_user_origin=False,
                                    # Structural provenance for the session
                                    # crew log: the queued twin above carries
                                    # SUBAGENT_COMPLETION_KIND, and this branch
                                    # is the same injector dispatching directly.
                                    _turn_actor="subagent",
                                    **_run_kwargs,
                                )
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
                        if _owes_delivery:
                            # Settle the owed tombstones only once the model has
                            # consumed this turn's prompt — the drain's own
                            # settlement path, reused verbatim. If the transfer
                            # above fell back (stubbed slot), the ledger holds
                            # no debt and the claim inside is an empty no-op.
                            _arm_queued_delivery_settlement(
                                self.dashboard_state,
                                _injection_slot,
                                _task,
                                [announce],
                                _consumed,
                            )
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
                        _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
                        client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                        _acquired = True
                        _footer_client = client
                        _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                        if self.ctx_builder:
                            # The completion is injected into the PARENT's
                            # conversation, so it reads the parent session's silo,
                            # resolved from that session's recorded binding. The
                            # child's own store is not the answer here: this turn
                            # continues the parent, and the reply it produces is
                            # consolidated into the parent's store.
                            msg, _ = await run_in_embed_pool(
                                self.ctx_builder.build_message,
                                announce,
                                is_new,
                                parent_key,
                                memory_store=_memory_store,
                                provider_type=_provider,
                                context_provider=client,
                                resumed=_resumed,
                            )
                        else:
                            msg = announce
                        if _boundary_completion_cancelled():
                            logger.info(
                                "Subagent %s completion discarded after stage "
                                "authority revocation",
                                info.id,
                            )
                            return
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
                    _memory_store = await session_store_for_turn(self.ctx_builder, parent_key)
                    client, is_new, _resumed = await self.sessions.get_or_create(parent_key)
                    acquired = True
                    _provider = self._cfg.agent.provider if hasattr(self, "_cfg") else "acp"
                    if self.ctx_builder:
                        # Same rule as the interactive injection above: the turn
                        # continues the PARENT conversation, so it reads the parent
                        # session's silo from that session's recorded binding.
                        msg, _ = await run_in_embed_pool(
                            self.ctx_builder.build_message,
                            announce,
                            is_new,
                            parent_key,
                            memory_store=_memory_store,
                            provider_type=_provider,
                            context_provider=client,
                            resumed=_resumed,
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
                    # also deliver the synthesized response to the job's own
                    # surfaces. honor the parent cron job's silent flag too:
                    # info.silent is never set from the cron's silent setting for
                    # spawn_run sub-agents, so a silent cron would otherwise still
                    # post every subagent-completion turn.
                    try:
                        await self._deliver_cron_response(
                            parent_key,
                            cron_response,
                            silent=info.silent or self._cron_job_is_silent(parent_key),
                        )
                    except Exception:
                        logger.exception(
                            "Subagent %s: failed to deliver cron response",
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
                    or await _subagent_queued_count(self.subagent_mgr, parent_key) > 0
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
                        # second colon-separated segment in both cases. Exact-key
                        # tracking means an older deferred reset removes only its
                        # own key even after a newer run registers another one,
                        # without stripping the reaper/ownership fence for either.
                        cron_svc = getattr(self, "cron_svc", None)
                        if cron_svc is not None:
                            parts = parent_key.split(":", 2)
                            if len(parts) >= 2:
                                cron_svc.clear_active_session_key(parts[1], parent_key)
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
        # A SECOND instance of the same callback, differing only in the
        # unreachable behaviour. It is a separate closure because the one above is
        # also wired as ``on_tool_approval``: a mid-run tool prompt has no
        # terminal path that could report the refusal, so raising there would turn
        # a recoverable park into a lost turn.
        _approve_spawn_gate = self._interactive_approval(
            "subagent", slot_resolver=_spawn_slot_resolver, raise_when_unreachable=True
        )

        async def _spawn_approve(
            request_id: str, description: str, parent_session_key: str = ""
        ) -> bool:
            # Channel-side delivery FIRST. A spawn parented on
            # a live channel conversation (Telegram, …) is best answered where the
            # human already is, with that channel's own Approve/Deny/Trust
            # keyboard. The seam returns True/False when the channel surfaced the
            # prompt and got a press; None means no channel hook owns this session,
            # or the hook could not surface it here — either way, fall through to
            # the unchanged Slack-DM/dashboard gate below (which still raises
            # SpawnApprovalUnreachable when no surface is attached).
            channel_decision = await deliver_spawn_approval(
                request_id, description, parent_session_key
            )
            if channel_decision is not None:
                return channel_decision
            event = LLMEvent(kind="permission_request", request_id=request_id, title=description)
            return await _approve_spawn_gate(event, parent_session_key)

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
                    task_preview = redact_and_truncate(info.task or "", 100)
                    error_text, _ = redact_exfiltration_urls(extra.get("error", "timed out"))
                    error_text, _ = redact_credentials(error_text)
                    # The visible transcript card must state the run's real
                    # outcome, same as the queued LLM copy: this event fires for
                    # every terminal state whose report could not be injected,
                    # not only successful completions.
                    outcome_line = _injection_notice_outcome(info)
                    usage = format_subagent_usage(info.credits, info.elapsed)
                    slot.append(
                        "assistant",
                        f"{SUBAGENT_COMPLETION_PREFIX}\n"
                        f"Agent `{info.id}` ❌\n"
                        f"Task: {task_preview}\n\n"
                        f"Usage: {usage}\n\n"
                        f"Error: {error_text}\n"
                        f"⚠️ Result delivery failed — {outcome_line}",
                        "msg msg-a",
                        meta={
                            SUBAGENT_COMPLETION_META_KEY: single_completion_meta(
                                agent_id=info.id,
                                outcome=OUTCOME_FAILED,
                                agent_name=info.agent or "",
                                task=task_preview,
                                requested_model=info.requested_model or info.model or "",
                                resolved_model=info.resolved_model or "",
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

            ``meta`` carries the structured completion facts so the
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

        def _report_failure_boundary(parent: str, owner: str) -> object | None:
            if self.dashboard_state is None:
                return None
            from kiro_crew.dashboard.handlers.messaging import (
                _stage_boundary_slot_for_parent,
            )

            slot = _stage_boundary_slot_for_parent(
                self.dashboard_state,
                parent,
                boundary_owner=owner,
            )
            return stage_boundary_for(slot) if slot is not None else None

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
            # Rows that survived the restart are claimed by the manager's pump,
            # which the reaper start below kicks as soon as the loop yields --
            # that yield is ``run()``'s memory barrier. Hold the pump until
            # ``_start_subagent_dispatch_after_memory_ready`` opens it.
            defer_queue_dispatch=True,
            stage_boundary_for_scope=_report_failure_boundary,
        )
        # A parent that ends takes its children with it, on every backend. The
        # session lifecycle owns the boundary and drives both halves at each of its
        # parent-end paths, so no surface that closes, resets, discards or retires a
        # conversation needs a cancel call of its own. The manager is passed whole
        # rather than as two bound methods because the halves have to agree about
        # which runs they are talking about.
        self.sessions.set_child_teardown_handler(self.subagent_mgr)
        self.subagent_mgr.start_reaper()

    # gateway_runtime/admission.py
    _start_subagent_dispatch_after_memory_ready = (
        _admission._start_subagent_dispatch_after_memory_ready
    )

    _register_child_liveness = _admission._register_child_liveness

    _start_adaptive_controller = _admission._start_adaptive_controller

    _wire_overload_health = _admission._wire_overload_health

    _subagent_dependency_coordinator = _admission._subagent_dependency_coordinator

    _ensure_subagent_coordinator = _admission._ensure_subagent_coordinator

    _unwire_overload_health = _admission._unwire_overload_health

    _wire_runner_admission = _admission._wire_runner_admission

    _subscribe_runner_admission = staticmethod(_admission._subscribe_runner_admission)

    _attach_runner_admission_consumers = _admission._attach_runner_admission_consumers

    _runner_admission_store_ready = _admission._runner_admission_store_ready

    _unwire_runner_admission = _admission._unwire_runner_admission

    _start_dashboard_workers_after_memory_ready = (
        _admission._start_dashboard_workers_after_memory_ready
    )

    def _init_task_runner(self) -> None:
        """Initialize the task runner."""

        async def _task_notify(
            title: str, body: str, task_id: str = "", *, session_key: str = ""
        ) -> None:
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
                    safe_t = redact_credentials(redact_exfiltration_urls(title)[0])[0]
                    safe_b = redact_credentials(redact_exfiltration_urls(body)[0])[0]
                    notice = f"*{safe_t}*\n{safe_b}"
                    # Governed channel ladder first, owner DM as the fallback —
                    # the same ordering the cron delivery path uses, and for the
                    # same reason: a task blocked on an approval nobody was told
                    # about is indistinguishable from a hung one. The owner DM
                    # reaches Slack only, so a Telegram-only operator learned
                    # nothing and the run simply stalled. ``session_key`` is the
                    # ORIGINATING conversation, threaded down from
                    # ``start_background``; it is empty for a dashboard- or
                    # CLI-started run, and ``_deliver_channel_reply`` also
                    # returns False for a Slack, dashboard or unrecognized key,
                    # so the DM below is reached in every case that has no
                    # channel behind it.
                    if session_key and await self._deliver_channel_reply(session_key, notice):
                        return
                    if self.slack and self._owner_id:
                        ch = await self.slack.open_dm(self._owner_id)
                        if ch:
                            await self.slack.post_message(ch, notice)
                except Exception as exc:
                    # Not "to Slack DM" any more: the try now spans the channel
                    # ladder as well, so naming one surface would misdirect
                    # whoever reads this line.
                    logger.warning("Failed to send task approval notification: %s", exc)

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
            defer_channel_agent_resume=True,
            schedule_memory_preparation=self._schedule_memory_preparation,
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
            context_builder=self.ctx_builder,
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
            schedule_memory_preparation=self._schedule_memory_preparation,
        )
        if dashboard_port == 0 and self._dashboard_runner is not None:
            addresses = self._dashboard_runner.addresses
            if addresses:
                self._dashboard_port = addresses[0][1]
        if self.dashboard_state:
            self.dashboard_state.no_crons = self._no_crons  # API-only mode

    # gateway_runtime/memory_lifecycle.py
    _initialize_memory_worker = _memory_lifecycle._initialize_memory_worker

    _stop_memory_startup = _memory_lifecycle._stop_memory_startup

    _start_memory_after_ready = _memory_lifecycle._start_memory_after_ready

    _schedule_memory_preparation = _memory_lifecycle._schedule_memory_preparation

    _wait_for_memory_preparation = _memory_lifecycle._wait_for_memory_preparation

    _repair_member_memory_once = _memory_lifecycle._repair_member_memory_once

    _repair_member_memory = _memory_lifecycle._repair_member_memory

    _start_embeddings = _memory_lifecycle._start_embeddings

    _auto_migrate_memory = _memory_lifecycle._auto_migrate_memory

    _set_memory_migrated = _memory_lifecycle._set_memory_migrated

    # ------------------------------------------------------------------
    # MCP Gateway
    # ------------------------------------------------------------------

    # gateway_runtime/mcp_broker.py
    _schedule_mcp_launch_approval_persist = _mcp_broker._schedule_mcp_launch_approval_persist

    _init_mcp_gateway = _mcp_broker._init_mcp_gateway

    #: Floor on the gap between timed pre-resolve passes. ``refresh_hours = 0``
    #: legitimately means "always stale" for the freshness check, but it must not
    #: turn the timer into a spin loop that reinstalls continuously.
    _MCP_RESOLVE_MIN_SLEEP_SECS = 300.0

    # gateway_runtime/mcp_broker.py
    _mcp_resolve_refresh_secs = _mcp_broker._mcp_resolve_refresh_secs

    _mcp_resolve_prefetch_loop = _mcp_broker._mcp_resolve_prefetch_loop

    _prefetch_mcp_resolutions = _mcp_broker._prefetch_mcp_resolutions

    _refresh_mcp_resolutions = _mcp_broker._refresh_mcp_resolutions

    _stop_mcp_broker = _mcp_broker._stop_mcp_broker

    _apply_mcp_gateway_enabled = _mcp_broker._apply_mcp_gateway_enabled

    _apply_mcp_stub = _mcp_broker._apply_mcp_stub

    _wire_mcp_gateway_dashboard = _mcp_broker._wire_mcp_gateway_dashboard

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    async def _shutdown(self) -> None:
        """Graceful cleanup of all services."""
        self._memory_repair_stop.set()
        if self._memory_repair_task is not None:
            self._memory_repair_task.cancel()
            await asyncio.gather(self._memory_repair_task, return_exceptions=True)
        if self._memory_startup is not None:
            self._memory_startup.stop()
        # Stop the boot-time inbound-spool notice pass before the transports it
        # sends through are closed. Nothing is lost by cancelling: an entry is
        # removed from disk only AFTER its notice is confirmed, so an entry cut
        # off mid-send is noticed again on the next start (at most one duplicate
        # line). Awaited with a small budget so a slow platform send cannot spend
        # the GRACEFUL_SHUTDOWN_SECS that saves active chat slots.
        replay = self._inbound_replay_task
        if replay is not None and not replay.done():
            replay.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(replay, timeout=1.0)
        # Stop polling the central policy source, so a fetch in flight cannot
        # install a ceiling into a context the rest of this teardown is dismantling.
        # The join budget is deliberately small: the thread waits on an Event, so
        # setting it wakes an idling refresher immediately and the join costs
        # nothing, while a refresher mid-fetch must not spend the shutdown budget
        # that saves active chat slots (this whole method runs under
        # GRACEFUL_SHUTDOWN_SECS). It is a daemon thread, so anything still running
        # after that dies at exit anyway.
        with contextlib.suppress(Exception):
            from kiro_crew.platform.policy_distribution import stop_refresher

            # ``stop_refresher`` JOINS a thread, which is a blocking call and must
            # not run on the event loop. Offloaded with its own deadline so a
            # refresher mid-fetch cannot eat the GRACEFUL_SHUTDOWN_SECS budget that
            # saves active chat slots; it is a daemon thread, so whatever is still
            # running after that dies at exit anyway.
            await asyncio.wait_for(asyncio.to_thread(stop_refresher, 0.5), timeout=1.5)

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

        # The general _background_tasks set is retention, not lifecycle ownership.
        # This task can own a dep_sync child plus pip/build descendants, so cancel
        # and await it explicitly while _check_console_script still has a live loop
        # on which to kill the process tree and perform its bounded reap.
        repair_task = self._console_script_repair_task
        if repair_task is not None and not repair_task.done():
            repair_task.cancel()
            await asyncio.gather(repair_task, return_exceptions=True)

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
        if self._adaptive_controller is not None:
            # Reached only past a live controller, so the module is resident
            # already: this import is a dict lookup, never the load a
            # module-scope spelling puts on the boot path.
            from kiro_crew.adaptive import controller as adaptive_controller

            adaptive_controller.register(None)
            self._unwire_overload_health()
            cleanup_tasks.append(self._adaptive_controller.stop())
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
        # A reconnect the config applier scheduled must not race the close below
        # by starting a fresh transport into a gateway that is going away.
        for restart in list(self._channel_restart_tasks):
            restart.cancel()
        cleanup_tasks.extend(registry.shutdown_tasks(self._channel_handles, timeout=2.0))
        # Cancel background model download if still in flight
        if self._model_download_task is not None and not self._model_download_task.done():
            self._model_download_task.cancel()
        # Cancel the feature-video transfer if still in flight
        if self._feature_video_task is not None and not self._feature_video_task.done():
            self._feature_video_task.cancel()
        # Cancel background auto-migration if still in flight
        if self._auto_migrate_task is not None and not self._auto_migrate_task.done():
            self._auto_migrate_task.cancel()
        # Cancel the recurring update coordinator if it is still in flight — a
        # check or installer subprocess can take ~70s to time out.
        if self._update_check_task is not None and not self._update_check_task.done():
            self._update_check_task.cancel()
        from kiro_crew.dashboard.handlers.updates import _cancel_update_check

        cleanup_tasks.append(_cancel_update_check())

        if cleanup_tasks:
            await asyncio.gather(*cleanup_tasks, return_exceptions=True)

        # AFTER the gather, not beside cancel_all() above: cancel_all() is what stops
        # the runs that still write to the durable task queue, so closing the store
        # before it finishes would pull the connection out from under them. Off-loop,
        # because ``close()`` is synchronous and waits for the store's writer lock --
        # on the loop that stalls shutdown behind an in-flight executor write.
        if self.subagent_mgr:
            await asyncio.to_thread(self.subagent_mgr.close)

        await asyncio.to_thread(self._stop_memory_startup)

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    async def _run_update_checks(self) -> None:
        """Run the full check-and-apply coordinator now and every interval."""
        from kiro_crew.dashboard.handlers import _UPDATE_CHECK_INTERVAL

        while True:
            self._update_apply_deferred = False
            try:
                if getattr(self, "_pending_update_respawn", None) is not None:
                    await self._retry_pending_update_restart()
                else:
                    await self._check_for_updates()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed cycle must not permanently kill automatic updates.
                logger.exception("Automatic update coordinator failed")
            delay = (
                self._UPDATE_BUSY_RETRY_SECS
                if self._update_apply_deferred
                else _UPDATE_CHECK_INTERVAL
            )
            await asyncio.sleep(delay)

    async def _retry_pending_update_restart(self) -> None:
        """Retry only the safe restart after an update already applied."""
        respawn = getattr(self, "_pending_update_respawn", None)
        if respawn is None:
            return
        if not await self._prepare_auto_update_apply(mandatory=False):
            return
        try:
            await self._restart_after_update(respawn)
        finally:
            await self._finish_auto_update_apply()

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

    async def _auto_update_effect(self, provider: object | None) -> AutoUpdateEffect:
        """What an available update leads to here: the one answer every apply path acts on.

        Recorded for the status surface too, so the dashboard's auto-update
        switch reads the same derivation this loop branches on. ``notify`` never
        reaches ``_prepare_auto_update_apply``: admission is not paused for an
        update this install will not apply.
        """
        effect = await asyncio.to_thread(auto_update_effect, provider=provider)
        record_auto_update_effect(effect)
        return effect

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

        # Loaded BEFORE provider.apply(): the legacy-nested-venv migration the
        # resolver exists for deletes the venv this process imports from, so a
        # deferred import after a successful apply would raise
        # ModuleNotFoundError and leave a closed-session gateway un-restarted
        # (found in review). The module stays off the boot path either way —
        # this method runs from the update timer, not from gateway start.
        from kiro_crew.platform.wheel_engine import respawn_executable

        assert isinstance(provider, UpdateProvider)

        # Say who owns updates on every path, "already latest" included, so the
        # dashboard need not run its own check to learn it.
        _update_info["managed_by"] = MANAGED_BY_COMMAND
        effect = await self._auto_update_effect(provider)
        result = await provider.check()

        # The mandatory floor is an enterprise ceiling and is evaluated FIRST,
        # before any check-error early return: a host below min_version must
        # still be updated even when the provider's check could not complete
        # (a timed-out or misconfigured command must not strand the host below
        # the policy floor).
        mandatory_required = update_required(_running_version)
        if not mandatory_required:
            self._mandatory_update_deferred_at = None
            self._mandatory_update_deferred_key = None
        if mandatory_required:
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
            if effect.effect != AUTO_EFFECT_MANDATORY:
                # The floor mandates it but nothing here can apply it: tell the
                # operator instead of pausing admission for an apply that fails.
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s, "
                    "but %s — notifying",
                    _running_version,
                    min_version(),
                    effect.reason,
                )
                self._publish_provider_update_state(result)
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
                return
            if not await self._prepare_auto_update_apply(
                mandatory=True,
                mandatory_key=f"provider:{min_version()}:{result.remote_version or ''}",
            ):
                self._publish_provider_update_state(result)
                return
            try:
                logger.warning(
                    "Version compliance: running %s is below the policy minimum %s — "
                    "applying mandatory update via provider (overrides auto_update)",
                    _running_version,
                    min_version(),
                )
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "pulling", "Applying mandatory update…"
                    )
                success = await provider.apply()
                if success:
                    await self._restart_after_update(respawn_executable)
                elif self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed", "Update apply failed — run manually: kirocrew update"
                    )
            finally:
                await self._finish_auto_update_apply()
            return

        # Below the mandatory floor: a check error is a non-answer, not a
        # verdict — report it and stop rather than treating it as "up to date".
        if result.error:
            logger.info("Update check did not complete (%s)", result.error)
            return

        if result.available:
            cfg = await asyncio.to_thread(KiroCrewConfig.load)
            if cfg.auto_update and effect.effect == AUTO_EFFECT_INSTALL:
                if not await self._prepare_auto_update_apply(mandatory=False):
                    self._publish_provider_update_state(result)
                    return
                try:
                    logger.info("Auto-update enabled — applying update via provider")
                    if self.dashboard_state:
                        self.dashboard_state.push_update_progress("pulling", "Downloading update…")
                    success = await provider.apply()
                    if success:
                        await self._restart_after_update(respawn_executable)
                    elif self.dashboard_state:
                        self.dashboard_state.push_update_progress(
                            "failed", "Update apply failed — run manually: kirocrew update"
                        )
                finally:
                    await self._finish_auto_update_apply()
            else:
                if cfg.auto_update:
                    logger.info("Auto-update is on, but %s — notifying instead", effect.reason)
                self._publish_provider_update_state(result)
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
        else:
            print("👻 Already on latest version")

    async def _restart_after_update(self, respawn: Callable[[], str]) -> None:
        """Save state and restart only after callback/refusal work is durable.

        ``respawn`` is loaded before apply can replace the environment. If the
        pre-fence drain cannot finish, retain it and retry the restart in five
        minutes with admission reopened; never force through accepted work.

        The interpreter is established BEFORE any of that. When it is missing, this
        returns without saving, fencing or draining, because an exec that cannot
        succeed must not be reached after every session has been closed.
        """
        logger.info("Update applied, preparing a callback-safe gateway restart")
        self._pending_update_respawn = respawn
        launcher = await asyncio.to_thread(resolve_restart_launcher)
        exe = await asyncio.to_thread(respawn) if launcher is None else None
        # Off-loop for the same reason as the two resolvers above: both predicates
        # are metadata syscalls against a pathname this process does not control,
        # and an interpreter on a stalled network mount would freeze every gateway
        # task -- including the heartbeat -- rather than one restart.
        usable = launcher is not None
        if not usable and exe:
            usable = await asyncio.to_thread(platform_compat.execv_target_available, exe)
        if not usable:
            # No usable interpreter: the apply pruned the tree this process was
            # running from. RETURN BEFORE saving, fencing or draining. Reaching
            # the exec with no interpreter closes every session first and then
            # raises ENOENT. Admission itself does come back --
            # ``_finish_auto_update_apply`` resumes it -- but the sessions
            # ``close_all()`` tore down do not, and ``_pending_update_respawn``
            # is cleared just before the exec, so nothing retries: what survives
            # is a gateway with no sessions, running a different version from the
            # install on disk. Deferring here keeps the sessions instead, and
            # ``_pending_update_respawn`` stays set so
            # ``_retry_pending_update_restart`` finishes the update once an
            # operator repairs the install.
            self._update_apply_deferred = True
            logger.error(
                "Update applied but restart deferred: no usable interpreter. "
                "Restore the interpreter and this retries itself -- the retry "
                "re-resolves it, so no configuration change is needed."
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "restarting",
                    "Update applied — restart needs a usable interpreter",
                )
            return
        if self.dashboard_state:
            self.dashboard_state.push_update_progress("restarting", "Preparing safe restart…")
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
        # Same reason as the dashboard restart path: os.execv does not drain the
        # safety-override writer. No await is permitted between final drain and exec.
        try:
            await asyncio.to_thread(flush_breadcrumb_writes, 2.0)
        except Exception:
            logger.debug("Breadcrumb flush before update restart failed", exc_info=True)

        if not await self._drain_update_callback_work(timeout=self._UPDATE_DRAIN_TIMEOUT_SECS):
            self._update_apply_deferred = True
            logger.warning(
                "Update applied but restart deferred: callback/refusal work did not drain"
            )
            if self.dashboard_state:
                self.dashboard_state.push_update_progress(
                    "restarting", "Update ready — restart deferred until messages are safe"
                )
            return

        sessions = self.sessions
        if sessions is not None:
            # Yield-free with the successful drain above: a callback is now
            # either already counted, or it observes this fence and writes its
            # refusal synchronously before returning. That closes the final
            # snapshot-to-exec window without holding ordinary retries closed.
            if not sessions.fence_update_restart():
                self._update_apply_deferred = True
                logger.warning("Update restart fence lost to real shutdown; deferring restart")
                return
            await sessions.close_all()

        # The broker this gateway spawned dies with it, the same as ``_shutdown``
        # does on a clean exit; an adopted daemon belongs to its own owner and is
        # left alone, as ``GatewayManager.shutdown`` already does. The exec below
        # skips that shutdown, and the successor can only replace a survivor whose
        # owner pid is its own (an exec that kept the pid) or gone. Through a
        # launcher that runs the new gateway as a child, this pid lives on as its
        # supervisor: the daemon's owner-liveness check keeps passing, and the
        # successor refuses a broker "owned by another live gateway" for its whole
        # lifetime. Sessions are closed, so nothing is mid-call. The stop never
        # raises and is bounded: the daemon's own drain budget on SIGTERM, then a
        # SIGKILL and a reap of its pooled backends if the drain does not finish.
        await self._stop_mcp_broker()

        # Any callback that ran while close_all awaited used the synchronous
        # fenced refusal path. Drain pre-fence workers and registered handlers
        # one final time; never cancel or force through them.
        await self._drain_update_callback_work(timeout=None)
        logger.info("Update callback drain complete, restarting gateway")
        self._pending_update_respawn = None
        # The exec is past the point of no return: the guard above removed the
        # reachable failures, but only the kernel can refuse the image itself
        # (wrong architecture, truncated, replaced since the check). Returning
        # from here is what strands the gateway, so hand that outcome to the
        # exec seam's own fatal partner instead of unwinding into the update
        # coordinator, which logs and loops with every session already closed.
        try:
            if launcher is not None:
                platform_compat.reexec_launcher(launcher, sys.argv[1:])
            else:
                platform_compat.reexec_python_module("kiro_crew", sys.argv[1:], executable=exe)
        except OSError:
            await platform_compat.exit_after_failed_restart_exec(launcher or exe)

    async def _check_for_updates_legacy(self) -> None:
        """Legacy update check — the existing layout-aware logic."""
        try:
            from kiro_crew import __version__ as _running_version
            from kiro_crew.dashboard.handlers import _do_update_check, _update_info

            # Before the check: it reads no check result, and the status frame
            # should not wait out a slow fetch to learn it.
            effect = await self._auto_update_effect(None)
            await _do_update_check()
            # Snapshot: the branches below read several keys with awaits
            # between them, and a dashboard-triggered check running
            # concurrently replaces the cache wholesale.
            info = dict(_update_info)
            if info.get("managed_by") == MANAGED_BY_COMMAND and effect.route is not None:
                # The check resolved a policy provider that was not configured
                # when the effect was derived, which a live policy refresh can
                # do between the two. A provider OWNS the update, so no
                # built-in route may apply it: the next cycle resolves the
                # provider up front and routes there.
                effect = AutoUpdateEffect(
                    AUTO_EFFECT_NOTIFY,
                    None,
                    "a policy update command now owns this install",
                )
            from kiro_crew.platform.update_governance import min_version, update_required

            mandatory_target_key = (
                f"legacy:{min_version()}:{info.get('channel') or ''}:"
                f"{info.get('latest_version') or ''}:{info.get('managed_by') or ''}"
            )

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
            mandatory_required = update_required(_running_version)
            if not mandatory_required:
                self._mandatory_update_deferred_at = None
                self._mandatory_update_deferred_key = None
            if mandatory_required:
                # A mandatory floor is handled by the route `auto_update_effect`
                # chose, because "apply" means different things per install shape:
                #   * git checkout -> git fetch + reset applies.
                #   * managed venv -> the installer can apply it, so a floor does
                #     drive it; a floor above the newest build notifies instead of
                #     reinstalling the same bytes forever.
                #   * externally managed (dmg/appimage/deb/rpm/nsis/docker: no
                #     `can_apply` and no command) -> its own updater owns this; the
                #     backend must not drive a git reset on a non-git tree nor show
                #     an inapplicable CLI-update badge.
                if effect.effect == AUTO_EFFECT_MANDATORY and effect.route == AUTO_ROUTE_GIT:
                    if not await self._prepare_auto_update_apply(
                        mandatory=True,
                        mandatory_key=mandatory_target_key,
                    ):
                        return
                    try:
                        logger.warning(
                            "Version compliance: running %s is below the policy minimum %s — "
                            "applying a mandatory update (overrides auto_update)",
                            _running_version,
                            min_version(),
                        )
                        await self._auto_apply_update()
                    finally:
                        await self._finish_auto_update_apply()
                    return
                # A managed-venv install cannot apply in-process, but its
                # installer can, and a policy floor outranks auto_update. Runtime
                # ownership is authoritative: older managed wheels have no build
                # stamp, while a foreign source or wheel must never be rewritten.
                if (
                    effect.effect == AUTO_EFFECT_MANDATORY
                    and effect.route == AUTO_ROUTE_WHEEL
                    # The installer command is composed by the CHECK, so it is
                    # not part of the static derivation: without it the apply
                    # would pause admission and then warn-skip.
                    and _remediation_command(info)
                ):
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
                    if not await self._prepare_auto_update_apply(
                        mandatory=True,
                        mandatory_key=mandatory_target_key,
                    ):
                        return
                    try:
                        logger.warning(
                            "Version compliance: running %s is below the policy minimum %s — "
                            "applying mandatory update via installer (overrides auto_update)",
                            _running_version,
                            min_version(),
                        )
                        await self._auto_apply_wheel_update()
                    finally:
                        await self._finish_auto_update_apply()
                    return
                if effect.blocked:
                    # The source pin refuses every update path, `kirocrew update`
                    # included, so there is nothing to point the operator at.
                    logger.warning(
                        "Version compliance: running %s is below the policy minimum %s, "
                        "but %s — not applying",
                        _running_version,
                        min_version(),
                        effect.reason,
                    )
                    return
                # Everything below cannot apply here, so the operator has to act.
                # Two of the three cases light the badge; the third deliberately
                # does not, because a dmg/appimage/deb/rpm/nsis/docker install cannot
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
                        "but this install (%s) cannot apply it unattended (%s) — "
                        "run `kirocrew update`",
                        _running_version,
                        min_version(),
                        info.get("managed_by") or "unknown",
                        effect.reason,
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

                cfg = await asyncio.to_thread(KiroCrewConfig.load)
                # `_auto_apply_update` replaces code with git fetch + reset, so it
                # serves only the git route `auto_update_effect` chose. A managed
                # venv replaces itself by re-running the installer, which the
                # branch below drives instead.
                #
                # `version_newer` is the other half, and it is not redundant:
                # `update_available` is true on commit distance alone, which for a
                # source checkout means any upstream commit — acting on that would
                # `git reset --hard` a developer's tree within 12 hours of one,
                # where before it only happened at a release. Commit distance
                # without a version bump lights the badge below instead, and the
                # dashboard's own apply path (`git pull`, dirty tree refused) is
                # the non-destructive way in.
                installs = cfg.auto_update and effect.effect == AUTO_EFFECT_INSTALL
                if installs and effect.route == AUTO_ROUTE_GIT and info.get("version_newer"):
                    if not await self._prepare_auto_update_apply(mandatory=False):
                        return
                    try:
                        logger.info("Auto-update enabled — applying update")
                        await self._auto_apply_update()
                    finally:
                        await self._finish_auto_update_apply()
                elif installs and effect.route == AUTO_ROUTE_WHEEL and _remediation_command(info):
                    if not await self._prepare_auto_update_apply(mandatory=False):
                        return
                    try:
                        # Only the managed venv can be safely self-updated by
                        # re-running cli.sh: it replaces the environment serving this
                        # process. Other source and wheel installs notify instead.
                        logger.info("Auto-update enabled for managed install — running installer")
                        await self._auto_apply_wheel_update()
                    finally:
                        await self._finish_auto_update_apply()
                else:
                    if cfg.auto_update and not installs:
                        logger.warning(
                            "Auto-update is on, but this install (%s) cannot apply it "
                            "unattended (%s) — notifying instead",
                            info.get("managed_by") or "unknown",
                            effect.reason,
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
        # Timeout/cancel discipline for every spawn below. Function-local like
        # the wheel path's import further down this file: the helper owns the
        # kill-the-tree + bounded-reap contract, and there is exactly one
        # implementation of it.
        from kiro_crew.platform.update_provider import _kill_and_reap

        # Loaded before the reinstall below for the same reason as the provider
        # path: `pip install -e .` rewrites the package this process imports
        # from, so the resolver must already be in memory when the restart
        # needs it. Called only after the install succeeded (see the exec below).
        from kiro_crew.platform.wheel_engine import respawn_executable

        try:
            # Every git call below reads a tree an agent can write, and several of
            # them (`status`, `diff`, `reset`) will EXEC a program the repository
            # names in its own config. Bound once here, ahead of the first spawn,
            # so the whole sequence is covered and a later-added command cannot
            # quietly opt out of it.
            #
            # A redirected work tree is handled separately, by the
            # `repo_exec_config_reason` refusal below: git ignores a
            # `core.worktree` supplied through the environment, so it cannot be
            # pinned here.
            #
            # `git_command_env` BUILDS the environment rather than merging over
            # `os.environ`, because an inherited `GIT_DIR` has to be ABSENT and a
            # merge can only add keys. Left in place it would point every call
            # below at unrelated metadata while `cwd` still says `proj`.
            _git_env = git_command_env()

            # `git` itself is resolved OFF `PATH`. A gateway's `PATH` can lead
            # with an agent-writable directory (a worktree venv's `bin`,
            # `~/.local/bin`), so a bare `"git"` lets a planted shim run — and on
            # THIS path what git reports decides which code is installed and
            # re-executed, so the shim would not merely lie, it would choose the
            # payload. `AGENTS.md` already requires this for system tools;
            # `cli_doctor` already did it for git.
            #
            # Resolved ONCE here rather than per call, so every step below runs
            # the same binary: re-resolving per spawn would leave a window for the
            # answer to change mid-sequence.
            _git = platform_compat.trusted_git_bin()
            if _git is None:
                logger.warning(
                    "Auto-update: skipping — no trustworthy `git` outside PATH. "
                    "Run `kirocrew update` to apply this manually."
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # Detect current branch
            branch_proc = await asyncio.create_subprocess_exec(
                _git,
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                # Own process group (POSIX; no-op on Windows) so a timeout or
                # cancellation kill reaches the whole tree, not just the direct
                # child. Every spawn in this method carries the same discipline:
                # on TimeoutError/CancelledError, kill the tree
                # and reap under a bound via the shared `_kill_and_reap`, then
                # re-raise so the outer handler keeps its current behaviour —
                # without this the child is ABANDONED on timeout, not stopped.
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                branch_out, _ = await asyncio.wait_for(branch_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(branch_proc)
                raise
            if branch_proc.returncode != 0:
                logger.error("Auto-update: could not determine current branch")
                return
            branch = branch_out.strip().decode() if branch_out else ""

            # Only a PRIMARY branch is auto-updated: a feature or beta branch
            # needs a deliberate `kirocrew update`, and a detached HEAD has no
            # branch to fast-forward at all.
            #
            # This gate read `branch != "mainline"` — inherited verbatim from the
            # internal repo whose primary line is named that — so on this repo,
            # whose primary line is `main`, it matched nothing and returned at
            # `logger.debug`. Every git checkout (the documented `install.sh`
            # path) therefore never auto-updated, and said so nowhere.
            #
            # `is_primary_branch` reads a reviewed allowlist and nothing else, so
            # no local git ref can steer or veto this decision. See its docstring
            # — this is also the path a mandatory `min_version` floor drives.
            if not is_primary_branch(branch):
                logger.info(
                    "Auto-update: skipping — %s is not a primary branch",
                    branch or "detached HEAD",
                )
                return

            # A content filter or textconv driver is named BY THE REPOSITORY, so
            # there is no fixed key to pin and `_git_env` cannot reach it. Refuse
            # the unattended run rather than execute it; the operator still has
            # `kirocrew update`, where a human is deciding.
            exec_config = await asyncio.get_running_loop().run_in_executor(
                None, lambda: repo_exec_config_reason(proj)
            )
            if exec_config:
                logger.warning(
                    "Auto-update refused: %s, which git would run during the update",
                    exec_config,
                )
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
                return

            # The availability check compares HEAD against `@{u}` (the TRACKED
            # upstream) while this applies `origin/<branch>`. On a fork checkout
            # whose branch tracks `upstream` and whose `origin` is a stale fork,
            # those are different refs: the check sees the canonical remote move
            # ahead and the reset below would discard commits. Only reset when the
            # branch tracks the remote this actually fetches and pins.
            if not await asyncio.get_running_loop().run_in_executor(
                None, lambda: tracks_upstream(proj, branch)
            ):
                logger.info(
                    "Auto-update: skipping — %s does not track origin, and the "
                    "update check measures against its tracked upstream",
                    branch,
                )
                if self.dashboard_state:
                    self.dashboard_state.push_refresh("update_available")
                return

            # Source pin, checked before the fetch. This is the most privileged
            # update path in the product — no auth, no click, `git reset --hard`
            # + pip + execv on boot — so a blocked host must not touch its tree.
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
                _git,
                "fetch",
                "origin",
                branch,
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                await asyncio.wait_for(fetch.communicate(), timeout=60)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(fetch)
                raise

            if fetch.returncode != 0:
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Capture the fetched commit as an OID, immediately after the fetch,
            # and use that OID for the comparison AND the reset below. A ref name
            # is re-resolved on every command, so `origin/<branch>` could be moved
            # by a concurrent fetch between the decision and the reset — deciding
            # against one commit and resetting to another. An OID cannot move.
            #
            # The ref is spelled in FULL (`refs/remotes/origin/...`) because the
            # short form is ambiguous in the attacker's favour: rev-parse's
            # disambiguation order checks `refs/tags/<name>` BEFORE
            # `refs/remotes/<name>`, so a tag literally named `origin/main`
            # resolves instead of the remote-tracking branch — and the update's
            # own `git fetch` auto-follows tags, so publishing that tag upstream
            # is enough to create it locally. git prints "refname is ambiguous"
            # to stderr and still writes the TAG's OID to stdout, which is what
            # this capture reads, so the short form fails silently here.
            target_proc = await asyncio.create_subprocess_exec(
                _git,
                "rev-parse",
                "--verify",
                f"refs/remotes/origin/{branch}^{{commit}}",
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                target_out, _ = await asyncio.wait_for(target_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(target_proc)
                raise
            target = (target_out or b"").strip().decode()
            if target_proc.returncode != 0 or not target:
                logger.warning(
                    "Auto-update: skipping — could not resolve origin/%s to a commit",
                    branch,
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # Check if there are actually new commits
            diff_proc = await asyncio.create_subprocess_exec(
                _git,
                "diff",
                "HEAD",
                target,
                "--quiet",
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                await asyncio.wait_for(diff_proc.wait(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(diff_proc)
                raise
            if diff_proc.returncode == 0:
                # No diff — already up to date
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return

            # LAST-MOMENT REVALIDATION, after the fetch and immediately before the
            # only destructive step. Everything checked so far was checked
            # earlier: the availability verdict came from a separate pass, and
            # the config probe ran before the fetch. A checkout is a live tree —
            # a developer can commit, or repo config can be rewritten, in the
            # window between those checks and this reset. `reset --hard` is not
            # recoverable from, so the two facts that decide whether it destroys
            # anything are re-read here rather than trusted from before.
            #
            # 1. Local commits. `git status --porcelain` below reports
            #    working-tree edits, NOT commits, and the `git diff` above is
            #    satisfied by any difference in either direction — so a checkout
            #    that is ahead of origin passes both and then loses those commits.
            # Counted against `target` — the OID the reset will use — not against
            # `origin/<branch>`. A ref is re-resolved per command, so a concurrent
            # fetch could advance it, make this read zero against the new tip, and
            # leave the reset discarding commits relative to the old one.
            ahead = await asyncio.get_running_loop().run_in_executor(
                None, lambda: commits_ahead(proj, target)
            )
            if ahead != 0:
                logger.warning(
                    "Auto-update: skipping — %s is ahead of origin/%s by %s commit(s); "
                    "a reset would discard them. Run `kirocrew update` to decide.",
                    branch,
                    branch,
                    "an unknown number of" if ahead is None else ahead,
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # 2. The work tree, and the repo-named exec drivers, re-read after the
            #    fetch. The earlier probe is a check-then-use otherwise: config
            #    rewritten in between would redirect this reset, or hand the
            #    checkout's own driver to the command that performs it.
            exec_config_now = await asyncio.get_running_loop().run_in_executor(
                None, lambda: repo_exec_config_reason(proj)
            )
            if exec_config_now:
                logger.warning("Auto-update refused before reset: %s", exec_config_now)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # 3. Uncommitted tracked edits. REFUSE, like the two checks above —
            #    logging a warning and resetting anyway would make an unattended
            #    boot-time update the one code path that could silently destroy a
            #    developer's uncommitted work. `reset --hard`
            #    is not recoverable and nothing here has the standing to make
            #    that trade on the developer's behalf: the count check one screen
            #    up already refuses for COMMITTED work and defers to `kirocrew
            #    update`, and uncommitted work is the strictly more fragile case
            #    (a discarded commit is at least recoverable from the reflog; an
            #    uncommitted edit is gone). The manual path keeps the destructive
            #    semantics, because there a human chose them.
            #
            #    Untracked files are excluded: `reset --hard` preserves them, so
            #    task specs and notes are not a reason to refuse.
            status_proc = await asyncio.create_subprocess_exec(
                _git,
                "status",
                "--porcelain",
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                status_out, _ = await asyncio.wait_for(status_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(status_proc)
                raise
            if status_proc.returncode != 0:
                # Cannot prove the tree is clean, and the next step is
                # irreversible — treat an unreadable status as dirty.
                logger.warning(
                    "Auto-update: skipping — could not read the work-tree status of %s; "
                    "a reset could discard uncommitted changes. Run `kirocrew update`.",
                    loggable_path(proj),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return
            tracked = [
                ln
                for ln in (status_out or b"").decode(errors="replace").splitlines()
                if ln.strip() and not ln.startswith("??")
            ]
            if tracked:
                logger.warning(
                    "Auto-update: skipping — %s has %s uncommitted tracked change(s); "
                    "a reset would discard them. Run `kirocrew update` to decide.",
                    loggable_path(proj),
                    len(tracked),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # 3b. Tracked edits git was TOLD not to look at. `status --porcelain`
            #     above honours `assume-unchanged` / `skip-worktree` and reports a
            #     clean tree for an edited file, while `reset --hard` still
            #     overwrites it -- so check 3 alone cannot see this loss.
            hidden = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(), lambda: hidden_worktree_edits(proj)
            )
            if hidden is None or hidden:
                logger.warning(
                    "Auto-update: skipping — %s has %s tracked change(s) hidden by "
                    "assume-unchanged/skip-worktree (e.g. %s); a reset would discard "
                    "them. Run `kirocrew update` to decide.",
                    loggable_path(proj),
                    "an unknown number of" if hidden is None else len(hidden),
                    loggable_path(hidden[0]) if hidden else "unknown",
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # 4. Untracked files that the TARGET would create. `reset --hard`
            #    leaves untracked files alone ONLY while they do not collide with
            #    a path the target adds -- where it does, the local file is
            #    overwritten. `git status --porcelain` reports such a file as
            #    `??`, which check 3 deliberately skips, so this is the one
            #    data-loss case that survives a "clean" tracked tree. Verified:
            #    upstream adds `newfile.txt`, a local untracked `newfile.txt` is
            #    replaced by the upstream content.
            #
            #    Detected rather than prevented by switching to `merge --ff-only`:
            #    the reset semantics are deliberate (documented as discarding
            #    tracked edits) and this path keeps them. A collision is a refusal
            #    for the same reason as the three above -- it is unrecoverable and
            #    unattended.
            added_proc = await asyncio.create_subprocess_exec(
                _git,
                "diff",
                "--name-only",
                "--diff-filter=A",
                # Rename detection is ON by default for porcelain diffs, and it
                # DEFEATS this guard: a pure `git mv` upstream is reported as a
                # single `R` entry, which `--diff-filter=A` excludes, so the
                # destination path never appears as added. Verified — upstream
                # renaming `a.txt` to `b.txt` yields `R100` and an EMPTY added
                # list, while an untracked local `b.txt` is still overwritten by
                # the reset. `--no-renames` decomposes the rename into a delete
                # plus an add, which is what this check needs to see.
                "--no-renames",
                "-z",
                "HEAD",
                target,
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                added_out, _ = await asyncio.wait_for(added_proc.communicate(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                await _kill_and_reap(added_proc)
                raise
            if added_proc.returncode != 0:
                logger.warning(
                    "Auto-update: skipping — could not list the paths %s would add; "
                    "a reset could overwrite untracked files. Run `kirocrew update`.",
                    branch,
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return
            # `os.fsdecode`, NOT `.decode(errors="replace")`: a path byte that is
            # not valid UTF-8 becomes U+FFFD under `replace`, and the resulting
            # name does not exist on disk — so the check answers "no collision"
            # for a file it is looking straight at. Verified: a `bad\xffname.txt`
            # decodes to `bad\ufffdname.txt` (lexists False) under `replace` and
            # to `bad\udcffname.txt` (lexists True) under `fsdecode`, and the
            # reset overwrote it. `fsdecode` round-trips through surrogateescape,
            # which is what the os functions below need.
            added_names = [os.fsdecode(raw) for raw in (added_out or b"").split(b"\0") if raw]

            def _obstructions(name: str) -> bool:
                """Whether *name* collides with something already on disk.

                Two shapes, both unrecoverable and both invisible to check 3:

                * the path ITSELF exists untracked, and the reset overwrites it;
                * an ANCESTOR exists as a non-directory. When the target adds
                  `pkg/mod.py` and `pkg` is locally an untracked FILE, git must
                  replace that file with a directory — `lexists("pkg/mod.py")` is
                  False, so checking only the full path misses it. Verified: the
                  untracked `pkg` was destroyed while the full-path check passed.
                """
                full = os.path.join(proj, name)
                if os.path.lexists(full):
                    return True
                parent = os.path.dirname(name)
                while parent:
                    candidate = os.path.join(proj, parent)
                    # Link check FIRST: `isdir` follows the link, so an untracked
                    # symlink-to-directory reported "directory, not an
                    # obstruction" -- and the reset then replaced the developer's
                    # symlink with a real directory. Verified against real git.
                    #
                    # `is_link_or_junction`, not `os.path.islink`: `islink` returns
                    # False for a Windows JUNCTION, so a junction ancestor would
                    # read as a plain directory and the reset would write through
                    # it, outside the checkout. AGENTS.md names this helper as the
                    # required form for exactly this reason, and its own docstring
                    # describes this failure -- using the bare `islink` here was a
                    # rule violation, not a judgement call.
                    if platform_compat.is_link_or_junction(candidate):
                        return True
                    if os.path.lexists(candidate) and not os.path.isdir(candidate):
                        return True
                    parent = os.path.dirname(parent)
                return False

            # Offloaded: `_obstructions` walks each added path's ancestors with
            # synchronous `os.path` probes, so a large update would run an
            # unbounded stat walk ON THE EVENT LOOP and stall every chat and the
            # heartbeat (`no-blocking-call-on-event-loop`).
            collisions = await asyncio.get_running_loop().run_in_executor(
                subprocess_executor(),
                lambda: [name for name in added_names if _obstructions(name)],
            )
            if collisions:
                logger.warning(
                    "Auto-update: skipping — %s would add %s path(s) that already "
                    "exist untracked here (e.g. %s); a reset would overwrite them. "
                    "Run `kirocrew update` to decide.",
                    branch,
                    len(collisions),
                    # `loggable_path`, not the raw name: this is the one log line
                    # that carries a filename straight from git output, and a
                    # non-UTF-8 byte in it would make logging DROP the record --
                    # silently losing the evidence that the update refused here.
                    loggable_path(collisions[0]),
                )
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                    self.dashboard_state.push_refresh("update_available")
                return

            # 5. Interpreter floor of the TARGET revision, read from the fetched
            #    commit itself. pip would refuse the reinstall below on the same
            #    floor, but only after the reset has moved the tree to code this
            #    venv cannot import -- a state every boot then re-enters, because
            #    the next check sees the commit already applied and never retries.
            #    Refuse before the reset, with the remedy, and leave the tree.
            try:
                floor_breach = await asyncio.get_running_loop().run_in_executor(
                    subprocess_executor(),
                    lambda: dep_sync.incoming_python_floor_breach(
                        Path(proj), target, Path(sys.executable), git_bin=_git, env=_git_env
                    ),
                )
            except dep_sync.IncomingFloorUnreadable as exc:
                # A floor git could not read is not a floor that is absent:
                # refusing here is what keeps a git hiccup from re-admitting
                # the stranded state this whole gate exists to prevent.
                floor_breach = (
                    "could not read the incoming revision's interpreter "
                    f"requirement ({exc}); leaving the checkout where it is"
                )
            if floor_breach:
                # Redact first, cap last -- the same treatment every sibling push
                # gives text it did not author. The refusal quotes the remote's
                # `requires-python` verbatim and paths from the local install.
                floor_breach, _ = redact_exfiltration_urls(floor_breach)
                floor_breach, _ = redact_credentials(floor_breach)
                floor_breach = floor_breach[:500]
                logger.warning("Auto-update refused: %s", floor_breach)
                if self.dashboard_state:
                    self.dashboard_state.push_update_progress(
                        "failed", f"Update refused: {floor_breach}"
                    )
                    self.dashboard_state.push_refresh("update_available")
                return

            # Hard reset to remote. Reached only with a clean tracked tree and no
            # untracked collisions, so it overwrites nothing the developer owns.
            reset = await asyncio.create_subprocess_exec(
                _git,
                "reset",
                "--hard",
                target,
                cwd=proj,
                env=_git_env,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                start_new_session=platform_compat.IS_POSIX,
            )
            try:
                await asyncio.wait_for(reset.wait(), timeout=10)
            except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                # This child is a MUTATION, not a query: abandoned, it is a
                # hard reset still running against the operator's checkout.
                await _kill_and_reap(reset)
                raise
            if reset.returncode != 0:
                logger.error("Auto-update: git reset --hard failed (rc=%d)", reset.returncode)
                if self.dashboard_state:
                    self.dashboard_state.clear_update_progress()
                return
            logger.info("Auto-update: reset to origin/%s, rebuilding", branch)

            # Update the optional kiro-cli backend, by the pinned absolute path
            # `_pinned_kiro_cli` returns — never a bare argv0 this unattended
            # path would let `PATH` answer. `None` means do not spawn it,
            # skipped like any absent backend, which this step already treats as
            # non-fatal.
            kiro_cli_bin = await _pinned_kiro_cli("the optional kiro-cli backend update")
            # The desktop app's bundled copy is skipped too: it sits inside the
            # signed app bundle, where an in-place self-update would break the
            # codesign seal, and the app update is what replaces it. Checked
            # against the live environment because that is where the Electron
            # shell publishes the bundled directory.
            if kiro_cli_bin is not None and is_bundled_kiro_cli(kiro_cli_bin, os.environ):
                logger.debug(
                    "Auto-update: kiro-cli is the app's bundled copy, not updated in place"
                )
                kiro_cli_bin = None
            if kiro_cli_bin is not None:
                kiro_update: asyncio.subprocess.Process | None = None
                try:
                    kiro_update = await asyncio.create_subprocess_exec(
                        kiro_cli_bin,
                        "update",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                        # Own process group (POSIX; no-op on Windows) so the
                        # kill in the arms below reaches the whole tree.
                        start_new_session=platform_compat.IS_POSIX,
                    )
                    await asyncio.wait_for(kiro_update.wait(), timeout=120)
                except (TimeoutError, asyncio.TimeoutError):
                    # Kill the tree BEFORE falling through: this step is
                    # non-fatal, but the code below rebuilds the frontend and
                    # reinstalls the Python deps, and an abandoned
                    # `kiro-cli update` would keep mutating the installation
                    # concurrently — the same half-replaced-install race the
                    # wheel path's CancelledError branch exists to prevent.
                    if kiro_update is not None:
                        await _kill_and_reap(kiro_update)
                    logger.debug("Auto-update: kiro-cli update timed out (non-fatal)")
                except asyncio.CancelledError:
                    # Shutdown cancels the update task; without this the child
                    # keeps mutating the installation unsupervised.
                    if kiro_update is not None:
                        await _kill_and_reap(kiro_update)
                    raise
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
                    *platform_compat.isolated_python_argv(
                        "-m",
                        "pip",
                        "install",
                        "--quiet",
                        *core_deps,
                    ),
                    cwd=proj,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    # Own process group (POSIX; no-op on Windows) so the kill
                    # below reaches pip's build-backend grandchildren too.
                    start_new_session=platform_compat.IS_POSIX,
                )
                try:
                    _fb_out, fb_err = await asyncio.wait_for(fallback.communicate(), timeout=300)
                except (TimeoutError, asyncio.TimeoutError, asyncio.CancelledError):
                    await _kill_and_reap(fallback)
                    raise
                if fallback.returncode == 0:
                    logger.info(
                        "Auto-update: core deps repaired after pip failure (%s)",
                        ", ".join(core_deps),
                    )
                else:
                    logger.error(
                        "Auto-update: core dep repair also failed (rc=%d): %s",
                        fallback.returncode,
                        # Redact the whole stream (pip can echo an index URL
                        # with credentials), then keep the tail where pip
                        # prints its error.
                        redact_log_via_context(fb_err.decode(errors="replace"))[-300:],
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

            logger.info("Auto-update: rebuild complete, preparing safe restart")
            # Re-read version from rebuilt package for the operator-facing log;
            # restart ownership itself is centralized below.
            importlib.reload(kiro_crew)
            new_ver = kiro_crew.__version__
            print(f"👻 New version {new_ver} available — auto-updating and restarting…")
            await self._restart_after_update(respawn_executable)
        except Exception:
            logger.warning("Auto-update failed", exc_info=True)
            if self.dashboard_state:
                # Surface the platform-correct manual restart command so a failed
                # auto-restart doesn't leave the user guessing. Resolved OFF the
                # loop thread: the hint stats the two unit-file locations, and
                # the per-user one is under the account's home, which can be a
                # network mount — a stat against a disconnected mount blocks for
                # as long as the mount does, and on this thread that freezes
                # chat and the liveness heartbeat together with nothing in-band
                # to clear it (the watchdog's kill is the only exit). A worker
                # thread waits in its place; the loop keeps serving.
                hint = await asyncio.to_thread(restart_command_hint)
                self.dashboard_state.push_update_progress("failed", f"Restart failed — run: {hint}")

    async def _auto_apply_wheel_update(self) -> None:
        """Auto-apply a wheel/cli.sh update by re-running the signed installer.

        The installer (``cli.sh``) handles the full security chain: RSA-SHA256
        signature verification of the manifest against a pinned public key,
        SHA-256 checksum of the downloaded wheel, and channel assertion. This
        method simply invokes it as a subprocess, then restarts the gateway via
        ``os.execv`` so the new code takes effect.

        Preconditions (checked by the caller):
        * ``auto_update`` is True in config, or a policy floor mandates the update.
        * ``auto_update_effect`` chose the wheel route: a managed venv on POSIX
          with a safe, policy-permitted CDN (not a git checkout, not externally
          managed).

        Re-checked here, after the caller paused admission: the capability's
        ``remediation`` carries the installer command (the feed check succeeded
        and composed it locally from validated inputs).

        The command is composed by
        :func:`kiro_crew.platform.update_layout.wheel_update_command` from a
        validated channel name and a scheme-pinned artifact base URL
        (``--proto '=https'``), never from feed data. A successful run replaces the
        venv in-place; a failure leaves the existing install intact (cli.sh writes
        to a temp dir and atomically replaces via ``ln -sf``).
        """
        from kiro_crew.dashboard.handlers import _update_info

        # Loaded before cli.sh runs: the installer's legacy-venv migration
        # `rm -rf`s the venv this process imports from once the new tree is
        # linked, so a deferred import after it would not find the module.
        # Called only after the installer succeeded (see the exec below).
        from kiro_crew.platform.wheel_engine import respawn_executable

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
            # half, and half a token does not match the redactors' patterns
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

        logger.info("Auto-update (wheel): installer succeeded, preparing safe restart")
        await self._restart_after_update(respawn_executable)

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    # gateway_runtime/channel_lifecycle.py
    _connect_slack = _channel_lifecycle._connect_slack

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

        The clear here is ``clear_late_marker_write``, not ``clear_marker``,
        and the difference is what makes this thread safe to run at an
        arbitrary time. The shutdown-side clear holds the listener while it
        runs, so a location still identifies its owner; this thread may run
        after the listener is free and a replacement gateway has bound the
        port, where it does not. ``clear_late_marker_write`` therefore deletes
        no credential at all, so a replacement gateway's clients keep
        authenticating whatever this thread does, and it declines the marker
        files outright when the pid record names another process. Its own
        docstring states what that second scope does not reach: a late write
        of this generation rewrites the pid record, so marker files a landed
        late write produced are always removable.
        """
        try:
            run_marker.write_marker(port)
        finally:
            if self._marker_clear_pending.is_set():
                try:
                    if not run_marker.clear_late_marker_write(port):
                        logger.debug(
                            "Late run-marker self-clear declined for port %s: "
                            "the pid record names another gateway",
                            port,
                        )
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

        # Off-loop: the sweep is synchronous filesystem work and this runs
        # inside the orchestrator coroutine.
        #
        # ``narrow_with_leaders=False`` keeps boot doing exactly what it did
        # before the recycled-pid work: the leaders snapshot is a /proc read this
        # path may not carry, since no-new-work-on-gateway-boot-path names orphan
        # sweeps.
        #
        # The narrowing is asked for on the graceful-shutdown sweep only, so a
        # gateway that is hard-killed never runs it and its recycled-pid mappings
        # wait for a later clean exit. That is the accepted cost of keeping this
        # path, and the force-exit handler, doing exactly their pre-existing work.
        await asyncio.to_thread(cleanup_orphaned_sessions, narrow_with_leaders=False)

        # Same "previous run left residue" concern as the orphan sweep above, for
        # telemetry rather than processes: any open-session crumb on disk belongs
        # to a session that never reached a teardown path, so it is emitted as
        # end_reason=crashed.
        #
        # Deliberately NOT awaited here. The scan is a glob plus a read/stat/unlink
        # per crumb, so its cost scales with accumulated user data, and running it
        # inline would delay readiness in proportion to that. It goes to a worker
        # thread on a tracked task instead, and the process-start cutoff means it
        # cannot mistake a session THIS process opens in the meantime for a
        # casualty of the last one -- so it does not have to finish before the
        # gateway starts serving.
        _telemetry_backfill_cutoff = time.time()

        async def _backfill_unclean_session_telemetry() -> None:
            try:
                from kiro_crew.metrics.sessions import backfill_crashed_sessions

                count = await asyncio.to_thread(
                    backfill_crashed_sessions, _telemetry_backfill_cutoff
                )
                if count:
                    logger.info(
                        "Telemetry: back-filled %d unclean session lifetime(s) "
                        "from the previous run",
                        count,
                    )
            except Exception:  # telemetry must never break boot
                logger.debug("unclean-session telemetry backfill failed", exc_info=True)

        _backfill_task = asyncio.create_task(_backfill_unclean_session_telemetry())
        self._background_tasks.add(_backfill_task)
        _backfill_task.add_done_callback(self._background_tasks.discard)

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

        # Auto-migration starts only after deferred restore and memory init.

        # Start MCP gateway sidecar before any ACP session can spawn.  The
        # rewriter writes the agent-JSON overlay first so kiro-cli picks up
        # the broker-wired MCP entries the moment a session starts.  No-op
        # when ``mcp_gateway.enabled`` is False.
        await cautious_boot.pause_before("MCP gateway sidecar")
        await self._init_mcp_gateway()

        # Loading the cron scheduler reads and reconciles durable jobs. Under
        # cautious boot this pause keeps that work out of the app/MCP launch
        # window; overdue callbacks remain disarmed until memory is prepared.
        await cautious_boot.pause_before("cron scheduler")
        # Load and reconcile durable jobs for the dashboard, but do not arm
        # overdue callbacks while the restore/open worker still owns memory.
        await self._init_cron(arm=False)
        self._init_mcp_discovery()
        self._init_subagents()
        self._init_task_runner()
        if not self._no_dashboard:
            await self._init_dashboard()
        else:
            await self._init_api_server()
        # The dashboard/API socket is bound now. A missing wrapper can take the
        # full pip timeout to repair, so track that work without delaying READY.
        # The task itself catches and logs failures; startup remains available.
        self._schedule_console_script_repair()

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
        #
        # The dashboard/API factory synchronously published the memory task at
        # its ready=True boundary. No suspension has followed its return, so the
        # restore/open worker still cannot run before this process-ready marker.
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

        # The HTTP socket is bound, and the optional machine-readable marker
        # has been emitted. Background approval-state writes may start now.
        approval_ready = getattr(self, "_mcp_launch_approval_ready", None)
        if approval_ready is not None:
            approval_ready.set()

        self._install_shutdown_signal_handlers()

        # The run directories a PREDECESSOR gateway of this data home left behind
        # (session_work_dir). Ordered after ``cleanup_orphaned_sessions`` above,
        # which reaped this home's pid ledger -- the ledger's remaining entries
        # are what the sweep keeps -- and before any session writer below can
        # create a directory. Past KIROCREW_READY so readiness does not wait for
        # it (no-new-work-on-gateway-boot-path); off-loop, bounded by entries and
        # wall clock, fail-open, and skipped in test_mode like the hourly wake.
        # Nothing this process has marked is ever its business, so the ordering
        # is for determinism, not safety.
        if not self._test_mode:
            try:
                await asyncio.to_thread(
                    _sweep_predecessor_session_work_dirs,
                    _live_session_work_dirs(self.sessions),
                )
            except Exception:
                logger.debug("boot session work-dir sweep failed", exc_info=True)

        # TaskRunner + workflow agent calls join the durable task queue and the
        # runner lane now that both consumers exist (the WorkflowService is
        # built by the dashboard server). AFTER the READY print, not before it:
        # the coordinator's first build runs `rebuild()` over every waiting row,
        # and `test_memory_startup` pins that no such work precedes readiness.
        # Nothing dispatches in between -- the subagent pump is held closed
        # (``defer_queue_dispatch``) and the dashboard workers and cron start
        # further down, after the memory barrier.
        await self._ensure_subagent_coordinator()
        self._wire_runner_admission()

        if not await self._wait_for_memory_preparation():
            await self._shutdown_and_exit()
            return
        # The startup crewmate prune judges each sync-generated crewmate from
        # the session history it can see; every writer below (subagent pump,
        # channel agent resume, cron) can bind a crewmate to a NEW session, so
        # none may start until the pass has RETURNED. Past KIROCREW_READY, so
        # readiness does not wait. A pass that outlives its budget is told to
        # stop deleting and is still waited for; it always returns (bounded
        # locks, non-blocking opens), so this cannot hold the gateway for good.
        if self.dashboard_state is not None:
            from kiro_crew.dashboard.server import await_crewmate_prune_settled

            await await_crewmate_prune_settled(
                self.dashboard_state, before="the memory-backed session writers"
            )
        if self.subagent_mgr is not None:
            await self.subagent_mgr.wait_taskq_ready()
            # The store exists now; bind the coordinator and the adoption sweep
            # the socket-bind pass could not see. Before the dashboard workers
            # and cron start, so the sweep never races live runner work.
            await self._runner_admission_store_ready()
            # Also off-loop for the health wiring the controller start does: the
            # pass above binds no coordinator when it wired no admission.
            await self._ensure_subagent_coordinator()
            # Memory is prepared and the store is bound: the durable rows that
            # survived the restart may start now.
            await self._start_subagent_dispatch_after_memory_ready()
            self._start_adaptive_controller()
            # The manager exists and readiness is past, so the session ledger's
            # repair can be given its child-liveness probe. Before the dashboard
            # workers and cron start, so no session can open a ledger and repair
            # it while the probe is missing -- which would let the repair close a
            # child that is still running.
            self._register_child_liveness()

        # Persisted Crew work and legacy channel agents can dispatch providers
        # immediately when resumed, so start them only after the shared memory
        # barrier. The dashboard control shell existed throughout preparation.
        if not self._no_dashboard:
            self._start_dashboard_workers_after_memory_ready()

        # These services can run memory-backed work as soon as they start.
        # Arm them only after preparation; the dashboard socket remains bound
        # throughout recovery so status and owner controls stay available.
        await self._start_cron_after_memory_ready()
        await self._init_heartbeat()

        self._start_memory_after_ready()

        # ── Central governance-policy refresh ──
        # Started HERE, after readiness, not on the boot path: the
        # no-new-work-on-gateway-boot-path rule applies, and nothing about this
        # loop needs to exist before the gateway can serve. Boot has already
        # established the ceiling from the same source (the load tier does that),
        # so this only keeps it current.
        #
        # A detached daemon thread, NOT awaited, for the reason the beacon is: the
        # fetch is blocking urllib and must never sit on the event loop. It is a
        # no-op unless a policy or the environment names a source AND an interval,
        # and it waits one full interval before its first poll, so a fleet
        # restarting together does not stampede the admin's endpoint.
        #
        # This is what makes an admin's push land on a running fleet: a changed
        # document is validated through the same floor gates boot applies and then
        # installed in place. One that fails them is refused and the running
        # ceiling is kept, so a bad push cannot take down hosts already up.
        #
        # ``_test_mode`` skips it so the offline E2E gate never makes an outbound
        # request.
        if not self._test_mode:
            with contextlib.suppress(Exception):
                from kiro_crew.agent import (
                    prime_ceiling_projection,
                    reproject_for_ceiling_change,
                )
                from kiro_crew.dashboard.tailnet_serve import (
                    revoke_if_governance_now_pins_off,
                )
                from kiro_crew.platform.policy_distribution import (
                    register_post_install_hook,
                    start_refresher,
                )

                # Hooks are registered BEFORE the poller starts, so the first installed
                # ceiling already re-derives what was materialised from the previous one.
                # Most governed controls are live evaluations and need nothing here. These two
                # are the exceptions: a published tailnet origin, whose gate fires when
                # publish is CALLED and so does not retract what is already serving, and the
                # agent config's ``allowedTools``, which kiro-cli reads from the FILE — so a
                # list written under a looser ceiling keeps auto-approving what the fleet has
                # since forbidden.
                _tailnet_port = self._dashboard_port
                register_post_install_hook(lambda: revoke_if_governance_now_pins_off(_tailnet_port))
                # Seeded BEFORE the poller starts: the first poll can itself install a new
                # ceiling, and a baseline taken on the hook's first call would record that
                # generation and skip the rebuild it needed.
                prime_ceiling_projection()
                register_post_install_hook(reproject_for_ceiling_change)
                await asyncio.to_thread(start_refresher)

        # ── Hosted feature-video clips ──
        # Started HERE, after readiness, for the same reason the refresher above is:
        # the no-new-work-on-gateway-boot-path rule. Nothing downstream needs a clip
        # before the gateway can serve.
        #
        # The import sits INSIDE the flag test so that the TRANSFER's startup is
        # deferred behind the switch: with the feature off, no task is scheduled,
        # no config beyond the flag is read, and no manifest is looked for. The
        # modules themselves are already loaded — the dashboard imports both at
        # route setup — so this is about work, not import cost.
        #
        # The kill switch is read here because it decides whether this subsystem
        # exists for this process. The ceiling and the manifest stay inside the
        # task, where blocking work is allowed.
        if self._cfg.dashboard.feature_videos_enabled:
            from kiro_crew.feature_videos_cache import (
                start_background_feature_video_download,
            )

            self._feature_video_task = start_background_feature_video_download()

        # Claim the install-scoped telemetry reporter role for this process.
        # Install-level inventory (crons, skills, knowledge, config toggles) is the
        # same for every process in the install, so if each telemetry-enabled
        # process published it -- the gateway, gatewayd, spawned agents -- an
        # aggregate over installs would count this install once per process. This
        # is the gateway, so it is the one that publishes.
        #
        # It lives in run() itself, unconditionally: the claim belongs to being the
        # gateway, not to any single service, and a feature-flagged host would take
        # the whole subsystem down with it. _init_autonudge() below returns early
        # under KIROCREW_AUTONUDGE=0, so a claim made in there means no process ever
        # publishes inventory -- and nothing says so, because the gauge callbacks
        # bail before probing and leave probe.failures empty, which reads exactly
        # like a host that stopped exporting. test_reporter_claim_is_unconditional
        # pins the call site. Best-effort: telemetry is never a boot blocker.
        try:
            from kiro_crew.metrics.inventory_gauges import mark_install_reporter

            mark_install_reporter()
        except Exception:  # noqa: BLE001 -- telemetry must never break gateway boot
            logger.debug("could not claim the telemetry inventory role", exc_info=True)

        # AutoNudge must run after dashboard init — _fire callback dereferences
        # self.dashboard_state. In --no-dashboard mode the guard inside _fire
        # early-returns so persisted loops are harmless until a dashboard
        # process takes over.
        await self._init_autonudge()

        # Per-member event-log startup reconcile. Runs AFTER AutoNudge is
        # constructed (it consults live loops to decide patrol closers) and
        # after slot restoration: member slots are rehydrated lazily on demand
        # rather than eagerly at boot, so ``state._slots`` here holds whatever
        # the dashboard restored, and any driving.open slot not present is
        # closed as interrupted. Off-loop (ensure/append are synchronous file
        # IO) and best-effort — the helper swallows its own failures so a
        # logging fault never blocks boot.
        if self.dashboard_state is not None:
            from kiro_crew import eventlog_hooks

            async def _reconcile_members() -> None:
                # Off-loop (ensure/append are synchronous file IO, and first-boot
                # migration fsyncs per record) and best-effort. Run as a background
                # task rather than an awaited boot step: it must not delay Slack
                # availability or socket connect, and it is idempotent on reboot.
                try:
                    await asyncio.to_thread(
                        eventlog_hooks.reconcile_members_at_startup,
                        self._cfg,
                        self.dashboard_state,
                        self.autonudge_svc,
                    )
                except Exception:
                    logger.debug("member event-log startup reconcile failed", exc_info=True)

            # Retain a strong reference: a bare create_task is only weakly held,
            # so the loop could garbage-collect it mid-run.
            self._member_reconcile_task = asyncio.create_task(_reconcile_members())

        # Wire up event routing and interactive handlers
        init_interactions(self)
        # Awaited ON the loop, never offloaded whole: WSSocketModeClient's
        # __init__ ends in ``asyncio.ensure_future``, which needs a current
        # event loop in the *constructing* thread — a ``to_thread`` worker has
        # none, so offloading the whole function to keep its blocking YOLO-grant
        # profiles walk off the loop crashes every Slack-enabled boot with
        # "There is no current event loop".  The two
        # blocking calls inside (the YOLO grant and the enterprise auth.test)
        # are offloaded individually within the coroutine instead, preserving
        # the security-relevant early-return ordering.  Pinned by
        # test_slack_events_coverage.py::TestInitSocketMode.
        await init_socket_mode(self, seen)

        await self._start_channel_transports()

        # Update coordinator — fire-and-forget, NOT awaited. The first cycle runs
        # immediately; later cycles own both checking and automatic application.
        # Registered so shutdown can cancel a stalled network or installer child.
        print("👻 Checking for updates…")
        self._update_check_task = asyncio.create_task(self._run_update_checks())
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
                # Off the loop: the Remote hint resolves the host's own name, and
                # a name the resolver cannot answer (every hosted macOS runner)
                # would otherwise sit on the loop for the lookup's bound.
                url_lines = await asyncio.to_thread(
                    format_dashboard_urls,
                    dashboard_url,
                    port=self._dashboard_port,
                    local_only=self._local_only,
                    has_custom_host=bool(self._configured_host),
                )
                for line in url_lines:
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
                # It sits ON the boot path, where an escaping failure WOULD take
                # the gateway down, so every failure is contained here rather than
                # propagated. The dashboard is already listening either way — the
                # operator loses a printed line, not
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
            _scope_task.add_done_callback(log_probe_failure)

        # Block until shutdown
        await shutdown_event.wait()
        await self._shutdown_and_exit(_watchdog)

    def _install_shutdown_signal_handlers(self) -> None:
        """Make owner stop effective before waiting on memory preparation."""
        from kiro_crew.session import cleanup_orphaned_sessions

        # ── Signal handlers ──
        # Installed after READY and before memory preparation or update checks
        # are awaited. A slow worker must not prevent Ctrl-C from setting the
        # same event used by the authenticated owner shutdown route.
        loop = asyncio.get_running_loop()
        _shutting_down = False

        def _on_signal(*_args: object) -> None:
            nonlocal _shutting_down
            if _shutting_down:
                print("\n👻 Force exit!")
                # Synchronous by necessity: a signal handler cannot await.
                # The process calls os._exit immediately below, so loop latency
                # does not matter on this path.
                #
                # ``narrow_with_leaders=False`` so this handler does exactly the
                # work it did before the recycled-pid change: killing leftover
                # processes is what this path is for, and a handler that reaches
                # for extra work before its os._exit is a handler that may not
                # get there.
                cleanup_orphaned_sessions(narrow_with_leaders=False)
                # Same reason as the log queue below: os._exit skips atexit, so the
                # member event log's own drain hook never runs. Synchronous because
                # a signal handler cannot await, and bounded inside the module for
                # the same reason the log-queue drain is bounded here -- a wedged
                # disk must delay this exit, never hold it.
                try:
                    from kiro_crew import eventlog_hooks

                    eventlog_hooks.drain_for_shutdown()
                except Exception:
                    pass  # force exit must never be blocked by bookkeeping
                # os._exit skips atexit, so the log queue's drain hook never
                # runs — flush the queued gateway.log tail here, bounded so a
                # wedged disk cannot hang the force exit.
                try:
                    from kiro_crew.cli import _stop_log_queue_listener

                    _stop_log_queue_listener(timeout=2.0)
                except Exception:
                    pass  # force exit must never be blocked by logging
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

    async def _shutdown_and_exit(self, watchdog: asyncio.Future | None = None) -> None:
        """Use the same bounded cleanup for early owner stop and normal shutdown."""
        from kiro_crew.session import cleanup_orphaned_sessions

        print("👻 Shutting down…")

        # Exit status for the os._exit below. 0 for an operator stop (SIGTERM,
        # `systemctl stop`, Ctrl-C) so a restart-on-failure supervisor leaves
        # the gateway down as asked. Non-zero when the stale-asset watchdog is
        # what set the event: that shutdown exists ONLY to be restarted, and
        # a supervisor with `Restart=on-failure` semantics never relaunches an
        # exit 0 — a unit generated before `Restart=always` landed stranded a
        # gateway for hours on exactly this path. The watchdog has already
        # returned (True on the vanish path) by the time it sets the event, so
        # its task result is the signal; see shutdown_exit_code.
        #
        # The listener guard is the other self-initiated shutdown: it set the
        # event because the TCP listener died and could not be rebound, so the
        # process was alive but unreachable. That state must never be an
        # exit 0 either -- the supervisor has to relaunch it.
        # BOTH guards, because either can be the one that gave up. The second
        # loopback family's guard normally degrades instead of exiting, but it
        # escalates to this exit when its sidecar can be neither removed nor
        # blanked -- a live credential readable for an address nothing holds --
        # and reading only the primary would turn that escalation into an exit 0
        # the supervisor does not relaunch.
        exit_code = (
            shutdown_exit_code(watchdog)
            or listener_guard_exit_code(getattr(self.dashboard_state, "_listener_guard", None))
            or listener_guard_exit_code(
                getattr(self.dashboard_state, "_secondary_listener_guard", None)
            )
        )

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
        # The clear itself is filesystem work -- unlinking the marker, the pid and
        # start sidecars, and every credential sidecar the listeners published --
        # so it is offloaded rather than run on the loop. It is AWAITED, not
        # queued, because the ordering above is the point: the clear has to land
        # before _shutdown() frees the listener. An awaited thread also cannot be
        # beaten by os._exit the way a loop callback can.
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
                await asyncio.to_thread(run_marker.clear_marker, self._dashboard_port)
        except Exception:
            logger.debug("Gateway run-marker clear skipped", exc_info=True)

        try:
            await asyncio.wait_for(self._shutdown(), timeout=GRACEFUL_SHUTDOWN_SECS)
        except (asyncio.TimeoutError, Exception):
            logger.warning("Graceful shutdown timed out — force exiting")

        print("👻 Goodbye!")
        # Kill any kiro-cli processes that survived graceful shutdown.
        # Off-loop: still inside the orchestrator coroutine here.
        #
        # The one call site that asks for the leaders narrowing. Nothing spawns a
        # session by this point, so the sweep is not racing a mapping publisher --
        # the same position the sweep already held here before this change.
        await asyncio.to_thread(cleanup_orphaned_sessions)
        # The session's log's buffered appends, for EVERY gateway mode. The
        # dashboard registers its own cleanup hook, but a mode that builds no
        # dashboard app -- slack-only is the plain case -- never runs one, and
        # os._exit below skips atexit, so without this the buffer dies with the
        # process. What it drops is the last thing each session did, which is
        # exactly what a reader looks for after a restart. Bounded inside the
        # emitter and off-loop, like the log-queue drain that follows; calling it
        # twice is a no-op, so the dashboard hook stays as it is.
        try:
            # Imported HERE, not at module scope: AUTOSDE's
            # no-new-work-on-gateway-boot-path rule asks for an optional subsystem's
            # import to be gated, and a shutdown drain is the only use in this module.
            from kiro_crew.crew_log import emit as crew_log_emit

            if not await asyncio.to_thread(crew_log_emit.drain_for_shutdown):
                logger.warning("the session's log did not fully drain before exit")
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("the session's log drain failed during shutdown", exc_info=True)
        # The member event log's queued appends, for the same reason and on the same
        # terms. Its appends are ORDERED on one executor, so the tail sitting there
        # at exit is the newest transitions -- a patrol stop, a slot close -- and
        # they are exactly what a reader looks for after a restart. It registers an
        # atexit hook of its own, which os._exit skips, so this is the only drain
        # that runs on this path. Bounded inside the module and off-loop, like the
        # drain above; calling it twice is a no-op.
        try:
            from kiro_crew import eventlog_hooks

            if not await asyncio.to_thread(eventlog_hooks.drain_for_shutdown):
                logger.warning("the member event log did not fully drain before exit")
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.debug("the member event log drain failed during shutdown", exc_info=True)
        # This is a hard exit too: os._exit skips atexit, so the log queue's
        # drain hook never runs here either. Without this the whole shutdown
        # tail is lost -- including the "Graceful shutdown timed out" warning
        # logged a few lines up, the one record a stuck-shutdown post-mortem
        # actually needs. Bounded and off-loop so a wedged disk cannot delay
        # the exit (see drain_log_queue_before_hard_exit).
        from kiro_crew.cli import drain_log_queue_before_hard_exit

        await drain_log_queue_before_hard_exit()
        os._exit(exit_code)

    # ── per-channel hoists ───────────────────────────────────────────────
    # Each one is the ONLY place its channel's boot state is derived, so a
    # restart re-derives exactly what boot derived. A hoist resets the legacy
    # ``_<channel>_client`` mirror too: it runs only before a start (boot or
    # restart), where the mirror is None by construction.

    def _hoist_wecom(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        self._wecom_bot_id = creds.get(CRED_WECOM_BOT_ID, "")
        self._wecom_secret = creds.get(CRED_WECOM_SECRET, "")
        self._wecom_enabled = bool(cfg.wecom.enabled and self._wecom_bot_id and self._wecom_secret)

    def _hoist_telegram(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
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

    def _hoist_weixin(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
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

    def _hoist_whatsapp(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # WhatsApp (QR-linked personal account) — no credential: pairing state
        # lives in the channel's session DB, created by the Settings QR flow.
        # Enablement is config-only; maybe_start_whatsapp reports the missing
        # optional dependency or an unpaired session via the status badge.
        self._whatsapp_enabled = bool(cfg.whatsapp.enabled)
        self._whatsapp_client: "WhatsAppClient | None" = None

    def _hoist_feishu(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Feishu (Lark/飞书) — FEISHU_APP_ID / FEISHU_APP_SECRET (env/.env),
        # matching the Feishu developer console's own naming; everything else
        # from the typed cfg.feishu dataclass. Both are registered credentials,
        # so they are stripped from the agent subprocess environment by
        # sandbox._AGENT_DENIED_ENV_KEYS — the gateway is their only consumer.
        # Deny-by-default: an empty allowed_open_ids authorises nobody, and a
        # group chat needs BOTH allow_group and an allow-listed chat_id. The
        # client handle is owned by the channel registry (``kiro_crew.channels``),
        # which also closes it on shutdown.
        self._feishu_app_id = creds.get(CRED_FEISHU_APP_ID, "")
        self._feishu_app_secret = creds.get(CRED_FEISHU_APP_SECRET, "")
        self._feishu_enabled = bool(
            cfg.feishu.enabled and self._feishu_app_id and self._feishu_app_secret
        )
        self._feishu_allowed_open_ids: list[str] = list(cfg.feishu.allowed_open_ids)
        self._feishu_allow_group: bool = bool(cfg.feishu.allow_group)
        self._feishu_allowed_group_ids: list[str] = list(cfg.feishu.allowed_group_ids)

    def _hoist_discord(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
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

    def _hoist_webex(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # Webex — the WEBEX_BOT_TOKEN credential (env/.env) overrides
        # cfg.webex.bot_token; all other settings come from the typed
        # cfg.webex dataclass (no ad-hoc config.json re-parse).
        self._webex_bot_token = creds.get(CRED_WEBEX_BOT_TOKEN, "") or cfg.webex.bot_token
        self._webex_enabled = bool(cfg.webex.enabled and self._webex_bot_token)
        self._webex_allowed_emails: list[str] = list(cfg.webex.allowed_emails)
        self._webex_client: "WebexClient | None" = None

    def _hoist_imessage(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
        # iMessage — no credential exists to hoist: the transport is the user's
        # own signed-in Messages.app, so enablement is the config flag alone.
        # Everything else is read from the typed cfg.imessage dataclass at start.
        self._imessage_enabled = bool(cfg.imessage.enabled)
        self._imessage_client: "IMessageClient | None" = None

    def _hoist_teams(self, cfg: KiroCrewConfig, creds: Mapping[str, str]) -> None:
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

    # ── live config: per-channel restart + Slack hot fields ─────────────

    #: Slack-owned top-level and section prefixes whose changes the Slack applier
    #: reconciles. Everything Slack reads lives under ``slack.*`` in the
    #: serialized document (``slack.channels``, ``slack.dm_activation`` and the
    #: ``observe_*`` caps are emitted inside the ``slack`` section) plus the
    #: transport switch under ``messaging``.
    _SLACK_APPLIER_PREFIXES: tuple[str, ...] = ("slack", "messaging")

    def _register_config_appliers(self) -> None:
        """Subscribe this orchestrator's config appliers on the process watcher.

        Bound methods are held weakly by the watcher, so an orchestrator built
        and discarded by a test does not pin itself into the registry. The
        Subscription objects are kept on ``self`` so they live as long as the
        orchestrator does.
        """
        channel_prefixes = tuple(
            d.channel_type for d in registry.bootable(builtin_channel_descriptors())
        )
        self._config_subs = [
            live.subscribe(
                *channel_prefixes,
                callback=self._on_channel_config_change,
                name="GatewayOrchestrator.channel_restart",
            ),
            live.subscribe(
                *self._SLACK_APPLIER_PREFIXES,
                callback=self._on_slack_config_change,
                name="GatewayOrchestrator.slack",
            ),
        ]

    # gateway_runtime/channel_lifecycle.py
    _on_channel_config_change = _channel_lifecycle._on_channel_config_change

    _restart_changed_channels = _channel_lifecycle._restart_changed_channels

    channel_restarts_settled = _channel_lifecycle.channel_restarts_settled

    restart_channel = _channel_lifecycle.restart_channel

    _forget_superseded_start = _channel_lifecycle._forget_superseded_start

    _close_channel_locked = _channel_lifecycle._close_channel_locked

    _on_slack_config_change = _channel_lifecycle._on_slack_config_change

    _adopt_channel_sections_from_watcher = _channel_lifecycle._adopt_channel_sections_from_watcher

    _adopt_channel_section_from_watcher = _channel_lifecycle._adopt_channel_section_from_watcher

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
        standard OSS build) the gate permits, so every transport starts. Slack is
        a registry member too (``start=None``) but is gated in ``_connect_slack``
        rather than here,
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
        the no-policy default permits: every ENABLED transport resolves to permit
        and starts.
        """
        if descriptors is None:
            descriptors = builtin_channel_descriptors()
        boot = registry.bootable(descriptors)
        creds = await self._adopt_channel_sections_from_watcher(boot)
        enabled = {
            d.channel_type: bool(getattr(self, f"_{d.channel_type}_enabled", False)) for d in boot
        }
        # The enabled-only gate below never calls a factory whose flag is
        # False — for a disabled and an enabled-but-uncredentialed channel
        # alike — so a factory-level skip-reason log can never be reached.
        # Say WHY each channel is being skipped here, at the decision point.
        # Runs after KIROCREW_READY, outside the
        # boot-path window. Each row lists exactly the credential operands its
        # _<channel>_enabled predicate reads: telegram folds in the
        # deprecated-accounts stop (which already has its own warning at
        # config-load time, so pointing at the token would misname the actual
        # blocker), and teams deliberately omits the tenant id its predicate
        # never reads. whatsapp/imessage enablement is config-only (no
        # credential operand), so they have no row.
        uncredentialed_probe_rows: tuple[
            tuple[str, str, bool, tuple[tuple[str, bool], ...]], ...
        ] = (
            (
                "wecom",
                "WeCom",
                self._cfg.wecom.enabled,
                (
                    (CRED_WECOM_BOT_ID, bool(self._wecom_bot_id)),
                    (CRED_WECOM_SECRET, bool(self._wecom_secret)),
                ),
            ),
            (
                "telegram",
                "Telegram",
                bool(self._cfg.telegram.enabled and not self._cfg.telegram.accounts),
                ((CRED_TELEGRAM_BOT_TOKEN, bool(self._telegram_bot_token)),),
            ),
            (
                "weixin",
                "WeChat",
                self._cfg.weixin.enabled,
                (
                    (CRED_WEIXIN_TOKEN, bool(self._weixin_token)),
                    ("weixin.account_id", bool(self._weixin_account_id)),
                ),
            ),
            (
                "feishu",
                "Feishu",
                self._cfg.feishu.enabled,
                (
                    (CRED_FEISHU_APP_ID, bool(self._feishu_app_id)),
                    (CRED_FEISHU_APP_SECRET, bool(self._feishu_app_secret)),
                ),
            ),
            (
                "discord",
                "Discord",
                self._cfg.discord.enabled,
                ((CRED_DISCORD_BOT_TOKEN, bool(self._discord_bot_token)),),
            ),
            (
                "webex",
                "Webex",
                self._cfg.webex.enabled,
                ((CRED_WEBEX_BOT_TOKEN, bool(self._webex_bot_token)),),
            ),
            (
                "teams",
                "Teams",
                self._cfg.teams.enabled,
                (
                    (CRED_MICROSOFT_APP_ID, bool(self._teams_app_id)),
                    (CRED_MICROSOFT_APP_PASSWORD, bool(self._teams_app_password)),
                ),
            ),
        )
        for channel_type, settings_name, cfg_enabled, presence in uncredentialed_probe_rows:
            warn_if_channel_uncredentialed(channel_type, settings_name, cfg_enabled, presence)
        loop = asyncio.get_running_loop()
        permitted = await loop.run_in_executor(
            maintenance_executor(),
            lambda: {
                m: (_channel_transport_permitted(m) if enabled[m] else False) for m in enabled
            },
        )
        # BEFORE starting: a channel that starts sets its own badge, so seeding
        # first lets a success overwrite this and leaves it only where the factory
        # bailed out early.
        await loop.run_in_executor(maintenance_executor(), self._badge_unready_channels, boot)
        # Second adoption, per channel, right before its factory: the first pass
        # above fed the enabled census; this one feeds each transport's first
        # authorization state with whatever landed while the channels ahead of it
        # were connecting. Synchronous by contract (see the hook's docstring).
        self._channel_handles = await registry.start_channels(
            self,
            descriptors,
            permitted,
            before_start=lambda desc: self._adopt_channel_section_from_watcher(desc, creds),
        )
        # A connection-parameter edit that landed while the channels were still
        # starting was deferred by the applier and sits in the watcher's stale
        # table; its next tick re-runs the applier against the current snapshot,
        # with the same per-channel restart the edit would have triggered a
        # moment later.
        self._channel_transports_started = True
        # Tell the sender of whatever the SHUTDOWN GATE refused on the way down
        # that it was never processed. Ordered AFTER the transports
        # because the notice is sent through the channel that received the
        # message, and detached from boot so a slow platform send cannot hold the
        # gateway's start open. The spool's location is resolved HERE, on the
        # loop, as the task is scheduled: the pass reads the spool on a worker
        # thread, and a path resolved there would name whatever data home the
        # environment holds at that later moment -- not the one this gateway
        # booted under. Under the test suite that moment is after the starting
        # test's pins are gone; five full runs left a lock file in the operator's
        # REAL data home that way.
        self._schedule_inbound_replay()

    # gateway_runtime/channel_lifecycle.py
    _schedule_inbound_replay = _channel_lifecycle._schedule_inbound_replay

    _replay_spooled_inbound = _channel_lifecycle._replay_spooled_inbound

    _badge_unready_channels = _channel_lifecycle._badge_unready_channels


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.slack.gateway.<name>`` reaches it wherever it now lives; see
# ``gateway_runtime``. Run once, after the class has bound the owner methods.
_runtime.compose(
    globals(),
    GatewayOrchestrator,
    (
        _admission,
        _channel_lifecycle,
        _cron_dispatch,
        _cron_verdict,
        _delivery,
        _mcp_broker,
        _memory_lifecycle,
        _tool_policy,
    ),
)


# Strong reference to the background slice-limit apply task: the event loop
# holds tasks weakly, so a fire-and-forget create_task with no reference can
# be garbage-collected mid-flight.
_SLICE_LIMITS_TASK: "asyncio.Task[None] | None" = None

# Strong ref to the fire-and-forget agents-dir janitor sweep launched at boot
# (the loop holds tasks weakly, so without this it could be GC'd mid-flight).
_AGENTS_JANITOR_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the liveness-keyed agent-scratch sweep loop.
_AGENT_SCRATCH_SWEEP_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the kiro-cli log cap loop.
_KIRO_CLI_LOG_CAP_TASK: "asyncio.Task[None] | None" = None


async def run_gateway(
    cfg: KiroCrewConfig,
    *,
    no_dashboard: bool = False,
    no_crons: bool = False,
    no_tunnel: bool = False,
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
    # ── Name the default executor ──
    # asyncio.to_thread and run_in_executor(None, ...) route onto the loop's
    # default executor, which Python names threads anonymously.  This names
    # them ``mc-default`` so profilers like py-spy can attribute blocking work
    # to this gateway.  Must run BEFORE any to_thread offload.
    configure_default_executor()

    # ── Publish surface, pinned for the process ──
    # Recorded BEFORE any service spins up, because both doors out are opened by
    # services started below: the dashboard's boot-time ``setup_tunnel`` and the
    # on-demand provisioning in ``slack.allowlist`` that a Slack message can reach
    # as soon as the gateway is listening. Set unconditionally so a False here
    # also CLEARS a value a previous gateway left behind in the same process
    # (the test harness boots more than one), rather than letting it leak.
    set_publish_disabled(no_tunnel)

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
                    # The per-spawn skill-view prune is capped, so a backlog
                    # left by earlier builds would take hundreds of spawns to
                    # clear. Drain it once here, in lock-bounded batches.
                    from kiro_crew.agent_sdk.drivers import acp as acp_driver

                    acp_driver.drain_skill_view_aliases()

                await asyncio.to_thread(_sweep_in_thread)
            except Exception:
                logging.getLogger(__name__).debug(
                    "agents-dir janitor sweep failed at boot", exc_info=True
                )

        _AGENTS_JANITOR_TASK = asyncio.create_task(_run_agents_janitor(), name="agents-dir-janitor")

    # ── Agent scratch + work root sweep (fire-and-forget, hourly) ──
    # Reclaim per-process agent scratch dirs whose owner process is dead
    # (see kiro_crew.agent_scratch). Liveness-keyed, never age-keyed, so a
    # long-lived session's in-flight work is never deleted under it -- and
    # because agent processes can OUTLIVE a gateway restart, the sweep checks
    # each recorded owner pid instead of clearing wholesale at boot. Hourly
    # repeats catch processes that die while the gateway stays up (no
    # per-teardown hook: the positive liveness signal covers every death
    # path by construction). Same containment posture as the janitor above:
    # offloaded, fail-open, skipped in test_mode. The same wake also sweeps the
    # cross-process work root (see kiro_crew.work_root), which is idle-keyed
    # BECAUSE outliving its creator is that root's contract.
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
                # The cross-process work root rides the SAME hourly wake rather
                # than a scheduler of its own: it reclaims on an idle window
                # with no correctness deadline either, and a second timer would
                # double the wake cost for nothing. Its own try/except, so one
                # root's failure never skips the other's sweep.
                try:
                    await asyncio.to_thread(work_root.sweep_work_root)
                except Exception:
                    logging.getLogger(__name__).debug("work-root sweep failed", exc_info=True)
                # The per-run work directories of subagent and stateless cron
                # sessions ride the same wake. The ordinary end of a run reclaims
                # its own (AcpProvider.shutdown); the boot sweep took what a dead
                # predecessor of this data home left, and this re-runs the SAME
                # predecessor-only rule for whatever its bounds left over. A
                # directory this process marked is never its business; one a
                # registered provider names is skipped whatever it holds.
                # ``orchestrator`` is bound later in this function and read only
                # here, an hour in.
                try:
                    await asyncio.to_thread(
                        _sweep_predecessor_session_work_dirs,
                        _live_session_work_dirs(getattr(orchestrator, "sessions", None)),
                    )
                except Exception:
                    logging.getLogger(__name__).debug(
                        "session work-dir sweep failed", exc_info=True
                    )

        _AGENT_SCRATCH_SWEEP_TASK = asyncio.create_task(
            _run_agent_scratch_sweep(), name="agent-scratch-sweep"
        )

    # ── kiro-cli log cap (fire-and-forget, every 5 minutes) ──
    # Each spawned kiro-cli logs into its own scratch dir (agent_scratch
    # .scratch_env pins KIRO_CHAT_LOG_FILE there) and never bounds that log
    # while running; at KIRO_LOG_LEVEL=debug one process writes ~40 MiB a
    # minute for as long as it lives. The hourly sweep above is too slow for
    # that rate, so this loop rotates any oversized log in place. Same
    # posture: offloaded, fail-open, sleep-first, skipped in test_mode.
    global _KIRO_CLI_LOG_CAP_TASK
    if not test_mode:

        async def _run_kiro_cli_log_cap() -> None:
            while True:
                await asyncio.sleep(agent_scratch.KIRO_CLI_LOG_CAP_INTERVAL_SECONDS)
                try:
                    await asyncio.to_thread(agent_scratch.cap_kiro_cli_logs)
                except Exception:
                    logging.getLogger(__name__).debug("kiro-cli log cap failed", exc_info=True)

        _KIRO_CLI_LOG_CAP_TASK = asyncio.create_task(
            _run_kiro_cli_log_cap(), name="kiro-cli-log-cap"
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
    try:
        await orchestrator.run()
    finally:
        # Includes failures before READY, when the restore worker was never
        # scheduled. Release only this gateway's latch after closing its handle.
        await asyncio.to_thread(orchestrator._stop_memory_startup)
