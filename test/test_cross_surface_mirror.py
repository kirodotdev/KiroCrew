"""Tests for C2: channel-neutral cross-surface reply delivery (dashboard->channel).

Covers the DashboardState transport-registry seam and
``_deliver_cross_surface_reply``: it pushes a completed dashboard reply to a
linked non-Slack proactive channel via ``Transport.send_message``,
capability-gated, and is a silent no-op for Slack (its own streaming mirror),
WeCom (no proactive send), unregistered transports, unlinked sessions, and
empty replies.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_runner import (
    _deliver_cross_surface_reply,
    _deliver_cross_surface_user_message,
)
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.platform import redact_via_context
from kiro_crew.security import (
    redact_credentials,
    redact_exfiltration_urls,
)


def _fake_transport(channel_type: str = "telegram", proactive: bool = True):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=SimpleNamespace(supports_proactive_send=proactive, max_message_chars=4096),
        send_message=AsyncMock(return_value="mid-1"),
    )


def _bind(state, *links):
    """Stub BOTH mirror accessors so the double matches the real interface.

    Outbound delivery reads ``get_mirror_links`` (a session can hold several
    bindings); callers that know they mean one still read ``get_mirror_link``,
    which returns None rather than an arbitrary sibling when several exist.

    ``get_mirror_link`` HONOURS its ``channel_type``, because the real one does and
    the per-send still-bound check calls it that way: named, it answers for that
    channel; unnamed, it refuses to guess between siblings. A double that ignored
    the argument returned None for every multi-binding session, which reads to the
    caller as "this link was unlinked mid-delivery" and silently stopped delivery.
    """
    state.sessions.get_mirror_links = MagicMock(return_value=list(links))

    def _one(_key, channel_type=""):
        if not channel_type:
            return links[0] if len(links) == 1 else None
        return next((link for link in links if link.channel_type == channel_type), None)

    state.sessions.get_mirror_link = MagicMock(side_effect=_one)


class TestSeveralChannelsAtOnce:
    """A session can mirror to several channels, and each stands on its own.

    Three independent properties, all of which a single-target implementation
    would have silently broken: delivery fans out, one channel's failure does not
    cost the others their message, and a per-binding mute silences only its own.
    """

    @staticmethod
    def _two(tmp_path, *, discord_paused=False, telegram_paused=False):
        state = _make_state(tmp_path)
        discord = _fake_transport("discord")
        telegram = _fake_transport("telegram")
        state.register_channel_transport(discord)
        state.register_channel_transport(telegram)
        links = [
            ChannelLink("discord", channel_id="D1"),
            ChannelLink("telegram", channel_id="T1"),
        ]
        state.sessions.get_mirror_links = MagicMock(return_value=links)
        state.sessions.is_mirror_paused = MagicMock(
            side_effect=lambda _key, channel_type="": (
                discord_paused if channel_type == "discord" else telegram_paused
            )
        )
        # The per-send still-bound check reads the SINGULAR accessor with a
        # channel_type; left as a bare MagicMock its return value never equals the
        # link, which reads as an unlink mid-delivery and stops everything.
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda _key, channel_type="": next(
                (link for link in links if link.channel_type == channel_type), None
            )
        )
        return state, discord, telegram

    @pytest.mark.asyncio
    async def test_the_reply_reaches_every_connected_channel(self, tmp_path):
        state, discord, telegram = self._two(tmp_path)
        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "the answer")
        discord.send_message.assert_awaited_once_with("D1", "the answer", thread_id=None)
        telegram.send_message.assert_awaited_once_with("T1", "the answer", thread_id=None)

    @pytest.mark.asyncio
    async def test_the_user_echo_reaches_every_connected_channel(self, tmp_path):
        state, discord, telegram = self._two(tmp_path)
        await _deliver_cross_surface_user_message(state, "dashboard:chat-1", "my question")
        assert discord.send_message.await_count == 1
        assert telegram.send_message.await_count == 1

    @pytest.mark.asyncio
    async def test_muting_one_channel_leaves_the_other_delivering(self, tmp_path):
        state, discord, telegram = self._two(tmp_path, discord_paused=True)
        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "the answer")
        discord.send_message.assert_not_awaited()
        telegram.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_channel_failing_does_not_cost_the_other_its_message(self, tmp_path):
        state, discord, telegram = self._two(tmp_path)
        discord.send_message = AsyncMock(side_effect=RuntimeError("discord down"))
        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "the answer")
        telegram.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_each_channel_is_split_at_its_own_length_limit(self, tmp_path):
        """One shared split would cut every channel at the strictest limit."""
        state = _make_state(tmp_path)
        roomy = _fake_transport("discord")
        roomy.capabilities.max_message_chars = 4000
        tight = _fake_transport("telegram")
        tight.capabilities.max_message_chars = 100
        state.register_channel_transport(roomy)
        state.register_channel_transport(tight)
        links = [
            ChannelLink("discord", channel_id="D1"),
            ChannelLink("telegram", channel_id="T1"),
        ]
        state.sessions.get_mirror_links = MagicMock(return_value=links)
        # Channel-aware, because the per-send still-bound check reads it: a bare
        # MagicMock never equals the link and would stop delivery before the split.
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda _key, channel_type="": next(
                (link for link in links if link.channel_type == channel_type), None
            )
        )
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 350)

        assert roomy.send_message.await_count == 1
        assert tight.send_message.await_count > 1


class TestGovernanceDegradationFailsClosed:
    """A degraded governance evaluation must DENY the mirror egress, not permit it.

    ``governance_permits`` catches its own internal errors and, by default,
    returns a permissive "no opinion" Decision — its own docstring notes that a
    caller wrapping it in ``except`` can never observe the failure, so the DENY
    has to be produced at the call site via ``fail_closed=True``. Without that,
    a governance outage silently becomes permission to send to an external
    channel. These tests pin both halves of the gate.
    """

    @pytest.mark.asyncio
    async def test_degraded_evaluation_blocks_delivery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.resolve_active_scope",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("profile store down")),
        )
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123", thread_id=None))

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "hi there")

        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_decision_without_permitted_attr_blocks_delivery(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.platform.governance_profiles.governance_permits",
            lambda *a, **k: SimpleNamespace(),
        )
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123", thread_id=None))

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "hi there")

        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_composition_error_propagates(self, tmp_path, monkeypatch):
        """A broken governance ceiling must NOT read as an ordinary skip.

        ``governance_permits`` deliberately re-raises PlatformCompositionError
        instead of degrading, so the resolver's generic fail-closed handler must
        let it through rather than swallowing it into a silent no-mirror.
        """
        from kiro_crew.platform.context import PlatformCompositionError

        def _boom(*a, **k):
            raise PlatformCompositionError("ceiling weakened")

        monkeypatch.setattr("kiro_crew.platform.governance_profiles.governance_permits", _boom)
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123", thread_id=None))

        with pytest.raises(PlatformCompositionError):
            await _deliver_cross_surface_reply(state, "dashboard:chat-1", "hi there")

        tp.send_message.assert_not_awaited()


class TestRegistrySeam:
    def test_register_and_get(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        assert state.get_channel_transport("telegram") is tp

    def test_register_attaches_dashboard_state_to_the_dispatcher(self, tmp_path):
        state = _make_state(tmp_path)
        dispatcher = SimpleNamespace()
        tp = _fake_transport("telegram")
        tp.dispatcher = dispatcher

        state.register_channel_transport(tp)

        assert dispatcher.dashboard_state is state

    def test_get_missing_returns_none(self, tmp_path):
        state = _make_state(tmp_path)
        assert state.get_channel_transport("telegram") is None

    def test_register_ignores_blank_channel_type(self, tmp_path):
        state = _make_state(tmp_path)
        state.register_channel_transport(SimpleNamespace(channel_type=""))
        assert state.channel_transports == {}


class TestDeliverCrossSurfaceReply:
    @pytest.mark.asyncio
    async def test_delivers_to_telegram(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123", thread_id=None))
        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "hi there")
        tp.send_message.assert_awaited_once_with("123", "hi there", thread_id=None)

    @pytest.mark.asyncio
    async def test_passes_thread_id(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="C", thread_id="T"))
        await _deliver_cross_surface_reply(state, "k", "x")
        tp.send_message.assert_awaited_once_with("C", "x", thread_id="T")

    @pytest.mark.asyncio
    async def test_skips_slack(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("slack")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("slack", channel_id="C1", thread_id="ts"))
        await _deliver_cross_surface_reply(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_link(self, tmp_path):
        state = _make_state(tmp_path)
        _bind(state)
        await _deliver_cross_surface_reply(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_skips_when_transport_unregistered(self, tmp_path):
        state = _make_state(tmp_path)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_reply(state, "k", "hi")  # telegram not registered

    @pytest.mark.asyncio
    async def test_skips_when_not_proactive(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("wecom", proactive=False)
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("wecom", channel_id="u1"))
        await _deliver_cross_surface_reply(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_empty_text(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_reply(state, "k", "")
        tp.send_message.assert_not_awaited()
        state.sessions.get_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_applies_redaction_pipeline(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        raw = "see https://evil.example/exfil?q=1 and AKIAIOSFODNN7EXAMPLE"
        expected = redact_credentials(redact_exfiltration_urls(raw)[0])[0]
        await _deliver_cross_surface_reply(state, "k", raw)
        assert tp.send_message.await_args.args[1] == expected

    @pytest.mark.asyncio
    async def test_send_failure_is_swallowed(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_reply(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_long_reply_is_chunked(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.capabilities.max_message_chars = 100
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        long_text = "x" * 250
        await _deliver_cross_surface_reply(state, "k", long_text)
        # 250 chars / 100 per chunk = 3 sends; content preserved end-to-end and
        # each part stays within the channel's max_message_chars.
        assert tp.send_message.await_count == 3
        sent = "".join(c.args[1] for c in tp.send_message.await_args_list)
        assert sent == long_text
        for c in tp.send_message.await_args_list:
            assert len(c.args[1]) <= 100


class TestDeliverCrossSurfaceUserMessage:
    @pytest.mark.asyncio
    async def test_delivers_with_prefix(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123", thread_id=None))
        await _deliver_cross_surface_user_message(state, "k", "hello there")
        tp.send_message.assert_awaited_once_with("123", "💬 hello there", thread_id=None)

    @pytest.mark.asyncio
    async def test_passes_thread_id(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="C", thread_id="T"))
        await _deliver_cross_surface_user_message(state, "k", "x")
        tp.send_message.assert_awaited_once_with("C", "💬 x", thread_id="T")

    @pytest.mark.asyncio
    async def test_skips_slack(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("slack")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("slack", channel_id="C1", thread_id="ts"))
        await _deliver_cross_surface_user_message(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_link(self, tmp_path):
        state = _make_state(tmp_path)
        _bind(state)
        await _deliver_cross_surface_user_message(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_skips_when_transport_unregistered(self, tmp_path):
        state = _make_state(tmp_path)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_user_message(state, "k", "hi")

    @pytest.mark.asyncio
    async def test_skips_when_not_proactive(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("wecom", proactive=False)
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("wecom", channel_id="u1"))
        await _deliver_cross_surface_user_message(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_empty_message(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_user_message(state, "k", "")
        tp.send_message.assert_not_awaited()
        state.sessions.get_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_truncates_and_redacts(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        raw = "tok AKIAIOSFODNN7EXAMPLE " + "x" * 800
        await _deliver_cross_surface_user_message(state, "k", raw)
        sent = tp.send_message.await_args.args[1]
        # _prepare_mirror_msg truncates to 500 THEN redacts (redact_via_context),
        # matching the Slack echo. Distinct from security.redact_and_truncate,
        # which redacts-then-truncates (security-review e27617c6) — the mirror echo keeps
        # the truncate-first order so the 500-char budget is measured pre-redaction.
        assert sent == "💬 " + redact_via_context(raw[:500])

    @pytest.mark.asyncio
    async def test_send_failure_is_swallowed(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(tp)
        _bind(state, ChannelLink("telegram", channel_id="123"))
        await _deliver_cross_surface_user_message(state, "k", "hi")  # must not raise


class TestGovernanceIsRecheckedBetweenChunks:
    """A multipart reply is a sequence of egress actions, not one.

    The decision that authorized part 1 does not authorize part 7: the transport
    takes real time per send, and a disconnect or a narrowed policy arriving in
    between has to stop the rest. Before this, the target was resolved once at the
    top of the turn and every chunk rode that one answer.
    """

    @staticmethod
    def _long_reply(tmp_path, *, mute_after_sends: int):
        """A reply long enough to need several chunks, muted after N have SENT.

        Keyed to sends rather than to how many times the gate asks: it asks the mute
        on both sides of its governance await, so a question count would encode the
        gate's internals instead of the scenario.
        """
        state = _make_state(tmp_path)
        transport = _fake_transport("telegram")
        transport.capabilities.max_message_chars = 10
        state.register_channel_transport(transport)
        _bind(state, ChannelLink("telegram", channel_id="T1"))
        sent = {"n": 0}
        real_send = transport.send_message

        async def _count(*args, **kwargs):
            sent["n"] += 1
            return await real_send(*args, **kwargs)

        transport.send_message = AsyncMock(side_effect=_count)

        def _paused(_state, _key, _channel_type=""):
            return sent["n"] >= mute_after_sends

        return state, transport, _paused

    @pytest.mark.asyncio
    async def test_a_disconnect_mid_reply_stops_the_remaining_chunks(
        self, tmp_path, monkeypatch
    ):
        state, transport, paused = self._long_reply(tmp_path, mute_after_sends=2)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.mirror_is_paused", paused
        )

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 45)

        # One question before chunk 1 (the resolver), one before chunk 2. The third
        # question answers "muted", so chunk 3 must never be sent.
        assert transport.send_message.await_count == 2, (
            "a disconnect between chunks did not stop the reply: "
            f"{transport.send_message.await_count} parts went out"
        )

    @pytest.mark.asyncio
    async def test_all_chunks_go_when_nothing_changes(self, tmp_path, monkeypatch):
        state, transport, _ = self._long_reply(tmp_path, mute_after_sends=99)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.mirror_is_paused",
            lambda *_a, **_k: False,
        )

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 45)

        assert transport.send_message.await_count == 5, (
            "the recheck dropped chunks it should have delivered: "
            f"{transport.send_message.await_count}/5"
        )


class TestADenialEndsTheReplyRatherThanSkippingAChunk:
    """A denied chunk must end the delivery, not be stepped over.

    `continue` instead of `break` looks equivalent while the denial persists — the
    remaining rechecks deny too, so the same number of parts go out and a test with
    a permanently-muted binding cannot tell the two apart. It stops being equivalent
    the moment the binding is permitted again mid-reply: stepping over resumes the
    stream, so the reader receives parts 1, 2, 4, 5 — a message with a hole in it,
    silently. Ending the delivery is the only outcome that keeps the transcript
    honest about what it dropped.
    """

    @pytest.mark.asyncio
    async def test_it_does_not_resume_after_a_transient_denial(
        self, tmp_path, monkeypatch
    ):
        state = _make_state(tmp_path)
        transport = _fake_transport("telegram")
        transport.capabilities.max_message_chars = 10
        state.register_channel_transport(transport)
        _bind(state, ChannelLink("telegram", channel_id="T1"))

        # Muted only while exactly 2 parts have gone out, live before and after —
        # keyed to SENDS because the gate asks the mute twice per resolve.
        sent = {"n": 0}
        real_send = transport.send_message

        async def _count(*args, **kwargs):
            sent["n"] += 1
            return await real_send(*args, **kwargs)

        transport.send_message = AsyncMock(side_effect=_count)
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_runner.mirror_is_paused",
            lambda *_a, **_k: sent["n"] == 2,
        )

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 45)

        assert transport.send_message.await_count == 2, (
            "the reply resumed after a denial, so the channel received a message "
            f"with a hole in it: {transport.send_message.await_count} of 5 parts"
        )


class TestAnUnlinkMidReplyStopsTheRest:
    """A CLEARED binding is not a paused one, and the mute check cannot see it.

    An in-channel `!unlink` removes the binding outright. A session with no binding
    is not paused, so a recheck that only asks the mute reads a cleared link as live
    and keeps posting `link` — the address captured before the unlink — into a
    conversation this session no longer owns, or that another session has since
    claimed. So the binding must still BE this location.
    """

    @staticmethod
    def _linked(tmp_path, *, clear_after_sends: int):
        """Deliver a 5-chunk reply; the binding disappears after N parts have SENT.

        Keyed to sends, not to how many times the gate reads the binding: the gate
        asks on both sides of its governance await, so a read count would encode its
        internals rather than the scenario.
        """
        state = _make_state(tmp_path)
        transport = _fake_transport("telegram")
        transport.capabilities.max_message_chars = 10
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="T1")
        state.sessions.get_mirror_links = MagicMock(return_value=[link])
        sent = {"n": 0}

        async def _count(*_args, **_kwargs):
            sent["n"] += 1
            return "mid"

        transport.send_message = AsyncMock(side_effect=_count)
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda _k, channel_type="": (
                link if sent["n"] < clear_after_sends else None
            )
        )
        state.sessions.is_mirror_paused = MagicMock(return_value=False)
        return state, transport

    @pytest.mark.asyncio
    async def test_a_cleared_binding_stops_the_remaining_chunks(self, tmp_path):
        state, transport = self._linked(tmp_path, clear_after_sends=2)

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 45)

        assert transport.send_message.await_count == 2, (
            "an in-channel unlink mid-reply did not stop the delivery: "
            f"{transport.send_message.await_count} of 5 parts went out anyway"
        )

    @pytest.mark.asyncio
    async def test_a_rebind_elsewhere_also_stops_it(self, tmp_path):
        """`link` is the address captured BEFORE the rebind, so it is now wrong."""
        state = _make_state(tmp_path)
        transport = _fake_transport("telegram")
        transport.capabilities.max_message_chars = 10
        state.register_channel_transport(transport)
        here = ChannelLink("telegram", channel_id="T1")
        moved = ChannelLink("telegram", channel_id="T2")
        state.sessions.get_mirror_links = MagicMock(return_value=[here])
        sent = {"n": 0}

        async def _count(*_args, **_kwargs):
            sent["n"] += 1
            return "mid"

        transport.send_message = AsyncMock(side_effect=_count)
        # Keyed to sends, not reads: the gate asks twice per resolve.
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda _k, channel_type="": here if sent["n"] < 2 else moved
        )
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "x" * 45)

        sent_to = {call.args[0] for call in transport.send_message.await_args_list}
        assert sent_to == {"T1"}, (
            f"the reply followed a rebind it never re-chunked for: {sent_to}"
        )
        assert sent["n"] == 2, f"the rebind did not stop the delivery: {sent['n']}"


class TestADisconnectDuringTheGovernanceAwaitIsCaught:
    """The policy read is file I/O, so it is a window — not an instant.

    The gate asked "still bound, not muted" and then awaited `_resolve_channel_target`,
    which loads policy from disk. A disconnect landing inside that await was invisible:
    the authorization that came back was true when it was requested and stale by the
    time it was used, so the next chunk went to a detached conversation. The question is
    asked again after the await, which is the answer that actually decides the send.
    """

    @pytest.mark.asyncio
    async def test_a_disconnect_inside_the_await_stops_the_delivery(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import chat_runner

        state = _make_state(tmp_path)
        transport = _fake_transport("telegram")
        state.register_channel_transport(transport)
        link = ChannelLink("telegram", channel_id="T1")
        _bind(state, link)

        live = {"bound": True}
        state.sessions.get_mirror_link = MagicMock(
            side_effect=lambda _k, channel_type="": link if live["bound"] else None
        )
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        def _resolve_then_disconnect(_state, _key, _link):
            # The user disconnects while the policy load is in flight.
            live["bound"] = False
            return (link, transport)

        monkeypatch.setattr(
            chat_runner, "_resolve_channel_target", _resolve_then_disconnect
        )

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "the answer")

        transport.send_message.assert_not_awaited()


class TestTheFirstSendToEachBindingIsReAuthorized:
    """Targets are resolved for ALL bindings up front, then the loop sends.

    So binding #2's authorization is obtained before binding #1 has sent anything —
    and those sends take real time. The per-part recheck used to start at part 2,
    which left every binding's FIRST send riding a decision that could already be
    stale by the time it was used.
    """

    DISCORD = ChannelLink("discord", channel_id="D1")
    TELEGRAM = ChannelLink("telegram", channel_id="T1")

    def _two_with_a_slow_first(self, tmp_path):
        state = _make_state(tmp_path)
        discord = _fake_transport("discord")
        telegram = _fake_transport("telegram")
        state.register_channel_transport(discord)
        state.register_channel_transport(telegram)
        links = [self.DISCORD, self.TELEGRAM]
        state.sessions.get_mirror_links = MagicMock(return_value=links)
        live = {"telegram": True}

        def _one(_key, channel_type=""):
            if channel_type == "telegram" and not live["telegram"]:
                return None
            return next((x for x in links if x.channel_type == channel_type), None)

        state.sessions.get_mirror_link = MagicMock(side_effect=_one)
        state.sessions.is_mirror_paused = MagicMock(return_value=False)

        async def _slow_then_disconnect_telegram(*_args, **_kwargs):
            # Discord's send is slow; the user disconnects Telegram while it runs.
            live["telegram"] = False
            return "mid"

        discord.send_message = AsyncMock(side_effect=_slow_then_disconnect_telegram)
        return state, discord, telegram

    @pytest.mark.asyncio
    async def test_a_reply_skips_a_binding_disconnected_while_another_sent(
        self, tmp_path
    ):
        state, discord, telegram = self._two_with_a_slow_first(tmp_path)

        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "the answer")

        assert discord.send_message.await_count == 1, "discord should have delivered"
        telegram.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_user_echo_skips_it_too(self, tmp_path):
        """The echo had no per-send recheck at all — one send per binding, but the
        bindings are still a sequence."""
        state, discord, telegram = self._two_with_a_slow_first(tmp_path)

        await _deliver_cross_surface_user_message(state, "dashboard:chat-1", "hello")

        assert discord.send_message.await_count == 1, "discord should have delivered"
        telegram.send_message.assert_not_awaited()
