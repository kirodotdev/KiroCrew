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
``_held_approval_modes``, ``_sessions``, ``_stopping_parents``.
"""

from __future__ import annotations

import asyncio as _asyncio
import functools as _functools
import logging as _logging
import time as _time
from typing import TYPE_CHECKING, Any, Mapping

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
    try:
        absent, lanes = self._refill_absent(store, children_only)
        room, want = self._refill_make_room(store, absent, lanes)
        rows, retired, stranded = self._refill_fetch_retiring(
            store, absent, room, want, children_only
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
    try:
        absent, lanes = await store.run(self._refill_absent, store, children_only)
        room, want = self._refill_make_room(store, absent, lanes)
        rows, retired, stranded = await store.run(
            self._refill_fetch_retiring, store, absent, room, want, children_only
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
) -> tuple[int, int]:
    """Loop step: every lane with a store row waiting must have its head in
    the window, or the pick cannot take turns with it. A window full of
    one lane makes room by dropping that lane's YOUNGEST entries back to
    store-only (they are queued rows; the refill brings them back in FIFO
    order); a window with no lane to spare gives up one head instead of
    making no room at all (:meth:`_evict_for_lanes`)."""
    queue = self._manager._queue
    # Restricted entries have no durable copy and are outside its window.
    windowed = sum(p.get("_memory_mode", "persistent") == "persistent" for p in queue)
    want = min(len(absent), store.window) if absent else 0
    if absent:
        windowed -= self._evict_for_lanes(want - (store.window - windowed), lanes)
    return max(0, store.window - windowed), want


def _refill_fetch(
    self: _TaskqBridgeMixin,
    store: "_taskq.TaskStore",
    absent: list[str],
    room: int,
    want: int,
    children_only: bool,
) -> list["_taskq.TaskRecord"]:
    """Store read: the absent lanes' heads first, then whatever room is left."""
    from kiro_crew import taskq as _taskq

    exclude = self.taskq_dispatch_excluded_ids()
    rows: list[_taskq.TaskRecord] = []
    if absent and room > 0:
        rows.extend(
            store.fetch_dispatchable_fair(
                _taskq.KIND_SUBAGENT,
                limit=min(room, want),
                scheduler=self.lane_refill_scheduler(),
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
                scheduler=self.lane_refill_scheduler(),
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
    self: _TaskqBridgeMixin, count: int, lanes: "Mapping[str, str] | None" = None
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
    """
    queue = self._manager._queue
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
            by_lane.setdefault(self.lane_of_entry(params, lanes), []).append(idx)
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


def lane_refill_scheduler(self: _TaskqBridgeMixin) -> "_lanes.LaneScheduler":
    """A second balance for the store->window refill, so filling the window
    does not spend the credit the drain picks with."""
    from kiro_crew.taskq import lanes as _lanes

    scheduler = getattr(self._manager, "_lane_refill_scheduler", None)
    settings = self.fairness_settings()
    if not isinstance(scheduler, _lanes.LaneScheduler):
        scheduler = _lanes.LaneScheduler(weights=settings.lane_weights)
        setattr(self._manager, "_lane_refill_scheduler", scheduler)
    else:
        scheduler.weights = settings.lane_weights
    return scheduler
