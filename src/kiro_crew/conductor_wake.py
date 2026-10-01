"""One lookup: a bound worker's slot -> its conductor's armed work-ledger loop -> fire.

WHY A MODULE OF ITS OWN. Three places learn that a worker said or did something: the
crew-log eager drain (a ``work/recorded`` entry landed), the dashboard's slot close (the
worker's session is gone), and the turn-complete hook (a worker's turn ended, with any
outcome). None of them knows anything about conductors, and each would otherwise carry
its own copy of the same four steps -- resolve the binding, find the conductor's loop,
check it is the right kind and still active, fire it. A fourth trigger is likely (an
item's acceptance promoted by a human, say), so the copy count is the thing to bound.

WHAT THE PUSH IS. :meth:`AutoNudgeService.fire_now`, and nothing else. It re-arms the
loop's own timer at delay zero, so the cycle runs inside the ordinary ``_timer`` body --
the stop sentinel, the cycle cap, the wall-clock budget, the approval stall and the
probe gate all apply exactly as on a scheduled tick. So this module moves a DEADLINE and
decides nothing: whether a turn is spent is still the gate's answer, read under the
conductor's own identity. The worker gains no handle on its conductor and sends it no
payload.

WHAT A FAILURE COSTS. Nothing that needs recovering. ``fire_now`` refuses with 404 (the
loop is not registered), 409 (not active) or 409 (mid-fire); each is logged at DEBUG and
dropped, because the conductor's scheduled tick runs the identical gate over the
identical store a cadence later and sees the same ledger. That is also why the eager
path may drop a wake under pressure and why this module never retries: the tick is the
fallback, and a push is only ever an early one.

TWO ENTRY POINTS, because the callers sit on different sides of the event loop.
:func:`fire_for_worker_slot` is for a caller already ON the gateway loop (the close
path, the turn-complete hook); :func:`fire_for_worker_slot_from_thread` is for the eager
drain, which is a plain worker thread. The split is not cosmetic: the binding is a FILE
read and the loop table is the service's own dict, so each entry point puts the file read
off the loop and every ``_loops`` read on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


#: Prefix a dashboard slot can be registered under in addition to its bare name. The
#: work ledger stores the BARE key (it comes from ``X-Session-Key``), so a trigger holding
#: the prefixed spelling has to try both -- the same two spellings
#: ``ledger_wake.worker_running`` tries, walked in the other direction.
_SLOT_PREFIX = "dashboard_"


def _binding_candidates(worker_slot_key: str) -> "tuple[str, ...]":
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
    covers it. The import is function-local for the reason ``crew_log.eager`` gives
    about the fold surface -- this module is reachable from the gateway's boot path and
    the ledger store is not, so an install where no conductor ever opened a ledger never
    pays for it.
    """
    from kiro_crew import work_ledger

    for candidate in _binding_candidates(worker_slot_key):
        try:
            binding = work_ledger.read_binding(candidate)
        except Exception:  # pragma: no cover - an unreadable binding is not ours to repair
            logger.debug("conductor wake: binding read failed for %s", candidate)
            return None
        if binding is not None:
            return binding
    return None


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
    per_loop[item_id] = recent
    capped.discard(pair)
    return True


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

    For a caller already on the gateway event loop. The binding read is offloaded, so a
    slow disk cannot stall the loop (the no-blocking-call-on-event-loop rule), and the
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
    and the trigger threads are built by the crew log, so there is no one place that
    holds both. The reconciler is the right task to ask -- ``start()`` guarantees it for
    the life of a running service -- and a live timer is the fallback for the window
    before the first reconcile pass.

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


def fire_for_worker_slot_from_thread(worker_slot_key: str) -> bool:
    """Same push, from a thread. ``True`` when the fire was handed to the loop.

    The crew-log eager drain is a plain daemon thread, so it cannot await. It CAN read
    the binding, and that is the right division: the file read belongs on the thread, and
    the loop table belongs on the event loop, so the coroutine handed over does the
    lookup rather than carrying its answer across.

    Never waits for the result. The drain's own contract is that a slow consumer costs
    currency and never turn latency, and the answer this would wait for is one it would
    only log -- so the future is left to a callback and the thread goes back for the next
    batch. ``True`` therefore means "scheduled", not "fired".

    Never raises: a trigger that has already observed the entry must not be broken by
    the attempt to tell someone about it.
    """
    if not worker_slot_key:
        return False
    try:
        from kiro_crew import autonudge

        svc = autonudge.get_instance()
        if svc is None:
            return False
        binding = _read_binding(worker_slot_key)
        if binding is None:
            return False
        running = _service_loop(svc)
        if running is None:
            return False
        future = asyncio.run_coroutine_threadsafe(_fire(svc, binding[0], binding[1]), running)
    except Exception:  # pragma: no cover - the trigger must not see this
        logger.debug("conductor wake: could not schedule a push for %s", worker_slot_key)
        return False

    def _note(done: "Any") -> None:
        try:
            done.result()
        except Exception:  # pragma: no cover - already logged inside ``_fire``
            logger.debug("conductor wake: scheduled push failed for %s", worker_slot_key)

    future.add_done_callback(_note)
    return True
