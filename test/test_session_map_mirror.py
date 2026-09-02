"""Tests for SessionMap channel-neutral outbound mirror binding.

Covers the C1 generalization of the Slack-only dashboard->channel mirror into a
channel-agnostic ``ChannelLink`` binding: non-Slack targets are stored under
``mirror``; Slack routes back through the dedicated slack-link fields (keeping
its reverse index intact); legacy Slack sessions surface as a synthesized
Slack ``ChannelLink`` without needing migration.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.messaging.link import (
    UNBIND_REASON_PRUNED_STALE,
    UNBIND_REASON_UNSPECIFIED,
    ChannelLink,
    legacy_dashboard_mirror_key,
    release_conversation_location,
)
from kiro_crew.session import SessionManager, _opt_out_key
from kiro_crew.session_map import (
    MIRROR_OPT_OUT_FLAG,
    ConversationOwnershipConflict,
    SessionMap,
    set_unbind_listener,
)


@pytest.fixture()
def session_map(tmp_path):
    """A SessionMap backed by a temp directory."""
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


def _manager_over(session_map):
    """A SessionManager wired to just this map.

    ``set_mirror_opt_out`` / ``mirror_opt_out`` touch nothing but
    ``_session_map``, so binding that one attribute exercises the real accessors
    without standing up a whole manager.
    """
    mgr = SessionManager.__new__(SessionManager)
    mgr._session_map = session_map
    return mgr


def plant_binding(session_map, key, link, *, accepts_inbound=False):
    """Write a binding straight into the map, bypassing the writer's exclusivity.

    ``set_mirror_link`` refuses to put a second session on an inbound-committed
    conversation, so co-located rows cannot be *created* through it. They can
    still EXIST: a ``session_map.json`` written before conversations became
    exclusive, or hand-edited, can hold two owners. Readers and sweeps have to
    cope with that state — the resolver's fail-closed branch and the in-channel
    conflict detection both depend on seeing every owner — so the tests below
    plant the rows directly.
    """
    entry = session_map._ensure_entry(key)
    entry["mirror"] = link.to_dict()
    if accepts_inbound:
        entry["mirror_accepts_inbound"] = True
    session_map._save()


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
        # Planted, not written: see plant_binding. A map holding two owners still
        # reports BOTH, so the resolver refuses to pick one instead of guessing —
        # the reader is permissive where the writer is strict.
        plant_binding(session_map, "dashboard:chat-1", link, accepts_inbound=True)
        plant_binding(session_map, "dashboard:chat-2", link, accepts_inbound=True)

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
        # plus a dashboard session mirroring in — all at one location. Planted
        # directly: the writer now refuses co-location, but a map file can still
        # hold it and the sweep has to free all of it.
        plant_binding(session_map, "discord:agent:direct:u1", self.LINK)
        plant_binding(session_map, "dashboard:discord_agent_direct_u1", self.LINK)
        plant_binding(session_map, "dashboard:chat-3", self.LINK)
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
        # the reply owns up to the full count. Planted directly — the writer
        # refuses co-location, but the sweep has to cope with a map that holds it.
        plant_binding(session_map, self.KEY, self.LINK)
        plant_binding(session_map, f"{self.KEY}:gen1", self.LINK)
        plant_binding(session_map, "dashboard:chat-9", self.LINK)
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

    def test_the_three_clears_are_one_write(self, session_map):
        # Freeing a location is ONE action. Its three clears each rewrite the
        # whole map, so unbatched they are three writes and three separately
        # interruptible steps — a crash or a concurrent writer partway through
        # leaves the location half-freed while the reply already said ✅.
        plant_binding(session_map, self.KEY, self.LINK)
        plant_binding(session_map, f"{self.KEY}:gen1", self.LINK)
        with patch.object(SessionMap, "_write", autospec=True) as write:
            release_conversation_location(
                session_map, key=self.KEY, location=self.LINK, channel="discord"
            )
        assert write.call_count == 1

    def test_an_outer_batch_still_collapses_to_one_write(self, session_map):
        # Telegram wraps this call together with its opt-out write. Nesting is
        # counted, so the wider sequence must stay a single write rather than
        # this function's batch flushing early inside it.
        plant_binding(session_map, self.KEY, self.LINK)
        with patch.object(SessionMap, "_write", autospec=True) as write:
            with session_map.batched_save():
                session_map.set_flag(self.KEY, "mirror_opt_out", True)
                release_conversation_location(
                    session_map, key=self.KEY, location=self.LINK, channel="discord"
                )
        assert write.call_count == 1


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

    def test_stale_sid_repairs_a_resume_binding_instead_of_dropping_it(self, tmp_path):
        """A restart after kiro-cli collected the session file must not unlink.

        The entry is stale by the ``sid`` predicate, but it carries the inbound
        resume binding: delete it and the next message from that channel falls
        back to the channel's own session instead of resuming the linked one.
        """
        key = "dashboard:chat-1"
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set(key, "sid-that-no-longer-exists")
            sm.set_mirror_link(key, link, accepts_inbound=True)
            assert sm.prune() == 0
            assert sm.get_mirror_link(key) == link
            assert sm.mirror_accepts_inbound(key) is True
            assert (sm._data.get(key) or {}).get("sid") == ""
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        # The repair reached disk, so the next startup does not redo it.
        assert reloaded.get_mirror_link(key) == link
        assert reloaded.mirror_accepts_inbound(key) is True
        assert not (reloaded._data.get(key) or {}).get("sid")

    def test_stale_sid_repairs_a_slack_thread_binding(self, tmp_path):
        """Same branch for Slack, whose binding lives in the dedicated fields.

        The thread has to keep resolving to this session after the restart, or
        the next reply in it starts a new conversation.
        """
        key = "dashboard:chat-1"
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set(key, "sid-that-no-longer-exists")
            sm.set_slack_link(key, "1700000000.000100", "C123")
            assert sm.prune() == 0
            assert (sm._data.get(key) or {}).get("sid") == ""
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.get_session_for_thread("1700000000.000100") == key
        assert not (reloaded._data.get(key) or {}).get("sid")

    def test_stale_sid_with_no_binding_is_still_collected(self, tmp_path):
        """Repair is for entries that carry state; a bare stale row is garbage."""
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set("dashboard:chat-1", "sid-that-no-longer-exists")
            assert sm.prune() == 1
            assert "dashboard:chat-1" not in sm._data

    def test_a_live_sid_with_a_mirror_is_left_alone(self, tmp_path):
        """Prune only touches entries whose session file is gone."""
        key = "dashboard:chat-1"
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sm = SessionMap()
            sm.set(key, "sid-alive")
            sm.set_mirror_link(key, link, accepts_inbound=True)
            with patch("kiro_crew.session_map._kiro_sessions_dir", return_value=tmp_path):
                (tmp_path / "sid-alive.json").write_text("{}", encoding="utf-8")
                assert sm.prune() == 0
            assert (sm._data.get(key) or {}).get("sid") == "sid-alive"
            assert sm.get_mirror_link(key) == link
            assert sm.mirror_accepts_inbound(key) is True


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


class TestConversationOwnership:
    """One conversation, at most one session — enforced on the writer.

    Not a policy preference. The inbound resolver refuses to choose between two
    candidates, and "no owner" and "two owners" are the same ``None`` to it, so a
    duplicate binding does not misroute a reply — it unroutes it, and the reply
    silently starts a fresh session. Marking a binding inbound-capable without
    this rule would move the fork rather than fix it.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_a_second_session_is_refused(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", self.LINK, accepts_inbound=True)
        # The incumbent is untouched — a refusal never half-applies.
        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-1"]

    def test_an_outbound_claim_over_an_inbound_occupant_is_refused(self, session_map):
        """The scan stays UNFILTERED once the conversation is inbound-committed.

        An in-channel ``!link`` is an outbound claim. Letting it land a second
        binding on a conversation the dashboard is resuming through is the exact
        collision that leaves the resolver two candidates and strands the reply.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("discord:agent:direct:u1", self.LINK)

    def test_an_inbound_claim_over_an_outbound_occupant_is_refused(self, session_map):
        session_map.set_mirror_link("discord:agent:direct:u1", self.LINK)
        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

    def test_two_outbound_mirrors_are_left_alone(self, session_map):
        """Exclusivity is owed to inbound routing, so it is scoped to it.

        Two outbound-only mirrors are merely noisy — both write out, nobody reads
        back — so they stay allowed. Refusing them would reach every transport that
        cannot resume at all (Telegram, Teams, Webex, WeCom, Weixin), whose
        in-channel link handlers do not translate this refusal because they can
        never provoke it.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        # Must not raise.
        session_map.set_mirror_link("dashboard:chat-2", self.LINK)
        assert sorted(session_map.find_mirror_sessions(self.LINK)) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]

    def test_a_non_resuming_transport_is_never_refused(self, session_map):
        """Pins the blast radius: a Telegram chat cannot become inbound-committed.

        ``telegram/transport_dispatch.py`` calls ``set_mirror_link`` without
        catching this exception, and an uncaught raise inside a channel command
        handler is a dropped task and a silent no-reply. It stays unreachable
        because Telegram does not declare ``supports_session_resume``, so the
        dashboard never marks its bindings inbound and nothing else does either.
        """
        chat = ChannelLink(channel_type="telegram", channel_id="55", thread_id=None)
        session_map.set_mirror_link("dashboard:chat-1", chat)
        # Two dashboard sessions mirroring one Telegram chat: allowed before this
        # rule, allowed after it.
        session_map.set_mirror_link("dashboard:chat-2", chat)
        assert len(session_map.find_mirror_sessions(chat)) == 2

    def test_the_same_session_may_rebind_itself(self, session_map):
        """A reconnect is not a rivalry."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        assert session_map.find_mirror_sessions(self.LINK, inbound_only=True) == [
            "dashboard:chat-1"
        ]

    def test_a_session_may_supersede_its_own_legacy_row(self, session_map):
        """The self-set has to include the row the binding actually lives on.

        A pre-unification ``dashboard:`` row IS this session's binding, and only
        ``_mirror_key`` can say so. Deriving the legacy name unconditionally would
        excuse rows that are not this session's; not consulting it at all makes a
        session a rival to itself and refuses its own reconnect.
        """
        key = "discord:agent:direct:u1"
        plant_binding(session_map, legacy_dashboard_mirror_key(key), self.LINK)
        # Must not raise: the only occupant is this same session, older spelling.
        session_map.set_mirror_link(key, self.LINK, accepts_inbound=True)
        assert key in session_map.find_mirror_sessions(self.LINK, inbound_only=True)

    def test_an_unrelated_location_is_never_a_rival(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        elsewhere = ChannelLink(channel_type="discord", channel_id="dm-2")
        session_map.set_mirror_link("dashboard:chat-2", elsewhere, accepts_inbound=True)
        assert session_map.find_mirror_sessions(elsewhere) == ["dashboard:chat-2"]

    def test_a_different_channel_type_at_the_same_id_is_not_a_rival(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        same_id_other_channel = ChannelLink(channel_type="telegram", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-2", same_id_other_channel)
        assert session_map.find_mirror_sessions(same_id_other_channel) == ["dashboard:chat-2"]

    def test_readers_still_report_every_owner_of_a_pre_existing_duplicate(self, session_map):
        """Enforce on the writer; keep the reader permissive.

        A map written before this check can hold two owners. If the reader hid
        one, the resolver would stop failing closed and start routing a reply to
        an arbitrary session, and the in-channel conflict detection that tells the
        user to `!unlink` would see nothing to report.
        """
        plant_binding(session_map, "dashboard:chat-1", self.LINK, accepts_inbound=True)
        plant_binding(session_map, "dashboard:chat-2", self.LINK, accepts_inbound=True)
        assert session_map.find_mirror_sessions(self.LINK, inbound_only=True) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]

    def test_an_inbound_binding_on_a_never_saved_session_survives_a_reload(
        self, session_map, tmp_path
    ):
        """The loader drops a row with no ``sid``, which would lose the binding.

        A dashboard connect can be the first thing that ever writes a row for a
        session, so the row it creates has to be a shape ``_load`` accepts.
        Otherwise the binding is correct in memory, correct on disk, and silently
        gone after the next restart — the fork would come back on reboot only.
        """
        session_map.set_mirror_link("dashboard:brand-new", self.LINK, accepts_inbound=True)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.find_mirror_sessions(self.LINK, inbound_only=True) == [
            "dashboard:brand-new"
        ]
        assert reloaded.mirror_accepts_inbound("dashboard:brand-new") is True


class TestBatchedSave:
    """One write per related mutation sequence, not one per mutation.

    A mutation rewrites the WHOLE map (measured: ~1ms at 192 entries, ~43ms at
    10k), and on the event loop each write is a stall every task shares.
    """

    LINK = ChannelLink(channel_type="telegram", channel_id="7")

    def test_a_sequence_writes_once(self, session_map):
        writes = []
        with patch.object(session_map, "_write", side_effect=lambda: writes.append(1)):
            with session_map.batched_save():
                session_map.set_mirror_link("telegram:kirocrew:direct:7", self.LINK)
                session_map.set_flag("telegram:kirocrew:direct:7", MIRROR_OPT_OUT_FLAG, True)
                session_map.set("telegram:kirocrew:direct:7", "sid-1")
        assert writes == [1]

    def test_the_write_still_happens_when_the_block_raises(self, session_map):
        writes = []
        with patch.object(session_map, "_write", side_effect=lambda: writes.append(1)):
            with pytest.raises(RuntimeError):
                with session_map.batched_save():
                    session_map.set_mirror_link("telegram:kirocrew:direct:7", self.LINK)
                    raise RuntimeError("mid-sequence failure")
        # Leaving the mutation only in memory would lose it on the next restart.
        assert writes == [1]

    def test_nesting_writes_once_at_the_outermost_exit(self, session_map):
        writes = []
        with patch.object(session_map, "_write", side_effect=lambda: writes.append(1)):
            with session_map.batched_save():
                with session_map.batched_save():
                    session_map.set_mirror_link("telegram:kirocrew:direct:7", self.LINK)
                assert writes == []  # inner exit must not write
        assert writes == [1]

    def test_a_block_that_mutates_nothing_writes_nothing(self, session_map):
        writes = []
        with patch.object(session_map, "_write", side_effect=lambda: writes.append(1)):
            with session_map.batched_save():
                session_map.get_mirror_link("telegram:kirocrew:direct:7")
        assert writes == []

    def test_the_batched_data_actually_reaches_disk(self, session_map, tmp_path):
        key = "telegram:kirocrew:direct:7"
        with session_map.batched_save():
            session_map.set_mirror_link(key, self.LINK)
            session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.get_mirror_link(key) == self.LINK
        assert reloaded.get_flag(key, MIRROR_OPT_OUT_FLAG) is True


class TestAutomaticMirrorOptOut:

    """The persisted refusal of automatic origin mirroring (issue #2959).

    A channel that binds its own conversation on every inbound turn re-asserts
    the mirror after a restart, so the in-channel "off" has to outlive the
    binding it removes. Clearing ``mirror`` cannot express that — an entry with
    no binding is indistinguishable from one that was never linked.
    """

    LINK = ChannelLink(channel_type="telegram", channel_id="7")

    def test_opt_out_survives_a_reload(self, session_map, tmp_path):
        session_map.set_flag("telegram:kirocrew:direct:7", MIRROR_OPT_OUT_FLAG, True)
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        assert reloaded.get_flag("telegram:kirocrew:direct:7", MIRROR_OPT_OUT_FLAG) is True

    def test_clearing_the_binding_does_not_clear_the_opt_out(self, session_map):
        """The two are independent: unlink does both, and only one must persist."""
        key = "telegram:kirocrew:direct:7"
        session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        session_map.set_mirror_link(key, self.LINK)
        assert session_map.clear_mirror_link(key) is True
        assert session_map.get_mirror_link(key) is None
        assert session_map.get_flag(key, MIRROR_OPT_OUT_FLAG) is True

    def test_the_flag_name_is_the_one_the_session_manager_writes(self):
        """Pins the ON-DISK spelling.

        ``SessionManager.set_mirror_opt_out`` is the only writer; a rename would
        silently re-enable mirroring for every conversation that turned it off.
        """
        assert MIRROR_OPT_OUT_FLAG == "mirror_opt_out"

    def test_the_refusal_is_keyed_by_the_durable_bucket_not_the_generation(self):
        """A preference about the conversation, not about one session.

        ``/new`` and the configured idle/daily reset rotate the ``:genN`` suffix.
        Keyed per generation the refusal expires on rotation — an idle reset would
        undo the user's "off" unprompted — and each rotated generation strands its
        own row that pruning is forbidden to collect.
        """
        assert _opt_out_key("telegram:kirocrew:direct:7:gen3") == "telegram:kirocrew:direct:7"
        assert _opt_out_key("telegram:kirocrew:direct:7") == "telegram:kirocrew:direct:7"
        assert (
            _opt_out_key("telegram:kirocrew:forum:-100123:5:gen9")
            == "telegram:kirocrew:forum:-100123:5"
        )
        # Outside the canonical grammar there is no generation to strip.
        assert _opt_out_key("dashboard:chat-9") == "dashboard:chat-9"

    def test_the_suffix_is_stripped_even_when_the_key_does_not_parse(self):
        """The shapes that most need stripping are the ones the parser rejects.

        A ``dm_scope="unified"`` bucket is ``unified:{agent}`` — too short for the
        canonical grammar — so a parser-only rule would leave every unified
        conversation keyed per generation, which is the bug being fixed.
        """
        assert _opt_out_key("unified:kirocrew:gen3") == "unified:kirocrew"
        assert _opt_out_key("unified:kirocrew") == "unified:kirocrew"
        # A trailing segment that merely starts with "gen" is not a generation.
        assert _opt_out_key("telegram:kirocrew:direct:general") == (
            "telegram:kirocrew:direct:general"
        )

    def test_every_generation_shares_one_flag_row(self, session_map):
        """Bucket-keying is what bounds the unprunable rows to one per chat."""
        for gen in ("", ":gen1", ":gen2", ":gen7"):
            session_map.set_flag(
                _opt_out_key(f"telegram:kirocrew:direct:7{gen}"), MIRROR_OPT_OUT_FLAG, True
            )
        flagged = [k for k, e in session_map._data.items() if e.get("flags")]
        assert flagged == ["telegram:kirocrew:direct:7"]

    def test_a_refusal_stored_under_the_old_generation_key_is_still_honoured(
        self, session_map
    ):
        """Upgrading must not silently restore mirroring.

        An earlier build keyed the refusal by the generation-suffixed session key.
        Reading only the bucket would miss every refusal already on disk — the
        fix for the expiry bug would itself deliver the expiry bug, once.
        """
        mgr = _manager_over(session_map)
        key = "telegram:kirocrew:direct:7:gen3"
        session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        assert mgr.mirror_opt_out(key) is True

    def test_reading_a_legacy_refusal_promotes_it_to_the_bucket(self, session_map):
        """Otherwise the refusal is honoured for that generation and lost at the next.

        Reading without promoting hands an upgrading user the expiring behaviour
        this change exists to remove, and leaves an unprunable row per generation.
        """
        mgr = _manager_over(session_map)
        session_map.set_flag("telegram:kirocrew:direct:7:gen3", MIRROR_OPT_OUT_FLAG, True)
        assert mgr.mirror_opt_out("telegram:kirocrew:direct:7:gen3") is True
        # Promoted to the bucket, and the generation row retired with it.
        assert session_map.get_flag("telegram:kirocrew:direct:7", MIRROR_OPT_OUT_FLAG) is True
        assert (
            session_map.get_flag("telegram:kirocrew:direct:7:gen3", MIRROR_OPT_OUT_FLAG)
            is False
        )
        # And it now survives the rotation that would have dropped it.
        assert mgr.mirror_opt_out("telegram:kirocrew:direct:7:gen4") is True

    def test_withdrawing_also_retires_the_old_generation_key(self, session_map):
        """Otherwise a legacy refusal outlives the withdrawal that cleared it."""
        mgr = _manager_over(session_map)
        key = "telegram:kirocrew:direct:7:gen3"
        session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        mgr.set_mirror_opt_out(key, False)
        assert mgr.mirror_opt_out(key) is False

    def test_a_session_scoped_flag_does_not_make_an_entry_immortal(self, session_map):
        """Immortality is opt-in, because prune is the only collection path.

        Slack's ``temporary`` / ``incognito`` flags describe ONE session, not a
        durable preference. Keeping their entries would leak a row per such
        thread — and the map is rewritten whole on every mutation, so the leak
        costs every later write, not just disk.
        """
        for flag in ("temporary", "incognito"):
            key = f"slack:kirocrew:{flag}"
            session_map.set_flag(key, flag, True)
        assert session_map.prune() == 2
        assert session_map.get_flag("slack:kirocrew:temporary", "temporary") is False
        assert session_map.get_flag("slack:kirocrew:incognito", "incognito") is False

    def test_a_stale_sid_is_still_collected_when_the_flag_is_session_scoped(
        self, session_map
    ):
        """The repair branch is for settings only, not for any flag at all."""
        key = "slack:kirocrew:direct:7"
        session_map.set(key, "sid-that-no-longer-exists")
        session_map.set_flag(key, "temporary", True)
        assert session_map.prune() == 1
        assert key not in session_map._data

    def test_prune_keeps_an_opt_out_that_has_nothing_else_on_it(self, session_map):
        """``/unlink`` as the very first message writes exactly this shape.

        No ``sid``, no thread, no mirror — which is the stale predicate. Pruned,
        the setting silently reverts at the next restart and the user's next
        message lands on the default they had just switched off.
        """
        key = "telegram:kirocrew:direct:7"
        session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        assert session_map.prune() == 0
        assert session_map.get_flag(key, MIRROR_OPT_OUT_FLAG) is True

    def test_prune_clears_a_stale_sid_instead_of_dropping_the_opt_out(
        self, session_map, tmp_path
    ):
        """The other stale branch: the setting must outlive the native session.

        A conversation that HAS run turns carries a ``sid``. When kiro-cli
        garbage-collects that session file the entry is stale by the first
        predicate — and deleting it would take the opt-out with it, silently
        restoring mirroring on the next message.
        """
        key = "telegram:kirocrew:direct:7"
        session_map.set(key, "sid-that-no-longer-exists")
        session_map.set_flag(key, MIRROR_OPT_OUT_FLAG, True)
        assert session_map.prune() == 0
        assert session_map.get_flag(key, MIRROR_OPT_OUT_FLAG) is True
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()
        # The repair reached disk, so the next startup does not redo it.
        assert reloaded.get_flag(key, MIRROR_OPT_OUT_FLAG) is True
        assert not (reloaded._data.get(key) or {}).get("sid")


INBOUND_LINK = ChannelLink(channel_type="discord", channel_id="chan-1")


@pytest.fixture()
def unbind_calls():
    """Capture ``(key, link, reason)`` per announced removal, then unregister.

    The listener registry is module-level (a removal performed through a
    throwaway map must still be announced), so it is restored unconditionally —
    a leaked listener would fire on every later test in this worker.
    """
    calls: list[tuple[str, ChannelLink, str]] = []
    set_unbind_listener(lambda key, link, reason: calls.append((key, link, reason)))
    try:
        yield calls
    finally:
        set_unbind_listener(None)


@pytest.fixture()
def sel_events():
    """Capture every SEL event the map emits during a removal."""
    with patch("kiro_crew.session_map.sel") as fake_sel:
        fake_sel.return_value.log_api_access = MagicMock()
        yield fake_sel.return_value.log_api_access


def _inbound_audits(log_api_access):
    """The inbound-unbind events among everything captured."""
    return [
        call.kwargs
        for call in log_api_access.call_args_list
        if call.kwargs.get("operation") == "session.inbound_unbind"
    ]


class TestInboundUnbindIsLoud:
    """Every removal of an inbound resume binding is audited and announced.

    The binding is what routes a channel message back to an existing session, so
    losing it silently strands the conversation and nothing in the trail says which
    removal did it. These pin the choke point rather than the call sites, so a
    future caller inherits the behavior instead of having to remember it.
    """

    def test_clear_mirror_link_audits_and_announces(
        self, session_map, unbind_calls, sel_events
    ):
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        assert session_map.clear_mirror_link("dashboard:chat-1", reason="dashboard_unlink")

        assert unbind_calls == [("dashboard:chat-1", INBOUND_LINK, "dashboard_unlink")]
        audits = _inbound_audits(sel_events)
        assert len(audits) == 1
        assert "dashboard:chat-1" in audits[0]["resources"]
        assert "discord:chan-1" in audits[0]["resources"]
        assert "dashboard_unlink" in audits[0]["resources"]

    def test_clear_mirror_links_at_announces_each_loser(
        self, session_map, unbind_calls, sel_events
    ):
        """A location sweep can clear several sessions; each lost its own way back."""
        plant_binding(session_map, "dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        plant_binding(session_map, "dashboard:chat-2", INBOUND_LINK, accepts_inbound=True)

        cleared = session_map.clear_mirror_links_at(INBOUND_LINK, reason="user_unlink")

        assert sorted(cleared) == ["dashboard:chat-1", "dashboard:chat-2"]
        assert sorted(key for key, _, _ in unbind_calls) == [
            "dashboard:chat-1",
            "dashboard:chat-2",
        ]
        assert {reason for _, _, reason in unbind_calls} == {"user_unlink"}
        assert len(_inbound_audits(sel_events)) == 2

    @pytest.mark.parametrize(
        "removal, reason",
        [
            # An explicit clear through set_mirror_link(None).
            (lambda m, k: m.set_mirror_link(k, None, reason="dashboard_unlink"),
             "dashboard_unlink"),
            # An overwrite onto another location ends the old resume as thoroughly.
            (lambda m, k: m.set_mirror_link(
                k,
                ChannelLink(channel_type="discord", channel_id="chan-2"),
                accepts_inbound=True,
                reason="origin_rebind",
            ), "origin_rebind"),
            # Same location, inbound flag dropped: no longer resumable.
            (lambda m, k: m.set_mirror_link(k, INBOUND_LINK, reason="origin_rebind"),
             "origin_rebind"),
            # A whole-entry delete carrying its caller's reason.
            (lambda m, k: m.delete(k, reason="session_destroyed"), "session_destroyed"),
            # A caller that names none is recorded as unattributed, not skipped.
            (lambda m, k: m.clear_mirror_link(k), UNBIND_REASON_UNSPECIFIED),
        ],
    )
    def test_every_removal_shape_announces_with_its_reason(
        self, session_map, unbind_calls, removal, reason
    ):
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        removal(session_map, "dashboard:chat-1")

        assert unbind_calls == [("dashboard:chat-1", INBOUND_LINK, reason)]

    def test_rebinding_the_same_inbound_binding_is_not_a_removal(
        self, session_map, unbind_calls
    ):
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)

        assert unbind_calls == []

    def test_deleting_the_whole_entry_announces(self, session_map, unbind_calls, sel_events):
        """A dying entry takes its binding with it, so the entry path announces too."""
        session_map.set("dashboard:chat-1", "sid-1")
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)

        session_map.delete("dashboard:chat-1")

        assert unbind_calls == [("dashboard:chat-1", INBOUND_LINK, "entry_deleted")]
        assert len(_inbound_audits(sel_events)) == 1

    def test_prune_repairs_a_bound_entry_without_unbinding_it(self, session_map, unbind_calls):
        """A stale sid on a bound entry clears the sid; the binding is not a casualty.

        ``_survives_prune`` keeps any entry carrying a channel binding, so prune
        cannot reach an inbound binding at all — nothing is removed, so nothing is
        announced.
        """
        session_map.set("dashboard:chat-1", "sid-that-no-longer-exists")
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)

        assert session_map.prune() == 0
        assert session_map.get_mirror_link("dashboard:chat-1") == INBOUND_LINK
        assert session_map.mirror_accepts_inbound("dashboard:chat-1") is True
        assert unbind_calls == []


class TestPruneRemovesThroughTheChokePoint:
    """Prune's deletions are audited like every other entry removal.

    ``_survives_prune`` keeps every bound entry out of prune's delete branch, so
    today prune cannot reach a binding at all — which is precisely why this needs
    pinning rather than leaving to inspection. Prune used to delete straight out
    of ``_data``, so the audit and the announcement were not skipped by policy,
    they were simply unreachable: any future loosening of that predicate would
    have reopened a silent binding-removal path and nothing in the trail would
    have named prune as the remover.
    """

    def test_a_pruned_binding_is_audited_and_announced(
        self, session_map, unbind_calls, sel_events, monkeypatch
    ):
        """The latent path, made reachable: the removal still has to be loud."""
        key = "dashboard:chat-1"
        session_map.set(key, "sid-that-no-longer-exists")
        session_map.set_mirror_link(key, INBOUND_LINK, accepts_inbound=True)
        # Stand in for a future edit to the predicate: the entry becomes
        # collectable while still holding the binding prune would take with it.
        monkeypatch.setattr("kiro_crew.session_map._survives_prune", lambda entry: False)

        assert session_map.prune() == 1
        assert key not in session_map._data

        audits = _inbound_audits(sel_events)
        assert len(audits) == 1
        assert key in audits[0]["resources"]
        assert "discord:chan-1" in audits[0]["resources"]
        assert UNBIND_REASON_PRUNED_STALE in audits[0]["resources"]
        assert unbind_calls == [(key, INBOUND_LINK, UNBIND_REASON_PRUNED_STALE)]

    def test_collecting_an_unbound_row_stays_silent(
        self, session_map, unbind_calls, sel_events
    ):
        """The reachable case is unchanged: garbage strands nobody, so no event.

        Routing prune through the choke point must not start narrating ordinary
        collection — the audit exists for a lost binding, and a bare stale row
        holds none.
        """
        session_map.set("dashboard:chat-1", "sid-that-no-longer-exists")

        assert session_map.prune() == 1
        assert "dashboard:chat-1" not in session_map._data
        assert unbind_calls == []
        assert _inbound_audits(sel_events) == []

    @pytest.mark.asyncio
    async def test_startup_collection_defers_the_write_off_loop(self, session_map):
        """Prune on a running loop pays no inline whole-map write.

        ``_save`` defers the disk write to a worker thread precisely so the
        loop never blocks on serialization (its docstring cites #2405), and
        prune's sole caller is ``start_pool`` on the startup loop. Routing
        removals through the choke point must keep that property: per-key
        audits, zero loop-thread writes, one coalesced flush afterwards.
        """
        import kiro_crew.session_map as mod

        loop_thread = threading.current_thread()
        replace_threads: list[threading.Thread] = []
        real_replace = mod.os.replace

        def _recording_replace(src, dst, **kw):
            replace_threads.append(threading.current_thread())
            return real_replace(src, dst, **kw)

        # NOTE: mod.os IS the os module, so this patch is process-global, not
        # module-local. Harmless under xdist process isolation; do not widen.
        with patch.object(mod.os, "replace", side_effect=_recording_replace):
            for n in range(5):
                session_map.set(f"dashboard:chat-{n}", f"sid-gone-{n}")

            assert session_map.prune() == 5
            assert [t for t in replace_threads if t is loop_thread] == []
            while True:
                task = session_map._flush_task
                if task is None:
                    break
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                if task is session_map._flush_task:
                    break
            assert len(replace_threads) == 1
            assert replace_threads[0] is not loop_thread

    def test_a_collected_thread_binding_leaves_the_index(
        self, session_map, monkeypatch
    ):
        """The reverse index cannot outlive the entry that owned the thread."""
        key = "dashboard:chat-1"
        session_map.set_slack_link(key, "1700000000.000100", "C123")
        assert session_map.get_session_for_thread("1700000000.000100") == key
        monkeypatch.setattr("kiro_crew.session_map._survives_prune", lambda entry: False)

        assert session_map.prune() == 1
        assert session_map.get_session_for_thread("1700000000.000100") is None


class TestOutboundOnlyStaysQuiet:
    """An outbound-only mirror routes nothing back, so losing it strands nobody."""

    @pytest.mark.parametrize(
        "setup, remove",
        [
            # An outbound-only mirror, cleared by key and deleted with its entry;
            # a Slack binding (its own reverse index); and an inbound flag with no
            # mirror, which routes nothing and so is no loss.
            (lambda m: m.set_mirror_link("dashboard:chat-1", INBOUND_LINK),
             lambda m: m.clear_mirror_link("dashboard:chat-1")),
            (lambda m: m.set_mirror_link("dashboard:chat-1", INBOUND_LINK),
             lambda m: m.delete("dashboard:chat-1")),
            (lambda m: m.set_mirror_link(
                "dashboard:chat-1",
                ChannelLink(channel_type="slack", channel_id="C1", thread_id="ts-1"),
             ),
             lambda m: m.clear_mirror_link("dashboard:chat-1")),
            (lambda m: (m._ensure_entry("dashboard:chat-1").update(
                {"mirror_accepts_inbound": True}), m._save()),
             lambda m: m.delete("dashboard:chat-1")),
        ],
    )
    def test_losing_it_announces_nothing(
        self, session_map, unbind_calls, sel_events, setup, remove
    ):
        setup(session_map)
        remove(session_map)

        assert unbind_calls == []
        assert _inbound_audits(sel_events) == []

    def test_sweeping_outbound_mirrors_does_not_announce(self, session_map, unbind_calls):
        plant_binding(session_map, "dashboard:chat-1", INBOUND_LINK)
        plant_binding(session_map, "dashboard:chat-2", INBOUND_LINK)

        assert len(session_map.clear_mirror_links_at(INBOUND_LINK)) == 2
        assert unbind_calls == []


class TestAnnouncementIsBestEffort:
    """A broken notifier or audit sink cannot fail the removal that provoked it."""

    def test_listener_exception_is_swallowed_and_logged_at_warning(
        self, session_map, caplog
    ):
        def _explode(key, link, reason):
            raise RuntimeError("notifier down")

        set_unbind_listener(_explode)
        try:
            with caplog.at_level(logging.WARNING, logger="kiro_crew.session_map"):
                session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
                assert session_map.clear_mirror_link("dashboard:chat-1") is True
        finally:
            set_unbind_listener(None)
        # The removal still committed, and the failure is visible in production.
        assert session_map.get_mirror_link("dashboard:chat-1") is None
        assert any("listener failed" in r.message for r in caplog.records)

    def test_manager_registers_on_the_shared_registry(self, session_map, tmp_path):
        """A removal through a DIFFERENT map instance is announced too."""
        calls: list[str] = []
        _manager_over(session_map).set_unbind_listener(
            lambda key, link, reason: calls.append(key)
        )
        try:
            session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
            with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
                other = SessionMap()
            other.clear_mirror_link("dashboard:chat-1")
        finally:
            set_unbind_listener(None)

        assert calls == ["dashboard:chat-1"]


class TestAuditDoesNotBlockTheLoop:
    """The SEL write, ``sel()`` resolution included, must leave the loop free.

    ``sel()`` resolves the data home, creates the log and mints the HMAC trust root
    on its first call, and every write appends. ``SessionMap`` is called
    synchronously from coroutines, so doing that inline stalls the gateway.
    """

    @pytest.mark.asyncio
    async def test_the_loop_keeps_beating_while_the_audit_runs(
        self, session_map, unbind_calls
    ):
        """The clear must RETURN while the sink is still blocked.

        Timing the synchronous call is the only assertion that fails when ``sel()``
        is resolved on the caller's thread: a heartbeat measured after the call
        returns cannot tell a stall from a slow sink.
        """
        entered = threading.Event()
        release = threading.Event()

        def _blocking_sel():
            entered.set()
            # Long enough that an inline resolution cannot finish inside the
            # assertion below; released in the finally so nothing hangs.
            release.wait(30)
            return MagicMock()

        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        try:
            with patch("kiro_crew.session_map.sel", _blocking_sel):
                started = time.monotonic()
                session_map.clear_mirror_link("dashboard:chat-1", reason="dashboard_unlink")
                elapsed = time.monotonic() - started
                # The audit is in flight on the executor...
                assert await asyncio.to_thread(entered.wait, 10) is True
                # ...and the loop-side caller did not wait for it.
                assert elapsed < 1.0, f"clear blocked the caller for {elapsed:.1f}s"
                # The loop is still able to run work.
                beats = 0
                for _ in range(5):
                    await asyncio.sleep(0)
                    beats += 1
                assert beats == 5
        finally:
            release.set()

    @pytest.mark.asyncio
    async def test_an_audit_failure_is_isolated_and_logged_at_warning(
        self, session_map, caplog
    ):
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_map"):
            with patch("kiro_crew.session_map.sel", side_effect=RuntimeError("sel down")):
                # The removal still commits despite the broken sink.
                assert session_map.clear_mirror_link("dashboard:chat-1") is True
                for _ in range(200):
                    if any("audit failed" in r.message for r in caplog.records):
                        break
                    await asyncio.sleep(0.01)

        assert session_map.get_mirror_link("dashboard:chat-1") is None
        assert any("audit failed" in r.message for r in caplog.records)

    def test_off_loop_the_audit_runs_inline_exactly_once(self, session_map):
        """With no loop to protect there is nothing to offload to."""
        calls: list[dict] = []
        fake = MagicMock()
        fake.log_api_access = lambda **kw: calls.append(kw)

        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        with patch("kiro_crew.session_map.sel", return_value=fake):
            session_map.clear_mirror_link("dashboard:chat-1", reason="dashboard_unlink")

        assert len(calls) == 1
        assert calls[0]["operation"] == "session.inbound_unbind"
        assert "dashboard_unlink" in calls[0]["resources"]


class TestReasonIsNormalizedAtTheChokePoint:
    """An unexpected reason must not reach SEL or the notice copy."""

    def test_an_unknown_reason_is_normalized_and_warned(
        self, session_map, unbind_calls, caplog
    ):
        session_map.set_mirror_link("dashboard:chat-1", INBOUND_LINK, accepts_inbound=True)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.session_map"):
            session_map.clear_mirror_link("dashboard:chat-1", reason="totally_made_up")

        assert unbind_calls == [
            ("dashboard:chat-1", INBOUND_LINK, UNBIND_REASON_UNSPECIFIED)
        ]
        assert any("totally_made_up" in r.getMessage() for r in caplog.records)


class TestGetRepairsRatherThanUnbinds:
    """``get()``'s stale-entry path must not delete a bound conversation.

    Same hazard prune was corrected for: a garbage-collected session file leaves a
    stale ``sid``, and deleting the whole entry takes the channel binding with it.
    """

    @pytest.mark.parametrize(
        "plant, verify",
        [
            # A mirror binding (the inbound resume identity), a Slack thread, and a
            # durable per-conversation SETTING — each must outlive the session.
            (
                lambda m, k: m.set_mirror_link(k, INBOUND_LINK, accepts_inbound=True),
                lambda m, k: m.get_mirror_link(k) == INBOUND_LINK
                and m.mirror_accepts_inbound(k) is True,
            ),
            (
                lambda m, k: m.set_slack_link(k, "ts-1", "C1"),
                lambda m, k: m.get_slack_link(k) == ("ts-1", "C1"),
            ),
            (
                lambda m, k: m.set_flag(k, MIRROR_OPT_OUT_FLAG, True),
                lambda m, k: m.get_flag(k, MIRROR_OPT_OUT_FLAG) is True,
            ),
        ],
    )
    def test_state_that_must_outlive_the_session_survives(
        self, session_map, unbind_calls, plant, verify
    ):
        key = "dashboard:chat-1"
        session_map.set(key, "sid-that-no-longer-exists")
        plant(session_map, key)

        assert session_map.get(key) is None
        assert key in session_map._data
        assert not session_map._data[key]["sid"]
        assert verify(session_map, key)
        # Nothing was unbound, so nothing is audited or announced.
        assert unbind_calls == []

    def test_the_repair_reaches_disk(self, session_map, unbind_calls, tmp_path):
        key = "dashboard:chat-1"
        session_map.set(key, "sid-that-no-longer-exists")
        session_map.set_mirror_link(key, INBOUND_LINK, accepts_inbound=True)
        assert session_map.get(key) is None

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            reloaded = SessionMap()

        assert reloaded.get_mirror_link(key) == INBOUND_LINK
        assert unbind_calls == []

    def test_a_truly_unbound_stale_entry_is_still_collected(
        self, session_map, unbind_calls
    ):
        key = "dashboard:chat-1"
        session_map.set(key, "sid-gone")

        assert session_map.get(key) is None
        assert key not in session_map._data
        # It held no binding, so there is nothing to announce.
        assert unbind_calls == []
