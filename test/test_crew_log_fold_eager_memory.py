"""The memory the eager fold's warm memos hold.

Covers a closed session dropping its slot's eager memos, the byte budget (evicting back
to a cold fold, keeping more small cells than large ones, an advanced cell moving to the
back and outliving an older neighbour), how a cell's charge is derived (each slot fold's
per-row cost from its own rows, the rows nested inside each item, the values a work cell
keeps whole) and a cell above the whole ceiling: served but not kept, not folded again on
every wake, with a bounded record of refused cells. The isolated data home and the
slot-log helpers come from ``crew_log_fold_eager_helpers.py``.
"""

from __future__ import annotations

import json
import unittest.mock
from collections.abc import Mapping
from typing import Any

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    GATEWAY,
    WORK_SLOT,
    _isolated_home,
    _memo_keys,
    _slot_log,
    _work_entry,
)

from kiro_crew.crew_log import CrewLog, eager
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import RADAR_ENTRY_TYPE

# --------------------------------------------------------------------------- #
# 6. The memory the warm memos hold
# --------------------------------------------------------------------------- #


def test_a_closed_session_drops_the_slots_eager_memos():
    """``session/closed`` ends the reason to hold the slot warm -- for the EAGER folds.

    Two things this pins beyond the drop itself.

    IT IS SCOPED TO THE CLOSING SLOT. A closer is not always the end of a board: a slot
    reset, or one worker unit of a live crew, closes while the board goes on. Another
    board's cells are not this closer's to drop, and the case below holds one to prove it.

    The drop walks ``EAGER_FOLD_NAMES`` rather than passing a bare ``slot=``, and with
    every slot fold eager (the default) those two now reach the same cells. The loop stays
    because the RULE is "the closer drops what eager folding warmed", and a slot fold that
    ever goes back to lazy must not be taken by it.

    AND THE BYTE BUDGET IS NOT WHAT DOES IT. Eviction fires only once the ceiling is
    exceeded; under it nothing is removed, ever, and a closed board's cell is never
    re-stored so it never moves towards the back either. On any gateway holding less than
    :func:`slot_fold_cache_bytes` of cells -- the ordinary case -- that cell would sit
    there for the life of the process. The assertion below records how far under the
    ceiling the table was when the drop happened, so the budget cannot be the cause.
    """
    slot = WORK_SLOT
    handle = _slot_log("s-work-6", slot)
    seq = _work_entry(handle, "it-1")
    eager.note_commit("s-work-6", "work/recorded", seq)
    assert eager.drain(timeout=10.0)
    assert (slot, "work") in _memo_keys()
    # Another board's cell, which this closer has no business touching.
    other = "dashboard:kept"
    other_handle = _slot_log("s-work-6-other", other)
    _work_entry(other_handle, "it-1", slot=other)
    crew_log.fold_slot_warm("work", ("s-work-6-other",), slot=other)
    assert (other, "work") in _memo_keys(), "the neighbour cell this test is about is not warm"

    held_bytes = sum(memo.weight for memo in crew_log._slot_memos.values())
    assert held_bytes < crew_log.slot_fold_cache_bytes(), (
        f"{held_bytes:,} bytes held against a ceiling of "
        f"{crew_log.slot_fold_cache_bytes():,}: this case has to sit under the budget, or "
        "eviction could be what drops the cell rather than the closer"
    )

    closed = handle.append("session/closed", {"reason": "done"}, src=GATEWAY)
    eager.note_commit("s-work-6", "session/closed", int(closed.seq))
    assert eager.drain(timeout=10.0)

    assert (slot, "work") not in _memo_keys()
    assert (other, "work") in _memo_keys(), (
        f"the closer took another board's cell with it; warm = {sorted(_memo_keys())}. It "
        "drops the slot that closed, not every slot this process folded"
    )


