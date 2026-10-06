"""The refusals and witnesses that keep a save from writing the wrong thing.

A save runs on a worker thread while the event loop keeps mutating the slot, and a
close, a rebind or a permanent delete can land between its snapshot and its write.
This module owns the checks that decide whether the write may go ahead: the paired
window/queue snapshot and its bounded retry, the stale-queue witness, the rule that
a refusal keeps the state owed, the delete witness (and the lock-free probes fork
and transfer ask before republishing a slot's content), and the registry that makes
a guarded truncating write visible to a close.

New refusal paths for a save belong here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from kiro_crew.dashboard.chat_utils import effective_session_key, slot_history_key
from kiro_crew.execution_context import STRICTEST_MEMORY_MODE
from kiro_crew.history import METADATA_LINE_CORRUPT

if TYPE_CHECKING:
    import asyncio
    from pathlib import Path

    from kiro_crew.dashboard.state import DashboardState, _ChatSlot
    from kiro_crew.history import ConversationLog

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


# Bounded retries for taking a consistent (window, _disk_older_count) snapshot
# when _save_slot_to_history runs in the flush executor thread concurrently with
# event-loop mutations. A handful suffices — the only racing mutation is the
# rare >10000-message trim; retries just re-read until the two reads agree.
_FLUSH_SNAPSHOT_RETRIES = 4


def _stable_durable_queue(slot: _ChatSlot) -> tuple[list[dict], int]:
    """One self-consistent read of *slot*'s durable queue value and its count.

    ``durable_queue_entries`` builds its list entry by entry, so a drain landing
    mid-build could hand back a value the queue never held. Read it twice and
    keep going while the two disagree; the last read is returned when the budget
    is spent, because a self-consistent value is a best effort here, not a
    precondition — the caller-supplied-window path cannot prove its pairing with
    the queue in the first place (see ``expected_disk_older_count``).

    The candidate count rides along because the save SUBTRACTS the two to report
    an over-cap queue, and that difference is only true of one observation.
    """
    entries, candidates = slot.durable_queue_view()
    for _ in range(_FLUSH_SNAPSHOT_RETRIES):
        again, again_candidates = slot.durable_queue_view()
        if again == entries:
            return entries, candidates
        entries, candidates = again, again_candidates
    return entries, candidates


def _keep_owed_after_refusal(slot: _ChatSlot) -> None:
    """Keep a refused save's state owed to the next flush pass.

    A refusal is not a commit, but the periodic flush cannot see the difference:
    ``flush_slot_now`` clears ``_dirty`` on any return that did not raise, and it
    protects itself against clobbering a concurrent mark by comparing
    ``_dirty_gen`` rather than the flag. So a refused pass would clear the dirty
    bit over window rows it never wrote — and the queue signal cannot cover them,
    because the writer that overtook this one has already set
    ``_queue_persisted_sig``, leaving ``queue_persist_pending`` false and the next
    pass short-circuiting with nothing owed. The rows would then wait for the next
    append, and a restart before it loses committed transcript rows.

    Marking the slot dirty here is exactly the concurrent mark that comparison
    exists for: the flag stays true and the generation advances past the one the
    flush captured, so the next pass re-decides against the state that exists.
    Called by the retryable refusals: a stale queued-prompt snapshot, a slot
    replaced before the write committed, and a queue that moved during every
    window snapshot. The permanent declines (delete-won, routing moved) do not
    call it and keep their own semantics, so nothing has to tell a retryable
    refusal from a permanent one.
    """
    slot._dirty = True


def _line_is_this_slots(slot: _ChatSlot, existing_meta: dict) -> bool:
    """Was the metadata line on disk published by *slot* itself?

    ``tab_id`` is the only per-writer mark the line carries: it is minted per
    slot OBJECT (``get_or_create_slot`` assigns a fresh uuid; a rehydrate adopts
    the file's), and every save stamps the writer's own onto the line.
    """
    own_tab_id = getattr(slot, "_tab_id", "") or ""
    return bool(own_tab_id) and existing_meta.get("tab_id") == own_tab_id


def _queue_snapshot_is_stale(slot: _ChatSlot, queue_write_basis: str) -> bool:
    """Did another queue writer commit while this save held its snapshot?

    The transcript's file lock orders the queue writers' COMMITS, not their
    reads. The immediate write, the periodic flush pass and ``chat_summary``'s
    own flush each take their own paired (window, queue) snapshot off-loop, so
    the one holding the older queue can acquire the lock second and put that
    older value back on disk. The drift check leaves the newer value owed, so the
    next pass repairs disk — but a restart inside that interval loses an
    acknowledged prompt, which is the whole window the durable queue exists to
    close.

    The signal is the slot's own committed-queue witness, not a comparison
    against disk or against the live queue, because only the witness distinguishes
    the two ways a save's snapshot stops describing the present:

    * the QUEUE MOVED — a prompt arrived, or the drain popped one and appended
      its row. Ordinary, and this save's pair was taken while it held; writing it
      is committing a consistent past state the next pass supersedes;
    * another WRITER COMMITTED — the witness now names a value this save never
      read, so the value it holds is older than what is already durable and
      writing it would take an acknowledged prompt back off disk.

    Only the second is a loss, so only the second refuses: nothing written, the
    queue stays owed by the drift check, and the next pass re-decides against the
    state that exists. ``_queue_persisted_sig`` is written under the routing
    guard by every path that commits the key — the full save and the empty-window
    metadata merge — which is what makes it the whole writer set rather than the
    one this flag can see.
    """
    return slot._queue_persisted_sig != queue_write_basis


def session_transcript_remains(state: DashboardState, slot: _ChatSlot) -> bool:
    """Whether a transcript file is still on disk for this slot's key.

    A narrower question than :func:`session_was_deleted`, and a different one.
    That probe answers "may I republish this slot's content", and collapses three
    outcomes into ``True``: the file is GONE, the file belongs to a NEW
    incarnation, and existence is UNVERIFIABLE. Collapsing them is right there,
    because all three refuse the copy.

    A caller that has already written a transcript and is now refusing needs the
    distinction, because it decides what it may TRUTHFULLY say. Only "gone" lets
    it report that nothing was kept; the other two leave a file on disk that it
    must neither claim to have removed nor delete — a new incarnation belongs to
    somebody else, and an unverifiable read names nothing it can safely unlink.

    Fails CLOSED toward "something remains": any stat failure other than
    ``FileNotFoundError`` answers ``True``, because the dangerous direction here
    is claiming a clean slate that may not exist. A store with no path resolver
    answers ``False`` — there is no file it can name, so there is nothing to
    disclose.

    Lock-free and a point-in-time reading, exactly like the witness beside it.
    """
    if not state.conversation_log:
        return False
    path_fn = getattr(state.conversation_log, "_path", None)
    if path_fn is None:
        return False
    try:
        path_fn(slot_history_key(slot)).stat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    return True


def session_was_deleted(state: DashboardState, slot: _ChatSlot) -> bool:
    """True when this slot's session was permanently deleted out from under it.

    The same delete witness as the delete-won guard in
    :func:`_save_slot_to_history`, exposed for callers that REPUBLISH a slot's
    content (fork, transfer export) and cannot rely on observing the guard's
    ``False`` return: the periodic 5s flush can hit the guard first and clear
    ``_dirty``, after which those callers skip their own flush arm entirely and
    would copy from the in-memory window. This probe answers directly, however
    the flush ordering fell out. Same evidence rule: the slot must have
    OBSERVED its file on disk (``_disk_meta_created_at`` non-empty, or the
    ``_disk_meta_observed`` bit for legacy metadata that records no
    ``created_at`` — both recorded
    at the hydrate sites and at committed saves, nowhere else), so a fresh
    slot is never "deleted". Witnesses, in order: a missing file
    (``FileNotFoundError``; any other ``stat`` failure also refuses — the
    file's existence is unverifiable, same fail-closed rule as the metadata
    read), and an on-disk ``created_at`` that differs from the observed one (a
    fresh incarnation created after the delete). An UNREADABLE metadata line
    returns True — identity unverifiable, so the copy is refused (fork 409 /
    transfer ``SnapshotUnstable``, both retryable) rather than republishing.
    An EMPTY ``created_at`` is re-stated before it is trusted: being lock-free,
    this probe can have the delete land between its stat and its metadata read,
    and a file that has just vanished reads back as a genuine ``({}, True)``,
    so the empty answer alone cannot tell "legacy metadata" (fails open) from
    "deleted a moment ago" (must refuse).
    Lock-free: a permanent delete never un-happens, so a True is stable; a
    False can race a delete landing right after, which is the same residual
    as a delete landing right after the copy itself completed.
    """
    if not state.conversation_log:
        return False
    # Same evidence rule as the guard: the slot must have OBSERVED its file on
    # disk, and ``_disk_meta_created_at`` is that observation (recorded at the
    # hydrate sites and at committed saves, nowhere else). Identity alone is
    # the gate — the window counters take no part (fork/transfer set
    # ``_resumed_count`` optimistically after a best-effort save that may have
    # failed, and a restored zero-message session has all-zero counters).
    known = str(getattr(slot, "_disk_meta_created_at", "") or "")
    # Same widening as the guard: legacy metadata records no ``created_at``,
    # so the observation BIT carries the evidence there — the missing-file
    # stat below is the legacy delete witness, while the identity comparison
    # at the tail still requires the recorded ``known``.
    if not known and not bool(getattr(slot, "_disk_meta_observed", False)):
        return False
    path_fn = getattr(state.conversation_log, "_path", None)
    if path_fn is None:
        # A log without a path resolver (stub/alternate store) cannot witness a
        # delete — same fail-open-is-fail-safe rule as the OSError arm below.
        return False
    try:
        path_fn(slot_history_key(slot)).stat()
    except FileNotFoundError:
        return True
    except OSError:
        # Any other stat failure fails CLOSED, like the metadata read below:
        # the file's existence cannot be verified, so the copy is refused
        # (retryably) rather than republishing what may be a deleted
        # conversation from the surviving in-memory window.
        return True
    # The file exists but may be a fresh incarnation created by another writer
    # AFTER the delete (e.g. a channel/cron append) — same identity rule as the
    # save's own guard: the observed ``created_at`` differing from the
    # on-disk one means this slot's session was deleted and the file belongs
    # to a new one. Status form for the same reason as the guard: a transient
    # metadata read failure must not blank the comparison. UNREADABLE fails
    # CLOSED here too — the copy is refused (fork 409 / transfer
    # SnapshotUnstable, both retryable) rather than republishing content whose
    # identity cannot be verified.
    meta_fn = getattr(state.conversation_log, "get_metadata_status", None)
    if meta_fn is not None:
        try:
            current_meta, readable = meta_fn(slot_history_key(slot))
        except Exception:
            return True  # cannot verify identity — refuse the copy
        if not readable:
            return True
        current = str((current_meta or {}).get("created_at") or "")
        if not current:
            # An empty ``created_at`` is ambiguous, and this probe is
            # deliberately lock-free, so the delete can land BETWEEN the stat
            # above and this read: ``get_metadata_status`` reports a file that
            # has just vanished as ``({}, True)`` -- by its own contract a
            # GENUINE empty answer, not an unreadable one -- which would blank
            # the comparison below and answer "not deleted" for a session that
            # is gone. Re-stat to tell the two empties apart. The save's own
            # guard needs no equivalent: it reads the metadata and stats the
            # path inside ``_locked``, the lock ``delete_session`` unlinks
            # under, so no delete can interleave between its two reads.
            try:
                path_fn(slot_history_key(slot)).stat()
            except OSError:
                # Gone (``FileNotFoundError``) is the delete witness; any other
                # stat failure leaves existence unverifiable. Both refuse the
                # copy, exactly as the first stat's arms do.
                return True
            # Still there, so the empty ``created_at`` is a genuine legacy-
            # metadata answer, which fails OPEN by the documented rule.
            return False
        # Compare identities only when one was RECORDED (same rule as the
        # guard): a legacy observation cannot tell "the same legacy file,
        # stamped since by a sibling's save" from a fresh incarnation.
        if known and current != known:
            return True
    return False


def register_guarded_history_write(slot: _ChatSlot, save: "asyncio.Future[bool]") -> None:
    """Make a truncating write visible to a retraction of this slot's name.

    A close fences the slot, then waits for every future in this registry to
    finish before it pops the name. A write that is not registered here is
    invisible to that wait, so the close can pop while the write is still on its
    way to the rename and a same-name replacement then adopts the truncated
    transcript.

    The registry holds FUTURES, not a count: a count released in an awaiter's
    ``finally`` reads zero as soon as that awaiter is cancelled, while the worker
    thread it dispatched runs on. A future completes when the thread returns,
    whatever happened to the awaiter.

    Anything that is not a real set is treated as an absent registry: a
    compatibility caller may pass a slot double that synthesizes attributes, and
    a synthesized object must not reach the done callback.
    """
    writes = getattr(slot, "_guarded_history_writes", None)
    if not isinstance(writes, set):
        writes = set()
        slot._guarded_history_writes = writes
    writes.add(save)
    save.add_done_callback(writes.discard)


def paired_window_snapshot(
    slot: _ChatSlot,
    messages: list[dict] | None,
    expected_disk_older_count: int | None,
) -> tuple[list[dict], list[dict], int, int] | None:
    """One save's ``(window, queue, queue candidates, disk_older)``, or ``None``.

    ``None`` refuses the save: nothing may be written, the reason is logged here,
    and the refusal the periodic flush could read as a commit keeps the state owed.
    *expected_disk_older_count* is :func:`_save_slot_to_history`'s pairing
    contract for a caller-supplied *messages* snapshot.
    """
    # Snapshot the window and its disk-older count CONSISTENTLY. The save
    # may run in the flush executor thread while _flush_segment (reassigns
    # slot.messages) or append (trims the front AND bumps _disk_older_count)
    # run on the event loop. A trim is the only mutation that changes the
    # window/_disk_older_count relationship, so we read _disk_older_count,
    # snapshot the window, then confirm _disk_older_count is unchanged; a small
    # bounded retry closes the race without locks (slot._lock is an asyncio.Lock
    # and so cannot be acquired from this thread). An explicit snapshot is
    # internally consistent by construction, but its PAIRING with the frozen
    # prefix boundary is not -- see the save's ``expected_disk_older_count``.
    #
    # The QUEUE is snapshotted in the same stretch, and for the same reason at a
    # different boundary: the drain pops an entry and appends its user row in
    # one event-loop step, so the two halves only ever agree in a pair taken
    # while no drain ran between them. Reading the queue separately from the
    # window is what lets a file commit BOTH halves missing -- window frozen
    # before the drain, queue read after it -- which is the prompt disappearing
    # with no row, the exact loss this key exists to prevent. The pair is
    # therefore proven, not assumed: read the queue, snapshot the window, read
    # the queue again, and retry while the two queue reads disagree.
    if messages is not None:
        window = list(messages)
        # A caller-supplied window was frozen before this call, so the save
        # cannot prove ITS pairing with the queue -- the same limitation the
        # frozen-prefix boundary has here, which is why callers that freeze
        # across an await pass ``expected_disk_older_count``. Take the queue as
        # a self-consistent value and let the caller own the pairing.
        queue_snapshot, queue_candidates = _stable_durable_queue(slot)
        disk_older = slot._disk_older_count
        if expected_disk_older_count is not None and disk_older != expected_disk_older_count:
            # The window hit the cap and trimmed while this save was in flight,
            # so the trimmed rows are now credited to the frozen prefix AND
            # still present at the head of the frozen snapshot. Writing would
            # duplicate them. Refuse like the save's other guards: nothing written, and
            # the caller (which holds the retryable-503 contract) re-decides
            # against the state that actually exists.
            logger.warning(
                "Slot %s save refused: frozen prefix moved from %d to %d during the write",
                slot.key,
                expected_disk_older_count,
                disk_older,
            )
            return None
    else:
        for _ in range(_FLUSH_SNAPSHOT_RETRIES):
            disk_older = slot._disk_older_count
            queue_snapshot, queue_candidates = slot.durable_queue_view()
            window = list(slot.messages)
            if (
                slot._disk_older_count == disk_older
                and slot.durable_queue_entries() == queue_snapshot
            ):
                break
        else:
            disk_older = slot._disk_older_count
            queue_snapshot, queue_candidates = slot.durable_queue_view()
            window = list(slot.messages)
            if slot.durable_queue_entries() != queue_snapshot:
                # The pair could not be proven inside the retry budget. Refuse
                # rather than commit a file that may show neither the entry nor
                # its row: nothing is written, the queue stays owed by the drift
                # check, and the next flush pass re-decides against the state
                # that actually exists.
                logger.warning(
                    "Slot %s save refused: the queue moved during every window snapshot",
                    slot.key,
                )
                _keep_owed_after_refusal(slot)
                return None
    return window, queue_snapshot, queue_candidates, disk_older


def routing_snapshot(slot: _ChatSlot) -> tuple[str, str]:
    """``(live_session, history_key)``, both taken from ONE observation of the routing."""
    # Authorization and the write target must come from ONE observation of the
    # routing. Both keys derive from ``slot.linked_session_key``, which the event
    # loop rebinds with no running gate, so reading it per row -- or again when
    # the write target is resolved -- authorizes rows against one session and
    # then writes the file of another. Snapshot-then-confirm with the same
    # bounded retry the window pair uses. The two keys
    # stay DISTINCT: collapsing them would send a channel-born slot the
    # dashboard could not bind to the phantom file ``slot_history_key`` exists
    # to avoid.
    for _ in range(_FLUSH_SNAPSHOT_RETRIES):
        routing = getattr(slot, "linked_session_key", "")
        live_session = effective_session_key(slot)
        history_key = slot_history_key(slot)
        if getattr(slot, "linked_session_key", "") == routing:
            break
    return live_session, history_key


def drop_notes_authorized_elsewhere(
    slot: _ChatSlot, window: list[dict], live_session: str
) -> list[dict]:
    """*window* without the note rows authorized for a session other than *live_session*."""
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    # Filter the SNAPSHOT, never slot.messages: this may run in the flush
    # executor thread, where mutating the live window is exactly the race the
    # window snapshot exists to avoid. A note row whose slot was rebound after
    # the write must not be persisted into the session it now routes to; the
    # drain drops it from the live window on the event loop.
    kept = [m for m in window if not cp._note_authorized_elsewhere(m.get("meta"), live_session)]
    dropped_notes = len(window) - len(kept)
    window = kept
    if dropped_notes:
        # Count-gated exactly like the drain's own denial at state.py:2320. This is
        # the PERIODIC save path, so an ungated emit would record a denial on every
        # save of every slot, and the same row would re-emit on each one until the
        # loop-side drain removes it from slot.messages. ``critical`` stays default
        # False: a denial must never be able to fail a save that is otherwise
        # correct. Nothing raises or returns early here either, so the authorized
        # remainder still persists. Called directly rather than through loop
        # plumbing because sel is pure threading -- unlike the slot's asyncio lock,
        # it is safe from this executor thread. Only the slot key and a count
        # are recorded; note content never enters the audit line.
        cp.sel().log_api_access(
            caller="dashboard",
            operation="note_save_drop",
            outcome="denied",
            source="app_isolation",
            resources=f"slot={slot.key} dropped={dropped_notes}",
            error="slot was rebound to another session after the note was written",
        )
        logger.warning(
            "Slot %s dropped %d note row(s) from a save: authorized elsewhere, "
            "slot now routes to %s",
            slot.key,
            dropped_notes,
            live_session,
        )
    return window


def read_line_for_save(
    conv_log: ConversationLog, slot: _ChatSlot, history_key: str
) -> tuple[dict, bool, bool]:
    """``(existing_meta, readable, corrupt)`` for the line a full save folds.

    Called under the transcript lock. Raises ``OSError`` for a transiently
    unreadable line, so the save fails and stays owed instead of relabelling a
    line it cannot read; a CORRUPT line comes back as the strictest-mode stand-in
    the save rewrites it under.
    """
    # Status form, not bare ``get_metadata``: the delete-won identity
    # comparison below is exactly the "empty result triggers something
    # destructive" case that ``get_metadata_status`` exists for — a
    # transient read failure returns ``{}`` from the bare getter,
    # indistinguishable from "no metadata", which would blank the
    # identity check and let a pending save overwrite a replacement
    # session with deleted content.
    existing_meta, _meta_readable = conv_log.get_metadata_status(history_key)
    # Set when the first line is bytes that are not JSON: this save
    # REWRITES it (below) instead of deferring, because no retry will
    # ever read it and a save that keeps deferring never persists a row
    # again -- ``closed`` never lands and the tab resurrects on restart.
    _line_corrupt = False
    if not _meta_readable:
        # The line EXISTS but could not be read (``get_metadata_status``
        # reports an absent file as readable-and-empty). Everything
        # the save folds from ``existing_meta`` -- the ``memory_mode`` ratchet
        # above all -- and an empty dict would read as ``persistent``,
        # so a transient read failure on a restricted line would let this
        # save relabel it persistent and stamp a store name on it: the
        # one write the ratchet exists to make impossible. Fail CLOSED
        # the way the identity check below does: raise, so the outer
        # handler leaves ``_dirty`` armed and the flush retries once the
        # read clears, rather than returning False and dropping rows.
        # Only a line the companion state read calls CORRUPT is treated
        # otherwise; a disagreement between the two reads (the file
        # changed in between) stays transient.
        if conv_log.metadata_line_state(history_key) != METADATA_LINE_CORRUPT:
            raise OSError(
                f"history metadata for {history_key} is transiently unreadable; "
                "cannot vouch for the line's privacy contract before writing -- "
                "save deferred for retry"
            )
        logger.warning(
            "Slot %s: history metadata line for %s is corrupt (not JSON); "
            "rewriting it under the %s mode so the transcript's rows can land",
            slot.key,
            history_key,
            STRICTEST_MEMORY_MODE,
        )
        _line_corrupt = True
        # The line as this save will rebuild it, in place of the empty
        # dict: no identity (``created_at`` comes from the slot's own),
        # no store, and the STRICTEST mode. The line's real contract is
        # unknowable and the ratchet forbids relabelling it looser, so
        # the strictest mode is the only value the rewrite may carry;
        # every fold the save makes -- the full rebuild's, the rows-only carry's --
        # reads it from here. The rows after the corrupt line are kept
        # by the frozen-prefix read, which skips the first line by
        # position.
        existing_meta = {"memory_mode": STRICTEST_MEMORY_MODE}
    return existing_meta, _meta_readable, _line_corrupt


def delete_won(
    slot: _ChatSlot,
    path: Path,
    existing_meta: dict,
    *,
    meta_readable: bool,
    line_corrupt: bool,
    history_key: str,
) -> bool:
    """Whether a permanent delete won the race this save lost; logged when it did.

    Called under the transcript lock. Raises ``OSError`` when the file exists but
    its identity cannot be read, so the save stays owed instead of writing over an
    incarnation it cannot verify.
    """
    # ── Delete-won guard ────────────────────────────────────────────
    # ``delete_session`` unlinks the session file under the SAME
    # ``_locked`` region and deliberately leaves no tombstone (its
    # docstring notes a concurrent writer can recreate the session
    # once it releases the lock). ``save_slot_off_loop`` routes
    # on-loop callers to a worker thread that takes the PATIENT
    # acquire, so this save can legitimately sit waiting while a
    # permanent delete runs to completion ahead of it — writing now
    # would silently undo a delete that already reported success,
    # resurrecting the conversation in Older Sessions. A missing
    # file alone is NOT that signal: a brand-new slot's first save
    # also starts with no file. The abort therefore requires
    # evidence that this slot has OBSERVED its session file on disk
    # (``_disk_meta_created_at`` or ``_disk_meta_observed``, see
    # below). A fresh slot has neither and proceeds with a normal
    # first create. (``path`` is resolved after the delete, so for a
    # legacy-aliased Slack key it may name the canonical file rather
    # than the legacy one the delete unlinked — both are gone, so
    # the answer is the same.) Returning cleanly (no mkdir, no
    # write, no raise) lets the flush loop clear ``_dirty`` so the
    # delete's reported success stands; the ``False`` return lets
    # callers that must CONFIRM durability (fork, transfer export)
    # distinguish this skip from a committed write. Only a missing
    # file counts as the delete witness — any other ``stat`` failure
    # (permissions, device not ready) propagates to the outer
    # handler, which re-raises and leaves the retry armed.
    _delete_won = False
    # Evidence is the slot having OBSERVED its file on disk, and
    # ``_disk_meta_created_at`` is that observation: recorded exactly
    # at the hydrate sites and at each committed save, nowhere else.
    # Identity ALONE is the gate. The window counters take no part in
    # it, in either direction: fork/transfer set ``_resumed_count``
    # optimistically after a best-effort first save (a transient
    # failure would read as "was on disk, now gone" and eat the retry
    # best-effort re-armed), and a restored ZERO-message session has
    # all-zero counters while its delete must still win against the
    # save of its first message.
    _known = slot._disk_meta_created_at
    # ``created_at`` is the identity, but legacy metadata carries none
    # — the observation BIT is the evidence there, so a save racing a
    # permanent delete cannot recreate a legacy transcript through the
    # "no identity recorded" gap. The missing-file witness needs only
    # the observation; the identity COMPARISON below still needs the
    # recorded ``created_at``.
    if _known or slot._disk_meta_observed:
        try:
            path.stat()
        except FileNotFoundError:
            _delete_won = True
        else:
            # The file EXISTS but may not be the one this slot knows: a
            # permanent delete followed by another writer's append (a
            # channel/cron ``append_off_loop``) creates a FRESH file
            # with a new metadata ``created_at``. Merging this slot's
            # window into that file would restore the deleted
            # conversation into the new transcript. ``created_at`` is
            # the file's identity — the save always carries the on-disk
            # value forward, so for a continuously-existing file it
            # never changes. An UNREADABLE metadata line fails CLOSED:
            # the identity cannot be verified, so the save must not
            # proceed — raising (rather than returning False) leaves
            # ``_dirty`` armed via the outer handler, so the flush
            # retries once the transient read failure clears, instead
            # of the delete-won path discarding the content. A
            # readable-but-absent ``created_at`` (legacy meta) fails
            # open for an EXISTING file only — a missing file is the
            # legacy delete witness via the observation bit above. A
            # CORRUPT line (``line_corrupt``) carries no identity to
            # compare and is rewritten by this save; it fails open here
            # like a legacy line does, since a fresh incarnation minted
            # by another writer after a delete starts with a valid line.
            if not meta_readable and not line_corrupt:
                raise OSError(
                    f"history metadata for {history_key} is transiently "
                    "unreadable; cannot verify the session's identity "
                    "before writing — save deferred for retry"
                )
            _current = str(existing_meta.get("created_at") or "")
            # Compare identities only when one was RECORDED: a legacy
            # observation (``_known`` empty) cannot distinguish "the
            # same legacy file, stamped with a ``created_at`` by a
            # sibling's save since" from "a fresh incarnation born
            # after a delete" — fail open for the existing file,
            # matching the documented legacy behavior above. The
            # missing-file witness is the legacy delete evidence.
            if _known and _current and _current != _known:
                _delete_won = True
    if _delete_won:
        # WARNING, with the slot key: for a slot the delete's cleanup
        # could not pop (e.g. a cron-linked tab whose slot key does not
        # match any spelling the cleanup probes), every later save of
        # new activity aborts here — the slot's in-memory content is
        # not durable, and this line is the only operator-visible
        # evidence of that.
        logger.warning(
            "Skipping history save for %s (slot=%s): the session was "
            "permanently deleted while this save awaited the lock",
            history_key,
            slot.key,
        )
    return _delete_won
