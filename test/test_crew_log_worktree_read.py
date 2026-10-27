"""Reading the ``worktree`` fold off REAL logs, from the root board's own units.

The companion file (``test_crew_log_worktree_fold.py``) drives the fold's rules over
hand-built entries. This file builds a real three-board fleet through the crew-log
writer, with the gateway's relay (``work_relay``) copying each nested board's records
up into its parent's log, and reads the tree through
:func:`~kiro_crew.crew_log.projection.read_tree_projection`.

The tree is folded from the ROOT board's own units only. A nested board reaches it as
the relayed copies in the root's log, so no bind is walked and no other board's log is
opened, and a read with nothing new reads no entry.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import work_relay

FOLD = crew_log.WORKTREE_FOLD_NAME

ROOT = "chat-1-root"
MID = "chat-2-mid"
LEAF = "chat-3-leaf"
ROOT_UNIT = "s-root"
MID_UNIT = "s-mid"
LEAF_UNIT = "s-leaf"


@pytest.fixture(autouse=True)
def _live_crew_log(tmp_path, _floor_monkeypatch):
    """A real crew log on its own data home, with the relay's bindings pinned.

    Through the isolation floor's OWN ``MonkeyPatch``, so a test that undoes its own
    patches cannot drop the data-home pin.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    bindings = {MID: ROOT, LEAF: MID}
    _floor_monkeypatch.setattr(work_relay, "_bound_board", lambda worker: bindings.get(worker, ""))
    _reset()
    yield bindings
    emit.flush()
    _reset()


def _reset() -> None:
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    work_relay.forget_for_tests()
    emit.reset_caches()


def _log(unit_id: str, slot: str) -> CrewLog:
    """A session log whose HEADER names *slot*."""
    return CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)


def _settle() -> None:
    # Each relay hop is queued from inside the append it copies, one round per hop.
    for _ in range(3):
        emit.flush()
        assert eager.drain(timeout=10.0)


def _work(unit: str, slot: str, action: str, *, actor: str = "conductor", **data: Any) -> None:
    """One ``work/recorded`` through the real append path, relays included."""
    fields = {"slot": slot, "actor": actor, "by": data.pop("by", slot), "action": action, **data}
    assert emit.on_work_recorded(unit, fields, timeout=5.0) is True
    _settle()


def _row(value: dict[str, Any], board: str) -> dict[str, Any]:
    matched = [row for row in value["boards"] if row["board"] == board]
    assert len(matched) == 1, f"{board!r} not in {[r['board'] for r in value['boards']]}"
    return matched[0]


def _three_board_fleet() -> None:
    """ROOT conducts MID, MID conducts LEAF -- one log per board, as a real fleet is.

    The parent's log holds the ``create`` that says which board owns an item, and the
    child's goal cites it. The child's goal reaches the parent's log as a relayed copy.
    """
    _log(ROOT_UNIT, ROOT)
    _work(ROOT_UNIT, ROOT, "goal", goal="ship the fleet", round=1, generation="g-root")
    _work(ROOT_UNIT, ROOT, "create", item_id="it-mid", title="run the middle", generation="g-root")
    _work(ROOT_UNIT, ROOT, "bind", item_id="it-mid", worker_session_key=MID, generation="g-root")

    _log(MID_UNIT, MID)
    _work(
        MID_UNIT,
        MID,
        "goal",
        goal="run the middle",
        parent_item="it-mid",
        depth=1,
        generation="g-mid",
    )
    _work(MID_UNIT, MID, "create", item_id="it-leaf", title="run the leaf", generation="g-mid")
    _work(MID_UNIT, MID, "bind", item_id="it-leaf", worker_session_key=LEAF, generation="g-mid")
    # MID reports against ROOT's item: the board's copy in ROOT's log, the mirror in
    # MID's. Reading the report as a claim on ``it-mid`` would invert the edge.
    report = {"by": MID, "item_id": "it-mid", "status": "progress"}
    _work(ROOT_UNIT, ROOT, "report", actor="worker", board_copy=True, **report)
    _work(MID_UNIT, ROOT, "report", actor="worker", mirror=True, **report)

    _log(LEAF_UNIT, LEAF)
    _work(
        LEAF_UNIT,
        LEAF,
        "goal",
        goal="run the leaf",
        parent_item="it-leaf",
        depth=2,
        generation="g-leaf",
    )


class _Opens:
    """Which units ``CrewLog.open`` was asked for inside the block."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.units: list[str] = []
        real = CrewLog.open.__func__  # type: ignore[attr-defined]
        opens = self

        def opened(cls: type, kind: str, unit_id: str, *args: Any, **kwargs: Any) -> CrewLog:
            opens.units.append(unit_id)
            return real(cls, kind, unit_id, *args, **kwargs)

        patch.setattr(CrewLog, "open", classmethod(opened))


def test_the_tree_is_folded_from_the_roots_own_units() -> None:
    """Read from ROOT, and the answer holds the boards whose records were relayed up."""
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
    assert _row(value, LEAF)["goal"] == "run the leaf"
    assert _row(value, LEAF)["depth"] == 2


def test_the_population_is_the_roots_own_units() -> None:
    """The units are the root board's own, the same selection the project report uses."""
    _three_board_fleet()
    units, dropped = crew_log.tree_fold_units(ROOT, FOLD)
    assert units == tuple(crew_log.session_ledger.work_crew_log_units(ROOT))
    assert set(units) == {ROOT_UNIT}
    assert dropped == 0


