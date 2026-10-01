"""The remote_flat tag: an MCP tool blocked on its own remote call.

Oracle side: a genuinely flat tool subtree in which a TOOL-side process (not
the kiro-cli runtime, and not the sandbox launcher's kiro-cli child) holds an
established TCP connection is tagged ``remote_flat``. Watchdog side: that tag
narrows the UNKNOWN window to ``watchdog.remote_flat_probe_secs``, measured from
the later of the last own frame and the last WORKING reading.
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
)
from kiro_crew.acp.session_handle import (
    AcpSessionHandle,
    WatchdogSettings,
    _watchdog_evidence_class,
)
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
from kiro_crew.config.loader import WatchdogConfig

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


def _oracle(fake, clock, sample_min: float = 3.0, tenancy=lambda: 1) -> LivenessOracle:
    return LivenessOracle(
        str(fake.root), now=clock, sample_min_secs=sample_min, socket_tenancy=tenancy
    )


def _mcp_tool(clock: _Clock, tool_name: str = "") -> ToolCallState:
    return ToolCallState(
        title="ReadInternalWebsites", command="{}", dispatch_ts=clock.t, tool_name=tool_name
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
def test_the_same_tree_with_a_sole_tenant_is_tagged(tmp_path):
    clock = _Clock()
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0)

    _, (_, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert evidence.startswith(EVIDENCE_REMOTE_FLAT)


@requires_symlinks
@pytest.mark.parametrize(
    "tenancy",
    [None, lambda: 2, lambda: 0, _raises],
    ids=["undeclared", "co-tenant", "unreadable-count", "probe-raises"],
)
def test_remote_flat_needs_a_declared_sole_tenant(tmp_path, tenancy):
    """The scan reads the whole tree, so a co-tenant's (or a riding subagent's)
    remote call must not shorten this session's window."""
    clock = _Clock()
    oracle = _oracle(_remote_call_tree(tmp_path), clock, sample_min=1.0, tenancy=tenancy)

    _, (verdict, evidence) = _two_ticks(oracle, clock, 100, _mcp_tool(clock))

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_REMOTE_FLAT)
    assert evidence.startswith("mcp subtree flat")


def test_handle_declares_its_runtime_tenancy():
    rt = MagicMock()
    rt._session_queues = {"sA": object(), "sB": object()}
    rt._session_inits_in_flight = 1
    handle = AcpSessionHandle(
        "sA", asyncio.Queue(), rt, watchdog=WatchdogSettings(remote_flat_probe_secs=900.0)
    )

    assert handle._runtime_tenancy() == 3
    assert handle._oracle._socket_tenancy() == 3
    assert handle._oracle.fresh()._socket_tenancy() == 3

    rt._session_queues = {"sA": object()}
    rt._session_inits_in_flight = 0
    assert handle._oracle._socket_tenancy() == 1


def test_handle_declares_no_socket_tenancy_while_the_window_is_off():
    """Off by default means no tag at all: the evidence and metric bucket stay put."""
    rt = MagicMock()
    rt._session_queues = {"sA": object()}
    rt._session_inits_in_flight = 0
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())

    assert handle._runtime_tenancy() == 1
    assert handle._oracle._socket_tenancy() is None

    handle._watchdog = WatchdogSettings(remote_flat_probe_secs=900.0)
    assert handle._oracle._socket_tenancy() == 1


def test_handle_counts_an_unsettled_start_as_a_tenant():
    """A timed-out session/new still owns a tree after its init scope closes."""
    rt = MagicMock()
    rt._session_queues = {"sA": object()}
    rt._session_inits_in_flight = 0
    rt._start_collectors = {7: object()}
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())

    assert handle._runtime_tenancy() == 2

    rt._start_collectors = {}
    assert handle._runtime_tenancy() == 1


def test_tenancy_reads_the_real_runtime_session_tables():
    """Pins the three AcpRuntime attributes the count reads, so a rename there
    cannot silently fall back to a default that reads as a sole tenant."""
    from kiro_crew.acp.runtime import AcpRuntime

    rt = AcpRuntime()
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())
    assert handle._runtime_tenancy() == 0

    rt._session_queues["sA"] = asyncio.Queue()
    rt._session_queues["sB"] = asyncio.Queue()
    rt._session_inits_in_flight += 1
    rt._start_collectors[7] = object()  # type: ignore[assignment]
    assert handle._runtime_tenancy() == 4


def test_handle_leaves_the_model_wait_tenancy_undeclared():
    """The DEAD fast path keeps its reading: only the socket scan is gated."""
    rt = MagicMock()
    rt._session_queues = {"sA": object(), "sB": object()}
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())

    assert handle._oracle._tenancy is None
    assert handle._oracle._shared_tree_reason() == ""


def test_handle_without_a_session_table_is_unreadable():
    rt = MagicMock(spec=["pid"])
    handle = AcpSessionHandle("sA", asyncio.Queue(), rt, watchdog=WatchdogSettings())

    assert handle._runtime_tenancy() is None


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
    oracle = _oracle(fake, clock, sample_min=1.0)

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
    oracle = LivenessOracle(str(tmp_path / "nonexistent"), now=clock, sample_min_secs=1.0)
    assert oracle._tool_side_established(100) is None


# ── Oracle: darwin backend ──────────────────────────────────────────────────


class _Backend:
    def __init__(self, tcp: dict[int, int] | None, *, with_probe: bool = True) -> None:
        self.rows = {300: ProcessRow(pid=300, started=None, cmdline="node mcp-server.js")}
        self.tcp = tcp or {}
        if with_probe:
            self.established_tcp = lambda pid: self.tcp.get(pid, 0)

    def descendants(self, root_pid: int) -> list[int] | None:
        return [300]

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
        socket_tenancy=lambda: 1,
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
    """The holder is not yet tied to the in-flight tool's own MCP server, so
    the narrowing is opt-in."""
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
