"""Checks the repository gate-script test files share, written once.

Each check lives here as a mixin. A test class in a gate script's test file takes the
mixin as a base, so the node id stays ``<file>::<Class>::<test>``, the class keeps its
own setup, and pytest resolves the test's fixtures from that file. A check that reads
the file's own constants or helpers does so through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class SelfTestPasses:
    def test_self_test_passes(self) -> None:
        here = _file_module(self)
        assert here.gate._self_test() == 0
