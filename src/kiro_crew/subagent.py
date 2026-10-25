"""Subagent orchestration — spawn isolated background agents.

Each subagent gets its own LLM session (via SessionManager) with a
focused system prompt.  Results are announced back to the caller via
a callback.  Max concurrent limit prevents resource exhaustion.

Nested spawns are supported: a subagent may spawn its own subagents, and a
nested spawn is tracked apart from the parent's wave, so its children are not
counted against that wave's completion total.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import math
import os
import threading
import time
from collections.abc import (
    Awaitable,
    Callable,
    Container,
    Iterable,
    Iterator,
    Mapping,
    Sequence,
)
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, AbstractSet, Any, Literal, Optional

from kiro_crew._facade_compose import compose as _facade_compose
from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    InFlightToolTracker,
    LivenessOracle,
    ToolCallState,
    boottime_now,
    consult_offloaded,
)
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import PROVIDER_LABEL_CLAUDE, PROVIDER_LABEL_DEFAULT
from kiro_crew.agent_sdk.drivers.acp_vocab import (  # noqa: F401 - STOP_* resolved by run_turn.py/run_yields.py via bind_component_globals
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_SUCCEEDED,
    STOP_RECOVERY_MAX_RETRIES,
    TERMINAL_TOOL_STATUSES,
    classify_stop_reason,
    is_runtime_death,
)
from kiro_crew.execution_context import read_session_execution
from kiro_crew.executors import run_in_embed_pool

if TYPE_CHECKING:
    from kiro_crew.aws_project_profiles import ResolvedAwsProfile
    from kiro_crew.execution_context import ExecutionContext
    from kiro_crew.acp.runtime import AcpRuntime
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.subagent_manager.admission.types import (
        MemoryWait,
        QueuedRun,
        QueuedRunListing,
    )

from kiro_crew import platform_compat
from kiro_crew.agent_discovery import (
    AgentsDirMemo,
    _kiro_agents_dir,
    _read_agent_spec,
    cached_project_agent_names,
    is_internal_agent_spec,
    list_agents,
    plain_markdown_document,
)
from kiro_crew.agent_sdk.capabilities import capabilities_of
from kiro_crew.agent_sdk.provider_identity import PROVIDER_CLAUDE_CODE
from kiro_crew.agent_sdk.spec_hooks import (
    invalidate_stale_kas_session,
    refuse_stale_switch,
    replace_stale_shared_session,
    reproject_claimed_session,
    turn_spec_hooks,
)
from kiro_crew.agent_spec_format import is_markdown_spec, iter_agent_spec_files
from kiro_crew.atomic_write import on_event_loop  # noqa: F401
from kiro_crew.config import live
from kiro_crew.config.loader import DEFAULT_MODEL, KIRO_CLI_BUILTIN_AGENTS, KiroCrewConfig
from kiro_crew.config.paths import data_home, project_agents_dir
from kiro_crew.config.sections import SESSION_START_TIMEOUT_MIN, AgentConfig
from kiro_crew.constants import (  # noqa: F401 - DENY_CAUSE_* resolved by admission/pump.py via bind_component_globals
    DEFAULT_SPAWN_MIN_MEMORY_GB,
    DEFAULT_SUBAGENT_COST_GB,
    DEFAULT_SUBAGENT_MAX_TURNS,
    DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS,
    DENY_CAUSE_APPROVAL_UNDELIVERABLE,
    DENY_CAUSE_HOOK_ERROR,
    INITIALIZE_TIMEOUT_SECS,
    SUBAGENT_COMPLETION_PREFIX,
    SUBAGENT_TIMEOUT_SECS,
)
from kiro_crew.context import (
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    ContextBuilder,
    window_for_provider_client,
)
from kiro_crew.context_management import (
    COMPLETION_KEEP_DEFAULT_CHARS,
    apply_completion_keep,
    evict_completed_agents,
)
from kiro_crew.dashboard.side_readonly_spec import readonly_base_name
from kiro_crew.effort import effort_settings_key, model_supports_effort
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.hooks import (
    HOOK_EVENT_POST_TOOL_USE,
    fire_tool_hooks,
    hook_gate_kwargs,
)
from kiro_crew.llm_helpers import (
    FALLBACK_CANDIDATE_ATTEMPTS,
    FALLBACK_STORY_ATTR,
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    TRANSIENT_RETRIES,
    FallbackState,
    _billing_stats,
    _sum_usage,
    acp_error_is_session_not_found,
    acp_error_is_transient,
    advance_fallback_candidate,
    annotate_model_fallback,
    append_fallback_story,
    configured_fallback_chain,
    provider_fallback_active,
    provider_last_turn_usage,
    transient_retry_delay,
)
from kiro_crew.mcp_gateway import STUB_MODULE
from kiro_crew.platform.context import redact_via_context
from kiro_crew.process_identity import (  # noqa: F401 - resolved by run_lifecycle.py/run_yields.py/terminal.py via bind_component_globals
    MAX_ERROR_DETAIL_LEN,
    ProcessHandle,
    add_handle,
    ending_fence,
    failure_name,
    join_failures,
    kill_each,
    kill_set,
    kill_verified_process,
    process_handle_of,
    process_survived_async,
    refresh_live_process_tree,
    spawn_in_flight,
    teardown_capture,
    with_kill_failure,
)
from kiro_crew.providers.base import (
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    LLMEvent,
)
from kiro_crew.resource_status import pressure_level_held, read_memory_pressure_level
from kiro_crew.sandbox import _agents_slice_cgroup_dir
from kiro_crew.security import (
    redact_and_truncate,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.sel import sel
from kiro_crew.session import (  # noqa: F401 - child_process_helpers resolved by terminal.py via bind_component_globals
    SessionManager,
    child_process_helpers,
)
from kiro_crew.session_start_sizing import cached_session_start_concurrency
from kiro_crew.session_surface import has_dashboard_surface
from kiro_crew.session_workspace import result_path as _ws_result_path
from kiro_crew.slack.format import extract_options
from kiro_crew.stats import Stats
from kiro_crew.subagent_completion_meta import (
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_OK,
    single_completion_meta,
)
from kiro_crew.subagent_cost import (
    append_cost_sample,
    cap_buckets,
    compact_cost_log,
    learned_settled_for,
    read_cap_costs,
    read_learned_costs_checked,
)
from kiro_crew.subagent_manager import (
    CancellationCoordinator,
    ClaimPoint,
    ContinuationCoordinator,
    MemoryReadPoint,
    OrphanStallMonitor,
    PreparedSpawn,
    PublishedQueueDepths,
    RunEventCoordinator,
    SpawnAdmissionCoordinator,
    TerminalCoordinator,
    WaveDigestCoordinator,
    bind_component_globals,
    copy_component_docs,
)
from kiro_crew.subagent_manager.monitoring import (  # noqa: F401 - resolved by monitoring.py via bind_component_globals
    orphan_carried_fields,
    orphan_resume_hint,
    tombstone_recovery_action,
)
from kiro_crew.subagent_manager.run import _PendingDepthEmit, _PendingDepthRetry
from kiro_crew.subagent_parts import admission_surface as _admission_surface
from kiro_crew.subagent_parts import agent_policy as _agent_policy
from kiro_crew.subagent_parts import cancellation_surface as _cancellation_surface
from kiro_crew.subagent_parts import config_surface as _config_surface
from kiro_crew.subagent_parts import continuation_surface as _continuation_surface
from kiro_crew.subagent_parts import delivery as _delivery
from kiro_crew.subagent_parts import host_memory as _host_memory
from kiro_crew.subagent_parts import manager as _manager
from kiro_crew.subagent_parts import model_choice as _model_choice
from kiro_crew.subagent_parts import monitor_surface as _monitor_surface
from kiro_crew.subagent_parts import run_limits as _run_limits
from kiro_crew.subagent_parts import run_output as _run_output
from kiro_crew.subagent_parts import run_record as _run_record
from kiro_crew.subagent_parts import run_surface as _run_surface
from kiro_crew.subagent_parts import start_pricing as _start_pricing
from kiro_crew.subagent_parts import terminal_surface as _terminal_surface
from kiro_crew.subagent_parts.agent_policy import (  # noqa: F401
    _AGENT_GLOB_RE,
    _MAX_AVAILABLE_AGENTS_GLOB_CHARS,
    _MAX_AVAILABLE_AGENTS_GLOBS,
    _MAX_AVAILABLE_IN_ERROR,
    _PARENT_ALLOWLIST_MEMO,
    _PARENT_PROJECT_ALLOWLIST_MEMO,
    _UNRESOLVED_PARENT_WARNED,
    _UNRESOLVED_PARENT_WARNED_MAX,
    AGENT_INTERNAL_CODE,
    AGENT_NOT_AVAILABLE_CODE,
    AGENT_NOT_FOUND_CODE,
    UNADVERTISED_AGENTS,
    AgentCheck,
    ParentRecordUnreadable,
    ParentSpawnPolicy,
    _available_agents_hint,
    _inherited_member_agent,
    _internal_agent_refusal,
    _parent_project_resolver,
    _parent_template_for_spawn,
    _scan_parent_spawn_allowlists,
    _scope_spawn_allowlists,
    _ScopeAllowlists,
    _surface_unresolved_parent_spec,
    _validate_agent,
    _validate_app_agent_ownership,
    _vet_parent_available_agents,
    _vet_spawn_governance,
    agent_matches_allowlist,
    conductor_spawn_refusal,
    parent_project_dirs,
    parent_spawn_allowlists,
    parent_spawn_policy,
    parent_template_allowlists,
    set_parent_project_resolver,
    spawn_allowlist,
    validate_cwd,
    visible_agent_names,
)
from kiro_crew.subagent_parts.delivery import (  # noqa: F401
    _TEARDOWN_GATE_TTL_SECS,
    DELIVERY_ROUTING_FIELDS,
    DELIVERY_ROUTING_MODULES,
    NOT_DELIVERY_STATE,
    PARKS_WHEN_SET,
    PARKS_WHEN_UNSET,
    SpawnApprovalCallback,
    SpawnApprovalUnreachable,
    SubagentEventCallback,
    ToolApprovalCallback,
    _AgingIdSet,
    _audit_ids,
    _injection_notice_outcome,
    delivery_is_parked,
)
from kiro_crew.subagent_parts.host_memory import (  # noqa: F401
    _CGROUP_UNLIMITED,
    _CLK_TCK,
    _LEGACY_DEFAULT_MAX,
    _MEMORY_STAT_READ_CAP,
    MEMORY_CAUSE_CGROUP_USAGE_UNREADABLE,
    MEMORY_CAUSE_READ_UNANSWERED,
    _agents_slice_available_gb,
    _attributed_count,
    _auto_ceiling,
    _available_memory_gb,
    _cgroup_available_gb,
    _cgroup_memory_roots,
    _configured_max_subagents,
    _container_cgroup_available_gb,
    _host_available_gb,
    _host_mem_term,
    _macos_available_memory_gb,
    _macos_vm_reclaimable_pages,
    _memory_check_cause,
    _proc_subtree_sample,
    _read_int_file,
    _read_reclaimable_file_bytes,
    _subtree_cpu_jiffies,
    _working_set,
    check_memory_available,
    compute_max_subagents,
    compute_memory_sized_parallel_cap,
    pop_memory_check_cause,
    resolve_max_subagents,
    subagent_count_ceiling,
)
from kiro_crew.subagent_parts.manager import SubagentManager
from kiro_crew.subagent_parts.model_choice import (  # noqa: F401
    PINNED_RUNTIME_NOTE,
    _effort_channel,
    _effort_judged_at_session_start,
    _resolved_model_of,
    _route_unpinned_start,
    _spawn_effective_model,
    _subagent_default_effort,
    _subagent_default_model,
    effort_applied_note,
    effort_drop_reason,
    effort_pending_note,
)
from kiro_crew.subagent_parts.run_limits import (  # noqa: F401
    _CANCEL_RESUME_PREFIX,
    _CONVERSATION_TTL_SECS,
    _DEDICATED_TOPUP_POLL_SECS,
    _DEDICATED_TOPUP_WAIT_SECS,
    _DEFAULT_DIGEST_HOLD_SECS,
    _DEFAULT_INJECTION_TIMEOUT,
    _HEADLESS_DENY_REASON,
    _HOST_READ_OFF_LOOP_SECS,
    _LOW_FIDELITY_DENY_REASON,
    _MAX_CONCURRENT,
    _ON_DONE_TIMEOUT,
    _PRESSURE_EPISODE_MAX_GAP_SECS,
    _PRESSURE_HOLD_PRUNE_FACTOR,
    _REAPER_INTERVAL,
    _RECOVERY_SLOT_WAIT_SECS,
    _RELEASE_REPUMP_SECS,
    _REPORT_DRAIN_TIMEOUT,
    _RESET_TIMEOUT,
    _RSS_SAMPLES_TO_SETTLE,
    _RUN_ID_HEX_CHARS,
    _SETTLE_AFTER_SECS,
    _STALL_IDLE_SECS,
    _STARTUP_CAP_GATE_ROUNDS,
    _STARTUP_COLLECT_GRACE_SECS,
    _STARTUP_HANDSHAKES,
    _STARTUP_LAUNCH_MARGIN_SECS,
    _STARTUP_TIMEOUT_SECS,
    _STATE_DRAIN_TIMEOUT,
    _STEER_STARTUP_POLL_SECS,
    _STEER_STARTUP_WAIT_SECS,
    _SUPPRESS_CEILING,
    _SYSTEM_PREFIX,
    _TEARDOWN_REPORT_GRACE,
    _TIMEOUT_SECS,
    _TRANSIENT_CONTINUE_MSG,
    _TURN_LIMIT,
    _WAVE_STUCK_SECS,
    DIGEST_HOLD_SECS,
    INJECTION_TIMEOUT,
    SEL_MEMORY_PRESSURE_NEVER_STARTED,
    SUCCESSOR_PENDING,
    SUCCESSOR_UNKNOWN,
    _digest_hold_secs,
    _env_float,
    _resolve_injection_timeout,
)
from kiro_crew.subagent_parts.run_output import (  # noqa: F401
    _MAX_DONE_RESULT_LEN,
    _MAX_ERROR_CHAIN,
    ORPHAN_NOTICE_OWED,
    SubagentDelivery,
    _describe_exception,
    _done_result,
    _finite_nonnegative_number,
    _redact,
    _redact_and_truncate,
    _RunCreditAccounting,
    _timeout_context,
    format_subagent_usage,
)
from kiro_crew.subagent_parts.run_record import (  # noqa: F401
    _NEUTRAL_REAP_REASONS,
    AnnounceCallback,
    SubagentInfo,
    _BareStateView,
    _context_groups_field,
    _context_groups_of,
)
from kiro_crew.subagent_parts.start_pricing import (  # noqa: F401
    _SETTLED_PROJECTION_CEILING_GB,
    _SHARED_START_MIN_GB,
    _SHARED_START_SAVING_GB,
    _UNLEARNED_DEDICATED_START_GB,
    REMOTE_COST_BUCKET_SUFFIX,
    _cost_bucket,
    _dedicated_start_price_gb,
    _holds_dedicated_runtime,
    _host_memory_reading,
    _host_memory_reading_off_loop,
    _host_read_in_flight,
    _owns_dedicated_runtime,
    _parent_runs_remotely,
    _parked_at_spawn_approval,
    _row_settled,
    _selection_kind,
    _shared_start_price_gb,
    _SharingPlan,
    _spawn_memory_floor_and_cost,
    _startup_memory_reserve_gb,
)
from kiro_crew.subagent_persistence import (  # noqa: F401 - read_tombstone resolved by run_lifecycle.py via bind_component_globals
    DISMISSAL_FAILED,
    _agent_dir,
    _cleanup_session_files_sync,
    _subagents_dir,
    agent_dir_for_display,
    clear_leaked_process,
    clear_tombstone,
    create_agent_folder,
    list_leaked_processes,
    list_orphans,
    mark_delivered,
    prune_stale_tombstones,
    read_state,
    read_tombstone,
    record_panel_dismissal_outcome,
    record_slow_command,
    result_is_whole,
    settle_delivered_batch,
    update_state,
    write_finished_result,
    write_result_chunk,
    write_tombstone,
)
from kiro_crew.subagent_wait_reasons import (  # noqa: F401 - re-exported: the gate and handlers read them from this namespace
    DEFERRED_QUEUED_REASONS,
    MEMORY_PRESSURE_DETAIL,
    MEMORY_PRESSURE_NEVER_STARTED,
    MEMORY_PRESSURE_RECHECK_SECS,
    QUEUED_REASON_CONCURRENCY_LIMIT,
    QUEUED_REASON_LOW_MEMORY,
    QUEUED_REASON_MEMORY_PRESSURE,
    QUEUED_WAIT_EXPIRED_TEXT,
)
from kiro_crew.validation import _AGENT_NAME_RE, is_registered_agent_name

# Standalone ClaudeCodeProvider removed (KiroACP-only). Name kept as None so the
# legacy isinstance guards short-circuit; which seam serves a session is answered
# by ``SessionCapabilities.provider_seam``.
ClaudeCodeProvider = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks


def _safe_fire(coro: Awaitable[None]) -> None:
    """Schedule a coroutine, preventing GC and logging failures."""

    async def _wrap() -> None:
        try:
            await coro
        except Exception:
            logger.warning("Subagent callback failed", exc_info=True)

    task = asyncio.ensure_future(_wrap())
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


# Why an accepted spawn is WAITING rather than running -- the label the admission
# gate puts on the wait it decided on, carried on ``SubagentInfo.queued_reason``,
# on the ``subagent_queued`` lifecycle event (``reason``) and, for the deferred
# kinds, on ``POST /api/spawn``'s ``status: "queued"`` answer. Defined in the leaf
# module :mod:`kiro_crew.subagent_wait_reasons` so a surface that must not import
# this module at runtime (the channel command layer) can still read them; re-exported
# here (see the import block above) because the gate and the handlers read them
# from this namespace.


# Imported here only for the subagent_parts owners: compose runs their functions on
# this module's globals, so every name they read must stay bound here.
_PARTS_GLOBAL_BINDINGS = (
    AbstractSet,
    AgentConfig,
    AgentsDirMemo,
    COMPLETION_KEEP_DEFAULT_CHARS,
    Callable,
    ClaimPoint,
    Container,
    ContextBuilder,
    DEFAULT_MODEL,
    InFlightToolTracker,
    Iterable,
    Iterator,
    KIRO_CLI_BUILTIN_AGENTS,
    Literal,
    Mapping,
    MemoryReadPoint,
    Optional,
    PROVIDER_CLAUDE_CODE,
    PROVIDER_LABEL_CLAUDE,
    PreparedSpawn,
    PublishedQueueDepths,
    PurePosixPath,
    SESSION_START_TIMEOUT_MIN,
    STUB_MODULE,
    Sequence,
    ToolCallState,
    _PendingDepthEmit,
    _PendingDepthRetry,
    _agents_slice_cgroup_dir,
    _billing_stats,
    _kiro_agents_dir,
    _read_agent_spec,
    _sum_usage,
    boottime_now,
    cached_project_agent_names,
    cached_session_start_concurrency,
    capabilities_of,
    data_home,
    effort_settings_key,
    fnmatch,
    is_internal_agent_spec,
    is_markdown_spec,
    is_registered_agent_name,
    iter_agent_spec_files,
    list_agents,
    live,
    math,
    model_supports_effort,
    plain_markdown_document,
    project_agents_dir,
    provider_last_turn_usage,
    read_cap_costs,
    read_session_execution,
    readonly_base_name,
    redact_and_truncate,
    redact_via_context,
    threading,
)

if TYPE_CHECKING:
    # Type-only names the owners import from this module for the type checker.
    _PARTS_TYPE_ONLY_NAMES = (
        AcpRuntime,
        ExecutionContext,
        LLMProvider,
        MemoryWait,
        QueuedRun,
        QueuedRunListing,
        ResolvedAwsProfile,
    )


# Every function the subagent_parts owners define runs on this module's globals, so a
# patch of ``kiro_crew.subagent.<name>`` reaches it wherever it now lives; see
# ``subagent_parts``. Run once, after every owner is imported and re-exported.
_facade_compose(
    globals(),
    (
        _run_limits,
        _run_output,
        _agent_policy,
        _model_choice,
        _host_memory,
        _start_pricing,
        _run_record,
        _delivery,
        _config_surface,
        _admission_surface,
        _continuation_surface,
        _monitor_surface,
        _terminal_surface,
        _run_surface,
        _cancellation_surface,
        _manager,
    ),
    host=SubagentManager,
)


# Component implementations deliberately resolve globals through this module:
# existing integrations patch ``kiro_crew.subagent.*`` after manager creation.
_COMPONENT_GLOBAL_BINDINGS = (
    AcpSessionProvider,
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_NOT_REPLAYED_TEXT,
    DEFAULT_SPAWN_MIN_MEMORY_GB,
    cap_buckets,
    learned_settled_for,
    read_learned_costs_checked,
    Any,
    CONTEXT_GROUP_LESSONS,
    CONTEXT_GROUP_MEMORY,
    CONTEXT_GROUP_PROJECT,
    EVENT_AGENT_SWITCHED,
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    FALLBACK_CANDIDATE_ATTEMPTS,
    FALLBACK_STORY_ATTR,
    FallbackState,
    HOOK_EVENT_POST_TOOL_USE,
    KiroCrewConfig,
    LLMEvent,
    LivenessOracle,
    OUTCOME_FAILED,
    OUTCOME_INTERRUPTED,
    OUTCOME_OK,
    PROVIDER_LABEL_DEFAULT,
    Path,
    SUBAGENT_COMPLETION_PREFIX,
    Stats,
    TRANSIENT_RETRIES,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    _AGENT_NAME_RE,
    _agent_dir,
    _cleanup_session_files_sync,
    _subagents_dir,
    _ws_result_path,
    acp_error_is_session_not_found,
    acp_error_is_transient,
    advance_fallback_candidate,
    agent_dir_for_display,
    annotate_model_fallback,
    append_cost_sample,
    append_fallback_story,
    apply_completion_keep,
    asyncio,
    pressure_level_held,
    read_memory_pressure_level,
    clear_tombstone,
    compact_cost_log,
    configured_fallback_chain,
    _cost_bucket,
    consult_offloaded,
    create_agent_folder,
    evict_completed_agents,
    extract_options,
    fire_tool_hooks,
    format_subagent_usage,
    hook_gate_kwargs,
    has_dashboard_surface,
    result_is_whole,
    write_finished_result,
    list_orphans,
    maintenance_executor,
    mark_delivered,
    os,
    platform_compat,
    provider_fallback_active,
    prune_stale_tombstones,
    read_state,
    redact_credentials,
    redact_exfiltration_urls,
    run_in_embed_pool,
    sel,
    settle_delivered_batch,
    single_completion_meta,
    subprocess_executor,
    time,
    transient_retry_delay,
    turn_spec_hooks,
    invalidate_stale_kas_session,
    refuse_stale_switch,
    replace_stale_shared_session,
    reproject_claimed_session,
    update_state,
    window_for_provider_client,
    write_result_chunk,
    write_tombstone,
)
_MANAGER_COMPONENTS = (
    OrphanStallMonitor,
    TerminalCoordinator,
    SpawnAdmissionCoordinator,
    ContinuationCoordinator,
    WaveDigestCoordinator,
    RunEventCoordinator,
    CancellationCoordinator,
)
bind_component_globals(_MANAGER_COMPONENTS, globals())
copy_component_docs(SubagentManager, _MANAGER_COMPONENTS)
