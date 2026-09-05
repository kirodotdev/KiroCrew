"""Unit tests for the per-session liveness oracle (``acp/liveness.py``).

The oracle is the detector behind the verdict-driven watchdogs in
``session_handle._dispatch_events``: WORKING is never acted on, DEAD acts
immediately, UNKNOWN falls back to (non-lethal) timeouts. These tests exercise
the /proc evidence paths against a fake proc tree — no real processes.
"""

from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import requires_symlinks
from kiro_crew.acp import liveness
from kiro_crew.acp.liveness import (
    CHILD_EXIT_GRACE_SECS,
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_SHELL_CHILD_ABSENT,
    VERDICT_DEAD,
    VERDICT_STUCK_INPUT,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
    consult_offloaded,
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
        # Default starttime is huge (in ticks) so its boot-clock start lands far
        # after any dispatch stamp these tests use → "started after dispatch" →
        # the pre-existing-lookalike guard accepts the match on any host HZ.
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
    """A shell ToolCallState dispatched now.

    ``dispatch_boot_ts`` is the boot-clock stamp production takes at
    EVENT_TOOL_CALL; the fake tree has no suspend, so it coincides with the fake
    monotonic clock. ``FakeProc.add_pid``'s huge default starttime therefore reads
    as started-after-dispatch on any host HZ, and ``starttime=0`` reads as older.
    """
    return ToolCallState(
        title="bash",
        command=command,
        dispatch_ts=clock.t,
        dispatch_boot_ts=clock.t,
        is_shell=True,
    )


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


# ── The never-matched fork: absent child vs unrecognized live child (#4840) ──

# Dating a process against its dispatch needs the platform tick rate, which does
# not exist off Linux (Windows has no os.sysconf, and no /proc for the oracle to
# read either). There the attribution fails open, so these start-time cases have
# nothing to assert; the fail-open itself is pinned by
# ``test_no_tick_rate_fails_open_instead_of_claiming_absence``, which runs
# everywhere.
_needs_tick_rate = pytest.mark.skipif(
    not hasattr(os, "sysconf"),
    reason="start-time attribution needs SC_CLK_TCK (Linux); fail-open covered separately",
)


def _hz() -> int:
    return os.sysconf("SC_CLK_TCK")


def _old_pid(fake: FakeProc, pid: int, **kw) -> None:
    """A descendant that started LONG before any tool dispatch.

    ``FakeProc.add_pid`` defaults to a huge starttime so a process reads as
    "started after dispatch" on any host HZ; these tests need the opposite, so
    the starttime is 0 ticks, i.e. boot second 0.
    """
    fake.add_pid(pid, starttime=0.0, **kw)


def test_no_tick_rate_fails_open_instead_of_claiming_absence(tmp_path, monkeypatch):
    """Off Linux the attribution is unavailable, and that must read as unknown.

    ``os.sysconf`` does not exist on Windows (AttributeError, not OSError), which
    once escaped as far as ``check_tool``'s catch-all and turned every shell
    verdict into "oracle error". With the tick rate unreadable every descendant
    reads as possibly-this-tool's, so the absence tag cannot fire -- the same
    fail-open direction as a missing boot stamp.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    monkeypatch.setattr(liveness, "process_start_boot_secs", lambda _ticks: None)
    oracle = _oracle(fake, clock)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence == "no matching shell child", evidence


def test_tick_rate_lookup_survives_a_platform_without_sysconf(monkeypatch):
    """The helper answers None rather than raising where os.sysconf is absent."""
    monkeypatch.delattr(os, "sysconf", raising=False)

    assert liveness.process_start_boot_secs(12345.0) is None


@_needs_tick_rate
def test_absent_shell_child_is_tagged_when_every_descendant_predates_dispatch(tmp_path):
    """#4840: the sub-second command whose result frame was lost.

    The oracle's first look happens at check_after_secs, by which time an ``ls |
    grep | wc`` child is long gone — it is never observed alive, so the
    matched-then-gone DEAD branch cannot fire. The runtime's tree still holds its
    long-lived MCP stub children, all of them older than the dispatch, which is
    positive evidence that nothing was started for this tool.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 202], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    _old_pid(fake, 202, cmdline="python -m kiro_crew.mcp_gateway.stub --server slack")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)
    clock.advance(61.0)  # first look lands after the tool-idle threshold

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN  # inferred absence is never a kill
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_unmatched_but_young_descendant_keeps_the_full_window(tmp_path):
    """The match heuristic missing live work must NOT read as absence.

    A shell command that exec'd away (or whose cached input was redacted past
    any usable fragment) leaves a descendant started after the dispatch. That is
    the case build-scale forbearance exists for, so it keeps the plain evidence.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    fake.add_pid(300, cmdline="/opt/vendor/bin/opaque-worker --serve")  # young
    oracle = _oracle(fake, clock)
    tool = _shell_tool("[REDACTED-CREDENTIAL] x", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_readable_but_empty_tree_is_an_absent_child(tmp_path):
    """A runtime with no MCP servers has no descendants at all, and its child
    list still reads (as empty) — which IS evidence: nothing is running, so the
    tag must fire rather than falling back to the full window."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, cmdline="kiro-cli acp")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_unreadable_child_list_keeps_the_full_window(tmp_path):
    """An unobservable tree is not an absent child.

    Without a readable ``/proc/<pid>/task/<tid>/children`` (no procfs, a kernel
    without CONFIG_PROC_CHILDREN, a sandbox that hides the subtree) the walk
    returns the runtime alone — the same shape as a genuinely empty tree, so
    absence must not be claimed from it.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, cmdline="kiro-cli acp")
    (fake.root / "100" / "task" / "100" / "children").unlink()
    oracle = _oracle(fake, clock)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_every_production_dispatch_answers_both_attribution_fields():
    """Drift ratchet: both attribution inputs fail OPEN when omitted.

    Without ``dispatch_boot_ts`` a never-matched shell tool silently returns to
    the full build window; without ``dispatch_parked_secs`` a frame queued behind
    an approval disowns its own live child. Neither shows up as a test failure
    anywhere else, so every production ``ToolCallState`` construction must answer
    both explicitly -- including with a 0.0 that says "this path cannot park".
    """
    import ast

    import kiro_crew.acp.liveness as liveness_mod

    required = {"dispatch_boot_ts", "dispatch_parked_secs"}
    package_root = Path(liveness_mod.__file__).resolve().parents[1]
    sites: list[tuple[str, int, set[str]]] = []
    for path in package_root.rglob("*.py"):
        if "/tests/" in path.as_posix():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "ToolCallState(" not in text:
            continue
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ToolCallState":
                passed = {kw.arg for kw in node.keywords if kw.arg}
                sites.append((path.name, node.lineno, required - passed))

    assert sites, "no production ToolCallState construction found - the ratchet has gone blind"
    missing = [(name, line, sorted(gap)) for name, line, gap in sites if gap]
    assert not missing, f"dispatch sites not answering an attribution field: {missing}"


def test_missing_boot_stamp_keeps_the_full_window(tmp_path):
    """No boot-clock stamp, no absence claim.

    ``boottime_now()`` answers None where the clock is unavailable, so every
    descendant reads as possibly-this-tool's and the window is untouched.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    oracle = _oracle(fake, clock)
    tool = ToolCallState(
        title="bash",
        command="ls /some/dir | grep needle | wc -l",
        dispatch_ts=clock.t,
        dispatch_boot_ts=None,
        is_shell=True,
    )

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


