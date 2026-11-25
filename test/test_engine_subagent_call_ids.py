"""An engine sub-agent's call ids are held as bounded keys, and still match.

A ``toolCallId`` is backend-authored and bounded on the wire only by the transport
frame cap. The engine card keeps a child's tool calls by id and broadcasts the id;
the roster row keeps a dispatch's id so its approval card can show the task. Each
holds the id as ``liveness_key`` keys it: as it is up to the bound, as its digest
past it, so the same id matches itself and two ids never merge.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS
from kiro_crew.dashboard import engine_subagent_cards as cards

_LONG = "c" * 1_000_000


def _row(call: str, prompt: str) -> dict[str, Any]:
    handle = SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )
    frame = {
        "sessionUpdate": "tool_call",
        "toolCallId": call,
        "title": "Sub-agent: kirocrew-helper",
        "status": "pending",
        "rawInput": {"name": "kirocrew-helper", "prompt": prompt},
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": "sub-" + prompt}},
    }
    [event] = children._handle_kas_subagent(handle, frame) or []
    [row] = event.subagents
    return row


def _turn() -> Any:
    return SimpleNamespace(_engine_children=True, _engine_dispatch_tasks={})


def _request(call: str) -> Any:
    return SimpleNamespace(tool_call_id=call, requesting_subagent="")


def test_a_long_tool_call_id_is_neither_kept_nor_broadcast_whole() -> None:
    info: dict[str, Any] = {}
    frame = cards.child_tool_frame(info, _LONG, "ls")
    assert frame is not None
    assert len(frame["tool_call_id"]) <= TOOL_CALL_CACHE_MAX_KEY_CHARS
    assert all(len(key) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for key in info["tool_calls"])


def test_a_short_tool_call_id_is_kept_and_broadcast_as_sent() -> None:
    info: dict[str, Any] = {}
    frame = cards.child_tool_frame(info, "run_command_toolu_1", "ls")
    assert frame is not None and frame["tool_call_id"] == "run_command_toolu_1"
    assert list(info["tool_calls"]) == ["run_command_toolu_1"]


def test_two_long_ids_that_differ_stay_two_calls_and_each_matches_itself() -> None:
    info: dict[str, Any] = {}
    first = cards.child_tool_frame(info, _LONG + "a", "ls")
    second = cards.child_tool_frame(info, _LONG + "b", "pwd")
    assert first is not None and second is not None
    assert first["tool_call_id"] != second["tool_call_id"]
    assert len(info["tool_calls"]) == 2 and second["tool_count"] == 2
    # The same long id again, renamed: an update of its own entry, not a new call.
    renamed = cards.child_tool_frame(info, _LONG + "a", "ls -la")
    assert renamed is not None and renamed.get("is_update") is True
    assert renamed["tool_call_id"] == first["tool_call_id"] and renamed["tool_count"] == 2


def test_a_tool_call_id_that_is_not_a_string_lists_nothing() -> None:
    info: dict[str, Any] = {}
    assert cards.child_tool_frame(info, 7, "ls") is None  # type: ignore[arg-type]
    assert "tool_count" not in info


def test_a_long_dispatch_id_still_finds_its_task_and_is_kept_bounded() -> None:
    row = _row(_LONG, "the task")
    assert len(row["dispatch_call"]) <= TOOL_CALL_CACHE_MAX_KEY_CHARS
    turn = _turn()
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, [row])
    assert cards.approval_card_fields(turn, _request(_LONG)) == {"subagent_task": "the task"}


def test_two_long_dispatch_ids_with_one_start_show_their_own_tasks() -> None:
    rows = [_row(_LONG + "a", "first"), _row(_LONG + "b", "second")]
    turn = _turn()
    cards.remember_dispatch_tasks(turn._engine_dispatch_tasks, rows)
    assert cards.approval_card_fields(turn, _request(_LONG + "a")) == {"subagent_task": "first"}
    assert cards.approval_card_fields(turn, _request(_LONG + "b")) == {"subagent_task": "second"}
