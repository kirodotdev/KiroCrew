"""Tests for kiro_crew.wecom.transport (WeComTransport, Layer 1)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.wecom.client import WeComInbound
from kiro_crew.wecom.transport import WECOM_CAPABILITIES, WeComTransport


class FakeClient:
    """Minimal WeComClient stand-in recording lifecycle + sends."""

    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.replies: list[tuple[str, str]] = []

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True

    async def send_reply(self, url: str, content: str) -> None:
        self.replies.append((url, content))


def _inbound(userid: str = "Wei", text: str = "hi") -> WeComInbound:
    return WeComInbound(userid=userid, text=text, response_url="https://r", req_id="rq1", chatid="")


class TestCapabilities:
    def test_wecom_shape(self) -> None:
        cap = WECOM_CAPABILITIES
        assert cap.streaming is True
        assert cap.edit is True
        assert cap.max_buttons == 0  # no tappable chips
        assert cap.supports_proactive_send is False  # reply bound to inbound
        assert cap.max_message_chars == 20000


class TestConfiguredTargets:
    def test_allow_all_policy_remains_visible_but_unavailable(self) -> None:
        transport = WeComTransport(FakeClient(), allow_all=True)

        assert [target.to_dict("wecom") for target in transport.configured_targets()] == [
            {
                "channel_type": "wecom",
                "target_id": "policy:all",
                "label": "WeCom · organization users",
                "available": False,
                "unavailable_reason": "WeCom only allows replies to an inbound message",
            }
        ]


class TestAuthorize:
    def test_owner_allowed(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei")
        assert t.authorize(_msg("Wei")) is True

    def test_allowlist_member_allowed(self) -> None:
        t = WeComTransport(FakeClient(), allowed_users=["Wei", "LiHaoYi"])
        assert t.authorize(_msg("LiHaoYi")) is True

    def test_unknown_denied(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei", allowed_users=["LiHaoYi"])
        with patch("kiro_crew.wecom.transport.sel") as mock_sel:
            assert t.authorize(_msg("stranger")) is False
        mock_sel().log_api_access.assert_called_once()

    def test_empty_userid_denied(self) -> None:
        t = WeComTransport(FakeClient(), owner_id="Wei")
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("")) is False

    def test_empty_allowlist_and_no_owner_denies_everyone(self) -> None:
        t = WeComTransport(FakeClient())  # fail closed
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("anyone")) is False

    def test_allow_all_admits_any_userid(self) -> None:
        t = WeComTransport(FakeClient(), allow_all=True)  # explicit opt-in
        assert t.authorize(_msg("anyone")) is True
        assert t.authorize(_msg("someone-else")) is True

    def test_allow_all_still_denies_empty_userid(self) -> None:
        # Even under allow-all, an anonymous/malformed frame never dispatches.
        t = WeComTransport(FakeClient(), allow_all=True)
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("")) is False

    def test_allow_all_off_is_not_inferred_from_empty_list(self) -> None:
        # The everybody grant is ONLY the explicit flag — an empty allow-list
        # plus allow_all=False stays fail-closed.
        t = WeComTransport(FakeClient(), allowed_users=[], allow_all=False)
        with patch("kiro_crew.wecom.transport.sel"):
            assert t.authorize(_msg("anyone")) is False


class TestReceive:
    @pytest.mark.asyncio
    async def test_authorized_dispatches_inbound(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive(_inbound("Wei", "hello"))
        assert len(dispatched) == 1
        assert dispatched[0].text == "hello"
        assert dispatched[0].req_id == "rq1"

    @pytest.mark.asyncio
    async def test_unauthorized_does_not_dispatch(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        with patch("kiro_crew.wecom.transport.sel"):
            await t.receive(_inbound("stranger", "hello"))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_empty_text_dropped(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive(_inbound("Wei", ""))
        assert dispatched == []

    @pytest.mark.asyncio
    async def test_non_wecom_envelope_dropped(self) -> None:
        dispatched: list[WeComInbound] = []

        async def dispatch(inbound: WeComInbound) -> None:
            dispatched.append(inbound)

        t = WeComTransport(FakeClient(), owner_id="Wei", dispatch=dispatch)
        await t.receive({"not": "a WeComInbound"})
        assert dispatched == []


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_connect_disconnect_delegate(self) -> None:
        client = FakeClient()
        t = WeComTransport(client, owner_id="Wei")
        await t.connect()
        assert client.started is True
        await t.disconnect()
        assert client.closed is True


def _msg(userid: str) -> InboundMessage:
    return InboundMessage(channel_type="wecom", user_id=userid, conversation_id=userid, text="hi")
