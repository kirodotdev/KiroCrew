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

# Both legs gate on the SHIPPED capability objects, so the refusal cases below
# read them instead of a matching fake. Same roster the mirror-link suite uses.
from test_chat_mirror import (
    NON_PROACTIVE_CHANNELS,
    _channels_declaring_proactive,
    _real_caps_transport,
)

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

#: A channel whose REAL capabilities permit a proactive send, so both legs must
#: DELIVER to it. Named rather than derived: if Weixin's declaration is ever
#: locked down, this fails loudly here instead of quietly moving suites.
PROACTIVE_CHANNEL = "weixin"


def _fake_transport(channel_type: str = "telegram", proactive: bool = True, may_send: bool = True):
    return SimpleNamespace(
        channel_type=channel_type,
        capabilities=SimpleNamespace(
            supports_proactive_send=proactive,
            max_message_chars=4096,
            # 0 = not byte-capped, so chunk_for_transport takes the character
            # path these tests are about. Webex is the byte-capped one.
            max_message_bytes=0,
        ),
        send_message=AsyncMock(return_value="mid-1"),
        # Part of the MessagingTransport contract the send ladder consults: a
        # proactive send re-checks that the link's recipient is still on the
        # roster. Permissive here so these tests keep exercising delivery;
        # test_channel_transport_outbound_authz owns the refusal path.
        may_send_to=lambda conversation_id, thread_id=None, principal="": may_send,
    )


