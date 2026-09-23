"""Adoption and release: the records that MOVE a session in the tree.

The creating edge is stamped once and never rewritten, so every property here is about
a second kind of statement living beside it. Four things can break independently and
each gets its own test:

* the FOLD prefers the newest decision over the creating citation, and over an older
  decision, whatever order the records arrive in;
* the fold's cycle colouring still runs over the applied decisions, because a takeover
  can be recorded against a reading of the tree that has since moved;
* the PROJECTION advances only after the append succeeded, refuses a decision that
  arrives out of order, and hands out the same object when nothing moved;
* a COLD scan with no checkpoint reaches the same tree as the fold, which is the whole
  claim that lets the checkpoint be a shortcut rather than an authority.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import (
    EdgeRecord,
    OpenedRecord,
    SessionTree,
    edge_record,
    edge_supersedes,
    fold_tree,
    latest_edges,
    log_rank_of,
)
from kiro_crew.crew_log.session_tree_projection import (
    CHECKPOINT_NAME,
    SessionTreeProjection,
)
from kiro_crew.crew_log.store import read_last_tree_edge
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test writes into its own data home, and no write is armed on the real pool.

    Same shape as the projection suite's fixture and for the same two reasons: the
    process-wide projection is bound to ONE store, so a test inheriting another test's
    fold would read another store's records; and ``_debounced_write`` saves outside the
    lock, so a worker that already passed the epoch check could land a file in this
    test's tmp home after pytest considers it finished.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()


def _rec(
    sid: str,
    slot: str,
    created: int = 1,
    parent: str | None = None,
    previous: str | None = None,
) -> OpenedRecord:
    return OpenedRecord(
        sid=sid, slot=slot, created_at=created, parent_slot=parent, previous_sid=previous
    )


def _parents(nodes) -> dict[str, str | None]:
    return {slot: node.parent_slot for slot, node in nodes.items()}


def _log(sid: str, slot: str) -> CrewLog:
    return CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)


def _opened(handle: CrewLog, slot: str, *, parent: str | None = None) -> None:
    data = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent is not None:
        data["parent"] = {"slot": parent}
    handle.append("session/opened", data, src=GATEWAY)


def _checkpoint_path():
    from kiro_crew.crew_log.store import crew_log_root

    return crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME


# ── the fold ───────────────────────────────────────────────────────────────


def test_an_adoption_replaces_the_creating_citation_and_carries_the_subtree():
    """The takeover case, which is the whole feature: one record moves a branch.

    D adopts A, and B and C follow with no record of their own -- they cite A's SLOT,
    so nothing about them has to change for the tree under them to move. A fold that
    keyed children on a PATH would need an entry per descendant here.
    """
    records = [
        _rec("s-a", "A", created=1),
        _rec("s-b", "B", created=2, parent="A"),
        _rec("s-c", "C", created=3, parent="B"),
        _rec("s-d", "D", created=4),
    ]
    adopted = fold_tree(records, [EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a")])
    assert _parents(adopted) == {"A": "D", "B": "A", "C": "B", "D": None}


def test_a_release_clears_the_edge_that_the_opening_entry_recorded():
    """The one record that RETRACTS a parent.

    B was opened by A, and the release says it has none now -- which a
    ``session/opened`` carrying no parent deliberately cannot say, because that only
    means the entry did not repeat a creator.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-b", "B", created=2, parent="A")]
    released = fold_tree(records, [EdgeRecord(slot="B", parent_slot=None, at=50, sid="s-b")])
    assert _parents(released) == {"A": None, "B": None}


def test_the_newest_decision_wins_whatever_order_the_records_arrive_in():
    """Order-independence, which is the property a stale checkpoint plus a replayed
    tail actually needs.

    The release is newer than the adoption, so the answer is "no parent" whether the
    fold sees it first or second. Taking the LAST arrival instead would put the session
    back under a parent that already let it go, and it would stay there until the
    process restarted.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-d", "D", created=2)]
    adopt = EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a", seq=2)
    release = EdgeRecord(slot="A", parent_slot=None, at=200, sid="s-a", seq=3)
    assert _parents(fold_tree(records, [adopt, release]))["A"] is None
    assert _parents(fold_tree(records, [release, adopt]))["A"] is None
    # And the reverse pairing: an adoption written after a release still wins.
    later_adopt = EdgeRecord(slot="A", parent_slot="D", at=300, sid="s-a", seq=4)
    assert _parents(fold_tree(records, [later_adopt, release]))["A"] == "D"


def test_two_decisions_in_one_millisecond_are_ordered_by_the_citing_log():
    """The tie-break, so two runs over the same records agree.

    Wall-clock milliseconds collide, and a fold whose answer depended on input order
    would flip between two scans of the same files.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-d", "D", created=2)]
    same_ms = [
        EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a1", seq=2),
        EdgeRecord(slot="A", parent_slot=None, at=100, sid="s-a2", seq=2),
    ]
    assert _parents(fold_tree(records, same_ms))["A"] is None
    assert _parents(fold_tree(records, list(reversed(same_ms))))["A"] is None
    assert latest_edges(same_ms)["A"].sid == "s-a2"


