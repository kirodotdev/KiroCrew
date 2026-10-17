"""Resuming a log walk at a remembered byte offset, and every reason not to.

``iter_from`` yields from a seq, and reaching that seq means decoding every record
below it -- deliberately, because its rule is that every seq it WALKS must advance, and
a record it never read is a record it never checked. The cost of that rule is in
``test_crew_log_tail_read_cost.py``; this file is the mechanism and its limits.

A walk can report where it stopped, and a later walk handed that report opens the file
there. What it gives up is the prefix: the advancing-seq rule, the earlier segments'
headers, their filename-against-first-entry claims and the boundaries between them no
longer run over those records. So a point is honoured only against evidence that
nothing below it moved -- the unit's cut count unchanged, and the record at the offset
still carrying the seq it names. Each test below is one way that evidence fails, and
each asserts the walk falls back rather than trusting the point.

The last test is the trade itself, written as a test so the residual risk is named in
code rather than only in a design note.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log.schema import Entry
from kiro_crew.crew_log.store import ReadCursor, ResumePoint

SLOT = "chat-iterfrom"
UNIT = "acp-iterfrom-unit"


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


def _entries(unit_id: str, count: int) -> None:
    for step in range(count):
        crew_log_emit.on_ledger_recorded(
            unit_id, {"slot": SLOT, "event": f"e{step}", "event_kind": "progress"}
        )
        crew_log_emit.flush(timeout=5.0)


def _log_lines(unit_id: str) -> "list[bytes]":
    """*unit_id*'s log as raw lines, read as BYTES.

    Bytes throughout, here and in :func:`_put_log_lines`, because every test that
    plants damage in a log is testing a BYTE OFFSET. Text mode translates line endings
    on Windows, so a round-trip through it rewrites every separator in the file and
    moves the offsets the point under test names.
    """
    return _log(unit_id).read_bytes().rstrip(b"\n").split(b"\n")


def _put_log_lines(unit_id: str, lines: "list[bytes]") -> None:
    """Put *lines* back in *unit_id*'s log, separators byte-for-byte as given."""
    _log(unit_id).write_bytes(b"\n".join(lines) + b"\n")


def _counted_cut(unit_id: str) -> None:
    """Declare and settle a cut of *unit_id*'s log, the way the store does one.

    The counter is a seqlock, so a settled cut is two raises: to an odd number while
    the bytes move, and back to an even one afterwards. A test that raised it once
    would leave the unit reading ``None`` and pass for the wrong reason.
    """
    crew_log_store._open_cut(_dir(unit_id))
    crew_log_store._close_cut(_dir(unit_id))


def _offset_of(unit_id: str, seq: int) -> int:
    """Where the record carrying *seq* begins in *unit_id*'s log, counted off the bytes.

    Walked here rather than taken from the code under test, so a test that checks a
    reported offset is checking it against the file.
    """
    at = 0
    for line in _log(unit_id).read_bytes().split(b"\n"):
        stripped = line.strip()
        if stripped:
            try:
                parsed = json.loads(stripped)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("seq") == seq:
                return at
        at += len(line) + 1
    raise AssertionError(f"{unit_id}'s log has no record for seq {seq}")


def _record_at(unit_id: str, offset: int) -> "dict[str, Any]":
    return json.loads(_log(unit_id).read_bytes()[offset:].split(b"\n")[0])


def _full_walk(unit_id: str) -> "list[int]":
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    return [entry.seq for entry in handle.iter_from(1)]


def _resumed_walk(unit_id: str, point: "ResumePoint | None", seq: int) -> "list[int]":
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    return [entry.seq for entry in handle.iter_from(seq, resume=point)]


def _walk_to_cursor(unit_id: str, seq: int = 1, **kwargs: Any) -> ReadCursor:
    cursor = ReadCursor()
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    for _entry in handle.iter_from(seq, cursor=cursor, **kwargs):
        pass
    return cursor


class _Parses:
    """How many raw records the block inside it JSON-decoded.

    Its own :class:`pytest.MonkeyPatch` rather than the shared fixture's: undoing that
    one would also undo the autouse fixture's data home.
    """

    def __init__(self) -> None:
        self.count = 0
        self._patch = pytest.MonkeyPatch()

    def __enter__(self) -> "_Parses":
        real = crew_log_store._parses_to_object

        def _counted(blob):
            self.count += 1
            return real(blob)

        self._patch.setattr(crew_log_store, "_parses_to_object", _counted)
        return self

    def __exit__(self, *_exc) -> None:
        self._patch.undo()