@_needs_tick_rate
def test_a_suspend_after_dispatch_does_not_disown_a_live_child(tmp_path):
    """Regression: a host suspend must not age a live child out of its dispatch.

    ``/proc`` dates processes on the boot clock, which counts suspended time;
    ``time.monotonic()`` does not. Deriving the start as ``monotonic_now - age``
    therefore placed a child a full suspend EARLIER than it started, so a laptop
    resumed mid-command saw its live child rejected as a pre-existing lookalike
    AND counted as "nothing started since dispatch" -- the absent-child narrowing
    would then cancel a running command. Both sides now read the boot clock, so
    the suspend moves neither.

    Here: dispatched at boot 5000, child spawned at boot 5001, then the host
    suspends 300s (uptime jumps to 5301 while the monotonic clock does not move).
    """
    hz = _hz()
    clock = _Clock()  # monotonic: unmoved by the suspend
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    fake.add_pid(300, cmdline="bash -c long-build release", starttime=5001.0 * hz)
    (fake.root / "uptime").write_text("5301.00 9000.00\n")  # boot clock after resume
    oracle = _oracle(fake, clock)
    tool = ToolCallState(
        title="bash",
        command="long-build release",
        dispatch_ts=clock.t,
        dispatch_boot_ts=5000.0,
        is_shell=True,
    )

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_WORKING, evidence
    assert "300" in evidence


