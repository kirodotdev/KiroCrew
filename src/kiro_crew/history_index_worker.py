"""The session search index's write side, as its own process.

Why this is not a thread
------------------------
Building the index is pure-Python CPU: read a transcript, ``casefold`` it, project
its CJK inventory, hand the result to SQLite. Run through ``asyncio.to_thread``
inside the gateway — which is how it shipped — that work holds the GIL, and the
gateway's event loop needs the same GIL to do anything at all. The result was not
a slow index but a slow GATEWAY: ``event-loop heartbeat: lag 1.0-6.6s (loop was
blocked)`` under an ordinary fleet, session creation taking 47 s, and the path
gate's resolver budget expiring on plain reads. The machine was 84% idle at the
time; the process was pinned at ~1.1 cores across 143 threads, which is the GIL
ceiling, not a capacity limit. ``py-spy top --gil`` attributed 28% of all
GIL-holding samples to this indexer.

A thread pool cannot fix that, because the contended resource is the interpreter
lock rather than a core. Only a separate interpreter can, so the writer gets its
own process and the gateway keeps just the read-only query path.

Spawn, never fork
-----------------
The gateway carries 140+ threads and several GB resident. ``fork`` copies the
address space lazily but inherits only the calling thread, so any lock another
thread happened to hold at fork time is held forever in the child — the classic
fork-in-a-threaded-server deadlock — and the copy-on-write pages make the child's
memory cost unpredictable. ``spawn`` starts a fresh interpreter and re-imports
what it needs, which costs a few hundred milliseconds once and owes nothing to
the parent's thread or heap state.

That is also why this module takes PATHS and numbers, never live objects: a
``DashboardState`` or a ``ConversationLog`` cannot be pickled to a spawned child,
and trying would couple the child's startup to the gateway's. The child builds
its own ``ConversationLog`` over the same directory.

Two processes, one database
---------------------------
The index is WAL (see ``history_index``), so one writer and many readers across
processes is exactly the configuration WAL exists to serve, and SQLite's own file
locks plus ``busy_timeout`` serialize the writes.

The ordering that matters is delete-vs-reindex: a deletion must not be overtaken
by an indexer that writes the deleted session's text back. That is guarded by the
per-session lock in ``history`` (``ConversationLog._locked``), which layers a
POSIX ``flock`` on a ``.lock`` sidecar — an advisory lock held against the file,
not against the process — so it serializes this child against the gateway exactly
as it serialized two gateway threads before. ``index_session`` takes it for the
whole stat -> read -> write, so the race stays closed across the process boundary.

Deletion itself stays in the gateway on purpose. ``delete_session`` removes the
index row BEFORE unlinking the transcript and aborts the delete when the row
cannot be removed, because the index holds a copy of the session's text; that
ordering is only meaningful if the caller learns the outcome synchronously.
Routing it through this child would make a delete depend on a child that may be
restarting, and would turn a fail-closed check into a queued request.

No work queue
-------------
The child polls: it compares each window session's stat against the row that
claims it, and indexes the difference. That comparison IS the queue, and it is
self-healing — a missed notification costs one pass, a crashed child costs
nothing, and a corrupt index costs a rebuild. A notification channel would add a
second source of truth that can disagree with the files, for no correctness gain.
"""

from __future__ import annotations

import logging
import multiprocessing
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from multiprocessing.context import SpawnProcess

_LOGGER = logging.getLogger(__name__)

#: Seconds of indexing work per pass, and the pauses between passes. Carried here
#: rather than in the gateway because the loop that honours them lives here now.
#: The pass budget keeps one pass short enough that a shutdown signal is noticed
#: promptly; the busy pause is what the child uses while sessions are still
#: pending, and the idle pause once the window is fully indexed.
INDEX_PASS_BUDGET_SECS = 5.0
INDEX_BUSY_PAUSE_SECS = 2.0
INDEX_IDLE_PAUSE_SECS = 60.0

#: ``optimize`` runs on a slow multiple of the pass: FTS5 deletes leave
#: tombstones that every query pays for until segments merge.
INDEX_OPTIMIZE_EVERY_PASSES = 60

#: Longest the child sleeps without re-checking that the gateway is still there.
#: The check cannot be left to the end of a pause, because the gateway's shutdown
#: and restart paths can end the process with ``os._exit`` (see cli.py and
#: session_lifecycle.py), which runs NO ``atexit`` handler. That skips both of the
#: parent-side ways this child would otherwise be retired -- the supervisor's own
#: ``terminate`` and ``multiprocessing``'s daemon reaping -- so the only thing
#: that can retire an orphan is the orphan. Slicing the wait bounds how long one
#: can overlap its replacement, whatever the pause.
_PARENT_CHECK_SECS = 5.0

