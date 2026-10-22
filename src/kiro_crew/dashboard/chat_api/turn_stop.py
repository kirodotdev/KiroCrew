"""Stop and interrupt: ``stop_slot_turn`` with ``POST /api/chat/slots/{slot}/stop``, the
interrupt route, the stop-event card, and the approval rejection a stop runs first.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    import time

    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        _app_cancel_denied,
        _ChatSlot,
        _history_key_for,
        _mark_permission_resolved,
        _orphan_in_current_turn,
        _slot_replaced_while_queued,
        _start_next_queued_turn,
        _unblock_pending_waits,
        compaction_in_flight,
        deny_app_slot_access,
        effective_session_key,
        logger,
        read_bounded_json,
        relay_archive_refusal,
        sel,
        slot_not_found,
        stop_declined_armed,
    )


def _reject_pending_approvals(slot: _ChatSlot) -> None:
    """Reject all pending approval futures so the chat runner unblocks.

    When a stop/interrupt is triggered while the agent is waiting for tool
    approval, the chat runner is suspended on the approval future. Without
    resolving it, the stream generator stays paused, _turn_done never fires,
    and the cooperative cancel times out — forcing a hard kill.

    Resolving the future is not enough on its own: the ``permission`` message
    the UI renders the approval bar from must ALSO be marked resolved.
    Otherwise the future is gone while the message still reads pending, so the
    bar survives a history reload and every button on it answers
    ``404 no pending approval`` — an approval card the user cannot action.
    """
    for aid, fut in list(slot._approval_futures.items()):
        if not fut.done():
            # Mark BEFORE resolving. Resolving can wake the runner immediately,
            # and the runner reads this set where it records who decided; marking
            # after would race its own reader. The provenance is already known
            # here -- the SEL line below calls it ``rejected_on_stop`` -- and the
            # resolved value stays a plain "rejected" so no caller of this future
            # has to learn a new one.
            slot._approval_stopped.add(aid)
            fut.set_result("rejected")
            if _mark_permission_resolved(slot.messages, aid, "rejected"):
                slot._dirty = True
            sel().log_tool_invocation(
                session_key=effective_session_key(slot),
                agent=getattr(slot, "agent", "") or "kirocrew",
                source="dashboard",
                tool_name=f"approval_reject:{aid}",
                tool_kind="permission",
                outcome="rejected_on_stop",
            )


def _resolve_stop_event(slot: _ChatSlot, outcome: str) -> None:
    """Update the in-flight stop_event message in place with final state."""
    stop_id = slot._stop_event_id
    logger.debug("_resolve_stop_event: outcome=%s stop_id=%r", outcome, stop_id)
    if not stop_id:
        return
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    if outcome == "soft":
        final_state = "stopped"
    elif outcome == "compacting":
        # Nothing was stopped: the session's own /compact turn held it and a
        # cooperative Stop was declined. The card becomes the notice,
        # so the row the press opened tells the user what happened to it.
        final_state = "stop_declined_compacting"
    else:
        final_state = "stop_failed_reset"
    found = False
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        cls_data["state"] = final_state
        cls_data["outcome"] = outcome
        cls_data["ts_end"] = now_ts
        serialized = json.dumps(cls_data)
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        found = True
        # Re-broadcast updated stop_event so frontend StopEventCard
        # transitions from "stopping" → "stopped"/"stop_failed_reset".
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-broadcast failed", exc_info=True)
        break
    if not found:
        logger.debug("_resolve_stop_event: no matching message for stop_id=%s", stop_id)
    slot._stop_event_id = None


def _rearm_stop_event(slot: _ChatSlot, stop_data: dict[str, Any]) -> bool:
    """Reset an orphaned stop card back to "stopping" in place, same id.

    A new press that finds an orphan must not sweep it and append a fresh row:
    the pane upserts stop cards by ``meta.id``, so the settled old row plus the
    new row render as TWO "[Stopped]" chips for one press. Re-arming
    the existing row keeps the id — and therefore the chip — stable, the same
    reuse the escalation path performs via ``slot._stop_escalated_card_id``.

    Returns False when no row carries the id (e.g. the window was trimmed), in
    which case the caller appends the press's one card instead.
    """
    stop_id = stop_data["id"]
    serialized = json.dumps(stop_data)
    for msg in reversed(slot.messages):
        cls_val = msg.get("cls", "")
        if not cls_val:
            continue
        try:
            cls_data = json.loads(cls_val) if isinstance(cls_val, str) else None
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(cls_data, dict) or cls_data.get("kind") != "stop_event":
            continue
        if cls_data.get("id") != stop_id:
            continue
        msg["cls"] = serialized
        msg["content"] = serialized
        slot.invalidate_source_links()
        slot._dirty = True
        # Re-broadcast so connected panes transition the existing chip back to
        # "stopping" — the same channel _resolve_stop_event settles it on.
        on_msg = getattr(slot, "_on_message", None)
        if on_msg:
            try:
                on_msg(slot.key, msg)
            except Exception:
                logger.debug("stop_event re-arm re-broadcast failed", exc_info=True)
        return True
    logger.debug("_rearm_stop_event: no matching message for stop_id=%s", stop_id)
    return False


def _open_stop_event_card(slot: _ChatSlot, state_label: str) -> str:
    """Open this press's ONE stop card and return its id.

    An orphaned card from a prior attempt is REUSED, not swept. The old
    "defensive stale-card sweep" resolved the orphan AND appended a fresh row,
    so one press put two ``stop_event`` rows on the wire and the pane — which
    upserts by ``meta.id`` — renders two "[Stopped]" chips. Re-arming
    the existing row in place keeps the single-card-per-press invariant the
    peer-bound branch of ``stop_slot_turn`` documents, mirroring the
    escalation path's reuse of the open card rather than minting a second one.

    One helper for both cancel routes (``stop_slot_turn`` and
    ``api_chat_slot_interrupt``) so the reuse rule cannot drift between them.

    Reuse is scoped to a SAME-TURN orphan: when a ``user`` row follows the
    orphan, the next turn has begun and re-arming would mutate a row sitting
    in the previous turn's block — the press's chip would then appear (and
    transition) in earlier scrollback, attributing the stop to the wrong turn.
    A cross-turn orphan is settled where it lies and this press's card is
    appended fresh, which never renders two
    ADJACENT chips.
    """
    stale_id = slot._stop_event_id
    if stale_id and not _orphan_in_current_turn(slot, stale_id):
        _resolve_stop_event(slot, "soft")  # settle it where it lies
        stale_id = None
    stop_id = stale_id or f"stop-{uuid.uuid4().hex}"
    slot._stop_event_id = stop_id
    now_ts = datetime.now(tz=timezone.utc).isoformat()
    stop_data = {
        "kind": "stop_event",
        "id": stop_id,
        "state": state_label,
        "outcome": None,
        "ts_start": now_ts,
    }
    # cls must be JSON-encoded so parse_cls_meta() populates meta on the wire.
    # content mirrors the data for backward-compat with any consumer that only
    # reads content.
    stop_msg = json.dumps(stop_data)
    if not (stale_id and _rearm_stop_event(slot, stop_data)):
        if stale_id:
            # The re-arm found no row (lost a race with window trimming):
            # appending under the REUSED id would upsert into a client still
            # holding the old row and land the chip in old scrollback — the
            # failure mode reuse exists to avoid. Mint fresh for the append.
            stop_id = f"stop-{uuid.uuid4().hex}"
            slot._stop_event_id = stop_id
            stop_data["id"] = stop_id
            stop_msg = json.dumps(stop_data)
        # No same-turn orphan to re-arm: this press's one card is a fresh
        # append (a cross-turn or vanished orphan was settled above).
        slot.append("system", stop_msg, stop_msg)
    if stale_id and slot._stop_escalated_card_id == stale_id:
        # A stale escalation marker scoped to the REUSED id would make this
        # press's cooperative ack defer to a hard callback that already fired
        # (or never will), stranding the re-armed card at "stopping" — the
        # exact failure the id-scoped marker exists to remove. A swept card
        # never hit this because the fresh id could not match; reuse must
        # clear it explicitly.
        slot._stop_escalated_card_id = None
    return stop_id


def _make_stop_resolver(
    state: DashboardState, slot: _ChatSlot, outcome: str, card_id: str | None
) -> Callable[[], Awaitable[None]]:
    """Build the stop_turn on_soft/on_hard callback that settles the stop card.

    Key the guard on `_stop_event_id`, not on `_stop_state`. The card id is
    already the idempotency token: `_resolve_stop_event` no-ops when it is None
    and clears it once it has settled the card, so a state gate buys nothing
    there. What the state gate did buy was a bug. A turn tearing down
    concurrently drives `_stop_state` back to "idle" (`_finish_queue_cycle` in
    chat_runner.py, through the `_stopping` setter in state_parts/chat_slot.py), and that
    teardown races the escalation. When teardown won, the hard callback bailed,
    `_resolve_stop_event` never ran, and the card pulsed at "stopping" for the
    rest of the session instead of settling to "stop_failed_reset".

    Precedence needs its own non-racy marker. A cooperative ack that arrives
    after the user escalated must not relabel a hard kill as a clean stop, and
    `_stop_state` cannot carry that fact because the same teardown resets it to
    "idle" from `killing` just as readily as from `soft_pending`. Reading it
    here would reproduce the bug one dimension over: teardown erases the
    escalation, the late soft callback sees a neutral state, and the card
    settles as "stopped" for a session that was killed. So the escalation path
    sets `slot._stop_escalated_card_id`, which teardown never touches, and only
    the soft callback defers on it. `hard` is terminal and nothing outranks it.
    The marker holds an id rather than a flag so it cannot leak onto a later
    card: a bare boolean left set would make the NEXT card's cooperative ack
    defer to a hard callback that never fires, stranding that card at
    "stopping", which is the failure this marker exists to remove.

    Bind to `card_id`, the specific card this callback was created for, and not
    to whatever card happens to be in flight when it fires. `stop_turn` awaits
    these callbacks, so one can still be pending when teardown resets the stop
    posture, a new turn starts, and a second stop opens a card of its own —
    usually a NEW id, but a same-turn orphan is RE-ARMED under this very id
    (`_open_stop_event_card`), which is why the id comparison alone is
    not per-attempt identity; see the generation paragraph below. Reading
    `slot._stop_event_id` at call time would settle the newer stop's card with
    this older outcome and clear its posture, so the newer stop's own callback
    would find nothing left to settle. Callers pass the id they just assigned.

    `card_id` may be None, for a stop that escalated before any card existed.
    Such a callback still releases the stop posture; it simply has no card to
    label. Only a mismatching non-None current id means "someone else owns
    this", so only that case returns without touching the slot.

    Also bind `slot._stop_generation`, captured at creation. Card REUSE
    (`_open_stop_event_card`) makes the id comparison insufficient by
    construction: a press that re-arms an orphaned card carries the SAME id the
    prior press's still-pending callback was bound to, so matching ids no
    longer prove matching stops — the old callback would settle the re-armed
    card with the old outcome and release the new stop's posture. The
    generation counts stop INITIATIONS (the `_stop_state` setter bumps it on
    every idle → active edge and teardown never rewinds it), so "a newer stop
    has initiated since this callback was created" is exactly `generation !=
    slot._stop_generation` — and that newer stop's own callbacks own both the
    card and the posture, including the cardless-posture-release duty above
    (an initiation that rolls back before binding callbacks, like /interrupt's
    refused-body branch, resets the posture itself — its CARD, if a prior
    press's orphan was in flight, can stay at "stopping" until the next press
    sweeps or re-arms it: the generation is monotonic and never rewound, so
    the prior resolver bails. That residual is accepted deliberately — the
    posture is safe, the strand self-corrects on the next press, and settling
    the card from the rollback would label it with an outcome the still-
    pending cancel has not produced).
    """
    generation = slot._stop_generation
    session_key = effective_session_key(slot)

    async def _resolve() -> None:
        logger.debug(
            "stop resolver (%s): card_id=%r current=%r stop_state=%r escalated=%r gen=%d/%d",
            outcome,
            card_id,
            slot._stop_event_id,
            slot._stop_state,
            slot._stop_escalated_card_id,
            generation,
            slot._stop_generation,
        )
        # A newer stop initiated after this callback was created: everything —
        # the (possibly re-armed, same-id) card AND the posture — belongs to
        # that stop's own callbacks now. See the generation paragraph above.
        if generation != slot._stop_generation:
            return
        # Bail only when a DIFFERENT card is genuinely in flight, because that
        # card belongs to a later stop that owns the posture. Do not bail merely
        # because this attempt has no card: settling a card and releasing the
        # stop posture are separate jobs, and the posture must be released even
        # when there was never a card to settle. A stop can reach a callback
        # with `card_id` None: `api_chat_slot_interrupt` claims
        # `_stop_state = "soft_pending"` before it awaits the request body and
        # only then opens its card, so a concurrent `/stop` escalates against a
        # slot that has none yet. Skipping the reset there strands `_stop_state`
        # at "killing", which permanently suppresses re-queue
        # (`_should_suppress_requeue`) and rejects every later interrupt. That
        # wedges the slot, which is worse than the mislabel this guard prevents.
        if slot._stop_event_id is not None and slot._stop_event_id != card_id:
            return
        # `card_id is None` cannot mean "escalated": the marker holds a real
        # card id, so comparing None to None would defer a callback that no
        # hard kill will ever follow, and the posture would never be released.
        if outcome == "soft" and card_id is not None and slot._stop_escalated_card_id == card_id:
            logger.debug("stop resolver (soft): escalated to hard kill, deferring to hard")
            return
        # No-ops when there is no card, which is exactly the case above.
        _resolve_stop_event(slot, outcome)
        slot._stop_state = "idle"
        if card_id is not None and slot._stop_escalated_card_id == card_id:
            slot._stop_escalated_card_id = None
        state.push_slots_update()

        from kiro_crew.dashboard import responsive_chat

        # Teardown can yield the floor while reset is still awaiting completion.
        # Only the settled stop may wake its retained priority, on that same slot.
        if responsive_chat.first_priority_id(slot):
            async with slot._lock:
                if (
                    generation == slot._stop_generation
                    and state._slots.get(slot.key) is slot
                    and effective_session_key(slot) == session_key
                    and not slot.running
                    and not slot._turn_admission_reserved
                    and not slot.is_closing
                    and not slot._last_turn_auth_required
                    and slot._stop_state == "idle"
                    and not responsive_chat.pending_interaction(slot, idle=True)
                ):
                    slot._turn_admission_reserved = True
                    try:
                        await _start_next_queued_turn(state, slot)
                    finally:
                        slot._turn_admission_reserved = False

    return _resolve


def _cancel_target(slot: _ChatSlot) -> str:
    """The session a cancel on *slot* must address.

    Never ``_history_key_for(name)``: every slot carrying a
    ``linked_session_key`` — a cron-born tab (``cron:<job_id>``), a channel-born
    tab (``slack:<ts>``), a workflow-born tab — runs its turns under that key,
    while the dashboard-prefixed spelling names a session that never existed.
    ``SessionManager.stop_turn`` then finds nothing and returns ``"idle"``, the
    handler settles the card as "stopped", and the turn keeps streaming, so Stop
    is a silent no-op that reports success once per press.

    Routing alone is not enough either. A running turn owns a stable identity:
    ``_run_chat`` captures the key it acquires and keeps using that one for the
    whole turn, while
    ``linked_session_key`` remains mutable underneath it — a cron injection
    binds an already-live slot with no ``running`` gate. Re-deriving the key at
    cancel time therefore names wherever the slot routes the NEXT turn, which
    after a mid-turn rebind is not the turn the operator is trying to stop.

    Falls back to the routing when no turn is in flight (nothing to have
    captured an identity), which is also what a slot restored from disk answers
    — the field is runtime-only and empty after a restart.
    """
    return getattr(slot, "_active_turn_session_key", "") or effective_session_key(slot)


async def _settle_discarded_stage_deliveries(
    state: "DashboardState",
    slot: "_ChatSlot",
    contents: list[str],
) -> None:
    """Settle the queued completion debt a discarded queue owed."""
    manager = getattr(state, "subagents", None)
    if manager is None:
        return
    owed = slot.take_pending_subagent_deliveries(contents)
    if owed:
        try:
            settlement = manager.settle_queued_delivery(owed)
            if asyncio.iscoroutine(settlement):
                await settlement
        except Exception:
            logger.warning(
                "Could not settle discarded stage deliveries for slot %s",
                slot.key,
                exc_info=True,
            )


async def stop_slot_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    *,
    force: bool = False,
    source: str = "dashboard",
    cancel_key: str = "",
    escalate: bool = True,
) -> dict[str, Any]:
    """Stop the slot's turn: cooperative cancel, hard kill on a second call.

    First call: soft cancel. A second call while the first is still pending
    escalates to a hard kill, regardless of *force* — the caller's view of the
    stop state can lag the backend's, so the backend's own ``_stop_state`` is
    what decides.

    *escalate* is how a caller says its second call may not be a second
    DECISION. It defaults to True because that is true of the Stop button this
    function was written for: a person pressing again has watched the cooperative
    stop fail to take. It is not true of an RPC, where a client that timed out
    re-sends the same request — so ``session_control.stop_target`` passes False
    for a call it cannot distinguish from a retry, and the repeat falls through to
    the no-op below instead of discarding the target's queue. It
    withholds only the ESCALATION: a stop that finds the slot running still stops
    it either way.

    Inserts a ``stop_event`` card into the slot transcript so whoever is
    watching the session sees the stop, and returns the JSON body the route
    would have sent. *source* labels the SEL audit line with who asked.

    *cancel_key* is the session the stop must land on, resolved ONCE by the
    caller. A caller that authorizes the stop has to pass the very key it
    authorized: re-deriving it here could name a different session if a rebind
    lands between the check and the cancel, which is the whole reason the route
    resolves it up front. Omitted only by callers with nothing to authorize
    against, which fall back to the slot's own routing.
    """
    name = slot.key
    cancel_key = cancel_key or _cancel_target(slot)

    # The second press after a DECLINED Stop (the session was compacting) is
    # the user's escape hatch and takes the escalation branch below, which is
    # the only path to a hard kill. The decline itself leaves ``_stop_state``
    # idle -- the queue drain reads that machine as "a stop is in progress" and
    # would persist a false "Session reset" row on the next queued turn -- so the
    # arming lives in its own marker, consumed here: the claim it makes lasts
    # exactly as long as the escalation needs it, on a press that IS a stop.
    # Gated on the caller's own ``escalate``: a caller that says its call may be
    # a retry (a board stop re-sent by a timed-out browser, the steer-containment
    # stop) must never be turned into a hard kill by a marker it did not set.
    # ...and on the compaction still holding the session: the marker is a
    # memory of a refusal, not proof the refusal still applies. Once the
    # compaction has ended, the next press is an ordinary first press and
    # takes the cooperative path; the stale marker is simply dropped.
    # Set when this press is the ESCAPE from a compaction decline (the armed
    # second press, or an explicit ``?force=true``). The hard kill below then
    # keeps the SESSION queue: on a channel-linked slot that queue is the linked
    # channel's, holding other people's messages the compaction, not the user,
    # is what the Stop is aimed at; ``stop_turn`` parks it for the successor.
    # The slot's own dashboard queue is still discarded, as on any hard kill.
    compaction_escape = False
    if slot.running and slot._stop_state == "idle" and stop_declined_armed(slot):
        if escalate and _compaction_in_flight(state, cancel_key):
            # Consumed by the press it armed. A non-escalating call (a retry, the
            # steer-containment stop) must not spend the user's hatch, and a
            # marker for a compaction that has ended is dropped as stale.
            slot._stop_declined_at = 0.0
            slot._stop_state = "soft_pending"
            compaction_escape = True
        elif not _compaction_in_flight(state, cancel_key):
            slot._stop_declined_at = 0.0
    elif (
        force
        and escalate
        and slot.running
        and slot._stop_state == "idle"
        and _compaction_in_flight(state, cancel_key)
    ):
        # An explicit ``?force=true`` on an idle stop state is the escape hatch
        # from the DECLINE only: it must reach the hard-stop escalation below
        # rather than be declined. Without a compaction, a ``force`` arriving on
        # an idle state is a retried first press whose original was lost on the
        # wire, and it keeps the cooperative path it always had.
        slot._stop_state = "soft_pending"
        compaction_escape = True

    # Escalation path: a second stop press while a cooperative cancel is
    # already pending hard-kills. We escalate on ANY second press — not only
    # when the client computed force=true — because the client derives force
    # from the WS-echoed stop_state, which may lag behind the actual state on a
    # slow connection. The backend's own _stop_state is the authoritative
    # "already soft_pending" signal, so a second press always means "kill it".
    #
    # Unless the caller told us this call may not be a second press at all
    # (*escalate*): a re-sent RPC carries no new intent, and the caller is the
    # only layer that can know whether its second call was a decision or a
    # timeout retry. A withheld escalation falls into the no-op branch below.
    if escalate and slot._stop_state == "soft_pending":
        slot._stop_state = "killing"
        # A decline settled its own card and left no open one; the hard kill
        # that follows needs a row of its own, or the last stop row the user
        # sees still reads "nothing was stopped" for a session that was reset.
        if not slot._stop_event_id:
            _open_stop_event_card(slot, "stopping")
        # Survives turn teardown, which resets _stop_state to "idle". Without
        # it a cooperative ack from the first press could still land and label
        # this hard kill a clean stop. Scoped to this card so it cannot defer
        # a later card's ack.
        slot._stop_escalated_card_id = slot._stop_event_id
        # Take the queue and empty it in one step, then settle what was taken. The
        # settle can suspend (it waits on a sub-agent's teardown and writes its
        # tombstones on a worker thread), and a send that lands meanwhile is
        # acknowledged as queued; a clear after the await would discard it with
        # nothing settled and nothing said. The kill discards what was queued
        # when it was pressed, and a later send stays queued for the next turn.
        discarded = list(slot._queue)
        slot._queue.clear()
        # A cross-session delivery among the taken entries has a sender holding a
        # "queued, will run later" receipt; nothing else here reaches that sender.
        # Told before the settle can suspend, so a cancellation of this stop
        # cannot leave a discarded entry unreported.
        # circular import: session_control imports this package's modules at module level.
        from kiro_crew.dashboard.session_control import (
            SEND_DISCARD_HARD_STOP,
            notify_send_origin_discarded,
        )

        for _entry in discarded:
            notify_send_origin_discarded(
                state,
                entry_meta=_entry.get("meta"),
                target_slot=slot,
                text=_entry.get("content") or "",
                cause=SEND_DISCARD_HARD_STOP,
            )
        await _settle_discarded_stage_deliveries(
            state,
            slot,
            [str(entry.get("content", "")) for entry in discarded],
        )
        # Hard kill = "discard everything": drop unconsumed steers too, so the
        # end-of-turn requeue (chat_runner finally) has nothing to resurrect.
        # Mirrors the queue clear above; a soft stop preserves both.
        #
        # Their delivery ids go with them, and that is load-bearing rather than
        # tidiness: `steer_into_running_turn` reconciles an in-flight steer by
        # asking what removed its registration, and a CONSUMED steer leaves its
        # `_steer_delivery_ids` entry in place. Dropping the entry here is
        # therefore what tells the two apart -- absence means this hard kill
        # discarded the text, so the caller is told it was not delivered instead
        # of having a row persisted for a message that never ran.
        for _discarded in slot._pending_steers:
            slot._steer_delivery_ids.pop(_discarded, None)
            # Lockstep with the line above (see `_ChatSlot._steer_send_ids`): a hard
            # kill discards the text, so there is no requeued entry left to carry
            # the client's send id onto.
            slot._steer_send_ids.pop(_discarded, None)
            slot._steer_user_origin.pop(_discarded, None)
            slot._steer_channel_origin.pop(_discarded, None)
            slot._steer_admissions.pop(_discarded, None)
            slot._steer_attachment_meta.pop(_discarded, None)
            slot._steer_act_now.discard(_discarded)
            slot._steer_automation.pop(_discarded, None)
            slot._steer_decision_strips.pop(_discarded, None)
            slot._steer_possibly_delivered.discard(_discarded)
        slot._pending_steers.clear()
        state.push_slots_update()
        logger.info("Stop (force): hard-killing session for slot %s", name)

        # Escalation reuses the card the first press opened, so bind to it.
        _on_hard_force = _make_stop_resolver(state, slot, "hard", slot._stop_event_id)

        # Unblock chat runner if it's suspended waiting for tool approval or on
        # a pending ask_question card.
        _unblock_pending_waits(state, slot)
        # Stop addresses the SESSION, so it resolves through
        # effective_session_key: a channel-linked slot's turns run under its
        # linked_session_key (slack:<ts>), and handing stop_turn the
        # dashboard:<slot> key names a session no running turn owns — the stop
        # reports success and cancels nothing. The SEL record below stays on the
        # slot-derived key, which identifies the tab the operator pressed.
        await state.sessions.stop_turn(
            cancel_key,
            force=True,
            preserve_queue=compaction_escape,
            on_hard=_on_hard_force,
        )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="hard",
            # Record what the client requested (force flag) vs. the escalation
            # the backend actually performed (always a hard kill here).
            metadata={"slot": name, "via": source, "force": force, "escalated": True},
        )
        return {"ok": True}

    # A turn another path runs on this slot's linked session (the Slack thread a
    # channel-born tab is bound to) reads as running on the dashboard but owns no
    # task here, so the stop machine below has nothing to drive. Hand the press
    # to the session it runs on, keeping the queue (it holds the channel's other
    # messages); the row settles when the channel path ends the turn and
    # releases the lease. ``stop_turn`` owns the session-side rules (it records
    # the Stop so a replay stays dropped, escalates a cancel that is never
    # acked, and declines while the session compacts); the dashboard half of the
    # compaction decline is mirrored here: a decline arms the marker, and the
    # next press -- or an explicit force -- is the hard stop.
    if slot._stop_state == "idle" and not slot.running and slot.linked_turn_running:
        # Same escape rules as the dashboard's own turn above: a hard stop is
        # only the way out of a compaction decline (the armed second press, or
        # an explicit force), and a marker for a compaction that has ended is
        # dropped, so a force re-sent after a lost reply stays cooperative.
        compacting = _compaction_in_flight(state, cancel_key)
        if not compacting:
            slot._stop_declined_at = 0.0
        hard = escalate and compacting and (force or stop_declined_armed(slot))
        logger.info(
            "Stop: %s cancel for the linked turn on slot %s",
            "hard" if hard else "cooperative",
            name,
        )
        outcome = await state.sessions.stop_turn(cancel_key, force=hard, preserve_queue=True)
        if hard:
            slot._stop_declined_at = 0.0
        elif outcome == "compacting" and escalate:
            slot._stop_declined_at = time.monotonic()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome=f"linked_{outcome}",
            metadata={"slot": name, "via": source, "session": cancel_key, "force": hard},
        )
        if outcome == "compacting":
            state.push_slots_update()
            return {"ok": True, "info": "compacting", "compacting": True}
        if outcome == "idle":
            # The lease is held but the provider has no turn to cancel yet (the
            # window before the prompt is sent). Named, like the main path's.
            return {"ok": True, "info": "no active turn"}
        return {"ok": True, "info": "stopping linked turn", "already_stopping": True}

    # Already stopping or not running — no-op (idempotent repeat press guard)
    if slot._stop_state != "idle" or not slot.running:
        if not slot.running:
            logger.info("Stop: slot %s not running, ignoring", name)
            _info = "not running"
        else:
            _info = "stop already in progress"
        _meta: dict[str, Any] = {"slot": name, "via": source, "reason": _info}
        if not escalate and slot._stop_state == "soft_pending":
            # The branch a de-duplicated retry lands on. Recorded so the audit
            # shows an escalation was WITHHELD rather than never asked for --
            # without it there is no record that a retry rather than a decision
            # caused the outcome, and an absorbed retry has to be visible too.
            _meta["escalation_withheld"] = True
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="noop",
            metadata=_meta,
        )
        # ``already_stopping`` separates the two facts this branch merges: a
        # target that was never running has nothing to stop, while one whose
        # cooperative cancel is still in flight IS stopping. Both answer
        # ``info``, and a caller that renders them alike tells the second one the
        # opposite of what happened — which the de-duplicated retry above now
        # reaches routinely.
        return {"ok": True, "info": _info, "already_stopping": bool(slot.running)}

    # A cooperative Stop while the session's own automatic /compact holds it is
    # DECLINED, before any of the soft-stop side effects below run. Cancelling
    # that turn fails the compaction, and the failure arm recycles the session:
    # a user who pressed Stop on what looked like a stalled turn would lose the
    # session's memory to a restart the notice then blames on compaction.
    # The card the press would have opened is opened and settled in
    # one step, so the press still leaves a visible answer in the transcript.
    # A force stop (second press, or ?force=true) is the escape hatch and is
    # never declined: the force branch above runs first, and the decline below
    # arms ``_stop_declined_at`` so the NEXT press reaches that branch instead
    # of being declined again -- a live turn that shares the session with a
    # compaction must stay stoppable. ``_stop_state`` stays idle: nothing is
    # stopping. ``stop_turn`` repeats the compacting check for the race in
    # which a compaction starts between here and the cancel.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "stopping")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        if escalate:
            # Only a press that could itself escalate arms the second press; a
            # caller that said "this may be a retry" must not arm a hard kill
            # for the user's next ordinary Stop.
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
        logger.info("Stop: declined for slot %s — compaction in flight", name)
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_stop",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "via": source, "force": False, "stop_id": stop_id},
        )
        return {"ok": True, "info": "compacting", "compacting": True}

    # First press: soft stop
    slot._stop_state = "soft_pending"
    # NOTE: Do NOT clear the queue here — stop should only cancel the
    # currently running turn, leaving queued messages intact for the user
    # to process or dismiss individually.

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "stopping")
    state.push_slots_update()
    logger.info("Stop: cooperative cancel for slot %s (queue=%d)", name, len(slot._queue))

    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # A genuine in-flight turn whose cooperative cancel does not confirm within
    # the budget answers ``stop_turn`` with a non-acked outcome, which that
    # method escalates to a hard reset on its own -- a dispatched, mid-execution
    # tool call that never acks is reaped there. ``"idle"`` is the opposite
    # signal: the provider holds no active turn to cancel (its model stream
    # reached the done boundary, or no session is registered for the key). When
    # the slot still reads running at that point its turn ended at the provider
    # but the slot has not seen the terminal event settle it. The orphaned card
    # is resolved and the reply names the honest state -- there is no running
    # provider turn here to report as "stopped", and no provider turn to kill.
    _idle_running = outcome == "idle" and slot.running
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The race the pre-check above cannot close: the compaction committed
        # between that read and the cancel. Same answer, and the soft-stop side
        # effects taken above are undone: ``_stop_state`` back to idle (nothing
        # is stopping), the decline marker armed so the next press escalates,
        # as on the pre-check path.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        if escalate:
            slot._stop_declined_at = time.monotonic()
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "via": source, "force": False},
    )
    if outcome == "compacting":
        return {"ok": True, "info": "compacting", "compacting": True}
    if _idle_running:
        # The provider held no active turn to cancel while the slot still read
        # running: the turn ended at the provider but the slot had not settled.
        # Naming it keeps the reply from reading as "stopped a running turn".
        return {"ok": True, "info": "no active turn"}
    return {"ok": True}


def _compaction_in_flight(state: DashboardState, cancel_key: str) -> bool:
    """The shared pre-stop probe (``session_lifecycle.compaction_in_flight``)."""
    return compaction_in_flight(state.sessions, cancel_key)


async def api_chat_slot_stop(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/stop — cooperative stop with kill fallback.

    The route is where authorization lives, because it is the only layer holding
    the ``request`` an app token rides on. ``stop_slot_turn`` is the mechanism
    and takes a slot, so every caller that reaches it by another path (session
    control) has to establish its own authority — the guard cannot be inherited
    by accident.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    denied = deny_app_slot_access(request.get("app", ""), slot, name, "slot_stop")
    if denied is not None:
        return denied
    # Before ANY side effect — the escalation branch inside stop_slot_turn clears
    # the queue and drops pending steers, so a guard placed later would still let
    # a foreign caller mutate the slot. One target, resolved once: the session the
    # in-flight turn actually runs on, so authorization and the stop cannot
    # disagree across a mid-turn rebind.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_stop", cancel_key)
    if denied is not None:
        return denied
    force = request.query.get("force", "").lower() == "true"
    return web.json_response(await stop_slot_turn(state, slot, force=force, cancel_key=cancel_key))


async def api_chat_slot_interrupt(request: web.Request) -> web.Response:
    """POST /api/chat/slots/{slot}/interrupt — run a selected queued message.

    A running parent turn is stopped while its queue is preserved for the normal
    tail drain. An idle parent requires ``{"queue_id": "..."}`` and dispatches
    that selected queue card directly; this is the explicit override for a user
    who does not want to wait for attached subagents. A running parent accepts
    ``queue_id`` optionally to promote one card before the preserved queue drains.
    """
    state: DashboardState = request.app["state"]
    name = request.match_info["slot"]
    slot = state._slots.get(name)
    if not slot:
        return slot_not_found()
    # Before the _stop_state claim and the queue promotion below, both of which
    # mutate the slot ahead of stop_turn.
    # Resolved once, before the request-body await below, and used for both the
    # guard and the cancel — see api_chat_slot_stop for the same rule.
    cancel_key = _cancel_target(slot)
    denied = _app_cancel_denied(request, slot, "chat_interrupt", cancel_key)
    if denied is not None:
        return denied
    # The turn to interrupt may run on the channel path (the Slack thread this
    # tab is bound to): no task here to stop, so it is cancelled on the session
    # it runs on, keeping the queue -- but only once the request has passed every
    # check that could still refuse it, so a refused interrupt never kills a
    # live turn (possibly someone else's, in a shared thread).
    linked_turn = not slot.running and slot.linked_turn_running

    async def _cancel_linked_turn() -> str:
        outcome = await state.sessions.stop_turn(cancel_key, preserve_queue=True)
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome=f"linked_{outcome}",
            metadata={"slot": name, "session": cancel_key},
        )
        return outcome

    if linked_turn and not slot._queue:
        outcome = await _cancel_linked_turn()
        if outcome == "compacting":
            return web.json_response({"ok": True, "info": "compacting", "compacting": True})
        if outcome == "idle":
            return web.json_response({"ok": True, "info": "no active turn"})
        return web.json_response({"ok": True, "info": "stopping linked turn"})
    if not slot.running:
        if not slot._queue:
            return web.json_response({"ok": True, "info": "not running"})
        refusal = relay_archive_refusal(slot)
        if refusal is not None:
            return refusal
        body, body_err = await read_bounded_json(request, allow_absent=True)
        if body_err is not None:
            return body_err
        assert body is not None  # read_bounded_json returns (dict, None) on success
        raw_queue_id = body.get("queue_id")
        if raw_queue_id is not None and not isinstance(raw_queue_id, str):
            return web.json_response(
                {"error": "queue_id must be a string", "code": "invalid_queue_id"},
                status=400,
            )
        queue_id = (raw_queue_id or "").strip() or None
        # The idle bypass is the "run THIS selected card while attached
        # subagents keep going" action, and nothing else. Requiring an explicit
        # queue_id keeps `allow_user_during_subagents=True` and the dispatch
        # itself bound to a card the user picked: without one there is no
        # selection to justify bypassing the child-work hold, and dispatching
        # whatever sits at the queue front would run — and acknowledge —
        # unselected work the user never chose. Reject rather than fall back.
        if queue_id is None:
            return web.json_response(
                {"error": "queue_id required for idle interrupt", "code": "invalid_queue_id"},
                status=400,
            )
        if linked_turn:
            # Validated: cancel the channel-path turn, then dispatch the selected
            # card below, which starts once the channel path releases the session.
            await _cancel_linked_turn()
        async with slot._lock:
            # The request body and lock acquisition both yield. A close followed
            # by same-name recreation during either await must not let this stale
            # object dispatch work into the replacement's session namespace.
            if _slot_replaced_while_queued(state, slot, name, request, "chat.slot_interrupt"):
                return slot_not_found()
            if slot.running:
                return web.json_response(
                    {"error": "slot started running", "code": "slot_running"}, status=409
                )
            if slot._stopping or slot._stop_state != "idle":
                return web.json_response(
                    {"error": "a stop is in progress", "code": "slot_stopping"}, status=409
                )
            # The body read above can race a cron/workflow rebind on this same
            # live slot. Re-authorize the session the queued turn will use while
            # holding the dispatch lock, immediately before starting it.
            denied = _app_cancel_denied(
                request, slot, "chat_interrupt", effective_session_key(slot)
            )
            if denied is not None:
                return denied
            started = await _start_next_queued_turn(
                state,
                slot,
                allow_user_during_subagents=True,
                required_queue_id=queue_id,
            )
        if not started:
            return web.json_response(
                {
                    "error": "queued message is no longer available",
                    "code": "queue_item_unavailable",
                },
                status=409,
            )
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="started",
            metadata={"slot": name, "queue_id": queue_id},
        )
        state.push_slots_update()
        return web.json_response({"ok": True, "outcome": "started"})
    # Idempotent guard: interrupt already in progress. State alone decides —
    # do NOT also require _stop_event_id: after the early soft_pending claim
    # below, a concurrent request can arrive before the stop card is created
    # (event id still None), and a compound condition would let it through.
    if slot._stop_state != "idle":
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop already in progress"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})
    if not slot._queue:
        return web.json_response({"error": "queue empty, use /stop instead"}, status=400)

    # An automatic compaction holds the session: the interrupt is declined
    # BEFORE the claim below mutates the running turn (``_stop_state``) and
    # before ``_unblock_pending_waits`` rejects its pending
    # approvals -- none of that may happen to a turn that is not being stopped.
    # No escalation marker: an interrupt is "run the next queued message", not
    # a Stop, and must not turn the user's next Stop press into a hard kill.
    # ``_stop_state`` stays idle.
    if _compaction_in_flight(state, cancel_key):
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Claim the stop slot synchronously BEFORE the await below: the
    # idempotency guard above is check-then-act, and a concurrent /interrupt
    # arriving during the awaited body read below would otherwise still see
    # _stop_state == "idle" and slip past the guard (double stop_turn +
    # double SEL audit for one logical press). /stop is race-safe because it
    # has no await between guard and claim; this makes /interrupt match.
    slot._stop_state = "soft_pending"
    # Per-attempt identity for the claim itself. The stand-down guard below
    # cannot rely on the state VALUE alone: a concurrent /stop can escalate,
    # settle to idle, and a further press can re-claim "soft_pending" — a
    # LATER stop wearing the same value. The generation tells the two apart
    # (`_make_stop_resolver` already establishes it as the only per-attempt
    # identity that survives card reuse); the claim above bumped it, so any
    # later initiation moves it again.
    claim_generation = slot._stop_generation

    # Optionally promote a specific queue item to front. The except is not a
    # parse guard (read_bounded_json owns that): it rolls the claimed stop
    # state back when the body read fails in transit, and the refused-body
    # branch below rolls it back the same way. The rollback is
    # conditional on our claim being intact: a concurrent /stop arriving
    # during the body await may escalate _stop_state (e.g. to "killing"),
    # and an unconditional reset to "idle" would erase that escalation and
    # admit another stop while the hard kill is still running.
    try:
        body, body_err = await read_bounded_json(request, allow_absent=True)
    except Exception:
        # Generation-guarded like the stand-down below: "soft_pending" alone
        # cannot prove the claim is OURS — an escalate-settle-repress sequence
        # during the await leaves a LATER press's live claim wearing the same
        # value, and rolling that back would idle its stop mid-cancel and
        # re-enable auto-run under a real stop.
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        raise
    if body_err is not None:
        if slot._stop_state == "soft_pending" and slot._stop_generation == claim_generation:
            slot._stop_state = "idle"
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success
    queue_id = body.get("queue_id")
    if queue_id and slot._stop_generation == claim_generation:
        # Wire-side field is `queue_id`; stored items carry `id` (the key
        # queue_append/queue_insert write and every *_by_id helper matches).
        # The previous inline loop compared item.get("queue_id"), which is
        # None on every production item — a silent no-op that made the
        # "run this next" click land on whatever happened to be at the
        # front of the queue instead of the selected message.
        #
        # BEFORE the supersede guard below, and GENERATION-GATED, because the
        # supersessions differ: a claim superseded benignly (the running turn
        # ends during the body read, teardown resets the posture, generation
        # unmoved) must still land the user's "run this next" choice; a claim
        # superseded by a LATER stop (generation moved) must NOT — that stop's
        # own /interrupt may have promoted ITS selection, and a stale write
        # here would overwrite it. Escalation keeps the same generation and a
        # cleared queue, so promotion there is a harmless no-op.
        slot.queue_promote_by_id(queue_id)

    # A concurrent /stop can supersede our claim during the body await:
    # escalate it (soft_pending → killing), or escalate-settle-and-be-followed
    # by a FURTHER press whose fresh claim wears the same "soft_pending" value
    # — which is why this compares the GENERATION, our claim's per-attempt
    # identity, not just the state value. Continuing on a superseded claim
    # would open/reuse a card owned by the other stop — and the reuse path's
    # marker-clear would erase a LIVE escalation marker, letting a late
    # cooperative ack relabel the hard kill as a clean stop. The other stop
    # owns the posture now: stand down and answer like the idempotent-repeat
    # branch above. This also fires when the superseding stop has ALREADY settled
    # (state back to "idle"), including the benign case where the running
    # turn simply ended during the body read; queue promotion already
    # happened above, so nothing of the user's intent is dropped.
    if slot._stop_state != "soft_pending" or slot._stop_generation != claim_generation:
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="noop",
            metadata={"slot": name, "reason": "stop claim superseded during body read"},
        )
        return web.json_response({"ok": True, "info": "stop already in progress"})

    # The probe again, AFTER the body await and with no await between here and
    # the pending-wait rejection below. A compaction that committed during the
    # body read would otherwise have the user's pending approval or question
    # rejected (irreversible) by a Stop the compaction then declines. Same
    # answer as the pre-check: the claim is released, nothing is touched.
    if _compaction_in_flight(state, cancel_key):
        slot._stop_state = "idle"
        stop_id = _open_stop_event_card(slot, "interrupting")
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        state.push_slots_update()
        sel().log_tool_invocation(
            session_key=_history_key_for(name),
            agent=getattr(slot, "agent", "") or "kirocrew",
            source="dashboard",
            tool_name="dashboard_interrupt",
            tool_kind="command",
            outcome="compacting",
            metadata={"slot": name, "stop_id": stop_id, "reason": "committed during body read"},
        )
        return web.json_response({"ok": True, "outcome": "compacting", "compacting": True})

    # Stop current turn but preserve the queue so dequeue loop fires
    # (soft_pending already claimed above, before the request-body await)

    # One card per press: re-arm an orphaned card in place or append a fresh
    # one (see _open_stop_event_card for why sweeping the orphan rendered two
    # chips).
    stop_id = _open_stop_event_card(slot, "interrupting")
    state.push_slots_update()

    # Built after the card exists so each resolver is bound to this card.
    _on_soft = _make_stop_resolver(state, slot, "soft", stop_id)
    _on_hard = _make_stop_resolver(state, slot, "hard", stop_id)

    # Unblock chat runner if it's suspended waiting for tool approval or on a
    # pending ask_question card.
    _unblock_pending_waits(state, slot)

    outcome = await state.sessions.stop_turn(
        cancel_key,
        force=False,
        preserve_queue=True,
        on_soft=_on_soft,
        on_hard=_on_hard,
    )
    # Resolve orphaned card when provider reports no active turn
    if outcome == "idle" and slot._stop_event_id:
        _resolve_stop_event(slot, "soft")
        slot._stop_state = "idle"
        state.push_slots_update()
    elif outcome == "compacting":
        # The window the two probes above cannot close: a compaction commits
        # between the second probe and ``stop_turn`` taking the registry lock,
        # with no await of ours in between, so only another task's tick can
        # land here. Nothing was interrupted. The pending waits
        # ``_unblock_pending_waits`` rejected cannot be un-rejected, which is
        # why both probes run first. ``_stop_state`` goes back to idle; no
        # escalation marker, for the reason the pre-check gives.
        _resolve_stop_event(slot, "compacting")
        slot._stop_event_id = None
        slot._stop_state = "idle"
        state.push_slots_update()
    sel().log_tool_invocation(
        session_key=_history_key_for(name),
        agent=getattr(slot, "agent", "") or "kirocrew",
        source="dashboard",
        tool_name="dashboard_interrupt",
        tool_kind="command",
        outcome=outcome,
        metadata={"slot": name, "queue_id": queue_id},
    )
    return web.json_response({"ok": True, "outcome": outcome})
