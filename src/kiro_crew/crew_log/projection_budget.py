"""The warm slot memo's byte budget: what one retained row of each slot and tree fold
costs, the operator ceiling, and what a cell is charged against it.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, Final

from kiro_crew.crew_log.projection_catalogue import TREE_PROJECTION_NAMES, WORKTREE_FOLD_NAME
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

# --------------------------------------------------------------------------- #
# A slot's folds on the projection kernel
# --------------------------------------------------------------------------- #

#: What ONE RETAINED ROW of each slot fold costs, measured rather than reasoned about:
#: the marginal bytes of the WIDEST row kind that fold keeps.
#:
#: PER ROW AND NOT PER CELL, which is the whole shape of this budget and not a detail.
#: A per-cell figure measured at the caps cannot bound these states, because three of
#: the four nest a capped list INSIDE each capped item -- so their cap-state is the
#: PRODUCT of two caps rather than the sum:
#:
#:   ``radar``   :data:`RADAR_ITEM_LIMIT` items x :data:`RADAR_TRIED_LIMIT` tried rows,
#:               and one ``phase_lines`` key per item x :data:`RADAR_PHASE_LINE_LIMIT`
#:   ``work``    :data:`WORK_ITEM_LIMIT` items x :data:`WORK_EVENT_LIMIT` events
#:   ``panel``   :data:`PANEL_OWNER_LIMIT` owners x :data:`PANEL_HISTORY_LIMIT` rows
#:   ``ledger``  flat
#:
#: So ``radar``'s cap-state is 156,500 rows, which at the figure below is 24,402 MiB --
#: not the 26 MiB a cell driven to its items and skips alone measures, and not the
#: 995,342 bytes the spec recorded before that. A static per-cell charge is therefore not
#: an upper bound at all, and a budget built on one is not a bound. The four cap-state
#: charges are ``radar`` 24,402 MiB, ``work`` 165.6 MiB, ``panel`` 10.1 MiB, ``ledger``
#: 2.8 MiB.
#:
#: HOW MEASURED. Each row kind is driven with its free-text fields at the clamp the FOLD
#: applies, in 4-byte UTF-8 characters, because the clamps count CHARACTERS and an
#: ASCII row is a quarter of the bytes the same clamp admits. The cost is
#: ``(bytes at N rows - bytes at 1 row) / (N - 1)`` of
#: ``len(json.dumps(state, ensure_ascii=False).encode("utf-8"))``, except ``radar``,
#: whose figure is the widest item weighed alone (each item brings narrow companion
#: rows that pull an average below it), and ``panel``, driven in ASCII: its row is one
#: entry's document kept whole, so the entry's byte cap binds, and ASCII fills it widest.
#: ``test_every_slot_folds_row_cost_is_derived_from_its_own_rows`` re-derives all four,
#: so a clamp raised without re-measuring fails CI rather than quietly invalidating this.
#:
#: ONE FIGURE PER FOLD, the widest of its own kinds, so a ``radar`` skip, tried or
#: progress row is charged as a whole item. That over-charges every cell but one made of
#: full items, which is the safe direction: the budget then holds fewer cells than its
#: bytes allow, never more.
_SLOT_FOLD_ROW_BYTES: Final[dict[str, int]] = {
    # a published document per owner, filling one entry (51,655 measured)
    "panel": 52_000,
    # a work item with every field it keeps at its clamp (163,464 measured)
    "radar": 163_500,
    # a tried row: two fields at LEDGER_TEXT_LIMIT (16,065 measured)
    "ledger": 16_100,
    # an item with title, summary and decision at their clamps; its acceptance and
    # artifacts have no clamp and are charged per container (``_work_opaque``)
    "work": 2_705,
    # one agentic field: its value at DASHBOARD_VALUE_BYTES, plus the field name,
    # the declared type, the stamp and the ownership digest
    "agentic": 8_400,
    # one mistake group: code, field and reason at their clamps, the ownership
    # digest, two stamps and the bounded correction list (772 measured)
    "mistakes": 1_100,
    # a workstreams task with title and summary at their clamps, beside the hourly
    # buckets a board and a spender each keep (measured below, see the test)
    "workstreams": 1_150,
    # a milestone row: text, target, current and its stamp at CREWMATE_TEXT_LIMIT
    "goal": 1_700,
    # a waiting row: kind, text, stamp and its options, each at CREWMATE_TEXT_LIMIT
    "waiting": 11_000,
}

#: Default ceiling on the bytes EVERY warm slot cell may hold together, across every
#: data home, slot and fold. 256 MiB.
#:
#: Chosen so that one fold at its declared caps still FITS and the one that cannot is the
#: one that must not: ``work`` at 256 items x 200 events is charged 165.6 MiB (133.6
#: for its rows, 32 for each item's ``acceptance`` and ``artifacts`` at the entry bound,
#: :func:`_work_opaque`), ``panel``
#: 10.1 MiB, ``ledger`` 2.8 MiB -- while ``radar`` at its caps is 24,402 MiB and is refused
#: rather than fitted (:func:`_remember_slot_fold`). A smaller default would start
#: refusing the biggest work board, which is the one a cold fold costs most.
#:
#: It is a ceiling on CHARGES, not on bytes allocated, and the charge runs about 4x over a
#: real cell's serialized size (one row cost covers the fold's widest row kind). So a full
#: table at this ceiling is nearer 60 MiB resident than 256.
#:
#: The ceiling it replaces admitted 64 cells of any size, so its own worst case was
#: 64 x 24,402 MiB, which no number in the code had to move to reach.
DEFAULT_SLOT_FOLD_CACHE_BYTES: Final[int] = 256 * 1024 * 1024

#: The variable an operator lowers the ceiling with, in BYTES. Read per call, so a test
#: or a small deployment can set it without a restart; an unparseable or non-positive
#: value leaves the default standing rather than switching the cache off, because a
#: typo'd ceiling must not silently cost every read a cold fold.
SLOT_FOLD_CACHE_BYTES_ENV: Final[str] = "KIROCREW_SLOT_FOLD_CACHE_BYTES"

#: Charged per row to a fold that declares no row cost, so an unmeasured fold cannot make
#: the budget unbounded by costing nothing. 64 KiB is :data:`MAX_ENTRY_BYTES`: no row can
#: hold more than the entry that carried it.
_UNMEASURED_ROW_BYTES: Final[int] = 64 * 1024


def slot_fold_cache_bytes() -> int:
    """The live ceiling on all warm slot cells together, in bytes.

    One accessor rather than a constant, because the variable is read per call and the
    eviction loop and every test that reports the budget must agree on the same number.
    """
    raw = os.environ.get(SLOT_FOLD_CACHE_BYTES_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return DEFAULT_SLOT_FOLD_CACHE_BYTES


#: What ONE RETAINED ROW of each TREE-keyed fold costs -- the third key kind's own
#: budget, measured the same way :data:`_SLOT_FOLD_ROW_BYTES` measures its members and
#: deliberately NOT folded into that table: these folds have no warm cell to charge, so
#: a figure sitting among the ones the eviction loop reads would describe a cell that
#: does not exist.
#:
#: WHAT IT IS FOR, given there is no cell. Two readers. A composing agent sizing a block
#: against a fold that retains rows reads it out of the catalog
#: (:attr:`~kiro_crew.dashboard_types.FoldType.row_bytes`), which is the whole reason the
#: figure has to be true rather than absent. And :func:`slot_fold_cell_bytes` charges a
#: fold with no entry in EITHER table :data:`_UNMEASURED_ROW_BYTES` -- 64 KiB a row --
#: so admitting a key kind here with no measured cost is what would make that budget
#: unbounded in practice the moment a tree fold ever did get a cell.
#:
#: PER ROW AND NOT PER CELL, for :data:`_SLOT_FOLD_ROW_BYTES`' reason, arriving here by
#: the other route. This fold keeps TWO row kinds and they ADD rather than nest --
#: ``boards`` (:data:`WORKTREE_BOARD_LIMIT`) and the item-to-board map
#: (:data:`WORKTREE_OWNER_LIMIT`) -- so its cap-state is 200 + 4,000 = 4,200 rows, which
#: at the figure below is 4.2 MiB. A per-cell figure still could not bound it: the two
#: caps are independent and the second is set by the RECORD's item count rather than by
#: anything a page shows, so one number measured at one shape would be right for one
#: tree and wrong for the next.
#:
#: HOW MEASURED, and against WHICH member. One figure per fold, the WIDEST of its own
#: kinds, which here is a BOARD row: it carries the clamped goal preview
#: (:data:`WORKTREE_GOAL_CHARS`) while an owner row is two bounded ids. The cost is
#: ``(bytes at N rows - bytes at 1 row) / (N - 1)`` of
#: ``len(json.dumps(state, ensure_ascii=False).encode("utf-8"))``, with every free-text
#: field driven at the clamp the FOLD applies and in 4-byte UTF-8 characters, because
#: the clamps count CHARACTERS and an ASCII row is a quarter of the bytes the same clamp
#: admits. Measured over 40 rows: a board row is **1,017 bytes** (1 row 1,100 B, 40 rows
#: 40,754 B) and an owner row is 41, so the board row is the figure and it over-charges
#: an owner row twenty-five-fold -- the safe direction, the one
#: :data:`_SLOT_FOLD_ROW_BYTES` takes for the same reason.
#: ``test_the_tree_folds_row_cost_is_derived_from_its_own_rows`` re-derives both kinds
#: from a driven fold, so a clamp raised without re-measuring fails CI rather than
#: quietly invalidating this.
_TREE_FOLD_ROW_BYTES: Final[dict[str, int]] = {
    # a board row with its goal preview at WORKTREE_GOAL_CHARS in 4-byte characters,
    # beside the slot key, the cited parent item, the depth and the generation
    # (1,017 measured)
    WORKTREE_FOLD_NAME: 1_050,
}


def slot_fold_row_bytes(name: str) -> int:
    """What one retained row of the *name* fold is charged. See :data:`_SLOT_FOLD_ROW_BYTES`."""
    if name in _SLOT_FOLD_ROW_BYTES:
        return _SLOT_FOLD_ROW_BYTES[name]
    return _TREE_FOLD_ROW_BYTES.get(name, _UNMEASURED_ROW_BYTES)


def slot_fold_cell_bytes(name: str, state: "Mapping[str, Any] | None" = None) -> int:
    """An upper bound on what *state* retains, charged against the ceiling.

    ``rows + 1`` rather than ``rows``: the extra row covers the fold's flat header fields
    -- a ``goal``, a ``next``, a crew id -- which belong to no row and which an empty cell
    holds anyway. It also means a cell is never charged zero.

    COUNTING, not serializing. The count is a handful of ``len`` calls plus one pass over
    the items (bounded by the fold's own item cap), where weighing the state itself would
    mean serializing up to 2.4 GiB on every store -- more than the fold that produced it.
    And counting is still an UPPER BOUND, because each row's own text is clamped by the
    fold, and a value the fold keeps whole is charged the entry that carried it
    (``count_opaque``): that is what a static per-cell figure could not be.

    *state* absent answers the one-row floor, which is what a caller asking about a fold
    rather than about a cell wants.
    """
    fold = _FOLDS.get(name)
    rows = 0
    opaque = 0
    if state is not None and fold is not None:
        if fold.count_rows is not None:
            rows = max(0, fold.count_rows(state))
        if fold.count_opaque is not None:
            opaque = max(0, fold.count_opaque(state))
    return (rows + 1) * slot_fold_row_bytes(name) + opaque * MAX_ENTRY_BYTES


#: THE THIRD KEY KIND'S OWN BUDGET GUARD, and the reason the kind could not simply be
#: admitted: a tree fold with no measured row cost falls through to
#: :data:`_UNMEASURED_ROW_BYTES` -- 64 KiB a row -- which is not a measurement and makes
#: any ceiling stated over it unbounded in practice. Both halves are required, because
#: either one alone is a constant: a row cost with no counter charges one row forever,
#: and a counter with no cost charges the fallback. Checked at import, like the slot
#: guard, because a registry the process cannot honour is not a thing to discover under
#: load.
_UNMEASURED_TREE_FOLDS: Final[tuple[str, ...]] = tuple(
    name
    for name in TREE_PROJECTION_NAMES
    if _FOLDS[name].count_rows is None or name not in _TREE_FOLD_ROW_BYTES
)
if _UNMEASURED_TREE_FOLDS:  # pragma: no cover - import-time
    raise RuntimeError(
        "a tree-keyed fold must declare BOTH count_rows and a measured row cost in "
        "_TREE_FOLD_ROW_BYTES, or its budget is a constant charged against an "
        f"unmeasured fallback: {list(_UNMEASURED_TREE_FOLDS)}"
    )
