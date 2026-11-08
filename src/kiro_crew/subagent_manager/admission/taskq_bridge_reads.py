"""Store reads of accepted work that has no run yet.

The overflow counts the pending-work guards and the queue-depth chip take, the pending ids a
stop cancels, the queued listing and the by-id lookup, and a wave's batch membership. "A run
exists" has one spelling, ``_live_run_ids``, which the class's exclusion sets and the expiry
sweep build on; "accepted, no run yet" has one definition, ``_overflow_query``. A store nobody
could read answers by the rule each reader states.

A function here whose first parameter is ``self`` is a ``_TaskqBridgeMixin`` method: the class
binds it under the same name in ``admission/taskq_bridge.py``, so it reaches the manager through
``self._manager`` and its sibling members through ``self``, and a class-level patch of a member
reaches every call made through ``self`` or the class. The functions with no ``self`` are this
module's store-side helpers, not members.

The manager fields this module uses. Writes: ``_queued_listing_partial``. Reads:
``_admitting_waiting``, ``_agents``, ``_queue``, ``_queue_wait``, ``_retained_claims``,
``_undurable_in_dispatch``.
"""

from __future__ import annotations

import dataclasses as _dataclasses
import functools as _functools
import logging as _logging
from typing import TYPE_CHECKING, Any, Collection, Mapping, Sequence

from kiro_crew.subagent_wait_reasons import RESUMING_AFTER_RESTART, RESUMING_RETRY

from .types import QueuedReadUnavailable, QueuedRun, QueuedRunListing

if TYPE_CHECKING:
    # Typing only. ``taskq`` loads on first use, as everywhere in the bridge, and the
    # facade imports this module at run time.
    from kiro_crew import taskq as _taskq

    from .taskq_bridge import _TaskqBridgeMixin

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


#: What a store-only queued count answers while the store cannot be read: SOME
#: waiting work, never none. Every consumer of that count is a fail-closed
#: predicate -- the attached-children guard before a session teardown
#: (``dashboard.chat_utils.subagents_attached``), the cron reset-deferral guards
#: (``has_pending_work_for``), the Slack pending probe -- and 0 is the one answer
#: that lets them strand an accepted child's completion on a cold-started
#: replacement session. The queue-depth chip does not read this answer: its
#: reader, :meth:`_TaskqBridgeMixin.taskq_chip_overflow_async`, publishes nothing at
#: all while the store cannot be read.
UNKNOWN_PENDING = 1


#: Most store rows one queued listing returns. A listing is a page for a reader,
#: not an inventory a caller acts on, so the oldest this many are enough; the
#: Stop-all cascade has its own unbounded read (``taskq_pending_ids_for``). A
#: listing that stopped here says so (``QueuedRunListing.partial``), since a
#: cut-off tail otherwise reads as spawns that were never accepted.
QUEUED_LISTING_CAP = 100


#: The events that close a deferral: a claim or a state change after the
#: ``deferred`` event means the sentence does not describe the row's current wait.
_DEFER_CLOSERS = ("claimed", "transition")


def _defer_details(store: "_taskq.TaskStore", recs: "list[_taskq.TaskRecord]") -> dict[str, str]:
    """Each still-parked row's deferral sentence, from ONE batched event read.

    A row's sentence is reported only while the deferral is in force
    (``next_run_at`` in the future) AND its ``deferred`` event is newer than
    the row's last claim or transition: a ``recovering`` backoff or a
    dependency park also sets ``next_run_at``, and an older memory-gate
    sentence does not describe either wait.
    """
    now = store.now()
    parked = [r.id for r in recs if r.next_run_at is not None and r.next_run_at > now]
    latest = store.latest_events(parked, ("deferred", *_DEFER_CLOSERS))
    out: dict[str, str] = {}
    for rid in parked:
        deferred = latest.get((rid, "deferred"))
        if deferred is None:
            continue
        closed_at = max(
            (latest[(rid, k)].seq for k in _DEFER_CLOSERS if (rid, k) in latest), default=-1
        )
        if deferred.seq > closed_at:
            out[rid] = str(deferred.data.get("reason") or "")
    return out