def _one_item_cell_bytes(name: str, items: int = 1) -> int:
    """What a ``name`` cell holding *items* records is charged, read off a stored memo.

    Read off the memo rather than computed here, because the charge is
    ``(rows + 1) x row cost`` and the row count is the fold's own counter's to give.
    """
    slot = f"dashboard:weigh-{name}-{items}"
    unit = f"s-weigh-{name}-{items}"
    handle = _slot_log(unit, slot)
    if name == "panel":
        handle.append(
            "panel/published",
            {"crew_key": "c0", "template": "t", "title": "a", "data": {"k": "v"}},
            src=GATEWAY,
        )
    else:
        for index in range(items):
            _work_entry(handle, f"it-{index}", slot=slot)
    crew_log.fold_slot_warm(name, (unit,), slot=slot)
    # Matched on the FOLD as well as the slot: a ``work/recorded`` entry stores a memo
    # for every slot fold that reads the type, so the slot alone names several cells
    # and this would otherwise weigh whichever one came first.
    weight = next(
        memo.weight
        for key, memo in crew_log._slot_memos.items()
        if key[1] == slot and key[2] == name
    )
    crew_log.forget_slot_folds(slot=slot, name=name)
    return weight


def _work_cell_ceiling(monkeypatch, cells: int) -> None:
    """Set the byte ceiling to exactly *cells* one-item ``work`` cells, via the real variable.

    Through the ENV VAR rather than by patching a constant, because that is the knob an
    operator has and :func:`slot_fold_cache_bytes` reads it per call -- so a test that
    patched a module attribute instead would be pinning a path production does not take.
    """
    monkeypatch.setenv(
        crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(_one_item_cell_bytes("work") * cells)
    )


def test_the_byte_budget_evicts_back_to_a_cold_fold(monkeypatch):
    """Past the resident-byte ceiling the least recently stored memo goes.

    The memos are an optimization with no answer of their own, so the budget is spent on
    the slots being written now and an evicted one pays one refold.

    IN BYTES, not in cells, because the cells are not comparable: a ``radar`` cell at its
    declared caps is charged 24,402 MiB against a ``panel`` cell's 10.1 MiB, so a count
    ceiling priced a resident total across orders of magnitude with no number moving.
    """
    # One SLOT's worth of memos, which is what a ``work/recorded`` entry stores: every
    # slot fold that declares the type gets a cell, so a ceiling of one ``work`` cell
    # would measure which fold stored last rather than which slot was written last.
    woken = [
        name
        for name in crew_log.EAGER_SLOT_FOLD_NAMES
        if "work/recorded" in (crew_log._FOLDS[name].affects or ())
    ]
    assert "work" in woken, f"the work fold does not read its own type: {woken}"
    monkeypatch.setenv(
        crew_log.SLOT_FOLD_CACHE_BYTES_ENV,
        str(sum(_one_item_cell_bytes(name) for name in woken)),
    )
    slots = ["dashboard:20", "dashboard:21", "dashboard:22"]
    for index, slot in enumerate(slots):
        handle = _slot_log(f"s-budget-{index}", slot)
        seq = _work_entry(handle, f"it-{index}", slot=slot)
        eager.note_commit(f"s-budget-{index}", "work/recorded", seq, board=slot)
        assert eager.drain(timeout=10.0)

    kept = _memo_keys()
    assert {slot for slot, _fold in kept} == {slots[-1]}, (
        f"a budget of one slot's cells kept memos for {sorted({s for s, _ in kept})}; "
        f"only the newest slot {slots[-1]} should remain"
    )
    assert (slots[-1], "work") in kept
    # Correctness is untouched: the evicted slot still folds its own record.
    assert crew_log.read_slot_projection(slots[0], "work").value["items"]


