"""The ``workers`` slot fold: the worker sessions a crewmate created, read from real logs.

Pins what a crewmate's Dashboard page draws for "what are my sessions doing":

* a ``session/spawned`` in the crewmate's own log makes a row, and the worker's own
  log (admitted only when its opener names the crewmate as parent) moves it;
* running, between turns (``idle``), failed, done (only from the worker's own
  ``done`` report), blocked, closed and released, each from an entry the log writes;
* the task is the first line the worker was given, ``last_said`` the last line of its
  newest reply;
* the newest rows survive the limit and the rest are counted;
* ``workers_units`` joins the children the session tree names, and the eager path
  routes a worker's entry to its creator's cell.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_workers as pw
from kiro_crew.crew_log import schema as lg

MATE = "member-dm:tide"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    crew_log.forget_slot_folds()
    yield
    crew_log.forget_slot_folds()


_n = iter(range(10_000))
#: Each unit a test made, and the slot it was made under.
_slot_of: dict[str, str] = {}


def _unit(slot: str, entries: list[tuple[str, dict[str, Any]]], *, parent: str = "") -> str:
    unit = f"s-workers-{next(_n)}"
    handle = CrewLog.create(lg.KIND_SESSION, unit, owner="raymond", agent="kirocrew", slot=slot)
    opened: dict[str, Any] = {
        "agent": "kirocrew-worker" if parent else "kirocrew",
        "slot": slot,
        "model": "",
        "cwd": "/tmp",
        "owner": "raymond",
        "resumed": False,
    }
    if parent:
        opened["parent"] = {"slot": parent}
    handle.append("session/opened", opened, src="gateway")
    for entry_type, data in entries:
        handle.append(entry_type, data, src="gateway")
    handle.release_ownership()
    _slot_of[unit] = slot
    return unit


def _fold(units: list[str]) -> dict[str, Any]:
    """The merged ``workers`` value, each part folded the way the reader folds it: the
    crewmate's units into ``workers``, each worker's own units into its ``worker``."""
    own = crew_log.fold_slot_warm(
        "workers", tuple(u for u in units if _slot_of[u] == MATE), slot=MATE
    ).state
    by_child: dict[str, list[str]] = {}
    for unit in units:
        if _slot_of[unit] != MATE:
            by_child.setdefault(_slot_of[unit], []).append(unit)
    worker = crew_log._FOLDS["worker"]
    workers = {
        child: worker.render(crew_log.fold_slot_warm("worker", tuple(us), slot=child).state)
        for child, us in by_child.items()
    }
    return pw.merge_render(own, MATE, workers)


def _spawned(child: str) -> tuple[str, dict[str, Any]]:
    return ("session/spawned", {"child": {"slot": child}})


def _turn(n: int) -> tuple[str, dict[str, Any]]:
    return ("turn/started", {"turn": n, "actor": "crew", "depth": 1})


def _done(n: int, stop: str = "end_turn") -> tuple[str, dict[str, Any]]:
    return ("turn/completed", {"turn": n, "stop_reason": stop})


def _asked(text: str) -> tuple[str, dict[str, Any]]:
    return ("message/received", {"turn": 1, "role": "user", "source": "crew", "text": text})


def _said(text: str) -> tuple[str, dict[str, Any]]:
    return ("message/sent", {"turn": 1, "text": text})


def test_a_created_worker_is_a_row_and_its_own_log_moves_it():
    mate = _unit(MATE, [_spawned("w-tests")])
    worker = _unit(
        "w-tests",
        [_asked("Fix the two failing tests.\nThen run the suite."), _turn(1), _asked("status?")],
        parent=MATE,
    )
    (row,) = _fold([mate, worker])["items"]
    assert (row["slot"], row["state"], row["turns"]) == ("w-tests", "running", 1)
    assert row["task"] == "Fix the two failing tests."
    assert row["agent"] == "kirocrew-worker"
    assert row["started_at"] and row["last_activity_at"]


def test_between_turns_reads_idle_never_done():
    """A worker that answered and stopped looks the same as one waiting: the log does
    not say it finished, so the fold says idle."""
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit(
        "w1", [_asked("go"), _turn(1), _said("Done.\nAll 4 tests pass."), _done(1)], parent=MATE
    )
    value = _fold([mate, worker])
    (row,) = value["items"]
    assert row["state"] == "idle"
    assert row["last_said"] == "All 4 tests pass."
    assert value["counts"]["idle"] == 1 and value["counts"]["done"] == 0


@pytest.mark.parametrize(
    ("before", "turn_end", "state"),
    [("blocked", False, "running"), ("blocked", True, "idle"), ("done", False, "running")],
    ids=["blocked-mid-turn", "blocked-then-turn-ended", "done-mid-turn"],
)
def test_a_progress_report_takes_back_an_earlier_done_or_blocked(before, turn_end, state):
    mate = _unit(MATE, [_spawned("w1")])
    tail = [_report(before, "stuck"), _report("progress", "unstuck")]
    if turn_end:
        tail.append(_done(1))
    worker = _unit("w1", [_asked("go"), _turn(1), *tail], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert (row["state"], row["report_status"], row["report"]) == (state, "progress", "unstuck")


def test_an_unseeded_tree_names_no_creator_and_no_children(_floor_monkeypatch):
    """Before the session tree is seeded for this store it is not evidence: no creator,
    no children, and no worker reads as let go."""
    from kiro_crew.crew_log import session_tree as st
    from kiro_crew.crew_log import session_tree_projection as stp

    tree = stp.SessionTreeProjection()
    tree.apply(st.OpenedRecord(sid="u1", slot="w1", created_at=1, parent_slot=MATE))
    _floor_monkeypatch.setattr(stp, "projection", lambda: tree)
    assert not tree.seeded_for_current_store
    assert stp.creator_of("w1") == "" and stp.created_children(MATE) == ()
    assert not pw.tree_seeded() and not pw._let_go("w1")


def test_a_read_before_the_tree_is_seeded_is_served_at_revision_zero(_floor_monkeypatch):
    mate = _unit(MATE, [_spawned("w1")])
    w1 = _unit("w1", [_asked("go"), _turn(1)], parent=MATE)
    _reader_env(_floor_monkeypatch, {MATE: (mate,), "w1": (w1,)})
    _floor_monkeypatch.setattr(crew_log, "tree_seeded", lambda: False)
    assert crew_log.read_slot_projection(MATE, "workers").revision == 0
    _floor_monkeypatch.setattr(crew_log, "tree_seeded", lambda: True)
    assert crew_log.read_slot_projection(MATE, "workers").revision > 0


def test_a_close_mid_turn_stays_closed_when_the_cut_turn_ends_after_it():
    mate = _unit(MATE, [_spawned("w1")])
    closed = ("session/closed", {"reason": "reset"})
    worker = _unit("w1", [_asked("go"), _turn(1), closed, _done(1, "interrupted")], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert (row["state"], row["stop_reason"], row["closed_reason"]) == (
        "closed",
        "interrupted",
        "reset",
    )


def test_a_chunked_first_message_is_the_task_and_a_follow_up_is_not():
    mate = _unit(MATE, [_spawned("w1")])
    chunked = (
        "message/received",
        {"turn": 1, "role": "user", "source": "crew", "chunks": [4, 5], "chars": 12000},
    )
    worker = _unit("w1", [chunked, _turn(1), _asked("status?")], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert row["task"] == ""


def test_a_takeover_by_another_session_reads_released_and_its_work_is_not_drawn():
    """Adopted with no release first: from the adoption on, the worker does another
    session's work, so its creator's row stops there."""
    mate = _unit(MATE, [_spawned("w1")])
    took = ("session/adopted", {"parent": {"slot": "member-dm:quill"}})
    worker = _unit(
        "w1", [_asked("go"), _turn(1), took, _turn(2), _said("Quill's work.")], parent=MATE
    )
    (row,) = _fold([mate, worker])["items"]
    assert (row["state"], row["turns"], row["last_said"]) == ("released", 1, "")


def test_an_adoption_naming_its_own_creator_changes_nothing():
    mate = _unit(MATE, [_spawned("w1")])
    back = ("session/adopted", {"parent": {"slot": MATE}})
    worker = _unit("w1", [_asked("go"), back, _turn(1)], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert row["state"] == "running"


def test_a_long_line_is_cut_at_the_rows_limit_with_an_ellipsis():
    long_task = "Fix " + "the parser and its tests " * 20
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit("w1", [_asked(long_task), _turn(1)], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert len(row["task"]) == pw.WORKERS_TEXT_LIMIT and row["task"].endswith("…")
    assert pw._clip("short line") == "short line"


def test_a_chunked_reply_does_not_leave_the_older_one_as_last_said():
    """A reply too long for one entry carries ``chunks`` and no inline ``text``; the
    earlier, shorter reply is not shown as what the worker said last."""
    mate = _unit(MATE, [_spawned("w1")])
    chunked = ("message/sent", {"turn": 2, "chunks": [7, 8], "chars": 9000})
    worker = _unit(
        "w1", [_asked("go"), _turn(1), _said("Halfway."), _done(1), _turn(2), chunked], parent=MATE
    )
    (row,) = _fold([mate, worker])["items"]
    assert row["last_said"] == ""


@pytest.mark.parametrize(
    ("tail", "state"),
    [
        ([_turn(1), _done(1, "failed")], "failed"),
        ([_turn(1), _done(1, "interrupted")], "failed"),
        ([_turn(1), _done(1, "timeout")], "failed"),
        ([_turn(1), _done(1, "error: cancel unacked")], "failed"),
        ([_turn(1), _done(1, "cancelled")], "idle"),
        ([("session/closed", {"reason": "destroyed"})], "closed"),
        ([("session/released", {})], "released"),
    ],
)
def test_each_end_state_comes_from_an_entry_the_log_writes(tail, state):
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit("w1", tail, parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert row["state"] == state


def _report(status: str, summary: str) -> tuple[str, dict[str, Any]]:
    return (
        "work/recorded",
        {
            "slot": MATE,
            "actor": "worker",
            "by": "w1",
            "action": "report",
            "item_id": "it_00000001",
            "status": status,
            "summary": summary,
            "event": f"{status}: {summary}",
            "event_kind": "report",
        },
    )


def test_done_and_blocked_come_only_from_the_workers_own_report():
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit("w1", [_turn(1), _report("done", "parser merged"), _done(1)], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert (row["state"], row["report"], row["report_status"]) == ("done", "parser merged", "done")
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit("w1", [_turn(1), _report("question", "which unit?"), _done(1)], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert row["state"] == "blocked"


def test_a_unit_whose_opener_names_another_parent_is_not_admitted():
    mate = _unit(MATE, [_spawned("w1")])
    stranger = _unit("w-other", [_turn(1)], parent="member-dm:quill")
    value = _fold([mate, stranger])
    assert [r["slot"] for r in value["items"]] == ["w1"]
    assert value["items"][0]["state"] == "starting"


def test_the_newest_workers_are_kept_and_the_rest_counted():
    limit = pw.WORKERS_LIMIT
    mate = _unit(MATE, [_spawned(f"w{n:02d}") for n in range(limit + 3)])
    value = _fold([mate])
    assert len(value["items"]) == limit
    assert value["total"] == limit + 3 and value["omitted"] == 3
    assert value["items"][0]["slot"] == f"w{limit + 2:02d}"  # newest first
    assert sum(value["counts"].values()) == limit + 3


def test_past_the_cap_the_newest_are_held_and_each_dropped_one_counted_once():
    """The fold holds ``2 * WORKERS_LIMIT`` rows. Creates fold oldest first, so a full
    fold lets the oldest go for a newer one; the workers running now are never the
    ones refused, and a dropped worker's own log does not count it twice."""
    cap = pw.WORKERS_LIMIT * 2
    names = [f"w{n:02d}" for n in range(cap + 5)]
    mate = _unit(MATE, [_spawned(name) for name in names])
    newest = _unit(names[-1], [_asked("ship it"), _turn(1)], parent=MATE)
    oldest = _unit(names[0], [_turn(1)], parent=MATE)
    value = _fold([mate, newest, oldest])
    assert value["items"][0]["slot"] == names[-1]
    assert value["items"][0]["state"] == "running"
    assert value["counts"]["running"] == 1
    assert value["total"] == cap + 5
    assert value["omitted"] == cap + 5 - pw.WORKERS_LIMIT