def test_an_adoption_that_would_close_a_loop_is_coloured_as_a_cycle_by_the_fold():
    """The fold-time guard, which is not the same guard as the tool's.

    The tool refuses a target already above the caller, reading the tree as it stands
    then. This is the case that reading cannot cover: a decision reaching the fold from
    a checkpoint and a replayed tail, in an order no writer saw. Every slot on the loop
    is marked, so a consumer nests none of them rather than rendering a branch that
    eats itself.
    """
    records = [
        _rec("s-a", "A", created=1),
        _rec("s-b", "B", created=2, parent="A"),
        _rec("s-c", "C", created=3, parent="B"),
    ]
    nodes = fold_tree(records, [EdgeRecord(slot="A", parent_slot="C", at=100, sid="s-a")])
    assert sorted(slot for slot, node in nodes.items() if node.cycle) == ["A", "B", "C"]


def test_a_decision_for_a_slot_with_no_log_changes_nothing():
    """An edge onto a node that does not exist is dropped, the same rule the fold
    already applies to a cited creator with no log of its own."""
    records = [_rec("s-a", "A", created=1)]
    ghost = EdgeRecord(slot="GHOST", parent_slot="A", at=100, sid="s-ghost")
    assert _parents(fold_tree(records, [ghost])) == {"A": None}


def test_an_adoption_naming_a_parent_with_no_log_keeps_the_citation_unfollowed():
    """The citation is the child's record and is retained; only the FOLLOWING stops.

    Same posture as an opened entry citing a creator that has no log: the reader is
    told what the session says about itself, and the tree does not invent a node.
    """
    records = [_rec("s-a", "A", created=1)]
    nodes = fold_tree(records, [EdgeRecord(slot="A", parent_slot="GONE", at=100, sid="s-a")])
    assert nodes["A"].parent_slot == "GONE"
    assert nodes["A"].cycle is False


# ── the record builder ─────────────────────────────────────────────────────


def _entry(entry_type: str, data: dict, *, at: int = 7) -> object:
    from kiro_crew.crew_log.schema import Entry

    return Entry(type=entry_type, data=data, src=GATEWAY, seq=2, time=at)


def test_an_adoption_with_no_parent_slot_is_refused_rather_than_read_as_a_release():
    """The strongest possible meaning is exactly what a malformed entry must not get.

    A release detaches a subtree. Inferring one from an adoption whose ``parent`` did
    not survive would do that on the strength of damage.
    """
    assert edge_record("A", "s-a", _entry("session/adopted", {})) is None
    assert edge_record("A", "s-a", _entry("session/adopted", {"parent": {}})) is None
    assert edge_record("A", "s-a", _entry("session/adopted", {"parent": "D"})) is None


def test_a_release_needs_no_parent_and_folds_as_no_parent():
    built = edge_record("A", "s-a", _entry("session/released", {}))
    assert built is not None and built.parent_slot is None and built.at == 7


def test_an_over_long_key_is_refused_rather_than_truncated():
    """A truncated slot key is a DIFFERENT key: it matches nothing, or it matches
    another session."""
    from kiro_crew.validation import MAX_SHORT_STRING

    long_slot = "x" * (MAX_SHORT_STRING + 1)
    assert edge_record(long_slot, "s-a", _entry("session/released", {})) is None
    adopt = _entry("session/adopted", {"parent": {"slot": long_slot}})
    assert edge_record("A", "s-a", adopt) is None


def test_an_entry_of_another_type_contributes_no_decision():
    assert edge_record("A", "s-a", _entry("turn/started", {"turn": 1})) is None
    assert edge_record("A", "s-a", None) is None


# ── the projection ─────────────────────────────────────────────────────────


def test_the_projection_folds_a_decision_and_keeps_the_same_object_when_it_repeats():
    """dsh's same-reference rule, extended to decisions.

    A decision already held is not a change, so a replay of a tail the checkpoint
    already covered costs neither a re-render nor a checkpoint write.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    before = proj.nodes()
    edge = EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a")
    proj.apply_edge(edge)
    after = proj.nodes()
    assert after["A"].parent_slot == "D"
    assert after is not before
    proj.apply_edge(edge)
    assert proj.nodes() is after


def test_the_projection_refuses_a_decision_that_arrives_out_of_order():
    """A replay walks units in directory order, so an older decision can arrive last.

    Taking the last arrival would re-parent a session that has already been released,
    with nothing to correct it until the process restarted.
    """
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot=None, at=200, sid="s-a"))
    stale = proj.nodes()
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.nodes() is stale
    assert proj.nodes()["A"].parent_slot is None


def test_a_checkpoint_round_trip_keeps_the_decisions():
    """The checkpoint is a shortcut, and a shortcut that lost adoptions would be an
    authority for the wrong answer: it would load as "nothing was ever adopted".

    Real units on disk, because the replay drops a held record whose unit is gone -- so a
    checkpoint of hand-built records would be reconciled away before the decision could be
    asserted, and the test would pass or fail for the wrong reason. The DECISION is on disk
    for the same reason one step further: the replay also drops a held edge that a complete
    read of the log contradicts, which is how a decision retention removed stops being the
    tree's answer. The emitter never folds an edge it has not appended, so a decision only
    in memory is not a state production reaches.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    written = handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    seq = written.seq
    at = written.time
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=at, sid="s-a", seq=seq))
    assert proj.flush_checkpoint() is True
    payload = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
    assert payload["edges"] == [{"slot": "A", "at": at, "sid": "s-a", "seq": seq, "parent": "D"}]

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["A"].parent_slot == "D"


