"""Per-test reset of the chat search redaction memo (``kiro_crew.search_shown_text``).

Part of ``test/conftest.py``, which imports and re-binds this name so pytest registers
the fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_search_shown_text_memo():
    """Start each test with an empty redaction memo for chat search.

    ``search_shown_text`` keeps texts by digest with the form ``redact`` gave
    them, and its epoch does not cover a rule table a test patches. A form one
    test left could answer the next test's search under other redaction rules.
    """

    def clear():
        module = sys.modules.get("kiro_crew.search_shown_text")
        if module is not None:
            module._clean.clear()
            module._changed.clear()
            module._changed_chars = 0
            module._memo_epoch = None

    clear()
    try:
        yield
    finally:
        clear()
