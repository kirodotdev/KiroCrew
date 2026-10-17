"""The remote_flat tag: an MCP tool blocked on its own remote call.

Oracle side: a genuinely flat tool subtree in which the in-flight tool's OWN MCP
server (the one process launched as the server the call names, or a process
below it) holds an established TCP connection is tagged ``remote_flat``; a
sibling server's or a shell tunnel's connection never is. Watchdog side: that
tag narrows the UNKNOWN window to ``watchdog.remote_flat_probe_secs``, measured
from the later of the last own frame and the last WORKING reading.
"""

from __future__ import annotations

import asyncio
import ctypes
import struct
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_acp_liveness import FakeProc, _Clock
from test_acp_stale_recovery import _SilentQueue

from conftest import requires_symlinks
from kiro_crew import platform_compat
from kiro_crew.acp import liveness
from kiro_crew.acp.liveness import (
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_REMOTE_FLAT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ProcessRow,
    ToolCallState,
    launch_matches,
)
from kiro_crew.acp.mcp_session_report import BUCKET_CAP, NAME_CAP
from kiro_crew.acp.session_handle import (
    _LAUNCH_MAX_ARGS,
    _LAUNCH_MAX_TOKEN,
    _LAUNCHES_PER_SERVER,
    AcpSessionHandle,
    WatchdogSettings,
    _watchdog_evidence_class,
    mcp_launch_roster,
)
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
from kiro_crew.config.loader import WatchdogConfig
from kiro_crew.testing.ids import UNALLOCATABLE_PID

# ── Oracle: /proc ────────────────────────────────────────────────────────────

# 10.0.0.10:443 as /proc/net/tcp writes it (little-endian word).
_REMOTE_PEER = "0A00000A:01BB"
_LOOPBACK_PEER = "0100007F:1F90"


def _set_tcp(fake: FakeProc, pid: int, inodes: list[str], peer: str = _REMOTE_PEER) -> None:
    d = fake.root / str(pid) / "net"
    d.mkdir(exist_ok=True)
    header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
    rows = [
        f"   {i}: 0100007F:C350 {peer} 01 00000000:00000000 00:00000000 00000000  1000        0 {ino} 1\n"
        for i, ino in enumerate(inodes)
    ]
    (d / "tcp").write_text(header + "".join(rows))
    (d / "tcp6").write_text(header)


# The server every single-server tree below runs, as its session launched it.
_SERVER = "remote"
_ROSTER = {_SERVER: (("node", "mcp-server.js"),)}


def _oracle(fake, clock, sample_min: float = 3.0, launches=lambda: _ROSTER) -> LivenessOracle:
    return LivenessOracle(
        str(fake.root), now=clock, sample_min_secs=sample_min, server_launches=launches
    )


def _mcp_tool(clock: _Clock, tool_name: str = "", server: str = _SERVER) -> ToolCallState:
    return ToolCallState(
        title="ReadInternalWebsites",
        command="{}",
        dispatch_ts=clock.t,
        tool_name=tool_name,
        mcp_server_name=server,
    )


def _two_ticks(oracle: LivenessOracle, clock: _Clock, pid: int, tool: ToolCallState):
    first = oracle.check_tool(pid, tool)
    clock.advance(2.0)
    return first, oracle.check_tool(pid, tool)


