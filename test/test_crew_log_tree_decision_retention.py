"""A slot's session-tree decisions outlive the unit that recorded them.

The tree reads each unit's own ``session/adopted``, ``session/released`` and
``session/spawned``, so a superseded unit that retention removes would take them along:
an adopted or released worker would revert to the parent that created it, and a creator
whose only ``session/spawned`` lived there would lose the child it recorded. The slot
carry writes them into the successor before the predecessor may be closed.
"""

from __future__ import annotations

import time

import pytest
from crew_log_drain import assert_drained, settle

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog, emit
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    yield


@pytest.fixture
def carrying(monkeypatch):
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    stp.reset_for_tests()
    monkeypatch.setattr(emit, "_submit_carry_fold", lambda job: job())
    emit.set_session_key_probe(lambda session_id: None)
    try:
        yield
    finally:
        emit.set_session_key_probe(None)
        settle()
        tree = stp.projection()
        stp.reset_for_tests()
        wait_until(
            lambda: not tree._write_scheduled,
            describe=lambda: "the session tree's debounced checkpoint is still sleeping",
        )


def _parent(slot: str):
    node = stp.projection().nodes().get(slot)
    return None if node is None else node.parent_slot


@pytest.mark.usefixtures("carrying")
@pytest.mark.parametrize("decision", ["adopted", "released"])
def test_an_adopt_or_release_survives_the_reset_and_the_sweep(decision):
    w1, w2 = f"{decision}-w1", f"{decision}-w2"
    emit.on_session_opened(
        w1,
        agent="kirocrew",
        slot="worker-1",
        parent_slot="chat-a",
        session_key="dashboard:worker-1",
    )
    assert_drained()
    if decision == "adopted":
        emit.on_session_adopted(w1, slot="worker-1", parent_slot="chat-b")
        want = "chat-b"
    else:
        emit.on_session_released(w1, slot="worker-1")
        want = None
    assert_drained()
    assert _parent("worker-1") == want
    session_ledger.record_update("worker-1", session_id=w1, goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        w2,
        agent="kirocrew",
        slot="worker-1",
        parent_slot="chat-a",
        previous_sid=w1,
        session_key="dashboard:worker-1",
    )
    assert_drained()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, w1))
    store.sweep_expired(0, now=time.time() + 60)
    assert not CrewLog.exists(lg.KIND_SESSION, w1)
    assert _parent("worker-1") == want


@pytest.mark.usefixtures("carrying")
def test_a_creators_only_spawn_survives_the_reset_and_the_sweep():
    c1, c2 = "creator-c1", "creator-c2"
    emit.on_session_opened(c1, agent="kirocrew", slot="creator", session_key="dashboard:creator")
    assert_drained()
    emit.on_session_spawned(c1, creator_slot="creator", child_slot="kid")
    assert_drained()
    assert stp.projection().spawned_creator("kid") == "creator"
    session_ledger.record_update("creator", session_id=c1, goal="g", event="e1")
    assert_drained()
    emit.on_session_opened(
        c2,
        agent="kirocrew",
        slot="creator",
        previous_sid=c1,
        session_key="dashboard:creator",
    )
    assert_drained()
    assert store.closed_for_good(store.unit_dir_for(lg.KIND_SESSION, c1))
    store.sweep_expired(0, now=time.time() + 60)
    assert not CrewLog.exists(lg.KIND_SESSION, c1)
    assert stp.projection().spawned_creator("kid") == "creator"
    assert "kid" in [
        slot for slot, (by, _at) in stp.projection().created().items() if by == "creator"
    ]
    assert _parent("kid") == "creator"


@pytest.mark.usefixtures("carrying")
def test_a_carried_decision_follows_succession_after_a_backward_clock_step(monkeypatch):
    """The newest decision wins by succession, not by header ``createdAt``.

    ``b`` follows ``a`` but its header is stamped earlier (the clock stepped back), so the
    header order lists ``b`` first. ``a`` adopted the worker and ``b`` released it; ``c``
    carries what ``b`` decided, the later decision.
    """
    stamp = [1_700_000_000_000]
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    a, b, c = "clock-a", "clock-b", "clock-c"
    key = "dashboard:worker-1"
    emit.on_session_opened(
        a, agent="kirocrew", slot="worker-1", parent_slot="chat-a", session_key=key
    )
    assert_drained()
    emit.on_session_adopted(a, slot="worker-1", parent_slot="chat-b")
    assert_drained()
    stamp[0] = 1_650_000_000_000
    emit.on_session_opened(
        b, agent="kirocrew", slot="worker-1", parent_slot="chat-a", previous_sid=a, session_key=key
    )
    assert_drained()
    emit.on_session_released(b, slot="worker-1")
    assert_drained()
    assert store.session_units_for_slot("worker-1")[:2] == (b, a), "the control"
    stamp[0] = 1_750_000_000_000
    emit.on_session_opened(
        c, agent="kirocrew", slot="worker-1", parent_slot="chat-a", previous_sid=b, session_key=key
    )
    assert_drained()
    store.sweep_expired(0, now=time.time() + 60)
    assert not CrewLog.exists(lg.KIND_SESSION, a)
    assert not CrewLog.exists(lg.KIND_SESSION, b)
    assert _parent("worker-1") is None


@pytest.mark.usefixtures("carrying")
def test_a_sweep_keeps_the_succession_link_to_a_unit_that_stays(monkeypatch):
    """Removing ``b`` must not leave ``c`` ordered before the ``a`` it follows.

    ``a`` stays (a session key still maps it), ``b`` is superseded and expired, and the
    clock stepped back so ``c``'s header is stamped before ``a``'s.
    """
    stamp = [1_700_000_000_000]
    monkeypatch.setattr(store, "now_ms", lambda: stamp[0])  # flake-ok: fixed clock pin
    emit.set_session_key_probe(
        lambda session_id: "dashboard:link" if session_id == "link-a" else None
    )
    a, b, c = "link-a", "link-b", "link-c"
    emit.on_session_opened(a, agent="kirocrew", slot="link", session_key="dashboard:link")
    assert_drained()
    emit.on_session_opened(
        b, agent="kirocrew", slot="link", previous_sid=a, session_key="dashboard:link"
    )
    assert_drained()
    stamp[0] = 1_650_000_000_000
    emit.on_session_opened(
        c, agent="kirocrew", slot="link", previous_sid=b, session_key="dashboard:link"
    )
    assert_drained()
    store.sweep_expired(0, now=time.time() + 60)
    assert CrewLog.exists(lg.KIND_SESSION, a), "the control: a stays"
    from kiro_crew.crew_log import projection_readers

    order = projection_readers.slot_succession("link")
    assert order.index(c) > order.index(a), order
