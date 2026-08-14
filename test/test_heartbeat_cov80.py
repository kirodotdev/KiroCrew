"""``heartbeat`` — the loop/tick scaffolding the behavioural suites never enter.

The existing ``test_heartbeat_*`` files all drive ``_process_heartbeat_file``
directly, so everything around it is unexercised. What that leaves uncovered is
exactly the part that decides *whether* maintenance ever runs:

* ``start`` seeding HEARTBEAT.md and publishing the loop task, and ``stop``
  cancelling it (a leaked task keeps ticking after shutdown);
* ``_loop``'s two exits — the shutdown wait returning (clean stop) vs. timing
  out (normal wake-up) — and its swallow-everything guard, since a raise there
  would kill the heartbeat subsystem for the life of the process;
* ``_beat``'s tick arithmetic: FTS rebuild only on the 15th tick, history
  prune only on the 1440th, and the SEL prune whose failure must stay
  best-effort;
* the early ``return`` when HEARTBEAT.md does not exist, and the header-seeding
  branch in ``append_heartbeat_task``;
* ``_extract_tasks``'s multi-line HTML comment state machine, which silently
  turns comment prose into agent tasks if it regresses.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from kiro_crew.heartbeat import (
    _HEADER,
    HeartbeatService,
    _extract_tasks,
    append_heartbeat_task,
    heartbeat_lock_path,
    heartbeat_path,
    is_keep_response,
    strip_keep_sentinel,
)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``heartbeat_path()`` at a tmp workspace."""
    monkeypatch.setattr("kiro_crew.heartbeat.workspace_dir", lambda: tmp_path / "ws")
    return tmp_path / "ws"


def _memory() -> Any:
    """A MemoryStore stub whose maintenance calls are all observable."""
    mem = MagicMock()
    mem.rebuild_index.return_value = 7
    return mem


class _ImmediateExecutor:
    """Runs ``run_in_executor`` work inline so no thread pool is needed."""

    def submit(self, fn, *args, **kwargs):  # pragma: no cover - unused shape
        raise AssertionError("submit should not be called")


@pytest.fixture()
def inline_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``loop.run_in_executor`` synchronous by handing back the real loop's default."""
    monkeypatch.setattr("kiro_crew.heartbeat.maintenance_executor", lambda: None)


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_seeds_file_and_publishes_task(self, workspace: Path) -> None:
        svc = HeartbeatService(_memory(), interval=3600)
        await svc.start()
        try:
            assert heartbeat_path().read_text(encoding="utf-8") == _HEADER
            assert svc._task is not None
            assert not svc._task.done()
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_start_keeps_existing_file_contents(self, workspace: Path) -> None:
        workspace.mkdir(parents=True)
        heartbeat_path().write_text("# Heartbeat Tasks\n\n- [ ] zibble\n", encoding="utf-8")
        svc = HeartbeatService(_memory(), interval=3600)
        await svc.start()
        try:
            assert "zibble" in heartbeat_path().read_text(encoding="utf-8")
        finally:
            svc.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_and_clears_the_task(self, workspace: Path) -> None:
        svc = HeartbeatService(_memory(), interval=3600)
        await svc.start()
        task = svc._task
        svc.stop()
        assert svc._task is None
        assert task is not None
        with pytest.raises(asyncio.CancelledError):
            await task

    def test_stop_without_start_is_a_no_op(self) -> None:
        svc = HeartbeatService(_memory())
        svc.stop()  # must not raise
        assert svc._task is None