@requires_symlinks
def test_flat_tool_with_mcp_side_connection_is_tagged_remote_flat(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")  # kiro-cli's own model connection
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")  # the MCP server's remote call
    _set_tcp(fake, 300, ["555"])
    oracle = _oracle(fake, clock, sample_min=1.0)

    (v0, e0), (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert v0 == VERDICT_UNKNOWN and not e0.startswith(EVIDENCE_REMOTE_FLAT)  # baseline
    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "pid 300" in evidence


@requires_symlinks
def test_runtime_own_connection_alone_is_not_remote_flat(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.set_net_tcp(300, [])
    oracle = _oracle(fake, clock, sample_min=1.0)

    _, (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert evidence.startswith("mcp subtree flat")


@requires_symlinks
def test_sandbox_launcher_child_connection_is_not_tool_side(tmp_path):
    """Launcher (no sockets) -> kiro-cli (model connection) -> MCP server.

    kiro-cli's connection must not pass for the tool's; the grandchild's does.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200], io_bytes=10)
    fake.add_pid(200, cmdline="kiro-cli acp", children=[300], io_bytes=1000)
    fake.add_socket_fd(200, 7, "111")
    _set_tcp(fake, 200, ["111"])  # a real remote peer: only the exclusion keeps it out
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.set_net_tcp(300, [])
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = _mcp_tool(clock)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, tool)
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)

    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"])
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "pid 300" in evidence


@requires_symlinks
def test_moving_tree_with_mcp_side_connection_stays_working(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"])
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = _mcp_tool(clock)

    oracle.check_tool(100, tool)
    fake.set_io(300, 4000)  # bytes arrived on the remote call
    clock.advance(2.0)
    verdict, _ = oracle.check_tool(100, tool)
    assert verdict == VERDICT_WORKING


@requires_symlinks
def test_model_wrapping_tool_keeps_established_flat(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"])
    oracle = _oracle(fake, clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock, "use_subagent"))

    assert evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)


def _remote_call_tree(tmp_path) -> FakeProc:
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")  # kiro-cli's own model connection
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"])
    return fake


def _raises() -> int:
    raise RuntimeError("runtime is being torn down")


@requires_symlinks
def test_the_same_tree_with_a_declared_roster_is_tagged(tmp_path):
    clock = _Clock()
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "own server (pid 300)" in evidence


def _not_a_mapping():
    return [("remote", ("node", "mcp-server.js"))]


@requires_symlinks
@pytest.mark.parametrize(
    "launches",
    [None, lambda: None, _raises, _not_a_mapping, lambda: {}],
    ids=["undeclared", "declared-off", "probe-raises", "not-a-mapping", "empty"],
)
def test_remote_flat_needs_a_declared_launch_roster(tmp_path, launches):
    """Without a roster the tool's own server cannot be found, so no socket in
    the tree may shorten the window."""
    clock = _Clock()
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0, launches=launches)

    _, (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert evidence.startswith("mcp subtree flat")


@requires_symlinks
@pytest.mark.parametrize("server", ["", "unlisted"], ids=["untrusted-identity", "unknown-server"])
def test_remote_flat_needs_the_call_to_name_a_listed_server(tmp_path, server):
    """No trusted server name (fail-closed identity) or a name the roster does
    not launch: there is no subtree to read, so the full window holds."""
    clock = _Clock()
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock, server=server))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


def _two_server_tree(tmp_path) -> FakeProc:
    """kiro-cli (100) runs server ``keepalive`` (300) and server ``quiet`` (400).

    Only ``keepalive`` holds a remote connection, as a persistent keepalive or an
    ``mcp-remote`` bridge does for its whole life.
    """
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300, 400], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")  # kiro-cli's own model connection
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node keepalive.js", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"])
    fake.add_pid(400, cmdline="node quiet.js", io_bytes=3000)
    fake.set_net_tcp(400, [])
    return fake


_TWO_SERVERS = {"keepalive": (("node", "keepalive.js"),), "quiet": (("node", "quiet.js"),)}


@requires_symlinks
def test_another_servers_persistent_connection_does_not_tag_a_quiet_tool(tmp_path):
    """The case that kept the window off: server A holds a remote connection
    while server B's quiet tool runs. B's call must keep its full window."""
    clock = _Clock()
    fake = _two_server_tree(tmp_path)
    oracle = _oracle(fake, clock, sample_min=1.0, launches=lambda: _TWO_SERVERS)

    _, (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock, server="quiet"))

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert evidence.startswith("mcp subtree flat")


@requires_symlinks
def test_the_holding_servers_own_quiet_tool_is_tagged(tmp_path):
    """Same tree, but the quiet call is A's own: that is the remote-call shape."""
    clock = _Clock()
    fake = _two_server_tree(tmp_path)
    oracle = _oracle(fake, clock, sample_min=1.0, launches=lambda: _TWO_SERVERS)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock, server="keepalive"))

    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "pid 300" in evidence


@requires_symlinks
def test_a_shell_tunnel_elsewhere_in_the_tree_does_not_tag_a_quiet_tool(tmp_path):
    """A background tunnel a shell tool left running is not the server's call."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300, 500], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.set_net_tcp(300, [])
    fake.add_pid(500, cmdline="ssh -N -L 9000:db:5432 bastion", io_bytes=4000)
    fake.add_socket_fd(500, 3, "777")
    _set_tcp(fake, 500, ["777"])
    oracle = _oracle(fake, clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


@requires_symlinks
def test_a_connection_below_the_server_counts_and_names_the_server(tmp_path):
    """A launcher that starts a versioned copy of itself as its child matches
    twice: the topmost match is the server, and its child's connection
    is the server's call."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="/opt/bin/tool mcp serve x", children=[310], io_bytes=10)
    fake.set_net_tcp(300, [])
    fake.add_pid(310, cmdline="/opt/tool/1.2/tool mcp serve x", children=[320], io_bytes=20)
    fake.set_net_tcp(310, [])
    fake.add_pid(320, cmdline="/opt/x/server", io_bytes=30)
    fake.add_socket_fd(320, 4, "888")
    _set_tcp(fake, 320, ["888"])
    roster = {"x": (("/opt/bin/tool", "mcp", "serve", "x"),)}
    oracle = _oracle(fake, clock, sample_min=1.0, launches=lambda: roster)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock, server="x"))

    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "pid 320" in evidence and "own server (pid 300)" in evidence


