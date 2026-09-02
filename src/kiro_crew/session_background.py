"""Background-session runtime boundary for :mod:`kiro_crew.session`.

The session manager has two background execution shapes:

* a persistent ``BACKGROUND_KEY`` provider stored in the owner's live-session
  registry, used by provider backends that cannot share an ``AcpRuntime``; and
* a multiplexed runtime that gives each caller an ephemeral session handle.

This module owns the latter runtime's slot, lock, and draining list while using
the owner facade for registry and lifecycle operations.  It deliberately does
not import ``kiro_crew.session`` at runtime: the facade injects module-level
compatibility seams through :class:`BackgroundRuntimeDeps`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping, MutableMapping, Set
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.metrics.sessions import (
    END_REASON_RECYCLED,
    discard_session_start,
    record_session_ended,
    record_session_started,
)

if TYPE_CHECKING:
    from kiro_crew.acp.types import AcpEvent
    from kiro_crew.providers.base import LLMProvider
else:
    # Runtime-importing providers.base from this leaf enters the
    # providers -> acp -> runtime -> session_pid -> providers cycle.
    LLMProvider = Any


class _BackgroundSessionEntry(Protocol):
    """The live-registry session shape used by the background boundary."""

    provider: LLMProvider
    semaphore: asyncio.BoundedSemaphore
    prompt_count: int

    def adopt_provider(self, provider: LLMProvider) -> None: ...


class _BackgroundRuntime(Protocol):
    """The subset of ``AcpRuntime`` used by the background boundary."""

    pid: int | None
    acp_backend: str
    _session_queues: Mapping[str, object]

    def is_alive(self) -> bool: ...

    def has_active_sessions(self) -> bool: ...

    def has_active_or_initializing_sessions(self) -> bool: ...

    def _stale_by_age(self) -> bool: ...

    async def _is_stale(self) -> str | None: ...

    async def spawn(self) -> None: ...

    async def kill(self, expected: bool = False) -> None: ...

    async def create_session(self, *, agent: str) -> object: ...


class _BackgroundOwner(Protocol):
    """Facade operations and shared state consumed by this service.

    Calls between extracted boundaries deliberately go through this protocol so
    existing ``patch.object(manager, ...)`` seams continue to observe the same
    dispatch points after the facade is wired to the service.
    """

    _cfg: Any
    _provider_factory: Callable[..., LLMProvider] | None
    _sessions: MutableMapping[str, _BackgroundSessionEntry]
    _lock: asyncio.Lock
    _closing: bool
    _start_sem: asyncio.Semaphore

    async def _ensure_background(self) -> None: ...

    def _configured_bg_backend_raw(self) -> str | None: ...

    def _configured_bg_backend(self) -> str: ...

    def _bg_backend_supports_runtime(self) -> bool: ...

    async def _reap_drained_bg_runtimes_locked(self) -> None: ...

    async def _displace_bg_runtime_locked(
        self,
        runtime: _BackgroundRuntime,
        cached_backend: str,
        configured_backend: str,
    ) -> None: ...

    async def _retire_stale_backend_bg_runtime(self) -> None: ...

    async def _provider_backed_bg_session(self) -> object: ...

    async def _reacquire_and_validate(
        self,
        key: str,
        session: _BackgroundSessionEntry,
    ) -> bool: ...


@dataclass(slots=True)
class BackgroundRuntimeState:
    """Mutable state exclusively owned by ``BackgroundSessionRuntime``."""

    runtime: _BackgroundRuntime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    draining: list[_BackgroundRuntime] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BackgroundRuntimeDeps:
    """Injected leaf dependencies and compatibility seams.

    The facade should supply callables that resolve patchable module globals at
    call time.  In particular, ``runtime_types`` stays lazy to avoid recreating
    the ``session -> acp.runtime -> acp.client -> session`` import cycle.
    """

    logger: logging.Logger
    background_key: str
    background_agent: str
    heartbeat_key: str
    runtime_agent: str
    acp_backend_kiro: str
    bg_recycle_pct: float
    bg_blind_recycle_prompts: int
    runtime_backends: Callable[[], Set[str]]
    context_pct_is_unknown: Callable[[LLMProvider], bool]
    runtime_types: Callable[
        [],
        tuple[Callable[..., _BackgroundRuntime], type[BaseException]],
    ]
    session_factory: Callable[..., _BackgroundSessionEntry]
    first_turn_nothing_armed: object
    provider_bg_session_factory: Callable[[_BackgroundSessionEntry], object]
    session_closing_error: Callable[[str], BaseException]


class _ProviderBgSession:
    """``AcpSessionHandle``-compatible handle over the shared background entry.

    All callers share one provider session, so turns are serialized by the
    existing per-session semaphore.  The adapter is plumbing only: event parsing
    remains the provider's responsibility.
    """

    def __init__(self, sess: _BackgroundSessionEntry) -> None:
        self._sess = sess
        self._sem_held = False

    @property
    def session_id(self) -> str:
        try:
            return self._sess.provider.session_id
        except Exception:
            return ""

    def _release(self) -> None:
        if self._sem_held:
            self._sem_held = False
            self._sess.semaphore.release()

    async def prompt(
        self,
        message: str,
        timeout: float | None = None,
    ) -> AsyncIterator[AcpEvent]:
        # timeout is accepted for AcpSessionHandle signature parity; the
        # underlying provider/client manages its own stale-turn watchdog.
        await self._sess.semaphore.acquire()
        self._sem_held = True
        try:
            async for event in self._sess.provider.stream(message):
                yield event
        finally:
            self._release()

    async def reject_tool(self, request_id: str | int) -> None:
        await self._sess.provider.reject_tool(request_id)

    async def destroy(self) -> None:
        # The background _Session is persistent and shared — never tear it down
        # here. Just release the turn semaphore deterministically so the next
        # caller isn't blocked on generator finalization.
        self._release()


class BackgroundSessionRuntime:
    """Composition service for persistent and multiplexed background sessions."""

    def __init__(self, owner: _BackgroundOwner, deps: BackgroundRuntimeDeps) -> None:
        self._owner = owner
        self._deps = deps
        self.state = BackgroundRuntimeState()

    # Compatibility-shaped properties keep the migrated method bodies close to
    # their source while making BackgroundRuntimeState the sole state owner.
    @property
    def _bg_runtime(self) -> _BackgroundRuntime | None:
        return self.state.runtime

    @_bg_runtime.setter
    def _bg_runtime(self, runtime: _BackgroundRuntime | None) -> None:
        self.state.runtime = runtime

    @property
    def _bg_runtime_lock(self) -> asyncio.Lock:
        return self.state.lock

    @_bg_runtime_lock.setter
    def _bg_runtime_lock(self, lock: asyncio.Lock) -> None:
        self.state.lock = lock

    @property
    def _draining_bg_runtimes(self) -> list[_BackgroundRuntime]:
        return self.state.draining

    @_draining_bg_runtimes.setter
    def _draining_bg_runtimes(self, runtimes: list[_BackgroundRuntime]) -> None:
        self.state.draining = runtimes

    async def _ensure_background(self) -> None:
        """Create the persistent background session if it doesn't exist."""
        background_key = self._deps.background_key
        background_agent = self._deps.background_agent
        logger = self._deps.logger
        async with self._owner._lock:
            if self._owner._closing or background_key in self._owner._sessions:
                return
        # Create outside lock
        if not self._owner._provider_factory:
            return
        try:
            provider = self._owner._provider_factory(background_key, agent=background_agent)
            async with self._owner._start_sem:
                await provider.start()
        except Exception:
            logger.warning("Failed to create background session", exc_info=True)
            return
        async with self._owner._lock:
            # _closing is rechecked because the start above spans the window
            # in which close_all takes its session snapshot: registering now
            # would leak this provider past graceful shutdown.
            if not self._owner._closing and background_key not in self._owner._sessions:
                sess = self._deps.session_factory(
                    provider=provider,
                    first_turn=self._deps.first_turn_nothing_armed,
                    agent=background_agent,
                )
                self._owner._sessions[background_key] = sess
                try:
                    await record_session_started(background_key)
                except BaseException:
                    # Cancelled between registering and recording: roll the entry
                    # back so no claimant sees a session the caller is tearing
                    # down, and consume the crumb so it cannot become a false
                    # crash at the next boot.
                    if self._owner._sessions.get(background_key) is sess:
                        del self._owner._sessions[background_key]
                    await discard_session_start(background_key)
                    raise
                logger.info("Background session created")
                return
        # Racing registration lost, or shutdown began while we were starting:
        # tear the fresh provider down instead of registering it.
        await provider.shutdown()

    def _configured_bg_backend_raw(self) -> str | None:
        """Return the configured background backend, or ``None`` if unreadable."""
        logger = self._deps.logger
        try:
            backend = getattr(self._owner._cfg.agent, "acp_backend", self._deps.acp_backend_kiro)
        except Exception:
            logger.warning(
                "agent.acp_backend is unreadable; treating the _bg backend as unknown",
                exc_info=True,
            )
            return None
        return backend if isinstance(backend, str) else None

    def _configured_bg_backend(self) -> str:
        """Return the backend background runtimes must spawn under."""
        backend = self._owner._configured_bg_backend_raw()
        return backend if backend is not None else self._deps.acp_backend_kiro

    def _bg_backend_supports_runtime(self) -> bool:
        """Whether the configured backend can use the multiplexed runtime."""
        return self._owner._configured_bg_backend() in self._deps.runtime_backends()

    async def _reap_drained_bg_runtimes_locked(self) -> None:
        """Kill and drop parked runtimes whose last live handle has drained.

        Caller MUST hold ``_bg_runtime_lock``. A runtime that is still busy
        stays parked for the next pass; a failed kill also stays parked so the
        process is retried rather than orphaned. ``kill()`` is called even on an
        already-dead runtime so it can release PID bookkeeping.
        """
        logger = self._deps.logger
        remaining: list[_BackgroundRuntime] = []
        for runtime in self._draining_bg_runtimes:
            try:
                busy = runtime.is_alive() and runtime.has_active_or_initializing_sessions()
            except Exception:
                # Fail toward preserving work, not toward recycling — a probe
                # that cannot answer must not kill a runtime whose handles may
                # be live. The runtime stays parked and is probed again next
                # pass.
                busy = True
            if busy:
                remaining.append(runtime)
                continue
            try:
                await runtime.kill(expected=True)  # drained backend-switch teardown
                logger.info("Reaped a drained _bg runtime spawned under the previous backend")
            except Exception:
                logger.warning("Failed to reap a drained _bg runtime; will retry", exc_info=True)
                remaining.append(runtime)
        self._draining_bg_runtimes = remaining

    async def _displace_bg_runtime_locked(
        self,
        runtime: _BackgroundRuntime,
        cached_backend: str,
        configured_backend: str,
    ) -> None:
        """Displace a cached runtime after a configured backend switch.

        Caller MUST hold ``_bg_runtime_lock``. An idle runtime is killed; a busy
        one, or one whose kill failed, is parked until its handles drain.
        """
        logger = self._deps.logger
        try:
            busy = runtime.has_active_or_initializing_sessions()
        except Exception:
            busy = True
        if busy:
            logger.info(
                "Parking the _bg runtime (PID %s, backend %r) to drain after a "
                "switch to backend %r",
                runtime.pid,
                cached_backend,
                configured_backend,
            )
            self._draining_bg_runtimes.append(runtime)
            if len(self._draining_bg_runtimes) > 1:
                # Each entry is a live agent process shielded from the orphan
                # sweep; more than one parked at a time means backend flapping
                # is outpacing the drain, which should be visible, not silent.
                logger.warning(
                    "%d _bg runtimes are parked draining after backend switches",
                    len(self._draining_bg_runtimes),
                )
        else:
            logger.info(
                "Recycling the _bg runtime (PID %s) spawned under backend %r; "
                "configured backend is now %r",
                runtime.pid,
                cached_backend,
                configured_backend,
            )
            try:
                await runtime.kill(expected=True)  # deliberate backend-switch teardown
            except Exception:
                logger.warning(
                    "Backend-switch kill failed; parking the runtime for the reaper",
                    exc_info=True,
                )
                self._draining_bg_runtimes.append(runtime)
        self._bg_runtime = None

    async def _retire_stale_backend_bg_runtime(self) -> None:
        """Retire a cached runtime spawned under a different backend."""
        async with self._bg_runtime_lock:
            # close_all's locked detach may already have run; parking into the
            # cleared list after it would strand a shielded process until the
            # next-startup orphan reaper. One gate here covers every park this
            # helper can do.
            if self._owner._closing:
                return
            await self._owner._reap_drained_bg_runtimes_locked()
            runtime = self._bg_runtime
            if runtime is None:
                return
            cached_backend = getattr(runtime, "acp_backend", None)
            if not isinstance(cached_backend, str):
                return
            configured = self._owner._configured_bg_backend_raw()
            if configured is None or cached_backend == configured:
                return
            await self._owner._displace_bg_runtime_locked(runtime, cached_backend, configured)

    async def _provider_backed_bg_session(self) -> object:
        """Return the shared provider-backed background-session adapter."""
        if self._owner._closing:
            # Typed for the same reason as get_bg_session's gates: a shutdown
            # racing this path must classify as shutdown, not as the missing-
            # session error below.
            raise self._deps.session_closing_error(
                "session manager is closing; no background session"
            )
        await self._owner._ensure_background()
        sess = self._owner._sessions.get(self._deps.background_key)
        if sess is None:
            raise RuntimeError("background session unavailable for non-kiro _bg provider")
        return self._deps.provider_bg_session_factory(sess)

    async def get_bg_session(self) -> object:
        """Acquire a background handle, dispatching by configured backend.

        Runtime-capable backends receive an ephemeral handle on the shared
        multiplexed runtime. Other backends receive a provider-backed adapter
        over the persistent background registry entry. The caller must destroy
        the returned handle in a ``finally`` block.
        """
        logger = self._deps.logger
        if self._owner._closing:
            raise self._deps.session_closing_error(
                "session manager is closing; no background session"
            )

        if not self._owner._bg_backend_supports_runtime():
            # A cached runtime spawned under a previous runtime-capable backend
            # is unreachable from the branch below, so finish any deferred
            # retirement before serving the provider path.
            await self._owner._retire_stale_backend_bg_runtime()
            return await self._owner._provider_backed_bg_session()

        # Supplied lazily by the facade to preserve the existing import cycle.
        AcpRuntime, AcpRuntimeDead = self._deps.runtime_types()

        max_retries = 1
        for attempt in range(max_retries + 1):
            async with self._bg_runtime_lock:
                # Paired with close_all()'s locked detach: once _closing is
                # set, spawning or parking here would install a runtime the
                # shutdown sweep has already run past.
                if self._owner._closing:
                    raise self._deps.session_closing_error(
                        "session manager is closing; no background session"
                    )
                await self._owner._reap_drained_bg_runtimes_locked()
                runtime = self._bg_runtime
                configured_backend_raw = self._owner._configured_bg_backend_raw()
                configured_backend = (
                    configured_backend_raw
                    if configured_backend_raw is not None
                    else self._deps.acp_backend_kiro
                )
                runtime_capable = configured_backend in self._deps.runtime_backends()
                if runtime_capable and runtime is not None and runtime.is_alive():
                    cached_backend = getattr(runtime, "acp_backend", None)
                    if (
                        configured_backend_raw is not None
                        and isinstance(cached_backend, str)
                        and cached_backend != configured_backend_raw
                    ):
                        await self._owner._displace_bg_runtime_locked(
                            runtime,
                            cached_backend,
                            configured_backend_raw,
                        )
                    elif not runtime.has_active_sessions():
                        reason = await runtime._is_stale()
                        if reason:
                            logger.info(
                                "get_bg_session: recycling stale _bg runtime "
                                "(PID %s, reason=%s)",
                                runtime.pid,
                                reason,
                            )
                            await runtime.kill(expected=True)  # deliberate staleness recycle
                            self._bg_runtime = None
                    elif runtime._stale_by_age():
                        logger.info(
                            "get_bg_session: _bg runtime (PID %s) stale by age "
                            "but has %d active session(s); deferring recycle",
                            runtime.pid,
                            len(runtime._session_queues),
                        )

                if runtime_capable and (
                    self._bg_runtime is None or not self._bg_runtime.is_alive()
                ):
                    # Reap the dead runtime before replacing it — kill() releases
                    # its PID tracking + sweep-protection shield.
                    if self._bg_runtime is not None:
                        try:
                            await self._bg_runtime.kill()
                        except Exception:
                            logger.debug(
                                "get_bg_session: dead _bg runtime kill failed",
                                exc_info=True,
                            )
                    runtime = AcpRuntime(
                        agent=self._deps.runtime_agent,
                        sandbox_mode=getattr(self._owner._cfg.agent, "sandbox", "auto"),
                        acp_backend=configured_backend,
                        expect_mcp_reports=False,
                    )
                    await runtime.spawn()
                    self._bg_runtime = runtime
                # Pinned under the lock: use the selected object even if a later
                # displacement changes the shared slot.
                selected = self._bg_runtime if runtime_capable else None
            if selected is None:
                await self._owner._retire_stale_backend_bg_runtime()
                return await self._owner._provider_backed_bg_session()
            try:
                return await selected.create_session(agent=self._deps.runtime_agent)
            except AcpRuntimeDead:
                if attempt >= max_retries:
                    raise
                logger.warning(
                    "get_bg_session: _bg runtime died, respawning (attempt %d/%d)",
                    attempt + 1,
                    max_retries,
                )
                async with self._bg_runtime_lock:
                    if self._bg_runtime is not None and not self._bg_runtime.is_alive():
                        try:
                            await self._bg_runtime.kill()
                        except Exception:
                            logger.debug(
                                "get_bg_session: dead _bg runtime kill failed",
                                exc_info=True,
                            )
                        self._bg_runtime = None
        raise AcpRuntimeDead("get_bg_session exhausted retries")

    async def recycle_background(self) -> None:
        """Recycle the persistent background provider when context is full."""
        background_key = self._deps.background_key
        background_agent = self._deps.background_agent
        logger = self._deps.logger
        session = self._owner._sessions.get(background_key)
        if not session:
            return

        # Take the same semaphore a turn takes, then re-validate identity and
        # liveness under the owner lock. False means the helper already released
        # the semaphore.
        if not await self._owner._reacquire_and_validate(background_key, session):
            return
        try:
            provider = session.provider

            # check_context_usage is a chat-turn hook and never advances the
            # background entry, so count its completed turn here.
            session.prompt_count += 1

            pct = provider.context_usage_pct()
            needs_recycle = pct >= self._deps.bg_recycle_pct
            post_compaction = pct == 0.0 and self._deps.context_pct_is_unknown(provider)
            if not needs_recycle and post_compaction:
                needs_recycle = True
            elif not needs_recycle and pct == 0.0:
                needs_recycle = session.prompt_count >= self._deps.bg_blind_recycle_prompts

            if not needs_recycle:
                return

            if pct > 0:
                reason = f"context at {pct:.0f}%"
            elif post_compaction:
                reason = "compacted in place (context size unknown)"
            else:
                reason = f"blind ({session.prompt_count} prompts)"
            logger.info("Recycling background session — %s", reason)

            if not self._owner._provider_factory:
                return
            # Spawn the replacement BEFORE tearing the old one down: a failed
            # spawn leaves the working session in place.
            try:
                replacement = self._owner._provider_factory(
                    background_key,
                    agent=background_agent,
                )
                async with self._owner._start_sem:
                    await replacement.start()
            except Exception:
                logger.warning(
                    "Background session recycle kept the old provider — "
                    "replacement failed to start",
                    exc_info=True,
                )
                return

            async with self._owner._lock:
                # Lifecycle methods do not take the turn semaphore, so the entry
                # can still have moved while the replacement was starting.
                adopted = self._owner._sessions.get(background_key) is session
                if adopted:
                    session.adopt_provider(replacement)

            doomed = provider if adopted else replacement
            try:
                await doomed.shutdown()
            except Exception:
                logger.debug(
                    "Background recycle provider shutdown failed",
                    exc_info=True,
                )
        finally:
            session.semaphore.release()

    async def recycle_heartbeat(self) -> None:
        """Tear down the heartbeat session at the end of a cycle."""
        heartbeat_key = self._deps.heartbeat_key
        logger = self._deps.logger
        session = self._owner._sessions.get(heartbeat_key)
        if not session:
            return

        pct = session.provider.context_usage_pct()
        logger.info(
            "Recycling heartbeat session — cycle end (context at %.0f%%)",
            pct,
        )

        # Kill old session — the next get_or_create starts a fresh one. This is
        # deliberately cycle-scoped, never per concurrently gathered task.
        async with self._owner._lock:
            old = self._owner._sessions.pop(heartbeat_key, None)
            if old:
                # Same tick as the pop, before the shutdown await. An unrecorded
                # removal here does not merely lose a sample: the start crumb
                # survives and the next boot reports this cleanly recycled
                # session as crashed.
                await record_session_ended(heartbeat_key, end_reason=END_REASON_RECYCLED)
        if old:
            await old.provider.shutdown()


__all__ = [
    "BackgroundRuntimeDeps",
    "BackgroundRuntimeState",
    "BackgroundSessionRuntime",
    "_ProviderBgSession",
]
