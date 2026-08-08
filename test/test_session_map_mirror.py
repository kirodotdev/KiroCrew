"""Tests for SessionMap channel-neutral outbound mirror binding.

Covers the C1 generalization of the Slack-only dashboard->channel mirror into a
channel-agnostic ``ChannelLink`` binding: non-Slack targets are stored under
``mirror``; Slack routes back through the dedicated slack-link fields (keeping
its reverse index intact); legacy Slack sessions surface as a synthesized
Slack ``ChannelLink`` without needing migration.
"""

from __future__ import annotations

import json
import os
import threading
from unittest.mock import patch

import pytest

from kiro_crew.messaging.link import (
    ChannelLink,
    legacy_dashboard_mirror_key,
    release_conversation_location,
)
from kiro_crew.session_map import ConversationOwnershipConflict, SessionMap


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

    def test_stored_under_mirrors_keyed_by_channel_type(self, session_map):
        """One binding per channel type, so the map is keyed by it.

        This asserts the on-disk SHAPE, which changed when a session became able
        to hold several bindings. The legacy single-``mirror`` shape is still
        read (see TestLegacySingleBindingCompat) — it is simply no longer written.
        """
        session_map.set_mirror_link(
            "dashboard:chat-1", ChannelLink(channel_type="telegram", channel_id="99")
        )
        entry = session_map._data["dashboard:chat-1"]
        assert entry["mirrors"]["telegram"]["channel_id"] == "99"
        assert "mirror" not in entry

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
        # Written straight into the map, because `set_mirror_link` now REFUSES to
        # create this state (see TestInboundOwnershipIsExclusive below). The reader
        # contract still has to hold for it: a map file written before that check
        # existed can carry two inbound owners, and the reader must report BOTH so
        # the resolver refuses to pick and conflict detection can see it — silently
        # resolving to one is how a reply reaches the wrong session.
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        for key in ("dashboard:chat-1", "dashboard:chat-2"):
            session_map._data[key] = {
                "mirrors": {"discord": {**link.to_dict(), "accepts_inbound": True}}
            }

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
        #
        # Written RAW: `set_mirror_link` now refuses a second session at one
        # location, so this state can no longer be created through the writer.
        # It is still reachable — a map file written before that check, or a row
        # stranded under a rotated spelling — and the sweep must free all of it.
        for _key in (
            "discord:agent:direct:u1",
            "dashboard:discord_agent_direct_u1",
            "dashboard:chat-3",
        ):
            session_map._data[_key] = {
                "mirrors": {self.LINK.channel_type: dict(self.LINK.to_dict())}
            }
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
        #
        # Raw for the same reason as the clear test above: the writer now refuses
        # to put a second session on one conversation, but a file written before
        # that check can still hold this shape.
        for _key in (self.KEY, f"{self.KEY}:gen1", "dashboard:chat-9"):
            session_map._data[_key] = {
                "mirrors": {self.LINK.channel_type: dict(self.LINK.to_dict())}
            }
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


class TestInboundOwnershipIsExclusive:
    """At most one key may own inbound at a conversation, enforced atomically.

    Both claimants — the dashboard connect endpoint and the Discord
    session-selection button — precheck occupancy and then write, but under
    DIFFERENT locks, so their prechecks can both pass before either writes. The
    check inside `set_mirror_link` runs while `_mutate_lock` is held, which is the
    one mutex both writers pass through, so check-and-claim is atomic there.
    """

    def test_a_second_session_cannot_claim_inbound_at_the_same_conversation(
        self, session_map
    ):
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        # The refusal leaves the incumbent untouched — no partial write.
        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-1"
        ]
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_a_takeover_still_works_because_it_evicts_before_claiming(self, session_map):
        """The check must refuse a LOST RACE, not a legitimate takeover.

        The connect endpoint clears the location and then claims it, so by the time
        it writes no rival holds the conversation. If this test ever fails, the
        atomic check has started deleting the product requirement that a user may
        take a conversation from another session after confirming.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        session_map.clear_mirror_links_at(link)
        session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        assert session_map.find_mirror_sessions(link, inbound_only=True) == [
            "dashboard:chat-2"
        ]

    def test_a_session_may_reclaim_its_own_conversation(self, session_map):
        """A reconnect re-asserts `accepts_inbound` on a binding it already owns."""
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:chat-1", True, "discord")

        # Must not raise: the only inbound owner here is this very key.
        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)

        assert session_map.is_mirror_paused("dashboard:chat-1", "discord") is not True

    def test_a_legacy_row_holding_the_binding_counts_as_the_same_session(
        self, session_map
    ):
        """The legacy `dashboard:`-spelled row is SELF, not a rival.

        A channel session's binding may still sit on the pre-unification
        `dashboard:`-spelled row; this same writer consolidates it onto the
        canonical row, so at check time it is still on the legacy spelling. Reading
        that as another owner would refuse the session's own reconnect. Uses a
        CHANNEL session key because that is the only shape where the legacy fallback
        applies (`SessionMap._mirror_key` gates it on `is_channel_session_key`).
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        key = "discord:dm-1"
        session_map._data[legacy_dashboard_mirror_key(key)] = {
            "mirrors": {"discord": {**link.to_dict(), "accepts_inbound": True}}
        }

        session_map.set_mirror_link(key, link, accepts_inbound=True)

        # Consolidated onto the canonical row, and still exactly one owner.
        assert session_map.find_mirror_sessions(link, inbound_only=True) == [key]

    def test_outbound_claims_are_exclusive_too(self, session_map):
        """One session per conversation, whichever direction the binding routes.

        This replaces an assertion that two OUTBOUND bindings could share a location.
        That rule was justified on the grounds that an outbound binding does not own
        replies, so it cannot make a message unroutable — true, but it misses the
        actual damage: two sessions delivering into one conversation interleave their
        transcripts into it. It also stopped covering the dashboard's Telegram connect
        the moment `accepts_inbound` became Discord-only, which is the claim that most
        needed the backstop.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)  # outbound only

        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", link)

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_accepts_inbound_only_sets_the_marker(self, session_map):
        """The flag no longer gates ownership — it does what its name says.

        Ownership is refused for an outbound claim as well, so the only thing left
        for the flag to decide is whether this binding is a session-RESUME target.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")

        session_map.set_mirror_link("dashboard:chat-1", link)
        assert session_map.mirror_accepts_inbound("dashboard:chat-1", "discord") is False

        session_map.set_mirror_link("dashboard:chat-1", link, accepts_inbound=True)
        assert session_map.mirror_accepts_inbound("dashboard:chat-1", "discord") is True

    def test_a_session_may_still_rewrite_its_own_binding(self, session_map):
        """Self is not a rival — an outbound rebind of one's own link must pass."""
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)

        session_map.set_mirror_link("dashboard:chat-1", link)  # must not raise

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]

    def test_an_outbound_occupant_also_blocks_an_inbound_claim(self, session_map):
        """The atomic check must agree with the endpoint precheck on "occupied".

        The precheck uses the unfiltered `find_mirror_sessions(link)`, so it treats a
        plain outbound binding as an occupant and asks for confirmation. Filtering to
        inbound here made this backstop weaker than the gate it backs: an outbound
        binding arriving in the window (a concurrent Discord `!link`) was invisible,
        and the claim landed beside it — two sessions delivering into one conversation.
        """
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)  # outbound only

        with pytest.raises(ConversationOwnershipConflict):
            session_map.set_mirror_link("dashboard:chat-2", link, accepts_inbound=True)

        # Refused cleanly: the outbound occupant is untouched and no partial write.
        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-1"]
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_a_confirmed_takeover_still_displaces_an_outbound_occupant(self, session_map):
        """Stricter occupancy must not cost the takeover: eviction clears all kinds."""
        link = ChannelLink(channel_type="discord", channel_id="dm-1")
        session_map.set_mirror_link("dashboard:chat-1", link)  # outbound only

        session_map.replace_mirror_owner("dashboard:chat-2", link, accepts_inbound=True)

        assert session_map.find_mirror_sessions(link) == ["dashboard:chat-2"]


