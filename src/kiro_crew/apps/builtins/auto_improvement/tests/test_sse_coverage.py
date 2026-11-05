"""The auto-improvement SSE fan-out: delivery, dropped clients, the cap and stream cleanup."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps.builtins.auto_improvement.backend import sse
from kiro_crew.testing.wait import finished

#: The subscriber registry typed for the fakes below, which stand in for a StreamResponse.
_registry = cast("set[Any]", sse.sse_clients)


class _Client:
    """A subscriber whose write succeeds, fails, or never completes."""

    def __init__(self, *, fail: bool = False, hang: bool = False) -> None:
        self.writes: list[bytes] = []
        self._fail = fail
        self._hang = hang

    async def write(self, data: bytes) -> None:
        if self._hang:
            await asyncio.Event().wait()
        if self._fail:
            raise ConnectionResetError("client gone")
        self.writes.append(data)


@pytest.fixture(autouse=True)
def no_subscribers():
    sse.sse_clients.clear()
    yield
    sse.sse_clients.clear()


@pytest.mark.asyncio
async def test_broadcast_with_no_subscribers_returns_quietly():
    await sse.broadcast({"kind": "keep"})
    assert sse.sse_clients == set()


@pytest.mark.asyncio
async def test_broadcast_writes_one_frame_to_every_live_subscriber():
    first, second = _Client(), _Client()
    _registry.update({first, second})
    await sse.broadcast({"kind": "keep", "n": 1})
    expected = b'data: {"kind": "keep", "n": 1}\n\n'
    assert first.writes == [expected]
    assert second.writes == [expected]


@pytest.mark.asyncio
async def test_a_failing_subscriber_is_dropped_and_the_rest_still_receive():
    broken, healthy = _Client(fail=True), _Client()
    _registry.update({broken, healthy})
    await sse.broadcast({"kind": "error"})
    assert broken not in sse.sse_clients
    assert healthy in sse.sse_clients
    assert len(healthy.writes) == 1


@pytest.mark.asyncio
async def test_a_stuck_subscriber_is_dropped_after_the_write_deadline(monkeypatch):
    monkeypatch.setattr(sse, "_BROADCAST_TIMEOUT_S", 0.01)
    stuck, healthy = _Client(hang=True), _Client()
    _registry.update({stuck, healthy})
    await finished(sse.broadcast({"kind": "pr"}), "the broadcast")
    assert stuck not in sse.sse_clients
    assert healthy.writes


@pytest.mark.asyncio
async def test_an_unserializable_payload_is_dropped_without_touching_subscribers():
    circular: dict[str, Any] = {}
    circular["self"] = circular
    client = _Client()
    _registry.add(client)
    await sse.broadcast(circular)
    assert client.writes == []
    assert client in sse.sse_clients


@pytest.mark.asyncio
async def test_a_full_subscriber_list_refuses_new_streams_with_503():
    _registry.update(_Client() for _ in range(sse._MAX_CLIENTS))
    request = make_mocked_request("GET", "/events")
    response = cast(web.Response, await sse.stream(request))
    assert response.status == 503
    assert isinstance(response.body, bytes)
    assert json.loads(response.body) == {
        "code": "sse_subscriber_limit",
        "error": "too many event subscribers",
    }
    assert len(sse.sse_clients) == sse._MAX_CLIENTS


@pytest.mark.asyncio
async def test_a_closed_transport_subscribes_then_unsubscribes_at_once():
    request = make_mocked_request("GET", "/events")
    cast(MagicMock, request.transport).is_closing.return_value = True
    response = await sse.stream(request)
    assert response.status == 200
    assert response.headers["Content-Type"] == "text/event-stream"
    assert response not in sse.sse_clients


@pytest.mark.asyncio
async def test_cancelling_an_open_stream_returns_it_and_unsubscribes():
    async def scenario():
        request = make_mocked_request("GET", "/events")
        cast(MagicMock, request.transport).is_closing.return_value = False
        task = asyncio.ensure_future(sse.stream(request))
        for _ in range(100):
            if sse.sse_clients:
                break
            await asyncio.sleep(0)
        subscribed = len(sse.sse_clients)
        task.cancel()
        response = await task
        return subscribed, response

    subscribed, response = await scenario()
    assert subscribed == 1
    assert response.status == 200
    assert sse.sse_clients == set()
