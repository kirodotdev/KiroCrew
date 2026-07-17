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