def test_a_checkpoint_with_no_edges_key_is_discarded_rather_than_read_as_no_adoptions():
    """The one wrong answer that looks exactly like a right one.

    This build writes the key whether or not anything was adopted, so a payload that
    lacks it is damaged rather than old -- and reading it as "no adoptions" would serve
    a tree with a takeover silently missing.
    """
    proj = SessionTreeProjection()
    proj.ensure_seeded()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.flush_checkpoint() is True
    path = _checkpoint_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.pop("edges")
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert stp._load_checkpoint() is None


def test_forgetting_a_unit_drops_the_decisions_it_recorded():
    """A unit that is gone is not evidence for where its slot hangs."""
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-a"))
    assert proj.nodes()["A"].parent_slot == "D"
    proj.forget("s-a")
    assert "A" not in proj.nodes()


def test_forgetting_the_winning_log_leaves_the_surviving_logs_decision():
    """Deleting the unit whose decision WON must not take the slot's other decisions.

    A slot outlives its ACP session, so two logs can each hold a decision for it and the
    newer log's is the one the fold serves. Holding one winner per slot would discard the
    older log's decision on arrival -- and then deleting the winner leaves the slot with
    no decision at all, so the fold falls back to the OPENING edge and a session that had
    been taken over silently moves back to the conductor that opened it. Decisions are
    held per ``(slot, sid)`` for exactly this, so the survivor is still there to be read.
    """
    proj = SessionTreeProjection()
    # One slot, two logs: the replacement log is newer by the header's ``created_at``.
    proj.apply(_rec("s-old", "A", created=1, parent="P"))
    proj.apply(_rec("s-new", "A", created=9, parent="P"))
    proj.apply(_rec("s-p", "P", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply(_rec("s-e", "E", created=3))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-old", seq=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="E", at=200, sid="s-new", seq=2))
    # The newer LOG decides, which is the fold's own rule.
    assert proj.nodes()["A"].parent_slot == "E"

    proj.forget("s-new")
    # The older log's decision survives and now decides -- not the opening citation.
    assert proj.nodes()["A"].parent_slot == "D"


def test_a_backward_clock_step_does_not_let_the_earlier_decision_win():
    """Ordering comes from the STORE's sequence, never from a clock.

    A clock that moves backward between two appends on one log -- an NTP correction, a
    VM resume -- gives the later entry the smaller timestamp. Comparing timestamps would
    then keep the superseded edge, silently, until some later decision happened to land
    past the held time. ``seq`` is assigned by the writer and only increases, so it
    cannot be inverted this way.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-d", "D", created=2)]
    adopt = EdgeRecord(slot="A", parent_slot="D", at=5000, sid="s-a", seq=4)
    # Written AFTER the adoption, and the clock went back 4 seconds in between.
    release = EdgeRecord(slot="A", parent_slot=None, at=1000, sid="s-a", seq=9)
    assert edge_supersedes(release, adopt) is True
    assert edge_supersedes(adopt, release) is False
    for order in ([adopt, release], [release, adopt]):
        assert _parents(fold_tree(records, order))["A"] is None


def test_decisions_in_two_logs_of_one_slot_are_placed_by_the_newer_log():
    """Seqs are not comparable across logs: the log that replaces one starts again.

    So the newer LOG decides, ranked by the same key the record fold sorts by, and the
    decision with the LOWER seq wins when its log is the newer one.
    """
    old_log = _rec("s-old", "A", created=10)
    new_log = _rec("s-new", "A", created=20)
    rank = log_rank_of([old_log, new_log])
    in_old = EdgeRecord(slot="A", parent_slot="D", at=100, sid="s-old", seq=99)
    in_new = EdgeRecord(slot="A", parent_slot=None, at=100, sid="s-new", seq=2)
    assert edge_supersedes(in_new, in_old, rank) is True
    assert edge_supersedes(in_old, in_new, rank) is False
    records = [old_log, new_log, _rec("s-d", "D", created=1)]
    assert _parents(fold_tree(records, [in_old, in_new]))["A"] is None


def test_the_projection_keeps_the_higher_sequence_whatever_the_timestamps_say():
    proj = SessionTreeProjection()
    proj.apply(_rec("s-a", "A", created=1))
    proj.apply(_rec("s-d", "D", created=2))
    proj.apply_edge(EdgeRecord(slot="A", parent_slot=None, at=1000, sid="s-a", seq=9))
    held = proj.nodes()
    proj.apply_edge(EdgeRecord(slot="A", parent_slot="D", at=5000, sid="s-a", seq=4))
    assert proj.nodes() is held
    assert proj.nodes()["A"].parent_slot is None


def test_the_cold_read_finds_a_decision_the_tail_window_cannot_reach():
    """The cold rebuild reads the WHOLE log, because losing a decision here is not a
    missing edge but a wrong one: the fold falls back to the creating edge and shows the
    session under a parent that released it, with nothing to correct it.
    """
    from kiro_crew.crew_log.store import _TAIL_WINDOW, find_last_tree_edge, read_last_tree_edge

    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    # Bury it: enough bytes after the decision that the bounded window cannot see it.
    filler = "x" * 900
    written = 0
    while written < _TAIL_WINDOW * 2:
        handle.append(
            "message/received",
            {"turn": 1, "role": "user", "source": "peer", "text": filler},
            src=GATEWAY,
        )
        written += 1000
    del handle

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    assert (
        read_last_tree_edge(crew_store.newest_segment(directory)) is None
    ), "the window was supposed to be outrun; the filler is too small"
    entry = find_last_tree_edge(directory)
    assert entry is not None and entry.data["parent"]["slot"] == "D"

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.nodes()["A"].parent_slot == "D"


# ── the store read, and cold-scan parity ───────────────────────────────────


def _unit_with_decision(sid: str, slot: str, *, parent: str | None, adopt_to: str | None):
    """A real unit on disk whose decision sits PAST its head lines.

    The turns in between are the point: a reader of the head cannot see the decision,
    which is why the store grew a tail read for it.
    """
    handle = _log(sid, slot)
    _opened(handle, slot, parent=parent)
    for turn in range(3):
        handle.append("turn/started", {"turn": turn, "actor": "user", "depth": 0}, src=GATEWAY)
    if adopt_to is None:
        handle.append("session/released", {}, src=GATEWAY)
    else:
        handle.append("session/adopted", {"parent": {"slot": adopt_to}}, src=GATEWAY)
    del handle


def test_the_store_reads_a_decision_that_sits_behind_later_entries():
    """The head read cannot see it, so the tail read must."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)
    entry = read_last_tree_edge(segment)
    assert entry is not None and entry.type == "session/adopted"
    assert entry.data["parent"]["slot"] == "D"


def test_the_store_reads_the_newest_decision_when_a_log_holds_two():
    """A session adopted and then released reads as released."""
    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src=GATEWAY)
    handle.append("session/released", {"previous_parent": {"slot": "D"}}, src=GATEWAY)
    del handle
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/released"


def test_a_decision_is_not_a_lifecycle_entry_so_retention_still_sees_the_unit_as_open():
    """The set that decides open-versus-closed authorizes a DELETE, so an adoption must
    not be in it: a unit whose newest such entry was an adoption would never expire.
    """
    assert "session/adopted" not in crew_store._LIFECYCLE_TYPES
    assert "session/released" not in crew_store._LIFECYCLE_TYPES
    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/closed", {"reason": "reset"}, src=GATEWAY)
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    tail = crew_store._scan_tail(crew_store.newest_segment(directory))
    assert crew_store._last_lifecycle_entry(tail).type == "session/closed"


def test_a_cold_scan_with_no_checkpoint_reaches_the_same_tree_as_the_fold():
    """The claim that lets the checkpoint be a shortcut rather than an authority.

    Written to disk, then read by a scanner that has never seen a checkpoint -- with
    the decisions sitting past each unit's head lines, which is the case a head-only
    scan gets wrong.
    """
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    _unit_with_decision("s-b", "B", parent="A", adopt_to=None)
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    scanned = SessionTree().reading(with_edges=True)
    assert _parents(scanned.nodes) == {"A": "D", "B": None, "D": None}

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert _parents(proj.nodes()) == _parents(scanned.nodes)


def test_a_cold_seed_keeps_every_logs_decision_not_one_per_slot():
    """A seed must install each unit's decision, not the winner it computed itself.

    Two things rest on that. Choosing between LOGS needs the record order that ranks
    them, which the seed does not have when it reduces on its own -- with no ranking the
    comparison falls back to the timestamp, so a clock that stepped backward between the
    two logs would seed the OLDER decision and a cold rebuild would restore a lineage
    that had been superseded. And a decision discarded at seed time is not there later:
    deleting the winning unit would then leave the slot with no decision rather than with
    the survivor's.

    Asserted from a real store through the delete, because that is the shape both
    failures share -- the survivor has to still be in the projection to be read.
    """
    _unit_with_decision("s-1-old", "A", parent="P", adopt_to="D")
    _unit_with_decision("s-2-new", "A", parent="P", adopt_to="E")
    for sid, slot in (("s-p", "P"), ("s-d", "D"), ("s-e", "E")):
        handle = _log(sid, slot)
        _opened(handle, slot)
        del handle

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    # The newer LOG decides, ranked by the records the same fold already sorts by.
    assert proj.nodes()["A"].parent_slot == "E"

    proj.forget("s-2-new")
    # Present only because the seed installed it too.
    assert proj.nodes()["A"].parent_slot == "D"


def test_a_scan_without_edges_reports_where_each_slot_was_opened():
    """The default is the cheaper read and a DIFFERENT question, so it is pinned
    rather than left to be discovered by a caller that wanted the other one."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle
    assert _parents(SessionTree().reading().nodes) == {"A": None, "D": None}


def test_a_stale_checkpoint_is_corrected_by_the_replay_for_a_unit_it_already_holds():
    """The reason the decision pass covers held names and not only new ones.

    A decision lives at the END of a log, so the unit a checkpoint already holds is
    exactly where a decision it missed will be. A replay that skipped those names would
    make the checkpoint authoritative for adoptions, and a stale one would leave a
    session hanging under a parent that released it.
    """
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    # A checkpoint that knows both units and NO decisions -- the shape a process that
    # died between the adopt append and the debounced write leaves behind. Written by
    # hand, because a projection seeded from THIS disk would have read the decision
    # already and its checkpoint would not be stale.
    seeded = SessionTreeProjection()
    seeded.ensure_seeded()
    assert seeded.flush_checkpoint() is True
    path = _checkpoint_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["edges"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")
    stp.reset_for_tests()

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["A"].parent_slot == "D"


def test_a_removed_unit_takes_its_decision_out_of_the_replay():
    """The same rule ``forget`` applies, on the cold path."""
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle
    first = SessionTreeProjection()
    first.ensure_seeded()
    assert first.nodes()["A"].parent_slot == "D"
    assert first.flush_checkpoint() is True

    crew_store.remove_unit(lg.KIND_SESSION, "s-a", guard=lambda _directory: True)
    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert "A" not in revived.nodes()


# ── the emitter ────────────────────────────────────────────────────────────


def test_the_emitter_writes_the_entry_and_advances_the_fold():
    """Durability first, then memory: the fold is advanced from inside the job, after
    the append returned -- so the disk can never hold a decision the memory lacks."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_opened(
        "s-d", agent="kirocrew", slot="D", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted("s-a", slot="A", parent_slot="D", parent_sid="s-d")
    assert emit.flush(timeout=5.0) is True

    assert stp.projection().nodes()["A"].parent_slot == "D"
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/adopted"
    assert entry.data == {"parent": {"slot": "D", "sid": "s-d"}}


def test_the_emitter_reports_the_durable_outcome_to_a_waiting_caller():
    """``on_settled`` is how a verb learns the append actually landed.

    The emitter hands the entry to the writer and returns, so a caller that must not
    claim a takeover it did not persist has no other way to know. Called once, with
    ``True`` only after the entry is on disk.
    """
    settled: list[bool] = []
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_opened(
        "s-d", agent="kirocrew", slot="D", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted(
        "s-a", slot="A", parent_slot="D", parent_sid="s-d", on_settled=settled.append
    )
    assert emit.flush(timeout=5.0) is True
    assert settled == [True]
    # And the entry really is there, so ``True`` is not merely "the job ran".
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/adopted"


def test_the_emitter_reports_failure_when_there_is_no_log_to_write_to():
    """A job that finds no log appends NOTHING, and that must not reach the caller as a
    completed write: the terminal cleanup runs either way, so the two would otherwise be
    indistinguishable and the verb would report a takeover of a session it never
    touched."""
    settled: list[bool] = []
    emit.on_session_adopted("s-missing", slot="A", parent_slot="D", on_settled=settled.append)
    assert emit.flush(timeout=5.0) is True
    assert settled == [False]


def test_the_emitter_reports_failure_for_an_entry_it_refuses_outright():
    """Neither side of the edge means nothing to fold, so there is nothing to wait for --
    and the caller is told that rather than left waiting for a write never submitted."""
    settled: list[bool] = []
    emit.on_session_adopted("s-a", slot="", parent_slot="D", on_settled=settled.append)
    assert settled == [False]
    released: list[bool] = []
    emit.on_session_released("", slot="A", on_settled=released.append)
    assert released == [False]


def test_the_settle_signal_reports_the_append_not_the_absence_of_a_drop():
    """The arm that must not read as success.

    ``_submit`` runs an entry's terminal cleanup for EVERY outcome, and only some of them
    run the permanent-drop hook: an entry rejected for crossing the buffer's memory
    ceiling is finished without it, which is exactly the wedged-writer condition the
    ceiling exists for. A settle that inferred success from that hook's absence would
    tell the caller its takeover landed while nothing was appended and the projection
    never moved. So the signal is what the job DID -- ``wrote`` is set after
    ``log.append`` returns -- and a terminal cleanup with no append behind it is False.
    """
    told: list[bool] = []
    settle = emit._tree_settle_hooks(told.append)
    settle.after()
    assert told == [False], "a cleanup with no append behind it is not a landed write"

    wrote: list[bool] = []
    landed = emit._tree_settle_hooks(wrote.append)
    landed.wrote()
    landed.after()
    assert wrote == [True]

    # Told once, whatever else arrives: the caller is waiting on a single answer.
    again: list[bool] = []
    once = emit._tree_settle_hooks(again.append)
    once.wrote()
    once.after()
    once.after()
    assert again == [True]


def test_the_emitter_records_the_parent_a_takeover_replaced():
    """``previous_parent`` is audit and the fold ignores it, so the fold is asserted to
    show only the NEW parent while the entry keeps both."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted(
        "s-a",
        slot="A",
        parent_slot="NEW",
        previous_parent_slot="OLD",
        previous_parent_sid="s-old",
    )
    assert emit.flush(timeout=5.0) is True
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry.data["previous_parent"] == {"slot": "OLD", "sid": "s-old"}
    assert stp.projection().nodes()["A"].parent_slot == "NEW"


def test_a_release_written_by_the_emitter_returns_the_session_to_a_root():
    emit.on_session_opened(
        "s-d", agent="kirocrew", slot="D", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_opened(
        "s-a",
        agent="kirocrew",
        slot="A",
        model="opus",
        cwd="/w",
        owner="raymond",
        parent_slot="D",
    )
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().nodes()["A"].parent_slot == "D"

    emit.on_session_released("s-a", slot="A", previous_parent_slot="D", previous_parent_sid="s-d")
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().nodes()["A"].parent_slot is None
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry.data == {"previous_parent": {"slot": "D", "sid": "s-d"}}


def test_the_emitter_writes_nothing_without_both_sides_of_the_edge():
    """A decision with no slot names nothing a reader could fold, so it is not
    written -- rather than written and silently ignored."""
    emit.on_session_opened(
        "s-a", agent="kirocrew", slot="A", model="opus", cwd="/w", owner="raymond"
    )
    emit.on_session_adopted("s-a", slot="", parent_slot="D")
    emit.on_session_adopted("s-a", slot="A", parent_slot="")
    emit.on_session_released("s-a", slot="")
    assert emit.flush(timeout=5.0) is True
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    assert read_last_tree_edge(crew_store.newest_segment(directory)) is None
    assert _store_name("s-a") == directory.name


def test_a_store_that_cannot_be_listed_is_not_answered_as_no_decision(monkeypatch):
    """A listing that fails has established NOTHING, so it must raise rather than return
    ``None``.

    Both callers cache what this returns. Answered as "no decision", the projection's seed
    installs the creating edge for a session that was moved, and the scanner stores that as
    its verdict for the segment's whole stat identity -- so one moment's fault becomes the
    tree's standing answer. Each caller catches ``OSError`` and marks its scan incomplete
    instead, which is the honest reading.
    """
    from pathlib import Path

    from kiro_crew.crew_log.store import find_last_tree_edge

    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    # The decision IS there -- so a ``None`` below could only come from the swallow.
    assert find_last_tree_edge(directory) is not None

    def _refuse(_self):
        raise OSError(5, "input/output error")

    monkeypatch.setattr(Path, "iterdir", _refuse)
    with pytest.raises(OSError):
        find_last_tree_edge(directory)


def test_an_unlistable_directory_is_reported_incomplete_not_as_no_decision(monkeypatch):
    """The scanner's second value is what a DECIDING reader consults, so an unreadable
    directory must set it.

    ``newest_segment`` collapsed absent, empty and unlistable into one ``None``, and the
    scanner read all three as a complete "no decision" -- which leaves a stale cached edge
    standing as the tree's answer, and lets a release authorize the parent that gave the
    session up. Absent and empty ARE answers; a failed listing is not.
    """
    from pathlib import Path

    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    tree = SessionTree()
    assert _parents(tree.reading(with_edges=True).nodes)["A"] == "D"

    real = Path.iterdir

    def _refuse_one(self):
        if self.name == _store_name("s-a"):
            raise OSError(5, "input/output error")
        return real(self)

    monkeypatch.setattr(Path, "iterdir", _refuse_one)
    reading = SessionTree().reading(with_edges=True)
    monkeypatch.undo()
    assert reading.incomplete is True


def test_a_complete_read_that_finds_no_decision_drops_the_checkpoints_edge():
    """A decision retention removed must stop being the tree's answer.

    The replay skipped a unit whose complete read found no decision, which KEPT whatever
    the checkpoint loaded -- so an edge whose segment is gone from the log pinned the slot
    under that parent on every restart, with nothing on disk behind it and no later scan
    able to contradict it. A complete read finding nothing is the only thing that can.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    written = handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.nodes()["A"].parent_slot == "D"
    assert proj.flush_checkpoint() is True

    # Retention takes the decision out of the log, leaving the unit and its header.
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)
    kept = [
        line
        for line in segment.read_text(encoding="utf-8").splitlines(keepends=True)
        if '"session/adopted"' not in line
    ]
    segment.write_text("".join(kept), encoding="utf-8")
    assert crew_store.find_last_tree_edge(directory) is None

    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert (
        revived.nodes()["A"].parent_slot is None
    ), "the checkpoint's edge outlived its removal from the log"
    assert written.seq > 0


def test_a_failed_stat_in_the_whole_file_scan_is_not_answered_as_no_decision(monkeypatch):
    """The third site of the same mistake, on the path the bounded window could not answer.

    ``_scan_whole_for_types`` stats the segment to decide whether the window already covered
    it. A swallowed stat returned ``None``, which ``find_last_tree_edge`` reports as "this
    unit records no decision" -- and that verdict is CACHED against the segment's unchanged
    dev/ino/size, so an idle unit never re-reads it and nothing corrects it. The ``open``
    immediately below already propagates, so the guard made one function answer two
    different ways about the same file.
    """
    from pathlib import Path

    from kiro_crew.crew_log.store import _TAIL_WINDOW, find_last_tree_edge, read_last_tree_edge

    handle = _log("s-a", "A")
    _opened(handle, "A")
    handle.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    filler = "x" * 900
    written = 0
    while written < _TAIL_WINDOW * 2:
        handle.append(
            "message/received",
            {"turn": 1, "role": "user", "source": "peer", "text": filler},
            src=GATEWAY,
        )
        written += 1000
    del handle

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)
    # The window must be outrun, or the whole-file scan is never reached and the test
    # would pass without exercising the guard at all.
    assert read_last_tree_edge(segment) is None
    assert find_last_tree_edge(directory) is not None

    real = Path.stat

    def _refuser(fail_from: int):
        seen: list[int] = []

        def _stat(self, *a, **kw):
            if self == segment:
                seen.append(1)
                if len(seen) >= fail_from:
                    raise OSError(5, "input/output error")
            return real(self, *a, **kw)

        return _stat

    # The unit itself: its ONE stat of the segment fails, and it must not answer ``None``.
    monkeypatch.setattr(Path, "stat", _refuser(1))
    with pytest.raises(OSError):
        crew_store._scan_whole_for_types(segment, crew_store._TREE_EDGE_TYPES)
    monkeypatch.undo()

    # And the walk above it does not convert that into "no decision". Its bounded window
    # stats the segment FIRST, so the scan's own stat is the second one.
    monkeypatch.setattr(Path, "stat", _refuser(2))
    with pytest.raises(OSError):
        find_last_tree_edge(directory)


