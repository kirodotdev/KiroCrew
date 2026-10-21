"""Operations over every session at once.

Adopting new config defaults for new sessions (``refresh_defaults``), reloading the
provider factory and retiring every registered session (``reload_provider_factory``),
bringing unfinished native turns to a safe boundary (``drain_active_turns``), and the
bounded shutdown of every session, background runtime and warm provider
(``close_all``, ``drain_all_providers``). A new whole-registry operation goes here.

Instance fields written: none.

Instance fields read: ``_deps``, ``_owner``, ``state``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from kiro_crew.metrics.sessions import (
    END_REASON_RETIRED,
    END_REASON_SHUTDOWN,
    record_sessions_ended,
)
from kiro_crew.session_pool import pool_kiro_agent

if TYPE_CHECKING:
    from kiro_crew.session_lifecycle import SessionLifecycleService, _SessionEntry


async def refresh_defaults(self: SessionLifecycleService, cfg: Any = None) -> None:
    """Adopt config changes that only affect new sessions.

    ``cfg`` is an already-loaded config -- the config watcher hands in the
    one it just loaded so the apply needs no second read. ``None`` loads
    here, off-loop, for the request-handler callers.
    """
    owner = self._owner
    logger = self._deps.logger
    constants = self._deps.constants()
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
        if cfg is None:
            cfg = await asyncio.to_thread(self._deps.load_config)
        # Same reason as the load: default_project_dir() reads the config
        # file and stats the workspace directory, so it stays off the loop
        # and outside owner._lock, which every session turn contends for.
        pool_cwd = await asyncio.to_thread(self._deps.default_project_dir)
        # Built before the lock is taken and before either owner attribute
        # is touched: if this raises, ``owner._cfg`` and
        # ``owner._provider_factory`` must still be the previous,
        # consistent pair -- not cfg swapped in with the old factory still
        # live, which is what a watcher retry would otherwise re-enter
        # against.
        provider_factory = self._deps.build_provider_factory(cfg)
        async with owner._lock:
            owner._cfg = cfg
            owner._provider_factory = provider_factory
            # The warm pool's shape is config too: size, agent, cwd and TTL
            # are captured into WarmPoolState at construction, so a refresh
            # that rebuilt the factory but left them alone kept spawning the
            # OLD pool size and agent, and the TTL was never re-adopted by
            # any path. Same clamp as WarmSessionPool._state_from_owner.
            owner._pool_size = min(constants.max_pool, max(0, cfg.session.pool_size))
            owner._pool_agent = pool_kiro_agent(cfg)
            owner._pool_ttl_secs = max(0, cfg.session.pool_ttl_secs)
            owner._pool_cwd = pool_cwd
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


async def reload_provider_factory(self: SessionLifecycleService, cfg: Any = None) -> None:
    """Reload the provider factory and tear down providers from the old one.

    ``cfg`` is the already-loaded config the live applier hands in so the
    switch does no filesystem work on the loop; ``None`` loads it here.
    """
    owner = self._owner
    logger = self._deps.logger
    constants = self._deps.constants()
    if cfg is None:
        cfg = self._deps.load_config()
    stale: list[tuple[str, Any]] = []
    async with owner._pool_fill_lock:
        pool_cwd = await asyncio.to_thread(self._deps.default_project_dir)
        async with owner._lock:
            owner._cfg = cfg
            owner._provider_factory = self._deps.build_provider_factory(cfg)
            # The same four pool fields refresh_defaults adopts: a reset
            # handler that loads a disk-edited pool_ttl_secs must not evict
            # the warm pool at the stale TTL until the watcher's next cycle.
            owner._pool_size = min(constants.max_pool, max(0, cfg.session.pool_size))
            owner._pool_agent = pool_kiro_agent(cfg)
            owner._pool_ttl_secs = max(0, cfg.session.pool_ttl_secs)
            owner._pool_cwd = pool_cwd
            while not owner._warm_pool.empty():
                try:
                    provider, _ = owner._warm_pool.get_nowait()
                except asyncio.QueueEmpty:
                    break
                await owner._discard_pool_provider(provider, "Stale pool drain")
            # Intentionally clear only the registry: the original reload
            # path does not rewrite session-map or compaction state here.
            stale = list(owner._sessions.items())
            for stale_key, _ in stale:
                owner._advance_session_generation(stale_key)
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


async def drain_active_turns(self: SessionLifecycleService, timeout: float | None = None) -> int:
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


async def close_all(self: SessionLifecycleService, drain_timeout: float | None = None) -> None:
    """Shut down every session after a bounded cooperative turn drain."""
    owner = self._owner
    logger = self._deps.logger
    constants = self._deps.constants()
    # Enter closing under the registry lock before taking the drain
    # snapshot. This prevents a new prompt or provider registration from
    # landing in the multi-second window after that snapshot.
    async with owner._lock:
        owner._closing = True
        owner._update_pause_owned = False
    # Nothing will claim a successor once closing; release anyone waiting
    # behind a replay gap so the shutdown does not strand their task (their
    # claim then meets the closing refusal like any other).
    for gap_key in list(self.state.replay_gaps):
        self._discard_replay_gap(gap_key)

    try:
        await owner.drain_active_turns(timeout=drain_timeout)
    except Exception:
        # CancelledError is intentionally not caught: callers use an outer
        # wait_for deadline as the hard restart cap.
        logger.debug("close_all: drain_active_turns failed", exc_info=True)

    if owner._cleanup_task:
        owner._cleanup_task.cancel()
        # Await so the coroutine's finally-block runs while the event loop is
        # still active.  Without this, _cleanup_loop is left suspended at an
        # await point; when it is later garbage-collected Python throws
        # GeneratorExit into it, the finally: runs boot_reclaim.cancel(), and
        # boot_reclaim.cancel() calls Future.__schedule_callbacks() which calls
        # loop.call_soon() on the now-closed loop, raising RuntimeError("Event
        # loop is closed") -- which fires sys.unraisablehook and, if the call
        # stack is deep at GC time, causes RecursionError inside
        # traceback.format_exception and fails the next test's setup.
        # return_exceptions=True absorbs the task's own CancelledError without
        # swallowing a cancellation aimed at close_all itself (CancelledError
        # propagates from gather when the *caller* cancels, not the gathered
        # task).  This keeps close_all cancellable by its wait_for deadline.
        await asyncio.gather(owner._cleanup_task, return_exceptions=True)

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
            await bg_runtime.kill(expected=True, reason="graceful shutdown")
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
    # Parked displaced runtimes are live processes outside the registry;
    # shutdown ends them like every other holder.
    parked_doomed = list(owner._draining_subagent_runtimes)
    owner._draining_subagent_runtimes[:] = []
    for parked in parked_doomed:
        try:
            await parked.kill(expected=True, reason="graceful shutdown")
        except Exception:
            logger.debug("close_all: displaced subagent runtime cleanup failed", exc_info=True)

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
            # The identity sweep already moved this old-account pointer to
            # discarded_sid. Do not map the live child's sid back at shutdown.
            if sess.retire_on_identity_change:
                continue
            cwd_str = sess.provider.cwd
            if isinstance(sess.provider, acp_provider_type):
                sid = sess.provider.client._session_id
                # A replay-pending fresh child is not yet the durable
                # conversation. Allocation retained the prior full-history
                # SID; shutdown must not overwrite it before the replay
                # settlement in chat_runner commits the fresh transcript.
                if (
                    sid
                    and not sess.provider_switch_replay
                    and key != constants.background_key
                    and (
                        not (
                            key in constants.stateless_keys
                            or any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
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
                        not (
                            key in constants.stateless_keys
                            or any(
                                key.startswith(prefix) for prefix in constants.stateless_prefixes
                            )
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
        for session_key in sessions:
            owner._advance_session_generation(session_key)
        owner._sessions.clear()
        owner._compact_cooldown_until.clear()
        self._suppress_replay.clear()
        owner._compact_pending_verdict.clear()
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


async def drain_all_providers(self: SessionLifecycleService) -> list[Any]:
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
            owner._advance_session_generation(key)
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