def test_the_dropped_workers_it_remembers_are_bounded_and_charged():
    """A long-lived crewmate's dropped workers do not grow the cell without end: the
    fold remembers at most ``2 * WORKERS_LIMIT`` of them, and its row charge counts
    them beside the rows."""
    cap = pw.WORKERS_LIMIT * 2
    mate = _unit(MATE, [_spawned(f"w{n:03d}") for n in range(cap * 4)])
    fold = crew_log._FOLDS["workers"]
    state = crew_log.fold_slot_warm("workers", (mate,), slot=MATE).state
    assert len(state["dropped"]) <= cap
    assert fold.render(state)["omitted"] == cap * 4 - pw.WORKERS_LIMIT
    assert pw._workers_rows(state) == len(state["rows"]) + len(state["dropped"])


def test_a_released_worker_stays_released():
    """After ``session/released`` the slot is not this crewmate's worker any more, so what
    it does next does not move the row."""
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit(
        "w1", [_asked("go"), _turn(1), _done(1), ("session/released", {}), _turn(2)], parent=MATE
    )
    (row,) = _fold([mate, worker])["items"]
    assert (row["state"], row["turns"], row["task"]) == ("released", 1, "go")


class _Tree:
    """The tree's creation records: each slot's creator and when (``created``)."""

    seeded_for_current_store = True

    def __init__(self, created: dict[str, tuple[str, int]]) -> None:
        self._created = created

    def created(self) -> dict[str, tuple[str, int]]:
        return self._created


