"""A warm slot read settles each OLDER unit with two stats, and still sees every change.

``_unit_marks`` keeps each older unit's mark beside the stats of its directory and
segments, and reuses it while those stats hold. These tests let the units age past the
racy window first, so the cached path is the one under test, then change an older unit
in each way the full mark would notice and require the warm read to equal a cold fold.
The last test pins the cost: an empty warm read grows by at most two file system calls
per older unit.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
from typing import Any, Callable

import pytest
from crew_log_core_helpers import _split_into_segments
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.config.paths import data_home
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_slot_memo as psm
from kiro_crew.crew_log import projection_unit_mark_cache as mark_cache
from kiro_crew.crew_log import store_handle, store_retention, store_units
from kiro_crew.testing.links import make_dir_link
from kiro_crew.testing.wait import wait_until

SLOT = "chat-unitmarks"
FIRST = "acp-older"
SECOND = "acp-newest"
#: The racy window under test: short, so a test waits a fraction of a second for it.
RACY_NS = 50_000_000


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    _floor_monkeypatch.setattr(mark_cache, "_MARK_RACY_NS", RACY_NS)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


def _unit(unit_id: str) -> None:
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=SLOT)


def _ledger(unit_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": SLOT}
    payload.update(fields)
    crew_log_emit.on_ledger_recorded(unit_id, payload)
    assert_drained()


def _value(checkpoint: Any) -> str:
    return json.dumps(
        crew_log.projection_of(checkpoint).value, sort_keys=True, separators=(",", ":"), default=str
    )


def _warm(units: tuple[str, ...]) -> str:
    return _value(crew_log.fold_slot_warm("ledger", units, slot=SLOT))


def _cold(units: tuple[str, ...]) -> str:
    value = crew_log.fold_slot("ledger", units, slot=SLOT).value
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cached(unit_id: str) -> bool:
    return (str(data_home()), unit_id) in mark_cache._mark_cache


def _unit_dir(unit_id: str):
    return lg.crew_log_dir(lg.KIND_SESSION, unit_id)


def _read_until_cached(units: tuple[str, ...], unit_id: str, *, slot: str = SLOT) -> None:
    """Warm-read *units* until *unit_id*'s files are past the racy window and cached."""

    def cached() -> bool:
        crew_log.fold_slot_warm("ledger", units, slot=slot)
        return _cached(unit_id)

    wait_until(cached, interval=0.02, describe=lambda: f"{unit_id} was never cached")


def _warm_with_older_cached() -> tuple[str, ...]:
    """Two units, warm, aged past the racy window, and FIRST's mark cached."""
    _unit(FIRST)
    _unit(SECOND)
    for step in range(4):
        _ledger(FIRST, goal="original goal", event=f"a{step}", event_kind="progress")
    _ledger(SECOND, event="b", event_kind="progress")
    units = (FIRST, SECOND)
    assert _warm(units) == _cold(units)
    _read_until_cached(units, FIRST)
    assert _warm(units) == _cold(units)
    assert not _cached(SECOND), "the newest unit must always read its full mark"
    return units


def _grow(_units: tuple[str, ...]) -> None:
    _ledger(FIRST, event="late from the older unit", event_kind="progress")


def _rewrite_same_size(_units: tuple[str, ...]) -> None:
    # Same length, so only the timestamps move: the weakest change a stat can see.
    for path in lg.segment_paths(lg.KIND_SESSION, FIRST):
        text = path.read_text()
        if "original goal" in text:
            path.write_text(text.replace("original goal", "REWRITTEN GO"))
            return
    raise AssertionError("nothing to rewrite")


def _recreate_on_the_same_inode(_units: tuple[str, ...]) -> None:
    """Remove FIRST and create it again, with the new bytes on the OLD inode."""
    head = lg.crew_log_path(lg.KIND_SESSION, FIRST)
    keep = head.parent / "keep.tmp"
    os.link(head, keep)
    old_inode = os.stat(head).st_ino
    for path in lg.segment_paths(lg.KIND_SESSION, FIRST):
        path.unlink()
    crew_log_emit.reset_caches()
    _unit(FIRST)
    _ledger(FIRST, goal="after the recreate", event="fresh", event_kind="progress")
    fresh = head.read_bytes()
    with open(keep, "r+b") as handle:
        handle.truncate(0)
        handle.write(fresh)
    os.replace(keep, head)
    assert os.stat(head).st_ino == old_inode
    crew_log_emit.reset_caches()


def _roll_a_new_segment(_units: tuple[str, ...]) -> None:
    _ledger(FIRST, event="into the next segment", event_kind="progress")
    _split_into_segments(lg.KIND_SESSION, FIRST, 4)


