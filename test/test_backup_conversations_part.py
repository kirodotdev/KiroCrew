"""The conversation export's errors keep the backup facade as their home.

The terminal conversation export lives in ``backup_parts.conversations`` and is reached
through ``backend.backup`` like every other owner's code. Its three errors are raised and
caught inside the export, but a traceback, a logged error type and ``pickle`` name an
error by its module, so each names ``backup``, the engine's one import path; the class
itself is the one the part defines and raises. The part is read from ``sys.modules``
rather than imported, so the composition contract stays the one test that imports a part.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew.apps.builtins.aws_control.backend import backup

PART = "kiro_crew.apps.builtins.aws_control.backend.backup_parts.conversations"


@pytest.mark.parametrize(
    "name", ["_ConversationTooLarge", "_RedactionFailed", "_ScratchExportUnsafe"]
)
def test_the_exports_errors_keep_the_facade_as_their_home(name: str) -> None:
    cls = getattr(backup, name)
    assert cls is vars(sys.modules[PART])[name]
    assert cls.__module__ == backup.__name__