class TestTheTakeoverLeavesNoVacancy:
    """Eviction and replacement are ONE mutation, so the location is never free.

    As two calls — clear, then claim — a confirmed takeover briefly left the
    conversation with no owner. The Discord picker could claim that vacancy, and the
    takeover was then refused by the exclusivity check while the evicted binding
    stayed deleted: the user lost their link and nobody gained one.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_the_conversation_has_an_owner_at_every_observable_point(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        # Observe from inside the mutation: `_write_mirrors` runs while the lock is
        # held, so a reader here sees exactly the intermediate states a rival would.
        seen: list[list[str]] = []
        original = session_map._write_mirrors

        def _spy(entry, mirrors):
            result = original(entry, mirrors)
            seen.append(session_map.find_mirror_sessions(self.LINK))
            return result

        session_map._write_mirrors = _spy  # type: ignore[method-assign]
        try:
            displaced = session_map.replace_mirror_owner(
                "dashboard:chat-2", self.LINK, accepts_inbound=True
            )
        finally:
            session_map._write_mirrors = original  # type: ignore[method-assign]

        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-2"]
        assert [("dashboard:chat-1", self.LINK, True, False)] == [
            (k, ln, inb, p) for k, ln, inb, p in displaced
        ]
        assert seen, "the spy never observed an intermediate write"

    def test_the_displaced_binding_comes_back_with_its_flags(self, session_map):
        """The snapshot must carry the mute, or a failed takeover un-mutes a channel."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:chat-1", True, "discord")

        displaced = session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert displaced == [("dashboard:chat-1", self.LINK, True, True)], (
            "the mute did not travel with the snapshot, so a rollback would "
            "silently reconnect a muted binding"
        )

    def test_a_refused_claim_puts_the_eviction_back(self, session_map):
        """All-or-nothing: the caller must never inherit a half-done takeover.

        There is only ONE save for the takeover itself now — the eviction and the
        claim are staged together and published with a single rebind — so this no
        longer needs to pick out "the second save". Failing that save is the whole
        failure surface: the change is in memory but not on disk, and the undo has to
        put the evicted owner back rather than leave the caller a takeover it never
        learned about. (The undo then saves on its own account, so counting saves
        across the whole call proves nothing; that no PERSISTED state is ever
        ownerless is asserted by `TestTheTakeoverIsDurablyAtomic`.)
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        original = session_map._save

        def _fail_the_publish():
            raise OSError("disk full")

        session_map._save = _fail_the_publish  # type: ignore[method-assign]
        try:
            with pytest.raises(OSError):
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
        finally:
            session_map._save = original  # type: ignore[method-assign]

        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-1"], (
            "the evicted owner was not restored after the claim was refused"
        )

    def test_an_unreadable_occupant_is_still_evicted(self, session_map):
        """Eviction follows occupancy, not snapshot readability.

        An occupant whose binding cannot be read is still holding the location;
        skipping its eviction would leave it there and get the claim refused.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        session_map.get_mirror_link = lambda *a, **k: None  # type: ignore[method-assign]

        displaced = session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert displaced == []
        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-2"]


