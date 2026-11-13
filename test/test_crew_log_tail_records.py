"""``CrewLog.tail_records``: the newest records and where each one begins.

A caller uses the offsets to prove what sits around a record it wrote, so the
tests check them against the bytes of the file, and check that an unsettled or
damaged tail answers short instead of guessing.
"""

from __future__ import annotations

import pytest

from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log.schema import KIND_MEMBER
from kiro_crew.eventlog import types
from kiro_crew.eventlog.log import MemberLog

RECORDS = 6


@pytest.fixture
def unit(tmp_path, monkeypatch):
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    log = MemberLog("tail")
    log.create("Tail")
    for i in range(1, RECORDS + 1):
        log.append(types.ACTIVITY_RECORD, {"ts": i, "via": "cli", "project": f"p{i}"})
    return log.path


def _tail(count: int):
    return CrewLog.open(KIND_MEMBER, "tail").tail_records(count)


def test_each_offset_is_where_that_record_begins(unit):
    data = unit.read_bytes()
    found = _tail(3)

    assert [entry.seq for entry, _ in found] == [RECORDS - 2, RECORDS - 1, RECORDS]
    lines = data.split(b"\n")
    for entry, begin in found:
        assert data[begin:].startswith(lines[entry.seq] + b"\n")
        assert begin == 0 or data[begin - 1 : begin] == b"\n"
    assert found[-1][1] + len(lines[RECORDS]) + 1 == len(data)


def test_the_answer_is_bounded_by_count_and_stops_at_the_header(unit):
    assert [entry.seq for entry, _ in _tail(1)] == [RECORDS]
    assert [entry.seq for entry, _ in _tail(100)] == list(range(1, RECORDS + 1))


def test_a_record_with_no_newline_is_not_settled(unit):
    unit.write_bytes(unit.read_bytes().rstrip(b"\n"))

    assert _tail(2) == ()


def test_a_damaged_line_ends_the_answer(unit):
    lines = unit.read_bytes().split(b"\n")
    lines[RECORDS - 1] = b"{not json"
    unit.write_bytes(b"\n".join(lines))

    assert [entry.seq for entry, _ in _tail(3)] == [RECORDS]
