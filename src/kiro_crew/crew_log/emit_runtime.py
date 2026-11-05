"""The emitter's runtime: the collaborators it installs and every function that reads them.

The durable writer, the turn tracker and the warning budget are built on first use and retired
by :func:`reset_caches`, which also resets the child-origin loss counters. Each of those is a
module global that a ``global`` statement rebinds, and such a write lands in one module's
namespace only, so every function that rebinds or reads one lives here, together with the
functions that call back into them: the handle cache that is the writer's ``open_unit``
adapter; the submit and settle path every append takes (:func:`_write`, :func:`_submit`,
:func:`_hand_over`, :func:`_settle_rejected`); the supersede repair (:func:`_queue_tail_repair`,
:func:`_repair_superseded`, :func:`_walk_to_predecessor`); the loss counters, :func:`flush` and
:func:`drain_for_shutdown`; :func:`live_turn`; the child-origin pins; and
:func:`on_session_closed`.
"""

from __future__ import annotations

import atexit
import sys
import time
import traceback
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Final

from kiro_crew.crew_log.emit_state import (
    _KIND,
    _MAX_OPEN_CREW_LOGS,
    _SRC_ACP,
    _SRC_GATEWAY,
    _child_origin,
    _creation_failed,
    _crew_log,
    _growth_listeners,
    _install_lock,
    _last_class,
    _last_config,
    _lock,
    _note_eager,
    _open,
    _repair_walked,
    enabled,
    logger,
)
from kiro_crew.crew_log.emit_tree import (
    _candidate_is_same_slot,
    _durable_previous,
    _tree_settle_hooks,
)
from kiro_crew.executors import crew_log_executor

if TYPE_CHECKING:  # pragma: no cover -- typing only; the runtime imports stay gated
    from concurrent.futures import Executor

    # Type-only, so the boot-path import gate is untouched: this name exists for the
    # checker and for test_crew_log_exc_info_sites.py, which reads annotations to decide
    # whether a frame can hold a handle. A handle passed in as ``Any`` is invisible to it.
    from kiro_crew.crew_log.store import CrewLog
    from kiro_crew.crew_log.turn_tracker import TurnTracker
    from kiro_crew.crew_log.writer import CrewLogWriter, WarningBudget, WriteJob, WriterLimits


#: How many child origins were dropped while their child was still running, so
#: the entries that pin would have carried are absent from that session's log.
#: Counted rather than only logged: a hole an append-only reader cannot see is the
#: one loss this module refuses, and this sits beside the other counts a shutdown
#: report shows. Reaching it takes the whole cap's worth of children live at once.
_lost_child_origins = 0

#: Whether a lost origin is named in the log already, so the state is reported
#: once rather than once per eviction.
_lost_origin_reported = False

#: True once the backstop ``atexit`` drain has been registered. Deliberately NOT
#: cleared by ``reset_caches``: the registration is a property of this process, not
#: of the writer's state, and clearing it would let a second registration stack up
#: another drain on exit.
_shutdown_hook_registered = False


#: Answers "is this ``agent_id`` still running IN THIS PROCESS", registered by the
#: gateway because it owns the subagent manager and this module cannot reach it.
#: Consulted only by the resume repair, to decide whether an unmatched
#: ``subagent/spawned`` may be closed. It is deliberately NOT ``_child_origin``:
#: that map is this module's own bookkeeping and ``reset_caches`` clears it, which
#: is exactly the idle-teardown path where a child keeps running -- reading it
#: would report a live child as finished. Unset means no repair closes a child.
_child_liveness: "Callable[[str], bool] | None" = None


#: The installed durable writer, the turn tracker and the shared warning budget, each
#: built on first use (see the module docstring). ``None`` until then, and again after
#: :func:`reset_caches`.
_writer_instance: "CrewLogWriter | None" = None

_tracker_instance: "TurnTracker | None" = None

_warnings_instance: "WarningBudget | None" = None

#: The limits the NEXT writer is built with, set by :func:`reset_caches` -- ``None``
#: means the writer's defaults.
_next_limits: "WriterLimits | None" = None

#: How long :func:`drain_for_shutdown` gives the writer by default. Bounded so a wedged
#: filesystem delays exit rather than hanging it.
_SHUTDOWN_DRAIN_SECONDS: Final[float] = 5.0


def _warnings() -> "WarningBudget":
    """The warning budget every crew log failure in this process is reported through."""
    global _warnings_instance
    budget = _warnings_instance
    if budget is None:
        with _install_lock:
            if _warnings_instance is None:
                from kiro_crew.crew_log.writer import WarningBudget

                _warnings_instance = WarningBudget(logger)
            budget = _warnings_instance
    return budget


def _writer() -> "CrewLogWriter":
    """The installed durable writer, built on first use.

    Its adapters are this module's: :func:`_late_handle` opens a unit for the writer's
    own loss markers, :func:`_writer_pool` hands it the one-worker crew-log pool (and
    registers the exit drain on first use), :func:`_notify_growth` tells the registered
    listeners a log grew, and :func:`_late_retry_delay` is the retry schedule. The two
    ``_late_*`` adapters read their target by name at call time, because tests patch
    :func:`_handle` and :func:`_retry_delay` here, and a writer built while a patch was
    in force must not keep it after the patch is undone. Logging stays on this module's
    logger, so every record the write path produces carries one logger name.
    """
    global _writer_instance
    writer = _writer_instance
    if writer is None:
        warnings = _warnings()
        with _install_lock:
            if _writer_instance is None:
                from kiro_crew.crew_log.writer import DEFAULT_LIMITS, CrewLogWriter

                _writer_instance = CrewLogWriter(
                    _late_handle,
                    executor=_writer_pool,
                    limits=_next_limits if _next_limits is not None else DEFAULT_LIMITS,
                    retry_delay=_late_retry_delay,
                    on_grew=_notify_growth,
                    warnings=warnings,
                    logger=logger,
                )
            writer = _writer_instance
    return writer


def _tracker() -> "TurnTracker":
    """The installed turn tracker, built on first use."""
    global _tracker_instance
    tracker = _tracker_instance
    if tracker is None:
        with _install_lock:
            if _tracker_instance is None:
                from kiro_crew.crew_log.turn_tracker import TurnTracker

                _tracker_instance = TurnTracker(max_sessions=_MAX_OPEN_CREW_LOGS, logger=logger)
            tracker = _tracker_instance
    return tracker


def _writer_pool() -> "Executor":
    """The writer's executor adapter: the crew-log pool, with the exit drain registered.

    The registration happens HERE, immediately before the pool is first asked for work,
    so ``atexit``'s last-registered-first order still runs the pool's own handler ahead of
    this module's drain (see :func:`_ensure_shutdown_hook`).
    """
    _ensure_shutdown_hook()
    return crew_log_executor()


def _late_retry_delay(attempts: int) -> float:
    """The writer's retry schedule, read through :func:`_retry_delay` at call time."""
    return _retry_delay(attempts)


def _late_handle(session_id: str) -> Any:
    """The writer's ``open_unit`` adapter, read through :func:`_handle` at call time."""
    return _handle(session_id)


def _retry_delay(attempts: int) -> float:
    """How long to wait before retry number *attempts* + 1. Seconds.

    The writer's schedule (``WriterLimits.retry_delay``), exposed here as one named
    thing because the writer this module installs reads it through this name at call
    time: a test shrinks it to zero and drives the whole attempt budget
    deterministically, instead of waiting out real backoff under load and asserting on
    a stopwatch. Called under the writer's lock, so it must not call back into the
    writer.
    """
    writer = _writer_instance
    if writer is None:
        from kiro_crew.crew_log.writer import DEFAULT_LIMITS

        return DEFAULT_LIMITS.retry_delay(attempts)
    return writer.limits.retry_delay(attempts)


