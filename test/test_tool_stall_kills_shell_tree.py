"""A tool-stall cancel also ends the matched shell command's process tree."""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from collections.abc import Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import platform_compat
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

    def row(self, pid: int) -> ProcessRow | None:
        return self.rows.get(pid)

    def cpu_nanos(self, pid: int) -> int | None:
        return 0 if pid in self.rows or pid == RUNTIME else None


def _darwin_oracle(
    backend: _DarwinTree,
    *,
    cleanup_tenancy: Callable[[], int | None] | None = None,
) -> LivenessOracle:
    return LivenessOracle(
        "/missing-proc",
        now=lambda: 1_000.0,
        darwin_backend=backend,
        wall_now=lambda: 1_000.0,
        steady_now_fn=lambda: 1_000.0,
        cleanup_tenancy=(cleanup_tenancy if cleanup_tenancy is not None else lambda: 1),
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


def test_oracle_returns_the_matched_shell_tree_leaf_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.add(201, 200, "python slow_query.py")
    backend.add(202, 201, "helper --download-results")
    starts = {200: "start-200", 201: "start-201", 202: "start-202"}
    monkeypatch.setattr(liveness, "get_process_start_id", starts.get)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING

    assert oracle._tracked_child_start_id == "start-200"
    assert oracle.stalled_shell_tree(RUNTIME) == [
        (202, "start-202"),
        (201, "start-201"),
        (200, "start-200"),
    ]


def test_duplicate_matching_commands_are_ambiguous_and_not_tracked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    backend.add(300, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(
        liveness,
        "get_process_start_id",
        {200: "1000.000000", 300: "1000.000000"}.get,
    )
    oracle = _darwin_oracle(backend)

    verdict, _ = oracle.check_tool(RUNTIME, _shell_tool())

    assert verdict == liveness.VERDICT_UNKNOWN
    assert oracle._tracked_child is None
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
        {200: "1000.000000", 300: "1000.000001"}.get,
    )
    oracle = _darwin_oracle(backend)
    tool = _shell_tool()

    assert oracle.check_tool(RUNTIME, tool)[0] == liveness.VERDICT_UNKNOWN
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


def test_shared_runtime_match_never_grants_cleanup_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = _DarwinTree()
    backend.add(200, RUNTIME, "/bin/sh -c python slow_query.py")
    monkeypatch.setattr(liveness, "get_process_start_id", {200: "start-200"}.get)
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
    monkeypatch.setattr(liveness, "get_process_start_id", {200: "start-200"}.get)
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
    monkeypatch.setattr(liveness, "get_process_start_id", {200: "start-200"}.get)
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
    monkeypatch.setattr(liveness, "get_process_start_id", {200: "start-200"}.get)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle._tracked_child == 200
    assert oracle._tracked_child_start_id is None
    assert oracle.stalled_shell_tree(RUNTIME) == []


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
    monkeypatch.setattr(liveness, "get_process_start_id", {200: "1000.000000"}.get)
    oracle = _darwin_oracle(backend)

    assert oracle.check_tool(RUNTIME, _shell_tool())[0] == liveness.VERDICT_WORKING
    assert oracle.stalled_shell_tree(RUNTIME) == []


def test_terminate_pids_pinned_uses_term_then_kill_and_rechecks_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {200: "start-200", 201: "start-201", 202: "start-202"}
    signals: list[tuple[int, int]] = []

    def signal(pid: int, expected: str, sig: int) -> bool:
        assert live.get(pid) == expected
        signals.append((pid, sig))
        if pid == 202 or sig == platform_compat.SIGKILL:
            live.pop(pid, None)
        return True

    monkeypatch.setattr(platform_compat, "get_process_start_id", live.get)
    monkeypatch.setattr(platform_compat, "kill_pid_pinned", signal)

    terminated = platform_compat.terminate_pids_pinned(
        [(202, "start-202"), (201, "start-201"), (200, "start-200")],
        protected_pids=(RUNTIME,),
        grace_secs=0.0,
    )

    assert terminated == 3
    assert signals == [
        (202, platform_compat.SIGTERM),
        (201, platform_compat.SIGTERM),
        (200, platform_compat.SIGTERM),
        (201, platform_compat.SIGKILL),
        (200, platform_compat.SIGKILL),
    ]


def test_terminate_pids_pinned_never_signals_a_recycled_or_protected_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {RUNTIME: "runtime", 200: "replacement"}
    signals: list[int] = []
    monkeypatch.setattr(platform_compat, "get_process_start_id", live.get)
    monkeypatch.setattr(
        platform_compat,
        "kill_pid_pinned",
        lambda pid, expected, sig: signals.append(pid) or True,
    )

    assert (
        platform_compat.terminate_pids_pinned(
            [(RUNTIME, "runtime"), (200, "start-200")],
            protected_pids=(RUNTIME,),
            grace_secs=0.0,
        )
        == 0
    )
    assert signals == []


def test_terminate_pids_pinned_does_not_count_a_survivor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = {200: "start-200"}
    signals: list[int] = []
    monkeypatch.setattr(platform_compat, "get_process_start_id", live.get)

    def signal(pid: int, expected: str, sig: int) -> bool:
        signals.append(sig)
        return sig == platform_compat.SIGTERM

    monkeypatch.setattr(platform_compat, "kill_pid_pinned", signal)

    assert platform_compat.terminate_pids_pinned([(200, "start-200")], grace_secs=0.0) == 0
    assert signals == [platform_compat.SIGTERM, platform_compat.SIGKILL]


def test_runtime_tenancy_requires_the_current_session_and_no_pending_start() -> None:
    runtime = MagicMock()
    runtime._session_queues = {"session-a": object()}
    runtime._session_inits_in_flight = 0
    runtime._start_collectors = {}

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


def _handle(tool: ToolCallState) -> AcpSessionHandle:
    runtime = MagicMock()
    runtime.pid = RUNTIME
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


@pytest.mark.skipif(os.name != "posix", reason="requires a POSIX shell and signals")
def test_real_shell_and_child_are_terminated_without_killing_the_runtime(tmp_path) -> None:
    command = "/bin/sleep 30 & wait"
    tool = ToolCallState(
        title="bash",
        command=command,
        dispatch_ts=time.monotonic(),
        dispatch_boot_ts=platform_compat.boottime_now(),
        dispatch_steady_ts=liveness.steady_now(),
        is_shell=True,
    )
    proc = subprocess.Popen(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        start_new_session=True,
    )
    oracle = LivenessOracle(cleanup_tenancy=lambda: 1)
    try:
        wait_until(
            lambda: oracle.check_tool(os.getpid(), tool)[0] == liveness.VERDICT_WORKING,
            timeout=2.0,
            interval=0.02,
            describe=lambda: "the oracle matching the real shell child",
        )

        targets = oracle.stalled_shell_tree(os.getpid())
        assert targets and targets[-1][0] == proc.pid
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
