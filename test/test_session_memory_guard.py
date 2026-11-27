"""The host memory guard's pass (rfc-host-memory-guard.md §4, §8 tests 1-6 and 9-11).

Victim choice runs against a fake process view, so every shape (an xdist tree, an
MCP root, a harness, a leftover) is exact on every host. The stop itself is proved
against real child processes on Linux, where the pinned terminator signals.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import signal
import subprocess
import sys
import threading
from types import SimpleNamespace
from typing import Any

import pytest
import spawn_test_helpers

from kiro_crew import _process_group_supervisor, platform_compat, session_memory_guard
from kiro_crew.resource_status import (
    POSTURE_AMPLE,
    POSTURE_CRITICAL,
    POSTURE_TIGHT,
    ResourceStatus,
)
from kiro_crew.session_memory_guard import (
    COOLDOWN_SECS,
    TIER_TEST,
    TIER_TOOL,
    Candidate,
    MemoryGuardState,
    ProcessView,
    pick,
    scan,
)

GATEWAY = os.getpid()
PYTEST = b"/venv/bin/python\x00-m\x00pytest\x00-n\x002\x00test"
XDIST = b"/venv/bin/python\x00-c\x00import sys;exec(eval(sys.stdin.readline()))"


class FakeHost:
    """pid -> (cmdline, rss MB, children); leftovers per runtime."""

    def __init__(self) -> None:
        self.procs: dict[int, tuple[bytes, float, list[int]]] = {}
        self.left: dict[int, list[int]] = {}
        self.parents: dict[int, int] = {}
        #: Shells started as an MCP server's command: no shell-tool launch marker.
        self.unmarked: set[int] = set()

    def add(self, pid: int, cmd: bytes, rss: float = 10.0, parent: int | None = None) -> int:
        self.procs[pid] = (cmd, rss, [])
        if parent is not None:
            self.parents[pid] = parent
            self.procs.setdefault(parent, (b"/sbin/init", 1.0, []))
            self.procs[parent][2].append(pid)
        return pid

    def view(self) -> ProcessView:
        return ProcessView(
            children=lambda pid: list(self.procs.get(pid, (b"", 0.0, []))[2]),
            cmdline=lambda pid: self.procs.get(pid, (b"", 0.0, []))[0],
            rss_mb=lambda pid: self.procs.get(pid, (b"", 0.0, []))[1],
            start_id=lambda pid: f"s{pid}" if pid in self.procs else None,
            leftovers=lambda root: [(pid, f"s{pid}") for pid in self.left.get(root, ())],
            parent_of=lambda pid: self.parents.get(pid),
            verifies_parents=True,
            is_shell_tool=lambda pid: pid not in self.unmarked,
        )


def _runtime(host: FakeHost, pid: int) -> int:
    return host.add(pid, b"/usr/bin/kiro-cli\x00acp")


def _pytest_tree(host: FakeHost, parent: int, root: int, worker_mb: float) -> None:
    host.add(root, PYTEST, 100.0, parent)
    host.add(root + 1, XDIST, worker_mb, root)
    host.add(root + 2, XDIST, worker_mb, root)


def _shell_tree(host: FakeHost, parent: int, root: int, mb: float) -> None:
    host.add(root, b"/bin/bash\x00-c\x00make", 5.0, parent)
    host.add(root + 1, b"/usr/bin/node\x00build.js", mb, root)


class TestVictimChoice:
    def test_largest_test_tree_beats_a_larger_tool_tree_and_xdist_is_one_tree(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        host.add(110, b"/bin/bash\x00-c\x00pytest", 5.0, 100)
        _pytest_tree(host, 110, 120, 400.0)  # 900 MB test tree under the shell
        _shell_tree(host, 100, 200, 5000.0)  # 5 GB tool tree
        found = scan({100: "chat-a"}, frozenset({100, GATEWAY}), host.view())
        tests = [c for c in found if c.tier == TIER_TEST]
        assert [(c.root_pid, c.procs) for c in tests] == [(120, 3)]  # one tree, topmost
        chosen = pick(found, {}, 0.0)
        assert chosen is not None and chosen.root_pid == 120 and chosen.label == "pytest"
        # Leaf first, root last: the workers before the pytest that owns them.
        assert chosen.targets[-1] == (120, "s120")

    def test_a_console_script_pytest_and_its_workers_are_one_tier_1_tree(self) -> None:
        # ``pytest -n 2`` through its shebang, left behind after its shell exited.
        host = FakeHost()
        _runtime(host, 100)
        host.add(400, b"/venv/bin/python\x00/venv/bin/pytest\x00-n\x002\x00tests", 100.0, 1)
        host.add(401, XDIST, 400.0, 400)
        host.add(402, XDIST, 400.0, 400)
        host.left[100] = [400, 401, 402]
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.tier, c.root_pid, c.procs) for c in found] == [(TIER_TEST, 400, 3)]

    @pytest.mark.parametrize(
        ("cmdline", "expected"),
        [
            (b"/venv/bin/python3.12\x00/venv/bin/pytest\x00-q", True),
            (b"/usr/bin/python3\x00/usr/local/bin/py.test", True),
            (b"/venv/bin/python\x00/work/pytest-dashboard/server.py", False),
            (b"/usr/bin/node\x00/venv/bin/pytest", False),
            (b"/venv/bin/python\x00-m\x00pytest", True),
        ],
    )
    def test_the_test_runner_shapes(self, cmdline: bytes, expected: bool) -> None:
        assert session_memory_guard.is_test_runner(cmdline) is expected

    def test_protected_harness_mcp_and_outside_processes_are_never_picked(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        host.add(101, b"/bin/bash\x00-c\x00kiro-cli\x00chat", 5.0, 100)
        host.add(102, b"/usr/bin/kiro-cli\x00acp", 9000.0, 101)  # a harness inside a shell tree
        host.add(140, b"/usr/bin/python3\x00-m\x00kiro_crew.mcp_gateway.stub", 9000.0, 100)
        _pytest_tree(host, 140, 150, 2000.0)  # under an MCP root
        host.add(160, b"npx\x00-y\x00@playwright/mcp", 9000.0, 100)  # marked MCP launcher
        host.add(170, b"/usr/bin/kiro-cli\x00acp", 9000.0, 100)  # pool spare pid
        _pytest_tree(host, 170, 180, 2000.0)
        _pytest_tree(host, 1, 300, 9000.0)  # the user's own terminal: no session above
        protected = frozenset({100, 170, GATEWAY})
        found = scan({100: "chat-a"}, protected, host.view())
        assert found == []
        assert pick(found, {}, 0.0) is None

    def test_the_kiro_cli_v3_chain_reaches_the_shell_tool_below_the_harness(self) -> None:
        # The live shape: runtime shim -> aim sandbox -> launcher -> kiro-cli -> KAS
        # node -> zsh (the shell tool); MCP servers are the KAS node's other children.
        host = FakeHost()
        host.add(100, b"/venv/bin/python\x00-I\x00-S\x00shim.py\x00kiro-cli\x00acp")
        host.add(101, b"aim\x00sandbox\x00--client\x00kiro-cli", 20.0, 100)
        host.add(102, b"/aim/sandbox/launcher\x00--session-id\x00x", 20.0, 101)
        host.add(103, b"/tools/kiro-cli/3.5.0/kiro-cli\x00acp", 300.0, 102)
        host.add(104, b"/share/kiro-cli/node\x00--experimental-wasm-modules\x00kas.js", 900.0, 103)
        host.add(110, b"/bin/zsh\x00-c\x00npm run build", 5.0, 104)
        host.add(111, b"/usr/bin/node\x00build.js", 6000.0, 110)
        host.add(120, b"/usr/bin/node\x00/npm/bin/npx-cli.js\x00some-mcp", 50.0, 104)
        host.add(121, b"/bin/sh\x00-c\x00some-mcp", 5.0, 120)  # npx runs the bin via sh
        host.add(122, b"/usr/bin/node\x00some-mcp/server.js", 7000.0, 121)
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.tier, c.root_pid) for c in found] == [(TIER_TOOL, 110)]
        chosen = pick(found, {}, 0.0)
        assert chosen is not None and chosen.root_pid == 110 and chosen.label == "node"

    def test_an_engine_the_runtime_runs_directly_hosts_the_shell_tool(self) -> None:
        # The launcher runs the bundled KAS node as the runtime's only child.
        host = FakeHost()
        _runtime(host, 100)
        host.add(104, b"/share/kiro-cli/node\x00--experimental-wasm-modules\x00kas.js", 900.0, 100)
        host.add(110, b"/bin/bash\x00-c\x00make", 5.0, 104)
        host.add(111, b"/usr/bin/node\x00build.js", 6000.0, 110)
        host.add(120, b"/usr/bin/node\x00/npm/bin/npx-cli.js\x00some-mcp", 50.0, 104)
        host.add(121, b"/bin/sh\x00-c\x00some-mcp", 5.0, 120)
        host.add(122, b"/usr/bin/node\x00server.js", 7000.0, 121)
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.tier, c.root_pid) for c in found] == [(TIER_TOOL, 110)]

    def test_an_unsandboxed_kiro_cli_runtime_reaches_its_engine_shell(self) -> None:
        # Sandbox off: the runtime pid is kiro-cli itself, its engine its only child.
        host = FakeHost()
        host.add(100, b"/tools/kiro-cli/3.5.0/kiro-cli\x00acp")
        host.add(104, b"/share/kiro-cli/node\x00--experimental-wasm-modules\x00kas.js", 900.0, 100)
        host.add(110, b"/bin/bash\x00-c\x00make", 5.0, 104)
        host.add(111, b"/usr/bin/node\x00build.js", 6000.0, 110)
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.tier, c.root_pid) for c in found] == [(TIER_TOOL, 110)]

    def test_an_mcp_server_configured_as_a_shell_command_is_never_a_root(self) -> None:
        # ``"command": "bash", "args": ["-c", "node server.js"]``: same shape and parent
        # as the shell tool, without the shell tool's launch marker.
        host = FakeHost()
        _runtime(host, 100)
        host.add(103, b"/tools/kiro-cli/kiro-cli\x00acp", 300.0, 100)
        host.add(104, b"/share/kiro-cli/node\x00kas.js", 900.0, 103)
        host.add(130, b"bash\x00-c\x00node server.js", 5.0, 104)
        host.add(131, b"/usr/bin/node\x00server.js", 7000.0, 130)
        host.unmarked.add(130)
        assert scan({100: "chat-a"}, frozenset({100}), host.view()) == []

    def test_a_shell_an_npx_launcher_runs_beside_the_shell_tool_is_never_a_root(self) -> None:
        # kiro-cli v2 shape: the harness hosts the shell tool and the MCP launcher.
        host = FakeHost()
        _runtime(host, 100)
        host.add(103, b"/usr/bin/kiro-cli\x00acp", 300.0, 100)
        host.add(110, b"/bin/bash\x00-c\x00ls", 5.0, 103)
        host.add(120, b"/usr/bin/node\x00/npm/bin/npx-cli.js\x00some-mcp", 50.0, 103)
        host.add(121, b"/bin/sh\x00-c\x00some-mcp", 5.0, 120)
        host.add(122, b"/usr/bin/node\x00some-mcp/server.js", 7000.0, 121)
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [c.root_pid for c in found] == [110]
        assert pick(found, {}, 0.0) is None  # the shell tool's tree is under the floor

    def test_a_runtime_pid_itself_is_never_a_target(self) -> None:
        host = FakeHost()
        host.add(100, PYTEST, 9000.0)  # a runtime whose own argv looks like pytest
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert all(100 not in [pid for pid, _ in c.targets] for c in found)

    def test_non_shell_tier2_root_and_a_tree_under_the_floor_are_never_picked(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        host.add(110, b"/usr/bin/node\x00server.js", 6000.0, 100)  # not a shell
        _shell_tree(host, 100, 200, 400.0)  # a shell tree of 405 MB
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [c.root_pid for c in found] == [200]
        assert pick(found, {}, 0.0) is None  # under 512 MB

    def test_a_detached_leftover_test_tree_is_in_reach(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        _pytest_tree(host, 1, 400, 600.0)  # reparented to init, same session id
        host.left[100] = [401, 400, 402]  # listed in any order
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.root_pid, c.procs) for c in found] == [(400, 3)]

    def test_a_pid_recycled_after_the_walk_is_never_a_target(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        _pytest_tree(host, 100, 110, 400.0)
        host.add(113, b"/usr/bin/python3\x00helper.py", 50.0, 112)  # below worker 112
        host.parents[112] = 4242  # 112 exited and its pid now belongs to another process
        found = scan({100: "chat-a"}, frozenset({100}), host.view())
        assert [(c.root_pid, sorted(p for p, _ in c.targets)) for c in found] == [(110, [110, 111])]

    def test_a_detached_mcp_launcher_walls_its_leftover_children(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        host.add(500, b"npx\x00-y\x00@playwright/mcp", 50.0, 1)  # reparented to init
        host.add(501, b"/bin/sh\x00-c\x00mcp-server", 5.0, 500)
        host.add(502, b"/usr/bin/node\x00server.js", 7000.0, 501)
        host.left[100] = [502, 501, 500]  # the session lists all three
        assert scan({100: "chat-a"}, frozenset({100}), host.view()) == []

    def test_a_pid_recycled_after_its_argv_was_judged_is_never_a_target(self) -> None:
        # Worker 112's argv is read under one identity, then the pid names a new process.
        host = FakeHost()
        _runtime(host, 100)
        _pytest_tree(host, 100, 110, 400.0)
        reads: dict[int, int] = {}

        def start_id(pid: int) -> str | None:
            reads[pid] = reads.get(pid, 0) + 1
            return f"s{pid}" if pid != 112 or reads[pid] == 1 else "reused"

        view = dataclasses.replace(host.view(), start_id=start_id)
        found = scan({100: "chat-a"}, frozenset({100}), view)
        assert [sorted(p for p, _ in c.targets) for c in found] == [[110, 111]]

    def test_a_leftover_root_whose_identity_changed_is_dropped(self) -> None:
        host = FakeHost()
        _runtime(host, 100)
        _pytest_tree(host, 1, 400, 600.0)
        host.left[100] = [400, 401, 402]
        view = host.view()
        moved = dataclasses.replace(view, start_id=lambda pid: "new" if pid == 400 else f"s{pid}")
        assert scan({100: "chat-a"}, frozenset({100}), moved) == []

    def test_cooldown_skips_a_just_stopped_session_unless_it_is_the_only_one(self) -> None:
        big = Candidate("chat-a", 10, TIER_TEST, 1, "s1", "pytest", 4000, 3, ((1, "s1"),))
        small = Candidate("chat-b", 20, TIER_TEST, 2, "s2", "pytest", 900, 3, ((2, "s2"),))
        stopped = {"chat-a": 100.0}
        assert pick([big, small], stopped, 100.0 + COOLDOWN_SECS - 1) is small
        assert pick([big], stopped, 100.0 + COOLDOWN_SECS - 1) is big
        assert pick([big, small], stopped, 100.0 + COOLDOWN_SECS) is big


# ── the stop, against real processes ────────────────────────────────────────

linux_only = pytest.mark.skipif(
    sys.platform != "linux" or not _process_group_supervisor.can_reap(),
    reason="the pinned stop needs Linux and a kernel that pins processes with pidfd_open",
)


def _spawn(ignore_term: bool) -> subprocess.Popen[bytes]:
    code = (
        "import signal,sys,time\n"
        + ("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "")
        + "sys.stdout.write('ready\\n'); sys.stdout.flush()\ntime.sleep(60)\n"
    )
    # No inherited ignore or block: the cooperative child must take SIGTERM.
    with spawn_test_helpers.unblocked_signals():
        proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE)
    assert proc.stdout is not None and proc.stdout.readline() == b"ready\n"
    # Reaped at once, as a session host reaps its own children: an unreaped child
    # keeps its start identity as a zombie and would read as a SIGTERM survivor.
    threading.Thread(target=proc.wait, daemon=True).start()
    return proc


def _candidate(*procs: subprocess.Popen[bytes], start: str | None = None) -> Candidate:
    targets = tuple(
        (p.pid, start or str(platform_compat.get_process_start_id(p.pid))) for p in procs
    )
    return Candidate(
        "chat-a", 10, TIER_TEST, procs[-1].pid, targets[-1][1], "pytest", 900, 1, targets
    )


@pytest.fixture
def reap() -> Any:
    """Kill and wait every child a stop test spawned, whatever the test's outcome."""
    procs: list[subprocess.Popen[bytes]] = []
    yield lambda proc: procs.append(proc) or proc
    for proc in procs:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=5)


