"""Tests for kiro_crew.webex.transport (WebexTransport, Layer 1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.webex.cards import MAX_CARD_ACTIONS
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
        # Adaptive Cards are Webex's Block Kit analogue, so choices DO render as
        # buttons now; Webex's own overview caps a card at five actions.
        assert cap.max_buttons == MAX_CARD_ACTIONS
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


class TestRoomGate:
    """Group spaces are an opt-in with their OWN allow-list, and both must pass.

    A reply in a space is readable by every member, including people the email
    allow-list excludes — which is why this channel is DM-first and why turning
    the switch on alone grants nothing.
    """

    @staticmethod
    def _transport(**kw):
        from kiro_crew.webex.transport import WebexTransport

        return WebexTransport(_FakeClient(), allowed_emails=["kyle@example.com"], **kw)

    @staticmethod
    def _inbound(room_type: str, room_id: str = "ROOM"):
        from kiro_crew.webex.client import WebexInbound

        return WebexInbound(
            person_email="kyle@example.com", room_id=room_id, text="hi", room_type=room_type
        )

    def test_a_direct_room_is_always_permitted(self) -> None:
        assert self._transport().room_permitted(self._inbound("direct"))

    def test_a_group_room_is_refused_by_default(self) -> None:
        assert not self._transport().room_permitted(self._inbound("group"))

    def test_the_switch_alone_grants_nothing(self) -> None:
        # Deny-all room list: the operator must ALSO name the space.
        t = self._transport(allow_group_rooms=True, allowed_room_ids=[])
        assert not t.room_permitted(self._inbound("group"))

    def test_a_named_space_with_the_switch_on_is_permitted(self) -> None:
        t = self._transport(allow_group_rooms=True, allowed_room_ids=["ROOM"])
        assert t.room_permitted(self._inbound("group"))

    def test_an_unnamed_space_is_refused_even_with_the_switch_on(self) -> None:
        t = self._transport(allow_group_rooms=True, allowed_room_ids=["OTHER"])
        assert not t.room_permitted(self._inbound("group"))

    @pytest.mark.parametrize("room_type", ["", "unknown", "future-type"])
    def test_an_unrecognised_room_type_is_refused(self, room_type: str) -> None:
        """Positive membership, not ``!= "direct"``.

        A room type this code has never seen must not inherit whatever a direct
        room gets — that is the permissive direction.
        """
        t = self._transport(allow_group_rooms=True, allowed_room_ids=["ROOM"])
        assert not t.room_permitted(self._inbound(room_type))


class TestConfiguredTargets:
    @staticmethod
    def _transport(**kw):
        from kiro_crew.webex.transport import WebexTransport

        return WebexTransport(_FakeClient(), allowed_emails=["kyle@example.com"], **kw)

    def test_dm_targets_are_exposed(self) -> None:
        ids = [t.target_id for t in self._transport().configured_targets()]
        assert ids == ["user:kyle@example.com"]

    def test_space_targets_appear_only_when_group_rooms_are_on(self) -> None:
        assert [
            t.target_id for t in self._transport(allowed_room_ids=["R1"]).configured_targets()
        ] == ["user:kyle@example.com"]
        with_rooms = self._transport(allow_group_rooms=True, allowed_room_ids=["R1"])
        assert "room:R1" in [t.target_id for t in with_rooms.configured_targets()]

    @pytest.mark.asyncio
    async def test_resolution_revalidates_against_the_allow_list(self) -> None:
        """The id travelled through the browser or the model.

        Re-checking here is what makes a config that narrowed after the id was
        minted actually take effect.
        """
        t = self._transport(allow_group_rooms=True, allowed_room_ids=["R1"])
        assert await t.resolve_configured_target("user:kyle@example.com") == (
            "kyle@example.com",
            None,
        )
        assert await t.resolve_configured_target("room:R1") == ("R1", None)
        # Not configured, wrong kind, and malformed all refuse.
        assert await t.resolve_configured_target("user:someone@else.com") is None
        assert await t.resolve_configured_target("room:R9") is None
        assert await t.resolve_configured_target("bogus:R1") is None
        assert await t.resolve_configured_target("nocolon") is None

    @pytest.mark.asyncio
    async def test_a_space_target_is_refused_when_group_rooms_are_off(self) -> None:
        t = self._transport(allowed_room_ids=["R1"])
        assert await t.resolve_configured_target("room:R1") is None


class TestThreading:
    @pytest.mark.asyncio
    async def test_the_thread_id_is_forwarded(self) -> None:
        """``threads=True`` is only honest if the id is actually used.

        Declaring the capability while dropping the argument is exactly the drift
        the capability ledger exists to catch.
        """
        from kiro_crew.webex.transport import WebexTransport

        client = _FakeClient()
        t = WebexTransport(client, allowed_emails=["kyle@example.com"])
        await t.send_message("ROOM", "hi", thread_id="ROOT")
        assert client.sent[-1]["parent_id"] == "ROOT"


class _FakeClient:
    """Records outbound calls; enough surface for the transport's contract."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_message(self, conversation_id, markdown, *, parent_id=None, **kw):
        self.sent.append(
            {"conversation_id": conversation_id, "markdown": markdown, "parent_id": parent_id}
        )
        return "MSG1"

    async def list_messages(self, query: str) -> list[dict]:
        return []
