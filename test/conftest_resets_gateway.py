"""MCP-gateway per-test resets, split from ``conftest_resets.py`` for the file cap.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest
registers each fixture as the suite conftest's own; see the import block there.
Plain module, never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _forget_stub_agent_bindings():
    """Start each test with no agent bound to any stub session token.

    ``mcp_gateway.claim._BOUND_AGENTS`` (what the gateway re-sends with each claim)
    and ``mcp_gateway.daemon.identity._AGENT_BINDINGS`` (what the daemon serves a
    stub's declared env by) are process-wide, and tests reuse tokens, so one test's
    binding would grant an agent to, or send a frame for, the next test's stub.
    The daemon's confined runtimes and tokens (``_CONFINED_RUNTIMES``,
    ``_CONFINED_TOKENS``) are cleared for the same reason: one test's confining
    claim would refuse the next test's stub.
    """

    def clear():
        for name, table in (
            ("kiro_crew.mcp_gateway.claim", "_BOUND_AGENTS"),
            ("kiro_crew.mcp_gateway.claim", "_CONFINED_BINDINGS"),
            ("kiro_crew.mcp_gateway.daemon.identity", "_AGENT_BINDINGS"),
            ("kiro_crew.mcp_gateway.daemon.identity", "_CONFINED_RUNTIMES"),
            ("kiro_crew.mcp_gateway.daemon.identity", "_CONFINED_TOKENS"),
        ):
            module = sys.modules.get(name)
            if module is not None:
                getattr(module, table).clear()

    clear()
    try:
        yield
    finally:
        clear()
