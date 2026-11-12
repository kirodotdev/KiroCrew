"""The rendering reads seek by seq: get, page and read_page decode a span, not the prefix.

Each read is checked twice: what it decoded (the read budget's own count, on a gateway
thread) and that its answer equals the full walk's, which is what the seek replaced.
The full walk is the same reader with the seek turned off (``_offset_below`` answering 0,
its own "read from the top"), so the comparison is reader against reader.
"""

from __future__ import annotations

import json

import pytest
from crew_log_core_helpers import _split_into_segments

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog, checkpoint
from kiro_crew.crew_log import eager as crew_log_eager
from kiro_crew.crew_log import read as crew_read
from kiro_crew.crew_log import read_budget
from kiro_crew.crew_log import store as crew_log_store
from kiro_crew.crew_log.errors import CrewLogError

UNIT = "acp-seek"
BIG = 8 * read_budget.K


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    _floor_monkeypatch.delenv(read_budget.MODE_ENV, raising=False)
    _floor_monkeypatch.setattr(crew_log_eager, "note_commit", lambda *a, **k: None)
    read_budget.reset_for_tests()
    yield
    read_budget.reset_for_tests()


def _unit(records: int = BIG) -> None:
    handle = CrewLog.create(lg.KIND_SESSION, UNIT, owner="o", agent="a", slot="s")
    for at in range(0, records, 1000):
        batch = range(at, min(records, at + 1000))
        handle.append_many(
            [{"type": "ledger/recorded", "data": {"slot": "s", "goal": f"g{i}"}} for i in batch],
            src="gateway",
        )
    handle.release_ownership()


def _rewrite(keep) -> None:
    """Rewrite the unit's entry lines through *keep* (raw line -> list of lines)."""
    path = lg.crew_log_path(lg.KIND_SESSION, UNIT)
    lines = path.read_bytes().splitlines(keepends=True)
    out = [lines[0]]
    for raw in lines[1:]:
        out.extend(keep(raw))
    path.write_bytes(b"".join(out))


@pytest.fixture
def decoded(monkeypatch):
    """The records each settled read decoded, newest last."""
    seen: "list[int]" = []
    real = read_budget._settle

    def spy(tally: read_budget.Tally) -> None:
        seen.append(tally.records)
        real(tally)

    monkeypatch.setattr(read_budget, "_settle", spy)
    read_budget.mark_gateway_process()
    return seen


def _full_walk(monkeypatch, read):
    """*read*'s answer with every seek and the read_page tail shortcut turned off."""
    with monkeypatch.context() as patch:
        patch.setattr(crew_log_store, "_offset_below", lambda *_a: 0, raising=False)
        patch.setattr(CrewLog, "tail_records", lambda self, count: ())
        return read()


def _get(seq: int):
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    try:
        found = handle.get(seq)
        return None if found is None else found.to_dict()
    finally:
        handle.release_ownership()


def _page(before, limit=50):
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    try:
        page = handle.page(before, limit)
        return [e.to_dict() for e in page.entries], page.next_before
    finally:
        handle.release_ownership()


