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
import re
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

# Lifecycle of a mid-turn steer as recorded on the persisted transcript row, in
# `meta["steerState"]`. These are three DIFFERENT facts and the row must not
# claim one while holding another:
#
#   written  -- the bytes reached the backend process and `steer()` returned. The
#              backend may answer `steering_queued`, which says only that it
#              accepted the message, NOT that the running turn took it.
#   consumed -- the backend echoed `steering_consumed` and the running turn
#              incorporated the message. This is the ONLY state that proves the
#              in-flight generation was actually redirected.
#   requeued -- the turn ended with no consumption echo, so the teardown moved the
#              message to the queue and it runs as its own turn.
#
# A steer can only be injected at a model-inference boundary, so a turn that is
# streaming text without dispatching a tool may never reach one before it ends
# (see `AcpSessionHandle.last_steer_monotonic`). That path is `written` followed
# by `requeued` and never touches `consumed` -- the case the row used to render as
# a successful injection (#7246).
STEER_STATE_WRITTEN = "written"
STEER_STATE_CONSUMED = "consumed"
STEER_STATE_REQUEUED = "requeued"

# Upper bound on a client-minted ``meta.sendId`` accepted into the steer path.
# Client mints are ~17 chars; the bound exists because the value is raw client
# input that gets persisted into slot history and broadcast to every tab.
SEND_ID_MAX_LEN = 128

# The accepted send-id alphabet. Client mints are ``s-<base36>-<base36>``; the
# allowlist is deliberately a little wider (URL-safe id charset) so a future
# client id shape does not silently lose reconciliation, while still excluding
# every separator a structured secret needs (``/ + = .`` — base64 padding, JWT
# dots, path-shaped tokens).
_SEND_ID_RE = re.compile(rf"^[A-Za-z0-9_-]{{1,{SEND_ID_MAX_LEN}}}$")


def normalize_send_id(value: object) -> str | None:
    """Return *value* when it is a usable client send-correlation id, else None.

    Deny-by-default over raw client input, in two gates:

    1. Shape: a non-empty string in the id alphabet within ``SEND_ID_MAX_LEN``.
    2. Content: the canonical credential scan (``redact_credentials``) finds
       nothing. The alphabet alone cannot exclude bare alphanumeric key shapes
       (an AWS access-key id, a ``ghp_`` token), and this value is persisted
       into slot history and broadcast on ``steer_push`` WITHOUT the outbound
       redaction the message text goes through — so anything the scanner would
       redact is refused outright here instead.

    A failing value is treated as ABSENT (the old-client shape), never
    truncated or redacted-in-place — a rewritten id would silently mismatch the
    client's copy and defeat the reconciliation it exists for. Lives here, with
    the sink that persists and broadcasts the value, so every caller inherits
    both gates.
    """
    if not isinstance(value, str) or not _SEND_ID_RE.fullmatch(value):
        return None
    cleaned, _warnings = redact_credentials(value)
    if cleaned != value:
        return None
    return value


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


