"""The deferred reset of a dashboard session: consuming a pending reset before the next turn
allocates, and the retry armed while the reset cannot run yet."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _broadcast_expired_oauth_banners,
        _ChatSlot,
        _pending_reset_retries,
        effective_session_key,
        logger,
        subagents_attached_async,
        teardown_hold_active,
    )
    from kiro_crew.dashboard.chat_turn.eager_spawn import schedule_eager_spawn
    from kiro_crew.dashboard.chat_turn.queue_cycle import _arm_synthesis_recheck
    from kiro_crew.dashboard.chat_turn.queue_drain import _start_next_queued_turn

# Retry cadence for a deferred project-change reset or conversation discard
# whose consume DECLINED (busy channel turn, attached children, a pending
# question) or raised. A dashboard slot gets its retry for
# free at the next turn boundary, but a channel-linked slot's turns do not
# pass through those boundaries — without an owned retry, a decline against a
# busy channel session would leave the flag armed forever while the live
# session keeps serving the old CWD, and an idle slot holding its queue behind
# a discard would hold it for good. The retry consumes with ``allow_discard``,
# which is safe off a turn end for the atomic refusals ``_consume_pending_reset``
# names. The retry of the project RESET is deliberately COEXTENSIVE
# with that flag's lifetime: it exits only when the flag clears or the slot is
# replaced, never on a clock — a channel turn can outlast any fixed budget,
# and a timed-out retry would strand the old project permanently on a slot
# with no other consume boundary. One sleep per tick per armed slot is the
# whole cost; a stuck consume is surfaced by the periodic warning below.
_PENDING_RESET_RETRY_DELAY_SECS = 5.0


_PENDING_RESET_RETRY_WARN_EVERY = 120  # one warning per ~10 minutes armed


# The RETRY is unbounded, for the reason above. The QUEUE HOLD a pending
# discard puts on the slot is bounded here instead, and the two are not the
# same risk: an unlanded reset serves the old project, which is wrong but
# answers, while a held queue answers nothing and reports nothing. Only a
# REFUSAL counts toward the budget — a discard that raised, or that
# ``skip_if_busy`` turned down. A wait on attached children or a pending card
# is not a refusal: it ends on its own, and giving up there would throw away a
# reset that was waiting correctly.
_TEARDOWN_HOLD_MAX_TICKS = 60  # ~5 minutes of refusals before the hold is given up


_pending_reset_wakeups: dict[asyncio.Task, asyncio.Event] = {}


def _a_foreign_turn_owns_the_slot(slot: "_ChatSlot", *, own_turn: bool) -> bool:
    """Whether a turn OTHER than this caller holds or has reserved *slot*.

    ``slot.running`` cannot answer this, and neither can comparing ``slot.task``
    to the running task. ``slot.task`` is the OUTER task
    (``spawn_guarded_turn``); ``_bounded_turn`` runs the turn in a second task
    and ``run_to_completion`` runs ``_end_turn_tail`` in a third, so a turn-path
    caller is never the task the slot records. The caller therefore has to SAY
    whether it is the slot's own turn -- ``own_turn=True`` from
    ``_end_turn_tail`` and the in-turn ``/clear``, and the default from the
    background retry, which is a turn's peer rather than a turn.

    A reservation defers either way: ``chat_send`` sets it and clears it in a
    ``finally`` that runs before the turn task's body does, so a turn never
    observes its own.
    """
    if slot._turn_admission_reserved:
        return True
    if own_turn:
        return False
    task = slot.task
    return bool(task is not None and not task.done())


def _arm_pending_reset_retry(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Own the retry of a declined deferred reset (channel slots have no
    turn-boundary consume, so somebody must).

    Deduped by OWNER IDENTITY, not just key: a decline observed by the retry
    task itself re-enters here and must not stack a second task, while a
    REPLACEMENT slot under the same key must not be suppressed by a retiring
    owner's still-live task — that task exits on its own identity check, and
    its compare-and-pop cleanup cannot remove a successor's registration.
    """
    entry = _pending_reset_retries.get(slot.key)
    if entry is not None and entry[0] is slot and not entry[1].done():
        return

    def _teardown_owed() -> bool:
        return (
            slot._pending_reset_history_key is not None
            or slot._pending_discard_conversation_key is not None
        )

    async def _release_and_drain() -> None:
        """Hand the floor back to whatever the hold was keeping from running.

        Reached when the DISCARD key clears, not when every teardown is done: a
        project reset on a key with no session declines forever, so waiting for
        both would strand a queue whose own hold has ended. Landing a teardown
        here is outside any turn boundary, so no turn end is coming to do this.
        Re-asserted on the live slot because the consume above yields, so a
        replacement can take the key meanwhile.

        A sign-in failure holds the queue independently, and
        ``_start_next_queued_turn`` carries no check of its own -- every other
        drain site gates on ``_last_turn_auth_required`` at the CALLER
        (``queue_cycle``), so this one must too. Draining into a signed-out CLI
        fails every queued prompt in turn, and ``_requeue_auth_retry`` restores
        only runner-authored, synthesis and sub-agent inputs, so a person's
        prompt pulled in here would drop out of the post-login resume
        altogether. Only a turn that RUNS clears that flag, and the person's own
        send is never held by it, so their next send after signing in is what
        releases the rest.
        """
        if state.get_slot(slot.key) is not slot or slot.running:
            return
        if teardown_hold_active(state, slot) or slot._last_turn_auth_required:
            return
        if slot._queue and await _start_next_queued_turn(state, slot):
            return
        # A queue that was there is not a turn that ran: the drain's own
        # admission sweep can drop every entry, and it also declines while
        # memory preparation is pending or a superseded continuation is purged.
        # Each returns False having started nothing, so the recheck below has to
        # be reachable from there as well as from an empty queue.
        #
        # An empty queue is not nothing to do: the hold also withheld the
        # synthesis dispatch, and that turn's only other trigger is a fresh
        # send, so it would sit pending until the person wrote again.
        if slot._pending_synthesis and not slot._synthesis_inflight:
            _arm_synthesis_recheck(state, slot)

    async def _wait_is_structural() -> bool:
        """Whether the discard is deferred by something that ends on its own.

        Attached children end and a pending card is answered. Spending the
        budget on either throws away a reset that was waiting correctly, and
        the sub-agent wait is deliberate: landing the discard there kills the
        children. Re-derived per attempt rather than recorded by the consume, so
        there is no second copy of the reason to go stale. An admitted turn is
        not listed because the loop skips the attempt entirely while one holds
        admission, so no refusal is counted there either.
        """
        key = slot._pending_discard_conversation_key
        if key is None:
            return False
        if await subagents_attached_async(state, slot, key, "teardown_hold_budget"):
            return True
        if not getattr(slot, "_pending_discard_from_wake", False):
            return False
        # deferred: session_directive_apply loads autonudge and the monitor
        # stack at import.
        from kiro_crew.dashboard.session_directive_apply import _slot_has_pending_interaction

        return _slot_has_pending_interaction(state, slot)

    def _the_hold_is_withholding_something() -> bool:
        """Whether giving the hold up would actually release anything.

        The budget exists to stop the hold stranding work, so it has nothing to
        buy on a slot that holds none: an armed discard with an empty queue and
        no held synthesis is simply waiting for a boundary, and clearing it
        throws the person's reset away to free nothing. A linked channel session
        that stays busy refuses every attempt without any turn of its own
        appearing on this slot, so those refusals would otherwise reach the cap.
        """
        if slot._queue:
            return True
        return bool(slot._pending_synthesis and not slot._synthesis_inflight)

    def _give_up_the_hold(attempts: int) -> None:
        """Stop holding *slot*'s queue behind a discard that will not land.

        Clears the discard rather than keeping it armed-but-ignored: every
        reader of the hold derives it from this one key, so an armed key that
        does not hold needs a second piece of state saying so, and a stale copy
        of exactly that kind is what deriving the hold avoids. The cost of
        giving up is that replies can come from the conversation the person
        asked to drop, which is why the row below says so.
        """
        if slot._pending_discard_conversation_key is None:
            return
        released_entries = bool(slot._queue)
        slot._pending_discard_conversation_key = None
        slot._pending_discard_from_wake = False
        logger.warning(
            "Giving up the queue hold on slot %s: the queued conversation discard "
            "was refused or raised on %d attempts with nothing left to wait for",
            slot.key,
            attempts,
        )
        released = (
            "your queued messages have been released and may be answered from it"
            if released_entries
            else "the reply it was holding back may come from it"
        )
        slot.append(
            "system",
            f"⚠️ Could not drop the previous conversation, so {released}. Start a "
            "new chat if the replies look like they came from the wrong "
            "conversation.",
            "msg msg-info",
        )

    wake = asyncio.Event()

    async def _retry() -> None:
        ticks = 0
        refusals = 0
        counting_for: str | None = None
        try:
            while True:
                try:
                    await asyncio.wait_for(wake.wait(), _PENDING_RESET_RETRY_DELAY_SECS)
                except TimeoutError:
                    pass
                wake.clear()
                if state.get_slot(slot.key) is not slot:
                    return  # slot deleted or replaced; nothing owed BY US
                held = slot._pending_discard_conversation_key
                if not _teardown_owed():
                    await _release_and_drain()
                    return  # consumed by a turn boundary or eager consume
                if _a_foreign_turn_owns_the_slot(slot, own_turn=False):
                    # A turn holds admission but has not acquired the session
                    # yet, so `skip_if_busy` would see an unlocked semaphore and
                    # allow the teardown. The turn would then get a fresh
                    # conversation with replay suppressed, losing the context it
                    # was answering. Its own end is the right boundary. This
                    # task is a turn's peer, never a turn, so it declares so.
                    ticks += 1
                    continue
                await _consume_pending_reset(state, slot, allow_discard=True)
                if held is not None and slot._pending_discard_conversation_key is None:
                    # The discard landed. Its hold is over even if the project
                    # reset is still owed, so release now rather than waiting
                    # for a reset that may never land.
                    #
                    # Respawn for the same reason the turn-path consume does:
                    # the teardown dropped the session, and without one the next
                    # message pays the full cold start the eager path hides.
                    # Only this task's own landing qualifies -- a turn that
                    # landed it has already respawned for itself.
                    schedule_eager_spawn(state, slot)
                    await _release_and_drain()
                if not _teardown_owed():
                    return
                ticks += 1
                # The budget belongs to ONE discard. It restarts when the armed
                # key changes, so ticks are never inherited, and it counts only
                # while a discard is actually armed AND holding something back --
                # otherwise a reset-only retry would spin to the cap and reach
                # the give-up's release on a slot that holds nothing, and a
                # linked channel session that merely stays busy would have the
                # person's reset discarded to free an empty queue.
                armed = slot._pending_discard_conversation_key
                if armed != counting_for:
                    counting_for = armed
                    refusals = 0
                if (
                    armed is not None
                    and _the_hold_is_withholding_something()
                    and not await _wait_is_structural()
                ):
                    refusals += 1
                    if refusals >= _TEARDOWN_HOLD_MAX_TICKS:
                        _give_up_the_hold(refusals)
                        counting_for = None
                        refusals = 0
                        await _release_and_drain()
                        if not _teardown_owed():
                            return
                if ticks % _PENDING_RESET_RETRY_WARN_EVERY == 0:
                    logger.warning(
                        "Pending project-change reset for slot %s still armed "
                        "after %d retries; the owning session has stayed busy "
                        "(or kept children attached) the whole time",
                        slot.key,
                        ticks,
                    )
        finally:
            # Compare-and-pop: only remove OUR OWN registration — a
            # replacement slot may have overwritten it while this task was
            # winding down, and popping unconditionally would orphan the
            # successor's dedupe entry.
            own = asyncio.current_task()
            if own is not None:
                _pending_reset_wakeups.pop(own, None)
            current = _pending_reset_retries.get(slot.key)
            if current is not None and current[1] is own:
                _pending_reset_retries.pop(slot.key, None)

    task = asyncio.create_task(_retry())
    _pending_reset_wakeups[task] = wake
    _pending_reset_retries[slot.key] = (slot, task)