#: Restart backoff for a child that dies. Doubles from the first to the last
#: value, and resets once a child has stayed up for ``_HEALTHY_UPTIME_SECS`` — so
#: an unlucky crash restarts promptly while a crash LOOP backs off.
_RESTART_BACKOFF_MIN_SECS = 1.0
_RESTART_BACKOFF_MAX_SECS = 60.0
_HEALTHY_UPTIME_SECS = 300.0

#: How often to check that a healthy child is still alive. Short enough that a
#: death is noticed promptly, and it costs one ``is_alive`` call.
_HEALTHY_POLL_SECS = 5.0

#: Consecutive rapid deaths before the supervisor stops trying. Giving up is the
#: honest end state: search reads the transcripts when no row vouches for them,
#: so a gateway with no indexer serves the same results by the slower path it
#: used before the index existed. Restarting forever would hide that in a log.
_MAX_RAPID_RESTARTS = 5

#: How long to wait for the child to honour SIGTERM before killing it. The index
#: is WAL and every write is one transaction, so an abrupt kill cannot leave a
#: torn row -- the wait is courtesy to finish a pass, not a correctness need.
_SHUTDOWN_GRACE_SECS = 5.0


def _configure_child_logging(log_level: int) -> None:
    """Send this child's warnings to the stream the gateway's log captures.

    A spawned child inherits file descriptors but not the parent's ``logging``
    configuration, so without this its warnings go nowhere and an indexer that
    cannot write would look merely idle. ``stderr`` is inherited from the
    gateway, which is what puts these lines in ``gateway.log`` alongside the
    gateway's own.
    """
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stderr)
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")
        )
        root.addHandler(handler)
    root.setLevel(log_level)


def _wait_or_retire(stopping: threading.Event, pause_secs: float) -> bool:
    """Wait up to *pause_secs*. Return False when this child should stop.

    Three things can end the wait: the pause elapsing (return True, carry on), a
    SIGTERM setting *stopping*, or the gateway having gone away. The parent is
    re-checked every ``_PARENT_CHECK_SECS`` rather than once at the end, because
    an idle pause is a minute long and an orphan that keeps indexing for that
    minute is writing on behalf of an exited gateway, alongside its own
    replacement's child.

    Checking at all is what makes the orphan case safe. The gateway's shutdown and
    restart paths can end the process with ``os._exit``, which runs no ``atexit``
    handler, so neither the supervisor's ``terminate`` nor ``multiprocessing``'s
    daemon reaping is guaranteed to run.
    """
    deadline = time.monotonic() + pause_secs
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return True
        if stopping.wait(min(_PARENT_CHECK_SECS, remaining)):
            return False
        if _parent_is_gone():
            return False


def _pause_for(report: dict[str, int], *, busy_pause_secs: float, idle_pause_secs: float) -> float:
    """How long to wait after a pass that returned *report*.

    Paces on ``remaining``, the count of sessions waiting on THIS loop: a short
    pause picks them up, and nothing pending means wait for the disk to change.

    Read with ``get`` so a report that omits the key pauses rather than raising.
    That is also what makes this correct if ``backfill_index`` grows a separate
    count for sessions it defers because they are still being written: such a
    session is waiting on the clock rather than on this loop, and a deferral is
    deliberately kept OUT of ``remaining`` for exactly that reason, so keying on
    ``remaining`` alone already gives it the idle pause.
    """
    return busy_pause_secs if report.get("remaining", 0) else idle_pause_secs


