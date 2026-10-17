"""The manager's live configuration: re-reading the sizing config, the turn limit and
completion keep, the effective concurrency cap and its raise listener, and the queue
dispatch release.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_admission``, ``_queue``, ``_run_events``.

Manager fields written: ``_adaptive_cap``, ``_cap_raise_listener``, ``_default_timeout``,
``_default_turn_limit``, ``_last_sizing_fields``, ``_max_concurrent``,
``_queue_dispatch_held``, ``_result_ttl_secs``, ``_spawn_stagger_secs``,
``_stall_idle_secs``, ``_subagent_queue_max_wait_secs``, ``_user_max_concurrent``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        _STALL_IDLE_SECS,
        _TIMEOUT_SECS,
        Callable,
        KiroCrewConfig,
        SubagentInfo,
        SubagentManager,
        asyncio,
        logger,
        resolve_max_subagents,
    )


def _effective_turn_limit(self: SubagentManager, info: SubagentInfo) -> int:
    return self._run_events._effective_turn_limit_impl(info)


def update_completion_keep(self: SubagentManager, mode: str, max_chars: int) -> None:
    return self._run_events.update_completion_keep_impl(mode, max_chars)


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


async def reconfigure(self: SubagentManager, cfg: KiroCrewConfig) -> None:
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


def apply_limits(
    self: SubagentManager, cfg: KiroCrewConfig, *, max_concurrent: int | None = None
) -> None:
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


def release_queue_dispatch(self: SubagentManager) -> None:
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


def _clamp_effective_cap(self: SubagentManager) -> int:
    if self._adaptive_cap is None:
        return self._user_max_concurrent
    return max(0, min(self._user_max_concurrent, int(self._adaptive_cap)))


def set_cap_raise_listener(self: SubagentManager, listener: Callable[[], object] | None) -> None:
    """Register the ONE hook a cap raise rings, or ``None`` to drop it.

    For a gate that is bounded by ``max_concurrent`` but lives outside this
    manager: it can read the new cap whenever it likes, but it has no edge
    to react to, and its own occupancy may never produce one (see
    :meth:`_notify_cap_raised`). Set, never appended: the gateway owns the
    single runner lane and re-registration replaces the stale handle.
    """
    self._cap_raise_listener = listener


def _notify_cap_raised(self: SubagentManager) -> None:
    """Fan freed capacity out to every gate the live cap bounds.

    MUST be called on the event loop, not from a worker thread: the runner
    lane resolves its parked waiters' futures, which is loop-affine.

    The subagent queue drains through the staggered pump. The runner lane
    (`taskq/adapters/runner_lane.py`) reads this cap as its ceiling but parks its
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


def set_effective_cap(self: SubagentManager, cap: int | None) -> int:
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
