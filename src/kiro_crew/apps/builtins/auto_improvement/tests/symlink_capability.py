"""Whether this host can create a symlink, for the tests whose contract is one.

Defined here rather than imported from core's ``test/conftest.py``: the app's tests
carry no import from core's test tree.
"""

from __future__ import annotations

import os
import tempfile

import pytest


def _can_create_symlink() -> bool:
    """Whether this host lets the test process create a directory symlink."""
    with tempfile.TemporaryDirectory() as tmp:
        target = os.path.join(tmp, "target")
        os.mkdir(target)
        try:
            os.symlink(target, os.path.join(tmp, "link"))
        except (OSError, NotImplementedError, AttributeError):
            return False
        return True


requires_symlinks = pytest.mark.skipif(
    not _can_create_symlink(),
    reason="creating a symlink needs SeCreateSymbolicLinkPrivilege on Windows",
)