def run_index_worker(
    sessions_dir: str,
    *,
    pass_budget_secs: float = INDEX_PASS_BUDGET_SECS,
    busy_pause_secs: float = INDEX_BUSY_PAUSE_SECS,
    idle_pause_secs: float = INDEX_IDLE_PAUSE_SECS,
    optimize_every_passes: int = INDEX_OPTIMIZE_EVERY_PASSES,
    log_level: int = logging.WARNING,
) -> None:
    """Index *sessions_dir* until told to stop. The child process's whole body.

    Every argument is a string or a number so the call survives pickling to a
    spawned interpreter. The child builds its own ``ConversationLog`` over
    *sessions_dir* rather than receiving one.

    A pass failure is logged and the loop continues: one missing row costs one
    scanned file on the next query, so the honest response to an index that will
    not build is to keep serving searches from the transcripts.
    """
    _configure_child_logging(log_level)

    stopping = threading.Event()

    def _stop(signum: int, _frame: object) -> None:
        stopping.set()

    # SIGTERM is how the supervisor asks for a clean exit. SIGINT is ignored
    # because a Ctrl-C reaches the whole process group: the gateway decides when
    # this child stops, and a KeyboardInterrupt here would race that with a
    # traceback on the way out.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, signal.SIG_IGN)

    from kiro_crew.history import ConversationLog

    log = ConversationLog(Path(sessions_dir))
    projection = log._catalog_projection
    passes = 0
    try:
        while not stopping.is_set():
            try:
                report = projection.backfill_index(budget_secs=pass_budget_secs)
            except Exception:  # noqa: BLE001 — search must survive a bad index
                _LOGGER.warning("Session search index pass failed", exc_info=True)
                # Same wait as the success path below, for the same reason: a
                # child whose passes keep failing must still notice a parent that
                # has gone, or a restart loop accumulates one orphan per gateway.
                if not _wait_or_retire(stopping, idle_pause_secs):
                    break
                continue
            passes += 1
            if passes % optimize_every_passes == 0:
                try:
                    projection.search_index.optimize()
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("Session search index optimize failed", exc_info=True)
            if not _wait_or_retire(
                stopping,
                _pause_for(
                    report,
                    busy_pause_secs=busy_pause_secs,
                    idle_pause_secs=idle_pause_secs,
                ),
            ):
                break
    finally:
        try:
            projection.search_index.close()
        except Exception:  # noqa: BLE001 — exiting anyway
            pass


def _parent_is_gone() -> bool:
    """Whether the gateway that spawned this child has exited.

    ``parent_process()`` is the direct answer and is ``None`` only in the parent
    itself. The ``getppid`` fallback covers a re-parented child: on Linux an
    orphan is adopted by init (or a subreaper), so a ppid of 1 means the original
    parent is gone.
    """
    try:
        parent = multiprocessing.parent_process()
    except Exception:  # noqa: BLE001 — treat an unanswerable check as alive
        return False
    if parent is not None:
        return not parent.is_alive()
    try:
        return os.getppid() == 1
    except OSError:
        return False


