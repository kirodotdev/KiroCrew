"""The per-loop timer tasks, the turn-lifecycle hooks that arm them, and the reconciler.

One cancellation policy (:func:`_cancel_timer`) retires a loop's timer task, and one
arming rule (:func:`_arm_from_deadline`) re-arms it toward the loop's persisted
deadline, so a user turn defers a pending fire without pushing the schedule back. The
dashboard's turn hooks (``notify_*``) are the reactive half: they cancel on user input,
resume on turn completion and record approval and start-failure evidence for the next
tick to act on, deferring around a loop's fire window. The reconciler is the backstop
that re-arms an active loop left with no live timer across two passes.

Its functions that take the service as ``self`` are
:class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound on the class by
name and runs against the service's state through ``self``, and a call to any other
service method goes through ``self`` too, so a patch on the instance reaches it. The
plain helpers beside them are imported directly by the owners that use them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew import shutdown_event
from kiro_crew.autonudge_service.model import (
    _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
    _START_FAILURE_BACKOFF_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
    NudgeLoop,
    is_structured_monitor_loop,
)
from kiro_crew.monitoring.models import MONITOR_STATE_VERSION, MonitorDispatchResult

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


# Re-arm delay after a skipped/failed fire so a busy slot or a transient fire
# error can't silently orphan the loop. The delay escalates exponentially per
# consecutive failure (base << streak) up to _REARM_MAX_BACKOFF_SECS, and is
# always capped by the loop's idle_secs, so a permanently-wedged callback backs
# off to a slow poll instead of hammering every base interval.
_REARM_BACKOFF_SECS = 15
_REARM_MAX_BACKOFF_SECS = 300  # 5m ceiling for the escalated re-arm delay
_REARM_BACKOFF_MAX_SHIFT = 16  # clamp the 2**shift exponent
_MONITOR_RETRY_BACKOFF_SECS = 15
_MONITOR_RETRY_MAX_BACKOFF_SECS = 300


# Re-arm delay when a loop's deadline has already passed while a user turn was
# in flight. Small but non-zero: firing the instant the user's turn ends would
# race their follow-up message; a short beat leaves room for notify_user_input
# to cancel the pending fire again if they are still actively conversing.
_OVERDUE_REARM_SECS = 10


# How often the reconciler walks the store looking for an active loop with no
# live timer task. A stranded loop (fire delivered but the slot's stop hook
# never arrived, a dropped deferred re-arm) is rescued after two consecutive
# eligible passes -- so within two to three intervals of going quiet; the walk
# itself is an in-memory scan of a small dict, so the interval is chosen for
# rescue latency, not cost. The two-pass requirement, not this number, is what
# keeps the reconciler from mistaking short-lived live states (a running user
# turn, a mutation window) for strandings; see _reconcile_once.
_RECONCILE_INTERVAL_SECS = 60


def _resolve_beat(beat: "asyncio.Future[None]") -> None:
    """Resolve one reconciler heartbeat future (see ``_reconcile_forever``)."""
    if not beat.done():
        beat.set_result(None)


def _current_task_or_none() -> "asyncio.Task[Any] | None":
    """:func:`asyncio.current_task`, or ``None`` when no loop is running.

    ``current_task`` raises ``RuntimeError: no running event loop`` outside a loop, and
    ``stop()`` is reached from SYNCHRONOUS callers — the gateway's shutdown path and test
    teardown — where nothing is running. There, no task can be "the current" one, which is
    the answer this returns rather than an exception the caller would have to know about.
    """
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def notify_approval_stalled(self: AutoNudgeService, slot_key: str) -> None:
    """Record that a tool approval in *slot_key* went unanswered: hold the loop.

    Called from the approval path when a prompt times out with no decision.
    That is the only evidence available that an unattended loop cannot
    act, and it is evidence rather than inference: an auto-approved tool
    never reaches the interactive wait, so this is unreachable for a loop
    whose cycles only touch read-only tools.

    Schedules the durable record (``_record_approval_hold``) and returns. The
    HOLD is applied by ``_timer``, which
    already owns every terminal and scheduling decision and evaluates them
    serialized before a fire -- acting from here would mean cancelling a timer
    that may be mid-fire (the one thing the fire-window contracts forbid, since
    it kills the in-flight turn) and racing the very turn that produced the
    evidence. Deferring costs the cycle already in flight and saves every later
    one.

    A hold, not a stop: the loop stays active, fires nothing, spends neither
    its cycle cap nor its runtime budget, and resumes on its own through
    :func:`release_approval_hold` once a person is back in the slot. A stop
    here would end a long patrol overnight for a prompt nobody was awake to
    answer, and only a person re-arming it could bring it back.

    The evidence is slot-level, not cycle-level: an unanswered prompt in an
    attended tab counts too. That is the conservative direction -- the loop
    only waits, and a person who was merely away releases it by acting in the
    slot -- whereas the alternative needs a reliable "is this turn a nudge
    cycle?" test, which the fire window does not provide for dashboard slots
    (their turn outlives it).
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active or loop.approval_stalled:
        return
    try:
        task = asyncio.ensure_future(_record_approval_hold(self, slot_key))
    except RuntimeError:
        # No running loop to write on. The hold is not recorded, which costs at
        # most one more declined cycle, and the next unanswered prompt records it.
        return
    self._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[None]") -> None:
        self._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "AutoNudge: recording the approval hold for %s failed; the loop "
                "keeps running until the next unanswered prompt",
                slot_key,
                exc_info=t.exception(),
            )

    task.add_done_callback(_finish)


