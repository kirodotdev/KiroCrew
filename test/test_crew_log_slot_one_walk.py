"""One crew-log walk feeds every fold of a slot on a cold or stale-resumed prime.

Before this, the background prime folded one (slot, fold) cell per job, and each cold or
stale cell walked the slot's units on its own: 18 cold folds of one 136 MB unit read it
18 times. Now the prime folds every queued cell of the slot that would walk the same
units over one walk, and each cell keeps its own state, savepoint and watermark.

Every test runs the same scenario twice: once one cell at a time (the per-fold path,
``_per_fold``), and once with every cell queued together (``_queued``), and requires the
two to leave identical cells and savepoint files. The walk counts are what the change
buys; the equality is what it must never cost.
"""

from __future__ import annotations

import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import (
    CrewLog,
)
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import (
    projection_readers,
    projection_slot_memo,
    slot_backfill_child,
    slot_prime,
)
from kiro_crew.crew_log.projection_registry import _FOLDS
from kiro_crew.crew_log.store import crew_log_dir

SLOT = "chat-onewalk"
FIRST = "acp-ow-first"
SECOND = "acp-ow-second"
CREW = "crew-ow"
#: Folds this test drives; the ones reading the same units as the ledger are the group.
CANDIDATES = ("ledger", "radar", "mistakes", "goal", "waiting", "status", "timeline", "tools")


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, eager folder and backfill child off, nothing warm."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(crew_log_eager, "_publish_fold", lambda *a, **k: None)
    _floor_monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    _floor_monkeypatch.setattr(projection_slot_memo, "SAVE_DURING_FOLD_ENTRIES", 5)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    slot_prime.reset_for_tests()
    yield
    slot_prime.reset_for_tests()
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


@pytest.fixture
def walks(monkeypatch) -> "dict[str, int]":
    """``CrewLog._walk_from`` calls per unit: one call is one walk of that unit's log."""
    counts: "dict[str, int]" = {}
    real = CrewLog._walk_from

    def counted(self, *args, **kwargs):
        counts[self.id] = counts.get(self.id, 0) + 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "_walk_from", counted)
    return counts


def _unit(unit_id: str) -> None:
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=SLOT)


def _entries(unit_id: str, first: int, last: int) -> None:
    for step in range(first, last + 1):
        crew_log_emit.on_ledger_recorded(
            unit_id,
            {"slot": SLOT, "next": f"step {step}", "event": f"e{step}", "event_kind": "progress"},
        )
        crew_log_emit.on_radar_recorded(
            unit_id,
            {
                "crew_id": CREW,
                "owner": "kirodotdev",
                "repo": "KiroCrew",  # brand-ok: the repo half of the slug, an identifier
                "number": 100 + step % 3,
                "event": f"r{step}",
                "event_kind": "investigate",
            },
        )
        assert_drained()


def _slot() -> None:
    _unit(FIRST)
    _entries(FIRST, 1, 12)
    _unit(SECOND)
    _entries(SECOND, 13, 30)


def _group() -> "list[str]":
    """The candidate folds that read exactly the ledger's unit list for this slot."""
    units = projection_readers._slot_units_for_fold(SLOT, "ledger")
    names = [n for n in CANDIDATES if projection_readers._slot_units_for_fold(SLOT, n) == units]
    assert len(names) >= 3 and units == (FIRST, SECOND), (names, units)
    return names


def _restart() -> None:
    """What a gateway restart leaves: no warm cell, no prime state, files as they are."""
    crew_log.forget_slot_folds()
    slot_prime.reset_for_tests()


def _per_fold(names: "list[str]") -> None:
    """Main's shape: each cell primed alone, one after the other."""
    for name in names:
        slot_prime.schedule(SLOT, name, for_reader=True)
        assert slot_prime.drain()


def _queued(names: "list[str]") -> None:
    """Every cell queued before the prime thread starts, as at boot."""
    with slot_prime._fold_gate:
        for name in names:
            slot_prime.schedule(SLOT, name, for_reader=True)
    assert slot_prime.drain()


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=repr)


def _cells(names: "list[str]") -> "dict[str, str]":
    """Each held cell's state, watermark, unit marks, reach, saved ordinal and position."""
    out = {}
    for name in names:
        memo = projection_slot_memo._slot_memos.get((str(data_home()), SLOT, name))
        if memo is None:
            continue
        state, watermark = memo.registry.cells(SLOT)[name]
        out[name] = _canon(
            [state, watermark, memo.units, memo.marks, memo.reached, memo.saved, memo.resume]
        )
    return out


def _savepoint_dirs() -> "list[Path]":
    return [
        crew_log_dir(lg.KIND_SESSION, unit) / savepoints.SLOT_CHECKPOINT_DIR
        for unit in (FIRST, SECOND)
    ]


