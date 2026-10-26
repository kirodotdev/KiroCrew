"""Background primes for slot folds that have no warm cell and no usable savepoint.

WHY THIS EXISTS. A slot fold with nothing warm to continue and no savepoint to resume
folds every unit the slot ran under from its first record. On a large data home that is
seconds of pure-Python parsing per cell, and without this module it runs on whichever thread asks:
a dashboard socket's ``bus.subscribe(baseline=True)`` and the eager fold worker's
``_advance``, so every reconnect paid it again while the loop starved.

So those two callers read with ``cold=False``
(:func:`~kiro_crew.crew_log.projection_readers.read_slot_projection`). A cell that is
warm, or resumes from a savepoint, answers as before: O(entries since the savepoint).
A cell that would fold cold raises
:class:`~kiro_crew.crew_log.projection_slot_memo.SlotFoldNotReady` instead, the caller
answers "not ready" at once, and calls :func:`schedule` here.

WHAT :func:`schedule` GUARANTEES.

* ONE fold per (data home, slot, fold) at a time. Any number of subscribers and wakes
  on the same cold cell while its prime runs share that one prime.
* OFF the request path, on ONE worker thread (a single-worker executor), so primes of
  different cells queue rather than compete for the GIL with the event loop.
* The value reaches every subscriber as a normal
  :class:`~kiro_crew.crew_log.bus.FoldAdvanced` on the bus once the prime finishes. A
  subscriber registered before the publish receives it (the bus holds events that land
  during its own baseline read); one registered after it reads the now-warm cell.
* A prime that raises is logged. The next :func:`schedule` for that cell retries once;
  after a second failure in a row the cell waits :data:`RETRY_BACKOFF_SECONDS` before
  another attempt, so a broken log cannot become a hot loop.
* A request that arrives WHILE a prime runs asks for one catch-up: a warm read that
  folds what landed during the prime and is published. It folds cold again only if the
  cell is still not ready (a unit rolled over mid-fold). A cell whose last fold was too
  big to keep (``slot_fold_over_ceiling``) is not primed for an eager WAKE, which has no
  reader waiting; a READER's request (a baseline) still primes it, because that reader
  has no other way to get the value. Such a cell is folded once per reader burst.
* Each prime holds :data:`_fold_gate` while it reads, and ``eager.paused`` takes it too,
  so a caller about to unlink unit files waits for a running prime the way it waits for
  the eager worker's batch. A separate lock from the eager worker's, so a cold prime
  never holds up eager wakes.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Final

logger = logging.getLogger(__name__)

#: Wait after a SECOND consecutive failure of one cell's prime before trying it again.
RETRY_BACKOFF_SECONDS: Final[float] = 60.0
#: Most cells whose prime state is remembered at once. Entries are dropped on success,
#: so this bounds only failed cells; past it the oldest failure is forgotten.
_STATE_LIMIT: Final[int] = 4096


@dataclass
class _Prime:
    running: bool = False
    rerun: bool = False
    failures: int = 0
    failed_at: float = 0.0


_lock = threading.Lock()
#: Held across each prime's fold. ``eager.paused`` takes it beside the eager worker's own
#: gate, so unit files are not unlinked under an open prime read.
_fold_gate = threading.Lock()
#: Notified whenever a prime's state settles, for :func:`wait`.
_settled = threading.Condition(_lock)
_state: "dict[tuple[str, str, str], _Prime]" = {}
_executor: "ThreadPoolExecutor | None" = None
#: Completed folds per key since the last reset. A test seam and a log figure.
_primes_run: "dict[tuple[str, str, str], int]" = {}


def _pool() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="crew-log-slot-prime")
    return _executor


def _key(slot: str, name: str) -> "tuple[str, str, str]":
    from kiro_crew.config.paths import data_home

    return (str(data_home()), slot, name)


def schedule(slot: str, name: str, *, for_reader: bool = False) -> bool:
    """Ask for *slot*'s *name* to be primed in the background. Never raises, never folds.

    *for_reader* is ``True`` when a reader (a subscriber's baseline) is waiting for the
    value. Returns whether a new prime was submitted: ``False`` when one is already
    running for this cell (a rerun is noted instead), the cell is backing off after
    failures, or an eager wake asked for a cell too big to keep.
    """
    try:
        from kiro_crew.crew_log.projection_slot_memo import slot_fold_over_ceiling

        if not for_reader and slot_fold_over_ceiling(slot, name):
            # The last fold of this cell was too big to keep, so priming it again would
            # fold every unit for a value nobody keeps -- once per request. The eager
            # worker skips such a cell for the same reason.
            return False
        key = _key(slot, name)
        now = time.monotonic()
        with _lock:
            held = _state.get(key)
            if held is not None and held.running:
                held.rerun = True
                return False
            if (
                held is not None
                and held.failures >= 2
                and now - held.failed_at < RETRY_BACKOFF_SECONDS
            ):
                return False
            if held is None:
                held = _Prime()
                _state[key] = held
                while len(_state) > _STATE_LIMIT:
                    oldest = next(iter(_state))
                    if _state[oldest].running:
                        break
                    del _state[oldest]
            held.running = True
            held.rerun = False
        _pool().submit(_run, key, slot, name)
        return True
    except Exception:
        _log_exc("crew log slot prime could not be scheduled for %s/%s", slot, name)
        return False


def _run(key: "tuple[str, str, str]", slot: str, name: str) -> None:
    """Fold one cell cold on the prime thread, publish it, and settle its state."""
    from kiro_crew.crew_log import eager, projection

    # The prime is background folding of the same kind the eager worker does, so it
    # records the same ``fold.start`` / ``fold.apply`` points, on its own thread.
    eager._claim_thread_for_metrics()
    while True:
        ok = False
        folded: Any = None
        started = eager._begin_pass()
        try:
            with _fold_gate:
                folded = projection.read_slot_projection(slot, name)
            ok = True
        except Exception:
            _log_exc("crew log slot prime failed for %s/%s", slot, name)
        finally:
            eager._close_pass(slot=True, folds=(name,) if ok else (), started=started)
        if ok:
            with _lock:
                _primes_run[key] = _primes_run.get(key, 0) + 1
            try:
                eager._publish_fold(slot, name, folded)
            except Exception:  # pragma: no cover - the bus never raises
                _log_exc("crew log slot prime could not publish %s/%s", slot, name)
        with _lock:
            held = _state.get(key)
            if held is None:  # pragma: no cover - only reset_for_tests drops a running one
                return
            if not ok:
                held.running = False
                held.rerun = False
                held.failures += 1
                held.failed_at = time.monotonic()
                _settled.notify_all()
                return
            again = held.rerun
            held.rerun = False
            if not again:
                del _state[key]
                _settled.notify_all()
                return
        # Someone asked while this fold ran: an entry may have landed after the prime's
        # stream passed it. Catch up warm and publish, again for every request that
        # arrives during a catch-up, so none is dropped; fold cold again only if the cell
        # went cold (a unit rolled over). A subscriber's revision floor drops a value it
        # already holds.
        if _catch_up(key, slot, name):
            continue
        return


def _catch_up(key: "tuple[str, str, str]", slot: str, name: str) -> bool:
    """Warm catch-up reads until no request is pending. ``True`` when a cold fold is owed.

    Settles the cell's state itself when it returns ``False``.
    """
    from kiro_crew.crew_log import eager, projection
    from kiro_crew.crew_log.projection_slot_memo import SlotFoldNotReady

    while True:
        try:
            with _fold_gate:
                caught_up = projection.read_slot_projection(slot, name, cold=False)
        except SlotFoldNotReady:
            return True
        except Exception:
            _log_exc("crew log slot prime could not catch up %s/%s", slot, name)
        else:
            eager._publish_fold(slot, name, caught_up)
        with _lock:
            held = _state.get(key)
            if held is not None and held.rerun:
                # A request landed during this catch-up: its subscriber may have missed
                # the publish above, so read and publish once more.
                held.rerun = False
                continue
            _state.pop(key, None)
            _settled.notify_all()
            return False


def wait(slot: str, name: str, timeout: float) -> bool:
    """Wait up to *timeout* s for *slot*'s *name* prime to settle. ``True`` when settled.

    For a ONE-SHOT reader, which subscribes, reads and unsubscribes: it waits here for the
    background fold rather than folding on its own thread. The prime publishes before it
    settles, so a subscriber registered first already holds the value when this returns.
    Waiting holds no lock the fold needs and costs no CPU.
    """
    key = _key(slot, name)
    deadline = time.monotonic() + max(timeout, 0.0)
    with _settled:
        while True:
            held = _state.get(key)
            if held is None or not held.running:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _settled.wait(remaining)


def is_priming(slot: str, name: str) -> bool:
    """Whether a prime for *slot*'s *name* is queued or running. For a test and a log."""
    with _lock:
        held = _state.get(_key(slot, name))
        return held is not None and held.running


def primes_run(slot: str, name: str) -> int:
    """How many primes of *slot*'s *name* completed since the last reset. A test seam."""
    with _lock:
        return _primes_run.get(_key(slot, name), 0)


def drain(timeout: float = 30.0) -> bool:
    """Wait until no prime is queued or running. A test seam; ``True`` when idle."""
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        with _lock:
            if not any(held.running for held in _state.values()):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.01)


def reset_for_tests() -> None:
    """Forget every prime's state and count. Waits for a running prime first."""
    drain()
    with _lock:
        _state.clear()
        _primes_run.clear()


def _log_exc(message: str, *args: Any) -> None:
    """Log *message* with any exception RENDERED TO TEXT. Never raises.

    Text, never a traceback object: these frames bind open ``CrewLog`` handles, whose
    write lease a finalizer releases (see ``eager._log_exc``).
    """
    try:
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(logger, logging.WARNING, message, *args)
    except Exception:  # pragma: no cover - logging must never be the failure
        logger.warning(message, *args)
