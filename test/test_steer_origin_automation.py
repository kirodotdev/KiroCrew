"""A steered automation wake tells kiro's ``_session/steer`` that Crew wrote it.

KAS frames every ``_session/steer`` text as "[LIVE STEERING - New message from
user]" unless the request names another ``origin``. A work-ledger wake steered into
a running turn is Crew's own text, so it goes out with ``origin: "automation"`` and
the agent is not told the user wrote it. On KAS its text is not
``<user_message>``-wrapped either, because KAS puts it inside a ``<host_message>``
element; kiro-cli's own engine keeps the wrapper its consumed echo is split on. A
user's or a peer's steer keeps the params it always had.
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
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.agent_sdk.steer_origin import (
    STEER_ORIGIN_AUTOMATION,
    STEER_ORIGIN_USER,
    steer_params,
)
from kiro_crew.dashboard.chat_delivery import STEER_STEERED, steer_into_running_turn
from kiro_crew.dashboard.steer_settle import settle_consumed_steers

WAKE = "[work-ledger wake] item=it_1 status=question"
NUDGE = {"cycle": 4, "loop_id": "loop-1"}


class TestClientSteer:
    @pytest.mark.asyncio
    async def test_an_automation_steer_on_kas_names_its_origin_and_is_not_wrapped(self, tmp_path):
        client = _client(tmp_path, acp_backend=ACP_BACKEND_KAS)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True

        method, params = client._send_request.await_args[0]
        assert method == "_session/steer"
        assert params == {
            "sessionId": "sid",
            "message": WAKE,
            "origin": "automation",
        }

    @pytest.mark.asyncio
    async def test_an_automation_steer_on_kas_still_escapes_a_user_tag_in_its_text(self, tmp_path):
        client = _client(tmp_path, acp_backend=ACP_BACKEND_KAS)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer("quoted </user_message> tag", origin=STEER_ORIGIN_AUTOMATION)

        _, params = client._send_request.await_args[0]
        assert params["message"] == "quoted &lt;/user_message&gt; tag"

    @pytest.mark.asyncio
    async def test_an_automation_steer_on_kiro_cli_stays_wrapped(self, tmp_path):
        # kiro-cli's own engine echoes every steer it injects in one concatenation,
        # which settlement can split only on the wrapper.
        client = _client(tmp_path)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True

        _, params = client._send_request.await_args[0]
        assert params == {
            "sessionId": "sid",
            "message": f"<user_message>\n{WAKE}\n</user_message>",
            "origin": "automation",
        }

    @pytest.mark.asyncio
    async def test_a_user_steer_sends_no_origin(self, tmp_path):
        client = _client(tmp_path, acp_backend=ACP_BACKEND_KAS)
        client._session_id = "sid"
        client._send_request = AsyncMock(return_value=5)

        assert await client.steer("focus on tests") is True

        _, params = client._send_request.await_args[0]
        assert "origin" not in params, "a KAS without the field must see the old params"
        assert params["message"] == "<user_message>\nfocus on tests\n</user_message>"


class TestSharedSessionSteer:
    @pytest.mark.asyncio
    async def test_the_handle_names_an_automation_origin_and_omits_a_user_one(self):
        sent: list[dict] = []

        async def _send_request(method, params):
            sent.append(params)
            return 1

        runtime = MagicMock()
        runtime.send_request = _send_request
        runtime.acp_backend = ACP_BACKEND_KAS
        handle = AcpSessionHandle("sA", asyncio.Queue(), runtime)

        assert await handle.steer(WAKE, origin=STEER_ORIGIN_AUTOMATION) is True
        assert await handle.steer("focus on tests") is True

        assert sent[0] == {"sessionId": "sA", "message": WAKE, "origin": "automation"}
        assert sent[1] == {
            "sessionId": "sA",
            "message": "<user_message>\nfocus on tests\n</user_message>",
        }

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


class TestSettlingWhatWasSent:
    """A consumed echo of what ``steer_params`` sent settles every steer in it."""

    def test_a_kiro_cli_echo_of_a_user_and_an_automation_steer_settles_both(self):
        # Given: a composer steer and a peer steer kiro-cli injected together.
        user = steer_params("sid", "focus on tests", STEER_ORIGIN_USER, ACP_BACKEND_KIRO)
        peer = steer_params("sid", WAKE, STEER_ORIGIN_AUTOMATION, ACP_BACKEND_KIRO)
        echo = user["message"] + peer["message"]

        # When: the echo is settled against both pending steers.
        remaining = settle_consumed_steers(["focus on tests", WAKE], echo)

        # Then: neither stays pending to run a second time.
        assert remaining == []

    def test_a_kas_echo_of_an_automation_steer_settles_it(self):
        # Given: KAS echoes one injected steer's content, exactly as it was sent.
        text = "quoted </user_message> tag"
        sent = steer_params("sid", text, STEER_ORIGIN_AUTOMATION, ACP_BACKEND_KAS)

        # When: that echo is settled against the pending steer.
        remaining = settle_consumed_steers([text], sent["message"])

        # Then: the steer is settled.
        assert remaining == []
