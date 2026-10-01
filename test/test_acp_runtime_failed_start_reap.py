"""A runtime whose start fails leaves none of its process tree behind.

kiro-cli launches each stdio MCP server as the leader of a process group of its
own, so the group kill a failed start ends with never reaches them. These tests
spawn a real stand-in tree -- a root that starts one child in a new process
group, the shape an MCP server has -- fail the ``initialize`` handshake, and
check that the child is gone afterwards.
"""

from __future__ import annotations

import asyncio
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp import skill_projection
from kiro_crew.acp.harness.base import SpawnPlan
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeError

pytestmark = pytest.mark.skipif(
    not platform_compat.IS_POSIX, reason="process groups are a POSIX mechanism"
)

_FAKE_AGENT = textwrap.dedent("""
    import subprocess, sys, time
    # Stand-in for a stdio MCP server: the leader of a process group of its own.
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        process_group=0,
        stdin=subprocess.DEVNULL,
    )
    with open(sys.argv[1] + ".tmp", "w") as fh:
        fh.write(str(child.pid))
    import os
    os.replace(sys.argv[1] + ".tmp", sys.argv[1])
    time.sleep(120)
    """)


def _gone(pid: int) -> bool:
    """True once *pid* has stopped running (absent, or a zombie awaiting its reaper)."""
    if not platform_compat.pid_exists(pid):
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True
    return False


async def _reap_stand_in(pid: int, start_id: str | None) -> None:
    """SIGKILL the stand-in only while *pid* is still the process the test recorded.

    A red run reaches this with the stand-in possibly alive; a green one reaches
    it after the stand-in was already reaped, when *pid* may name another
    process. The start identity read at record time tells the two apart.
    """
    if start_id is not None and platform_compat.get_process_start_id(pid) == start_id:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    await _wait_gone(pid)


async def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _gone(pid)


@pytest.mark.asyncio
async def test_a_failed_init_leaves_no_child_processes(tmp_path, monkeypatch, caplog):
    script = tmp_path / "fake_agent.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    pid_file = tmp_path / "child.pid"

    rt = AcpRuntime(work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off")
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=[sys.executable, str(script), str(pid_file), "--agent", "kirocrew"])

    child_pid: list[int] = []
    start_ids: dict[int, str | None] = {}

    async def _failing_handshake(_caps):
        # Wait for the tree to exist, then fail the way a stalled kiro-cli does.
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        pid = int(pid_file.read_text())
        child_pid.append(pid)
        start_ids[pid] = platform_compat.get_process_start_id(pid)
        # The shape under test: the child leads its own group, outside the root's.
        assert os.getpgid(pid) == pid != os.getpgid(rt.pid)
        raise AcpRuntimeError("initialize timed out")

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", lambda _wd: None)
    monkeypatch.setattr(rt, "_initialize_handshake", _failing_handshake)

    try:
        with caplog.at_level("WARNING", logger="kiro_crew.acp.runtime"):
            with pytest.raises(AcpRuntimeError, match="initialize timed out"):
                await rt.spawn()
        assert child_pid, "the stand-in tree never started"
        assert await _wait_gone(
            child_pid[0]
        ), f"the MCP stand-in (PID {child_pid[0]}) outlived the failed start"
        # A reaped tree is not reported as a leak: no survivor warning from the
        # kill's teardown or from the reap, for processes that did exit.
        survivor_lines = [
            r.getMessage()
            for r in caplog.records
            if "survived" in r.getMessage() or "retained tracking" in r.getMessage()
        ]
        assert survivor_lines == [], survivor_lines
    finally:
        # A red run must not leave the 120s sleeper behind.
        for pid in child_pid:
            await _reap_stand_in(pid, start_ids.get(pid))


@pytest.mark.asyncio
async def test_a_cancel_during_the_failed_start_scan_still_reaps_the_tree(tmp_path, monkeypatch):
    """A newer slot signal cancelling the spawn mid-cleanup must not skip the reap.

    The eager-spawn task that runs this start is cancelled by ordinary dashboard
    signals. The cancel is delivered while the failed-start cleanup is inside its
    descendant scan; the stand-in MCP server must still be gone afterwards, and
    the cancellation must still reach the caller.
    """
    script = tmp_path / "fake_agent.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    pid_file = tmp_path / "child.pid"

    rt = AcpRuntime(work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off")
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=[sys.executable, str(script), str(pid_file), "--agent", "kirocrew"])

    child_pid: list[int] = []
    start_ids: dict[int, str | None] = {}

    async def _failing_handshake(_caps):
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        pid = int(pid_file.read_text())
        child_pid.append(pid)
        start_ids[pid] = platform_compat.get_process_start_id(pid)
        raise AcpRuntimeError("initialize timed out")

    in_scan = asyncio.Event()
    real_snapshot = rt._snapshot_descendants

    async def _slow_snapshot(**kwargs):
        # Hold the scan open long enough for the cancel below to land inside it.
        in_scan.set()
        await asyncio.sleep(0.3)
        await real_snapshot(**kwargs)

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", lambda _wd: None)
    monkeypatch.setattr(rt, "_initialize_handshake", _failing_handshake)
    monkeypatch.setattr(rt, "_snapshot_descendants", _slow_snapshot)

    try:
        spawn = asyncio.ensure_future(rt.spawn())
        await asyncio.wait_for(in_scan.wait(), timeout=10)
        spawn.cancel()
        # A second signal (a slot deletion after a newer slot signal) cancels again.
        await asyncio.sleep(0.05)
        spawn.cancel()
        (outcome,) = await asyncio.gather(spawn, return_exceptions=True)
        assert child_pid, "the stand-in tree never started"
        # The leak first: it is the harm, and it is independent of what was raised.
        assert await _wait_gone(
            child_pid[0]
        ), f"the MCP stand-in (PID {child_pid[0]}) outlived a cancelled failed start"
        assert isinstance(outcome, asyncio.CancelledError), repr(outcome)
    finally:
        for pid in child_pid:
            await _reap_stand_in(pid, start_ids.get(pid))
