"""Liveness probe for the event loop's default executor.

The loop-stall watchdog (:mod:`kiro_crew.dashboard.loop_watchdog`) sees a loop
that stops running callbacks. It cannot see the opposite failure: the loop keeps
answering while every worker of its default executor is blocked (a disk stall
that never returns, a wedged mount), so every ``asyncio.to_thread`` and
``run_in_executor(None, ...)`` call queues forever. Then tool-policy reads time
out, turns never start, and every health signal the loop itself produces stays
green.

:class:`ExecutorStallProbe` rides the event-loop heartbeat. Each tick it keeps
exactly one no-op submitted to the default executor; one probe at a time, so a
stalled pool never collects a backlog of them. When that no-op has waited
:data:`EXECUTOR_STALL_REPORT_SECS` for a worker it logs every thread's stack at
ERROR, once per stall.

A busy pool is not a stuck one, so the exit asks for evidence of no progress as
well as a long wait. From the report on, each tick samples which work item every
default-executor worker is running (:func:`default_worker_items`). A worker that
picked up a different item since the last sample means the pool is still moving
work, and the no-progress clock restarts. The process exits only once the no-op
has waited :data:`EXECUTOR_STALL_EXIT_SECS` AND no worker has changed items for
:data:`EXECUTOR_STALL_NO_PROGRESS_SECS`, and only while the gateway's loop-stall
watchdog is running (the real gateway entrypoint, never a process that only
hosts the dashboard). It ends with :data:`EXECUTOR_STALL_EXIT_CODE` so the
service manager or the desktop app starts a fresh gateway. The exit is a hard
``os._exit``: an orderly shutdown runs its stop hooks through the same stalled
executor and would never finish.

The stacks are rendered on the loop from :func:`sys._current_frames`, which
works on every platform, without reading any source file. Writing them out
(the log record, and the full faulthandler dump to stderr) happens on a thread
of its own, because the device behind stderr or the log file may be the one that
stalled. The exit runs on that thread once the report is written, and the loop
also schedules it :data:`_EXIT_REPORT_GRACE_SECS` later, so a report that never
finishes writing cannot hold the restart. Nothing is written into the
crash-dump file: that file is how the next boot reports an event-loop stall,
and this is not one.
"""

from __future__ import annotations

import asyncio
import faulthandler
import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures.thread import _WorkItem
from types import FrameType

from kiro_crew.executors import DEFAULT_EXECUTOR_THREAD_PREFIX
from kiro_crew.platform_compat import awake_now

logger = logging.getLogger("kiro_crew.dashboard.executor_watchdog")

#: Seconds the probe's no-op may wait for a default-executor worker before the
#: stall is logged with every thread's stack. Long blocking work runs on its own
#: pools (:mod:`kiro_crew.executors`), so a healthy default executor picks the
#: no-op up in milliseconds.
EXECUTOR_STALL_REPORT_SECS: float = 60.0

#: Seconds the no-op must have waited before the gateway may exit. Well past the
#: report, and far short of the hour a stalled pool was observed to stay stuck.
EXECUTOR_STALL_EXIT_SECS: float = 300.0

#: Seconds no default-executor worker may change work items before the gateway
#: may exit. A pool that is merely saturated by slow work keeps changing items
#: and is never restarted for it.
EXECUTOR_STALL_NO_PROGRESS_SECS: float = EXECUTOR_STALL_EXIT_SECS - EXECUTOR_STALL_REPORT_SECS

#: The exit status of a stall exit: non-zero, so a service manager restarts it.
EXECUTOR_STALL_EXIT_CODE: int = 1

#: How long the exit waits for its report to be written before it goes anyway.
_EXIT_REPORT_GRACE_SECS: float = 5.0

#: Most threads and frames per thread a rendered dump carries, so one dump of a
#: process holding a hundred threads stays one readable log record.
_MAX_THREADS = 100
_MAX_FRAMES_PER_THREAD = 30

#: The code object every pool worker runs one submitted call inside; a new
#: frame of it on a worker is a new work item. ``_WorkItem`` is a CPython
#: private: if it stops wrapping each call, every worker reads as idle and only
#: a worker appearing or exiting counts as progress, which
#: ``test_a_no_op_queued_behind_a_parked_worker_reads_as_a_stall_without_progress``
#: catches by asserting a busy worker has an item.
_WORK_ITEM_CODE = _WorkItem.run.__code__