def _read_queued_row(
    store: "_taskq.TaskStore", agent_id: str
) -> "tuple[_taskq.TaskRecord, str] | None":
    """Store read for the by-id lookup: the row when it is accepted, not started."""
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
    from kiro_crew import taskq as _taskq

    try:
        rec = store.get(agent_id)
    except ValueError as exc:
        # A row this build cannot model (a state, kind or class written by a
        # newer one): unreadable, the outage answer, never an escaped 500.
        _glue_logger.warning("taskq: row %s is unreadable by this build: %s", agent_id, exc)
        raise _taskq.TaskStoreUnavailable(f"row {agent_id} unreadable") from exc
    if rec is None or rec.kind != _taskq.KIND_SUBAGENT:
        return None
    if rec.state not in _taskq.CLAIMABLE | {_taskq.ADMITTED}:
        return None
    return rec, _defer_details(store, [rec]).get(rec.id, "")


def _read_queued_rows(
    store: "_taskq.TaskStore",
    *,
    session_key: str | None,
    app: str | None,
    exclude_ids: list[str],
    registered_ids: tuple[str, ...] = (),
) -> "tuple[list[tuple[_taskq.TaskRecord, str]], bool]":
    """Store read for :meth:`_TaskqBridgeMixin.taskq_queued_runs_async`.

    Runs on the writer thread. Returns the rows and whether the listing stopped
    at :data:`QUEUED_LISTING_CAP` with more rows left: one row past the cap is
    read to tell the two apart. *app* narrows the read in SQL, before the cap,
    so an app's page is its own rows and the flag says nothing about others'.
    """
    # Lazy, as everywhere in this module: the bridge loads taskq on first use.
    from kiro_crew import taskq as _taskq

    try:
        recs = store.list_pending(
            _taskq.KIND_SUBAGENT,
            session_key=session_key,
            exclude_ids=exclude_ids,
            limit=QUEUED_LISTING_CAP + 1,
            include_admitted=True,
            exclude_admitted_ids=registered_ids,
            app=app,
        )
    except ValueError as exc:
        _glue_logger.warning("taskq: a queued row is unreadable by this build: %s", exc)
        raise _taskq.TaskStoreUnavailable("queued rows unreadable") from exc
    truncated = len(recs) > QUEUED_LISTING_CAP
    recs = recs[:QUEUED_LISTING_CAP]
    details = _defer_details(store, recs)
    return [(rec, details.get(rec.id, "")) for rec in recs], truncated


def _in_memory_queued_entries(manager: Any) -> list[dict[str, Any]]:
    """The accepted entries *manager* holds in memory with no registered run.

    The dispatch window (``_queue``) and the entries with no durable row the
    pump holds while it re-checks them (``_undurable_in_dispatch``): popped
    from the window, they are in neither the window nor the registry until
    the gate starts, re-queues or refuses them, and that re-check awaits
    off-loop reads. The cancellation paths read the same two halves. A copy,
    since the loop changes both while a caller iterates.
    """
    return [*manager._queue, *manager._undurable_in_dispatch.values()]


def _overflow_query(
    self: _TaskqBridgeMixin, parent_session_key: str | None, exclude_ids: list[str] | None = None
) -> dict[str, Any]:
    """The ``count_pending`` arguments every overflow entry takes (the
    "accepted, no run yet" definition), snapshotted from manager state on the
    caller's thread (the loop, for the async ones). *exclude_ids* defaults to
    :meth:`taskq_excluded_ids`; the chip passes its own set, and also leaves
    out ``recovering`` rows (:meth:`taskq_chip_overflow_async`)."""
    return {
        "exclude_ids": self.taskq_excluded_ids() if exclude_ids is None else exclude_ids,
        "session_key": parent_session_key,
        "include_admitted": True,
        "exclude_admitted_ids": list(self._manager._agents),
    }


