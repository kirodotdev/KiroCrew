"""A tree carry that cannot hold every spawn it read keeps the units it read from.

``tree_carries`` merges the older units' ``session/spawned`` entries and keeps the newest
``SPAWNED_PER_UNIT_CAP`` of them. Two creator logs written before the carry existed can
hold more than that between them, and a child still waiting for its first turn has no
record of its creating edge other than its creator's spawn. Carried short and closed, the
older units go to retention and that edge goes with them.
"""

from __future__ import annotations

import time

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.crew_log.session_tree import SPAWNED_PER_UNIT_CAP

CREATOR = "creator-cap"
#: Spawns per older unit: each fits the reader's cap, both together do not.
PER_UNIT = SPAWNED_PER_UNIT_CAP // 2 + 88


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    """The live emitter, the carry folded inline, and a session map that holds nothing.

    The session tree's debounced checkpoint is not armed: its worker sleeps on the
    maintenance pool past the test, and the conftest teardown drops the projection that
    armed it before this fixture's own teardown could wait for it. A restart below then
    seeds from the logs alone.
    """
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    monkeypatch.setattr(stp.SessionTreeProjection, "_schedule_checkpoint", lambda self: None)
    emit.set_session_key_probe(lambda session_id: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        stp.reset_for_tests()


def _older_creator_unit(unit_id: str, children: "range", previous: str = "") -> None:
    """A creator unit an earlier build wrote: an opening and one spawn per child, no carry."""
    log = CrewLog.create(lg.KIND_SESSION, unit_id, owner="default", agent="kirocrew", slot=CREATOR)
    opened: dict = {
        "agent": "kirocrew",
        "slot": CREATOR,
        "model": "",
        "cwd": "",
        "owner": "default",
        "resumed": False,
    }
    if previous:
        opened["previous"] = {"sid": previous}
    log.append("session/opened", opened, src="gateway")
    batch = [
        {"type": "session/spawned", "data": {"child": {"slot": f"kid-{n:04d}"}}, "ignorable": False}
        for n in children
    ]
    for at in range(0, len(batch), 100):
        log.append_many(batch[at : at + 100], src="gateway")
    log.release_ownership()


def _spawned_children(unit_id: str) -> "set[str]":
    directory = store.unit_dir_for(lg.KIND_SESSION, unit_id)
    assert directory is not None
    entries, _cut, _hit_cap = store.find_spawned_entries(directory, 10 * SPAWNED_PER_UNIT_CAP)
    return {entry.data["child"]["slot"] for entry in entries}


@pytest.mark.usefixtures("carrying")
def test_a_carry_short_of_the_older_spawns_closes_neither_older_unit():
    """The GPT trigger: two older units of PER_UNIT spawns each, then the next reset."""
    _older_creator_unit("cap-c1", range(PER_UNIT))
    _older_creator_unit("cap-c2", range(PER_UNIT, 2 * PER_UNIT), previous="cap-c1")
    every_child = {f"kid-{n:04d}" for n in range(2 * PER_UNIT)}

    emit.on_session_opened(
        "cap-c3",
        agent="kirocrew",
        slot=CREATOR,
        previous_sid="cap-c2",
        session_key=f"dashboard:{CREATOR}",
    )
    assert_drained()

    missing = every_child - _spawned_children("cap-c3")
    closed = [
        unit
        for unit in ("cap-c1", "cap-c2")
        if store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, unit))
    ]
    assert not (
        missing and closed
    ), f"{len(missing)} children are carried nowhere, yet {closed} were closed for retention"


@pytest.mark.usefixtures("carrying")
def test_an_early_child_keeps_its_creator_after_the_sweep_and_a_restart():
    """The user-visible outcome: the first child's creating edge outlives the sweep."""
    _older_creator_unit("cap-c1", range(PER_UNIT))
    _older_creator_unit("cap-c2", range(PER_UNIT, 2 * PER_UNIT), previous="cap-c1")
    emit.on_session_opened(
        "cap-c3",
        agent="kirocrew",
        slot=CREATOR,
        previous_sid="cap-c2",
        session_key=f"dashboard:{CREATOR}",
    )
    assert_drained()

    store.sweep_expired(0, now=time.time() + 60)
    # A restart: nothing in memory, the tree seeded again from what the sweep left.
    stp.reset_for_tests()
    stp.projection().ensure_seeded()

    assert stp.spawned_creator("kid-0000") == CREATOR


@pytest.mark.usefixtures("carrying")
def test_an_older_unit_past_the_reader_bound_is_not_closed():
    """One older unit holding more spawns than a reader keeps: its oldest are not carried."""
    _older_creator_unit("cap-c1", range(SPAWNED_PER_UNIT_CAP + 6))
    emit.on_session_opened(
        "cap-c2",
        agent="kirocrew",
        slot=CREATOR,
        previous_sid="cap-c1",
        session_key=f"dashboard:{CREATOR}",
    )
    assert_drained()

    assert not store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, "cap-c1"))
    assert not _spawned_children("cap-c2"), "nothing is carried from a unit read short"


@pytest.mark.usefixtures("carrying")
def test_the_spawns_the_newest_unit_holds_take_their_share_of_the_bound():
    """What the newest unit already holds counts toward what its reader keeps."""
    from kiro_crew.crew_log.tree_carry import tree_carries

    _older_creator_unit("cap-c1", range(PER_UNIT))
    _older_creator_unit("cap-c2", range(PER_UNIT, 2 * PER_UNIT), previous="cap-c1")

    assert tree_carries(CREATOR, "cap-c2", ("cap-c1",)) is None
    held_fewer = SPAWNED_PER_UNIT_CAP - PER_UNIT
    _older_creator_unit("cap-c3", range(2 * PER_UNIT, 2 * PER_UNIT + held_fewer))
    carried = tree_carries(CREATOR, "cap-c3", ("cap-c1",))
    assert carried is not None and len(carried) == PER_UNIT
