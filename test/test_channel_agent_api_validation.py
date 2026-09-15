"""Field-level input contracts for Channels agent mutations."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers_channel as handlers
from kiro_crew.channel import ApprovalPolicy, ChannelManager, ListenMode


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


def _body(response: web.StreamResponse) -> dict:
    return json.loads(response.text)


@pytest.fixture
def manager(tmp_path) -> ChannelManager:
    return ChannelManager(channels_dir=str(tmp_path / "channels"))


@pytest.fixture(autouse=True)
def spawn_spy(monkeypatch) -> MagicMock:
    spy = MagicMock()
    monkeypatch.setattr(handlers, "_spawn_agent_task", spy)
    return spy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"role": 7}, "channel_agent_role_type_invalid"),
        ({"agent": []}, "channel_agent_name_type_invalid"),
        ({"task": 3}, "channel_agent_task_type_invalid"),
        ({"is_orchestrator": "yes"}, "channel_agent_orchestrator_type_invalid"),
        ({"approval": "unknown"}, "channel_agent_approval_invalid"),
    ],
)
async def test_add_agent_rejects_invalid_fields_before_mutation(manager, spawn_spy, payload, code):
    channel = manager.create("incident")

    response = await _invoke(
        handlers.api_channel_add_agent,
        _request(manager, payload, id=channel.id),
    )

    assert response.status == 400
    assert _body(response)["code"] == code
    assert channel.members == {}
    spawn_spy.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({"approval": "unknown"}, "channel_agent_approval_invalid"),
        (
            {"approval": "trusted", "listen": ["all"]},
            "channel_agent_listen_invalid",
        ),
    ],
)
async def test_update_agent_rejects_invalid_enums_without_saving(
    manager, monkeypatch, payload, code
):
    channel = manager.create("incident")
    agent = channel.add_agent(role="Orchestrator", is_orchestrator=True)
    save_spy = MagicMock()
    monkeypatch.setattr(channel, "_save", save_spy)

    response = await _invoke(
        handlers.api_channel_update_agent,
        _request(manager, payload, id=channel.id, aid=agent.id),
    )

    assert response.status == 400
    assert _body(response)["code"] == code
    assert agent.approval_policy is ApprovalPolicy.WRITES
    assert agent.listen_mode is ListenMode.ALL
    save_spy.assert_not_called()


@pytest.mark.asyncio
async def test_update_agent_still_applies_valid_enum_values(manager, monkeypatch):
    channel = manager.create("incident")
    agent = channel.add_agent(role="Worker")
    save_spy = MagicMock()
    monkeypatch.setattr(channel, "_save", save_spy)

    response = await _invoke(
        handlers.api_channel_update_agent,
        _request(
            manager,
            {"approval": "trusted", "listen": "silent"},
            id=channel.id,
            aid=agent.id,
        ),
    )

    assert response.status == 200
    assert agent.approval_policy is ApprovalPolicy.TRUSTED
    assert agent.listen_mode is ListenMode.SILENT
    save_spy.assert_called_once_with()
