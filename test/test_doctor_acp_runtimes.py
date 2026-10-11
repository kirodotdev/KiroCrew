"""The ``ACP Runtimes`` rows of ``kirocrew doctor``.

A runtime that outlives its session can hold a core with no turn in flight, and
enough of them take the host down; this section is what names them in the health
pass. Every census test runs against a fabricated process table, so nothing here
depends on what the host running the suite has alive.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from kiro_crew import cli_doctor, platform_compat, session_pid
from kiro_crew.constants import (
    KIROCREW_SPAWN_HOME_ENV,
    KIROCREW_SPAWNED_ENV,
    KIROCREW_SPAWNED_VALUE,
)
from kiro_crew.doctor_checks import resources

_HOME = "/home/someone/.kiro/crew"
_SECRET = "sk-live-0123456789abcdef"


def _stat_line(pid: int, ppid: int, cpu: int, start: int) -> str:
    # Post-comm tokens: state(0) ppid(1) pgrp(2) session(3) ... utime(11)
    # stime(12) ... starttime(19) vsize(20) rss(21).
    tokens = ["S", ppid, ppid, ppid, 0, 0, 0, 0, 0, 0, 0, cpu, 0, 0, 0, 20, 0, 1, 0, start, 0, 0]
    return f"{pid} (x) " + " ".join(str(token) for token in tokens)


class _Table:
    """A fabricated ``/proc``: one directory per pid, written as the kernel would."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.later: dict[int, int] = {}
        self.gone_later: set[int] = set()
        self.now = 0.0

    def add(
        self,
        pid: int,
        ppid: int,
        argv: list[str],
        *,
        cpu: int = 0,
        cpu_later: int | None = None,
        start: int = 500,
        env: dict[str, str] | None = None,
    ) -> None:
        proc = self.root / str(pid)
        proc.mkdir(parents=True)
        (proc / "stat").write_text(_stat_line(pid, ppid, cpu, start), encoding="ascii")
        (proc / "cmdline").write_bytes(b"\x00".join(a.encode() for a in argv) + b"\x00")
        if env is not None:
            (proc / "environ").write_bytes(
                b"\x00".join(f"{k}={v}".encode() for k, v in env.items()) + b"\x00"
            )
        self.later[pid] = cpu if cpu_later is None else cpu_later

    def clock(self) -> float:
        return self.now

    def sleep(self, secs: float) -> None:
        """Advance the window and move every counter to its later value."""
        self.now += secs
        for pid, cpu in self.later.items():
            stat = self.root / str(pid) / "stat"
            if pid in self.gone_later:
                stat.unlink()
                continue
            line = stat.read_text(encoding="ascii").split()
            line[2 + 11] = str(cpu)  # pid, comm, then post-comm index 11 (utime)
            stat.write_text(" ".join(line), encoding="ascii")

    def census(self, *, tracked=None, tracked_pids=(), gateway_pid=1, ownership_known=True):
        return self.full_census(
            tracked=tracked,
            tracked_pids=tracked_pids,
            gateway_pid=gateway_pid,
            ownership_known=ownership_known,
        ).runtimes

    def full_census(self, *, tracked=None, tracked_pids=(), gateway_pid=1, ownership_known=True):
        return resources._acp_runtime_census(
            tracked=tracked or {},
            tracked_pids=set(tracked_pids),
            gateway_pid=gateway_pid,
            ownership_known=ownership_known,
            own_home=_HOME,
            proc_root=self.root,
            window_secs=1.0,
            sleep=self.sleep,
            clock=self.clock,
            clk_tck=100,
            age_of=lambda start: 7200.0,
        )


_MARKED = {KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE, KIROCREW_SPAWN_HOME_ENV: _HOME}


@pytest.fixture
def table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Table:
    fabricated = _Table(tmp_path / "proc")
    # The reaper's untracked predicate reads the marker from the live /proc; point
    # it at the fabricated table, through the same reader it uses there.
    monkeypatch.setattr(
        session_pid,
        "_env_has_kirocrew_marker",
        lambda pid, proc_root=None: session_pid._read_env_has_kirocrew_marker(pid, fabricated.root)
        is True,
    )
    return fabricated


