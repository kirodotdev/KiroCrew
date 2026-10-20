"""The session tree projection's cold start, on the paths a healthy store never takes.

A replay that cannot list the store root, read a unit's head, or read a unit's decision
or spawns serves what it has and says the reconciliation is incomplete; an install keeps
out a unit removed while the scan ran; and a failed seed's re-attempt is armed on a timer
that re-arms when it fires early and stands down when the store or the projection moved.
Each test drives the code synchronously: the store's readers are patched through
``kiro_crew.crew_log.store`` and the timer is a recording stand-in, so nothing here waits
on a clock or a thread.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.crew_log import session_tree_projection as stp
from kiro_crew.crew_log import store as crew_store
from kiro_crew.crew_log.session_tree import EdgeRecord, OpenedRecord, _ScanFaults
from kiro_crew.crew_log.session_tree_projection import SessionTreeProjection
from kiro_crew.session_ledger import _store_name


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SessionTreeProjection]:
    """A projection over a data home of this test's own, with a pool that runs nothing."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    pool = type("_Pool", (), {"submit": staticmethod(lambda *a, **k: None)})
    monkeypatch.setattr("kiro_crew.executors.maintenance_executor", lambda: pool)
    stp.reset_for_tests()
    projection = SessionTreeProjection()
    yield projection
    projection.cancel_pending_checkpoint()
    stp.reset_for_tests()


class _UnlistableRoot:
    """A store root whose listing fails the way a permission fault does."""

    def iterdir(self) -> Iterator[Path]:
        raise PermissionError("the store root cannot be listed")

    def __truediv__(self, name: str) -> Path:
        raise AssertionError("nothing below an unlistable root is read")


def _record(sid: str, slot: str, parent: str | None = None) -> OpenedRecord:
    return OpenedRecord(sid=sid, slot=slot, created_at=1, parent_slot=parent)


def _unit_root(tmp_path: Path, *sids: str) -> Path:
    """A store root holding one directory per unit, named as the store names them."""
    root = tmp_path / "store-root"
    for sid in sids:
        (root / _store_name(sid)).mkdir(parents=True)
    return root


def _raise(exc: BaseException) -> Any:
    def _boom(*_a: Any, **_k: Any) -> Any:
        raise exc

    return _boom


# --------------------------------------------------------------------------- #
# The tail replay
# --------------------------------------------------------------------------- #


