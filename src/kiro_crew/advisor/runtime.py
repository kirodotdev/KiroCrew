"""Advisor reviewer runtime: one bounded, self-healing pool.

One shared reviewer runtime process serves every enabled parent session (v1
has a single global reviewer model), with one isolated reviewer session per
parent. The lifecycle mirrors the existing worker-pool precedent: serialized
startup, bounded review concurrency, replacement of a dead runtime on the
next acquire, and reaping after an idle grace once the last reviewer session
is released. PID protection is the runtime's own (``AcpRuntime``).

Collaborators are injected (runtime factory, the prompt function) so policy
stays testable without a live ACP process; the service layer wires the real
ones.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)


class _RuntimeLike(Protocol):
    """The slice of the ACP runtime surface the pool relies on."""

    @property
    def pid(self) -> int | None: ...

    def is_alive(self) -> bool: ...

    async def spawn(self) -> None: ...

    async def kill(self, *, expected: bool = False) -> None: ...


class ReviewerSession:
    """One parent's isolated reviewer conversation on the shared runtime."""

    def __init__(self, parent_session_key: str) -> None:
        self.parent_session_key = parent_session_key
        # Derived from the parent key so reviewer spend stays traceable across
        # restarts; distinct from the parent's own usage key by the prefix.
        self.session_id = f"advisor:{parent_session_key}"


class AdvisorReviewerRuntime:
    """Owns the shared reviewer process and per-parent reviewer sessions."""

    def __init__(
        self,
        runtime_factory: Callable[[], _RuntimeLike],
        prompt_fn: Callable[[ReviewerSession, dict[str, Any]], Awaitable[Any]],
        max_concurrent: int = 2,
        idle_grace_secs: float = 30.0,
        pre_spawn: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._runtime_factory = runtime_factory
        #: Async hook awaited before every runtime spawn -- the composition
        #: layer offloads agent-spec materialization (file I/O) here so it
        #: never blocks the event loop. Its failure aborts acquisition:
        #: launching without verified spec hardening would fail open.
        self._pre_spawn = pre_spawn
        self._prompt_fn = prompt_fn
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._idle_grace_secs = idle_grace_secs
        self._runtime: _RuntimeLike | None = None
        self._lock = asyncio.Lock()
        self._sessions: dict[str, ReviewerSession] = {}
        self._reap_task: asyncio.Task[None] | None = None

    # -- sessions ----------------------------------------------------------

    async def acquire_session(self, parent_session_key: str) -> ReviewerSession:
        """Return the parent's reviewer session, creating runtime as needed."""
        self._cancel_reap()
        async with self._lock:
            await self._ensure_runtime_locked()
            session = self._sessions.get(parent_session_key)
            if session is None:
                session = ReviewerSession(parent_session_key)
                self._sessions[parent_session_key] = session
            return session

    async def release_session(self, parent_session_key: str) -> None:
        """Dispose a parent's reviewer session; reap the runtime when idle."""
        async with self._lock:
            self._sessions.pop(parent_session_key, None)
            if not self._sessions and self._runtime is not None:
                self._schedule_reap()

    # -- reviewing ---------------------------------------------------------

    async def review(self, parent_session_key: str, payload: dict[str, Any]) -> Any | None:
        """Run one bounded review prompt for a parent session.

        Reviewer failures never propagate: the session degrades visibly and
        the caller receives ``None``. The primary turn must not be affected
        by a broken reviewer.
        """
        session = self._sessions.get(parent_session_key)
        if session is None:
            # The caller's key was never acquired (a rebind can move the
            # slot's key between acquire and review): honor the contract --
            # degrade to None, never raise out of a fire-and-forget task.
            logger.warning(
                "advisor review requested for unacquired session %s; degrading",
                parent_session_key,
            )
            return None
        # The caller's LIVE authorization predicate (observer identity,
        # boundary generation, slot key). Not serialized into the prompt.
        authorized = payload.get("_authorized")
        async with self._semaphore:
            # Re-check AFTER the semaphore wait: an opt-out or reset landing
            # while this review sat queued revokes its authorization, and
            # the evidence must not be transmitted after that.
            if callable(authorized) and not authorized():
                logger.info(
                    "advisor review cancelled before transmission: authorization "
                    "revoked while queued for %s",
                    parent_session_key,
                )
                return None
            # The prompt function opens its reviewer session on the SHARED
            # runtime, which only this pool holds; hand it over on a copy so
            # the caller's payload is never mutated, with the predicate still
            # in it: that session open is an await the prompt function must
            # re-check across. Re-checked under the lock so a crash between
            # acquire and review still self-heals.
            try:
                async with self._lock:
                    runtime = await self._ensure_runtime_locked()
            except Exception:
                # Covers pre_spawn (spec hardening MUST persist before any
                # spawn -- failing open is worse than not reviewing), the
                # factory, and the spawn itself. Contract as documented:
                # degrade visibly, return None, never break the primary turn.
                logger.warning(
                    "advisor reviewer runtime unavailable; degrading session",
                    exc_info=True,
                )
                return None
            payload = dict(payload)
            payload["_runtime"] = runtime
            # Final check immediately before the prompt leaves the process:
            # the runtime ensure above can itself await (a cold spawn).
            if callable(authorized) and not authorized():
                logger.info(
                    "advisor review cancelled before transmission: authorization "
                    "revoked during runtime acquisition for %s",
                    parent_session_key,
                )
                return None
            try:
                result = await self._prompt_fn(session, payload)
            except Exception:
                logger.warning(
                    "advisor reviewer prompt failed; degrading session",
                    exc_info=True,
                )
                return None
            return result

    # -- lifecycle ---------------------------------------------------------

    async def shutdown(self) -> None:
        """Kill the shared runtime and clear all reviewer sessions."""
        self._cancel_reap()
        async with self._lock:
            self._sessions.clear()
            await self._kill_runtime_locked()

    async def _ensure_runtime_locked(self) -> _RuntimeLike:
        runtime = self._runtime
        if runtime is not None and not runtime.is_alive():
            # Crash self-heal: replace the dead process on this acquire
            # (AcpRuntime owns its own PID protection).
            self._runtime = runtime = None
        if runtime is None:
            if self._pre_spawn is not None:
                await self._pre_spawn()
            runtime = self._runtime_factory()
            await runtime.spawn()
            self._runtime = runtime
        return runtime

    async def _kill_runtime_locked(self) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        self._runtime = None
        await runtime.kill(expected=True)

    def _schedule_reap(self) -> None:
        self._cancel_reap()
        self._reap_task = asyncio.get_running_loop().create_task(self._reap_after_grace())

    def _cancel_reap(self) -> None:
        task = self._reap_task
        if task is not None and not task.done():
            task.cancel()
        self._reap_task = None

    async def _reap_after_grace(self) -> None:
        await asyncio.sleep(self._idle_grace_secs)
        async with self._lock:
            if self._sessions:
                return
            await self._kill_runtime_locked()
