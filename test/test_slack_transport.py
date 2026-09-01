"""Tests for v1d -- SlackTransport (Slack as the first MessagingTransport).

Focus on the security-critical inbound path (deny-by-default owner-only
authorization + bot-drop) and faithful Tier-1 delegation to the underlying
``SlackClientOps`` client. The transport is additive: no gateway wiring is
exercised here (the real dispatch callback is injected/faked).
"""

from __future__ import annotations

import pytest

from kiro_crew.messaging.transport import InboundMessage, MessagingTransport
from kiro_crew.slack.format import SLACK_MSG_LIMIT
from kiro_crew.slack.transport import SlackTransport


class FakeClient:
    """Minimal duck-typed SlackClientOps for the methods the transport uses."""

    def __init__(self) -> None:
        self.posted: list[tuple] = []
        self.dms: list[str] = []
        self.replies: list[dict] = []

    async def post_message(self, channel, text, thread_ts=None, **kw):
        self.posted.append((channel, text, thread_ts))
        return "1700.0001"

    async def open_dm(self, user_id):
        self.dms.append(user_id)
        return f"D-{user_id}"

    async def fetch_thread_replies(self, channel, thread_ts, limit=200, warn_on_pagination=True):
        return self.replies


def _t(**kw) -> SlackTransport:
    return SlackTransport(FakeClient(), **kw)


class TestContract:
    def test_is_messaging_transport(self):
        assert isinstance(_t(), MessagingTransport)

    def test_channel_type(self):
        assert _t().channel_type == "slack"

    def test_capabilities_reflect_slack(self):
        cap = _t().capabilities
        assert cap.streaming and cap.rich_blocks and cap.threads and cap.reactions
        assert cap.max_message_chars == SLACK_MSG_LIMIT

    def test_client_is_exposed(self):
        c = FakeClient()
        assert SlackTransport(c).client is c  # G2


class TestAuthorize:
    def test_deny_by_default_empty_allowlist(self):
        t = _t()  # no allowed_users
        assert t.authorize(InboundMessage("slack", "U_OWNER", "C1", "hi")) is False

    def test_owner_allowed(self):
        t = _t(allowed_users={"U_OWNER"})
        assert t.authorize(InboundMessage("slack", "U_OWNER", "C1", "hi")) is True

    def test_non_owner_denied(self):
        t = _t(allowed_users={"U_OWNER"})
        assert t.authorize(InboundMessage("slack", "U_OTHER", "C1", "hi")) is False

    def test_empty_user_denied(self):
        t = _t(allowed_users={"U_OWNER"})
        assert t.authorize(InboundMessage("slack", "", "C1", "hi")) is False

    def test_denial_audited_even_with_empty_user(self, monkeypatch):
        # Deny-by-default must be observable: SEL audit fires for ALL denials,
        # including empty/missing user_id (caller falls back to "unknown").
        from unittest.mock import MagicMock

        import kiro_crew.slack.transport as transport_mod

        rec = MagicMock()
        monkeypatch.setattr(transport_mod, "sel", lambda: rec)
        t = _t(allowed_users={"U_OWNER"})

        assert t.authorize(InboundMessage("slack", "", "C1", "hi")) is False
        rec.log_api_access.assert_called_once()
        kwargs = rec.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "unknown"
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "slack_transport.authorize"

    def test_allowlist_is_frozen_snapshot(self):
        live = {"U_OWNER"}
        t = SlackTransport(FakeClient(), allowed_users=live)
        live.add("U_INTRUDER")  # mutate the source set after construction
        assert t.authorize(InboundMessage("slack", "U_INTRUDER", "C1", "x")) is False


class TestTier1:
    @pytest.mark.asyncio
    async def test_send_message_delegates(self):
        c = FakeClient()
        ts = await SlackTransport(c).send_message("C1", "hello", "1700.0")
        assert ts == "1700.0001"
        assert c.posted == [("C1", "hello", "1700.0")]

    @pytest.mark.asyncio
    async def test_resolve_conversation_delegates(self):
        c = FakeClient()
        ch = await SlackTransport(c).resolve_conversation("U1")
        assert ch == "D-U1"

    @pytest.mark.asyncio
    async def test_fetch_history_normalizes(self):
        c = FakeClient()
        c.replies = [
            {"user": "U1", "text": "first"},
            {"bot_id": "B1", "text": "from bot"},
        ]
        msgs = await SlackTransport(c).fetch_history("C1", "1700.0")
        assert [(m.user_id, m.text) for m in msgs] == [("U1", "first"), ("B1", "from bot")]
        assert all(m.channel_type == "slack" and m.thread_id == "1700.0" for m in msgs)

    @pytest.mark.asyncio
    async def test_fetch_history_no_thread_is_empty(self):
        assert await _t().fetch_history("C1", None) == []


