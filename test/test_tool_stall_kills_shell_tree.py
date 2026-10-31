"""A tool-stall cancel also ends the matched shell command's process tree."""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_acp_liveness import FakeProc

from kiro_crew import _process_group_supervisor, platform_compat
from kiro_crew.acp import liveness, runtime_liveness
from kiro_crew.acp.liveness import (
    InFlightToolTracker,
    LivenessOracle,
    ProcessRow,
    ToolCallState,
)
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.types import STOP_REASON_TOOL_STALL
from kiro_crew.dashboard.state import build_tool_stall_recovery_prompt
from kiro_crew.testing.ids import unallocatable_pids
from kiro_crew.testing.wait import wait_until

RUNTIME = 100


class _DarwinTree:
    def __init__(self) -> None:
        self.parents: dict[int, int] = {}
        self.rows: dict[int, ProcessRow] = {}
        self.enumerable = True

    def add(self, pid: int, parent: int, command: str) -> None:
        self.parents[pid] = parent
        self.rows[pid] = ProcessRow(pid=pid, started=1_000.0, cmdline=command)

    def descendants(self, root_pid: int) -> list[int] | None:
        if not self.enumerable:
            return None
        result: list[int] = []
        frontier = [root_pid]
        while frontier:
            parents = set(frontier)
            frontier = [
                pid
                for pid, parent in self.parents.items()
                if parent in parents and pid not in result
            ]
            result.extend(frontier)
        return result

    def direct_children(self, parent_pid: int) -> list[int] | None:
        if not self.enumerable:
            return None
        return [pid for pid, parent in self.parents.items() if parent == parent_pid]

    def row(self, pid: int) -> ProcessRow | None:
        return self.rows.get(pid)

    def cpu_nanos(self, pid: int) -> int | None:
        return 0 if pid in self.rows or pid == RUNTIME else None


def _darwin_oracle(
    backend: _DarwinTree,
    *,
    cleanup_tenancy: Callable[[], int | None] | None = None,
    sandbox_wrapped_by_crew: bool = False,
) -> LivenessOracle:
    return LivenessOracle(
        "/missing-proc",
        now=lambda: 1_000.0,
        darwin_backend=backend,
        wall_now=lambda: 1_000.0,
        steady_now_fn=lambda: 1_000.0,
        cleanup_tenancy=(cleanup_tenancy if cleanup_tenancy is not None else lambda: 1),
        sandbox_wrapped_by_crew=lambda: sandbox_wrapped_by_crew,
    )


def _shell_tool(command: str = "python slow_query.py") -> ToolCallState:
    return ToolCallState(
        title="bash",
        command=command,
        dispatch_ts=1_000.0,
        dispatch_boot_ts=1_000.0,
        dispatch_steady_ts=1_000.0,
        is_shell=True,
    )


def _date_fake_proc_at_100_hz(monkeypatch: pytest.MonkeyPatch) -> None:
    """FakeProc's ticks are synthetic, so date them at a fixed 100 Hz on every host."""
    monkeypatch.setattr(liveness, "process_start_boot_secs", lambda ticks: ticks / 100.0)


def test_oracle_returns_the_matched_shell_tree_leaf_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.add(201, 200, "python slow_query.py")
    backend.add(202, 201, "helper --download-results")
    starts = {
        RUNTIME: "start-runtime",
        200: "start-200",
        201: "start-201",
        202: "start-202",
    }
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING

    assert oracle._tracked_child_start_id == "start-200"
    assert oracle.stalled_shell_tree(RUNTIME) == [
        (202, "start-202"),
        (201, "start-201"),
        (200, "start-200"),
    ]


