"""Per-test resets of the memory page's process-global caches in ``kiro_crew.memory_edit``.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _isolate_memory_edit_caches():
    """Empty the memory page's search-summary and facet caches around each test.

    A summary entry is keyed on the store and its change fingerprint, and tests
    reuse temporary paths, so one test's kept summaries must not answer another's
    search. Only an imported module can hold entries.
    """
    _clear_memory_edit_caches()
    yield
    _clear_memory_edit_caches()


def _clear_memory_edit_caches() -> None:
    memory_edit = sys.modules.get("kiro_crew.memory_edit")
    if memory_edit is not None:
        memory_edit._SUMMARY_CACHE.clear()
        memory_edit._FACET_CACHE.clear()
