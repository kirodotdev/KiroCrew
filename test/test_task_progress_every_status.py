"""``save_progress`` writes TASK_PROGRESS.md for a run holding a task in any status.

The progress file picks an icon per task status, inside a broad ``except`` that
only logs. A status without an icon would make the whole write fail quietly and
leave the previous snapshot on disk, so every ``TaskStatus`` member is pinned
here, and a cancel must replace the running snapshot.
"""

from __future__ import annotations

import pytest

from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_reporter import PROGRESS_FILE, load_checkpoint, save_progress


def _run(tmp_path, statuses: list[TaskStatus]) -> Project:
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    tasks = [
        Task(index=i + 1, title=f"step {i + 1}", description="", status=status)
        for i, status in enumerate(statuses)
    ]
    return Project(spec_path=str(spec), spec_content="# spec\n", tasks=tasks, status="cancelled")


@pytest.mark.parametrize("status", list(TaskStatus))
def test_every_task_status_is_written(tmp_path, status):
    save_progress(_run(tmp_path, [status]))

    text = (tmp_path / PROGRESS_FILE).read_text(encoding="utf-8")
    assert "**Task 1:** step 1" in text


def test_a_cancelled_run_replaces_the_running_snapshot(tmp_path):
    run = _run(tmp_path, [TaskStatus.PASSED, TaskStatus.IN_PROGRESS, TaskStatus.PENDING])
    run.status = "running"
    save_progress(run)

    run.tasks[1].status = TaskStatus.CANCELLED
    run.tasks[2].status = TaskStatus.CANCELLED
    run.status = "cancelled"
    save_progress(run)

    text = (tmp_path / PROGRESS_FILE).read_text(encoding="utf-8")
    assert "**Status:** cancelled" in text
    assert "🔄" not in text
    # The passed step still reads as the resume checkpoint; cancelled ones do not.
    assert load_checkpoint(tmp_path / "spec.md") == {"step 1"}
