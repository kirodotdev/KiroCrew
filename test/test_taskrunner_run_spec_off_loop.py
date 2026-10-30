"""A foreground task run probes its spec and work dir off the event loop.

``TaskRunner.run`` checks that the caller-supplied spec path exists and creates
the run's work directory before any step starts. Both are filesystem syscalls
that block on a slow or wedged mount, so each runs in a worker thread, and a
missing spec still fails the run with ``FileNotFoundError``. The shared doubles
live in ``taskrunner_helpers.py``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from taskrunner_helpers import _make_mock_sessions

from kiro_crew.taskrunner import TaskRunner


def _spy(monkeypatch: pytest.MonkeyPatch, method: str, match: Callable[[Path], bool]) -> list[int]:
    """Record the thread of every ``Path.<method>`` call whose path matches."""
    threads: list[int] = []
    original = getattr(Path, method)

    def _wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
        if match(self):
            threads.append(threading.get_ident())
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, _wrapper)
    return threads


def _write_spec(tmp_path: Path) -> Path:
    spec = tmp_path / "offloop_spec.md"
    spec.write_text("# Task: Off loop\n## Steps\n1. Do thing\n   - run: echo hi", encoding="utf-8")
    return spec


class TestRunSpecIoOffTheEventLoop:
    @pytest.mark.asyncio
    async def test_spec_exists_probe_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _write_spec(tmp_path)
        loop_thread = threading.get_ident()
        probes = _spy(monkeypatch, "exists", lambda p: p == spec)
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path / "work")
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec)
        assert run.spec_path == str(spec)
        assert probes and all(thread != loop_thread for thread in probes)

    @pytest.mark.asyncio
    async def test_work_dir_mkdir_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _write_spec(tmp_path)
        task_dir = tmp_path / "work" / spec.stem
        loop_thread = threading.get_ident()
        mkdirs = _spy(monkeypatch, "mkdir", lambda p: p == task_dir)
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path / "work")
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec)
        assert run.work_dir == str(task_dir)
        assert task_dir.is_dir()
        assert mkdirs and all(thread != loop_thread for thread in mkdirs)

    @pytest.mark.asyncio
    async def test_missing_spec_still_raises_file_not_found(self, tmp_path: Path) -> None:
        missing = tmp_path / "absent_spec.md"
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path / "work")
        with pytest.raises(FileNotFoundError, match="Spec not found"):
            await runner.run(missing)
        assert not (tmp_path / "work" / missing.stem).exists()