async def _record_approval_hold(svc: AutoNudgeService, slot_key: str) -> None:
    """Set *slot_key*'s approval hold durably, then announce it.

    Persist before publishing, the same shape as ``release_approval_hold``: the
    flag and its start time are written under ``_lock`` and the write is AWAITED
    before the WARNING, the ``updated`` event and the one-time ``held`` notice.
    A write that fails restores
    both fields, so a hold the store never accepted is never shown or acted on.
    A ``CancelledError`` from the writer arrives only after the write settled, so
    the hold is kept; only the announcement is skipped.
    """
    async with svc._lock:
        loop = svc._find_by_slot(slot_key)
        if not loop or not loop.active or loop.approval_stalled:
            return
        # A structured monitor does not hold: its tick path never reads the flag
        # and the release refuses it. Its action-completion path does read it,
        # to settle the monitor as an ``approval_stall`` stop, so the flag is
        # still recorded for it -- with no hold time and no hold notice.
        structured = is_structured_monitor_loop(loop)
        loop.approval_stalled = True
        loop.approval_stalled_at = 0.0 if structured else time.time()
        try:
            await svc._write_monitor_snapshot_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            loop.approval_stalled = False
            loop.approval_stalled_at = 0.0
            raise
    if structured:
        return
    logger.warning(
        "AutoNudge: a tool approval went unanswered in loop %s's session -- "
        "it is paused for approval and fires no cycle until someone answers "
        "an approval, sends a message or fires it in that session",
        loop.id,
    )
    svc._emit("updated", loop)
    # A one-time notice, through the observer that announces the terminal
    # bounds: the loop is not stopped, but nobody is looking at it, and this is
    # the only signal that reaches a person who is away from the session.
    svc._emit("held", loop)


async def release_approval_hold(
    self: AutoNudgeService, slot_key: str, *, why: str, arm: bool = True
) -> bool:
    """End *slot_key*'s approval hold, because a person is back. True if one ended.

    Reached from the places that prove a person is present in the slot: an
    approval answered there (dashboard, Slack, Discord and the channel-neutral
    registry, through ``autonudge.release_approval_hold_for``), a message a
    person typed into the dashboard (``notify_user_input(human=True)``), and the
    popover's manual fire. An agent's or app's turn does not count -- what the
    hold waits for is someone who can answer the next prompt, and a landed turn
    proves only that the session runs.

    The held time is added to ``created_ts`` (the runtime-budget clock), so the
    hold spends none of the budget. ``arm`` re-arms toward the loop's deadline,
    which has usually passed while it was held, so the next cycle runs within
    ``_OVERDUE_REARM_SECS``. A caller about to start a turn in the slot passes
    ``arm=False``: its own turn-complete hook arms the loop when that turn ends,
    which is the "user wins" rule ``notify_user_input`` already keeps.

    Persist before publishing: the cleared hold and the moved clock are written
    under ``_lock`` and the write is AWAITED before anything is emitted, armed or
    reported. A write that fails restores all three fields, so the loop stays
    held exactly as the store says. A ``CancelledError`` from the writer arrives
    only after the write settled, so the release is durable and kept; the arm is
    skipped then, and the reconciler re-arms the loop, which is not held any more.
    A structured monitor never holds (its tick path does not read the flag), so
    it is left alone.
    """
    async with self._lock:
        loop = self._find_by_slot(slot_key)
        if (
            not loop
            or not loop.active
            or not loop.approval_stalled
            or is_structured_monitor_loop(loop)
        ):
            return False
        now = time.time()
        since = loop.approval_stalled_at
        held = 0.0
        if isinstance(since, (int, float)) and not isinstance(since, bool) and 0 < since <= now:
            held = now - since
        prior = (loop.approval_stalled, loop.approval_stalled_at, loop.created_ts)
        created = loop.created_ts
        if (
            held
            and isinstance(created, (int, float))
            and not isinstance(created, bool)
            and created > 0
        ):
            loop.created_ts = created + held
        loop.approval_stalled = False
        loop.approval_stalled_at = 0.0
        try:
            await self._write_monitor_snapshot_locked()
        except asyncio.CancelledError:
            raise
        except Exception:
            loop.approval_stalled, loop.approval_stalled_at, loop.created_ts = prior
            raise
    logger.warning(
        "AutoNudge: loop %s resumed after %.0fs paused for approval (%s)",
        loop.id,
        held,
        why,
    )
    self._emit("updated", loop)
    if arm and loop.active and loop.id in self._loops:
        if loop.id in self._firing:
            self._rearm_pending.add(loop.id)
        else:
            self._arm_from_deadline(loop)
    return True


