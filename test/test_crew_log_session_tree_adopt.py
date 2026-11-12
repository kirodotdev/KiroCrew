"""Adoption and release: the records that MOVE a session in the tree.

The creating edge is never rewritten; decisions live beside it. The fold prefers the
newest decision in any arrival order and still colours cycles; the projection advances
only after the append; a cold scan reaches the same tree, so the checkpoint is only a
shortcut.
"""

from __future__ import annotations

import json

import pytest
from crew_log_drain import assert_drained

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

    The process-wide projection is bound to one store, and ``_debounced_write`` saves
    outside the lock, so a live pool could land a file here after the test ends.
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
    # Kept emitter handles hold their write leases process-wide; drop them so they do not
    # show up as another test's "lease still held" on the same xdist worker.
    emit.reset_caches()


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

    B and C follow with no record of their own because they cite A's SLOT.
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

    A ``session/opened`` with no parent cannot say this: it only did not repeat a creator.
    """
    records = [_rec("s-a", "A", created=1), _rec("s-b", "B", created=2, parent="A")]
    released = fold_tree(records, [EdgeRecord(slot="B", parent_slot=None, at=50, sid="s-b")])
    assert _parents(released) == {"A": None, "B": None}


def test_the_newest_decision_wins_whatever_order_the_records_arrive_in():
    """Order-independence, which a stale checkpoint plus a replayed tail needs.

    Taking the last arrival would put the session back under a parent that released it.
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

    A checkpoint plus a replayed tail can apply decisions in an order no writer saw.
    Every slot on the loop is marked, so a consumer nests none of them.
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
    """A checkpoint that lost adoptions would load as "nothing was ever adopted".

    Units and the decision are real on disk: the replay drops a held record whose unit
    is gone and a held edge the log contradicts, so hand-built state would be reconciled
    away before the assertion.
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

    Two logs of one slot can each hold a decision. Decisions are held per ``(slot, sid)``,
    so after the winner is deleted the fold does not fall back to the opening edge.
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

    A clock step backward (NTP, VM resume) gives the later entry a smaller timestamp;
    ``seq`` only increases.
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

    Reducing alone, the seed lacks the record order that ranks logs and falls back to
    timestamps. A decision dropped at seed time is also gone after the winner is deleted.
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

    A decision lives at the end of a log, so a missed one is in a unit the checkpoint
    already holds.
    """
    _unit_with_decision("s-a", "A", parent=None, adopt_to="D")
    handle = _log("s-d", "D")
    _opened(handle, "D")
    del handle

    # Both units and no decisions, as a crash before the debounced write leaves it.
    # Edited by hand: a seed from this disk would already hold the decision.
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
    assert_drained()

    assert stp.projection().nodes()["A"].parent_slot == "D"
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/adopted"
    assert entry.data == {"parent": {"slot": "D", "sid": "s-d"}}


