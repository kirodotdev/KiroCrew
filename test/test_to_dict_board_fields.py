"""Tests for to_dict() Board fields: options, waiting_for_input, pending_approval_info, last_activity_ts."""
import asyncio
import json

from kiro_crew.dashboard.state import _ChatSlot


def _slot(*messages: dict) -> _ChatSlot:
    s = _ChatSlot("test-slot")
    for m in messages:
        s.messages.append(m)
    return s


def test_options_from_assistant():
    s = _slot({"role": "assistant", "content": "Pick one.\n[OPTIONS: A | B | C]", "ts": "t1"})
    d = s.to_dict()
    assert d["has_options"] is True
    assert d["options"] == ["A", "B", "C"]
    assert "[OPTIONS:" not in d["prompt_preview"]


def test_no_options_when_user_last():
    s = _slot(
        {"role": "assistant", "content": "Here you go.\n[OPTIONS: X | Y]", "ts": "t1"},
        {"role": "user", "content": "X", "ts": "t2"},
    )
    d = s.to_dict()
    assert d["has_options"] is False
    assert d["options"] == []


def test_waiting_for_input_assistant_last():
    s = _slot({"role": "assistant", "content": "Done. What next?", "ts": "t1"})
    # task=None means not running
    d = s.to_dict()
    assert d["waiting_for_input"] is True


def test_not_waiting_when_user_last():
    s = _slot(
        {"role": "assistant", "content": "Done.", "ts": "t1"},
        {"role": "user", "content": "Thanks", "ts": "t2"},
    )
    d = s.to_dict()
    assert d["waiting_for_input"] is False


def test_not_waiting_when_running():
    s = _slot({"role": "assistant", "content": "Working on it.", "ts": "t1"})
    loop = asyncio.new_event_loop()
    # Create a non-done future to simulate running
    s.task = loop.create_future()
    d = s.to_dict()
    assert d["waiting_for_input"] is False
    loop.close()


def test_pending_approval_info():
    meta = json.dumps({"tool_input": "ls -la", "tool_kind": "bash", "request_id": "r1"})
    s = _slot({"role": "permission", "content": "shell", "cls": meta, "ts": "t1"})
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    s._approval_futures["r1"] = fut
    d = s.to_dict()
    assert d["pending_approval"] is True
    assert d["pending_approval_info"]["tool"] == "shell"
    assert d["pending_approval_info"]["request_id"] == "r1"
    loop.close()


def test_pending_approval_skips_resolved():
    old_meta = json.dumps({"resolved": True, "request_id": "r0"})
    new_meta = json.dumps({"tool_input": "cat foo", "request_id": "r2"})
    s = _slot(
        {"role": "permission", "content": "old_tool", "cls": old_meta, "ts": "t1"},
        {"role": "permission", "content": "new_tool", "cls": new_meta, "ts": "t2"},
    )
    loop = asyncio.new_event_loop()
    fut = loop.create_future()
    s._approval_futures["r2"] = fut
    d = s.to_dict()
    assert d["pending_approval_info"]["tool"] == "new_tool"
    assert d["pending_approval_info"]["request_id"] == "r2"
    loop.close()


def test_last_activity_ts_from_tool_call():
    s = _slot(
        {"role": "assistant", "content": "Let me check.", "ts": "t1"},
        {"role": "tool_call", "content": "grep ...", "ts": "t2"},
        {"role": "tool_result", "content": "found", "ts": "t3"},
        {"role": "assistant", "content": "Here's what I found.", "ts": "t4"},
    )
    d = s.to_dict()
    assert d["last_activity_ts"] == "t4"


def test_prompt_preview_truncation():
    long_text = "x" * 300 + "\n[OPTIONS: A | B]"
    s = _slot({"role": "assistant", "content": long_text, "ts": "t1"})
    d = s.to_dict()
    assert len(d["prompt_preview"]) == 241  # 240 + "…"
    assert d["prompt_preview"].endswith("…")


def test_queue_depth_zero_when_no_queue():
    s = _slot({"role": "assistant", "content": "Done. What next?", "ts": "t1"})
    d = s.to_dict()
    assert d["queue_depth"] == 0


def test_queue_depth_reflects_queued_prompts():
    # A finished turn (assistant last) with prompts queued behind it: the Board
    # must see queue_depth so it can show the session in Working, not Your Turn.
    s = _slot({"role": "assistant", "content": "Done.", "ts": "t1"})
    s.queue_append("next prompt")
    s.queue_append("and another")
    d = s.to_dict()
    assert d["queue_depth"] == 2
