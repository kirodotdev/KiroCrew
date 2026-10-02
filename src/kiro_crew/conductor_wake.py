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
whole board: each item's ``last_report_at``. The event's ``key`` IS the conductor's
slot, because a worker's ``work/recorded`` entry names the conductor's board
(:func:`~kiro_crew.crew_log.projection._work_bind_slot`), so the bus side reads no
binding file. What one event does not say is WHICH item moved, so the :class:`_Registry` keeps the
previous board's ``last_report_at`` per item and diffs: an item whose stamp changed is a
worker that reported; a board whose stamps are all unchanged moved on a conductor's own
write (``create``, ``bind``, ``decide``, ``close``) and pushes nothing, which is also
what keeps a nested conductor's own bookkeeping from spending its PARENT's budget.

THE LOOP-SIDE TRIGGERS READ THE BINDING. A close or a turn end names a worker slot, not
a board, so those two resolve worker slot -> ``(conductor, item)`` through
``work_ledger.read_binding`` (:func:`_read_binding`), the one authoritative record of
that pair. A second map rebuilt from fold renders would diverge from it whenever a
render is stale, so this module keeps none.

WHAT A FAILURE COSTS. Nothing that needs recovering. ``fire_now`` refuses with 404 (the
loop is not registered), 409 (not active) or 409 (mid-fire); each is logged at DEBUG and
dropped, because the conductor's scheduled tick runs the identical gate over the
identical store a cadence later and sees the same ledger. A bus event is not retained
either (:mod:`kiro_crew.crew_log.bus` says why), so this module never retries: the tick
is the fallback, and a push is only ever an early one.

ONE SUBSCRIPTION PER WATCHED BOARD. Each board whose conductor has an active work-ledger
loop gets its own keyed subscription, ``(slot, <board>, "work")``, joined with
``baseline=True`` (:func:`_join`): the bus hands it the board as it stands, then only
newer revisions. That baseline is the board's first sight, so the first report after a
restart or a loop just armed is diffed against the board as it stood. The
subscriptions follow the service's loop table (:func:`install`,
:func:`sync_subscriptions`): a board is disposed the moment its loop ends, and all of
them when the service stops.

TWO SIDES OF THE EVENT LOOP. The bus fans out on the eager fold worker's thread, so
:func:`_observe_event` does nothing there but copy the board's stamps out of the event
and hand them to the service's loop; the registry and the loop table are read and written
ON that loop only. :func:`fire_for_worker_slot` is for a caller already on it (the close
path, the turn-complete hook).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any, Callable

from kiro_crew.work_vocab import WORK_FOLD_NAME

logger = logging.getLogger(__name__)


#: Prefix a dashboard slot can be registered under in addition to its bare name. The
#: work ledger stores the BARE key (it comes from ``X-Session-Key``), so a trigger holding
#: the prefixed spelling has to try both -- the same two spellings
#: ``ledger_wake.worker_running`` tries, walked in the other direction.
_SLOT_PREFIX = "dashboard_"


def _slot_candidates(worker_slot_key: str) -> "tuple[str, ...]":
    """*worker_slot_key* and, when it is prefixed, its bare form."""
    if worker_slot_key.startswith(_SLOT_PREFIX):
        return (worker_slot_key, worker_slot_key[len(_SLOT_PREFIX) :])
    return (worker_slot_key,)


def _read_binding(worker_slot_key: str) -> "tuple[str, str] | None":
    """*worker_slot_key*'s ``(conductor_slot_key, item_id)``, or ``None``.

    BLOCKING: one small JSON read per spelling. Every caller arranges to run it off the
    event loop.

    Read with ``strict=False`` (the default), so a momentarily unreadable binding
    answers "unbound" rather than raising: no push, and the conductor's scheduled tick
    covers it. The import is function-local: this module is reachable from the gateway's
    boot path and the ledger store is not, so an install where no conductor ever opened a
    ledger never pays for it.
    """
    from kiro_crew import work_ledger

    for candidate in _slot_candidates(worker_slot_key):
        try:
            binding = work_ledger.read_binding(candidate)
        except Exception:  # pragma: no cover - an unreadable binding is not ours to repair
            logger.debug("conductor wake: binding read failed for %s", candidate)
            return None
        if binding is not None:
            return binding
    return None


# --------------------------------------------------------------------------- #
# The registry: what the ``work`` fold last said about each board
# --------------------------------------------------------------------------- #


