"""The ``stopped_loop_tombstone`` seam, and the stop path that asks it.

A stop of a legacy autonudge loop removes the row, except on a slot whose app reads
the stopped row back. Core asks through :mod:`kiro_crew.apps.seams` with the slot's
persisted app provenance, and Research Lab answers from its own ``seams.py`` with the
verdict its ``session_keys.is_owned_research_slot`` gives.

The registry is process-global, so every test that changes it does so through
``monkeypatch`` on ``seams._providers`` and the change is undone afterwards.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.apps import seams
from kiro_crew.apps.builtins.auto_research.session_keys import (
    AUTO_RESEARCH_APP,
    is_owned_research_slot,
)
from kiro_crew.dashboard import session_directive_apply_monitor as monitor

SEAM = seams.SEAM_STOPPED_LOOP_TOMBSTONE


@pytest.fixture()
def registry(monkeypatch):
    """The real registry with the shipped providers loaded, restored afterwards."""
    seams.stopped_loop_tombstone()
    monkeypatch.setattr(seams, "_providers", dict(seams._providers))
    return seams._providers


class _Keeps:
    def __init__(self, answer):
        self.answer = answer
        self.asked: list[tuple[str, str]] = []

    def keeps_stopped_loop(self, slot_key, app_id):
        self.asked.append((slot_key, app_id))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return self.answer


class TestResearchLabHoldsTheSeam:
    def test_research_lab_is_the_provider(self):
        provider = seams.stopped_loop_tombstone()
        assert provider is not None
        assert seams._providers[SEAM][0] == AUTO_RESEARCH_APP

    def test_the_provider_reads_slot_key_and_provenance(self):
        provider = seams.stopped_loop_tombstone()
        assert provider.keeps_stopped_loop("research-a1b2c3d4", AUTO_RESEARCH_APP) is True
        assert provider.keeps_stopped_loop("research-a1b2c3d4", "") is False
        assert provider.keeps_stopped_loop("research-notes", AUTO_RESEARCH_APP) is False


class TestTheStopPathAsksTheSeam:
    @pytest.mark.parametrize(
        "slot_key",
        [
            "research-a1b2c3d4",
            "research-deadbeef",
            "research-notes",
            "research-a1b2c3d4-extra",
            "research-A1B2C3D4",
            "research-",
            "dashboard-1",
            "",
        ],
    )
    @pytest.mark.parametrize("app", [AUTO_RESEARCH_APP, "", "issue-radar", "auto_research"])
    def test_the_verdict_is_the_one_the_core_import_gave(self, slot_key, app):
        """Every slot key and provenance keeps a tombstone exactly when it did before."""
        slot = SimpleNamespace(_app=app)
        assert monitor._keeps_stopped_loop(slot, slot_key) is is_owned_research_slot(slot_key, app)

    def test_the_slots_provenance_is_what_the_provider_reads(self, registry):
        spy = _Keeps(True)
        registry[SEAM] = ("app-a", spy)
        assert monitor._keeps_stopped_loop(SimpleNamespace(_app="app-a"), "w-1") is True
        assert monitor._keeps_stopped_loop(SimpleNamespace(), "w-2") is True
        assert monitor._keeps_stopped_loop(None, "w-3") is True
        assert spy.asked == [("w-1", "app-a"), ("w-2", ""), ("w-3", "")]

    def test_with_no_provider_research_slots_are_removed(self, registry):
        del registry[SEAM]
        slot = SimpleNamespace(_app=AUTO_RESEARCH_APP)
        assert monitor._keeps_stopped_loop(slot, "research-a1b2c3d4") is False

    def test_a_provider_that_raises_keeps_nothing(self, registry, caplog):
        registry[SEAM] = ("app-a", _Keeps(RuntimeError("boom")))
        assert monitor._keeps_stopped_loop(SimpleNamespace(_app="app-a"), "w-1") is False
        assert "the stopped_loop_tombstone seam could not answer" in caplog.text
