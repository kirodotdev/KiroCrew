"""The two store recovery paths reuse a committed seq, and what a reader does about it.

The first half proves that dropping an orphan chunk group and rolling back a failed
append both bring back a folded seq with different bytes, in a file that looks appended.

The second half pins that the cut counter proves a warm fold from a few bytes, so the
folded prefix is not hashed on every read.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest
from crew_log_drain import assert_drained

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log.errors import CrewLogError, CutUnaccounted

SLOT = "chat-cutcounter"
UNIT = "acp-cut-unit"
OTHER = "acp-cut-other"


# Patched through the isolation floor's own MonkeyPatch (testing-conventions D11), so a
# test's own ``monkeypatch.undo()`` cannot strip the data home out from under the store.
@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch: pytest.MonkeyPatch):
    """Own data home, crew log on, and no warm fold carried between tests."""
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


def _a_completed_cut(directory) -> None:
    """Record a cut of *directory*'s log that has FINISHED, as the store records one.

    Both halves (``_open_cut`` / ``_close_cut``) in the store's order: a single raise
    would leave the counter odd, which reads as a cut still in flight.
    """
    crew_log_store._open_cut(directory)
    crew_log_store._close_cut(directory)


def _unit(unit_id: str, *, slot: str = SLOT) -> None:
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _dir(unit_id: str) -> Path:
    return lg.crew_log_path(lg.KIND_SESSION, unit_id).parent


def _log(unit_id: str) -> Path:
    return lg.crew_log_path(lg.KIND_SESSION, unit_id)


def _bytes_by_seq(unit_id: str) -> "dict[int, bytes]":
    """Every parseable record of *unit_id*'s log, keyed by the seq it carries.

    The raw bytes, not the decoded entry: what these tests are about is whether the
    bytes under a seq changed, and comparing decoded objects would let a re-encoding
    pass as equal.
    """
    found: dict[int, bytes] = {}
    for line in _log(unit_id).read_bytes().split(b"\n"):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
        except ValueError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("seq"), int):
            found[parsed["seq"]] = stripped
    return found


def _reused(before: "dict[int, bytes]", after: "dict[int, bytes]") -> "list[int]":
    """The seqs present in both that carry DIFFERENT bytes."""
    return sorted(seq for seq in before if seq in after and before[seq] != after[seq])


def _orphan_chunk_group(unit_id: str) -> None:
    """Leave a turn open, a tool call open, and one chunk nothing cites.

    The shape a hard kill mid-group leaves (``test_crew_log_real_crash.py`` shows it).
    One chunk and two openers, so the repair leaves the file taller, like an append.
    """
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
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


def _ledger(unit_id: str, **fields: Any) -> None:
    payload: dict[str, Any] = {"slot": SLOT}
    payload.update(fields)
    crew_log_emit.on_ledger_recorded(unit_id, payload)
    assert_drained()


def _stub_directory_sync(
    scoped: pytest.MonkeyPatch, directory: Path, *, supported: bool
) -> "dict[str, bool]":
    """Drive `_cuts_dir_forced`'s two outcomes without asking the host which it is.

    Directory sync support differs by platform, so the three descriptor calls are stubbed
    for THIS directory only; every other path delegates to the real calls.

    The returned dict records which calls were made, to tell "reported False" from
    "never got that far".
    """
    fake_fd = 0x7F000001  # past any real descriptor, so interception is by value
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    seen = {"opened": False, "fsynced": False, "closed": False}

    def _open(path, *args, **kwargs):
        if str(path) == str(directory):
            seen["opened"] = True
            return fake_fd
        return real_open(path, *args, **kwargs)

    def _fsync(fd):
        if fd != fake_fd:
            return real_fsync(fd)
        seen["fsynced"] = True
        if not supported:
            raise OSError(errno.EINVAL, "this filesystem cannot sync a directory")
        return None

    def _close(fd):
        if fd != fake_fd:
            return real_close(fd)
        seen["closed"] = True
        return None

    scoped.setattr(os, "open", _open)
    scoped.setattr(os, "fsync", _fsync)
    scoped.setattr(os, "close", _close)
    return seen


def _normalized(value: "dict[str, Any]") -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _cold(units: "tuple[str, ...]") -> str:
    return _normalized(crew_log.fold_slot("ledger", units, slot=SLOT).value)


def _warm(units: "tuple[str, ...]") -> str:
    return _normalized(
        crew_log.projection_of(crew_log.fold_slot_warm("ledger", units, slot=SLOT)).value
    )


# --------------------------------------------------------------------------- #
# the hazard: a committed seq really is reused, with other bytes
# --------------------------------------------------------------------------- #


def test_the_orphan_group_repair_reuses_a_committed_seq_with_other_bytes():
    """Dropping an unreachable chunk group hands its seq to a closer.

    The closers are numbered from the CUT, so the first of them lands on a seq the
    dropped group was using -- and because there are more closers than dropped lines,
    the file also ends up taller than it was. A reader that remembered the old height
    sees growth, which is what makes this indistinguishable from an append.
    """
    _unit(UNIT)
    _orphan_chunk_group(UNIT)

    before = _bytes_by_seq(UNIT)
    height_before = max(before)
    assert json.loads(before[3])["type"] == "message/chunk", before

    assert CrewLog.open(lg.KIND_SESSION, UNIT).repair_interrupted_turn() == 2

    after = _bytes_by_seq(UNIT)
    assert _reused(before, after) == [3], "no committed seq came back with other bytes"
    assert json.loads(after[3])["type"] == "tool/completed"
    assert max(after) > height_before, "the file did not grow, so a height test would catch it"


def test_a_rolled_back_append_reuses_a_committed_seq_with_other_bytes(monkeypatch):
    """An append whose fsync fails is removed, and the retry reuses its seq.

    The bytes are flushed before the failing fsync, so a lock-free reader can fold them.
    The file is read from inside the failing fsync, so nothing here is timed.
    """
    _unit(UNIT)
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")

    seen: "list[dict[int, bytes]]" = []
    real_fsync = os.fsync
    failed = {"once": False}

    def _refusing_fsync(fd):
        window = _bytes_by_seq(UNIT)
        seen.append(window)
        carries = any(b"ROLLED-BACK" in raw for raw in window.values())
        if carries and not failed["once"]:
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

    window = next(
        (snap for snap in seen if any(b"ROLLED-BACK" in raw for raw in snap.values())),
        None,
    )
    assert window is not None, "the flushed bytes were never on disk, so nothing was observable"
    assert 2 not in _bytes_by_seq(UNIT), "the rollback left the entry behind"

    handle.append(
        "tool/called",
        {"turn": 1, "name": "execute_bash", "call_id": "THE-RETRY", "server": "", "kind": ""},
        src="acp",
    )
    after = _bytes_by_seq(UNIT)
    assert _reused(window, after) == [2]
    assert json.loads(after[2])["data"]["call_id"] == "THE-RETRY"


# --------------------------------------------------------------------------- #
# the counter
# --------------------------------------------------------------------------- #


def test_a_new_log_starts_its_cut_counter_at_zero():
    """Seeded at create, so a log is on the cheap read path from its first entry.

    An absent counter is unproven rather than zero, so a log that never got one would
    fall back to hashing its folded records for as long as it lived.
    """
    _unit(UNIT)
    assert CrewLog.open(lg.KIND_SESSION, UNIT).cuts() == 0


def test_the_orphan_group_repair_raises_the_cut_counter():
    _unit(UNIT)
    _orphan_chunk_group(UNIT)
    assert CrewLog.open(lg.KIND_SESSION, UNIT).cuts() == 0

    CrewLog.open(lg.KIND_SESSION, UNIT).repair_interrupted_turn()

    assert CrewLog.open(lg.KIND_SESSION, UNIT).cuts() == 2, "one cut is two phases"


def test_a_rolled_back_append_raises_the_cut_counter(monkeypatch):
    _unit(UNIT)
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    assert handle.cuts() == 0

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

    assert handle.cuts() == 2, "one cut is two phases: in flight, then settled"


def test_an_append_that_removes_nothing_leaves_the_counter_alone():
    """A cut down to the size the file already has is not a cut.

    Counting it would send every reader of this unit back to the slow path for a
    mutation that did not happen.
    """
    _unit(UNIT)
    path = _log(UNIT)
    before = crew_log_store.read_cuts(_dir(UNIT))

    crew_log_store._truncate(path, path.stat().st_size)

    assert crew_log_store.read_cuts(_dir(UNIT)) == before


def test_an_unreadable_counter_reads_as_unproven_not_as_zero():
    """Garbage is ``None``, which every caller treats as "fall back to the digest".

    Read as zero it would compare equal across a cut whose counter was lost, which is
    the one reading the file exists to prevent.
    """
    _unit(UNIT)
    (_dir(UNIT) / crew_log_store._CUTS_FILE).write_bytes(b"not a number\n")

    assert crew_log_store.read_cuts(_dir(UNIT)) is None
    assert CrewLog.open(lg.KIND_SESSION, UNIT).cuts() is None


def test_the_counter_is_not_a_segment_and_is_not_walked_as_one():
    """Every segment walk filters on the segment naming rule, so the counter is invisible.

    Pinned because a counter that any walk mistook for a segment would be read as
    entries, and retention would be free to delete it off the front.
    """
    _unit(UNIT)
    paths = crew_log_store.segment_paths(lg.KIND_SESSION, UNIT)

    assert paths == [_log(UNIT)]
    assert (_dir(UNIT) / crew_log_store._CUTS_FILE).is_file()


# --------------------------------------------------------------------------- #
# what the reader does with it
# --------------------------------------------------------------------------- #


def _digest_calls(monkeypatch) -> "list[int]":
    """Every ``records`` boundary ``raw_prefix_digest`` is asked for, in order."""
    asked: list[int] = []
    real = CrewLog.raw_prefix_digest

    def _spy(self, records):
        asked.append(records)
        return real(self, records)

    monkeypatch.setattr(CrewLog, "raw_prefix_digest", _spy)
    return asked


def test_a_warm_read_does_not_hash_the_records_it_already_folded(monkeypatch):
    """The cost this change exists to remove.

    Re-walking and re-hashing the whole already-folded prefix of the newest unit makes
    an idle board's reads more expensive as its log grows while nothing about it changes.
    The counter answers the same question from a few bytes.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="one", event_kind="progress")
    units = (UNIT,)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    asked = _digest_calls(monkeypatch)
    for step in range(2, 8):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
        crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    assert asked == [], f"the warm path still hashed the folded prefix: {asked}"


