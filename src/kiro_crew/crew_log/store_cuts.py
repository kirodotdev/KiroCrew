"""The cut counter: how a reader learns a unit's history was cut back.

``.cuts`` beside the log (``_CUTS_FILE``) is a seqlock: it rises once before a cut and once
after it (``_open_cut``, ``_close_cut``, ``_note_cut_if_shorter``), so a reader holding the
value it saw last time can tell in one small read whether the bytes it folded are still the
bytes on disk (``read_cuts``). Every write of it is durable or reported (``_write_cuts``,
``_sync_cuts_dir``, ``_retire_cuts_inode``), a counter that cannot be written is made to
prove nothing (``_unprove_cuts``), and ``_seed_cuts`` starts it at create.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.crew_log.errors import CutUnaccounted
from kiro_crew.crew_log.store_paths import _mkdir_private, log_exception_text, logger

#: How many times this unit's history has been CUT BACK, as a decimal count.
#:
#: The store's rule is that a committed line is never rewritten, and two recovery
#: paths bend it: an unreachable chunk group is dropped and closers are appended
#: from the cut, and a failed append is removed after its bytes reached the disk.
#: Both leave a file that reuses seqs an earlier read already consumed, and both
#: are invisible to a reader -- the file is longer, its newest seq is higher, and
#: every stat says "appended to". A reader that wants to CONTINUE a fold over such
#: a file therefore has to be told, and this counter is how: it rises once before
#: each cut and once after it, so a reader holding the value it saw last time can
#: decide in one small read whether the bytes it already folded are still the bytes
#: on disk.
#:
#: A SEQLOCK, so the value counts cut PHASES rather than cuts. Even means settled;
#: odd means a cut is in flight and :func:`read_cuts` reports nothing at all, which
#: is what stops a reader that sampled halfway through a cut from matching the same
#: value again once the bytes have moved.
#:
#: Not a segment name, so retention and every segment walk ignore it, and it is
#: removed with the unit.
_CUTS_FILE = ".cuts"


def _cuts_path(directory: Path) -> Path:
    return directory / _CUTS_FILE


def _read_cuts_raw(directory: Path) -> int | None:
    """*directory*'s counter exactly as it is written, parity and all.

    The counter's PHASE, which is the question a WRITER has: :func:`_open_cut` has to
    advance from the number on disk even when that number is odd, and an odd number is
    precisely what :func:`read_cuts` refuses to hand back. ``None`` is narrower here too
    -- absent or unparseable, the two cases with no number to advance from -- because a
    reader's three situations collapse into one answer and a writer's do not.

    Private, and it has to stay that way: a READER that took an odd value at face value
    is the whole failure this file exists to prevent. Both functions parse here so there
    is one spelling of what the file says, rather than two waiting to disagree.
    """
    try:
        raw = _cuts_path(directory).read_bytes()
    except OSError:
        return None
    text = raw.decode("ascii", errors="replace").strip()
    if not text.isascii() or not text.isdecimal():
        return None
    try:
        return int(text)
    except ValueError:
        # All digits and still not a number this build will convert: CPython refuses a
        # decimal string past its integer-conversion limit (4300 digits by default), and
        # the counter is a plain file anything on the host can write. NOT PROVEN is the
        # same answer the other unparseable shapes get, so a counter too long to read
        # costs the digest rather than raising out of a fold that only wanted to know
        # whether a cut had happened.
        log_exception_text(
            logger, logging.DEBUG, "crew log cut counter for %s is not convertible", directory
        )
        return None


def _fresh_phase() -> int:
    """An odd phase no earlier reading of this counter can be holding.

    What a cut advances to when the counter is GONE or unparseable, which is the one
    case with no number to advance from. Starting the count again LOW is the thing that
    must not happen: a reader or a savepoint holds a real even number this unit issued,
    and a count that began again at 1 would close on 2 -- so a savepoint written before
    the lost cut would compare equal across it and be admitted over the records the cut
    removed.

    A nanosecond clock reading is around 1.7e18, far past any count a log reaches by
    being cut, so it cannot equal a value this unit issued by counting. The low bit is
    forced so the value is ODD, which keeps the cut in flight and is also what sends
    every reader still holding the lost count to the digest in the meantime.

    It need not be monotonic, unique, or ordered against anything: no caller compares
    two counts for order, the counter is only ever an equality test against one value
    this unit wrote down earlier. A collision would need the clock to come back to the
    same nanosecond, and the value it collided with to be one this unit had settled on.
    """
    return time.time_ns() | 1


def read_cuts(directory: Path) -> int | None:
    """*directory*'s cut counter, or ``None`` when it proves nothing.

    ``None`` means NOT PROVEN, and it covers three situations a caller must treat the
    same way. The counter may be ABSENT, which is every log written before it existed
    and every log that has never been cut. It may be there and UNREADABLE -- a torn
    write, a hand-edit, a permission fault. Or it may read ODD, which says a cut is in
    flight right now (:func:`_open_cut`), so the bytes under it are moving as this
    reader looks at them.

    All three answer ``None`` because the only correct response to any of them is to
    fall back to whatever stronger evidence the caller has. A caller that read an absent
    counter as "zero cuts" would compare equal across a cut whose counter was lost, and
    one that took an odd reading at face value would compare equal across a cut it
    caught halfway -- which are the two readings this file exists to prevent.
    """
    value = _read_cuts_raw(directory)
    if value is None:
        return None
    # ODD is a cut IN FLIGHT, which is not a value to compare. A reader admitted on an
    # odd reading would be one that sampled between the raise and the truncation, folded
    # the doomed records, and then found the same odd number again afterwards -- nothing
    # raises it a third time -- and continued over the bytes the cut replaced.
    return None if value % 2 else value


def _sync_cuts_dir(directory: Path) -> bool:
    """Force *directory*'s own entries out, so a counter just put there or taken away
    is still there or still gone after a power cut. ``True`` when it is.

    The half ``fsync=True`` does not cover. Syncing a descriptor forces the file's
    BYTES; the NAME that reaches them lives in the parent directory, so until that is
    synced a power-off can return from the rename -- or from the unlink -- and still
    come back to the entry that was there before.

    A failed sync answers ``False`` and the cut does not proceed on it, because an
    error here says the device refused a write and refusing the cut over that is cheap.

    ITS SILENCE IS NOT EVIDENCE, though, and no caller may read it as such.
    ``fsync_dir`` returns quietly where the platform cannot express a directory sync --
    Windows has no directory descriptor, and some filesystems reject ``fsync`` on a
    directory -- so ``True`` here means "nothing refused it", which on those
    filesystems is not the same as "the name is on disk". That is why every retirement
    and every publication goes through :func:`_retire_cuts_inode` first: emptying the
    file in place needs no directory entry, so what a reader comes back to after a lost
    rename or a lost unlink is a counter that proves nothing rather than a pre-cut
    count. This function makes the new name durable SOONER; it is not what makes the
    old value stop counting.

    A caller with nothing else holding correctness up -- the unlink fallback reached
    when the emptying FAILED -- must not use this one. :func:`_cuts_dir_forced` is the
    spelling for that, and it reports only a sync it actually performed.
    """
    try:
        fsync_dir(directory)
    except OSError:
        log_exception_text(
            logger,
            logging.DEBUG,
            "crew log cut counter directory %s could not be synced",
            directory,
        )
        return False
    return True


def _cuts_dir_forced(directory: Path) -> bool:
    """``True`` only when *directory*'s own entries were FORCED out, here, just now.

    The strict twin of :func:`_sync_cuts_dir`, and the two differ on exactly one case:
    a platform or filesystem that cannot express a directory sync. ``fsync_dir`` returns
    QUIETLY there -- Windows has no directory descriptor, and some filesystems reject
    ``fsync`` on a directory -- which is correct for a caller whose work is already
    committed and useless to one asking whether a NAME is on disk. So this does the
    sync itself and reports only what it actually did.

    Used where a directory change is the ONLY thing standing between a cut and a reader
    holding the count it removes: the unlink fallback in :func:`_unprove_cuts`, reached
    when the counter could not be emptied in place. Everywhere else the emptying carries
    correctness and the lenient spelling is right, because a cut refused on a filesystem
    with no directory sync would refuse every cut that platform ever makes.

    Nothing is logged for the unsupported case: it is not a fault, and the caller's
    answer to it is to refuse the cut rather than to report a problem.
    """
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        dir_fd = os.open(str(directory), flags)
    except OSError:
        return False
    forced = False
    try:
        os.fsync(dir_fd)
        forced = True
    except OSError:
        forced = False
    try:
        os.close(dir_fd)
    except OSError:
        # A deferred write error the kernel held back until the close, which is the same
        # signal as a failed fsync for a caller about to cut on the strength of it.
        return False
    return forced


def _retire_cuts_inode(directory: Path) -> bool:
    """Empty *directory*'s counter IN PLACE, durably. ``True`` when it is empty on disk.

    The one way to retire this counter that no filesystem can leave half-done, and the
    foundation every other durability claim here stands on. Emptying a file changes its
    CONTENTS, which an ``fsync`` of the descriptor forces, and leaves the directory
    entry exactly where it was -- so there is no name to publish, no rename to lose, and
    nothing owed to :func:`_sync_cuts_dir`. An empty file is not a decimal number, so
    :func:`read_cuts` answers ``None`` for it and every reader falls back to the digest.

    Why that matters more than it looks: a directory sync is not available everywhere.
    ``fsync_dir`` returns QUIETLY where the platform cannot express one -- Windows has no
    directory descriptor, and some filesystems reject ``fsync`` on a directory -- which
    is the right contract for it and the wrong evidence for a cut. A rename or an unlink
    there is as durable as the filesystem feels like being, so a cut that rested on one
    could come back to the PRE-CUT count beside a log that was truncated.

    ``True`` only for an emptying this performed, or for a counter that is already
    absent: there is no old value left to certify either way. A failure is ``False`` --
    the file may still hold what it held.

    Not needed at create (:func:`_seed_cuts`), where there is no earlier value.
    """
    path = _cuts_path(directory)
    try:
        with open(path, "r+b") as handle:
            handle.truncate(0)
            handle.flush()
            os.fsync(handle.fileno())
    except FileNotFoundError:
        return True
    except OSError:
        log_exception_text(
            logger, logging.DEBUG, "crew log cut counter for %s could not be emptied", directory
        )
        return False
    return True


def _write_cuts(directory: Path, value: int) -> bool:
    """Put *value* in *directory*'s counter, durably. ``True`` when it landed.

    THE OLD VALUE IS RETIRED FIRST, in place and durably
    (:func:`_retire_cuts_inode`), and that ordering is what makes this function's
    promise true on a filesystem that cannot sync a directory. ``atomic_write`` renames
    a NEW inode over the counter's name; if that rename's metadata never reaches disk,
    the name still points at the OLD inode -- and because this emptied that inode first,
    what a reader comes back to is a counter that proves nothing rather than the
    pre-cut count. Unproven is slower and never wrong; the pre-cut count beside a
    truncated log is the one wrong answer this file exists to prevent.

    So correctness rests on the emptying, which needs no directory entry, and the
    directory sync below is what it can actually be: the step that makes the new NAME
    durable sooner. It is still required to succeed, because an error from it (an
    ``EIO``, say) says the device refused a write, and refusing the cut over that is
    cheap. Its SILENCE carries nothing, and nothing here reads it as proof.

    An absent counter has no old value to retire, which is the create path and every log
    whose counter was lost.
    """
    if not _retire_cuts_inode(directory):
        return False
    try:
        _mkdir_private(directory)
        atomic_write(_cuts_path(directory), f"{value}\n", fsync=True, newline="\n")
    except OSError:
        log_exception_text(
            logger, logging.DEBUG, "crew log cut counter for %s could not be written", directory
        )
        return False
    return _sync_cuts_dir(directory)


def _unprove_cuts(directory: Path) -> bool:
    """Make *directory*'s counter prove nothing, without needing a byte of space.

    The fallback when :func:`_write_cuts` cannot raise the counter, and the reason a
    failed raise is not a wrong answer. Removing the file, or emptying it in place, both
    make :func:`read_cuts` answer ``None``, which sends every reader to the digest or to
    a fold from empty.

    TWO WAYS, and the DURABLE one first. Emptying the file in place
    (:func:`_retire_cuts_inode`) changes its contents, which an ``fsync`` of the
    descriptor forces, and touches no directory entry -- so it is as real as the device
    is, on every filesystem. Unlinking is the tidier result but it publishes a
    directory change, and a directory sync is not available everywhere, so an unlink
    alone can come back. Doing the emptying first makes the unlink's durability stop
    mattering: if the removal is lost, the name points at an inode that is already
    empty, which reads as nothing proven.

    Neither needs a byte of free space, which is why both are here. A full disk is the
    likely cause of a failed raise -- the append that triggered the rollback failed for
    the same reason -- so a path that had to allocate would fail for the same reason
    again.

    ``True`` ONLY where the retirement is one this function performed and made durable.
    Anything else is ``False``, which refuses the cut, and the asymmetry is deliberate:
    this answer is a claim about what a READER will find, so it may rest on an action
    taken, never on a reading taken. Asking :func:`read_cuts` afterwards would be
    exactly that -- it answers ``None`` for an UNREADABLE counter as readily as for an
    absent one, so a counter held open by another process, with its old value still on
    disk, would certify its own retirement and then come back the moment the handle
    closed. A Windows sharing lock denies the replace, the unlink and the open together,
    which is the one fault that reaches all three attempts here.
    """
    if _retire_cuts_inode(directory):
        # Retired for good. The unlink after it is housekeeping -- it stops a file that
        # proves nothing from sitting in the unit's directory -- so its failure, and the
        # durability of the directory change it makes, are both beside the point.
        try:
            _cuts_path(directory).unlink()
        except OSError:
            pass
        return True
    # The emptying failed, so the file may still hold what it held, and an unlink is all
    # that is left. It is the WEAKER way, because it publishes a directory change, and
    # correctness here rests on nothing else -- so the sync is required to have actually
    # HAPPENED (:func:`_cuts_dir_forced`) rather than merely not to have complained.
    # `_sync_cuts_dir` would answer `True` on a filesystem that cannot express a
    # directory sync at all, which is the reading this whole file refuses to make.
    try:
        _cuts_path(directory).unlink()
    except FileNotFoundError:
        # Gone between the failed open and here. The absence still has to be made
        # durable: a removal nothing synced can be sitting in unwritten directory
        # entries, and the file that comes back would hold the count this cut removes.
        return _cuts_dir_forced(directory)
    except OSError:
        log_exception_text(
            logger, logging.DEBUG, "crew log cut counter for %s could not be retired", directory
        )
        # Nothing here changed what a reader will find. Whether the counter is readable
        # right now does not decide that: an unreadable counter still holds its old
        # value, and reporting it retired is how a cut proceeds over a count that comes
        # back to certify the bytes it removed.
        return False
    return _cuts_dir_forced(directory)


def _open_cut(directory: Path) -> None:
    """Declare a cut of *directory*'s log IN FLIGHT, durably, before it happens.

    Half of a seqlock, and the half that must not be skipped. It raises the counter to
    an ODD number, which :func:`read_cuts` refuses to return -- so a reader that samples
    anywhere inside the cut is told "not proven" rather than handed a value it could
    match again afterwards. :func:`_close_cut` raises it back to EVEN once the bytes
    have settled, and that second raise is what makes a reading taken before a cut
    differ from one taken after it.

    Counting PHASES rather than cuts is the whole point. A single raise brackets "a cut
    STARTED since you last looked", which is not the question: a reader that sampled the
    raised value, folded the records the cut was about to remove, and sampled the same
    value again would compare equal across a completed rewrite.

    NO VALUE IS EVER ISSUED TWICE, which is the invariant behind every reading of this
    counter: a held count is believed because it could only have come from the state it
    was taken in. So the phase advances from what is ON DISK, odd readings included, and
    where the file says nothing it jumps somewhere no earlier reading can sit
    (:func:`_fresh_phase`) instead of counting up from one. A cut left unclosed is the
    case that makes this load-bearing -- a counter that recovered from it by restarting
    would close on the very value the interrupted cut had moved away from.

    RAISES :class:`CutUnaccounted` when it cannot make the counter stop proving the old
    value (:func:`_unprove_cuts`). Everywhere else this counter is an optimisation token
    and a write fault only costs speed, but not here: cutting while a reader can still
    read a count that certifies the bytes being removed is a wrong answer that persists,
    since the slot savepoint records the same value. Refusing the cut leaves the file as
    it was, which the next open repairs again.

    That refusal is an ``OSError``, not a :class:`CrewLogError`, and the difference is
    whether a caller's entry survives. Every cut here is a RECOVERY the caller is doing
    on its way to something else -- repairing a torn tail before an append, dropping an
    unreachable group at open -- so this exception surfaces as that caller's failure.
    The write-behind reads a `CrewLogError` as "declined on its own merits, retrying is
    pointless" and drops the entry; the causes here are storage faults that CLEAR, so a
    transient lock on the counter would permanently lose a valid event. See
    :class:`CutUnaccounted`.
    """
    raw = _read_cuts_raw(directory)
    if raw is None:
        # Absent or unparseable: there is no number to advance from, so the count starts
        # again somewhere no earlier reading of this unit can sit. Starting at 1 would
        # close on 2, and 2 is a value an old savepoint or warm memo may still hold.
        value = _fresh_phase()
    elif raw % 2:
        # A cut that was opened and never closed -- a crash after the raise, or a close
        # whose write failed. Advancing by TWO keeps the value odd, so this cut is in
        # flight like any other, and it never lands on raw - 1: that is the settled
        # value readers held before the unclosed cut, and the one value re-issuing would
        # let a stale fold match across both cuts.
        value = raw + 2
    else:
        value = raw + 1
    if _write_cuts(directory, value):
        return
    if _unprove_cuts(directory):
        return
    raise CutUnaccounted(
        f"crew log cut counter for {directory} can neither be raised nor retired, "
        "so a reader could keep a fold built from the bytes this cut removes"
    )


def _close_cut(directory: Path) -> None:
    """Declare *directory*'s in-flight cut SETTLED, durably, after the bytes moved.

    The other half of the seqlock: the counter goes back to EVEN, and the value it lands
    on differs from the one it held before the cut, so no reader holding either can
    compare equal across it.

    Best-effort and silent, because failing is already safe. A counter left ODD reads as
    a cut in flight forever, which every reader answers by falling back to the digest or
    folding from empty -- slower, never wrong. That is also what a crash between the
    truncation and this call leaves behind, so the failure and the crash need one answer
    rather than two.
    """
    now = _cuts_path(directory)
    try:
        raw = now.read_bytes().decode("ascii", errors="replace").strip()
        current = int(raw) if raw.isdecimal() else None
    except (OSError, ValueError):
        current = None
    if current is None:
        # Nothing to close: the counter was retired instead of raised, so it already
        # proves nothing and writing an even number here would restore a proof for a
        # cut nothing recorded the start of.
        return
    _write_cuts(directory, current + 1)


def _note_cut_if_shorter(path: Path, size: int) -> bool:
    """Open a cut of *path* down to *size*, unless it would remove nothing.

    ``True`` when a cut was declared and :func:`_close_cut` is owed; ``False`` when
    there was nothing to declare.

    A cut that removes no byte changes no reader's evidence, so declaring it would send
    every reader of this unit back to the slow path for a mutation that did not happen.
    The guard also covers the rollback of a write that reached no byte of the file at
    all, which is an ordinary outcome rather than a cut.

    A stat that fails counts the cut. Not knowing the file's length is not grounds for
    claiming nothing was removed.
    """
    try:
        current: int | None = path.stat().st_size
    except OSError:
        current = None
    if current is None or current > size:
        _open_cut(path.parent)
        return True
    return False


def _seed_cuts(directory: Path) -> None:
    """Start *directory*'s cut counter at zero, at create.

    Written at CREATE and nowhere else, so the file is never produced by a read. What
    it buys is that a log whose counter exists is on the cheap read path from its first
    entry: an absent counter is unproven rather than zero, so a reader that found none
    would fall back to hashing the records it folded for the whole life of the log.

    Zero is EVEN, which is what "settled, no cut in flight" reads as. There is no
    earlier value to retire here, which is the one call where
    :func:`_retire_cuts_inode` has nothing to do.

    Best-effort, and silent when it fails: at create there is nothing to protect yet, so
    a failure costs this log the cheap read path and a create that refused over it would
    refuse a perfectly good crew log.
    """
    _write_cuts(directory, 0)
