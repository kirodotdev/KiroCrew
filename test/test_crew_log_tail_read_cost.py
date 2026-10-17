"""What a warm slot read COSTS, and that cutting the cost did not change the answer.

A board on a timer polls its slot, finds one new entry, and folds it. The danger is
that such a read decodes every record of the log to reach the one it wants:
``iter_from`` yields from a seq, and a walk that starts at the top has to pass
everything below it. An idle board's poll would then cost the whole log, and cost more
every time anything was written.

Everything here goes through the public fold, with no reference to how the saving is
made. The first two tests measure the cost and pin that it does not track the log's
size. The rest pin the answer: on every shape where a continuation is refused -- a
repair that renumbered from a cut, a rolled-back append, a slot that gained a unit, a
log recreated under the same id, an entry rewritten with its cut counted -- the warm
fold must still equal the cold one, which reads every byte from seq 1.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store

SLOT = "chat-tailcost"
UNIT = "acp-tailcost-unit"
OTHER = "acp-tailcost-other"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Own data home, crew log on, and no warm fold carried between tests.

    Patches through ``_floor_monkeypatch`` rather than the shared ``monkeypatch``, so a
    test calling ``monkeypatch.undo()`` cannot lift the home pin out from under it.
    """
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _unit(unit_id: str, *, slot: str = SLOT) -> None:
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _log(unit_id: str) -> Path:
    return lg.crew_log_path(lg.KIND_SESSION, unit_id)


def _dir(unit_id: str) -> Path:
    return _log(unit_id).parent


def _ledger(unit_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": SLOT}
    payload.update(fields)
    crew_log_emit.on_ledger_recorded(unit_id, payload)
    crew_log_emit.flush(timeout=5.0)


def _entries(unit_id: str, count: int) -> None:
    for step in range(count):
        _ledger(unit_id, event=f"e{step}", event_kind="progress")


def _normalized(value: "dict[str, Any]") -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cold(units: "tuple[str, ...]") -> str:
    return _normalized(crew_log.fold_slot("ledger", units, slot=SLOT).value)


def _warm(units: "tuple[str, ...]") -> str:
    return _normalized(
        crew_log.projection_of(crew_log.fold_slot_warm("ledger", units, slot=SLOT)).value
    )


class _Decodes:
    """How many raw records one read JSON-decoded, and how many entries it yielded.

    The decode is where the cost lands: the framing layer reads bytes, and every record
    reaching ``_parses_to_object`` is one the read paid to understand. The yield count
    says what the reader actually got, so together they answer whether a read cost the
    tail or the log.

    A CONTEXT MANAGER with its OWN :class:`pytest.MonkeyPatch`. The tests below measure
    several reads in one run and must stop counting between them, and ``undo()`` on the
    shared ``monkeypatch`` fixture would also undo the autouse fixture's patches --
    including the data home every later read depends on.
    """

    def __init__(self) -> None:
        self.parsed = 0
        self.yielded = 0
        self._patch = pytest.MonkeyPatch()

    def __enter__(self) -> "_Decodes":
        real_parse = crew_log_store._parses_to_object
        real_iter_from = CrewLog.iter_from

        def _counted_parse(blob):
            self.parsed += 1
            return real_parse(blob)

        def _counted_iter_from(inner, seq=1, **kwargs):
            for entry in real_iter_from(inner, seq, **kwargs):
                self.yielded += 1
                yield entry

        self._patch.setattr(crew_log_store, "_parses_to_object", _counted_parse)
        self._patch.setattr(CrewLog, "iter_from", _counted_iter_from)
        return self

    def __exit__(self, *_exc) -> None:
        self._patch.undo()


# --------------------------------------------------------------------------- #
# the cost
# --------------------------------------------------------------------------- #


def test_a_warm_read_of_one_new_entry_does_not_decode_the_whole_log():
    """The load-bearing one. A poll that finds one entry must not cost the log.

    Two logs, one twenty times the other, each polled for a single new entry. What is
    pinned is the SLOPE: the big log's read may not decode materially more than the
    small one's. A read proportional to the log decodes about twenty times as much,
    which is what this refuses.
    """
    small, large = "acp-cost-small", "acp-cost-large"
    for unit_id, count in ((small, 50), (large, 1000)):
        _unit(unit_id, slot=f"{SLOT}-{unit_id}")
        for step in range(count):
            crew_log_emit.on_ledger_recorded(
                unit_id,
                {"slot": f"{SLOT}-{unit_id}", "event": f"e{step}", "event_kind": "progress"},
            )
        crew_log_emit.flush(timeout=20.0)
    # The slope is only a slope if the two logs really differ, so the gap is asserted
    # rather than assumed: an emitter that dropped the thousand would make a flat
    # measurement look like the fix working.
    assert CrewLog.open(lg.KIND_SESSION, large).last_seq > 15 * (
        CrewLog.open(lg.KIND_SESSION, small).last_seq
    ), "the two logs are not far enough apart to measure a slope"

    costs: "dict[str, tuple[int, int]]" = {}
    for unit_id in (small, large):
        slot = f"{SLOT}-{unit_id}"
        crew_log.fold_slot_warm("ledger", (unit_id,), slot=slot)  # the cold fold
        crew_log_emit.on_ledger_recorded(
            unit_id, {"slot": slot, "event": "the one new entry", "event_kind": "progress"}
        )
        crew_log_emit.flush(timeout=20.0)
        with _Decodes() as seen:
            crew_log.fold_slot_warm("ledger", (unit_id,), slot=slot)
        costs[unit_id] = (seen.parsed, seen.yielded)

    assert costs[small][1] == 1, f"the small read folded {costs[small][1]} entries, not 1"
    assert costs[large][1] == 1, f"the large read folded {costs[large][1]} entries, not 1"
    assert costs[large][0] <= costs[small][0] + 4, (
        "the warm read still decodes in proportion to the log: "
        f"{costs[small][0]} records at 50 entries, {costs[large][0]} at 1000"
    )


def test_the_warm_read_cost_does_not_move_as_the_log_grows():
    """The same slot polled again and again: each poll costs the same.

    The shape a board on a timer actually produces, and the slope above measured from
    the other direction. A read that walks the prefix climbs by one record per poll, so
    twelve polls spread the cost across twelve different values.
    """
    _unit(UNIT)
    _entries(UNIT, 40)
    crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)

    costs: "list[int]" = []
    for step in range(12):
        _ledger(UNIT, event=f"poll{step}", event_kind="progress")
        with _Decodes() as seen:
            crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)
        assert seen.yielded == 1, f"poll {step} folded {seen.yielded} entries, not 1"
        costs.append(seen.parsed)

    assert max(costs) - min(costs) <= 2, f"the per-poll cost moved across the run: {costs}"