@pytest.mark.parametrize("view", ["darwin", "procfs"])
def test_foreign_parent_exact_match_is_liveness_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path, view: str
) -> None:
    """An MCP server or an older shell can launch the same command after dispatch.

    Its child is evidence that work exists, but the current shell call did not launch
    that child directly and therefore has no authority to terminate it.
    """
    command = "/bin/sh -c python slow_query.py"
    starts = {
        RUNTIME: "start-runtime",
        150: "start-foreign-parent",
        200: "start-200",
    }
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    if view == "darwin":
        backend = _DarwinTree()
        backend.add(150, RUNTIME, "mcp-server --stdio")
        backend.add(200, 150, command)
        oracle = _darwin_oracle(backend)
    else:
        _date_fake_proc_at_100_hz(monkeypatch)
        fake = FakeProc(tmp_path / "proc")
        fake.add_pid(RUNTIME, children=[150], cmdline="kiro-cli acp")
        fake.add_pid(150, children=[200], cmdline="mcp-server --stdio")
        fake.add_pid(200, cmdline=command)
        oracle = LivenessOracle(
            str(fake.root),
            now=lambda: 1_000.0,
            cleanup_tenancy=lambda: 1,
            sandbox_wrapped_by_crew=lambda: False,
        )
    tool = _shell_tool()

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == 200
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_procfs_cleanup_refuses_an_unreadable_nested_branch(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _date_fake_proc_at_100_hz(monkeypatch)
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(RUNTIME, children=[200], cmdline="kiro-cli acp")
    fake.add_pid(200, children=[201], cmdline="/bin/sh -c python slow_query.py")
    fake.add_pid(201, children=[202], cmdline="helper --download-results")
    fake.add_pid(202, cmdline="worker --flush")
    (fake.root / "201" / "task" / "201" / "children").unlink()
    starts = {
        RUNTIME: "start-runtime",
        200: "start-200",
        201: "start-201",
        202: "start-202",
    }
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = LivenessOracle(
        str(fake.root),
        now=lambda: 1_000.0,
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=lambda: False,
    )

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child_start_id == "start-200"
    assert oracle.stalled_shell_tree(RUNTIME) == []


@pytest.mark.parametrize("failure", ["missing", "raising"])
def test_cleanup_authority_requires_trusted_runtime_shape(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {RUNTIME: "start-runtime", 200: "start-200"}.get,
    )

    def raising_probe() -> bool:
        raise RuntimeError("runtime shape unavailable")

    oracle = LivenessOracle(
        "/missing-proc",
        now=lambda: 1_000.0,
        darwin_backend=backend,
        wall_now=lambda: 1_000.0,
        steady_now_fn=lambda: 1_000.0,
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=None if failure == "missing" else raising_probe,
    )
    tool = _shell_tool()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_WORKING
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None


def test_darwin_crew_wrapper_keeps_the_runtime_as_harness_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {RUNTIME: "start-runtime", 200: "start-shell"}.get,
    )
    oracle = _darwin_oracle(backend, sandbox_wrapped_by_crew=True)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child_start_id == "start-shell"
    assert oracle.stalled_shell_tree(RUNTIME) == [(200, "start-shell")]


def test_crew_launcher_uses_its_sole_harness_as_the_shell_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    _date_fake_proc_at_100_hz(monkeypatch)
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(RUNTIME, children=[110], cmdline="kirocrew sandbox launcher")
    fake.add_pid(110, children=[200], cmdline="kiro-cli acp")
    fake.add_pid(200, cmdline="/bin/sh -c python slow_query.py")
    starts = {
        RUNTIME: "start-launcher",
        110: "start-harness",
        200: "start-shell",
    }
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = LivenessOracle(
        str(fake.root),
        now=lambda: 1_000.0,
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=lambda: True,
    ).fresh()

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child_start_id == "start-shell"
    assert oracle.stalled_shell_tree(RUNTIME) == [(200, "start-shell")]


@pytest.mark.parametrize("failure", ["multiple", "unidentified"])
def test_crew_launcher_requires_one_identified_harness(
    monkeypatch: pytest.MonkeyPatch, tmp_path, failure: str
) -> None:
    _date_fake_proc_at_100_hz(monkeypatch)
    fake = FakeProc(tmp_path / "proc")
    launcher_children = [110, 120] if failure == "multiple" else [110]
    fake.add_pid(RUNTIME, children=launcher_children, cmdline="kirocrew sandbox launcher")
    fake.add_pid(110, children=[200], cmdline="kiro-cli acp")
    if failure == "multiple":
        fake.add_pid(120, cmdline="unrelated child")
    fake.add_pid(200, cmdline="/bin/sh -c python slow_query.py")
    starts = {
        RUNTIME: "start-launcher",
        200: "start-shell",
        120: "start-other",
    }
    if failure != "unidentified":
        starts[110] = "start-harness"
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = LivenessOracle(
        str(fake.root),
        now=lambda: 1_000.0,
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=lambda: True,
    )
    tool = _shell_tool()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_WORKING
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_cleanup_rechecks_the_harness_to_root_edge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(150, RUNTIME, "mcp-server --stdio")
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    starts = {
        RUNTIME: "start-runtime",
        150: "start-mcp",
        200: "start-shell",
    }
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child_start_id == "start-shell"

    backend.parents[200] = 150
    assert 200 in (backend.descendants(RUNTIME) or [])
    assert oracle.stalled_shell_tree(RUNTIME) == []


