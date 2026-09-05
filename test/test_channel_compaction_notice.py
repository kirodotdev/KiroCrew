"""Auto-compaction notice on channel-originated sessions (Slack + Discord).

Before this, ``SessionManager``'s compact callback was dashboard-only: it
returned early for any key that did not start with ``dashboard:``, so a Slack
thread or Discord DM was compacted in silence. These tests pin the channel leg:

  * the notice text (copy, pct rounding, per-channel manual command);
  * Slack delivery into the session's persisted thread, gated on the
    ``channels`` governance scope;
  * non-Slack delivery through the governed cross-surface ladder, using the
    session's ``origin`` link as the target;
  * every no-target / no-transport / governance-denial / delivery-failure path
    degrading to a logged no-op — a compaction that SUCCEEDED must never
    surface as an error;
  * the origin-link store itself: in-memory on ``SessionManager`` (NOT
    persisted), evicted with the session, and bounded;
  * the wiring in ``DashboardState.wire_session_compact_callback``.

All against fakes — no real transport, session manager, or network.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew.dashboard.chat_compaction_notice import (
    deliver_channel_compaction_notice,
    notice_text,
)
from kiro_crew.messaging.link import ChannelLink


class _SlackClient:
    def __init__(self, fail: bool = False) -> None:
        self.posted: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def post_message(self, channel: str, text: str, thread_ts: str | None = None) -> str:
        if self.fail:
            raise RuntimeError("slack down")
        self.posted.append((channel, text, thread_ts))
        return "ts-posted"


class _Transport:
    def __init__(self, fail: bool = False, max_chars: int = 2000) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail = fail
        self.capabilities = SimpleNamespace(
            supports_proactive_send=True, max_message_chars=max_chars
        )

    def may_send_to(
        self, conversation_id: str, thread_id: str | None = None, *, principal: str = ""
    ) -> bool:
        """Part of the contract the send ladder consults before a proactive send.

        Permissive so these tests keep exercising the notice itself;
        ``test_channel_transport_outbound_authz`` owns the refusal path.
        """
        return True

    async def send_message(
        self, conversation_id: str, content: str, thread_id: str | None = None
    ) -> str:
        if self.fail:
            raise RuntimeError("discord down")
        self.sent.append((conversation_id, content, thread_id))
        return "mid-1"


class _Sessions:
    def __init__(
        self,
        *,
        slack_link: tuple[str | None, str | None] = (None, None),
        origin: ChannelLink | None = None,
        mirror: ChannelLink | None = None,
        raise_on_lookup: bool = False,
    ) -> None:
        self._slack_link = slack_link
        self._origin = origin
        self._mirror = mirror
        self._raise = raise_on_lookup

    def get_slack_link(self, key: str) -> tuple[str | None, str | None]:
        if self._raise:
            raise RuntimeError("map unreadable")
        return self._slack_link

    def get_origin_link(self, key: str) -> ChannelLink | None:
        if self._raise:
            raise RuntimeError("map unreadable")
        return self._origin

    def get_mirror_link(self, key: str) -> ChannelLink | None:
        if self._raise:
            raise RuntimeError("map unreadable")
        return self._mirror


def _state(
    *,
    slack_client: Any = None,
    sessions: Any = None,
    transport: Any = None,
    channel_type: str = "discord",
) -> Any:
    """A fake dashboard state serving *transport* for *channel_type*.

    The channel type is a parameter, not a hardcoded ``"discord"``: the notice path
    resolves the transport BY the link's channel type, so a helper that only ever
    answered for one channel could not express a second one — which is how the
    origin-vs-mirror asymmetry stayed untested.
    """
    return SimpleNamespace(
        slack_client=slack_client,
        sessions=sessions or _Sessions(),
        channel_transports={channel_type: transport} if transport is not None else {},
        get_channel_transport=lambda ct: (
            transport if transport is not None and ct == channel_type else None
        ),
    )


@contextmanager
def _permit_governance():
    """Permit BOTH governance gates on the notice path.

    There are two, resolved differently: this module's Slack gate holds
    ``vet_and_audit`` as a module-scope name, while ``chat_runner``'s transport
    ladder imports it inside the function (so it must be patched at its source).
    A test that patched only one would leave the other reading the host's real
    governance profile.
    """
    permit = SimpleNamespace(permitted=True)
    with (
        patch("kiro_crew.dashboard.slack_egress.vet_and_audit", return_value=permit),
        patch("kiro_crew.platform.governance_profiles.vet_and_audit", return_value=permit),
    ):
        yield


class TestNoticeText:
    def test_success_copy_rounds_pct(self) -> None:
        text = notice_text("discord", 91.7, success=True)
        assert "92%" in text
        assert "auto-compacted" in text

    def test_failure_copy_quotes_bang_commands_on_discord(self) -> None:
        text = notice_text("discord", 90.0, success=False)
        assert "!compact" in text
        assert "!new" in text

    def test_failure_copy_quotes_bang_commands_on_slack(self) -> None:
        assert "!compact" in notice_text("slack", 90.0, success=False)

    def test_failure_copy_falls_back_to_slash_commands(self) -> None:
        """An unchecked transport gets the conservative slash form, not a wrong bang."""
        text = notice_text("webex", 90.0, success=False)
        assert "/compact" in text
        assert "!compact" not in text

    def test_no_emoji_in_notices(self) -> None:
        for success in (True, False):
            text = notice_text("discord", 90.0, success=success)
            assert all(ord(ch) < 0x2190 for ch in text), text


@pytest.mark.asyncio
class TestSlackDelivery:
    async def test_posts_into_persisted_thread(self, monkeypatch) -> None:
        client = _SlackClient()
        state = _state(
            slack_client=client,
            sessions=_Sessions(slack_link=("ts-1", "C123")),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 92.0, success=True)

        assert len(client.posted) == 1
        channel, text, thread_ts = client.posted[0]
        assert (channel, thread_ts) == ("C123", "ts-1")
        assert "92%" in text

    async def test_posts_top_level_when_no_thread(self, monkeypatch) -> None:
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("", "C123")))

        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted[0][2] is None

    async def test_a_recipient_no_authority_names_is_still_delivered(self, monkeypatch) -> None:
        """Pins the DEFERRAL, not an ideal: this path does not re-authorize the
        recipient, so a notice still goes out where the hardened chain would refuse.

        The note mirror covers the refusing behaviour. This test exists so that
        adopting the chain here later is a visible, reviewed behaviour change --
        it must be updated in the same commit that widens the refusal, rather than
        the widening slipping in unnoticed as a rider.
        """
        import kiro_crew.slack.handler as h

        monkeypatch.setattr(h, "_tracking_channels", set(), raising=False)
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("ts-1", "C123")))

        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 92.0, success=True)

        assert len(client.posted) == 1, "the compaction notice unexpectedly re-authorizes"

    async def test_noop_without_client(self) -> None:
        state = _state(sessions=_Sessions(slack_link=("ts-1", "C123")))
        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

    async def test_noop_without_channel(self) -> None:
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("ts-1", None)))

        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted == []

    async def test_governance_denial_blocks_the_post(self) -> None:
        """Slack is absent from the transport ladder, so it needs its own gate."""
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("ts-1", "C123")))

        with patch(
            "kiro_crew.dashboard.slack_egress.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted == []

    async def test_decision_without_permitted_is_denial(self) -> None:
        """An unusable answer from the gate must not read as permission."""
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("ts-1", "C123")))

        with patch(
            "kiro_crew.dashboard.slack_egress.vet_and_audit",
            return_value=SimpleNamespace(),
        ):
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted == []

    async def test_governance_error_fails_closed(self) -> None:
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(slack_link=("ts-1", "C123")))

        with patch(
            "kiro_crew.dashboard.slack_egress.vet_and_audit",
            side_effect=RuntimeError("profile unreadable"),
        ):
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted == []

    async def test_swallows_post_failure(self) -> None:
        state = _state(
            slack_client=_SlackClient(fail=True),
            sessions=_Sessions(slack_link=("ts-1", "C123")),
        )
        # A failed notice must not propagate into the compaction task.
        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

    async def test_swallows_map_failure(self) -> None:
        client = _SlackClient()
        state = _state(slack_client=client, sessions=_Sessions(raise_on_lookup=True))

        with _permit_governance():
            await deliver_channel_compaction_notice(state, "slack:ts-1", 90.0, success=True)

        assert client.posted == []


@pytest.mark.asyncio
class TestTransportDelivery:
    async def test_sends_to_origin_conversation(self) -> None:
        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=ChannelLink("discord", channel_id="chan-9")),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 93.0, success=True
            )

        assert len(transport.sent) == 1
        conversation, text, thread = transport.sent[0]
        assert (conversation, thread) == ("chan-9", None)
        assert "93%" in text

    async def test_unified_bucket_uses_resolved_channel_for_hint(self) -> None:
        """A ``unified:`` key still quotes Discord's bang command, not ``/compact``."""
        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=ChannelLink("discord", channel_id="chan-9")),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "unified:kirocrew:direct:u1", 90.0, success=False
            )

        assert "!compact" in transport.sent[0][1]

    async def test_a_mirror_only_conversation_still_gets_the_notice(self) -> None:
        # Origin links are written by exactly ONE channel (Discord's resume path).
        # Telegram, WeCom and Weixin bind a MIRROR on their first turn, so an
        # origin-only lookup computed this notice and dropped it: the conversation
        # was summarized silently, and on a compaction FAILURE the operator never
        # saw the line telling them to run /compact or /new. Every other proactive
        # leg walks origin-then-mirror; this one now does too.
        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=None, mirror=ChannelLink("telegram", channel_id="4242")),
            channel_type="telegram",
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "telegram:kirocrew:direct:7", 91.0, success=False
            )

        assert len(transport.sent) == 1
        conversation, text, _thread = transport.sent[0]
        assert conversation == "4242"
        assert "/compact" in text, "the failure notice must name the manual command"

    async def test_origin_wins_when_both_are_bound(self) -> None:
        # The ladder's order is load-bearing: origin is the conversation's REAL send
        # target, a mirror is a copy destination.
        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(
                origin=ChannelLink("discord", channel_id="origin-9"),
                mirror=ChannelLink("discord", channel_id="mirror-9"),
            ),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

        assert transport.sent[0][0] == "origin-9"

    async def test_noop_without_any_link(self) -> None:
        transport = _Transport()
        state = _state(transport=transport, sessions=_Sessions(origin=None, mirror=None))

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

        assert transport.sent == []

    async def test_noop_when_transport_unregistered(self) -> None:
        state = _state(sessions=_Sessions(origin=ChannelLink("discord", channel_id="chan-9")))

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

    async def test_noop_when_governance_denies(self) -> None:
        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=ChannelLink("discord", channel_id="chan-9")),
        )

        # The ladder resolves vet_and_audit at its source (function-local import).
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            return_value=SimpleNamespace(permitted=False),
        ):
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

        assert transport.sent == []

    async def test_swallows_send_failure(self) -> None:
        transport = _Transport(fail=True)
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=ChannelLink("discord", channel_id="chan-9")),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

    async def test_the_binding_walk_goes_through_the_one_shared_ladder(self, monkeypatch) -> None:
        """The notice must not carry its own copy of the origin->mirror walk.

        THREE sites spelled that ladder by hand; this consolidates TWO of them --
        the note mirror's capture and this notice. A hand-written copy is how they
        come to disagree: a pause-awareness or ordering fix applied to one leaves
        the others silently on the old rule, and routing both through
        ``snapshot_channel_link`` is what makes THOSE TWO agree by construction.

        The third, ``slack/gateway.py``'s ``_channel_reply_link``, is deliberately
        NOT consolidated and is not claimed here: it excludes Slack and dashboard
        keys up front and falls back to a stored ``get_channel`` value when neither
        rung answers, so it is a longer ladder doing a partly different job. Folding
        it in would either widen the shared helper with rungs its other two callers
        must then reason about, or silently drop that fallback.

        Pinned by SUBSTITUTION rather than by reading the source: the shared helper
        is replaced, and the notice must end up at the destination the replacement
        returns. A copy of the walk would ignore it and reach the real binding
        instead, so this fails on any re-divergence.
        """
        from kiro_crew.dashboard.handlers import messaging as messaging_mod

        transport = _Transport()
        state = _state(
            transport=transport,
            sessions=_Sessions(origin=ChannelLink("discord", channel_id="REAL")),
        )

        seen: list[bool] = []

        def _fake_snapshot(_state, _key, *, skip_paused=False):
            # Records the pause posture the notice asks for and hands back a
            # destination the real ladder would never produce.
            seen.append(skip_paused)
            return ChannelLink("discord", channel_id="SUBSTITUTED"), True

        monkeypatch.setattr(messaging_mod, "snapshot_channel_link", _fake_snapshot)

        with _permit_governance():
            await deliver_channel_compaction_notice(
                state, "discord:kirocrew:direct:u1", 90.0, success=True
            )

        assert seen == [False], (
            "the notice must consult the shared ladder exactly once and be "
            f"pause-BLIND (a paused conversation still needs telling), got {seen}"
        )
        assert transport.sent, "the notice was not delivered at all"
        assert transport.sent[0][0] == "SUBSTITUTED", (
            "the notice reached the REAL binding, so it resolved the link itself "
            "instead of through snapshot_channel_link -- the hand-written walk is "
            f"back. Delivered to {transport.sent[0][0]!r}."
        )


