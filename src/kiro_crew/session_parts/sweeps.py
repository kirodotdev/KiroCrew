"""The periodic cleanup loop's wiring and its watchdog hooks.

Process Sweep Architecture
--------------------------
Four mechanisms clean up processes. They are complementary — not redundant.

1. ``cleanup_orphaned_sessions()`` — **startup + shutdown only**.
   Reads ``kiro_session_pids.txt`` (bare sandbox root PIDs from the previous
   gateway run). Validates each with ``_is_managed_agent_process``, kills descendants
   bottom-up, then kills the root. Truncates the file afterward.
   *Cannot be replaced by the periodic sweep* because sandbox roots are
   independent processes with no idle timeout — they survive indefinitely
   unless explicitly killed.

2. ``_cleanup_orphaned_mcp_servers()`` — **periodic** (every ~5 min).
   Reads ``kiro_pids.txt`` (child:parent[:start-id] entries). Kills children whose parent
   is confirmed dead. PPid-based reuse guard prevents killing recycled PIDs.
   Also prunes dead bare PIDs. *Depends on (1)* — children are only orphaned
   after their sandbox root is killed.

3. ``_expire_idle()`` — **periodic** (every ~5 min).
   Kills sessions idle for >``timeout_secs`` (default 60 min) via
   ``reset()`` → ``provider.shutdown()`` → SIGKILL process tree.
   Protected keys: ``_PERSISTENT_KEYS`` (``_bg`` and ``_hb``).
   Also expires on a second, clock-independent axis: the session's owning
   dashboard slot is gone (``_owner_is_gone``). A ``dashboard:`` key is
   slot-owned by construction; a key of any other shape counts as slot-owned
   only if a published live set once carried it, which is what keeps a
   ``cron:`` fire or a ``taskrunner:{id}:task{n}`` step that never had a tab
   from being read as finished. BOTH axes ask the sub-agent probe and refuse a
   session with attached sub-agent work; only the orphan axis then re-asserts
   against the live set as the last read before the reset, with no await in
   between. BOTH axes also refuse a session with a completion injection in
   flight: a turn already committed to a session is not finished work,
   whichever test elected it. *Cannot be replaced by the idle clock* — a finished session holds its
   runtime and its per-session MCP servers for the whole timeout, so the live
   process count is the number of unreaped sessions times the servers each one
   spawns.
   Both axes also refuse a session whose turn permit is held: ``_expire_idle``
   tests ``session.semaphore.locked()`` and skips before it reads the clock at
   all, and each reset it does elect is asked to skip a busy session again. That
   guard is what bounds how coarse ``last_used`` is allowed to be. The clock is
   advanced by the allocation paths and by ``touch()``, not on every LLM
   round-trip, so a task runner step working continuously for longer than
   ``timeout_secs`` does carry a stale timestamp; it survives regardless,
   because it holds its permit for the whole step. The clock therefore measures
   one thing: how long a session with no turn in flight has been quiet.

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_cleanup``, ``_injection_probe``, ``_subagent_probe``.

Instance fields read: none.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from kiro_crew import platform_compat, shutdown_event
from kiro_crew.config.paths import config_dir
from kiro_crew.executors import maintenance_executor, subprocess_executor
from kiro_crew.member_memory_auth import prune_legacy_member_pid_bindings
from kiro_crew.metrics.events import SESSION_IDLE_EXPIRED, emit_counter
from kiro_crew.pycache_gc import PYCACHE_GC_INTERVAL_SECS, prune_pycache
from kiro_crew.sandbox import cleanup_stale_sandbox_profiles
from kiro_crew.sel import sel
from kiro_crew.session_cleanup import CleanupDeps, SessionCleanup
from kiro_crew.session_parts.policy import (
    _CHANNEL_PREFIX,
    _PERSISTENT_KEYS,
    _STUCK_TURN_REPORT_SECS,
)
from kiro_crew.session_parts.provider_probes import (
    _provider_background_launch,
    _provider_has_active_turn,
)
from kiro_crew.session_pid import (
    _build_child_map,
    _cleanup_orphaned_mcp_servers,
    _collect_active_pids,
    _kill_confirmed_and_writeback,
    _periodic_pid_sweep,
    _prune_stale_session_pid_files,
    _rss_mb_from_tree,
    cleanup_orphaned_session_roots,
    find_orphan_mcp_candidates,
    get_session_rss_mb,
    kill_orphan_mcps,
)
from kiro_crew.session_scope_reap import reap_abandoned_agent_scopes
from kiro_crew.shell_audit_log import rotate_shell_audit_log
from kiro_crew.stats import Stats
from kiro_crew.watchdog import CleanupHook, SessionWatchdog

if TYPE_CHECKING:
    from kiro_crew.session import SessionManager

#: The session manager's own logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.session")


def runtime_reconciler(self: SessionManager) -> Any:
    """The retained runtime reconciler, or ``None`` before the first cleanup tick."""
    return self._cleanup_state_boundary().runtime_reconciler


def _cleanup_deps(self: SessionManager) -> CleanupDeps:
    # Resolved HERE, on the thread that builds the deps, and carried into the
    # sandbox sweep. That sweep runs on the maintenance pool, and a path a pool
    # thread resolves for itself is resolved whenever the thread gets scheduled:
    # under the test suite that is routinely after the test that queued it has
    # dropped its KIROCREW_HOME pin, so the sweep walked the operator's real
    # ~/.kiro/crew. The data home does not move during a gateway's life, so
    # resolving it once at construction loses nothing.
    data_home = config_dir()
    return CleanupDeps(
        logger=logger,
        get_shutdown_signal=lambda: shutdown_event,
        get_maintenance_executor=lambda: maintenance_executor(),
        get_subprocess_executor=lambda: subprocess_executor(),
        cleanup_orphaned_mcp_servers=lambda: _cleanup_orphaned_mcp_servers(),
        cleanup_orphaned_session_roots=lambda: cleanup_orphaned_session_roots(),
        cleanup_stale_sandbox_profiles=lambda: cleanup_stale_sandbox_profiles(data_home=data_home),
        prune_session_pid_mappings=lambda: _prune_stale_session_pid_files(),
        prune_member_pid_bindings=lambda: prune_legacy_member_pid_bindings(),
        # Same resolved home as the sandbox sweep, for the same reason: the
        # step runs on the maintenance pool, and the hook's own expansion of
        # ``${KIROCREW_HOME:-$HOME/.kiro/crew}`` is this path.
        rotate_shell_audit_log=lambda: rotate_shell_audit_log(data_home),
        prune_pycache=lambda: prune_pycache(),
        collect_active_pids=lambda sessions: _collect_active_pids(cast(dict[Any, Any], sessions)),
        periodic_pid_sweep=lambda gateway_pid, active_pids: _periodic_pid_sweep(
            gateway_pid,
            active_pids,
        ),
        kill_confirmed_and_writeback=lambda gateway_pid, candidates, dead: (
            _kill_confirmed_and_writeback(gateway_pid, candidates, dead)
        ),
        find_orphan_mcp_candidates=lambda active_pids: find_orphan_mcp_candidates(active_pids),
        kill_orphan_mcps=lambda candidates: kill_orphan_mcps(candidates),
        reap_agent_scopes=lambda active_pids: reap_abandoned_agent_scopes(active_pids),
        build_child_map=lambda: _build_child_map(),
        rss_mb_from_tree=lambda pid, child_map: _rss_mb_from_tree(pid, child_map),
        get_session_rss_mb=lambda pid: get_session_rss_mb(pid),
        is_windows=lambda: platform_compat.IS_WINDOWS,
        getpid=lambda: os.getpid(),
        monotonic=lambda: time.monotonic(),
        stats_factory=lambda: Stats(),
        sel_factory=lambda: sel(),
        provider_has_active_turn=lambda provider: _provider_has_active_turn(provider),
        emit_counter=lambda event, dimensions: emit_counter(event, dimensions),
        get_persistent_keys=lambda: _PERSISTENT_KEYS,
        provider_background_launch=lambda provider: _provider_background_launch(provider),
        get_channel_prefix=lambda: _CHANNEL_PREFIX,
        get_stuck_turn_report_secs=lambda: _STUCK_TURN_REPORT_SECS,
        get_pycache_gc_interval_secs=lambda: PYCACHE_GC_INTERVAL_SECS,
        get_session_idle_expired_event=lambda: SESSION_IDLE_EXPIRED,
        # Resolved per call, not captured: the dashboard installs the probe
        # after this manager (and possibly its cleanup boundary) exists.
        has_attached_subagents=lambda key: self._has_attached_subagents(key),
        # Same reason, different owner: the gateway installs this one.
        has_pending_injection=lambda key: self._has_pending_injection(key),
    )


def _cleanup_boundary(self: SessionManager) -> SessionCleanup:
    service = self.__dict__.get("_cleanup")
    if service is None:
        state = self._cleanup_state_boundary()
        if state.watchdog is None:
            # Construct through this module's patchable names and bind the
            # facade methods so instance monkeypatches remain observable.
            state.watchdog = SessionWatchdog(
                [
                    CleanupHook("idle_expiry", self._expire_idle_hook),
                    CleanupHook("orphan_mcp", self._orphan_mcp_hook),
                    CleanupHook("reap_agent_scopes", self._reap_agent_scopes_hook),
                    CleanupHook("rss_threshold", self._rss_threshold_check),
                    CleanupHook("stuck_turn", self._stuck_turn_check),
                    CleanupHook("bg_drain_reap", self._bg_drain_reap_hook),
                ]
            )
        service = SessionCleanup(
            cast(Any, self),
            self._cleanup_deps(),
            state=state,
        )
        self.__dict__["_cleanup"] = service
    return service


def _ensure_cleanup_task(self: SessionManager) -> None:
    """Start the one cleanup loop at the allocation registration point."""
    self._cleanup_boundary().start_cleanup()


def set_subagent_probe(
    self: SessionManager, fn: "Callable[[str], bool | Awaitable[bool]] | None"
) -> None:
    """Install the "does *key* have sub-agent work attached?" predicate.

    The RSS ceiling consults it before recycling an idle session: with
    session sharing on, a parent's sub-agents run on the parent's runtime
    after its own turn ends, so the busy semaphore alone cannot see them.
    ``None`` uninstalls the probe (no dashboard, no children).
    """
    self._subagent_probe = fn


def _has_attached_subagents(self: SessionManager, key: str) -> bool | Awaitable[bool]:
    """Answer the installed sub-agent probe, or False when none is installed.

    The probe's answer is handed back UNCOERCED: the dashboard installs a
    coroutine probe (its queued half reads the task store, and the sweep
    that asks is on the gateway loop), and ``bool()`` of a coroutine is True
    for every session while never running the probe at all. The cleanup
    boundary awaits an awaitable answer and coerces there.

    A raising probe propagates: the cleanup boundary treats that as
    "attached" so the session is kept.
    """
    probe = self._subagent_probe
    if probe is None:
        return False
    return probe(key)


def set_injection_probe(self: SessionManager, fn: "Callable[[str], bool] | None") -> None:
    """Install the "is a completion injection in flight for *key*?" predicate.

    Cleanup consults it before expiring a session: the gateway commits a
    turn to a parent session, then awaits that turn's store read before
    acquiring the session, so for the length of that await the session looks
    idle to every other signal. ``None`` uninstalls it (no gateway, nothing
    injecting). Synchronous by contract -- the gateway answers from a
    counter, and cleanup asks it in the last breath before ``reset``.
    """
    self._injection_probe = fn


def _has_pending_injection(self: SessionManager, key: str) -> bool:
    """Answer the installed injection probe, or False when none is installed.

    Coerced to ``bool`` here, unlike the sub-agent probe: this predicate is
    synchronous by contract, so there is no awaitable to hand onward.

    A raising probe propagates: the cleanup boundary treats that as
    "attached" so the session is kept.
    """
    probe = self._injection_probe
    if probe is None:
        return False
    return bool(probe(key))


# ── Idle cleanup ──

# ── Watchdog hooks ──
# Each hook is the execution half of a CleanupHook (see watchdog.py). Each
# one reproduces the exact try/except of the inline cleanup-loop block it
# was lifted from, so SessionWatchdog.tick() can stay a dumb dispatcher and
# the move is behaviour-preserving (no severity promotion of swallowed
# errors). The orphan-PID sweep is deliberately NOT a hook in CR 1.


async def _expire_idle_hook(self: SessionManager) -> None:
    """Run the configured idle-expiry cleanup hook."""
    await self._cleanup_boundary()._expire_idle_hook()


async def _bg_drain_reap_hook(self: SessionManager) -> None:
    """Reap drained background runtimes during a cleanup tick."""
    await self._cleanup_boundary()._bg_drain_reap_hook()


async def _orphan_mcp_hook(self: SessionManager) -> None:
    """Run the legacy orphan-MCP cleanup hook."""
    await self._cleanup_boundary()._orphan_mcp_hook()


async def _reap_agent_scopes_hook(self: SessionManager) -> None:
    """Reclaim abandoned agent cgroup scopes during a cleanup tick."""
    await self._cleanup_boundary()._reap_agent_scopes_hook()


async def _rss_threshold_check(self: SessionManager) -> None:
    """Recycle idle sessions whose process trees exceed the RSS policy."""
    await self._cleanup_boundary()._rss_threshold_check()


async def _stuck_turn_check(self: SessionManager) -> None:
    """Report newly observed parked turn consumers."""
    await self._cleanup_boundary()._stuck_turn_check()


async def _cleanup_loop(self: SessionManager) -> None:
    """Run periodic watchdog and process-cleanup policy until shutdown."""
    await self._cleanup_boundary()._cleanup_loop()


def set_active_dashboard_slots(self: SessionManager, slot_keys: set[str]) -> None:
    """Publish the live dashboard slot set for orphan-session expiry."""
    self._cleanup_boundary().set_active_dashboard_slots(slot_keys)


async def _expire_idle(self: SessionManager, timeout_secs: int) -> None:
    """Expire idle and orphaned sessions using the cleanup policy."""
    await self._cleanup_boundary()._expire_idle(timeout_secs)
