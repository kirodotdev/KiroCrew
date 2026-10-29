"""Shared Crew repair: A slot permission prompt and an agent question open their shared decision when they show."""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kiro_crew.shared_crew import decisions as shared_decisions
from kiro_crew.shared_crew.collab import DecisionKind, DecisionState
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


def _person(login: str) -> Principal:
    return Principal(
        id=principal_id_for("tailscale", "tailnet", login),
        kind=PrincipalKind.HUMAN,
        provider="tailscale",
        issuer="tailnet",
        subject=login,
        provenance=Provenance.TAILSCALE_SERVE,
        display=DisplaySnapshot(name=login.split("@")[0], login=login),
    )


ALICE = _person("alice@example.test")
BOB = _person("bob@example.test")


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)


# -- 2. shared decisions open when the request shows ------------------------------------


@pytest.fixture
def dash_state(tmp_path, monkeypatch, _floor_monkeypatch):
    from chat_test_helpers import _make_state

    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.broadcast_ws_owners = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    return st


def _authored_slot(state, key: str, author: Principal):
    from kiro_crew.dashboard.state import SlotOrigin

    slot = state.get_or_create_slot(key, origin=SlotOrigin.USER)
    slot.append("user", "go", "msg msg-u", meta={"author": author.to_public_dict()})
    return slot


@pytest.mark.asyncio
async def test_slot_permission_prompt_opens_its_decision_through_the_real_turn(tmp_path):
    """``_run_chat`` drives a real permission event; the decision is pending before any answer."""
    from test_dashboard_approval import (
        _complete_event,
        _context_builder,
        _make_slot,
        _make_state,
        _patch_stats,
        _permission_event,
        _set_stream,
    )

    from kiro_crew.dashboard.chat import _run_chat

    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot("chat-decision")
    slot.append("user", "go", "msg msg-u", meta={"author": ALICE.to_public_dict()})
    state._slots[slot.key] = slot
    _set_stream(client, [_permission_event(), _complete_event()])
    decision_id = shared_decisions.approval_decision_id(slot.key, "req-1")
    seen: dict[str, object] = {}

    async def answer_as_bob():
        await async_wait_until(lambda: "req-1" in slot._approval_futures)
        pending = shared_decisions.decisions_for(state).get(decision_id)
        seen["pending"] = pending
        request = {ACTOR_REQUEST_KEY: _actor(BOB)}
        shared_decisions.resolve_slot_approval(
            state, request, slot, "req-1", slot._approval_futures["req-1"], "approved", "r-bob"
        )

    answer = asyncio.create_task(answer_as_bob())
    with _patch_stats():
        await asyncio.wait_for(_run_chat(state, slot, "hello"), 30)
    await asyncio.wait_for(answer, 30)
    pending = seen["pending"]
    assert pending is not None and pending.state is DecisionState.PENDING
    assert pending.initiator.id == ALICE.id
    final = shared_decisions.decisions_for(state).get(decision_id)
    assert final.state is DecisionState.RESOLVED
    assert final.responder.id == BOB.id and final.outcome == "approved"
    assert any(call.args[0] == "shared_crew.decision" for call in state.broadcast_ws.call_args_list)


def test_unanswered_slot_approval_closes_expired_or_withdrawn(dash_state):
    slot = _authored_slot(dash_state, "tab-x", ALICE)
    loop = asyncio.new_event_loop()
    try:
        for rid, withdrawn, expected in (
            ("q1", False, DecisionState.EXPIRED),
            ("q2", True, DecisionState.WITHDRAWN),
        ):
            fut = loop.create_future()
            did = shared_decisions.open_slot_approval(dash_state, slot, rid)
            shared_decisions.settle_slot_approval(dash_state, did, fut, withdrawn=withdrawn)
            assert shared_decisions.decisions_for(dash_state).get(did).state is expected
        host = loop.create_future()
        host.set_result("rejected")
        did = shared_decisions.open_slot_approval(dash_state, slot, "q3")
        shared_decisions.settle_slot_approval(dash_state, did, host, withdrawn=False)
        settled = shared_decisions.decisions_for(dash_state).get(did)
        assert settled.state is DecisionState.RESOLVED and settled.responder.is_unknown
    finally:
        loop.close()


@pytest.mark.asyncio
async def test_agent_question_opens_its_decision_and_names_who_answered(dash_state):
    from kiro_crew.dashboard.interaction_coordinator import QuestionCoordinator

    slot = _authored_slot(dash_state, "tab-q", ALICE)

    async def one_client(*_a, **_kw):
        return 1

    dash_state.deliver_ws_owners = one_client
    questions = [
        {"question": "Which?", "header": "Pick", "options": [{"label": "A"}, {"label": "B"}]}
    ]
    assert await QuestionCoordinator.open_agent_ask(
        dash_state, "ask-1", slot.key, "dashboard:tab-q", questions
    )
    registry = shared_decisions.decisions_for(dash_state)
    pending = registry.for_session(slot.key)
    assert [(d.kind, d.state, d.initiator.id) for d in pending] == [
        (DecisionKind.QUESTION, DecisionState.PENDING, ALICE.id)
    ]
    assert QuestionCoordinator.resolve(
        dash_state, "ask-1", {"Which?": "A"}, responder=BOB, response_id="r1"
    )
    done = registry.get(shared_decisions.question_decision_id("ask-1"))
    assert done.state is DecisionState.RESOLVED and done.responder.id == BOB.id

    assert await QuestionCoordinator.open_agent_ask(
        dash_state, "ask-2", slot.key, "dashboard:tab-q", questions
    )
    assert QuestionCoordinator.resolve(dash_state, "ask-2", None, reason="expired")
    assert (
        registry.get(shared_decisions.question_decision_id("ask-2")).state is DecisionState.EXPIRED
    )
    for ask in ("ask-1", "ask-2"):
        watcher = dash_state._agent_asks.get(ask, {}).get("watcher")
        if watcher is not None:
            watcher.cancel()
