"""Reading the ``worktree`` fold off REAL logs, across several slots.

The companion file (``test_crew_log_worktree_fold.py``) drives the fold's rules over
hand-built entries, which is the right level for a rule. It cannot show the thing that
makes this a third key kind: that the value is assembled from the logs of MANY SLOTS,
reached by following what the record says rather than by asking the store what one slot
ran under. So these cases build real crew logs -- one per board, each with its own slot
header -- and read the tree through
:func:`~kiro_crew.crew_log.projection.read_tree_projection`.

That is also the end-to-end claim the work item turns on. A block binds a fold NAME, and
a name is only worth binding if a reader can get a value back from it. Both halves are
here: the manifest's gate admits the name (companion file) and this file shows the value
the bound path resolves to, folded from logs the key's own slot listing never names.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log.entry_types import WORK_ENTRY_TYPE
from kiro_crew.dashboard_templates import manifest

FOLD = crew_log.WORKTREE_FOLD_NAME
GATEWAY = "gateway"

ROOT = "chat-1-root"
MID = "chat-2-mid"
LEAF = "chat-3-leaf"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Every test writes into its own data home, never the live one.

    Through the isolation floor's OWN ``MonkeyPatch`` rather than the shared
    ``monkeypatch`` a test also receives, because the two are undone independently.
    ``monkeypatch.undo()`` reverts every record on the instance it is called on, so a
    test that calls it to drop one of its own patches would drop this pin with them and
    whatever ran next would resolve the operator's real data home.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()
    yield
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    emit.reset_caches()


def _log(unit_id: str, slot: str) -> CrewLog:
    """A session log whose HEADER names *slot*, which is what a slot read joins on."""
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _work(
    handle: CrewLog,
    slot: str,
    action: str,
    *,
    actor: str = "conductor",
    by: str = "",
    **data: Any,
):
    """One real ``work/recorded`` entry, through the store's own validation.

    ``by`` defaults to the board's slot for a conductor action, which is what the
    writer records: a conductor acts as the board. A worker's report passes its own.
    """
    return handle.append(
        WORK_ENTRY_TYPE,
        {"slot": slot, "actor": actor, "by": by or slot, "action": action, **data},
        src=GATEWAY,
    )


def _row(value: dict[str, Any], board: str) -> dict[str, Any]:
    matched = [row for row in value["boards"] if row["board"] == board]
    assert len(matched) == 1, f"{board!r} not in {[r['board'] for r in value['boards']]}"
    return matched[0]


def _three_board_fleet() -> None:
    """ROOT conducts MID, MID conducts LEAF -- one log per board, as a real fleet is.

    The shape that matters: every edge is recorded in TWO logs. The parent's log holds
    the ``create`` that says which board owns the item, and the child's own log holds
    the ``goal`` that cites it. Neither log alone places the child.
    """
    root = _log("s-root", ROOT)
    _work(root, ROOT, "goal", goal="ship the fleet", round=1, generation="g-root")
    _work(root, ROOT, "create", item_id="it-mid", title="run the middle", generation="g-root")
    # The bind is what makes MID's log reachable from ROOT: the closure walk follows
    # ``worker_session_key``, and nothing else in ROOT's log names MID.
    _work(root, ROOT, "bind", item_id="it-mid", worker_session_key=MID, generation="g-root")

    mid = _log("s-mid", MID)
    _work(
        mid,
        MID,
        "goal",
        goal="run the middle",
        parent_item="it-mid",
        depth=1,
        generation="g-mid",
    )
    _work(mid, MID, "create", item_id="it-leaf", title="run the leaf", generation="g-mid")
    _work(mid, MID, "bind", item_id="it-leaf", worker_session_key=LEAF, generation="g-mid")
    # MID also reports back against ROOT's item, in its own log. That entry names ROOT's
    # board, and reading it as a claim on ``it-mid`` would invert the edge.
    _work(mid, ROOT, "report", actor="worker", by=MID, item_id="it-mid", status="progress")

    leaf = _log("s-leaf", LEAF)
    _work(
        leaf,
        LEAF,
        "goal",
        goal="run the leaf",
        parent_item="it-leaf",
        depth=2,
        generation="g-leaf",
    )


def test_the_tree_is_folded_from_the_logs_of_every_slot_it_reaches() -> None:
    """THE THIRD KEY KIND, shown rather than argued.

    Read from ROOT, and the answer contains boards whose entries are in MID's and LEAF's
    logs. A slot read of ROOT sees ROOT's units only, so no slot-keyed fold could produce
    this value however it was written.
    """
    _three_board_fleet()
    value = crew_log.read_tree_projection(ROOT, FOLD).value

    assert value["root"] == ROOT
    assert {row["board"] for row in value["boards"]} == {ROOT, MID, LEAF}
    assert _row(value, ROOT)["parent"] is None
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, LEAF)["parent"] == MID
    assert value["roots"] == [ROOT]
    assert value["cycles"] == 0
    assert value["unresolved"] == 0

    # The labels came from each board's OWN log, which is the other half of the join.
    assert _row(value, LEAF)["goal"] == "run the leaf"
    assert _row(value, LEAF)["depth"] == 2


def test_the_population_is_wider_than_the_keys_own_slot() -> None:
    """The key does not determine the population, which is reason 2 for a third kind.

    ``tree_fold_units`` returns units from three slots; the store's listing for the key's
    own slot returns one. A kind whose population were a property of its key could not
    show this difference.
    """
    _three_board_fleet()
    from kiro_crew.crew_log.store import session_units_for_slot

    units, dropped = crew_log.tree_fold_units(ROOT, FOLD)
    assert set(units) == {"s-root", "s-mid", "s-leaf"}
    assert dropped == 0
    assert set(session_units_for_slot(ROOT)) == {"s-root"}


def test_a_bind_appended_later_widens_the_tree_with_the_key_unchanged() -> None:
    """Reason 2 again, in the direction that matters for invalidation.

    Nothing about ROOT changed; a record appended in ROOT's log brought a slot into the
    closure that was not in it before. That is why the fold is lazy and has no bus
    scope: the event a warm cell would need is "a member's log grew", and the member is
    not known until the walk runs.
    """
    root = _log("s-root", ROOT)
    _work(root, ROOT, "goal", goal="ship the fleet", generation="g-root")
    _work(root, ROOT, "create", item_id="it-mid", title="run the middle", generation="g-root")
    mid = _log("s-mid", MID)
    _work(mid, MID, "goal", goal="run the middle", parent_item="it-mid", generation="g-mid")

    before = crew_log.read_tree_projection(ROOT, FOLD).value
    assert {row["board"] for row in before["boards"]} == {ROOT}

    _work(root, ROOT, "bind", item_id="it-mid", worker_session_key=MID, generation="g-root")
    after = crew_log.read_tree_projection(ROOT, FOLD).value
    assert {row["board"] for row in after["boards"]} == {ROOT, MID}
    assert _row(after, MID)["parent"] == ROOT


def test_a_tree_read_from_a_mid_tree_root_reports_its_edge_as_unresolved() -> None:
    """Read from MID, whose own ``parent_item`` names an item in ROOT's board.

    The walk follows binds DOWNWARD only, so ROOT's log is not read and the ``create``
    that owns ``it-mid`` is not folded. MID is therefore a root that still carries its
    citation, and ``unresolved`` is what tells a reader "a parent outside this tree"
    apart from "no parent".

    ROOT still appears as a NODE, and that is the record's doing rather than the fold's:
    MID's own log holds a report filed against ROOT's board, so this tree knows that
    board exists while knowing nothing else about it. It renders headerless -- no goal,
    no items -- which is the honest answer and the same posture the rest of this module
    takes: degrade to a node with less, never invent the missing half.
    """
    _three_board_fleet()
    value = crew_log.read_tree_projection(MID, FOLD).value

    assert {row["board"] for row in value["boards"]} == {MID, LEAF, ROOT}
    mid = _row(value, MID)
    assert mid["parent"] is None
    assert mid["parent_item"] == "it-mid"
    assert value["unresolved"] == 1
    # ROOT, known only from the report, carries nothing it was not told.
    headerless = _row(value, ROOT)
    assert headerless["goal"] == ""
    assert headerless["items"] == 0
    assert headerless["parent_item"] is None
    # LEAF's edge still resolves: the unresolvable one above it does not propagate down.
    assert _row(value, LEAF)["parent"] == MID
    assert set(value["roots"]) == {MID, ROOT}


def test_a_workers_report_against_its_parents_item_does_not_invert_the_edge() -> None:
    """The report is in MID's log and names ROOT's board and ROOT's item.

    Driven here rather than only over hand-built entries because this is the arrangement
    a real record produces, and the wrong reading of it -- last writer owns the item --
    would make MID own ``it-mid`` and hang ROOT under MID.
    """
    _three_board_fleet()
    value = crew_log.read_tree_projection(ROOT, FOLD).value
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, ROOT)["parent"] is None
    assert _row(value, ROOT)["items"] == 1


def test_the_read_does_not_steal_the_workstreams_folds_refused_count() -> None:
    """The two folds share the closure WALK and must not share its side map.

    ``_workstreams_units`` hands its refused-unit count to its own render through a
    read-and-cleared map. A tree read that went through that function would overwrite
    the entry and, not consuming it, leave the workstreams fold reading this read's
    number -- or zero, if it read second. The tree read takes the walk's second return
    instead, so the map is untouched.
    """
    _three_board_fleet()
    crew_log._WORKSTREAMS_UNITS_DROPPED.clear()
    crew_log._WORKSTREAMS_UNITS_DROPPED[ROOT] = 7

    crew_log.read_tree_projection(ROOT, FOLD)

    assert crew_log._WORKSTREAMS_UNITS_DROPPED.get(ROOT) == 7
    assert crew_log._workstreams_units_dropped(ROOT) == 7


def test_the_extracted_walk_and_the_workstreams_selector_agree() -> None:
    """The refactor is behaviour-preserving, pinned rather than asserted.

    ``_workstreams_units`` is now a thin caller of ``_board_closure_units``, so the units
    it selects must be the walk's own and in the same order -- otherwise the workstreams
    fold's answer changed under a change that was supposed to be about a different fold.
    """
    _three_board_fleet()
    walked, dropped = crew_log._board_closure_units(ROOT)
    crew_log._WORKSTREAMS_UNITS_DROPPED.clear()
    selected = crew_log._workstreams_units(ROOT)
    assert selected == walked
    assert crew_log._workstreams_units_dropped(ROOT) == dropped


def test_the_bound_path_of_a_validated_block_resolves_against_the_folded_value() -> None:
    """END TO END, both halves in one case.

    A manifest field validates (the gate admits the fold name and the catalog has the
    path), and the SAME path walked over the value this fold produced from three logs
    comes back with the rows. A fold that validated and returned nothing would pass every
    other test in these two files.
    """
    _three_board_fleet()
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
            },
        }
    )
    value: Any = crew_log.read_tree_projection(ROOT, FOLD).value
    for name in ("boards", "roots"):
        spec = parsed.fields[name]
        assert spec.fold == FOLD
        walked: Any = value
        for key in spec.path.split("."):
            walked = walked[key]
        assert walked, f"the bound path {spec.path!r} resolved to an empty value"
    assert len(value["boards"]) == 3
    assert value["roots"] == [ROOT]


def test_an_unfolded_tree_is_an_empty_tree_and_not_a_refusal() -> None:
    """A root with no log of its own is a tree of nothing, which is the same posture
    every fold here takes: a missing unit is skipped rather than refused."""
    value = crew_log.read_tree_projection("chat-99-nothing", FOLD).value
    assert value["root"] == "chat-99-nothing"
    assert value["boards"] == []
    assert value["roots"] == []
    assert value["dropped"] == 0


def test_the_checkpoint_reports_the_newest_units_last_seq() -> None:
    """``last_seq`` is the only figure a later read can compare against -- the last entry
    folded from the NEWEST unit, never a sum across files, which is a number no file
    carries and a reader could not truncate against.
    """
    _three_board_fleet()
    checkpoint = crew_log.fold_tree_checkpoint(FOLD, ROOT)
    assert checkpoint.name == FOLD
    # LEAF's log is the newest unit in the walk and holds one work entry beside its
    # opener, so the reached seq is that file's own and far below the three logs' total.
    units, _ = crew_log.tree_fold_units(ROOT, FOLD)
    newest = CrewLog.open(lg.KIND_SESSION, units[-1])
    assert newest is not None
    reached = max(int(entry.seq) for entry in newest.iter_from(1))
    assert 0 < checkpoint.last_seq <= reached
    # And NOT a sum across the three files, which is the reading this guards against.
    total = 0
    for unit_id in units:
        handle = CrewLog.open(lg.KIND_SESSION, unit_id)
        assert handle is not None
        total += max(int(entry.seq) for entry in handle.iter_from(1))
    assert checkpoint.last_seq < total
    assert crew_log.state_is_serializable(checkpoint)
