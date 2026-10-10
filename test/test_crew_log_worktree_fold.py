"""The ``worktree`` fold: the tree of work BOARDS, under the THIRD key kind.

What this closes. A v3 dashboard block binds a fold NAME plus a dotted path. The tree
placement rules lived in
:func:`~kiro_crew.crew_log.session_tree.fold_citations`, a pure function, so however
correct they were no block could reach a tree: a block has no way to call a function.
This fold is a registered name over those same rules, and the tests below are in three
groups for the three things that had to be true at once.

1. THE KEY KIND. ``session`` and ``slot`` were the whole vocabulary, and every consumer
   -- the manifest's binding gate, the bus scope, the catalog's ``keyed_by``, the
   generated catalogue's ``mode`` -- routed on exactly two sets. A third kind that
   degraded to one of those would be the dangerous failure, not an error: a composing
   agent would key a whole fleet's tree by one conversation and get a complete-looking
   wrong answer. So the partition is pinned in both directions, at every consumer.

2. THE MEASURED ROW COST. The data line stopped at this fold for one reason: a key kind
   with no measured row cost charges :data:`~kiro_crew.crew_log.projection.
   _UNMEASURED_ROW_BYTES`, which is not a measurement, so any ceiling stated over it is
   unbounded in practice. The cost here is RE-DERIVED from a driven fold rather than
   asserted, both row kinds, with the same two bounds the slot table's own test applies.

3. THE PLACEMENT, which is NOT this fold's and is tested as such. The orphan and cycle
   handling is ``fold_citations``' and is shared with the session tree; what belongs to
   this fold is the WORK part -- that an edge is recorded as an ITEM id and has to be
   resolved to the board that created that item -- so the tests drive the edge cases
   through the item indirection, which is where this fold can get them wrong.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import dashboard_feed
from kiro_crew import dashboard_types as dt
from kiro_crew.crew_log import projection
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.crew_log.errors import CrewLogError
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.session_tree import fold_citations
from kiro_crew.dashboard_templates import manifest

FOLD = projection.WORKTREE_FOLD_NAME
ROOT = "chat-1-root"
MID = "chat-2-mid"
LEAF = "chat-3-leaf"

#: 2026-10-03T12:00:00Z.
NOON = 1_767_441_600_000

#: Free text past every clamp this fold applies, in the WIDEST UTF-8 character (4
#: bytes), because the clamp counts CHARACTERS: an ASCII row is a quarter of the bytes
#: the same clamp admits, so a cost measured on one would under-charge by four.
WIDE = "\U0001f600" * (projection.WORKTREE_GOAL_CHARS * 4)

_seq = iter(range(1, 1_000_000))


def _work(
    slot: str,
    action: str = "goal",
    *,
    actor: str = "conductor",
    time: int = NOON,
    **data: Any,
) -> Entry:
    return Entry(
        type=WORK_ENTRY_TYPE,
        seq=next(_seq),
        time=time,
        src="test",
        data={"slot": slot, "actor": actor, "action": action, **data},
    )


def _fold(entries: list[Entry], root: str = ROOT) -> dict[str, Any]:
    """*entries* folded from nothing, bound to *root* the way a reader asks."""
    start = projection.initial(FOLD)
    bind = projection._FOLDS[FOLD].bind_slot
    assert bind is not None
    bind(start.state, root)
    return projection.projection_of(projection.advance(start, entries)).value


def _state(entries: list[Entry], root: str = ROOT) -> dict[str, Any]:
    start = projection.initial(FOLD)
    bind = projection._FOLDS[FOLD].bind_slot
    assert bind is not None
    bind(start.state, root)
    return projection.advance(start, entries).state


def _row(value: dict[str, Any], board: str) -> dict[str, Any]:
    matched = [row for row in value["boards"] if row["board"] == board]
    assert len(matched) == 1, f"{board!r} not in {[r['board'] for r in value['boards']]}"
    return matched[0]


def _hang(child: str, under_item: str, in_board: str, *, depth: int = 1) -> list[Entry]:
    """The two entries that hang *child* under *under_item* of *in_board*.

    An edge takes BOTH: the parent board's create, which is what says which board owns
    the item, and the child board's own goal write, which is what cites it. They are in
    different logs in a real record, which is the whole reason the fold needs a map.
    """
    return [
        _work(in_board, "create", item_id=under_item, title="an item"),
        _work(child, "goal", goal=f"{child}'s goal", parent_item=under_item, depth=depth),
    ]


# --------------------------------------------------------------------------- #
# 1. The third key kind
# --------------------------------------------------------------------------- #


def test_the_fold_is_registered_under_the_third_key_kind() -> None:
    """Registered, and in the TREE set only.

    A tree joins the logs of many slots, so neither of the first two kinds describes it:
    ``session`` reads one log and ``slot`` joins the units of one slot. Being in the
    wrong set is not caught by a type -- every consumer just routes it elsewhere.
    """
    assert FOLD in projection.FOLD_NAMES
    assert FOLD in projection.TREE_PROJECTION_NAMES
    assert FOLD not in projection.SESSION_FOLD_NAMES
    assert FOLD not in projection.SLOT_PROJECTION_NAMES


def test_the_three_key_kinds_partition_the_registry() -> None:
    """Exactly one kind per fold. The import-time guard's own property.

    A name in two sets is routed one way by the manifest gate and another by the
    catalog; a name in none is registered and unreachable. Neither raises at the point
    of use, so both are pinned here as well as at import.
    """
    families = (
        set(projection.SESSION_FOLD_NAMES),
        set(projection.SLOT_PROJECTION_NAMES),
        set(projection.TREE_PROJECTION_NAMES),
    )
    for name in projection.FOLD_NAMES:
        assert sum(name in family for family in families) == 1, name
    assert set().union(*families) == set(projection.FOLD_NAMES)


def test_the_fold_is_lazy_with_a_stated_reason_and_no_warm_path() -> None:
    """LAZY, and the only lazy fold in the registry.

    Eager would mean a warm cell, and a tree cell is stale when ANY member's log grows
    -- including a member that was not in the closure when the cell was built. No
    publisher raises that event, so an eager tree fold would wake on the root's own log
    and then serve a tree missing every change made in a child's.
    """
    fold = projection._FOLDS[FOLD]
    assert fold.mode == "lazy"
    assert fold.lazy_reason.strip()
    assert FOLD in projection.LAZY_FOLD_REASONS
    assert FOLD not in projection.EAGER_FOLD_NAMES
    assert FOLD not in projection.EAGER_SLOT_FOLD_NAMES
    assert FOLD not in projection.EAGER_SESSION_FOLD_NAMES


def test_a_block_can_bind_the_tree_by_name() -> None:
    """THE GAP, closed: the binding gate admits the fold name.

    ``manifest.FOLD_NAMES`` is what decides whether a block's ``source.fold`` validates,
    and it is the union of the kernel's key-kind sets. Before the third set was in that
    union a tree block was refused however correct ``fold_citations`` was.
    """
    assert FOLD in manifest.FOLD_NAMES
    assert manifest.FOLD_NAMES == (
        frozenset(projection.SESSION_FOLD_NAMES)
        | frozenset(projection.SLOT_PROJECTION_NAMES)
        | frozenset(projection.TREE_PROJECTION_NAMES)
    )


def test_a_v3_package_block_binding_the_tree_validates() -> None:
    """END TO END on the binding half: a real manifest, parsed by the real loader.

    Two fields, because the two halves of a tree block fail differently: one binds the
    board list (the rows a renderer walks) and one binds a scalar beside it. A path this
    fold does not render is still refused, which is what proves the acceptance is about
    THIS fold's shape rather than about any string getting through.
    """
    parsed = manifest.parse_manifest(
        {
            "id": "fleet-tree",
            "version": 1,
            "title": "Fleet",
            "description": "every board, and which board it hangs under",
            "source": "user",
            "fields": {
                "boards": {"type": "array", "source": {"fold": FOLD, "path": "boards"}},
                "roots": {"type": "array", "source": {"fold": FOLD, "path": "roots"}},
                "cycles": {"type": "number", "source": {"fold": FOLD, "path": "cycles"}},
            },
        }
    )
    assert parsed.folds == frozenset({FOLD})
    assert parsed.fields["boards"].path == "boards"

    # And the catalog agrees those three paths are real, which is the other half of a
    # binding: the gate says the FOLD exists, the catalog says the PATH does.
    shape = dt.catalog_by_name()[FOLD].shape
    assert dt.shape_at(shape, "boards") == {"type": "array"}
    assert dt.shape_at(shape, "roots") == {"type": "array"}
    assert dt.shape_at(shape, "cycles") == {"type": "number"}
    assert dt.shape_at(shape, "not_a_field") is None


def test_the_catalog_keys_the_fold_by_tree_and_not_by_slot() -> None:
    """``keyed_by`` is the word a composing agent branches on.

    ``slot`` would send it to key a whole fleet's tree by one board's slot, and
    ``session`` by one conversation. Both read as complete answers.
    """
    entry = dt.catalog_by_name()[FOLD]
    assert entry.keyed_by == dt.KEYED_BY_TREE
    assert entry.keyed_by not in (dt.KEYED_BY_SLOT, dt.KEYED_BY_SESSION)
    assert FOLD in dt.describe()["tree_keyed"]
    assert FOLD not in dt.describe()["slot_keyed"]
    assert FOLD not in dt.describe()["session_keyed"]


def test_the_fold_has_no_bus_scope_and_that_is_the_design() -> None:
    """No scope, because no bus key covers "any member's log grew".

    Pinned beside a slot fold's scope so the two are read together: this is not a name
    the feed failed to recognise, it is a registered and bindable fold served on a page
    load and on a refetch instead of pushed.
    """
    assert dashboard_feed.scope_for(FOLD) == ""
    assert dashboard_feed.scope_for("work") != ""
    assert dashboard_feed.scope_for("status") != ""


def test_a_tree_read_refuses_a_fold_of_another_key_kind() -> None:
    """The unit resolver is the third kind's own route, so it refuses the other kinds.

    Answering for a slot fold here would fold it over a whole tree's units and serve
    several boards' entries as one board's.
    """
    for name in ("work", "workstreams", "status", "outline", "not-a-fold"):
        with pytest.raises(CrewLogError):
            projection.tree_fold_units(ROOT, name)
    # And the probe CAN pass, so the refusals are not read from a route that raises on
    # everything: the tree fold's own name resolves (to an empty closure in this home).
    units, dropped = projection.tree_fold_units(ROOT, FOLD)
    assert units == ()
    assert dropped == 0


# --------------------------------------------------------------------------- #
# 2. The measured row cost
# --------------------------------------------------------------------------- #


def _state_bytes(state: dict[str, Any]) -> int:
    """The state weighed the way the recorded row costs were weighed."""
    return len(json.dumps(state, ensure_ascii=False).encode("utf-8"))


def _board_rows(count: int) -> list[Entry]:
    """*count* board rows, every free-text field at the fold's own clamp."""
    return [
        _work(
            f"dashboard:board-{index:06d}",
            "goal",
            goal=WIDE,
            depth=index,
            parent_item=f"it-{index:08x}",
            generation=f"gen-{index:06d}-{'g' * 32}",
        )
        for index in range(count)
    ]


