"""Detector C: private-key armour blocks (PEM and PGP).

``str.find`` locates the BEGIN line's fixed lead; the key type is read over a named
set; the matching END line is found with one bounded ``str.find``. Markers work
anywhere in a line, so a key held in a JSON string with ``\\n`` escapes is found too.

Only the BODY is a hit: the BEGIN and END lines stay, and so do the blanks and line
breaks (or ``\\n`` escapes) beside them. A body that is one placeholder this redactor
wrote is no hit, so redaction is a fixed point. A block with no END within
``max_block`` runs from its body to the end of the text (owner ruling 1a). A text that
ends inside that bound keeps the blanks and the start of an END line it ends with: a
stream commits a decided block up to a blank, which can fall inside its END line, and
that prefix reads alike.
The BEGIN line decides a hit, so it is the hit's anchor. Scanning resumes after each
block, so repeated headers stay linear.
"""

from __future__ import annotations

from .charsets import cached
from .model import PLAN_TAGS, TAIL_ALL, Hit
from .table import ArmourRow

# What stays beside a body: blanks and line breaks, real or JSON-escaped.
_EDGE_BLANKS = frozenset(" \t\r\n")
_EDGE_ESCAPES = ("\\n", "\\r")


def _header_end(text: str, p: int, row: ArmourRow) -> tuple[int, str] | None | bool:
    """``(header_end, type)`` for a full header at ``p``; ``True`` if it may still arrive."""
    lead = p + len(row.begin)
    n = len(text)
    if row.type_max == 0:
        if text.startswith(row.suffix, lead):
            return lead + len(row.suffix), ""
        rest = text[lead : lead + len(row.suffix)]
        return len(rest) < len(row.suffix) and row.suffix.startswith(rest)
    limit = lead + row.type_max + len(row.suffix)
    idx = text.find(row.suffix, lead, limit)
    if idx != -1:
        key_type = text[lead:idx]
        if all(ch in row.type_chars for ch in key_type):
            return idx + len(row.suffix), key_type
        return None
    # Not found within the bound: decided unless the header may still be arriving.
    if n >= limit:
        return None
    tail = text[lead:]
    allowed = set(row.type_chars) | set(row.suffix)
    return all(ch in allowed for ch in tail)


def _body_start(text: str, i: int, stop: int) -> int:
    """Where the body after a BEGIN line ending at *i* starts, at most *stop*."""
    while i < stop:
        if text[i] in _EDGE_BLANKS:
            i += 1
        elif i + 2 <= stop and text.startswith(_EDGE_ESCAPES, i):
            i += 2
        else:
            break
    return i


def _body_end(text: str, start: int, j: int) -> int:
    """Where a body starting at *start* ends before an END line at *j*."""
    while j > start:
        if text[j - 1] in _EDGE_BLANKS:
            j -= 1
        elif j - 2 >= start and text.startswith(_EDGE_ESCAPES, j - 2):
            j -= 2
        else:
            break
    return j


def _cut_short(text: str, start: int, end_line: str) -> int:
    """Where a body with no END line ends: before the blanks and the proper prefix of
    *end_line* the text ends with, else before trailing blanks only."""
    n = len(text)
    for size in range(min(len(end_line) - 1, n - start), 0, -1):
        if text.endswith(end_line[:size]):
            j = _body_end(text, start, n - size)
            if j < n - size:
                return j
    return _body_end(text, start, n)


def _is_tag(text: str, start: int, end: int) -> bool:
    """Whether ``text[start:end]`` is exactly one placeholder this redactor writes:
    a body that only opens like one (``[REDACTED: <key material>]``) is read."""
    return text[start:end] in PLAN_TAGS


def _begin_groups(rows: tuple[ArmourRow, ...]) -> tuple[tuple[str, tuple[ArmourRow, ...]], ...]:
    begins = dict.fromkeys(row.begin for row in rows)
    return tuple((b, tuple(r for r in rows if r.begin == b)) for b in begins)


def detect_armour(
    text: str, rows: tuple[ArmourRow, ...], final: bool
) -> tuple[list[Hit], list[int]]:
    n = len(text)
    hits: list[Hit] = []
    opens: list[int] = []
    for begin, group in cached(rows, _begin_groups):
        p = text.find(begin)
        while p != -1:
            nxt = p + 1
            for row in group:
                found = _header_end(text, p, row)
                if found is True:
                    if not final:
                        opens.append(p)
                        return hits, opens
                    continue
                if not found:
                    continue
                header_end, key_type = found
                end_line = row.end + key_type + row.suffix
                limit = header_end + row.max_block + len(end_line)
                # An END line right after the header ends an empty block.
                idx = text.find(end_line, header_end, limit)
                if idx != -1:
                    start = _body_start(text, header_end, idx)
                    end = _body_end(text, start, idx)
                    if end > start and not _is_tag(text, start, end):
                        hits.append(Hit(start, end, row.id, row.tier, "armour", "armour", anchor=p))
                    nxt = idx + len(end_line)
                    break
                if final or n >= limit:
                    # Fail closed: the body runs to the end of the text.
                    start = _body_start(text, header_end, n)
                    # A text that ends inside the block's bound may have lost its END
                    # line; past the bound the END line cannot count.
                    end = _cut_short(text, start, end_line) if final and n < limit else n
                    if end > start and not _is_tag(text, start, end):
                        tail = "" if final else TAIL_ALL
                        hits.append(
                            Hit(
                                start,
                                end,
                                row.unterminated_id,
                                row.tier,
                                "armour",
                                "armour",
                                tail=tail,
                                anchor=p,
                            )
                        )
                    return hits, opens
                opens.append(p)
                return hits, opens
            p = text.find(begin, nxt)
    return hits, opens
