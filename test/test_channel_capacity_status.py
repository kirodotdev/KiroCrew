"""A full Channels capacity cap answers 409 Conflict, not 429.

Both caps clear only when the user closes a channel or dismisses an agent, so
waiting never helps. 429 tells clients and proxies to retry; 409 does not.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers_channel as handlers
from kiro_crew.channel import ChannelManager


def _request(manager: ChannelManager, body: dict, **match_info: str) -> MagicMock:
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(channel_manager=manager, sessions=MagicMock(), _yolo=False)
    }
    request.match_info = match_info
    request.json = AsyncMock(return_value=body)
    return request


async def _invoke(handler, request) -> web.StreamResponse:
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return exc


@pytest.fixture(autouse=True)
def spawn_spy(_floor_monkeypatch) -> MagicMock:
    spy = MagicMock()
    _floor_monkeypatch.setattr(handlers, "_spawn_agent_task", spy)
    return spy


@pytest.mark.asyncio
async def test_channel_cap_full_answers_409_with_its_code(tmp_path):
    manager = ChannelManager(channels_dir=str(tmp_path / "channels"), max_channels=1)
    assert manager.create("first") is not None

    response = await _invoke(
        handlers.api_channel_create, _request(manager, {"topic": "second", "agents": []})
    )

    assert response.status == 409
    body = json.loads(response.text)
    assert body["code"] == "channel_limit_reached"
    assert body["error"] == "Channel limit reached. Close an existing channel first."
    assert len(manager.list_channels()) == 1


@pytest.mark.asyncio
async def test_agent_cap_full_answers_409_with_its_code(tmp_path, spawn_spy):
    manager = ChannelManager(channels_dir=str(tmp_path / "channels"), max_agents=1)
    channel = manager.create("incident")
    assert channel.add_agent(role="Orchestrator", is_orchestrator=True) is not None
    spawn_spy.reset_mock()

    response = await _invoke(
        handlers.api_channel_add_agent,
        _request(manager, {"role": "Helper"}, id=channel.id),
    )

    assert response.status == 409
    body = json.loads(response.text)
    assert body["code"] == "channel_agent_limit_reached"
    assert body["error"] == "Agent limit reached. Dismiss an agent first."
    assert len(channel.members) == 1
    spawn_spy.assert_not_called()
