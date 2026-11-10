"""A session's savepoint lands where its fold stopped, and is sealed when it closes.

The owner's rule for these folds: a restart's cost must not grow with the unit. Two
shapes broke it. A pass over a unit that kept growing wrote no savepoint, so the next
restart folded the whole unit again; and a unit that stopped short of the 256-entry write
threshold left that gap for every later restart. Both run here on the real store and
the real fold path, at 1/64, 1/16, 1/4 and the full size of a busy host's largest unit
(94 MB, synthetic entries), counting the records the restart reads.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import checkpoint as savepoints
from kiro_crew.crew_log import eager, emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

GATEWAY = "gateway"
#: A busy host's largest unit, in synthetic entries of about 646 bytes.
HOST_UNIT_ENTRIES = 94441330 // 646
PAD = "x" * 480
APPEND_EVERY = 500


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    eager.stop_for_tests()
    crew_log.forget_session_folds()
    emit.reset_caches()
    yield
    for handle in _held.values():
        handle.release_ownership()
    _held.clear()
    eager.stop_for_tests()
    crew_log.forget_session_folds()
    emit.reset_caches()


_held: "dict[str, CrewLog]" = {}


def _entries(first: int, count: int) -> "list[dict[str, Any]]":
    data = {"slot": "s", "event_kind": "progress"}
    return [
        {"type": "ledger/recorded", "data": {**data, "next": f"s{i}", "event": f"e{i} {PAD}"}}
        for i in range(first, first + count)
    ]


def _unit(unit: str, count: int) -> None:
    _held[unit] = CrewLog.create(lg.KIND_SESSION, unit, owner="owner", agent="lead")
    _append(unit, count)


def _append(unit: str, count: int, first: int = 0) -> None:
    for start in range(0, count, 500):
        _held[unit].append_many(_entries(first + start, min(500, count - start)), src=GATEWAY)


def _fold(unit: str, *, grow: bool = False, during=None) -> int:
    """One ``fold_session`` pass with no warm bundle, as after a restart; records read.

    *grow* appends one entry per :data:`APPEND_EVERY` records read, from the first record
    to the last, so the unit grows through the whole pass. *during* runs once, on the
    first record.
    """
    read = [0]
    real = CrewLog.iter_from

    def counted(self, *args, **kwargs):
        for entry in real(self, *args, **kwargs):
            read[0] += 1
            if grow and read[0] % APPEND_EVERY == 1:
                _append(unit, 1, first=10**8 + read[0])
            if during is not None and read[0] == 1:
                during()
            yield entry

    CrewLog.iter_from = counted  # type: ignore[method-assign]
    try:
        crew_log.fold_session(unit)
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]
    return read[0]


def _witness_seq(unit: str) -> "int | None":
    directory = savepoints.checkpoint_dir(lg.KIND_SESSION, unit)
    seqs = [
        json.loads(p.read_text(encoding="utf-8"))["witness"]["seq"]
        for p in directory.glob("*.json")
    ]
    return min(seqs) if seqs else None


def _value(unit: str) -> str:
    bundle = crew_log.fold_session(unit)
    return json.dumps(
        {name: bundle.projection(name).value for name in bundle.checkpoints}, sort_keys=True
    )


def _cold_value(unit: str) -> str:
    savepoints.discard(_held[unit], crew_log.PROJECTION_NAMES)
    return _value(unit)


def _sizes() -> "list[int]":
    return [HOST_UNIT_ENTRIES // divisor for divisor in (64, 16, 4, 1)]


def test_a_pass_over_a_growing_unit_leaves_a_savepoint_and_a_flat_restart():
    for size in _sizes():
        unit = f"u-grown-{size}"
        _unit(unit, size)
        _fold(unit, grow=True)
        seq = _witness_seq(unit)
        assert seq is not None and seq >= size, "a pass over a growing unit still writes"
        assert _held[unit].last_seq > seq, "the unit grew past what the pass folded"

        _append(unit, 5, first=10**7)
        read = _fold(unit)
        # Only entries above the savepoint: the ones that landed during the first pass
        # after its walk ended, and the five since. Nothing the pass folded is read again.
        assert read == _held[unit].last_seq - seq
        assert _value(unit) == _cold_value(unit)


def _wake(unit: str, entry_type: str, seq: int) -> None:
    eager.note_commit(unit, entry_type, seq)
    assert eager.drain(timeout=FALLBACK_TIMEOUT_SECS)


def test_a_closed_unit_is_sealed_at_its_closer_and_a_restart_reads_none_of_it():
    for size in _sizes():
        unit = f"u-closed-{size}"
        _unit(unit, size)
        _wake(unit, "ledger/recorded", _held[unit].last_seq)
        _append(unit, 200, first=10**7)  # below the 256-entry threshold
        closed = _held[unit].append("session/closed", {"reason": "done"}, src=GATEWAY)
        _wake(unit, "session/closed", int(closed.seq))

        assert _witness_seq(unit) == int(closed.seq), "sealed at the closer"
        crew_log.forget_session_folds()
        assert _fold(unit) == 0, f"restart re-read the closed unit at {size}"
        assert _value(unit) == _cold_value(unit)


def test_a_cut_during_a_grown_pass_never_writes_the_pre_cut_count():
    """The pass that saw the cut is retried cold; only the post-cut count is written."""
    _unit("u-cut", 600)
    directory = lg.crew_log_path(lg.KIND_SESSION, "u-cut").parent
    before = _held["u-cut"].cuts()

    def cut() -> None:
        crew_log_store._open_cut(directory)
        crew_log_store._close_cut(directory)

    _fold("u-cut", grow=True, during=cut)
    files = list(savepoints.checkpoint_dir(lg.KIND_SESSION, "u-cut").glob("*.json"))
    counts = {json.loads(p.read_text(encoding="utf-8"))["witness"]["prefix_cuts"] for p in files}
    assert before not in counts
    assert counts <= {_held["u-cut"].cuts()}
    assert _value("u-cut") == _cold_value("u-cut")


def test_a_grown_pass_on_a_unit_with_no_cut_counter_still_writes_nothing():
    """shortcut kept: a digest covers fewer records than a grown pass folded."""
    _unit("u-digest", 600)
    (lg.crew_log_path(lg.KIND_SESSION, "u-digest").parent / crew_log_store._CUTS_FILE).unlink()
    _fold("u-digest", grow=True)
    assert _witness_seq("u-digest") is None


def test_a_seal_on_a_unit_with_no_cut_counter_writes_nothing():
    _unit("u-digest-closed", 300)
    (
        lg.crew_log_path(lg.KIND_SESSION, "u-digest-closed").parent / crew_log_store._CUTS_FILE
    ).unlink()
    _wake("u-digest-closed", "ledger/recorded", _held["u-digest-closed"].last_seq)
    before = _witness_seq("u-digest-closed")
    _append("u-digest-closed", 10, first=10**7)
    closed = _held["u-digest-closed"].append("session/closed", {"reason": "done"}, src=GATEWAY)
    _wake("u-digest-closed", "session/closed", int(closed.seq))
    assert _witness_seq("u-digest-closed") == before


def test_a_same_size_rewrite_during_a_pass_writes_no_savepoint():
    """A file that moved without growing keeps the pre-pass fingerprint, so the write refuses.

    The rewrite is an in-place edit of the first record the pass reads, same length, which
    the cut count does not see. The next read resumes from the last valid savepoint.
    """
    _unit("u-rewrite", 300)
    _fold("u-rewrite")
    valid = _witness_seq("u-rewrite")
    assert valid is not None
    _append("u-rewrite", 300, first=10**7)
    path = crew_log_store.segment_paths(lg.KIND_SESSION, "u-rewrite")[-1]
    old = f"e{10**7} {PAD}".encode()
    new = f"e{10**7} {'y' * len(PAD)}".encode()

    def rewrite() -> None:
        data = path.read_bytes()
        assert data.count(old) == 1
        with open(path, "r+b") as handle:
            handle.seek(data.index(old))
            handle.write(new)

    _fold("u-rewrite", during=rewrite)
    assert _witness_seq("u-rewrite") == valid, "a rewritten pass wrote a savepoint"
    assert _fold("u-rewrite") == _held["u-rewrite"].last_seq - valid
    assert _value("u-rewrite") == _cold_value("u-rewrite")
