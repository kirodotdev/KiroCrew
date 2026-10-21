"""Rewrite-vs-takeover ordering: the sync rules a save and a reopen consult.

The arbitration between a truncating save and a same-key takeover is decided
in ``slot_registry``; these are the rules the persistence paths apply around
it. For the WRITE side: ``open_dispatch_basis`` opens the key's takeover basis
at the dispatcher's last synchronous instant, ``_transfer_basis_to_worker``
hands it to a worker the dispatcher was cancelled away from,
``_truncation_claim_scope`` registers the save's claim -- with the transcript
it writes -- for the whole write, and ``refuse_overtaken_rewrite`` is what a
refused commit gate does under the lock it still holds. For the READ side:
``_slot_closed_before`` is the tombstone gate, and
``admit_rehydrate_takeover_note`` applies it and records a delivery's takeover
note. The save transaction, its on-loop entry point and the rehydrate drivers
stay in ``chat_persistence`` (the composition contract pins them there, where
the off-loop gates scan them); this module reaches them through the facade at
call time and holds no coroutine.

Each function here is bound on ``chat_persistence`` under the name it had there,
so a patch of ``chat_persistence.<name>`` reaches every caller.
"""

from __future__ import annotations

import contextlib
import logging
import math
from typing import TYPE_CHECKING, Literal

from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.slot_registry import (
    DeferredDismissal,
    OwedUnstamp,
    SlotRegistry,
    TakeoverBasis,
    TakeoverNote,
    TruncationClaim,
)
from kiro_crew.history import HistoryLockTimeout

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Iterator

    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

logger = logging.getLogger("kiro_crew.dashboard.chat_persistence")


def _slot_closed_before(state: DashboardState, slot_name: str, instant: float) -> bool:
    """True when *slot_name*'s close tombstone stands at or before *instant*.

    The complement of :func:`slot_closed_since`, for a caller that BEGINS after
    the close rather than one whose snapshot predates it: a resume or delivery
    that starts once the user has already dismissed the tab is reopening a
    closed tab, not racing the close, and must not act as the close's
    replacement. Same tombstone, same key spelling.
    """
    from kiro_crew.dashboard.channel_slots import _RECENT_CLOSES

    closes = _RECENT_CLOSES.get(state) or {}
    when = closes.get(slot_name)
    return when is not None and when <= instant


def admit_rehydrate_takeover_note(
    state: DashboardState,
    slot_name: str,
    history_key: str,
    *,
    started: float,
    adopt_closed: bool,
) -> TakeoverNote | None:
    """Record a delivery rehydrate's takeover note, unless the tab was closed before it began.

    A rehydrate whose start (*started*, read before any await) postdates the
    key's close tombstone is a reopen of a closed tab, not a takeover: it notes
    nothing, so the close's archive commits the user's edit and the reopen is
    refused by the ``closed`` flag the close wrote, the way any reopen of a
    closed tab is (``adopt_closed`` callers read closed transcripts on purpose
    and skip the gate). Otherwise the takeover is recorded against the key,
    declaring *history_key* as its read target. Returns the note's handle for
    the caller to retract should it publish nothing, ``None`` when nothing was
    recorded. Raises :class:`HistoryLockTimeout` -- the retryable
    lock-contention error the read layer already speaks -- when the note is
    refused at admission: the key's teardown watch already retains as many
    declared transcripts as one teardown admits, so this takeover's target
    could not be recorded and the read must not run.
    """
    if not adopt_closed and _slot_closed_before(state, slot_name, started):
        logger.debug(
            "Rehydration of %s begins after the tab was closed; reading the closed "
            "transcript rather than taking the key over",
            slot_name,
        )
        return None
    note = SlotRegistry.open_same_key_takeover_note(state, slot_name, transcript=history_key)
    if note is None:
        raise HistoryLockTimeout(
            f"resume of {slot_name!r} refused: its takeover note could not be recorded "
            "against the key's teardown watch"
        )
    return note