class TestARivalCannotLandInsideTheTakeover:
    """The real protection: a rival WRITE cannot land between eviction and claim.

    A rival that merely READS the gap is harmless — reads take no lock, but the
    exclusivity check inside the claim catches it and it is refused. What must be
    impossible is a rival WRITE committing inside the window, because then the
    takeover is refused, its rollback restores the previous owner, and the
    conversation ends up with TWO inbound owners: the one that slipped in and the
    one that was put back.

    Needs real threads and a widened window: the window exists but is far too small
    to lose naturally, so a plain race would pass with or without the fix.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_exactly_one_owner_survives_a_concurrent_claim(self, session_map):
        import concurrent.futures
        import time

        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        original_clear = session_map.clear_mirror_links_at

        def _slow_clear(link):
            # Delay AFTER the eviction returns, i.e. exactly in the evict→claim gap.
            # Placed here deliberately: a sleep inside `_write_mirrors` would run
            # while an inner mutator still holds the lock, so it would not widen the
            # window this test is about and the test would pass either way.
            result = original_clear(link)
            time.sleep(0.05)
            return result

        session_map.clear_mirror_links_at = _slow_clear  # type: ignore[method-assign]

        def _takeover():
            try:
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
                return None
            except ConversationOwnershipConflict as exc:
                return exc

        def _rival():
            time.sleep(0.02)  # aim for the middle of the gap
            try:
                session_map.set_mirror_link(
                    "dashboard:chat-3", self.LINK, accepts_inbound=True
                )
            except ConversationOwnershipConflict:
                pass

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                takeover = pool.submit(_takeover)
                rival = pool.submit(_rival)
                refusal = takeover.result()
                rival.result()
        finally:
            session_map.clear_mirror_links_at = original_clear  # type: ignore[method-assign]

        # The CONFIRMED takeover must win. Serialised, the rival simply arrives after
        # it and is refused. Un-serialised, the rival claims the vacancy the takeover
        # itself opened — so the takeover is refused by its own eviction, and the
        # rival ends up owning a conversation the user handed to someone else.
        assert refusal is None, (
            "the takeover was refused because its own eviction left a vacancy for "
            "the rival to claim"
        )
        owners = session_map.find_mirror_sessions(self.LINK, inbound_only=True)
        assert owners == ["dashboard:chat-2"], (
            f"the confirmed takeover did not win the conversation: owners={sorted(owners)}"
        )


class TestReadersSurviveConcurrentWriters:
    """A reader must not crash because a worker thread is mutating the map.

    Mutators now run on `asyncio.to_thread` and hold `_mutate_lock`; readers hold
    nothing. `_ensure_entry` adds keys to `_data`, so iterating the live top-level
    mapping can raise `RuntimeError: dictionary changed size during iteration` — and
    the reader on the Discord inbound path is `find_mirror_sessions`, so that crash
    drops a user's message rather than merely logging.

    Readers therefore iterate a shallow SNAPSHOT of `_data`. Locking them would also
    be correct but would put a reader on the event loop behind a worker doing file
    I/O in `_save`, which is exactly what moving the writes off the loop avoided.

    Only the top level needs this. `_write_mirrors` installs a NEW bindings dict by
    rebinding `entry["mirrors"]`, so the inner dict a binding reader walks is never
    mutated in place — verified by reverting a snapshot there and finding no test
    could distinguish it, because there is no defect to catch.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_find_mirror_sessions_does_not_crash_while_keys_are_added(self, session_map):
        import threading
        import time

        session_map.set_mirror_link("dashboard:chat-0", self.LINK, accepts_inbound=True)

        # Hold the iteration OPEN across a real window. Without this the loop over a
        # small dict finishes inside one GIL slice and the race is never lost, so the
        # test passes with or without the snapshot (it did, 5 runs out of 5).
        # `_mirrors` is called once per entry, i.e. inside the loop body.
        original_mirrors = session_map._mirrors

        def _slow_mirrors(entry):
            time.sleep(0.002)
            return original_mirrors(entry)

        session_map._mirrors = _slow_mirrors  # type: ignore[method-assign]

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer():
            i = 0
            while not stop.is_set() and i < 500:
                # A brand-new key each time: changing the dict SIZE is what makes a
                # live iteration raise.
                session_map._data[f"dashboard:filler-{i}"] = {"sid": ""}
                i += 1
                time.sleep(0.001)

        def _reader():
            try:
                for _ in range(20):
                    session_map.find_mirror_sessions(self.LINK)
            except BaseException as exc:  # noqa: BLE001 - recorded and re-asserted
                errors.append(exc)

        writer = threading.Thread(target=_writer)
        reader = threading.Thread(target=_reader)
        try:
            writer.start()
            reader.start()
            reader.join()
            stop.set()
            writer.join()
        finally:
            session_map._mirrors = original_mirrors  # type: ignore[method-assign]

        assert not errors, f"a reader crashed against a concurrent writer: {errors!r}"

    def test_the_snapshot_is_of_the_mapping_not_a_deep_copy(self, session_map):
        """Cheap by design: entries are shared, only the key list is private.

        A deep copy per read would be a real cost on a hot path. Sharing the entry
        objects is safe because writers replace an entry's bindings by rebinding a
        fresh dict rather than mutating the one a reader holds.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        entry = session_map._data["dashboard:chat-1"]
        found = session_map.find_mirror_sessions(self.LINK)

        assert found == ["dashboard:chat-1"]
        assert session_map._data["dashboard:chat-1"] is entry

    def test_the_binding_readers_are_safe_too(self, session_map):
        """`_mirrors` is the choke point every binding reader passes through."""
        import threading

        key = "dashboard:chat-1"
        session_map.set_mirror_link(key, self.LINK, accepts_inbound=True)

        stop = threading.Event()
        errors: list[BaseException] = []

        def _writer():
            i = 0
            while not stop.is_set() and i < 400:
                # Same session, different channel types: this mutates the INNER
                # `mirrors` dict that the binding readers walk.
                session_map.set_mirror_link(
                    key, ChannelLink(channel_type=f"ch{i % 7}", channel_id="x")
                )
                i += 1

        def _reader():
            try:
                for _ in range(400):
                    session_map.mirror_accepts_inbound(key)
                    session_map.is_mirror_paused(key, "")
                    session_map.get_mirror_links(key)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        writer = threading.Thread(target=_writer)
        reader = threading.Thread(target=_reader)
        writer.start()
        reader.start()
        reader.join()
        stop.set()
        writer.join()

        assert not errors, f"a binding reader crashed against a writer: {errors!r}"


class TestTheTakeoverIsDurablyAtomic:
    """A confirmed takeover must never be observable ON DISK as a vacancy.

    In-memory atomicity under `_mutate_lock` is not enough: the compound mutator was
    built from primitives that each save, so the eviction was already durable when
    the claim ran. A process that exited in between left the previous binding
    permanently deleted and no new owner — the user loses a link and nobody gains
    one, and unlike the in-memory race a restart does not heal it.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_the_whole_takeover_lands_in_one_write(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        # Counted at `os.replace`, the atomic commit point — NOT at `_save`, which is
        # still CALLED by each inner mutator and merely returns early while the
        # deferral is active. Counting calls would report 3 here and prove nothing
        # about how many durable states existed.
        with patch(
            "kiro_crew.session_map.os.replace", side_effect=os.replace
        ) as commits:
            session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert commits.call_count == 1, (
            f"the takeover committed {commits.call_count} on-disk states; every extra "
            f"one is a state a crash could freeze"
        )

    def test_no_persisted_state_ever_shows_the_conversation_unowned(self, session_map):
        """The property itself, read back from the FILE at every write.

        Stronger than counting writes: it reads what a restarting process would load
        after each save and asserts the conversation always has exactly one owner.
        """
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        seen: list[list[str]] = []
        # Wraps `_flush`, not `_save`: `_save` only STAGES a serialised snapshot
        # under `_mutate_lock` now (so the lock is never held across the disk write),
        # and `_flush` is what actually replaces the file. Wrapping `_save` observed
        # the file BEFORE the write it was supposed to be checking.
        original = session_map._flush

        def _observing_save(epoch, version, payload):
            result = original(epoch, version, payload)
            # Exactly what a fresh process would see.
            on_disk = json.loads(session_map._path.read_text(encoding="utf-8"))
            owners = [
                key for key, entry in on_disk.items()
                if any(
                    b.get("channel_type") == self.LINK.channel_type
                    and b.get("channel_id") == self.LINK.channel_id
                    for b in (entry.get("mirrors") or {}).values()
                )
            ]
            seen.append(owners)
            return result

        session_map._flush = _observing_save  # type: ignore[method-assign]
        try:
            session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)
        finally:
            session_map._flush = original  # type: ignore[method-assign]

        assert seen, "no write was observed at all"
        for owners in seen:
            assert owners, (
                "a persisted state had the conversation unowned — a crash there loses "
                f"the binding permanently; observed sequence: {seen}"
            )
        assert seen[-1] == ["dashboard:chat-2"]

    def test_a_plain_single_mutator_still_saves_normally(self, session_map):
        """The coalescing must not swallow ordinary writes."""
        with patch(
            "kiro_crew.session_map.os.replace", side_effect=os.replace
        ) as commits:
            session_map.set_mirror_link(
                "dashboard:chat-1", self.LINK, accepts_inbound=True
            )

        assert commits.call_count == 1
        assert session_map._save_depth == 0
        assert session_map._save_pending is False


