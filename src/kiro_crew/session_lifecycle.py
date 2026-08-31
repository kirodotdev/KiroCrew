"""Session teardown and identity-retirement boundary.

This leaf module owns the lifecycle state that survives across individual
provider objects, while the ``SessionManager`` facade remains the authority for
session allocation, warm-pool state, background runtimes, compaction policy,
and persistence.  Cross-boundary calls deliberately route through ``owner`` so
existing instance monkeypatch seams remain observable after wiring.

There is no runtime import of :mod:`kiro_crew.session`.  Patchable module
globals, provider types, process helpers, and policy constants are resolved by
call-time dependencies supplied by the facade.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from kiro_crew.metrics.sessions import (
    END_REASON_DESTROYED,
    END_REASON_DISCARDED,
    END_REASON_REMOVED,
    END_REASON_RESET,
    END_REASON_RETIRED,
    END_REASON_SHUTDOWN,
    END_REASON_UNCLAIMED,
    record_session_ended,
    record_sessions_ended,
)

CancelOutcome = Literal["acked", "timeout", "no_turn", "error"]
StopOutcome = Literal["soft", "hard", "idle"]
ProviderFactory = Callable[..., Any]


class _RecycleCallback(Protocol):
    async def __call__(self, key: str, *, reason: str) -> None: ...


class _SessionEntry(Protocol):
    provider: Any
    semaphore: asyncio.BoundedSemaphore
    first_turn: object
    retire_on_identity_change: bool
    prev_turn_cancelled: bool


class _SessionMapPort(Protocol):
    def clear_sid(self, key: str) -> None: ...

    def delete(self, key: str, *, reason: str | None = None) -> None: ...

    def set(
        self,
        key: str,
        sid: str,
        *,
        provider: str,
        cwd: str | None = None,
    ) -> None: ...

    async def aclose(self) -> None: ...


class _BackgroundRuntime(Protocol):
    def has_active_or_initializing_sessions(self) -> bool: ...

    async def kill(self, expected: bool = False) -> None: ...


class SessionLifecycleOwner(Protocol):
    """Facade state and operations consumed by the lifecycle service."""

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _sessions: MutableMapping[str, _SessionEntry]
    _lock: asyncio.Lock
    _closing: bool
    _start_sem: asyncio.Semaphore
    _starting_pids: set[int]

    _pool_fill_lock: asyncio.Lock
    _warm_pool: asyncio.Queue[tuple[Any, float]]
    _pool_size: int
    _pool_agent: str
    _pool_cwd: str
    _pool_started: bool
    _pool_health_task: asyncio.Task[Any] | None

    _compact_cooldown_until: MutableMapping[str, float]
    _compact_pending_verdict: MutableMapping[str, float]

    def _allocation_boundary(self) -> Any: ...

    _cleanup_task: asyncio.Task[Any] | None
    _background_tasks: set[asyncio.Task[Any]]

    _bg_runtime_lock: asyncio.Lock
    _bg_runtime: _BackgroundRuntime | None
    _draining_bg_runtimes: list[_BackgroundRuntime]
    _subagent_runtimes: MutableMapping[str, _BackgroundRuntime]
    _subagent_runtime_locks: MutableMapping[str, asyncio.Lock]

    _session_map: _SessionMapPort

    def _fold_key(self, key: str) -> str: ...

    def set_autocompact_pct(self, key: str, pct: float | None) -> None: ...

    def _is_continuable_key(self, key: str) -> bool: ...

    def clear_queue(self, key: str) -> None: ...

    def release(self, key: str) -> None: ...

    async def _discard_pool_provider(self, provider: Any, context: str) -> None: ...

    async def start_pool(self, *, blocking: bool = True) -> None: ...

    async def _retire_stale_backend_bg_runtime(self) -> None: ...

    async def release_subagent_runtime(self, parent_session_key: str) -> None: ...

    async def _retire_kiro_warm_pool(self) -> bool: ...

    async def _retire_kiro_subagent_runtimes(self) -> bool: ...

    async def _retire_kiro_bg_runtime(self) -> bool: ...

    async def _reap_drained_bg_runtimes_locked(self) -> None: ...

    async def drain_active_turns(self, timeout: float | None = None) -> int: ...

    async def reset(
        self,
        key: str,
        *,
        expect_session: _SessionEntry | None = None,
        skip_if_busy: bool = False,
        clear_conversation: bool = False,
    ) -> bool: ...

    async def _send_abort_for_session(self, key: str, session: Any) -> None: ...

    async def _eager_respawn(self, key: str) -> None: ...

    async def get_or_create(self, key: str, **kwargs: Any) -> tuple[Any, bool, bool]: ...


@dataclass(frozen=True, slots=True)
class SessionLifecycleConstants:
    """Patch-sensitive policy values resolved as one call-time snapshot."""

    max_pool: int
    max_concurrent_cold_starts: int
    background_key: str
    stateless_prefixes: tuple[str, ...]
    close_all_concurrency: int
    drain_active_turns_timeout_secs: float
    unbind_reason_session_destroyed: str
    first_turn_nothing_armed: object
    provider_label_claude: str


@dataclass(frozen=True, slots=True)
class SessionLifecycleDeps:
    """Leaf dependencies supplied by the ``SessionManager`` facade.

    The facade should pass forwarding callables, rather than captured module
    globals, for every patch-sensitive dependency.  That keeps patches such as
    ``kiro_crew.session.build_provider_factory`` and
    ``kiro_crew.session.schedule_abort`` effective after service construction.
    """

    logger: logging.Logger
    load_config: Callable[[], Any]
    build_provider_factory: Callable[[Any], ProviderFactory]
    default_project_dir: Callable[[], str]
    constants: Callable[[], SessionLifecycleConstants]
    get_unlink_session_queue: Callable[[], Callable[[Any], None]]
    get_child_process_helpers: Callable[
        [],
        tuple[Callable[..., Any], Callable[..., Any], Callable[..., Any]],
    ]
    get_subprocess_executor: Callable[[], Executor]
    get_platform_compat: Callable[[], Any]
    get_acp_provider_type: Callable[[], type[Any]]
    get_claude_code_provider_type: Callable[[], type[Any] | None]
    provider_label: Callable[[Any], str]
    provider_has_unfinished_turn: Callable[[Any], bool]
    provider_uses_kiro_identity_store: Callable[[Any], bool]
    get_audit_logger: Callable[[], Any]
    schedule_abort: Callable[..., None]
    monotonic: Callable[[], float]


@dataclass(slots=True)
class SessionLifecycleState:
    """Mutable state exclusively owned by :class:`SessionLifecycleService`."""

    identity_sweep_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    recycling: dict[str, _SessionEntry] = field(default_factory=dict)
    suppress_replay: set[str] = field(default_factory=set)
    origin_links: dict[str, Any] = field(default_factory=dict)
    on_recycled: _RecycleCallback | None = None


class SessionLifecycleService:
    """Coordinate provider retirement while preserving facade dispatch seams."""

    def __init__(
        self,
        owner: SessionLifecycleOwner,
        deps: SessionLifecycleDeps,
        state: SessionLifecycleState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    @property
    def _identity_sweep_lock(self) -> asyncio.Lock:
        return self.state.identity_sweep_lock

    @_identity_sweep_lock.setter
    def _identity_sweep_lock(self, lock: asyncio.Lock) -> None:
        self.state.identity_sweep_lock = lock

    @property
    def _recycling(self) -> dict[str, _SessionEntry]:
        return self.state.recycling

    @_recycling.setter
    def _recycling(self, recycling: dict[str, _SessionEntry]) -> None:
        self.state.recycling = recycling

    @property
    def _suppress_replay(self) -> set[str]:
        return self.state.suppress_replay

    @_suppress_replay.setter
    def _suppress_replay(self, suppress_replay: set[str]) -> None:
        self.state.suppress_replay = suppress_replay

    @property
    def _origin_links(self) -> dict[str, Any]:
        return self.state.origin_links

    @_origin_links.setter
    def _origin_links(self, origin_links: dict[str, Any]) -> None:
        self.state.origin_links = origin_links

    @property
    def _on_recycled(self) -> _RecycleCallback | None:
        return self.state.on_recycled

    @_on_recycled.setter
    def _on_recycled(self, callback: _RecycleCallback | None) -> None:
        self.state.on_recycled = callback

    async def refresh_defaults(self) -> None:
        """Adopt config changes that only affect new sessions."""
        owner = self._owner
        logger = self._deps.logger
        async with owner._pool_fill_lock:
            # Loaded OFF the event loop, and INSIDE the fill lock. Both halves
            # are load-bearing:
            #
            # Off-loop, because load_config() stats and reads the file, validates
            # it, and deep-copies the validated dict even on a cache hit. Every
            # caller here is a request handler (a settings write, a crew write, a
            # Slack command), so that work on the loop stalls every other
            # session's turn, and a stall past
            # dashboard.loop_stall_exit_after_secs takes the gateway down.
            #
            # Inside the lock, because going off-loop introduces an await point
            # between READING the config and INSTALLING it. Two overlapping
            # refreshes could then finish their loads out of order and let the
            # older one install last, pinning every new session to stale defaults
            # until the next restart -- silently, which is the failure mode this
            # method exists to prevent. Holding the lock across both makes
            # read-then-install atomic per refresh, and costs only that
            # serialization: the load still never touches the loop.
            cfg = await asyncio.to_thread(self._deps.load_config)
            async with owner._lock:
                owner._cfg = cfg
                owner._provider_factory = self._deps.build_provider_factory(cfg)
                while not owner._warm_pool.empty():
                    try:
                        provider, _ = owner._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await owner._discard_pool_provider(provider, "Default changed")
        # The health sweep returns early on an empty pool, so a drained pool
        # must be explicitly restarted with the new factory.
        owner._pool_started = False
        if owner._pool_health_task and not owner._pool_health_task.done():
            owner._pool_health_task.cancel()
            owner._pool_health_task = None
        await owner.start_pool(blocking=False)
        # Background runtimes capture the backend at spawn and must be retired
        # separately from registered providers after a backend switch.
        await owner._retire_stale_backend_bg_runtime()
        logger.info(
            "Session defaults refreshed: model=%s effort=%r (live sessions untouched)",
            cfg.agent.model,
            cfg.agent.reasoning_effort,
        )

    async def reload_provider_factory(self) -> None:
        """Reload the provider factory and tear down providers from the old one."""
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        cfg = self._deps.load_config()
        stale: list[tuple[str, Any]] = []
        async with owner._pool_fill_lock:
            async with owner._lock:
                owner._cfg = cfg
                owner._provider_factory = self._deps.build_provider_factory(cfg)
                owner._pool_size = min(constants.max_pool, max(0, cfg.session.pool_size))
                owner._pool_agent = cfg.session.pool_agent or getattr(
                    cfg.agent,
                    "default_agent",
                    "",
                )
                owner._pool_cwd = self._deps.default_project_dir()
                while not owner._warm_pool.empty():
                    try:
                        provider, _ = owner._warm_pool.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    await owner._discard_pool_provider(provider, "Stale pool drain")
                # Intentionally clear only the registry: the original reload
                # path does not rewrite session-map or compaction state here.
                stale = list(owner._sessions.items())
                owner._sessions.clear()
                # Same tick as the clear. This removal had no end record, so a
                # replacement under a reused key inherited the old start and
                # reported a lifetime spanning two sessions. Recorded as one set,
                # so the awaited unlink cannot be cancelled between two keys and
                # leave the rest behind as fabricated crashes.
                await record_sessions_ended(
                    [stale_key for stale_key, _ in stale], end_reason=END_REASON_RETIRED
                )
        # Shutdown remains outside both locks. Queue unlinking and companion
        # runtime release are intentionally not added to this historical path.
        for key, sess in stale:
            try:
                await sess.provider.shutdown()
            except Exception:
                logger.debug(
                    "Failed to shut down session %s on provider switch",
                    key,
                    exc_info=True,
                )
        owner._pool_started = False
        if owner._pool_health_task and not owner._pool_health_task.done():
            owner._pool_health_task.cancel()
            owner._pool_health_task = None
        await owner.start_pool(blocking=False)
        logger.info(
            "Provider factory reloaded: provider=%s, cleared %d sessions",
            cfg.agent.provider,
            len(stale),
        )

    async def reset(
        self,
        key: str,
        *,
        expect_session: _SessionEntry | None = None,
        skip_if_busy: bool = False,
        clear_conversation: bool = False,
    ) -> bool:
        """Kill a live session while preserving the exact reset semantics."""
        owner = self._owner
        logger = self._deps.logger
        key = owner._fold_key(key)
        async with owner._lock:
            current = owner._sessions.get(key)
            if expect_session is not None and current is not expect_session:
                return False
            if skip_if_busy and current is not None and current.semaphore.locked():
                return False
            session = owner._sessions.pop(key, None)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            if session is not None:
                # Same event-loop tick as the pop, for the reason the clear_sid
                # call below documents: the awaits further down let a racing cold
                # start register a SUCCESSOR under this key, and recording the
                # end after them would consume the successor's start instead of
                # this session's. The pop and the sample happen before this call's
                # own suspension point, so that ordering still holds; only the
                # crumb unlink is deferred to a worker.
                await record_session_ended(key, end_reason=END_REASON_RESET)
        if clear_conversation and session is not None:
            # The registry lock, not an absence of suspension points, is what makes
            # this safe: the end record above awaits, but it awaits while this
            # coroutine still holds ``owner._lock`` -- the lock a racing cold start
            # must take to register and map a SUCCESSOR -- so no successor SID can
            # be published across that suspension. Releasing an ``asyncio.Lock``
            # wakes its waiter without yielding, so control reaches this line before
            # any of them runs, and this clear cannot erase a successor's pointer.
            owner._session_map.clear_sid(key)
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            # Capture PID and child tree before shutdown clears them.
            client = getattr(session.provider, "_client", None)
            raw_pid = getattr(client, "_pid", None) if client else None
            if raw_pid is None:
                cc_proc = getattr(session.provider, "_proc", None)
                if cc_proc is not None and cc_proc.returncode is None:
                    raw_pid = cc_proc.pid
            if raw_pid is None:
                cc_proc = getattr(session.provider, "_active_proc", None)
                if cc_proc is not None and cc_proc.returncode is None:
                    raw_pid = cc_proc.pid
            pid = raw_pid if isinstance(raw_pid, int) else None
            raw_children = getattr(client, "_child_pids", None) if client else None
            child_pids: dict[Any, Any] = (
                dict(raw_children) if isinstance(raw_children, dict) else {}
            )
            capture_child_records, get_child_pids, kill_escaped_children = (
                self._deps.get_child_process_helpers()
            )

            if pid:
                # Snapshot descendants before shutdown; record capture retains
                # process start times so a recycled PID is never killed later.
                loop = asyncio.get_running_loop()
                fresh = await loop.run_in_executor(
                    self._deps.get_subprocess_executor(),
                    get_child_pids,
                    pid,
                )
                new_pids = [candidate for candidate in fresh if candidate not in child_pids]
                if new_pids:
                    child_pids.update(
                        await loop.run_in_executor(
                            self._deps.get_subprocess_executor(),
                            capture_child_records,
                            new_pids,
                        )
                    )
            await session.provider.shutdown()
            platform_compat = self._deps.get_platform_compat()
            if pid:
                if platform_compat.pid_exists(pid):
                    logger.warning("Reset %s: PID %d survived shutdown, force-killing", key, pid)
                    try:
                        await platform_compat.kill_process_tree_async(
                            pid,
                            platform_compat.SIGKILL,
                        )
                    except (ProcessLookupError, OSError):
                        try:
                            await platform_compat.kill_pid_async(pid, platform_compat.SIGKILL)
                        except (ProcessLookupError, OSError):
                            pass
                if child_pids:
                    try:
                        sweep_loop = asyncio.get_running_loop()
                        await sweep_loop.run_in_executor(
                            self._deps.get_subprocess_executor(),
                            kill_escaped_children,
                            child_pids,
                        )
                    except Exception:
                        logger.exception("Reset %s: child sweep failed", key)
            if key in owner._subagent_runtimes:
                try:
                    await owner.release_subagent_runtime(key)
                except Exception:
                    logger.debug(
                        "Reset %s: subagent runtime cleanup failed",
                        key,
                        exc_info=True,
                    )
            logger.debug("Reset session: %s (pid=%s)", key, pid)
        return session is not None

    def set_recycle_callback(self, cb: _RecycleCallback | None) -> None:
        """Register the watchdog recycle notification callback."""
        if self._on_recycled is not None and cb is not None:
            self._deps.logger.warning(
                "Recycle callback already registered; replacing existing handler"
            )
        self._on_recycled = cb

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None:
        """Invoke ``_on_recycled`` if registered, swallowing exceptions."""
        callback = self._on_recycled
        if callback is None:
            return
        try:
            await callback(key, reason=reason)
        except Exception:
            self._deps.logger.exception("Recycle callback failed for %s", key)

    async def remove(self, key: str) -> None:
        """Shut down a session while preserving its session-map entry."""
        owner = self._owner
        key = owner._fold_key(key)
        async with owner._lock:
            session = owner._sessions.pop(key, None)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            if session is not None:
                # Same tick as the pop: see reset for why recording after the
                # teardown awaits would consume a successor's start.
                await record_session_ended(key, end_reason=END_REASON_REMOVED)
        if session:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
            await session.provider.shutdown()
            # A companion subagent runtime lives outside the provider registry
            # and must be reaped separately from the parent provider.
            await owner.release_subagent_runtime(key)
            self._deps.logger.info("Removed session (map preserved): %s", key)

    async def retire_kiro_identity_sessions(self) -> tuple[list[str], bool]:
        """Retire idle Kiro-backed processes after an identity-store change.

        The start-permit barrier is acquired before the registry scan, making
        that scan authoritative over cold starts that began before the account
        change. Busy sessions are marked for retirement on their next turn and
        keep the sweep incomplete; the session map and pending compaction
        verdicts intentionally survive.
        """
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        doomed: list[tuple[str, Any]] = []
        skipped = False
        # One sweep at a time. Two peers draining permits one-by-one could each
        # hold a partial barrier forever, preventing both finally blocks from
        # restoring cold-start capacity.
        async with self._identity_sweep_lock:
            held = 0
            try:
                for _ in range(constants.max_concurrent_cold_starts):
                    await owner._start_sem.acquire()
                    held += 1
                async with owner._lock:
                    # Selection and unregistering share one lock hold so a
                    # chosen idle object cannot start a turn before its pop.
                    retired_keys: list[str] = []
                    for key in list(owner._sessions):
                        sess = owner._sessions[key]
                        if not self._deps.provider_uses_kiro_identity_store(sess.provider):
                            continue
                        if sess.semaphore.locked():
                            sess.retire_on_identity_change = True
                            skipped = True
                            continue
                        del owner._sessions[key]
                        owner._compact_cooldown_until.pop(key, None)
                        self._suppress_replay.discard(key)
                        self._origin_links.pop(key, None)
                        retired_keys.append(key)
                        # Do not clear _compact_pending_verdict: the identity
                        # recycle historically preserves that deferred verdict.
                        doomed.append((key, sess.provider))
                    # Same lock hold as the removals, not down in the shutdown
                    # loop below: that loop awaits, and a replacement session can
                    # register under a retired key while it does. Recorded as one
                    # set so the awaited unlink cannot be cancelled between two
                    # keys and leave the rest behind as fabricated crashes.
                    await record_sessions_ended(retired_keys, end_reason=END_REASON_RETIRED)
            finally:
                for _ in range(held):
                    owner._start_sem.release()

        retired: list[str] = []
        for key, provider in doomed:
            try:
                await provider.shutdown()
                await owner.release_subagent_runtime(key)
                retired.append(key)
            except Exception:
                logger.warning(
                    "Failed to retire session %s after an identity change",
                    key,
                    exc_info=True,
                )
                skipped = True

        # Warm-pool policy remains owned by the pool service; route through the
        # facade to retain direct manager monkeypatches and its fill-lock policy.
        if not await owner._retire_kiro_warm_pool():
            skipped = True
        if not await owner._retire_kiro_subagent_runtimes():
            skipped = True
        if not await owner._retire_kiro_bg_runtime():
            skipped = True
        if owner._starting_pids:
            # With every cold-start permit held above, residue here means a
            # producer bypassed the barrier; fail toward another sweep.
            skipped = True
        return retired, not skipped

    async def _retire_kiro_subagent_runtimes(self) -> bool:
        """Retire idle Kiro-backed companion runtimes."""
        owner = self._owner
        logger = self._deps.logger
        complete = True
        for parent_key in list(owner._subagent_runtimes):
            runtime = owner._subagent_runtimes.get(parent_key)
            if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
                continue
            if runtime.has_active_or_initializing_sessions():
                complete = False
                continue
            try:
                await owner.release_subagent_runtime(parent_key)
            except Exception:
                logger.warning(
                    "Failed to retire subagent runtime for %s after an identity change",
                    parent_key,
                    exc_info=True,
                )
                complete = False
        if any(lock.locked() for lock in owner._subagent_runtime_locks.values()):
            complete = False
        # This post-condition catches a runtime installed after the snapshot but
        # before its per-parent spawn lock was released.
        if any(
            runtime is not None and self._deps.provider_uses_kiro_identity_store(runtime)
            for runtime in list(owner._subagent_runtimes.values())
        ):
            complete = False
        return complete

    async def _retire_kiro_bg_runtime(self) -> bool:
        """Retire the idle Kiro-backed background runtime and drained holders."""
        owner = self._owner
        logger = self._deps.logger
        async with owner._bg_runtime_lock:
            await owner._reap_drained_bg_runtimes_locked()
            complete = not any(
                self._deps.provider_uses_kiro_identity_store(runtime)
                for runtime in owner._draining_bg_runtimes
            )
            runtime = owner._bg_runtime
            if runtime is None or not self._deps.provider_uses_kiro_identity_store(runtime):
                return complete
            if runtime.has_active_or_initializing_sessions():
                return False
            try:
                await runtime.kill(expected=True)  # deliberate logout teardown
            except Exception:
                logger.warning(
                    "Failed to retire the background runtime after an identity change",
                    exc_info=True,
                )
                return False
            # Clear only after kill succeeds; otherwise retain the live-process
            # reference for the next retirement attempt.
            owner._bg_runtime = None
            logger.info("Retired the background runtime started under the previous account")
            return complete

    async def remove_if_unclaimed(self, key: str) -> bool:
        """Remove a speculative session only while its first turn is unclaimed."""
        owner = self._owner
        constants = self._deps.constants()
        key = owner._fold_key(key)
        async with owner._lock:
            session = owner._sessions.get(key)
            if (
                session is None
                or session.first_turn is constants.first_turn_nothing_armed
                or session.semaphore.locked()
            ):
                return False
            del owner._sessions[key]
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            self._origin_links.pop(key, None)
            # Same tick as the removal: see reset.
            await record_session_ended(key, end_reason=END_REASON_UNCLAIMED)
        await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        await session.provider.shutdown()
        await owner.release_subagent_runtime(key)
        self._deps.logger.info(
            "Removed unclaimed speculative session (map preserved): %s",
            key,
        )
        return True

    async def destroy(self, key: str) -> None:
        """Permanently destroy a live session and its persistence entry."""
        owner = self._owner
        constants = self._deps.constants()
        key = owner._fold_key(key)
        async with owner._lock:
            session = owner._sessions.pop(key, None)
            owner._compact_cooldown_until.pop(key, None)
            self._suppress_replay.discard(key)
            owner._compact_pending_verdict.pop(key, None)
            # The per-session compaction-threshold override dies with the
            # session's permanent destruction (unlike reset/recycle, which it
            # deliberately survives): a later session recreated on this key is
            # a NEW conversation, and inheriting the deleted one's threshold
            # while the slot reports "following global" is silent divergence.
            owner.set_autocompact_pct(key, None)
            # The slot itself is gone -- the session-map entry goes with it -- so no
            # successor can arrive to pay the arm and it must not outlive them.
            owner._allocation_boundary().spend_retire_arm(key)
            # _origin_links deliberately survives destroy; existing callers
            # rely on the historical asymmetry with reset/remove.
            if session is not None:
                # Same tick as the pop: see reset.
                await record_session_ended(key, end_reason=END_REASON_DESTROYED)
        try:
            if session:
                await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
                await session.provider.shutdown()
            await owner.release_subagent_runtime(key)
        finally:
            owner._session_map.delete(
                key,
                reason=constants.unbind_reason_session_destroyed,
            )
            self._deps.logger.info("Destroyed session (map deleted): %s", key)

    async def discard_conversation(
        self, key: str, *, replay: bool = True, skip_if_busy: bool = False
    ) -> bool:
        """Drop only the native conversation while preserving channel linkage.

        Returns whether a session was actually torn down. False means
        ``skip_if_busy`` made it a no-op; nothing was changed, including the
        replay flag and the session map.

        ``skip_if_busy`` refuses the teardown when the session has a turn in
        flight, and is enforced HERE, atomically with the pop, for the same
        reason :meth:`reset` enforces its own: a caller that probes busy-ness
        first and calls this second leaves a window between the two in which a
        turn can be admitted — a channel message acquiring the session's
        semaphore, say — and the teardown then removes the provider from under a
        reply that has started. The probe is the SEMAPHORE rather than
        ``provider.has_active_turn()``, which is deliberately stricter: a turn
        that holds the semaphore but has not yet put a prompt in flight is
        invisible to ``has_active_turn`` and is exactly the case a caller-side
        pre-check cannot close.

        The sid clear runs in the SAME event-loop tick as the pop, with no await
        between them, so a cold start racing this teardown cannot have mapped a
        replacement sid for the key by the time it runs — the clear can never
        erase a successor's pointer. Clearing it after the shutdown awaits would
        do exactly that, since the shutdown is the window a concurrent channel
        turn needs to create and map a new session under the same key.
        """
        owner = self._owner
        key = owner._fold_key(key)
        async with owner._lock:
            current = owner._sessions.get(key)
            if skip_if_busy and current is not None and current.semaphore.locked():
                return False
            session = owner._sessions.pop(key, None)
            owner._compact_cooldown_until.pop(key, None)
            owner._compact_pending_verdict.pop(key, None)
            # Store replay suppression atomically with the pop. Origin-link
            # state intentionally survives this operation.
            if replay:
                self._suppress_replay.discard(key)
            else:
                self._suppress_replay.add(key)
            if session is not None:
                # Same lock hold as the pop, exactly like the clear_sid below.
                await record_session_ended(key, end_reason=END_REASON_DISCARDED)
        # The registry lock, not an absence of suspension points, is what keeps a
        # cold start racing this teardown from registering a replacement sid for the
        # key in between, so this clear cannot erase a SUCCESSOR's pointer. The end
        # record above awaits, but it does so while ``owner._lock`` is still held --
        # the lock that cold start must take to register and map a successor -- and
        # releasing an ``asyncio.Lock`` wakes its waiter without yielding, so control
        # reaches this line before any of them runs.
        # Deferring it past the shutdown awaits below is exactly that bug: the
        # provider shutdown is slow, a concurrent channel turn creates and maps a
        # new session under the same key while it runs, and a clear in the
        # ``finally`` then wipes the new session's sid. Mirrors ``reset``'s
        # ``clear_conversation``, which clears in this same position for this
        # same reason. Outside the lock rather than inside it because
        # ``clear_sid`` persists to disk, and the lock must not span blocking IO.
        owner._session_map.clear_sid(key)
        try:
            if session:
                await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
                await session.provider.shutdown()
            await owner.release_subagent_runtime(key)
        finally:
            self._deps.logger.info(
                "Discarded native conversation (sid cleared, map entry kept): %s",
                key,
            )
        return True

    async def drain_active_turns(self, timeout: float | None = None) -> int:
        """Bring unfinished native turns to a safe boundary before teardown."""
        owner = self._owner
        logger = self._deps.logger
        if timeout is None:
            timeout = self._deps.constants().drain_active_turns_timeout_secs
        if timeout <= 0:
            return 0

        async with owner._lock:
            providers = [session.provider for session in owner._sessions.values()]
        # Already-cancelled turns can report inactive before the native done ack;
        # unfinished is the signal that the native lock may still be held.
        unfinished = [
            provider for provider in providers if self._deps.provider_has_unfinished_turn(provider)
        ]
        if not unfinished:
            return 0

        logger.info(
            "Draining %d unfinished turn(s) to a safe boundary before teardown (<= %.1fs)",
            len(unfinished),
            timeout,
        )

        async def _drain_one(provider: Any) -> None:
            cancel_fn = getattr(provider, "cancel", None)
            if not callable(cancel_fn):
                return
            try:
                outcome = await cancel_fn(wait_ack_timeout=timeout)
            except Exception:
                logger.debug("drain_active_turns: cancel failed", exc_info=True)
                return
            if outcome == "no_turn" and self._deps.provider_has_unfinished_turn(provider):
                waiter = getattr(provider, "wait_turn_done", None)
                if callable(waiter):
                    try:
                        await waiter(timeout=timeout)
                    except asyncio.TimeoutError:
                        logger.debug("drain_active_turns: post-cancel wait_turn_done timed out")
                    except Exception:
                        logger.debug(
                            "drain_active_turns: wait_turn_done failed",
                            exc_info=True,
                        )

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_drain_one(provider) for provider in unfinished],
                    return_exceptions=True,
                ),
                # Slightly exceed each cancel budget so its own timeout resolves
                # before the gather is cancelled.
                timeout=timeout + 1.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "drain_active_turns: %d turn(s) did not reach a safe boundary within "
                "%.1fs — proceeding to kill (kiro-cli SIGTERM grace still applies)",
                len(unfinished),
                timeout,
            )
        return len(unfinished)

    async def close_all(self, drain_timeout: float | None = None) -> None:
        """Shut down every session after a bounded cooperative turn drain."""
        owner = self._owner
        logger = self._deps.logger
        constants = self._deps.constants()
        # Enter closing under the registry lock before taking the drain
        # snapshot. This prevents a new prompt or provider registration from
        # landing in the multi-second window after that snapshot.
        async with owner._lock:
            owner._closing = True

        try:
            await owner.drain_active_turns(timeout=drain_timeout)
        except Exception:
            # CancelledError is intentionally not caught: callers use an outer
            # wait_for deadline as the hard restart cap.
            logger.debug("close_all: drain_active_turns failed", exc_info=True)

        if owner._cleanup_task:
            owner._cleanup_task.cancel()

        # Pool-health and spawn tasks are registered in the same owned-task set.
        for task in list(owner._background_tasks):
            task.cancel()
        if owner._background_tasks:
            await asyncio.gather(*owner._background_tasks, return_exceptions=True)
            owner._background_tasks.clear()

        # Detach both background-runtime holders under their creation lock.
        # Killing the snapshot outside it prevents a wedged process teardown
        # from blocking every later observer of that boundary.
        async with owner._bg_runtime_lock:
            bg_doomed = [
                runtime
                for runtime in (owner._bg_runtime, *owner._draining_bg_runtimes)
                if runtime is not None
            ]
            owner._bg_runtime = None
            owner._draining_bg_runtimes = []
        for bg_runtime in bg_doomed:
            try:
                await bg_runtime.kill(expected=True)  # graceful shutdown
            except Exception:
                logger.debug("close_all: _bg runtime kill failed", exc_info=True)
        for key in list(owner._subagent_runtimes):
            try:
                await owner.release_subagent_runtime(key)
            except Exception:
                logger.debug(
                    "close_all: subagent runtime cleanup failed for %s",
                    key,
                    exc_info=True,
                )

        # Drain queued warm providers. This intentionally does not call the
        # public pool drain helper, whose informational log is not present on
        # the close_all path.
        pool_providers: list[Any] = []
        while not owner._warm_pool.empty():
            try:
                provider, _ = owner._warm_pool.get_nowait()
                pool_providers.append(provider)
            except asyncio.QueueEmpty:
                break

        async with owner._lock:
            acp_provider_type = self._deps.get_acp_provider_type()
            claude_code_provider_type = self._deps.get_claude_code_provider_type()
            for key, sess in owner._sessions.items():
                cwd_str = sess.provider.cwd
                if isinstance(sess.provider, acp_provider_type):
                    sid = sess.provider.client._session_id
                    if (
                        sid
                        and key != constants.background_key
                        and (
                            not any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
                            or owner._is_continuable_key(key)
                        )
                    ):
                        provider_label = self._deps.provider_label(sess.provider)
                        owner._session_map.set(
                            key,
                            sid,
                            provider=provider_label,
                            cwd=cwd_str,
                        )
                elif claude_code_provider_type is not None and isinstance(
                    sess.provider,
                    claude_code_provider_type,
                ):
                    sid = sess.provider.session_id
                    if (
                        sid
                        and key != constants.background_key
                        and (
                            not any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
                            or owner._is_continuable_key(key)
                        )
                    ):
                        owner._session_map.set(
                            key,
                            sid,
                            provider=constants.provider_label_claude,
                            cwd=cwd_str,
                        )

            # set() defers disk writes. aclose() is the durability point before
            # restart paths that terminate with os._exit.
            try:
                await owner._session_map.aclose()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.debug("close_all: session map flush failed", exc_info=True)

            sessions = dict(owner._sessions)
            owner._sessions.clear()
            owner._compact_cooldown_until.clear()
            self._suppress_replay.clear()
            owner._compact_pending_verdict.clear()
            closing_alloc = owner._allocation_boundary()
            closing_alloc.discard_all_retire_arms()
            # Same lock hold as the clear: the whole drained set is accounted for
            # in one call, so the awaited unlink cannot be cancelled between two
            # keys. Per-key awaits would leave every key after the cancellation
            # popped but unrecorded -- and shutdown is precisely the path a
            # cancellation reaches, so that would turn one orderly close into a
            # burst of sessions the next boot reports as crashed.
            await record_sessions_ended(sessions, end_reason=END_REASON_SHUTDOWN)

        # Provider shutdown can enqueue multiple blocking process-maintenance
        # jobs, so keep the original bounded fan-out.
        close_sem = asyncio.Semaphore(constants.close_all_concurrency)

        async def _close_one(provider: Any) -> None:
            async with close_sem:
                try:
                    await provider.shutdown()
                except Exception:
                    pass

        all_providers = [session.provider for session in sessions.values()] + pool_providers
        if not all_providers:
            return

        try:
            await asyncio.wait_for(
                asyncio.gather(
                    *[_close_one(provider) for provider in all_providers],
                    return_exceptions=True,
                ),
                timeout=5.0,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout closing %d sessions — orphan cleanup at next startup",
                len(all_providers),
            )
        logger.info("All sessions closed (active=%d)", len(sessions))

    async def cancel_current(
        self,
        key: str,
        *,
        wait_ack_timeout: float = 0.0,
    ) -> CancelOutcome:
        """Cancel the in-flight operation without destroying its session."""
        owner = self._owner
        key = owner._fold_key(key)
        session = owner._sessions.get(key)
        if not session:
            return "no_turn"
        outcome = await session.provider.cancel(wait_ack_timeout=wait_ack_timeout)
        self._deps.logger.info("Cancelled in-flight operation for %s: %s", key, outcome)
        return outcome

    async def stop_turn(
        self,
        key: str,
        *,
        force: bool = False,
        preserve_queue: bool = False,
        on_soft: Callable[[], Awaitable[None]] | None = None,
        on_hard: Callable[[], Awaitable[None]] | None = None,
    ) -> StopOutcome:
        """Cooperatively stop a turn, escalating to reset and eager respawn."""
        owner = self._owner
        logger = self._deps.logger
        key = owner._fold_key(key)
        session = owner._sessions.get(key)
        if not session:
            return "idle"

        if not preserve_queue:
            owner.clear_queue(key)
        budget: float = owner._cfg.agent.soft_stop_budget_secs
        t0 = self._deps.monotonic()

        if not force:
            outcome = await session.provider.cancel(wait_ack_timeout=budget)
            logger.debug("stop_turn: provider.cancel outcome=%r for %s", outcome, key)
            if outcome == "acked":
                elapsed = self._deps.monotonic() - t0
                logger.info(
                    "stop_turn outcome=soft-acked session=%s elapsed=%.2fs",
                    key,
                    elapsed,
                )
                # The native harness discards cancelled turns from its log; the
                # next prompt must therefore re-inject the cancelled context.
                session.prev_turn_cancelled = True
                if on_soft:
                    try:
                        await on_soft()
                    except Exception:
                        logger.warning("on_soft hook failed for %s", key, exc_info=True)
                return "soft"
            if outcome == "no_turn":
                logger.info("stop_turn outcome=idle session=%s (no active turn)", key)
                return "idle"
            logger.info(
                "stop_turn outcome=escalated-to-hard session=%s " "cancel_result=%r elapsed=%.2fs",
                key,
                outcome,
                self._deps.monotonic() - t0,
            )

        # Abort pooled gateway work before killing the owning provider.
        await owner._send_abort_for_session(key, session)
        await owner.reset(key)
        elapsed = self._deps.monotonic() - t0
        logger.info(
            "stop_turn outcome=hard-done session=%s elapsed=%.2fs",
            key,
            elapsed,
        )
        # Retain the task strongly until completion; the event loop alone keeps
        # only a weak reference.
        task = asyncio.create_task(owner._eager_respawn(key))
        owner._background_tasks.add(task)
        task.add_done_callback(owner._background_tasks.discard)
        if on_hard:
            try:
                await on_hard()
            except Exception:
                logger.warning("on_hard hook failed for %s", key, exc_info=True)
        return "hard"

    async def _send_abort_for_session(self, key: str, session: Any) -> None:
        """Best-effort gateway abort for a session's runtime process."""
        logger = self._deps.logger
        try:
            pid, socket_path = session.provider.runtime_info()

            if pid is None:
                client = getattr(session.provider, "_client", None)
                pid = getattr(client, "_pid", None) if client else None
            if socket_path is None:
                client = getattr(session.provider, "_client", None)
                socket_path = getattr(client, "_mcp_gateway_socket", None) if client else None

            if isinstance(pid, int) and pid > 1 and socket_path:
                # Audit at the decision point: downstream logging happens only
                # if the fire-and-forget gateway abort eventually succeeds.
                try:
                    self._deps.get_audit_logger().log_api_access(
                        caller="session",
                        operation="mcp-gateway.abort-initiated",
                        outcome="initiated",
                        source="session",
                        resources=f"pid={pid} session={key}",
                        error="reason=hard-stop",
                    )
                except Exception:  # pragma: no cover - audit cannot block kill
                    logger.debug("SEL audit for abort initiation failed", exc_info=True)
                self._deps.schedule_abort(
                    socket_path,
                    [pid],
                    reason=f"hard-stop session={key}",
                )
            else:
                logger.warning(
                    "abort-push skipped for %s: no runtime pid/socket resolved "
                    "(pid=%r socket=%r) — in-flight tool calls will not be cancelled",
                    key,
                    pid,
                    socket_path,
                )
        except Exception:
            logger.debug("_send_abort_for_session failed for %s", key, exc_info=True)

    async def _eager_respawn(self, key: str) -> None:
        """Respawn after hard kill and release its acquired turn semaphore."""
        try:
            await self._owner.get_or_create(key)
            self._owner.release(key)
        except Exception:
            self._deps.logger.debug("Eager respawn failed for %s", key, exc_info=True)

    async def drain_all_providers(self) -> list[Any]:
        """Pop every registered session and return its providers.

        Records an end per popped key even though the one current caller drains
        an already-empty registry (it calls ``reload_provider_factory`` first,
        which clears and records). An unrecorded mass pop here would not lose
        samples, it would manufacture crashes: every crumb left behind is
        reported as ``crashed`` at the next boot.
        """
        owner = self._owner
        providers: list[Any] = []
        popped: list[_SessionEntry] = []
        async with owner._lock:
            keys = list(owner._sessions.keys())
            ended: list[str] = []
            for key in keys:
                session = owner._sessions.pop(key, None)
                if session:
                    providers.append(session.provider)
                    popped.append(session)
                    ended.append(key)
            # One call for the whole set, so a cancellation cannot land between two
            # keys and leave the rest of them behind as fabricated crashes.
            await record_sessions_ended(ended, end_reason=END_REASON_SHUTDOWN)
        # Filesystem unlink stays outside the registry lock.
        for session in popped:
            await asyncio.to_thread(self._deps.get_unlink_session_queue(), session)
        return providers


__all__ = [
    "CancelOutcome",
    "SessionLifecycleConstants",
    "SessionLifecycleDeps",
    "SessionLifecycleService",
    "SessionLifecycleState",
    "StopOutcome",
]
