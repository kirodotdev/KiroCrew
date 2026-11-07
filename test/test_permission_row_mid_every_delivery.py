"""One pending native approval carries the same ``mid`` on every delivery path.

A chat runner's request is decided by its slot and the ``mid`` of the
permission row the runner wrote for it, and the dashboard client has no
bare-id fallback: a card that arrives without that ``mid`` cannot be decided
anywhere. So every path that hands a pending permission row to the SPA must
carry the ``mid`` the slot's approval registry answers for it
(``approval_instance``):

- the live ``chat_message`` frame the append broadcasts;
- the slot-detail rebuild (``_prepare_messages``), which serves a reload, a
  second tab and a popped-out window, since they all read the slot's rows
  through the same route;
- the SSE stream chunk (``_build_stream_chunk``);
- the slots push (``pending_approval_info.request_mid``), which the composer
  bar and the Needs Approval lane read.

A transcript restored from history after the slot is gone cannot carry a live
request: its future does not survive, so ``approval_instance`` answers nothing
and no path offers a decide.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.chat_utils import _build_stream_chunk, _prepare_messages
from kiro_crew.dashboard.state import DashboardState, row_mid
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state


@pytest.mark.asyncio
async def test_a_pending_native_row_names_its_mid_on_every_delivery_path(tmp_path):
    state = _make_state(tmp_path)
    frames: list[dict] = []
    state._broadcast = frames.append  # the live chat_message egress
    slot = state.get_or_create_slot("parent")
    row = slot.append(
        "permission", "shell", json.dumps({"request_id": "req-1", "tool_call_id": "tc-1"})
    )
    future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    slot.register_approval("req-1", future, row)

    expected = slot.approval_instance("req-1")
    assert expected and expected == row_mid(row)

    # Live frame.
    (live,) = [
        f for f in frames if f.get("_type") == "chat_message" and f.get("role") == "permission"
    ]
    assert live["meta"]["mid"] == expected
    assert live["meta"]["approval_id"] == "req-1"

    # Slot detail (reload, second tab, popped-out window).
    permission_rows = [m for m in slot.messages if m.get("role") == "permission"]
    (detail,) = _prepare_messages(permission_rows, True, live_child="")
    assert detail["meta"]["mid"] == expected

    # SSE stream chunk.
    chunk = json.loads(_build_stream_chunk(row))
    assert chunk["meta"]["mid"] == expected

    # Slots push.
    info = slot.to_dict()["pending_approval_info"]
    assert info["origin"] == "native"
    assert info["request_id"] == "req-1"
    assert info["request_mid"] == expected

    future.cancel()
