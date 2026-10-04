"""A Stop that already resolved to idle vetoes every runtime-loss requeue in ``_run_chat``.

``_should_suppress_requeue`` reads only a Stop that is in flight. A Stop that
pressed and snapped back to idle before the turn's error reached its handler
leaves ``_stopping`` False and moves only the Stop counters, which
``_stop_pressed()`` reads. The runtime-death arm (``AcpProcessDied``) and the
busy / pipe-death arm (a retry-eligible ``AcpError``) must read both, as the
lost-session arm does, or the prompt the user stopped is queued for replay on a
fresh runtime. The replay goes through ``_queue_recovery``, which takes no Stop
snapshot of its own, so the arm's gate is the only place a resolved Stop can be
seen.

Each case runs twice: once with no Stop, to prove the arm really requeues the
prompt in this harness, and once with the Stop resolved before the error.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.transport_errors import AcpProcessDied, AcpPromptBusy

_MESSAGE = "delete the old branches"

# One factory per arm: the typed runtime death, the pipe death that arrives as a
# plain AcpError, and a prompt-busy answer. The last two share one gate.
_ERRORS = {
    "process-died": lambda: AcpProcessDied("kiro-cli exited"),
    "pipe-death": lambda: AcpError("ACP process exited unexpectedly"),
    "prompt-busy": lambda: AcpPromptBusy("prompt already in progress"),
}


def _state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.push_refresh = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _client(stream):
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.pop_pending_oauth_requests = MagicMock(return_value=[])
    client.available_models = MagicMock(return_value=[])
    client.client = None
    client.stream = stream
    client.stream_command = stream
    client.served_model = "test-model"
    return client


def _wire(state, client):
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.get_pid = MagicMock(return_value=None)
    state.sessions.check_context_usage = MagicMock()
    state.sessions.record_success = MagicMock()
    state.sessions.record_failure = AsyncMock()
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.discard_conversation = AsyncMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))


async def _drain(state, limit=30):
    for _ in range(limit):
        pending = [t for t in list(state._background_tasks) if not t.done()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)


def _retry_cards(slot):
    return [
        m["content"]
        for m in slot.messages
        if m.get("role") == "error" and "retrying" in str(m.get("content", ""))
    ]


async def _run(tmp_path, monkeypatch, error, *, stop_resolves_first):
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    calls: list[str] = []

    async def _stream(msg):
        calls.append(msg)
        if len(calls) == 1:
            if stop_resolves_first:
                # The user's Stop lands mid-turn and resolves straight back to
                # idle before the runtime's failure reaches the handler.
                slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1
                slot._stopping = False
            raise error()
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="replayed and answered")
        yield LLMEvent(kind=EVENT_COMPLETE)

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stream))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, _MESSAGE)
        await _drain(state)
    return calls, slot


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", sorted(_ERRORS))
async def test_the_arm_requeues_the_prompt_when_nothing_was_stopped(tmp_path, monkeypatch, arm):
    """Positive control: without a Stop, each arm replays the prompt once."""
    calls, slot = await _run(tmp_path, monkeypatch, _ERRORS[arm], stop_resolves_first=False)

    assert calls == [_MESSAGE, _MESSAGE]
    assert _retry_cards(slot)


@pytest.mark.asyncio
@pytest.mark.parametrize("arm", sorted(_ERRORS))
async def test_a_stop_resolved_before_the_error_never_queues_a_replay(tmp_path, monkeypatch, arm):
    """A Stop already back at idle is seen through the Stop counters, so nothing
    is queued and no "retrying" card promises a replay that must not run."""
    calls, slot = await _run(tmp_path, monkeypatch, _ERRORS[arm], stop_resolves_first=True)

    assert calls == [_MESSAGE]
    assert slot._queue == []
    assert _retry_cards(slot) == []