def _owner_rows(count: int) -> list[Entry]:
    """*count* item-to-board rows, all on one board."""
    return [
        _work(
            "dashboard:board-000000",
            "create",
            item_id=f"it-{index:08x}",
            title=WIDE,
            generation="gen-000000-" + "g" * 32,
        )
        for index in range(count)
    ]


_ROWS = 40


@pytest.mark.parametrize("make,kind", [(_board_rows, "board"), (_owner_rows, "owner")])
def test_the_tree_folds_row_cost_is_derived_from_its_own_rows(make, kind) -> None:
    """MUTATION-SENSITIVE: the recorded charge is RE-MEASURED, not asserted.

    This is the test the whole third key kind turns on. A key kind admitted with no
    measured cost falls through to ``_UNMEASURED_ROW_BYTES`` -- 64 KiB a row -- and a
    ceiling stated over that is not a bound. So the figure in ``_TREE_FOLD_ROW_BYTES``
    is load bearing, and it is derived here the way the slot table's members are:
    ``(bytes at N rows - bytes at 1 row) / (N - 1)``, every text field at the fold's own
    clamp, in 4-byte UTF-8 characters.

    TWO BOUNDS, both ways round. The charge must be at LEAST the measured cost of the
    fold's widest kind, or the ceiling admits more bytes than it says; and a whole
    driven cell must weigh no more than it is charged, or the charge is not an upper
    bound at all. A clamp raised without re-measuring fails the first; a figure left far
    above the truth after a clamp is LOWERED fails nothing here by design for the narrow
    kind, which is why the 10x band is applied only to the widest one.
    """
    one = _state(make(1))
    many = _state(make(_ROWS))
    count = projection._FOLDS[FOLD].count_rows
    assert count is not None
    one_rows, many_rows = count(one), count(many)
    assert many_rows > one_rows, f"the {kind} fixture added no counted rows"
    measured = (_state_bytes(many) - _state_bytes(one)) / (many_rows - one_rows)
    recorded = projection.tree_fold_row_bytes(FOLD)

    assert _state_bytes(many) <= projection.slot_fold_cell_bytes(FOLD, many), (
        f"a driven {kind} state weighs {_state_bytes(many):,} bytes and is charged "
        f"{projection.slot_fold_cell_bytes(FOLD, many):,}: the charge is not an upper bound"
    )
    assert measured <= recorded, (
        f"one {kind} row measures {measured:,.0f} bytes against the recorded "
        f"{recorded:,}; every row of this fold is charged that figure -- re-measure and "
        "update _TREE_FOLD_ROW_BYTES"
    )
    if kind == "board":
        # The WIDEST kind, which is the one the single figure must track. An owner row
        # is two bounded ids and sits far below it on purpose, so a band applied there
        # would be a band on the gap between the kinds rather than on the measurement.
        assert recorded <= 10 * measured, (
            f"the recorded {recorded:,} is more than ten times the measured "
            f"{measured:,.0f}: a clamp was lowered without re-measuring"
        )


