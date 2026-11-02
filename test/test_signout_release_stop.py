"""The sign-in hold at the stop and synthesis hand-offs.

A person's priority send made after a sign-in failure is the send that lets the
queue try again, so a stop that settles at one of these sites starts it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_responsive_chat import _busy
from test_responsive_chat import fixture as _responsive_fixture

from kiro_crew.dashboard import chat_handlers, chat_runner
from kiro_crew.dashboard import responsive_chat as rc
from kiro_crew.dashboard.chat_utils_completion import SYNTHESIS_HELD
from kiro_crew.dashboard.chat_utils_injected_rows import mark_signout_retained

fixture = _responsive_fixture


async def _settle_stop_during_done_read(state, slot, request, monkeypatch, *, mark_priority):
    _busy(slot)
    owner = slot.task
    slot._stop_state = "soft_pending"
    slot._last_turn_auth_required = True
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)
    await rc.accept(request, slot, "priority", None)
    if mark_priority:
        mark_signout_retained(slot)
    resolver = chat_handlers._make_stop_resolver(state, slot, "hard", None)
    run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", run)

    async def done_payload(*args, **kwargs):
        await resolver()
        return {"slot": slot.key}

    monkeypatch.setattr(chat_runner, "chat_done_payload", done_payload)
    try:
        await chat_runner._finish_queue_cycle(state, slot, allow_automatic_successor=False)
        if slot.task is not None:
            await asyncio.wait_for(slot.task, 30)
    finally:
        owner.cancel()
    return run


@pytest.mark.asyncio
async def test_a_stop_settling_in_the_done_read_resumes_a_post_failure_send(fixture, monkeypatch):
    state, slot, request, *_ = fixture

    run = await _settle_stop_during_done_read(
        state, slot, request, monkeypatch, mark_priority=False
    )

    assert run.call_count == 1, "the post-failure send stayed queued behind the stop"


@pytest.mark.asyncio
async def test_a_stop_settling_in_the_done_read_leaves_a_retained_queue_held(fixture, monkeypatch):
    state, slot, request, *_ = fixture

    run = await _settle_stop_during_done_read(state, slot, request, monkeypatch, mark_priority=True)

    run.assert_not_called()


@pytest.mark.asyncio
async def test_a_settled_hard_stop_resumes_a_post_failure_send(fixture, monkeypatch):
    state, slot, request, *_ = fixture
    _busy(slot)
    owner = slot.task
    slot._last_turn_auth_required = True
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)
    await rc.accept(request, slot, "priority", None)
    run = AsyncMock()
    monkeypatch.setattr(chat_runner, "_run_chat", run)

    async def stop_turn(key, **kwargs):
        await chat_runner._finish_queue_cycle(state, slot, allow_automatic_successor=False)
        owner.cancel()
        await kwargs["on_hard"]()
        return "hard"

    state.sessions.stop_turn = AsyncMock(side_effect=stop_turn)
    await chat_handlers.stop_slot_turn(state, slot)
    if slot.task is not None:
        await asyncio.wait_for(slot.task, 30)

    assert run.call_count == 1, "the settled stop left the post-failure send stranded"


@pytest.mark.asyncio
async def test_a_held_synthesis_drains_a_send_queued_after_the_failure(fixture, monkeypatch):
    state, slot, *_ = fixture
    slot._last_turn_auth_required = True
    slot._pending_synthesis = True
    slot._synthesis_inflight = False
    state.subagents = MagicMock()
    slot.queue_append("retained", directive_user_origin=True)
    mark_signout_retained(slot)
    slot.queue_append("sent after the failure", directive_user_origin=True)
    monkeypatch.setattr(
        chat_runner, "synthesis_fire_verdict", AsyncMock(return_value=SYNTHESIS_HELD)
    )
    start = AsyncMock(return_value=True)
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", start)

    await chat_runner._finish_queue_cycle(state, slot, drain=True)

    start.assert_awaited_once()
