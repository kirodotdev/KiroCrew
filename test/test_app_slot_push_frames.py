"""The dashboard's app slot-push filter grants no more than the gateway's own event gate.

``website/src/utils/appSlotPush.ts`` signals an app embed when a frame
from the SLOT_CHANGE_FRAMES set arrives for a slot that app owns. That claim is
only safe while every name in the set is a slot-scoped event here, outside the
subagent family, so the gateway itself admits it to an app token for its own
slot with no ``slots:*`` declaration and refuses it for a slot another app or a
person owns. These tests read the set from the client source and check each
name against ``ws_event_scope``, and check that every other slot-scoped event
is named as one that leaves the embed's read unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import ws_event_scoping_helpers
from ws_event_scoping_helpers import _allowed, _make_slot, _make_state

from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.dashboard.ws_event_scope import (
    _SLOT_SCOPED_EVENTS,
    _SUBAGENT_EVENTS,
    ws_event_allowed,
)

_clear_module_caches = ws_event_scoping_helpers._clear_module_caches
_clear_ws_scope_module_caches = ws_event_scoping_helpers._clear_ws_scope_module_caches

_PUSH_SOURCE = Path(__file__).resolve().parents[1] / "website" / "src" / "utils" / "appSlotPush.ts"


def _set_literal(name: str) -> list[str]:
    source = _PUSH_SOURCE.read_text(encoding="utf-8")
    match = re.search(rf"const {name}[^=]*= new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, f"{name} literal not found in appSlotPush.ts"
    names = re.findall(r"'([^']+)'", match.group(1))
    assert names, f"{name} is empty"
    return names


def _push_frames() -> list[str]:
    return _set_literal("SLOT_CHANGE_FRAMES")


def _key_frames() -> list[str]:
    return _set_literal("KEY_SLOT_FRAMES")


def test_every_push_frame_is_slot_scoped_and_not_subagent():
    frames = _push_frames()
    assert sorted(set(frames) - _SLOT_SCOPED_EVENTS) == []
    assert sorted(set(frames) & _SUBAGENT_EVENTS) == []


def test_title_frame_is_signalled_and_read_from_key():
    # The embed header shows the slot read's title, so a rename must signal.
    assert "slot_title" in _push_frames()
    assert _key_frames() == ["slot_title"]
    assert set(_key_frames()) <= set(_push_frames())


def test_resolved_elicitation_is_signalled():
    # It stamps the card's transcript row resolved, which the read returns.
    assert "elicitation_resolved" in _push_frames()


#: Slot-scoped frames that leave the embed's slot read (rows, running and stop
#: state, turn, title, queue) as it was, each with the reason. A frame that does
#: change that read belongs in SLOT_CHANGE_FRAMES instead.
_READ_UNCHANGED = {
    # Sidecar channels: replies never enter ``slot.messages`` (ws.py
    # ``broadcast_side_result``, chat_threads.py module docstring).
    "chat.side_result": "side chat is ephemeral and kept out of the transcript",
    "chat.thread_reply": "thread replies live in a sidecar, not slot.messages",
    # The row each of these writes reaches clients as its own chat_message.
    "slot_agent_switch": "the read carries no agent; the reply row is a chat_message",
    "workflow_result_injected": "the injected row is surfaced as a chat_message",
    # Panels and liveness the read does not carry.
    "heartbeat": "turn liveness only",
    "context_usage": "context meter",
    "todo_update": "todo panel",
    "mcp_report_update": "MCP session panel",
    "activity_event": "status line text",
    "session_summary": "sidebar intent summary",
    "voice_chunk": "voice playback",
    "voice_complete": "voice playback",
    "voice_error": "voice playback",
    "autonudge_state": "autonudge chip",
    "spawn_batch_started": "subagent batch progress",
    "batch_finished": "subagent batch progress",
    # The task-runner refine panel; its payload names no slot at all.
    "refine": "task-runner refine panel",
    **{name: "subagent lifecycle panel" for name in _SUBAGENT_EVENTS},
}


def test_every_other_slot_scoped_frame_is_named_as_read_unchanged():
    # The reverse direction: a slot-scoped frame the client does not signal must
    # be named here with its reason, so a new one cannot slip past both lists.
    frames = set(_push_frames())
    assert sorted(_SLOT_SCOPED_EVENTS - frames - set(_READ_UNCHANGED)) == []
    assert sorted(set(_READ_UNCHANGED) - _SLOT_SCOPED_EVENTS) == []
    assert sorted(set(_READ_UNCHANGED) & frames) == []


@pytest.mark.parametrize("event_type", _push_frames())
def test_gateway_admits_push_frame_for_own_slot_only(event_type):
    own = _make_slot(owner_app="spec-builder", origin=SlotOrigin.APP, key="own")
    foreign = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="foreign")
    person = _make_slot(owner_app="", origin=SlotOrigin.USER, key="person")
    state = _make_state({"own": own, "foreign": foreign, "person": person})
    no_scope = _allowed()
    # The same field the client reads the slot from for this frame.
    field = "key" if event_type in _key_frames() else "slot"

    def allowed(slot_key: str) -> bool:
        return ws_event_allowed(
            event_type, {field: slot_key}, app="spec-builder", allowed_events=no_scope, state=state
        )

    assert allowed("own") is True
    assert allowed("foreign") is False
    assert allowed("person") is False
