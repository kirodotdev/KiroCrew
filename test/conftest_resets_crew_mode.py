"""Per-test resets of process-global ``kiro_crew.crew_mode`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_crew_mode_conductor(_floor_monkeypatch):
    """Every test starts on the shipped conductor, with no configured name marked unusable.

    ``crew_mode.remember`` keeps the ``agent.crew_mode_agent`` of every config a reader
    is handed, and the migration records an unusable name in ``_unusable``. Both are
    module values, so a test that resolves a custom conductor would otherwise move the
    Crew Mode lock of whichever test runs next on the worker onto that name. Reset only
    when the module is already imported.
    """
    module = sys.modules.get("kiro_crew.crew_mode")
    if module is not None:
        _floor_monkeypatch.setattr(module, "_configured", module.DEFAULT_CREW_MODE_AGENT)
        _floor_monkeypatch.setattr(module, "_unusable", {})