def test_the_creates_are_read_from_the_crewmates_own_units_only(_floor_monkeypatch):
    from kiro_crew.crew_log import store

    listing = {MATE: ("m1",), "w1": ("a", "b", "c")}
    _floor_monkeypatch.setattr(
        store, "session_units_for_slot", lambda slot, **_kw: listing.get(slot, ())
    )
    assert pw.workers_units(MATE) == ("m1",)
    # The first unit, which holds the task, and the newest two.
    assert pw.worker_units("w1", ("a", "b", "c", "d")) == ("a", "c", "d")
    assert pw.worker_units("w1", ("a", "b")) == ("a", "b")


def test_a_worker_on_its_third_session_keeps_its_original_task():
    mate = _unit(MATE, [_spawned("w1")])
    first = _unit("w1", [_asked("Fix the parser"), _turn(1), _done(1)], parent=MATE)
    later = [_unit("w1", [_asked("status?"), _turn(1)]) for _ in range(2)]
    units = pw.worker_units("w1", (first, *later))
    (row,) = _fold([mate, *units])["items"]
    assert (row["task"], row["state"]) == ("Fix the parser", "running")


def test_a_workers_units_follow_succession_not_the_header_clock(_floor_monkeypatch):
    """A clock that stepped back lists the replacement before the retired session; the
    reader hands the worker's units over in succession order, so the retired one is
    not folded last."""
    from kiro_crew.crew_log import store

    _floor_monkeypatch.setattr(store, "session_units_for_slot", lambda _s, **_kw: ("new", "old"))
    _floor_monkeypatch.setattr(crew_log, "units_in_succession", lambda _s: ("old", "new"))
    assert crew_log._slot_units_for_fold("w1", "worker") == ("old", "new")