@linux_only
# Each case starts a real interpreter child: one xdist group runs them one at a time.
@pytest.mark.xdist_group(name="memory_guard_real_children")
class TestStop:
    def test_term_ends_a_cooperative_tree_without_waiting_the_grace(self, reap: Any) -> None:
        proc = reap(_spawn(ignore_term=False))
        ended, _label = session_memory_guard.stop(_candidate(proc), frozenset())
        assert ended == 1
        proc.wait(timeout=5)
        # The exit status, not elapsed time, proves SIGTERM ended it: a SIGKILL
        # after the grace would read -SIGKILL here.
        assert proc.returncode == -signal.SIGTERM

    def test_a_survivor_gets_kill_after_the_grace(
        self, monkeypatch: pytest.MonkeyPatch, reap: Any
    ) -> None:
        monkeypatch.setattr(
            session_memory_guard, "GRACE_SECS", 0.3
        )  # flake-ok: SIGTERM is ignored, so the grace always runs out; this only shortens it
        proc = reap(_spawn(ignore_term=True))
        ended, sig = session_memory_guard.stop(_candidate(proc), frozenset())
        assert (ended, sig) == (1, "KILL")
        proc.wait(timeout=5)
        assert proc.returncode == -signal.SIGKILL

    def test_a_recycled_pid_is_not_signalled(self) -> None:
        proc = _spawn(ignore_term=False)
        try:
            ended, _ = session_memory_guard.stop(
                _candidate(proc, start="not-its-start"), frozenset()
            )
            assert ended == 0
            assert proc.poll() is None  # still running: the identity did not match
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_a_protected_pid_is_not_signalled(self) -> None:
        proc = _spawn(ignore_term=False)
        try:
            ended, _ = session_memory_guard.stop(_candidate(proc), frozenset({proc.pid}))
            assert ended == 0 and proc.poll() is None
        finally:
            proc.kill()
            proc.wait(timeout=5)