def _schedule_release(svc: AutoNudgeService, slot_key: str, *, why: str, arm: bool = True) -> None:
    """Run :func:`release_approval_hold` detached, for a synchronous caller.

    Supervised through ``_inflight_adds`` like the judge label and the conductor
    wake: strongly referenced, and a failed write is logged rather than lost. A
    plain helper rather than a service member, for the reason
    ``_wake_bound_conductor`` gives. Never raises: the approval and message paths
    that call it must not fail because a release could not be scheduled.
    """
    if not slot_key:
        return
    loop = svc._find_by_slot(slot_key)
    if loop is None or not loop.approval_stalled:
        return
    try:
        task = asyncio.ensure_future(svc.release_approval_hold(slot_key, why=why, arm=arm))
    except RuntimeError:
        # No running loop: nothing to schedule onto, and the hold stays until the
        # next sign of a person -- the harmless direction.
        return
    svc._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[bool]") -> None:
        svc._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.warning(
                "AutoNudge: approval-hold release for %s failed; the loop stays paused",
                slot_key,
                exc_info=t.exception(),
            )

    task.add_done_callback(_finish)


def notify_cycle_start_failed(self: AutoNudgeService, slot_key: str) -> None:
    """Record that a turn in *slot_key* never obtained a model session.

    Called from the chat runner's terminal-error path when the failure is
    tagged ``session_start_failed`` and the turn was this loop's own cycle.
    Evidence, not inference: the turn was dispatched, spent its budget and
    produced nothing, which is the one thing that distinguishes a starved
    cycle from a quiet one.

    Records and returns. Like ``notify_approval_stalled``, the decision is
    left to ``_timer``, which owns every terminal and scheduling decision and
    evaluates them serialized before a fire -- deciding here would mean
    touching a timer that may be mid-fire.
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active:
        return
    loop.consecutive_start_failures += 1
    logger.warning(
        "AutoNudge: loop %s's cycle never got a model session "
        "(%d consecutive); it will back off at %d and stand down at %d",
        loop.id,
        loop.consecutive_start_failures,
        _START_FAILURE_BACKOFF_AFTER,
        _START_FAILURE_STANDDOWN_AFTER,
    )
    self._persist_soon()


async def notify_cycle_failed(
    self: AutoNudgeService,
    slot_key: str,
    *,
    loop_id: str,
    expected_generation: int,
) -> None:
    """Record that this loop's own delivered cycle in *slot_key* ended in a fault.

    Called from the chat runner's terminal-error paths when the failure is neither
    a structural rejection nor a session-start failure and the turn was this
    loop's own cycle -- a turn that reached a model session and dispatched, then
    died (``error`` or ``timeout``). Evidence, not inference: the loop spent a
    turn and it failed, which is the one thing that distinguishes a loop making no
    progress from a quiet one.

    Only the loop's OWN cycle counts (the chat runner passes the self-wake guard,
    excluding the structural and session-start cases, before calling this), so a
    human turn that happened to error on a slot carrying a loop cannot spend the
    loop's stand-down budget. That is the same guard ``notify_cycle_start_failed``
    relies on, and for the same reason.

    Scoped to the fired loop by BOTH its id AND its ``config_generation``,
    captured at fire time (chat_runner passes ``_directive_loop_id`` and
    ``_directive_loop_gen``), matched under ``_lock`` so there is no TOCTOU
    window -- the same ``(id, generation)`` fence the structural-terminal verdict
    is applied under. The id guards the slot-reuse case the generation alone
    cannot: a loop A on this slot replaced by a fresh loop B that happens to carry
    the same slot and the same generation (both start at 0) would otherwise take
    A's stale failure onto B. The generation guards the revision case (A->B->A):
    a completion whose generation advanced under it describes the OLD instruction
    and must not stand the revised loop down. Either mismatch is a stale fault and
    is dropped.

    Durability (persist before you publish): the increment and its durable write
    happen together under ``_lock`` and the write is AWAITED. A write that
    genuinely fails rolls the live ``consecutive_failed_cycles`` back to what the
    store last held, so the stand-down never reads a count the store has not
    accepted. A ``CancelledError``, by contrast, is raised by the cancellation-
    safe writer ONLY after its executor future has settled, so on cancel the write
    DID land -- the committed increment is durable and is kept, never rolled back
    (rolling it back would erase an accepted failure and leave the live count one
    short of the store). The chat runner's terminal arm is async and awaits this,
    which is what lets it stage-then-write rather than detach the write the way a
    deadline-reassigning sync hook must. The stand-down DECISION still belongs to
    ``_timer``, which owns every terminal and scheduling decision and evaluates
    them serialized before a fire -- this only records the evidence.
    """
    async with self._lock:
        loop = self._loops.get(loop_id)
        if loop is None or not loop.active or loop.slot_key != slot_key:
            return
        if loop.config_generation != expected_generation:
            # Stale completion of a now-revised loop: the generation advanced
            # between fire and fault, so this charge belongs to the old
            # instruction, not the loop live on the slot now.
            return
        if is_structured_monitor_loop(loop):
            # A structured monitor loop's ``_timer`` returns at its own
            # ``is_structured_monitor_loop`` guard (firing.py) BEFORE the only
            # reader of ``consecutive_failed_cycles`` (the consecutive-failure
            # stand-down), so charging it here would grow the counter
            # monotonically in the store and on the loop's API row while no bound
            # could ever act on it -- a write-only field. Monitor faults are
            # bounded by the monitor's own ``consecutive_provider_errors``
            # budget, not this stand-down, so skip the charge and keep the
            # counter meaning exactly what the bound that reads it expects.
            return
        # Persist BEFORE publishing: increment, drive it to a durable write
        # under the SAME lock hold, and roll back ONLY when the store genuinely
        # refused it -- so a near-threshold streak the store never accepted can
        # never stand the loop down, and a streak the store DID accept is never
        # erased. The two failure modes are not the same event and must not share
        # a handler:
        #   * A real write error (``Exception``) means the fsync did not land, so
        #     the increment is unpublished evidence -- roll the live field back to
        #     what the store last held and propagate.
        #   * ``CancelledError`` from ``_write_monitor_snapshot_locked`` is raised
        #     ONLY AFTER its executor future is observed (it shields the write and
        #     calls ``future.result()`` before re-raising), so the write DID land
        #     -- the committed increment is now durable and must stand. Rolling it
        #     back here (the earlier ``except BaseException``) would erase a
        #     failure the store already accepted and leave the live count one
        #     short of the durable one, so a cancellation during the fifth
        #     charge's fsync would cost an extra cycle before the stand-down. Let
        #     the cancellation propagate with the increment intact.
        prior = loop.consecutive_failed_cycles
        loop.consecutive_failed_cycles = prior + 1
        try:
            # ``_write_monitor_snapshot_locked`` offloads the fsyncing
            # ``_write_state`` to a worker thread and ABSORBS cancellation until
            # the executor result is observed, so the lock scope cannot release
            # (or this method return) around a half-applied snapshot -- the one
            # cancellation-safe writer the service already uses under the lock.
            await self._write_monitor_snapshot_locked()
        except asyncio.CancelledError:
            # The write already settled before the writer re-raised: the
            # increment is durable, so keep it and only propagate the cancel.
            raise
        except Exception:
            # The durable write actually failed: the increment is unaccepted
            # evidence, so unpublish it before propagating.
            loop.consecutive_failed_cycles = prior
            raise
        logger.warning(
            "AutoNudge: loop %s's cycle failed (%d consecutive); it will stand "
            "down at %d unless a turn lands first",
            loop.id,
            loop.consecutive_failed_cycles,
            _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
        )


def notify_cycle_landed(self: AutoNudgeService, slot_key: str) -> None:
    """Clear *slot_key*'s failure streaks: a turn on it completed.

    Any landed turn counts, a human's as much as a cycle's -- the streaks are a
    reading of whether this session can start (``consecutive_start_failures``)
    and make progress (``consecutive_failed_cycles``) at all, and a turn that
    reached completion proves both. That is the conservative direction: it can
    only let a loop keep running, never stop one.

    Deliberately SYNCHRONOUS and lock-free: it clears ONE field on the live loop
    and must be callable from the turn-completion path WHILE a baseline commit
    holds ``_lock`` mid-write (see
    ``test_a_streak_cleared_during_the_commit_write_stays_cleared``), so it must
    not itself take the lock or await a write. The durable write is detached via
    ``_persist_soon`` -- and a LOST clear is the harmless direction: the streak
    returns to a stale non-zero value, which only ever slows or stands a loop
    down, never fires one early or spuriously, and any later landed turn clears
    it again. The dangerous direction (a durable count AHEAD of what the store
    accepted, which can stand a loop down early) belongs to the INCREMENT, and
    ``notify_cycle_failed`` closes that one by awaiting its write under the lock
    with rollback. The two directions are not symmetric, so they do not share a
    remedy.
    """
    loop = self._find_by_slot(slot_key)
    if not loop or not (loop.consecutive_start_failures or loop.consecutive_failed_cycles):
        return
    loop.consecutive_start_failures = 0
    loop.consecutive_failed_cycles = 0
    # Drop the paid-deferral marker with the streak it belonged to: a streak
    # that climbs back to the same value must pay its own deferral again.
    self._start_failure_deferred.pop(loop.id, None)
    self._persist_soon()


def notify_turn_complete(
    self: AutoNudgeService,
    slot_key: str,
    *,
    tool_calls: int | None = None,
    reply_text: str | None = None,
    reply_flushed: bool = False,
    nudge_turn: bool | None = None,
    tool_identities: object = None,
) -> None:
    """Called by gateway after HOOK_EVENT_STOP — resume the countdown for this slot.

    Re-arms toward the loop's persistent deadline (``_arm_from_deadline``),
    NOT with a fresh full interval: after a user turn the timer picks up
    the remaining time (or fires shortly after, if the deadline passed
    mid-turn), while the first turn-complete after a delivered fire — the
    nudge turn's own end — finds the deadline cleared and starts the next
    full cycle. DEFERS while the loop's own timer task is mid-fire:
    ``_arm_timer`` cancels the existing task, and during the fire window
    that task may be parked on ``_persist_locked()`` writing the delivered
    cycle. Cancelling it there loses the ``cycle_count`` bump and lets the
    loop run extra cycles after a restart. The deferred re-arm is applied
    when the window closes.

    *tool_calls*, *tool_identities* and *reply_text* are what the completed turn
    DID, and this is the only hook where the service can see it: the runner holds
    all three as locals of the turn it is finishing. *tool_identities* is what makes
    the label DISCRIMINATE -- it names each dispatch, so a turn whose only call read
    a file is not counted as having acted, which a bare *tool_calls* count cannot
    express. It is optional, and its absence falls back to the count.
    *nudge_turn* binds those facts to this loop's own delivered turn.
    *reply_flushed* says the visible text is only the final segment of the reply. An
    unknown *nudge_turn* reads as not this loop's turn: declining to label costs one
    row of hit rate, while labelling an unrelated turn writes a wrong row. None of
    the five is stored: the rule reduces them to one boolean plus two numbers and a
    flag, and that is what the loop's record and the calibration log keep.
    """
    # TRIGGER THREE of the crew-log wake, and it has to run BEFORE the lookup below.
    # That lookup asks for a loop on THIS slot; a worker has none, so every early return
    # under it is the normal case for the slot this trigger is about.
    _wake_bound_conductor(self, slot_key)
    loop = self._find_by_slot(slot_key)
    if not loop or not loop.active:
        return
    # Before the re-arm and before the mid-fire deferral, because this labels the
    # turn that just ENDED. A deferred re-arm postpones the next tick; the verdict
    # that woke this one is already decided and is waiting for exactly this answer.
    if loop.judge_recent_verdicts and nudge_turn is True:
        try:
            # The local import keeps the decisions graph off the gateway boot path.
            from kiro_crew import autonudge_judge as judge

            asyncio.get_running_loop()
            acted, tool_call_count, reply_chars, names_known = judge.owner_action_reading(
                tool_calls,
                reply_text,
                reply_flushed=reply_flushed,
                tool_identities=tool_identities,
            )
            task = asyncio.ensure_future(
                self._label_judge_delivery_locked(
                    loop,
                    acted,
                    tool_calls=tool_call_count,
                    reply_chars=reply_chars,
                    tool_names_known=names_known,
                )
            )
        except RuntimeError:
            logger.debug(
                "AutoNudge: skipped judge delivery label for loop %s without "
                "a running event loop",
                loop.id,
            )
        except Exception:
            logger.debug(
                "AutoNudge: could not schedule the judge delivery label for loop %s",
                loop.id,
                exc_info=True,
            )
        else:
            self._inflight_adds.add(task)

            def _finish(t: "asyncio.Task[None]") -> None:
                self._inflight_adds.discard(t)
                if not t.cancelled() and t.exception() is not None:
                    logger.warning(
                        "AutoNudge: detached judge delivery label failed for loop %s",
                        loop.id,
                        exc_info=t.exception(),
                    )

            task.add_done_callback(_finish)
    if loop.id in self._firing:
        self._rearm_pending.add(loop.id)
        return
    self._arm_from_deadline(loop)


def _wake_bound_conductor(svc: AutoNudgeService, slot_key: str) -> None:
    """Pull *slot_key*'s conductor forward, if *slot_key* is a bound worker's slot.

    OUTCOME-BLIND on purpose, and that is what this trigger adds over the other two. The
    hook this sits in is called for every turn end after HOOK_EVENT_STOP, so a turn that
    raised, a turn that produced nothing, and a turn that simply forgot to report all
    reach it identically -- and those are exactly the endings that write no
    ``work/recorded`` entry, so trigger one never sees them. The gate still decides
    whether a turn is spent: a worker that reported ``progress`` and then ended its turn
    pulls the tick forward and the probe answers quiet.

    DETACHED, because this hook is synchronous and its caller is the gateway finishing a
    turn. Supervised through ``_inflight_adds`` like the judge label above it, so the task
    is strongly referenced and its failure is logged rather than swallowed by the garbage
    collector. Never raises: a turn end must not fail because a push could not be
    scheduled.

    A PLAIN HELPER taking *svc*, not a service member: it is reached from exactly one
    call site in this module, and binding it on the class would put a name on the
    service's surface that nothing outside here can use.
    """
    if not slot_key:
        return
    try:
        # Local, for the reason the judge import below is: ``conductor_wake`` reaches the
        # work-ledger store, and this hook runs on every turn of every session.
        from kiro_crew import conductor_wake

        asyncio.get_running_loop()
        task = asyncio.ensure_future(conductor_wake.fire_for_worker_slot(slot_key))
    except RuntimeError:
        # No running loop: a synchronous test driver or a shutdown path. Nothing to
        # schedule onto, and the conductor's own tick still covers it.
        return
    except Exception:  # pragma: no cover - a turn end must not fail on this
        logger.debug("AutoNudge: could not schedule a conductor wake for %s", slot_key)
        return
    svc._inflight_adds.add(task)

    def _finish(t: "asyncio.Task[str]") -> None:
        svc._inflight_adds.discard(t)
        if not t.cancelled() and t.exception() is not None:
            logger.debug(
                "AutoNudge: conductor wake for %s failed",
                slot_key,
                exc_info=t.exception(),
            )

    task.add_done_callback(_finish)


def notify_user_input(self: AutoNudgeService, slot_key: str, *, human: bool = False) -> None:
    """Called when user sends a message — cancel the pending nudge task.

    *human* says a PERSON typed it (not an app token). That is proof someone is
    back in the slot, so it also ends an approval hold (``release_approval_hold``,
    unarmed: this turn's own completion re-arms the loop).

    Cancelling the TASK defers delivery until the user's turn ends (a
    nudge must never race a human turn); the loop's ``next_due_ts`` is
    untouched, so the schedule itself survives — ``notify_turn_complete``
    resumes the same countdown rather than restarting the full interval.

    While the loop is mid-fire this must NOT cancel the timer: that task may
    be parked on ``_persist_locked()`` writing the delivered cycle, and
    cancelling it there abandons an in-flight executor write whose stale
    payload can later overwrite a newer update/delete (state resurrected
    after a restart). User priority is still honoured — the deferred re-arm
    is dropped, so no further nudge is scheduled from this cycle.
    """
    if human:
        _schedule_release(self, slot_key, why="a person sent a message", arm=False)
    loop = self._find_by_slot(slot_key)
    if not loop:
        return
    # A user turn starting is proof the slot is alive: restart the
    # reconciler's two-pass clock so the stranded-loop backstop never
    # re-arms a timer this hook is about to cancel on purpose. If this
    # turn then dies without its stop hook, candidacy simply rebuilds
    # over the next two passes and the rescue still happens.
    self._reconcile_candidates.discard(loop.id)
    if loop.id in self._firing:
        self._rearm_pending.discard(loop.id)
        logger.info(
            "AutoNudge: user input during loop %s's fire window — dropped the "
            "deferred re-arm instead of cancelling mid-persist",
            loop.id,
        )
        return
    self._cancel_timer(loop.id)


def _cancel_timer(self: AutoNudgeService, loop_id: str, *, drop_claims: bool = True) -> None:
    """Retire one loop's timer task. The single cancellation policy.

    Two conditions make a cancel wrong rather than merely redundant, and both are
    stated here so no caller has to remember either:

    * **The currently running timer task** (a self-re-arm from inside ``_timer``) is
      about to return on its own, and cancelling it would inject a spurious
      ``CancelledError`` into the finishing task.
    * **A task whose event loop has already closed.** ``Task.cancel`` schedules the
      cancellation through ``loop.call_soon``, which raises ``RuntimeError: Event loop
      is closed`` — so this raises out of ``remove``/``remove_sync`` and the dashboard
      handler above it answers 500. The service is a process-global singleton, so its
      ``_timers`` outlive the loop that created them whenever one loop is replaced by
      another: the gateway's own shutdown, and every test that drives a handler after
      an earlier test's loop closed. Asked positively (``get_loop().is_closed()``)
      rather than by catching the ``RuntimeError``, because a closed loop is the one
      state where cancelling is a NO-OP by definition — the task can never run again —
      and catching would also swallow a genuine scheduling fault.

    The closed-loop question is asked FIRST because it needs no running loop of its
    own, and ``stop()`` reaches here from synchronous callers (gateway shutdown, test
    teardown) where ``asyncio.current_task()`` would raise instead of answering — hence
    :func:`_current_task_or_none`.
    """
    t = self._timers.pop(loop_id, None)
    if t is None or t.done():
        return
    # Closed-loop check FIRST: it needs no running loop of its own, so a dead timer is
    # retired even from a synchronous caller.
    if t.get_loop().is_closed():
        logger.debug(
            "AutoNudge: dropped loop %s's timer without cancelling — its event loop "
            "has closed, so the task can no longer run",
            loop_id,
        )
        return
    if t is _current_task_or_none():
        return
    t.cancel()
    if not drop_claims:
        # Replacing a timer is not cancelling a cycle. ``_arm_timer`` cancels before
        # it creates, so every ordinary re-arm came through here -- including the
        # backoff re-arm on the refused-fire path, which erased the claim that same
        # path had re-owed one statement earlier. The accounting fix was defeated by
        # the cleanup meant to protect it.
        return
    # A cancelled CYCLE drops its claim. Without this the id stays in the claim set
    # and the loop's next delivered fire -- a fallback, a floor tick -- inherits it
    # and is charged as a wake as well, counting one delivered turn under two
    # counters. That trade is deliberate: an undelivered observation is lost rather
    # than attributed to a turn that did not carry it.
    self._pending_monitor_wake.discard(loop_id)
    self._pending_floor_tick.discard(loop_id)


def _arm_timer(self: AutoNudgeService, loop: NudgeLoop, delay: float | None = None) -> None:
    self._cancel_timer(loop.id, drop_claims=False)
    # Any arm replaces the armed timer, so a push mark naming the old one is stale. A
    # push re-sets it right after this call.
    self._pushed_ticks.discard(loop.id)
    self._timers[loop.id] = asyncio.create_task(self._timer(loop, delay))


def _arm_from_deadline(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """(Re)arm the timer toward the loop's persistent deadline.

    The countdown anchors on ``next_due_ts`` instead of restarting at the
    full interval on every arm, so user turns in the bound session defer a
    pending fire without pushing the schedule back. An unset deadline (0 —
    a just-delivered fire, a legacy store entry) starts a fresh full
    countdown from now, and the assignment is persisted through a
    supervised background write so a restart resumes this countdown
    rather than restarting the interval. A deadline still in the future
    resumes with exactly the remaining time; only one already in the past
    fires after a short beat (``_OVERDUE_REARM_SECS``) rather than
    instantly, so a user mid-conversation keeps deferring it simply by
    sending another message. The delay is capped at ``idle_secs`` so a
    clock jump can never park the timer beyond one full interval.

    A monitor loop arms through this same path and on the same deadline. Its
    cadence is the interval the user already set, not a second clock on the
    monitor record: two clocks for one countdown would have to be kept
    agreed, and the one the user can see is the one they set. What differs
    for a monitor is not WHEN the timer wakes but what the wake costs -- the
    probe gate in :meth:`_timer` decides whether that tick spends a turn.

    ONE monitor is refused a timer outright: a record whose ``version`` this
    gateway does not implement. Such a record belongs to a newer gateway
    (a downgrade or a rollback read its store), and this controller cannot
    interpret its policy -- so arming it would run the loop under a policy
    nobody here understands, which for the pre-gate code path means
    injecting the raw message every interval with no decision at all. The
    refusal is deliberately made HERE, on the arm, rather than by rewriting
    the record: the stored ``active`` intent belongs to the gateway that
    wrote it and must survive the downgrade so an upgrade resumes the watch.
    Inertness is the local consequence, not a change of intent.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    monitor = loop.monitor
    if monitor is not None and monitor.version != MONITOR_STATE_VERSION:
        logger.info(
            "AutoNudge: not arming loop %s -- its monitor record is version %s and "
            "this gateway implements %s",
            loop.id,
            monitor.version,
            MONITOR_STATE_VERSION,
        )
        return
    now = time.time()
    if loop.next_due_ts <= 0:
        loop.next_due_ts = now + loop.idle_secs
        if loop.monitor is not None:
            loop.monitor.next_probe_at = loop.next_due_ts
        self._persist_soon()
    remaining = loop.next_due_ts - now
    if remaining <= 0:
        delay = float(seams._OVERDUE_REARM_SECS)
    else:
        delay = min(remaining, float(loop.idle_secs))
    self._arm_timer(loop, delay=delay)


