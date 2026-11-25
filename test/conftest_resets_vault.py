"""Per-test reset of the browser sign-in's held "Save this login?" offers.

Part of ``test/conftest.py``, which imports and re-binds this name so pytest
registers the fixture as the suite conftest's own. Plain module, never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _drop_held_vault_save_offers():
    """Start and end each test with no typed login held for "Save this login?".

    ``browser_harness.vault_fill`` keeps one held offer per session key, process
    wide, each with a password and a timer on the loop that armed it. Tests reuse
    session keys, so a kept offer would answer another test's save.
    """

    def clear():
        module = sys.modules.get("kiro_crew.browser_harness.vault_fill")
        if module is not None:
            for key in list(module._offers):
                module.drop_offer(key)

    clear()
    try:
        yield
    finally:
        clear()
