"""A small in-process event bus for crew-log events, and the events themselves.

WHY THIS EXISTS. The eager folder produces a value several consumers want -- a socket
exporter now, a summary fold and a card trigger later -- and it must know about none of
them. Calling a dashboard broadcaster from :mod:`kiro_crew.crew_log.eager` would import
the dashboard from inside the crew log, which closes a cycle (the dashboard imports the
append path) and puts a consumer's name in the producer's code. A subscription registered
AT the consumer inverts that: the crew log publishes, and whoever cares subscribes.

WHAT IT IS NOT. A queue, a thread, or a delivery guarantee.

Fan-out is SYNCHRONOUS and on the publisher's own thread, which is the eager fold worker
-- never the append path, which still pays one ``queue.put_nowait`` and nothing else. So
a subscriber owes the same contract the emitter's growth listener owes: do no real work
here, hand it to your own loop. One that blocks blocks the folder.

A subscriber that RAISES is logged and the others still run, because a bus whose
first subscriber can silence the rest is a bus that makes every consumer depend on every
other one's bugs.

And nothing is retained: an event published with no subscriber is dropped, with no replay
and no backlog. That is safe for the same reason a dropped eager wake is safe -- the fold
is not the record, the log is -- so a lost event leaves a consumer behind the file and the
next read carries it forward.

WHO SUBSCRIBES. One subscriber today, registered where the dashboard state exists
(``install_crew_log_publisher``): the WS exporter, which turns a
:class:`FoldAdvanced` into a ``slot_projection`` or ``session_projection`` frame. Three
more are named and NOT built: a summary fold over a session's events, the automatic-card
sentence trigger, and channel
notifications. They are named here because the shape of this module is the answer to
"where does the next consumer go", and a reader asking that should not have to guess.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping
from typing import Any, Final, NamedTuple

logger = logging.getLogger(__name__)


class FoldAdvanced(NamedTuple):
    """One fold advanced to a new value, as the eager folder saw it.

    ``scope`` says what ``key`` names: ``"slot"`` for a slot-keyed fold (a board joined
    across every unit the slot ran under) and ``"session"`` for a session-keyed one (one
    unit's file, ``key`` being that unit's id). One event type for both, because a
    consumer's rule is the same for both -- keep the highest revision per
    (scope, key, fold) -- and the two revisions come from one counter.

    Published once per COALESCED BATCH per (key, fold), not once per entry: the folder
    drains its whole queue and folds each affected cell once, and this rides that. A turn
    that writes six entries therefore produces one event per fold it moved.

    ``revision`` is what ORDERS two of these for the same (key, fold), and ``seq`` is
    not. A slot fold's seq is the newest unit's own by contract, so a conductor-side
    change on a board with any worker bound leaves it unmoved, and adding a unit can move
    the value while moving the seq DOWN; a session fold's seq restarts when its unit is
    recreated under the same id. The revision is minted by the process that folded and
    never read off a file, so it rises across all of those alike. ``seq`` is carried
    anyway because it is what a reader truncates a crew-log read against.

    ``value`` is the RENDERED projection, not the fold state. A subscriber is a reader,
    and handing it the state would hand it the object the memo is still folding.
    """

    scope: str
    key: str
    fold: str
    revision: int
    value: Mapping[str, Any]
    seq: int


#: The two scopes a :class:`FoldAdvanced` names.
SCOPE_SLOT: Final[str] = "slot"
SCOPE_SESSION: Final[str] = "session"


#: The event kinds this bus carries. A string rather than the type itself, so a subscriber
#: registers against a name it can hold without importing the event's module.
FOLD_ADVANCED: Final[str] = "fold_advanced"

_lock = threading.Lock()
_subscribers: "dict[str, list[Callable[[Any], None]]]" = {}


def subscribe(kind: str, callback: "Callable[[Any], None]") -> None:
    """Call *callback* with each event published under *kind*.

    Registering the same callable twice registers it twice; the gateway installs each
    subscriber once, at startup. There is deliberately no ``unsubscribe``: nothing in the
    process has a reason to stop listening, and an unsubscribe that races a fan-out is a
    surface with no caller to justify it.
    """
    with _lock:
        _subscribers.setdefault(kind, []).append(callback)


def publish(kind: str, event: Any) -> None:
    """Hand *event* to every subscriber of *kind*, in registration order. Never raises.

    The list is COPIED under the lock and then walked outside it, so a subscriber that
    subscribes another one -- or that blocks -- cannot deadlock against the registry or
    mutate the list being iterated.

    One subscriber's exception is logged and the walk continues. The publisher is told
    nothing: it has already done the thing that mattered (the fold), and a failure to tell
    a cache or a socket about it must not reach the thread that folded.
    """
    with _lock:
        listeners = list(_subscribers.get(kind, ()))
    for listener in listeners:
        try:
            listener(event)
        except Exception:
            # Rendered to text, never as a traceback object: a record carrying frames
            # carries their callers, and those frames bind an open ``CrewLog`` whose write
            # lease is released by a finalizer when the handle is dropped. See
            # ``eager._log_exc``, which states this at length for the same reason.
            _log_exc("crew log bus subscriber for %s failed", kind)


def subscriber_count(kind: str) -> int:
    """How many subscribers *kind* has. For a test, and for a startup log line."""
    with _lock:
        return len(_subscribers.get(kind, ()))


def reset_for_tests() -> None:
    """Forget every subscriber.

    A TEST SEAM, named as one. The registry is process-wide, so a case that subscribes
    would otherwise be called by every later case in the worker -- against a data home
    that is already gone.
    """
    with _lock:
        _subscribers.clear()


def _log_exc(message: str, *args: Any) -> None:
    """Log *message* with any exception RENDERED TO TEXT. Never raises."""
    try:
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(logger, logging.WARNING, message, *args)
    except Exception:  # pragma: no cover - logging must never be the failure
        logger.warning(message, *args)
