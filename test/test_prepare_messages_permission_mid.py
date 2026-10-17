"""A permission row keeps its delivery id (`meta.mid`) on the wire.

The runner writes a permission row's request data into `cls`;
`_ChatSlot.append` stamps the row identity into `meta` as `{"mid": ...}`, and
`register_approval` binds the pending request to that mid. `_prepare_messages`
replaces `meta` with the parsed `cls` dict, so without a carry the mid never
reaches a client, and a client that must answer by row identity (the crew
window's strict native approve) can answer nothing.
"""

from __future__ import annotations

import json

from kiro_crew.dashboard.chat_utils import _prepare_messages
from kiro_crew.dashboard.state import _ChatSlot


def _permission_slot() -> tuple[_ChatSlot, str]:
    slot = _ChatSlot("k1", "Build fix")
    cls = json.dumps(
        {"request_id": "7", "approval_id": "7", "tool_call_id": "tc", "tool_input": "ls"}
    )
    slot.append("permission", "shell", cls, broadcast=False)
    row = slot.messages[-1]
    mid = row["meta"]["mid"]
    assert isinstance(mid, str) and mid
    return slot, mid


def test_permission_row_keeps_its_stored_mid_when_cls_replaces_meta() -> None:
    slot, mid = _permission_slot()
    [out] = _prepare_messages(list(slot.messages), running=True, live_child="")
    assert out["meta"]["mid"] == mid
    # The cls fields are still the ones that reach the client.
    assert out["meta"]["approval_id"] == "7"
    assert out["meta"]["tool_input"] == "ls"


def test_a_mid_inside_cls_is_not_overwritten_by_the_stored_one() -> None:
    slot, _mid = _permission_slot()
    row = dict(slot.messages[-1])
    row["cls"] = json.dumps({"approval_id": "7", "mid": "from-cls"})
    [out] = _prepare_messages([row], running=True, live_child="")
    assert out["meta"]["mid"] == "from-cls"


def test_a_row_with_no_stored_mid_gains_none() -> None:
    row = {
        "role": "permission",
        "content": "shell",
        "cls": json.dumps({"approval_id": "7"}),
        "ts": "t",
    }
    [out] = _prepare_messages([row], running=True, live_child="")
    assert "mid" not in out["meta"]


def test_a_non_permission_row_keeps_its_wire_meta_unchanged() -> None:
    slot = _ChatSlot("k1", "Build fix")
    slot.append("tool", "🔧 ls", json.dumps({"tool_call_id": "tc"}), broadcast=False)
    [out] = _prepare_messages(list(slot.messages), running=True, live_child="")
    assert "mid" not in out["meta"]
