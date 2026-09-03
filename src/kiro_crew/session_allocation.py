"""Session registry, allocation, and claim coordination.

``SessionAllocationService`` owns the live-session registry and every lock or
lease needed to allocate from it.  Warm-pool inventory, compaction, teardown,
and cleanup policy remain separate owner-facade responsibilities.  This module
never imports :mod:`kiro_crew.session` at runtime; patchable compatibility seams
are supplied through :class:`AllocationDeps`.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast

from kiro_crew.metrics.sessions import (
    END_REASON_EVICTED,
    discard_session_start,
    record_session_ended,
    record_session_started,
)

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider
else:
    # Importing providers.base from this leaf enters providers -> acp.runtime ->
    # session_pid -> providers when the module is imported standalone.
    LLMProvider = Any


ProviderFactory = Callable[..., LLMProvider]


class SessionClosingError(RuntimeError):
    """A turn was requested after manager shutdown began."""


class SessionBusyError(RuntimeError):
    """A caller requested an immediate turn claim while the session was held."""


class SpeculativeResumeRefused(RuntimeError):
    """A speculative allocation may not consume an unrequested native resume."""


@dataclass(frozen=True, slots=True)
class AllocationConstants:
    """Behavioral constants supplied by the facade's patchable namespace."""

    max_concurrent_cold_starts: int
    won_race_max_retries: int
    circuit_breaker_threshold: int
    agent_model_cache_ttl: Callable[[], float]
    background_key: str
    heartbeat_key: str
    background_agent: str
    subagent_prefix: str
    stateless_prefixes: tuple[str, ...]
    provider_label_default: str
    provider_label_claude: str


@dataclass(frozen=True, slots=True)
class AllocationDeps:
    """Injected leaf dependencies and dynamic compatibility seams.

    Functions whose source names are monkeypatched in existing tests should be
    passed as forwarding lambdas.  The service deliberately holds no copy of
    ``SessionMap``: the owner's live instance remains persistence authority.
    """

    logger: logging.Logger
    constants: AllocationConstants
    canonical_key: Callable[[str], str]
    legacy_key: Callable[[str], str | None]
    provider_has_active_turn: Callable[[LLMProvider], bool]
    provider_effectively_alive: Callable[[LLMProvider], bool]
    is_acp_provider: Callable[[LLMProvider], bool]
    is_claude_provider: Callable[[LLMProvider], bool]
    is_claude_backend: Callable[[LLMProvider], bool]
    provider_label: Callable[[LLMProvider], str]
    detect_provider_switch: Callable[[Any, str, str], bool]
    session_factory: Callable[..., Any]
    first_turn_nothing_armed: object
    first_turn_fresh: object
    first_turn_resumed: object
    runtime_types: Callable[[], tuple[Callable[..., Any], type[BaseException]]]
    session_provider_type: Callable[[], Callable[[Any, Any], LLMProvider]]
    unlink_session_queue: Callable[[Any], None]
    unlink_queued_temp_paths: Callable[[dict[str, Any]], None]
    session_model: Callable[[Any, str | None], str | None]
    load_config: Callable[[], Any]
    resolve_crew_identity: Callable[[Any, str | None, str | None], str]
    load_watchdog_settings: Callable[[str], object]
    advertised_model_ids: Callable[[Any], list[str]]
    model_is_unusable: Callable[[str, list[str]], bool]
    to_provider_id: Callable[[str, str], str]
    to_acp_id: Callable[[str], str]
    inc_session_created: Callable[[], None]
    get_sel: Callable[[], Any]
    get_subprocess_executor: Callable[[], Executor]
    get_sync_kill_provider: Callable[[], Callable[[LLMProvider], None]]
    agents_dir_path: Callable[[], Path]
    read_agent_spec: Callable[..., dict[str, Any] | None]
    spec_model: Callable[[dict[str, Any]], str]
    agent_model_cache: Callable[[], dict[str, tuple[str, float, float]]]


@dataclass(slots=True)
class SessionRegistryState:
    """Mutable state exclusively owned by the allocation boundary."""

    sessions: dict[str, Any] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closing: bool = False
    start_sem: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(4))
    starting_pids: set[int] = field(default_factory=set)
    subagent_runtimes: dict[str, Any] = field(default_factory=dict)
    subagent_runtime_locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    continuable_keys: set[str] = field(default_factory=set)
    continuable_fallback: Callable[[str], bool] | None = None


class _AllocationOwner(Protocol):
    """Facade and cross-service surface consumed by allocation."""

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _session_map: Any
    _recycling: dict[str, Any]
    _pool_size: int
    _pool_agent: str
    _pool_cwd: str
    _warm_pool: asyncio.Queue[tuple[LLMProvider, float]]
    _background_tasks: set[asyncio.Task[Any]]
    _bg_runtime: Any | None

    def _fold_key(self, key: str) -> str: ...

    def get_provider(self, key: str) -> LLMProvider | None: ...

    async def get_subagent_runtime(
        self, parent_session_key: str, agent: str | None = None
    ) -> Any: ...

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
    ) -> Any: ...

    async def _reacquire_and_validate(
        self,
        key: str,
        session: Any,
        *,
        wait_if_busy: bool = True,
    ) -> bool: ...

    async def _evict_stale_session(self, key: str, session: Any) -> None: ...

    async def open_task_session(
        self, parent_session_key: str, session_key: str, **kwargs: Any
    ) -> Any: ...

    def _get_session_agent(self, session_key: str) -> str: ...

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]: ...

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None: ...

    def _record_pool_decision(self, decision: str, key: str) -> None: ...

    def _schedule_replenish(self) -> None: ...

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None: ...

    def _resolve_agent_model(self, agent: str) -> str: ...

    def _ensure_cleanup_task(self) -> None: ...

    async def get_or_create(self, key: str, **kwargs: Any) -> Any: ...

    async def reset(self, key: str, **kwargs: Any) -> bool: ...

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None: ...

    def mark_continuable(self, key: str) -> None: ...

    def _is_continuable_key(self, folded: str) -> bool: ...

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None: ...


