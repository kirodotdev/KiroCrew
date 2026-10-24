"""The bounded dispatch window over the store's queued rows.

Whether a just-accepted row may go straight into ``_queue``, the lane-fair refill that tops the
window up from the store, the eviction that drops entries back to store-only to make room, the
hydration of a row into a window entry, the wake the refill arms for a row that is not claimable
yet, the refill's own lane balance, and the stop of rows a removed chat Autopilot stage queued,
done inside the refill's one store step.

A function here whose first parameter is ``self`` is a ``_TaskqBridgeMixin`` method: the class
binds it under the same name in ``admission/taskq_bridge.py``, so it reaches the manager through
``self._manager`` and its sibling members through ``self``, and a class-level patch of a member
reaches every call made through ``self`` or the class. ``_retire_legacy_stage_rows`` and
``_window_entry`` take no ``self`` and are bound as static methods.

The manager fields this module uses. Writes: ``_lane_refill_scheduler``,
``_overdue_wake_warned_at``, ``_queue``. Reads: ``_floor_deferred_ids``,
``_floor_fit_ids``, ``_held_approval_modes``, ``_sessions``, ``_stopping_parents``.
"""

from __future__ import annotations

import asyncio as _asyncio
import functools as _functools
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Callable, Mapping

from .types import MEMORY_WAIT_UNTIL_KEY, MIN_RECHECK_DELAY_SECS, WINDOW_ENTRY_RECOVERING

if TYPE_CHECKING:
    # Typing only. ``taskq`` loads on first use, as everywhere in the bridge, and the
    # facade imports this module at run time.
    from kiro_crew import taskq as _taskq
    from kiro_crew.taskq import lanes as _lanes

    from .taskq_bridge import _TaskqBridgeMixin

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


#: How far past its wake a deferred row may be before an empty refill pass is
#: reported as stuck (a row that came due during the pass is ordinary), and how
#: often that report may repeat.
_OVERDUE_WAKE_GRACE_SECS = 1.0
_OVERDUE_WAKE_WARN_EVERY_SECS = 60.0


async def taskq_should_window_async(self: _TaskqBridgeMixin, agent_id: str) -> bool:
    """:meth:`taskq_should_window` with its store count on the writer thread."""
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return True
    if len(self._manager._queue) >= store.window:
        return False
    exclude = [*self.taskq_window_ids(), agent_id]
    try:
        waiting_outside = await store.run(
            store.count_pending, _taskq.KIND_SUBAGENT, exclude_ids=exclude
        )
    except _taskq.TaskStoreUnavailable:
        return True
    return int(waiting_outside) == 0


def taskq_should_window(self: _TaskqBridgeMixin, agent_id: str) -> bool:
    """Whether the just-accepted spawn *agent_id* may go straight into ``_queue``.

    Only when the window has room AND no OTHER row is waiting outside it;
    otherwise the new row is store-only and the refill brings it in later,
    which is what keeps dispatch FIFO across the window boundary.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return True
    if len(self._manager._queue) >= store.window:
        return False
    try:
        waiting_outside = store.count_pending(
            _taskq.KIND_SUBAGENT, exclude_ids=[*self.taskq_window_ids(), agent_id]
        )
    except _taskq.TaskStoreUnavailable:
        return True
    return waiting_outside == 0


def taskq_refill_window(self: _TaskqBridgeMixin, *, children_only: bool = False) -> int:
    """Pull eligible store rows into ``_queue`` up to the window; returns how many.

    Lane-fair across the whole store, not just the window: each lane's
    rows enter oldest-first, and the lanes are interleaved by the same
    weighted round-robin the drain picks with, so one lane's backlog never
    fills the window while another lane's single row waits on disk.
    ``children_only`` admits nested rows only (the child reserve). Rows
    deferred or leased past now are skipped, and when the window is
    otherwise empty the pump schedules its own wake-up at the earliest
    time a row this pass may claim becomes claimable: the later of its
    ``next_run_at`` and ``lease_expires_at`` (:meth:`TaskStore.next_eligible_at`).

    Inline variant (sync callers): the store steps run on the calling
    thread. :meth:`taskq_refill_window_async` runs the same steps with
    every store read on the writer thread.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return 0
    # Wait deadlines are checked on the pump path: cheap, and the pump is
    # what runs whenever capacity moves.
    self.taskq_expire_waits()
    rows: list[_taskq.TaskRecord] = []
    tiered = not children_only or self.capacity_view().root_slot
    priorities = self.lane_priorities() if tiered else {}
    try:
        absent, lanes = self._refill_absent(store, children_only)
        room, want = self._refill_make_room(
            store, absent, lanes, priorities=priorities, children_only=children_only
        )
        rows, retired, stranded = self._refill_fetch_retiring(
            store, absent, room, want, children_only, priorities
        )
        self._report_retired_stage_rows(store, retired, stranded)
        self._refill_apply(rows)
        wake_at = (
            self._refill_idle_wake_read(store, children_only)()
            if not rows and not self._manager._queue
            else None
        )
    except _taskq.TaskStoreUnavailable:
        return len(rows)
    self._refill_schedule_wake(store, wake_at)
    return len(rows)