def flush(timeout: float = 5.0) -> bool:
    """Wait until no append is queued. True when the queue drained in time.

    For a caller that must read the file it just wrote -- a test, a shutdown
    path -- since an entry point returns as soon as the work is HANDED to the
    writer. Never called from the turn path: waiting there would reintroduce the
    block this queue exists to remove.

    Terminates even against a filesystem that never answers: a batch the writer
    cannot write is retried a bounded number of times and then dropped, so the
    buffer reaches empty rather than holding entries no wait could ever satisfy.
    A process that never wrote has no writer, and nothing to wait for.
    """
    writer = _writer_instance
    return True if writer is None else writer.flush(timeout=timeout)


def dropped_writes() -> int:
    """How many appends were given up on after their attempt budget was spent.

    That is the only way an append is abandoned: the filesystem kept refusing it.
    Nothing is discarded for backlog depth, however deep it gets, so a non-zero
    reading always names a storage refusal rather than pressure.

    Normally 0, and a non-zero reading is a real hole in one or more session logs:
    the writer could not append those entries and stopped trying. It is counted
    rather than merely logged because the alternative to a bounded loss is an
    unbounded wait -- entries live in memory until they are written, so a wedged
    filesystem would otherwise hold them forever and make every bounded caller
    time out. A loss a caller can read is the lesser failure, and this is how it
    is read.
    """
    writer = _writer_instance
    return 0 if writer is None else writer.stats().dropped


def overflow_writes(session_id: str | None = None) -> int:
    """How many appends were rejected for crossing the buffer's memory ceiling.

    With *session_id*, only that session's rejections: a caller judging its own
    append reads this figure before and after, and another session's rejection in
    the same window must not read as its own.

    Distinct from :func:`dropped_writes`: that names a storage refusal the writer
    gave up on, this names backpressure the buffer refused to hold once it reached
    its count or byte ceiling (``WriterLimits.max_pending_count`` /
    ``max_pending_bytes``). Normally 0. A non-zero reading means the writer fell so
    far behind that the backlog became a memory-exhaustion risk, and the newest
    entries of the overwhelmed session were rejected -- at the tail, so that log is
    short by them from a named point rather than holed in its middle. Reading it
    apart from :func:`dropped_writes` is how a stuck disk is told from a saturated one.
    """
    writer = _writer_instance
    if writer is None:
        return 0
    if session_id is not None:
        return writer.overflowed_for(session_id)
    return writer.stats().overflowed


def lost_child_origins() -> int:
    """How many children lost their pinned origin while they were still running.

    A pin carries the parent session and asking turn every later entry about that
    child reuses, and it is released by the child's own terminal entry. Dropping a
    live one costs that child its remaining entries: its opener when the pin goes
    before the run starts, its outcome when the pin goes after. Distinct from
    :func:`dropped_writes` and :func:`overflow_writes`, which count entries the
    writer refused; this counts attribution the map could not keep, so the entries
    are never composed at all.

    Normally 0, and reaching it needs the pin cap's worth of children running at
    once: a pin whose child has finished is dropped in preference, which is what
    keeps children lost to a crash from filling the map over long uptime.

    The same count also covers a pin REFUSED because its session id is longer than
    :data:`_MAX_SESSION_ID_CHARS`. One count for both, because the consequence a
    reader cares about is identical -- that child's entries are absent -- and the
    log line names which cause fired.
    """
    with _lock:
        return _lost_child_origins


def buffered_writes() -> int:
    """How many appends are waiting on the writer right now."""
    writer = _writer_instance
    return 0 if writer is None else writer.stats().buffered


def peak_buffered_writes() -> int:
    """The most appends ever waiting at once since the last reset."""
    writer = _writer_instance
    return 0 if writer is None else writer.stats().peak_buffered


def reset_caches(*, limits: "WriterLimits | None" = None) -> None:
    """Drop cached handles, live turns and timings. Tests and restart.

    Retires the installed writer first -- :meth:`CrewLogWriter.close` waits a bounded
    moment for what it owes, because a job still holding a stale handle would otherwise
    write after the reset that was meant to forget it, and then DISCARDS whatever it
    could not write. Reaching that point means the writer is wedged, and the handles
    those jobs would have written through are being dropped in this same call -- so
    keeping them would leave the writer retrying entries against a crew log this
    process has stopped believing in. A successful wait makes the discard a no-op,
    which is every call that is not recovering from a wedge. The writer is retired
    while still installed, so a job that queues follow-on work during that wait queues
    it into the writer being retired rather than building a new one.

    Then the writer, the turn tracker and the warning budget are uninstalled, so the
    next use builds fresh ones -- with *limits* for the next writer when given, which is
    how a test asks for small ceilings -- and this module's own maps are cleared. The
    growth listeners, the child-liveness probe and the exit-hook registration are
    properties of the process and survive.
    """
    global _writer_instance, _tracker_instance, _warnings_instance, _next_limits
    global _lost_child_origins, _lost_origin_reported
    writer = _writer_instance
    if writer is not None:
        # Bounded well below the default: this wait exists so a job holding a stale
        # handle cannot write after the reset, and it returns the instant the writer is
        # quiet. When the writer is GONE -- the pool shut down with work still owed --
        # no job can run at all, so waiting the full default would stall every caller
        # for nothing and the discard is the answer either way.
        writer.close(timeout=2.0)
    with _install_lock:
        _writer_instance = None
        _tracker_instance = None
        _warnings_instance = None
        _next_limits = limits
    with _lock:
        _open.clear()
        _last_config.clear()
        _last_class.clear()
        _child_origin.clear()
        _creation_failed.clear()
        _repair_walked.clear()
        _lost_child_origins = 0
        _lost_origin_reported = False


def _report(what: str, exc: BaseException, *, op: str) -> None:
    """Report a crew log failure through the shared warning budget.

    One budget for the whole write path, writer and emitter alike, so a kind of failure
    is named once per window however many sites hit it -- see
    :class:`kiro_crew.crew_log.writer.WarningBudget` for the key and the text-only
    record rule. Takes the budget's own lock, so it is never called with :data:`_lock`
    held.
    """
    _warnings().report(what, exc, op=op)


def add_growth_listener(listener: "Callable[[str], None]") -> None:
    """Call *listener* with a session id after that session's log GROWS.

    The one signal a consumer of this stream needs and cannot get from the file:
    that there is something new to read. It fires once per drained batch rather
    than once per entry, because the write-behind already groups a turn's burst
    into one pass, and a listener woken per entry would do the same work several
    times over the same read.

    Registered rather than imported: this module is imported BY the dashboard, so
    calling into a dashboard publisher from here would close an import cycle and
    would put a consumer's name in the writer's own code. A listener that raises
    is reported like a failed write and cannot stop the drain.

    It runs on the WRITER thread, so a listener that does real work must hand it
    to its own loop. Registering the same callable twice registers it twice; the
    gateway installs its publisher once, at startup.
    """
    with _lock:
        _growth_listeners.append(listener)


def _notify_growth(session_id: str) -> None:
    """Tell every listener *session_id* has new entries. Never raises."""
    with _lock:
        listeners = list(_growth_listeners)
    for listener in listeners:
        try:
            listener(session_id)
        except Exception as exc:  # pragma: no cover - a listener's own failure
            _report("growth listener", exc, op="growth-listener")


