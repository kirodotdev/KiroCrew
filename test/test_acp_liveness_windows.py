"""Unit tests for the liveness oracle's Windows shell-tool backend.

On Windows the oracle reads the shell tool's tree through ``WinProcessBackend``
(``platform_compat.attributed_descendants`` over one Toolhelp snapshot per
check), and uses it only to prove life: a moving tree
reads WORKING, everything else UNKNOWN tagged ``platform_limited``, and the check
never returns DEAD or ``shell_child_absent``. These tests replace the Windows
readers the walk and the backend call through ``platform_compat`` (the parent
map snapshot, the creation-time read, the exit-code liveness read and the CPU
counter) with a
hand-built table, so they run on every host. One test at the end drives the real
backend on Windows only. Nothing here signals or kills a process.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import liveness
from kiro_crew.acp.liveness import (
    CHILD_EXIT_GRACE_SECS,
    EVIDENCE_PLATFORM_LIMITED,
    EVIDENCE_SHELL_CHILD_ABSENT,
    VERDICT_DEAD,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
    ToolCallState,
    WinProcessBackend,
    select_windows_backend,
)

RUNTIME = 100
# Wall-clock seconds the fake host's processes are dated against.
WALL = 1_700_000_000.0
# CPU nanoseconds that count as movement (the 10 ms floor), and a bit more.
STEP = 50_000_000


def _filetime(unix_secs: float) -> str:
    """A creation token in ``process_start_time``'s Windows encoding (FILETIME)."""
    return str(int((unix_secs + 11_644_473_600) * 10_000_000))


class _Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, secs: float) -> None:
        self.t += secs


class FakeHost:
    """A Toolhelp parent map and the per-process reads the walk and backend make.

    ``add`` lists a process under *ppid* with a creation time in wall seconds;
    ``exit`` keeps it listed but not running (a handle is still open), and
    ``remove`` drops it from the snapshot. ``snapshot_error`` makes the
    snapshot raise, as a failed ``CreateToolhelp32Snapshot`` does.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.entries: dict[int, tuple[int, str]] = {}
        self.starts: dict[int, str | None] = {}
        self.alive: set[int] = set()
        self.cpu: dict[int, int] = {}
        self.snapshot_error = False
        self.snapshots = 0
        self.add(RUNTIME, 1, "kiro-cli.exe", created=WALL - 3600)
        # A macOS test host would otherwise pick libproc for the absent proc_root.
        monkeypatch.setattr(liveness, "select_darwin_backend", lambda _proc_root: None)
        # attributed_descendants reads the Toolhelp parent map only on Windows.
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
        monkeypatch.setattr(platform_compat, "_windows_process_parent_map", self._parent_map)
        monkeypatch.setattr(platform_compat, "process_start_time", self.starts.get)
        monkeypatch.setattr(platform_compat, "pid_exists", lambda pid: pid in self.alive)
        monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", self.cpu.get)

    def _parent_map(self) -> dict[int, int]:
        self.snapshots += 1
        if self.snapshot_error:
            raise OSError("Windows process snapshot creation failed")
        return {pid: ppid for pid, (ppid, _image) in self.entries.items()}

    def add(self, pid: int, ppid: int, image: str, *, created: float, cpu: int = 0) -> None:
        self.entries[pid] = (ppid, image)
        self.starts[pid] = _filetime(created)
        self.alive.add(pid)
        self.cpu[pid] = cpu

    def exit(self, pid: int) -> None:
        self.alive.discard(pid)

    def remove(self, pid: int) -> None:
        self.entries.pop(pid, None)
        self.starts.pop(pid, None)
        self.alive.discard(pid)
        self.cpu.pop(pid, None)


class _Run:
    """An oracle, a shell tool dispatched at ``WALL``, and the two clocks."""

    def __init__(self, tmp_path, command: str, *, parked: float = 0.0) -> None:
        self.clock = _Clock(5000.0)
        self.wall = _Clock(WALL)
        self.oracle = LivenessOracle(
            str(tmp_path / "no-proc"), now=self.clock, sample_min_secs=1.0, wall_now=self.wall
        )
        self.oracle._windows_backend = WinProcessBackend()
        # No dispatch_boot_ts: ``boottime_now()`` has no Windows clock.
        self.tool = ToolCallState(
            title="shell",
            command=command,
            dispatch_ts=self.clock.t,
            is_shell=True,
            dispatch_parked_secs=parked,
        )

    def check(self, advance: float = 2.0) -> tuple[str, str]:
        self.clock.advance(advance)
        self.wall.advance(advance)
        return self.oracle.check_tool(RUNTIME, self.tool)


def _assert_limited(reading: tuple[str, str]) -> None:
    verdict, evidence = reading
    assert verdict == VERDICT_UNKNOWN, reading
    assert evidence.startswith(EVIDENCE_PLATFORM_LIMITED), reading


def _assert_never_ends_the_turn(run: _Run, reading: tuple[str, str]) -> None:
    verdict, evidence = reading
    assert verdict != VERDICT_DEAD, reading
    assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), reading
    assert run.oracle._tracked_child is None


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> FakeHost:
    return FakeHost(monkeypatch)


# ── The backend's walk ───────────────────────────────────────────────────────


def test_descendants_are_the_runtime_subtree_with_creation_times(host: FakeHost) -> None:
    host.add(200, RUNTIME, "cmd.exe", created=WALL - 10)
    host.add(201, 200, "node.exe", created=WALL - 9)
    host.add(300, 999, "explorer.exe", created=WALL - 9000)

    created = WinProcessBackend().descendants_created(RUNTIME)

    assert created is not None
    assert sorted(created) == [200, 201]
    assert created[201] == pytest.approx(WALL - 9)
    assert host.snapshots == 1


def test_a_child_created_before_its_listed_parent_is_left_out_with_its_subtree(
    host: FakeHost,
) -> None:
    """Pid 200 may have been recycled: 201 was born before it, so 201 and everything
    below it are not this runtime's."""
    host.add(200, RUNTIME, "cmd.exe", created=WALL - 10)
    host.add(201, 200, "node.exe", created=WALL - 500)
    host.add(202, 201, "git.exe", created=WALL - 5)

    assert sorted(WinProcessBackend().descendants_created(RUNTIME) or {}) == [200]


