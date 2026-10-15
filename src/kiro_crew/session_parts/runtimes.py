"""The shared runtimes beside the per-key sessions: background, warm pool and companions.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: none.

Instance fields read: ``_background_runtime``, ``_pool``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.executors import subprocess_executor
from kiro_crew.messaging.link import telemetry_channel_of
from kiro_crew.metrics.provider import get_recorder
from kiro_crew.session_parts.policy import POOL_DECISIONS
from kiro_crew.session_pid import _sync_kill_provider
from kiro_crew.session_pool import WarmSessionPool
from kiro_crew.start_priority import StartPriority

if TYPE_CHECKING:
    from kiro_crew.session import AcpRuntime, AcpSessionHandle, LLMProvider, SessionManager
    from kiro_crew.session_background import _ProviderBgSession

#: The session manager's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.session")


# ── Background Session ──


async def start_pool(self: SessionManager, *, blocking: bool = True) -> None:
    """Delegate background and warm-pool startup."""
    await self._pool.start_pool(blocking=blocking)


async def _ensure_background(
    self: SessionManager, *, start_priority: StartPriority = StartPriority.BACKGROUND
) -> None:
    """Delegate creation of the persistent background session."""
    await self._background_runtime._ensure_background(start_priority=start_priority)


# ── Warm Pool ──


def _configured_bg_backend_raw(self: SessionManager) -> str | None:
    """Return the configured background backend when readable."""
    return self._background_runtime._configured_bg_backend_raw()


def _configured_bg_backend(self: SessionManager) -> str:
    """Return the backend background runtimes should use."""
    return self._background_runtime._configured_bg_backend()


def _bg_backend_supports_runtime(self: SessionManager) -> bool:
    """Return whether the configured backend supports a shared runtime."""
    return self._background_runtime._bg_backend_supports_runtime()


async def _reap_drained_bg_runtimes_locked(self: SessionManager) -> None:
    """Delegate reaping of drained displaced runtimes."""
    await self._background_runtime._reap_drained_bg_runtimes_locked()


async def _detach_bg_runtime_locked(
    self: SessionManager, runtime: "AcpRuntime", cause: str, *, park_only: bool = False
) -> None:
    """Delegate freeing the ``_bg`` slot (kill idle / park busy to drain)."""
    await self._background_runtime._detach_bg_runtime_locked(
        cast(Any, runtime), cause, park_only=park_only
    )


async def _displace_bg_runtime_locked(
    self: SessionManager, runtime: "AcpRuntime", cached_backend: str, configured_backend: str
) -> None:
    """Delegate backend-switch displacement under the runtime lock."""
    await self._background_runtime._displace_bg_runtime_locked(
        cast(Any, runtime), cached_backend, configured_backend
    )


async def _retire_stale_backend_bg_runtime(self: SessionManager) -> None:
    """Delegate stale-backend runtime retirement."""
    await self._background_runtime._retire_stale_backend_bg_runtime()


async def _reap_idle_stale_bg_runtime(self: SessionManager) -> bool:
    """Delegate the periodic idle-and-stale retirement of the shared runtime."""
    return await self._background_runtime.reap_idle_stale_bg_runtime()


async def _provider_backed_bg_session(
    self: SessionManager, start_priority: StartPriority = StartPriority.BACKGROUND
) -> "_ProviderBgSession":
    """Return the serialized provider-backed background adapter."""
    return cast(
        "_ProviderBgSession",
        await self._background_runtime._provider_backed_bg_session(start_priority),
    )


async def get_bg_session(
    self: SessionManager, start_priority: StartPriority = StartPriority.BACKGROUND
) -> "AcpSessionHandle | _ProviderBgSession":
    """Acquire a background handle from the configured runtime shape.

    ``start_priority`` orders its ``session/new`` (rule: ``kiro_crew.start_priority``).
    """
    return cast(
        "AcpSessionHandle | _ProviderBgSession",
        await self._background_runtime.get_bg_session(start_priority),
    )


async def get_subagent_runtime(
    self: SessionManager,
    parent_session_key: str,
    agent: str | None = None,
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    on_gate_queued: Callable[..., None] | None = None,
    on_gate_acquired: Callable[..., None] | None = None,
) -> "AcpRuntime":
    """Get or spawn the shared companion runtime for a parent (the priority
    and clock callbacks: the allocation boundary's ``get_subagent_runtime``)."""
    return await self._allocation_boundary().get_subagent_runtime(
        parent_session_key,
        agent=agent,
        start_priority=start_priority,
        on_gate_queued=on_gate_queued,
        on_gate_acquired=on_gate_acquired,
    )


async def release_subagent_runtime(
    self: SessionManager, parent_session_key: str, *, expected: Any = None
) -> bool:
    """Release the shared companion runtime for a parent.

    ``expected`` restricts the release to that runtime object (see the
    allocation boundary); returns whether one was released.
    """
    return await self._allocation_boundary().release_subagent_runtime(
        parent_session_key, expected=expected
    )


async def _get_or_bootstrap_run_runtime(
    self: SessionManager,
    parent_session_key: str,
    *,
    agent: str | None = None,
    cwd: str | None = None,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> "AcpRuntime":
    """Get or bootstrap a task-runner shared runtime."""
    return await self._allocation_boundary()._get_or_bootstrap_run_runtime(
        parent_session_key, agent=agent, cwd=cwd, start_priority=start_priority
    )


async def open_task_session(
    self: SessionManager,
    parent_session_key: str,
    session_key: str,
    *,
    agent: str | None = None,
    cwd: str | None = None,
    approval_policy: str = "",
    _won_race_retries: int = 0,
    start_priority: StartPriority = StartPriority.BACKGROUND,
) -> tuple[LLMProvider, bool, bool]:
    """Open a task session on its run-scoped shared runtime (``start_priority``:
    rule ``kiro_crew.start_priority``)."""
    return await self._allocation_boundary().open_task_session(
        parent_session_key,
        session_key,
        agent=agent,
        cwd=cwd,
        approval_policy=approval_policy,
        _won_race_retries=_won_race_retries,
        start_priority=start_priority,
    )


async def _fill_warm_pool(self: SessionManager) -> None:
    """Delegate warm-provider creation."""
    await self._pool._fill_warm_pool()


# Bound on the graceful shutdown attempt during a pool discard. The pool
# health sweep is a single long-lived task: an unbounded await on a wedged
# shutdown would freeze every future sweep, silently disabling TTL
# enforcement for the whole pool.
_POOL_DISCARD_TIMEOUT = 10.0


def _dispatch_hard_kill(provider: LLMProvider) -> None:
    """Dispatch a blocking provider kill off the event loop."""
    WarmSessionPool.dispatch_hard_kill(
        provider,
        get_sync_kill_provider=lambda: _sync_kill_provider,
        get_subprocess_executor=lambda: subprocess_executor(),
    )


async def _discard_pool_provider(self: SessionManager, provider: LLMProvider, context: str) -> None:
    """Delegate bounded shutdown and hard-kill fallback."""
    await self._pool._discard_pool_provider(provider, context)


def _record_pool_decision(self: SessionManager, decision: str, key: str) -> None:
    """Delegate bounded-cardinality pool decision telemetry."""
    pool = self.__dict__.get("_pool")
    if pool is not None:
        pool._record_pool_decision(decision, key)
        return
    # Focused metric tests construct the facade with ``__new__``. Keep that
    # established seam without inventing a config-backed pool.
    try:
        get_recorder().counter(
            "kirocrew.session.pool.decision",
            1,
            attrs={
                "outcome": decision if decision in POOL_DECISIONS else "other",
                "channel": telemetry_channel_of(key),
            },
        )
    except Exception:
        logger.debug("pool decision metric emit failed", exc_info=True)


def _claim_from_pool(self: SessionManager, agent: str | None) -> tuple[LLMProvider, float] | None:
    """Delegate exact-agent warm-pool claiming."""
    return self._pool._claim_from_pool(agent)


async def _drain_and_claim(self: SessionManager, agent: str | None) -> LLMProvider | None:
    """Delegate stale/dead provider filtering during claim."""
    return await self._pool._drain_and_claim(agent)


def _schedule_replenish(self: SessionManager) -> None:
    """Delegate owned refill-task scheduling."""
    self._pool._schedule_replenish()


def _pool_pids(self: SessionManager) -> set[int]:
    """Return pooled and in-sweep provider PIDs."""
    return self._pool._pool_pids()


def _in_flight_pids(self: SessionManager) -> set[int]:
    """Return start-to-registration PID shields."""
    return self._pool._in_flight_pids()


def _companion_runtime_pids(self: SessionManager) -> set[int]:
    """PIDs of live AcpRuntimes NOT registered as ``self._sessions`` entries.

    Every AcpRuntime records its
    PID in ``kiro_session_pids.txt`` at spawn, so the periodic orphan sweep
    treats any tracked PID it can't find in the active set as an orphan and
    SIGKILLs it (surfacing as ``process exited (rc=-9)`` mid-chat). Two
    runtime kinds live OUTSIDE ``self._sessions`` and are therefore invisible
    to ``_collect_active_pids``:

    - ``self._subagent_runtimes`` — companion runtimes multiplexing a parent
      session's subagents (alive for the parent's whole lifetime), plus any
      ``_draining_subagent_runtimes`` displaced by the spawn-identity gate
      while their in-flight work finishes.
    - ``self._bg_runtime`` — the background runtime backing ``get_bg_session``
      (kirocrew-lite title-gen / memory consolidation), plus any
      ``_draining_bg_runtimes`` displaced by a backend switch or by
      staleness while their handles finish — killing one mid-drain is
      exactly what parking avoids.

    All are shielded from the sweep by unioning their live PIDs into the
    active set here (mirrors ``_pool_pids``/``_in_flight_pids``). Only alive
    runtimes contribute — a dead entry SHOULD be reaped. Returns a copy.
    """
    pids: set[int] = set()
    for runtime in [
        *self._subagent_runtimes.values(),
        *self._draining_subagent_runtimes,
    ]:
        try:
            if runtime is not None and runtime.is_alive() and isinstance(runtime.pid, int):
                pids.add(runtime.pid)
        except Exception:
            logger.debug("companion runtime pid probe failed", exc_info=True)
    for bg in [self._bg_runtime, *self._draining_bg_runtimes]:
        try:
            if bg is not None and bg.is_alive() and isinstance(bg.pid, int):
                pids.add(bg.pid)
        except Exception:
            logger.debug("bg runtime pid probe failed", exc_info=True)
    return pids


_POOL_HEALTH_INTERVAL = 30  # seconds between health sweeps


async def _pool_health_loop(self: SessionManager) -> None:
    """Delegate the periodic warm-pool health loop."""
    await self._pool._pool_health_loop()


async def _sweep_warm_pool_once(self: SessionManager) -> None:
    """Delegate one race-safe warm-pool sweep."""
    await self._pool._sweep_warm_pool_once()


async def recycle_background(
    self: SessionManager,
    session_key: str | None = None,
    *,
    agent: str | None = None,
    context_overflowed: bool = False,
) -> None:
    """Recycle a persistent background-style provider when context is full."""
    await self._background_runtime.recycle_background(
        session_key, agent=agent, context_overflowed=context_overflowed
    )


async def recycle_heartbeat(self: SessionManager) -> None:
    """Delegate cycle-scoped heartbeat-provider recycling."""
    await self._background_runtime.recycle_heartbeat()


async def _retire_kiro_warm_pool(self: SessionManager) -> bool:
    """Delegate pooled-provider retirement after identity change."""
    return await self._pool._retire_kiro_warm_pool()


def _mark_identity_epoch(self: SessionManager) -> None:
    """Disqualify already-pooled providers from claims after an account change."""
    self._pool.mark_identity_epoch()


async def drain_warm_pool(self: SessionManager) -> list:
    """Remove and return all queued warm providers."""
    return await self._pool.drain_warm_pool()


def warm_providers(self: SessionManager) -> list[LLMProvider]:
    """Snapshot the queued warm providers without consuming them."""
    return self._pool.warm_providers()
