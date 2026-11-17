"""Whether two memory rows are the same item: one rule for every stage that folds rows.

:func:`same_item` is the only definition. Write-time dedup in a private store
(:mod:`kiro_crew.memory_lesson_dedup`) calls it today; a render-time fold and the
offline maintenance pass are meant to call the same function, so the three can
never disagree about what a duplicate is.

Two rows are the same item when, in order:

1. they share scope and tier, or they are never the same;
2. their normalized texts are equal (NFKC, case folded, whitespace collapsed);
3. otherwise both carry a stored vector of the same width, neither zero, from the
   same embedding space, and their cosine is at least :data:`SAME_ITEM_COSINE`.

A missing, zero, mismatched or unattributed vector is never a match, so an embed
failure leaves behavior as it is without vectors. No vector is computed here.

:func:`leader_fold` groups rows by comparing each one only with the leaders kept
so far, never with rows already folded, so a chain of near pairs cannot pull two
different rules into one group. A user-taught row is always a leader and is never
folded into another row.
"""

from __future__ import annotations

import math
import operator
import struct
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Hashable, Iterable, Sequence

_np: Any
try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is a declared dependency
    _np = None

#: Cosine at or above which two stored vectors name the same item. Lower bands
#: merge different rules (chains form at 0.90 on measured private stores).
SAME_ITEM_COSINE = 0.95

TIER_RULE = "rule"
TIER_FINDINGS = "findings"


def normalize_text(text: object) -> str:
    """*text* in NFKC, case folded and with runs of whitespace collapsed to one space."""
    if not isinstance(text, str):
        return ""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def unit_vector(blob: object, dim: int) -> Any:
    """A stored float32 blob of width *dim* as a unit vector, or ``None`` when unusable.

    ``None`` for a missing blob, a width other than *dim*, a non-finite value or a
    zero vector. With numpy installed the vector is a float64 numpy array computed
    without a Python loop per value, since a session-start render converts every
    stored rule vector; a tuple otherwise.
    """
    if not isinstance(blob, (bytes, bytearray, memoryview)) or dim <= 0:
        return None
    raw = bytes(blob)
    if len(raw) != dim * 4:
        return None
    if _np is not None:
        array = _np.frombuffer(raw, dtype=_np.float32).astype(_np.float64)
        if not _np.isfinite(array).all():
            return None
        length = float(_np.sqrt(array @ array))
        if length == 0.0:
            return None
        return array / length
    values = struct.unpack(f"{dim}f", raw)
    if not all(math.isfinite(v) for v in values):
        return None
    norm = math.sqrt(math.fsum(v * v for v in values))
    if norm == 0.0:
        return None
    return tuple(v / norm for v in values)


@dataclass(frozen=True, eq=False)
class Item:
    """One row as :func:`same_item` sees it.

    *vector* is a unit vector (see :func:`unit_vector`) or ``None``. *space* names
    the embedding space the vector came from; ``None`` means it cannot be told,
    which makes the vector unusable for a match.
    """

    key: str
    text: str
    scope: str | None
    tier: str
    vector: Any = None
    space: Hashable | None = None
    user_taught: bool = False
    normalized: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "normalized", normalize_text(self.text))


def cosine(a: Item, b: Item) -> float | None:
    """Cosine of the two stored vectors, or ``None`` when either cannot be compared."""
    if (
        a.vector is None
        or b.vector is None
        or len(a.vector) != len(b.vector)
        or a.space is None
        or a.space != b.space
    ):
        return None
    if _np is not None and isinstance(a.vector, _np.ndarray) and isinstance(b.vector, _np.ndarray):
        return float(_np.dot(a.vector, b.vector))
    return float(sum(map(operator.mul, a.vector, b.vector)))


def same_item(a: Item, b: Item) -> bool:
    """Whether *a* and *b* are one item (rules in the module docstring)."""
    if a.scope != b.scope or a.tier != b.tier:
        return False
    if a.normalized and a.normalized == b.normalized:
        return True
    sim = cosine(a, b)
    return sim is not None and sim >= SAME_ITEM_COSINE


def leader_of(candidate: Item, leaders: Iterable[Item]) -> Item | None:
    """The first of *leaders* that *candidate* is the same item as, or ``None``.

    A user-taught *candidate* is never folded, so it has no leader.
    """
    if candidate.user_taught:
        return None
    for leader in leaders:
        if same_item(candidate, leader):
            return leader
    return None


def leader_fold(items: Sequence[Item]) -> list[tuple[Item, list[Item]]]:
    """Group *items*, visited in the given order, into ``(leader, folded)`` pairs.

    The caller orders *items* (authority first, then newest first, for a render
    fold). Each item joins the first leader it matches, or becomes a leader.
    """
    groups: list[tuple[Item, list[Item]]] = []
    for item in items:
        leader = leader_of(item, (g[0] for g in groups))
        if leader is None:
            groups.append((item, []))
            continue
        for kept, folded in groups:
            if kept is leader:
                folded.append(item)
                break
    return groups