def test_the_board_row_is_the_widest_kind_the_figure_covers() -> None:
    """ONE figure per fold, the widest of its own kinds -- so which one is widest is a
    fact the figure depends on, not a remark.

    If an owner row ever became the wider one the recorded figure would under-charge it
    while still passing its own bound above, because that bound compares each kind
    against the figure and not against the other kind.
    """
    count = projection._FOLDS[FOLD].count_rows
    assert count is not None

    def marginal(make) -> float:
        one, many = _state(make(1)), _state(make(_ROWS))
        return (_state_bytes(many) - _state_bytes(one)) / (count(many) - count(one))

    board, owner = marginal(_board_rows), marginal(_owner_rows)
    assert board > owner, f"an owner row ({owner:,.0f}B) is now wider than a board ({board:,.0f}B)"


def test_both_row_kinds_are_counted() -> None:
    """``count_rows`` counts boards AND owners.

    A counter that weighed only ``boards`` would charge a tree of one board and four
    thousand owner rows as one row, and the owner map is the half that grows with the
    RECORD rather than with the page.
    """
    count = projection._FOLDS[FOLD].count_rows
    assert count is not None
    boards_only = _state(_board_rows(5))
    assert count(boards_only) == 5
    with_owners = _state(_board_rows(5) + _owner_rows(7))
    assert count(with_owners) == 5 + 7


