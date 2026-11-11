"""Per-test resets of process-global ``kiro_crew.config.sections_stt`` state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_stt_openai_model_warning():
    """Start each test with no remembered ``stt.openai_model`` warning.

    ``config.sections_stt`` logs an unusable stored model once per shape, so a test
    that loaded one would silence the next test's warning for the same shape.
    """

    def clear():
        module = sys.modules.get("kiro_crew.config.sections_stt")
        if module is not None:
            module._LAST_WARNED_OPENAI_MODEL = None

    clear()
    try:
        yield
    finally:
        clear()
