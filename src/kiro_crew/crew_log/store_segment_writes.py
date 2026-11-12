"""Writing a segment: one durable append, its rollback, and the one allowed truncation.

``_append_line`` and ``_append_lines`` write and fsync through ``_write_then_sync``, which
puts the previous size back when the write or the sync fails (``_rollback_append``).
``_truncate`` -- and ``_truncate_now``, for a caller that already declared the cut --
drops a torn tail, the store's only mutation of written bytes. Each of those cuts is
declared on the cut counter before the bytes move. A new way bytes reach a segment goes
here.
"""

from __future__ import annotations

import hashlib
import os
import threading
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

    The rollback makes a failure DEFINITE. Bytes reach the file before the fsync, so a
    failed fsync leaves an unknown outcome, and the write-behind's retry would either
    write the same fact twice under two seqs or have to reason about what is there.
    After truncating back, the append is either whole and synced or gone. A partial
    write is rolled back too, so no half line waits for the next torn-tail truncation.

    If the ROLLBACK fails (likely, since the fault often persists) the file may hold
    unaccounted bytes: :class:`IndeterminateAppend`. The next ``open`` truncates that
    tail; the distinct type tells "nothing happened" from "something may have". A
    rollback that REFUSES counts too: on a read-only filesystem it cannot record its cut,
    so it raises :class:`CrewLogError` without truncating. That is reported as
    indeterminate so the entry stays retryable; as a refusal the writer would drop it.

    Truncating is safe because every caller holds the session's append lock, so the bytes
    removed can only be this call's own.
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
            # A refused rollback (`CutUnaccounted`, an OSError) leaves the bytes in place,
            # so the outcome is unknown. CrewLogError is a BACKSTOP: anything raised once
            # bytes are on disk must be indeterminate, since the writer reads CrewLogError
            # as "declined before any byte" and drops the event instead of retrying it.
            except (OSError, CrewLogError) as undo:
                _note_residue(path, before, blob)
                raise IndeterminateAppend(
                    f"append to {path.name} failed ({exc}) and could not be rolled "
                    f"back ({undo}): the file may hold bytes no entry claims",
                    written=blob,
                    offset=before,
                ) from exc
            raise


#: path -> ``(offset, marks)`` of an append whose rollback failed
#: (:class:`IndeterminateAppend`), undone by :func:`_settle_residue` under the unit's
#: lock before that path's next append. ``marks`` is one ``(end, sha256 of the bytes up
#: to end)`` per line of the failed write, so no payload is held. Keyed by path, so a
#: handle reopened between the failure and the retry still finds it.
_RESIDUE: "dict[Path, tuple[int, tuple[tuple[int, bytes], ...]]]" = {}

#: Most residues held at once, oldest forgotten first. A forgotten one costs what it
#: cost before this map existed: the retry may write that run again.
_RESIDUE_MAX = 64

#: Guards :data:`_RESIDUE`: the writer's lanes append to different units at once.
_RESIDUE_LOCK = threading.Lock()


def _note_residue(path: Path, offset: int, blob: bytes) -> None:
    """Remember where *blob* went, as line-end digests, for :func:`_settle_residue`."""
    digest = hashlib.sha256()
    marks: "list[tuple[int, bytes]]" = []
    start = 0
    while start < len(blob):
        end = blob.find(b"\n", start)
        end = len(blob) if end < 0 else end + 1
        digest.update(blob[start:end])
        marks.append((end, digest.copy().digest()))
        start = end
    with _RESIDUE_LOCK:
        _RESIDUE.pop(path, None)
        _RESIDUE[path] = (offset, tuple(marks))
        while len(_RESIDUE) > _RESIDUE_MAX:
            _RESIDUE.pop(next(iter(_RESIDUE)))


def _settle_residue(path: Path) -> None:
    """Undo *path*'s leftover failed-append bytes, if any. The unit's lock is held.

    A retry of an :class:`IndeterminateAppend` would otherwise write the same entries a
    second time under new seqs. A cut that raises keeps the entry for the next try.
    """
    with _RESIDUE_LOCK:
        residue = _RESIDUE.get(path)
    if residue is not None:
        _undo_residue(path, *residue)
        with _RESIDUE_LOCK:
            _RESIDUE.pop(path, None)


def _undo_residue(path: Path, offset: int, marks: "tuple[tuple[int, bytes], ...]") -> None:
    """Finish the rollback an :class:`IndeterminateAppend` could not do. Lock held.

    Cuts *path* back to *offset* only when everything after *offset* is the failed
    write or a prefix of it: whole lines whose running digest matches a mark, then at
    most an unterminated piece of the next line. Then those bytes are this append's own
    and nothing came after them. Without this a retry appends the same entries again
    under new seqs, and a group commit's whole run is on disk twice. Anything else on the
    tail is left for the torn-tail repair, as before. A cut that fails raises, so the
    retry fails too and stays retryable.
    """
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    held = size - offset
    if held <= 0 or not marks or held > marks[-1][0]:
        return
    end, want = 0, b""
    for mark_end, mark_digest in marks:
        if mark_end > held:
            break
        end, want = mark_end, mark_digest
    with open(path, "rb") as handle:
        handle.seek(offset)
        ours = handle.read(held)
    # A PARTIAL write counts too: its complete lines would survive the torn-tail repair.
    if end and hashlib.sha256(ours[:end]).digest() != want:
        return
    if b"\n" in ours[end:]:
        return
    _truncate(path, offset)


def _rollback_append(path: Path, size: int) -> None:
    """Cut *path* back to *size* and fsync it.

    Not an exception to the never-rewrite rule: the bytes removed are this caller's own
    failed append, the same category as the torn-tail truncation, done at the tear.

    The cut is COUNTED because the bytes were flushed before the failed fsync, so a
    lockless reader may have read them as a committed entry that the retry then replaces
    under the same seq. The counter is opened before and closed after, so a sampling
    reader sees bytes moving; the close is in a ``finally`` because a truncation that
    RAISED may still have moved bytes. Leaving it odd would be safe but pin the unit to
    the slow path forever.

    The open can REFUSE (read-only filesystem), and then nothing is truncated and this
    raises before the ``try``; :func:`_write_then_sync` reports that as
    :class:`IndeterminateAppend` because the bytes are still there and worth retrying.
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
