"""Crew log edge cases: backend throttle, model fallback, and model attribution.

The throttle-fallback ladder in ``_run_chat`` is the one model decision a
transcript cannot answer afterwards: the user picked the primary and the turn
ran somewhere else. These tests pin what the crew log writes and — more critically
— what it does NOT write, so a reader can trust that the model named in an entry
is the model that produced the work.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.client import AcpError
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    AcpEvent,
    TurnUsage,
)
from kiro_crew.crew_log import emit, ledger_path
from kiro_crew.dashboard.chat_runner import _ledger_model, _run_chat
from kiro_crew.llm_helpers import TRANSIENT_RETRIES

SESSION = "acp-throttle-0001"


@pytest.fixture(autouse=True)
def _ledger_home(tmp_path, monkeypatch):
    """Own data home, emitter on, no state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(emit.CREW_LOG_ENV, "1")
    emit.reset_caches()
    yield
    emit.reset_caches()


def _entries() -> list[dict]:
    path = ledger_path("session", SESSION)
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()][1:]


def _entries_of(entry_type: str) -> list[dict]:
    return [e for e in _entries() if e["type"] == entry_type]


def _make_turn_usage(
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> TurnUsage:
    u = TurnUsage()
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_read_tokens = 0
    u.cache_creation_tokens = 0
    u.credits = 0.001
    u.cost_usd = 0.0001
    u.duration_ms = 50
    return u


def _state_and_slot(
    tmp_path: Path,
    stream_fn,
    *,
    served_model: str = "",
):
    """Build a state + slot whose client.stream calls *stream_fn*.

    *stream_fn* is an async generator factory ``() -> AsyncIterator[AcpEvent]``.
    """
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = SESSION
    client.client._session_id = SESSION
    client.shutdown = AsyncMock()

    async def _stream(*_a, **_kw):
        async for event in stream_fn():
            yield event

    client.stream = _stream
    client.stream_command = _stream
    client.served_model = served_model or ""

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
    slot = state.get_or_create_slot("throttle-slot")
    slot.append("user", "hello", "msg msg-u")
    slot.served_model = served_model or ""
    return state, slot, client


def _successful_events(text: str = "ok") -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text=text),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", usage=_make_turn_usage()),
    ]


def _transient_error() -> AcpError:
    return AcpError("InternalServerError: throttled", transient=True)


def _set_fallback_attrs(slot, monkeypatch):
    """Set fallback bookkeeping attributes on a slot for the swap path."""
    for attr, val in [
        ("_fallback_candidate_idx", 0),
        ("_fallback_primary_model", ""),
        ("_fallback_slot_model", ""),
        ("_fallback_pick_gen", 0),
        ("_active_fallback_model", ""),
        ("_fallback_walked", []),
        ("_model_pick_lock", asyncio.Lock()),
        ("_model_pick_gen", 0),
    ]:
        try:
            setattr(slot, attr, val)
        except AttributeError:
            object.__setattr__(slot, attr, val)
    # _sync_served_model reads record_served_model — stub the side effect.
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._sync_served_model",
        lambda _s, _c: None,
    )


