"""The steer-or-queue decision a busy slot's human send asks ``message.steer`` for.

Every way the seam can fail to answer takes the steer path with no receipt, so the
seam may cost an observation and never a send; only cancellation escapes.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard import chat_handlers
from kiro_crew.decisions.points import message_steer as ms


def _slot() -> SimpleNamespace:
    return SimpleNamespace(key="chat-1-1700000000", messages=[{"role": "user", "content": "hi"}])


@pytest.mark.parametrize(
    ("value", "auto"),
    [("auto", True), (" AUTO ", True), (True, False), ("steer", False), (None, False)],
)
def test_only_the_auto_string_asks_the_seam_to_choose(value, auto):
    assert chat_handlers.steer_is_auto(value) is auto


@pytest.mark.asyncio
@pytest.mark.parametrize(("choice", "queues"), [(ms.CHOICE_QUEUE, True), ("steer", False)])
async def test_an_answer_picks_the_path_and_returns_its_receipt(monkeypatch, choice, queues):
    async def _decide(message, *, session_key, rows):
        return {"choice": choice}

    monkeypatch.setattr(ms, "steer_or_queue", _decide)
    monkeypatch.setattr(ms, "record_outcome", lambda session_key, decided: {"turn": "t-1"})
    assert await chat_handlers.decided_message_handling(_slot(), "hi") == (queues, {"turn": "t-1"})


@pytest.mark.asyncio
async def test_no_decision_takes_the_steer_path_with_no_receipt(monkeypatch):
    async def _decide(message, *, session_key, rows):
        return None

    monkeypatch.setattr(ms, "steer_or_queue", _decide)
    assert await chat_handlers.decided_message_handling(_slot(), "hi") == (False, None)


@pytest.mark.asyncio
async def test_a_failing_seam_takes_the_steer_path_with_no_receipt(monkeypatch):
    async def _decide(message, *, session_key, rows):
        raise RuntimeError("seam down")

    monkeypatch.setattr(ms, "steer_or_queue", _decide)
    assert await chat_handlers.decided_message_handling(_slot(), "hi") == (False, None)


@pytest.mark.asyncio
async def test_cancellation_is_never_swallowed(monkeypatch):
    async def _decide(message, *, session_key, rows):
        raise asyncio.CancelledError

    monkeypatch.setattr(ms, "steer_or_queue", _decide)
    with pytest.raises(asyncio.CancelledError):
        await chat_handlers.decided_message_handling(_slot(), "hi")