class TestAFailedFinalWriteDoesNotStrandTheEvictedSession:
    """The one deferred write happens on EXIT, after the inner rollback.

    Coalescing the takeover into a single save moved the write to the end of the
    block — past the handler that undoes a refused claim. A failure there escaped
    carrying the only copy of `displaced` with it: the caller's snapshot stayed
    empty, so its own rollback cleared the claimant and restored nobody, and the
    session that had been evicted was left unbound for a takeover that never landed.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-1")

    def test_the_previous_owner_is_restored_when_the_write_fails(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:chat-1", True, "discord")

        # Fail the COMMIT itself, which is the only way to fail the single deferred
        # write without also breaking the restore path's bookkeeping.
        with patch("kiro_crew.session_map.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        # In memory — which is what the caller and the next reader see — the previous
        # owner is back, with its mute, and the claimant holds nothing.
        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-1"], (
            "the evicted session was left unbound after a failed takeover"
        )
        assert session_map.is_mirror_paused("dashboard:chat-1", "discord") is True, (
            "the restore silently reconnected a binding the user had muted"
        )
        assert session_map.get_mirror_link("dashboard:chat-2") is None

    def test_the_failure_still_propagates(self, session_map):
        """Undoing must not swallow the error — the caller has to answer for it."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        with patch("kiro_crew.session_map.os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

    def test_a_healthy_takeover_is_untouched_by_the_guard(self, session_map):
        """Non-vacuity: the added try must not swallow the success path."""
        session_map.set_mirror_link("dashboard:chat-1", self.LINK, accepts_inbound=True)

        displaced = session_map.replace_mirror_owner("dashboard:chat-2", self.LINK)

        assert displaced == [("dashboard:chat-1", self.LINK, True, False)]
        assert session_map.find_mirror_sessions(self.LINK) == ["dashboard:chat-2"]


class TestEveryMapWriterIsSynchronized:
    """`@_synchronized` is an invariant, not a convention.

    Design Review's point: the map is now a hand-rolled transaction layer (an RLock,
    `_deferred_save` coalescing, a compensating `_undo_takeover`) and every future
    mutator has to REMEMBER the decorator. A missed one reintroduces a silent
    lost-update with no test that would generically catch it. So this asserts the
    property over the whole class instead of trusting the next author to notice.
    """

    # `_deferred_save` is the coalescing mechanism itself: it is only ever entered
    # from a method that already holds the lock, and it is what the decorated
    # compound mutators defer INTO. Every other writer must be decorated.
    EXEMPT = {"_deferred_save"}

    def _writers(self):
        import ast
        import inspect

        from kiro_crew import session_map

        tree = ast.parse(inspect.getsource(session_map))
        cls = next(
            n
            for n in tree.body
            if isinstance(n, ast.ClassDef) and n.name == "SessionMap"
        )
        out = []
        for node in cls.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            writes = any(
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr in {"_save", "_deferred_save"}
                and isinstance(c.func.value, ast.Name)
                and c.func.value.id == "self"
                for c in ast.walk(node)
            )
            if not writes:
                continue
            guarded = any(
                isinstance(d, ast.Name) and d.id == "_synchronized"
                for d in node.decorator_list
            )
            out.append((node.name, guarded))
        return out

    def test_the_detector_actually_finds_the_writers(self):
        """Anti-vacuity guard: a broken detector would pass the test below silently."""
        writers = self._writers()
        assert len(writers) >= 18, (
            f"only {len(writers)} map writers detected — the AST walk broke, so the "
            "invariant below is passing without checking anything"
        )

    def test_every_writer_holds_the_mutate_lock(self):
        bare = sorted(n for n, guarded in self._writers() if not guarded)
        assert bare == sorted(self.EXEMPT), (
            f"these SessionMap methods write the map without @_synchronized: {bare}. "
            "Two mutators dispatched through asyncio.to_thread can interleave, each "
            "reading the same dict and writing back its own version, so the later "
            "write silently discards the earlier one while both callers see success."
        )


