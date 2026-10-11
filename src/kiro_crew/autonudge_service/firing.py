"""One tick: its terminal bounds, the fire, its bookkeeping and the re-arm.

:func:`_timer` is the body of every loop's timer task. It applies the kill switch and
the terminal bounds in their fixed order (cycle cap, approval hold, runtime budget,
start-failure stand-down) before a turn can be spent, lets the gate decide a quiet
tick, and runs :func:`_run_fire_cycle` inside the loop's fire window, which charges a
delivered turn exactly once, keeps a refused turn owed and decides the re-arm.
:func:`fire_now` brings the next cycle forward through that same body.

Its functions are :class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound
on the class by name and runs against the service's state through ``self``, and a call
to any other service method goes through ``self`` too, so a patch on the instance
reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from kiro_crew import shutdown_event
from kiro_crew.autonudge_service.gate import (
    _WAKE_FOLLOWUP_TICKS,
    _delivery_basis,
    _record_delivered_reading,
    _record_delivered_revision,
)
from kiro_crew.autonudge_service.maintenance import _release_mutation_lock
from kiro_crew.autonudge_service.model import (
    _CONSECUTIVE_FAILURE_STANDDOWN_AFTER,
    _START_FAILURE_BACKOFF_AFTER,
    _START_FAILURE_STANDDOWN_AFTER,
    CONSECUTIVE_FAILURE_REASON,
    MONITOR_TERMINAL_REASON,
    SESSION_START_FAILURE_REASON,
    STOP_SENTINEL_REASON,
    NudgeLoop,
    is_channel_key,
    is_structured_monitor_loop,
    runtime_budget_exceeded,
)
from kiro_crew.autonudge_service.timers import (
    _REARM_BACKOFF_MAX_SHIFT,
    _REARM_BACKOFF_SECS,
    _REARM_MAX_BACKOFF_SECS,
    _current_task_or_none,
)
from kiro_crew.monitoring.models import MonitorOutcome

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService

# The service's own logger: callers and tests filter on it by name.
logger = logging.getLogger("kiro_crew.autonudge")


async def _timer(self: AutoNudgeService, loop: NudgeLoop, delay: float | None = None) -> None:
    try:
        await asyncio.sleep(loop.idle_secs if delay is None else delay)
    except asyncio.CancelledError:
        return
    if shutdown_event.is_set():
        return
    # Whether THIS tick is one a worker's push armed. Moved off the armed-timer mark
    # the moment the tick starts, so a push landing from here on is news this tick may
    # not see and is free to arm the next one; and reset on every tick, so a tick that
    # returns early below leaves nothing for a later scheduled tick to inherit.
    if loop.id in self._pushed_ticks:
        self._pushed_ticks.discard(loop.id)
        self._pushed_running.add(loop.id)
    else:
        self._pushed_running.discard(loop.id)
    # Reached before either dispatch: a fire settles through the same refused writer,
    # so an unattended turn would go out with no restart able to tell that it had.
    if self._store.load_refused:
        logger.error(
            "AutoNudge: not firing loop %s -- persistence is refused, so a delivered "
            "cycle could not be recorded; fix the store and restart",
            loop.id,
        )
        return
    if is_structured_monitor_loop(loop):
        assert loop.monitor is not None
        waiting_for_terminal_completion = self._waits_for_terminal_completion(loop)
        if not loop.active and not waiting_for_terminal_completion:
            return
        if self._on_monitor_tick is None:
            if waiting_for_terminal_completion:
                await self.record_monitor_completion_evidence_unavailable(
                    loop.id,
                    loop.monitor.last_wake_fingerprint,
                    now=time.time(),
                )
                return
            # A structured record must never fall through to legacy prompt
            # delivery when its typed controller is unavailable.
            await self._deactivate_unwired_monitor(loop.id)
            return
        self._firing.add(loop.id)
        try:
            await self._on_monitor_tick(loop)
        except Exception:
            logger.exception("structured monitor tick failed for %s", loop.id)
        finally:
            self._firing.discard(loop.id)
            self._rearm_pending.discard(loop.id)
            if (
                (loop.active or self._waits_for_terminal_completion(loop))
                and loop.id in self._loops
                and loop.next_due_ts > 0
            ):
                self._arm_from_deadline(loop)
        return
    # Kill switch: sentinel file present? The goal is finished, so the record is
    # KEPT under its own reason rather than removed: a removed row left the goal
    # popover on its empty form with nothing saying the goal was met. The file
    # stays where it was written -- the next arm on this slot unlinks it before
    # the new loop exists (``authorize_and_add_nudge``), and an inactive loop's
    # timer is never armed, so the stale file can kill nothing in between. No
    # ``expired`` here: that event says the loop stopped SHORT of its goal, and
    # this stop is the agent reporting the goal reached.
    if loop.stop_sentinel_path and Path(loop.stop_sentinel_path).exists():
        logger.info("AutoNudge: stop sentinel found for %s — deactivating loop", loop.id)
        # Retire THIS task's registration first. ``update`` runs its mutation in a
        # shielded task, from which ``_cancel_timer`` cannot recognise this timer as
        # the current task and would cancel it mid-await; popped here, from the task
        # itself, the self-guard applies and the deactivation returns normally.
        self._cancel_timer(loop.id)
        await self.update(loop.id, active=False, stopped_reason=STOP_SENTINEL_REASON)
        return
    # The person-wait HOLD, ahead of both bounds: a work-ledger watch whose open items
    # all wait on a person, unchanged since its last delivered turn, has nothing to
    # read. Held, it fires no turn, takes no floor turn (the gate below is never
    # reached), and is neither extended nor stopped by a spent bound.
    if await self._holds_for_person_wait(loop):
        return
    # Cycle cap reached?
    if loop.max_cycles and loop.cycle_count >= loop.max_cycles:
        if await self._extend_for_open_ledger(loop, "max_cycles"):
            return
        logger.info("AutoNudge: loop %s reached max_cycles — deactivating", loop.id)
        await self.update(loop.id, active=False, stopped_reason="cycle_cap")
        # Signal the cap. Reaching max_cycles is NOT a successful finish —
        # the loop ran out of cycles with its goal possibly unmet — yet without
        # a signal of its own the only trace is this log line plus an ``updated``
        # event indistinguishable from a user pressing Stop, so a capped-out
        # babysit cannot be told apart from the agent stopping on
        # its own. ``expired`` is emitted so an observer can raise a
        # notification the user actually sees.
        #
        # Emitted AFTER update() (which already persisted active=False and
        # emitted ``updated``), so a subscriber handling ``expired`` always
        # observes the loop in its final deactivated state. Deliberately a
        # NEW event kind rather than overloading ``updated``: the many
        # benign updates (message edits, manual pause) must not notify.
        self._emit("expired", loop)
        return
    # Wall-clock budget spent? Checked AFTER the cycle cap (both exhausted
    # → the cap wins, keeping historical wording) and BEFORE the fire, so
    # a spent budget never buys one more unattended turn. Same terminal
    # treatment as the cap: deactivate (inspectable/restartable, not
    # removed) and emit ``expired`` so the existing observer raises a
    # user-visible notification — a budget that stops a loop silently
    # would be indistinguishable from the agent stopping on its own.
    #
    # The approval HOLD sits between the two bounds. After the cycle cap, because
    # a held loop spends no cycles, so a cap it reached was reached before it held
    # and still ends it. Before the runtime budget, because the wall clock keeps
    # running while nobody answers: checked after it, a hold would quietly spend
    # the budget and stop the loop it exists to keep. The held time is handed back
    # to the budget on release (``release_approval_hold``).
    #
    # Recorded evidence only (``notify_approval_stalled``): a cycle's approval
    # went unanswered. Firing again would wake, be declined and spend a cycle that
    # could never work, so the tick fires nothing and arms nothing. The loop stays
    # active and inspectable (``approval_stalled`` on the record), the reconciler
    # leaves it alone, and the release re-arms it once a person is back. A wake
    # that lands meanwhile (a restart, a worker's push) reaches this line again.
    if loop.approval_stalled:
        logger.debug(
            "AutoNudge: loop %s is paused for approval -- not firing cycle %d",
            loop.id,
            loop.cycle_count + 1,
        )
        return
    if runtime_budget_exceeded(loop):
        if await self._extend_for_open_ledger(loop, "max_runtime_secs"):
            return
        logger.info(
            "AutoNudge: loop %s exceeded max_runtime_secs=%d — deactivating",
            loop.id,
            loop.max_runtime_secs,
        )
        await self.update(loop.id, active=False, stopped_reason="runtime_budget")
        self._emit("expired", loop)
        return
    # Cycles that never reach a model session. A delivered cycle whose turn
    # dies on ``session/new`` produced nothing, and firing the next one on the
    # plain interval reproduces it -- 15 times in a row on the host this came
    # from, until ``max_cycles`` happened to run out. Checked here, with the
    # other terminal bounds, on recorded evidence only
    # (``notify_cycle_start_failed``); a single landed turn on the slot clears
    # the streak, so a loop that recovers is never held back.
    #
    # Stand-down is terminal in the same shape as the other bounds
    # (deactivate, ``expired`` so the notifier tells the user rather than
    # letting it go silent, re-armable once the host recovers). Below that,
    # the wake is DEFERRED rather than spent: the delay escalates per failure
    # past the threshold and is capped by the loop's own interval, so a loop
    # under a briefly-loaded host slows to a poll instead of adding its own
    # retries to the contention.
    if loop.consecutive_start_failures >= _START_FAILURE_STANDDOWN_AFTER:
        logger.warning(
            "AutoNudge: loop %s stood down — %d consecutive cycles never got "
            "a model session, so cycle %d would spend a turn to fail the same "
            "way; it stays inspectable and can be resumed once the host "
            "recovers",
            loop.id,
            loop.consecutive_start_failures,
            loop.cycle_count + 1,
        )
        self._start_failure_deferred.pop(loop.id, None)
        await self.update(loop.id, active=False, stopped_reason=SESSION_START_FAILURE_REASON)
        self._emit("expired", loop)
        return
    if loop.consecutive_start_failures >= _START_FAILURE_BACKOFF_AFTER:
        # ONE deferral per streak value, then fire again. The streak only
        # grows on a DELIVERED cycle that fails, so deferring every wake at
        # the same value would freeze it below the stand-down threshold and
        # poll at the backoff interval forever -- a slower version of the
        # very loop this exists to end. Paying the delay once per failure
        # lets the next cycle either recover (a landed turn clears the
        # streak) or advance it toward the stand-down.
        already = self._start_failure_deferred.get(loop.id)
        if already != loop.consecutive_start_failures:
            self._start_failure_deferred[loop.id] = loop.consecutive_start_failures
            shift = min(
                loop.consecutive_start_failures - _START_FAILURE_BACKOFF_AFTER,
                _REARM_BACKOFF_MAX_SHIFT,
            )
            backoff = min(
                _REARM_BACKOFF_SECS * (2**shift),
                _REARM_MAX_BACKOFF_SECS,
                loop.idle_secs,
            )
            logger.info(
                "AutoNudge: loop %s deferring cycle %d by %gs — %d consecutive "
                "cycles never got a model session",
                loop.id,
                loop.cycle_count + 1,
                backoff,
                loop.consecutive_start_failures,
            )
            self._arm_timer(loop, delay=backoff)
            return
    # Own cycles that keep FAILING. A delivered cycle that reached a session and
    # dispatched but died -- turn outcome ``error`` or ``timeout`` -- produced
    # nothing, and firing the next one on the plain interval reproduces it. The
    # three bounds above each catch one deterministic sub-case (a malformed
    # payload, an unanswered approval, a session that never started); a turn that
    # ran and errored is none of them, so without this a loop firing every
    # interval into a turn that always fails burns its whole cycle cap producing
    # nothing. Checked here with the other terminal bounds, on recorded evidence
    # only (``notify_cycle_failed``); a single landed turn on the slot clears the
    # streak, so a loop that recovers is never held back. No deferral tier: a
    # failed turn already spent its own time (a start failure fails fast, so its
    # deferral throttles a tight retry loop that this one does not have), and the
    # stand-down threshold is set high enough that a couple of transient errors
    # pass through untouched.
    #
    # Terminal in the same shape as the other bounds: deactivate (inspectable and
    # restartable, not removed), ``expired`` so the notifier tells the user it
    # stopped rather than finished, and re-armable once the backend or tool
    # recovers.
    if loop.consecutive_failed_cycles >= _CONSECUTIVE_FAILURE_STANDDOWN_AFTER:
        logger.warning(
            "AutoNudge: loop %s stood down — %d consecutive cycles failed with "
            "nothing landing between them, so cycle %d would spend a turn to fail "
            "the same way; it stays inspectable and can be resumed once the cause "
            "clears",
            loop.id,
            loop.consecutive_failed_cycles,
            loop.cycle_count + 1,
        )
        await self.update(loop.id, active=False, stopped_reason=CONSECUTIVE_FAILURE_REASON)
        self._emit("expired", loop)
        return
    # Fire. Update state only if the callback reports actual delivery —
    # otherwise skipped nudges (e.g. slot mid-turn) inflate cycle_count and
    # prematurely trip max_cycles. Missing callback → nothing to deliver.
    if self._on_fire is None:
        return
    # Probe gate. For a monitor loop, decide whether this tick is worth a
    # turn BEFORE spending one: a quiet tick returns here having cost one
    # bounded subprocess and no model call at all, which is the entire
    # saving this path exists for. A loop with no monitor -- and a monitor
    # whose subject has no probe, or whose probe failed -- falls straight
    # through to the unchanged legacy fire, so the absence of a gate can
    # never be the reason a loop goes silent.
    #
    # This moves what ``max_cycles`` bounds. ``cycle_count`` only advances on
    # a DELIVERED fire, so for a gated loop the cap counts delivered TURNS
    # rather than ticks. Not "wakes": a floor delivery, a fallback and a
    # follow-up are all delivered turns that advance it, and only quiet ticks
    # are free. Calling it wakes would undercount what the number actually
    # bounds, which is what the user pays for. A watch can still sit on a pull
    # request for days inside a small cap, which is the intended reading of the
    # number, and monitor_start's own description says so at the arming surface.
    #
    # Exception-safe on purpose, and exception-safe in the SPENDING
    # direction. The gate resolves every uncertainty it can reason about
    # toward firing, and an exception ESCAPING it means its own failure
    # handling was bypassed -- so the escape is treated exactly like every
    # other uncertain observation: not quiet, fall through to the fire.
    # Letting it escape here instead killed the timer task outright:
    # ``self._timers`` holds a strong reference, so the dead task was never
    # garbage-collected, "Task exception was never retrieved" was never
    # emitted, and the loop sat persisted active with nothing left to wake
    # it. Skipping the tick would be the other wrong answer: a gate that
    # raises deterministically would keep the loop alive, re-arming and
    # delivering nothing forever -- the silent-mute shape this whole gate
    # is documented to avoid ("every uncertain path resolves toward
    # spending"). Firing keeps the loop doing its job with the gate's
    # saving lost for that tick, and the traceback makes the defect loud.
    try:
        tick_is_quiet = await self._monitor_tick_is_quiet(loop)
    except Exception:  # noqa: BLE001 - any escape here used to kill the timer
        logger.exception(
            "AutoNudge: probe gate failed for loop %s -- treating the tick "
            "as not quiet and firing",
            loop.id,
        )
        tick_is_quiet = False
    pushed = loop.id in self._pushed_running
    self._pushed_running.discard(loop.id)
    if tick_is_quiet:
        # A quiet tick MUST re-arm itself. Nothing else will: the delivered
        # paths re-arm through notify_turn_complete (dashboard slots) or
        # through the fire cycle's own exit (channel keys), and a quiet tick
        # reaches neither. Returning here without arming would make the FIRST
        # quiet observation the last one the watch ever makes -- the exact
        # silent failure this gate is otherwise built to avoid, and invisible
        # from outside because a dead watch and a calm one look identical.
        #
        # Self-re-arm from inside the running timer is the supported pattern
        # (see _cancel_timer, which refuses to cancel the current task), and
        # is what the delivered path's own `finally` already does.
        #
        # ONLY while the loop is still live, though. "Do not spend a turn" and
        # "keep watching" are different answers, and the terminal verdict
        # returns the first while having just deactivated the loop: re-arming
        # on that would poll a merged pull request forever and re-emit its
        # expiry notification on every tick. Registration is checked too, so
        # a loop removed during the observation is not resurrected by its own
        # in-flight tick.
        if loop.active and loop.id in self._loops:
            now = time.time()
            fresh = now + loop.idle_secs
            if pushed and loop.next_due_ts > now:
                # A tick a worker's push brought forward is EXTRA. Re-arming it at a
                # full interval from now would move a deadline that was still ahead
                # further out, so every quiet push -- a worker's turn ending, say --
                # would delay the scheduled check it was meant to precede. The earlier
                # of the two keeps the slow tick where it was.
                loop.next_due_ts = min(loop.next_due_ts, fresh)
            else:
                loop.next_due_ts = fresh
            self._persist_soon()
            self._arm_from_deadline(loop)
        return
    self._firing.add(loop.id)
    try:
        await self._run_fire_cycle(loop)
    finally:
        self._firing.discard(loop.id)
        # A re-arm requested DURING the fire window (a dashboard turn that
        # completed while we were still persisting) was deferred rather than
        # applied, because applying it would have cancelled this very task
        # mid-persist. Apply it now that the window is closed — dropping it
        # would leave a dashboard loop with no armed timer at all, since the
        # delivered path relies on notify_turn_complete for those slots.
        # A pull-forward refused during the same window is applied here too, and at
        # delay ZERO rather than toward the deadline: it was asking for a cycle NOW
        # because a worker had just written something, and the cycle that was in flight
        # read the ledger before that write landed. Draining it in the same place as the
        # deferred re-arm is deliberate -- one release point per claim, and the immediate
        # arm supersedes the deadline one when both are held, since a deadline re-arm
        # would discard the news.
        pulled_forward = loop.id in self._pulled_forward
        self._pulled_forward.discard(loop.id)
        if loop.id in self._rearm_pending or pulled_forward:
            self._rearm_pending.discard(loop.id)
            if loop.active and loop.id in self._loops:
                if pulled_forward:
                    self._arm_timer(loop, delay=0.0)
                    self._pushed_ticks.add(loop.id)
                else:
                    self._arm_from_deadline(loop)


async def _run_fire_cycle(self: AutoNudgeService, loop: NudgeLoop) -> None:
    """Fire once, then persist bookkeeping and decide the re-arm.

    Runs entirely inside the caller's ``_firing`` window so a concurrent
    ``update()`` never cancels this task between delivery and persistence.
    """
    if self._on_fire is None:
        return
    # Mark the fire window so a concurrent update() defers its re-arm
    # instead of cancelling this task mid-turn (see update()). The window
    # stays open through the post-delivery bookkeeping and the re-arm
    # decision, NOT just the callback: clearing it the moment _on_fire
    # returned let a waiting update() cancel this task while it was parked
    # on _persist_locked(), so the delivered cycle was never written and the
    # loop could run extra cycles after a restart. _run_fire_cycle owns the
    # window; this method is the body.
    # Read before the dispatch, which is a long await an owner can re-aim the loop
    # inside; see ``_record_delivered_reading``.
    delivery_basis = _delivery_basis(loop)
    try:
        delivered = await self._on_fire(loop)
    except Exception:
        delivered = False
        # Full traceback only on the first failure of a streak; subsequent
        # failures stay at debug so a permanently-wedged callback can't spam
        # a traceback every re-arm.
        if self._rearm_fail_count.get(loop.id, 0) == 0:
            logger.exception("AutoNudge fire callback failed for %s", loop.id)
        else:
            logger.debug(
                "AutoNudge fire still failing for %s (streak=%d)",
                loop.id,
                self._rearm_fail_count.get(loop.id, 0) + 1,
            )
    claimed_wake = loop.id in self._pending_monitor_wake
    # Taken, not merely tested: the claim carries the wake's own text, and the
    # re-owe below has to put the same text back rather than an empty one.
    claimed_briefs = self._pending_monitor_wake.pop(loop.id, "")
    claimed_floor = loop.id in self._pending_floor_tick
    self._pending_floor_tick.discard(loop.id)
    if claimed_wake and loop.monitor is not None:
        # Delivery is settled either way now -- landed or refused -- so the doubt
        # the wake carried across the fire is discharged here. A debounced write
        # is enough: this process survived, and a lost clear only costs one extra
        # fire on the next tick.
        loop.monitor.poll_in_flight = False
        self._persist_soon()
    if not delivered and loop.monitor is not None and loop.id in self._loops:
        # A REFUSED fire must not consume the tick that earned it. Two credits
        # are at stake and both are spent by the time we get here: a claimed
        # wake, and a follow-up allowance the gate decremented to let this
        # tick through. Neither can be recovered by simply re-arming, because
        # the kernel has already DEDUPED the observation this fire was
        # carrying -- the next tick would look at an unchanged subject, judge
        # it quiet, and the signal would not come back until the streak floor.
        # A busy slot is ordinary (the user is typing), so that is a routine
        # path to losing a real wake.
        #
        # Granting one gate-free tick makes the next tick RETRY the delivery
        # instead of re-observing. Set rather than incremented, so a
        # permanently refusing callback cannot accumulate an unbounded
        # bypass; the existing per-failure backoff bounds how fast it retries.
        loop.monitor.followup_ticks = _WAKE_FOLLOWUP_TICKS
        if claimed_wake:
            # And keep the wake OWED. The claim was discarded above on the
            # assumption that reaching here meant the wake had been accounted
            # for, but a refused fire accounts for nothing: the next tick takes
            # the observation-free bypass, DELIVERS the turn, and finds no claim
            # to charge -- so a wake that really happened and really woke the
            # agent was missing from ``wakes`` entirely. That counter is the one
            # artifact this PR exists to make trustworthy, and omitting a
            # delivered wake makes the saving look BETTER than it is, which is
            # the same dishonesty as the zero nothing surfaced before.
            #
            # Re-adding cannot double-charge: the charge happens once, at the
            # single point delivery is confirmed, and the claim is discarded
            # there. A retry that is refused again re-owes it, which is correct
            # and bounded by the same backoff that bounds the retry itself.
            #
            # Re-owed WITH ITS TEXT. The retry takes the observation-free bypass, so
            # it never re-reads the probe and the briefs cannot be rebuilt: an empty
            # re-owe here would deliver the news as a bare cycle header, which is the
            # whole defect the text was added to close.
            self._pending_monitor_wake[loop.id] = claimed_briefs
        if claimed_floor:
            # Same reasoning: a refused floor delivery spent nothing, so the charge
            # stays owed rather than being recorded or dropped.
            self._pending_floor_tick.add(loop.id)
        self._persist_soon()
    if delivered:
        # BEFORE the settlement below, not after. That block carries a comment
        # forbidding an early RETURN precisely so this bookkeeping still runs --
        # but a re-raised ``CancelledError`` leaves by the same door a return
        # would, and the terminal write deliberately DRAINS before propagating,
        # so the loop would be committed as finished while the turn that carried
        # the news went uncounted. Recording a delivery that has already happened
        # cannot be wrong; deferring it past a re-raise can.
        self._rearm_fail_count.pop(loop.id, None)
        loop.cycle_count += 1
        loop.last_fire_ts = time.time()
        # What this turn's ledger looked like, read at the top of its tick: the
        # person-wait hold compares later ticks against it. Read BEFORE the turn,
        # so a write that lands during it counts as news for the next tick.
        tick_fp = self._ledger_tick_fp.pop(loop.id, None)
        if tick_fp is not None:
            loop.ledger_seen_fp = tick_fp
        # The turn really went out, so the verdict that asked for it can now be
        # labelled by what that turn does. Stamped HERE and nowhere earlier, for the
        # same reason the flag below is cleared here: a refused fire, a timer the
        # owner's own input cancelled, and a busy slot all settle nothing, and a row
        # marked delivered in any of those cases would be handed the actions of
        # whatever turn finishes next.
        self._confirm_judge_delivery(loop)
        if loop.judge_wake_pending:
            # The owed judge wake landed, so the doubt it carried across the fire is
            # discharged -- here and nowhere earlier, because a refused fire settles
            # nothing and must leave the turn owed. A debounced write is enough: this
            # process survived, and a lost clear costs one extra fire rather than a
            # missed one, which is the direction to be wrong in.
            loop.judge_wake_pending = False
            self._persist_soon()
    if delivered and loop.monitor is not None and loop.monitor.terminal_pending:
        # The owed turn landed, so the watch can be closed now -- and only now.
        # Until this point the loop stayed live on purpose, so a refused fire
        # would re-arm and retry rather than leave the channel unaware. The
        # probe re-raises a terminal state on every tick (it is not deduped),
        # which is what makes that retry converge.
        # SERIALIZED, like the gate's own settlement. ``update`` takes the
        # MAINTENANCE lock and awaits inside it, so without holding that same
        # lock a retarget could land between the read and the write here and
        # have its new subject deactivated by the old subject's finish. This is
        # the site the previous round named as still open; closing it needs the
        # lock, not merely the ordering fix that round shipped.
        #
        # Safe to take here: this runs after ``_on_fire`` has returned, and the
        # timer that called us does not hold the lock -- the same evidence that
        # lets the gate's settlement take it.
        settle_lock = await self._acquire_mutation_lock(loop.id)
        # No early RETURN in here: the rest of this fire cycle still has to
        # charge the wake and run its re-arm bookkeeping. Skipping that to bail
        # out of a settlement would trade one defect for another.
        if settle_lock is not None:
            try:
                # Re-read under the lock, and re-check that the monitor is still
                # THERE. Waiting for the lock is an await, so a retarget can
                # clear ``loop.monitor`` to None in that gap -- and dereferencing
                # it then raises out of the fire cycle, which leaves the newly
                # retargeted loop active with no timer: a watch that never ticks
                # again. The earlier checks covered the debt and the
                # registration but not the object itself.
                monitor = loop.monitor
                pending = monitor.terminal_pending if monitor is not None else ""
                settle_now = bool(monitor is not None and pending and loop.id in self._loops)
                if settle_now and monitor is not None:
                    if not await self._terminal_still_holds(loop, monitor):
                        # The subject came back while the turn was being delivered.
                        # Every earlier guard for a reopened subject lives on the
                        # NEXT TICK -- the debt clearing and the forced
                        # re-observation -- and this
                        # settlement runs before any tick can happen, so the window
                        # between the terminal observation and the turn landing had
                        # no evidence in it at all. A channel turn runs inline and
                        # can take minutes, which is long enough for a pull request
                        # to be reopened.
                        #
                        # Settling is the one action that STOPS work, so it needs a
                        # CONFIRMED terminal rather than merely an unrefuted one:
                        # anything else -- reopened, or simply unobservable -- leaves
                        # the watch alive. Failure resolves toward spending, here as
                        # everywhere else in this file.
                        #
                        # SKIPPED, not returned from. This block's own comment forbids
                        # an early exit because the rest of the fire cycle still has
                        # to run, and a re-raise leaving through the same door breaks
                        # that exact rule.
                        monitor.terminal_pending = ""
                        self._persist_soon()
                        logger.info(
                            "AutoNudge: loop %s had its subject come back while the "
                            "final turn was delivered -- dropping the owed settlement "
                            "and keeping the watch alive",
                            loop.id,
                        )
                        settle_now = False
                if settle_now and monitor is not None:
                    restore = (
                        pending,
                        monitor.outcome,
                        monitor.stopped_reason,
                        monitor.stopped_at,
                        loop.active,
                        loop.stopped_reason,
                    )
                    monitor.terminal_pending = ""
                    monitor.outcome = (
                        MonitorOutcome.SUCCESS if pending == "success" else MonitorOutcome.BLOCKED
                    )
                    monitor.stopped_reason = MONITOR_TERMINAL_REASON
                    monitor.stopped_at = time.time()
                    loop.stopped_reason = MONITOR_TERMINAL_REASON
                    loop.active = False
                    # PERSIST BEFORE ANNOUNCING -- the same rule the gate's own
                    # settlement follows. This site was added two rounds later
                    # and did not inherit it: the delivered path does reach a
                    # write further down, but it is AFTER the emit, so a failed
                    # write left memory reporting a finish while the record
                    # still said active-and-owed, and the restart would deliver
                    # the final turn a second time.
                    try:
                        async with self._lock:
                            await self._write_monitor_snapshot_locked()
                    except asyncio.CancelledError:
                        # Committed before the cancellation propagates, so the
                        # user must hear it now or never -- a restart reads the
                        # loop as settled, owing no further turn.
                        self._emit("expired", loop)
                        raise
                    except Exception:
                        (
                            monitor.terminal_pending,
                            monitor.outcome,
                            monitor.stopped_reason,
                            monitor.stopped_at,
                            loop.active,
                            loop.stopped_reason,
                        ) = restore
                        logger.exception(
                            "AutoNudge: could not persist the delivered terminal "
                            "settlement for %s -- leaving the watch live so it "
                            "retries",
                            loop.id,
                        )
                    else:
                        self._emit("expired", loop)
            finally:
                _release_mutation_lock(settle_lock)
    if claimed_wake and delivered and loop.monitor is not None:
        # The turn happened, so it is a wake, and only now does the agent own
        # work the probe cannot see -- which is what the follow-up allowance
        # protects. A refused fire falls through here uncharged.
        #
        # No persist call of its own: the delivered path below reaches
        # ``await self._persist_locked()`` with no await in between, so these
        # counters are already in the state that write serialises -- and that
        # write is the stronger one, since it holds the lock and cannot be
        # clobbered by a concurrent update()'s snapshot.
        loop.monitor.wakes += 1
        loop.monitor.followup_ticks = _WAKE_FOLLOWUP_TICKS
    if delivered and claimed_floor and loop.monitor is not None:
        # The floor's turn happened. No follow-up allowance goes with it: the floor
        # exists to break a silence, not to protect work the agent had already
        # started, so there is nothing in progress for a bypassed tick to shield.
        loop.monitor.floor_ticks += 1
        # And the durable debt is discharged HERE and nowhere earlier, because this
        # is the one place the turn is known to have landed. A refused fire keeps it
        # owed alongside the re-taken claim above, and so does a process that never
        # reaches this line -- which is the case the in-memory claim alone cannot
        # carry. The clear may ride the delivered path's own write: losing it costs
        # one extra fire, which is the direction to be wrong in.
        loop.monitor.floor_fire_pending = False
    if delivered and loop.monitor is not None:
        # A work-ledger watch remembers the revision this turn was delivered at, so
        # a later quiet floor can tell whether the ledger moved since. Rides the
        # delivered path's own write, like the counters above.
        _record_delivered_revision(loop.monitor)
        # And a pull-request watch remembers the short reading this turn carried, if
        # any, so a repeat of it can be held instead of delivering again.
        _record_delivered_reading(loop, delivery_basis)
    if not delivered:
        # If the fire path already removed the loop (e.g. slot missing →
        # remove()), do NOT resurrect it with a fresh timer — that would
        # orphan-poll forever. Clear the streak and stop.
        if loop.id not in self._loops:
            self._rearm_fail_count.pop(loop.id, None)
            return
        # A concurrent update() may have DEACTIVATED this loop while the
        # callback was in flight; that update deliberately deferred the
        # cancel to avoid killing the turn, so the failure path must honour
        # the pause instead of re-arming. Otherwise "stop the loop" during a
        # cycle whose delivery then fails silently resumes unattended tool
        # execution.
        if not loop.active:
            logger.info(
                "AutoNudge: loop %s was deactivated mid-fire — not re-arming",
                loop.id,
            )
            self._rearm_fail_count.pop(loop.id, None)
            return
        # Slot was busy mid-turn, or the fire callback errored. Do NOT end
        # the loop — re-arm so it self-heals and never depends solely on the
        # external notify_turn_complete hook (skipped on a slot's error/
        # timeout/cancel exit paths). Escalate the delay per consecutive
        # failure so a never-delivering loop backs off to a slow poll
        # instead of hammering, capped by idle_secs and _REARM_MAX_BACKOFF.
        n = self._rearm_fail_count.get(loop.id, 0) + 1
        self._rearm_fail_count[loop.id] = n
        shift = min(n - 1, _REARM_BACKOFF_MAX_SHIFT)
        backoff = min(
            _REARM_BACKOFF_SECS * (2**shift),
            _REARM_MAX_BACKOFF_SECS,
            loop.idle_secs,
        )
        self._arm_timer(loop, delay=backoff)
        return
    # Delivered — the failure streak and the turn accounting were already
    # recorded above, before the terminal settlement could re-raise past them.
    loop.next_due_ts = 0.0
    # Persist through the shared locked+offloaded path so this bookkeeping
    # cannot be clobbered by a concurrent update()'s snapshot (and so the
    # fsync stays off the event loop).
    await self._persist_locked()
    # At INFO, deliberately. Without a line per delivered fire, a loop that
    # died and a loop with nothing to report are byte-identical in the
    # journal. One line per DELIVERED turn -- each of
    # which already spends a model turn, so the log can never outpace the
    # work -- is what makes both this loop's health and the reconciler's
    # rescues observable from outside the process.
    logger.info(
        "AutoNudge: loop %s fired cycle %d on slot %s (delivered)",
        loop.id,
        loop.cycle_count,
        loop.slot_key,
    )
    # Only for a loop still in the store: a default patrol displaced mid-wake
    # (``mutations.detach_firing_default_timer``) still delivers, and a "fired"
    # frame for its removed row would resurrect it in the dashboard.
    if loop.id in self._loops:
        self._emit("fired", loop)
    # POST-DELIVERY budget check: the budget gates when turns START, so a
    # slow in-flight turn can overshoot it (bounded by the transport's
    # per-turn ceiling, constants.CHAT_TURN_TIMEOUT — this service must
    # not cancel a running turn; see the mid-fire contracts above). But
    # once the turn HAS finished, a spent budget must take effect NOW —
    # deactivating here instead of on the next idle timer closes the
    # window where notify_turn_complete arms another full idle cycle for
    # a loop that is already over budget.
    if runtime_budget_exceeded(loop) and loop.active and loop.id in self._loops:
        logger.info(
            "AutoNudge: loop %s exceeded max_runtime_secs=%d during its turn "
            "— deactivating post-delivery",
            loop.id,
            loop.max_runtime_secs,
        )
        await self._update_unserialized(loop.id, active=False, stopped_reason="runtime_budget")
        self._emit("expired", loop)
        return
    # Channel-bound loops (Slack/Discord/...) have no dashboard
    # turn-lifecycle hook to re-arm them (notify_turn_complete never fires
    # for these keys), so they self-re-arm on a fixed interval. The fire
    # callback runs the turn inline, so the next fire lands idle_secs
    # after the previous turn finished; the busy-skip + backoff above
    # handles any overlap.
    if is_channel_key(loop.slot_key) and loop.active and loop.id in self._loops:
        self._arm_from_deadline(loop)


# How much one automatic extension of a work-ledger watch adds: a quarter of the
# bound as it stands, never less than these floors. A quarter keeps the loop's
# own "10% or less left" budget line meaningful between extensions, and the
# floors keep a small cap from being extended one cycle at a time.
_LEDGER_EXTEND_SHARE = 4
_LEDGER_EXTEND_MIN_CYCLES = 10
_LEDGER_EXTEND_MIN_RUNTIME_SECS = 3600


def _ledger_has_open_items(conductor_key: str) -> bool:
    """Whether *conductor_key*'s ledger still has open work. Blocking: call off-loop.

    Read through the work-ledger probe, the store's in-process seam for watches.
    """
    from kiro_crew.probes.work_ledger import has_open_items

    return has_open_items(conductor_key)


def _ledger_person_wait_fp(
    conductor_key: str, worker_closed: Callable[[str], bool] | None = None
) -> str | None:
    """The ledger's fingerprint while every open item waits on a person. Blocking."""
    from kiro_crew.probes.work_ledger import person_wait_fingerprint

    return person_wait_fingerprint(conductor_key, worker_closed=worker_closed)


