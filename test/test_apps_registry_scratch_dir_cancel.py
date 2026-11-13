"""A cancel during a throwaway clone's scratch-dir hop leaves no dir behind.

The external index fetch, the manifest fetch and the blob proxy's clone each make
their scratch dir in a worker thread, so a cancel (a fetch budget, a gateway
shutdown) can land while that thread is still making it. The dir the thread then
makes must still be removed, however many cancels land.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import tempfile
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from kiro_crew.apps import registry, routes
from kiro_crew.testing.wait import await_until, default_timeout

_REPO = "https://github.com/example/CoolApp.git"

_SITES = [
    pytest.param(
        "kirocrew-registry-",
        lambda tmp: registry._fetch_external_registry_index(_REPO, "main"),
        id="registry-index",
    ),
    pytest.param(
        "kirocrew-manifest-",
        lambda tmp: registry._fetch_app_manifest(_REPO, "main", git_url=_REPO),
        id="app-manifest",
    ),
    pytest.param(
        "kirocrew-blob-",
        lambda tmp: routes._fetch_git_blob(
            _REPO, "main", "icon.png", tmp / "icon.png", git_url=_REPO
        ),
        id="blob-proxy",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix, fetch", _SITES)
async def test_cancels_during_the_scratch_mkdtemp_still_remove_the_dir(
    monkeypatch, tmp_path: Path, prefix: str, fetch: Callable[[Path], Awaitable[object]]
) -> None:
    """Two cancels land while the mkdtemp thread is held, and the thread makes the
    dir only after both were delivered. Each step is queued on the loop behind the
    one before it, so the order is fixed rather than raced against a timer."""
    loop = asyncio.get_running_loop()
    ceiling = default_timeout()
    entered = asyncio.Event()
    finished = asyncio.Event()
    release = threading.Event()
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _held_mkdtemp(*args, **kwargs):
        if kwargs.get("prefix") != prefix:
            return real_mkdtemp(*args, **kwargs)
        try:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=ceiling), "never released"
            path = real_mkdtemp(*args, **kwargs)
            made.append(path)
            return path
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(tempfile, "mkdtemp", _held_mkdtemp)
    # The clone-host gate runs before the hop; the SEL record is not under test.
    monkeypatch.setattr(registry, "is_clone_host_trusted", lambda url: True)
    monkeypatch.setattr(registry, "_sel_fn", None)

    def _second_cancel_then_release() -> None:
        # Runs after the first cancel was delivered; the release is queued
        # behind the second one, so the thread returns only after both.
        task.cancel()
        loop.call_soon(release.set)

    task = asyncio.ensure_future(fetch(tmp_path))
    try:
        try:
            await await_until(entered, what="the scratch mkdtemp thread")
        finally:
            task.cancel()
            loop.call_soon(_second_cancel_then_release)
        with pytest.raises(asyncio.CancelledError):
            await await_until(task, what="the cancelled fetch")
        # Without the settle the thread outlives the task; wait for it to return.
        await await_until(finished, what="the scratch mkdtemp thread's return")

        assert made, "the mkdtemp thread never made the scratch dir"
        leaked = [p for p in made if os.path.exists(p)]
        assert not leaked, f"a cancel during mkdtemp leaked the scratch dir: {leaked}"
    finally:
        release.set()
        for path in made:
            with contextlib.suppress(OSError):
                os.rmdir(path)