class SessionIndexWorkerSupervisor:
    """Keeps one index-writer child alive for as long as the gateway wants one.

    Separate from the gateway module so the process lifecycle is testable without
    booting a gateway: ``start`` / ``poll`` / ``stop`` are ordinary synchronous
    calls, and the gateway contributes only the loop that calls ``poll``.
    """

    def __init__(
        self,
        sessions_dir: Path,
        *,
        pass_budget_secs: float = INDEX_PASS_BUDGET_SECS,
        busy_pause_secs: float = INDEX_BUSY_PAUSE_SECS,
        idle_pause_secs: float = INDEX_IDLE_PAUSE_SECS,
        optimize_every_passes: int = INDEX_OPTIMIZE_EVERY_PASSES,
    ) -> None:
        # Normalised through ``str`` because that is exactly what is handed to
        # the child: whatever this resolves to is what the child opens, so a
        # value that is not really a path is a broken path HERE rather than a
        # directory created in the child's working directory.
        self._sessions_dir = Path(str(sessions_dir))
        self._kwargs = {
            "pass_budget_secs": pass_budget_secs,
            "busy_pause_secs": busy_pause_secs,
            "idle_pause_secs": idle_pause_secs,
            "optimize_every_passes": optimize_every_passes,
            "log_level": logging.getLogger().getEffectiveLevel(),
        }
        self._process: "SpawnProcess | None" = None
        self._started_at = 0.0
        self._rapid_restarts = 0
        self._backoff_secs = _RESTART_BACKOFF_MIN_SECS
        self._gave_up = False
        self._stopped = False

    @property
    def pid(self) -> int | None:
        """The child's pid, or ``None`` when no child is running."""
        proc = self._process
        return proc.pid if proc is not None and proc.is_alive() else None

    @property
    def gave_up(self) -> bool:
        """Whether the supervisor has stopped restarting a crash-looping child."""
        return self._gave_up

    def start(self) -> bool:
        """Spawn the child. Returns whether it is now running.

        A failure to spawn at all is reported rather than raised: the gateway's
        response to no indexer is to serve search from the transcripts, which is
        the behaviour that predates this index.
        """
        if self._stopped or self._gave_up:
            return False
        if self._process is not None and self._process.is_alive():
            return True
        if not self._sessions_dir.is_absolute() or not self._sessions_dir.is_dir():
            # The child resolves this string in ITS process, whose working
            # directory it inherited, so a relative or bogus value would be
            # created there rather than reported -- for a test run, inside the
            # checkout. Refused before the spawn: a directory that is not
            # there has nothing to index either way. Terminal, because an
            # absent transcript directory does not become one by retrying.
            _LOGGER.warning(
                "Session search index writer not started: the transcript "
                "directory is not an existing absolute path"
            )
            self._gave_up = True
            return False
        ctx = multiprocessing.get_context("spawn")
        try:
            proc = ctx.Process(
                target=run_index_worker,
                args=(str(self._sessions_dir),),
                kwargs=dict(self._kwargs),
                name="kirocrew-session-index",
                daemon=True,
            )
            proc.start()
        except Exception:  # noqa: BLE001 — no indexer is a degraded mode, not a crash
            _LOGGER.warning(
                "Session search index writer could not be started; search falls "
                "back to scanning the transcripts",
                exc_info=True,
            )
            self._process = None
            # Clear the start time with it, so the invariant is "set only while
            # a child is actually running". Leaving the DEAD child's start time
            # in place makes the next poll measure the uptime of a process that
            # is gone: a healthy child that dies, followed by spawns that keep
            # failing, would then look like a healthy death every time, reset
            # the rapid-restart count, and retry at the minimum backoff forever
            # without ever giving up -- under exactly the memory and thread
            # pressure that makes a spawn fail in the first place.
            self._started_at = 0.0
            return False
        self._process = proc
        self._started_at = time.monotonic()
        return True

    def poll(self) -> float:
        """Restart a dead child if it is time to, and say how long to wait next.

        Returns the seconds the caller should sleep before polling again. The
        supervisor owns the backoff so the caller stays a plain loop.
        """
        if self._stopped or self._gave_up:
            return _HEALTHY_POLL_SECS
        proc = self._process
        if proc is not None and proc.is_alive():
            # A child that has been up a while has proved itself; forget the
            # earlier crashes so a much later death restarts promptly.
            if time.monotonic() - self._started_at >= _HEALTHY_UPTIME_SECS:
                self._rapid_restarts = 0
                self._backoff_secs = _RESTART_BACKOFF_MIN_SECS
            return _HEALTHY_POLL_SECS
        uptime = time.monotonic() - self._started_at if self._started_at else 0.0
        exitcode = proc.exitcode if proc is not None else None
        if proc is not None:
            # Reap it fully rather than leaving the object behind: a restart loop
            # would otherwise accumulate one unclosed Process handle per death.
            proc.join(timeout=0)
            try:
                proc.close()
            except Exception:  # noqa: BLE001 — a still-running child cannot be closed
                pass
        if uptime >= _HEALTHY_UPTIME_SECS:
            self._rapid_restarts = 0
            self._backoff_secs = _RESTART_BACKOFF_MIN_SECS
        else:
            self._rapid_restarts += 1
        if self._rapid_restarts > _MAX_RAPID_RESTARTS:
            self._gave_up = True
            _LOGGER.warning(
                "Session search index writer died %d times in quick succession "
                "(last exit code %s); not restarting it again. Search falls back "
                "to scanning the transcripts.",
                self._rapid_restarts,
                exitcode,
            )
            self._process = None
            return _HEALTHY_POLL_SECS
        wait = self._backoff_secs
        self._backoff_secs = min(self._backoff_secs * 2, _RESTART_BACKOFF_MAX_SECS)
        self._process = None
        self.start()
        return wait

    def request_stop(self) -> None:
        """Ask the child to exit, WITHOUT waiting for it.

        Safe to call from the event loop: ``is_alive`` is a ``waitpid`` with
        ``WNOHANG`` and ``terminate`` only delivers SIGTERM, so neither blocks.
        This is the half a coroutine may call inline; :meth:`reap` is the half
        that waits and must go to a thread.

        Deliberately does not clear ``_process`` — ``reap`` still needs it.
        """
        self._stopped = True
        proc = self._process
        if proc is None:
            return
        try:
            if proc.is_alive():
                proc.terminate()
        except Exception:  # noqa: BLE001 — shutting down; a stuck child is not fatal
            _LOGGER.warning(
                "Session search index writer did not accept the stop signal", exc_info=True
            )

    def reap(self, *, grace_secs: float = _SHUTDOWN_GRACE_SECS) -> None:
        """Wait for the child to go and release it. BLOCKING — never on the loop.

        Terminates first in case :meth:`request_stop` was not called, then kills
        if the child does not honour the signal. Every index write is one
        transaction on a WAL database, so the kill path cannot leave a half
        written row; the grace period is courtesy to finish a pass, not a
        correctness requirement.
        """
        self._stopped = True
        proc = self._process
        self._process = None
        if proc is None:
            return
        try:
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=grace_secs)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=grace_secs)
        except Exception:  # noqa: BLE001 — shutting down; a stuck child is not fatal
            _LOGGER.warning("Session search index writer did not stop cleanly", exc_info=True)
        finally:
            try:
                proc.close()
            except Exception:  # noqa: BLE001
                pass