def test_get_near_the_end_decodes_a_span_and_answers_as_the_full_walk(decoded, monkeypatch):
    _unit()
    for seq in (BIG, BIG - 1, BIG // 2, 2, 1, BIG + 1):
        expected = _full_walk(monkeypatch, lambda s=seq: _get(s))
        del decoded[:]
        assert _get(seq) == expected
        assert decoded[-1] <= read_budget.K + 1, (seq, decoded)
    assert _get(BIG)["seq"] == BIG and _get(BIG + 1) is None


def test_page_from_the_end_decodes_a_span_and_answers_as_the_full_walk(decoded, monkeypatch):
    _unit()
    for before in (None, BIG // 2, 60, 2):
        expected = _full_walk(monkeypatch, lambda b=before: _page(b))
        del decoded[:]
        assert _page(before) == expected, before
        assert decoded[-1] <= 51 + read_budget.K, (before, decoded)
    assert _page(None)[0][0]["seq"] == BIG and _page(None)[1] == BIG - 49


def test_read_page_reads_its_range_and_still_reports_the_true_last_seq(decoded, monkeypatch):
    _unit()
    for start, end in ((BIG - 50, BIG), (1, 50), (BIG // 2, BIG // 2 + 10)):
        expected = _full_walk(monkeypatch, lambda s=start, e=end: crew_read.read_page(UNIT, s, e))
        del decoded[:]
        got = crew_read.read_page(UNIT, start, end)
        assert json.dumps(got, sort_keys=True) == json.dumps(expected, sort_keys=True)
        assert got["last_seq"] == BIG
        assert max(decoded) <= 51 + read_budget.K, ((start, end), decoded)


def test_a_damaged_line_under_a_probe_only_starts_the_walk_earlier(decoded, monkeypatch):
    _unit()

    def tear(raw: bytes) -> "list[bytes]":
        seq = json.loads(raw)["seq"]
        return [raw[: len(raw) // 2] + b"\n"] if seq % 7 == 0 else [raw]

    _rewrite(tear)
    for read in (lambda: _get(BIG - 3), lambda: _page(None), lambda: _page(BIG // 2)):
        expected = _full_walk(monkeypatch, read)
        del decoded[:]
        assert read() == expected
        assert max(decoded) <= 51 + read_budget.K, decoded


def test_a_seq_gap_below_the_page_falls_back_to_the_full_walk(decoded, monkeypatch):
    """A gap leaves the seeked window short, which proves nothing: read from the top.

    The span is one byte so the seek lands on the last record below the gap, exactly.
    """

    monkeypatch.setattr(crew_log_store, "_SEEK_SPAN", 1, raising=False)
    _unit()
    _rewrite(lambda raw: [] if BIG - 1500 < json.loads(raw)["seq"] < BIG - 30 else [raw])
    expected = _full_walk(monkeypatch, lambda: _page(None))
    del decoded[:]
    assert _page(None) == expected
    assert len(expected[0]) == 50 and expected[0][-1]["seq"] < BIG - 1500
    assert len(decoded) == 2 and decoded[0] < 50 and decoded[-1] == BIG - 1469


def test_a_unit_of_two_segments_reads_from_the_top(decoded, monkeypatch):
    _unit()
    _split_into_segments(lg.KIND_SESSION, UNIT, BIG // 2)
    for read in (lambda: _get(BIG - 3), lambda: _page(None)):
        expected = _full_walk(monkeypatch, read)
        del decoded[:]
        assert read() == expected
        assert decoded[-1] >= BIG // 2, "no seek across segments"


def test_a_digest_witness_hashes_in_the_walk_that_finds_its_boundary(monkeypatch):
    _unit()
    (lg.crew_log_path(lg.KIND_SESSION, UNIT).parent / crew_log_store._CUTS_FILE).unlink()
    handle = CrewLog.open(lg.KIND_SESSION, UNIT)
    digests: "list[int]" = []
    real = CrewLog.raw_prefix_digest

    def counting(self, records):
        digests.append(records)
        return real(self, records)

    monkeypatch.setattr(CrewLog, "raw_prefix_digest", counting)
    witness = checkpoint.fold_witness(handle, BIG - 5)
    assert isinstance(witness, checkpoint.PrefixWitness) and digests == []
    assert (witness.sha, witness.records) == real(handle, witness.records)
    assert witness.records == BIG - 5
    assert checkpoint.witness_unchanged(handle, witness)
    handle.release_ownership()


def test_read_page_past_end_still_checks_a_later_segment_header():
    """A later segment with another unit's header is refused, never read for last_seq.

    The tail shortcut reads only the newest segment's last record, so on a unit of more
    segments it would report a foreign file's seq before the walk checked its header.
    """
    _unit()
    _split_into_segments(lg.KIND_SESSION, UNIT, BIG // 2)
    later = lg.crew_log_path(lg.KIND_SESSION, UNIT).parent / f"log.{BIG // 2}.jsonl"
    lines = later.read_bytes().splitlines(keepends=True)
    header = json.loads(lines[0])
    header["id"] = "acp-foreign"
    later.write_bytes(json.dumps(header).encode() + b"\n" + b"".join(lines[1:]))
    with pytest.raises(CrewLogError):
        crew_read.read_page(UNIT, 10, 20)


def test_a_probe_inside_an_over_cap_line_does_not_move_the_seek_past_its_seq():
    """A probe whose discard read stops inside an over-cap line has no record boundary.

    The over-cap line sits after seq 1500 and ends in a copy of record 1, placed so the
    first probe's capped discard read stops exactly where that copy begins. Read as a
    record, the copy says "below seq 1000" and would start the walk past seq 1000.
    """
    from kiro_crew.crew_log.schema import MAX_ENTRY_BYTES

    _unit(4000)
    path = lg.crew_log_path(lg.KIND_SESSION, UNIT)
    lines = path.read_bytes().splitlines(keepends=True)
    header, entries = lines[0], lines[1:]
    record_one = entries[0]
    before = header + b"".join(entries[:1500])
    start = len(before)  # where the over-cap line begins
    mid = start + 1
    junk = b"x" * (mid + MAX_ENTRY_BYTES + 1 - start)
    path.write_bytes(before + junk + record_one + b"".join(entries[1500:]))
    end = 2 * mid - len(header)  # so the first probe lands at *mid*
    assert end <= path.stat().st_size
    offset = crew_log_store._offset_below(path, 1000, end)
    target = len(header) + sum(len(raw) for raw in entries[:999])  # record 1000 starts here
    assert offset <= target, (offset, target)
