"""Tests for SessionMap.clear_slack_link — the unlink half of Slack linking.

Covers Mesh-1969: removing a session's Slack link must drop both link fields,
evict the reverse index, preserve the session entry/sid, and skip _save() when
there was nothing to clear.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.session_map import SessionMap


@pytest.fixture()
def session_map(tmp_path):
    """Create a SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestClearSlackLink:
    def test_removes_both_link_fields_when_linked(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")

        assert session_map.clear_slack_link("dash:1") is True
        assert session_map.get_slack_link("dash:1") == (None, None)

    def test_preserves_sid_and_entry(self, session_map):
        session_map.set("dash:1", "sid-abc", provider="claude_code", cwd="/tmp/ws")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")

        session_map.clear_slack_link("dash:1")

        # Entry kept: sid/provider/cwd survive the unlink.
        assert session_map.get("dash:1") == "sid-abc"
        assert session_map.get_provider("dash:1") == "claude_code"
        assert session_map.get_cwd("dash:1") == "/tmp/ws"

    def test_evicts_reverse_index(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")
        assert session_map.get_session_for_thread("ts-1") == "dash:1"

        session_map.clear_slack_link("dash:1")

        assert session_map.get_session_for_thread("ts-1") is None

    def test_reverse_index_not_evicted_when_owned_by_other(self, session_map):
        """If the reverse index points the old ts at a DIFFERENT session, the
        unlink of this session must not steal/drop the other session's entry."""
        session_map.set("dash:1", "sid-1")
        session_map.set("dash:2", "sid-2")
        session_map.set_slack_link("dash:1", "ts-shared", "C-1")
        # dash:2 takes over the thread (reverse index now points to dash:2).
        session_map.set_slack_link("dash:2", "ts-shared", "C-1")
        assert session_map.get_session_for_thread("ts-shared") == "dash:2"

        # Clearing dash:1 (a stale link to ts-shared) must not evict dash:2's index.
        session_map.clear_slack_link("dash:1")

        assert session_map.get_session_for_thread("ts-shared") == "dash:2"

    def test_returns_false_when_no_link(self, session_map):
        session_map.set("dash:1", "sid-abc")  # entry exists, but no slack link
        assert session_map.clear_slack_link("dash:1") is False

    def test_returns_false_when_no_entry(self, session_map):
        assert session_map.clear_slack_link("nonexistent") is False

    def test_no_save_when_nothing_changed(self, session_map):
        session_map.set("dash:1", "sid-abc")
        with patch.object(session_map, "_save") as mock_save:
            assert session_map.clear_slack_link("dash:1") is False
            mock_save.assert_not_called()

    def test_save_when_link_cleared(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")
        with patch.object(session_map, "_save") as mock_save:
            assert session_map.clear_slack_link("dash:1") is True
            mock_save.assert_called_once()

    def test_idempotent(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")

        assert session_map.clear_slack_link("dash:1") is True
        # Second call: nothing left to clear.
        assert session_map.clear_slack_link("dash:1") is False
        assert session_map.get_slack_link("dash:1") == (None, None)

    def test_round_trip_link_unlink(self, session_map):
        session_map.set("dash:1", "sid-abc")
        session_map.set_slack_link("dash:1", "ts-1", "C-1")
        assert session_map.get_slack_link("dash:1") == ("ts-1", "C-1")

        session_map.clear_slack_link("dash:1")

        assert session_map.get_slack_link("dash:1") == (None, None)
        assert session_map.get_session_for_thread("ts-1") is None

    def test_clear_persists_to_disk(self, tmp_path):
        # provider="claude_code" so get() returns the sid without a kiro-session
        # file existence check (which would otherwise prune the entry).
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set("dash:1", "sid-abc", provider="claude_code")
            sm.set_slack_link("dash:1", "ts-1", "C-1")
            sm.clear_slack_link("dash:1")

        # Reload from disk: the cleared link must stay cleared, sid preserved.
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            assert sm2.get_slack_link("dash:1") == (None, None)
            assert sm2.get("dash:1") == "sid-abc"
            assert sm2.get_session_for_thread("ts-1") is None

    def test_channel_only_link_is_cleared(self, session_map):
        """A link with channel but no thread_ts still counts as present."""
        session_map.set("dash:1", "sid-abc")
        session_map._data["dash:1"]["slack_channel_id"] = "C-1"
        session_map._data["dash:1"]["slack_thread_ts"] = None

        assert session_map.clear_slack_link("dash:1") is True
        assert session_map.get_slack_link("dash:1") == (None, None)