# ── one pass, end to end over a fake manager ────────────────────────────────


def _status(gb: float = 1.4) -> ResourceStatus:
    return ResourceStatus(
        available_gb=gb,
        cpu_count=8,
        load_per_cpu=0.5,
        posture=POSTURE_CRITICAL,
        pressure_gb=4.0,
        critical_gb=2.0,
    )


class FakeManager:
    def __init__(self, sessions: dict[str, int]) -> None:
        self._lock = asyncio.Lock()
        self._pids = sessions
        self._cleanup_boundary = lambda: SimpleNamespace(
            session_runtime_pids=lambda: [
                (k, self._pids[k], self._sessions[k]) for k in self._sessions
            ]
        )
        self._sessions = {
            k: SimpleNamespace(
                semaphore=asyncio.Semaphore(1),
                # The start identity the client recorded at spawn (FakeHost's form).
                provider=SimpleNamespace(client=SimpleNamespace(_start_time=f"s{pid}")),
            )
            for k, pid in sessions.items()
        }
        self._memory_guard = MemoryGuardState()
        self.told: list[tuple[str, str, str | None]] = []
        self.on_memory_guard_stop = lambda key, notice, summary: self.told.append(
            (key, notice, summary)
        )

    def _pool_pids(self) -> set[int]:
        return set()

    def _in_flight_pids(self) -> set[int]:
        return set()

    def _companion_runtime_pids(self) -> set[int]:
        return set()


