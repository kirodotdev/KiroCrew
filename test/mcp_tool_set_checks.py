"""Checks the crew-log and debug MCP test files share, written once.

Each check lives here as a mixin. A test class in ``test_mcp_crew_log.py`` or
``test_mcp_debug.py`` takes the mixin as a base, so the node id stays
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


class TheDeclaredSetMatchesWhatIsAdvertised:
    def test_the_declared_set_matches_what_is_advertised(self) -> None:
        here = _file_module(self)
        assert here.server.TOOLS == tuple(t["name"] for t in here.server._list_tools())