class _Nodes:
    """The tree's folded nodes, and its creation records (``creator_of``)."""

    seeded_for_current_store = True

    def __init__(self, parents: dict[str, "str | None"], created: dict[str, str]) -> None:
        from kiro_crew.crew_log.session_tree import TreeNode

        self._nodes = {s: TreeNode(slot=s, parent_slot=p, cycle=False) for s, p in parents.items()}
        self._created = {s: (c, 1) for s, c in created.items()}

    def nodes(self) -> dict[str, Any]:
        return self._nodes

    def created(self) -> dict[str, tuple[str, int]]:
        return self._created


def test_a_worker_is_let_go_when_its_tree_parent_is_not_its_creator(_floor_monkeypatch):
    from kiro_crew.crew_log import session_tree_projection as stp

    tree = _Nodes(
        {"held": MATE, "released": None, "adopted": "member-dm:quill"},
        {"held": MATE, "released": MATE, "adopted": MATE, "unseen": MATE},
    )
    _floor_monkeypatch.setattr(stp, "projection", lambda: tree)
    assert not pw._let_go("held")
    assert pw._let_go("released") and pw._let_go("adopted")
    # Only its creator's create is seen yet: still its creator's.
    assert not pw._let_go("unseen")


def test_the_children_are_the_newest_the_tree_names_a_released_one_included(_floor_monkeypatch):
    """Children come from creation records, newest first, so a worker the crewmate
    let go (no parent edge any more) is still read, and the oldest fall past the cap."""
    from kiro_crew.crew_log import session_tree_projection

    cap = pw.WORKERS_LIMIT * 2
    created = {f"w{n:02d}": (MATE, n) for n in range(cap + 2)}
    created["w-other"] = ("member-dm:quill", 99)
    _floor_monkeypatch.setattr(session_tree_projection, "projection", lambda: _Tree(created))
    children = pw.workers_children(MATE)
    assert children[0] == f"w{cap + 1:02d}" and len(children) == cap
    assert "w00" not in children and "w01" not in children and "w-other" not in children