def test_a_clock_rollback_between_two_sessions_does_not_invert_the_cross_log_order():
    """The log ranking must not be a clock value either.

    ``seq`` already made two decisions in ONE log clock-free. Across two logs of one slot
    the rank led with the header's ``created_at``, so the rollback the comparison documents
    as ordinary -- an NTP correction, a VM resume, here landing between the old session
    closing and its successor opening -- made the SUCCESSOR's log rank BELOW its
    predecessor's. The release in the newer log then lost to the adoption in the older one,
    and the sidebar kept a parent that had let the session go, with the loser written into
    the checkpoint and no path that re-reads it.

    ``previous_sid`` is what the store wrote about which session replaced which, so the
    chain answers where the clock cannot.
    """
    # The successor's header carries the EARLIER stamp: that is the rollback.
    old_log = _rec("s-old", "A", created=5000)
    new_log = _rec("s-new", "A", created=10, previous="s-old")
    rank = log_rank_of([old_log, new_log])
    assert rank["s-new"] > rank["s-old"], "the succession chain must outrank the clock"

    adopt_in_old = EdgeRecord(slot="A", parent_slot="D", at=5000, sid="s-old", seq=99)
    release_in_new = EdgeRecord(slot="A", parent_slot=None, at=10, sid="s-new", seq=2)
    assert edge_supersedes(release_in_new, adopt_in_old, rank) is True
    assert edge_supersedes(adopt_in_old, release_in_new, rank) is False

    records = [old_log, new_log, _rec("s-d", "D", created=1)]
    for order in ([adopt_in_old, release_in_new], [release_in_new, adopt_in_old]):
        assert _parents(fold_tree(records, order))["A"] is None