@pytest.mark.parametrize("view", ["darwin", "procfs"])
def test_duplicate_sibling_matches_stay_working_without_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path, view: str
) -> None:
    """Either sibling can be another session's, so neither may be killed, but the
    liveness verdict stays the first match's WORKING, as it was before cleanup."""
    command = "/bin/sh -c python slow_query.py"
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {RUNTIME: "start-runtime", 200: "start-200", 300: "start-300"}.get,
    )
    if view == "darwin":
        backend = _DarwinTree()
        backend.add(200, RUNTIME, command)
        backend.add(300, RUNTIME, command)
        oracle = _darwin_oracle(backend)
        first_match = backend.descendants(RUNTIME)[0]
    else:
        _date_fake_proc_at_100_hz(monkeypatch)
        fake = FakeProc(tmp_path / "proc")
        fake.add_pid(RUNTIME, children=[200, 300], cmdline="kiro-cli acp")
        fake.add_pid(200, cmdline=command)
        fake.add_pid(300, cmdline=command)
        oracle = LivenessOracle(
            str(fake.root),
            now=lambda: 1_000.0,
            cleanup_tenancy=lambda: 1,
            sandbox_wrapped_by_crew=lambda: False,
        )
        first_match = liveness.iter_descendants(str(fake.root), RUNTIME)[1]
    tool = _shell_tool()

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == first_match
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_ambiguous_match_never_later_grants_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.add(300, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {
            RUNTIME: "start-runtime",
            200: "1000.000000",
            300: "1000.000001",
        }.get,
    )
    oracle = _darwin_oracle(backend)
    tool = _shell_tool()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_WORKING
    backend.parents.pop(200)
    backend.rows.pop(200)
    oracle = oracle.fresh()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 300
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_parallel_shell_calls_disqualify_both_cleanup_candidates() -> None:
    tracker = InFlightToolTracker(max_calls=4)
    first = _shell_tool("python first.py")
    second = _shell_tool("python second.py")

    tracker.dispatch("call-1", first)
    tracker.dispatch("call-2", second)

    assert first.shell_cleanup_disqualified is True
    assert second.shell_cleanup_disqualified is True


@pytest.mark.parametrize("shell_first", [True, False])
def test_a_state_without_is_shell_is_judged_and_withholds_overlapping_cleanup(
    shell_first: bool,
) -> None:
    """A duck-typed call state that cannot say whether it runs a shell is still the
    judged call, and an overlapping shell call loses cleanup authority for it."""
    tracker = InFlightToolTracker(max_calls=4)
    shell = _shell_tool()
    opaque = SimpleNamespace(tool_name="@kirocrew-core/spawn_sub_agents", title="call")
    calls = [("call-1", shell), ("", opaque)] if shell_first else [("", opaque), ("call-1", shell)]

    for call_id, state in calls:
        tracker.dispatch(call_id, state)

    assert tracker.current is calls[-1][1]
    assert shell.shell_cleanup_disqualified is True
    assert vars(opaque) == {"tool_name": "@kirocrew-core/spawn_sub_agents", "title": "call"}


def test_a_non_shell_state_still_leaves_shell_cleanup_authority_alone() -> None:
    tracker = InFlightToolTracker(max_calls=4)
    shell = _shell_tool()
    mcp = ToolCallState(title="ReadInternalWebsites", command="{}", is_shell=False)

    tracker.dispatch("call-1", shell)
    tracker.dispatch("call-2", mcp)

    assert shell.shell_cleanup_disqualified is False
    assert mcp.shell_cleanup_disqualified is False


def test_shared_runtime_match_never_grants_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    oracle = _darwin_oracle(backend, cleanup_tenancy=lambda: 2)
    tool = _shell_tool()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 200
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_cleanup_rechecks_tenancy_after_tracking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    tenants = [1]
    oracle = _darwin_oracle(backend, cleanup_tenancy=lambda: tenants[0])

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child_start_id == "start-200"

    tenants[0] = 2
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_tenancy_is_rechecked_immediately_before_identity_capture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    tenancies = iter((1, 2))
    oracle = _darwin_oracle(backend, cleanup_tenancy=lambda: next(tenancies))

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 200
    assert oracle._tracked_child_start_id is None


def test_program_only_match_tracks_liveness_without_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python unrelated.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 200
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


@pytest.mark.parametrize("view", ["darwin", "procfs"])
def test_fragment_only_match_preserves_liveness_without_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path, view: str
) -> None:
    cached = '{\n  "command": "printf \'python server.py\'"\n}'
    unrelated = "python server.py --serve-mcp-runtime"
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    if view == "darwin":
        backend = _DarwinTree()
        backend.add(200, RUNTIME, unrelated)
        oracle = _darwin_oracle(backend)
    else:
        _date_fake_proc_at_100_hz(monkeypatch)
        fake = FakeProc(tmp_path / "proc")
        fake.add_pid(RUNTIME, children=[200], cmdline="kiro-cli acp")
        fake.add_pid(200, cmdline=unrelated)
        oracle = LivenessOracle(
            str(fake.root),
            now=lambda: 1_000.0,
            cleanup_tenancy=lambda: 1,
            sandbox_wrapped_by_crew=lambda: False,
        )
    tool = _shell_tool(cached)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == 200
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_predispatch_exact_match_preserves_liveness_without_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.rows[200] = ProcessRow(
        pid=200,
        started=995.0,
        cmdline="/bin/sh -c python slow_query.py",
    )
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    oracle = _darwin_oracle(backend)
    tool = _shell_tool()

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == 200
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


