"""The claude harness's ``ultracode`` switch, read and written per session.

claude-agent-acp (0.97.0 and later) advertises ``ultracode`` in ``configOptions`` only
on a model that supports it, as an ``on``/``off`` select. Crew surfaces it only for the
claude harness: the session reports it through ``selection-capabilities`` while the
option is advertised, and ``POST /ultracode`` writes it with
``session/set_config_option`` and answers the value the adapter's reply carries.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.dashboard.chat import (
    api_chat_slot_selection_capabilities,
    api_chat_slot_ultracode,
)
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider


def _ultracode_option(value: str) -> dict:
    # The select-fallback shape claude-agent-acp 0.97.0 builds for a client that did
    # not opt into boolean config options (dist/session-ultracode.js).
    return {
        "id": "ultracode",
        "name": "Ultracode",
        "category": "model_config",
        "type": "select",
        "currentValue": value,
        "options": [{"value": "on", "name": "On"}, {"value": "off", "name": "Off"}],
    }


def _provider(backend: str, options: list[dict]) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    client = MagicMock()
    client.backend = backend
    client.acp_config_options = options
    client.has_active_turn = MagicMock(return_value=False)

    async def _write(config_id: str, value: str) -> None:
        # The adapter answers with the session's full configOptions, which the
        # client stores in place of the old ones.
        client.acp_config_options = [_ultracode_option(value)]

    client.set_config_option = AsyncMock(side_effect=_write)
    provider._client = client
    return provider


@pytest.mark.asyncio
async def test_a_claude_session_writes_its_switch_and_reads_the_reply_back():
    provider = _provider(ACP_BACKEND_CLAUDE, [_ultracode_option("off")])
    assert provider.ultracode_enabled() is False

    assert await provider.set_ultracode(True) is True

    provider._client.set_config_option.assert_awaited_once_with("ultracode", "on")
    assert provider.ultracode_enabled() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "options"),
    [
        # Another harness advertising an option of the same id is not offered it.
        (ACP_BACKEND_KIRO, [_ultracode_option("off")]),
        (ACP_BACKEND_KAS, [_ultracode_option("off")]),
        (ACP_BACKEND_CODEX, [_ultracode_option("off")]),
        # A claude model without ultracode support advertises no option.
        (ACP_BACKEND_CLAUDE, [{"id": "effort", "type": "select", "currentValue": "high"}]),
    ],
)
async def test_no_switch_is_offered_or_written_without_the_claude_option(backend, options):
    provider = _provider(backend, options)

    assert provider.ultracode_enabled() is None
    assert await provider.set_ultracode(True) is None
    provider._client.set_config_option.assert_not_awaited()


def _app(provider: AcpProvider) -> tuple[web.Application, _ChatSlot]:
    slot = _ChatSlot("test")
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    state.sessions.try_acquire = AsyncMock(return_value=True)
    state.sessions.release = MagicMock()
    state.sessions._fold_key = MagicMock(side_effect=lambda key: key)
    app = web.Application()
    app["state"] = state
    app.router.add_get(
        "/api/chat/slots/{slot}/selection-capabilities", api_chat_slot_selection_capabilities
    )
    app.router.add_post("/api/chat/slots/{slot}/ultracode", api_chat_slot_ultracode)
    return app, slot


@pytest.mark.asyncio
async def test_the_dashboard_reports_the_switch_and_writes_it_live():
    provider = _provider(ACP_BACKEND_CLAUDE, [_ultracode_option("off")])
    app, _slot = _app(provider)
    sessions = app["state"].sessions
    with (
        patch.object(AcpProvider, "supports_effort", return_value=False),
        patch("kiro_crew.dashboard.chat_handlers._own_models_field", AsyncMock(return_value={})),
    ):
        async with TestClient(TestServer(app)) as client:
            caps = await (await client.get("/api/chat/slots/test/selection-capabilities")).json()
            assert caps["ultracode"] is False

            resp = await client.post("/api/chat/slots/test/ultracode", json={"enabled": True})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "ultracode": True}

            caps = await (await client.get("/api/chat/slots/test/selection-capabilities")).json()
            assert caps["ultracode"] is True
    provider._client.set_config_option.assert_awaited_once_with("ultracode", "on")
    sessions.try_acquire.assert_awaited_once_with("dashboard:test")
    sessions.release.assert_called_once_with("dashboard:test", wake_queue=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "busy", "code"),
    [
        (ACP_BACKEND_KIRO, False, "ultracode_not_offered"),
        # A write mid-turn would read the harness's output beside the streaming turn.
        (ACP_BACKEND_CLAUDE, True, "turn_in_flight"),
    ],
)
async def test_the_dashboard_refuses_a_write_it_cannot_make_safely(backend, busy, code):
    provider = _provider(backend, [_ultracode_option("off")])
    provider._client.has_active_turn = MagicMock(return_value=busy)
    app, _slot = _app(provider)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/ultracode", json={"enabled": True})
        assert resp.status == 409
        assert (await resp.json())["code"] == code
    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_dashboard_refuses_a_write_while_a_channel_turn_holds_the_session():
    # A channel turn takes the lease before its prompt is in flight, so the provider
    # reports no active turn yet; its prompt would read the stdout the write waits on.
    provider = _provider(ACP_BACKEND_CLAUDE, [_ultracode_option("off")])
    app, _slot = _app(provider)
    app["state"].sessions.try_acquire = AsyncMock(return_value=False)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/test/ultracode", json={"enabled": True})
        assert resp.status == 409
        assert (await resp.json())["code"] == "turn_in_flight"
    provider._client.set_config_option.assert_not_awaited()
    app["state"].sessions.release.assert_not_called()