def test_the_byte_budget_keeps_more_small_cells_than_large_ones(monkeypatch):
    """MUTATION-SENSITIVE: the charge follows each CELL's size, so cheap cells keep more.

    This is the behaviour a count ceiling could not express and the whole reason for the
    reversal. One budget, two kinds of cell: at a ceiling of four one-item ``work`` cells
    the same bytes hold more one-document ``panel`` cells when a panel cell is charged
    less, and fewer when it is charged more -- either way the counts DIFFER, where a
    table charging every cell the same holds four of each.
    """
    work_cell = _one_item_cell_bytes("work")
    panel_cell = _one_item_cell_bytes("panel")
    budget = work_cell * 4
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(budget))
    expected_panel = budget // panel_cell
    assert expected_panel != 4, (
        f"a panel cell is charged {panel_cell:,} against work's {work_cell:,}; this case "
        "needs them to differ enough that the two folds hold different counts"
    )

    for index in range(12):
        unit = f"s-small-{index}"
        slot = f"dashboard:small-{index}"
        handle = _slot_log(unit, slot)
        handle.append(
            "panel/published",
            {"crew_key": f"c{index}", "template": "t", "title": "a", "data": {"k": "v"}},
            src=GATEWAY,
        )
        crew_log.fold_slot_warm("panel", (unit,), slot=slot)
    panels = len(_memo_keys())

    crew_log.forget_slot_folds()
    for index in range(12):
        unit = f"s-big-{index}"
        slot = f"dashboard:big-{index}"
        handle = _slot_log(unit, slot)
        _work_entry(handle, "it-0", slot=slot)
        crew_log.fold_slot_warm("work", (unit,), slot=slot)
    works = len(_memo_keys())

    assert works == 4, f"four work cells fit the budget, {works} held"
    assert panels == expected_panel, (
        f"the same budget held {panels} panel cells where its bytes allow "
        f"{expected_panel}; the charge is not following each fold's own measured cost"
    )
    assert panels != works, (
        f"{panels} panel cells against {works} work cells: cells of different sizes must "
        "keep different counts out of one budget, which a count ceiling cannot do"
    )


def test_an_advanced_cell_moves_to_the_back_and_outlives_an_older_neighbour(monkeypatch):
    """MUTATION-SENSITIVE: a stored cell moves to the back, so the NEXT eviction skips it.

    The ceiling alone does not say this. At one cell every store evicts the other, so a
    table that appended without moving a re-stored cell would pass that test and still
    evict the cell most recently advanced -- which is the eviction order this change
    declares and the eager worker depends on: a board being written now is stored on every
    wake and must not be the one that goes.

    Three boards at a budget of two cells, and the discriminating step is the middle one:
    board A is advanced by a fresh entry AFTER B is folded, so with the move A sits behind
    B and C's arrival evicts B. Without it A is still at the front and C's arrival evicts A.

    The ceiling is A-after-its-second-item plus one one-item cell, because a cell is
    charged what it holds: A grows when it is advanced, and a ceiling of two one-item
    cells would then evict for size rather than for order.
    """
    ceiling = _one_item_cell_bytes("work", items=2) + _one_item_cell_bytes("work")
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, str(ceiling))
    boards = {}
    for index, slot in enumerate(("dashboard:30", "dashboard:31")):
        unit = f"s-lru-{index}"
        handle = _slot_log(unit, slot)
        _work_entry(handle, "it-0", slot=slot)
        boards[slot] = (unit, handle)
        crew_log.fold_slot_warm("work", (unit,), slot=slot)

    first, second = tuple(boards)
    # A store, not merely a read: the cell is behind the file, so the pass carries it
    # forward and records a new memo. A read that found it already current stores nothing.
    unit, handle = boards[first]
    _work_entry(handle, "it-1", slot=first)
    crew_log.fold_slot_warm("work", (unit,), slot=first)

    third = "dashboard:32"
    third_unit = "s-lru-2"
    third_handle = _slot_log(third_unit, third)
    _work_entry(third_handle, "it-0", slot=third)
    crew_log.fold_slot_warm("work", (third_unit,), slot=third)

    held = _memo_keys()
    assert len(held) == 2, f"a two-cell budget should hold two memos, holds {sorted(held)}"
    assert (third, "work") in held, "the newest fold is not held at all"
    assert (first, "work") in held, (
        f"the cell advanced most recently was evicted; held = {sorted(held)}. A store has to "
        "move its cell to the back, or the board being written now is the one that goes"
    )
    assert (second, "work") not in held, (
        f"the untouched older cell survived; held = {sorted(held)} -- eviction is not "
        "following the order this change claims"
    )


