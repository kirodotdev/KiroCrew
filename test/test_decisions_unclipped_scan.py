"""Every point that clips the new message hands the gate the text before the clip.

A point sends ``text[:MAX_MESSAGE_CHARS]``, and the gate refuses a request by
scanning what it is handed. A credential straddling the clip arrives as a
fragment no pattern matches, so the refusal would never fire while the fragment
went on the wire. Each test below drives one point with such a message, captures
what it handed ``decide``, and asserts the gate's own scrub refuses it.
"""

from __future__ import annotations

import asyncio

import pytest

from kiro_crew.decisions import gate as gate_mod
from kiro_crew.decisions.points import memory_recall as recall_mod
from kiro_crew.decisions.points import message_steer as steer_mod
from kiro_crew.decisions.points import model_route as route_mod
from kiro_crew.decisions.points import skills_select as skills_mod

#: A vendor token the gate's table carries, placed so the 2000-char clip lands
#: ten characters into it.
_MESSAGE = "w " * 995 + "sk-" + "a" * 32


@pytest.fixture
def captured(monkeypatch):
    """Replace the gate with a spy that records each call and answers nothing."""
    calls: list[tuple] = []

    async def _decide(point, state, questions, **kwargs):
        calls.append((state, questions, kwargs))
        return None

    monkeypatch.setattr("kiro_crew.decisions.decide", _decide)
    return calls


def _refused(calls) -> bool:
    assert len(calls) == 1, "the point never reached the gate"
    state, questions, kwargs = calls[0]
    assert gate_mod.scrub_reason(state, questions) is None, "the clip no longer halves the key"
    assert kwargs.get("unclipped") == _MESSAGE, "the gate was handed only the clipped text"
    reason = gate_mod.scrub_reason(state, questions, unclipped=kwargs["unclipped"])
    return reason == gate_mod.ERROR_SCRUBBED_CREDENTIAL


def test_skills_select_hands_the_gate_the_unclipped_message(captured):
    asyncio.run(
        skills_mod._ask_one(
            _MESSAGE,
            [{"key": "k", "description": "d"}],
            [],
            session_key=None,
            extra={},
            deadline=None,
        )
    )
    assert _refused(captured)


def test_memory_recall_hands_the_gate_the_unclipped_message(captured):
    rows = [{"key": "m1", "snippet": "remembered"}]
    asyncio.run(recall_mod.keep_decision(_MESSAGE, rows))
    assert _refused(captured)


def test_model_route_hands_the_gate_the_unclipped_message(captured):
    asyncio.run(route_mod.routed_model(_MESSAGE, track=False))
    assert _refused(captured)


def test_message_steer_hands_the_gate_the_unclipped_message(captured):
    asyncio.run(steer_mod.steer_or_queue(_MESSAGE))
    assert _refused(captured)
