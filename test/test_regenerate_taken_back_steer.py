"""Regenerate past a steer the person took back (``steer_retract._mark_retracted``).

A taken-back steer keeps its row, with its text cleared, between the reply written
before the steer and the one written after it. Regenerate reads the nearest
``user`` row as the request, so it steps past that row to the request the turn was
answering. A steer the turn read is still a request of its own.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import steer_retract
from kiro_crew.dashboard.chat_delivery import (
    STEER_RETRACTABLE_META,
    STEER_STATE_CONSUMED,
    STEER_STATE_WRITTEN,
)

_PROMPT = "draft the rollout plan"
_STEER = "only cover the EU stage"
_REPLY_BEFORE_STEER = "Stage one ships to beta on Monday."
_REPLY_AFTER_STEER = "Stage two ships to prod on Thursday."


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return _make_state(tmp_path)


def _steer_row(slot, steer_state: str = STEER_STATE_WRITTEN) -> dict:
    return slot.append(
        "user",
        _STEER,
        "msg msg-u",
        meta={"steer": True, "steerState": steer_state, STEER_RETRACTABLE_META: True},
    )


def _take_back(state, slot, row: dict) -> None:
    """Leave *row* exactly as an Unsend leaves it: in place, text cleared."""
    record = steer_retract.SteerRetract(
        message=_STEER, mid=row["meta"]["mid"], action=steer_retract.RETRACT_UNSEND
    )
    steer_retract._mark_retracted(state, slot, record)
    assert row["content"] == ""


async def _regenerate(state, slot):
    """POST regenerate with the turn stubbed out.

    Returns the status, the body, the stubbed turn and the variants it was handed.
    """
    variants: list[dict] = []
    run_chat = AsyncMock(side_effect=lambda *a, **kw: variants.extend(slot._pending_variants))
    with patch("kiro_crew.dashboard.chat_regenerate._run_chat", new=run_chat):
        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post(f"/api/chat/slots/{slot.key}/regenerate")
            body = await resp.json()
            await asyncio.sleep(0)
    return resp.status, body, run_chat, variants


def _transcript(slot) -> list[tuple[str, str]]:
    return [(m["role"], m["content"]) for m in slot.messages]


@pytest.mark.asyncio
async def test_regenerate_runs_the_prompt_before_a_taken_back_steer(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY_BEFORE_STEER)
    _take_back(state, slot, _steer_row(slot))
    slot.append("assistant", _REPLY_AFTER_STEER)
    slot.drain()

    status, body, run_chat, variants = await _regenerate(state, slot)

    assert status == 200, body
    assert run_chat.call_args.args[2] == _PROMPT
    assert _transcript(slot) == [("user", _PROMPT)]
    assert [v["content"] for v in variants] == [_REPLY_AFTER_STEER]


@pytest.mark.asyncio
async def test_regenerate_takes_the_reply_before_a_taken_back_steer_with_none_after(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY_BEFORE_STEER)
    _take_back(state, slot, _steer_row(slot))
    slot.drain()

    status, body, run_chat, variants = await _regenerate(state, slot)

    assert status == 200, body
    assert run_chat.call_args.args[2] == _PROMPT
    assert _transcript(slot) == [("user", _PROMPT)]
    assert [v["content"] for v in variants] == [_REPLY_BEFORE_STEER]


@pytest.mark.asyncio
async def test_a_taken_back_steer_does_not_let_regenerate_cross_the_request_above_it(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", "an earlier request")
    slot.append("assistant", "the earlier reply")
    slot.append("user", _PROMPT)
    _take_back(state, slot, _steer_row(slot))
    slot.drain()
    before = _transcript(slot)

    status, body, run_chat, _variants = await _regenerate(state, slot)

    assert (status, body["code"]) == (400, "no_assistant_message")
    run_chat.assert_not_called()
    assert _transcript(slot) == before


@pytest.mark.asyncio
async def test_regenerate_still_runs_a_steer_the_turn_read_as_the_request(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY_BEFORE_STEER)
    _steer_row(slot, STEER_STATE_CONSUMED)
    slot.append("assistant", _REPLY_AFTER_STEER)
    slot.drain()

    status, body, run_chat, variants = await _regenerate(state, slot)

    assert status == 200, body
    assert run_chat.call_args.args[2] == _STEER
    assert _transcript(slot) == [
        ("user", _PROMPT),
        ("assistant", _REPLY_BEFORE_STEER),
        ("user", _STEER),
    ]
    assert [v["content"] for v in variants] == [_REPLY_AFTER_STEER]


@pytest.mark.asyncio
async def test_regenerate_still_stops_at_a_steer_the_turn_read_with_no_reply_after(state):
    slot = state.get_or_create_slot("s1")
    slot.append("user", _PROMPT)
    slot.append("assistant", _REPLY_BEFORE_STEER)
    _steer_row(slot, STEER_STATE_CONSUMED)
    slot.drain()
    before = _transcript(slot)

    status, body, run_chat, _variants = await _regenerate(state, slot)

    assert (status, body["code"]) == (400, "no_assistant_message")
    run_chat.assert_not_called()
    assert _transcript(slot) == before