async def taskq_refill_window_async(self: _TaskqBridgeMixin, *, children_only: bool = False) -> int:
    """:meth:`taskq_refill_window` for the event-loop pump: the store
    reads run on the store's writer thread (``TaskStore.run``); only the
    window bookkeeping touches loop state."""
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return 0
    rows: list[_taskq.TaskRecord] = []
    # One tier snapshot for the whole pass, read on the loop: the eviction
    # ceiling, the eviction and the hydrating fetch must not see different
    # tiers. The child reserve's refill is untiered only when capacity is
    # reserve-only; while a root slot is free a tier still decides who starts.
    tiered = not children_only or self.capacity_view().root_slot
    priorities = self.lane_priorities() if tiered else {}
    try:
        absent, lanes = await store.run(self._refill_absent, store, children_only)
        room, want = self._refill_make_room(
            store, absent, lanes, priorities=priorities, children_only=children_only
        )
        rows, retired, stranded = await store.run(
            self._refill_fetch_retiring, store, absent, room, want, children_only, priorities
        )
        self._report_retired_stage_rows(store, retired, stranded)
        self._refill_apply(rows)
        wake_at = (
            await store.run(self._refill_idle_wake_read(store, children_only))
            if not rows and not self._manager._queue
            else None
        )
    except _taskq.TaskStoreUnavailable:
        return len(rows)
    self._refill_schedule_wake(store, wake_at)
    return len(rows)


def _refill_absent(
    self: _TaskqBridgeMixin, store: "_taskq.TaskStore", children_only: bool
) -> tuple[list[str], dict[str, str]]:
    """Store read: lanes with a waiting row whose head is not in the window.

    Returns the absent lanes AND the lane resolution the window entries
    needed, because the eviction that follows runs on the LOOP and asks the
    same question of the same entries: resolving it here (on the writer
    thread, in the async path) is what keeps that step off the connection.
    (The window's lane set is read on whatever thread calls.)
    """
    from kiro_crew import taskq as _taskq

    lanes = self._resolve_lanes(self._window_lane_keys())
    present = {
        self.lane_of_entry(p, lanes)
        for p in list(self._manager._queue)
        if not p.get("_resume_id") and (not children_only or self.entry_is_child(p))
    }
    pending = store.pending_lanes(
        _taskq.KIND_SUBAGENT,
        exclude_ids=self.taskq_dispatch_excluded_ids(),
        children_only=children_only,
    )
    return [lane for lane in pending if lane not in present], lanes


