"""A savepoint that still describes its log is resumed in O(1) work, after a restart.

A resume must not re-read the consumed prefix (a digest to admit the savepoint and
another to re-verify it) or decode every record below the savepoint to reach the tail.
These tests bound the work by COUNTING it, not timing it:

* ``digests`` -- calls to :meth:`CrewLog.raw_prefix_digest` and
  :meth:`CrewLog.raw_records_through`, each a walk of the prefix;
* ``decoded`` -- records the store decoded (``CrewLog._iter_segments_located``), which
  includes the ones a walk skips to reach its start.

And they keep the other half: a prefix that was rewritten, truncated or replaced is still
refused, so the resumed answer is the cold one. A "restart" is a read with no in-process
bundle, which is all a new process has.
"""

from __future__ import annotations

import gc
import json
import os
import shutil
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import projection_session, store

SESSION = "s-o1"
OTHER = "s-o1-earlier"
SLOT = "chat-o1"
GATEWAY = "gateway"
_TURNS = (savepoints.MIN_ADVANCE_ENTRIES // 4) + 4


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    crew_log_emit.reset_caches()
    crew_log_eager.stop_for_tests()
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    yield
    crew_log.forget_slot_folds()
    crew_log.forget_session_folds()
    crew_log_eager.stop_for_tests()


def _turn_items(turn: int, credits: float = 0.5) -> list[dict[str, Any]]:
    return [
        {"type": "turn/started", "data": {"turn": turn, "actor": "user", "depth": 0}},
        {"type": "step/started", "data": {"turn": turn, "step": 1}},
        {"type": "step/completed", "data": {"turn": turn, "step": 1, "ms": 120}},
        {
            "type": "turn/completed",
            "data": {
                "turn": turn,
                "stop_reason": "end_turn",
                "depth": 0,
                "duration_ms": 900,
                "model": "opus",
                "provider": "kiro",
                "credits": credits,
                "tokens": {"input": 100, "output": 20, "cache_read": 5, "cache_write": 1},
            },
        },
    ]


def _log(unit_id: str = SESSION, *, slot: str = "dashboard:1") -> CrewLog:
    handle = CrewLog.create(lg.KIND_SESSION, unit_id, owner="raymond", agent="kirocrew", slot=slot)
    handle.append(
        "session/opened",
        {
            "agent": "kirocrew",
            "slot": slot,
            "model": "opus",
            "cwd": "/w",
            "owner": "raymond",
            "resumed": False,
        },
        src=GATEWAY,
    )
    return handle


def _grow(handle: CrewLog, turns: int, *, first: int = 1) -> int:
    """*turns* whole turns appended; the number of entries added."""
    items: list[dict[str, Any]] = []
    for turn in range(first, first + turns):
        items.extend(_turn_items(turn))
    for start in range(0, len(items), 256):
        handle.append_many(items[start : start + 256], src=GATEWAY)
    return len(items)


def _drop_cut_counter(unit_id: str = SESSION) -> None:
    """Make *unit_id* look like a log created before the cut counter existed."""
    (store.crew_log_dir(lg.KIND_SESSION, unit_id) / ".cuts").unlink()


def _segment(unit_id: str = SESSION):
    return store.segment_paths(lg.KIND_SESSION, unit_id)[0]


def _cold(names: tuple[str, ...] = ("usage",), unit_id: str = SESSION) -> dict[str, Any]:
    """Every *names* fold over the whole log, persisting nothing."""
    reader = CrewLog.open(lg.KIND_SESSION, unit_id)
    out = {}
    for name in names:
        checkpoint = crew_log.advance(
            crew_log.initial(name), reader.iter_from(1, known=crew_log.KNOWN_TYPES)
        )
        out[name] = crew_log.projection_of(checkpoint).to_dict()
    return out


def _rendered(bundle: crew_log.SessionProjections, names=("usage",)) -> dict[str, Any]:
    return {name: bundle.projection(name).to_dict() for name in names}


@pytest.fixture
def work(monkeypatch) -> dict[str, int]:
    """Counts of prefix walks and decoded records, reset per test."""
    counts = {"digests": 0, "decoded": 0}
    real_digest = CrewLog.raw_prefix_digest
    real_through = CrewLog.raw_records_through
    real_walk = CrewLog._iter_segments_located

    def digest(self, records):
        counts["digests"] += 1
        return real_digest(self, records)

    def through(self, seq):
        counts["digests"] += 1
        return real_through(self, seq)

    def walk(self, *args, **kwargs):
        for item in real_walk(self, *args, **kwargs):
            counts["decoded"] += 1
            yield item

    monkeypatch.setattr(CrewLog, "raw_prefix_digest", digest)
    monkeypatch.setattr(CrewLog, "raw_records_through", through)
    monkeypatch.setattr(CrewLog, "_iter_segments_located", walk)
    return counts


def _reset(counts: dict[str, int]) -> None:
    for key in counts:
        counts[key] = 0


# --------------------------------------------------------------------------- #
# Session folds on a unit with a cut counter
# --------------------------------------------------------------------------- #


def test_a_session_restart_on_an_unchanged_log_reads_nothing_below_the_savepoint(work):
    _grow(_log(), _TURNS)
    first = crew_log.fold_session(SESSION)
    assert first.saved_seq == first.last_seq, "the cold fold wrote its savepoint"
    _reset(work)

    resumed = crew_log.fold_session(SESSION)

    assert work == {"digests": 0, "decoded": 0}
    assert resumed.last_seq == first.last_seq
    assert _rendered(resumed) == _cold()


def test_a_session_restart_after_appends_decodes_only_the_new_entries(work):
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session(SESSION)
    added = _grow(handle, 3, first=_TURNS + 1)
    _reset(work)

    resumed = crew_log.fold_session(SESSION)

    assert work["digests"] == 0
    # The record AT the stored position is read to confirm it, then the new ones.
    assert work["decoded"] <= added + 1
    assert _rendered(resumed) == _cold()


def test_the_work_of_a_resume_does_not_grow_with_the_log(work):
    """The same appended tail costs the same on a log four times longer."""
    costs = []
    for unit_id, turns in (("s-short", _TURNS), ("s-long", 4 * _TURNS)):
        handle = _log(unit_id)
        _grow(handle, turns)
        crew_log.fold_session(unit_id)
        _grow(handle, 3, first=turns + 1)
        _reset(work)
        crew_log.fold_session(unit_id)
        costs.append(dict(work))
    assert costs[0] == costs[1]


def test_a_warm_advance_after_a_quiet_restart_read_decodes_only_the_new_entries(work):
    """Restart, a read with nothing new, then an append: the warm path still seeks."""
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session_warm(SESSION)
    crew_log.forget_session_folds()
    quiet = crew_log.fold_session_warm(SESSION)  # resumed from disk, nothing to read
    assert quiet.bundle.resume is not None and quiet.bundle.extent is not None
    added = _grow(handle, 2, first=_TURNS + 1)
    _reset(work)

    crew_log.fold_session_warm(SESSION)

    assert work["digests"] == 0
    assert work["decoded"] <= added + 1


def test_an_old_savepoint_without_a_position_is_resumed_then_upgraded(work):
    """New build, old file: the walk it always paid, once, then O(1) after."""
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session(SESSION)
    path = savepoints.checkpoint_path(lg.KIND_SESSION, SESSION, "usage")
    raw = json.loads(path.read_text())
    raw["witness"].pop("resume")
    raw["witness"].pop("prefix_extent", None)
    path.write_text(json.dumps(raw))
    added = _grow(handle, 2, first=_TURNS + 1)

    resumed = crew_log.fold_session(SESSION, ("usage",))
    assert _rendered(resumed) == _cold()
    assert "resume" in json.loads(path.read_text())["witness"], "the pass gave it a position"

    _grow(handle, 2, first=_TURNS + 3)
    _reset(work)
    again = crew_log.fold_session(SESSION, ("usage",))
    assert work["digests"] == 0 and work["decoded"] <= added + 1
    assert _rendered(again) == _cold()


def test_the_new_witness_keys_are_additive_for_an_older_reader():
    """Old build, new file: the evidence keys an older build reads are all still there."""
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session(SESSION)
    raw = json.loads(savepoints.checkpoint_path(lg.KIND_SESSION, SESSION, "usage").read_text())
    witness = raw["witness"]
    assert witness["seq"] == raw["watermark"]
    assert witness["prefix_cuts"] == handle.cuts()
    # What a build without positions or extents does with this file: compare the count.
    stripped = {key: witness[key] for key in ("seq", "prefix_cuts")}
    first_seq = savepoints._identity(handle)[1]
    assert savepoints.prefix_admit(handle, first_seq)({}, stripped)


def test_a_session_savepoint_is_refused_when_its_segment_is_replaced():
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session(SESSION, ("usage",))
    del handle
    gc.collect()
    path = _segment()
    data = path.read_bytes().replace(b'"credits":0.5', b'"credits":0.7')
    replacement = path.with_suffix(".tmp")
    replacement.write_bytes(data)
    os.replace(replacement, path)  # new inode, same size, no cut

    assert _rendered(crew_log.fold_session(SESSION, ("usage",))) == _cold()


# --------------------------------------------------------------------------- #
# A legacy unit: no cut counter
# --------------------------------------------------------------------------- #


def _legacy() -> CrewLog:
    handle = _log()
    _drop_cut_counter()
    _grow(handle, _TURNS)
    first = crew_log.fold_session(SESSION, ("usage",))
    assert first.saved_seq == first.last_seq
    return handle


def test_an_unchanged_legacy_unit_resumes_without_a_prefix_walk(work):
    _legacy()
    _reset(work)
    resumed = crew_log.fold_session(SESSION, ("usage",))
    assert work == {"digests": 0, "decoded": 0}
    assert _rendered(resumed) == _cold()


def test_a_legacy_unit_rewritten_in_place_is_detected(work):
    handle = _legacy()
    del handle
    gc.collect()
    path = _segment()
    before = path.stat()
    data = path.read_bytes()
    index = data.rfind(b'"credits":0.5')
    path.write_bytes(data[:index] + b'"credits":0.7' + data[index + len(b'"credits":0.5') :])
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    _reset(work)

    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert work["digests"] >= 1, "same size, new mtime: the extent does not hold"
    assert _rendered(resumed) == _cold()


def _rewrite_first_credit(path) -> None:
    """Change the FIRST turn's credits in place, same length, far below any tail window."""
    before = path.stat()
    data = path.read_bytes()
    index = data.find(b'"credits":0.5')
    path.write_bytes(data[:index] + b'"credits":0.7' + data[index + len(b'"credits":0.5') :])
    # A distinct mtime, set explicitly: a same-size write inside one coarse timestamp tick
    # would otherwise leave the file's fingerprint unchanged.
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def test_a_legacy_unit_edited_deep_then_appended_is_detected(work):
    """An edit far before the tail, then an append: the full digest still decides."""
    handle = _legacy()
    _rewrite_first_credit(_segment())
    _grow(handle, 2, first=_TURNS + 1)
    _reset(work)

    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert work["digests"] >= 1, "a grown legacy unit is not admitted on the extent"
    assert _rendered(resumed) == _cold()


def test_a_legacy_unit_that_only_grew_is_rehashed_once_then_o1_again(work):
    handle = _legacy()
    _grow(handle, 2, first=_TURNS + 1)
    crew_log.fold_session(SESSION, ("usage",))  # one full digest, then the extent is re-taken
    _reset(work)

    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert work["digests"] == 0
    assert _rendered(resumed) == _cold()


def test_a_slot_savepoint_takes_no_extent_when_the_files_moved_after_sampling(monkeypatch):
    """The extent must describe the bytes the witness vouched for, or none is stored."""
    earlier = _log(OTHER, slot=SLOT)
    _drop_cut_counter(OTHER)
    _grow(earlier, _TURNS)
    newest = _log(SESSION, slot=SLOT)
    _drop_cut_counter(SESSION)
    _grow(newest, _TURNS)
    real = savepoints.with_extent

    def edit_then_extent(handle, witness):
        _rewrite_first_credit(_segment())
        return real(handle, witness)

    monkeypatch.setattr(savepoints, "with_extent", edit_then_extent)
    _slot_read((OTHER, SESSION))
    monkeypatch.setattr(savepoints, "with_extent", real)
    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, SESSION, "status")
    assert path.is_file(), "the slot savepoint was written"
    assert "prefix_extent" not in json.loads(path.read_text())["witness"]
    crew_log.forget_slot_folds()
    assert _slot_read((OTHER, SESSION)) == _slot_cold((OTHER, SESSION))


