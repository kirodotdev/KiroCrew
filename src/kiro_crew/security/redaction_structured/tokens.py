"""Detector B: classify whole tokens by a published format.

Each format row has literal anchors (its prefixes). One ``str.find`` loop per anchor
finds candidates; around each, the token is read by set membership and checked in
order: prefix, body alphabet and length per part, right boundary, checksum.

Every detector here returns ``(hits, opens)``. ``opens`` lists the start of each unit
that the text read so far cannot decide yet (``final=False`` only): a token whose run
reaches the end of the text while it could still be a match. A stream holds output
from the earliest open unit, so streamed and whole text give the same result.
"""

from __future__ import annotations

import bisect

from .charsets import B64URL, B64URL_DOT, cached, run_end
from .checks import CHECKSUMS, b64url_json_object, is_placeholder
from .model import Hit
from .table import FormatRow, JoseRow, PairRow, Part

# JOSE runs longer than every JOSE row's maximum are decided (rejected) once read.
_JOSE_TRAILING_DOTS = 3


def _match_parts(text: str, pos: int, parts: tuple[Part, ...]) -> int | None:
    for part in parts:
        if part.is_literal:
            for lit in part.literals:
                if text.startswith(lit, pos):
                    pos += len(lit)
                    break
            else:
                return None
            continue
        end = run_end(text, pos, part.chars, part.max)
        length = end - pos
        if part.lengths is not None:
            if length not in part.lengths:
                return None
        elif not part.min <= length <= part.max:
            return None
        pos = end
    return pos


_HEAD = 2


def _prefix_groups(
    rows: tuple[FormatRow, ...],
) -> tuple[tuple[str, tuple[tuple[str, FormatRow], ...]], ...]:
    """Every row's prefixes grouped by their first two characters, in row order."""
    groups: dict[str, list[tuple[str, FormatRow]]] = {}
    for row in rows:
        for prefix in row.prefixes:
            groups.setdefault(prefix[:_HEAD], []).append((prefix, row))
    return tuple((head, tuple(members)) for head, members in groups.items())


def detect_formats(
    text: str, rows: tuple[FormatRow, ...], final: bool
) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    # Prefix gate: one substring test per two-character head before any per-prefix search.
    for head, members in cached(rows, _prefix_groups):
        if head not in text:
            continue
        for prefix, row in members:
            i = text.find(prefix)
            while i != -1:
                if i == 0 or text[i - 1] not in row.left:
                    r = run_end(text, i, row.run_chars, row.max_total + 1)
                    if not final and r == n and r - i <= row.max_total:
                        opens.append(i)
                    else:
                        e = _match_parts(text, i + len(prefix), row.parts)
                        if (
                            e is not None
                            and (e == n or text[e] not in row.right)
                            and (row.checksum is None or CHECKSUMS[row.checksum](text[i:e]))
                        ):
                            hits.append(Hit(i, e, row.id, row.tier, "token_format", row.certainty))
                i = text.find(prefix, i + 1)
    return hits, opens


# ── JOSE ──


def _segments_ok(row: JoseRow, segs: list[str]) -> bool:
    if len(segs) != row.segments:
        return False
    for idx, (seg, (lo, hi)) in enumerate(zip(segs, row.segment_lengths, strict=True)):
        body = seg
        if idx in row.eyj_segments:
            if not seg.startswith("eyJ"):
                return False
            body = seg[3:]
        if not lo <= len(body) <= hi:
            return False
    return True


def _jose_ok(row: JoseRow, segs: list[str]) -> bool:
    if not _segments_ok(row, segs):
        return False
    header = b64url_json_object(segs[0])
    if header is None:
        return False
    for key in row.header_keys:
        value = header.get(key)
        if not isinstance(value, str) or not value or value.lower() == "none":
            return False
    if row.payload_object or row.payload_claims:
        payload = header if row.segments == 2 else b64url_json_object(segs[1])
        if payload is None:
            return False
        if not row.payload_claims <= payload.keys():
            return False
    return True


def _jose_plan(
    rows: tuple[JoseRow, ...],
) -> tuple[list[JoseRow], list[JoseRow], int, frozenset[str]]:
    multi = [r for r in rows if r.segments > 2]
    link = [r for r in rows if r.segments == 2]
    hold = max((r.max_total for r in rows), default=0) + _JOSE_TRAILING_DOTS
    # A character every row refuses on the left (empty when there are no rows).
    lefts = frozenset.intersection(*(r.left for r in rows)) if rows else frozenset()
    return multi, link, hold, lefts