class _Registry:
    """Per board, each item's report stamp as the ``work`` fold last rendered it.

    LOOP-CONFINED: every method is called on the service's event loop, from the coroutine
    the bus subscriber schedules there. That is what lets it consult the loop table
    without a lock of its own and keep its bound honest.

    THE BOUND. A board is retained only while its conductor has an active work-ledger
    loop (:func:`work_ledger_loop_id`). An event for a board nobody is watching is read
    and dropped, and every observation and every subscription sync ends with a sweep
    (:meth:`sweep`) that drops each retained board whose loop has since ended -- a
    conductor whose last ``close`` was folded while its loop still ran, and whose loop
    then reached its cap, leaves on that loop-table change rather than on a further
    write to its own. So the table holds at most the boards of the loops active at the
    last sweep, which the service bounds, and the service's ``stop`` clears it outright.
    Items per board are bounded by the fold itself (``WORK_ITEM_LIMIT``).
    """

    def __init__(self) -> None:
        self.boards: "dict[str, dict[str, str]]" = {}

    def forget(self, board: str) -> None:
        self.boards.pop(board, None)

    def observe(self, board: str, items: "list[_Item]") -> "tuple[bool, list[str]]":
        """Record *items* for *board*.

        Returns ``(first_sight, reported)``: whether this is the first board this process
        has seen for *board*, and the ids of the items whose stamp moved since the
        previous observation. On first sight ``reported`` is empty by construction --
        there is nothing to diff against -- and the caller decides what that costs.
        """
        previous = self.boards.get(board)
        first = previous is None
        stamps: dict[str, str] = {}
        reported: list[str] = []
        for item_id, stamp in items:
            if not item_id:
                continue
            stamps[item_id] = stamp
            if previous is not None and stamp and previous.get(item_id) != stamp:
                reported.append(item_id)
        self.boards[board] = stamps
        return first, reported

    def sweep(self, watched: "Callable[[str], bool]") -> None:
        """Forget every retained board for which *watched* answers ``False``."""
        for board in [b for b in self.boards if not watched(b)]:
            self.forget(board)


_registry = _Registry()


#: One rendered item as the registry reads it: ``(item_id, report stamp)``.
_Item = tuple[str, str]


def _board_items(value: Any) -> "list[_Item]":
    """The registry's view of each item of a rendered board.

    Tolerant of shape: a missing or oddly typed field reads as ``""``, because a render the fold produced is the contract and a field
    this module cannot read is a reason to push less, never to raise on the fold worker's
    thread.

    THE STAMP is :func:`_report_stamp`: ``last_report_at`` joined with a digest of what
    the report wrote, so two reports in one second still differ when either changed
    anything.
    """
    items = value.get("items") if isinstance(value, dict) else None
    out: "list[_Item]" = []
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        item_id = item.get("item_id")
        out.append(
            (
                item_id if isinstance(item_id, str) else "",
                _report_stamp(item),
            )
        )
    return out


#: The item fields a worker's ``report`` writes, besides its stamp. The fold's own list
#: is ``projection._WORK_WORKER_FIELDS``; ``test_conductor_wake`` pins the two equal.
_REPORT_FIELDS = ("status", "summary", "artifacts", "pr")


def _report_stamp(item: "dict[str, Any]") -> str:
    """What a worker's report moves on *item*, as one comparable string; ``""`` if never.

    ``last_report_at`` alone has whole-second resolution, and a report event's id is
    content-addressed, so two reports in one second can share both. The digest of every
    field a report writes, plus the newest report event's id, moves whenever a report
    changed anything; a report identical in every field carries no news to push.
    """
    stamp = item.get("last_report_at")
    if not isinstance(stamp, str) or not stamp:
        return ""
    newest = ""
    events = item.get("events")
    if isinstance(events, list):
        for event in events:
            if isinstance(event, dict) and event.get("kind") == "report":
                event_id = event.get("id")
                if isinstance(event_id, str):
                    newest = event_id
    fields = [item.get(name) for name in _REPORT_FIELDS] + [newest]
    blob = json.dumps(fields, sort_keys=True, default=str, separators=(",", ":"))
    return f"{stamp}|{hashlib.sha256(blob.encode()).hexdigest()[:16]}"


def _watched(svc: Any) -> "Callable[[str], bool]":
    """The sweep predicate: whether *board*'s conductor has an active work-ledger loop."""
    return lambda board: bool(work_ledger_loop_id(svc, board))


