"""Detector B: classify whole tokens by a published format.

Each format row has literal anchors (its prefixes). One ``str.find`` loop per anchor
finds candidates; around each, the token is read by set membership and checked in
order: prefix, body alphabet and length per part, right boundary, checksum. A row
with a checksum skips both boundaries, because the checksum alone decides it. A
Secret row without one skips a body a documented example writes
(``xoxb-your-bot-token``, ``sk_live_xxxxxxxx``).

Every detector here returns ``(hits, opens)``. ``opens`` lists the start of each unit
that the text read so far cannot decide yet (``final=False`` only): a token whose run
reaches the end of the text while it could still be a match. A stream holds output
from the earliest open unit, so streamed and whole text give the same result.
"""

from __future__ import annotations

import bisect
import heapq
from dataclasses import replace

from .charsets import B64URL, B64URL_DOT, cached, run_end
from .checks import CHECKSUMS, b64url_json_object, is_placeholder, is_placeholder_body
from .model import SECRET, Hit
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


def _body_end(text: str, i: int, prefix: str, row: FormatRow) -> int | None:
    """End of the token *row* reads at *i*, boundaries aside; ``None`` when it reads none."""
    e = _match_parts(text, i + len(prefix), row.parts)
    if e is None:
        return None
    if row.checksum is not None:
        return e if CHECKSUMS[row.checksum](text[i:e]) else None
    if row.tier == SECRET and is_placeholder_body(text[i + len(prefix) : e]):
        return None
    return e


def detect_formats(
    text: str,
    rows: tuple[FormatRow, ...],
    final: bool,
    right_blocked: list[tuple[int, int, FormatRow]] | None = None,
) -> tuple[list[Hit], list[int]]:
    """Format hits and open units; a Secret token whose only miss is its right
    boundary goes to *right_blocked* for :func:`detect_glued`."""
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    # Prefix gate: one substring test per two-character head before any per-prefix search.
    for head, members in cached(rows, _prefix_groups):
        if head not in text:
            continue
        for prefix, row in members:
            # A checksum decides a token by itself, so a row with one reads the token
            # glued to a word too: whitespace a renderer strips at a message edge must
            # not hand the reader a key the redactor refuses to see.
            glued = row.checksum is not None
            i = text.find(prefix)
            while i != -1:
                if glued or i == 0 or text[i - 1] not in row.left:
                    r = run_end(text, i, row.run_chars, row.max_total + 1)
                    if not final and r == n and r - i <= row.max_total:
                        opens.append(i)
                    else:
                        # _body_end, inline: this loop is the plan's hottest.
                        e = _match_parts(text, i + len(prefix), row.parts)
                        if e is None:
                            pass
                        elif row.checksum is not None:  # glued
                            if CHECKSUMS[row.checksum](text[i:e]):
                                hits.append(
                                    Hit(i, e, row.id, row.tier, "token_format", row.certainty)
                                )
                        elif row.tier == SECRET and is_placeholder_body(text[i + len(prefix) : e]):
                            pass
                        elif e == n or text[e] not in row.right:
                            hits.append(Hit(i, e, row.id, row.tier, "token_format", row.certainty))
                        elif right_blocked is not None and row.tier == SECRET:
                            right_blocked.append((i, e, row))
                i = text.find(prefix, i + 1)
    return hits, opens


# Held at the end of a stream window: longer than every format prefix and ``eyJ``,
# so a credential that may still start right after a token is seen before it is decided.
_GLUED_HOLD = 16
# Glued JWT starts read per base64url-and-dot run; each read costs up to the run's
# length, so the bound keeps the scan linear. Past it the rest of the run is
# redacted whole, so the bound fails closed.
_GLUED_JOSE_TRIES = 8


def _glued_groups(
    rows: tuple[FormatRow, ...],
) -> tuple[tuple[str, tuple[tuple[str, FormatRow, tuple[tuple[str, ...], int] | None], ...]], ...]:
    """:func:`_prefix_groups` for the rows :func:`detect_glued` reads (Secret, no
    checksum), each with its first literal part's literals and how far past the
    prefix that part can start; ``None`` for a row with no literal part."""
    out = []
    for head, members in _prefix_groups(rows):
        kept = []
        for prefix, row in members:
            if row.tier != SECRET or row.checksum is not None:
                continue  # a checksum row already reads a token glued to a word
            gate, reach = None, 0
            for part in row.parts:
                if part.is_literal:
                    gate = None if "" in part.literals else (part.literals, reach)
                    break
                reach += part.max  # run_end reads a part at most this long
            kept.append((prefix, row, gate))
        if kept:
            out.append((head, tuple(kept)))
    return tuple(out)