def find_written_steer_row(
    slot: Any, message: str, siblings: list[str] | None = None
) -> dict[str, Any] | None:
    """Return the persisted row for *message* still in the WRITTEN state, or None.

    The lifecycle transitions need the row they are correcting, and the delivery
    id cannot supply it: the successful-steer path is terminal for that id and
    pops it (the map is keyed by message text and would otherwise hold one full
    message string per steer for the slot's lifetime).

    Returns None while this steer still has an entry in ``_steer_delivery_ids``:
    that entry lives from registration until the persisting tail pops it, so its
    presence means THIS steer has no row yet. Any `written` row matching the
    content at that moment belongs to an EARLIER steer -- for instance one whose
    turn was hard-killed, which clears the pending list without reaching either
    transition and truthfully leaves its row `written` forever. Patching it would
    mark a steer consumed that never was.

    Otherwise resolved by the SANITIZED content of this exact message plus a
    still-`written` state. SEVERAL rows can match, because those hard-killed rows
    stay `written` for the slot's life, so the tie is broken by asking how many
    LIVE steers could own one: *siblings* is the in-flight message list (the slot's
    pending steers by default; the requeue passes the batch it captured before
    clearing). When exactly one of them sanitizes to this target, the NEWEST match
    is unambiguously this steer's row and every older one is a dead row.

    When two or more LIVE steers share the sanitized content, this returns None and
    the rows keep `written`. That is the residual redaction collision: the
    in-flight guard admits one steer per RAW text while the row stores the
    SANITIZED text, so two steers differing only in credential material are both
    admitted with byte-identical rows -- the same injectivity loss ``steer_settle``
    documents for its own keys. Understating a state is recoverable; claiming the
    wrong message was the one the turn consumed is not. Real identity for a pending
    steer is the refactor tracked in #4333, not this fix.
    """
    if message in getattr(slot, "_steer_delivery_ids", {}):
        # Registered but not yet persisted: this steer owns no row, so every
        # candidate below is somebody else's.
        return None
    target = sanitize_outbound(message)
    live = siblings if siblings is not None else getattr(slot, "_pending_steers", [])
    if sum(1 for p in live if sanitize_outbound(p) == target) > 1:
        logger.info(
            "steer state left unchanged for slot %s: more than one live steer "
            "sanitizes to this content, so which row is this one's is unknowable",
            getattr(slot, "key", "?"),
        )
        return None
    matches = [
        m
        for m in slot.messages
        if isinstance(m.get("meta"), dict)
        and m["meta"].get("steerState") == STEER_STATE_WRITTEN
        and m.get("content") == target
    ]
    # Newest wins: an older match is a row whose own steer already died without
    # transitioning, so it cannot be this one.
    return matches[-1] if matches else None


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
    *,
    send_id: str | None = None,
) -> str:
    """Inject *message* into the slot's RUNNING turn; return a ``STEER_*`` outcome.

    Requires a live, steer-capable inner ACP client that the turn published on
    the slot. Fire-and-forget by design: the inline steer card materializes when
    kiro-cli echoes ``steering_consumed``.

    ``send_id`` is the client-minted correlation id from the send's meta (the
    same ``sendId`` convention the plain send path persists). When present it is
    stamped onto the persisted steer row AND the ``steer_push`` broadcast, so the
    client can reconcile its optimistic bubble — and resolve the bubble's
    accepted-vs-new-turn ambiguity — by id identity instead of text. Optional
    and additive: a send without one keeps the exact prior row/payload shape.
    Normalized at entry (``normalize_send_id``) so the type/length bound holds
    for every caller, not just the current one.
    """
    send_id = normalize_send_id(send_id)
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
    # Recorded HERE, next to the delivery id, because the requeue is what needs it
    # and the requeue runs in the TURN's teardown -- another coroutine, which never
    # sees this call's arguments. The three `STEER_REQUEUED` returns below cannot
    # do this themselves: two of them have no queue entry to write to at the moment
    # they run (one returns before the teardown has requeued anything, the other
    # after the drain already wrote the row), so the only common writer is
    # `_requeue_unconsumed_steers`. Normalized value, not the raw argument -- the
    # entry meta is persisted with the queue and reaches the row, so it must clear
    # the same gate the row stamp does. Absent id stores nothing, which keeps the
    # requeued entry's meta byte-identical to its pre-#6751 shape.
    if send_id:
        slot._steer_send_ids[message] = send_id
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
        slot._steer_send_ids.pop(message, None)
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
            slot._steer_send_ids.pop(message, None)
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
    # Same lockstep, same reason: this delivery stamps `sendId` onto its own row a
    # few lines below, so nothing will read the map entry again and leaving it
    # would hold a full message string for the slot's lifetime.
    slot._steer_send_ids.pop(message, None)

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
    # `steer` marks the row as a steer (the client's turn-boundary logic reads it
    # and must keep seeing it); `steerState` says WHICH of the three lifecycle
    # states it is in.
    #
    # TWO routes reach this tail and they are in different states, so the state is
    # derived rather than assumed. Still registered means the entry survived the
    # RPC: delivered and live, with no consumption echo yet, so `written`.
    #
    # Gone means SOME remover took it during the await, and absence alone does not
    # say which -- that is the whole difficulty. AT LEAST TWO can: the settle path
    # promoting an entry a non-empty echo accounted for, and the
    # `settle_all_on_empty` sweep clearing the pending list on an EMPTY echo, which
    # is no evidence of consumption at all. After the fact the two removals are
    # indistinguishable here, so inferring `consumed` from absence persisted a
    # success badge on a frame that proved nothing -- terminal and never corrected,
    # which is the exact claim this change exists to stop. An earlier version of
    # this comment asserted that every other remover had returned above; it had not,
    # and that sentence is why the bug read as correct.
    #
    # So the state comes from POSITIVE evidence: the settle path records the delivery
    # ids a non-empty echo accounted for, and only a recorded id yields `consumed`.
    # Absence of a record means `written`, which is what is actually known. "At least
    # two" is deliberate -- these files are hot, and a remover added later must not
    # inherit `consumed` by default. With this gate it cannot: it would have to
    # record evidence to get it.
    # FAIL CLOSED. "No evidence" and "no marker" must be the SAME branch: absent,
    # None, empty, or not a set (a future refactor, a slot rebuilt from disk) all
    # yield `written`. A marker whose absence produced the CONFIRMING value would
    # reintroduce this bug through a different door, and invisibly, because the row
    # is terminal. The isinstance test is load-bearing rather than defensive:
    # `in` raises TypeError on a non-container and `.discard` raises AttributeError
    # on a non-set, so an unreadable marker would otherwise crash the steer path
    # instead of degrading to the honest state.
    # Written as one `isinstance` BRANCH rather than a boolean plus two uses: a
    # narrowing does not survive being stored in a separate flag, so mypy still
    # saw `Any | None` at the `in` and the `.discard` and failed the type gate on
    # the very guard that exists to make those two calls safe.
    _confirmed_ids = getattr(slot, "_steer_confirmed", None)
    if isinstance(_confirmed_ids, set):
        _had_evidence = delivery_id in _confirmed_ids
        # Single-use: a later steer minting a new id must not inherit this one's.
        _confirmed_ids.discard(delivery_id)
    else:
        _had_evidence = False
    _state = (
        STEER_STATE_CONSUMED if (not still_registered and _had_evidence) else STEER_STATE_WRITTEN
    )
    meta: dict[str, Any] = {"steer": True, "steerState": _state}
    if send_id:
        # Persist the client correlation id alongside the steer flag: the
        # transcript page is what mergePreservedThinking reads to resolve an
        # optimistic bubble by id (accepted steer vs raced new turn, #6075).
        meta["sendId"] = send_id
    # Store the sanitized form — raw content must never reach an external
    # surface — so the steer survives a page reload via the dirty-flush cycle.
    _row = slot.append("user", sanitized, "msg msg-u", ts=ts, meta=meta)
    push_payload: dict[str, Any] = {
        "slot": slot.key,
        "content": _redact_for_display(sanitized),
        "ts": ts,
        # Same state the row carries, so a live client and a page reload agree.
        # A later `chat_message_update` moves a `written` row to consumed or
        # requeued; a row already persisted as consumed is terminal.
        "steerState": _state,
    }
    # The row's own id, so the client stores it and the later state patch -- which
    # is keyed on `mid` -- can find this row. Without it the client row has no
    # `mid`, the mid-keyed patch matches nothing, and the promotion is a silent
    # no-op until the page is reloaded.
    _row_mid = (_row.get("meta") or {}).get("mid") if isinstance(_row, dict) else None
    if isinstance(_row_mid, str) and _row_mid:
        push_payload["mid"] = _row_mid
    if send_id:
        # Echoed back so the initiating tab reconciles its optimistic bubble by
        # id; omitted when absent so the payload shape is unchanged for sends
        # that never minted one.
        push_payload["sendId"] = send_id
    state.broadcast_ws("steer_push", push_payload)
    return STEER_STEERED


