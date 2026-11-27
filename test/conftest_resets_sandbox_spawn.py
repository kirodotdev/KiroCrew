"""Per-test resets of process-global ``kiro_crew.sandbox_spawn`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_aws_key_drop_log():
    """Clear the dropped-AWS-key log-once mark before and after each test.

    ``sandbox_spawn._log_dropped_aws_keys`` logs once per process which AWS key
    variables an agent child does not get, so a test that scrubs an environment
    holding one would otherwise silence the line for every later test on the same
    worker.
    """

    def clear():
        # Only when a test already loaded the module: one loaded later starts clear.
        sandbox_spawn = sys.modules.get("kiro_crew.sandbox_spawn")
        logged = getattr(sandbox_spawn, "_aws_key_drop_logged", None)
        if logged is not None:
            logged.clear()

    clear()
    try:
        yield
    finally:
        clear()
