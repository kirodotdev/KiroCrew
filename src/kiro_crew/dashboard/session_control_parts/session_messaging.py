"""Writing into another session's conversation: ``session_send`` and
``session_broadcast``, the provenance envelope, and the steer deliveries a cancelled
sender leaves behind.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.context_assembly.markers import _apply_marker_spans, _marker_spans
from kiro_crew.dashboard import session_managers
from kiro_crew.dashboard.chat_delivery import sanitize_outbound
from kiro_crew.dashboard.relay_archive import (
    RELAY_ARCHIVE_CODE,
    RELAY_ARCHIVE_ERROR,
    is_relay_archive,
)
from kiro_crew.dashboard.session_control_parts.caller_access import (
    SessionControlError,
    _audit,
    _caller_is_ownership_fenced,
    caller_slot_key,
    prewarm_enabled_check,
)
from kiro_crew.dashboard.session_control_parts.delivery_containment import (
    containment_meta,
    containment_snapshot,
    newly_held_constraints,
    send_origin_meta,
)
from kiro_crew.dashboard.session_control_parts.target_authorization import (
    _broadcast_resolution_slots,
    _deny_factory,
    _resolve_slot,
    authorize_target,
    load_target_window,
    refuse_caller_identity,
    refuse_caller_surface,
)
from kiro_crew.sel import sel
from kiro_crew.validation import (
    BROADCAST_TARGET_ALLOWANCE_SECS,
    MAX_BROADCAST_TARGETS,
    MAX_LONG_STRING,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState

# The module's original logger name, which log filters and caplog assertions key on.
logger = logging.getLogger("kiro_crew.dashboard.session_control")


#: Cap on one delivered message. Aliased to ``validation.MAX_LONG_STRING`` rather
#: than restated as its own number: a seed prompt is that shape, the MCP schema
#: layer already rejects on that constant, and two spellings of one 50k limit
#: would drift apart the first time either moved.
MAX_SEND_MESSAGE_CHARS = MAX_LONG_STRING


#: Provenance prefix on every delivered message. The target's transcript renders
#: the message as a user row, and without this line it is indistinguishable from
#: something the person typed — the same reason auto-nudge tags its injected
#: turns ``[auto-nudge cycle N]``. The model in the target session sees it too,
#: so it can weigh the instruction as coming from a peer session, not its user.
_SEND_PROVENANCE = "[sent by session {caller} via {via}]\n\n"


#: A provenance-shaped opener inside a caller's body. The envelope tells the
#: target which peer sent the message, and the body is caller-controlled, so a
#: body line of the same shape must not read as a second envelope.
#:
#: Matched on a normalized view (see ``context_assembly.markers._marker_spans``:
#: NFKC, every ``Cf`` char dropped, dashes folded) rather than by listing
#: spellings, so bidi controls, zero-width chars, soft hyphens and compatibility
#: forms all fold away before the match. Any ``Ps`` opening bracket counts; every
#: ``Ps`` code point sits in the BMP, and scanning only the BMP keeps import cheap.
_PS_OPENERS = "".join(chr(cp) for cp in range(0x10000) if unicodedata.category(chr(cp)) == "Ps")


_FORGED_PROVENANCE_RE = re.compile(
    rf"[{re.escape(_PS_OPENERS)}]\s*sent\s*by\s*session\b", re.IGNORECASE
)


_QUOTED_PROVENANCE = "[quoted: sent by session"


def _neutralize_send_provenance(body: str) -> str:
    """Defang any provenance-shaped opener in a peer-sent *body*.

    Only the matched span in the ORIGINAL text is rewritten, so the rest of the
    body stays byte-exact. Substitution rather than deletion, so a reader still
    sees that the sender wrote something envelope-shaped. Only the genuine
    envelope, prepended by this module, opens with ``[sent by session``.
    """
    # Case is folded by the pattern's IGNORECASE (Unicode simple case folding),
    # which keeps the normalized view length-stable for the span map back.
    spans = _marker_spans(body, (_FORGED_PROVENANCE_RE,))
    return _apply_marker_spans(body, spans, _QUOTED_PROVENANCE)


#: What the envelope names for a broadcast. The verb is part of the message's
#: meaning, not decoration: "the base moved, rebase before you push" addressed to
#: one worker is a fact about that worker's branch, and the same words addressed to
#: eight are a fact about the base. A worker that cannot tell which it received
#: cannot judge whether a sibling is about to make the same change.
BROADCAST_VIA = "session_broadcast"


@dataclass
class _DeliveryProgress:
    """Delivery-call-owned facts that survive cancellation into the caller."""

    steer_await_entered: bool = False


# Shielded steer deliveries whose awaiting frame was cancelled before they
# finished. A shielded task is referenced only by the shield wrapper our frame
# just dropped, so without a strong reference here the event loop may collect it
# MID-RPC -- which is the very interruption the shield exists to prevent. Entries
# remove themselves in the done callback, so the set holds at most the deliveries
# currently outliving their caller.
_ORPHANED_STEER_DELIVERIES: set["asyncio.Future[Any]"] = set()


def _retain_orphaned_steer_delivery(
    task: "asyncio.Future[Any]",
    slot_key: str,
    *,
    unavailable_outcome: str | None = None,
    on_unavailable: "Callable[[], Awaitable[None]] | None" = None,
) -> None:
    """Keep a shielded steer delivery alive after its caller stopped waiting.

    The caller has already reported this delivery as cancelled-with-unknown-outcome,
    so nothing downstream reads the result. Two things still have to happen. The
    delivery coroutine's OWN reconciliation -- popping the per-text steer maps and
    recording the transcript row -- which it does itself once it is not interrupted.
    And the caller's post-outcome handling for ``unavailable_outcome``, which a
    cancelled frame cannot run: that outcome means the text was never handed over,
    so without ``on_unavailable`` queueing it the instruction is lost rather than
    merely delayed. Logged either way, because this is the only trace that a
    delivery completed after its caller moved on.
    """
    _ORPHANED_STEER_DELIVERIES.add(task)

    def _done(finished: "asyncio.Future[Any]") -> None:
        _ORPHANED_STEER_DELIVERIES.discard(finished)
        if finished.cancelled():
            # Only a loop shutdown reaches here: the shield absorbed the caller's
            # cancellation, so nothing else cancels this task.
            logger.warning(
                "session_send: shielded steer delivery to %s was cancelled outright; "
                "its steer bookkeeping may not have reconciled",
                slot_key,
            )
            return
        exc = finished.exception()
        if exc is not None:
            logger.warning(
                "session_send: shielded steer delivery to %s finished with %r after "
                "its caller stopped waiting",
                slot_key,
                exc,
            )
            return
        outcome = finished.result()
        logger.info(
            "session_send: shielded steer delivery to %s finished with outcome=%s "
            "after its caller stopped waiting",
            slot_key,
            outcome,
        )
        if on_unavailable is None or outcome != unavailable_outcome:
            return
        # Scheduled rather than awaited: a done callback runs on the loop and cannot
        # await. Retained in the same set for the same reason the delivery is -- a
        # task referenced only by a local would be collectible mid-queue.
        fallback = asyncio.ensure_future(on_unavailable())
        _ORPHANED_STEER_DELIVERIES.add(fallback)

        def _fallback_done(done: "asyncio.Future[Any]") -> None:
            _ORPHANED_STEER_DELIVERIES.discard(done)
            if done.cancelled():
                logger.warning(
                    "session_send: the queue fallback for the orphaned steer to %s was "
                    "cancelled; that message is not queued",
                    slot_key,
                )
                return
            exc = done.exception()
            if exc is not None:
                logger.warning(
                    "session_send: the queue fallback for the orphaned steer to %s "
                    "failed with %r; that message is not queued",
                    slot_key,
                    exc,
                )

        fallback.add_done_callback(_fallback_done)

    task.add_done_callback(_done)


def _start_auto_tag(state: DashboardState, slot: Any) -> None:
    """Tag a session whose turn this send started, as a typed message would.

    The composer starts auto-tagging on every send (``chat_api.chat_send``). A
    session that only ever receives ``session_send`` never passes through it,
    so without this its sidebar row carries no project tag. Only the run arm
    calls this: the user row is already on the transcript there, so the topic
    pass reads the right first message. ``maybe_auto_tag``'s own once-guard
    makes a repeat call free.
    """
    if getattr(slot, "_auto_tagged", False):
        return
    # Deferred for the same import cycle `_run_chat` documents in `send_to_target`.
    from kiro_crew.dashboard.chat_auto_tag import maybe_auto_tag

    task = asyncio.create_task(maybe_auto_tag(state, slot))
    state._background_tasks.add(task)
    task.add_done_callback(state._background_tasks.discard)


async def send_to_target(
    state: "DashboardState",
    *,
    caller_session_key: str,
    target: str,
    message: str,
    steer: bool = False,
    caller_fenced: bool | None = None,
    via: str = "session_send",
    _delivery_progress: "_DeliveryProgress | None" = None,
) -> dict[str, Any]:
    """Deliver *message* to *target* as its next agent turn.

    ``via`` names the VERB in the provenance envelope the target reads. It exists
    because :func:`broadcast_to_targets` delivers through this function, and a
    worker told something every sibling was also told must be able to see that:
    the same sentence means one thing addressed to one session and another
    addressed to eight. It never affects authorization or delivery.

    The delivery path is the same queue-vs-run decision the dashboard composer
    uses (``enqueue_or_run_prompt``): an idle target starts a turn immediately,
    a busy one queues the message for its next turn. Both outcomes are reported
    distinctly — ``started`` says which happened — because "it ran" and "it will
    run later" must not look the same to a caller coordinating several sessions.
    A queued delivery is re-validated at the drain: the entry
    carries the containment that held here, and a constraint newly held at
    delivery time drops it with a visible notice instead of executing it under
    the weaker authorization that admitted it. The entry also carries THIS
    caller's slot, so that drop appends a notice to the caller's own transcript
    as well: ``started: False`` says the message will run later, and a caller
    told only that would otherwise wait for a reply that can never come.

    ``steer`` asks for a THIRD outcome on a busy target: the message cuts into
    the turn already running (``steer_into_running_turn``) instead of waiting for
    it, so a caller watching a worker go the wrong way can say so while the work
    is still in flight rather than after it lands. Reported as ``steered``, its
    own field, for the same reason ``started`` is one.

    Two properties make that arm safe rather than a hole in the queued arm's
    checks. The delivery happening NOW removes the queue's waiting window and
    leaves a narrower one: the steer RPC suspends on ``stdin.drain()``.

    * Containment is re-validated at the queue drain because a queued prompt runs
      later than the moment it was authorized, so the target can gain a channel
      link or an outbound mirror while it waits. Nothing suspends between
      ``authorize_target`` and the steer call, so the text is committed against the
      containment the gate cleared — but the RPC's own suspension is a window, and
      each outcome closes it differently. The fallback re-runs the gate before
      taking the queue arm and refuses a target that resolves to a different slot
      OBJECT. A requeued steer becomes an ordinary queued entry and meets the drain
      unexempted. A SUCCESSFUL steer cannot be recalled, so the admission
      containment is compared against the post-RPC containment and the turn is
      stopped when a constraint newly holds — the reply path resolves its mirror
      live, so a mirror linked mid-turn is a real audience.
    * Provenance is carried by the text, not by the delivery mode: both arms hand
      over the same redacted, envelope-prefixed prompt, so neither the target
      agent nor a person reading the transcript can read an injected message as
      something the human typed. It also travels to the requeue as
      ``user_origin=False``, so a peer's steer does not inherit the composer's
      exemption from the drain's LINKED drop.

    A steer that cannot be injected falls back to the queue — never dropped, and
    the caller is told it queued rather than steered.

    The turn is NOT charged against the background-turn cap, and deliberately so:
    that cap only binds unattended (app-owned) slots, and every target this
    function can authorize is attended — see the comment at the delivery call.
    """
    # A broadcast passes its own instance so the timeout handler can read facts
    # this delivery recorded before cancellation erased its stack. A plain send
    # needs the same local state even though no outer timeout reads it.
    delivery_progress = (
        _delivery_progress if _delivery_progress is not None else _DeliveryProgress()
    )

    # The delivery appends to the target's rows: a target restored as a
    # sidebar row loads its window first, ahead of the prewarm below.
    await load_target_window(state, target)
    # Same prewarm ordering as `stop_target`, for the same reasons: the SEL
    # write inside `authorize_target`'s deny path must be a cache hit, and the
    # config warm must be the LAST suspension before the synchronous gate.
    try:
        await asyncio.to_thread(sel)
    except Exception:  # noqa: BLE001 - a prewarm failure must not fail the send
        logger.warning("session-control SEL prewarm failed", exc_info=True)
    await prewarm_enabled_check()

    body = message.strip()
    if not body:
        raise SessionControlError("message is empty", code="message_empty", status=400)
    if len(body) > MAX_SEND_MESSAGE_CHARS:
        raise SessionControlError(
            f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
            code="message_too_long",
            status=400,
        )

    slot = authorize_target(
        state,
        caller_session_key=caller_session_key,
        target=target,
        operation="send",
        precomputed_ownership_fenced=caller_fenced,
    )

    # A relay archive is read-only: the delivery below would run the crew's old
    # session on THIS machine, the same thing the send / regenerate / rewind /
    # continue paths refuse.
    if is_relay_archive(slot):
        raise SessionControlError(RELAY_ARCHIVE_ERROR, code=RELAY_ARCHIVE_CODE, status=409)

    # Cancellation can only enter at an await. Route every await below the
    # authorization through this helper so no authorized cancellation loses its
    # target-level permission trail.
    async def _await_authorized_delivery(awaitable: Any) -> Any:
        try:
            return await awaitable
        except asyncio.CancelledError:
            # `cancelled` means authorization was granted, delivery was cancelled
            # mid-flight, and whether the message landed is unknown.
            _audit(
                caller_session_key=caller_session_key,
                operation="send",
                slot_key=slot.key,
                outcome="cancelled",
                detail={
                    "steer_requested": bool(steer),
                    "chars": len(body),
                    "delivery_result": "unknown",
                },
            )
            raise

    # Deferred for the same import cycle `stop_target` documents.
    from kiro_crew.dashboard import session_deliver

    caller_key = caller_slot_key(state, caller_session_key)
    if caller_key and getattr(slot, "_created_by", "") == caller_key:
        # The live witness that this child's creator still drives it, so its
        # turn ends may report back (``dashboard.mate_thread_wakes``).
        slot._reports_to_creator = True
    # Sanitized on the same grounds as the steer path (``chat_delivery`` sanitizes
    # before ``slot.append``): this body comes from ANOTHER session and is persisted
    # into — and broadcast from — the target's transcript, so raw content must never
    # reach that surface. The length gate above deliberately measures the RAW body:
    # redaction can only shrink the text, so validating the raw form is the honest
    # limit and keeps the error keyed to what the caller actually sent.
    prompt = _SEND_PROVENANCE.format(
        caller=caller_key or "unknown", via=via or "session_send"
    ) + _neutralize_send_provenance(sanitize_outbound(body))

    steered = False
    requeued = False
    # Containment names that newly held when a successful steer's turn was stopped;
    # empty on every other path. Read by the audit below.
    steer_audience_changed: list[str] = []
    # True only when the containment stop below raised. The steer was already
    # consumed by the turn at that point, so the failure travels in the audit trail
    # and the caller still sees the delivery it got.
    steer_containment_stop_failed = False
    if steer and slot.running:
        # The mid-turn arm. ONE text is handed to both arms, so what the target
        # reads and what its transcript keeps are the same bytes either way:
        # already redacted, already carrying the provenance envelope, so an
        # injected steer can no more pose as human typing than a queued delivery
        # can. ``chat_delivery`` runs its own ``sanitize_outbound`` before it
        # appends the row, which is a second pass over text that already cleared
        # the same guard.
        #
        # Gated on ``slot.running`` because a steer needs a turn to cut into: the
        # turn publishes the steer-capable client and clears it at teardown, so on
        # an idle slot there is nothing to inject and the queue-or-run arm below is
        # the whole delivery.
        #
        # Captured BEFORE the RPC and synchronous with the gate above -- no await
        # separates `authorize_target` from here -- so this IS the containment the
        # authorization cleared, recorded in the drain's own vocabulary so the
        # comparison below is the same one queued prompts get.
        #
        # The sending slot rides the same dict because the REQUEUE copies it onto
        # the entry verbatim, so a steer that falls back to the queue and is then
        # dropped at the drain reports back to us like any queued delivery. Inert
        # for the two other readers: `newly_held_constraints` reads only the
        # containment key, and so does the audience fence below.
        admission = {**containment_meta(state, slot), **send_origin_meta(state, caller_key)}

        # Which turn this steer is going into. `_turn_generation` increments on every
        # task assignment, so it identifies a turn even if a later task object reuses
        # an address. Captured here, synchronously with the gate, because the stop
        # below must not cancel a DIFFERENT turn: if the steered turn ends during the
        # RPC and a queued prompt starts the next one, `slot` still reads as running
        # and an unguarded stop would cancel work that never received this text.
        steered_turn_generation = slot._turn_generation

        # Set BEFORE the RPC, synchronously with the gate above, because this is the
        # half the post-RPC stop cannot cover: `_deliver_cross_surface_reply` runs
        # from the turn's own completion path and resolves the mirror live, so a turn
        # that finishes while this coroutine is suspended would publish to the new
        # audience before any check of ours resumes. Reacting cannot recall a sent
        # reply; withholding until the check has run can.
        audience_fence = uuid.uuid4().hex
        slot._steer_audience_fences[audience_fence] = admission

        # `user_origin=False`: this text was not typed into the target's own
        # surface. The requeue reads it to decide `directive_user_origin`, and a
        # peer must not inherit the composer's exemption from the LINKED drop.
        async def _steer_delivery() -> str:
            return await session_deliver.deliver(
                state, slot, prompt, session_deliver.peer(steer=True), admission=admission
            )

        delivery_progress.steer_await_entered = True

        # Shielded, because THIS await is what `broadcast_to_targets` cancels when a
        # target overruns `BROADCAST_TARGET_ALLOWANCE_SECS`, and
        # `steer_into_running_turn` guards its own `client.steer` with
        # `except Exception` -- which does not catch `CancelledError`. Awaited
        # directly, the cancellation unwinds THROUGH the RPC and skips that
        # coroutine's single reconciliation tail, while the bytes may already have
        # reached kiro-cli: the turn then runs text no `slot.append` recorded, and
        # `_steer_delivery_ids` / `_steer_send_ids` / `_steer_user_origin` /
        # `_steer_admissions` are never popped. Nothing else pops them --
        # `_settle_consumed_steers` clears only the attachment and decision-strip
        # maps, and `_requeue_unconsumed_steers` returns early once settling emptied
        # `_pending_steers`. The surviving `_steer_delivery_ids` entry then refuses
        # this exact text on that slot forever (the one-per-text guard reads that
        # dict) and `retained_steer_count` never falls back below
        # `MAX_PENDING_STEERS`, after which the slot refuses every steer.
        #
        # The shield splits the two halves the cancellation conflated: our frame
        # still receives it, so the broadcast reports its timeout row on schedule
        # and the caller still hears "outcome unknown", while the delivery runs to
        # its own end and reconciles itself. Inert on the direct `session_send`
        # path, which has no per-target budget above it.

        # The fallback a CANCELLED frame cannot reach. `STEER_UNAVAILABLE` means no
        # live steer-capable client, an RPC that lost the text, or an identical steer
        # already in flight -- in every case the text was never handed over and the
        # shielded delivery has cleared its own per-text state, so nothing downstream
        # holds it. The arm below queues it instead of dropping it; once the frame is
        # gone that arm cannot run, and shielding the delivery
        # without this would turn an outcome the caller recovers from into a lost
        # instruction. Re-gated on the same terms as that arm, because the RPC
        # suspended and the queue entry records the containment for the drain to
        # re-assert: a snapshot taken now must not certify a link the authorization
        # never saw.
        async def _queue_after_orphaned_unavailable() -> None:
            await prewarm_enabled_check()
            regated = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="send",
                precomputed_ownership_fenced=caller_fenced,
            )
            if regated is not slot:
                # Object identity, for the reason the in-frame arm gives: a target
                # closed and resumed under the same key is a different object whose
                # key still compares equal, and queueing onto the detached one loses
                # the text as surely as dropping it.
                logger.warning(
                    "session_send: orphaned steer to %s came back unavailable, but the "
                    "target resolved to a different session; the message is not queued",
                    slot.key,
                )
                return
            await session_deliver.deliver(
                state,
                slot,
                prompt,
                session_deliver.peer(steer=False),
                queue_meta=send_origin_meta(state, caller_key),
            )
            logger.info(
                "session_send: orphaned steer to %s came back unavailable and was "
                "queued instead of lost",
                slot.key,
            )

        _steer_task = asyncio.ensure_future(_steer_delivery())
        try:
            outcome = await _await_authorized_delivery(asyncio.shield(_steer_task))
        except asyncio.CancelledError:
            _retain_orphaned_steer_delivery(
                _steer_task,
                slot.key,
                unavailable_outcome=session_deliver.NOT_DELIVERED,
                on_unavailable=_queue_after_orphaned_unavailable,
            )
            raise
        steered = outcome == session_deliver.DELIVERED_STEERED
        # The turn ended while the steer RPC was suspended and its teardown moved
        # the text onto the queue: it WILL run, and taking the queue arm below
        # would deliver it a second time. Reported as a queued delivery, which is
        # what it now is.
        requeued = outcome == session_deliver.DELIVERED_REQUEUED
        if requeued and state._slots.get(slot.key) is not slot:
            # The teardown queued this text on THIS slot object, and the key has
            # since stopped resolving to it -- the target was closed and recreated
            # while the RPC was suspended on `stdin.drain()`. The queue that now
            # holds the prompt belongs to a detached object no drain will ever
            # reach, so the text is gone. Refusing is the only honest answer: the
            # `steered` arm already guards this same replacement (it records
            # `slot_replaced` below) and the fallback arm re-runs the gate, so this
            # was the one outcome that reported success for a prompt that had
            # quietly stopped existing.
            #
            # Object identity, not `key != key`: a fresh object under the same key
            # compares equal by key and would pass a key comparison.
            raise SessionControlError(
                "that target was replaced while the steer was in flight and the "
                "message did not survive the hand-over; re-read the session list "
                "and send again",
                code="target_moved",
                status=409,
            )
        if steered:
            # The text is IN the running turn: `STEER_STEERED` means kiro-cli
            # acknowledged consumption, so unlike the requeue arm there is nothing
            # left to remove and unlike the fallback arm there is nothing left to
            # refuse. What can still be prevented is the turn's remaining output
            # reaching an audience this send was never authorized against -- a
            # mirror linked, retargeted, or a channel link bound while the RPC was
            # suspended on `stdin.drain()`. `_deliver_cross_surface_reply` resolves
            # the mirror LIVE at reply-delivery time, so that audience is real and
            # not fixed at turn start.
            #
            # So: compare, and stop the turn when a constraint newly holds. One
            # cooperative cancel, the same thing the Stop button's first press does,
            # with `escalate=False` because this is one automatic decision and not a
            # person who watched a cancel fail to take.
            #
            # This comparison does NOT decide whether the reply may be published. The
            # admission recorded before the RPC stays on the slot for the whole turn,
            # and the publisher re-evaluates it at delivery -- a mirror bound between
            # this moment and the reply would be just as unauthorized, and only the
            # publisher's own moment can see it.
            steer_audience_changed = newly_held_constraints(
                containment_snapshot(state, slot, on_probe_failure=True), admission
            )
            if state._slots.get(slot.key) is not slot:
                # The slot object was replaced under the same name while the RPC was
                # in flight, so the turn holding this text belongs to a session that
                # the name has stopped resolving to. Reported alongside the containment
                # names rather than folded into them: it is an identity change, not
                # a constraint that newly holds.
                steer_audience_changed = [*steer_audience_changed, "slot_replaced"]
            if steer_audience_changed:
                # Deferred import for the cycle the steer import above documents.
                from kiro_crew.dashboard.chat_handlers import stop_slot_turn

                if slot._turn_generation != steered_turn_generation:
                    # The turn this text entered has already ended and another one
                    # has started on the same slot. Stopping now would cancel work
                    # that never received this steer, which is worse than the
                    # exposure being narrowed: the steered turn is over, so whatever
                    # it published is already published, and the fence recorded
                    # before the RPC still withholds the cross-surface legs for the
                    # remainder of the turn that holds it.
                    steer_audience_changed = [
                        *steer_audience_changed,
                        "turn_rolled_over_stop_skipped",
                    ]
                else:
                    # The text is already consumed by the turn at this point, so this
                    # stop is a best-effort narrowing of what the turn goes on to
                    # publish -- it is NOT part of delivering the message. Letting it
                    # raise would abort before the success audit and the ok:True
                    # return below, reporting a steer the turn has already taken as
                    # failed; the caller's natural response is to send again, which
                    # delivers the same text twice. The turn's own reply legs consult
                    # the audience fence recorded before the RPC, so containment does
                    # not depend on this stop succeeding.
                    try:
                        await _await_authorized_delivery(
                            stop_slot_turn(
                                state,
                                slot,
                                source="session_send_steer_containment",
                                escalate=False,
                            )
                        )
                    except Exception:
                        logger.exception(
                            "Containment stop failed after a steer was consumed "
                            "(slot=%s, newly held=%s); the fence still withholds "
                            "cross-surface publication",
                            slot.key,
                            steer_audience_changed,
                        )
                        steer_containment_stop_failed = True
        else:
            # Not steered: no text of ours is inside the running turn on this path, so
            # there is nothing for the record to protect -- the requeue arm's entry
            # faces the drain and the fallback arm re-runs the gate. Dropped by key so
            # another peer's concurrent steer keeps its own.
            slot._steer_audience_fences.pop(audience_fence, None)
        if not steered and not requeued:
            # STEER_UNAVAILABLE: no live steer-capable client, an RPC that lost the
            # text, or an identical steer already in flight. The message falls back
            # to the queue arm rather than being dropped — but that arm records the
            # containment holding at APPEND time for the drain to re-assert, and
            # the RPC above suspends, so the state it would record can differ from
            # the state this send was authorized against. Re-run the gate so the
            # snapshot cannot certify a link the authorization never saw.
            # The prewarm before the gate at the top of this function predates the
            # steer RPC, which suspends: a config edit during that suspension
            # invalidates the cache, and `authorize_target` -> `session_control_enabled`
            # would then run `KiroCrewConfig.load()` synchronously on the shared loop,
            # stalling every other session while a file is read and validated. Warming
            # again here is what `prewarm_enabled_check`'s own docstring prescribes for
            # a gate that sits after an await.
            async def _prewarm_regate() -> None:
                await prewarm_enabled_check()

            await _await_authorized_delivery(_prewarm_regate())
            regated = authorize_target(
                state,
                caller_session_key=caller_session_key,
                target=target,
                operation="send",
                precomputed_ownership_fenced=caller_fenced,
            )
            if regated is not slot:
                # Object identity, not ``regated.key != slot.key``. The delivery
                # below uses THIS ``slot`` object, so a target closed and resumed
                # under the same key during the RPC yields a fresh object whose key
                # still compares equal: the guard would pass, the message would land
                # on the detached object, and the caller would be told it succeeded.
                raise SessionControlError(
                    "that target resolved to a different session while the steer "
                    "was in flight; re-read the session list and send again",
                    code="target_moved",
                    status=409,
                )

    if steered or requeued:
        # Neither arm starts a turn: a steer runs inside one that is already going,
        # and a requeued steer waits for the next like any queued message.
        started = False
    else:
        # `deliver` passes `_run_chat` straight through, NOT wrapped in
        # `state.run_background_turn`: that cap is structurally unreachable here.
        # `run_background_turn` returns the coroutine untouched for an attended slot
        # (`state_parts/dashboard_services.py`, "this wrapper is inert"), `_ChatSlot.unattended` is
        # `bool(self._app) and not self._human_seen`, and `authorize_target` refuses
        # every `_app` target above (`app_scoped_target`) — so no target this
        # function can reach is ever unattended, and a wrapper would only add a
        # never-taken timeout arm. The composer's own queued path does the same
        # (`server.py` passes `_run_chat` directly).
        # No suspension inside: the non-steer peer arm is the synchronous
        # queue-or-run call, so no cancellation can enter this await.
        started = (
            await session_deliver.deliver(
                state,
                slot,
                prompt,
                session_deliver.peer(steer=False),
                # Only the QUEUE arm keeps it (the run arm has no entry): a
                # delivery that waits is the one a later drain can drop, and this
                # stamp is what lets that drop be reported back to us instead of
                # ending at the target's own transcript.
                queue_meta=send_origin_meta(state, caller_key),
            )
            == session_deliver.DELIVERED_TURN
        )
        if started:
            _start_auto_tag(state, slot)
    try:
        state.push_slots_update()
    except Exception:  # pragma: no cover - sidebar refresh is best-effort
        logger.debug("session_send: push_slots_update failed", exc_info=True)

    _audit(
        caller_session_key=caller_session_key,
        operation="send",
        slot_key=slot.key,
        outcome="allowed",
        # `steer` is what the caller ASKED for and `steered` is what happened, so a
        # fallback to the queue is readable in the trail rather than looking like a
        # caller that never asked.
        detail={
            "started": started,
            "steer_requested": bool(steer),
            "steered": steered,
            "chars": len(body),
            # Only when a steer's turn was actually stopped, so the common shape is
            # unchanged. Names the constraints that newly held, which is what a
            # reader needs to tell this apart from an ordinary delivery: the message
            # WAS delivered into the turn and the turn was then cancelled because
            # its audience had changed under the RPC.
            **({"steer_stopped_on": steer_audience_changed} if steer_audience_changed else {}),
            # The stop raised. Present only in that case, so its absence is not a
            # claim that a stop succeeded on a path where none was attempted. The
            # delivery outcome above is unchanged: the turn had already taken the
            # text, and the audience fence recorded before the RPC still withholds
            # cross-surface publication whether or not the stop took.
            **({"steer_containment_stop_failed": True} if steer_containment_stop_failed else {}),
        },
    )
    return {"ok": True, "target": slot.key, "started": started, "steered": steered}


#: The two broadcast modes, named rather than a boolean. ``queue`` waits for each
#: target's current turn; ``steer`` cuts into it. They are separate names because
#: the choice is not a refinement of one delivery — "tell everyone when they next
#: come up for air" and "interrupt everyone now" are different instructions, and a
#: caller that means the first must not reach the second by defaulting.
BROADCAST_MODES = ("queue", "steer")


#: The per-delivery bound this module ENFORCES, and the MCP client's one request
#: budget, are one name in ``kiro_crew.validation`` rather than two literals that
#: must be remembered together -- the same reason ``MAX_BROADCAST_TARGETS`` lives
#: there. See that definition for why a client budget below this bound would
#: discard the per-target report.

#: The refusal code a timed-out delivery reports. Distinct from
#: ``delivery_failed`` because a timeout is classified from delivery-owned
#: progress plus any exact text the target still retains: pre-authorization,
#: retained text, ambiguous or completed hand-over, and an unavailable target
#: each need different caller guidance. See :func:`broadcast_to_targets`.
BROADCAST_TIMEOUT_CODE = "delivery_timeout"


_TIMEOUT_RETAINED = "retained"


_TIMEOUT_NOT_HANDED_OVER = "not_handed_over"


_TIMEOUT_UNKNOWN = "unknown"


def _timeout_delivery_observation(
    state: "DashboardState",
    slot: Any | None,
    prompt: str,
    delivery_progress: _DeliveryProgress,
) -> str:
    """Classify a cancelled delivery from its own progress and retained text."""
    if slot is None or state._slots.get(slot.key) is not slot:
        return _TIMEOUT_UNKNOWN
    if prompt in slot._pending_steers or any(
        isinstance(entry, dict) and entry.get("content") == prompt for entry in slot._queue
    ):
        return _TIMEOUT_RETAINED
    if delivery_progress.steer_await_entered:
        return _TIMEOUT_UNKNOWN
    return _TIMEOUT_NOT_HANDED_OVER


def broadcast_audience(state: "DashboardState", caller_key: str) -> list[str]:
    """The slot keys *caller_key* manages and can still address, sorted.

    The DEFAULT audience of a broadcast: a conductor's own workers, the ones it
    created and the ones handed to it. Read from :func:`session_managers.holder`
    (the creator until a ``session_handoff`` names another) -- the manager the
    fence reads for ``send`` -- so the default audience is by construction
    a subset of what the fence would admit, and an audience this returns can still
    be refused per target (a worker that went incognito, gained a channel mirror,
    or is an app's) by the gate each delivery runs. Nothing here authorizes.

    The caller itself is excluded: ``authorize_target`` refuses a self-target, and
    silently collecting a refusal the caller could not have avoided would make
    every broadcast report one failure.

    Sorted by key so the delivery order is stable across calls. A broadcast is not
    atomic — deliveries land one at a time — so a caller reading two reports needs
    the same order in both to compare them.
    """
    out = [
        key
        for key, slot in list(state._slots.items())
        if key != caller_key and session_managers.holder(slot) == caller_key
    ]
    return sorted(out)


def _too_many_targets(count: int) -> SessionControlError:
    """The audience-cap refusal, from one place because it is raised from two.

    Refused rather than truncated. A silently-cut audience is a broadcast the
    caller believes reached everyone, and the sessions past the cut are the ones it
    will never think to check.
    """
    return SessionControlError(
        f"a broadcast reaches at most {MAX_BROADCAST_TARGETS} sessions and this "
        f"one names {count}; send to a named subset instead",
        code="too_many_targets",
        status=400,
    )


async def broadcast_to_targets(
    state: "DashboardState",
    *,
    caller_session_key: str,
    message: str,
    mode: str,
    targets: "list[str] | None" = None,
    caller_fenced: bool | None = None,
) -> dict[str, Any]:
    """Deliver *message* to several sessions at once, one at a time.

    Two modes, and they are the whole point of the verb being separate from a loop
    of :func:`send_to_target` calls the model writes itself: ``queue`` lets every
    target finish what it is doing, ``steer`` cuts into every running turn. A
    conductor telling eight workers "the base moved, rebase before you push" wants
    the first; one telling them "stop, the issue was already fixed" wants the
    second, and the difference is worth eight turns of latency.

    ``targets`` omitted means :func:`broadcast_audience` — the sessions this caller
    created. Naming them explicitly is for the subset case, and every named target
    goes through the same :func:`authorize_target` gate, so an explicit list can
    only ever be as permissive as one send at a time.

    PARTIAL DELIVERY IS THE NORMAL OUTCOME and the return shape says so per
    target. A target closed between the audience read and its own delivery, one
    that went incognito, one bound to a remote crew: each is one row with its
    refusal ``code``, and the remaining deliveries still happen. The alternative —
    abort on the first refusal — would leave a broadcast half-delivered with
    nothing saying which half, which is the one outcome a caller cannot recover
    from.

    Deliveries are SEQUENTIAL, never gathered. Each one takes the same slot locks,
    runs the same containment re-checks and (for a steer) suspends on an RPC, and
    running them concurrently would interleave those windows across sessions for
    no gain a caller can observe — the report is only read once all of them are
    done either way.

    Which is exactly why each delivery carries its own bound
    (``BROADCAST_TARGET_ALLOWANCE_SECS``): sequential delivery means one target
    that never answers is one target that starves every target behind it, and the
    urgent mode is the one that suspends on an RPC with no ceiling under it. On
    expiry that delivery is cancelled, reported as a ``delivery_timeout`` row, and
    the loop continues. The delivery records ONE marker: that it entered the steer
    await. Cancellation before that marker is provably before hand-over, so
    re-sending is safe. Cancellation once it is set is ambiguous -- inside the
    await or after it returned are indistinguishable here, and a second marker for
    the return would not separate them, because it could only be set once this one
    already was. Both read as unknown and must not invite a retry. The target's
    ``_pending_steers`` and queue
    still add useful information when they retain the exact text, but their shared
    absence cannot reconstruct delivery history. A target whose slot disappeared
    or was replaced also gets an unknown row. None of the rows claims delivery or
    certain execution.
    """
    if mode not in BROADCAST_MODES:
        raise SessionControlError(
            f"mode must be one of {', '.join(BROADCAST_MODES)}",
            code="invalid_broadcast_mode",
            status=400,
        )
    body = message.strip()
    if not body:
        raise SessionControlError("message is empty", code="message_empty", status=400)
    if len(body) > MAX_SEND_MESSAGE_CHARS:
        raise SessionControlError(
            f"message exceeds {MAX_SEND_MESSAGE_CHARS} characters",
            code="message_too_long",
            status=400,
        )

    # The caller gate runs ONCE, here, before any audience is read: a caller that
    # may not control sessions at all must be refused without learning how many it
    # created. The per-target gate below re-runs it for each delivery, which is
    # what actually binds every send — this one is the early, cheap refusal.
    await prewarm_enabled_check()
    deny = _deny_factory(caller_session_key=caller_session_key, operation="broadcast", target="")
    caller_key = refuse_caller_identity(state, caller_session_key=caller_session_key, deny=deny)
    caller_slot = refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    # Captured for the re-check before the return below, on the same terms
    # `created_session_status` captures it: this verb suspends many times between
    # here and its payload, and the rows it returns name the caller's own sessions.
    caller_workspace = str(getattr(caller_slot, "workspace", "default"))
    ownership_fenced = (
        _caller_is_ownership_fenced(state, caller_key) if caller_fenced is None else caller_fenced
    )
    resolution_slots = _broadcast_resolution_slots(
        state,
        caller_key=caller_key,
        caller_slot=caller_slot,
        ownership_fenced=ownership_fenced,
    )

    explicit = targets is not None
    if explicit:
        # THE CAP, CHECKED BEFORE ANYTHING RESOLVES. Below the resolution loop this
        # check is correct and useless: reaching it costs one `_resolve_slot` per
        # name -- a copy of the authorized-slot set and two full passes over it,
        # with nothing awaited between them -- so an oversized list does the work
        # the refusal exists to prevent and is refused afterwards. Here the cost of
        # an oversized list is one integer comparison.
        #
        # Counted on the caller's RAW list rather than on the deduplicated audience,
        # so the bound is on what the caller submitted. A list of `cap + 1`
        # duplicate spellings of one session is therefore refused even though it
        # would collapse under the cap, and that is the intended reading: the cap
        # bounds the work this call asks for, and deduplication is that work.
        #
        # The route bounds this too, and both are deliberate: the route is the
        # boundary an in-sandbox shell reaches, this is the boundary every
        # in-process caller reaches.
        if len(targets or []) > MAX_BROADCAST_TARGETS:
            raise _too_many_targets(len(targets or []))
        # De-duplicated by an AUTHORIZED resolved slot while PRESERVING the
        # caller's order, so two spellings of one session still deliver once.
        # Resolution is restricted to slots the caller may address; a private
        # title therefore remains the caller's own string and cannot turn into a
        # slot key in the refusal row. This loses no real target because
        # ``authorize_target`` would refuse every slot outside this set anyway,
        # and every retained key still goes through that gate at delivery.
        # A name that cannot be resolved, or is ambiguous within the authorized
        # set, remains in the audience so the gate can return its refusal row.
        seen: set[tuple[str, str]] = set()
        audience: list[str] = []
        for raw in targets or []:
            name = str(raw).strip()
            if not name:
                continue
            try:
                slot = _resolve_slot(state, name, candidates=resolution_slots)
            except SessionControlError:
                slot = None
            identity = ("slot", slot.key) if slot is not None else ("unresolved", name)
            if identity not in seen:
                seen.add(identity)
                audience.append(slot.key if slot is not None else name)
        if not audience:
            # The caller named targets and not one of them is usable. Both silent
            # answers are wrong here: falling back to the default audience turns a
            # malformed argument into an accidental fleet-wide send, and reporting
            # "delivered 0 of 0" hides a call that never had a chance of working.
            raise SessionControlError(
                "targets was given but names no session; omit it to reach every "
                "session you created",
                code="target_required",
                status=400,
            )
    else:
        audience = broadcast_audience(state, caller_key)

    if len(audience) > MAX_BROADCAST_TARGETS:
        # The DEFAULT audience's own bound. An explicit list was already refused
        # above, before resolution, and deduplication only ever shrinks it -- so
        # this now guards the fence-built audience, whose size the caller did not
        # choose. It should be unreachable: ``broadcast_audience`` returns the live
        # slots this caller created, which ``MAX_SLOTS_PER_CREATOR`` bounds at or
        # below this cap (see ``MAX_BROADCAST_TARGETS``). Kept because "unreachable
        # because two constants are ordered correctly" is a property a later edit
        # can break, and truncating a fleet-wide send is the failure that must not
        # be the fallback.
        raise _too_many_targets(len(audience))

    results: list[dict[str, Any]] = []
    timeout_prompt = _SEND_PROVENANCE.format(
        caller=caller_key or "unknown", via=BROADCAST_VIA
    ) + _neutralize_send_provenance(sanitize_outbound(body))
    for name in audience:
        try:
            timeout_slot = _resolve_slot(state, name)
        except SessionControlError:
            timeout_slot = None
        delivery_progress = _DeliveryProgress()
        try:
            # Each delivery gets its OWN bound. Without one a single unresponsive
            # target starves every target behind it: the steer arm suspends on
            # `client.steer` -> `stdin.drain()` with no `wait_for` anywhere beneath
            # it, so a session whose process is wedged blocks this loop
            # indefinitely and targets 3-8 of a "stop, the issue was already fixed"
            # never hear it. The client's request budget then expires and the
            # caller is told the whole broadcast failed, discarding the per-target
            # report that is the only thing that could have said which half landed.
            # Bounding here and continuing turns that into one refused row.
            #
            # The bound wraps the WHOLE call rather than the steer RPC alone, which
            # is what gives the queue arm the same protection: that arm also
            # suspends before it delivers (`asyncio.to_thread(sel)`,
            # `prewarm_enabled_check`, and the config load that can sit behind it),
            # and a queue broadcast stalled on a config read starves the fleet just
            # as thoroughly as a wedged steer does.
            #
            # The bound wraps two pre-authorization awaits as well as the
            # delivery arms. A cancel at `asyncio.to_thread(sel)` or
            # `prewarm_enabled_check()` happens before `authorize_target`, so it
            # cannot leave this text pending or queued. A cancel at the steer RPC
            # can leave the exact prompt in `_pending_steers`, and turn teardown can
            # move it into a queue entry's `content` while cancellation unwinds.
            #
            # Capture the resolved slot and a delivery-owned progress marker before
            # the call. After cancellation, retained text still proves it may run,
            # but empty target containers prove nothing: consumption and a send that
            # never started both empty them. The progress marker distinguishes a
            # pre-authorization cancellation from the ambiguous steer await and every
            # post-return await. If the object disappeared or was replaced, the
            # outcome is unknown regardless.
            #
            # `send_to_target` records its `cancelled` audit only after
            # authorization. Cancellation at either earlier await therefore leaves
            # no target-level permission row, while cancellation in an authorized
            # delivery keeps that row unchanged.
            sent = await asyncio.wait_for(
                send_to_target(
                    state,
                    caller_session_key=caller_session_key,
                    target=name,
                    message=body,
                    steer=(mode == "steer"),
                    caller_fenced=caller_fenced,
                    via=BROADCAST_VIA,
                    _delivery_progress=delivery_progress,
                ),
                timeout=BROADCAST_TARGET_ALLOWANCE_SECS,
            )
        except asyncio.TimeoutError:
            # A timeout says only that this side stopped waiting. Delivery-owned
            # progress decides whether retry advice is safe; target containers only
            # add the stronger fact that an exact retained copy may still run.
            observation = _timeout_delivery_observation(
                state, timeout_slot, timeout_prompt, delivery_progress
            )
            if observation == _TIMEOUT_RETAINED:
                error = (
                    f"this target did not finish its delivery within "
                    f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                    "stopped waiting and moved on; whether the message has run is "
                    "UNKNOWN. The same text is pending or queued on that target and "
                    "may still run. Do NOT re-send it: a duplicate could run too"
                )
            elif observation == _TIMEOUT_NOT_HANDED_OVER:
                error = (
                    f"this target did not finish its delivery within "
                    f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                    "stopped waiting and moved on; the target has neither a pending "
                    "steer nor a queued copy, so the delivery did not reach the "
                    "hand-over. Re-sending the same text is safe"
                )
            else:
                if timeout_slot is not None and state._slots.get(timeout_slot.key) is timeout_slot:
                    error = (
                        f"this target did not finish its delivery within "
                        f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                        "stopped waiting and moved on; cancellation reached the steer "
                        "await or a later await, so whether the message has run is "
                        "UNKNOWN. Re-sending could execute it twice"
                    )
                else:
                    error = (
                        f"this target did not finish its delivery within "
                        f"{BROADCAST_TARGET_ALLOWANCE_SECS:.3g}s, so the broadcast "
                        "stopped waiting and moved on; the target is unavailable, so "
                        "whether the message has run or is pending or queued is UNKNOWN"
                    )
            logger.warning(
                "session_broadcast: delivery to %s exceeded %.3gs and was "
                "cancelled mid-flight; post-cancel observation=%s",
                name,
                BROADCAST_TARGET_ALLOWANCE_SECS,
                observation,
            )
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": BROADCAST_TIMEOUT_CODE,
                    "error": error,
                }
            )
            continue
        except SessionControlError as exc:
            # Collected, not raised. This target is out of reach; the rest are not,
            # and the caller needs to know which is which. The refusal was already
            # audited by the gate that produced it.
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": exc.code,
                    "error": exc.message,
                }
            )
            continue
        except Exception:
            # An unexpected failure on ONE delivery must not take the broadcast
            # down with it, and it must not be reported as a refusal either: a
            # refusal has a code the caller can act on, this has none. Logged with
            # its traceback and reported as the internal failure it is.
            logger.exception("session_broadcast: delivery to %s failed", name)
            results.append(
                {
                    "target": name,
                    "ok": False,
                    "code": "delivery_failed",
                    "error": "delivery failed unexpectedly; see the gateway log",
                }
            )
            continue
        results.append(
            {
                "target": sent.get("target", name),
                "ok": True,
                "started": bool(sent.get("started")),
                "steered": bool(sent.get("steered")),
            }
        )

    delivered = sum(1 for row in results if row.get("ok"))
    _audit(
        caller_session_key=caller_session_key,
        operation="broadcast",
        slot_key=caller_key,
        outcome="allowed",
        detail={
            "mode": mode,
            # WHERE the audience came from, because the two are different acts: an
            # explicit list is a caller naming sessions, the default is the fence's
            # own set. A trail that cannot tell them apart cannot answer "who chose
            # these targets".
            "audience": "explicit" if explicit else "created",
            "requested": len(audience),
            "delivered": delivered,
            "chars": len(body),
        },
    )
    # The entry gate RAN, but its verdict does not survive this verb's suspensions,
    # and every `results` row below names one of the caller's own sessions. A channel
    # mirror can be bound onto an already-open dashboard session while a delivery is
    # awaiting -- the channel picker (`messaging/session_resume.py`) and the Slack
    # link route (`slack/interactions.py`) both do it with no idle-slot requirement --
    # so a payload built under the entry gate would reach that channel's audience
    # carrying private session keys. Same re-check, same audited path, as
    # `created_session_status` does after its scan; workspace is compared for the
    # same reason (it IS reassigned on a live slot, while a live key is never rebound
    # to a new object, so a slot-identity check could not fire).
    #
    # Raised AFTER the allowed audit above, deliberately: the deliveries were each
    # individually gated and have already happened, so the trail must record them.
    # What is refused is the report, which is why the message says so -- a caller
    # that reads this as "the broadcast failed" and sends again delivers twice.
    refuse_caller_surface(state, caller_key=caller_key, deny=deny)
    if str(getattr(caller_slot, "workspace", "default")) != caller_workspace:
        raise deny(
            "the calling session moved workspace while this broadcast was in flight, "
            "so the per-target report is withheld. The deliveries already happened -- "
            "do NOT send the same text again; read the targets' own transcripts",
            "caller_changed_mid_broadcast",
        )
    return {
        "ok": True,
        "mode": mode,
        "requested": len(audience),
        "delivered": delivered,
        # True only for the DEFAULT audience: the caller has created nothing that
        # is still open, which is a real state and not an error. An explicit list
        # that names nothing is refused before delivery, so it cannot reach here.
        **({"audience_empty": True} if not audience and not explicit else {}),
        "results": results,
    }