def test_a_warm_read_of_a_log_with_no_counter_still_hashes_its_folded_records(monkeypatch):
    """The upgrade story: a log written before the counter existed keeps the old proof.

    Nothing migrates such a log, and nothing has to. Its counter is absent, which reads
    as unproven, and the digest is then the only evidence there is -- so the reader uses
    it, exactly as it did before, until the log's first cut creates a counter.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="one", event_kind="progress")
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    units = (UNIT,)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    asked = _digest_calls(monkeypatch)
    _ledger(UNIT, event="two", event_kind="progress")
    assert _warm(units) == _cold(units)

    assert asked, "a log with no counter was continued on no evidence at all"


def test_a_counter_that_moved_folds_cold_rather_than_continuing():
    """A cut landed, so the records the cell folded may be gone. Refold, do not resume.

    Asserted on the VALUE, not on a read count: what matters is that the answer served
    is the one a fold from empty reaches over the bytes that are there now.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    # The shape a cut leaves: an already-folded record carries other bytes, and the
    # file is taller than it was. The counter is what says so.
    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "CUT-AND-GREW")
    _a_completed_cut(_dir(UNIT))
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "CUT-AND-GREW",
        "ORIGINAL-THREE",
    ]


def test_a_deleted_counter_folds_cold_rather_than_continuing():
    """A counter that vanishes is unproven, and unproven with no digest refuses.

    The direction that costs a fold from empty rather than serving a cell built out of
    bytes nothing can vouch for.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "LOST-COUNTER")
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "LOST-COUNTER",
        "ORIGINAL-THREE",
    ]


# --------------------------------------------------------------------------- #
# warm equals cold across the real recovery paths
# --------------------------------------------------------------------------- #


def test_a_warm_read_across_a_real_orphan_group_repair_equals_a_cold_fold():
    """The whole point, over the real store path rather than an edited file.

    A read lands between the orphan group and the repair, so the cell holds the dropped
    chunk and a height the repaired file has climbed back past. Continuing from there
    would serve entries the log does not hold and miss the closers that took their seqs.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="BEFORE-THE-TEAR", event_kind="progress")
    _orphan_chunk_group(UNIT)
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    CrewLog.open(lg.KIND_SESSION, UNIT).repair_interrupted_turn()
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="AFTER-THE-REPAIR", event_kind="progress")

    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "BEFORE-THE-TEAR",
        "AFTER-THE-REPAIR",
    ]