@pytest.mark.parametrize(("start_tick", "authorized"), [(100_000, False), (100_001, True)])
def test_a_procfs_start_in_the_dispatch_tick_is_liveness_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path, start_tick: int, authorized: bool
) -> None:
    """At 100 Hz a shell started at 1000.004, before a 1000.005 dispatch, and one started
    at 1000.009, after it, both read as tick 100000. That tick cannot say which side of
    the dispatch it is, so it keeps liveness and grants no signal authority; the next
    tick lies wholly after the dispatch."""
    _date_fake_proc_at_100_hz(monkeypatch)
    fake = FakeProc(tmp_path / "proc")
    fake.add_pid(RUNTIME, children=[200], cmdline="kiro-cli acp")
    fake.add_pid(200, cmdline="/bin/sh -c python slow_query.py", starttime=start_tick)
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    oracle = LivenessOracle(
        str(fake.root),
        now=lambda: 1_000.0,
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=lambda: False,
    )
    tool = _shell_tool()
    tool.dispatch_boot_ts = 1_000.005

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == 200
    assert tool.shell_cleanup_disqualified is not authorized
    assert oracle.stalled_shell_tree(RUNTIME) == ([(200, "start-200")] if authorized else [])


def test_exact_match_without_dispatch_stamp_has_no_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    oracle = _darwin_oracle(backend)
    tool = _shell_tool()
    tool.dispatch_boot_ts = None

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert oracle._tracked_child == 200
    assert tool.shell_cleanup_disqualified is True
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


@pytest.mark.parametrize("view", ["darwin", "procfs"])
def test_complete_decoded_command_keeps_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch, tmp_path, view: str
) -> None:
    cached = '{\n  "command": "printf \'python server.py\'"\n}'
    owned = "/bin/sh -c printf 'python server.py'"
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    if view == "darwin":
        backend = _DarwinTree()
        backend.add(200, RUNTIME, owned)
        oracle = _darwin_oracle(backend)
    else:
        _date_fake_proc_at_100_hz(monkeypatch)
        fake = FakeProc(tmp_path / "proc")
        fake.add_pid(RUNTIME, children=[200], cmdline="kiro-cli acp")
        fake.add_pid(200, cmdline=owned)
        oracle = LivenessOracle(
            str(fake.root),
            now=lambda: 1_000.0,
            cleanup_tenancy=lambda: 1,
            sandbox_wrapped_by_crew=lambda: False,
        )
    tool = _shell_tool(cached)

    verdict, evidence = oracle.check_tool(RUNTIME, tool)

    assert verdict == liveness.VERDICT_WORKING, evidence
    assert tool.shell_cleanup_disqualified is False
    assert oracle._tracked_child_start_id == "start-200"
    assert oracle.stalled_shell_tree(RUNTIME) == [(200, "start-200")]


@pytest.mark.parametrize("failure", ["recycled", "unreadable", "runtime"])
def test_oracle_refuses_an_unproven_shell_tree(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    starts = {RUNTIME: "runtime", 200: "start-200"}
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = _darwin_oracle(backend)
    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING

    if failure == "recycled":
        starts[200] = "replacement"
    elif failure == "unreadable":
        backend.enumerable = False
    else:
        oracle._tracked_child = RUNTIME
        oracle._tracked_child_start_id = "runtime"

    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_match_does_not_capture_a_recycled_pid_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")

    def recycled_start(pid: int) -> str | None:
        if pid == RUNTIME:
            return "start-runtime"
        if pid == 200:
            backend.rows[200] = ProcessRow(
                pid=200,
                started=1_001.0,
                cmdline="unrelated replacement",
            )
            return "1001.000000"
        return None

    monkeypatch.setattr(liveness, "get_process_start_id", recycled_start)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 200
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_oracle_refuses_a_tree_with_an_unidentifiable_descendant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.add(201, 200, "helper --download-results")
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {RUNTIME: "start-runtime", 200: "1000.000000"}.get,
    )
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle.stalled_shell_tree(RUNTIME) == []


# These tests reach the real ``os``, so their fake PIDs are ones no OS can allocate.
_RUNTIME_PID, _SHELL_PID, _CHILD_PID, _LEAF_PID = unallocatable_pids(4)