@_needs_tick_rate
def test_zombie_only_tree_is_absent_not_live(tmp_path):
    """A zombie is the exited child, not a running one.

    The reaped-but-not-yet-collected shell child must not buy the full window
    back: zombies are skipped, so the tree reads as having no live descendant
    started since dispatch.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    fake.add_pid(300, state="Z", cmdline="bash -c ls /some/dir | grep needle | wc -l")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_live_matched_child_still_wins_over_the_absence_test(tmp_path):
    """The absence pass must not disturb the WORKING path: a matched live child
    is still WORKING even though its older siblings fill the tree."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    fake.add_pid(300, cmdline="bash -c long-build release > build.log 2>&1")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_WORKING
    assert "300" in evidence


@_needs_tick_rate
def test_a_stamp_taken_late_still_owns_its_child(tmp_path):
    """Regression: a frame queued behind an approval must not disown its child.

    The stamp is taken when the tool_call frame is PROCESSED. While the dispatch
    loop is parked on a consumer-side await (an approval, an IM send, a hook) the
    runtime can already have spawned, so the child predates its own stamp -- and
    an UNMATCHABLE one (a command redacted past any usable fragment, or a shell
    that exec'd away) would then be counted as "nothing started since dispatch".
    The loop measures that park, so the attribution window opens by exactly it.

    Here: 150s of banked parking, child spawned at boot 4900, stamp taken at
    5000 -- 100s "before" its own dispatch, inside the 10s + 150s window.
    """
    hz = _hz()
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    fake.add_pid(300, cmdline="/opt/vendor/bin/opaque-worker", starttime=4900.0 * hz)
    oracle = _oracle(fake, clock)
    tool = ToolCallState(
        title="bash",
        command="[REDACTED-CREDENTIAL] x",  # nothing matchable survives
        dispatch_ts=clock.t,
        dispatch_boot_ts=5000.0,
        dispatch_parked_secs=150.0,
        is_shell=True,
    )

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


@_needs_tick_rate
def test_matching_but_older_child_vetoes_the_absence_claim(tmp_path):
    """A live process that looks like the command is not evidence of absence.

    The dispatch stamp is taken when the tool_call frame is PROCESSED, and the
    dispatch loop's consumer can park for minutes on an approval, an IM send or
    a hook while kiro-cli has already spawned. Such a child predates its own
    stamp, so it is still refused as a match (it may equally be a coincidental
    lookalike) -- but it must keep the full window rather than being reported as
    nothing-is-running.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[201, 300], cmdline="kiro-cli acp")
    _old_pid(fake, 201, cmdline="python -m kiro_crew.mcp_gateway.stub --server github")
    _old_pid(fake, 300, cmdline="bash -c long-build release > build.log 2>&1")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, evidence = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


@_needs_tick_rate
def test_pre_existing_lookalike_is_still_rejected_as_a_match(tmp_path):
    """Start-time attribution moved into a helper — the lookalike guard it came
    from must keep rejecting a matching process that predates the dispatch."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    _old_pid(fake, 100, children=[200], cmdline="kiro-cli acp")
    _old_pid(fake, 200, cmdline="bash -c long-build release > build.log 2>&1")
    oracle = _oracle(fake, clock)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, _ = oracle.check_tool(100, tool)

    assert verdict == VERDICT_UNKNOWN  # matching cmdline, but it predates us


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


