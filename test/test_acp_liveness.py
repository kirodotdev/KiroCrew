"""Unit tests for the per-session liveness oracle (``acp/liveness.py``).

The oracle is the detector behind the verdict-driven watchdogs in
``session_handle._dispatch_events``: WORKING is never acted on, DEAD acts
immediately, UNKNOWN falls back to (non-lethal) timeouts. These tests exercise
the /proc evidence paths against a fake proc tree — no real processes.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.acp.liveness import (
    CHILD_EXIT_GRACE_SECS,
    EVIDENCE_ESTABLISHED_FLAT,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
)


class _Clock:
    """Injectable monotonic clock."""

    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class FakeProc:
    """Builds a fake /proc tree under tmp_path."""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        (root / "uptime").write_text("5000.00 9000.00\n")

    def add_pid(
        self,
        pid: int,
        *,
        state: str = "S",
        cmdline: str = "",
        children: list[int] | None = None,
        cpu: int = 0,
        io_bytes: int = 0,
        wchan: str = "",
        starttime: float = 10_000_000.0,
    ) -> None:
        # Default starttime is huge (in ticks) so process_age_secs computes a
        # negative age → "started after dispatch" → the pre-existing-lookalike
        # start-time guard accepts the match regardless of the host's HZ.
        d = self.root / str(pid)
        (d / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
        kids = " ".join(str(c) for c in (children or []))
        (d / "task" / str(pid) / "children").write_text(kids)
        # stat: pid (comm) state ... utime(14) stime(15) ... starttime(22)
        fields = ["0"] * 50
        fields[0] = state          # field 3
        fields[11] = str(cpu)      # utime (field 14)
        fields[12] = "0"           # stime (field 15)
        fields[19] = str(int(starttime))  # starttime (field 22)
        (d / "stat").write_text(f"{pid} (fake proc) {' '.join(fields)}\n")
        (d / "cmdline").write_bytes(cmdline.replace(" ", "\0").encode() + b"\0")
        (d / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")
        (d / "wchan").write_text(wchan)
        (d / "fd").mkdir(exist_ok=True)

    def set_io(self, pid: int, io_bytes: int) -> None:
        (self.root / str(pid) / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")

    def remove_pid(self, pid: int) -> None:
        import shutil

        shutil.rmtree(self.root / str(pid), ignore_errors=True)

    def set_blocked_read(self, pid: int, fd: int, target: str) -> None:
        d = self.root / str(pid)
        (d / "syscall").write_text(f"0 0x{fd:x} 0x0 0x0 0x0 0x0 0x0\n")
        # fd symlink target
        link = d / "fd" / str(fd)
        link.parent.mkdir(exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    def add_socket_fd(self, pid: int, fd: int, inode: str) -> None:
        link = self.root / str(pid) / "fd" / str(fd)
        link.parent.mkdir(exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        link.symlink_to(f"socket:[{inode}]")

    def set_net_tcp(self, pid: int, established_inodes: list[str]) -> None:
        d = self.root / str(pid) / "net"
        d.mkdir(exist_ok=True)
        header = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"
        lines = [header]
        for i, ino in enumerate(established_inodes):
            lines.append(
                f"   {i}: 0100007F:1F90 0100007F:0050 01 00000000:00000000 00:00000000 00000000  1000        0 {ino} 1\n"
            )
        (d / "tcp").write_text("".join(lines))
        (d / "tcp6").write_text(header)


def _oracle(fake: FakeProc, clock: _Clock, sample_min: float = 3.0) -> LivenessOracle:
    return LivenessOracle(str(fake.root), now=clock, sample_min_secs=sample_min)


# ── Shell tool evidence ──────────────────────────────────────────────────────


def _shell_tool(command: str, clock: _Clock) -> ToolCallState:
    return ToolCallState(title="bash", command=command, dispatch_ts=clock.t, is_shell=True)


def test_matched_live_shell_child_is_working(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200], cmdline="kiro-cli acp")
    fake.add_pid(200, cmdline="bash -c long-build release > build.log 2>&1")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_WORKING
    assert "200" in evidence


def test_matched_child_exit_flips_dead_after_grace(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200])
    fake.add_pid(200, cmdline="bash -c long-build release > build.log 2>&1")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    assert oracle.check_tool(100, tool)[0] == VERDICT_WORKING  # tracked now

    fake.remove_pid(200)
    clock.advance(1.0)
    verdict, _ = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN  # inside the exit grace

    clock.advance(CHILD_EXIT_GRACE_SECS + 1.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_DEAD
    assert "exited" in evidence


def test_zombie_child_is_not_working(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200])
    fake.add_pid(200, state="Z", cmdline="bash -c long-build release")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release", clock)

    verdict, _ = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN  # zombie never matches as alive


def test_no_matching_child_is_unknown(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200])
    fake.add_pid(200, cmdline="some-unrelated-daemon --serve")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    assert "no matching" in evidence


def test_stuck_input_detected_on_flat_tty_blocked_child(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200])
    fake.add_pid(200, cmdline="bash -c ssh remote-host uptime", wchan="n_tty_read", io_bytes=500)
    fake.set_blocked_read(200, 3, "/dev/tty")
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = _shell_tool("ssh remote-host uptime", clock)

    # First check: matches + baseline sample (cannot claim flat yet).
    assert oracle.check_tool(100, tool)[0] == VERDICT_WORKING
    # Second check past sample interval, counters unchanged → flat + tty-blocked.
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_STUCK_INPUT
    assert "stuck_input" in evidence and "/dev/tty" in evidence


def test_socket_blocked_child_is_not_stuck(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[200])
    # wchan wait_woken but blocked fd is a SOCKET → network wait, not stuck.
    fake.add_pid(200, cmdline="bash -c curl https://big-download", wchan="wait_woken", io_bytes=500)
    fake.set_blocked_read(200, 4, "socket:[5555]")
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = _shell_tool("curl https://big-download", clock)

    assert oracle.check_tool(100, tool)[0] == VERDICT_WORKING
    clock.advance(2.0)
    verdict, _ = oracle.check_tool(100, tool)
    assert verdict == VERDICT_WORKING  # live child, no stuck evidence


# ── Wait tool declared duration ──────────────────────────────────────────────


def test_wait_tool_working_until_declared_duration(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100)
    oracle = _oracle(fake, clock)
    tool = ToolCallState(
        title="wait", command='{"seconds": 300, "reason": "poll"}',
        dispatch_ts=clock.t, is_shell=False,
    )

    clock.advance(299.0)
    assert oracle.check_tool(100, tool)[0] == VERDICT_WORKING
    clock.advance(300.0)  # past 300 + 120 slack
    assert oracle.check_tool(100, tool)[0] == VERDICT_UNKNOWN


# ── MCP tool + model-wait movement sampling ──────────────────────────────────


def test_mcp_tool_moving_counters_working(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = ToolCallState(title="ReadInternalWebsites", command="{}", dispatch_ts=clock.t)

    assert oracle.check_tool(100, tool)[0] == VERDICT_UNKNOWN  # baseline sample
    fake.set_io(300, 9000)
    clock.advance(2.0)
    verdict, _ = oracle.check_tool(100, tool)
    assert verdict == VERDICT_WORKING


def test_model_wait_bytes_flowing_working(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    oracle = _oracle(fake, clock, sample_min=1.0)

    assert oracle.check_model_wait(100)[0] == VERDICT_UNKNOWN  # baseline
    fake.set_io(100, 5000)
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(100)
    assert verdict == VERDICT_WORKING
    assert "backend activity" in evidence


def test_model_wait_flat_no_socket_is_dead(tmp_path):
    """Flat counters + no established backend socket = the done-but-lost-frame
    wedge signature → DEAD (probed immediately, non-lethally)."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    fake.set_net_tcp(100, [])  # no established sockets
    oracle = _oracle(fake, clock, sample_min=1.0)

    oracle.check_model_wait(100)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(100)
    assert verdict == VERDICT_DEAD
    assert "no established backend socket" in evidence


