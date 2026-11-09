"""The update fetch never leaves its throwaway git dir behind.

The dir sits under the crew scratch root with no owner record, and the scratch
sweep never deletes such a dir, so one left here stays for good. It must go on
an error after it is made, and on a cancel (gateway shutdown) that lands while
the off-loop prepare step is still building it, however often that cancel
repeats, without the cancel waiting on the worker.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest

from kiro_crew.platform import update_fetch
from kiro_crew.testing.wait import async_wait_until, await_until, default_timeout


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch root, and git probes that let the real prepare step run."""
    from kiro_crew import agent_scratch

    scratch_root = tmp_path / "scratch-root"
    objects = tmp_path / "objects"
    objects.mkdir()

    def _git(_proj: str, *args: str) -> str:
        if "objects" in args:
            return str(objects)
        if "--show-object-format" in args:
            return "sha1"
        return ""

    monkeypatch.setattr(agent_scratch, "scratch_root", lambda: scratch_root)
    monkeypatch.setattr(update_fetch, "resolve_remote_url", lambda *_a, **_k: "file:///upstream")
    monkeypatch.setattr(update_fetch, "_local_fetch_url", lambda _p, url: url)
    monkeypatch.setattr(update_fetch, "_git", _git)
    return scratch_root


def _left(root: Path) -> list[Path]:
    return list(root.iterdir()) if root.exists() else []


def test_an_error_after_the_dir_is_made_removes_it(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _broken_listing(_proj: str) -> list[tuple[str, str]]:
        raise OSError("disk went away")

    monkeypatch.setattr(update_fetch, "_scratch_http_auth_pairs", _broken_listing)
    with pytest.raises(OSError):
        update_fetch._prepare_isolated_fetch(str(tmp_path), "origin", "main")
    assert _left(root) == []


@pytest.mark.asyncio
async def test_a_cancel_during_prepare_removes_the_dir_it_built(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = threading.Event()
    release = threading.Event()

    def _held_listing(_proj: str) -> list[tuple[str, str]]:
        started.set()
        release.wait(default_timeout())  # held on the worker thread, released below
        return []

    async def _no_spawn(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a cancelled fetch must not start git")

    monkeypatch.setattr(update_fetch, "_scratch_http_auth_pairs", _held_listing)
    monkeypatch.setattr(update_fetch.asyncio, "create_subprocess_exec", _no_spawn)

    task = asyncio.create_task(
        update_fetch.fetch_and_pin_upstream(str(tmp_path), "origin", "main", git="git", env={})
    )
    try:
        await async_wait_until(started.is_set)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled fetch")  # no wait on the worker
    finally:
        release.set()
    await async_wait_until(lambda: _left(root) == [], off_loop=True)


@pytest.mark.asyncio
async def test_a_cancel_after_the_worker_handed_the_dir_over_removes_it(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(update_fetch, "_scratch_http_auth_pairs", lambda _p: [])
    handoff = update_fetch._ScratchHandoff()
    prepared = await asyncio.to_thread(
        update_fetch._prepare_isolated_fetch, str(tmp_path), "origin", "main", handoff
    )
    assert prepared is not None and handoff.scratch == prepared[2]
    update_fetch._abandon(handoff)
    await async_wait_until(lambda: _left(root) == [], off_loop=True)