# --------------------------------------------------------------------------- #
# reporting a point
# --------------------------------------------------------------------------- #


def test_a_walk_reports_where_the_last_entry_it_yielded_begins():
    """The cursor names a segment, a byte offset and a seq, and the file agrees.

    Checked against the raw bytes rather than against another call into the same code:
    an offset is only useful if it indexes the file, so the file is the authority.
    """
    _unit(UNIT)
    _entries(UNIT, 6)
    top = CrewLog.open(lg.KIND_SESSION, UNIT).last_seq

    point = _walk_to_cursor(UNIT).at

    assert point is not None
    assert point.seq == top
    assert point.segment == lg.LOG_FILE
    assert point.offset == _offset_of(UNIT, top)
    assert point.cuts == crew_log_store.read_cuts(_dir(UNIT))
    assert _record_at(UNIT, point.offset)["seq"] == top


def test_a_point_never_narrows_what_the_walk_answers():
    """The same seq yields the same entries whether a point is passed or not.

    This is the whole promise of the token: it changes how MUCH is read, never WHAT
    comes back. The way to break that promise is not damage, it is an ordinary caller
    asking from a seq BELOW the point -- the point verifies against the bytes
    perfectly, and opening there would silently drop every record in between.

    Checked across the range rather than at one value, because the interesting seqs are
    exactly the ones either side of the point's own.
    """
    _unit(UNIT)
    _entries(UNIT, 6)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    top = CrewLog.open(lg.KIND_SESSION, UNIT).last_seq
    assert point.seq == top, "the point should name the last entry the walk yielded"

    for seq in range(1, top + 2):
        with_point = _resumed_walk(UNIT, point, seq)
        without = [entry.seq for entry in CrewLog.open(lg.KIND_SESSION, UNIT).iter_from(seq)]
        assert with_point == without, (
            f"iter_from({seq}) answered {with_point} with the point and {without} "
            "without it; a resume point must not change the answer"
        )


def test_a_walk_that_yielded_nothing_reports_no_point():
    """Nothing new means the caller keeps the point it already has.

    A walk that reported the position it RESUMED from would be correct, and reporting
    nothing is better: the distinction a caller needs is "is this a NEW position", and
    an empty cursor says no without the caller comparing anything.
    """
    _unit(UNIT)
    _entries(UNIT, 4)
    top = CrewLog.open(lg.KIND_SESSION, UNIT).last_seq

    assert _walk_to_cursor(UNIT, top + 1).at is None


def test_a_point_is_not_advanced_past_an_entry_the_walk_skipped():
    """An ignorable type the reader does not know is one it has not folded.

    So the point names the last entry YIELDED, not the last record framed: a point past
    the skipped record would hide it from every later read, and a later read is the only
    thing that could ever pick it up.
    """
    _unit(UNIT)
    _entries(UNIT, 3)
    CrewLog.open(lg.KIND_SESSION, UNIT).append_many(
        [{"type": "message/chunk", "data": {"turn": 1, "delta": "aa"}, "ignorable": True}],
        src="acp",
    )
    top = CrewLog.open(lg.KIND_SESSION, UNIT).last_seq

    point = _walk_to_cursor(UNIT, known={"ledger/recorded"}).at

    assert point is not None
    assert point.seq < top, "the point advanced onto a record the walk never yielded"
    assert _record_at(UNIT, point.offset)["type"] == "ledger/recorded"


def test_the_point_carries_the_cut_count_from_before_the_walk():
    """Sampled at the start, so a cut landing mid-walk falls towards a full walk.

    Stamping the count from AFTER the walk would hand out a point whose count matches
    the post-cut file, and the next read would resume over bytes the cut moved. Sampled
    first, the same race costs one reader one full walk.
    """
    _unit(UNIT)
    _entries(UNIT, 6)
    started = crew_log_store.read_cuts(_dir(UNIT))
    assert started is not None

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    cursor = ReadCursor()
    for index, _entry in enumerate(handle.iter_from(1, cursor=cursor)):
        if index == 2:
            _counted_cut(UNIT)

    # Two raises per settled cut: the counter is a seqlock.
    assert crew_log_store.read_cuts(_dir(UNIT)) == started + 2
    assert cursor.at is not None
    assert cursor.at.cuts == started, "the point was stamped with the count from after the cut"
    assert _resumed_walk(UNIT, cursor.at, 1) == _full_walk(UNIT)