@pytest.mark.asyncio
class TestNonChannelKeys:
    @pytest.mark.parametrize(
        "key",
        ["cron:daily-digest", "heartbeat", "subagent:abc", "hook:webhook-1", "_bg"],
    )
    async def test_no_delivery_for_non_channel_session(self, key: str) -> None:
        client = _SlackClient()
        transport = _Transport()
        state = _state(
            slack_client=client,
            transport=transport,
            sessions=_Sessions(
                slack_link=("ts-1", "C123"),
                origin=ChannelLink("discord", channel_id="chan-9"),
            ),
        )

        with _permit_governance():
            await deliver_channel_compaction_notice(state, key, 95.0, success=True)

        assert client.posted == []
        assert transport.sent == []


class TestOriginLinkStore:
    """The notice target lives in memory on SessionManager, not in SessionMap.

    Its lifetime is the SESSION's: a target is only ever needed to talk about a
    live session, and a gateway restart takes the session with it. Keeping it out
    of the session map also keeps disk I/O and cross-thread state off the
    transport's turn path.
    """

    def _manager(self, tmp_path):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        with patch("kiro_crew.session_map.config_dir", return_value=tmp_path):
            return SessionManager(KiroCrewConfig())

    def test_round_trip(self, tmp_path) -> None:
        mgr = self._manager(tmp_path)
        link = ChannelLink("discord", channel_id="chan-9")
        mgr.set_origin_link("discord:kirocrew:direct:u1", link)
        assert mgr.get_origin_link("discord:kirocrew:direct:u1") == link

    def test_absent_returns_none(self, tmp_path) -> None:
        assert self._manager(tmp_path).get_origin_link("discord:nope") is None

    def test_overwrites_previous_target(self, tmp_path) -> None:
        mgr = self._manager(tmp_path)
        mgr.set_origin_link("discord:k", ChannelLink("discord", channel_id="c1"))
        mgr.set_origin_link("discord:k", ChannelLink("discord", channel_id="c2"))
        got = mgr.get_origin_link("discord:k")
        assert got is not None and got.channel_id == "c2"

    def test_not_persisted_to_the_session_map(self, tmp_path) -> None:
        """No session-map write means no whole-map rewrite on a turn path."""
        mgr = self._manager(tmp_path)
        mgr.set_origin_link("discord:k", ChannelLink("discord", channel_id="c1"))
        assert not hasattr(mgr._session_map, "set_origin_link")
        assert mgr._session_map._data.get("discord:k") is None

    @pytest.mark.asyncio
    async def test_evicted_with_the_session(self, tmp_path) -> None:
        mgr = self._manager(tmp_path)
        mgr.set_origin_link("discord:k", ChannelLink("discord", channel_id="c1"))
        await mgr.remove("discord:k")
        assert mgr.get_origin_link("discord:k") is None

    def test_bounded_against_unevicted_growth(self, tmp_path) -> None:
        from kiro_crew.session import _MAX_ORIGIN_LINKS

        mgr = self._manager(tmp_path)
        for i in range(_MAX_ORIGIN_LINKS + 10):
            mgr.set_origin_link(f"discord:k{i}", ChannelLink("discord", channel_id=str(i)))
        assert len(mgr._origin_links) == _MAX_ORIGIN_LINKS
        # FIFO: the oldest keys went first, the newest survive.
        assert mgr.get_origin_link("discord:k0") is None
        assert mgr.get_origin_link(f"discord:k{_MAX_ORIGIN_LINKS + 9}") is not None
