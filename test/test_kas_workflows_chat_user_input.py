"""A ``_kiro/userInput`` question with Kiro Workflows off and on.

A chat's own question always takes main's elicitation path (``acp.elicitation``),
switch or no switch: that path holds it for a person and is never answered by an
approval mode. Only a question nobody can read, or one a workflow step's sink
fails on, is answered by this PR's code, and only with the switch on. KAS is only
ever sent ``{"action": "answered", "answer": <non-empty>}``: it reads any other
reply, ``dismissed`` included, as "move on", which its tool tells the model is yes.

Driven through the REAL reader loop on a fake stdout (``test_acp_observe_turn``).
"""

from __future__ import annotations

import asyncio

import pytest
from test_acp_observe_turn import _feed, _info, _queued, _session, _stop, _written

from kiro_crew.acp import elicitation as el
from kiro_crew.acp.kas_wire import KIND_TURN_END, KIND_TURN_START
from kiro_crew.acp.types import EVENT_ELICITATION_REQUEST, EVENT_PERMISSION_REQUEST
from kiro_crew.kas_workflows import asks
from kiro_crew.kas_workflows import chat as kas_chat
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout

_UI = "_kiro/userInput"
_DECLINED = {"action": "answered", "answer": el.USER_INPUT_DECLINED_ANSWER}
_NO_PERSON = {"action": "answered", "answer": el.USER_INPUT_NO_PERSON_ANSWER}


def _ask(rid: int = 41, session_id: str = "sA") -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": _UI,
        "params": {
            "sessionId": session_id,
            "toolCallId": "tc-1",
            "question": "Which database?",
            "options": [{"title": "Postgres"}, {"title": "SQLite"}],
        },
    }


def _switch(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: on)


def _replies(proc, rid: int) -> list:
    return [m.get("result", m.get("error")) for m in _written(proc) if m.get("id") == rid]


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_a_chat_question_nobody_can_see_takes_the_elicitation_path(monkeypatch, on):
    _switch(monkeypatch, on)
    rt, reader, proc, handle, task = await _session()
    try:
        assert handle.accepts_elicitation is False
        _feed(reader, _info(KIND_TURN_START))
        _feed(reader, _ask())
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        await _queued(handle, 3)
        events = [
            ev async for ev in handle.observe_turn(timeout=default_timeout(), idle_window=5.0)
        ]
        assert not [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        # The elicitation path answers a question no person can see this way.
        assert _replies(proc, 41) == [_NO_PERSON]
    finally:
        await _stop(task)


@pytest.mark.parametrize("on", [False, True])
@pytest.mark.asyncio
async def test_a_person_sees_the_elicitation_card_and_a_deny_is_a_decline(monkeypatch, on):
    _switch(monkeypatch, on)
    rt, reader, proc, handle, task = await _session()
    try:
        handle.accepts_elicitation = True
        _feed(reader, _info(KIND_TURN_START))
        _feed(reader, _ask())
        await _queued(handle, 2)
        events: list = []

        async def drive():
            async for ev in handle.observe_turn(timeout=default_timeout(), idle_window=5.0):
                events.append(ev)

        turn = asyncio.ensure_future(drive())
        await async_wait_until(lambda: any(e.kind == EVENT_ELICITATION_REQUEST for e in events))
        assert not [e for e in events if e.kind == EVENT_PERMISSION_REQUEST]
        assert await handle.answer_elicitation(41, el.ELICITATION_DECLINE)
        assert _replies(proc, 41) == [_DECLINED]
        _feed(reader, _info(KIND_TURN_END, stopReason="end_turn"))
        await await_until(turn, what="the observed turn ending")
    finally:
        await _stop(task)


@pytest.mark.parametrize("on", [True, False])
@pytest.mark.asyncio
async def test_a_question_on_a_session_nobody_reads(monkeypatch, on):
    # On: answered as no person saw it. Off: left as it is without the switch.
    _switch(monkeypatch, on)
    rt, reader, proc, handle, task = await _session()
    try:
        _feed(reader, _ask(rid=43, session_id="sNobody"))
        _feed(reader, _ask(rid=44))  # a frame the reader loop surely read after it
        await _queued(handle, 1)
        if on:
            await async_wait_until(lambda: _replies(proc, 43))
            assert _replies(proc, 43) == [_NO_PERSON]
        else:
            assert _replies(proc, 43) == []
    finally:
        await _stop(task)


class _FailingSink:
    """A workflow sink that claims every step request and then fails on it."""

    def claims_step_request(self, session_id: str, method: str) -> bool:
        return True

    def on_step_request(self, *args, **kwargs) -> None:
        raise RuntimeError("router down")

    def __getattr__(self, name):
        return lambda *a, **k: False


@pytest.mark.asyncio
async def test_a_step_question_the_sink_fails_on_is_answered_not_cancelled(monkeypatch):
    _switch(monkeypatch, True)
    rt, reader, proc, handle, task = await _session()
    try:
        rt._workflow_sink = _FailingSink()
        _feed(reader, _ask(rid=45, session_id="step-1"))
        await async_wait_until(lambda: _replies(proc, 45))
        assert _replies(proc, 45) == [_NO_PERSON]
    finally:
        await _stop(task)


def test_every_no_answer_result_is_answered_with_text():
    assert asks.dismiss_result_for(_UI) == _NO_PERSON
    assert asks.user_input_no_answer(person=True) == _DECLINED
    assert asks.user_input_no_answer(person=False) == _NO_PERSON
    assert asks.user_input_answered("") == _DECLINED
    assert asks.user_input_answered(" \n") == _DECLINED
    assert asks.user_input_answered("Postgres") == {"action": "answered", "answer": "Postgres"}
