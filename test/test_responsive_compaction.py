"""A queued human reply precedes only Crew's deferrable compaction."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_chat_runner_coverage import _complete, _runner_state, _slot
from test_compact_after_turn_notice import KEY, _managed, _provider

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import responsive_chat as rc
from kiro_crew.providers.base import EVENT_TEXT_CHUNK, LLMEvent


@pytest.mark.asyncio
async def test_human_handoff_defers_compaction_but_accounts_then_answer_compacts(monkeypatch):
    provider = _provider(80.0)
    async with _managed(provider) as mgr:
        compact = AsyncMock(return_value="ok")
        monkeypatch.setattr(mgr, "_compact_session", compact)
        before = mgr._sessions[KEY].prompt_count
        assert mgr.check_context_usage(KEY, provider, defer_compaction=True) == 80.0
        assert mgr._sessions[KEY].prompt_count == before + 1
        assert not mgr.is_compacting(KEY)
        assert not mgr._background_tasks
        compact.assert_not_called()

        # The answer has no persistent deferral to inherit.
        mgr.check_context_usage(KEY, provider)
        assert mgr.is_compacting(KEY)
        await asyncio.wait_for(asyncio.gather(*mgr._background_tasks), 30)
        compact.assert_awaited_once_with(KEY, 80.0)
        mgr._compaction._set_compacting(KEY, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("pct", [95.0, 100.0, 110.0])
async def test_human_handoff_keeps_critical_compaction(monkeypatch, pct):
    provider = _provider(pct)
    async with _managed(provider) as mgr:
        compact = AsyncMock(return_value="ok")
        monkeypatch.setattr(mgr, "_compact_session", compact)
        mgr.check_context_usage(KEY, provider, defer_compaction=True)
        assert mgr.is_compacting(KEY)
        await asyncio.wait_for(asyncio.gather(*mgr._background_tasks), 30)
        compact.assert_awaited_once_with(KEY, pct)
        mgr._compaction._set_compacting(KEY, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["unknown", "pending", "in_progress", "recycle"])
async def test_human_handoff_preserves_nondeferrable_gate_path(monkeypatch, case):
    provider = _provider(80.0, unknown=case == "unknown")
    if case == "recycle":
        provider.manual_compact_unsupported_backend = "opaque"
        provider.compaction_unmanaged_backend = "opaque"
    async with _managed(provider) as mgr:
        if case == "pending":
            mgr._compaction.state.pending_verdict[KEY] = 80.0
        if case == "in_progress":
            mgr._compaction._set_compacting(KEY, True)
        trigger = MagicMock(return_value="declined")
        monkeypatch.setattr(mgr, "_trigger_compaction", trigger)
        mgr.check_context_usage(KEY, provider, defer_compaction=True)
        trigger.assert_called_once_with(KEY, "context at 80%", 80.0, provider)
        mgr._compaction._set_compacting(KEY, False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interrupted", "priority", "same_owner", "expected"),
    [
        (True, True, True, True),
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
    ],
)
async def test_runner_defers_only_interrupted_owner_with_priority_waiting(
    tmp_path, monkeypatch, interrupted, priority, same_owner, expected
):
    state, client = _runner_state(tmp_path)
    slot = _slot("human-handoff")
    state._slots[slot.key] = slot
    slot.append("user", "Original work")
    client.billing_stats = MagicMock(return_value=None)
    client.has_active_turn = MagicMock(return_value=False)
    client.is_alive = MagicMock(return_value=True)
    client.supports_steer = False
    client.client = SimpleNamespace(
        pop_pending_oauth_requests=lambda: [],
        awaiting_permission=False,
        last_prompt_stats=None,
        pending_steers=[],
        supports_steer=False,
    )
    # Keep the next turn admitted, but leave its execution to the caller.
    monkeypatch.setattr(chat_runner, "_start_next_queued_turn", AsyncMock(return_value=False))

    async def stream(prompt):
        turn = rc.turn_state(slot)
        turn.interrupted = interrupted
        if not same_owner:
            turn.owner = object()
        slot._queue.append(
            {
                "id": "human",
                "content": "What is happening?",
                rc.RESPONSIVE_KEY: priority,
                "_directive_user_origin": True,
            }
        )
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="Partial work")
        yield _complete("cancelled")

    client.stream = MagicMock(side_effect=stream)
    await asyncio.wait_for(
        chat_runner._run_chat(state, slot, "Original work", _directive_user_origin=True), 30
    )
    check = state.sessions.check_context_usage
    check.assert_called_once()
    assert check.call_args.kwargs == ({"defer_compaction": True} if expected else {})
