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

from kiro_crew.watchdog import SessionWatchdog

if TYPE_CHECKING:
    # Type-only: importing providers.base from this leaf at runtime enters the
    # providers -> acp package -> runtime -> session_pid -> providers cycle.
    from kiro_crew.providers.base import LLMProvider
else:
    LLMProvider = Any


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

    def get_pid(self, key: str) -> int | None: ...

    async def reset(
        self,
        key: str,
        *,
        expect_session: SessionEntry | None = None,
        skip_if_busy: bool = False,
        clear_conversation: bool = False,
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


@dataclass(slots=True)
class CleanupState:
    """Mutable state exclusively owned by :class:`SessionCleanup`."""

    cleanup_task: asyncio.Task[Any] | None = None
    rss_max_mb: int = 0
    idle_sweep_enabled: bool = False
    idle_timeout: int = 0
    stuck_reported: dict[str, float] = field(default_factory=dict)
    last_pycache_gc: float | None = None
    active_dashboard_slots: set[str] | None = None
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
    prune_pycache: Callable[[], tuple[int, int]]
    collect_active_pids: ActivePidCollector
    periodic_pid_sweep: PeriodicPidSweep
    kill_confirmed_and_writeback: PidWriteback
    find_orphan_mcp_candidates: Callable[[set[int]], list[int]]
    kill_orphan_mcps: Callable[[list[int]], int]
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


class SessionCleanup:
    """Coordinate cleanup hooks, sweeps, and idle-expiry policy."""

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
        # Avoid the runtime lock on the common empty-list path.  Parked runtimes
        # otherwise remain PID-shielded forever on an idle gateway.
        if not self._owner._draining_bg_runtimes:
            return
        try:
            async with self._owner._bg_runtime_lock:
                await self._owner._reap_drained_bg_runtimes_locked()
        except Exception:
            self._deps.logger.warning(
                "bg_drain_reap hook failed; will retry next tick",
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
            # This sweep historically treats failures as a silent best-effort
            # miss.  The watchdog must not promote the severity.
            pass

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
                # reset revalidates both object identity and the busy semaphore
                # under its own lock after the unlocked RSS measurement.
                recycled = await self._owner.reset(
                    key,
                    expect_session=session,
                    skip_if_busy=True,
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

    async def _cleanup_loop(self) -> None:
        timeout = self._owner._cfg.session.timeout_secs
        if 0 < timeout < 60:
            self._deps.logger.warning(
                "session.timeout_secs=%d is below minimum 60; clamping to 60",
                timeout,
            )
            timeout = 60
        idle_sweep_enabled = timeout > 0
        if not idle_sweep_enabled:
            self._deps.logger.info(
                "Idle session sweep disabled (session.timeout_secs=%d); "
                "MCP/PID sweeps still run at default cadence",
                timeout,
            )
        self.state.idle_sweep_enabled = idle_sweep_enabled
        self.state.idle_timeout = timeout
        interval = max(timeout // 6, 60) if idle_sweep_enabled else 300

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
        while not self._deps.get_shutdown_signal().is_set():
            try:
                await asyncio.wait_for(
                    self._deps.get_shutdown_signal().wait(),
                    timeout=interval,
                )
                return
            except asyncio.TimeoutError:
                pass

            # Resolve through the facade so replacing the manager watchdog after
            # construction continues to affect the live cleanup task.
            await self._owner._watchdog.tick()
            await self._sweep_session_roots()
            await self._sweep_sandbox_artifacts()
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
        self.state.active_dashboard_slots = set(slot_keys)

    async def _expire_idle(self, timeout_secs: int) -> None:
        now = self._deps.monotonic()
        expired: list[tuple[str, bool]] = []
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
                orphaned = (
                    key.startswith("dashboard:")
                    and self.state.active_dashboard_slots is not None
                    and key not in self.state.active_dashboard_slots
                )
                if idle or orphaned:
                    expired.append((key, orphaned))

        if expired:
            self._deps.logger.warning(
                "Idle sweep: %d checked, %d expired",
                total_checked,
                len(expired),
            )
        elif total_checked:
            self._deps.logger.debug("Idle sweep: %d checked, 0 expired", total_checked)

        for key, is_orphan in expired:
            if is_orphan:
                self._deps.logger.warning(
                    "Expiring orphaned dashboard session (slot gone): %s",
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

            if not await self._owner.reset(key, skip_if_busy=True):
                self._deps.logger.info(
                    "Idle sweep: %s became busy before reset — left running",
                    key,
                )
            else:
                self._deps.emit_counter(
                    self._deps.get_session_idle_expired_event(),
                    {"turn_active": turn_active, "orphaned": bool(is_orphan)},
                )