def _submit(
    job: Callable[[], None],
    what: str,
    session_id: str,
    nbytes: int = 0,
    *,
    after: Callable[[], None] | None = None,
    on_permanent_drop: Callable[[], None] | None = None,
) -> None:
    """Hand one ordinary append to the writer.

    The writer's contract applies (:meth:`kiro_crew.crew_log.writer.CrewLogWriter.submit`):
    a lifecycle record is never dropped for backpressure alone, the event loop is never
    blocked, and with no event loop running the write happens inline, ordered against
    the writer. ``after`` runs once the append lands or is definitively dropped;
    ``on_permanent_drop`` runs, ahead of it, only when it is given up on.
    """
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        session_id,
        WriteJob.append(job, what, nbytes=nbytes, after=after, on_drop=on_permanent_drop),
    )


def _hand_over(session_id: str, job: "WriteJob") -> None:
    """Submit *job* for *session_id*, and settle it here if the ceiling rejected it."""
    if _writer().submit(session_id, job):
        return
    _settle_rejected(session_id, job)


def _settle_rejected(session_id: str, job: "WriteJob") -> None:
    """Finish a job the writer rejected at its memory ceiling. Never raises.

    The rejected entry never entered the buffer, so nothing will ever run its cleanup
    unless it runs here -- release the pin it holds now, and let the writer keep
    draining the entries that did fit. A ceiling rejection is a PERMANENT loss of this
    entry, exactly like a spent retry budget, so the tail repair that stood down for a
    dropped terminal must be re-queued here too: a terminal handed over for a
    superseded crew log carries :func:`_terminal_dropped`, and its loss is the trigger
    that re-queues the repair. Skipping it leaves the repair debt set with nothing to
    consume it, so the predecessor's tail stays open for the life of the file.

    TWO constraints pull in opposite directions, and neither ordering of "finish" and
    "fire the hook" satisfies both, because finishing does two things at once. The
    terminal's ``after`` is :func:`_closing`'s release, which (a) releases the turn's
    live pin AND (b) clears the repair debt once no live turn of the session remains.

    * Fire the hook BEFORE finishing: the hook re-queues a repair that runs on the
      WRITER thread and can observe this turn's pin still live (our finish not yet
      run), stand down, re-record the debt -- which our finish then erases, with no
      trigger left. (cross-thread race)
    * Fire the hook AFTER finishing: finishing has already cleared the debt, so the
      hook reads an empty slot and re-queues nothing.

    So neither is done blindly. The debt is read BEFORE finishing (preserved through
    the cleanup), the finish releases the live-turn pin, and the repair is re-queued
    from that reading -- now the pin is gone, so the repair closes the tail truthfully
    instead of standing down against our own dying turn. :func:`_queue_tail_repair` is
    the one submission site and is guarded, so re-queuing from the reading is exactly
    what the hook would have done, minus the stale read. A job with no debt behind it
    fires its own drop hook as-is.
    """
    tracker = _tracker_instance
    owed_slot = tracker.repair_debt(session_id) if tracker is not None else ""
    if job.after is not None:
        try:
            job.after()
        except Exception as exc:
            _report(f"finishing {job.what}", exc, op="finish-write")
    if owed_slot:
        # A crew-log terminal stood a repair down; its pin is released now, so re-queue
        # the repair directly from the reading. The finish may already have consumed
        # the debt, so consume the reading rather than reading again.
        if tracker is not None:
            tracker.take_repair_debt(session_id)
        logger.warning(
            "session log %s: the terminal of superseded crew log %s was REJECTED "
            "at the pending ceiling, so re-queueing the tail repair that stood "
            "down for it -- the turn it deferred to has no outcome coming now, "
            "and its tail would otherwise stay open for the life of the file",
            owed_slot,
            session_id,
        )
        _queue_tail_repair(session_id, owed_slot)
    elif job.on_drop is not None:
        try:
            job.on_drop()
        except Exception as exc:
            _report(f"flagging a permanent drop of {job.what}", exc, op="flag-permanent-drop")


def _writer_owes(session_id: str) -> bool:
    """Whether the installed writer still owes *session_id* anything. Takes no lock here."""
    writer = _writer_instance
    return writer is not None and writer.owes(session_id)


def _ensure_shutdown_hook() -> None:
    """Register the backstop drain once, on the first pass that needs a writer.

    Registered HERE rather than at import for the boot-path rule: a launch with
    the flag unset never reaches a drain pass, so it registers nothing. Ordering
    is preserved because ``atexit`` runs handlers last-registered-first and this
    runs immediately BEFORE the pool is first asked for work -- so the executor's
    own hook, registered when it builds that pool, still runs ahead of this drain.
    """
    global _shutdown_hook_registered
    with _lock:
        if _shutdown_hook_registered:
            return
        _shutdown_hook_registered = True
    atexit.register(drain_for_shutdown)


def drain_for_shutdown(timeout: float = _SHUTDOWN_DRAIN_SECONDS) -> bool:
    """Write out everything buffered, then let the eager folder finish what it was handed.

    Returns whether the LOG is complete, which is the record:
    :meth:`~kiro_crew.crew_log.writer.CrewLogWriter.drain_for_shutdown` is the writer's
    bounded quiescence barrier, and it names what it could not write. A process that
    never wrote has no writer and nothing to drain. The fold step after it is part of
    the quiescence barrier and not of that answer: the eager folder runs on its own
    thread and writes savepoints into the data home, so a caller that tears the home
    down (a test's temp directory, a gateway exiting) right after this call would
    otherwise race a fold still reading or writing there. It is waited for within what
    is left of *timeout*, with a short floor so a writer that spent the whole budget
    does not skip it entirely, and only when that module is loaded -- a process that
    never folded eagerly pays nothing. A fold that does not settle in time costs a
    savepoint, never an entry.
    """
    started = time.monotonic()
    writer = _writer_instance
    drained = True if writer is None else writer.drain_for_shutdown(timeout).drained
    eager = sys.modules.get("kiro_crew.crew_log.eager")
    settle = getattr(eager, "drain", None)
    if settle is not None:
        budget = max(started + timeout - time.monotonic(), _EAGER_SETTLE_FLOOR_SECONDS)
        try:
            if not settle(budget):
                logger.debug("crew log eager folds still in flight %.1fs into shutdown", budget)
        except Exception:  # pragma: no cover - a fold must not cost the shutdown
            # Rendered text, never ``exc_info``, for the reason ``_note_eager`` gives.
            logger.debug(
                "crew log eager settle failed at shutdown:\n%s", traceback.format_exc().rstrip()
            )
    return drained


#: The least time :func:`drain_for_shutdown` gives the eager folder to settle, even when
#: the writer spent the whole budget: one batch of folds over a busy session's tail.
_EAGER_SETTLE_FLOOR_SECONDS: Final[float] = 1.0


def live_turn(session_id: str) -> int:
    """The ordinal of *session_id*'s running turn, or 0 when none is running.

    For callers that must name the turn an entry belongs to but do not hold the
    ordinal themselves. The live record is opened by ``on_turn_started``, which
    runs BEFORE the ACP client is published on the slot -- so a caller reading a
    slot attribute assigned later in the turn sees 0 or the PREVIOUS turn's
    ordinal in that window, and attributes its entry to a turn that did not
    produce it. Reading the record closes that window.

    The HIGHEST live ordinal, because a session can hold more than one: a nested
    turn pins its own record while its parent's is still open, and the newest is
    the one currently producing entries.

    Returns 0 rather than raising when nothing is running. 0 is not a turn, so a
    caller must treat it as "no running turn" and record the fact it actually has
    -- not stamp 0 on an entry.
    """
    tracker = _tracker_instance
    if not session_id or tracker is None:
        return 0
    return tracker.live_turn(session_id)


