"""Writing a segment: one durable append, its rollback, and the one allowed truncation.

``_append_line`` and ``_append_lines`` write and fsync through ``_write_then_sync``, which
puts the previous size back when the write or the sync fails (``_rollback_append``).
``_truncate`` -- and ``_truncate_now``, for a caller that already declared the cut --
drops a torn tail, the store's only mutation of written bytes. Each of those cuts is
declared on the cut counter before the bytes move. A new way bytes reach a segment goes
here.
"""

from __future__ import annotations

import os
from pathlib import Path

from kiro_crew.crew_log.errors import CrewLogError, IndeterminateAppend
from kiro_crew.crew_log.store_cuts import _close_cut, _note_cut_if_shorter
from kiro_crew.crew_log.store_paths import _mkdir_private
from kiro_crew.owner_only_files import owner_only_opener


def _append_line(path: Path, line: str, *, needs_newline: bool) -> None:
    """Append *line* plus its terminator, then fsync.

    The handle is BINARY, which is what keeps the terminator exactly one ``\\n``:
    text mode on Windows translates it to ``\\r\\n``, and the byte offsets the
    torn-tail truncation computes then stop matching what is on disk.

    *needs_newline* re-supplies a separator the previous write lost -- the one
    case where a record survived but its terminator did not. It PREPENDS rather
    than rewriting that line, so the append-only rule holds.

    A failure ROLLS BACK. See :func:`_write_then_sync`.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    _write_then_sync(path, f"{prefix}{line}\n".encode())


def _append_lines(path: Path, lines: "list[str]", *, needs_newline: bool) -> None:
    """Append every line in ONE write and ONE fsync.

    The atomicity this buys is not a filesystem guarantee -- a write can still tear
    -- but it collapses the window: a group written this way is on disk as a whole
    or ends in a torn tail the existing truncation removes, instead of leaving each
    line separately durable and the group half-present.

    Same binary-handle and *needs_newline* rules as :func:`_append_line`, and the
    same rollback on failure, for the same reasons.
    """
    _mkdir_private(path.parent)
    prefix = "\n" if needs_newline else ""
    body = "".join(f"{line}\n" for line in lines)
    _write_then_sync(path, f"{prefix}{body}".encode())


def _write_then_sync(path: Path, blob: bytes) -> None:
    """Append *blob* and fsync it, restoring the previous size if either fails.

    The rollback is what makes a failure DEFINITE. Bytes reach the file before the
    fsync runs, so a failing fsync leaves an outcome nobody knows: the entry may well
    be durable. The write-behind retains and retries a failure, and a retry against
    an unknown outcome either writes the same fact twice under two seqs -- a duplicate
    no reader can tell from a real repeat -- or has to reason about what is already
    there. Truncating back to the size the file had before removes the question:
    either the append is whole and synced, or it is gone and the retry writes it
    cleanly.

    A partial write is rolled back for the same reason, and one more: leaving half a
    line behind would make the next append's torn-tail truncation the thing that
    cleans up, so the file would carry a fragment until then.

    If the ROLLBACK itself fails -- likely, since whatever broke the write is often
    still broken -- the file may hold bytes nobody can account for, and that is what
    :class:`IndeterminateAppend` reports. Its bytes are unterminated or unparseable
    at the tail, which is exactly the shape the next ``open`` truncates, so the
    recovery already exists; the distinct type is so a caller can tell "nothing
    happened" from "something may have".

    "Fails" there covers the rollback REFUSING as well as erroring. The rollback counts
    its cut, and on a filesystem that has gone read-only it can neither record the cut
    nor disown the count, so it declines to truncate and raises a
    :class:`CrewLogError`. The bytes are then still in the file, which is this clause's
    case exactly -- so it is reported as indeterminate and the entry stays retryable.
    Reported as a refusal it would be read as "declined before any byte was written",
    and the event would be dropped over a fault that recovery clears.

    Truncating is safe against a concurrent writer because every caller reaches here
    holding the session's append lock, so no other handle can have appended between
    this write and its rollback -- the bytes removed can only be this call's own.
    """
    with open(path, "ab", opener=owner_only_opener) as handle:  # conversation bodies: 0600
        before = handle.tell()
        try:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        except OSError as exc:
            try:
                handle.close()
                _rollback_append(path, before)
            # The rollback REFUSING lands here too, because the cut it has to account
            # for can be refused (`CutUnaccounted`, an OSError) and then the bytes are
            # left in place deliberately. That is this clause's case exactly: the file
            # has been touched and the outcome is unknown. CrewLogError is named as a
            # BACKSTOP rather than for a refusal that reaches here today -- anything
            # raised once this append's bytes are on disk has to be reported as
            # indeterminate, since the writer reads a CrewLogError as "declined before
            # any byte" and drops the event instead of retrying it.
            except (OSError, CrewLogError) as undo:
                raise IndeterminateAppend(
                    f"append to {path.name} failed ({exc}) and could not be rolled "
                    f"back ({undo}): the file may hold bytes no entry claims",
                    written=blob,
                    offset=before,
                ) from exc
            raise


def _rollback_append(path: Path, size: int) -> None:
    """Cut *path* back to *size* and fsync it.

    Not an exception to the never-rewrite rule: the bytes removed are the ones the
    caller's own failed append just wrote, so this restores the file to a state a
    reader could already have seen rather than editing history. It is the same
    category as the torn-tail truncation, moved to the moment the tear happens
    instead of the next open.

    "A state a reader could already have seen" is also why the cut is COUNTED. The
    bytes removed were flushed before the fsync that failed, so a reader holding no
    lock can have read them as a committed entry -- and the retry then writes a
    different entry under the same seq. That is the same evidence problem the
    torn-tail cut has, and it gets the same accounting.

    The counter is opened before and closed after, so a reader that samples it at any
    point during this function is told the bytes are moving rather than handed a value
    it could match again once they have. The close runs in a ``finally`` because a
    truncation that RAISED may still have moved bytes, so every reader holding the old
    value has to fold cold either way -- and the only reading that says so is a settled
    one that differs from it. Leaving the counter odd forever would also be safe, and it
    would pin this unit to the slow path for the rest of its life.

    The open can REFUSE -- on a filesystem gone read-only the count can neither be
    recorded nor disowned -- and then nothing is truncated and this raises before the
    ``try``. :func:`_write_then_sync` turns that into an :class:`IndeterminateAppend`
    rather than letting it out as a refusal, because the failed append's bytes are still
    in the file and the entry is still worth retrying.
    """
    opened = _note_cut_if_shorter(path, size)
    try:
        with open(path, "r+b") as handle:
            handle.truncate(size)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        if opened:
            _close_cut(path.parent)


def _truncate(path: Path, offset: int) -> None:
    """Drop everything at or after *offset* -- the one allowed mutation.

    Bracketed by the unit's cut counter, because this is the one place the mutation
    happens: every caller that cuts a segment reaches here, so accounting for it here is
    the only placement a later caller cannot forget. The counter is raised to an ODD
    value before the truncation and back to an EVEN one after it, so a reader that
    samples it mid-cut is told "not proven" and a reader that samples before and after
    sees two different values. See :func:`_open_cut`.
    """
    opened = _note_cut_if_shorter(path, offset)
    try:
        _truncate_now(path, offset)
    finally:
        if opened:
            _close_cut(path.parent)


def _truncate_now(path: Path, offset: int) -> None:
    """:func:`_truncate`'s bytes, with the cut already declared in flight."""
    with open(path, "r+b") as handle:
        handle.truncate(offset)
        handle.flush()
        os.fsync(handle.fileno())
