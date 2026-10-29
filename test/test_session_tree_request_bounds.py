"""``dispatch_view`` and ``slot_chain`` cost the units a request names.

Each case drives the real read over a store of many units, counts the unit reads and
store listings one request does, and asserts a bound that does not grow with the
store, plus the cases a cache can break: a unit opened after the seed, a named unit
the projection never saw, a unit that does not exist.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, session_tree, session_tree_projection
from kiro_crew.crew_log.session_tree import CHAIN_END_FIRST, SessionTree
from kiro_crew.session_ledger import _store_name

GATEWAY = "gateway"
OTHER_UNITS = 300


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    session_tree_projection.reset_for_tests()
    yield
    session_tree_projection.reset_for_tests()


def _unit(sid: str, slot: str, *, parent: str | None = None, previous: str | None = None) -> None:
    handle = CrewLog.create(lg.KIND_SESSION, sid, owner="raymond", agent="kirocrew", slot=slot)
    data: dict[str, Any] = {
        "agent": "kirocrew",
        "slot": slot,
        "model": "opus",
        "cwd": "/w",
        "owner": "raymond",
        "resumed": False,
    }
    if parent is not None:
        data["parent"] = {"slot": parent, "sid": "acp-lead"}
    if previous is not None:
        data["previous"] = {"sid": previous}
    else:
        data["previous_none"] = True
    handle.append("session/opened", data, src=GATEWAY)


class _TreeMeter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.reads = 0
        self.listings = 0
        real_read = SessionTree._read
        real_dirs = session_tree.unit_dirs

        def read(tree: SessionTree, directory: Any) -> Any:
            self.reads += 1
            return real_read(tree, directory)

        def dirs(*args: Any, **kwargs: Any) -> Any:
            self.listings += 1
            return real_dirs(*args, **kwargs)

        monkeypatch.setattr(SessionTree, "_read", read)
        monkeypatch.setattr(session_tree, "unit_dirs", dirs)

    def reset(self) -> None:
        self.reads = self.listings = 0


@pytest.fixture
def lineage_store() -> None:
    _unit("acp-lead", "chat-lead")
    _unit("acp-child", "chat-child", parent="chat-lead")
    for index in range(OTHER_UNITS):
        _unit(f"acp-other-{index}", f"chat-other-{index}")


def test_a_dispatch_view_for_one_unit_reads_only_the_named_unit_once_the_tree_is_seeded(
    lineage_store, monkeypatch
):
    """One read: the named unit, re-read so a held record whose bytes are gone cannot
    grant. It is also the only record citing chat-child's creator, so it is read once."""
    from kiro_crew.crew_log.read import dispatch_view

    session_tree_projection.projection().ensure_seeded()
    meter = _TreeMeter(monkeypatch)
    view = dispatch_view(("acp-child",))
    assert view.dispatched_by("acp-child", "chat-lead")
    assert not view.dispatched_by("acp-other-3", "chat-lead")
    assert (meter.reads, meter.listings) == (1, 0)
    # A second request is a cache hit on the same fold, plus the same one re-read.
    assert dispatch_view(("acp-child",)) is view
    assert (meter.reads, meter.listings) == (2, 0)


def test_a_dispatch_view_before_the_tree_is_seeded_seeds_it_once(lineage_store, monkeypatch):
    """The cold path seeds the projection (a store-sized scan, once); the next request
    reads only the named unit."""
    from kiro_crew.crew_log.read import dispatch_view

    meter = _TreeMeter(monkeypatch)
    view = dispatch_view(("acp-child",))
    assert view.dispatched_by("acp-child", "chat-lead")
    assert meter.reads >= OTHER_UNITS
    meter.reset()
    assert dispatch_view(("acp-child",)).dispatched_by("acp-child", "chat-lead")
    assert (meter.reads, meter.listings) == (1, 0)


def test_a_unit_opened_after_the_seed_is_visible_without_a_scan(lineage_store, monkeypatch):
    from kiro_crew.crew_log.read import dispatch_view

    session_tree_projection.projection().ensure_seeded()
    dispatch_view(("acp-child",))  # warm the cached fold
    _unit("acp-new", "chat-new", parent="chat-lead")
    session_tree_projection.record_opened("acp-new", "chat-new", 1, "chat-lead", None)
    meter = _TreeMeter(monkeypatch)
    view = dispatch_view(("acp-new",))
    assert view.dispatched_by("acp-new", "chat-lead")
    # One re-read of the named unit: the emitter's record carries no previous_edge
    # or header clock, and it still matches the log on the fields the fence uses.
    assert (meter.reads, meter.listings) == (1, 0)