def _refill_make_room(
    self: _TaskqBridgeMixin,
    store: "_taskq.TaskStore",
    absent: list[str],
    lanes: "Mapping[str, str] | None" = None,
    *,
    priorities: "Mapping[str, int] | None" = None,
    children_only: bool = False,
) -> tuple[int, int]:
    """Loop step: every lane with a store row waiting must have its head in
    the window, or the pick cannot take turns with it. A window full of
    one lane makes room by dropping that lane's YOUNGEST entries back to
    store-only (they are queued rows; the refill brings them back in FIFO
    order); a window with no lane to spare gives up one head instead of
    making no room at all (:meth:`_evict_for_lanes`).

    A ROOT refill never evicts an entry of a higher priority tier than the
    best absent lane: that lane could not win a tiered pick against the
    entry, so dropping the entry would only let the lower tier start first
    once the higher one has left the window.

    *priorities* is the pass's one tier snapshot (``None`` reads it here). An
    empty mapping is an untiered refill: the child reserve's own top-up when
    capacity is reserve-only, untiered like its pick.

    A child-only refill does not keep a root entry that cannot start now (the
    memory-pressure hold is in force, or the entry waits on the floor): the
    ceiling would keep a running tree's nested child out of a window full of
    held higher-tier roots, and the tree would wait on its own child."""
    if priorities is None:
        priorities = self.lane_priorities()
    queue = self._manager._queue
    # Restricted entries have no durable copy and are outside its window.
    windowed = sum(p.get("_memory_mode", "persistent") == "persistent" for p in queue)
    want = min(len(absent), store.window) if absent else 0
    if absent:
        windowed -= self._evict_for_lanes(
            want - (store.window - windowed),
            lanes,
            max_rank=self._refill_evict_ceiling(absent, priorities),
            priorities=priorities,
            held=self._refill_held_roots() if children_only else None,
        )
    return max(0, store.window - windowed), want


def _refill_held_roots(self: _TaskqBridgeMixin) -> "Callable[[Mapping[str, Any]], bool]":
    """Predicate for a child-only refill: True for a window ROOT entry that
    cannot start now, so the tier ceiling does not keep it. Reads the
    memory-pressure hold once; any hold level counts as holding every root.
    A floor-deferred root counts only until the memory wake finds it fitting
    (``_floor_fit_ids``): from then on it can start, and keeps its tier."""
    mgr = self._manager
    hold = getattr(mgr, "_memory_pressure_hold", None)
    level = hold() if callable(hold) else None
    deferred = set(getattr(mgr, "_floor_deferred_ids", None) or ())
    deferred -= set(getattr(mgr, "_floor_fit_ids", None) or ())

    def held(params: "Mapping[str, Any]") -> bool:
        if self.entry_is_child(params):
            return False
        return level is not None or str(params.get("_preassigned_id") or "") in deferred

    return held


def _refill_evict_ceiling(
    self: _TaskqBridgeMixin, absent: list[str], priorities: "Mapping[str, int]"
) -> int | None:
    """Highest priority rank an entry may hold and still be evicted for
    *absent*: the best absent lane's rank. ``None`` (no ceiling) when no
    tier is set or a system lane is absent, because the system lane takes
    part in every tiered pick whatever the tier."""
    from kiro_crew.taskq import lanes as _lanes

    if not priorities or any(_lanes.is_system_lane(lane) for lane in absent):
        return None
    return max(priorities.get(lane, _lanes.DEFAULT_PRIORITY_RANK) for lane in absent)


def _refill_fetch(
    self: _TaskqBridgeMixin,
    store: "_taskq.TaskStore",
    absent: list[str],
    room: int,
    want: int,
    children_only: bool,
    priorities: "Mapping[str, int] | None" = None,
) -> list["_taskq.TaskRecord"]:
    """Store read: the absent lanes' heads first, then whatever room is left.

    *priorities* is the pass's tier snapshot (see :func:`lane_refill_scheduler`)."""
    from kiro_crew import taskq as _taskq

    exclude = self.taskq_dispatch_excluded_ids()
    sched = self.lane_refill_scheduler(priorities=priorities)
    rows: list[_taskq.TaskRecord] = []
    if absent and room > 0:
        rows.extend(
            store.fetch_dispatchable_fair(
                _taskq.KIND_SUBAGENT,
                limit=min(room, want),
                scheduler=sched,
                exclude_ids=exclude,
                children_only=children_only,
                lanes=absent,
                per_lane_limit=1,
            )
        )
        exclude = exclude + [rec.id for rec in rows]
        room -= len(rows)
    if room > 0:
        rows.extend(
            store.fetch_dispatchable_fair(
                _taskq.KIND_SUBAGENT,
                limit=room,
                scheduler=sched,
                exclude_ids=exclude,
                children_only=children_only,
            )
        )
    return rows


