"""Session manager — maps conversation session keys to LLM provider sessions.

Each conversation (channel thread, dashboard slot, CLI) gets its own
LLMProvider instance. Sessions are
cleaned up after idle timeout (default 60 min).

Warm session pool: ``start_pool()`` pre-spawns kiro-cli processes so
``get_or_create()`` returns instantly.  After handing out a warm session,
a replacement is created in the background to maintain the target count.

Background session: ``BACKGROUND_KEY`` is a persistent shared session for
lightweight background work (heartbeat, lesson extraction).  It
stays alive between uses, serialized by the per-session semaphore.

How each backend compacts at ``cfg.session.autocompact_pct`` is described in
``session_parts.turn_context``, and the four process-cleanup mechanisms in
``session_parts.sweeps``: ``SessionManager`` binds its members from the
``session_parts`` owner modules listed at the end of this module.

Circuit breaker: after 5 consecutive failures on a session, the session
is force-reset instead of retrying forever.

Per-session semaphore: serializes prompts on the same session key so
concurrent messages on the same conversation don't interleave.

"""

from __future__ import annotations

import asyncio
import importlib
import inspect  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import sys
import time
from collections import deque  # noqa: F401
from collections.abc import Awaitable, Callable, Iterator  # noqa: F401
from contextlib import AbstractContextManager, contextmanager  # noqa: F401
from dataclasses import dataclass, field  # noqa: F401
from enum import Enum, auto  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast  # noqa: F401

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime, AcpSessionHandle
    from kiro_crew.session_capabilities import LoadedCapabilities  # noqa: F401

import kiro_crew._facade_mirror as _facade_mirror
from kiro_crew import model_registry, model_scope, platform_compat, shutdown_event  # noqa: F401
from kiro_crew.acp.client import advertised_model_ids, model_is_unusable
from kiro_crew.acp.types import (
    ACP_BACKEND_KIRO,
    PROVIDER_LABEL_CLAUDE,
    PROVIDER_LABEL_DEFAULT,
)
from kiro_crew.acp_backends import selectable_backends  # noqa: F401
from kiro_crew.agent import kiro_agents_dir_path
from kiro_crew.agent_discovery import _read_agent_spec, spec_model
from kiro_crew.agent_sdk.backend_identity import is_claude_backend_name
from kiro_crew.agent_sdk.backends import model_registry_namespace
from kiro_crew.agent_sdk.drivers.acp import resolve_pin_spelling_on
from kiro_crew.agent_spec_format import iter_agent_spec_files
from kiro_crew.cold_start_sizing import (
    cached_cold_start_concurrency,
    configure_runtime_spawn_width,
)
from kiro_crew.config import KiroCrewConfig, live
from kiro_crew.config.live import ConfigChange  # noqa: F401
from kiro_crew.config.loader import (  # noqa: F401
    AUTOCOMPACT_PCT_MAX,
    AUTOCOMPACT_PCT_MIN,
    CONTEXT_WARN_MARGIN_PCT,
    POOL_SIZE_MAX,
    build_provider_factory,
    crew_autocompact_pct,
    default_project_dir,
    normalize_agent_model,
    published_autocompact_pct,
)
from kiro_crew.config.paths import config_dir  # noqa: F401
from kiro_crew.constants import COMPACT_WAIT_TIMEOUT_SECS  # noqa: F401
from kiro_crew.executors import maintenance_executor, subprocess_executor  # noqa: F401
from kiro_crew.mcp_gateway.abort import schedule_abort_for
from kiro_crew.member_memory_auth import prune_legacy_member_pid_bindings  # noqa: F401
from kiro_crew.messaging.link import (  # noqa: F401
    UNBIND_REASON_SESSION_DESTROYED,
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    canonical_key,
    legacy_key,
    telemetry_channel_of,
)
from kiro_crew.metrics.events import SESSION_IDLE_EXPIRED, emit_counter  # noqa: F401
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.providers.base import CancelOutcome, LLMProvider
from kiro_crew.pycache_gc import PYCACHE_GC_INTERVAL_SECS, prune_pycache  # noqa: F401
from kiro_crew.runtime_ownership import PidRefcount  # noqa: F401
from kiro_crew.sandbox import cleanup_stale_sandbox_profiles  # noqa: F401
from kiro_crew.sel import sel
from kiro_crew.session_allocation import (  # noqa: F401
    AllocationConstants,
    AllocationDeps,
    InboundCallbackReservation,
    SessionAllocationService,
)
from kiro_crew.session_allocation import SessionBusyError as SessionBusyError  # noqa: F401
from kiro_crew.session_allocation import (  # noqa: F401
    SessionClosingError,
)
from kiro_crew.session_allocation import SessionEndingError as SessionEndingError  # noqa: F401
from kiro_crew.session_allocation import (  # noqa: F401
    SessionRegistryState,
)
from kiro_crew.session_allocation import (  # noqa: F401
    SpeculativeResumeRefused as SpeculativeResumeRefused,
)
from kiro_crew.session_allocation import (  # noqa: F401
    _collect_parent_runtime_kwargs,
    new_cold_start_semaphore,
)
from kiro_crew.session_allocation import (  # noqa: F401
    parent_work_scratch_dir as _parent_work_scratch_dir,
)
from kiro_crew.session_allocation import (  # noqa: F401
    widen_cold_start_semaphore,
)
from kiro_crew.session_background import (
    BackgroundRuntimeDeps,
    BackgroundSessionRuntime,
    _ProviderBgSession,
)
from kiro_crew.session_cleanup import CleanupDeps, CleanupState, SessionCleanup  # noqa: F401
from kiro_crew.session_compaction import (  # noqa: F401
    COMPACT_OUTCOME_COMPACTED,
    CompactionCoordinator,
    CompactionDeps,
    CompactionState,
)
from kiro_crew.session_lifecycle import (  # noqa: F401
    SessionLifecycleConstants,
    SessionLifecycleDeps,
    SessionLifecycleService,
    SessionLifecycleState,
    TornDown,
)
from kiro_crew.session_map import _kiro_sessions_dir  # noqa: F401
from kiro_crew.session_map import (  # noqa: F401
    MIRROR_OPT_OUT_FLAG,
    SUPPRESS_REPLAY_FLAG,
    BindListener,
)
from kiro_crew.session_map import SessionMap as SessionMap  # noqa: F401
from kiro_crew.session_map import (  # noqa: F401
    UnbindListener,
    set_bind_listener,
    set_unbind_listener,
)
from kiro_crew.session_parts import claims as _claims
from kiro_crew.session_parts import config_apply as _config_apply
from kiro_crew.session_parts import links as _links
from kiro_crew.session_parts import runtimes as _runtimes
from kiro_crew.session_parts import state_views as _views
from kiro_crew.session_parts import sweeps as _sweeps
from kiro_crew.session_parts import teardown as _teardown
from kiro_crew.session_parts import turn_context as _turn_context
from kiro_crew.session_parts.claims import (  # noqa: F401
    process_shutdown_requested,
)
from kiro_crew.session_parts.policy import (  # noqa: F401
    _AGENT_MODEL_CACHE_TTL,
    _BG_BLIND_RECYCLE_PROMPTS,
    _BG_RECYCLE_PCT,
    _CIRCUIT_BREAKER_THRESHOLD,
    _CLOSE_ALL_CONCURRENCY,
    _COMPACT_FAILURE_COOLDOWN_SECS,
    _COMPACT_MIN_EFFECT_PCT_POINTS,
    _COMPACT_RESULT_WAIT_MARGIN_SECS,
    _DRAIN_ACTIVE_TURNS_TIMEOUT_SECS,
    _MAX_POOL,
    _POST_COMPACT_RESET_PCT,
    _STATELESS_KEYS,
    _STATELESS_PREFIXES,
    _SUBAGENT_PREFIX,
    _WON_RACE_MAX_RETRIES,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_SESSION_EVICTION,
    BACKGROUND_AGENT,
    BACKGROUND_KEY,
    HEARTBEAT_KEY,
    POOL_DECISIONS,
    _bg_runtime_backends,
    _compact_result_wait_secs,
    _resolve_allocation_crew_identity,
    _session_model,
)
from kiro_crew.session_parts.provider_probes import (  # noqa: F401
    ClaudeCodeProvider,
    _context_pct_is_unknown,
    _is_claude_provider,
    _provider_effectively_alive,
    _provider_has_active_turn,
    _provider_has_unfinished_turn,
    _provider_turn_is_known_terminal,
    _provider_uses_kiro_identity_store,
    _watchdog_handle_of,
)
from kiro_crew.session_parts.record import (  # noqa: F401
    FirstTurnState,
    _Session,
    _unlink_session_queue,
    unlink_queued_temp_paths,
)
from kiro_crew.session_pid import (  # noqa: F401
    _build_child_map,
    _cleanup_orphaned_mcp_servers,
    _collect_active_pids,
    _kill_confirmed_and_writeback,
    _periodic_pid_sweep,
    _prune_stale_session_pid_files,
    _rss_mb_from_tree,
    _sync_kill_provider,
)
from kiro_crew.session_pid import _track_child_pids as _track_child_pids  # noqa: F401
from kiro_crew.session_pid import _track_pid as _track_pid  # noqa: F401
from kiro_crew.session_pid import _track_session_pid as _track_session_pid  # noqa: F401
from kiro_crew.session_pid import _untrack_child_pids as _untrack_child_pids  # noqa: F401
from kiro_crew.session_pid import _untrack_pid as _untrack_pid  # noqa: F401
from kiro_crew.session_pid import _untrack_session_pid as _untrack_session_pid  # noqa: F401
from kiro_crew.session_pid import (  # noqa: F401
    cleanup_orphaned_session_roots,
)
from kiro_crew.session_pid import (  # noqa: F401
    cleanup_orphaned_sessions as cleanup_orphaned_sessions,
)
from kiro_crew.session_pid import (  # noqa: F401
    find_orphan_mcp_candidates,
    get_session_rss_mb,
    kill_orphan_mcps,
)
from kiro_crew.session_pool import WarmPoolDeps, WarmSessionPool
from kiro_crew.session_scope_reap import reap_abandoned_agent_scopes  # noqa: F401
from kiro_crew.shell_audit_log import rotate_shell_audit_log  # noqa: F401
from kiro_crew.start_priority import PrioritySemaphore, StartPriority  # noqa: F401
from kiro_crew.stats import Stats
from kiro_crew.watchdog import CleanupHook, SessionWatchdog  # noqa: F401