class TestLoop:
    @pytest.mark.asyncio
    async def test_loop_returns_when_shutdown_is_already_set(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A set shutdown event ends the loop without ever beating."""
        event = asyncio.Event()
        event.set()
        monkeypatch.setattr("kiro_crew.heartbeat.shutdown_event", event)
        svc = HeartbeatService(_memory(), interval=3600)
        beats: list[int] = []
        monkeypatch.setattr(svc, "_beat", lambda: beats.append(1))  # never awaited
        await svc._loop()
        assert beats == []

    @pytest.mark.asyncio
    async def test_loop_returns_when_shutdown_fires_during_the_wait(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Shutdown arriving mid-wait exits immediately — it does not wait out the interval."""
        event = asyncio.Event()
        monkeypatch.setattr("kiro_crew.heartbeat.shutdown_event", event)
        svc = HeartbeatService(_memory(), interval=3600)
        beats: list[int] = []
        monkeypatch.setattr(svc, "_beat", lambda: beats.append(1))  # never awaited

        loop_task = asyncio.create_task(svc._loop())
        await asyncio.sleep(0)  # let the loop reach the shutdown wait
        event.set()
        await asyncio.wait_for(loop_task, timeout=5)
        assert beats == []
        assert svc._tick == 0

    @pytest.mark.asyncio
    async def test_loop_beats_on_wakeup_then_exits_on_shutdown(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wait timing out is the normal wake-up: tick increments, _beat runs."""
        event = asyncio.Event()
        monkeypatch.setattr("kiro_crew.heartbeat.shutdown_event", event)
        svc = HeartbeatService(_memory(), interval=0.001)  # type: ignore[arg-type]
        beats: list[int] = []

        async def _beat() -> None:
            beats.append(svc._tick)
            event.set()

        monkeypatch.setattr(svc, "_beat", _beat)
        await asyncio.wait_for(svc._loop(), timeout=5)
        assert beats == [1]

    @pytest.mark.asyncio
    async def test_loop_swallows_a_failing_beat(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A raising _beat must not kill the loop — it keeps ticking."""
        event = asyncio.Event()
        monkeypatch.setattr("kiro_crew.heartbeat.shutdown_event", event)
        svc = HeartbeatService(_memory(), interval=0.001)  # type: ignore[arg-type]
        calls: list[int] = []

        async def _beat() -> None:
            calls.append(svc._tick)
            if len(calls) >= 2:
                event.set()
            raise RuntimeError("zibble")

        monkeypatch.setattr(svc, "_beat", _beat)
        await asyncio.wait_for(svc._loop(), timeout=5)
        assert calls == [1, 2]


class TestBeat:
    @pytest.mark.asyncio
    async def test_ordinary_tick_only_processes_the_file(
        self, workspace: Path, inline_executor: None
    ) -> None:
        mem = _memory()
        svc = HeartbeatService(mem)
        svc._tick = 1
        await svc._beat()
        mem.rebuild_index.assert_not_called()
        mem.prune_history.assert_not_called()

    @pytest.mark.asyncio
    async def test_fifteenth_tick_rebuilds_the_fts_index(
        self, workspace: Path, inline_executor: None
    ) -> None:
        mem = _memory()
        svc = HeartbeatService(mem)
        svc._tick = 15
        await svc._beat()
        mem.rebuild_index.assert_called_once_with()
        mem.prune_history.assert_not_called()

    @pytest.mark.asyncio
    async def test_daily_tick_prunes_history_with_configured_retention(
        self,
        workspace: Path,
        inline_executor: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cfg = MagicMock()
        cfg.memory.history_max_days = 11
        monkeypatch.setattr("kiro_crew.heartbeat.KiroCrewConfig.load", staticmethod(lambda: cfg))
        sel_obj = MagicMock()
        monkeypatch.setattr("kiro_crew.heartbeat.sel", lambda: sel_obj)

        mem = _memory()
        svc = HeartbeatService(mem)
        svc._tick = 1440
        await svc._beat()

        mem.prune_history.assert_called_once_with(keep_days=11)
        sel_obj.prune.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_sel_prune_failure_is_best_effort(
        self,
        workspace: Path,
        inline_executor: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A raising SEL prune must not abort the beat, so consolidation still runs."""
        cfg = MagicMock()
        cfg.memory.history_max_days = 3
        monkeypatch.setattr("kiro_crew.heartbeat.KiroCrewConfig.load", staticmethod(lambda: cfg))
        sel_obj = MagicMock()
        sel_obj.prune.side_effect = RuntimeError("zibble")
        monkeypatch.setattr("kiro_crew.heartbeat.sel", lambda: sel_obj)

        consolidator = MagicMock()
        svc = HeartbeatService(_memory(), consolidator=consolidator)
        svc._tick = 1440
        await svc._beat()

        assert consolidator.check_idle_sessions.call_count == 1

    @pytest.mark.asyncio
    async def test_beat_skips_file_processing_while_already_processing(
        self, workspace: Path, inline_executor: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        svc = HeartbeatService(_memory())
        svc._processing = True
        calls: list[int] = []

        async def _process() -> None:
            calls.append(1)

        monkeypatch.setattr(svc, "_process_heartbeat_file", _process)
        svc._tick = 1
        await svc._beat()
        assert calls == []


class TestProcessFileGuards:
    @pytest.mark.asyncio
    async def test_missing_file_returns_without_calling_back(self, workspace: Path) -> None:
        called: list[str] = []

        async def _on_task(text: str, deliver: str) -> str | None:
            called.append(text)
            return None

        svc = HeartbeatService(_memory(), on_task=_on_task)
        await svc._process_heartbeat_file()
        assert called == []
        assert not heartbeat_path().exists()


class TestAppendHeartbeatTask:
    def test_seeds_the_header_when_the_file_is_absent(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "HEARTBEAT.md"
        append_heartbeat_task("- [ ] zibble", path=target)
        text = target.read_text(encoding="utf-8")
        assert text.startswith(_HEADER)
        assert text.endswith("- [ ] zibble\n")
        assert heartbeat_lock_path(target).exists()

    def test_appends_to_an_existing_file_without_reseeding(self, tmp_path: Path) -> None:
        target = tmp_path / "HEARTBEAT.md"
        target.write_text(_HEADER + "- [ ] first\n", encoding="utf-8")
        append_heartbeat_task("- [ ] second\n\n", path=target)
        assert target.read_text(encoding="utf-8").count(_HEADER) == 1
        assert _extract_tasks(target.read_text(encoding="utf-8")) == [
            ("first", ""),
            ("second", ""),
        ]


class TestExtractTasks:
    def test_multiline_comment_block_is_not_a_task(self) -> None:
        content = (
            "# Heartbeat Tasks\n"
            "<!-- opening line\n"
            "   still inside the comment\n"
            "-->\n"
            "- [ ] real task  <!-- deliver:slack -->\n"
            "<!-- one-line comment -->\n"
            "\n"
            "-\n"
            "* starred task\n"
        )
        assert _extract_tasks(content) == [
            ("real task", "slack"),
            ("starred task", ""),
        ]

    def test_keep_sentinel_helpers(self) -> None:
        assert is_keep_response("all done heartbeat_keep") is True
        assert is_keep_response("all done") is False
        assert is_keep_response(None) is False
        assert strip_keep_sentinel("  work left HEARTBEAT_KEEP ") == "work left"