@requires_symlinks
def test_a_second_copy_of_the_server_is_not_attributable(tmp_path):
    """Two processes launched the same way are two sessions' (or a subagent's)
    copies; which one serves this call is unknowable, so nothing is tagged."""
    clock = _Clock()
    fake = _remote_call_tree(tmp_path)
    fake.add_pid(100, children=[300, 301], io_bytes=1000)
    fake.add_pid(301, cmdline="node mcp-server.js", io_bytes=5000)
    fake.set_net_tcp(301, [])
    oracle = _oracle(fake, clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


@requires_symlinks
def test_a_process_that_fits_two_servers_is_not_attributable(tmp_path):
    clock = _Clock()
    roster = {_SERVER: (("node", "mcp-server.js"),), "twin": (("/usr/bin/node", "mcp-server.js"),)}
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0, launches=lambda: roster)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


@requires_symlinks
def test_the_roster_is_read_at_verdict_time(tmp_path):
    """A resumed session re-declares its servers; the next probe reads the new
    roster rather than the one the oracle was built with."""
    clock = _Clock()
    current: dict = {}
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0, launches=lambda: current)
    tool = _mcp_tool(clock)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, tool)
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)

    current.update(_ROSTER)
    clock.advance(2.0)
    _, evidence = oracle.check_tool(100, tool)
    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert oracle.fresh()._launch_roster() == _ROSTER


# ── Launch matching and the roster ───────────────────────────────────────────


@pytest.mark.parametrize(
    "cmdline, launch, expected",
    [
        ("node srv.js --port 1", ("node", "srv.js", "--port", "1"), True),  # as launched
        ("/usr/bin/node srv.js", ("node", "srv.js"), True),  # PATH-resolved
        ("/opt/bin/artifactory-mcp", ("artifactory-mcp",), True),  # no args
        ("/usr/bin/python3 /opt/bin/tool serve", ("/opt/bin/tool", "serve"), True),  # #! line
        ("/bin/sh /opt/bin/tool serve", ("tool", "serve"), True),  # #! via PATH
        ("node srv.js --port 2", ("node", "srv.js", "--port", "1"), False),  # other args
        ("node srv.js", ("node", "srv.js", "--port", "1"), False),  # args missing
        ("node other.js", ("node", "srv.js"), False),
        # A launcher that execs into a different argv is not recognized.
        ("/lib/ld.so --library-path /x /x/python -m pkg serve", ("/opt/bin/pkg", "serve"), False),
        ("/opt/My App/bin/srv --x", ("/opt/My App/bin/srv", "--x"), True),  # exact only
        ("/other/My App/bin/srv --x", ("/opt/My App/bin/srv", "--x"), False),
        ("", ("node", "srv.js"), False),
        ("node srv.js", (), False),
    ],
)
def test_launch_matches(cmdline, launch, expected):
    assert launch_matches(cmdline, launch) is expected


def test_launch_roster_reads_the_spec_and_the_wire_array():
    spec = {
        "mcpServers": {
            "builder": {"command": "/opt/bin/builder", "args": ["--tags", "a,b"]},
            "bare": {"command": "artifactory-mcp"},
            "remote-http": {"url": "https://example.invalid/mcp"},
            "bad-args": {"command": "x", "args": [1, 2]},
            "no-command": {"args": ["x"]},
            "shared": {"command": "kc", "args": ["mcp-core"]},
        }
    }
    wire = [
        {"name": "shared", "type": "stdio", "command": "/py", "args": ["-m", "stub"], "env": []},
        {"name": "shared", "type": "stdio", "command": "kc", "args": ["mcp-core"], "env": []},
        {"name": "sse", "type": "sse", "url": "http://127.0.0.1:1/sse", "headers": []},
        "not-an-element",
    ]

    roster, refused = mcp_launch_roster(spec, wire)

    assert roster == {
        "builder": (("/opt/bin/builder", "--tags", "a,b"),),
        "bare": (("artifactory-mcp",),),
        # Both sources stay, once each: which one the host launched is its call.
        "shared": (("kc", "mcp-core"), ("/py", "-m", "stub")),
    }
    # Remote and malformed entries have no launch to bound: not refusals.
    assert refused == 0