def _beside(path: str, unit: str) -> bool:
    """Whether a savepoint file sits in *unit*'s directory (``<unit>-<hash>``), any OS."""
    return Path(path).parent.parent.name.startswith(f"{unit}-")


def _files() -> "dict[str, bytes]":
    return {
        str(path): path.read_bytes()
        for folder in _savepoint_dirs()
        if folder.is_dir()
        for path in sorted(folder.rglob("*.json"))
    }


def _put_files(files: "dict[str, bytes]") -> None:
    for folder in _savepoint_dirs():
        for path in folder.rglob("*.json") if folder.is_dir() else ():
            path.unlink()
    for name, raw in files.items():
        Path(name).parent.mkdir(parents=True, exist_ok=True)
        Path(name).write_bytes(raw)


def _outcome(names: "list[str]") -> "tuple[dict[str, str], dict[str, str]]":
    files = {path: _canon(json.loads(raw)) for path, raw in _files().items()}
    return _cells(names), files


def _differing(left, right) -> "list[str]":
    """The cell names and savepoint paths whose values differ between two outcomes."""
    return sorted(
        key
        for mine, theirs in zip(left, right)
        for key in set(mine) | set(theirs)
        if mine.get(key) != theirs.get(key)
    )


def _both_ways(names: "list[str]", walks: "dict[str, int]", start: "dict[str, bytes]"):
    """(per-fold outcome, queued outcome, queued walk counts) from the same files."""
    _put_files(start)
    _restart()
    _per_fold(names)
    per_fold = _outcome(names)
    _put_files(start)
    _restart()
    walks.clear()
    _queued(names)
    return per_fold, _outcome(names), dict(walks)


def test_a_cold_prime_of_every_fold_walks_each_unit_once(walks):
    _slot()
    names = _group()
    per_fold, queued, counted = _both_ways(names, walks, {})

    assert counted == {FIRST: 1, SECOND: 1}
    assert set(queued[0]) == set(names)
    assert _differing(queued, per_fold) == []


def test_a_stale_resumed_prime_walks_the_newest_unit_once(walks):
    _slot()
    names = _group()
    _per_fold(names)  # writes every cell's savepoint
    _restart()
    _entries(SECOND, 31, 60)  # every savepoint is now stale
    stale = _files()
    assert len(stale) >= len(names)

    per_fold, queued, counted = _both_ways(names, walks, stale)

    assert counted == {SECOND: 1}
    assert set(queued[0]) == set(names)
    assert _differing(queued, per_fold) == []


def test_a_fold_refused_at_admission_folds_alone_and_the_rest_share(walks):
    _slot()
    names = _group()
    _per_fold(names)
    _restart()
    _entries(SECOND, 31, 60)
    stale = _files()
    refused = next(p for p in stale if Path(p).name == f"{names[1]}.json" and _beside(p, SECOND))
    stale[refused] = b"{not json"

    per_fold, queued, counted = _both_ways(names, walks, stale)

    # The rest share one tail walk of SECOND. The refused fold takes its own path, as on
    # main: it resumes from its mid-fold savepoint beside FIRST (two short reads of FIRST,
    # one of SECOND).
    assert counted == {FIRST: 2, SECOND: 2}
    assert set(queued[0]) == set(names)
    assert _differing(queued, per_fold) == []


def test_one_fold_raising_leaves_every_fold_as_the_per_fold_path_does(walks, monkeypatch):
    _slot()
    names = _group()
    broken = names[1]
    real = _FOLDS[broken]

    def step(state: Any, entry: Any) -> None:
        if entry.seq >= 7:
            raise RuntimeError("a fold defect on a real entry")
        real.step(state, entry)

    monkeypatch.setitem(_FOLDS, broken, replace(real, step=step))
    per_fold, queued, _counted = _both_ways(names, walks, {})

    assert broken not in queued[0]
    assert set(queued[0]) == set(names) - {broken}
    assert queued[0] == per_fold[0]
    # Beside the newest unit every savepoint is the per-fold path's. Beside FIRST the
    # aborted shared pass already wrote mid-fold savepoints, so each good fold's own pass
    # then resumes from one and seals FIRST at its end: same files, further along.
    newest = [{p: v for p, v in out[1].items() if _beside(p, SECOND)} for out in (queued, per_fold)]
    assert newest[0] == newest[1] and len(newest[0]) == len(names) - 1
    older = [
        sorted(Path(p).name for p in out[1] if _beside(p, FIRST)) for out in (queued, per_fold)
    ]
    assert older[0] == older[1]


