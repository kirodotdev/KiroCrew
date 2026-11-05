"""Resolve rule-table hits and replace what their tier says to replace.

Where hits overlap, one hit keeps each character: the higher tier, then the more
certain detector, then the longer range, then the earlier position, then the
lower rule id, so the outcome never depends on rule order or set iteration.

Identifier hits take part in the resolution and are never replaced. A pattern
hit inside a hit from a name-keyed detector (``labelled``, ``url_param``)
splits it: an identifier stays readable, a vendor key keeps its own record, and
the rest of the value is still replaced. A pattern rule is never split this
way: a key-ID-shaped run inside a PEM block must not cut that hit.

An existing ``[REDACTED...]`` placeholder is never matched: a hit inside one or
cutting across one is dropped, which is what makes redaction idempotent. A
structural hit that encloses a whole placeholder is kept.
"""

from __future__ import annotations

import bisect
import logging
import re

from kiro_crew.security.redaction_rules.detectors import detect
from kiro_crew.security.redaction_rules.model import (
    NAME_DETECTORS,
    PLACEHOLDER,
    Detector,
    Hit,
    Record,
    RedactionFailed,
    RedactionResult,
)
from kiro_crew.security.redaction_rules.rules import RuleTable, load_table

logger = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\[REDACTED[^\]\r\n]{0,200}\]")


class _Claimed:
    """Non-overlapping spans kept so far, with an O(log n) overlap test."""

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def overlaps(self, start: int, end: int) -> bool:
        i = bisect.bisect_left(self._starts, end)
        return i > 0 and self._ends[i - 1] > start

    def add(self, start: int, end: int) -> None:
        i = bisect.bisect_left(self._starts, start)
        self._starts.insert(i, start)
        self._ends.insert(i, end)


def _resolution_key(hit: Hit) -> tuple[int, int, int, int, str]:
    return (hit.tier.rank, hit.detector.rank, -hit.length, hit.start, hit.rule_id)


#: Characters trimmed from the edges of what is left of a name-keyed value once
#: an identifier is cut out of it, so ``AC...:token`` leaves ``token``.
_PIECE_EDGE = " \t:/=,;@|"


def _touches_placeholder(hit: Hit, spans: list[tuple[int, int]]) -> bool:
    """The hit lies inside a placeholder or cuts across one.

    A ``structural`` hit that encloses a whole placeholder (a private-key block
    whose comment line was redacted earlier) is kept: dropping it would leave
    the rest of the block in the clear. A name-keyed hit never is, since its
    value has already been handled once.
    """
    for start, end in spans:
        if hit.start < end and start < hit.end:
            encloses = hit.start <= start and end <= hit.end and hit.length > end - start
            if not (encloses and hit.detector is Detector.STRUCTURAL):
                return True
    return False


def _split_name_keyed(hit: Hit, text: str, anchors: list[Hit]) -> list[Hit]:
    """A name-keyed hit cut around the pattern hits inside it.

    An identifier inside a labelled value (``Credential=AKIA...``,
    ``AC...:<auth token>``) stays readable, and a vendor key inside one
    (``api_key=old sk-...``) keeps its own record; every other piece of the
    value is still replaced, so neither shields what sits beside it.
    """
    if hit.detector not in NAME_DETECTORS:
        return [hit]
    inside = sorted(
        (a for a in anchors if a.overlaps(hit) and a.detector.rank < hit.detector.rank),
        key=lambda a: a.start,
    )
    if not inside:
        return [hit]
    pieces: list[Hit] = []
    cursor = hit.start
    for anchor in [*inside, None]:
        stop = hit.end if anchor is None else max(cursor, anchor.start)
        lo, hi = cursor, stop
        while lo < hi and text[lo] in _PIECE_EDGE:
            lo += 1
        while hi > lo and text[hi - 1] in _PIECE_EDGE:
            hi -= 1
        if hi > lo:
            pieces.append(Hit(hit.rule_id, hit.tier, hit.detector, lo, hi))
        if anchor is not None:
            cursor = max(cursor, anchor.end)
    return pieces


def hits(text: str, table: RuleTable | None = None) -> list[Hit]:
    """The resolved, non-overlapping hits on ``text``, in text order."""
    table = table if table is not None else load_table()
    spans = [m.span() for m in _PLACEHOLDER_RE.finditer(text)]
    found = [h for h in detect(table, text) if h.length and not _touches_placeholder(h, spans)]

    anchors = [h for h in found if h.detector not in NAME_DETECTORS]
    if anchors:
        found = [
            piece
            for h in found
            for piece in _split_name_keyed(h, text, anchors)
            if not _touches_placeholder(piece, spans)
        ]

    kept: list[Hit] = []
    claimed = _Claimed()
    for hit in sorted(found, key=_resolution_key):
        if not claimed.overlaps(hit.start, hit.end):
            claimed.add(hit.start, hit.end)
            kept.append(hit)
    kept.sort(key=lambda h: (h.start, h.end))
    return kept


def _redact(text: str, table: RuleTable | None) -> RedactionResult:
    if not isinstance(text, str):
        raise TypeError("text must be a str")
    parts: list[str] = []
    records: list[Record] = []
    cursor = 0
    for hit in hits(text, table):
        if not hit.tier.redacts:
            continue
        parts.append(text[cursor : hit.start])
        parts.append(PLACEHOLDER)
        records.append(Record(hit.rule_id, hit.tier, hit.detector, hit.length))
        cursor = hit.end
    parts.append(text[cursor:])
    return RedactionResult("".join(parts), tuple(records))


def redact_with_findings(text: str, *, table: RuleTable | None = None) -> RedactionResult:
    """Replace every secret and bearer-link value in ``text`` and describe each replacement.

    Identifier values are kept. Any internal error raises :class:`RedactionFailed`
    with only the error's type in its message, so neither the text nor a value
    from it reaches a log through the exception.
    """
    try:
        return _redact(text, table)
    except Exception as exc:
        raise RedactionFailed(f"credential redaction failed ({type(exc).__name__})") from None