def _recorded_pair(table: _Table) -> None:
    """A sandbox launcher the gateway recorded, its harness, and what the harness runs."""
    table.add(1, 0, ["/sbin/init"])
    table.add(
        10,
        1,
        ["/opt/sandbox/launcher", "--session-id", "u-1", "/opt/kiro-cli", "acp"],
        cpu=100,
        cpu_later=120,
    )
    table.add(11, 10, ["/opt/kiro-cli-chat", "acp"], cpu=50, cpu_later=65)
    table.add(12, 11, ["bash", "-c", "make -j8"], cpu=0, cpu_later=90)
    table.add(13, 11, ["node", "/srv/mcp/server.js"], cpu=0, cpu_later=40)


# ── The census ────────────────────────────────────────────────────────────────


def test_a_recorded_launcher_pair_is_one_runtime_and_its_tools_are_not_its_backend(
    table: _Table,
) -> None:
    """The launcher and the harness are the backend; a build and an MCP server the
    harness runs are not, so a session doing real work does not read as spinning."""
    _recorded_pair(table)
    runtimes = table.census(tracked={10: (1, "500")})
    assert runtimes == [resources._AcpRuntime(10, "gateway", pytest.approx(35.0), 7200.0)]


def test_a_recycled_registry_pid_is_not_a_runtime(table: _Table) -> None:
    _recorded_pair(table)
    assert table.census(tracked={10: (1, "499")}) == []


def test_an_entry_without_a_recorded_start_still_counts(table: _Table) -> None:
    _recorded_pair(table)
    assert [r.root for r in table.census(tracked={10: (1, None)})] == [10]


def test_a_dead_registry_entry_is_not_a_runtime(table: _Table) -> None:
    table.add(1, 0, ["/sbin/init"])
    assert table.census(tracked={77: (1, "500")}) == []


def test_a_runtime_another_gateway_recorded_has_no_running_owner(table: _Table) -> None:
    _recorded_pair(table)
    (runtime,) = table.census(tracked={10: (4242, "500")}, gateway_pid=1)
    assert runtime.owner == "stale"


def test_a_runtime_a_live_cli_chat_recorded_is_owned_and_not_called_idle(table: _Table) -> None:
    """``kirocrew chat`` records its runtime under its own pid; while that process is
    still the runtime's ancestor a turn may be in flight, so it is not ownerless."""
    table.add(1, 0, ["/sbin/init"])
    table.add(7, 1, ["/usr/bin/python3", "-m", "kiro_crew", "chat"])
    table.add(10, 7, ["/opt/kiro-cli", "acp"], cpu=0, cpu_later=40)
    (runtime,) = table.census(tracked={10: (7, "500")}, gateway_pid=99)
    assert runtime.owner == "other"
    lines = resources._acp_runtime_lines(resources._AcpCensus([runtime]), 1.0)
    assert lines[0] == ("  runtimes:    1 alive: 1 owned by another live process (a kirocrew chat)")
    assert "               pid 10: 40%, up 2.0h, owned by another live process" in lines
    assert not any(line.startswith("  no owner:") for line in lines)


def test_a_recorder_pid_that_is_alive_but_not_an_ancestor_does_not_own_it(
    table: _Table,
) -> None:
    """A recycled recorder pid names a stranger, not the process that held the runtime."""
    _recorded_pair(table)
    table.add(7, 1, ["/usr/bin/python3", "-m", "kiro_crew", "chat"])
    (runtime,) = table.census(tracked={10: (7, "500")}, gateway_pid=99)
    assert runtime.owner == "stale"


def test_no_running_gateway_means_no_runtime_has_an_owner(table: _Table) -> None:
    _recorded_pair(table)
    (runtime,) = table.census(tracked={10: (1, "500")}, gateway_pid=None)
    assert runtime.owner == "stale"


def test_an_indeterminate_lock_probe_names_no_owner(table: _Table) -> None:
    _recorded_pair(table)
    (runtime,) = table.census(tracked={10: (1, "500")}, gateway_pid=None, ownership_known=False)
    assert runtime.owner == "unknown"


def test_an_unrecorded_runtime_this_install_spawned_is_untracked_with_its_launcher(
    table: _Table,
) -> None:
    table.add(1, 0, ["/sbin/init"])
    table.add(
        20,
        1,
        [
            "/lib64/ld-linux-x86-64.so.2",
            "/opt/python3.12",
            "-I",
            "-S",
            f"{_HOME}/run/kirocrew_sandbox_20_ab.py",
            "/opt/kiro-cli",
            "acp",
        ],
        cpu=0,
        cpu_later=30,
        env=_MARKED,
    )
    table.add(21, 20, ["/opt/kiro-cli", "acp"], cpu=0, cpu_later=5, env=_MARKED)
    table.add(22, 21, ["/opt/kiro-cli-chat", "acp"], cpu=0, cpu_later=5, env=_MARKED)
    assert table.census() == [resources._AcpRuntime(20, "untracked", pytest.approx(40.0), 7200.0)]