@pytest.mark.parametrize(
    "spec, wire",
    [(None, None), ("spec", 7), ({"mcpServers": ["x"]}, {"name": "x"}), ({"mcpServers": None}, [])],
)
def test_launch_roster_of_malformed_input_is_empty(spec, wire):
    assert mcp_launch_roster(spec, wire) == ({}, 0)


def test_launch_roster_bounds_its_server_count_and_says_so():
    spec = {"mcpServers": {f"s{i}": {"command": f"c{i}"} for i in range(BUCKET_CAP + 7)}}

    roster, refused = mcp_launch_roster(spec, None)

    assert len(roster) == BUCKET_CAP
    assert refused == 7


@pytest.mark.parametrize(
    "name, entry",
    [
        ("n" * (NAME_CAP + 1), {"command": "node"}),
        ("argc", {"command": "node", "args": ["a"] * (_LAUNCH_MAX_ARGS + 1)}),
        ("long-arg", {"command": "node", "args": ["a" * (_LAUNCH_MAX_TOKEN + 1)]}),
        ("long-command", {"command": "/" + "c" * _LAUNCH_MAX_TOKEN}),
    ],
    ids=["name", "arg-count", "arg-length", "command-length"],
)
def test_launch_roster_refuses_an_oversized_entry_whole(name, entry):
    """A retained field past its bound refuses the entry outright: a truncated
    argv would match some other process, or none, so nothing of it is kept."""
    roster, refused = mcp_launch_roster({"mcpServers": {name: entry, "ok": {"command": "x"}}}, None)

    assert roster == {"ok": (("x",),)}
    assert refused == 1


def test_launch_roster_bounds_launches_per_server():
    wire = [{"name": "srv", "command": f"c{i}"} for i in range(_LAUNCHES_PER_SERVER + 2)]

    roster, refused = mcp_launch_roster(None, wire)

    assert len(roster["srv"]) == _LAUNCHES_PER_SERVER
    assert refused == 2


def test_recording_a_refused_entry_is_logged_once(caplog):
    handle = AcpSessionHandle("sA", asyncio.Queue(), MagicMock(), watchdog=WatchdogSettings())
    spec = {"mcpServers": {f"s{i}": {"command": f"c{i}"} for i in range(BUCKET_CAP + 3)}}

    with caplog.at_level("WARNING", logger="kiro_crew.acp.session_handle"):
        handle.record_mcp_launches(spec, None)

    warnings = [r for r in caplog.records if "past the roster bounds" in r.getMessage()]
    assert len(warnings) == 1
    assert " 3 MCP server launch" in warnings[0].getMessage()


# ── Handle: what it declares to the oracle ───────────────────────────────────


def _on() -> WatchdogSettings:
    return WatchdogSettings(remote_flat_probe_secs=900.0)


def test_handle_declares_its_launch_roster_while_the_window_is_on():
    handle = AcpSessionHandle("sA", asyncio.Queue(), MagicMock(), watchdog=_on())
    assert handle._oracle._launch_roster() == {}

    handle.record_mcp_launches(
        {"mcpServers": {"srv": {"command": "node", "args": ["srv.js"]}}},
        [{"name": "core", "command": "kc", "args": ["mcp-core"]}],
    )

    expected = {"srv": (("node", "srv.js"),), "core": (("kc", "mcp-core"),)}
    assert handle._oracle._launch_roster() == expected
    assert handle._oracle.fresh()._launch_roster() == expected

    # session/load re-declares the servers: the roster is replaced, not merged.
    handle.record_mcp_launches(None, [])
    assert handle._oracle._launch_roster() == {}


def test_handle_declares_no_roster_while_the_window_is_off():
    """Off by default means no tag at all: the evidence and metric bucket stay put."""
    handle = AcpSessionHandle("sA", asyncio.Queue(), MagicMock(), watchdog=WatchdogSettings())
    handle.record_mcp_launches({"mcpServers": {"srv": {"command": "node"}}}, None)

    assert handle._oracle._launch_roster() is None

    handle._watchdog = _on()
    assert handle._oracle._launch_roster() == {"srv": (("node",),)}


