"""Per-test resets of process-global ``kiro_crew.workflows`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_workflow_shared_node_places(_floor_monkeypatch):
    """Start every test with no workflow step placed on a chat's KAS process.

    ``shared_nodes._placed`` counts the steps on each chat's process for the
    per-parent cap, ``_closing`` holds the places of cancelled opens still in
    flight, and ``_teardowns`` the tasks that end them, which are bound to the
    loop of the test that made them. Any of them left by one test would count
    against, or be awaited by, a later test on the same worker. Only reset when
    the module is loaded, so the floor imports nothing.
    """
    shared_nodes = sys.modules.get("kiro_crew.workflows.shared_nodes")
    if shared_nodes is None:
        return
    _floor_monkeypatch.setattr(shared_nodes, "_placed", {})
    _floor_monkeypatch.setattr(shared_nodes, "_closing", set())
    _floor_monkeypatch.setattr(shared_nodes, "_teardowns", set())
