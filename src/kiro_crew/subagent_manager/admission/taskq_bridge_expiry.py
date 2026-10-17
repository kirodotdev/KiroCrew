"""The max wait of a memory deferral, and the expiry reports it owes.

The sweep that ends a row parked by the memory floor for longer than
``agent.subagent_queue_max_wait_secs``, the report of that expiry to the row's parent, the owed-
report mark cleared once a report has reached the parent, and the replay of the reports an
earlier process wrote and never delivered.

A function here whose first parameter is ``self`` is a ``_TaskqBridgeMixin`` method: the class
binds it under the same name in ``admission/taskq_bridge.py``, so it reaches the manager through
``self._manager`` and its sibling members through ``self``, and a class-level patch of a member
reaches every call made through ``self`` or the class. ``_expire_memory_waits_db`` takes no
``self`` and is bound as a ``staticmethod``.

The manager fields this module uses. Writes: ``_taskq_owed_replay_after``,
``_taskq_owed_replay_failures``, ``_taskq_owed_replayed``, ``_taskq_owed_replaying``,
``_teardown_cancelled_ids``. Reads: ``_admitting_ids``, ``_cancellation``, ``_dispatching_ids``,
``_queue_dispatch_held``, ``_report_owners``, ``_subagent_queue_max_wait_secs``.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
from typing import TYPE_CHECKING, Any, Collection

from kiro_crew.subagent_wait_reasons import QUEUED_WAIT_EXPIRED_TEXT

if TYPE_CHECKING:
    # Typing only. ``taskq`` loads on first use, as everywhere in the bridge, and the
    # facade imports this module at run time.
    from kiro_crew import taskq as _taskq

    from .taskq_bridge import _TaskqBridgeMixin

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


#: Owed expiry reports one replay read returns. The replay pages until a read
#: comes back short, so this bounds one writer-thread call, not the replay.
_OWED_REPLAY_PAGE = 256


# ── the max wait of a memory deferral (``agent.subagent_queue_max_wait_secs``) ──
#
# Every re-park of a deferred row writes ``now + admit_wait``, so on its own a deferral
# renews itself for as long as the host stays short. The bound ends it: a row
# parked by the memory floor that has spent the bound
# PARKED (``TaskStore.deferred_longer_than``) is failed in the store and
# reported to its parent as ``QUEUED_WAIT_EXPIRED_TEXT``. The pump runs the
# sweep at the END of a pass, after the pass has re-parked what it
# re-checked, so a row whose re-check still found the host short is caught in
# the same pass. A row whose deferral merely lapsed waits for a slot, not for
# memory: it is not parked, so it is left alone, and the time it spends that
# way never counts toward the bound once it is parked again.


def taskq_memory_wait_bound_secs(self: _TaskqBridgeMixin) -> float:
    """The live bound, in seconds; 0 means no bound.

    One reader for both memory waits: this sweep's store deferrals and the
    macOS kernel memory-pressure hold (``_memory_pressure_holds``), which
    holds non-durable starts too, so it is the manager's value, not the store's.
    """
    return float(getattr(self._manager, "_subagent_queue_max_wait_secs", 0) or 0)


def _memory_wait_exclusions(self: _TaskqBridgeMixin) -> list[str]:
    """Rows the sweep must not end: a live run, an accept in flight, a pop in flight.

    Read on the loop, which owns these sets. Window rows are NOT excluded: a
    row there is still only queued, and ending it drops its entry too.
    """
    admitting = list(getattr(self._manager, "_admitting_ids", ()) or ())
    dispatching = list(getattr(self._manager, "_dispatching_ids", ()) or ())
    return [*self._live_run_ids(), *admitting, *dispatching]


def _expire_memory_waits_db(
    store: "_taskq.TaskStore",
    bound: float,
    exclude_ids: list[str],
) -> "list[_taskq.TaskRecord]":
    """Store half of the sweep (writer thread): fail each row past the bound.

    The read and every write share this one writer-thread call, and each
    write is fenced by the generation the read returned, so a row a claim
    took in between is refused rather than failed under its new owner. Each
    failure is written as owing its report (``report_owed``), cleared only
    once the report has reached the parent (:meth:`_clear_owed_report_after`),
    so a process lost in between, or a report that never got through, leaves
    the next one to make it (:meth:`taskq_replay_owed_expiries_async`).
    Returns the rows whose failure landed.

    Each ``finish`` commits on its own, so a refusal after the first one
    never discards what already landed: this process owes those rows their
    report, and no later read names them (the sweep's read wants a waiting
    row, the replay another incarnation's). A refused ``finish`` ends the
    loop and leaves that row and the rest queued for the next sweep. Only
    the read may raise, before anything is written.
    """
    from kiro_crew import taskq as _taskq

    ended: list[_taskq.TaskRecord] = []
    for rec in store.deferred_longer_than(_taskq.KIND_SUBAGENT, bound, exclude_ids=exclude_ids):
        try:
            landed = store.finish(
                rec.id,
                _taskq.FAILED,
                generation=rec.generation,
                error=QUEUED_WAIT_EXPIRED_TEXT,
                report_owed=True,
            )
        except _taskq.TaskStoreUnavailable:
            _glue_logger.debug(
                "taskq: memory-wait expiry of %s refused; left to the next sweep",
                rec.id,
                exc_info=True,
            )
            break
        if landed:
            ended.append(rec)
    return ended


def _sweep_inputs(self: _TaskqBridgeMixin) -> "tuple[float, list[str]] | None":
    """The bound and the exclusions; None for no sweep (no bound)."""
    bound = self.taskq_memory_wait_bound_secs()
    if bound <= 0:
        return None
    return bound, self._memory_wait_exclusions()


def taskq_expire_memory_waits(self: _TaskqBridgeMixin) -> int:
    """End every memory-deferred row past the bound; how many ended (inline twin).

    For the inline pump (no off-loop pump). Without a running loop nothing is
    ended, since the terminal report it owes needs one.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    inputs = self._sweep_inputs()
    if store is None or inputs is None:
        return 0
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return 0
    try:
        ended = self._expire_memory_waits_db(store, *inputs)
    except _taskq.TaskStoreUnavailable:
        _glue_logger.debug("taskq: memory-wait expiry failed", exc_info=True)
        return 0
    self._report_memory_waits_expired(ended, inputs[0])
    return len(ended)


async def taskq_expire_memory_waits_async(self: _TaskqBridgeMixin) -> int:
    """:meth:`taskq_expire_memory_waits` with its store half on the writer thread."""
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    inputs = self._sweep_inputs()
    if store is None or inputs is None:
        return 0
    try:
        ended = await store.run(self._expire_memory_waits_db, store, *inputs)
    except _taskq.TaskStoreUnavailable:
        _glue_logger.debug("taskq: memory-wait expiry failed", exc_info=True)
        return 0
    self._report_memory_waits_expired(ended, inputs[0])
    return len(ended)


def _report_memory_waits_expired(
    self: _TaskqBridgeMixin, ended: "list[_taskq.TaskRecord]", bound: float, *, replay: bool = False
) -> None:
    """Loop half: drop each ended row's window entry and report it.

    The report is the queued-stop report with the expiry as its ``error``, so
    batch accounting, the digest, a waiting parent's wake and the re-published
    queued depth take the path a stopped queued row already takes. A row
    ended while a teardown of its parent is open, and accepted before that
    teardown's snapshot (``accepted_before_open_teardown``), is reported to
    the dashboard but never injected: the injector creates a session when
    none is live, which would rebuild the conversation that ended. A
    successor's own rows under the same key are reported as usual. Once
    the teardown's store sweep is done, it has stopped every such row.

    Once a report has reached the parent, the row's owed report is cleared
    in the store (:meth:`_clear_owed_report_after`); a report cancelled with
    its process (the shutdown drain's stragglers) stays owed, for the next
    start. *replay* is that start.
    """
    manager = self._manager
    for rec in ended:
        params = dict(rec.params)
        parent = rec.session_key or str(params.get("parent_session_key") or "")
        params["_preassigned_id"] = rec.id
        params["parent_session_key"] = parent
        if replay:
            _glue_logger.warning(
                "taskq: reporting subagent %s's memory-wait expiry, which an earlier "
                "process wrote and did not report (%s)",
                rec.id,
                QUEUED_WAIT_EXPIRED_TEXT,
            )
        else:
            _glue_logger.warning(
                "taskq: subagent %s waited for memory longer than %.0fs; ending it (%s)",
                rec.id,
                bound,
                QUEUED_WAIT_EXPIRED_TEXT,
            )
        if manager._cancellation.accepted_before_open_teardown(parent, rec.id):
            manager._teardown_cancelled_ids.add(rec.id)
        entry = manager._unqueue(rec.id, stored=params, store_cancelled=True)
        # The sweep already wrote ``failed``: that is the row's terminal
        # write, so the report's settle writes no second one.
        report = manager._report_queued_stop(
            entry or params,
            error=QUEUED_WAIT_EXPIRED_TEXT,
            report_owed=True,
            row_settled=True,
        )
        self._clear_owed_report_after(rec.id, report)


def _clear_owed_report_after(
    self: _TaskqBridgeMixin, agent_id: str, report: "_asyncio.Task[Any] | None"
) -> None:
    """Clear *agent_id*'s owed report once *report* has reached the parent.

    At once with no report: no task means no report will run here (an empty
    id, or a finalize claim another path already holds), so nothing is left
    for this process to owe. A report whose record the gateway PARKED is not
    delivered when its task returns: a wave member held for its digest
    (``_digest_held``) or an announce queued behind a busy slot
    (``_delivery_queued``) is only in this process's memory until that
    digest or that turn reaches the parent. The gateway hands the debt on
    with it (``SubagentDelivery.report_owed``), and whichever settle
    delivers it clears the mark (:meth:`taskq_clear_owed_reports`), so a
    restart in between leaves it owed for the replay. A report that did not
    reach the parent stays owed too: one that returned False (the injection
    timed out, or ``_on_done`` raised), raised, or was cancelled with its
    process, and one whose task returned True without the gateway
    acknowledging delivery (``_report_delivered`` unset: a channel or cron
    parent's attempts all failed and the gateway swallowed that, or a route
    parked it). The next start replays it.
    """
    info = self._manager._report_owners.get(report) if report is not None else None

    def _clear(task: "_asyncio.Task[Any] | None" = None) -> None:
        if task is not None and (
            task.cancelled() or task.exception() is not None or task.result() is not True
        ):
            return
        if info is None:
            self.taskq_clear_owed_reports([agent_id])
        else:
            self.taskq_clear_owed_report_if_delivered(info)

    if report is None:
        _clear()
    else:
        report.add_done_callback(_clear)


def taskq_clear_owed_report_if_delivered(self: _TaskqBridgeMixin, info: Any) -> None:
    """Clear *info*'s owed report if the report task that just returned True delivered it.

    Only on the positive acknowledgement the delivering route sets where the
    report lands (``_report_delivered``), never on the absence of a parked
    or failed flag: a route that parks the report (a digest hold, a queued
    announce, whose settle clears it on delivery), gives up on it, or is
    added later without acknowledging it leaves the mark owed for the
    replay. A record that owes nothing is left alone.
    """
    if not info._report_owed:
        return
    if not info._report_delivered:
        return
    self.taskq_clear_owed_reports([info.id])


def taskq_clear_owed_reports(self: _TaskqBridgeMixin, agent_ids: "Collection[str]") -> None:
    """Clear the store's owed report of each of *agent_ids*: it reached the parent."""
    store = self.taskq_store()
    if store is None:
        return
    for agent_id in agent_ids:
        self._post_store_write(store, f"report cleared {agent_id}", store.mark_reported, agent_id)


async def taskq_replay_owed_expiries_async(self: _TaskqBridgeMixin) -> int:
    """Report the memory-wait expiries an earlier process wrote and never reported.

    After the store is attached, until one replay has read every owed row:
    the rows ``TaskStore.owed_reports`` names (another incarnation's, so none
    of this process's in-flight reports), a page at a time. Each is reported
    exactly as a live expiry is, and cleared only once that report has
    reached the parent (:meth:`_clear_owed_report_after`). The
    replay is marked done only once a page comes back short; a read the
    store refuses leaves it to the next call (the reaper sweep), which
    resumes after the last page reported, so no row is reported twice by
    this process. A replayed row of a parent that ended before the restart
    is reported to that key like any restored row. Nothing is replayed while the gateway
    holds dispatch for its memory barrier (``_queue_dispatch_held``): a
    report injected then fails on ``MemoryStartupUnavailable``, which the
    injector swallows, so the report would look run and its owed mark would
    be cleared undelivered. ``release_queue_dispatch`` schedules it, and
    each reaper sweep retries it. Returns the rows reported by this call.
    """
    from kiro_crew import taskq as _taskq

    manager = self._manager
    store = self.taskq_store()
    if store is None or not self._owed_replay_wanted():
        return 0
    setattr(manager, "_taskq_owed_replaying", True)
    reported = 0
    try:
        while True:
            after = getattr(manager, "_taskq_owed_replay_after", None)
            try:
                page = await store.run(
                    store.owed_reports,
                    _taskq.KIND_SUBAGENT,
                    limit=_OWED_REPLAY_PAGE,
                    after=after,
                )
            except _taskq.TaskStoreUnavailable:
                failures = int(getattr(manager, "_taskq_owed_replay_failures", 0)) + 1
                setattr(manager, "_taskq_owed_replay_failures", failures)
                # One line per outage at warning; the sweep's retries at debug.
                _glue_logger.log(
                    _logging.WARNING if failures <= 1 else _logging.DEBUG,
                    "taskq: owed expiry reports could not be read; retrying next sweep",
                    exc_info=True,
                )
                return reported
            expired = [rec for rec in page if rec.state == _taskq.FAILED]
            self._report_memory_waits_expired(
                expired, self.taskq_memory_wait_bound_secs(), replay=True
            )
            reported += len(expired)
            if len(page) < _OWED_REPLAY_PAGE:
                setattr(manager, "_taskq_owed_replayed", True)
                return reported
            setattr(manager, "_taskq_owed_replay_after", (page[-1].updated_at, page[-1].id))
    finally:
        setattr(manager, "_taskq_owed_replaying", False)


def _owed_replay_wanted(self: _TaskqBridgeMixin) -> bool:
    """True while an owed-report replay is still owed and may run now.

    Not once one replay has read every owed row, not while one is in
    flight, and not while dispatch is held for the memory barrier.
    """
    manager = self._manager
    return not (
        getattr(manager, "_taskq_owed_replayed", False)
        or getattr(manager, "_taskq_owed_replaying", False)
        or getattr(manager, "_queue_dispatch_held", False)
    )


def taskq_schedule_owed_replay(self: _TaskqBridgeMixin) -> None:
    """Post :meth:`taskq_replay_owed_expiries_async` on the running loop, if any.

    Called at every store attach (``taskq_boot_dispatch``, ``_initialize_taskq``),
    at ``release_queue_dispatch``, and from each reaper sweep, which retries a
    replay the store refused or the dispatch hold put off.
    """
    if self.taskq_store() is None or not self._owed_replay_wanted():
        return
    try:
        loop = _asyncio.get_running_loop()
    except RuntimeError:
        return
    self.track_store_task(loop.create_task(self.taskq_replay_owed_expiries_async()))