async def _holds_for_person_wait(self: AutoNudgeService, loop: NudgeLoop) -> bool:
    """Whether this tick of a work-ledger watch holds because everyone waits on a person.

    Holds when every open item is ``blocked`` / ``question`` with reason ``approval``
    or ``needs_human`` AND the ledger's fingerprint equals the one recorded at the
    loop's last delivered turn (``ledger_seen_fp``). The fingerprint covers only
    worker-owned fields, so a worker's report releases the hold on the next tick with
    no re-arm (its push also pulls that tick forward); the conductor's own writes
    leave it held.

    A held tick re-arms at the loop's own interval, the way a quiet tick does, so the
    next check costs one ledger read and no model call. Doubt never holds: an
    unreadable ledger, or a loop with no recorded fingerprint yet, fires as before.
    True when the tick is held and the caller must return.
    """
    # An approval-stalled loop already holds further down, arming nothing; this
    # hold's interval re-arm must not wake it on a schedule.
    if not loop.active or loop.approval_stalled or not self._observes_work_ledger(loop):
        self._ledger_tick_fp.pop(loop.id, None)
        return False
    fp = await asyncio.get_running_loop().run_in_executor(
        None, _ledger_person_wait_fp, loop.slot_key, self._worker_closed
    )
    self._ledger_tick_fp[loop.id] = fp or ""
    if fp and loop.ledger_seen_fp == fp:
        if not (loop.active and self._loops.get(loop.id) is loop):
            return True
        if not loop.waiting_on_person:
            # Persist before publishing, as the approval hold does: the hold and its
            # start time are written under the lock and the write is awaited before
            # anything is announced. A refused write restores both and fires as
            # before -- doubt never holds.
            if not await _commit_person_wait(self, loop, held=True):
                return False
            logger.warning(
                "AutoNudge: loop %s holds -- every open work-ledger item waits on a "
                "person and nothing changed since its last turn; it fires nothing "
                "until the ledger moves",
                loop.id,
            )
            self._emit("updated", loop)
        loop.next_due_ts = time.time() + loop.idle_secs
        self._persist_soon()
        self._arm_from_deadline(loop)
        return True
    if loop.waiting_on_person:
        if not await _commit_person_wait(self, loop, held=False):
            # The store still says held, so the loop stays held and re-checks on its
            # interval rather than acting on a release nothing recorded.
            loop.next_due_ts = time.time() + loop.idle_secs
            self._arm_from_deadline(loop)
            return True
        logger.warning(
            "AutoNudge: loop %s released from its person-wait hold -- the ledger moved",
            loop.id,
        )
        self._emit("updated", loop)
    return False


