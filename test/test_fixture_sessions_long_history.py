"""Consumer coverage for the sessions-long-history seed fixture.

The fixture exists so a paging trace can be performed through the real routes:
its session's oldest rows sit below a size-rotation boundary, which is the only
state in which the slot-detail fast path answers ``has_more=true``. These tests
pin that design point, and pin the shipped archive segment against what
``_maybe_rotate`` writes today, so the fixture cannot silently drift from the
archive shape the rotation writer actually produces.
"""

from __future__ import annotations

import json
from pathlib import Path

from kiro_crew.dashboard.chat_utils import _collapse_wire_rows
from kiro_crew.history import ConversationLog
from kiro_crew.testing.fixtures import seeded_home

KEY = "dashboard_long-history"
ROTATED_ROWS = 60
LIVE_ROWS = 8


def _segments(sessions: Path) -> list[Path]:
    return sorted((sessions / "archive").glob(f"{KEY}__*.jsonl"))


def _segment(sessions: Path) -> Path:
    segments = _segments(sessions)
    assert len(segments) == 1, f"expected one archive segment, found {segments}"
    return segments[0]


def test_sessions_long_history_advertises_a_reachable_earlier_page() -> None:
    """The real readers see a rotated head the slot-detail cursor can reach."""
    with seeded_home("sessions-long-history") as home:
        sessions = home / "sessions"
        log = ConversationLog(base_dir=sessions)

        assert [session["key"] for session in log.list_sessions()] == [KEY]

        live = log.read_messages(KEY)
        rotated = log.read_rotated_messages_chained(KEY)
        full = log.read_messages_chained_full(KEY)
        assert len(live) == LIVE_ROWS
        assert len(rotated) == ROTATED_ROWS

        # The archived rows are a contiguous PREFIX of the paginated corpus, which
        # is what makes a single `next_before` cursor exact. `chain_mid_rotation`
        # says the same thing from the handler's side.
        assert full == rotated + live
        assert log.chain_mid_rotation(KEY) is False

        # The two numbers the slot-detail fast path derives: rotated rows exist,
        # so has_more is true, and next_before is their COLLAPSED count.
        assert _collapse_wire_rows(rotated) == rotated
        assert len(_collapse_wire_rows(rotated)) == ROTATED_ROWS

        # Paging with that cursor must land inside the archive rather than
        # re-serving the live tail.
        assert full[:ROTATED_ROWS] == rotated
        assert all(row["role"] in {"user", "assistant"} for row in rotated)

        metadata = log.get_metadata(KEY)
        assert metadata["rotation_generation"] == 1
        assert metadata["rotated_at"]
        assert metadata["title"]
        # Pinned, so the session restores as a slot whatever the mtime restore
        # window is when the home is seeded. Without it the scenario only
        # produces a slot for a pod seeded inside that window.
        assert metadata["pinned"] is True


def test_the_shipped_segment_matches_what_rotation_writes_today(tmp_path, monkeypatch) -> None:
    """The fixture's archive segment is real writer output, not authored bytes."""
    monkeypatch.setattr("kiro_crew.history._SESSION_MAX_BYTES", 400)
    monkeypatch.setattr("kiro_crew.history._SESSION_KEEP_LINES", 3)
    fresh = ConversationLog(base_dir=tmp_path)
    for index in range(20):
        fresh.append(KEY, "user", f"row {index:02d} with enough text to outgrow the budget")
    segments = _segments(tmp_path)
    assert segments, "the reference log must rotate"
    produced_lines = segments[0].read_text(encoding="utf-8").splitlines()
    produced_header = json.loads(produced_lines[0])
    produced_row = json.loads(produced_lines[1])

    with seeded_home("sessions-long-history") as home:
        shipped_lines = _segment(home / "sessions").read_text(encoding="utf-8").splitlines()
    shipped_header = json.loads(shipped_lines[0])
    shipped_row = json.loads(shipped_lines[1])

    assert set(shipped_header) == set(produced_header)
    assert shipped_header["_type"] == produced_header["_type"] == "archive"
    assert shipped_header["reason"] == produced_header["reason"] == "rotate"
    assert shipped_header["count"] == len(shipped_lines) - 1 == ROTATED_ROWS
    assert set(shipped_row) == set(produced_row)
