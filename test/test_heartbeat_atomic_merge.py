"""Regression test for heartbeat mid-cycle append preservation (Track A, bug 3).

The heartbeat cycle reads HEARTBEAT.md, runs tasks, then rewrites the file. A
task appended by another session/agent *while the cycle is running* must not be
clobbered by the stale in-memory snapshot — the rewrite re-reads the file under
the lock and merges any new entries.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import kiro_crew.heartbeat as hb_mod
from kiro_crew.heartbeat import _HEADER, HeartbeatService


@pytest.mark.asyncio
async def test_midcycle_append_preserved(tmp_path: Path) -> None:
    hb_path = tmp_path / "HEARTBEAT.md"
    hb_path.write_text(_HEADER + "- original task\n", encoding="utf-8")

    async def on_task(text: str, deliver: str) -> str:
        # Simulate another session appending a new task mid-cycle (lock-free
        # append directly to the file), then complete the original task.
        with open(hb_path, "a", encoding="utf-8") as f:
            f.write("- newly appended task\n")
        return "All done!"  # no HEARTBEAT_KEEP → original should be removed

    svc = HeartbeatService(memory=MagicMock(), on_task=on_task)

    original = hb_mod.heartbeat_path
    hb_mod.heartbeat_path = lambda: hb_path
    try:
        await svc._process_heartbeat_file()
    finally:
        hb_mod.heartbeat_path = original

    content = hb_path.read_text(encoding="utf-8")
    # Completed task is dropped, but the mid-cycle append is preserved.
    assert "original task" not in content
    assert "newly appended task" in content
    # File remains well-formed (header intact, no temp artifacts left behind).
    assert content.startswith("# Heartbeat Tasks")
    assert list(tmp_path.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_midcycle_append_preserved_alongside_kept(tmp_path: Path) -> None:
    hb_path = tmp_path / "HEARTBEAT.md"
    hb_path.write_text(_HEADER + "- keep me\n", encoding="utf-8")

    async def on_task(text: str, deliver: str) -> str:
        with open(hb_path, "a", encoding="utf-8") as f:
            f.write("- appended mid cycle\n")
        return "still working. HEARTBEAT_KEEP"  # original retained

    svc = HeartbeatService(memory=MagicMock(), on_task=on_task)

    original = hb_mod.heartbeat_path
    hb_mod.heartbeat_path = lambda: hb_path
    try:
        await svc._process_heartbeat_file()
    finally:
        hb_mod.heartbeat_path = original

    content = hb_path.read_text(encoding="utf-8")
    assert "keep me" in content  # incomplete original retained
    assert "appended mid cycle" in content  # mid-cycle append preserved


@pytest.mark.asyncio
async def test_append_serialized_with_final_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A writer using the shared lock appends only after replace completes."""
    hb_path = tmp_path / "HEARTBEAT.md"
    hb_path.write_text(_HEADER + "- completed task\n", encoding="utf-8")

    rewrite_started = threading.Event()
    allow_replace = threading.Event()
    real_atomic_write = hb_mod.atomic_write

    def paused_atomic_write(path, content, *, fsync=False) -> None:
        rewrite_started.set()
        assert allow_replace.wait(timeout=5)
        real_atomic_write(path, content, fsync=fsync)

    monkeypatch.setattr(hb_mod, "atomic_write", paused_atomic_write)

    async def on_task(text: str, deliver: str) -> str:
        return "done"

    svc = HeartbeatService(memory=MagicMock(), on_task=on_task)
    monkeypatch.setattr(hb_mod, "heartbeat_path", lambda: hb_path)

    cycle = asyncio.create_task(svc._process_heartbeat_file())
    assert await asyncio.to_thread(rewrite_started.wait, 5)

    append = asyncio.create_task(
        asyncio.to_thread(hb_mod.append_heartbeat_task, "- contended append", hb_path)
    )
    await asyncio.sleep(0.05)
    assert not append.done()

    allow_replace.set()
    await asyncio.gather(cycle, append)

    content = hb_path.read_text(encoding="utf-8")
    assert "completed task" not in content
    assert "contended append" in content
