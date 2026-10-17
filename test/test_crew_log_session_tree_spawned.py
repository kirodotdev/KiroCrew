"""``session/spawned``: the creating edge recorded on the CREATOR's log at mint.

A ``session_create`` child writes its own ``session/opened.parent`` only at its first
turn, because its log is keyed by an ACP session id that does not exist at mint. The
creator's log does exist at mint, so the edge is written there, and the tree folds it
when the child's own records name no parent. A restart between mint and the child's
first turn therefore keeps the child under its creator.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from types import SimpleNamespace

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree as stp_session_tree
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import (
    EdgeRecord,
    OpenedRecord,
    SessionTree,
    fold_tree,
    spawned_record,
)
from kiro_crew.crew_log.session_tree_projection import CHECKPOINT_NAME, SessionTreeProjection
from kiro_crew.dashboard.session_memory import lineage_parents

GATEWAY = "gateway"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home per test, and no checkpoint write armed on the real pool. Patched
    through ``_floor_monkeypatch`` so a test's own ``monkeypatch.undo()`` cannot lift it."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    _floor_monkeypatch.setattr(
        "kiro_crew.executors.maintenance_executor",
        lambda: type("_NoPool", (), {"submit": staticmethod(lambda *a, **k: None)}),
    )
    stp.reset_for_tests()
    yield
    stp.reset_for_tests()
    emit.reset_caches()


def _rec(sid: str, slot: str, created: int = 1, parent: str | None = None) -> OpenedRecord:
    return OpenedRecord(sid=sid, slot=slot, created_at=created, parent_slot=parent)


def _spawn(child: str, creator: str, sid: str, at: int = 10, seq: int = 2) -> EdgeRecord:
    return EdgeRecord(slot=child, parent_slot=creator, at=at, sid=sid, seq=seq)


def _open_creator(sid: str, slot: str) -> None:
    emit.on_session_opened(sid, agent="kirocrew", slot=slot, model="opus", cwd="/w", owner="o")


def _restart() -> SessionTreeProjection:
    """What a gateway restart leaves: no in-memory state, only the disk."""
    stp.reset_for_tests()
    emit.reset_caches()
    revived = SessionTreeProjection()
    revived.ensure_seeded()
    return revived


def _checkpoint_path():
    return crew_store.crew_log_root(lg.KIND_SESSION) / "projections" / CHECKPOINT_NAME


# ── the fold ───────────────────────────────────────────────────────────────


def test_a_spawn_gives_a_child_with_no_log_a_node_under_its_creator():
    nodes = fold_tree([_rec("s-l", "L")], spawned=[_spawn("W", "L", "s-l")])
    assert nodes["W"].parent_slot == "L"
    assert nodes["W"].cycle is False


def test_the_childs_own_opened_parent_wins_over_a_spawn():
    records = [_rec("s-l", "L"), _rec("s-m", "M"), _rec("s-w", "W", created=2, parent="M")]
    nodes = fold_tree(records, spawned=[_spawn("W", "L", "s-l")])
    assert nodes["W"].parent_slot == "M"


def test_a_childs_own_log_speaks_for_itself_over_a_spawn():
    """Once the child has a log of its own, the spawn is not consulted for it: its own
    records and any later edge decide where it hangs. A child whose log names no parent
    is a root, not refilled from the creator's long-lived spawn -- that refill is what
    would resurrect a creator edge after a move's unit is evicted."""
    records = [_rec("s-l", "L"), _rec("s-w", "W", created=2)]
    nodes = fold_tree(records, spawned=[_spawn("W", "L", "s-l")])
    assert nodes["W"].parent_slot is None


def test_a_released_child_does_not_snap_back_under_its_creator_after_eviction():
    """The regression trusted_creator refuses: a long-lived spawn in the creator's log
    outlives the unit that recorded the move. The child's own records and its release
    edge decide where it hangs; the spawn is not consulted for a child that has a log,
    so retention removing an EARLIER unit does not let the creator's spawn re-establish
    the old edge over the surviving release."""
    spawned = [_spawn("W", "L", "s-l")]
    # L created W (spawn on L's log). W ran (first unit s-w0, parent L), then was
    # released -- the release rides on a later unit s-w1.
    records_before = [
        _rec("s-l", "L"),
        _rec("s-w0", "W", created=2, parent="L"),
        _rec("s-w1", "W", created=3),
    ]
    edges = [EdgeRecord("W", None, at=50, sid="s-w1", seq=3)]
    released = fold_tree(records_before, edges, spawned)
    assert released["W"].parent_slot is None
    # Retention removes W's first unit s-w0; the release-bearing unit s-w1 survives, so W
    # still has a log and the release still stands. W must NOT snap back under L.
    records_after = [_rec("s-l", "L"), _rec("s-w1", "W", created=3)]
    after = fold_tree(records_after, edges, spawned)
    assert after["W"].parent_slot is None


