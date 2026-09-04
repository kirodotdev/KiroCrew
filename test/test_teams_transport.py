"""Tests for the Microsoft Teams transport layer.

Covers the declared capabilities, import purity (messaging must not import
teams), deny-by-default authorization, direct/personal-only scope gating,
unresolved-identity fail-closed, and inbound normalization -> dispatch.
"""

from __future__ import annotations

from typing import Any

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.teams.client import TeamsInbound
from kiro_crew.teams.transport import TEAMS_CAPABILITIES, TeamsTransport


class _FakeSel:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def log_api_access(self, **kwargs: Any) -> None:
        self.events.append(kwargs)


class _FakeClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send_message(self, conversation_id: str, content: str, service_url: str):
        self.sent.append((conversation_id, content, service_url))
        return "mid-1"


def _dm(text: str = "hi", email: str = "alice@example.com", aad: str = "aad-1") -> TeamsInbound:
    return TeamsInbound(
        conversation_id="conv-1",
        conversation_type="personal",
        service_url="https://smba.trafficmanager.net/",
        text=text,
        user_email=email,
        aad_object_id=aad,
        activity_id="act-1",
    )


class TestCapabilities:
    def test_teams_shape(self) -> None:
        # streaming stays False: Teams' native token streaming is 1:1-only,
        # throttled to 1 req/s and cut off at two minutes, which an agent turn
        # exceeds -- a stream that dies mid-answer is worse than one message.
        assert TEAMS_CAPABILITIES.streaming is False
        # edit is True because the renderer really does rewrite its own activities
        # (PUT .../activities/{id}) for the progress message and queue receipt.
        assert TEAMS_CAPABILITIES.edit is True
        # Adaptive Cards make interactive choices real, so the cap must be > 0 or
        # apply_options_cap discards every chip.
        assert TEAMS_CAPABILITIES.rich_blocks is True
        assert TEAMS_CAPABILITIES.max_buttons == 5
        # A bot cannot ADD a reaction in Teams (messageReaction is inbound-only),
        # so a steer is acknowledged with a message instead.
        assert TEAMS_CAPABILITIES.reactions is False
        assert TEAMS_CAPABILITIES.supports_proactive_send is True
        assert TEAMS_CAPABILITIES.max_message_chars > 0


# The messaging-package import-purity invariant is enforced for EVERY forbidden
# package (all eight channels plus ``dashboard``) in
# ``test/test_messaging_import_purity.py``. The teams-only copy that used to live
# here named one package, so a ``dashboard`` edge added to ``messaging/`` while
# hoisting shared channel code went unnoticed. One gate over the whole set is the
# fix; adding a channel means adding its name there, not writing another test.


class TestAuthorize:
    def test_allow_list_permits_member(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=["Alice@example.com"])
        msg = InboundMessage(
            channel_type="teams", user_id="alice@example.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is True

    def test_deny_by_default_and_audit(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"])
        msg = InboundMessage(
            channel_type="teams", user_id="mallory@evil.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is False
        assert any(e["outcome"] == "denied" for e in fake.events)

    def test_empty_allow_list_denies_everyone(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=[])
        msg = InboundMessage(
            channel_type="teams", user_id="alice@example.com", conversation_id="c", text="x"
        )
        assert t.authorize(msg) is False


class TestReceive:
    @pytest.mark.asyncio
    async def test_personal_message_dispatches(self, monkeypatch) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch)
        await t.receive(_dm())
        assert len(dispatched) == 1
        # serviceUrl learned for the conversation
        assert t.service_url_for("conv-1") == "https://smba.trafficmanager.net/"

    @pytest.mark.asyncio
    async def test_configured_target_becomes_available_after_inbound(self) -> None:
        async def _dispatch(inbound: TeamsInbound) -> None:
            return None

        transport = TeamsTransport(
            _FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch
        )
        assert transport.configured_targets()[0].available is False
        await transport.receive(_dm())
        assert transport.configured_targets()[0].available is True
        assert await transport.resolve_configured_target("user:alice@example.com") == (
            "conv-1",
            None,
        )

    @pytest.mark.asyncio
    async def test_channel_scope_denied_and_audited(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch)
        inb = _dm()
        inb.conversation_type = "channel"
        await t.receive(inb)
        assert dispatched == []
        assert any(e["outcome"] == "denied_non_personal_scope" for e in fake.events)

    @pytest.mark.asyncio
    async def test_unresolved_identity_fails_closed(self, monkeypatch) -> None:
        fake = _FakeSel()
        monkeypatch.setattr("kiro_crew.teams.transport.sel", lambda: fake)
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch)
        # Neither email nor AAD object id -> unresolved -> denied.
        await t.receive(_dm(email="", aad=""))
        assert dispatched == []
        assert any(e["outcome"] == "denied_unresolved_identity" for e in fake.events)

    @pytest.mark.asyncio
    async def test_aad_object_id_fallback_authorized(self) -> None:
        # Email absent (typical Teams activity) but the AAD object id is on the
        # allow-list -> dispatches (feature works out of the box).
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["aad-42"], dispatch=_dispatch)
        await t.receive(_dm(email="", aad="aad-42"))
        assert len(dispatched) == 1
        # ...and an unlisted object id is denied.
        dispatched.clear()
        await t.receive(_dm(email="", aad="aad-99"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_no_turn(self) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch)
        await t.receive(_dm(text=""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_teams_envelope_dropped(self) -> None:
        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"])
        # Should not raise
        await t.receive({"not": "a TeamsInbound"})

    @pytest.mark.asyncio
    async def test_non_allowed_email_denied(self, monkeypatch) -> None:
        dispatched: list[TeamsInbound] = []

        async def _dispatch(inb: TeamsInbound) -> None:
            dispatched.append(inb)

        t = TeamsTransport(_FakeClient(), allowed_emails=["alice@example.com"], dispatch=_dispatch)
        await t.receive(_dm(email="mallory@evil.com"))
        assert dispatched == []