@requires_symlinks
def test_mcp_tool_flat_with_runtime_backend_socket_is_tagged_established_flat(tmp_path):
    """A genuinely flat tool subtree whose RUNTIME process holds an established
    backend socket is the LLM-turn-inside-a-tool shape (e.g. use_subagent
    wrapping a model turn) → UNKNOWN with the established_flat tag so the
    caller narrows the window to the model-silent budget.

    Requires positive tool-identity attribution: tool_name must be a known
    model-wrapping tool (use_subagent) for the tag to apply."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    fake.add_socket_fd(100, 7, "31337")
    fake.set_net_tcp(100, ["31337"])
    oracle = _oracle(fake, clock, sample_min=1.0)
    # tool_name="use_subagent" → positive model-wrapping attribution
    tool = ToolCallState(title="use_subagent", command="{}", dispatch_ts=clock.t,
                         tool_name="use_subagent")

    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN  # baseline sample — never tagged
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)


@requires_symlinks
def test_mcp_tool_flat_ordinary_tool_with_runtime_socket_not_tagged(tmp_path):
    """F1 regression: a quiet ordinary MCP tool (no model-wrapping tool_name)
    running while the runtime holds a persistent ESTABLISHED socket must NOT
    receive the established_flat tag.

    The runtime may hold a keepalive socket to the model service at all times;
    the socket's presence alone is not proof that the *current tool* is waiting
    on a model response. Without positive tool-identity attribution (tool_name
    in _MODEL_WRAPPING_TOOLS) the oracle must fall back to plain mcp_subtree_flat
    so the full 1h build-scale window governs, not the 15-min model-silent budget.
    """
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    # Runtime holds a persistent backend socket (model-service keepalive)
    fake.add_socket_fd(100, 7, "31337")
    fake.set_net_tcp(100, ["31337"])
    oracle = _oracle(fake, clock, sample_min=1.0)
    # tool_name="" → no model-wrapping attribution; plain MCP call
    tool = ToolCallState(title="ReadInternalWebsites", command="{}", dispatch_ts=clock.t,
                         tool_name="")

    oracle.check_tool(100, tool)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    # Must NOT be tagged established_flat for a non-model-wrapping tool
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT), (
        "established_flat must not fire for a tool without model-wrapping attribution"
    )
    assert "mcp subtree flat" in evidence


@requires_symlinks
def test_mcp_tool_flat_without_runtime_socket_keeps_plain_evidence(tmp_path):
    """A quiet MCP tool with no established socket on the runtime process keeps
    the untagged flat evidence — the full build-scale tool windows apply. Also
    covers the socket-on-a-DESCENDANT case: an MCP server blocked on its own
    remote call must not read as an LLM wait."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, children=[300], io_bytes=1000)
    fake.add_pid(300, cmdline="node mcp-server.js", io_bytes=2000)
    fake.set_net_tcp(100, [])  # runtime itself: no established sockets
    # The DESCENDANT holds an established socket (its own remote call) — this
    # must NOT trigger the tag; only the runtime's own backend connection is
    # LLM-wait evidence.
    fake.add_socket_fd(300, 7, "555")
    fake.set_net_tcp(300, ["555"])
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = ToolCallState(title="ReadInternalWebsites", command="{}", dispatch_ts=clock.t)

    oracle.check_tool(100, tool)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)
    assert "mcp subtree flat" in evidence


