"""The two reads the Bot editor's Abilities pane waits on answer within a deadline.

``GET /api/agents/{name}/capabilities`` and ``GET .../worker-kit`` read the
Bot's own file off the event loop. On a hung mount that read blocks in the
kernel, and the pane shows its spinner for as long as the GET is pending. Each
GET therefore gives up on its read at a deadline and answers ``503
capabilities_unavailable``, which the pane shows as an error, while the read is
still blocked. The stubbed read below blocks until the test releases it, so the
deadline always expires first however slow the host is.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mate_worker_kit
from kiro_crew.dashboard.handlers import agent_capabilities as capabilities_handler
from kiro_crew.dashboard.handlers import member_worker_kit as worker_kit_handler
from kiro_crew.dashboard.routes.agents import register
from kiro_crew.testing.wait import finished


class _BlockedRead:
    """A read that blocks until released, recording whether it ever returned."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.returned = False

    def __call__(self, *_args: object) -> dict:
        self.release.wait()
        self.returned = True
        return {}


def _app(read: _BlockedRead) -> web.Application:
    @web.middleware
    async def owner(request, handler):
        request["user"], request["app"] = "owner", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=lambda _topic: None)
    app[capabilities_handler._SERVICE] = SimpleNamespace(get=read)
    register(app)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "route, handler_module, body",
    [
        (
            "/api/agents/pixel/capabilities",
            capabilities_handler,
            {"error": "capabilities_unavailable", "code": "capabilities_unavailable"},
        ),
        (
            "/api/agents/pixel/worker-kit",
            worker_kit_handler,
            {"error": "capabilities_unavailable", "code": "capabilities_unavailable", "detail": ""},
        ),
    ],
)
async def test_a_stalled_read_answers_503_while_it_is_still_blocked(
    monkeypatch, route, handler_module, body
):
    read = _BlockedRead()
    monkeypatch.setattr(mate_worker_kit, "view", read)
    monkeypatch.setattr(
        handler_module, "_GET_TIMEOUT_SECS", 0.05
    )  # flake-ok: the read blocks until released, so the deadline always expires first
    async with TestClient(TestServer(_app(read))) as client:
        try:
            response = await finished(client.get(route), f"GET {route} with its read blocked")
            assert response.status == 503
            assert await response.json() == body
            assert not read.returned
        finally:
            # Inside the client: the server waits for its handler on close, and a
            # handler without a deadline waits for this read.
            read.release.set()
