"""The send path's ``message.steer`` decision answers queue-or-steer and never costs a send."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import chat_handlers
from kiro_crew.decisions.points import message_steer


def _slot():
    return SimpleNamespace(key="dashboard:s1", messages=[{"role": "user", "content": "hi"}])


@pytest.fixture
def session_key(monkeypatch):
    monkeypatch.setattr(chat_handlers, "effective_session_key", lambda slot: "dashboard:s1")


def _decide(monkeypatch, outcome):
    async def steer_or_queue(message, *, session_key, rows):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(message_steer, "steer_or_queue", steer_or_queue)


@pytest.mark.parametrize("value", ["auto", " Auto "])
def test_only_the_auto_string_asks_for_a_decision(value):
    assert chat_handlers.steer_is_auto(value)


@pytest.mark.parametrize("value", [True, "automatic", "", None])
def test_anything_else_is_not_auto(value):
    assert not chat_handlers.steer_is_auto(value)


@pytest.mark.asyncio
async def test_no_decision_takes_the_steer_path(monkeypatch, session_key):
    _decide(monkeypatch, None)
    assert await chat_handlers.decided_message_handling(_slot(), "hello") == (False, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("choice", "queues"), [(message_steer.CHOICE_QUEUE, True), ("steer", False)]
)
async def test_a_decision_returns_its_path_and_the_recorded_row(
    monkeypatch, session_key, choice, queues
):
    _decide(monkeypatch, {"choice": choice})
    record = {"turn_id": "t1"}
    monkeypatch.setattr(message_steer, "record_outcome", lambda key, decided: record)
    assert await chat_handlers.decided_message_handling(_slot(), "hello") == (queues, record)


@pytest.mark.asyncio
async def test_a_failing_seam_falls_back_to_steer(monkeypatch, session_key):
    _decide(monkeypatch, RuntimeError("seam down"))
    assert await chat_handlers.decided_message_handling(_slot(), "hello") == (False, None)


@pytest.mark.asyncio
async def test_cancellation_is_not_swallowed(monkeypatch, session_key):
    _decide(monkeypatch, asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await chat_handlers.decided_message_handling(_slot(), "hello")
