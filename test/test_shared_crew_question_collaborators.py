"""Every Shared Crew collaborator sees an agent question and may answer it.

Drives the real ``/api/ask-question`` read, answer and dismiss handlers with a
verified actor published the way the auth middleware publishes it, and a token
subject that is NOT the owner's. The first valid answer wins through the shared
decision registry, which records the initiator and the responder.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard.handlers.ask_question import (
    api_ask_question_answer,
    api_ask_question_dismiss,
    api_ask_question_pending,
)
from kiro_crew.shared_crew import collaborators
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
from kiro_crew.shared_crew.decisions import decisions_for, question_decision_id
from kiro_crew.testing.wait import async_wait_until


def _person(login: str, provenance: Provenance = Provenance.TAILSCALE_SERVE) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=provenance,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")
DIRECT = _person("dave@example.test", Provenance.TAILSCALE_DIRECT)
#: A member the direct tailnet listener verified and admitted (the default path).
LISTENER = _person("lee@example.test", Provenance.TAILSCALE_LISTENER)
LOCAL = Principal(
    id=principal_id_for("local", "dashboard", "slack-user"),
    kind=PrincipalKind.HUMAN,
    provider="local",
    issuer="dashboard",
    subject="slack-user",
    provenance=Provenance.LOCAL_OPERATOR,
    display=DisplaySnapshot(name="slack-user", login="slack-user"),
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.deliver_ws_owners = AsyncMock(return_value=1)
    st.push_slots_update = MagicMock()
    st.owner_id = "owner"
    return st


def _client(state, principal: Principal, *, subject: str = "bob") -> TestClient:
    @web.middleware
    async def mw(request, handler):
        request["app"] = ""
        request["user"] = subject
        request["is_dashboard_user"] = True
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=principal, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[mw])
    app["state"] = state
    app.router.add_get("/api/ask-question/pending", api_ask_question_pending)
    app.router.add_post("/api/ask-question/dismiss", api_ask_question_dismiss)
    app.router.add_post("/api/ask-question/{ask_id}/answer", api_ask_question_answer)
    return TestClient(TestServer(app))


async def _open_question(state, ask_id: str = "q1") -> asyncio.Task:
    slot = state.get_or_create_slot("s1")
    slot.append("user", "alice asks", meta={"author": ALICE.to_public_dict()})
    task = asyncio.create_task(
        state.request_question(
            ask_id=ask_id, slot_key="s1", questions=[{"question": "Ship it?"}], timeout=30
        )
    )
    await async_wait_until(lambda: ask_id in state._pending_questions)
    return task


@pytest.mark.asyncio
async def test_a_collaborator_sees_and_answers_and_the_first_answer_wins(state):
    task = await _open_question(state)
    async with _client(state, BOB) as bob, _client(state, ALICE, subject="alice") as alice:
        resp = await bob.get("/api/ask-question/pending")
        assert resp.status == 200
        assert [p["ask_id"] for p in (await resp.json())["pending"]] == ["q1"]

        won = await bob.post(
            "/api/ask-question/q1/answer",
            json={"answers": {"Ship it?": "yes"}, "response_id": "bob-click"},
        )
        assert won.status == 200
        lost = await alice.post(
            "/api/ask-question/q1/answer",
            json={"answers": {"Ship it?": "no"}, "response_id": "alice-click"},
        )
        assert lost.status == 409
        assert (await lost.json())["code"] == "already_resolved"

    assert await task == {"Ship it?": "yes"}
    decision = decisions_for(state).get(question_decision_id("q1"))
    assert decision is not None
    assert decision.responder is not None and decision.responder.id == BOB.id
    assert decision.initiator.id == ALICE.id


@pytest.mark.asyncio
async def test_a_direct_listener_member_sees_and_answers(state):
    task = await _open_question(state)
    async with _client(state, LISTENER, subject="lee") as lee:
        resp = await lee.get("/api/ask-question/pending")
        assert resp.status == 200
        assert [p["ask_id"] for p in (await resp.json())["pending"]] == ["q1"]
        won = await lee.post(
            "/api/ask-question/q1/answer",
            json={"answers": {"Ship it?": "yes"}, "response_id": "lee-click"},
        )
        assert won.status == 200
    assert await task == {"Ship it?": "yes"}


@pytest.mark.asyncio
async def test_the_losing_answer_is_audited_as_a_conflict(state, monkeypatch):
    from kiro_crew.shared_crew import audit

    events: list[Any] = []
    monkeypatch.setattr(audit, "_default_writer", events.append)
    task = await _open_question(state, "q-race")
    async with _client(state, BOB) as bob, _client(state, ALICE, subject="alice") as alice:
        won = await bob.post(
            "/api/ask-question/q-race/answer",
            json={"answers": {"Ship it?": "yes"}, "response_id": "bob-click"},
        )
        assert won.status == 200
        replay = await bob.post(
            "/api/ask-question/q-race/answer",
            json={"answers": {"Ship it?": "yes"}, "response_id": "bob-click"},
        )
        assert replay.status == 200
        lost = await alice.post(
            "/api/ask-question/q-race/answer",
            json={"answers": {"Ship it?": "no"}, "response_id": "alice-click"},
        )
        assert lost.status == 409
    await task
    while audit._pending_audits:
        await asyncio.gather(*list(audit._pending_audits))
    rows = [(e.operation, e.outcome, e.metadata["responder"]["id"], e.resources) for e in events]
    target = question_decision_id("q-race")
    # The replay of the winner adds nothing; the different losing answer is recorded.
    assert rows == [
        ("shared_crew.question_answer", "applied", BOB.id, target),
        ("shared_crew.question_answer", "conflict", ALICE.id, target),
    ]
    assert all(e.caller_identity == ALICE.id for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "principal", [LOCAL, DIRECT, Principal.unknown()], ids=lambda p: p.provenance.value
)
async def test_a_non_member_token_is_still_refused(state, principal):
    task = await _open_question(state)
    try:
        async with _client(state, principal, subject="slack-user") as other:
            resp = await other.get("/api/ask-question/pending")
            assert resp.status == 403
            resp = await other.post(
                "/api/ask-question/q1/answer", json={"answers": {"Ship it?": "yes"}}
            )
            assert resp.status == 403
            resp = await other.post(
                "/api/ask-question/dismiss", json={"slot": "s1", "card_id": "c1"}
            )
            assert resp.status == 403
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_an_app_token_is_still_refused(state):
    @web.middleware
    async def mw(request, handler):
        request["app"] = "notes"
        request["user"] = "owner"
        request[ACTOR_REQUEST_KEY] = ActorContext(
            principal=BOB, request_id="r", transport=Transport.HTTP
        )
        return await handler(request)

    app = web.Application(middlewares=[mw])
    app["state"] = state
    app.router.add_get("/api/ask-question/pending", api_ask_question_pending)
    async with TestClient(TestServer(app)) as client:
        assert (await client.get("/api/ask-question/pending")).status == 403


@pytest.mark.asyncio
async def test_a_collaborator_may_dismiss_a_stateless_card(state):
    state.get_or_create_slot("s1")
    state.mark_question_pending(
        "s1", blocking=False, card_id="c1", questions=[{"question": "Ship it?"}]
    )
    async with _client(state, BOB) as bob:
        resp = await bob.post("/api/ask-question/dismiss", json={"slot": "s1", "card_id": "c1"})
        assert resp.status == 200


class _Ws(dict):
    """A socket double: item storage like ``WebSocketResponse``, hashed by identity."""

    closed = False
    __hash__ = object.__hash__  # type: ignore[assignment]


def _sockets(state) -> dict[str, Any]:
    owner, bob, slack, app = _Ws(), _Ws(), _Ws(), _Ws()
    owner.update(_is_dashboard_user=True)
    bob.update(
        _is_dashboard_user=True,
        _shared_crew_actor=ActorContext(
            principal=BOB, request_id="r", transport=Transport.WEBSOCKET
        ),
    )
    slack.update(
        _is_dashboard_user=True,
        _shared_crew_actor=ActorContext(
            principal=LOCAL, request_id="r", transport=Transport.WEBSOCKET
        ),
    )
    app.update(
        _is_dashboard_user=False,
        _app="notes",
        _shared_crew_actor=ActorContext(
            principal=BOB, request_id="r", transport=Transport.WEBSOCKET
        ),
    )
    state._ws_clients[:] = [owner, bob, slack, app]
    state._owner_ws_clients.clear()
    state._owner_ws_clients.add(owner)
    return {"owner": owner, "bob": bob, "slack": slack, "app": app}


@pytest.mark.asyncio
async def test_question_frames_reach_collaborator_sockets_only(state, monkeypatch):
    socks = _sockets(state)
    sent: list[tuple[str, str]] = []
    names = {id(ws): name for name, ws in socks.items()}
    monkeypatch.setattr(state, "_spawn_ws_send", lambda ws, msg: sent.append((names[id(ws)], msg)))

    task = await _open_question(state)
    await async_wait_until(lambda: any('"question_card"' in m for _n, m in sent))
    state.resolve_question("q1", {"Ship it?": "yes"}, responder=BOB, response_id="r1")
    await task
    await async_wait_until(lambda: any('"question_card_resolved"' in m for _n, m in sent))

    assert {n for n, _m in sent} == {"bob"}
    kinds = {m.split('"type": "', 1)[1].split('"', 1)[0] for _n, m in sent}
    assert {"question_card", "question_card_resolved", "shared_crew.decision"} <= kinds
    # The owner set still receives every frame through its own channel.
    owner_types = [c.args[0] for c in state.broadcast_ws_owners.call_args_list]
    assert {"question_card", "question_card_resolved"} <= set(owner_types)


def _ctx(p: Principal) -> ActorContext:
    return ActorContext(principal=p, request_id="r", transport=Transport.HTTP)


def test_only_membership_provenances_are_collaborators():
    ctx = _ctx
    assert collaborators.is_collaborator_actor(ctx(BOB))
    assert collaborators.is_collaborator_actor(ctx(LISTENER))
    for p in (DIRECT, LOCAL, Principal.unknown()):
        assert not collaborators.is_collaborator_actor(ctx(p))
    assert not collaborators.is_collaborator_actor(None)