def open_dispatch_basis(
    state: DashboardState,
    slot: _ChatSlot,
    takeover_basis: TakeoverBasis | None,
    *,
    expected_slot_name: str | None,
    truncating: bool,
) -> tuple[TakeoverBasis | None, TakeoverBasis | None]:
    """The basis a save dispatch carries, and the one it owns (``None`` when a caller's).

    A caller-opened basis (the close/cleanup teardowns open theirs before they
    pop the slot) is carried as it is and closed by its opener. Otherwise one
    is opened here, at the dispatcher's last synchronous instant, keyed by the
    map key the caller checked (``expected_slot_name``) or the slot's own, and
    the worker closes it once the save settles. *truncating* is what the
    dispatcher can see of the write's shape; the worker raises an append that
    turns truncating in the dispatch gap.
    """
    if takeover_basis is not None:
        return takeover_basis, None
    owned = SlotRegistry.open_takeover_basis(
        state,
        expected_slot_name if expected_slot_name is not None else slot.key,
        truncating=truncating,
    )
    return owned, owned


@contextlib.contextmanager
def _truncation_claim_scope(
    state: DashboardState,
    slot: _ChatSlot,
    *,
    rewrite: bool,
    expected_slot_name: str | None,
    takeover_basis: TakeoverBasis | None,
) -> Iterator[TruncationClaim | None]:
    """Hold a truncating save's publication-ordering claim across its write.

    The claim (see :class:`~kiro_crew.dashboard.slot_registry.TruncationClaim`)
    is the contract that orders this save's file replace against same-key slot
    publication: registered here, marked by every same-key takeover, and
    decided atomically at the commit gate immediately before ``atomic_write``.
    Entered BEFORE the per-session lock, so a takeover that lands while this
    save is still queued behind another writer wins too — the takeover's own
    read serializes behind the same lock, so whichever of the two acquires it
    first, the claim decision (not lock order) settles who the transcript
    belongs to.

    ``takeover_basis`` extends the coverage backwards to the caller's dispatch:
    a takeover firing before this registration has no claim to mark, so the
    registration compares the key's open takeover watch against the snapshot
    the dispatcher took at its last synchronous instant, and a moved count
    means the claim is born already overtaken. The dispatcher that opened the
    basis closes it; this scope only reads it.

    Registered only for truncating writes: an append-shaped save is a superset
    of the file, so a replacement reading around it can lose nothing. Keyed by
    the caller-checked map key when one was supplied, else by the slot's own
    key — a rewrite retried from ``_pending_rewrite`` carries no
    ``expected_slot_name``, and its window is exactly as stale to a takeover
    as a guarded caller's.
    """
    if not rewrite:
        yield None
        return
    name = expected_slot_name if expected_slot_name is not None else slot.key
    claim = SlotRegistry.begin_truncation_claim(
        state, name, basis=takeover_basis, transcript=slot_history_key(slot)
    )
    try:
        yield claim
    finally:
        SlotRegistry.retire_truncation_claim(state, name, claim)


def _transfer_basis_to_worker(
    state: DashboardState,
    basis: TakeoverBasis | None,
    future: "asyncio.Future[bool] | None",
) -> None:
    """Hand a caller-opened basis to its still-running worker on cancellation.

    The shield in :func:`save_slot_off_loop` keeps the worker running through a
    caller's cancellation, but the CALLER's unwind reaches its own ``finally``
    close immediately — and the worker may still be pre-registration, so
    retiring the watch there leaves a takeover in that gap unrecorded and the
    orphaned truncation commits over the takeover's transcript. Transferring
    makes the caller's close a no-op and releases the holder from the worker
    future's settlement instead — the same worker-owns-the-close rule ``_do``
    already applies to the basis that function opens itself. A future already
    settled needs no transfer: the caller's close is then correctly ordered
    after the write.
    """
    if basis is None or future is None or future.done():
        return
    if not SlotRegistry.defer_basis_close(state, basis):
        return
    future.add_done_callback(lambda _f: SlotRegistry.close_transferred_basis(state, basis))


