"""A live backend that answers 'Session not found' gets one fresh-runtime reload.

The backend process is alive, so nothing marks it dead and the claim-time
dead-provider eviction never runs. Without a recovery the chat stays bound to a
backend session id the process does not hold, and every prompt (Continue
included) draws the same error. The turn must reset the session -- which keeps
the session-map entry, so the next claim session/loads the SAME backend id on a
fresh runtime -- and retry the turn exactly once.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError
from kiro_crew.dashboard.chat_runner import (
    SESSION_NOT_FOUND_CANCELLED_TEXT,
    SESSION_NOT_FOUND_GIVE_UP_TEXT,
    SESSION_NOT_FOUND_RETRY_TEXT,
)
from kiro_crew.dashboard.recovery_replays import ReplayFamily
from kiro_crew.llm_helpers import acp_error_is_session_not_found

_SNF = ReplayFamily.SESSION_NOT_FOUND

# The shape the adapter's JSON-RPC answer takes once formatted for the turn.
_NOT_FOUND = (
    "Prompt error: {'code': -32603, 'message': 'Internal error', 'data': 'Session not found'}"
)


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


def _errors(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "error"]


def _assistant(slot):
    return [m["content"] for m in slot.messages if m.get("role") == "assistant"]


@pytest.mark.asyncio
async def test_lost_session_resets_and_retries_once_then_lands(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent

    calls: list[str] = []

    async def _stream(msg):
        calls.append(msg)
        if len(calls) == 1:
            raise AcpError(_NOT_FOUND)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="reloaded and answered")
        yield LLMEvent(kind=EVENT_COMPLETE)

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stream))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, "what next?")
        await _drain(state)

    assert len(calls) == 2
    # Nothing was emitted before the failure, so the request is replayed as is.
    assert calls[1] == "what next?"
    # reset (not discard): the mapped backend id survives for session/load.
    state.sessions.reset.assert_awaited_once()
    state.sessions.discard_conversation.assert_not_awaited()
    assert SESSION_NOT_FOUND_RETRY_TEXT in _errors(slot)
    assert any("reloaded and answered" in t for t in _assistant(slot))
    # The landed turn re-arms the one-shot for a later loss.
    assert slot._session_not_found_retry_used is False


@pytest.mark.asyncio
async def test_lost_session_twice_ends_on_a_clear_error_without_looping(tmp_path, monkeypatch):
    from kiro_crew.dashboard.chat import _run_chat

    calls: list[str] = []

    async def _fail(msg):
        calls.append(msg)
        raise AcpError(_NOT_FOUND)
        yield  # pragma: no cover

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_fail))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, "continue")
        await _drain(state)

    assert len(calls) == 2
    assert state.sessions.reset.await_count == 2
    errors = _errors(slot)
    assert errors.count(SESSION_NOT_FOUND_RETRY_TEXT) == 1
    assert errors[-1] == SESSION_NOT_FOUND_GIVE_UP_TEXT
    assert slot._queue == []
    assert slot._session_not_found_retry_used is True


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AcpError(_NOT_FOUND), True),
        (AcpError("session not found"), True),
        (AcpError("Internal error: API Error: Internal server error"), False),
        (RuntimeError("Session not found"), False),
    ],
    ids=["formatted", "bare", "other-acp-error", "not-an-acp-error"],
)
def test_predicate_is_scoped_to_acp_errors(exc, expected):
    assert acp_error_is_session_not_found(exc) is expected


@pytest.mark.asyncio
async def test_stop_during_the_reset_purges_the_replay_before_dispatch(
    tmp_path, monkeypatch, caplog
):
    """A soft Stop landing while the reset is awaited keeps the queue and snaps
    back to idle; the Stop counter is the only evidence, and it must veto the
    replay so a cancelled request is never re-run."""
    from kiro_crew.dashboard.chat import _run_chat

    calls: list[str] = []

    async def _fail(msg):
        calls.append(msg)
        raise AcpError(_NOT_FOUND)
        yield  # pragma: no cover

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_fail))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    async def _reset_then_stop(*_a, **_kw):
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1
        return True

    state.sessions.reset = AsyncMock(side_effect=_reset_then_stop)
    caplog.set_level(logging.INFO, logger="kiro_crew.dashboard.chat_runner")

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, "delete the old branches")
        await _drain(state)

    assert len(calls) == 1
    state.sessions.reset.assert_awaited_once()
    assert slot._queue == []
    assert any(m.get("content") == SESSION_NOT_FOUND_CANCELLED_TEXT for m in slot.messages)
    # The aborted episode refunds the one-shot.
    assert slot._session_not_found_retry_used is False
    assert not slot.replays.armed(_SNF)
    # Purged at the drain, before any replay turn was spawned.
    assert "Dropped lost-session replay before dispatch" in caplog.text


@pytest.mark.asyncio
async def test_stop_after_dispatch_aborts_the_replay_before_the_provider(tmp_path, monkeypatch):
    """A Stop after dequeue but before the guarded task consumes the replay still
    vetoes it, from the snapshots the drain handed over."""
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.dashboard.chat_utils import effective_session_key
    from kiro_crew.providers.base import EVENT_COMPLETE, LLMEvent

    provider_calls = 0

    async def _stream(msg):
        nonlocal provider_calls
        provider_calls += 1
        yield LLMEvent(kind=EVENT_COMPLETE)

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stream))
    slot = state.get_or_create_slot("s1")
    slot._titled = True
    state.sessions.stop_generation = lambda key: 4
    slot._session_not_found_retry_used = True
    slot.replays.arm(
        _SNF,
        entry_id="snf-qid",
        session_key=effective_session_key(slot),
        stop_gen=7,
        session_stop_gen=4,
    )
    slot._stop_generation = 8

    await _run_chat(
        state,
        slot,
        "delete the old branches",
        _replay=frozenset({_SNF}),
        _synthetic_recovery_turn=True,
    )

    assert provider_calls == 0
    assert not slot.replays.armed(_SNF)
    assert slot._session_not_found_retry_used is False
    assert any(m.get("content") == SESSION_NOT_FOUND_CANCELLED_TEXT for m in slot.messages)


@pytest.mark.asyncio
async def test_stop_resolved_before_the_error_never_queues_a_replay(tmp_path, monkeypatch):
    """A Stop that pressed and already snapped back to idle before the lost-session
    error reaches the handler leaves ``_stopping`` False; only the Stop counter
    moved. The handler must read that counter itself: the replay snapshots are
    taken after the Stop, so the drain-side veto cannot see it either."""
    from kiro_crew.dashboard.chat import _run_chat

    calls: list[str] = []

    async def _stop_then_fail(msg):
        calls.append(msg)
        # The user's Stop lands mid-turn and resolves straight back to idle.
        slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1
        slot._stopping = False
        raise AcpError(_NOT_FOUND)
        yield  # pragma: no cover

    state = _state(tmp_path, monkeypatch)
    _wire(state, _client(_stop_then_fail))
    slot = state.get_or_create_slot("s1")
    slot._titled = True

    with patch("asyncio.sleep", new_callable=AsyncMock):
        await _run_chat(state, slot, "delete the old branches")
        await _drain(state)

    assert calls == ["delete the old branches"]
    assert slot._queue == []
    assert SESSION_NOT_FOUND_RETRY_TEXT not in _errors(slot)
    assert not slot.replays.armed(_SNF)
    assert slot._session_not_found_retry_used is False