def _state_bytes(name: str, unit: str, slot: str) -> int:
    """The warm cell for *name*, weighed the way the recorded row costs were weighed."""
    folded = crew_log.fold_slot_warm(name, (unit,), slot=slot)
    return len(json.dumps(folded.state, ensure_ascii=False).encode("utf-8"))


#: Free text past every clamp a fold applies, so each field lands AT the fold's own clamp
#: -- the only width a per-row cost may be measured at. In the widest UTF-8 character
#: (4 bytes), because the folds clamp by CHARACTERS: an ASCII row is a quarter of the
#: bytes the same clamp admits.
_WIDE = "\U0001f600" * 6000

#: The one exception: a ``panel`` row is one entry's document kept whole, so the entry's
#: own byte cap binds rather than a character clamp. An entry stores a non-ASCII
#: character escaped (up to 12 bytes) where the state holds at most 4, so ASCII, at one
#: byte in both, is the text that fills that row widest.
_WIDE_ASCII = "n" * 6000

#: Rows driven to measure a marginal cost. Enough that the per-cell header is noise.
_ROWS = 40


def _append_row(handle: CrewLog, name: str, slot: str, index: int) -> None:
    """One row of *name*'s WIDEST kind, every text field at the fold's clamp."""
    if name == "ledger":
        handle.append(
            "ledger/recorded",
            {
                "slot": slot,
                "tried": {
                    "approach": f"{index:04d}{_WIDE}"[: crew_log.LEDGER_TEXT_LIMIT],
                    "rejected_because": _WIDE[: crew_log.LEDGER_TEXT_LIMIT],
                },
            },
            src=GATEWAY,
        )
    elif name == "work":
        handle.append(
            "work/recorded",
            {
                "slot": slot,
                "actor": "conductor",
                "by": slot,
                "action": "create",
                "item_id": f"it-{index:06d}",
                "title": _WIDE[:200],
                "summary": _WIDE[:500],
                "decision": _WIDE[:2000],
                "acceptance": {"kind": "pr_checks", "pr": index + 1, "repo": _WIDE[:120]},
            },
            src=GATEWAY,
        )
    elif name == "panel":
        handle.append(
            "panel/published",
            {
                "crew_key": f"{_WIDE_ASCII[: crew_log.PANEL_CREW_KEY_LIMIT - 4]}{index:04d}",
                "crew": _WIDE_ASCII[: crew_log.PANEL_TITLE_LIMIT],
                "template": _WIDE_ASCII[: crew_log.PANEL_TEMPLATE_LIMIT],
                "title": _WIDE_ASCII[: crew_log.PANEL_TITLE_LIMIT],
                "data": {f"k{key:03d}": _WIDE_ASCII[:200] for key in range(240)},
            },
            src=GATEWAY,
        )
    elif name == "agentic":
        # The WIDEST agentic row: a distinct field name per entry (so each adds a
        # counted row rather than replacing one) carrying a value at the fold's own
        # per-value byte ceiling. A string is the widest shape that reaches the
        # ceiling with no structural overhead.
        handle.append(
            "dashboard/agentic_value",
            {
                "field": f"f{index:06d}",
                "type": "string",
                # Built to the clamp rather than sliced from _WIDE_ASCII, which is
                # shorter than this fold's per-value ceiling: a slice would measure
                # the string's length instead of the fold's limit, and the recorded
                # charge has to cover the widest row the fold can really retain.
                # Exactly AT the clamp once serialized: the fold measures the value's
                # JSON form, so a bare string of the cap's length is two bytes over
                # (the quotes) and is dropped -- which is how an earlier version of
                # this fixture added no counted rows at all.
                "value": {"v": "n" * (crew_log.DASHBOARD_VALUE_BYTES - 2)},
                "instance_version": index + 1,
                "crew_key": f"{_WIDE_ASCII[: crew_log.PANEL_CREW_KEY_LIMIT - 4]}{index:04d}",
            },
            src=GATEWAY,
        )
    elif name == "goal":
        # A SNAPSHOT fold: each entry replaces the whole record, so the Nth entry carries
        # N milestones (the record grew) for each to add a counted row, every text cell
        # at the fold's own clamp. ASCII, for the panel's reason: the state is one
        # entry kept whole, so the entry's own byte cap binds before a wide character
        # clamp could.
        cell = _WIDE_ASCII[: crew_log.CREWMATE_TEXT_LIMIT]
        row = {"text": cell, "target": cell, "current": cell, "met": True, "at": cell}
        handle.append(
            "dashboard/goal_recorded",
            {
                "goals": [],
                "milestones": [
                    dict(row) for _ in range(min(index + 1, crew_log.CREWMATE_ROW_LIMIT))
                ],
            },
            src=GATEWAY,
        )
    elif name == "waiting":
        # The same posture: the Nth snapshot lists N items, each with its options at
        # the fold's limit and every text cell at its clamp, in ASCII as above.
        cell = _WIDE_ASCII[: crew_log.CREWMATE_TEXT_LIMIT]
        item = {
            "kind": cell,
            "text": cell,
            "options": [cell] * crew_log.CREWMATE_OPTION_LIMIT,
            "asked_at": cell,
        }
        handle.append(
            "dashboard/waiting_changed",
            {"items": [dict(item) for _ in range(min(index + 1, crew_log.CREWMATE_WAITING_LIMIT))]},
            src=GATEWAY,
        )
    elif name == "mistakes":
        # The WIDEST mistake row: a distinct field per entry so each mints its own
        # group, with the code, field and reason each at their clamp.
        handle.append(
            "dashboard/agentic_refused",
            {
                "code": _WIDE_ASCII[: crew_log.DASHBOARD_CODE_LIMIT],
                "field": f"{_WIDE_ASCII[: crew_log.DASHBOARD_FIELD_LIMIT - 6]}{index:06d}",
                "reason": _WIDE_ASCII[: crew_log.DASHBOARD_REASON_LIMIT],
                "crew_key": f"{_WIDE_ASCII[: crew_log.PANEL_CREW_KEY_LIMIT - 4]}{index:04d}",
            },
            src=GATEWAY,
        )
    elif name == "workstreams":
        _append_workstreams_task(handle, slot, index)
    else:
        _append_radar_item(handle, index)


