"""A subscriber on the crew-log bus: the ``work`` fold moved -> fire its conductor's loop.

WHY A MODULE OF ITS OWN. Three places learn that a worker said or did something: the
``work`` fold advancing on the crew-log bus (a ``work/recorded`` entry landed and was
folded), the dashboard's slot close (the worker's session is gone), and the turn-complete
hook (a worker's turn ended, with any outcome). None of them knows anything about
conductors, and each would otherwise carry its own copy of the same steps -- find the
conductor's loop, check it is the right kind and still active, fire it. A fourth trigger
is likely (an item's acceptance promoted by a human, say), so the copy count is the thing
to bound.

WHAT THE PUSH IS. :meth:`AutoNudgeService.fire_now`, and nothing else. It re-arms the
loop's own timer at delay zero, so the cycle runs inside the ordinary ``_timer`` body --
the stop sentinel, the cycle cap, the wall-clock budget, the approval stall and the
probe gate all apply exactly as on a scheduled tick. So this module moves a DEADLINE and
decides nothing: whether a turn is spent is still the gate's answer, read under the
conductor's own identity. The worker gains no handle on its conductor and sends it no
payload.

WHAT IT KNOWS, AND WHERE FROM. Only what the ``work`` fold's rendered board says. A
:class:`~kiro_crew.crew_log.bus.FoldAdvanced` for ``(slot, <board>, "work")`` carries the
whole board: each item's ``worker_session_key`` and its ``last_report_at``. The event's
``key`` IS the conductor's slot, because a worker's ``work/recorded`` entry names the
conductor's board (:func:`~kiro_crew.crew_log.projection._work_bind_slot`), so no
binding file is read here and this module is not an importer of the work-ledger store.
What one event does not say is WHICH item moved, so the :class:`_Registry` keeps the
previous board's ``last_report_at`` per item and diffs: an item whose stamp changed is a
worker that reported; a board whose stamps are all unchanged moved on a conductor's own
write (``create``, ``bind``, ``decide``, ``close``) and pushes nothing, which is also
what keeps a nested conductor's own bookkeeping from spending its PARENT's budget. The
same registry answers the two loop-side triggers in reverse -- worker slot ->
``(board, item)`` -- from the ``worker_session_key`` each item carries.

WHAT A FAILURE COSTS. Nothing that needs recovering. ``fire_now`` refuses with 404 (the
loop is not registered), 409 (not active) or 409 (mid-fire); each is logged at DEBUG and
dropped, because the conductor's scheduled tick runs the identical gate over the
identical store a cadence later and sees the same ledger. A bus event is not retained
either (:mod:`kiro_crew.crew_log.bus` says why), so this module never retries: the tick
is the fallback, and a push is only ever an early one. A board this process has not yet
seen on the bus -- the window after a restart, before its first write -- is primed from
the fold's read path when a close or a turn end asks about an unknown worker
(:func:`_prime`), bounded by the number of active work-ledger loops.

TWO SIDES OF THE EVENT LOOP. The bus fans out on the eager fold worker's thread, so
:func:`_on_fold_advanced` does nothing there but copy the board's stamps out of the event
and hand them to the service's loop; the registry and the loop table are read and written
ON that loop only. :func:`fire_for_worker_slot` is for a caller already on it (the close
path, the turn-complete hook).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


#: Prefix a dashboard slot can be registered under in addition to its bare name. The
#: work ledger stores the BARE key (it comes from ``X-Session-Key``), so a trigger holding
#: the prefixed spelling has to try both -- the same two spellings
#: ``ledger_wake.worker_running`` tries, walked in the other direction.
_SLOT_PREFIX = "dashboard_"

#: The fold this module subscribes to. Spelled here rather than imported from
#: ``work_vocab`` so the module stays importable on the gateway's boot path with nothing
#: but the standard library; ``test_conductor_wake`` pins the two spellings equal.
WORK_FOLD = "work"


def _slot_candidates(worker_slot_key: str) -> "tuple[str, ...]":
    """*worker_slot_key* and, when it is prefixed, its bare form."""
    if worker_slot_key.startswith(_SLOT_PREFIX):
        return (worker_slot_key, worker_slot_key[len(_SLOT_PREFIX) :])
    return (worker_slot_key,)


# --------------------------------------------------------------------------- #
# The registry: what the ``work`` fold last said about each board
# --------------------------------------------------------------------------- #


class _Registry:
    """Per board, each item's ``last_report_at`` and worker; per worker, its board and item.

    LOOP-CONFINED: every method is called on the service's event loop, from the coroutine
    the bus subscriber schedules there or from the loop-side triggers. That is what lets
    it consult the loop table without a lock of its own and keep its bound honest.

    THE BOUND. A board is retained only while its conductor has an active work-ledger
    loop (:func:`work_ledger_loop_id`). An event for a board nobody is watching is read
    and dropped, and every observation and every prime ends with :meth:`sweep`, which
    drops each retained board whose loop has since ended -- a conductor whose last
    ``close`` was folded while its loop still ran, and whose loop then reached its cap,
    leaves on the next observation of ANY board or the next loop-side lookup rather than
    on a further write to its own. So the table holds at most the boards of the loops
    active at the last sweep, which the service bounds, and the service's ``stop``
    clears it outright. Items per board are bounded by the fold itself
    (``WORK_ITEM_LIMIT``); the reverse map holds at most one key per OPEN item.
    """

    def __init__(self) -> None:
        self.boards: "dict[str, dict[str, str]]" = {}
        self.workers: "dict[str, tuple[str, str]]" = {}

    def forget(self, board: str) -> None:
        for item_id in self.boards.pop(board, {}):
            self._unlink(board, item_id)

    def _unlink(self, board: str, item_id: str) -> None:
        for worker, bound in list(self.workers.items()):
            if bound == (board, item_id):
                del self.workers[worker]

    def observe(self, board: str, items: "list[_Item]") -> "tuple[bool, list[str]]":
        """Record *items* for *board*.

        Returns ``(first_sight, reported)``: whether this is the first board this process
        has seen for *board*, and the ids of the items whose ``last_report_at`` moved since
        the previous observation. On first sight ``reported`` is empty by construction --
        there is nothing to diff against -- and the caller decides what that costs.

        Only an OPEN item claims its worker in the reverse map. The store lets a worker
        session be rebound once its item is terminal, and a closed item keeps its
        ``worker_session_key`` in the render -- so a terminal item re-observed later must
        not take the worker back from the board that holds it open now, and the mapping it
        held is dropped the moment it is seen closed.
        """
        previous = self.boards.get(board)
        first = previous is None
        stamps: dict[str, str] = {}
        reported: list[str] = []
        seen_items: set[str] = set()
        for item_id, stamp, worker, open_ in items:
            if not item_id:
                continue
            seen_items.add(item_id)
            stamps[item_id] = stamp
            if previous is not None and stamp and previous.get(item_id) != stamp:
                reported.append(item_id)
            if not worker:
                continue
            if open_:
                self.workers[worker] = (board, item_id)
            elif self.workers.get(worker) == (board, item_id):
                del self.workers[worker]
        if previous is not None:
            for gone in set(previous) - seen_items:
                self._unlink(board, gone)
        self.boards[board] = stamps
        return first, reported

    def sweep(self, watched: "Callable[[str], bool]") -> None:
        """Forget every retained board for which *watched* answers ``False``."""
        for board in [b for b in self.boards if not watched(b)]:
            self.forget(board)

    def lookup(self, worker_slot_key: str) -> "tuple[str, str] | None":
        for candidate in _slot_candidates(worker_slot_key):
            bound = self.workers.get(candidate)
            if bound is not None:
                return bound
        return None


_registry = _Registry()


#: One rendered item as the registry reads it: ``(item_id, last_report_at,
#: worker_session_key, open)``. ``open`` is ``state == "open"`` -- the one state of the
#: four (``work_vocab.WORK_ITEM_STATES``) under which a worker still has the item.
_Item = tuple[str, str, str, bool]


def _board_items(value: Any) -> "list[_Item]":
    """The registry's view of each item of a rendered board.

    Tolerant of shape: a missing or oddly typed field reads as ``""`` (and an unreadable
    state as not open), because a render the fold produced is the contract and a field
    this module cannot read is a reason to push less, never to raise on the fold worker's
    thread.
    """
    items = value.get("items") if isinstance(value, dict) else None
    out: "list[_Item]" = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        stamp = item.get("last_report_at")
        worker = item.get("worker_session_key")
        out.append(
            (
                item_id if isinstance(item_id, str) else "",
                stamp if isinstance(stamp, str) else "",
                worker if isinstance(worker, str) else "",
                item.get("state") == "open",
            )
        )
    return out


def _watched(svc: Any) -> "Callable[[str], bool]":
    """The sweep predicate: whether *board*'s conductor has an active work-ledger loop."""
    return lambda board: bool(work_ledger_loop_id(svc, board))


