"""Streaming redaction: ``feed(chunk) -> safe output``, ``flush() -> the rest``.

Each feed runs the whole-text detectors over ``context + pending``, where context is
the last ``CONTEXT`` characters already committed (raw, for look-behind only). Output
is committed up to the commit point, the earliest of:

- the start of any unit a detector reports undecided (a token run, header or block,
  known-key value or Identifier still running at the end of the window, or a pairing
  candidate whose 300-character window is not complete yet);
- ``len - TAIL``, so a proper prefix of any anchor literal is never committed;
- the start of any resolved hit crossing that point.

Every hold is bounded by its unit's maximum length, so pending text is bounded. A
unit that is decided while still running (an unterminated private-key block past
``max_block``; a session-token run, or an encoded run with a text head, past 4 KiB) is
replaced and the stream discards input to that unit's own end: the end of the text, or
the end of the run.
The private-key scan of each window starts where the last committed block's END
line ended, as whole text resumes it, so a BEGIN inside a block whose own BEGIN
has left the look-behind is not read as a new header.
Chunked output and spans therefore equal the whole-text result at every split, with one
exception that only masks more. A header, cookie or command-line value still running
past 4 KiB (``headers._HOLD_MAX``) is replaced from its start and the stream discards
the rest of its line, even text the whole text keeps: a stream past the bound does
not keep the line's start, which the readers of that text read it from. When the
discarded line names a private-key block (a BEGIN line), whose body can run past the
line's end, the stream discards the rest of the text (:func:`line_tail`).
"""

from __future__ import annotations

from kiro_crew.default_ignorable import DEFAULT_IGNORABLE

from .charsets import B64STD
from .encoded import RUN_CHARS
from .engine import render, resolve, scan
from .model import SECRET, TAG, TAIL_ALL, TAIL_B64RUN, TAIL_ENCRUN, TAIL_LINE, Hit, Span
from .table import TABLE

# Look-behind kept from committed text: covers the pairing window (300 + 40 + 21),
# line leads (64), key-token expansion (64) and URL schemes (32) with margin.
CONTEXT = 1024
# Longer than every anchor literal (``github_pat_``, key spellings, ``-----BEGIN ``,
# URL scheme plus ``://``), so a partial anchor is always held.
TAIL = 47
#: The characters each run tail drops.
_RUN_TAILS = {TAIL_B64RUN: B64STD, TAIL_ENCRUN: RUN_CHARS}
#: What :func:`line_tail` reads a dropped line for: the private-key BEGIN markers, the
#: longest one's length less one, and the characters it reads them through.
_BEGINS = tuple({row.begin for row in TABLE.armour})
_BEGIN_KEEP = max(map(len, _BEGINS)) - 1
_IGNORABLE = dict.fromkeys(DEFAULT_IGNORABLE)


