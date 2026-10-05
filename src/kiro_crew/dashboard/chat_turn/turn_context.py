"""The context a dashboard turn's input carries: the execution context's memory-mode
fold, the pending context drain and the merge-card context it takes, puts back and
retires, the folder-steering gate and the appended-context split."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Iterable, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _CONTEXT_FRAME_CONTRACT,
        _MAX_CONTEXT_PER_SOURCE,
        _MAX_PENDING_CONTEXT,
        DashboardState,
        _ChatSlot,
        _MemoryUnavailable,
        canonical_memory_mode,
        context_entry_expired,
        effective_session_key,
        logger,
        read_session_execution,
        slot_history_key,
        stricter_memory_mode,
        tighten_live_session_execution,
        transcripts_share_file,
    )


def _folder_steering_turn(
    slot: Any,
    execution_context: Any,
    *,
    context_is_new: bool,
    provider_has_history: bool,
    needs_reinjection: bool,
) -> bool:
    """Whether this turn must resolve the folder's steering directories.

    A template chat reads the folder tree only on the two turns that carry
    session-start context: a fresh provider session (not a resumed one, which
    already holds its original injection) and a reinjection after compaction.
    Warm template turns never touch it.

    A V2 MEMBER chat is different. ``build_message`` rebuilds the member's
    essentials envelope on EVERY turn, and that envelope declares itself the
    complete replacement for all prior snapshots ("do not keep applying removed
    sources"). Folder steering rides inside that envelope, so a warm member turn
    that passed no directories would hand the model a snapshot that silently
    withdraws the folder's guides. Every turn that rebuilds the envelope resolves.
    """
    if (context_is_new and not provider_has_history) or needs_reinjection:
        return True
    return bool(
        getattr(slot, "mode", "") == "member"
        and getattr(slot, "agent", "")
        and execution_context is not None
        and getattr(execution_context, "member_id", None)
    )


def _read_and_tighten_turn_execution(
    conversation_log: Any, session_key: str, transcript_key: str | None = None
):
    """Fold the transcript privacy line into the live turn carrier off-loop.

    ``read_session_execution`` deliberately serves a live carrier without file I/O
    because synchronous callers also use it on the event loop. Turn admission has
    already moved to a worker thread, so this is the one read-back that can safely
    compare the live carrier with the line and republish only a stricter mode.
    """
    execution = read_session_execution(session_key)
    if execution is None:
        return None
    if conversation_log is None:
        # No transcript store at all (history disabled, or a state built without
        # one): there is no line on disk to fold, so the carrier stands as read.
        return execution
    # The carrier is addressed by the SESSION key; the privacy line lives on the
    # TRANSCRIPT, which for an unbound channel-born slot is a different file
    # (``slot_history_key`` vs ``effective_session_key``). Read the line by the
    # transcript key, or the fold finds no line there and tightens nothing.
    metadata, readable = conversation_log.get_metadata_status(transcript_key or session_key)
    if not readable:
        # An unreadable line is a transient read failure (fd exhaustion, a
        # sharing violation while another writer's replace lands), not a mode
        # -- and it leaves this turn with NO contract to run under. Neither
        # extreme is right: tightening to Temporary would turn one failed read
        # into a permanent ratchet on a persistent chat (everything this helper
        # publishes only ever narrows), while proceeding on the carrier as read
        # would let a line another writer already tightened -- Temporary over
        # this process's persistent carrier -- go unseen for a whole turn, with
        # memory injection and memory writes still enabled under the looser
        # mode. So the turn is refused, retryably: the same answer the save
        # gives when it meets an unreadable line ("deferred for retry"), and the
        # same card every other memory-unavailable turn shows. Nothing ran,
        # nothing was written; the next turn re-reads the line.
        raise _MemoryUnavailable(
            "memory_unavailable: this conversation's privacy line could not be read "
            "just now; nothing was sent -- try again in a moment"
        )
    if "memory_mode" not in metadata:
        return execution
    retained_mode = stricter_memory_mode(
        canonical_memory_mode(metadata.get("memory_mode")), execution.memory_mode
    )
    if retained_mode == execution.memory_mode:
        return execution
    tightened_live = tighten_live_session_execution(session_key, retained_mode, expected=execution)
    return tightened_live or execution.with_mode(retained_mode)


def _consumed_merge_contexts(
    state: DashboardState, before: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Merge-card contexts removed from every queue by this turn's drain."""
    remaining = {
        id(entry) for candidate in state._slots.values() for entry in candidate._pending_context
    }
    return [entry for entry in before if id(entry) not in remaining]


def _merge_card_identity(entry: dict[str, Any]) -> tuple[str, str] | None:
    """A merge-card context's ``(noteId, noteTranscript)``, or None for any other entry."""
    note_id = entry.get("noteId")
    transcript = entry.get("noteTranscript")
    if isinstance(note_id, str) and note_id and isinstance(transcript, str) and transcript:
        return note_id, transcript
    return None


def _same_merge_card(first: dict[str, Any], second: dict[str, Any]) -> bool:
    """Whether two context entries are one card's context on one transcript file.

    A card's ``noteTranscript`` is stamped by whichever path queued it: the flush
    writes the slot's live key (``slack:<ts>`` once bound), a restore writes the
    key it read the file by (``slack_<ts>``). Both name one file, so the card is
    matched by file, with the rule ``replacement_shares_transcript`` uses.
    """
    first_identity = _merge_card_identity(first)
    second_identity = _merge_card_identity(second)
    if first_identity is None or second_identity is None:
        return (first.get("noteId"), first.get("noteTranscript")) == (
            second.get("noteId"),
            second.get("noteTranscript"),
        )
    (first_id, first_transcript), (second_id, second_transcript) = first_identity, second_identity
    return first_id == second_id and transcripts_share_file(first_transcript, second_transcript)


def _merge_card_row_committed(conversation_log: Any, transcript: str, note_id: str) -> bool:
    """Whether a card's durable hold proves its visible row committed.

    The ``delivered`` stamp and the row land in one atomic file replacement.
    Read it under the transcript lock; any unreadable or malformed state is not
    proof and therefore keeps the hold for replay.
    """
    if conversation_log is None:
        return False
    try:
        with conversation_log._locked(transcript):
            metadata, readable = conversation_log.get_metadata_status(transcript)
    except Exception:  # noqa: BLE001 - failed proof keeps the durable hold
        return False
    if not readable:
        return False
    held = metadata.get("deferred_notes")
    return isinstance(held, list) and any(
        isinstance(entry, dict)
        and entry.get("id") == note_id
        and isinstance(entry.get("merged_from"), dict)
        and entry.get("delivered") is True
        for entry in held
    )


def _retire_merge_cards_with_live_rows(
    state: DashboardState, consumed: list[dict[str, Any]]
) -> list[tuple[str, str, list["_ChatSlot"]]]:
    """Retire the consumed cards whose row a live sharing slot's window holds; return the rest.

    A holder adds the card's id to ``_dropped_note_ids`` and is marked dirty, so
    its next full save writes the row and the retirement in one file replace (a
    rows-only save retires nothing). Synchronous, so a turn's ``finally`` can
    call it ahead of the steps that must not wait. The cards it returns, each as
    ``(note_id, transcript, sharing slots)``, have no live row holder and keep
    their durable hold until ``_record_consumed_merge_contexts`` proves the row
    commit.
    """
    retired = {identity for entry in consumed if (identity := _merge_card_identity(entry))}
    rowless: list[tuple[str, str, list["_ChatSlot"]]] = []
    for note_id, transcript in retired:
        sharing_slots = [
            candidate
            for candidate in state._slots.values()
            if transcripts_share_file(transcript, slot_history_key(candidate))
        ]
        holders = [
            candidate
            for candidate in sharing_slots
            if any(
                isinstance(message.get("meta"), dict) and message["meta"].get("noteId") == note_id
                for message in candidate.messages
            )
        ]
        if not holders:
            rowless.append((note_id, transcript, sharing_slots))
            continue
        for candidate in holders:
            candidate._dropped_note_ids.add(note_id)
            candidate._dirty = True
    return rowless


async def _record_consumed_merge_contexts(
    state: DashboardState, consumed: list[dict[str, Any]]
) -> None:
    """Schedule durable merge-card retirement once kiro-cli kept the prompt that carried the cards.

    A card retires through sharing slots whose in-memory window holds its row
    (``_retire_merge_cards_with_live_rows``). When no live sharing slot holds the
    row, retirement requires the durable hold's ``delivered`` stamp, written
    atomically with that row and read off the event loop. Failed proof keeps the
    hold so a restart replays the card instead of losing its only copy.
    """
    for note_id, transcript, sharing_slots in _retire_merge_cards_with_live_rows(state, consumed):
        if not await asyncio.to_thread(
            _merge_card_row_committed, state.conversation_log, transcript, note_id
        ):
            continue
        # A row-less card retires only after its durable hold proves the row
        # commit; no holder's save carries it, so no slot is marked dirty.
        for candidate in sharing_slots:
            candidate._dropped_note_ids.add(note_id)


def _restore_consumed_merge_contexts(slot: "_ChatSlot", consumed: list[dict[str, Any]]) -> None:
    """Put back at the queue front the merge context of a turn whose prompt the next turn's session lacks.

    Either kiro-cli did not keep the prompt (a Stop, an empty-response
    re-queue) or the session the next turn runs on does not hold it: the
    native conversation was discarded, the session was reset before the model
    finished answering, or the runtime died under the turn and the next claim
    replaces it (``chat_runner._native_session_lacks_the_prompt``, read by
    ``_merge_cards_reached_the_model``).
    Its seats stayed reserved while the turn ran (``has_pending_context_seat``
    counts ``_inflight_merge_contexts``), so putting it back evicts nothing.
    """
    queued = list(slot._pending_context)
    restored = []
    for entry in consumed:
        if not any(_same_merge_card(entry, present) for present in queued):
            restored.append(entry)
            queued.append(entry)
    slot._pending_context[:0] = restored


class TurnContext(NamedTuple):
    """What ``take_turn_context`` drained for one turn.

    ``prefix`` is the whole context prefix the turn prepends to its message.
    ``prefix_without_cards`` is the same drain with every merge card's frame left
    out, joined as ``prefix`` is: the message a turn that does not land replays
    verbatim must not carry its cards. A prompt kiro-cli kept already holds their
    frames, and one it did not keep has their contexts put back on the queue
    (``_restore_consumed_merge_contexts``) for the replay turn to drain like any
    turn, so a replayed message that still held their frames would hand the
    model a card twice. Plain
    pending context is kept: nothing puts it back, so the replayed message is
    the only way it reaches the model. ``consumed`` is the merge-card contexts
    the drain removed from a queue (``_consumed_merge_contexts``).
    """

    prefix: str
    prefix_without_cards: str
    consumed: list[dict[str, Any]]


def _drain_pending_frames(slot: "_ChatSlot") -> list[tuple[dict[str, Any], str]]:
    """Drain ``slot._pending_context`` into ``(entry, frame)`` pairs and clear the queue.

    The one place the frame format is written: ``drain_pending_context`` joins
    every frame, ``take_turn_context`` joins them twice, with and without the
    merge cards, so the two prefixes cannot drift apart. Expired entries
    (``maxAge`` elapsed) are discarded and get no pair.
    """
    # A note's halves resolve their destination here, not at the POST, so a slot
    # rebound since the write must not hand its content to the new session.
    slot.drop_foreign_authorized_notes()
    if not slot._pending_context:
        return []
    now = time.time()
    frames: list[tuple[dict[str, Any], str]] = []
    for entry in slot._pending_context:
        if context_entry_expired(entry, now):
            continue  # expired — silently discard
        # `or "app"` (not a dict default): api_chat_slot_context always writes
        # the key — as "" when the caller omitted it — so a plain .get() default
        # never fires and the header would render [Background context from ""],
        # an unattributed block under a "not authored by the user" claim.
        source = entry.get("source") or "app"
        frame = (
            f'[Background context from "{source}"]\n'
            f"{_CONTEXT_FRAME_CONTRACT}\n"
            f'{entry["content"]}\n'
            f"[End of background context]\n"
        )
        frames.append((entry, frame))
    slot._pending_context.clear()
    return frames


def _join_context_frames(frames: Iterable[str]) -> str:
    """The prepend-ready prefix for *frames*: empty when there is nothing to inject."""
    parts = list(frames)
    return "\n".join(parts) + "\n" if parts else ""


def drain_pending_context(slot: "_ChatSlot") -> str:
    """Drain ``slot._pending_context`` into a prepend-ready context prefix.

    Returns the concatenated ``[Background context from "<source>"] … [End of
    background context]`` blocks (empty string when there is nothing to inject)
    and clears the queue. Expired entries (``maxAge`` elapsed) are discarded.

    Each frame carries an explicit silent-consumption contract line
    (``_CONTEXT_FRAME_CONTRACT``) between the opening delimiter and the
    content. The endpoint's promise is *silent* background context, and the
    frame has to say so: without the contract, on a fresh session whose visible
    message is one short line, the agent recites the injected feature-request
    workflow verbatim as its reply — surfacing internal instructions in the
    transcript on every click of the header button. The contract is part of the
    frame, not any producer's payload, so every producer (app-kit context
    inject, artifact companion, Slack thread backfill, feature-request seed)
    is covered without each having to remember to say "don't echo this".

    Extracted from ``_run_chat`` so the entry contract — the ``content`` /
    ``source`` keys and the delimiter frame — is pinned by a unit test and
    shared by every producer (app-kit context inject, Slack thread backfill),
    rather than duplicated inline where a key rename could silently break a
    consumer while its producer's own tests stay green.
    """
    return _join_context_frames(frame for _entry, frame in _drain_pending_frames(slot))


def _session_card_holders(
    state: DashboardState, slot: "_ChatSlot"
) -> list[tuple["_ChatSlot", list[dict[str, Any]]]]:
    """The other same-app slots holding a live merge card stamped with *slot*'s session.

    Each pair is a slot of the same ``_app`` other than *slot* and the entries of
    its pending queue that carry a merge-card identity (``_merge_card_identity``),
    name *slot*'s session (``noteSession == effective_session_key(slot)``) and
    have not expired. Expiry is the mover's own predicate
    (``context_entry_expired``): an expired card is one
    ``adopt_alias_note_context`` refuses and that slot's own drain discards, so
    it holds nothing for this turn and opens no identity read.
    Pure: it reads the queues and changes nothing, so the turn can call it to
    decide whether an identity is needed at all and again to name the holders
    the mover gets.
    """
    live_session = effective_session_key(slot)
    now = time.time()
    holders: list[tuple[_ChatSlot, list[dict[str, Any]]]] = []
    for other in state._slots.values():
        if other is slot or not other._pending_context or other._app != slot._app:
            continue
        held = [
            entry
            for entry in other._pending_context
            if _merge_card_identity(entry) is not None
            and entry.get("noteSession") == live_session
            and not context_entry_expired(entry, now)
        ]
        if held:
            holders.append((other, held))
    return holders


async def resolve_turn_transcript_identity(state: DashboardState, slot: "_ChatSlot") -> str:
    """The transcript creation identity the turn on *slot* runs under, or ``""``.

    A slot that has hydrated or saved its transcript carries its ``created_at``
    as ``_disk_meta_created_at``, and that observed value is the answer with no
    read and no suspension. A slot minted beside a live twin by a create or a
    send that names the chat's key (``api_chat_slot_create``, ``api_chat``) has
    observed nothing yet, so on its first turn that field is empty although its
    transcript is on disk. Only then, and only when another same-app slot holds
    a merge card for this session (``_session_card_holders``), the identity is
    the ``created_at`` the conversation log reports for the slot's own transcript
    key (``ConversationLog.thread_transcript_identity``), read off the event loop
    in a worker thread (``asyncio.to_thread``), as ``_read_and_tighten_turn_execution``
    is: that read stats the file and, on a memo miss, opens it. The thread gets
    the plain transcript key, never the state or the slot. The ordinary turn, with
    no such holder, answers ``""`` without touching the log.

    The read returns ``None`` when no file exists, so a session whose transcript
    was permanently deleted answers ``""`` until a write mints the replacement
    file: the turn's own row append does that with a fresh ``metadata_now_iso()``,
    or a save does with the saving slot's own ``created_at``. A state with no
    conversation log, or a transcript whose metadata has no ``created_at``,
    answers ``""``.
    """
    observed = str(getattr(slot, "_disk_meta_created_at", "") or "")
    if observed:
        return observed
    conversation_log = getattr(state, "conversation_log", None)
    if conversation_log is None or not _session_card_holders(state, slot):
        return ""
    transcript_key = slot_history_key(slot)
    on_disk = await asyncio.to_thread(conversation_log.thread_transcript_identity, transcript_key)
    return on_disk if isinstance(on_disk, str) else ""


def adopt_alias_note_context(
    slot: "_ChatSlot", slots: Iterable["_ChatSlot"], *, slot_created_at: str
) -> int:
    """Move merge-card context queued on another slot of *slot*'s session onto *slot*.

    A merge card is delivered to the parent chat and its context is queued on one
    slot of the parent session (``_live_parent`` in ``chat_merge_back``). That
    session can be open in two slots: a workflow result whose originating chat
    was closed lands in a ``workflow-<run_id>`` slot bound to the chat's
    ``dashboard:`` session (``workflow_inject``). A History resume of that chat
    finds the workflow slot by its session (``_live_slot_for_resume``), but a
    create or a send that names the chat's own key (``api_chat_slot_create``,
    ``api_chat``) dedups by name alone and mints the chat beside it, and a hooked
    resume (session control's revive) publishes the chat after a result landed in
    the window that holds its slot retracted. Neither slot is
    ``channel_origin``, so ``_parent_not_dashboard_refusal`` admits the merge,
    and the person can take the next turn in either slot, the minted chat's first
    turn included. The card is context for the session's next turn, whichever
    slot runs it, so every entry that carries a merge-card identity
    (``_merge_card_identity``) and names *slot*'s session (``noteSession``) moves
    here, and the drain that follows delivers it once.

    *slot_created_at* is the turn slot's transcript creation identity as the
    caller resolved it (``resolve_turn_transcript_identity``: the ``created_at``
    the slot observed, else the one on disk for its transcript key, read off the
    event loop). This function owns the rule: an entry moves only when
    *slot_created_at* is non-empty and the holding slot observed the same
    ``_disk_meta_created_at``. A recycled session key therefore cannot bridge two
    transcript incarnations: a chat created on a deleted chat's key has no file
    until its first write (the turn's own row append mints it with a fresh
    ``metadata_now_iso()``, or a save does with the saving slot's own
    ``created_at``), so neither side of the comparison can ever be the deleted
    file's. An empty identity on either side fails closed. A transcript
    whose metadata has no ``created_at`` (written before the field existed)
    therefore never opens the fence, which is vacuous: the merge route proves the
    parent by that same identity (``_parent_identity`` in ``chat_merge_back``,
    UNPROVEN without it) and queues no card on such a session. Every other
    entry stays on the slot it was queued on: a plain ``/note``, app or subagent
    context is that slot's own, delivered by its own next turn, and nothing
    crosses an app boundary.

    An entry moves only while *slot*'s queue stays within what one slot may
    hold: ``_MAX_PENDING_CONTEXT`` entries, and ``_MAX_CONTEXT_PER_SOURCE`` live
    ones per source. The turn's prompt is then bounded as one queue is, and
    nothing acknowledged is dropped: an entry that does not fit stays on its
    own slot, in order, for a later turn, and an expired one stays for that
    slot's drain to discard. Returns how many entries moved.
    """
    live_session = effective_session_key(slot)
    now = time.time()
    queued = 0
    per_source: dict[str, int] = {}
    for entry in slot._pending_context:
        if isinstance(entry, dict) and not context_entry_expired(entry, now):
            queued += 1
            if source := entry.get("source"):
                per_source[source] = per_source.get(source, 0) + 1
    moved = 0
    for other in slots:
        if other is slot or not other._pending_context or other._app != slot._app:
            continue
        kept = []
        for entry in other._pending_context:
            movable = (
                isinstance(entry, dict)
                and _merge_card_identity(entry) is not None
                and entry.get("noteSession") == live_session
                and bool(
                    slot_created_at
                    and str(getattr(other, "_disk_meta_created_at", "") or "") == slot_created_at
                )
                and not context_entry_expired(entry, now)
            )
            source = entry.get("source") if movable else None
            fits = queued < _MAX_PENDING_CONTEXT and (
                not source or per_source.get(source, 0) < _MAX_CONTEXT_PER_SOURCE
            )
            if not (movable and fits):
                kept.append(entry)
                continue
            slot._pending_context.append(entry)
            queued += 1
            if source:
                per_source[source] = per_source.get(source, 0) + 1
            moved += 1
        if len(kept) != len(other._pending_context):
            other._pending_context[:] = kept
    return moved


def take_turn_context(
    state: DashboardState, slot: "_ChatSlot", *, transcript_identity: str
) -> TurnContext:
    """Drain the turn's context: the prefix, the prefix without its cards, and the cards taken.

    A merge card's context is queued on one slot of the parent session and the
    turn that reads it may run on another slot of that session
    (``adopt_alias_note_context``). Only a turn whose session holds a card moves
    entries between slots or reports cards consumed. The session's cards are the
    ones the adoption and the drain can take: every card in the turn slot's own
    queue, whatever ``noteSession`` it names (a card the slot's rebinding made
    foreign is dropped by that drain, and the drop retires it), and every card a
    same-app slot holds stamped with this slot's session
    (``_session_card_holders``). Finding them reads those queues without
    changing them. This function reads no transcript metadata itself:
    *transcript_identity* is the turn slot's transcript creation identity as the
    caller resolved it, once, with ``resolve_turn_transcript_identity`` (the
    ``created_at`` the slot observed, else, only when such a holder exists, the
    one the conversation log reports for its transcript key, read off the event
    loop). When any holder exists it is handed with the holder slots to
    ``adopt_alias_note_context``, which owns the rule that moves a card only
    between slots of one transcript incarnation. A card the rule refuses stays in
    its holder's queue and is not reported consumed. A turn whose session holds
    none, the ordinary turn, drains its own queue and changes no other.

    Returns a ``TurnContext``: the context prefix, the same prefix with every
    merge card's frame left out (a card the drain discarded wrote no frame, so
    the two differ by exactly the frames of the cards it delivered), and the
    merge-card contexts the drain removed from a queue, for
    ``slot._inflight_merge_contexts``: retired once kiro-cli kept the prompt
    carrying them (``_record_consumed_merge_contexts``, or
    ``_retire_merge_cards_with_live_rows`` from the turn's ``finally``), put back
    when it did not (``_restore_consumed_merge_contexts``).
    """
    cards = [entry for entry in slot._pending_context if _merge_card_identity(entry) is not None]
    holders = _session_card_holders(state, slot)
    for _holder, held in holders:
        cards.extend(held)
    if holders:
        adopt_alias_note_context(
            slot, [holder for holder, _held in holders], slot_created_at=transcript_identity
        )
    frames = _drain_pending_frames(slot)
    prefix = _join_context_frames(frame for _entry, frame in frames)
    prefix_without_cards = _join_context_frames(
        frame for entry, frame in frames if _merge_card_identity(entry) is None
    )
    consumed = _consumed_merge_contexts(state, cards) if cards else []
    return TurnContext(prefix, prefix_without_cards, consumed)


def _detach_appended_context(original: str, expanded: str) -> tuple[str, str]:
    """Return ``(original request, generated context)`` for append-only transforms.

    ``$skill`` and theme-persona producers return one concatenated
    string. The provider prompt now needs generated bytes BEFORE the request, so
    split them at their shared append-only seam. If a future producer stops
    preserving the original prefix, fail safe: keep the original as the request
    tail and move the whole transformed value into generated context.
    """
    if expanded == original:
        return original, ""
    if expanded.startswith(original):
        return original, expanded[len(original) :]
    logger.warning("generated request context stopped honoring append-only contract")
    return original, expanded
