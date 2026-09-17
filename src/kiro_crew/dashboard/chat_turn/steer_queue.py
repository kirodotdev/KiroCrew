"""Steer settlement, queue entry classification and the drain's admission sweep."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import (
    CRON_NOTIFICATION_KIND,
    MCP_APP_MESSAGE_KIND,
    SUBAGENT_COMPLETION_KIND,
)

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        COMMANDS_OFF_META_KEY,
        STEER_POSSIBLY_DELIVERED_META,
        STEER_STATE_CONSUMED,
        STEER_STATE_REQUEUED,
        DashboardState,
        _ChatSlot,
        _remove_queued_by_id,
        _stamped_turn_actor,
        attachment_meta,
        crew_log_emit,
        find_written_steer_row,
        is_synthetic_payload_item,
        is_system_injection_item,
        logger,
        queued_text_for_display,
        quote_meta,
        settle_consumed_steers,
    )


def _mark_steer_row_state(
    state: "DashboardState",
    slot: "_ChatSlot",
    message: str,
    new_state: str,
    siblings: list[str] | None = None,
) -> None:
    """Move *message*'s persisted steer row to *new_state* and tell open clients.

    One writer for both lifecycle transitions, so `consumed` and `requeued` can
    never disagree about how a row is patched. Best-effort by design: a row that
    cannot be found or updated must never stop the settle or the requeue, because
    losing the message is worse than a row left in `written`.

    Reuses the existing `chat_message_update` patch rather than inventing a
    steer-specific event -- the client already applies that to a rendered row --
    and keys it on the row's `mid` where it has one, because `ts` is not an
    identity: two rows minted in the same clock tick share it, so a ts-only lookup
    takes whichever came first.
    """
    row = find_written_steer_row(slot, message, siblings)
    if row is None:
        return
    ts = str(row.get("ts") or "")
    new_meta = dict(row.get("meta") or {})
    new_meta["steerState"] = new_state
    _mid_raw = new_meta.get("mid")
    _mid = _mid_raw if isinstance(_mid_raw, str) and _mid_raw else None
    if not ts and not _mid:
        return
    # Patch by `mid` where the row has one: `ts` is not an identity, so a ts-only
    # lookup takes the FIRST row carrying it and could patch a same-tick twin.
    if slot.update_message(ts, meta=new_meta, mid=_mid) is None:
        return
    payload: dict[str, object] = {"slot": slot.key, "ts": ts, "meta": new_meta}
    if _mid:
        # Carried so the client resolves the same row this did; omitted when the
        # row has no mid, keeping the payload shape unchanged for legacy rows.
        payload["mid"] = _mid
    try:
        state.broadcast_ws("chat_message_update", payload)
    except Exception:
        # The row on the slot is already correct; clients reconcile from slot
        # detail on the next fetch.
        logger.warning(
            "steer state broadcast failed for slot %s (state %s)",
            slot.key,
            new_state,
            exc_info=True,
        )


def _settle_consumed_steers(
    slot: "_ChatSlot",
    snapshot: str,
    state: "DashboardState | None" = None,
) -> None:
    """Settle pending steers covered by a ``steering_consumed`` echo.

    The parse-and-match rules live in ``steer_settle.settle_consumed_steers``,
    shared with the ``/side`` sidecar, which hands kiro-cli the same
    fire-and-forget steers and needs the same answer.

    ``state`` is optional only so existing direct callers keep working; the
    production call site passes it, and without it the settled rows keep the
    ``written`` state they were persisted with rather than being promoted to
    ``consumed``.

    """
    if not slot._pending_steers:
        return
    # An empty echo is no evidence of consumption (``steer_settle`` says so in
    # as many words), so nothing settles and every entry stays pending. ``_requeue_unconsumed_steers`` -- wired into
    # ``_run_chat``'s outer finally, so it runs on every turn-exit path --
    # degrades a pending entry to a queue card at the head of the queue. The
    # cost is a duplicate: on a backend that injected the steer but echoed no
    # text, the steer runs again -- usually immediately, since the turn-exit
    # drain starts the next queued turn -- but it runs VISIBLY, as its own turn
    # in the transcript (and holds as a cancellable card when the drain is
    # withheld, e.g. sign-in required), unlike the silent loss that sweeping
    # the list on no evidence produces. Every sibling call site (the
    # ``_refusal_notices`` settle in the same event branch, and the ``/side``
    # sidecar) settles nothing on an empty echo too.
    previous = list(slot._pending_steers)
    remaining = settle_consumed_steers(previous, snapshot)
    settled_count = len(previous) - len(remaining)
    logger.debug(
        "Steer consumed for slot %s (%d settled, %d still pending)",
        slot.key,
        settled_count,
        len(remaining),
    )
    if state is not None and snapshot.strip():
        # Promote ONLY on an echo that carried evidence. An empty echo settles
        # nothing, so the multiset difference below is empty and promotes
        # nothing even without this guard -- it stays as an explicit
        # fail-closed gate, mirroring ``chat_delivery``'s positive-evidence
        # discipline, so a future change to the settle rules cannot make an
        # evidence-free frame promote a row.
        # Promote exactly the entries this echo accounted for. Computed as a
        # multiset difference against `remaining` so a duplicate identical steer
        # that stayed pending does not get its row promoted by its twin's echo.
        _still = list(remaining)
        for _msg in slot._pending_steers:
            if _msg in _still:
                _still.remove(_msg)
                continue
            # Record the evidence before promoting. `chat_delivery` decides a row's
            # INITIAL state by whether its entry is still registered, and an entry
            # removed by the empty-echo sweep is indistinguishable from one removed
            # by a matched echo at that point -- so it read an empty frame as a
            # confirmed injection. Keyed on the delivery id so a later identical
            # steer cannot inherit this one's evidence.
            _cdid = getattr(slot, "_steer_delivery_ids", {}).get(_msg, "")
            if _cdid:
                _confirmed_ids = getattr(slot, "_steer_confirmed", None)
                if _confirmed_ids is None:
                    _confirmed_ids = set()
                    slot._steer_confirmed = _confirmed_ids
                _confirmed_ids.add(_cdid)
            # No ledger entry from here, and none from the delivery either: the
            # session vocabulary carries no steer type, because its POSITION needs
            # two coroutines to agree and neither of them can.
            #
            # This echo is the only positive evidence that a turn consumed the text
            # and the only site that knows which turn did, so this is where such an
            # entry would have to be written. But the text the steer INTERRUPTED
            # reaches the log later, from the handler's segment cut, which runs when
            # `client.steer()` returns. Under stdin backpressure that RPC is still
            # in `drain()` when this echo arrives, so the steer entry would take a
            # lower seq than the assistant text it cut, and a fold reads that text
            # as the reply TO the steer rather than the reply it interrupted.
            #
            # Cutting the segment from here instead trades the defect for a worse
            # one: post-steer text arriving in the same window would be flushed
            # above the steer row in the transcript. Recording it at all therefore
            # waits on a resolver that owns both facts. A reader sees the steer as a
            # `message/received` on the next turn, which is what the transcript
            # shows too.
            # `remaining + [_msg]` is the live-steer list, NOT `_pending_steers`:
            # the resolver refuses to patch when two live steers share the
            # sanitized content, and `_pending_steers` still holds this whole
            # echo's entries until the assignment below. So a redaction collision
            # whose members the echo ALL accounted for looked ambiguous and both
            # rows kept `written` forever -- understating a confirmed injection,
            # the mirror of the defect this fix exists for. Ambiguity is about
            # attribution, and only a steer that is still PENDING can also claim
            # this row; ``steer_settle`` already draws the line this way for its
            # own keys. When a twin stayed pending the count is 2 again and the
            # refusal stands, because then which row is which is unknowable.
            _mark_steer_row_state(state, slot, _msg, STEER_STATE_CONSUMED, remaining + [_msg])
        if settled_count:
            # A steer row is persisted as soon as the RPC accepts it, while this
            # echo is the later authority that the running turn actually consumed
            # the user's message. The agent can post an ``ask_question`` card in
            # between. In that order the earlier row append found no card to
            # retire, so finish the same next-user-message lifecycle here.
            #
            # Keep the filter narrow: an unmatched/empty echo proves nothing,
            # and a legacy blocking ask owns a parked wait that only its
            # round-trip may resolve. ``clear_question_pending`` also broadcasts
            # the card ids and pushes the slot status, so reconnecting clients
            # cannot rehydrate the stale card.
            state.clear_question_pending(slot.key, blocking=False)
    slot._pending_steers[:] = remaining
    for settled_msg in set(previous) - set(remaining):
        slot._steer_attachment_meta.pop(settled_msg, None)
        slot._steer_decision_strips.pop(settled_msg, None)
        slot._steer_possibly_delivered.discard(settled_msg)
        # Held past the steer RPC's acknowledgement until this settle: the running
        # turn reads it at the consumption echo, before calling here. The channel
        # mark rides in the same map lockstep, so a consumed steer leaves neither.
        slot._steer_user_origin.pop(settled_msg, None)
        slot._steer_channel_origin.pop(settled_msg, None)


def _requeue_unconsumed_steers(state: "DashboardState", slot: "_ChatSlot") -> None:
    """Degrade unconsumed mid-turn steers into ordinary queue cards.

    The first step of :func:`_hand_off_queue`, so it runs once on every turn
    exit. A steer that kiro-cli never confirmed via ``steering_consumed`` died
    with the turn (stall-cancel, soft STOP, error, or a steer racing the
    turn's natural end); without this it would vanish silently.

    Requeues at the HEAD of the slot queue — steers were meant to be injected
    before any queued item ran — preserving their relative order, and
    broadcasts a ``queue_push`` per message so open clients render the card.
    The card is visible and individually cancellable: a user whose STOP meant
    "discard" dismisses it with one click; nothing is ever silently lost.
    A hard kill never reaches here with pending steers (the force-stop
    handler clears ``_pending_steers`` alongside ``_queue``).
    """
    if not slot._pending_steers:
        return
    requeued = slot._pending_steers[:]
    slot._pending_steers.clear()
    for steer_msg in reversed(requeued):
        # The turn is over and never confirmed this steer, so the row persisted at
        # write time is now WRONG if it still reads as a successful injection.
        # Correct it before the queue card goes out, so the transcript and the card
        # tell the same story: this message did not redirect that turn, it runs as
        # its own. Done for every requeued entry, including the ones whose
        # delivery id below is still live -- the row is the user-facing claim and it
        # has to be corrected either way.
        # `requeued` is passed as the live-steer list because `_pending_steers` was
        # cleared above: the resolver needs to know how many steers in THIS batch
        # share the sanitized content before it trusts the newest matching row.
        _mark_steer_row_state(state, slot, steer_msg, STEER_STATE_REQUEUED, requeued)
        # Raw-at-rest by design: slot._queue is a DELIVERY payload (the drained
        # entry becomes the next turn's LLM input), matching every other queue
        # producer (queue_append in chat_handlers / messaging). The dashboard
        # egresses go through `queued_text_for_display`: the session's own
        # human's text is shown as typed, like an ordinary send's row, and every
        # other origin is display-redacted. Sanitizing at insert would corrupt the
        # delivered message relative to the normal queue path.
        #
        # Carry the delivery id the steer registered under. The drain unions every
        # consumed entry's meta onto the row it writes, so this reaches the row even
        # when several queued items are merged into one — which is the only way the
        # steer's caller can tell "already persisted by the drain" from "consumed by
        # the turn" after both bookkeeping lists have emptied.
        #
        # Stamp the containment snapshot too: a requeued steer is plain
        # user speech re-entering the queue, and this requeue is the last moment
        # its admission is re-affirmed — a link appearing between here and the
        # drain must drop it like any other queued prompt, while a session that
        # was ALREADY channel-born keeps its steers.
        #
        # The snapshot comes from the SEND's gate, never from reading the slot here:
        # this requeue runs in the teardown, past the steer RPC's suspension, so a
        # slot read would fold a mirror linked during that suspension into the
        # baseline and the drain would then read the widened audience as admitted.
        # `steer_into_running_turn` requires the stamp from every caller, so the
        # absent case is a steer registered by code that predates it; that entry
        # carries NO containment key and the drain checks it against every currently
        # held constraint, which is the documented fail-closed floor.
        _recorded = getattr(slot, "_steer_admissions", {}).pop(steer_msg, None)
        _meta: dict = dict(_recorded) if _recorded else {}
        _did = getattr(slot, "_steer_delivery_ids", {}).pop(steer_msg, "")
        if _did:
            _meta["steer_delivery_id"] = _did
        # Carry the client's `sendId` the same way, for the same reason one step
        # further on. The drain unions this meta onto the row it writes, so
        # this is what gives a REQUEUED steer's row the `meta.sendId` an ACCEPTED
        # steer's row already gets from `steer_into_running_turn` -- without it the
        # row is id-less, `mergePreservedThinking` has no id to resolve the
        # optimistic bubble against, and the pre-steer thinking chip strands at the
        # tail until a reload. Popped in lockstep with the delivery id above so the
        # two maps never disagree about what is still in flight. Additive: a steer
        # whose POST carried no id stores nothing here and its entry meta keeps the
        # exact prior shape.
        _sid = getattr(slot, "_steer_send_ids", {}).pop(steer_msg, "")
        if _sid:
            _meta["sendId"] = _sid
        _meta.update(getattr(slot, "_steer_attachment_meta", {}).pop(steer_msg, {}))
        # The decision receipt rides the entry for the same reason the two ids do:
        # the drain unions a consumed entry's meta onto the row it writes, so this is
        # the only writer a requeued steer has. Both outcomes a `message.steer`
        # decision can CHOOSE already stamp it -- the steer path on its persisted row,
        # `queue_for_next_turn` on its entry -- and this is the third path, the race
        # where the turn ended while the RPC was suspended. Popped in lockstep with the
        # maps above. Additive: a manual steer recorded nothing and its entry meta
        # keeps the exact prior shape.
        _strip = getattr(slot, "_steer_decision_strips", {}).pop(steer_msg, None)
        if _strip:
            _meta["decisions_strip"] = _strip
        # Provenance is REPORTED by the steer's caller, not derived from the slot.
        # `steer_into_running_turn`'s callers differ on exactly this point: the
        # api_chat composer branch, whose text its session's own human typed, and
        # `session_send`, whose text a peer sent (a channel conversation resumed
        # into the session reports as the composer does -- its owner gate makes the
        # author the session's own human). The slot cannot tell them apart, and the
        # difference is the whole point of the flag:
        # `directive_user_origin` exempts the entry from the drain's LINKED drop
        # because "the author typed into the session's own surface", which is true
        # of the composer and false of a peer. Deriving it would hand a peer the
        # human's exemption, so a link appearing while the steer RPC was suspended
        # would let the peer's text run and mirror to an audience
        # `authorize_target` refuses outright.
        #
        # Absent means NOT the session's own human: an unrecorded steer fails closed
        # into the ordinary drop rather than inheriting the exemption. An app slot's
        # steers stay unexempted as before.
        _origin = bool(getattr(slot, "_steer_user_origin", {}).pop(steer_msg, False))
        _requeue_user_origin = _origin and not bool(getattr(slot, "_app", ""))
        # The channel mark rides the same way, from its own lockstep map: a
        # requeued steer runs as its own turn, so a channel human's text keeps the
        # narrower channel authority a queued channel message carries. Absent means
        # not through a channel.
        _channel = bool(getattr(slot, "_steer_channel_origin", {}).pop(steer_msg, False))
        if _channel:
            # A channel steer that re-enters the queue runs as its own turn, so it
            # takes the mark the hand-off's queued entry carries: the text is the
            # sender's words, never a dashboard command to run on drain.
            _meta[COMMANDS_OFF_META_KEY] = True
        _maybe_delivered: set[str] = getattr(slot, "_steer_possibly_delivered", set())
        # An RPC still in flight counts too: its frame may already be in the
        # pipe, and its verdict lands after this entry may have drained.
        if steer_msg in _maybe_delivered or steer_msg in getattr(
            slot, "_steer_rpc_in_flight", set()
        ):
            _maybe_delivered.discard(steer_msg)
            _meta[STEER_POSSIBLY_DELIVERED_META] = True
        qid = slot.queue_insert(
            0,
            steer_msg,
            meta=_meta,
            directive_user_origin=_requeue_user_origin,
            directive_channel_origin=_channel,
        )
        try:
            _push: dict = {
                "slot": slot.key,
                # As typed only for the session's own human -- the rule the queue view
                # applies to the entry (``queue_entry_is_user_origin``), which reads
                # the channel stamp too.
                "content": queued_text_for_display(
                    steer_msg, user_origin=_requeue_user_origin and not _channel
                ),
                "ts": datetime.now(timezone.utc).isoformat(),
                "queue_id": qid,
            }
            # The requeued steer's attachment lists and quote were folded into
            # `_meta` above; the card drawn from this frame is what a cancel
            # restores, and a frame without the quote would show the raw
            # blockquote as text until a reload.
            _push_meta = {
                **attachment_meta(_meta),
                # Same rule the entry's text follows (`queue_entry_is_user_origin`):
                # the human's own words stay as typed, anyone else's are redacted.
                **quote_meta(_meta, user_origin=_requeue_user_origin and not _channel),
            }
            if _push_meta:
                _push["meta"] = _push_meta
            state.broadcast_ws("queue_push", _push)
        except Exception:
            # Broadcast is best-effort — the message is already safely in the
            # queue; clients reconcile from slot detail on next fetch.
            logger.warning(
                "queue_push broadcast failed for requeued steer (slot %s)",
                slot.key,
                exc_info=True,
            )
    logger.info(
        "Requeued %d unconsumed steer(s) for slot %s (turn ended before consumption)",
        len(requeued),
        slot.key,
    )


def _queue_entry_is_orchestration(item: dict) -> bool:
    """True when a queue entry is runner/system orchestration, not user speech.

    Used only by the promise-only guards (via `_has_user_queued_followup`) to
    decide "did the USER intervene". A background cron notification or sub-agent
    completion queued mid-turn is orchestration, not a user "don't do that", so it
    must NOT block or purge a pending recovery.

    Classification is PURELY STRUCTURAL — the `kind` tag stamped at enqueue, never
    the message text. `is_system_injection_item` covers the three orchestration
    kinds (`CRON_NOTIFICATION_KIND`, `SUBAGENT_COMPLETION_KIND`,
    `SYNTHETIC_RECOVERY_KIND`); `is_synthetic_payload_item` additionally covers a
    recovery entry that replays runner-authored text. There is deliberately NO
    content match: a `CRON_NOTIFY_RE.match` / prefix test is prefix-anchored and
    therefore spoofable — a user could queue
    `[Cron notification from "x"]\ndon't delete it` during a promise-only turn and
    have their intervention silently ignored while the announced action dispatches
    anyway. A user message carries no enqueue tag, so it correctly counts as a
    user follow-up and aborts the pending recovery; real cron / sub-agent events
    are tagged at their injection sites and stay excluded."""
    return is_synthetic_payload_item(item) or is_system_injection_item(item)


#: Which turn actor an enqueue-time queue ``kind`` names. The tag is stamped by
#: the producer at ``queue_append`` and is not derivable from the entry's text,
#: which is the point: the banners these two injections wrap their text in
#: (``CRON_NOTIFY_PREFIX``, ``SUBAGENT_COMPLETION_PREFIXES``) are strings a user
#: can type, and attributing a turn in the session's log is a claim a reader
#: takes as fact. Same source, same reason as ``is_system_injection_item``.
#:
#: ``SYNTHETIC_RECOVERY_KIND`` is deliberately absent: a recovery's actor is
#: whoever caused the ORIGINAL turn, which no kind can name, so it travels in the
#: entry's meta instead (:data:`TURN_ACTOR_META_KEY`).
_QUEUE_KIND_ACTORS: dict[str, str] = {
    CRON_NOTIFICATION_KIND: "cron",
    SUBAGENT_COMPLETION_KIND: "subagent",
    MCP_APP_MESSAGE_KIND: "app",
}


def _actor_for_queue_items(items: "list[dict]") -> str:
    """The turn actor the consumed queue entries name, or ``""`` for none.

    First mapped kind wins, then a stamped actor. A merge run never mixes them --
    it stops AT a system injection (``_dequeue_next_message``), so a merged batch
    is either all plain user messages or one injection alone.
    """
    for item in items:
        actor = _QUEUE_KIND_ACTORS.get(item.get("kind", ""))
        if actor:
            return actor
    for item in items:
        stamped = _stamped_turn_actor(item)
        if isinstance(stamped, str) and stamped in crew_log_emit.ACTORS:
            return stamped
    return ""


def _has_user_queued_followup(slot: "_ChatSlot") -> bool:
    """True when the slot queue holds a USER-authored follow-up message.

    The promise-only guards use this to decide "did the USER intervene". Every
    entry that is runner/system orchestration (`_queue_entry_is_orchestration`) is
    excluded; anything left is user speech, which must block or purge a pending
    recovery so the user's intent wins."""
    return any(not _queue_entry_is_orchestration(q) for q in getattr(slot, "_queue", []))


def _drop_stale_admissions(state: DashboardState, slot: _ChatSlot) -> None:
    """Drop queued entries whose admission-time containment has lapsed.

    Authorization is decided when a prompt is ADMITTED (`authorize_target` for
    `session_send`, the authenticated composer for a human typing into a busy
    session), but delivery happens later, at this drain — and the target-side
    containment those decisions rest on can change in between: a target
    authorized while unlinked can be given a channel or mirror link before its
    queue drains, and the queued prompt would then execute and republish to an
    audience its admission never contemplated.

    Producers of plain (user-speech) entries stamp the containment snapshot at
    enqueue (`session_control.containment_meta`); this sweep recomputes the same
    constraints and drops any entry for which a constraint holds NOW that did
    not hold at admission — including a WORKSPACE change, which swaps the
    memory/lessons/project context under a waiting prompt. An unmarked plain
    entry fails closed against the boolean constraint set, so an untagged
    producer can never ride a queued prompt past a boundary the tagged paths
    respect.

    Entries carrying `_directive_user_origin` (authenticated-human provenance)
    are exempt from the LINKED constraint only: the author typed into the
    session's own surface and linking it is that owner's deliberate act, so
    composer input into a just-linked session is designed behaviour. A NEW
    outbound mirror still drops them — the author does not control mirror
    links — as do all other constraints (see
    `session_control.newly_held_constraints`).

    Structural exemption is narrow: cron notifications and sub-agent
    completions only (`CRON_NOTIFICATION_KIND` / `SUBAGENT_COMPLETION_KIND`) —
    runner machinery minted fresh by trusted internal producers, which
    channel-born sessions receive by design. Synthetic-recovery entries are
    NOT exempt: a recovery replays externally admitted content verbatim, so it
    is re-validated like any plain entry against the admission stamp its
    requeue recorded (`_queue_recovery`), failing closed when unmarked.

    Runs at the top of the drain with no suspension point between the snapshot
    and the dequeue (everything below is synchronous on the event loop), so the
    decision cannot go stale before the surviving entry becomes a turn. A drop
    is never silent: the queue card is retracted, a visible notice naming the
    changed constraint lands in the transcript, and the drop is written to the
    SEL.

    A CROSS-SESSION delivery is reported in both directions. The entry carries
    the sending session as a slot key plus that slot's tab identity
    (`session_control.send_origin_meta`, stamped at admission beside the
    containment snapshot), so the sender gets its own notice naming
    the target and the changed constraint
    (`session_control.notify_send_origin_dropped`) and the SEL row names it as
    the drop's origin. Without that the sender's last word on the message is the
    `started: False` receipt it got when the target queued it, and the outcome it
    most needs — the message will never run — would reach only the target's
    transcript. A human-typed entry carries no stamp and is unaffected.
    """
    if not slot._queue:
        return
    # circular import: session_control imports this package's modules at module level.
    from kiro_crew.dashboard import session_control as _sc

    now = _sc.containment_snapshot(state, slot, on_probe_failure=True)
    _mirror_unverified = bool(now.get("mirror_unverified"))
    doomed: list[tuple[dict, list[str]]] = []
    for q in slot._queue:
        # Exempt ONLY cron notifications and sub-agent completions: both are
        # minted fresh by trusted internal producers for THIS slot's own turn
        # lifecycle, and channel-born sessions receive them by design. A
        # synthetic-recovery entry is deliberately NOT exempt — it replays
        # externally admitted content verbatim under a fresh queue id, so an
        # exemption would let the retry ride past a link that appeared during
        # the recovery window. Every recovery producer stamps admission context
        # at requeue (`_queue_recovery`, the manual continue), so a recovery in
        # a channel-born session still drains: its stamp records linked=True.
        if q.get("kind") in (CRON_NOTIFICATION_KIND, SUBAGENT_COMPLETION_KIND):
            continue
        changed = _sc.newly_held_constraints(
            now,
            q.get("meta"),
            directive_user_origin=q.get("_directive_user_origin") is True,
        )
        if changed:
            doomed.append((q, changed))
    for q, changed in doomed:
        slot.queue_remove_by_id(q["id"])
        # The broadcast is unconditional: the frontend's queue card was created
        # by the producer's queue_push, not by a transcript placeholder row, so
        # gating retraction on the (rare) placeholder existing would leave a
        # card on screen for a message the server discarded. The placeholder
        # removal is the separate, best-effort half.
        _remove_queued_by_id(slot.messages, q["id"])
        state.broadcast_ws("queue_pop", {"slot": slot.key, "content": "", "queue_id": q["id"]})
        slot.append(
            "notice",
            "⚠️ Queued message dropped: "
            + _sc.describe_containment_change(changed, mirror_unverified=_mirror_unverified)
            + " after it was queued, so the authorization that admitted it no longer holds.",
            "msg msg-info",
        )
        # A cross-session delivery has a SENDER waiting on it, and the notice
        # above is on the target's transcript, which that sender does not read.
        # It was told at admission that the message was queued, so without this
        # the one outcome it most needs — the message will never run — is the one
        # it is never told. Read off the entry's own stamp, which is empty for a
        # human-typed entry and for an entry restored after a restart: the
        # restore path strips the stamp deliberately, because it names a write
        # target and the metadata line is editable, so a delivery that outlives a
        # restart is dropped without a report rather than reported to whoever an
        # edited stamp named.
        #
        # The stamp's TAB identity rides along so the notice can only reach the
        # slot object that sent the message. Named slot keys are reused, and the
        # notifier refuses a key whose current occupant is a different tab.
        _meta = q.get("meta")
        _origin = _sc.send_origin_slot(_meta)
        _sc.notify_send_origin_dropped(
            state,
            origin=_origin,
            origin_tab=_sc.send_origin_tab(_meta),
            target_slot=slot,
            text=q.get("content") or "",
            constraints=changed,
            mirror_unverified=_mirror_unverified,
        )
        # A message a CHANNEL conversation queued here (a resumed dashboard
        # session's Discord DM) has the same reader problem one step further
        # out: it was told "queued" in the channel and reads neither this
        # transcript nor the SEL. Same stamp discipline -- read off the entry,
        # stripped on restore -- and the notice re-runs the outbound recipient
        # check before anything is sent.
        _sc.notify_channel_recipient_dropped(
            state,
            entry_meta=_meta,
            target_slot=slot,
            text=q.get("content") or "",
            constraints=changed,
            mirror_unverified=_mirror_unverified,
        )
        _sc.audit_queued_drop(slot, q["id"], changed, origin=_origin)
        _log = logger.warning if _mirror_unverified and "mirrored" in changed else logger.info
        _log(
            "Dropped queued entry %s for slot %s at drain re-validation " "(newly held: %s%s)",
            q["id"],
            slot.key,
            ",".join(changed),
            (
                "; mirror probe FAILED — refusal is fail-closed, not an observed link"
                if _mirror_unverified and "mirrored" in changed
                else ""
            ),
        )