async def _observe_board(svc: Any, board: str, items: "list[_Item]") -> None:
    """Fold *board*'s latest stamps into the registry and fire for what moved.

    ON THE EVENT LOOP. A board whose conductor has no active work-ledger loop is dropped
    from the registry rather than recorded -- nothing can be fired for it -- and every
    other retained board is swept against the loop table at the same time, which is what
    keeps the registry at the size of the live loop table (:class:`_Registry`).

    FIRST SIGHT fires once, uncapped (``item_id=""``). The event exists because an entry
    was just written, and with no previous board to diff against this module cannot say
    whether it was a worker's report or a conductor's own write; a tick the gate answers
    quiet costs no turn, and the alternative -- staying silent on the first write after a
    restart -- would hide exactly the report the boot-time replay exists to surface.
    """
    _registry.sweep(_watched(svc))
    if not work_ledger_loop_id(svc, board):
        _registry.forget(board)
        return
    first, reported = _registry.observe(board, items)
    if first:
        await _fire(svc, board, "")
        return
    for item_id in reported:
        await _fire(svc, board, item_id)


async def _prime(svc: Any) -> None:
    """Register every active work-ledger loop's board this process has not seen yet.

    The window this closes: after a restart the registry is empty until each board's
    first write reaches the bus, and a worker that closes or whose turn ends in that
    window would otherwise be heard only at the scheduled cadence. The fold is read
    through the same read path the dashboard uses (:func:`read_slot_projection`), off the
    loop, once per unseen board -- bounded by the loop table, not by the number of boards
    on disk.

    Through the FOLD, not the store: this module reads what the crew log says a board is,
    exactly as a bus event would have told it.
    """
    _registry.sweep(_watched(svc))
    lister = getattr(svc, "list_all", None)
    if not callable(lister):
        return
    try:
        loops = list(lister())
    except Exception:  # pragma: no cover - a loop-table read must not fail a trigger
        return
    for loop in loops:
        board = str(getattr(loop, "slot_key", "") or "")
        if not board or board in _registry.boards or not work_ledger_loop_id(svc, board):
            continue
        items = await asyncio.to_thread(_read_board_items, board)
        if items is None:
            continue
        _registry.observe(board, items)