@pytest.mark.parametrize("now", [None, "member-dm:quill"], ids=["released", "then-adopted"])
def test_a_release_older_than_the_window_is_still_read(_floor_monkeypatch, now):
    """A worker let go -- released, or released and then adopted in the same session, so
    the tree's decision for that unit is the adoption -- that ran two newer sessions
    since: every unit is read, so the row stays released and the later sessions are
    not drawn as this crewmate's."""
    from kiro_crew.crew_log import session_tree_projection as stp

    mate = _unit(MATE, [_spawned("w1")])
    first = _unit("w1", [_asked("go"), _turn(1), ("session/released", {})], parent=MATE)
    later = [_unit("w1", [_asked("other work"), _turn(1)]) for _ in range(2)]
    _floor_monkeypatch.setattr(stp, "projection", lambda: _Nodes({"w1": now}, {"w1": MATE}))
    units = pw.worker_units("w1", (first, *later))
    assert units == (first, *later)
    (row,) = _fold([mate, *units])["items"]
    assert row["state"] == "released" and row["task"] == "go"


def _live_unit(slot: str, entries: list, *, parent: str = "") -> tuple[str, Any]:
    """A unit whose handle the test keeps, to append to it later."""
    unit = f"s-workers-{next(_n)}"
    handle = CrewLog.create(lg.KIND_SESSION, unit, owner="raymond", agent="kirocrew", slot=slot)
    opened: dict[str, Any] = {"agent": "kirocrew-worker", "slot": slot, "model": "", "cwd": "/tmp"}
    opened.update({"owner": "raymond", "resumed": False})
    if parent:
        opened["parent"] = {"slot": parent}
    handle.append("session/opened", opened, src="gateway")
    for entry_type, data in entries:
        handle.append(entry_type, data, src="gateway")
    _slot_of[unit] = slot
    return unit, handle


def _reader_env(_floor_monkeypatch, listing: dict[str, tuple[str, ...]]) -> None:
    from kiro_crew.crew_log import session_tree_projection as stp
    from kiro_crew.crew_log import store

    children = tuple(slot for slot in listing if slot != MATE)
    _floor_monkeypatch.setattr(stp, "created_children", lambda slot: children)
    _floor_monkeypatch.setattr(crew_log, "tree_seeded", lambda: True)
    _floor_monkeypatch.setattr(
        store, "session_units_for_slot", lambda slot, **_kw: listing.get(slot, ())
    )


