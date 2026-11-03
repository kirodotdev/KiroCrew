"""An interrupted TASK_PROGRESS.md write keeps the previous resume checkpoint.

``load_checkpoint`` reads the passed lines of TASK_PROGRESS.md to decide which
steps a resumed run may skip. A save that dies part-way (disk full, a crash
during the write) must therefore leave the last complete file in place: a torn
file would drop passed lines and the resume would run those steps again.

The interruption is simulated at both write primitives a progress save can go
through, writing half of the payload and then failing with ``ENOSPC``, so the
test pins the behaviour rather than one implementation.
"""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

import pytest

from kiro_crew import atomic_write as atomic_write_mod
from kiro_crew import task_reporter
from kiro_crew.task_models import Project, Task, TaskStatus
from kiro_crew.task_reporter import PROGRESS_FILE, load_checkpoint, save_progress


def _run(spec: Path, passed: int, total: int) -> Project:
    tasks = [
        Task(
            index=i + 1,
            title=f"step {i + 1}",
            description="",
            status=TaskStatus.PASSED if i < passed else TaskStatus.PENDING,
        )
        for i in range(total)
    ]
    return Project(spec_path=str(spec), spec_content="# spec\n", tasks=tasks, status="running")


def _torn_path_write_text(self, data, encoding=None, errors=None, newline=None):
    with open(self, "w", encoding=encoding, errors=errors, newline=newline) as handle:
        handle.write(data[: len(data) // 2])
    raise OSError(errno.ENOSPC, "No space left on device")


def _torn_write_all(fd, data, path):
    os.write(fd, data[: len(data) // 2])
    raise OSError(errno.ENOSPC, "No space left on device")


@pytest.mark.skipif(os.name != "posix", reason="Windows keeps the in-place write")
def test_an_interrupted_save_keeps_every_passed_step(tmp_path, monkeypatch):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    save_progress(_run(spec, passed=4, total=6))
    assert load_checkpoint(spec) == {"step 1", "step 2", "step 3", "step 4"}
    before = (tmp_path / PROGRESS_FILE).read_bytes()

    monkeypatch.setattr(Path, "write_text", _torn_path_write_text)
    monkeypatch.setattr(atomic_write_mod, "_write_all", _torn_write_all)
    save_progress(_run(spec, passed=5, total=6))

    assert (tmp_path / PROGRESS_FILE).read_bytes() == before
    assert load_checkpoint(spec) == {"step 1", "step 2", "step 3", "step 4"}
    assert sorted(p.name for p in tmp_path.iterdir()) == [PROGRESS_FILE, "spec.md"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_a_replaced_file_keeps_its_permission_bits(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    save_progress(_run(spec, passed=1, total=2))
    (tmp_path / PROGRESS_FILE).chmod(0o600)

    save_progress(_run(spec, passed=2, total=2))

    assert stat.S_IMODE((tmp_path / PROGRESS_FILE).stat().st_mode) == 0o600
    assert load_checkpoint(spec) == {"step 1", "step 2"}


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_symlinked_file_keeps_its_link(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    real = tmp_path / "real-progress.md"
    real.write_text("", encoding="utf-8")
    (tmp_path / PROGRESS_FILE).symlink_to(real)

    save_progress(_run(spec, passed=1, total=1))

    assert (tmp_path / PROGRESS_FILE).is_symlink()
    assert "**Task 1:** step 1" in real.read_text(encoding="utf-8")


@pytest.mark.skipif(os.name != "posix", reason="symlinks need privileges on Windows")
def test_a_dangling_symlink_is_written_through(tmp_path):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    real = tmp_path / "not-yet-created.md"
    (tmp_path / PROGRESS_FILE).symlink_to(real)

    save_progress(_run(spec, passed=1, total=1))

    assert (tmp_path / PROGRESS_FILE).is_symlink()
    assert "**Task 1:** step 1" in real.read_text(encoding="utf-8")


def test_a_removed_spec_directory_is_not_recreated(tmp_path):
    spec_dir = tmp_path / "gone"
    spec_dir.mkdir()
    spec = spec_dir / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    run = _run(spec, passed=1, total=1)
    spec.unlink()
    spec_dir.rmdir()

    save_progress(run)

    assert not spec_dir.exists()


def test_where_a_rename_drops_access_control_the_file_is_written_in_place(tmp_path, monkeypatch):
    spec = tmp_path / "spec.md"
    spec.write_text("# spec\n", encoding="utf-8")
    monkeypatch.setattr(task_reporter, "_RENAME_KEEPS_ACCESS_CONTROL", False)

    def _no_rename(*_args, **_kwargs):
        raise AssertionError("atomic_write must not run where a rename drops the DACL")

    monkeypatch.setattr(task_reporter, "atomic_write", _no_rename)
    save_progress(_run(spec, passed=1, total=1))

    assert load_checkpoint(spec) == {"step 1"}
