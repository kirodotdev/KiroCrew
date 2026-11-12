"""Eviction: ending a registered session the claim path found it cannot hand out.

``_evict_stale_session`` pops the one session object a claim observed -- a dead one, one
flagged for an account change, or a live one spawned under a looser ``agent.sandbox``
tier than is now configured (``_sandbox_tightened``) -- and closes it outside the lock.
Once the pop has happened nothing else reaches that process, so the close runs to its
end even when the caller is cancelled (``_drain_despite_cancellation``).

Each ``self``-first function here is a ``SessionAllocationService`` member: its body is
the method's, and the class binds it by name, so the instance and its registry state
stay on the class. This module never imports :mod:`kiro_crew.session_allocation` at run
time.

Instance fields written: ``_sessions``.

Instance fields read: ``_deps``, ``_lock``, ``_owner``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from kiro_crew.metrics.sessions import (
    END_REASON_EVICTED,
    END_REASON_RETIRED,
    record_session_ended,
)
from kiro_crew.runtime_ownership import release_session_lease
from kiro_crew.session_allocation_parts.companions import sandbox_tier_tightened

if TYPE_CHECKING:
    from kiro_crew.session_allocation import LLMProvider, SessionAllocationService


def _sandbox_tightened(self: SessionAllocationService, provider: Any) -> bool:
    """Whether ``agent.sandbox`` is now stricter than the tier *provider* was spawned under."""
    return sandbox_tier_tightened(provider.sandbox_mode, self._owner._cfg.agent.sandbox)


async def _drain_despite_cancellation(work: Coroutine[Any, Any, None]) -> None:
    """Run *work* to its end despite caller cancellation, then re-raise that cancellation."""
    cleanup = asyncio.create_task(work)
    cancellation: asyncio.CancelledError | None = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as exc:
            cancellation = exc
    await cleanup
    if cancellation is not None:
        raise cancellation


async def _evict_stale_session(
    self: SessionAllocationService, key: str, session: Any, *, held_by_caller: bool = False
) -> None:
    """Pop only the observed stale object and close it outside the lock, even when cancelled.

    A live session evicted because ``agent.sandbox`` is now stricter than its spawn
    tier is a recycle: its queued follow-ups are parked for the successor, and its
    whole process tree is killed before the shutdown, unless an incognito or temporary
    shutdown must first delete its record. A turn that took its permit
    since the claim refused it (``try_acquire`` needs no claim) keeps it: the next
    claim recycles it. That turn is named in ``turn_owner``; a permit the refusing
    claim's release handed on to a parked claimant names no one and does not keep it.
    ``held_by_caller`` says the permit is the caller's own.
    """
    dead: LLMProvider | None = None
    tightened = False
    async with self._lock:
        if self._sessions.get(key) is session:
            # An identity retirement keeps its own eviction, whatever the tier.
            tightened = (
                not session.retire_on_identity_change
                and self._deps.provider_effectively_alive(session.provider)
                and self._sandbox_tightened(session.provider)
            )
            # A locked permit naming no holder was just handed to a parked claimant,
            # which finds the session gone (``_orphan_turn_holder``); only a turn keeps it.
            if (
                tightened
                and not held_by_caller
                and session.turn_owner is not None
                and session.semaphore.locked()
            ):
                return
            del self._sessions[key]
            self.advance_ownership_generation(key)
            dead = session.provider
            if tightened:
                # Same lock hold as the pop, as ``reset`` parks them.
                self._owner._lifecycle_boundary()._park_reset_queue(key, session)
            # Same lock hold as the removal, before any await: a turn still
            # holding this permit is recorded and its parked claimants woken,
            # exactly as every lifecycle teardown verb does.
            self._owner.retire_turn_permit(key, session)
            # Same tick as the removal. Left unrecorded, the start crumb
            # survives and the next boot calls this a crash.
            await record_session_ended(
                key,
                end_reason=END_REASON_RETIRED if tightened else END_REASON_EVICTED,
            )

    async def teardown(provider: LLMProvider) -> None:
        deps = self._deps
        await asyncio.to_thread(deps.unlink_session_queue, session)
        if tightened:
            deps.logger.info("Recycling %s under a stricter agent.sandbox", key)
            # Released first: the provider's own lease makes the kill gate refuse it.
            await release_session_lease(provider)
        # An incognito or temporary shutdown deletes the harness record (on KAS a verb
        # only the live engine answers) and then ends the tree itself.
        if tightened and getattr(provider, "memory_mode", "persistent") == "persistent":
            try:
                await asyncio.get_running_loop().run_in_executor(
                    deps.get_subprocess_executor(), deps.get_sync_kill_provider(), provider
                )
            except Exception:
                deps.logger.warning("Tree kill failed for %s", key, exc_info=True)
        try:
            await provider.shutdown()
        except Exception:
            deps.logger.warning("Failed to shut down stale provider for %s", key, exc_info=True)

    if dead is not None:  # nothing but this close reaches the popped process
        await _drain_despite_cancellation(teardown(dead))
