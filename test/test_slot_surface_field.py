"""Tests for the `surface` field on serialized chat slots.

The frontend's surface registry routes slot-bearing UI (Chat tab badge,
Autopilot tab badge, slot list filter on each page) by reading
`slot.surface ?? slot.mode`. The backend emits `surface` as a forward-compat
mirror of `mode`: today every slot's surface equals its mode, but the
distinct field gives us the seam to split them later (e.g. introduce a new
mode that shares an existing surface) without another wire-format change.
These tests pin the contract.
"""

from kiro_crew.dashboard.state import _ChatSlot


def test_to_dict_emits_surface_field_for_default_chat():
    s = _ChatSlot("chat-1")  # mode defaults to ""
    d = s.to_dict()
    assert d["mode"] == ""
    assert "surface" in d, "to_dict must emit surface field for the frontend registry"
    assert d["surface"] == ""


def test_to_dict_emits_surface_field_for_orchestrator():
    s = _ChatSlot("orch-1", mode="orchestrator")
    d = s.to_dict()
    assert d["mode"] == "orchestrator"
    assert d["surface"] == "orchestrator"


def test_surface_mirrors_mode_for_arbitrary_modes():
    # If a future mode is added (and it claims its own surface initially),
    # the mirror must hold so the frontend registry sees the slot.
    s = _ChatSlot("custom-1", mode="custom-mode")
    d = s.to_dict()
    assert d["surface"] == d["mode"] == "custom-mode"