# ---------------------------------------------------------------------------
# Test 1: Same-model retry succeeds — no fallback model/selected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_model_retry_success_no_fallback_logged(tmp_path, monkeypatch):
    """A throttle that retries on the SAME model and succeeds.

    The crew log must NOT contain ``model/selected`` with ``source=fallback``.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)

    call_count = 0

    async def _stream():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _transient_error()
        for ev in _successful_events("recovered"):
            yield ev

    state, slot, client = _state_and_slot(tmp_path, _stream, served_model="claude-sonnet-4")

    await _run_chat(state, slot, "say something")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    fallback_picks = [
        e
        for e in entries
        if e["type"] == "model/selected" and e.get("data", {}).get("source") == "fallback"
    ]
    assert (
        fallback_picks == []
    ), f"a same-model retry wrote a fallback model/selected: {fallback_picks}"
    # The turn must close, confirming the retry path ran.
    closers = _entries_of("turn/completed")
    assert len(closers) >= 1, f"no turn closer: {[e['type'] for e in entries]}"


# ---------------------------------------------------------------------------
# Test 2: Fallback swap — model/selected written with source=fallback.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_swap_records_model_selected(tmp_path, monkeypatch):
    """Throttle exhausts same-model budget, swaps to fallback.

    Pre-set the retry counter to TRANSIENT_RETRIES so one more failure enters
    the fallback branch. ``model/selected`` must carry ``source=fallback`` and
    the turn ordinal.
    """
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
        lambda: ("fallback-model-A", "auto"),
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)

    async def _fake_advance(client, fb_state, *, surface="", log_suffix=""):
        if fb_state.pos < len(fb_state.chain):
            candidate = fb_state.chain[fb_state.pos]
            fb_state.pos += 1
            return candidate
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.advance_fallback_candidate",
        _fake_advance,
    )

    async def _stream():
        raise _transient_error()
        yield  # noqa: unreachable

    state, slot, client = _state_and_slot(tmp_path, _stream, served_model="primary-model")
    # Pre-set counter to the threshold so the fallback branch fires.
    slot._transient_5xx_retries = TRANSIENT_RETRIES
    _set_fallback_attrs(slot, monkeypatch)

    await _run_chat(state, slot, "say something")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    fb_selected = [
        e
        for e in entries
        if e["type"] == "model/selected" and e["data"].get("source") == "fallback"
    ]
    assert len(fb_selected) >= 1, (
        f"no model/selected with source=fallback in: "
        f"{[(e['type'], e.get('data', {}).get('source', '')) for e in entries]}"
    )
    fb = fb_selected[0]["data"]
    assert fb["model"] == "fallback-model-A"
    assert "turn" in fb, "model/selected must name the turn the swap happened in"
    assert isinstance(fb["turn"], int) and fb["turn"] > 0


# ---------------------------------------------------------------------------
# Test 3: Empty fallback chain — turn fails, tokens/credits absent.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_fallback_chain_fails_with_no_cost(tmp_path, monkeypatch):
    """Throttle exhausts budget with NO fallback candidates.

    ``tokens`` and ``credits`` must be ABSENT: nothing was measured.
    """
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner._agent_fallback_chain", lambda: ())
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)

    async def _stream():
        raise _transient_error()
        yield  # noqa: unreachable

    state, slot, client = _state_and_slot(tmp_path, _stream, served_model="some-model")

    await _run_chat(state, slot, "say something")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    closers = _entries_of("turn/completed")
    assert len(closers) >= 1, f"no turn/completed: {[e['type'] for e in entries]}"

    failed = [
        c for c in closers if c["data"].get("error") or c["data"].get("stop_reason") == "failed"
    ]
    if failed:
        data = failed[-1]["data"]
        assert "tokens" not in data, f"failed turn carried tokens: {data}"
        assert "credits" not in data, f"failed turn carried credits: {data}"


# ---------------------------------------------------------------------------
# Test 4: Withheld model pin — _ledger_model returns the session fact.
# ---------------------------------------------------------------------------


def test_withheld_pin_names_served_model_not_configured_pin():
    """_ledger_model returns the model the session RUNS on, not the pin."""

    class _WithheldSlot:
        model = "expensive-pinned-model"
        served_model = ""

    class _PinnedSlot:
        model = "some-model"
        served_model = "some-model"

    class _FallbackSlot:
        model = "primary-model"
        served_model = "fallback-model"

    class _MinimalDouble:
        model = "stub-model"

    assert _ledger_model(_WithheldSlot()) == ""
    assert _ledger_model(_PinnedSlot()) == "some-model"
    assert _ledger_model(_FallbackSlot()) == "fallback-model"
    assert _ledger_model(_MinimalDouble(), "fb") == "fb"


# ---------------------------------------------------------------------------
# Test 5: Repeated fallback swaps — each produces a model/selected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_fallback_swaps_produce_multiple_model_selected(tmp_path, monkeypatch):
    """Multiple fallback candidates: each swap writes its own model/selected.

    Pre-set the counter to TRANSIENT_RETRIES, then have _fallback_swap_for_turn
    return a candidate. The fallback branch rewinds the counter to
    fallback_rewound_transient_budget() and requeues. On the next _run_chat
    invocation (which we simulate by calling _run_chat again with the slot's
    persisted state), the counter climbs back to TRANSIENT_RETRIES and the
    second candidate fires.

    Since the test harness can only drive one _run_chat at a time (the queue
    drain is external), we call _run_chat twice with state that persists on
    the slot between calls.
    """
    candidates = ("fallback-A", "fallback-B", "auto")
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._agent_fallback_chain",
        lambda: candidates,
    )
    monkeypatch.setattr("kiro_crew.dashboard.chat_runner.transient_retry_delay", lambda _a: 0.0)

    async def _fake_advance(client, fb_state, *, surface="", log_suffix=""):
        if fb_state.pos < len(fb_state.chain):
            candidate = fb_state.chain[fb_state.pos]
            fb_state.pos += 1
            return candidate
        return None

    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner.advance_fallback_candidate",
        _fake_advance,
    )

    async def _stream():
        raise _transient_error()
        yield  # noqa: unreachable

    state, slot, client = _state_and_slot(tmp_path, _stream, served_model="primary-model")
    slot._transient_5xx_retries = TRANSIENT_RETRIES
    _set_fallback_attrs(slot, monkeypatch)

    # First call: counter at TRANSIENT_RETRIES → fallback-A selected, requeue.
    await _run_chat(state, slot, "say something")

    # The fallback swap rewound the counter. Push it back up to the threshold
    # so the second call enters the fallback branch again.
    slot._transient_5xx_retries = TRANSIENT_RETRIES
    # Re-add a user message (the slot needs an active message for _run_chat).
    slot.append("user", "retry", "msg msg-u")

    # Second call: counter at TRANSIENT_RETRIES → fallback-B selected.
    await _run_chat(state, slot, "say something again")
    assert emit.flush(timeout=20.0)

    entries = _entries()
    fb_selected = [
        e
        for e in entries
        if e["type"] == "model/selected" and e["data"].get("source") == "fallback"
    ]
    assert len(fb_selected) >= 2, (
        f"expected ≥2 fallback model/selected, got {len(fb_selected)}: "
        f"{[e['data'] for e in fb_selected]}"
    )

    models = [e["data"]["model"] for e in fb_selected]
    assert models[0] != models[1], f"repeated swaps named the same model: {models}"

    turns = [e["data"]["turn"] for e in fb_selected]
    assert all(isinstance(t, int) and t > 0 for t in turns)
    for i in range(1, len(turns)):
        assert turns[i] >= turns[i - 1], f"turn ordinals went backwards: {turns}"


# ---------------------------------------------------------------------------
# Direct emit tests — shape contracts.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_model_selected_shape(tmp_path):
    """on_model_selected writes model/selected with expected fields."""
    emit.on_session_opened(SESSION, agent="test", slot="s", model="m")
    emit.on_model_selected(SESSION, "claude-fallback", "fallback", turn=3)
    assert emit.flush(timeout=10.0)

    selected = _entries_of("model/selected")
    assert len(selected) == 1
    data = selected[0]["data"]
    assert data["model"] == "claude-fallback"
    assert data["source"] == "fallback"
    assert data["turn"] == 3


@pytest.mark.asyncio
async def test_on_model_selected_omits_turn_when_zero(tmp_path):
    """A model pick outside a turn records no turn ordinal."""
    emit.on_session_opened(SESSION, agent="test", slot="s", model="m")
    emit.on_model_selected(SESSION, "some-model", "user_pick")
    assert emit.flush(timeout=10.0)

    selected = _entries_of("model/selected")
    assert len(selected) == 1
    assert "turn" not in selected[0]["data"]


@pytest.mark.asyncio
async def test_turn_failed_has_no_tokens_or_credits(tmp_path):
    """A failed turn carries no tokens or credits — nothing was measured."""
    emit.on_session_opened(SESSION, agent="test", slot="s", model="m")
    emit.on_turn_started(SESSION, 1)
    emit.on_turn_failed(SESSION, 1, error="AcpError", duration_ms=42, model="m")
    assert emit.flush(timeout=10.0)

    closers = _entries_of("turn/completed")
    assert len(closers) == 1
    data = closers[0]["data"]
    assert data["stop_reason"] == "failed"
    assert data["error"] == "AcpError"
    assert "tokens" not in data, f"failed turn carried tokens: {data}"
    assert "credits" not in data, f"failed turn carried credits: {data}"
    assert "duration_ms" in data


@pytest.mark.asyncio
async def test_turn_completed_carries_tokens_and_model(tmp_path):
    """A successful turn's closer carries tokens, credits, and model."""
    emit.on_session_opened(SESSION, agent="test", slot="s", model="m")
    emit.on_turn_started(SESSION, 1)
    emit.on_turn_completed(
        SESSION,
        1,
        input_tokens=100,
        output_tokens=50,
        credits=0.01,
        duration_ms=200,
        stop_reason="end_turn",
        model="claude-sonnet-4",
    )
    assert emit.flush(timeout=10.0)

    closers = _entries_of("turn/completed")
    assert len(closers) == 1
    data = closers[0]["data"]
    assert data["model"] == "claude-sonnet-4"
    assert data["tokens"]["input"] == 100
    assert data["tokens"]["output"] == 50
    assert data["credits"] == 0.01
    assert data["stop_reason"] == "end_turn"