def test_an_unlistable_root_serves_the_checkpoint_and_reports_a_gap_no_unit_owns(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", lambda kind: _UnlistableRoot())
    faults = _ScanFaults()
    held = {"s-1": _record("s-1", "slot-a")}

    records, edges, scans, incomplete, over_cap = proj._replay_tail(held, {}, None, faults=faults)

    assert (records, edges, scans) == (held, {}, {})
    assert (incomplete, over_cap) == (True, False)
    assert faults.unattributed is True


def test_a_unit_whose_head_cannot_be_read_marks_the_replay_incomplete(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _unit_root(tmp_path, "s-new")
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", lambda kind: root)
    monkeypatch.setattr(crew_store, "oldest_segment", lambda directory: directory / "log.jsonl")
    monkeypatch.setattr(crew_store, "read_head", _raise(OSError("unreadable head")))

    records, _edges, _scans, incomplete, over_cap = proj._replay_tail({}, {}, None)

    assert records == {}
    assert (incomplete, over_cap) == (True, False)


def test_a_unit_created_but_not_announced_is_not_a_gap(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _unit_root(tmp_path, "s-new")
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", lambda kind: root)
    monkeypatch.setattr(crew_store, "oldest_segment", lambda directory: directory / "log.jsonl")
    monkeypatch.setattr(crew_store, "read_head", lambda segment: (None, None, False))

    records, _edges, _scans, incomplete, _over_cap = proj._replay_tail({}, {}, None)

    assert records == {}
    assert incomplete is False


def test_a_unit_that_cannot_be_stated_or_read_is_suspect_and_keeps_its_decision(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _unit_root(tmp_path, "s-stat", "s-read")
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", lambda kind: root)

    def identity(directory: Path) -> tuple[int, int, int, int]:
        if directory.name == _store_name("s-stat"):
            raise OSError("cannot stat the unit")
        return (1, 1, 1, 1)

    monkeypatch.setattr(crew_store, "tree_edge_scan_identity", identity)
    monkeypatch.setattr(crew_store, "find_last_tree_edge", _raise(ValueError("torn decision")))
    held = {"s-stat": _record("s-stat", "slot-a"), "s-read": _record("s-read", "slot-b")}
    decision = EdgeRecord(slot="slot-b", parent_slot="slot-a", at=5, sid="s-read", seq=2)
    faults = _ScanFaults()

    records, edges, scans, incomplete, _over_cap = proj._replay_tail(
        held, {("slot-b", "s-read"): decision}, None, faults=faults
    )

    assert records == held
    assert edges == {("slot-b", "s-read"): decision}, "an unread decision is kept, not dropped"
    assert scans == {}
    assert incomplete is True
    assert sorted(faults.sids) == ["s-read", "s-stat"]


def test_spawns_are_kept_and_reported_when_the_root_cannot_be_resolved(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", _raise(OSError("no root")))
    records = {"s-1": _record("s-1", "slot-a")}
    spawn = EdgeRecord(slot="slot-c", parent_slot="slot-a", at=2, sid="s-1", seq=1)
    gone = EdgeRecord(slot="slot-d", parent_slot="slot-x", at=2, sid="s-gone", seq=1)
    loaded = {("slot-c", "s-1"): spawn, ("slot-d", "s-gone"): gone}

    spawned, scans, incomplete = proj._replay_spawned(
        records, loaded, {"s-1": (1, 1, 1, 1), "s-gone": (2, 2, 2, 2)}
    )

    assert spawned == {("slot-c", "s-1"): spawn}, "a spawn whose creator unit is gone is dropped"
    assert scans == {"s-1": (1, 1, 1, 1)}
    assert incomplete is True


def test_a_creator_whose_spawns_cannot_be_read_keeps_them_and_is_suspect(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = _unit_root(tmp_path, "s-stat", "s-read")
    monkeypatch.setattr(crew_store, "_checked_crew_log_root", lambda kind: root)

    def identity(directory: Path) -> tuple[int, int, int, int]:
        if directory.name == _store_name("s-stat"):
            raise OSError("cannot stat the unit")
        return (3, 3, 3, 3)

    monkeypatch.setattr(crew_store, "tree_edge_scan_identity", identity)
    monkeypatch.setattr(crew_store, "find_spawned_entries", _raise(OSError("torn spawns")))
    records = {"s-stat": _record("s-stat", "slot-a"), "s-read": _record("s-read", "slot-b")}
    spawn = EdgeRecord(slot="slot-c", parent_slot="slot-b", at=2, sid="s-read", seq=1)
    faults = _ScanFaults()

    spawned, scans, incomplete = proj._replay_spawned(
        records, {("slot-c", "s-read"): spawn}, {}, faults=faults
    )

    assert spawned == {("slot-c", "s-read"): spawn}
    assert scans == {}
    assert incomplete is True
    assert sorted(faults.sids) == ["s-read", "s-stat"]


# --------------------------------------------------------------------------- #
# The install
# --------------------------------------------------------------------------- #


def test_a_unit_removed_while_the_scan_ran_takes_its_decisions_and_spawns_with_it(
    proj: SessionTreeProjection,
) -> None:
    kept = EdgeRecord(slot="slot-a", parent_slot="slot-p", at=1, sid="s-kept", seq=1)
    scanned_gone = EdgeRecord(slot="slot-b", parent_slot="slot-p", at=1, sid="s-gone", seq=1)
    held_gone = EdgeRecord(slot="slot-c", parent_slot="slot-p", at=2, sid="s-gone", seq=2)
    spawn_gone = EdgeRecord(slot="slot-d", parent_slot="slot-p", at=3, sid="s-gone", seq=3)
    with proj._lock:
        proj._edges = {("slot-c", "s-gone"): held_gone}
        proj._spawned = {("slot-d", "s-gone"): spawn_gone}
        proj._forgotten_while_seeding = {"s-gone"}
        proj._install_seed_edges_locked(
            {("slot-a", "s-kept"): kept, ("slot-b", "s-gone"): scanned_gone}
        )
        proj._install_seed_spawned_locked({("slot-d", "s-gone"): spawn_gone})
        edges, spawned = dict(proj._edges), dict(proj._spawned)
    assert edges == {("slot-a", "s-kept"): kept}
    assert spawned == {}


# --------------------------------------------------------------------------- #
# The re-attempt of a failed seed
# --------------------------------------------------------------------------- #


class _Timers:
    """Stands in for ``_start_seed_retry_timer``: records each arm instead of starting one."""

    def __init__(self) -> None:
        self.armed: list[tuple[float, Any]] = []

    def __call__(self, delay: float, fire: Any) -> None:
        self.armed.append((delay, fire))


def test_a_retry_with_no_deadline_waits_the_whole_cooldown(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers = _Timers()
    monkeypatch.setattr(proj, "_start_seed_retry_timer", timers)
    monkeypatch.setattr(stp, "SEED_RETRY_COOLDOWN_SECS", 7.0)

    proj._arm_seed_retry(stp._current_root(), proj._cancel_epoch)

    assert [delay for delay, _fire in timers.armed] == [7.0 + 0.001]


def test_a_timer_that_fires_early_re_arms_for_the_rest(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers = _Timers()
    seeds: list[tuple[str, ...]] = []
    monkeypatch.setattr(proj, "_start_seed_retry_timer", timers)
    monkeypatch.setattr(proj, "ensure_seeded", lambda live_sids=(): seeds.append(live_sids))
    with proj._lock:
        proj._seed_retry_at = float("inf")
    proj._arm_seed_retry(stp._current_root(), proj._cancel_epoch)
    _delay, fire = timers.armed[0]

    fire()

    assert len(timers.armed) == 2, "an early tick must re-arm rather than give up"
    assert timers.armed[1][0] == float("inf")
    assert seeds == []


def test_a_due_timer_seeds_the_store_it_was_armed_for(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers = _Timers()
    seeds: list[tuple[str, ...]] = []
    monkeypatch.setattr(proj, "_start_seed_retry_timer", timers)
    monkeypatch.setattr(proj, "ensure_seeded", lambda live_sids=(): seeds.append(live_sids))
    with proj._lock:
        proj._seed_retry_at = float("-inf")
    proj._arm_seed_retry(stp._current_root(), proj._cancel_epoch)
    _delay, fire = timers.armed[0]

    fire()

    assert seeds == [()]
    assert proj._seed_retry_timer is None


def test_a_due_timer_stands_down_when_the_store_or_the_projection_moved(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    timers = _Timers()
    seeds: list[tuple[str, ...]] = []
    monkeypatch.setattr(proj, "_start_seed_retry_timer", timers)
    monkeypatch.setattr(proj, "ensure_seeded", lambda live_sids=(): seeds.append(live_sids))
    with proj._lock:
        proj._seed_retry_at = float("-inf")
    proj._arm_seed_retry("/another/store", proj._cancel_epoch)
    proj._arm_seed_retry(stp._current_root(), proj._cancel_epoch - 1)
    for _delay, fire in list(timers.armed):
        fire()
    with proj._lock:
        proj._seed_retry_at = None
    proj._arm_seed_retry(stp._current_root(), proj._cancel_epoch)
    timers.armed[-1][1]()

    assert seeds == []
    assert len(timers.armed) == 3


class _FakeTimer:
    """A ``threading.Timer`` that never starts a thread."""

    made: list[_FakeTimer] = []

    def __init__(self, delay: float, fire: Any) -> None:
        self.delay, self.fire = delay, fire
        self.daemon = False
        self.started = self.cancelled = False
        _FakeTimer.made.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


def test_one_timer_is_held_and_a_second_arm_cancels_the_first(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeTimer.made = []
    fake = type("_Threading", (), {"Timer": _FakeTimer, "Lock": threading.Lock})
    monkeypatch.setattr(stp, "threading", fake)

    proj._start_seed_retry_timer(5.0, lambda: None)
    proj._start_seed_retry_timer(6.0, lambda: None)
    proj.cancel_pending_checkpoint()

    first, second = _FakeTimer.made
    assert (first.started, first.daemon, first.cancelled) == (True, True, True)
    assert (second.started, second.daemon, second.cancelled) == (True, True, True)
    assert proj._seed_retry_timer is None


def test_a_timer_that_cannot_be_started_is_swallowed(
    proj: SessionTreeProjection, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = type("_Threading", (), {"Timer": staticmethod(_raise(RuntimeError("no threads")))})
    monkeypatch.setattr(stp, "threading", fake)

    proj._start_seed_retry_timer(5.0, lambda: None)

    assert proj._seed_retry_timer is None
