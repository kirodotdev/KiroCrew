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


@per_test_guard
def _fresh_backend_tmp_socket_room_warnings(_floor_monkeypatch):
    """Start each test with no low socket room warning given, and room to spare.

    ``mcp_gateway.backend_tmp._warned_roots`` holds the managed roots whose low
    socket room warning this process logged, so a root an earlier test on the same
    worker warned about would silence this test's warning. And whether a path is
    too long depends on how long this runner's temp folder is, so every failed
    probe's error would gain a hint on one runner and not on another. The budget is
    pinned out of reach here; the socket room tests put the real one back.
    """
    from kiro_crew.mcp_gateway import backend_tmp

    _floor_monkeypatch.setattr(backend_tmp, "_warned_roots", set(), raising=False)
    _floor_monkeypatch.setattr(backend_tmp, "_SUN_PATH_BYTES", sys.maxsize, raising=False)


@per_test_guard
def _no_mcp_auto_share(request, _floor_monkeypatch):
    """Keep the gateway start's auto-share pass off the host's own MCP servers.

    ``mcp_gateway.auto_share`` lists every configured server (the host's
    ``~/.kiro`` included) and writes a stub for each into config, so a test that
    starts the gateway would otherwise stub servers it never declared. A module
    that tests the pass itself sets ``AUTO_SHARE = True``.
    """
    if getattr(request.module, "AUTO_SHARE", False):
        return
    from kiro_crew.mcp_gateway import auto_share

    _floor_monkeypatch.setattr(auto_share, "share_declared_servers", lambda: [])
    # Nor may it strip the host's own rendered agent spec.
    _floor_monkeypatch.setattr(
        auto_share,
        "prune_removed_sources",
        lambda previous, pending=frozenset(): (previous, pending),
    )
    # The declaration watch it starts polls for as long as the gateway runs;
    # with nothing to watch it returns at once.
    _floor_monkeypatch.setattr(auto_share, "declaration_files_stat", lambda: None)


@per_test_guard
def _no_first_sight_measure(request, _floor_monkeypatch):
    """Keep a probe round from starting its unasked pre-flight pass.

    ``mcp_probe._start_first_sight_measure`` spawns each fake ``status="ok"``
    server twice in the background after a test's probe round, which the test
    never asked for. A module that tests that pass sets ``FIRST_SIGHT_MEASURE = True``.
    """
    if getattr(request.module, "FIRST_SIGHT_MEASURE", False):
        return
    from kiro_crew.dashboard.handlers import mcp_probe

    _floor_monkeypatch.setattr(mcp_probe, "_start_first_sight_measure", lambda *_a: None)


@per_test_guard
def _no_backend_sandbox(request, _floor_monkeypatch):
    """Spawn gateway backends without the global sandbox, except where it is the subject.

    ``backend.spawn_backend`` wraps a shared backend in the host's sandbox and
    refuses the share where none can be built, as inside a test worker that is
    already sandboxed. Tests that start a real backend pin what it does once
    running. A module that tests the wrap itself sets ``BACKEND_SANDBOX = True``.
    """
    if getattr(request.module, "BACKEND_SANDBOX", False):
        return
    from kiro_crew.mcp_gateway import backend

    async def _unwrapped(argv, _env, _tmp, _secrets=(), **_kwargs):
        return argv, None, False

    _floor_monkeypatch.setattr(backend, "_sandboxed_backend_argv", _unwrapped)