def refuse_overtaken_rewrite(
    state: DashboardState,
    slot: _ChatSlot,
    history_key: str,
    window: list[dict],
    *,
    note_auth_key: str,
    messages: list[dict] | None,
    expected_slot_name: str | None,
) -> None:
    """A truncating save's commit gate refused: a same-key takeover won the ordering.

    Runs INSIDE the save's per-session lock, immediately after
    ``commit_truncation_claim`` answered ``False`` and before the save returns
    its refusal. Logs the refusal, lands the append-safe fallback for the
    hand-over shape, and leaves the slot owing its write.

    The refusal is right, but only for the TRUNCATION: a ``_pending_rewrite``
    retry riding a teardown save also carries window rows appended AFTER the
    failed inline rewrite — real conversation whose last writer is this frame
    (the slot is out of the map, so no flush retries it). The takeover's own
    transcript read serializes behind the per-session lock THIS save still
    holds, so landing those rows here — id-deduped appends, nothing rebuilt —
    is what makes the replacement hydrate a window that already carries them.
    Deferring the append past the release (the drain's loop-side arm) lets the
    queued read acquire the lock first and publish a window without the rows;
    a later truncating rewrite by that replacement rebuilds the file without
    collecting foreign appends (``collect_foreign=not rewrite``), which drops
    the rows the drain appended too late.

    Scoped to the hand-over shape only: ``slot._pending_rewrite`` with no
    explicit snapshot. An edit path's snapshot (``messages is not None``) is
    the truncation itself — its regenerated rows presuppose the truncation the
    gate just refused, so they lose with it (the recreate-won rule).
    ``_locked`` is reentrant for the same key on this thread, so the per-row
    locks inside ``append_if_absent`` reuse the lock held here. Best-effort: a
    failed append leaves the refusal as it was, and the drain's own arm
    retries it after release.

    The rows travel as the fully built persisted dicts this save would have
    written (the ``entry`` passthrough), so the appended rows carry the same
    ``ts``, provenance, ``variants`` and ``meta`` the committed payload would
    have; a (role, content) reconstruction here would silently strip every one
    of those fields from the only durable copy these rows get. The attachment
    pair scopes the builder's inline-image rewrite to this transcript, exactly
    as the composed payload does. The replay is the one the drain's own arm
    runs: header settled first (an unconfirmed contract raises and appends
    nothing, the state staying owed), rows built by the full save's builder,
    appended id-deduped against the whole durable history.
    """
    from kiro_crew.dashboard import chat_persistence as cp  # circular import: facade imports owners

    logger.warning(
        "Slot %s save refused: a same-name replacement took over key %s "
        "before this truncating write committed",
        history_key,
        expected_slot_name if expected_slot_name is not None else slot.key,
    )
    if messages is None and slot._pending_rewrite and state.conversation_log:
        try:
            appended = cp.append_window_rows(
                state.conversation_log,
                slot,
                history_key,
                window,
                live_session=note_auth_key,
            )
            if appended:
                logger.info(
                    "Slot %s: %d unsaved row(s) appended to %s under the "
                    "refused save's lock, ahead of the takeover's read",
                    slot.key,
                    appended,
                    history_key,
                )
        except Exception:
            logger.warning(
                "Slot %s: append-safe fallback failed under the refused "
                "save's lock for %s; the hand-over drain retries it",
                slot.key,
                history_key,
                exc_info=True,
            )
    cp._keep_owed_after_refusal(slot)


#: What :func:`record_deferred_dismissal` did with an owed dismissal.
#:
#: ``written``    -- the ``closed`` marker is confirmed on disk (or the session
#:                   is confirmed deleted, which satisfies the dismissal the same
#:                   way); the dismissal is settled. A line that merely reads
#:                   back is never taken as written.
#: ``superseded`` -- nothing is owed to write: a publication took the key while
#:                   the write was queued and settled the dismissal (the tab now
#:                   holding the key stays open), or the conversation the user
#:                   closed is gone and a different incarnation lives under its
#:                   key (its line is not ours to mark), or the closed slot
#:                   recorded no identity to match against. A success for the
#:                   close.
#: ``failed``     -- the write raised, the line was unreadable, or it still reads
#:                   open after the attempt; the dismissal stays owed on its watch
#:                   for the next exit on the key.
DismissalWrite = Literal["written", "superseded", "failed"]