class TestTheTakeoverRollbackIsOneMutation:
    """A rollback composed from the public writers loses the evicted binding.

    The endpoint's conversation lock does not cover this: it lives in chat_mirror,
    so the Discord and Telegram in-channel `/link` handlers never take it. A claim
    arriving between "clear the claimant" and "restore the occupant" makes the
    restore refuse — exclusivity is checked on every claim now — and the refusal is
    swallowed per occupant, so the user's binding simply disappears. Doing the whole
    compensation under one `_mutate_lock` hold is what closes the window.
    """

    LOC = ChannelLink("discord", channel_id="dm-race")

    def test_a_rival_claim_cannot_cost_the_evicted_session_its_binding(
        self, session_map
    ):
        sm = session_map
        sm.set_mirror_link("dashboard:victim", self.LOC, accepts_inbound=True)
        sm.set_mirror_paused("dashboard:victim", True, "discord")

        displaced = sm.replace_mirror_owner("dashboard:taker", self.LOC)
        assert [d[0] for d in displaced] == ["dashboard:victim"]

        # A rival grabs the location before the failed takeover can be undone — the
        # in-channel path, which holds no conversation lock. It has to go in raw:
        # the writer would (correctly) refuse a second session at one location.
        # `thread_id` copied from the link, not spelled "": `ChannelLink` is a
        # dataclass, so None != "" and the occupancy scan compares by VALUE — a row
        # with the wrong spelling is invisible there and the rival would not exist.
        sm._data.setdefault("dashboard:rival", {})["mirrors"] = {
            "discord": {
                "channel_id": self.LOC.channel_id,
                "thread_id": self.LOC.thread_id,
            }
        }
        assert "dashboard:rival" in sm.find_mirror_sessions(self.LOC), (
            "the injected rival is invisible to the occupancy scan, so the race this "
            "test describes cannot happen and the assertions below prove nothing"
        )

        sm.restore_mirror_owner("dashboard:taker", self.LOC, displaced)

        assert sm.get_mirror_link("dashboard:victim", "discord") is not None, (
            "the evicted session lost its binding to a rival that only got in "
            "because the takeover had the location transiently vacant"
        )
        assert sm.is_mirror_paused("dashboard:victim", "discord") is True, (
            "the binding came back CONNECTED for a user who had muted it"
        )
        assert sm.get_mirror_link("dashboard:taker", "discord") is None, (
            "the failed takeover kept its claim"
        )

    def test_it_restores_the_claimants_own_previous_binding_too(self, session_map):
        sm = session_map
        elsewhere = ChannelLink("discord", channel_id="dm-elsewhere")
        sm.set_mirror_link("dashboard:taker", elsewhere, accepts_inbound=True)
        sm.set_mirror_paused("dashboard:taker", True, "discord")
        sm.set_mirror_link("dashboard:victim", self.LOC, accepts_inbound=True)

        displaced = sm.replace_mirror_owner("dashboard:taker", self.LOC)
        sm.restore_mirror_owner(
            "dashboard:taker", self.LOC, displaced, (elsewhere, True, True)
        )

        restored = sm.get_mirror_link("dashboard:taker", "discord")
        assert restored is not None and restored.channel_id == "dm-elsewhere", (
            f"the claimant's own prior binding was not put back: {restored}"
        )
        assert sm.is_mirror_paused("dashboard:taker", "discord") is True, (
            "a failed connect silently un-muted a channel the user had muted"
        )

    def test_a_refused_claim_does_not_clear_an_innocent_rival(self, session_map):
        """Nothing displaced means nothing to hand back — touch only our own key."""
        sm = session_map
        sm.set_mirror_link("dashboard:holder", self.LOC, accepts_inbound=True)

        # Our claim never landed, so `displaced` is empty.
        sm.restore_mirror_owner("dashboard:taker", self.LOC, [])

        assert sm.get_mirror_link("dashboard:holder", "discord") is not None, (
            "the rollback for a connect that never happened deleted the binding of "
            "a session it never evicted"
        )


class TestNoReaderEverSeesTheConversationUnowned:
    """Writers are serialised; readers are deliberately lock-free.

    `_mutate_lock` stops two WRITERS interleaving, but it does not stop a reader
    looking in mid-mutation — and that is the dangerous observer here. Between the
    eviction and the claim the conversation has no owner, and `resumed_session`
    treats "no owner" exactly like "not resumed": the inbound reply runs under the
    conversation's own native session, with the wrong history. Reordering does not
    help, because that resolver also returns None on AMBIGUITY, so claim-first just
    trades an ownerless window for an ambiguous one.

    So the takeover stages its whole change and rebinds `_data` once. This samples
    the map from a READER's point of view at every step a writer takes, and requires
    an owner at all times.
    """

    LOC = ChannelLink("discord", channel_id="dm-atomic")

    def test_a_reader_sampling_throughout_always_finds_an_owner(self, session_map):
        session_map.set_mirror_link("dashboard:old", self.LOC, accepts_inbound=True)

        seen: list[list[str]] = []
        real_write = session_map._write_mirrors

        def _sample_after_every_entry_write(entry, mirrors):
            result = real_write(entry, mirrors)
            # A lock-free reader can land here, between any two entry rewrites.
            seen.append(session_map.find_mirror_sessions(self.LOC))
            return result

        session_map._write_mirrors = _sample_after_every_entry_write  # type: ignore[method-assign]
        try:
            session_map.replace_mirror_owner("dashboard:new", self.LOC)
        finally:
            session_map._write_mirrors = real_write  # type: ignore[method-assign]

        assert seen, "the probe never fired, so this asserts nothing"
        assert all(owners for owners in seen), (
            "a reader saw this conversation with NO owner mid-takeover, which routes "
            f"an inbound reply under the wrong session; samples: {seen}"
        )
        assert session_map.find_mirror_sessions(self.LOC) == ["dashboard:new"], (
            "the takeover did not land"
        )


