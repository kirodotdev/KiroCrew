"""Per-test resets of process-global ``kiro_crew.context_assembly`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_cross_session_context_flags(_floor_monkeypatch):
    """No agent's ``includeCrossSessionContext`` read yet.

    ``inclusion._INCLUDE_CROSS_SESSION_CONTEXT_CACHE`` keeps one answer per agent
    name, and tests reuse names: a ``False`` kept from a test whose spec opted out
    would withhold memory and lessons from a later test's agent of the same name.
    Only an imported module can hold one, so a run that never imports it pays nothing.
    """
    inclusion = sys.modules.get("kiro_crew.context_assembly.inclusion")
    if inclusion is None:
        return
    _floor_monkeypatch.setattr(inclusion, "_INCLUDE_CROSS_SESSION_CONTEXT_CACHE", {})
