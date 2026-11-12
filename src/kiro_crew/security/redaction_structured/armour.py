"""Detector C: private-key armour blocks (PEM and PGP).

``str.find`` locates the BEGIN line's fixed lead; the key type is read over a named
set; the matching END line is found with one bounded ``str.find``. Markers work
anywhere in a line, so a key held in a JSON string with ``\\n`` escapes is found too.
A block with no END within ``max_block`` runs to the end of the text (owner ruling 1a).
Scanning resumes after each block, so repeated headers stay linear.
"""

from __future__ import annotations

from .charsets import cached
from .model import TAIL_ALL, Hit
from .table import ArmourRow


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
                idx = text.find(end_line, header_end + 1, limit)
                if idx != -1:
                    stop = idx + len(end_line)
                    hits.append(Hit(p, stop, row.id, row.tier, "armour", "armour"))
                    nxt = stop
                    break
                if final or n >= limit:
                    tail = "" if final else TAIL_ALL
                    hits.append(
                        Hit(p, n, row.unterminated_id, row.tier, "armour", "armour", tail=tail)
                    )
                    return hits, opens
                opens.append(p)
                return hits, opens
            p = text.find(begin, nxt)
    return hits, opens
