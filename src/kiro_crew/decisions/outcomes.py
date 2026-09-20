"""The hand-off from a decision to the message that shows it.

A decision point runs deep inside prompt assembly, on a worker thread, long
before the reply it influenced exists. The chat strip has to ride on THAT reply's
message record. Nothing already connects the two: the point has a session key and
no message, and the message finalizer has a session key and no decision.

This module is that one connection, and nothing else. One outcome per POINT per
session, handed over once:

    from kiro_crew.decisions.outcomes import publish

    publish(session_key, {"turn_id": ..., "point": "skills.select", ...})

and, at the message boundary:

    from kiro_crew.decisions.outcomes import consume

    strips = consume(session_key)   # the list, once, or []

Per point, and not one per session, because two points now decide the same turn:
``skills.select`` runs during prompt assembly and ``model.route`` before the
prompt is sent, and both describe the SAME reply. One slot per session would make
the second publish erase the first, so a reader would be told about one decision
and never learn the other happened. A second publish for the same point still
replaces -- there is one such decision per turn, and the newer one is the one the
next reply belongs to.

In process, deliberately
------------------------
The registry is memory in the gateway process, not a file and not a queue. The
producer and the consumer are the same turn in the same process, microseconds
apart, so a disk round trip would buy durability nobody can use: an outcome whose
reply was never persisted is not worth showing, and the decision log
(:mod:`kiro_crew.decisions.log`) already owns the durable record. That is also
what keeps the seam's cost where the seam's docstring promises it: a session with
no decision pays one dict lookup.

Why consume POPS
----------------
The strip describes ONE turn. Leaving the outcome in place would attach the same
decision to every later reply in the session -- a stale strip is worse than no
strip, because it reads as a decision that was made on this turn. So the read is
destructive, and a second reader in the same turn correctly gets ``None``.

Three bounds, because the consumer is allowed to never arrive
------------------------------------------------------------
A published outcome is not guaranteed a reader: the turn can be cancelled, the
slot can be closed, a provider can fail before any segment is flushed. Unbounded,
that leaks one dict per abandoned turn for the life of the process. So:

* one entry per (session, point) -- a second publish for the same point replaces
  the first, because the newer decision is the one the next reply belongs to --
  and at most :data:`MAX_POINTS_PER_SESSION` points per session, oldest published
  first, so a point name a caller invents cannot grow the entry without limit;
* :data:`TTL_SECONDS` -- an outcome older than that is dropped rather than shown,
  since a strip about a decision ten minutes ago is not about the reply it would
  land on;
* :data:`MAX_SESSIONS` -- oldest published first, so a runaway producer bounds
  itself instead of the process.

And one rule on top of the bounds, enforced by the consumer's side: a turn
STARTING calls :func:`discard`, so an outcome only ever reaches the reply of the
turn that published it. The TTL alone would not do that -- a later turn that
reaches no decision publishes nothing to replace the leftover, and inside ten
minutes it would claim one.

Thread safety is the point of the lock, not an extra
----------------------------------------------------
``publish`` is called from the executor thread that assembles the prompt;
``consume`` is called on the event loop when the reply is finalized. Both mutate
the same mapping, so both hold the lock. Every critical section here is a dict
operation over a bounded mapping -- no I/O, no callback, no config read -- which
is what makes it safe to take this lock on the loop.

Neither function raises. The producer is inside a turn that must not fail over an
observation, and the consumer is inside the message finalizer, where an exception
would cost the user their reply.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any

logger = logging.getLogger(__name__)

#: How long a published outcome stays claimable. The gap between publish and
#: consume is one turn -- milliseconds to a few minutes for a long tool-using
#: reply -- so ten minutes is generous for the real path and still short enough
#: that an abandoned entry is gone well before anyone could see it attached to an
#: unrelated reply.
TTL_SECONDS = 600.0

#: Most sessions holding an unconsumed outcome at once. A gateway serving even a
#: busy crew has tens of live sessions, so reaching this means outcomes are being
#: published and never read, and the oldest are the least likely to ever be.
MAX_SESSIONS = 1000

#: Most points one session may hold an unconsumed outcome for. Two ship, so this
#: is a bound on a caller that publishes under an invented point name rather than
#: a limit anything real reaches.
MAX_POINTS_PER_SESSION = 8

#: ``session_key -> (published_at_monotonic, [outcome, ...])``, oldest publish
#: first, each session's list in publish order.
_pending: "OrderedDict[str, tuple[float, list[dict[str, Any]]]]" = OrderedDict()

_lock = threading.Lock()


def _drop_expired(now: float) -> None:
    """Evict entries older than :data:`TTL_SECONDS`. Caller holds :data:`_lock`.

    Walks from the oldest and stops at the first live entry: the mapping is kept
    in publish order, so everything after it is younger. That makes the sweep
    proportional to what it actually removes rather than to the number of live
    sessions, which is what lets it run on every publish and every consume
    instead of needing a timer of its own.
    """
    for key in list(_pending.keys()):
        published_at, _ = _pending[key]
        if now - published_at <= TTL_SECONDS:
            return
        del _pending[key]


def publish(session_key: str, outcome: dict[str, Any]) -> None:
    """Hand *outcome* to whatever finalizes this session's next assistant message.

    Replaces an unread outcome this session already held FOR THE SAME POINT and
    keeps the others: see the module docstring for why the newer decision wins
    within a point and why two points coexist.

    A falsy *session_key* or a non-dict *outcome* is DROPPED, not stored and not
    raised: the key is what a consumer matches on, so an outcome filed under
    ``""`` would be handed to the next keyless reader, and a non-dict would break
    the finalizer rather than this call. Both are programming errors in the
    caller, reported at DEBUG because this path must not turn a decision into a
    log line per turn.
    """
    if not session_key or not isinstance(session_key, str) or not isinstance(outcome, dict):
        logger.debug("decisions: outcome dropped (key=%r, type=%s)", session_key, type(outcome))
        return
    point = _point_of(outcome)
    now = time.monotonic()
    with _lock:
        _drop_expired(now)
        # Re-inserted at the END even when the key was already present: the entry
        # carries a NEW outcome, so it must age from now, and it must be the
        # newest for the eviction order to mean what it says. Ageing the whole
        # session rather than each outcome is deliberate -- the TTL exists to drop
        # a session whose reply never arrived, and the reply the list rides on is
        # one event.
        prior = _pending.pop(session_key, None)
        rows = [row for row in (prior[1] if prior else []) if _point_of(row) != point]
        rows.append(outcome)
        _pending[session_key] = (now, rows[-MAX_POINTS_PER_SESSION:])
        while len(_pending) > MAX_SESSIONS:
            _pending.popitem(last=False)


def _point_of(outcome: dict[str, Any]) -> str:
    """The decision point an outcome is about, or ``""`` for one that names none.

    ``""`` is a real key and not a wildcard: two outcomes that both decline to
    name a point are the same slot, which keeps an unlabelled producer behaving
    exactly as the single-slot registry did.
    """
    value = outcome.get("point")
    return value if isinstance(value, str) else ""


def consume(session_key: str) -> list[dict[str, Any]]:
    """Pop this session's pending outcomes, in publish order, or ``[]``.

    ``[]`` is the overwhelmingly common answer -- the seam is off by default and
    samples a bucket when on -- so it is the cheap path: one lookup on a bounded
    mapping, no I/O.

    A LIST rather than one dict, because two points can decide the same turn and
    the reply is one row: the caller stamps every outcome it is handed, so neither
    decision is dropped for having been made second.

    An entry past :data:`TTL_SECONDS` reads as ``[]`` and is discarded, so a stale
    decision is never attached to a reply it does not describe.
    """
    if not session_key:
        return []
    now = time.monotonic()
    with _lock:
        _drop_expired(now)
        entry = _pending.pop(session_key, None)
    if entry is None:
        return []
    published_at, outcomes = entry
    if now - published_at > TTL_SECONDS:
        return []
    return list(outcomes)


def discard(session_key: str) -> bool:
    """Drop this session's pending outcome without reading it. Returns whether one went.

    The counterpart to :func:`consume` for the caller that is not claiming
    anything: a turn STARTING knows that any outcome already pending belongs to a
    turn that has finished, and an outcome whose own turn never produced a reply
    has no reply left to describe. Claiming it on the next reply would be the
    stale strip the module docstring rules out, so the next turn drops it on the
    way in.

    Named rather than left as a ``consume`` whose value is thrown away, because
    "discard the previous turn's leftover" and "claim this turn's outcome" are
    different acts and a reader should not have to infer which one a bare
    ``consume`` meant.
    """
    return bool(consume(session_key))


def pending_count() -> int:
    """How many sessions hold an unconsumed outcome. For tests and diagnostics."""
    with _lock:
        return len(_pending)


def reset() -> None:
    """Forget every pending outcome.

    For tests, and for a session-wide teardown that wants no outcome surviving
    into the next one. Not called by the publish/consume pair.
    """
    with _lock:
        _pending.clear()
