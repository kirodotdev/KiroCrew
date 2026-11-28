"""Per-test resets of process-global ``kiro_crew.apps`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_app_auto_update_state():
    """Give every test an app update loop with no stop, hold, applying app or outcome.

    ``apps.auto_update`` keeps the stop request gateway shutdown sets, the
    restart hold count, the apps mid-apply and the outcomes this process
    recorded at module level. A test that ends a gateway or drives an update
    leaves them set, and a later test on the same worker would then have every
    update put off, or read ledger entries from an earlier data home. Cleared
    before and after, so a test that never imported the module pays one dict
    lookup.
    """
    updater = sys.modules.get("kiro_crew.apps.auto_update")
    if updater is not None:
        updater.reset_for_tests()
    yield
    updater = sys.modules.get("kiro_crew.apps.auto_update")
    if updater is not None:
        updater.reset_for_tests()
