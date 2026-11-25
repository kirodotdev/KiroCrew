"""Per-test resets of process-global ``kiro_crew.cron_service`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _fresh_cron_grant_nonce_cache():
    """No grant nonce file parsed yet: ``approval_grant.read_nonces`` keeps its last
    parse per path, keyed by the file's stat, and each test has its own data home.
    Cleared in place, only when the module is already imported."""

    def clear():
        module = sys.modules.get("kiro_crew.cron_service.approval_grant")
        if module is not None:
            module._NONCE_CACHE.clear()

    clear()
    try:
        yield
    finally:
        clear()