@pytest.mark.parametrize(
    "env",
    [
        None,  # a user's own kiro-cli in a terminal: no environment marker readable
        {"PATH": "/usr/bin"},
        {KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE, KIROCREW_SPAWN_HOME_ENV: "/other/home"},
        {KIROCREW_SPAWNED_ENV: KIROCREW_SPAWNED_VALUE},
    ],
)
def test_a_harness_this_install_did_not_spawn_is_not_counted(table: _Table, env) -> None:
    table.add(1, 0, ["/sbin/init"])
    table.add(30, 1, ["/opt/kiro-cli", "acp"], cpu=0, cpu_later=80, env=env)
    assert table.census() == []


def test_a_harness_inside_a_recorded_runtime_is_not_counted_twice(table: _Table) -> None:
    """A ``kiro-cli`` a session runs as a tool belongs to that session's tree, and
    to the tool: it pulls neither itself nor the tool command into the backend."""
    _recorded_pair(table)
    table.add(14, 12, ["/opt/kiro-cli", "chat", "--no-interactive"], cpu=0, cpu_later=50)
    table.add(15, 14, ["/opt/kiro-cli-chat", "chat"], cpu=0, cpu_later=50, env=_MARKED)
    assert table.census(tracked={10: (1, "500")}) == [
        resources._AcpRuntime(10, "gateway", pytest.approx(35.0), 7200.0)
    ]


def test_an_unrecorded_runtime_the_running_gateway_still_parents_is_its_own(
    table: _Table,
) -> None:
    """A runtime whose pid-file append failed is still the running gateway's: it may
    be in the middle of a turn, so it must not be called ownerless."""
    table.add(1, 0, ["/sbin/init"])
    table.add(5, 1, ["/usr/bin/python3", "-m", "kiro_crew", "gateway"])
    table.add(21, 5, ["/opt/kiro-cli", "acp"], env=_MARKED)
    (runtime,) = table.census(gateway_pid=5)
    assert (runtime.root, runtime.owner) == (21, "gateway")


def test_a_harness_either_pid_file_lists_is_not_untracked(table: _Table) -> None:
    """The reaper's own predicate decides "untracked", so doctor and the reaper agree."""
    table.add(1, 0, ["/sbin/init"])
    table.add(30, 1, ["/opt/kiro-cli", "acp"], env=_MARKED)
    assert table.census(tracked_pids={30}) == []
    assert [r.owner for r in table.census()] == ["untracked"]


def test_the_tree_cap_holds_at_every_insertion(monkeypatch: pytest.MonkeyPatch) -> None:
    """One wide level cannot carry the map past the cap: it is checked per child."""
    monkeypatch.setattr(resources, "_ACP_TREE_CAP", 3)
    tree, refused = resources._subtree(1, {1: [2, 3, 4], 2: [5, 6]})
    assert (sorted(tree), refused) == ([1, 2, 3], 3)


def test_a_tree_past_the_cap_is_counted_and_what_it_refused_is_not_a_runtime(
    table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "_ACP_TREE_CAP", 3)
    _recorded_pair(table)  # 10 -> 11 -> {12, 13}: the cap of 3 refuses 13
    table.add(16, 13, ["/opt/kiro-cli", "acp"], env=_MARKED)
    census = table.full_census(tracked={10: (1, "500")})
    assert [r.root for r in census.runtimes] == [10]
    assert (census.capped_trees, census.capped_procs) == (1, 1)


