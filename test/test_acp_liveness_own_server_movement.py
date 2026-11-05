"""The opaque-MCP movement sample covers the in-flight tool's own server.

On the KAS relay route the runtime's tree is kiro-cli (the relay) -> the Node engine
-> the MCP servers. A whole-tree sample counts the engine's own CPU/IO as the tool
working, which holds a server blocked forever on a read to
``tool_stall_hard_cap_secs`` whatever ``tool_stall_suspect_secs`` says. When the
call's own server is found exactly once, only that server's subtree is sampled; a
server that cannot be found keeps the whole-tree sample.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

from test_acp_liveness import FakeProc, _Clock

from kiro_crew.acp.liveness import (
    EVIDENCE_MCP_SUBTREE_ACTIVE,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
)
from kiro_crew.acp.session_handle import AcpSessionHandle, WatchdogSettings

_SERVER = "hang"
_ROSTER = {_SERVER: (("python3", "hang_server.py"),)}


def _kas_tree(tmp_path) -> FakeProc:
    """Relay (no sockets) -> Node engine -> the tool's server, nothing moving yet."""
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, cmdline="kiro-cli acp --agent-engine v3", children=[200], io_bytes=10)
    fake.add_pid(200, cmdline="node kas-server.js", children=[300], io_bytes=1000, cpu=50)
    fake.add_pid(300, cmdline="python3 hang_server.py", io_bytes=2000, cpu=5)
    return fake


def _oracle(fake: FakeProc, clock: _Clock, attribution=lambda: _ROSTER) -> LivenessOracle:
    return LivenessOracle(
        str(fake.root), now=clock, sample_min_secs=1.0, attribution_launches=attribution
    )


def _tool(clock: _Clock, server: str = _SERVER) -> ToolCallState:
    return ToolCallState(
        title="block_forever", command="{}", dispatch_ts=clock.t, mcp_server_name=server
    )


def _tick_with_engine_moving(
    fake: FakeProc, oracle: LivenessOracle, clock: _Clock, tool: ToolCallState
):
    oracle.check_tool(100, tool)  # baseline
    fake.set_io(200, 5000)  # the engine's own traffic, not the tool's
    clock.advance(2.0)
    return oracle.check_tool(100, tool)


def test_engine_movement_does_not_hold_a_flat_own_server(tmp_path):
    fake = _kas_tree(tmp_path)
    clock = _Clock()
    oracle = _oracle(fake, clock)

    verdict, evidence = _tick_with_engine_moving(fake, oracle, clock, _tool(clock))

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith("mcp subtree flat")


def test_own_server_movement_reads_working_with_its_pid(tmp_path):
    fake = _kas_tree(tmp_path)
    clock = _Clock()
    oracle = _oracle(fake, clock)
    tool = _tool(clock)

    oracle.check_tool(100, tool)
    fake.set_io(300, 9000)
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_WORKING
    assert evidence.startswith(EVIDENCE_MCP_SUBTREE_ACTIVE)
    assert "own server pid 300" in evidence


def test_without_a_roster_the_whole_tree_is_sampled(tmp_path):
    fake = _kas_tree(tmp_path)
    clock = _Clock()
    oracle = _oracle(fake, clock, attribution=lambda: None)

    verdict, evidence = _tick_with_engine_moving(fake, oracle, clock, _tool(clock))

    assert verdict == VERDICT_WORKING
    assert evidence.startswith(EVIDENCE_MCP_SUBTREE_ACTIVE)
    assert "own server" not in evidence


def test_an_unnamed_server_keeps_the_whole_tree_sample(tmp_path):
    fake = _kas_tree(tmp_path)
    clock = _Clock()
    oracle = _oracle(fake, clock)

    verdict, _ = _tick_with_engine_moving(fake, oracle, clock, _tool(clock, server=""))

    assert verdict == VERDICT_WORKING


def test_two_copies_of_the_server_keep_the_whole_tree_sample(tmp_path):
    """Which copy serves this call cannot be read from the tree."""
    fake = _kas_tree(tmp_path)
    fake.add_pid(200, cmdline="node kas-server.js", children=[300, 301], io_bytes=1000)
    fake.add_pid(301, cmdline="python3 hang_server.py", io_bytes=2000)
    clock = _Clock()
    oracle = _oracle(fake, clock)

    verdict, evidence = _tick_with_engine_moving(fake, oracle, clock, _tool(clock))

    assert verdict == VERDICT_WORKING
    assert "own server" not in evidence


def test_a_raising_roster_keeps_the_whole_tree_sample(tmp_path):
    def _raises():
        raise RuntimeError("torn down")

    fake = _kas_tree(tmp_path)
    clock = _Clock()
    oracle = _oracle(fake, clock, attribution=_raises)

    verdict, _ = _tick_with_engine_moving(fake, oracle, clock, _tool(clock))

    assert verdict == VERDICT_WORKING


def test_handle_declares_the_roster_with_the_remote_flat_window_off():
    handle = AcpSessionHandle("sA", asyncio.Queue(), MagicMock(), watchdog=WatchdogSettings())
    assert handle._watchdog.remote_flat_probe_secs <= 0
    assert handle._oracle._launch_roster() is None

    handle.record_mcp_launches({"mcpServers": {"srv": {"command": "node", "args": ["s.js"]}}}, [])

    expected = {"srv": (("node", "s.js"),)}
    assert handle._oracle._attribution_launches() == expected
    assert handle._oracle.fresh()._attribution_launches() == expected
    assert handle._oracle._launch_roster() is None