class _Pidfds:
    """Linux's pidfd calls on any host, over real devnull descriptors.

    ``start_ids`` is the process table: an open pins the start id its PID holds then.
    ``events`` logs each open, signal and close in order; the code under test closes
    every descriptor itself, through the real ``os.close``. ``wrappers=False`` is an
    interpreter with neither ``os.pidfd_open`` nor ``signal.pidfd_send_signal``, as
    python-build-standalone CPython 3.12 is: the same calls then arrive as the raw
    syscalls ``_process_group_supervisor`` issues, each logged as ``("syscall", number)``
    first, and each raises ``raw_error`` instead when that is set.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, wrappers: bool = True) -> None:
        self.start_ids: dict[int, str] = {}
        self.pinned: dict[int, tuple[int, str]] = {}
        self.events: list[tuple[object, ...]] = []
        self.unpinned_reads: list[int] = []
        self.on_signal: Callable[[int, int], None] = lambda _pid, _sig: None
        self.raw_error: OSError | None = None
        self._wrappers = wrappers
        self._monkeypatch = monkeypatch
        self._real_close = os.close
        monkeypatch.setattr(platform_compat, "IS_WINDOWS", False)
        monkeypatch.setattr(platform_compat, "IS_POSIX", True)
        monkeypatch.setattr(platform_compat, "IS_LINUX", True)
        monkeypatch.setattr(platform_compat, "get_process_start_id", self.start_ids.get)
        monkeypatch.setattr(platform_compat, "kill_pid", self._numeric_signal)
        monkeypatch.setattr(_process_group_supervisor, "_syscall", self._raw_syscall)
        if wrappers:
            monkeypatch.setattr(os, "pidfd_open", self._open, raising=False)
            monkeypatch.setattr(signal, "pidfd_send_signal", self._send, raising=False)
        else:
            monkeypatch.delattr(os, "pidfd_open", raising=False)
            monkeypatch.delattr(signal, "pidfd_send_signal", raising=False)
        monkeypatch.setattr(os, "close", self._close)

    @property
    def signals(self) -> list[tuple[object, ...]]:
        return [event[1:] for event in self.events if event[0] == "signal"]

    def read_identities_only_while_pinned(self) -> None:
        """Log each identity read under an open pin; record any other read as gone.

        Until a SIGTERM lands, only the re-identification under an open pin reads an
        identity. A read with no pin open is the grace wait or the SIGKILL pass, and
        answering it as gone ends such a wait at once instead of sleeping it out.
        """

        def read(pid: int) -> str | None:
            if not self.pinned:
                self.unpinned_reads.append(pid)
                return None
            self.events.append(("verify", pid))
            return self.start_ids.get(pid)

        self._monkeypatch.setattr(platform_compat, "get_process_start_id", read)

    def _open(self, pid: int) -> int:
        if pid not in self.start_ids:
            raise ProcessLookupError(pid)
        fd = os.open(os.devnull, os.O_RDONLY)
        self.pinned[fd] = (pid, self.start_ids[pid])
        self.events.append(("open", pid))
        return fd

    def _send(self, fd: int, sig: int) -> None:
        pid, start_id = self.pinned[fd]
        self.events.append(("signal", pid, start_id, sig))
        self.on_signal(pid, sig)

    def _close(self, fd: int) -> None:
        pinned = self.pinned.pop(fd, None)
        if pinned is not None:
            self.events.append(("close", pinned[0]))
        self._real_close(fd)

    def _raw_syscall(self, number: int, *args: object) -> int:
        if self._wrappers:
            raise AssertionError(f"raw syscall {number} although both pidfd wrappers exist")
        self.events.append(("syscall", number))
        if self.raw_error is not None:
            raise self.raw_error
        if number == _process_group_supervisor._SYS_PIDFD_OPEN:
            pid, flags = args
            assert isinstance(pid, int) and flags == 0
            return self._open(pid)
        if number == _process_group_supervisor._SYS_PIDFD_SEND_SIGNAL:
            fd, sig, info, flags = args
            assert isinstance(fd, int) and isinstance(sig, int) and info is None and flags == 0
            self._send(fd, sig)
            return 0
        raise AssertionError(f"unexpected raw syscall {number}")

    @staticmethod
    def _numeric_signal(pid: int, sig: int = platform_compat.SIGTERM) -> bool:
        raise AssertionError(f"numeric signal {sig} sent to {pid}")


def test_terminate_pids_pinned_uses_term_then_kill_and_rechecks_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids.update(
        {_SHELL_PID: "start-shell", _CHILD_PID: "start-child", _LEAF_PID: "start-leaf"}
    )

    def exits(pid: int, sig: int) -> None:
        if pid == _LEAF_PID or sig == platform_compat.SIGKILL:
            del host.start_ids[pid]

    host.on_signal = exits

    terminated = platform_compat.terminate_pids_pinned(
        [(_LEAF_PID, "start-leaf"), (_CHILD_PID, "start-child"), (_SHELL_PID, "start-shell")],
        protected_pids=(_RUNTIME_PID,),
        grace_secs=0.0,
    )

    assert terminated == 3
    assert host.signals == [
        (_LEAF_PID, "start-leaf", platform_compat.SIGTERM),
        (_CHILD_PID, "start-child", platform_compat.SIGTERM),
        (_SHELL_PID, "start-shell", platform_compat.SIGTERM),
        (_CHILD_PID, "start-child", platform_compat.SIGKILL),
        (_SHELL_PID, "start-shell", platform_compat.SIGKILL),
    ]
    assert host.pinned == {}


def test_terminate_pids_pinned_never_retargets_after_identity_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids[_SHELL_PID] = "start-shell"

    def read_start(pid: int) -> str | None:
        start = host.start_ids.get(pid)
        if start == "start-shell":
            host.start_ids[pid] = "replacement"
        return start

    monkeypatch.setattr(platform_compat, "get_process_start_id", read_start)

    assert platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=0.0) == 1
    assert host.events == [
        ("open", _SHELL_PID),
        ("signal", _SHELL_PID, "start-shell", platform_compat.SIGTERM),
        ("close", _SHELL_PID),
    ]


def test_terminate_pids_pinned_uses_the_raw_pidfd_syscalls_without_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default python-build-standalone CPython 3.12 has neither pidfd wrapper; the
    raw syscalls keep the open, re-identify, signal-through-the-pin order on Linux."""
    host = _Pidfds(monkeypatch, wrappers=False)
    host.start_ids[_SHELL_PID] = "start-shell"

    def read_start(pid: int) -> str | None:
        host.events.append(("verify", pid))
        return host.start_ids.get(pid)

    def dies_on_kill(pid: int, sig: int) -> None:
        if sig == platform_compat.SIGKILL:
            del host.start_ids[pid]

    monkeypatch.setattr(platform_compat, "get_process_start_id", read_start)
    host.on_signal = dies_on_kill

    assert platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=0.0) == 1
    pinned_send = [
        ("syscall", _process_group_supervisor._SYS_PIDFD_OPEN),
        ("open", _SHELL_PID),
        ("verify", _SHELL_PID),
        ("syscall", _process_group_supervisor._SYS_PIDFD_SEND_SIGNAL),
    ]
    assert host.events == [
        *pinned_send,
        ("signal", _SHELL_PID, "start-shell", platform_compat.SIGTERM),
        ("close", _SHELL_PID),
        ("verify", _SHELL_PID),
        *pinned_send,
        ("signal", _SHELL_PID, "start-shell", platform_compat.SIGKILL),
        ("close", _SHELL_PID),
    ]
    assert host.pinned == {}