def asking_turn(session_id: str) -> "tuple[int, int]":
    """``(turn, attempt)`` of the turn that can be asking for a dispatch, or ``(0, 0)``.

    The reader for pinning a child's origin. It differs from :func:`live_turn` in one
    case: a turn whose terminal is already queued for the writer has ended, so a
    dispatch arriving then (a cron, a hook, a slash command) has no asking turn and
    gets none. See :meth:`TurnTracker.asking_turn`.
    """
    tracker = _tracker_instance
    if not session_id or tracker is None:
        return (0, 0)
    return tracker.asking_turn(session_id)


def _bound_open() -> None:
    """Trim the handle cache. ``_lock`` held. See :meth:`TurnTracker.trim` for the rule."""
    _tracker().trim(_open, _MAX_OPEN_CREW_LOGS, lambda k: k, "open handles")


def _remember(session_id: str, log: CrewLog) -> None:
    with _lock:
        _open[session_id] = log
        _open.move_to_end(session_id)
        _bound_open()


def _handle(session_id: str) -> Any:
    """An open crew log for *session_id*, or None when it has none. Never creates one.

    The writer's ``open_unit`` adapter, and the handle every job here appends through.

    None means exactly one thing: this session has no crew log file AND its creation
    was never attempted (feature off, never opened), so an event for it is
    deliberately not written rather than starting a crew log with no header. That is
    a policy no-op, not a loss, and it is not counted as one.

    A session whose creation FAILED permanently is different: it was opened, so its
    later entries are a real loss. That case raises a refusal rather than returning
    None (see the ``_creation_failed`` branch below), so the entry is dropped and
    counted instead of vanishing as a silent no-op.

    A FAILURE to reopen is different and is allowed to propagate. This runs inside
    a queued job, so the exception reaches the writer and the entry is
    retained and retried like any other failed append -- where returning None would
    make the job succeed with nothing written, and the entry would vanish with no
    retry and no entry in :func:`dropped_writes`. That is the same silent hole the
    retention exists to close, one layer up. The ``exists`` stat is inside that
    rule too: a stat that raises has not answered whether the crew log is there, and
    reading it as absent would discard the entry on a guess.

    Runs on the writer, so the stat and the open read are off the loop like the
    append they precede.
    """
    if not session_id or not enabled():
        return None
    with _lock:
        cached = _open.get(session_id)
        if cached is not None:
            _open.move_to_end(session_id)
            return cached
        creation_failed = session_id in _creation_failed
    if not _crew_log().CrewLog.exists(_KIND, session_id):
        if creation_failed:
            # The file-creating record died permanently, so this session's crew log
            # will never exist -- but it was OPENED, so its later entries are a real
            # loss, not the silent no-op an unopened session gets. Raise a refusal
            # rather than return None: the exception reaches the writer, which reads a
            # CrewLogError as a refusal (never retried), and the entry is dropped AND
            # counted in dropped_writes, with a write/dropped marker owed, instead
            # of vanishing uncounted.
            raise _crew_log().CrewLogError(
                "session log creation failed permanently; entry cannot be recorded",
                code=_crew_log().CODE_NO_LEDGER,
            )
        return None
    # RECONNECT, never a resume: this path is reached when a handle is missing
    # from the cache, which says nothing about the writer's health -- an
    # eviction is enough. So it opens WITHOUT repair; closing a turn here
    # would close one that is still running.
    log = _crew_log().CrewLog.open(_KIND, session_id)
    _remember(session_id, log)
    return log


def _closing(session_id: str, turn: int) -> "Callable[[], None]":
    """Mark *turn*'s pin as owed to the writer, and return the release for it.

    Called where a terminal event is HANDED OVER, which is not where it lands: the
    entry is queued, and the pin it releases has to outlive the handover so eviction
    cannot take the handle before the closer is on disk. See
    :meth:`TurnTracker.closing` for the release and the repair debt it pays.

    With no tracker installed no turn of this process is pinned, so there is nothing
    to mark and nothing to release -- which is also what keeps a flag-off terminal
    from building one.
    """
    tracker = _tracker_instance
    if tracker is None:
        return _nothing_to_release
    return tracker.closing(session_id, turn)


def _nothing_to_release() -> None:
    """The release of a turn this process never pinned."""


def _write(
    session_id: str,
    entry_type: str,
    data: dict[str, Any],
    *,
    src: str = _SRC_ACP,
    after: Callable[[], None] | None = None,
    on_permanent_drop: Callable[[], None] | None = None,
    on_settled: "Callable[[bool], None] | None" = None,
    ignorable: bool = False,
) -> None:
    """Queue one entry.

    ``thread`` is never set. A session entry names its turn in ``data.turn``,
    which the runner already knows when it calls -- so nothing has to be looked
    up, cached, or read back from a line written earlier. The envelope's
    ``thread`` field points at another LINE's seq, which is only knowable for a
    unit whose anchor line is written before the entries that cite it; that is
    the crew's log shape, not this one's.

    ``ignorable`` marks an entry a reader may skip when it does not know the
    type. It is the writer's promise that nothing later in the file depends on
    this entry having been interpreted, so only an entry that samples a stream
    carries it.

    ``after`` runs once the append lands or the writer definitively drops it. A
    retryable failure leaves it attached to the retained job.

    ``on_settled`` is handed the same answer with the OUTCOME attached: True only
    when this entry's ``append`` returned. It is for a caller that must not
    publish before the append commits, and it carries :class:`_TreeSettle`'s
    reasoning -- absence of a permanent drop is not success, because an entry
    rejected at the buffer's memory ceiling finishes with no drop hook at all,
    which is precisely the wedged-writer condition the ceiling exists for.
    """
    if not session_id or not enabled():
        if after is not None:
            after()
        if on_settled is not None:
            # No record was asked for, so none is owed. A caller that must not
            # publish without one reads False and says so.
            on_settled(False)
        return

    settle = _tree_settle_hooks(on_settled) if on_settled is not None else None

    def _job() -> None:
        log = _handle(session_id)
        if log is None:
            if settle is not None:
                settle.fail()
            return
        entry = log.append(entry_type, data, src=src, ignorable=ignorable)
        if settle is not None:
            settle.wrote()
        # Here rather than at each emitter: this is the append every ordinary entry type
        # goes through, so an entry type that becomes eager later is covered without a
        # second edit. The hook's own membership test drops the types no eager fold
        # names, which is nearly all of them.
        _note_eager(entry, entry_type, session_id, data)

    def _after() -> None:
        if settle is not None:
            settle.after()
        if after is not None:
            after()

    _submit(
        _job,
        f"appending {entry_type}",
        session_id,
        after=_after if (settle is not None or after is not None) else None,
        on_permanent_drop=settle.fail if settle is not None else on_permanent_drop,
    )


def _queue_tail_repair(previous_sid: str, slot: str, *, walked: "tuple[str, ...]" = ()) -> None:
    """Queue the tail repair for *previous_sid*. The ONE submission site for it.

    Four things reach this: the supersede that names the predecessor, a re-attach
    recovering a job a crash lost, a dropped terminal that a stand-down had
    deferred to, and the predecessor walk stepping from a log it just repaired to
    that log's own predecessor. They differ only in what brought them here, never
    in what is submitted, so the guards, the bucket and the ceiling exemption cannot
    drift apart between them. *walked* is the walk's own state -- the logs it has
    already repaired, newest first -- and is empty for every caller but the walk.

    Queued under the PREDECESSOR's id, which IS the deferral: the writer runs a
    session's jobs in submission order, so this cannot run until everything that crew
    log already owes has been attempted -- written, or dropped and admitted in a
    marker. A ``WriteJob.follow_on``, which is the writer's whole policy for such a job:
    never written inline (the callers that queue it from a job are on the writer
    thread, where the inline path would wait for the pass it is inside), never refused
    at the memory ceiling (the opening entry that queues it is exempt, so under pressure
    the successor's crew log would be created while its follow-on repair is refused),
    and submitted ONCE more if its whole retained batch is dropped, behind the loss
    marker, without being counted as lost.
    """
    from kiro_crew.crew_log.writer import WriteJob

    _hand_over(
        previous_sid,
        WriteJob.follow_on(
            lambda: _repair_superseded(previous_sid, slot, walked=walked),
            "closing a superseded crew log's interrupted tail",
        ),
    )