def _refill_fetch_retiring(
    self: _TaskqBridgeMixin,
    store: "_taskq.TaskStore",
    *fetch_args: Any,
) -> tuple[list["_taskq.TaskRecord"], list[dict[str, Any]], bool]:
    """Store phase: :meth:`_refill_fetch`, then retire legacy stage rows.

    One store step, so the off-loop refill keeps its single writer-thread hop.
    """
    return self._retire_legacy_stage_rows(store, self._refill_fetch(store, *fetch_args))


def _retire_legacy_stage_rows(
    store: "_taskq.TaskStore",
    rows: list["_taskq.TaskRecord"],
) -> tuple[list["_taskq.TaskRecord"], list[dict[str, Any]], bool]:
    """Store phase: cancel rows a chat Autopilot stage queued, keep the rest.

    Such a row carries ``_stage_boundary_owner``. The stage that owned it is
    gone with the retired Autopilot, so the row is stopped rather than run.
    Returns the rows still to dispatch, the params of each one stopped, and
    whether a store outage left any stage row uncancelled. An outage stops
    the cancelling but still returns every row already cancelled, so each
    committed cancel is reported; the rest stay queued, are never
    dispatched from this pass, and the caller arms a retry pass for them.
    """
    from kiro_crew import taskq as _taskq

    kept: list[_taskq.TaskRecord] = []
    retired: list[dict[str, Any]] = []
    store_down = False
    for rec in rows:
        if not rec.params.get("_stage_boundary_owner"):
            kept.append(rec)
            continue
        if store_down:
            continue
        try:
            previous = store.cancel(
                rec.id,
                reason="user_stop",
                only_from=_taskq.CLAIMABLE,
                generation=rec.generation,
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.warning(
                "taskq: cancel of retired-stage row %s failed", rec.id, exc_info=True
            )
            store_down = True
            continue
        if previous is not None:
            params = dict(rec.params)
            params["_preassigned_id"] = rec.id
            params["_stop_origin"] = "queued by chat Autopilot, which was removed"
            retired.append(params)
    return kept, retired, store_down


def _report_retired_stage_rows(
    self: _TaskqBridgeMixin,
    store: "_taskq.TaskStore",
    retired: list[dict[str, Any]],
    stranded: bool,
) -> None:
    """Loop step: publish each retired stage row as stopped before start.

    A stage row a store outage left queued gets a retry pass of its own:
    the refill that follows may finish without any further store step, and
    nothing else is certain to drain the queue again in this process.
    """
    if stranded:
        self._refill_schedule_wake(store, store.now() + self.taskq_admit_wait_secs())
    for params in retired:
        _glue_logger.info(
            "taskq: %s was queued by chat Autopilot, which was removed; stopped",
            params.get("_preassigned_id"),
        )
        self._manager._report_queued_stop(params)


def _refill_apply(self: _TaskqBridgeMixin, rows: list["_taskq.TaskRecord"]) -> None:
    """Loop step: the fetched rows join the window (skipping any that a
    concurrent step already placed).

    NOTHING joins the window while gateway admission is closed, the same
    gate every spawn passes: the updater reads the in-flight census once and
    then replaces the process, and a row hydrated behind that read would be
    picked by the pump only to be REFUSED by the spawn gate -- an accepted,
    durable row announced as a rejection. Left on disk it is simply the next
    boot's work. Nothing is lost by dropping them here: the fetch is a pure
    read (``fetch_dispatchable_fair`` claims nothing) and the eviction that
    made room put its entries back to store-only, so a pass that runs into
    the gate can only SHRINK the window -- which is what the census wants.
    """
    sessions = getattr(self._manager, "_sessions", None)
    if getattr(sessions, "admission_closed", False) is True:
        return
    present = {p.get("_preassigned_id") for p in self._manager._queue}
    held_modes = getattr(self._manager, "_held_approval_modes", {})
    floor_deferred = getattr(self._manager, "_floor_deferred_ids", ())
    # A parent with a Stop all in flight gets nothing back in the window.
    # This fetch may have run on the writer thread before the stop's
    # cancels did, so its rows can be ones the stop just cancelled, or this
    # parent's store-only rows, which the stop's pending read leaves out
    # once they are windowed. Left on disk, they are the stop's to cancel.
    stopping = getattr(self._manager, "_stopping_parents", None) or {}
    for rec in rows:
        entry = self._window_entry(rec)
        if rec.id in held_modes:
            # This process accepted the row with that mode and is the one
            # starting it: the same request's consent, not a replay
            # (``spawn_impl`` records it; ``_window_entry`` says why the store
            # never carries it).
            entry["approval_mode"] = held_modes[rec.id]
        if rec.id in floor_deferred:
            # Its last defer was the memory floor's: a floor wait whose admit
            # wait the store already metered (``next_run_at``), so the pick
            # leaves it to the gate, which re-checks the floor before the
            # pressure hold, and no pressure clock starts below the floor.
            entry[MEMORY_WAIT_UNTIL_KEY] = 0.0
        if str(entry.get("parent_session_key") or "") in stopping:
            continue
        if rec.id not in present:
            self._manager._queue.append(entry)


def _refill_idle_wake_read(
    self: _TaskqBridgeMixin, store: "_taskq.TaskStore", children_only: bool
) -> "_functools.partial[float | None]":
    """The store read for when the earliest row this pass may claim, and
    could not, becomes claimable -- over the same rows the pass read. Its
    exclusions are taken here, by the caller (on the loop)."""
    from kiro_crew import taskq as _taskq

    return _functools.partial(
        store.next_eligible_at,
        _taskq.KIND_SUBAGENT,
        exclude_ids=self.taskq_dispatch_excluded_ids(),
        children_only=children_only,
    )


def _refill_schedule_wake(
    self: _TaskqBridgeMixin, store: "_taskq.TaskStore", wake_at: float | None
) -> None:
    if wake_at is None:
        return
    due_in = wake_at - store.now()
    if due_in < -_OVERDUE_WAKE_GRACE_SECS:
        # The wake is read over the rows this pass may claim, so a row well
        # past it that the pass still did not take is held by something the
        # read cannot see. A wake re-armed at 0 would re-run this same
        # empty pass on every loop turn for as long as that lasts; the
        # floor below bounds it, and this says so.
        now = _time.monotonic()
        if now - self._manager._overdue_wake_warned_at >= _OVERDUE_WAKE_WARN_EVERY_SECS:
            self._manager._overdue_wake_warned_at = now
            _glue_logger.warning(
                "taskq: a waiting subagent row is %.0fs past its wake and none is "
                "claimable; re-checking every %.2fs",
                -due_in,
                MIN_RECHECK_DELAY_SECS,
            )
    delay = max(MIN_RECHECK_DELAY_SECS, min(due_in, self.taskq_admit_wait_secs()))
    try:
        _asyncio.get_event_loop().call_later(delay, self._manager._drain_queue)
    except RuntimeError:
        pass


def _window_entry(rec: "_taskq.TaskRecord") -> dict[str, Any]:
    from kiro_crew import taskq as _taskq

    params = dict(rec.params)
    params["_preassigned_id"] = rec.id
    params["_lane"] = rec.lane
    # The row is a run being rebuilt after its owner was lost, so the
    # chip's window half leaves it out as its store half does; ``spawn``
    # takes the mark as its keyword and puts it back on a re-queued entry.
    # Set from the row's state alone, never from its params.
    params.pop(WINDOW_ENTRY_RECOVERING, None)
    if rec.state == _taskq.RECOVERING:
        params[WINDOW_ENTRY_RECOVERING] = True
    params.pop("_legacy_import", None)
    # Every row written before the stage-owner token was removed carries
    # ``_stage_boundary_owner`` (empty for an unowned spawn). ``spawn`` no
    # longer takes that keyword, so it is dropped here; a NON-empty owner
    # never reaches this point, ``_retire_legacy_stage_rows`` stops it first.
    params.pop("_stage_boundary_owner", None)
    # Both process-local params are stripped on the READ side as well as by
    # ``taskq_build_record``, because a row is written by one build and
    # started by another: a row that still carries either one replays one
    # request's moment into a start the request never authorised -- the
    # ownership and agent gates skipped (``_agent_prevalidated``), or the
    # spawn gate skipped and the run's tools pre-approved on a consent
    # nobody renewed (``approval_mode``). The value stays readable in
    # ``scope_ref``, which the schema defines as references rather than
    # grants and which no start path reads.
    params.pop("_agent_prevalidated", None)
    params.pop("approval_mode", None)
    return params


def _evict_for_lanes(
    self: _TaskqBridgeMixin,
    count: int,
    lanes: "Mapping[str, str] | None" = None,
    *,
    max_rank: int | None = None,
    priorities: "Mapping[str, int] | None" = None,
    held: "Callable[[Mapping[str, Any]], bool] | None" = None,
) -> int:
    """Drop up to *count* window entries back to store-only to make room.

    Takes the youngest entry of the most-represented lane each time and
    never a resume entry, so every lane in the window keeps its head and its
    FIFO order for as long as any lane has a SPARE entry to give.

    A window holding exactly one entry per lane has no spare, and keeping
    every head there frees NOTHING -- which is not a milder outcome but the
    worst one: the refill hydrates no row at all, so the slot the child
    reserve holds open can never be filled from disk and a tree waits on its
    own child for as long as the process lives. The youngest head goes
    instead, at most ONE per call, so the window churns by a single entry:
    that entry is a queued row, refetched in FIFO order on a later pass.

    *lanes* answers the lane of an entry that names none from
    :meth:`_refill_absent`'s off-loop resolution, so this loop step takes no
    store read of its own.

    *max_rank* keeps every entry of a lane whose priority rank is above it
    (see :meth:`_refill_make_room`); ``None`` considers every lane. The ranks
    come from *priorities*, the refill pass's snapshot, or are read here when
    a ceiling is given without one. An entry *held* answers True for is
    evictable whatever its rank: it cannot start now, so keeping it protects
    no tier.
    """
    from kiro_crew.taskq import lanes as _lanes

    queue = self._manager._queue
    if max_rank is None:
        priorities = {}
    elif priorities is None:
        priorities = self.lane_priorities()
    evicted = 0
    head_dropped = False
    while evicted < count:
        by_lane: dict[str, list[int]] = {}
        for idx, params in enumerate(queue):
            if (
                params.get("_resume_id")
                or not params.get("_preassigned_id")
                or params.get("_memory_mode", "persistent") != "persistent"
            ):
                continue
            lane = self.lane_of_entry(params, lanes)
            if (
                max_rank is not None
                and priorities.get(lane, _lanes.DEFAULT_PRIORITY_RANK) > max_rank
                and not (held is not None and held(params))
            ):
                continue
            by_lane.setdefault(lane, []).append(idx)
        spare = [(len(ids), ids[-1]) for ids in by_lane.values() if len(ids) > 1]
        if spare:
            _, victim = max(spare)
        elif by_lane and not head_dropped:
            victim = max(ids[-1] for ids in by_lane.values())
            head_dropped = True
        else:
            break
        queue.pop(victim)
        evicted += 1
    return evicted


def lane_refill_scheduler(
    self: _TaskqBridgeMixin, *, priorities: "Mapping[str, int] | None" = None
) -> "_lanes.LaneScheduler":
    """A second balance for the store->window refill, so filling the window
    does not spend the credit the drain picks with.

    *priorities* is the refill pass's tier snapshot (``None`` reads it now).
    An empty mapping clears the tiers on it for this caller, which is how
    the ``children_only`` refill hydrates rows when capacity is reserve-only:
    the store reader drives :meth:`LaneScheduler.interleave` itself, so the
    exemption is expressed on the scheduler handed to it rather than as an
    argument. A tier there would hydrate one chat's queued children pass
    after pass and leave another chat's in-flight tree with no head in the
    window for the reserve to start.
    """
    from kiro_crew.taskq import lanes as _lanes

    scheduler = getattr(self._manager, "_lane_refill_scheduler", None)
    settings = self.fairness_settings()
    if not isinstance(scheduler, _lanes.LaneScheduler):
        scheduler = _lanes.LaneScheduler(weights=settings.lane_weights)
        setattr(self._manager, "_lane_refill_scheduler", scheduler)
    else:
        scheduler.weights = settings.lane_weights
    scheduler.priorities = dict(self.lane_priorities() if priorities is None else priorities)
    return scheduler
