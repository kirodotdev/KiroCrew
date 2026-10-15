"""The exception classes ``session_transfer_intake`` holds are named by their facade.

A raised class renders as ``module.qualname`` in a traceback and an error chain, so
each class keeps the name of the module callers import it from,
``kiro_crew.dashboard.session_transfer``, and resolves back to itself through it.
"""

from __future__ import annotations

import sys

import pytest

from kiro_crew.dashboard import session_transfer, session_transfer_intake


@pytest.mark.parametrize(
    "name", ["_DiskFull", "_UploadsBusy", "_ExpansionBusy", "_NeverFits", "_MemoryWaitTimedOut"]
)
def test_an_intake_exception_class_is_named_by_the_facade(name: str) -> None:
    cls = getattr(session_transfer, name)
    assert cls is vars(session_transfer_intake)[name]
    assert issubclass(cls, Exception)
    assert cls.__module__ == "kiro_crew.dashboard.session_transfer"
    assert getattr(sys.modules[cls.__module__], cls.__qualname__) is cls
