"""One consistent view of a live slot's transcript.

A reader that copies a conversation -- the fork, the transfer bundle, the bounded
slot-detail page -- reads the durable rows off the event loop and pairs them with
the window rows that are not on disk yet. While it is off the loop the slot keeps
moving: the periodic flush lands, a turn appends, a rewind truncates, a delete
completes. So the read is optimistic. The slot is observed on the loop, the read
runs, the slot is observed again, and the attempt is kept only when the two
observations agree on the fields the reader depends on; otherwise it is spent and
the next one starts from the state that exists. All of that, with the budget the
attempts share, lives here.

What differs between the readers is declared in one table, :data:`FORK`,
:data:`TRANSFER` and :data:`PAGE`: which fields must hold still across the read,
what a pending rewrite or a boundary ahead of the window means, whether a dirty slot
is persisted first, whether a deleted session is refused. The read itself and the
saves are passed in, because they genuinely differ per reader -- a plain chained
read, a derivation-seam read with redacting assembly, a bounded page; a guarded
truncating rewrite, a flush -- and each stays with the rest of its reader's rules.

New consistent readers of a slot's transcript belong here.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sized
from dataclasses import dataclass
from typing import TYPE_CHECKING, Generic, Literal, TypeVar, cast

from kiro_crew.dashboard.chat_persistence import _FLUSH_SNAPSHOT_RETRIES, session_was_deleted

if TYPE_CHECKING:
    from kiro_crew.dashboard.state import DashboardState, _ChatSlot

T = TypeVar("T")

#: Attempts one snapshot gets, shared by every reader: an attempt is spent by a read
#: the slot moved under, by a save that had to land first, or by a read that asked to
#: go again. A flush is 5s-periodic, so even one interleave is rare. The value is the
#: bound the save's own window/queue pair retries under, defined once in
#: ``slot_persistence.write_guards``.
SNAPSHOT_ATTEMPTS: int = _FLUSH_SNAPSHOT_RETRIES


class SnapshotUnstable(RuntimeError):
    """No consistent view of the source transcript could be taken.

    Raised here when the slot kept moving inside every attempt, or when the on-disk
    transcript cannot be trusted under the reader's rules (a rewind/regenerate
    rewrite is still owed, the persisted boundary ran ahead of the window, the
    session was deleted); a reader's own ``persist`` raises it too when the slot
    cannot be saved first (the transfer's flush).

    Raised instead of copying anyway or falling back to a blocking inline read. A
    copy is cheap to refuse and the caller can retry, whereas copying would hand out
    the wrong conversation, and a synchronous read of a large transcript on the
    event loop can starve the liveness heartbeat until the watchdog exits the
    gateway.
    """


class RetryRead(Exception):
    """Raised by a ``read`` callable to spend this attempt and start the next one.

    For a read whose own failure says only "the transcript moved" (a revision change
    between two ranged reads, a transient I/O error), as opposed to a failure the
    reader must answer itself.
    """


@dataclass(frozen=True)
class SnapshotPurpose:
    """The rules one reader takes its snapshot under. One row of the table below.

    *witness* names the :class:`SlotView` fields that must read the same after the
    read as before it. *pending_rewrite* says what a rewind/regenerate whose
    truncating rewrite is still owed means: ``"save"`` it through the caller's
    ``rewrite`` and start again, ``"refuse"`` the snapshot, or ``"read"`` through it
    because the reader reconciles those rows itself. *boundary_ahead* does the same
    for a persisted boundary past the end of the window: ``"resync"`` (merge from the
    restore point when disk holds rows the counters do not represent, otherwise
    persist and start again; the read must then return the row list, whose length
    is the discriminator), ``"refuse"``, or ``"read"``. *persist_dirty* saves a
    dirty slot before every read. *refuse_deleted* refuses a permanently deleted
    session before the read and again after it. *logger* names the reader the retry and
    refusal lines are logged for, ``None`` for a reader that logs its own; *retry_log*
    is the DEBUG line a spent attempt logs (``%s`` is the slot key), and *noun* is
    what a refusal says it is refusing.
    """

    witness: tuple[str, ...]
    pending_rewrite: Literal["save", "refuse", "read"]
    boundary_ahead: Literal["resync", "refuse", "read"]
    persist_dirty: bool
    refuse_deleted: bool
    logger: str | None
    retry_log: str
    noun: str


#: The fork reads the chained transcript and merges the unpersisted tail itself, so
#: every counter that sizes or places that tail must hold still: ``_disk_older_count``
#: is half the window's identity (the window is ``messages[_disk_older_count:]``, and
#: the capped-restore discriminator below is computed from it). And ``_dirty`` too,
#: as a STABILITY WITNESS only: an in-place content edit (a variant switch) moves
#: neither the length nor the older count, its save re-assigns the boundary to the
#: value it already had, and clearing ``_dirty`` cannot move ``_dirty_gen``, so a save
#: that merely COMPLETES under the read is visible only there -- without it the fork
#: would adopt the pre-save bytes and carry the superseded content.
FORK = SnapshotPurpose(
    witness=("boundary", "generation", "length", "older", "dirty"),
    pending_rewrite="save",
    boundary_ahead="resync",
    persist_dirty=False,
    refuse_deleted=False,
    logger="kiro_crew.dashboard.chat_fork",
    retry_log="chat_fork: slot=%s changed during the transcript read; retrying",
    noun="fork",
)

#: The transfer flushes a dirty slot before each read, so the tail it slices is
#: honest, and refuses rather than repairs: a transfer is a copy, so failing costs
#: the user only a retry. ``_dirty_gen`` is the primary witness; the boundary catches
#: the one change the generation does not, a completed flush (it advances
#: ``_disk_window_len`` without marking the slot dirty); the length is a backstop for
#: any path that mutates ``slot.messages`` without marking dirty, kept because this
#: snapshot has already been wrong twice by assuming one field told the whole story.
TRANSFER = SnapshotPurpose(
    witness=("generation", "boundary", "length"),
    pending_rewrite="refuse",
    boundary_ahead="refuse",
    persist_dirty=True,
    refuse_deleted=True,
    logger="kiro_crew.dashboard.session_transfer",
    retry_log="session_transfer: slot %s flushed during the transcript read; retrying",
    noun="transfer",
)

#: The bounded page reconciles the window by message identity rather than by a
#: boundary slice, so it reads through a pending rewrite or a boundary ahead, and
#: only the counters that index its durable prefix must hold still.
PAGE = SnapshotPurpose(
    witness=("generation", "older", "durable_older"),
    pending_rewrite="read",
    boundary_ahead="read",
    persist_dirty=False,
    refuse_deleted=False,
    logger=None,
    retry_log="",
    noun="page",
)


@dataclass(frozen=True)
class SlotView:
    """One observation of a slot, taken on the event loop before an attempt's read.

    *tail* is the window past the persisted boundary -- the rows the read cannot
    return -- or ``None`` when the boundary runs ahead of a dirty window and the read
    must first say which of its two causes this is. *attempt* counts from 1 up to
    :data:`SNAPSHOT_ATTEMPTS`.
    """

    attempt: int
    boundary: int
    generation: int
    length: int
    older: int
    durable_older: int
    dirty: bool
    tail: list[dict] | None


@dataclass(frozen=True)
class TranscriptSnapshot(Generic[T]):
    """What one attempt's read returned, with the window rows it pairs with.

    *tail* is the window the read does not hold. *disk_holds_unrepresented* is set
    when disk was found to hold rows the slot's counters do not represent (a capped
    restore), so the caller must not persist the slot over them.
    """

    result: T
    tail: list[dict]
    view: SlotView
    disk_holds_unrepresented: bool = False


def _durable_older(slot: _ChatSlot) -> int:
    # A stand-in that does not carry the durable counter reads as a value the raw
    # counter can never equal, never as a default equal to it.
    value = getattr(slot, "_disk_older_durable_count", None)
    return -1 if value is None else int(value)


#: How each witness field is read. ``_dirty_gen`` is monotonic and bumped centrally
#: by the ``_dirty`` setter, so it catches an in-place edit that moves neither the
#: boundary nor the length.
_FIELDS: dict[str, Callable[[_ChatSlot], object]] = {
    "boundary": lambda slot: slot._disk_window_len,
    "generation": lambda slot: getattr(slot, "_dirty_gen", 0),
    "length": lambda slot: len(slot.messages),
    "older": lambda slot: slot._disk_older_count,
    "durable_older": _durable_older,
    "dirty": lambda slot: slot._dirty,
}


def _witness(slot: _ChatSlot, purpose: SnapshotPurpose) -> tuple[object, ...]:
    return tuple(_FIELDS[name](slot) for name in purpose.witness)


def _observe(slot: _ChatSlot, purpose: SnapshotPurpose, attempt: int) -> SlotView:
    # The tail's offset is ``_disk_window_len``, "how many window messages are now on
    # disk", which the save advances. Neither nearby counter can serve:
    # ``_resumed_count`` records only how many messages a rehydrate loaded and the
    # flush never advances it, so a persisted tail would stay in the slice and be
    # copied twice; and a length captured on a ``_dirty`` transition cannot tell
    # "never flushed" from "flushed, then re-dirtied by an append".
    # Every field is read ONCE and the witness is taken from this view: a worker-
    # thread save stores ``_disk_window_len`` with no regard for the loop, so a
    # second read could land after it and vouch for a tail cut at the old boundary.
    boundary = slot._disk_window_len
    length = len(slot.messages)
    dirty = slot._dirty
    if purpose.boundary_ahead == "read":
        tail: list[dict] | None = []
    elif boundary <= length:
        # Authoritative and deliberately NOT gated on ``_dirty``: the slice is empty
        # exactly when everything is persisted, and a gate is what would let a flush
        # clearing ``_dirty`` under the read skip the merge and drop the tail.
        tail = list(slot.messages[boundary:])
    elif dirty:
        tail = None
    else:
        tail = []
    return SlotView(
        attempt=attempt,
        boundary=boundary,
        generation=getattr(slot, "_dirty_gen", 0),
        length=length,
        older=slot._disk_older_count,
        durable_older=_durable_older(slot),
        dirty=dirty,
        tail=tail,
    )


def _refuse_untrusted_disk(slot: _ChatSlot, purpose: SnapshotPurpose) -> None:
    # While a rewind/regenerate rewrite is owed, disk holds the PRE-EDIT transcript,
    # longer than the window, so the boundary slice appends nothing and the copy
    # would carry turns the user rewound away.
    if purpose.pending_rewrite == "refuse" and slot._pending_rewrite:
        raise SnapshotUnstable("a pending rewrite means the on-disk transcript is stale")
    # The boundary can run AHEAD of the window: the save sets ``_disk_window_len``
    # over the RAW window, streaming chunk rows included, and ``_flush_segment`` then
    # drops that chunk run without moving the boundary, so the slice yields nothing.
    if purpose.boundary_ahead == "refuse" and slot._disk_window_len > len(slot.messages):
        raise SnapshotUnstable(
            "the persisted boundary is ahead of the resident window (a flush landed mid-stream)"
        )


def _refuse_deleted(
    state: DashboardState, slot: _ChatSlot, purpose: SnapshotPurpose, when: str
) -> None:
    # Independent of any save the reader made: the periodic flush can hit the
    # delete-won guard first and clear ``_dirty``, after which no save of the
    # reader's ever returns the False that would have told it.
    if purpose.refuse_deleted and session_was_deleted(state, slot):
        name = purpose.logger or __name__
        logging.getLogger(name).warning(
            "%s: slot=%s %s; refusing the %s",
            name.rsplit(".", 1)[-1],
            slot.key,
            when,
            purpose.noun,
        )
        raise SnapshotUnstable("the session was permanently deleted")


async def read_consistent_transcript(
    state: DashboardState,
    slot: _ChatSlot,
    purpose: SnapshotPurpose,
    read: Callable[[SlotView], Awaitable[T]],
    *,
    persist: Callable[[], Awaitable[object]] | None = None,
    rewrite: Callable[[], Awaitable[object]] | None = None,
    discard: Callable[[T], object] | None = None,
) -> TranscriptSnapshot[T]:
    """Read *slot*'s transcript through *read* on one consistent view of the slot.

    **Run on the event loop**, and hold whatever serialises the reader (the fork
    holds ``slot._fork_lock``) for the duration. Every observation of the slot is
    taken here, on the loop; *read* is awaited once per attempt and is where the
    caller goes off the loop.

    *read(view)* performs one attempt's read and returns its result; it may capture
    more slot state first, on the loop, because nothing awaits between the
    observation in *view* and the call. It raises :class:`RetryRead` to spend the
    attempt. *persist()* saves the slot as it stands: before each read under
    ``persist_dirty``, and to re-sync a boundary ahead of a shrunken window under
    ``boundary_ahead="resync"``. *rewrite()* saves an owed truncating rewrite under
    ``pending_rewrite="save"``. Each is required by the rules that call it.
    *discard(result)* releases a read result this function will not return.

    A refusal under *purpose*'s rules raises :class:`SnapshotUnstable`, and so does
    spending every attempt. Anything *read*, *persist* or *rewrite* raises
    propagates unchanged, which is how a caller answers in its own terms.
    """
    if purpose.pending_rewrite == "save" and rewrite is None:
        raise TypeError("a purpose that saves a pending rewrite needs a rewrite callable")
    if (purpose.persist_dirty or purpose.boundary_ahead == "resync") and persist is None:
        raise TypeError("a purpose that persists the slot needs a persist callable")
    _refuse_untrusted_disk(slot, purpose)
    # Carries a SUSPICION across attempts. The rewrite save clears ``_pending_rewrite``
    # unconditionally once the archive-safe rewrite succeeds, with no check that the
    # flag it clears is the one its own snapshot was taken for, so a rewind landing
    # while that save is suspended has its flag erased; without this the next attempt
    # would read ``False`` and fall through to a disk holding the discarded turns.
    rewrite_owed = False
    for attempt in range(1, SNAPSHOT_ATTEMPTS + 1):
        if purpose.pending_rewrite == "save" and (slot._pending_rewrite or rewrite_owed):
            # Disk is KNOWN stale, and none of the counters observed below carries
            # that: ``chat_rewind`` sets ``_dirty``, zeroes ``_resumed_count`` and sets
            # the flag but never touches ``_disk_window_len``, so the boundary keeps
            # its pre-rewind value, the read would return the discarded turns, and the
            # re-check would PASS -- it measures stability, not correctness. Save,
            # then spend the attempt.
            generation = slot._dirty_gen
            await cast(Callable[[], Awaitable[object]], rewrite)()
            # ``_dirty_gen`` advances only on a True assignment, so the save clearing
            # ``_dirty`` cannot trip this; only a NEW mark landing during it does.
            rewrite_owed = slot._dirty_gen != generation
            continue
        if purpose.persist_dirty and slot._dirty:
            # The save writes the window it captured on entry, so an edit landing
            # inside it leaves disk on the EARLIER content: spend the attempt.
            generation = slot._dirty_gen
            await cast(Callable[[], Awaitable[object]], persist)()
            if slot._dirty_gen != generation:
                continue
            _refuse_untrusted_disk(slot, purpose)
        view = _observe(slot, purpose, attempt)
        held = tuple(getattr(view, name) for name in purpose.witness)
        # The other tail candidate, for a boundary ahead of a dirty window. It is
        # taken here, on the loop, so whichever is chosen pairs with this read.
        restore_tail = list(slot.messages[slot._resumed_count :]) if view.tail is None else None
        _refuse_deleted(state, slot, purpose, "belongs to a permanently deleted session")
        try:
            result = await read(view)
        except RetryRead:
            continue
        try:
            # Re-checked AFTER the read as well: a rewind or a delete can complete
            # inside it, and the counters alone do not reveal a rewind.
            _refuse_untrusted_disk(slot, purpose)
            _refuse_deleted(state, slot, purpose, "was permanently deleted during bundle assembly")
        except BaseException:
            if discard is not None:
                discard(result)
            raise
        # A rewind can also land during the read under ``"save"`` -- the fork's
        # ``_fork_lock`` has one acquirer in the tree and no rewind path takes it --
        # and no counter moves back for it, so the flag is checked on this side too
        # and the next attempt saves it.
        moved_rewrite = purpose.pending_rewrite == "save" and slot._pending_rewrite
        if moved_rewrite or _witness(slot, purpose) != held:
            if discard is not None:
                discard(result)
            if purpose.logger is not None:
                logging.getLogger(purpose.logger).debug(purpose.retry_log, slot.key)
            continue
        if view.tail is not None:
            return TranscriptSnapshot(result, view.tail, view)
        # The boundary is ahead AND the window is dirty, which has two causes needing
        # opposite remedies, and the read just supplied the one discriminator: the
        # true on-disk length. Disk holds ``older`` frozen rows followed by the
        # window, so more than ``older + length`` means rows the counters do not
        # represent -- a CAPPED RESTORE, which dropped leading messages from memory
        # without bumping ``_disk_older_count``. A save would write the smaller window
        # over them: its frozen prefix is keyed on that count, so it would be empty
        # and truncate disk to the window. Merge from ``_resumed_count`` instead,
        # which the restore set to exactly "how many resident messages came from disk".
        if len(cast(Sized, result)) > view.older + view.length:
            return TranscriptSnapshot(
                result, restore_tail or [], view, disk_holds_unrepresented=True
            )
        # The counters agree with disk, so the window shrank mid-stream (a
        # ``_flush_segment`` dropping a trailing chunk run) and a save is what
        # re-syncs the boundary; the next attempt slices from it.
        if discard is not None:
            discard(result)
        await cast(Callable[[], Awaitable[object]], persist)()
    raise SnapshotUnstable(f"transcript snapshot did not settle in {SNAPSHOT_ATTEMPTS} attempts")