def test_recording_the_roster_never_raises(monkeypatch):
    """The runtime calls this on session establishment: a failure must cost the
    narrowing, never the session."""
    from kiro_crew.acp import session_handle as session_handle_mod

    def _boom(*_a):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(session_handle_mod, "mcp_launch_roster", _boom)
    handle = AcpSessionHandle("sA", asyncio.Queue(), MagicMock(), watchdog=_on())

    handle.record_mcp_launches({"mcpServers": {}}, [])

    assert handle._oracle._launch_roster() == {}


def test_handle_leaves_the_model_wait_tenancy_undeclared():
    """The DEAD fast path keeps its reading: only the socket scan is gated."""
    rt = MagicMock()
    rt._session_queues = {"sA": object(), "sB": object()}
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())

    assert handle._oracle._tenancy is None
    assert handle._oracle._shared_tree_reason() == ""


def test_every_runtime_roster_handoff_records_the_launches():
    """Both runtime establishment paths (session/new, session/load), held by
    structure: where the wire array is final, the handle is told how its servers
    were launched, from the same spec and the same array the guard reads."""
    import inspect

    from kiro_crew.acp import runtime as runtime_mod

    lines = inspect.getsource(runtime_mod).splitlines()
    handoffs = [i for i, ln in enumerate(lines) if ".begin_session(" in ln]
    assert len(handoffs) == 2, "a runtime session-establishment path was added or removed"
    for i in handoffs:
        roster = lines[i].split(".begin_session(", 1)[1].rstrip(")").strip()
        window = [ln.strip() for ln in lines[i + 1 : i + 8]]
        assert (
            f"handle.record_mcp_launches(ref_spec, {roster})" in window
        ), f"line {i} hands over a final roster and never records its launches"


@pytest.mark.asyncio
async def test_a_runtime_session_records_the_spec_and_wire_launches():
    """A real runtime session/new: the roster holds the spec's own servers and the
    array that reached the wire, not the pre-filter one."""
    from contextlib import ExitStack
    from unittest.mock import patch

    from kiro_crew.acp.runtime import AcpRuntime, _MirroredSessionMcp
    from kiro_crew.acp.types import ACP_BACKEND_CODEX

    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_CODEX, expect_mcp_reports=False)
    proc = MagicMock()
    proc.stdout = None
    proc.stdin = MagicMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    # stdio only, so the sse element below never reaches the wire.
    rt._agent_capabilities = {"mcpCapabilities": {"http": False, "sse": False}}
    kept = {"name": "kept", "type": "stdio", "command": "x", "args": ["serve"], "env": []}
    dropped = {"name": "dropped", "type": "sse", "url": "http://127.0.0.1:1/sse", "headers": []}
    spec = {"tools": ["@kept"], "mcpServers": {"own": {"command": "node", "args": ["own.js"]}}}

    async def _send_and_await(method, params, timeout=None):
        if method == "session/new":
            return {
                "sessionId": "sid-1",
                "modes": {"currentModeId": "agent"},
                "configOptions": [
                    {"id": "mode", "options": [{"value": "read-only"}, {"value": "agent"}]}
                ],
            }
        return {}

    async def _send_request(method, params, **_kw):
        return 999

    async def _wait_for_response(_self, req_id, timeout=None):
        return {}

    async def _mirrored(*_a, **_k):
        return _MirroredSessionMcp(
            servers=[kept, dropped],
            denied_tools=frozenset(),
            stub_token="",
            derived_spec_snapshot=None,
            ref_spec=spec,
        )

    with ExitStack() as stack:
        stack.enter_context(patch.object(rt, "_send_and_await", _send_and_await))
        stack.enter_context(patch.object(rt, "send_request", _send_request))
        stack.enter_context(
            patch.object(AcpSessionHandle, "_wait_for_response", _wait_for_response)
        )
        stack.enter_context(patch.object(rt, "_mirrored_session_mcp", _mirrored))
        handle = await rt.create_session(cwd="/w", agent="kirocrew")

    assert handle._mcp_launches == {"own": (("node", "own.js"),), "kept": (("x", "serve"),)}


