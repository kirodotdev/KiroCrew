"""Shared fixtures for tests that drive the guide HTTP routes in-process.

A dedicated ``*_helpers.py`` imported by bare name, per the repo convention (see
``mcp_merge_helpers``): no ``test_*`` module imports another, and a
``from test.test_guide_routes import ...`` form does not resolve under CI's
rootdir -- ``test`` is not an importable package there.

The auth layer is replaced by a tiny middleware that sets exactly the request
attributes the real ``token_auth_middleware`` sets (``internal_auth`` / ``user``
/ ``app``), so every refusal a caller sees is the guide handler's OWN check.
"""

from __future__ import annotations

import asyncio
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.handlers import guide as guide_routes
from kiro_crew.dashboard.state import _ChatSlot

CREWMATE = [{"id": "crewmate.create", "params": {"name": "Scout"}}]


class DashboardTurn:
    """Stands in for the task of a turn the user sent from the dashboard.

    The guide and card routes admit a tool call only from the turn its slot is
    executing (``_ChatSlot.turn_running``), so a slot a test opens carries one.
    """

    def done(self) -> bool:
        return False

    def cancel(self) -> bool:
        return False


def in_dashboard_turn(slot: _ChatSlot) -> _ChatSlot:
    """Mark *slot* as running a turn the user sent from the dashboard."""
    slot.task = DashboardTurn()  # type: ignore[assignment]
    slot._turn_user_sent = True
    return slot


class FakeState:
    """The slice of ``DashboardState`` the guide routes read."""

    owner_id = ""

    def __init__(self) -> None:
        self._slots: dict[str, _ChatSlot] = {}
        self.frames: list[tuple[str, dict[str, Any]]] = []

    def open_slot(self, key: str) -> _ChatSlot:
        slot = in_dashboard_turn(_ChatSlot(key))
        self._slots[key] = slot
        return slot

    def get_slot(self, name: str) -> _ChatSlot | None:
        return self._slots.get(name)

    async def deliver_ws_owners(self, kind: str, payload: dict[str, Any]) -> int:
        self.frames.append((kind, payload))
        return 1


@web.middleware
async def _fake_auth(request: web.Request, handler):
    who = request.headers.get("X-Test-Auth", "")
    if who == "internal":
        request["internal_auth"] = True
        request["app"] = ""
    elif who == "internal-app":
        request["internal_auth"] = True
        request["app"] = "some-app"
    elif who == "owner":
        request["user"] = "local-app"
        request["app"] = ""
    elif who == "app":
        request["user"] = "local-app"
        request["app"] = "some-app"
    return await handler(request)


def run_guide_app(coro_fn, state: FakeState, extra_routes=()):
    """Serve the guide routes on an in-process app and run *coro_fn* against it."""

    async def _main():
        app = web.Application(middlewares=[_fake_auth])
        app["state"] = state
        guide_routes.register_guide_routes(app)
        for method, path, handler in extra_routes:
            app.router.add_route(method, path, handler)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            return await coro_fn(client)
        finally:
            await client.close()

    return asyncio.run(_main())


def agent(sk: str, auth: str = "internal") -> dict[str, str]:
    """Headers for an agent-side call from session *sk*."""
    return {"X-Test-Auth": auth, "X-Session-Key": sk}