class TestTheStagedExclusivityCheck:
    """`_rival_at` asks the exclusivity question about an ARBITRARY map.

    The takeover has to ask it about the STAGED copy, before anything is published,
    so the question cannot be delegated to `set_mirror_link` (which asks about live
    `_data` and saves). One implementation shared by both paths, so the staged answer
    cannot drift from the direct one.

    Pinned directly because the branch that consumes it inside `replace_mirror_owner`
    is a backstop for a rival that occupies the location while exposing no evictable
    binding, and I could not construct that state faithfully — both scans match by
    value, so they agree. Asserting the helper is honest; asserting the backstop
    through a contrived map would not be.
    """

    LOC = ChannelLink("discord", channel_id="dm-rival")

    def test_it_finds_another_session_at_the_location(self, session_map):
        session_map.set_mirror_link("dashboard:holder", self.LOC)
        assert (
            session_map._rival_at(session_map._data, "dashboard:mine", self.LOC)
            == "dashboard:holder"
        )

    def test_it_does_not_report_the_claimant_itself(self, session_map):
        session_map.set_mirror_link("dashboard:mine", self.LOC)
        assert session_map._rival_at(session_map._data, "dashboard:mine", self.LOC) is None

    def test_it_ignores_a_binding_on_a_DIFFERENT_conversation(self, session_map):
        session_map.set_mirror_link(
            "dashboard:holder", ChannelLink("discord", channel_id="dm-elsewhere")
        )
        assert session_map._rival_at(session_map._data, "dashboard:mine", self.LOC) is None

    def test_it_answers_about_the_map_it_is_GIVEN(self, session_map):
        """The whole point: a staged copy, not whatever `_data` currently holds."""
        session_map.set_mirror_link("dashboard:holder", self.LOC)
        staged = session_map._staged_data()
        staged.pop("dashboard:holder", None)
        assert session_map._rival_at(staged, "dashboard:mine", self.LOC) is None, (
            "the check read live `_data` instead of the staged map, so a takeover "
            "would refuse itself over an occupant its own staging had just evicted"
        )


