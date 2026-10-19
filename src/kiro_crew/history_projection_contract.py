"""The vocabulary the transcript projections share: line states, page types, read rules.

The metadata-line states, the display-only roles, the chained page and revision types,
the bounded readers' tuning and their chunked line splitter, the persisted-prefix rule,
the ``kiro_crew.history`` logger the projections write to, and their call-time accessor
of the ``kiro_crew.history`` facade. ``kiro_crew.history_projection`` is the import path
and the patch surface; this owner imports no other member of that family.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import IO, Any

from kiro_crew.history_cache import FileStamp

#: Every projection reader decodes one ``.jsonl`` line at a time and then reads a
#: field off it. ``json.JSONDecodeError`` covers the line that will not parse;
#: it does NOT cover a line that parses to something other than an object
#: (``[]``, ``"text"``, ``12``, ``null``), which reaches ``.get`` and raises
#: ``AttributeError`` -- abandoning the read, and every valid row after the bad
#: one, on an error none of these callers expect. So a decode is followed by a
#: shape check, exactly as ``read_file_change_messages`` already does for the
#: same rows of the same files.
_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")

#: What :meth:`TranscriptReadProjection.metadata_line_state` answers about a
#: transcript's first line. ``get_metadata_status`` folds the last two into one
#: ``readable=False``, which is the right answer for every READER (a line whose
#: contract cannot be seen is refused either way) but not for a WRITER: a
#: ``transient`` failure (the file could not be opened or decoded as UTF-8 after
#: the bounded retries) clears on its own and the write is deferred, while a
#: ``corrupt`` line (bytes on disk that are not JSON) never will, so a writer
#: that keeps deferring on it never persists another row. The two-tuple API is
#: kept for readers; writers ask this companion only once the tuple says
#: unreadable.
METADATA_LINE_READABLE = "readable"
METADATA_LINE_TRANSIENT = "transient"
METADATA_LINE_CORRUPT = "corrupt"

#: Rows drawn for the person reading a transcript that are not conversation. No
#: model-bound reader carries one: :meth:`TranscriptReadProjection.recent_with_provenance`
#: skips them, as does memory consolidation and auto-skill detection
#: (``history_consolidation``), and every other model-bound reader already filters
#: to conversation roles. The Slack thread-parent row, in particular, is untrusted
#: text whose only route to the model is a fenced, injection-screened block. A
#: ``card`` row is an agent's change card or guide offer drawn at the point it was
#: proposed: the card store owns its state, and the agent hears an outcome only
#: through its own reporting path, never by reading this row back.
DISPLAY_ONLY_ROLES = frozenset({"notice", "card"})


def _history_facade() -> Any:
    """Return the facade lazily, after its component imports have completed."""
    from kiro_crew import history

    return history


def _facade_flock_acquire_timeout() -> float:
    """Read the one timeout with an established facade rebind seam."""
    return float(_history_facade()._FLOCK_ACQUIRE_TIMEOUT_S)


_TRANSCRIPT_PAGE_INDEX_STRIDE = 128
#: Ceiling on retained ``(row, offset)`` checkpoints per index entry. Past it the
#: stride doubles, so a transcript of any length costs at most this many pairs
#: (about 100 KiB) and a warm page decodes at most one stride of extra rows,
#: where the stride is the smallest power-of-two multiple of the base that fits.
_TRANSCRIPT_PAGE_INDEX_MAX_CHECKPOINTS = 1024
_TRANSCRIPT_PAGE_READ_ATTEMPTS = 2
#: Bytes before a saved scan boundary that must still be identical for an
#: append-only file to resume its row index from there. Arbitrary: large enough
#: to span several rows, small enough to re-read on every request.
_TRANSCRIPT_INDEX_FINGERPRINT_BYTES = 4096

#: Characters per text read in ``_read_messages_locked``. One whole-file
#: ``read()`` or ``splitlines()`` is a single C call that holds the GIL for the
#: whole file (0.45 s and 0.67 s on a 400 MB transcript), so even a reader in a
#: worker thread froze the event loop that long. A megabyte per call keeps each
#: hold to about a millisecond.
_TRANSCRIPT_READ_CHUNK = 1 << 20


def _iter_splitlines(handle: IO[str]) -> Iterator[str]:
    """Yield ``handle.read().splitlines(keepends=True)``, one bounded read at a time.

    Exactly the same pieces: a piece is cut only at a boundary character, so the
    last piece of a read is carried into the next one unless it ends on a
    boundary. The text read has already turned ``\\r\\n`` into one ``\\n`` and
    holds back a trailing ``\\r`` until it sees the next character, so no
    two-character boundary can straddle two reads.
    """
    carry: list[str] = []
    while chunk := handle.read(_TRANSCRIPT_READ_CHUNK):
        pieces = chunk.splitlines(keepends=True)
        last = pieces.pop()
        for piece in pieces:
            if carry:
                carry.append(piece)
                piece = "".join(carry)
                carry.clear()
            yield piece
        carry.append(last)
        if last.splitlines()[0] != last:  # it ends on a boundary, so it is whole
            yield "".join(carry)
            carry.clear()
    if carry:
        yield "".join(carry)


#: ``(key, file stamp or None when the file does not exist, generation)`` per chain member.
ChainRevision = tuple[tuple[str, FileStamp | None, int], ...]


class TranscriptRevisionChanged(RuntimeError):
    """A chained transcript changed between reads composing one response."""


@dataclass(frozen=True)
class TranscriptPage:
    """One exact page in chained durable-message index space."""

    messages: list[dict]
    total: int
    next_before: int
    has_more: bool
    revision: ChainRevision


def drop_persisted_tail_prefix(
    full_disk: list[dict], tail: list[dict], *, require_mid: bool = False
) -> list[dict]:
    """`tail` minus whatever of its head `full_disk` already ends with.

    Two callers, one shape: a corpus read from disk, followed by rows that MAY
    already be in it.

    A fork rebuild appends the slot's unflushed tail onto a freshly read disk
    corpus. "Unflushed" was decided against an EARLIER read, so a save landing
    between the two reads leaves those rows in both lists.

    ``read_messages_chained_full`` concatenates a key's rotated archive with its
    live file. Rotation writes the archive first and rewrites the live file's head
    second, so a crash between those two steps leaves the rotated rows in both
    files permanently.

    In both cases appending blind duplicates rows, and a duplicated row is not
    cosmetic here: this is the index space pagination cursors and fork indices
    resolve against, so one extra row shifts every index above it.

    Rows are matched by their stable id (``meta.mid``) when BOTH carry one. That
    id is what ``history`` assigns per row, so it is the only identity that
    cannot collide. Without it the fallback requires ``(ts, role, content)`` to
    all match: ``(ts, role)`` alone is a spot-check elsewhere, but here a match
    DELETES a row, and two distinct rows can share a second and a role (the same
    speaker twice inside one second, or two processes stamping the same ts), so
    dropping on that alone would remove a genuinely unpersisted message. The
    longest overlap wins, so a partially-written duplication is handled as well as
    a whole one: both producers persist a PREFIX, never an interior slice.
    """
    if not full_disk or not tail:
        return tail

    def _same(a: dict, b: dict) -> bool:
        a_meta, b_meta = a.get("meta"), b.get("meta")
        a_mid = a_meta.get("mid") if isinstance(a_meta, dict) else None
        b_mid = b_meta.get("mid") if isinstance(b_meta, dict) else None
        if a_mid and b_mid:
            return bool(a_mid == b_mid)
        if require_mid:
            # A match here DELETES a row. Callers whose overlap can only come
            # from a re-archived prefix (whose rows always carry mids) opt in
            # to mid-proven matching: an id-less coincidence on the fallback
            # triple must be preserved, because deleting a genuine row is
            # strictly worse than serving a duplicate.
            return False
        return (
            a.get("ts", "") == b.get("ts", "")
            and a.get("role") == b.get("role")
            and a.get("content", "") == b.get("content", "")
        )

    for k in range(min(len(tail), len(full_disk)), 0, -1):
        base = len(full_disk) - k
        if all(_same(tail[i], full_disk[base + i]) for i in range(k)):
            return tail[k:]
    return tail