def test_the_record_fold_and_the_decision_ranking_share_one_log_order():
    """Two orderings of one slot's logs would let the tree contradict itself.

    The creating citation comes from a slot's OLDEST record and the decisions are ranked by
    log; if those two used different keys, a decision could be placed in a log the record
    fold considers older. Both now read ``log_rank_of``, so the rollback above moves them
    together.
    """
    old_log = _rec("s-old", "A", created=5000, parent="P")
    new_log = _rec("s-new", "A", created=10, previous="s-old", parent="Q")
    records = [old_log, new_log, _rec("s-p", "P", created=1), _rec("s-q", "Q", created=2)]
    # The chain start is the oldest log, so its citation is the one that stands -- even
    # though its header stamp is the LATER of the two.
    assert _parents(fold_tree(records))["A"] == "P"


def test_a_previous_sid_cycle_does_not_hang_the_ranking():
    """Forged or damaged records can point ``previous_sid`` in a loop, and the walk has to
    end -- and end the SAME way whichever sid it reaches first.

    Breaking the loop at the first member visited is the trap: it makes that member the
    chain start, so the rank depends on iteration order and ``fold_tree`` stops answering
    the same tree for the same input in a different order. Every log on a cycle is given
    depth 0 instead, which puts them all on the clock tiebreak.
    """
    a = _rec("s-a", "A", created=10, previous="s-b")
    b = _rec("s-b", "A", created=20, previous="s-a")
    forward = log_rank_of([a, b])
    backward = log_rank_of([b, a])
    assert forward == backward, "a cycle must not rank by iteration order"
    assert set(forward) == {"s-a", "s-b"}
    assert forward["s-b"] > forward["s-a"], "the clock tiebreak still orders two unrelated logs"