async def _observe_board(svc: Any, board: str, items: "list[_Item]") -> None:
    """Fold *board*'s latest stamps into the registry and fire for what moved.

    ON THE EVENT LOOP. A board whose conductor has no active work-ledger loop is dropped
    from the registry and its subscription disposed rather than recorded -- nothing can be
    fired for it -- and every other retained board is swept against the loop table at the
    same time, which is what keeps the registry at the size of the live loop table
    (:class:`_Registry`).

    FIRST SIGHT records and fires nothing. It is the join's baseline: the board as it
    stood when the subscription began, which says nothing new. What landed before it is
    the loop's own tick's to read -- at boot ``start`` already arms every work-ledger loop
    at delay zero for exactly that, and a push into that tick would buy a second cycle
    at its tail, one more against ``max_cycles`` and the quiet streak. A board with no
    fold revision yet delivers no baseline, so its first push is first sight too; that
    is a board whose first entry is the conductor's own ``goal`` or ``create``, since a
    worker report needs a ``bind`` on the board before it.
    """
    _registry.sweep(_watched(svc))
    if not work_ledger_loop_id(svc, board):
        _drop(board)
        return
    _first, reported = _registry.observe(board, items)
    for item_id in reported:
        await _fire(svc, board, item_id)


# --------------------------------------------------------------------------- #
# The bus subscriptions: one per board an active work-ledger loop watches
# --------------------------------------------------------------------------- #


class _Subscriptions:
    """Per watched board, the disposer of its keyed bus subscription.

    LOOP-CONFINED, like :class:`_Registry`, and bounded by the same predicate: a board
    holds a subscription only while its conductor has an active work-ledger loop
    (:func:`work_ledger_loop_id`). ``joining`` holds the boards whose subscribe is in
    flight -- the baseline read is file I/O, so it runs off the loop -- and
    ``generation`` is bumped by :func:`dispose_all`, so a join that completes after the
    service stopped disposes what it got instead of keeping it.
    """

    def __init__(self) -> None:
        self.live: "dict[str, Callable[[], None]]" = {}
        self.joining: "dict[str, asyncio.Task[None]]" = {}
        self.generation = 0


_subscriptions = _Subscriptions()


def _watched_boards(svc: Any) -> "set[str]":
    """Every board whose conductor has an active work-ledger loop on *svc*. ON THE LOOP."""
    lister = getattr(svc, "list_all", None)
    if not callable(lister):
        return set()
    try:
        loops = list(lister())
    except Exception:  # pragma: no cover - a loop-table read must not fail a trigger
        return set()
    boards = {str(getattr(loop, "slot_key", "") or "") for loop in loops}
    return {board for board in boards if board and work_ledger_loop_id(svc, board)}


def _drop(board: str) -> None:
    """Dispose *board*'s subscription and forget what the registry held for it."""
    dispose = _subscriptions.live.pop(board, None)
    if dispose is not None:
        dispose()
    _registry.forget(board)


def sync_subscriptions(svc: Any) -> None:
    """Make the bus subscriptions match *svc*'s active work-ledger loops.

    ON THE EVENT LOOP, and cheap: one walk of the loop table. A board whose loop ended
    is disposed at once; a board whose loop is new gets a join task (:func:`_join`),
    held in ``_subscriptions.joining`` until it lands. A board already joining is not
    joined twice.

    Called from the service's own lifecycle -- its ``start``, and every loop-table change
    it emits (:func:`install`) -- so a subscription lives exactly as long as the loop
    that can be fired for it.
    """
    watched = _watched_boards(svc)
    for board in [b for b in _subscriptions.live if b not in watched]:
        _drop(board)
    _registry.sweep(lambda board: board in watched)
    for board in sorted(watched):
        if board in _subscriptions.live:
            continue
        task = _subscriptions.joining.get(board)
        if task is None or task.done():
            _subscriptions.joining[board] = asyncio.get_running_loop().create_task(
                _join(svc, board, _subscriptions.generation)
            )


