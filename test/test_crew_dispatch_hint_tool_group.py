"""The Crew Mode dispatch hint lands inside the conductor's open tool group.

``session_create`` writes the hint while its own tool row is still open, so the row
sits between tool rows. The turn's tool-group walks must read past it like a ``card``
row: the post-tool fallback still closes a tool that sent no result, the segment flush
keeps the hint in the window, and ``_tool_rows_settled`` still sees the open tool.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
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
from kiro_crew.dashboard.chat_runner import _run_chat
from kiro_crew.dashboard.chat_turn.recovery import _tool_rows_settled
from kiro_crew.dashboard.crew_dispatch_notice import CREW_DISPATCH_HINT_KIND, is_crew_dispatch_hint
from kiro_crew.dashboard.state import append_and_surface


def _state_and_slot(tmp_path: Path, script: list[Any]):
    """A slot whose backend runs *script*: an AcpEvent is yielded, a callable is run."""
    state = _make_state(tmp_path)
    client = MagicMock()
    client.session_id = "acp-crew-hint-tool-group"
    client.shutdown = AsyncMock()
    client.supports_refusal_steer = True
    client.steer = AsyncMock(return_value=True)
    client.context_usage_pct = MagicMock(return_value=1.0)
    slot = state.get_or_create_slot("crew-hint-tool-group-slot")

    async def _stream(*_a, **_kw):
        for step in script:
            if callable(step):
                step(state, slot)
            else:
                yield step

    client.stream = _stream
    client.stream_command = _stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, False, False))
    state.sessions.release = MagicMock()
    state.sessions.reset = AsyncMock()
    state.sessions.set_approval_policy = MagicMock()
    state.sessions.check_context_usage = MagicMock()
    state.sessions.compaction_due_after_turn = MagicMock(return_value=False)
    state.sessions.get_slack_link = MagicMock(return_value=(None, None))
    state.sessions.record_failure = AsyncMock()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    state.is_yolo_active = MagicMock(return_value=False)
    state._background_tasks = set()
    slot.append("user", "split this in two", "msg msg-u")
    return state, slot


def _write_hint(state: Any, slot: Any) -> None:
    append_and_surface(
        state,
        slot,
        "notice",
        "Crew started work.",
        "msg msg-info",
        meta={"kind": CREW_DISPATCH_HINT_KIND},
    )


def _tool_row(slot: Any, tcid: str) -> dict:
    return next(
        m
        for m in slot.messages
        if m.get("role") == "tool" and (m.get("meta") or {}).get("tool_call_id") == tcid
    )


@pytest.mark.asyncio
async def test_a_tool_with_no_result_is_still_closed_past_the_hint(tmp_path):
    """An earlier call that sent no result frame is closed when the model's text arrives."""
    script = [
        AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id="tc-quiet", title="fs_read", tool_name="fs_read"
        ),
        AcpEvent(
            kind=EVENT_TOOL_CALL,
            tool_call_id="tc-create",
            title="session_create",
            tool_name="session_create",
        ),
        _write_hint,
        AcpEvent(
            kind=EVENT_TOOL_RESULT, tool_call_id="tc-create", tool_output="ok", tool_final=True
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="A crew member is on it."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    state, slot = _state_and_slot(tmp_path, script)

    await _run_chat(state, slot, "split this in two")

    assert (_tool_row(slot, "tc-quiet").get("meta") or {}).get("done") is True
    # The segment flush kept the hint in the window, after the call that wrote it.
    hints = [m for m in slot.messages if is_crew_dispatch_hint(m)]
    assert len(hints) == 1
    assert slot.messages.index(hints[0]) > slot.messages.index(_tool_row(slot, "tc-create"))


def test_tool_rows_settled_reads_past_the_hint():
    open_tool = {"role": "tool", "content": "🔧 fs_read", "meta": {"tool_call_id": "a"}}
    hint = {"role": "notice", "content": "x", "meta": {"kind": CREW_DISPATCH_HINT_KIND}}
    done_tool = {
        "role": "tool",
        "content": "🔧 fs_read",
        "meta": {"tool_call_id": "b", "done": True},
    }
    assert _tool_rows_settled(SimpleNamespace(messages=[open_tool, hint])) is False
    assert _tool_rows_settled(SimpleNamespace(messages=[done_tool, hint])) is True
    # Any other notice still ends the trailing run, as before.
    other = {"role": "notice", "content": "x", "meta": {"kind": "arm_refusal"}}
    assert _tool_rows_settled(SimpleNamespace(messages=[open_tool, other])) is True


def test_only_a_notice_with_the_kind_is_the_hint():
    assert is_crew_dispatch_hint({"role": "notice", "meta": {"kind": CREW_DISPATCH_HINT_KIND}})
    assert not is_crew_dispatch_hint(
        {"role": "assistant", "meta": {"kind": CREW_DISPATCH_HINT_KIND}}
    )
    assert not is_crew_dispatch_hint({"role": "notice", "meta": {"kind": "other"}})
    assert not is_crew_dispatch_hint({"role": "notice"})