def test_a_warm_read_across_an_older_unit_being_cut_equals_a_cold_fold():
    """An earlier unit's cut is caught by whole-mark equality, which now covers the count.

    The newest unit grows here too, which is the one shape a continuation may carry --
    so without the earlier unit's own evidence this growth alone would qualify for the
    tail-only route and the cut unit would never be read again.
    """
    _unit(UNIT)
    _unit(OTHER)
    _ledger(UNIT, goal="g", event="OLDEST-ORIGINAL", event_kind="progress")
    _ledger(OTHER, event="NEWEST", event_kind="progress")
    units = (UNIT, OTHER)
    assert _warm(units) == _cold(units)

    _rewrite_in_place(UNIT, "OLDEST-ORIGINAL", "OLDEST-CUT-BACK")
    _a_completed_cut(_dir(UNIT))
    crew_log_emit.reset_caches()
    _ledger(OTHER, event="NEWEST-TWO", event_kind="progress")

    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "OLDEST-CUT-BACK",
        "NEWEST",
        "NEWEST-TWO",
    ]


def _rewrite_in_place(unit_id: str, find: str, replace: str) -> None:
    """Swap one string inside an already-committed record, same length, same seq.

    Stands in for the bytes a cut and a regrow leave behind, with the length held equal
    so a size comparison cannot be what catches it. The cut counter, or the digest, has
    to be.
    """
    assert len(find) == len(replace), "the replacement must not move the file's size"
    path = _log(unit_id)
    blob = path.read_bytes()
    assert find.encode() in blob, f"{find!r} is not in the log, so nothing was rewritten"
    path.write_bytes(blob.replace(find.encode(), replace.encode()))


# --------------------------------------------------------------------------- #
# the narrowing this trades for the speed, stated rather than left implied
# --------------------------------------------------------------------------- #


def test_an_uncounted_in_place_rewrite_is_served_warm_on_a_counter_bearing_unit():
    """The accepted narrowing, pinned so it is a decision and not a surprise.

    With a counter, an in-place rewrite that is not a cut (hand edit, corruption) keeps
    the count and is served warm. Asserting the divergence makes a return to hashing
    the folded prefix a deliberate decision.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "HAND-EDITED!")
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))

    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "ORIGINAL-TWO",
        "ORIGINAL-THREE",
    ], "the warm read keeps what it folded, because no cut was recorded"
    assert [event["text"] for event in json.loads(_cold(units))["events"]] == [
        "ORIGINAL-ONE",
        "HAND-EDITED!",
        "ORIGINAL-THREE",
    ], "and a fold from empty reads the edited bytes"


def test_an_uncounted_in_place_rewrite_is_still_caught_on_a_unit_with_no_counter():
    """The same edit on a counter-less log, where the digest is the evidence.

    What the narrowing above costs is bounded by this: the fallback has not been removed,
    so a log the counter does not cover is defended exactly as it was.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "HAND-EDITED!")
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "HAND-EDITED!",
        "ORIGINAL-THREE",
    ]


# --------------------------------------------------------------------------- #
# the on-disk savepoint settles its prefix the same way
# --------------------------------------------------------------------------- #


@pytest.fixture
def _cheap_writes(monkeypatch):
    """Earn a savepoint write after two entries instead of 256.

    The threshold is a property of the savepoint files and is pinned where they are
    (``test_crew_log_slot_fold_savepoint.py``); what the tests below are about is which
    EVIDENCE a savepoint records, so paying 256 emitter round trips to reach one would
    buy nothing.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    monkeypatch.setattr(savepoints, "MIN_ADVANCE_ENTRIES", 2)


def _savepoint(unit_id: str) -> "dict[str, Any]":
    from kiro_crew.crew_log import checkpoint as savepoints

    path = savepoints.slot_checkpoint_path(lg.KIND_SESSION, unit_id, "ledger")
    return json.loads(path.read_text(encoding="utf-8"))


def _after_a_restart(units: "tuple[str, ...]") -> str:
    """One read with the warm table cleared, so the savepoint on disk is what answers."""
    crew_log.forget_slot_folds()
    return _warm(units)


def test_a_savepoint_on_a_counter_bearing_unit_records_the_count_not_a_digest(_cheap_writes):
    """One spelling of the evidence, chosen by what the unit states.

    A digest would decode every record up to the boundary on each savepoint write; the
    counter gives the same proof from a few bytes.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    witness = _savepoint(UNIT)["witness"]

    assert witness["prefix_cuts"] == 0, "the unit's count, and 0 is a real reading"
    assert witness["seq"] == 6, "at the boundary the pass reached"
    assert "prefix_sha" not in witness and "prefix_records" not in witness


