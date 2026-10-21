"""A batch's manifest: writing it, reading it back under a size cap, and syncing a batch.

An owner module of :mod:`kiro_crew.session_storage`, the one import path: callers,
tests and documents reach every name here as ``kiro_crew.session_storage.<name>``.

The manifest is the append-only record of what a batch holds: a header, then one record
per staged session. This owner writes the header and each entry, reads the records under
``_MANIFEST_RECORD_CAP``, summarizes and rewrites a manifest, lists and digests the staged
paths it names, checks that a header names the batch it was read from, and makes a
batch's directories durable (:func:`_sync_batch`).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import IO, Any

from kiro_crew.atomic_write import atomic_write, fsync_dir
from kiro_crew.session_storage_parts.store_layout import MANIFEST_NAME, MANIFEST_SCHEMA, logger


def _write_header(handle: IO[str], batch_id: str, created_at: float, reason: str) -> None:
    header = {
        "schema": MANIFEST_SCHEMA,
        "batch_id": batch_id,
        "created_at": created_at,
        "reason": reason,
    }
    handle.write(json.dumps(header) + "\n")
    handle.flush()


def _append_entry(handle: IO[str], entry: dict[str, Any]) -> None:
    """Record one moved session and flush, so an interruption loses at most the
    session currently in flight."""
    handle.write(json.dumps(entry) + "\n")
    handle.flush()


# Longest manifest record accepted, in characters (terminator excluded: the
# threshold applies to the stripped record). A real record is a header or one
# session's file list — kilobytes at most. The manifest lives in an
# agent-writable tree, so a reader that trusts line length would hand a single
# multi-gigabyte no-newline line one allocation; a record past this cap ABORTS the
# read instead (the whole batch is then treated as having no readable manifest,
# exactly like an unreadable file). Skipping just the record would be worse than
# failing: a partial restore REWRITES the manifest from the records it parsed, so
# a skipped record's staged files would be silently orphaned. The transient peak
# on a hostile manifest is a small multiple of this value (up to two cap-sized
# reads concatenated, times Python's up-to-4-bytes-per-char widening), flat
# regardless of file size — this constant is the dial if that ceiling moves.
_MANIFEST_RECORD_CAP = 8 * 1024 * 1024


class _OversizedManifestRecord(Exception):
    """A manifest record exceeded ``_MANIFEST_RECORD_CAP``; the batch is unreadable."""


# Named by the facade, like SessionStorageError: its repr and a traceback name that path.
_OversizedManifestRecord.__module__ = "kiro_crew.session_storage"


def _manifest_records(handle: IO[str], batch: Path) -> Iterator[dict[str, Any]]:
    """Yield each JSON object record of an open manifest, header first.

    Record boundaries match ``str.splitlines``, so a manifest split on any unicode
    line boundary parses identically:
    reads are accumulated in a carry-over buffer and re-split per chunk, so a
    cap-sized read ending mid-record never invents or destroys a boundary. Blank
    and non-dict lines are skipped silently. A record that fails to parse is
    skipped and counted: a trailing partial line (a crash mid-append) is expected
    and logged at debug, while any other unparseable record — mid-file corruption —
    gets one aggregated warning per read. Either way every complete
    record before it describes real moved files that must stay restorable, so a
    parse failure never fails the batch wholesale.

    A record longer than ``_MANIFEST_RECORD_CAP`` is different: it raises
    :class:`_OversizedManifestRecord` (readers map it to ``None``, the
    no-readable-manifest posture) rather than being skipped, and its bytes are
    never materialised past the cap. Skipping would lose data: ``restore()``
    rewrites the manifest from the records it parsed, so a skipped record's
    staged files would be orphaned. Aborting keeps every file protected by the
    unlisted-file guards until a human looks.

    The skip counting and its log lines run only when the generator is driven to
    exhaustion; both callers do, and a future caller that stops early forfeits
    them knowingly.
    """
    corrupt = 0
    trailing_partial = False
    buf = ""  # partial record carried between reads; capped below

    def _parse(piece: str) -> dict[str, Any] | None:
        nonlocal corrupt
        piece = piece.strip()
        if not piece:
            return None
        if len(piece) > _MANIFEST_RECORD_CAP:
            raise _OversizedManifestRecord(batch.name)
        try:
            record = json.loads(piece)
        except ValueError:
            corrupt += 1
            return None
        return record if isinstance(record, dict) else None

    while True:
        chunk = handle.readline(_MANIFEST_RECORD_CAP)
        if not chunk:
            break
        data = buf + chunk
        pieces = data.splitlines()
        # The final piece is complete iff data ends on a line boundary. \n is the
        # common case; the appended probe catches every other splitlines boundary
        # (\u2028, \x1c, ...) without enumerating them.
        ends_on_boundary = data.endswith("\n") or len((data + "x").splitlines()) > len(pieces)
        if ends_on_boundary:
            complete, buf = pieces, ""
        else:
            complete, buf = pieces[:-1], pieces[-1]
        for piece in complete:
            record = _parse(piece)
            if record is not None:
                yield record
        if len(buf) > _MANIFEST_RECORD_CAP:
            raise _OversizedManifestRecord(batch.name)
    # EOF: whatever is left in buf is a genuinely unterminated final line. It can
    # never exceed the cap here — an over-cap carry raised above — and the assert
    # keeps that invariant enforced if the loop is ever reshaped.
    assert len(buf) <= _MANIFEST_RECORD_CAP
    if buf:
        stripped = buf.strip()
        if stripped:
            try:
                record = json.loads(stripped)
            except ValueError:
                trailing_partial = True
            else:
                if isinstance(record, dict):
                    yield record
    if corrupt:
        # %r: the batch name is a directory name from an agent-writable tree, so it
        # can embed newlines; the repr keeps one log record from forging others.
        logger.warning("trash manifest in %r: skipped %d unparseable line(s)", batch.name, corrupt)
    if trailing_partial:
        logger.debug("trash manifest in %r ends in a partial line (crash mid-append)", batch.name)


def _read_manifest(
    batch: Path, *, dir_fd: int | None = None, expect_ino: int | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    """Parse a batch manifest into its header and session entries.

    Streams the file line by line rather than materialising it as one string; the
    entries list itself is still O(sessions), which the callers that rewrite or
    enumerate the manifest genuinely need. Callers that need only aggregates should
    use :func:`_summarize_manifest` instead. A trailing partial line (a crash
    mid-append) is skipped rather than failing the whole batch — every complete
    line before it describes real moved files that must stay restorable (see
    :func:`_manifest_records`).

    *expect_ino* refuses a manifest that is not the file the caller already identified,
    checked by ``fstat`` on the OPEN handle rather than by a stat of the name - so there is
    no interval between the check and the read for a replacement to land in. A caller that
    needs both the header and the listing to describe ONE file passes it and makes a single
    call: two calls are two opens, and a manifest rewritten between them lets the header
    check pass on one file while the listing that authorises a deletion comes from another.
    """
    header: dict[str, Any] | None = None
    entries: list[dict[str, Any]] = []
    try:
        with _open_manifest(batch, dir_fd) as handle:
            if expect_ino is not None and os.fstat(handle.fileno()).st_ino != expect_ino:
                return None
            for record in _manifest_records(handle, batch):
                if header is None:
                    header = record
                    continue
                entries.append(record)
    except OSError:
        return None
    except _OversizedManifestRecord:
        logger.warning(
            "trash manifest in %r has a record over %d chars; treating it as unreadable",
            batch.name,
            _MANIFEST_RECORD_CAP,
        )
        return None
    if header is None or header.get("schema") != MANIFEST_SCHEMA:
        return None
    return header, entries


def _open_manifest(batch: Path, dir_fd: int | None) -> IO[str]:
    """Open *batch*'s manifest, by path or relative to an already-pinned descriptor.

    With *dir_fd* the read is bound to the directory the caller pinned rather than to
    whatever answers to the batch's name by the time the read happens. That matters where
    the manifest's numbers and the directory's identity are recorded together: derived from
    one pinned object they describe the same batch, while a path-based read and a separate
    stat can straddle a swap and approve a replacement's identity with the original's
    numbers. ``O_NOFOLLOW`` because a link at that name is never this batch's manifest.
    """
    if dir_fd is None:
        return (batch / MANIFEST_NAME).open(encoding="utf-8")
    fd = os.open(MANIFEST_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    try:
        return os.fdopen(fd, encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _summarize_manifest(
    batch: Path, *, dir_fd: int | None = None
) -> tuple[dict[str, Any], int, int] | None:
    """The manifest's header plus (session count, staged byte total).

    A batch can hold six figures of sessions, and the trash listing needs only
    these two aggregates — so they are accumulated during the same single streamed
    pass :func:`_read_manifest` makes, without ever holding the parsed entries.
    Guards match :func:`_read_manifest` exactly: same skipped-line tolerance, same
    schema rejection, and ``None`` on an unreadable manifest.
    """
    header: dict[str, Any] | None = None
    sessions = 0
    staged_bytes = 0
    try:
        with _open_manifest(batch, dir_fd) as handle:
            for record in _manifest_records(handle, batch):
                if header is None:
                    header = record
                    continue
                sessions += 1
                staged_bytes += _entry_bytes(record)
    except OSError:
        return None
    except _OversizedManifestRecord:
        logger.warning(
            "trash manifest in %r has a record over %d chars; treating it as unreadable",
            batch.name,
            _MANIFEST_RECORD_CAP,
        )
        return None
    if header is None or header.get("schema") != MANIFEST_SCHEMA:
        return None
    return header, sessions, staged_bytes


def _rewrite_manifest(batch: Path, header: dict[str, Any], entries: list[dict[str, Any]]) -> None:
    """Replace and sync a manifest after a partial restore."""
    header_line = json.dumps(header) + "\n"
    entry_lines = "".join(json.dumps(entry) + "\n" for entry in entries)
    atomic_write(batch / MANIFEST_NAME, header_line + entry_lines, fsync=True)
    # ``fsync=True`` made the new CONTENT durable; this makes the rename that gave
    # it the manifest's name durable. Without it a power-off just after this returns
    # can come back to the pre-restore manifest, which lists files restore has
    # already moved back out — an entry that now names live session data.
    fsync_dir(batch)


def _levels_between(root: Path, leaf: Path) -> list[Path]:
    """Every directory from *root*'s first child down to *leaf*, shallowest first.

    ``mkdir(parents=True)`` creates intermediates, and a directory's own NAME lives in
    its PARENT's entries — so syncing only the leaf leaves the entries that reach it
    unrecorded. ``relative_to`` bounds the walk: a leaf outside *root* raises here
    rather than climbing past it.
    """
    levels: list[Path] = []
    level = root
    for part in leaf.relative_to(root).parts:
        level = level / part
        levels.append(level)
    return levels


def _fsync_tree(root: Path, leaf: Path) -> None:
    """Sync *root* and every level down to *leaf*, deepest first."""
    for level in reversed(_levels_between(root, leaf)):
        fsync_dir(level)
    fsync_dir(root)


def _sync_batch(
    target: Path,
    staged_dirs: set[Path],
    manifest: IO[str],
    source_dirs: set[Path] | None = None,
) -> None:
    """Force a staged batch out to disk: the manifest's bytes, then every name.

    Called at each point :func:`_move_to_trash_locked` can leave a batch on disk,
    because until this runs the batch is only durable by luck. The moves themselves
    are renames, whose metadata a journalling filesystem commits on its own
    schedule; the manifest is buffered text. So a power-off just after the move
    reported success can come back to a batch holding the sessions' only copies with
    an empty manifest — and a batch with no readable manifest is one
    :func:`list_trash` omits, which puts those files out of reach of restore AND of
    empty. That is the loss this whole layer exists to prevent, arrived at by
    reporting success too early.

    Order matters. The manifest's CONTENT first, because it is what makes the moves
    reversible; then the directories that hold the staged names, deepest first, so a
    child's entries are durable before the entry naming that child is.

    A failing sync RAISES rather than being logged away. The files have already moved,
    so raising costs the caller a reported failure for work that partly happened —
    but the alternative is reporting a durable batch that is not one, and the batch is
    the only copy. Each caller decides what to do with it; nothing is swallowed here.

    *source_dirs* — the live-store directories the batch drained — are the exception,
    on both counts. They are synced ONCE here rather than per file, because the
    same-filesystem path is a bare ``os.rename`` and a sync per staged file is exactly
    the cost that would stop a trash working at tens of gigabytes; and they are synced
    ``best_effort`` because a source entry that is not yet durable can only resurrect
    a name whose staged copy is intact — a duplicate the user thought they reclaimed,
    never a lost session. Raising for that after the batch is fully recorded would
    report a completed move as failed.

    This does not make a crash MID-move consistent — a batch interrupted halfway
    still holds files its manifest never named, which is what :func:`_unlisted_files`
    exists to protect. It makes a move that RETURNED durable.
    """
    manifest.flush()
    os.fsync(manifest.fileno())
    for staged in sorted(staged_dirs, key=lambda path: len(path.parts), reverse=True):
        fsync_dir(staged)
    fsync_dir(target)
    for source in sorted(source_dirs or (), key=lambda path: len(path.parts), reverse=True):
        fsync_dir(source, best_effort=True)


def _entry_bytes(entry: dict[str, Any]) -> int:
    files = entry.get("files")
    if not isinstance(files, list):
        return 0
    total = 0
    for record in files:
        if isinstance(record, dict):
            size = record.get("bytes")
            if isinstance(size, int):
                total += size
    return total


def _rels_digest(rels: list[str]) -> str:
    """A stable digest of the staged paths a manifest names.

    Order-sensitive on purpose: the delete walks the rels in order, and two listings that
    differ only in order are still two different listings. NUL-separated so no path can
    forge a boundary by containing the separator - the rels are plain batch-relative paths,
    which cannot contain NUL on any supported platform.

    Not a security hash of a secret, just a comparison that does not need to hold six
    figures of strings in the approval: sha256 because it is what the stdlib makes cheap and
    nobody has to reason about collisions.
    """
    digest = hashlib.sha256()
    for rel in rels:
        digest.update(rel.encode("utf-8", "surrogatepass"))
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_rels(batch: Path, *, dir_fd: int | None = None) -> list[str]:
    """Every staged file the batch's manifest names, as batch-relative paths.

    With *dir_fd* the manifest is read through that already-pinned directory rather than
    by path, which is what the approval needs: the listing it records has to be the one
    inside the directory whose identity it is recording, not whatever answers to the name
    a moment later.
    """
    parsed = _read_manifest(batch, dir_fd=dir_fd)
    if parsed is None:
        return []
    rels: list[str] = []
    for entry in parsed[1]:
        files = entry.get("files")
        if not isinstance(files, list):
            continue
        for record in files:
            if isinstance(record, dict) and isinstance(record.get("rel"), str):
                rels.append(record["rel"])
    return rels


def _header_names_this_batch(summary: tuple[dict[str, Any], int, int] | None, name: str) -> bool:
    """Whether a manifest summary's header claims the batch it was read from.

    `list_trash()` already refuses a batch whose header names a different id -- a header
    claiming another batch would make a targeted empty delete the batch it named rather than
    the one it came from. That check happens at LISTING time, and the approval reads the
    manifest again afterwards, so the same rule has to hold on the second read: a directory
    swapped into the selected name between the two brings its OWN manifest, whose header
    names ITS id, and everything else about that approval is self-consistent -- identity,
    files and digest all describe one directory. What they do not describe is the batch the
    user selected. The name is the only link back to that selection, and this is what checks
    it.

    The header is never RESOLVED in favour of, only compared: a disagreement withholds the
    batch, which is the same posture the listing takes.

    It demands EQUALITY rather than tolerating a missing id. `_write_header` always writes
    `batch_id` beside `schema`, and a summary is only returned at all when the header's schema
    is the current one -- so within the schema that reaches this check the id is always
    present, and its absence means the header was tampered with or truncated. Accepting that
    as "nothing to disagree with" is the fail-OPEN reading, and it is the one an actor gets to
    choose: strip the field and the check waves the swap through.
    """
    if summary is None:
        return False
    claimed = summary[0].get("batch_id")
    if claimed != name:
        # %r on both: manifest content is agent-controlled, same forgery risk as the listing.
        logger.warning(
            "refusing to approve %r: its manifest claims batch id %r",
            name,
            claimed,
        )
        return False
    return True
