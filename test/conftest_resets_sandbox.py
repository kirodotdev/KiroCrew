"""Per-test resets of process-global ``kiro_crew`` sandbox probe state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_sandbox_exec_failure():
    """Forget why the last ``sandbox-exec`` probe failed.

    ``sandbox_probe._probe_sandbox_exec`` keeps that reason in a module global for
    ``detect_backend`` to report, so a test that drives a failing probe would hand
    its reason to a later darwin ``detect_backend`` test on the same worker.
    """

    def clear():
        module = sys.modules.get("kiro_crew.sandbox_probe")
        if module is not None:
            module._last_sandbox_exec_failure = None

    clear()
    try:
        yield
    finally:
        clear()
