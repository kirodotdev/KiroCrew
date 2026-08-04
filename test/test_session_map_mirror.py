"""Tests for SessionMap channel-neutral outbound mirror binding.

Covers the C1 generalization of the Slack-only dashboard->channel mirror into a
channel-agnostic ``ChannelLink`` binding: non-Slack targets are stored under
``mirror``; Slack routes back through the dedicated slack-link fields (keeping
its reverse index intact); legacy Slack sessions surface as a synthesized
Slack ``ChannelLink`` without needing migration.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import SessionMap


@pytest.fixture()
def session_map(tmp_path):
    """A SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestNonSlackMirror:
    def test_set_get_round_trip(self, session_map):
        link = ChannelLink(channel_type="telegram", channel_id="12345", thread_id=None)
        session_map.set_mirror_link("dashboard:chat-1", link)
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == link

    def test_stored_under_mirror_field(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="99")
        )
        assert session_map._data["dashboard:chat-1"]["mirror"]["channel_type"] == "telegram"

    def test_does_not_touch_slack_link(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="99")
        )
        # A telegram mirror is NOT a Slack link.
        assert session_map.get_slack_link("dashboard:chat-1") == (None, None)

    def test_creates_entry_when_absent(self, session_map):
        session_map.set_mirror_link(
            "fresh:key", ChannelLink(channel_type="telegram", channel_id="1")
        )
        assert "fresh:key" in session_map._data

    def test_overwrites_existing_mirror(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="2")
        )
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got is not None and got.channel_id == "2"


class TestSlackRouting:
    def test_set_mirror_routes_to_slack_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        # Routed through the dedicated Slack fields + reverse index.
        assert session_map.get_slack_link("dashboard:chat-1") == ("ts-1", "C1")
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"
        # No parallel ``mirror`` field is written for Slack.
        assert "mirror" not in session_map._data["dashboard:chat-1"]

    def test_get_mirror_reflects_slack_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1")


class TestLegacyFallback:
    def test_slack_link_surfaces_as_mirror(self, session_map):
        # A session linked via the legacy slack path (no explicit ``mirror``).
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_slack_link("dashboard:chat-1", "ts-9", "C9")
        assert "mirror" not in session_map._data["dashboard:chat-1"]
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C9", thread_id="ts-9")

    def test_channel_only_legacy_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map._data["dashboard:chat-1"]["slack_channel_id"] = "C9"
        session_map._data["dashboard:chat-1"]["slack_thread_ts"] = None
        got = session_map.get_mirror_link("dashboard:chat-1")
        assert got == ChannelLink(channel_type="slack", channel_id="C9", thread_id=None)


class TestGetMirrorLinkNone:
    def test_no_entry(self, session_map):
        assert session_map.get_mirror_link("nope:key") is None

    def test_entry_without_link(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        assert session_map.get_mirror_link("dashboard:chat-1") is None


class TestMirrorReverseLookup:
    def test_outbound_only_mirror_is_not_an_inbound_route(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]
        assert session_map.find_mirror_sessions(link, inbound_only=True) == []

    def test_resume_binding_is_found_by_exact_location(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            link,
            accepts_inbound=True,
        )

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1"
        ]
        assert session_map.find_mirror_sessions(
            ChannelLink(channel_type="discord", channel_id="dm-2"),
            inbound_only=True,
        ) == []

    def test_duplicate_locations_are_explicit_not_arbitrarily_resolved(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]

    def test_outbound_overwrite_removes_inbound_marker(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        session_map.set_mirror_link("dashboard:chat-1", link)

        assert session_map.find_mirror_sessions(link, inbound_only=True) == []
        assert "mirror_accepts_inbound" not in session_map._data["dashboard:chat-1"]


class TestClearMirrorLink:
    def test_clear_non_slack(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        assert session_map.clear_mirror_link("dashboard:chat-1") is True
        assert session_map.get_mirror_link("dashboard:chat-1") is None

    def test_clear_slack_routes_and_evicts_reverse_index(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"
        assert session_map.clear_mirror_link("dashboard:chat-1") is True
        assert session_map.get_mirror_link("dashboard:chat-1") is None
        assert session_map.get_session_for_thread("ts-1") is None

    def test_clear_returns_false_when_absent(self, session_map):
        session_map.set("dashboard:chat-1", "sid-abc")
        assert session_map.clear_mirror_link("dashboard:chat-1") is False

    def test_clear_returns_false_when_no_entry(self, session_map):
        assert session_map.clear_mirror_link("nope:key") is False

    def test_set_none_clears(self, session_map):
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
        )
        session_map.set_mirror_link("dashboard:chat-1", None)
        assert session_map.get_mirror_link("dashboard:chat-1") is None


class TestPrunePreservesMirror:
    def test_mirror_only_entry_survives_prune(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            # No sid yet, no Slack thread — only a non-Slack mirror binding.
            sm.set_mirror_link(
                "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="1")
            )
            pruned = sm.prune()
            assert pruned == 0
            assert sm.get_mirror_link("dashboard:chat-1") is not None


class TestPersistence:
    def test_inbound_resume_marker_round_trips_to_disk(self, tmp_path):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            assert sm2.find_mirror_sessions(link, inbound_only=True) == [
                "dashboard:chat-1"
            ]

    def test_mirror_round_trips_to_disk(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set_mirror_link(
                "dashboard:chat-1",
                ChannelLink(channel_type="telegram", channel_id="777", thread_id=None),
            )
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm2 = SessionMap()
            got = sm2.get_mirror_link("dashboard:chat-1")
            assert got == ChannelLink(channel_type="telegram", channel_id="777", thread_id=None)


class TestLegacyDashboardSpelling:
    """A channel conversation's mirror now lives on its own session key; a
    binding written under the old ``dashboard:<safe key>`` spelling must still
    resolve and still be clearable, so an existing link is not orphaned."""

    CHANNEL = "telegram:kirocrew:direct:7"
    LEGACY = "dashboard:telegram_kirocrew_direct_7"

    def test_read_falls_back_to_legacy_row(self, session_map):
        link = ChannelLink(channel_type="telegram", channel_id="7")
        session_map.set_mirror_link(self.LEGACY, link)
        assert session_map.get_mirror_link(self.CHANNEL) == link

    def test_clear_reaches_legacy_row(self, session_map):
        session_map.set_mirror_link(
            self.LEGACY, ChannelLink(channel_type="telegram", channel_id="7")
        )
        assert session_map.clear_mirror_link(self.CHANNEL) is True
        assert session_map.get_mirror_link(self.CHANNEL) is None

    def test_canonical_binding_wins_over_legacy(self, session_map):
        session_map.set_mirror_link(
            self.LEGACY, ChannelLink(channel_type="telegram", channel_id="old")
        )
        fresh = ChannelLink(channel_type="telegram", channel_id="new")
        session_map.set_mirror_link(self.CHANNEL, fresh)
        assert session_map.get_mirror_link(self.CHANNEL) == fresh

    def test_no_fallback_for_dashboard_born_key(self, session_map):
        # Only a channel key has a legacy twin; a dashboard session must not
        # inherit a binding from some unrelated sanitized name.
        assert session_map.get_mirror_link("dashboard:chat-1") is None
