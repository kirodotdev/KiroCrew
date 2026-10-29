"""A slot read in a new process resumes from its savepoint and reads the gap, not the log.

The restart case of the full-scan guard: a slot of six 400-entry units folds cold once,
earns and moves its savepoint forward, then every in-memory cell is dropped (what a new
process has) and three entries are appended. The next read must hash nothing and decode
at most a constant number of entries beyond those three -- independent of the slot's
2400 entries.

Work is counted, not timed: ``entries`` through the one framer every ``iter_from`` walk
goes through (``store._iter_entries_located``), ``hashed`` through
``CrewLog.raw_prefix_digest``.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, bus, checkpoint, eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store

GATEWAY = "gateway"
BOARD = "dashboard:guard-board"
#: Entries in each unit of the measured slot; a walk of one unit alone is this many.
ENTRIES_PER_UNIT = 400
#: Units the measured slot ran under.
UNITS_IN_SLOT = 6
#: Records a resume may re-read to land on its boundary, plus slack. Far below one unit.
ENTRY_SLACK = 40
#: Appended past the savepoint write interval, so the warm read moves the savepoint up.
SAVEPOINT_GAP = 300
#: A slot fold whose units come from the header index alone, so the case measures the
#: fold and its savepoint and nothing else.
RESUME_FOLD = "ledger"
_FILLER_TYPE = "turn/started"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, no warm cell, subscriber or worker left over."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _reset()
    yield
    _reset()


def _reset() -> None:
    eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    bus.reset_for_tests()
    emit.reset_caches()


class _Meter:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entries = 0
        self.hashed = 0
        meter = self
        real_located = crew_log_store._iter_entries_located

        def located(*args: Any, **kwargs: Any) -> Iterator[Any]:
            for pair in real_located(*args, **kwargs):
                meter.entries += 1
                yield pair

        # Through the facade: it mirrors the patch onto every owner that binds the name.
        monkeypatch.setattr(crew_log_store, "_iter_entries_located", located)
        real_digest = CrewLog.raw_prefix_digest

        def digest(self: CrewLog, records: int) -> "tuple[str, int]":
            sha, hashed = real_digest(self, records)
            meter.hashed += int(hashed)
            return sha, hashed

        monkeypatch.setattr(CrewLog, "raw_prefix_digest", digest)

    def reset(self) -> None:
        self.entries = self.hashed = 0

    def __repr__(self) -> str:
        return f"_Meter(entries={self.entries}, hashed={self.hashed})"


def _work_create(board: str, item_id: str) -> dict:
    return {
        "slot": board,
        "actor": "conductor",
        "by": board,
        "action": "create",
        "item_id": item_id,
        "title": f"item {item_id}",
    }


def _unit(unit_id: str, *, filler: int, work_items: int) -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="guard", agent="kirocrew", slot=BOARD)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": BOARD,
            "model": "m",
            "cwd": "/w",
            "owner": "guard",
            "resumed": False,
            "class": {"memory": "persistent"},
        },
        src=GATEWAY,
    )
    items = [
        {"type": "work/recorded", "data": _work_create(BOARD, f"{unit_id}-it-{index}")}
        for index in range(work_items)
    ]
    items += [
        {"type": _FILLER_TYPE, "data": {"turn": index + 1, "actor": "user", "depth": 0}}
        for index in range(filler)
    ]
    for start in range(0, len(items), 200):
        handle.append_many(items[start : start + 200], src=GATEWAY)
    return handle


def _append_work(handle: CrewLog, count: int, tag: str) -> None:
    for index in range(count):
        handle.append("work/recorded", _work_create(BOARD, f"{tag}-{index}"), src=GATEWAY)


def test_a_slot_read_after_restart_resumes_from_the_savepoint(monkeypatch):
    units = [
        _unit(f"acp-guard-{index:02d}", filler=ENTRIES_PER_UNIT - 4, work_items=3)
        for index in range(UNITS_IN_SLOT)
    ]
    assert SAVEPOINT_GAP >= checkpoint.MIN_ADVANCE_ENTRIES
    crew_log.read_slot_projection(BOARD, RESUME_FOLD)  # cold fold, writes a savepoint
    _append_work(units[-1], SAVEPOINT_GAP, "earn")
    crew_log.read_slot_projection(BOARD, RESUME_FOLD)  # warm, moves the savepoint on
    # The restart: every in-memory cell is gone, only the files remain.
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    _append_work(units[-1], 3, "gap")
    meter = _Meter(monkeypatch)

    crew_log.read_slot_projection(BOARD, RESUME_FOLD)

    assert meter.hashed == 0, meter  # no re-hash of the folded prefix
    assert meter.entries <= ENTRY_SLACK + 3, meter
