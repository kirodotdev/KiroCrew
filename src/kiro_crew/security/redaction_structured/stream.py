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
the end of the run. A ``Cookie:`` line still running 8 KiB after its name is replaced
from its value to its CR or LF.
Chunked output and spans therefore equal the whole-text result at every split, except
that over-long Cookie line, which a stream masks (fail closed) where the whole text
may leave it unchanged.
"""

from __future__ import annotations

from .armour import end_line_at
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


#: Detectors whose units can run past a line end further than the look-behind reaches
#: (``engine.ScanResult.cross_line_opens``); a line drop is carried through them.
CROSS_LINE_DETECTORS = frozenset({"armour"})


class LineDrop:
    """A :data:`~.model.TAIL_LINE` drop, shared by both streams.

    The drop runs to the later of the line's CR or LF and the end of every unit that
    can run past it (:data:`CROSS_LINE_DETECTORS`) whose anchor sits on the dropped
    line. Dropped text is still read, never emitted: a unit's anchor that arrives
    during the drop extends it. The last :data:`TAIL` characters of a buffer the drop
    has not finished stay unread until more arrives, so an anchor split across chunks
    is read whole.
    """

    __slots__ = ("_ends", "_line_end", "_pos", "_reach", "_tail_all")

    def __init__(self) -> None:
        self._pos = 0  # characters dropped so far
        self._line_end: int | None = None  # where the dropped line ended (offset)
        self._reach = 0  # the furthest end of a completed unit (offset)
        # An open block's anchor -> ``(END line, lo, hi)`` from ``armour.end_search``
        # (offsets), read once its BEGIN header is whole, so the drop still finds the
        # END after the BEGIN has left the look-behind. Only open blocks are kept.
        self._ends: dict[int, tuple[str, int, int]] = {}
        self._tail_all = False

    @property
    def tail_all(self) -> bool:
        """A unit on the line was decided to run to the end of the text."""
        return self._tail_all

    def step(self, ctx: str, buf: str) -> tuple[int, bool]:
        """``(dropped, done)`` for *buf*, read after the look-behind *ctx*."""
        if not buf:
            return 0, False
        look = ctx[-CONTEXT:]
        base = len(look)
        n = len(buf)
        result = scan(look + buf, final=False, origin=base)
        line_end = self._line_end
        if line_end is None:
            found = next((k for k, ch in enumerate(buf) if ch in "\r\n"), -1)
            if found != -1:
                line_end = self._pos + found
        # A unit counts when its anchor is on the dropped line (before its end).
        limit = line_end if line_end is not None else self._pos + n
        # A BEGIN whose header is not whole yet holds the drop for this step only; each
        # step's own read decides whether it is still a candidate.
        pending = False
        for h in result.hits:
            if h.detector not in CROSS_LINE_DETECTORS or h.end <= base:
                continue
            anchor = self._pos + (min(h.start, h.anchor) if h.anchor >= 0 else h.start) - base
            if anchor < limit:
                if h.tail == TAIL_ALL:
                    self._tail_all = True
                    return n, False
                self._reach = max(self._reach, self._pos + h.end - base)
                self._ends.pop(anchor, None)
        for o in result.cross_line_opens:
            anchor = self._pos + o - base
            if anchor < limit and anchor not in self._ends:
                found_end = end_line_at(look + buf, o, TABLE.armour) if o >= 0 else None
                if found_end is None:
                    pending = True
                else:
                    end_line, lo, hi = found_end
                    self._ends[anchor] = (end_line, anchor + lo, anchor + hi)
        for anchor, (end_line, lo, bound) in list(self._ends.items()):
            # The END is searched where whole-text armour searches it (after the
            # header, inside the bound); the drop stops at that line, which streams.
            start = max(0, lo - self._pos)
            at = buf.find(end_line, start, max(start, bound - self._pos))
            if at != -1:
                self._reach = max(self._reach, self._pos + at)
                del self._ends[anchor]
            elif self._pos + n >= bound:
                # No END inside the bound: the block runs to the end of the text, as
                # whole-text armour reads an unterminated block (fail closed).
                self._tail_all = True
                return n, False
        must = max(self._reach, line_end if line_end is not None else self._pos + n)
        if line_end is not None and not self._ends and not pending and must < self._pos + n:
            dropped = must - self._pos
            self._pos = must
            self._line_end = line_end
            return dropped, True
        dropped = max(0, n - TAIL)
        self._pos += dropped
        self._line_end = line_end
        return dropped, False


def line_drop_ok(tail_hit: Hit, beyond_line_open: int | None) -> bool:
    """Whether a decided unit's drop may start now. A drop that stops at the line's
    CR or LF (:data:`~.model.TAIL_LINE`) waits while a private-key block, the one
    unit that can run past that line end, is still open inside it, so the text the
    block decides is held rather than released after the line. The key reader's
    length bound bounds the wait. A pairing open never holds it: dropped text is
    never emitted, and a pairing's second half past the line end is scanned as
    usual."""
    if tail_hit.tail != TAIL_LINE or beyond_line_open is None:
        return True
    return beyond_line_open < tail_hit.start


def discard(tail: str, pad: int, chunk: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a replaced unit's continuation in ``chunk``.

    ``tail`` is :data:`~.model.TAIL_ALL` (the unit runs to the end of the text: drop
    everything), :data:`~.model.TAIL_LINE` (drop up to the line's CR or LF) or a run
    tail (drop the rest of the run and at most two ``=`` after it).
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


class StreamRedactor:
    def __init__(self) -> None:
        self._ctx = ""
        self._pending = ""
        self._pos = 0  # input offset of the start of ``_pending``
        self._spans: list[Span] = []
        self._mode = ""
        self._pad = 0
        self._open_span: tuple[int, str, str] | None = None
        self._line_drop: LineDrop | None = None
        self._carry = ""
        self._closed = False

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
        if self._mode == TAIL_LINE:
            return self._line_discard(chunk)
        i, self._pad, done = discard(self._mode, self._pad, chunk)
        self._pos += i
        if self._mode != TAIL_ALL:
            self._ctx = (self._ctx + chunk[:i])[-CONTEXT:]
        if not done:
            return ""
        self._close_open_span(self._pos)
        self._mode = ""
        self._pad = 0
        return chunk[i:]

    def _line_discard(self, chunk: str) -> str:
        if self._line_drop is None:
            self._line_drop = LineDrop()
        buf = self._carry + chunk
        dropped, done = self._line_drop.step(self._ctx, buf)
        self._pos += dropped
        self._ctx = (self._ctx + buf[:dropped])[-CONTEXT:]
        if self._line_drop.tail_all:
            # A unit on the line runs to the end of the text: drop the rest of it.
            self._mode = TAIL_ALL
            self._pos += len(buf) - dropped
            self._carry = ""
            self._line_drop = None
            return ""
        if not done:
            self._carry = buf[dropped:]
            return ""
        self._close_open_span(self._pos)
        self._mode = ""
        self._carry = ""
        self._line_drop = None
        return buf[dropped:]

    def _close_open_span(self, end: int) -> None:
        if self._open_span is not None:
            start, rule_id, tier = self._open_span
            self._spans.append((start, end, rule_id, tier))
            self._open_span = None

    def _record(self, resolved: list[Hit], lo: int, hi: int, offset: int) -> None:
        for h in resolved:
            if h.start >= lo and h.end <= hi:
                self._spans.append((h.start + offset, h.end + offset, h.rule_id, h.tier))

    def _commit(self, final: bool) -> str:
        window = self._ctx + self._pending
        base = len(self._ctx)
        offset = self._pos - base
        result = scan(window, final=final, origin=base)
        resolved = resolve([h for h in result.hits if h.start >= base])
        n = len(window)
        if final:
            cut = n
        else:
            tail_hit = next(
                (h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None
            )
            if (
                tail_hit is not None
                and (result.open_from is None or result.open_from >= tail_hit.start)
                and line_drop_ok(tail_hit, result.beyond_line_open)
            ):
                return self._start_discard(window, base, offset, resolved, tail_hit)
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
                    # Retreat only while the cut moves back; a hit decided from
                    # committed text (anchor below base) holds the window whole.
                    if first < cut < h.end and max(base, first) < cut:
                        cut = max(base, first)
                        moved = True
        out = render(window, resolved, base, cut)
        self._record(resolved, base, cut, offset)
        self._ctx = window[:cut][-CONTEXT:]
        self._pending = window[cut:]
        self._pos += cut - base
        return out

    def _start_discard(
        self, window: str, base: int, offset: int, resolved: list[Hit], tail_hit: Hit
    ) -> str:
        out = render(window, resolved, base, tail_hit.start) + TAG
        self._record(resolved, base, tail_hit.start, offset)
        self._open_span = (tail_hit.start + offset, tail_hit.rule_id, tail_hit.tier)
        self._mode = tail_hit.tail
        self._pad = 0
        if self._mode != TAIL_ALL:
            # The window ends inside the run: no padding has been read yet.
            self._ctx = window[-CONTEXT:]
        self._pos += len(window) - base
        self._pending = ""
        return out