def taskq_overflow(self: _TaskqBridgeMixin, parent_session_key: str | None = None) -> int:
    """Accepted rows with no run that live only in the store (outside the window).

    Claimable rows and ``admitted`` ones nothing registered: a claim the
    pump awaits, or one retained across a store outage
    (``_retained_claims``), is this parent's accepted work exactly like a
    queued row, and no other probe would count it.

    A store that cannot be read answers :data:`UNKNOWN_PENDING`, not 0: no
    store at all is a queue with no rows in it, while a locked or full one is
    a queue whose rows nobody can see, and only the first of those is
    evidence that this parent has nothing waiting.

    A row the pump has popped and not yet claimed is counted: it is still
    this parent's accepted work, which is what the guard that holds a
    parent's reset while its children wait asks about. The chip's reading
    leaves it out (:meth:`taskq_chip_overflow_async`).
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return 0
    try:
        return store.count_pending(_taskq.KIND_SUBAGENT, **self._overflow_query(parent_session_key))
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning(
            "taskq: queued count for %s unreadable; answering some",
            parent_session_key or "<any parent>",
            exc_info=True,
        )
        return UNKNOWN_PENDING


async def taskq_overflow_async(
    self: _TaskqBridgeMixin, parent_session_key: str | None = None
) -> int:
    """:meth:`taskq_overflow` with its store count on the writer thread.

    The exclusion set is in-memory manager state, so it is snapshotted HERE
    on the loop and handed over, never read from the writer thread -- the
    same split :meth:`taskq_child_registered_async` makes. The outage answer
    is the sync entry's.
    """
    count = await self.taskq_overflow_or_none_async(parent_session_key)
    return UNKNOWN_PENDING if count is None else count


async def taskq_overflow_or_none_async(
    self: _TaskqBridgeMixin, parent_session_key: str | None = None
) -> int | None:
    """:meth:`taskq_overflow_async`, answering None for a store nobody could read.

    For the one reader that must tell "children are waiting" from "nobody
    could look" -- the synthesis fire gate, which re-checks on an outage
    rather than waiting for a completion that may never come.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return 0
    try:
        return int(
            await store.run(
                store.count_pending,
                _taskq.KIND_SUBAGENT,
                **self._overflow_query(parent_session_key),
            )
        )
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning(
            "taskq: queued count for %s unreadable; answering some",
            parent_session_key or "<any parent>",
            exc_info=True,
        )
        return None


def taskq_chip_excluded_ids(self: _TaskqBridgeMixin) -> list[str]:
    """The rows the queue-depth chip leaves out of its store count.

    The refill's never-claim set (:meth:`taskq_dispatch_excluded_ids`) with
    one difference: a row a ``spawn_async`` caller is still admitting is
    left out only until the gate has queued it (``_admitting_waiting``).
    The refill must never claim such a row, but once it is deferred or
    behind the cap it IS waiting, and the gate's own labelled request for
    it must count it -- leaving it out would publish 0 for a wave whose
    rows all wait, and drop the label with it.
    """
    return self.taskq_dispatch_excluded_ids(counted=self._manager._admitting_waiting)


async def taskq_chip_overflow_async(self: _TaskqBridgeMixin, parent_session_key: str) -> int | None:
    """The store half of the queue-depth chip's count, or ``None`` when the
    store cannot say.

    The chip's one reader, excluding :meth:`taskq_chip_excluded_ids`. Like
    :meth:`taskq_overflow` it counts ``admitted`` rows no run is registered
    for (a claim retained across an outage is still waiting work). Unlike
    it, a ``recovering`` row is not counted: it is a run that had started
    before its owner was lost (a gateway restart), waiting to be rebuilt,
    not a spawn waiting to start -- the queued listing names it "waiting to
    resume", and counting it would put "N waiting to start" on the card
    after every restart. Work still owed (the pending-work guards) keeps it. The
    exclusion sets are snapshotted here, on the loop, before the read; on
    the inline pump (``pump_off_loop`` off) the read itself runs here too.

    An unreadable store answers ``None``, not :data:`UNKNOWN_PENDING`: the
    chip then publishes nothing, because any number it sent would be a
    guess. ``TaskStoreUnavailable`` is the outage and is logged at DEBUG
    (the caller retries); anything else is a defect and is logged as one.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return 0
    try:
        count = _functools.partial(
            store.count_pending,
            _taskq.KIND_SUBAGENT,
            **self._overflow_query(parent_session_key, self.taskq_chip_excluded_ids()),
            include_recovering=False,
        )
        return int(await store.run(count) if type(self).pump_off_loop else count())
    except _taskq.TaskStoreUnavailable:
        _glue_logger.debug(
            "taskq: queue depth for %s unreadable", parent_session_key, exc_info=True
        )
    except Exception:
        _glue_logger.warning("taskq: queue depth for %s failed", parent_session_key, exc_info=True)
    return None


def taskq_pending_ids_for(self: _TaskqBridgeMixin, parent_session_key: str) -> list[str]:
    """Ids of this parent's waiting rows that are NOT in the in-memory window.

    Waiting means accepted with no run yet, the definition the overflow
    count uses: claimable rows, and ``admitted`` rows nothing has
    registered -- a claim the pump is still awaiting, or a retained one.
    Stop all cancels exactly these ids, and the claimer's generation-fenced
    ``admitted -> starting`` write is refused for a row cancelled here, so a
    claimed row never starts after the stop. A claim retained across a
    store outage whose row is ``starting`` (``_retained_claims``) is waiting
    work too, with no run and no claimer in flight, so its id is named here
    as well.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None or not parent_session_key:
        return []
    exclude = self.taskq_excluded_ids()
    registered = list(self._manager._agents)
    try:
        rows = store.list_pending(
            _taskq.KIND_SUBAGENT,
            session_key=parent_session_key,
            exclude_ids=exclude,
            include_admitted=True,
            exclude_admitted_ids=registered,
        )
        ids = [r.id for r in rows]
        retained = self._retained_claim_ids_for(parent_session_key)
        if retained:
            ids += self._retained_starting_ids(store, retained, [*ids, *exclude, *registered])
    except _taskq.TaskStoreUnavailable:
        return []
    return ids


