"""A thinking-binding rebuild replays a turn that carried a merge card.

Its own file because ``test_chat_runner_coverage_turn_start.py``, where the
other verbatim-replay cases live, is at the 2,000-line cap.
"""

from __future__ import annotations

import pytest
from chat_runner_coverage_helpers import _complete
from test_chat_runner_coverage_turn_start import TestVerbatimReplayMergeCards as _CardReplays

from kiro_crew.acp.client import AcpConversationBindingMismatch
from kiro_crew.acp.types import EVENT_TEXT_CHUNK
from kiro_crew.providers.base import LLMEvent


@pytest.mark.usefixtures("close_skills_loaders")
@pytest.mark.asyncio
async def test_a_thinking_binding_rebuild_replays_the_card_once(tmp_path):
    """The rebuild re-queues the turn's own ``message``, frame included, so
    ``_queue_recovery`` is what strips the card: the turn raised before its
    card's context was consumed, and the recovery turn drains the restored card."""
    state, client, slot = _CardReplays._state_with_context_builder(tmp_path)
    mismatch = AcpConversationBindingMismatch(
        "Invalid `signature` in `thinking` block. The block is bound to a different conversation."
    )
    _CardReplays._script(
        client,
        mismatch,
        [LLMEvent(kind=EVENT_TEXT_CHUNK, text="ok"), _complete()],
    )

    await _CardReplays._run_and_await_replay(state, slot, "what landed?")

    state.sessions.discard_conversation.assert_awaited_once()
    prompts = [_CardReplays._delivered(call.args[0]) for call in client.stream.call_args_list]
    assert len(prompts) == 2, prompts
    first, replay = prompts
    assert first.count(_CardReplays.CARD) == 1 and first.count(_CardReplays.PLAIN) == 1
    assert replay.count(_CardReplays.CARD) == 1, replay
    assert replay.count(_CardReplays.PLAIN) == 1, replay
    assert replay.endswith("what landed?")
    assert slot._pending_context == []
    assert slot._inflight_merge_contexts == []