def test_writing_a_savepoint_on_a_counter_bearing_unit_hashes_nothing(monkeypatch, _cheap_writes):
    """The write side of the same saving, measured rather than argued.

    A digest witness has to be read before the pass and re-confirmed after it, so a
    savepoint written on every advanced read charged two whole-file walks to a path whose
    job is to make the NEXT process cheaper.
    """
    _unit(UNIT)
    _ledger(UNIT, event="one", event_kind="progress")
    units = (UNIT,)
    crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    asked = _digest_calls(monkeypatch)
    for step in range(2, 8):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
        crew_log.fold_slot_warm("ledger", units, slot=SLOT)

    assert _savepoint(UNIT)["witness"]["prefix_cuts"] == 0, "savepoints were written"
    assert asked == [], f"the savepoint writes still hashed the log: {asked}"


def test_a_savepoint_written_before_a_cut_is_refused_and_the_read_folds_cold(_cheap_writes):
    """A cut since the write means the records the state came from may be gone.

    The savepoint's whole claim is about bytes, so a count that has moved retires it --
    and the read that finds it that way reaches the fold-from-empty answer over the bytes
    the file holds now.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    for step in range(2, 7):
        _ledger(UNIT, event=f"ORIGINAL-{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    assert _savepoint(UNIT)["witness"]["prefix_cuts"] == 0

    _rewrite_in_place(UNIT, "ORIGINAL-6", "CUT-N-GREW")
    _a_completed_cut(_dir(UNIT))
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="AFTER-THE-CUT", event_kind="progress")

    resumed = json.loads(_after_a_restart(units))

    assert resumed == json.loads(_cold(units))
    assert [event["text"] for event in resumed["events"]][-2:] == [
        "CUT-N-GREW",
        "AFTER-THE-CUT",
    ]


def test_a_savepoint_whose_counter_was_deleted_is_refused(_cheap_writes):
    """An absent count is NOT PROVEN, never zero, on the disk path as on the warm one.

    A counter removed after a cut would otherwise compare equal across it. Reading absent
    as zero is the one shortcut this evidence exists to refuse, so it costs a cold fold.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    assert _savepoint(UNIT)["witness"]["prefix_cuts"] == 0

    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="after", event_kind="progress")

    crew_log.forget_slot_folds()
    parsed: list[int] = []
    real = CrewLog.iter_from

    def _spy(self, seq, *args, **kwargs):
        for entry in real(self, seq, *args, **kwargs):
            parsed.append(seq)
            yield entry

    try:
        CrewLog.iter_from = _spy  # type: ignore[method-assign]
        resumed = _warm(units)
    finally:
        CrewLog.iter_from = real  # type: ignore[method-assign]

    assert resumed == _cold(units)
    assert parsed and min(parsed) == 1, "the savepoint was refused and the log read whole"


def test_a_log_with_no_counter_still_gets_a_digest_witness(_cheap_writes):
    """The upgrade story on the disk path: no counter, so the old evidence, and it works.

    Nothing migrates and nothing is rewritten. Such a log keeps being settled by the
    digest exactly as before -- which is also what keeps the narrowing above bounded.
    """
    _unit(UNIT)
    _ledger(UNIT, event="one", event_kind="progress")
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    for step in range(2, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    witness = _savepoint(UNIT)["witness"]

    assert len(witness["prefix_sha"]) == 64
    assert witness["prefix_records"] >= witness["seq"]
    assert "prefix_cuts" not in witness, "one spelling, never both"
    assert _after_a_restart(units) == _cold(units), "and it resumes from that witness"


# --------------------------------------------------------------------------- #
# the counter brackets a cut, so no reading spans one
# --------------------------------------------------------------------------- #


def test_a_reader_that_samples_mid_cut_is_told_nothing_rather_than_a_matchable_count():
    """The window a single raise would leave open, closed by the second one.

    A lock-free reader can sample between the raise and the byte move. With one raise
    that value would match again after the cut; the odd in-flight value reports nothing.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="one", event_kind="progress")
    directory = _dir(UNIT)
    settled_before = crew_log_store.read_cuts(directory)

    crew_log_store._open_cut(directory)
    mid = crew_log_store.read_cuts(directory)
    raw_mid = (directory / crew_log_store._CUTS_FILE).read_text(encoding="ascii").strip()
    crew_log_store._close_cut(directory)
    settled_after = crew_log_store.read_cuts(directory)

    assert settled_before == 0, "a fresh log is settled at zero"
    assert mid is None, "a sample taken inside the cut reports nothing to match"
    assert int(raw_mid) % 2 == 1, f"the in-flight value is odd on disk, not {raw_mid}"
    assert settled_after is not None and settled_after % 2 == 0, settled_after
    assert settled_after != settled_before, "and it differs from the reading before the cut"


def test_a_real_cut_moves_the_settled_count_by_two():
    """Both halves run on the store's own path, not only in the helper above.

    Phases rather than cuts, which is the counter's unit: one completed cut is an open
    and a close, so the settled value moves by two. Asserted on the real repair so a
    call site that opened without closing -- leaving the unit unproven for good -- fails
    here rather than being discovered as a permanent slow path.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="one", event_kind="progress")
    _orphan_chunk_group(UNIT)
    before = crew_log_store.read_cuts(_dir(UNIT))

    assert CrewLog.open(lg.KIND_SESSION, UNIT).repair_interrupted_turn() == 2
    after = crew_log_store.read_cuts(_dir(UNIT))

    assert before == 0
    assert after == 2, f"one completed cut is two phases, not {after}"


