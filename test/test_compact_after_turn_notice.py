"""Once per turn, a turn that crosses the compaction threshold says so in the chat.

Compaction runs only between turns (``check_context_usage`` at turn end), so a long
agentic turn could run far past ``autocompact_pct`` with nothing said. These pin the
mid-turn notice: at the first settled tool-result boundary after the reading crosses
the threshold, the chat gets ONE "will compact after this turn" row. It is a display
row only: the model is not steered, nothing compacts mid-turn, and the threshold
itself is untouched.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_state

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.chat_runner import (
    COMPACT_AFTER_TURN_NOTICE_KIND,
    _run_chat,
)
from kiro_crew.session import SessionManager

KEY = "dashboard:compact-after-turn"


def _tool_round(tcid: str) -> list[AcpEvent]:
    return [
        AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=tcid, title="fs_read", tool_name="fs_read"),
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id=tcid, tool_output="ok", tool_final=True),
    ]


def _state_and_slot(tmp_path: Path, events: list[AcpEvent], *, due: bool):
    """A slot whose backend streams *events*, with the compaction verdict pinned to *due*."""
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-compact-after-turn"
    client.shutdown = AsyncMock()
    client.supports_refusal_steer = True
    client.steer = AsyncMock(return_value=True)
    client.context_usage_pct = MagicMock(return_value=47.0)

    async def _stream(*_a, **_kw):
        for event in events:
            yield event

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.compaction_due_after_turn = MagicMock(return_value=due)
    state.sessions.effective_autocompact_pct = MagicMock(return_value=40.0)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot = state.get_or_create_slot("compact-after-turn-slot")
    slot.append("user", "do the long task", "msg msg-u")
    return state, slot, client


def _notice_rows(slot: Any) -> list[dict]:
    return [
        m
        for m in slot.messages
        if (m.get("meta") or {}).get("notice") == COMPACT_AFTER_TURN_NOTICE_KIND
    ]


@pytest.mark.asyncio
async def test_past_threshold_notices_once_per_turn(tmp_path):
    """Three tool rounds past the threshold: one row, no steer, compaction left to turn end."""
    events = [
        *_tool_round("tc-1"),
        *_tool_round("tc-2"),
        *_tool_round("tc-3"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state, slot, client = _state_and_slot(tmp_path, events, due=True)

    await _run_chat(state, slot, "do the long task")

    rows = _notice_rows(slot)
    assert len(rows) == 1, f"expected one notice row, got {len(rows)}"
    assert rows[0]["role"] == "assistant"
    assert rows[0]["meta"]["kind"] == "compaction"
    assert rows[0]["content"] == "🔄 Context at 47% (limit 40%) — will compact after this turn."
    # A display row only: the model is not steered and the turn runs on.
    client.steer.assert_not_awaited()
    # The row lands after the tool that crossed, never between a call and its result.
    first_tool = next(i for i, m in enumerate(slot.messages) if m.get("role") == "tool")
    assert slot.messages.index(rows[0]) > first_tool
    # Compaction itself still waits for the turn-end check.
    state.sessions.check_context_usage.assert_called_once()


@pytest.mark.asyncio
async def test_open_streaming_message_is_finalized_before_the_notice(tmp_path):
    """Narration before a tool stays the clients' streaming row until a segment cut.

    Without the cut, the notice's assistant frame would replace that row on screen.
    """
    events = [
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Let me run the tests."),
        *_tool_round("tc-1"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state, slot, _client = _state_and_slot(tmp_path, events, due=True)
    seen: list[tuple[str, int]] = []
    state.broadcast_ws = MagicMock(
        side_effect=lambda name, *_a, **_kw: seen.append((name, len(_notice_rows(slot))))
    )

    await _run_chat(state, slot, "do the long task")

    assert len(_notice_rows(slot)) == 1
    cuts_before_notice = [n for n, rows in seen if n == "chat_segment" and rows == 0]
    assert cuts_before_notice, f"no chat_segment before the notice row: {seen}"


@pytest.mark.asyncio
async def test_below_threshold_says_nothing(tmp_path):
    events = [
        *_tool_round("tc-1"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state, slot, client = _state_and_slot(tmp_path, events, due=False)

    await _run_chat(state, slot, "do the long task")

    assert _notice_rows(slot) == []
    client.steer.assert_not_awaited()


@pytest.mark.asyncio
async def test_each_turn_gets_its_own_notice(tmp_path):
    """Once per TURN, not once per session: a second turn past the threshold says it again."""
    events = [
        *_tool_round("tc-1"),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state, slot, client = _state_and_slot(tmp_path, events, due=True)

    await _run_chat(state, slot, "first")
    slot.append("user", "second", "msg msg-u")
    await _run_chat(state, slot, "second")

    assert len(_notice_rows(slot)) == 2
    client.steer.assert_not_awaited()


# ── The verdict itself: read-only, and only where Crew would really compact ──


@contextlib.asynccontextmanager
async def _managed(provider: Any):
    cfg = KiroCrewConfig()
    cfg.session.timeout_secs = 2
    cfg.session.autocompact_pct = 40.0
    mgr = SessionManager(cfg, provider_factory=lambda *_a, **_kw: provider)
    try:
        await mgr.get_or_create(KEY)
        mgr.release(KEY)
        yield mgr
    finally:
        await mgr.close_all()


def _provider(pct: float, *, unknown: bool = False, unsupported: Any = None) -> Any:
    m = AsyncMock()
    m.start = AsyncMock()
    m.shutdown = AsyncMock()
    m.is_process_alive = lambda: True
    m.has_active_turn = lambda: False
    m.context_usage_pct = lambda: pct
    m.context_usage_unknown = lambda: unknown
    m.manual_compact_unsupported_backend = unsupported
    m.harness_managed_compaction_backend = None
    m.compaction_unmanaged_backend = None
    m.is_claude_code = False
    return m


@pytest.mark.asyncio
async def test_due_only_at_or_past_the_threshold():
    async with _managed(_provider(39.0)) as mgr:
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False
    async with _managed(_provider(40.0)) as mgr:
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is True


@pytest.mark.asyncio
async def test_not_due_where_crew_would_not_compact():
    async with _managed(_provider(90.0, unknown=True)) as mgr:
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False
    async with _managed(_provider(90.0, unsupported="kas")) as mgr:
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False
    async with _managed(_provider(90.0)) as mgr:
        mgr._compaction.state.compacting.add(KEY)
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False
        mgr._compaction.state.compacting.discard(KEY)
        mgr._compact_cooldown_until[KEY] = 10**12
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False


@pytest.mark.asyncio
async def test_a_pending_verdict_promises_nothing():
    """The gate settles a pending verdict first and may arm a cooldown from it."""
    async with _managed(_provider(90.0)) as mgr:
        mgr._compaction.state.pending_verdict[KEY] = 90.0
        assert mgr.compaction_due_after_turn(KEY, mgr._sessions[KEY].provider) is False
        # Read-only: the verdict is still waiting for the real gate.
        assert KEY in mgr._compaction.state.pending_verdict


@pytest.mark.asyncio
async def test_the_recycle_arm_is_not_called_a_compaction():
    """The gate lets an unmanaged, uncompactable backend through to a RESTART."""
    provider = _provider(90.0, unsupported="opaque")
    provider.compaction_unmanaged_backend = "opaque"
    async with _managed(provider) as mgr:
        assert mgr._compaction._gate_rung(KEY, provider, 90.0, log=False) is None
        assert mgr.compaction_due_after_turn(KEY, provider) is False


@pytest.mark.parametrize(
    ("pct", "unknown", "unsupported", "compacting", "cooldown"),
    [
        (39.0, False, None, False, False),
        (40.0, False, None, False, False),
        (90.0, True, None, False, False),
        (90.0, False, "kas", False, False),
        (90.0, False, None, True, False),
        (90.0, False, None, False, True),
    ],
)
@pytest.mark.asyncio
async def test_agrees_with_the_turn_end_gate(pct, unknown, unsupported, compacting, cooldown):
    """One ladder: mid-turn "due" is exactly the turn-end gate passing (no pending verdict)."""
    provider = _provider(pct, unknown=unknown, unsupported=unsupported)
    async with _managed(provider) as mgr:
        if compacting:
            mgr._compaction.state.compacting.add(KEY)
        if cooldown:
            mgr._compact_cooldown_until[KEY] = 10**12
        due = mgr.compaction_due_after_turn(KEY, provider)
        gate = mgr._compaction_gate_decision(KEY, provider, pct)
        assert due is (gate is None)
        mgr._compaction.state.compacting.discard(KEY)


@pytest.mark.asyncio
async def test_asking_changes_nothing():
    """Read-only: no compaction scheduled, nothing marked compacting."""
    async with _managed(_provider(90.0)) as mgr:
        provider = mgr._sessions[KEY].provider
        assert mgr.compaction_due_after_turn(KEY, provider) is True
        assert KEY not in mgr._compaction.state.compacting
        assert not mgr._compaction.state.compaction_tasks
        provider.stream_command.assert_not_called()
