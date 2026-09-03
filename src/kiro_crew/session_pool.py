"""Warm-session pool state and lifecycle service.

The service owns only pre-spawned providers.  Session registration, cold-start
serialization, background-session creation, and task ownership remain on the
``SessionManager`` facade and are exposed through :class:`WarmPoolOwner`.

Dependencies whose defining namespace is intentionally patchable are injected
as callables.  The facade must supply forwarding callables which resolve those
names when invoked; capturing the current function object at construction time
would break ``kiro_crew.session.*`` patch seams.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    # Type-only: importing providers.base from this leaf at runtime enters the
    # providers -> acp package -> runtime -> session_pid -> providers cycle.
    from kiro_crew.providers.base import LLMProvider
else:
    LLMProvider = Any


ProviderFactory = Callable[..., LLMProvider]
KillProvider = Callable[[LLMProvider], None]


class _SessionMapPort(Protocol):
    def prune(self) -> int: ...


class WarmPoolOwner(Protocol):
    """Cross-boundary operations retained by the ``SessionManager`` facade.

    Calls between pool operations deliberately go through this owner instead of
    calling another service method directly.  Tests and integrations replace
    these manager methods on individual instances, so owner lookup at the call
    site is part of the compatibility contract.
    """

    _cfg: Any
    _provider_factory: ProviderFactory | None
    _session_map: _SessionMapPort
    _start_sem: asyncio.Semaphore
    _background_tasks: set[asyncio.Task[Any]]
    _starting_pids: set[int]

    async def _ensure_background(self) -> None: ...

    async def _fill_warm_pool(self) -> None: ...

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None: ...

    async def _discard_pool_provider(self, provider: LLMProvider, context: str) -> None: ...

    def _claim_from_pool(self, agent: str | None) -> tuple[LLMProvider, float] | None: ...

    def _schedule_replenish(self) -> None: ...

    async def _pool_health_loop(self) -> None: ...

    async def _sweep_warm_pool_once(self) -> None: ...


@dataclass(slots=True)
class WarmPoolState:
    """Mutable state exclusively owned by :class:`WarmSessionPool`."""

    pool_started: bool = False
    size: int = 0
    agent: str = ""
    ttl_secs: int = 0
    cwd: str = ""
    queue: asyncio.Queue[tuple[LLMProvider, float]] = field(default_factory=asyncio.Queue)
    fill_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    health_task: asyncio.Task[Any] | None = None
    sweep_pids: set[int] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class WarmPoolDeps:
    """Patch-aware dependencies for warm-pool policy and process teardown."""

    logger: logging.Logger
    default_project_dir: Callable[[], str]
    get_sync_kill_provider: Callable[[], KillProvider]
    get_subprocess_executor: Callable[[], Executor]
    get_pid_exists: Callable[[], Callable[[int], bool]]
    get_identity_predicate: Callable[[], Callable[[LLMProvider], bool]]
    get_discard_timeout: Callable[[], float]
    get_health_interval: Callable[[], float]
    get_recorder: Callable[[], Any]
    telemetry_channel_of: Callable[[str], str]
    max_pool: int
    pool_decisions: frozenset[str]


class WarmSessionPool:
    """Own and coordinate the pre-spawned provider pool.

    ``owner`` remains the authority for provider construction, cold-start
    permits, and background task ownership.  An optional state is accepted only
    by keyword for focused tests; production construction derives it from the
    owner's current config.
    """

    def __init__(
        self,
        owner: WarmPoolOwner,
        deps: WarmPoolDeps,
        *,
        state: WarmPoolState | None = None,
    ) -> None:
        self._owner = owner
        self._deps = deps
        self.state = state if state is not None else self._state_from_owner()

    def _state_from_owner(self) -> WarmPoolState:
        cfg = self._owner._cfg
        requested_size = cfg.session.pool_size
        size = min(self._deps.max_pool, max(0, requested_size))
        if requested_size > self._deps.max_pool:
            self._deps.logger.warning(
                "pool_size %d exceeds max %d, clamping",
                requested_size,
                self._deps.max_pool,
            )
        return WarmPoolState(
            size=size,
            agent=cfg.session.pool_agent or getattr(cfg.agent, "default_agent", ""),
            ttl_secs=max(0, cfg.session.pool_ttl_secs),
            cwd=self._deps.default_project_dir(),
        )

    # Compatibility-shaped state accessors make facade forwarding explicit and
    # preserve identity for mutable queue/lock/set objects (never return copies).
    @property
    def _pool_started(self) -> bool:
        return self.state.pool_started

    @_pool_started.setter
    def _pool_started(self, value: bool) -> None:
        self.state.pool_started = value

    @property
    def _pool_size(self) -> int:
        return self.state.size

    @_pool_size.setter
    def _pool_size(self, value: int) -> None:
        self.state.size = value

    @property
    def _pool_agent(self) -> str:
        return self.state.agent

    @_pool_agent.setter
    def _pool_agent(self, value: str) -> None:
        self.state.agent = value

    @property
    def _pool_ttl_secs(self) -> int:
        return self.state.ttl_secs

    @_pool_ttl_secs.setter
    def _pool_ttl_secs(self, value: int) -> None:
        self.state.ttl_secs = value

    @property
    def _pool_cwd(self) -> str:
        return self.state.cwd

    @_pool_cwd.setter
    def _pool_cwd(self, value: str) -> None:
        self.state.cwd = value

    @property
    def _warm_pool(self) -> asyncio.Queue[tuple[LLMProvider, float]]:
        return self.state.queue

    @_warm_pool.setter
    def _warm_pool(self, value: asyncio.Queue[tuple[LLMProvider, float]]) -> None:
        self.state.queue = value

    @property
    def _pool_fill_lock(self) -> asyncio.Lock:
        return self.state.fill_lock

    @_pool_fill_lock.setter
    def _pool_fill_lock(self, value: asyncio.Lock) -> None:
        self.state.fill_lock = value

    @property
    def _pool_health_task(self) -> asyncio.Task[Any] | None:
        return self.state.health_task

    @_pool_health_task.setter
    def _pool_health_task(self, value: asyncio.Task[Any] | None) -> None:
        self.state.health_task = value

    @property
    def _pool_sweep_pids(self) -> set[int]:
        return self.state.sweep_pids

    @_pool_sweep_pids.setter
    def _pool_sweep_pids(self, value: set[int]) -> None:
        self.state.sweep_pids = value

    async def start_pool(self, *, blocking: bool = True) -> None:
        """Start the background session and configured warm-pool workers."""
        if self._pool_started or not self._owner._provider_factory:
            return

        self._owner._session_map.prune()
        self._pool_started = True

        if not blocking:

            async def _start_bg_and_pool() -> None:
                await self._owner._ensure_background()
                await self._owner._fill_warm_pool()
                if self._pool_size:
                    self._pool_health_task = asyncio.create_task(self._owner._pool_health_loop())
                    self._owner._background_tasks.add(self._pool_health_task)
                    self._pool_health_task.add_done_callback(self._owner._background_tasks.discard)

            task = asyncio.create_task(_start_bg_and_pool())
            self._owner._background_tasks.add(task)
            task.add_done_callback(self._owner._background_tasks.discard)
            self._deps.logger.info("Background session starting (non-blocking)")
            return

        await self._owner._ensure_background()
        self._deps.logger.info("Background session ready")

        if self._pool_size:
            task = asyncio.create_task(self._owner._fill_warm_pool())
            self._owner._background_tasks.add(task)
            task.add_done_callback(self._owner._background_tasks.discard)
            self._pool_health_task = asyncio.create_task(self._owner._pool_health_loop())
            self._owner._background_tasks.add(self._pool_health_task)
            self._pool_health_task.add_done_callback(self._owner._background_tasks.discard)

    async def _fill_warm_pool(self) -> None:
        """Spawn providers up to the configured size and enqueue them."""
        if not self._pool_size or not self._owner._provider_factory:
            return
        async with self._pool_fill_lock:
            while self._warm_pool.qsize() < self._pool_size:
                provider: LLMProvider | None = None
                try:
                    provider = self._owner._provider_factory(
                        "",
                        agent=self._pool_agent or None,
                        cwd=self._pool_cwd or None,
                    )
                    async with self._owner._start_sem:
                        await provider.start()
                    self._warm_pool.put_nowait((provider, time.monotonic()))
                    provider = None
                    self._deps.logger.info(
                        "Warm pool: spawned process (pool=%d/%d agent=%s)",
                        self._warm_pool.qsize(),
                        self._pool_size,
                        self._pool_agent or "default",
                    )
                except Exception:
                    self._deps.logger.warning("Warm pool: failed to spawn process", exc_info=True)
                    break
                finally:
                    if provider is not None:
                        await self._owner._discard_pool_provider(provider, "Warm pool fill cleanup")

    def _dispatch_hard_kill(self, provider: LLMProvider) -> None:
        """Dispatch a blocking provider kill without blocking the event loop."""
        self.dispatch_hard_kill(
            provider,
            get_sync_kill_provider=self._deps.get_sync_kill_provider,
            get_subprocess_executor=self._deps.get_subprocess_executor,
        )

    @staticmethod
    def dispatch_hard_kill(
        provider: LLMProvider,
        *,
        get_sync_kill_provider: Callable[[], KillProvider],
        get_subprocess_executor: Callable[[], Executor],
    ) -> None:
        """Static implementation used by the facade's legacy static seam."""
        try:
            asyncio.get_running_loop().run_in_executor(
                get_subprocess_executor(),
                get_sync_kill_provider(),
                provider,
            )
        except RuntimeError:
            # Executor shutdown is possible during gateway teardown.  Running
            # the kill inline can block on waitpid/taskkill and stall watchdogs.
            threading.Thread(
                target=get_sync_kill_provider(),
                args=(provider,),
                daemon=True,
            ).start()

    async def _discard_pool_provider(self, provider: LLMProvider, context: str) -> None:
        """Bound, verify, and if necessary hard-kill a discarded provider."""
        client = getattr(provider, "_client", None) or getattr(provider, "client", None)
        pid = getattr(client, "_pid", None)
        try:
            await asyncio.wait_for(provider.shutdown(), timeout=self._deps.get_discard_timeout())
        except asyncio.CancelledError:
            # Awaiting an offload here would immediately re-raise cancellation;
            # synchronous kill blocks the loop, so dispatch before propagating.
            self._owner._dispatch_hard_kill(provider)
            raise
        except Exception:
            self._deps.logger.warning(
                "%s: provider shutdown failed — falling back to hard kill",
                context,
                exc_info=True,
            )
        except BaseException:
            self._owner._dispatch_hard_kill(provider)
            raise

        if isinstance(pid, int):
            still_alive = self._deps.get_pid_exists()(pid)
        else:
            try:
                still_alive = provider.is_process_alive()
            except Exception:
                still_alive = False
        if not still_alive:
            return

        self._deps.logger.warning(
            "%s: provider process (pid=%s) still alive after shutdown — hard-killing",
            context,
            pid,
        )
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._deps.get_subprocess_executor(),
                self._deps.get_sync_kill_provider(),
                provider,
            )
        except Exception:
            # Batch callers must continue to later providers even if one
            # executor submission or kill fails.
            self._deps.logger.warning(
                "%s: executor hard kill failed (pid=%s) — dispatching to a dedicated thread",
                context,
                pid,
                exc_info=True,
            )
            self._owner._dispatch_hard_kill(provider)

    def _record_pool_decision(self, decision: str, key: str) -> None:
        """Count one bounded-cardinality warm-pool decision."""
        try:
            self._deps.get_recorder().counter(
                "kirocrew.session.pool.decision",
                1,
                attrs={
                    "outcome": decision if decision in self._deps.pool_decisions else "other",
                    "channel": self._deps.telemetry_channel_of(key),
                },
            )
        except Exception:
            self._deps.logger.debug("pool decision metric emit failed", exc_info=True)

    def _claim_from_pool(self, agent: str | None) -> tuple[LLMProvider, float] | None:
        """Claim a provider only when the requested and pooled agents match."""
        if self._warm_pool.empty():
            return None
        requested = agent if agent else (self._pool_agent or "")
        pool_agent = self._pool_agent or ""
        if requested != pool_agent:
            return None
        try:
            return self._warm_pool.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def _drain_and_claim(self, agent: str | None) -> LLMProvider | None:
        """Claim the first live, non-expired provider available for ``agent``."""
        discarded = False
        claimed = self._owner._claim_from_pool(agent)
        while claimed is not None:
            provider, spawn_time = claimed
            age = time.monotonic() - spawn_time
            if self._pool_ttl_secs and age > self._pool_ttl_secs:
                try:
                    ttl_alive = provider.is_process_alive()
                except Exception:
                    ttl_alive = False
                ttl_log = self._deps.logger.info if ttl_alive else self._deps.logger.warning
                ttl_log(
                    "Warm pool: %.0fs old provider exceeds TTL %ds, discarding",
                    age,
                    self._pool_ttl_secs,
                )
                discarded = True
                await self._owner._discard_pool_provider(provider, "Warm pool discard")
                claimed = self._owner._claim_from_pool(agent)
                continue

            # Pool processes are expected to be idle, so the process-level
            # probe must be used instead of stale-activity responsiveness.
            if not provider.is_process_alive():
                self._deps.logger.warning(
                    "Warm pool: claimed provider is dead (returncode=%s), discarding",
                    provider.exit_code,
                )
                discarded = True
                await self._owner._discard_pool_provider(provider, "Warm pool discard")
                claimed = self._owner._claim_from_pool(agent)
                continue
            return provider

        if discarded:
            self._owner._schedule_replenish()
        return None

    def _schedule_replenish(self) -> None:
        """Schedule a refill task owned by the facade."""
        if not self._pool_size:
            return
        task = asyncio.create_task(self._owner._fill_warm_pool())
        self._owner._background_tasks.add(task)
        task.add_done_callback(self._owner._background_tasks.discard)

    def _pool_pids(self) -> set[int]:
        """Return pooled and temporarily swept PIDs without consuming entries."""
        pids: set[int] = set()
        items: list[tuple[LLMProvider, float]] = []
        while not self._warm_pool.empty():
            try:
                items.append(self._warm_pool.get_nowait())
            except asyncio.QueueEmpty:
                break
        for provider, spawn_time in items:
            pid = getattr(getattr(provider, "client", None), "_pid", None)
            if isinstance(pid, int):
                pids.add(pid)
            self._warm_pool.put_nowait((provider, spawn_time))
        pids.update(self._pool_sweep_pids)
        return pids

    def warm_providers(self) -> list[LLMProvider]:
        """Snapshot the queued warm providers without consuming any entry.

        Drain-and-requeue like :meth:`_pool_pids` (``asyncio.Queue`` exposes no
        peek), so a concurrent claim between the get and the put-back is the
        one race: it sees a momentarily shorter queue, never a lost provider.
        Read by the MCP hot-reload gate, which must treat a pooled process as a
        live session — it already completed its handshake and loaded its MCP
        servers, so it reconciles (or fails to) exactly like a claimed one.
        """
        items: list[tuple[LLMProvider, float]] = []
        while not self._warm_pool.empty():
            try:
                items.append(self._warm_pool.get_nowait())
            except asyncio.QueueEmpty:
                break
        for entry in items:
            self._warm_pool.put_nowait(entry)
        return [provider for provider, _spawn_time in items]

    def _in_flight_pids(self) -> set[int]:
        """Return a copy of the facade's start-to-registration PID guard."""
        return set(self._owner._starting_pids)

    async def _pool_health_loop(self) -> None:
        """Periodically discard dead/expired providers and refill the pool."""
        while True:
            await asyncio.sleep(self._deps.get_health_interval())
            try:
                await self._owner._sweep_warm_pool_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._deps.logger.exception("Pool health sweep failed")

    async def _sweep_warm_pool_once(self) -> None:
        """Perform one race-safe health sweep over the current queue snapshot."""
        if not self._pool_size:
            return
        qsize = self._warm_pool.qsize()
        if not qsize:
            return
        self._deps.logger.debug(
            "Pool health: sweeping %d providers (target=%d, ttl=%ds)",
            qsize,
            self._pool_size,
            self._pool_ttl_secs,
        )
        healthy: list[tuple[LLMProvider, float]] = []
        to_shutdown: list[LLMProvider] = []
        now = time.monotonic()
        try:
            for _ in range(qsize):
                try:
                    provider, spawn_time = self._warm_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                age = now - spawn_time
                pid = getattr(getattr(provider, "client", None), "_pid", None)
                if isinstance(pid, int):
                    self._pool_sweep_pids.add(pid)
                if self._pool_ttl_secs and age > self._pool_ttl_secs:
                    try:
                        ttl_alive = provider.is_process_alive()
                    except Exception:
                        ttl_alive = False
                    ttl_log = self._deps.logger.info if ttl_alive else self._deps.logger.warning
                    ttl_log(
                        "Pool health: %.0fs old provider (pid=%s) exceeds TTL %ds, discarding",
                        age,
                        pid,
                        self._pool_ttl_secs,
                    )
                    to_shutdown.append(provider)
                    continue
                try:
                    alive = provider.is_process_alive()
                except Exception:
                    alive = False
                if not alive:
                    self._deps.logger.warning(
                        "Pool health: dead provider (pid=%s, returncode=%s, age=%.0fs), discarding",
                        pid,
                        provider.exit_code,
                        age,
                    )
                    to_shutdown.append(provider)
                    continue
                self._deps.logger.debug("Pool health: provider pid=%s alive (age=%.0fs)", pid, age)
                healthy.append((provider, spawn_time))
        finally:
            # Survivors return before any await so a claimant never observes an
            # avoidable empty-queue window.  Sweep shields clear even if
            # cancellation interrupts a later provider shutdown.
            try:
                for entry in healthy:
                    self._warm_pool.put_nowait(entry)
                for provider in to_shutdown:
                    await self._owner._discard_pool_provider(provider, "Pool health discard")
            finally:
                self._pool_sweep_pids.clear()

        removed = qsize - len(healthy)
        if removed:
            self._deps.logger.info(
                "Pool health: removed %d dead/expired, %d healthy remain",
                removed,
                len(healthy),
            )
            self._owner._schedule_replenish()
        else:
            self._deps.logger.debug("Pool health: all %d providers healthy", len(healthy))

    async def drain_warm_pool(self) -> list[LLMProvider]:
        """Remove and return all queued providers without shutting them down."""
        drained: list[LLMProvider] = []
        while not self._warm_pool.empty():
            try:
                provider, _ = self._warm_pool.get_nowait()
                drained.append(provider)
            except asyncio.QueueEmpty:
                break
        if drained:
            self._deps.logger.info("Drained %d provider(s) from warm pool", len(drained))
        return drained

    async def _retire_kiro_warm_pool(self) -> bool:
        """Discard providers authenticated against the Kiro identity store."""
        keep: list[tuple[LLMProvider, float]] = []
        drop: list[LLMProvider] = []
        complete = True
        # Holding the fill lock across drain and shutdown prevents a fill that
        # authenticated before an identity change from enqueueing behind us.
        async with self._pool_fill_lock:
            for _ in range(self._warm_pool.qsize()):
                try:
                    provider, spawn_time = self._warm_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if self._deps.get_identity_predicate()(provider):
                    pid = getattr(getattr(provider, "client", None), "_pid", None)
                    if isinstance(pid, int):
                        self._pool_sweep_pids.add(pid)
                    drop.append(provider)
                else:
                    keep.append((provider, spawn_time))
            for entry in keep:
                self._warm_pool.put_nowait(entry)
            for provider in drop:
                try:
                    await provider.shutdown()
                except Exception:
                    self._deps.logger.warning(
                        "Failed to discard a pooled provider after an identity change",
                        exc_info=True,
                    )
                    complete = False
        if drop:
            self._deps.logger.info(
                "Discarded %d pooled provider(s) started under the previous account",
                len(drop),
            )
        return complete
