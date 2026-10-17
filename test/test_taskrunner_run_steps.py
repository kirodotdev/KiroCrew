"""Running a planned project's steps within the run's bounds.

The watchdog notices and cancels a stalled step on the monotonic clock, the
global timeout counts from the run's start stamp, a dead process is caught by its
heartbeat, the token budget stops a run, dependency groups run in order, and a
semaphore keeps the parallel steps at the configured cap without a batch stall.
The shared doubles live in ``taskrunner_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from taskrunner_helpers import (
    _GENEROUS_DEADLINE,
    _make_mock_sessions,
)

import kiro_crew.taskrunner as taskrunner_module
from kiro_crew.taskrunner import (
    _STALL_CANCEL_TIMEOUT,
    _STALL_TIMEOUT,
    Step,
    StepStatus,
    TaskRun,
    TaskRunner,
)


class _SteppedClock:
    """Stands in for the ``time`` module: seconds pass only when a test says so.

    ``monotonic()`` follows the real passage of time the test scripts;
    ``time()`` follows it too, plus any wall-clock step on top. Every other
    name is the real module's.
    """

    def __init__(self) -> None:
        self._wall = 1_800_000_000.0
        self._mono = 5_000.0

    def time(self) -> float:
        return self._wall

    def monotonic(self) -> float:
        return self._mono

    def advance(self, passed: float, *, step: float = 0.0) -> None:
        self._mono += passed
        self._wall += passed + step

    def __getattr__(self, name: str) -> Any:
        import time as _time

        return getattr(_time, name)


# ── 12.1e: Task Watchdog ──


class TestWatchdog:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- class-scoped autouse fixture moved verbatim; no test in its class calls monkeypatch.undo()
    def _passthrough_sandbox(self, monkeypatch):
        """TestWatchdog runs the real ``TaskRunner.run()`` which reaches
        ``git_coord.init_workspace`` → ``sandboxed_spawn_argv`` → ``wrap_argv``,
        raising on hosts without an OS-level sandbox backend (e.g. the dry-run
        build fleet). These tests exercise watchdog/timeout logic, not sandbox
        availability, so run commands unwrapped in-test (same pattern as
        TestGitCoord)."""
        import os as _os

        from kiro_crew import git_coord

        monkeypatch.setattr(
            git_coord,
            "sandboxed_spawn_argv",
            lambda argv, *a, **k: (list(argv), dict(_os.environ), None),
        )

    @pytest.mark.asyncio
    async def test_watchdog_stall_notification(self) -> None:
        """Watchdog sends notification when no progress in _STALL_TIMEOUT."""
        import time

        sessions = _make_mock_sessions()

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.current_task = 3
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_TIMEOUT - 60

        # Run watchdog for one tick then stop
        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert any("stalled" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("passed", "step", "cancelled"),
        [
            (5.0, 0.0, False),
            (5.0, 2 * 3600.0 + 60, False),
            (_STALL_CANCEL_TIMEOUT + 60.0, 0.0, True),
            (_STALL_CANCEL_TIMEOUT + 60.0, -3600.0, True),
        ],
        ids=["progressing", "progressing_forward_step", "stalled", "stalled_backward_step"],
    )
    async def test_watchdog_measures_stalls_on_the_monotonic_clock(
        self, monkeypatch: pytest.MonkeyPatch, passed: float, step: float, cancelled: bool
    ) -> None:
        """A wall-clock step neither cancels a progressing task nor spares a stalled one.

        *passed* seconds really pass after the last progress stamp, and the wall
        clock also steps by *step*. Before the fix the watchdog read the age from
        the wall clock: the forward step reset a task that progressed 5 s ago,
        and the backward step left a task stalled past the cancel timeout alone.
        """
        clock = _SteppedClock()
        monkeypatch.setattr(taskrunner_module, "time", clock)
        sessions = _make_mock_sessions()
        sessions.is_provider_alive = AsyncMock(return_value=True)
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.current_task = 1
        run.started_at = run.last_task_time = clock.time()
        ticks = [lambda: clock.advance(passed, step=step)]

        async def _tick(_secs: float) -> None:
            if not ticks:
                raise asyncio.CancelledError()
            ticks.pop(0)()

        class _AsyncioWithSleep:
            """The watchdog's own ``asyncio`` with a scripted ``sleep``; the rest is real."""

            sleep = staticmethod(_tick)

            def __getattr__(self, name: str):
                return getattr(asyncio, name)

        monkeypatch.setattr(taskrunner_module, "asyncio", _AsyncioWithSleep())
        await runner._watchdog_loop(run)

        assert sessions.reset.await_count == (1 if cancelled else 0), (
            f"{passed:.0f} s after the last progress, with a {step:+.0f} s wall-clock step, "
            f"the watchdog reset the step's session {sessions.reset.await_count} time(s)"
        )

    @pytest.mark.asyncio
    async def test_watchdog_exits_when_not_running(self) -> None:
        """Watchdog exits when run status changes from running."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="completed")

        # Should exit immediately
        await runner._watchdog_loop(run)

    @pytest.mark.asyncio
    async def test_watchdog_no_stall_when_recent_progress(self) -> None:
        """Watchdog does NOT notify when last_task_time is recent."""
        import time

        sessions = _make_mock_sessions()
        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.started_at = time.time()
        run.last_task_time = time.time()  # just now

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert not any("stalled" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_resets_correct_session_key(self) -> None:
        """Watchdog must reset the session key matching the current task index."""
        import time

        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=AsyncMock())
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_123"
        run.current_task = 3
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_CANCEL_TIMEOUT - 60

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        sessions.reset.assert_called_with("taskrunner:plan_123:task3")

    @pytest.mark.asyncio
    async def test_watchdog_uses_run_fields_not_runner(self) -> None:
        """Watchdog reads last_task_time and current_task from run, not runner."""
        import time

        sessions = _make_mock_sessions()
        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.started_at = time.time()
        run.last_task_time = time.time()  # recent — should NOT stall

        # Set decoy on runner (the old bug) — watchdog must ignore this
        runner._last_task_time = 0.0  # type: ignore[attr-defined]

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        # If watchdog read runner._last_task_time (0.0), it would stall-notify.
        # It should read run.last_task_time (recent) and NOT notify.
        assert not any("stalled" in t.lower() for t, _ in notifications)


class TestGlobalTimeoutClock:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("passed", "step", "timed_out"),
        [
            (5.0, 0.0, False),
            (5.0, 2 * 3600.0, False),
            (2 * 3600.0, 0.0, True),
            (2 * 3600.0, -3 * 3600.0, True),
        ],
        ids=["healthy", "healthy_forward_step", "over", "over_backward_step"],
    )
    async def test_executor_measures_the_global_timeout_on_the_monotonic_clock(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        passed: float,
        step: float,
        timed_out: bool,
    ) -> None:
        """A wall-clock step neither fails a run inside its global timeout nor spares one past it.

        The first step really takes *passed* seconds, and the wall clock also steps by
        *step*. The executor checks the 1 h timeout before the second step. Read as
        ``time.time() - started_at``, the forward step failed a run 5 s old with "Global
        timeout exceeded", and the backward step let a run 2 h old start another step.
        """
        clock = _SteppedClock()
        monkeypatch.setattr(taskrunner_module, "time", clock)
        runner = TaskRunner(
            sessions=_make_mock_sessions(),
            auto_test=False,
            work_dir=tmp_path,
            global_timeout=3600.0,
        )
        runner._notify = AsyncMock()
        executed: list[int] = []

        async def _execute(run, task, hk="", session_key=""):
            executed.append(task.index)
            if task.index == 1:
                clock.advance(passed, step=step)
            task.status = StepStatus.PASSED
            return True

        runner._execute_single_task = _execute
        run = TaskRun(task_id="gt", spec_path="", spec_content="s", status="running")
        run.started_at = run.last_task_time = clock.time()
        run.tasks = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
        ]

        await runner._execute_tasks(run, "hk")

        assert executed == ([1] if timed_out else [1, 2]), (
            f"{passed:.0f} s into the run, with a {step:+.0f} s wall-clock step, "
            f"the executor ran steps {executed} ({run.status}: {run.error!r})"
        )
        assert (run.status == "failed" and "Global timeout" in run.error) is timed_out

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("passed", "step", "timed_out"),
        [
            (5.0, 0.0, False),
            (5.0, 2 * 3600.0, False),
            (2 * 3600.0, -3 * 3600.0, True),
        ],
        ids=["no_step", "forward_step", "over_backward_step"],
    )
    async def test_the_global_timeout_counts_from_the_run_start_stamp(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        passed: float,
        step: float,
        timed_out: bool,
    ) -> None:
        """The age is taken where ``execute_plan`` stamps the start, not at the executor's entry.

        The clock moves after the start is stamped and before the executor's first
        check: *passed* seconds pass and the wall clock steps by *step*. An age read
        from the wall clock there failed a run 5 s old, or let a run 2 h old run.
        """
        clock = _SteppedClock()
        monkeypatch.setattr(taskrunner_module, "time", clock)
        runner = TaskRunner(
            sessions=_make_mock_sessions(),
            auto_test=False,
            work_dir=tmp_path,
            global_timeout=3600.0,
        )
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="Implement", description="make the change")]
        )
        run = await runner.plan("implement the feature")
        execute_tasks = runner._execute_tasks

        async def _after_the_stamp(project, history_key):
            clock.advance(passed, step=step)
            await execute_tasks(project, history_key)

        async def _execute(project, task, hk="", session_key=""):
            task.status = StepStatus.PASSED
            return True

        runner._execute_tasks = _after_the_stamp
        runner._execute_single_task = _execute

        await runner.execute_plan(run.task_id)
        await asyncio.wait_for(runner._tasks[run.task_id], timeout=_GENEROUS_DEADLINE)

        assert (run.status == "failed" and "Global timeout" in run.error) is timed_out, (
            f"{passed:.0f} s after the start stamp, with a {step:+.0f} s wall-clock step, "
            f"the run ended {run.status} ({run.error!r})"
        )


# ── Phase 12.2: Active Stall Recovery ──


class TestActiveStallRecovery:
    @pytest.mark.asyncio
    async def test_watchdog_cancels_stalled_step(self) -> None:
        """Watchdog resets session after _STALL_CANCEL_TIMEOUT."""
        import time

        sessions = _make_mock_sessions()

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.current_task = 1
        run.started_at = time.time()
        run.last_task_time = time.time() - _STALL_CANCEL_TIMEOUT - 60

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = [None, asyncio.CancelledError()]
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        assert run.task_id in runner._stall_cancelled_ids
        sessions.reset.assert_called()
        assert any("cancelling" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_heartbeat_detects_dead_process(self) -> None:
        """Watchdog resets session when ACP process is dead (heartbeat check)."""
        import time

        sessions = _make_mock_sessions()
        # is_provider_alive returns False → process is dead
        sessions.is_provider_alive = AsyncMock(return_value=False)

        notifications: list[tuple[str, str]] = []

        async def _on_notify(title: str, body: str, task_id: str = "") -> None:
            notifications.append((title, body))

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=_on_notify)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_42"
        run.current_task = 2
        run.started_at = time.time()
        run.last_task_time = time.time()  # recent — no stall

        # Run watchdog for 3 ticks (need 2 consecutive dead checks to trigger reset)
        tick = 0

        async def _tick_sleep(_interval: float) -> None:
            nonlocal tick
            tick += 1
            if tick >= 3:
                raise asyncio.CancelledError()

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = _tick_sleep
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        sessions.reset.assert_called_with("taskrunner:plan_42:task2")
        assert any("process died" in t.lower() for t, _ in notifications)

    @pytest.mark.asyncio
    async def test_watchdog_heartbeat_resets_count_on_task_change(self) -> None:
        """dead_process_count resets when current_task changes between ticks."""
        import time

        sessions = _make_mock_sessions()
        # Always report dead
        sessions.is_provider_alive = AsyncMock(return_value=False)

        runner = TaskRunner(sessions=sessions, auto_test=False, on_notify=AsyncMock())
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running")
        run.task_id = "plan_99"
        run.current_task = 1
        run.started_at = time.time()
        run.last_task_time = time.time()

        tick = 0

        async def _tick_sleep(_interval: float) -> None:
            nonlocal tick
            tick += 1
            if tick == 2:
                run.current_task = 2  # change after task1's check completes
            if tick >= 3:
                raise asyncio.CancelledError()  # stop after task2's first check

        # flake-ok: pre-existing patch of the stdlib asyncio.sleep through kiro_crew.taskrunner for one watchdog tick, restored on exit, moved verbatim; tracked for a de-flake pass
        with patch("kiro_crew.taskrunner.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            mock_sleep.side_effect = _tick_sleep
            try:
                await runner._watchdog_loop(run)
            except asyncio.CancelledError:
                pass

        # Should NOT have triggered reset — each task only got 1 dead check (< threshold of 2)
        sessions.reset.assert_not_called()


# ── Phase 12.2: Token Budget ──


class TestTokenBudget:
    @pytest.mark.asyncio
    async def test_token_budget_exceeded(self, tmp_path: Path) -> None:
        """Token budget exceeded → task fails."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        step_json = json.dumps(
            [
                {"title": "Task ", "description": "d"},
                {"title": "Task ", "description": "d", "depends_on": [1]},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        # Step provider that generates lots of text (tokens)
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text="x" * 4000)  # ~1000 tokens
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path, token_budget=500)

        with patch.object(runner, "self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "failed"
        assert "Token budget" in result.error

    def test_status_includes_tokens(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        run = TaskRun(spec_path="/t.md", spec_content="s", status="running", tokens_used=500)
        run.task_id = "test_1"
        runner._runs["test_1"] = run
        s = runner.status()
        assert s["runs"][0]["tokens_used"] == 500


# ── Phase 12.2: Parallel Step Grouping ──


class TestParallelStepGrouping:
    def test_no_deps_all_parallel(self) -> None:
        """Steps with no dependencies → single parallel group."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d"),
            Step(index=3, title="C", description="d"),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 1
        assert len(groups[0]) == 3

    def test_sequential_deps(self) -> None:
        """Each step depends on previous → all sequential."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
            Step(index=3, title="C", description="d", depends_on=[2]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 3
        assert len(groups[0]) == 1
        assert groups[0][0].title == "A"
        assert groups[1][0].title == "B"
        assert groups[2][0].title == "C"

    def test_diamond_deps(self) -> None:
        """Diamond: A → B,C → D."""
        steps = [
            Step(index=1, title="A", description="d"),
            Step(index=2, title="B", description="d", depends_on=[1]),
            Step(index=3, title="C", description="d", depends_on=[1]),
            Step(index=4, title="D", description="d", depends_on=[2, 3]),
        ]
        groups = TaskRunner._group_parallel_tasks(steps)
        assert len(groups) == 3
        assert len(groups[0]) == 1  # A
        assert len(groups[1]) == 2  # B, C in parallel
        assert len(groups[2]) == 1  # D

    def test_empty_steps(self) -> None:
        groups = TaskRunner._group_parallel_tasks([])
        assert groups == []

    def test_parse_depends_on(self) -> None:
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d", "depends_on": [1]},
            ]
        )
        steps = runner._parse_tasks(text)
        assert steps[0].depends_on == []
        assert steps[1].depends_on == [1]

    def test_parse_invalid_depends_on(self) -> None:
        """Non-list depends_on → treated as empty."""
        sessions = _make_mock_sessions()
        runner = TaskRunner(sessions=sessions, auto_test=False)
        text = json.dumps([{"title": "A", "description": "d", "depends_on": "invalid"}])
        steps = runner._parse_tasks(text)
        assert steps[0].depends_on == []


# ── Parallel Groups ──


class TestParallelGroups:
    @pytest.mark.asyncio
    async def test_parallel_group_runs_all_steps(self, tmp_path: Path) -> None:
        """Steps in a parallel group all execute (via asyncio.gather)."""
        spec = tmp_path / "TASK.md"
        spec.write_text("# Feature", encoding="utf-8")

        sessions = _make_mock_sessions()

        from kiro_crew.providers.base import LLMEvent

        # 3 independent steps (no deps → single parallel group)
        step_json = json.dumps(
            [
                {"title": "A", "description": "d"},
                {"title": "B", "description": "d"},
                {"title": "C", "description": "d"},
            ]
        )

        decompose_provider = MagicMock()

        async def _decompose_stream(msg: str):
            yield LLMEvent(kind="text_chunk", text=step_json)
            yield LLMEvent(kind="complete")

        decompose_provider.stream = _decompose_stream
        decompose_provider.approve_tool = AsyncMock()
        decompose_provider.context_usage_pct = MagicMock(return_value=0.0)

        execution_order: list[str] = []
        step_provider = MagicMock()

        async def _step_stream(msg: str):
            # Track execution order via the step title in the prompt
            for title in ("A", "B", "C"):
                if f"**{title}**" in msg:
                    execution_order.append(title)
                    break
            yield LLMEvent(kind="text_chunk", text="done")
            yield LLMEvent(kind="complete")

        step_provider.stream = _step_stream
        step_provider.approve_tool = AsyncMock()
        step_provider.context_usage_pct = MagicMock(return_value=0.0)

        async def _get_or_create(key: str, agent=None, cwd=None, **kwargs):
            if "decompose" in key:
                return decompose_provider, True, False
            return step_provider, True, False

        sessions.get_or_create = _get_or_create

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            result = await runner.run(spec)

        assert result.status == "completed"
        # All 3 should have run (order may vary but all present)
        assert len(execution_order) == 3


# ── Semaphore-based parallel scheduling (replaces batch loop) ──


class TestSemaphoreParallelScheduling:
    """Pin: _execute_tasks uses asyncio.Semaphore so that a finished slot is
    refilled immediately -- a slow step must NOT block idle slots.

    Regression: the old fixed-size batch loop caused batch-stall where one slow
    step blocked the entire batch even when other slots were free.
    """

    @staticmethod
    def _make_run(tmp_path: Path, n: int, task_id: str) -> TaskRun:
        tasks = [Step(index=i, title=f"t{i}", description="d") for i in range(1, n + 1)]
        run = TaskRun(
            spec_path=str(tmp_path / "spec.md"),
            spec_content="# spec",
            tasks=tasks,
            status="running",
            task_id=task_id,
            work_dir=str(tmp_path),
        )
        run.started_at = run.last_task_time = 1.0
        return run

    def _peak_tracker(self):
        state = {"active": 0, "peak": 0}
        lock = asyncio.Lock()

        async def _fake_exec(run, task, history_key="", session_key="") -> bool:
            async with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            # flake-ok: pre-existing 20 ms hold so the stub steps overlap, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.02)  # hold slot so concurrency overlaps
            async with lock:
                state["active"] -= 1
            task.status = StepStatus.PASSED
            task.result = "ok"
            return True

        return state, _fake_exec

    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrency_at_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Peak concurrency == max_parallel_steps (3) when 8 tasks are dispatched.

        The host-safe ceiling is pinned high on purpose. A small CI runner
        computes a ceiling of 3, which equals the knob under test — the
        assertion would then hold even if the knob were ignored entirely, so
        without this the test proves nothing.
        """
        monkeypatch.setattr(
            "kiro_crew.taskrunner.compute_memory_sized_parallel_cap", lambda _cfg: 64
        )
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=3
        )
        state, fake_exec = self._peak_tracker()
        runner._execute_single_task = fake_exec  # type: ignore[assignment]

        run = self._make_run(tmp_path, n=8, task_id="cap_test")
        await runner._execute_tasks(run, history_key="")

        assert state["peak"] == 3, f"expected peak concurrency 3, got {state['peak']}"
        assert all(t.status == StepStatus.PASSED for t in run.tasks)

    @pytest.mark.asyncio
    async def test_host_ceiling_still_caps_the_knob(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The host-safe ceiling is the upper bound; the knob may only lower it.

        Counterpart to the two tests above, which pin the ceiling out of the way
        to isolate the knob. This one pins it BELOW the knob to prove the OOM
        guard still wins — the property those tests deliberately stop covering.
        """
        monkeypatch.setattr(
            "kiro_crew.taskrunner.compute_memory_sized_parallel_cap", lambda _cfg: 2
        )
        runner = TaskRunner(
            sessions=_make_mock_sessions(),
            auto_test=False,
            work_dir=tmp_path,
            max_parallel_steps=6,
        )
        assert runner._max_parallel_steps == 2

    @pytest.mark.asyncio
    async def test_knob_lifts_above_legacy_cap_of_3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A knob of 6 allows peak concurrency of 6, proving the legacy cap is gone.

        The host-safe ceiling is pinned high because it is derived from the
        machine's memory/CPU headroom: a small CI runner computes 3 — exactly the
        legacy value this test exists to disprove — so leaving it live makes the
        assertion depend on the runner's size rather than on the knob. The
        ceiling's own authority is covered by
        ``test_host_ceiling_still_caps_the_knob``.
        """
        monkeypatch.setattr(
            "kiro_crew.taskrunner.compute_memory_sized_parallel_cap", lambda _cfg: 64
        )
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=6
        )
        state, fake_exec = self._peak_tracker()
        runner._execute_single_task = fake_exec  # type: ignore[assignment]

        run = self._make_run(tmp_path, n=8, task_id="lift_test")
        await runner._execute_tasks(run, history_key="")

        assert state["peak"] == 6, f"expected peak concurrency 6, got {state['peak']}"
        assert all(t.status == StepStatus.PASSED for t in run.tasks)

    @pytest.mark.asyncio
    async def test_no_batch_stall_slot_refilled_immediately(self, tmp_path: Path) -> None:
        """KEY REGRESSION TEST: With limit=2 and 4 tasks where task 1 is slow,
        task 3 must start BEFORE task 1 finishes (it fills the slot vacated by
        task 2). The old batch loop would block because task 1 and 2 were in
        the same batch, and task 3 could only start after that batch completed.
        """
        sessions = _make_mock_sessions()
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, max_parallel_steps=2
        )

        # Events to sequence and observe ordering
        events: list[str] = []
        task2_done = asyncio.Event()
        task3_started = asyncio.Event()
        task1_may_finish = asyncio.Event()

        async def _fake_exec(run, task, history_key="", session_key="") -> bool:
            idx = task.index
            events.append(f"start:{idx}")
            if idx == 1:
                # Slow task: waits until told to finish
                # flake-ok: pre-existing unbounded Event.wait the test releases on its own path, moved verbatim; tracked for a de-flake pass
                await task1_may_finish.wait()
            elif idx == 2:
                # Fast task: signals done quickly
                # flake-ok: pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.01)
                task2_done.set()
            elif idx == 3:
                # Must start while task 1 is still running (refills task 2's slot)
                task3_started.set()
                # flake-ok: pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.01)
            else:
                # flake-ok: pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms stub step, moved verbatim; tracked for a de-flake pass
                await asyncio.sleep(0.01)
            events.append(f"end:{idx}")
            task.status = StepStatus.PASSED
            task.result = "ok"
            return True

        runner._execute_single_task = _fake_exec  # type: ignore[assignment]
        run = self._make_run(tmp_path, n=4, task_id="stall_test")

        async def _orchestrate():
            # Wait for task 2 to finish, then verify task 3 starts before task 1 ends
            # flake-ok: pre-existing unbounded Event.wait on a stub step the run starts itself, moved verbatim; tracked for a de-flake pass
            await task2_done.wait()
            # flake-ok: pre-existing 50 ms wait for the refilled slot, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 50 ms wait for the refilled slot, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0.05)  # give task 3 time to acquire semaphore
            assert task3_started.is_set(), (
                "BATCH STALL: task 3 did not start after task 2 freed its slot; "
                "this means the scheduler is still using fixed batches"
            )
            # Now let task 1 finish
            task1_may_finish.set()

        # Run both concurrently
        # flake-ok: pre-existing unbounded gather of the run and its orchestrator, moved verbatim; tracked for a de-flake pass
        await asyncio.gather(
            runner._execute_tasks(run, history_key=""),
            _orchestrate(),
        )

        assert all(t.status == StepStatus.PASSED for t in run.tasks)
        # task 3 started before task 1 ended -- proving slot was refilled immediately
        start3_idx = events.index("start:3")
        end1_idx = events.index("end:1")
        assert start3_idx < end1_idx, (
            f"task 3 started at index {start3_idx} but task 1 ended at {end1_idx}; "
            "expected task 3 to start BEFORE task 1 finishes (slot refill)"
        )