def test_a_counter_that_cannot_be_raised_is_retired_so_it_proves_nothing():
    """A failed raise must not leave the OLD value standing while the cut goes ahead.

    A failed raise is likely a full disk; the old value would let readers match across
    the cut. Retiring needs no space, so the counter reads as not proven instead.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    assert crew_log_store.read_cuts(_dir(UNIT)) == 0

    real_write = crew_log_store._write_cuts
    try:
        crew_log_store._write_cuts = lambda *a, **k: False  # type: ignore[assignment]
        crew_log_store._open_cut(_dir(UNIT))
    finally:
        crew_log_store._write_cuts = real_write  # type: ignore[assignment]

    assert crew_log_store.read_cuts(_dir(UNIT)) is None, "the old count must not stand"

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "CUT-AND-GREW")
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units)), "an unproven counter must not serve stale state"
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "CUT-AND-GREW",
        "ORIGINAL-THREE",
    ]


def test_a_cut_refuses_when_the_counter_can_neither_be_raised_nor_retired():
    """The one case left, and it refuses the cut rather than cutting on a false proof.

    Cutting here would let the old count (also held by the slot savepoint) certify the
    removed bytes across restarts. Leaving the file is recoverable: the next open repairs.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="one", event_kind="progress")
    before = _log(UNIT).read_bytes()

    real_write = crew_log_store._write_cuts
    real_unprove = crew_log_store._unprove_cuts
    try:
        crew_log_store._write_cuts = lambda *a, **k: False  # type: ignore[assignment]
        crew_log_store._unprove_cuts = lambda *a, **k: False  # type: ignore[assignment]
        with pytest.raises(CutUnaccounted, match="neither be raised nor retired"):
            crew_log_store._truncate(_log(UNIT), 10)
    finally:
        crew_log_store._write_cuts = real_write  # type: ignore[assignment]
        crew_log_store._unprove_cuts = real_unprove  # type: ignore[assignment]

    assert _log(UNIT).read_bytes() == before, "the refusal left the file alone"


def test_a_close_that_fails_leaves_the_counter_in_flight_rather_than_settled():
    """The safe direction, which is why the close is best-effort and the open is not.

    A counter left odd reads as a cut in flight for good: every reader falls back to the
    digest or folds from empty, which is slower and never wrong. That is also what a
    crash between the truncation and the close leaves behind, so the failure and the
    crash get one answer instead of two.
    """
    _unit(UNIT)
    _ledger(UNIT, goal="g", event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, goal="g", event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)

    crew_log_store._open_cut(_dir(UNIT))
    real_write = crew_log_store._write_cuts
    try:
        crew_log_store._write_cuts = lambda *a, **k: False  # type: ignore[assignment]
        crew_log_store._close_cut(_dir(UNIT))
    finally:
        crew_log_store._write_cuts = real_write  # type: ignore[assignment]

    assert crew_log_store.read_cuts(_dir(UNIT)) is None, "still in flight, so still unproven"

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "CUT-AND-GREW")
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")

    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "CUT-AND-GREW",
        "ORIGINAL-THREE",
    ]


def test_a_savepoint_cannot_be_written_or_admitted_from_a_mid_cut_reading(_cheap_writes):
    """The savepoint half of the same window: no witness names an in-flight count.

    `_slot_witness` reads the counter again at the write, so a pass whose sampled count
    differs from the settled one records nothing -- and `_cuts_admit` refuses a witness
    citing a count the unit does not state. Both directions keep an odd reading out of a
    file that outlives the process.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    witness = _savepoint(UNIT)["witness"]
    assert witness["prefix_cuts"] == 0

    # A cut opens and does not close: the counter is in flight from here on.
    crew_log_store._open_cut(_dir(UNIT))

    from kiro_crew.crew_log import checkpoint as savepoint_module

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    assert handle.cuts() is None, "the unit states nothing while its cut is in flight"
    assert savepoint_module._cuts_admit(handle, 0) is False, "a stale count is refused"
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="after", event_kind="progress")
    crew_log.forget_slot_folds()

    assert _warm(units) == _cold(units), "the refused savepoint still reaches the cold answer"
    assert "prefix_cuts" not in _savepoint(UNIT)["witness"], (
        "the stale count must not survive: a unit whose counter states nothing falls all "
        "the way back to the digest, on the write side as well as the read side"
    )


# --------------------------------------------------------------------------- #
# no value is issued twice, so a held count can only mean the state it came from
# --------------------------------------------------------------------------- #


def test_a_cut_after_an_interrupted_one_never_re_issues_the_count_it_left(_cheap_writes):
    """A cut left in flight must not be recovered from by starting the count again.

    A savepoint holds 2, then a cut is left open at 3. Restarting from 1 would close on 2
    and admit that savepoint, so the next cut advances from the odd number on disk.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    directory = _dir(UNIT)
    _a_completed_cut(directory)
    assert crew_log_store.read_cuts(directory) == 2, "the count a reader now holds"

    # The crash: opened, never closed.
    crew_log_store._open_cut(directory)
    assert (directory / crew_log_store._CUTS_FILE).read_text(encoding="ascii").strip() == "3"

    # The next cut, which must not come back to 2.
    _a_completed_cut(directory)
    settled = crew_log_store.read_cuts(directory)

    from kiro_crew.crew_log import checkpoint as savepoint_module

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)

    assert settled is not None and settled % 2 == 0, settled
    assert settled != 2, "2 was already issued, and a savepoint can still be holding it"
    assert settled == 6, f"two phases on from the odd 3 the crash left, not {settled}"
    assert (
        savepoint_module._cuts_admit(handle, 2) is False
    ), "a witness holding the pre-cut count must stay refused across the interruption"