def test_a_located_walk_reports_offsets_the_file_agrees_with():
    """EVERY offset the walk reports is where that record's bytes begin.

    Checked for every record rather than the last, because one wrong offset is enough
    to aim a resume inside a record -- and the boundary check is the thing those
    offsets must not have to rely on.
    """
    _unit(UNIT)
    _entries(UNIT, 10)
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)

    located = list(handle._iter_segments_located())
    assert len(located) == handle.last_seq
    for entry, segment, at in located:
        assert segment == lg.LOG_FILE
        there = Entry.from_dict(_record_at(UNIT, at))
        assert there is not None, f"offset {at} does not begin an entry record"
        assert there.seq == entry.seq


# --------------------------------------------------------------------------- #
# resuming at one
# --------------------------------------------------------------------------- #


def test_a_resumed_walk_yields_exactly_what_a_full_walk_yields():
    """The whole point: the same entries, from a different starting byte."""
    _unit(UNIT)
    _entries(UNIT, 20)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    _entries(UNIT, 5)

    full = [seq for seq in _full_walk(UNIT) if seq > point.seq]
    assert full, "nothing was appended after the point, so this proves nothing"
    assert _resumed_walk(UNIT, point, point.seq + 1) == full


def test_a_resumed_walk_decodes_the_tail_and_not_the_prefix():
    """Measured at the store, with no fold in the way.

    Forty records below the point and three above it. A resumed walk decodes the record
    at the offset -- it has to, that is the check -- plus the tail, plus the segment's
    own header and first line. Not forty.
    """
    _unit(UNIT)
    _entries(UNIT, 40)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    _entries(UNIT, 3)

    with _Parses() as seen:
        resumed = _resumed_walk(UNIT, point, point.seq + 1)

    assert len(resumed) == 3, resumed
    assert seen.count <= 10, f"the resumed walk decoded {seen.count} records for 3 entries"


def test_a_resumed_walk_still_refuses_a_non_advancing_seq_above_the_offset():
    """The guarantee is kept for every record the walk DOES read.

    The record at the offset is what anchors it: the resumed walk's first record is
    that known one, so a duplicate immediately after it is still compared against a
    predecessor and still refused.
    """
    _unit(UNIT)
    _entries(UNIT, 5)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    _entries(UNIT, 2)

    lines = _log_lines(UNIT)
    last = json.loads(lines[-1])
    last["seq"] = json.loads(lines[-2])["seq"]
    lines[-1] = json.dumps(last, separators=(",", ":")).encode()
    _put_log_lines(UNIT, lines)

    with pytest.raises(crew_log_store.CrewLogError) as refusal:
        _resumed_walk(UNIT, point, point.seq + 1)
    assert refusal.value.code == crew_log_store.CODE_BAD_DATA


# --------------------------------------------------------------------------- #
# every refusal: the point is declined and the full walk answers
# --------------------------------------------------------------------------- #


def test_a_point_whose_cut_count_moved_is_refused():
    """A cut is the one thing that can move a record below the offset.

    So the count is the only evidence there is for the prefix a resumed walk never
    reads, and a count that moved is not a slow case to re-check but a decided one.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    _counted_cut(UNIT)

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    assert handle._resume_at(
        crew_log_store.segment_paths(lg.KIND_SESSION, UNIT),
        point,
        None,
        crew_log_store.read_cuts(_dir(UNIT)),
    ) == (0, 0)
    assert _resumed_walk(UNIT, point, 1) == _full_walk(UNIT)


def test_a_point_with_no_cut_count_is_refused():
    """Absent is NOT PROVEN, never zero.

    A log written before the counter existed has no evidence about its prefix at all,
    so it takes the full walk -- which is what it already paid, not a regression.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    (_dir(UNIT) / crew_log_store._CUTS_FILE).unlink()
    assert crew_log_store.read_cuts(_dir(UNIT)) is None

    for candidate in (point, replace(point, cuts=None)):
        assert _resumed_walk(UNIT, candidate, 1) == _full_walk(UNIT)