def _append_workstreams_task(handle: CrewLog, slot: str, index: int) -> None:
    """One ``workstreams`` task with every field this fold keeps at its clamp, plus
    the spender row and hourly buckets that task's own worker adds.

    Several entries, because one cannot carry them all: the conductor's ``create``
    and ``bind`` set the title and the worker key, the worker's ``report`` sets the
    summary, and only the worker's own ``turn/completed`` can mint the spend row the
    task is costed from. The item is the widest row this fold keeps -- narrower than
    ``work``'s by the ``acceptance``, ``artifacts``, ``decision`` and event tail it
    deliberately does not retain.
    """
    worker = f"chat-{index:06d}-worker"
    base = {"slot": slot, "by": slot}
    item = f"it-{index:06d}"
    handle.append(
        "work/recorded",
        {**base, "actor": "conductor", "action": "create", "item_id": item, "title": _WIDE[:200]},
        src=GATEWAY,
    )
    handle.append(
        "work/recorded",
        {
            **base,
            "actor": "conductor",
            "action": "bind",
            "item_id": item,
            "worker_session_key": worker,
        },
        src=GATEWAY,
    )
    handle.append(
        "work/recorded",
        {
            **base,
            "actor": "worker",
            "action": "report",
            "item_id": item,
            "summary": _WIDE[:500],
            "pr": index + 1,
        },
        src=GATEWAY,
    )
    # The worker's own unit, which is where the charge that costs this task lives.
    handle.append(
        "session/opened",
        {
            "agent": "worker",
            "slot": worker,
            "model": "opus",
            "cwd": "/w",
            "owner": "",
            "resumed": False,
        },
        src=GATEWAY,
    )
    handle.append(
        "turn/completed",
        {"turn": index + 1, "stop_reason": "end_turn", "duration_ms": 1_000, "credits": 1.5},
        src=GATEWAY,
    )


