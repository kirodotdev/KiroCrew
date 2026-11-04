"""Every reader of ``last_consolidated`` reads it as a row count.

The metadata line is caller-supplied JSON. A negative value used as a slice
start counts from the END of the transcript, so the rows before it are skipped
and never consolidated; a non-integer one raises in the slice or subtraction.
Each reader treats an unreadable or negative value as 0, as
``consolidated_offset`` does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew.history import ConversationLog


@pytest.fixture
def log(tmp_path: Path) -> ConversationLog:
    log = ConversationLog(base_dir=tmp_path)
    for i in range(5):
        log.append("k", "user", f"row {i}")
    return log


@pytest.mark.parametrize("stored", [-2, "3", 3.0, None, "x", True], ids=repr)
def test_an_unusable_offset_reads_the_whole_transcript_or_its_integer(
    log: ConversationLog, stored: object
) -> None:
    log.update_metadata("k", {"last_consolidated": stored})
    expected = log.consolidated_offset("k")
    assert 0 <= expected <= 5

    rows, total = log.get_unconsolidated("k")
    assert (len(rows), total) == (5 - expected, 5)
    assert log.unconsolidated_count("k") == 5 - expected
    assert len(log.snapshot_for_consolidation("k")[0]) == 5 - expected


def test_a_negative_offset_reads_as_zero(log: ConversationLog) -> None:
    log.update_metadata("k", {"last_consolidated": -2})
    assert log.consolidated_offset("k") == 0
    assert log.unconsolidated_count("k") == 5
    assert len(log.snapshot_for_consolidation("k")[0]) == 5


def test_a_valid_offset_is_unchanged(log: ConversationLog) -> None:
    log.update_metadata("k", {"last_consolidated": 3})
    rows, total = log.get_unconsolidated("k")
    assert [r["content"] for r in rows] == ["row 3", "row 4"] and total == 5
    assert log.unconsolidated_count("k") == 2


def test_a_repaired_marker_drops_the_stored_sub_offset(log: ConversationLog) -> None:
    """The sub-offset is a position in the row at the old marker, not the head."""
    generation = log.rotation_generation("k")
    log.update_metadata(
        "k",
        {
            "last_consolidated": -2,
            "consolidation_sub_offset": 3,
            "consolidation_sub_offset_generation": generation,
        },
    )
    rows, _total, _gen, sub_offset = log.snapshot_for_consolidation("k")
    assert len(rows) == 5
    assert sub_offset == 0


def test_a_valid_marker_keeps_its_sub_offset(log: ConversationLog) -> None:
    generation = log.rotation_generation("k")
    log.update_metadata(
        "k",
        {
            "last_consolidated": 2,
            "consolidation_sub_offset": 3,
            "consolidation_sub_offset_generation": generation,
        },
    )
    assert log.snapshot_for_consolidation("k")[3] == 3


@pytest.mark.parametrize("stored", [-2, "x"], ids=repr)
def test_slice_progress_on_a_repaired_marker_is_kept(log: ConversationLog, stored: object) -> None:
    """The first slice's progress saves the repaired marker, so the next pass resumes."""
    log.update_metadata("k", {"last_consolidated": stored})
    _rows, _total, generation, sub_offset = log.snapshot_for_consolidation("k")
    assert sub_offset == 0

    log.advance_consolidation_sub_offset("k", 0, 7, generation)

    assert log.get_metadata("k")["last_consolidated"] == 0
    assert log.snapshot_for_consolidation("k")[3] == 7


@pytest.mark.parametrize("stored", [-2, "x"], ids=repr)
def test_failures_on_a_repaired_marker_count_toward_the_cap(
    log: ConversationLog, stored: object
) -> None:
    """A charge taken from the repaired snapshot describes the span still in front of us."""
    from kiro_crew.history_consolidation_span import AttemptedSpan

    generation = log.rotation_generation("k")
    log.update_metadata(
        "k",
        {
            "last_consolidated": stored,
            "consolidation_sub_offset": 3,
            "consolidation_sub_offset_generation": generation,
        },
    )
    rows, total, gen, sub_offset = log.snapshot_for_consolidation("k")
    span = AttemptedSpan(
        total=total, generation=gen, offset=0, prompted=0, sub_offset=sub_offset, sub_prompted=4
    )

    counts = [log.record_consolidation_failure("k", 900.0, 86400.0, span, now=0.0)[0]]
    counts.append(log.record_consolidation_failure("k", 900.0, 86400.0, span, now=1.0)[0])

    assert counts == [1, 2]
