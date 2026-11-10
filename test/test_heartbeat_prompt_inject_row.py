"""A heartbeat prompt delivered to a chat is an injected row, not the person's bubble.

A heartbeat task tagged ``<!-- deliver:prompt:dashboard:<slot> -->`` starts an agent
turn in that chat with ``<title>\\n\\n<result>``. That text is the gateway's, never
something the person typed: on an idle slot and on a busy one (queued, then drained)
it is the same ``inject`` row (``injectKind: heartbeat``), the queued entry cannot be
edited, and the turn runs as a synthetic payload.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from chat_runner_coverage_helpers import _slot, _state

from kiro_crew.dashboard import chat, chat_runner

TITLE = "💓 Heartbeat"
RESULT = "Two CRs have new comments."
PROMPT = f"{TITLE}\n\n{RESULT}"


async def _no_turn(state, slot, prompt, **_kwargs):
    return None


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", [False, True], ids=["idle", "queued"])
async def test_the_heartbeat_prompt_is_an_inject_row_on_both_paths(tmp_path, busy):
    from kiro_crew.slack.gateway import GatewayOrchestrator

    state, slot = _state(tmp_path), _slot("chat-0")
    state.broadcast_ws = MagicMock()
    state.notify = MagicMock()
    state.push_slots_update = MagicMock()
    slot._turn_admission_reserved = busy
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.dashboard_state = state
    orch.slack = None
    orch._owner_id = None

    run_chat = MagicMock(side_effect=_no_turn)
    with (
        patch.object(state, "resolve_slot", return_value=slot),
        patch.object(chat, "_run_chat", run_chat),
    ):
        await orch._deliver_result(TITLE, "CR check", RESULT, "prompt:dashboard:chat-0")

    if busy:
        [entry] = slot._queue
        assert entry["content"] == PROMPT
        assert entry["kind"] == "heartbeat_prompt"
        # Its kind decides the row, so an edit cannot pass the person's words off as it.
        assert slot.queue_edit_by_id(entry["id"], "my own words") is False
        slot._turn_admission_reserved = False
        drained_run = MagicMock()
        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", drained_run),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True
        assert drained_run.call_args.kwargs["_synthetic_payload"] is True
        pops = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_pop"]
        assert [p["drain_writes_row"] for p in pops] == [True], "never rebuilt as a user row"
    else:
        await slot.task
        assert run_chat.call_args.kwargs.get("_synthetic_payload") is True

    rows = [m for m in slot.messages if m.get("content") == PROMPT]
    assert [(r["role"], r["meta"]["injectKind"]) for r in rows] == [("inject", "heartbeat")]


@pytest.mark.parametrize("busy", [False, True], ids=["idle", "queued"])
def test_an_unknown_gateway_prompt_kind_is_refused_on_both_paths(tmp_path, busy):
    state, slot = _state(tmp_path), _slot("chat-0")
    slot._turn_admission_reserved = busy

    with pytest.raises(ValueError, match="unknown gateway prompt kind"):
        slot.enqueue_or_run_prompt(PROMPT, _no_turn, state, gateway_prompt_kind="nope")
    assert slot._queue == [] and slot.messages == []


@pytest.mark.asyncio
async def test_a_queued_heartbeat_prompt_survives_a_restart(tmp_path):
    """A heartbeat prompt queued behind a running turn is written with the slot's
    durable queue and handed back by the restore, as a plain kindless prompt queued
    on a busy slot is: a restart before it drains must not lose the task's report.
    The restore never trusts a kind read from disk, so the restored entry is that
    plain prompt."""
    import json

    from kiro_crew.dashboard.slot_queue_repository import sanitize_restored_queue
    from kiro_crew.slack.gateway import GatewayOrchestrator

    state, slot = _state(tmp_path), _slot("chat-0")
    state.broadcast_ws = MagicMock()
    state.notify = MagicMock()
    state.push_slots_update = MagicMock()
    slot._turn_admission_reserved = True
    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.dashboard_state = state
    orch.slack = None
    orch._owner_id = None
    with (
        patch.object(state, "resolve_slot", return_value=slot),
        patch.object(chat, "_run_chat", MagicMock(side_effect=_no_turn)),
    ):
        await orch._deliver_result(TITLE, "CR check", RESULT, "prompt:dashboard:chat-0")

    [entry] = slot._queue
    assert entry["kind"] == "heartbeat_prompt"
    on_disk = json.loads(json.dumps(slot.durable_queue_entries()))
    assert [e["content"] for e in on_disk] == [PROMPT]
    [restored] = sanitize_restored_queue(on_disk)
    assert (restored["content"], restored["kind"]) == (PROMPT, "")