@requires_symlinks
def test_loopback_peer_is_not_a_remote_call(tmp_path):
    """An MCP server talking to a local service (the Kiro Crew gateway) is not
    waiting on a remote peer, so the full window holds."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_socket_fd(100, 7, "111")  # the root's own socket: pid 300 is tool side
    fake.set_net_tcp(100, ["111"])
    fake.add_pid(300, cmdline="python -m kiro_crew.mcp_core", io_bytes=2000)
    fake.add_socket_fd(300, 9, "555")
    _set_tcp(fake, 300, ["555"], peer=_LOOPBACK_PEER)
    roster = {_SERVER: (("python", "-m", "kiro_crew.mcp_core"),)}
    oracle = _oracle(fake, clock, sample_min=1.0, launches=lambda: roster)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


@pytest.mark.parametrize(
    "addr, loopback",
    [
        ("0100007F:1F90", True),  # 127.0.0.1
        ("0200007F:1F90", True),  # 127.0.0.2
        ("0A00000A:01BB", False),  # 10.0.0.10
        ("00000000000000000000000001000000:01BB", True),  # ::1
        ("0000000000000000FFFF00000100007F:01BB", True),  # ::ffff:127.0.0.1
        ("0000000000000000FFFF00000A00000A:01BB", False),  # ::ffff:10.0.0.10
        ("B80D0120000000000000000001000000:01BB", False),  # 2001:db8::1
    ],
)
def test_proc_loopback_hex(addr, loopback):
    assert liveness._is_loopback_hex(addr) is loopback


def test_no_procfs_and_no_backend_never_tags(tmp_path):
    clock = _Clock()
    oracle = LivenessOracle(
        str(tmp_path / "nonexistent"),
        now=clock,
        sample_min_secs=1.0,
        server_launches=lambda: _ROSTER,
    )
    assert oracle._own_server_remote_holder(100, _SERVER) is None


# ── Oracle: darwin backend ──────────────────────────────────────────────────


class _Backend:
    """kiro-cli (100) running server ``remote`` (300) and server ``other`` (301)."""

    def __init__(self, tcp: dict[int, int] | None, *, with_probe: bool = True) -> None:
        self.rows = {
            300: ProcessRow(pid=300, started=None, cmdline="node mcp-server.js"),
            301: ProcessRow(pid=301, started=None, cmdline="node other.js"),
        }
        self.tcp = tcp or {}
        if with_probe:
            self.established_tcp = lambda pid: self.tcp.get(pid, 0)

    def descendants(self, root_pid: int) -> list[int] | None:
        # libproc's walk lists everything below the root, never the root itself.
        return [300, 301] if root_pid == 100 else []

    def row(self, pid: int) -> ProcessRow | None:
        return self.rows.get(pid)

    def cpu_nanos(self, pid: int) -> int | None:
        return 1_000


def _darwin_oracle(backend, clock, tmp_path) -> LivenessOracle:
    return LivenessOracle(
        str(tmp_path / "nonexistent"),
        now=clock,
        sample_min_secs=1.0,
        darwin_backend=backend,
        wall_now=clock,
        steady_now_fn=clock,
        server_launches=lambda: {
            _SERVER: (("node", "mcp-server.js"),),
            "other": (("node", "other.js"),),
        },
    )


def test_darwin_tool_side_connection_is_tagged(tmp_path):
    clock = _Clock()
    # The runtime's own connection (pid 100) is never consulted on darwin.
    oracle = _darwin_oracle(_Backend({100: 3, 300: 1}), clock, tmp_path)

    _, (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert "pid 300" in evidence


def test_darwin_runtime_connection_alone_is_not_tagged(tmp_path):
    clock = _Clock()
    oracle = _darwin_oracle(_Backend({100: 3}), clock, tmp_path)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


def test_darwin_another_servers_connection_is_not_tagged(tmp_path):
    clock = _Clock()
    oracle = _darwin_oracle(_Backend({100: 3, 301: 1}), clock, tmp_path)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


def test_darwin_backend_without_socket_probe_is_not_tagged(tmp_path):
    clock = _Clock()
    oracle = _darwin_oracle(_Backend({300: 1}, with_probe=False), clock, tmp_path)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)


# ── Metric bucket ────────────────────────────────────────────────────────────


def test_remote_flat_has_its_own_metric_bucket():
    evidence = "remote_flat: mcp subtree flat, pid 300 holds an established TCP connection (io +0B cpu +0t)"
    assert _watchdog_evidence_class(evidence) == "remote_flat"


def test_remote_flat_narrowing_is_off_by_default():
    """Opt-in: turning it on by default ships as a new default value, so an
    operator who already set the key keeps exactly the seconds they set."""
    assert WatchdogConfig().remote_flat_probe_secs == 0.0
    assert WatchdogSettings().remote_flat_probe_secs == 0.0


# ── Watchdog window ──────────────────────────────────────────────────────────


def _handle(wd: WatchdogSettings, verdicts) -> AcpSessionHandle:
    rt = MagicMock()
    rt._last_activity = time.monotonic()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=wd)
    handle._turn_done.clear()
    handle._stale_eligible = False
    handle._tool_dispatched = True
    handle._inflight_tool = ToolCallState(title="ReadInternalWebsites", command="{}")
    handle._queue = _SilentQueue()  # type: ignore[assignment]
    handle._oracle.check_tool = verdicts
    return handle


async def _drain(handle: AcpSessionHandle, timeout: float) -> list:
    return [ev async for ev in handle._dispatch_events(1, timeout)]


_REMOTE = (VERDICT_UNKNOWN, "remote_flat: mcp subtree flat, pid 300 holds ... (io +0B cpu +0t)")


@pytest.mark.asyncio
async def test_remote_flat_narrows_to_the_remote_window():
    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
        remote_flat_probe_secs=0.05,
    )
    handle = _handle(wd, lambda pid, tool: _REMOTE)

    events = await _drain(handle, timeout=5.0)

    assert handle._runtime.send_notification.await_args.args[0] == "session/cancel"
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL


@pytest.mark.asyncio
async def test_remote_flat_zero_window_keeps_the_full_window():
    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
        remote_flat_probe_secs=0.0,
    )
    handle = _handle(wd, lambda pid, tool: _REMOTE)

    events = await _drain(handle, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_plain_flat_is_not_narrowed_by_the_remote_window():
    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
        remote_flat_probe_secs=0.05,
    )
    handle = _handle(wd, lambda pid, tool: (VERDICT_UNKNOWN, "mcp subtree flat (io +0B cpu +0t)"))

    events = await _drain(handle, timeout=0.3)

    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_intermittent_movement_restarts_the_remote_quiet_clock():
    """A stream that moves bytes on every other probe is never cut off, even
    though each flat probe in between carries the remote_flat tag."""
    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
        remote_flat_probe_secs=0.2,
    )
    ticks = {"n": 0}

    def alternate(pid, tool):
        ticks["n"] += 1
        if ticks["n"] % 2:
            return VERDICT_WORKING, "mcp subtree active (io +10B cpu +0t)"
        return _REMOTE

    handle = _handle(wd, alternate)

    events = await _drain(handle, timeout=0.8)

    assert ticks["n"] >= 4  # both shapes were really observed
    handle._runtime.send_notification.assert_not_awaited()
    assert all(ev.stop_reason != STOP_REASON_TOOL_STALL for ev in events)


@pytest.mark.asyncio
async def test_a_slow_working_probe_does_not_shorten_the_remote_window():
    """The quiet stretch starts when the WORKING probe returns, not when it began."""
    wd = WatchdogSettings(
        check_after_secs=0.01,
        tool_stall_suspect_secs=999.0,
        tool_stall_hard_cap_secs=999.0,
        remote_flat_probe_secs=0.3,
    )
    seen = {"n": 0, "working_done": 0.0}

    def slow_then_remote(pid, tool):
        seen["n"] += 1
        if seen["n"] == 1:
            time.sleep(0.4)
            seen["working_done"] = time.monotonic()
            return VERDICT_WORKING, "mcp subtree active (io +10B cpu +0t)"
        return _REMOTE

    handle = _handle(wd, slow_then_remote)
    cancelled_at = {}

    async def _cancel(*args, **kwargs):
        cancelled_at.setdefault("t", time.monotonic())

    handle._runtime.send_notification = AsyncMock(side_effect=_cancel)

    await _drain(handle, timeout=5.0)

    assert cancelled_at["t"] - seen["working_done"] >= 0.3


# ── libproc socket parser (fake lib; the real layout is checked by fill size) ──


# The macOS ABI, written out here rather than read from the module under test,
# so a wrong production offset cannot move the fake record along with it.
_SOCKET_FDINFO_SIZE = 792
_SOI_KIND = 256
_TCPSI_STATE = 344
_INSI_VFLAG = 288
_INSI_FADDR = 296
_V4_REMOTE = (0x1, bytes(12) + bytes([10, 0, 0, 10]))
_V4_LOOPBACK = (0x1, bytes(12) + bytes([127, 0, 0, 1]))
_V6_LOOPBACK = (0x2, bytes(15) + b"\x01")
_V6_REMOTE = (0x2, b"\x20\x01\x0d\xb8" + bytes(11) + b"\x01")


class _FakeLibproc:
    """``proc_pidinfo`` / ``proc_pidfdinfo`` over a table of fds.

    ``fds`` maps fd -> (fdtype, soi_kind, tcpsi_state, fill_size, peer).
    """

    def __init__(self, fds: dict[int, tuple[int, int, int, int]]) -> None:
        self.fds = fds

    def proc_pidinfo(self, pid, flavor, arg, buf, size):
        assert flavor == platform_compat._DARWIN_PROC_PIDLISTFDS
        records = b"".join(struct.pack("<iI", fd, meta[0]) for fd, meta in self.fds.items())
        if buf is None:
            return len(records)
        ctypes.memmove(buf, records, len(records))
        return len(records)

    def proc_pidfdinfo(self, pid, fd, flavor, buf, size):
        assert flavor == platform_compat._DARWIN_PROC_PIDFDSOCKETINFO
        _, kind, state, fill, (vflag, faddr) = self.fds[fd]
        raw = bytearray(size)
        struct.pack_into("<i", raw, _SOI_KIND, kind)
        struct.pack_into("<i", raw, _TCPSI_STATE, state)
        raw[_INSI_VFLAG] = vflag
        raw[_INSI_FADDR : _INSI_FADDR + 16] = faddr
        ctypes.memmove(buf, bytes(raw), size)
        return fill


def test_libproc_counts_only_established_tcp_sockets(monkeypatch):
    size, sock, tcp, est = _SOCKET_FDINFO_SIZE, 2, 2, 4
    assert platform_compat._DARWIN_SOCKET_FDINFO_SIZE == size
    lib = _FakeLibproc(
        {
            3: (1, 0, 0, size, _V4_REMOTE),  # a vnode, not a socket
            4: (sock, tcp, est, size, _V4_REMOTE),  # counted
            5: (sock, tcp, 1, size, _V4_REMOTE),  # LISTEN
            6: (sock, 1, est, size, _V4_REMOTE),  # not TCP (a unix socket)
            7: (sock, tcp, est, size - 8, _V4_REMOTE),  # wrong fill size: refused
            8: (sock, tcp, est, size, _V4_LOOPBACK),  # loopback peer
            9: (sock, tcp, est, size, _V6_LOOPBACK),  # ::1
            10: (sock, tcp, est, size, _V6_REMOTE),  # counted
            11: (sock, tcp, est, size, (0x0, bytes(16))),  # unknown family
        }
    )
    monkeypatch.setattr(platform_compat, "_darwin_libproc_fd_handle", lambda: lib)
    assert platform_compat.darwin_established_tcp_count(123) == 2


def test_libproc_unreadable_listing_is_none(monkeypatch):
    class _Refuses:
        def proc_pidinfo(self, *args):
            return 0

    monkeypatch.setattr(platform_compat, "_darwin_libproc_fd_handle", lambda: _Refuses())
    assert platform_compat.darwin_established_tcp_count(123) is None


# ── Real libproc (macOS lane only) ───────────────────────────────────────────


def _primary_ipv4() -> str | None:
    """This host's non-loopback IPv4 address, or None when it has none.

    A connected UDP socket picks the outbound interface without sending a
    packet, so no network access is needed.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))
        addr = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if addr.startswith("127.") or addr == "0.0.0.0" else addr


def _tcp_pair(host: str):
    import socket

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind((host, 0))
    server.listen(1)
    client = socket.create_connection(server.getsockname(), timeout=5)
    accepted, _ = server.accept()
    return server, client, accepted


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
def test_real_libproc_counts_a_non_loopback_connection_and_not_a_loopback_one():
    """The ABI offsets above are hand-derived; this runs them against the kernel."""
    import os

    host = _primary_ipv4()
    if host is None:
        pytest.skip("no non-loopback IPv4 address on this host")
    baseline = platform_compat.darwin_established_tcp_count(os.getpid())
    assert baseline is not None

    loop = _tcp_pair("127.0.0.1")
    try:
        assert platform_compat.darwin_established_tcp_count(os.getpid()) == baseline
    finally:
        for s in loop:
            s.close()

    remote = _tcp_pair(host)
    try:
        # Both ends live in this process and each names the other as a
        # non-loopback peer.
        assert platform_compat.darwin_established_tcp_count(os.getpid()) == baseline + 2
    finally:
        for s in remote:
            s.close()