def test_an_adopted_child_does_not_snap_back_under_its_creator_after_eviction():
    """Same shape for a takeover: once W's first unit is evicted, L's spawn must not
    re-establish L as W's parent over the surviving adopt edge to B."""
    spawned = [_spawn("W", "L", "s-l")]
    records_before = [
        _rec("s-l", "L"),
        _rec("s-b", "B"),
        _rec("s-w0", "W", created=2, parent="L"),
        _rec("s-w1", "W", created=3),
    ]
    edges = [EdgeRecord("W", "B", at=50, sid="s-w1", seq=3)]
    adopted = fold_tree(records_before, edges, spawned)
    assert adopted["W"].parent_slot == "B"
    # W's first unit is evicted; the adopt-bearing unit s-w1 and B's unit remain. W must
    # hang under B, never back under L.
    records_after = [_rec("s-l", "L"), _rec("s-b", "B"), _rec("s-w1", "W", created=3)]
    after = fold_tree(records_after, edges, spawned)
    assert after["W"].parent_slot == "B"


def test_adoption_and_release_still_win_over_a_spawn():
    records = [_rec("s-l", "L"), _rec("s-d", "D"), _rec("s-w", "W", created=2)]
    spawned = [_spawn("W", "L", "s-l")]
    adopted = fold_tree(records, [EdgeRecord("W", "D", at=50, sid="s-w", seq=3)], spawned)
    assert adopted["W"].parent_slot == "D"
    released = fold_tree(records, [EdgeRecord("W", None, at=50, sid="s-w", seq=3)], spawned)
    assert released["W"].parent_slot is None


def test_a_spawn_whose_creator_has_no_log_is_not_folded():
    nodes = fold_tree([_rec("s-x", "X")], spawned=[_spawn("W", "L", "s-l")])
    assert "W" not in nodes


def test_the_creator_comes_from_the_unit_never_from_the_entry():
    entry = lg.Entry.from_dict(
        {
            "seq": 4,
            "time": 9,
            "type": "session/spawned",
            "src": GATEWAY,
            "data": {"child": {"slot": "W"}, "parent": {"slot": "FORGED"}},
        }
    )
    spawn = spawned_record("L", "s-l", entry)
    assert spawn == EdgeRecord(slot="W", parent_slot="L", at=9, sid="s-l", seq=4)
    assert spawned_record("W", "s-w", entry) is None  # a child naming itself


# ── the emitter and the restart ────────────────────────────────────────────


def test_mint_then_restart_with_no_child_first_turn_still_nests_the_child():
    """Mint, restart before the child's first turn, cold seed: the child still nests."""
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    # In process the fold already has it, from the emitter's own apply.
    assert stp.projection().nodes()["W"].parent_slot == "L"

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-l")
    entries, cut, hit_cap = crew_store.find_spawned_entries(directory, 10)
    assert entries[0].data == {"child": {"slot": "W"}} and cut is False and hit_cap is False
    # No unit was ever written for the child.
    assert crew_store.unit_dir_for(lg.KIND_SESSION, "s-w") is None or not (
        crew_store.unit_dir_for(lg.KIND_SESSION, "s-w").exists()
    )

    revived = _restart()
    assert revived.nodes()["W"].parent_slot == "L"

    # The join the sidebar reads: the restored child row has NO mint witness, yet it
    # still nests under the live creator row, from the folded node.
    rows = [
        {"key": "L"},
        {"key": "W", "lineage_minted": False, "created_by": "L"},
    ]
    parents = lineage_parents(rows, revived.nodes())
    assert parents["W"] == {"slot": "L", "key": "L"}


def test_spawned_creator_resolves_a_logless_child_but_not_once_it_has_a_log():
    """The open-time fallback: spawned_creator names the creator for a child with no log
    of its own, and declines once the child opens one (its own records then decide)."""
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    proj = stp.projection()
    # Child W has no log yet -> the trusted spawn names its creator.
    assert proj.spawned_creator("W") == "L"
    # A creator with no log of its own cannot be cited this way.
    assert proj.spawned_creator("unknown-child") == ""
    # Once the child opens its own log, its own records decide and the spawn is skipped.
    _open_creator("s-w", "W")
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().spawned_creator("W") == ""


