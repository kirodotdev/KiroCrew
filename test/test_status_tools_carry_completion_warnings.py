"""``workflow_status`` and ``spawn_status`` report the warnings the completion
message carries for the same run.

A run checked later through a status tool must not read as a clean success when
its completion message said otherwise: failed agent calls / preserved partials
for a workflow, ``made_no_tool_calls`` / ``leaked_tool_call`` for a sub-agent.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew import mcp_core
from kiro_crew.constants import LEAKED_TOOL_CALL_NOTE, NO_TOOL_CALLS_NOTE
from kiro_crew.mcp_core import _call_tool_inner


def _call(tool: str, args: dict[str, Any], payload: dict[str, Any]) -> str:
    with (
        patch.object(mcp_core, "_get", return_value=payload),
        patch.object(mcp_core, "_resolve_session_key", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "_resolve_session_key_strict", return_value="dashboard:chat-1"),
        patch.object(mcp_core, "sel", MagicMock()),
    ):
        return _call_tool_inner(tool, args)


def test_workflow_status_reports_failed_calls_and_partials() -> None:
    snap = {
        "run_id": "r1",
        "status": "finished",
        "event_count": 9,
        "agent_error_count": 4,
        "partial_result_count": 2,
    }
    out = _call("workflow_status", {"run_id": "r1"}, snap)
    assert "4 agent call(s) failed" in out
    assert "`agent_errors`" in out
    assert "2 agent result(s) preserved" in out
    assert "`partial_results`" in out


@pytest.mark.parametrize("value", [0, None, True, "4", -1])
def test_workflow_status_clean_line_unchanged(value: Any) -> None:
    snap = {"run_id": "r1", "status": "finished", "event_count": 9}
    baseline = _call("workflow_status", {"run_id": "r1"}, snap)
    noisy = _call(
        "workflow_status",
        {"run_id": "r1"},
        {**snap, "agent_error_count": value, "partial_result_count": value},
    )
    assert noisy == baseline
    assert "failed" not in baseline and "preserved" not in baseline


@pytest.mark.parametrize(
    ("flag", "note"),
    [("made_no_tool_calls", NO_TOOL_CALLS_NOTE), ("leaked_tool_call", LEAKED_TOOL_CALL_NOTE)],
    ids=["no_tool_calls", "leaked_tool_call"],
)
@pytest.mark.parametrize("args", [{}, {"offset": 1, "limit": 5}], ids=["full", "paged"])
def test_spawn_status_carries_completion_note(flag: str, note: str, args: dict) -> None:
    payload: dict[str, Any] = {"done": True, "result": "wrote a.py, commit abc", flag: True}
    if args:
        payload["result_meta"] = {"total_lines": 3, "offset": 1, "returned_lines": 1}
    out = _call("spawn_status", {"agent_id": "a1", **args}, payload)
    line = f"This agent {note}"
    assert line in out
    assert out.index(line) < out.index("wrote a.py, commit abc")


@pytest.mark.parametrize("value", [False, None, "true", 1])
def test_spawn_status_no_note_unless_exactly_true(value: Any) -> None:
    payload = {"done": True, "result": "ok", "made_no_tool_calls": value, "leaked_tool_call": value}
    out = _call("spawn_status", {"agent_id": "a1"}, payload)
    assert "This agent" not in out


def test_spawn_status_running_gets_no_note() -> None:
    payload = {"done": False, "result": "partial", "turns": 1, "made_no_tool_calls": True}
    out = _call("spawn_status", {"agent_id": "a1"}, payload)
    assert "This agent" not in out
