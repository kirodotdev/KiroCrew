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


@per_test_guard
def _reset_chat_search_cache_stores():
    """Start each test with no chat history search cache store open.

    ``chat_search_cache`` keeps one store per sessions directory (``_stores``,
    emptied by ``reset_stores``) with its open
    SQLite connections and the transcript revision each chat was last brought to,
    and ``chat_history_cached`` keeps the builds in flight. A store one test left
    would hold a connection into that test's removed ``tmp_path``.
    """

    def clear():
        module = sys.modules.get("kiro_crew.chat_search_cache")
        if module is not None:
            module.reset_stores()
        glue = sys.modules.get("kiro_crew.dashboard.chat_history_cached")
        if glue is not None:
            glue._updates.clear()
            executor, glue._executor = glue._executor, None
            if executor is not None:
                # A build a test left running finishes before the next test.
                executor.shutdown(wait=True)

    clear()
    try:
        yield
    finally:
        clear()
