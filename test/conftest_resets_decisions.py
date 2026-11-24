"""Per-test resets of process-global ``kiro_crew.decisions`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_decision_provider_error_warnings():
    """No provider failure warned about yet: ``gate._provider_error_warned`` logs the
    first failure per point and exception class at WARNING and repeats at INFO, so a
    pair an earlier test on the same worker filled would turn this test's WARNING into
    INFO. Cleared in place, only when the module is already imported."""

    def clear():
        module = sys.modules.get("kiro_crew.decisions.gate")
        if module is not None:
            module._provider_error_warned.clear()

    clear()
    try:
        yield
    finally:
        clear()
