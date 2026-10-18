"""Shared constants, fixture and builders for the session ledger projection tests.

``_isolated_home`` gives each test its own data home with the crew log on and resets the
emitter and slot-fold caches around it. ``_unit`` creates one session crew log for a slot
and drops its handle, and ``_entries`` reads a unit's entries back.
``test_session_ledger_projection.py`` and its ``_carry_forward`` and
``_durability_and_cache`` siblings import them from here.
"""

from __future__ import annotations

import pytest

from kiro_crew import crew_log as lg
from kiro_crew.crew_log import CrewLog
from kiro_crew.crew_log import emit as crew_log_emit
from kiro_crew.crew_log import projection as crew_log

SLOT = "chat-1"
SESSION = "acp-1"
LATER_SESSION = "acp-2"


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the modules that import it calls monkeypatch.undo()
def _isolated_home(tmp_path, monkeypatch):
    """Own data home, crew log on, and no writer state carried between tests."""
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_CREW_LOG", "1")
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()
    yield
    crew_log_emit.reset_caches()
    crew_log.forget_slot_folds()


def _unit(unit_id: str = SESSION, *, slot: str = SLOT) -> None:
    """Create one session crew log, then drop the handle so it holds no lease.

    The emitter opens its own handle when the ledger appends, and a handle this
    test kept would own the write lease it needs.
    """
    CrewLog.create(lg.KIND_SESSION, unit_id, owner="owner", agent="kirocrew", slot=slot)


def _entries(unit_id: str = SESSION) -> list:
    handle = CrewLog.open(lg.KIND_SESSION, unit_id)
    try:
        return list(handle.iter_from(1))
    finally:
        del handle
