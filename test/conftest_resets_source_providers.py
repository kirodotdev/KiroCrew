"""Per-test reset of the chip reads' "not a pull request" answers.

Part of ``test/conftest.py``, which imports and re-binds this name so pytest registers
the fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_not_a_pull_request_answers():
    """Start each test with no chip URL remembered as "not a pull request".

    ``chip_status._check_not_a_change`` holds, per URL, the monotonic time a chip
    read learned GitHub has no pull request at that number, and ``chip_refresh``
    starts no read of that URL for hours after it. A mark an earlier test left would
    make this test's chip sweep skip the URL. Only when already imported, and read
    with ``getattr`` so a tree without the map (``prove.py``'s revert) still runs.
    """

    def clear():
        chip_status = sys.modules.get("kiro_crew.dashboard.source_providers.chip_status")
        marks = getattr(chip_status, "_check_not_a_change", None)
        if marks is not None:
            marks.clear()

    clear()
    try:
        yield
    finally:
        clear()
