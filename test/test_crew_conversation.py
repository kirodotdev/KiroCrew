"""Crew conversation index: pointers + escalation lifecycle, no bodies.

The index lives at ``$KIROCREW_HOME/members/<slug>/conversation.json`` (isolated
per test by the autouse ``_isolate_kirocrew_home`` fixture). These tests pin the
three properties the spec relies on: entries are pointers or escalation
records (never transcript bodies), ``needs_you`` is derived from pending
escalations and clears on reply or deadline, and the deadline window is
bounded.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from kiro_crew import crew_conversation as conv

SLUG = "radar"
NOW = datetime(2026, 9, 4, 22, 0, 0, tzinfo=timezone.utc)


class TestSchema:
    def test_missing_file_reads_as_empty_scaffold(self):
        record = conv.read_conversation(SLUG)
        assert record["conversation_id"] == "dm:radar"
        assert record["entries"] == []
        assert record["participants"] == []
        assert record["sessions"] == []

    def test_record_escalation_stores_a_pointer_not_a_body(self):
        entry = conv.record_escalation(
            SLUG,
            member="Radar",
            session_key="member-radar",
            mid="m-abc",
            escalation_id="esc-1",
            from_session="member-radar",
            deadline="2026-09-04T22:40:00Z",
            default_action="Push A",
            goal="nightly-triage",
            options=["Push A", "Hold"],
        )
        assert entry["type"] == "escalation"
        assert entry["state"] == "pending"
        assert "content" not in entry and "message" not in entry
        on_disk = json.loads(conv.conversation_path(SLUG).read_text())
        assert on_disk["entries"][0]["mid"] == "m-abc"
        assert on_disk["participants"] == [
            {"kind": "human", "id": "owner"},
            {"kind": "member", "slug": "radar", "name": "Radar"},
        ]
        assert on_disk["sessions"] == ["member-radar"]

    def test_append_ref_points_at_a_foreign_session_row(self):
        conv.append_ref(
            SLUG,
            member="Radar",
            session_key="chat-9",
            mid="m-9",
            role="assistant",
            ts="2026-09-04T21:00:00Z",
        )
        record = conv.read_conversation(SLUG)
        assert record["entries"] == [
            {
                "type": "ref",
                "session_key": "chat-9",
                "mid": "m-9",
                "role": "assistant",
                "ts": "2026-09-04T21:00:00Z",
            }
        ]
        assert record["sessions"] == ["chat-9"]

    def test_unreadable_file_reads_as_scaffold(self):
        path = conv.conversation_path(SLUG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json")
        conv.invalidate_cache()
        assert conv.read_conversation(SLUG)["entries"] == []
        assert conv.needs_you(SLUG) is False

    def test_malformed_field_types_do_not_crash_the_next_writer(self):
        path = conv.conversation_path(SLUG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": "1",
                    "conversation_id": 7,
                    "participants": None,
                    "sessions": "member-radar",
                    "entries": [{"type": "ref", "mid": "m-0"}, "garbage", 3],
                }
            )
        )
        entry = conv.record_escalation(
            SLUG,
            member="Radar",
            session_key="member-radar",
            mid="m-1",
            escalation_id="esc-1",
            from_session="member-radar",
        )
        record = conv.read_conversation(SLUG)
        assert entry in record["entries"]
        assert record["conversation_id"] == "dm:radar"
        assert record["sessions"] == ["member-radar"]
        assert record["participants"][0] == {"kind": "human", "id": "owner"}

    def test_cold_process_reads_false_until_primed(self):
        _pending(mid="m-1", eid="esc-1")
        conv.invalidate_cache()  # a fresh process has no in-memory view yet
        assert conv.needs_you(SLUG, now=NOW) is False
        conv.prime(SLUG)
        assert conv.needs_you(SLUG, now=NOW) is True

    def test_entries_are_capped(self):
        for i in range(conv._MAX_ENTRIES + 5):
            conv.append_ref(SLUG, member="Radar", session_key="chat-1", mid=f"m-{i}", role="user")
        assert len(conv.read_conversation(SLUG)["entries"]) == conv._MAX_ENTRIES

    def test_cap_never_evicts_a_pending_escalation(self):
        _pending(mid="m-esc", eid="esc-keep")
        for i in range(conv._MAX_ENTRIES + 5):
            conv.append_ref(SLUG, member="Radar", session_key="chat-1", mid=f"m-{i}", role="user")
        entries = conv.read_conversation(SLUG)["entries"]
        assert len(entries) == conv._MAX_ENTRIES
        assert entries[0]["type"] == "escalation" and entries[0]["id"] == "esc-keep"
        assert conv.needs_you(SLUG, now=NOW) is True

    def test_all_pending_exceeds_the_cap_rather_than_losing_a_decision(self):
        for i in range(conv._MAX_ENTRIES + 3):
            _pending(mid=f"m-{i}", eid=f"esc-{i}")
        entries = conv.read_conversation(SLUG)["entries"]
        assert len(entries) == conv._MAX_ENTRIES + 3
        assert all(e["state"] == "pending" for e in entries)

    def test_sessions_list_is_pruned_with_evicted_entries(self):
        for i in range(conv._MAX_ENTRIES + 5):
            conv.append_ref(
                SLUG, member="Radar", session_key=f"chat-{i}", mid=f"m-{i}", role="user"
            )
        record = conv.read_conversation(SLUG)
        assert len(record["sessions"]) == conv._MAX_ENTRIES
        assert "chat-0" not in record["sessions"]
        assert f"chat-{conv._MAX_ENTRIES + 4}" in record["sessions"]

    def test_concurrent_writers_do_not_lose_entries(self):
        import threading

        def _writer(n: int) -> None:
            for i in range(20):
                conv.append_ref(
                    SLUG, member="Radar", session_key=f"chat-{n}", mid=f"m-{n}-{i}", role="user"
                )

        threads = [threading.Thread(target=_writer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        entries = conv.read_conversation(SLUG)["entries"]
        assert len(entries) == 80
        assert len({e["mid"] for e in entries}) == 80

    def test_needs_you_cache_tracks_the_file(self):
        assert conv.needs_you(SLUG, now=NOW) is False
        _pending(mid="m-1", eid="esc-1")
        assert conv.needs_you(SLUG, now=NOW) is True
        conv.mark_answered(SLUG, now=NOW)
        assert conv.needs_you(SLUG, now=NOW) is False


def _pending(deadline=None, default_action=None, mid="m-1", eid="esc-1"):
    return conv.record_escalation(
        SLUG,
        member="Radar",
        session_key="member-radar",
        mid=mid,
        escalation_id=eid,
        from_session="member-radar",
        deadline=deadline,
        default_action=default_action,
    )


class TestNeedsYou:
    def test_pending_escalation_sets_needs_you(self):
        assert conv.needs_you(SLUG, now=NOW) is False
        _pending()
        assert conv.needs_you(SLUG, now=NOW) is True

    def test_free_text_reply_answers_the_only_pending_record(self):
        _pending(mid="m-1", eid="esc-1")
        assert conv.mark_answered(SLUG, now=NOW) == 1
        record = conv.read_conversation(SLUG)
        assert record["entries"][0]["state"] == "answered"
        assert record["entries"][0]["answered_ts"]
        assert conv.needs_you(SLUG, now=NOW) is False

    def test_free_text_reply_with_several_pending_answers_nothing(self):
        """An unrelated message must not silently retire N open decisions."""
        _pending(mid="m-1", eid="esc-1")
        _pending(mid="m-2", eid="esc-2")
        assert conv.mark_answered(SLUG, now=NOW) == 0
        record = conv.read_conversation(SLUG)
        assert [e["state"] for e in record["entries"]] == ["pending", "pending"]
        assert conv.needs_you(SLUG, now=NOW) is True

    def test_scoped_reply_answers_exactly_that_record(self):
        _pending(mid="m-1", eid="esc-1")
        _pending(mid="m-2", eid="esc-2")
        assert conv.mark_answered(SLUG, escalation_id="esc-2", now=NOW) == 1
        record = conv.read_conversation(SLUG)
        assert [e["state"] for e in record["entries"]] == ["pending", "answered"]
        assert conv.needs_you(SLUG, now=NOW) is True
        # Answering the last one clears the badge.
        assert conv.mark_answered(SLUG, escalation_id="esc-1", now=NOW) == 1
        assert conv.needs_you(SLUG, now=NOW) is False

    def test_scoped_reply_for_unknown_or_settled_id_is_a_noop(self):
        _pending(mid="m-1", eid="esc-1")
        assert conv.mark_answered(SLUG, escalation_id="esc-nope", now=NOW) == 0
        conv.mark_answered(SLUG, escalation_id="esc-1", now=NOW)
        assert conv.mark_answered(SLUG, escalation_id="esc-1", now=NOW) == 0

    def test_late_reply_never_answers_a_passed_deadline(self):
        _pending(deadline="2026-09-04T22:10:00Z", default_action="Push A")
        later = NOW + timedelta(minutes=11)
        assert conv.mark_answered(SLUG, now=later) == 0
        assert conv.read_conversation(SLUG)["entries"][0]["state"] == "defaulted"

    def test_reply_with_nothing_pending_does_not_write(self):
        assert conv.mark_answered(SLUG, now=NOW) == 0
        assert not conv.conversation_path(SLUG).exists()

    def test_deadline_passing_clears_without_a_write(self):
        _pending(deadline="2026-09-04T22:10:00Z", default_action="Push A")
        before = conv.conversation_path(SLUG).read_text()
        assert conv.needs_you(SLUG, now=NOW) is True
        assert conv.needs_you(SLUG, now=NOW + timedelta(minutes=11)) is False
        # Derived on read: the file itself is untouched until the next write.
        assert conv.conversation_path(SLUG).read_text() == before

    def test_deadline_with_default_becomes_defaulted_else_expired(self):
        _pending(deadline="2026-09-04T22:10:00Z", default_action="Push A", mid="m-1", eid="esc-1")
        _pending(deadline="2026-09-04T22:10:00Z", mid="m-2", eid="esc-2")
        record = conv.read_conversation(SLUG)
        assert conv.sweep_deadlines(record, now=NOW + timedelta(minutes=11)) is True
        assert [e["state"] for e in record["entries"]] == ["defaulted", "expired"]
        # A later reply persists the sweep and does not resurrect them.
        conv.mark_answered(SLUG, now=NOW + timedelta(minutes=12))
        record = conv.read_conversation(SLUG)
        assert [e["state"] for e in record["entries"]] == ["defaulted", "expired"]

    def test_public_view_derives_counts(self):
        _pending(mid="m-1", eid="esc-1")
        view = conv.public_view(conv.read_conversation(SLUG), now=NOW)
        assert view["needs_you"] is True
        assert view["pending_escalations"] == 1
        assert view["conversation_id"] == "dm:radar"


class TestDeadlineParsing:
    @pytest.mark.parametrize(
        "raw, expected_secs",
        [("30m", 1800), ("2h", 7200), ("900s", 900), ("1d", 86400), (900, 900), ("120", 120)],
    )
    def test_durations_resolve_relative_to_now(self, raw, expected_secs):
        out = conv.resolve_deadline(raw, now=NOW)
        assert out == (NOW + timedelta(seconds=expected_secs)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_absolute_iso_is_kept(self):
        assert conv.resolve_deadline("2026-09-04T22:40:00Z", now=NOW) == "2026-09-04T22:40:00Z"
        assert conv.resolve_deadline("2026-09-05T00:40:00+02:00", now=NOW) == "2026-09-04T22:40:00Z"

    def test_empty_means_no_deadline(self):
        assert conv.resolve_deadline(None, now=NOW) is None
        assert conv.resolve_deadline("", now=NOW) is None

    @pytest.mark.parametrize("raw", ["10s", "8d", "2020-01-01T00:00:00Z", "soon", "5x", True])
    def test_out_of_window_or_garbage_is_refused(self, raw):
        with pytest.raises(ValueError):
            conv.resolve_deadline(raw, now=NOW)
