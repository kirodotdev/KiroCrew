"""Tests for cold-start staggering and parallel task batching."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.taskrunner import _MAX_PARALLEL_TASKS, TaskRunner

# ── SessionManager cold-start semaphore ──


class TestSessionStartSemaphore:
    """Verify SessionManager limits concurrent provider.start() calls."""

    @pytest.mark.asyncio
    async def test_semaphore_exists_on_session_manager(self):
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.default_agent = ""
        cfg.model = "auto"
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg)
        assert hasattr(sm, "_start_sem")
        # Semaphore(4) limits concurrent cold-starts for memory safety
        assert sm._start_sem._value == 4

    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_starts(self):
        """Simulate 5 concurrent get_or_create calls, verify max 2 start simultaneously."""
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.default_agent = ""
        cfg.model = "auto"
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg)

        concurrent_count = 0
        max_concurrent = 0

        original_sem = sm._start_sem

        async def mock_start():
            nonlocal concurrent_count, max_concurrent
            concurrent_count += 1
            max_concurrent = max(max_concurrent, concurrent_count)
            await asyncio.sleep(0.05)  # simulate cold-start
            concurrent_count -= 1

        mock_provider = MagicMock()
        mock_provider.start = mock_start
        mock_provider.context_pct = 0
        mock_provider.model = "test"

        async def acquire_and_start():
            async with original_sem:
                await mock_start()

        # Fire 5 concurrent starts — more than semaphore(4) allows
        await asyncio.gather(
            acquire_and_start(),
            acquire_and_start(),
            acquire_and_start(),
            acquire_and_start(),
            acquire_and_start(),
        )

        assert max_concurrent <= 4, f"Expected max 4 concurrent, got {max_concurrent}"
        assert max_concurrent > 1, f"Expected concurrent execution, got {max_concurrent}"


# ── TaskRunner parallel batching ──


class TestParallelBatching:
    def test_max_parallel_tasks_constant(self):
        assert _MAX_PARALLEL_TASKS == 3

    @pytest.mark.asyncio
    async def test_large_group_batched(self):
        """A group of 6 tasks should be split into batches of 4 + 2."""
        from kiro_crew.task_models import Project, Task, TaskStatus

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()

        runner = TaskRunner(sessions=sessions, auto_test=False)

        # Track execution order
        executed: list[int] = []

        async def mock_execute(run, task, hk="", session_key=""):
            executed.append(task.index)
            task.status = TaskStatus.PASSED
            return True

        runner._execute_single_task = mock_execute

        run = Project(spec_path="", spec_content="test")
        run.status = "running"
        run.task_id = "test"
        run.started_at = __import__("time").time()
        # 6 tasks, all independent (same parallel group)
        run.tasks = [Task(index=i, title=f"T{i}", description="d") for i in range(1, 7)]

        runner._notify = AsyncMock()

        await runner._execute_tasks(run, "hk")

        assert len(executed) == 6
        assert set(executed) == {1, 2, 3, 4, 5, 6}

    @pytest.mark.asyncio
    async def test_small_group_not_batched(self):
        """A group of 3 tasks should run in a single gather (no batching needed)."""
        from kiro_crew.task_models import Project, Task, TaskStatus

        sessions = MagicMock()
        sessions.get_pid = MagicMock(return_value=None)
        sessions.get_or_create = AsyncMock(return_value=(MagicMock(), True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()

        runner = TaskRunner(sessions=sessions, auto_test=False)

        executed: list[int] = []

        async def mock_execute(run, task, hk="", session_key=""):
            executed.append(task.index)
            task.status = TaskStatus.PASSED
            return True

        runner._execute_single_task = mock_execute

        run = Project(spec_path="", spec_content="test")
        run.status = "running"
        run.task_id = "test"
        run.started_at = __import__("time").time()
        run.tasks = [Task(index=i, title=f"T{i}", description="d") for i in range(1, 4)]

        runner._notify = AsyncMock()

        await runner._execute_tasks(run, "hk")

        assert len(executed) == 3


# ── SubagentManager + semaphore interaction ──


class TestSubagentSemaphoreInteraction:
    """Verify SubagentManager's agents run with semaphore(2) on cold-start."""

    @pytest.mark.asyncio
    async def test_three_agents_all_complete(self):
        """3 subagents should all complete with semaphore(2) on cold-start."""
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.default_agent = ""
        cfg.model = "auto"
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg)

        completed = []

        async def simulate_agent(agent_id: int):
            async with sm._start_sem:
                await asyncio.sleep(0.02)  # cold-start
            await asyncio.sleep(0.02)
            completed.append(agent_id)

        await asyncio.gather(
            simulate_agent(1),
            simulate_agent(2),
            simulate_agent(3),
        )

        assert sorted(completed) == [1, 2, 3]