def record_deferred_dismissal(
    state: DashboardState, dismissal: DeferredDismissal
) -> DismissalWrite:
    """Write the ``closed`` marker a hand-over close left owed; say what became of it.

    The close reported success and handed its key to a taker whose read was
    still pending; that taker has now given up (or there was none standing), so
    nobody holds the key and nothing else records the dismissal -- without the
    marker the recent-session restore brings back a tab the user closed. Written
    under the transcript's lock, and inside that lock the dismissal is checked
    to be STILL OWED with the key unpublished
    (:meth:`SlotRegistry.dismissal_still_owed`): a reopen that published while
    this write was queued owns the key, and stamping its live transcript closed
    would hide the tab the user just opened -- the write is skipped and the
    publication's own settlement stands (``superseded``). Written only onto a
    line that is not already closed (a newer close owns its own stamp) and only
    for a session that still exists (``require_existing``: a session deleted
    meanwhile must not come back as a metadata-only line).

    The dismissal is settled -- cleared from its watch, holder released -- only
    after the write is CONFIRMED (:meth:`SlotRegistry.settle_written`). A write
    that fails leaves the dismissal owed: the next exit on the key (a
    retraction, a hand-over, a close) asks :meth:`SlotRegistry.owed_dismissal`
    for it and writes it again, so the dismissal is never lost to one failed
    attempt (``failed``). A close that writes its own marker reads the outcome:
    ``written`` and ``superseded`` are a committed close, ``failed`` is not.
    Runs OFF the loop: the write takes the cross-process lock.
    """
    log = state.conversation_log
    if not log:
        return "failed"
    if not dismissal.created_at:
        # The closed slot recorded no identity for its transcript (a legacy
        # line): nothing can tell the conversation the user closed from one
        # created since under the same key, so the marker is never written --
        # stamping a line one cannot identify is the harm this witness exists
        # to prevent -- and the dismissal is settled as unwritable rather than
        # retried forever against a line it could never match.
        logger.warning(
            "Slot %s: the dismissal a hand-over left owed carries no transcript identity; "
            "the closed marker is not written to %s",
            dismissal.name,
            dismissal.transcript,
        )
        SlotRegistry.settle_written(state, dismissal)
        return "superseded"

    def _same_incarnation(meta: dict) -> bool:
        # The line about to be stamped must be the conversation the user
        # closed. A session deleted and created again under this key carries
        # a fresh ``created_at`` (and a line with none is not identifiable).
        return str(meta.get("created_at") or "") == dismissal.created_at

    # The guard decides under the transcript lock, but a publication that
    # takes the key needs no lock: a same-name create's ``put_slot`` can land
    # between the guard's "still owed" and the rename, settle the dismissal on
    # the registry side, and the rename then stamps ``closed`` onto the line of
    # a tab that is live. The record is the witness through the write: after
    # the rename commits, still under the same lock, the ownership is read
    # again (a brief registry-mutex read, the order the guard already uses).
    # The hook only RECORDS the verdict -- ``update_metadata_if`` documents its
    # callback as I/O-free -- and the un-stamp runs right after it returns, as
    # a compare-and-clear under its own lock (:func:`unstamp_superseded_marker`).
    superseded_after_commit: list[bool] = [False]

    def _recheck_after_commit() -> None:
        if not SlotRegistry.dismissal_still_owed(state, dismissal):
            superseded_after_commit[0] = True

    try:
        written = bool(
            log.update_metadata_if(
                dismissal.transcript,
                {"closed": True, "closed_at": dismissal.closed_at},
                lambda meta: "closed" not in meta
                and _same_incarnation(meta)
                and SlotRegistry.dismissal_still_owed(state, dismissal),
                require_existing=True,
                require_write=True,
                after_commit_under_lock=_recheck_after_commit,
            )
        )
    except Exception:
        logger.error(
            "Slot %s: the dismissal a hand-over left owed could not be recorded on %s; "
            "it stays owed and the next exit on the key retries it",
            dismissal.name,
            dismissal.transcript,
            exc_info=True,
        )
        return "failed"
    if written and superseded_after_commit[0]:
        # A publication took the key between the guard and the rename and
        # settled the dismissal: the stamp just written sits on a live tab's
        # line and comes off again. The dismissal IS settled either way; an
        # un-stamp that does not land is parked owed and retried by the next
        # exit on the key, never reported as a failed close.
        unstamp_superseded_marker(
            state, OwedUnstamp(dismissal.name, dismissal.transcript, dismissal.closed_at)
        )
        return "superseded"
    if written:
        SlotRegistry.settle_written(state, dismissal)
        return "written"
    if not SlotRegistry.dismissal_still_owed(state, dismissal):
        # A publication took the key (and settled the dismissal) while this
        # write was queued, or an explicit resume is reading: nothing for THIS
        # write to do; the publication's or the resume's exit owns the
        # dismissal.
        return "superseded"
    # Not written while still owed. The write was skipped for one of three
    # reasons the status read tells apart, and only the first two settle: the
    # line already carries a marker (a newer close owns its own stamp) or the
    # session is gone (a delete disposed of it) -- the tab cannot come back
    # either way. A line that reads back OPEN means the write did not happen
    # (``update_metadata_if`` refuses a line it could not read, and a transient
    # read failure inside it answers False without writing): readability is
    # not a commit, so the dismissal stays owed and the next exit retries.
    try:
        after, readable = log.get_metadata_status(dismissal.transcript)
        exists = log._path(dismissal.transcript).exists()
    except Exception:
        after, readable, exists = {}, False, True
    if readable and (not exists or "closed" in after):
        SlotRegistry.settle_written(state, dismissal)
        return "written"
    if readable and exists and not _same_incarnation(after):
        # The conversation the user closed is gone and another lives under
        # its key: the dismissal is satisfied (there is nothing left to
        # dismiss) and the new conversation is not ours to mark.
        logger.info(
            "Slot %s: the transcript at %s is a different incarnation than the one "
            "the user closed; the owed marker is dropped rather than written onto it",
            dismissal.name,
            dismissal.transcript,
        )
        SlotRegistry.settle_written(state, dismissal)
        return "superseded"
    logger.warning(
        "Slot %s: the dismissal a hand-over left owed was not written to %s "
        "(%s); it stays owed and the next exit on the key retries it",
        dismissal.name,
        dismissal.transcript,
        "unreadable line" if not readable else "the line still reads open",
    )
    return "failed"