def test_the_emitter_reports_the_durable_outcome_to_a_waiting_caller():
    """``on_settled`` is how a verb learns the append actually landed.

    The emitter returns before the write, so this is the caller's only signal. It is
    called once, with ``True`` only after the entry is on disk.
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
    assert_drained()
    assert settled == [True]
    # And the entry really is there, so ``True`` is not merely "the job ran".
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    entry = read_last_tree_edge(crew_store.newest_segment(directory))
    assert entry is not None and entry.type == "session/adopted"


def test_the_emitter_reports_failure_when_there_is_no_log_to_write_to():
    """A job that finds no log appends nothing, and the caller is told False.

    The terminal cleanup runs either way, so it cannot stand in for a write."""
    settled: list[bool] = []
    emit.on_session_adopted("s-missing", slot="A", parent_slot="D", on_settled=settled.append)
    assert_drained()
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

    An entry rejected at the buffer's memory ceiling finishes without the permanent-drop
    hook, so success cannot be inferred from that hook's absence. ``wrote`` is set only
    after ``log.append`` returns.
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
    assert_drained()
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
    assert_drained()
    assert stp.projection().nodes()["A"].parent_slot == "D"

    emit.on_session_released("s-a", slot="A", previous_parent_slot="D", previous_parent_sid="s-d")
    assert_drained()
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
    assert_drained()
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-a")
    assert read_last_tree_edge(crew_store.newest_segment(directory)) is None
    assert _store_name("s-a") == directory.name


def test_a_store_that_cannot_be_listed_is_not_answered_as_no_decision(monkeypatch):
    """A listing that fails must raise rather than return ``None``.

    Both callers cache the answer, so "no decision" would become the tree's standing
    answer. They catch ``OSError`` and mark the scan incomplete.
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
    """An unreadable directory sets the scanner's incomplete flag.

    Absent and empty are answers; a failed listing is not, or a stale cached edge stands
    and a release could authorize the parent that gave the session up.
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

    with monkeypatch.context() as mp:
        mp.setattr(Path, "iterdir", _refuse_one)
        reading = SessionTree().reading(with_edges=True)
    assert reading.incomplete is True


def test_a_complete_read_that_finds_no_decision_drops_the_checkpoints_edge():
    """A decision retention removed must stop being the tree's answer.

    Only a complete read finding nothing can contradict the checkpoint's edge; otherwise
    it pins the slot under that parent on every restart.
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
    """A failed stat in ``_scan_whole_for_types`` raises, like its ``open`` does.

    A ``None`` would be cached as "no decision" against the segment's unchanged
    dev/ino/size, and an idle unit would never re-read it.
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
    # The window must be outrun, or the whole-file scan is never reached.
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
    with monkeypatch.context() as mp:
        mp.setattr(Path, "stat", _refuser(1))
        with pytest.raises(OSError):
            crew_store._scan_whole_for_types(segment, crew_store._TREE_EDGE_TYPES)

    # And the walk above it does not convert that into "no decision". Its bounded window
    # stats the segment FIRST, so the scan's own stat is the second one.
    monkeypatch.setattr(Path, "stat", _refuser(2))
    with pytest.raises(OSError):
        find_last_tree_edge(directory)


def test_a_clock_rollback_between_two_sessions_does_not_invert_the_cross_log_order():
    """The log ranking must not be a clock value either.

    A clock rollback between two sessions of one slot gives the successor the earlier
    ``created_at``. The ``previous_sid`` chain records which replaced which, so it ranks.
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

    The creating citation (oldest record) and the decision ranking both read
    ``log_rank_of``.
    """
    old_log = _rec("s-old", "A", created=5000, parent="P")
    new_log = _rec("s-new", "A", created=10, previous="s-old", parent="Q")
    records = [old_log, new_log, _rec("s-p", "P", created=1), _rec("s-q", "Q", created=2)]
    # The chain start is the oldest log, so its citation is the one that stands -- even
    # though its header stamp is the LATER of the two.
    assert _parents(fold_tree(records))["A"] == "P"


def test_a_previous_sid_cycle_does_not_hang_the_ranking():
    """A ``previous_sid`` loop ends the same way whichever sid the walk reaches first.

    Breaking at the first member visited would make rank depend on iteration order; every
    log on a cycle gets depth 0 and falls to the clock tiebreak.
    """
    a = _rec("s-a", "A", created=10, previous="s-b")
    b = _rec("s-b", "A", created=20, previous="s-a")
    forward = log_rank_of([a, b])
    backward = log_rank_of([b, a])
    assert forward == backward, "a cycle must not rank by iteration order"
    assert set(forward) == {"s-a", "s-b"}
    assert forward["s-b"] > forward["s-a"], "the clock tiebreak still orders two unrelated logs"


def test_a_previous_sid_naming_another_slots_log_does_not_borrow_its_depth():
    """A ``previous_sid`` naming another slot's log is not walked.

    Depth leads the ranking, so a borrowed foreign depth would let an older adoption beat
    a newer release.
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
    """A partial removal re-reads the decision from the segments that survive.

    Otherwise only a cold rebuild re-derives it, and until then the tree serves a takeover
    with no segment behind it as complete.
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

    # Partial removal: the header still yields a record, but no decision is on disk.
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
    """An unreadable directory proves nothing: the decision stays, the reading is partial."""
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
    """A partial removal of a unit with no held decision reads no file.

    No held decision means the removal cannot have stranded one.
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
    """The framing reader's own exception is converted to ``ValueError`` at one boundary.

    Callers guard only ``OSError`` and ``ValueError``; anything else reaches the boot
    guard, which seeds nothing and refuses lineage for the process. The newest readable
    decision is no answer either: a later decision may sit past the damage.
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
    """A unit with no decision costs a whole-log read, so its verdict is cached by identity.

    Most sessions are never adopted. The verdict is a function of the bytes, so later boots
    skip the read while the file identity holds; the cold first boot still reads.
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

    # An append moves the identity, so the skip cannot hide a decision written since.
    handle_a.append("session/adopted", {"parent": {"slot": "D"}}, src=GATEWAY)
    del handle_a
    handle_d = _log("s-d", "D")
    _opened(handle_d, "D")
    del handle_d

    fourth = SessionTreeProjection()
    fourth.ensure_seeded()
    assert fourth.nodes()["A"].parent_slot == "D", "a changed unit must be re-read"