def test_an_exited_or_undatable_descendant_is_left_out(host: FakeHost) -> None:
    host.add(200, RUNTIME, "cmd.exe", created=WALL - 10)
    host.add(201, 200, "git.exe", created=WALL - 5)
    host.add(202, 200, "cargo.exe", created=WALL - 4)
    host.exit(201)
    host.starts[202] = "garbled"

    assert sorted(WinProcessBackend().descendants_created(RUNTIME) or {}) == [200]


@pytest.mark.parametrize("failure", ["root_missing", "root_undatable", "root_gone"])
def test_an_unreadable_root_is_none(host: FakeHost, failure: str) -> None:
    host.add(200, RUNTIME, "cmd.exe", created=WALL - 10)
    if failure == "root_gone":
        host.exit(RUNTIME)
    elif failure == "root_missing":
        host.remove(RUNTIME)
    else:
        host.starts[RUNTIME] = None

    assert WinProcessBackend().descendants_created(RUNTIME) is None


def test_a_failed_snapshot_reads_as_no_descendants(host: FakeHost) -> None:
    host.add(200, RUNTIME, "cmd.exe", created=WALL - 10)
    host.snapshot_error = True

    assert WinProcessBackend().descendants_created(RUNTIME) == {}


def test_the_backend_is_selected_only_on_windows_without_a_proc_root(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert isinstance(select_windows_backend(str(tmp_path / "no-proc")), WinProcessBackend)
    assert select_windows_backend(str(tmp_path)) is None
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
    assert select_windows_backend(str(tmp_path / "no-proc")) is None


def test_a_darwin_backend_wins_over_the_windows_one(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(liveness, "select_darwin_backend", lambda _proc_root: None)
    darwin = object()
    oracle = LivenessOracle(str(tmp_path / "no-proc"), darwin_backend=darwin)  # type: ignore[arg-type]
    assert oracle._windows() is None
    assert isinstance(LivenessOracle(str(tmp_path / "no-proc"))._windows(), WinProcessBackend)


# ── Shell-tool verdicts: life only ───────────────────────────────────────────


def _build(host: FakeHost, tmp_path, command: str = "cargo build --release") -> _Run:
    run = _Run(tmp_path, command)
    host.add(200, RUNTIME, "cmd.exe", created=WALL + 0.5, cpu=20_000_000)
    host.add(201, 200, "cargo.exe", created=WALL + 0.6, cpu=1_000_000_000)
    first = run.check()
    _assert_limited(first)
    assert "sampling" in first[1]
    return run


def test_a_growing_tree_reads_working(host: FakeHost, tmp_path) -> None:
    run = _build(host, tmp_path)
    host.add(202, 201, "rustc.exe", created=WALL + 3, cpu=STEP)

    verdict, evidence = run.check()

    assert verdict == VERDICT_WORKING
    assert evidence.startswith("windows cpu-only: tree cpu moved (cpu +50000000ns")


def test_a_silent_tree_reads_unknown_platform_limited(host: FakeHost, tmp_path) -> None:
    run = _build(host, tmp_path)
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        _assert_limited(reading)
        assert "no movement" in reading[1]
        _assert_never_ends_the_turn(run, reading)


@pytest.mark.parametrize("how", ["removed", "open_handle"])
def test_an_exited_tree_reads_unknown_platform_limited_never_dead(
    host: FakeHost, tmp_path, how: str
) -> None:
    run = _build(host, tmp_path)
    for pid in (201, 200):
        if how == "removed":
            host.remove(pid)
        else:
            host.exit(pid)
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        _assert_limited(reading)
        assert "no process started for this tool" in reading[1]
        _assert_never_ends_the_turn(run, reading)


def test_a_process_leaving_the_tree_is_not_movement(host: FakeHost, tmp_path) -> None:
    """``git pull && cargo build`` after ``git`` exits and before ``cargo`` starts:
    the sum shrinks, which says nothing about the command, so it stays UNKNOWN."""
    run = _build(host, tmp_path, "git pull && cargo build")
    host.remove(201)

    reading = run.check()

    _assert_limited(reading)
    _assert_never_ends_the_turn(run, reading)


def test_a_chain_whose_first_program_exited_reads_working_while_the_next_runs(
    host: FakeHost, tmp_path
) -> None:
    run = _Run(tmp_path, "git pull && cargo build")
    host.add(200, RUNTIME, "cmd.exe", created=WALL + 0.5)
    host.add(201, 200, "git.exe", created=WALL + 0.6, cpu=STEP)
    run.check()
    host.remove(201)
    host.add(202, 200, "cargo.exe", created=WALL + 5)
    run.check()
    for _ in range(3):
        host.cpu[202] += STEP
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        assert reading[0] == VERDICT_WORKING, reading
        _assert_never_ends_the_turn(run, reading)


def test_a_shell_working_in_process_reads_working(host: FakeHost, tmp_path) -> None:
    """``git fetch; Expand-Archive big.zip``: PowerShell does the second step itself
    and starts no process; its own CPU proves the command alive."""
    run = _Run(tmp_path, "git fetch; Expand-Archive big.zip")
    host.add(200, RUNTIME, "powershell.exe", created=WALL + 0.5, cpu=STEP)
    host.add(201, 200, "git.exe", created=WALL + 0.6, cpu=STEP)
    run.check()
    host.remove(201)
    run.check()
    for _ in range(3):
        host.cpu[200] += STEP
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        assert reading[0] == VERDICT_WORKING, reading
        _assert_never_ends_the_turn(run, reading)


def test_two_same_image_commands_on_a_shared_runtime_never_end_the_turn(
    host: FakeHost, tmp_path
) -> None:
    """Two sessions on one runtime each run ``cargo build``. Image names cannot say
    which is this tool's, and one finishing never reads the other as DEAD."""
    run = _Run(tmp_path, "cargo build")
    host.add(200, RUNTIME, "cmd.exe", created=WALL + 0.5)
    host.add(201, 200, "cargo.exe", created=WALL + 0.6)
    host.add(300, RUNTIME, "cmd.exe", created=WALL + 0.7)
    host.add(301, 300, "cargo.exe", created=WALL + 0.8)
    run.check()
    host.remove(201)
    host.remove(200)
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        _assert_limited(reading)
        _assert_never_ends_the_turn(run, reading)


def test_a_prompt_shaped_command_keeps_the_platform_limited_tag(host: FakeHost, tmp_path) -> None:
    """``npm init`` runs through a ``.cmd``/``.ps1`` shim, so the snapshot holds only
    ``powershell.exe`` and ``node.exe``. A flat tree must keep the tag the
    interactive narrowing and ``waiting_input`` read."""
    run = _Run(tmp_path, "npm init")
    host.add(200, RUNTIME, "powershell.exe", created=WALL + 0.5, cpu=STEP)
    host.add(201, 200, "node.exe", created=WALL + 0.6, cpu=STEP)
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        _assert_limited(reading)
        _assert_never_ends_the_turn(run, reading)


def test_an_unreadable_tree_is_platform_limited(host: FakeHost, tmp_path) -> None:
    run = _build(host, tmp_path)
    host.starts[RUNTIME] = None
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        assert reading == (
            VERDICT_UNKNOWN,
            f"{EVIDENCE_PLATFORM_LIMITED}: process tree unreadable on this platform",
        )


def test_a_failed_snapshot_is_platform_limited(host: FakeHost, tmp_path) -> None:
    run = _build(host, tmp_path)
    host.snapshot_error = True
    for _ in range(3):
        reading = run.check(CHILD_EXIT_GRACE_SECS + 1.0)
        _assert_limited(reading)
        _assert_never_ends_the_turn(run, reading)


def test_unreadable_counters_are_platform_limited(host: FakeHost, tmp_path) -> None:
    run = _Run(tmp_path, "cargo build")
    host.add(200, RUNTIME, "cargo.exe", created=WALL + 0.5)
    del host.cpu[200]

    reading = run.check()

    _assert_limited(reading)
    assert "cpu unreadable" in reading[1]


def test_the_three_trees_read_different_verdicts(host: FakeHost, tmp_path) -> None:
    """The issue's acceptance: a growing tree differs from a silent or finished one."""
    readings = []
    for state in ("growing", "silent", "finished"):
        host.entries.clear()
        host.starts.clear()
        host.alive.clear()
        host.cpu.clear()
        host.add(RUNTIME, 1, "kiro-cli.exe", created=WALL - 3600)
        run = _build(host, tmp_path / state)
        if state == "growing":
            host.cpu[201] += STEP
        elif state == "finished":
            host.remove(201)
            host.remove(200)
        readings.append(run.check(CHILD_EXIT_GRACE_SECS + 2.0)[0])

    assert readings == [VERDICT_WORKING, VERDICT_UNKNOWN, VERDICT_UNKNOWN]


def test_a_process_older_than_the_dispatch_never_proves_life(host: FakeHost, tmp_path) -> None:
    """A busy ``node.exe`` MCP server started with the session is not the command."""
    run = _Run(tmp_path, "node scripts/build.js")
    host.add(150, RUNTIME, "node.exe", created=WALL - 1800, cpu=9_000_000_000)
    for _ in range(3):
        host.cpu[150] += STEP
        _assert_limited(run.check())


def test_a_process_inside_the_tolerance_and_park_counts(host: FakeHost, tmp_path) -> None:
    run = _Run(tmp_path, "cargo build", parked=120.0)
    host.add(200, RUNTIME, "cargo.exe", created=WALL - 100)
    run.check()
    host.cpu[200] += STEP

    assert run.check()[0] == VERDICT_WORKING


def test_an_unattributed_process_never_proves_life(host: FakeHost, tmp_path) -> None:
    run = _Run(tmp_path, "cargo build")
    host.add(200, RUNTIME, "cmd.exe", created=WALL + 5)
    host.add(201, 200, "cargo.exe", created=WALL + 1)
    run.check()
    host.cpu[201] += STEP

    _assert_limited(run.check())


def test_a_tool_without_a_dispatch_stamp_is_platform_limited(host: FakeHost, tmp_path) -> None:
    run = _Run(tmp_path, "cargo build")
    run.tool.dispatch_ts = 0.0
    host.add(200, RUNTIME, "cargo.exe", created=WALL + 0.5, cpu=STEP)

    _assert_limited(run.check())


def test_an_early_sample_does_not_advance_the_baseline(host: FakeHost, tmp_path) -> None:
    """Two early samples of 6 ms each: the second compares against the first
    baseline (12 ms, movement), not against the early reading (6 ms)."""
    run = _build(host, tmp_path)
    host.cpu[201] += 6_000_000
    _assert_limited(run.check(0.1))
    host.cpu[201] += 6_000_000

    assert run.check(0.1)[0] == VERDICT_WORKING


# ── What stays as it was ─────────────────────────────────────────────────────


def test_mcp_and_model_wait_never_read_the_windows_tree(
    host: FakeHost, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _Run(tmp_path, "")
    monkeypatch.setattr(platform_compat, "proc_cpu_nanos_for_pid", lambda pid: None)
    tool = ToolCallState(
        title="kirocrew-core___spawn_run",
        command='{"task": "x"}',
        dispatch_ts=run.clock.t,
        is_shell=False,
        tool_name="spawn_run",
    )

    verdict, evidence = run.oracle.check_tool(RUNTIME, tool)
    assert verdict == VERDICT_UNKNOWN
    assert "mcp subtree unobservable" in evidence
    assert run.oracle.check_model_wait(RUNTIME)[0] == VERDICT_UNKNOWN
    assert host.snapshots == 0


def test_a_fake_proc_tree_keeps_the_proc_walk_on_windows(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(platform_compat, "IS_WINDOWS", True)
    assert LivenessOracle(str(tmp_path))._windows() is None


def test_the_windows_backend_is_reexported_by_the_facade() -> None:
    assert liveness.WinProcessBackend is WinProcessBackend
    assert liveness.select_windows_backend is select_windows_backend


# ── The real backend, on Windows only ────────────────────────────────────────


@pytest.mark.skipif(not platform_compat.IS_WINDOWS, reason="reads real Toolhelp snapshots")
def test_a_real_child_is_never_read_dead(tmp_path) -> None:
    """A child that reads its stdin until EOF, checked while it runs and after it
    exits: never DEAD, never ``shell_child_absent``. Closing its stdin pipe is the
    only thing done to it."""
    clock = _Clock(5000.0)
    oracle = LivenessOracle(str(tmp_path / "no-proc"), now=clock, sample_min_secs=0.0)
    assert isinstance(oracle._windows(), WinProcessBackend)
    tool = ToolCallState(
        title="shell",
        command='python -c "import sys; sys.stdin.read()"',
        dispatch_ts=clock.t,
        is_shell=True,
    )
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    def _assert_life_only() -> None:
        # The test process may hold other recent children (earlier tests' helpers),
        # so a WORKING here can be theirs; the contract is that no reading ends the
        # turn and every UNKNOWN keeps the platform_limited tag.
        clock.advance(CHILD_EXIT_GRACE_SECS + 1.0)
        verdict, evidence = oracle.check_tool(os.getpid(), tool)
        assert verdict in (VERDICT_WORKING, VERDICT_UNKNOWN), evidence
        assert not evidence.startswith(EVIDENCE_SHELL_CHILD_ABSENT), evidence
        if verdict == VERDICT_UNKNOWN:
            assert evidence.startswith(EVIDENCE_PLATFORM_LIMITED), evidence

    try:
        for _ in range(3):
            _assert_life_only()
    finally:
        assert child.stdin is not None
        child.stdin.close()
        child.wait(timeout=60)

    for _ in range(2):
        _assert_life_only()