def close_metadata_under_lock(state: DashboardState, history_key: str) -> tuple[dict, bool]:
    """``get_metadata_status`` taken inside the transcript lock, off the loop.

    The lock is the one ``update_metadata_if`` holds for an owed close marker
    (:func:`record_deferred_dismissal`), so a snapshot taken here is ORDERED
    against that write: a marker that landed before this read is seen, and one
    that has not landed cannot land afterwards while the reader stays
    registered (``SlotRegistry.begin_resume_read``), because the write-time
    guard then finds the read in flight. A plain ``get_metadata`` is a cached
    file read with no such ordering. Runs OFF the loop: the lock is a
    cross-process flock.
    """
    log = state.conversation_log
    if not log:
        return {}, False
    with log._locked(history_key):
        return log.get_metadata_status(history_key)


def unstamp_superseded_marker(state: DashboardState, record: OwedUnstamp) -> bool:
    """Remove a ``closed`` stamp of ours from the line of a tab a publication won; ``True`` when off.

    A compare-and-clear under the transcript's own lock: ``clear_closed`` with
    ``only_if_closed_before`` set to the smallest float above the ``closed_at``
    we wrote, so exactly our stamp (and nothing stamped later -- a legitimate
    close of the new tab carries a later ``closed_at``) is removed. On success,
    or when the line provably does not carry our stamp, the record is
    settled; on a failure (a transient write or read error) it is parked owed
    on the key's watch (:meth:`SlotRegistry.park_owed_unstamp`) so the next
    exit on the key -- a resume's or rehydrate's, in either direction -- retries
    it (:func:`retry_owed_unstamp`). The dismissal itself is settled on the
    registry already; what is owed here is only the clear. Runs OFF the loop.
    """
    log = state.conversation_log
    if not log:
        return False
    try:
        log.clear_closed(
            record.transcript, only_if_closed_before=math.nextafter(record.closed_at, math.inf)
        )
        meta, readable = log.get_metadata_status(record.transcript)
    except Exception:
        logger.error(
            "Slot %s: the closed marker a publication won against could not be cleared from %s; "
            "the clear stays owed and the next exit on the key retries it",
            record.name,
            record.transcript,
            exc_info=True,
        )
        SlotRegistry.park_owed_unstamp(state, record)
        return False
    if readable and str(meta.get("closed_at") or "") != str(record.closed_at):
        # Our stamp is gone: cleared now, or replaced by a later close that is
        # not ours to touch.
        SlotRegistry.settle_unstamped(state, record)
        return True
    logger.warning(
        "Slot %s: the closed marker a publication won against still reads on %s; "
        "the clear stays owed and the next exit on the key retries it",
        record.name,
        record.transcript,
    )
    SlotRegistry.park_owed_unstamp(state, record)
    return False


def retry_owed_unstamp(state: DashboardState, name: str) -> None:
    """An exit on *name* retries the un-stamp an earlier publication left owed, if any."""
    record = SlotRegistry.owed_unstamp(state, name)
    if record is not None:
        unstamp_superseded_marker(state, record)