def test_a_childs_first_turn_after_a_restart_cites_the_creator_from_the_spawn():
    """The scenario this PR exists for, end to end: mint, restart before the child's
    first turn, then that first turn runs. It has no predecessor log, so the parent is
    resolved from the trusted session/spawned record and written on the child's OWN
    session/opened -- so the edge now lives in the child's log and outlives the
    creator's spawn being dropped."""
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True

    revived = _restart()
    # The child's first turn runs post-restart: no predecessor log (head_sid == "").
    # The open path resolves the parent from the spawn and writes it on the child.
    resolved = revived.spawned_creator("W")
    assert resolved == "L"
    emit.on_session_opened(
        "s-w", agent="kirocrew", slot="W", model="opus", cwd="/w", owner="o", parent_slot=resolved
    )
    assert emit.flush(timeout=5.0) is True
    assert stp.projection().nodes()["W"].parent_slot == "L"

    # The edge now lives in the child's own log: dropping the creator's spawn leaves it.
    stp.projection().forget("s-l")
    assert stp.projection().nodes()["W"].parent_slot == "L"


def test_the_spawn_survives_a_restart_through_the_checkpoint_too():
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    first = _restart()
    assert first.flush_checkpoint() is True
    payload = json.loads(_checkpoint_path().read_text(encoding="utf-8"))
    assert payload["spawned"] and payload["spawned"][0]["slot"] == "W"

    revived = _restart()
    assert revived.nodes()["W"].parent_slot == "L"


def test_a_cold_scan_reads_the_spawn_like_the_projection():
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    reading = SessionTree().reading(with_edges=True)
    assert reading.nodes["W"].parent_slot == "L"
    assert not reading.incomplete


def test_removing_the_creator_unit_drops_its_spawns():
    _open_creator("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    proj = stp.projection()
    assert "W" in proj.nodes()
    proj.forget("s-l")
    assert "W" not in proj.nodes()


def test_a_creator_with_no_log_writes_nothing():
    """No ACP session for the creator: today's in-memory behavior, nothing on disk."""
    emit.on_session_spawned("s-none", creator_slot="L", child_slot="W")
    assert emit.flush(timeout=5.0) is True
    assert "W" not in stp.projection().nodes()


# ── the bound ──────────────────────────────────────────────────────────────


def _spawn_many(creator_sid: str, creator: str, children: list[str]) -> None:
    _open_creator(creator_sid, creator)
    for child in children:
        emit.on_session_spawned(creator_sid, creator_slot=creator, child_slot=child)
    assert emit.flush(timeout=10.0) is True


def test_a_cut_read_keeps_the_newest_spawns_and_says_it_was_cut(_floor_monkeypatch):
    """Past the per-unit bound the NEWEST child still nests, and a seed built from the
    cut read reports itself incomplete instead of complete."""
    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 3)
    _spawn_many("s-l", "L", ["W1", "W2", "W3", "W4", "W5"])

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-l")
    entries, cut, hit_cap = crew_store.find_spawned_entries(directory, 3)
    assert [e.data["child"]["slot"] for e in entries] == ["W3", "W4", "W5"]
    assert cut is True and hit_cap is True

    reading = SessionTree().reading(with_edges=True)
    assert reading.nodes["W5"].parent_slot == "L"
    assert reading.incomplete is True
    assert "s-l" in reading.suspect_sids

    revived = _restart()
    assert revived.nodes()["W5"].parent_slot == "L"
    assert revived.reading().incomplete is True


def test_a_cut_read_of_spawns_whose_children_self_record_stays_complete(_floor_monkeypatch):
    """A creator past the per-unit bound whose children have ALL opened their own log
    naming a parent is not a cut answer the fold needs: those spawns are dead weight the
    child's own ``session/opened.parent`` wins over, so dropping them past the cap must
    leave the reading complete and keep adopt/release lineage working."""
    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 3)
    children = ["W1", "W2", "W3", "W4", "W5"]
    _spawn_many("s-l", "L", children)
    # Every child then took its first turn and opened its own log, naming L as parent --
    # the ordinary life of a child once it starts, which makes its spawn redundant.
    for i, child in enumerate(children):
        emit.on_session_opened(
            f"s-{child.lower()}",
            agent="kirocrew",
            slot=child,
            model="opus",
            cwd="/w",
            owner="o",
            parent_slot="L",
            parent_sid="s-l",
        )
    assert emit.flush(timeout=10.0) is True

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-l")
    # The held set is ALWAYS the newest ``limit`` spawns -- a pure function of the
    # creator's own log, so it caches soundly by that log's identity. ``self_parented``
    # relaxes only the cut flag: every dropped spawn here is a self-parented child, so
    # the scan is NOT cut even though it dropped two -- but hit_cap stays True, so a
    # caller still refuses to cache the scan (a child's own log can later vanish).
    kept_known, cut_when_known, hit_cap_known = crew_store.find_spawned_entries(
        directory, 3, set(children)
    )
    assert [e.data["child"]["slot"] for e in kept_known] == ["W3", "W4", "W5"]
    assert cut_when_known is False and hit_cap_known is True
    # Without that knowledge the same held set reports cut -- the signal is intact for a
    # caller that cannot name self-parented children.
    kept_blind, cut_when_blind, hit_cap_blind = crew_store.find_spawned_entries(directory, 3)
    assert [e.data["child"]["slot"] for e in kept_blind] == ["W3", "W4", "W5"]
    assert cut_when_blind is True and hit_cap_blind is True

    reading = SessionTree().reading(with_edges=True)
    assert all(reading.nodes[c].parent_slot == "L" for c in children)
    assert reading.incomplete is False
    assert "s-l" not in reading.suspect_sids

    revived = _restart()
    assert all(revived.nodes()[c].parent_slot == "L" for c in children)
    assert revived.reading().incomplete is False