def test_a_cut_after_a_lost_counter_starts_past_every_count_it_could_have_issued(_cheap_writes):
    """The same rule where there is no number on disk to advance from.

    A retired or never-written counter tells a writer nothing; counting from one would
    close on 2, the commonest savepoint value. The nanosecond clock starts past any count.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    directory = _dir(UNIT)
    _a_completed_cut(directory)
    assert crew_log_store.read_cuts(directory) == 2
    assert _savepoint(UNIT)["witness"]["prefix_cuts"] == 0, "a witness holding an issued count"

    (directory / crew_log_store._CUTS_FILE).unlink()
    assert crew_log_store.read_cuts(directory) is None, "nothing proven, nothing to count from"

    _a_completed_cut(directory)
    settled = crew_log_store.read_cuts(directory)

    from kiro_crew.crew_log import checkpoint as savepoint_module

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)

    assert settled is not None and settled % 2 == 0, settled
    assert settled > 2, f"a restarted count would land on 0 or 2, not {settled}"
    assert savepoint_module._cuts_admit(handle, 0) is False, "0 was issued by this unit"
    assert savepoint_module._cuts_admit(handle, 2) is False, "and so was 2"


# --------------------------------------------------------------------------- #
# the counter is durable before the bytes move, or the cut does not happen
# --------------------------------------------------------------------------- #


def test_publishing_the_counter_forces_out_the_directory_that_names_it(monkeypatch):
    """A renamed-into-place counter is not durable until its directory is.

    The name lives in the parent directory: without a sync, a power-off can bring back the
    pre-cut count beside the truncated log.
    """
    _unit(UNIT)
    _ledger(UNIT, event="one", event_kind="progress")
    directory = _dir(UNIT)
    real_sync = crew_log_store.fsync_dir
    synced: "list[Path]" = []

    def _recording_sync(path, **kwargs):
        synced.append(Path(path))
        return real_sync(path, **kwargs)

    monkeypatch.setattr(crew_log_store, "fsync_dir", _recording_sync)
    _a_completed_cut(directory)

    assert synced.count(directory) == 2, f"once per phase written, not {synced.count(directory)}"


def test_a_cut_outlives_a_directory_that_cannot_be_synced(monkeypatch):
    """A cut does not need the directory, because the invalidation does not.

    Only the directory sync fails. Emptying the counter in place is forced by the file
    fsync and needs no directory entry, so the cut goes ahead and the digest decides.
    """
    _unit(UNIT)
    _ledger(UNIT, event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    assert crew_log_store.read_cuts(_dir(UNIT)) == 0
    before = _log(UNIT).read_bytes()

    def _unsyncable_dir(path, **kwargs):
        raise OSError(5, "the test refuses this directory sync")

    with monkeypatch.context() as scoped:
        scoped.setattr(crew_log_store, "fsync_dir", _unsyncable_dir)
        _a_completed_cut(_dir(UNIT))

    assert (
        crew_log_store.read_cuts(_dir(UNIT)) is None
    ), "the old count must not stand: the sync that would publish a new one failed"
    assert _log(UNIT).read_bytes() == before, "and the counter path touched no history"

    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "CUT-AND-GREW")
    crew_log_emit.reset_caches()
    _ledger(UNIT, goal="g", event="ORIGINAL-THREE", event_kind="progress")
    crew_log.forget_slot_folds()
    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units)), "an unproven counter must not serve stale state"
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "CUT-AND-GREW",
        "ORIGINAL-THREE",
    ]


# --------------------------------------------------------------------------- #
# a cut that cannot be accounted for is an unknown outcome, not a refusal
# --------------------------------------------------------------------------- #


def test_a_rollback_that_cannot_account_for_its_cut_is_retryable_not_a_refusal(monkeypatch):
    """The bytes are still in the file, so the entry is still worth writing again.

    On a read-only filesystem the rollback declines to truncate. A ``CrewLogError`` would
    make the writer drop the event; :class:`IndeterminateAppend` makes it retry.
    """
    from kiro_crew.crew_log import writer as crew_log_writer
    from kiro_crew.crew_log.errors import IndeterminateAppend

    _unit(UNIT)
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    handle.append("turn/started", {"turn": 1, "actor": "user", "depth": 0}, src="acp")
    real_fsync = os.fsync
    failed = {"once": False}

    def _refusing_fsync(fd):
        if not failed["once"] and b"UNACCOUNTED" in _log(UNIT).read_bytes():
            failed["once"] = True
            raise OSError(30, "the test makes the filesystem read-only here")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _refusing_fsync)
    monkeypatch.setattr(crew_log_store, "_write_cuts", lambda *a, **k: False)
    monkeypatch.setattr(crew_log_store, "_unprove_cuts", lambda *a, **k: False)
    with pytest.raises(IndeterminateAppend) as raised:
        handle.append(
            "tool/called",
            {"turn": 1, "name": "fs_write", "call_id": "UNACCOUNTED", "server": "", "kind": ""},
            src="acp",
        )

    assert failed["once"], "the append never failed, so nothing was rolled back"
    assert not isinstance(raised.value, CrewLogError), "a refusal would be dropped, not retried"
    assert (
        crew_log_writer._is_refusal(type(raised.value)) is False
    ), "the writer has to read this as an unknown outcome and retry the event"
    assert (
        b"UNACCOUNTED" in _log(UNIT).read_bytes()
    ), "the refused rollback left the bytes in place, which is why the outcome is unknown"
    # `raised` holds a traceback through this handle's append, and the test frame holds
    # `raised`: a cycle only the collector frees. Release the lease now, not at a GC.
    handle.release_ownership()


def test_a_counter_that_cannot_be_read_is_not_evidence_that_it_was_retired(_cheap_writes):
    """Retirement is an action taken, never a reading taken.

    A Windows sharing lock blocks raise, remove and read while the old value stays on
    disk. Unreadable reads like absent, so both attempts failing must refuse the cut.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"e{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    directory = _dir(UNIT)
    held = crew_log_store.read_cuts(directory)
    assert held == 0, "the count a savepoint is holding"
    assert _savepoint(UNIT)["witness"]["prefix_cuts"] == held
    before = _log(UNIT).read_bytes()
    real_path = crew_log_store._cuts_path(directory)
    real_bytes = real_path.read_bytes()

    class _Locked(os.PathLike):
        """The counter while another process holds it: every way in fails.

        ``unlink`` and ``read_bytes`` raise outright, and ``open`` is pointed at a
        directory so it refuses a path that really exists -- the counter is held, not
        gone, which are different facts with different answers.
        """

        def __fspath__(self) -> str:
            # The unit's own DIRECTORY: it exists, so this is not "the counter is
            # absent" -- `open` refuses it with IsADirectoryError, which is the shape
            # of a file that is there and cannot be opened.
            return str(real_path.parent)

        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError(13, "another process holds the counter")

        def read_bytes(self) -> bytes:
            raise PermissionError(13, "another process holds the counter")

    real_cuts_path = crew_log_store._cuts_path
    real_write = crew_log_store._write_cuts
    try:
        # The lock denies the replace as well, which is why the raise is the premise
        # here rather than another thing to assert.
        crew_log_store._write_cuts = lambda *a, **k: False  # type: ignore[assignment]
        crew_log_store._cuts_path = lambda _directory: _Locked()  # type: ignore[assignment]
        assert (
            crew_log_store.read_cuts(directory) is None
        ), "a locked counter reports nothing, which is why a reading cannot decide this"
        with pytest.raises(CutUnaccounted, match="neither be raised nor retired"):
            crew_log_store._truncate(_log(UNIT), 10)
    finally:
        crew_log_store._cuts_path = real_cuts_path  # type: ignore[assignment]
        crew_log_store._write_cuts = real_write  # type: ignore[assignment]

    from kiro_crew.crew_log import checkpoint as savepoint_module

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)

    assert _log(UNIT).read_bytes() == before, "the refusal left the log alone"
    assert real_path.read_bytes() == real_bytes, "and left the counter saying what it said"
    assert crew_log_store.read_cuts(directory) == held, "so the count comes back unchanged"
    assert (
        savepoint_module._cuts_admit(handle, held) is True
    ), "and it is still TRUE, because the records it stands for were never cut"
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    warm = json.loads(_warm(units))
    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [f"e{step}" for step in range(1, 7)]


