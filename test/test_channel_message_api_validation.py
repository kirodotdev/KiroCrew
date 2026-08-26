"""Field-level input contract for Channels message posts."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

from kiro_crew.channel import ChannelManager
from kiro_crew.dashboard.handlers_channel import api_channel_post


def _request(manager: ChannelManager, body: dict, channel_id: str) -> MagicMock:
    request = MagicMock()
    request.app = {"state": SimpleNamespace(channel_manager=manager)}
    request.match_info = {"id": channel_id}
    request.json = AsyncMock(return_value=body)
    return request


async def _invoke(request) -> web.StreamResponse:
    try:
        return await api_channel_post(request)
    except web.HTTPException as exc:
        return exc


def _body(response: web.StreamResponse) -> dict:
    return json.loads(response.text)


@pytest.fixture
def manager(tmp_path) -> ChannelManager:
    return ChannelManager(channels_dir=str(tmp_path / "channels"))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, [], {}, 7, True])
async def test_post_rejects_non_string_content_before_mutation(manager, monkeypatch, content):
    channel = manager.create("incident")
    post_spy = AsyncMock()
    monkeypatch.setattr(channel, "post", post_spy)

    response = await _invoke(_request(manager, {"content": content}, channel.id))

    assert response.status == 400
    assert _body(response)["code"] == "channel_message_content_type_invalid"
    assert channel.messages == []
    post_spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_keeps_string_trim_and_length_limit(manager):
    channel = manager.create("incident")

    response = await _invoke(_request(manager, {"content": f"  {'a' * 10001}  "}, channel.id))

    assert response.status == 200
    assert _body(response)["message"]["content"] == "a" * 10000
    assert channel.messages[-1].content == "a" * 10000
