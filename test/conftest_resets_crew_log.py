"""Per-test resets of process-global ``kiro_crew.crew_log`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_crew_log_session_key_probe():
    """Start each test with no session-key probe registered for the crew log's carry.

    A gateway boot registers a probe bound to its own session map, and
    ``emit.reset_caches`` keeps it on purpose. An in-process gateway one test booted
    would otherwise have every later test on the worker run slot carries against that
    torn-down map.
    """

    def clear():
        module = sys.modules.get("kiro_crew.crew_log.emit_state")
        if module is not None:
            module._session_key_probe.clear()

    clear()
    try:
        yield
    finally:
        clear()
