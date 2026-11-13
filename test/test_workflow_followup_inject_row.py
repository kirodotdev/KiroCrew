"""The gateway's follow-up prompt after a workflow result is an injected row.

When a chat-launched workflow finishes, the gateway asks the agent to answer the
request that started it. That prompt is the gateway's own words, never a chat bubble
the person typed: on an idle slot and on a busy one (queued, then drained) it is the
same ``inject`` row, and the turn it starts runs as a synthetic payload.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat_runner

PROMPT = "[Workflow `nightly` finished: finished] Its result was just posted above."


async def _no_turn(state, slot, prompt):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "queued"])
async def test_the_workflow_followup_is_an_inject_row_on_both_paths(tmp_path, busy):
    state, slot = _state(tmp_path), _slot()
    state.broadcast_ws = MagicMock()
    slot._turn_admission_reserved = busy

    started = slot.enqueue_or_run_prompt(
        PROMPT, _no_turn, state, gateway_prompt_kind="workflow_followup"
    )

    if busy:
        assert started is False
        [entry] = slot._queue
        assert entry["kind"] == "workflow_followup"
        # Its kind decides the row, so an edit cannot pass the person's words off as it.
        assert slot.queue_edit_by_id(entry["id"], "my own words") is False
        slot._turn_admission_reserved = False
        run_chat = MagicMock()
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", run_chat),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True
        assert run_chat.call_args.kwargs["_synthetic_payload"] is True
        pops = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop"]
        assert [p["drain_writes_row"] for p in pops] == [True], "never rebuilt as a user row"
    else:
        assert started is True
        await slot.task

    rows = [m for m in slot.messages if m.get("content") == PROMPT]
    assert [(r["role"], r["meta"]["injectKind"]) for r in rows] == [("inject", "workflow")]
