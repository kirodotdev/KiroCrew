"""A turn cut short more than a week ago is not offered by the bulk Resume.

``is_interruption_stale`` reads the newest durable row's stamp. The slots
projection reports it as ``interrupted_stale`` beside ``interrupted``, and
``collect_interrupted_idle`` leaves such a slot out of a bulk press, while the
slot itself stays ``interrupted`` for its own Resume button.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_resume_all as resume_all
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.dashboard.state_parts.transcript_rows import (
    INTERRUPTED_BULK_RESUME_MAX_AGE_SECS,
    is_interruption_stale,
)

NOW = 1_800_000_000.0


def _stamp(seconds_before_now: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_before_now, timezone.utc).isoformat()


def _ago(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _slot(*messages: dict) -> _ChatSlot:
    slot = _ChatSlot("test-slot")
    slot.messages.extend(messages)
    return slot


class TestIsInterruptionStale:
    def test_a_row_older_than_the_window_is_stale(self):
        rows = [
            {
                "role": "user",
                "content": "go",
                "ts": _stamp(INTERRUPTED_BULK_RESUME_MAX_AGE_SECS + 1),
            }
        ]

        assert is_interruption_stale(rows, NOW) is True

    def test_a_row_exactly_at_the_window_is_not_stale(self):
        rows = [
            {"role": "user", "content": "go", "ts": _stamp(INTERRUPTED_BULK_RESUME_MAX_AGE_SECS)}
        ]

        assert is_interruption_stale(rows, NOW) is False

    def test_a_recent_row_is_not_stale(self):
        rows = [{"role": "user", "content": "go", "ts": _stamp(60)}]

        assert is_interruption_stale(rows, NOW) is False

    def test_the_newest_row_decides_not_the_oldest(self):
        rows = [
            {"role": "user", "content": "old", "ts": _stamp(90 * 24 * 3600)},
            {"role": "user", "content": "new", "ts": _stamp(60)},
        ]

        assert is_interruption_stale(rows, NOW) is False

    @pytest.mark.parametrize("ts", ["", "not-a-time", None])
    def test_an_unreadable_stamp_is_never_stale(self, ts):
        assert is_interruption_stale([{"role": "user", "content": "go", "ts": ts}], NOW) is False

    def test_a_missing_stamp_is_never_stale(self):
        assert is_interruption_stale([{"role": "user", "content": "go"}], NOW) is False

    def test_a_transient_row_is_skipped(self):
        rows = [
            {
                "role": "user",
                "content": "go",
                "ts": _stamp(INTERRUPTED_BULK_RESUME_MAX_AGE_SECS + 1),
            },
            {"role": "chunk", "content": "partial", "ts": _stamp(1)},
        ]

        assert is_interruption_stale(rows, NOW) is True

    def test_an_empty_transcript_is_not_stale(self):
        assert is_interruption_stale([], NOW) is False


class TestSlotRow:
    def test_an_old_unanswered_turn_is_interrupted_and_stale(self):
        row = _slot({"role": "user", "content": "go", "ts": _ago(30 * 24 * 3600)}).to_dict()

        assert row["interrupted"] is True
        assert row["interrupted_stale"] is True

    def test_a_recent_unanswered_turn_is_interrupted_and_not_stale(self):
        row = _slot({"role": "user", "content": "go", "ts": _ago(3600)}).to_dict()

        assert row["interrupted"] is True
        assert row["interrupted_stale"] is False

    def test_an_old_answered_turn_is_neither(self):
        ts = _ago(30 * 24 * 3600)
        row = _slot(
            {"role": "user", "content": "go", "ts": ts},
            {"role": "assistant", "content": "done", "ts": ts},
        ).to_dict()

        assert row["interrupted"] is False
        assert row["interrupted_stale"] is False


class TestCollectInterruptedIdle:
    def test_a_stale_slot_is_left_out_and_a_recent_one_stays(self, tmp_path):
        state = _make_state(tmp_path)
        old = state.get_or_create_slot("old")
        old.append("user", "go", "msg msg-u")
        old.messages[-1]["ts"] = _ago(30 * 24 * 3600)
        recent = state.get_or_create_slot("recent")
        recent.append("user", "go", "msg msg-u")

        picked = [name for name, _ in resume_all.collect_interrupted_idle(state)]

        assert picked == ["recent"]

    def test_the_left_out_slot_keeps_its_own_resume(self, tmp_path):
        state = _make_state(tmp_path)
        old = state.get_or_create_slot("old")
        old.append("user", "go", "msg msg-u")
        old.messages[-1]["ts"] = _ago(30 * 24 * 3600)

        (row,) = state.serialize_slots()

        assert row["interrupted"] is True
        assert row["interrupted_stale"] is True
