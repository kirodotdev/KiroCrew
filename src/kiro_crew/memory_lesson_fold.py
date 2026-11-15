"""Fold near-duplicate rules out of the session-start rule block at render time.

A store can hold several rows that state one rule: the same words in another case
or spacing, or a rewording whose stored vector sits at cosine 0.95 or more. Each
copy takes room in the capped rule block, so a copy can push a different rule out.
``memory.lesson_fold`` picks the mode:

* ``off`` (default) -- nothing is read; the block is today's, byte for byte.
* ``shadow`` -- the block is today's; the injection receipt records each
  ``(leader, folded)`` id pair the fold would apply.
* ``enforce`` -- each folded row is left out of the block before the budget is
  applied, and the block says how many were folded and that ``learn_list`` shows
  all. A folded row is not a budget omission and is not counted as one.

Rows are grouped with :func:`kiro_crew.memory_item_identity.same_item`, the one
rule every stage uses: same scope and tier, then equal normalized text or stored
vectors at cosine 0.95 or more. Rows are visited by authority level (user-taught
first, :func:`kiro_crew.memory_rule_authority.authority`), then newest first, and
each is compared only with leaders already kept, so a chain of near pairs never
joins two different rules. A user-taught row is always a leader. A row with a
NOT-clause its leader lacks stays a leader, so no clause leaves the prompt. A
missing, zero, mismatched or unattributed vector never matches, so a store without
vectors folds textual copies only.

Render only: no row is written, retired or re-tiered.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final, Hashable

from kiro_crew.config.memory_sections import lesson_fold_mode_from
from kiro_crew.memory_item_identity import (
    SAME_ITEM_COSINE,
    TIER_RULE,
    Item,
    leader_of,
    normalize_text,
    same_item,
    unit_vector,
)
from kiro_crew.memory_rule_authority import authority

_np: Any
try:
    import numpy as _np
except ImportError:  # pragma: no cover - numpy is a declared dependency
    _np = None

if TYPE_CHECKING:
    from kiro_crew.vector_memory import VectorMemoryStore

logger = logging.getLogger(__name__)

MODE_OFF: Final = "off"
MODE_SHADOW: Final = "shadow"
MODE_ENFORCE: Final = "enforce"

#: Bump when what folds, or the visit order, changes, so receipts taken under the
#: old policy can be told from the new.
POLICY_VERSION: Final = 1

#: The line the rule block carries under ``enforce`` when it folded any row.
FOLD_NOTICE: Final = "[Folded {count} near-duplicate rules; learn_list shows all.]"


def configured_mode() -> str:
    """``memory.lesson_fold`` from the live config; the default when unreadable."""
    try:
        from kiro_crew.config.loader import KiroCrewConfig

        raw = getattr(KiroCrewConfig.load().memory, "lesson_fold", None)
    except Exception:
        logger.debug("Could not read memory.lesson_fold; using the default", exc_info=True)
        raw = None
    return lesson_fold_mode_from(raw)


def store_space(store: "VectorMemoryStore") -> tuple[Hashable | None, int]:
    """``(space, dim)`` for *store*'s stored vectors; space is ``None`` when unrecorded."""
    signature = store.recorded_embedding_space()
    space = (signature, store._space_generation) if signature is not None else None
    return space, int(store._embedding_dim)


def _clause(value: object, key: str) -> str:
    """The NOT-clause of stored lesson *value*, normalized; ``""`` when it has none.

    A legacy string row carries its clause in-band and is split by the lesson
    codec. One whose separator the codec cannot place is read as all clause, so
    it stays a leader rather than lose a clause nobody can read back.
    """
    # Imported here: the lesson renderer imports this module.
    from kiro_crew.vector_memory_runtime import lessons as _lessons

    fields = _lessons._lesson_fields_for_row(value, key)
    if fields is not None and fields[1]:
        return normalize_text(fields[1])
    if isinstance(value, str) and _lessons._LESSON_NEGATIVE_SEP in value:
        return normalize_text(value)
    return ""


