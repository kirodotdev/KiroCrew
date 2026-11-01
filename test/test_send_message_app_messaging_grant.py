"""Every proactive egress leg re-vets the calling app's messaging grant.

The MCP tool vets ``capabilities.messaging`` in a process that knows no app, so a
per-app profile that revokes messaging only bites if the gateway asks again with
the app it attested. Each leg is driven through the real route, once with the app
denied (nothing leaves, 403 ``channel_not_permitted``) and once with it permitted
(the same request delivers), so a refusal cannot come from a leg that had simply
stopped working. ``update_message``'s edit route is a leg too: an edit publishes new
text to the audience the message already has.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import api_send_message, api_update_message
from kiro_crew.discord.transport import DiscordTransport
from kiro_crew.messaging.link import ChannelLink

APP = "test-app"
ROOM = "C0ROOM123"
EDITED_TS = "1712793600.000001"
OWNER_DM = "D0OWNERDM"
OWNER_ID = "424242424242424242"
DM_CHANNEL = "999000111222333444"
TG_KEY = "telegram:kirocrew:dm:99887766"
TG_LINK = ChannelLink(channel_type="telegram", channel_id="99887766", thread_id="17")


@web.middleware
async def _stamp_app(request: web.Request, handler: Any) -> web.StreamResponse:
    """Stand in for ``token_auth_middleware``: publish the app claim it attests."""
    claimed = request.headers.get("X-Test-App", "")
    if claimed:
        request["app"] = claimed
    return await handler(request)


def _app(state: MagicMock) -> web.Application:
    app = web.Application(middlewares=[_stamp_app])
    app.router.add_post("/api/send-message", api_send_message)
    app.router.add_post("/api/update-message", api_update_message)
    app["state"] = state
    return app


def _decision(permitted: bool) -> MagicMock:
    decision = MagicMock()
    decision.permitted = permitted
    return decision


class _Governance:
    """``vet_and_audit`` double: ``channels`` always permits; ``capabilities.messaging``
    answers *app_may_message* for APP and records every call."""

    def __init__(self, app_may_message: bool) -> None:
        self.app_may_message = app_may_message
        self.calls: list[tuple[str, str]] = []
        self.tool_names: list[str] = []

    def __call__(self, scope: str, member: str, **kwargs: Any) -> MagicMock:
        app = str(kwargs.get("app", "") or "")
        self.calls.append((scope, app))
        if scope == "capabilities.messaging":
            self.tool_names.append(str(kwargs.get("tool_name", "")))
        if scope == "capabilities.messaging" and app == APP:
            return _decision(self.app_may_message)
        return _decision(True)

    def messaging_vets(self) -> list[str]:
        return [app for scope, app in self.calls if scope == "capabilities.messaging"]


def _governance(app_may_message: bool) -> tuple[_Governance, Any]:
    gov = _Governance(app_may_message)
    return gov, patch("kiro_crew.platform.governance_profiles.vet_and_audit", side_effect=gov)


@pytest.fixture(autouse=True)
def _audit():
    with patch("kiro_crew.sel.sel") as factory:
        factory.return_value = MagicMock()
        yield


def _state(**overrides: Any) -> MagicMock:
    state = MagicMock()
    # An empty slot registry, so the app comes from the stamped claim alone and a
    # MagicMock slot cannot be read as some other app owning the caller.
    state._slots = {}
    state.slack_client = None
    state.owner_id = ""
    state.get_channel_transport = MagicMock(return_value=None)
    state.channel_transports = {}
    for name, value in overrides.items():
        setattr(state, name, value)
    return state


def _discord() -> DiscordTransport:
    client = MagicMock()
    client.create_dm_channel = AsyncMock(return_value=DM_CHANNEL)
    client.send_message = AsyncMock(return_value="1234567890")
    client.is_thread_channel = AsyncMock(return_value=True)
    return DiscordTransport(client, allowed_user_ids=(OWNER_ID,), allowed_thread_ids=())


def _telegram() -> MagicMock:
    transport = MagicMock()
    transport.send_message = AsyncMock(return_value="tg-1")
    transport.capabilities.supports_proactive_send = True
    transport.capabilities.max_message_chars = 4096
    return transport


def _slack() -> MagicMock:
    slack = MagicMock()
    slack.open_dm = AsyncMock(return_value="D_OWNER")
    slack.post_message = AsyncMock(return_value="1712793600.000001")
    slack.update_message = AsyncMock()
    return slack


async def _post(
    state: MagicMock, body: dict, headers: dict, path: str = "/api/send-message"
) -> tuple[int, dict]:
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(path, json=body, headers=headers)
        return resp.status, await resp.json()


async def _edit(state: MagicMock, channel: str, headers: dict) -> tuple[int, dict]:
    body = {"channel": channel, "ts": EDITED_TS, "text": "edited"}
    return await _post(state, body, headers, path="/api/update-message")


class TestEveryLegRefusesAnAppWithoutMessaging:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_configured_target_leg(self, permitted: bool) -> None:
        transport = _discord()
        state = _state(channel_transports={"discord": transport})
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _post(
                state,
                {"text": "hello", "channel_type": "discord", "target_id": f"user:{OWNER_ID}"},
                {"X-Test-App": APP},
            )
        assert gov.messaging_vets() == [APP]
        if permitted:
            assert status == 200, data
            transport.client.send_message.assert_awaited_once_with(DM_CHANNEL, "hello")
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            # Refused before the target resolves: opening a DM is itself visible.
            transport.client.create_dm_channel.assert_not_called()
            transport.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_owner_dm_leg(self, permitted: bool) -> None:
        transport = _discord()
        state = _state(
            get_channel_transport=MagicMock(
                side_effect=lambda channel_type: transport if channel_type == "discord" else None
            )
        )
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _post(
                state, {"text": "hello", "session": "discord"}, {"X-Test-App": APP}
            )
        assert gov.messaging_vets() == [APP]
        if permitted:
            assert status == 200, data
            assert data["delivered_to"] == "discord"
            transport.client.send_message.assert_awaited_once_with(DM_CHANNEL, "hello")
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            transport.client.create_dm_channel.assert_not_called()
            transport.client.send_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_channel_type_leg(self, permitted: bool) -> None:
        transport = _telegram()
        state = _state(get_channel_transport=MagicMock(return_value=transport))
        state.sessions.get_origin_link.return_value = TG_LINK
        state.sessions.get_mirror_link.return_value = None
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _post(
                state,
                {"text": "hello", "channel_type": "telegram"},
                {"X-Test-App": APP, "X-Session-Key": TG_KEY},
            )
        assert gov.messaging_vets() == [APP]
        if permitted:
            assert status == 200, data
            assert data["delivered_to"] == "telegram"
            transport.send_message.assert_awaited_once_with("99887766", "hello", thread_id="17")
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            assert data["error"].startswith("telegram ")
            transport.send_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_slack_leg(self, permitted: bool) -> None:
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _post(
                state, {"text": "hello", "session": "slack"}, {"X-Test-App": APP}
            )
        assert gov.messaging_vets() == [APP]
        if permitted:
            assert status == 200, data
            assert data["delivered_to"] == "slack"
            slack.post_message.assert_awaited_once()
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            assert data["error"].startswith("slack ")
            slack.open_dm.assert_not_called()
            slack.post_message.assert_not_called()


class TestTheVetFailsClosed:
    @pytest.mark.asyncio
    async def test_an_evaluation_error_refuses_the_app(self) -> None:
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            side_effect=RuntimeError("profile store unreadable"),
        ):
            status, data = await _post(
                state, {"text": "hello", "session": "slack"}, {"X-Test-App": APP}
            )
        assert status == 403
        assert data["code"] == "channel_not_permitted"
        slack.post_message.assert_not_called()


class TestThePersonIsNotReVetted:
    @pytest.mark.asyncio
    async def test_a_caller_no_app_owns_sends_without_the_app_vet(self) -> None:
        """No app claim is the person, whose surface profile the MCP tool vets."""
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        gov, patched = _governance(False)
        with patched:
            status, data = await _post(state, {"text": "hello", "session": "slack"}, {})
        assert status == 200, data
        assert gov.messaging_vets() == []
        slack.post_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_bell_only_send_is_not_an_egress_leg(self) -> None:
        """No channel and no Slack: the dashboard bell is not a leg the grant gates."""
        state = _state()
        gov, patched = _governance(False)
        with patched:
            status, data = await _post(state, {"text": "hello"}, {"X-Test-App": APP})
        assert status == 200, data
        assert data["delivered_to"] == "notification"
        assert gov.messaging_vets() == []


class TestTheEditLegRefusesAnAppWithoutMessaging:
    @pytest.fixture(autouse=True)
    def _room_is_tracked(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("kiro_crew.slack.handler.is_tracked_channel", lambda cid: cid == ROOM)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_a_room_edit(self, permitted: bool) -> None:
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _edit(state, ROOM, {"X-Test-App": APP})
        assert gov.messaging_vets() == [APP]
        assert gov.tool_names == ["update_message"]
        if permitted:
            assert status == 200, data
            slack.update_message.assert_awaited_once_with(ROOM, EDITED_TS, "edited", None)
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            assert data["error"].startswith("slack ")
            slack.update_message.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("permitted", [False, True])
    async def test_an_owner_dm_edit(self, permitted: bool) -> None:
        slack = _slack()
        slack.open_dm = AsyncMock(return_value=OWNER_DM)
        state = _state(slack_client=slack, owner_id="U_OWNER")
        gov, patched = _governance(permitted)
        with patched:
            status, data = await _edit(state, OWNER_DM, {"X-Test-App": APP})
        assert gov.messaging_vets() == [APP]
        if permitted:
            assert status == 200, data
            slack.update_message.assert_awaited_once()
        else:
            assert status == 403
            assert data["code"] == "channel_not_permitted"
            # Refused before the owner DM is resolved: opening a DM is itself visible.
            slack.open_dm.assert_not_called()
            slack.update_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_evaluation_error_refuses_the_edit(self) -> None:
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        with patch(
            "kiro_crew.platform.governance_profiles.vet_and_audit",
            side_effect=RuntimeError("profile store unreadable"),
        ):
            status, data = await _edit(state, ROOM, {"X-Test-App": APP})
        assert status == 403
        assert data["code"] == "channel_not_permitted"
        slack.update_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_persons_edit_is_not_re_vetted(self) -> None:
        """No app claim is the person, whose surface profile the MCP tool vets."""
        slack = _slack()
        state = _state(slack_client=slack, owner_id="U_OWNER")
        gov, patched = _governance(False)
        with patched:
            status, data = await _edit(state, ROOM, {})
        assert status == 200, data
        assert gov.messaging_vets() == []
        slack.update_message.assert_awaited_once()
