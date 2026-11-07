"""A progress save on a full disk keeps the previous resume checkpoint.

``load_checkpoint`` reads the passed lines of TASK_PROGRESS.md to choose which
steps a resumed run skips, so a save that runs out of space must not cut them off.
"""

from __future__ import annotations

import builtins
import errno
import io
import os
from pathlib import Path

from kiro_crew.task_models import PROGRESS_FILE, Project, Task, TaskStatus
from kiro_crew.task_reporter import load_checkpoint, save_progress

_REAL_OPEN = io.open


class _FullDiskFile(io.FileIO):
    """A file on a full disk: a write lands only on bytes the file already holds."""

    def write(self, b):
        room = os.fstat(self.fileno()).st_size - self.tell()
        if room <= 0:
            raise OSError(errno.ENOSPC, "No space left on device")
        return super().write(memoryview(b)[:room])


def _open_on_full_disk(
    file,
    mode="r",
    buffering=-1,
    encoding=None,
    errors=None,
    newline=None,
    closefd=True,
    opener=None,
):
    if "w" not in mode:
        return _REAL_OPEN(file, mode, buffering, encoding, errors, newline, closefd, opener)
    buffered = io.BufferedWriter(_FullDiskFile(file, "w", closefd, opener))
    return buffered if "b" in mode else io.TextIOWrapper(buffered, encoding, errors, newline)


def _run(spec: Path, passed: int) -> Project:
    tasks = [
        Task(
            index=i,
            title=f"step {i}",
            description="",
            status=TaskStatus.PASSED if i <= passed else TaskStatus.PENDING,
        )
        for i in range(1, 7)
    ]
    return Project(spec_path=str(spec), spec_content="# spec\n", tasks=tasks, status="running")


def test_a_save_on_a_full_disk_keeps_every_passed_step(tmp_path, monkeypatch):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    save_progress(_run(spec, passed=4))
    progress = tmp_path / PROGRESS_FILE
    before = progress.read_bytes()

    grown = _run(spec, passed=5)
    grown.tasks[4].attempts = 2  # the file grows, so this save needs room the disk lacks
    with monkeypatch.context() as full_disk:
        full_disk.setattr(builtins, "open", _open_on_full_disk)
        full_disk.setattr(io, "open", _open_on_full_disk)
        save_progress(grown)

    assert progress.read_bytes() == before
    assert load_checkpoint(spec) == {"step 1", "step 2", "step 3", "step 4"}


def test_a_shorter_save_drops_the_old_tail(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    save_progress(_run(spec, passed=4))

    shorter = _run(spec, passed=1)
    shorter.tasks = shorter.tasks[:2]  # a replan to fewer tasks
    save_progress(shorter)

    assert load_checkpoint(spec) == {"step 1"}
