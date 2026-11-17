"""A unit that stops being newest gets its slot savepoint sealed at its end.

A reload, an agent switch or a reset opens a newer unit in the same slot. A restart whose
newest savepoint is missing or refused falls back to the savepoint beside the old unit,
so a seal left below that unit's end costs the restart a read of its tail. These tests switch agents mid-slot on the real store and the real read path,
then restart with the newest savepoint gone, and count the records read per unit.

The largest size has the shape of a busy host's largest slot (8 units, 121 MB, the
largest 94 MB), built from synthetic entries.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log

#: A busy host's largest slot, bytes per unit in order.
HOST_UNIT_BYTES = (4036820, 17328681, 647773, 2952, 48878, 4622377, 94441330, 305797)
ENTRY_BYTES = 646
PAD = "x" * 480
#: Entries the old unit gains after its last earned savepoint, below the 256 threshold.
LATE = 200


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    for handle in _held.values():
        handle.release_ownership()
    _held.clear()
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


_held: "dict[str, CrewLog]" = {}


def _entries(slot: str, first: int, count: int) -> "list[dict[str, Any]]":
    return [
        {
            "type": "ledger/recorded",
            "data": {
                "slot": slot,
                "next": f"step {i}",
                "event": f"e{i} {PAD}",
                "event_kind": "progress",
            },
        }
        for i in range(first, first + count)
    ]


def _open(slot: str, unit: str, agent: str) -> None:
    _held[unit] = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent=agent, slot=slot)


def _append(slot: str, unit: str, count: int, first: int = 0) -> None:
    for start in range(0, count, 500):
        _held[unit].append_many(
            _entries(slot, first + start, min(500, count - start)), src="gateway"
        )


def _read(slot: str, units: "tuple[str, ...]") -> "tuple[str, Counter[str]]":
    """One ledger read through the warm route: its value and the records read per unit."""
    read: Counter[str] = Counter()
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read[self.id] += 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        checkpoint = crew_log.fold_slot_warm("ledger", units, slot=slot)
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    value = json.dumps(crew_log.projection_of(checkpoint).value, sort_keys=True, default=str)
    return value, read


def _cold(slot: str, units: "tuple[str, ...]") -> str:
    return json.dumps(
        crew_log.fold_slot("ledger", units, slot=slot).value, sort_keys=True, default=str
    )


def _savepoint(unit: str):
    return savepoints.slot_checkpoint_path(lg.KIND_SESSION, unit, "ledger")


def _switch_agents(slot: str, counts: "tuple[int, ...]") -> "tuple[str, ...]":
    """Fold a slot, let its newest unit grow below the write threshold, switch agents.

    Returns the unit list after the switch. The old newest unit is ``units[-2]``.
    """
    units = tuple(f"{slot}-u{index}" for index in range(len(counts)))
    for unit, count in zip(units, counts):
        _open(slot, unit, "lead")
        _append(slot, unit, count)
    _read(slot, units)
    _append(slot, units[-1], LATE, first=10**6)
    _read(slot, units)  # a warm pass that earns no write
    switched = units + (f"{slot}-switched",)
    _open(slot, switched[-1], "worker")
    _append(slot, switched[-1], 3, first=2 * 10**6)
    _read(slot, switched)  # the rollover
    return switched


def test_an_agent_switch_seals_the_old_unit_and_a_restart_reads_none_of_it():
    """At 1/64, 1/16, 1/4 and the full host shape: zero old-unit records on restart."""
    for divisor in (64, 16, 4, 1):
        slot = f"chat-switch-{divisor}"
        counts = tuple(max(1, size // ENTRY_BYTES // divisor) for size in HOST_UNIT_BYTES[:7])
        units = _switch_agents(slot, counts)
        old, newest = units[-2], units[-1]
        witness = json.loads(_savepoint(old).read_text(encoding="utf-8"))["witness"]
        assert witness["seq"] == _held[old].last_seq, "sealed at the old unit's end"

        # The newest savepoint missing or refused: the restart falls back to the old one.
        _savepoint(newest).unlink()
        crew_log.forget_slot_folds()
        value, read = _read(slot, units)

        assert read[old] == 0, f"restart re-read the old unit at 1/{divisor}"
        assert sum(read.values()) == read[newest] == _held[newest].last_seq
        assert value == _cold(slot, units)


def test_a_restart_with_the_newest_savepoint_reads_only_the_delta():
    units = _switch_agents("chat-switch-kept", (300, 400))
    _append("chat-switch-kept", units[-1], 4, first=3 * 10**6)
    crew_log.forget_slot_folds()
    value, read = _read("chat-switch-kept", units)
    assert read[units[-2]] == 0
    assert read[units[-1]] <= 4 + 3
    assert value == _cold("chat-switch-kept", units)


def test_an_old_unit_that_grows_after_its_seal_is_resumed_from_the_seal():
    """Supersede repair and background calls can still append to an older unit."""
    units = _switch_agents("chat-switch-late", (300, 400))
    _append("chat-switch-late", units[-2], 5, first=4 * 10**6)
    _savepoint(units[-1]).unlink()
    crew_log.forget_slot_folds()
    value, read = _read("chat-switch-late", units)
    assert read[units[-2]] == 5, "only what landed after the seal"
    assert value == _cold("chat-switch-late", units)


def test_a_seal_that_fails_does_not_fail_the_rollover(monkeypatch):
    real = savepoints.save_slot
    old = "chat-switch-broken-u1"

    def broken(handle: Any, *args: Any, **kwargs: Any) -> bool:
        if handle.id == old and "chat-switch-broken-switched" in _held:
            raise OSError("disk full")
        return real(handle, *args, **kwargs)

    monkeypatch.setattr(savepoints, "save_slot", broken)
    units = _switch_agents("chat-switch-broken", (300, 400))
    assert units[-2] == old
    assert _savepoint(units[-1]).is_file(), "the rollover still wrote the newest savepoint"
    crew_log.forget_slot_folds()
    assert _read("chat-switch-broken", units)[0] == _cold("chat-switch-broken", units)