def queue_for_next_turn(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    *,
    directive_user_origin: bool = False,
    send_id: object = None,
) -> str:
    """Append *message* to the slot's queue and announce it; return the queue id.

    The running turn's teardown drains the queue, so this is how a message
    reaches a busy slot when steering is unavailable or not asked for.

    ``send_id`` is echoed on the broadcast for the same reason ``steer_push``
    echoes it: the queued card is what now owns the message, so the initiating
    tab must be able to retire the optimistic row it rendered. The POST receipt
    also reports ``queued``, but a 2xx whose body will not parse leaves that
    unreadable, and then only this event can release the row.
    """
    # circular import: session_control imports this module at module level.
    from kiro_crew.dashboard.session_control import containment_meta

    normalized = normalize_send_id(send_id)
    entry_meta = containment_meta(state, slot)
    if normalized:
        # Durable, not just broadcast: a tab that misses `queue_push` learns the send
        # landed only from the drained row, and the drain reads the ENTRY's meta.
        entry_meta = {**(entry_meta or {}), "sendId": normalized}
    qid = slot.queue_append(
        message,
        meta=entry_meta,
        directive_user_origin=directive_user_origin,
    )
    push_payload: dict[str, object] = {
        "slot": slot.key,
        "content": _redact_for_display(sanitize_outbound(message)),
        "ts": datetime.now(timezone.utc).isoformat(),
        "queue_id": qid,
    }
    if normalized:
        # Omitted when absent so the payload shape is unchanged for a send that
        # never minted one; the client treats absence as "no release".
        push_payload["sendId"] = normalized
    state.broadcast_ws("queue_push", push_payload)
    return qid