def _collect_parent_runtime_kwargs(
    owner: _AllocationOwner,
    parent_session_key: str,
) -> dict[str, Any]:
    """Mirror the parent client's sandbox, gateway, env, and backend posture."""
    provider = owner.get_provider(parent_session_key)
    if provider is None:
        return {}
    client = getattr(provider, "client", None) or getattr(provider, "_client", None)
    if client is None:
        return {}
    kwargs: dict[str, Any] = {}
    for attribute, key in (
        ("_sandbox_mode", "sandbox_mode"),
        ("_extra_env", "extra_env"),
        ("_mcp_gateway_overlay", "mcp_gateway_overlay"),
        ("_mcp_gateway_settings_mcp_json", "mcp_gateway_settings_mcp_json"),
        ("_mcp_gateway_socket", "mcp_gateway_socket"),
        ("backend", "acp_backend"),
    ):
        value = getattr(client, attribute, None)
        if value is not None:
            kwargs[key] = value
    return kwargs


class SessionAllocationService:
    """Allocate providers and serialize claims while the manager stays facade."""

    def __init__(
        self,
        owner: _AllocationOwner,
        deps: AllocationDeps,
        *,
        state: SessionRegistryState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state

    # Compatibility properties preserve identity for maps, locks, and sets.
    @property
    def _sessions(self) -> dict[str, Any]:
        return self.state.sessions

    @_sessions.setter
    def _sessions(self, value: dict[str, Any]) -> None:
        self.state.sessions = value

    @property
    def _lock(self) -> asyncio.Lock:
        return self.state.lock

    @_lock.setter
    def _lock(self, value: asyncio.Lock) -> None:
        self.state.lock = value

    @property
    def _closing(self) -> bool:
        return self.state.closing

    @_closing.setter
    def _closing(self, value: bool) -> None:
        self.state.closing = value

    @property
    def _start_sem(self) -> asyncio.Semaphore:
        return self.state.start_sem

    @_start_sem.setter
    def _start_sem(self, value: asyncio.Semaphore) -> None:
        self.state.start_sem = value

    @property
    def _starting_pids(self) -> set[int]:
        return self.state.starting_pids

    @_starting_pids.setter
    def _starting_pids(self, value: set[int]) -> None:
        self.state.starting_pids = value

    @property
    def _subagent_runtimes(self) -> dict[str, Any]:
        return self.state.subagent_runtimes

    @_subagent_runtimes.setter
    def _subagent_runtimes(self, value: dict[str, Any]) -> None:
        self.state.subagent_runtimes = value

    @property
    def _subagent_runtime_locks(self) -> dict[str, asyncio.Lock]:
        return self.state.subagent_runtime_locks

    @_subagent_runtime_locks.setter
    def _subagent_runtime_locks(self, value: dict[str, asyncio.Lock]) -> None:
        self.state.subagent_runtime_locks = value

    @property
    def _continuable_keys(self) -> set[str]:
        return self.state.continuable_keys

    @_continuable_keys.setter
    def _continuable_keys(self, value: set[str]) -> None:
        self.state.continuable_keys = value

    @property
    def _continuable_fallback(self) -> Callable[[str], bool] | None:
        return self.state.continuable_fallback

    @_continuable_fallback.setter
    def _continuable_fallback(self, value: Callable[[str], bool] | None) -> None:
        self.state.continuable_fallback = value

    def _fold_key(self, key: str) -> str:
        """Resolve exact, canonical, then legacy-bare aliases onto a live key."""
        if key in self._sessions:
            return key
        canonical = self._deps.canonical_key(key)
        if canonical != key and canonical in self._sessions:
            return canonical
        bare = self._deps.legacy_key(key)
        if bare is not None and bare in self._sessions:
            return bare
        return key

    def has_session(self, key: str) -> bool:
        return self._owner._fold_key(key) in self._sessions

    def get_provider(self, key: str) -> LLMProvider | None:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.provider if session else None

    async def try_acquire(self, key: str) -> bool:
        """Acquire only an exact-key idle session; alias folding is intentional absent."""
        session = self._sessions.get(key)
        if session is None or session.semaphore.locked():
            return False
        # Idle Semaphore(1).acquire completes without suspending, keeping the
        # locked check and decrement atomic on the event loop.
        await session.semaphore.acquire()
        return True

    def active_providers(self) -> list[LLMProvider]:
        return [session.provider for session in self._sessions.values()]

    def any_active_turn(self) -> bool:
        return any(
            self._deps.provider_has_active_turn(session.provider)
            for session in self._sessions.values()
        )

    def get_pid(self, key: str) -> int | None:
        session = self._sessions.get(self._owner._fold_key(key))
        if not session:
            return None
        try:
            return session.provider.client._pid
        except AttributeError:
            return None

    async def get_subagent_runtime(self, parent_session_key: str, agent: str | None = None) -> Any:
        """Get or spawn the canonical shared companion runtime for a parent."""
        runtime_type, runtime_dead = self._deps.runtime_types()
        max_retries = 1
        attempt = 0
        selected_agent = agent
        while True:
            lock = self._subagent_runtime_locks.setdefault(parent_session_key, asyncio.Lock())
            async with lock:
                if self._subagent_runtime_locks.get(parent_session_key) is not lock:
                    # release_subagent_runtime removed the lock while we waited;
                    # retry under the newly-canonical lock without spending a
                    # process-spawn retry.
                    continue
                existing = self._subagent_runtimes.get(parent_session_key)
                if existing is not None and existing.is_alive():
                    return existing
                if existing is not None:
                    try:
                        await existing.kill()
                    except Exception:
                        self._deps.logger.debug(
                            "get_subagent_runtime: dead runtime kill failed for %s",
                            parent_session_key,
                            exc_info=True,
                        )
                selected_agent = (
                    selected_agent
                    or self._owner._get_session_agent(parent_session_key)
                    or "kirocrew"
                )
                kwargs = self._owner._parent_runtime_kwargs(parent_session_key)
                runtime = runtime_type(agent=selected_agent, **kwargs)
                try:
                    await runtime.spawn()
                except runtime_dead:
                    if attempt >= max_retries:
                        raise
                    attempt += 1
                    self._deps.logger.warning(
                        "Subagent runtime spawn failed for %s (attempt %d/%d), retrying",
                        parent_session_key,
                        attempt,
                        max_retries + 1,
                        exc_info=True,
                    )
                    continue
                self._subagent_runtimes[parent_session_key] = runtime
                return runtime

    async def release_subagent_runtime(self, parent_session_key: str) -> None:
        """Serialize release with spawn and kill the detached runtime off-map."""
        lock = self._subagent_runtime_locks.get(parent_session_key)
        if lock is not None:
            async with lock:
                runtime = self._subagent_runtimes.pop(parent_session_key, None)
                # A waiter on this removed lock re-checks canonical identity in
                # get_subagent_runtime and retries under the live lock.
                self._subagent_runtime_locks.pop(parent_session_key, None)
        else:
            runtime = self._subagent_runtimes.pop(parent_session_key, None)
        if runtime is not None:
            try:
                await runtime.kill(expected=True)
            except Exception:
                self._deps.logger.warning(
                    "Failed to kill subagent runtime for %s",
                    parent_session_key,
                    exc_info=True,
                )

    async def _get_or_bootstrap_run_runtime(
        self,
        parent_session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
    ) -> Any:
        """Adopt a configured bootstrap provider's runtime for a task run."""
        owner = self._owner
        if not owner._provider_factory:
            # Outside the per-key lock: get_subagent_runtime takes that lock and
            # asyncio.Lock is not reentrant.
            return await owner.get_subagent_runtime(parent_session_key, agent=agent)

        if parent_session_key not in self._subagent_runtime_locks:
            self._subagent_runtime_locks[parent_session_key] = asyncio.Lock()
        lock = self._subagent_runtime_locks[parent_session_key]
        async with lock:
            existing = self._subagent_runtimes.get(parent_session_key)
            if existing is not None and existing.is_alive():
                return existing
            provider = owner._provider_factory(parent_session_key, agent=agent, cwd=cwd)
            await provider.start()
            session_provider = getattr(provider, "_client", None)
            runtime = getattr(session_provider, "_runtime", None)
            if session_provider is not None and runtime is not None:
                try:
                    session_provider._owns_runtime = False
                except Exception:
                    self._deps.logger.debug("run runtime ownership transfer failed", exc_info=True)
                self._subagent_runtimes[parent_session_key] = runtime
                try:
                    handle = getattr(session_provider, "_handle", None)
                    session_id = getattr(handle, "session_id", None) or getattr(
                        handle, "_session_id", None
                    )
                    if session_id:
                        await runtime.terminate_session(session_id)
                except Exception:
                    self._deps.logger.debug(
                        "run runtime bootstrap-session terminate failed", exc_info=True
                    )
                return runtime
            try:
                await provider.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "run runtime bootstrap provider shutdown failed", exc_info=True
                )
        return await owner.get_subagent_runtime(parent_session_key, agent=agent)

    async def _reacquire_and_validate(
        self,
        key: str,
        session: Any,
        *,
        wait_if_busy: bool = True,
    ) -> bool:
        """Acquire with the global lock released, then validate exact identity."""
        if not wait_if_busy and session.semaphore.locked():
            raise SessionBusyError(key)
        # An idle Semaphore(1) acquires without suspension, so this is the
        # authoritative non-waiting claim boundary after the locked check.
        await session.semaphore.acquire()
        try:
            async with self._lock:
                still_valid = (
                    self._sessions.get(key) is session
                    and not session.retire_on_identity_change
                    and self._deps.provider_effectively_alive(session.provider)
                )
        except BaseException:
            # The held-semaphore contract was never returned to the caller.
            session.semaphore.release()
            raise
        if not still_valid:
            session.semaphore.release()
        return still_valid

    async def _evict_stale_session(self, key: str, session: Any) -> None:
        """Pop only the observed stale object and close it outside the lock."""
        dead: LLMProvider | None = None
        async with self._lock:
            if self._sessions.get(key) is session:
                del self._sessions[key]
                dead = session.provider
                # Same tick as the removal. Left unrecorded, the start crumb
                # survives and the next boot calls this a crash.
                await record_session_ended(key, end_reason=END_REASON_EVICTED)
        if dead is not None:
            await asyncio.to_thread(self._deps.unlink_session_queue, session)
            try:
                await dead.shutdown()
            except Exception:
                self._deps.logger.warning(
                    "Failed to shut down stale provider for %s", key, exc_info=True
                )

    async def open_task_session(
        self,
        parent_session_key: str,
        session_key: str,
        *,
        agent: str | None = None,
        cwd: str | None = None,
        approval_policy: str = "",
        _won_race_retries: int = 0,
    ) -> tuple[LLMProvider, bool, bool]:
        """Open a per-step session on the task run's shared runtime.

        A descriptor-bound macOS runtime cannot safely serve a later exact cwd
        through ACP's string-only session request. In that case the facade's
        normal dedicated-provider path binds a runtime at the requested cwd.
        """
        # Circular import: runtime imports the session provider path indirectly.
        from kiro_crew.acp.runtime import AcpWorkspaceBindingError

        owner = self._owner
        key = owner._fold_key(session_key)

        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None:
                existing.last_used = time.monotonic()
                if approval_policy:
                    existing.approval_policy = approval_policy
        if existing is not None:
            if await owner._reacquire_and_validate(key, existing):
                return existing.provider, False, False
            await owner._evict_stale_session(key, existing)

        runtime = await owner._get_or_bootstrap_run_runtime(
            parent_session_key, agent=agent, cwd=cwd
        )
        try:
            handle = await runtime.create_session(cwd=cwd or None, agent=agent or None)
        except AcpWorkspaceBindingError:
            return await owner.get_or_create(
                key,
                agent=agent,
                approval_policy=approval_policy,
                cwd=cwd,
            )
        provider = self._deps.session_provider_type()(handle, runtime)

        duplicate: LLMProvider | None = None
        won_race_session: Any | None = None
        async with self._lock:
            current = self._sessions.get(key)
            if current is not None:
                session = current
                session.last_used = time.monotonic()
                if approval_policy:
                    session.approval_policy = approval_policy
                duplicate = provider
            else:
                session = self._deps.session_factory(
                    provider=provider,
                    first_turn=self._deps.first_turn_fresh,
                    approval_policy=approval_policy,
                    agent=agent or "",
                )
                self._sessions[key] = session
                won_race_session = session
                try:
                    await record_session_started(key)
                except BaseException:
                    # This await is the only suspension point between registering
                    # the session and returning it. Cancelled here, the caller
                    # hard-kills the provider while the entry stays visible, so a
                    # claimant can be handed a session whose process is already
                    # dying -- and the crumb would outlive it into a false crash.
                    if self._sessions.get(key) is session:
                        del self._sessions[key]
                    await discard_session_start(key)
                    raise
        if duplicate is not None:
            try:
                await duplicate.shutdown()
            except Exception:
                self._deps.logger.debug(
                    "open_task_session: duplicate session teardown failed",
                    exc_info=True,
                )
            if await owner._reacquire_and_validate(key, session):
                return session.provider, False, False
            await owner._evict_stale_session(key, session)
            maximum = self._deps.constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"open_task_session({key!r}) exceeded {maximum} won-race "
                    "retries — session kept going stale between acquire and re-validate"
                )
            return await owner.open_task_session(
                parent_session_key,
                session_key,
                agent=agent,
                cwd=cwd,
                approval_policy=approval_policy,
                _won_race_retries=_won_race_retries + 1,
            )
        assert won_race_session is session
        await session.semaphore.acquire()
        return session.provider, True, False

    def _get_session_agent(self, session_key: str) -> str:
        session = self._sessions.get(session_key)
        if session is None:
            return ""
        return getattr(session, "agent", "") or ""

    def _parent_runtime_kwargs(self, parent_session_key: str) -> dict[str, Any]:
        return _collect_parent_runtime_kwargs(self._owner, parent_session_key)

    def is_session_sharing_eligible(self, parent_session_key: str) -> bool:
        # Exact-key lookup is current behavior; do not fold this seam here.
        session = self._sessions.get(parent_session_key)
        if session is None:
            return False
        return getattr(session.provider, "is_session_sharing_eligible", False)

    @staticmethod
    def _runtime_pid(runtime: Any) -> int | None:
        pid = getattr(runtime, "pid", None)
        return pid if isinstance(pid, int) and pid > 0 else None

    def runtime_pids(self) -> list[dict[str, object]]:
        """Return process-identity snapshots without performing OS sampling."""
        rows: list[dict[str, object]] = []
        for key, session in self._sessions.items():
            client = getattr(session.provider, "_client", None)
            if client is None:
                client = session.provider
            runtime = getattr(client, "_runtime", None)
            rows.append(
                {
                    "key": key,
                    "agent": session.agent,
                    "pid": self._runtime_pid(runtime),
                    "owns_runtime": bool(getattr(client, "_owns_runtime", True)),
                    "created_at": session.created_at,
                    "prompts": session.prompt_count,
                }
            )
        self._owner._append_companion_runtime_rows(rows)
        return rows

    def _append_companion_runtime_rows(self, rows: list[dict[str, object]]) -> None:
        """Append manager-owned background and subagent runtime process rows."""
        now_wall = time.time()
        now_monotonic = time.monotonic()

        def add(label: str, runtime: object, agent: str) -> None:
            try:
                if runtime is None or not runtime.is_alive():  # type: ignore[attr-defined]
                    return
                pid = self._runtime_pid(runtime)
                if pid is None:
                    return
                spawned = getattr(runtime, "_spawn_monotonic", None)
                created = (
                    now_wall - (now_monotonic - spawned)
                    if isinstance(spawned, (int, float))
                    else None
                )
                rows.append(
                    {
                        "key": label,
                        "agent": agent,
                        "pid": pid,
                        "owns_runtime": True,
                        "created_at": created,
                        "prompts": None,
                    }
                )
            except Exception:
                self._deps.logger.debug("runtime_pids: probe failed for %s", label, exc_info=True)

        add(
            "Background runtime",
            self._owner._bg_runtime,
            self._deps.constants.background_agent,
        )
        for parent_key, runtime in list(self._subagent_runtimes.items()):
            add(f"Subagent runtime ({parent_key})", runtime, "")

    def context_info(self) -> list[dict[str, object]]:
        """Return the dashboard-facing context snapshot for live sessions."""
        result: list[dict[str, object]] = []
        for key, session in self._sessions.items():
            provider = session.provider
            pct = provider.context_usage_pct()
            model = "unknown"
            agent = ""
            if self._deps.is_claude_provider(provider):
                model = provider._model or "auto"
                agent = provider._agent or ""
            elif self._deps.is_acp_provider(provider):
                model = provider.client._model or "auto"
                agent = provider.client._agent or ""
                if model == "auto" and agent and agent != "kirocrew":
                    model = self._owner._resolve_agent_model(agent)
                model = model or "auto"

            if key == self._deps.constants.background_key:
                name = "Background (titles, cron, heartbeat)"
            elif key.startswith("dashboard:"):
                name = f"Chat ({key.split(':', 1)[1]})"
            else:
                name = key

            window = 0
            if hasattr(provider, "context_window_tokens"):
                window = provider.context_window_tokens()
            result.append(
                {
                    "key": key,
                    "name": name,
                    "model": model,
                    "agent": agent,
                    "context_pct": round(pct, 1),
                    "context_window_tokens": window,
                    "prompts": session.prompt_count,
                }
            )
        return result

    def record_success(self, key: str) -> None:
        session = self._sessions.get(self._owner._fold_key(key))
        if session:
            session.consecutive_failures = 0

    async def record_failure(self, key: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        session.consecutive_failures += 1
        if session.consecutive_failures >= self._deps.constants.circuit_breaker_threshold:
            self._deps.logger.error(
                "Circuit breaker tripped for %s (%d consecutive failures) — resetting",
                key,
                session.consecutive_failures,
            )
            await self._owner.reset(key)
            return True
        return False

    def begin_turn(self, key: str) -> None:
        """Yield-free pre-dispatch closing gate for an already-issued lease."""
        if self._closing:
            raise SessionClosingError(
                "SessionManager is closing (gateway restart/shutdown in "
                "progress); refusing to start a turn"
            )

    def mark_continuable(self, key: str) -> None:
        self._continuable_keys.add(self._owner._fold_key(key))

    def unmark_continuable(self, key: str) -> None:
        self._continuable_keys.discard(self._owner._fold_key(key))

    def set_continuable_fallback(self, callback: Callable[[str], bool] | None) -> None:
        self._continuable_fallback = callback

    def _is_continuable_key(self, folded: str) -> bool:
        if folded in self._continuable_keys:
            return True
        fallback = self._continuable_fallback
        if fallback is None:
            return False
        try:
            if fallback(folded):
                self._continuable_keys.add(folded)
                return True
        except Exception:
            self._deps.logger.debug("continuable fallback failed for %s", folded, exc_info=True)
        return False

    def is_continuable(self, key: str) -> bool:
        return self._owner._is_continuable_key(self._owner._fold_key(key))

    # Persistence forwarding deliberately uses the owner's one SessionMap.
    def resumable_sid(self, key: str) -> str | None:
        return self._owner._session_map.get(self._owner._fold_key(key))

    def resumable_hint(self, key: str) -> bool:
        return self._owner._session_map.has_hint(self._owner._fold_key(key))

    def seed_conversation(
        self,
        key: str,
        sid: str,
        *,
        provider: str = "",
        cwd: str = "",
    ) -> None:
        if sid:
            self._owner._session_map.set(
                self._owner._fold_key(key),
                sid,
                provider=provider,
                cwd=cwd,
            )

    def forget_conversation(self, key: str) -> str | None:
        folded = self._owner._fold_key(key)
        sid = self._owner._session_map.get(folded)
        self._owner._session_map.delete(folded)
        self._continuable_keys.discard(folded)
        return sid

    def conversation_provider(self, key: str) -> str:
        return self._owner._session_map.get_provider(self._owner._fold_key(key))

    def release(self, key: str, *, cleanup: bool = False) -> None:
        """Release the current registry occupant's semaphore.

        This intentionally preserves the existing key-only lease identity: it
        does not repair the known stale-release window when a locked replacement
        occupies the same key.
        """
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            if (
                cleanup
                and key.startswith(self._deps.constants.subagent_prefix)
                and not self._owner._is_continuable_key(key)
            ):
                try:
                    session_id = session.provider.session_id
                    if session_id:
                        asyncio.ensure_future(
                            self._owner._safe_cleanup(session.provider, session_id)
                        )
                except Exception:
                    self._deps.logger.debug("Failed to get session_id for cleanup", exc_info=True)
            try:
                session.semaphore.release()
            except ValueError:
                self._deps.logger.warning(
                    "release(%s): session was replaced under us; dropping "
                    "stray semaphore release instead of over-releasing the "
                    "new occupant's",
                    key,
                )

    async def _safe_cleanup(self, provider: LLMProvider, session_id: str) -> None:
        try:
            await provider.cleanup_session(session_id)
            self._deps.logger.debug("Cleaned up session files for %s", session_id)
        except Exception:
            self._deps.logger.warning(
                "Failed to clean up session files for %s",
                session_id,
                exc_info=True,
            )

    def is_busy(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        return bool(session and session.semaphore.locked())

    def touch(self, key: str) -> bool:
        session = self._sessions.get(self._owner._fold_key(key))
        if session is None:
            return False
        session.last_used = time.monotonic()
        return True

    def enqueue(
        self,
        key: str,
        msg_ts: str,
        text: str,
        *,
        force: bool = False,
        **kwargs: object,
    ) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if force or session.semaphore.locked():
            session.queue.append((msg_ts, text, kwargs))
            return True
        return False

    def dequeue(self, key: str) -> tuple[str, str, dict[str, Any]] | None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return None
        while session.queue:
            msg_ts, text, kwargs = session.queue.popleft()
            if msg_ts not in session.cancelled:
                return msg_ts, text, kwargs
            session.cancelled.discard(msg_ts)
            self._deps.unlink_queued_temp_paths(kwargs)
        return None

    def cancel_queued(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        for index, (queued_ts, _, kwargs) in enumerate(session.queue):
            if queued_ts == msg_ts:
                self._deps.unlink_queued_temp_paths(kwargs)
                del session.queue[index]
                return True
        if session.semaphore.locked():
            session.cancelled.add(msg_ts)
        return False

    def is_cancelled(self, key: str, msg_ts: str) -> bool:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if not session:
            return False
        if msg_ts in session.cancelled:
            session.cancelled.discard(msg_ts)
            return True
        return False

    def clear_queue(self, key: str) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            for _, _, kwargs in session.queue:
                self._deps.unlink_queued_temp_paths(kwargs)
            session.queue.clear()
            session.cancelled.clear()

    async def is_provider_alive(self, key: str) -> bool | None:
        key = self._owner._fold_key(key)
        async with self._lock:
            session = self._sessions.get(key)
        if session is None:
            return None
        return session.provider.is_process_alive()

    def get_approval_policy(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.approval_policy if session else ""

    def get_agent(self, key: str) -> str:
        session = self._sessions.get(self._owner._fold_key(key))
        return session.agent if session else ""

    def set_approval_policy(self, key: str, policy: str) -> None:
        key = self._owner._fold_key(key)
        session = self._sessions.get(key)
        if session:
            previous = session.approval_policy
            session.approval_policy = policy
            if previous != policy:
                self._deps.get_sel().log_tool_invocation(
                    session_key=key,
                    source="session",
                    tool_name="set_approval_policy",
                    outcome=policy or "default",
                    metadata={"old_policy": previous, "new_policy": policy},
                )

    def _resolve_agent_model(self, agent: str) -> str:
        """Resolve an agent JSON model with directory-mtime and TTL invalidation."""
        agents_dir = self._deps.agents_dir_path()
        try:
            directory_mtime = agents_dir.stat().st_mtime
        except OSError:
            directory_mtime = 0.0
        now = time.monotonic()
        cache = self._deps.agent_model_cache()

        entry = cache.get(agent)
        if entry is not None:
            cached_model, cached_mtime, cached_at = entry
            if (
                cached_mtime == directory_mtime
                and now - cached_at < self._deps.constants.agent_model_cache_ttl()
            ):
                return cached_model

        model = "auto"
        try:
            for agent_file in agents_dir.glob("*.json"):
                data = self._deps.read_agent_spec(
                    agent_file,
                    operation="resolve_agent_model",
                    source="unknown",
                )
                if data is None:
                    continue
                if data.get("name") == agent or agent_file.stem == agent:
                    model = self._deps.spec_model(data)
                    break
        except Exception:
            pass
        cache[agent] = (model, directory_mtime, now)
        return model

    async def _crew_pins_effort(self, agent: str | None, crew_agent: object) -> bool:
        """True when the crew this session runs as pins its own reasoning effort.

        Read off the event loop: ``load_config`` parses (and deep-copies, even on
        a cache hit) the whole config, which is not work to do inline. Only the
        warm-pool decision calls this, so the cost lands once per cold start
        rather than once per turn, and never on a session that was already
        skipping the pool for a cheaper reason.

        Failure answers False -- the pre-field behaviour. An unreadable config
        must not stop a session from starting, and pooling it is only wrong for a
        crew that pins an effort, which is exactly what could not be read.
        """
        try:
            config = await asyncio.to_thread(self._deps.load_config)
            crew = crew_agent if isinstance(crew_agent, str) else None
            return bool(config.crew_pinned_effort(agent, crew))
        except Exception:
            self._deps.logger.warning(
                "Could not read the crew effort pin for agent=%r; pooling as before",
                agent,
                exc_info=True,
            )
            return False

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None:
        """Dispatch blocking provider teardown away from the event-loop thread."""
        kill = self._deps.get_sync_kill_provider()
        try:
            asyncio.get_running_loop().run_in_executor(
                self._deps.get_subprocess_executor(),
                kill,
                provider,
            )
        except RuntimeError:
            # During executor shutdown, a daemon thread is safer than running
            # waitpid/taskkill inline and wedging the event loop.
            threading.Thread(target=kill, args=(provider,), daemon=True).start()

    async def get_or_create(
        self,
        key: str,
        agent: str | None = None,
        channel_id: str | None = None,
        approval_policy: str = "",
        model: str | None = None,
        cwd: str | None = None,
        extra_env: dict[str, str] | None = None,
        speculative: bool = False,
        speculative_resume: bool = False,
        wait_if_busy: bool = True,
        _won_race_retries: int = 0,
        **extra_factory_kwargs: Any,
    ) -> tuple[LLMProvider, bool, bool]:
        """Claim a live session or cold-start one, returning its held lease.

        The returned tuple is ``(provider, is_new, resumed)``.  A successful
        return always owns the session semaphore and must be paired with
        ``release``.  First-turn observation is consumed only by the real
        claimant that actually wins that semaphore.
        """
        owner = self._owner
        constants = self._deps.constants
        key = owner._fold_key(key)
        stale_provider: LLMProvider | None = None
        stale_session: Any | None = None
        claimed: Any | None = None
        factory: ProviderFactory
        try:
            async with self._lock:
                if self._closing:
                    raise SessionClosingError(
                        "SessionManager is closing (gateway restart/shutdown in "
                        "progress); refusing to start or resume a turn"
                    )

                existing = self._sessions.get(key)
                recycling = existing is not None and owner._recycling.get(key) is existing
                if existing is not None and not recycling:
                    session = existing
                    alive = session.provider.is_process_alive()
                    if not alive:
                        if (
                            self._deps.is_claude_provider(session.provider)
                            and session.provider.connection_mode == "per_session"
                        ):
                            self._deps.logger.info(
                                "Session %s CC process dead — will reconnect on next stream()",
                                key,
                            )
                            alive = True
                        else:
                            self._deps.logger.warning(
                                "Session %s has dead provider — removing stale entry",
                                key,
                            )
                            stale_provider = session.provider
                            stale_session = session
                            del self._sessions[key]
                            # Same tick as the removal. Left unrecorded, the
                            # start crumb survives and the next boot calls this
                            # a crash rather than an eviction.
                            await record_session_ended(key, end_reason=END_REASON_EVICTED)
                    if alive:
                        session.last_used = time.monotonic()
                        if (
                            self._deps.is_claude_provider(session.provider)
                            and session.provider.session_id
                            and not owner._session_map.get(key)
                        ):
                            owner._session_map.set(
                                key,
                                session.provider.session_id,
                                provider=constants.provider_label_claude,
                                cwd=session.provider.cwd,
                            )
                        # The semaphore may be held for a full turn; claim it
                        # only after releasing the global registry lock.
                        claimed = session

                if claimed is None:
                    if not owner._provider_factory:
                        raise RuntimeError("No provider factory configured")
                    factory = owner._provider_factory
        finally:
            if stale_provider is not None:
                if stale_session is not None:
                    await asyncio.to_thread(self._deps.unlink_session_queue, stale_session)
                try:
                    await stale_provider.shutdown()
                except Exception:
                    self._deps.logger.warning(
                        "Failed to shut down stale provider for %s",
                        key,
                        exc_info=True,
                    )

        if claimed is not None:
            session = claimed
            if await owner._reacquire_and_validate(
                key,
                session,
                wait_if_busy=wait_if_busy,
            ):
                first_turn = session.first_turn
                if not speculative:
                    session.first_turn = self._deps.first_turn_nothing_armed
                return session.provider, first_turn.is_new, first_turn.resumed
            await owner._evict_stale_session(key, session)
            if not owner._provider_factory:
                raise RuntimeError("No provider factory configured")
            factory = owner._provider_factory

        # Model resolution reads agent JSON and therefore stays off the loop.
        if model is None:
            model = await asyncio.get_running_loop().run_in_executor(
                None,
                self._deps.session_model,
                owner._cfg,
                agent,
            )

        resume_sid: str | None = None
        is_stateless = (
            key in (constants.background_key, constants.heartbeat_key)
            or any(key.startswith(prefix) for prefix in constants.stateless_prefixes)
        ) and not owner._is_continuable_key(key)
        if not is_stateless:
            resume_sid = owner._session_map.get(key)
        if speculative and resume_sid and not speculative_resume:
            raise SpeculativeResumeRefused(key)

        self._deps.logger.info(
            "Pool decision: key=%s resume_sid=%s model=%s agent=%s "
            "pool_size=%d pool_qsize=%d cwd=%s pool_cwd=%s",
            key,
            resume_sid,
            model,
            agent,
            owner._pool_size,
            owner._warm_pool.qsize(),
            cwd,
            owner._pool_cwd,
        )
        provider_switched = False
        cwd_blocks_pool = bool(cwd and cwd != owner._pool_cwd)
        if not owner._pool_size:
            pool_decision = "disabled"
        elif resume_sid:
            pool_decision = "bypass_resume"
        elif is_stateless:
            pool_decision = "bypass_stateless"
        elif cwd_blocks_pool:
            pool_decision = "bypass_cwd"
        elif extra_factory_kwargs.get("reasoning_effort_override"):
            pool_decision = "bypass_effort"
        elif extra_env:
            pool_decision = "bypass_env"
        elif await self._crew_pins_effort(agent, extra_factory_kwargs.get("crew_agent")):
            # Same reason as bypass_effort above, for the effort a CREW pins
            # rather than one a caller passed: a pooled child was spawned under
            # whatever effort overlay was current when the pool filled, and the
            # claim path re-keys the model but never re-pushes effort — so a warm
            # hit would silently run this crew at the wrong depth. Cold-starting
            # is what makes the pin real.
            #
            # Last in the chain on purpose: it is the only arm that needs to read
            # config, so every cheaper reason to skip the pool is settled first
            # and a bypassing session never pays for the lookup.
            pool_decision = "bypass_effort"
        else:
            pool_decision = ""

        pooled = None if pool_decision else await owner._drain_and_claim(agent)
        if not pool_decision:
            pool_decision = "hit" if pooled is not None else "miss_empty"
        owner._record_pool_decision(pool_decision, key)
        if pooled is not None:
            provider = pooled
            try:
                if self._deps.is_acp_provider(provider):
                    claim_kwarg = extra_factory_kwargs.get("crew_agent")

                    def resolve_claim_watchdog() -> tuple[str, object]:
                        # Resolve from a fresh config off-loop.  AcpClient.rekey
                        # resets prompt cost/context state while rebinding the
                        # handle and watchdog to the claiming crew.
                        config = self._deps.load_config()
                        crew = self._deps.resolve_crew_identity(
                            config,
                            agent,
                            None if claim_kwarg is None else str(claim_kwarg),
                        )
                        return crew, self._deps.load_watchdog_settings(crew)

                    claim_crew, claim_watchdog = await asyncio.to_thread(resolve_claim_watchdog)
                    cast(Any, provider).client.rekey(
                        key,
                        channel_id,
                        crew_agent=claim_crew,
                        watchdog=claim_watchdog,
                    )
                    if model:
                        pool_model = (
                            owner._resolve_agent_model(owner._pool_agent)
                            if owner._pool_agent
                            else None
                        )
                        if self._deps.is_claude_backend(provider):
                            switch_model = self._deps.to_provider_id(model, "claude_code")
                            comparable_pool = (
                                self._deps.to_provider_id(pool_model, "claude_code")
                                if pool_model
                                else pool_model
                            )
                        else:
                            switch_model = self._deps.to_acp_id(model)
                            comparable_pool = (
                                self._deps.to_acp_id(pool_model) if pool_model else pool_model
                            )
                        if pool_model and switch_model != comparable_pool:
                            try:
                                advertised = self._deps.advertised_model_ids(
                                    provider.available_models()
                                )
                            except Exception:  # pragma: no cover - defensive
                                advertised = []
                            if advertised and self._deps.model_is_unusable(
                                switch_model, advertised
                            ):
                                self._deps.logger.warning(
                                    "Pool post-claim: model %s is not available to this "
                                    "account; leaving the claimed process on %s",
                                    switch_model,
                                    pool_model,
                                )
                            else:
                                await cast(Any, provider).client.set_model(switch_model)
                                self._deps.logger.info(
                                    "Pool post-claim: switched model to %s",
                                    switch_model,
                                )
                self._deps.logger.info(
                    "Claimed warm-pool process for %s (agent=%s)",
                    key,
                    agent or owner._pool_agent,
                )
                owner._schedule_replenish()
            except (asyncio.CancelledError, Exception):
                owner._dispatch_hard_kill(provider)
                raise
        else:
            effective_cwd = cwd
            if not effective_cwd and resume_sid:
                stored_cwd = owner._session_map.get_cwd(key)
                if stored_cwd and Path(stored_cwd).is_dir():
                    effective_cwd = stored_cwd
                    self._deps.logger.info("Resume CWD override for %s: %s", key, stored_cwd)
            provider = factory(
                key,
                agent=agent,
                channel_id=channel_id,
                model_override=model,
                cwd=effective_cwd,
                extra_env=extra_env,
                **extra_factory_kwargs,
            )
            provider_switched = False
            if resume_sid:
                is_claude_now = self._deps.is_claude_provider(
                    provider
                ) or self._deps.is_claude_backend(provider)
                current_provider = (
                    constants.provider_label_claude
                    if is_claude_now
                    else self._deps.provider_label(provider)
                )
                if self._deps.detect_provider_switch(owner._session_map, key, current_provider):
                    resume_sid = None
                    provider_switched = True
                    owner._session_map.clear_sid(key)

            if resume_sid:
                if self._deps.is_acp_provider(provider):
                    cast(Any, provider).client.set_resume_session_id(resume_sid)
                    self._deps.logger.info(
                        "Attempting session/load for %s (sid=%s)", key, resume_sid
                    )
                elif self._deps.is_claude_provider(provider):
                    cast(Any, provider).set_resume_session_id(resume_sid)
                    self._deps.logger.info("CC resume for %s (sid=%s)", key, resume_sid)
            async with self._start_sem:
                try:
                    await provider.start()
                except (asyncio.CancelledError, Exception):
                    owner._dispatch_hard_kill(provider)
                    raise

        # start() has published the PID, but registry ownership is not visible
        # until the lock section below. Shield this narrow orphan-sweep window.
        starting_pid = getattr(getattr(provider, "client", None), "_pid", None)
        if not isinstance(starting_pid, int):
            process = getattr(provider, "_proc", None)
            starting_pid = (
                process.pid if process is not None and process.returncode is None else None
            )
        if not isinstance(starting_pid, int):
            starting_pid = None
        if starting_pid is not None:
            self._starting_pids.add(starting_pid)

        won_race_session: Any | None = None
        duplicate_provider: LLMProvider | None = None
        try:
            resumed = False
            if self._deps.is_acp_provider(provider):
                resumed = cast(Any, provider).client.resumed
            if speculative and speculative_resume and not resumed:
                raise SpeculativeResumeRefused(key)

            async with self._lock:
                # start() can span the complete close_all snapshot, so closing
                # must be checked a second time immediately before registration.
                if self._closing:
                    raise SessionClosingError(
                        "SessionManager began closing during provider startup; "
                        "refusing to register a session behind the shutdown snapshot"
                    )

                existing = self._sessions.get(key)
                recycling = existing is not None and owner._recycling.get(key) is existing
                if existing is not None and not recycling:
                    session = existing
                    session.last_used = time.monotonic()
                    if approval_policy:
                        session.approval_policy = approval_policy
                    if agent:
                        session.agent = agent
                    won_race_session = session
                    duplicate_provider = provider
                else:
                    if not speculative:
                        first_turn = self._deps.first_turn_nothing_armed
                    elif resumed:
                        first_turn = self._deps.first_turn_resumed
                    else:
                        first_turn = self._deps.first_turn_fresh
                    session = self._deps.session_factory(
                        provider=provider,
                        first_turn=first_turn,
                        approval_policy=approval_policy,
                        agent=agent or "",
                    )
                    replay_needed = getattr(provider, "_history_replay_needed", False) is True
                    if provider_switched or replay_needed:
                        session.provider_switch_replay = True
                    if (
                        replay_needed
                        and self._deps.provider_label(provider) != constants.provider_label_default
                    ):
                        owner._session_map.clear_sid(key)
                    self._sessions[key] = session
                    try:
                        await record_session_started(key)
                    except BaseException:
                        # See open_task_session: a cancellation here would leave a
                        # registered session whose provider the caller is about to
                        # kill, plus a crumb the next boot reads as a crash.
                        if self._sessions.get(key) is session:
                            del self._sessions[key]
                        await discard_session_start(key)
                        raise
                    self._deps.logger.info(
                        "New session: %s agent=%s resumed=%s provider_switch=%s (total=%d)",
                        key,
                        agent or "kirocrew",
                        resumed,
                        provider_switched,
                        len(self._sessions),
                    )

                    provider_cwd = provider.cwd
                    if not is_stateless and self._deps.is_acp_provider(provider):
                        sid = cast(Any, provider).client._session_id
                        provider_label = self._deps.provider_label(provider)
                        if sid:
                            owner._session_map.set(
                                key,
                                sid,
                                provider=provider_label,
                                cwd=provider_cwd,
                            )
                    elif not is_stateless and self._deps.is_claude_provider(provider):
                        sid = provider.session_id
                        if sid:
                            owner._session_map.set(
                                key,
                                sid,
                                provider=constants.provider_label_claude,
                                cwd=provider_cwd,
                            )

                    # Cleanup owns its task slot; allocation only asks the
                    # facade to ensure it at the original registration point.
                    owner._ensure_cleanup_task()
                    # Fresh semaphore acquisition is synchronous and cannot
                    # wait, so doing it under _lock does not invert lock order.
                    await session.semaphore.acquire()
                    self._deps.inc_session_created()
                    result = (provider, True, resumed)
        except BaseException:
            owner._dispatch_hard_kill(provider)
            raise
        finally:
            if starting_pid is not None:
                self._starting_pids.discard(starting_pid)

        if won_race_session is not None:
            if duplicate_provider is not None:
                try:
                    await duplicate_provider.shutdown()
                except Exception:
                    self._deps.logger.warning(
                        "Failed to shut down duplicate provider for %s",
                        key,
                        exc_info=True,
                    )
            if await owner._reacquire_and_validate(
                key,
                won_race_session,
                wait_if_busy=wait_if_busy,
            ):
                first_turn = won_race_session.first_turn
                if not speculative:
                    won_race_session.first_turn = self._deps.first_turn_nothing_armed
                return (
                    won_race_session.provider,
                    first_turn.is_new,
                    first_turn.resumed,
                )
            maximum = constants.won_race_max_retries
            if _won_race_retries >= maximum:
                raise RuntimeError(
                    f"get_or_create({key!r}) exceeded {maximum} won-race retries — "
                    "session kept going stale between acquire and re-validate"
                )
            return await owner.get_or_create(
                key,
                agent=agent,
                channel_id=channel_id,
                approval_policy=approval_policy,
                model=model,
                cwd=cwd,
                extra_env=extra_env,
                speculative=speculative,
                speculative_resume=speculative_resume,
                wait_if_busy=wait_if_busy,
                _won_race_retries=_won_race_retries + 1,
                **extra_factory_kwargs,
            )

        return result
