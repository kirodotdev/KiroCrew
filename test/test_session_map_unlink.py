"""Tests for SessionMap.clear_slack_link — the unlink half of Slack linking.

Covers removing a session's Slack link must drop both link fields,
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


class TestSetSlackLinkEvictsPriorOwner:
    """Relinking a thread to a new session must strip the prior owner's link.

    Before the eviction, a second link of the same thread_ts to a different
    key rewrote the reverse index but left the loser's ``slack_thread_ts`` /
    ``slack_channel_id`` in place — two entries claimed one thread, and the
    inconsistency was persisted.
    """

    def test_prior_owner_link_fields_are_cleared(self, session_map):
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map.set_slack_link("dash:a", "ts-1", "C-1")

        session_map.set_slack_link("dash:b", "ts-1", "C-1")

        # Loser keeps its entry and sid, but no longer claims the thread.
        assert session_map.get_slack_link("dash:a") == (None, None)
        assert session_map.get("dash:a") == "sid-a"
        # Winner owns the thread in both directions.
        assert session_map.get_slack_link("dash:b") == ("ts-1", "C-1")
        assert session_map.get_session_for_thread("ts-1") == "dash:b"

    def test_single_owner_invariant_survives_reload(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set("dash:a", "sid-a", provider="claude_code")
            sm.set("dash:b", "sid-b", provider="claude_code")
            sm.set_slack_link("dash:a", "ts-1", "C-1")
            sm.set_slack_link("dash:b", "ts-1", "C-1")

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            assert sm2.get_slack_link("dash:a") == (None, None)
            assert sm2.get("dash:a") == "sid-a"
            assert sm2.get_slack_link("dash:b") == ("ts-1", "C-1")
            assert sm2.get_session_for_thread("ts-1") == "dash:b"

    def test_same_key_same_thread_is_idempotent(self, session_map):
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set_slack_link("dash:a", "ts-1", "C-1")
        with patch.object(session_map, "_save") as mock_save:
            session_map.set_slack_link("dash:a", "ts-1", "C-1")
            mock_save.assert_not_called()

        assert session_map.get_slack_link("dash:a") == ("ts-1", "C-1")
        assert session_map.get_session_for_thread("ts-1") == "dash:a"

    def test_same_key_new_thread_still_evicts_old_index(self, session_map):
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set_slack_link("dash:a", "ts-1", "C-1")

        session_map.set_slack_link("dash:a", "ts-2", "C-1")

        assert session_map.get_session_for_thread("ts-1") is None
        assert session_map.get_session_for_thread("ts-2") == "dash:a"
        assert session_map.get_slack_link("dash:a") == ("ts-2", "C-1")

    def test_eviction_and_claim_land_in_one_save(self, session_map):
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map.set_slack_link("dash:a", "ts-1", "C-1")
        with patch.object(session_map, "_save") as mock_save:
            session_map.set_slack_link("dash:b", "ts-1", "C-1")
            assert mock_save.call_count == 1

    def test_prior_owner_missing_entry_is_tolerated(self, session_map):
        """A dangling reverse-index entry (no backing _data entry) must not crash."""
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map._thread_to_session["ts-1"] = "dash:ghost"

        session_map.set_slack_link("dash:b", "ts-1", "C-1")

        assert session_map.get_session_for_thread("ts-1") == "dash:b"
        assert session_map.get_slack_link("dash:b") == ("ts-1", "C-1")

    def test_clear_sentinel_empty_ts_never_evicts(self, session_map, caplog):
        """set_slack_link(key, "", "") is the displaced-slot clear sentinel.

        It must not evict whoever a stale "" index entry names, must not
        sweep other displaced entries (which carry ts="" themselves), must
        not log a phantom reassignment of thread "", and "" must not enter
        the reverse index.
        """
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map.set("dash:c", "sid-c", provider="claude_code")
        session_map._thread_to_session[""] = "dash:a"
        session_map.set_slack_link("dash:a", "ts-2", "C-1")
        # dash:b was displaced earlier: its entry carries the "" sentinel.
        session_map.set_slack_link("dash:b", "", "")

        with caplog.at_level("INFO", logger="kiro_crew.session_map"):
            session_map.set_slack_link("dash:c", "", "")

        assert session_map.get_slack_link("dash:a") == ("ts-2", "C-1")
        assert session_map.get_session_for_thread("ts-2") == "dash:a"
        assert session_map.get_session_for_thread("") != "dash:c"
        # The other displaced entry is not swept by a fellow clear.
        assert session_map.get_slack_link("dash:b") == ("", "")
        assert "reassigned" not in caplog.text

    def test_self_derived_claim_does_not_strip_the_owner(self, session_map):
        """A slack:<ts> self-link must not evict the non-derived owner.

        Neither the owner's fields (the load-time tie-break needs them to
        heal) nor its live reverse-index routing: a self-link lands mid-turn
        on the native path while the dashboard owner is live, and stealing
        the index would route later replies to the fork.
        """
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set_slack_link("dash:a", "ts-1", "C-1")

        session_map.set_slack_link("slack:ts-1", "ts-1", "C-1")

        assert session_map.get_slack_link("dash:a") == ("ts-1", "C-1")
        assert session_map.get_session_for_thread("ts-1") == "dash:a"

    def test_self_derived_claim_routes_when_unclaimed(self, session_map):
        """A lone self-link still enters the reverse index."""
        session_map.set_slack_link("slack:ts-9", "ts-9", "C-1")
        assert session_map.get_session_for_thread("ts-9") == "slack:ts-9"

    def test_all_rival_claimants_are_swept(self, session_map):
        """A legacy map can hold several claimants; a new claim clears them all."""
        for k in ("dash:a", "dash:b", "dash:c"):
            session_map.set(k, f"sid-{k[-1]}", provider="claude_code")
            session_map._data[k]["slack_thread_ts"] = "ts-1"
            session_map._data[k]["slack_channel_id"] = "C-1"
        session_map._rebuild_thread_index()

        session_map.set_slack_link("dash:new", "ts-1", "C-1")

        for k in ("dash:a", "dash:b", "dash:c"):
            assert session_map.get_slack_link(k) == (None, None)
        assert session_map.get_session_for_thread("ts-1") == "dash:new"

    def test_fast_path_still_sweeps_hidden_rivals(self, session_map):
        """Re-linking a key whose own fields already match must still clear a
        rival entry left behind by a pre-fix map."""
        session_map.set("dash:a", "sid-a", provider="claude_code")
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map.set_slack_link("dash:b", "ts-1", "C-1")
        # Simulate a legacy two-claimant map: dash:a also carries the fields.
        session_map._data["dash:a"]["slack_thread_ts"] = "ts-1"
        session_map._data["dash:a"]["slack_channel_id"] = "C-1"

        session_map.set_slack_link("dash:b", "ts-1", "C-1")

        assert session_map.get_slack_link("dash:a") == (None, None)
        assert session_map.get_session_for_thread("ts-1") == "dash:b"

    def test_reassignment_log_only_fires_on_a_real_eviction(self, session_map, caplog):
        session_map.set("dash:b", "sid-b", provider="claude_code")
        session_map._thread_to_session["ts-1"] = "dash:ghost"
        with caplog.at_level("INFO", logger="kiro_crew.session_map"):
            session_map.set_slack_link("dash:b", "ts-1", "C-1")
        assert "reassigned" not in caplog.text
