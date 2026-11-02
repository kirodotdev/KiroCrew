"""The dashboard slot save transaction.

``_save_slot_to_history`` (the snapshot, the refusals, the transcript lock, the
atomic replace and the witness stamping), its on-loop entry point
``save_slot_off_loop`` with the close fence it applies, the shutdown save of every
slot, and the wait for the turns ``close_all`` cut off. Composed onto
:mod:`kiro_crew.dashboard.chat_persistence`; see this package's ``__init__``. New
save-path orchestration goes here; a new rule the save consults goes to its
``slot_persistence`` owner.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_persistence import (
        DashboardState,
        _ChatSlot,
        _display_verdicts,
        _keep_owed_after_refusal,
        _line_is_this_slots,
        _metadata_line,
        _queue_snapshot_is_stale,
        _record_pending_memory_mode,
        _transcript_merge,
        _write_guards,
        apply_pending_slot_memory_mode,
        atomic_write,
        is_channel_session_key,
        logger,
        queue_persist_signature,
        register_guarded_history_write,
        slot_history_key,
        transcript_lock_stems,
    )


def save_all_slots_to_history(state: DashboardState) -> None:
    """Save all active slots to history. Called on gateway shutdown."""
    for slot in list(state._slots.values()):
        try:
            _save_slot_to_history(state, slot, force=True)
        except Exception:
            logger.error("Shutdown: failed to save slot %s", slot.key, exc_info=True)
    # Snapshot the open-tab set so the next startup restores them. This is
    # belt-and-braces vs the periodic flush snapshot — it ensures graceful
    # shutdown captures the very latest state, including tabs whose
    # _dirty was False but were still visually present in the sidebar.
    try:
        state._persist_open_slots()
    except Exception:
        logger.debug("Shutdown: open_slots snapshot failed", exc_info=True)
    # Same reasoning for the context-meter readings: a graceful restart is the
    # case the reopen seed exists to serve, so the last reading must reach disk
    # rather than waiting for a periodic flush that will not come.
    try:
        state._persist_context_snapshots()
    except Exception:
        logger.debug("Shutdown: context snapshot flush failed", exc_info=True)


async def wait_for_unwinding_turns(state: DashboardState | None) -> None:
    """Wait for the dashboard turns ``sessions.close_all()`` cut off to finish unwinding.

    The graceful shutdown, the update restart, the dashboard restart and the Slack
    restart each run :func:`save_all_slots_to_history`, then ``close_all()``, then
    exit or exec, and each awaits this right after ``close_all()``. A turn that was
    mid-reply saves the text its user watched stream as it unwinds, after that first
    save, and the exit or exec would otherwise beat it.

    Bounded by ``DASHBOARD_TURN_UNWIND_SECS``, and never cancelling: a turn still
    running at the deadline is left to the exit.
    """
    slots = getattr(state, "_slots", None)
    if not isinstance(slots, dict):
        return
    unwinding = {
        task
        for task in (getattr(slot, "task", None) for slot in list(slots.values()))
        if isinstance(task, asyncio.Future) and not task.done()
    }
    if not unwinding:
        return
    from kiro_crew import gateway_shutdown_budget

    await asyncio.wait(unwinding, timeout=gateway_shutdown_budget.DASHBOARD_TURN_UNWIND_SECS)


# Transient/streaming roles that are never persisted (mirrors
# ``_build_message_entry``). A window-region disk line carrying one of these is
# not a real message and is never treated as a cross-process append to preserve.
# Canonically defined in ``state`` (imported above) so the trim path that must
# count durable rows shares the same set; re-exported here for this module's
# other readers (session_control, chat_handlers).


def _save_slot_to_history(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    closed_at: float | None = None,
    force: bool = False,
    rewrite: bool = False,
    expected_history_key: str | None = None,
    expected_disk_older_count: int | None = None,
    expected_slot_name: str | None = None,
    rows_only: bool = False,
    pending_mode_slot: _ChatSlot | None = None,
    mutes_opened_override: bool | None = None,
    muted_override: bool | None = None,
    after_commit_under_lock: Callable[[], None] | None = None,
    refuse_stale_empty_merge: bool = False,
    turn_timeout_override: tuple[int | None] | None = None,
    retired_rows: list[dict] | None = None,
) -> bool:
    """Persist slot messages to JSONL history (append-safe).

    ``retired_rows``: with an explicit *messages* snapshot, the rows the
    snapshot removes on purpose. The rewrite then still preserves rows another
    writer appended after the snapshot was taken (see ``compose_payload``);
    without it a rewrite drops every window-region line the snapshot lacks.

    ``refuse_stale_empty_merge``: let the empty-window merge of a message-less
    slot refuse a stale queue snapshot or a replaced slot, as the full save
    does. Only the periodic flush passes it, because it re-runs a refused pass
    on its next tick. The forced route saves (tag, folder, pin, transfer) keep
    committing their merge unconditionally, so none of them can newly report an
    acknowledged edit as refused.

    The session file is modeled as **frozen prefix + live window**:

    - The **frozen prefix** is the first ``slot._disk_older_count`` on-disk
      message lines — the turns OLDER than the in-memory window (set at
      restore/resume). These bytes are read verbatim and NEVER rewritten, so a
      restart that loaded only a recent window cannot destroy older
      history.
    - The **live window** is ``slot.messages`` (small, ~500 messages). It is
      re-serialized in full on every save. Re-serializing the whole window means
      in-place edits to already-shown messages (stop-event resolution, file-change
      chips, mcp_oauth banner completion) and any reordering done by
      ``_flush_segment`` all persist correctly — there is no position counter to
      get out of sync.

    The default save writes ``meta + frozen_prefix + serialize(window)``.

    Pass ``rewrite=True`` (or an explicit *messages* snapshot, which implies it)
    for operations that INTENTIONALLY truncate the window (rewind/regenerate/
    fork): the file is rebuilt as ``meta + frozen_prefix + serialize(snapshot)``
    and the dropped window tail is archived first via ``_archive_dropped_lines``.

    Concurrency: ``_flush_dirty_slots`` runs this in an executor thread
    while ``_run_chat`` mutates ``slot.messages`` on the event loop. We snapshot
    ``list(slot.messages)`` (a single GIL-atomic attribute read) and the matching
    ``slot._disk_older_count`` up front, then operate only on that snapshot, so a
    concurrent ``_flush_segment`` reassigning ``slot.messages`` cannot interleave
    with the read-serialize-write and skip/duplicate a message.

    Operates ONLY on this slot's own single session file (``_path(history_key)``);
    tab_id chaining is 1:1 (a slot's tab_id maps to exactly one file — fork makes
    a fresh slot with its own file), so this never reads/writes a sibling and
    legacy no-tab_id sessions stay isolated.

    ``rows_only``: persist the window but leave the metadata line's slot-owned
    fields as they are on disk, keeping authority over only
    :data:`~kiro_crew.history.ROWS_ONLY_OWNED_META_KEYS`. It exists for the one
    caller whose slot is not the transcript's only writer: the close/cleanup
    hand-over drain, which writes a popped slot's unsaved rows onto a transcript a
    concurrent same-key replacement now holds. The default rebuild would revert
    whatever that replacement had already published (a folder or a pinned title
    from ``POST /api/chat/slots``, a tag, a pin), so the rows move and the line does
    not. The deferred set is
    :data:`~kiro_crew.history.ROWS_ONLY_DEFERRED_META_KEYS`, which is wider than the
    owned fields alone: a title's provenance and refresh budget describe the title
    and travel with it. It includes ``closed``/``closed_at``, so an open-shaped
    rows-only write does not erase a dismissal the replacement committed while this
    one was in flight.

    The deferral is conditional on there being another writer to defer to, decided
    from the line's ``tab_id``: a line this slot published itself, or no line at
    all, gets the ordinary rebuild. Otherwise the flag would cost the popped slot
    its own uncommitted metadata — an edit is acknowledged when it lands in memory
    and persists on a later flush, and after the pop no flush ever visits that slot
    again.

    ``expected_disk_older_count`` pairs an explicit *messages* snapshot with the
    ``slot._disk_older_count`` the caller observed in the SAME synchronous stretch
    it froze that snapshot in. A snapshot is internally consistent by
    construction, but the frozen-prefix boundary it must be written against is
    not: a concurrent ``append`` at the window cap trims the front and credits
    the trimmed rows to ``_disk_older_count``, so a snapshot frozen before that
    trim, written against the counter read after it, emits those rows twice —
    once in the frozen prefix and once at the head of the snapshot. Supplying the
    paired count makes that drift refuse the save (``False``, nothing written)
    instead of committing a duplicated transcript; a caller that reads the live
    counter itself cannot detect the drift at all. Ignored without *messages*,
    where the bounded retry below already takes both halves together.

    Returns ``False`` when the delete-won guard aborted the save because the
    session was permanently deleted while this save awaited the lock, when
    ``expected_history_key`` does not match the slot's routing, or when
    ``expected_disk_older_count`` drifted — the in-memory window was NOT
    persisted and must not be treated as durable. Every other completion
    (including the benign no-op skips) returns ``True``.
    """
    if not state.conversation_log:
        return True
    pending_mode_target = pending_mode_slot or slot
    # An explicit message snapshot always means "this is the full authoritative
    # window state" → rewrite. Edit paths (rewind/regenerate/fork) pass a snapshot.
    # A slot left in _pending_rewrite by a failed inline rewrite also takes
    # the archive-safe rewrite path until it succeeds.
    if messages is not None or slot._pending_rewrite:
        rewrite = True
    # The committed-queue witness as it stands BEFORE this save reads the queue,
    # so the guard inside the lock can tell "another writer committed since" from
    # "the queue moved since". Taken first, which is the conservative order: a
    # writer that commits between here and the read costs a refused pass, never a
    # committed value this save could not prove.
    queue_write_basis = slot._queue_persisted_sig
    # Read before the copy, so the check under the lock can tell a window
    # copied before a Regenerate swap committed (see ``snapshot_predates_swap``).
    swap_basis = _write_guards.swap_basis(slot)
    snapshot = _write_guards.paired_window_snapshot(slot, messages, expected_disk_older_count)
    if snapshot is None:
        return False
    window, queue_snapshot, queue_candidates, disk_older = snapshot
    note_auth_key, history_key = _write_guards.routing_snapshot(slot)
    if expected_history_key is not None and history_key != expected_history_key:
        # The caller authorized a write against a specific transcript and the
        # slot's routing moved before this snapshot (a rebind on the event
        # loop wins any race with this worker). Writing would land the
        # caller's mutation on a transcript it never authorized -- refuse the
        # whole save instead, exactly like the delete-won guard: return False
        # with nothing written, and let the caller roll back and re-decide.
        logger.warning(
            "Slot %s save refused: routing moved from %s to %s during the write",
            slot.key,
            expected_history_key,
            history_key,
        )
        return False
    window = _write_guards.drop_notes_authorized_elsewhere(slot, window, note_auth_key)

    # The two retryable refusals, evaluated under the transcript lock by both the
    # empty-window merge and the full save below. Each returns the reason to log,
    # or ``None`` to proceed.
    def _queue_line_is_ours(meta: dict) -> bool:
        # A rows-only write over another holder's line defers the queue key to
        # that line, so it neither decides nor commits the queue.
        return not (rows_only and meta and not _line_is_this_slots(slot, meta))

    def _stale_queue_refusal(meta: dict) -> str | None:
        # The lock orders the queue writers' commits, not their reads, so a
        # writer holding an older queue can arrive here second.
        if _queue_line_is_ours(meta) and _queue_snapshot_is_stale(slot, queue_write_basis):
            return (
                "another writer committed a newer queued-prompt value "
                "while this save held an older snapshot"
            )
        return None

    def _replaced_refusal() -> str | None:
        if expected_slot_name is not None and state._slots.get(expected_slot_name) is not slot:
            return f"slot {expected_slot_name} was replaced before the write committed"
        return None

    if not window:
        # A restored row whose window is still pending has every row on disk
        # already, so anything that dirtied it is a metadata edit (a rename, a
        # colour) and the metadata-only merge is its whole save.
        if force or closed or (getattr(slot, "window_pending", False) and slot._dirty):

            def _refusal_under_lock(meta: dict) -> str | None:
                return _stale_queue_refusal(meta) or _replaced_refusal()

            refusal = _metadata_line.merge_empty_window(
                state.conversation_log,
                slot,
                history_key,
                live_session=note_auth_key,
                queue_snapshot=queue_snapshot,
                closed=closed,
                closed_at=closed_at,
                pending_mode_target=pending_mode_target,
                # Only the periodic flush opts in. A close commits
                # unconditionally, as it does on main: its callers ignore a
                # refused save, so a refusal would leave an open-shaped line
                # that a restart resurrects.
                refusal_under_lock=(
                    _refusal_under_lock if refuse_stale_empty_merge and not closed else None
                ),
                # A route save (tag, folder, pin, transfer) is not refused, but
                # an older queue snapshot must not overwrite a newer committed
                # queue either: the route's edit commits and the queue key is
                # left as it is on disk, still owed to the next save.
                queue_deferred_under_lock=(
                    (lambda meta: _stale_queue_refusal(meta) is not None)
                    if not refuse_stale_empty_merge and not closed
                    else None
                ),
                mutes_opened_override=mutes_opened_override,
                muted_override=muted_override,
                after_commit_under_lock=after_commit_under_lock,
                turn_timeout_override=turn_timeout_override,
            )
            if refusal is not None:
                logger.warning("Slot %s empty-window save refused: %s", slot.key, refusal)
                _keep_owed_after_refusal(slot)
                return False
        return True
    # Skip a pure no-op: a freshly resumed slot with no new AND no edited
    # messages. ``slot._dirty`` is set by both append and in-place edits
    # (update_message / _resolve_stop_event / file-change + mcp_oauth patches),
    # so a dirty slot whose length merely equals the resumed count still falls
    # through and re-serializes the window — otherwise an in-place edit after
    # resume would never reach disk. closed/force/rewrite always proceed.
    if (
        slot._resumed_count > 0
        and len(window) <= slot._resumed_count
        and not slot._dirty
        # A queued prompt lives on the metadata line, so a slot whose window has
        # not grown since resume can still owe one. Skipping here would leave
        # that prompt with no durable copy for as long as the slot stays quiet.
        and not slot.queue_persist_pending
        and not closed
        and not force
        and not rewrite
    ):
        return True
    observed_history_key = str(getattr(slot, "_disk_meta_key", "") or "")
    if observed_history_key and not _write_guards._same_transcript(
        observed_history_key, history_key
    ):
        # A drifted save verifies the observed source A before committing target B.
        # Hold both physical lock sets across that whole check-then-write span so
        # delete_session(A) cannot unlink A after verification and let its rows be
        # published into B. locked_stems deduplicates and sorts the union, matching
        # every transcript writer's order and avoiding cross-key deadlock.
        save_lock = state.conversation_log.locked_stems(
            (
                *transcript_lock_stems(observed_history_key),
                *transcript_lock_stems(history_key),
            )
        )
    else:
        # Keep the ordinary one-transcript path on its existing lock.
        save_lock = state.conversation_log._locked(history_key)
    try:
        # Hold the SAME per-session cross-process lock that ``append`` /
        # ``append_off_loop`` / rotate / rewrite / metadata mutations take, across
        # the whole read-modify-atomic_write below (metadata read, frozen-prefix
        # read, archive-diff read, and the file-replacing ``atomic_write``).
        # Without it, a concurrent ``append_off_loop`` (e.g. a workflow/cron
        # result appended to the originating dashboard session) can land between
        # this save's snapshot of the file and its ``atomic_write`` — the save
        # then replaces the file with meta+frozen+window and silently deletes the
        # acknowledged append. The selected lock serializes the writers so neither
        # is lost. On the event loop it makes ONE non-blocking acquire per stem and
        # raises ``HistoryLockTimeout`` under contention (never blocking the loop);
        # ``save_slot_off_loop`` routes on-loop callers to a worker thread so they
        # take the patient acquire path instead of dropping the save.
        with save_lock:
            # ── Swap-won guard ──────────────────────────────────────────────
            # A save copies the window before it waits for this lock, and a
            # Regenerate swap publishes its commit inside this same lock. A copy
            # that predates the commit (or falls in its commit-to-memory stretch)
            # still holds the reply the swap retired, and lacks the variants it
            # gave the new reply; writing it would undo the swap on disk.
            if _write_guards.snapshot_predates_swap(slot, swap_basis):
                if messages is not None:
                    # A caller's own snapshot cannot be re-taken here: refuse,
                    # and the caller re-decides against the swapped window.
                    logger.warning(
                        "Slot %s save refused: a Regenerate swap committed after its snapshot",
                        slot.key,
                    )
                    _keep_owed_after_refusal(slot)
                    return False
                # Captured before the copy, not re-read after it (see
                # ``apply_committed_swap``).
                applying = slot._window_swap_applying
                retaken = _write_guards.paired_window_snapshot(slot, None, None)
                if retaken is None:
                    _keep_owed_after_refusal(slot)
                    return False
                window, queue_snapshot, queue_candidates, disk_older = retaken
                window = _write_guards.drop_notes_authorized_elsewhere(slot, window, note_auth_key)
                window = _write_guards.apply_committed_swap(window, applying)
            existing_meta, _meta_readable, _line_corrupt = _write_guards.read_line_for_save(
                state.conversation_log, slot, history_key
            )

            # ── Stale-queue guard ───────────────────────────────────────────
            # Refuse rather than put an older queue back: nothing is written, the
            # queue stays owed by the drift check, and the next pass re-decides
            # against the state that exists.
            refusal = _stale_queue_refusal(existing_meta)
            if refusal is not None:
                logger.warning("Slot %s save refused: %s", slot.key, refusal)
                _keep_owed_after_refusal(slot)
                return False
            queue_line_is_ours = _queue_line_is_ours(existing_meta)

            path = state.conversation_log._path(history_key)
            if _write_guards.delete_won(
                slot,
                path,
                existing_meta,
                meta_readable=_meta_readable,
                line_corrupt=_line_corrupt,
                history_key=history_key,
                conv_log=state.conversation_log,
            ):
                return False
            # ── Recreate-won guard ──────────────────────────────────────────
            # Re-read the live occupant of the slot's map key INSIDE the lock,
            # after the patient off-loop acquire. A truncating caller checks
            # object identity before dispatching this write, but the executor
            # wait between that check and here frees the event loop, and a
            # same-name close-and-recreate is not serialized against the slot's
            # own lock (the cleanup pops ``state._slots[name]`` and
            # ``get_or_create_slot`` re-inserts, neither taking it). A recreate
            # that resumes the SAME transcript keeps ``history_key`` identical,
            # so the routing guard above waves it through. Confirming the map
            # still holds THIS slot object, at the commit boundary with no await
            # before the write, is what catches it: if the map now holds a
            # replacement the original slot is being torn down and its
            # truncation has no future, so refuse the whole save (``False``,
            # nothing written) rather than land the stale snapshot on the
            # replacement's transcript.
            refusal = _replaced_refusal()
            if refusal is not None:
                logger.warning("Slot %s save refused: %s", history_key, refusal)
                # A refusal is not a commit, and the periodic writer cannot tell
                # the difference: it clears ``_dirty`` on any return that did not
                # raise. Keeping the state owed is what makes this guard safe for
                # a caller whose edit lives only in memory -- the same treatment
                # the other retryable refusals in this function already apply.
                _keep_owed_after_refusal(slot)
                return False
            path.parent.mkdir(parents=True, exist_ok=True)
            meta_line, _mode, _durable_queue, retired_drop_ids, tab_id = (
                _metadata_line.build_full_line(
                    slot,
                    existing_meta,
                    live_session=note_auth_key,
                    closed=closed,
                    closed_at=closed_at,
                    window=window,
                    queue_snapshot=queue_snapshot,
                    queue_candidates=queue_candidates,
                    rewrite=rewrite,
                    rows_only=rows_only,
                    mutes_opened_override=mutes_opened_override,
                    muted_override=muted_override,
                    turn_timeout_override=turn_timeout_override,
                )
            )
            meta_str = json.dumps(meta_line) + "\n"
            payload, frozen_prefix, foreign_lines = _transcript_merge.compose_payload(
                state,
                slot,
                path,
                history_key,
                window,
                disk_older,
                meta_str,
                rewrite=rewrite,
                retired_rows=retired_rows,
            )

            _preserve_mtime: float | None = None
            if closed and (slot.linked_session_key or is_channel_session_key(history_key)):
                # This slot shares its transcript with a channel, and the
                # reconciler decides whether a close still stands by comparing
                # the file's mtime against ``closed_at``: activity newer than the
                # close means the conversation moved on and the tab comes back.
                # Writing the close flag IS a write, so it would advance mtime
                # past ``closed_at`` and make the close outrun itself — the tab
                # would reopen on the next pass. Restore the pre-close mtime so
                # only a genuine channel append can outrun the close.
                #
                # Gated on the TRANSCRIPT, not on ``linked_session_key``: an
                # UNBOUND channel tab (the session map could not resolve its
                # stem) writes this very same shared file, so testing the binding
                # left exactly that tab unprotected — its close bumped the
                # channel file's mtime and ``_close_stands`` then rejected the
                # close, resurfacing the tab on the next reconcile. Keeping the
                # ``linked_session_key`` arm makes this strictly additive for
                # cron- and workflow-linked slots, whose keys are not channel
                # keys but which also share a transcript.
                try:
                    _preserve_mtime = path.stat().st_mtime
                except OSError:
                    _preserve_mtime = None

            try:
                atomic_write(path, payload, fsync=True)
            except BaseException:
                # A failed save keeps nothing staged: its rows stay unjudged and
                # the next save offers them again.
                _display_verdicts.take_pending()
                raise
            # The rows just written are judged for display once, now, on the
            # verdict worker (display_verdicts); a cheap hand-off, not the work.
            _display_verdicts.submit_committed(slot.workspace)
            # The staged value is now durable. Flip the live flag HERE, still
            # inside the transcript ``_locked`` block, so a concurrent dirty
            # flush -- which needs this same lock to serialize the slot -- cannot
            # observe the still-prior flag and overwrite the committed value.
            # This is the full-save twin of the empty-window merge's
            # ``after_commit_under_lock`` hook; one or the other runs, never both.
            if after_commit_under_lock is not None:
                after_commit_under_lock()
            _record_pending_memory_mode(pending_mode_target, _mode)
            # The write committed: the deferred-note drop records it retired
            # are now safe to consume (see ``build_full_line``). Discard is
            # idempotent, and a record consumed here is not needed again —
            # the retired entry left the durable hold in the same file replace.
            for retired_id in retired_drop_ids:
                slot._dropped_note_ids.discard(retired_id)
            if _preserve_mtime is not None:
                try:
                    os.utime(path, (_preserve_mtime, _preserve_mtime))
                except OSError:
                    # Best-effort: a failure only costs a resurfaced tab on the
                    # next pass, never data.
                    logger.debug(
                        "could not restore pre-close mtime for %s", history_key, exc_info=True
                    )
            # The witnesses below all describe THIS file. They live on the live
            # slot, so they may only be stamped while the slot still routes to
            # the transcript this save wrote. The event loop can rebind the slot
            # (a cron injection re-linking it) after the routing snapshot above
            # and while this worker writes: the write itself stays correct (it
            # lands on the authorized transcript), but stamping would then
            # describe the OLD file on a slot that now writes the NEW one —
            # clearing ``_pending_rewrite`` the new transcript still owes,
            # over-claiming ``_disk_window_len`` rows as persisted, and handing
            # the delete-won guard another file's identity. Skipping leaves every
            # witness at its pre-save value, which is the conservative side of
            # each one: the next save re-reads the prefix, re-takes the
            # archive-safe path, and re-observes the file. The cache
            # invalidations after this block are keyed on the file that WAS
            # written, so they stay unconditional.
            # Everything the stamping needs is computed BEFORE the routing
            # re-check, so the stamped region is assignments only: this runs in a
            # worker thread, and a syscall between the check and the last
            # assignment is the realistic point at which the event loop gets to
            # rebind the slot underneath a half-applied stamp. It cannot be made
            # atomic against the loop from here (``slot._lock`` is an asyncio lock
            # and no undo is right once the rebind path has recomputed these for
            # its own transcript) -- collapsing the five fields into one
            # assignable record carrying the key it describes is the real fix, and
            # belongs with that record rather than here.
            #
            # The frozen-prefix cache records the post-write mtime (even when
            # there is no frozen prefix, ``disk_older == 0``). It doubles as the
            # "did another process touch this file since we last wrote it?"
            # signal: a matching mtime on the next save proves THIS slot was the
            # last writer, so the frozen prefix is reusable and no NEW
            # cross-process append can have landed — letting the foreign-append
            # scan take the O(window) fast path instead of re-reading the whole
            # file. The foreign lines this save just preserved are cached
            # alongside so the fast path re-emits them verbatim: they now live in
            # the on-disk window region (after the frozen prefix), and because
            # ``disk_older`` is unchanged a bare frozen+window rebuild on the next
            # save would otherwise silently delete them.
            _post_write_cache: tuple[float, int, int, str, list[str]] | None
            try:
                _st = path.stat()
            except OSError:
                _post_write_cache = None
            else:
                _post_write_cache = (
                    _st.st_mtime,
                    _st.st_size,
                    disk_older,
                    frozen_prefix,
                    foreign_lines,
                )
            # The disk identity this save just wrote (carried forward from
            # ``existing_meta`` when present), so the delete-won guard can
            # recognize a file recreated by another writer after a permanent
            # delete on the NEXT save.
            _post_write_created_at = str(meta_line.get("created_at") or "")
            if slot_history_key(slot) == history_key:
                # A rewrite (archive-safe) save succeeded → clear the pending-rewrite
                # flag so later saves return to the cheap default path.
                if rewrite:
                    slot._pending_rewrite = False
                # How many window messages are now on disk, so memory trimming can
                # safely fold leading window messages into the frozen prefix.
                slot._disk_window_len = len(window)
                slot._disk_meta_created_at = _post_write_created_at
                # A committed save is a direct observation of the file this slot
                # writes — even when the carried-forward metadata is legacy and
                # has no ``created_at`` for the identity string above.
                slot._disk_meta_observed = True
                # The key this identity was observed under is the target just
                # written — this block is already gated on
                # ``slot_history_key(slot) == history_key``. The delete
                # witnesses compare it against the live target to tell a routing
                # drift apart from a delete-recreate.
                slot._disk_meta_key = history_key
                slot._frozen_prefix_cache = _post_write_cache
                if queue_line_is_ours:
                    # The queued prompts are now on disk, so the flush's drift
                    # check stops reporting them as owed until the queue moves
                    # again.
                    slot._queue_persisted_sig = queue_persist_signature(_durable_queue)
            else:
                logger.warning(
                    "Slot %s was rebound from %s while its save was in flight; "
                    "leaving the persistence witnesses at their pre-save values",
                    slot.key,
                    history_key,
                )
            state.conversation_log._invalidate_cache(history_key)
            state.conversation_log.note_tab_id(history_key, tab_id)
            return True
    except Exception:
        logger.error("Failed to save slot %s to history", slot.key, exc_info=True)
        raise


async def save_slot_off_loop(
    state: DashboardState,
    slot: _ChatSlot,
    messages: list[dict] | None = None,
    *,
    closed: bool = False,
    closed_at: float | None = None,
    force: bool = False,
    rewrite: bool = False,
    best_effort: bool = True,
    expected_history_key: str | None = None,
    expected_slot_name: str | None = None,
    expected_disk_older_count: int | None = None,
    rows_only: bool = False,
    issued_by_the_retraction: bool = False,
    mutes_opened_override: bool | None = None,
    muted_override: bool | None = None,
    after_commit_under_lock: Callable[[], None] | None = None,
    turn_timeout_override: tuple[int | None] | None = None,
    retired_rows: list[dict] | None = None,
) -> bool:
    """Persist a slot from the event loop without blocking or dropping the save.

    ``retired_rows``: forwarded to :func:`_save_slot_to_history`.

    :func:`_save_slot_to_history` holds the per-session cross-process
    ``_locked`` across its read-modify-``atomic_write``. That lock, invoked on
    the gateway event loop, makes a single
    non-blocking acquire and raises :class:`~kiro_crew.history.HistoryLockTimeout`
    under any concurrent holder (e.g. a workflow/cron result appending via
    :func:`~kiro_crew.history.append_off_loop`) — so calling the save inline on
    the loop would both risk a disk write on the loop and drop the save under
    benign contention, or surface the timeout into the aiohttp handler.

    This helper mirrors :func:`~kiro_crew.history.append_off_loop`: on a running
    loop it dispatches the save to a worker thread so it takes the *patient*
    off-loop acquire path; off the loop it saves inline.

    ``best_effort`` (default ``True``): a lock timeout / I/O error is logged and
    the slot is marked ``_dirty`` so the periodic flush retries the write — the
    in-memory slot is the source of truth. This retry re-arm matters for the
    metadata mutation endpoints (pin / folder / tag / mode), which call this with
    ``force=True`` but do not otherwise mark the slot dirty: without it a
    swallowed failure would drop an acknowledged edit with no retry, losing it
    after a restart. Pass ``best_effort=False`` for archival paths (session
    close/cleanup) that must CONFIRM the durable write succeeded before removing
    the session: the save still runs off-loop (patient acquire), but any
    exception propagates so the caller can roll back and keep the slot.

    ``expected_history_key``: the transcript key the caller authorized its
    mutation against. The save refuses (returns ``False``, nothing written)
    when the slot's routing does not resolve to that key at write time -- a
    rebind on the event loop can land between the caller's authorization and
    the worker's routing snapshot, and without this pin the durable write
    would target a transcript the caller never authorized.

    ``expected_slot_name``: the ``state._slots`` map key the caller checked its
    slot object against before dispatching. The save refuses (returns ``False``,
    nothing written) when the map holds a different slot object at the locked
    commit boundary -- a same-name close-and-recreate that resumes the
    same transcript keeps ``expected_history_key`` identical and slips past the
    routing pin, so this object-identity recheck under the lock stops the
    truncating snapshot from landing on the replacement's transcript.

    ``expected_disk_older_count``: the ``slot._disk_older_count`` read together
    with *messages*; see :func:`_save_slot_to_history`. A drift refuses the save.

    ``rows_only``: write the window but leave the metadata line's slot-owned
    fields as they stand on disk when the line was published by ANOTHER slot --
    for a caller persisting a slot's rows onto a transcript another live slot now
    holds. See :func:`_save_slot_to_history` for the full contract, including the
    ``tab_id`` test that keeps the flag from deferring to the caller's own line.

    Returns ``False`` only when the save was skipped WITHOUT writing: the
    session was permanently deleted while the save awaited the lock (the
    delete-won guard in :func:`_save_slot_to_history`), or the routing moved
    off ``expected_history_key``. A guarded write is also skipped when the slot
    is already fenced for close, because the retraction of its name has passed
    the point where it can wait for this write -- unless the retraction itself
    issued it (``issued_by_the_retraction``), which the handover drain does and
    nothing else does. Neither skip raises, for either
    ``best_effort`` mode, so a clean return does NOT prove a committed write.
    Callers that go on to republish the slot's content elsewhere (fork, the
    transfer export) must check the return; archival callers (close/cleanup)
    may ignore it — the delete already disposed of what they were archiving.
    """

    pending_mode_slot = slot
    current = state._slots.get(slot.key)
    if current is not None and slot_history_key(current) == slot_history_key(slot):
        pending_mode_slot = current

    def _do() -> bool:
        return _save_slot_to_history(
            state,
            slot,
            messages,
            closed=closed,
            closed_at=closed_at,
            force=force,
            rewrite=rewrite,
            expected_history_key=expected_history_key,
            expected_slot_name=expected_slot_name,
            expected_disk_older_count=expected_disk_older_count,
            rows_only=rows_only,
            pending_mode_slot=pending_mode_slot,
            mutes_opened_override=mutes_opened_override,
            muted_override=muted_override,
            after_commit_under_lock=after_commit_under_lock,
            turn_timeout_override=turn_timeout_override,
            retired_rows=retired_rows,
        )

    def _begin_guarded_metadata_write() -> None:
        inflight = getattr(slot, "_metadata_persist_inflight", 0)
        # Production slots initialize this counter, while compatibility callers
        # may provide a mock that synthesizes missing attributes. Treat a
        # non-integer value as an absent counter rather than leaking it into the
        # write's cleanup path.
        slot._metadata_persist_inflight = inflight + 1 if type(inflight) is int else 1

    def _finish_guarded_metadata_write() -> None:
        inflight = getattr(slot, "_metadata_persist_inflight", 0)
        slot._metadata_persist_inflight = (
            inflight - 1 if type(inflight) is int and inflight > 0 else 0
        )

    def _register_guarded_write(save: "asyncio.Future[bool]") -> None:
        """Hold a guarded write's executor future until the WORKER completes.

        ``_finish_guarded_metadata_write`` runs in THIS coroutine's ``finally``,
        so a caller cancelled while the executor is running releases the count
        with the worker thread still on its way to the rename. The future does
        not share that fate: cancelling the awaiter either cancels the work
        before it starts, in which case nothing is written, or fails to cancel a
        thread already running and the future still completes when that thread
        returns. A retraction of this slot's name orders itself after that
        completion, which is a guarantee the count cannot give.
        """
        register_guarded_history_write(slot, save)

    async def _dispatch_to_worker(active_loop: asyncio.AbstractEventLoop) -> bool:
        if guarded_metadata and not issued_by_the_retraction and getattr(slot, "is_closing", False):
            # A retraction of this slot's name is already past its own wait for
            # the guarded writes registered below, so dispatching now would put a
            # worker thread on its way to the rename with nothing left to order
            # against it. The caller that reached here checked the same fence
            # before its own awaits, and those awaits are where the fence went
            # up; re-reading it HERE is what makes the pair decidable. There is
            # no suspension between this read and the registration below, so the
            # two possible interleavings are the only ones: fence first and this
            # write refuses, or registration first and the retraction waits.
            #
            # The fence asks whether this write races a retraction. A write the
            # retraction ITSELF issues does not: the handover drain runs inside
            # the close that raised the fence, is sequenced by it, and is the
            # last chance the original's unsaved rows have to reach disk.
            # ``issued_by_the_retraction`` is how that caller says so, and only
            # that caller passes it.
            #
            # Refusing writes nothing, which is the ``False`` contract this
            # function already documents for a write its own guards decline. The
            # close's archival save is unaffected for a second reason: it carries
            # no authorized transcript key, so it is not a guarded write.
            logger.warning(
                "Refusing a guarded history write for %s: the conversation is being closed",
                getattr(slot, "key", "?"),
            )
            # Refusing must not be silently FINAL. A metadata-only mutation
            # (recreate title, folder filing, tag, pin, mode) calls this with
            # ``force=True``, does not otherwise set ``_dirty``, and its callers
            # publish on the strength of the acknowledged edit without reading
            # this return. A close can raise the fence and then leave the slot
            # live -- it refuses on a breached wait, and the sweep raises and
            # releases the fence around a deferral -- so an edit landing in that
            # window would be dropped and the old value would come back after a
            # restart. Arming the flush is the same remedy this function's own
            # exception arms use below, and it converges once the fence is down.
            try:
                slot._dirty = True
            except Exception:  # noqa: BLE001 - a slot double may not accept it
                pass
            return False
        save = active_loop.run_in_executor(None, _do)
        # ``run_in_executor`` returns an asyncio Future bound to ``active_loop``;
        # its callbacks therefore already run on that loop. Register before any
        # await so adoption follows the worker's completion even when the
        # awaiting task is cancelled, and runs before a normal awaiter resumes.
        save.add_done_callback(
            lambda _completed: apply_pending_slot_memory_mode(state, pending_mode_slot)
        )
        if not guarded_metadata:
            return await save
        _register_guarded_write(save)
        # Shield the FUTURE from this caller's cancellation. Cancelling an
        # asyncio future returned by ``run_in_executor`` succeeds whatever the
        # worker thread is doing -- the chained cancel of the underlying
        # concurrent future cannot stop a thread already running -- so an
        # unshielded await would resolve the registration above while the write
        # is still on its way to the rename, which is the exact blindness the
        # registration exists to remove. The thread runs on either way; shielding
        # only keeps it observable.
        return await asyncio.shield(save)

    guarded_metadata = expected_history_key is not None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        if best_effort:
            try:
                return _do()
            except Exception:  # noqa: BLE001 - best-effort durable copy
                # A swallowed failure must NOT be silently final: mark the slot
                # dirty so the periodic flush retries the write. Metadata-only
                # mutations (pin / folder / tag / mode) call this with
                # ``force=True`` but do not otherwise set ``_dirty``; without this
                # a lock timeout / I/O error would drop the change and the flush
                # would never retry it, losing an acknowledged edit after restart.
                slot._dirty = True
                logger.warning(
                    "save_slot_off_loop: inline save failed slot=%s", slot.key, exc_info=True
                )
                return True
        return _do()
    if best_effort:
        if guarded_metadata:
            _begin_guarded_metadata_write()
        try:
            return await _dispatch_to_worker(loop)
        except Exception:  # noqa: BLE001 - best-effort durable copy
            # See the inline branch above: re-arm the periodic flush so a
            # swallowed metadata/message save is retried rather than lost.
            slot._dirty = True
            logger.warning(
                "save_slot_off_loop: offloaded save failed slot=%s", slot.key, exc_info=True
            )
            return True
        finally:
            if guarded_metadata:
                _finish_guarded_metadata_write()
    # Non-best-effort: propagate so the caller can roll back (do NOT remove the
    # session until the durable write is confirmed).
    if guarded_metadata:
        _begin_guarded_metadata_write()
    try:
        return await _dispatch_to_worker(loop)
    finally:
        if guarded_metadata:
            _finish_guarded_metadata_write()
