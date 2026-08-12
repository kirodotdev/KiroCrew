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
from kiro_crew.session import SessionManager, _opt_out_key
from kiro_crew.session_map import MIRROR_OPT_OUT_FLAG, ConversationOwnershipConflict, SessionMap


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