async def _join(svc: Any, board: str, generation: int) -> None:
    """Subscribe to *board*'s ``work`` fold with a baseline, and keep the disposer.

    The subscription is KEYED -- ``(slot, board, "work")`` -- so a publish about any
    other board or fold never reaches this module. ``baseline=True`` hands the callback
    the board as the fold renders it now before any pushed event; the bus registers
    first, reads second, and holds what arrives in between, so no write is lost to the
    join. That baseline is this board's FIRST SIGHT in the registry
    (:func:`_observe_board`), and the deliveries it schedules are awaited here, so the
    join ends only once that first sight is recorded.

    The subscribe runs on a worker thread because the baseline read is file I/O and the
    bus requires a baseline caller off the event loop. A failed read leaves no
    subscription behind (the bus disposes it before raising) and is logged at DEBUG: the
    next loop-table change joins again, and the loop's own tick
    reads the ledger meanwhile.
    """
    from kiro_crew.crew_log import bus

    joined: "list[Any]" = []
    collecting = True

    def _on_event(event: Any) -> None:
        # The bus's thread: the fold worker, or this join's own worker for the baseline.
        future = _observe_event(event)
        if collecting and future is not None:
            joined.append(future)

    try:
        dispose = await asyncio.to_thread(
            bus.subscribe,
            bus.FOLD_ADVANCED,
            _on_event,
            scope=bus.SCOPE_SLOT,
            key=board,
            fold=WORK_FOLD_NAME,
            baseline=True,
        )
    except Exception:
        logger.debug("conductor wake: could not subscribe to board %s", board)
        return
    finally:
        if _subscriptions.joining.get(board) is asyncio.current_task():
            del _subscriptions.joining[board]
    collecting = False
    if (
        generation != _subscriptions.generation
        or board in _subscriptions.live
        or not work_ledger_loop_id(svc, board)
    ):
        dispose()
        return
    _subscriptions.live[board] = dispose
    for future in joined:
        try:
            await asyncio.wrap_future(future)
        except Exception:  # pragma: no cover - already logged by ``_observe_event``
            pass


def install(svc: Any) -> None:
    """Follow *svc*'s loop table: subscribe now, and again on every change it emits.

    Called from :meth:`AutoNudgeService.start`, the owner of the loops this module fires
    -- the bus's rule is that a consumer subscribes where its state exists. The service's
    observer list has no removal, so the observer is attached once per service object;
    a restart of the same object re-uses it.
    """
    if not getattr(svc, "_conductor_wake_installed", False):
        svc.subscribe(lambda _event, _loop: _on_loop_table_change(svc))
        svc._conductor_wake_installed = True
    sync_subscriptions(svc)


def _on_loop_table_change(svc: Any) -> None:
    """Service observer: a loop was added, updated, removed or expired.

    Only on the service's own loop and only for the live instance: the observer is
    called synchronously wherever the service emits, and both tables this touches are
    loop-confined.
    """
    from kiro_crew import autonudge

    if autonudge.get_instance() is not svc:
        return
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        return
    if running is not _service_loop(svc):
        return
    sync_subscriptions(svc)


def _observe_event(event: Any) -> "Any | None":
    """Schedule *event*'s board observation on the service loop. The future, or ``None``.

    ON THE BUS'S THREAD, so it owes the bus contract: no real work here. It reads three
    fields and the item stamps out of the event, then schedules :func:`_observe_board`
    on the service's loop and returns without waiting. The subscription is keyed, so
    only this board's ``work`` events arrive; the scope and fold are still checked,
    because a check costs one comparison and a wrong event must cost a push, not a fire.

    Never raises: the bus would log and carry on, but the fold worker's thread is not the
    place to find out.
    """
    try:
        if getattr(event, "scope", "") != "slot" or getattr(event, "fold", "") != WORK_FOLD_NAME:
            return None
        board = str(getattr(event, "key", "") or "")
        if not board:
            return None
        items = _board_items(getattr(event, "value", None))
        from kiro_crew import autonudge

        svc = autonudge.get_instance()
        if svc is None:
            return None
        running = _service_loop(svc)
        if running is None:
            return None
        future = asyncio.run_coroutine_threadsafe(_observe_board(svc, board, items), running)
    except Exception:  # pragma: no cover - the fold worker must not see this
        logger.debug("conductor wake: could not schedule a board observation")
        return None

    def _note(done: "Any") -> None:
        try:
            done.result()
        except Exception:  # pragma: no cover - already logged inside ``_fire``
            logger.debug("conductor wake: scheduled board observation failed for %s", board)

    future.add_done_callback(_note)
    return future


def dispose_all() -> None:
    """Dispose every subscription and forget every board.

    Called by the service's ``stop``: the loops the two tables are bounded by are gone
    with it. A join still in flight sees the bumped generation and disposes what it gets.
    """
    _subscriptions.generation += 1
    for board in list(_subscriptions.live):
        _drop(board)
    _subscriptions.joining.clear()
    _registry.boards.clear()


def reset_for_tests() -> None:
    """:func:`dispose_all`. A TEST SEAM, named as one, so a case's subscriptions and
    boards never answer the next case."""
    dispose_all()


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
    hook. The worker's conductor and item come from the binding file
    (:func:`_read_binding`), read off the loop so a slow disk cannot stall it; the
    loop-table read then happens back here where it is safe.

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
    binding = await asyncio.to_thread(_read_binding, worker_slot_key)
    if binding is None:
        return ""
    return await _fire(svc, binding[0], binding[1])


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
