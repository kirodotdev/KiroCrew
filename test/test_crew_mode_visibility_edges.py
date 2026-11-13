"""Crew Mode stamps survive a peer row's allowlist, and a forged root flag does not."""

from __future__ import annotations

import pytest

from kiro_crew import crew_mode


@pytest.fixture(autouse=True)
def _clean(_floor_monkeypatch):
    _floor_monkeypatch.setattr(crew_mode, "_known_roots", set())
    _floor_monkeypatch.setattr(crew_mode, "persisted_parents", lambda: {})
    _floor_monkeypatch.setattr(crew_mode, "_live_cfg", lambda: None)
    yield


def test_E9_a_peer_row_keeps_its_crew_stamps_and_drops_a_forged_root_flag():
    from kiro_crew.dashboard.handlers_instances_peer import _clean_peer_slot

    worker = _clean_peer_slot({"key": "w", "crew_root": "r", "crew_mode_root": False})
    root = _clean_peer_slot({"key": "r", "crew_mode_root": True})
    forged = _clean_peer_slot({"key": "f", "crew_mode_root": "yes", "crew_root": 7})
    assert worker == {**worker, "crew_root": "r"} and "crew_mode_root" not in worker
    assert root is not None and root["crew_mode_root"] is True
    assert forged is not None and "crew_mode_root" not in forged and "crew_root" not in forged
