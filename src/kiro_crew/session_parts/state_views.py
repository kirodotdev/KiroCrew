"""``SessionManager``'s attribute views onto the state its collaborators own.

The registry, lifecycle, cleanup, compaction, warm-pool and background-runtime state
each live on one state object; these properties keep the attribute seams callers and
tests read and write on the manager (``_sessions``, ``_lock``, ``_pool_size``, ...).

Each function here whose first parameter is ``self`` is a ``SessionManager``
member: the class binds it by name (``kiro_crew.session``), so it reads and writes the
manager's own state on the instance, and a patch of the member on the class or
on an instance reaches every caller.

Instance fields written: ``_allocation_state``, ``_cleanup_state``,
``_lifecycle_state``.

Instance fields read: ``_background_runtime``, ``_compaction_state``, ``_pool``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, cast

from kiro_crew.session_allocation import SessionRegistryState
from kiro_crew.session_cleanup import CleanupState
from kiro_crew.session_lifecycle import SessionLifecycleState
from kiro_crew.start_priority import PrioritySemaphore

if TYPE_CHECKING:
    from kiro_crew.messaging.link import ChannelLink
    from kiro_crew.runtime_ownership import PidRefcount
    from kiro_crew.session import AcpRuntime, LLMProvider, SessionManager
    from kiro_crew.session_parts.policy import _CompactCallback, _RecycleCallback
    from kiro_crew.session_parts.record import _Session
    from kiro_crew.watchdog import SessionWatchdog


_T = TypeVar("_T")

if TYPE_CHECKING:

    def StateProperty(
        fget: Callable[[Any], _T], fset: Callable[[Any, _T], None] | None = None
    ) -> _T:
        """Typed as what the getter returns, so ``SessionManager`` meets the owner
        protocols its collaborators declare (``_lock: asyncio.Lock``, ...) as its
        ``@property`` members did; a ``property(fget, fset)`` call types as ``Any``."""

else:

    class StateProperty(property):
        """``property(fget, fset)`` under its own name: each state view below is one.

        At run time it IS a ``property`` (``inspect.getattr_static``, ``create_autospec``
        and the ``__new__`` builds read it as one); only type checkers see it as the
        getter's return type, through the stub above.
        """


def _registry_state(self: SessionManager) -> SessionRegistryState:
    """Return allocation state, lazily supporting focused ``__new__`` tests."""
    state = self.__dict__.get("_allocation_state")
    if state is None:
        state = SessionRegistryState()
        self.__dict__["_allocation_state"] = state
    return state


def _lifecycle_state_boundary(self: SessionManager) -> SessionLifecycleState:
    """Return lifecycle state, including for focused ``__new__`` tests."""
    state = self.__dict__.get("_lifecycle_state")
    if state is None:
        state = SessionLifecycleState()
        self.__dict__["_lifecycle_state"] = state
    return state


def _cleanup_state_boundary(self: SessionManager) -> CleanupState:
    state = self.__dict__.get("_cleanup_state")
    if state is None:
        state = CleanupState()
        self.__dict__["_cleanup_state"] = state
    return state


def _get_cleanup_task(self: SessionManager) -> asyncio.Task[Any] | None:
    return self._cleanup_state_boundary().cleanup_task


def _set_cleanup_task(self: SessionManager, value: asyncio.Task[Any] | None) -> None:
    self._cleanup_state_boundary().cleanup_task = value


_cleanup_task = StateProperty(_get_cleanup_task, _set_cleanup_task)


def _get_rss_max_mb(self: SessionManager) -> int:
    return self._cleanup_state_boundary().rss_max_mb


def _set_rss_max_mb(self: SessionManager, value: int) -> None:
    self._cleanup_state_boundary().rss_max_mb = value


_rss_max_mb = StateProperty(_get_rss_max_mb, _set_rss_max_mb)


def _get_idle_sweep_enabled(self: SessionManager) -> bool:
    return self._cleanup_state_boundary().idle_sweep_enabled


def _set_idle_sweep_enabled(self: SessionManager, value: bool) -> None:
    self._cleanup_state_boundary().idle_sweep_enabled = value


_idle_sweep_enabled = StateProperty(_get_idle_sweep_enabled, _set_idle_sweep_enabled)


def _get_idle_timeout(self: SessionManager) -> int:
    return self._cleanup_state_boundary().idle_timeout


def _set_idle_timeout(self: SessionManager, value: int) -> None:
    self._cleanup_state_boundary().idle_timeout = value


_idle_timeout = StateProperty(_get_idle_timeout, _set_idle_timeout)


def _get_stuck_reported(self: SessionManager) -> dict[str, float]:
    return self._cleanup_state_boundary().stuck_reported


def _set_stuck_reported(self: SessionManager, value: dict[str, float]) -> None:
    self._cleanup_state_boundary().stuck_reported = value


_stuck_reported = StateProperty(_get_stuck_reported, _set_stuck_reported)


def _get_last_pycache_gc(self: SessionManager) -> float | None:
    return self._cleanup_state_boundary().last_pycache_gc


def _set_last_pycache_gc(self: SessionManager, value: float | None) -> None:
    self._cleanup_state_boundary().last_pycache_gc = value


_last_pycache_gc = StateProperty(_get_last_pycache_gc, _set_last_pycache_gc)


def _get_active_dashboard_slots(self: SessionManager) -> set[str] | None:
    return self._cleanup_state_boundary().active_dashboard_slots


def _set_active_dashboard_slots(self: SessionManager, value: set[str] | None) -> None:
    self._cleanup_state_boundary().active_dashboard_slots = value


_active_dashboard_slots = StateProperty(_get_active_dashboard_slots, _set_active_dashboard_slots)


def _get_watchdog(self: SessionManager) -> SessionWatchdog:
    watchdog = self._cleanup_state_boundary().watchdog
    if watchdog is None:
        return self._cleanup_boundary()._watchdog
    return watchdog


def _set_watchdog(self: SessionManager, value: SessionWatchdog) -> None:
    self._cleanup_state_boundary().watchdog = value


_watchdog = StateProperty(_get_watchdog, _set_watchdog)


def _get_identity_sweep_lock(self: SessionManager) -> asyncio.Lock:
    return self._lifecycle_state_boundary().identity_sweep_lock


def _set_identity_sweep_lock(self: SessionManager, value: asyncio.Lock) -> None:
    self._lifecycle_state_boundary().identity_sweep_lock = value


_identity_sweep_lock = StateProperty(_get_identity_sweep_lock, _set_identity_sweep_lock)


def _get_pending_identity_sweep_fingerprint(self: SessionManager) -> str:
    """The account a sweep is still trying to retire sessions FOR, or ``""``.

    Non-empty exactly while an identity sweep stayed incomplete, and cleared
    the moment one completes. Read-only on purpose: the lifecycle service
    owns every write, and a consumer needs this only to answer "is a change
    still outstanding", which the reconciled baseline alone cannot say --
    that baseline deliberately does not advance on an incomplete sweep.
    """
    return self._lifecycle_state_boundary().identity_sweep_fingerprint


pending_identity_sweep_fingerprint = StateProperty(_get_pending_identity_sweep_fingerprint)


def _get_identity_sweep_waiting_on(self: SessionManager) -> tuple[str, ...]:
    """What the last identity sweep could not finish, or ``()`` after a complete one.

    One short label per holdout: a session key tagged ``busy`` or ``channel
    member``, or the runtime that stayed up. Read-only, like the pending
    fingerprint: the lifecycle service owns every write.
    """
    return self._lifecycle_state_boundary().identity_sweep_waiting_on


identity_sweep_waiting_on = StateProperty(_get_identity_sweep_waiting_on)


def _get_recycling(self: SessionManager) -> dict[str, "_Session"]:
    return self._lifecycle_state_boundary().recycling  # type: ignore[return-value]


def _set_recycling(self: SessionManager, value: dict[str, "_Session"]) -> None:
    self._lifecycle_state_boundary().recycling = cast(Any, value)


_recycling = StateProperty(_get_recycling, _set_recycling)


def _get_suppress_replay(self: SessionManager) -> set[str]:
    return self._lifecycle_state_boundary().suppress_replay


def _set_suppress_replay(self: SessionManager, value: set[str]) -> None:
    self._lifecycle_state_boundary().suppress_replay = value


_suppress_replay = StateProperty(_get_suppress_replay, _set_suppress_replay)


def _get_origin_links(self: SessionManager) -> dict[str, ChannelLink]:
    return self._lifecycle_state_boundary().origin_links


def _set_origin_links(self: SessionManager, value: dict[str, ChannelLink]) -> None:
    self._lifecycle_state_boundary().origin_links = value


_origin_links = StateProperty(_get_origin_links, _set_origin_links)


def _get_on_recycled(self: SessionManager) -> _RecycleCallback | None:
    return self._lifecycle_state_boundary().on_recycled


def _set_on_recycled(self: SessionManager, value: _RecycleCallback | None) -> None:
    self._lifecycle_state_boundary().on_recycled = value


_on_recycled = StateProperty(_get_on_recycled, _set_on_recycled)


def _get_sessions(self: SessionManager) -> dict[str, "_Session"]:
    return self._registry_state().sessions


def _set_sessions(self: SessionManager, value: dict[str, "_Session"]) -> None:
    self._registry_state().sessions = value


_sessions = StateProperty(_get_sessions, _set_sessions)


def _get_lock(self: SessionManager) -> asyncio.Lock:
    return self._registry_state().lock


def _set_lock(self: SessionManager, value: asyncio.Lock) -> None:
    self._registry_state().lock = value


_lock = StateProperty(_get_lock, _set_lock)


def _get_closing(self: SessionManager) -> bool:
    return self._registry_state().closing


def _set_closing(self: SessionManager, value: bool) -> None:
    self._registry_state().closing = value


_closing = StateProperty(_get_closing, _set_closing)


def _get_admission_closed(self: SessionManager) -> bool:
    """Return the shared shutdown/update admission gate without yielding.

    Background launchers read this immediately before registering work. On
    the event loop that check-and-register span is atomic with respect to
    ``pause_turn_admission_for_update()``, which sets the same state under
    the registry lock.
    """
    return self._closing


admission_closed = StateProperty(_get_admission_closed)


def _get_final_drain_started(self: SessionManager) -> bool:
    """Whether ``close_all()`` began, so no turn here outlives this process.

    An update pause also closes admission, but it can resume. ``close_all``
    revokes that pause, so closing without an owned pause is the final drain
    of a shutdown or an in-app re-exec restart.
    """
    return self._closing and not self._update_pause_owned


final_drain_started = StateProperty(_get_final_drain_started)


def _get_update_pause_owned(self: SessionManager) -> bool:
    return self._registry_state().update_pause_owned


def _set_update_pause_owned(self: SessionManager, value: bool) -> None:
    self._registry_state().update_pause_owned = value


_update_pause_owned = StateProperty(_get_update_pause_owned, _set_update_pause_owned)


def _get_update_restart_fenced(self: SessionManager) -> bool:
    """Whether refused callbacks must persist inline before imminent re-exec."""
    return self._registry_state().update_restart_fenced


def _set_update_restart_fenced(self: SessionManager, value: bool) -> None:
    self._registry_state().update_restart_fenced = value


update_restart_fenced = StateProperty(_get_update_restart_fenced, _set_update_restart_fenced)


def _get_start_sem(self: SessionManager) -> PrioritySemaphore:
    return self._registry_state().start_sem


def _set_start_sem(self: SessionManager, value: PrioritySemaphore) -> None:
    if not isinstance(value, PrioritySemaphore):
        raise TypeError("_start_sem must be a PrioritySemaphore")
    self._registry_state().start_sem = value


_start_sem = StateProperty(_get_start_sem, _set_start_sem)


def _get_starting_pids(self: SessionManager) -> PidRefcount:
    return self._registry_state().starting_pids


def _set_starting_pids(self: SessionManager, value: PidRefcount) -> None:
    self._registry_state().starting_pids = value


_starting_pids = StateProperty(_get_starting_pids, _set_starting_pids)


def _get_subagent_runtimes(self: SessionManager) -> dict[str, "AcpRuntime"]:
    return self._registry_state().subagent_runtimes


def _set_subagent_runtimes(self: SessionManager, value: dict[str, "AcpRuntime"]) -> None:
    self._registry_state().subagent_runtimes = value


_subagent_runtimes = StateProperty(_get_subagent_runtimes, _set_subagent_runtimes)


def _get_subagent_runtime_locks(self: SessionManager) -> dict[str, asyncio.Lock]:
    return self._registry_state().subagent_runtime_locks


def _set_subagent_runtime_locks(self: SessionManager, value: dict[str, asyncio.Lock]) -> None:
    self._registry_state().subagent_runtime_locks = value


_subagent_runtime_locks = StateProperty(_get_subagent_runtime_locks, _set_subagent_runtime_locks)


def _get_draining_subagent_runtimes(self: SessionManager) -> list["AcpRuntime"]:
    return self._registry_state().draining_subagent_runtimes


def _set_draining_subagent_runtimes(self: SessionManager, value: list["AcpRuntime"]) -> None:
    self._registry_state().draining_subagent_runtimes = value


_draining_subagent_runtimes = StateProperty(
    _get_draining_subagent_runtimes, _set_draining_subagent_runtimes
)


def _get_continuable_keys(self: SessionManager) -> set[str]:
    return self._registry_state().continuable_keys


def _set_continuable_keys(self: SessionManager, value: set[str]) -> None:
    self._registry_state().continuable_keys = value


_continuable_keys = StateProperty(_get_continuable_keys, _set_continuable_keys)


def _get_continuable_fallback(self: SessionManager) -> Callable[[str], bool] | None:
    return self._registry_state().continuable_fallback


def _set_continuable_fallback(self: SessionManager, value: Callable[[str], bool] | None) -> None:
    self._registry_state().continuable_fallback = value


_continuable_fallback = StateProperty(_get_continuable_fallback, _set_continuable_fallback)


# Keep the established state-inspection seams while giving the coordinator
# a single authoritative state object.
def _get_compacting(self: SessionManager) -> set[str]:
    return self._compaction_state.compacting


def _set_compacting(self: SessionManager, value: set[str]) -> None:
    self._compaction_state.compacting = value


_compacting = StateProperty(_get_compacting, _set_compacting)


def _get_compact_cooldown_until(self: SessionManager) -> dict[str, float]:
    return self._compaction_state.cooldown_until


def _set_compact_cooldown_until(self: SessionManager, value: dict[str, float]) -> None:
    self._compaction_state.cooldown_until = value


_compact_cooldown_until = StateProperty(_get_compact_cooldown_until, _set_compact_cooldown_until)


def _get_compact_pending_verdict(self: SessionManager) -> dict[str, float]:
    return self._compaction_state.pending_verdict


def _set_compact_pending_verdict(self: SessionManager, value: dict[str, float]) -> None:
    self._compaction_state.pending_verdict = value


_compact_pending_verdict = StateProperty(_get_compact_pending_verdict, _set_compact_pending_verdict)


def _get_on_compacted(self: SessionManager) -> _CompactCallback | None:
    return self._compaction_state.on_compacted


def _set_on_compacted(self: SessionManager, value: _CompactCallback | None) -> None:
    self._compaction_state.on_compacted = value


_on_compacted = StateProperty(_get_on_compacted, _set_on_compacted)


def _get_pool_started(self: SessionManager) -> bool:
    return self._pool._pool_started


def _set_pool_started(self: SessionManager, value: bool) -> None:
    self._pool._pool_started = value


_pool_started = StateProperty(_get_pool_started, _set_pool_started)


def _get_pool_size(self: SessionManager) -> int:
    return self._pool._pool_size


def _set_pool_size(self: SessionManager, value: int) -> None:
    self._pool._pool_size = value


_pool_size = StateProperty(_get_pool_size, _set_pool_size)


def _get_pool_agent(self: SessionManager) -> str:
    return self._pool._pool_agent


def _set_pool_agent(self: SessionManager, value: str) -> None:
    self._pool._pool_agent = value


_pool_agent = StateProperty(_get_pool_agent, _set_pool_agent)


def _get_pool_ttl_secs(self: SessionManager) -> int:
    return self._pool._pool_ttl_secs


def _set_pool_ttl_secs(self: SessionManager, value: int) -> None:
    self._pool._pool_ttl_secs = value


_pool_ttl_secs = StateProperty(_get_pool_ttl_secs, _set_pool_ttl_secs)


def _get_pool_cwd(self: SessionManager) -> str:
    return self._pool._pool_cwd


def _set_pool_cwd(self: SessionManager, value: str) -> None:
    self._pool._pool_cwd = value


_pool_cwd = StateProperty(_get_pool_cwd, _set_pool_cwd)


def _get_warm_pool(self: SessionManager) -> asyncio.Queue[tuple[LLMProvider, float]]:
    return self._pool._warm_pool


def _set_warm_pool(self: SessionManager, value: asyncio.Queue[tuple[LLMProvider, float]]) -> None:
    self._pool._warm_pool = value


_warm_pool = StateProperty(_get_warm_pool, _set_warm_pool)


def _get_pool_fill_lock(self: SessionManager) -> asyncio.Lock:
    return self._pool._pool_fill_lock


def _set_pool_fill_lock(self: SessionManager, value: asyncio.Lock) -> None:
    self._pool._pool_fill_lock = value


_pool_fill_lock = StateProperty(_get_pool_fill_lock, _set_pool_fill_lock)


def _get_pool_health_task(self: SessionManager) -> asyncio.Task[Any] | None:
    return self._pool._pool_health_task


def _set_pool_health_task(self: SessionManager, value: asyncio.Task[Any] | None) -> None:
    self._pool._pool_health_task = value


_pool_health_task = StateProperty(_get_pool_health_task, _set_pool_health_task)


def _get_pool_sweep_pids(self: SessionManager) -> set[int]:
    return self._pool._pool_sweep_pids


def _set_pool_sweep_pids(self: SessionManager, value: set[int]) -> None:
    self._pool._pool_sweep_pids = value


_pool_sweep_pids = StateProperty(_get_pool_sweep_pids, _set_pool_sweep_pids)


def _get_bg_runtime(self: SessionManager) -> "AcpRuntime | None":
    return cast("AcpRuntime | None", self._background_runtime._bg_runtime)


def _set_bg_runtime(self: SessionManager, value: "AcpRuntime | None") -> None:
    self._background_runtime._bg_runtime = cast(Any, value)


_bg_runtime = StateProperty(_get_bg_runtime, _set_bg_runtime)


def _get_bg_runtime_lock(self: SessionManager) -> asyncio.Lock:
    return self._background_runtime._bg_runtime_lock


def _set_bg_runtime_lock(self: SessionManager, value: asyncio.Lock) -> None:
    self._background_runtime._bg_runtime_lock = value


_bg_runtime_lock = StateProperty(_get_bg_runtime_lock, _set_bg_runtime_lock)


def _get_draining_bg_runtimes(self: SessionManager) -> list["AcpRuntime"]:
    return cast(list["AcpRuntime"], self._background_runtime._draining_bg_runtimes)


def _set_draining_bg_runtimes(self: SessionManager, value: list["AcpRuntime"]) -> None:
    self._background_runtime._draining_bg_runtimes = cast(Any, value)


_draining_bg_runtimes = StateProperty(_get_draining_bg_runtimes, _set_draining_bg_runtimes)


def _get_inbound_callback_count(self: SessionManager) -> int:
    """Return callbacks admitted but not yet finished."""
    return self._allocation_boundary().inbound_callback_count


inbound_callback_count = StateProperty(_get_inbound_callback_count)


def _get_count(self: SessionManager) -> int:
    return len(self._sessions)


count = StateProperty(_get_count)