def _read_board_items(board: str) -> "list[_Item] | None":
    """*board*'s items as the ``work`` fold renders them now, or ``None`` when unreadable.

    BLOCKING: a fold read. Function-local import for the reason ``crew_log.eager`` gives
    about the fold surface -- this module is on the gateway's boot path and the fold is
    not.
    """
    try:
        from kiro_crew.crew_log.projection import read_slot_projection

        return _board_items(read_slot_projection(board, WORK_FOLD).value)
    except Exception:
        logger.debug("conductor wake: could not prime board %s from the work fold", board)
        return None


# --------------------------------------------------------------------------- #
# The bus subscription
# --------------------------------------------------------------------------- #

_subscribed = False
_subscribe_lock = threading.Lock()


def subscribe_to_crew_log() -> bool:
    """Register :func:`_on_fold_advanced` on the crew-log bus, once per process.

    Called from :meth:`AutoNudgeService.start`, the owner of the loops this module fires
    -- the bus's own rule is that a consumer subscribes where its state exists. Returns
    whether THIS call registered; a second call is a no-op, because the bus has no
    unsubscribe and a gateway restarted in one process would otherwise fire twice per
    event.
    """
    global _subscribed
    with _subscribe_lock:
        if _subscribed:
            return False
        from kiro_crew.crew_log import bus

        bus.subscribe(bus.FOLD_ADVANCED, _on_fold_advanced)
        _subscribed = True
        return True


def _on_fold_advanced(event: Any) -> None:
    """Bus callback: :func:`_observe_event`, with the answer the bus does not take."""
    _observe_event(event)