def detect_glued(
    text: str,
    rows: tuple[FormatRow, ...],
    jose_rows: tuple[JoseRow, ...],
    hits: list[Hit],
    right_blocked: list[tuple[int, int, FormatRow]],
    blocks: list[tuple[int, int]],
    final: bool,
    open_from: int | None,
) -> tuple[list[Hit], list[int]]:
    """Secret tokens written with no gap after or before another credential.

    A boundary keeps a token from being read out of a longer word. A character
    that belongs to another credential (a Secret hit, a private-key block, or a
    token read here) is not such a word, so it does not block. Two glued tokens
    both redact, and a chain of them does too.

    Only the text inside a credential is searched for a glued start, so the
    cost follows the credentials in the text, not its length.
    """
    n = len(text)
    # An armour hit's BEGIN anchor borders the preceding credential even when
    # its END is missing (or cut off by a stream window); the hit covers only its body.
    spans = [
        (h.anchor if h.detector == "armour" and h.anchor >= 0 else h.start, h.end)
        for h in hits
        if h.tier == SECRET
    ] + list(blocks)
    # (start, end, rule, certainty, detector, right neighbour that must be a credential's).
    # A start found below sits inside a credential, so its left side always is.
    cands = [(i, e, row.id, row.certainty, "token_format", e) for i, e, row in right_blocked]
    heap = spans + [(i, e) for i, e, _row in right_blocked]
    heapq.heapify(heap)
    opens: list[int] = []
    groups = cached(rows, _glued_groups)
    hold = cached(jose_rows, _jose_plan)[2] if jose_rows else 0
    done = 0  # every start before this has been searched: each position once
    run_from, run_to, tries = -1, -1, 0  # the JOSE run read last, and its starts tried
    # (outer span, start, end, rule) of a glued token that starts inside the outer
    # span and runs past its end. Once accepted, the outer format token ends where
    # it starts: the two still cover every character, as separate spans a stream
    # can cut between.
    nested: list[tuple[tuple[int, int], int, int, str]] = []
    while heap:
        s, e = heapq.heappop(heap)
        lo = max(s + 1, done)
        if lo > e:
            continue
        done = e + 1
        area = text[lo : e + _GLUED_HOLD]
        for head, members in groups:
            if head not in area:
                continue
            for prefix, row, gate in members:
                stop = e + len(prefix)
                first = lo
                if gate is not None and not any(
                    text.find(lit, lo + len(prefix), stop + gate[1] + len(lit)) != -1
                    for lit in gate[0]
                ):
                    # No start here can reach the row's literal part, so none reads a
                    # token: only a run still open at the end of the text can matter.
                    if final:
                        continue
                    first = max(lo, n - row.max_total)
                i = text.find(prefix, first, stop)
                while i != -1:
                    if text[i - 1] in row.left:  # else the plain scan read it
                        r = run_end(text, i, row.run_chars, row.max_total + 1)
                        if not final and r == n and r - i <= row.max_total:
                            opens.append(i)
                        else:
                            end = _body_end(text, i, prefix, row)
                            if end is not None:
                                right = end if end < n and text[end] in row.right else -1
                                cands.append((i, end, row.id, row.certainty, "token_format", right))
                                heapq.heappush(heap, (i, end))
                                if i < e < end:
                                    nested.append(((s, e), i, end, row.id))
                    i = text.find(prefix, i + 1, e + len(prefix))
        i = text.find("eyJ", lo, e + 3) if jose_rows else -1
        while i != -1:
            if not run_from <= i < run_to:
                # Read each run once, whole: every start in it shares its real end.
                run_from, run_to = i, run_end(text, i, B64URL_DOT)
                tries = 0
            r = run_to
            if r - i > hold:
                pass  # longer than every JOSE row: decided (rejected) once read
            elif not final and r == n:
                opens.append(i)
            else:
                tries += 1
                if tries <= _GLUED_JOSE_TRIES:
                    found, _ = detect_jose(text[i : r + 1], jose_rows, True)
                    if not any(h.start == 0 and h.tier == SECRET for h in found):
                        # shortcut: mask an invalid glued run whole; split it only if
                        # individual JOSE spans become necessary.
                        end = len(text[i:r].rstrip("."))  # keep sentence punctuation
                        found.append(Hit(0, end, "jwt", SECRET, "jose", "jose"))
                    for h in found:
                        if h.start == 0 and h.tier == SECRET:
                            cands.append((i, i + h.end, h.rule_id, h.certainty, "jose", -1))
                            heapq.heappush(heap, (i, i + h.end))
                            if i < e < i + h.end:
                                nested.append(((s, e), i, i + h.end, h.rule_id))
                elif tries == _GLUED_JOSE_TRIES + 1:
                    # Out of tries: the rest of the run is redacted, failing closed.
                    cands.append((i, r, "jwt", "jose", "jose", -1))
                    heapq.heappush(heap, (i, r))
            i = text.find("eyJ", i + 1, e + 3)
    if not cands:
        return [], opens
    spans += [(c[0], c[1]) for c in cands]
    spans.sort()
    starts = [lo for lo, _hi in spans]
    reach: list[int] = []
    for _lo, hi in spans:
        reach.append(max(reach[-1], hi) if reach else hi)

    def covered(pos: int) -> bool:
        k = bisect.bisect_right(starts, pos) - 1
        return k >= 0 and reach[k] > pos

    if opens:
        open_from = min(opens) if open_from is None else min(open_from, *opens)
    glued: list[Hit] = []
    accepted: set[tuple[int, int, str]] = set()
    for start, end, rule, certainty, detector, right in cands:
        if right == -1 or covered(right):
            glued.append(Hit(start, end, rule, SECRET, detector, certainty))
            accepted.add((start, end, rule))
        elif not final and (
            (n - right < _GLUED_HOLD and not any(c.isspace() for c in text[right:]))
            or (open_from is not None and right >= open_from)
        ):
            # What follows may still turn out to be a credential.
            opens.append(start)
    trim: dict[tuple[int, int], int] = {}
    for outer, start, end, rule in nested:
        if (start, end, rule) in accepted:
            trim[outer] = min(trim.get(outer, outer[1]), start)
    if trim:
        for group in (hits, glued):
            for k, h in enumerate(group):
                cut = trim.get((h.start, h.end))
                if cut is not None and h.detector == "token_format" and h.tier == SECRET:
                    group[k] = replace(h, end=cut)
    return glued, opens


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
                if row.allow_values and cand.rsplit(".", 1)[0] in row.allow_values:
                    break
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
