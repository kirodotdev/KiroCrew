"""What a task run is started with, before any step runs.

A run's name (given, or derived from the spec's task header, heading, path or
first line) and the lookup by name or id; a ``workspace_dir`` that names a
credential or secret location is refused, through a traversal or a symlink too;
the parallel-step knob is clamped to the host-safe ceiling; and the session key
a background start came from reaches the notify sink. The shared doubles live in
``taskrunner_helpers.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from taskrunner_helpers import (
    _make_mock_sessions,
)

from kiro_crew.taskrunner import Step, TaskRun, TaskRunner


class TestTaskNaming:
    """Tests for the task naming feature."""

    def test_auto_name_from_task_header(self) -> None:
        assert TaskRunner._auto_name("# Task: Deploy new service\nStep 1") == "Deploy new service"

    def test_auto_name_task_header_after_generic_heading(self) -> None:
        assert TaskRunner._auto_name("# Introduction\n# Task: Deploy Service") == "Deploy Service"

    def test_auto_name_from_h1(self) -> None:
        assert TaskRunner._auto_name("# My Cool Task\ndo stuff") == "My Cool Task"

    def test_auto_name_from_spec_path(self) -> None:
        assert TaskRunner._auto_name("no heading here", "deploy_service.md") == "Deploy Service"

    def test_auto_name_fallback_first_line(self) -> None:
        assert TaskRunner._auto_name("no heading here") == "no heading here"

    def test_auto_name_empty(self) -> None:
        assert TaskRunner._auto_name("") == ""

    def test_auto_name_truncates_at_60(self) -> None:
        long = "# Task: " + "A" * 100
        assert len(TaskRunner._auto_name(long)) == 60

    def test_taskrun_name_field_default(self) -> None:
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="planned"
        )
        assert run.name == ""

    def test_resolve_task_by_name(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="running"
        )
        run.task_id = "abc_123"
        run.name = "deploy prod"
        runner._runs["abc_123"] = run
        assert runner._resolve_task("deploy prod") is run

    def test_resolve_task_by_id(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        run = TaskRun(
            spec_path="x", spec_content="x", started_at=0, last_task_time=0, status="running"
        )
        run.task_id = "abc_123"
        runner._runs["abc_123"] = run
        assert runner._resolve_task("abc_123") is run

    def test_resolve_task_not_found(self) -> None:
        runner = TaskRunner.__new__(TaskRunner)
        runner._runs = {}
        assert runner._resolve_task("nope") is None

    @pytest.mark.asyncio
    async def test_run_uses_explicit_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: Auto Name\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec, name="My Custom Name")
        assert run.name == "My Custom Name"

    @pytest.mark.asyncio
    async def test_run_auto_derives_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: Auto Derived\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec)
        assert run.name == "Auto Derived"

    @pytest.mark.asyncio
    async def test_start_background_passes_name(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Bg Task\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock) as mock_run:
            task_id = await runner.start_background(spec, name="bg-task")
            assert task_id
            # flake-ok: pre-existing zero-delay yield before the call assert, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(0)  # yield to let the background task run
            mock_run.assert_called_once()
            assert mock_run.call_args.kwargs.get("name") == "bg-task"


class TestWorkspaceDirValidation:
    """taskrunner.workspace_dir must reject credential/secret paths (security)."""

    def test_sensitive_workspace_dir_rejected(self):
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir="~/.aws")

    def test_sensitive_ssh_dir_rejected(self):
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir="~/.ssh")

    def test_normal_workspace_dir_accepted(self, tmp_path):
        tr = TaskRunner(sessions=_make_mock_sessions(), workspace_dir=str(tmp_path))
        assert tr._workspace_dir == str(Path(tmp_path).resolve())

    def test_traversal_into_sensitive_dir_rejected(self):
        # A `..`-traversal spelling that resolves into ~/.aws must be caught
        # AFTER canonicalization, not slip past the raw-string check.
        aws = Path("~/.aws").expanduser().resolve()
        sneaky = str(aws.parent / "x" / ".." / ".aws")
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir=sneaky)

    def test_symlink_to_sensitive_dir_rejected(self, tmp_path):
        # A symlink pointing at a credential dir must be rejected once resolved.
        target = Path("~/.ssh").expanduser().resolve()
        link = tmp_path / "innocent"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks not supported on this platform")
        with pytest.raises(ValueError, match="sensitive"):
            TaskRunner(sessions=_make_mock_sessions(), workspace_dir=str(link))

    def test_resolve_helper_blank_and_normal(self, tmp_path):
        from kiro_crew.taskrunner import _resolve_workspace_dir

        assert _resolve_workspace_dir("") == ""
        assert _resolve_workspace_dir("   ") == ""
        assert _resolve_workspace_dir(str(tmp_path)) == str(Path(tmp_path).resolve())

    def test_resolve_helper_rejects_sensitive(self):
        from kiro_crew.taskrunner import _resolve_workspace_dir

        with pytest.raises(ValueError, match="sensitive"):
            _resolve_workspace_dir("~/.aws")

    @pytest.mark.asyncio
    async def test_start_background_rejects_sensitive_workspace(self, tmp_path):
        # The per-run override is validated synchronously at the top of
        # start_background so the HTTP handler surfaces a 400 immediately —
        # a bad path must raise before any background task is spawned.
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        with pytest.raises(ValueError, match="sensitive"):
            await runner.start_background("anything.md", workspace_dir="~/.ssh")

    @pytest.mark.asyncio
    async def test_plan_per_run_workspace_override(self, tmp_path):
        # A per-run workspace_dir overrides the runner's default base dir: the
        # planned run operates directly in the chosen (resolved) folder.
        runner = TaskRunner(sessions=_make_mock_sessions(), auto_test=False, work_dir=tmp_path)
        runner._decompose = AsyncMock(
            return_value=[Step(index=1, title="step", description="desc")]
        )
        override = tmp_path / "custom-root"
        run = await runner.plan(input_text="do X", source="text", workspace_dir=str(override))
        assert run.work_dir == str(override.resolve())


class TestMaxParallelStepsClamp:
    """`compute_memory_sized_parallel_cap` is the host-safe ceiling; a positive
    `max_parallel_steps` may only lower it, never raise it above the ceiling."""

    def _cap(self, value):
        sessions = _make_mock_sessions()
        # Pin the computed host-safe ceiling to a known value (9).
        with patch("kiro_crew.taskrunner.compute_memory_sized_parallel_cap", return_value=9):
            runner = TaskRunner(sessions=sessions, auto_test=False, max_parallel_steps=value)
        return runner._max_parallel_steps

    def test_auto_zero_uses_computed_ceiling(self):
        assert self._cap(0) == 9

    def test_none_uses_computed_ceiling(self):
        assert self._cap(None) == 9

    def test_positive_below_ceiling_is_honored(self):
        # Intentional throttle below the ceiling is respected.
        assert self._cap(2) == 2

    def test_positive_above_ceiling_is_clamped(self):
        # An aggressive value can never exceed the host-safe ceiling.
        assert self._cap(50) == 9

    def test_compute_failure_falls_back_to_legacy_default(self):
        sessions = _make_mock_sessions()
        with patch(
            "kiro_crew.taskrunner.compute_memory_sized_parallel_cap",
            side_effect=RuntimeError("boom"),
        ):
            runner = TaskRunner(sessions=sessions, auto_test=False, max_parallel_steps=0)
        # Falls back to _MAX_PARALLEL_TASKS (3) when the ceiling can't be computed.
        assert runner._max_parallel_steps == 3


class TestNotifySessionKey:
    """``start_background(session_key=)`` reaches the notify sink as a keyword.

    The sink is what routes a stall-worthy notice (an approval request, a denial)
    back to the surface the run was started from. ``notify`` swallows sink
    failures at debug level, so "the keyword broke the sink" and "there was no
    notification" look identical from the outside — which is why each of these
    asserts the notification ARRIVED, not merely that the call was shaped right.
    """

    @staticmethod
    def _spec(tmp_path: Path) -> Path:
        spec = tmp_path / "spec.md"
        spec.write_text("# Bg Task\n## Steps\n1. Do thing\n   - run: echo hi")
        return spec

    async def _start(self, tmp_path: Path, sink, session_key: str) -> tuple[TaskRunner, TaskRun]:
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path, on_notify=sink)
        with patch.object(runner, "run", new_callable=AsyncMock):
            task_id = await runner.start_background(self._spec(tmp_path), session_key=session_key)
            # Drain the background wrapper here rather than leaving it to be
            # garbage-collected: a task still pending at teardown escapes into
            # the next test and prints "Task was destroyed but it is pending".
            background = runner._tasks.get(task_id)
            if background is not None:
                # flake-ok: pre-existing unbounded await of the background run task, which ends on its own, moved verbatim; tracked for a de-flake pass
                await background
        return runner, runner._runs[task_id]

    @pytest.mark.asyncio
    async def test_session_aware_sink_receives_the_originating_key(self, tmp_path: Path) -> None:
        seen: list[tuple[str, str]] = []

        async def _sink(title: str, body: str, task_id: str = "", *, session_key: str = "") -> None:
            seen.append((title, session_key))

        runner, run = await self._start(tmp_path, _sink, "telegram:kirocrew:direct:U9")
        await runner._notify("Task 1 requires approval", "run the deploy?", run=run)

        assert seen == [("[spec] Task 1 requires approval", "telegram:kirocrew:direct:U9")]

    @pytest.mark.asyncio
    async def test_omitted_session_key_leaves_the_call_shape_untouched(
        self, tmp_path: Path
    ) -> None:
        """No originating conversation ⇒ the sink sees the call it always saw.

        Asserted on the call SHAPE, not merely on an empty value: a sink is only
        obliged to accept ``session_key`` once something hands it one, so a
        notification with no origin has to arrive as the three-argument call every
        pre-existing sink was written against.
        """
        calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        async def _sink(*args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))

        runner, run = await self._start(tmp_path, _sink, "")
        await runner._notify("Task 1 requires approval", "run the deploy?", run=run)

        assert calls == [(("[spec] Task 1 requires approval", "run the deploy?", run.task_id), {})]

    @pytest.mark.asyncio
    async def test_legacy_three_arg_sink_is_still_notified(self, tmp_path: Path) -> None:
        """A sink that predates the keyword keeps working, present key or not.

        The keyword is keyword-only WITH a default so a sink can adopt it at its
        own pace; handing it to one that cannot take it would raise a
        ``TypeError`` into ``notify``'s best-effort ``except``, and that sink's
        notifications would simply stop arriving with nothing logged above debug.
        """
        seen: list[str] = []

        async def _legacy(title: str, body: str, task_id: str = "") -> None:
            seen.append(title)

        runner, run = await self._start(tmp_path, _legacy, "telegram:kirocrew:direct:U9")
        await runner._notify("Task 1 requires approval", "run the deploy?", run=run)

        assert seen == ["[spec] Task 1 requires approval"]

    @pytest.mark.asyncio
    async def test_delete_forgets_the_originating_key(self, tmp_path: Path) -> None:
        """The mapping is per-run bookkeeping, so deleting a run releases it."""
        runner, run = await self._start(tmp_path, AsyncMock(), "telegram:kirocrew:direct:U9")
        assert runner._run_session_keys.get(run.task_id)

        assert await runner.delete_run(run.task_id) is True
        assert run.task_id not in runner._run_session_keys
