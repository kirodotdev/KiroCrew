"""Deltas between two renders of the ``work`` fold, and the one rule that applies them.

The ``work`` board is the only slot fold big enough to be worth a delta (about 1 MB to
2.4 MB on a large conductor board) and the only one the browser caches, so it is the
only fold this module knows. Every other fold keeps publishing its whole value.

THE OPS. A ``work`` render is ``{conductor, items, omitted, forgotten}`` with ``items``
in creation order. The fold changes an item in ways that are not "row appended" -- an
event is inserted in time order, two progress reports collapse into one, the event tail
is capped, a close can evict the oldest closed item -- but each of those changes ONE
item, so replacing that item whole is always correct. A delta is therefore::

    {
        "header": {"conductor": {...}, "omitted": 3, "forgotten": {...},
                   "order": ["it_a", "it_b"]},
        "items": {"it_b": {...the whole item as the render shows it...}},
        "remove": ["it_old"],
    }

``header`` is always present and whole: every top-level key of the render but
``items`` (the conductor header and two counters today), plus the ids in render order. ``items`` holds every item whose rendered value
differs from the base render. ``remove`` lists ids the base held and the new render does
not.

HOW A DELTA IS MADE. :func:`diff_work` compares two RENDERS, the one last published and
the one being published, item by item. That is O(items) dict comparisons, a few ms on
the largest board measured, and it needs no change marks inside the fold step, so the
fold and its memo are untouched. The cost is that the publisher holds the last
published render per board (bounded, see ``eager``).

HOW A DELTA IS APPLIED. :func:`apply_work` is the receiver rule, written once here so
the in-process receivers and the tests share it (the browser's TypeScript twin,
``website/src/hooks/websocket/workDelta.ts``, runs the same fixtures). The result holds EXACTLY the ids in
``header.order``: an id the receiver holds that ``order`` does not name is dropped even
when ``remove`` does not list it, and an id ``order`` names that the receiver neither
holds nor received is an inconsistency, answered with ``None`` so the receiver pulls a
snapshot instead of drawing a board with a hole in it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

#: The folds that publish deltas. One today.
DELTA_FOLDS: Final[frozenset[str]] = frozenset({"work"})

#: :attr:`~kiro_crew.crew_log.bus.FoldAdvanced.kind` values.
KIND_VALUE: Final[str] = "value"
KIND_DELTA: Final[str] = "delta"
KIND_SNAPSHOT_NEEDED: Final[str] = "snapshot_needed"

#: Why a publish could not be a delta. Carried in the delta slot of a
#: ``snapshot_needed`` event as ``{"reason": ...}`` so a log line can say which.
REASON_NO_BASE: Final[str] = "no_base"
REASON_GENERATION: Final[str] = "generation"
REASON_REVISION: Final[str] = "revision_not_rising"
REASON_SHAPE: Final[str] = "shape"
#: Set by a RECEIVER, never by the publisher: the delta's base is not the revision the
#: receiver holds, so applying it would draw a board with holes in it.
REASON_GAP: Final[str] = "gap"


def chained(event: Any, held: int) -> Any:
    """*event* as a receiver holding revision *held* must take it.

    The receiver rule (RFC section 2.4) for the one case a receiver cannot apply: a
    ``delta`` whose ``base_revision`` is not *held*. That event comes back as
    ``snapshot_needed`` with reason :data:`REASON_GAP`, everything else as it is. The
    whole value an event still carries during the migration is kept, so a receiver
    that can use it need not read.

    Duplicates (``revision <= held``) are not this function's business: the bus floor
    and every receiver drop those before asking.
    """
    if getattr(event, "kind", KIND_VALUE) != KIND_DELTA:
        return event
    if int(getattr(event, "base_revision", 0) or 0) == held:
        return event
    replace = getattr(event, "_replace", None)
    if replace is None:  # pragma: no cover - every bus event is a NamedTuple
        return event
    return replace(kind=KIND_SNAPSHOT_NEEDED, base_revision=0, delta={"reason": REASON_GAP})


def _item_id(item: Any) -> str:
    if isinstance(item, Mapping):
        value = item.get("item_id")
        if isinstance(value, str) and value:
            return value
    return ""


def _index(value: Mapping[str, Any]) -> "tuple[list[str], dict[str, Mapping[str, Any]]] | None":
    """The ids in render order and each item by id, or ``None`` for a render this module
    cannot address (an item with no id, or two items with one id)."""
    items = value.get("items")
    if not isinstance(items, list):
        return None
    order: list[str] = []
    by_id: dict[str, Mapping[str, Any]] = {}
    for item in items:
        item_id = _item_id(item)
        if not item_id or item_id in by_id:
            return None
        order.append(item_id)
        by_id[item_id] = item
    return order, by_id


def generation_of(value: Mapping[str, Any]) -> str:
    """The board generation a ``work`` render names, ``""`` when it names none."""
    conductor = value.get("conductor")
    if isinstance(conductor, Mapping):
        generation = conductor.get("generation")
        if isinstance(generation, str):
            return generation
    return ""


def snapshot_reason(before: Mapping[str, Any], after: Mapping[str, Any]) -> str:
    """Why *after* cannot be published as a delta on *before*, or ``""`` when it can.

    A generation change resets the whole board (``_work_reset_board``): the earlier
    board's items are gone and nothing a receiver holds is a base for the new one.
    """
    if generation_of(before) != generation_of(after):
        return REASON_GENERATION
    if _index(before) is None or _index(after) is None:
        return REASON_SHAPE
    return ""


def diff_work(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """The ops that turn render *before* into render *after*.

    The caller has checked :func:`snapshot_reason` is empty; a render this module cannot
    index raises ``ValueError`` rather than producing a delta that would apply wrong.
    """
    indexed_before = _index(before)
    indexed_after = _index(after)
    if indexed_before is None or indexed_after is None:
        raise ValueError("a work render without unique item ids has no delta")
    _, old = indexed_before
    order, new = indexed_after
    # Every top-level key but ``items``, so a key the render gains later travels too.
    header: dict[str, Any] = {key: part for key, part in after.items() if key != "items"}
    header["order"] = order
    return {
        "header": header,
        "items": {item_id: item for item_id, item in new.items() if old.get(item_id) != item},
        "remove": [item_id for item_id in old if item_id not in new],
    }


def apply_work(value: Mapping[str, Any], ops: Mapping[str, Any]) -> "dict[str, Any] | None":
    """*value* with *ops* applied, or ``None`` when the ops do not fit *value*.

    ``None`` is the receiver's cue to pull a snapshot. It is returned for malformed ops
    and for an ``order`` that names an id the receiver neither holds nor was sent. The
    input is never mutated: a receiver may hand the same base to several readers.
    """
    header = ops.get("header")
    items = ops.get("items")
    remove = ops.get("remove")
    if not isinstance(header, Mapping) or not isinstance(items, Mapping):
        return None
    if not isinstance(remove, list):
        return None
    order = header.get("order")
    if not isinstance(order, list):
        return None
    indexed = _index(value)
    if indexed is None:
        return None
    _, held = indexed
    gone = set(remove)
    result_items: list[Any] = []
    for item_id in order:
        if item_id in items:
            result_items.append(items[item_id])
        elif item_id in held and item_id not in gone:
            result_items.append(held[item_id])
        else:
            return None
    result: dict[str, Any] = {key: part for key, part in header.items() if key != "order"}
    result["items"] = result_items
    return result
