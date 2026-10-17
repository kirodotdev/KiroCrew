"""The live ``tool_result`` frame says whether the call has RETURNED.

A shell's streamed stdout reaches the runner as status-less, non-final
``EVENT_TOOL_RESULT`` frames while the command still runs. The crewmate chat's
one live status line (``useSlotActivity``) reads a returned call as "the model
is thinking again", so a frame that cannot be told apart from the final result
flipped a long build's line to "Thinking" at its first line of output, and the
crewmate looked stuck. ``final`` is what lets the client tell them apart.
"""

from __future__ import annotations

import pytest
from test_nothing_to_do_directive import _drive_turn, _stub_state

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)


def _frames(state) -> list[dict]:
    return [
        c.args[1]
        for c in state.broadcast_ws.call_args_list
        if c.args and c.args[0] == "tool_result"
    ]


@pytest.mark.asyncio
async def test_streamed_output_is_not_final_and_the_completed_frame_is(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("tool-frame-build")
    slot._titled = True
    events = [
        AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id="tc-build", title="Running: make test", is_shell=True
        ),
        # Mid-stream stdout: no status, not final.
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-build", tool_output="collecting"),
        AcpEvent(
            kind=EVENT_TOOL_RESULT,
            tool_call_id="tc-build",
            tool_output="12 passed",
            tool_final=True,
            tool_status="completed",
        ),
        AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id="tc-fail", title="Running: false", is_shell=True
        ),
        # A FAILED call has returned too, though `tool_final` is for success only.
        AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="tc-fail", tool_status="failed"),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive_turn(state, slot, events, monkeypatch, self_wake=False)
    by_id = [(f["tool_call_id"], f["final"]) for f in _frames(state)]
    assert by_id == [("tc-build", False), ("tc-build", True), ("tc-fail", True)], by_id


@pytest.mark.asyncio
async def test_the_text_after_tools_sweep_closes_calls_as_final(tmp_path, monkeypatch):
    state = _stub_state(tmp_path)
    slot = state.get_or_create_slot("tool-frame-sweep")
    slot._titled = True
    events = [
        AcpEvent(
            kind=EVENT_TOOL_CALL, tool_call_id="tc-quiet", title="Running: true", is_shell=True
        ),
        AcpEvent(kind=EVENT_TEXT_CHUNK, text="Done."),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn"),
    ]
    await _drive_turn(state, slot, events, monkeypatch, self_wake=False)
    swept = [f for f in _frames(state) if f["tool_call_id"] == "tc-quiet"]
    assert swept and all(f["final"] is True for f in swept), swept