@requires_symlinks
def test_mcp_tool_baseline_sample_never_tagged(tmp_path):
    """The first (baseline) tick reports "sampling" — no real flatness delta
    exists yet, so the established_flat tag must not fire even with a live
    backend socket on the runtime."""
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    fake.add_socket_fd(100, 7, "31337")
    fake.set_net_tcp(100, ["31337"])
    oracle = _oracle(fake, clock, sample_min=1.0)
    tool = ToolCallState(title="use_subagent", command="{}", dispatch_ts=clock.t)

    verdict, evidence = oracle.check_tool(100, tool)
    assert verdict == VERDICT_UNKNOWN
    assert "sampling" in evidence
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT)


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


@requires_symlinks
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


# ── Portable model-wait fallback (no procfs) — issue #8520 ───────────────────
#
# macOS and Windows have no ``/proc``, so the tree walk reads NO counter at all
# and the verdict was "unknown: no readable counters" — which the AcpClient's
# stale cutoff treats as reap. Absent evidence must not read as death on the
# platform most third-party backends run on.


def _no_procfs_oracle(clock: _Clock, tmp_path) -> LivenessOracle:
    """An oracle whose ``/proc`` root does not exist — i.e. any non-Linux host."""
    return LivenessOracle(str(tmp_path / "nonexistent"), now=clock, sample_min_secs=1.0)


def test_model_wait_portable_cpu_delta_is_working(tmp_path, monkeypatch):
    """A CPU delta on a live pid forgives silence where the /proc walk is blind."""
    from kiro_crew import platform_compat

    clock = _Clock()
    cpu = {"ns": 5_000_000_000}
    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda _pid: cpu["ns"])
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: True)
    oracle = _no_procfs_oracle(clock, tmp_path)

    assert oracle.check_model_wait(100) == (VERDICT_UNKNOWN, "sampling")  # baseline
    cpu["ns"] += 250_000_000
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(100)
    assert verdict == VERDICT_WORKING
    assert "backend activity" in evidence


def test_model_wait_portable_flat_cpu_stays_unknown(tmp_path, monkeypatch):
    """An idle process is not evidence of work — today's cutoff is preserved."""
    from kiro_crew import platform_compat

    clock = _Clock()
    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda _pid: 5_000_000_000)
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: True)
    oracle = _no_procfs_oracle(clock, tmp_path)

    oracle.check_model_wait(100)  # baseline
    clock.advance(2.0)
    assert oracle.check_model_wait(100)[0] == VERDICT_UNKNOWN


def test_model_wait_portable_movement_needs_a_live_pid(tmp_path, monkeypatch):
    """A moving counter for a pid that is gone must not attest to work.

    Both halves are required: the alive check alone would forgive a
    finished-but-lost-frame backend forever (a wedged process is alive too),
    trading a 90s truncation for a full-prompt-timeout hang.
    """
    from kiro_crew import platform_compat

    clock = _Clock()
    cpu = {"ns": 1_000_000_000}
    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda _pid: cpu["ns"])
    monkeypatch.setattr(platform_compat, "pid_exists", lambda _pid: False)
    oracle = _no_procfs_oracle(clock, tmp_path)

    oracle.check_model_wait(100)  # baseline
    cpu["ns"] += 900_000_000
    clock.advance(2.0)
    assert oracle.check_model_wait(100)[0] == VERDICT_UNKNOWN


def test_model_wait_without_any_portable_counter_is_unknown(tmp_path, monkeypatch):
    """No counter readable anywhere → the pre-fix verdict, unchanged."""
    from kiro_crew import platform_compat

    clock = _Clock()
    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda _pid: None)
    oracle = _no_procfs_oracle(clock, tmp_path)

    assert oracle.check_model_wait(100) == (VERDICT_UNKNOWN, "no readable counters")