def test_a_previous_sid_naming_another_slots_log_does_not_borrow_its_depth():
    """``previous_sid`` says which session of THIS slot was replaced, so a link naming
    another slot's log measures nothing about this one and must not be walked.

    Following it borrows the foreign chain's length as this log's depth, and depth is the
    leading term of the ranking -- so a log the chain cannot place at all outranks one that
    is genuinely newer, and the decision held in the loser is the one the fold applies. Here
    the borrowed depth makes an older ADOPTION beat a newer RELEASE and the released session
    keeps a parent it was let go from. The two logs are unrelated once the foreign step is
    refused, which puts them on the clock tiebreak where they belong.
    """
    later = _rec("s-later", "A", created=100)
    borrower = _rec("s-borrower", "A", created=50, previous="s-foreign")
    foreign = _rec("s-foreign", "B", created=1)
    records = [later, borrower, foreign, _rec("s-d", "D", created=2)]

    rank = log_rank_of(records)
    assert rank["s-later"] > rank["s-borrower"], "a foreign predecessor must not outrank a log"

    adopt_in_borrower = EdgeRecord(slot="A", parent_slot="D", at=10, sid="s-borrower", seq=1)
    release_in_later = EdgeRecord(slot="A", parent_slot=None, at=20, sid="s-later", seq=1)
    assert edge_supersedes(release_in_later, adopt_in_borrower, rank) is True
    for order in ([adopt_in_borrower, release_in_later], [release_in_later, adopt_in_borrower]):
        assert _parents(fold_tree(records, order))["A"] is None


