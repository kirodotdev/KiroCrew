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

#: Projection readers decode one ``.jsonl`` line, then read a field off it. A
#: line that parses to a non-object (``[]``, ``"text"``, ``null``) would raise
#: ``AttributeError`` at ``.get`` and abandon every valid row after it, so each
#: decode is followed by a shape check, as in ``read_file_change_messages``.
_HISTORY_LOGGER = logging.getLogger("kiro_crew.history")

#: What :meth:`TranscriptReadProjection.metadata_line_state` answers about a
#: transcript's first line. ``get_metadata_status`` folds the last two into
#: ``readable=False``, which suits readers. Writers need the split: a
#: ``transient`` failure (open or UTF-8 decode failed after retries) clears on its
#: own, so the write is deferred, but a ``corrupt`` line never does, and a writer
#: deferring on it would never persist another row.
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

    Callers: a fork rebuild appending the slot's unflushed tail (decided against
    an earlier read, so a save in between puts rows in both lists), and
    ``read_messages_chained_full`` joining a rotated archive to its live file (a
    crash between writing the archive and rewriting the live head leaves rows in
    both). A duplicate row shifts every pagination cursor and fork index above it.

    Rows match by ``meta.mid`` when both carry one. Otherwise all of
    ``(ts, role, content)`` must match: a match deletes a row, and two real rows
    can share a second and a role. The longest overlap wins, because both
    producers persist a prefix, never an interior slice.
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
