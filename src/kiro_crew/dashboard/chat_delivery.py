"""Delivery of a user message into a chat slot, independent of the caller.

The interesting cases are all in the bookkeeping: a mid-turn steer has to be
registered before the RPC suspends, the transcript segment has to be cut at the
steer boundary, and a steer the live client refuses must fall through to the
queue rather than vanish. Held here rather than inside the route handler so the
bookkeeping can be tested against a slot directly, without an HTTP request, and
so a second delivery caller inherits it rather than growing a second copy that
drifts until a message is silently dropped.

The helpers own that bookkeeping and nothing else — no HTTP, no request parsing,
no response shaping — so a caller layers its own authorization and response
format on top.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from kiro_crew.dashboard.chat_utils import _redact_for_display
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger(__name__)

# Outcomes of :func:`steer_into_running_turn`.
#
# The two middle values split the case where the optimistic registration
# vanished during the steer RPC. Both mean "do NOT queue it again", but they are
# opposite answers to "did the message survive": the turn's teardown REQUEUED it
# (it is in the slot's queue and will run). A hard stop clears both the queue and
# the pending-steer list, so a discarded message has no outcome to report -- the
# refusals that cover that case raise directly rather than returning a code.
STEER_STEERED = "steered"
STEER_REQUEUED = "requeued"
STEER_UNAVAILABLE = "unavailable"


def sanitize_outbound(text: str) -> str:
    """Return *text* with credentials and exfiltration URLs stripped.

    The single sanitization chain every delivery path uses before a message is
    persisted or broadcast: raw content must never reach an external surface.
    """
    sanitized, _ = redact_exfiltration_urls(text)
    sanitized, _ = redact_credentials(sanitized)
    return sanitized


def _row_has_delivery_id(slot: Any, delivery_id: str) -> bool:
    """Whether a durable row already carries *delivery_id* in its meta.

    The drain unions every consumed queue entry's meta onto the row it appends, so
    this is true exactly when the requeue-then-drain path already persisted this
    delivery — including when the row merged several queued messages together, where
    no content comparison would match.
    """
    for m in reversed(slot.messages):
        meta = m.get("meta")
        if not isinstance(meta, dict):
            continue
        if meta.get("steer_delivery_id") == delivery_id:
            return True
        # A merged row names every delivery it stands for, so membership — not
        # equality — is the question once the drain has folded messages together.
        many = meta.get("steer_delivery_ids")
        if isinstance(many, list) and delivery_id in many:
            return True
    return False


def _queue_has_delivery_id(slot: Any, delivery_id: str) -> bool:
    """Whether a QUEUE entry carries *delivery_id* in its meta.

    True exactly when the turn's teardown requeued THIS steer: the requeue moves
    the id out of `_steer_delivery_ids` and into the new queue entry's meta.

    Identity rather than content, because a content count cannot tell this steer's
    requeue apart from an unrelated client queueing the same text in the same
    window -- and reading that as "mine was requeued" drops the transcript row for
    a steer the turn actually consumed.
    """
    for item in slot._queue:
        meta = item.get("meta")
        if isinstance(meta, dict) and meta.get("steer_delivery_id") == delivery_id:
            return True
    return False


def _log_stop_race(slot: Any, stop_gen: int, *, preserved: bool) -> None:
    """Record a steer that raced a stop, and which way it resolved."""
    logger.info(
        "steer for slot %s raced a stop (generation %d -> %d); message %s",
        slot.key,
        stop_gen,
        int(getattr(slot, "_stop_generation", 0) or 0),
        "preserved" if preserved else "discarded",
    )


async def steer_into_running_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
) -> str:
    """Inject *message* into the slot's RUNNING turn; return a ``STEER_*`` outcome.

    Requires a live, steer-capable inner ACP client that the turn published on
    the slot. Fire-and-forget by design: the inline steer card materializes when
    kiro-cli echoes ``steering_consumed``.
    """
    client = getattr(slot, "_acp_client", None)
    if client is None or not getattr(client, "supports_steer", False):
        return STEER_UNAVAILABLE

    # Register as pending BEFORE the await: ``steer()`` suspends on
    # ``stdin.drain()``, and if the turn's finally runs during that suspension
    # it must already see this steer to requeue it (an append after the await
    # would land on an idle slot and orphan the message). The force-stop
    # ``clear()`` races correctly for the same reason: a hard kill during the
    # await discards the entry, so a late write cannot resurrect it.
    # Captured BEFORE the await. ``_stop_generation`` counts stop INITIATIONS and
    # is never reset by turn teardown, so it detects a Stop that fired AND
    # resolved while ``steer()`` was suspended — re-reading ``_stop_state`` after
    # the await would miss exactly that window.
    stop_gen = int(getattr(slot, "_stop_generation", 0) or 0)

    # AT MOST ONE pending steer per distinct text, enforced here at entry.
    #
    # `_pending_steers` holds plain strings and every consumer of it matches by
    # CONTENT — the turn teardown requeues by content, the queue comparison below
    # matches by content. So with two identical entries in flight, no amount of
    # counting downstream can say WHOSE entry survived: if another caller's copy is
    # consumed while ours is refused, the count falls back exactly as it would if
    # ours had gone, and we would persist a refused message as delivered and then
    # let the teardown requeue it — the same text twice.
    #
    # Rather than try to resolve an ambiguous signal, remove the ambiguity: refuse
    # the second identical steer. Nothing is lost, because `STEER_UNAVAILABLE`
    # sends the caller down the queue path. And since a concurrent caller hits this
    # same guard, once our entry is appended no further identical entry can appear,
    # which is what makes every check after the await unambiguously about ours.
    # "In flight" is BOTH markers, not just the pending list. A steer whose pending
    # entry has already been consumed by the running turn is still in flight: it is
    # still awaiting and still owns an entry in `_steer_delivery_ids`. Consulting
    # only `_pending_steers` therefore lets a second identical steer through at
    # exactly that moment, and its `_steer_delivery_ids[message] = ...` overwrites
    # the first caller's live id -- after which reconciliation removes the second's
    # id and the first's row can persist twice. The dict is keyed by message
    # precisely because this guard promises one in-flight steer per text, so the
    # guard has to read it or the uniqueness it promises is not enforced.
    if slot._pending_steers.count(message) or message in slot._steer_delivery_ids:
        logger.info("identical steer already pending for slot %s; queueing instead", slot.key)
        return STEER_UNAVAILABLE

    # A real identity, not a content match. Every earlier attempt here compared
    # text, and text cannot survive the transitions: consumed, requeued, drained,
    # or merged into a larger row all look alike afterwards. The id is keyed by the
    # message only because the one-per-text guard above makes that key unique, and
    # it is handed to the requeue, which puts it on the queue entry; the drain then
    # unions entry meta onto the row it appends, so the id reaches the row even
    # through a merge.
    delivery_id = uuid.uuid4().hex
    slot._steer_delivery_ids[message] = delivery_id
    slot._pending_steers.append(message)
    try:
        steered = await client.steer(message)
    except Exception as exc:  # best-effort — the caller falls back to the queue
        logger.warning("steer failed for slot %s: %s", slot.key, exc)
        steered = False

    # ONE reconciliation for every path. The outcome turns on WHERE the text is
    # now, not on `steered`: the RPC returning True only means the client
    # accepted the write, and the turn it was written into may already have ended
    # during the await. A natural teardown is the case a `steered`-gated check
    # misses entirely — it requeues the pending steer without touching
    # `_stop_generation`, so reporting STEERED would let the caller persist a row
    # that the queue drain then appends a second time.
    # Our entry is the only possible match (see the one-per-text guard), so a
    # surviving match is unambiguously ours.
    if _row_has_delivery_id(slot, delivery_id):
        # The whole requeue-then-drain sequence completed while we were suspended,
        # so the row is already written and the only thing left to get wrong is
        # writing a second one. Checked first: it is the one signal that survives
        # every intermediate transition, including a merged row.
        slot._steer_delivery_ids.pop(message, None)
        logger.info(
            "steer for slot %s was requeued and drained during the RPC; row already " "persisted",
            slot.key,
        )
        return STEER_REQUEUED

    still_registered = bool(slot._pending_steers.count(message))
    queued = _queue_has_delivery_id(slot, delivery_id)
    stopped = int(getattr(slot, "_stop_generation", 0) or 0) != stop_gen

    if still_registered:
        if not steered:
            # Unwind the optimistic registration so a queue fallback cannot
            # double-deliver. Unambiguous by construction: the one-per-text guard
            # above means this is the only matching entry, which is why this is a
            # plain remove and not an index dance over possible duplicates.
            slot._pending_steers.remove(message)
            slot._steer_delivery_ids.pop(message, None)
            return STEER_UNAVAILABLE
        if stopped:
            # Still registered means the teardown has not run yet and will
            # requeue it, so the text still runs — the caller must NOT resend.
            _log_stop_race(slot, stop_gen, preserved=True)
            return STEER_REQUEUED
        # Delivered and live: fall through to cut the segment and persist the row.

    # Ours vanished during the await, so some consumer took it. Which one decides
    # whether the message still runs, and only the queue can tell them apart.
    if queued:
        # The turn's teardown moved it — a natural end or a soft stop. Either
        # way it gets its own queue card and the drain appends it, so persisting
        # a row here would duplicate it.
        if stopped:
            _log_stop_race(slot, stop_gen, preserved=True)
        return STEER_REQUEUED
    # Absence alone does not say WHICH consumer took the registration. THREE
    # things remove one: the running turn CONSUMING the steer, the hard-kill
    # clear, and a teardown requeue whose queue card the user then cancelled
    # before we resumed. Only the first means the text ran, and they are told
    # apart by the delivery id, because a consume leaves `_steer_delivery_ids`
    # populated while the hard kill and `_requeue_unconsumed_steers` both drop it
    # (the requeue moves it into the queue entry's meta, which the `queued` check
    # above already answered -- reaching here means that entry is gone too).
    #
    # Checked regardless of `stopped`: a natural stage end requeues without ever
    # touching `_stop_generation`, so the cancelled-card case arrives with
    # `stopped` false and would otherwise fall through to the persisting tail.
    if message not in slot._steer_delivery_ids:
        # It did not run -- either a hard kill discarded the turn it was written
        # into, or it was requeued and the user cancelled its card. Persisting
        # here would write a transcript row for text that never executed and
        # tell the caller it landed. Resending is safe precisely because neither
        # path ran it.
        if stopped:
            _log_stop_race(slot, stop_gen, preserved=False)
        return STEER_UNAVAILABLE
    if stopped:
        # Consumed, then stopped: the text is already delivered and its side
        # effects may be complete, so this must never tell the caller to resend.
        # A duplicate execution is worse than a transcript row for a turn that was
        # killed, and worse still for an unattended caller that retries on its own.
        _log_stop_race(slot, stop_gen, preserved=True)
    if not steered:
        # NOT discarded. The entry is gone and nothing queued it, and the thing
        # that removes a registration in that state is the running turn CONSUMING
        # it. `steer()` writing successfully and then raising on `stdin.drain()`
        # lands exactly here, so trusting the exception over the evidence would
        # answer 409 for a message the target already has: the caller resends and
        # the target runs it twice.
        #
        # The asymmetry is deliberate. Telling a caller to RESEND is the one
        # answer that can cause a duplicate execution, so nothing reports it on
        # evidence that cannot tell delivery from loss. Every path here is
        # accounted for by somebody, and a duplicate is worse than a stale error.
        logger.info(
            "steer RPC for slot %s failed but its registration was consumed; "
            "treating as delivered",
            slot.key,
        )
    # The entry is gone because the RUNNING turn consumed it — the
    # `steering_consumed` settle path removes it exactly as a requeue would, which
    # is why absence alone can never be read as loss. A real delivery, so it takes
    # the same persisting tail as the live case.

    # Terminal for this delivery: the row is persisted below rather than by a
    # later drain, so nothing downstream will ever read this id again. The map is
    # keyed by the message TEXT, so leaving it would hold one full message string
    # per successful steer for the slot's whole lifetime -- the requeue paths above
    # deliberately keep theirs because `chat_runner`'s drain still has to match it,
    # and that entry is bounded by the queue.
    slot._steer_delivery_ids.pop(message, None)

    ts = datetime.now(timezone.utc).isoformat()
    # Cut the in-flight text segment at the steer boundary BEFORE persisting the
    # user message, so the transcript reads [assistant(pre-steer), user(steer),
    # …] — the order the client rendered live. Without this the whole segment
    # lands BELOW the steer bubble at end-of-turn and the refresh visibly
    # reorders the reply. Best-effort: a cut failure must never lose the steer.
    cut = getattr(slot, "_steer_segment_cut", None)
    if cut is not None:
        try:
            cut()
        except Exception:
            logger.warning("steer segment cut failed for slot %s", slot.key, exc_info=True)

    sanitized = sanitize_outbound(message)
    meta: dict[str, Any] = {"steer": True}
    # Store the sanitized form — raw content must never reach an external
    # surface — so the steer survives a page reload via the dirty-flush cycle.
    slot.append("user", sanitized, "msg msg-u", ts=ts, meta=meta)
    state.broadcast_ws(
        "steer_push",
        {
            "slot": slot.key,
            "content": _redact_for_display(sanitized),
            "ts": ts,
        },
    )
    return STEER_STEERED


def queue_for_next_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
) -> str:
    """Append *message* to the slot's queue and announce it; return the queue id.

    The running turn's teardown drains the queue, so this is how a message
    reaches a busy slot when steering is unavailable or not asked for.
    """
    qid = slot.queue_append(message)
    state.broadcast_ws(
        "queue_push",
        {
            "slot": slot.key,
            "content": _redact_for_display(sanitize_outbound(message)),
            "ts": datetime.now(timezone.utc).isoformat(),
            "queue_id": qid,
        },
    )
    return qid