def test_a_torn_tail_repair_that_cannot_account_for_its_cut_keeps_the_event(_cheap_writes):
    """A refused cut must not cost the entry the caller was trying to write.

    The torn-tail cut runs inside `append`, and the write-behind drops on `CrewLogError`.
    `CutUnaccounted` is an `OSError` so the append is retried once the lock lifts.
    """
    from kiro_crew.crew_log import writer as crew_log_writer

    _unit(UNIT)
    _ledger(UNIT, goal="g", event="BEFORE-THE-TEAR", event_kind="progress")
    directory = _dir(UNIT)
    held = crew_log_store.read_cuts(directory)
    assert held == 0
    # The handle is opened BEFORE the tear, which is what puts the repair on the APPEND
    # path rather than in an `open`: a long-lived writer holds its handle across a crash
    # somewhere else, so its next append is what finds the torn bytes.
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    _log(UNIT).write_bytes(_log(UNIT).read_bytes() + b'{"type":"activity/tick","seq":9,"ti')
    torn = _log(UNIT).read_bytes()
    real_path = crew_log_store._cuts_path(directory)
    real_bytes = real_path.read_bytes()

    class _Locked(os.PathLike):
        """The counter while another process holds it open: no way in works."""

        def __fspath__(self) -> str:
            # The unit's own DIRECTORY: it exists, so this is not "the counter is
            # absent" -- `open` refuses it with IsADirectoryError, which is the shape
            # of a file that is there and cannot be opened.
            return str(real_path.parent)

        def unlink(self, missing_ok: bool = False) -> None:
            raise PermissionError(13, "another process holds the counter")

        def read_bytes(self) -> bytes:
            raise PermissionError(13, "another process holds the counter")

    real_cuts_path = crew_log_store._cuts_path
    real_write = crew_log_store._write_cuts
    try:
        crew_log_store._write_cuts = lambda *a, **k: False  # type: ignore[assignment]
        crew_log_store._cuts_path = lambda _directory: _Locked()  # type: ignore[assignment]
        with pytest.raises(CutUnaccounted) as raised:
            handle.append(
                "ledger/recorded",
                {"slot": SLOT, "event": "THE-ENTRY-AT-RISK", "event_kind": "progress"},
                src="gateway",
            )
    finally:
        crew_log_store._cuts_path = real_cuts_path  # type: ignore[assignment]
        crew_log_store._write_cuts = real_write  # type: ignore[assignment]

    assert (
        crew_log_writer._is_refusal(type(raised.value)) is False
    ), "a refusal is dropped by the write-behind, so this entry would be lost for good"
    assert isinstance(raised.value, OSError), "the cause is a storage fault, and it clears"
    assert not isinstance(raised.value, CrewLogError)
    assert raised.value.code == "cut_unaccounted"
    assert _log(UNIT).read_bytes() == torn, "the refusal truncated nothing"
    assert real_path.read_bytes() == real_bytes, "and left the counter saying what it said"

    # The lock lifts, and the same entry lands.
    landed = CrewLog.open(lg.KIND_SESSION, UNIT).append(
        "ledger/recorded",
        {"slot": SLOT, "event": "THE-ENTRY-AT-RISK", "event_kind": "progress"},
        src="gateway",
    )

    assert landed.seq == 2, landed.seq
    assert (
        b'{"type":"activity/tick","seq":9,"ti' not in _log(UNIT).read_bytes()
    ), "the torn bytes were cut by the repair this append DID complete"
    assert crew_log_store.read_cuts(directory) == held + 2, "one completed cut, two phases"
    # See the rollback test above: `raised` keeps this handle in a cycle.
    handle.release_ownership()
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    warm = json.loads(_warm((UNIT,)))
    assert warm == json.loads(_cold((UNIT,)))
    assert [event["text"] for event in warm["events"]] == [
        "BEFORE-THE-TEAR",
        "THE-ENTRY-AT-RISK",
    ]


