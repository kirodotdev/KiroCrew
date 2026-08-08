"""Tests for a session bound to SEVERAL channels at once.

A session can mirror to Discord and Telegram simultaneously, mute either without
touching the other, and reconnect one while the other stays muted. One binding per
channel TYPE is the rule — a session never holds two Discord conversations,
because a conversation hosts one session and the reverse could not be addressed.

Two properties carry the most weight here:

1. **Legacy rows read forward without migration.** Every session map on disk today
   carries a single ``mirror`` dict plus entry-level ``mirror_accepts_inbound`` /
   ``mirror_paused`` flags. Those must keep working unread-modified, or an upgrade
   silently drops every existing channel binding.
2. **Mute is per binding.** A muted Discord binding must never silence a Telegram
   sibling that is still connected — which is why the unnamed ``is_mirror_paused``
   asks whether ALL bindings are muted rather than any.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.link import ChannelLink, canonical_key
from kiro_crew.session_map import SessionMap

KEY = "dashboard:chat-1"
DISCORD = ChannelLink(channel_type="discord", channel_id="1122334455")
TELEGRAM = ChannelLink(channel_type="telegram", channel_id="99887766")


@pytest.fixture()
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestSeveralBindings:
    def test_two_channels_coexist(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        assert session_map.get_mirror_links(KEY) == [DISCORD, TELEGRAM]

    def test_binding_one_channel_does_not_disturb_the_other(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD, accepts_inbound=True)
        session_map.set_mirror_link(KEY, TELEGRAM)
        assert session_map.mirror_accepts_inbound(KEY, "discord") is True
        assert session_map.mirror_accepts_inbound(KEY, "telegram") is False

    def test_rebinding_a_channel_replaces_only_that_one(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        moved = ChannelLink(channel_type="discord", channel_id="5555555555")
        session_map.set_mirror_link(KEY, moved)
        assert session_map.get_mirror_link(KEY, "discord") == moved
        assert session_map.get_mirror_link(KEY, "telegram") == TELEGRAM

    def test_clearing_one_channel_leaves_the_other_bound(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        assert session_map.clear_mirror_link(KEY, "discord") is True
        assert session_map.get_mirror_links(KEY) == [TELEGRAM]

    def test_clearing_with_no_channel_clears_them_all(self, session_map):
        """The historical single-binding contract: no argument means everything."""
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        assert session_map.clear_mirror_link(KEY) is True
        assert session_map.get_mirror_links(KEY) == []

    def test_unnamed_get_refuses_to_pick_among_several(self, session_map):
        """A caller that assumes one binding must not silently act on a sibling."""
        session_map.set_mirror_link(KEY, DISCORD)
        assert session_map.get_mirror_link(KEY) == DISCORD
        session_map.set_mirror_link(KEY, TELEGRAM)
        assert session_map.get_mirror_link(KEY) is None


class TestMuteIsPerBinding:
    def test_muting_one_leaves_the_other_delivering(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        session_map.set_mirror_paused(KEY, True, "discord")
        assert session_map.is_mirror_paused(KEY, "discord") is True
        assert session_map.is_mirror_paused(KEY, "telegram") is False

    def test_the_unnamed_read_is_all_not_any(self, session_map):
        """The outbound gate keys off this: one muted channel must not silence a
        sibling that is still connected."""
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        session_map.set_mirror_paused(KEY, True, "discord")
        assert session_map.is_mirror_paused(KEY) is False
        session_map.set_mirror_paused(KEY, True, "telegram")
        assert session_map.is_mirror_paused(KEY) is True

    def test_reconnecting_one_leaves_the_other_muted(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)
        session_map.set_mirror_paused(KEY, True)
        session_map.set_mirror_paused(KEY, False, "discord")
        assert session_map.is_mirror_paused(KEY, "discord") is False
        assert session_map.is_mirror_paused(KEY, "telegram") is True

    def test_clearing_a_binding_takes_its_mute_with_it(self, session_map):
        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_paused(KEY, True, "discord")
        session_map.clear_mirror_link(KEY, "discord")
        session_map.set_mirror_link(KEY, DISCORD)
        assert session_map.is_mirror_paused(KEY, "discord") is False

    def test_a_muted_binding_still_occupies_its_location(self, session_map):
        """Conflict detection and in-channel unlink both key off this."""
        session_map.set_mirror_link(KEY, DISCORD, accepts_inbound=True)
        session_map.set_mirror_paused(KEY, True, "discord")
        assert session_map.find_mirror_sessions(DISCORD) == [KEY]
        assert session_map.find_mirror_sessions(DISCORD, inbound_only=True) == [KEY]


class TestMuteNeverOutlivesItsLink:
    """A `slack_paused` flag must not survive its link and mute the next one.

    `clear_slack_link` pops the flag, but `set_slack_link(key, "", "")` — how a
    Slack-side handoff releases the previous owner — empties the coordinates and
    leaves it. The next thread bound to that row would be born muted.
    """

    def test_a_thread_bound_after_a_release_is_not_born_muted(self, session_map):
        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")
        session_map.set_slack_paused(KEY, True)
        assert session_map.is_slack_paused(KEY) is True

        # The handoff release: coordinates cleared, flag left behind.
        session_map.set_slack_link(KEY, "", "")
        # A fresh thread arrives on the same row.
        session_map.set_slack_link(KEY, "1700000999.9", "C_OTHER")

        assert session_map.is_slack_paused(KEY) is False

    def test_a_live_link_that_is_muted_still_reads_muted(self, session_map):
        """The guard must not swallow a legitimate mute."""
        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")
        session_map.set_slack_paused(KEY, True)

        assert session_map.is_slack_paused(KEY) is True


class TestNamingAChannelNeverTouchesSlack:
    """A channel-scoped clear must not fall through to the Slack link.

    The fall-through exists for the UNNAMED clear ("release everything"). Once
    `release_conversation_location` began naming the location's channel, a channel
    holding no binding reached that fall-through and disconnected Slack instead —
    a conversation nobody mentioned in a command about a different one.
    """

    def test_unlinking_an_unbound_channel_leaves_slack_connected(self, session_map):
        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")

        assert session_map.clear_mirror_link(KEY, "discord") is False

        assert session_map.get_slack_link(KEY) == ("1700000000.1", "C_TEAM")

    def test_in_channel_unlink_on_an_unbound_channel_spares_slack(self, session_map):
        """The whole path, not just the accessor: this is the reachable route."""
        from kiro_crew.messaging.link import release_conversation_location

        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")

        release_conversation_location(
            session_map, key=KEY, location=DISCORD, channel="discord"
        )

        assert session_map.get_slack_link(KEY) == ("1700000000.1", "C_TEAM")

    def test_naming_slack_still_clears_the_slack_link(self, session_map):
        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")

        assert session_map.clear_mirror_link(KEY, "slack") is True

        assert session_map.get_slack_link(KEY) == (None, None)

    def test_the_unnamed_clear_keeps_its_slack_fallback(self, session_map):
        """Unnamed means every binding, which historically included Slack."""
        session_map.set_slack_link(KEY, "1700000000.1", "C_TEAM")

        assert session_map.clear_mirror_link(KEY) is True

        assert session_map.get_slack_link(KEY) == (None, None)


class TestLegacyRowConsolidation:
    """Adding a channel must not orphan a binding held on the legacy row.

    Bindings written before key unification sit on the sanitized
    `dashboard:`-spelled row, and `_mirror_key` resolves reads there ONLY while
    the canonical row has none. Under single-binding, writing to the canonical row
    was a deliberate supersession — the new binding replaced the old. With several
    bindings that same write makes the canonical row win, and the legacy row's
    binding disappears from delivery and from the UI while still occupying its
    location by value. So a write consolidates instead of superseding.
    """

    LEGACY_KEY = "dashboard:discord_kirocrew_direct_42"
    CHANNEL_KEY = "discord:kirocrew:direct:42"

    def _legacy_bound(self, session_map):
        session_map._data[self.LEGACY_KEY] = {
            "sid": "",
            "mirror": {"channel_type": "discord", "channel_id": "1122334455"},
            "mirror_accepts_inbound": True,
        }

    def test_adding_a_second_channel_keeps_the_legacy_binding_visible(self, session_map):
        self._legacy_bound(session_map)
        assert session_map.get_mirror_links(self.CHANNEL_KEY) == [DISCORD]

        session_map.set_mirror_link(self.CHANNEL_KEY, TELEGRAM)

        # Both, not just the one just written.
        assert session_map.get_mirror_links(self.CHANNEL_KEY) == [DISCORD, TELEGRAM]
        assert session_map.mirror_accepts_inbound(self.CHANNEL_KEY, "discord") is True

    def test_the_consolidated_binding_is_not_double_counted_by_location(self, session_map):
        """Leaving a copy on the legacy row would report the session twice in every
        by-location scan, and the inbound resolver refuses to pick from duplicates."""
        self._legacy_bound(session_map)
        session_map.set_mirror_link(self.CHANNEL_KEY, TELEGRAM)
        assert session_map.find_mirror_sessions(DISCORD) == [
            canonical_key(self.CHANNEL_KEY)
        ]


class TestUnlinkIsScopedToItsConversation:
    """An in-channel unlink frees THIS conversation, not the whole session.

    `release_conversation_location` documents itself as "nothing mirrors into this
    conversation"; calling the key-addressed clear unnamed made it mean "nothing
    mirrors anywhere", so a Discord `!unlink` erased a Telegram binding nobody
    mentioned.
    """

    def test_unlinking_one_conversation_leaves_the_other_channel_bound(self, session_map):
        from kiro_crew.messaging.link import release_conversation_location

        session_map.set_mirror_link(KEY, DISCORD, accepts_inbound=True)
        session_map.set_mirror_link(KEY, TELEGRAM)

        release_conversation_location(
            session_map, key=KEY, location=DISCORD, channel="discord"
        )

        assert session_map.get_mirror_links(KEY) == [TELEGRAM]

    def test_it_still_frees_the_conversation_it_names(self, session_map):
        from kiro_crew.messaging.link import release_conversation_location

        session_map.set_mirror_link(KEY, DISCORD, accepts_inbound=True)
        session_map.set_mirror_link(KEY, TELEGRAM)

        release_conversation_location(
            session_map, key=KEY, location=DISCORD, channel="discord"
        )

        assert session_map.find_mirror_sessions(DISCORD) == []


class TestLegacySingleBindingCompat:
    """Every session map written before multi-bind carries the old shape."""

    @staticmethod
    def _legacy(session_map, **extra):
        session_map._data[KEY] = {
            "sid": "",
            "mirror": {"channel_type": "discord", "channel_id": "1122334455"},
            **extra,
        }

    def test_a_legacy_binding_is_readable(self, session_map):
        self._legacy(session_map)
        assert session_map.get_mirror_links(KEY) == [DISCORD]
        assert session_map.get_mirror_link(KEY, "discord") == DISCORD

    def test_a_legacy_inbound_flag_is_readable(self, session_map):
        self._legacy(session_map, mirror_accepts_inbound=True)
        assert session_map.mirror_accepts_inbound(KEY, "discord") is True
        assert session_map.find_mirror_sessions(DISCORD, inbound_only=True) == [KEY]

    def test_a_legacy_mute_is_readable(self, session_map):
        self._legacy(session_map, mirror_paused=True)
        assert session_map.is_mirror_paused(KEY, "discord") is True
        assert session_map.is_mirror_paused(KEY) is True

    def test_reading_a_legacy_row_does_not_rewrite_it(self, session_map):
        """Reads must not migrate: a read path that writes turns any listing into
        a disk mutation, and a crash mid-listing into a half-migrated file."""
        self._legacy(session_map, mirror_paused=True)
        session_map.get_mirror_links(KEY)
        session_map.is_mirror_paused(KEY)
        assert "mirror" in session_map._data[KEY]
        assert "mirrors" not in session_map._data[KEY]

    def test_writing_migrates_and_retires_the_legacy_keys(self, session_map):
        self._legacy(session_map, mirror_accepts_inbound=True, mirror_paused=True)
        session_map.set_mirror_link(KEY, TELEGRAM)
        entry = session_map._data[KEY]
        assert "mirror" not in entry
        assert "mirror_accepts_inbound" not in entry
        assert "mirror_paused" not in entry
        # The legacy binding survives the migration alongside the new one, with
        # its own flags carried across rather than dropped.
        assert entry["mirrors"]["discord"]["accepts_inbound"] is True
        assert entry["mirrors"]["discord"]["paused"] is True
        assert entry["mirrors"]["telegram"]["channel_id"] == "99887766"

    def test_a_legacy_row_survives_a_reload(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            first = SessionMap()
            self._legacy(first)
            first._save()
            assert SessionMap().get_mirror_links(KEY) == [DISCORD]


class TestConcurrentBindingWritesDoNotDiscardEachOther:
    """Two channels connected at the same time must both survive.

    Every binding mutator is a read-modify-write of the session map ending in a
    full-map `_save()`. Those calls are dispatched through `asyncio.to_thread` (a
    save must not block the gateway loop), so the concurrency is REAL: without a
    lock, two connects each read the same `mirrors` dict, each add their own
    channel, and the later write silently discards the earlier binding — while both
    callers see success.
    """

    def test_parallel_connects_to_different_channels_both_persist(self, session_map):
        """Smoke check: the ordinary concurrent path keeps every binding.

        Note this alone does NOT prove the lock is doing anything — CPython's GIL
        makes the lost-update window so small that 12 unsynchronised runs never lost
        it. The next test widens the window so the invariant is actually tested.
        """
        import concurrent.futures

        channels = [
            ChannelLink(channel_type=name, channel_id=f"conv-{i}")
            for i, name in enumerate(
                ["discord", "telegram", "whatsapp", "webex", "wecom", "teams"]
            )
        ]

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(channels)) as pool:
            list(pool.map(lambda ln: session_map.set_mirror_link(KEY, ln), channels))

        persisted = {link.channel_type for link in session_map.get_mirror_links(KEY)}
        assert persisted == {c.channel_type for c in channels}, (
            f"a concurrent connect was discarded: kept {sorted(persisted)}"
        )

    def test_the_lost_update_window_is_closed(self, session_map):
        """Force the interleaving the GIL usually hides, and assert nothing is lost.

        The read-modify-write window is real but tiny, so a plain thread race is not a
        test — it passes with or without synchronisation. Delaying INSIDE the mutator
        (between its read of `mirrors` and its write-back) makes the window
        observable: serialised, each connect sees the other's binding and both
        survive; unserialised, both read the same dict and the later write discards
        the earlier binding.
        """
        import concurrent.futures
        import time

        original_write = session_map._write_mirrors

        def _slow_write(entry, mirrors):
            # Inside the mutator, therefore inside the lock when one is held.
            time.sleep(0.05)
            return original_write(entry, mirrors)

        session_map._write_mirrors = _slow_write  # type: ignore[method-assign]
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(
                    lambda ln: session_map.set_mirror_link(KEY, ln), [DISCORD, TELEGRAM]
                ))
        finally:
            session_map._write_mirrors = original_write  # type: ignore[method-assign]

        kept = {link.channel_type for link in session_map.get_mirror_links(KEY)}
        assert kept == {"discord", "telegram"}, (
            f"a concurrent binding was lost to an unsynchronised read-modify-write: "
            f"kept {sorted(kept)}"
        )

    def test_parallel_mutes_do_not_resurrect_a_cleared_binding(self, session_map):
        """Mixed mutators race too — mute and clear are the same read-modify-write."""
        import concurrent.futures

        session_map.set_mirror_link(KEY, DISCORD)
        session_map.set_mirror_link(KEY, TELEGRAM)

        def _work(which):
            if which == "mute":
                session_map.set_mirror_paused(KEY, True, "discord")
            else:
                session_map.clear_mirror_link(KEY, "telegram")

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(_work, ["mute", "clear"]))

        kept = {link.channel_type for link in session_map.get_mirror_links(KEY)}
        assert kept == {"discord"}, f"expected only discord to remain, got {sorted(kept)}"
        assert session_map.is_mirror_paused(KEY, "discord") is True


class TestInChannelMutationsStayOffTheEventLoop:
    """`!link` / `!unlink` must not write the session map synchronously.

    These handlers were safe when every mutator ran on the event loop. This branch
    put `_mutate_lock` around them and dispatches the dashboard's writes through
    `asyncio.to_thread`, so a worker can now hold that lock across a whole `_save`.
    A synchronous call from the loop then waits on a disk write, stalling every other
    gateway task and the heartbeat with it.

    Asserted on the CALL SITES rather than on a helper existing: `"to_thread" in src`
    is satisfied by any single unrelated use elsewhere in the module.
    """

    SITES = (
        "await asyncio.to_thread(\n                self.sessions.set_mirror_link,",
        "await asyncio.to_thread(\n            self.sessions.clear_mirror_link,",
        "await asyncio.to_thread(\n            release_conversation_location,",
    )

    def _source(self, module):
        import inspect

        return inspect.getsource(module)

    def test_discord_offloads_every_in_channel_write(self):
        from kiro_crew.discord import transport_dispatch

        src = self._source(transport_dispatch)
        for site in self.SITES:
            assert site in src, f"discord writes the map on the event loop: {site!r}"
        assert "\n            self.sessions.set_mirror_link(" not in src, (
            "a synchronous set_mirror_link survives in the Discord handler"
        )

    def test_telegram_offloads_every_in_channel_write(self):
        from kiro_crew.telegram import transport_dispatch

        src = self._source(transport_dispatch)
        for site in self.SITES:
            assert site in src, f"telegram writes the map on the event loop: {site!r}"
        assert "\n        self.sessions.clear_mirror_link(" not in src, (
            "a synchronous clear_mirror_link survives in the Telegram handler"
        )
