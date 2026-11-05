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
from kiro_crew.testing.wait import async_wait_until, await_until

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


@pytest.mark.asyncio
async def test_a_late_click_never_adopts_a_later_request_s_decision_under_the_same_id(state):
    """A is approved and ends; B reuses the id and is rejected and ends. A's
    delayed second click names A's instance: the settled decision now under the
    id is B's, so the click gets 404, not B's rejection as A's outcome. A late
    click naming B's own instance still gets B's decision."""
    first, a_instance = await _register(state, "spawn:a3")
    async with (
        TestClient(TestServer(_app(state, BOB))) as bob,
        TestClient(TestServer(_app(state, CAROL))) as carol,
    ):
        won = await bob.post(_url("spawn:a3", "approve", a_instance), json={"response_id": "r-a"})
        assert won.status == 200
        assert await asyncio.wait_for(first, 30.0) is True
        second, b_instance = await _register(state, "spawn:a3")
        rejected = await bob.post(
            _url("spawn:a3", "reject", b_instance), json={"response_id": "r-b"}
        )
        assert rejected.status == 200
        assert await asyncio.wait_for(second, 30.0) is False
        stale = await carol.post(
            _url("spawn:a3", "approve", a_instance), json={"response_id": "r-late-a"}
        )
        late_b = await carol.post(
            _url("spawn:a3", "approve", b_instance), json={"response_id": "r-late-b"}
        )
        late_b_body = await late_b.json()
    assert stale.status == 404
    assert late_b.status == 409 and late_b_body["decision"]["outcome"] == "rejected"


def _actor_request(principal: Principal) -> dict:
    return {
        ACTOR_REQUEST_KEY: ActorContext(
            principal=principal, request_id="req", transport=Transport.HTTP
        )
    }


@pytest.mark.asyncio
async def test_a_second_click_before_cleanup_keeps_the_winners_decision(state):
    """Two clicks reach the instance before the coordinator wakes to clean up.

    The second must not reopen the settled decision: it gets 409 with the
    winner, and the winner's retry still replays success.
    """
    from kiro_crew.shared_crew import decisions as shared_decisions

    task, _instance = await _register(state, "spawn:a3")
    # No await between these calls, so the coordinator cannot clean up.
    first = shared_decisions.resolve_state_approval(
        state, _actor_request(BOB), "spawn:a3", True, "approved", "r-bob"
    )
    late = shared_decisions.resolve_state_approval(
        state, _actor_request(CAROL), "spawn:a3", False, "rejected", "r-carol"
    )
    retry = shared_decisions.resolve_state_approval(
        state, _actor_request(BOB), "spawn:a3", True, "approved", "r-bob"
    )
    assert "spawn:a3" in state._pending_approvals
    assert first is not None and first[0] == 200
    assert late is not None and late[0] == 409
    assert late[1]["decision"]["responder"]["id"] == BOB.id
    assert retry is not None and retry[0] == 200 and retry[1].get("replayed") is True
    record = shared_decisions.decisions_for(state).get(
        shared_decisions.state_approval_decision_id("spawn:a3")
    )
    assert record is not None and record.responder is not None and record.responder.id == BOB.id
    assert await await_until(task, what="the approval Bob won") is True


@pytest.mark.asyncio
async def test_a_late_coordinator_click_never_adopts_a_native_decision_under_the_same_id(state):
    """A native request in the same slot holds the coordinator's id and was
    approved; the coordinator request was rejected and ended. A late click
    naming the coordinator's instance gets the coordinator's rejection, not
    the native approval."""
    from kiro_crew.shared_crew import decisions as shared_decisions

    task, instance = await _register(state, "shared-id")
    async with (
        TestClient(TestServer(_app(state, BOB))) as bob,
        TestClient(TestServer(_app(state, CAROL))) as carol,
    ):
        rejected = await bob.post(
            _url("shared-id", "reject", instance), json={"response_id": "r-b"}
        )
        assert rejected.status == 200
        assert await await_until(task, what="the rejected coordinator approval") is False
        registry = shared_decisions.decisions_for(state)
        native_id = shared_decisions.approval_decision_id(SLOT, "shared-id")
        registry.open(native_id, shared_decisions.DecisionKind.APPROVAL, SLOT, BOB)
        registry.resolve(
            native_id, response_id="r-native", responder=BOB, outcome="approved", apply=lambda: True
        )
        late = await carol.post(
            _url("shared-id", "approve", instance), json={"response_id": "r-late"}
        )
        body = await late.json()
    assert late.status == 409
    assert body["decision"]["outcome"] == "rejected"