def test_a_read_never_opens_a_nested_boards_log(monkeypatch) -> None:
    _three_board_fleet()
    opens = _Opens(monkeypatch)
    crew_log.read_tree_projection(ROOT, FOLD)
    assert MID_UNIT not in opens.units and LEAF_UNIT not in opens.units, opens.units


def test_a_nested_record_appended_later_reaches_the_tree_by_relay() -> None:
    """A nested board's record lands in the root's log, so the next read shows it."""
    _log(ROOT_UNIT, ROOT)
    _work(ROOT_UNIT, ROOT, "goal", goal="ship the fleet", generation="g-root")
    _work(ROOT_UNIT, ROOT, "create", item_id="it-mid", title="run the middle", generation="g-root")
    before = crew_log.read_tree_projection(ROOT, FOLD).value
    assert {row["board"] for row in before["boards"]} == {ROOT}

    _log(MID_UNIT, MID)
    _work(MID_UNIT, MID, "goal", goal="run the middle", parent_item="it-mid", generation="g-mid")
    after = crew_log.read_tree_projection(ROOT, FOLD).value
    assert {row["board"] for row in after["boards"]} == {ROOT, MID}
    assert _row(after, MID)["parent"] == ROOT


def test_a_tree_read_from_a_mid_tree_root_reports_its_edge_as_unresolved() -> None:
    """Read from MID, whose ``parent_item`` names an item on ROOT's board.

    MID's own units hold MID's records and LEAF's relayed ones. The ``create`` that owns
    ``it-mid`` is in ROOT's log, which this read does not open, so MID is a root that
    still carries its citation and ``unresolved`` says so. The report MID mirrored is
    skipped, so ROOT is not a node here.
    """
    _three_board_fleet()
    value = crew_log.read_tree_projection(MID, FOLD).value

    assert {row["board"] for row in value["boards"]} == {MID, LEAF}
    mid = _row(value, MID)
    assert mid["parent"] is None
    assert mid["parent_item"] == "it-mid"
    assert value["unresolved"] == 1
    assert _row(value, LEAF)["parent"] == MID
    assert value["roots"] == [MID]


def test_a_workers_report_against_its_parents_item_does_not_invert_the_edge() -> None:
    _three_board_fleet()
    value = crew_log.read_tree_projection(ROOT, FOLD).value
    assert _row(value, MID)["parent"] == ROOT
    assert _row(value, ROOT)["parent"] is None
    assert _row(value, ROOT)["items"] == 1


def test_the_read_does_not_steal_the_workstreams_folds_refused_count() -> None:
    """The tree read takes the selection's second return, never the workstreams
    fold's read-and-cleared side map."""
    _three_board_fleet()
    crew_log._WORKSTREAMS_UNITS_DROPPED.clear()
    crew_log._WORKSTREAMS_UNITS_DROPPED[ROOT] = 7

    crew_log.read_tree_projection(ROOT, FOLD)

    assert crew_log._WORKSTREAMS_UNITS_DROPPED.get(ROOT) == 7
    assert crew_log._workstreams_units_dropped(ROOT) == 7


def test_units_past_the_limit_are_reported_as_dropped(monkeypatch) -> None:
    from kiro_crew.crew_log import projection_workstreams

    _three_board_fleet()
    _log("s-root-2", ROOT)
    _work("s-root-2", ROOT, "goal", goal="ship the fleet", round=2, generation="g-root")
    monkeypatch.setattr(projection_workstreams, "WORKSTREAMS_UNIT_LIMIT", 1)
    crew_log.forget_slot_folds()
    value = crew_log.read_tree_projection(ROOT, FOLD).value
    assert value["dropped"] >= 1


def test_the_bound_path_of_a_validated_block_resolves_against_the_folded_value() -> None:
    """END TO END: a package field validates, and the same path walked over the value
    this fold produced comes back with the rows."""
    _three_board_fleet()
    from kiro_crew.artifact_store import dashboard_package as dp
    from kiro_crew.dashboard.handlers import member_dashboard_fields as mdf

    package = dp.validate_package(
        {
            "kind": "dashboard",
            "bound_to": "crewmate:atlas",
            "model": {
                "types": {
                    "boards": {"type": "array", "source": {"fold": FOLD, "path": "boards"}},
                    "roots": {"type": "array", "source": {"fold": FOLD, "path": "roots"}},
                }
            },
            "view": {
                "html": (
                    '<p data-dashboard-field="boards"></p>' '<p data-dashboard-field="roots"></p>'
                )
            },
            "theme": {"tokens": {}},
        }
    )
    parsed = mdf.package_manifest(package, slug="fleet-tree", version=1)
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
    value = crew_log.read_tree_projection("chat-99-nothing", FOLD).value
    assert value["root"] == "chat-99-nothing"
    assert value["boards"] == []
    assert value["roots"] == []
    assert value["dropped"] == 0


def test_the_checkpoint_reports_the_newest_units_last_seq() -> None:
    """``last_seq`` is the last entry folded from the newest unit, a seq that file holds."""
    _three_board_fleet()
    checkpoint = crew_log.fold_tree_checkpoint(FOLD, ROOT)
    assert checkpoint.name == FOLD
    units, _ = crew_log.tree_fold_units(ROOT, FOLD)
    newest = CrewLog.open(lg.KIND_SESSION, units[-1])
    assert newest is not None
    reached = max(int(entry.seq) for entry in newest.iter_from(1))
    assert 0 < checkpoint.last_seq <= reached
    assert crew_log.state_is_serializable(checkpoint)