def test_the_self_parented_filter_never_drops_a_spawn_from_the_held_set(_floor_monkeypatch):
    """The held set stays the newest ``limit`` spawns whatever ``self_parented`` says, so
    it is a pure function of the creator's log. If it filtered the held set by the
    children's logs instead, forgetting a child's own log would leave a scan that no
    longer carries the creator's spawn and strand the child at the top level across a
    restart. Deleting a child's log must leave the creator's spawn to re-root it."""
    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 5)
    children = ["W1", "W2", "W3"]
    _spawn_many("s-l", "L", children)
    for child in children:
        emit.on_session_opened(
            f"s-{child.lower()}",
            agent="kirocrew",
            slot=child,
            model="opus",
            cwd="/w",
            owner="o",
            parent_slot="L",
            parent_sid="s-l",
        )
    assert emit.flush(timeout=10.0) is True

    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-l")
    kept_all = [e.data["child"]["slot"] for e in crew_store.find_spawned_entries(directory, 5)[0]]
    kept_filtered = [
        e.data["child"]["slot"]
        for e in crew_store.find_spawned_entries(directory, 5, set(children))[0]
    ]
    # Under cap, nothing is dropped and the held set is the same whether or not the
    # caller names the children self-parented -- the filter touches only the cut flag.
    assert kept_all == kept_filtered == children

    # While its own log stands, W2 nests from that log (its own parent edge wins).
    assert SessionTree().reading(with_edges=True).nodes["W2"].parent_slot == "L"

    # W2's own log is then removed (retention / delete). The creator's spawn for W2 must
    # survive in the held set so a fresh disk scan still roots W2 under L.
    w2_dir = crew_store.unit_dir_for(lg.KIND_SESSION, "s-w2")
    assert w2_dir is not None and w2_dir.exists()
    # Drop the live writer's cached handles first: Windows refuses to unlink a file a
    # handle still holds open, which is exactly the lease a live log handle keeps.
    emit.reset_caches()
    shutil.rmtree(w2_dir)
    rooted = SessionTree().reading(with_edges=True)
    assert rooted.nodes["W2"].parent_slot == "L"


def test_a_capped_scan_is_never_cached_even_when_self_parented_suppresses_the_cut(
    _floor_monkeypatch,
):
    """The replay must not cache a scan that hit the per-unit cap, even when every
    evicted child is self-parented and the cut flag is suppressed. If it did, removing
    one of those children's own logs later -- which leaves the creator's identity
    unchanged -- would re-serve the cached scan with that now-load-bearing spawn still
    omitted, and report the reading complete. So a capped scan leaves the unit uncached
    and the next boot re-reads it."""
    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 2)
    children = ["W1", "W2", "W3"]
    _spawn_many("s-l", "L", children)
    for child in children:
        emit.on_session_opened(
            f"s-{child.lower()}",
            agent="kirocrew",
            slot=child,
            model="opus",
            cwd="/w",
            owner="o",
            parent_slot="L",
            parent_sid="s-l",
        )
    assert emit.flush(timeout=10.0) is True

    first = _restart()
    assert first.flush_checkpoint() is True
    # Every evicted spawn is self-parented, so the reading is complete (not cut) ...
    assert first.reading().incomplete is False
    # ... but the capped unit must NOT be cached: the next boot has to re-read it.
    assert "s-l" not in first._spawn_scans


