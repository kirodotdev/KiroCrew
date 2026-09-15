"""Unit tests for the liveness oracle's darwin backend (``acp/liveness.py``).

On a host without procfs the oracle reads the runtime's tree through a
``DarwinProcessBackend``. These tests drive that path with a fake backend — a
hand-built descendant table, no libproc — so they are deterministic on every
platform. The ``/proc`` walk keeps its own tests in ``test_acp_liveness.py``;
this file pins that the darwin path reaches the same verdicts from the same
matching rules, and that the evidence procfs alone carries is never invented.
"""

from __future__ import annotations

import os
import sys

import pytest

from kiro_crew.acp import liveness
from kiro_crew.acp.liveness import (
    CHILD_EXIT_GRACE_SECS,
    EVIDENCE_ESTABLISHED_FLAT,
    EVIDENCE_SAMPLING,
    EVIDENCE_SHELL_CHILD_ABSENT,
    VERDICT_DEAD,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LibprocBackend,
    LivenessOracle,
    ProcessRow,
    ToolCallState,
)

RUNTIME = 100


class _Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class FakeBackend:
    """A descendant table standing in for libproc.

    ``add(pid, cmdline, started=..., cpu=...)`` registers a live process under
    the runtime; ``remove`` makes it gone; ``enumerable=False`` answers None from
    ``descendants`` the way an unreadable tree does.
    """

    def __init__(self, *, enumerable: bool = True) -> None:
        self.enumerable = enumerable
        self.rows: dict[int, ProcessRow] = {}
        self.cpu: dict[int, int] = {}

    def add(
        self, pid: int, cmdline: str, *, started: float | None = 10_000_000.0, cpu: int = 0
    ) -> None:
        self.rows[pid] = ProcessRow(pid=pid, started=started, cmdline=cmdline)
        self.cpu[pid] = cpu

    def remove(self, pid: int) -> None:
        self.rows.pop(pid, None)
        self.cpu.pop(pid, None)

    # -- DarwinProcessBackend --

    def descendants(self, root_pid: int) -> list[int] | None:
        if not self.enumerable:
            return None
        return [pid for pid in self.rows if pid != root_pid]

    def row(self, pid: int) -> ProcessRow | None:
        return self.rows.get(pid)

    def cpu_nanos(self, pid: int) -> int | None:
        if pid == RUNTIME:
            return 1_000_000
        return self.cpu.get(pid)


def _oracle(
    backend: FakeBackend,
    clock: _Clock,
    tmp_path,
    sample_min: float = 3.0,
    wall: _Clock | None = None,
):
    # A proc_root that does not exist: the oracle must not read a fake tree here.
    # The wall clock (what libproc dates processes on) defaults to the same fake
    # clock as the steady one, so the two agree unless a test steps ``wall``.
    return LivenessOracle(
        str(tmp_path / "nonexistent"),
        now=clock,
        sample_min_secs=sample_min,
        darwin_backend=backend,
        wall_now=wall if wall is not None else clock,
        steady_now_fn=clock,
    )


def _shell_tool(command: str, clock: _Clock, wall: _Clock | None = None) -> ToolCallState:
    return ToolCallState(
        title="bash",
        command=command,
        dispatch_ts=clock.t,
        dispatch_boot_ts=(wall if wall is not None else clock).t,
        dispatch_steady_ts=clock.t,
        is_shell=True,
    )


def _mcp_tool(clock: _Clock, tool_name: str = "use_subagent") -> ToolCallState:
    return ToolCallState(
        title="kirocrew-core___use_subagent",
        command='{"task": "x"}',
        dispatch_ts=clock.t,
        dispatch_boot_ts=clock.t,
        dispatch_steady_ts=clock.t,
        is_shell=False,
        tool_name=tool_name,
    )


# ── (a) match ────────────────────────────────────────────────────────────────


