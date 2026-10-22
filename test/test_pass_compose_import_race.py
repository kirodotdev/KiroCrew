"""Regression test: pass_compose must be fully loaded when text_redaction loads.

:mod:`kiro_crew.security.text_redaction` now eagerly imports
:mod:`kiro_crew.security.pass_compose` so that a concurrent thread calling
:func:`~kiro_crew.security.text_redaction.redact_with_findings` can never see a
partially-initialised ``pass_compose`` in ``sys.modules``.  Before the fix,
``_submodule("pass_compose")`` read ``sys.modules`` directly (no import lock), so a
second thread running ``redact_with_findings`` while a first was still executing
``pass_compose.py``'s body would get the partial module and raise::

    AttributeError: partially initialized module
    'kiro_crew.security.pass_compose' has no attribute 'compose'

Fixes the flaky test::

    test/test_skill_read_paging.py::TestPagedRead::
        test_a_body_over_the_capacity_is_read_whole_across_pages
"""

from __future__ import annotations

import subprocess
import sys


def test_pass_compose_fully_loaded_when_text_redaction_imports() -> None:
    """Importing text_redaction must bring pass_compose into sys.modules complete.

    The subprocess runs with a clean ``sys.modules``; if
    ``kiro_crew.security.pass_compose`` is absent or lacks ``.compose`` after
    ``text_redaction`` loads, the ``_submodule`` lazy-load race is still present.
    """
    code = (
        "import sys, pathlib\n"
        "src = str(pathlib.Path('src').resolve())\n"
        "sys.path.insert(0, src)\n"
        "import kiro_crew.security.text_redaction\n"
        "pc = sys.modules.get('kiro_crew.security.pass_compose')\n"
        "assert pc is not None, 'pass_compose absent from sys.modules'\n"
        "assert hasattr(pc, 'compose'), "
        "f'pass_compose partially initialised: {list(vars(pc))}'\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, (
        "pass_compose was not fully loaded when text_redaction imported.\n"
        "This means the threading race that flakes "
        "test_a_body_over_the_capacity_is_read_whole_across_pages is still present.\n"
        f"stderr: {result.stderr}"
    )