def _append_radar_item(handle: CrewLog, index: int) -> None:
    """One ``radar`` item with EVERY field it keeps at its clamp, one field per entry.

    No single entry can carry them all: at 4 bytes a character, each free-text field is
    most of an entry's byte cap on its own. The item is the widest row this fold keeps;
    its companion rows (the progress line each entry adds, a phase line, the update
    digest) are far narrower.
    """
    limit = crew_log.RADAR_TEXT_LIMIT
    base = {"crew_id": _WIDE[:64], "number": index + 1, "event_kind": "investigate", "event": "e"}
    # Every entry names the repository, but only the first one's is kept (and copied
    # into each item), so only that one carries it at its clamp.
    wide = _WIDE[:256] if index == 0 else "o"
    handle.append(
        RADAR_ENTRY_TYPE, {**base, "owner": wide, "repo": wide, "phase": "resolved"}, src=GATEWAY
    )
    base = {**base, "owner": "o", "repo": "o"}
    fields: list[dict[str, Any]] = [
        {name: _WIDE[:limit]}
        for name in ("decision", "why", "next", "worktree", "branch", "base_sha", "outcome")
    ]
    fields.append({"labels_applied": [_WIDE[:256]] * crew_log.RADAR_LABELS_LIMIT})
    fields.append({"ci_state": {"state": _WIDE[:32], "passed": 1, "total": 1, "round": 1}})
    for field in fields:
        handle.append(RADAR_ENTRY_TYPE, {**base, **field}, src=GATEWAY)


def _widest_radar_item(state: Mapping[str, Any]) -> int:
    """The serialized bytes of the widest ``radar`` item *state* holds."""
    return max(
        len(json.dumps(item, ensure_ascii=False).encode("utf-8"))
        for item in state["items"].values()
    )


@pytest.mark.parametrize("name", crew_log.SLOT_PROJECTION_NAMES)
def test_every_slot_folds_row_cost_is_derived_from_its_own_rows(name):
    """MUTATION-SENSITIVE: each recorded per-row charge is re-measured, not asserted.

    The budget charges a cell ``(rows + 1) x`` its fold's row cost, so every figure in
    ``_SLOT_FOLD_ROW_BYTES`` is load bearing: too low and the ceiling admits more bytes
    than it says. The cost is the MARGINAL bytes per counted row of the fold's widest
    kind, ``(bytes at N entries - bytes at 1) / (rows at N - rows at 1)``, with every text
    field at the fold's own clamp; a clamp raised without re-measuring fails here.

    Two bounds. The charge must be AT LEAST the measured cost, and a whole driven cell
    must weigh no more than it is charged, or the budget is not a bound. And it must be
    within 10x, so a figure left far above the truth after a clamp is LOWERED fails too:
    one figure per fold covers its widest row kind, so an average over mixed kinds sits
    below it by design (``radar``'s skip and event rows, ``work``'s event tail).
    """
    slot = f"dashboard:row-{name}"
    unit = f"s-row-{name}"
    handle = _slot_log(unit, slot)
    count = crew_log._FOLDS[name].count_rows
    _append_row(handle, name, slot, 0)
    one = _state_bytes(name, unit, slot)
    one_rows = count(crew_log.fold_slot_warm(name, (unit,), slot=slot).state)
    for index in range(1, _ROWS):
        _append_row(handle, name, slot, index)
    many = _state_bytes(name, unit, slot)
    many_rows = count(crew_log.fold_slot_warm(name, (unit,), slot=slot).state)
    assert many_rows > one_rows, f"the {name} fixture added no counted rows"
    measured = (many - one) / (many_rows - one_rows)
    recorded = crew_log.slot_fold_row_bytes(name)
    state = crew_log.fold_slot_warm(name, (unit,), slot=slot).state
    if name == "radar":
        # Each item brings narrow companion rows, so the average sits far below the
        # item; the figure must cover the item itself.
        measured = _widest_radar_item(state)
    assert many <= crew_log.slot_fold_cell_bytes(name, state), (
        f"a driven {name} cell weighs {many:,} bytes and is charged "
        f"{crew_log.slot_fold_cell_bytes(name, state):,}: the charge is not an upper bound"
    )
    assert measured <= recorded <= 10 * measured, (
        f"one {name} row measures {measured:,.0f} bytes ({many_rows - one_rows} rows over "
        f"{_ROWS - 1} entries) against the recorded {recorded:,}; the byte budget charges "
        "every row of this fold that figure -- re-measure and update _SLOT_FOLD_ROW_BYTES "
        "and docs/system-specs/modules/crew-log-projection.md"
    )