def _wake_pending_reset_retry(slot: "_ChatSlot") -> None:
    """Run *slot*'s armed retry now instead of at its next tick.

    An idle send that queues behind a discard is the case: the discard can
    often land at once, and the tick would make the person wait for nothing.
    The attempt is the retry's own, so it keeps every guard the tick has.
    """
    entry = _pending_reset_retries.get(slot.key)
    if entry is None or entry[0] is not slot:
        return
    wake = _pending_reset_wakeups.get(entry[1])
    if wake is not None:
        wake.set()


async def _consume_pending_reset(
    state: DashboardState, slot: _ChatSlot, *, allow_discard: bool = False, own_turn: bool = False
) -> bool:
    """Apply a deferred session reset queued on *slot*, if any.

    Returns whether a teardown actually ran, so a caller can decide whether a
    respawn is owed. A deferral left armed returns False.

    Two independent deferrals share this consumer, and both are queued rather
    than applied inline because their producer runs INSIDE the turn they would
    tear down: a project change (set_project) and a conversation discard
    (reset_conversation).

    They are not alternatives and neither subsumes the other, so when both are
    eligible both run, project change first. ``reset`` recreates the session but
    leaves replay suppression alone, so a discard that asked for
    ``replay=False`` still has to run to suppress the ``[CONVERSATION HISTORY]``
    rebuild — dropping it because a reset had already torn the session down
    would hand the next turn a reconstruction of the conversation the caller
    discarded.

    ``allow_discard`` IS THE BOUNDARY. Three callers set it: the end-of-turn one,
    a no-channel ``/clear`` (``_answer_slash_without_channel``), and the
    armed-reset retry below. The retry runs whether or not a turn is in flight,
    so what makes it safe is not a turn end but the two atomic
    refusals described below: ``skip_if_busy`` under the session lock, and the
    attached-children wait. A turn end is simply the boundary that comes for
    free on a dashboard slot.
    The project reset is consumed at three points including the one just before
    ``get_or_create``, and that pre-acquire point is safe for it only in the
    narrow sense its own comment claims: no lock is held by THIS turn, so
    ``reset`` cannot self-kill. It says nothing about another actor on the same
    session — a channel (Slack, Discord) turn runs on the linked session with no
    dashboard task at all, so a discard consumed there tears the provider down
    under a channel response that is still streaming and the reply is lost. The
    discard therefore waits for a moment when no turn is running on this
    session. A turn end is one such moment; the retry finds others, and checks
    each atomically rather than assuming it.

    Even there the boundary is checked atomically, not assumed: the discard goes
    through ``discard_conversation(..., skip_if_busy=True)``, which refuses under
    the same session lock that pops the session. Probing from here and tearing
    down afterwards would leave a window between the two — long enough for a
    channel message to acquire the session's semaphore and begin streaming a
    reply the teardown would then destroy. The semaphore is also a stricter
    signal than ``has_active_turn``, which cannot see a turn that holds the
    semaphore but has not yet put a prompt in flight.

    The discard additionally waits on sub-agent children.
    ``discard_conversation`` releases the shared runtime those children run on,
    and turn end is exactly when they are most likely to outlive their parent:
    ``slot.running`` is already False while they keep going, and the last child
    can still have a ``[Subagent completion event]`` injection in flight. So a
    slot with attached children leaves the flag ARMED and the discard lands at a
    later consume instead — the caller waits, the child's work survives. That is
    the same policy the immediate route enforces as a 409, applied through the
    same shared predicate rather than a second copy of the probes.

    Each flag is cleared only after its own effect succeeds, and compare-and-
    cleared so a key queued by a concurrent producer during the await is not
    clobbered.
    """
    torn_down = False
    if slot._pending_reset_history_key:
        pending_key = slot._pending_reset_history_key
        current_key = effective_session_key(slot)
        if pending_key != current_key:
            # The slot REBOUND after the flag was armed (a cron/workflow slot
            # gets linked when its first result is injected; a channel link can
            # land between arming and this consume). Resetting the stale key and
            # clearing the flag would tear down a session nobody is on and let
            # the slot's ACTUAL session keep the old CWD forever — the exact
            # stale-binding class this deferral exists to remove. Re-arm to the
            # current session so the reset lands where the turns run; the
            # producer already validated the project change belongs to this
            # slot, and re-pointing the key needs no re-authorization (it names
            # the slot's own live session, not a new authority).
            slot._pending_reset_history_key = current_key
            _arm_pending_reset_retry(state, slot)
            return torn_down
        if await subagents_attached_async(state, slot, pending_key, "consume_pending_reset"):
            # Left armed on purpose, same as the discard branch below: the
            # reset releases the shared runtime attached children run on, so
            # applying it now would discard their work. The retry task owns
            # the follow-up — children can outlive every turn boundary.
            logger.debug(
                "Deferring queued project-change reset for slot %s: sub-agents attached",
                slot.key,
            )
            _arm_pending_reset_retry(state, slot)
        else:
            try:
                # skip_if_busy, for the same reason the discard branch below
                # gives: the pending key can name a LIVE channel session (a
                # linked slot's project change arms `slack:<ts>`), and a force
                # reset here would tear the provider down under a streaming
                # channel reply. The check and the teardown must be one atomic
                # step under the session lock.
                #
                # The flag is spent ONLY on a real teardown (reset returned
                # True). A False return cannot distinguish "nothing registered
                # under the key" from a busy decline or a session that is
                # COLD-STARTING and not yet registered — clearing on a probe
                # that answered None would let a concurrent cold start carrying
                # the old CWD register afterwards and serve the stale project
                # with the flag already gone. Leaving it armed is always safe:
                # the next consume lands it, at worst costing one redundant
                # cold start after the reset tears down an already-correct
                # idle session.
                reset_ok = await state.sessions.reset(pending_key, skip_if_busy=True)
            except Exception:
                # A teardown that raised leaves the session in a state this
                # slot cannot vouch for — neither is its withhold verdict, so
                # drop it ("unknown" fails open), mirroring
                # `_reset_slot_session`'s BaseException path. This reset does
                # not go through that helper, so it owns the drop itself.
                slot.forget_session_model_state()
                logger.warning(
                    "Failed to consume pending project-change reset for slot %s",
                    slot.key,
                    exc_info=True,
                )
            else:
                if reset_ok:
                    # The verdict describes the session that advertised the
                    # model list, and that session is gone. Dropped only on a
                    # REAL teardown (or the raise above): a busy decline
                    # leaves the session — and therefore its verdict — live
                    # and accurate, and erasing it would let the dashboard
                    # show a withheld model as available.
                    slot.forget_session_model_state()
                    torn_down = True
                    if slot._pending_reset_history_key == pending_key:
                        slot._pending_reset_history_key = None
                    # Freshness push for open tabs; verdict-driven (see
                    # _broadcast_expired_oauth_banners).
                    _broadcast_expired_oauth_banners(state, slot)
                else:
                    logger.debug(
                        "Deferring queued project-change reset for slot %s: "
                        "no teardown landed (busy, cold-starting, or no session)",
                        slot.key,
                    )
                    # A channel-linked slot's turns never pass a dashboard
                    # turn boundary, so a decline here would otherwise never
                    # be retried and the live channel session would keep the
                    # old CWD indefinitely. The bounded retry task owns the
                    # follow-up (deduped; a decline observed by the task
                    # itself does not stack a second one).
                    _arm_pending_reset_retry(state, slot)
    if allow_discard and slot._pending_discard_conversation_key:
        discard_key = slot._pending_discard_conversation_key
        if await subagents_attached_async(state, slot, discard_key, "consume_pending_discard"):
            # Left armed on purpose: releasing the shared runtime now would kill
            # children that are still running, queued, or delivering a result.
            logger.debug(
                "Deferring queued conversation discard for slot %s: sub-agents attached",
                slot.key,
            )
            _arm_pending_reset_retry(state, slot)
            return torn_down
        from kiro_crew.dashboard.session_directive_apply import _slot_has_pending_interaction

        if getattr(slot, "_pending_discard_from_wake", False) and _slot_has_pending_interaction(
            state, slot
        ):
            # A patrol wake's reset never lands while a person is being asked
            # something: the answer would reach a chat with no memory of it.
            logger.debug(
                "Deferring queued conversation discard for slot %s: question or approval pending",
                slot.key,
            )
            _arm_pending_reset_retry(state, slot)
            return torn_down
        if _a_foreign_turn_owns_the_slot(slot, own_turn=own_turn):
            # Re-read AFTER the awaited probe above, and only for a turn that is
            # not this caller: a turn can reserve admission during that probe,
            # and `skip_if_busy` cannot see one that has reserved but not yet
            # acquired the session. Tearing down there hands that turn a fresh
            # conversation with replay suppressed, losing the context it was
            # about to answer. ``own_turn`` is how the slot's own turn says so,
            # since the task it would be compared against is two levels out.
            logger.debug(
                "Deferring queued conversation discard for slot %s: another turn admitted",
                slot.key,
            )
            _arm_pending_reset_retry(state, slot)
            return torn_down
        try:
            # ``skip_if_busy`` rather than a busy-probe here: the check and the
            # teardown have to be ONE atomic step under the session lock. A
            # caller-side probe leaves a window in which a channel turn acquires
            # the session's semaphore and starts streaming, and the teardown then
            # takes its provider away. False means it refused — leave the flag
            # armed and let a later boundary apply it.
            #
            # ``refuse_if`` puts the admission check under that same lock, and
            # the check above does NOT make it redundant: acquiring the registry
            # lock can wait arbitrarily long, because another key's teardown
            # holds it across ``record_session_ended``, and a turn can reserve
            # admission during that wait. ``skip_if_busy`` cannot catch it
            # either, since it reads the session's semaphore and a reservation
            # has not reached one. The check above still earns its place: it
            # refuses without taking the lock at all.
            #
            # ``replay=False`` is the only value this path ever wants: replaying
            # the transcript into the fresh conversation returns most of what the
            # reset reclaimed. The flag exists on the manager for the HTTP route,
            # which does let a caller choose.
            discarded = await state.sessions.discard_conversation(
                discard_key,
                replay=False,
                skip_if_busy=True,
                refuse_if=lambda: _a_foreign_turn_owns_the_slot(slot, own_turn=own_turn),
            )
            if not discarded:
                logger.debug(
                    "Deferring queued conversation discard for slot %s: a turn holds or"
                    " has been admitted to the session",
                    slot.key,
                )
                _arm_pending_reset_retry(state, slot)
                return torn_down
            torn_down = True
            # The discarded conversation is the one that advertised the model
            # list, so its verdict goes with it. AFTER the await here, unlike the
            # reset above: a refusal is a normal outcome on this path (turn in
            # flight -> flag stays armed, session untouched), so dropping before
            # would forget a verdict that is still accurate.
            slot.forget_session_model_state()
            if slot._pending_discard_conversation_key == discard_key:
                slot._pending_discard_conversation_key = None
                slot._pending_discard_from_wake = False
            # The discarded conversation's MCP report describes a session that no
            # longer exists; the fresh one will report for itself.
            if slot.clear_mcp_report():
                state.broadcast_ws("mcp_report_update", {"slot": slot.key, "mcp_report": None})
            # Freshness push for open tabs (verdict-driven — see
            # _broadcast_expired_oauth_banners).
            _broadcast_expired_oauth_banners(state, slot)
        except Exception:
            logger.warning(
                "Failed to consume pending conversation discard for slot %s",
                slot.key,
                exc_info=True,
            )
            _arm_pending_reset_retry(state, slot)
    return torn_down
