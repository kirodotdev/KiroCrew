"""The collaboration reads a reconnecting tab reconciles against."""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.shared_crew.collab import Activity, DecisionKind
from kiro_crew.shared_crew.contracts import Principal
from kiro_crew.shared_crew.decisions import decisions_for
from kiro_crew.shared_crew.presence import presence_for
from kiro_crew.shared_crew.routes_collab import register


class _State:
    def __init__(self) -> None:
        self._slots = {"s1": object()}


def _app(state, *, dashboard_user: bool) -> web.Application:
    @web.middleware
    async def auth(request, handler):
        request["is_dashboard_user"] = dashboard_user
        return await handler(request)

    app = web.Application(middlewares=[auth])
    app["state"] = state
    register(app)
    return app


@pytest.mark.asyncio
async def test_presence_and_decisions_reads():
    state = _State()
    someone = Principal.system("tester")
    presence_for(state).report("ws:1", someone, "s1", Activity.VIEWING)
    decisions_for(state).open("d1", DecisionKind.APPROVAL, "s1", someone)
    decisions_for(state).open("d2", DecisionKind.APPROVAL, "other", someone)
    async with TestClient(TestServer(_app(state, dashboard_user=True))) as client:
        presence = await (await client.get("/api/shared-crew/presence?session_key=s1")).json()
        decisions = await (await client.get("/api/shared-crew/decisions?session_key=s1")).json()
        missing = await client.get("/api/shared-crew/presence")
    assert presence["type"] == "shared_crew.presence"
    assert [e["principal"]["id"] for e in presence["entries"]] == [someone.id]
    assert [d["decision_id"] for d in decisions["items"]] == ["d1"]
    assert missing.status == 400


@pytest.mark.asyncio
async def test_app_tokens_are_refused():
    async with TestClient(TestServer(_app(_State(), dashboard_user=False))) as client:
        resp = await client.get("/api/shared-crew/presence?session_key=s1")
    assert resp.status == 403
