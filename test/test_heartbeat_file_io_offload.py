"""HEARTBEAT.md's reads belong off the event loop, like its writes already are.

Two sites are pinned.

1. ``_process_heartbeat_file`` opens its lock window with an ``exists()`` +
   ``read_text()`` and closes it with ``asyncio.to_thread(_rewrite_heartbeat_locked,
   ...)``, whose comment already says "the entire durable transaction runs off
   the gateway event-loop thread". Same file, same critical section, opposite
   treatment — and unlike the rewrite, the read runs on EVERY tick for the life
   of the process.
2. ``start()`` seeds the file on the gateway boot path, before KIROCREW_READY.

``workspace_dir()`` is operator-configurable and routinely sits on a synced or
network volume, so neither call is guaranteed to be a RAM-speed syscall. A stall
here freezes the loop the liveness heartbeat is supposed to prove alive — the
self-inflicted version of the watchdog hard-exit in issue #2960.

Asserted on the thread the IO ACTUALLY ran on, matching
``test_heartbeat_sel_prune_offload.py``: an inline call reports ``MainThread``
and fails.
"""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import _HEADER, HeartbeatService


def _spy(monkeypatch: pytest.MonkeyPatch, method: str, target: Path, sink: list[str]) -> None:
    """Record the thread name of a real ``Path`` call against *target*."""
    original = getattr(Path, method)

    def _wrapper(self: Path, *args: object, **kwargs: object) -> object:
        if self == target:
            sink.append(threading.current_thread().name)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, method, _wrapper)


def _service() -> HeartbeatService:
    memory = MagicMock()
    memory.rebuild_index.return_value = 0
    return HeartbeatService(memory=memory, on_task=None)


class TestStartSeedsTheFileOffLoop:
    @pytest.mark.asyncio
    async def test_seed_write_runs_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "ws" / "HEARTBEAT.md"
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        writes: list[str] = []
        _spy(monkeypatch, "write_text", target, writes)
        svc = _service()

        await svc.start()
        svc.stop()

        assert target.read_text(encoding="utf-8") == _HEADER
        assert writes and all(t != "MainThread" for t in writes)

    @pytest.mark.asyncio
    async def test_parent_mkdir_runs_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "ws" / "HEARTBEAT.md"
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        mkdirs: list[str] = []
        _spy(monkeypatch, "mkdir", target.parent, mkdirs)
        svc = _service()

        await svc.start()
        svc.stop()

        assert mkdirs and all(t != "MainThread" for t in mkdirs)

    @pytest.mark.asyncio
    async def test_an_existing_file_is_not_overwritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The seed stays conditional — moving it to a worker must not turn
        start() into a truncation of the operator's task list."""
        target = tmp_path / "ws" / "HEARTBEAT.md"
        target.parent.mkdir(parents=True)
        target.write_text("# Heartbeat Tasks\n\n- keep me\n", encoding="utf-8")
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        svc = _service()

        await svc.start()
        svc.stop()

        assert "- keep me" in target.read_text(encoding="utf-8")


class TestTickReadsTheFileOffLoop:
    @pytest.mark.asyncio
    async def test_read_runs_off_the_event_loop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        target = tmp_path / "HEARTBEAT.md"
        target.write_text(f"{_HEADER}- do a thing\n", encoding="utf-8")
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        reads: list[str] = []
        _spy(monkeypatch, "read_text", target, reads)
        seen: list[str] = []

        async def _on_task(text: str, _deliver: str) -> str:
            seen.append(text)
            return "done"

        svc = HeartbeatService(memory=MagicMock(), on_task=_on_task)

        await svc._process_heartbeat_file()

        assert seen == ["do a thing"]
        assert reads and all(t != "MainThread" for t in reads)

    @pytest.mark.asyncio
    async def test_missing_file_is_a_no_op_and_does_not_raise(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The existence check moved INTO the worker, so a file that never
        existed — or that vanished between the check and the read — must still
        return quietly rather than surfacing FileNotFoundError."""
        target = tmp_path / "gone" / "HEARTBEAT.md"
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        on_task = MagicMock()
        svc = HeartbeatService(memory=MagicMock(), on_task=on_task)

        await svc._process_heartbeat_file()

        on_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_read_happens_inside_the_file_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Offloading must not move the read outside the lock the rewrite
        relies on — otherwise two cycles could rewrite from stale snapshots."""
        target = tmp_path / "HEARTBEAT.md"
        target.write_text(_HEADER, encoding="utf-8")
        monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: target)
        held: list[bool] = []
        _spy(monkeypatch, "read_text", target, [])
        original = Path.read_text
        svc = HeartbeatService(memory=MagicMock(), on_task=None)

        def _wrapper(self: Path, *args: object, **kwargs: object) -> object:
            if self == target:
                held.append(svc._file_lock.locked())
            return original(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", _wrapper)

        await svc._process_heartbeat_file()

        assert held == [True]