def _observe_event(event: Any) -> bool:
    """``True`` when *event* is a ``work`` board and its observation reached the service loop.

    ON THE FOLD WORKER'S THREAD, so it owes the bus contract: no real work here. It reads
    three fields and the item stamps out of the event, then schedules
    :func:`_observe_board` on the service's loop and returns without waiting. Every other
    scope and fold is not this subscriber's and costs one comparison.

    Never raises: the bus would log and carry on, but the fold worker's thread is not the
    place to find out.
    """
    try:
        if getattr(event, "scope", "") != "slot" or getattr(event, "fold", "") != WORK_FOLD:
            return False
        board = str(getattr(event, "key", "") or "")
        if not board:
            return False
        items = _board_items(getattr(event, "value", None))
        from kiro_crew import autonudge

        svc = autonudge.get_instance()
        if svc is None:
            return False
        running = _service_loop(svc)
        if running is None:
            return False
        future = asyncio.run_coroutine_threadsafe(_observe_board(svc, board, items), running)
    except Exception:  # pragma: no cover - the fold worker must not see this
        logger.debug("conductor wake: could not schedule a board observation")
        return False

    def _note(done: "Any") -> None:
        try:
            done.result()
        except Exception:  # pragma: no cover - already logged inside ``_fire``
            logger.debug("conductor wake: scheduled board observation failed for %s", board)

    future.add_done_callback(_note)
    return True


def forget_boards() -> None:
    """Drop every retained board and worker. Called by the service's ``stop``: the loops
    the registry is bounded by are gone with it, and a service restarted in the same
    process re-learns its boards from the bus and the prime."""
    _registry.boards.clear()
    _registry.workers.clear()


def reset_for_tests() -> None:
    """Forget every board and worker, and the once-per-process subscription mark. A TEST
    SEAM, named as one: the bus's own ``reset_for_tests`` drops the callback, so the mark
    has to drop with it or the next test's ``subscribe_to_crew_log`` is a no-op."""
    global _subscribed
    _registry.boards.clear()
    _registry.workers.clear()
    with _subscribe_lock:
        _subscribed = False


#: Pull-forwards one work item may buy its conductor's loop in an hour.
#:
#: Not the same bound as ``ledger_wake.MAX_WAKES_PER_ITEM_PER_HOUR``, which the probe
#: applies to WAKES -- turns spent on an item's news. This one bounds TICKS: a push that
#: the gate answers quiet spends no wake, so the wake cap never sees it, yet each quiet
#: tick still runs the probe and can still advance the quiet streak whose floor delivers a
#: turn anyway. A worker writing ``progress`` in a loop would otherwise buy its conductor
#: a floor turn every ``_MAX_QUIET_STREAK`` writes, with nothing capping the rate.
#:
#: A FIRST GUESS, not a derived number: it is the bar the pod QA harness measures
#: against. Every trigger counts -- a report, a close and a turn end each arm a tick --
#: unless the push coalesces into one already armed (:func:`_admit`). Only the push is
#: capped. The write still lands, and the loop's own scheduled tick still reads it, so
#: an item over its cap is heard at the patrol cadence instead of at once.
ITEM_PULLS_PER_HOUR = 12

#: The window :data:`ITEM_PULLS_PER_HOUR` counts over, in seconds.
_ITEM_WINDOW_SECS = 3600.0


def _admit(svc: Any, loop_id: str, item_id: str, now: float) -> bool:
    """Whether *item_id* may pull *loop_id* forward now, recording it when it may.

    CALL ON THE EVENT LOOP: it reads and writes the service's own tables.

    A push that would buy NOTHING is admitted and not counted, so the cap measures
    pull-forwards rather than writes. Two such states exist: a pushed tick already armed
    and not yet started (it has not read the ledger, so it will see this write), and a
    cycle in flight that already holds a deferred pull-forward (its tail runs one tick
    for every write that landed during it). Counting those would let a worker's report
    plus its own turn end spend two of the budget on one tick.

    A service without the tables -- a test stub -- is not capped. An item id of ``""``
    is not capped either: every caller resolves one from the binding, so an empty one
    means a binding this module cannot attribute, and dropping its push would turn a
    lookup gap into a lost wake.
    """
    counts = getattr(svc, "_pull_forward_counts", None)
    if counts is None or not item_id:
        return True
    pending = getattr(svc, "_pushed_ticks", ())
    firing = getattr(svc, "_firing", ())
    deferred = getattr(svc, "_pulled_forward", ())
    if loop_id in pending or (loop_id in firing and loop_id in deferred):
        return True
    per_loop = counts.setdefault(loop_id, {})
    recent = [t for t in per_loop.get(item_id, ()) if now - t < _ITEM_WINDOW_SECS]
    capped: "set[tuple[str, str]]" = getattr(svc, "_pull_forward_capped", set())
    pair = (loop_id, item_id)
    if len(recent) >= ITEM_PULLS_PER_HOUR:
        per_loop[item_id] = recent
        if pair not in capped:
            capped.add(pair)
            logger.info(
                "conductor wake: item %s reached %d pull-forwards of loop %s within an "
                "hour -- its further writes wait for the loop's scheduled tick",
                item_id,
                ITEM_PULLS_PER_HOUR,
                loop_id,
            )
        return False
    recent.append(now)
    # Write the live window back, but POP the item id once that window is empty so a
    # finished item leaves both tables. ``recent`` is non-empty here (we just
    # appended), so this admit always retains -- the empty-and-pop arm exists for the
    # shared ``_evict_stale`` sweep below, which prunes items that stopped writing and
    # would otherwise retain an aged-out key for the life of the loop (``max_cycles =
    # 0`` outlives the gateway). The persisted half of this population is bounded at
    # ``ledger_wake._MAX_TRACKED_ITEMS``; this bounds the in-memory half the same way.
    if recent:
        per_loop[item_id] = recent
    else:  # pragma: no cover - defensive; the append above keeps it non-empty here
        per_loop.pop(item_id, None)
    capped.discard(pair)
    _evict_stale(per_loop, capped, loop_id, now)
    return True