def test_a_partial_removal_re_reads_the_decision_from_the_segments_that_survive():
    """A decision is appended, so it can be in any segment -- including the ones a partial
    removal took while leaving the rest.

    Nothing else corrects that. A decision is otherwise only re-derived by a cold rebuild,
    so between the failed removal and the next restart the tree asserts a takeover with no
    segment behind it, serves that reading as COMPLETE, and writes it into the checkpoint.
    The trigger is an ordinary unlink refusal, which the removal path already expects.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.nodes()["A"].parent_slot == "D", "the adoption must hold before the removal"

    # The removal gets the decision's segment and is then refused the rest, which is the
    # partial shape: the header still yields a record, and nothing on disk records a
    # decision.
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)
    kept = [
        line
        for line in segment.read_text(encoding="utf-8").splitlines(keepends=True)
        if '"session/adopted"' not in line
    ]
    segment.write_text("".join(kept), encoding="utf-8")

    proj.reconcile_edge("s-a", "A")

    assert (
        proj.nodes()["A"].parent_slot is None
    ), "the decision outlived the segment it was written in"
    assert proj.reading().incomplete is False, "a completed re-read is not a degraded reading"


def test_a_partial_removal_that_cannot_be_read_keeps_the_decision_and_says_so(monkeypatch):
    """An unreadable directory proves nothing, so the held decision stays and the reading
    stops claiming to be complete.

    The opposite choice is worse in both directions: dropping on a transient error discards
    a valid takeover, and keeping it silently serves a possibly-stale tree as authoritative
    to a caller that decides on it.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    proj = SessionTreeProjection()
    proj.ensure_seeded()
    assert proj.nodes()["A"].parent_slot == "D"

    def _refuse(_directory):
        raise OSError("partial removal left this unreadable")

    monkeypatch.setattr(crew_store, "find_last_tree_edge", _refuse)
    proj.reconcile_edge("s-a", "A")

    assert proj.nodes()["A"].parent_slot == "D", "an unreadable re-read must not drop a decision"
    assert proj.reading().incomplete is True, "a re-read that proved nothing is not complete"


