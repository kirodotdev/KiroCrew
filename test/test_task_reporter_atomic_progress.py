"""A failed TASK_PROGRESS.md save must not truncate the previous checkpoint.

``save_progress`` records which steps a run has passed; ``load_checkpoint`` reads
that file to decide which steps a resumed run skips. ``Path.write_text`` truncates
the file before writing, so a save that fails part-way (ENOSPC, a crash) left a
truncated file and a resume re-ran steps that were already done. The save is
atomic now (temp sibling + rename), so a failed save leaves the previous file
intact.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import kiro_crew.task_reporter as task_reporter
from kiro_crew.task_models import Project, Task, TaskStatus


def _project(spec: Path, titles: list[str]) -> Project:
    run = Project(spec_path=str(spec), spec_content="x")
    run.status = "running"
    run.started_at = 1000.0
    run.finished_at = 1001.0
    run.tasks = [
        Task(index=i + 1, title=t, description="d", status=TaskStatus.PASSED)
        for i, t in enumerate(titles)
    ]
    return run


def test_a_clean_save_round_trips_through_load_checkpoint(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    task_reporter.save_progress(_project(spec, ["Explore the codebase", "Write the API"]))
    assert task_reporter.load_checkpoint(spec) == {"explore the codebase", "write the api"}


def test_a_failed_save_leaves_the_previous_checkpoint_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")

    # First save records two passed steps.
    task_reporter.save_progress(_project(spec, ["Explore the codebase", "Write the API"]))
    assert task_reporter.load_checkpoint(spec) == {"explore the codebase", "write the api"}

    # The next save fails part-way: a truncate-then-fail write, which is exactly
    # what an in-place ``Path.write_text`` does on ENOSPC or a crash. Patched at
    # the OS layer both the in-place write and the atomic write's staging file
    # go through, so the test is implementation-independent -- the old code
    # truncated TASK_PROGRESS.md itself, the fix truncates only its temp sibling.
    real_write = os.write
    progress = spec.parent / task_reporter.PROGRESS_FILE
    progress_bytes = progress.read_bytes()

    def _truncating_write(fd: int, data: bytes) -> int:
        # Write the first chunk, then fail -- so a target opened for truncation
        # is left short, matching a real interrupted write.
        if data:
            real_write(fd, data[: len(data) // 2])
        raise OSError("ENOSPC: no space left on device")

    monkeypatch.setattr(os, "write", _truncating_write)
    # save_progress swallows the failure (logs at DEBUG), by contract.
    task_reporter.save_progress(
        _project(spec, ["Explore the codebase", "Write the API", "Ship the release"])
    )
    monkeypatch.undo()

    # The previous checkpoint is still whole -- byte-for-byte, not truncated to
    # fewer steps -- so a resume does not re-run the steps the first save
    # recorded.
    assert progress.read_bytes() == progress_bytes
    assert task_reporter.load_checkpoint(spec) == {"explore the codebase", "write the api"}


def test_the_progress_file_is_never_left_as_a_temp_sibling(tmp_path: Path) -> None:
    spec = tmp_path / "feature.md"
    spec.write_text("# Feature\n", encoding="utf-8")
    task_reporter.save_progress(_project(spec, ["Explore the codebase"]))
    # Atomic write cleans up its staging file: only TASK_PROGRESS.md (and the
    # spec) remain, no ``.tmp`` leftover.
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["TASK_PROGRESS.md", "feature.md"]
