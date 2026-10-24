"""The session tree checkpoint's own promises, driven through the projection facade.

The checkpoint is a fold shortcut, never an authority: a row the codec cannot prove is
dropped, a file it cannot trust loads as "no checkpoint", and the debounced write saves
the state it was armed for or re-dirties it so a later change tries again. Each test
calls the code synchronously -- no pool, no thread, no clock -- and patches through
``kiro_crew.crew_log.session_tree_projection``, the surface callers use.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord
from kiro_crew.crew_log.session_tree_projection import (
    CHECKPOINT_VERSION,
    SessionTreeProjection,
)


@pytest.fixture
def submitted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[Any, ...]]]:
    """A data home of this test's own, and a maintenance pool that runs nothing.

    ``apply`` arms a debounced write on the pool; the tests below call the write
    themselves, so a submitted job is only recorded, in the list this yields.
    """
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    jobs: list[tuple[Any, ...]] = []
    pool = type("_Pool", (), {"submit": staticmethod(lambda *a, **_k: jobs.append(a))})
    monkeypatch.setattr("kiro_crew.executors.maintenance_executor", lambda: pool)
    stp.reset_for_tests()
    yield jobs
    stp.reset_for_tests()


def _record(sid: str = "s-1", slot: str = "slot-a", parent: str | None = None) -> OpenedRecord:
    return OpenedRecord(sid=sid, slot=slot, created_at=7, parent_slot=parent)


def _write(payload: Any) -> None:
    path = stp._checkpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = payload if isinstance(payload, str) else json.dumps(payload)
    path.write_text(text, encoding="utf-8")


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ver": CHECKPOINT_VERSION,
        "root": stp._current_root(),
        "records": [{"sid": "s-1", "slot": "slot-a", "at": 7}],
        "edges": [{"slot": "slot-b", "at": 3, "sid": "s-2", "seq": 1, "parent": "slot-a"}],
        "scans": {"s-1": [1, 2, 3, 4]},
        "spawned": [{"slot": "slot-c", "at": 4, "sid": "s-1", "seq": 2, "parent": "slot-a"}],
        "spawn_scans": {"s-1": [5, 6, 7, 8]},
    }
    payload.update(overrides)
    return payload


# --------------------------------------------------------------------------- #
# The debounced write
# --------------------------------------------------------------------------- #


def test_an_apply_arms_one_write_for_the_store_it_was_folded_from(
    submitted: list[tuple[Any, ...]],
) -> None:
    proj = SessionTreeProjection()
    proj.apply(_record("s-1"))
    proj.apply(_record("s-2", "slot-b"))
    assert len(submitted) == 1, "a burst of applies must coalesce into one armed write"
    work, target, epoch, root = submitted[0]
    assert work == proj._debounced_write
    assert target == stp._checkpoint_path()
    assert epoch == proj._cancel_epoch
    assert root == stp._current_root(), "the payload's store is pinned when the write is armed"


def test_the_debounced_write_saves_the_state_it_was_armed_for(
    submitted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[tuple[dict[str, Any], Any]] = []
    monkeypatch.setattr(stp, "CHECKPOINT_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(
        stp, "_save_checkpoint", lambda p, path=None: saved.append((p, path)) or True
    )
    proj = SessionTreeProjection()
    proj.apply(_record("s-1", parent="slot-root"))
    proj.apply_edge(EdgeRecord(slot="slot-a", parent_slot="slot-x", at=9, sid="s-1", seq=3))
    target = stp._checkpoint_path()

    proj._debounced_write(target, proj._cancel_epoch, stp._current_root())

    assert len(saved) == 1
    payload, path = saved[0]
    assert path == target
    assert payload["ver"] == CHECKPOINT_VERSION
    assert payload["root"] == stp._current_root()
    assert payload["records"] == [{"sid": "s-1", "slot": "slot-a", "at": 7, "parent": "slot-root"}]
    assert payload["edges"] == [
        {"slot": "slot-a", "at": 9, "sid": "s-1", "seq": 3, "parent": "slot-x"}
    ]
    assert payload["spawned"] == []
    assert proj._dirty is False
    assert proj._write_scheduled is False


def test_a_write_that_does_not_land_leaves_the_state_owed(
    submitted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stp, "CHECKPOINT_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(stp, "_save_checkpoint", lambda p, path=None: False)
    proj = SessionTreeProjection()
    proj.apply(_record())

    proj._debounced_write(stp._checkpoint_path(), proj._cancel_epoch)

    assert proj._dirty is True, "a lost write must be retried by the next change"


def test_a_write_with_nothing_owed_saves_nothing(
    submitted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved: list[Any] = []
    monkeypatch.setattr(stp, "CHECKPOINT_DEBOUNCE_SECS", 0)
    monkeypatch.setattr(stp, "_save_checkpoint", lambda p, path=None: saved.append(p) or True)
    proj = SessionTreeProjection()

    proj._debounced_write(stp._checkpoint_path(), proj._cancel_epoch)

    assert saved == []


def test_a_flush_round_trips_through_the_load(submitted: list[tuple[Any, ...]]) -> None:
    proj = SessionTreeProjection()
    proj.apply(_record("s-1", parent="slot-root"))
    assert proj.flush_checkpoint() is True
    loaded = stp._load_checkpoint()
    assert loaded is not None
    records, edges, scans, spawned, spawn_scans = loaded
    assert records == {"s-1": _record("s-1", parent="slot-root")}
    assert (edges, scans, spawned, spawn_scans) == ({}, {}, {}, {})


def test_an_empty_clean_projection_has_nothing_to_flush(submitted: list[tuple[Any, ...]]) -> None:
    assert SessionTreeProjection().flush_checkpoint() is False


# --------------------------------------------------------------------------- #
# The load: anything it cannot prove is no checkpoint at all
# --------------------------------------------------------------------------- #


def test_a_well_formed_checkpoint_loads_every_half(submitted: list[tuple[Any, ...]]) -> None:
    _write(_payload())
    loaded = stp._load_checkpoint()
    assert loaded is not None
    records, edges, scans, spawned, spawn_scans = loaded
    assert set(records) == {"s-1"}
    assert set(edges) == {("slot-b", "s-2")}
    assert scans == {"s-1": (1, 2, 3, 4)}
    assert set(spawned) == {("slot-c", "s-1")}
    assert spawn_scans == {"s-1": (5, 6, 7, 8)}


@pytest.mark.parametrize("text", ["{not json", "[]"], ids=["unparseable", "not-an-object"])
def test_a_checkpoint_that_is_not_a_json_object_loads_as_none(
    submitted: list[tuple[Any, ...]], text: str
) -> None:
    _write(text)
    assert stp._load_checkpoint() is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"ver": CHECKPOINT_VERSION - 1},
        {"root": "/another/store"},
        {"root": None},
        {"records": {}},
        {"edges": None},
        {"scans": []},
        {"spawned": None},
        {"spawn_scans": []},
    ],
    ids=[
        "older-version",
        "another-store",
        "no-root",
        "records-not-a-list",
        "edges-missing",
        "scans-not-a-map",
        "spawned-missing",
        "spawn-scans-not-a-map",
    ],
)
def test_a_checkpoint_it_cannot_prove_loads_as_none(
    submitted: list[tuple[Any, ...]], overrides: dict[str, Any]
) -> None:
    """Each case differs from a payload that loads in the one field it names, so the
    refusal is that field's: the payload is built here, after the home is pinned, because
    its ``root`` names the store of the home in force when it is built."""
    _write(_payload())
    assert stp._load_checkpoint() is not None
    _write(_payload(**overrides))
    assert stp._load_checkpoint() is None


def test_a_missing_checkpoint_loads_as_none(submitted: list[tuple[Any, ...]]) -> None:
    assert not stp._checkpoint_path().exists()
    assert stp._load_checkpoint() is None


def test_an_oversized_checkpoint_is_discarded_unread(
    submitted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _write(_payload())
    monkeypatch.setattr(stp, "MAX_CHECKPOINT_BYTES", 8)
    assert stp._load_checkpoint() is None


def test_a_save_past_the_ceiling_writes_nothing(
    submitted: list[tuple[Any, ...]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stp, "MAX_CHECKPOINT_BYTES", 8)
    assert stp._save_checkpoint(_payload()) is False
    assert not stp._checkpoint_path().exists()


def test_a_save_that_raises_reports_it_did_not_land(submitted: list[tuple[Any, ...]]) -> None:
    assert stp._save_checkpoint({"unserialisable": object()}) is False


# --------------------------------------------------------------------------- #
# The codec: a row is refused, never coerced or truncated
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw",
    [
        None,
        {"slot": "a", "at": 1},
        {"sid": "", "slot": "a", "at": 1},
        {"sid": "s", "slot": 3, "at": 1},
        {"sid": "s", "slot": "a", "at": True},
        {"sid": "s", "slot": "a", "at": "1"},
        {"sid": "s", "slot": "a", "at": 1, "parent": 5},
        {"sid": "s", "slot": "a", "at": 1, "prev": 5},
        {"sid": "s" * (stp.MAX_ACP_SESSION_ID_LEN + 1), "slot": "a", "at": 1},
        {"sid": "s", "slot": "a" * (stp.MAX_SHORT_STRING + 1), "at": 1},
        {"sid": "s", "slot": "a", "at": 1, "parent": "p" * (stp.MAX_SHORT_STRING + 1)},
        {"sid": "s", "slot": "a", "at": 1, "prev": "q" * (stp.MAX_ACP_SESSION_ID_LEN + 1)},
    ],
)
def test_a_record_row_it_cannot_read_is_dropped(raw: Any) -> None:
    assert stp._record_from_json(raw) is None


def test_a_record_row_round_trips_with_its_optional_halves() -> None:
    record = OpenedRecord(sid="s", slot="a", created_at=1, parent_slot="p", previous_sid="o")
    assert stp._record_from_json(stp._record_to_json(record)) == record
    bare = OpenedRecord(sid="s", slot="a", created_at=1)
    assert stp._record_to_json(bare) == {"sid": "s", "slot": "a", "at": 1}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"at": 1, "sid": "s"},
        {"slot": "", "at": 1, "sid": "s"},
        {"slot": "a" * (stp.MAX_SHORT_STRING + 1), "at": 1, "sid": "s"},
        {"slot": "a", "at": False, "sid": "s"},
        {"slot": "a", "at": 1, "sid": ""},
        {"slot": "a", "at": 1, "sid": "s" * (stp.MAX_ACP_SESSION_ID_LEN + 1)},
        {"slot": "a", "at": 1, "sid": "s", "parent": 2},
        {"slot": "a", "at": 1, "sid": "s", "parent": "p" * (stp.MAX_SHORT_STRING + 1)},
        {"slot": "a", "at": 1, "sid": "s", "seq": -1},
        {"slot": "a", "at": 1, "sid": "s", "seq": True},
    ],
)
def test_a_decision_row_it_cannot_read_is_dropped(raw: Any) -> None:
    assert stp._edge_from_json(raw) is None


def test_a_decision_row_round_trips_and_a_release_omits_its_parent() -> None:
    adoption = EdgeRecord(slot="a", parent_slot="p", at=2, sid="s", seq=4)
    assert stp._edge_from_json(stp._edge_to_json(adoption)) == adoption
    release = EdgeRecord(slot="a", parent_slot=None, at=2, sid="s", seq=5)
    assert "parent" not in stp._edge_to_json(release)
    assert stp._edge_from_json(stp._edge_to_json(release)) == release


def test_a_scan_row_that_does_not_parse_costs_only_its_unit() -> None:
    rows = {
        "kept": [1, 2, 3, 4],
        "": [1, 2, 3, 4],
        "short": [1, 2, 3],
        "not-a-list": "1,2,3,4",
        "a-bool": [1, 2, 3, True],
        "a-str": [1, 2, "3", 4],
    }
    assert stp._scans_from_json(rows) == {"kept": (1, 2, 3, 4)}


def test_every_door_refuses_an_over_long_string() -> None:
    assert stp._within_bounds(_record()) is True
    assert stp._within_bounds(_record(sid="s" * (stp.MAX_ACP_SESSION_ID_LEN + 1))) is False
    edge = EdgeRecord(slot="a", parent_slot="p" * (stp.MAX_SHORT_STRING + 1), at=1, sid="s")
    assert stp._edge_within_bounds(edge) is False
    assert stp._edge_within_bounds(EdgeRecord(slot="a", parent_slot=None, at=1, sid="s")) is True