def test_a_host_past_the_table_cap_counts_what_it_did_not_read(
    table: _Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(resources, "_ACP_TABLE_CAP", 2)
    _recorded_pair(table)  # five processes
    assert table.full_census().unread == 3


def test_a_process_that_exits_inside_the_window_adds_no_delta(table: _Table) -> None:
    _recorded_pair(table)
    table.gone_later.add(11)
    (runtime,) = table.census(tracked={10: (1, "500")})
    assert runtime.cpu_pct == pytest.approx(20.0)


def test_no_clock_tick_rate_takes_no_rate(table: _Table) -> None:
    _recorded_pair(table)
    (runtime,) = resources._acp_runtime_census(
        tracked={10: (1, "500")},
        tracked_pids=set(),
        gateway_pid=1,
        ownership_known=True,
        own_home=_HOME,
        proc_root=table.root,
        sleep=table.sleep,
        clock=table.clock,
        clk_tck=0,
        age_of=lambda start: None,
    ).runtimes
    assert runtime.cpu_pct is None


def test_an_unlistable_process_table_is_none_not_an_empty_host(tmp_path: Path) -> None:
    assert (
        resources._acp_runtime_census(
            tracked={},
            tracked_pids=set(),
            gateway_pid=1,
            ownership_known=True,
            own_home=_HOME,
            proc_root=tmp_path / "absent",
            sleep=lambda s: None,
        )
        is None
    )


# ── The rows ──────────────────────────────────────────────────────────────────


def _rt(root: int, owner: str = "gateway", cpu: float | None = 1.0, age: float | None = 3600.0):
    return resources._AcpRuntime(root, owner, cpu, age)


def test_no_runtime_is_one_line() -> None:
    assert resources._acp_runtime_lines(resources._AcpCensus([]), 1.0) == [
        "  runtimes:    ✅ none running"
    ]


def test_a_quiet_host_reads_green() -> None:
    lines = resources._acp_runtime_lines(
        resources._AcpCensus([_rt(10, cpu=2.0), _rt(20, cpu=3.0)]), 1.0
    )
    assert lines == [
        "  runtimes:    2 alive: 2 owned by the running gateway",
        "  cpu:         ✅ none at or above 25% of a core (all 2 backends: 5% of a core over 1.0s)",
    ]


def test_hot_and_unowned_runtimes_are_named_by_pid() -> None:
    lines = resources._acp_runtime_lines(
        resources._AcpCensus(
            [
                _rt(10, cpu=2.0),
                _rt(20, "stale", cpu=61.0, age=102_000.0),
                _rt(30, "untracked", 30.0),
            ]
        ),
        1.0,
    )
    assert (
        lines[0]
        == "  runtimes:    3 alive: 1 owned by the running gateway, 2 no running gateway owns"
    )
    assert lines[1] == (
        "  cpu:         ⚠️  2 at or above 25% of a core (all 3 backends: 93% of a core over 1.0s)"
    )
    assert lines[2] == "               pid 20: 61%, up 28.3h, no running gateway owns it"
    assert lines[3] == "               pid 30: 30%, up 1.0h, no running gateway owns it"
    assert "check the session before ending it" in lines[6]
    assert lines[7:] == [
        "  no owner:    ⚠️  2 no running gateway owns, so no turn can be in flight on them",
        "               pids 20, 30",
    ]


def test_a_long_list_is_capped() -> None:
    hot = [_rt(100 + i, cpu=50.0) for i in range(resources._ACP_LISTED + 2)]
    lines = resources._acp_runtime_lines(resources._AcpCensus(hot), 1.0)
    assert "               … and 2 more" in lines


def test_an_unknown_owner_is_said_so() -> None:
    lines = resources._acp_runtime_lines(resources._AcpCensus([_rt(10, "unknown")]), 1.0)
    assert "could not answer" in lines[0]
    assert not any(line.startswith("  no owner:") for line in lines)


def test_a_cut_read_is_said_out_loud() -> None:
    census = resources._AcpCensus([], unread=7, capped_trees=1, capped_procs=9)
    assert resources._acp_runtime_lines(census, 1.0) == [
        "  runtimes:    ✅ none running",
        "  read cap:    ⚠️  7 processes past the first 65536 were not read; a runtime among"
        " them is not counted",
        "  tree cap:    ⚠️  1 runtime(s) hold more than 4096 processes; 9 past the cap were"
        " not walked",
    ]


def test_no_rate_is_said_so() -> None:
    lines = resources._acp_runtime_lines(resources._AcpCensus([_rt(10, cpu=None)]), 1.0)
    assert lines[1].startswith("  cpu:         ⏹ could not take a rate")


# ── The section ───────────────────────────────────────────────────────────────


def test_off_linux_the_section_is_one_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    issues: list[str] = []
    resources._doctor_acp_runtimes(issues)
    out = capsys.readouterr().out
    assert (
        out
        == "\nACP Runtimes\n  runtimes:    ⏹ not measured (darwin — the census reads Linux procfs)\n"
    )
    assert issues == []


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        ("registry", "could not read the session registry — check skipped"),
        ("raises", "could not take the census (probe failed) — check skipped"),
        ("table", "could not read the process table — check skipped"),
    ],
)
def test_every_failure_is_a_skipped_line_never_an_issue(monkeypatch, capsys, setup, expected):
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 1)
    monkeypatch.setattr(cli_doctor, "data_home", lambda: Path(_HOME))
    monkeypatch.setattr(
        cli_doctor.session_pid,
        "tracked_session_roots",
        (lambda: None) if setup == "registry" else (lambda: {}),
    )
    monkeypatch.setattr(cli_doctor.session_pid, "_tracked_agent_pids", set)
    if setup == "raises":

        def boom(**_kw):
            raise RuntimeError("probe")

        monkeypatch.setattr(resources, "_acp_runtime_census", boom)
    else:
        monkeypatch.setattr(resources, "_acp_runtime_census", lambda **_kw: None)
    issues: list[str] = []
    resources._doctor_acp_runtimes(issues)
    assert expected in capsys.readouterr().out
    assert issues == []


