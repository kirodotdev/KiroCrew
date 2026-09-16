"""Fairness: lanes, the child reserve, the capacity view (RFC section 6, Q5)."""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Iterable, Mapping

from .._component import ManagerComponent
from .types import (
    FAIRNESS_SETTINGS_TTL_SECS,
    CapacityView,
    FairnessSettings,
)

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")

if TYPE_CHECKING:
    from kiro_crew import taskq as _taskq
    from kiro_crew.taskq import lanes as _lanes

    from ...subagent import SubagentInfo, asyncio, time


class _FairnessMixin(ManagerComponent):
    __slots__ = ()

    if TYPE_CHECKING:
        # Sibling-mixin methods this module reaches through ``self``; typing only.
        pump_off_loop: bool

        def resume_granted(self, agent_id: str) -> bool: ...

        def taskq_excluded_ids(self) -> list[str]: ...

        def taskq_store(self) -> "_taskq.TaskStore | None": ...

    # ── fairness: lanes, the child reserve, the capacity view (RFC §6, Q5) ──
    #
    # A lane is one root session's queue (automation roots share ``system``).
    # The pump picks the next lane by smooth weighted round-robin, so a
    # 1000-row fan-out from one session takes turns with another session's
    # single spawn and with cron/hook work instead of starving them. The
    # child reserve keeps the last slot(s) for nested rows and resuming
    # parents while a tree is in flight.

    def fairness_settings(self) -> FairnessSettings:
        """Config-backed knobs, cached on the manager for a short TTL."""
        cached = getattr(self._manager, "_fairness_settings", None)
        stamp = float(getattr(self._manager, "_fairness_settings_ts", 0.0) or 0.0)
        now = _time.monotonic()
        if isinstance(cached, FairnessSettings) and now - stamp < FAIRNESS_SETTINGS_TTL_SECS:
            return cached
        settings = cached if isinstance(cached, FairnessSettings) else FairnessSettings()
        try:
            from kiro_crew.config.loader import KiroCrewConfig

            settings = FairnessSettings.from_agent_config(KiroCrewConfig.load().agent)
        except Exception:
            _glue_logger.debug("fairness settings: config unreadable, keeping last", exc_info=True)
        setattr(self._manager, "_fairness_settings", settings)
        setattr(self._manager, "_fairness_settings_ts", now)
        return settings

    def set_fairness_settings(self, settings: FairnessSettings | None) -> None:
        """Install *settings* now (live reload, tests); None re-reads config next time."""
        from kiro_crew.taskq import lanes as _lanes

        if settings is None:
            setattr(self._manager, "_fairness_settings", None)
            setattr(self._manager, "_fairness_settings_ts", 0.0)
            return
        setattr(self._manager, "_fairness_settings", settings)
        # Pinned: a TTL far in the future keeps the pump from re-reading config.
        setattr(self._manager, "_fairness_settings_ts", _time.monotonic() + 1e12)
        scheduler = getattr(self._manager, "_lane_scheduler", None)
        if isinstance(scheduler, _lanes.LaneScheduler):
            scheduler.weights = settings.lane_weights

    def lane_scheduler(self) -> "_lanes.LaneScheduler":
        """The manager's one weighted round-robin state, shared by refill and pick."""
        from kiro_crew.taskq import lanes as _lanes

        scheduler = getattr(self._manager, "_lane_scheduler", None)
        settings = self.fairness_settings()
        if not isinstance(scheduler, _lanes.LaneScheduler):
            scheduler = _lanes.LaneScheduler(weights=settings.lane_weights)
            setattr(self._manager, "_lane_scheduler", scheduler)
        else:
            scheduler.weights = settings.lane_weights
        return scheduler

    def lane_for_session(self, session_key: str | None) -> str:
        """The lane a spawn from *session_key* is dispatched under.

        A ``subagent:<id>`` key resolves up the live parent chain to the root
        session (the store row is consulted for a parent that is not live);
        any other key is a root and maps by :func:`lanes.lane_key_for`.

        The dead-parent step is a SYNCHRONOUS ``store.get``, so a caller on the
        gateway loop takes it off the loop instead: the coroutine pump resolves
        every window key through :meth:`resolve_window_lanes_async` before it
        picks, and :meth:`lane_snapshot_async` does the same for its own keys.
        Reached ON the loop anyway -- an entry that joined the window during
        that resolve -- the walk stops at the key it has rather than spending
        the connection here, the way :meth:`pending_children` stops at its
        cache: this decides which lane takes the next turn, and one pass of a
        nested row counted under its own lane instead of its root's costs
        ordering, while the read costs every session on the loop the
        connection's whole busy wait. The next pass has the resolved answer,
        and the entry keeps its FIFO place either way.
        """
        from kiro_crew import taskq as _taskq
        from kiro_crew.taskq import lanes as _lanes

        key = str(session_key or "")
        seen: set[str] = set()
        while key.startswith(_lanes.SUBAGENT_PREFIX) and key not in seen:
            seen.add(key)
            parent_id = key[len(_lanes.SUBAGENT_PREFIX) :]
            live = self._manager._agents.get(parent_id)
            if live is not None:
                key = str(live.parent_session_key or "")
                continue
            store = self.taskq_store()
            rec = None
            if store is not None:
                if self.store_reads_are_off_loop_here():
                    return _lanes.lane_key_for(key)
                try:
                    rec = store.get(parent_id)
                except _taskq.TaskStoreUnavailable:
                    rec = None
            if rec is None:
                return key
            if rec.lane:
                return rec.lane
            key = rec.session_key
        return _lanes.lane_key_for(key)

    def store_reads_are_off_loop_here(self) -> bool:
        """Whether a synchronous store read on THIS thread would be a defect.

        True on a thread with a running event loop while the coroutine pump
        owns the store phases: the read belongs on the writer thread, and the
        caller here has an off-loop answer to fall back on. False on the
        writer thread and in the inline (no-loop) pump, where the synchronous
        read is the only read there is.
        """
        try:
            _asyncio.get_running_loop()
        except RuntimeError:
            return False
        return bool(type(self).pump_off_loop)

    def _resolve_lanes(self, session_keys: Iterable[str]) -> dict[str, str]:
        """The lane each of *session_keys* resolves to.

        Called on whatever thread has the store: the loop callers hand it to
        the writer thread (:meth:`resolve_window_lanes_async`), the inline pump
        runs it where it stands.
        """
        return {str(key): self.lane_for_session(key) for key in session_keys}

    def _window_lane_keys(self) -> list[str]:
        """Window entries' parent keys that still need a lane resolved.

        An entry carrying ``_lane`` needs none, and every entry the refill
        hydrates from a row carries it, so an ordinary pass resolves nothing
        and asks the store for nothing.
        """
        return sorted(
            {
                str(params.get("parent_session_key") or "")
                for params in self._manager._queue
                if not params.get("_lane")
            }
        )

    async def resolve_window_lanes_async(self) -> dict[str, str]:
        """Lanes for :meth:`_window_lane_keys`, resolved on the writer thread.

        The pick and the eviction ask :meth:`lane_of_entry` for a lane per
        window entry, and an entry with no ``_lane`` walks its parent chain
        through ``store.get``. The pump asking is a coroutine, so the walk runs
        once per pass off the loop and the answer is handed down. Nothing
        awaits between this and the pick that uses it, so the window the keys
        came from is the window that gets picked from.
        """
        keys = self._window_lane_keys()
        store = self.taskq_store()
        if store is None or not keys:
            return {}
        return dict(await store.run(self._resolve_lanes, keys))

    def lane_of_entry(
        self, params: Mapping[str, Any], resolved: Mapping[str, str] | None = None
    ) -> str:
        """The lane *params* is dispatched under. *resolved* answers from lanes
        already resolved off-loop (:meth:`lane_snapshot_async`) instead of
        walking the parent chain here."""
        lane = str(params.get("_lane") or "")
        if lane:
            return lane
        key = str(params.get("parent_session_key") or "")
        if resolved is not None and key in resolved:
            return resolved[key]
        return self.lane_for_session(key)

    @staticmethod
    def entry_is_child(params: Mapping[str, Any]) -> bool:
        from kiro_crew.taskq import lanes as _lanes

        return str(params.get("parent_session_key") or "").startswith(_lanes.SUBAGENT_PREFIX)

    def waiting_parents(self) -> list["SubagentInfo"]:
        """Live runs that yielded their slot to wait on children."""
        from kiro_crew import taskq as _taskq

        out = []
        for info in self._manager._agents.values():
            if info.done or not info._slot_released:
                continue
            record = info._wait_record if isinstance(info._wait_record, dict) else None
            if record is not None and record.get("state") == _taskq.WAITING_CHILDREN:
                out.append(info)
        return out

    def pending_children(self) -> int:
        """Nested rows waiting to start, in the window or store-only.

        The store half is a read the capacity snapshot needs synchronously.
        On the event loop (off-loop pump on) it comes from the cache the
        coroutine pump refreshes on the writer thread at the start of every
        pass (:meth:`refresh_pending_children_async`); inline callers read
        the store directly.
        """
        count = sum(1 for p in self._manager._queue if self.entry_is_child(p))
        store = self.taskq_store()
        # A nested row has a live parent (orphans are cancelled at boot), so
        # with no live run at all the store cannot hold one: skip the query.
        any_live = any(not info.done for info in self._manager._agents.values())
        if store is None or not any_live:
            return count
        try:
            _asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        if on_loop and type(self).pump_off_loop:
            return count + int(getattr(self._manager, "_pending_children_store", 0) or 0)
        return count + self._store_pending_children(store, self.taskq_excluded_ids())

    @staticmethod
    def _store_pending_children(store: "_taskq.TaskStore", exclude_ids: list[str]) -> int:
        from kiro_crew import taskq as _taskq

        try:
            lanes = store.pending_lanes(
                _taskq.KIND_SUBAGENT, exclude_ids=exclude_ids, children_only=True
            )
        except _taskq.TaskStoreUnavailable:
            return 0
        return sum(lanes.values())

    async def ensure_coordinator_async(self) -> None:
        """Build the dependency coordinator on the writer thread when it does
        not exist yet: its one-time ``rebuild`` reads the waiting rows, and
        the first loop-side caller should not pay for that on the loop."""
        store = self.taskq_store()
        if (
            store is None
            or getattr(self._manager, "_taskq_dependency_coordinator", None) is not None
        ):
            return
        await store.run(self._manager.dependency_coordinator)

    async def refresh_pending_children_async(self) -> None:
        """Refresh the store half of :meth:`pending_children` on the writer thread."""
        store = self.taskq_store()
        if store is None:
            return
        exclude = self.taskq_excluded_ids()
        value = await store.run(self._store_pending_children, store, exclude)
        setattr(self._manager, "_pending_children_store", int(value))

    def capacity_view(self) -> CapacityView:
        """Read the cap, the running count and the child reserve as one snapshot."""
        settings = self.fairness_settings()
        cap = int(self._manager._max_concurrent)
        running = int(self._manager._running_count)
        waiting = len(self.waiting_parents())
        lifted_from: int | None = None
        if waiting and settings.child_reserve > 0:
            # Honour the reserve under an adaptive squeeze: never above the
            # user's ceiling, and only when a controller has lowered the cap
            # (a cap set by the user or a test is not lifted).
            adaptive = getattr(self._manager, "_adaptive_cap", None)
            ceiling = int(getattr(self._manager, "_user_max_concurrent", cap) or cap)
            if adaptive is not None:
                floor_with_reserve = min(ceiling, settings.adaptive_floor + settings.child_reserve)
                if floor_with_reserve > cap:
                    lifted_from = cap
                    cap = floor_with_reserve
        # The reserve is for STARTS that unblock a tree: nested rows waiting to
        # start and resumes waiting for a slot. A parent that merely waits
        # while its children run reserves nothing -- unrelated work fills the
        # cap (RFC §14.3: siblings and other sessions keep going).
        reserve_active = settings.child_reserve > 0 and (
            any(p.get("_resume_id") for p in self._manager._queue) or self.pending_children() > 0
        )
        return CapacityView(
            cap_total=cap,
            running=running,
            child_reserve=settings.child_reserve,
            reserve_active=reserve_active,
            waiting_parents=waiting,
            lifted_from=lifted_from,
        )

    def root_may_start(self) -> bool:
        """Whether a depth-0 spawn may take a slot right now (the reserve honoured)."""
        return self.capacity_view().root_slot

    def pick_window_index(
        self, view: CapacityView | None = None, *, lanes: Mapping[str, str] | None = None
    ) -> int | None:
        """Which ``_queue`` entry the pump takes next, or None when none may start.

        Order: a queued resume (front, FIFO among resumes), then the weighted
        round-robin over lanes among eligible entries. With only the reserve
        left, eligible means nested; with no slot at all, nothing is.

        *lanes* answers the per-entry lane from a resolution the caller made
        off the loop (:meth:`resolve_window_lanes_async`); without it the lane
        of an entry that carries none is walked here, which only an inline
        (no-loop) caller may pay for.
        """
        queue = self._manager._queue
        if not queue:
            return None
        view = view or self.capacity_view()
        if not view.any_slot:
            return None
        for idx, params in enumerate(queue):
            if params.get("_resume_id"):
                return idx
        roots_ok = view.root_slot

        def eligible(params: Mapping[str, Any]) -> bool:
            return roots_ok or self.entry_is_child(params)

        def lane_of(params: Mapping[str, Any]) -> str:
            return self.lane_of_entry(params, lanes)

        return self.lane_scheduler().pick_index(queue, lane_of=lane_of, eligible=eligible)

    def lane_snapshot(self) -> dict[str, Any]:
        """Per-lane queue depth and running count with the scheduler's balance.

        Inline variant (sync callers): the store steps run on the calling
        thread. :meth:`lane_snapshot_async` runs the same steps with every
        store read on the writer thread.
        """
        store = self.taskq_store()
        if store is None:
            return self._lane_snapshot_from({}, {})
        pending, resolved = self._store_lane_reads(store, self.taskq_excluded_ids(), [])
        return self._lane_snapshot_from(pending, resolved)

    async def lane_snapshot_async(self) -> dict[str, Any]:
        """:meth:`lane_snapshot` for an event-loop caller.

        Both store halves run on the writer thread -- the rows waiting per lane
        and the lane a session key with a dead parent resolves to --
        while the window, the live runs and the scheduler balance are read on
        the caller's loop (:meth:`refresh_pending_children_async`'s shape).
        """
        store = self.taskq_store()
        if store is None:
            return self._lane_snapshot_from({}, {})
        pending, resolved = await store.run(
            self._store_lane_reads,
            store,
            self.taskq_excluded_ids(),
            self._lane_session_keys(),
        )
        return self._lane_snapshot_from(pending, resolved)

    def _store_lane_reads(
        self, store: "_taskq.TaskStore", exclude_ids: list[str], session_keys: list[str]
    ) -> tuple[dict[str, int], dict[str, str]]:
        """The store half of :meth:`lane_snapshot`: rows waiting per lane, and
        the lane each of *session_keys* resolves to."""
        from kiro_crew import taskq as _taskq

        try:
            pending = dict(store.pending_lanes(_taskq.KIND_SUBAGENT, exclude_ids=exclude_ids))
        except _taskq.TaskStoreUnavailable:
            pending = {}
        return pending, self._resolve_lanes(session_keys)

    def _lane_session_keys(self) -> list[str]:
        """The session keys :meth:`lane_snapshot` has to resolve to a lane.

        Read on the caller's thread: the window and the live runs are loop
        state. An entry that already carries ``_lane`` needs no resolution.
        """
        keys = set(self._window_lane_keys())
        keys.update(
            str(info.parent_session_key or "")
            for info in list(self._manager._agents.values())
            if not info.done
        )
        return sorted(keys)

    def _lane_snapshot_from(
        self, pending: Mapping[str, int], resolved: Mapping[str, str]
    ) -> dict[str, Any]:
        """Assemble the snapshot from the store half *pending* / *resolved*."""
        settings = self.fairness_settings()
        scheduler = self.lane_scheduler()
        lanes: dict[str, dict[str, Any]] = {}

        def bucket(lane: str) -> dict[str, Any]:
            return lanes.setdefault(
                lane,
                {"queued": 0, "running": 0, "waiting": 0, "weight": scheduler.weight_of(lane)},
            )

        def lane_of_key(session_key: str) -> str:
            key = str(session_key or "")
            return resolved[key] if key in resolved else self.lane_for_session(key)

        for params in self._manager._queue:
            bucket(self.lane_of_entry(params, resolved))["queued"] += 1
        for lane, n in pending.items():
            bucket(lane)["queued"] += n
        for info in list(self._manager._agents.values()):
            if info.done:
                continue
            b = bucket(lane_of_key(info.parent_session_key))
            if info._slot_released:
                b["waiting"] += 1
            else:
                b["running"] += 1
        return {
            "lanes": lanes,
            "credit": scheduler.snapshot(),
            "capacity": self.capacity_view().to_dict(),
            "child_reserve": settings.child_reserve,
        }

    async def wait_resume_granted(self, agent_id: str, *, timeout: float) -> bool:
        """Await the slot grant for a yielded run; True when it holds its slot.

        Event-driven, not polled: :meth:`resume_grant` sets the per-run event.
        Answers True at once for a run that never yielded or is unknown here
        (nothing to hold for), False when *timeout* passes first.
        """
        info = self._manager._agents.get(agent_id)
        if info is None or info.done or self.resume_granted(agent_id):
            return True
        event = getattr(info, "_resume_event", None)
        if not isinstance(event, _asyncio.Event):
            event = _asyncio.Event()
            info._resume_event = event
        if self.resume_granted(agent_id):
            return True
        try:
            await _asyncio.wait_for(event.wait(), timeout=max(0.0, float(timeout)))
        except _asyncio.TimeoutError:
            return self.resume_granted(agent_id)
        return True