def test_the_read_merges_the_creates_with_each_workers_row(_floor_monkeypatch):
    """``read_slot_projection(slot, "workers")`` is the creates merged with each worker's
    own row: a created worker with no log yet reads starting, a worker the crewmate did
    not create (its opener names another parent) is not drawn."""
    mate = _unit(MATE, [_spawned("w1"), _spawned("w2")])
    w1 = _unit(
        "w1", [_asked("Fix the parser"), _turn(1), _said("Parser fixed."), _done(1)], parent=MATE
    )
    stranger = _unit("w9", [_turn(1)], parent="member-dm:quill")
    _reader_env(_floor_monkeypatch, {MATE: (mate,), "w1": (w1,), "w2": (), "w9": (stranger,)})
    folded = crew_log.read_slot_projection(MATE, "workers")
    rows = {row["slot"]: row for row in folded.value["items"]}
    assert set(rows) == {"w1", "w2"}
    assert (rows["w1"]["state"], rows["w1"]["task"], rows["w1"]["last_said"]) == (
        "idle",
        "Fix the parser",
        "Parser fixed.",
    )
    assert rows["w2"]["state"] == "starting"
    assert folded.value["counts"]["idle"] == 1 and folded.value["total"] == 2
    assert folded.revision > 0


def test_one_worker_growing_does_not_refold_the_others(_floor_monkeypatch):
    """Each worker is a cell of its own: when one writes, its cell continues and every
    other part answers from the cell it holds, and the merged revision moves."""
    from kiro_crew.crew_log import projection_slot_memo as memo

    mate = _unit(MATE, [_spawned("w1"), _spawned("w2")])
    w1, w1_log = _live_unit("w1", [_asked("one"), _turn(1)], parent=MATE)
    w2, _w2_log = _live_unit("w2", [_asked("two"), _turn(1)], parent=MATE)
    _reader_env(_floor_monkeypatch, {MATE: (mate,), "w1": (w1,), "w2": (w2,)})
    first = crew_log.read_slot_projection(MATE, "workers")

    def cell(slot: str, name: str) -> Any:
        return next(m for k, m in memo._slot_memos.items() if k[1:] == (slot, name))

    held_w2, held_own = cell("w2", "worker"), cell(MATE, "workers")
    w1_log.append("message/sent", {"turn": 1, "text": "Halfway."}, src="gateway")
    second = crew_log.read_slot_projection(MATE, "workers")
    assert cell("w2", "worker") is held_w2 and cell(MATE, "workers") is held_own
    rows = {row["slot"]: row for row in second.value["items"]}
    assert rows["w1"]["last_said"] == "Halfway."
    assert second.revision > first.revision
    # Nothing moved: the same revision, so a consumer re-seeds from nothing.
    assert crew_log.read_slot_projection(MATE, "workers").revision == second.revision


def test_a_released_worker_stays_released_through_the_read(_floor_monkeypatch):
    mate = _unit(MATE, [_spawned("w1")])
    w1 = _unit("w1", [_asked("go"), _turn(1), ("session/released", {}), _turn(2)], parent=MATE)
    _reader_env(_floor_monkeypatch, {MATE: (mate,), "w1": (w1,)})
    (row,) = crew_log.read_slot_projection(MATE, "workers").value["items"]
    assert (row["state"], row["turns"]) == ("released", 1)


def test_an_adoption_after_a_release_still_reads_as_let_go():
    """The real tree: a release and then an adoption in ONE unit leave the adoption as
    that unit's decision, and the slot's parent is the adopter, not its creator."""
    from kiro_crew.crew_log import session_tree as st
    from kiro_crew.crew_log import session_tree_projection as stp

    tree = stp.SessionTreeProjection()
    tree.apply(st.OpenedRecord(sid="u1", slot="w1", created_at=1, parent_slot=MATE))
    tree.apply_edge(st.EdgeRecord(slot="w1", parent_slot=None, at=5, sid="u1", seq=3))
    tree.apply_edge(st.EdgeRecord(slot="w1", parent_slot="member-dm:quill", at=6, sid="u1", seq=4))
    assert tree.nodes()["w1"].parent_slot == "member-dm:quill"
    assert tree.created()["w1"][0] == MATE