def test_a_lost_rename_on_a_filesystem_that_cannot_sync_a_directory_proves_nothing(
    _cheap_writes,
):
    """A directory sync that was never possible is not a durable publication.

    `fsync_dir` returns quietly where unsupported, so a power loss can lose the rename and
    keep the old inode. Emptying it in place first means a lost rename proves nothing.

    Modelled as a no-op directory sync plus a rename that never lands.
    """
    _unit(UNIT)
    for step in range(1, 7):
        _ledger(UNIT, event=f"EVENT-{step}", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    directory = _dir(UNIT)
    _a_completed_cut(directory)
    held = crew_log_store.read_cuts(directory)
    assert held == 2, "the count a savepoint is holding"

    real_sync = crew_log_store.fsync_dir
    real_atomic = crew_log_store.atomic_write
    try:
        # The filesystem cannot express a directory sync, so nothing refuses it and
        # nothing is made durable by it either.
        crew_log_store.fsync_dir = lambda *a, **k: None  # type: ignore[assignment]
        # The rename's metadata never reaches disk, so the counter's name still points
        # at the inode it pointed at before.
        crew_log_store.atomic_write = lambda *a, **k: None  # type: ignore[assignment]
        crew_log_store._open_cut(directory)
    finally:
        crew_log_store.fsync_dir = real_sync  # type: ignore[assignment]
        crew_log_store.atomic_write = real_atomic  # type: ignore[assignment]

    from kiro_crew.crew_log import checkpoint as savepoint_module

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)

    assert (
        crew_log_store.read_cuts(directory) is None
    ), f"the pre-cut count came back: {crew_log_store.read_cuts(directory)}"
    assert (
        savepoint_module._cuts_admit(handle, held) is False
    ), "a witness holding the pre-cut count must not be admitted across the cut"

    # And the reader really does fall back rather than serving the stale fold.
    _rewrite_in_place(UNIT, "EVENT-6", "CUTGREW")
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="after", event_kind="progress")
    crew_log.forget_slot_folds()
    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "EVENT-1",
        "EVENT-2",
        "EVENT-3",
        "EVENT-4",
        "EVENT-5",
        "CUTGREW",
        "after",
    ]


def test_a_counter_too_long_to_convert_reads_as_unproven_rather_than_raising():
    """All digits is not the same as convertible, and a fold must not learn that twice.

    CPython refuses ``int`` on more than 4300 digits, though `str.isdecimal` accepts them.
    A `ValueError` from `read_cuts` would fail the fold, so it reads as not proven.
    """
    _unit(UNIT)
    _ledger(UNIT, event="ORIGINAL-ONE", event_kind="progress")
    _ledger(UNIT, event="ORIGINAL-TWO", event_kind="progress")
    units = (UNIT,)
    assert _warm(units) == _cold(units)
    directory = _dir(UNIT)
    assert crew_log_store.read_cuts(directory) == 0

    too_long = "9" * 5000
    with pytest.raises(ValueError):
        int(too_long)  # the limit this test exists for, asserted rather than assumed
    (directory / crew_log_store._CUTS_FILE).write_text(f"{too_long}\n", encoding="ascii")

    assert crew_log_store.read_cuts(directory) is None, "unproven, not an exception"
    assert crew_log_store._read_cuts_raw(directory) is None, "and the writer's read agrees"

    # The fold still answers, and it answers the cold answer rather than a stale one.
    _rewrite_in_place(UNIT, "ORIGINAL-TWO", "CUT-AND-GREW")
    crew_log_emit.reset_caches()
    _ledger(UNIT, event="ORIGINAL-THREE", event_kind="progress")
    crew_log.forget_slot_folds()
    warm = json.loads(_warm(units))

    assert warm == json.loads(_cold(units))
    assert [event["text"] for event in warm["events"]] == [
        "ORIGINAL-ONE",
        "CUT-AND-GREW",
        "ORIGINAL-THREE",
    ]

    # And a cut from here still never re-issues a value: there is no number to advance
    # from, so the phase restarts past anything this unit could have settled on.
    _a_completed_cut(directory)
    settled = crew_log_store.read_cuts(directory)
    assert settled is not None and settled % 2 == 0 and settled > 2, settled


def test_a_cut_refuses_when_only_an_unsynced_unlink_stands_behind_it(monkeypatch):
    """The weaker retirement is not allowed to rest on a sync that cannot happen.

    When emptying in place fails, the unlink fallback is a directory change, so it needs
    a directory sync that was actually performed. Neither works here, so the cut refuses.

    Stubbed because directory sync support differs by platform (Windows has none).
    """
    _unit(UNIT)
    _ledger(UNIT, event="one", event_kind="progress")
    before = _log(UNIT).read_bytes()
    directory = _dir(UNIT)

    with monkeypatch.context() as scoped:
        seen = _stub_directory_sync(scoped, directory, supported=True)
        assert crew_log_store._cuts_dir_forced(directory) is True
        assert seen == {"opened": True, "fsynced": True, "closed": True}, seen

    with monkeypatch.context() as scoped:
        seen = _stub_directory_sync(scoped, directory, supported=False)
        assert (
            crew_log_store._cuts_dir_forced(directory) is False
        ), "a sync the filesystem cannot express is not a sync that happened"
        assert seen["closed"], "and the descriptor is closed even when the sync is refused"

    with monkeypatch.context() as scoped:
        # The raise cannot land and the counter cannot be emptied in place: a full disk
        # and a counter held open, which is the state the fallback exists for. The unlink
        # itself is real and succeeds; the directory sync behind it cannot happen.
        _stub_directory_sync(scoped, directory, supported=False)
        scoped.setattr(crew_log_store, "_write_cuts", lambda *a, **k: False)
        scoped.setattr(crew_log_store, "_retire_cuts_inode", lambda *a, **k: False)
        with pytest.raises(CutUnaccounted, match="neither be raised nor retired"):
            crew_log_store._truncate(_log(UNIT), 10)

    assert _log(UNIT).read_bytes() == before, "the refusal left the log alone"
