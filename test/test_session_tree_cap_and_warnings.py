"""Silent loss in the session tree: a discarded checkpoint, a store past the unit cap,
a unit admitted after its spawns were written, and the order a full tree evicts in.

Each test writes real session logs through the emitter into a data home of its own.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew.crew_log import emit
from kiro_crew.crew_log import session_tree as session_tree_mod
from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord
from kiro_crew.crew_log.session_tree_projection import CHECKPOINT_VERSION, SessionTreeProjection

LOGGER = "kiro_crew.crew_log.session_tree_projection"


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, _floor_monkeypatch: Any) -> Any:
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


def _open(sid: str, slot: str, parent: str = "") -> None:
    emit.on_session_opened(
        sid, agent="kirocrew", slot=slot, model="m", cwd="/w", owner="o", parent_slot=parent
    )


def _checkpoint(**overrides: Any) -> None:
    payload: dict[str, Any] = {
        "ver": CHECKPOINT_VERSION,
        "root": stp._current_root(),
        "records": [],
        "edges": [],
        "spawned": [],
        "marks": {},
    }
    payload.update(overrides)
    path = stp._checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _restart() -> SessionTreeProjection:
    stp.reset_for_tests()
    emit.reset_caches()
    return SessionTreeProjection()


def _warnings(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_an_older_checkpoint_version_is_named_in_a_warning_and_the_tree_is_rebuilt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _open("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert_drained()
    _checkpoint(ver=CHECKPOINT_VERSION - 1)
    proj = _restart()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        proj.ensure_seeded()
    messages = _warnings(caplog)
    assert len(messages) == 1
    assert f"version {CHECKPOINT_VERSION - 1}" in messages[0]
    assert f"expected {CHECKPOINT_VERSION}" in messages[0]
    assert "full rebuild" in messages[0]
    assert proj.nodes()["W"].parent_slot == "L"


def _store_order() -> list[str]:
    """The unit names in the order the replay walks them: the set it builds over the
    root's listing, built the same way, so the order is the one the code under test sees."""
    from kiro_crew.crew_log.checkpoint import CHECKPOINT_DIR
    from kiro_crew.crew_log.schema import KIND_SESSION
    from kiro_crew.crew_log.store import _checked_crew_log_root

    root = _checked_crew_log_root(KIND_SESSION)
    return list({p.name for p in root.iterdir() if p.name != CHECKPOINT_DIR})


def _open_families(count: int) -> list[tuple[str, str, str, str]]:
    """``count`` creators, each with one spawned child that has a log naming it."""
    families = []
    for i in range(count):
        parent_sid, child_sid, parent_slot, child_slot = f"s-l{i}", f"s-w{i}", f"L{i}", f"W{i}"
        _open(parent_sid, parent_slot)
        emit.on_session_spawned(parent_sid, creator_slot=parent_slot, child_slot=child_slot)
        _open(child_sid, child_slot, parent=parent_slot)
        families.append((parent_sid, child_sid, parent_slot, child_slot))
    assert_drained()
    return families


def _last_family(families: list[tuple[str, str, str, str]]) -> tuple[str, str, str, str]:
    """The family whose two units sit latest in the replay's walk order, so a cap that
    admits the walk's first units reads neither."""
    from kiro_crew.session_ledger import _store_name

    order = _store_order()
    return min(
        families,
        key=lambda f: -min(order.index(_store_name(f[0])), order.index(_store_name(f[1]))),
    )


def _assert_live_family_kept(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    families: list[tuple[str, str, str, str]],
    cap: int,
) -> None:
    from kiro_crew.session_ledger import _store_name

    live = _last_family(families)
    order = _store_order()
    assert min(order.index(_store_name(live[0])), order.index(_store_name(live[1]))) >= cap
    _checkpoint()
    monkeypatch.setattr(session_tree_mod, "TREE_UNIT_CAP", cap)
    proj = _restart()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        proj.ensure_seeded((live[0], live[1]))
    assert proj.nodes()[live[3]].parent_slot == live[2]
    assert proj.over_cap is True
    assert any(f"past the {cap}-unit cap" in m for m in _warnings(caplog))


def test_a_store_past_the_cap_admits_live_units_first_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    families = _open_families(8)
    _assert_live_family_kept(monkeypatch, caplog, families, cap=2)