async def _reconcile_forever(self: AutoNudgeService) -> None:
    """Periodically rescue any active loop left with no live timer.

    A dashboard-bound loop has exactly one re-arm path after a delivered
    fire: ``notify_turn_complete``, called by the gateway after the slot's
    stop hook. If that hook never arrives -- the nudge turn errors, times
    out or is cancelled on a path that skips it, or the deferred re-arm was
    dropped by ``notify_user_input`` during the fire window -- the loop is
    left persisted ``active=true`` with a finished (or missing) timer task
    and nothing on a timer ever revives it. Without this task the only
    rescues are a gateway restart or a genuine turn completing in that
    exact slot. This task is the general backstop: it re-arms toward the loop's own
    persisted deadline, so a rescue never fires earlier than the schedule
    the user set (``_arm_from_deadline`` self-heals a cleared deadline into
    a fresh full countdown).

    The wait is scheduled through ``loop.call_later`` rather than
    ``asyncio.sleep`` on purpose: this file's own test suite (and any
    similar consumer) routinely patches module-level ``asyncio.sleep`` to
    a no-op to fast-forward the per-loop timers, and under that patch a
    sleep-based periodic task degrades into a busy loop that re-arms and
    re-fires everything continuously. A watchdog's cadence must stay on
    the wall clock regardless of how the timers it watches are driven.
    """
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    ev_loop = asyncio.get_running_loop()
    while True:
        beat: asyncio.Future[None] = ev_loop.create_future()
        handle = ev_loop.call_later(seams._RECONCILE_INTERVAL_SECS, _resolve_beat, beat)
        try:
            await beat
        finally:
            handle.cancel()
        if shutdown_event.is_set():
            return
        try:
            self._reconcile_once()
        except Exception:  # noqa: BLE001 - one bad pass must not kill the backstop
            logger.exception("AutoNudge: reconciler pass failed")