def _terminal_dropped(session_id: str) -> "Callable[[], None]":
    """The hook a terminal carries so its DROP re-queues a repair that stood down.

    Paired with :func:`_closing` at the same three handover sites, and separate from it
    because the two answer different questions. ``after`` runs when the append is
    RESOLVED, written or given up on alike, so it cannot tell those apart; a
    permanent-drop hook fires only on the giving up, which is the one outcome that
    makes a stand-down wrong in hindsight.

    A repair that stood down for a running turn is correct exactly while that turn's
    terminal is still coming. Once the terminal is gone the turn has no outcome coming
    at all, and leaving the stand-down in place keeps a ``turn/started`` open for the
    life of a file nothing rewrites.

    The returned callable does nothing unless this session actually owes a repair, so
    the ordinary terminal -- no supersede, nothing waiting -- pays one dict read.
    """

    def _dropped() -> None:
        tracker = _tracker_instance
        slot = tracker.take_repair_debt(session_id) if tracker is not None else ""
        if not slot:
            return
        logger.warning(
            "session log %s: the terminal of superseded crew log %s was DROPPED, so "
            "re-queueing the tail repair that stood down for it -- the turn it "
            "deferred to has no outcome coming now, and its tail would otherwise stay "
            "open for the life of the file",
            slot,
            session_id,
        )
        _queue_tail_repair(session_id, slot)

    return _dropped


def _repair_superseded(previous_sid: str, slot: str, *, walked: "tuple[str, ...]" = ()) -> None:
    """Close *previous_sid*'s interrupted tail. WRITER THREAD, never a caller's.

    The repair half of a supersede. The successor's ``session/opened`` only NAMES
    the crew log the slot was writing before; this closes that crew log's own
    dangling ``turn/started`` as ``turn/completed {stop_reason: "interrupted"}``
    and its open ``tool/called`` frames as ``status: "unknown"``, which is what
    makes a fold able to tell an interrupted turn from one still running. Without
    it every gateway restart leaves one permanently open tail per active slot, and
    that turn's cost, duration and outcome are absent from every reading of the
    file for the rest of its life.

    **Then it walks on.** A repair that lands goes on to *previous_sid*'s own
    predecessor (:func:`_walk_to_predecessor`), so a tail a crash left open further
    back in the chain is closed by the next cold start rather than never. *walked*
    is that walk's state, the logs it already repaired, and only the walk passes it.
    A repair that stands down or refuses below ends the walk with it.

    **The deferral is the queue, not a decision.** This job is submitted into
    ``previous_sid``'s OWN bucket, and the writer runs a session's jobs in
    submission order -- so it cannot run until everything already owed for that
    crew log has been attempted. That is the whole ordering requirement, and
    taking it from the buffer rather than from a fresh predicate is what makes it
    resumable by construction: a real ``turn/completed`` still queued or retrying
    at supersede time is written BEFORE this runs, and one abandoned after its
    attempt budget is spent is dropped and admitted in a ``write/dropped`` marker
    before this runs. Either way the tail is closed exactly once and the file
    never carries two outcomes for one turn. Asking at create time instead --
    whether the predecessor still owes anything, standing down while it does --
    leaves nothing that can re-run it: a superseded id is never resumed and
    nothing maps to it once the successor takes over, so precisely the crew log
    that most needs repairing is the one left open for good.

    **The candidate is re-verified adjacent to the write.** The edge already
    refused to NAME a crew log whose own header does not name this slot, so a
    forged mapping entry cannot reach this function through
    :func:`on_session_opened`. What CAN change between that decision and this one
    is the crew log itself: the deferral window lasts as long as the predecessor's
    outstanding writes do, and retention can collect the unit inside it. Asking
    again covers both -- the accessor's ``None`` means "cannot prove", which a
    collected crew log and a foreign one answer alike -- so a unit that is gone is
    a quiet stand-down rather than a ``no_ledger`` refusal the writer would drop
    and count as a lost append, reporting a hole in a file that is gone.
    Asking again is also what keeps the guarantee local: this is the one place an
    outcome is authored into a unit that is not this session's own, and a check
    whose failure costs a foreign outcome belongs next to the write rather than
    inherited from a caller two decisions away. The same
    :func:`_candidate_is_same_slot` the edge uses answers it, against the
    candidate's own immutable header -- one spelling of the rule, so the two
    cannot drift.

    A live turn of ours for that id stands the repair down, exactly as it does on
    the resume path and for the same reason: the turn is still producing entries,
    so closing it would record an outcome it never had and then be followed by the
    rest of it. Standing down is not a hole here, because such a turn ends in its
    own real ``turn/completed``, which closes the tail truthfully.

    Its live record is read as it stands, never released first. ``closer_owed`` is
    set where a terminal is HANDED OVER, so a turn still running is indistinguishable
    from a leaked record by that field alone, and releasing on it would drop the
    record of a turn a forced reset tore down mid-flight -- the one case whose
    closer arrives later, from its own ``finally``. ``on_session_closed`` preserves
    those records for exactly this reason; a release here would undo that and let
    this job write an outcome ahead of a real one. A predecessor whose turn was
    still running at supersede time and whose real closer is later DROPPED is
    covered: the stand-down records the debt with the turn tracker and the terminal's
    own permanent-drop hook (:func:`_terminal_dropped`) re-queues this job once its
    outcome is given up on, so the tail is closed rather than left open. The debt is
    cleared only once the session has no live turn left, so a nested turn landing
    first cannot pay it out from under a sibling still running.

    No ``child_gone`` predicate is passed, so an unmatched ``subagent/spawned`` is
    left OPEN. A superseded crew log's children were dispatched by a session that
    is gone, and this process cannot answer for another process's children: a child
    still running can file its own real terminal, and a synthesised ``unknown``
    ahead of it would leave two outcomes for one ``agent_id`` in a file nothing
    rewrites. Leaving the opener unmatched leaves a reader one fact short; closing
    it can leave a reader wrong.
    """
    # Live records are LEFT ALONE, which is the same rule `on_session_closed`
    # applies to them and for the same reason: a forced reset tears a session down
    # MID-TURN, that turn goes on running, and its closer is handed over later by
    # its own ``finally``. Releasing such a record here would take back the one
    # signal that says so -- ``closer_owed`` is set at handover, so a turn still
    # running reads exactly like a leaked record -- and ``live_turn`` would then
    # report 0 and let this job write ``interrupted`` ahead of a real
    # ``turn/completed`` that is still coming. That is the two-outcomes-for-one-turn
    # hazard, in a file nothing rewrites.
    # Whether a live turn stands the repair down, and the debt recorded when one
    # does, are read and written in ONE critical section of the tracker's
    # (`TurnTracker.stand_down`), for the window its docstring names. Standing down
    # is right only while that terminal is still COMING, and it may instead spend its
    # attempt budget and be dropped. The debt is recorded against the predecessor so
    # the drop can re-queue this job: the terminal's drop hook is the one site that
    # learns the deferral ended badly, and without the debt it has no way to know a
    # repair was waiting.
    running = _tracker().stand_down(previous_sid, slot)
    if running:
        logger.warning(
            "session log %s: NOT closing the interrupted tail of superseded crew "
            "log %s -- turn %d is still running for it in this process, so closing "
            "it would record an outcome that turn never had and then be followed "
            "by the rest of it",
            slot,
            previous_sid,
            running,
        )
        return
    if not _candidate_is_same_slot(previous_sid, slot):
        # Also the answer for a unit RETENTION COLLECTED while this job waited, and
        # deliberately the same branch: the accessor reports None for a crew log
        # that is gone as readily as for one whose header names another slot, and
        # both mean "not known to be this slot's predecessor". A separate existence
        # check ahead of it can change no outcome, because this branch already
        # returns before the open that would raise ``no_ledger``.
        logger.warning(
            "session log %s: NOT closing the interrupted tail of %s -- its own "
            "header does not name this slot, or the crew log is gone, so it is not "
            "known to be this slot's predecessor",
            slot,
            previous_sid,
        )
        return
    # Opened WITHOUT ``repair`` and repaired through the method, for the count:
    # ``open(repair=True)`` does the same work and returns a handle rather than how
    # many closers landed, and a repair that closed nothing is worth telling apart
    # from one that closed a turn and three calls.
    log = _crew_log().CrewLog.open(_KIND, previous_sid)
    closed = log.repair_interrupted_turn()
    # Dropping the handle is what releases the write ownership
    # ``repair_interrupted_turn`` took: the lease is bound to the object's lifetime,
    # so holding it any longer would keep a crew log nothing is writing owned by
    # this process. Released BEFORE the walk below queues the next hop, so a long
    # walk never holds more than the one log it is repairing.
    del log
    if closed:
        logger.info(
            "session log %s: closed %d dangling entr%s on superseded crew log %s",
            slot,
            closed,
            "y" if closed == 1 else "ies",
            previous_sid,
        )
    _walk_to_predecessor(previous_sid, slot, walked)


