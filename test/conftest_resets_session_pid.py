"""Per-test resets of process-global ``kiro_crew.session_pid`` sweep state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_dead_root_retain_warnings():
    """Clear the Windows dead-root warn-once set before and after each test.

    ``session_pid._DEAD_ROOT_RETAIN_WARNED`` holds the entries this process already
    warned about, so one test's sweep would silence a later test's WARNING for the
    same entry.
    """

    def clear():
        module = sys.modules.get("kiro_crew.session_pid")
        warned = getattr(module, "_DEAD_ROOT_RETAIN_WARNED", None)
        if warned is not None:
            warned.clear()

    clear()
    try:
        yield
    finally:
        clear()