def test_a_cell_is_never_charged_zero() -> None:
    """An empty tree still holds its flat header fields, so it is charged one row."""
    empty = _state([])
    assert projection.slot_fold_cell_bytes(FOLD, empty) == projection.tree_fold_row_bytes(FOLD)


def test_the_measured_tables_are_kept_apart_by_key_kind() -> None:
    """Each kind states its figure against its OWN largest member, so a tree fold must
    not be read out of the slot table and a slot fold must not be read out of this one.
    """
    assert FOLD in projection._TREE_FOLD_ROW_BYTES
    assert FOLD not in projection._SLOT_FOLD_ROW_BYTES
    assert not set(projection._TREE_FOLD_ROW_BYTES) & set(projection._SLOT_FOLD_ROW_BYTES)
    # A name that is not a tree fold answers the unmeasured fallback here rather than
    # silently borrowing a slot fold's measurement.
    assert projection.tree_fold_row_bytes("work") == projection._UNMEASURED_ROW_BYTES


# --------------------------------------------------------------------------- #
# 3. The placement, through the item indirection
# --------------------------------------------------------------------------- #


def test_an_empty_tree_states_its_shape() -> None:
    value = _fold([])
    assert value["root"] == ROOT
    assert value["boards"] == []
    assert value["roots"] == []
    assert value["cycles"] == 0
    assert value["unresolved"] == 0
    assert value["dropped"] == 0
    assert value["limit"] == projection.WORKTREE_BOARD_LIMIT
    assert value["goal_chars"] == projection.WORKTREE_GOAL_CHARS


