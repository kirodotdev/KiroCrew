"""A content-filter retry cancelled at its consume seam hands the floor back.

The replay's consume gate (``_replay_vetoed_at_consume`` for the content-filter
family) re-checks a Stop, a session rebind and a newer message before the queued
retry runs. When it
cancels, the turn ends without a provider call. The gate only decides; the turn's
exit guard runs its tail, which hands the floor to a newer message or sends the
cycle's one ``chat_done``. That frame must carry the awaited
:func:`chat_done_payload` dict: a bare coroutine is not a JSON payload, and it is
never awaited.
"""

from __future__ import annotations

import inspect
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_runner as cr
from kiro_crew.dashboard.chat_utils import effective_session_key
from kiro_crew.dashboard.recovery_replays import ReplayFamily

_CF = ReplayFamily.CONTENT_FILTER
_CF_REPLAY = frozenset({_CF})


def _state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.context_builder = None
    state.consolidator = None
    state._hook_store = None
    state._yolo = False
    return state


def _arm_recorded_replay(slot: Any, session_key: str = "") -> None:
    """The record a refusal swap leaves for its queued retry."""
    slot._refusal_fallback_primary = "fable-5"
    slot._refusal_fallback_candidate = "opus-test"
    slot._refusal_fallback_attempted = True
    slot._refusal_fallback_session_key = session_key or effective_session_key(slot)
    slot.replays.arm(
        _CF,
        entry_id="q-retry",
        session_key=slot._refusal_fallback_session_key,
        stop_gen=getattr(slot, "_stop_generation", 0),
        session_stop_gen=0,
    )


def _stopped(slot: Any) -> None:
    slot._stop_generation = getattr(slot, "_stop_generation", 0) + 1


def _rebound(slot: Any) -> None:
    # The swap ran under another session than the one the slot is bound to now.
    _arm_recorded_replay(slot, session_key="dash:old-session")


def _superseded(slot: Any) -> None:
    slot._pending_steers = ["a newer message"]


_ABORTS = [
    pytest.param(_stopped, "the turn was stopped.", id="stopped"),
    pytest.param(_rebound, "this chat moved to another session.", id="rebound"),
    pytest.param(_superseded, "your newer message runs instead.", id="superseded"),
]


def _chat_done_payloads(state: Any) -> list[Any]:
    return [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "chat_done"]


async def _assert_one_done_frame(state: Any, slot: Any) -> None:
    payloads = _chat_done_payloads(state)
    assert len(payloads) == 1, f"expected one chat_done frame, got {payloads}"
    payload = payloads[0]
    assert not inspect.iscoroutine(payload), "chat_done carried an un-awaited coroutine"
    assert isinstance(payload, dict)
    assert set(payload) == {"slot", "continuing", "needs_input"}
    assert payload["slot"] == slot.key
    assert payload == await cr.chat_done_payload(state, slot)


@pytest.mark.asyncio
@pytest.mark.parametrize(("abort", "reason"), _ABORTS)
async def test_a_cancelled_replay_is_decided_without_a_broadcast(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, abort: Any, reason: str
) -> None:
    """The gate decides and explains; the exit guard's tail sends the frame."""
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _arm_recorded_replay(slot)
    abort(slot)

    assert await cr._replay_vetoed_at_consume(state, slot, _CF_REPLAY, "after_allowances") is True

    assert _chat_done_payloads(state) == []
    notices = [m["content"] for m in slot.messages if m.get("role") == "notice"]
    assert notices == ["ℹ️ Content-filter retry cancelled — " + reason]
    assert not slot.replays.armed(_CF)


@pytest.mark.asyncio
@pytest.mark.parametrize(("abort", "reason"), _ABORTS[:2])
async def test_run_chat_ends_a_cancelled_replay_with_the_awaited_done_payload(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, abort: Any, reason: str
) -> None:
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = AsyncMock()
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    _arm_recorded_replay(slot)
    abort(slot)

    await cr._run_chat(state, slot, "retry me", _replay=_CF_REPLAY)

    await _assert_one_done_frame(state, slot)
    notices = [m["content"] for m in slot.messages if m.get("role") == "notice"]
    assert notices == ["ℹ️ Content-filter retry cancelled — " + reason]
    state.sessions.get_or_create.assert_not_awaited()
    client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_a_replay_that_may_run_broadcasts_nothing(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    _arm_recorded_replay(slot)

    assert await cr._replay_vetoed_at_consume(state, slot, _CF_REPLAY, "after_allowances") is False

    assert _chat_done_payloads(state) == []
    # An accepted retry keeps its record until the turn settles its episode.
    assert slot.replays.armed(_CF)


@pytest.mark.asyncio
async def test_a_replay_superseded_while_preparing_hands_the_floor_to_the_newer_message(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A newer message queued during session acquisition outranks the replay at
    the dispatch check, and the turn's tail starts that message next."""
    state = _state(tmp_path, monkeypatch)
    slot = state.get_or_create_slot("s1")
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.available_models = MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])

    async def _acquire(*_args: Any, **_kwargs: Any) -> tuple[Any, bool, bool]:
        _superseded(slot)
        return client, True, False

    state.sessions.get_or_create = AsyncMock(side_effect=_acquire)
    _arm_recorded_replay(slot)
    started: list[list[str]] = []

    async def _start_next(_state: Any, _slot: Any, **_kwargs: Any) -> bool:
        started.append([str(q.get("content")) for q in _slot._queue])
        return True

    monkeypatch.setattr(cr, "_start_next_queued_turn", _start_next)

    await cr._run_chat(state, slot, "retry me", _replay=_CF_REPLAY)

    state.sessions.get_or_create.assert_awaited()
    assert started == [["a newer message"]]
    assert _chat_done_payloads(state) == []
    notices = [m["content"] for m in slot.messages if m.get("role") == "notice"]
    assert notices == ["ℹ️ Content-filter retry cancelled — your newer message runs instead."]
    client.stream.assert_not_called()