def _item(row: dict, value: object, space: Hashable | None, dim: int) -> Item | None:
    # Imported here: the lesson renderer imports this module.
    from kiro_crew.vector_memory_runtime import lessons as _lessons

    text = _lessons._lesson_embed_text(value)
    if not text:
        return None
    return Item(
        key=str(row.get("key") or ""),
        text=text,
        scope=_lessons._lesson_scope(value),
        tier=TIER_RULE,
        vector=unit_vector(row.get("embedding"), dim),
        space=space,
        user_taught=row.get("source") == "user_explicit",
    )


class _Leaders:
    """The leaders kept so far, grouped by scope and tier, with their vectors stacked.

    :meth:`first` answers what ``leader_of(item, leaders)`` answers. One matrix
    product per row picks the leaders whose cosine could reach the bar, and
    :func:`same_item` still decides each of them in leader order, so a store of
    a few hundred rules folds in milliseconds rather than one Python call per pair.
    """

    def __init__(self, dim: int) -> None:
        self._dim = dim
        self._groups: dict[tuple[str | None, str], dict[str, Any]] = {}

    def first(self, item: Item) -> Item | None:
        if item.user_taught:
            return None
        group = self._groups.get((item.scope, item.tier))
        if group is None:
            return None
        kept: list[Item] = group["items"]
        vector = item.vector
        if _np is None or not isinstance(vector, _np.ndarray) or len(vector) != self._dim:
            return leader_of(item, kept)
        candidates = set(group["texts"].get(item.normalized, ())) if item.normalized else set()
        # The margin keeps a pair the matrix product rounds just under the bar;
        # same_item below is what decides it.
        sims = group["matrix"][: len(kept)] @ vector
        candidates.update(int(i) for i in _np.nonzero(sims >= SAME_ITEM_COSINE - 1e-6)[0])
        for index in sorted(candidates):
            if same_item(item, kept[index]):
                return kept[index]
        return None

    def add(self, item: Item) -> None:
        key = (item.scope, item.tier)
        group = self._groups.get(key)
        if group is None:
            matrix = _np.zeros((16, self._dim)) if _np is not None and self._dim > 0 else None
            group = self._groups[key] = {"items": [], "texts": {}, "matrix": matrix}
        index = len(group["items"])
        group["items"].append(item)
        group["texts"].setdefault(item.normalized, []).append(index)
        matrix = group["matrix"]
        if matrix is None:
            return
        if index == len(matrix):
            # Doubling keeps the copies linear in the number of leaders.
            matrix = group["matrix"] = _np.concatenate([matrix, _np.zeros_like(matrix)])
        vector = item.vector
        if isinstance(vector, _np.ndarray) and len(vector) == self._dim:
            matrix[index] = vector


def fold_pairs(
    entries: Sequence[tuple[dict, str]],
    newest_first: Sequence[tuple[dict, str]],
    *,
    space: Hashable | None,
    dim: int,
) -> list[tuple[str, str]]:
    """``(leader key, folded key)`` for every rule-block row the fold leaves out.

    *entries* are the rule block's ``(row, text)`` pairs; *newest_first* is the
    store's recency order of the same rows (or a superset), which breaks ties
    inside an authority level. Every row is a rule-tier row, so tier never
    separates two of them.
    """
    position = {id(row): index for index, (row, _) in enumerate(newest_first)}
    visit = sorted(
        entries, key=lambda entry: (authority(entry[0]), position.get(id(entry[0]), len(position)))
    )
    leaders = _Leaders(dim)
    values: dict[str, Any] = {}
    pairs: list[tuple[str, str]] = []
    for row, _ in visit:
        try:
            value = json.loads(row["value_json"])
        except (KeyError, TypeError, ValueError):
            continue
        item = _item(row, value, space, dim)
        if item is None or not item.key:
            continue
        leader = leaders.first(item)
        clause = _clause(value, item.key)
        if leader is None or clause not in ("", _clause(values[leader.key], leader.key)):
            leaders.add(item)
            values[item.key] = value
            continue
        pairs.append((leader.key, item.key))
    return pairs
