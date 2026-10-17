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

Subscribers can be KEYED by (scope, key, fold), so a publish walks only the ones that
can match, and each subscription returns a DISPOSER that removes it. A subscriber that
joins late can ask for a BASELINE: the current value of its one cell, read through the
projection read path on the subscriber's own thread, followed only by pushed events with
a higher revision. See :func:`subscribe`.

A subscriber that RAISES is logged and the others still run, because a bus whose
first subscriber can silence the rest is a bus that makes every consumer depend on every
other one's bugs.

And nothing is retained: an event published with no subscriber is dropped, with no replay
and no backlog. A baseline is a fresh READ of the fold, not a replay from the bus. That is safe for the same reason a dropped eager wake is safe -- the fold
is not the record, the log is -- so a lost event leaves a consumer behind the file and the
next read carries it forward.

WHO SUBSCRIBES. Two subscribers. The dashboard's WS exporter, registered where the
dashboard state exists (``install_crew_log_publisher``), is unfiltered: it turns a
:class:`FoldAdvanced` into a ``slot_projection`` or ``session_projection`` frame, and a
:class:`TreeAdvanced` into one coalesced ``slot_patch`` of the rows whose ``parent``
moved. ``conductor_wake`` subscribes to :data:`FOLD_ADVANCED` filtered to one board
(``scope=SCOPE_SLOT``, ``key=<board>``, ``fold=<work fold>``), one subscription per
watched board, to wake that board's conductor. Three more are
named and NOT built: a summary fold over a session's events, the automatic-card
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
#: The session tree moved: a session was opened, adopted, released or forgotten, or the
#: projection finished seeding. Published by
#: :mod:`~kiro_crew.crew_log.session_tree_projection` with a :class:`TreeAdvanced`.
TREE_ADVANCED: Final[str] = "tree_advanced"


class TreeAdvanced(NamedTuple):
    """The session tree projection changed state.

    Carries no tree. The tree is one in-memory fold with one reader API
    (``projection().nodes()``), and every consumer resolves it against its OWN live
    rows -- the sidebar joins it to the slots it is showing -- so shipping a copy here
    would hand each subscriber a value it has to re-join anyway. It carries no counter
    either: the one subscriber coalesces by time, and a field nothing reads is a
    contract nothing checks.

    Published on whatever thread changed the tree: the emitter's writer thread for an
    open or a takeover, the maintenance pool for a seed. A subscriber hops to its own
    loop and returns, the contract every subscriber of this bus already owes.
    """


#: What :func:`subscribe` returns: call it to stop listening. Idempotent.
Disposer = Callable[[], None]

# One subscriber's FILTER, the index key. ``None`` in a position matches anything there.
type _Filter = tuple[str | None, str | None, str | None]
_ANY: "tuple[None, None, None]" = (None, None, None)


class _Subscriber:
    """One registration: its callback, its place in line, and its delivery state.

    ``order`` is a process-wide counter, so a publish that collects matches from several
    index buckets can still call them in registration order.

    ``floor`` is the highest revision this subscriber was handed for its one
    (scope, key, fold). A pushed event at or below it is dropped, which is the client
    rule (higher revision wins) applied once here instead of in every subscriber. It is
    only consulted for a subscriber that asked for a baseline, so an unfiltered one sees
    events exactly as published.

    ``pending`` is not ``None`` while a baseline read is in flight: events that arrive in
    that window are held there and replayed after the baseline, so none is lost to the
    gap between registering and reading.
    """

    __slots__ = ("callback", "order", "kind", "where", "active", "floor", "pending", "guard")

    def __init__(
        self, callback: "Callable[[Any], None]", order: int, kind: str, where: _Filter
    ) -> None:
        self.callback = callback
        self.order = order
        self.kind = kind
        self.where = where
        self.active = True
        self.floor: int | None = None
        self.pending: "list[Any] | None" = None
        # Serializes THIS subscriber's deliveries, so a replay of held events and a live
        # publish cannot interleave and hand it revisions out of order. Re-entrant: a
        # callback that publishes to the same subscriber on its own thread must not
        # deadlock. Never held while taking ``_lock``'s registry write.
        self.guard = threading.RLock()

    def deliver(self, event: Any) -> None:
        """Hand *event* to the callback unless disposed, held, or at or below the floor."""
        with self.guard:
            if not self.active:
                return
            if self.pending is not None:
                self.pending.append(event)
                return
            if self.floor is not None:
                revision = int(getattr(event, "revision", 0) or 0)
                if revision <= self.floor:
                    return
                self.floor = revision
            self.callback(event)


