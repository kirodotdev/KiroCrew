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
``gateway_runtime``: the orchestrator's construction and each channel's boot,
tool-approval policy and the approval callbacks, the sub-agent manager with its
completion report, wave digest and delivery into the parent, the runtime-death
charge, the cron callback with its command, script and agent-turn arms, cron
dispatch and run tallies, cron result delivery and failure alerts, delivery
routing and the result egress legs, the channel lifecycle, the MCP broker, the
memory lifecycle, service construction and ``run``, the heartbeat, admission,
boot-time install repair, the update schedule and apply, AutoNudge's wiring and
fire paths, the exit path, and the process entry point. This module stays their
only import path and patch surface, and their functions run on its globals, so an
import no function left here reads is still an owner's global or part of the
module's name surface (``test_gateway_runtime_bindings.py`` checks each is read).
What stays here, and why, is recorded in
``docs/system-specs/modules/slack-gateway.md`` (Composition).
"""

from __future__ import annotations

# Many imports here are the gateway_runtime owners' globals, so setup.cfg turns F401 off for
# this file: removing one fails test_every_global_an_owner_function_reads_is_bound_on_the_facade,
# not a call at runtime, and test_gateway_runtime_bindings.py checks that each import is read.
import asyncio
import contextlib
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
from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.acp.client import AcpError, AcpProcessDied
from kiro_crew.agent_sdk import AgentTurnUsage
from kiro_crew.agent_sdk.mcp_problems import (
    UNATTENDED_MCP_WAIT_SECS,
    declared_mcp_not_ready_note,
    wait_for_declared_mcp,
)
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
    LEAKED_TOOL_CALL_NOTE,
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
from kiro_crew.gateway_shutdown_budget import (
    GRACEFUL_SHUTDOWN_SECS,
    PERSISTENCE_DRAIN_GRACE_SECS,
    UPDATE_INSTALLER_STOP_SECS,
)
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
from kiro_crew.slack.client import RealSlackClient
from kiro_crew.slack.format import (
    build_cron_ack_block,
    build_options_blocks,
    escape_mrkdwn,
    extract_options,
    render_for_slack,
)
from kiro_crew.slack.gateway_runtime import admission as _admission
from kiro_crew.slack.gateway_runtime import channel_boot as _channel_boot
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
from kiro_crew.slack.gateway_runtime import orchestrator_init as _orch_init
from kiro_crew.slack.gateway_runtime import process_entry as _entry
from kiro_crew.slack.gateway_runtime import result_egress as _egress
from kiro_crew.slack.gateway_runtime import runtime_charge as _runtime_charge
from kiro_crew.slack.gateway_runtime import service_boot as _boot
from kiro_crew.slack.gateway_runtime import subagent_injection as _subagent_injection
from kiro_crew.slack.gateway_runtime import subagent_report as _subagent_report
from kiro_crew.slack.gateway_runtime import subagent_wiring as _subagent_wiring
from kiro_crew.slack.gateway_runtime import tool_policy as _tool_policy
from kiro_crew.slack.gateway_runtime import turn_approvals as _approvals
from kiro_crew.slack.gateway_runtime import update_apply as _upd_apply
from kiro_crew.slack.gateway_runtime import update_schedule as _upd_sched
from kiro_crew.slack.gateway_runtime.channel_lifecycle import (
    _channel_transport_permitted,
    _push_observe_limits,
)
from kiro_crew.slack.gateway_runtime.cron_delivery import (
    _CRON_MSG_LIMIT,
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
from kiro_crew.slack.gateway_runtime.exit_path import (
    _MARKER_WRITE_WAIT_SECS,
)
from kiro_crew.slack.gateway_runtime.heartbeat_turns import (
    _HEARTBEAT_KEEP_INJECTION,
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
    _SIGNING_KEY_PREFLIGHT_TIMEOUT_SECS,
    BOOT_RESTART_HINT,
    _kas_floor_gates_spawns,
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
from kiro_crew.slack.gateway_runtime.runtime_charge import (
    _apply_gate_verdict,
    _charge_failure,
    _cron_failure_attribution,
)
from kiro_crew.slack.gateway_runtime.service_boot import (
    _PROFILE_PROJECTION_POLL_S,
)
from kiro_crew.slack.gateway_runtime.subagent_injection import (
    _MAX_INJECT_ATTEMPTS,
    _injection_slot_busy,
)
from kiro_crew.slack.gateway_runtime.subagent_report import (
    SUBAGENT_DIGEST_CHUNK_SIZE,
    _compose_subagent_completion,
    _digest_chunk_size,
    _SubagentReport,
    _WaveDigest,
)
from kiro_crew.slack.gateway_runtime.subagent_wiring import (
    _WORKFLOW_STEP_PREFIXES,
    _spawn_parent_slot,
    _subagent_batch_pending,
    _subagent_queued_count,
    _subagent_work_pending,
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
    from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
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


logger = logging.getLogger(__name__)


# Full chat turn timeout — tool calls, multi-step reasoning, spawning.
# More generous than INJECTION_TIMEOUT (default 900s, tunable via
# KIROCREW_INJECTION_TIMEOUT) which only covers a single injected continuation turn.


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
    # The constructor's fields (gateway_runtime/orchestrator_init.py):
    _cfg: KiroCrewConfig
    _no_dashboard: bool
    _no_crons: bool
    _no_open: bool
    _port_override: str | None
    _json_ready: bool
    _approval_mode: str | None
    _test_mode: bool
    _boot_kiro_service: KiroPrerequisiteService | None
    _app_token: str
    _bot_token: str
    _owner_id: str
    _allowed_users: set[str]
    _tracking_channels: set[str]
    _open_channels: set[str]
    _slack_enabled: bool
    slack_command: str
    _channel_restart_lock: asyncio.Lock
    _channel_transports_started: bool
    _channel_restart_tasks: set[asyncio.Task[None]]
    _channel_restart_gen: dict[str, int]
    _session_environments_ready: bool
    _config_subs: list[Subscription]
    slack: RealSlackClient | None
    sessions: SessionManager | None
    ctx_builder: ContextBuilder | None
    conv_log: ConversationLog | None
    consolidator: HistoryConsolidator | None
    cron_svc: CronService | None
    _cron_reconciled: bool
    _cron_armed: bool
    heartbeat_svc: HeartbeatService | None
    autonudge_svc: AutoNudgeService | None
    secretary_svc: object | None
    subagent_mgr: SubagentManager | None
    _adaptive_controller: AdaptiveController | None
    _subagent_coalescer_inst: "SubagentEventCoalescer | None"
    _batch_progress: dict[str, dict]
    _cron_injecting: dict[str, int]
    _running_script_ids: set[str]
    task_runner: TaskRunner | None
    _runner_admission: Any
    _runner_admission_adopted: bool
    channel_history: ChannelHistory | None
    dashboard_state: DashboardState | None
    _background_tasks: set[asyncio.Task]
    _mcp_launch_approval_ready: asyncio.Event
    _memory_startup: MemoryStartup | None
    _memory_startup_task: asyncio.Task | None
    _memory_repair_task: asyncio.Task | None
    _memory_repair_stop: threading.Event
    _memory_repair_cursor: str
    _console_script_repair_task: "asyncio.Task[None] | None"
    _marker_write_task: "asyncio.Task[None] | None"
    _sign_in_refresher_task: "asyncio.Task[None] | None"
    _marker_clear_pending: threading.Event
    _dashboard_runner: web.AppRunner | None
    _handler_tasks: set[asyncio.Task]
    _session_tasks: dict[str, asyncio.Task]
    _pending_queue: dict[str, list]
    _socket_client: WSSocketModeClient | None
    _wecom_client: "WeComClient | None"
    _channel_handles: dict[str, object]
    _inbound_replay_task: "asyncio.Task[None] | None"
    _model_download_task: "asyncio.Task[bool] | None"
    _feature_video_task: "asyncio.Task[bool] | None"
    _auto_migrate_task: "asyncio.Task[None] | None"
    _update_check_task: "asyncio.Task[None] | None"
    _profile_watch_task: "asyncio.Task[None] | None"
    _update_apply_deferred: bool
    _pending_update_respawn: Callable[[], str] | None
    _pending_update_mandatory: bool
    _pending_update_mandatory_key: str
    _mandatory_update_deferred_at: float | None
    _mandatory_update_deferred_key: str | None
    _wheel_update_noticed: set[str]
    _mcp_gateway_manager: GatewayManager | None
    _mcp_resolve_prefetch: asyncio.Task[None] | None
    _mcp_target_env: dict[str, str]
    _mcp_overlay_refresh_lock: asyncio.Lock | None = None
    _mcp_rewrite_inputs: dict[str, Any] | None = None
    _mcp_rewrite_policy: dict[str, Any] | None = None
    _mcp_resolve_home: str
    # The channel hoists' fields (gateway_runtime/channel_boot.py):
    _wecom_bot_id: str
    _wecom_secret: str
    _wecom_enabled: bool
    _telegram_bot_token: str
    _telegram_enabled: bool
    _telegram_allowed_user_ids: list[int]
    _telegram_allow_forum: bool
    _telegram_allowed_forum_chat_ids: list[int]
    _telegram_client: "TelegramClient | None"
    _weixin_token: str
    _weixin_account_id: str
    _weixin_base_url: str
    _weixin_dm_policy: str
    _weixin_allowed_user_ids: list[str]
    _weixin_enabled: bool
    _weixin_client: "WeixinClient | None"
    _whatsapp_enabled: bool
    _whatsapp_client: "WhatsAppClient | None"
    _feishu_app_id: str
    _feishu_app_secret: str
    _feishu_enabled: bool
    _feishu_allowed_open_ids: list[str]
    _feishu_allow_group: bool
    _feishu_allowed_group_ids: list[str]
    _discord_bot_token: str
    _discord_enabled: bool
    _discord_allowed_user_ids: list[str]
    _discord_allowed_thread_ids: list[str]
    _discord_allowed_channel_ids: list[str]
    _discord_auto_thread: bool
    _discord_client: "DiscordClient | None"
    _webex_bot_token: str
    _webex_enabled: bool
    _webex_allowed_emails: list[str]
    _webex_client: "WebexClient | None"
    _imessage_enabled: bool
    _imessage_client: "IMessageClient | None"
    _teams_app_id: str
    _teams_app_password: str
    _teams_tenant_id: str
    _teams_enabled: bool
    _teams_allowed_emails: list[str]
    _teams_client: "TeamsClient | None"

    # gateway_runtime/orchestrator_init.py
    if TYPE_CHECKING:
        # The type checker reads an assignment to ``__init__`` as an incompatible
        # override of ``object.__init__``; this is the owner's signature.
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
        ) -> None: ...

    else:
        __init__ = _orch_init.__init__

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
    _announce_cron_quarantine = _cron_fire._announce_cron_quarantine
    _start_cron_after_memory_ready = _cron_fire._start_cron_after_memory_ready
    # gateway_runtime/cron_exec.py
    _cron_run_command = _cron_exec._cron_run_command
    _deliver_script_result = _cron_exec._deliver_script_result
    _cron_run_script = _cron_exec._cron_run_script
    _live_internal_secret = _cron_exec._live_internal_secret
    # gateway_runtime/cron_turn.py
    _cron_run_sequence = _cron_turn._cron_run_sequence
    _cron_run_single = _cron_turn._cron_run_single
    # gateway_runtime/cron_delivery.py
    _deliver_cron_agent_result = _cron_delivery._deliver_cron_agent_result
    # gateway_runtime/heartbeat_turns.py
    _init_heartbeat = _heartbeat._init_heartbeat
    # gateway_runtime/nudge_channel_fire.py
    _fire_slack_nudge = _nudge_chan._fire_slack_nudge
    _fire_slack_inner_nudge = _nudge_chan._fire_slack_inner_nudge
    _bookkeeping_section = _nudge_chan._bookkeeping_section
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
    # gateway_runtime/subagent_injection.py
    _defer_queued_delivery = staticmethod(_subagent_injection._defer_queued_delivery)
    _route_subagent_to_tab = _subagent_injection._route_subagent_to_tab
    _inject_subagent_into_channel_parent = _subagent_injection._inject_subagent_into_channel_parent
    _finish_subagent_cron_or_notify = _subagent_injection._finish_subagent_cron_or_notify

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
    # gateway_runtime/subagent_wiring.py
    _subagent_coalescer = _subagent_wiring._subagent_coalescer
    _init_subagents = _subagent_wiring._init_subagents
    # gateway_runtime/subagent_report.py
    _subagent_wave_digest = _subagent_report._subagent_wave_digest
    # gateway_runtime/runtime_charge.py
    _inject_with_retry = _runtime_charge._inject_with_retry
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
    _refresh_mcp_overlays = _mcp_broker._refresh_mcp_overlays
    _wire_mcp_gateway_dashboard = _mcp_broker._wire_mcp_gateway_dashboard

    # ------------------------------------------------------------------
    # Auto-update
    # ------------------------------------------------------------------

    @update_ownership.owning(update_ownership.Step.RESTART)
    async def _restart_after_update(self, respawn: Callable[[], str]) -> None:
        """Restart into an applied update, owning the restart step while it runs."""
        await GatewayOrchestrator._restart_after_update_body(self, respawn)

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
    _restart_after_update_body = _upd_sched._restart_after_update_body
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
    _stop_services_and_sessions = _exit._stop_services_and_sessions
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
    # gateway_runtime/channel_boot.py
    _hoist_wecom = _channel_boot._hoist_wecom
    _hoist_telegram = _channel_boot._hoist_telegram
    _hoist_weixin = _channel_boot._hoist_weixin
    _hoist_whatsapp = _channel_boot._hoist_whatsapp
    _hoist_feishu = _channel_boot._hoist_feishu
    _hoist_discord = _channel_boot._hoist_discord
    _hoist_webex = _channel_boot._hoist_webex
    _hoist_imessage = _channel_boot._hoist_imessage
    _hoist_teams = _channel_boot._hoist_teams
    _SLACK_APPLIER_PREFIXES = _channel_boot._SLACK_APPLIER_PREFIXES
    _on_session_environment_config_change = _channel_boot._on_session_environment_config_change
    _register_config_appliers = _channel_boot._register_config_appliers
    _start_channel_transports = _channel_boot._start_channel_transports

    # ── live config: per-channel restart + Slack hot fields ─────────────

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
    # gateway_runtime/channel_lifecycle.py
    _schedule_inbound_replay = _channel_lifecycle._schedule_inbound_replay
    _replay_spooled_inbound = _channel_lifecycle._replay_spooled_inbound
    _badge_unready_channels = _channel_lifecycle._badge_unready_channels


# Every function the owners define runs on this module's globals, so a patch of
# ``kiro_crew.slack.gateway.<name>`` reaches it wherever it now lives; see
# ``gateway_runtime``. Run once, after the class has bound the owner methods.
_facade_compose(
    globals(),
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
        _orch_init,
        _subagent_wiring,
        _subagent_report,
        _subagent_injection,
        _runtime_charge,
        _channel_boot,
    ),
    host=GatewayOrchestrator,
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
