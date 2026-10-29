"""The end of a dashboard turn's queue cycle: the per-exit record and its ordered tail, the
hand-off to the next queued turn, the AutoNudge turn-end signal, and the pending synthesis turn
with its re-check, and the outcome a turn records for that tail when it ends
(``_turn_record_outcome``, with the session-switch lock's hand-back)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, NamedTuple

from kiro_crew.dashboard.chat_utils import SYNTHESIS_HELD, SYNTHESIS_UNKNOWN
from kiro_crew.dashboard.state import SUBAGENT_SYNTHESIS_PROMPT

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        SYNTHESIS_CLEAR,
        DashboardState,
        StartPriority,
        _ChatSlot,
        _note_mate_wakes,
        _run_chat,
        chat_done_payload,
        effective_session_key,
        generate_session_summary,
        logger,
        spawn_guarded_turn,
        synthesis_fire_verdict,
        title_then_refresh,
    )
    from kiro_crew.dashboard.chat_turn.eager_spawn import schedule_eager_spawn
    from kiro_crew.dashboard.chat_turn.queue_drain import (
        _arm_parked_queue_drain,
        _mark_turn_end,
        _start_next_queued_turn,
    )
    from kiro_crew.dashboard.chat_turn.regenerate_swap import settle_regenerate
    from kiro_crew.dashboard.chat_turn.session_reset import _consume_pending_reset
    from kiro_crew.dashboard.chat_turn.steer_queue import _requeue_unconsumed_steers
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn
    from kiro_crew.dashboard.chat_turn.turn_marker import _gateway_shutdown_requested


async def _run_pending_synthesis(state: DashboardState, slot: _ChatSlot) -> None:
    """Consume and run one armed synthesis turn.

    Kiro readiness is not waited on here. Readiness is latched at boot and only
    refreshed by an explicit user action, so waiting on a stale not-ready value
    would park this waiter indefinitely instead of letting the ACP attempt
    report the real auth state. The turn runs; a signed-out CLI surfaces as an
    ``AcpAuthRequired`` error card from ``_run_chat``.
    """

    try:
        if not slot._pending_synthesis:
            await _finish_queue_cycle(state, slot)
            return
        if slot._queue:
            state.push_slots_update()
            # The caller reached here only on a `clear` verdict
            # (`_launch_synthesis`), so a user message drained now ends the
            # batch: no second read.
            if await _start_next_queued_turn(state, slot, end_batch=True):
                return
        # The fire gate's verdict was taken by the caller (`_finish_queue_cycle`
        # or the outage re-check), the one place the decision reads the store.

        # All delivery guards hold. Consume immediately before the turn begins.
        slot._pending_synthesis = False
        slot._synthesis_rechecks = 0
        slot._synthesis_completion_turns = 0
        # Same successor boundary as the queue drain (see the finalize comment in
        # `_start_next_queued_turn`): this dispatch is reached from the previous
        # turn's tail without a `chat_done`, so the predecessor's streaming row
        # must be finalized before the synthesis turn's row and first chunk.
        # Every not-eligible path above already returned into
        # `_finish_queue_cycle`, whose `chat_done` stays the sole finalizer there.
        state.broadcast_ws("chat_segment", {"slot": slot.key})
        # Append the row BEFORE dispatching, matching `_start_next_queued_turn`.
        # This site bypasses that function (it runs no queue entry), and it was
        # the only turn-dispatching path that appended nothing — so the prompt
        # reached the conversation log with no dashboard row, and on replay it
        # resurfaced attributed to the USER. `inject` is the role every other
        # runner-authored continuation already uses, which is exactly what
        # SUBAGENT_SYNTHESIS_PREFIX's own docstring promises.
        slot.append(
            "inject",
            SUBAGENT_SYNTHESIS_PROMPT,
            "msg msg-inject",
            meta={"injectKind": "synthesis"},
        )
        state.push_slots_update()
        synthesis_task = spawn_guarded_turn(
            state,
            slot,
            # Declare the provenance structurally too. Without it this turn starts
            # a time-to-first-token clock whose own contract excludes synthetic
            # prompts, and `_is_synthetic` has to recover the same fact by
            # re-matching the marker string downstream. The actor is `subagent`
            # because the sub-agent completions that just landed are what makes
            # this turn run; the prompt itself is runner-authored, so `user` would
            # attribute machine orchestration to a person.
            _run_chat(
                state,
                slot,
                SUBAGENT_SYNTHESIS_PROMPT,
                _synthetic_payload=True,
                _turn_actor="subagent",
            ),
        )
        try:
            await synthesis_task
        except (asyncio.TimeoutError, TimeoutError):
            # The ceiling fired; spawn_guarded_turn's callback already rendered
            # the card naming the limit. Swallow here so the timeout does not
            # also propagate into this function's caller, which dispatches
            # fire-and-forget and would drop it unretrieved.
            pass
    finally:
        slot._synthesis_inflight = False


#: How often, and how many times, an idle slot re-asks the synthesis fire gate
#: after the task store could not be read (``SYNTHESIS_UNKNOWN``). Bounded: a
#: store that stays down past it leaves the synthesis armed for the next turn
#: end, as before.
_SYNTHESIS_RECHECK_SECS = 5.0
_SYNTHESIS_RECHECK_MAX = 12


def _launch_synthesis(state: DashboardState, slot: _ChatSlot) -> None:
    slot._synthesis_inflight = True
    task = asyncio.create_task(_run_pending_synthesis(state, slot))
    slot.task = task
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)
    state.push_slots_update()


def _drop_single_turn_synthesis(slot: _ChatSlot) -> bool:
    """Consume a cleared arm whose whole batch reached the slot in one turn.

    That turn (a lone sub-agent's completion, or a wave digest) already
    reported every result, so the synthesis prompt would only restate it.
    Exactly one counted turn drops it. Any other count fires, including 0:
    an arm nothing counted is not judged.
    """
    if slot._synthesis_completion_turns != 1:
        return False
    slot._pending_synthesis = False
    slot._synthesis_rechecks = 0
    slot._synthesis_completion_turns = 0
    return True


def _arm_synthesis_recheck(state: DashboardState, slot: _ChatSlot) -> None:
    """Re-ask the fire gate later: an unreadable store has no completion to wake it.

    One timer per slot (``slot._synthesis_recheck``), cancelled when the slot
    closes (``begin_close``), at most :data:`_SYNTHESIS_RECHECK_MAX` times per
    armed synthesis. Each attempt runs only while the slot is idle, registered
    and still armed; a real "children attached" answer stops the chain, since
    that child's own completion re-triggers the check.
    """
    if slot._synthesis_recheck is not None or slot._synthesis_rechecks >= _SYNTHESIS_RECHECK_MAX:
        return
    slot._synthesis_rechecks += 1

    async def _recheck() -> None:
        slot._synthesis_recheck = None

        def _still_wanted() -> bool:
            return bool(
                slot._pending_synthesis
                and not slot._synthesis_inflight
                and not slot.turn_running
                and not slot._closing
                and state._slots.get(slot.key) is slot
            )

        if not _still_wanted():
            return
        verdict = await synthesis_fire_verdict(state, slot)
        if verdict == SYNTHESIS_UNKNOWN:
            _arm_synthesis_recheck(state, slot)
        elif verdict == SYNTHESIS_CLEAR and _still_wanted():
            # Re-read after the store read: the tab may have started closing.
            _launch_synthesis(state, slot)

    def _fire() -> None:
        task = asyncio.ensure_future(_recheck())
        state._background_tasks.add(task)
        task.add_done_callback(state._background_tasks.discard)

    try:
        slot._synthesis_recheck = asyncio.get_running_loop().call_later(
            _SYNTHESIS_RECHECK_SECS, _fire
        )
    except RuntimeError:
        slot._synthesis_recheck = None


async def _finish_queue_cycle(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    allow_automatic_successor: bool = True,
    drain: bool = True,
) -> None:
    """Start synthesis when eligible, otherwise mark a queue cycle idle.

    A coroutine because the terminal ``chat_done`` frame it emits asks
    :func:`chat_utils.chat_done_payload` whether the floor really goes back to
    the user, and that question reaches the task store. The finishing turn
    still holds ``slot.task`` while the frame is read, so no turn can start
    inside the read: a send queues behind it instead, and is handed the floor
    here rather than stranded on an idle slot. With ``drain`` False only an
    entry that arrived during the read gets it, and only when the sub-agent
    hold would let it run on an idle slot; the held entries stay queued, and
    the frame does not count them as work that continues. The done row, the
    release and the frame then follow with no await between them.

    Titling, the session summary and the source-status refresh run only when a
    turn of this cycle reached a provider (``slot._cycle_reached_provider``):
    a cycle of local commands and cancelled replays has no reply to title and
    no remote work to re-read."""

    will_synthesize = False
    verdict = SYNTHESIS_HELD
    if (
        allow_automatic_successor
        and slot._pending_synthesis
        and not slot._synthesis_inflight
        # A slot gone from the registry is being torn down, so it has no next
        # user turn to owe a held note to -- withholding there would lose it.
        and state._slots.get(slot.key) is slot
        and state.subagents is not None
    ):
        # Running, delivering, pending in memory, or queued in the store: the
        # one fire-gate read (`synthesis_fire_verdict`).
        verdict = await synthesis_fire_verdict(state, slot)
        will_synthesize = verdict == SYNTHESIS_CLEAR and not _drop_single_turn_synthesis(slot)
        if verdict == SYNTHESIS_UNKNOWN:
            _arm_synthesis_recheck(state, slot)
        if not will_synthesize and drain and slot._queue and not slot._last_turn_auth_required:
            # A message the user sent while the fire gate read the store found
            # the turn still running and was queued behind it. Drain it as a
            # normal turn end does (`_start_next_queued_turn` keeps its own hold
            # rules); only this await could have let it in after the turn's own
            # drain attempt. A held queue stays held: its arrivals are handed the
            # floor by the read loop below.
            state.push_slots_update()
            if await _start_next_queued_turn(state, slot):
                return

    # The cycle's end is also the end of the turn that ran it, and it is marked
    # before the held-note flush below: that note is owed to the next turn.
    _mark_turn_end(slot)

    # Before any successor is dispatched. A held note's CONTEXT half drains into
    # the next turn, so flushing after that turn started would let the note shape
    # a turn its visible line appears below. Synthesis is withheld from, since a
    # note is owed to the next USER turn; the cycle after synthesis flushes it.
    if not will_synthesize:
        try:
            slot.flush_deferred_notes()
        except Exception:
            # Below this are the two ways a cycle ends: the synthesis dispatch and
            # the terminal append("done") / slot.task = None / chat_done. A raise
            # reaches _run_pending_synthesis, whose only handler is a narrow
            # (asyncio.TimeoutError, TimeoutError) around its await and a finally
            # that clears _synthesis_inflight -- neither emits done -- and it is
            # dispatched fire-and-forget, so the error is discarded and the slot
            # wedges with its spinner up. Log and let the cycle finish.
            logger.warning(
                "flush_deferred_notes failed at the queue-cycle end for slot %s",
                slot.key,
                exc_info=True,
            )

    if not slot._queue:
        slot._stopping = False
    if will_synthesize:
        _launch_synthesis(state, slot)
        return

    owner = slot.task
    stop_pending = slot._stop_state != "idle"
    stop_session_key = effective_session_key(slot)
    # A held queue keeps what it held when the hold was decided. A send that
    # queues during the read below is the user's next send, the one a hold
    # resumes on, so it gets the floor either way.
    held = {entry.get("id") for entry in slot._queue}
    while True:
        queued = [entry.get("id") for entry in slot._queue]
        payload = await chat_done_payload(state, slot, queue_held=not drain)
        if [entry.get("id") for entry in slot._queue] == queued:
            break
        # The queue moved during the read: a send that arrived queued behind the
        # still-running turn. Hand it the floor, then read again.
        if drain and slot._queue and await _start_next_queued_turn(state, slot):
            return
        arrived = next((e.get("id") for e in slot._queue if e.get("id") not in held), None)
        if (
            not drain
            and arrived
            and await _start_next_queued_turn(state, slot, required_queue_id=arrived)
        ):
            return
    if slot.task is not owner:
        # This cycle ran outside ``slot.task`` and a turn took the floor during
        # the read. That turn owns the slot now and ends its own cycle.
        return
    # A stop resolver cannot drain while this owner still holds the floor.
    # If it settled during the done read, this is the final handoff opportunity.
    if (
        drain
        and stop_pending
        and slot._stop_state == "idle"
        and state._slots.get(slot.key) is slot
        and effective_session_key(slot) == stop_session_key
        and not slot.is_closing
        and not slot._last_turn_auth_required
    ):
        from kiro_crew.dashboard import responsive_chat

        if (
            responsive_chat.first_priority_id(slot)
            and not responsive_chat.pending_interaction(slot, idle=True)
            and await _start_next_queued_turn(state, slot)
        ):
            return
    slot.append("done", "", "done")
    # Frontend contract: the dashboard treats the first `slots` frame reporting
    # `running: false` as the turn's end (runState settleEndedActiveTurn), so all
    # content is already out, `slot.task = None` precedes the push and the push
    # precedes `chat_done`. Pinned by test/test_finish_queue_cycle_terminal_order.py.
    slot.task = None
    state.push_slots_update()
    await _send_chat_done(state, slot, payload=payload)
    reached_provider = slot._cycle_reached_provider
    slot._cycle_reached_provider = False
    if not reached_provider:
        return
    # The turn that just finished is the most likely moment for this session's
    # PRs to have moved (opened, pushed, merged, reviewed), so re-read their
    # status now instead of leaving the sidebar chips on TTL rotation and the
    # detail panel on no refresh at all.
    state.refresh_slot_source_status(slot.key)
    state.push_refresh("history")
    # Chain the titling attempt into a refresh check, waiting out a
    # still-running on-send attempt first (see title_then_refresh). Both the
    # untitled and the already-titled case go through the same chain: an
    # already-locked title makes _maybe_auto_title a no-op, and the refresh
    # self-guards (origin/milestone/in-flight checks in maybe_refresh_title) —
    # the common case returns without any LLM work. Routing the titled branch
    # through the wait matters too: an on-send attempt that locked a LOW-SIGNAL
    # title (a URL/ticket-key echo — see chat_title._is_low_signal_title) can
    # still hold the in-flight guard here, and a direct maybe_refresh_title
    # would bounce off it — with no later chat_done for a one-message session
    # to catch the early milestone.
    title_task = asyncio.create_task(title_then_refresh(state, slot))
    state._background_tasks.add(title_task)
    title_task.add_done_callback(state._background_tasks.discard)

    # Intent summary for the chat summary panel. Self-guarding: the common case
    # (feature disabled) returns before any work, and an unchanged transcript is
    # served from the sidecar cache without a model call.
    summary_task = asyncio.create_task(generate_session_summary(state, slot))
    state._background_tasks.add(summary_task)
    summary_task.add_done_callback(state._background_tasks.discard)


async def _send_chat_done(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    continuing: bool = False,
    ends_turn: bool = True,
    payload: dict[str, Any] | None = None,
) -> None:
    """Send a turn-boundary ``chat_done`` frame; the runner sends no other way.

    ``payload`` is a frame the caller already read: ``_finish_queue_cycle``
    reads it while the finishing turn still holds the slot. Otherwise it is read
    here. Either way :func:`chat_utils.chat_done_payload` is awaited before the
    broadcast, so a coroutine never reaches the serializer."""
    if payload is None:
        payload = await chat_done_payload(state, slot, continuing=continuing, ends_turn=ends_turn)
    state.broadcast_ws("chat_done", payload)


async def _hand_off_queue(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    drain: bool,
    allow_automatic_successor: bool,
    predecessor_actor: str = "",
    predecessor_turn_id: str = "",
) -> None:
    """Requeue unconsumed steers, then start the next queued turn or end the cycle.

    The last step of :func:`_end_turn_tail`, so it runs once per turn. With
    ``drain`` False the queue is held (cards stay visible and individually
    cancellable) and the cycle ends with its terminal ``chat_done``. The
    ``predecessor_*`` pair names the ending turn, so the drain marks its end
    unless the head is that turn's own recovery."""
    # Steers outrank queued messages, so they go to the head before the drain.
    _requeue_unconsumed_steers(state, slot)
    if drain and slot._queue:
        # A user message drained at a turn's end ends an armed synthesis's
        # batch only when no child of it is still out (running, delivering,
        # pending in memory, or queued in the store): the same
        # `synthesis_fire_verdict` read `_finish_queue_cycle` takes. Read here,
        # not in the drain, because the ending turn still holds `slot.task`
        # through this await (released by `_finish_queue_cycle`), so a
        # completion queues behind it and nothing can take the floor during the
        # read; the drain itself then keeps origin/main's await-free dequeue.
        # Unarmed hand-offs read no store.
        end_batch = (
            bool(slot._pending_synthesis)
            and await synthesis_fire_verdict(state, slot) == SYNTHESIS_CLEAR
        )
        state.push_slots_update()
        if await _start_next_queued_turn(
            state,
            slot,
            predecessor_actor=predecessor_actor,
            predecessor_turn_id=predecessor_turn_id,
            end_batch=end_batch,
        ):
            return
    await _finish_queue_cycle(
        state,
        slot,
        allow_automatic_successor=allow_automatic_successor,
        drain=drain,
    )


def _notify_autonudge_turn_end(
    slot: _ChatSlot,
    *,
    tool_calls: int = 0,
    tool_identities: tuple = (),
    reply_text: str = "",
    reply_flushed: bool = False,
    nudge_turn: bool = False,
) -> None:
    """(Re)arm the slot's AutoNudge idle timer; runs on every turn exit.

    A turn that ended any other way than through this would orphan the loop:
    the fire path already cleared the next due time and relies on this to set
    it again. Passed as keywords so a build whose service predates them is
    unaffected."""
    try:
        from kiro_crew.autonudge import (
            get_instance as _autonudge_get,  # circular: autonudge -> dashboard.chat -> chat_runner
        )

        _autonudge = _autonudge_get()
        if _autonudge is not None:
            _autonudge.notify_turn_complete(
                slot.key,
                tool_calls=tool_calls,
                tool_identities=tool_identities,
                reply_text=reply_text,
                reply_flushed=reply_flushed,
                nudge_turn=nudge_turn,
            )
    except Exception:
        logger.debug("autonudge.notify_turn_complete failed", exc_info=True)


def _memory_preparation_settled(state: DashboardState) -> bool:
    """Whether the turn's memory barrier would let a turn through now.

    The verdict for an exit that never reached the barrier itself
    (``memory_startup.wait_for_memory_preparation``): the shared preparation
    task, if any, finished without being cancelled, and the preparation it ran
    recorded no failure and was not stopped. Otherwise the queue is held
    instead of walking each entry into the same refusal."""
    # Deferred like the turn's own barrier import of this module.
    from kiro_crew.memory_startup import memory_prepared

    task = getattr(state, "memory_startup_task", None)
    if isinstance(task, asyncio.Future) and not (task.done() and not task.cancelled()):
        return False
    return memory_prepared()


class _TurnOutcome(NamedTuple):
    """What a turn that reached its main ``try`` learned, for its tail."""

    auth_required: bool
    memory_admitted: bool
    # The turn's start priority, for the eager respawn its pending reset may arm.
    start_priority: StartPriority
    # The keyword facts :func:`_notify_autonudge_turn_end` labels a verdict from.
    nudge_facts: dict[str, Any]


class _TurnExit:
    """How one ``_run_chat`` call ends.

    Its exit guard creates it and passes it to the turn, which records here what
    the tail needs: its depth, whether a provider saw it, the local slash
    command it is running, and -- at the top of the ``finally``, before anything
    there can be cancelled -- its outcome. The guard runs :func:`_end_turn_tail`
    from it unless the turn already did."""

    __slots__ = (
        "actor",
        "depth",
        "handed_off",
        "local_command",
        "outcome",
        "reached_provider",
        "responsive_owner",
        "stop_reason",
        "turn_id",
    )

    def __init__(self) -> None:
        # The turn's crew-log actor and its recovery id, so the hand-off can tell
        # this turn's own recovery from a new turn (``_start_next_queued_turn``).
        self.actor = ""
        self.turn_id = ""
        self.depth = 0
        self.handed_off = False
        self.local_command = ""
        self.outcome: _TurnOutcome | None = None
        self.reached_provider = False
        self.responsive_owner: Any = None
        # The terminal event's stop reason; None while the turn reached none.
        self.stop_reason: str | None = None


async def _end_turn_tail(
    state: DashboardState,
    slot: _ChatSlot,
    turn_exit: _TurnExit,
    *,
    setup_failed: bool = False,
) -> None:
    """The per-exit duties of a ``_run_chat`` turn, in order, then its hand-off.

    Runs at most once per turn: at the end of the turn's ``finally``, or from
    the exit guard for every exit the ``finally`` did not finish -- a return
    above the main ``try`` (a replay cancelled at its consume seam, a blocked or
    local slash command), an exception there, or a ``finally`` cut short by a
    second cancel. Its inputs are what the turn recorded, never where it
    stopped:

    - Without ``turn_exit.outcome`` the turn never reached its ``try`` and
      acquired no session. It consumes no deferred reset and retires no wait
      state, so a refused ``/compact`` behaves as if the turn never started;
      the slot keeps the
      sign-in outcome the last turn recorded, so a queue held after a sign-in
      failure stays held; and the memory barrier's verdict comes from the
      shared preparation task.
    - ``setup_failed`` (an exception above the ``try`` outside a local slash
      command's own handler) holds the queue: the same setup would fail every
      queued entry in turn.
    """
    turn_exit.handed_off = True
    outcome = turn_exit.outcome
    # A Regenerate replaces the previous reply only now that the turn is over
    # and before a queued successor can start (``regenerate_swap``). Whatever
    # the depth: the frame that runs this tail is the turn's end, since a
    # ``/prompts get`` re-entry (depth 1) runs it for an outer frame that handed
    # its tail off, and the hand-off below is not depth-gated either. The
    # target's generation check keeps any other turn out.
    try:
        await settle_regenerate(state, slot)
    except Exception:
        logger.warning("Regenerate settle failed", exc_info=True)
    if outcome is not None:
        # End-of-turn fallback: catches set_project and reset_conversation calls
        # that fired mid-turn, after the start-of-turn consume already ran. This
        # is the only TURN-PATH caller that may consume a queued conversation
        # discard -- the earlier consume points run just before a turn acquires
        # the session, where a teardown can land under a channel turn already
        # streaming on it. A no-channel /clear also consumes one, relying on
        # skip_if_busy for that case. Guarded because a raise here would skip the queue
        # hand-off below, silently stranding queued work at the end of an
        # otherwise successful turn.
        try:
            # A model pick the picker took during THIS turn lands first: the
            # session is idle now, so the harness's live switch can take it
            # (no teardown, no replay); where it cannot, the helper arms the
            # same deferred reset the consume below spends. Before the
            # consume for exactly that reason.
            # circular import: session_control imports this package's modules at module level.
            from kiro_crew.dashboard.session_control import apply_deferred_model_switch

            await apply_deferred_model_switch(state, slot)
            # The consume tore down the session for a mid-turn project change or
            # conversation discard; without a respawn the NEXT message pays the
            # full cold start the eager path exists to hide. Keyed on what
            # actually tore down, not on what was queued -- a discard left armed
            # behind attached sub-agents changed nothing to respawn for.
            if await _consume_pending_reset(state, slot, allow_discard=True):
                schedule_eager_spawn(state, slot, start_priority=outcome.start_priority)
        except Exception:
            logger.debug("_consume_pending_reset failed", exc_info=True)
    # Drop the audience admissions with the turn they belonged to -- the peer
    # steers admitted into it and the transcript reads it made. They govern
    # whether THIS turn may publish across surfaces, which is their whole job;
    # carrying them further would judge a later turn by an authorization that
    # was never about it. Cleared on every exit, so a hard stop, a crash or a
    # gateway abort cannot leave a record behind to silence the next turn.
    slot._steer_audience_fences.clear()
    slot._steer_audience_fence_holders.clear()
    if turn_exit.depth == 0:
        # A channel steer's narrowing of this turn's directive provenance ends
        # with the turn; the next turn's provenance is its own opener's.
        # Outermost frame only: the depth-1 re-entry's exit is not the turn's end.
        slot._turn_channel_narrowed = False
        slot._turn_channel_origin = False
        slot._turn_user_sent = False
    # A healthy `wait` clears its own state with a final keepalive ping, but
    # that ping is best-effort and cannot run at all if the MCP subprocess died
    # mid-sleep (hard stop, crash, gateway abort). Clearing at turn end is the
    # backstop that keeps a dead wait from leaving a countdown ticking toward a
    # deadline nothing is waiting on, and drops any end request the tool never
    # collected so it cannot reach the next sleep. It also releases the
    # contested latch: that latch is deliberately turn-scoped, so this is the
    # ONLY thing that clears it. Only a turn that reached its ``try`` can have
    # run a wait: an exit above it leaves alone the wait state a sub-agent's
    # sleep keeps on this slot.
    if outcome is not None:
        slot._wait_state = None
        slot._end_wait_request = None
        slot._wait_contested = False
        # Record this turn's auth outcome so the orchestrator _stage_loop, which
        # runs stages as separate _run_chat calls, can mirror this same "hold
        # the queue for post-login resume" guard on its end-of-plan handoff.
        slot._last_turn_auth_required = outcome.auth_required
    # (Re)arm the AutoNudge idle timer on EVERY exit: a turn that ends via
    # timeout, AcpProcessDied, AcpError or a cancel would otherwise orphan it.
    _notify_autonudge_turn_end(slot, **(outcome.nudge_facts if outcome is not None else {}))
    if turn_exit.depth == 0:
        # A crewmate's worker reports back to the crewmate's thread on its own.
        _note_mate_wakes(state, slot, outcome.nudge_facts["reply_text"] if outcome else None)
        # A crewmate session owed a reload onto an updated spec while it was busy.
        from kiro_crew.dashboard.crewmate_worker_reload import note_turn_end

        note_turn_end(state, slot)
    if turn_exit.reached_provider:
        slot._cycle_reached_provider = True
    memory_admitted = (
        outcome.memory_admitted if outcome is not None else _memory_preparation_settled(state)
    )
    # After startup admission, the successor's own ACP attempt remains the
    # authority for a later sign-out. A turn cancelled while waiting on shared
    # preparation retains the queue instead of walking every item through the
    # same unfinished or cancelled gateway task.
    #
    # A recorded sign-in failure is the one other hold: the CLI is signed out,
    # so every queued prompt would fail identically. The queue is left intact
    # and resumes on the user's next send after they log in -- the no-loss rule,
    # without a readiness waiter to strand it.
    if slot._queue and not memory_admitted:
        # Refused or stopped at the admission seam: the queue behind this turn
        # is retained, so make sure preparation's completion starts it rather
        # than waiting for the user's next send.
        _arm_parked_queue_drain(state, slot, notice=False)
    if slot._responsive is not None and slot._responsive.owner is turn_exit.responsive_owner:
        slot._responsive.owner = None
        slot._responsive.cancel_grace()
    await _hand_off_queue(
        state,
        slot,
        drain=memory_admitted and not slot._last_turn_auth_required and not setup_failed,
        allow_automatic_successor=memory_admitted and not setup_failed,
        predecessor_actor=turn_exit.actor,
        predecessor_turn_id=turn_exit.turn_id,
    )


def _hand_off_unstarted_turn(
    state: DashboardState,
    slot: _ChatSlot,
    task: "asyncio.Task",  # type: ignore[type-arg]
) -> None:
    """End the queue cycle of a dispatch that held *slot* but never started its turn.

    Every ``_run_chat`` exit hands the queue off (:func:`_end_turn_tail`), but a
    dispatch can end without entering ``_run_chat``: a dashboard monitor wake
    refused before its runner (``nudge_dashboard_fire._run_dashboard_turn``), or
    an unattended turn whose wait for a background-turn permit gave up or was
    cancelled (``DashboardState.run_background_turn``). It held ``slot.task``
    while it waited, so a send queued behind it; without this, that entry waits
    for the next send (on an app-owned slot with no other sender, indefinitely)
    and the cycle never sends ``chat_done``.

    Called from the dispatch's done callback (``turn_dispatch.spawn_guarded_turn``),
    so *task* has ended. Owed only while *task* still holds ``slot.task``, the
    exit marked its turn unstarted (``turn_dispatch.mark_turn_unstarted``), no
    send has reserved the floor, and the slot is still the registered, open,
    local one. Never while the gateway is going away: the queue stays for the
    history save, as the parked drain leaves it. The cycle's end runs on a task
    that takes ``slot.task`` before this returns, as :func:`_launch_synthesis`
    does, so a send arriving meanwhile queues behind it and
    :func:`_finish_queue_cycle` hands it the floor.
    """
    if (
        slot.task is not task
        or slot._unstarted_generation != slot._turn_generation
        # *task* has ended, so this reads a send's admission reservation.
        or slot.running
        or state._slots.get(slot.key) is not slot
        or slot.is_closing
        or slot.executor == "remote"
        or _gateway_shutdown_requested(state)
    ):
        return
    cycle_end = asyncio.create_task(_end_unstarted_cycle(state, slot))
    slot.task = cycle_end
    state._background_tasks.add(cycle_end)
    cycle_end.add_done_callback(state._background_tasks.discard)


async def _end_unstarted_cycle(state: DashboardState, slot: _ChatSlot) -> None:
    """The queue hand-off :func:`_end_turn_tail` gives a turn with no outcome, alone.

    The rest of that tail does not apply: no session was acquired, so there is
    no reset to consume, wait to retire or sign-in outcome to record, and the
    AutoNudge re-arm stays with the fire that dispatched the turn, which settles
    its own result. The holds are the tail's: memory preparation that is still
    running or did not succeed holds the queue and parks it for preparation's
    end, and a recorded sign-in failure holds it until the next send.
    """
    try:
        memory_admitted = _memory_preparation_settled(state)
        if slot._queue and not memory_admitted:
            _arm_parked_queue_drain(state, slot, notice=False)
        await _hand_off_queue(
            state,
            slot,
            drain=memory_admitted and not slot._last_turn_auth_required,
            allow_automatic_successor=memory_admitted,
        )
    except Exception:
        logger.warning(
            "queue hand-off after an unstarted turn failed for slot %s",
            slot.key,
            exc_info=True,
        )


#: Meta key on the error row of a turn that raised before its main ``try``. The
#: cycle still ends with a ``done`` row, so an HTTP reader that waits for
#: ``done`` reads this key to report the failure instead of an empty reply.
TURN_FAILED_META = "turn_failed"


def _note_failed_before_the_try(slot: _ChatSlot, turn_exit: _TurnExit) -> None:
    """Show the user an exit that raised before the turn reached its main ``try``.

    Nothing else answers it: the dispatcher only logs the exception."""
    failed = (
        f"⚠️ `{turn_exit.local_command}` failed."
        if turn_exit.local_command
        else "⚠️ This message failed before it started."
    )
    slot.append(
        "error",
        f"{failed} The gateway log has the details.",
        "msg msg-err",
        meta={TURN_FAILED_META: True},
    )


async def _turn_record_outcome(turn: _ChatTurn) -> None:
    """Hand back the session-switch lock and record the turn's outcome for its tail."""
    _directive_self_wake = turn._directive_self_wake
    _turn_flushed_visible_text = turn._turn_flushed_visible_text
    _turn_priority = turn._turn_priority
    _turn_tool_calls = turn._turn_tool_calls
    _turn_tool_identities = turn._turn_tool_identities
    turn_exit = turn.turn_exit
    # First: hand back the session-switch lock if this turn exited between
    # its acquire and its post-registration release. Before anything that
    # can await, and before _restore_refusal_fallback (chat_turn/model_fallback.py) takes the
    # same lock -- otherwise a turn that failed to register would block
    # every switch on its session until the gateway restarts.
    if turn._dispatch_lock_held:
        turn._dispatch_lock_held = False
        turn._dispatch_lock.release()
    # Second: record this turn's outcome for its tail. Everything the tail
    # needs is known by now, and any later step of _run_chat's finally can raise or
    # be cut short by a second cancel; recorded first, the exit guard then runs the
    # tail with THIS turn's policy rather than an early exit's.
    # The AutoNudge facts are the ones only this frame holds: what the turn
    # dispatched and what it answered. The wake judge's feedback loop labels
    # its own verdict from them -- a woken turn that changed nothing and
    # answered short is the quiet-cycle shape the judge should have
    # suppressed. The IDENTITIES go with the count because a count alone
    # cannot tell a read from a write, which made that label constant-true;
    # this is the same tuple the read-only-preparation check already reads,
    # so the names cost nothing extra to collect. None is retained: the
    # service reduces them to one boolean and two numbers. A nudge turn
    # labels its verdict only when its response lands; failed and cancelled
    # turns keep the retry unlabelled.
    turn_exit.outcome = _TurnOutcome(
        auth_required=turn._auth_required,
        memory_admitted=turn._memory_preparation_admitted,
        start_priority=_turn_priority,
        nudge_facts={
            "tool_calls": _turn_tool_calls,
            "tool_identities": tuple(_turn_tool_identities),
            "reply_text": turn.assistant_text if isinstance(turn.assistant_text, str) else "",
            "reply_flushed": _turn_flushed_visible_text,
            "nudge_turn": _directive_self_wake and turn._turn_landed,
        },
    )
