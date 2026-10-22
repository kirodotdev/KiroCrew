"""The page's session folds read the unit the slot's LIVE session serves.

A pod slot held its live chain plus two later, unrelated units; "newest" picked one of
those, and the page showed 0.0 credits beside 94.1 recorded for the live session.
"""

from __future__ import annotations

from types import SimpleNamespace

from kiro_crew.crew_log import projection, resolve
from kiro_crew.dashboard.handlers import member_dashboard as md

SLOT = "member-x.memory-member-x-0123"
UNITS = ("sess_live", "later-chain-a", "later-chain-b")


def test_the_live_session_unit_wins_over_a_later_unrelated_chain(monkeypatch):
    monkeypatch.setattr(projection, "units_in_succession", lambda slot: UNITS)
    seen: list[str] = []

    def live(sessions, key):
        seen.append(key)
        return "sess_live"

    monkeypatch.setattr(resolve, "unit_for_session_key", live)
    state = SimpleNamespace(sessions=object())
    assert md._newest_unit(SLOT) == "later-chain-b"
    assert md._live_or_newest_unit(state, SLOT) == "sess_live"
    assert seen == [f"dashboard:{SLOT}"]


def test_no_live_session_or_a_foreign_unit_falls_back_to_the_newest(monkeypatch):
    monkeypatch.setattr(projection, "units_in_succession", lambda slot: UNITS)
    state = SimpleNamespace(sessions=object())
    monkeypatch.setattr(resolve, "unit_for_session_key", lambda sessions, key: "")
    assert md._live_or_newest_unit(state, SLOT) == "later-chain-b"
    # A live id that is not one of the slot's units is not trusted for it.
    monkeypatch.setattr(resolve, "unit_for_session_key", lambda sessions, key: "elsewhere")
    assert md._live_or_newest_unit(state, SLOT) == "later-chain-b"
    assert md._live_or_newest_unit(state, None) == ""
