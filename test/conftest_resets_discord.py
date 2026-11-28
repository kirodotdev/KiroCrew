"""Per-test resets of process-global ``kiro_crew.discord`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_discord_thread_titles():
    """No Discord thread rename or naming task remembered yet.

    ``thread_title`` remembers the name it gave each thread and every thread Discord
    refused, keyed by thread id, and the ledger holds its naming tasks. Tests reuse
    thread ids, so a rename one test left could make the next one skip. Cleared in
    place, only when the module is already imported.
    """

    def clear():
        titles = sys.modules.get("kiro_crew.discord.thread_title")
        if titles is not None:
            titles.reset()
        ledger = sys.modules.get("kiro_crew.discord.transport_dispatch_ledger")
        if ledger is not None:
            ledger._TITLE_TASKS.clear()

    clear()
    try:
        yield
    finally:
        clear()
