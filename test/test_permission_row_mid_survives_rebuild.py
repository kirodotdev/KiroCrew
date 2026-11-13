"""A permission row keeps its delivery identity (``mid``) on every read path.

A chat runner's approval is decided by the slot and the ``mid`` of the
permission row the runner wrote for it. The row's structured fields live in its
JSON ``cls`` and its ``mid`` in the stored ``meta``; the live ``chat_message``
frame merges the two. The slot-detail rebuild (a reload) and the SSE stream
chunk rebuilt meta from ``cls`` alone, so a card rendered from either named no
request and refused its own presses while the runner waited.
"""

from __future__ import annotations

import json

from kiro_crew.dashboard.chat_utils import _build_stream_chunk, _prepare_messages
from kiro_crew.dashboard.state import _ChatSlot, row_mid


def _permission_row() -> tuple[_ChatSlot, dict]:
    slot = _ChatSlot("parent")
    row = slot.append(
        "permission", "shell", json.dumps({"request_id": "req-1", "tool_call_id": "tc-1"})
    )
    return slot, row


def test_a_reloaded_permission_row_carries_its_mid():
    slot, row = _permission_row()
    mid = row_mid(row)
    assert mid
    (out,) = _prepare_messages(list(slot.messages), True, live_child="")
    assert out["meta"]["approval_id"] == "req-1"
    assert out["meta"]["mid"] == mid


def test_a_streamed_permission_row_carries_its_mid():
    _slot, row = _permission_row()
    chunk = json.loads(_build_stream_chunk(row))
    assert chunk["meta"]["mid"] == row_mid(row)


def test_a_row_without_a_stored_mid_gains_none():
    row = {"role": "permission", "content": "shell", "cls": json.dumps({"request_id": "r"})}
    (out,) = _prepare_messages([row], True, live_child="")
    assert "mid" not in out["meta"]