def test_a_point_whose_cut_count_is_unreadable_is_refused():
    """Unreadable is the third face of NOT PROVEN, and it is the untrusting one.

    Absent is an ordinary log that has never been cut; unreadable is a counter that was
    there and is now damaged -- a torn write, a hand-edit. The resume must decline on
    both, and the one that matters is this one: the value it cannot parse may be the
    very value the point holds, so reading it optimistically is how a point survives a
    cut it should not have survived.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    # An EMPTY file is the shape the store itself produces: retiring the counter
    # truncates it to zero bytes before publishing or unlinking, so a crash in that
    # window leaves exactly this. The whitespace-only cases are the same window with a
    # separator already written. A real `0` is NOT here -- that is a proven zero.
    for damaged in (b"not a number\n", b"", b"\n", b"   \n", b"12x\n", b"\xff\xfe\n", b"-4\n"):
        (_dir(UNIT) / crew_log_store._CUTS_FILE).write_bytes(damaged)
        assert crew_log_store.read_cuts(_dir(UNIT)) is None, f"{damaged!r} read as a count"
        assert _resumed_walk(UNIT, point, 1) == _full_walk(UNIT)

    # The control: the same file holding a real count is not refused, so the loop above
    # is a property of these values and not of the fixture having broken the unit.
    (_dir(UNIT) / crew_log_store._CUTS_FILE).write_bytes(f"{point.cuts}\n".encode())
    assert crew_log_store.read_cuts(_dir(UNIT)) == point.cuts
    assert _resumed_walk(UNIT, point, point.seq) == [point.seq]


def test_a_point_read_while_a_cut_is_in_flight_is_refused():
    """An odd counter is a cut happening right now, and it proves nothing.

    The counter is a seqlock: it goes odd before the bytes move and even after. So the
    reading taken halfway through is the one case where an EQUAL comparison would be
    worst -- the reader would fold the records the cut is about to replace, find the
    same odd number afterwards because nothing raises it a third time, and continue
    over the rewritten bytes. ``read_cuts`` answering ``None`` is what stops it, and
    this pins that the resume declines on that ``None`` rather than on the number.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    assert point.cuts is not None and point.cuts % 2 == 0, "a settled count is even"

    crew_log_store._open_cut(_dir(UNIT))
    raw = (_dir(UNIT) / crew_log_store._CUTS_FILE).read_text().strip()
    assert int(raw) % 2 == 1, "a cut in flight leaves an odd number on disk"
    assert crew_log_store.read_cuts(_dir(UNIT)) is None, "and it is reported as unproven"

    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    assert handle._resume_at(
        crew_log_store.segment_paths(lg.KIND_SESSION, UNIT),
        point,
        None,
        crew_log_store.read_cuts(_dir(UNIT)),
    ) == (0, 0)
    assert _resumed_walk(UNIT, point, 1) == _full_walk(UNIT)

    # A point STAMPED mid-cut carries None itself, and is declined for the same reason
    # from the other side: there is nothing to compare a later reading against.
    assert _walk_to_cursor(UNIT).at is not None
    assert _walk_to_cursor(UNIT).at.cuts is None  # type: ignore[union-attr]
    assert _resumed_walk(UNIT, _walk_to_cursor(UNIT).at, 1) == _full_walk(UNIT)

    # Settling the cut leaves a different even number, so neither point resumes.
    crew_log_store._close_cut(_dir(UNIT))
    settled = crew_log_store.read_cuts(_dir(UNIT))
    assert settled is not None and settled != point.cuts
    assert _resumed_walk(UNIT, point, 1) == _full_walk(UNIT)


def test_an_offset_inside_a_record_is_refused():
    """An offset must be a record BOUNDARY, and reading there is what settles it.

    Land mid-record and the framing layer delivers that record's remainder, which does
    not parse -- so the first entry found begins ABOVE the offset, and the walk
    declines rather than accepting a position it cannot place.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    assert _resumed_walk(UNIT, replace(point, offset=point.offset + 12), 1) == _full_walk(UNIT)


def test_an_offset_whose_record_carries_another_seq_is_refused():
    """The positive check: the bytes there must be the bytes the caller thinks.

    A boundary alone is not enough. This offset is a perfectly good record start -- it
    is just a different record -- and only comparing the seq catches it.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    elsewhere = replace(point, offset=_offset_of(UNIT, point.seq - 2))
    assert _resumed_walk(UNIT, elsewhere, 1) == _full_walk(UNIT)