def test_terminate_pids_pinned_fails_closed_when_the_kernel_refuses_pidfd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch, wrappers=False)
    host.start_ids[_SHELL_PID] = "start-shell"
    host.raw_error = OSError(errno.ENOSYS, os.strerror(errno.ENOSYS))
    host.read_identities_only_while_pinned()

    assert (
        platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=3600.0) == 0
    )
    assert host.events == [("syscall", _process_group_supervisor._SYS_PIDFD_OPEN)]
    assert host.unpinned_reads == []


@pytest.mark.parametrize("wrappers", [True, False], ids=["wrappers", "no-wrappers"])
def test_terminate_pids_pinned_signals_nothing_off_linux(
    monkeypatch: pytest.MonkeyPatch, wrappers: bool
) -> None:
    """A non-Linux POSIX kernel has no pidfd, and a raw Linux syscall number means
    something else there, so nothing is opened, sent or waited for."""
    host = _Pidfds(monkeypatch, wrappers=wrappers)
    monkeypatch.setattr(platform_compat, "IS_LINUX", False)
    host.start_ids[_SHELL_PID] = "start-shell"
    host.read_identities_only_while_pinned()

    assert (
        platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=3600.0) == 0
    )
    assert host.events == []
    assert host.unpinned_reads == []


def test_terminate_pids_pinned_skips_the_grace_when_no_term_was_delivered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids.update({_CHILD_PID: "start-child", _SHELL_PID: "start-shell"})
    host.read_identities_only_while_pinned()

    def refuses(_pid: int, _sig: int) -> None:
        raise PermissionError

    host.on_signal = refuses

    assert (
        platform_compat.terminate_pids_pinned(
            [(_CHILD_PID, "start-child"), (_SHELL_PID, "start-shell")], grace_secs=3600.0
        )
        == 0
    )
    assert host.events == [
        ("open", _CHILD_PID),
        ("verify", _CHILD_PID),
        ("signal", _CHILD_PID, "start-child", platform_compat.SIGTERM),
        ("close", _CHILD_PID),
        ("open", _SHELL_PID),
        ("verify", _SHELL_PID),
        ("signal", _SHELL_PID, "start-shell", platform_compat.SIGTERM),
        ("close", _SHELL_PID),
    ]
    assert host.unpinned_reads == []
    assert host.pinned == {}