def test_a_cells_charge_counts_the_rows_nested_inside_each_item():
    """MUTATION-SENSITIVE: ``count_rows`` walks INTO each item, not just the top level.

    A work item carries its own event tail, and that nesting is why a per-cell figure
    could not bound these states. A counter that stopped at ``items`` would charge a
    board with one item and two hundred events as one row.
    """
    slot = "dashboard:nested"
    unit = "s-nested"
    handle = _slot_log(unit, slot)
    _work_entry(handle, "it-0", slot=slot)
    flat = crew_log.fold_slot_warm("work", (unit,), slot=slot)
    flat_rows = crew_log._FOLDS["work"].count_rows(flat.state)
    for _ in range(10):
        handle.append(
            "work/recorded",
            {
                "slot": slot,
                "actor": "conductor",
                "by": slot,
                "action": "decide",
                "item_id": "it-0",
                "decision": "d",
                "event_kind": "decision",
                "event": "e",
            },
            src=GATEWAY,
        )
    deep = crew_log.fold_slot_warm("work", (unit,), slot=slot)
    events = len(deep.state["items"]["it-0"]["events"])
    assert events > 1, "the fixture added no events to the item, so this measures nothing"
    assert crew_log._FOLDS["work"].count_rows(deep.state) >= flat_rows + events - 1
    assert crew_log.slot_fold_cell_bytes("work", deep.state) > crew_log.slot_fold_cell_bytes(
        "work", flat.state
    )


def test_a_work_cell_is_charged_for_the_values_it_keeps_whole():
    """MUTATION-SENSITIVE: ``acceptance``, ``artifacts`` and parked entries have no clamp.

    The store caps every work field but ``acceptance``, and the fold keeps it, an
    ``artifacts`` map and each parked entry WHOLE -- so a near-limit one weighs far more
    than the 2,705-byte row cost. Drop ``count_opaque`` from the ``work`` fold and these
    cells weigh more than they are charged.
    """
    slot = "dashboard:opaque"
    unit = "s-opaque"
    handle = _slot_log(unit, slot)
    bulk = "a" * 40_000
    handle.append(
        "work/recorded",
        {
            "slot": slot,
            "actor": "conductor",
            "by": slot,
            "action": "create",
            "item_id": "it-0",
            "title": "t",
            "acceptance": {"kind": "human_approval", "note": bulk},
        },
        src=GATEWAY,
    )
    handle.append(
        "work/recorded",
        {
            "slot": slot,
            "actor": "worker",
            "by": slot,
            "action": "report",
            "item_id": "it-0",
            "status": "progress",
            "artifacts": {"log": bulk},
        },
        src=GATEWAY,
    )
    # An entry for an item the board has not seen is parked whole.
    handle.append(
        "work/recorded",
        {
            "slot": slot,
            "actor": "worker",
            "by": slot,
            "action": "report",
            "item_id": "it-unseen",
            "status": "progress",
            "summary": "s",
            "artifacts": {"log": bulk},
        },
        src=GATEWAY,
    )
    state = crew_log.fold_slot_warm("work", (unit,), slot=slot).state
    assert state["items"]["it-0"]["acceptance"]["note"] == bulk
    assert state["items"]["it-0"]["artifacts"] == {"log": bulk}
    assert state["parked"]["it-unseen"], "the fixture parked nothing, so this measures nothing"
    weighed = _state_bytes("work", unit, slot)
    assert weighed > 3 * len(bulk)
    assert weighed <= crew_log.slot_fold_cell_bytes("work", state), (
        f"a work cell weighs {weighed:,} bytes and is charged "
        f"{crew_log.slot_fold_cell_bytes('work', state):,}: its whole-kept values are not charged"
    )
    assert crew_log._FOLDS["work"].count_opaque(state) == 3