#: A worker thread's ident mapped to the frame of the work item it is running,
#: or ``None`` while it is idle. Frames are compared by identity; holding them
#: keeps a finished frame from having its id reused by the next item's.
WorkerItems = dict[int, FrameType | None]


def _noop() -> None:
    """The probe's work item: returns at once once a worker picks it up."""


def _default_worker_idents() -> dict[int, str]:
    return {
        t.ident: t.name
        for t in threading.enumerate()
        if t.ident is not None and t.name.startswith(DEFAULT_EXECUTOR_THREAD_PREFIX)
    }


def default_worker_items() -> WorkerItems:
    """Which work item each default-executor worker is running right now."""
    frames = sys._current_frames()
    items: WorkerItems = {}
    for ident in _default_worker_idents():
        frame = frames.get(ident)
        item = None
        while frame is not None:
            if frame.f_code is _WORK_ITEM_CODE:
                item = frame
                break
            frame = frame.f_back
        items[ident] = item
    return items


def _progressed(before: WorkerItems, after: WorkerItems) -> bool:
    """True when any worker started, ended or changed a work item between samples."""
    if before.keys() != after.keys():
        return True
    return any(before[ident] is not after[ident] for ident in before)


def format_thread_stacks(
    *, max_threads: int = _MAX_THREADS, max_frames: int = _MAX_FRAMES_PER_THREAD
) -> str:
    """Every thread's stack, newest frame last, without reading a source file."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    frames = sys._current_frames()
    lines: list[str] = []
    for ident, frame in list(frames.items())[:max_threads]:
        lines.append(f"Thread {names.get(ident, '?')} (0x{ident:x}), most recent call last:")
        stack = [
            (f.f_code.co_filename, lineno, f.f_code.co_name)
            for f, lineno in traceback.walk_stack(frame)
        ][:max_frames]
        for filename, lineno, name in reversed(stack):
            lines.append(f'  File "{filename}", line {lineno}, in {name}')
    if len(frames) > max_threads:
        lines.append(f"... {len(frames) - max_threads} more thread(s) not shown")
    return "\n".join(lines)


def _faulthandler_to_stderr() -> None:
    try:
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
    except Exception:
        logger.debug("faulthandler dump to stderr failed", exc_info=True)


def _run_off_loop(work: Callable[[], None]) -> None:
    """Run *work* on a daemon thread of its own, never on an executor."""
    threading.Thread(target=work, name="mc-executor-stall-report", daemon=True).start()


def _call_later(delay: float, callback: Callable[[], None]) -> object:
    return asyncio.get_running_loop().call_later(delay, callback)


class ExecutorStallProbe:
    """Detects a default executor that stopped running work, and restarts on it.

    Args:
        armed: Whether a stall may end the process. Asked only once a stall
            has met both exit conditions; the heartbeat passes the loop-stall
            watchdog's ``is_running``.
        report_after: Seconds of waiting before the stacks are logged.
        exit_after: Seconds of waiting before the process may exit.
        no_progress_after: Seconds without a worker changing items before the
            process may exit.
        now: Clock the wait is measured on, injectable for tests. Defaults to
            :func:`kiro_crew.platform_compat.awake_now`, which a host suspend
            does not advance.
        submit: Submits the no-op and returns its future, injectable for tests.
            Defaults to the running loop's ``run_in_executor(None, ...)``.
        worker_items: Samples the workers' current items, injectable for tests.
            Defaults to :func:`default_worker_items`.
        render_stacks: Returns the rendered stacks, injectable for tests.
        write_report: Writes the full faulthandler dump, injectable for tests.
        run_off_loop: Runs the report writer away from the loop, injectable for
            tests. Defaults to a daemon thread of its own.
        call_later: Schedules the exit's fallback on the loop, injectable for
            tests.
        exit_process: Ends the process with a status, injectable for tests.
            Defaults to :func:`os._exit`.
        log: Logger, injectable for tests.
    """

    def __init__(
        self,
        *,
        armed: Callable[[], bool],
        report_after: float = EXECUTOR_STALL_REPORT_SECS,
        exit_after: float = EXECUTOR_STALL_EXIT_SECS,
        no_progress_after: float = EXECUTOR_STALL_NO_PROGRESS_SECS,
        now: Callable[[], float] = awake_now,
        submit: "Callable[[], asyncio.Future[None]] | None" = None,
        worker_items: Callable[[], WorkerItems] = default_worker_items,
        render_stacks: Callable[[], str] = format_thread_stacks,
        write_report: Callable[[], None] = _faulthandler_to_stderr,
        run_off_loop: Callable[[Callable[[], None]], None] = _run_off_loop,
        call_later: Callable[[float, Callable[[], None]], object] = _call_later,
        exit_process: Callable[[int], object] = os._exit,
        log: logging.Logger | None = None,
    ) -> None:
        self._armed = armed
        self._report_after = report_after
        self._exit_after = exit_after
        self._no_progress_after = no_progress_after
        self._now = now
        self._submit = submit or (lambda: asyncio.get_running_loop().run_in_executor(None, _noop))
        self._worker_items = worker_items
        self._render_stacks = render_stacks
        self._write_report = write_report
        self._run_off_loop = run_off_loop
        self._call_later = call_later
        self._exit_process = exit_process
        self._log = log or logger
        self._pending: asyncio.Future[None] | None = None
        self._submitted_at = 0.0
        self._completed_at: float | None = None
        self._reported = False
        # The workers' items at the last sample that showed progress, and when.
        self._items: WorkerItems | None = None
        self._items_at = 0.0
        self._exit_lock = threading.Lock()
        self._exiting = False
        self._exited = False

    def _completed(self, _future: "asyncio.Future[None]") -> None:
        self._completed_at = self._now()

    def tick(self) -> None:
        """Check the probe in flight, or submit the next one. Called on the loop.

        Never raises: the heartbeat that calls it also beats the loop-stall
        watchdog, and a probe failure must not stop that.
        """
        try:
            self._tick()
        except Exception:
            self._log.debug("default executor probe failed", exc_info=True)

    def _tick(self) -> None:
        pending = self._pending
        if pending is not None and not pending.done():
            self._check_waiting(self._now() - self._submitted_at)
            return
        if self._reported:
            # The done callback may not have run yet; then it finished just now.
            completed_at = self._now() if self._completed_at is None else self._completed_at
            self._log.warning(
                "default executor recovered: its liveness probe waited %.0fs for a worker",
                completed_at - self._submitted_at,
            )
        self._reported = False
        self._items = None
        self._completed_at = None
        try:
            future = self._submit()
        except RuntimeError:
            # The executor is shut down: the loop is closing.
            self._pending = None
            self._log.debug("default executor probe not submitted", exc_info=True)
            return
        self._submitted_at = self._now()
        future.add_done_callback(self._completed)
        self._pending = future

    def _idle_for(self, now: float) -> float:
        """Sample the workers; return how long none of them has changed items."""
        items = self._worker_items()
        if self._items is None or _progressed(self._items, items):
            self._items, self._items_at = items, now
        return now - self._items_at

    def _check_waiting(self, waited: float) -> None:
        if self._exiting:
            return
        if waited < self._report_after:
            return
        now = self._now()
        idle_for = self._idle_for(now)
        if not self._reported:
            self._reported = True
            self._report(
                "default executor stalled: a no-op has waited %.0fs for one of its %d "
                "worker thread(s), so to_thread work (tool policy, turn starts) is stuck; "
                "the gateway restarts if it is still stuck at %.0fs with no worker "
                "progress for %.0fs. Thread stacks:\n%s",
                waited,
                len(_default_worker_idents()),
                self._exit_after,
                self._no_progress_after,
                exit_code=None,
            )
            return
        if waited < self._exit_after or idle_for < self._no_progress_after:
            return
        if not self._armed():
            return
        self._exiting = True
        self._report(
            "default executor stalled: a no-op has waited %.0fs for one of its %d "
            "worker thread(s) and none of them has started new work for %.0fs. "
            "Exiting with status %d to be restarted. Thread stacks:\n%s",
            waited,
            len(_default_worker_idents()),
            idle_for,
            EXECUTOR_STALL_EXIT_CODE,
            exit_code=EXECUTOR_STALL_EXIT_CODE,
        )

    def _exit_once(self, code: int) -> None:
        """Exit from whichever of the report thread and the loop's fallback is first."""
        with self._exit_lock:
            if self._exited:
                return
            self._exited = True
        self._exit_process(code)

    def _report(self, message: str, *args: object, exit_code: int | None) -> None:
        """Log *message* with the stacks off the loop, then exit when asked."""
        stacks = self._render_stacks()

        def _write() -> None:
            try:
                self._write_report()
                self._log.error(message, *args, stacks)
            finally:
                if exit_code is not None:
                    self._exit_once(exit_code)

        if exit_code is not None:
            self._call_later(_EXIT_REPORT_GRACE_SECS, lambda: self._exit_once(exit_code))
        self._run_off_loop(_write)