@pytest.mark.parametrize("failure", ["identity", "signal"])
def test_terminate_pids_pinned_closes_pidfd_on_refusal(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids[_SHELL_PID] = "replacement" if failure == "identity" else "start-shell"

    def read_start(pid: int) -> str | None:
        host.events.append(("verify", pid))
        return host.start_ids.get(pid)

    def exited(_pid: int, _sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(platform_compat, "get_process_start_id", read_start)
    host.on_signal = exited

    assert platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=0.0) == 0
    expected: list[tuple[object, ...]] = [
        ("open", _SHELL_PID),
        ("verify", _SHELL_PID),
        ("close", _SHELL_PID),
    ]
    if failure == "signal":
        expected = [
            ("open", _SHELL_PID),
            ("verify", _SHELL_PID),
            ("signal", _SHELL_PID, "start-shell", platform_compat.SIGTERM),
            ("close", _SHELL_PID),
        ]
    assert host.events == expected
    assert host.pinned == {}


def test_terminate_pids_pinned_never_signals_a_recycled_or_protected_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids.update({_RUNTIME_PID: "start-runtime", _SHELL_PID: "replacement"})

    assert (
        platform_compat.terminate_pids_pinned(
            [(_RUNTIME_PID, "start-runtime"), (_SHELL_PID, "start-shell")],
            protected_pids=(_RUNTIME_PID,),
            grace_secs=0.0,
        )
        == 0
    )
    assert host.events == [("open", _SHELL_PID), ("close", _SHELL_PID)]


def test_terminate_pids_pinned_does_not_count_a_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = _Pidfds(monkeypatch)
    host.start_ids[_SHELL_PID] = "start-shell"

    def refuses_kill(_pid: int, sig: int) -> None:
        if sig == platform_compat.SIGKILL:
            raise PermissionError

    host.on_signal = refuses_kill

    assert platform_compat.terminate_pids_pinned([(_SHELL_PID, "start-shell")], grace_secs=0.0) == 0
    assert host.signals == [
        (_SHELL_PID, "start-shell", platform_compat.SIGTERM),
        (_SHELL_PID, "start-shell", platform_compat.SIGKILL),
    ]
    assert host.pinned == {}


def _tenancy_runtime() -> SimpleNamespace:
    """Exactly the runtime fields ``session_tenancy`` reads, for one sole session."""
    return SimpleNamespace(
        _session_queues={"session-a": object()},
        _session_inits_in_flight=0,
        _start_collectors={},
        _subagent_sessions=set(),
        _subagent_roster_overflow=0,
    )


def test_runtime_tenancy_requires_the_current_session_and_no_pending_start() -> None:
    runtime = _tenancy_runtime()

    assert runtime_liveness.session_tenancy(runtime, "session-a") == 1

    runtime._session_inits_in_flight = 1
    assert runtime_liveness.session_tenancy(runtime, "session-a") is None

    runtime._session_inits_in_flight = 0
    runtime._start_collectors = {1: object()}
    assert runtime_liveness.session_tenancy(runtime, "session-a") is None

    runtime._start_collectors = {}
    runtime._session_queues = {"session-b": object()}
    assert runtime_liveness.session_tenancy(runtime, "session-a") is None

    runtime._session_queues = {}
    assert runtime_liveness.session_tenancy(runtime, "session-a") is None


@pytest.mark.parametrize(
    "roster",
    [{"_subagent_sessions": {"native-child"}}, {"_subagent_roster_overflow": 1}],
    ids=["announced", "overflow"],
)
def test_native_subagent_roster_revokes_cleanup_tenancy_but_not_liveness(
    monkeypatch: pytest.MonkeyPatch, roster: dict[str, object]
) -> None:
    """A native backend subagent runs in the harness's own tree with no queue here, so
    its exact-command child reads like the parent call's. Liveness keeps tracking that
    child; the authority to signal it is gone, also for an oracle that already held it."""
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness, "get_process_start_id", {RUNTIME: "start-runtime", 200: "start-200"}.get
    )
    runtime = _tenancy_runtime()

    def judged() -> tuple[tuple[str, str], LivenessOracle]:
        oracle = _darwin_oracle(
            backend,
            cleanup_tenancy=lambda: runtime_liveness.session_tenancy(runtime, "session-a"),
        )
        return oracle.check_tool(RUNTIME, _shell_tool()), oracle

    alone_verdict, alone = judged()
    assert alone.stalled_shell_tree(RUNTIME) == [(200, "start-200")]

    vars(runtime).update(roster)

    assert runtime_liveness.session_tenancy(runtime, "session-a") is None
    shared_verdict, shared = judged()
    assert shared_verdict == alone_verdict
    assert shared_verdict[0] == liveness.VERDICT_WORKING
    assert shared._tracked_child == alone._tracked_child == 200
    assert shared._tracked_child_start_id is None
    assert shared.stalled_shell_tree(RUNTIME) == []
    assert alone.stalled_shell_tree(RUNTIME) == []


def _handle(tool: ToolCallState) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.pid = RUNTIME
    runtime.sandbox_wrapped_by_crew = False
    runtime._last_activity = time.monotonic()
    runtime.is_alive.return_value = True
    runtime.send_notification = AsyncMock()
    runtime.session_tenancy.return_value = 1
    handle = AcpSessionHandle("session-a", asyncio.Queue(), runtime)
    handle._turn_done.clear()
    handle._inflight_tool = tool
    return handle


