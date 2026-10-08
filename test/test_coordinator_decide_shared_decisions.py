"""A targeted coordinator decide settles through the Shared Crew decision registry.

The dashboard decides a coordinator approval by its slot and instance
(``?origin=coordinator``). That route must record the verified responder the way
the bare-id route does: the first click wins and names its person, a different
collaborator's later click gets 409 with the winner, and a retry of the winning
click replays it. A card for a request that another one replaced under the same
id gets a plain 404 and never adopts the live request's outcome.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers import api_approval_resolve
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.shared_crew.collab import FRAME_DECISION
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
from kiro_crew.testing.wait import async_wait_until

SLOT = "selected"


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
    st.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    return st


def _as(principal: Principal):
    @web.middleware
    async def middleware(request: web.Request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="req", transport=Transport.HTTP
        )
        return await handler(request)

    return middleware


def _app(state, principal: Principal) -> web.Application:
    app = web.Application(middlewares=[_as(principal)])
    app["state"] = state
    app.router.add_post("/api/approvals/{id}/{action}", api_approval_resolve)
    return app


async def _register(state, approval_id: str) -> tuple[asyncio.Task, str]:
    task = asyncio.get_running_loop().create_task(
        state.request_approval(approval_id, "subagent", "spawn_run(t)", slot=SLOT)
    )
    await async_wait_until(
        lambda: approval_id in state._pending_approvals,
        timeout=30.0,
        describe=lambda: f"approval {approval_id!r} never registered",
    )
    return task, state._pending_approvals[approval_id]["instance"]


def _url(approval_id: str, action: str, instance: str) -> str:
    return (
        f"/api/approvals/{approval_id}/{action}?origin=coordinator&slot={SLOT}&instance={instance}"
    )


def _resolved_frames(state) -> list[dict]:
    """Decision frames that settle a decision (registration also opens one)."""
    return [
        c.args[1]
        for c in state.broadcast_ws.call_args_list
        if c.args[0] == FRAME_DECISION and c.args[1]["decision"]["state"] == "resolved"
    ]


@pytest.mark.asyncio
async def test_a_targeted_decide_names_its_responder_and_a_second_click_gets_the_winner(state):
    task, instance = await _register(state, "spawn:a1")
    async with (
        TestClient(TestServer(_app(state, BOB))) as bob,
        TestClient(TestServer(_app(state, CAROL))) as carol,
    ):
        first = await bob.post(_url("spawn:a1", "approve", instance), json={"response_id": "r-bob"})
        first_body = await first.json()
        retry = await bob.post(_url("spawn:a1", "approve", instance), json={"response_id": "r-bob"})
        retry_body = await retry.json()
        late = await carol.post(
            _url("spawn:a1", "reject", instance), json={"response_id": "r-carol"}
        )
        late_body = await late.json()
    assert first.status == 200
    assert first_body["decision"]["responder"]["id"] == BOB.id
    assert retry.status == 200 and retry_body["replayed"] is True
    assert late.status == 409 and late_body["code"] == "already_resolved"
    assert late_body["decision"] == first_body["decision"]
    assert await asyncio.wait_for(task, 30.0) is True
    frames = _resolved_frames(state)
    assert len(frames) == 1 and frames[0]["decision"] == first_body["decision"]


@pytest.mark.asyncio
async def test_a_replaced_request_s_card_gets_404_not_the_live_request_s_outcome(state):
    older, old_instance = await _register(state, "spawn:a2")
    replacement = asyncio.get_running_loop().create_task(
        state.request_approval("spawn:a2", "subagent", "spawn_run(t)", slot=SLOT)
    )
    await async_wait_until(
        lambda: state._pending_approvals["spawn:a2"]["instance"] != old_instance,
        timeout=30.0,
        describe=lambda: "replacement never registered",
    )
    new_instance = state._pending_approvals["spawn:a2"]["instance"]
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        stale = await bob.post(
            _url("spawn:a2", "approve", old_instance), json={"response_id": "r-stale"}
        )
    assert stale.status == 404
    assert _resolved_frames(state) == []
    assert state._pending_approvals["spawn:a2"]["instance"] == new_instance
    older.cancel()
    replacement.cancel()
    await asyncio.gather(older, replacement, return_exceptions=True)