def test_a_cut_replay_is_not_cached_as_the_units_whole_answer(_floor_monkeypatch):
    """A checkpoint replay that hit the bound must read the unit again next boot."""
    _spawn_many("s-l", "L", ["W1"])
    first = _restart()
    assert first.flush_checkpoint() is True
    _floor_monkeypatch.setattr(stp_session_tree, "SPAWNED_PER_UNIT_CAP", 1)
    for child in ["W2", "W3"]:
        emit.on_session_spawned("s-l", creator_slot="L", child_slot=child)
    assert emit.flush(timeout=10.0) is True

    stp.reset_for_tests()
    emit.reset_caches()
    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert revived.nodes()["W3"].parent_slot == "L"
    assert revived.reading().incomplete is True
    assert "s-l" not in revived._spawn_scans


def test_eviction_past_the_cap_is_not_undone_by_the_replay_scan_cache(_floor_monkeypatch):
    """Spawns evicted while a replay installs them leave their unit uncached, so the
    next boot re-reads it rather than trusting a cache entry for a set it does not hold."""
    _spawn_many("s-a", "A", ["A1", "A2"])
    _spawn_many("s-b", "B", ["B1", "B2"])
    first = _restart()
    assert first.flush_checkpoint() is True
    # Drop the checkpoint's spawns so the replay must read them, then cap the store.
    path = _checkpoint_path()
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["spawned"] = []
    payload["spawn_scans"] = {}
    path.write_text(json.dumps(payload), encoding="utf-8")
    _floor_monkeypatch.setattr(stp, "TREE_UNIT_CAP", 3)

    stp.reset_for_tests()
    emit.reset_caches()
    revived = SessionTreeProjection()
    revived.ensure_seeded()
    assert len(revived._spawned) == 3
    held = [spawn.sid for spawn in revived._spawned.values()]
    evicted_units = {sid for sid in ("s-a", "s-b") if held.count(sid) < 2}
    assert evicted_units
    assert revived.reading().incomplete is True
    for sid in evicted_units:
        assert sid not in revived._spawn_scans


# ── session_control's call ─────────────────────────────────────────────────


def test_session_control_records_only_a_witnessed_mint(_floor_monkeypatch):
    from kiro_crew.dashboard import session_control as sc

    calls: list[tuple] = []

    def _fake(sid, **kw):
        settled = kw.pop("on_settled")
        calls.append((sid, kw))
        settled(True)

    _floor_monkeypatch.setattr(sc.crew_log_emit, "on_session_spawned", _fake)
    minted = SimpleNamespace(key="W", _lineage_minted=True, _created_by="L", _created_by_sid="s-l")
    assert asyncio.run(sc._record_spawn("L", minted)) is True
    assert calls == [("s-l", {"creator_slot": "L", "child_slot": "W"})]

    calls.clear()
    # Restored (no witness), no creator session, or a caller that is not the minter.
    for caller, child in [
        ("L", SimpleNamespace(**{**vars(minted), "_lineage_minted": False})),
        ("L", SimpleNamespace(**{**vars(minted), "_created_by_sid": ""})),
        ("OTHER", minted),
    ]:
        assert asyncio.run(sc._record_spawn(caller, child)) is True
    assert calls == []


def test_the_create_is_answered_only_after_the_spawn_is_on_disk():
    """``_record_spawn`` returns True only once the entry is durable, so the create's
    success reply cannot outrun the edge it depends on."""
    from kiro_crew.dashboard import session_control as sc

    _open_creator("s-l", "L")
    assert emit.flush(timeout=5.0) is True
    minted = SimpleNamespace(key="W", _lineage_minted=True, _created_by="L", _created_by_sid="s-l")
    assert asyncio.run(sc._record_spawn("L", minted)) is True
    directory = crew_store.unit_dir_for(lg.KIND_SESSION, "s-l")
    entries, _, _ = crew_store.find_spawned_entries(directory, 10)
    assert [e.data["child"]["slot"] for e in entries] == ["W"]

    # A creator sid with no log: nothing lands, and the reply says so.
    orphan = SimpleNamespace(
        key="X", _lineage_minted=True, _created_by="L", _created_by_sid="s-none"
    )
    assert asyncio.run(sc._record_spawn("L", orphan)) is False


def test_the_creator_log_is_a_real_unit_with_the_entry_type_registered():
    handle = CrewLog.create(lg.KIND_SESSION, "s-l", owner="o", agent="kirocrew", slot="L")
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": "L",
            "model": "opus",
            "cwd": "/w",
            "owner": "o",
            "resumed": False,
        },
        src=GATEWAY,
    )
    written = handle.append("session/spawned", {"child": {"slot": "W"}}, src=GATEWAY)
    assert written is not None and written.type == "session/spawned"