def _retained_claim_ids_for(self: _TaskqBridgeMixin, parent_session_key: str) -> list[str]:
    """Ids of *parent_session_key*'s claims held in ``_retained_claims``.

    Loop-owned state: read on the loop, and handed to the store read."""
    return [
        agent_id
        for agent_id, entry in list(self._manager._retained_claims.items())
        if entry[0].parent_session_key == parent_session_key
    ]


def _retained_starting_ids(
    store: "_taskq.TaskStore", retained_ids: Sequence[str], skip: Collection[str]
) -> list[str]:
    """Each of *retained_ids* not in *skip* whose row is ``starting``.

    A store job that reads no manager state: the ids come from the loop."""
    from kiro_crew import taskq as _taskq

    found: list[str] = []
    for agent_id in retained_ids:
        if agent_id in skip:
            continue
        rec = store.get(agent_id)
        if rec is not None and rec.state == _taskq.STARTING:
            found.append(agent_id)
    return found


async def taskq_pending_ids_for_async(
    self: _TaskqBridgeMixin, parent_session_key: str, *, include_window: bool = False
) -> list[str]:
    """:meth:`taskq_pending_ids_for` with its store read on the writer thread.

    *include_window* is the parent-end teardown's reading
    (``cancel_for_teardown_impl``): the window's rows too. The teardown
    unqueues a window entry together with its row, and an entry the refill
    hydrated after the teardown's snapshot was named by nobody; the
    snapshot's fence, not the window, is what tells a retired
    conversation's row from one its successor under the same key queued.
    That reading leaves ``admitted`` rows out: the teardown refuses a
    claimed-not-started row (``allow_admitted=False``), so naming one would
    only mark it in the delivery gate and the sweep's audit as stopped.

    A refused read is the reading's to decide. The teardown's reading lets
    :class:`TaskStoreUnavailable` propagate: "nothing to stop" would release
    the fence over rows nobody stopped, so the teardown keeps the fence and
    owes the sweep instead. The default reading (``cancel_for_parent``)
    answers ``[]``.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None or not parent_session_key:
        return []
    exclude = self.taskq_excluded_ids(window=not include_window)
    registered = list(self._manager._agents)
    try:
        rows = await store.run(
            store.list_pending,
            _taskq.KIND_SUBAGENT,
            session_key=parent_session_key,
            exclude_ids=exclude,
            include_admitted=not include_window,
            exclude_admitted_ids=registered,
        )
        ids = [r.id for r in rows]
        # The default reading names a retained claim's ``starting`` row too.
        # The retained ids are read here, on the loop, after the pending
        # read, and handed to the store job.
        retained = [] if include_window else self._retained_claim_ids_for(parent_session_key)
        if retained:
            ids += await store.run(
                self._retained_starting_ids,
                store,
                retained,
                [*ids, *exclude, *registered, *self._manager._agents],
            )
    except _taskq.TaskStoreUnavailable:
        if include_window:
            raise
        return []
    return ids


def _live_run_ids(self: _TaskqBridgeMixin) -> list[str]:
    """Ids with a live run in this process: the registry answers for them.

    The ONE spelling of "a run exists", shared by the refill's exclusions
    and the queued reads, so "accepted, no run yet" means one thing.
    """
    # Copy before filtering: the refill runs this on the store's writer
    # thread while the loop registers and retires runs.
    return [aid for aid, info in list(self._manager._agents.items()) if not info.done]


async def taskq_queued_run_async(self: _TaskqBridgeMixin, agent_id: str) -> QueuedRun | None:
    """The accepted spawn *agent_id* when no run exists for it yet, else None.

    The store row may be claimable or ``admitted``: a claim the pump is
    awaiting (or a retained one) has left ``queued`` but is not registered,
    and answering "not found" for it is the gap this read closes. A
    registered id is never answered here; the registry answers it. An entry
    with no store row answers from :func:`_in_memory_queued_entries`. An
    unreadable store answers from those entries too, and when they hold no
    such entry raises :class:`QueuedReadUnavailable`: "not
    queued" is not knowable then, and a caller must not read it as gone.
    """
    from kiro_crew import taskq as _taskq

    manager = self._manager
    if agent_id in manager._agents:
        return None
    store = self.taskq_store()
    outage: Exception | None = None
    if store is not None:
        try:
            found = await store.run(_read_queued_row, store, agent_id)
        except _taskq.TaskStoreUnavailable as exc:
            _glue_logger.warning(
                "taskq: queued row %s unreadable; answering from the window",
                agent_id,
                exc_info=True,
            )
            found, outage = None, exc
        if found is not None and agent_id not in manager._agents:
            rec, detail = found
            return self._queued_run_from_row(rec, detail)
    for params in _in_memory_queued_entries(manager):
        if str(params.get("_preassigned_id") or "") == agent_id and not params.get("_resume_id"):
            return self._queued_run(
                agent_id, params, str(params.get("parent_session_key") or ""), 0.0, ""
            )
    if outage is not None and agent_id not in manager._agents:
        raise QueuedReadUnavailable(str(outage)) from outage
    return None


async def taskq_queued_runs_async(
    self: _TaskqBridgeMixin, parent_session_key: str | None = None, *, app: str | None = None
) -> QueuedRunListing:
    """Accepted spawns no run exists for yet, oldest first.

    Both halves of the queue are read: the dispatch window (``_queue``) and
    the store's rows, which include every gate-deferred row, every row
    waiting outside the window and every claimed, unregistered one.
    *parent_session_key* ``None`` means every parent; *app* narrows to one
    owning app inside the store read.

    The listing is ``partial`` when it stopped at :data:`QUEUED_LISTING_CAP`
    or the store could not be read: an outage leaves the window half, and a
    listing cannot answer it with invented rows, but it must not read as
    complete either. The transition into a partial listing is logged once.
    """
    from kiro_crew import taskq as _taskq

    manager = self._manager
    rows: list[tuple[_taskq.TaskRecord, str]] = []
    truncated = unreadable = False
    store = self.taskq_store()
    if store is not None:
        try:
            rows, truncated = await store.run(
                _read_queued_rows,
                store,
                session_key=parent_session_key,
                app=app,
                exclude_ids=self._live_run_ids(),
                registered_ids=tuple(manager._agents),
            )
        except _taskq.TaskStoreUnavailable:
            unreadable = True
    partial = truncated or unreadable
    if partial != manager._queued_listing_partial:
        manager._queued_listing_partial = partial
        if partial:
            _glue_logger.warning(
                "taskq: queued listing is partial (more than %d store rows, or the "
                "store is unreadable)",
                QUEUED_LISTING_CAP,
            )
    out: list[QueuedRun] = []
    seen: set[str] = set()
    live = set(self._live_run_ids())  # re-read: a run may have registered meanwhile
    for rec, detail in rows:
        if rec.id in live or (rec.state == _taskq.ADMITTED and rec.id in manager._agents):
            continue
        out.append(self._queued_run_from_row(rec, detail))
        seen.add(rec.id)
    # In-memory entries the store did not return: legacy in-memory and
    # restricted work, which has no row (in the window, or held by the pump
    # while it re-checks the entry), and every entry while the store is
    # unreadable. A ``_resume_id`` entry belongs to a resident run, not to an
    # unstarted one. A page cut at the cap may simply not have reached an
    # entry's row, so a truncated store half adds none.
    for params in [] if truncated else _in_memory_queued_entries(manager):
        aid = str(params.get("_preassigned_id") or "")
        if not aid or params.get("_resume_id") or aid in seen or aid in manager._agents:
            continue
        parent = str(params.get("parent_session_key") or "")
        if parent_session_key is not None and parent != parent_session_key:
            continue
        if app is not None and str(params.get("app") or "") != app:
            continue
        out.append(self._queued_run(aid, params, parent, 0.0, ""))
        seen.add(aid)
    return QueuedRunListing(tuple(out), partial)


def _queued_run_from_row(
    self: _TaskqBridgeMixin, rec: "_taskq.TaskRecord", detail: str
) -> QueuedRun:
    from kiro_crew import taskq as _taskq

    params = dict(rec.params)
    parent = rec.session_key or str(params.get("parent_session_key") or "")
    resuming = ""
    if rec.state == _taskq.RECOVERING:
        resuming = RESUMING_AFTER_RESTART
    elif rec.state == _taskq.RETRY_WAIT and rec.attempts > 0:
        resuming = RESUMING_RETRY
    run = self._queued_run(
        rec.id, params, parent, rec.created_at, "" if resuming else detail, resuming
    )
    return _dataclasses.replace(run, state=str(rec.state))


def _queued_run(
    self: _TaskqBridgeMixin,
    agent_id: str,
    params: Mapping[str, Any],
    parent: str,
    accepted_at: float,
    detail: str,
    resuming: str = "",
) -> QueuedRun:
    manager = self._manager
    label = manager._queue_wait.get(parent) or {}
    reason = "" if resuming else str(label.get("reason") or "")
    return QueuedRun(
        id=agent_id,
        task=str(params.get("task") or ""),
        parent_session_key=parent,
        agent=str(params.get("agent") or params.get("crew") or ""),
        app=str(params.get("app") or ""),
        accepted_at=float(accepted_at or 0.0),
        reason=reason,
        reason_detail=detail,
        resuming=resuming,
    )


def taskq_batch_pending(self: _TaskqBridgeMixin, batch_id: str) -> bool:
    """True when a store-only queued row belongs to *batch_id*.

    True as well while the store cannot be read, for
    :data:`UNKNOWN_PENDING`'s reason: the wave's own bookkeeping is PRUNED
    when the digest closes, so a digest closed over members nobody could see
    is not one wrong message -- a SECOND digest can then fire for the same
    batch. Holding the wave open costs a later digest instead.

    Two readers take the answer with OPPOSITE polarity and True is the safer
    failure for both. ``_sweep_stuck_waves`` skips its reconcile on True, and
    the next sweep re-examines the wave. ``_sweep_digest_holds`` forces the
    partial digest out on True -- which it reaches only for a hold already
    past ``DIGEST_HOLD_SECS`` -- so the price is a chunk that may race the
    wave-close flush, against the alternative of leaving every finished
    sibling's result undelivered for as long as the outage lasts.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None or not batch_id:
        return False
    try:
        rows = store.fetch_pending_by_batch(
            _taskq.KIND_SUBAGENT, batch_id, exclude_ids=self.taskq_excluded_ids()
        )
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning(
            "taskq: batch %s membership unreadable, holding the wave", batch_id, exc_info=True
        )
        return True
    return bool(rows)


async def taskq_batch_pending_async(self: _TaskqBridgeMixin, batch_id: str) -> bool:
    """:meth:`taskq_batch_pending` with its store read on the writer thread."""
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None or not batch_id:
        return False
    exclude = self.taskq_excluded_ids()
    try:
        rows = await store.run(
            store.fetch_pending_by_batch,
            _taskq.KIND_SUBAGENT,
            batch_id,
            exclude_ids=exclude,
        )
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning(
            "taskq: batch %s membership unreadable, holding the wave", batch_id, exc_info=True
        )
        return True
    return bool(rows)