def test_a_named_unit_the_projection_never_saw_is_answered_by_the_scan(lineage_store, monkeypatch):
    """A log written outside this process's emitter, or evicted past the cap, is not
    in the projection; the request is answered by the scan, which admits it first.
    A name with no log is answered by one by-name probe, never a scan."""
    from kiro_crew.crew_log.read import dispatch_view

    session_tree_projection.projection().ensure_seeded()
    _unit("acp-outside", "chat-outside", parent="chat-lead")
    meter = _TreeMeter(monkeypatch)
    view = dispatch_view(("acp-outside",))
    assert view.dispatched_by("acp-outside", "chat-lead")
    assert view.slot_of("acp-outside") == "chat-outside"
    assert meter.reads >= OTHER_UNITS
    meter.reset()
    view = dispatch_view(("acp-nobody",))
    assert not view.dispatched_by("acp-nobody", "chat-lead")
    assert (meter.reads, meter.listings) == (0, 0)


def test_a_slot_chain_reads_only_the_chain(lineage_store, monkeypatch):
    from kiro_crew.crew_log.read import slot_chain

    _unit("acp-lead-2", "chat-lead", previous="acp-lead")
    meter = _TreeMeter(monkeypatch)
    reading = slot_chain("acp-lead-2")
    assert reading.chain.sids == ("acp-lead-2", "acp-lead")
    assert reading.chain.ended == CHAIN_END_FIRST
    assert not reading.incomplete
    assert (meter.reads, meter.listings) == (2, 0)


def test_a_failed_seed_falls_back_to_the_scan(lineage_store, monkeypatch):
    """A seed that raised leaves an EMPTY stand-in that still reads as seeded. A
    fence deciding on it would refuse every grandchild, so it scans instead."""
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-grand", "chat-grand", parent="chat-child")
    proj = session_tree_projection.projection()
    proj.ensure_seeded()
    monkeypatch.setattr(proj, "_seed_retry_at", 1.0)
    monkeypatch.setattr(proj, "_records", {})
    monkeypatch.setattr(proj, "_nodes", None)
    meter = _TreeMeter(monkeypatch)
    view = dispatch_view(("acp-grand",))
    assert view.dispatched_by("acp-grand", "chat-lead")
    assert meter.reads >= OTHER_UNITS


def test_a_named_unit_cannot_close_a_forged_cycle_into_a_grant(lineage_store):
    """The projection holds X citing S; S's own log, not held, cites X. The fold
    marks both a cycle and refuses, and so does the view the request gets."""
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-x", "chat-x", parent="chat-s")
    session_tree_projection.projection().ensure_seeded()
    _unit("acp-s", "chat-s", parent="chat-x")
    view = dispatch_view(("acp-s",))
    assert view.slot_of("acp-s") == "chat-s"
    assert not view.dispatched_by("acp-s", "chat-x")


def test_a_named_original_log_restores_the_creator_an_evicted_record_held(lineage_store):
    """The projection lost a slot's original, creator-citing log (evicted past its cap)
    and still holds the slot's newer, parentless log. Reading the original by name
    must restore the citation the fold takes from a slot's oldest citing record."""
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-w1", "chat-w", parent="chat-lead")
    _unit("acp-w2", "chat-w", previous="acp-w1")
    proj = session_tree_projection.projection()
    proj.ensure_seeded()
    proj.forget("acp-w1")
    view = dispatch_view(("acp-w1",))
    assert view.slot_of("acp-w1") == "chat-w"
    assert view.dispatched_by("acp-w1", "chat-lead")


def test_a_seed_that_missed_a_faulted_head_is_not_used_for_dispatch(lineage_store, monkeypatch):
    """A head that faulted during the seed leaves the projection incomplete and missing
    an intermediate slot. An incomplete projection is not used: the request is
    answered by the scan, so once the fault clears the conductor reaches its
    grandchild, an unrelated unit is still refused, and the view reports complete."""
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-grand", "chat-grand", parent="chat-child")
    real_read = SessionTree._read

    def faulting(tree: SessionTree, directory: Any) -> Any:
        if directory.name == _store_name("acp-child"):
            return None, True
        return real_read(tree, directory)

    monkeypatch.setattr(SessionTree, "_read", faulting)
    session_tree_projection.projection().ensure_seeded()
    monkeypatch.setattr(SessionTree, "_read", real_read)
    assert session_tree_projection.projection().reading().incomplete
    view = dispatch_view(("acp-grand",))
    assert not view.incomplete
    assert view.dispatched_by("acp-grand", "chat-lead")
    assert not view.dispatched_by("acp-other-1", "chat-lead")