logger = logging.getLogger(__name__)


def _is_claude_backend(provider: Any) -> bool:
    """Check if a provider drives claude-agent-acp via the ACP adapter.

    Returns True when an AcpProvider wraps claude-agent-acp (backend="claude"),
    which the public build offers: ``BASELINE_SELECTABLE_BACKENDS`` includes it, so
    the factory selects it whenever an operator has chosen it and the adapter is
    installed.

    The ``isinstance`` gate and the backend-string read stay here -- they are
    what makes this predicate mock-safe, and the shape they read is the
    provider's own business. Only the comparison is delegated to
    ``agent_sdk.backend_identity``, so this and ``AcpProvider.is_claude_backend``
    cannot disagree about which string counts as the claude backend.
    """
    from kiro_crew.providers.acp import AcpProvider  # circular import: providers -> session

    if not isinstance(provider, AcpProvider):
        return False
    backend = getattr(provider.client, "backend", "")
    return is_claude_backend_name(backend)


def _provider_label(provider: Any) -> str:
    """Backend identity key for *provider* — see ``providers.acp.provider_label``.

    Deferred import for the same reason ``_is_claude_backend`` defers it.
    """
    from kiro_crew.providers.acp import provider_label  # circular: providers -> session

    return provider_label(provider)


def _is_acp_provider(provider: Any) -> bool:
    """Runtime type check kept lazy across the providers/session cycle."""
    from kiro_crew.providers.acp import AcpProvider

    return isinstance(provider, AcpProvider)


def _load_acp_session_provider_type() -> Callable[..., LLMProvider]:
    """Load the shared-runtime provider adapter without an eager ACP edge."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    return AcpSessionProvider


def _load_acp_provider_type() -> type[LLMProvider]:
    """Load the registered-session ACP provider without an eager cycle."""
    from kiro_crew.providers.acp import AcpProvider

    return AcpProvider


def _load_child_process_helpers() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
]:
    """Resolve reset's process-tree helpers at call time for patch compatibility."""
    from kiro_crew.acp.client import (
        _capture_child_records,
        _get_child_pids,
        _kill_escaped_children,
    )

    return _capture_child_records, _get_child_pids, _kill_escaped_children


def child_process_helpers() -> tuple[
    Callable[..., Any],
    Callable[..., Any],
    Callable[..., Any],
]:
    """The client's ``(capture_child_records, get_child_pids, kill_escaped_children)``.

    The same triple the session teardown resolves for itself, for a caller that
    kills a session's process on its own handle after the teardown lost it (the
    cron reaper through ``kiro_crew.process_identity``): resolved at call time,
    so it is the one place outside the ACP layer that names these helpers, and a
    test's patch of the client module is what every caller's sweep then runs.
    """
    return _load_child_process_helpers()


def _load_allocation_watchdog_settings(crew: str, cfg: object = None) -> object:
    """The one seam this module reaches the ACP watchdog through.

    ``cfg`` is the already-loaded config the watcher hands to the hot-apply path
    so the re-clamp runs on the loop without a file read; ``None`` loads.
    """
    from kiro_crew.acp.session_handle import _load_watchdog_settings

    return _load_watchdog_settings(crew, cfg=cfg)