_lock = threading.Lock()
_order = 0
# kind -> (scope, key, fold) filter -> subscribers in registration order.
_subscribers: "dict[str, dict[_Filter, list[_Subscriber]]]" = {}


def subscribe(
    kind: str,
    callback: "Callable[[Any], None]",
    *,
    scope: str | None = None,
    key: str | None = None,
    fold: str | None = None,
    baseline: bool = False,
) -> Disposer:
    """Call *callback* with each event published under *kind*; return a disposer.

    FILTERS. *scope*, *key* and *fold* narrow the subscription to events whose attribute
    of that name equals the given value; any left ``None`` matches everything. They are
    the INDEX, not a predicate: :func:`publish` looks up only the buckets an event can
    match, so a subscriber keyed to one board costs a publish about another board
    nothing. A subscription with no filter receives every event of *kind*, which is what
    the dashboard's WS exporter uses; ``conductor_wake`` filters by board. Events without those attributes (any kind other
    than :data:`FOLD_ADVANCED`) reach only unfiltered subscribers.

    THE DISPOSER removes the subscription. Calling it twice is a no-op, and calling it
    from inside the callback is safe: a publish already in progress does not call a
    disposed subscriber again, and the registry lock is never held while a callback runs.

    BASELINE. With ``baseline=True`` (which needs *scope*, *key* and *fold* all set, and
    *kind* :data:`FOLD_ADVANCED`) the subscriber is first handed the CURRENT value of its
    one (scope, key, fold) as a :class:`FoldAdvanced`, read through the projection read
    path, and after that only pushed events with a higher revision. The order closes the
    join race: the subscription is registered BEFORE the read, events that arrive during
    the read are held, and once the baseline is delivered the held ones are replayed
    through the same revision floor. A fold with no minted revision yet delivers no
    baseline and the floor stays at 0.

    The baseline read is FILE I/O on the CALLER's thread. That keeps it off the append
    path and off the fold worker, as this module's contract requires, and it means a
    caller on an event loop runs this through ``asyncio.to_thread``; a callback on the
    fold worker must not subscribe with a baseline. If the read raises, the subscription
    is removed and the exception propagates, so a caller never holds a disposer for a
    subscriber that silently missed its starting value.
    """
    global _order
    if baseline:
        if kind != FOLD_ADVANCED or not (scope and key and fold):
            raise ValueError("a baseline needs kind=FOLD_ADVANCED and scope, key and fold")
        if scope not in (SCOPE_SLOT, SCOPE_SESSION):
            raise ValueError(f"unknown scope for a baseline: {scope!r}")
    where: _Filter = (scope, key, fold)
    with _lock:
        _order += 1
        sub = _Subscriber(callback, _order, kind, where)
        if baseline:
            sub.pending = []
            sub.floor = 0
        _subscribers.setdefault(kind, {}).setdefault(where, []).append(sub)

    def dispose() -> None:
        with _lock:
            if not sub.active:
                return
            sub.active = False
            buckets = _subscribers.get(sub.kind)
            if buckets is None:
                return
            listed = buckets.get(sub.where)
            if listed is not None:
                try:
                    listed.remove(sub)
                except ValueError:  # pragma: no cover - only dispose removes it
                    pass
                if not listed:
                    del buckets[sub.where]
            if not buckets:
                del _subscribers[sub.kind]

    if baseline:
        assert scope is not None and key is not None and fold is not None
        try:
            current = _read_baseline(scope, key, fold)
        except BaseException:
            dispose()
            raise
        _release_baseline(sub, current)
    return dispose


def _release_baseline(sub: _Subscriber, current: "FoldAdvanced | None") -> None:
    """Deliver *current*, then replay what arrived during the read, then go live."""
    with sub.guard:
        held = sub.pending or []
        sub.pending = None
        if not sub.active:
            return
        if current is not None and current.revision > 0:
            sub.floor = current.revision
            _call(sub, current)
        for event in held:
            if not sub.active:
                return
            revision = int(getattr(event, "revision", 0) or 0)
            if sub.floor is not None and revision <= sub.floor:
                continue
            sub.floor = revision
            _call(sub, event)


def _call(sub: _Subscriber, event: Any) -> None:
    """Run one callback, logging rather than raising. Same rule as :func:`publish`."""
    try:
        sub.callback(event)
    except Exception:
        _log_exc("crew log bus subscriber for %s failed", sub.kind)


