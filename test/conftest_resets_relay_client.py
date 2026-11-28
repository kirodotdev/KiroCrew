"""Per-test resets of process-global ``kiro_crew.relay_client`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_unsaved_relay_instance_ids():
    """No unsaved crew instance ID kept yet: when ``lease.crew_instance_id`` cannot save
    the ID it makes, it keeps it in ``_UNSAVED_INSTANCE_IDS`` by file path and answers
    with it for the rest of the process. A later test on the same worker that uses the
    same data-home path would get that ID instead of reading or making its own. Cleared
    in place, only when the module is already imported."""

    def clear():
        module = sys.modules.get("kiro_crew.relay_client.lease")
        if module is not None:
            module._UNSAVED_INSTANCE_IDS.clear()

    clear()
    try:
        yield
    finally:
        clear()
