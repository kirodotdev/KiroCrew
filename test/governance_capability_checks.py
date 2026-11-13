"""Checks the social-share and tailnet governance test files share, written once.

Each check lives here as a mixin. A test class in ``test_social_share_governance.py`` or
``test_tailnet_governance.py`` takes the mixin as a base, so the node id stays
``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves the
test's fixtures from that file. A check that reads the file's own constants or helpers
does so through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class RowIsRegisteredAsADefaultOnCapability:
    def test_row_is_registered_as_a_default_on_capability(self) -> None:
        here = _file_module(self)
        from kiro_crew.platform.governance import CAPABILITY, SCOPE_CATALOG

        spec = SCOPE_CATALOG[here._SCOPE]
        assert spec.kind == CAPABILITY
        # A policy that governs some OTHER capabilities.* row must not silently
        # withdraw a menu entry it never mentioned.
        assert spec.capability_default is True