# --------------------------------------------------------------------------- #
# the answer, on every shape a continuation is refused
# --------------------------------------------------------------------------- #


def test_poll_after_poll_of_a_growing_log_equals_one_cold_fold():
    """Twelve warm reads over a log that grew between each, against one cold fold.

    This is where a resumed read that dropped or double-counted an entry shows up: the
    cold fold reads every byte from seq 1 and is the answer the warm path must reach.
    """
    _unit(UNIT)
    _entries(UNIT, 15)
    assert _warm((UNIT,)) == _cold((UNIT,))

    for step in range(12):
        _ledger(UNIT, event=f"poll{step}", event_kind="progress")
        assert _warm((UNIT,)) == _cold((UNIT,)), f"warm and cold parted at poll {step}"

    texts = [event["text"] for event in json.loads(_warm((UNIT,)))["events"]]
    assert "poll11" in texts, texts[-4:]


def test_a_warm_read_across_a_real_orphan_group_repair_equals_a_cold_fold():
    """The repair cuts the file and renumbers from the cut -- the hazard, for real.

    A seq the fold already consumed comes back carrying other bytes, and the file is
    taller than it was, which every stat and every seq comparison reads as an append.
    The cut counter is what refuses it, and the pin is that the answer still equals the
    cold fold: a read that resumed here would serve state folded from records the
    repair has since rewritten.
    """
    _unit(UNIT)
    _entries(UNIT, 4)
    assert _warm((UNIT,)) == _cold((UNIT,))

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    handle.append(
        "tool/called",
        {"turn": 1, "name": "fs_write", "call_id": "tc-1", "server": "", "kind": ""},
        src="acp",
    )
    handle.append_many(
        [{"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True}],
        src="acp",
    )
    assert CrewLog.open(lg.KIND_SESSION, UNIT).repair_interrupted_turn() == 2
    _ledger(UNIT, event="after the repair", event_kind="progress")

    assert _warm((UNIT,)) == _cold((UNIT,))


def test_a_warm_read_after_a_rolled_back_append_equals_a_cold_fold(monkeypatch):
    """A failed append is cut back after its bytes reached the disk.

    The bytes are flushed before the fsync that fails, so a reader holding no lock can
    read them as committed in that window; the rollback then removes them and the retry
    writes something else under the same seq. The rollback counts its cut, so the
    continuation is refused and the fold starts over.
    """
    _unit(UNIT)
    _entries(UNIT, 4)
    assert _warm((UNIT,)) == _cold((UNIT,))

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    real_fsync = os.fsync
    failed = {"once": False}

    def _refusing_fsync(fd):
        if not failed["once"] and b"ROLLED-BACK" in _log(UNIT).read_bytes():
            failed["once"] = True
            raise OSError(5, "the test refuses this fsync")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _refusing_fsync)
    with pytest.raises(OSError):
        handle.append(
            "tool/called",
            {"turn": 1, "name": "fs_write", "call_id": "ROLLED-BACK", "server": "", "kind": ""},
            src="acp",
        )
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert failed["once"], "the fsync never failed, so nothing was rolled back"

    _ledger(UNIT, event="after the rollback", event_kind="progress")
    assert _warm((UNIT,)) == _cold((UNIT,))


def test_a_warm_read_equals_a_cold_fold_when_the_slot_gains_a_unit():
    """A changed unit list is not a continuation, so nothing remembered applies.

    Whatever position the earlier read reached names a place in the OLD newest unit's
    log, and the newest unit is now a different file.
    """
    _unit(UNIT)
    _entries(UNIT, 10)
    assert _warm((UNIT,)) == _cold((UNIT,))

    _unit(OTHER)
    _entries(OTHER, 3)
    units = (UNIT, OTHER)
    assert _warm(units) == _cold(units)

    _ledger(OTHER, event="into the new unit", event_kind="progress")
    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert "into the new unit" in [event["text"] for event in warm["events"]]


def test_a_warm_read_equals_a_cold_fold_when_the_unit_is_recreated():
    """A log removed and recreated under the same id has seqs that start again.

    Its identity changes, so the continuation is refused before any remembered position
    is looked at -- which matters, because such a position could still be a real record
    boundary in the new file and could still find its seq there.
    """
    _unit(UNIT)
    _entries(UNIT, 10)
    assert _warm((UNIT,)) == _cold((UNIT,))

    for path in crew_log_store.segment_paths(lg.KIND_SESSION, UNIT):
        path.unlink()
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink(missing_ok=True)
    crew_log_emit.reset_caches()
    _unit(UNIT)
    _entries(UNIT, 4)

    warm = json.loads(_warm((UNIT,)))
    assert warm == json.loads(_cold((UNIT,)))
    assert [event["text"] for event in warm["events"]] == ["e0", "e1", "e2", "e3"]


def test_a_warm_read_equals_a_cold_fold_when_an_entry_is_rewritten_with_its_cut_counted():
    """The store's recovery shape: cut, then write over the seqs the cut freed."""
    _unit(UNIT)
    _entries(UNIT, 8)
    assert _warm((UNIT,)) == _cold((UNIT,))

    # Bytes, not text: a round-trip through text mode translates line endings on
    # Windows, which rewrites every separator and moves the offsets this file measures.
    blob = _log(UNIT).read_bytes()
    assert b"e3" in blob
    _log(UNIT).write_bytes(blob.replace(b"e3", b"XX"))
    # A settled cut is two raises: the counter is a seqlock, odd while the bytes move.
    crew_log_store._open_cut(_dir(UNIT))
    crew_log_store._close_cut(_dir(UNIT))
    _ledger(UNIT, event="after the rewrite", event_kind="progress")

    warm = json.loads(_warm((UNIT,)))
    assert warm == json.loads(_cold((UNIT,)))
    assert "XX" in [event["text"] for event in warm["events"]]


def test_get_and_page_and_a_full_walk_still_read_the_whole_log():
    """The readers that share the segment walk, unchanged.

    They go through the same walk the tail read now resumes inside, so this is the pin
    that teaching it to start elsewhere did not drop a header, shift a record or lose
    an entry for the callers that always start at the top.
    """
    _unit(UNIT)
    _entries(UNIT, 12)
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    top = handle.last_seq

    assert handle.get(1) is not None
    assert handle.get(top) is not None
    assert handle.get(top + 1) is None
    assert [entry.seq for entry in handle.iter_from(1)] == list(range(1, top + 1))

    page = handle.page()
    seqs = [entry.seq for entry in page.entries]
    assert seqs == sorted(seqs, reverse=True)
    assert max(seqs) == top