def test_the_report_carries_no_command_line(monkeypatch, capsys, table: _Table) -> None:
    """A harness's argv can hold a credential; the report names pids, never argv."""
    table.add(1, 0, ["/sbin/init"])
    table.add(10, 1, ["/opt/kiro-cli", "acp", f"--api-key={_SECRET}"], cpu=0, env=_MARKED)
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.setattr(resources, "_PROC_ROOT", table.root)
    monkeypatch.setattr(resources, "_ACP_CPU_WINDOW_SECS", 0.0)
    monkeypatch.setattr(resources, "_gateway_lock_indeterminate", lambda: False)
    monkeypatch.setattr(cli_doctor, "_read_gateway_pid", lambda: 1)
    monkeypatch.setattr(cli_doctor, "data_home", lambda: Path(_HOME))
    monkeypatch.setattr(cli_doctor.session_pid, "tracked_session_roots", lambda: {10: (1, "500")})
    monkeypatch.setattr(cli_doctor.session_pid, "_tracked_agent_pids", lambda: {10})
    resources._doctor_acp_runtimes([])
    out = capsys.readouterr().out
    assert "runtimes:    1 alive: 1 owned by the running gateway" in out
    assert _SECRET not in out
    assert "api-key" not in out


def test_the_orchestrator_runs_the_section_after_memory_pressure() -> None:
    """``_doctor()`` spawns, probes and exits, so its source is read rather than run."""
    source = inspect.getsource(cli_doctor._doctor)
    memory = source.index("resources._doctor_memory_pressure(issues)")
    assert source.index("resources._doctor_acp_runtimes(issues)") > memory


# ── The two readers the census is built on ────────────────────────────────────


def test_a_stat_read_carries_cpu_ticks_from_the_same_read(tmp_path: Path) -> None:
    (tmp_path / "1234").mkdir()
    (tmp_path / "1234" / "stat").write_bytes(
        b"1234 (kiro cli (node)) S 2 3 4 5 6 7 8 9 10 11 120 60 0 0 20 0 1 0 4242 0 55"
    )
    stat = platform_compat.read_proc_stat(1234, proc_root=tmp_path)
    assert stat is not None
    assert (stat.cpu_ticks, stat.start_ticks) == (180, 4242)


def test_a_stat_read_missing_stime_has_no_cpu_ticks(tmp_path: Path) -> None:
    (tmp_path / "1").mkdir()
    (tmp_path / "1" / "stat").write_bytes(b"1 (x) S 2 3 4 5 6 7 8 9 10 11 120")
    stat = platform_compat.read_proc_stat(1, proc_root=tmp_path)
    assert stat is not None and stat.cpu_ticks is None


def test_the_session_registry_reads_roots_with_their_recorder(monkeypatch, tmp_path: Path) -> None:
    path = tmp_path / "kiro_session_pids.txt"
    path.write_text(
        "100:10:500\n100:11\n100:12:\nbad\n100:x:1\n0:13:1\n100:14:1:extra\n100:10:999\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: path)
    assert session_pid.tracked_session_roots() == {
        10: (100, "500"),
        11: (100, None),
        12: (100, None),
    }


def test_a_missing_registry_is_empty_and_an_unreadable_one_is_none(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: tmp_path / "absent")
    assert session_pid.tracked_session_roots() == {}
    unreadable = tmp_path / "a-directory"
    unreadable.mkdir()
    monkeypatch.setattr(session_pid, "_session_pid_file_path", lambda: unreadable)
    assert session_pid.tracked_session_roots() is None
