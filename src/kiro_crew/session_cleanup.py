"""Periodic cleanup and watchdog policy for session lifecycle management.

The :class:`SessionCleanup` service owns cleanup-loop state while the
``SessionManager`` facade remains the authority for the session registry and
all lifecycle mutations.  Calls back into the manager deliberately use the
facade's legacy method names: tests and integrations replace those methods on
individual manager instances, so late owner lookup is part of the compatibility
contract.

Dependencies whose defining names are patchable in ``kiro_crew.session`` are
injected as forwarding callables.  The facade must resolve those names when a
call is made rather than capture their values during construction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from kiro_crew.messaging.link import is_channel_session_key
from kiro_crew.watchdog import SessionWatchdog

if TYPE_CHECKING:
    from kiro_crew.providers.base import LLMProvider


class ShutdownSignal(Protocol):
    """The subset of ``asyncio.Event`` used by the cleanup loop."""

    def is_set(self) -> bool: ...

    def wait(self) -> Awaitable[bool]: ...


class SessionEntry(Protocol):
    """Registry entry shape consumed by cleanup policy."""

    provider: LLMProvider
    semaphore: asyncio.BoundedSemaphore
    last_used: float


class StatsPort(Protocol):
    def inc_session_cleaned(self) -> None: ...


class SelPort(Protocol):
    def log_api_access(
        self,
        *,
        caller: str,
        operation: str,
        outcome: str,
        source: str = "",
        resources: str = "",
        error: str = "",
        critical: bool = False,
    ) -> None: ...


class CleanupOwner(Protocol):
    """SessionManager operations and state retained across this boundary."""

    _cfg: Any
    _sessions: MutableMapping[str, SessionEntry]
    _lock: asyncio.Lock
    _draining_bg_runtimes: list[Any]
    _bg_runtime_lock: asyncio.Lock
    _watchdog: SessionWatchdog
    on_session_expire: Callable[[str], None] | None
    on_stuck_turn: Callable[[str, float], None] | None

    async def _cleanup_loop(self) -> None: ...

    async def _expire_idle(self, timeout_secs: int) -> None: ...

    async def _reap_drained_bg_runtimes_locked(self) -> None: ...

    async def _reap_idle_stale_bg_runtime(self) -> bool: ...

    def get_pid(self, key: str) -> int | None: ...

    async def reset(
        self,
        key: str,
        *,
        expect_session: SessionEntry | None = None,
        skip_if_busy: bool = False,
        skip_if_injecting: bool = False,
        clear_conversation: bool = False,
        ends_conversation: bool = False,
    ) -> bool: ...

    async def _fire_recycle_callback(self, key: str, *, reason: str) -> None: ...

    def _pool_pids(self) -> set[int]: ...

    def _in_flight_pids(self) -> set[int]: ...

    def _companion_runtime_pids(self) -> set[int]: ...


ActivePidCollector = Callable[
    [MutableMapping[str, SessionEntry]],
    tuple[set[int], bool],
]
PeriodicPidSweep = Callable[[int, set[int]], tuple[set[str], list[int]]]
PidWriteback = Callable[[int, list[int], set[str]], int]


def _no_attached_subagents(key: str) -> bool:
    """Default sub-agent probe: a manager with no dashboard has no children."""
    return False


def _no_pending_injection(key: str) -> bool:
    """Default injection probe: a manager with no gateway injects nothing."""
    return False


@dataclass(slots=True)
class CleanupState:
    """Mutable state exclusively owned by :class:`SessionCleanup`."""

    cleanup_task: asyncio.Task[Any] | None = None
    rss_max_mb: int = 0
    idle_sweep_enabled: bool = False
    idle_timeout: int = 0
    # The (timeout_secs, watchdog_rss_max_mb) pair the policy was last derived
    # from, as configured; transitions are logged only when it moves.
    idle_policy_source: tuple[int, int] | None = None
    stuck_reported: dict[str, float] = field(default_factory=dict)
    last_pycache_gc: float | None = None
    active_dashboard_slots: set[str] | None = None
    # Every session key a dashboard slot has owned since boot, which is what
    # makes "this session's owner is gone" answerable for a key of ANY shape.
    # ``active_dashboard_slots`` alone cannot answer it: absence from that set
    # is equally true of a session that never had a slot and is legitimately
    # running without one (a cron fire, a task step, a hook), so reaping on
    # absence alone would kill live work. A key recorded here and now absent
    # from the live set had an owner and lost it. Channel-owned keys are
    # deliberately never recorded: a tab viewing a Slack thread does not own it.
    # Pruned every sweep to the keys that still have a live session OR a slot
    # open right now, so it is bounded by the union of those two sets. An open
    # slot's key is kept even with no session yet, because a slot publishes its
    # linked key before the first turn creates that session.
    slot_owned_keys: set[str] = field(default_factory=set)
    watchdog: SessionWatchdog | None = None


@dataclass(frozen=True, slots=True)
class CleanupDeps:
    """Patch-aware dependencies for periodic session cleanup."""

    logger: logging.Logger
    get_shutdown_signal: Callable[[], ShutdownSignal]
    get_maintenance_executor: Callable[[], Executor]
    get_subprocess_executor: Callable[[], Executor]
    cleanup_orphaned_mcp_servers: Callable[[], int]
    cleanup_orphaned_session_roots: Callable[[], int]
    cleanup_stale_sandbox_profiles: Callable[[], int]
    prune_session_pid_mappings: Callable[[], int]
    prune_member_pid_bindings: Callable[[], int]
    prune_pycache: Callable[[], tuple[int, int]]
    collect_active_pids: ActivePidCollector
    periodic_pid_sweep: PeriodicPidSweep
    kill_confirmed_and_writeback: PidWriteback
    find_orphan_mcp_candidates: Callable[[set[int]], list[int]]
    kill_orphan_mcps: Callable[[list[int]], int]
    # Linux/systemd agent-scope reaper. Takes the live provider PID set and
    # returns a summary object exposing ``.reclaimed`` (int). Typed loosely so
    # this module need not import ``session_scope_reap`` at module load.
    reap_agent_scopes: Callable[[set[int]], Any]
    build_child_map: Callable[[], dict[int, list[int]]]
    rss_mb_from_tree: Callable[[int, dict[int, list[int]]], int]
    get_session_rss_mb: Callable[[int], int]
    is_windows: Callable[[], bool]
    getpid: Callable[[], int]
    monotonic: Callable[[], float]
    stats_factory: Callable[[], StatsPort]
    sel_factory: Callable[[], SelPort]
    provider_has_active_turn: Callable[[LLMProvider], bool]
    emit_counter: Callable[[str, dict[str, str | int | bool | float]], None]
    get_persistent_keys: Callable[[], frozenset[str]]
    get_channel_prefix: Callable[[], str]
    get_stuck_turn_report_secs: Callable[[], float]
    get_pycache_gc_interval_secs: Callable[[], float]
    get_session_idle_expired_event: Callable[[], str]
    # Whether *key* has sub-agent work attached (running, queued, or a
    # completion delivery in flight). With session sharing on, children run on
    # the parent's runtime after the parent's own turn ends, so the busy
    # semaphore alone cannot see them. Defaults to "no children" so a manager
    # without a dashboard keeps its existing behaviour. The answer may be an
    # AWAITABLE: the dashboard's probe reads the task store, and the sweep
    # asking is on the gateway loop.
    has_attached_subagents: Callable[[str], bool | Awaitable[bool]] = _no_attached_subagents
    # Whether a completion injection is in flight for *key*: a turn already
    # committed to that session whose semaphore is NOT held yet, because the
    # store read that precedes ``get_or_create`` is awaited first. The gateway
    # honours this counter at its own three reset sites; the sweep is the fourth
    # resetter and needs the same fact. Synchronous -- it reads one counter --
    # and defaults to "nothing injecting" so a manager without a gateway keeps
    # its existing behaviour.
    has_pending_injection: Callable[[str], bool] = _no_pending_injection


class SessionCleanup:
    """Coordinate cleanup hooks, sweeps, and idle-expiry policy."""

    # Upper bound on one sleep of the cleanup loop, so a lowered idle timeout
    # is adopted within this many seconds regardless of the previous interval.
    POLICY_REFRESH_SECS = 60.0

    # Ceiling on the tick interval itself.
    #
    # One tick drives the idle-expiry hook AND every housekeeping sweep in
    # ``_run_cleanup_ticks``: orphaned session roots, tracked PIDs, untracked
    # MCP servers, sandbox artifacts. Deriving that interval from
    # ``session.timeout_secs`` alone couples the housekeeping cadence to a
    # setting that is about something else, and couples it the wrong way round:
    # ``timeout_secs=86400`` yields an ``86400 // 6`` = 4-hour tick, while
    # DISABLING idle expiry (``timeout_secs=0``) yields 300 s. Without a ceiling,
    # asking for long-lived sessions buys slower orphan cleanup than switching
    # the idle sweep off entirely.
    #
    # ``session.py``'s module map states the intended cadence --
    # "``_expire_idle()`` -- **periodic** (every ~5 min)" -- which is this
    # ceiling. Capping cannot expire a session early: ``_expire_idle_hook``
    # passes ``state.idle_timeout``, so the timeout decides WHEN a session is
    # stale and the interval only decides how often the question is asked.
    MAX_TICK_INTERVAL_SECS = 300.0

    def __init__(
        self,
        owner: CleanupOwner,
        deps: CleanupDeps,
        *,
        state: CleanupState,
    ) -> None:
        self._owner = owner
        self._deps = deps
        if state.watchdog is None:
            raise ValueError("cleanup state requires a watchdog")
        self.state = state

    # Compatibility-shaped accessors preserve the legacy manager state seams.
    # Mutable objects are returned directly rather than copied.
    @property
    def _cleanup_task(self) -> asyncio.Task[Any] | None:
        return self.state.cleanup_task

    @_cleanup_task.setter
    def _cleanup_task(self, value: asyncio.Task[Any] | None) -> None:
        self.state.cleanup_task = value

    @property
    def _rss_max_mb(self) -> int:
        return self.state.rss_max_mb

    @_rss_max_mb.setter
    def _rss_max_mb(self, value: int) -> None:
        self.state.rss_max_mb = value

    @property
    def _idle_sweep_enabled(self) -> bool:
        return self.state.idle_sweep_enabled

    @_idle_sweep_enabled.setter
    def _idle_sweep_enabled(self, value: bool) -> None:
        self.state.idle_sweep_enabled = value

    @property
    def _idle_timeout(self) -> int:
        return self.state.idle_timeout

    @_idle_timeout.setter
    def _idle_timeout(self, value: int) -> None:
        self.state.idle_timeout = value

    @property
    def _stuck_reported(self) -> dict[str, float]:
        return self.state.stuck_reported

    @_stuck_reported.setter
    def _stuck_reported(self, value: dict[str, float]) -> None:
        self.state.stuck_reported = value

    @property
    def _last_pycache_gc(self) -> float | None:
        return self.state.last_pycache_gc

    @_last_pycache_gc.setter
    def _last_pycache_gc(self, value: float | None) -> None:
        self.state.last_pycache_gc = value

    @property
    def _active_dashboard_slots(self) -> set[str] | None:
        return self.state.active_dashboard_slots

    @_active_dashboard_slots.setter
    def _active_dashboard_slots(self, value: set[str] | None) -> None:
        self.state.active_dashboard_slots = value

    @property
    def _watchdog(self) -> SessionWatchdog:
        watchdog = self.state.watchdog
        if watchdog is None:  # pragma: no cover - constructor establishes this invariant
            raise RuntimeError("cleanup watchdog is not initialized")
        return watchdog

    @_watchdog.setter
    def _watchdog(self, value: SessionWatchdog) -> None:
        self.state.watchdog = value

    def start_cleanup(self) -> None:
        """Start the single cleanup task if none is currently live."""
        task = self.state.cleanup_task
        if task is None or task.done():
            self.state.cleanup_task = asyncio.create_task(self._owner._cleanup_loop())

    def cancel_cleanup(self) -> None:
        """Request cleanup-loop cancellation, preserving legacy task ownership."""
        if self.state.cleanup_task:
            self.state.cleanup_task.cancel()

    async def _expire_idle_hook(self) -> None:
        if not self.state.idle_sweep_enabled:
            return
        try:
            await self._owner._expire_idle(self.state.idle_timeout)
        except Exception:
            self._deps.logger.exception("Cleanup loop: _expire_idle crashed; continuing")

    async def _bg_drain_reap_hook(self) -> None:
        # Two passes over the same lock, in this order: reap what already
        # drained, then ask whether the LIVE shared runtime has itself gone idle
        # and stale. The second is not optional housekeeping -- the staleness
        # ceilings are otherwise only evaluated when the runtime is reused, and
        # its pid is shielded from the orphan sweep, so a runtime nobody calls
        # any more is never asked and never reaped (see
        # ``BackgroundSessionRuntime.reap_idle_stale_bg_runtime``).
        if self._owner._draining_bg_runtimes:
            try:
                async with self._owner._bg_runtime_lock:
                    await self._owner._reap_drained_bg_runtimes_locked()
            except Exception:
                self._deps.logger.warning(
                    "bg_drain_reap hook failed; will retry next tick",
                    exc_info=True,
                )
        try:
            if await self._owner._reap_idle_stale_bg_runtime():
                self._deps.logger.info(
                    "Periodic sweep: retired the idle stale shared background runtime"
                )
        except Exception:
            self._deps.logger.warning(
                "idle-stale bg runtime sweep failed; will retry next tick",
                exc_info=True,
            )

    async def _orphan_mcp_hook(self) -> None:
        try:
            mcp_killed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.cleanup_orphaned_mcp_servers,
            )
            if mcp_killed:
                self._deps.logger.info(
                    "Periodic sweep: cleaned %d orphaned MCP servers",
                    mcp_killed,
                )
        except Exception:
            # This sweep treats failures as a silent best-effort
            # miss.  The watchdog must not promote the severity.
            pass

    async def _reap_agent_scopes_hook(self) -> None:
        # Reclaim abandoned agent cgroup scopes (Linux/systemd; a no-op
        # elsewhere). The live provider/pool/in-flight PID set is gathered here
        # so a scope containing any live tree is never touched; the reaper reads
        # tracked PIDs from the session_pid files itself. Blocking subprocess
        # work runs on the maintenance pool, exactly like the orphan-MCP sweep.
        try:
            active_pids, safe = self._active_pids()
            if not safe:
                return
            summary = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.reap_agent_scopes,
                active_pids,
            )
            reclaimed = int(getattr(summary, "reclaimed", 0) or 0)
            if reclaimed:
                self._deps.logger.info(
                    "Periodic sweep: reclaimed %d abandoned agent scope(s)",
                    reclaimed,
                )
        except Exception:
            # Best-effort, like the orphan-MCP sweep: never promote severity.
            self._deps.logger.debug("agent-scope reap hook failed", exc_info=True)

    async def _rss_threshold_check(self) -> None:
        if not self.state.rss_max_mb:
            return

        candidates: list[tuple[str, int, SessionEntry]] = []
        persistent_keys = self._deps.get_persistent_keys()
        channel_prefix = self._deps.get_channel_prefix()
        async with self._owner._lock:
            for key, session in self._owner._sessions.items():
                if key in persistent_keys or key.startswith(channel_prefix):
                    continue
                if session.semaphore.locked():
                    continue
                pid = self._owner.get_pid(key)
                if pid is not None:
                    candidates.append((key, pid, session))

        victims: list[tuple[str, int, SessionEntry]] = []
        if candidates:
            loop = asyncio.get_running_loop()
            measure: Callable[[int], int]
            if self._deps.is_windows():

                def measure(pid: int) -> int:
                    return self._deps.get_session_rss_mb(pid)

            else:
                # A single immutable /proc snapshot is shared across every
                # candidate in a tick; rebuilding it per process is expensive.
                child_map = await loop.run_in_executor(
                    self._deps.get_maintenance_executor(),
                    self._deps.build_child_map,
                )

                def measure(pid: int) -> int:
                    return self._deps.rss_mb_from_tree(pid, child_map)

            for key, pid, session in candidates:
                rss = await loop.run_in_executor(
                    self._deps.get_maintenance_executor(),
                    measure,
                    pid,
                )
                if rss > self.state.rss_max_mb:
                    victims.append((key, rss, session))

        for key, rss, session in victims:
            try:
                # A free semaphore only proves the parent's OWN turn is over.
                # Sub-agents spawned by that turn keep running on this session's
                # runtime, so a reset here discards their work. reset() only
                # re-checks the semaphore under its lock; this is the sole
                # guard for attached children, so it runs as late as possible.
                if await self._has_attached_subagents(key):
                    self._deps.logger.debug(
                        "RSS recycle: session %s tree rss=%dMB exceeds %dMB "
                        "but has attached sub-agent work; skipping",
                        key,
                        rss,
                        self.state.rss_max_mb,
                    )
                    continue
                # Ask the injection counter AGAIN, here. The wrapper above asks
                # it once before suspending on the sub-agent probe, so an
                # injection that STARTS inside that await is invisible to that
                # read -- the same window the orphan branch closes, and reachable
                # on this path for the same reason: while a completion injection
                # is committed to this session it holds no semaphore and has no
                # running child, so neither ``skip_if_busy`` nor
                # ``expect_session`` can see it. Synchronous by contract, and
                # nothing between here and ``reset`` may suspend.
                if self._injection_pending(key):
                    self._deps.logger.debug(
                        "RSS recycle: session %s began an injection mid-check; skipping",
                        key,
                    )
                    continue
                # reset revalidates both object identity and the busy semaphore
                # under its own lock after the unlocked RSS measurement, and
                # ``skip_if_injecting`` puts the counter read under that same
                # lock. The read above is a cheap early-out that also names its
                # own reason in the log; this one is the atomic answer, because
                # acquiring the lock suspends and an injection can begin there.
                recycled = await self._owner.reset(
                    key,
                    expect_session=session,
                    skip_if_busy=True,
                    skip_if_injecting=True,
                )
                if not recycled:
                    continue
                self._deps.logger.warning(
                    "RSS recycle: session %s tree rss=%dMB exceeds %dMB",
                    key,
                    rss,
                    self.state.rss_max_mb,
                )
                self._deps.stats_factory().inc_session_cleaned()
                await self._owner._fire_recycle_callback(
                    key,
                    reason=f"memory limit ({rss}MB)",
                )
            except Exception:
                # One victim cannot suppress the rest of this tick.
                self._deps.logger.exception("RSS recycle failed for session %s", key)

    def _injection_pending(self, key: str) -> bool:
        """Fail-closed read of "is a completion injection in flight for *key*?".

        Synchronous by contract, which is what lets it be asked in the last
        breath before ``reset`` without reopening the window it closes. An
        unreadable counter is an unknown turn, not the absence of one, so it
        keeps the session.
        """
        try:
            return bool(self._deps.has_pending_injection(key))
        except Exception:
            self._deps.logger.debug(
                "Injection probe failed for session %s; keeping it",
                key,
                exc_info=True,
            )
            return True

    async def _has_attached_subagents(self, key: str) -> bool:
        """Fail-closed wrapper around the injected work probes.

        A probe that raises is a probe that cannot see the children, not a
        session with none; recycling on that answer is exactly the hazard the
        guard exists to prevent, so an error keeps the session.

        An AWAITABLE answer is awaited: the dashboard's probe reads the task
        store off the loop, and a sync probe (a double, a build with no queue)
        answers straight away.

        A PENDING COMPLETION INJECTION also counts as attached work, and is
        asked FIRST because it costs no await. The gateway increments that
        counter before awaiting the injected turn's store read, so in the window
        before ``get_or_create`` the session holds no semaphore, has no running
        child (the delivering sub-agent is already ``done``) and, with its tab
        closed, no in-flight delivery either. Every signal this guard otherwise
        reads is absent while a turn is committed to that session, and the reset
        discards the very runtime the injection is about to write into.

        That read happens BEFORE the probe below suspends, so it cannot see an
        injection that starts inside the await. It is therefore not sufficient on
        its own: a caller that resets after awaiting this wrapper re-asks
        ``_injection_pending`` immediately before the act. Both such callers do
        -- the idle sweep's orphan branch and the RSS recycle.
        """
        try:
            if self._injection_pending(key):
                return True
            answer = self._deps.has_attached_subagents(key)
            if isinstance(answer, Awaitable):
                answer = await answer
            return bool(answer)
        except Exception:
            self._deps.logger.debug(
                "Work probe failed for session %s; keeping it",
                key,
                exc_info=True,
            )
            return True

    async def _stuck_turn_check(self) -> None:
        try:
            stuck: list[tuple[str, float]] = []
            live_parks: dict[str, float] = {}
            async with self._owner._lock:
                for key, session in self._owner._sessions.items():
                    if not session.semaphore.locked():
                        continue
                    handle = getattr(session.provider, "_handle", None)
                    if handle is None:
                        continue
                    parked_for = getattr(handle, "parked_for_secs", None)
                    if not callable(parked_for):
                        continue
                    parked = float(parked_for())
                    if parked <= self._deps.get_stuck_turn_report_secs():
                        continue
                    if getattr(handle, "awaiting_permission", False):
                        continue
                    began = getattr(handle, "parked_since", None)
                    ident = float(began) if isinstance(began, (int, float)) else parked
                    live_parks[key] = ident
                    if self.state.stuck_reported.get(key) == ident:
                        continue
                    stuck.append((key, parked))

            # The latch tracks park identity and is dropped as soon as a session
            # is no longer parked, allowing a later park to report again.
            self.state.stuck_reported = live_parks
            for key, parked in stuck:
                self._deps.logger.warning(
                    "Turn on session %s has not been pulled for %.0fs — its "
                    "consumer is parked, so the in-band watchdog cannot run",
                    key,
                    parked,
                )
                if self._owner.on_stuck_turn:
                    try:
                        self._owner.on_stuck_turn(key, parked)
                    except Exception:
                        self._deps.logger.debug(
                            "on_stuck_turn callback failed",
                            exc_info=True,
                        )
        except Exception:
            self._deps.logger.exception("Cleanup loop: _stuck_turn_check crashed; continuing")

    def _adopt_idle_policy(self) -> float:
        """Re-derive the idle-sweep policy from the owner's CURRENT config.

        Returns the tick interval. Called at loop start and again before every
        sleep, so a ``session.timeout_secs`` or ``session.watchdog_rss_max_mb``
        write reaches the loop on its next tick from any writer -- the values
        are never frozen into the state for the loop's lifetime. The clamps are
        the loader's: a timeout in (0, 60) becomes 60, a negative or non-int RSS
        ceiling disables the check. Transitions are logged once, on change,
        so a steady config costs the loop nothing but two attribute reads.

        The returned interval is capped at :data:`MAX_TICK_INTERVAL_SECS`: the
        tick also drives the housekeeping sweeps, which must not slow down
        because an operator asked for long-lived sessions. See that constant for
        why the cap is the same 300 s the disabled path already used.
        """
        cfg = self._owner._cfg
        timeout = cfg.session.timeout_secs
        if not isinstance(timeout, int) or isinstance(timeout, bool):
            timeout = 0
        rss_cfg = getattr(cfg.session, "watchdog_rss_max_mb", 0)
        rss_max = (
            max(0, rss_cfg) if isinstance(rss_cfg, int) and not isinstance(rss_cfg, bool) else 0
        )
        changed = (timeout, rss_max) != self.state.idle_policy_source
        self.state.idle_policy_source = (timeout, rss_max)

        if 0 < timeout < 60:
            if changed:
                self._deps.logger.warning(
                    "session.timeout_secs=%d is below minimum 60; clamping to 60",
                    timeout,
                )
            timeout = 60
        idle_sweep_enabled = timeout > 0
        if not idle_sweep_enabled and changed:
            self._deps.logger.info(
                "Idle session sweep disabled (session.timeout_secs=%d); "
                "MCP/PID sweeps still run at default cadence",
                timeout,
            )
        self.state.idle_sweep_enabled = idle_sweep_enabled
        self.state.idle_timeout = timeout

        if rss_max != self.state.rss_max_mb:
            self._deps.logger.info(
                "session.watchdog_rss_max_mb now %d (was %d)", rss_max, self.state.rss_max_mb
            )
            self.state.rss_max_mb = rss_max

        return float(
            min(max(timeout // 6, 60), self.MAX_TICK_INTERVAL_SECS)
            if idle_sweep_enabled
            else self.MAX_TICK_INTERVAL_SECS
        )

    async def _cleanup_loop(self) -> None:
        interval = self._adopt_idle_policy()

        # One reclaim pass at START, and deliberately NOT awaited here. Every
        # other sweep in this loop is housekeeping that can wait an interval, but
        # this one reclaims the runtime-tmpfs entries whose exhaustion makes
        # `systemd-run --scope` fail, and a host in that state cannot spawn an
        # agent AT ALL -- so an update that installs the fix must apply it now,
        # not in 5-10 minutes. Fire-and-forget for two reasons: the loop's other
        # sweeps (idle sessions, PIDs, MCPs) must not queue behind it, and a pass
        # slowed by a pathological pile or a stalled filesystem must not be able
        # to keep this loop from ever starting. The work itself is bounded twice
        # over: it runs in the maintenance executor (never on the event loop) and
        # the sweep enforces its own wall-clock budget per pass, resuming on the
        # next tick. Cancelled on shutdown with the loop.
        boot_reclaim = asyncio.create_task(self._sweep_sandbox_artifacts())
        try:
            await self._run_cleanup_ticks(interval)
        finally:
            boot_reclaim.cancel()

    async def _run_cleanup_ticks(self, interval: float) -> None:
        # The sleep is chopped into short waits so a config write that shortens
        # ``session.timeout_secs`` is felt within one refresh cadence, not after
        # the old (possibly much longer) interval has run out. Each wake re-reads
        # the policy; when the interval moves, the next sweep is re-anchored to
        # the LAST sweep plus the new interval, so a lowered timeout can pull the
        # sweep forward and a raised one pushes it out.
        # Time is accounted from the waits this loop issued (a wait that timed
        # out slept for its timeout), not from the wall clock, so the cadence
        # is a property of the loop alone and a test can collapse the waits.
        remaining = interval
        while not self._deps.get_shutdown_signal().is_set():
            wait = min(max(remaining, 0.0), self.POLICY_REFRESH_SECS)
            try:
                await asyncio.wait_for(self._deps.get_shutdown_signal().wait(), timeout=wait)
                return
            except asyncio.TimeoutError:
                pass
            remaining -= wait

            fresh = self._adopt_idle_policy()
            if fresh != interval:
                remaining, interval = remaining - interval + fresh, fresh
            if remaining > 0:
                continue
            remaining = interval

            # Resolve through the facade so replacing the manager watchdog after
            # construction continues to affect the live cleanup task.
            await self._owner._watchdog.tick()
            await self._sweep_session_roots()
            await self._sweep_sandbox_artifacts()
            await self._sweep_session_pid_mappings()
            await self._sweep_member_pid_bindings()
            await self._maybe_prune_pycache()
            await self._sweep_periodic_pids()
            await self._sweep_untracked_mcps()

    async def _sweep_session_roots(self) -> None:
        try:
            roots_killed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_subprocess_executor(),
                self._deps.cleanup_orphaned_session_roots,
            )
            if roots_killed:
                self._deps.logger.info(
                    "Periodic sweep: cleaned %d orphaned session root processes",
                    roots_killed,
                )
        except Exception:
            pass

    async def _sweep_sandbox_artifacts(self) -> None:
        try:
            sandbox_removed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.cleanup_stale_sandbox_profiles,
            )
            if sandbox_removed:
                self._deps.logger.info(
                    "Periodic sweep: removed %d stale sandbox artifacts",
                    sandbox_removed,
                )
        except Exception as exc:
            self._deps.logger.debug(
                "sandbox launcher sweep failed: %s",
                type(exc).__name__,
            )

    async def _sweep_session_pid_mappings(self) -> None:
        """Retract ``session_pid_<pid>`` mappings whose pid is dead or names no process.

        Those two are the whole of what the pass removes: an unsignalable pid, or
        one absent from the thread-group-leaders snapshot whose per-pid re-read
        confirms it names no process. A pid that is LIVE keeps its mapping even
        when the number has been recycled away from the session that published
        it -- that mapping is already refused at read time by
        ``session_pid_sig._pid_recycled`` on both the strict and the lenient
        resolution path, so it carries no identity, and its file goes when the
        unrelated process exits.

        ``session_pid._prune_stale_session_pid_files`` is otherwise reached only
        from ``cleanup_orphaned_sessions``, which ``session.py``'s module map
        records as startup + shutdown, so a gateway that keeps running holds
        every mapping it publishes until it restarts — one per released session,
        and a recycled pid number goes on carrying a dashboard slot's key. The
        pass itself needs nothing added; it needs a caller on a bounded cadence,
        which is what :data:`MAX_TICK_INTERVAL_SECS` makes this one.

        The cadence lives here rather than on a provider teardown deliberately,
        even though a teardown is the moment a runtime is known dead.
        ``_untrack_session_pid`` is synchronous and ``AcpClient._reset_state``
        calls it on the event loop, so a mapping read or unlink placed on that
        stack is filesystem work on the loop over predictable same-uid
        agent-writable paths — the whole of
        ``no-blocking-call-on-event-loop``, where a planted FIFO or reparse
        point parks the gateway. On the maintenance executor the same pass needs
        no per-syscall bounding at all, and the one case a teardown hook cannot
        reach — a gateway that dies without running one — this pass covers too.
        The cost is latency: a released session's mapping survives at most one
        tick rather than vanishing with its process.
        """
        try:
            removed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.prune_session_pid_mappings,
            )
            if removed:
                self._deps.logger.info(
                    "Periodic sweep: retracted %d stale session pid mappings",
                    removed,
                )
        except Exception as exc:
            self._deps.logger.debug(
                "session pid mapping sweep failed: %s",
                type(exc).__name__,
            )

    async def _sweep_member_pid_bindings(self) -> None:
        """Collect aged per-pid member-memory binding records.

        A removed routing path wrote one record per agent process and deleted
        none, and nothing else sweeps them, so they accumulate for the install's
        life (165,975 files measured on an operator host). The pass itself is in
        ``member_memory_auth.prune_legacy_member_pid_bindings``; it needs a caller
        on a bounded cadence, which is what this one is. Blocking unlinks, so it
        runs on the maintenance executor for the same reason the session-pid
        mapping sweep does: the directory is same-uid agent-writable, and
        filesystem work over such a path on the event loop parks the gateway.
        """
        try:
            removed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.prune_member_pid_bindings,
            )
            if removed:
                self._deps.logger.info(
                    "Periodic sweep: pruned %d stale member-memory pid binding(s)",
                    removed,
                )
        except Exception as exc:
            self._deps.logger.debug(
                "member-memory pid binding sweep failed: %s",
                type(exc).__name__,
            )

    async def _maybe_prune_pycache(self) -> None:
        now = self._deps.monotonic()
        last = self.state.last_pycache_gc
        if last is not None and now - last < self._deps.get_pycache_gc_interval_secs():
            return

        # Stamp before the walk so a failed prune retries at the bounded GC
        # cadence, not on every cleanup tick.
        self.state.last_pycache_gc = now
        try:
            removed, freed = await asyncio.get_running_loop().run_in_executor(
                self._deps.get_maintenance_executor(),
                self._deps.prune_pycache,
            )
            if removed:
                self._deps.logger.info(
                    "Periodic sweep: pruned %d bytecode-cache files (%d MiB)",
                    removed,
                    freed // (1024 * 1024),
                )
        except Exception as exc:
            self._deps.logger.debug(
                "bytecode-cache GC failed: %s",
                type(exc).__name__,
            )

    def _active_pids(self) -> tuple[set[int], bool]:
        active_pids, safe = self._deps.collect_active_pids(self._owner._sessions)
        active_pids.update(self._owner._pool_pids())
        active_pids.update(self._owner._in_flight_pids())
        active_pids.update(self._owner._companion_runtime_pids())
        return active_pids, safe

    async def _sweep_periodic_pids(self) -> None:
        try:
            active_pids, safe = self._active_pids()
            if not safe:
                return

            gateway_pid = self._deps.getpid()
            # Identification and persistent-file I/O stay off the event loop.
            # Phase one never kills; phase two revalidates against a fresh active
            # set and fails closed if PID extraction is unreliable.
            killed_or_dead, candidates = await asyncio.to_thread(
                self._deps.periodic_pid_sweep,
                gateway_pid,
                active_pids,
            )
            confirmed: list[int] = []
            if candidates:
                current_pids, phase2_safe = self._active_pids()
                if phase2_safe:
                    confirmed = [pid for pid in candidates if pid not in current_pids]
            if confirmed or killed_or_dead:
                orphan_killed = await asyncio.to_thread(
                    self._deps.kill_confirmed_and_writeback,
                    gateway_pid,
                    confirmed,
                    killed_or_dead,
                )
                if orphan_killed:
                    self._deps.logger.warning(
                        "Periodic sweep: killed %d orphaned kiro-cli processes",
                        orphan_killed,
                    )
        except Exception:
            self._deps.logger.debug("Orphan PID sweep failed", exc_info=True)

    async def _sweep_untracked_mcps(self) -> None:
        try:
            sweep_pids, sweep_safe = self._active_pids()
            if sweep_safe:
                candidates = await asyncio.get_running_loop().run_in_executor(
                    self._deps.get_maintenance_executor(),
                    self._deps.find_orphan_mcp_candidates,
                    sweep_pids,
                )
                if candidates:
                    fresh_pids, fresh_safe = self._active_pids()
                    if fresh_safe:
                        confirmed = [pid for pid in candidates if pid not in fresh_pids]
                        if confirmed:
                            await asyncio.get_running_loop().run_in_executor(
                                self._deps.get_maintenance_executor(),
                                self._deps.kill_orphan_mcps,
                                confirmed,
                            )
                    else:
                        self._deps.logger.warning(
                            "Orphan MCP sweep skipped kill phase: fresh "
                            "active-PID re-verification unreliable "
                            "(fresh_ok=False)"
                        )
            else:
                self._deps.logger.warning(
                    "Orphan MCP sweep skipped: active-PID enumeration "
                    "unreliable (sweep_ok=False)"
                )
        except Exception:
            self._deps.logger.warning("Orphan MCP sweep failed", exc_info=True)

    def set_active_dashboard_slots(self, slot_keys: set[str]) -> None:
        """Adopt the dashboard's open-slot set as the live-owner authority.

        Publishing also RECORDS the keys as slot-owned, which is what later lets
        the sweep tell a session whose owner is gone from one that never had an
        owner. Recorded on publish rather than at session creation because this
        is the one call that already knows the answer.
        """
        live = set(slot_keys)
        self.state.active_dashboard_slots = live
        # Record slot-owned keys ONLY. A channel conversation is owned by its
        # channel, not by the tab that views it: a dashboard slot opened on a
        # Slack thread publishes the THREAD's own key, so dismissing that tab
        # says nothing about whether the thread is finished. Recording those
        # keys would put a live conversation on the terminated-owner axis and
        # tear its runtime down mid-thread. The sweep already skips
        # ``channel:``-prefixed keys for this reason; the other channel
        # namespaces (``slack:`` and its siblings) do not carry that prefix,
        # so the exclusion has to be spelled here as well.
        self.state.slot_owned_keys |= {k for k in live if not is_channel_session_key(k)}
        # Bound the record HERE as well as in the sweep, because the sweep is
        # not guaranteed to run: ``session.timeout_secs=0`` sets
        # ``idle_sweep_enabled`` false, and the prune lives inside the sweep, so
        # the record would then grow with every key ever published and nothing
        # would ever shrink it. Same expression as the sweep's prune -- a key is
        # worth remembering only while it has a live session or an open slot --
        # which makes the two idempotent rather than two policies. Reading the
        # session map unlocked is sound here: this method is synchronous and
        # every caller reaches it on the event loop, so no await can interleave.
        self.state.slot_owned_keys &= set(self._owner._sessions) | live

    def _owner_is_gone(self, key: str) -> bool:
        """Whether *key*'s owning dashboard slot existed and is now closed.

        Two populations answer yes, to deliberately different questions. A
        ``dashboard:`` key is owned by a slot by construction, so the live set
        alone settles it; that is the long-standing behaviour and is unchanged.
        Any OTHER key is owned by a slot only if one claimed it -- a
        channel-born slot contributes its channel key, a linked slot its
        ``linked_session_key`` -- so it must have appeared in a published live
        set before its absence from that set means anything. Without that
        record, absence is equally true of a cron fire, a task step or a hook
        that is running right now and never had a tab.

        False while no live set has been published at all, so a build with no
        dashboard never reaps on this axis.
        """
        live = self.state.active_dashboard_slots
        if live is None or key in live:
            return False
        return key.startswith("dashboard:") or key in self.state.slot_owned_keys

    async def _expire_idle(self, timeout_secs: int) -> None:
        now = self._deps.monotonic()
        # The session object travels with its key: every judgement below is
        # about THAT incarnation, and the reset must not land on a different one
        # that took the key while this sweep was suspended.
        expired: list[tuple[str, bool, SessionEntry]] = []
        total_checked = 0
        persistent_keys = self._deps.get_persistent_keys()
        channel_prefix = self._deps.get_channel_prefix()
        async with self._owner._lock:
            for key, session in self._owner._sessions.items():
                if key in persistent_keys or key.startswith(channel_prefix):
                    continue
                total_checked += 1
                if session.semaphore.locked():
                    continue
                idle = now - session.last_used > timeout_secs
                orphaned = self._owner_is_gone(key)
                if idle or orphaned:
                    expired.append((key, orphaned, session))
            # A key is only worth remembering while the thing it describes still
            # exists, which bounds the record by two finite sets rather than by
            # uptime: the live session map, and the open slots themselves.
            #
            # The second half is load-bearing. A slot publishes its linked key
            # as soon as it opens, which can be BEFORE any turn has created that
            # session, so a prune against the session map alone forgets the
            # record while the slot is still open. The session appears on the
            # first turn, the tab then closes, and nothing marks it as ever
            # having had an owner -- it would sit outside this axis and wait out
            # the idle clock. A key kept only because it is in the live set
            # cannot be reaped while it stays there, since _owner_is_gone
            # requires absence from that same set.
            live_now = self.state.active_dashboard_slots or set()
            self.state.slot_owned_keys &= set(self._owner._sessions) | live_now

        if expired:
            self._deps.logger.warning(
                "Idle sweep: %d checked, %d expired",
                total_checked,
                len(expired),
            )
        elif total_checked:
            self._deps.logger.debug("Idle sweep: %d checked, 0 expired", total_checked)

        for key, is_orphan, scanned in expired:
            # BOTH axes, before the split. A turn already committed to this
            # session must not be reset under it, and the axis that found the
            # session says nothing about that: the idle clock can elect a
            # never-tabbed ``cron:`` parent whose ``last_used`` went stale during
            # a long sub-agent run, exactly while that sub-agent's completion
            # injection is suspended on its store read. Cheap, synchronous, and
            # for the idle branch this is also the last read before its reset.
            if self._injection_pending(key):
                self._deps.logger.info(
                    "Idle sweep: %s has a completion injection in flight - left running",
                    key,
                )
                continue
            if is_orphan:
                # A closed tab is not the same as finished work. With session
                # sharing on, sub-agents run on the parent's runtime after the
                # parent's own turn ends, so the busy semaphore cannot see them
                # and the probe is the only witness. Fail-closed: a probe that
                # cannot answer keeps the session.
                if await self._has_attached_subagents(key):
                    self._deps.logger.info(
                        "Idle sweep: %s still has sub-agent work - left running",
                        key,
                    )
                    continue
                # Ask the injection counter AGAIN, here. The read above happened
                # before the sub-agent probe suspended, and an injection that
                # STARTS inside that await would be invisible to it -- which is
                # the closed-tab case this guard exists for. Everything from here
                # to ``reset`` is synchronous.
                if self._injection_pending(key):
                    self._deps.logger.info(
                        "Idle sweep: %s began an injection mid-sweep - left running",
                        key,
                    )
                    continue
                # Re-ask against the CURRENT live set, not the one the scan
                # read. Two awaits stand between them: the scan drops the lock,
                # and the probe above reads the task store off-loop. A slot can
                # reopen in either window, and reaping on the stale answer is
                # how a session the user just resumed loses its runtime.
                #
                # Position is the point. This must be the LAST read of the live
                # set before the act, so it sits AFTER the probe and nothing
                # between it and ``reset`` may suspend -- the counter, the
                # turn-active read and the expire callback below are all
                # synchronous. Asking any earlier reopens the window it closes.
                if not self._owner_is_gone(key):
                    self._deps.logger.info(
                        "Idle sweep: %s regained its slot before reset - left running",
                        key,
                    )
                    continue
                self._deps.logger.warning(
                    "Expiring orphaned session (slot gone): %s",
                    key,
                )
            else:
                self._deps.logger.warning("Expiring idle session: %s", key)

            # Preserve the historical attempt counter: it increments before the
            # race-safe reset, while the hang-resilience counter below is gated
            # on reset actually succeeding.
            self._deps.stats_factory().inc_session_cleaned()
            try:
                entry = self._owner._sessions.get(key)
                provider = getattr(entry, "provider", None)
                turn_active = provider is not None and self._deps.provider_has_active_turn(provider)
            except Exception:
                turn_active = False

            if self._owner.on_session_expire:
                try:
                    self._deps.sel_factory().log_api_access(
                        caller="session_manager",
                        operation="consolidate_session_expire",
                        outcome="allowed",
                        source="idle_sweep",
                        resources=key,
                    )
                    self._owner.on_session_expire(key)
                except Exception:
                    self._deps.logger.debug(
                        "on_session_expire (or SEL) failed for %s",
                        key,
                        exc_info=True,
                    )

            # Release the record together with the incarnation it describes, but
            # ONLY when the owner is gone. The prune above runs during the scan,
            # before any reset pops a key, so a record that survives an orphan
            # reap outlives its session: a cron fire or task step arriving under
            # the SAME key before the next scan is present at prune time,
            # inherits the record, and is then judged orphaned even though this
            # incarnation never had a tab. Keying by string is what makes that
            # inheritance possible.
            #
            # An IDLE reap is the opposite case and must keep the record. There
            # the slot is typically still open -- that is why the owner-gone
            # test said no -- and the record describes the SLOT's claim, which
            # outlives any one session under it. Releasing it here would leave
            # the slot's next session unrecorded, so closing that tab later
            # would not reap it and it would wait out the idle clock instead.
            #
            # Discard BEFORE the reset rather than after it succeeds. A publish
            # landing inside reset's await re-adds the key, which is the right
            # answer when a slot reopens for it; a discard placed after would
            # erase that fresh record instead. set.discard is synchronous, so
            # it does not reopen the window the re-assert above closes.
            if is_orphan:
                self.state.slot_owned_keys.discard(key)

            # Pin the reset to the incarnation this sweep actually judged, on the
            # orphan path. Every test above -- idle or orphaned, the probe, the
            # live-set re-assert -- was asked about ``scanned``, and two awaits
            # separate the first of them from here, so the key may by now hold a
            # DIFFERENT session: a fire under the same cron key, or a tab
            # reopened and a turn taken. ``reset`` revalidates identity under its
            # own lock and declines on a mismatch, so a fresh incarnation keeps
            # its runtime instead of inheriting a verdict about its predecessor.
            #
            # The idle path is spelled separately rather than passing
            # ``expect_session=None``, so it keeps asking nothing about identity:
            # that axis is not what this change touches. Both paths DO pass
            # ``skip_if_injecting``, because both reach ``reset`` and ``reset``
            # suspends on the registry lock, so on either one an injection can
            # begin after this sweep's own read and before the pop.
            if is_orphan:
                reset_done = await self._owner.reset(
                    key,
                    expect_session=scanned,
                    skip_if_busy=True,
                    skip_if_injecting=True,
                )
            else:
                # Expiry recycles a PROCESS; the conversation survives on disk and
                # resumes through ``session/load``. So this is not a parent end, and the
                # session's in-flight sub-agent runs are left alone -- they have a
                # conversation to deliver into, and their own run timeout bounds them.
                # That is why neither call here passes ``ends_conversation``.
                reset_done = await self._owner.reset(key, skip_if_busy=True, skip_if_injecting=True)
            if not reset_done:
                # The release above ran BEFORE the reset, so a reset that
                # DECLINED leaves the record stripped from a session that is
                # still registered. Put it back. Without this the key leaves
                # the terminated-owner axis for good: while its tab stays
                # closed it is absent from the live set, carries no
                # ``dashboard:`` prefix, and nothing else re-adds it, so it
                # would hold its runtime and its per-session MCP servers until
                # the idle clock -- the cost this axis exists to cut short, for
                # a session that survived only because it was momentarily busy.
                #
                # Conditioned on ``is_orphan`` because only that path released
                # the record: adding a key here on the idle path could CREATE a
                # record for a session no slot ever claimed, which is exactly
                # what puts a cron fire or task step on this axis wrongly. Also
                # conditioned on the SAME session still holding the key, which
                # covers both reasons a reset declines. Busy: the session the
                # release described is still there, so the claim is still its
                # claim and goes back. Replaced: some other incarnation took the
                # key, and handing IT the record would grant a claim it never
                # made -- the inheritance this release exists to prevent. A
                # publish that re-added the key inside the await is unaffected,
                # since set.add is idempotent; that is the reopened-slot case.
                if is_orphan and self._owner._sessions.get(key) is scanned:
                    self.state.slot_owned_keys.add(key)
                self._deps.logger.info(
                    "Idle sweep: %s not reset (busy, or the key changed hands) — left running",
                    key,
                )
            else:
                self._deps.emit_counter(
                    self._deps.get_session_idle_expired_event(),
                    {"turn_active": turn_active, "orphaned": bool(is_orphan)},
                )