def test_matched_live_child_is_working_and_tracked(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(200, "/bin/bash -c long-build release > build.log 2>&1")
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_WORKING
    assert evidence == "shell child 200 matched command"
    assert oracle._tracked_child == 200

    clock.advance(4.0)
    verdict, evidence = oracle.check_tool(RUNTIME, tool)
    assert verdict == VERDICT_WORKING
    assert evidence == "shell child 200 alive"


def test_program_basename_matches_when_only_the_path_is_readable(tmp_path):
    """argv unreadable → the row carries the executable path, and the
    program-name rule still recognizes it (reduced fidelity, same verdict)."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(200, "/opt/homebrew/bin/brazil-build")
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("brazil-build release", clock)

    assert oracle.check_tool(RUNTIME, tool) == (VERDICT_WORKING, "shell child 200 matched command")


# ── (b) exit detection ───────────────────────────────────────────────────────


def test_tracked_child_gone_is_unknown_in_grace_then_dead(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(200, "/bin/bash -c long-build release > build.log 2>&1")
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)
    assert oracle.check_tool(RUNTIME, tool)[0] == VERDICT_WORKING

    backend.remove(200)
    clock.advance(1.0)
    verdict, evidence = oracle.check_tool(RUNTIME, tool)
    assert verdict == VERDICT_UNKNOWN
    assert "grace" in evidence

    clock.advance(CHILD_EXIT_GRACE_SECS + 1.0)
    verdict, evidence = oracle.check_tool(RUNTIME, tool)
    assert verdict == VERDICT_DEAD
    assert evidence.startswith("shell child 200 exited")


def test_enumeration_failure_does_not_age_a_live_tracked_child_into_dead(tmp_path):
    """The child still reads (alive) while the tree cannot be enumerated: that
    is not an exit, and DEAD is the one verdict this branch must not guess."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(200, "/bin/bash -c long-build release > build.log 2>&1")
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("long-build release > build.log 2>&1", clock)
    assert oracle.check_tool(RUNTIME, tool)[0] == VERDICT_WORKING

    backend.enumerable = False
    clock.advance(CHILD_EXIT_GRACE_SECS + 5.0)
    assert oracle.check_tool(RUNTIME, tool)[0] == VERDICT_WORKING


# ── (c) observable tree, nothing started since dispatch ─────────────────────


def test_observable_tree_with_only_old_descendants_is_tagged_absent(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(201, "python -m kiro_crew.mcp_gateway.stub --server github", started=0.0)
    backend.add(202, "python -m kiro_crew.mcp_gateway.stub --server slack", started=0.0)
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)
    clock.advance(61.0)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence
    assert "2 live descendants" in evidence


def test_empty_but_enumerable_tree_is_an_absent_child(tmp_path):
    clock = _Clock()
    oracle = _oracle(FakeBackend(), clock, tmp_path)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_young_unmatched_descendant_vetoes_the_absence_claim(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(201, "python -m kiro_crew.mcp_gateway.stub --server github", started=0.0)
    backend.add(300, "/opt/vendor/bin/opaque-worker --serve")  # young
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("[REDACTED-CREDENTIAL] x", clock)

    assert oracle.check_tool(RUNTIME, tool) == (VERDICT_UNKNOWN, "no matching shell child")


def test_backward_wall_step_never_grounds_an_absence_claim(tmp_path):
    """The darwin ruler is the wall clock, and it can step backwards between the
    dispatch stamp and the runtime's fork. A live child then predates its own
    dispatch; with a command the matchers cannot recognise there is nothing to
    veto the absence claim, and the caller would narrow to the stale window and
    cancel live work. The steady stamp exposes the step, and attribution fails
    open instead."""
    clock = _Clock()
    wall = _Clock(1_000_000.0)
    backend = FakeBackend()
    oracle = _oracle(backend, clock, tmp_path, wall=wall)
    tool = _shell_tool("[REDACTED-CREDENTIAL] x", clock, wall=wall)
    # NTP steps the wall clock back two minutes, then kiro-cli forks the command:
    # libproc dates the child at the post-step instant, 120s before its dispatch.
    wall.t -= 120.0
    backend.add(300, "/opt/vendor/bin/opaque-worker --serve", started=wall.t)
    clock.advance(61.0)
    wall.advance(61.0)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_backward_wall_step_keeps_a_matched_child_working(tmp_path):
    clock = _Clock()
    wall = _Clock(1_000_000.0)
    backend = FakeBackend()
    oracle = _oracle(backend, clock, tmp_path, wall=wall)
    tool = _shell_tool("make -j8 all && ./run-tests.sh --verbose", clock, wall=wall)
    wall.t -= 120.0
    backend.add(300, "/bin/sh -c make -j8 all && ./run-tests.sh --verbose", started=wall.t)
    clock.advance(61.0)
    wall.advance(61.0)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_WORKING, evidence
    assert "300" in evidence


def test_agreeing_clocks_still_date_an_old_descendant_as_old(tmp_path):
    """Negative control for the step guard: with wall and steady clocks in
    agreement the ruler is trusted, so a genuinely old descendant is still what
    grounds the absence claim (the same setup as the tagged-absent test, with an
    explicit wall clock)."""
    clock = _Clock()
    wall = _Clock(1_000_000.0)
    backend = FakeBackend()
    backend.add(201, "python -m kiro_crew.mcp_gateway.stub --server github", started=0.0)
    oracle = _oracle(backend, clock, tmp_path, wall=wall)
    tool = _shell_tool("[REDACTED-CREDENTIAL] x", clock, wall=wall)
    clock.advance(61.0)
    wall.advance(61.0)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_missing_steady_stamp_declines_to_attribute_on_darwin(tmp_path):
    """No steady stamp means the wall clock cannot be validated, and an absence
    claim on darwin rests on nothing else — so none is made."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(201, "python -m kiro_crew.mcp_gateway.stub --server github", started=0.0)
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("[REDACTED-CREDENTIAL] x", clock)
    tool.dispatch_steady_ts = None
    clock.advance(61.0)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence


def test_undatable_descendant_keeps_the_full_window(tmp_path):
    """A row without a start reads as possibly-this-tool's: fail-open."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(201, "python -m kiro_crew.mcp_gateway.stub --server github", started=None)
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("ls /some/dir | grep needle | wc -l", clock)

    assert oracle.check_tool(RUNTIME, tool) == (VERDICT_UNKNOWN, "no matching shell child")


# ── (d) tree cannot be enumerated ────────────────────────────────────────────


def test_unenumerable_tree_is_plain_unknown_without_an_absence_claim(tmp_path):
    clock = _Clock()
    backend = FakeBackend(enumerable=False)
    backend.add(200, "/bin/bash -c long-build release")
    oracle = _oracle(backend, clock, tmp_path)
    tool = _shell_tool("long-build release", clock)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert (verdict, evidence) == (VERDICT_UNKNOWN, "no matching shell child")
    assert oracle._tracked_child is None


# ── (e) MCP subtree movement ─────────────────────────────────────────────────


def test_mcp_subtree_cpu_delta_is_working_with_the_cpu_only_label(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(210, "python -m kiro_crew.mcp_gateway.stub --server github", cpu=5_000_000_000)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)
    tool = _mcp_tool(clock, tool_name="ReadInternalWebsites")

    assert oracle.check_tool(RUNTIME, tool) == (
        VERDICT_UNKNOWN,
        f"mcp subtree flat ({EVIDENCE_SAMPLING})",
    )

    backend.cpu[210] += 250_000_000
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(RUNTIME, tool)
    assert verdict == VERDICT_WORKING
    assert evidence == "mcp subtree active (cpu +250000000ns (darwin cpu-only))"


def test_mcp_subtree_without_any_cpu_counter_is_plain_unknown(tmp_path):
    clock = _Clock()
    backend = FakeBackend(enumerable=False)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)

    verdict, evidence = oracle.check_tool(RUNTIME, _mcp_tool(clock))
    assert (verdict, evidence) == (VERDICT_UNKNOWN, "mcp subtree flat (no readable counters)")


def test_model_wait_subtree_cpu_delta_is_working(tmp_path):
    """The busy descendant under an idle root that the root-only portable probe
    could not see now reads WORKING through the subtree sum."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(210, "python -m kiro_crew.mcp_gateway.stub --server github", cpu=5_000_000_000)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)

    assert oracle.check_model_wait(RUNTIME) == (VERDICT_UNKNOWN, EVIDENCE_SAMPLING)
    backend.cpu[210] += 1_000_000
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(RUNTIME)
    assert verdict == VERDICT_WORKING
    assert evidence == "backend activity (cpu +1000000ns (darwin cpu-only))"


# ── (f) /proc-only evidence is never invented ────────────────────────────────


def test_flat_model_wrapping_tool_is_not_tagged_established_flat_on_darwin(tmp_path):
    clock = _Clock()
    backend = FakeBackend()
    backend.add(210, "python -m kiro_crew.mcp_gateway.stub --server github", cpu=5_000_000_000)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)
    tool = _mcp_tool(clock, tool_name="use_subagent")

    oracle.check_tool(RUNTIME, tool)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT), evidence
    assert evidence == "mcp subtree flat (cpu +0ns (darwin cpu-only))"


def test_flat_model_wait_is_unknown_never_dead_on_darwin(tmp_path):
    """No socket evidence → the lost-frame wedge cannot be claimed."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(210, "python -m kiro_crew.mcp_gateway.stub --server github", cpu=5_000_000_000)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)

    oracle.check_model_wait(RUNTIME)  # baseline
    clock.advance(2.0)
    verdict, evidence = oracle.check_model_wait(RUNTIME)

    assert verdict == VERDICT_UNKNOWN
    assert not evidence.startswith(EVIDENCE_ESTABLISHED_FLAT), evidence
    assert evidence == "backend subtree flat (cpu +0ns (darwin cpu-only))"


def test_live_tracked_child_never_reads_stuck_input_on_darwin(tmp_path):
    """wchan / blocked-fd evidence is /proc-only: a flat live child is WORKING."""
    clock = _Clock()
    backend = FakeBackend()
    backend.add(200, "/bin/bash -c long-build release", cpu=1_000)
    oracle = _oracle(backend, clock, tmp_path, sample_min=1.0)
    tool = _shell_tool("long-build release", clock)

    assert oracle.check_tool(RUNTIME, tool)[0] == VERDICT_WORKING
    clock.advance(2.0)
    assert oracle.check_tool(RUNTIME, tool) == (VERDICT_WORKING, "shell child 200 alive")
    clock.advance(2.0)
    assert oracle.check_tool(RUNTIME, tool) == (VERDICT_WORKING, "shell child 200 alive")


# ── Backend selection & lifecycle ────────────────────────────────────────────


def test_fake_proc_tree_never_selects_the_darwin_backend(tmp_path):
    """A readable proc_root pins the /proc walk on every host."""
    proc = tmp_path / "proc"
    proc.mkdir()
    assert LivenessOracle(str(proc))._darwin is None
    assert liveness.select_darwin_backend(str(proc)) is None


def test_absent_proc_root_selects_libproc_only_on_darwin(tmp_path, monkeypatch):
    missing = str(tmp_path / "nonexistent")
    monkeypatch.setattr(sys, "platform", "linux")
    assert liveness.select_darwin_backend(missing) is None
    monkeypatch.setattr(sys, "platform", "darwin")
    assert isinstance(liveness.select_darwin_backend(missing), LibprocBackend)


def test_fresh_carries_the_backend(tmp_path):
    backend = FakeBackend()
    oracle = _oracle(backend, _Clock(), tmp_path)
    assert oracle.fresh()._darwin is backend


def test_darwin_dispatch_stamp_is_wall_clock(monkeypatch):
    """Where CLOCK_BOOTTIME is absent, darwin dates processes on the wall clock,
    so the dispatch stamp must come from the same clock."""
    monkeypatch.delattr(liveness.time, "CLOCK_BOOTTIME", raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    before = liveness.time.time()
    stamp = liveness.boottime_now()
    assert stamp is not None
    assert before <= stamp <= liveness.time.time()
    monkeypatch.setattr(sys, "platform", "win32")
    assert liveness.boottime_now() is None


def test_steady_stamp_is_taken_only_where_the_start_clock_can_step(monkeypatch):
    """The gate is the platform, not the host: a faked clock makes the test prove
    the gate on every CI OS, including one with no ``clock_gettime`` at all."""
    monkeypatch.setattr(liveness.time, "clock_gettime", lambda clk: 12_345.0, raising=False)
    monkeypatch.setattr(liveness.time, "CLOCK_MONOTONIC", 6, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")
    assert liveness.steady_now() == 12_345.0
    monkeypatch.setattr(sys, "platform", "linux")
    assert liveness.steady_now() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="reads the real darwin clock")
def test_steady_stamp_reads_a_monotonic_clock_on_darwin():
    first = liveness.steady_now()
    assert first is not None
    assert first <= liveness.steady_now()


# ── Real libproc smoke (own subtree only) ────────────────────────────────────


@pytest.mark.skipif(sys.platform != "darwin", reason="libproc is macOS only")
def test_libproc_backend_reads_own_subtree():
    import subprocess

    backend = LibprocBackend()
    child = subprocess.Popen(["/bin/sleep", "30"])
    try:
        descendants = backend.descendants(os.getpid())
        assert descendants is not None
        assert child.pid in descendants
        row = backend.row(child.pid)
        assert row is not None
        assert row.started is not None
        assert "sleep" in row.cmdline
        assert row.started <= liveness.boottime_now() + 1.0
        assert backend.cpu_nanos(os.getpid()) is not None
    finally:
        child.kill()
        child.wait()
    # A reaped child is gone: no row, and it drops out of the tree.
    assert backend.row(child.pid) is None
    gone = backend.descendants(os.getpid())
    assert gone is not None and child.pid not in gone
    # An unknown pid has no observable tree.
    assert backend.descendants(2**22 - 1) is None