async def _commit_person_wait(svc: AutoNudgeService, loop: NudgeLoop, *, held: bool) -> bool:
    """Write the person-wait hold's start (*held*) or its release, durably. Blocking-free.

    The release hands the held time back to ``created_ts``, as the approval hold
    does: a hold fires nothing, so it must spend neither the runtime budget nor the
    backstop, and the tick that carries the news must not be the one that stops the
    loop. A stored start that is not a finite positive number (a hand-edited row)
    credits nothing rather than raising. True once the write landed; on a refused
    write every field is restored and False is returned.
    """
    async with svc._lock:
        # Staged on a copy and written first: the live loop -- which the REST row, the
        # inspect reading and the state frame read without the lock -- takes the new
        # fields only once the store has them, as ``release_approval_hold`` does.
        now = time.time()
        if held:
            new = (True, now, loop.created_ts)
        else:
            since = loop.waiting_on_person_at
            created = loop.created_ts
            if (
                isinstance(since, (int, float))
                and not isinstance(since, bool)
                and 0 < since <= now
                and isinstance(created, (int, float))
                and not isinstance(created, bool)
                and created > 0
            ):
                created = created + (now - since)
            new = (False, 0.0, created)
        staged = deepcopy(loop)
        staged.waiting_on_person, staged.waiting_on_person_at, staged.created_ts = new
        try:
            await svc._write_monitor_snapshot_locked(
                svc._monitor_snapshot_with_replacement(loop, staged)
            )
        except asyncio.CancelledError:
            # Settled before the re-raise: the write is durable, so keep it.
            loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts = new
            raise
        except Exception:
            logger.warning(
                "AutoNudge: loop %s person-wait %s was not recorded -- the store "
                "refused the write",
                loop.id,
                "hold" if held else "release",
                exc_info=True,
            )
            return False
        loop.waiting_on_person, loop.waiting_on_person_at, loop.created_ts = new
    return True


