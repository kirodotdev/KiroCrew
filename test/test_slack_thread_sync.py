"""Tests for Live Slack thread sync (bidirectional mirroring)."""
from __future__ import annotations

from unittest.mock import MagicMock

from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog

# -- Helpers --


def _make_state(tmp_path, **kwargs):
    sessions = MagicMock(count=0)
    sessions.remove = MagicMock()
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
        **kwargs,
    )


# -- Unit tests: _ChatSlot slack fields --


class TestChatSlotSlackFields:
    def test_default_slack_linked_is_false(self):
        slot = _ChatSlot("s1")
        assert slot._slack_linked is False

    def test_default_slack_channel_empty(self):
        slot = _ChatSlot("s1")
        assert slot._slack_channel == ""

    def test_default_slack_thread_ts_empty(self):
        slot = _ChatSlot("s1")
        assert slot._slack_thread_ts == ""

    def test_to_dict_includes_slack_linked(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert "slack_linked" in d
        assert d["slack_linked"] is False

    def test_to_dict_includes_slack_channel(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert d["slack_channel"] == ""

    def test_to_dict_includes_slack_thread_ts(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert d["slack_thread_ts"] == ""

    def test_to_dict_reflects_linked_state(self):
        slot = _ChatSlot("s1")
        slot._slack_linked = True
        slot._slack_channel = "C123"
        slot._slack_thread_ts = "1234.5678"
        d = slot.to_dict()
        assert d["slack_linked"] is True
        assert d["slack_channel"] == "C123"
        assert d["slack_thread_ts"] == "1234.5678"


# -- Unit tests: DashboardState.link_slack --


class TestDashboardStateLinkSlack:
    def test_link_slack_sets_fields(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        state.link_slack("s1", "1234.5678", "C123")
        assert slot._slack_linked is True
        assert slot._slack_channel == "C123"
        assert slot._slack_thread_ts == "1234.5678"

    def test_link_slack_persists_to_session_store(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.link_slack("s1", "1234.5678", "C123")
        state.sessions.set_slack_link.assert_called_once()
        call_args = state.sessions.set_slack_link.call_args[0]
        assert "s1" in call_args[0]  # history key contains slot name
        assert call_args[1] == "1234.5678"
        assert call_args[2] == "C123"

    def test_link_slack_missing_slot_noop(self, tmp_path):
        state = _make_state(tmp_path)
        # Should not raise
        state.link_slack("nonexistent", "1234.5678", "C123")

    def test_link_multiple_slots(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("s1")
        state.get_or_create_slot("s2")
        state.link_slack("s1", "111.000", "C1")
        state.link_slack("s2", "222.000", "C2")
        assert state._slots["s1"]._slack_linked is True
        assert state._slots["s2"]._slack_linked is True
        assert state._slots["s1"]._slack_thread_ts == "111.000"
        assert state._slots["s2"]._slack_thread_ts == "222.000"


# -- Unit tests: slot restore with slack link --


class TestSlotRestoreSlackLink:
    # TODO: Add integration test for restore_sessions() populating slack link
    # from SessionStore. The restore path is complex and requires full
    # DashboardState initialization with real SessionManager.

    def test_unlinked_slot_stays_false(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        assert slot._slack_linked is False