def test_reads_during_the_shared_walk_answer_as_main_does(walks, monkeypatch):
    """A cold=False read of a joined cell answers "not ready"; a warm cell's is not held;
    a cold=True read waits and returns the per-fold value."""
    _slot()
    names = _group()
    warm, joined = names[0], names[1]
    _per_fold(names)
    expected = {name: _canon(crew_log.read_slot_projection(SLOT, name).value) for name in names}
    _put_files({})
    _restart()
    crew_log.read_slot_projection(SLOT, warm)  # warm before the prime: not in the walk
    seen: "dict[str, Any]" = {}
    waiter: "list[threading.Thread]" = []
    real = CrewLog._walk_from

    def during(self, *args, **kwargs):
        if not seen:
            seen["started"] = True

            def not_ready() -> None:
                try:
                    crew_log.read_slot_projection(SLOT, joined, cold=False)
                    seen["joined"] = "served"
                except projection_slot_memo.SlotFoldNotReady:
                    seen["joined"] = "not ready"
                seen["warm"] = _canon(crew_log.read_slot_projection(SLOT, warm, cold=False).value)

            probe = threading.Thread(target=not_ready)
            probe.start()
            probe.join(10)
            assert not probe.is_alive()

            def cold_read() -> None:
                seen["cold"] = _canon(crew_log.read_slot_projection(SLOT, joined).value)

            waiter.append(threading.Thread(target=cold_read))
            waiter[0].start()
        return real(self, *args, **kwargs)

    monkeypatch.setattr(CrewLog, "_walk_from", during)
    _queued(names)
    waiter[0].join(30)
    assert not waiter[0].is_alive()

    assert seen["joined"] == "not ready"
    assert seen["warm"] == expected[warm]
    assert seen["cold"] == expected[joined]
    assert {n: _canon(crew_log.read_slot_projection(SLOT, n).value) for n in names} == expected


def _stale_slot() -> "tuple[list[str], dict[str, bytes]]":
    """The slot with every cell's savepoint written and then made stale."""
    _slot()
    names = _group()
    _per_fold(names)
    _restart()
    _entries(SECOND, 31, 60)
    return names, _files()


def test_a_stale_prime_shares_its_walk_while_the_backfill_child_is_usable(walks, monkeypatch):
    """The gateway turns the child on at boot. It folds cold cells, never resumed ones, so
    the resumed cells still share one tail walk."""
    names, stale = _stale_slot()
    monkeypatch.setattr(slot_backfill_child, "usable", lambda: True)
    monkeypatch.setattr(slot_backfill_child, "backfill", lambda *a, **k: False)

    per_fold, queued, counted = _both_ways(names, walks, stale)

    assert counted == {SECOND: 1}
    assert set(queued[0]) == set(names)
    assert _differing(queued, per_fold) == []


def test_a_busy_sibling_is_left_out_and_a_held_one_answers_not_ready_at_once(monkeypatch):
    """The prime never waits for a sibling's lock another pass holds, and a ``cold=False``
    read of a cell the shared walk holds answers "not ready" while the walk is still in
    progress: the walk does not go on until that read has returned."""
    names, stale = _stale_slot()
    _put_files(stale)
    _restart()
    busy, joined = names[2], names[0]
    key = (str(data_home()), SLOT, busy)
    entry = projection_slot_memo._acquire_fold_lock(key)
    entry.lock.acquire()
    released = threading.Event()

    def release() -> None:
        if not released.is_set():
            released.set()
            entry.lock.release()
            projection_slot_memo._release_fold_lock(key, entry)

    entered = threading.Event()  # the shared walk reached ``_walk_from``
    walking = threading.Event()  # set while the shared walk is inside ``_walk_from``
    answered = threading.Event()  # the probe's read returned
    done = threading.Event()
    seen: "dict[str, Any]" = {}
    real = CrewLog._walk_from

    def probe() -> None:
        try:
            crew_log.read_slot_projection(SLOT, joined, cold=False)
            seen["joined"] = "served"
        except projection_slot_memo.SlotFoldNotReady:
            seen["joined"] = "not ready"
        seen["walk_still_inside"] = walking.is_set()
        answered.set()

    def during(self, *args, **kwargs):
        if entered.is_set():
            yield from real(self, *args, **kwargs)
            return
        seen["busy_held"] = not released.is_set()
        walking.set()
        entered.set()
        try:
            threading.Thread(target=probe, daemon=True).start()
            seen["answered"] = answered.wait(timeout=30)
            yield from real(self, *args, **kwargs)
        finally:
            walking.clear()

    def drive() -> None:
        _queued(names)
        done.set()

    monkeypatch.setattr(CrewLog, "_walk_from", during)
    driver = threading.Thread(target=drive, daemon=True)
    driver.start()
    try:
        # An old prime that waits for the busy lock never reaches the walk while it is held.
        reached_while_busy = entered.wait(timeout=30)
    finally:
        release()
    driver.join(timeout=60)

    assert reached_while_busy and seen["busy_held"] is True
    assert not driver.is_alive() and done.is_set()
    assert seen["answered"] is True
    assert seen["joined"] == "not ready"
    assert seen["walk_still_inside"] is True
