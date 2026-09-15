"""A read must publish whether a close can still pop that object."""

from __future__ import annotations

from kiro_crew.dashboard.state import _ChatSlot


def test_a_quiet_slot_publishes_that_no_close_can_pop_it() -> None:
    slot = _ChatSlot("closing-quiet")

    assert slot.to_dict()["closing"] is False


def test_a_slot_with_a_close_in_flight_publishes_it() -> None:
    slot = _ChatSlot("closing-busy")
    slot._closes_in_flight = 1

    assert slot.to_dict()["closing"] is True


def test_the_field_tracks_the_count_back_down() -> None:
    slot = _ChatSlot("closing-drains")
    slot._closes_in_flight = 2
    assert slot.to_dict()["closing"] is True

    slot._closes_in_flight = 0

    assert slot.to_dict()["closing"] is False