def _read_baseline(scope: str, key: str, fold: str) -> "FoldAdvanced | None":
    """The current value of one (scope, key, fold), through the projection read path.

    The same reads a route serves -- ``read_slot_projection`` for a slot fold and
    ``read_projection`` for a session one -- so the baseline carries the revision a
    pushed event for that cell would carry. ``None`` when the fold has no revision yet.

    Imported here and not at module level: the bus is loaded by the eager folder, which
    is on the gateway's boot path, and the fold surface must not be.
    """
    from kiro_crew.crew_log import projection

    if scope == SCOPE_SLOT:
        folded = projection.read_slot_projection(key, fold)
    else:
        folded = projection.read_projection(key, fold)
    revision = int(getattr(folded, "revision", 0) or 0)
    value = getattr(folded, "value", None)
    if revision <= 0 or not isinstance(value, Mapping):
        return None
    return FoldAdvanced(
        scope=scope,
        key=key,
        fold=fold,
        revision=revision,
        value=value,
        seq=int(getattr(folded, "seq", 0) or 0),
    )


def _matching(kind: str, event: Any) -> "list[_Subscriber]":
    """Every subscriber of *kind* whose filter *event* satisfies, in registration order.

    An event can match at most eight buckets: each of its (scope, key, fold) positions is
    either the event's own value or the wildcard. Those are looked up directly, as a SET
    so an event whose attribute is itself missing (and so equal to the wildcard) does not
    reach one bucket twice. Called under ``_lock``.
    """
    buckets = _subscribers.get(kind)
    if not buckets:
        return []
    if len(buckets) == 1 and _ANY in buckets:
        return list(buckets[_ANY])
    scope = _field(event, "scope")
    key = _field(event, "key")
    fold = _field(event, "fold")
    wanted = {(s, k, f) for s in {scope, None} for k in {key, None} for f in {fold, None}}
    found = [sub for where in wanted for sub in buckets.get(where, ())]
    found.sort(key=lambda sub: sub.order)
    return found


def _field(event: Any, name: str) -> str | None:
    """*event*'s *name* when it is a string, else ``None`` (matches wildcards only).

    A bus carries whatever a publisher sends; an unhashable or missing attribute must
    cost that event its filtered subscribers, never raise inside the registry lock.
    """
    value = getattr(event, name, None)
    return value if isinstance(value, str) else None


def publish(kind: str, event: Any) -> None:
    """Hand *event* to every matching subscriber of *kind*, in registration order.

    Never raises. The matching subscribers are COLLECTED under the lock and then called
    outside it, so a subscriber that subscribes, disposes, or blocks cannot deadlock
    against the registry or mutate the list being walked. A subscriber disposed after
    collection is skipped when its turn comes.

    One subscriber's exception is logged and the walk continues. The publisher is told
    nothing: it has already done the thing that mattered (the fold), and a failure to tell
    a cache or a socket about it must not reach the thread that folded.
    """
    with _lock:
        listeners = _matching(kind, event)
    for listener in listeners:
        try:
            listener.deliver(event)
        except Exception:
            # Rendered to text, never as a traceback object: a record carrying frames
            # carries their callers, and those frames bind an open ``CrewLog`` whose write
            # lease is released by a finalizer when the handle is dropped. See
            # ``eager._log_exc``, which states this at length for the same reason.
            _log_exc("crew log bus subscriber for %s failed", kind)


def subscriber_count(kind: str) -> int:
    """How many subscribers *kind* has, across every filter. For a test and a log line."""
    with _lock:
        return sum(len(listed) for listed in _subscribers.get(kind, {}).values())


def reset_for_tests() -> None:
    """Forget every subscriber.

    A TEST SEAM, named as one. The registry is process-wide, so a case that subscribes
    would otherwise be called by every later case in the worker -- against a data home
    that is already gone. Every forgotten subscriber is marked disposed, so a publish
    already holding it calls nothing.
    """
    with _lock:
        for buckets in _subscribers.values():
            for listed in buckets.values():
                for sub in listed:
                    sub.active = False
        _subscribers.clear()


def _log_exc(message: str, *args: Any) -> None:
    """Log *message* with any exception RENDERED TO TEXT. Never raises."""
    try:
        from kiro_crew.crew_log.store import log_exception_text

        log_exception_text(logger, logging.WARNING, message, *args)
    except Exception:  # pragma: no cover - logging must never be the failure
        logger.warning(message, *args)