def _ledger_backstop_secs() -> int:
    """The runaway backstop: the configured monitoring runtime ceiling."""
    try:
        from kiro_crew.monitoring.limits import runtime_ceiling_secs

        return int(runtime_ceiling_secs())
    except Exception:  # noqa: BLE001 - a config fault falls back to the shipped value
        from kiro_crew.monitoring.limits import DEFAULT_RUNTIME_CEILING_SECS

        return DEFAULT_RUNTIME_CEILING_SECS


async def _extend_for_open_ledger(self: AutoNudgeService, loop: NudgeLoop, bound: str) -> bool:
    """Raise *loop*'s spent *bound* when it watches a ledger that still has open work.

    A work-ledger watch is how a conductor learns its workers moved, and its job
    ends when the ledger does: every item closed is the probe's own terminal
    settlement. A cycle cap or runtime budget running out first would end the
    patrol with items still open, and nothing re-arms it. So for such a loop the
    spent bound is raised server-side instead -- ``max_cycles`` to the count plus
    a quarter of the cap (at least ``_LEDGER_EXTEND_MIN_CYCLES``),
    ``max_runtime_secs`` to the loop's age plus a quarter of the budget (at least
    ``_LEDGER_EXTEND_MIN_RUNTIME_SECS``) -- and logged at WARNING. A user or agent
    stop is untouched: this runs only when the timer is about to apply a bound.

    The RUNAWAY BACKSTOP is the configured monitoring runtime ceiling
    (``monitoring.max_runtime_secs``, seven days as shipped), measured as the
    loop's age from ``created_ts``. Past it nothing is extended, the bound stops
    the loop as before, and that refusal is logged at WARNING too. A runtime
    extension is also clamped to the ceiling, so it never writes a budget the
    update path would refuse.

    True when the bound was raised: the update re-armed toward the deadline and
    this tick returns, so the cycle runs on the next tick through the whole ladder
    again rather than firing from a tick that already passed the gate. False
    leaves the caller's terminal treatment in charge. A loop with no
    ``created_ts`` has no age to measure the backstop from and is never extended.
    """
    # Only a RUNNING loop: a stopped one reaching this tick (a timer that outlived
    # a pause) is no patrol to keep, and raising its bound would rewrite a stop.
    if not loop.active or not self._observes_work_ledger(loop):
        return False
    created = loop.created_ts
    if isinstance(created, bool) or not isinstance(created, (int, float)) or created <= 0:
        return False
    now = time.time()
    age = max(0.0, now - created)
    ceiling = _ledger_backstop_secs()
    if age >= ceiling:
        logger.warning(
            "AutoNudge: loop %s watches a work ledger but is %.0fs old, past the %ds "
            "runaway backstop -- not extending %s; the bound stops it",
            loop.id,
            age,
            ceiling,
            bound,
        )
        return False
    if not await asyncio.get_running_loop().run_in_executor(
        None, _ledger_has_open_items, loop.slot_key
    ):
        return False
    # Detach THIS tick from the timer table before the update. ``update`` cancels
    # the loop's registered timer from inside its own shielded task, where the
    # running tick is not "the current task", so it would cancel this tick
    # mid-await and the WARNING and the re-arm below would never run. Detached,
    # the update finds no timer to cancel and arms one of its own.
    if self._timers.get(loop.id) is _current_task_or_none():
        self._timers.pop(loop.id, None)
    if bound == "max_cycles":
        current = int(loop.max_cycles)
        raised = loop.cycle_count + max(_LEDGER_EXTEND_MIN_CYCLES, current // _LEDGER_EXTEND_SHARE)
        updated = await self.update(loop.id, max_cycles=raised)
    else:
        current = int(loop.max_runtime_secs)
        step = max(_LEDGER_EXTEND_MIN_RUNTIME_SECS, current // _LEDGER_EXTEND_SHARE)
        raised = min(int(age) + step, ceiling)
        if raised <= current:
            return False
        updated = await self.update(loop.id, max_runtime_secs=raised)
    if updated is None or not updated.active:
        return False
    logger.warning(
        "AutoNudge: loop %s reached %s=%d with work-ledger items still open -- "
        "extended it to %d (runaway backstop %ds, loop age %.0fs)",
        loop.id,
        bound,
        current,
        raised,
        ceiling,
        age,
    )
    self._arm_from_deadline(updated)
    return True


def _pushed_arm_delay(loop: NudgeLoop, requested: float, now: float) -> float:
    """*requested* seconds, clamped so a push only ever moves a fire EARLIER.

    A pull-forward is allowed to bring a loop's next cycle closer and nothing else, so
    a batching window may not outlive the deadline the loop already holds: arming past
    it would push the loop's own scheduled tick out by the difference, because
    :func:`_arm_timer` replaces the armed timer rather than adding a second one. A loop
    with no deadline yet (``next_due_ts`` is 0 -- a just-delivered fire, a legacy store
    entry) has nothing to be later than, so the window stands as asked.

    Returns ``0.0`` for anything not a positive window, which is the immediate arm every
    caller had before a window existed.
    """
    if requested <= 0:
        return 0.0
    if loop.next_due_ts <= 0:
        return requested
    return max(min(requested, loop.next_due_ts - now), 0.0)


async def fire_now(
    self: AutoNudgeService, loop_id: str, *, defer_if_firing: bool = False, delay: float = 0.0
) -> tuple["NudgeLoop | None", str, int]:
    """Bring one loop's next cycle forward to now, out of band from its countdown.

    Returns ``(loop, "", 200)`` once the cycle is armed to run, or
    ``(None, reason, status)`` on refusal — the ``(obj, error, status)``
    shape the authz chokepoints in :mod:`kiro_crew.autonudge_authz` already
    use, so the HTTP handler stays a thin mapping.

    WHAT THIS DELIBERATELY DOES NOT DO: it does not deliver the nudge
    itself. It re-arms through :meth:`_arm_timer`, so the cycle runs inside
    the ordinary :meth:`_timer` body — the stop sentinel, the cycle cap, the
    wall-clock budget, the approval hold and the probe gate all apply
    exactly as they do on a scheduled tick, and the delivery goes through the
    one ``_on_fire`` path. Calling :meth:`_run_fire_cycle` directly would have
    needed that whole ladder restated here, and a second copy of a
    five-condition gate is a divergence waiting to happen.

    ``delay=0.0`` rather than :meth:`_arm_from_deadline`'s
    ``_OVERDUE_REARM_SECS`` beat. That beat exists so an elapsed deadline
    does not ambush a user mid-conversation — they keep deferring it simply
    by typing. A manual trigger IS the user asking, so the condition the beat
    protects against is not present.

    ``delay`` arms the pulled-forward cycle after a BATCHING WINDOW instead of at
    once, and is the whole mechanism behind the work-ledger wake batch: several
    workers reporting inside one window share the single tick the first of them
    armed, because that tick reads the ledger when it runs rather than when it was
    armed. The window is clamped to the loop's own deadline
    (:func:`_pushed_arm_delay`), so a push still only ever moves a fire EARLIER --
    the arm replaces the armed timer, and an unclamped window on a loop about to
    fire anyway would delay the loop's own scheduled tick. Zero, the default, is the
    immediate arm every caller had before the parameter existed; a caller with a
    person behind it keeps it, since nobody presses a button to wait a minute.

    Three refusals, and each one is load-bearing rather than defensive:

    * **Not registered** -> 404. Nothing to fire. This is also where the stop
      SENTINEL lands: it goes through ``remove``, so the loop is gone rather
      than merely inactive.
    * **Not active** -> 409. The non-removing terminal bounds — the cycle
      cap, the wall-clock budget and the failure stand-downs — all leave the loop
      registered but inactive, so this ONE condition covers them without
      restating the list. A manual press must not buy a turn past a bound the
      user armed.
    * **Mid-fire** -> 409. :meth:`_arm_timer` cancels the existing timer
      task, and during the fire window that task may be parked on
      ``_persist_locked()`` writing the delivered cycle; cancelling it there
      loses the ``cycle_count`` bump. This is the same window
      ``notify_turn_complete``/``notify_user_input`` defer around, and the
      same answer the sibling immediate-trigger route gives for a run
      already in flight (``POST /api/crons/{id}/run`` -> 409).

      ``defer_if_firing`` changes what happens AFTER that refusal, never the refusal
      itself: the loop is recorded so the end of the in-flight cycle re-arms at delay
      zero rather than toward the loop's deadline. Opt-in, because a caller with a
      person behind it reports the refusal and is pressed again, while the crew-log
      wake's caller is a drain thread with nobody to tell -- and a deadline re-arm on
      an hours-long patrol cadence would hold a worker's report for hours.
      The same flag marks the tick it does arm as a worker's push: that tick observes
      the subject instead of spending the post-wake follow-up, and a quiet answer keeps
      the loop's earlier deadline.

    NO SUSPENSION POINT, and that is the design rather than an omission.
    ``async def`` for the caller's convenience, but nothing inside awaits, so
    the guards and the arm are atomic with respect to the event loop: between
    reading ``loop`` and arming it, no other coroutine can run.

    This shape was arrived at the hard way and the history is worth keeping.
    An earlier revision wrote ``next_due_ts = time.time()`` and awaited a
    DURABLE persist before arming, so a restart between the press and the
    fire would resume overdue. That await was a suspension window, and this
    module has several writers to ``next_due_ts`` that hold NO lock while
    writing it — the quiet-tick re-arm on the gated-wake branch is one. Each
    guard added to close one writer's window exposed the next: a concurrent
    ``remove`` arming a stale object, a cancelled caller abandoning the write,
    a countdown entering ``_firing`` mid-persist, a quiet tick overwriting the
    deadline, then the refused path leaving its own value durably committed.
    Five rounds, each caused by the fix before it. The window is not closable
    at this call site, because the racing writers take no lock at all.

    SO THE WRITE IS GONE. ``_arm_timer(delay=0.0)`` is what brings the cycle
    forward: :meth:`_timer` sleeps the delay it is given and fires WITHOUT
    consulting ``next_due_ts``. The write was only ever for restart cosmetics,
    and that is exactly what is given up — a gateway restart between the press
    and the fire resumes on the loop's own schedule instead of overdue, and
    the operator presses again. That is the same degradation
    :meth:`_persist_soon` documents as acceptable for every other deadline
    assignment in this class ("a lost write degrades to a fresh full countdown
    after restart, never a premature or dropped fire"), and a far better trade
    than a sixth guard on an uncloseable window.

    The countdown reset callers ask for is UNAFFECTED, because it never
    came from this write: a delivered cycle clears ``next_due_ts`` in
    :meth:`_run_fire_cycle` and the re-arm then starts a fresh full interval.
    """
    loop = self.get_by_id(loop_id)
    if loop is None:
        return None, "loop not found", 404
    if not loop.active:
        return None, "loop is not active", 409
    if loop_id in self._firing:
        if defer_if_firing:
            # The refusal still STANDS as this call's answer -- nothing is armed now, and
            # the caller is told so -- but the intent is remembered, and
            # ``_run_fire_cycle``'s tail arms at delay zero instead of toward the
            # deadline. Opt-in rather than the default because the operator's Fire-now
            # button reports its own refusal to a person who can press again, while the
            # crew-log wake has no one to tell: its caller is a drain thread reacting to
            # a worker's write, and dropping the intent there would make the report wait
            # out the conductor's whole patrol cadence.
            self._pulled_forward.add(loop_id)
        return None, "loop is already firing", 409
    armed_in = _pushed_arm_delay(loop, delay, time.time())
    self._arm_timer(loop, delay=armed_in)
    if defer_if_firing:
        # A worker's push, not a person's press: the tick it armed is marked so the gate
        # observes rather than spending the post-wake follow-up, and so a quiet answer
        # keeps the loop's deadline. The mark is also what tells a later push inside this
        # window that the tick it needs is already armed. Logged at DEBUG because it
        # happens once per worker write, turn end and close; the operator's button keeps
        # its INFO line below.
        self._pushed_ticks.add(loop_id)
        logger.debug(
            "AutoNudge: loop %s pulled forward by a worker's write -- cycle %d armed to "
            "run in %.1fs",
            loop.id,
            loop.cycle_count + 1,
            armed_in,
        )
        return loop, "", 200
    # A person asked for this turn, so it must not meet the person-wait hold:
    # forgetting what the last turn saw makes the next tick fire.
    loop.ledger_seen_fp = ""
    logger.info(
        "AutoNudge: loop %s brought forward by hand — cycle %d armed to run now",
        loop.id,
        loop.cycle_count + 1,
    )
    return loop, "", 200
