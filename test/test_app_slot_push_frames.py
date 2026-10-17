"""The dashboard's app slot-push filter grants no more than the gateway's own event gate.

``website/src/utils/appSlotPush.ts`` signals an app embed when a frame
from the SLOT_CHANGE_FRAMES set arrives for a slot that app owns. That claim is
only safe while every name in the set is a slot-scoped event here, outside the
subagent family, so the gateway itself admits it to an app token for its own
slot with no ``slots:*`` declaration and refuses it for a slot another app or a
person owns. These tests read the set from the client source and check each
name against ``ws_event_scope``.
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


def _push_frames() -> list[str]:
    source = _PUSH_SOURCE.read_text(encoding="utf-8")
    match = re.search(r"const SLOT_CHANGE_FRAMES[^=]*= new Set\(\[(.*?)\]\)", source, re.DOTALL)
    assert match, "SLOT_CHANGE_FRAMES literal not found in appSlotPush.ts"
    names = re.findall(r"'([^']+)'", match.group(1))
    assert names, "SLOT_CHANGE_FRAMES is empty"
    return names


def test_every_push_frame_is_slot_scoped_and_not_subagent():
    frames = _push_frames()
    assert sorted(set(frames) - _SLOT_SCOPED_EVENTS) == []
    assert sorted(set(frames) & _SUBAGENT_EVENTS) == []


@pytest.mark.parametrize("event_type", _push_frames())
def test_gateway_admits_push_frame_for_own_slot_only(event_type):
    own = _make_slot(owner_app="spec-builder", origin=SlotOrigin.APP, key="own")
    foreign = _make_slot(owner_app="other-app", origin=SlotOrigin.APP, key="foreign")
    person = _make_slot(owner_app="", origin=SlotOrigin.USER, key="person")
    state = _make_state({"own": own, "foreign": foreign, "person": person})
    no_scope = _allowed()

    def allowed(slot_key: str) -> bool:
        return ws_event_allowed(
            event_type, {"slot": slot_key}, app="spec-builder", allowed_events=no_scope, state=state
        )

    assert allowed("own") is True
    assert allowed("foreign") is False
    assert allowed("person") is False
