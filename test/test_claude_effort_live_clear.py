"""Clearing a claude session's effort live, with the adapter's own ``default`` value.

claude-agent-acp's effort select offers ``default`` beside the levels, and a write of
it drops the session's pinned level (``claude/effort-clear-live.jsonl``). So a clear
on a live claude slot writes that value instead of answering False, which made the
dashboard kill and cold-start the session. A harness with no clear value keeps the
reset, and so does a claude session that did not advertise the value or refused it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_frame_replay_harness import read_fixture
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
    effort_config_option_clear_value,
)
from kiro_crew.agent_sdk.backends import (
    EFFORT_CONFIG_OPTION_CLEAR_VALUES,
    EFFORT_CONFIG_OPTION_NO_OVERRIDE_VALUES,
    effort_config_option_id,
    effort_config_option_levels,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat import api_chat_slot_reasoning_effort
from kiro_crew.dashboard.state import DashboardState, _ChatSlot
from kiro_crew.providers.acp import AcpProvider

_CAPTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "claude" / "effort-clear-live.jsonl"
_MODEL = "claude-opus-4.7"


def _effort_option(frame: dict) -> dict:
    return next(o for o in frame["result"]["configOptions"] if o["id"] == "effort")


def _captured_frames() -> list[dict]:
    _meta, frames = read_fixture(_CAPTURE)
    return frames


def _captured_levels() -> list[str]:
    """The effort values the captured ``session/new`` advertised, as the client reads them."""
    session_new = next(
        f
        for f in _captured_frames()
        if isinstance(f.get("result"), dict) and "sessionId" in f["result"]
    )
    values = [o["value"] for o in _effort_option(session_new)["options"]]
    return effort_config_option_levels(ACP_BACKEND_CLAUDE, values)


def _provider(backend: str, levels: list[str]) -> AcpProvider:
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=backend)
    provider._client = MagicMock()
    provider._client.backend = backend
    provider._client._model = _MODEL
    provider._client.has_active_turn = MagicMock(return_value=False)
    provider._client.set_config_option = AsyncMock()
    provider._client.send_command = AsyncMock()
    provider._client.supports_config_option = MagicMock(return_value=True)
    provider._client.get_valid_effort_levels = MagicMock(return_value=levels)
    provider._effort_per_model = {_MODEL: "high"}
    return provider


def test_the_capture_shows_default_accepted_after_a_level():
    # The evidence the clear value rests on: the last two frames answer the
    # driver's effort=high and effort=default writes on one session, each read back.
    responses = [f for f in _captured_frames() if f.get("id") in (3, 4)]
    assert [_effort_option(f)["currentValue"] for f in responses] == ["high", "default"]
    assert all("error" not in f for f in responses)


def test_only_claude_has_a_live_clear_value():
    assert dict(EFFORT_CONFIG_OPTION_CLEAR_VALUES) == {ACP_BACKEND_CLAUDE: "default"}
    assert {b for b in ACP_BACKENDS_KNOWN if effort_config_option_clear_value(b)} == {
        ACP_BACKEND_CLAUDE
    }


def test_no_backend_hides_its_own_clear_value():
    # The live clear checks the value against the advertised levels, which a
    # no-override row strips, so a backend carrying both rows could never clear live.
    for backend, value in EFFORT_CONFIG_OPTION_CLEAR_VALUES.items():
        assert value not in EFFORT_CONFIG_OPTION_NO_OVERRIDE_VALUES.get(backend, frozenset())


@pytest.mark.asyncio
async def test_claude_clear_writes_default_live():
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    assert await provider.clear_effort() is True
    provider._client.set_config_option.assert_awaited_once_with(
        effort_config_option_id(ACP_BACKEND_CLAUDE), "default"
    )
    assert _MODEL not in provider._effort_per_model


@pytest.mark.asyncio
async def test_claude_clear_without_an_advertised_default_asks_for_a_reset():
    levels = [level for level in _captured_levels() if level != "default"]
    provider = _provider(ACP_BACKEND_CLAUDE, levels)
    assert await provider.clear_effort() is False
    provider._client.set_config_option.assert_not_awaited()
    assert _MODEL not in provider._effort_per_model


@pytest.mark.asyncio
async def test_claude_clear_without_an_effort_option_asks_for_a_reset():
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._client.supports_config_option = MagicMock(return_value=False)
    assert await provider.clear_effort() is False
    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_refused_claude_clear_restores_the_level_and_asks_for_a_reset():
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._client.set_config_option = AsyncMock(
        side_effect=AcpError("Invalid value for config option effort: default")
    )
    assert await provider.clear_effort() is False
    provider._client.set_config_option.assert_awaited_once()
    assert provider._effort_per_model[_MODEL] == "high"


@pytest.mark.asyncio
async def test_claude_clear_with_an_inherited_level_asks_for_a_reset():
    # A cold start applies the crew/role/chat level, not the adapter's default,
    # so writing ``default`` live would run a level the composer does not show.
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._inherited_effort = "low"
    assert await provider.clear_effort(every_model=True) is False
    provider._client.set_config_option.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_slot_clear_drops_every_model_level_so_a_fallback_restore_keeps_default():
    # High on model A, a throttle fallback onto B, then Default on B: a fallback
    # restore to A must not find A's level still stored and re-apply it.
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._effort_per_model = {"claude-sonnet-4.6": "high"}
    assert await provider.clear_effort(every_model=True) is True
    assert provider._effort_per_model == {}


@pytest.mark.asyncio
async def test_a_model_switch_clear_keeps_the_other_models_levels():
    # set_model's re-resolve calls clear_effort without every_model: the slot's
    # level still stands, and a stored level for another model stays for it.
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._effort_per_model = {"claude-sonnet-4.6": "high"}
    assert await provider.clear_effort() is True
    assert provider._effort_per_model == {"claude-sonnet-4.6": "high"}


@pytest.mark.asyncio
async def test_a_refused_slot_clear_puts_every_model_level_back():
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._effort_per_model = {_MODEL: "high", "claude-sonnet-4.6": "max"}
    provider._client.set_config_option = AsyncMock(side_effect=AcpError("refused"))
    assert await provider.clear_effort(every_model=True) is False
    assert provider._effort_per_model == {_MODEL: "high", "claude-sonnet-4.6": "max"}


@pytest.mark.parametrize(
    ("backend", "expected"), [(ACP_BACKEND_CLAUDE, "low"), (ACP_BACKEND_KIRO, "")]
)
def test_the_factory_hands_the_inherited_level_only_to_a_clear_value_harness(
    tmp_path, backend, expected
):
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.acp_backend = backend
    cfg.agent.reasoning_effort = "low"
    with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
        mock_provider.return_value = MagicMock()
        cfg.create_provider_factory()(
            cwd=str(tmp_path),
            session_key="dashboard:1",
            model_override=_MODEL,
            reasoning_effort_override="high",
        )
    assert mock_provider.call_args.kwargs["inherited_effort"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE])
async def test_a_backend_without_a_clear_value_still_asks_for_a_reset(backend):
    provider = _provider(backend, ["default", "low", "medium", "high"])
    with patch.object(AcpProvider, "supports_effort", return_value=True):
        assert await provider.clear_effort() is False
    provider._client.set_config_option.assert_not_awaited()


def _handler_state(slot: _ChatSlot, provider: AcpProvider) -> MagicMock:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    return state


@pytest.mark.asyncio
async def test_the_dashboard_clears_a_live_claude_slot_without_a_reset():
    provider = _provider(ACP_BACKEND_CLAUDE, _captured_levels())
    provider._effort_per_model["claude-sonnet-4.6"] = "high"
    slot = _ChatSlot("test")
    slot.reasoning_effort = "high"
    state = _handler_state(slot, provider)
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/reasoning-effort", api_chat_slot_reasoning_effort)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/slots/test/reasoning-effort", json={"reasoning_effort": ""}
        )
        assert resp.status == 200
    assert slot.reasoning_effort == ""
    provider._client.set_config_option.assert_awaited_once_with("effort", "default")
    state.sessions.reset.assert_not_called()
    # A slot clear: no level stays stored for the model the session ran before.
    assert provider._effort_per_model == {}
