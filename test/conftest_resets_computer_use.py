"""Per-test resets of process-global ``kiro_crew.computer_use`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_computer_use_warning(_floor_monkeypatch):
    """Start every test with no unusable ``computer_use.json`` version warned about.

    ``enable_state.load_state`` warns once per file version, so a test that reads a
    bad keystone would otherwise silence that warning for a later test on the
    same worker.
    """
    from kiro_crew.computer_use import enable_state

    _floor_monkeypatch.setattr(enable_state, "_warned_version", None)