def _get_agent_model_cache() -> dict[str, tuple[str, float, float]]:
    if not hasattr(SessionManager, "_agent_model_cache"):
        SessionManager._agent_model_cache = {}
    return SessionManager._agent_model_cache


def detect_provider_switch(session_map: "SessionMap", session_key: str, new_provider: str) -> bool:
    """Detect if the provider for a session differs from the stored one.

    Returns True if the stored provider is set AND differs from *new_provider*
    AND a stored session ID exists. As a side effect, emits a SEL audit event
    when a switch is detected.

    This guards against attempting to resume incompatible session IDs across
    providers (kiro session IDs vs Claude Code UUIDs). Cross-provider continuity
    is achieved via KiroCrew's own history replay (build_session_replay), never
    via session_id translation.
    """
    stored_provider = session_map.get_provider(session_key) or PROVIDER_LABEL_DEFAULT
    if stored_provider == new_provider:
        return False
    # Only counts as a switch if there's actually a stored SID to discard
    stored_sid = session_map.get(session_key)
    if not stored_sid:
        return False
    sel().log_tool_invocation(
        session_key=session_key,
        agent="kirocrew",
        source="session",
        tool_name="provider_switch_detected",
        tool_kind="lifecycle",
        outcome="switch",
        metadata={
            "stored_provider": stored_provider,
            "new_provider": new_provider,
        },
    )
    logger.info(
        "Provider switch detected for %s: %s -> %s",
        session_key,
        stored_provider,
        new_provider,
    )
    return True


def _load_bg_runtime_types() -> tuple[Any, type[BaseException]]:
    """Load runtime types lazily across the session/acp import cycle."""
    from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead

    return AcpRuntime, AcpRuntimeDead


# Type alias for provider factory — accepts optional session key
ProviderFactory = Callable[..., LLMProvider]


