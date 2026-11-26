"""Per-test resets of process-global session search state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_session_vector_legs():
    """Give every test no loaded session meaning matrix and no idle timer.

    ``history_vectors_search`` holds each store file's matrix, query memo and idle
    timer in ``_STATES``, keyed by the file's path. A later test on the same worker
    that reuses a path would read the earlier test's matrix, and the earlier
    test's timer would still be running. Released and cleared before and after,
    so a test that never imported the module pays one dict lookup.
    """
    legs = sys.modules.get("kiro_crew.history_vectors_search")
    if legs is not None:
        legs.reset_for_tests()
    yield
    legs = sys.modules.get("kiro_crew.history_vectors_search")
    if legs is not None:
        legs.reset_for_tests()
