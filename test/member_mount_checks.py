"""Checks the member dispatch and member panel mount test files share, written once.

Each check lives here as a mixin. A test class in ``test_member_dispatch_mount.py`` or
``test_member_panel_mount.py`` takes the mixin as a base, so the node id stays
``<file>::<Class>::<test>``, the class keeps its own setup, and pytest resolves the
test's fixtures from that file. A check that reads the file's own constants or helpers
does so through :func:`_file_module`.
"""

from __future__ import annotations

import sys
from types import ModuleType

from kiro_crew.acp.types import ACP_BACKEND_KIRO


def _file_module(test: object) -> ModuleType:
    """The test file the class running this check is defined in."""
    return sys.modules[type(test).__module__]


class NonMemberSessionIsUntouched:
    def test_non_member_session_is_untouched(self):
        here = _file_module(self)
        stub = here._ClientStub()
        stub._session_key = "dashboard_abc123"
        assert self._run(stub) == here._base_servers()


class KiroBackendIsUntouched:
    def test_kiro_backend_is_untouched(self):
        here = _file_module(self)
        stub = here._ClientStub()
        stub.backend = ACP_BACKEND_KIRO
        assert self._run(stub) == here._base_servers()