def test_a_unit_admitted_after_its_spawns_were_written_holds_them() -> None:
    _open("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert_drained()
    _checkpoint()
    proj = _restart()
    proj.ensure_seeded()
    proj.forget("s-l")
    assert ("W", "s-l") not in proj._spawned
    proj.apply(OpenedRecord(sid="s-l", slot="L", created_at=1))
    assert ("W", "s-l") in proj._spawned


def test_eviction_keeps_a_unit_that_holds_spawns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = SessionTreeProjection()
    proj.apply_spawned(EdgeRecord(slot="W", parent_slot="L", at=5, sid="s-old", seq=1))
    proj.apply(OpenedRecord(sid="s-old", slot="L", created_at=1))
    proj.apply(OpenedRecord(sid="s-a", slot="A", created_at=2))
    proj.apply(OpenedRecord(sid="s-b", slot="B", created_at=3))
    assert set(proj._records) == {"s-old", "s-b"}


@pytest.mark.parametrize("adopter", [None, "X"])
def test_eviction_keeps_a_moved_childs_unit_so_a_spawn_cannot_restore_the_old_parent(
    monkeypatch: pytest.MonkeyPatch, adopter: str | None
) -> None:
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 3 if adopter else 2)
    proj = SessionTreeProjection()
    proj.apply(OpenedRecord(sid="s-l", slot="L", created_at=1))
    proj.apply_spawned(EdgeRecord(slot="W", parent_slot="L", at=2, sid="s-l", seq=1))
    proj.apply(OpenedRecord(sid="s-w", slot="W", created_at=2, parent_slot="L"))
    proj.apply_edge(EdgeRecord(slot="W", parent_slot=adopter, at=3, sid="s-w", seq=2))
    if adopter:
        proj.apply(OpenedRecord(sid="s-x", slot=adopter, created_at=9))
    proj.apply(OpenedRecord(sid="s-a", slot="A", created_at=5))
    assert "s-w" in proj._records
    assert proj.nodes()["W"].parent_slot == adopter


def test_eviction_keeps_a_child_whose_own_record_names_its_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 3)
    proj = SessionTreeProjection()
    proj.apply_spawned(EdgeRecord(slot="Z", parent_slot="L", at=1, sid="s-l", seq=1))
    proj.apply(OpenedRecord(sid="s-w", slot="W", created_at=1, parent_slot="L"))
    proj.apply(OpenedRecord(sid="s-l", slot="L", created_at=2))
    proj.apply(OpenedRecord(sid="s-a", slot="A", created_at=3))
    proj.apply(OpenedRecord(sid="s-b", slot="B", created_at=4))
    assert "s-w" in proj._records
    assert proj.nodes()["W"].parent_slot == "L"


def test_a_new_top_level_session_is_not_the_record_evicted_when_the_rest_are_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = SessionTreeProjection()
    proj.apply_spawned(EdgeRecord(slot="W", parent_slot="L", at=1, sid="s-l", seq=1))
    proj.apply(OpenedRecord(sid="s-l", slot="L", created_at=1))
    proj.apply(OpenedRecord(sid="s-w", slot="W", created_at=2, parent_slot="L"))
    proj.apply(OpenedRecord(sid="s-t", slot="T", created_at=3))
    assert set(proj._records) == {"s-t", "s-w"}


def test_the_live_pins_follow_the_roster_at_eviction_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    live = ["s-live"]
    proj = SessionTreeProjection()
    proj.set_live_roster(lambda: live)
    proj.apply(OpenedRecord(sid="s-live", slot="V", created_at=1))
    proj.apply(OpenedRecord(sid="s-a", slot="A", created_at=2))
    proj.apply(OpenedRecord(sid="s-b", slot="B", created_at=3))
    assert set(proj._records) == {"s-live", "s-b"}
    live.clear()
    proj.apply(OpenedRecord(sid="s-c", slot="C", created_at=4))
    assert set(proj._records) == {"s-b", "s-c"}


def test_a_creator_admitted_past_the_cap_keeps_its_children_nested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert_drained()
    _checkpoint()
    proj = _restart()
    proj.ensure_seeded()
    proj.forget("s-l")
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj.apply(OpenedRecord(sid="s-a", slot="A", created_at=5))
    proj.apply(OpenedRecord(sid="s-b", slot="B", created_at=6))
    proj.apply(OpenedRecord(sid="s-l", slot="L", created_at=1))
    assert "s-l" in proj._records
    assert ("W", "s-l") in proj._spawned
    assert proj.nodes()["W"].parent_slot == "L"


def test_a_live_unit_the_checkpoint_did_not_hold_is_replayed_ahead_of_older_checkpoint_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _open("s-o1", "O1")
    _open("s-o2", "O2")
    _open("s-l", "L")
    emit.on_session_spawned("s-l", creator_slot="L", child_slot="W")
    assert_drained()
    _checkpoint(
        records=[
            {"sid": "s-o1", "slot": "O1", "at": 1},
            {"sid": "s-o2", "slot": "O2", "at": 2},
        ]
    )
    monkeypatch.setattr(session_tree_mod, "TREE_UNIT_CAP", 2)
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = _restart()
    proj.ensure_seeded(("s-l",))
    assert "s-l" in proj._records
    assert ("W", "s-l") in proj._spawned
    assert proj.nodes()["W"].parent_slot == "L"


def test_a_cap_excluded_unit_keeps_its_adoption_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    _open("s-w1", "W")
    emit.on_session_adopted("s-w1", slot="W", parent_slot="X")
    _open("s-x", "X")
    emit.on_session_opened(
        "s-w2", agent="kirocrew", slot="W", model="m", cwd="/w", owner="o", previous_sid="s-w1"
    )
    assert_drained()
    first = _restart()
    first.ensure_seeded()
    assert first.nodes()["W"].parent_slot == "X"
    assert first.flush_checkpoint() is True

    monkeypatch.setattr(session_tree_mod, "TREE_UNIT_CAP", 2)
    monkeypatch.setattr(stp, "TREE_UNIT_CAP", 2)
    proj = _restart()
    proj.ensure_seeded(("s-w2", "s-x"))
    assert "s-w1" not in proj._records
    assert proj.nodes()["W"].parent_slot == "X"