def discard(tail: str, pad: int, chunk: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a replaced unit's continuation in ``chunk``.

    ``tail`` is :data:`~.model.TAIL_ALL` (the unit runs to the end of the text: drop
    everything), :data:`~.model.TAIL_LINE` (drop up to the line's CR or LF, which
    stays) or a run tail (drop the rest of the run and at most two ``=`` after it).
    ``pad`` counts the ``=`` already dropped.
    """
    if tail == TAIL_ALL:
        return len(chunk), pad, False
    if tail == TAIL_LINE:
        i = next((k for k, ch in enumerate(chunk) if ch in "\r\n"), len(chunk))
        return i, pad, i < len(chunk)
    i = 0
    if pad == 0:
        i = len(chunk) - len(chunk.lstrip(_RUN_TAILS[tail]))
    while i < len(chunk) and chunk[i] == "=" and pad < 2:
        pad += 1
        i += 1
    return i, pad, not (i == len(chunk) and pad < 2)


def span_tail(hits: list[Hit], tail_hit: Hit, text: str) -> tuple[str, str]:
    """``(tail, seen)``: how far the drop of *tail_hit* runs, the Secret span a stream
    is about to replace in *text*: the furthest any unit that ends the span runs. A
    unit that runs to the end of the text (an unterminated private-key block)
    outranks a held value's line, and that line outranks a run, which cannot pass a
    line's end. *seen* is :func:`line_tail`'s, for a line drop."""
    tails = {
        h.tail
        for h in hits
        if h.tier == SECRET and h.tail and h.end == tail_hit.end and h.start >= tail_hit.start
    }
    if TAIL_ALL in tails:
        return TAIL_ALL, ""
    if TAIL_LINE in tails:
        return line_tail("", text[tail_hit.start :])
    return tail_hit.tail, ""


def line_tail(seen: str, dropped: str) -> tuple[str, str]:
    """``(tail, seen)`` for a line drop that drops *dropped* next:
    :data:`~.model.TAIL_LINE`, or :data:`~.model.TAIL_ALL` once the dropped text names
    a private-key block. That block's body can run past the line's end, and its BEGIN
    line, which the whole text masks it by, is dropped: the rest of the text goes too.

    The marker is read as the credential plan reads it, with default-ignorable
    characters (``U+200B``) removed. *seen* is the end of the text dropped so far,
    read that way and just long enough to hold a marker's start, so a marker split
    across chunks, however many such characters lie inside it, is still found."""
    seen += dropped.translate(_IGNORABLE)
    tail = TAIL_ALL if any(begin in seen for begin in _BEGINS) else TAIL_LINE
    return tail, seen[-_BEGIN_KEEP:]


class StreamRedactor:
    def __init__(self) -> None:
        self._ctx = ""
        self._pending = ""
        self._pos = 0  # input offset of the start of ``_pending``
        self._spans: list[Span] = []
        self._mode = ""
        self._pad = 0
        self._open_span: tuple[int, str, str] | None = None
        self._closed = False
        # Input offset where the last committed private-key block's END line ends.
        self._armour_from = 0
        # The end of the line a line drop has dropped so far (:func:`line_tail`).
        self._seen = ""

    @property
    def spans(self) -> list[Span]:
        """Spans committed so far, over input offsets, in text order."""
        return list(self._spans)

    def feed(self, chunk: str) -> str:
        if self._closed:
            raise ValueError("feed after flush")
        if self._mode:
            chunk = self._discard(chunk)
            if self._mode:
                return ""
        if not chunk:
            return ""
        self._pending += chunk
        return self._commit(final=False)

    def flush(self) -> str:
        if self._closed:
            return ""
        self._closed = True
        if self._mode:
            self._close_open_span(self._pos)
            self._mode = ""
            return ""
        return self._commit(final=True)

    # ── internals ──

    def _discard(self, chunk: str) -> str:
        """Drop input belonging to a unit already replaced; return what follows it."""
        i, self._pad, done = discard(self._mode, self._pad, chunk)
        if self._mode == TAIL_LINE:
            self._mode, self._seen = line_tail(self._seen, chunk[:i])
            if self._mode == TAIL_ALL:
                i, done = len(chunk), False
        self._pos += i
        if self._mode != TAIL_ALL:
            self._ctx = (self._ctx + chunk[:i])[-CONTEXT:]
        if not done:
            return ""
        self._close_open_span(self._pos)
        self._mode = ""
        self._pad = 0
        return chunk[i:]

    def _close_open_span(self, end: int) -> None:
        if self._open_span is not None:
            start, rule_id, tier = self._open_span
            self._spans.append((start, end, rule_id, tier))
            self._open_span = None

    def _record(self, resolved: list[Hit], lo: int, hi: int, offset: int) -> None:
        for h in resolved:
            if h.start >= lo and h.end <= hi:
                self._spans.append((h.start + offset, h.end + offset, h.rule_id, h.tier))

    def _note_blocks(self, blocks: tuple[tuple[int, int], ...], cut: int, offset: int) -> None:
        """Keep where whole text resumes its private-key scan after a committed BEGIN."""
        for begin, end in blocks:
            if begin < cut:
                self._armour_from = max(self._armour_from, end + offset)

    def _commit(self, final: bool) -> str:
        window = self._ctx + self._pending
        base = len(self._ctx)
        offset = self._pos - base
        result = scan(
            window, final=final, origin=base, armour_from=max(0, self._armour_from - offset)
        )
        resolved = resolve([h for h in result.hits if h.start >= base])
        n = len(window)
        if final:
            cut = n
        else:
            tail_hit = next(
                (h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None
            )
            if tail_hit is not None and (
                result.open_from is None or result.open_from >= tail_hit.start
            ):
                self._note_blocks(result.armour_blocks, tail_hit.start, offset)
                tail, self._seen = span_tail(result.hits, tail_hit, window)
                return self._start_discard(window, base, offset, resolved, tail_hit, tail)
            cut = n - TAIL
            if result.open_from is not None:
                cut = min(cut, result.open_from)
            cut = max(cut, base)
            moved = True
            while moved:
                moved = False
                for h in resolved:
                    # A hit not yet committed keeps the text it was decided from
                    # (its anchor) in the window, so a later feed decides it alike.
                    first = h.start if h.anchor < 0 else min(h.start, h.anchor)
                    if first < cut < h.end:
                        cut = max(base, first)
                        moved = True
        out = render(window, resolved, base, cut)
        self._record(resolved, base, cut, offset)
        self._note_blocks(result.armour_blocks, cut, offset)
        self._ctx = window[:cut][-CONTEXT:]
        self._pending = window[cut:]
        self._pos += cut - base
        return out

    def _start_discard(
        self, window: str, base: int, offset: int, resolved: list[Hit], tail_hit: Hit, tail: str
    ) -> str:
        out = render(window, resolved, base, tail_hit.start) + TAG
        self._record(resolved, base, tail_hit.start, offset)
        self._open_span = (tail_hit.start + offset, tail_hit.rule_id, tail_hit.tier)
        self._mode = tail
        self._pad = 0
        if self._mode != TAIL_ALL:
            # The window ends inside the run: no padding has been read yet.
            self._ctx = window[-CONTEXT:]
        self._pos += len(window) - base
        self._pending = ""
        return out