def test_model_wait_prefers_procfs_when_it_is_readable(tmp_path, monkeypatch):
    """On Linux the portable probe is never consulted — the tree walk answers."""
    from kiro_crew import platform_compat

    def _never(_pid):
        raise AssertionError("portable probe consulted while /proc was readable")

    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", _never)
    clock = _Clock()
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(100, io_bytes=1000)
    oracle = _oracle(fake, clock, sample_min=1.0)

    oracle.check_model_wait(100)  # baseline
    fake.set_io(100, 9000)
    clock.advance(2.0)
    assert oracle.check_model_wait(100)[0] == VERDICT_WORKING


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


# ── Shared offloaded-consult guard ──
#
# consult_offloaded is the single copy of the guard sequence AcpClient and
# AcpSessionHandle both delegate to. The call-site behaviors (retirement at
# boundaries, oracle generation swaps) stay pinned by test_acp_client.py and
# test_acp_stale_recovery.py; these tests pin the helper's own contract so a
# regression in it is attributed to the shared code, not to one caller.


class _Holder:
    """Minimal ConsultFutureHolder: just the tracked-future slot."""

    def __init__(self) -> None:
        self._consult_future: asyncio.Future[tuple[str, str]] | None = None


@pytest.mark.asyncio
async def test_consult_offloaded_refused_submission_reads_unknown():
    """A refused executor job degrades to UNKNOWN — it never raises.

    The callers are silent-read polls and watchdog ticks: an executor shut
    down during teardown (or refusing thread creation under load) must read as
    an inconclusive probe, not abort the live turn with a RuntimeError.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    pool.shutdown(wait=True)
    holder = _Holder()

    verdict = await consult_offloaded(
        holder,
        lambda: (VERDICT_WORKING, "never runs"),
        (),
        executor_factory=lambda: pool,
    )

    assert verdict == (VERDICT_UNKNOWN, "oracle offload error")
    # The failed submission left no tracked future behind to wedge the next
    # poll on "prior consult still in flight".
    assert holder._consult_future is None


@pytest.mark.asyncio
async def test_consult_offloaded_skips_while_prior_walk_is_in_flight():
    """An unfinished prior walk answers UNKNOWN without submitting again."""
    holder = _Holder()
    prior = asyncio.get_running_loop().create_future()
    holder._consult_future = prior

    verdict = await consult_offloaded(
        holder,
        lambda: (VERDICT_WORKING, "must not be submitted"),
        (),
        executor_factory=lambda: pytest.fail("submitted despite in-flight prior"),
    )

    assert verdict == (VERDICT_UNKNOWN, "prior consult still in flight")
    # The in-flight prior stays tracked; the guard did not replace it.
    assert holder._consult_future is prior
    prior.cancel()


@pytest.mark.asyncio
async def test_consult_offloaded_tracks_and_returns_the_walk_result():
    """The happy path stores the submitted future and returns its verdict."""
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        holder = _Holder()

        verdict = await consult_offloaded(
            holder,
            lambda pid: (VERDICT_WORKING, f"pid {pid} moving"),
            (42,),
            executor_factory=lambda: pool,
        )

        assert verdict == (VERDICT_WORKING, "pid 42 moving")
        assert holder._consult_future is not None
        assert holder._consult_future.done()
    finally:
        pool.shutdown(wait=True)


@pytest.mark.asyncio
async def test_consult_offloaded_consumes_a_failed_priors_exception():
    """A done-with-exception prior is consumed, then a fresh walk submitted.

    A prior that completed after a ``wait_for`` timeout detached its awaiter
    would otherwise report through ``Future.__del__`` as an unhandled-asyncio
    crash for what is an ordinary probe failure.
    """
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        holder = _Holder()
        prior: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        prior.set_exception(RuntimeError("walk failed after awaiter left"))
        holder._consult_future = prior

        verdict = await consult_offloaded(
            holder,
            lambda: (VERDICT_WORKING, "fresh walk"),
            (),
            executor_factory=lambda: pool,
        )

        assert verdict == (VERDICT_WORKING, "fresh walk")
        # exception() retrieved without raising == consumed.
        assert prior.exception() is not None
    finally:
        pool.shutdown(wait=True)