def _reconcile_once(self: AutoNudgeService) -> None:
    """One reconciler pass: rescue active loops stranded with no live timer.

    "No live timer" means the ``_timers`` entry is absent OR its task has
    finished. The finished-task form matters: nothing pops a timer task
    from ``_timers`` when it completes normally, so the stranded states
    this backstop exists for (a delivered fire whose stop hook never came,
    a timer task killed by an exception) leave a DONE task behind rather
    than an empty slot -- a membership test alone would miss every one of
    them. The absent form covers a loop whose pending timer was cancelled
    by ``notify_user_input`` and whose ``notify_turn_complete`` then never
    arrived because the slot's turn died on a hook-skipping path.

    A loop is re-armed only after TWO CONSECUTIVE passes observe it
    eligible-and-unarmed, because one observation cannot tell "stranded"
    apart from two live states that look identical for a while:

    * A slot whose user turn is still running. ``notify_user_input``
      cancelled the timer on purpose, and ``notify_turn_complete`` will
      re-arm when the turn ends. The turn-start hook also clears this
      loop's candidacy (see ``notify_user_input``), so a session showing
      any sign of life defers its rescue by a full two intervals. A turn
      that outlives BOTH intervals is re-armed anyway -- one observation
      window has to end somewhere, and the fire path's busy-slot refusal
      (plus its backoff) keeps a rescue that guessed wrong from ever
      delivering into the running turn; the wasted attempt is the cost of
      rescuing the turn that died silently, which looks identical from
      here.
    * A loop inside another coroutine's mutation window. ``update()``
      mutates fields, awaits an offloaded store write, and ROLLS BACK the
      fields if the write fails -- a single-pass reconciler could arm the
      transiently-active shape and leave a rolled-back inactive loop with
      a live timer. Two passes shrink that window, but the write has no
      timeout, so the guard that CLOSES it is the lock check below: every
      mutation runs inside ``self._lock``, this pass is synchronous, and
      a pass that finds the lock held defers entirely.

    Deliberately never touched, whatever the passes observe:

    * A loop mid-fire (``_firing``): its running task must never be
      cancelled (see ``update``), and ``_arm_timer`` cancels before it
      creates. The fire window owns its own re-arm bookkeeping.
    * A loop quiesced by administrative cleanup: cleanup owns it.
    * A monitor record whose version this gateway does not implement:
      ``_arm_from_deadline`` refuses those with an INFO line, and letting
      the reconciler retry it would repeat that line every pass forever.
    * A monitor whose wake claim is in flight with NO completion-evidence
      deadline -- EXCEPT a ``BUSY`` retry. The no-deadline shape is a
      claim that died mid-handoff: ``_load`` retires it on restart, and
      arming it here would wake a controller that answers ``NO_CHANGE``
      forever (the probe path is never reached, so no budget or cap can
      end it) -- an unretirable zombie dressed as a rescue. A ``BUSY``
      delivery is the one no-deadline shape that is legitimately LIVE:
      it proves no action turn started, and ``_load`` resumes it at its
      persisted retry deadline, so this pass must too. A claim WITH a
      deadline is safe: its ``next_due_ts`` is that deadline, and the
      armed tick either finds evidence or retires the claim through
      ``record_monitor_completion_evidence_unavailable``.
    """
    if self._lock.locked():
        # A mutation or persist is mid-flight. ``update()`` mutates loop
        # fields, awaits an offloaded store write, and ROLLS BACK the
        # fields if the write fails -- all inside ``self._lock`` -- and
        # that write has no timeout, so a wedged disk can hold the
        # transient shape across ANY number of passes; observation counts
        # alone cannot bound it. This pass is synchronous, so deferring
        # whenever the lock is held at entry makes overlap with a locked
        # mutation window impossible rather than merely unlikely.
        # Candidacies are left untouched: the deferred pass neither
        # confirms nor refutes them, and dropping them would push every
        # rescue behind a busy store's persist cadence.
        return
    eligible: set[str] = set()
    for loop in list(self._loops.values()):
        # Mirror _timer's own re-arm guard, not a stricter one: an
        # INACTIVE loop still waiting for terminal-completion evidence
        # owns a finite accepted-turn correlation whose expiry needs a
        # timer (_waits_for_terminal_completion), and losing that timer
        # to a user-input cancel with no turn-complete re-arm (the hook
        # ignores inactive loops) would otherwise strand the claim and
        # refuse every replacement watch on the slot forever.
        if not loop.active and not self._waits_for_terminal_completion(loop):
            continue
        if loop.id in self._firing or loop.id in self._maintenance_quiescing:
            continue
        if loop.approval_stalled and not is_structured_monitor_loop(loop):
            # Paused for approval: no live timer is the intended state, and
            # ``release_approval_hold`` re-arms it. Rescuing it here would only
            # wake a tick that holds again, every pass, for as long as it waits.
            continue
        monitor = loop.monitor
        if monitor is not None and monitor.version != MONITOR_STATE_VERSION:
            continue
        if (
            monitor is not None
            and monitor.wake_in_flight
            and monitor.completion_evidence_deadline <= 0
            and monitor.wake_delivery is not MonitorDispatchResult.BUSY
        ):
            # A BUSY retry is EXEMPT from this skip: it proves no action
            # turn started, its evidence deadline is intentionally empty,
            # and _load resumes exactly this shape at its persisted retry
            # deadline on restart -- so retiring it here would kill a
            # retry the store's own recovery logic considers live.
            continue
        timer = self._timers.get(loop.id)
        if timer is not None and not timer.done():
            continue
        if loop.id not in self._reconcile_candidates:
            eligible.add(loop.id)
            continue
        logger.info(
            "AutoNudge: reconciler re-arming stranded loop %s on slot %s "
            "(active with no live timer across two passes)",
            loop.id,
            loop.slot_key,
        )
        self._arm_from_deadline(loop)
    self._reconcile_candidates = eligible