def test_model_wait_flat_with_established_socket_is_unknown_tagged(tmp_path):
    """Flat counters but an established backend connection → probably a
    non-streamed server-side think → UNKNOWN with the established_flat tag
    (the caller extends the probe window)."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    fake.add_socket_fd(100, 7, "31337")
    fake.set_net_tcp(100, ["31337"])
    oracle = _oracle(fake, clock, sample_min=1.0)

    oracle.check_model_wait(100)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(100)
    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)


# ── Fail-safe behavior ───────────────────────────────────────────────────────


def test_missing_proc_degrades_to_unknown(tmp_path):
    clock = _Clock()
    oracle = LivenessOracle(str(tmp_path / "nonexistent"), now=clock)
    tool = ToolCallState(title="bash", command="ls", dispatch_ts=clock.t, is_shell=True)

    assert oracle.check_tool(100, tool)[0] == VERDICT_UNKNOWN
    assert oracle.check_model_wait(100)[0] == VERDICT_UNKNOWN


def test_no_pid_is_unknown(tmp_path):
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    oracle = _oracle(fake, clock)
    tool = ToolCallState(title="bash", command="ls -la /tmp", dispatch_ts=clock.t, is_shell=True)

    assert oracle.check_tool(None, tool)[0] == VERDICT_UNKNOWN
    assert oracle.check_model_wait(None)[0] == VERDICT_UNKNOWN


def test_helpers_never_raise_on_garbage(tmp_path):
    """A malformed /proc entry must degrade, never raise."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    d = fake.root / "666"
    (d / "task" / "666").mkdir(parents=True)
    (d / "stat").write_text("garbage without parens\n")
    (d / "io").write_text("nonsense\n")
    fake.add_pid(100, children=[666])
    oracle = _oracle(fake, clock)
    tool = ToolCallState(title="bash", command="whatever-cmd", dispatch_ts=clock.t, is_shell=True)

    verdict, _ = oracle.check_tool(100, tool)
    assert verdict in (VERDICT_UNKNOWN, VERDICT_WORKING)
