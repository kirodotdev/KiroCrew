"""A slot fold whose state encodes past 2 MiB still keeps a savepoint, so a restart
after an agent switch reads nothing from the unit the slot ran under before.

The kernel store refuses any file over ``MAX_PAYLOAD_BYTES`` on write and read. A busy
board's ``work`` state is bounded but about 2.4 MB of JSON, so it had no loadable
savepoint beside its newest unit, and every restart resumed from an older, staler file
beside the old unit and re-read that unit's tail. ``slot_state_codec`` stores such a
state compressed, under the same cap.

Real store, real read path; cost is counted per unit with a wrapper around
``CrewLog.iter_from``.
"""

from __future__ import annotations

import hashlib
import itertools
import json
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew import session_ledger
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import slot_state_codec
from kiro_crew.crew_log.projection_readers import _slot_units_for_fold
from kiro_crew.projection import MAX_PAYLOAD_BYTES

SLOT = "member-big-board"
#: Items with a large acceptance map: 40 x about 60 KB of hex is about 2.4 MB of state
#: JSON, past the 2 MiB cap, while each entry stays under the 64 KB entry ceiling.
BIG_ITEMS = 40
ACCEPTANCE_CHARS = 60_000


@pytest.fixture(autouse=True)
def _home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    # Each header stamp one second after the last, so two units never tie on createdAt.
    from kiro_crew.crew_log import store as crew_log_store

    ticks = itertools.count(1_700_000_000_000, 1_000)
    _floor_monkeypatch.setattr(
        crew_log_store, "now_ms", lambda: next(ticks)  # flake-ok: a fixed clock pin
    )
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    for handle in _handles.values():
        handle.release_ownership()
    _handles.clear()
    crew_log.forget_slot_folds()
    crew_log_emit.reset_caches()


_handles: "dict[str, CrewLog]" = {}


def _open(unit: str) -> CrewLog:
    _handles[unit] = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=SLOT)
    return _handles[unit]


def _work(unit: str, item_id: str, **fields: Any) -> None:
    data = {"slot": SLOT, "actor": "conductor", "by": SLOT, "action": "create"}
    data.update(item_id=item_id, title=f"item {item_id}", **fields)
    _handles[unit].append("work/recorded", data, src="gateway")
    session_ledger.note_work_unit_recorded(SLOT, unit)


def _hex(seed: str, chars: int) -> str:
    """Deterministic text that zlib cannot shrink to nothing (hex compresses about 2x)."""
    out = []
    block = seed.encode()
    while sum(len(x) for x in out) < chars:
        block = hashlib.sha256(block).digest()
        out.append(block.hex())
    return "".join(out)[:chars]


def _big_board(unit: str) -> None:
    for n in range(BIG_ITEMS):
        _work(unit, f"big-{n}", acceptance={"spec": _hex(f"{unit}-{n}", ACCEPTANCE_CHARS)})


def _read() -> Any:
    units = _slot_units_for_fold(SLOT, "work")
    return units, crew_log.fold_slot_warm("work", units, slot=SLOT).state


def _payload(unit: str) -> "dict[str, Any] | None":
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, unit, "work")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _restart_reads() -> "tuple[dict[str, int], Any]":
    """Entries read per unit by one work read after a restart, and the value."""
    crew_log.forget_slot_folds()
    read: dict[str, int] = {}
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read[self.id] = read.get(self.id, 0) + 1
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        _units, state = _read()
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return read, state


def _switch_agent(old: str, new: str, filler: int) -> None:
    """A big board in *old* plus *filler* entries of its own traffic, then the slot moves to
    *new* (an agent switch: a new unit opens and records)."""
    _open(old)
    _big_board(old)
    for n in range(filler):
        _handles[old].append(
            "turn/started", {"turn": n + 1, "actor": "user", "depth": 0}, src="gateway"
        )
    _read()
    _open(new)
    _work(new, "after-switch")
    _read()


@pytest.mark.parametrize("filler", [300, 1_200, 4_800, 19_200])
def test_a_restart_after_an_agent_switch_reads_nothing_from_the_old_unit(
    filler, _floor_monkeypatch
):
    """Red on main: the state is over the cap, so no savepoint beside the new unit loads,
    and the restart walks the old unit. Here the file is compressed, loads, and the old
    unit is read zero times at every size."""
    _floor_monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)
    old, new = f"old-{filler}", f"new-{filler}"
    _switch_agent(old, new, filler)

    payload = _payload(new)
    assert payload is not None, "a savepoint was written beside the new newest unit"
    assert list(payload["state"]) == [slot_state_codec.STATE_ZLIB], "stored compressed"
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, new, "work")
    assert path.stat().st_size <= MAX_PAYLOAD_BYTES, "under the one cap, read and write"
    state_json = json.dumps(crew_log.fold_slot_checkpoint("work", (old, new), slot=SLOT).state)
    assert len(state_json.encode()) > MAX_PAYLOAD_BYTES, "the fixture's state is over the cap"

    read, state = _restart_reads()

    assert read.get(old, 0) == 0, read
    assert read.get(new, 0) <= 2, read
    units = _slot_units_for_fold(SLOT, "work")
    assert units[-1] == new
    assert state == crew_log.fold_slot_checkpoint("work", units, slot=SLOT).state
