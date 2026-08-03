"""Tests for kiro_crew.webex.transport (WebexTransport, Layer 1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.transport import WEBEX_CAPABILITIES, WEBEX_SAFE_MESSAGE_CHARS, WebexTransport


class FakeClient:
    """Minimal WebexClient stand-in recording lifecycle + sends."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.sent: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send_message(self, conversation_id: str, markdown: str, **kw) -> str:
        self.sent.append((conversation_id, markdown))
        return "MSG1"


def _inbound(
    email: str = "kyle@example.com", text: str = "hi", room_type: str = "direct"
) -> WebexInbound:
    return WebexInbound(person_email=email, room_id="ROOM", text=text, room_type=room_type)


def _msg(email: str) -> InboundMessage:
    return InboundMessage(channel_type="webex", user_id=email, conversation_id="ROOM", text="hi")


class TestCapabilities:
    def test_webex_shape(self) -> None:
        cap = WEBEX_CAPABILITIES
        assert cap.streaming is False  # 10-edit cap rules out typewriter edits
        assert cap.edit is True
        assert cap.max_buttons == 0  # no tappable chips
        assert cap.supports_proactive_send is True
        assert cap.max_message_chars == WEBEX_SAFE_MESSAGE_CHARS


class TestAuthorize:
    def test_allowlist_member_allowed(self) -> None:
        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"])
        assert t.authorize(_msg("kyle@example.com")) is True

    def test_case_insensitive(self) -> None:
        t = WebexTransport(FakeClient(), allowed_emails=["Kyle@Example.COM"])
        assert t.authorize(_msg("kyle@example.com")) is True

    def test_unknown_denied(self) -> None:
        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"])
        with patch("kiro_crew.webex.transport.sel") as mock_sel:
            assert t.authorize(_msg("stranger@example.com")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_empty_email_denied(self) -> None:
        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"])
        with patch("kiro_crew.webex.transport.sel"):
            assert t.authorize(_msg("")) is False

    def test_empty_allowlist_denies_everyone(self) -> None:
        t = WebexTransport(FakeClient())  # fail closed
        with patch("kiro_crew.webex.transport.sel"):
            assert t.authorize(_msg("anyone@example.com")) is False


class TestReceive:
    @pytest.mark.asyncio
    async def test_authorized_dispatches_inbound(self) -> None:
        dispatched: list[WebexInbound] = []

        async def dispatch(inbound: WebexInbound) -> None:
            dispatched.append(inbound)

        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"], dispatch=dispatch)
        await t.receive(_inbound("kyle@example.com", "hello"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "hello"
        assert dispatched[0].room_id == "ROOM"

    @pytest.mark.asyncio
    async def test_unauthorized_does_not_dispatch(self) -> None:
        dispatched: list[WebexInbound] = []

        async def dispatch(inbound: WebexInbound) -> None:
            dispatched.append(inbound)

        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"], dispatch=dispatch)
        with patch("kiro_crew.webex.transport.sel"):
            await t.receive(_inbound("stranger@example.com", "hello"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_group_room_denied_even_for_allowed_user(self) -> None:
        """Fail closed: a group-space reply would expose output to the room."""
        dispatched: list[WebexInbound] = []

        async def dispatch(inbound: WebexInbound) -> None:
            dispatched.append(inbound)

        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"], dispatch=dispatch)
        with patch("kiro_crew.webex.transport.sel") as mock_sel:
            await t.receive(_inbound("kyle@example.com", "hello", room_type="group"))
        assert dispatched == []
        mock_sel().log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        dispatched: list[WebexInbound] = []

        async def dispatch(inbound: WebexInbound) -> None:
            dispatched.append(inbound)

        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"], dispatch=dispatch)
        await t.receive(_inbound("kyle@example.com", ""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_webex_envelope_dropped(self) -> None:
        dispatched: list[WebexInbound] = []

        async def dispatch(inbound: WebexInbound) -> None:
            dispatched.append(inbound)

        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"], dispatch=dispatch)
        await t.receive({"not": "a WebexInbound"})
        assert dispatched == []


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect_delegate(self) -> None:
        client = FakeClient()
        t = WebexTransport(client, allowed_emails=["kyle@example.com"])
        await t.connect()
        assert client.started is True
        await t.disconnect()
        assert client.closed is True

    @pytest.mark.asyncio
    async def test_resolve_conversation_is_email(self) -> None:
        t = WebexTransport(FakeClient(), allowed_emails=["kyle@example.com"])
        assert await t.resolve_conversation("kyle@example.com") == "kyle@example.com"

    @pytest.mark.asyncio
    async def test_send_message_returns_id(self) -> None:
        client = FakeClient()
        t = WebexTransport(client, allowed_emails=["kyle@example.com"])
        mid = await t.send_message("ROOM", "content")
        assert mid == "MSG1"
        assert client.sent == [("ROOM", "content")]