def test_a_partial_removal_of_a_unit_holding_no_decision_reads_nothing(monkeypatch):
    """The common partial removal is of a unit that never recorded a decision, and it must
    not pay for a file read to learn that.

    The held state answers it: no decision for this unit means a removal cannot have
    stranded one.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    del handle_a

    proj = SessionTreeProjection()
    proj.ensure_seeded()

    def _unexpected(_directory):  # pragma: no cover -- asserts it is never reached
        raise AssertionError("a unit with no decision must not be read from disk")

    monkeypatch.setattr(crew_store, "find_last_tree_edge", _unexpected)
    proj.reconcile_edge("s-a", "A")
    assert proj.reading().incomplete is False


def test_byte_damage_in_a_segment_reads_as_a_failed_read_not_as_no_decision(monkeypatch):
    """The framing reader raises its OWN exception class, which is neither ``OSError`` nor
    ``ValueError`` -- the two every tree-edge caller guards.

    Left as-is it escaped all of them into the projection's boot guard, which marks the
    projection seeded with nothing installed and then refuses lineage and both verbs for
    the rest of the process. Converted at the single boundary the callers guard, rather
    than by adding a class to three separate tuples that would have to stay in step.

    Not answered with the newest READABLE decision either. The framing reader aborts at
    the damage, so everything past it in the file is unseen -- and that is exactly where a
    later decision would be, so a confident answer there is a wrong edge served as
    complete.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")
    handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    segment = crew_store.newest_segment(directory)

    def _damaged(*_args, **_kwargs):
        raise crew_store.UnreadableRecord("record over the cap")

    # Forced past the window guard so the whole-file reader is the one that runs.
    monkeypatch.setattr(crew_store, "_TAIL_WINDOW", 1)
    monkeypatch.setattr(crew_store, "strict_raw_records", _damaged)
    with pytest.raises(ValueError):
        crew_store._scan_whole_for_types(segment, crew_store._TREE_EDGE_TYPES)


def test_a_steady_state_seed_does_not_re_read_a_unit_whose_log_has_not_changed():
    """The complete read is proportional to a unit's WHOLE log, and a unit that records no
    decision is the only case that reads all of it.

    Paid per unit on every process start, that is a read of the entire store to re-learn
    what the last boot already established -- and most sessions are never adopted, so it is
    the dominant case rather than a corner of it. The verdict is a pure function of the
    bytes, so a boot that reaches it records the identity it was reached at and later boots
    skip the read while that identity holds.

    The FIRST boot here is the cold one and it does read: with no checkpoint there is no
    prior verdict to trust, which is the one boot that genuinely has to look. What this
    pins is the steady state after it.
    """
    handle_a = _log("s-a", "A")
    _opened(handle_a, "A")

    first = SessionTreeProjection()
    first.ensure_seeded()
    assert first.flush_checkpoint() is True

    reads: list[str] = []
    real = crew_store.find_last_tree_edge

    def _counted(directory):
        reads.append(directory.name)
        return real(directory)

    # The boot after the cold one reaches the verdict and records the identity with it.
    second = SessionTreeProjection()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(crew_store, "find_last_tree_edge", _counted)
        second.ensure_seeded()
    assert len(reads) == 1, "the boot that has no cached verdict must read for it"
    assert second.flush_checkpoint() is True

    # And every boot after that skips it, which is the steady state on a real store.
    reads.clear()
    third = SessionTreeProjection()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(crew_store, "find_last_tree_edge", _counted)
        third.ensure_seeded()
    assert reads == [], "an unchanged unit was read again to reach a verdict already held"
    assert third.nodes()["A"].parent_slot is None

    # An APPEND moves the identity, so the next boot reads it rather than trusting the
    # cache -- which is what keeps the skip from hiding a decision written since.
    handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    fourth = SessionTreeProjection()
    fourth.ensure_seeded()
    assert fourth.nodes()["A"].parent_slot == "D", "a changed unit must be re-read"
