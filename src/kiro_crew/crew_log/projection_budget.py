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

#: What ONE RETAINED ROW of each slot fold costs, measured: the marginal bytes of the
#: WIDEST row kind that fold keeps.
#:
#: PER ROW, NOT PER CELL. Three of the four nest a capped list INSIDE each capped item, so
#: their cap-state is the PRODUCT of two caps and no per-cell figure can bound it:
#:
#:   ``radar``   :data:`RADAR_ITEM_LIMIT` items x :data:`RADAR_TRIED_LIMIT` tried rows,
#:               and one ``phase_lines`` key per item x :data:`RADAR_PHASE_LINE_LIMIT`
#:   ``work``    :data:`WORK_ITEM_LIMIT` items x :data:`WORK_EVENT_LIMIT` events
#:   ``panel``   :data:`PANEL_OWNER_LIMIT` owners x :data:`PANEL_HISTORY_LIMIT` rows
#:   ``ledger``  flat
#:
#: Cap-state charges: ``radar`` 156,500 rows = 24,402 MiB, ``work`` 165.6 MiB, ``panel``
#: 10.1 MiB, ``ledger`` 2.8 MiB.
#:
#: HOW MEASURED. Free-text fields at the FOLD's clamp in 4-byte UTF-8 characters (clamps
#: count CHARACTERS). Cost is ``(bytes at N rows - bytes at 1 row) / (N - 1)`` of
#: ``len(json.dumps(state, ensure_ascii=False).encode("utf-8"))``, except ``radar`` (the
#: widest item weighed alone; its narrow companion rows would pull an average down) and
#: ``panel`` (ASCII: its row is one entry kept whole, so the entry byte cap binds).
#: ``test_every_slot_folds_row_cost_is_derived_from_its_own_rows`` re-derives all four,
#: so a clamp raised without re-measuring fails CI.
#:
#: ONE FIGURE PER FOLD, its widest kind, so smaller rows are over-charged: the budget
#: holds fewer cells than its bytes allow, never more.
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
    # one custom record: its data at CUSTOM_RECORD_BYTES, plus the type name, the
    # stamp, the seq, and the writer, member and ownership cells at their clamps
    "custom": 8_900,
    # a worker row with every text cell at its clamp -- slot and stop reason at
    # TEXT_LIMIT, task, agent, last line, report and close reason at
    # WORKERS_TEXT_LIMIT -- serializes to 2,034 bytes; a one-unit fixture can only
    # drive the crewmate's own creates, so the figure is set from that widest row
    "workers": 2_100,
    # one worker's row: the same widest row ``workers`` is measured from, and the
    # parent slot it was opened under at TEXT_LIMIT
    "worker": 2_400,
}

#: Default ceiling on the bytes EVERY warm slot cell may hold together, across every
#: data home, slot and fold. 256 MiB.
#:
#: Sized so every fold at its caps FITS except ``radar`` (24,402 MiB), which is refused
#: (:func:`_remember_slot_fold`): ``work`` at 256 items x 200 events is 165.6 MiB (133.6
#: for rows, 32 for ``acceptance`` and ``artifacts``, :func:`_work_opaque`), ``panel``
#: 10.1 MiB, ``ledger`` 2.8 MiB. Smaller would refuse the biggest work board, the
#: costliest to cold-fold.
#:
#: A ceiling on CHARGES, which run about 4x a cell's serialized size, so a full table is
#: nearer 60 MiB resident.
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


#: What ONE RETAINED ROW of each TREE-keyed fold costs, measured as
#: :data:`_SLOT_FOLD_ROW_BYTES` is. Kept OUT of that table because these folds have no
#: warm cell, and the eviction loop reads that table.
#:
#: Two readers: a composing agent sizing a block reads it from the catalog
#: (:attr:`~kiro_crew.dashboard_types.FoldType.row_bytes`), and
#: :func:`slot_fold_cell_bytes` would otherwise charge an unlisted fold
#: :data:`_UNMEASURED_ROW_BYTES` (64 KiB a row) if a tree fold ever got a cell.
#:
#: PER ROW, NOT PER CELL: the two row kinds ADD -- ``boards``
#: (:data:`WORKTREE_BOARD_LIMIT`) plus the item-to-board map
#: (:data:`WORKTREE_OWNER_LIMIT`), 200 + 4,000 = 4,200 rows, 4.2 MiB at the figure below --
#: and the second cap is set by the RECORD's item count, so no one-shape figure fits.
#:
#: The figure is a BOARD row, the widest kind (it carries the clamped goal preview,
#: :data:`WORKTREE_GOAL_CHARS`), measured as above in 4-byte characters over 40 rows:
#: **1,017 bytes** (1 row 1,100 B, 40 rows 40,754 B). An owner row is 41, so it is
#: over-charged, the safe direction.
#: ``test_the_tree_folds_row_cost_is_derived_from_its_own_rows`` re-derives both kinds, so
#: a clamp raised without re-measuring fails CI.
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
