"""Crew's own notices and peer messages steer as automation, not as the user.

kiro's ``_session/steer`` frames a text as "[LIVE STEERING - New message from
user]" unless the request names ``origin: "automation"``. A tool-refusal notice,
a repeat-loop notice, a ``spawn_steer`` from a parent agent and a message another
session sent are not the user's words, so each steer names that origin, and the
provider wrapper the notices go through passes it on.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state
from steer_requeue_helpers import _patch_sel, _running_slot  # noqa: F401 -- fixture

from kiro_crew.agent_sdk.steer_origin import STEER_ORIGIN_AUTOMATION
from kiro_crew.dashboard import chat_runner, session_deliver, steer_retract
from kiro_crew.dashboard.chat_delivery import steer_into_running_turn
from kiro_crew.deny_notice import steer_notice_into_refused_turn
from kiro_crew.providers import acp_turns


def _steer_client() -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.supports_steer_retract = True
    client.supports_refusal_steer = True
    client.steer = AsyncMock(return_value=True)
    client.clear_pending_steers = AsyncMock(return_value=True)
    return client


class TestNoticeSteers:
    @pytest.mark.asyncio
    async def test_a_policy_notice_steers_as_automation(self):
        client = _steer_client()
        notices: list[str] = []

        sent = await chat_runner._steer_policy_notice(
            client, "execute_bash", "blocked by policy", notices
        )

        assert sent is True
        assert client.steer.await_args.kwargs == {"origin": STEER_ORIGIN_AUTOMATION}

    @pytest.mark.asyncio
    async def test_a_repeat_loop_notice_steers_as_automation(self):
        client = _steer_client()

        sent = await chat_runner._steer_repeat_loop_notice(client, "stop repeating")

        assert sent is True
        client.steer.assert_awaited_once_with("stop repeating", origin=STEER_ORIGIN_AUTOMATION)

    @pytest.mark.asyncio
    async def test_a_refusal_notice_through_a_provider_steers_as_automation(self):
        provider = _steer_client()

        sent = await steer_notice_into_refused_turn(provider, "the call was refused")

        assert sent is True
        provider.steer.assert_awaited_once_with(
            "the call was refused", origin=STEER_ORIGIN_AUTOMATION
        )

    @pytest.mark.asyncio
    async def test_the_acp_provider_passes_the_origin_to_its_client(self):
        provider = MagicMock()
        provider.steer_needs_loss_recovery = False
        provider._client = _steer_client()

        await acp_turns.steer(provider, "notice", origin=STEER_ORIGIN_AUTOMATION)

        provider._client.steer.assert_awaited_once_with("notice", origin=STEER_ORIGIN_AUTOMATION)


class TestPeerSteers:
    @pytest.mark.asyncio
    async def test_another_sessions_message_steers_as_automation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()
        text = "[sent by session peer-1 via session_send]\n\nlook at the logs"

        await session_deliver.deliver(
            state, slot, text, session_deliver.peer(steer=True), admission={}
        )

        slot._acp_client.steer.assert_awaited_once_with(text, origin=STEER_ORIGIN_AUTOMATION)

    @pytest.mark.asyncio
    async def test_a_composer_steer_after_a_peer_steer_is_still_the_users(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        slot._acp_client = _steer_client()

        await steer_into_running_turn(
            state, slot, "from a peer", user_origin=False, peer_origin=True, admission={}
        )
        await steer_into_running_turn(state, slot, "typed", user_origin=True, admission={})

        calls = slot._acp_client.steer.await_args_list
        assert calls[0].kwargs == {"origin": STEER_ORIGIN_AUTOMATION}
        assert calls[1].args == ("typed",) and calls[1].kwargs == {}
        assert slot._steer_peer_origin == {"from a peer"}

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_patch_sel")
    async def test_a_peer_steer_the_clear_dropped_is_sent_again_as_automation(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client = _steer_client()
        slot._acp_client = client
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_steer_retract

        app = _make_app(state)
        app.router.add_post(
            "/api/chat/slots/{slot}/steers/{mid}/retract", api_chat_slot_steer_retract
        )
        async with TestClient(TestServer(app)) as http:
            resp = await http.post(
                "/api/chat", json={"slot": "test", "message": "take me back", "steer": True}
            )
            assert (await resp.json()).get("steered") is True
            await steer_into_running_turn(
                state, slot, "from a peer", user_origin=False, peer_origin=True, admission={}
            )
            mid = next(
                m["meta"]["mid"]
                for m in slot.messages
                if (m.get("meta") or {}).get("steer") is True and m["content"] == "take me back"
            )
            resp = await http.post(
                f"/api/chat/slots/test/steers/{mid}/retract", json={"action": "unsend"}
            )
            assert resp.status == 202
        client.steer.reset_mock()

        await steer_retract.on_dropped(state, slot)

        client.steer.assert_awaited_once_with("from a peer", origin=STEER_ORIGIN_AUTOMATION)
