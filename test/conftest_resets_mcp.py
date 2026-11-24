"""Per-test resets of process-global MCP discovery and agent-rebuild state.

Part of ``test/conftest.py``, which imports and re-binds these names so pytest registers
each fixture as the suite conftest's own; see the import block there. Plain module,
never collected.
"""

from __future__ import annotations

import sys

from conftest_guards import per_test_guard


@per_test_guard
def _reset_mcp_sources_read_snapshot():
    """Start each test with no remembered read of the user's MCP files.

    The rebuild keeps the agents directory it wrote and the digest of every MCP
    file it read, so a session start can tell whether a file changed since. Tests
    use their own homes and files, so a snapshot one test left would make the next
    test's start rebuild, or skip a rebuild, for files it never wrote.
    """

    def clear():
        # The kiro_crew.agent facade composes spec_rebuild's functions onto its own
        # globals, so the rebuild reads and writes the facade's copy; clear both.
        for name in ("kiro_crew.agent_parts.spec_rebuild", "kiro_crew.agent"):
            module = sys.modules.get(name)
            if module is not None and hasattr(module, "_mcp_sources_read"):
                module._mcp_sources_read = None

    clear()
    try:
        yield
    finally:
        clear()


@per_test_guard
def _reset_managed_server_tool_spellings():
    """Start each test with no remembered tool list of Kiro Crew's own MCP servers.

    ``platform.server_pattern_reach`` keeps, per managed server, every name the gate
    judges its calls under, read once from the servers' own modules. A test that
    patches a server's tool list would otherwise leave the next test its answer.
    """

    def clear():
        module = sys.modules.get("kiro_crew.platform.server_pattern_reach")
        if module is not None and hasattr(module, "_managed_spellings"):
            module._managed_spellings.cache_clear()

    clear()
    try:
        yield
    finally:
        clear()


@per_test_guard
def _reset_mcp_probe_same_process_locks():
    """Start each test with no probe lock entry for a server's launch line.

    An entry lives only while a probe holds or waits on its lock. A test that
    leaves a probe suspended inside one leaves its count behind, so the entry
    never drops and the next test with the same command starts with a waiter
    that does not exist.
    """

    def clear():
        # A test may stand a stand-in module in for this one; clear only real tables.
        module = sys.modules.get("kiro_crew.mcp_discovery")
        for name in ("_same_process_locks", "_same_process_users"):
            table = getattr(module, name, None)
            if isinstance(table, dict):
                table.clear()

    clear()
    try:
        yield
    finally:
        clear()


@per_test_guard
def _fresh_kas_env_left_out_marks():
    """Start and end each test with no KAS self-reference left-out line already logged.

    ``acp.kas_env_launch._LEFT_OUT_LOGGED`` holds the (agent, server, keys) marks
    whose "left out, the engine's own value reaches the server" INFO line this
    process gave, so a mark an earlier test on the same worker logged would silence
    this test's line. Only when already imported, and read with ``getattr`` so a
    tree without the set still runs its tests.
    """
    launch = sys.modules.get("kiro_crew.acp.kas_env_launch")
    marks = getattr(launch, "_LEFT_OUT_LOGGED", None)
    if marks is not None:
        marks.clear()
    yield
    launch = sys.modules.get("kiro_crew.acp.kas_env_launch")
    marks = getattr(launch, "_LEFT_OUT_LOGGED", None)
    if marks is not None:
        marks.clear()
