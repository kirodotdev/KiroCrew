"""A slot fold over a unit that keeps growing still leaves a savepoint where it stopped.

A live conductor appends about once a second, and a cold fold of a large slot takes
minutes in a busy gateway, so the newest unit almost always grows during the pass. A
pass that skips its write when that happens leaves the biggest cells with no savepoint,
and every restart or eviction refolds them whole. These tests append to the newest unit
through the whole fold, on the real store and the real read path, and count the entries
the read after a restart parses.

The largest size has the busy host's shape (8 units, 121 MB, the largest 94 MB), built
from synthetic entries.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_slot_memo as memo_mod
from kiro_crew.crew_log import store as crew_log_store

#: A busy host's largest slot, bytes per unit in order.
HOST_UNIT_BYTES = (4036820, 17328681, 647773, 2952, 48878, 4622377, 94441330, 305797)
#: About what one synthetic entry below costs on disk.
ENTRY_BYTES = 646
PAD = "x" * 480
#: One append to the newest unit per this many entries the fold reads.
APPEND_EVERY = 500


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


def _build(slot: str, counts: "tuple[int, ...]") -> "tuple[str, ...]":
    """One unit per count, filled in batches; the newest unit's handle is kept open."""
    units = tuple(f"{slot}-u{index}" for index in range(len(counts)))
    for unit, count in zip(units, counts):
        handle = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead", slot=slot)
        for start in range(0, count, 500):
            handle.append_many(_entries(slot, start, min(500, count - start)), src="gateway")
        _held[unit] = handle
    for unit in units[:-1]:
        _held.pop(unit).release_ownership()
    return units


def _append(slot: str, unit: str, count: int = 1) -> None:
    _held[unit].append_many(_entries(slot, 10**9, count), src="gateway")


def _read(slot: str, units: "tuple[str, ...]", *, grow: "str | None" = None, stop_after: int = 0):
    """``(checkpoint, entries parsed)`` for one ledger read.

    *grow* names a unit that gets one append per :data:`APPEND_EVERY` entries parsed,
    from the first entry to the last, so it grows through the whole pass. *stop_after*
    raises after that many entries, which is a process dying mid-fold.
    """
    parsed = [0]
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            parsed[0] += 1
            if grow is not None and parsed[0] % APPEND_EVERY == 1:
                _append(slot, grow)
            yield entry
            if stop_after and parsed[0] >= stop_after:
                raise RuntimeError("the process died mid-fold")

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        return crew_log.fold_slot_warm("ledger", units, slot=slot), parsed[0]
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]


def _value(checkpoint) -> str:
    return json.dumps(crew_log.projection_of(checkpoint).value, sort_keys=True, default=str)


def _cold(slot: str, units: "tuple[str, ...]") -> str:
    return json.dumps(
        crew_log.fold_slot("ledger", units, slot=slot).value, sort_keys=True, default=str
    )


