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
``max_block``; a session-token run past 4 KiB) is replaced and the stream discards
input to that unit's own end: the end of the text, or the end of the base64 run.
Chunked output and spans therefore equal the whole-text result at every split.
"""

from __future__ import annotations

from .charsets import B64STD
from .engine import render, resolve, scan
from .model import SECRET, TAG, TAIL_ALL, TAIL_B64RUN, Hit, Span

# Look-behind kept from committed text: covers the pairing window (300 + 40 + 21),
# line leads (64), key-token expansion (64) and URL schemes (32) with margin.
CONTEXT = 1024
# Longer than every anchor literal (``github_pat_``, key spellings, ``-----BEGIN ``,
# URL scheme plus ``://``), so a partial anchor is always held.
TAIL = 47


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
        if self._mode == TAIL_ALL:
            self._pos += len(chunk)
            return ""
        i = 0
        if self._pad == 0:
            rest = chunk.lstrip(B64STD)
            i = len(chunk) - len(rest)
        while i < len(chunk) and chunk[i] == "=" and self._pad < 2:
            self._pad += 1
            i += 1
        dropped = chunk[:i]
        self._pos += i
        self._ctx = (self._ctx + dropped)[-CONTEXT:]
        if i == len(chunk) and self._pad < 2:
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
            if tail_hit is not None and (
                result.open_from is None or result.open_from >= tail_hit.start
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
                    if first < cut < h.end:
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
        if self._mode == TAIL_B64RUN:
            # The window ends inside the base64 run: no padding has been read yet.
            self._ctx = window[-CONTEXT:]
        self._pos += len(window) - base
        self._pending = ""
        return out
