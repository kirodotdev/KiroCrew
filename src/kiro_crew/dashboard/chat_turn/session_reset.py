"""The deferred reset of a dashboard session: consuming a pending reset before the next turn
allocates, and the retry armed while the reset cannot run yet; and the hand-back of the
session when a turn ends, on any exit path (``_turn_release_session``): the deferred OPTIONS
expiry, the mirror stream's close, the reset or conversation discard the turn asked for, the
session permit's release, the turn identity's clear and the unclaimed-directive report."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import _broadcast_expired_oauth_banners

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        _pending_reset_retries,
        effective_session_key,
        expire_slack_options,
        logger,
        subagents_attached_async,
    )
    from kiro_crew.dashboard.chat_turn.directives import _report_unclaimed_directives
    from kiro_crew.dashboard.chat_turn.recipient import (
        cross_surface_withheld,
        slack_publication_withheld,
    )
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn

# Retry cadence for a deferred project-change reset whose consume DECLINED
# (busy channel turn, attached children). A dashboard slot gets its retry for
# free at the next turn boundary, but a channel-linked slot's turns do not
# pass through those boundaries — without an owned retry, a decline against a
# busy channel session would leave the flag armed forever while the live
# session keeps serving the old CWD. The retry is deliberately COEXTENSIVE
# with the flag's lifetime: it exits only when the flag clears or the slot is
# replaced, never on a clock — a channel turn can outlast any fixed budget,
# and a timed-out retry would strand the old project permanently on a slot
# with no other consume boundary. One sleep per tick per armed slot is the
# whole cost; a stuck consume is surfaced by the periodic warning below.
_PENDING_RESET_RETRY_DELAY_SECS = 5.0


_PENDING_RESET_RETRY_WARN_EVERY = 120  # one warning per ~10 minutes armed


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

    async def _retry() -> None:
        ticks = 0
        try:
            while True:
                await asyncio.sleep(_PENDING_RESET_RETRY_DELAY_SECS)
                if state.get_slot(slot.key) is not slot:
                    return  # slot deleted or replaced; nothing owed BY US
                if not slot._pending_reset_history_key:
                    return  # consumed by a turn boundary or eager consume
                await _consume_pending_reset(state, slot)
                if not slot._pending_reset_history_key:
                    return
                ticks += 1
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
            current = _pending_reset_retries.get(slot.key)
            if current is not None and current[1] is asyncio.current_task():
                _pending_reset_retries.pop(slot.key, None)

    _pending_reset_retries[slot.key] = (slot, asyncio.create_task(_retry()))


async def _consume_pending_reset(
    state: DashboardState, slot: _ChatSlot, *, allow_discard: bool = False
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

    ``allow_discard`` IS THE BOUNDARY. Two callers set it: the end-of-turn one,
    and a no-channel ``/clear`` (``_answer_slash_without_channel``), which runs
    before any session work and relies on ``skip_if_busy`` to refuse under a
    streaming channel turn.
    The project reset is consumed at three points including the one just before
    ``get_or_create``, and that pre-acquire point is safe for it only in the
    narrow sense its own comment claims: no lock is held by THIS turn, so
    ``reset`` cannot self-kill. It says nothing about another actor on the same
    session — a channel (Slack, Discord) turn runs on the linked session with no
    dashboard task at all, so a discard consumed there tears the provider down
    under a channel response that is still streaming and the reply is lost. The
    discard therefore waits for the end of a turn, where the session is between
    turns rather than about to start one.

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
            return torn_down
        try:
            # ``skip_if_busy`` rather than a busy-probe here: the check and the
            # teardown have to be ONE atomic step under the session lock. A
            # caller-side probe leaves a window in which a channel turn acquires
            # the session's semaphore and starts streaming, and the teardown then
            # takes its provider away. False means it refused — leave the flag
            # armed and let a later boundary apply it.
            #
            # ``replay=False`` is the only value this path ever wants: replaying
            # the transcript into the fresh conversation returns most of what the
            # reset reclaimed. The flag exists on the manager for the HTTP route,
            # which does let a caller choose.
            discarded = await state.sessions.discard_conversation(
                discard_key, replay=False, skip_if_busy=True
            )
            if not discarded:
                logger.debug(
                    "Deferring queued conversation discard for slot %s: turn in flight",
                    slot.key,
                )
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
    return torn_down


async def _turn_release_session(turn: _ChatTurn) -> None:
    """Hand the session back: OPTIONS expiry, mirror close, reset or discard, permit."""
    _acquired = turn._acquired
    _deferred_options_expiry = turn._deferred_options_expiry
    _mirror_active_task = turn._mirror_active_task
    _mirror_active_task_title = turn._mirror_active_task_title
    _mirror_chan = turn._mirror_chan
    _mirror_stream_ts = turn._mirror_stream_ts
    _mirror_thread = turn._mirror_thread
    _seen_tool_identity = turn._seen_tool_identity
    _turn_started = turn._turn_started
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # Clean up mirror stream on any exit path.
    #
    # The release below MUST happen however this block exits. Each await in
    # here is already guarded against ``Exception``, but ``CancelledError``
    # derives from ``BaseException`` (since 3.8), so a cancellation
    # delivered while one of them is suspended slips past every
    # ``except Exception`` and skips the release entirely.
    #
    # The permit is keyed by SESSION, so leaking it does not merely lose
    # this turn: the session reads as permanently busy, every later turn for
    # it blocks forever, no queued turn drains, and only a gateway restart
    # clears it. The nested try/finally makes the release unconditional
    # while preserving the reset-then-release ordering.
    try:
        if _deferred_options_expiry:
            _deferred_options_expiry = False
            turn._deferred_options_expiry = _deferred_options_expiry
            await expire_slack_options(state, session_key)
        if _mirror_stream_ts and state.slack_client and _mirror_chan:
            try:
                # Fenced for the same reason _on_tool_call's in-progress append is:
                # if that one was withheld, marking it complete here would publish the
                # title for the first time. This runs BEFORE the fence is
                # cleared in _end_turn_tail (chat_turn/queue_cycle.py), so it still
                # sees the turn's own records.
                if (
                    _mirror_active_task
                    and not cross_surface_withheld(state, slot)
                    and not slack_publication_withheld(state, slot, _mirror_chan, _mirror_thread)
                ):
                    await state.slack_client.append_task(
                        _mirror_chan,
                        _mirror_stream_ts,
                        _mirror_active_task,
                        _mirror_active_task_title,
                        "complete",
                    )
            except Exception:
                logger.debug("Task append cleanup failed", exc_info=True)
            try:
                await state.slack_client.stop_stream(_mirror_chan, _mirror_stream_ts)
            except Exception:
                logger.debug("Stream cleanup failed", exc_info=True)
        if _acquired and (turn.needs_session_reset or turn.needs_conversation_discard):
            # Neither branch below goes through `_reset_slot_session`, so the
            # withhold verdict is dropped here: both replace the session that
            # advertised the model list (an agent switch can even change the
            # provider), and the verdict describes that session. Dropped for
            # both branches and before the await, so a failed teardown leaves
            # the slot at "unknown" rather than carrying a verdict it can no
            # longer vouch for.
            slot.forget_session_model_state()
            try:
                if turn.needs_conversation_discard:
                    # Poisoned-conversation escalation: clear ONLY the
                    # resume sid (keeping the session-map entry with its
                    # Slack thread/channel linkage), so the re-queued
                    # recovery turn cold-starts a fresh native
                    # conversation instead of session/load-ing the same
                    # rejected one (which reset() would do).
                    await state.sessions.discard_conversation(session_key)
                else:
                    await state.sessions.reset(session_key)
            except Exception:
                logger.warning("Failed to reset session %s after agent switch", session_key)
            # Freshness push for open tabs. Unconditional where the old
            # write-time retirement needed an outcome gate: the helper
            # re-resolves the live child at call time, so a swallowed
            # teardown failure leaves the child alive, the stamps still
            # match, and nothing is broadcast (see
            # _broadcast_expired_oauth_banners).
            _broadcast_expired_oauth_banners(state, slot)
    finally:
        if _acquired:
            # A successful reset() above already popped the key under its
            # own lock, so this is a no-op on that path (the popped
            # session's semaphore is discarded with it). Kept unconditional
            # so a reset that failed or was cancelled still hands back the
            # permit rather than stranding the session.
            state.sessions.release(session_key)
        # This turn's identity dies WITH its session, and inside the same
        # finally for the same reason the release is: the reset above can be
        # cancelled, and CancelledError derives from BaseException, so a
        # clear placed after this block is simply skipped and the slot keeps
        # advertising a turn that is gone.
        #
        # It must also land before `_start_next_queued_turn` in `_end_turn_tail`'s hand-off
        # can install the SUCCESSOR's key — a clear after that would wipe a
        # live turn's identity and drop the cancel routes back to mutable
        # routing. Compare-and-clear keeps that true if the ordering is ever
        # rearranged: only the turn that installed a key may retire it.
        #
        # Outside the `_acquired` guard: the identity is published before
        # the permit is taken, so a cold start that never acquired one still
        # has something to retire.
        if slot._active_turn_session_key == session_key:
            slot._active_turn_session_key = ""
        # Spelling-independent backstop for a directive call whose tool
        # identity and result marker were both lost by the backend. The
        # validated payload reached the gateway, but no frame claimed it,
        # so every per-frame diagnostic in _on_tool_result was structurally unreachable.
        # Inspect only records parked during THIS turn: residue from an
        # earlier abandoned turn must not blame the current one. Diagnostic
        # only — claim, refusal, and expiry semantics stay unchanged.
        await _report_unclaimed_directives(
            state,
            slot,
            session_key,
            _turn_started=_turn_started,
            _seen_tool_identity=_seen_tool_identity,
        )