def test_handle_declares_cleanup_tenancy_without_model_wait_tenancy() -> None:
    handle = _handle(_shell_tool())
    handle._runtime.session_tenancy.return_value = 2

    assert handle._oracle._tenancy is None
    assert handle._oracle._cleanup_tenancy() == 2
    assert handle._oracle._crew_sandbox_wrap_status() is False
    handle._runtime.session_tenancy.assert_called_once_with("session-a")


@pytest.mark.asyncio
async def test_stalled_shell_is_cancelled_then_its_tree_is_terminated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    handle = _handle(_shell_tool())

    async def cancel() -> None:
        order.append("cancel")

    handle.cancel = cancel  # type: ignore[method-assign]
    monkeypatch.setattr(
        handle._oracle,
        "stalled_shell_tree",
        lambda pid: order.append("snapshot") or [(200, "start-200")],
    )

    def terminate(targets, *, protected_pids, grace_secs=3.0):
        assert targets == [(200, "start-200")]
        assert protected_pids == (RUNTIME,)
        order.append("terminate")
        return 1

    monkeypatch.setattr(platform_compat, "terminate_pids_pinned", terminate)

    events = [
        event
        async for event in handle._end_stalled_tool(
            liveness.VERDICT_UNKNOWN, "shell child flat", 700.0
        )
    ]

    assert order == ["cancel", "snapshot", "terminate"]
    assert events[-1].stop_reason == STOP_REASON_TOOL_STALL
    assert "processes_terminated=1" in events[-1].text


@pytest.mark.asyncio
async def test_mcp_stall_never_terminates_a_process(monkeypatch: pytest.MonkeyPatch) -> None:
    tool = ToolCallState(
        title="ReadInternalWebsites",
        command='{"url": "https://example.test"}',
        is_shell=False,
    )
    handle = _handle(tool)
    handle.cancel = AsyncMock()  # type: ignore[method-assign]
    monkeypatch.setattr(
        platform_compat,
        "terminate_pids_pinned",
        lambda *args, **kwargs: pytest.fail("an MCP stall must not signal a process"),
        raising=False,
    )

    events = [
        event
        async for event in handle._end_stalled_tool(
            liveness.VERDICT_UNKNOWN, "mcp subtree flat", 700.0
        )
    ]

    assert "processes_terminated=0" in events[-1].text


def test_recovery_prompt_says_the_old_process_tree_was_terminated() -> None:
    prompt = build_tool_stall_recovery_prompt(
        "bash",
        700,
        command="python slow_query.py",
        processes_terminated=3,
    )

    assert "3 processes" in prompt
    assert "no longer running" in prompt
    assert "partial results" in prompt


@pytest.mark.skipif(
    sys.platform != "linux" or os.sysconf("SC_CLK_TCK") <= 0,
    reason="requires Linux and a /proc tick rate",
)
def test_real_shell_and_child_are_terminated_without_killing_the_runtime(tmp_path) -> None:
    if not _process_group_supervisor.can_reap():
        pytest.skip("requires a kernel that pins processes with pidfd_open")
    command = "/bin/sleep 30 & wait"
    hz = os.sysconf("SC_CLK_TCK")
    # /proc floors a start to whole ticks, so floor the stamp too: it stays <= the child's.
    dispatch_tick = time.clock_gettime_ns(time.CLOCK_BOOTTIME) * hz // 1_000_000_000
    tool = ToolCallState(
        title="bash",
        command=command,
        dispatch_ts=time.monotonic(),
        dispatch_boot_ts=dispatch_tick / hz,
        dispatch_steady_ts=liveness.steady_now(),
        is_shell=True,
    )
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        start_new_session=True,
    )
    oracle = LivenessOracle(
        cleanup_tenancy=lambda: 1,
        sandbox_wrapped_by_crew=lambda: False,
    )
    try:
        wait_until(
            lambda: oracle.check_tool(os.getpid(), tool)[0] == liveness.VERDICT_WORKING,
            timeout=2.0,
            interval=0.02,
            describe=lambda: "the oracle matching the real shell child",
        )
        last_read: list[tuple[int, str]] = []

        def whole_tree() -> list[tuple[int, str]]:
            # The shell forks its child after it starts; wait for one snapshot of both.
            last_read[:] = oracle.stalled_shell_tree(os.getpid())
            complete = len(last_read) == 2 and last_read[-1][0] == proc.pid
            return list(last_read) if complete else []

        targets = wait_until(
            whole_tree,
            interval=0.02,
            describe=lambda: f"shell {proc.pid} and its child as one tree, last read {last_read}",
        )
        assert os.getpid() not in {pid for pid, _ in targets}

        assert (
            platform_compat.terminate_pids_pinned(
                targets,
                protected_pids=(os.getpid(),),
                grace_secs=0.2,
            )
            >= 1
        )
        proc.wait(timeout=2.0)
        assert proc.returncode is not None
    finally:
        platform_compat.kill_popen_tree(proc)
        proc.wait(timeout=2.0)
