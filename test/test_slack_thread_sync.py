"""Tests for Live Slack thread sync (bidirectional mirroring)."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.messaging.link import ChannelLink

# -- Helpers --


def _make_state(tmp_path, **kwargs):
    sessions = MagicMock(count=0)
    sessions.remove = MagicMock()
    sessions.get_slack_link = MagicMock(return_value=(None, None))
    sessions.get_mirror_link = MagicMock(return_value=None)
    # Explicit default: a bare MagicMock attribute returns a truthy Mock, which
    # would make every mirror read as an inbound (two-way) resume binding.
    sessions.mirror_accepts_inbound = MagicMock(return_value=False)
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


def _fake_transport(channel_type: str):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=SimpleNamespace(supports_proactive_send=True),
    )


class TestChannelNeutralSlotLinks:
    def _permit_channels(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *args, **kwargs: SimpleNamespace(permitted=True),
        )

    def test_discord_legacy_fields_emit_origin_not_slack(self, tmp_path, monkeypatch):
        self._permit_channels(monkeypatch)
        state = _make_state(tmp_path)
        state.sessions.get_slack_link.return_value = (
            "",
            "discord:356163505868767244",
        )
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "slack",
            channel_id="discord:356163505868767244",
            thread_id="",
        )
        state.register_channel_transport(_fake_transport("discord"))

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert payload["slack_linked"] is False
        assert payload["slack_channel"] == ""
        assert payload["slack_thread_ts"] == ""
        assert payload["links"] == [
            {
                "channel": "discord",
                "label": "Discord DM",
                "target": "…767244",
                "direction": "origin",
                "live": True,
                "paused": False,
            }
        ]

    def test_discord_mirror_emits_out_link_not_slack(self, tmp_path, monkeypatch):
        self._permit_channels(monkeypatch)
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "discord",
            channel_id="356163505868767244",
        )
        state.register_channel_transport(_fake_transport("discord"))

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert payload["slack_linked"] is False
        assert payload["links"] == [
            {
                "channel": "discord",
                "label": "Discord DM",
                "target": "…767244",
                "direction": "out",
                "live": True,
                "paused": False,
            }
        ]

    def test_resume_binding_emits_two_way_direction(self, tmp_path, monkeypatch):
        """An inbound-accepting mirror is `both`, not `out`.

        A `!sessions` pick makes the binding two-way — messages from that channel
        land in this session — which is a different thing to see and release than
        a one-way `!link` mirror, so the payload must distinguish them.
        """
        self._permit_channels(monkeypatch)
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "discord",
            channel_id="356163505868767244",
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=True)
        state.register_channel_transport(_fake_transport("discord"))

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert [link["direction"] for link in payload["links"]] == ["both"]
        assert payload["slack_linked"] is False

    def test_outbound_only_mirror_stays_out(self, tmp_path, monkeypatch):
        self._permit_channels(monkeypatch)
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "discord",
            channel_id="356163505868767244",
        )
        state.sessions.mirror_accepts_inbound = MagicMock(return_value=False)
        state.register_channel_transport(_fake_transport("discord"))

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert [link["direction"] for link in payload["links"]] == ["out"]

    def test_missing_inbound_accessor_degrades_to_out(self, tmp_path, monkeypatch):
        """A SessionManager without the accessor must not drop the link."""
        self._permit_channels(monkeypatch)
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "discord",
            channel_id="356163505868767244",
        )
        state.sessions.mirror_accepts_inbound = MagicMock(side_effect=AttributeError)
        state.register_channel_transport(_fake_transport("discord"))

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert [link["direction"] for link in payload["links"]] == ["out"]

    def test_real_slack_link_remains_slack_linked(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.get_slack_link.return_value = ("1712793600.123456", "C123")
        state.sessions.get_mirror_link.return_value = ChannelLink(
            "slack",
            channel_id="C123",
            thread_id="1712793600.123456",
        )

        payload = state.serialize_slot(state.get_or_create_slot("s1"))

        assert payload["slack_linked"] is True
        assert payload["slack_channel"] == "C123"
        assert payload["slack_thread_ts"] == "1712793600.123456"
        assert payload["links"][0]["channel"] == "slack"