def test_the_whole_merged_read_runs_under_its_crewmates_lock(_floor_monkeypatch):
    """A read that captured older parts must not be handed a revision above one that
    read newer parts and finished first, so the lock covers the parts, not only the
    revision."""
    from kiro_crew.config.paths import data_home
    from kiro_crew.crew_log import projection_readers

    mate = _unit(MATE, [_spawned("w1")])
    w1 = _unit("w1", [_asked("go"), _turn(1)], parent=MATE)
    _reader_env(_floor_monkeypatch, {MATE: (mate,), "w1": (w1,)})
    locks = projection_readers._WORKERS_READ_LOCKS
    lock = locks[hash((str(data_home()), MATE)) % len(locks)]
    held: list[bool] = []
    real = pw.worker_units

    def units(child: str, ordered: "tuple[str, ...]") -> "tuple[str, ...]":
        held.append(lock.locked())
        return real(child, ordered)

    _floor_monkeypatch.setattr(crew_log, "worker_units", units)
    crew_log.read_slot_projection(MATE, "workers")
    assert held == [True]


def test_the_lifecycle_emitters_wake_the_eager_folder(_floor_monkeypatch):
    """A create, an adoption and a release each reach ``note_commit`` once they are on
    disk, so the creator's open page hears about them without a reload."""
    from kiro_crew.crew_log import eager, emit
    from kiro_crew.crew_log import session_tree_projection as stp

    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    woken: list[tuple[str, str]] = []
    _floor_monkeypatch.setattr(
        eager, "note_commit", lambda unit, entry_type, *_a, **_k: woken.append((unit, entry_type))
    )
    for sid, slot in (("s-mate", MATE), ("s-w1", "w1"), ("s-w2", "w2")):
        emit.on_session_opened(
            sid, agent="kirocrew", slot=slot, model="opus", cwd="/w", owner="raymond"
        )
    try:
        emit.on_session_spawned("s-mate", creator_slot=MATE, child_slot="w9")
        emit.on_session_adopted("s-w1", slot="w1", parent_slot="member-dm:quill")
        emit.on_session_released("s-w2", slot="w2")
        # A worker's own turn and messages too, a reply long enough to be chunked
        # waking for its citing entry.
        emit.on_turn_started("s-w1", 1, actor="crew", depth=1)
        emit.on_message_received("s-w1", 1, role="user", source="crew", text="go")
        emit.on_message_sent("s-w1", 1, text="x" * 200_000)
        assert emit.flush() is True
        for unit, entry_type in (
            ("s-mate", "session/spawned"),
            ("s-w1", "session/adopted"),
            ("s-w2", "session/released"),
            ("s-w1", "turn/started"),
            ("s-w1", "message/received"),
            ("s-w1", "message/sent"),
        ):
            assert (unit, entry_type) in woken, entry_type
    finally:
        # The emitter keeps each unit's handle, and with it the unit's write lease.
        emit.reset_caches()
        stp.reset_for_tests()


def test_the_tree_names_creators_from_creation_records_a_release_kept():
    from kiro_crew.crew_log import session_tree as st
    from kiro_crew.crew_log import session_tree_projection as stp

    proj = stp.SessionTreeProjection()
    proj.apply_spawned(st.EdgeRecord(slot="w1", parent_slot=MATE, at=5, sid="u-m"))
    proj.apply(st.OpenedRecord(sid="u-w2", slot="w2", created_at=7, parent_slot=MATE))
    proj.apply(st.OpenedRecord(sid="u-w1", slot="w1", created_at=6))
    proj.apply_edge(st.EdgeRecord(slot="w1", parent_slot=None, at=9, sid="u-w1", seq=3))
    assert proj.nodes()["w1"].parent_slot is None  # released
    assert proj.created() == {"w1": (MATE, 5), "w2": (MATE, 7)}