def _evict_stale(
    per_loop: "dict[str, list[float]]",
    capped: "set[tuple[str, str]]",
    loop_id: str,
    now: float,
) -> None:
    """Drop every item id whose pull-forward window has fully aged out.

    ``_admit`` is called only when a write lands for an item, so an item that opened,
    was pulled forward, then stopped writing (its ``it_<hex>`` id retired when its work
    closed) would keep an aged-out stamp list -- and its ``capped`` pair -- for the life
    of the loop, which ``model.max_cycles = 0`` lets outlive the gateway. The only other
    release was ``mutations.remove_sync`` at loop teardown. Sweeping the sibling items on
    each admit bounds the nested table at the count of items with a LIVE pull-forward,
    matching the persisted half's ``ledger_wake._MAX_TRACKED_ITEMS`` bound and satisfying
    ``a-bound-bounds-every-field-it-retains``.
    """
    stale = [
        key
        for key, stamps in per_loop.items()
        if not any(now - t < _ITEM_WINDOW_SECS for t in stamps)
    ]
    for key in stale:
        per_loop.pop(key, None)
        capped.discard((loop_id, key))


def work_ledger_loop_id(svc: Any, conductor_slot_key: str) -> str:
    """The id of *conductor_slot_key*'s ACTIVE work-ledger loop, or ``""``.

    CALL ON THE EVENT LOOP. ``get_by_slot`` walks the service's live loop table, which
    the loop's own coroutines mutate; reading it from another thread could observe a
    resize mid-walk.

    Three conditions, and each rejects a real state rather than a hypothetical one. No
    loop at all is the common case (a conductor that armed nothing). An inactive loop has
    reached one of its own bounds, and ``fire_now`` refuses it anyway -- answering ``""``
    here keeps that refusal out of the log where it would read as a fault. And a loop
    whose monitor is some OTHER kind is watching something else entirely: firing it would
    spend a turn of a budget armed for a pull request on news about a ledger it does not
    observe.
    """
    if svc is None or not conductor_slot_key:
        return ""
    from kiro_crew import probes

    getter = getattr(svc, "get_by_slot", None)
    if not callable(getter):
        return ""
    try:
        loop = getter(conductor_slot_key)
    except Exception:  # pragma: no cover - a loop-table read must not fail a trigger
        logger.debug("conductor wake: loop lookup failed for %s", conductor_slot_key)
        return ""
    if loop is None or not getattr(loop, "active", False):
        return ""
    monitor = getattr(loop, "monitor", None)
    if monitor is None or str(getattr(monitor, "kind", "")) != probes.WORK_LEDGER:
        return ""
    # A record this gateway cannot interpret is refused here as well, and for the reason
    # ``_arm_from_deadline`` refuses it: ``fire_now`` arms through ``_arm_timer``, which
    # carries no version test of its own, so a push would deliver an unattended turn under
    # a policy written by a newer gateway. The row's stored ``active`` intent is left
    # alone -- it belongs to that gateway and must survive the downgrade; what this
    # withholds is only the pull-forward.
    from kiro_crew.monitoring.models import MONITOR_STATE_VERSION

    if getattr(monitor, "version", None) != MONITOR_STATE_VERSION:
        logger.debug(
            "conductor wake: not pulling loop %s forward -- its monitor record is "
            "version %s and this gateway implements %s",
            getattr(loop, "id", "?"),
            getattr(monitor, "version", None),
            MONITOR_STATE_VERSION,
        )
        return ""
    return str(getattr(loop, "id", "") or "")


