"""A dashboard turn writes the engine's measurement into its crew log, in order.

Drives the real ``_run_chat`` over a scripted stream, the way ``test_crew_log_order``
does, with the provider's per-turn stats standing where the KAS session handle leaves
them: a FRESH stats object installed when the prompt was sent, carrying the profile
the engine's frames filled. What is asserted is the FILE.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state
from crew_log_drain import assert_drained

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, AcpEvent, AcpPromptStats
from kiro_crew.crew_log import crew_log_path, emit
from kiro_crew.dashboard.chat_runner import _run_chat

SESSION = "acp-measured-0001"

PROFILE = {
    "breakdown": {
        "schema": 1,
        "categories": {"history": 4_000, "toolSpecs": 2_865},
        "model_calls": 1,
        "total_chars": 6_865,
        "history": {"user": 3_990, "assistant": 5, "thinking": 5, "compaction_summary": 0},
    },
    "ttft": {"total_ms": 2_870},
    "turn_id": "engine-turn-1",
}


@pytest.fixture(autouse=True)
def _log_home(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _floor_monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _entries() -> list[dict]:
    path = crew_log_path("session", SESSION)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()][1:]


def _state_and_slot(tmp_path: Path, *, send_prompt: bool, stale_profile: dict | None = None):
    """A slot whose backend streams one reply.

    ``send_prompt`` stands for the runtime sending the prompt, which installs fresh
    per-turn stats (``carry_over``); the engine's frames then fill their profile.
    Without it the stats object the turn started with is still in place.
    """
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = SESSION
    client.client._session_id = SESSION
    client.shutdown = AsyncMock()
    client.last_prompt_stats = AcpPromptStats(kas_profile=stale_profile)

    async def _stream(*_a, **_kw):
        if send_prompt:
            client.last_prompt_stats = AcpPromptStats(kas_profile=dict(PROFILE))
        yield AcpEvent(kind=EVENT_TEXT_CHUNK, text="Hello.")
        yield AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("measured-slot")
    slot.append("user", "hello", "msg msg-u")
    return state, slot, client


@pytest.mark.asyncio
async def test_the_measurement_is_inserted_ahead_of_the_closers_and_moves_nothing_else(tmp_path):
    state, slot, client = _state_and_slot(tmp_path, send_prompt=True)
    await _run_chat(state, slot, "hello")
    assert_drained()
    entries = _entries()
    types = [e["type"] for e in entries]
    assert types.count("context/measured") == 1, types
    measured = next(e for e in entries if e["type"] == "context/measured")
    completed = next(e for e in entries if e["type"] == "turn/completed")
    # The same turn on main writes exactly this sequence without ``context/measured``:
    # the new entry is inserted ahead of the turn's closers and moves nothing else.
    # Every ``context/composed`` is written before ``turn/started``, so this is also
    # after every composition, where the fold finds the turn's row at the tail.
    assert types == [
        "session/class",
        "message/received",
        "request/configured",
        "turn/started",
        "step/started",
        "message/sent",
        "context/measured",
        "step/completed",
        "turn/completed",
    ]
    assert measured["data"]["turn"] == completed["data"]["turn"]
    assert measured["data"]["breakdown"] == PROFILE["breakdown"]
    assert measured["data"]["ttft"] == {"total_ms": 2_870}
    assert measured["data"]["turn_id"] == "engine-turn-1"
    # Taken once: nothing is left on the stats for a later reader.
    assert client.last_prompt_stats.kas_profile is None


@pytest.mark.asyncio
async def test_a_turn_that_sent_no_prompt_does_not_take_an_earlier_turns_profile(tmp_path):
    state, slot, _client = _state_and_slot(tmp_path, send_prompt=False, stale_profile=dict(PROFILE))
    await _run_chat(state, slot, "hello")
    assert_drained()
    types = [e["type"] for e in _entries()]
    assert "turn/completed" in types, types
    assert "context/measured" not in types


@pytest.mark.asyncio
async def test_a_backend_that_measures_nothing_writes_nothing_new(tmp_path):
    state, slot, client = _state_and_slot(tmp_path, send_prompt=False)
    await _run_chat(state, slot, "hello")
    assert_drained()
    types = [e["type"] for e in _entries()]
    assert "turn/completed" in types and "context/measured" not in types