#: How many crew logs ONE predecessor walk repairs, counting the one it starts on.
#: Each hop opens and scans a whole crew log on the writer thread, so this is what
#: keeps one walk's cost finite whatever the chain holds. Far below
#: :data:`~kiro_crew.crew_log.session_tree.SLOT_CHAIN_CAP`, which bounds a walk over
#: records already in memory: a tail is left open only when a crash lands between
#: a supersede's edge and its repair, so reaching a log this far back takes this
#: many consecutive processes each dying before its own walk finished. A walk that
#: reaches the bound says so and names the log it stopped before.
_REPAIR_WALK_HOPS: Final[int] = 32

#: How many repaired crew logs :data:`_repair_walked` remembers.
_MAX_REPAIR_WALKED: Final[int] = 4096


def _walk_to_predecessor(repaired_sid: str, slot: str, walked: "tuple[str, ...]") -> None:
    """Queue the repair of *repaired_sid*'s own predecessor. WRITER THREAD.

    The whole durable chain, not one hop. In A -> B -> C, a crash after B's
    opening entry named A but before A's repair ran loses only the repair: the edge is
    an append and the repair rode the in-memory buffer. B is never resumed once C
    supersedes it, so nothing re-queues A's repair from B, and C's supersede names B
    alone. Following each repaired log's own ``previous`` back is the only way a cold
    successor reaches A, and a hop whose tail was already closed says nothing about
    the hop BEFORE it, so the walk goes on past a log that needed no repair.

    The next hop is read from the repaired log's OWN opening entry, the same durable
    read the re-attach recovery uses, and each hop is a separate job queued into that
    log's own bucket through :func:`_queue_tail_repair`. So every hop gets every guard
    the first one got -- the deferral behind what that log still owes, the live-turn
    stand-down, the same-slot re-check against its immutable header -- and a hop that
    refuses ends the walk there, because a log not known to be this slot's is not one
    whose edge may be followed.

    Four stops, checked in this order:

    * **A loop.** The next hop is already on this walk. The emitter refuses to WRITE
      such an edge, but this reads files any earlier build wrote, and a damaged or
      hand-edited opening entry can name anything. Logged, because a loop is damage.
    * **Repaired already in this process** (:data:`_repair_walked`). Its own walk went
      on from it, so a second supersede on the same slot does not rescan the slot's
      whole history. Silent, because this is the ordinary case for a long-lived slot.
    * **The bound** (:data:`_REPAIR_WALK_HOPS`). Logged, naming the log the walk
      stopped before, so a tail left open past it is reported rather than hidden.
    * **A hop that is not this slot's.** Its header is read before the hop is
      queued. A log that is gone, or whose header cannot be read, ends the walk
      quietly, because retention removing the oldest logs is how an old chain
      normally ends. A header naming ANOTHER slot is damage and is logged.
    """
    walked = (*walked, repaired_sid)
    with _lock:
        _repair_walked[repaired_sid] = None
        _repair_walked.move_to_end(repaired_sid)
        while len(_repair_walked) > _MAX_REPAIR_WALKED:
            _repair_walked.popitem(last=False)
    following = _durable_previous(repaired_sid)
    if not following:
        return
    if following in walked:
        logger.warning(
            "session log %s: the predecessor chain loops back on itself -- crew log %s "
            "names %s, which this walk already repaired -- so the walk stops here; "
            "the opening entry naming it is damaged",
            slot,
            repaired_sid,
            following,
        )
        return
    with _lock:
        if following in _repair_walked:
            return
    if len(walked) >= _REPAIR_WALK_HOPS:
        logger.warning(
            "session log %s: stopped the predecessor walk at the walk's bound of %d "
            "crew logs, before %s -- its tail, and any older one, is left as it is",
            slot,
            _REPAIR_WALK_HOPS,
            following,
        )
        return
    # The same header comparison `_candidate_is_same_slot` makes, read here so the
    # two failures can be told apart. The repair re-checks it next to its write.
    from kiro_crew.crew_log.store import unit_header_slot

    owner = unit_header_slot(_KIND, following)
    if owner is None:
        # Retention removing the oldest logs is the ordinary end of a long-lived
        # slot's chain, so it ends the walk quietly: queueing the hop would only
        # have the repair warn about a gone log on every restart.
        logger.debug(
            "session log %s: the predecessor walk ends at %s -- its crew log is gone "
            "or its header cannot be read",
            slot,
            following,
        )
        return
    if owner != slot:
        logger.warning(
            "session log %s: the predecessor walk stops at %s -- crew log %s names it, "
            "but its own header names another slot, so the edge is not followed",
            slot,
            following,
            repaired_sid,
        )
        return
    _queue_tail_repair(following, slot, walked=walked)


#: How many dispatched children this module remembers an origin for. A child's
#: origin is released by its own terminal entry, so this cap is only reached by
#: children that never reach one -- a queued member cancelled before it starts,
#: a run lost to a crash. Generous, because the cost of holding one is two small
#: values and the cost of evicting one is a closer that cannot be filed.
_MAX_CHILD_ORIGINS = 2048

#: How many of the OLDEST pins are examined for a finished child before the cap
#: falls back to dropping the oldest outright. A pin is released by its child's
#: terminal entry, so one still held while its neighbours have gone belongs to a
#: child that never reported, and those collect at the old end -- which is why a
#: window there is where they are found. Bounded because answering takes a call
#: into the subagent side per pin, and scanning the whole map would charge one
#: caller the entire cap's worth of them.
_ORIGIN_REAP_SCAN = 64

#: The longest session id a pin will retain. The count cap above bounds memory
#: only if every field of a pin is bounded too, and this one is authored by the
#: provider rather than by this process. Far above any id this codebase produces
#: -- a provider session id is UUID-shaped and a channel session key is shorter
#: still -- so it rejects nothing legitimate and exists only so a broken or
#: hostile provider cannot make the count cap meaningless. An id past it is
#: refused, never shortened: an identity that has been cut down names a
#: different unit or none.
_MAX_SESSION_ID_CHARS = 512