class SessionManager:
    """Thread-keyed LLM provider pool with warm session pre-spawning."""

    _agent_model_cache: dict[str, tuple[str, float, float]] = {}

    def _allocation_deps(self) -> AllocationDeps:
        constants = AllocationConstants(
            won_race_max_retries=_WON_RACE_MAX_RETRIES,
            circuit_breaker_threshold=_CIRCUIT_BREAKER_THRESHOLD,
            agent_model_cache_ttl=lambda: _AGENT_MODEL_CACHE_TTL,
            background_key=BACKGROUND_KEY,
            heartbeat_key=HEARTBEAT_KEY,
            background_agent=BACKGROUND_AGENT,
            subagent_prefix=_SUBAGENT_PREFIX,
            stateless_prefixes=_STATELESS_PREFIXES,
            stateless_keys=_STATELESS_KEYS,
            provider_label_default=PROVIDER_LABEL_DEFAULT,
            provider_label_claude=PROVIDER_LABEL_CLAUDE,
        )
        return AllocationDeps(
            logger=logger,
            constants=constants,
            canonical_key=lambda key: canonical_key(key),
            legacy_key=lambda key: legacy_key(key),
            provider_has_active_turn=lambda provider: _provider_has_active_turn(provider),
            provider_effectively_alive=lambda provider: _provider_effectively_alive(provider),
            is_acp_provider=lambda provider: _is_acp_provider(provider),
            is_claude_provider=lambda provider: _is_claude_provider(provider),
            is_claude_backend=lambda provider: _is_claude_backend(provider),
            provider_label=lambda provider: _provider_label(provider),
            detect_provider_switch=lambda session_map, key, provider: detect_provider_switch(
                session_map, key, provider
            ),
            session_factory=lambda **kwargs: _Session(**kwargs),
            first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
            first_turn_fresh=FirstTurnState.FRESH,
            first_turn_resumed=FirstTurnState.RESUMED,
            runtime_types=lambda: _load_bg_runtime_types(),
            session_provider_type=lambda: _load_acp_session_provider_type(),
            unlink_session_queue=lambda session: _unlink_session_queue(session),
            unlink_queued_temp_paths=lambda kwargs: unlink_queued_temp_paths(kwargs),
            session_model=lambda cfg, agent, crew, **kw: _session_model(
                cfg, agent, crew_agent=crew, **kw
            ),
            load_config=lambda: KiroCrewConfig.load(),
            resolve_crew_identity=lambda cfg, agent, crew: _resolve_allocation_crew_identity(
                cfg, agent, crew
            ),
            load_watchdog_settings=lambda crew: _load_allocation_watchdog_settings(crew),
            advertised_model_ids=lambda models: advertised_model_ids(models),
            model_is_unusable=lambda model, advertised: model_is_unusable(model, advertised),
            model_pin_applies=lambda model, namespace, advertised: model_scope.pin_applies(
                model,
                namespace,
                advertised=advertised,
            ),
            provider_model_namespace=lambda provider: model_registry_namespace(
                getattr(getattr(provider, "client", provider), "backend", "") or ""
            ),
            resolve_pin_spelling=lambda model, advertised, provider: resolve_pin_spelling_on(
                model,
                advertised,
                getattr(getattr(provider, "client", provider), "backend", "") or "",
            ),
            to_provider_id=lambda model, provider: model_registry.to_provider_id(model, provider),
            to_acp_id=lambda model: model_registry.to_acp_id(model),
            inc_session_created=lambda: Stats().inc_session_created(),
            get_sel=lambda: sel(),
            get_subprocess_executor=lambda: subprocess_executor(),
            get_sync_kill_provider=lambda: _sync_kill_provider,
            agents_dir_path=lambda: kiro_agents_dir_path(),
            read_agent_spec=lambda path, *, operation, source: _read_agent_spec(
                path,
                operation=operation,
                source=source,
            ),
            spec_model=lambda spec: spec_model(spec),
            agent_model_cache=lambda: _get_agent_model_cache(),
        )

    def _lifecycle_deps(self) -> SessionLifecycleDeps:
        return SessionLifecycleDeps(
            logger=logger,
            load_config=lambda: KiroCrewConfig.load(),
            build_provider_factory=lambda cfg: build_provider_factory(cfg),
            default_project_dir=lambda: default_project_dir(),
            constants=lambda: SessionLifecycleConstants(
                max_pool=_MAX_POOL,
                background_key=BACKGROUND_KEY,
                stateless_prefixes=_STATELESS_PREFIXES,
                stateless_keys=_STATELESS_KEYS,
                close_all_concurrency=_CLOSE_ALL_CONCURRENCY,
                drain_active_turns_timeout_secs=_DRAIN_ACTIVE_TURNS_TIMEOUT_SECS,
                unbind_reason_session_destroyed=UNBIND_REASON_SESSION_DESTROYED,
                first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
                first_turn_fresh=FirstTurnState.FRESH,
                first_turn_resumed=FirstTurnState.RESUMED,
                provider_label_claude=PROVIDER_LABEL_CLAUDE,
            ),
            get_unlink_session_queue=lambda: _unlink_session_queue,
            get_child_process_helpers=lambda: _load_child_process_helpers(),
            get_subprocess_executor=lambda: subprocess_executor(),
            get_platform_compat=lambda: platform_compat,
            get_acp_provider_type=lambda: _load_acp_provider_type(),
            get_claude_code_provider_type=lambda: ClaudeCodeProvider,
            provider_label=lambda provider: _provider_label(provider),
            provider_has_unfinished_turn=lambda provider: _provider_has_unfinished_turn(provider),
            provider_turn_is_known_terminal=lambda provider: (
                _provider_turn_is_known_terminal(provider)
            ),
            provider_uses_kiro_identity_store=lambda provider: (
                _provider_uses_kiro_identity_store(provider)
            ),
            get_audit_logger=lambda: sel(),
            schedule_runtime_abort=lambda *args, **kwargs: schedule_abort_for(*args, **kwargs),
            monotonic=lambda: time.monotonic(),
        )

    # -- State views onto the collaborators' state objects (session_parts.state_views).
    _registry_state = _views._registry_state
    _lifecycle_state_boundary = _views._lifecycle_state_boundary
    _cleanup_state_boundary = _views._cleanup_state_boundary
    _cleanup_task = _views._cleanup_task
    _rss_max_mb = _views._rss_max_mb
    _idle_sweep_enabled = _views._idle_sweep_enabled
    _idle_timeout = _views._idle_timeout
    _stuck_reported = _views._stuck_reported
    _last_pycache_gc = _views._last_pycache_gc
    _active_dashboard_slots = _views._active_dashboard_slots
    _watchdog = _views._watchdog
    _identity_sweep_lock = _views._identity_sweep_lock
    pending_identity_sweep_fingerprint = _views.pending_identity_sweep_fingerprint
    identity_sweep_waiting_on = _views.identity_sweep_waiting_on
    _recycling = _views._recycling
    _suppress_replay = _views._suppress_replay
    _origin_links = _views._origin_links
    _on_recycled = _views._on_recycled
    _sessions = _views._sessions
    _lock = _views._lock
    _closing = _views._closing
    admission_closed = _views.admission_closed
    final_drain_started = _views.final_drain_started
    _update_pause_owned = _views._update_pause_owned
    update_restart_fenced = _views.update_restart_fenced
    _start_sem = _views._start_sem
    _starting_pids = _views._starting_pids
    _subagent_runtimes = _views._subagent_runtimes
    _subagent_runtime_locks = _views._subagent_runtime_locks
    _draining_subagent_runtimes = _views._draining_subagent_runtimes
    _continuable_keys = _views._continuable_keys
    _continuable_fallback = _views._continuable_fallback
    _compacting = _views._compacting
    _compact_cooldown_until = _views._compact_cooldown_until
    _compact_pending_verdict = _views._compact_pending_verdict
    _on_compacted = _views._on_compacted
    _pool_started = _views._pool_started
    _pool_size = _views._pool_size
    _pool_agent = _views._pool_agent
    _pool_ttl_secs = _views._pool_ttl_secs
    _pool_cwd = _views._pool_cwd
    _warm_pool = _views._warm_pool
    _pool_fill_lock = _views._pool_fill_lock
    _pool_health_task = _views._pool_health_task
    _pool_sweep_pids = _views._pool_sweep_pids
    _bg_runtime = _views._bg_runtime
    _bg_runtime_lock = _views._bg_runtime_lock
    _draining_bg_runtimes = _views._draining_bg_runtimes
    inbound_callback_count = _views.inbound_callback_count
    count = _views.count
    # -- Claims, leases, admission and the message queue (session_parts.claims).
    _allocation_boundary = _claims._allocation_boundary
    _fold_key = _claims._fold_key
    set_lifecycle_turn_active = _claims.set_lifecycle_turn_active
    mark_lifecycle_lease = _claims.mark_lifecycle_lease
    has_session = _claims.has_session
    get_provider = _claims.get_provider
    session_generation = _claims.session_generation
    _advance_session_generation = _claims._advance_session_generation
    session_keys = _claims.session_keys
    _has_allocation_reservation = _claims._has_allocation_reservation
    _spawn_in_flight = _claims._spawn_in_flight
    try_acquire = _claims.try_acquire
    capability_runtime_view = _claims.capability_runtime_view
    active_providers = _claims.active_providers
    any_active_turn = _claims.any_active_turn
    get_pid = _claims.get_pid
    _reacquire_and_validate = _claims._reacquire_and_validate
    _evict_stale_session = _claims._evict_stale_session
    _get_session_agent = _claims._get_session_agent
    _parent_runtime_kwargs = _claims._parent_runtime_kwargs
    parent_work_scratch_dir = _claims.parent_work_scratch_dir
    is_session_sharing_eligible = _claims.is_session_sharing_eligible
    aws_profile_split_possible = _claims.aws_profile_split_possible
    project_aws_profile = _claims.project_aws_profile
    aws_profile_differs_from_parent = _claims.aws_profile_differs_from_parent
    runtime_pids = _claims.runtime_pids
    _append_companion_runtime_rows = _claims._append_companion_runtime_rows
    get_or_create = _claims.get_or_create
    ending_key = _claims.ending_key
    allocation_requested_model = _claims.allocation_requested_model
    retire_effort_override = _claims.retire_effort_override
    record_success = _claims.record_success
    record_failure = _claims.record_failure
    reserve_inbound_callback = _claims.reserve_inbound_callback
    begin_turn = _claims.begin_turn
    pause_turn_admission_for_update = _claims.pause_turn_admission_for_update
    fence_update_restart = _claims.fence_update_restart
    resume_turn_admission_after_update = _claims.resume_turn_admission_after_update
    mark_continuable = _claims.mark_continuable
    unmark_continuable = _claims.unmark_continuable
    set_continuable_fallback = _claims.set_continuable_fallback
    _is_continuable_key = _claims._is_continuable_key
    is_continuable = _claims.is_continuable
    resumable_sid = _claims.resumable_sid
    resumable_hint = _claims.resumable_hint
    mapped_sid = _claims.mapped_sid
    allocation_predecessor = _claims.allocation_predecessor
    mapped_session_keys = _claims.mapped_session_keys
    seed_conversation = _claims.seed_conversation
    forget_conversation = _claims.forget_conversation
    forget_conversation_if_sid = _claims.forget_conversation_if_sid
    conversation_provider = _claims.conversation_provider
    release = _claims.release
    _safe_cleanup = _claims._safe_cleanup
    is_busy = _claims.is_busy
    touch = _claims.touch
    enqueue = _claims.enqueue
    dequeue = _claims.dequeue
    cancel_queued = _claims.cancel_queued
    is_cancelled = _claims.is_cancelled
    detach_queue = _claims.detach_queue
    restore_queue = _claims.restore_queue
    clear_queue = _claims.clear_queue
    is_provider_alive = _claims.is_provider_alive
    get_approval_policy = _claims.get_approval_policy
    get_agent = _claims.get_agent
    get_agent_selection = _claims.get_agent_selection
    set_approval_policy = _claims.set_approval_policy
    # -- Background, warm-pool and companion runtimes (session_parts.runtimes).
    start_pool = _runtimes.start_pool
    _ensure_background = _runtimes._ensure_background
    _configured_bg_backend_raw = _runtimes._configured_bg_backend_raw
    _configured_bg_backend = _runtimes._configured_bg_backend
    _bg_backend_supports_runtime = _runtimes._bg_backend_supports_runtime
    _reap_drained_bg_runtimes_locked = _runtimes._reap_drained_bg_runtimes_locked
    _detach_bg_runtime_locked = _runtimes._detach_bg_runtime_locked
    _displace_bg_runtime_locked = _runtimes._displace_bg_runtime_locked
    _retire_stale_backend_bg_runtime = _runtimes._retire_stale_backend_bg_runtime
    _reap_idle_stale_bg_runtime = _runtimes._reap_idle_stale_bg_runtime
    _provider_backed_bg_session = _runtimes._provider_backed_bg_session
    get_bg_session = _runtimes.get_bg_session
    get_subagent_runtime = _runtimes.get_subagent_runtime
    release_subagent_runtime = _runtimes.release_subagent_runtime
    _get_or_bootstrap_run_runtime = _runtimes._get_or_bootstrap_run_runtime
    open_task_session = _runtimes.open_task_session
    _fill_warm_pool = _runtimes._fill_warm_pool
    _POOL_DISCARD_TIMEOUT = _runtimes._POOL_DISCARD_TIMEOUT
    _dispatch_hard_kill = staticmethod(_runtimes._dispatch_hard_kill)
    _discard_pool_provider = _runtimes._discard_pool_provider
    _record_pool_decision = _runtimes._record_pool_decision
    _claim_from_pool = _runtimes._claim_from_pool
    _drain_and_claim = _runtimes._drain_and_claim
    _schedule_replenish = _runtimes._schedule_replenish
    _pool_pids = _runtimes._pool_pids
    _in_flight_pids = _runtimes._in_flight_pids
    _companion_runtime_pids = _runtimes._companion_runtime_pids
    _POOL_HEALTH_INTERVAL = _runtimes._POOL_HEALTH_INTERVAL
    _pool_health_loop = _runtimes._pool_health_loop
    _sweep_warm_pool_once = _runtimes._sweep_warm_pool_once
    recycle_background = _runtimes.recycle_background
    recycle_heartbeat = _runtimes.recycle_heartbeat
    _retire_kiro_warm_pool = _runtimes._retire_kiro_warm_pool
    _mark_identity_epoch = _runtimes._mark_identity_epoch
    drain_warm_pool = _runtimes.drain_warm_pool
    warm_providers = _runtimes.warm_providers
    # -- Context budget, compaction and owed history (session_parts.turn_context).
    compact_wait_budget_secs = _turn_context.compact_wait_budget_secs
    check_context_usage = _turn_context.check_context_usage
    effective_autocompact_pct = _turn_context.effective_autocompact_pct
    compaction_due_after_turn = _turn_context.compaction_due_after_turn
    agent_autocompact_default = _turn_context.agent_autocompact_default
    set_autocompact_pct = _turn_context.set_autocompact_pct
    compact_if_needed = _turn_context.compact_if_needed
    set_compact_callback = _turn_context.set_compact_callback
    set_compacting_callback = _turn_context.set_compacting_callback
    is_compacting = _turn_context.is_compacting
    compaction_waits_for_turn = _turn_context.compaction_waits_for_turn
    mark_needs_reinjection = _turn_context.mark_needs_reinjection
    consume_needs_reinjection = _turn_context.consume_needs_reinjection
    provider_switch_replay_pending = _turn_context.provider_switch_replay_pending
    mark_provider_switch_replay = _turn_context.mark_provider_switch_replay
    consume_provider_switch_replay = _turn_context.consume_provider_switch_replay
    first_turn_history_owed_pending = _turn_context.first_turn_history_owed_pending
    unlanded_marker_pending = _turn_context.unlanded_marker_pending
    consume_first_turn_history_owed = _turn_context.consume_first_turn_history_owed
    consume_replay_suppression = _turn_context.consume_replay_suppression
    _consume_persisted_replay_suppression = _turn_context._consume_persisted_replay_suppression
    suppress_replay_persistently = _turn_context.suppress_replay_persistently
    _compaction_gate_decision = _turn_context._compaction_gate_decision
    _trigger_compaction = _turn_context._trigger_compaction
    _compact_session = _turn_context._compact_session
    _recycle_held = _turn_context._recycle_held
    _recycle_unmanaged = _turn_context._recycle_unmanaged
    _compact_in_place = _turn_context._compact_in_place
    _settle_compact_cooldown = _turn_context._settle_compact_cooldown
    _judge_compact_effect = _turn_context._judge_compact_effect
    _reset_still_critical = _turn_context._reset_still_critical
    _fire_compact_callback = _turn_context._fire_compact_callback
    # -- Channel links, mirrors and origins (session_parts.links).
    set_slack_link = _links.set_slack_link
    get_slack_link = _links.get_slack_link
    clear_slack_link = _links.clear_slack_link
    set_slack_paused = _links.set_slack_paused
    is_slack_paused = _links.is_slack_paused
    get_session_for_thread = _links.get_session_for_thread
    channel_key_for_stem = _links.channel_key_for_stem
    set_mirror_link = _links.set_mirror_link
    get_mirror_link = _links.get_mirror_link
    mirror_accepts_inbound = _links.mirror_accepts_inbound
    has_mirror_row = _links.has_mirror_row
    mirror_link_nonce = _links.mirror_link_nonce
    slack_link_nonce = _links.slack_link_nonce
    set_mirror_opt_out = _links.set_mirror_opt_out
    mirror_opt_out = _links.mirror_opt_out
    batched_save = _links.batched_save
    set_origin_link = _links.set_origin_link
    get_origin_link = _links.get_origin_link
    find_mirror_sessions = _links.find_mirror_sessions
    mirror_claim_blockers = _links.mirror_claim_blockers
    clear_mirror_link = _links.clear_mirror_link
    clear_mirror_link_if = _links.clear_mirror_link_if
    replace_mirror_link_if = _links.replace_mirror_link_if
    clear_slack_link_if = _links.clear_slack_link_if
    clear_mirror_links_at = _links.clear_mirror_links_at
    aflush = _links.aflush
    set_mirror_paused = _links.set_mirror_paused
    is_mirror_paused = _links.is_mirror_paused
    set_channel = _links.set_channel
    get_channel = _links.get_channel
    find_key_by_sid = _links.find_key_by_sid
    reserve_generation = _links.reserve_generation
    max_generation = _links.max_generation
    set_thread = _links.set_thread
    get_thread = _links.get_thread
    # -- Live config apply (session_parts.config_apply).
    _CONFIG_SECTIONS = _config_apply._CONFIG_SECTIONS
    _FACTORY_CONFIG_PATHS = _config_apply._FACTORY_CONFIG_PATHS
    _on_config_change = _config_apply._on_config_change
    refresh_defaults = _config_apply.refresh_defaults
    _sync_autocompact_pct = _config_apply._sync_autocompact_pct
    reload_provider_factory = _config_apply.reload_provider_factory
    widen_cold_start_queues = _config_apply.widen_cold_start_queues
    # -- Reset, retirement, stop, drain and close (session_parts.teardown).
    _lifecycle_boundary = _teardown._lifecycle_boundary
    reset = _teardown.reset
    teardown_scope = _teardown.teardown_scope
    tearing_down = _teardown.tearing_down
    set_child_teardown_handler = _teardown.set_child_teardown_handler
    set_recycle_callback = _teardown.set_recycle_callback
    _fire_recycle_callback = _teardown._fire_recycle_callback
    remove = _teardown.remove
    retire_kiro_identity_sessions = _teardown.retire_kiro_identity_sessions
    flag_identity_stamp_mismatches = _teardown.flag_identity_stamp_mismatches
    _retire_kiro_subagent_runtimes = _teardown._retire_kiro_subagent_runtimes
    _retire_kiro_bg_runtime = _teardown._retire_kiro_bg_runtime
    remove_if_unclaimed = _teardown.remove_if_unclaimed
    end_children_for = _teardown.end_children_for
    destroy = _teardown.destroy
    destroy_if = _teardown.destroy_if
    discard_conversation = _teardown.discard_conversation
    drain_active_turns = _teardown.drain_active_turns
    close_all = _teardown.close_all
    absorb_orphaned_release = _teardown.absorb_orphaned_release
    adopt_turn = _teardown.adopt_turn
    cancel_current = _teardown.cancel_current
    cancel_owned_turn = _teardown.cancel_owned_turn
    stop_turn = _teardown.stop_turn
    stop_generation = _teardown.stop_generation
    note_stop = _teardown.note_stop
    open_replay_gap = _teardown.open_replay_gap
    close_replay_gap = _teardown.close_replay_gap
    await_replay_gap = _teardown.await_replay_gap
    _send_abort_for_session = _teardown._send_abort_for_session
    _eager_respawn = _teardown._eager_respawn
    drain_all_providers = _teardown.drain_all_providers
    # -- The cleanup loop and its watchdog hooks (session_parts.sweeps).
    runtime_reconciler = _sweeps.runtime_reconciler
    _cleanup_deps = _sweeps._cleanup_deps
    _cleanup_boundary = _sweeps._cleanup_boundary
    _ensure_cleanup_task = _sweeps._ensure_cleanup_task
    set_subagent_probe = _sweeps.set_subagent_probe
    _has_attached_subagents = _sweeps._has_attached_subagents
    set_injection_probe = _sweeps.set_injection_probe
    _has_pending_injection = _sweeps._has_pending_injection
    _expire_idle_hook = _sweeps._expire_idle_hook
    _bg_drain_reap_hook = _sweeps._bg_drain_reap_hook
    _orphan_mcp_hook = _sweeps._orphan_mcp_hook
    _reap_agent_scopes_hook = _sweeps._reap_agent_scopes_hook
    _rss_threshold_check = _sweeps._rss_threshold_check
    _stuck_turn_check = _sweeps._stuck_turn_check
    _cleanup_loop = _sweeps._cleanup_loop
    set_active_dashboard_slots = _sweeps.set_active_dashboard_slots
    _expire_idle = _sweeps._expire_idle

    @staticmethod
    def set_unbind_listener(callback: UnbindListener | None) -> None:
        """Register the sink notified when an inbound resume binding is removed.

        The registry it writes is the session map's, shared by every instance, so
        a removal performed through a throwaway map is announced too.
        """
        set_unbind_listener(callback)

    @staticmethod
    def set_bind_listener(callback: BindListener | None) -> None:
        """Register the sink notified when a channel binding COMMITS.

        Same registry reasoning as the unbind sink above: it is the session map's, shared
        by every instance, so a binding made through a throwaway map is announced too.
        """
        set_bind_listener(callback)

    def __init__(
        self,
        cfg: KiroCrewConfig,
        provider_factory: ProviderFactory | None = None,
    ):
        self._cfg = cfg
        # Baseline for adopt-on-change (see _sync_autocompact_pct). Captured here
        # so a caller that hands in a config with its own threshold -- a test, or
        # any embedder constructing a manager directly -- keeps that value until
        # a LOAD publishes a different one, rather than having it replaced by
        # whatever the last load in this process happened to publish.
        self._adopted_autocompact_pct = published_autocompact_pct()
        self._provider_factory = provider_factory
        # Installed by the dashboard once its state exists (set_subagent_probe);
        # None means "no dashboard, so no children can be attached".
        self._subagent_probe: "Callable[[str], bool | Awaitable[bool]] | None" = None
        # Installed by the dashboard once the Kiro prerequisite service exists
        # (its ``read_spawn_identity``); None means "no identity store to stamp
        # from" -- CLI entry points and tests -- and every spawn stays unstamped,
        # which is the pre-stamping status quo. When wired, each kiro-backed
        # provider records the account the store held as its process started,
        # so the turn gate can retire a child that provably spawned under a
        # different account than the live one even when no read ever observed
        # the interim (see ``flag_identity_stamp_mismatches``).
        self.spawn_identity_reader: "Callable[[], Awaitable[str]] | None" = None
        # Installed by the gateway once it owns this manager (set_injection_probe);
        # None means "no gateway, so no completion injection can be in flight".
        self._injection_probe: "Callable[[str], bool] | None" = None
        # Both cold-start widths are restart settings. Built on the gateway boot
        # path and the event loop, so "auto" is sized from the cached host
        # reading only (the floors until it exists); widen_cold_start_queues
        # grows both once the gateway's post-bind sizing has read the host.
        configure_runtime_spawn_width(getattr(cfg, "agent", None))
        self._allocation_state = SessionRegistryState(
            start_sem=new_cold_start_semaphore(
                cached_cold_start_concurrency(getattr(cfg, "agent", None))
            )
        )
        self._allocation_boundary()
        self._lifecycle_state = SessionLifecycleState()
        self._compaction_state = CompactionState()
        self._background_tasks: set[asyncio.Task] = set()  # type: ignore[type-arg]
        self._session_map = SessionMap()

        self._pool = WarmSessionPool(
            cast(Any, self),
            WarmPoolDeps(
                logger=logger,
                default_project_dir=lambda: default_project_dir(),
                get_sync_kill_provider=lambda: _sync_kill_provider,
                get_subprocess_executor=lambda: subprocess_executor(),
                get_pid_exists=lambda: platform_compat.pid_exists,
                get_identity_predicate=lambda: _provider_uses_kiro_identity_store,
                get_discard_timeout=lambda: self._POOL_DISCARD_TIMEOUT,
                get_health_interval=lambda: self._POOL_HEALTH_INTERVAL,
                get_recorder=lambda: get_recorder(),
                telemetry_channel_of=lambda key: telemetry_channel_of(key),
                max_pool=_MAX_POOL,
                pool_decisions=POOL_DECISIONS,
            ),
        )
        # Callback fired when a session expires (idle or orphaned).
        # Used by HistoryConsolidator to trigger skill extraction.
        self.on_session_expire: Callable[[str], None] | None = None
        # Fired by the stuck_turn hook for a turn whose consumer has stopped
        # pulling events. A seam, not a policy: this class only reports, and a
        # surface that can reach the user (a dashboard notification, a Slack DM)
        # decides what to do with it. Args: (session_key, parked_secs).
        self.on_stuck_turn: Callable[[str, float], None] | None = None

        self._compaction = CompactionCoordinator(
            self,
            CompactionDeps(
                logger=logger,
                is_claude_backend=lambda provider: _is_claude_backend(provider),
                is_cc_managed=lambda provider: bool(
                    ClaudeCodeProvider is not None
                    and isinstance(provider, ClaudeCodeProvider)
                    and provider.connection_mode == "per_session"
                ),
                get_recorder=lambda: get_recorder(),
                context_pct_is_unknown=lambda provider: _context_pct_is_unknown(provider),
                unlink_session_queue=lambda session: _unlink_session_queue(session),
                compact_wait_timeout_secs=lambda: self.compact_wait_budget_secs(),
                compact_result_wait_secs=lambda elapsed, budget: _compact_result_wait_secs(
                    elapsed, budget
                ),
                context_warn_margin_pct=CONTEXT_WARN_MARGIN_PCT,
                compact_result_wait_margin_secs=_COMPACT_RESULT_WAIT_MARGIN_SECS,
                compact_failure_cooldown_secs=_COMPACT_FAILURE_COOLDOWN_SECS,
                compact_min_effect_pct_points=_COMPACT_MIN_EFFECT_PCT_POINTS,
                post_compact_reset_pct=_POST_COMPACT_RESET_PCT,
                # Reads ``self._cfg`` at call time: the config watcher replaces it
                # on every write, so a crew's new default reaches its live
                # sessions on their next reading, with no file read on the loop.
                agent_autocompact_pct=lambda session: crew_autocompact_pct(
                    self._cfg, getattr(session, "capability_member", "") or ""
                ),
            ),
            state=self._compaction_state,
        )

        self._background_runtime = BackgroundSessionRuntime(
            cast(Any, self),
            BackgroundRuntimeDeps(
                logger=logger,
                background_key=BACKGROUND_KEY,
                background_agent=BACKGROUND_AGENT,
                heartbeat_key=HEARTBEAT_KEY,
                runtime_agent=BACKGROUND_AGENT,
                acp_backend_kiro=ACP_BACKEND_KIRO,
                bg_recycle_pct=_BG_RECYCLE_PCT,
                bg_blind_recycle_prompts=_BG_BLIND_RECYCLE_PROMPTS,
                rss_max_mb=lambda: self._rss_max_mb,
                tree_rss_mb=lambda pid: get_session_rss_mb(pid),
                runtime_backends=lambda: _bg_runtime_backends(),
                context_pct_is_unknown=lambda provider: _context_pct_is_unknown(provider),
                runtime_types=lambda: _load_bg_runtime_types(),
                session_factory=lambda **kwargs: _Session(**kwargs),
                first_turn_nothing_armed=FirstTurnState.NOTHING_ARMED,
                provider_bg_session_factory=lambda session: _ProviderBgSession(session),
                session_closing_error=lambda message: SessionClosingError(message),
            ),
        )
        self._lifecycle_boundary()

        _rss_cfg = getattr(cfg.session, "watchdog_rss_max_mb", 0)
        self._cleanup_state = CleanupState(
            rss_max_mb=max(0, _rss_cfg) if isinstance(_rss_cfg, int) else 0,
        )
        self._cleanup_boundary()
        # The watcher holds the bound method weakly, so a manager that a test or
        # a provider reload discards falls out of the registry on its own.
        self._config_sub = live.subscribe(
            *self._CONFIG_SECTIONS,
            callback=self._on_config_change,
            name="SessionManager",
        )

    async def _rebind_live_watchdogs(self, cfg: KiroCrewConfig) -> None:
        """Re-snapshot ``watchdog.*`` on every live session handle.

        New sessions resolve the windows at spawn; a live handle keeps the
        snapshot it was built with. The rebind runs ``_load_watchdog_settings``
        again for the handle's own crew identity -- override overlay and
        prompt-ceiling clamp included -- rather than copying raw values, and it
        reads the config the watcher already loaded, so nothing touches disk on
        the loop. The dispatch loop reads the snapshot every tick, so the new
        windows govern the next check.
        """
        async with self._lock:
            handles = [
                handle
                for handle in (_watchdog_handle_of(s.provider) for s in self._sessions.values())
                if handle is not None
            ]
        for handle in handles:
            try:
                crew = getattr(handle, "_crew_agent", "") or ""
                handle.rebind_watchdog(crew, _load_allocation_watchdog_settings(crew, cfg))
            except Exception:
                logger.debug("watchdog rebind failed for a live handle", exc_info=True)

    @staticmethod
    def _resolve_agent_model(agent: str) -> str:
        """Resolve model from agent config file. Cached at class level with
        mtime + TTL invalidation.

        The cache MUST NOT pin a resolution forever — in particular the
        ``"auto"`` miss (agent JSON absent, or present with no explicit model).
        A later create/edit of the agent's JSON has to be observed:

        - **mtime**: the agents-dir mtime is bumped by any add/remove/rename of
          a ``*.json`` file, so a newly-created agent config invalidates the
          whole cache immediately.
        - **TTL**: an in-place edit of an existing file does not change the dir
          mtime, so entries also expire after ``_AGENT_MODEL_CACHE_TTL`` seconds
          and are re-resolved.
        """
        agents_dir = kiro_agents_dir_path()
        try:
            dir_mtime = agents_dir.stat().st_mtime
        except OSError:
            dir_mtime = 0.0
        now = time.monotonic()

        if not hasattr(SessionManager, "_agent_model_cache"):
            SessionManager._agent_model_cache = {}  # type: ignore[attr-defined]
        cache = SessionManager._agent_model_cache  # type: ignore[attr-defined]

        entry = cache.get(agent)
        if entry is not None:
            cached_model, cached_mtime, cached_ts = entry
            if cached_mtime == dir_mtime and (now - cached_ts) < _AGENT_MODEL_CACHE_TTL:
                return cached_model

        model = "auto"
        try:
            # Use the SAME directory as the cache stamp and preserve the former
            # native-order, first-match scan. The async caller hands this to a
            # thread (the walk and the reads are filesystem work), and a match
            # still stops all later spec reads rather than building a full map
            # on every cache miss / TTL expiry.
            for agent_file in iter_agent_spec_files(agents_dir, ordered=False):
                data = _read_agent_spec(
                    agent_file,
                    operation="resolve_agent_model",
                    source="unknown",
                )
                if data is None:
                    continue
                if data.get("name") == agent or agent_file.stem == agent:
                    model = spec_model(data)
                    break
        except Exception:
            pass
        cache[agent] = (model, dir_mtime, now)
        return model

    def commit_provider_switch_replay_sid(self, key: str) -> bool:
        """Settle replay, promoting the live ACP SID when one was deferred.

        Allocation leaves the prior resumable SID in ``SessionMap`` only for an
        ACP provider that explicitly defers promotion. Other providers publish
        their own SID during allocation, so a landed replay consumes the lease
        without another mapping write. This keeps cross-provider history replay
        one-shot instead of re-arming forever on a non-ACP session.
        """
        folded = self._fold_key(key)
        session = self._sessions.get(folded)
        if session is None or not session.provider_switch_replay:
            return False
        if session.retire_on_identity_change:
            session.provider_switch_replay = False
            return True
        if not _is_acp_provider(session.provider):
            session.provider_switch_replay = False
            return True
        client = getattr(session.provider, "client", None)
        sid = getattr(client, "_session_id", None)
        if not isinstance(sid, str) or not sid:
            return False
        self._session_map.set(
            folded,
            sid,
            provider=_provider_label(session.provider),
            cwd=session.provider.cwd,
        )
        session.provider_switch_replay = False
        return True

    def mark_first_turn_history_owed(self, key: str) -> bool:
        """Arm the durable FRESH first-turn history debt.

        The ``first_turn`` observation is consumed at claim time, so a turn that
        assembled Kiro Crew history and then failed before the provider accepted
        it loses the record that history was still owed. The dashboard runner
        arms this from the FRESH observation; it is read (not cleared) each turn
        and settled once a non-slash context-bearing turn KEEPS its history with
        the provider — a normal ``end_turn``, any output-emitted exit that was not
        cancelled and had no empty-response verdict, or a completed compaction —
        or a reset suppresses it, so a pre-output failure the transient path
        re-queues onto the same live session rebuilds history instead of
        replaying bare.

        The debt is in memory and does not survive a restart, so it is also
        written onto the session map as the mapped sid's unlanded marker
        (``SessionMap.mark_sid_unlanded``). After a restart ``SessionMap.get``
        then refuses that sid on a backend it cannot stat, and the next turn
        cold-starts with the replay instead of loading an empty native session.
        The write is scheduled, not awaited: the caller awaits :meth:`aflush`
        before the turn goes out when :meth:`unlanded_marker_pending` says a
        marker is owed, so the marker is on disk off the event loop.
        """
        folded = self._fold_key(key)
        session = self._sessions.get(folded)
        if session is None:
            return False
        session.first_turn_history_owed = True
        if _is_acp_provider(session.provider):
            sid = getattr(getattr(session.provider, "client", None), "_session_id", None)
            if isinstance(sid, str) and sid:
                self._session_map.mark_sid_unlanded(folded, sid)
        return True


