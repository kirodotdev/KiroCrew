"""Per-test resets of process-global ``kiro_crew.shared_crew`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_ssm_invite_store():
    """Start each test with the SSM invite store unread.

    ``kiro_crew.shared_crew.ssm_invites`` keeps the parsed ``invites.json`` in ``_cache``,
    and whether reading it failed in ``_unreadable``, filled from the data home of the
    test that read it, so a later test would verify against, or refuse to write over,
    an earlier test's store. Reset only when the module is already imported.
    """

    def reset():
        module = sys.modules.get("kiro_crew.shared_crew.ssm_invites")
        if module is not None:
            module._reset_for_tests()

    reset()
    try:
        yield
    finally:
        reset()