@pytest.fixture
def two_sessions(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeManager, list[Candidate]]:
    host = FakeHost()
    _runtime(host, 100)
    _pytest_tree(host, 100, 110, 1500.0)
    _runtime(host, 200)
    _pytest_tree(host, 200, 210, 400.0)

    def view_off_the_loop() -> ProcessView:
        # The Linux view reads every /proc/<pid>/stat, so it is built on the executor.
        assert threading.current_thread() is not threading.main_thread()
        return host.view()

    monkeypatch.setattr(session_memory_guard, "host_view", view_off_the_loop)
    stops: list[Candidate] = []

    def fake_stop(candidate: Candidate, protected: frozenset[int]) -> tuple[int, str]:
        assert threading.current_thread() is not threading.main_thread()  # off the loop
        assert {100, 200, GATEWAY} <= protected
        stops.append(candidate)
        return candidate.procs, "TERM"

    monkeypatch.setattr(session_memory_guard, "stop", fake_stop)
    return FakeManager({"chat-a": 100, "chat-b": 200}), stops


class TestPass:
    def test_linux_stops_one_tree_logs_tells_the_chat_and_files_the_report(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        manager, stops = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        caplog.set_level(logging.WARNING, logger="kiro_crew.session_memory_guard")
        asyncio.run(session_memory_guard.pressure_reclaim(manager, _status(), 23.5, "critical"))
        assert [c.root_pid for c in stops] == [110]  # one tree per pass, the largest
        lines = [r.getMessage() for r in caplog.records if "Memory guard" in r.getMessage()]
        assert len(lines) == 1
        for part in (
            "stopped tier=test",
            "session=chat-a",
            "root_pid=110",
            "start=s110",
            "cmd=pytest",
            "tree_rss_mb=3100",
            "procs=3",
            "available_gb=1.4",
            "psi_full_avg10=23.50",
            "trigger=critical",
            "signal=TERM",
            "freed=yes",
        ):
            assert part in lines[0]
        assert len(manager.told) == 1
        key, notice, summary = manager.told[0]
        assert key == "chat-a"
        assert "Crew stopped `pytest` (3.0 GB, 3 processes)" in notice
        assert (
            summary
            == "memory guard: stopped pytest (3.0 GB); host memory ~1.4 GB free; the worker keeps running"
        )

    def test_a_psi_stop_says_pressure_not_critically_low(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _ = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        asyncio.run(session_memory_guard.pressure_reclaim(manager, _status(gb=3.1), 24.0, "psi"))
        notice = manager.told[0][1]
        assert notice.startswith("Host memory was under heavy pressure (~3.1 GB free)")
        assert "critically" not in notice

    @pytest.mark.parametrize("posture", [POSTURE_AMPLE, POSTURE_TIGHT])
    def test_a_cgroup_stall_stop_says_crew_cannot_read_its_memory_use(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any, posture: str
    ) -> None:
        # ``psi-cgroup`` fires only while the memory cgroup is out of view, so the free
        # figure is the host's and must not read as the shortage.
        manager, _ = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        caplog.set_level(logging.WARNING, logger="kiro_crew.session_memory_guard")
        status = dataclasses.replace(_status(gb=11.3), posture=posture)
        asyncio.run(session_memory_guard.pressure_reclaim(manager, status, 37.5, "psi-cgroup"))
        _, notice, summary = manager.told[0]
        assert notice.startswith(
            "Memory was under heavy pressure (stalled in Crew's cgroup, whose memory use Crew "
            "cannot read; ~11.3 GB free on the host), so Crew stopped `pytest` (3.0 GB, 3 "
            "processes)"
        )
        assert "Host memory" not in notice
        assert summary == (
            "memory guard: stopped pytest (3.0 GB); memory stalled in Crew's cgroup, whose "
            "memory use Crew cannot read (~11.3 GB free on the host); the worker keeps running"
        )
        # The figure handed over is a share of time stalled, so it is not named avg10.
        lines = [r.getMessage() for r in caplog.records if "Memory guard" in r.getMessage()]
        assert len(lines) == 1
        assert "psi_full_pct=37.50 trigger=psi-cgroup" in lines[0]
        assert "avg10" not in lines[0]

    def test_a_host_psi_stop_on_an_ample_reading_names_the_host(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the trigger says the memory cgroup is out of view; the posture alone does not.
        manager, _ = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        status = dataclasses.replace(_status(gb=11.3), posture=POSTURE_AMPLE)
        asyncio.run(session_memory_guard.pressure_reclaim(manager, status, 37.5, "psi"))
        _, notice, summary = manager.told[0]
        assert notice.startswith("Host memory was under heavy pressure (~11.3 GB free)")
        assert summary is not None and "host memory ~11.3 GB free" in summary
        assert "cannot read" not in notice + summary

    @pytest.mark.parametrize(
        ("shared", "can_stop", "opening"),
        [
            (False, False, "Memory is under heavy pressure (stalled in Crew's cgroup,"),
            (True, True, "Memory was under heavy pressure (stalled in Crew's cgroup,"),
            (True, False, "Memory is under heavy pressure (stalled in Crew's cgroup,"),
        ],
    )
    def test_every_cgroup_stall_notice_says_crew_cannot_read_its_memory_use(
        self, shared: bool, can_stop: bool, opening: str
    ) -> None:
        tree = Candidate(
            session_key="chat-a",
            runtime_pid=100,
            tier=TIER_TEST,
            root_pid=110,
            root_start="s110",
            label="pytest",
            rss_mb=3100,
            procs=3,
            targets=((110, "s110"),),
        )
        status = dataclasses.replace(_status(gb=11.3), posture=POSTURE_AMPLE)
        if shared:
            notice = session_memory_guard.shared_notice(
                tree, status, 2, stopped=can_stop, can_stop=can_stop, trigger="psi-cgroup"
            )
        else:
            notice = session_memory_guard.chat_notice(
                tree, status, stopped=False, can_stop=can_stop, trigger="psi-cgroup"
            )
        assert notice.startswith(opening)
        assert "whose memory use Crew cannot read; ~11.3 GB free on the host" in notice
        assert "Host memory" not in notice

    def test_an_agent_ceiling_reading_is_named_as_such(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, _ = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        # The two fields ``memory_reading`` reads from ``AgentCeiling``.
        ceiling = SimpleNamespace(ceiling_gb=40.0, host_available_gb=20.0)
        status = dataclasses.replace(_status(), agent_ceiling=ceiling)
        asyncio.run(session_memory_guard.pressure_reclaim(manager, status, None, "critical"))
        _, notice, summary = manager.told[0]
        assert notice.startswith("Agent memory was critically low (~1.4 GB left under the 40")
        assert "Host memory" not in notice
        assert summary is not None and "agent memory ~1.4 GB left under the 40" in summary

    def test_a_shared_runtime_tells_every_tenant_and_files_no_report(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any
    ) -> None:
        _, stops = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        caplog.set_level(logging.WARNING, logger="kiro_crew.session_memory_guard")
        manager = FakeManager({"chat-a": 100, "chat-c": 100, "chat-b": 200})
        asyncio.run(session_memory_guard.pressure_reclaim(manager, _status(), None, "critical"))
        assert [c.root_pid for c in stops] == [110]
        assert "tenants=2" in caplog.records[-1].getMessage()
        assert sorted(key for key, _, _ in manager.told) == ["chat-a", "chat-c"]
        assert all(summary is None for _, _, summary in manager.told)
        assert all("cannot tell which of them started it" in n for _, n, _ in manager.told)

    def test_a_shared_runtime_on_macos_credits_no_single_chat(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: False)
        manager = FakeManager({"chat-a": 100, "chat-c": 100, "chat-b": 200})
        asyncio.run(session_memory_guard.pressure_reclaim(manager, _status(), None, "critical"))
        assert sorted(key for key, _, _ in manager.told) == ["chat-a", "chat-c"]
        for _, notice, summary in manager.told:
            assert summary is None and "this chat started" not in notice
            assert "cannot stop it safely" in notice and "cannot tell which" in notice

    def test_a_runtime_pid_now_naming_another_process_is_not_walked(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager, stops = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        for session in manager._sessions.values():
            session.provider.client._start_time = "spawned-earlier"  # pid since reused
        asyncio.run(session_memory_guard.pressure_reclaim(manager, _status(), None, "critical"))
        assert stops == [] and manager.told == []

    def test_a_failed_stop_is_told_once_and_a_later_success_still_reports(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        manager = FakeManager({"chat-a": 100})
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)
        results = iter([(0, "TERM"), (0, "TERM"), (3, "TERM")])
        monkeypatch.setattr(session_memory_guard, "stop", lambda c, p: next(results))

        async def scenario() -> None:
            for _ in range(3):
                await session_memory_guard.pressure_reclaim(manager, _status(), None, "critical")

        asyncio.run(scenario())
        assert [summary is not None for _, _, summary in manager.told] == [False, True]
        assert "could not be stopped" in manager.told[0][1]
        assert "Crew stopped" in manager.told[1][1]

    def test_the_next_pass_skips_the_cooled_down_session(
        self, two_sessions: Any, monkeypatch: Any
    ) -> None:
        manager, stops = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: True)

        async def scenario() -> None:
            await session_memory_guard.pressure_reclaim(manager, _status(), None, "critical")
            await session_memory_guard.pressure_reclaim(manager, _status(), None, "critical")

        asyncio.run(scenario())
        # chat-a's tree is still listed by the fake host, but chat-b now goes first.
        assert [c.session_key for c in stops] == ["chat-a", "chat-b"]

    @pytest.mark.parametrize("arm", ["darwin", "win32"])
    def test_macos_and_windows_stop_nothing_and_file_no_report(
        self, two_sessions: Any, monkeypatch: pytest.MonkeyPatch, caplog: Any, arm: str
    ) -> None:
        manager, stops = two_sessions
        monkeypatch.setattr(session_memory_guard, "can_stop_trees", lambda: False)
        if arm == "win32":
            monkeypatch.setattr(session_memory_guard, "host_view", lambda: None)
        caplog.set_level(logging.WARNING, logger="kiro_crew.session_memory_guard")

        async def scenario() -> None:
            for _ in range(3):  # sampled every 5 s while critical: told once
                await session_memory_guard.pressure_reclaim(manager, _status(), None, "critical")

        asyncio.run(scenario())
        assert stops == []
        lines = [r.getMessage() for r in caplog.records if "Memory guard" in r.getMessage()]
        assert len(lines) == 1
        if arm == "darwin":
            assert "could not stop (unsupported host)" in lines[0] and "freed=no" in lines[0]
            assert len(manager.told) == 1
            key, notice, summary = manager.told[0]
            assert key == "chat-a" and summary is None
            assert "cannot stop it safely on this host" in notice
        else:
            assert "cannot read tool process trees" in lines[0]
            assert manager.told == []

    def test_request_is_single_flight(self, two_sessions: Any, monkeypatch: Any) -> None:
        manager, _ = two_sessions
        started: list[int] = []
        gate = asyncio.Event

        async def scenario() -> None:
            release = gate()

            async def slow(*_a: Any) -> None:
                started.append(1)
                await release.wait()

            monkeypatch.setattr(session_memory_guard, "pressure_reclaim", slow)
            first = session_memory_guard.request(manager, _status(), None, "critical")
            await asyncio.sleep(0)
            # The caller is handed the pass that runs, so it can tell when it ends.
            assert session_memory_guard.request(manager, _status(), None, "critical") is first
            await asyncio.sleep(0)
            release.set()
            await manager._memory_guard.task
            assert first.done()

        asyncio.run(scenario())
        assert started == [1]
