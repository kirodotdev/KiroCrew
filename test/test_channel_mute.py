"""Tests for the channel-neutral mute: a bound channel that is not receiving.

Disconnecting a channel in the dashboard mutes it and KEEPS the binding, so the
conversation still resolves to this session and reconnecting picks it back up.
That makes "muted" the only thing separating a bound-but-quiet channel from an
unbound one, and these tests pin the three properties that separation depends on:

1. A mute never outlives the binding it describes. Both clear paths and a rebind
   drop it, or a future binding at the same key would be born silently muted.
2. A mute never exists without a binding. ``set_mirror_paused`` refuses to create
   an entry, so a session that mirrors nowhere cannot accrue a stray flag.
3. Routing is untouched. ``find_mirror_sessions``, the resume-conflict check and
   in-channel ``!unlink`` must all still see a muted link, or conflict detection
   and the in-channel escape hatch both break.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import SessionMap

KEY = "dashboard:chat-1"
LINK = ChannelLink(channel_type="discord", channel_id="1122334455", thread_id=None)


@pytest.fixture()
def session_map(tmp_path):
    with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
        yield SessionMap()


class TestStorage:
    def test_mute_round_trip_reports_previous_state(self, session_map):
        session_map.set_mirror_link(KEY, LINK)
        assert session_map.is_mirror_paused(KEY) is False
        # Returns the PREVIOUS state, so an idempotent endpoint needs no re-read.
        assert session_map.set_mirror_paused(KEY, True) is False
        assert session_map.is_mirror_paused(KEY) is True
        assert session_map.set_mirror_paused(KEY, True) is True

    def test_unmute_removes_the_key_rather_than_storing_false(self, session_map):
        """House presence-flag style: True or absent, never a false-valued key."""
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.set_mirror_paused(KEY, False) is True
        assert "mirror_paused" not in session_map._data[KEY]

    def test_a_session_with_no_mirror_cannot_be_muted(self, session_map):
        """A mute with no binding would silently mute whatever is bound next."""
        assert session_map.set_mirror_paused(KEY, True) is False
        assert session_map.is_mirror_paused(KEY) is False
        assert KEY not in session_map._data

    def test_an_entry_without_a_mirror_cannot_be_muted(self, session_map):
        """Same rule when the entry exists for another reason (a Slack thread)."""
        session_map.set_slack_link(KEY, "ts-1", "C-1")
        assert session_map.set_mirror_paused(KEY, True) is False
        assert "mirror_paused" not in session_map._data[KEY]


class TestTheMuteNeverOutlivesItsBinding:
    def test_clear_mirror_link_drops_the_mute(self, session_map):
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.clear_mirror_link(KEY) is True
        assert session_map.is_mirror_paused(KEY) is False

    def test_clear_by_location_drops_the_mute(self, session_map):
        """The in-channel `!unlink` path clears by VALUE, not by key."""
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.clear_mirror_links_at(LINK) == [KEY]
        assert session_map.is_mirror_paused(KEY) is False

    def test_a_rebind_is_not_born_muted(self, session_map):
        """set_mirror_link replaces the binding, so it must not inherit the mute."""
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        session_map.set_mirror_link(KEY, ChannelLink("discord", "9988776655"))
        assert session_map.is_mirror_paused(KEY) is False

    def test_a_rebind_to_the_same_location_is_not_born_muted(self, session_map):
        """The reconnect shape: same coordinates, and it must come back live."""
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        session_map.set_mirror_link(KEY, LINK)
        assert session_map.is_mirror_paused(KEY) is False


class TestRoutingIsUntouched:
    def test_a_muted_link_still_resolves_by_location(self, session_map):
        """`!unlink` and the resume-conflict check both key off this lookup.

        If a mute hid the link here, a muted conversation would look free: a
        second session could bind it, and the in-channel escape hatch the code
        tells the user to run ("Run `!unlink` first") would report nothing to
        clear.
        """
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.find_mirror_sessions(LINK) == [KEY]

    def test_a_muted_link_is_still_returned_by_get_mirror_link(self, session_map):
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.get_mirror_link(KEY) == LINK

    def test_a_muted_two_way_binding_stays_inbound_capable(self, session_map):
        """Muting is about egress. Inbound is what makes a reply reconnect it."""
        session_map.set_mirror_link(KEY, LINK, accepts_inbound=True)
        session_map.set_mirror_paused(KEY, True)
        assert session_map.mirror_accepts_inbound(KEY) is True
        assert session_map.find_mirror_sessions(LINK, inbound_only=True) == [KEY]


class TestPersistence:
    def test_the_mute_survives_a_reload(self, tmp_path):
        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            first = SessionMap()
            first.set_mirror_link(KEY, LINK)
            first.set_mirror_paused(KEY, True)
            assert SessionMap().is_mirror_paused(KEY) is True

    def test_slack_and_channel_mutes_are_separate_stores(self, session_map):
        """One session can hold a Slack thread and a channel mirror at once, and
        muting one must not read as muting the other."""
        session_map.set_mirror_link(KEY, LINK)
        session_map.set_slack_link(KEY, "ts-1", "C-1")
        session_map.set_mirror_paused(KEY, True)
        assert session_map.is_mirror_paused(KEY) is True
        assert session_map.is_slack_paused(KEY) is False


class TestTheGate:
    """``mirror_is_paused`` demands identity with True, not truthiness.

    ``state.sessions`` is a bare ``MagicMock`` across much of the suite and
    returns a truthy child for ANY unstubbed accessor, so a truthiness check here
    would report every mirror muted and silence cross-surface delivery for the
    whole test suite — the exact trap the Slack gate documents.
    """

    def test_a_mock_sessions_object_does_not_read_as_muted(self):
        from kiro_crew.dashboard.chat_utils import mirror_is_paused

        assert mirror_is_paused(SimpleNamespace(sessions=MagicMock()), KEY) is False

    def test_a_real_true_reads_as_muted(self):
        from kiro_crew.dashboard.chat_utils import mirror_is_paused

        sessions = MagicMock()
        sessions.is_mirror_paused = MagicMock(return_value=True)
        assert mirror_is_paused(SimpleNamespace(sessions=sessions), KEY) is True

    def test_a_raising_lookup_fails_open(self):
        """Failing open leaves a muted channel noisy; failing closed would make a
        live one silently dead."""
        from kiro_crew.dashboard.chat_utils import mirror_is_paused

        sessions = MagicMock()
        sessions.is_mirror_paused = MagicMock(side_effect=RuntimeError("map gone"))
        assert mirror_is_paused(SimpleNamespace(sessions=sessions), KEY) is False

    def test_no_sessions_at_all_reads_as_not_muted(self):
        from kiro_crew.dashboard.chat_utils import mirror_is_paused

        assert mirror_is_paused(SimpleNamespace(), KEY) is False


class TestEgressIsMutedButRoutingIsNot:
    """The two turn-mirroring sends stop; nothing else is touched.

    Scope is narrower than Slack's on purpose: no cron result, sub-agent
    completion, requested file or auto-nudge tick reads the mirror link at all —
    those address a channel explicitly — so unlike the Slack gate this one cannot
    reroute a delivery to the owner's DM or destroy a monitor loop.
    """

    @staticmethod
    def _linked_state(tmp_path, *, muted: bool):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        transport = SimpleNamespace(
            channel_type="discord",
            capabilities=SimpleNamespace(supports_proactive_send=True, max_message_chars=4096),
            send_message=AsyncMock(return_value="mid-1"),
        )
        state.register_channel_transport(transport)
        # Both accessors, because outbound delivery reads the PLURAL one now: a
        # session can hold several bindings and each is resolved on its own.
        state.sessions.get_mirror_links = MagicMock(return_value=[LINK])
        state.sessions.get_mirror_link = MagicMock(return_value=LINK)
        state.sessions.is_mirror_paused = MagicMock(return_value=muted)
        return state, transport

    @pytest.mark.asyncio
    async def test_a_muted_channel_receives_no_assistant_reply(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _deliver_cross_surface_reply

        state, transport = self._linked_state(tmp_path, muted=True)
        await _deliver_cross_surface_reply(state, KEY, "the answer")
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_muted_channel_receives_no_user_echo(self, tmp_path):
        from kiro_crew.dashboard.chat_runner import _deliver_cross_surface_user_message

        state, transport = self._linked_state(tmp_path, muted=True)
        await _deliver_cross_surface_user_message(state, KEY, "my question")
        transport.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_live_channel_still_receives_both(self, tmp_path):
        """Non-vacuity: the same fixtures deliver when the link is not muted."""
        from kiro_crew.dashboard.chat_runner import (
            _deliver_cross_surface_reply,
            _deliver_cross_surface_user_message,
        )

        state, transport = self._linked_state(tmp_path, muted=False)
        await _deliver_cross_surface_reply(state, KEY, "the answer")
        await _deliver_cross_surface_user_message(state, KEY, "my question")
        assert transport.send_message.await_count == 2


class TestAReplyLiftsTheMute:
    """A reply in a muted conversation resumes it, exactly as a Slack reply does.

    Disconnect retains the binding and its inbound marker, so the reply still
    resolves to this session. Without lifting the mute the message arrived and the
    answer was swallowed by the outbound gate: the conversation looked dead while
    silently consuming input, and Slack behaved the opposite way for the same user
    action.
    """

    CHANNEL = "1122334455"
    KEY = "dashboard:chat-7"

    def _resume(self, session_map):
        from kiro_crew.discord.session_resume import DiscordSessionResume

        resume = DiscordSessionResume.__new__(DiscordSessionResume)
        resume.sessions = session_map
        return resume

    @pytest.mark.asyncio
    async def test_a_reply_to_a_muted_binding_lifts_the_mute(self, session_map):
        link = ChannelLink(channel_type="discord", channel_id=self.CHANNEL)
        session_map.set_mirror_link(self.KEY, link, accepts_inbound=True)
        session_map.set_mirror_paused(self.KEY, True, "discord")
        assert session_map.is_mirror_paused(self.KEY, "discord") is True

        lifted = await self._resume(session_map).resume_if_muted(self.CHANNEL, self.KEY)

        assert lifted is True
        assert session_map.is_mirror_paused(self.KEY, "discord") is False

    @pytest.mark.asyncio
    async def test_the_binding_itself_is_untouched(self, session_map):
        """Only the mute lifts — the binding was never gone, so nothing re-binds."""
        link = ChannelLink(channel_type="discord", channel_id=self.CHANNEL)
        session_map.set_mirror_link(self.KEY, link, accepts_inbound=True)
        session_map.set_mirror_paused(self.KEY, True, "discord")

        await self._resume(session_map).resume_if_muted(self.CHANNEL, self.KEY)

        assert session_map.get_mirror_links(self.KEY) == [link]
        assert session_map.mirror_accepts_inbound(self.KEY, "discord") is True

    @pytest.mark.asyncio
    async def test_a_reply_to_a_LIVE_binding_writes_nothing(self, session_map):
        """Guarded on the read, so `_save` runs only on the mute→live transition."""
        link = ChannelLink(channel_type="discord", channel_id=self.CHANNEL)
        session_map.set_mirror_link(self.KEY, link, accepts_inbound=True)

        lifted = await self._resume(session_map).resume_if_muted(self.CHANNEL, self.KEY)

        assert lifted is False

    @pytest.mark.asyncio
    async def test_a_stubbed_session_manager_does_not_fake_a_resume(self):
        """`is True`, not truthiness: a MagicMock is truthy and would "resume"."""
        stub = MagicMock()
        lifted = await self._resume(stub).resume_if_muted(self.CHANNEL, self.KEY)

        assert lifted is False
        stub.set_mirror_paused.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_persisting_write_is_offloaded_from_the_event_loop(self, session_map):
        """`set_mirror_paused` calls `_save`, which serialises the whole session map.

        On the inbound path that runs for EVERY reply, so a synchronous write would
        stall the gateway's loop on slow storage.
        """
        link = ChannelLink(channel_type="discord", channel_id=self.CHANNEL)
        session_map.set_mirror_link(self.KEY, link, accepts_inbound=True)
        session_map.set_mirror_paused(self.KEY, True, "discord")

        with patch(
            "kiro_crew.discord.session_resume.asyncio.to_thread",
            new_callable=AsyncMock,
        ) as offload:
            await self._resume(session_map).resume_if_muted(self.CHANNEL, self.KEY)

        offload.assert_awaited_once()
        assert offload.await_args[0][0] == session_map.set_mirror_paused


class TestASlackRowAlwaysAccompaniesSlackLinked:
    """The wire must never report `slack_linked` without a Slack row.

    The row is what carries Slack's `paused`. When the Slack append hung off an
    if/elif chain, any non-Slack binding won it and the Slack row vanished while
    `slack_linked=True` was still returned — so the frontend synthesized a
    replacement with no `paused`, and a MUTED thread rendered as connected.
    """

    CHANNEL = "C_TEAM"
    THREAD = "1700000000.1"

    def _links(self, tmp_path, *, muted: bool, also_discord: bool):
        from kiro_crew.dashboard.state import DashboardState

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            sessions = SessionMap()
        sessions.set_slack_link(KEY, self.THREAD, self.CHANNEL)
        if muted:
            sessions.set_slack_paused(KEY, True)
        if also_discord:
            sessions.set_mirror_link(KEY, LINK, accepts_inbound=True)

        class _Slot:
            key = KEY
            linked_session_key = KEY
            _slack_thread_ts = self.THREAD
            _slack_channel = self.CHANNEL

        state = DashboardState.__new__(DashboardState)
        state.sessions = sessions
        # `_channel_link_is_live` consults the transport registry; an empty one is
        # the honest "no transport registered" answer for a unit test.
        state.channel_transports = {}
        with patch(
            "kiro_crew.dashboard.chat_utils.effective_session_key", return_value=KEY
        ):
            links, slack_linked, _channel, _ts = state._slot_links(_Slot())
        return links, slack_linked

    def test_the_slack_row_survives_a_sibling_channel_binding(self, tmp_path):
        links, slack_linked = self._links(tmp_path, muted=False, also_discord=True)

        assert slack_linked is True
        slack_rows = [x for x in links if x["channel"] == "slack"]
        assert len(slack_rows) == 1, f"expected exactly one Slack row, got {links}"
        assert {x["channel"] for x in links} == {"slack", "discord"}

    def test_a_muted_slack_thread_reports_paused_even_beside_a_sibling(self, tmp_path):
        """The actual user-visible defect: the row read "connected" while muted."""
        links, _ = self._links(tmp_path, muted=True, also_discord=True)

        slack_row = next(x for x in links if x["channel"] == "slack")
        assert slack_row["paused"] is True

    def test_the_sibling_is_not_marked_paused_by_slacks_mute(self, tmp_path):
        links, _ = self._links(tmp_path, muted=True, also_discord=True)

        discord_row = next(x for x in links if x["channel"] == "discord")
        assert discord_row["paused"] is False

    def test_slack_alone_still_emits_its_row(self, tmp_path):
        """Non-vacuity: the single-binding case that already worked still works."""
        links, slack_linked = self._links(tmp_path, muted=False, also_discord=False)

        assert slack_linked is True
        assert [x["channel"] for x in links] == ["slack"]


class TestGovernanceResolutionRunsOffTheEventLoop:
    """The send ladder reads profile files from disk, once PER BINDING per turn.

    `_resolve_channel_target` walks governance profiles (`iterdir`/`stat`), so doing
    it inline on the turn path multiplies a slow-disk stall by the number of bound
    channels and stalls every other gateway task plus the watchdog. The singular
    resolver already gets this treatment at its `chat_mirror` call site; the plural
    one is what turn mirroring uses.
    """

    @staticmethod
    def _linked_state(tmp_path):
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        transport = SimpleNamespace(
            channel_type="discord",
            capabilities=SimpleNamespace(supports_proactive_send=True, max_message_chars=4096),
            send_message=AsyncMock(return_value="mid-1"),
        )
        state.register_channel_transport(transport)
        # `_make_state` hands back a MagicMock SessionManager, and iterating a
        # MagicMock yields NOTHING — so the binding list has to be given
        # explicitly or the resolver loop never runs and this test passes
        # vacuously.
        state.sessions.get_mirror_links = MagicMock(return_value=[LINK])
        # Same reason for the SINGULAR accessor: the per-send still-bound check reads
        # it, and a bare MagicMock's return value never equals LINK, which reads as
        # an unlink mid-delivery and stops the resolver before it runs.
        state.sessions.get_mirror_link = MagicMock(return_value=LINK)
        return state, transport

    @pytest.mark.asyncio
    async def test_each_binding_is_resolved_via_to_thread(self, tmp_path):
        from kiro_crew.dashboard import chat_runner

        state, _transport = self._linked_state(tmp_path)

        async def _passthrough(fn, *args, **kwargs):
            return fn(*args, **kwargs)

        with patch(
            "kiro_crew.dashboard.chat_runner.asyncio.to_thread",
            side_effect=_passthrough,
        ) as offload:
            await chat_runner._resolve_mirror_targets(state, KEY)

        assert offload.await_count == 1, (
            "each binding's governance resolution must be offloaded from the loop"
        )
        assert offload.await_args[0][0] is chat_runner._resolve_channel_target