def _trim_leading_segments(_units: tuple[str, ...]) -> None:
    lg.crew_log_path(lg.KIND_SESSION, FIRST).unlink()


def _delete_the_unit(_units: tuple[str, ...]) -> None:
    for path in lg.segment_paths(lg.KIND_SESSION, FIRST):
        path.unlink()


@pytest.mark.parametrize(
    "change",
    [
        _grow,
        _rewrite_same_size,
        _recreate_on_the_same_inode,
        _roll_a_new_segment,
        _trim_leading_segments,
        _delete_the_unit,
    ],
    ids=lambda change: change.__name__.strip("_"),
)
def test_a_cached_older_unit_that_changed_reads_as_a_cold_fold(change: Callable[..., None]):
    units = _warm_with_older_cached()
    change(units)
    assert _warm(units) == _cold(units)


def test_a_new_entry_in_a_cached_older_units_directory_reads_its_full_mark(monkeypatch):
    """A segment appearing with the old one untouched moves only the directory."""
    _unit(FIRST)
    _unit(SECOND)
    for step in range(4):
        _ledger(FIRST, event=f"a{step}", event_kind="progress")
    _ledger(SECOND, event="b", event_kind="progress")
    _split_into_segments(lg.KIND_SESSION, FIRST, 3)
    units = (FIRST, SECOND)
    assert _warm(units) == _cold(units)
    _read_until_cached(units, FIRST)
    before = [p.name for p in lg.segment_paths(lg.KIND_SESSION, FIRST)]

    # A stray file beside the segments: only the directory's stamp can see it.
    (_unit_dir(FIRST) / "neighbour.txt").write_text("x")
    calls: list[str] = []
    real = psm._unit_mark

    def counted(unit_id: str) -> Any:
        calls.append(unit_id)
        return real(unit_id)

    monkeypatch.setattr(psm, "_unit_mark", counted)
    _warm(units)
    assert FIRST in calls, "a new directory entry did not send the older unit to its full mark"
    assert [p.name for p in lg.segment_paths(lg.KIND_SESSION, FIRST)] == before


def test_a_cached_older_unit_swapped_for_a_link_is_never_read_through(tmp_path):
    units = _warm_with_older_cached()
    directory = _unit_dir(FIRST)
    outside = tmp_path / "outside"
    # Windows refuses to move a directory with a file held open, and the writers still
    # hold the unit's lease: drop them and collect, so their finalizers release it.
    crew_log_emit.reset_caches()
    gc.collect()
    shutil.move(str(directory), str(outside))
    for path in outside.iterdir():
        if path.name.startswith("log"):
            path.write_text(path.read_text().replace("original goal", "READ THROUGH"))
    make_dir_link(directory, outside)  # a junction on Windows, a symlink elsewhere

    marks = psm._unit_marks(units, str(data_home()))
    assert marks[0].origin is None
    try:
        value = _warm(units)
    except lg.CrewLogError:
        return
    assert "READ THROUGH" not in value


def _counting(monkeypatch) -> dict[str, int]:
    counts = {"calls": 0}

    def wrap(owner: Any, name: str) -> None:
        real = getattr(owner, name)

        def counted(*args: Any, **kwargs: Any) -> Any:
            counts["calls"] += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(owner, name, counted)

    wrap(os, "stat")
    wrap(os, "lstat")
    wrap(os.path, "realpath")
    for module in (store_handle, store_units, store_retention):
        wrap(module, "_read_header_line")
    return counts


def test_an_empty_warm_read_costs_at_most_two_calls_per_older_unit():
    sizes = (4, 16, 64)
    slots: dict[int, tuple[str, ...]] = {}
    for size in sizes:
        units = tuple(f"acp-flat-{size}-{n:03d}" for n in range(size))
        for unit_id in units:
            CrewLog.create(
                lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=f"s{size}"
            )
            crew_log_emit.on_ledger_recorded(unit_id, {"slot": f"s{size}", "event": unit_id})
        assert_drained()
        slots[size] = units
    for size, units in slots.items():
        crew_log.fold_slot_warm("ledger", units, slot=f"s{size}")
    for size, units in slots.items():
        # Every older unit's mark cached: the units were written in order, so the last
        # older one is the youngest and the last to clear the racy window.
        _read_until_cached(units, units[-2], slot=f"s{size}")
        assert all(_cached(unit_id) for unit_id in units[:-1])

    cost: dict[int, int] = {}
    with pytest.MonkeyPatch.context() as patch:
        counts = _counting(patch)
        for size, units in slots.items():
            counts["calls"] = 0
            crew_log.fold_slot_warm("ledger", units, slot=f"s{size}")
            cost[size] = counts["calls"]

    for size in sizes[1:]:
        assert cost[size] - cost[sizes[0]] <= 2 * (size - sizes[0]), cost
