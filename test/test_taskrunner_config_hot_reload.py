"""TaskRunner live config: ``taskrunner.*`` is re-read at every run entry.

The gateway constructs one ``TaskRunner`` from ``taskrunner.max_parallel_steps``
and ``taskrunner.workspace_dir``. Each run entry (``run`` / ``plan`` /
``execute_plan``) re-reads config and adopts a field whose value moved since
construction, while a field the config never changed keeps the constructor's
argument, and a run that is already executing keeps the cap it started with.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.taskrunner import TaskRunner


def _sessions() -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.get_or_create = AsyncMock()
    sessions.release = MagicMock()
    return sessions


def _runner(tmp_path: Path, **kw) -> TaskRunner:
    with (
        patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9),
        patch("kiro_crew.taskrunner.KiroCrewConfig.load", return_value=KiroCrewConfig()),
    ):
        return TaskRunner(sessions=_sessions(), auto_test=False, work_dir=tmp_path, **kw)


def _snapshot(cfg: KiroCrewConfig):
    return patch("kiro_crew.taskrunner.live.snapshot", return_value=cfg)


class TestPerRunReRead:
    def test_changed_max_parallel_steps_is_adopted(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path, max_parallel_steps=0)
        assert runner._max_parallel_steps == 9
        cfg = KiroCrewConfig()
        cfg.taskrunner.max_parallel_steps = 2
        with _snapshot(cfg), patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9):
            runner._refresh_from_config()
        assert runner._max_parallel_steps == 2

    def test_adopted_value_is_still_clamped_to_the_host_ceiling(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path, max_parallel_steps=0)
        cfg = KiroCrewConfig()
        cfg.taskrunner.max_parallel_steps = 50
        with _snapshot(cfg), patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9):
            runner._refresh_from_config()
        assert runner._max_parallel_steps == 9

    def test_unchanged_config_keeps_the_constructor_argument(self, tmp_path: Path) -> None:
        """An embedder's explicit ctor value survives a reload that never mentioned it."""
        runner = _runner(tmp_path, max_parallel_steps=2)
        assert runner._max_parallel_steps == 2
        with (
            _snapshot(KiroCrewConfig()),
            patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9),
        ):
            runner._refresh_from_config()
        assert runner._max_parallel_steps == 2

    def test_changed_workspace_dir_is_adopted_and_reverted(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        assert runner._workspace_dir == ""
        target = tmp_path / "ws"
        target.mkdir()
        cfg = KiroCrewConfig()
        cfg.taskrunner.workspace_dir = str(target)
        with _snapshot(cfg), patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9):
            runner._refresh_from_config()
        assert Path(runner._workspace_dir) == target.resolve()
        assert runner._work_dir == Path(runner._workspace_dir)

        # Writing the field back to what the runner was constructed against
        # restores the constructor's target exactly.
        with (
            _snapshot(KiroCrewConfig()),
            patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9),
        ):
            runner._refresh_from_config()
        assert runner._workspace_dir == ""
        assert runner._work_dir == tmp_path

    def test_rejected_workspace_dir_keeps_the_current_target(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        cfg = KiroCrewConfig()
        cfg.taskrunner.workspace_dir = "/somewhere/new"
        with (
            _snapshot(cfg),
            patch("kiro_crew.taskrunner._resolve_workspace_dir", side_effect=ValueError("no")),
            patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9),
        ):
            runner._refresh_from_config()
        assert runner._workspace_dir == ""
        assert runner._work_dir == tmp_path

    def test_before_the_watcher_is_primed_the_constructor_values_stand(
        self, tmp_path: Path
    ) -> None:
        """No snapshot means no refresh -- never a ``load()`` on the event loop
        the entry points run on. The constructor's values hold until the first
        run after priming."""
        runner = _runner(tmp_path, max_parallel_steps=0)
        assert runner._max_parallel_steps == 9
        with (
            patch("kiro_crew.taskrunner.live.snapshot", return_value=None),
            patch("kiro_crew.taskrunner.KiroCrewConfig.load") as load,
            patch("kiro_crew.taskrunner.compute_max_subagents", return_value=9),
        ):
            runner._refresh_from_config()
        load.assert_not_called()
        assert runner._max_parallel_steps == 9


class TestEntryPointsRefresh:
    @pytest.mark.asyncio
    async def test_run_refreshes_before_binding_the_work_dir(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        spec = tmp_path / "spec.md"
        spec.write_text("do a thing", encoding="utf-8")
        seen: list[str] = []

        def _refresh() -> None:
            seen.append("refreshed")
            raise RuntimeError("stop here")

        runner._refresh_from_config = _refresh  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="stop here"):
            await runner.run(spec)
        assert seen == ["refreshed"]
        assert runner._runs == {}

    @pytest.mark.asyncio
    async def test_a_running_execution_keeps_its_cap(self, tmp_path: Path) -> None:
        """``_execute_tasks`` binds the cap once; a later refresh does not resize it."""
        from kiro_crew.task_models import Project, Task, TaskStatus

        runner = _runner(tmp_path, max_parallel_steps=0)
        run = Project(spec_path="s", spec_content="c", status="running")
        run.task_id = "t1"
        run.tasks = [
            Task(index=1, title="a", description="a", status=TaskStatus.PENDING),
            Task(index=2, title="b", description="b", status=TaskStatus.PENDING),
        ]
        sizes: list[int] = []
        real_sem = __import__("asyncio").Semaphore

        def _sem(n: int):
            sizes.append(n)
            # Simulate a config reload adopted by ANOTHER run's entry while this
            # execution is mid-flight.
            runner._max_parallel_steps = 1
            return real_sem(n)

        with (
            patch(
                "kiro_crew.taskrunner.group_parallel_tasks",
                return_value=[[run.tasks[0], run.tasks[1]]],
            ),
            patch.object(runner, "_execute_single_task", AsyncMock(return_value=True)),
            patch.object(runner, "_notify", AsyncMock()),
            patch.object(runner, "_apersist_runs", AsyncMock()),
            patch("kiro_crew.taskrunner.asyncio.Semaphore", side_effect=_sem),
        ):
            await runner._execute_tasks(run, "hk")

        assert sizes == [9]