def set_child_liveness(probe: "Callable[[str], bool] | None") -> None:
    """Register the process's own answer to "is this subagent still running".

    Called once by the gateway, which constructs the subagent manager; this module
    has no route to it and must not grow one, since the emitters are called FROM
    that side. *probe* takes an ``agent_id`` and returns True while the child can
    still report its own outcome.

    Only the resume repair reads it, and only to decide whether an unmatched
    ``subagent/spawned`` may be closed. Nothing in the crew log file can answer
    that: an unbalanced opener is what a finished-but-unreported child and a
    still-running one both look like. Leaving it unset is safe and is what tests
    and any embedder without subagents get -- no child is ever closed, so a reader
    sees an open child rather than a fabricated outcome.
    """
    global _child_liveness
    _child_liveness = probe


def _child_gone_probe(session_id: str) -> "Callable[[str], bool] | None":
    """``child_gone`` for the store, or None when this process cannot answer.

    Inverted here rather than at the registration site so the gateway registers
    the fact it actually holds -- its manager lists what is RUNNING -- instead of
    a negation that reads backwards at the call site.

    A child is reported present while this process owes ANY entry for
    *session_id*, on top of what the registry says. A terminal outcome reaches
    the file through the writer and :func:`_submit` returns before it lands, so a
    child leaves the running set while its own closer is still queued: the file
    then shows an opener with no match while the registry omits the child. Closing on that reading puts a synthesised ``unknown`` ahead of the
    real outcome and leaves both standing in a file nothing rewrites. The debt
    set is the exact record of what is still owed -- queued, claimed by the
    writer, retained, and owed loss markers alike -- and it is the one source
    here carrying no bound that
    could shed a pending child, which the manager's completed-run retention does
    carry. An entry a hard ceiling refuses is absent from the debt set, which is
    the wanted answer: that closer is never coming, so the opener is the
    repair's to close.

    The two reads live in different lock domains, so they are sampled at
    different instants and their ORDER decides whether the gap between them can
    lie. Liveness is read first for that reason.

    Neither read is enough on its own, because the normal completion path flips
    ``done`` long BEFORE the closer is recorded: the manager's running set is
    everything not done, so the child leaves it at the flip, and the terminal
    entry is handed over later from the report task. Between those two the child
    is absent from the running set and owes nothing, and a repair reading exactly
    there synthesises an ``unknown`` that then stands beside the real outcome. So
    the child's OWN origin pin is consulted too: it is opened when the spawn is
    recorded and released only once the closer has been handed to the writer, an
    interval that contains that whole gap, and it is per-child rather than
    per-session. What remains is the pin the FIFO drops under its own ceiling,
    which is counted rather than silent.
    """
    probe = _child_liveness
    if probe is None:
        return None

    def _gone(agent_id: str) -> bool:
        if probe(agent_id):
            return False
        if child_origin(agent_id)[0]:
            return False
        return not _writer_owes(session_id)

    return _gone


def remember_child_origin(agent_id: str, session_id: str, turn: int, attempt: int = 0) -> None:
    """Pin the parent session and turn that dispatched *agent_id*, unopened.

    *attempt* is which try at *turn* asked, 0 when unobserved. A regenerate or rewind
    reruns the same ordinal, so the turn alone cannot tell the two tries' children
    apart. It is kept only with a turn: an attempt of no turn names nothing.

    A child's later facts -- its ``subagent/spawned`` entry, a steer, its terminal
    outcome -- are produced after the parent's turn has ended, often while the
    parent is on a different turn entirely. Reading the parent's CURRENT turn at
    any of those points would file the child under a turn that did not ask for it,
    so the ordinal is captured once, where the dispatch was accepted and the asking
    turn is still live, and every later entry about this child reuses it.

    The pin starts UNOPENED, because being accepted is not being started: a spawn
    still has to clear the approval gate, and a decline returns without ever
    running. :func:`open_child_origin` is what promotes it, at the one site that
    means "this run is really starting" -- so a declined spawn leaves no opener,
    and the closer helpers below refuse to close what was never opened.

    Idempotent: a queued member is accepted, waits behind the stagger gate, and
    re-enters the spawn path under the SAME id, which must not move the origin it
    was accepted with, nor un-open it.

    An over-long *session_id* is REFUSED rather than stored. A cap on how many
    pins are held bounds memory only if every field of a pin is bounded too, and
    the session id is authored by the provider, not by this process. It is refused
    rather than shortened because it is an IDENTITY: a truncated id names a
    different unit or none at all, so storing a cut-down copy would file this
    child's entries against the wrong crew log. The refusal is counted like any
    other lost origin, since the consequence is the same -- that child's entries
    are absent.
    """
    global _lost_child_origins, _lost_origin_reported

    if not agent_id or not session_id:
        return
    if len(session_id) > _MAX_SESSION_ID_CHARS:
        with _lock:
            _lost_child_origins += 1
            report = not _lost_origin_reported
            _lost_origin_reported = True
        if report:
            logger.warning(
                "crew log: a child's session id exceeds %d characters, so its "
                "origin is refused and its entries will be absent; counted in "
                "lost_child_origins()",
                _MAX_SESSION_ID_CHARS,
            )
        return
    with _lock:
        if agent_id in _child_origin:
            return
        _child_origin[agent_id] = (session_id, int(turn), False, int(attempt) if turn else 0)
        if len(_child_origin) <= _MAX_CHILD_ORIGINS:
            return
        oldest = list(_child_origin)[:_ORIGIN_REAP_SCAN]
    _reap_child_origin(oldest)


def _reap_child_origin(oldest: "list[str]") -> None:
    """Make room in the pin map, dropping a finished child's pin before a live one.

    Called with ``_lock`` RELEASED. Choosing which pin to drop means asking the
    liveness probe, and that probe belongs to the subagent side; holding this
    module's non-reentrant lock across a foreign call is how a deadlock is built.
    The map is re-checked under the lock before anything is removed, so a release
    that happens in between simply leaves nothing to do.

    A pin is released by its child's terminal entry, so a pin held while its
    neighbours have gone belongs to a child that never reported one -- a run lost
    to a crash, a member cancelled before it started. Those are free to drop: the
    entries they could still carry are never coming. Dropping them is also what
    keeps this cap from being reached by accumulation over long uptime, rather than
    only by that many children genuinely running at once.

    When every candidate is still running, the oldest goes and the loss is COUNTED
    in :func:`lost_child_origins` and named in the log once. That child's opener or
    outcome will be absent, and an absence a reader cannot see is the one loss this
    module refuses to allow silently. Nothing is WRITTEN for it: the registry
    declares no type for a lost pin, and inventing one here would be a shape change
    made to describe a bug rather than a fact of the session.

    The scan is bounded, so a finished pin sitting past the window is missed and
    the oldest live pin is dropped instead. That trades an exact choice for a
    bounded cost in a state that is already pathological, and the trade is visible
    because the drop is counted either way.
    """
    global _lost_child_origins, _lost_origin_reported

    probe = _child_liveness
    finished: list[str] = []
    if probe is not None:
        for candidate in oldest:
            try:
                running = probe(candidate)
            except Exception:
                # An unanswerable probe is not an answer. Treat the child as
                # running, so a pin is never dropped on a failed read.
                logger.debug("crew log: child liveness probe failed", exc_info=True)
                continue
            if not running:
                finished.append(candidate)

    with _lock:
        if len(_child_origin) <= _MAX_CHILD_ORIGINS:
            return
        for candidate in finished:
            if _child_origin.pop(candidate, None) is not None:
                return
        _child_origin.popitem(last=False)
        _lost_child_origins += 1
        report = not _lost_origin_reported
        _lost_origin_reported = True
    if report:
        logger.warning(
            "crew log: the child-origin map is full of running children, so a "
            "live child's origin was dropped and its remaining entries will be "
            "absent; counted in lost_child_origins()"
        )