def test_a_truncated_legacy_unit_is_detected():
    handle = _legacy()
    del handle
    gc.collect()
    path = _segment()
    lines = path.read_bytes().splitlines(keepends=True)
    path.write_bytes(b"".join(lines[:-5]))
    assert _rendered(crew_log.fold_session(SESSION, ("usage",))) == _cold()


def test_a_replaced_legacy_unit_is_detected(work):
    handle = _legacy()
    del handle
    gc.collect()
    path = _segment()
    copy = path.with_suffix(".tmp")
    shutil.copyfile(path, copy)
    data = copy.read_bytes()
    index = data.rfind(b'"credits":0.5')
    copy.write_bytes(data[:index] + b'"credits":0.7' + data[index + len(b'"credits":0.5') :])
    os.replace(copy, path)
    _reset(work)

    resumed = crew_log.fold_session(SESSION, ("usage",))

    assert work["digests"] >= 1, "a new inode: the extent does not hold"
    assert _rendered(resumed) == _cold()


# --------------------------------------------------------------------------- #
# The eager warm path: a savepoint write from the state already held
# --------------------------------------------------------------------------- #


def test_eager_advances_that_earn_writes_cost_only_the_new_entries(work, monkeypatch):
    origins: list[str] = []
    monkeypatch.setattr(projection_session, "_note_session_fold_origin", origins.append)
    handle = _log()
    _grow(handle, _TURNS)
    crew_log.fold_session_warm(SESSION)
    assert origins == ["cold"]
    saved = []
    for advance in range(3):
        added = _grow(handle, _TURNS, first=(advance + 1) * _TURNS + 1)
        origins.clear()
        _reset(work)
        warm = crew_log.fold_session_warm(SESSION)
        assert origins == ["warm"], "no fold from disk or from seq 1"
        assert work["digests"] == 0
        assert work["decoded"] <= added + 1
        assert warm.bundle.saved_seq == warm.bundle.last_seq, "the advance wrote its savepoint"
        saved.append(warm.bundle.saved_seq)
    assert saved == sorted(saved) and len(set(saved)) == 3
    crew_log.forget_session_folds()
    names = tuple(warm.bundle.checkpoints)
    assert _rendered(crew_log.fold_session(SESSION, names), ("usage",)) == _cold()


# --------------------------------------------------------------------------- #
# Slot folds
# --------------------------------------------------------------------------- #


def _slot_read(units: tuple[str, ...]) -> dict[str, Any]:
    return crew_log.projection_of(crew_log.fold_slot_warm("status", units, slot=SLOT)).to_dict()


def _slot_cold(units: tuple[str, ...]) -> dict[str, Any]:
    return crew_log.fold_slot("status", units, slot=SLOT).to_dict()


def test_a_slot_restart_decodes_only_the_new_entries_of_the_newest_unit(work):
    earlier = _log(OTHER, slot=SLOT)
    _grow(earlier, _TURNS)
    newest = _log(SESSION, slot=SLOT)
    _grow(newest, _TURNS)
    units = (OTHER, SESSION)
    _slot_read(units)
    crew_log.forget_slot_folds()
    _slot_read(units)  # the first resume stores the position it reached
    added = _grow(newest, 3, first=_TURNS + 1)
    crew_log.forget_slot_folds()
    _reset(work)

    value = _slot_read(units)

    assert work["digests"] == 0
    assert work["decoded"] <= added + 1
    assert value == _slot_cold(units)