def test_a_cell_above_the_whole_ceiling_is_served_but_not_kept(monkeypatch):
    """A cell that cannot fit is refused rather than parked or made room for.

    Parking it would hold its bytes until the next store; evicting everything else first
    would empty the table for a cell that still does not fit. The read still answers.
    """
    slot = "dashboard:huge"
    unit = "s-huge"
    handle = _slot_log(unit, slot)
    _work_entry(handle, "it-0", slot=slot)
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, "1")
    folded = crew_log.fold_slot_warm("work", (unit,), slot=slot)
    assert folded.state["items"], "the read must still answer"
    assert (slot, "work") not in _memo_keys()


def test_a_wake_for_a_cell_above_the_ceiling_performs_no_fold(monkeypatch):
    """MUTATION-SENSITIVE: a cell the table refused is not folded again on every wake.

    A cell whose charge is above the whole ceiling (``radar`` at its caps) cannot be kept,
    so an eager pass for it is a cold fold of every unit the slot ran under, thrown away.
    After the first refusal the worker leaves it to the read path, which still answers.
    """
    slot = "dashboard:oversize"
    unit = "s-oversize"
    handle = _slot_log(unit, slot)
    _work_entry(handle, "it-0", slot=slot)
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, "1")
    # Every slot fold that reads the type is woken by one entry, so each one's refusal
    # has to be on record before the wake -- a fold with no refusal recorded would be
    # folded here and the spy would count its read instead of ``work``'s.
    woken = [
        name
        for name in crew_log.EAGER_SLOT_FOLD_NAMES
        if "work/recorded" in (crew_log._FOLDS[name].affects or ())
    ]
    assert "work" in woken, f"the work fold does not read its own type: {woken}"
    for name in woken:
        crew_log.read_slot_projection(slot, name)
        assert crew_log.slot_fold_over_ceiling(slot, name), f"{name}'s refusal was not recorded"

    seq = _work_entry(handle, "it-1", slot=slot)
    with unittest.mock.patch.object(
        crew_log, "read_slot_projection", wraps=crew_log.read_slot_projection
    ) as spy:
        eager.note_commit(unit, "work/recorded", seq, board=slot)
        assert eager.drain(timeout=10.0)
    assert spy.call_count == 0, "the worker folded a cell it could not keep"
    assert [
        item["item_id"] for item in crew_log.read_slot_projection(slot, "work").value["items"]
    ] == [
        "it-0",
        "it-1",
    ]

    # A ceiling raised above the cell's charge lets the next wake advance it again.
    monkeypatch.delenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV)
    assert not crew_log.slot_fold_over_ceiling(slot, "work")


def test_the_refused_cell_record_is_bounded(monkeypatch):
    """The over-ceiling record keeps at most ``OVERSIZE_SLOT_CELL_LIMIT`` cells, oldest out."""
    monkeypatch.setattr(crew_log, "OVERSIZE_SLOT_CELL_LIMIT", 2)
    monkeypatch.setenv(crew_log.SLOT_FOLD_CACHE_BYTES_ENV, "1")
    slots = [f"dashboard:refused-{index}" for index in range(3)]
    for index, slot in enumerate(slots):
        handle = _slot_log(f"s-refused-{index}", slot)
        _work_entry(handle, "it-0", slot=slot)
        crew_log.read_slot_projection(slot, "work")
    held = {key[1] for key in crew_log._oversize_slot_cells}
    assert held == set(slots[1:]), f"held {sorted(held)}; the oldest refusal must go first"
    assert not crew_log.slot_fold_over_ceiling(slots[0], "work")
