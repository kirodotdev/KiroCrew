"""A cancelled voice model download does not leave its status stuck at downloading.

The download itself is pinned in ``test_stt_engine_download.py``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.stt import models


@pytest.mark.asyncio
async def test_a_cancelled_download_ends_failed_and_stays_failed(monkeypatch) -> None:
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()  # set from the worker thread, awaited on the loop
    release = threading.Event()
    report: list = []

    def _blocking_download(_model, on_progress):
        report.append(on_progress)
        on_progress(10, 100)
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=30)
        on_progress(20, 100)  # the thread outlives the cancelled caller
        raise RuntimeError("stopped by the test")

    store = models.ModelStore()

    async def _absent(_model):
        return None, False

    monkeypatch.setattr(store, "_accept_existing_or_refuse", _absent)
    monkeypatch.setattr(models, "_download_blocking", _blocking_download)
    model = SimpleNamespace(name="base", size_bytes=100)

    task = asyncio.create_task(store.ensure(model))
    await asyncio.wait_for(started.wait(), 30)
    assert store.status["step"] == "downloading"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    try:
        assert store.status["step"] == "failed"
        assert store.status["error"] == "download cancelled"
    finally:
        release.set()

    # Late progress from the still-running thread must not bring "downloading" back.
    report[0](30, 100)
    assert store.status["step"] == "failed"
