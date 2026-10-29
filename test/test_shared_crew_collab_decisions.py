"""Shared approvals and questions resolve once, name both people, and broadcast.

The registry tests use an explicit clock. The handler tests drive the real
``POST /api/chat/slots/{slot}/approve`` through an aiohttp ``TestClient`` with two
distinct verified principals published the way the auth middleware publishes
them (``request[ACTOR_REQUEST_KEY]``), and race them with explicit
synchronisation, never a sleep.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.chat_handlers import api_chat_slot_approve
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.shared_crew.collab import (
    FRAME_DECISION,
    DecisionKind,
    DecisionState,
    ResolveOutcome,
)
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
from kiro_crew.shared_crew.decisions import (
    DecisionRegistry,
    approval_decision_id,
    decisions_for,
    question_decision_id,
    read_response_id,
    state_approval_decision_id,
    turn_initiator,
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


# -- Registry --------------------------------------------------------------


def _registry() -> DecisionRegistry:
    ticks = iter(range(100, 10_000))
    return DecisionRegistry(clock=lambda: float(next(ticks)))


def test_first_response_wins_once_and_names_both_people():
    reg = _registry()
    reg.open("d1", DecisionKind.APPROVAL, "s1", ALICE)
    applied: list[str] = []

    won = reg.resolve(
        "d1",
        response_id="r-bob",
        responder=BOB,
        outcome="approved",
        apply=lambda: applied.append("bob") or True,
    )
    lost = reg.resolve(
        "d1",
        response_id="r-carol",
        responder=CAROL,
        outcome="rejected",
        apply=lambda: applied.append("carol") or True,
    )

    assert won.outcome is ResolveOutcome.WON
    assert lost.outcome is ResolveOutcome.ALREADY_RESOLVED
    assert applied == ["bob"]
    final = reg.get("d1")
    assert final.state is DecisionState.RESOLVED
    assert final.initiator == ALICE and final.responder == BOB
    assert final.initiator != final.responder
    assert final.outcome == "approved"
    assert lost.decision == final


def test_retry_with_the_winning_response_id_replays_without_a_second_effect():
    reg = _registry()
    reg.open("d1", DecisionKind.APPROVAL, "s1", ALICE)
    effects: list[int] = []
    reg.resolve(
        "d1",
        response_id="r1",
        responder=BOB,
        outcome="approved",
        apply=lambda: effects.append(1) or True,
    )
    again = reg.resolve(
        "d1",
        response_id="r1",
        responder=BOB,
        outcome="approved",
        apply=lambda: effects.append(2) or True,
    )
    assert again.outcome is ResolveOutcome.REPLAYED
    assert effects == [1]
    assert again.decision.revision == reg.get("d1").revision


def test_failed_side_effect_leaves_the_decision_pending():
    reg = _registry()
    reg.open("d1", DecisionKind.APPROVAL, "s1", ALICE)
    result = reg.resolve(
        "d1", response_id="r1", responder=BOB, outcome="approved", apply=lambda: False
    )
    assert result.outcome is ResolveOutcome.NOT_PENDING
    assert reg.get("d1").state is DecisionState.PENDING


def test_unanswered_decision_closes_and_late_answers_are_not_pending():
    reg = _registry()
    reg.open("d1", DecisionKind.QUESTION, "s1", ALICE)
    closed = reg.close("d1", DecisionState.EXPIRED)
    assert closed.state is DecisionState.EXPIRED and closed.responder is None
    late = reg.resolve("d1", response_id="r", responder=BOB, outcome="answered", apply=lambda: True)
    assert late.outcome is ResolveOutcome.NOT_PENDING


def test_recurring_id_opens_a_new_revision_after_settling():
    reg = _registry()
    first = reg.open("d1", DecisionKind.APPROVAL, "s1", ALICE)
    assert reg.open("d1", DecisionKind.APPROVAL, "s1", BOB) is first
    reg.resolve("d1", response_id="r", responder=BOB, outcome="approved", apply=lambda: True)
    second = reg.open("d1", DecisionKind.APPROVAL, "s1", BOB)
    assert second.state is DecisionState.PENDING
    assert second.revision > first.revision
    assert second.initiator == BOB


def test_retention_is_bounded_and_settled_decisions_go_first():
    reg = DecisionRegistry(max_retained=2)
    reg.open("old", DecisionKind.APPROVAL, "s", ALICE)
    reg.resolve("old", response_id="", responder=BOB, outcome="approved", apply=lambda: True)
    reg.open("p1", DecisionKind.APPROVAL, "s", ALICE)
    reg.open("p2", DecisionKind.APPROVAL, "s", ALICE)
    assert reg.get("old") is None
    assert reg.get("p1") is not None and reg.get("p2") is not None


def test_ids_are_stable_and_unambiguous():
    assert approval_decision_id("dashboard:x", "7") == approval_decision_id("dashboard:x", "7")
    assert approval_decision_id("dashboard:x", "7") != approval_decision_id("dashboard:y", "7")
    assert state_approval_decision_id("7") != approval_decision_id("state", "7")
    # Length-prefixed, so a key holding the separator cannot alias another pair.
    assert approval_decision_id("a:b", "c") != approval_decision_id("a", "b:c")
    assert question_decision_id("ask-1") != question_decision_id("ask-2")


def test_response_id_reader():
    assert read_response_id({}) == ("", True)
    assert read_response_id({"response_id": "abc"}) == ("abc", True)
    assert read_response_id({"response_id": ""})[1] is False
    assert read_response_id({"response_id": 7})[1] is False
    assert read_response_id({"response_id": "x" * 500})[1] is False


def test_turn_initiator_reads_the_latest_user_author():
    messages = [
        {"role": "user", "content": "a", "meta": {"author": ALICE.to_dict()}},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c", "meta": {"author": BOB.to_dict()}},
        {"role": "permission", "content": "tool"},
    ]
    assert turn_initiator(messages) == BOB
    assert turn_initiator([{"role": "user", "content": "x"}]).is_unknown
    assert turn_initiator(None).is_unknown


# -- Slot approve handler with two verified principals ----------------------

SLOT = "selected"


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
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


def _slot_with_pending(state, request_id: str, initiator: Principal = ALICE):
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    slot.append("user", "run it", "msg msg-u", meta={"author": initiator.to_dict()})
    fut = asyncio.get_running_loop().create_future()
    row = slot.append("permission", "Tool", f'{{"request_id": "{request_id}"}}')
    slot.register_approval(request_id, fut, row)
    return slot, fut


def _decision_frames(state) -> list[dict]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == FRAME_DECISION]


@pytest.mark.asyncio
async def test_two_collaborators_race_and_exactly_one_wins(state):
    slot, fut = _slot_with_pending(state, "rid-1")
    bob_app, carol_app = _app(state, BOB), _app(state, CAROL)
    async with TestClient(TestServer(bob_app)) as bob, TestClient(TestServer(carol_app)) as carol:
        gate = asyncio.Event()

        async def click(client, response_id, action):
            await gate.wait()
            resp = await client.post(
                f"/api/chat/slots/{SLOT}/approve",
                json={"action": action, "request_id": "rid-1", "response_id": response_id},
            )
            return resp.status, await resp.json()

        tasks = [
            asyncio.create_task(click(bob, "r-bob", "approved")),
            asyncio.create_task(click(carol, "r-carol", "rejected")),
        ]
        gate.set()
        results = await await_until(asyncio.gather(*tasks))

    statuses = sorted(status for status, _ in results)
    assert statuses == [200, 409]
    winner = next(body for status, body in results if status == 200)
    loser = next(body for status, body in results if status == 409)
    assert loser["code"] == "already_resolved"
    assert loser["decision"] == winner["decision"]
    decided = winner["decision"]
    assert decided["state"] == "resolved"
    assert decided["initiator"]["id"] == ALICE.id
    assert decided["responder"]["id"] in (BOB.id, CAROL.id)
    assert decided["responder"]["id"] != decided["initiator"]["id"]
    # The future carries the winner's decision, and only that.
    assert fut.result() == decided["outcome"]
    # Every tab is told the authoritative state once.
    frames = _decision_frames(state)
    assert len(frames) == 1 and frames[0]["decision"] == decided
    # The public shape names no issuer or subject.
    assert "subject" not in decided["responder"] and "issuer" not in decided["initiator"]


@pytest.mark.asyncio
async def test_same_response_id_retry_is_idempotent(state):
    _slot, fut = _slot_with_pending(state, "rid-2")
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        body = {"action": "approved", "request_id": "rid-2", "response_id": "r-1"}
        first = await bob.post(f"/api/chat/slots/{SLOT}/approve", json=body)
        retry = await bob.post(f"/api/chat/slots/{SLOT}/approve", json=body)
        first_body, retry_body = await first.json(), await retry.json()
    assert first.status == 200 and retry.status == 200
    assert retry_body["replayed"] is True
    assert retry_body["decision"] == first_body["decision"]
    assert fut.result() == "approved"
    assert len(_decision_frames(state)) == 1


@pytest.mark.asyncio
async def test_stale_response_after_resolution_gets_conflict_with_current_state(state):
    _slot, _fut = _slot_with_pending(state, "rid-3")
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        await bob.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "rejected", "request_id": "rid-3", "response_id": "r-bob"},
        )
    async with TestClient(TestServer(_app(state, CAROL))) as carol:
        stale = await carol.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "approved", "request_id": "rid-3", "response_id": "r-carol"},
        )
        body = await stale.json()
    assert stale.status == 409
    assert body["code"] == "already_resolved"
    assert body["decision"]["outcome"] == "rejected"
    assert body["decision"]["responder"]["display"]["name"] == "Bob"


@pytest.mark.asyncio
async def test_client_without_response_id_keeps_the_existing_404(state):
    _slot, _fut = _slot_with_pending(state, "rid-4")
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        await bob.post(
            f"/api/chat/slots/{SLOT}/approve", json={"action": "approved", "request_id": "rid-4"}
        )
        late = await bob.post(
            f"/api/chat/slots/{SLOT}/approve", json={"action": "approved", "request_id": "rid-4"}
        )
    assert late.status == 404
    decision = decisions_for(state).get(approval_decision_id(SLOT, "rid-4"))
    assert decision.responder == BOB


@pytest.mark.asyncio
async def test_malformed_response_id_is_refused_before_any_effect(state):
    _slot, fut = _slot_with_pending(state, "rid-5")
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        resp = await bob.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "approved", "request_id": "rid-5", "response_id": 42},
        )
    assert resp.status == 400
    assert not fut.done()


@pytest.mark.asyncio
async def test_existing_policy_checks_still_run_first(state):
    """An app token still cannot yolo: the shared path sits behind the old gates."""
    _slot, fut = _slot_with_pending(state, "rid-6")

    @web.middleware
    async def app_token(request, handler):
        request["app"] = "some-app"
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=BOB, request_id="req", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[app_token])
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "yolo", "request_id": "rid-6", "response_id": "r"},
        )
    assert resp.status in (403, 404)
    assert not fut.done()
    assert decisions_for(state).get(approval_decision_id(SLOT, "rid-6")) is None


@pytest.mark.asyncio
async def test_state_level_approval_records_responder_and_system_initiator(state):
    task = asyncio.get_running_loop().create_task(
        state.request_approval("rid-7", "cron:nightly", "fs_write", slot=SLOT)
    )
    for _ in range(200):
        if "rid-7" in state._pending_approvals:
            break
        await asyncio.sleep(0)
    state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    async with TestClient(TestServer(_app(state, BOB))) as bob:
        resp = await bob.post(
            f"/api/chat/slots/{SLOT}/approve",
            json={"action": "approved", "request_id": "rid-7", "response_id": "r1"},
        )
        body = await resp.json()
    assert resp.status == 200
    assert await await_until(task) is True
    decided = decisions_for(state).get(state_approval_decision_id("rid-7"))
    assert decided.state is DecisionState.RESOLVED
    assert decided.responder == BOB
    assert decided.initiator.kind is PrincipalKind.SYSTEM
    assert body["decision"]["responder"]["id"] == BOB.id
    # Pending, then resolved: the wait's own close did not overwrite the responder.
    states = [f["decision"]["state"] for f in _decision_frames(state)]
    assert states == ["pending", "resolved"]


@pytest.mark.asyncio
async def test_state_level_approval_that_expires_closes_the_decision(state):
    state._BACKGROUND_APPROVAL_TIMEOUT_SECS = 0.01
    state._APPROVAL_TIMEOUT = 0.01
    assert await state.request_approval("rid-8", "subagent", "fs_write", slot=SLOT) is False
    closed = decisions_for(state).get(state_approval_decision_id("rid-8"))
    assert closed.state is DecisionState.EXPIRED and closed.responder is None


# -- Questions --------------------------------------------------------------


@pytest.mark.asyncio
async def test_question_first_answer_wins_and_names_responder(state):
    slot = state.get_or_create_slot(SLOT, origin=SlotOrigin.USER)
    slot.append("user", "ask me", "msg msg-u", meta={"author": ALICE.to_dict()})
    from kiro_crew.dashboard.interaction_coordinator import QuestionCoordinator

    task = asyncio.get_running_loop().create_task(
        QuestionCoordinator().request(state, "ask-1", slot.key, [{"question": "Q?"}], 30)
    )
    for _ in range(200):
        if "ask-1" in state._question_futures:
            break
        await asyncio.sleep(0)

    won = QuestionCoordinator.resolve(
        state, "ask-1", {"Q?": "yes"}, responder=BOB, response_id="r-bob"
    )
    lost = QuestionCoordinator.resolve(
        state, "ask-1", {"Q?": "no"}, responder=CAROL, response_id="r-carol"
    )
    answers = await await_until(task)

    assert won is True and lost is False
    assert answers == {"Q?": "yes"}
    decided = decisions_for(state).get(question_decision_id("ask-1"))
    assert decided.state is DecisionState.RESOLVED
    assert decided.initiator == ALICE and decided.responder == BOB
    late = decisions_for(state).classify(question_decision_id("ask-1"), "r-carol")
    assert late.outcome is ResolveOutcome.ALREADY_RESOLVED
    assert decisions_for(state).classify(question_decision_id("ask-1"), "r-bob").outcome is (
        ResolveOutcome.REPLAYED
    )


# -- By-id resolver the approvals route hands off to -------------------------


def _request_as(principal: Principal) -> dict:
    return {
        ACTOR_REQUEST_KEY: ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
    }


@pytest.mark.asyncio
async def test_by_id_slot_approval_first_wins_then_conflict_then_replay(state):
    from kiro_crew.shared_crew.decisions import resolve_approval_by_id

    _slot, fut = _slot_with_pending(state, "rid-20")
    won = resolve_approval_by_id(
        state, _request_as(BOB), "rid-20", approved=True, rejected_once=False, response_id="r-bob"
    )
    lost = resolve_approval_by_id(
        state,
        _request_as(CAROL),
        "rid-20",
        approved=False,
        rejected_once=False,
        response_id="r-carol",
    )
    replay = resolve_approval_by_id(
        state, _request_as(BOB), "rid-20", approved=True, rejected_once=False, response_id="r-bob"
    )
    legacy = resolve_approval_by_id(
        state, _request_as(CAROL), "rid-20", approved=False, rejected_once=False, response_id=""
    )
    assert won[0] == 200 and fut.result() == "approved"
    assert lost[0] == 409 and lost[1]["decision"]["responder"]["id"] == BOB.id
    assert replay[0] == 200 and replay[1]["replayed"] is True
    assert legacy is None
    assert won[1]["decision"]["initiator"]["id"] == ALICE.id


@pytest.mark.asyncio
async def test_by_id_with_nothing_pending_returns_none(state):
    from kiro_crew.shared_crew.decisions import resolve_approval_by_id

    assert (
        resolve_approval_by_id(
            state, _request_as(BOB), "nope", approved=True, rejected_once=False, response_id="r"
        )
        is None
    )
