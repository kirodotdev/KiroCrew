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
the end of the run. A Cookie line past its hold bound (``TAIL_LINE``) is replaced by one
tag and its output hidden to the next line break, while its text is still read, so the
reply after it streams on.
Chunked output and spans therefore equal the whole-text result at every split.
"""

from __future__ import annotations

from .charsets import B64STD
from .encoded import RUN_CHARS
from .engine import render, resolve, scan
from .headers import _COOKIE_LINE_HOLD
from .model import SECRET, TAG, TAIL_ALL, TAIL_B64RUN, TAIL_ENCRUN, TAIL_LINE, Hit, Span

# Look-behind kept from committed text: covers the pairing window (300 + 40 + 21),
# line leads (64), key-token expansion (64) and URL schemes (32) with margin.
CONTEXT = 1024
# Longer than every anchor literal (``github_pat_``, key spellings, ``-----BEGIN ``,
# URL scheme plus ``://``), so a partial anchor is always held.
TAIL = 47
#: The characters each run tail drops.
_RUN_TAILS = {TAIL_B64RUN: B64STD, TAIL_ENCRUN: RUN_CHARS}


def discard(tail: str, pad: int, chunk: str) -> tuple[int, int, bool]:
    """``(dropped, pad, done)`` for a replaced unit's continuation in ``chunk``.

    ``tail`` is :data:`~.model.TAIL_ALL` (the unit runs to the end of the text: drop
    everything) or a run tail (drop the rest of the run and at most two ``=`` after it).
    ``pad`` counts the ``=`` already dropped.
    """
    if tail == TAIL_ALL:
        return len(chunk), pad, False
    i = 0
    if pad == 0:
        i = len(chunk) - len(chunk.lstrip(_RUN_TAILS[tail]))
    while i < len(chunk) and chunk[i] == "=" and pad < 2:
        pad += 1
        i += 1
    return i, pad, not (i == len(chunk) and pad < 2)


def line_break(text: str) -> int:
    """The first ``\\r`` or ``\\n`` in *text*, or -1."""
    ends = [k for k in (text.find("\r"), text.find("\n")) if k != -1]
    return min(ends) if ends else -1


def quiet(pending: str, chunk: str) -> bool:
    """Whether a feed may skip the read while a Cookie line is held: *chunk* only
    extends the line and the hold stays within its bound. Skipping only joins *chunk* to
    the next feed, which the stream reads as one; a line break or the bound reads again."""
    return (
        line_break(chunk) == -1
        and line_break(pending) == -1
        and len(pending) + len(chunk) <= _COOKIE_LINE_HOLD
    )


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
        # Output hidden to the next line break (a Cookie line past its bound), and the
        # input offset of a held Cookie name a feed may skip reading for.
        self._line = False
        self._line_span: tuple[int, str, str] | None = None
        self._quiet_at: int | None = None

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
        # shortcut: a held Cookie line rescans the whole window on each feed; a chunk that
        # only extends it is joined to the next feed instead.
        # The whole pending text is measured, so a skip never lets it pass the bound.
        if (self._line or self._quiet_at is not None) and quiet(self._pending, chunk):
            self._pending += chunk
            return ""
        self._pending += chunk
        return self._commit(final=False)

    def flush(self) -> str:
        if self._closed:
            return ""
        self._closed = True
        if self._mode:
            self._close_open_span(self._pos)
            self._end_line(self._pos)
            self._mode = ""
            return ""
        out = self._commit(final=True)
        self._end_line(self._pos)
        return out

    # ── internals ──

    def _discard(self, chunk: str) -> str:
        """Drop input belonging to a unit already replaced; return what follows it."""
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

    def _end_line(self, end: int) -> None:
        if self._line_span is not None:
            start, rule_id, tier = self._line_span
            self._spans.append((start, end, rule_id, tier))
        self._line = False
        self._line_span = None

    def _shown(self, out: str, raw: str, at: int) -> str:
        """*out* (rendered from *raw*, which starts at input offset *at*) with a hidden
        line's rest left out, to its line break. A tag holds no line break, so the first
        break in *out* comes from one in *raw* at or after the line's own."""
        if not self._line:
            return out
        k = line_break(out)
        if k == -1:
            return ""
        self._end_line(at + line_break(raw))
        return out[k:]

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
        n = len(window)
        kept = [h for h in result.hits if h.start >= base]
        # A Cookie line past its bound is a signal, not a span: the line is hidden.
        line = next((h for h in kept if h.tail == TAIL_LINE and h.end == n), None)
        if line is not None:
            kept = [h for h in kept if h.tail != TAIL_LINE]
            # The line already hidden is not reported again; a later one is.
            if self._line and line_break(window[base : line.start]) == -1:
                line = None
        resolved = resolve(kept)
        if final:
            cut = n
        else:
            tail_hit = next(
                (h for h in resolved if h.tier == SECRET and h.tail and h.end == n), None
            )
            # A run decided inside a Cookie line to hide is hidden with the line, so the
            # line's name is never dropped ahead of a later pair on it.
            if line is not None and tail_hit is not None and tail_hit.start >= line.start:
                tail_hit = None
            if tail_hit is not None and (
                result.open_from is None or result.open_from >= tail_hit.start
            ):
                return self._start_discard(window, base, offset, resolved, tail_hit)
            cut = n - TAIL
            if result.open_from is not None:
                cut = min(cut, result.open_from)
            if line is not None:
                cut = min(cut, line.start)
            cut = max(cut, base)
            moved = True
            while moved:
                moved = False
                for h in resolved:
                    # A hit not yet committed keeps the text it was decided from
                    # (its anchor) in the window, so a later feed decides it alike.
                    first = h.start if h.anchor < 0 else min(h.start, h.anchor)
                    # An anchor already committed (in the look-behind) holds the cut at
                    # ``base``; the cut only ever moves back, so the loop ends.
                    if first < cut < h.end and max(base, first) < cut:
                        cut = max(base, first)
                        moved = True
            if line is not None and cut < line.start:
                # Another hold stops the cut before the line: keep the Cookie name with
                # it, so the next read still decides the line from its name.
                cut = max(base, min(cut, line.anchor))
        out = self._shown(render(window, resolved, base, cut), window[base:cut], self._pos)
        self._record(resolved, base, cut, offset)
        if line is not None and cut == line.start:
            out += "" if self._line else TAG
            self._end_line(line.start + offset)
            self._line = True
            self._line_span = (line.start + offset, line.rule_id, line.tier)
        self._ctx = window[:cut][-CONTEXT:]
        self._pending = window[cut:]
        self._pos += cut - base
        held = result.open_from
        self._quiet_at = (
            held + offset
            if not final and held is not None and window[held : held + 6].lower() == "cookie"
            else None
        )
        return out

    def _start_discard(
        self, window: str, base: int, offset: int, resolved: list[Hit], tail_hit: Hit
    ) -> str:
        out = self._shown(
            render(window, resolved, base, tail_hit.start), window[base : tail_hit.start], self._pos
        )
        out += TAG
        self._quiet_at = None
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