def test_the_pinned_proactive_channel_still_declares_proactive_send() -> None:
    """Guards the two delivery assertions below from becoming vacuous.

    They prove the legs deliver where the capability allows it. Pointing them at
    a channel that had since been locked would turn both into tests of the skip
    path wearing a delivery test's name.
    """
    assert PROACTIVE_CHANNEL in _channels_declaring_proactive(True)
    assert PROACTIVE_CHANNEL not in NON_PROACTIVE_CHANNELS


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
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )

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
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )

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
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )

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
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        await _deliver_cross_surface_reply(state, "dashboard:chat-1", "hi there")
        tp.send_message.assert_awaited_once_with("123", "hi there", thread_id=None)

    @pytest.mark.asyncio
    async def test_passes_thread_id(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="C", thread_id="T")
        )
        await _deliver_cross_surface_reply(state, "k", "x")
        tp.send_message.assert_awaited_once_with("C", "x", thread_id="T")

    @pytest.mark.asyncio
    async def test_skips_slack(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("slack")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("slack", channel_id="C1", thread_id="ts")
        )
        await _deliver_cross_surface_reply(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_link(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        await _deliver_cross_surface_reply(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_skips_when_transport_unregistered(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_reply(state, "k", "hi")  # telegram not registered

    @pytest.mark.asyncio
    async def test_skips_when_not_proactive(self, tmp_path):
        """The leg must honour a False ``supports_proactive_send``.

        SYNTHETIC capability, so this branch is covered whatever
        ``NON_PROACTIVE_CHANNELS`` holds this week: it has been empty, and a suite
        parametrized over an empty set drives NOTHING while still reporting green.
        Real-capability coverage is the accepting counterpart below; this keeps the
        refusing branch alive.
        """
        channel = "synthetic"
        state = _make_state(tmp_path)
        tp = _fake_transport(channel, proactive=False)
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink(channel, channel_id="u1")
        )
        await _deliver_cross_surface_reply(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delivers_when_real_capabilities_allow_it(self, tmp_path):
        """The positive counterpart, on real capabilities rather than a fake."""
        state = _make_state(tmp_path)
        tp = _real_caps_transport(PROACTIVE_CHANNEL)
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink(PROACTIVE_CHANNEL, channel_id="u1")
        )
        await _deliver_cross_surface_reply(state, "k", "hi")
        tp.send_message.assert_awaited_once_with("u1", "hi", thread_id=None)

    @pytest.mark.asyncio
    async def test_skips_empty_text(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_reply(state, "k", "")
        tp.send_message.assert_not_awaited()
        state.sessions.get_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_applies_redaction_pipeline(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        raw = "see https://evil.example/exfil?q=1 and AKIAIOSFODNN7EXAMPLE"
        expected = redact_credentials(redact_exfiltration_urls(raw)[0])[0]
        await _deliver_cross_surface_reply(state, "k", raw)
        assert tp.send_message.await_args.args[1] == expected

    @pytest.mark.asyncio
    async def test_a_markdown_split_credential_is_redacted_in_display_form(self, tmp_path):
        """This leg passes no RENDERER, and a renderer is where a turn gets that floor.

        ``AKIA**IOSF**ODNN7EXAMPLE`` survives a byte-for-byte scan and the client
        reassembles it whole once it renders the emphasis away, so the mirror has to
        scan the DISPLAYED form — the same argument, and the same context-aware
        redactor, as the Slack chokepoint already applies.
        """
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )

        await _deliver_cross_surface_reply(state, "k", "token AKIA**IOSF**ODNN7EXAMPLE ok")

        sent = tp.send_message.await_args.args[1]
        assert "AKIAIOSFODNN7EXAMPLE" not in sent.replace("*", "")

    @pytest.mark.asyncio
    async def test_send_failure_is_swallowed(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_reply(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_a_byte_capped_channel_chunks_on_BYTES(self, tmp_path):
        """The mirror leg measures in the transport's own unit.

        A character count cannot express a byte cap without being wrong in one
        direction: the only safe char value is the byte budget over four, which
        cut an ASCII reply into quarters, while the true char cap would let a CJK
        reply exceed the byte limit and be truncated on send.
        """
        state = _make_state(tmp_path)
        tp = _fake_transport("webex")
        # What Webex declares: a byte cap plus the 4x-pessimistic char floor.
        tp.capabilities.max_message_bytes = 400
        tp.capabilities.max_message_chars = 100
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("webex", channel_id="ROOM")
        )

        # 350 ASCII chars is 350 bytes: under the byte cap, so ONE send — where
        # the char floor alone would have made it four.
        await _deliver_cross_surface_reply(state, "k", "x" * 350)
        assert tp.send_message.await_count == 1

        # The same length in 3-byte characters is 1050 bytes, so it must split.
        tp.send_message.reset_mock()
        await _deliver_cross_surface_reply(state, "k", "界" * 350)
        assert tp.send_message.await_count > 1
        for call in tp.send_message.await_args_list:
            assert len(call.args[1].encode("utf-8")) <= 400

    @pytest.mark.asyncio
    async def test_long_reply_is_chunked(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.capabilities.max_message_chars = 100
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
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
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123", thread_id=None)
        )
        await _deliver_cross_surface_user_message(state, "k", "hello there")
        tp.send_message.assert_awaited_once_with("123", "💬 hello there", thread_id=None)

    @pytest.mark.asyncio
    async def test_passes_thread_id(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="C", thread_id="T")
        )
        await _deliver_cross_surface_user_message(state, "k", "x")
        tp.send_message.assert_awaited_once_with("C", "💬 x", thread_id="T")

    @pytest.mark.asyncio
    async def test_skips_slack(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("slack")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("slack", channel_id="C1", thread_id="ts")
        )
        await _deliver_cross_surface_user_message(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_link(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(return_value=None)
        await _deliver_cross_surface_user_message(state, "k", "hi")  # must not raise

    @pytest.mark.asyncio
    async def test_skips_when_transport_unregistered(self, tmp_path):
        state = _make_state(tmp_path)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_user_message(state, "k", "hi")

    @pytest.mark.asyncio
    async def test_skips_when_not_proactive(self, tmp_path):
        """Synthetic capability, for the same reason as the reply leg's twin."""
        channel = "synthetic"
        state = _make_state(tmp_path)
        tp = _fake_transport(channel, proactive=False)
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink(channel, channel_id="u1")
        )
        await _deliver_cross_surface_user_message(state, "k", "hi")
        tp.send_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_delivers_when_real_capabilities_allow_it(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _real_caps_transport(PROACTIVE_CHANNEL)
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink(PROACTIVE_CHANNEL, channel_id="u1")
        )
        await _deliver_cross_surface_user_message(state, "k", "hi")
        tp.send_message.assert_awaited_once_with("u1", "💬 hi", thread_id=None)

    @pytest.mark.asyncio
    async def test_skips_empty_message(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_user_message(state, "k", "")
        tp.send_message.assert_not_awaited()
        state.sessions.get_mirror_link.assert_not_called()

    @pytest.mark.asyncio
    async def test_truncates_and_redacts(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        raw = "tok AKIAIOSFODNN7EXAMPLE " + "x" * 800
        await _deliver_cross_surface_user_message(state, "k", raw)
        sent = tp.send_message.await_args.args[1]
        # _prepare_mirror_msg redacts over the FULL text (redact_via_context)
        # THEN truncates to 500 — the same composition as
        # security.redact_and_truncate, applied through the egress shim.
        # Truncating first can cut a credential at the 500-char boundary into
        # fragments no redaction regex matches, so the raw remainder would leak
        # into the mirrored echo (the slice-before-redact class).
        assert sent == "💬 " + redact_via_context(raw)[:500]

    @pytest.mark.asyncio
    async def test_send_failure_is_swallowed(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.send_message = AsyncMock(side_effect=RuntimeError("boom"))
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )
        await _deliver_cross_surface_user_message(state, "k", "hi")  # must not raise


# A reply whose code block spans the transport's message cap: the shape of a cron
# log or diff dump, and the one a fixed-width slice mangles. Lines inside the
# block carry prose markup a channel's dialect converter WOULD rewrite if the part
# they land in has lost the opener.
_FENCED_REPLY = (
    "Here is the diff:\n\n"
    "```diff\n"
    "**never bold** inside a code block\n"
    "# never a heading inside a code block\n"
    "- never a bullet inside a code block\n"
    "~~never struck~~ inside a code block\n"
    "```\n"
)
_CODE_MARKERS = (
    "**never bold** inside a code block",
    "# never a heading inside a code block",
    "- never a bullet inside a code block",
    "~~never struck~~ inside a code block",
)


def _lines_the_channel_would_read_as_prose(part: str) -> list[str]:
    """Lines of *part* that fall OUTSIDE a fence, as a dialect converter sees them."""
    from kiro_crew.messaging.split import FENCE_OUTSIDE, iter_fence_lines

    return [line for line, role in iter_fence_lines(part) if role == FENCE_OUTSIDE]


class TestALongFencedReplyStaysCode:
    """The mirror pre-splits, so the split it does IS the one the channel gets.

    Each part arrives already under the cap, so a channel's own fence-safe
    splitter is a no-op on it: whatever cut this leg makes is final. A fixed-width
    slice through the block leaves part two with no opener, every line in it takes
    the prose branch of the channel's dialect converter, and the markup INSIDE the
    code gets rewritten.
    """

    @pytest.mark.asyncio
    async def test_no_code_line_escapes_its_fence(self, tmp_path):
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.capabilities.max_message_chars = 90
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )

        await _deliver_cross_surface_reply(state, "k", _FENCED_REPLY)

        parts = [c.args[1] for c in tp.send_message.await_args_list]
        assert len(parts) > 1, "precondition: the reply must actually have been split"
        for part in parts:
            prose = _lines_the_channel_would_read_as_prose(part)
            for marker in _CODE_MARKERS:
                assert marker not in prose, (
                    f"a code line landed outside its fence in this part, so the "
                    f"channel will rewrite the markup inside it:\n{part}"
                )

    @pytest.mark.asyncio
    async def test_every_source_line_still_arrives(self, tmp_path):
        """Fence scaffolding is added, never substituted for content."""
        state = _make_state(tmp_path)
        tp = _fake_transport("telegram")
        tp.capabilities.max_message_chars = 90
        state.register_channel_transport(tp)
        state.sessions.get_mirror_link = MagicMock(
            return_value=ChannelLink("telegram", channel_id="123")
        )

        await _deliver_cross_surface_reply(state, "k", _FENCED_REPLY)

        delivered = "\n".join(c.args[1] for c in tp.send_message.await_args_list)
        for marker in _CODE_MARKERS:
            assert marker in delivered
