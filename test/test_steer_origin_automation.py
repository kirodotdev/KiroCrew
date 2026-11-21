"""A steered automation wake tells kiro's ``_session/steer`` that Crew wrote it.

KAS frames every ``_session/steer`` text as "[LIVE STEERING - New message from
user]" unless the request names another ``origin``. A work-ledger wake steered into
a running turn is Crew's own text, so it goes out with ``origin: "automation"`` and
the agent is not told the user wrote it. A user's or a peer's steer keeps the
params it always had.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_more_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _client,
    _pinned_kiro_cli_version,
)
from chat_test_helpers import _make_state
from steer_requeue_helpers import _running_slot

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.steer_origin import STEER_ORIGIN_AUTOMATION
from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn

WAKE = "[work-ledger wake] item=it_1 status=question"
NUDGE = {"cycle": 4, "loop_id": "loop-1"}


class TestClientSteer:
    @pytest.mark.asyncio
    async def test_an_automation_steer_names_its_origin(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True

        method, params = client._send_request.await_args[0]
        assert method == "_session/steer"
        assert params == {
            "sessionId": "sid",
            "message": f"<user_message>\n{WAKE}\n</user_message>",
            "origin": "automation",
        }

    @pytest.mark.asyncio
    async def test_a_user_steer_sends_no_origin(self, tmp_path):
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer("focus on tests") is True

        _, params = client._send_request.await_args[0]
        assert "origin" not in params, "a KAS without the field must see the old params"


class TestSharedSessionSteer:
    @pytest.mark.asyncio
    async def test_the_handle_names_an_automation_origin_and_omits_a_user_one(self):
        sent: list[dict] = []

        async def _send_request(method, params):
            sent.append(params)
            return 1

        runtime = MagicMock()
        runtime.send_request = _send_request
        runtime.acp_backend = ACP_BACKEND_KIRO
        handle = AcpSessionHandle("sA", asyncio.Queue(), runtime)

        assert await handle.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True
        assert await handle.steer("focus on tests") is True

        assert sent[0]["origin"] == "automation"
        assert "origin" not in sent[1]

    @pytest.mark.asyncio
    async def test_the_session_provider_passes_the_origin_to_its_handle(self):
        handle = MagicMock()
        handle.steer = MagicMock(return_value="the handle's steer call")
        provider = AcpSessionProvider.__new__(AcpSessionProvider)
        provider._handle = handle
        provider._guarded = AsyncMock(return_value=True)

        assert await provider.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True

        handle.steer.assert_called_once_with(WAKE, origin=STEER_ORIGIN_AUTOMATION)
        assert provider._guarded.await_args.args[0] == "the handle's steer call"


class TestDashboardSteer:
    @pytest.mark.asyncio
    async def test_a_steered_work_ledger_wake_goes_out_as_automation(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        outcome = await steer_into_running_turn(
            state, slot, WAKE, user_origin=False, admission={}, nudge_meta=NUDGE
        )

        assert outcome == STEER_STEERED
        client.steer.assert_awaited_once_with(WAKE, origin=STEER_ORIGIN_AUTOMATION)

    @pytest.mark.asyncio
    async def test_a_composer_steer_passes_no_origin(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = _running_slot(state)
        client = MagicMock()
        client.supports_steer = True
        client.steer = AsyncMock(return_value=True)
        slot._acp_client = client

        await steer_into_running_turn(state, slot, "focus on tests", user_origin=True, admission={})

        client.steer.assert_awaited_once_with("focus on tests")