def detect_jose(text: str, rows: tuple[JoseRow, ...], final: bool) -> tuple[list[Hit], list[int]]:
    """JWT/JWE by full-token split; the two-segment link token by a bounded read."""
    hits: list[Hit] = []
    opens: list[int] = []
    i = text.find("eyJ")
    if i == -1:
        return hits, opens
    n = len(text)
    multi, link, hold, lefts = cached(rows, _jose_plan)
    run_from, run_to = -1, -1
    while i != -1:
        prev = text[i - 1] if i else ""
        if prev and prev in lefts:
            i = text.find("eyJ", i + 1)
            continue
        if run_from <= i < run_to:
            r = run_to
        else:
            run_from, run_to = i, run_end(text, i, B64URL_DOT)
            r = run_to
        if not final and r == n and r - i <= hold:
            opens.append(i)
            i = text.find("eyJ", i + 1)
            continue
        for row in multi:
            if prev and prev in row.left:
                continue
            if r - i > row.max_total + _JOSE_TRAILING_DOTS:
                continue
            token = text[i:r]
            stripped = token.rstrip(".")
            dots = len(token) - len(stripped)
            if dots > _JOSE_TRAILING_DOTS:
                continue
            # Sentence dots first; then keep trailing dots as empty final segments
            # (a JWE may end in an empty tag).
            for keep in range(dots + 1):
                cand = token[: len(stripped) + keep]
                if _jose_ok(row, cand.split(".")):
                    hits.append(Hit(i, i + len(cand), row.id, row.tier, "jose", "jose"))
                    break
        for row in link:
            if prev and prev in row.left:
                continue
            hit = _link_at(text, i, row)
            if hit is not None:
                hits.append(hit)
        i = text.find("eyJ", i + 1)
    return hits, opens


def _link_at(text: str, i: int, row: JoseRow) -> Hit | None:
    (lo1, hi1), (lo2, hi2) = row.segment_lengths
    head_end = run_end(text, i, B64URL, hi1 + 4)
    if not lo1 + 3 <= head_end - i <= hi1 + 3:
        return None
    if head_end >= len(text) or text[head_end] != ".":
        return None
    sig_end = run_end(text, head_end + 1, B64URL, hi2 + 1)
    if not lo2 <= sig_end - head_end - 1 <= hi2:
        return None
    if sig_end < len(text) and text[sig_end] in row.right:
        return None
    if not _jose_ok(row, [text[i:head_end], text[head_end + 1 : sig_end]]):
        return None
    return Hit(i, sig_end, row.id, row.tier, "jose", "jose")


# ── AWS secret key by key-ID pairing ──


def _merge(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    for lo, hi in sorted(windows):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


class _KeyIds:
    def __init__(self, spans: list[tuple[int, int]]) -> None:
        self._spans = sorted(spans)
        self._ends = [e for _, e in self._spans]

    def near(self, start: int, end: int, window: int) -> bool:
        i = bisect.bisect_left(self._ends, start - window)
        while i < len(self._spans) and self._spans[i][0] <= end + window:
            s, e = self._spans[i]
            if e <= start or s >= end:
                return True
            i += 1
        return False


def detect_pairs(
    text: str,
    rows: tuple[PairRow, ...],
    key_id_spans: list[tuple[int, int]],
    final: bool,
) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    if final and not key_id_spans:
        return hits, opens
    for row in rows:
        ids = [(s, e) for s, e in key_id_spans if text.startswith(row.pair_prefixes, s)]
        key_ids = _KeyIds(ids)
        reach = row.window + row.length + 1
        windows = [(max(0, s - reach), min(n, e + reach)) for s, e in ids]
        # A key ID still to come could pair with a candidate near the end.
        id_len = 21  # key ID (20) plus the boundary character after it
        if not final:
            windows.append((max(0, n - (row.window + row.length + id_len + 2)), n))
        for lo, hi in _merge(windows):
            pos = lo
            while pos < hi:
                if text[pos] not in row.left:
                    pos += 1
                    continue
                a = pos
                while a > 0 and text[a - 1] in row.left and pos - a <= row.length:
                    a -= 1
                if a > 0 and text[a - 1] in row.left:
                    pos = run_end(text, pos, row.chars)
                    continue
                b = run_end(text, pos, row.chars, row.length + 1 - (pos - a))
                if b - a > row.length:
                    pos = run_end(text, b, row.chars)
                    continue
                pos = b
                if b == n and not final:
                    opens.append(a)
                    continue
                if b - a != row.length:
                    continue
                if b < n and text[b] in row.right:
                    continue
                value = text[a:b]
                if row.allow_suffixes and value.endswith(row.allow_suffixes):
                    continue
                if is_placeholder(value):
                    continue
                if key_ids.near(a, b, row.window):
                    hits.append(Hit(a, b, row.id, row.tier, "key_id_pair", "key_id_pair"))
                elif not final and n < b + row.window + id_len:
                    opens.append(a)
    return hits, opens
