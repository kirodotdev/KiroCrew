"""The task-runner handlers keep their spec file I/O off the event loop.

Each potentially blocking filesystem call runs in a worker thread, and a
request cancelled mid-write, mid-start or mid-claim leaves no orphaned spec
or plan directory behind, while never unlinking a spec a registered run still
holds. Shared helpers live in ``handlers_taskrunner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _body,
    _planned_run,
    _request,
    _runner,
    _state,
    _stub_sel,
)

from kiro_crew.dashboard.handlers.taskrunner import api_taskrunner_from_chat, api_taskrunner_start
from kiro_crew.taskrunner import TaskRun

# ── AUTOSDE no-blocking-call-on-event-loop ──


class TestSpecIoRunsOffTheEventLoop:
    """The handler's potentially blocking filesystem calls run in workers."""

    @staticmethod
    def _spy(monkeypatch, method: str, match, sink: list[int]):
        original = getattr(Path, method)

        def _wrapper(self: Path, *args: Any, **kwargs: Any) -> Any:
            if match(self):
                sink.append(threading.get_ident())
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, _wrapper)

    @pytest.mark.asyncio
    async def test_inline_spec_write_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        loop_thread = threading.get_ident()
        writes: list[int] = []
        self._spy(monkeypatch, "write_text", lambda p: p.name.startswith("TASK_"), writes)
        runner = _runner(tmp_path)
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# hello"})
        )
        assert resp.status == 200
        assert Path(_body(resp)["spec"]).read_text(encoding="utf-8") == "# hello"
        assert writes and all(thread != loop_thread for thread in writes)

    @pytest.mark.asyncio
    async def test_spec_path_guard_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# s", encoding="utf-8")
        loop_thread = threading.get_ident()
        stats: list[int] = []
        self._spy(monkeypatch, "is_file", lambda p: p.name == "spec.md", stats)
        runner = _runner(tmp_path)
        resp = await api_taskrunner_start(_request(_state(runner), json_body={"spec": str(spec)}))
        assert resp.status == 200
        assert stats and all(thread != loop_thread for thread in stats)

    @pytest.mark.asyncio
    async def test_rejected_start_removes_inline_spec_off_the_loop(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        loop_thread = threading.get_ident()
        unlinks: list[int] = []
        self._spy(monkeypatch, "unlink", lambda p: p.name.startswith("TASK_"), unlinks)
        runner = _runner(tmp_path)
        runner.start_background = AsyncMock(side_effect=RuntimeError("cannot start"))
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# t"})
        )
        assert resp.status == 400
        assert unlinks and all(thread != loop_thread for thread in unlinks)

    @pytest.mark.asyncio
    async def test_from_chat_plan_mkdir_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        loop_thread = threading.get_ident()
        mkdirs: list[int] = []
        self._spy(monkeypatch, "mkdir", lambda p: p.name.startswith("plan_"), mkdirs)
        runner = _runner(tmp_path)
        runner.update_plan = AsyncMock(return_value=_planned_run("plan_x"))
        resp = await api_taskrunner_from_chat(
            _request(_state(runner), json_body={"steps": [{"title": "first"}]})
        )
        assert resp.status == 200
        assert mkdirs and all(thread != loop_thread for thread in mkdirs)

    @pytest.mark.asyncio
    async def test_from_chat_rollback_rmdir_runs_off_the_event_loop_thread(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        loop_thread = threading.get_ident()
        rmdirs: list[int] = []
        self._spy(monkeypatch, "rmdir", lambda p: p.name.startswith("plan_"), rmdirs)
        runner = _runner(tmp_path)
        runner.update_plan = AsyncMock(side_effect=ValueError("bad plan"))
        resp = await api_taskrunner_from_chat(
            _request(_state(runner), json_body={"steps": [{"title": "first"}]})
        )
        assert resp.status == 400
        assert rmdirs and all(thread != loop_thread for thread in rmdirs)

    @pytest.mark.asyncio
    async def test_cancelled_inline_write_does_not_orphan_spec(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import taskrunner as handlers

        started = threading.Event()
        release = threading.Event()
        original = handlers._write_inline_spec

        def _blocked_write(work_dir: str | Path, content: str) -> Path:
            result = original(work_dir, content)
            started.set()
            assert release.wait(30)
            return result

        monkeypatch.setattr(handlers, "_write_inline_spec", _blocked_write)
        runner = _runner(tmp_path)
        request_task = asyncio.create_task(
            api_taskrunner_start(
                _request(_state(runner), json_body={"spec": "__inline__:# cancel"})
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 30)
            request_task.cancel()
            release.set()

            with pytest.raises(asyncio.CancelledError):
                # flake-ok: the request task was cancelled just above and its stand-in released, so awaiting it collects its CancelledError
                await request_task

            assert list(runner._work_dir.glob("TASK_*.md")) == []
        finally:
            # Always unblock the worker thread and drain the task, so a handshake
            # timeout cannot leave a blocked worker outliving test teardown.
            release.set()
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # flake-ok: teardown drain of the request task, cancelled just above after its worker was released
                await request_task

    @pytest.mark.asyncio
    async def test_cancelled_start_backgrounding_does_not_orphan_spec(self, tmp_path: Path) -> None:
        """A cancel AFTER the shielded write, during ``start_background``, cleans up too."""
        started = asyncio.Event()
        release = asyncio.Event()

        async def _blocked_start(*args: Any, **kwargs: Any) -> str:
            started.set()
            # flake-ok: the stand-in start parks here until the test sets release right after cancelling the request
            await release.wait()
            return "t1"

        runner = _runner(tmp_path)
        runner.start_background = _blocked_start
        request_task = asyncio.create_task(
            api_taskrunner_start(
                _request(_state(runner), json_body={"spec": "__inline__:# cancel late"})
            )
        )
        # flake-ok: set when the request task reaches the stand-in start; pre-existing, moved verbatim
        await started.wait()
        assert list(runner._work_dir.glob("TASK_*.md")) != []  # spec exists mid-flight
        request_task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            # flake-ok: the request task was cancelled just above and its stand-in released, so awaiting it collects its CancelledError
            await request_task

        assert list(runner._work_dir.glob("TASK_*.md")) == []

    @pytest.mark.asyncio
    async def test_cancelled_start_never_unlinks_a_spec_a_registered_run_retains(
        self, tmp_path: Path
    ) -> None:
        """Cancel after ``start_background`` registered the run: the spec survives.

        ``start_background``'s internal rollback covers only its persistence
        hop; a cancel landing after run registration retains the placeholder,
        and deleting the spec it references would corrupt that run.
        """
        started = asyncio.Event()
        release = asyncio.Event()
        runner = _runner(tmp_path)

        async def _registering_start(spec_path: str, **kwargs: Any) -> str:
            runner._runs["t_retained"] = TaskRun(
                spec_path=spec_path, spec_content="# cancel late", task_id="t_retained"
            )
            started.set()
            # flake-ok: the stand-in start parks here until the test sets release right after cancelling the request
            await release.wait()
            return "t_retained"

        runner.start_background = _registering_start
        request_task = asyncio.create_task(
            api_taskrunner_start(
                _request(_state(runner), json_body={"spec": "__inline__:# cancel late"})
            )
        )
        # flake-ok: set when the request task reaches the stand-in start; pre-existing, moved verbatim
        await started.wait()
        request_task.cancel()
        release.set()

        with pytest.raises(asyncio.CancelledError):
            # flake-ok: the request task was cancelled just above and its stand-in released, so awaiting it collects its CancelledError
            await request_task

        # The registered run still owns its spec file — cleanup must not fire.
        assert list(runner._work_dir.glob("TASK_*.md")) != []

    @pytest.mark.asyncio
    async def test_cancelled_plan_claim_does_not_orphan_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from kiro_crew.dashboard.handlers import taskrunner as handlers

        started = threading.Event()
        release = threading.Event()
        original = handlers._make_plan_dir

        def _blocked_claim(work_dir: Path) -> tuple[str, Path]:
            result = original(work_dir)
            started.set()
            assert release.wait(30)
            return result

        monkeypatch.setattr(handlers, "_make_plan_dir", _blocked_claim)
        runner = _runner(tmp_path)
        request_task = asyncio.create_task(
            api_taskrunner_from_chat(
                _request(_state(runner), json_body={"steps": [{"title": "first"}]})
            )
        )
        try:
            assert await asyncio.to_thread(started.wait, 30)
            request_task.cancel()
            release.set()

            with pytest.raises(asyncio.CancelledError):
                # flake-ok: the request task was cancelled just above and its stand-in released, so awaiting it collects its CancelledError
                await request_task

            assert runner._runs == {}
            assert list(runner._work_dir.glob("plan_*")) == []
        finally:
            # Always unblock the worker thread and drain the task, so a handshake
            # timeout cannot leave a blocked worker outliving test teardown.
            release.set()
            request_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                # flake-ok: teardown drain of the request task, cancelled just above after its worker was released
                await request_task