def test_a_held_creator_citation_whose_bytes_are_gone_does_not_grant(lineage_store):
    """A -> B -> C. The projection still holds B citing A (as a checkpoint written
    before a partial removal would), but B's log on disk carries no such
    citation. The grant re-reads B, sees the change, and takes the scan, which
    refuses A reading C -- the answer a fresh process gives."""
    from kiro_crew.crew_log import read as crew_log_read
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-grand", "chat-grand", parent="chat-child")
    session_tree_projection.projection().ensure_seeded()
    assert dispatch_view(("acp-grand",)).dispatched_by("acp-grand", "chat-lead")
    child_log = lg.crew_log_path(lg.KIND_SESSION, "acp-child")
    lines = child_log.read_bytes().split(b"\n")
    lines[1] = b'{"not": "an entry"}'
    child_log.write_bytes(b"\n".join(lines))
    crew_log_read._TREE._heads.clear()
    assert session_tree_projection.projection().reading().records  # still held
    view = dispatch_view(("acp-grand",))
    assert not view.dispatched_by("acp-grand", "chat-lead")


def test_a_scoped_listing_sees_a_child_log_the_projection_never_held(lineage_store):
    """The scoped listing walks the store root and takes its lineage from the scan,
    so a dispatched child written outside this process's emitter is listed."""
    from kiro_crew.crew_log.read import list_session_units

    session_tree_projection.projection().ensure_seeded()
    _unit("acp-outside", "chat-outside", parent="chat-lead")
    listed = list_session_units(limit=500, scope_slot="chat-lead")
    assert "acp-outside" in json.dumps(listed)
    assert "acp-child" in json.dumps(listed)
    assert "acp-other-7" not in json.dumps(listed)


def test_a_held_unit_citing_an_unheld_creator_is_answered_by_the_scan(lineage_store):
    """X is held and cites S; S's log, from another writer, is not held and cites X.
    The cached fold sees no cycle, so the request takes the scan, which refuses."""
    from kiro_crew.crew_log.read import dispatch_view

    _unit("acp-x", "chat-x", parent="chat-s")
    session_tree_projection.projection().ensure_seeded()
    _unit("acp-s", "chat-s", parent="chat-x")
    view = dispatch_view(("acp-x",))
    assert not view.dispatched_by("acp-x", "chat-s")


def test_one_reconcile_fault_then_a_clean_read_returns_to_the_bounded_path(
    lineage_store, monkeypatch
):
    """A decision re-read that faults once marks the tree incomplete, so dispatch
    scans; the next clean re-read of the same unit takes the fault back out, and
    dispatch is served from the projection again, reading only the named unit."""
    from kiro_crew.crew_log import store as crew_store
    from kiro_crew.crew_log.read import dispatch_view

    proj = session_tree_projection.projection()
    proj.ensure_seeded()
    session_tree_projection.record_adopted("acp-child", "chat-child", 2, "chat-lead")
    real = crew_store.find_last_tree_edge

    def faulting(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("transient")

    monkeypatch.setattr(crew_store, "find_last_tree_edge", faulting)
    session_tree_projection.reconcile_unit_edge("acp-child", "chat-child")
    assert proj.reading().incomplete
    meter = _TreeMeter(monkeypatch)
    assert dispatch_view(("acp-child",)).dispatched_by("acp-child", "chat-lead")
    assert meter.reads >= OTHER_UNITS

    # The clean re-read finds the SAME decision it holds, so no edge changes: the
    # recovery alone must still reach a reader that caches the fold by identity.
    held = proj._edges[("chat-child", "acp-child")]
    monkeypatch.setattr(crew_store, "find_last_tree_edge", real)
    monkeypatch.setattr(session_tree, "edge_record", lambda *_a: held)
    session_tree_projection.reconcile_unit_edge("acp-child", "chat-child")
    assert proj._edges[("chat-child", "acp-child")] is held
    assert not proj.reading().incomplete
    meter.reset()
    assert dispatch_view(("acp-child",)).dispatched_by("acp-child", "chat-lead")
    assert (meter.reads, meter.listings) == (1, 0)  # the named unit's re-read only


def test_forgetting_a_unit_drops_its_reconcile_fault(lineage_store, monkeypatch):
    """A unit removed after its re-read faulted leaves no fault behind: the tree is
    complete again and its fault set holds nothing for a unit that is gone."""
    from kiro_crew.crew_log import store as crew_store

    proj = session_tree_projection.projection()
    proj.ensure_seeded()
    session_tree_projection.record_adopted("acp-child", "chat-child", 2, "chat-lead")

    def faulting(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("transient")

    monkeypatch.setattr(crew_store, "find_last_tree_edge", faulting)
    session_tree_projection.reconcile_unit_edge("acp-child", "chat-child")
    assert proj.reading().incomplete
    session_tree_projection.forget_unit("acp-child")
    assert not proj.reading().incomplete
    assert proj._fault_sids == set()
