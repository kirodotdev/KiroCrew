"""Input-contract regressions for the dashboard Channels write API."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.channel import ChannelManager
from kiro_crew.dashboard.handlers_channel import (
    api_channel_approve_agent,
    api_channel_clear_context,
    api_channel_create,
    api_channel_post,
)


def _request(manager: ChannelManager, body: object, **match_info: str) -> MagicMock:
    request = MagicMock()
    request.app = {
        "state": SimpleNamespace(channel_manager=manager, sessions=MagicMock(reset=AsyncMock()))
    }
    request.match_info = match_info
    request.json = AsyncMock(return_value=body)
    return request


async def _invoke(handler, request) -> web.StreamResponse:
    """Return HTTP exceptions as responses, matching aiohttp middleware."""
    try:
        return await handler(request)
    except web.HTTPException as exc:
        return exc


@pytest.fixture
def manager(tmp_path) -> ChannelManager:
    return ChannelManager(channels_dir=str(tmp_path / "channels"))


@pytest.mark.asyncio
async def test_create_rejects_non_object_json(manager):
    response = await _invoke(api_channel_create, _request(manager, ["not", "an", "object"]))

    assert response.status == 400
    assert json.loads(response.text)["code"] == "body_not_object"
    assert manager.count == 0


@pytest.mark.asyncio
async def test_create_validates_agent_objects_before_mutating(manager):
    response = await _invoke(
        api_channel_create,
        _request(manager, {"topic": "incident", "agents": ["not-an-agent-object"]}),
    )

    assert response.status == 400
    assert json.loads(response.text)["code"] == "channel_agent_type_invalid"
    assert manager.count == 0


@pytest.mark.asyncio
async def test_create_rejects_non_string_topic_before_mutating(manager):
    response = await _invoke(
        api_channel_create,
        _request(manager, {"topic": ["not", "a", "string"]}),
    )

    assert response.status == 400
    assert manager.count == 0


@pytest.mark.asyncio
async def test_create_rejects_invalid_agent_policy_before_mutating(manager):
    response = await _invoke(
        api_channel_create,
        _request(
            manager,
            {
                "topic": "incident",
                "agents": [{"role": "Responder", "approval": "invalid-policy"}],
            },
        ),
    )

    assert response.status == 400
    assert manager.count == 0


@pytest.mark.asyncio
async def test_shared_channel_body_rejects_non_object_json(manager):
    channel = manager.create("incident")
    response = await _invoke(
        api_channel_post,
        _request(manager, None, id=channel.id),
    )

    assert response.status == 400


@pytest.mark.asyncio
async def test_approval_rejects_non_object_json(manager):
    channel = manager.create("incident")
    agent = channel.add_agent(role="Orchestrator", is_orchestrator=True)
    response = await _invoke(
        api_channel_approve_agent,
        _request(manager, "approved", id=channel.id, aid=agent.id),
    )

    assert response.status == 400


@pytest.mark.asyncio
async def test_clear_context_rejects_non_object_json(manager):
    channel = manager.create("incident")
    response = await _invoke(
        api_channel_clear_context,
        _request(manager, 42, id=channel.id),
    )

    assert response.status == 400
