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
    MutableMapping,
    Sequence,
)
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, AbstractSet, Any, Literal, Optional, Protocol

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
from kiro_crew.agent_sdk.drivers.acp_vocab import (  # noqa: F401 - STOP_* resolved by run.py via bind_component_globals
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
from kiro_crew.process_identity import (  # noqa: F401 - resolved by run.py/terminal.py via bind_component_globals
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
from kiro_crew.subagent_parts import agent_policy as _agent_policy
from kiro_crew.subagent_parts import compose as _compose_parts
from kiro_crew.subagent_parts import host_memory as _host_memory
from kiro_crew.subagent_parts import model_choice as _model_choice
from kiro_crew.subagent_parts import run_limits as _run_limits
from kiro_crew.subagent_parts import run_output as _run_output
from kiro_crew.subagent_parts import start_pricing as _start_pricing
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
    parent_project_dirs,
    parent_spawn_allowlists,
    parent_spawn_policy,
    parent_template_allowlists,
    set_parent_project_resolver,
    spawn_allowlist,
    validate_cwd,
    visible_agent_names,
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
    _host_mem_term,
    _macos_available_memory_gb,
    _macos_vm_reclaimable_pages,
    _memory_check_cause,
    _proc_subtree_sample,
    _read_inactive_file_bytes,
    _read_int_file,
    _subtree_cpu_jiffies,
    _windows_available_memory_gb,
    _working_set,
    check_memory_available,
    compute_max_subagents,
    compute_memory_sized_parallel_cap,
    pop_memory_check_cause,
    resolve_max_subagents,
    subagent_count_ceiling,
)
from kiro_crew.subagent_parts.model_choice import (  # noqa: F401
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
from kiro_crew.subagent_parts.start_pricing import (  # noqa: F401
    _SETTLED_PROJECTION_CEILING_GB,
    _SHARED_START_MIN_GB,
    _SHARED_START_SAVING_GB,
    _UNLEARNED_DEDICATED_START_GB,
    _cost_bucket,
    _dedicated_start_price_gb,
    _holds_dedicated_runtime,
    _host_memory_reading,
    _host_memory_reading_off_loop,
    _host_read_in_flight,
    _owns_dedicated_runtime,
    _parked_at_spawn_approval,
    _row_settled,
    _selection_kind,
    _shared_start_price_gb,
    _SharingPlan,
    _spawn_memory_floor_and_cost,
    _startup_memory_reserve_gb,
)
from kiro_crew.subagent_persistence import (  # noqa: F401 - read_tombstone resolved by run.py via bind_component_globals
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
    AgentsDirMemo,
    Container,
    DEFAULT_MODEL,
    KIRO_CLI_BUILTIN_AGENTS,
    Optional,
    PurePosixPath,
    STUB_MODULE,
    _agents_slice_cgroup_dir,
    _billing_stats,
    _kiro_agents_dir,
    _read_agent_spec,
    _sum_usage,
    cached_project_agent_names,
    effort_settings_key,
    fnmatch,
    is_internal_agent_spec,
    is_markdown_spec,
    is_registered_agent_name,
    iter_agent_spec_files,
    list_agents,
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
)


#: Reap reasons that end a run on purpose. A tombstone written with one of these
#: is a neutral "stopped", never a failure, and the same set decides whether the
#: reap-echo arm in ``_run`` and ``_force_reap``'s own record synthesize an error.
_NEUTRAL_REAP_REASONS = frozenset({"user_stop", "parent_end"})


class _BareStateView(MutableMapping[str, Any]):
    """Live ``{toolCallId: ToolCallState}`` view over an :class:`InFlightToolTracker`.

    The sub-agent reads and writes ``_active_tool_calls`` as a map of bare
    :class:`ToolCallState` values (it carries no aux), while the tracker stores
    ``(state, aux)`` tuples shared with ``AcpSessionHandle``. This adapter
    unwraps on read and wraps (with ``aux=None``) on write so the hand-off still
    lives in the one tracker, yet a direct ``view[id] = state`` — used by a test
    staging a stale call, and by nothing in production — persists and is visible
    to membership and to :meth:`InFlightToolTracker.clear`. Reads go straight to
    the tracker's live dict, so a dispatch/result through the tracker shows up
    here with no copy.
    """

    __slots__ = ("_tracker",)

    def __init__(self, tracker: InFlightToolTracker) -> None:
        self._tracker = tracker

    def __getitem__(self, key: str) -> Any:
        return self._tracker.active_calls[key][0]

    def __setitem__(self, key: str, value: Any) -> None:
        self._tracker.active_calls[key] = (value, None)

    def __delitem__(self, key: str) -> None:
        del self._tracker.active_calls[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._tracker.active_calls)

    def __len__(self) -> int:
        return len(self._tracker.active_calls)


@dataclass
class SubagentInfo:
    """Metadata for a running subagent."""

    id: str
    task: str
    started: float = field(default_factory=time.time)
    done: bool = False
    # True for work accepted behind the stagger/concurrency gate but never
    # started. The record returned by ``spawn`` is normally not registered in
    # ``_agents``; a queued stop registers a synthetic copy temporarily so batch
    # settlement can observe it, and keeps this discriminator True so running
    # cancellation and live-resource monitoring do not treat it as executing.
    queued: bool = False
    # Why a ``queued`` record waits: one of the ``QUEUED_REASON_*`` kinds, or ""
    # for a wait the gate did not label (a claim retained across a store outage).
    # ``queued_reason_detail`` is the gate's own sentence for it -- the same text
    # the task store's ``deferred`` event records -- so ``POST /api/spawn`` can
    # relay it verbatim. Both stay "" on a running or terminal record.
    queued_reason: str = ""
    queued_reason_detail: str = ""
    result: str = ""
    result_path: str = ""
    result_truncated: bool = False  # completion-event copy dropped content → summary+path
    error: str = ""
    # Machine-readable identifier for ``error``, forwarded by ``POST /api/spawn``
    # as the response's ``code`` so a client can switch on the decision instead of
    # parsing the prose. Set today only by the unknown-agent refusal
    # (``AGENT_NOT_FOUND_CODE``), which is the one rejection a client acts on
    # differently: ``spawn_run`` stops re-posting a name the gateway already
    # refused. The other kinds that DO carry an error (bad task, low memory, a cwd
    # refusal, governance) stay un-coded and the handler answers them under a
    # generic code — a code with no consumer would be contract surface bought for
    # nothing. A capacity refusal never reaches this field at all: it returns no
    # record, and the handler answers 429 from that absence.
    error_code: str = ""
    parent_session_key: str = ""
    memory_mode: str = field(default="persistent", kw_only=True)
    _memory_mode_ready: bool = field(default=True, init=False, repr=False)
    agent: str = ""
    # The app that spawned this child (empty for a non-app spawn). Persisted so
    # the child's per-tool-call gate can resolve the app's Level-2 profile, not
    # just the spawn-time decision — without it the profile constrains only
    # whether the spawn was allowed, and the child's ongoing tool calls run
    # unconstrained by the app scope.
    app: str = ""
    approval_mode: str = ""  # "auto" to skip tool approvals in the subagent session
    silent: bool = False  # suppress completion notification (dashboard + Slack)
    turns: int = 0
    last_tool: str = ""
    tool_count: int = (
        0  # count of observed tool calls (incl. auto-approved); drives running-card progress
    )
    last_activity: float = field(
        default_factory=time.time
    )  # time.time() of last stream event; drives idle-stall detection
    stalled: bool = (
        False  # True while the reaper has flagged this subagent as idle/stalled (UI signal)
    )
    # follow_up delivery mode (spawn_steer mode="follow_up"): messages queued
    # here are NOT injected into the running turn — they are dispatched as ONE
    # continuation on this run's conversation after the run completes, so a
    # correction can wait for the current turn instead of interrupting it
    # mid-execution. Drained by the per-run watcher (_deliver_followups).
    pending_followups: list = field(default_factory=list)
    # True once a followup watcher task is armed for this run (one per run).
    _followup_watcher: bool = False
    _stall_suspect_at: float = (
        0.0  # first reaper sweep that saw the idle threshold exceeded; 2-sweep confirmation (scale dampening)
    )
    # True while blocked on a human approval prompt — either the pre-execution
    # spawn gate or a mid-run tool prompt; exempt from idle-stall. Paired with
    # ``_exec_started is None`` it also tells the reaper which of the two a
    # parked run is sitting on.
    _awaiting_approval: bool = False
    # The in-flight tool hand-off rule, shared with ``AcpSessionHandle`` via
    # ``InFlightToolTracker``. The sub-agent tracks its calls by toolCallId: a
    # native child's call (``sub_session_id`` set) is judged on arrival (so a
    # stall is attributed to the child's own command) but not stored, because
    # its result never reaches this loop; a call with an id is stored until its
    # terminal result and the judged slot passes to the newest one still running
    # when it finishes. The attribution lets the liveness oracle key evidence to
    # THIS subagent's own child process (by cmdline match) instead of to the
    # whole runtime subtree — which, on a session-shared runtime, is dominated
    # by kiro-cli's own background I/O.
    # ``_inflight_tool`` (the most recently dispatched call still in flight,
    # None when none is) and ``_active_tool_calls`` (every dispatched call by
    # toolCallId until its terminal result, so a fast call returning first does
    # not drop a slower one still running beside it) are tracker-backed
    # properties below, not fields — the hand-off rule lives in the tracker.
    _tool_tracker: InFlightToolTracker = field(default_factory=InFlightToolTracker)
    # Per-agent liveness oracle. One instance PER AGENT is required, not one per
    # manager: the oracle keys its counter samples by kind ("io"/"cpu"), not by
    # pid, so a shared instance would let one agent's sample become another's
    # baseline and read as movement. Retired (not cleared) on every new tool
    # dispatch so a walk still running against the previous tool cannot write
    # into the next tool's baseline.
    _stall_oracle: Any = None
    # The in-flight offloaded consult for this agent, if any. Tracked so at most
    # ONE /proc walk per agent is outstanding: a permanently wedged read would
    # otherwise leave a blocked worker behind on every reaper sweep and starve
    # the shared subprocess pool that teardown also draws from. Deliberately NOT
    # cleared when the oracle is retired on a new tool dispatch — dropping the
    # handle would un-bound exactly that growth.
    _consult_future: Any = None
    # Monotonic generation of the attribution snapshot above. Bumped on EVERY
    # retirement (new dispatch, final tool result, fresh stream activity) so an
    # offloaded consult that outlived the tool it was submitted for can be
    # recognised as stale and discarded instead of applied to whatever is in
    # flight now. Without it the ``/proc`` walk's own latency is enough to flag a
    # subagent that resumed work while the walk was still running.
    _stall_gen: int = 0
    # Batch/wave identity: set when this spawn is part of a multi-task wave
    # (spawn_run tasks=[...]) so scale plumbing can digest completions and
    # emit batch lifecycle events. Empty for standalone spawns.
    delegation: dict[str, str] = field(default_factory=dict)
    batch_id: str = ""
    batch_total: int = 0
    # True when this member's per-agent injection was HELD for the wave digest
    # (gateway _subagent_done). The run loop must then SKIP mark_delivered():
    # the result is not yet in the parent's context, and a "delivered"
    # tombstone would exclude it from orphan reconciliation — a gateway
    # restart mid-wave would silently lose every held completion. The gateway
    # marks held members delivered when the digest fires.
    _digest_held: bool = False
    # ``time.time()`` when the gateway HELD this member's delivery for the wave
    # digest; 0.0 once that hold has been flushed (or never held). Separate from
    # ``_digest_held`` on purpose: that flag is the restart-safety contract read
    # by the run loop, and must not be mutated by the hold-deadline sweep. This
    # timestamp is the sweep's only input — see ``_sweep_digest_holds``. It is a
    # ``time.monotonic()`` reading, compared with the reaper's monotonic ``now``.
    _digest_held_at: float = 0.0
    # True ONLY for the synthetic record that :meth:`force_digest_flush`
    # announces to release held results whose hold deadline expired. It is NOT a
    # wave member: it carries the wave's ``batch_id`` so the gateway can find
    # the wave's digest buffer, but the gateway must skip every per-member side
    # effect for it (WS terminal event, orchestration tracker accounting,
    # done/ok/err counters, digest lines) and only force the flush.
    _digest_flush_only: bool = False
    # A reap/stop has STARTED but may still be in its (awaiting) teardown. Split
    # out of `reaped` because that flag carries two incompatible meanings: the
    # cancel-recovery scheduler needs it set BEFORE the teardown awaits (or it
    # respawns the run being killed), while `_run`'s error synthesis needs it
    # still False until the reaper actually owns the record (or a run woken by
    # the reaper's session reset skips its own error and reports a FALSE
    # SUCCESS). One flag cannot be both early and late; this one is the early
    # half — "do not respawn, a reap is in flight".
    _reap_started: bool = False
    # WHY the reap in flight is happening, written next to ``_reap_started`` and
    # read by the run loop when its stream dies UNDER that reap. ``_force_reap``
    # tears the run's session down before it cancels the task, so the in-flight
    # turn observes its own runtime being killed first and raises
    # ``AcpProcessDied`` -- "killed (provider shutdown)". Without these two fields
    # that echo was recorded as the run's failure: a ``cause="error"`` tombstone
    # carrying the death text and an ERROR log, for a run a user had just pressed
    # Stop on (or a parent end had cancelled). ``_reap_reason`` is the tombstone
    # cause the reap itself would write (``user_stop`` / ``parent_end`` /
    # ``reaped`` / ``startup_timeout``); ``_stop_origin`` is the one-line WHO/WHY
    # for the record and the log ("stopped by user", "parent conversation ended
    # (retire_kiro_identity_sessions)", "reaped after 900s (reaped)").
    _reap_reason: str = ""
    _stop_origin: str = ""

    @property
    def stop_is_neutral(self) -> bool:
        """Whether the reap that owns this run is a deliberate stop, not a failure.

        Decided by the FIRST stopper (``_reap_reason``), never by
        ``user_stopped`` alone: a Stop that lands while a deadline reap is already
        tearing the run down sets ``user_stopped`` too, and reading that flag would
        let the late Stop convert a claimed deadline failure into a neutral stop.
        A user Stop and a parent end are neutral; a deadline or
        startup reap is the run's own failure.
        """
        return self._reap_reason in _NEUTRAL_REAP_REASONS

    @property
    def _inflight_tool(self) -> Any:
        """The :class:`ToolCallState` a stall verdict is read against (``None``
        when no tool is in flight).

        A view onto the shared :class:`InFlightToolTracker`'s current judged
        call, so the hand-off rule lives in one place
        (``_note_tool_dispatch`` / ``_note_tool_result`` drive it). The setter
        stages one call directly under the empty-id slot, which is what a test
        or a caller that only has a single call to pin uses.
        """
        return self._tool_tracker.current

    @_inflight_tool.setter
    def _inflight_tool(self, value: Any) -> None:
        if value is None:
            self._tool_tracker.clear()
        else:
            self._tool_tracker.dispatch("", value)

    @property
    def _active_tool_calls(self) -> MutableMapping[str, Any]:
        """The live ``{toolCallId: ToolCallState}`` map of calls still running.

        A :class:`_BareStateView` over the shared tracker, so
        ``_note_tool_result``'s successor hand-off and the tests that read
        membership or stage a stale call keep working. The tracker stores
        ``(state, aux)`` tuples; this unwraps to the bare ``ToolCallState`` the
        sub-agent reads and writes. Prefer ``_note_tool_dispatch`` /
        ``_note_tool_result`` / ``_clear_tool_dispatch`` to mutate it.
        """
        return _BareStateView(self._tool_tracker)

    # Set by the gateway on the wave's FINAL member only: terminal snapshots
    # whose delivery tombstones must be settled once the digest has been
    # successfully handed off (i.e. after _on_done returns without raising —
    # the same contract as the per-agent mark_delivered). Settling these at
    # digest COMPOSITION would re-open the restart-loss window between
    # composing and routing.
    _digest_settle_deliveries: list[SubagentDelivery] = field(default_factory=list)
    # True on the synthetic record of a memory-wait expiry whose report the store
    # owes until it reaches the parent (``finish(..., report_owed=True)``). The
    # gateway reads it to carry that debt with a digest or a queued announce
    # (``SubagentDelivery.report_owed``); the report's done-callback clears the
    # store mark itself only on ``_report_delivered``.
    _report_owed: bool = False
    # True once the route ``_on_done`` took has positively delivered this report:
    # the parent's turn consumed the injection, the blocking tool collected it
    # inline, or the route has no conversation and its notification is the whole
    # delivery. A route that parks the report (a digest hold, a queued announce)
    # or gives up on it leaves this unset, and so does any route added later that
    # does not say so; a memory-wait expiry's owed mark is cleared only on it.
    _report_delivered: bool = False
    # True once an injection of this report was given up on: every attempt the
    # gateway made failed, so at most a failure notice was queued for the
    # parent's next turn (``notify_injection_failed``). ``_on_done`` still
    # returns, so this is the only sign the parent was never told, and the run
    # loop writes no ``delivered`` tombstone while it is set.
    _report_undelivered: bool = False
    # True when the gateway QUEUED this completion's injection because the
    # parent's slot was busy. Delivery is not consumption: the announce sits in
    # the slot queue until a turn drains it, and that wait is bounded only by the
    # turn ceiling — far longer than agent.subagent_result_ttl_secs. The run loop
    # must therefore SKIP mark_delivered() (a "delivered" tombstone starts the
    # retention clock, so the reaper would prune result.txt while the promise of
    # it is still queued, and the parent would be handed a dead path). The drain
    # settles the tombstone instead — see
    # ``_ChatSlot.take_pending_subagent_deliveries``.
    _delivery_queued: bool = False
    max_turns: int = 0
    reaped: bool = False
    streaming_text: str = ""
    elapsed: float = 0.0
    # Deadline captured when execution starts (``0`` until then). A config
    # reload changes the manager default for future runs only; it never
    # rewrites this run's deadline.
    timeout_secs: int = 0
    # Cumulative across every attempted turn, including transient retries that
    # consumed credits before failing. Providers that do not bill in credits
    # report zero through the shared TurnUsage contract.
    credits: float = 0.0
    # Shared with terminal reporters so a reap need not wait for a cancelled
    # consumer's state-write drain before settling the active attempt.
    _credit_accounting: _RunCreditAccounting | None = field(default=None, repr=False, compare=False)
    _raw_task: str = ""  # unredacted task for kiro-cli execution prompt
    # CC-specific overrides (ignored for ACP)
    model: str = ""
    # The model id the live session ACTUALLY resolved to serve, read back from
    # the provider's public ``served_model`` accessor. Distinct from ``model``,
    # which is only the REQUESTED pin (often "" ⇒ provider default): the ACP
    # backend reports the served id even on the default, and a routing/config/
    # availability downgrade makes the two differ. "" means unknown/inconclusive
    # (never a wildcard) — the ACP session/new response fills it at spawn, while
    # the CC/raw path only knows it after the first turn, so it is refreshed at
    # completion too. Surfaced on the subagent WS frames and completion meta so a
    # model-pinned review's actual model is auditable.
    resolved_model: str = ""
    # The EFFECTIVE requested model — the per-spawn pin (``model``) OR, when that
    # is empty, the ``agent.role_models['subagent']`` config pin
    # (docs/system-specs/common/model-selection.md names
    # the config pin as *the* way to pin a subagent model). This is the side the
    # downgrade comparison must use: a config-pinned run served a different model
    # is exactly the "unverifiable pin" this feature exists to catch, and keying
    # off the bare per-spawn ``model`` would miss it.
    # ``"auto"`` ⇒ unpinned (no per-spawn pin, no role pin — the provider picks
    # the model). Resolved once at spawn.
    requested_model: str = ""
    # The model ``model.route`` picked for an UNPINNED start and the tier it ran
    # at; both "" when the start was not routed. Kept apart from ``model``, which
    # is only ever a caller's own pin, so a pinned start is never routed, a
    # recovery respawn of a routed start keeps the routing it already has, and
    # the usage row names the model that actually served (``model_source``)
    # rather than a routed id the child session may have withheld.
    routed_model: str = ""
    routed_tier: str = ""
    # Per-call reasoning-effort override (spawn_run ``reasoning_effort``).
    # Wins over the ``role_efforts['subagent']`` pin; ``""`` defers to it.
    # Like ``model``, a non-empty value forces the dedicated-process path.
    reasoning_effort: str = ""
    # Per-call backend (spawn_run ``backend``): the harness this run starts on
    # instead of the gateway default. ``None`` = the default; ``""`` is Kiro's
    # own id. Like ``model``, a value forces the dedicated-process path -- the
    # shared runtime is the PARENT's process, on the parent's backend.
    backend: str | None = None
    allowed_tools: list[str] = field(default_factory=list)
    bare: bool = False
    # Continuable conversations (spawn_run keep=True / spawn_continue):
    # keep=True forces a dedicated (non-shared) session, persists the sid via
    # SessionManager.mark_continuable, and skips session-file deletion at
    # teardown so the conversation can be resumed by a later run.
    keep: bool = False
    # Which switchable context groups this sub-agent inherits from the injected
    # session context. All True ⇒ byte-identical to a non-sub-agent session. A
    # parent opts one out when it can name why this task cannot need it; the
    # sub-agent is told by name what was withheld so it reports the gap instead
    # of guessing. Resolved once at spawn and carried through the queue and
    # retry paths, so a drained or retried run sees the same scope as the run
    # its caller asked for.
    include_memory: bool = True
    include_lessons: bool = True
    include_project: bool = True
    # The memory silo this child reads and writes, or "" for the global store.
    # Carried rather than derived: `agent` above holds a kiro-cli modeId, a
    # namespace disjoint from cfg.agents, so resolving a store from it answers
    # `default` for exactly the crew that configured otherwise — silently, and
    # toward the operator's own memory. The caller passes a name it already
    # resolved (ResolvedBindings.memory_store_name) or nothing at all.
    #
    # Empty is the correct default for a plain spawn: a child that inherits no
    # crew reads the global store, which is what every spawn did before crews
    # had silos.
    memory_store: str = ""
    execution_context: ExecutionContext | None = field(default=None, kw_only=True)
    # A named member remains the conversation owner during a template override.
    crew: str = field(default="", kw_only=True)
    # Session key override for continuation runs: a spawn_continue run reuses
    # the ORIGINAL run's session key (``subagent:<conv-id>``) so get_or_create
    # finds the persisted sid and arms session/load. Empty ⇒ the default
    # ``subagent:{id}``.
    conversation_key: str = ""
    # Successor claims, owned by ``SubagentManager.claim_retry`` and the
    # continuation entries. ``_retried_as``: the run a dashboard retry started
    # from this one, or ``SUCCESSOR_PENDING`` while that start is in flight.
    # ``_continued_as``: the FIRST ``spawn_continue`` of this run that landed,
    # never overwritten. ``_continuation_starting``: a continuation start is in
    # flight, set and cleared with the conversation id it claims in
    # ``SubagentManager._continuations_starting``; ``claim_retry`` reads it. A
    # failed card stays failed for history, so these are what stop a retry from
    # running its task beside another successor.
    _retried_as: str = ""
    _continued_as: str = ""
    _continuation_starting: bool = False
    # Optional subprocess cwd override. When set, the subagent kiro-cli/claude-code
    # process launches here instead of the default ``subagent_<id>`` sandbox, so
    # cwd-relative resource globs (``.kiro/steering/**/*.md``, ``AGENTS.md``,
    # ``CLAUDE.md``) resolve against this directory. Validated on spawn against
    # ``AgentConfig.subagent_cwd_allowed_roots``.
    cwd: str = ""
    _pid: int | None = None  # PID of kiro-cli child process, for tombstone diagnostics
    # Wall-clock (time.time) when _run_inner actually began executing. Distinct
    # from ``started`` (set at registration): a subagent may sit in ``_agents``
    # awaiting spawn approval for an arbitrary time before execution begins. The
    # startup watchdog measures from THIS timestamp so it never reaps an agent
    # that is merely waiting for approval. None until execution starts.
    _exec_started: float | None = None
    # ``_exec_started`` on the monotonic clock, stamped and cleared beside it.
    # When it is set the startup watchdogs measure from it, so a wall-clock step
    # cannot reap a start in progress.
    _exec_started_mono: float | None = None
    # The run's registration on the monotonic clock. The reaper measures the run
    # timeout from it, so a wall-clock step cannot reap a healthy run or spare a
    # stuck one; ``started`` stays wall-clock for display and state.json.
    _started_mono: float = field(default_factory=time.monotonic)
    # ``last_activity`` on the monotonic clock, written beside it. The
    # first-prompt-silent watchdog measures from it; ``last_activity`` stays
    # wall-clock for the dashboard's idle display and the stall flag.
    _last_activity_mono: float = field(default_factory=time.monotonic)
    # Wall-clock moment this execution's own session first answered its prompt
    # (``SubagentManager._leave_startup``): the first frame addressed to this
    # session, or a dependency verdict on the prompt. None until then, and reset
    # by ``_run_inner`` for every execution. The startup watchdog and the
    # in-startup bound read it only while ``_pid`` is None, because a runtime
    # PID ends startup on its own; an AcpRuntime-backed start records the PID
    # its runtime reports before the stream opens, and a runtime reports one
    # from spawn until its process is reset. The adaptive controller reads it
    # as the floor of this run's progress.
    _first_stream_started: float | None = None
    # The same moment on the monotonic clock, and the ``_rss_generation`` of the
    # process it belongs to: the reserve's time-based settle reads these, so a
    # wall-clock step or a respawned process never settles a warming row.
    _first_stream_mono: float | None = None
    _first_stream_generation: int = -1
    # ``runtime_global`` frames this execution received while still in
    # startup; named in the startup reap's error. Reset with the marker.
    _startup_cotenant_frames: int = 0
    # (start clock, deadline) the startup watchdog fixed for this start.
    _startup_deadline_stamp: tuple[float, int] | None = None
    # Wall-clock moment this run began waiting for a start-queue permit
    # (``_gate_wait_mark``); None outside that wait. While set, the startup
    # watchdog reads the start clock as paused at this moment: time queued for a
    # permit is not start time. Cleared by ``_gate_exit_reset`` at acquisition,
    # which adds the wait to ``_start_queue_wait_ms``.
    _gate_wait_started: float | None = None
    # ``_gate_wait_started`` on the monotonic clock, marked and cleared beside it.
    _gate_wait_started_mono: float | None = None
    # Learned-cost high-water marks (dynamic-subagent-sizing.md §4.1), sampled
    # periodically by the reaper loop and folded into the cost store at exit.
    peak_rss_gb: float = 0.0
    peak_cpu_cores: float = 0.0
    # Settled-runtime reading: the first quiet subtree sample of a DEDICATED
    # process after its own session answered, with no tool in flight across the
    # read. What the runtime holds once it is up, before its work grows the tree;
    # persisted as the cost store's ``settled_gb``, which the admission gate's
    # dedicated start projection learns from. ``_settled_rss_generation`` ties it
    # to the process it measured, so a respawn re-captures (cancellation.py).
    settled_rss_gb: float = 0.0
    _settled_rss_generation: int = -1
    # GiB the admission gate reserved this start at: the shared price when it was
    # predicted to share its parent's runtime, else the dedicated projection.
    # ``_startup_memory_reserve_gb`` charges it in full until the row
    # settles. None = priced at the dedicated projection (a row the gate did not
    # build, e.g. a recovered one).
    _start_price_gb: float | None = None
    # True while ``_start_price_gb`` is the SHARED price: the one row a
    # dedicated launch must top up first (``_ensure_dedicated_start_priced``).
    _start_priced_shared: bool = False
    # True while this row waits its turn for the dedicated top-up's re-check;
    # the row holding the turn does not count waiters queued behind it.
    _topup_waiting: bool = False
    # Most-recent sample of the same two signals. The peaks answer "how big can
    # this agent get" (what sizing needs); a live task-manager surface needs "how
    # big is it right now", which a high-water mark cannot express — it never
    # comes back down. Both are written by the same sweep, so exposing the last
    # sample costs no extra syscalls.
    last_rss_gb: float = 0.0
    last_cpu_cores: float = 0.0
    # Live process/MCP-stub counts of this run's subtree, from the same sweep.
    # ``None`` = not measured yet (or unmeasurable on this platform), which the
    # surface must render as an em dash: a live runtime with "0 processes" is a
    # lie, and it is exactly the reading that made subagent rows look like they
    # carried no MCP stubs at all.
    last_procs: int | None = None
    last_stubs: int | None = None
    _cpu_jiffies_prev: int = 0  # last subtree utime+stime sample (clock ticks)
    _cpu_sample_ts: float = 0.0  # monotonic time of the last CPU sample
    # How many reaper sweeps have measured a non-zero RSS for this run. The
    # spawn guard treats a dedicated worker as still WARMING until it has been
    # seen by two sweeps (an interval apart), so a single reading taken
    # mid-growth is not mistaken for the worker's size (see
    # _startup_memory_reserve_gb).
    _rss_samples: int = 0
    # Bumped when the run gets a NEW process (the cancel-recovery respawn), so
    # an off-loop sweep that read the old process cannot land its reading on
    # the new one: the sweep snapshots this before reading and writes only if
    # it is unchanged.
    _rss_generation: int = 0
    # Session sharing — when True, this subagent runs as a session on the
    # parent's shared AcpRuntime instead of its own process. Cleanup skips
    # release/reset (no entry in SessionManager) and instead calls shutdown()
    # on the _shared_provider directly.
    _session_sharing: bool = False
    _shared_provider: Any = None  # AcpSessionProvider when _session_sharing=True
    # ── Turn-resilience state (subagent parity with the main-agent guards) ──
    # True when the user explicitly stopped this agent (DELETE /api/spawn/{id}).
    # Renders as a neutral "stopped" terminal state (not an error) and
    # preserves whatever partial output was streamed.
    user_stopped: bool = False
    # One-shot budget for auto-continue after an UNEXPECTED (non-user, non-
    # shutdown) asyncio cancellation — mirrors the main path's cancel recovery.
    _cancel_retry_used: bool = False
    # A fresh subagent whose selected agent surface already fills the model
    # window can recover only by rebuilding that surface. Retry once on a
    # dedicated runtime, where native skill projection and Tool Search setup run
    # afresh; a second overflow is terminal so a too-large agent cannot loop.
    _context_overflow_retry_used: bool = False
    # One re-run on a fresh runtime after a live backend lost this run's
    # session; a second loss is terminal.
    _session_not_found_retry_used: bool = False
    _force_dedicated: bool = False
    # The run's completed-ending CLAIM, a one-shot token like ``_finalized``:
    # taken synchronously by a whole answer (a successful complete event,
    # post-processed into ``result``) when no stop got there first (``done``,
    # ``_reap_started``, ``user_stopped``). From then on the ending is
    # completed whatever lands: every stop path treats the claim as it treats
    # ``done`` (Stop, a parent end and the reaper do nothing), and a cancel, the
    # shutdown or the deadline only ends the run's tail early. A respawn would
    # re-run finished work and a failure would discard a whole answer.
    _ending_claimed: bool = False
    # Set by the tail for a successful ending a reap in flight got to first:
    # True when a complete event ended the answer, False when the stream just
    # stopped. ``_run`` names the reap's ending from it.
    _answer_finished: bool = False
    # True while a cancelled run is draining an in-flight off-loop state.json
    # write worker (every off-loop writer). _run's
    # unexpected-cancel recovery gate reads it: on Python 3.10 a second outer
    # cancel can deliver that gate BEFORE the drain finishes (wait_for's
    # _cancel_and_wait awaits an interruptible bare future), and scheduling a
    # recovery writer while the worker is live re-opens the stale-overwrite race
    # the drain exists to close.
    _state_drain_active: bool = False
    # Set only on the synthetic marker `_conversation_busy` returns for a
    # conversation held by an abandoned state writer, so the two
    # retention callers can say "still settling a state write" instead of
    # promising a completion event that has already fired. The authoritative
    # record is `SubagentManager._abandoned_state_writers`, which survives
    # `evict_completed_agents` pruning a completed run out of `_agents`.
    _state_writer_abandoned: bool = False
    # True between an unexpected cancellation and the recovery respawn; the
    # _run finally block skips terminal finalization (subagent_done, on_done)
    # while set so the agent is not reported done mid-recovery.
    _recovering: bool = False
    # Ownership token for the one-time TERMINAL REPORT (`subagent_done` +
    # `_on_done`), claimed via `SubagentManager._claim_finalize`.
    _finalized: bool = False
    # Ownership token for the one-time SLOT RELEASE (`_running_count` decrement
    # + queue drain), claimed via `SubagentManager._release_slot`. Separate from
    # both `reaped` and `done`: whichever terminal path arrives first frees the
    # slot exactly once, so neither a reap that loses the report claim nor a
    # `_run` that sees `reaped` can leave `_running_count` inflated. At
    # 60-100 concurrent agents, a leaked slot starves the queue.
    _slot_released: bool = False
    # Generation the durable task row was claimed under (``kiro_crew.taskq``).
    # Every store write the run makes carries it, so a late write from a
    # superseded dispatch of the same id is fenced out. 0 = no store row.
    _taskq_generation: int = 0
    # Scheduler-core fields (session-start gate, lane-slot waits; see
    # docs/system-specs/modules/subagent.md § Lane-slot waits).
    # Time this start spent queued at its start queues in total, ms (the paused
    # part of the startup clock); 0 when every queue was free.
    _start_queue_wait_ms: float = 0.0
    # ``_start_queue_wait_ms`` measured on the monotonic clock, accumulated beside
    # it; the startup watchdogs subtract it from a span measured on that clock.
    _start_queue_wait_mono_ms: float = 0.0
    # True once the durable row was written ``running`` -- at the FIRST stream
    # event addressed to the run's own session, not at execution start, so a row is never
    # ``running`` while the session is still being created (RFC §4.4).
    _taskq_running_marked: bool = False
    # Provider built by a StartCollector's late adoption, handed to the run
    # that was waiting for it; None otherwise.
    _late_start_provider: Any = None
    # The live WaitRecord (as a dict) while this run has yielded its lane
    # slot for a wait; None while it holds a slot or has none to hold.
    _wait_record: Any = None
    # Set when the run's lane slot was yielded for a wait and a resume entry
    # is queued in admission; cleared when the slot is granted back.
    _resume_pending: bool = False
    # The run loop's wake-up for a yielded slot: set by ``resume_grant`` when
    # the pump hands the slot back, or by the dependency coordinator's
    # ``on_fail`` when the wait ended in failure (``_wait_failed`` names why).
    # None while the run holds its slot.
    _resume_event: Any = None
    _wait_failed: str = ""
    # The wake-up for a start released from the spawn-approval prompt and
    # waiting to be metered into startup by the pump (``_admit_released_start``).
    # None except during that wait.
    _start_release: Any = None
    # True once the terminal report's `_on_done` injection has RETURNED, i.e.
    # the outcome actually reached the parent. Distinct from `_finalized` (the
    # claim, taken before delivery is attempted) and from the "delivered"
    # tombstone (written later, after teardown). Read by `cancel_all()` to tell
    # a report cancelled BEFORE delivery — which must be made recoverable on the
    # next start — from one cancelled AFTER it, which must not be re-delivered.
    _reported_to_parent: bool = False
    # Set by the run's teardown when its kill left the process standing and no
    # tombstone exists yet (a completed run). The terminal report reads it: a
    # delivery the parent acknowledged writes the ``delivered`` tombstone with
    # ``leaked_process`` so the next start ends the process without a notice.
    _leaked_process: bool = False
    # The run's final ACP ``stop_reason`` and its ``classify_stop_reason``
    # class (a ``STOP_CLASS_*`` value), recorded by ``_run_inner`` on the
    # completion that ended the run
    # and carried on the ``subagent_done`` event so the parent sees WHY the run
    # ended, not only whether ``error`` is set. Empty until the run completes.
    stop_reason: str = ""
    stop_class: str = ""
    # True when the delivered ``result`` is a PARTIAL: text streamed before a
    # non-success completion (stall, cancel, error). The parent must not read
    # it as a finished answer.
    partial: bool = False
    # Continue-nudges already spent recovering a ``stalled`` / ``recovering``
    # completion in place (``STOP_RECOVERY_MAX_RETRIES`` budget, shared with
    # the main chat's ``slot._tool_stall_retries``).
    _stop_recovery_used: int = 0

    @property
    def made_no_tool_calls(self) -> bool:
        """Did this run neither emit a ``tool_call`` frame nor raise a permission request?

        The pair the replay gates already read as "no side effect happened":
        ``tool_count`` counts ``tool_call`` frames (and approved child
        escalations) and ``turns`` counts the run's own permission requests, so a
        backend that surfaces a gated tool only as a permission request still
        counts as having called a tool. Such a run wrote no file, ran no command
        and made no commit, whatever its text reports.
        """
        return self.tool_count == 0 and self.turns == 0

    @property
    def outcome(self) -> str:
        """Canonical three-way terminal outcome: 'stopped' | 'failed' | 'completed'.

        THE single source of truth for terminal-state classification. Consumers
        MUST use this (or the ``outcome`` field carried on every subagent_done
        emission) instead of re-deriving from ``error``-nullability — the
        legacy ``error ? failed : completed`` idiom silently misreports a
        user-stopped agent as completed. ``stopped``/``error`` remain on the
        wire for compatibility.
        """
        if self.user_stopped:
            return "stopped"
        if self.error:
            return "failed"
        return "completed"


# Callback: (subagent_info) -> None
AnnounceCallback = Callable[[SubagentInfo], Awaitable[None]]


def _injection_notice_outcome(info: "SubagentInfo") -> str:
    """One-sentence outcome line for the injection-failure fallback notice.

    ``notify_injection_failed`` fires whenever a terminal report could not be
    injected into the parent — for EVERY terminal state, not just successful
    completion. Asserting "finished" for a run that was stopped or rejected
    before it executed misdescribes the outcome, so the line branches on the
    record's canonical :attr:`SubagentInfo.outcome` with one before-start
    refinement per branch: ``_exec_started`` — the marker ``_run_inner`` sets
    when execution actually begins — is ``None`` exactly when the run never
    executed, which covers every spawn-rejection site (all of them construct
    their record without it) with no wording contract between ``error``
    strings and this notice. The "no result to deliver" phrasings are guarded
    on the absence of any output so they can never contradict the result-path
    recovery hint.

    The completed branch names no delivery MECHANISM. Only two of the six
    ``notify_injection_failed`` call sites pass a timeout; the rest pass
    "provider dead after prompt-busy retries", "ACP process died", a raw
    ``str(exception)`` and the last injection-failure reason. Since the notice
    prints ``reason`` on the line directly above this one, asserting a timeout
    here would contradict it — and the reader is an LLM deciding whether to
    retry. The cause belongs to ``reason``; this line states only the outcome.

    Pure function of the record, unit-tested per branch.
    """
    never_ran = info._exec_started is None and not info.result and not info.result_path
    outcome = info.outcome
    if outcome == "stopped":
        if never_ran:
            return "The run was stopped before it started, so there is no result to deliver."
        return "The run was stopped before it completed."
    if outcome == "failed":
        if never_ran:
            return "The run failed before it started, so there is no result to deliver."
        return "The agent failed before a result could be delivered."
    return "The agent finished, but its result could not be delivered."


# Event callback: (event_type, info, extra_data) -> None
SubagentEventCallback = Callable[[str, "SubagentInfo", dict], Awaitable[None]]


def _context_groups_of(info: "SubagentInfo") -> frozenset[str]:
    """The switchable context groups this run KEEPS.

    One source of truth for the run's scope, shared by the ``build_message``
    call that applies it and the ``state.json`` record a continuation reads it
    back from, so the two cannot drift.
    """
    return frozenset(
        group
        for group, on in (
            (CONTEXT_GROUP_MEMORY, info.include_memory),
            (CONTEXT_GROUP_LESSONS, info.include_lessons),
            (CONTEXT_GROUP_PROJECT, info.include_project),
        )
        if on
    )


def _context_groups_field(info: "SubagentInfo") -> str:
    """``state.json`` encoding of the run's scope: comma-joined, sorted."""
    return ",".join(sorted(_context_groups_of(info)))


class ToolApprovalCallback(Protocol):
    async def __call__(self, event: LLMEvent, parent_session_key: str = "") -> bool:
        pass


class SpawnApprovalUnreachable(Exception):
    """A spawn-approval prompt has no surface that could ever answer it.

    Raised BY a :class:`SpawnApprovalCallback`, at the point it would otherwise
    park, and handled by the spawn gate in ``subagent_manager/admission/gate.py``.

    Why an exception rather than a ``False`` return, and why the callback rather
    than the gate decides:

    * ``False`` already means "a human refused", and the two must not collapse:
      a refusal is a decision, this is the absence of anyone who could decide.
      They want different prose, and only this one is a misconfiguration.
    * The gate cannot compute the answer. Every non-human auto-approve shortcut
      the callback owns -- ``hooks.auto_approve_sources``, the CLI ``--approval``
      mode, the YOLO override, slot trust -- is evaluated inside the callback and
      never reaches the gate's cascade, so a gate-side probe would have to
      re-derive all four and would reject spawns those rungs mean to allow (the
      ``auto_approve_sources`` opt-in is the documented workaround). Raising from
      the callback puts the check where "we are about
      to park with nobody attached" is the only remaining possibility.

    The message SHOULD name the surface that was missing ("no dashboard client is
    connected"), because the raiser is the only party that knows what the
    surfaces are. The gate quotes it and adds the config rungs, which are the
    gate's own; that split is what keeps the gate's prose from going stale when
    channel-side delivery lands.

    A callback that never raises it keeps today's behaviour unchanged.
    """


class SpawnApprovalCallback(Protocol):
    async def __call__(
        self, request_id: str, description: str, parent_session_key: str = ""
    ) -> bool:
        pass


# ── Delivery routing state: enumerated from the PRODUCING side ────────────────
#
# Every ``SubagentInfo`` attribute that the four modules owning terminal-outcome
# routing WRITE -- ``subagent_manager/terminal.py``, ``subagent_manager/waves.py``,
# ``subagent_manager/cancellation.py`` and ``slack/gateway.py`` -- classified by what it
# says about whether the outcome has reached the parent.
#
# The list exists because "has this run's outcome reached its parent" has more than one
# representation, and reading only the obvious one was wrong four separate times. A
# parent-end teardown has to suppress the delivery of a run whose parent is gone, so a
# representation it does not know about is a delivery that lands in a conversation that
# ended -- and the injector CREATES a session when none is live, so that delivery rebuilds
# the conversation the teardown just took down.
#
# ``PARKS_WHEN_SET``  -- truthy means the outcome is parked somewhere and has not landed.
# ``PARKS_WHEN_UNSET`` -- falsy means it has not landed; truthy means it has.
# ``NOT_DELIVERY_STATE`` -- written by those modules but says nothing about delivery.
#
# ``test_the_delivery_parked_states_are_enumerated_from_the_producers`` recomputes the
# write set from those modules' AST and fails when it stops matching this table, so a new
# field written by any of them cannot be added without being classified here.
PARKS_WHEN_SET = "parks-when-set"
PARKS_WHEN_UNSET = "parks-when-unset"
NOT_DELIVERY_STATE = "not-delivery-state"

DELIVERY_ROUTING_FIELDS: "dict[str, str]" = {
    # The gateway parked this member's per-agent injection for the wave digest. Two
    # fields on purpose: the flag is the restart-safety contract the run loop reads, the
    # timestamp is the hold-deadline sweep's only input, and the sweep must not mutate the
    # flag. Either being set means the result is not in the parent's context.
    "_digest_held": PARKS_WHEN_SET,
    "_digest_held_at": PARKS_WHEN_SET,
    # The held SIBLINGS whose delivery tombstones this member owes once its digest is
    # handed off. Non-empty means other runs' deliveries are parked ON this record.
    "_digest_settle_deliveries": PARKS_WHEN_SET,
    # The announce sits in the parent's slot queue because the slot was busy. Delivery is
    # not consumption: a turn has to drain it.
    "_delivery_queued": PARKS_WHEN_SET,
    # Set the moment ``_on_done`` RETURNS. Its truth is the only positive evidence the
    # outcome reached the parent -- which is why it reads the other way round, and why
    # reading it ALONE was wrong: two routes above return having merely parked the work.
    "_reported_to_parent": PARKS_WHEN_UNSET,
    # A synthetic record ``force_digest_flush`` builds to release an expired hold. It is a
    # CARRIER of a future injection rather than a member with a parked outcome, and it
    # carries a fresh id, so an id-keyed gate can never recognise it -- which is why the
    # wave hold is disarmed at its source (``_expired_digest_holds``) instead.
    "_digest_flush_only": NOT_DELIVERY_STATE,
    # A memory-wait expiry's report is owed in the store. It says who clears that mark,
    # not where the outcome is: the parked states above say that.
    "_report_owed": NOT_DELIVERY_STATE,
    # The route positively delivered the report. Set inside ``_on_done``, before
    # ``_reported_to_parent``, it decides only whether a memory-wait expiry's
    # owed mark may be cleared; the teardown gate reads the states above.
    "_report_delivered": NOT_DELIVERY_STATE,
    # An injection was given up on. Nothing in this process delivers the report
    # again, so there is no parked delivery for a teardown to stop; it decides
    # only whether the run's ``delivered`` tombstone is written.
    "_report_undelivered": NOT_DELIVERY_STATE,
    # Run bookkeeping these modules also write. None of them says where an outcome is.
    "_finalized": NOT_DELIVERY_STATE,
    "_reap_reason": NOT_DELIVERY_STATE,
    "_reap_started": NOT_DELIVERY_STATE,
    "_recovering": NOT_DELIVERY_STATE,
    # Recovery clears the first attempt's startup clocks before waiting to
    # launch its replacement. They govern startup reaping, not delivery.
    "_exec_started": NOT_DELIVERY_STATE,
    "_exec_started_mono": NOT_DELIVERY_STATE,
    "_startup_deadline_stamp": NOT_DELIVERY_STATE,
    # Context-overflow recovery clears the retired shared runtime's identity
    # before it waits to start the dedicated replacement. These fields govern
    # process lifecycle and sampling, not where terminal output was delivered.
    "_pid": NOT_DELIVERY_STATE,
    "_session_sharing": NOT_DELIVERY_STATE,
    "_shared_provider": NOT_DELIVERY_STATE,
    # The recovery respawn resets the dead process's RSS readings so the spawn
    # guard prices the fresh process as warming; memory sizing, not delivery.
    "_rss_generation": NOT_DELIVERY_STATE,
    "_rss_samples": NOT_DELIVERY_STATE,
    "_slot_released": NOT_DELIVERY_STATE,
    "_stop_origin": NOT_DELIVERY_STATE,
    "done": NOT_DELIVERY_STATE,
    "elapsed": NOT_DELIVERY_STATE,
    "error": NOT_DELIVERY_STATE,
    "last_rss_gb": NOT_DELIVERY_STATE,
    # Owned continuation work is cancelled separately when a parent ends; its presence
    # says nothing about whether this run's terminal outcome reached that parent.
    "pending_followups": NOT_DELIVERY_STATE,
    "reaped": NOT_DELIVERY_STATE,
    "result": NOT_DELIVERY_STATE,
    "streaming_text": NOT_DELIVERY_STATE,
    "user_stopped": NOT_DELIVERY_STATE,
}

# The modules the table is derived from. Named here so the test and the table cannot
# disagree about which producers were read.
DELIVERY_ROUTING_MODULES: tuple[str, ...] = (
    "subagent_manager/terminal.py",
    "subagent_manager/waves.py",
    "subagent_manager/cancellation.py",
    "slack/gateway.py",
)


def delivery_is_parked(info: "SubagentInfo") -> bool:
    """True when this run's outcome has not reached its parent.

    Reads :data:`DELIVERY_ROUTING_FIELDS` rather than naming fields inline, so the
    predicate and the classification cannot drift -- the drift is what let a parked
    representation through on four separate rounds.

    The union is deliberately conservative. Answering True for a run whose delivery has in
    fact landed costs nothing: the gate only SKIPS an injection, and a run that already
    delivered does not inject again. Answering False for a parked one rebuilds a retired
    conversation.
    """
    for field_name, rule in DELIVERY_ROUTING_FIELDS.items():
        value = getattr(info, field_name, None)
        if rule == PARKS_WHEN_SET and value:
            return True
        if rule == PARKS_WHEN_UNSET and not value:
            return True
    return False


def _audit_ids(ids: "Iterable[str]", cap: int = 20) -> str:
    """Render run ids for the parent-end audit line, bounded.

    Declared on the facade rather than in the component that logs, because a component
    method's module-level names resolve against THIS module's globals at runtime -- a
    helper defined beside its caller raises ``NameError`` there.

    A wave can carry more ids than one log line should hold, and a silently truncated list
    is worse than a count: it reads as the whole set.
    """
    listed = list(ids)
    if not listed:
        return "none"
    if len(listed) <= cap:
        return ",".join(listed)
    return ",".join(listed[:cap]) + f",+{len(listed) - cap}-more"


# How long the delivery gate remembers a teardown-cancelled run id when nothing has
# explicitly discarded it.
#
# A BACKSTOP, not the primary rule. The primary rule is that the gate keeps an id until
# that run's delivery has actually been suppressed, which is what ``_report_terminal_impl``
# discards on -- so the ordinary case never depends on this number. It exists for a marked
# run that never reaches a terminal at all.
#
# A day rather than an hour, because an approval-parked run is deliberately NOT cancelled
# (the approval is a person's decision to make) and a person can take far longer than an
# hour to answer. An hour let a later teardown prune the mark while such a run was still
# waiting, and its completion then injected into whatever the key served by then.
_TEARDOWN_GATE_TTL_SECS = 86400.0


class _AgingIdSet:
    """A membership set of run ids that forgets an entry once it is OLD, never when full.

    Age since MARKING is the only eviction rule, and the reason is that the alternative
    is unsafe. A CAPACITY rule evicts by arrival order regardless of whether the run
    could still announce, so a single parent with more queued children than the capacity
    would evict its own earliest ids while its reports were still being spawned -- and
    those reports then walk through the gate and rebuild the conversation the teardown
    took down. An age rule cannot do that: the TTL is chosen to exceed every window in
    which a marked run has an announce left.

    Age is also why the lifetime is not tied to the run's ``_agents`` record. That record
    is popped while a run is still tearing down (a dashboard "clear completed" does it),
    so discarding on the pop would disarm the gate while the run can still announce --
    the same reason ``_teardown_gates`` outlives those records.

    Not an LRU: a read must not extend an entry's life, or a hot gate check on one id
    would keep others alive past the point the TTL is reasoned about.
    """

    __slots__ = ("_marked_at", "_ttl")

    def __init__(self, ttl_secs: float) -> None:
        self._marked_at: dict[str, float] = {}
        self._ttl = max(1.0, float(ttl_secs))

    def _prune(self, now: float) -> None:
        cutoff = now - self._ttl
        if not self._marked_at:
            return
        # Insertion-ordered, and marking times are monotonic, so the expired entries are
        # a PREFIX: stop at the first live one instead of scanning the whole dict.
        for agent_id, marked_at in list(self._marked_at.items()):
            if marked_at > cutoff:
                break
            del self._marked_at[agent_id]

    def add(self, agent_id: str) -> None:
        if not agent_id:
            return
        now = time.monotonic()
        self._prune(now)
        self._marked_at.pop(agent_id, None)
        self._marked_at[agent_id] = now

    def update(self, agent_ids: "Iterable[str]") -> None:
        for agent_id in agent_ids:
            self.add(agent_id)

    def discard(self, agent_id: str) -> None:
        self._marked_at.pop(agent_id, None)

    def __contains__(self, agent_id: object) -> bool:
        return agent_id in self._marked_at

    def __iter__(self) -> "Iterator[str]":
        return iter(tuple(self._marked_at))

    def __len__(self) -> int:
        return len(self._marked_at)


class SubagentManager:
    """Spawn and track isolated background agents."""

    _COMPONENT_TYPES = {
        "_monitor": OrphanStallMonitor,
        "_terminal": TerminalCoordinator,
        "_admission": SpawnAdmissionCoordinator,
        "_continuation": ContinuationCoordinator,
        "_waves": WaveDigestCoordinator,
        "_run_events": RunEventCoordinator,
        "_cancellation": CancellationCoordinator,
    }
    _reconcile_task: asyncio.Task | None  # type: ignore[type-arg]

    def __getattr__(self, name: str) -> Any:
        """Lazily compose a missing coordinator for minimal facade construction."""
        component_type = self._COMPONENT_TYPES.get(name)
        if component_type is None:
            raise AttributeError(name)
        component = component_type(self)
        object.__setattr__(self, name, component)
        return component

    def __init__(
        self,
        sessions: SessionManager,
        ctx_builder: ContextBuilder,
        on_done: AnnounceCallback | None = None,
        max_concurrent: int = _MAX_CONCURRENT,
        default_turn_limit: int = _TURN_LIMIT,
        default_timeout: int = _TIMEOUT_SECS,
        startup_timeout: int = 0,
        stall_idle_secs: int = _STALL_IDLE_SECS,
        on_tool_approval: ToolApprovalCallback | None = None,
        on_tool_approval_factory: (
            Callable[["SubagentInfo"], Callable[[LLMEvent], Awaitable[bool]]] | None
        ) = None,
        on_spawn_approval: SpawnApprovalCallback | None = None,
        is_yolo: Callable[[], bool] | None = None,
        on_event: SubagentEventCallback | None = None,
        on_orphan_notify: Callable[..., Awaitable[bool | str]] | None = None,
        on_orphan_dm: Callable[[str], Awaitable[bool]] | None = None,
        completion_keep: str = "head",
        completion_keep_chars: int = COMPLETION_KEEP_DEFAULT_CHARS,
        memory_mode_for_session: Callable[[str], str] | None = None,
        defer_queue_dispatch: bool = False,
    ):
        self._sessions = sessions
        # Run ids a parent-end teardown stopped. Keyed by ID rather than carried
        # only on the run record because a QUEUED run has no ``_agents`` row at
        # all: ``_report_queued_stop`` builds a fresh ``SubagentInfo`` for its
        # synthetic terminal, which would default the flag to False and walk
        # straight through the delivery gate. The gate reads this set, so live
        # runs, queued runs and follow-up synthetics are all covered by the one
        # place the teardown writes.
        self._teardown_cancelled_ids = _AgingIdSet(_TEARDOWN_GATE_TTL_SECS)
        # The parent-end store sweep's fence (``CancellationCoordinator``
        # ``note_teardown_snapshot``): per parent key, the ids of the rows
        # accepted for it since its latest snapshot -- a successor's, which the
        # sweep must spare. Pending here until the cancel takes it, then in
        # ``_teardown_store_sweeps`` until that cancel is done, recording all
        # along. Accept-ordered, not clock-ordered: a wall clock stepped back
        # would stamp a successor's row "before" the snapshot. Under the lock
        # because the accept that records runs on the store's writer thread.
        # A snapshot whose cancel never ran is replaced by the next one.
        self._teardown_store_fences: dict[str, set[str]] = {}
        self._teardown_store_sweeps: list[tuple[str, set[str]]] = []
        # TENTATIVE admission fences the identity sweep opens before it decides
        # retire-vs-defer: they record admissions (so a concurrent /api/spawn is
        # seen by the retire-vs-defer re-check) but do NOT drive delivery
        # suppression, because the parent may be SPARED. Only a committed teardown
        # fence (``_teardown_store_fences``) suppresses an expiring row's delivery.
        self._identity_sweep_fences: dict[str, set[str]] = {}
        # Sweeps whose store read was refused: ``(key, fence, verb)``. Each fence
        # stays in ``_teardown_store_sweeps`` (recording, and gating an expiry of
        # the retired rows) until a reaper sweep's retry has read the store.
        self._teardown_sweeps_owed: list[tuple[str, AbstractSet[str], str]] = []
        self._teardown_fence_lock = threading.Lock()
        self._memory_mode_for_session = memory_mode_for_session
        self._ctx_builder = ctx_builder
        self._on_done = on_done
        #: While True the staggered pump admits nothing: the durable rows that
        #: survived a restart wait for :meth:`release_queue_dispatch`. The
        #: gateway sets it so the boot drain (``start_reaper`` /
        #: ``_initialize_taskq``) cannot claim a row during the memory barrier;
        #: a manager built without it (tests, tools) pumps as soon as it can.
        self._queue_dispatch_held = bool(defer_queue_dispatch)
        #: Set by the pump the first time it refuses a pass under the hold, so
        #: a hold that is never released leaves one debug line behind instead
        #: of the silent "accepted, never claimed" queue this fix diagnoses.
        self._queue_dispatch_hold_logged = False
        # ``_max_concurrent`` is the EFFECTIVE cap every admission read site
        # consults: ``min(user cap, adaptive cap)``. The user's resolved cap
        # (``agent.max_subagents`` / the auto ceiling) is the ceiling in
        # ``_user_max_concurrent``; the adaptive controller lowers the runtime
        # value through :meth:`set_effective_cap` and never writes the ceiling.
        self._user_max_concurrent = max_concurrent
        self._adaptive_cap: int | None = None
        self._max_concurrent = max_concurrent
        #: Gates OUTSIDE this manager that are bounded by ``_max_concurrent``
        #: and cannot see it change (the runner lane -- TaskRunner steps and
        #: workflow ``ctx.agent()`` calls). Registered by the gateway through
        #: :meth:`set_cap_raise_listener`; None everywhere else.
        self._cap_raise_listener: Callable[[], object] | None = None
        self._default_turn_limit = default_turn_limit
        self._default_timeout = default_timeout if default_timeout > 0 else _TIMEOUT_SECS
        # A positive value pins the window; 0 derives it from the budget.
        self._startup_timeout_override = startup_timeout if startup_timeout > 0 else None
        self._stall_idle_secs = stall_idle_secs if stall_idle_secs > 0 else _STALL_IDLE_SECS
        self._on_tool_approval = on_tool_approval  # fallback for non-auto sessions
        self._on_tool_approval_factory = on_tool_approval_factory
        self._on_spawn_approval = on_spawn_approval
        self._is_yolo = is_yolo
        self._on_event = on_event
        # Awaited by every run just before it executes, after the run is already
        # visible as pending work for its parent. The dashboard installs one that
        # waits out a branch switch on the run's repository
        # (``dashboard.repo_checkout_guard.wait_for_subagent_checkout``).
        self.pre_run_gate: Callable[[SubagentInfo], Awaitable[None]] | None = None
        # Orphan-notification delivery (gateway-wired). ``on_orphan_notify``
        # injects a message into the parent dashboard slot (returns True on
        # success); ``on_orphan_dm`` is the owner-DM / notification fallback.
        self._on_orphan_notify = on_orphan_notify
        self._on_orphan_dm = on_orphan_dm
        # Set by cancel_all() so shutdown-driven task cancellations never
        # trigger the one-shot unexpected-cancel auto-continue.
        self._shutting_down = False
        self._completion_keep = completion_keep
        self._completion_keep_chars = completion_keep_chars
        self._running_count = 0
        # Learned settled RSS per cost bucket (GiB), the dedicated start
        # projection's input. Replaced whole, off the loop, by the reaper's cost
        # sweep (``_refresh_learned_settled``); the gate only reads it, so it
        # never opens the cost log on the event loop.
        self._learned_settled_gb: dict[str, float] = {}
        # Set when a run records a settled reading (and at start), so the sweep
        # re-reads the cost log only when it can have changed what the map holds.
        self._learned_settled_dirty = True
        # Monotonic time of the last re-read; ``None`` until the first, so the
        # first sweep always reads.
        self._learned_settled_read_mono: float | None = None
        # Monotonic time of the oldest read whose figures the map may still
        # hold: an incomplete read keeps figures it could not confirm, but not
        # past the reading horizon.
        self._learned_settled_basis_mono: float | None = None
        # Claims admitted but not registered yet: (checked price, priced shared),
        # keyed by run id. Set with the ClaimPoint reservation, popped by the
        # re-entry that registers it or ``release_reservation``.
        self._claim_prices: dict[str, tuple[float, bool]] = {}
        # One start admitted at the shared price re-checks the floor at a time,
        # so waiters do not each count the others' raised prices and all hold.
        self._dedicated_topup_lock = asyncio.Lock()
        # Strong refs to in-flight shielded terminal reports (see
        # `_spawn_terminal_report`); drained in `cancel_all`.
        self._report_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # follow_up watchers (spawn_steer mode="follow_up"), keyed by run id.
        # Manager-OWNED on purpose: these tasks can spawn a brand-new run
        # (continue_conversation), so per this module's containment contract
        # (see _schedule_cancel_recovery) they must be reachable by
        # cancel_all() — a watcher parked in the global _safe_fire set would
        # survive shutdown and dispatch against a closing SessionManager.
        self._followup_watchers: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Run id -> parent session for each live watcher. Kept separately from
        # `_agents` because completed-record eviction can remove the run while
        # its watcher still owns a follow-up dispatch.
        self._followup_watcher_parents: dict[str, str] = {}
        # Run id -> the exact record captured by each watcher. Completed-record
        # eviction may remove it from ``_agents`` while queued follow-ups still
        # own a continuation, so parent-end cancellation needs this independent index.
        self._followup_watcher_infos: dict[str, SubagentInfo] = {}
        # task -> the agent whose terminal report it is delivering
        self._report_owners: dict[asyncio.Task, SubagentInfo] = {}  # type: ignore[type-arg]
        # Whether the last queued listing was partial, so the bridge logs the
        # transition into one once rather than per request.
        self._queued_listing_partial = False
        # A post-claim store outage cannot return an ADMITTED row to the ordinary
        # refill, which reads only claimable rows. Keep that generation, its
        # reserved slot, and its re-entry callback until a later pump pass can
        # revalidate it. One entry requires one already-reserved slot, so this map
        # is bounded by the effective concurrency cap for the process lifetime.
        self._retained_claims: dict[
            str,
            tuple[
                ClaimPoint,
                int,
                Callable[[tuple[int, bool, str]], Any],
                dict[str, Any],
            ],
        ] = {}
        self._retained_claim_retry_handle: asyncio.TimerHandle | None = None
        self._last_spawn_ts: float = 0.0  # monotonic time of the last actual start (stagger gate)
        self.hook_store: Any = None  # Optional ScriptHookStore, set by server.py
        self._agents: dict[str, SubagentInfo] = {}
        # Continuable conversations: session_key ("subagent:<conv-id>") →
        # last-used ``time.monotonic()`` reading. Drives the reaper's idle-TTL
        # sweep. Rebuilt from state.json (keep=True runs) on the reaper's first
        # pass after a gateway restart, which turns each stored wall-clock stamp
        # into an age, so promoted conversations stay owned by
        # the TTL sweep across restarts; a spawn_continue on an unknown key
        # also re-registers it on demand.
        self._conversations: dict[str, float] = {}
        self._conv_registry_rebuilt = False
        # Run ids whose bounded state-write drain EXPIRED, so a pool worker is
        # still live and its stale whole-file rewrite would roll back the
        # retention `keep` a promote / release writes on the loop.
        # `_conversation_busy` reports these as held, which defers both retention
        # writes past the worker. Keyed by run id, each entry the run's workers
        # still writing; each worker's own done-callback removes itself, and the
        # id goes with the last one, so a run can hold several. It lives on the
        # MANAGER, not on the run's SubagentInfo, because `evict_completed_agents`
        # prunes completed runs out of `_agents` and an eviction must not silently
        # release the hold.
        self._abandoned_state_writers: dict[str, set[asyncio.Future[Any]]] = {}
        # Conversation ids whose continuation start is in flight: the start claim for
        # every continuation, held here and not on the opening run's registry record,
        # because ``evict_completed_agents`` can drop that record (or a restart left
        # none) while a start awaits. Taken by ``_claim_continuation`` and released
        # by ``_settle_continuation``.
        self._continuations_starting: set[str] = set()
        # state.json is the source of truth for retention: give the
        # SessionManager's in-memory continuable cache a disk fallback so a
        # cache miss (restart window) cannot demote a promoted conversation.
        try:
            self._sessions.set_continuable_fallback(self._keep_recorded_on_disk)
        except AttributeError:
            pass  # test doubles without the setter
        self._tasks: dict[str, asyncio.Task] = {}  # type: ignore[type-arg]
        # Teardown gates for runs whose terminal report has started, keyed by id and
        # OUTLIVING both dicts above. A "delivered" tombstone excludes a folder from
        # restart orphan reconciliation, so it must never be written while the run's
        # child is still being killed -- and the settlement that writes it can happen
        # outside the run (the parent's queue drain), long after a
        # dashboard "clear completed" / "cancel" has popped BOTH ``_agents`` and
        # ``_tasks`` for a done-but-still-tearing-down run. Reading the gate from
        # here, rather than inferring "record gone means teardown finished", is what
        # makes that inference unnecessary. Removed by the same ``finally`` that sets
        # the event, so a missing entry always means "nothing left to wait for".
        self._teardown_gates: dict[str, asyncio.Event] = {}
        #: run id -> the kill handles of every process its session key named,
        #: RETAINED across the reset that pops the session from the map (see
        #: :class:`kiro_crew.process_identity.ProcessHandle`).
        #: Written by :meth:`_retain_process_handles` from whichever teardown path
        #: reaches the reset first -- the run's own ``finally`` or the reaper --
        #: and read by the other on a session-map miss, so a force-stop that
        #: arrives while the first reset is hanging can still name, verify and
        #: signal the process. Cleared when the path that holds it has decided
        #: (the handles were consumed by the kill, or the survivor check found
        #: the processes gone); an entry that outlives its run is one small record.
        self._process_handles: dict[str, list[ProcessHandle]] = {}
        # Queued spawns store the FULL spawn() kwarg set (not just a 5-tuple), so a
        # drained spawn preserves approval_mode / silent / model / allowed_tools / bare —
        # dropping them made a queued headless/auto spawn hit the deny-by-default gate and
        # a queued silent spawn emit output. See _drain_queue.
        self._queue: list[dict[str, Any]] = []
        # parent_session_key -> the last wait the gate labelled for that parent
        # (``{"reason": <QUEUED_REASON_*>, "available_gb"?, "required_gb"?}``).
        # ``_emit_queue_depth`` attaches it to every ``subagent_queued`` it sends
        # while the parent still has rows waiting, and forgets it at depth 0: the
        # drain and the cancel paths re-emit the depth without a verdict of their
        # own, and without this memory each re-emit would flip a memory-deferred
        # wave back to the default (concurrency) text.
        self._queue_wait: dict[str, dict[str, Any]] = {}
        # parent_session_key -> its one in-flight coalesced depth emit. An entry
        # lives only while that emit's read task does (``_emit_queue_depth``;
        # the task's own completion drops it), so it is bounded by the parents
        # with a read in flight and needs no eviction when a parent ends.
        self._queue_depth_emits: dict[str, _PendingDepthEmit] = {}
        # parent_session_key -> its one armed delayed re-read after a depth read
        # the store could not answer; cancelled by the next frame for that
        # parent and at shutdown, so at most one per parent.
        self._queue_depth_retries: dict[str, _PendingDepthRetry] = {}
        # Rows a ``spawn_async`` caller is still admitting that the gate has
        # already queued (deferred or behind the cap): the refill still leaves
        # them to their caller, but the queue-depth chip counts them. Marked
        # and cleared by that call alone (``_spawn_async_accepted``,
        # ``spawn_async``'s ``finally``), so always a subset of
        # ``_admitting_ids``.
        self._admitting_waiting: set[str] = set()
        # When the refill last reported a deferred row stuck past its wake
        # (monotonic); the report is rate-limited (``_refill_schedule_wake``).
        self._overdue_wake_warned_at: float = float("-inf")
        # The macOS kernel memory-pressure hold (``_memory_pressure_hold``): the
        # level it last warned at (None between episodes, so each episode warns
        # again), whether it applied at the last read, each held root start's
        # first-held time, the rows whose wait ran out (released, said once),
        # and the one re-check timer while it applies.
        self._pressure_hold_level: int | None = None
        self._pressure_hold_on = False
        # When the current kernel episode (level WARN or worse) was first read,
        # and whether it has outlived the bound, which ends the starts it would
        # hold, never started, until the level eases.
        self._pressure_episode_since: float | None = None
        self._pressure_episode_spent = False
        self._pressure_episode_read_at = 0.0
        self._pressure_holds: dict[str, float] = {}
        self._pressure_hold_expired: set[str] = set()
        # The pressure hold's per-lane progress. A lane is the parent session a
        # root row waits behind (``taskq.lanes.lane_key_for``), the one the
        # pump's weighted pick serves FIFO. A held row's bound counts from the
        # later of its first hold and its lane's last root start, so a lane
        # that keeps starting rows never expires the rows waiting behind them,
        # whichever wave they came from, and a lane none of whose rows starts
        # still ends them after the bound. Another lane's starts never extend
        # it; automation keys all share ``SYSTEM_LANE``, one FIFO queue, so
        # they are one lane here too. agent_id -> its lane, lane -> how many
        # rows it holds, and
        # lane -> its last root start (monotonic), kept only while the lane
        # holds a row; ``_forget_pressure_clock`` drops all three.
        self._pressure_hold_lanes: dict[str, str] = {}
        self._pressure_lane_held: dict[str, int] = {}
        self._pressure_lane_started_at: dict[str, float] = {}
        # agent_id -> (closed parked time, current park's start, its planned
        # end), all integer ``time.monotonic_ns()`` nanoseconds, for a start
        # with no durable row that waits on the memory floor: the store sweep
        # cannot see it, so the gate bounds it from this clock
        # (``agent.subagent_queue_max_wait_secs``) at each re-park.
        self._floor_waits: dict[str, tuple[int, int, int]] = {}
        self._pressure_recheck_handle: asyncio.TimerHandle | None = None
        # agent_id -> the ``approval_mode`` a durable row was accepted with, for
        # as long as it waits: the store never carries it, so a window refill
        # restores it from here (``_refill_apply``).
        self._held_approval_modes: dict[str, str] = {}
        # Durable rows whose last defer was the memory floor's (``low_memory``):
        # the store keeps only ``next_run_at``, so a window refill stamps the
        # entry as a floor wait from here (``_refill_apply``) and the pump's pick
        # leaves it to the gate, which re-checks the floor before the pressure
        # hold. Process-local, as the hold's own clocks are.
        self._floor_deferred_ids: set[str] = set()
        # The starts this process holds back for memory, from their first
        # deferral to their admission, start or end (subagent.md, *Memory
        # waits: event wake and the per-lane share*): what the event wake
        # brings forward, what the per-lane share reads, and what keeps the
        # sampler running -- it runs only while this is not empty.
        self._memory_waits: dict[str, "MemoryWait"] = {}
        # The fit pass in flight (``wake_memory_waits``), and whether a wake
        # arrived during it, so one more pass runs after it.
        self._memory_fit_task: "asyncio.Task[None] | None" = None
        self._memory_wake_again = False
        self._memory_sampler_handle: asyncio.TimerHandle | None = None
        self._memory_sample_task: "asyncio.Task[None] | None" = None
        # Rows the pump has popped from the window but not yet claimed. Their
        # durable state is still QUEUED, so without this set every store-backed
        # depth read between pop and claim counts them as waiting.
        self._dispatching_ids: set[str] = set()
        # The same popped ids, read by ``is_queued`` (the serial-lock
        # done-probe) while a row is in neither ``_queue`` nor ``_agents``:
        # without it the probe reads such a row as finished and releases the
        # guard. It is the ONLY record of a popped non-durable row; a durable
        # one is also in the store's unstarted index. Marked with
        # ``_dispatching_ids`` but outlives it across a retained claim, which
        # still holds pending work until the retry registers or refuses it
        # (``retry_retained_claims``).
        self._dispatch_window_ids: set[str] = set()
        # A popped row with no durable record (incognito or temporary memory,
        # or the task queue off), keyed by id, while the coroutine pump awaits
        # its off-loop reads before the gate starts it. It is the row's ONLY
        # copy then: a stop finds it here (``_unqueue``), the pump re-checks it
        # after each await and does not start a row a stop took, and a dispatch
        # that raises puts it back in the window instead of losing it. A durable
        # row needs none of this: the store keeps it and the claim re-checks
        # the stop.
        self._undurable_in_dispatch: dict[str, dict[str, Any]] = {}
        # parent_session_key -> how many Stop all calls for it are in flight.
        # While one is, the refill windows none of that parent's rows: a fetch
        # queued before the stop would otherwise put back rows the stop is
        # cancelling, or window store-only rows its pending read then skips.
        self._stopping_parents: dict[str, int] = {}
        # agent_id -> the per-row answer of the Stop all batch that is
        # cancelling its row and has not reported it yet. A single ``cancel``
        # of such a row joins the batch instead of cancelling and reporting it
        # again (``cancel_impl``), and a claim whose re-read the cancel may
        # have overtaken waits for it and re-reads (``claim_and_start``): the
        # row is in neither ``_queue`` nor ``_agents``.
        self._batched_stops: dict[str, asyncio.Future[Any]] = {}
        # agent_id -> the parent of each row filed in ``_batched_stops``, for
        # as long as it is filed: a parent-end teardown's snapshot gates the
        # batch's report of a row that is in neither ``_queue`` nor ``_agents``.
        self._batched_stop_parents: dict[str, str] = {}
        # Batch ids whose spawn_batch_started event has already fired.
        self._seen_batches: set[str] = set()
        # Submission accounting per wave: batch_id -> (submitted, expected).
        # Guards the wave digest against firing before every member's POST has
        # arrived — a fast-failing first member must not let the completion
        # fallback see "no pending members" while later submissions are still
        # in flight. Pruned by finalize_batch().
        self._batch_submitted: dict[str, list[int]] = {}
        # Wave liveness: last submission-progress time.monotonic() per batch_id,
        # aged against the reaper's monotonic reading so a wall-clock step cannot
        # move it.
        # Drives the reaper's stuck-wave backstop (a wave with lost
        # submissions and no progress is force-reconciled). Pruned by
        # finalize_batch alongside _batch_submitted.
        self._batch_progress_ts: dict[str, float] = {}
        # parent_session_key -> the ``subagent_queued`` depth last published for
        # it, recorded by ``_fire_event`` and serialized on each slot as
        # ``subagents_queued`` (see ``subagent_manager.published_depth``).
        self._published_depths = PublishedQueueDepths()
        # Done-but-unreported holds per wave: batch_id -> agent ids whose
        # ``done`` has flipped but whose terminal report the completion
        # consumer has not yet consumed. Held OUTSIDE the agent records
        # because ``_agents`` membership is operator-mutable (DELETE
        # /api/spawn pops done members) -- the wave-close fallback must not
        # lose its hold to a concurrent clear. Armed by the
        # report machinery at the done-flip, disarmed by the consumer when
        # the contribution lands or structurally when a report ends without
        # reaching the consumer. Pruned by finalize_batch.
        self._reports_in_flight: dict[str, set[str]] = {}
        self._reaper_task: asyncio.Task | None = None  # type: ignore[type-arg]
        # Every ``_force_reap`` that runs OUTSIDE the reaper task -- a dashboard
        # Stop or a parent-end cancel, each awaited inside its
        # caller's own task. ``cancel_all`` cancels these beside the reaper task
        # so their cancellation arms finish the record and release the report
        # inside the shutdown drain: a reap the shutdown never touched sat in
        # its hanging reset until the gateway's budget hard-exited the process,
        # with its report waiting on a gate nobody released.
        self._reap_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        # The one reap in flight per run, by agent id: a future its owner settles
        # on every exit. A second stop path arriving while it is pending (a
        # dashboard Stop racing a deadline reap, or the reverse) joins it
        # instead of running a second reap, so the kill is decided, recorded
        # and reported once. See ``_force_reap``.
        self._reaps_in_flight: dict[str, asyncio.Future] = {}  # type: ignore[type-arg]
        # Cache global approval_mode at init to avoid disk I/O on every
        # parentless spawn (cron, webhooks).
        try:
            self._global_approval_mode = KiroCrewConfig.load().agent.approval_mode
        except Exception:
            logger.warning(
                "Failed to load KiroCrewConfig for approval_mode; defaulting to interactive",
                exc_info=True,
            )
            self._global_approval_mode = ""
        # Retention window (seconds) for a delivered subagent's result.txt before
        # the reaper prunes it — the parent's grace window to read the full
        # transcript (spawn_status / read / grep) after the completion event.
        try:
            self._result_ttl_secs = int(KiroCrewConfig.load().agent.subagent_result_ttl_secs)
        except Exception:
            self._result_ttl_secs = 3600
        # Spawn stagger interval — serializes cold starts so a high cap fills as
        # a ramp rather than a burst (dynamic-subagent-sizing.md §5.3). It is a
        # smoothing interval, not the memory guard: every spawn still clears
        # ``spawn_min_memory_gb`` and the host budget, and the adaptive
        # controller cuts the cap on real pressure.
        try:
            self._spawn_stagger_secs = max(
                0.0, float(KiroCrewConfig.load().agent.subagent_spawn_stagger_secs)
            )
        except Exception:
            self._spawn_stagger_secs = 0.25
        # In-startup bound (dynamic-subagent-sizing.md, Configuration). The
        # stagger bounds the RATE of starts and ``_max_concurrent`` the RUNNING
        # population; :meth:`_startup_cap` bounds how many admitted agents may
        # be between execution start and their first runtime/stream/turn at
        # once, derived from the session-start gate's width alone -- there is
        # no key for it (see ``_startup_cap`` for why). ``session_start_concurrency``
        # is boot-only (``restart=True``), so it is captured here and never
        # re-read by ``apply_limits``: the SessionStartGate it sizes is fixed
        # for the loop's lifetime, and the derived bound must track the gate
        # that exists, not a value nothing is serving yet.
        # The manager is built on the gateway boot path and ``_startup_cap`` runs
        # on the event loop, so neither may probe the host for ``"auto"``. The
        # configured value is captured here (the same config read this block
        # always made); ``_startup_cap`` sizes it from the cached host reading
        # the gateway takes in a worker thread after the socket binds, and uses
        # the floor until that reading exists. ``_session_start_concurrency``
        # stays None unless a test pins a width.
        try:
            self._session_start_configured: object = (
                KiroCrewConfig.load().agent.session_start_concurrency
            )
        except Exception:
            self._session_start_configured = 2
        self._session_start_concurrency: int | None = None
        # ClaimPoint reservations not yet registered as a SubagentInfo; counted
        # by ``_startup_population`` (see there). +1 at reserve, -1 on the
        # re-entry that registers or on ``release_reservation``.
        self._startup_reservations = 0

        # Every limit captured above is a copy of config.json. The live watcher
        # pushes a rewrite at this object through ``reconfigure`` so a write from
        # the dashboard, ``kirocrew config set`` or ``$EDITOR`` lands without a
        # gateway restart. The subscription holds ``self`` weakly, so a discarded
        # manager (tests, provider reloads) falls out of the registry by itself.
        # The prefixes ARE the watched-path list, so the dispatcher filters an
        # unrelated ``agent.*`` write rather than the applier re-deriving on it.
        # ``None`` until the first ``reconfigure`` runs, so that first reload
        # always resolves the cap rather than comparing against a snapshot that
        # was never taken.
        self._last_sizing_fields: tuple[object, ...] | None = None
        self._config_sub: live.Subscription | None = None
        try:
            self._config_sub = live.watch_object(
                self, *self.LIVE_CONFIG_PATHS, name="SubagentManager"
            )
        except Exception:
            logger.warning("SubagentManager could not subscribe to live config", exc_info=True)

        # The facade is the single owner of mutable registries and slot tokens.
        # Coordinators own transition logic and route every cross-boundary call
        # back through this object, preserving overrides and monkeypatch seams.
        self._monitor = OrphanStallMonitor(self)
        self._terminal = TerminalCoordinator(self)
        self._admission = SpawnAdmissionCoordinator(self)
        # Durable task queue (``kiro_crew.taskq``): ``_queue`` above is a bounded
        # window over this store's rows. Schema, import and reconcile must
        # finish before attachment. Loop callers open in a worker; synchronous
        # callers open inline. Pending or failed opens refuse typed; only
        # agent.task_queue_enabled=false selects the in-memory queue.
        # ``admitted -> starting`` is written at claim; ``running`` at the
        # run's first stream event addressed to its session; waits and wakes
        # through admission.
        self._taskq: Any = None
        self._taskq_admit_wait_secs: float = 30.0
        #: ``agent.subagent_queue_max_wait_secs``: how long a memory-deferred row
        #: may wait before the pump ends it (``taskq_expire_memory_waits``); 0 is
        #: no bound. Live: :meth:`apply_limits` adopts a rewrite.
        try:
            self._subagent_queue_max_wait_secs = max(
                0, int(KiroCrewConfig.load().agent.subagent_queue_max_wait_secs)
            )
        except Exception:
            self._subagent_queue_max_wait_secs = DEFAULT_SUBAGENT_QUEUE_MAX_WAIT_SECS
        #: Set when ``agent.task_queue_enabled`` is on but the store could not
        #: be opened: every spawn is then REFUSED (typed, ``task_store_unavailable``)
        #: instead of accepted into an in-memory queue that a restart forgets.
        self._taskq_unavailable: str | None = None
        #: The re-open schedule for that refusal (``taskq_reopen_if_due``, driven
        #: by the reaper sweep). The count is how many opens have failed, which is
        #: the exponent of the shared recovery backoff; the deadline is monotonic,
        #: and 0.0 means the next sweep may attempt one.
        self._taskq_reopen_attempts: int = 0
        self._taskq_reopen_at: float = 0.0
        self._continuation = ContinuationCoordinator(self)
        self._waves = WaveDigestCoordinator(self)
        self._run_events = RunEventCoordinator(self)
        self._cancellation = CancellationCoordinator(self)
        self._taskq_init_task: asyncio.Task[None] | None = None
        try:
            loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or not SpawnAdmissionCoordinator.open_store_off_loop:
            self._taskq = self._open_taskq()
        else:
            self._taskq_unavailable = "durable task queue is initializing"
            self._taskq_init_task = loop.create_task(self._initialize_taskq())
            self._admission.track_store_task(self._taskq_init_task)

    def _open_taskq(self) -> Any:
        """Open, migrate and reconcile on a worker, or in a synchronous caller."""
        try:
            cfg = KiroCrewConfig.load()
            self._taskq_admit_wait_secs = float(cfg.agent.admit_wait_secs)
            if not cfg.agent.task_queue_enabled:
                self._taskq_unavailable = None
                return None
            store = self._admission.taskq_open(cfg, home=data_home())
            if store is None and self._taskq_unavailable is None:
                self._taskq_unavailable = "durable task queue could not be opened"
                self._admission.taskq_arm_reopen(cfg)
            return store
        except Exception as exc:
            logger.warning("durable task queue could not be opened", exc_info=True)
            self._taskq_unavailable = f"durable task queue could not be opened: {exc}"
            # Whoever records the refusal arms its retry, so no refusal path can
            # leave the store waiting for a restart. No cfg here: the load itself
            # is one of the things that may have raised.
            self._admission.taskq_arm_reopen()
            return None

    async def _initialize_taskq(self) -> None:
        opening = asyncio.create_task(asyncio.to_thread(self._open_taskq))
        try:
            store = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread still owns the open; retrieve and close its result.
            store = await opening
            if store is not None:
                await asyncio.to_thread(store.close)
            raise
        if getattr(self, "_shutting_down", False):
            if store is not None:
                await asyncio.to_thread(store.close)
            return
        if store is None and self._taskq is not None:
            # A re-open never UN-attaches: this task can be one the reaper armed
            # while the refusal stood, and an attach that happened meanwhile is
            # the newer fact.
            return
        # Attach only after schema, integrity check, imports and reconcile finish.
        # Before this assignment every entry point returns task_store_unavailable.
        self._taskq = store
        if store is not None:
            self._taskq_unavailable = None
            self._taskq_reopen_attempts = 0
            if self._reaper_task is not None and not self._reaper_task.done():
                self._admission.taskq_schedule_owed_replay()
                self._drain_queue()

    async def wait_taskq_ready(self) -> None:
        """Wait for startup recovery without cancelling it if this caller leaves."""
        if self._taskq_init_task is not None:
            await asyncio.shield(self._taskq_init_task)

    def close(self) -> None:
        """Release the process-lifetime durable task store this manager opened.

        ``__init__`` opens the durable task queue (a SQLite connection plus its
        dedicated writer thread, see :class:`~kiro_crew.taskq.store.TaskStore`);
        ``cancel_all`` cancels in-flight runs but never touches that store, so
        without this every manager leaks the connection's descriptors and its
        writer executor for the life of the process. Idempotent and safe to call
        from a synchronous teardown: it cancels a still-pending async open, then
        closes the store if one was attached.
        """
        taskq_open_task = self._taskq_init_task
        if taskq_open_task is not None and not taskq_open_task.done():
            taskq_open_task.cancel()
        self._taskq_init_task = None
        store, self._taskq = self._taskq, None
        if store is not None:
            store.close()

    def _effective_turn_limit(self, info: SubagentInfo) -> int:
        return self._run_events._effective_turn_limit_impl(info)

    def update_completion_keep(self, mode: str, max_chars: int) -> None:
        return self._run_events.update_completion_keep_impl(mode, max_chars)

    #: Dotted config paths whose value this manager copies at construction. They
    #: are the subscription's prefixes, so a reload that touches none of them
    #: never reaches this object; one that touches any of them re-derives EVERY
    #: copy from the new config (cheaper and safer than a per-field diff, and the
    #: derivations are all O(1)).
    LIVE_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.spawn_min_memory_gb",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
        "agent.subagent_max_turns",
        "agent.subagent_timeout_secs",
        "agent.subagent_stall_idle_secs",
        "agent.subagent_spawn_stagger_secs",
        "agent.subagent_result_ttl_secs",
        "agent.completion_keep",
        "agent.completion_keep_chars",
        "agent.subagent_queue_max_wait_secs",
    )

    #: The subset of ``LIVE_CONFIG_PATHS`` that actually feeds
    #: :func:`resolve_max_subagents` (the explicit pin, the auto ceiling, and
    #: the floor switch plus the memory terms the auto cap is sized from when
    #: ``agent.spawn_min_memory_gb`` disables the floor). Every other watched
    #: path only affects a plain field copy in :meth:`apply_limits`, so a
    #: reload that touches none of these has no way to change the resolved cap
    #: and must not pay for :func:`resolve_max_subagents`'s memory probe.
    SIZING_CONFIG_PATHS: tuple[str, ...] = (
        "agent.max_subagents",
        "agent.subagent_auto_max",
        "agent.spawn_min_memory_gb",
        "agent.subagent_mem_buffer_pct",
        "agent.subagent_cost_gb",
        "session.pool_size",
    )

    @staticmethod
    def _sizing_fields(cfg: KiroCrewConfig) -> tuple[object, ...]:
        """The values ``resolve_max_subagents`` reads from *cfg*, as a tuple.

        Comparing this tuple across reloads is how :meth:`reconfigure` tells
        whether a change could possibly move the resolved cap -- ``reconfigure``
        receives only the reloaded ``cfg``, not the ``ConfigChange`` that
        produced it (:func:`kiro_crew.config.live.watch_object` calls
        ``owner.reconfigure(cfg)``), so this diffs values rather than paths.
        """
        agent = cfg.agent
        return (
            agent.max_subagents,
            agent.subagent_auto_max,
            agent.spawn_min_memory_gb,
            agent.subagent_mem_buffer_pct,
            agent.subagent_cost_gb,
            cfg.session.pool_size,
        )

    async def reconfigure(self, cfg: KiroCrewConfig) -> None:
        """Live-config applier: re-derive the captured limits from *cfg*.

        Resolving the auto cap checks that host memory is readable
        (``/proc/meminfo``, cgroup files), which is filesystem I/O -- resolved
        off the loop and handed to :meth:`apply_limits` ready-made, but ONLY
        when a sizing input actually moved. ``reconfigure`` is invoked on every reload that touches
        any of ``LIVE_CONFIG_PATHS`` (e.g. ``agent.completion_keep``), most of
        which cannot change the resolved cap at all; re-probing host memory on
        every one of those is a needless thread hop. When nothing in
        :attr:`SIZING_CONFIG_PATHS` moved since the last reconfigure, the
        current cap is kept and only the other limits are re-applied.
        """
        sizing_now = self._sizing_fields(cfg)
        if self._last_sizing_fields is not None and sizing_now == self._last_sizing_fields:
            cap = self._user_max_concurrent
        else:
            try:
                cap = await asyncio.to_thread(resolve_max_subagents, cfg)
            except Exception:
                logger.warning("resolve_max_subagents failed on reload; keeping the current cap")
                cap = self._user_max_concurrent
        self._last_sizing_fields = sizing_now
        self.apply_limits(cfg, max_concurrent=cap)

    def apply_limits(self, cfg: KiroCrewConfig, *, max_concurrent: int | None = None) -> None:
        """Adopt every constructor-captured limit from *cfg*.

        Applies the same normalization the constructor does: ``0`` for a
        timeout / stall interval keeps the built-in default (the sentinel the
        gateway passes when the field is unset), the stagger interval is floored
        at ``0.0``, and the cap goes through :func:`resolve_max_subagents` (the
        explicit ``max_subagents`` pin, or the ``subagent_auto_max`` auto
        ceiling) unless the caller already resolved it and passes
        *max_concurrent*.

        Raising the cap admits queued spawns through the staggered pump;
        lowering it only stops new admissions -- an in-flight run is never
        cancelled to fit a smaller cap, the count simply drains below it as runs
        finish. Every read site (admission gate, reaper, stall detector, run
        timeout, parentless approval policy, completion-keep) reads the attribute
        at use, so the assignment is the whole apply.
        """
        agent = cfg.agent
        old_cap = self._max_concurrent
        if max_concurrent is None:
            try:
                max_concurrent = resolve_max_subagents(cfg)
            except Exception:
                logger.warning("resolve_max_subagents failed; keeping max_concurrent=%d", old_cap)
                max_concurrent = self._user_max_concurrent
        self._user_max_concurrent = max(1, int(max_concurrent))
        self._max_concurrent = self._clamp_effective_cap()
        try:
            self._default_turn_limit = int(agent.subagent_max_turns)
        except (TypeError, ValueError):
            pass
        try:
            timeout = int(agent.subagent_timeout_secs)
            self._default_timeout = timeout if timeout > 0 else _TIMEOUT_SECS
        except (TypeError, ValueError):
            pass
        try:
            stall = int(agent.subagent_stall_idle_secs)
            self._stall_idle_secs = stall if stall > 0 else _STALL_IDLE_SECS
        except (TypeError, ValueError):
            pass
        try:
            self._spawn_stagger_secs = max(0.0, float(agent.subagent_spawn_stagger_secs))
        except (TypeError, ValueError):
            pass
        try:
            self._result_ttl_secs = int(agent.subagent_result_ttl_secs)
        except (TypeError, ValueError):
            pass
        try:
            self._subagent_queue_max_wait_secs = max(0, int(agent.subagent_queue_max_wait_secs))
        except (TypeError, ValueError):
            pass
        # ``agent.approval_mode`` is deliberately NOT adopted here: it is
        # boot-only (schema ``restart=True``) because every channel dispatcher
        # resolves it once at start, and one consumer taking it live while the
        # others keep the boot value would make the UI's "restart required"
        # honest for some tool calls and false for others.
        self.update_completion_keep(agent.completion_keep, int(agent.completion_keep_chars))
        logger.info(
            "SubagentManager reconfigured: max_concurrent=%d (was %d), turn_limit=%d, "
            "timeout=%ds, stall_idle=%ds, stagger=%.1fs, max_startups=%d, result_ttl=%ds",
            self._max_concurrent,
            old_cap,
            self._default_turn_limit,
            self._default_timeout,
            self._stall_idle_secs,
            self._spawn_stagger_secs,
            self._startup_cap(),
            self._result_ttl_secs,
        )
        if self._max_concurrent > old_cap:
            self._notify_cap_raised()

    def start_reaper(self) -> None:
        return self._monitor.start_reaper_impl()

    def release_queue_dispatch(self) -> None:
        """Open the pump held by ``defer_queue_dispatch`` and drain once.

        Called by the gateway after the memory barrier. Every drain request
        that landed while the hold stood (the boot dispatch's ``call_later``,
        the store attach, a dependency wake) returned without a pass, so this
        one pass is what picks up the rows they would have. Idempotent: a
        manager that was never held, or was already released, drains nothing
        extra here. The owed-report replay the store attach put off for the
        same barrier is scheduled here too.
        """
        if not self._queue_dispatch_held:
            return
        self._queue_dispatch_held = False
        self._drain_queue()
        self._admission.taskq_schedule_owed_replay()

    async def _reconcile_orphans(self) -> None:
        return await self._monitor._reconcile_orphans_impl()

    async def _end_leaked_processes(self) -> None:
        return await self._monitor._end_leaked_processes_impl()

    async def _end_orphan_process(self, agent_id: str, state: dict, *, reason: str) -> str | None:
        return await self._monitor._end_orphan_process_impl(agent_id, state, reason=reason)

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """Check if a PID is still running."""
        # os.kill(pid, 0) would terminate the process on Windows — probe instead.
        return platform_compat.pid_exists(pid)

    @staticmethod
    def _is_orphan_process(pid: int, spawned_at: float) -> bool:
        """Check if PID belongs to the original subagent (not a recycled PID).

        Compares /proc/{pid} creation time against the recorded spawn time.
        Returns False if the process was created after the agent was spawned
        (indicating PID reuse).
        """
        try:
            proc_stat = os.stat(f"/proc/{pid}")
            # Process was created before or around the time we spawned the agent
            return proc_stat.st_ctime <= spawned_at + 2.0
        except (FileNotFoundError, OSError):
            return False

    @staticmethod
    async def _kill_orphan_pid(pid: int) -> str | None:
        """Best-effort SIGKILL of an orphaned process; returns what stopped it.

        ``None`` once the process is gone -- signalled here, or already exited
        by the time the signal went out (nothing to kill is not a failure) --
        and otherwise the failure the kill raised (``PermissionError: …``),
        named the way the reaper's record names one, so the caller's audit row
        can say ``failed`` for a process the kill left standing rather than
        ``killed`` regardless. Never raises: the caller owns a reconciliation
        it must still finish.

        The signal goes through :func:`platform_compat.kill_pid_async`: the
        Windows kill is a ``taskkill`` spawn that waits up to five seconds for
        the target, and the reconciliation runs on the event loop, so that wait
        happens on the subprocess executor while the loop keeps serving; the
        POSIX ``os.kill`` is a non-blocking syscall and runs inline.
        """
        try:
            await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
        except ProcessLookupError:
            return None
        except OSError as exc:
            return failure_name(exc)
        return None

    async def _notify_orphan(self, agent_id: str, state: dict, has_result: bool) -> str | None:
        return await self._monitor._notify_orphan_impl(agent_id, state, has_result)

    async def _try_inject_orphan_notification(
        self,
        parent_session: str,
        msg: str,
        meta: dict | None = None,
        delivery: SubagentDelivery | None = None,
    ) -> bool | str:
        return await self._monitor._try_inject_orphan_notification_impl(
            parent_session, msg, meta, delivery
        )

    async def _send_orphan_slack_dm(self, msg: str) -> None:
        return await self._monitor._send_orphan_slack_dm_impl(msg)

    def _live_shared_count(self, pid: int | None, agents: "list[SubagentInfo]") -> int:
        return self._monitor._live_shared_count_impl(pid, agents)

    def _sample_live_costs(self) -> None:
        return self._monitor._sample_live_costs_impl()

    def _refresh_learned_settled(self) -> None:
        return self._monitor._refresh_learned_settled_impl()

    def _record_cost(self, info: SubagentInfo) -> None:
        return self._monitor._record_cost_impl(info)

    async def _reaper_loop(self) -> None:
        return await self._monitor._reaper_loop_impl()

    @property
    def _startup_deadline(self) -> int:
        """Seconds a new start may run with no runtime, from the live config."""
        if self._startup_timeout_override is not None:
            return self._startup_timeout_override
        snap = live.snapshot()
        agent = snap.agent if snap is not None else AgentConfig()
        budget = max(SESSION_START_TIMEOUT_MIN, agent.session_start_timeout_secs)
        collect = agent.start_collect_timeout_secs + _STARTUP_COLLECT_GRACE_SECS
        # ``_INITIALIZE_TIMEOUT`` as well, once per handshake round: the clock
        # pauses in a queue but runs through the spawn's own handshakes, so a
        # retried start that spends its whole ``initialize`` and ``session/new``
        # budgets twice must still be inside the window -- or the watchdog reaps
        # a start whose own budgets have not expired.
        handshakes = _STARTUP_HANDSHAKES * (budget + INITIALIZE_TIMEOUT_SECS)
        derived = int(handshakes + collect + _STARTUP_LAUNCH_MARGIN_SECS)
        return max(_STARTUP_TIMEOUT_SECS, derived)

    def _is_startup_stalled(
        self, info: SubagentInfo, now: float, mono_now: float | None = None
    ) -> bool:
        return self._monitor._is_startup_stalled_impl(info, now, mono_now)

    @staticmethod
    def _note_tool_dispatch(info: SubagentInfo, event: Any) -> None:
        """Record the in-flight tool for liveness attribution.

        Mirrors ``AcpSessionHandle``'s ``_inflight_tool`` snapshot: title, the
        already-redacted input, the dispatch instant, and the TRUSTED
        ``is_shell`` / ``tool_name`` fields from ``_meta.kiro`` (never the
        LLM-authored title). The subagent event loop already receives the same
        ``AcpEvent``, and keeping only ``title`` would leave stall detection
        with nothing to attribute evidence with.

        The hand-off itself runs through the shared :class:`InFlightToolTracker`
        (``dispatch``), the same rule the main agent uses: the call becomes the
        judged one and, when it carries a toolCallId, is stored under it.

        Retiring the oracle here (rather than clearing it) is load-bearing: a
        movement walk still running against the PREVIOUS tool's command holds a
        reference to the old instance, and clearing in place would let its late
        write land on the new tool's baseline and read as movement. A dispatch
        always changes the judged call, so the oracle is always retired.

        A native child's call (``sub_session_id`` set, from a permission
        request) is judged but takes the empty-id slot and is NOT stored by id:
        its result goes to the backend's own session and never reaches this
        loop, so nothing would ever pop a stored entry. A normal call is stored
        under its toolCallId until its terminal result, so a parallel call that
        finishes first hands the slot back to one still running instead of
        emptying it.
        """
        state = ToolCallState(
            title=event.title or "",
            command=event.tool_input or "",
            dispatch_ts=time.monotonic(),
            dispatch_boot_ts=boottime_now(),
            # No consumer parking on this path: a subagent's events are consumed
            # by the run loop itself, with no approval / IM send / hook holding a
            # frame, so this stamp cannot lag the runtime's spawn the way the
            # dashboard dispatch loop's can.
            dispatch_parked_secs=0.0,
            is_shell=bool(getattr(event, "is_shell", False)),
            tool_name=getattr(event, "tool_name", "") or "",
            # Only a provenance-verified identity names the server, so a frame
            # without one cannot select the trusted wait contract.
            mcp_server_name=(
                getattr(event, "mcp_server_name", "") or ""
                if getattr(event, "mcp_identity_trusted", False) is True
                else ""
            ),
        )
        # A native child's call (``sub_session_id`` set, from a permission
        # request) is judged so a stall is attributed to the child's command,
        # but its result goes to the backend's own session and never reaches
        # this loop — so it is NOT stored by id (nothing would ever pop it). It
        # takes the empty-id slot, which the next dispatch overwrites. A normal
        # call is stored under its toolCallId so a parallel call finishing first
        # hands the judged slot back to one still running instead of emptying it.
        if getattr(event, "sub_session_id", ""):
            info._tool_tracker.dispatch("", state)
        else:
            info._tool_tracker.dispatch(getattr(event, "tool_call_id", "") or "", state)
        SubagentManager._retire_subagent_oracle(info)

    @staticmethod
    def _note_tool_result(info: SubagentInfo, event: Any) -> None:
        """Retire a call's attribution when its TERMINAL result arrives.

        The gate lives here rather than at the call site so the invariant is
        directly testable. ``EVENT_TOOL_RESULT`` is also emitted for
        The gate lives here rather than at the call site so the invariant is
        directly testable. ``EVENT_TOOL_RESULT`` is also emitted for
        non-terminal progress updates, and treating one of those as the end of
        the tool would drop attribution while the command is still running —
        degrading liveness to idle-time-only for exactly the long silent command
        this detection exists to judge, and so raising the badge on a healthy
        agent. A failed, cancelled or refused call is over as surely as a
        completed one, which is the set ``AcpSessionHandle`` retires on too, and
        which ``acp.client`` gates on the same way.

        The hand-off runs through the shared :class:`InFlightToolTracker`
        (``result``): the finished call is dropped and, when it was the judged
        one, the slot passes to the most recently dispatched call still running,
        so a file write returning beside a ``wait`` leaves the reaper judging the
        wait rather than nothing. A judged native-child call is never in the
        running set (it took the empty-id slot and was never stored), so this
        agent's own next terminal result moves the slot off it. The oracle is
        retired only when the judged call actually changed — the signal the
        tracker returns.
        """
        terminal = getattr(event, "tool_status", "") in TERMINAL_TOOL_STATUSES
        if not (event.tool_final or terminal):
            return
        judged_changed = info._tool_tracker.result(
            getattr(event, "tool_call_id", "") or "", terminal=True
        )
        if judged_changed:
            SubagentManager._retire_subagent_oracle(info)

    @staticmethod
    def _clear_tool_dispatch(info: SubagentInfo) -> None:
        """Drop every in-flight tool snapshot and retire the oracle with them.

        Called at the start of each prompt, so a call the previous turn never
        closed cannot be handed the slot when a later call returns.
        """
        info._tool_tracker.clear()
        SubagentManager._retire_subagent_oracle(info)

    @staticmethod
    def _retire_subagent_oracle(info: SubagentInfo) -> None:
        """Retire this agent's stall oracle and bump its generation.

        Called whenever the judged tool call changes (new dispatch, final
        result, cleared snapshot). Swaps in a ``fresh()`` oracle rather than
        clearing in place — a walk still running against the previous command
        holds the old instance, and the generation bump lets a verdict that
        outlived its tool be recognised as stale and discarded. The counterpart
        of ``AcpSessionHandle._retire_liveness_state``; the two differ only in
        the machinery each owns (a bound oracle + consult future there, a
        generation counter here), which is why the retirement is NOT part of the
        shared tracker — only the hand-off decision is.
        """
        oracle = info._stall_oracle
        info._stall_oracle = oracle.fresh() if oracle is not None else None
        info._stall_gen += 1

    async def _stall_verdict(self, info: SubagentInfo) -> tuple[str, str]:
        return await self._monitor._stall_verdict_impl(info)

    async def _maybe_flag_stall(self, agent_id: str, info: SubagentInfo, now: float) -> None:
        return await self._monitor._maybe_flag_stall_impl(agent_id, info, now)

    @staticmethod
    def _record_slow_command(info: SubagentInfo, idle: float) -> None:
        """Best-effort append of a stalled subagent's slow command for analysis.

        Writes to ``~/.kiro/crew/subagents/slow_commands.jsonl`` (rotated at
        1 MiB keeping one previous generation, survives per-agent folder
        cleanup). Deliberately separate from the
        tombstone path, which marks an agent dead — a stalled agent is still
        running.
        """
        try:
            record_slow_command(
                info.id,
                last_tool=_redact(info.last_tool or ""),
                tool_count=info.tool_count,
                turns=info.turns,
                idle_secs=int(idle),
                elapsed_secs=int(time.time() - info.started),
                parent_session=info.parent_session_key or "",
                session_sharing=info._session_sharing,
            )
        except Exception:
            logger.debug("Failed to record slow command for %s", info.id, exc_info=True)

    def _claim_finalize(
        self, info: SubagentInfo, *, supersede_recovery: bool = False, row_settled: bool = False
    ) -> bool:
        claimed = self._terminal._claim_finalize_impl(info, supersede_recovery=supersede_recovery)
        if claimed:
            # The one reporter of the outcome also writes it to the task store,
            # fenced by the generation the run was dispatched under -- unless
            # the reporter's own store call already wrote it (``row_settled``).
            self._admission.taskq_settle(info, row_settled=row_settled)
        return claimed

    async def _report_terminal(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
        gate: "asyncio.Future[bool] | None" = None,
    ) -> bool:
        return await self._terminal._report_terminal_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
            gate=gate,
        )

    async def _report_terminal_guarded(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> bool:
        return await self._terminal._report_terminal_guarded_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
        )

    async def _run_terminal_report(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
    ) -> bool:
        return await self._terminal._run_terminal_report_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
        )

    def _spawn_terminal_report(
        self,
        info: SubagentInfo,
        *,
        source: str,
        injection_timeout_reason: str,
        mark_delivered_on_success: bool,
        settle_digest: bool = False,
        teardown_done: "asyncio.Event | None" = None,
        gate: "asyncio.Future[bool] | None" = None,
    ) -> "asyncio.Task[bool]":
        return self._terminal._spawn_terminal_report_impl(
            info,
            source=source,
            injection_timeout_reason=injection_timeout_reason,
            mark_delivered_on_success=mark_delivered_on_success,
            settle_digest=settle_digest,
            teardown_done=teardown_done,
            gate=gate,
        )

    @staticmethod
    async def _await_report(task: "asyncio.Task[bool]") -> bool:
        """Block until a spawned terminal report completes, shielded.

        On normal completion this blocks until the report is delivered
        (sequencing unchanged). If the awaiting caller is cancelled, the shield
        keeps the report running to completion on its own task while the caller
        still receives ``CancelledError`` — teardown semantics are unchanged and
        the outcome is never stranded.
        """
        return await asyncio.shield(task)

    async def settle_before_delete(
        self,
        agent_id: str,
    ) -> Literal["delivered", "pending"]:
        """Settle completion debt and remove a finished run atomically."""
        info = self._agents.get(agent_id)
        if info is None:
            return "delivered"
        if info._ending_claimed and not info.done:
            # Claimed its completed ending and still writing its result (a
            # bounded wait): ``cancel()`` declined it as done, but its report
            # does not exist yet, so there is nothing to settle before the pop.
            return "pending"
        active_reports = tuple(
            task
            for task, report_info in self._report_owners.items()
            if report_info is info or report_info.id == agent_id
        )
        if active_reports:
            await asyncio.gather(
                *(asyncio.shield(task) for task in active_reports),
                return_exceptions=True,
            )
        # The pop is only half of a dismissal, and the panel's durable half reads
        # neither the manager nor the folder: it folds the session's crew log. So
        # the dismissal is recorded in BOTH places here, in one coroutine, so the
        # halves cannot come apart -- a route that wrote only one of them would
        # clear the card from live state and leave the fold still offering it.
        #
        # Written BEFORE the pop, because the pop is the PUBLISH. With the write
        # second, an unwritable store still popped the run and still answered
        # "delivered", which the DELETE route reports as success -- and the card
        # came back on the next reconnect with nothing to explain it. A failed
        # write returns the retryable result this coroutine already uses above,
        # leaving the run in the manager so the operator can dismiss it again.
        #
        # The folder write is OFF the loop, because it is a synchronous file write
        # and this is a coroutine; its two falsy cases are NOT the same, since a
        # run with no folder has nothing durable to resurrect its card, so its
        # dismissal stands. The log write enqueues, so it needs no thread; its unit
        # search does read files, so that part goes to a thread.
        #
        # The unit is found by SEARCHING the slot's logs for the child's row, not
        # from the emitter's pin: this method waits for the terminal report, whose
        # ``finally`` releases that pin, so by here every finished child has none.
        # Nor from the slot's current session id, which is the unit the slot is
        # landing work in now and not necessarily the one a child dispatched before
        # a reset went to. A child no unit holds is not a failure -- there is no
        # folded card to clear -- so it does not hold the pop.
        #
        # The log append is WAITED on, on the same terms the dismiss route's other
        # arm uses, because the queue returns as soon as the entry is handed over
        # and the entry can still be refused at the buffer's memory ceiling. A run
        # whose folder is later reclaimed has only this record, so a pop on an
        # uncommitted append is the same published-too-early failure the folder
        # write above is ordered to avoid.
        from kiro_crew.crew_log import emit as crew_log_emit
        from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child
        from kiro_crew.dashboard.chat_utils import subagent_event_slot

        if crew_log_emit.enabled():
            try:
                unit = await asyncio.to_thread(
                    unit_holding_child,
                    subagent_event_slot(info.parent_session_key),
                    agent_id,
                )
            except UnitSearchFailed:
                # The store would not say whether a unit holds this child, which is
                # not the same as none holding it. Popping here would report a
                # dismissal that was never even looked for, and the card returns
                # once the store recovers.
                logger.debug(
                    "crew log: the unit search for %s failed, so the dismissal is "
                    "left retryable rather than published",
                    agent_id,
                    exc_info=True,
                )
                return "pending"
            if unit and not await crew_log_emit.awaiting_commit(
                lambda on_settled: crew_log_emit.on_subagent_dismissed(
                    unit, agent_id=agent_id, on_settled=on_settled
                ),
                what=f"the panel dismissal for {agent_id}",
            ):
                return "pending"
        outcome = await asyncio.to_thread(record_panel_dismissal_outcome, agent_id)
        if outcome == DISMISSAL_FAILED:
            return "pending"
        self._agents.pop(agent_id, None)
        self._tasks.pop(agent_id, None)
        return "delivered"

    def _mint_agent_id(self) -> str:
        """Draw a run id: :data:`_RUN_ID_HEX_CHARS` hex characters of random bytes.

        Every spawn identity comes from here, which is the point -- one draw site
        is what lets the width be a single number.

        The width carries the uniqueness on its own, with nothing to remember and
        nothing to read. At 8 characters the id was 32 bits, so 2000 spawns on
        one host collided about once in 2,100 times, and the collision did not
        read as one: identity is assigned before registration, so the caller was
        handed the id and the accept then failed on the duplicate primary key,
        reaching the user as ``task store write failed`` -- naming a subsystem
        that was working correctly. 64 bits puts that at about 1 in 10**13.

        Drawn from ``os.urandom`` rather than a ``uuid4`` prefix. A v4 UUID spends
        its 13th hex character on the fixed version digit ``4``, so the first 16
        characters of one carry 60 random bits, not 64 -- a 16-fold worse bound
        than the width advertises, from a detail no reader of the slice can see.

        A checked narrow draw would need to know which ids are taken, and a
        durable task row outlives the process that wrote it, so it would have to
        ask the store -- which the spawn path cannot do, because taking a
        task-store connection on the event loop stalls every session's turn
        (:mod:`kiro_crew.on_loop_db` refuses it). Widening removes the question.
        """
        return os.urandom(_RUN_ID_HEX_CHARS // 2).hex()

    def _release_slot(self, info: SubagentInfo) -> bool:
        return self._terminal._release_slot_impl(info)

    async def _force_reap(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        return await self._terminal._force_reap_impl(agent_id, info, elapsed, reason=reason)

    async def _reap_once(
        self, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
    ) -> None:
        return await self._terminal._reap_once_impl(agent_id, info, elapsed, reason=reason)

    async def _sigkill_session(
        self,
        session_key: str,
        handle: ProcessHandle | None,
        *,
        popped: "list[tuple[Any, ProcessHandle]] | None" = None,
    ) -> str | None:
        return await self._terminal._sigkill_session_impl(session_key, handle, popped=popped)

    async def _sigkill_sessions(
        self,
        session_key: str,
        handles: list[ProcessHandle],
        *,
        popped: "list[tuple[Any, ProcessHandle]] | None" = None,
    ) -> str | None:
        return await self._terminal._sigkill_sessions_impl(session_key, handles, popped=popped)

    def _sessions_under(self, session_key: str) -> list[tuple[Any, ProcessHandle]]:
        return self._terminal._sessions_under_impl(session_key)

    def _retain_process_handles(
        self,
        agent_id: str,
        session_key: str,
        pairs: list[tuple[Any, ProcessHandle]] | None = None,
    ) -> list[ProcessHandle]:
        return self._terminal._retain_process_handles_impl(agent_id, session_key, pairs)

    def notify_injection_failed(
        self, info: SubagentInfo, reason: str = "delivery timed out"
    ) -> None:
        return self._terminal.notify_injection_failed_impl(info, reason)

    def _clamp_effective_cap(self) -> int:
        if self._adaptive_cap is None:
            return self._user_max_concurrent
        return max(0, min(self._user_max_concurrent, int(self._adaptive_cap)))

    # ── In-startup population ──────────────────────────────────────────────
    #
    # Three different things bound a fan-out: ``_spawn_stagger_secs`` bounds
    # the RATE of starts, ``_max_concurrent`` bounds the RUNNING population,
    # and ``_startup_cap`` bounds how many admitted agents may be IN STARTUP at
    # once. Without the third, one start is admitted every stagger interval however long each
    # takes, and when starts are slow (a dedicated process per model override,
    # a queue at the session-start gate, a throttled provider handshake) dozens
    # sit in startup together, every one of them contending for the same gate
    # and every one of them running down the same fixed startup deadline.
    # Measured on a 623-item fan-out: waves of 24-45 lost ~2%, a wave of 120
    # lost ~50%, every loss a healthy start the watchdog reaped as
    # "Failed to start within 120s".

    @staticmethod
    def _in_startup(info: SubagentInfo) -> bool:
        """True while *info* has entered execution but has nothing to show yet.

        The same shape the startup watchdog reaps on (:meth:`_is_startup_stalled`),
        minus the clock: past ``_run_inner``'s first statement (``_exec_started``
        set), no runtime PID, no answer on its own session yet
        (:meth:`_leave_startup`), no turn, and not already ending. A queued
        spawn is not in it (not registered until admitted), and
        neither is an agent PARKED at the spawn-approval prompt
        (``_awaiting_approval``, ``_exec_started`` still ``None``): it is
        starting nothing, so it must not consume the startup bound -- counting
        it would let a handful of unanswered prompts hold every other spawn on
        the host, auto-approved ones from unrelated parents included. What has
        to be bounded is the moment its prompt resolves, since a bulk grant
        (``tool_approval:bulk_trust`` / ``bulk_yolo``) resolves EVERY pending
        prompt at once: a released agent therefore re-enters through the pump
        (:meth:`_admit_released_start`) and is metered into startup by the SAME
        stagger and in-startup checks a fresh spawn passes, so ``_startup_cap``
        holds through the flood without the parked agents ever counting.
        """
        return (
            not info.done
            and not info._reap_started
            and info._exec_started is not None
            and info.turns == 0
            and info._pid is None
            and info._first_stream_started is None
        )

    def _startup_population(self, *, exclude: SubagentInfo | None = None) -> int:
        """How many admitted agents are in startup right now (see :meth:`_in_startup`).

        Includes ``_startup_reservations``: a durable-store spawn reserves its
        slot BEFORE its claim is awaited (``ClaimPoint``, reserve-then-commit)
        and registers its ``SubagentInfo`` only on re-entry, which skips the
        admission gate because the reservation already passed it. Between
        reserve and re-entry the start is admitted but has no info to count, so
        the reservation is counted in its place; the re-entry's registration
        and ``release_reservation`` both give it back.
        """
        return int(self._startup_reservations) + sum(
            1 for info in self._agents.values() if info is not exclude and self._in_startup(info)
        )

    def _startup_cap(self) -> int:
        """The in-startup bound: ``_STARTUP_CAP_GATE_ROUNDS x session_start_concurrency``.

        Twice the session-start gate's width, clamped to ``[1, cap]``. The
        bound is tied to the GATE, not to the running cap, because the gate is
        the one resource every start in startup contends for: ``session/new``
        runs under ``G`` permits, so at most ``G`` starts make progress at any
        moment and every other admitted start is a spawned process (dedicated
        path) or a claimed slot holding nothing but a place in the gate's
        queue. Time spent in that queue is not charged to the startup deadline
        (the clock pauses at queue entry and resumes at acquisition --
        ``_gate_wait_mark`` / ``_gate_exit_reset``), so the queue's length is
        not what reaps a healthy start; what the bound decides is how much of
        the running cap may sit in startup contending for ``G`` permits at
        once. ``2G`` is the smallest value that never idles the gate -- one
        round holding permits and one round already admitted to take them the
        moment they free -- and admitting more buys no starts, since the gate
        serves ``G`` per round however many are queued: it only lengthens the
        queue and grows the population of admitted-but-idle starts. A
        cap-derived term (``max(..., ceil(cap / 4))``, say) would do exactly
        that: 16 in startup at a cap of 64 against a 2-permit gate is seven
        rounds queued for two permits. The population the bound is checked
        against (:meth:`_in_startup`) is the agents actually starting, plus
        reservations not yet registered (:meth:`_startup_population`). Agents
        parked at the spawn-approval prompt are NOT in it -- they start nothing
        and must not block unrelated spawns -- and are instead metered through
        the pump when their prompt resolves (:meth:`_admit_released_start`), so
        "at most ``2G`` in startup" holds even when a bulk trust/yolo grant
        releases them all at once.

        There is deliberately NO config key for this value. ``2G`` is both the
        floor and the ceiling of the useful range: fewer than ``2G`` idles the
        gate between rounds (no next round already admitted when the current
        one releases), and more than ``2G`` buys no starts -- the gate serves
        ``G`` per round however many are queued behind it -- only a longer
        queue of idle admitted starts. A knob could therefore only make the
        system worse, and the operator's real lever on this bound already
        exists: ``agent.session_start_concurrency`` sizes the gate, and the
        bound tracks it.

        Never above the effective running cap (a larger value could not bind)
        and never below 1, so at a cap of ``0`` this returns ``1``: the running
        cap, not this bound, is what pauses admission there.
        """
        cap = max(0, int(self._max_concurrent))
        bound = _STARTUP_CAP_GATE_ROUNDS * self._resolved_session_start_width()
        return max(1, min(bound, cap))

    def _resolved_session_start_width(self) -> int:
        """The session-start gate width as an in-memory read (no probe, no lock)."""
        if self._session_start_concurrency is not None:
            return int(self._session_start_concurrency)
        try:
            return max(1, cached_session_start_concurrency(self._session_start_configured))
        except Exception:
            return 2

    def _note_startup_progress(self, info: SubagentInfo) -> None:
        """Wake the spawn queue when *info* leaves startup without ending.

        A runtime PID (``_run_inner``'s PID record, ``_bind_shared_handle``) or
        the first answer on its own session (:meth:`_leave_startup`) takes
        *info* out of the in-startup population, which may open a slot under
        :meth:`_startup_cap` that no other edge announces: the slot-release
        drain fires only on a terminal, and the pump does not poll. Called at
        those two transitions -- at most twice per start -- and the pump
        returns at once when nothing waits and re-checks every gate itself, so
        a call that opens nothing is cheap. A pump failure is logged, never
        raised: the run that just progressed is not the one at fault, and the
        next terminal or arrival pumps again.

        Also restarts the activity clock: the first-prompt silence window
        (``_FIRST_PROMPT_SILENT_SECS``) measures from here, so the handshake
        that preceded the PID is never charged to it.
        """
        info.last_activity = time.time()
        info._last_activity_mono = time.monotonic()
        try:
            self._drain_queue()
        except Exception:
            logger.debug("startup-progress queue pump failed for %s", info.id, exc_info=True)

    def _leave_startup(self, info: SubagentInfo) -> None:
        """Take *info* out of startup: its own session has answered its prompt.

        Two moments prove that, and each calls this: the first frame out of the
        provider stream that is addressed to THIS session (the stream loop in
        ``_run_inner``), and a dependency verdict on the prompt
        (``_yield_for_dependency``), which
        the durable row records as ``starting -> running`` in the same step.
        An opened stream proves neither, and neither does a ``runtime_global``
        frame -- a co-tenant's traffic that a shared runtime fanned out to
        every session on it. Stamps ``info._first_stream_started`` once per
        execution and, on a first turn, wakes a spawn the in-startup bound is
        holding (:meth:`_note_startup_progress`).
        """
        if info._first_stream_started is not None:
            return
        info._first_stream_started = time.time()
        info._first_stream_mono = time.monotonic()
        info._first_stream_generation = info._rss_generation
        if info.turns == 0:
            self._note_startup_progress(info)

    def set_cap_raise_listener(self, listener: Callable[[], object] | None) -> None:
        """Register the ONE hook a cap raise rings, or ``None`` to drop it.

        For a gate that is bounded by ``max_concurrent`` but lives outside this
        manager: it can read the new cap whenever it likes, but it has no edge
        to react to, and its own occupancy may never produce one (see
        :meth:`_notify_cap_raised`). Set, never appended: the gateway owns the
        single runner lane and re-registration replaces the stale handle.
        """
        self._cap_raise_listener = listener

    def _notify_cap_raised(self) -> None:
        """Fan freed capacity out to every gate the live cap bounds.

        MUST be called on the event loop, not from a worker thread: the runner
        lane resolves its parked waiters' futures, which is loop-affine.

        The subagent queue drains through the staggered pump. The runner lane
        (`taskq/adapters/runner.py`) reads this cap as its ceiling but parks its
        waiters on a bare future, so a waiter parked while the cap was ``0``
        holds no slot and has no running holder whose release would wake it:
        this raise is its only edge. The lane keeps its own occupancy count, so
        it grants exactly the FIFO prefix the new cap allows -- and nothing at
        all while the effective cap is still ``0``.
        """
        if self._queue:
            # Freed capacity: the pump re-checks the gate itself and honours the
            # stagger interval, so this never bursts.
            self._drain_queue()
        listener = self._cap_raise_listener
        if listener is None:
            return
        try:
            listener()
        except Exception:  # noqa: BLE001 - a broken hook must not block a raise
            logger.debug("cap-raise listener failed", exc_info=True)

    def set_effective_cap(self, cap: int | None) -> int:
        """Adaptive-controller seam: bound the live cap beneath the user's.

        ``None`` removes the bound. ``0`` pauses new grants (in-flight runs
        finish; nothing is cancelled). A raise notifies every gate the cap
        bounds exactly as a config raise does. Returns the cap now in force.
        """
        old_cap = self._max_concurrent
        self._adaptive_cap = None if cap is None else max(0, int(cap))
        self._max_concurrent = self._clamp_effective_cap()
        if self._max_concurrent != old_cap:
            logger.info(
                "SubagentManager effective cap %d -> %d (user ceiling %d)",
                old_cap,
                self._max_concurrent,
                self._user_max_concurrent,
            )
        if self._max_concurrent > old_cap:
            self._notify_cap_raised()
        return self._max_concurrent

    @property
    def user_max_concurrent(self) -> int:
        """The user's resolved cap -- the ceiling the adaptive cap sits under."""
        return self._user_max_concurrent

    @property
    def max_concurrent(self) -> int:
        return self._max_concurrent

    @property
    def running_count(self) -> int:
        return self._running_count

    @property
    def pending_work_count(self) -> int:
        """Return accepted subagent work that a process restart would interrupt.

        ``running_count`` alone stops representing work before shielded terminal
        delivery finishes, and an unexpected-cancel recovery can be live while
        holding no concurrency slot. Count the finite manager-owned registries
        instead, while retaining ``running_count`` as a fail-closed floor for a
        slot published just before its task registration. The perpetual reaper
        is maintenance and is deliberately excluded; its one-shot orphan
        reconciliation is finite delivery work and is included.
        """
        live_primary: set[int] = set()
        for task in self._tasks.values():
            if not task.done():
                live_primary.add(id(task))

        pending = len(self._queue) + max(max(0, int(self._running_count)), len(live_primary))
        seen = set(live_primary)
        extra_tasks = [*self._report_tasks, *self._followup_watchers.values()]
        reconcile = getattr(self, "_reconcile_task", None)
        if reconcile is not None:
            extra_tasks.append(reconcile)
        for task in extra_tasks:
            marker = id(task)
            if marker in seen:
                continue
            seen.add(marker)
            if not task.done():
                pending += 1

        # A cancelled to_thread state write can outlive its run task. Each entry
        # is a run with at least one such worker still writing, and the last of
        # them to land removes it from its done-callback, so every entry is
        # finite restart-sensitive work even though no awaitable is retained here.
        pending += len(self._abandoned_state_writers)
        return pending

    def running_agents_for(self, parent_key: str) -> list[dict]:
        return self._run_events.running_agents_for_impl(parent_key)

    def task_memory_rows(self) -> list[dict[str, object]]:
        return self._monitor.task_memory_rows_impl()

    def spawn(
        self,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        max_turns: int = 0,
        model: str | None = None,
        reasoning_effort: str = "",
        allowed_tools: list[str] | None = None,
        bare: bool = False,
        cwd: str = "",
        approval_mode: str | None = None,
        silent: bool = False,
        batch_id: str = "",
        batch_total: int = 0,
        keep: bool = False,
        conversation_key: str = "",
        app: str = "",
        include_memory: bool = True,
        include_lessons: bool = True,
        include_project: bool = True,
        memory_store: str = "",
        _agent_prevalidated: bool = False,
        _from_queue: bool = False,
        _preassigned_id: str = "",
        _crew_log_asked: "tuple[str, int] | None" = None,
        _memory_mode: str | None = None,
        _store_accepted: bool = False,
        _stop_before_claim: bool = False,
        _claimed: "tuple[int, bool, str] | None" = None,
        _window_hint: "bool | None" = None,
        _child_registration: bool = True,
        *,
        crew: str = "",
        target_member: str | None = None,
        delegation: dict[str, str] | None = None,
        backend: str | None = None,
        _execution_context: dict | None = None,
        _parent_spawn_policy: "ParentSpawnPolicy | None" = None,
        _agent_check: "AgentCheck | None" = None,
        _recovering_row: bool = False,
        _stop_before_memory_read: bool = False,
        _memory_reading: "tuple[float, str] | None" = None,
    ) -> SubagentInfo | None:
        result = self._admission.spawn_impl(
            task,
            parent_session_key,
            agent,
            max_turns,
            model,
            reasoning_effort,
            allowed_tools,
            bare,
            cwd,
            approval_mode,
            silent,
            batch_id,
            batch_total,
            keep,
            conversation_key,
            app,
            include_memory,
            include_lessons,
            include_project,
            memory_store,
            _agent_prevalidated,
            _from_queue,
            _preassigned_id,
            _crew_log_asked=_crew_log_asked,
            _memory_mode=_memory_mode,
            _store_accepted=_store_accepted,
            _stop_before_claim=_stop_before_claim,
            _claimed=_claimed,
            _window_hint=_window_hint,
            _child_registration=_child_registration,
            crew=crew,
            target_member=target_member,
            delegation=delegation,
            backend=backend,
            _execution_context=_execution_context,
            _parent_spawn_policy=_parent_spawn_policy,
            _agent_check=_agent_check,
            _recovering_row=_recovering_row,
            _stop_before_memory_read=_stop_before_memory_read,
            _memory_reading=_memory_reading,
        )
        assert not isinstance(result, PreparedSpawn)
        # Every synchronous gate return (started, queued, or refused) receives
        # the same admission snapshot before a scheduled announce can run.
        # ``ClaimPoint`` comes back ONLY for ``_stop_before_claim=True`` and
        # ``MemoryReadPoint`` ONLY for ``_stop_before_memory_read=True``, whose
        # callers are the event-loop entries (``spawn_async`` and the coroutine
        # pump); every other caller receives a ``SubagentInfo`` or None as
        # declared.
        return result  # type: ignore[return-value]

    async def _spawn_after_memory_read(
        self,
        first: Any,
        proceed: "Callable[[], bool] | None" = None,
        /,
        **reentry: Any,
    ) -> Any:
        """Finish a spawn the gate stopped at its memory read, else return *first*.

        The host is read on a worker (:func:`_host_memory_reading_off_loop`) and
        the gate re-entered from the params its first half built, with the
        caller's own re-entry flags (*reentry*) on top, so the floor is never
        read on the event loop. Anything else -- a refusal, a queued handle, a
        ``ClaimPoint`` -- passes through untouched. *proceed*, when given, is
        asked after the read: a False answer (the row was stopped while the
        read ran) re-enters nothing and returns None.
        """
        if not isinstance(first, MemoryReadPoint):
            return first
        reading = await _host_memory_reading_off_loop(first.min_gb)
        if proceed is not None and not proceed():
            return None
        return self.spawn(**{**first.params, **reentry}, _memory_reading=reading)

    def prepare_spawn(
        self, task: str, **kwargs: Any
    ) -> "SubagentInfo | PreparedSpawn | MemoryReadPoint | None":
        """Run every policy gate of :meth:`spawn` and return the row to persist
        instead of starting anything. A refusal comes back as the same done
        ``SubagentInfo`` :meth:`spawn` would return; ``None`` is the legacy
        at-capacity answer. A spawn with no row to persist runs the whole gate
        here, and with ``_stop_before_memory_read`` stops at its memory read."""
        kwargs.pop("_from_queue", None)
        kwargs.pop("_store_accepted", None)
        prepared = self._admission.spawn_impl(task, _prepare_only=True, **kwargs)
        assert not isinstance(prepared, ClaimPoint)  # never requested here
        return prepared

    async def _check_agent_off_loop(
        self,
        agent: str,
        cwd: str,
        *,
        app: str = "",
        execution_context: "Mapping[str, Any] | None" = None,
        prevalidated: bool = False,
    ) -> "AgentCheck | None":
        """The gate's two agent-directory checks for *agent*, on a worker thread.

        App ownership (when *app* is set) and ``_validate_agent`` in the cwd the
        run will use, keyed by their inputs so the gate uses the answer only for
        the same ``(agent, cwd, app)`` (``AgentCheck``). An explicit *cwd* is
        canonicalized here exactly as the gate resolves it (``validate_cwd``
        against the configured roots), and the app is the one the gate settles
        on (the captured *execution_context*'s app wins over the caller's, as
        ``resolve_spawn_execution`` does), so the keys match. None when there is
        nothing to check, or when the cwd will be refused anyway."""
        if prevalidated:
            return None
        named = agent
        context = execution_context or {}
        selection_kind = str(context.get("selection_kind") or "")
        template_id = str(context.get("template_id") or "")
        if not named and not _inherited_member_agent(selection_kind, template_id):
            return None
        app = str(context.get("app") or "") or app
        pool_cwd = str(getattr(self._sessions, "_pool_cwd", "") or "")

        def _check() -> "AgentCheck | None":
            effective = pool_cwd
            if cwd:
                try:
                    roots = KiroCrewConfig.load().agent.subagent_cwd_allowed_roots
                except Exception:
                    roots = []  # the gate fails closed the same way
                resolved, err = validate_cwd(cwd, roots)
                if err:
                    return None
                effective = resolved
            checked = named or _inherited_member_agent(selection_kind, template_id, effective)
            if not checked:
                return None
            owner_err = _validate_app_agent_ownership(checked, app) if app and named else ""
            return (
                checked,
                effective,
                app,
                owner_err or "",
                *_validate_agent(checked, effective),
            )

        return await asyncio.to_thread(_check)

    async def spawn_async(self, task: str, **kwargs: Any) -> SubagentInfo | None:
        """:meth:`spawn` for event-loop callers (``/api/spawn``).

        Write-before-ack with the write OFF the loop: the policy gates run
        first (``prepare_spawn``), the row is written on the store's dedicated
        writer thread (``TaskStore.run``), and only then does the sync
        ``spawn`` start the run with ``_store_accepted=True`` -- the SQLite
        lock wait never blocks the loop, and the caller is still acked only
        once the row exists. Without a durable store this is plain ``spawn``.
        """
        # Snapshot loop-owned policy inputs before reading a missing durable
        # carrier. The admission gate below still runs on-loop after the await.
        if (
            not isinstance(task, str)
            or not task.strip()
            or getattr(self._sessions, "admission_closed", False) is True
        ):
            return self.spawn(task, **kwargs)
        if kwargs.get("_execution_context") is None:
            from kiro_crew.execution_context import read_session_execution
            from kiro_crew.subagent_persistence import read_run_execution

            parent = str(kwargs.get("parent_session_key") or "")
            conversation = str(kwargs.get("conversation_key") or "")
            mode = kwargs.get("_memory_mode")
            inherited = None
            try:
                if mode is None:
                    resolver = self._memory_mode_for_session
                    mode = resolver(parent) if resolver is not None else "persistent"
                if not isinstance(mode, str) or mode not in {
                    "persistent",
                    "incognito",
                    "temporary",
                }:
                    raise ValueError("unknown memory mode")
                if parent and not kwargs.get("agent") and not conversation:
                    inherited = self._sessions.get_agent_selection(parent)
                record_id = (conversation or parent).removeprefix("subagent:")
                live = (
                    self._agents.get(record_id)
                    if (conversation or parent).startswith("subagent:")
                    else None
                )
                record = live.execution_context if live is not None else None
                if record is None:
                    record = (
                        await asyncio.to_thread(read_run_execution, record_id)
                        if conversation
                        else await asyncio.to_thread(read_session_execution, parent)
                    )
                if kwargs.get("_parent_spawn_policy") is None and not conversation:
                    # The PARENT record is in hand: its template names the spec
                    # whose ``availableAgents`` the gate honours, so the
                    # declaration is read here, off-loop, without a second
                    # record read.
                    template = record.template_id if record is not None else ""
                    kwargs["_parent_spawn_policy"] = (
                        template,
                        (
                            await asyncio.to_thread(parent_template_allowlists, parent, template)
                            if template
                            else ()
                        ),
                    )
                execution = self._admission.resolve_spawn_execution(
                    parent_session_key=parent,
                    conversation_key=conversation,
                    agent=kwargs.get("agent", ""),
                    memory_store=kwargs.get("memory_store", ""),
                    app=kwargs.get("app", ""),
                    crew=kwargs.get("crew", ""),
                    target_member=kwargs.get("target_member"),
                    _memory_mode=mode,
                    _record=record,
                    _inherited_selection=inherited,
                )
                kwargs["_execution_context"] = execution.to_record()
                kwargs["_memory_mode"] = execution.memory_mode
            except (OSError, ValueError) as exc:
                batch_id = str(kwargs.get("batch_id") or "")
                batch_total = max(0, int(kwargs.get("batch_total") or 0))
                if batch_id and not kwargs.get("_from_queue") and not kwargs.get("_store_accepted"):
                    submitted = self._batch_submitted.setdefault(batch_id, [0, batch_total])
                    submitted[0] += 1
                    self._batch_progress_ts[batch_id] = time.monotonic()
                return self._announce_rejection(
                    SubagentInfo(
                        id=kwargs.get("_preassigned_id") or self._mint_agent_id(),
                        task=_redact(task),
                        parent_session_key=parent,
                        agent=str(kwargs.get("agent") or ""),
                        memory_mode=mode if isinstance(mode, str) else "persistent",
                        done=True,
                        error=f"memory_unavailable: {exc}",
                        batch_id=batch_id,
                        batch_total=batch_total,
                    )
                )
        if kwargs.get("_parent_spawn_policy") is None and not kwargs.get("_store_accepted"):
            # A caller that supplied the execution (``/api/spawn`` hands the
            # policy along with it; a continuation or retry does not): the
            # parent's declaration is read OFF the loop here, like the record
            # above, so the gate below consumes it without a directory scan.
            kwargs["_parent_spawn_policy"] = await asyncio.to_thread(
                parent_spawn_policy, str(kwargs.get("parent_session_key") or "")
            )
        if kwargs.get("_agent_check") is None:
            # The agent-directory scan, off the loop: the gate consumes this
            # answer instead of walking the directory on the loop itself.
            kwargs["_agent_check"] = await self._check_agent_off_loop(
                str(kwargs.get("agent") or ""),
                str(kwargs.get("cwd") or ""),
                app=str(kwargs.get("app") or ""),
                execution_context=kwargs.get("_execution_context"),
                prevalidated=bool(kwargs.get("_agent_prevalidated")),
            )
        # The memory read's re-entry re-runs the policy gates and the agent
        # check; the parent's declaration and the agent answer read off the
        # loop above are handed to it too, so neither scans the agents
        # directory on the loop.
        # Neither pass registers a nested child synchronously: that walks the
        # store's ledger on the loop. It is awaited below, off-loop, as the
        # durable path does (taskq.waits, W3).
        policy = {
            "_parent_spawn_policy": kwargs.get("_parent_spawn_policy"),
            "_agent_check": kwargs.get("_agent_check"),
            "_child_registration": False,
        }
        prepared: Any = (
            self.spawn(task, **kwargs, _stop_before_memory_read=True, _child_registration=False)
            if self._admission.taskq_store() is None
            else self.prepare_spawn(
                task, **kwargs, _stop_before_memory_read=True, _child_registration=False
            )
        )
        if not isinstance(prepared, PreparedSpawn):
            # No row to write: the task queue is off, the spawn was refused, or
            # it is non-persistent and ran the whole gate in the prepare pass,
            # stopping at its memory read.
            result: SubagentInfo | None = await self._spawn_after_memory_read(prepared, **policy)
            if result is not None and not result.done:
                # Started or queued under a parent blocked in spawn_sub_agents:
                # the parent yields its slot, with the store I/O off the loop.
                await self._admission.taskq_child_registered_async(result)
            return result
        # From the moment the row exists until this call has claimed or
        # windowed it, the pump's refill must not pick it up: the awaits below
        # are where a concurrent drain could otherwise start it twice.
        admitting: set[str] = self.__dict__.setdefault("_admitting_ids", set())
        admitting.add(prepared.agent_id)
        outcome: SubagentInfo | None = None
        try:
            outcome = await self._spawn_async_accepted(task, prepared, **kwargs)
            return outcome
        finally:
            try:
                # A pressure defer posted on the way out must be ON the row
                # before the pump may refill it, or the next pass re-runs the
                # gate on a row whose ``next_run_at`` is not set yet. Shielded:
                # a cancel of this call must not cancel that write too, or the
                # row stays QUEUED with no ``next_run_at`` for good. A cancel
                # ends this wait at once, but the write is already queued on the
                # store's writer thread (posted by the call that deferred), ahead
                # of every refill read the release below lets through.
                await asyncio.shield(self._admission.await_pending_defer(prepared.agent_id))
            finally:
                # Whatever ended the wait, a cancel included: a row left marked
                # here is skipped by the refill, Stop all and the chip forever.
                waiting = prepared.agent_id in self._admitting_waiting
                admitting.discard(prepared.agent_id)
                self._admitting_waiting.discard(prepared.agent_id)
                if waiting or outcome is None:
                    # The row may still wait, and every pump pass during this
                    # call left it out: the slot one of them would have given
                    # it may be free already, with nothing left to ask again.
                    self._drain_queue()

    async def _spawn_async_accepted(
        self, task: str, prepared: PreparedSpawn, **kwargs: Any
    ) -> SubagentInfo | None:
        store = self._admission.taskq_store()
        assert store is not None
        store_err = await store.run(self._admission.taskq_accept_record, prepared.record)
        if store_err:
            sel().log_tool_invocation(
                session_key=str(kwargs.get("parent_session_key") or ""),
                source="subagent",
                tool_name="spawn_run",
                outcome="refused_task_store",
                metadata={"error": str(store_err)[:200], "subagent_id": prepared.agent_id},
            )
            return self._announce_rejection(
                SubagentInfo(
                    id=prepared.agent_id,
                    task=redact_credentials(redact_exfiltration_urls(task)[0])[0],
                    agent=str(kwargs.get("agent") or ""),
                    parent_session_key=str(kwargs.get("parent_session_key") or ""),
                    done=True,
                    error=f"spawn refused: task store unavailable ({store_err})",
                    error_code=self._admission.TASK_STORE_UNAVAILABLE_CODE,
                    batch_id=str(kwargs.get("batch_id") or ""),
                    batch_total=max(0, int(kwargs.get("batch_total") or 0)),
                )
            )
        params = dict(prepared.params)
        params.pop("_preassigned_id", None)
        # No store I/O on the loop from here on: the window decision and the
        # claim run on the writer thread; the sync re-entry only registers.
        await self._admission.ensure_coordinator_async()
        window_hint = await self._admission.taskq_should_window_async(prepared.agent_id)
        common: dict[str, Any] = dict(
            _preassigned_id=prepared.agent_id,
            _store_accepted=True,
            _window_hint=window_hint,
            _child_registration=False,  # the W3 branch runs awaited, below
            _agent_check=kwargs.get("_agent_check"),
        )
        # The read's re-entry re-runs the policy gates on this committed row
        # (a refusal there fails it); the parent's declaration ``spawn_async``
        # read off the loop is handed along so the allowlist vet does not
        # scan the agents directory on the loop.
        first: Any = await self._spawn_after_memory_read(
            self.spawn(**params, **common, _stop_before_claim=True, _stop_before_memory_read=True),
            **common,
            _stop_before_claim=True,
            _parent_spawn_policy=kwargs.get("_parent_spawn_policy"),
        )
        if not isinstance(first, ClaimPoint):
            if first is not None and first.queued and not first.done:
                # The gate queued the row this call still holds (deferred, or
                # behind the cap): the chip counts it from here on, while the
                # refill still leaves it to this call. Marked before any await,
                # so the read the verdict's depth request makes, a loop step
                # later, already sees it.
                self._admitting_waiting.add(prepared.agent_id)
                await self._admission.taskq_child_registered_async(first)
            return first
        # The slot is reserved (ClaimPoint); the claim is awaited off-loop and
        # the re-entry consumes the reservation or releases it.
        result = await self._admission.claim_and_start(
            first,
            lambda claimed: self.spawn(**params, **common, _claimed=claimed),
            stop_params={**params, **common},
        )
        if result is not None and result.queued and not result.done:
            # The claim did not take the row and the gate queued it again.
            self._admitting_waiting.add(prepared.agent_id)
        if result is not None and not result.done and result.id in self._agents:
            # Nested child of a parent blocked in spawn_sub_agents: the parent
            # yields its slot (taskq.waits, W3) with the store I/O off-loop.
            await self._admission.taskq_child_registered_async(result)
        return result

    async def _safe_announce(self, info: SubagentInfo) -> None:
        return await self._admission._safe_announce_impl(info)

    def _announce_rejection(self, info: SubagentInfo) -> SubagentInfo:
        return self._admission._announce_rejection_impl(info)

    def _should_stagger_queue(self, now: float) -> tuple[bool, bool]:
        return self._admission._should_stagger_queue_impl(now)

    def _memory_pressure_hold(self, *, floor_gb: float | None = None) -> int | None:
        return self._admission._memory_pressure_hold_impl(floor_gb=floor_gb)

    def memory_pressure_hold_active(self) -> bool:
        """Whether the macOS kernel memory-pressure hold applies right now.

        For a speculative caller (the eager-spawn admission) that must not take
        the memory held starts wait for.
        """
        return self._memory_pressure_hold() is not None

    def _memory_pressure_holds(
        self,
        agent_id: str,
        level: int,
        *,
        parent_session_key: str = "",
        batch_id: str = "",
        available_gb: float | None = None,
        relabel: bool = False,
        commit_expiry: bool = True,
    ) -> str:
        return self._admission._memory_pressure_holds_impl(
            agent_id,
            level,
            parent_session_key=parent_session_key,
            batch_id=batch_id,
            available_gb=available_gb,
            relabel=relabel,
            commit_expiry=commit_expiry,
        )

    def _pressure_lane_started(self, parent_session_key: str, started_at: float) -> None:
        self._admission._pressure_lane_started_impl(parent_session_key, started_at)

    def _forget_pressure_clock(self, agent_id: str) -> None:
        self._admission._forget_pressure_clock_impl(agent_id)

    def _forget_pending_start(self, agent_id: str) -> None:
        """Drop what this process kept for a start that began or never will."""
        self._forget_pressure_clock(agent_id)
        self._floor_waits.pop(agent_id, None)
        self._held_approval_modes.pop(agent_id, None)
        self._floor_deferred_ids.discard(agent_id)
        self._admission.memory_wait_ended(agent_id)

    # ── Continuable conversations (keep=True) ─────────────────────────────

    def _conversation_busy(self, conv_key: str) -> SubagentInfo | None:
        return self._continuation._conversation_busy_impl(conv_key)

    def _keep_recorded_on_disk(self, key: str) -> bool:
        return self._continuation._keep_recorded_on_disk_impl(key)

    def _promote_conversation(
        self, conv_id: str, conv_key: str, last_used: float | None = None
    ) -> None:
        return self._continuation._promote_conversation_impl(conv_id, conv_key, last_used)

    def _scan_keep_states(self) -> list[tuple[str, str, str, str, str, float]]:
        return self._continuation._scan_keep_states_impl()

    async def _rebuild_conversation_registry(self) -> None:
        return await self._continuation._rebuild_conversation_registry_impl()

    def native_child_resume_refusal(self, conversation_id: str) -> str | None:
        """Typed refusal when *conversation_id* is a harness-native child of a
        live session (no conversation of its own; the parent is the lever)."""
        return self._continuation.native_child_resume_refusal(conversation_id)

    def claim_retry(self, failed: SubagentInfo) -> str:
        """Claim the right to retry *failed*; ``""`` when granted, else the owner.

        A failed run has at most one successor. It is taken when a retry or a
        ``spawn_continue`` of this run already claimed it, or when a run holds
        its conversation ``subagent:<id>`` -- queued, live or finished. A grant
        marks the claim ``SUCCESSOR_PENDING`` with no await in between, so a
        concurrent retry or continuation sees it; :meth:`settle_retry` records
        the outcome.
        """
        for owner in (failed._retried_as, failed._continued_as):
            if owner:
                return owner
        if failed._continuation_starting:
            return SUCCESSOR_PENDING
        conv_key = f"subagent:{failed.id}"
        busy = self._conversation_busy(conv_key)
        if busy is not None and busy.id != failed.id:
            return busy.id
        for info in self._agents.values():
            if info.id != failed.id and info.conversation_key == conv_key:
                return info.id
        failed._retried_as = SUCCESSOR_PENDING
        return ""

    def settle_retry(self, failed: SubagentInfo, successor_id: str | None) -> None:
        """Record the run a granted retry started, or release a claim that did not
        land (``successor_id`` None). A claim already settled is left alone."""
        if failed._retried_as == SUCCESSOR_PENDING:
            failed._retried_as = successor_id or ""

    def _claim_continuation(
        self, conv_id: str, task: str, parent_session_key: str
    ) -> tuple[SubagentInfo | None, SubagentInfo | None]:
        """``(original, refusal)`` for a continuation of *conv_id*.

        A refusal when a retry already claimed the run (its task runs as that
        retry now), or while another continuation's start is still in flight
        (it is not yet visible to the conversation-busy lookup). Otherwise the
        continuation claims *conv_id* in ``_continuations_starting`` before any
        await, so a retry or a second continuation arriving while this start is
        in flight is refused. The claim is held by the manager, so it outlasts a
        registry record that ``evict_completed_agents`` drops during the start,
        and it covers a run with no record at all (evicted, or from before a
        restart). A registered run's ``_continuation_starting`` flag is set with
        it for ``claim_retry``. ``_settle_continuation`` ends both.
        """
        original = self._agents.get(conv_id)
        if original is not None and original._retried_as:
            reason = (
                f"run {conv_id} was retried as run {original._retried_as}, which "
                "owns its task now; continue that run's conversation instead"
            )
        elif conv_id in self._continuations_starting:
            reason = (
                f"another continuation of run {conv_id} is starting — wait for its "
                "completion event, or use spawn_steer once it is running"
            )
        else:
            self._continuations_starting.add(conv_id)
            if original is not None:
                original._continuation_starting = True
            return original, None
        refusal = SubagentInfo(
            id=self._mint_agent_id(),
            task=_redact(task),
            done=True,
            parent_session_key=parent_session_key,
            error=f"conversation_busy: {reason}",
        )
        return original, refusal

    def _settle_continuation(
        self,
        conv_id: str,
        original: SubagentInfo | None,
        result: SubagentInfo | None,
        *,
        raised: bool = False,
    ) -> None:
        """End the in-flight claim a start took on *conv_id*. A start that landed
        records its id only when no earlier continuation did; one that did not land
        leaves an earlier successor in place. A start that RAISED may have accepted
        its durable row first, so it counts as a continuation of unknown id.
        Called once per granted claim, by its holder only."""
        self._continuations_starting.discard(conv_id)
        if original is None:
            return
        original._continuation_starting = False
        if original._continued_as:
            return
        if raised:
            original._continued_as = SUCCESSOR_UNKNOWN
        elif result is not None and not (result.done and result.error):
            original._continued_as = result.id

    def continue_conversation(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
    ) -> SubagentInfo | None:
        original, refusal = self._claim_continuation(conv_id, task, parent_session_key)
        if refusal is not None:
            return refusal
        result: SubagentInfo | None = None
        raised = True
        try:
            result = self._continuation.continue_conversation_impl(
                conv_id,
                task,
                parent_session_key,
                agent,
                model,
                max_turns,
                cwd,
                _preassigned_id,
                _memory_mode=_memory_mode,
                _crew_log_asked=_crew_log_asked,
            )
            raised = False
        finally:
            # This call took the claim: every exit (a return, a raise, a cancel) ends it.
            self._settle_continuation(conv_id, original, result, raised=raised)
        return result

    async def continue_conversation_async(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
    ) -> SubagentInfo | None:
        original, refusal = self._claim_continuation(conv_id, task, parent_session_key)
        if refusal is not None:
            return refusal
        result: SubagentInfo | None = None
        raised = True
        try:
            result = await self._continuation.continue_conversation_async_impl(
                conv_id,
                task,
                parent_session_key,
                agent,
                model,
                max_turns,
                cwd,
                _preassigned_id,
                _memory_mode,
                _crew_log_asked,
            )
            raised = False
        finally:
            # This call took the claim: every exit (a return, a raise, a cancel) ends it.
            self._settle_continuation(conv_id, original, result, raised=raised)
        return result

    def _continue_prelude(
        self,
        conv_id: str,
        task: str,
        parent_session_key: str = "",
        agent: str = "",
        model: str | None = None,
        max_turns: int = 0,
        cwd: str = "",
        _preassigned_id: str = "",
        _memory_mode: str | None = None,
        _crew_log_asked: "tuple[str, int] | None" = None,
        *,
        _execution_context=None,
        _captured_state=...,
    ) -> "SubagentInfo | dict[str, Any] | None":
        return self._continuation._continue_prelude_impl(
            conv_id,
            task,
            parent_session_key,
            agent,
            model,
            max_turns,
            cwd,
            _preassigned_id,
            _memory_mode,
            _crew_log_asked,
            _execution_context=_execution_context,
            _captured_state=_captured_state,
        )

    def recorded_cwd(self, conv_id: str) -> str:
        return self._continuation.recorded_cwd_impl(conv_id)

    def _inherited_context_groups(self, conv_id: str) -> tuple[bool, bool, bool]:
        return self._continuation._inherited_context_groups_impl(conv_id)

    def _inherited_memory_store(self, conv_id: str) -> str:
        return self._continuation._inherited_memory_store_impl(conv_id)

    async def steer_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        return await self._continuation.steer_run_impl(agent_id, message)

    # Bounds for the follow_up watcher: poll cadence, post-done busy retries
    # (finalization may briefly hold the conversation), and a hard deadline so
    # a wedged run can never leave an immortal watcher behind.
    _FOLLOWUP_POLL_SECS = 2.0
    _FOLLOWUP_BUSY_RETRIES = 10
    _FOLLOWUP_BUSY_RETRY_SECS = 3.0

    async def follow_up_run(self, agent_id: str, message: str) -> tuple[bool, str]:
        return await self._continuation.follow_up_run_impl(agent_id, message)

    def _arm_followup_watcher(self, info: SubagentInfo) -> None:
        return self._continuation._arm_followup_watcher_impl(info)

    async def _deliver_followups(self, info: SubagentInfo) -> None:
        return await self._continuation._deliver_followups_impl(info)

    async def _announce_followup_failure(
        self,
        info: SubagentInfo,
        reason: str,
        failure_info: SubagentInfo | None = None,
        messages: list | None = None,
    ) -> None:
        return await self._continuation._announce_followup_failure_impl(
            info, reason, failure_info, messages
        )

    def _audit_followup(self, info: SubagentInfo, outcome: str) -> None:
        return self._continuation._audit_followup_impl(info, outcome)

    def release_conversation(self, conv_id: str) -> tuple[bool, str]:
        return self._continuation.release_conversation_impl(conv_id)

    async def release_conversation_async(self, conv_id: str) -> tuple[bool, str]:
        return await self._continuation.release_conversation_async_impl(conv_id)

    async def _sweep_conversations_async(self, now: float) -> None:
        return await self._continuation._sweep_conversations_async_impl(now)

    def _drain_queue(self) -> None:
        return self._admission._drain_queue_impl()

    async def _admit_released_start(self, info: SubagentInfo) -> str:
        return await self._admission._admit_released_start_impl(info)

    def _release_admitted_start(self) -> str:
        return self._admission._release_admitted_start_impl()

    async def _drain_queue_async(self) -> None:
        return await self._admission._drain_queue_async_impl()

    async def _drain_queue_pass(self) -> None:
        return await self._admission._drain_queue_pass_impl()

    def _drain_queue_sync(
        self,
        *,
        refill: Callable[..., int],
        dispatch: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        return self._admission._drain_queue_sync_impl(refill=refill, dispatch=dispatch)

    async def _dispatch_async(self, params: dict[str, Any]) -> SubagentInfo | None:
        return await self._admission._dispatch_async_impl(params)

    def _after_dispatch(
        self, params: dict[str, Any], drained: SubagentInfo | None, *, refill: Callable[..., int]
    ) -> None:
        return self._admission._after_dispatch_impl(params, drained, refill=refill)

    async def _spawn_with_approval(self, info: SubagentInfo) -> None:
        return await self._admission._spawn_with_approval_impl(info)

    def _log_spawned(self, info: SubagentInfo) -> None:
        return self._admission._log_spawned_impl(info)

    @property
    def running(self) -> list[SubagentInfo]:
        """Return currently running (not done) subagents."""
        return [a for a in self._agents.values() if not a.done]

    def has_live_shared_session(self, session_key: str) -> bool:
        """Recognize a shared child only while its exact runtime handle is live.

        Run records survive completion/restart for display and continuation;
        they are not session authority. The runtime's queue registry is what
        destroy() unregisters, even when the parent process keeps running.
        """
        for info in self._agents.values():
            if info.done or info.reaped or not info._session_sharing:
                continue
            if (info.conversation_key or f"subagent:{info.id}") != session_key:
                continue
            provider = info._shared_provider
            if not isinstance(provider, AcpSessionProvider):
                continue
            runtime, handle = provider._runtime, provider._handle
            if (
                runtime.is_alive()
                and runtime._session_queues.get(handle.session_id) is handle._queue
            ):
                return True
        return False

    @property
    def all_agents(self) -> list[SubagentInfo]:
        """Return all tracked subagents (running and done)."""
        return list(self._agents.values())

    def batch_members_pending(self, batch_id: str) -> bool:
        return self._waves.batch_members_pending_impl(batch_id)

    def batch_reports_in_flight(self, batch_id: str) -> bool:
        return self._waves.batch_reports_in_flight_impl(batch_id)

    def arm_report_in_flight(self, info: SubagentInfo) -> None:
        return self._waves.arm_report_in_flight_impl(info)

    def consume_report_hold(self, batch_id: str, agent_id: str) -> None:
        return self._waves.consume_report_hold_impl(batch_id, agent_id)

    async def batch_members_pending_async(self, batch_id: str) -> bool:
        return await self._waves.batch_members_pending_async_impl(batch_id)

    def wave_has_live_nested_spawns(self, batch_id: str) -> bool:
        return self._waves.wave_has_live_nested_spawns_impl(batch_id)

    def finalize_batch(self, batch_id: str) -> None:
        return self._waves.finalize_batch_impl(batch_id)

    def record_lost_submission(
        self,
        batch_id: str,
        batch_total: int,
        reason: str,
        parent_session_key: str = "",
    ) -> None:
        return self._waves.record_lost_submission_impl(
            batch_id, batch_total, reason, parent_session_key
        )

    def _sweep_stuck_waves(self, now: float) -> None:
        return self._waves._sweep_stuck_waves_impl(now)

    async def _sweep_stuck_waves_async(self, now: float) -> None:
        return await self._waves._sweep_stuck_waves_async_impl(now)

    def _sweep_digest_holds(self, now: float) -> None:
        return self._waves._sweep_digest_holds_impl(now)

    async def _sweep_digest_holds_async(self, now: float) -> None:
        return await self._waves._sweep_digest_holds_async_impl(now)

    def force_digest_flush(
        self,
        batch_id: str,
        parent_session_key: str,
        batch_total: int,
        held_secs: float,
    ) -> None:
        return self._waves.force_digest_flush_impl(
            batch_id, parent_session_key, batch_total, held_secs
        )

    async def _announce_digest_flush(self, info: SubagentInfo) -> None:
        return await self._waves._announce_digest_flush_impl(info)

    async def settle_queued_delivery(self, deliveries: list[SubagentDelivery]) -> None:
        return await self._waves.settle_queued_delivery_impl(deliveries)

    async def _settle_digest_holds(self, info: SubagentInfo) -> None:
        return await self._waves._settle_digest_holds_impl(info)

    def get(self, agent_id: str) -> SubagentInfo | None:
        return self._run_events.get_impl(agent_id)

    def is_queued(self, agent_id: str) -> bool:
        return self._run_events.is_queued_impl(agent_id)

    @property
    def count(self) -> int:
        return len(self.running)

    async def _teardown_run_session(self, info: SubagentInfo, session_key: str) -> None:
        return await self._run_events._teardown_run_session_impl(info, session_key)

    async def _run(self, info: SubagentInfo) -> None:
        return await self._run_events._run_impl(info)

    def _schedule_cancel_recovery(
        self, info: SubagentInfo, *, reason: str = "unexpected_cancel"
    ) -> None:
        return self._cancellation._schedule_cancel_recovery_impl(info, reason=reason)

    async def _touch_activity(self, info: SubagentInfo) -> None:
        return await self._run_events._touch_activity_impl(info)

    async def _fire_event(self, etype: str, info: SubagentInfo, extra: dict | None = None) -> None:
        return await self._run_events._fire_event_impl(etype, info, extra)

    def _queued_depth(self, parent_session_key: str) -> int:
        return self._run_events._queued_depth_impl(parent_session_key)

    async def _queued_depth_async(self, parent_session_key: str) -> int:
        return await self._run_events._queued_depth_async_impl(parent_session_key)

    @property
    def queued_count(self) -> int:
        """Return all not-yet-registered spawns in the stagger queue."""
        return len(self._queue)

    def queued_count_for(self, parent_session_key: str) -> int:
        return self._run_events.queued_count_for_impl(parent_session_key)

    async def queued_count_for_async(self, parent_session_key: str) -> int:
        return await self._run_events.queued_count_for_async_impl(parent_session_key)

    def published_queued_depths(self) -> dict[str, int]:
        """The queued depth last published per parent session key (non-zero only).

        The values of the newest ``subagent_queued`` frames, not a fresh count: a
        reader on the event loop (the slot list) gets them without a store read,
        and they agree with what the frame stream told every client. A copy.
        """
        return self._published_depths.snapshot()

    async def queued_run_async(self, agent_id: str) -> "QueuedRun | None":
        """The accepted, not yet registered spawn *agent_id*, or None.

        For a reader that must not answer "not found" for a spawn the gate is
        holding (``GET /api/spawn/{id}`` and its ownership check). None also
        for a registered id: :meth:`get` answers that one.
        """
        return await self._admission.taskq_queued_run_async(agent_id)

    async def queued_runs_async(
        self, parent_session_key: str | None = None, *, app: str | None = None
    ) -> "QueuedRunListing":
        """Every accepted spawn with no registered run, for one parent or all.

        The rows :attr:`all_agents` cannot list: gate-deferred rows, rows
        waiting for a slot, claimed unregistered rows and window entries
        (``GET /api/spawn?queued=1``). A bounded page: ``partial`` says when it
        cannot be all of them.
        """
        return await self._admission.taskq_queued_runs_async(parent_session_key, app=app)

    def has_in_memory_pending_work_for(
        self, parent_session_key: str, *, exclude_id: str = ""
    ) -> bool:
        return self._run_events.has_in_memory_pending_work_for_impl(
            parent_session_key, exclude_id=exclude_id
        )

    def has_unfinished_report_for(self, parent_session_key: str) -> bool:
        return self._run_events.has_unfinished_report_for_impl(parent_session_key)

    async def queued_count_or_none_async(self, parent_session_key: str) -> int | None:
        return await self._run_events.queued_count_or_none_async_impl(parent_session_key)

    def has_pending_work_for(self, parent_session_key: str) -> bool:
        return self._run_events.has_pending_work_for_impl(parent_session_key)

    async def has_pending_work_for_async(self, parent_session_key: str) -> bool:
        return await self._run_events.has_pending_work_for_async_impl(parent_session_key)

    def _emit_queue_depth(
        self,
        parent_session_key: str,
        batch_id: str = "",
        *,
        wait: dict[str, Any] | None = None,
    ) -> None:
        return self._run_events._emit_queue_depth_impl(parent_session_key, batch_id, wait=wait)

    @staticmethod
    def _write_tombstone(info: SubagentInfo, cause: str, *, leaked_process: bool = False) -> None:
        """Best-effort tombstone write for abnormal exits.

        A run that is not persistent gets no tombstone. ``write_tombstone``
        refuses one whose live-run state or recorded mode says so, but a run
        that ended before its folder was seeded -- a declined spawn prompt, a
        stop or a reap while it waited for admission into startup -- has
        neither, so the run's own mode decides here.

        ``leaked_process`` records that the kill left the run's process
        standing. The tombstone still ends the run (orphan reconciliation will
        not report it again); the flag is what the next start reads to end the
        survivor without a second notice (``list_leaked_processes``).
        """
        if info.memory_mode != "persistent":
            return
        # Finalize the run's elapsed time ONCE, here, before any abnormal-exit
        # tombstone is written. Every abnormal arm (timeout, cancel, reap,
        # error) writes the tombstone before ``_run``'s finally / the reaper
        # assigns ``info.elapsed`` (subagent_manager/run.py, terminal.py), so
        # sampling the wall clock inline at write time produced a DIFFERENT
        # value than the terminal ``subagent_done`` event later carried, and a
        # replayed ``spawn_status`` (which reads this tombstone) disagreed with
        # the terminal event. Setting ``info.elapsed`` once and persisting that
        # single field makes both writers read the same value; the later
        # finalizers preserve an already-set value rather than re-sampling.
        if info.elapsed <= 0:
            info.elapsed = time.time() - info.started
        try:

            write_tombstone(
                info.id,
                cause=cause,
                recovery_action=tombstone_recovery_action(info.id, read_state(info.id) or {}),
                pid=info._pid,
                turns=info.turns,
                last_tool=info.last_tool,
                outcome=info.outcome,
                elapsed=info.elapsed,
                credits=info.credits,
                # ``cause`` is a coarse bucket ("error", "timeout"), which is
                # not enough to act on. ``info.error`` is in-memory only and
                # dies with the gateway, so without this the specific reason is
                # recoverable from nothing but the log.
                detail=(_redact(info.error)[:MAX_ERROR_DETAIL_LEN] if info.error else ""),
                **({"leaked_process": True} if leaked_process else {}),
            )
        except Exception:
            logger.debug("Failed to write tombstone for %s", info.id, exc_info=True)

    async def _write_state_off_loop(self, info: SubagentInfo, what: str, **fields: object) -> bool:
        return await self._run_events._write_state_off_loop_impl(info, what, **fields)

    async def _write_finished_result_off_loop(
        self, info: SubagentInfo, text: str | None, *, hold_conversation: bool = False
    ) -> bool:
        return await self._run_events._write_finished_result_off_loop_impl(
            info, text, hold_conversation=hold_conversation
        )

    async def _start_result_file(self, info: SubagentInfo, text: str) -> bool:
        return await self._run_events._start_result_file_impl(info, text)

    async def _drain_state_writer(
        self,
        info: SubagentInfo,
        what: str,
        writer: "asyncio.Future[Any]",
        *,
        bound: float | None = None,
    ) -> bool:
        return await self._run_events._drain_state_writer_impl(info, what, writer, bound=bound)

    def _hold_for_detached_writer(
        self, info: SubagentInfo, what: str, writer: "asyncio.Future[Any]"
    ) -> None:
        self._run_events._hold_for_detached_writer_impl(info, what, writer)

    async def _cap_unclaimed_result(self, info: SubagentInfo) -> None:
        await self._run_events._cap_unclaimed_result_impl(info)

    def _record_reap_ending(self, info: SubagentInfo, unfinished: str) -> None:
        self._run_events._record_reap_ending_impl(info, unfinished)

    async def _run_inner(self, info: SubagentInfo, session_key: str) -> None:
        usage = _RunCreditAccounting(info)
        info._credit_accounting = usage
        try:
            return await self._run_events._run_inner_impl(info, session_key, usage)
        finally:
            # Cancellation may land in the event consumer, outside the stream
            # generator's exception handlers. Settle before terminal reporting.
            try:
                usage.settle()
            finally:
                if info._credit_accounting is usage:
                    info._credit_accounting = None

    # Facades for the completion / stop-reason handling and the lane-slot
    # waits that live in subagent_manager/run.py.
    def _stop_recovery_wanted(self, info: SubagentInfo, stop: Any) -> bool:
        return self._run_events._stop_recovery_wanted_impl(info, stop)

    async def _yield_for_stop_recovery(self, info: SubagentInfo, event: Any) -> str | None:
        return await self._run_events._yield_for_stop_recovery_impl(info, event)

    async def _await_lane_resume(
        self, info: SubagentInfo, *, reason: str, timeout: float, request: bool = True
    ) -> bool:
        return await self._run_events._await_lane_resume_impl(
            info, reason=reason, timeout=timeout, request=request
        )

    async def _yield_for_dependency(self, info: SubagentInfo, signal: Any) -> bool | None:
        return await self._run_events._yield_for_dependency_impl(info, signal)

    async def _yield_for_infra_retry(self, info: SubagentInfo, infra: Any) -> str | None:
        return await self._run_events._yield_for_infra_retry_impl(info, infra)

    def _dependency_coordinator(self) -> Any:
        return self._monitor._dependency_coordinator_impl()

    def dependency_coordinator(self) -> Any:
        """The manager's ONE ``DependencyCoordinator`` (or None without a store).

        Public seam for the gateway: it registers this process-wide so the
        main chat and the monitors read the shared ``retry_at`` per scope, and
        subscribes the runner adapters' waiters to the same schedule.
        """
        return self._dependency_coordinator()

    async def dependency_coordinator_async(self) -> Any:
        """:meth:`dependency_coordinator` for an event-loop caller.

        The FIRST build runs ``rebuild()`` over every waiting row, so a loop
        caller that may be the first one takes it on the store's writer thread.
        """
        await self._admission.ensure_coordinator_async()
        return self._dependency_coordinator()

    def _taskq_pump(self) -> None:
        self._monitor._taskq_pump_impl()

    def _stop_error_text(self, info: SubagentInfo, stop: Any, event: Any) -> str:
        return self._run_events._stop_error_text_impl(info, stop, event)

    def _taskq_note_stop_recovery(self, info: SubagentInfo, data: dict[str, Any]) -> None:
        self._run_events._taskq_note_stop_recovery_impl(info, data)

    def _should_use_session_sharing(self, info: SubagentInfo) -> bool:
        return self._run_events._should_use_session_sharing_impl(info)

    def _sharing_plan(
        self,
        info: SubagentInfo,
        *,
        cfg: Any = None,
        child_aws_profile: ResolvedAwsProfile | None = None,
    ) -> _SharingPlan:
        return self._run_events._sharing_plan_impl(
            info, cfg=cfg, child_aws_profile=child_aws_profile
        )

    async def _ensure_dedicated_start_priced(self, info: SubagentInfo) -> None:
        return await self._run_events._ensure_dedicated_start_priced_impl(info)

    async def _create_shared_session(
        self, info: SubagentInfo, session_key: str, agent: str
    ) -> "LLMProvider":
        return await self._run_events._create_shared_session_impl(info, session_key, agent)

    def _gate_exit_reset(self, info: SubagentInfo) -> Callable[..., None]:
        return self._run_events._gate_exit_reset_impl(info)

    def _gate_wait_mark(self, info: SubagentInfo) -> Callable[..., None]:
        return self._run_events._gate_wait_mark_impl(info)

    # Facades for the session-start gate's late-adoption path;
    # implementations live in run.py.
    async def _await_late_start(
        self, info: SubagentInfo, session_key: str, exc: Exception
    ) -> "LLMProvider":
        return await self._run_events._await_late_start_impl(info, session_key, exc)

    async def _bind_shared_handle(
        self, info: SubagentInfo, session_key: str, runtime: "AcpRuntime", handle: Any
    ) -> "LLMProvider":
        return await self._run_events._bind_shared_handle_impl(info, session_key, runtime, handle)

    def _get_parent_runtime(self, parent_session_key: str) -> "AcpRuntime | None":
        return self._run_events._get_parent_runtime_impl(parent_session_key)

    @staticmethod
    def _is_cc_provider(provider: object) -> bool:
        """Check if a provider routes to Claude Code.

        Matches both the (dead) standalone ``ClaudeCodeProvider`` and the
        real default backend ``AcpProvider(acp_backend="claude")``.  The
        latter is what ``_sessions.get_or_create`` actually returns for the
        ``claude_code`` provider, so detecting it here is what makes the
        session-file cleanup target ``~/.claude`` instead of ``~/.kiro``.

        Asks ``SessionCapabilities.provider_seam`` through
        :func:`~kiro_crew.agent_sdk.capabilities.capabilities_of`, which replaced a
        lazy ``from kiro_crew.providers.acp import is_claude_backend``. The import
        was lazy because ``providers.acp`` sits in a providers -> session cycle;
        the SDK is in no cycle, so this one can live at module scope. The calling
        convention is unchanged: a shape that is not a provider answers False,
        which is what the old predicate's ``isinstance`` gate bought.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return True
        return capabilities_of(provider).provider_seam == PROVIDER_CLAUDE_CODE

    @staticmethod
    def _provider_label_of(provider: object) -> str:
        """Backend identity key for *provider*, persisted with the run's state.

        Mirrors ``_is_cc_provider`` in also matching the (dead) standalone
        ``ClaudeCodeProvider``, which the shared ``provider_label`` helper does
        not know about.
        """
        if ClaudeCodeProvider is not None and isinstance(provider, ClaudeCodeProvider):
            return PROVIDER_LABEL_CLAUDE
        # circular import: see _is_cc_provider.
        from kiro_crew.providers.acp import provider_label

        return provider_label(provider)

    def _cancel_task_intentionally(
        self,
        task: "asyncio.Task | asyncio.TimerHandle",  # type: ignore[type-arg]
        info: "SubagentInfo | None" = None,
        *,
        reason: str,
    ) -> None:
        """The single sanctioned chokepoint for INTENTIONALLY cancelling a
        manager-owned subagent task or follow-up watcher.

        Enforces the intentional-cancel contract mechanically instead of by
        docstring: a managed run's terminal marker MUST already be visible
        before the cancel is issued (``info.user_stopped`` / ``info.reaped`` /
        ``info.done`` / ``self._shutting_down``), otherwise ``_run``'s
        CancelledError arm classifies the cancel as unexpected and auto-respawns
        the run — a zombie respawn of work this call site meant to kill. The
        manager-owned follow-up watchers have no run recovery arm; identity
        with their manager field is their marker. A source-scan
        test asserts every raw ``.cancel()`` on these objects routes through here.

        Missing marker → loud error + the recovery budget is consumed
        defensively (``_cancel_retry_used``) so a mis-marked intentional
        cancel can never zombie-respawn; the cancel still proceeds.
        """
        retry_timer = any(
            task is timer
            for timer in (
                getattr(self, "_retained_claim_retry_handle", None),
                *(retry.handle for retry in getattr(self, "_queue_depth_retries", {}).values()),
                getattr(self, "_pressure_recheck_handle", None),
                getattr(self, "_memory_sampler_handle", None),
            )
        )
        followup_watcher = any(
            task is watcher for watcher in getattr(self, "_followup_watchers", {}).values()
        )
        marked = (
            retry_timer
            or followup_watcher
            or self._shutting_down
            or (info is not None and (info.user_stopped or info.reaped or info.done))
        )
        if not marked:
            logger.error(
                "Intentional cancel (reason=%s) issued WITHOUT a terminal "
                "marker — consuming the recovery budget defensively to "
                "prevent a zombie auto-respawn. Fix the call site: set "
                "user_stopped/reaped/done or _shutting_down BEFORE cancelling.",
                reason,
            )
            if info is not None:
                info._cancel_retry_used = True
        task.cancel()

    def _unqueue(self, agent_id: str, **kwargs: Any) -> dict | None:
        return self._cancellation._unqueue_impl(agent_id, **kwargs)

    def _report_queued_stop(
        self,
        params: dict,
        *,
        row_settled: bool = False,
        error: str = "",
        report_owed: bool = False,
    ) -> "asyncio.Task[bool] | None":
        return self._cancellation._report_queued_stop_impl(
            params, row_settled=row_settled, error=error, report_owed=report_owed
        )

    async def cancel(self, agent_id: str) -> bool:
        return await self._cancellation.cancel_impl(agent_id)

    async def cancel_for_parent(self, parent_session_key: str) -> tuple[int, int]:
        return await self._cancellation.cancel_for_parent_impl(parent_session_key)

    def has_live_or_queued_children(self, parent_session_key: str) -> bool:
        """Whether *parent_session_key* owns a live or queued run, read with no await.

        A side-effect-free query, unlike :meth:`snapshot_teardown_children` which
        arms the delivery gate and tears down follow-ups. Lets a caller decide whether
        to defer a parent's retirement without touching its children's completion path.
        """
        return self._cancellation.has_live_or_queued_children_impl(parent_session_key)

    async def has_pending_store_children(self, parent_session_key: str) -> bool:
        """Whether *parent_session_key* owns a waiting child only the store holds.

        The off-loop companion to :meth:`has_live_or_queued_children`: it reads the
        task store on the writer thread, so the identity-retire deferral can count a
        durable-only or window-held child (which the teardown cancels) without a
        synchronous store read on the event loop. Routed through
        ``taskq_pending_ids_for_async(include_window=True)`` -- the SAME reading the
        teardown's own store sweep uses (window rows kept, admitted rows left out,
        raises when the store is unavailable) -- so the decision and the cancel can
        never disagree about which rows count. A read failure propagates, so the
        caller can defer rather than read an accepted child as absent.
        """
        rows = await self._admission.taskq_pending_ids_for_async(
            parent_session_key, include_window=True
        )
        return bool(rows)

    def open_teardown_fence(self, parent_session_key: str) -> "set[str] | None":
        """Open the teardown fence for *parent_session_key* WITHOUT snapshotting.

        The identity sweep opens the fence while it still holds the registry lock,
        then decides retire-vs-defer at the async teardown. From the moment the
        fence opens, the admission path records every child it accepts for this
        parent into it (``note_teardown_store_accept`` under
        ``_teardown_fence_lock``), so a child admitted between the open and the
        off-loop read is seen by :meth:`has_fenced_children` rather than raced.
        Unlike :meth:`snapshot_teardown_children` this takes NO in-memory snapshot:
        it arms no delivery gate and tears down no follow-ups, so a parent that is
        then spared keeps its children's completion path intact.

        Returns the fence set, so a sweep that DEFERS can release exactly its own
        fence (:meth:`release_teardown_fence`) and never one a concurrent teardown
        opened. ``None`` when there is no store.
        """
        return self._cancellation.open_identity_sweep_fence_inner(parent_session_key)

    def has_fenced_children(self, parent_session_key: str) -> bool:
        """Whether any child was recorded in this parent's OPEN teardown fence.

        Synchronous and cheap (a set read under ``_teardown_fence_lock``): the
        admissions that landed since :meth:`open_teardown_fence`. Combined with
        :meth:`has_live_or_queued_children` (in memory) and
        :meth:`has_pending_store_children` (off-loop, pre-existing rows), it is the
        third source the retire-vs-defer decision reads.
        """
        return self._cancellation.has_fenced_children_impl(parent_session_key)

    def release_teardown_fence(
        self, parent_session_key: str, expected: "AbstractSet[str] | None" = None
    ) -> None:
        """Discard this parent's teardown fence when the decision was to DEFER.

        A spared parent runs no cancel sweep, so its fence has no consumer; drop it
        so a later real teardown of the same key opens a fresh one. *expected* is
        the fence the sweep opened; the drop happens only when the stored fence IS
        it, so a concurrent ``destroy``'s fence is never dropped out from under its
        own cancel.
        """
        self._cancellation.release_teardown_fence_impl(parent_session_key, expected)

    async def cancel_queued(
        self, agent_id: str, parent_session_key: str, *, stop_origin: str = ""
    ) -> bool:
        return await self._cancellation.cancel_queued_impl(
            agent_id, parent_session_key, stop_origin=stop_origin
        )

    def snapshot_teardown_children(self, parent_session_key: str) -> tuple[str, ...]:
        """Run ids under *parent_session_key*, taken with no await. Parent-end use.

        Marks them as teardown-cancelled in the same synchronous step. The mark is what
        stops a terminal report from injecting into the retired parent, and a run can
        finish on its own during the provider-teardown awaits that follow — so marking
        later, when the cancel actually runs, is too late for exactly the runs whose
        report is already on its way.

        Also opens the fence that records every row the store accepts for this parent
        from here on, so the cancel can stop the rows accepted before it -- the rows
        held only by the store, which no snapshot can name -- and spare a successor's.
        """
        selected = self._cancellation.snapshot_teardown_children_impl(parent_session_key)
        self._cancellation.note_teardown_snapshot(parent_session_key)
        return selected

    def commit_identity_sweep_retire(
        self, parent_session_key: str, expected: "AbstractSet[str]"
    ) -> bool:
        """Atomically decide the identity sweep's final commit-vs-defer.

        Returns True (commit) only when *expected* (the sweep's tentative fence)
        recorded no child since it opened; it then becomes this key's COMMITTED
        teardown fence, so the cancel's store sweep inherits any concurrent
        admission. False means a child arrived (or a newer sweep owns the key) and
        the caller must defer and release. Removes the F1 window between the final
        fenced-child check and the committed fence opening.
        """
        return self._cancellation.commit_identity_sweep_retire_inner(parent_session_key, expected)

    def snapshot_teardown_children_only(self, parent_session_key: str) -> tuple[str, ...]:
        """Select the run ids to cancel WITHOUT opening a fresh committed fence.

        The identity sweep's commit path has already promoted its tentative fence
        to the committed one (:meth:`commit_identity_sweep_retire`), so re-opening
        a fresh fence here would discard the promoted set and the concurrent
        admissions it carries. This takes only the selection half.
        """
        return self._cancellation.snapshot_teardown_children_impl(parent_session_key)

    async def cancel_for_teardown(
        self,
        agent_ids: "Sequence[str]",
        *,
        parent_session_key: str,
        verb: str = "",
    ) -> int:
        """Stop the snapshotted runs without reporting them to a retired parent.

        ``parent_session_key`` is carried so the teardown's one audit line can name the
        conversation whose runs these were; the ids themselves come from the snapshot,
        which is the only reading of them that cannot drift. The store rows accepted
        before that snapshot are stopped after them.
        """
        # The parent is gone, so the depth its slot advertised goes with it; a
        # frame a cancel path publishes after this records the new value again.
        self._published_depths.forget(parent_session_key)
        fence = self._cancellation.take_teardown_snapshot(parent_session_key)
        try:
            return await self._cancellation.cancel_for_teardown_impl(
                agent_ids,
                parent_session_key=parent_session_key,
                verb=verb,
                accepted_since=fence,
            )
        finally:
            # Recording stops only once the sweep's store read is behind it: a row
            # a successor queues during the awaits above must still be spared. A
            # read the store refused keeps it recording for the reaper's retry.
            self._cancellation.release_teardown_snapshot(fence)

    async def retry_owed_teardown_sweeps(self) -> int:
        return await self._cancellation.retry_owed_teardown_sweeps_impl()

    async def cancel_all(self) -> None:
        return await self._cancellation.cancel_all_impl()


# Every function the subagent_parts owners define runs on this module's globals, so a
# patch of ``kiro_crew.subagent.<name>`` reaches it wherever it now lives; see
# ``subagent_parts``. Run once, after every owner is imported and re-exported.
_compose_parts(
    globals(),
    SubagentManager,
    (
        _run_limits,
        _run_output,
        _agent_policy,
        _model_choice,
        _host_memory,
        _start_pricing,
    ),
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
