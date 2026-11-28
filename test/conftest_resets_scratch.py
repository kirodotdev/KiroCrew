"""Per-test resets of process-global ``kiro_crew.agent_scratch_trees`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_unpersisted_scratch_refusals():
    """Start and end each test with no in-process scratch refusal.

    ``agent_scratch_trees._REFUSED_UNPERSISTED`` refuses, for the life of the
    process, a directory whose quarantine the tree index did not take. Tests reuse
    directory names under fresh scratch roots, so a refusal one test held would
    refuse an unmarked directory in the next.
    """

    def clear():
        # getattr: prove.py runs these guards against the tree with the fix reverted.
        held = getattr(
            sys.modules.get("kiro_crew.agent_scratch_trees"), "_REFUSED_UNPERSISTED", None
        )
        if held is not None:
            held.clear()
            sys.modules["kiro_crew.agent_scratch_trees"]._REFUSALS_OVERFLOWED = False

    clear()
    try:
        yield
    finally:
        clear()
