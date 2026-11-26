"""Per-test resets of process-global ``kiro_crew.memory_graph_service`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_memory_graphs():
    """Start and end each test with no cached store graph.

    ``memory_graph_service._GRAPHS`` keeps one ``StoreGraph`` per store path, holding a
    file stamp, a build back-off and any running build child. Tests reuse store paths
    under one data home, so a later test would inherit them. Read from ``sys.modules``
    so a test that never imports the module pays nothing.
    """
    module = sys.modules.get("kiro_crew.memory_graph_service")
    if module is not None:
        module.forget_graphs()
    yield
    module = sys.modules.get("kiro_crew.memory_graph_service")
    if module is not None:
        module.forget_graphs()
