"""Reading one segment's bytes: the bounded tail window, entry iteration, the header line.

``_scan_tail`` reads the file's end (``_TAIL_WINDOW``) and reports the newest seq, torn
bytes and emptiness as a ``_Tail``. ``_iter_entries`` and ``_iter_entries_located`` stream
every parseable entry, the latter with the byte offset each record begins at (bounded by
``_Bounded``); ``_read_header_line``, ``_read_first_entry_seq`` and ``read_head`` read the
top of a segment; and ``_anchor_exists`` proves a ``thread`` anchor parses. Nothing here
writes. A new read of a segment's raw records goes here.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES, Entry
from kiro_crew.jsonl_util import (
    UnreadableRecord,
    bounded_raw_records_with_offsets,
    strict_raw_records,
)

#: How much of the file's end a tail read covers. One maximum-size entry plus
#: slack, so the newest complete line is inside the window even when it is the
#: largest line the format allows.
_TAIL_WINDOW = 64 * 1024 + 8 * 1024


def read_head(path: Path) -> "tuple[dict[str, Any] | None, Entry | None, bool]":
    """Line 1 of *path* parsed as its header object, line 2 as its first entry,
    and whether a line 2 EXISTS at all.

    Header and entry are ``None`` when absent or when they cannot be delivered
    intact -- the abort posture of :func:`_read_header_line`, for the same
    reason: skipping a damaged line 1 would hand back an ENTRY as the header, a
    wrong answer rather than a missing one. The third value is what lets a
    caller tell two ``None`` entries apart: a header with NO line behind it is a
    normal transient (the emitter creates the file and appends
    ``session/opened`` in two writes, and a read can land between them), while
    a line that is there and does not parse is damage, and a caller that cached
    nothing for it would read it again forever.

    A line 2 with no terminator that does not parse is the transient, not the
    damage: the read landed inside the append, and the same bytes complete on
    the same inode a moment later. Reporting it as "no line behind the header
    yet" keeps a caller from caching a refusal it would otherwise hold for as
    long as the file exists. A terminated line that does not parse, and an
    unterminated one that does parse (the write landed short of its newline),
    are both final and reported as such.

    A file that cannot be opened or read raises the :class:`OSError` as is.
    Every ``None`` above is a verdict on bytes this function saw; a read that
    failed saw none, and reporting it as damage would let a caller cache a
    permanent refusal for a moment's I/O fault (or for a unit retention removed
    between the caller's ``stat`` and this open). The caller decides whether
    that is a retry next pass or a unit that is gone.
    """
    with open(path, "rb") as source:
        records = iter(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES))
        try:
            raw_header = next(records, b"").strip()
        except UnreadableRecord:
            return None, None, False
        header = _parses_to_object(raw_header) if raw_header else None
        if not header:
            return None, None, False
        try:
            raw_entry = next(records, None)
        except UnreadableRecord:
            return header, None, True
    if raw_entry is None:
        return header, None, False
    parsed = _parses_to_object(raw_entry.strip())
    if parsed is None and not raw_entry.endswith((b"\n", b"\r")):
        # Mid-append: nothing final to report yet.
        return header, None, False
    entry = None if parsed is None else Entry.from_dict(parsed)
    return header, entry, True


@dataclass(frozen=True)
class _Tail:
    """What a bounded read of the file's end says about its state.

    ``torn_offset`` and ``needs_newline`` are mutually exclusive: the trailing
    unterminated bytes either parse (the newline was lost, keep the record) or
    they do not (a crash artifact, drop it).

    ``window`` is the bytes that were read, already stripped of any torn tail,
    and ``at_start`` says whether they reach the beginning of the file. Both are
    carried so a caller that needs a SECOND answer about the tail -- does this
    seq exist -- can have it without a second read, and without this function
    parsing the whole window for a caller that does not ask.
    """

    last_seq: int
    torn_offset: int | None
    needs_newline: bool
    empty: bool
    window: bytes = b""
    at_start: bool = True


def _parses_to_object(blob: bytes) -> dict[str, Any] | None:
    """*blob* as a JSON object, or ``None``. Never raises."""
    try:
        parsed = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _seq_of(blob: bytes) -> int | None:
    """The ``seq`` of the entry *blob* encodes, or ``None`` when it has none."""
    parsed = _parses_to_object(blob)
    if parsed is None:
        return None
    entry = Entry.from_dict(parsed)
    return None if entry is None else entry.seq


def _scan_tail(path: Path) -> _Tail:
    """Read the end of *path* and report seq, torn bytes, and emptiness."""
    size = path.stat().st_size
    if size == 0:
        return _Tail(last_seq=0, torn_offset=None, needs_newline=False, empty=True)
    window = min(size, _TAIL_WINDOW)
    with open(path, "rb") as handle:
        handle.seek(size - window)
        blob = handle.read(window)
    at_start = window == size

    torn_offset: int | None = None
    needs_newline = False
    if not blob.endswith(b"\n"):
        cut = blob.rfind(b"\n")
        trailing = blob[cut + 1 :]
        if _parses_to_object(trailing) is not None:
            needs_newline = True
        else:
            torn_offset = size - len(trailing)
            blob = blob[: cut + 1]

    segments = blob.split(b"\n")
    if not at_start:
        # The window may begin mid-line; that first fragment is not a record.
        segments = segments[1:]
    last_seq = 0
    for segment in reversed(segments):
        stripped = segment.strip()
        if not stripped:
            continue
        seq = _seq_of(stripped)
        if seq is not None:
            last_seq = seq
            break
    if last_seq == 0 and not at_start:
        # Every line in the window was the header-less kind or damaged; only a
        # full scan can answer, and it is the rare path by construction.
        last_seq = _full_scan_last_seq(path)
    return _Tail(
        last_seq=last_seq,
        torn_offset=torn_offset,
        needs_newline=needs_newline,
        empty=False,
        window=blob,
        at_start=at_start,
    )


def _anchor_exists(path: Path, seq: int, tail: _Tail) -> bool:
    """Whether *seq* names a PARSEABLE entry in *path*.

    A thread pointing at a line no reader can parse is a pointer to nothing, so
    the range check ``1 <= seq <= last_seq`` is not enough on its own: a damaged
    interior line at exactly that seq is skipped by every reader, and the group
    would hang off an anchor that never appears.

    Bounded, in three steps, so proving this costs an ordinary append nothing:

    1. The window is already in memory, so an anchor inside it is free. A thread
       anchor is normally recent, which is exactly where that lands.
    2. If the window reached the start of the file, its answer is complete -- the
       whole file was examined.
    3. Only an anchor OLDER than the window falls back to a scan, and that scan
       stops AT the anchor instead of reading to the end.

    An append that passes no ``thread`` never calls this, so the bounded-tail
    cost of the ordinary write path is unchanged.
    """
    segments = tail.window.split(b"\n")
    if not tail.at_start:
        segments = segments[1:]
    for segment in segments:
        stripped = segment.strip()
        if stripped and _seq_of(stripped) == seq:
            return True
    if tail.at_start:
        return False
    for entry in _iter_entries(path):
        if entry.seq == seq:
            return True
        if entry.seq > seq:
            return False
    return False


def _full_scan_last_seq(path: Path) -> int:
    """The highest seq any parseable line carries. The fallback path only."""
    highest = 0
    for entry in _iter_entries(path):
        if entry.seq > highest:
            highest = entry.seq
    return highest


def _has_content(path: Path) -> bool:
    """Whether *path* holds any bytes. A zero-byte file reads as absent.

    ``create`` and ``open`` need the same answer: an empty file is not a crew log
    and never was one, so treating it as existing would strand a unit on a file
    that carries nothing to protect.
    """
    try:
        return path.stat().st_size > 0
    except FileNotFoundError:
        return False


class _Bounded:
    """A binary reader that stops at byte *end* of *source*.

    What :func:`_iter_entries` reads through when a caller fixed the end under the
    append lock: bytes past it may be an append whose fsync has not returned, and a
    line read from them can be truncated away a moment later. Only the two calls the
    record framer makes are offered, so a new one fails loudly rather than reading
    past the end.
    """

    def __init__(self, source: Any, end: int) -> None:
        self._source = source
        self._end = end

    def tell(self) -> int:
        return int(self._source.tell())

    def readline(self, size: int = -1) -> bytes:
        left = self._end - self.tell()
        if left <= 0:
            return b""
        return bytes(self._source.readline(left if size < 0 else min(size, left)))


def _iter_entries(path: Path, end: int | None = None) -> Iterator[Entry]:
    """Every parseable entry in *path*, oldest first, header excluded.

    A projection of :func:`_iter_entries_located`, which owns the framing, the skip
    posture and the strict per-record decode. One implementation rather than two, so
    a reader that wants byte offsets and one that does not cannot come to disagree
    about where a record begins or whether it counts.
    """
    for entry, _at in _iter_entries_located(path, end):
        yield entry


def _iter_entries_located(
    path: Path, end: int | None = None, *, start: int = 0
) -> "Iterator[tuple[Entry, int]]":
    """Every parseable entry in *path* and the byte offset its record begins at.

    *end*, when given, is the byte offset the read stops at (see :class:`_Bounded`).

    *start* is where the read BEGINS, and it is how a reader resumes at a record it
    has already located instead of walking to it. Zero reads the file from the top,
    where record 1 is the header and is skipped; any other value is already past the
    header, so nothing is skipped there -- a resumed read that dropped its first
    record would silently lose the entry the caller seeked to.

    The offset is the record's OWN start, taken from the framing layer rather than
    accumulated here, so an over-cap record -- which the framer discards in full,
    terminator included -- cannot leave every later offset short by its length.

    A malformed interior line is SKIPPED, not raised on: the log is append-only
    and one damaged line must not hide the history in front of it. The file is
    streamed line by line, so a large crew log costs one line of memory, not its
    size.

    Decoding is STRICT and per line. Replacement-decoding would be the wrong
    kind of tolerance here: invalid bytes inside a JSON string can decode into
    still-valid JSON, so a damaged line would be yielded with silently altered
    values instead of skipped -- handing a consumer corrupted data as authority,
    which is precisely what a record that calls itself the authority must never
    do. Byte damage is this machinery's expected adversary, so an undecodable
    line is damage and is skipped exactly like unparseable JSON.

    The file is read in BINARY mode and framed by
    :func:`jsonl_util.bounded_raw_records_with_offsets`, so no universal-newline
    translation can rewrite the bytes on the way in, and one planted line cannot
    cost more than :data:`MAX_ENTRY_BYTES` of memory however long it is. That cap is
    the format's own write limit, so a longer line is not something this writer
    produced: it is damage, and the skip posture already applies to damage.
    """
    try:
        with open(path, "rb") as raw_source:
            if start:
                raw_source.seek(start)
            source: Any = raw_source if end is None else _Bounded(raw_source, end)
            for index, (at, _stop, raw) in enumerate(
                bounded_raw_records_with_offsets(
                    source, path, cap=MAX_ENTRY_BYTES, label="crew log"
                )
            ):
                if index == 0 and not start:
                    continue  # the header
                if raw is None:
                    continue  # an over-cap record, reported so a resumer can step past it
                stripped = raw.strip()
                if not stripped:
                    continue
                parsed = _parses_to_object(stripped)
                if parsed is None:
                    continue
                entry = Entry.from_dict(parsed)
                if entry is not None:
                    yield entry, at
    except FileNotFoundError:
        return


def _read_header_line(path: Path) -> bytes | None:
    """Line 1 of *path* as raw bytes, or ``None`` when there is no usable one.

    Bytes, not text: the caller decodes strictly and reports a header it cannot
    decode as ``bad_header`` rather than accepting a replacement-decoded one.

    Framed with the ABORT posture (:func:`jsonl_util.strict_raw_records`) where
    the entry read below uses the skip posture, and the difference is the point.
    Skipping an over-cap line 1 would hand back line 2 -- an ENTRY -- as though it
    were the header, which is a wrong answer rather than a missing one. Raising
    instead becomes ``None`` here, and ``None`` is what the caller reports as a
    bad header, so an unreadable line 1 fails closed on the identity of the unit.
    """
    try:
        with open(path, "rb") as source:
            for raw in strict_raw_records(source, path, cap=MAX_ENTRY_BYTES):
                return raw.strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    return None


def _read_first_entry_seq(path: Path) -> int | None:
    """Seq on the physical first entry line, or ``None`` when unreadable.

    Filename provenance applies only when that line carries a usable entry. A
    damaged first record has no sequence claim to compare and remains subject to
    the same per-record skip posture as any later damaged record.
    """
    try:
        with open(path, "rb") as source:
            records = iter(strict_raw_records(source, path, cap=MAX_ENTRY_BYTES))
            next(records, None)  # header
            raw = next(records, b"").strip()
    except FileNotFoundError:
        return None
    except UnreadableRecord:
        return None
    parsed = _parses_to_object(raw)
    entry = None if parsed is None else Entry.from_dict(parsed)
    return None if entry is None else entry.seq