# The ``session_parts`` owners hold code that was this module's own. This module binds every
# name its own code reads and serves the other moved names from their owners; through
# ``_facade_mirror`` a write of either reaches every owner binding the same object. No
# owner imports this module. ``test/test_session_bindings.py`` pins the binding and reach.
#: The session manager's owner modules, lowest layer first: the first holder answers a read.
_SESSION_OWNERS: tuple[str, ...] = (
    "kiro_crew.session_parts.policy",
    "kiro_crew.session_parts.provider_probes",
    "kiro_crew.session_parts.record",
    "kiro_crew.session_parts.state_views",
    "kiro_crew.session_parts.claims",
    "kiro_crew.session_parts.runtimes",
    "kiro_crew.session_parts.turn_context",
    "kiro_crew.session_parts.links",
    "kiro_crew.session_parts.config_apply",
    "kiro_crew.session_parts.teardown",
    "kiro_crew.session_parts.sweeps",
)
for _owner in _SESSION_OWNERS:
    importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it. The
#: surface is this module's names before the split: its own, and those its helpers moved to.
_EXPORTS: dict[str, str] = {}
_MIRROR = _facade_mirror.install(
    sys.modules[__name__],
    _SESSION_OWNERS,
    excluded=(),
    surface=frozenset(globals()).union(*(vars(sys.modules[o]) for o in _SESSION_OWNERS[:3])),
)
_EXPORTS.update(_MIRROR.served)
# Installed last, so the mirror is live for every caller but never runs while this
# module is still binding its own names.
sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
# ``from kiro_crew.session import *`` binds what it bound before the split: every public
# name this module binds or serves, less the mirror's own plumbing. Derived, so it is not
# a second list of names to keep in step.
__all__ = sorted(
    name
    for name in set(globals()) | set(_EXPORTS)
    if not name.startswith("_") and name not in ("importlib", "sys")
)

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.session_parts.policy import (  # noqa: F401
        _CHANNEL_PREFIX,
        _COMPACT_RESULT_WAIT_FLOOR_SECS,
        _GEN_SUFFIX_RE,
        _MAX_ORIGIN_LINKS,
        _PERSISTENT_KEYS,
        _SIDE_PREFIX,
        _STUCK_TURN_REPORT_SECS,
        _THREAD_PREFIX,
        _WORKFLOW_AUTHOR_PREFIX,
        _WORKFLOW_POOL_PREFIX,
        CONSOLIDATE_KEY,
        _CompactCallback,
        _opt_out_key,
        _RecycleCallback,
        _resolve_compact_wait_secs,
    )
    from kiro_crew.session_parts.provider_probes import (  # noqa: F401
        _provider_background_launch,
    )
    from kiro_crew.session_parts.record import (  # noqa: F401
        StopOutcome,
    )

    # The owners annotate with these names, which the agent-SDK boundary lets only this
    # module import (``scripts/check_agent_sdk_boundary.py``); they read them from here.
    _OWNER_ANNOTATIONS = (AcpRuntime, AcpSessionHandle, CancelOutcome)