def open_child_origin(agent_id: str) -> "tuple[str, int]":
    """Mark *agent_id*'s pin OPENED and return it, or ``("", 0)`` if unknown.

    Called where the run actually begins. Returning the pinned pair rather than
    reading the parent's live turn is the whole point: this site can be reached a
    long human approval later, by which time the parent is on another turn.

    Idempotent, so a second call cannot produce a second opener.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None:
            return ("", 0)
        session_id, turn, _opened, attempt = found
        _child_origin[agent_id] = (session_id, turn, True, attempt)
        return (session_id, turn)


def child_origin_attempt(agent_id: str) -> int:
    """Which try at its pinned turn asked for *agent_id*, or 0 when unobserved.

    Read beside :func:`open_child_origin` by the ``subagent/spawned`` writer. A
    separate reader rather than a third field on the pair, because every other
    reader of a pin wants only the session and the turn.
    """
    if not agent_id:
        return 0
    with _lock:
        found = _child_origin.get(agent_id)
        return 0 if found is None else found[3]


def child_origin(agent_id: str) -> "tuple[str, int]":
    """*agent_id*'s pinned origin if its spawn was recorded, else ``("", 0)``.

    Gated on opened: an entry about a child that has no ``subagent/spawned`` line
    would be a fact with no cause, which is worse than the fact being missing.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None or not found[2]:
            return ("", 0)
        return (found[0], found[1])


def dispatch_origin(agent_id: str) -> "tuple[str, int]":
    """*agent_id*'s pinned origin whether or not its spawn was recorded.

    The one reader that is correct BEFORE the opener exists. :func:`child_origin`
    refuses an unopened pin because a fact about a child that never started would
    have no cause in the log; a spawn approval is the exception, because the wait
    for it is the cause of the gap. It happens between the dispatch being accepted
    and the run starting, and on two of its three exits no run ever starts -- so
    gating it on opened would lose exactly the prompt a reader is looking for.

    Answers ``("", 0)`` for a child with no pin at all, which is what a caller
    needs to skip the write rather than guess a parent.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.get(agent_id)
        if found is None:
            return ("", 0)
        return (found[0], found[1])


def forget_child_origin(agent_id: str) -> "tuple[str, int]":
    """Release *agent_id*'s origin and return it, or ``("", 0)``.

    Called from the child's terminal report, which is exclusive and one-shot, so
    the release happens exactly once and a second terminal cannot write a second
    closer with a resolved origin.

    Also gated on opened, and the release happens either way: a spawn declined at
    the approval gate is pinned but never opened, and its terminal report must
    close nothing while still dropping the pin rather than leaving it for the FIFO
    to evict.
    """
    if not agent_id:
        return ("", 0)
    with _lock:
        found = _child_origin.pop(agent_id, None)
        if found is None or not found[2]:
            return ("", 0)
        return (found[0], found[1])


def on_session_closed(
    session_id: str,
    reason: str,
    *,
    on_settled: "Callable[[bool], None] | None" = None,
) -> None:
    """Record a session teardown and drop its cached state.

    ``reason`` is the caller's own word for the teardown, recorded verbatim rather
    than remapped onto a second vocabulary. Three routes call it: reset,
    destroy (``destroyed`` or ``destroyed_sid_retained``), and the deliberate tab
    close (``removed``, from ``_close_slot``), each passing the same
    ``end_reason`` it records elsewhere; another teardown path passing its own
    reason needs no change here.

    This entry records a TEARDOWN, not the end of the file. A forced reset -- the
    model-switch route called with ``skip_running`` false -- tears a session down
    while a turn is still running, and that turn's closers are written later, by its
    own ``finally``, in another task; a steer already inside its RPC lands later
    still. So entries belonging to turns that were already in flight MAY follow this
    one, and a reader treats it as "the gateway stopped serving this session for
    reason R" rather than "nothing further appears".

    Holding the entry until the session went quiet was tried and abandoned: nothing
    in the gateway defines quiescence. A turn is pinned only once ``turn/started``
    is written, so an authorization await ahead of it is unpinned; eviction under
    the tracker's live-turn ceiling strands a held reason; and a resume that re-claims the id
    would have to decide whether the predecessor's teardown still happened. Each of
    those is a way to LOSE the terminal, which is worse than a terminal a late entry
    follows -- an absent one cannot be told apart from a crash.

    What this must not do is erase state a live turn still needs. The cleanup below
    drops only what a successor must not inherit; the live-turn records and the open
    tool calls of turns still running are left to their own release, so a turn whose
    session closed under it can still close its calls.

    ``on_settled`` lets a caller WAIT for this edge to commit before it publishes on
    the strength of it -- the same contract as :func:`on_session_adopted`. It is
    called once, off the caller's thread, with ``True`` once the entry is on disk and
    ``False`` when the write was dropped or there was no log to write to. Not awaited
    here (``_submit`` must never block the loop), so the bounding is the caller's --
    see :func:`awaiting_commit`. The deliberate-close path uses it so the ``removed``
    edge lands before the conductor is woken; every other caller omits it and keeps
    the fire-and-forget behaviour.
    """

    def _forget() -> None:
        tracker = _tracker_instance
        with _lock:
            # Only when no turn of this session is still running. A forced reset
            # tears a session down MID-TURN, and that turn goes on writing through
            # this handle -- which also carries the unit's write ownership, so
            # dropping it here releases the log to whichever process asks next
            # while the turn is still producing entries, and a successor's repair
            # then closes a turn that completes for real a moment later. A handle
            # left behind is not a leak: it belongs to a live turn, and the
            # capacity rule reclaims it once that turn is gone. With no tracker
            # installed, no turn of this process is live.
            if tracker is not None:
                tracker.pop_unless_live(_open, session_id)
            else:
                _open.pop(session_id, None)
            # A later session reusing this id must write its own configuration
            # rather than inheriting a closed session's as "unchanged".
            _last_config.pop(session_id, None)
            # The creation-failure flag is per SESSION, so it dies with the
            # session rather than living until the next ``reset_caches``: the
            # flag makes every later entry for this id a counted loss, and a
            # successor reusing the id creates its own crew log and must not
            # inherit that verdict. Cleared here rather than on the write path
            # because this runs as terminal cleanup, so it also runs when the
            # closing entry itself was dropped -- which is the case for exactly
            # the sessions the flag is set on.
            _creation_failed.discard(session_id)
        # The writer's overflow tally is per SESSION and is only ever read while that
        # session is writing, so it dies with the session. Left behind, a gateway that
        # runs for weeks keeps one entry per session that ever overflowed; and a
        # successor reusing the id would inherit a count it did not earn.
        writer = _writer_instance
        if writer is not None:
            writer.forget(session_id)
        # The attempt counts go with the session (a resume reseeds them from the file),
        # and so do the open calls and settled markers of turns already gone. A live
        # turn's are its own: dropping its open calls here would leave them open for the
        # life of the file, and a mid-teardown turn still suppresses its own duplicates.
        if tracker is not None:
            tracker.forget_session(session_id)

    _write(
        session_id,
        "session/closed",
        {"reason": reason},
        src=_SRC_GATEWAY,
        after=_forget,
        on_settled=on_settled,
    )


# The graceful path is the gateway's own cleanup hook, which drains in a thread
# before the process winds down. The backstop for every other exit -- a CLI run, a
# cron subprocess, a signal the server never sees -- is registered by
# ``_ensure_shutdown_hook`` on the first drain pass rather than here, so a launch
# with the flag unset registers nothing at all. See that function for why first
# use still puts this handler behind the executor's own.