def test_a_board_hangs_under_the_board_that_created_its_cited_item() -> None:
    """THE WORK PART. An edge is recorded as an ITEM id; the parent is a BOARD.

    Nothing in the child's own record names its parent board -- only the item -- so the
    fold has to hold which board created that item. Resolving it wrongly is the failure
    mode that still renders a tree, just the wrong one.
    """
    value = _fold([_work(ROOT, "goal", goal="root goal")] + _hang(MID, "it-a", ROOT))
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, MID)["parent_item"] == "it-a"
    assert _row(value, ROOT)["parent"] is None
    assert value["roots"] == [ROOT]
    assert value["unresolved"] == 0


def test_the_citation_can_arrive_before_the_create_that_resolves_it() -> None:
    """Order-independent, which it has to be: an item's creation and the board hanging
    under it are recorded in DIFFERENT logs, and the walk yields whole logs at a time.

    A fold that resolved the parent at step time would answer "orphan" here. Resolution
    runs in ``render``, over the whole map, which is why the order cannot matter.
    """
    forwards = _fold(
        [
            _work(ROOT, "create", item_id="it-a", title="an item"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    # Minted in the opposite order rather than reversed, because ``advance`` requires
    # ascending seqs: what is being varied is which LOG was read first, not whether a
    # checkpoint can go backwards.
    backwards = _fold(
        [
            _work(MID, "goal", goal="mid", parent_item="it-a"),
            _work(ROOT, "create", item_id="it-a", title="an item"),
        ]
    )
    assert _row(forwards, MID)["parent"] == ROOT
    assert _row(backwards, MID)["parent"] == ROOT


def test_three_levels_of_boards_chain() -> None:
    """A fleet is a chain of conductors, so the indirection has to compose."""
    value = _fold(
        [_work(ROOT, "goal", goal="root")]
        + _hang(MID, "it-a", ROOT)
        + _hang(LEAF, "it-b", MID, depth=2)
    )
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, LEAF)["parent"] == MID
    assert _row(value, LEAF)["depth"] == 2
    assert value["roots"] == [ROOT]


def test_a_cited_item_outside_the_tree_leaves_a_root_that_still_carries_it() -> None:
    """A tree read from a MID-tree root: the item lives in a board above the root.

    Degrades to a root rather than to an error, which is ``fold_citations``' posture.
    What this fold adds is the distinction a reader needs: ``parent`` is null while
    ``parent_item`` is not, and ``unresolved`` counts it -- so "no parent" and "a parent
    outside this tree" are not one answer.
    """
    value = _fold(
        [_work(MID, "goal", goal="mid", parent_item="it-above", depth=1)]
        + _hang(LEAF, "it-b", MID),
        root=MID,
    )
    row = _row(value, MID)
    assert row["parent"] is None
    assert row["parent_item"] == "it-above"
    assert row["cycle"] is False
    assert value["unresolved"] == 1
    assert value["roots"] == [MID]
    # The child below it still hangs correctly: an unresolvable edge at the top does
    # not propagate down.
    assert _row(value, LEAF)["parent"] == MID


def test_a_board_citing_an_item_of_its_own_is_a_cycle_of_one() -> None:
    """Self-citation through the item indirection. Reachable from a takeover recorded
    against a stale reading of the tree, and a consumer must not nest it."""
    value = _fold(
        [
            _work(MID, "create", item_id="it-self", title="an item"),
            _work(MID, "goal", goal="mid", parent_item="it-self"),
        ],
        root=MID,
    )
    assert _row(value, MID)["cycle"] is True
    assert value["cycles"] == 1
    # On a cycle and therefore NOT a root: it has a parent the tree can follow, and the
    # consumer is told not to nest it rather than told it is a top.
    assert value["roots"] == []


def test_two_boards_citing_each_others_items_are_both_on_the_cycle() -> None:
    value = _fold(
        [
            _work(ROOT, "create", item_id="it-a", title="an item"),
            _work(MID, "create", item_id="it-b", title="an item"),
            _work(ROOT, "goal", goal="root", parent_item="it-b"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    assert _row(value, ROOT)["cycle"] is True
    assert _row(value, MID)["cycle"] is True
    assert value["cycles"] == 2
    assert value["roots"] == []


def test_a_board_hanging_off_a_cycle_member_keeps_its_edge_and_is_not_on_the_cycle() -> None:
    entries = [
        _work(ROOT, "create", item_id="it-a", title="an item"),
        _work(MID, "create", item_id="it-b", title="an item"),
        _work(ROOT, "goal", goal="root", parent_item="it-b"),
        _work(MID, "goal", goal="mid", parent_item="it-a"),
    ] + _hang(LEAF, "it-c", MID)
    value = _fold(entries)
    assert _row(value, LEAF)["cycle"] is False
    assert _row(value, LEAF)["parent"] == MID
    assert value["cycles"] == 2


def test_the_placement_is_fold_citations_answer_and_not_a_second_copy() -> None:
    """The rules are the session tree's, shared rather than restated.

    Driven through BOTH entry points over the same edges: this fold, and
    ``fold_citations`` called directly on the board-to-board citations the fold resolved.
    Without this every case above could pass while the two drifted apart, and the point
    of the generalization was that there is one implementation of the hard part.
    """
    entries = [
        _work(ROOT, "goal", goal="root"),
        _work(ROOT, "create", item_id="it-a", title="an item"),
        _work(MID, "goal", goal="mid", parent_item="it-a"),
        _work(MID, "create", item_id="it-b", title="an item"),
        _work(LEAF, "goal", goal="leaf", parent_item="it-b"),
        # An orphan: cites an item no board here created.
        _work("chat-4-orphan", "goal", goal="orphan", parent_item="it-elsewhere"),
    ]
    value = _fold(entries)
    direct = fold_citations(
        [
            (ROOT, None),
            (MID, ROOT),
            (LEAF, MID),
            ("chat-4-orphan", None),
        ]
    )
    for row in value["boards"]:
        placement = direct[row["board"]]
        assert row["parent"] == placement.parent, row["board"]
        assert row["cycle"] == placement.cycle, row["board"]


def test_only_a_create_or_a_baseline_says_which_board_owns_an_item() -> None:
    """A worker's report carries ``slot`` and ``item_id`` too, and that slot is the board
    the report was filed AGAINST -- not a second claim on the item.

    Reading it as one would hand ownership to whichever board reported last, so a child
    board reporting against its parent's item would end up owning it and the edge would
    invert.
    """
    value = _fold(
        [
            _work(ROOT, "create", item_id="it-a", title="an item"),
            # The worker's report, naming the same item and the same board.
            _work(ROOT, "report", actor="worker", item_id="it-a", status="progress"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, ROOT)["items"] == 1


def test_a_report_naming_an_item_no_create_established_claims_nothing() -> None:
    """The negative direction, so the rule above is not passing by accident: a report
    alone must leave the item unowned, and the citing board an orphan."""
    value = _fold(
        [
            _work(LEAF, "report", actor="worker", item_id="it-a", status="progress"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ],
        root=MID,
    )
    assert _row(value, MID)["parent"] is None
    assert value["unresolved"] == 1
    # And the same probe CAN resolve an edge, so "nothing moved" is not read from a
    # broken fixture.
    moved = _fold(
        [
            _work(LEAF, "create", item_id="it-a", title="an item"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ],
        root=MID,
    )
    assert _row(moved, MID)["parent"] == LEAF


def test_a_baseline_establishes_ownership_like_a_create() -> None:
    """A board whose create predates the projection arrives as a baseline, and its
    children's edges have to resolve against it or a whole subtree detaches on rebuild.
    """
    value = _fold(
        [
            _work(ROOT, "report", actor="worker", item_id="it-a", baseline=True, title="an item"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    assert _row(value, MID)["parent"] == ROOT


def test_a_second_create_of_one_item_id_does_not_move_its_owner() -> None:
    """First claim wins, the rule the placement itself runs on. A second create is what
    the ``work`` fold omits too, and here it would silently re-point an edge."""
    value = _fold(
        [
            _work(ROOT, "create", item_id="it-a", title="an item"),
            _work(LEAF, "create", item_id="it-a", title="an item"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    assert _row(value, MID)["parent"] == ROOT
    assert value["dropped"] == 1


def test_a_boards_lineage_is_first_write_wins() -> None:
    """``parent_item`` and ``depth`` are minted with the board and never rewritten, so a
    later entry repeating them is a repeat and not a correction."""
    value = _fold(
        [_work(ROOT, "create", item_id="it-a", title="an item")]
        + [_work(LEAF, "create", item_id="it-b", title="an item")]
        + [
            _work(MID, "goal", goal="mid", parent_item="it-a", depth=1),
            _work(MID, "goal", goal="mid again", parent_item="it-b", depth=9),
        ]
    )
    assert _row(value, MID)["parent_item"] == "it-a"
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, MID)["depth"] == 1


def test_an_entry_naming_no_board_places_nothing_and_is_counted() -> None:
    value = _fold([_work("", "goal", goal="nowhere")])
    assert value["boards"] == []
    assert value["dropped"] == 1


# --------------------------------------------------------------------------- #
# Generations, clamps and bounds
# --------------------------------------------------------------------------- #


def test_a_new_generation_from_the_conductor_replaces_the_boards_header() -> None:
    """A generation is minted with a board's record, so a conductor entry carrying one
    this row does not hold is the NEXT board under that slot."""
    value = _fold(
        [
            _work(MID, "goal", goal="first board", parent_item="it-old", depth=3, generation="g1"),
            _work(MID, "goal", goal="second board", depth=1, generation="g2"),
        ],
        root=MID,
    )
    row = _row(value, MID)
    assert row["goal"] == "second board"
    assert row["generation"] == "g2"
    assert row["parent_item"] is None
    assert row["depth"] == 1


def test_an_item_id_from_a_superseded_generation_still_names_its_board() -> None:
    """DELIBERATE, and the opposite of the ``work`` fold's reset.

    ``owner`` is not cleared on a new generation. An item id from a superseded board
    still identifies the board that created it, which is the board a ``parent_item``
    citing that id meant -- and the citing child board may never be reset at all. The
    header is what belongs to a generation; ownership of an id is not.
    """
    value = _fold(
        [
            _work(ROOT, "create", item_id="it-a", title="an item", generation="g1"),
            _work(ROOT, "goal", goal="a new board", generation="g2"),
            _work(MID, "goal", goal="mid", parent_item="it-a"),
        ]
    )
    assert _row(value, MID)["parent"] == ROOT


def test_a_non_conductor_entry_of_another_generation_is_a_straggler() -> None:
    """The log is append-only, so a purged board's entries outlive it forever. Applied,
    a worker straggler's header would resurrect that board on every fold."""
    value = _fold(
        [
            _work(MID, "goal", goal="live board", generation="g2"),
            _work(MID, "goal", actor="worker", goal="purged board", generation="g1"),
        ],
        root=MID,
    )
    assert _row(value, MID)["goal"] == "live board"
    assert value["dropped"] == 1


def test_an_entry_with_no_generation_cannot_be_a_stamped_boards() -> None:
    value = _fold(
        [
            _work(MID, "goal", goal="live board", generation="g2"),
            _work(MID, "goal", goal="from before the stamp"),
        ],
        root=MID,
    )
    assert _row(value, MID)["goal"] == "live board"
    assert value["dropped"] == 1


def test_a_board_from_before_the_stamp_existed_still_folds() -> None:
    """It never adopts a generation, so its own generationless entries keep applying."""
    value = _fold(
        [_work(MID, "goal", goal="old board"), _work(MID, "goal", goal="still old")], root=MID
    )
    assert _row(value, MID)["goal"] == "still old"
    assert value["dropped"] == 0


@pytest.mark.parametrize("filler", ["a", "\u4e2d", "\U0001f600"])
def test_the_goal_is_clamped_in_characters_not_bytes(filler) -> None:
    """CHARACTERS, the unit every clamp in the module counts in -- and the unit the row
    cost is measured off. Driven with ASCII, CJK and an emoji, which is what actually
    proves it: a byte clamp would cut the CJK string at a third of its characters.
    """
    limit = projection.WORKTREE_GOAL_CHARS
    value = _fold([_work(MID, "goal", goal=filler * (limit + 50))], root=MID)
    assert _row(value, MID)["goal"] == filler * limit


def test_exactly_the_limit_survives_whole() -> None:
    limit = projection.WORKTREE_GOAL_CHARS
    value = _fold([_work(MID, "goal", goal="x" * limit)], root=MID)
    assert _row(value, MID)["goal"] == "x" * limit


def test_the_board_limit_bounds_the_tree_and_counts_the_rest() -> None:
    """A tree that silently shows fewer boards than the record holds reads as whole."""
    over = projection.WORKTREE_BOARD_LIMIT + 7
    value = _fold(_board_rows(over))
    assert len(value["boards"]) == projection.WORKTREE_BOARD_LIMIT
    assert value["dropped"] == 7


def test_the_owner_limit_bounds_the_item_map_and_counts_the_rest() -> None:
    over = projection.WORKTREE_OWNER_LIMIT + 3
    state = _state(_owner_rows(over))
    assert len(state["owner"]) == projection.WORKTREE_OWNER_LIMIT
    assert state["omitted"] == 3


def test_the_owner_limit_is_headroom_over_what_can_ever_be_read() -> None:
    """A board has at most ONE ``parent_item``, so at most ``WORKTREE_BOARD_LIMIT`` rows
    of the map can ever answer a citation. The limit's job is headroom for the ids that
    arrive before the citation they will answer, not capacity for every item."""
    assert projection.WORKTREE_OWNER_LIMIT >= projection.WORKTREE_BOARD_LIMIT


def test_the_fold_never_reaches_into_the_state_it_was_handed() -> None:
    """``copy_state`` must copy every container ``step`` can reach, transitively.

    A nested container left shared shows up as the PRIOR state moving, which the
    projection kernel reads as "nothing changed" and the change feed never reports.
    """
    fold = projection._FOLDS[FOLD]
    before = _state([_work(ROOT, "create", item_id="it-a", title="an item")])
    snapshot = json.loads(json.dumps(before))
    assert fold.copy_state is not None
    copy = fold.copy_state(before)
    fold.step(copy, _work(ROOT, "goal", goal="changed", parent_item="it-z"))
    fold.step(copy, _work(MID, "create", item_id="it-b", title="another"))
    assert before == snapshot


def test_only_the_work_entry_type_moves_the_fold() -> None:
    """``affects`` is the one work type, and the kernel skips the copy and the step for
    every other entry -- so no row can carry a message body or a tool row."""
    fold = projection._FOLDS[FOLD]
    assert fold.affects == frozenset({WORK_ENTRY_TYPE})
    for etype in ("message/sent", "tool/called", "turn/completed", "session/opened"):
        assert not fold.touched_by_type(etype), etype
    assert fold.touched_by_type(WORK_ENTRY_TYPE)


def test_the_fold_stands_at_the_base_state_version() -> None:
    """A NEW fold, so no savepoint is retired: nothing an existing fold stores changed.

    Pinned because the number is what a savepoint file carries, and a new fold silently
    inheriting a bumped sibling's version would resume onto the wrong logic.
    """
    assert projection._FOLDS[FOLD].state_version == projection._FOLD_STATE_VERSION_BASE


def test_the_state_survives_a_json_round_trip() -> None:
    """A checkpoint is only resumable if it can be written down."""
    start = projection.initial(FOLD)
    bind = projection._FOLDS[FOLD].bind_slot
    assert bind is not None
    bind(start.state, ROOT)
    grown = projection.advance(start, _board_rows(3) + _owner_rows(3))
    assert projection.state_is_serializable(grown)
