"""The board relay's consumer: decides each relayable entry off the writer thread.

The append job, on a writer lane, reads only what fixes the entry's board: the unit's
slot (noted by the writer at open; a header read only for a unit it did not open) and
the binding in force when the entry committed (one ``stat``, a read only when the file
changed). It queues that as a wake here and returns. This thread does the rest, which
the writer must not wait for: the board's newest unit (on a cold start that lists every
unit header in the store), the copy, and handing it back to the writer, one follow-on
per board unit and run of copies (:func:`work_relay.hand_over`).

A full queue is decided inline, on the writer, after everything queued before it: one
decider at a time (:data:`_decide_lock`), so a copy is never dropped and never written
ahead of one decided earlier.

The queue is in memory, like the follow-on a relay was before it: a crash after an entry
committed and before its copy landed loses that copy, as it did then. Making the relay
survive a crash is a separate change.
"""

from __future__ import annotations

import logging
import threading
import traceback
from collections import deque
from collections.abc import Mapping
from typing import Any, Final, NamedTuple

logger = logging.getLogger(__name__)

#: Wakes waiting for the consumer. Past this, the writer decides inline.
QUEUE_LIMIT: Final[int] = 4096


class Wake(NamedTuple):
    """One committed relayable entry, with the slot and board fixed at its commit."""

    unit: str
    entry_type: str
    seq: int
    data: Mapping[str, Any]
    own: str
    board: str


_cond = threading.Condition()
_queue: "deque[Wake]" = deque()
_thread: "threading.Thread | None" = None
_busy = False
#: Wakes ever queued, and wakes ever decided (inline ones included).
_queued = 0
_done = 0
#: Held by whoever decides: the consumer for a batch, or a writer deciding inline.
_decide_lock = threading.Lock()


def note(wake: Wake) -> None:
    """Queue *wake* for the consumer, or decide it here when the queue is full.

    Writer-thread time. Never raises.
    """
    global _queued
    with _cond:
        _queued += 1
        if len(_queue) < QUEUE_LIMIT and _ensure_thread_locked():
            _queue.append(wake)
            _cond.notify()
            return
    # Full, or no consumer can start (interpreter shutdown): decide inline, after what is
    # queued, so nothing is dropped or overtaken.
    _decide_batch(also=wake)


def generation() -> int:
    """How many wakes were ever noted: equal across two reads means none arrived."""
    with _cond:
        return _queued


def pending() -> int:
    """Wakes noted and not decided yet."""
    with _cond:
        return _queued - _done


def idle() -> bool:
    """Whether nothing is queued or being decided right now."""
    with _cond:
        return not _queue and not _busy and _done >= _queued


def drain(timeout: "float | None" = 5.0) -> bool:
    """Wait until every queued wake is decided and its copies handed to the writer.

    *timeout* ``None`` waits without a deadline.
    """
    import time

    deadline = None if timeout is None else time.monotonic() + timeout
    with _cond:
        _cond.notify()
        while _queue or _busy or _done < _queued:
            left = 0.05 if deadline is None else deadline - time.monotonic()
            if left <= 0 or _thread is None:
                return not (_queue or _busy) and _done >= _queued
            _cond.wait(min(left, 0.05))
    return True


def forget_for_tests() -> None:
    """Decide what is queued, then reset the counters, so one test cannot leak into the next."""
    global _queued, _done
    if not drain(10.0):
        raise AssertionError("crew log relay still busy after drain")
    with _cond:
        _queue.clear()
        _queued = _done = 0


def _ensure_thread_locked() -> bool:
    """Start the consumer if it is not running; False when it cannot be started."""
    global _thread
    if _thread is not None and _thread.is_alive():
        return True
    thread = threading.Thread(target=_run, name="crew-log-relay", daemon=True)
    try:
        thread.start()
    except RuntimeError:  # "can't create new thread at interpreter shutdown"
        return False
    _thread = thread
    return True


def _run() -> None:
    while True:
        with _cond:
            while not _queue:
                _cond.wait()
        _decide_batch()


def _decide_batch(also: "Wake | None" = None) -> None:
    """Decide every queued wake, then *also*, in order, and hand their copies over."""
    global _busy, _done
    from kiro_crew.crew_log import work_relay

    with _decide_lock:
        with _cond:
            wakes = list(_queue)
            _queue.clear()
            _busy = True
        if also is not None:
            wakes.append(also)
        out: "list[work_relay.Copy]" = []
        try:
            for wake in wakes:
                try:
                    work_relay.decide(wake, out)
                except Exception:
                    # Best effort, as every relay is: the entry being copied stands.
                    logger.debug(
                        "crew log: relaying %s from %s failed:\n%s",
                        wake.entry_type,
                        wake.unit,
                        traceback.format_exc().rstrip(),
                    )
            try:
                work_relay.hand_over(out)
            except Exception:  # pragma: no cover - a hand-over must not stop the consumer
                logger.debug("crew log relay hand-over failed:\n%s", traceback.format_exc())
        finally:
            with _cond:
                _busy = False
                _done += len(wakes)
                _cond.notify_all()
