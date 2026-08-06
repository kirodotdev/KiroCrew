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

from kiro_crew.messaging.link import (
    ChannelLink,
    legacy_dashboard_mirror_key,
    release_conversation_location,
)
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


class TestClearMirrorLinksAt:
    LINK = ChannelLink(channel_type="discord", channel_id="chan-1")

    def test_clears_every_spelling_at_the_location(self, session_map):
        # The stale-mirror shape: rows under key spellings the conversation no
        # longer derives (rotated generation, pre-unification dashboard row)
        # plus a dashboard session mirroring in — all at one location.
        session_map.set_mirror_link("discord:agent:direct:u1", self.LINK)
        session_map.set_mirror_link("dashboard:discord_agent_direct_u1", self.LINK)
        session_map.set_mirror_link("dashboard:chat-3", self.LINK)
        cleared = session_map.clear_mirror_links_at(self.LINK)
        assert sorted(cleared) == [
            "dashboard:chat-3",
            "dashboard:discord_agent_direct_u1",
            "discord:agent:direct:u1",
        ]
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_returns_empty_when_location_free(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        other = ChannelLink(channel_type="discord", channel_id="chan-2")
        assert session_map.clear_mirror_links_at(other) == []
        assert session_map.get_mirror_link("dashboard:chat-1") == self.LINK

    def test_no_save_when_location_free(self, session_map):
        # An empty sweep must not touch disk — the common case is `!unlink`
        # on an unlinked conversation.
        with patch.object(session_map, "_save") as save:
            assert session_map.clear_mirror_links_at(self.LINK) == []
        save.assert_not_called()

    def test_exact_location_match_includes_thread(self, session_map):
        topic = ChannelLink(channel_type="telegram", channel_id="7", thread_id="42")
        general = ChannelLink(channel_type="telegram", channel_id="7", thread_id=None)
        session_map.set_mirror_link("dashboard:chat-1", topic)
        assert session_map.clear_mirror_links_at(general) == []
        assert session_map.clear_mirror_links_at(topic) == ["dashboard:chat-1"]

    def test_clears_inbound_resume_binding_and_marker(self, session_map):
        # Duplicate/corrupt inbound bindings are exactly what the inbound
        # resolver refuses to pick from — the location sweep is the repair.
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        assert session_map.clear_mirror_links_at(self.LINK) == ["dashboard:chat-1"]
        assert session_map.mirror_accepts_inbound("dashboard:chat-1") is False
        assert session_map.get_mirror_link("dashboard:chat-1") is None

    def test_slack_bindings_are_out_of_scope(self, session_map):
        session_map.set(
            "dashboard:chat-1", "sid-abc"
        )  # Slack link needs an entry to attach to
        session_map.set_mirror_link(
            "dashboard:chat-1",
            ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
        )
        slack = ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1")
        assert session_map.clear_mirror_links_at(slack) == []
        assert session_map.get_session_for_thread("ts-1") == "dashboard:chat-1"

    def test_cleared_rows_survive_reload(self, session_map, tmp_path):
        # The sweep must persist: a clear that only mutates memory would
        # resurrect the stale binding on the next gateway start.
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        session_map.clear_mirror_links_at(self.LINK)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.find_mirror_sessions(self.LINK) == []


class TestReleaseConversationLocation:
    """The shared in-channel unlink, composed against the REAL SessionMap."""

    KEY = "discord:agent:direct:u1"
    LINK = ChannelLink(channel_type="discord", channel_id="chan-1")

    def test_free_location_reports_not_linked(self, session_map):
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "This conversation wasn't linked."
        assert swept == []

    def test_own_binding_reports_plain_success(self, session_map):
        session_map.set_mirror_link(self.KEY, self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        # The conversation's own row falls to the key-addressed clear BEFORE
        # the sweep runs, so one binding is never double-counted.
        assert reply == "✅ Unlinked."
        assert swept == []
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_stranded_and_foreign_rows_are_counted(self, session_map):
        # Own binding + a row stranded under a rotated-generation spelling +
        # a dashboard session mirroring in: one call frees the location and
        # the reply owns up to the full count.
        session_map.set_mirror_link(self.KEY, self.LINK)
        session_map.set_mirror_link(f"{self.KEY}:gen1", self.LINK)
        session_map.set_mirror_link("dashboard:chat-9", self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "✅ Unlinked (3 bindings)."
        assert sorted(swept) == ["dashboard:chat-9", f"{self.KEY}:gen1"]
        assert session_map.find_mirror_sessions(self.LINK) == []

    def test_legacy_spelling_row_counted_once(self, session_map):
        # A pre-unification row is reachable by the legacy key clear; the
        # sweep must not see it again.
        session_map.set_mirror_link(legacy_dashboard_mirror_key(self.KEY), self.LINK)
        reply, swept = release_conversation_location(
            session_map, key=self.KEY, location=self.LINK, channel="discord"
        )
        assert reply == "✅ Unlinked."
        assert swept == []


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
