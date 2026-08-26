"""Regression test: session/set_mode must be called for ALL agents, not just default.

Bug introduced in 24f98e5 (2026-02-27) — set_mode was skipped for custom agents
under the incorrect assumption that --agent CLI flag alone activates the agent.
In reality, --agent loads the agent config but set_mode is required to activate
the agent's prompt/persona in the session.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from kiro_crew.acp.client import CLIENT_NAME, AcpClient
from kiro_crew.acp.types import METHOD_SET_MODE


def _make_client(agent: str, tmp_path) -> AcpClient:
    c = AcpClient(work_dir=tmp_path, agent=agent)
    c._session_id = "sess-123"
    c._resume_session_id = None
    return c


async def _fake_wait(rid, timeout=None):
    """Return minimal valid responses for each init step."""
    return {"protocolVersion": "1.0", "sessionId": "sess-123"}


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", [CLIENT_NAME, "ops", "code-reviewer", "my-custom-agent"])
async def test_set_mode_called_for_all_agents(agent, tmp_path):
    """session/set_mode must be sent regardless of agent name."""
    client = _make_client(agent, tmp_path)
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(side_effect=_fake_wait)
    client._drain_notifications = AsyncMock()

    with patch("pathlib.Path.exists", return_value=False), \
         patch("pathlib.Path.stat"):
        await client._initialize_session()

    set_mode_calls = [
        c for c in client._send_request.call_args_list
        if c.args[0] == METHOD_SET_MODE
    ]
    assert len(set_mode_calls) == 1, (
        f"set_mode not called for agent={agent!r}; calls: {client._send_request.call_args_list}"
    )
    assert set_mode_calls[0].args[1]["modeId"] == agent


async def _wait_with_modes(mode_ids):
    """Init-step response factory that advertises a `modes` payload."""

    async def _fake(rid, timeout=None):
        return {
            "protocolVersion": "1.0",
            "sessionId": "sess-123",
            "modes": {
                "currentModeId": "kirocrew",
                "availableModes": [{"id": m} for m in mode_ids],
            },
        }

    return _fake


@pytest.mark.asyncio
async def test_set_mode_fails_closed_when_agent_not_in_advertised_modes(tmp_path):
    """Guard (A): backend advertises a `modes` list that excludes the agent →
    FAIL CLOSED (raise), never silently run kiro-cli's default mode in place of
    the requested (possibly more-restricted) agent."""
    from kiro_crew.acp.client import AcpError

    client = _make_client("ghost-agent", tmp_path)
    client._session_id = None  # force the session/new path so modes are captured
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(side_effect=await _wait_with_modes(["kirocrew"]))
    client._drain_notifications = AsyncMock()

    with patch("pathlib.Path.exists", return_value=False), patch("pathlib.Path.stat"):
        with pytest.raises(AcpError, match="not available"):
            await client._initialize_session()

    set_mode_calls = [
        c for c in client._send_request.call_args_list if c.args[0] == METHOD_SET_MODE
    ]
    assert set_mode_calls == [], "the wrong agent mode must never be activated"


@pytest.mark.asyncio
async def test_set_mode_sent_when_agent_in_advertised_modes(tmp_path):
    """When the agent IS advertised, set_mode still fires with its modeId."""
    client = _make_client("ops", tmp_path)
    client._session_id = None  # force the session/new path so modes are captured
    client._send_request = AsyncMock(return_value=1)
    client._wait_for_response = AsyncMock(side_effect=await _wait_with_modes(["kirocrew", "ops"]))
    client._drain_notifications = AsyncMock()

    with patch("pathlib.Path.exists", return_value=False), patch("pathlib.Path.stat"):
        await client._initialize_session()

    set_mode_calls = [
        c for c in client._send_request.call_args_list if c.args[0] == METHOD_SET_MODE
    ]
    assert len(set_mode_calls) == 1
    assert set_mode_calls[0].args[1]["modeId"] == "ops"