class TestTheMutationLockIsNeverHeldAcrossTheDisk:
    """`_mutate_lock` guards memory; the file write happens after it is released.

    Mutators are dispatched through `asyncio.to_thread`, and ~15 of them are still
    called synchronously on the event loop. While the write sat inside the critical
    section, one of those on-loop calls could wait on a worker's mkstemp + json.dump
    + os.replace and stall the gateway and its heartbeat with it. Offloading those
    callers one at a time would leave the same trap set for the next one, so the I/O
    moved out of the lock instead — which fixes every caller, including future ones.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-lock")

    def test_the_lock_is_free_while_the_write_happens(self, session_map):
        held: list[bool] = []
        real_flush = session_map._flush

        def _flush_checking_the_lock(epoch, version, payload):
            # Another THREAD must be able to take the lock while this write runs.
            acquired = []

            def _try():
                acquired.append(session_map._mutate_lock.acquire(timeout=2))
                if acquired[-1]:
                    session_map._mutate_lock.release()

            probe = threading.Thread(target=_try)
            probe.start()
            probe.join(5)
            held.append(not acquired or not acquired[0])
            return real_flush(epoch, version, payload)

        session_map._flush = _flush_checking_the_lock  # type: ignore[method-assign]
        try:
            session_map.set_mirror_link("dashboard:chat-1", self.LINK)
        finally:
            session_map._flush = real_flush  # type: ignore[method-assign]

        assert held, "no write happened, so this asserts nothing"
        assert not any(held), (
            "the mutation lock was still held during the file write, so a mutator "
            "called on the event loop can block on a worker's disk write"
        )

    def test_an_older_snapshot_never_lands_on_a_newer_one(self, session_map):
        """Two writes can now be in flight at once, so the write is version-guarded."""
        session_map.set_mirror_link("dashboard:winner", self.LINK)
        newest = session_map._flushed

        # A straggler carrying an older version must be dropped, not written.
        session_map._flush(
            session_map._epoch, newest - 1,
            json.dumps({"dashboard:stale": {"sid": "x"}}),
        )

        on_disk = json.loads(session_map._path.read_text(encoding="utf-8"))
        assert "dashboard:stale" not in on_disk, (
            "an older snapshot overwrote a newer one, so a restart reads a state that "
            "never existed"
        )


class TestAFailedWriteCannotRideAConcurrentMutationToDisk:
    """Preserving the newer mutation was how a failed write became durable.

    Two mutations can be at the write stage at once, and the newer one's payload was
    serialised from memory that already contained the older one. So when the older
    write failed, sparing the newer mutation left the failed change in memory — and the
    newer flush then wrote BOTH to disk, making durable a mutation whose caller had been
    told it failed. Memory is rolled back unconditionally now, and the epoch bump
    withdraws the newer staged payload so its caller is told it failed too, rather than
    having it silently carry the failure to disk.
    """

    OLD = ChannelLink(channel_type="discord", channel_id="dm-old")
    NEW = ChannelLink(channel_type="telegram", channel_id="dm-new")

    def test_the_newer_payload_is_refused_after_an_older_write_fails(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.OLD)
        durable_before = session_map._durable

        # A newer mutation staged but NOT yet flushed: memory holds it, disk does not.
        # Written raw so it does not flush on its own — going through the writer would
        # put it in `_durable` too, which is what let an earlier version of this test
        # pass with no mechanism at all.
        entry = session_map._data["dashboard:chat-1"]
        mirrors = session_map._mirrors(entry)
        mirrors[self.NEW.channel_type] = self.NEW.to_dict()
        session_map._write_mirrors(entry, mirrors)
        session_map._version += 1
        newer = (session_map._epoch, session_map._version, json.dumps(session_map._data))

        # The older write reports failure now.
        session_map._restore_durable()

        assert session_map.get_mirror_link("dashboard:chat-1", "telegram") is None, (
            "the failed mutation was left in memory, so the newer write would carry "
            "it to disk after its caller was told it failed"
        )
        with pytest.raises(RuntimeError):
            session_map._flush(*newer)
        assert session_map._durable == durable_before, (
            "the withdrawn payload reached disk anyway"
        )

    def test_the_newest_failure_still_rolls_back(self, session_map):
        """The mechanism must not disable the rollback it builds on."""
        session_map.set_mirror_link("dashboard:chat-1", self.OLD)

        with patch(
            "kiro_crew.session_map.os.replace", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                session_map.set_mirror_link("dashboard:chat-1", self.NEW)

        assert session_map.get_mirror_link("dashboard:chat-1", "telegram") is None, (
            "memory kept a binding whose bytes never reached disk, so the next "
            "successful write would make it durable after the fact"
        )
        assert session_map.get_mirror_link("dashboard:chat-1", "discord") is not None, (
            "the rollback discarded the last state that DID reach disk"
        )


class TestASetupFailureStillRollsBack:
    """A full disk fails at `mkstemp` as readily as at `os.replace`.

    The tempfile setup sat before the protected block, so failing there skipped the
    rollback entirely: memory kept a mutation whose bytes never landed, and the next
    successful save would persist an operation this call had already reported failed.
    """

    OLD = ChannelLink(channel_type="discord", channel_id="dm-setup-old")
    NEW = ChannelLink(channel_type="telegram", channel_id="dm-setup-new")

    def test_a_failed_mkstemp_rolls_memory_back(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.OLD)

        with patch(
            "kiro_crew.session_map.tempfile.mkstemp", side_effect=OSError("no space")
        ):
            with pytest.raises(OSError):
                session_map.set_mirror_link("dashboard:chat-1", self.NEW)

        assert session_map.get_mirror_link("dashboard:chat-1", "telegram") is None, (
            "a mutation whose write never even opened a temp file stayed in memory, "
            "so the next successful save would persist it"
        )
        assert session_map.get_mirror_link("dashboard:chat-1", "discord") is not None, (
            "the rollback discarded the last state that DID reach disk"
        )

    def test_a_failed_mkdir_rolls_memory_back_too(self, session_map):
        session_map.set_mirror_link("dashboard:chat-1", self.OLD)

        with patch(
            "kiro_crew.session_map.Path.mkdir", side_effect=OSError("no space")
        ):
            with pytest.raises(OSError):
                session_map.set_mirror_link("dashboard:chat-1", self.NEW)

        assert session_map.get_mirror_link("dashboard:chat-1", "telegram") is None, (
            "a mutation that failed before the temp file existed stayed in memory"
        )


class TestTheUndoOnlyClearsTheClaimItIsUndoing:
    """`clear_mirror_link(key, channel_type)` is broader than the claim being undone.

    Two connects for the SAME session to DIFFERENT conversations on one channel take
    different conversation locks, so both can be in flight. Clearing by channel alone
    let the earlier attempt's rollback delete the later attempt's successful binding.
    """

    FIRST = ChannelLink(channel_type="discord", channel_id="dm-first")
    SECOND = ChannelLink(channel_type="discord", channel_id="dm-second")

    def test_it_leaves_a_newer_binding_on_the_same_channel_alone(self, session_map):
        # The later connect won and its binding is live.
        session_map.set_mirror_link("dashboard:chat-1", self.SECOND)

        # The earlier connect now rolls back, undoing a claim on a DIFFERENT
        # conversation. It displaced nobody, so it takes the narrow-clear path.
        session_map._undo_takeover("dashboard:chat-1", self.FIRST, [])

        current = session_map.get_mirror_link("dashboard:chat-1", "discord")
        assert current is not None and current.channel_id == "dm-second", (
            "an earlier attempt's rollback deleted the binding a later, successful "
            f"connect had established: {current}"
        )

    def test_it_still_clears_its_own_claim(self, session_map):
        """The narrowing must not disable the clear it is narrowing."""
        session_map.set_mirror_link("dashboard:chat-1", self.FIRST)

        session_map._undo_takeover("dashboard:chat-1", self.FIRST, [])

        assert session_map.get_mirror_link("dashboard:chat-1", "discord") is None, (
            "the rollback left its own partial claim in place"
        )


class TestTheUndoDoesNotResurrectARemovedBinding:
    """A failed operation is no reason to overrule a user who unlinked.

    An in-channel `!unlink` during the catch-up REMOVES this session's binding. The
    rollback then put its prior link back, resurrecting something the user had
    explicitly deleted. Restoring the EVICTED OCCUPANTS stays unconditional: that
    undoes damage this call did to other sessions, and the user unlinking us says
    nothing about wanting them gone.
    """

    LOC = ChannelLink(channel_type="discord", channel_id="dm-res")
    BEFORE = ChannelLink(channel_type="discord", channel_id="dm-before")

    def test_a_previous_binding_is_not_restored_after_an_unlink(self, session_map):
        session_map.set_mirror_link("dashboard:taker", self.BEFORE)
        session_map.set_mirror_link("dashboard:victim", self.LOC)
        displaced = session_map.replace_mirror_owner("dashboard:taker", self.LOC)

        # The user unlinks mid-flight: our claim is gone.
        session_map.clear_mirror_link("dashboard:taker", "discord")

        session_map.restore_mirror_owner(
            "dashboard:taker", self.LOC, displaced, (self.BEFORE, False, False)
        )

        assert session_map.get_mirror_link("dashboard:taker", "discord") is None, (
            "the rollback resurrected a binding the user had explicitly unlinked"
        )
        assert session_map.get_mirror_link("dashboard:victim", "discord") is not None, (
            "the evicted session was left unbound — restoring IT is not conditional "
            "on the claimant still being connected"
        )

    def test_a_previous_binding_IS_restored_when_the_claim_is_intact(self, session_map):
        """The guard must not disable the restore it is narrowing."""
        session_map.set_mirror_link("dashboard:taker", self.BEFORE)
        session_map.set_mirror_link("dashboard:victim", self.LOC)
        displaced = session_map.replace_mirror_owner("dashboard:taker", self.LOC)

        session_map.restore_mirror_owner(
            "dashboard:taker", self.LOC, displaced, (self.BEFORE, False, False)
        )

        restored = session_map.get_mirror_link("dashboard:taker", "discord")
        assert restored is not None and restored.channel_id == "dm-before", (
            f"the claimant's own prior binding was not put back: {restored}"
        )


class TestTheVeryFirstWriteCanRollBackToo:
    """A fresh data home has no file to load, so there is no earlier durable state.

    `_durable` started as None there, and the rollback bailed out on None — which
    made the FIRST mutation the one case that could not roll back, exactly when there
    is nothing else on disk to fall back to. Seeded with the serialised empty map.
    """

    LINK = ChannelLink(channel_type="discord", channel_id="dm-first-write")

    def test_a_failed_first_write_does_not_stay_in_memory(self, session_map):
        # Nothing has ever been written by this map.
        assert session_map._flushed == 0

        with patch(
            "kiro_crew.session_map.os.replace", side_effect=OSError("disk full")
        ):
            with pytest.raises(OSError):
                session_map.set_mirror_link("dashboard:chat-1", self.LINK)

        assert session_map.get_mirror_link("dashboard:chat-1", "discord") is None, (
            "the first mutation's failed write stayed in memory, so the next "
            "successful write would persist a state the caller was told had failed"
        )


class TestAFailedTakeoverWriteStillRestoresTheEvictedOwner:
    """The write happens after the mutator's frame is gone, so its rollback data is too.

    `replace_mirror_owner` returns `displaced` so a caller whose delivery fails can put
    the evicted session back. Moving the write out of the critical section also moved it
    out of that frame's try/except: a failed write now raises from the decorator, the
    caller never receives `displaced`, and the evicted owner stays evicted with the
    claimant removed. The mutator registers its own compensation for that case.
    """

    LOC = ChannelLink(channel_type="discord", channel_id="dm-flushfail")

    def test_the_evicted_owner_comes_back_when_the_write_fails(self, session_map):
        """With a CONCURRENT mutation, so the durable rollback correctly stands aside.

        Without the concurrency this passes via `_restore_durable`, which rewinds memory
        to the last state on disk — and that state already has the victim bound, so the
        test says nothing about the compensation. `_restore_durable` is version-guarded
        and skips once a newer mutation exists (restoring then would delete it), and
        that is precisely when the takeover's own undo is the only thing that can put
        the evicted session back.
        """
        session_map.set_mirror_link("dashboard:victim", self.LOC, accepts_inbound=True)
        session_map.set_mirror_paused("dashboard:victim", True, "discord")

        def _fail_and_advance(*_args, **_kwargs):
            # A newer mutation lands while this write is in flight.
            session_map._version += 1
            raise OSError("disk full")

        with patch("kiro_crew.session_map.os.replace", side_effect=_fail_and_advance):
            with pytest.raises(OSError):
                session_map.replace_mirror_owner("dashboard:taker", self.LOC)

        assert session_map.find_mirror_sessions(self.LOC) == ["dashboard:victim"], (
            "the failed takeover write left the claimant removed and the evicted "
            "session still evicted — nobody owns the conversation"
        )
        assert session_map.is_mirror_paused("dashboard:victim", "discord") is True, (
            "the restore silently reconnected a binding the user had muted"
        )


class TestTwoRealThreadsCannotPersistAFailedMutation:
    """The reviewer's scenario, run with real threads rather than by hand.

    Write A fails while B — whose payload was serialised from memory containing A —
    is at the write stage. Before, B's flush wrote A+B and A's change was durable
    despite A raising.
    """

    A = ChannelLink(channel_type="discord", channel_id="dm-a")
    B = ChannelLink(channel_type="telegram", channel_id="dm-b")

    def test_the_failed_mutation_is_on_neither_disk_nor_in_memory(self, session_map):
        session_map.set_mirror_link("dashboard:base", ChannelLink("wecom", "seed"))

        b_staged = threading.Event()
        a_in_flush = threading.Event()
        real_replace = os.replace
        real_flush = session_map._flush

        def _flush(epoch, version, payload):
            # Entry to `_flush` is the point where the payload is already serialised
            # but the file has not been touched — B signals HERE, then blocks on the
            # io lock A is holding. Signalling after `set_mirror_link` returned would
            # never fire: B is parked inside this call for as long as A holds it.
            if threading.current_thread().name == "writer-b":
                b_staged.set()
            return real_flush(epoch, version, payload)

        def _replace(src, dst):
            if threading.current_thread().name == "writer-a":
                a_in_flush.set()
                assert b_staged.wait(timeout=10), "B never staged a payload"
                raise OSError("disk full")
            return real_replace(src, dst)

        errors: dict[str, BaseException | None] = {"a": None, "b": None}

        def _write(name, key, link):
            if name == "b":
                a_in_flush.wait(timeout=10)
            try:
                session_map.set_mirror_link(key, link)
            except BaseException as exc:  # noqa: BLE001 - recorded for the assertion
                errors[name] = exc

        session_map._flush = _flush  # type: ignore[method-assign]
        try:
            with patch("kiro_crew.session_map.os.replace", side_effect=_replace):
                ta = threading.Thread(
                    target=_write, args=("a", "dashboard:a", self.A), name="writer-a"
                )
                tb = threading.Thread(
                    target=_write, args=("b", "dashboard:b", self.B), name="writer-b"
                )
                ta.start()
                tb.start()
                ta.join(timeout=20)
                tb.join(timeout=20)
        finally:
            session_map._flush = real_flush  # type: ignore[method-assign]

        assert b_staged.is_set(), "the race never happened; the test proved nothing"
        assert errors["a"] is not None, "A's write failed but A reported success"
        assert errors["b"] is not None, (
            "B's payload contained A's failed mutation, so B must fail too rather "
            "than quietly carrying it to disk"
        )
        on_disk = json.loads(session_map._path.read_text(encoding="utf-8"))
        assert "dashboard:a" not in on_disk, (
            "A reported failure yet its mutation is durable — it rode B's payload "
            "to disk"
        )
        assert session_map.get_mirror_link("dashboard:a") is None, (
            "A's failed mutation is still in memory, so the next write persists it"
        )
