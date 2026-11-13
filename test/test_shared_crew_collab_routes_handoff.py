"""The approvals and question routes settle a shared decision once (handoff D-to-F).

Lives with the route edits in ``handoff/D-to-F.patch``: it drives the real
``POST /api/approvals/{id}/{action}`` and ``POST /api/ask-question/{id}/answer``
handlers with two verified principals.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.shared_crew.contracts import (
    ACTOR_REQUEST_KEY,
    ActorContext,
    DisplaySnapshot,
    Principal,
    PrincipalKind,
    Provenance,
    Transport,
    principal_id_for,
)
from kiro_crew.testing.wait import await_until


def _person(login: str, name: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=name, login=login),
    )


ALICE = _person("alice@example.test", "Alice")
BOB = _person("bob@example.test", "Bob")
CAROL = _person("carol@example.test", "Carol")


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _app(state, principal):
    @web.middleware
    async def mw(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[mw])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    return app


@pytest.mark.asyncio
async def test_approvals_route_two_collaborators_one_winner(state):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    slot.append("user", "go", "msg msg-u", meta={"author": ALICE.to_dict()})
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", '{"request_id": "rid-1"}')
    slot.register_approval("rid-1", fut, row)
    async with (
        TestClient(TestServer(_app(state, BOB))) as bob,
        TestClient(TestServer(_app(state, CAROL))) as carol,
    ):
        gate = asyncio.Event()

        async def click(client, action, rid):
            await gate.wait()
            resp = await client.post(f"/api/approvals/rid-1/{action}", json={"response_id": rid})
            return resp.status, await resp.json()

        tasks = [
            asyncio.create_task(click(bob, "approve", "r-bob")),
            asyncio.create_task(click(carol, "reject", "r-carol")),
        ]
        gate.set()
        results = await await_until(asyncio.gather(*tasks))
        retry = await bob.post("/api/approvals/rid-1/approve", json={"response_id": "r-bob"})
        retry_status = retry.status
    assert sorted(s for s, _ in results) == [200, 409]
    winner = next(b for s, b in results if s == 200)["decision"]
    assert winner["initiator"]["id"] == ALICE.id
    assert winner["responder"]["id"] in (BOB.id, CAROL.id)
    assert fut.done()
    assert retry_status in (200, 409)


@pytest.mark.asyncio
async def test_approvals_route_without_body_keeps_legacy_behaviour(state):
    slot = state.get_or_create_slot("selected", origin=SlotOrigin.USER)
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", '{"request_id": "rid-2"}')
    slot.register_approval("rid-2", fut, row)
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        first = await bob.post("/api/approvals/rid-2/approve")
        second = await bob.post("/api/approvals/rid-2/approve")
    assert first.status == 200 and fut.result() == "approved"
    assert second.status == 404