def _savepoint(unit: str) -> "dict[str, Any] | None":
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, unit, "ledger")
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _shape(divisor: int) -> "tuple[int, ...]":
    return tuple(max(1, size // ENTRY_BYTES // divisor) for size in HOST_UNIT_BYTES)


def test_a_fold_over_a_growing_unit_leaves_a_savepoint_and_a_flat_restart():
    """At 1/64, 1/16, 1/4 and the full host shape, the restart reads only the delta."""
    restarts = []
    for divisor in (64, 16, 4, 1):
        slot = f"chat-growth-{divisor}"
        units = _build(slot, _shape(divisor))
        newest = units[-1]
        before = _held[newest].last_seq

        checkpoint, cold_parsed = _read(slot, units, grow=newest)

        assert cold_parsed >= sum(_shape(divisor))
        assert checkpoint.last_seq > before, "the newest unit grew during the pass"
        saved = _savepoint(newest)
        assert saved is not None, "a pass over a growing unit still writes its savepoint"
        assert saved["witness"]["seq"] == checkpoint.last_seq, "written where the pass stopped"

        _append(slot, newest, 5)
        crew_log.forget_slot_folds()  # a restart, or an eviction of the cell
        resumed, parsed = _read(slot, units)
        # The delta: the five above, plus any append that landed after the pass's last read.
        assert parsed == _held[newest].last_seq - saved["witness"]["seq"]
        restarts.append(parsed)
        assert _value(resumed) == _cold(slot, units)
    assert max(restarts) <= 5 + 1, f"records read after a restart stay flat: {restarts}"


def test_a_cut_during_a_grown_pass_writes_no_savepoint():
    """The cut count sampled before the pass must still read the same at the write."""
    units = _build("chat-cut", (300, 300))
    real = CrewLog.iter_from
    cut = []

    def cut_mid_pass(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            if not cut and self.id == units[-1]:
                cut.append(1)
                _append("chat-cut", units[-1])
                directory = lg.crew_log_path(lg.KIND_SESSION, units[-1]).parent
                crew_log_store._open_cut(directory)
                crew_log_store._close_cut(directory)
            yield entry

    CrewLog.iter_from = cut_mid_pass  # type: ignore[method-assign]
    try:
        crew_log.fold_slot_warm("ledger", units, slot="chat-cut")
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    assert cut
    assert _savepoint(units[-1]) is None


def test_a_grown_pass_on_a_unit_with_no_cut_counter_writes_no_savepoint():
    """shortcut kept: a digest covers fewer records than a grown pass folded."""
    units = _build("chat-digest", (300,))
    (lg.crew_log_path(lg.KIND_SESSION, units[0]).parent / crew_log_store._CUTS_FILE).unlink()
    _read("chat-digest", units, grow=units[0])
    assert _savepoint(units[0]) is None


@pytest.mark.parametrize("counts", [(1000,), (1000, 300)], ids=["newest", "older"])
def test_a_fold_that_dies_part_way_resumes_from_its_last_savepoint(monkeypatch, counts):
    """The savepoints written during a cold fold: beside the newest unit, or an older one."""
    monkeypatch.setattr(memo_mod, "SAVE_DURING_FOLD_ENTRIES", 100)
    units = _build("chat-crash", counts)
    with pytest.raises(RuntimeError):
        _read("chat-crash", units, stop_after=750)
    saved = _savepoint(units[0])
    assert saved is not None and saved["witness"]["seq"] == 700

    crew_log.forget_slot_folds()
    resumed, parsed = _read("chat-crash", units)

    assert parsed == sum(counts) - 700, "only the entries above the mid-fold savepoint"
    assert _value(resumed) == _cold("chat-crash", units)


def test_a_cell_too_big_to_keep_warm_writes_nothing_mid_fold(monkeypatch):
    monkeypatch.setattr(memo_mod, "SAVE_DURING_FOLD_ENTRIES", 100)
    monkeypatch.setattr(crew_log, "slot_fold_cache_bytes", lambda: 1)
    units = _build("chat-big", (500,))
    with pytest.raises(RuntimeError):
        _read("chat-big", units, stop_after=450)
    assert _savepoint(units[0]) is None


def test_a_refused_mid_fold_write_is_not_retried_in_the_same_pass(monkeypatch):
    monkeypatch.setattr(memo_mod, "SAVE_DURING_FOLD_ENTRIES", 100)
    calls = []

    def refuse(*_args: Any, **_kwargs: Any) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(savepoints, "save_slot", refuse)
    units = _build("chat-refused", (500,))
    with pytest.raises(RuntimeError):
        _read("chat-refused", units, stop_after=450)
    assert calls == [1]


def test_a_kept_cell_holds_no_second_state_after_its_cold_fold():
    """The mid-fold state reference ends with the pass, so the cache charge is the truth."""
    units = _build("chat-kept", (300,))
    _read("chat-kept", units)
    memo = crew_log._slot_memos[(str(crew_log.data_home()), "chat-kept", "ledger")]
    assert memo.registry._defns["ledger"].latest is None