class TestReceive:
    @pytest.mark.asyncio
    async def test_authorized_message_dispatched(self):
        seen: list[InboundMessage] = []

        async def dispatch(m):
            seen.append(m)

        t = SlackTransport(FakeClient(), allowed_users={"U_OWNER"}, dispatch=dispatch)
        await t.receive({"event": {"user": "U_OWNER", "channel": "C1", "text": "hi", "ts": "9.9"}})
        assert len(seen) == 1
        assert seen[0].user_id == "U_OWNER" and seen[0].thread_id == "9.9"

    @pytest.mark.asyncio
    async def test_bot_message_dropped(self):
        seen = []

        async def dispatch(m):
            seen.append(m)

        t = SlackTransport(FakeClient(), allowed_users={"U_OWNER"}, dispatch=dispatch)
        await t.receive({"event": {"bot_id": "B1", "channel": "C1", "text": "spam"}})
        assert seen == []

    @pytest.mark.asyncio
    async def test_unauthorized_user_dropped(self):
        seen = []

        async def dispatch(m):
            seen.append(m)

        t = SlackTransport(FakeClient(), allowed_users={"U_OWNER"}, dispatch=dispatch)
        await t.receive({"event": {"user": "U_OTHER", "channel": "C1", "text": "hi"}})
        assert seen == []

    @pytest.mark.asyncio
    async def test_no_dispatch_is_safe(self):
        t = SlackTransport(FakeClient(), allowed_users={"U_OWNER"})  # dispatch=None
        # Should not raise even for an authorized message.
        await t.receive({"event": {"user": "U_OWNER", "channel": "C1", "text": "hi"}})


class TestReceiveTrustedBots:
    """The trusted_bot_ids second allow-list, mirroring the events.py gate."""

    @staticmethod
    def _collector():
        seen: list[InboundMessage] = []

        async def dispatch(m):
            seen.append(m)

        return seen, dispatch

    @pytest.mark.asyncio
    async def test_trusted_bot_admitted(self, monkeypatch):
        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        seen, dispatch = self._collector()
        t = SlackTransport(
            FakeClient(),
            allowed_users={"U_OWNER"},
            trusted_bot_ids={"B_PEER"},
            dispatch=dispatch,
        )
        # A trusted bot's message commonly carries subtype == "bot_message";
        # the trust decision must run first so the subtype gate does not eat it.
        await t.receive(
            {
                "event": {
                    "bot_id": "B_PEER",
                    "subtype": "bot_message",
                    "channel": "C1",
                    "text": "ping",
                    "ts": "1.1",
                }
            }
        )
        assert len(seen) == 1
        # The bot_id stands in as the sender id, as on the events.py path.
        assert seen[0].user_id == "B_PEER"

    @pytest.mark.asyncio
    async def test_untrusted_bot_still_dropped(self, monkeypatch):
        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        seen, dispatch = self._collector()
        t = SlackTransport(
            FakeClient(),
            allowed_users={"U_OWNER"},
            trusted_bot_ids={"B_PEER"},
            dispatch=dispatch,
        )
        await t.receive({"event": {"bot_id": "B_EVIL", "channel": "C1", "text": "spam"}})
        assert seen == []

    @pytest.mark.asyncio
    async def test_untrusted_bot_denial_is_audited(self, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        rec = MagicMock()
        monkeypatch.setattr(transport_mod, "sel", lambda: rec)
        t = SlackTransport(FakeClient(), trusted_bot_ids={"B_PEER"})
        await t.receive({"event": {"bot_id": "B_EVIL", "channel": "C1", "text": "spam"}})
        kwargs = rec.log_api_access.call_args.kwargs
        assert kwargs["caller"] == "B_EVIL"
        assert kwargs["outcome"] == "denied"
        assert kwargs["operation"] == "slack_transport.receive"
        assert kwargs["error"] == "untrusted_bot"

    @pytest.mark.asyncio
    async def test_trusted_admission_is_audited(self, monkeypatch):
        from unittest.mock import MagicMock

        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        rec = MagicMock()
        monkeypatch.setattr(transport_mod, "sel", lambda: rec)
        seen, dispatch = self._collector()
        t = SlackTransport(FakeClient(), trusted_bot_ids={"B_PEER"}, dispatch=dispatch)
        await t.receive({"event": {"bot_id": "B_PEER", "channel": "C1", "text": "ping"}})
        assert len(seen) == 1
        kwargs = rec.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "allowed"
        assert kwargs["resources"] == "trusted_bot"

    @pytest.mark.asyncio
    async def test_own_bot_never_trusted_even_when_listed(self, monkeypatch):
        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        seen, dispatch = self._collector()
        t = SlackTransport(FakeClient(), trusted_bot_ids={"B_SELF"}, dispatch=dispatch)
        await t.receive({"event": {"bot_id": "B_SELF", "channel": "C1", "text": "echo"}})
        assert seen == []

    @pytest.mark.asyncio
    async def test_unverified_self_id_fails_closed(self, monkeypatch):
        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "")
        seen, dispatch = self._collector()
        t = SlackTransport(FakeClient(), trusted_bot_ids={"B_PEER"}, dispatch=dispatch)
        await t.receive({"event": {"bot_id": "B_PEER", "channel": "C1", "text": "ping"}})
        assert seen == []

    @pytest.mark.asyncio
    async def test_trusted_set_is_frozen_snapshot(self, monkeypatch):
        import kiro_crew.slack.transport as transport_mod

        monkeypatch.setattr(transport_mod, "validated_self_bot_id", lambda: "B_SELF")
        seen, dispatch = self._collector()
        live = {"B_PEER"}
        t = SlackTransport(FakeClient(), trusted_bot_ids=live, dispatch=dispatch)
        live.add("B_INTRUDER")  # mutate the source set after construction
        await t.receive({"event": {"bot_id": "B_INTRUDER", "channel": "C1", "text": "x"}})
        assert seen == []