def test_an_offset_past_the_durable_end_is_refused():
    """Bytes past the durable end are an append still in flight."""
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    past = replace(point, offset=_log(UNIT).stat().st_size + 1)
    assert _resumed_walk(UNIT, past, 1) == _full_walk(UNIT)


def test_a_point_naming_a_segment_that_is_not_there_is_refused():
    """A byte offset means nothing without the file it indexes.

    Retention removes whole segments off the front, so a point into one of them is
    stale rather than wrong, and stale means walk from the top.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    assert _resumed_walk(UNIT, replace(point, segment="log.9999.jsonl"), 1) == _full_walk(UNIT)


@pytest.mark.parametrize("offset", [0, -1, True])
def test_a_nonsense_offset_is_refused(offset):
    """Offset zero is the header's own position, so it names no entry at all."""
    _unit(UNIT)
    _entries(UNIT, 6)
    point = _walk_to_cursor(UNIT).at
    assert point is not None

    assert _resumed_walk(UNIT, replace(point, offset=offset), 1) == _full_walk(UNIT)


# --------------------------------------------------------------------------- #
# the fold's own pairing check, and the trade
# --------------------------------------------------------------------------- #


def test_a_memo_point_ahead_of_what_the_cell_folded_is_not_used():
    """A point above what the cell folded would skip entries it never consumed.

    The store cannot catch this one: an offset that is a good boundary for the seq it
    names is exactly what the store checks for, and this is one. So the fold checks the
    pairing itself, and anything but equality walks from the top.
    """
    _unit(UNIT)
    _entries(UNIT, 10)
    crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)

    key = (str(crew_log.data_home()), SLOT, "ledger")
    memo = crew_log._slot_memos[key]
    assert memo.resume is not None
    assert memo.resume.seq == memo.reached
    assert crew_log._resume_from(memo) is memo.resume

    ahead = replace(memo.resume, seq=memo.resume.seq + 1)
    assert crew_log._resume_from(replace(memo, resume=ahead)) is None
    assert crew_log._resume_from(replace(memo, resume=None)) is None


def test_a_duplicate_seq_below_the_offset_does_not_refuse_a_resumed_walk():
    """The trade, stated as a test rather than only in a design note.

    A duplicate seq planted BELOW the offset is damage the full walk refuses with
    ``bad_data``. The resumed walk never reads those records, so it does not -- and the
    cut counter cannot help, because a hand-edit is not a cut. This is the corruption
    the skipped prefix stops catching, pinned here so a later reader of this code finds
    it named rather than discovers it.
    """
    _unit(UNIT)
    _entries(UNIT, 8)
    point = _walk_to_cursor(UNIT).at
    assert point is not None
    _entries(UNIT, 2)

    lines = _log_lines(UNIT)
    victim = json.loads(lines[3])
    victim["seq"] = json.loads(lines[2])["seq"]
    lines[3] = json.dumps(victim, separators=(",", ":")).encode()
    _put_log_lines(UNIT, lines)

    with pytest.raises(crew_log_store.CrewLogError):
        _full_walk(UNIT)
    assert _resumed_walk(UNIT, point, point.seq + 1), "the resumed walk found no tail"


def test_a_pass_that_writes_a_savepoint_keeps_its_resume_point():
    """A warm pass brings the on-disk savepoint forward and REPLACES its cell.

    The replacement has to carry the offset. One that dropped it would leave the read
    after every savepoint write decoding the whole log, and neither the savepoint's own
    tests nor the ones above would say so: the savepoint half asserts the value, and the
    offset half never earns a write at the sizes it works at.
    """
    from kiro_crew.crew_log import checkpoint as savepoints

    _unit(UNIT)
    _entries(UNIT, 4)
    crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)
    key = (str(crew_log.data_home()), SLOT, "ledger")
    before = crew_log._slot_memos[key]

    _entries(UNIT, savepoints.MIN_ADVANCE_ENTRIES + 1)
    crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)
    after = crew_log._slot_memos[key]

    assert after.saved > before.saved, "the pass did not earn a savepoint write"
    assert after.resume is not None
    assert after.resume.seq == after.reached

    _entries(UNIT, 1)
    with _Parses() as seen:
        crew_log.fold_slot_warm("ledger", (UNIT,), slot=SLOT)
    assert after.saved > savepoints.MIN_ADVANCE_ENTRIES
    assert seen.count <= 20, f"the read after a savepoint write decoded {seen.count} records"
