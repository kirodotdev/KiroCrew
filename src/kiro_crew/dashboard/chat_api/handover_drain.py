"""The hand-over drain: ``_persist_handover_tail`` and the lost-prompt accounting it
reports through.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_handlers import (
        DashboardState,
        UnknownMemoryStore,
        _ChatSlot,
        _replacement_shares_transcript,
        _tighten_replacement_to_restricted_original,
        drained_to_thread,
        logger,
        restore_replacement_if_handover_did_not_land,
        save_slot_off_loop,
        slot_history_key,
        tighten_live_slot_memory_mode,
    )


class _HandoverDrainResult(NamedTuple):
    """What a hand-over drain did with the state it is the last reader of.

    ``rows_committed`` answers for the transcript rows: True when nothing was
    owed or the write committed, False when rows were owed and did not reach
    disk. ``prompts_lost`` counts the durable-eligible queued prompts whose only
    copy dies with the popped slot — zero when none were owed or when the
    durable line holds them, whichever writer put them there.

    A NamedTuple is always truthy, so a bare ``if not result:`` silently passes
    over a failed drain. Read ``rows_committed``; never test the result itself.
    """

    rows_committed: bool
    prompts_lost: int


async def _owed_prompts_lost_on_line(
    state: DashboardState, name: str, slot: _ChatSlot, history_key: str
) -> int:
    """Count owed queued prompts with no durable future on the shared line.

    The per-slot persistence signature answers "did THIS slot's last commit
    carry the queue"; it cannot see a replacement's own full save rebuilding
    the shared line and clearing ``queued_prompts`` (an owned field, so an
    emptied queue is cleared by absence — and ``POST /api/chat/slots`` persists
    at birth, inside the very window a hand-over spans). Nor is a point-in-time
    read of the line enough on its own: a line still showing the ORIGINAL's
    entries is one full save away from losing them whenever a live
    transcript-sharing holder exists, because that holder's save rebuilds the
    whole ``queued_prompts`` value from its own queue. So survival demands a
    stable owner, not a lucky read:

    * a live holder shares this transcript — an owed entry survives only when
      that holder's own durable queue carries it (a rehydrated holder restores
      the entries as queue cards and re-persists them; a fresh recreate does
      not), because the holder's next save decides the line;
    * no live sharing holder — the line is at rest, so an entry it holds stays
      until an ordinary restore hands it back.

    A line that cannot be read cannot prove survival, so every owed entry
    counts as lost: over-reporting is recoverable by the reader, while silence
    over a real loss is the failure this count exists to end.

    The store read is synchronous file I/O, so it goes through
    ``drained_to_thread`` rather than running on the gateway event loop —
    the same seam every other blocking read this module performs takes.
    """
    owed = slot.durable_queue_entries()
    if not owed:
        return 0
    holder = state._slots.get(name)
    if holder is not None and _replacement_shares_transcript(state, name, slot):
        surviving = {entry.get("id") for entry in holder.durable_queue_entries()}
        return sum(1 for entry in owed if entry.get("id") not in surviving)
    log = state.conversation_log
    if log is None:
        return len(owed)
    persisted, readable = await drained_to_thread(log.get_metadata_status, history_key)
    if not readable:
        return len(owed)
    on_line = persisted.get("queued_prompts")
    if not isinstance(on_line, list):
        return len(owed)
    line_ids = {entry.get("id") for entry in on_line if isinstance(entry, dict)}
    return sum(1 for entry in owed if entry.get("id") not in line_ids)


def _report_lost_queued_prompts(
    state: DashboardState, name: str, count: int, history_key: str
) -> None:
    """Log and post the user-visible notice that a hand-over lost queued prompts.

    The gateway log alone is not reachable by the person whose words were
    dropped; the notification feed is, so the loss is told in both. The body
    carries the COUNT and the slot, never the prompt text: the entries may
    belong to a restricted session, and a notice about losing words must not
    be the thing that leaks them.

    Failure to deliver must not fail the drain — the hand-over has to complete
    for the replacement holding the key either way — so this swallows and logs,
    the same posture every other lifecycle notice takes.
    """
    logger.warning(
        "Slot %s: %d queued prompt(s) were not carried by the hand-over write to "
        "%s; they are lost with the original slot",
        name,
        count,
        history_key,
    )
    try:
        state.notify(
            "agent",
            "Queued prompts lost in a tab hand-over",
            (
                f"{count} queued prompt(s) on tab {name!r} could not be carried "
                f"to {history_key} when the tab was replaced mid-close; they are "
                "not recoverable."
            ),
            meta={"slot": name, "count": count, "history_key": history_key},
        )
    except Exception:
        logger.error(
            "Slot %s: the lost-queued-prompts notification failed to deliver; "
            "the gateway log is the only remaining report of the loss",
            name,
            exc_info=True,
        )


async def _persist_handover_tail(
    state: DashboardState, name: str, slot: _ChatSlot
) -> _HandoverDrainResult:
    """Write a handed-over original's still-unsaved rows before its object is dropped.

    A teardown that yields ``name`` to a concurrent same-key recreate stops
    referencing the original slot: it is out of ``state._slots``, and the periodic
    flush iterates exactly that map, so nothing retries the write for it. Anything
    the original held past its last commit — ``messages[_disk_window_len:]``, plus
    any note the cleanup path is still carrying in ``_deferred_notes`` — would be
    unreachable and never persist. Those rows belong to the ORIGINAL's own
    transcript, whether or not the replacement happens to share it, and this frame
    is the last moment anything can put them there.

    The target is ``slot_history_key(slot)``, never ``_history_key_for(name)``. A
    slot carrying a ``linked_session_key`` — cron-, channel- or workflow-injected —
    stores its conversation under that key, and the forced save resolves its own
    write target the same way and REFUSES the whole write when the caller's
    ``expected_history_key`` names a different transcript. Authorizing
    ``dashboard:{name}`` there would make this drain a silent no-op for exactly the
    slots whose transcript is shared with something outside the dashboard, and would
    name a row-less file in the report.

    Deliberately NOT ``closed=True``. What this frame is finishing is the close of
    the ORIGINAL; the KEY is open, because a live replacement holds it, so the
    durable line has to say so. Stamping ``closed`` on a key someone is still using
    is the harm the surrounding guard exists to prevent. Open-shaped is not the same
    as un-closing, though: on a line the replacement published, ``closed`` is that
    holder's own dismissal, so the save defers it rather than erasing it (see
    ``ROWS_ONLY_OWNED_META_KEYS``). It is only on a line THIS slot published — where
    there is no other holder's flag to lose — that the write clears a stale
    ``closed`` an earlier close of the reused key left behind.

    Non-destructive against the replacement's own rows in both directions. The save
    re-serializes the ORIGINAL's window over the on-disk window region, and the
    save's foreign-append scan classifies every on-disk line that window does not
    represent as another writer's append and carries it through verbatim, so rows a
    replacement already committed survive.

    ``rows_only``, and that is the whole of what this frame claims. The rows are
    owed to the transcript; the METADATA line may not be this slot's to move.
    ``_save_slot_to_history`` is otherwise authoritative for every
    ``SLOT_OWNED_META_KEYS`` field and REBUILDS the line from whichever slot it is
    handed, so a default save here would revert a folder, pinned title, tag or pin
    the replacement had already published (``POST /api/chat/slots`` persists both at
    birth) — silently undoing an acknowledged edit, and for a tab nobody types in
    again undoing it for good. ``rows_only`` keeps the on-disk value for each of
    those — the close flags included — and leaves this write owning only the file's
    identity and accounting, which it carries forward from disk anyway.

    It rides with the write rather than being a caller's choice because the save
    scopes the deferral itself, by the line's ``tab_id``: it holds back only fields
    on a line ANOTHER slot published, and rebuilds normally from a line this slot
    published or from no line at all. That distinction is what keeps the flag from
    costing the original its own uncommitted metadata — a rename, re-file, tag or
    pin is acknowledged the moment it lands in memory and persists on a later
    flush, and this frame is past the pop, so no flush will ever visit this slot
    again.

    Returns a :class:`_HandoverDrainResult`. ``rows_committed`` is True when
    nothing was owed or the write committed, False when rows were owed and did
    not reach disk. Callers MUST honour it: nothing in the process can reach
    these rows again, so a caller that discards the answer reports a close that
    succeeded while the rows became unreachable. The log line names the exact
    count for the same reason.

    ``prompts_lost`` is the other half of the answer. A committed write can
    still defer the queue (the metadata line belongs to the replacement holding
    this key), a failed one leaves the line as it was, and a replacement's own
    full save can rebuild the shared line without the original's entries at any
    moment it remains alive — so survival is judged by who writes the line
    next (see :func:`_owed_prompts_lost_on_line`), not by this slot's
    persistence signature, which only answers for this slot's own last commit.
    An owed entry with no durable future dies with the popped object; the count
    comes back where a caller can surface or tally it, and the drain posts a
    dashboard notification alongside the warning log — the log is not reachable
    by the person whose words were dropped. Carrying the prompts instead would
    mean making ``queued_prompts`` a merge field on the rows-only path, which is
    a change to what a durable metadata line MEANS for a key two slots share;
    the write stays as it is, and the loss is reported rather than silent.
    """
    try:
        tightening = _tighten_replacement_to_restricted_original(state, name, slot)
    except UnknownMemoryStore:
        tightening = None
        logger.warning(
            "Slot %s: replacement rebound twice during tightening; writing the tail "
            "under the ratcheted line without tightening the live replacement",
            name,
        )
    try:
        slot.flush_deferred_notes()
    except Exception:
        # The flush puts the unwritten suffix back before raising, so this count is
        # what is still held. The hold also has a durable copy in the slot's
        # metadata line, so these notes are re-delivered after the NEXT gateway
        # restart rather than dying with the popped object — but nothing
        # in THIS process will visit this slot again, so for this lifetime they
        # are undeliverable and the log must still say so.
        logger.error(
            "Slot %s: %d held note(s) could not be flushed before the key was handed "
            "to a concurrent recreate; they are undeliverable until the persisted "
            "hold replays on the next restart",
            name,
            len(slot._deferred_notes),
            exc_info=True,
        )
    # ``_disk_window_len`` is how much of the current window the last committed save
    # covered, so the difference is exactly what has never reached disk. ``_dirty``
    # covers the other shape of unsaved state: an in-place edit to a row already
    # persisted leaves the length unchanged.
    # A restored sidebar row whose window never loaded holds no row that is not
    # already on disk, so its window as held (empty) is the whole answer, and
    # reading ``messages`` would load its transcript on the loop to learn it.
    window = (
        slot.loaded_messages() if getattr(slot, "window_pending", False) is True else slot.messages
    )
    unsaved = max(0, len(window) - slot._disk_window_len)
    # A queued prompt is unsaved state that changes NEITHER of those: its row is
    # written by the drain, so the window length is unchanged, and an enqueue does
    # not dirty the slot. Reporting a clean hand-over over that state would send
    # the prompt's only copy away with the discarded object, unremarked.
    history_key = slot_history_key(slot)
    if not unsaved and not slot._dirty and not slot.queue_persist_pending:
        # No write is needed, but "this slot committed its queue" is not the
        # same fact as "the entries have a durable future": a same-key recreate
        # persists at birth, and its full save rebuilds the shared line and
        # clears ``queued_prompts`` by absence. Survival is decided by whoever
        # writes the line next, so that is what gets checked.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)
    try:
        committed = await save_slot_off_loop(
            state,
            slot,
            closed=False,
            best_effort=False,
            expected_history_key=history_key,
            rows_only=True,
            # This frame IS the retraction's drain, so the close's guarded-write
            # fence does not apply to it: the close raised that fence, sequenced
            # this write itself, and nothing in the process can reach these rows
            # again. Refusing here would drop exactly the rows this function
            # exists to save.
            issued_by_the_retraction=True,
        )
    except Exception:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        logger.error(
            "Slot %s: %d unpersisted row(s) could not be written to %s while handing "
            "the key to a concurrent recreate; they are lost with the original slot",
            name,
            unsaved,
            history_key,
            exc_info=True,
        )
        # A failed write leaves the line as it was; whether an owed entry still
        # has a durable future is decided by who writes that line next, not by
        # this slot's own persistence signature, which cannot see a
        # replacement's rebuild clearing the shared key's queue.
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    if not committed:
        await restore_replacement_if_handover_did_not_land(state, name, tightening, history_key)
        # The save declined without writing: the session was permanently deleted
        # while this write awaited the lock, or the slot's routing moved off the
        # transcript this frame authorized. Neither leaves anywhere for these rows
        # to go, and the object holding them is about to be dropped.
        logger.warning(
            "Slot %s: %d unpersisted row(s) were not written to %s while handing the "
            "key to a concurrent recreate; the save declined the write",
            name,
            unsaved,
            history_key,
        )
        lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
        if lost:
            _report_lost_queued_prompts(state, name, lost, history_key)
        return _HandoverDrainResult(rows_committed=False, prompts_lost=lost)
    # Only a replacement that WRITES THIS TRANSCRIPT follows the committed rows'
    # mode: a slot at ``name`` whose rows route elsewhere (a task-review or
    # workflow slot with a divergent ``linked_session_key``) shares no file with
    # the original and must not be ratcheted for rows it will never read.
    if _replacement_shares_transcript(state, name, slot):
        tighten_live_slot_memory_mode(state, name, slot.memory_mode)
    # The write committed, and it can still leave owed entries with no durable
    # future: a rows-only save over a line another live slot published defers
    # every slot-owned field, ``queued_prompts`` among them
    # (``queue_line_is_ours`` keeps them owed rather than falsely credited),
    # and a live sharing replacement rebuilds the line on its every full save.
    # Survival is decided by who writes the line next — an entry a rehydrated
    # replacement carries in its own queue lives on as a queue card and is not
    # lost. Nothing in this process will visit this slot again, so a loss is
    # said with the count — the same obligation the held-note arm above
    # carries, and for the same reason: these are the user's own words and this
    # frame is their last reader.
    #
    # Carrying them instead would mean making ``queued_prompts`` a merge
    # field on the rows-only path, which is a change to what a durable
    # metadata line MEANS for a key two slots share, not a loop-side
    # ordering fix. The write stays rows-only; the remedy is the report, in
    # every register that can still carry it — the warning log, the
    # notification, and the count in the returned result.
    lost = await _owed_prompts_lost_on_line(state, name, slot, history_key)
    if lost:
        _report_lost_queued_prompts(state, name, lost, history_key)
    return _HandoverDrainResult(rows_committed=True, prompts_lost=lost)
