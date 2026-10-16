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
``gateway_runtime``: tool-approval policy and the approval callbacks, the cron
callback with its command, script and agent-turn arms, cron dispatch and run
tallies, cron result delivery and failure alerts, delivery routing and the result
egress legs, the channel lifecycle, the MCP broker, the memory lifecycle, service
construction and ``run``, the heartbeat, admission, boot-time install repair, the
update schedule and apply, AutoNudge's wiring and fire paths, the exit path, and
the process entry point. This module stays their only import path and patch
surface, and their functions run on its globals, so an import no function left
here reads is still an owner's global or part of the module's name surface
(``test_gateway_runtime_bindings.py`` checks each is read). What stays here, and
why, is recorded in ``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""

from __future__ import annotations

# Many imports here are the gateway_runtime owners' globals, so setup.cfg turns F401 off for
# this file: removing one fails test_every_global_an_owner_function_reads_is_bound_on_the_facade,
# not a call at runtime, and test_gateway_runtime_bindings.py checks that each import is read.
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
    update_ownership,
    work_root,
)
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.agent_sdk import AgentTurnUsage
from kiro_crew.agents_janitor import sweep_agents_dir
from kiro_crew.autonudge import (
    APPROVAL_STALL_REASON,
    CONSECUTIVE_FAILURE_REASON,
    FINISHED_LOOP_REASONS,
    MODEL_REFUSED_REASON,
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
    reason_in,
    runtime_budget_exceeded,
    terminal_notification_delivery_matches,
)
from kiro_crew.beacon import distribution
from kiro_crew.channel_history import ChannelHistory
from kiro_crew.channels import builtin_channel_descriptors
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.config.live import ConfigChange, ConfigDeferred, Subscription
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
from kiro_crew.constants import (
    NO_TOOL_CALLS_NOTE,
    SUBAGENT_COMPLETION_META_KEY,
)
from kiro_crew.context import ContextBuilder, session_store_for_turn
from kiro_crew.context_management import summarize_result
from kiro_crew.cron import (
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
from kiro_crew.dashboard import cautious_boot, start_dashboard, token_secret
from kiro_crew.dashboard.chat_persistence import (
    rehydrate_slot_from_history_async,
    wait_for_unwinding_turns,
)
from kiro_crew.dashboard.chat_runner import (
    _arm_queued_delivery_settlement,
    _auto_approve_reason,
    _resolve_channel_target,
    _retire_sessions_on_identity_change,
    _run_chat,
    _slot_is_trusted,
    drain_idle_parent_queue_for_stall,
    turn_stats_meta,
)
from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    SUBAGENT_COMPLETION_KIND,
    _queued_depth_off_loop,
    dashboard_slot_key,
    mint_options_token,
    remember_slack_options,
    subagent_event_slot,
)
from kiro_crew.dashboard.cron_inject import (
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
    read_turn_model,
)
from kiro_crew.dashboard.listener_guard import listener_guard_exit_code
from kiro_crew.dashboard.origin import (
    build_dashboard_url,
    format_dashboard_urls,
    is_local_only,
    parse_dashboard_url,
    resolve_dashboard_host,
)
from kiro_crew.dashboard.persistence_watchdog import (
    probe_data_home,
    run_persistence_watchdog,
)
from kiro_crew.dashboard.persistence_watchdog import (
    shutdown_exit_code as persistence_lost_exit_code,
)
from kiro_crew.dashboard.sign_in_refresher import drain_sign_in_refresher, run_sign_in_refresher
from kiro_crew.dashboard.stale_asset_watchdog import (
    run_stale_asset_watchdog,
    shutdown_exit_code,
)
from kiro_crew.dashboard.stale_bundle_guard import check_bundle_freshness
from kiro_crew.dashboard.state import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_PREFIX,
    DashboardState,
    load_window_off_loop,
)
from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
from kiro_crew.dashboard.turn_dispatch import bounded_chat_turn, spawn_guarded_turn
from kiro_crew.embeddings import (
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
from kiro_crew.executors import (
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
from kiro_crew.gateway_restart import resolve_restart_launcher, supervisor_reentry
from kiro_crew.gateway_shutdown_budget import GRACEFUL_SHUTDOWN_SECS, UPDATE_INSTALLER_STOP_SECS
from kiro_crew.heartbeat import (
    HEARTBEAT_TASK_TIMEOUT_SECS,
    HeartbeatService,
    is_keep_response,
    strip_keep_sentinel,
)
from kiro_crew.history import ConversationLog, HistoryConsolidator
from kiro_crew.hooks import (
    HookManager,
    HooksConfig,
    _is_host_read_only_builtin,
    hooks_config_from_config_dict,
)
from kiro_crew.kiro_cli import PATH_ONLY_INSTALL_NOTE, is_bundled_kiro_cli, pin_kiro_cli
from kiro_crew.learn import LessonStore
from kiro_crew.llm_helpers import (
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    TOOL_ACTIVITY_ATTR,
    PromptBusyExhaustedError,
    ToolApprovalPolicy,
    acp_error_after_tool_activity,
    acp_error_is_session_not_found,
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
from kiro_crew.mcp_cron import vet_job_at_fire_time
from kiro_crew.mcp_gateway import is_gateway_supported
from kiro_crew.mcp_gateway.launch_approval import (
    LaunchApprovals,
    filter_target_env,
    load_approvals,
    save_pass,
)
from kiro_crew.mcp_gateway.launch_resolve import rewrite_kwargs
from kiro_crew.mcp_gateway.manager import (
    GatewayManager,
    GatewaySpec,
)
from kiro_crew.mcp_gateway.resolve_once import prefetch as resolve_prefetch
from kiro_crew.mcp_gateway.rewriter import rewrite_agents
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
    rollback_skill_bodies,
    stop_reason_landed,
)
from kiro_crew.messaging.display_safety import redact_for_display
from kiro_crew.messaging.egress import channel_egress_parts, send_channel_parts
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    CHANNEL_SESSION_NAMESPACES,
    CHAT_TYPE_DIRECT,
    DM_SCOPE_UNIFIED,
    SLACK_NAMESPACE,
    ChannelLink,
    channel_namespace_of,
    is_legacy_slack_key,
    parse_session_key,
)
from kiro_crew.messaging.renderer import (
    SilentRenderer,
    chunk_for_transport,
    display_safe,
)
from kiro_crew.messaging.spawn_approval_delivery import deliver_spawn_approval
from kiro_crew.messaging.transport import InboundMessage
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
from kiro_crew.platform.bootstrap import apply_kas_cli_floor
from kiro_crew.platform.context import (
    PlatformCompositionError,
    current_context,
    redact_log_via_context,
    redact_via_context,
    safe_context_call,
)
from kiro_crew.platform.governance_profiles import (
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
    AUTO_ROUTE_PROVIDER,
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
from kiro_crew.pre_exec_stops import run_pre_exec_stops
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
from kiro_crew.security.readonly_bash import command_invokes_git, is_read_only_bash
from kiro_crew.sel import flush_audit_queue_before_hard_exit, sel
from kiro_crew.service.common import restart_command_hint
from kiro_crew.session import (
    HEARTBEAT_KEY,
    SessionBusyError,
    SessionClosingError,
    SessionManager,
)
from kiro_crew.session_start_sizing import resolve_session_start_sizing
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
from kiro_crew.slack.gateway_runtime import cron_delivery as _cron_delivery
from kiro_crew.slack.gateway_runtime import cron_dispatch as _cron_dispatch
from kiro_crew.slack.gateway_runtime import cron_exec as _cron_exec
from kiro_crew.slack.gateway_runtime import cron_failure as _cron_failure
from kiro_crew.slack.gateway_runtime import cron_fire as _cron_fire
from kiro_crew.slack.gateway_runtime import cron_turn as _cron_turn
from kiro_crew.slack.gateway_runtime import cron_verdict as _cron_verdict
from kiro_crew.slack.gateway_runtime import delivery as _delivery
from kiro_crew.slack.gateway_runtime import exit_path as _exit
from kiro_crew.slack.gateway_runtime import heartbeat_turns as _heartbeat
from kiro_crew.slack.gateway_runtime import install_repair as _install
from kiro_crew.slack.gateway_runtime import mcp_broker as _mcp_broker
from kiro_crew.slack.gateway_runtime import memory_lifecycle as _memory_lifecycle
from kiro_crew.slack.gateway_runtime import nudge_channel_fire as _nudge_chan
from kiro_crew.slack.gateway_runtime import nudge_dashboard_fire as _nudge_dash
from kiro_crew.slack.gateway_runtime import nudge_wiring as _nudge_wiring
from kiro_crew.slack.gateway_runtime import process_entry as _entry
from kiro_crew.slack.gateway_runtime import result_egress as _egress
from kiro_crew.slack.gateway_runtime import service_boot as _boot
from kiro_crew.slack.gateway_runtime import tool_policy as _tool_policy
from kiro_crew.slack.gateway_runtime import turn_approvals as _approvals
from kiro_crew.slack.gateway_runtime import update_apply as _upd_apply
from kiro_crew.slack.gateway_runtime import update_schedule as _upd_sched
from kiro_crew.slack.gateway_runtime.channel_lifecycle import (
    _channel_transport_permitted,
    _push_observe_limits,
)
from kiro_crew.slack.gateway_runtime.cron_dispatch import (
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
from kiro_crew.slack.gateway_runtime.cron_fire import (
    _annotate_model_downgrade,
    _cron_extra_env,
    _CronCapture,
)
from kiro_crew.slack.gateway_runtime.cron_verdict import (
    _CRON_FAILURE_DETAIL_CAP,
    _EPOCH_RE,
    _EPOCH_WINDOW_SECS,
    _FAILURE_REMINDER_SECS,
    _NO_RESPONSE,
    _SUCCESS_REMINDER_SECS,
    _VOLATILE_RE,
    _annotate_partial_block,
    _dedup_text,
    _GateTally,
    _result_hash,
)
from kiro_crew.slack.gateway_runtime.heartbeat_turns import (
    _persist_turn_row,
)
from kiro_crew.slack.gateway_runtime.install_repair import (
    _KIRO_CLI_RESOLVE_TIMEOUT_SECS,
    _pinned_kiro_cli,
)
from kiro_crew.slack.gateway_runtime.nudge_channel_fire import (
    _NUDGE_TURN_TIMEOUT,
    _delivery_result,
    _DmDispatchAdapter,
)
from kiro_crew.slack.gateway_runtime.process_entry import (
    _live_session_agents,
    _live_session_work_dirs,
    _run_signing_key_preflight_bounded,
    _start_coder_retention,
    _start_kas_cli_floor,
    _stop_session_environments,
    _sweep_predecessor_session_work_dirs,
    run_gateway,
    signing_key_preflight,
)
from kiro_crew.slack.gateway_runtime.result_egress import (
    _heartbeat_slack_parts,
)
from kiro_crew.slack.gateway_runtime.tool_policy import (
    _BACKGROUND_APPROVAL_SOURCES,
    _HEARTBEAT_CORE_SERVER,
    _HEARTBEAT_SAFE_CORE_MCP_TOOLS,
    _HEARTBEAT_STATUS_PREFIXES,
    _READ_ONLY_TOOL_PREFIXES,
    _WRITE_INDICATORS,
    _bare_tool_name,
    _build_heartbeat_hooks,
    _is_heartbeat_safe_tool,
    _is_read_only_tool,
)
from kiro_crew.slack.gateway_runtime.update_schedule import (
    _UPDATE_STOP_GRACE_SECS,
)
from kiro_crew.slack.handler import (
    _get_agent_for_session,
    build_timing_footer,
    is_thread_incognito,
    is_thread_temporary,
    is_tracked_channel,
)
from kiro_crew.slack.outbound import PostedOptions
from kiro_crew.slack.retry import open_dm_with_retry
from kiro_crew.slack.scope_probe import log_probe_failure, warn_unreadable_tracked_channels
from kiro_crew.slack.transport import SlackTransport
from kiro_crew.subagent import (
    _TRANSIENT_CONTINUE_MSG,
    DIGEST_HOLD_SECS,
    INJECTION_TIMEOUT,
    ORPHAN_NOTICE_OWED,
    SpawnApprovalUnreachable,
    SubagentDelivery,
    SubagentInfo,
    SubagentManager,
    ToolApprovalCallback,
    _injection_notice_outcome,
    format_subagent_usage,
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
from kiro_crew.subagent_wait_reasons import (
    NEVER_STARTED_MEMORY_PREFIX,
    QUEUED_WAIT_EXPIRED_TEXT,
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
    from kiro_crew.vector_memory import VectorMemoryStore
    from kiro_crew.webex.client import WebexClient
    from kiro_crew.wecom.client import WeComClient
    from kiro_crew.weixin.client import WeixinClient
    from kiro_crew.whatsapp.client import WhatsAppClient


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
    return await _queued_depth_off_loop(manager, parent_session_key)


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

# Max attempts, the first one included, to inject a subagent result into a
# parent session.
_MAX_INJECT_ATTEMPTS = 2


# Budget for awaiting the in-flight run-marker write during shutdown. Bounded
# so a stalled write can never eat into GRACEFUL_SHUTDOWN_SECS (which saves
# active slots) — the marker is best-effort, the slot save is not.
_MARKER_WRITE_WAIT_SECS = 5.0

# How often the gateway re-stats the governance profiles directory and re-derives
# the on-disk auto-approvals when a saved profile has moved the governance answer.
# Bounds how long a profile that withdraws an auto-approved tool can be bypassed by
# a newly started session; an unchanged tick costs a directory stat.
_PROFILE_PROJECTION_POLL_S = 15.0


# Slack Block Kit section.text hard limit is 3000 chars.
# We split cron output at this boundary so each chunk fits in a section block.
_CRON_MSG_LIMIT = 3000


#: Session-key prefixes of a workflow RUN's step sessions, each followed by
#: ``<run_id>:``: per-call (``wf:``), pooled worker, unpooled call, and the
#: memory-scoped worker key.
_WORKFLOW_STEP_PREFIXES = ("wf:", "wf-pool:", "wf-unpooled:", "wf-worker:")


def _spawn_parent_slot(parent_key: str, workflow_service: Any) -> str:
    """The dashboard slot whose Trust governs a spawn parented on *parent_key*.

    A workflow step runs in its own ``<prefix>:<run_id>:...`` session, which no
    tab displays. Its spawns answer to the chat that STARTED the run: the registry
    records that origin ``session_key`` only after the run route proved the
    caller is that session. An unknown run or a blank origin yields ``""``, so
    the spawn prompts exactly as before.
    """
    if not parent_key:
        return ""
    if not parent_key.startswith(_WORKFLOW_STEP_PREFIXES):
        return subagent_event_slot(parent_key)
    run_id = parent_key.split(":", 2)[1]
    registry = getattr(workflow_service, "registry", None)
    try:
        handle = registry.get(run_id) if run_id and registry is not None else None
    except Exception:
        logger.warning("spawn slot: workflow run %s lookup failed", run_id, exc_info=True)
        return ""
    origin = str(getattr(handle, "session_key", "") or "")
    return subagent_event_slot(origin) if origin else ""


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


def _cron_failure_attribution(job: CronJob, _run_provider: object | None) -> tuple[bool, bool]:
    """Whether a failed cron run is its job's own failure, and whether its streak hands over.

    Returns ``(job_owns_failure, hand_over_streak)`` for :func:`_charge_failure`.
    """
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
    return _job_owns_failure, _hand_over_streak


def _charge_failure(job: CronJob, _hand_over_streak: bool) -> None:
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
        job.consecutive_failures = max(job.consecutive_failures, _AUTO_PAUSE_THRESHOLD - 1)
        runtime_death.clear_shared_deaths(f"cron:{job.id}")
    job.record_failure()


# Restart step named by the boot refusal. Deliberately static: probing for the
# installed service would stat HOME, which may be a hung network mount.
BOOT_RESTART_HINT = "kirocrew restart"

# Boot deadline for the signing-key preflight. Its lstat and key load normally
# take milliseconds (the loader's own retry budget is well under a second), so a
# preflight still running at this point is stuck on the filesystem.
_SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS = 30.0


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
    vector_memory: VectorMemoryStore
    _dashboard_port: int
    _configured_host: str
    _local_only: bool
    _member_reconcile_task: asyncio.Task[None]
    _persistence_watchdog_task: asyncio.Task[bool]
    _cron_quarantine: Path | None

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
        credentials: Mapping[str, str] | None = None,
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
        # *credentials* is ``load_credentials()`` already run off the event loop
        # (``run_gateway``): resolving a secret:// reference reads the vault.
        creds = dict(credentials) if credentials is not None else cfg.load_credentials()
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
        self._session_environments_ready = False
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
        # Shutdown waits on this task so an in-flight sign-in renewal can
        # persist its replacement credential before the hard exit.
        self._sign_in_refresher_task: "asyncio.Task[None] | None" = None
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
        # Periodic governance-profile re-projection; cancelled at shutdown beside the
        # distribution refresher, so no rebuild starts into a closing gateway.
        self._profile_watch_task: "asyncio.Task[None] | None" = None
        self._update_apply_deferred = False
        self._pending_update_respawn: Callable[[], str] | None = None
        # Whether the pending restart serves a policy floor, and its target key:
        # kept beside the respawn so every retry runs under the same mandatory
        # grace instead of restarting the clock.
        self._pending_update_mandatory = False
        self._pending_update_mandatory_key = ""
        self._mandatory_update_deferred_at: float | None = None
        self._mandatory_update_deferred_key: str | None = None
        # Managed-venv notices already sent in this process, so each reaches the
        # operator once per release: the signed metadata excludes this host
        # (``incompatible:<version>``), or applying it would detach the
        # sandbox's AppArmor profile (``reattach:<version>``). Nothing here
        # suppresses an attempt: incompatibility is decided again, before any
        # download, on every cycle.
        self._wheel_update_noticed: set[str] = set()
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

    # ------------------------------------------------------------------
    # Tool approval callback (shared by cron, heartbeat, subagent, task)
    # ------------------------------------------------------------------

    # gateway_runtime/turn_approvals.py
    _dashboard_client_attached = _approvals._dashboard_client_attached

    _interactive_approval = _approvals._interactive_approval

    _heartbeat_approval = _approvals._heartbeat_approval

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

    # ------------------------------------------------------------------
    # Service initialisation
    # ------------------------------------------------------------------

    # FTS rebuild is part of deferred memory initialization: it scans user
    # history and must complete before memory is released to consumers.

    # gateway_runtime/delivery.py
    _open_dm_with_retry = _delivery._open_dm_with_retry

    _record_cron_delivery = _delivery._record_cron_delivery

    _remember_options = _delivery._remember_options

    # gateway_runtime/delivery.py
    _channel_reply_link = _delivery._channel_reply_link

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

    # gateway_runtime/cron_failure.py
    _failure_alert_is_duplicate = _cron_failure._failure_alert_is_duplicate

    _slack_safe_fenced = _cron_failure._slack_safe_fenced

    _deliver_failure_alert = _cron_failure._deliver_failure_alert

    _advance_failure_dedup = _cron_failure._advance_failure_dedup

    _alert_cron_failure = _cron_failure._alert_cron_failure

    _cron_single_failure = _cron_failure._cron_single_failure

    # gateway_runtime/delivery.py
    _cron_job_is_silent = _delivery._cron_job_is_silent

    # gateway_runtime/cron_fire.py
    _init_cron = _cron_fire._init_cron

    _cron_capture = _cron_fire._cron_capture

    _resolve_cron_agent = _cron_fire._resolve_cron_agent

    _acquire_with_model_fallback = _cron_fire._acquire_with_model_fallback

    _retained_session_agent_mismatch = _cron_fire._retained_session_agent_mismatch

    # gateway_runtime/cron_exec.py
    _cron_run_command = _cron_exec._cron_run_command

    _deliver_script_result = _cron_exec._deliver_script_result

    _cron_run_script = _cron_exec._cron_run_script

    # gateway_runtime/cron_turn.py
    _cron_run_sequence = _cron_turn._cron_run_sequence

    _cron_run_single = _cron_turn._cron_run_single

    # gateway_runtime/cron_delivery.py
    _deliver_cron_agent_result = _cron_delivery._deliver_cron_agent_result

    def _announce_cron_quarantine(self) -> None:
        """Tell the dashboard that startup moved an unreadable cron store aside."""
        moved = getattr(self, "_cron_quarantine", None)
        if moved is None or self.dashboard_state is None:
            return
        self.dashboard_state.notify(
            "cron",
            "Scheduled tasks could not be read",
            "The schedule file was not a readable schedule (invalid JSON, or no jobs "
            f"list), so Kiro Crew moved it to {moved} and started with no scheduled "
            "tasks. Nothing was deleted. To restore: fix that copy, stop Kiro Crew, "
            f"move it back to {data_home() / 'crons.json'}, and start again. "
            "`kirocrew doctor` shows the same steps.",
        )

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

    # gateway_runtime/heartbeat_turns.py
    _init_heartbeat = _heartbeat._init_heartbeat

    # gateway_runtime/nudge_channel_fire.py
    _fire_slack_nudge = _nudge_chan._fire_slack_nudge

    _fire_dm_nudge = _nudge_chan._fire_dm_nudge

    _fire_discord_nudge = _nudge_chan._fire_discord_nudge

    _fire_webex_nudge = _nudge_chan._fire_webex_nudge

    _stop_message_loop_if_structural_terminal = (
        _nudge_chan._stop_message_loop_if_structural_terminal
    )

    _monitor_completion_hook = _nudge_chan._monitor_completion_hook

    _report_monitor_completion = _nudge_chan._report_monitor_completion

    # gateway_runtime/nudge_dashboard_fire.py
    _fire_dashboard_nudge = _nudge_dash._fire_dashboard_nudge

    _work_ledger_wake_loop = _nudge_dash._work_ledger_wake_loop

    _work_ledger_wake_text = _nudge_dash._work_ledger_wake_text

    _steer_work_ledger_wake = _nudge_dash._steer_work_ledger_wake

    _dashboard_mode_admits = staticmethod(_nudge_dash._dashboard_mode_admits)

    _audit_fire_refused = staticmethod(_nudge_dash._audit_fire_refused)

    # gateway_runtime/nudge_wiring.py
    _init_autonudge = _nudge_wiring._init_autonudge

    _notify_nudge_held = _nudge_wiring._notify_nudge_held

    _notify_nudge_expired = _nudge_wiring._notify_nudge_expired

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
        # A memory-wait expiry has no folder either, but the store owes its report
        # until the announce is consumed, so its debt is carried the same way.
        owed: list[SubagentDelivery] = []
        if not flush_only and info.outcome == "completed":
            owed.append(SubagentDelivery(info.id, info.elapsed, info.credits))
        elif not flush_only and getattr(info, "_report_owed", False) is True:
            owed.append(SubagentDelivery(info.id, info.elapsed, info.credits, report_owed=True))
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

    # gateway_runtime/result_egress.py
    _deliver_cron_response = _egress._deliver_cron_response

    _deliver_channel_reply = _egress._deliver_channel_reply

    _cron_outbound_thread = _egress._cron_outbound_thread

    _persist_slot_title = _egress._persist_slot_title

    _resolve_loaded_slot = _egress._resolve_loaded_slot

    _deliver_result = _egress._deliver_result

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
            if slot.recovery_retrigger_count >= _max_retrigger:
                logger.warning(
                    "Recovery retrigger cap (%d) reached for %s, dropping %d queued failures",
                    _max_retrigger,
                    parent_key,
                    len(slot._pending_subagent_failures),
                )
                slot._pending_subagent_failures.clear()
                return
            slot.recovery_retrigger_count += 1
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
                    ),
                    state=self.dashboard_state,
                    slot=slot,
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
                retry doesn't hit 'Prompt already in progress'. A live backend
                that lost the parent's session is answered once by a reset and a
                fresh claim, which re-loads the same backend id; the caller's
                single ``release(parent_key)`` balances against that claim.
                """
                _snf_reloaded = False
                attempt = 0
                while True:
                    try:
                        response = await stream_and_collect(client, msg, retry_transient=False)
                        # The parent's turn consumed the report: the positive
                        # acknowledgement an owed expiry report is cleared on.
                        info._report_delivered = True
                        return response
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
                    except AcpError as exc:
                        if acp_error_is_session_not_found(exc):
                            if acp_error_after_tool_activity(exc):
                                raise AcpError(
                                    f"{SESSION_NOT_FOUND_NOT_REPLAYED_TEXT} {exc}"
                                ) from exc
                            if _snf_reloaded:
                                raise AcpError(f"{SESSION_NOT_FOUND_GIVE_UP_TEXT} {exc}") from exc
                            _snf_reloaded = True
                            logger.warning(
                                "Subagent %s: backend lost parent session %s during %s "
                                "injection — re-loading it on a fresh runtime",
                                info.id,
                                parent_key,
                                label,
                            )
                            assert self.sessions is not None
                            await self.sessions.reset(parent_key)
                            client, _is_new, _resumed = await self.sessions.get_or_create(
                                parent_key
                            )
                            continue
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
                        attempt += 1

            # A synthetic flush-only record is NOT a wave member (see
            # SubagentManager.force_digest_flush): it exists only to force the
            # wave's pending digest chunk out when its hold deadline expired.
            # Every per-member side effect below must be skipped for it — a
            # terminal WS event, orchestration accounting or a done/ok counter
            # bump would invent an agent that never ran.
            _flush_only = getattr(info, "_digest_flush_only", False) is True
            parent_key = info.parent_session_key
            _parent_slot_name = dashboard_slot_key(parent_key)

            _injection_slot = None
            if self.dashboard_state and _parent_slot_name:
                from kiro_crew.dashboard.handlers.messaging import _slot_for_parent

                _injection_slot = _slot_for_parent(self.dashboard_state, parent_key)
                if _injection_slot is None:
                    _injection_slot = self.dashboard_state.get_slot(_parent_slot_name)
                if getattr(_injection_slot, "window_pending", False) is True:
                    # A parent tab restored as a sidebar row loads its window
                    # before the completion is delivered into it, off the loop.
                    await load_window_off_loop(_injection_slot)
                    _injection_slot = _slot_for_parent(
                        self.dashboard_state, parent_key
                    ) or self.dashboard_state.get_slot(_parent_slot_name)
            _completion_key = getattr(_injection_slot, "key", "")
            _injection_slot_name = (
                _completion_key
                if isinstance(_completion_key, str) and _completion_key
                else _event_slot(parent_key)
            )

            if not _flush_only:
                await _broadcast_subagent_status(info, "done", _injection_slot_name)
            # Three-way outcome: a user stop is neutral — neither a success nor
            # a failure. The record contract keeps ``error`` unset for stops, so
            # every consumer below must branch on ``user_stopped`` explicitly
            # rather than inferring success from an empty error.
            if info.user_stopped:
                # The stop's own origin when the record carries one (a
                # parent-end verb), so the announce does not
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
                # Same origin as the status line above: a parent end or a row
                # chat Autopilot queued must not read as the user's own Stop in
                # the digest text.
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

            # A completed run that made no tool call cannot have touched the disk,
            # so it carries the claim note in the event the parent reads its
            # narration from. A lone run or a single wave gets no synthesis turn,
            # so this event is the only place the parent can be told before it
            # reports the work to the user. Stops and failures say nothing: their
            # prose already tells the parent not to treat the output as finished.
            no_tools_note = ""
            if single_outcome == OUTCOME_OK and not _flush_only and info.made_no_tool_calls:
                no_tools_note = f"⚠ Agent `{info.id}` {NO_TOOL_CALLS_NOTE}"
            announce = (
                f"{SUBAGENT_COMPLETION_PREFIX}\n"
                f"Agent `{info.id}`"
                f"{f' ({info.agent})' if info.agent else ''}"
                f" {status} {emoji}\n"
                f"Task: {task_text}\n\n"
                f"Usage: {usage}\n\n"
                f"{detail}" + (f"\n\n{no_tools_note}" if no_tools_note else "")
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
            # The one await on the wave-accounting path runs BEFORE this
            # member touches the wave record. Everything from reading
            # ``_batch_progress`` to the hold-or-flush decision below is then a
            # single synchronous block, so a sibling completion can never close
            # the wave between this member writing its result line and deciding
            # how that line is delivered: the closer's settle set always carries
            # every member its digest names. While this member awaits, its own
            # report hold is still armed, so no sibling closes the wave on it.
            # The lookup is skipped when this member's count alone closes the
            # wave; nothing can interleave between the peek and the block.
            _no_batch_pending = False
            if _batch_id and not _flush_only and self.subagent_mgr:
                _peek = self._batch_progress.get(_batch_id)
                _peek_done = _peek["done"] if _peek is not None else 0
                if not (_batch_total > 0 and _peek_done + 1 >= _batch_total):
                    try:
                        _members = await _subagent_batch_pending(self.subagent_mgr, _batch_id)
                        _no_batch_pending = not _members
                    except Exception:
                        _no_batch_pending = False
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
                            # Failed members a memory wait ended before they
                            # started: dropped work the parent may resubmit. A
                            # refusal is not counted; resubmitting it would only
                            # be refused again.
                            "not_run": 0,
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
                # This member's report has been consumed: its contribution is
                # about to land in the wave's done-count, so it stops holding
                # the wave-close fallback open (batch_reports_in_flight).
                # Release in the same synchronous block as the
                # increment so the hold and the count can never be observed
                # apart. The release goes to the manager-level registry -- NOT
                # an agent-record flag -- so an operator clear (DELETE
                # /api/spawn) popping this member from ``_agents`` mid-flight
                # cannot have dropped the hold before this line lands it.
                if self.subagent_mgr is not None:
                    self.subagent_mgr.consume_report_hold(_batch_id, info.id)
                bp["done"] += 1
                _oc = info.outcome
                if _oc == "stopped":
                    bp["stopped"] += 1
                elif _oc == "failed":
                    bp["err"] += 1
                    if (info.error or "").startswith(NEVER_STARTED_MEMORY_PREFIX):
                        bp["not_run"] += 1
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
                    # A run that kept its output after a generate failure is
                    # completed but partial; the digest must say so.
                    _kept = " (partial: backend failed to generate the final response)"
                    bp["ok_lines"].append(
                        f"— `{info.id}` ✅{_kept if info.partial else ''} {task_text[:80]}"
                        f"{_model_tag} · {usage}"
                        + (f"\n  → {result_path}" if result_path else "")
                        + (f"\n  {no_tools_note}" if no_tools_note else "")
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
                    #
                    # A member whose ``done`` flag has flipped but whose
                    # terminal report has not reached this consumer yet is
                    # OUTSTANDING too: ``batch_members_pending`` does not
                    # count it while its contribution to bp["done"] is still
                    # in flight, so without the reports-in-flight check a
                    # sibling landing in that window finalizes the wave early
                    # and the in-flight report then finalizes it again.
                    #
                    # The membership lookup ran before this block (see
                    # ``_no_batch_pending``); the in-flight read is synchronous
                    # and runs after this member released its own hold above.
                    try:
                        _last = bool(
                            self.subagent_mgr
                            and _no_batch_pending
                            and not self.subagent_mgr.batch_reports_in_flight(_batch_id)
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
                        # Monotonic, as the sweep's ``now`` is: a wall-clock step
                        # must not force a partial digest out, or hold one past
                        # its deadline.
                        info._digest_held_at = time.monotonic()
                        bp.setdefault("held_infos", []).append(info)
                        if _oc == "completed":
                            bp["held_ok_deliveries"].append(
                                SubagentDelivery(info.id, info.elapsed, info.credits)
                            )
                        elif getattr(info, "_report_owed", False) is True:
                            # A memory-wait expiry the store owes a report until
                            # it reaches the parent: the debt rides with this
                            # chunk, and the settle that delivers the chunk
                            # clears it. Held only here, a restart before the
                            # flush leaves it owed for the next start.
                            bp["held_ok_deliveries"].append(
                                SubagentDelivery(
                                    info.id, info.elapsed, info.credits, report_owed=True
                                )
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
                        # Members a memory wait ended are dropped work, not
                        # done work. After the tally sentence, which the card
                        # regexes anchor on. "Once memory eases": a member the
                        # spent episode ended at once is counted too, and a
                        # resubmit while that episode lasts is ended at once.
                        _not_run_line = (
                            f"{bp['not_run']} of the ❌ never started, so their "
                            f"work was not done: resubmit them once memory "
                            f"eases if it is still needed. "
                            if bp.get("not_run")
                            else ""
                        )
                        if _nested_live:
                            _completion_line = (
                                f"These {bp['total']} sub-agents finished: "
                                f"{bp['ok']} ✅ · {bp['err']} ❌ · "
                                f"{bp['stopped']} ⏹. {_not_run_line}"
                                f"Their results are below. "
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
                                f"{_not_run_line}"
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
                    # When this is the LAST outstanding sub-agent for the parent,
                    # flag the slot so that once every completion has been
                    # processed and the queue drains, _run_chat fires ONE
                    # dedicated synthesis turn (see chat_runner drain/idle branch).
                    # The fire gate drops it when one completion turn carried the
                    # whole batch (slot._synthesis_completion_turns == 1).
                    # Ordering guarantees running_agents_for == [] here on the last
                    # agent (info.done set + _running_count decremented first).
                    # IN MEMORY ONLY, cheapest first, and with no await: the arm
                    # sits on the delivery path, so a store read here would queue
                    # every completion behind the writer, and an await would let
                    # the tab close or a sibling register under it. The store
                    # half (a sibling the gate still holds) is the fire gate's
                    # (chat_utils.synthesis_fire_verdict), which refuses while
                    # anything is queued; the other completions re-arm.
                    try:
                        _mgr = self.subagent_mgr
                        _arm_synthesis = (
                            _mgr is not None
                            and not _injection_slot._pending_synthesis
                            and not _flush_only
                            and info.id not in _injection_slot._subagents_inline_collected
                            and _mgr.running_agents_for(parent_key) == []
                            and not _mgr.has_in_memory_pending_work_for(
                                parent_key, exclude_id=info.id
                            )
                        )
                    except Exception:
                        _arm_synthesis = False  # error → don't arm (fail safe)
                    if _arm_synthesis:
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
                        # The blocking tool's return value carried it.
                        info._report_delivered = True
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
                                    info._report_delivered = True
                                    return
                                logger.info(
                                    "Subagent %s: slot %s claimed by another injection, queuing",
                                    info.id,
                                    _slot_name,
                                )
                                # Each queued completion drains as its own turn
                                # (a system injection never merges), so it is
                                # one turn for the synthesis fire gate's count.
                                _injection_slot._synthesis_completion_turns += 1
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
                            info._report_delivered = True
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
                        # failure tombstone, not this ledger. A memory-wait
                        # expiry is the exception: it has no tombstone, and the
                        # store owes its report until this turn consumes it.
                        _owes_delivery = bool(info._digest_settle_deliveries) or (
                            not _flush_only
                            and (
                                info.outcome == "completed"
                                or getattr(info, "_report_owed", False) is True
                            )
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
                        # One completion turn for the synthesis fire gate's
                        # count, as in the queued branch above.
                        _injection_slot._synthesis_completion_turns += 1
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
                                ),
                                state=self.dashboard_state,
                                slot=_injection_slot,
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
                    # The notification is this route's whole delivery.
                    info._report_delivered = True
                return

            if parent_key and not parent_key.startswith(("cron:", "subagent:")):
                # Parent with no tab to stream into — inject silently into the
                # parent's ACP session, then deliver only the synthesized reply
                # to the conversation. Make up to _MAX_INJECT_ATTEMPTS attempts;
                # only a timeout starts the next one (_inject_with_retry has its
                # own ACP-error retry inside each attempt). Slack keeps its
                # dedicated rich posting; every other channel namespace
                # (Telegram, Discord, …) delivers through the governed transport
                # ladder — its stored channel value is not a Slack channel id,
                # so posting it through the Slack client can never reach the
                # user. A parent with neither a channel namespace nor a Slack
                # thread (a taskrunner: session, say) is still injected, but its
                # reply goes to no channel.
                assert self.sessions is not None
                _namespace = channel_namespace_of(parent_key)
                _via_transport = bool(_namespace) and _namespace != SLACK_NAMESPACE
                _slack_thread = self.sessions.get_thread(parent_key)
                if _slack_thread is None and is_legacy_slack_key(parent_key):
                    _slack_thread = parent_key
                _via_slack = _namespace == SLACK_NAMESPACE or bool(_slack_thread)
                if _via_transport:
                    _inject_label = _namespace
                elif _via_slack:
                    _inject_label = "Slack"
                else:
                    _inject_label = "session"
                # Snapshot the transport reply link BEFORE the injection retry
                # loop: the timeout path's sessions.reset() evicts the session's
                # in-memory origin link, the first rung _channel_reply_link
                # tries, so resolving after a retry could miss the
                # conversation's own thread/forum target.
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
                            if response and _via_slack and self.slack and self._owner_id:
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
                                        await self.slack.post_message(channel, part, _slack_thread)
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
                                            _slack_thread,
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
                    # Swallowed with no failure notice, so the record is the only
                    # place that says the parent was never told.
                    info._report_undelivered = True
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
                # No conversation to inject into: the notification is this
                # route's whole delivery. A cron parent acknowledges in
                # ``_inject_with_retry`` instead, and only when it lands.
                info._report_delivered = True

        assert self.sessions is not None
        assert self.ctx_builder is not None

        def _is_yolo() -> bool:
            return safety_override().is_active()

        def _spawn_slot_resolver(request_id: str) -> str:
            """Resolve slot from spawn request_id (spawn:{agent_id})."""
            agent_id = request_id.removeprefix("spawn:")
            info = self.subagent_mgr.get(agent_id) if self.subagent_mgr is not None else None
            # A workflow step's spawn resolves to the run's origin chat.
            slot = _spawn_parent_slot(
                info.parent_session_key if info else "",
                getattr(self.dashboard_state, "workflow_service", None),
            )
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

        # Debounced slots push: keep slots[].subagents_running and
        # slots[].subagents_queued live for every
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

        def _tab_queued_depth(parent_key: str, slot_name: str, extra: dict) -> object:
            """The queued depth a ``subagent_queued`` frame carries: its tab's sum.

            Several parents can route to one tab (a cron tab's ``cron:<job>:<run>``
            and ``cron:<job>:<agent>`` runs). The client reducer sets the tab's
            count from each frame, and the slot list sums the published depths by
            tab, so a frame carrying only its own parent's depth would disagree
            with the next slots push and the count would flip between the two.
            The frame therefore carries this parent's depth plus every other
            parent's published depth on the same tab. A non-int depth is passed
            through unchanged for the client to ignore.
            """
            own = extra.get("queued")
            if not isinstance(own, int) or isinstance(own, bool) or not self.subagent_mgr:
                return own
            total = max(own, 0)
            for other, depth in self.subagent_mgr.published_queued_depths().items():
                if other != parent_key and _event_slot(other) == slot_name:
                    total += depth
            return total

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
            if etype == "subagent_queued":
                extra = {
                    **extra,
                    "queued": _tab_queued_depth(info.parent_session_key, slot_name, extra),
                }
            if etype == "subagent_injection_failed":
                # Show error in UI + queue for LLM context on next turn.
                slot = self.dashboard_state.get_slot(slot_name)
                if slot is not None and getattr(slot, "window_pending", False) is True:
                    # A parent tab restored as a sidebar row loads its window
                    # before the card is appended, with the read off the loop.
                    await load_window_off_loop(slot)
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
                        # A completed run keeps no delivered tombstone while its
                        # report is undelivered, and the drain that hands this
                        # notice to the parent writes it, keyed on the text.
                        if info.outcome == "completed":
                            try:
                                slot.note_pending_subagent_delivery(
                                    failure_msg,
                                    [SubagentDelivery(info.id, info.elapsed, info.credits)],
                                )
                            except Exception:
                                logger.debug(
                                    "Subagent %s: could not record the notice's delivery",
                                    info.id,
                                    exc_info=True,
                                )
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
                #
                # A child the reaper flagged stalled does not hold the parent's
                # user messages (``subagents_hold_user_messages``), yet nothing
                # else starts that drain: a message queued BEFORE the flag is set
                # stays queued until the next send or the wall-clock reap. Kick
                # the parent slot's drain here — before the coalescer can absorb
                # the UI frame — so the parked message starts now. The drain
                # re-reads the hold under the slot lock, so a live sibling keeps
                # the queue parked and a running slot is left alone.
                if etype == "subagent_stalled" and extra.get("stalled"):
                    _drain = asyncio.ensure_future(
                        drain_idle_parent_queue_for_stall(
                            self.dashboard_state, info.parent_session_key
                        )
                    )
                    self.dashboard_state._background_tasks.add(_drain)
                    _drain.add_done_callback(self.dashboard_state._background_tasks.discard)
                if self._subagent_coalescer().handle(etype, {**base, **extra}):
                    return
                self.dashboard_state.broadcast_ws(etype, {**base, **extra})
                # subagents_running flips truth value exactly at spawn/done, and
                # subagents_queued changes with every queued-depth frame — push
                # (debounced) so slots-stream consumers stay live, and a client
                # that missed a depth frame is corrected by the next push.
                if etype in ("subagent_spawn", "subagent_done", "subagent_queued"):
                    _schedule_slots_push()

        async def _orphan_notify(
            parent_session: str,
            msg: str,
            meta: dict | None = None,
            delivery: SubagentDelivery | None = None,
        ) -> bool | str:
            """Inject an orphan notification into the parent dashboard slot.

            Mirrors the subagent_injection_failed delivery: visible transcript
            card + queue into ``slot._pending_subagent_failures`` so the LLM
            drains it (as a digest with any other pending failures) on its next
            turn. Returns False when the slot no longer exists so the manager
            falls through to the owner-DM path. ``msg`` is redacted by the
            manager before delivery; re-redact defensively anyway.

            ``meta`` carries the structured completion facts so the
            orphan row renders as a card without re-parsing its prose header.
            *delivery* is the tombstone the notice owes. It is recorded on the
            slot's ledger under the queued text, for the drain of that text to
            write, and ``ORPHAN_NOTICE_OWED`` tells the manager it was taken.
            """
            # Deliver to whichever tab shows the parent conversation, including a
            # channel-born one; only a parent with no tab wants the owner DM.
            slot_name = dashboard_slot_key(parent_session)
            if not self.dashboard_state or not slot_name:
                return False
            slot = self.dashboard_state.get_slot(slot_name)
            if slot is not None and getattr(slot, "window_pending", False) is True:
                # A parent tab restored as a sidebar row loads its window before
                # the notice is appended, with the read off the loop.
                await load_window_off_loop(slot)
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
            owed = False
            if delivery is not None:
                try:
                    slot.note_pending_subagent_delivery(safe_msg, [delivery])
                    owed = True
                except Exception:
                    logger.debug(
                        "Could not record orphan %s's notice delivery",
                        delivery.agent_id,
                        exc_info=True,
                    )
            self.dashboard_state.push_slots_update()
            logger.info("Orphan notification injected into slot %s", slot_name)
            return ORPHAN_NOTICE_OWED if owed else True

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
            # Rows that survived the restart are claimed by the manager's pump,
            # which the reaper start below kicks as soon as the loop yields --
            # that yield is ``run()``'s memory barrier. Hold the pump until
            # ``_start_subagent_dispatch_after_memory_ready`` opens it.
            defer_queue_dispatch=True,
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

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    @update_ownership.owning(update_ownership.Step.RESTART)
    async def _restart_after_update(self, respawn: Callable[[], str]) -> None:
        """Save state and restart only after callback/refusal work is durable.

        ``respawn`` is loaded before apply can replace the environment. If the
        pre-fence drain cannot finish, retain it and retry the restart in five
        minutes with admission reopened; never force through accepted work.

        The interpreter is established BEFORE any of that. When it is missing, this
        returns without saving, fencing or draining, because an exec that cannot
        succeed must not be reached after every session has been closed.

        Owns a missing bundle while it runs (``update_ownership``), so the
        stale-asset watchdog does not race its teardown. A deferral while
        callback work drains is owned for a while too, from the first deferral
        on (a retry does not extend it), so the watchdog does not force the
        restart it just put off.
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
            # Not a deferral the watchdog waits out: with the tree pruned the
            # bundle is gone too, and its exit lets the supervisor relaunch
            # through its own command, which a retry here cannot do. So one an
            # earlier drain armed ends here too.
            update_ownership.clear_restart_deferral()
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
        # ONE restart sequence per process: the dashboard's ``_restart_gateway``
        # claims the same flag, and two sequences must never both drain sessions
        # and race separate successors. A restart already under way owns the
        # exec; this one stays pending and retries if that one is refused.
        state = self.dashboard_state
        if state is not None and getattr(state, "_gateway_restart_in_progress", False) is True:
            self._update_apply_deferred = True
            logger.info("A gateway restart is already in progress; the update restart waits")
            return
        if state is not None:
            state._gateway_restart_in_progress = True
        try:
            # Through the class, not ``self``: the restart is also driven with a
            # stand-in ``self`` that carries only the state it reads.
            await GatewayOrchestrator._restart_after_update_claimed(self, launcher, exe)
        finally:
            if state is not None:
                state._gateway_restart_in_progress = False

    # gateway_runtime/install_repair.py
    _DEP_INSTALL_TIMEOUT_SECS = _install._DEP_INSTALL_TIMEOUT_SECS

    _KIRO_CLI_VERSION_TIMEOUT_SECS = _install._KIRO_CLI_VERSION_TIMEOUT_SECS

    _STARTUP_CHILD_REAP_SECS = _install._STARTUP_CHILD_REAP_SECS

    _kill_startup_child = staticmethod(_install._kill_startup_child)

    _reap_startup_child = classmethod(_install._reap_startup_child)

    _check_missing_deps = _install._check_missing_deps

    _check_console_script = _install._check_console_script

    _schedule_console_script_repair = _install._schedule_console_script_repair

    _warn_if_kiro_cli_outdated = _install._warn_if_kiro_cli_outdated

    # gateway_runtime/update_schedule.py
    _in_flight_work_counts = _upd_sched._in_flight_work_counts

    _count_in_flight_work = _upd_sched._count_in_flight_work

    _live_update_handler_tasks = _upd_sched._live_update_handler_tasks

    _drain_update_callback_work = _upd_sched._drain_update_callback_work

    _UPDATE_BUSY_RETRY_SECS = _upd_sched._UPDATE_BUSY_RETRY_SECS

    _MANDATORY_UPDATE_MAX_DEFER_SECS = _upd_sched._MANDATORY_UPDATE_MAX_DEFER_SECS

    _UPDATE_DRAIN_TIMEOUT_SECS = _upd_sched._UPDATE_DRAIN_TIMEOUT_SECS

    _prepare_auto_update_apply = _upd_sched._prepare_auto_update_apply

    _finish_auto_update_apply = _upd_sched._finish_auto_update_apply

    _wheel_apply_module = staticmethod(_upd_sched._wheel_apply_module)

    _dashboard_policy_applies = staticmethod(_upd_sched._dashboard_policy_applies)

    _settle_update_work = _upd_sched._settle_update_work

    _run_update_checks = _upd_sched._run_update_checks

    _retry_pending_update_restart = _upd_sched._retry_pending_update_restart

    _check_for_updates = _upd_sched._check_for_updates

    _auto_update_effect = _upd_sched._auto_update_effect

    _publish_provider_update_state = _upd_sched._publish_provider_update_state

    _check_for_updates_via_provider = _upd_sched._check_for_updates_via_provider

    _restart_after_update_claimed = _upd_sched._restart_after_update_claimed

    _check_for_updates_legacy = _upd_sched._check_for_updates_legacy

    # gateway_runtime/update_apply.py
    _auto_apply_update = _upd_apply._auto_apply_update

    _auto_apply_wheel_update = _upd_apply._auto_apply_wheel_update

    _notice_wheel_update_once = _upd_apply._notice_wheel_update_once

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    # gateway_runtime/channel_lifecycle.py
    _connect_slack = _channel_lifecycle._connect_slack

    # gateway_runtime/exit_path.py
    _shutdown = _exit._shutdown

    _write_marker_worker = _exit._write_marker_worker

    _install_shutdown_signal_handlers = _exit._install_shutdown_signal_handlers

    _shutdown_and_exit = _exit._shutdown_and_exit

    # gateway_runtime/service_boot.py
    _auto_open_dashboard = _boot._auto_open_dashboard

    _init_services = _boot._init_services

    _notify_consolidation_abandoned = _boot._notify_consolidation_abandoned

    _schedule_session_start_sizing = _boot._schedule_session_start_sizing

    _log_session_start_sizing = _boot._log_session_start_sizing

    _init_task_runner = _boot._init_task_runner

    _init_dashboard = _boot._init_dashboard

    _init_api_server = _boot._init_api_server

    run = _boot.run

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

    def _on_session_environment_config_change(self, change: ConfigChange) -> None:
        if self._session_environments_ready and not self._test_mode:
            _start_coder_retention(change.new)

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
                "session_environments.coder_url",
                "session_environments.fargate_profiles",
                callback=self._on_session_environment_config_change,
                name="GatewayOrchestrator.session_environments",
            ),
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
            # Its own subscription: a deferral is merged per subscription, so a
            # discarded ``agent`` section is retried without keeping the Slack
            # paths stale beside it.
            live.subscribe(
                "agent.default_agent",
                callback=self._on_default_agent_config_change,
                name="GatewayOrchestrator.default_agent",
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

    _on_default_agent_config_change = _channel_lifecycle._on_default_agent_config_change

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
        _install,
        _upd_sched,
        _upd_apply,
        _nudge_wiring,
        _nudge_dash,
        _nudge_chan,
        _approvals,
        _boot,
        _heartbeat,
        _egress,
        _exit,
        _entry,
        _cron_fire,
        _cron_exec,
        _cron_turn,
        _cron_delivery,
        _cron_failure,
    ),
)


# Strong reference to the background slice-limit apply task: the event loop
# holds tasks weakly, so a fire-and-forget create_task with no reference can
# be garbage-collected mid-flight.
_SLICE_LIMITS_TASK: "asyncio.Task[None] | None" = None

# Strong reference to the background KAS kiro-cli floor check, for the same reason.
_KAS_CLI_FLOOR_TASK: "asyncio.Task[None] | None" = None


# Strong ref to the fire-and-forget agents-dir janitor sweep launched at boot
# (the loop holds tasks weakly, so without this it could be GC'd mid-flight).
_AGENTS_JANITOR_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the liveness-keyed agent-scratch sweep loop.
_AGENT_SCRATCH_SWEEP_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the kiro-cli log cap loop.
_KIRO_CLI_LOG_CAP_TASK: "asyncio.Task[None] | None" = None
#: Strong ref for the archived-session workspace retention sweeper.
_CODER_RETENTION_TASK: "asyncio.Task[None] | None" = None
