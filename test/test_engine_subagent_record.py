"""The lasting record of a turn's KAS engine sub-agents (``dashboard.engine_subagent_record``).

The record rides on the reply row the turn wrote, beside ``turn_stats``, so a reopened
chat can say which engine children ran: each one's agent, task, how it ended and the
titles of its tool calls, as the engine reported them. A turn with no engine card
stores nothing new.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import kiro_crew.dashboard.chat_runner as cr
from kiro_crew.acp import session_handle_native_children as children
from kiro_crew.dashboard import engine_subagent_cards as cards
from kiro_crew.dashboard import engine_subagent_record as record

SUBTASK = "31c14746-6447-4300-8ee9-074e74540ae3"
CALL = "invoke_subagent_toolu_bdrk_019Hyz9XEeDzAmxkRpDxDj4q"
PROMPT = "Run this exact shell command: sleep 20; echo one"
RAW_INPUT = {"name": "kirocrew-helper", "prompt": PROMPT}


def _dispatch(status: str, **more: Any) -> dict[str, Any]:
    frame: dict[str, Any] = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": CALL,
        "title": "Sub-agent: kirocrew-helper",
        "status": status,
        "rawInput": RAW_INPUT,
        "_meta": {"kiro": {"kind": "agent-subtask", "agentSubtaskId": SUBTASK}},
    }
    frame.update(more)
    return frame


def _rows(frame: dict[str, Any]) -> list[dict[str, Any]]:
    handle = SimpleNamespace(
        _prompt_or_tool_seen=False, _kas_subagent_roster={}, _note_native_child=lambda sid: True
    )
    [event] = children._handle_kas_subagent(handle, frame) or []
    return event.subagents


def _slot() -> Any:
    slot = MagicMock()
    slot.key = "slot-1"
    slot._native_subagent_tracker = {}
    slot._native_subagent_output = {}
    return slot


def _sync(slot: Any, frame: dict[str, Any], *, engine: bool = True) -> None:
    state = MagicMock()
    state._native_cards = {}
    rows = _rows(frame)
    if engine:
        rows = list(cards.engine_rows(rows, slot._native_subagent_tracker))
    cr._native_subagent_sync(
        state, slot, rows, slot._native_subagent_tracker, slot._native_subagent_output
    )


def _info(slot: Any) -> dict[str, Any]:
    [info] = slot._native_subagent_tracker.values()
    return info


def test_a_completed_child_is_recorded_with_its_task_and_tool_titles() -> None:
    slot = _slot()
    _sync(slot, _dispatch("in_progress"))
    info = _info(slot)
    cards.child_tool_frame(info, "c1", "Run sleep then echo")
    cards.child_tool_frame(info, "c2", "Read result.txt")
    _sync(slot, _dispatch("completed", rawOutput="one"))
    assert record.engine_record(slot._native_subagent_tracker) == {
        "children": [
            {
                "agent": "kirocrew-helper",
                "task": PROMPT,
                "end": "completed",
                "tools": ["Run sleep then echo", "Read result.txt"],
                "tool_count": 2,
            }
        ]
    }


def test_a_failed_child_keeps_the_engines_own_reason() -> None:
    slot = _slot()
    _sync(slot, _dispatch("in_progress"))
    _sync(slot, _dispatch("failed", rawOutput="Sub-agent execution was cancelled"))
    [entry] = record.engine_record(slot._native_subagent_tracker)["children"]
    assert entry["end"] == "failed"
    assert entry["reason"] == "Sub-agent execution was cancelled"


def test_a_child_the_turn_closed_before_the_engine_said_how_it_ended_is_not_reported() -> None:
    slot = _slot()
    _sync(slot, _dispatch("in_progress"))
    cr._native_subagent_close_all(
        MagicMock(),
        slot,
        slot._native_subagent_tracker,
        {},
        error="interrupted (parent turn ended)",
    )
    [entry] = record.engine_record(slot._native_subagent_tracker)["children"]
    # Kiro Crew's own words for the close are not the engine's, so no reason is kept.
    assert entry["end"] == "not_reported"
    assert "reason" not in entry


def test_a_turn_without_engine_cards_records_nothing() -> None:
    slot = _slot()
    _sync(slot, _dispatch("in_progress"), engine=False)
    _sync(slot, _dispatch("completed"), engine=False)
    assert slot._native_subagent_tracker
    assert record.engine_record(slot._native_subagent_tracker) is None
    row: dict[str, Any] = {"role": "assistant", "content": "done", "meta": {}}
    assert record.attach_engine_record(slot, row) is False
    assert row == {"role": "assistant", "content": "done", "meta": {}}


def test_the_record_is_bounded() -> None:
    tracker: dict[str, dict[str, Any]] = {}
    for n in range(record.RECORD_CHILDREN_MAX + 3):
        tracker[str(n)] = {
            "id": f"native:{n}",
            "engine": True,
            "agent": "a",
            "task": "t" * 2000,
            "engine_end": "completed",
            "tool_calls": {f"c{i}": f"tool {i}" for i in range(record.RECORD_TOOLS_MAX + 5)},
            "tool_count": 70,
        }
    got = record.engine_record(tracker)
    assert len(got["children"]) == record.RECORD_CHILDREN_MAX
    assert got["more"] == 3
    entry = got["children"][0]
    assert len(entry["task"]) == record.RECORD_TASK_MAX
    # The newest titles are kept; the count says how many the engine reported.
    assert entry["tools"][-1] == f"tool {record.RECORD_TOOLS_MAX + 4}"
    assert len(entry["tools"]) == record.RECORD_TOOLS_MAX
    assert entry["tool_count"] == 70


def test_a_credential_in_the_task_or_the_reason_stays_redacted_in_the_record() -> None:
    from kiro_crew.testing.synthetic_credentials import github_classic

    token = github_classic(seed=7)
    slot = _slot()
    leaky = {"name": "kirocrew-helper", "prompt": f"push with {token} now"}
    _sync(slot, _dispatch("in_progress", rawInput=leaky))
    _sync(slot, _dispatch("failed", rawInput=leaky, rawOutput=f"refused {token}"))
    got = record.engine_record(slot._native_subagent_tracker)
    assert got["children"][0]["end"] == "failed"
    assert token not in repr(got)


def test_the_footer_attach_puts_the_record_on_the_turns_reply_row() -> None:
    slot = _slot()
    slot.messages = [{"role": "assistant", "content": "answer", "meta": {"mid": "m1"}}]
    slot._turn_reply_mids = ["m1"]
    _sync(slot, _dispatch("in_progress"))
    _sync(slot, _dispatch("completed"))
    assert cr._attach_turn_stats(slot, 1200, 0.5, 0.0, turn_boundary=0) is True
    meta = slot.messages[0]["meta"]
    assert meta["turn_stats"]["elapsed_ms"] == 1200
    assert meta[record.RECORD_META_KEY]["children"][0]["end"] == "completed"


def test_the_footer_attach_of_a_turn_without_engine_cards_stores_nothing_new() -> None:
    slot = _slot()
    slot.messages = [{"role": "assistant", "content": "answer", "meta": {"mid": "m1"}}]
    slot._turn_reply_mids = ["m1"]
    _sync(slot, _dispatch("in_progress"), engine=False)
    assert cr._attach_turn_stats(slot, 1200, 0.5, 0.0, turn_boundary=0) is True
    assert set(slot.messages[0]["meta"]) == {"mid", "turn_stats"}
