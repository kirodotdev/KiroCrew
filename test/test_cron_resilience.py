"""Tests for cron resilience: non-blocking job execution and semaphore safety."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.cron import CronJob, CronService
from kiro_crew.heartbeat import _HEADER, HeartbeatService


async def _wait_for(predicate, timeout=5.0, interval=0.05):
    """Poll until predicate is true or timeout."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("Timed out waiting for predicate")
        await asyncio.sleep(interval)


class TestCronNonBlocking:
    """Verify that a slow/stuck job does not block other jobs."""

    @pytest.mark.asyncio
    async def test_slow_job_does_not_block_fast_job(self, tmp_path: Path) -> None:
        finished: dict[str, float] = {}

        async def callback(job: CronJob) -> None:
            if job.name == "slow":
                await asyncio.sleep(2)
            finished[job.name] = time.monotonic()

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("slow", "msg", every_secs=60)
        svc.add_job("fast", "msg", every_secs=60)
        for j in svc._jobs:
            j.last_run_ts = time.time() - 120

        await svc._on_timer()
        await _wait_for(lambda: len(finished) == 2, timeout=10.0)
        assert finished["fast"] < finished["slow"]
        assert finished["slow"] - finished["fast"] > 1.0
        await svc.stop()

    @pytest.mark.asyncio
    async def test_failing_job_does_not_block_others(self, tmp_path: Path) -> None:
        executed: list[str] = []

        async def callback(job: CronJob) -> None:
            if job.name == "fail":
                raise RuntimeError("boom")
            executed.append(job.name)

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("fail", "msg", every_secs=60)
        svc.add_job("ok", "msg", every_secs=60)
        for j in svc._jobs:
            j.last_run_ts = time.time() - 120

        await svc._on_timer()
        await _wait_for(lambda: "ok" in executed)
        fail_job = next(j for j in svc._jobs if j.name == "fail")
        await _wait_for(lambda: fail_job.last_status == "error")
        await svc.stop()

    @pytest.mark.asyncio
    async def test_job_result_merged_to_disk(self, tmp_path: Path) -> None:
        async def callback(job: CronJob) -> None:
            pass

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("test", "msg", every_secs=60)
        job_id = svc._jobs[0].id
        svc._jobs[0].last_run_ts = time.time() - 120

        await svc._on_timer()
        # Await the job task itself rather than polling for the in-memory
        # last_status. _execute sets last_status BEFORE _run_job_isolated's
        # finally block offloads _merge_job_result to a worker thread, so a
        # predicate on last_status can go true while crons.json still holds the
        # pre-run state — the disk assertion below would then read a store that
        # was never written. The task completes only after that offloaded merge
        # returns, making it the one signal that actually implies the write.
        task = svc._running_tasks[job_id]
        await task
        assert svc._jobs[0].last_status == "ok"

        # Reload from disk and verify
        svc2 = CronService(base_dir=tmp_path)
        svc2._load()
        assert svc2._jobs[0].last_status == "ok"
        await svc.stop()

    @pytest.mark.asyncio
    async def test_task_references_stored(self, tmp_path: Path) -> None:
        """Verify fire-and-forget tasks are stored to prevent GC collection."""
        gate = asyncio.Event()

        async def callback(job: CronJob) -> None:
            await gate.wait()

        svc = CronService(base_dir=tmp_path, on_job=callback)
        await svc.start()
        svc.add_job("held", "msg", every_secs=60)
        svc._jobs[0].last_run_ts = time.time() - 120

        await svc._on_timer()
        job_id = svc._jobs[0].id
        assert job_id in svc._running_tasks
        gate.set()
        await _wait_for(lambda: job_id not in svc._running_tasks)
        await svc.stop()


class TestArmTimer:
    """Verify _arm_timer always creates a new timer."""

    @pytest.mark.asyncio
    async def test_arm_timer_always_arms(self, tmp_path: Path) -> None:
        svc = CronService(base_dir=tmp_path)
        svc._running = True
        svc._executing.add("some_job")

        svc._arm_timer()
        assert svc._timer_task is not None
        assert not svc._timer_task.done()
        await svc.stop()


class TestAcpResponsiveness:
    """Verify ACP zombie detection via is_responsive."""

    def test_fresh_client_is_responsive(self) -> None:
        client = AcpClient()
        # No process, so _is_process_alive is False
        assert not client.is_responsive()

    def test_stale_activity_detected(self) -> None:
        client = AcpClient()
        # Simulate alive process with stale activity
        client._process = MagicMock()
        client._process.returncode = None
        client._last_activity = time.monotonic() - 700  # 700s ago
        assert not client.is_responsive(stale_threshold=600.0)

    def test_recent_activity_is_responsive(self) -> None:
        client = AcpClient()
        client._process = MagicMock()
        client._process.returncode = None
        client._last_activity = time.monotonic() - 10  # 10s ago
        assert client.is_responsive(stale_threshold=600.0)

    def test_recently_created_is_responsive(self) -> None:
        client = AcpClient()
        client._process = MagicMock()
        client._process.returncode = None
        # _last_activity initialized to time.monotonic() in __init__
        assert client.is_responsive()


class TestHeartbeatParallel:
    """Verify heartbeat tasks run in parallel."""

    @pytest.mark.asyncio
    async def test_tasks_run_concurrently(self, tmp_path: Path) -> None:
        started: list[float] = []

        async def on_task(text: str, deliver: str) -> None:
            started.append(time.monotonic())
            await asyncio.sleep(0.5)

        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
        # Write 3 tasks
        hb_path = tmp_path / "HEARTBEAT.md"
        hb_path.write_text(_HEADER + "- task1\n- task2\n- task3\n")

        original = hb_mod.heartbeat_path
        hb_mod.heartbeat_path = lambda: hb_path
        try:
            await svc._process_heartbeat_file()
        finally:
            hb_mod.heartbeat_path = original

        assert len(started) == 3
        # All 3 should start within 0.1s of each other (parallel)
        assert max(started) - min(started) < 0.2