async def _fire(svc: Any, conductor_slot_key: str, item_id: str = "") -> str:
    """Fire *conductor_slot_key*'s work-ledger loop for *item_id*. The loop id, or ``""``.

    Refused without calling ``fire_now`` once *item_id* has spent its hourly budget of
    pull-forwards on this loop (:func:`_admit`). The write still landed in the ledger and
    the loop's own tick still reads it, so a refusal here costs latency, never news.

    A refusal is DEBUG and dropped. ``fire_now``'s three refusals all describe a loop
    that either cannot or must not run now, and the scheduled tick reads the same ledger
    a cadence later -- so there is nothing for a caller to do about one, and a louder
    level would report the fallback working as a failure.
    """
    loop_id = work_ledger_loop_id(svc, conductor_slot_key)
    if not loop_id:
        return ""
    if not _admit(svc, loop_id, item_id, time.time()):
        return ""
    try:
        # ``defer_if_firing``: a refusal because the loop is mid-fire is the one refusal
        # worth remembering. The cycle in flight read the ledger BEFORE this write landed,
        # and the re-arm at its tail would otherwise aim at the loop's own deadline -- so
        # on the hours-long cadence this design is meant to enable, the report would wait
        # hours. With the flag, that tail arms at delay zero instead. The refusal still
        # comes back here and is still logged and dropped.
        #
        # Passed UNGUARDED. An earlier revision wrapped this in ``except TypeError`` for
        # "a service build that predates the flag"; no such build exists, because the
        # package defines one ``fire_now`` and it ships with this caller in the same
        # commit -- so the branch could only ever be entered by a test stub, while
        # silently retrying a genuine ``TypeError`` raised INSIDE ``fire_now``.
        _loop, reason, status = await svc.fire_now(loop_id, defer_if_firing=True)
    except Exception:  # pragma: no cover - a push must never reach its trigger
        logger.debug("conductor wake: fire_now raised for loop %s", loop_id, exc_info=False)
        return ""
    if reason:
        logger.debug(
            "conductor wake: loop %s declined the pull-forward (%s [status %s]); "
            "its scheduled tick reads the same ledger",
            loop_id,
            reason,
            status,
        )
        return ""
    return loop_id


async def fire_for_worker_slot(worker_slot_key: str) -> str:
    """Pull the conductor bound to *worker_slot_key* forward. The loop id, or ``""``.

    For a caller already on the gateway event loop: the close path and the turn-complete
    hook. The worker's board and item come from the registry the bus fills; a worker the
    registry does not know triggers one :func:`_prime` pass (fold reads, off the loop)
    before the lookup is given up, so the window after a restart costs one fold per active
    loop rather than a lost push.

    ``""`` for every ordinary absence -- an unbound slot, a conductor with no loop, a
    loop of another kind, a refusal -- so a caller has nothing to branch on and no
    reason to handle one.
    """
    if not worker_slot_key:
        return ""
    from kiro_crew import autonudge

    svc = autonudge.get_instance()
    if svc is None:
        return ""
    bound = _registry.lookup(worker_slot_key)
    if bound is None:
        await _prime(svc)
        bound = _registry.lookup(worker_slot_key)
    if bound is None:
        return ""
    return await _fire(svc, bound[0], bound[1])


def _service_loop(svc: Any) -> "asyncio.AbstractEventLoop | None":
    """The event loop *svc* runs its own tasks on, or ``None``.

    Taken from a task the service already owns rather than from a loop reference this
    module would have to be handed at construction: the service is built by the gateway
    and the bus fans out on the crew log's thread, so there is no one place that holds
    both. The reconciler is the right task to ask -- ``start()`` guarantees it for the
    life of a running service -- and a live timer is the fallback for the window before
    the first reconcile pass.

    A CLOSED loop answers ``None``. A singleton can outlive its loop (the service's own
    ``stop`` documents this), and scheduling onto a closed loop raises.
    """
    for task in (getattr(svc, "_reconciler", None), *list(getattr(svc, "_timers", {}).values())):
        if task is None:
            continue
        try:
            running = task.get_loop()
        except Exception:  # pragma: no cover - a task without a loop
            continue
        if running is not None and not running.is_closed():
            return running
    return None