def test_a_workers_entry_wakes_its_creators_cell(_floor_monkeypatch):
    from kiro_crew.crew_log import eager, session_tree_projection

    tree = _Tree({"w1": (MATE, 1)})
    _floor_monkeypatch.setattr(session_tree_projection, "projection", lambda: tree)
    _floor_monkeypatch.setattr(eager, "_slot_of", lambda unit: {"u-w1": "w1", "u-m": MATE}[unit])
    advanced: list[tuple[str, str]] = []
    _floor_monkeypatch.setattr(eager, "_advance", lambda slot, name: advanced.append((slot, name)))
    _floor_monkeypatch.setattr(eager, "_advance_session", lambda unit: None)
    eager._fold_batch({("u-w1", "turn/started", ""): eager._Wake("u-w1", "turn/started", 3)})
    assert advanced == [(MATE, "workers")]
    # The crewmate's own create moves its own cell.
    advanced.clear()
    eager._fold_batch({("u-m", "session/spawned", ""): eager._Wake("u-m", "session/spawned", 9)})
    assert (MATE, "workers") in advanced
    # A worker's own turn does not fold a workers cell under the worker's slot.
    assert ("w1", "workers") not in advanced


def test_a_report_and_a_close_wake_the_workers_creator(_floor_monkeypatch):
    """A report names its conductor's board, and a closer skips the routing: both
    still move the cell of the crewmate that created the worker."""
    from kiro_crew.crew_log import eager, session_tree_projection

    tree = _Tree({"w1": (MATE, 1), "board-x": ("member-dm:quill", 1)})
    _floor_monkeypatch.setattr(session_tree_projection, "projection", lambda: tree)
    _floor_monkeypatch.setattr(eager, "_slot_of", lambda unit: {"u-w1": "w1"}[unit])
    advanced: list[tuple[str, str]] = []
    _floor_monkeypatch.setattr(eager, "_advance", lambda slot, name: advanced.append((slot, name)))
    _floor_monkeypatch.setattr(eager, "_advance_session", lambda unit: None)
    report = eager._Wake("u-w1", "work/recorded", 4, board="board-x")
    eager._fold_batch({("u-w1", "board-x", "work/recorded"): report})
    assert (MATE, "workers") in advanced
    assert ("member-dm:quill", "workers") not in advanced
    advanced.clear()
    closer = eager._Wake("u-w1", "session/closed", 7)
    eager._fold_batch({("u-w1", "", "session/closed"): closer}, {"u-w1": 7})
    assert advanced.count((MATE, "workers")) == 1


def test_the_task_skips_the_relays_provenance_line():
    """``session_send`` prefixes ``[sent by session ... via session_send]``: who sent it,
    not the task, so the task is the first line after it."""
    mate = _unit(MATE, [_spawned("w1")])
    relayed = f"[sent by session {MATE} via session_send]\n\nFix the broken links.\nThen commit."
    worker = _unit("w1", [_asked(relayed)], parent=MATE)
    (row,) = _fold([mate, worker])["items"]
    assert row["task"] == "Fix the broken links."


def _task_of(text: str) -> str:
    mate = _unit(MATE, [_spawned("w1")])
    worker = _unit(
        "w1", [_asked(f"[sent by session {MATE} via session_send]\n\n{text}")], parent=MATE
    )
    (row,) = _fold([mate, worker])["items"]
    return row["task"]


def test_the_task_skips_a_dispatch_preamble_and_takes_the_ask():
    """Pod crewmates opened each dispatch with ``You are a worker on ...``, so every
    closed worker's row read as that preamble rather than its task."""
    # A labelled ask wins, past the preamble and any context before it.
    labelled = (
        "You are a worker on the `docs` repo. Work ONLY in this worktree.\n"
        "CONTEXT: the FAQ links to a missing page.\n"
        "TASK (read-only): Find the two fixes for each broken link.\n"
        "GOAL: never read, the first heading wins."
    )
    assert _task_of(labelled) == "Find the two fixes for each broken link."
    assert _task_of("You are a worker.\nGOAL (issue 3): add a reader.") == "add a reader."
    # With no heading, the first line past the preamble.
    assert (
        _task_of("You are a worker. Do exactly this.\nFix the two links.") == "Fix the two links."
    )
    # A message that is only a preamble keeps it rather than reading empty.
    assert _task_of("You are a worker on the docs repo.") == "You are a worker on the docs repo."
    # A line that merely mentions a heading word mid-sentence is not a heading.
    assert (
        _task_of("Check the task: it is long, keep it.") == "Check the task: it is long, keep it."
    )
