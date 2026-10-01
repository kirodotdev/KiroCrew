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

_FAKE_AGENT = textwrap.dedent(
    """
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
    """
)


def _gone(pid: int) -> bool:
    """True once *pid* no longer runs (absent, or a zombie awaiting its reaper)."""
    if not platform_compat.pid_exists(pid):
        return True
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] == "Z"
        except (OSError, IndexError):
            return True
    return False


async def _wait_gone(pid: int, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _gone(pid):
            return True
        await asyncio.sleep(0.05)
    return _gone(pid)


@pytest.mark.asyncio
async def test_a_failed_init_leaves_no_child_processes(tmp_path, monkeypatch):
    script = tmp_path / "fake_agent.py"
    script.write_text(_FAKE_AGENT, encoding="utf-8")
    pid_file = tmp_path / "child.pid"

    rt = AcpRuntime(work_dir=tmp_path / "wd", agent="kirocrew", sandbox_mode="off")
    rt._KILL_TERM_TIMEOUT = 1.0
    rt._KILL_REAP_TIMEOUT = 1.0

    async def _plan() -> SpawnPlan:
        return SpawnPlan(argv=[sys.executable, str(script), str(pid_file), "--agent", "kirocrew"])

    child_pid: list[int] = []

    async def _failing_handshake(_caps):
        # Wait for the tree to exist, then fail the way a stalled kiro-cli does.
        for _ in range(200):
            if pid_file.exists():
                break
            await asyncio.sleep(0.02)
        pid = int(pid_file.read_text())
        child_pid.append(pid)
        # The shape under test: the child leads its own group, outside the root's.
        assert os.getpgid(pid) == pid != os.getpgid(rt.pid)
        raise AcpRuntimeError("initialize timed out")

    monkeypatch.setattr(rt, "_resolve_spawn_plan", _plan)
    monkeypatch.setattr(skill_projection, "prepare_native_skill_projection", lambda _wd: None)
    monkeypatch.setattr(rt, "_initialize_handshake", _failing_handshake)

    try:
        with pytest.raises(AcpRuntimeError, match="initialize timed out"):
            await rt.spawn()
        assert child_pid, "the stand-in tree never started"
        assert await _wait_gone(child_pid[0]), (
            f"the MCP stand-in (PID {child_pid[0]}) outlived the failed start"
        )
    finally:
        for pid in child_pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
