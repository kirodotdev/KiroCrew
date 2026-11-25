"""Per-test resets of process-global ``kiro_crew.managed_env`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_managed_env_shared_client():
    """Forget the managed-environment lane's one client around each test.

    ``managed_env.engine.shared_client`` keeps one client per process, and its guard is
    bound to the data home of the test that made it and holds that test's backoff. Read
    from ``sys.modules`` so a test that never imports the lane pays nothing.
    """
    module = sys.modules.get("kiro_crew.managed_env.engine")
    if module is not None:
        module._shared = None
    yield
    module = sys.modules.get("kiro_crew.managed_env.engine")
    if module is not None:
        module._shared = None
