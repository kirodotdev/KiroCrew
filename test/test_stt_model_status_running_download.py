"""A check of another model does not overwrite a running download's status.

``ensure`` checks the custom model, and a catalog model already on disk, before it
takes the download lock. A running download owns the shown status until it ends, so
the settings page keeps its progress instead of showing that check's result, and
each model's own last result is readable through ``status_for``.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from kiro_crew.stt import models


@pytest.mark.asyncio
async def test_a_custom_check_keeps_a_running_download_status(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv(models.SKIP_DOWNLOAD_ENV, raising=False)
    root = tmp_path / "home" / "models" / "whisper"
    root.mkdir(parents=True)
    monkeypatch.setattr(models, "models_dir", lambda: root)
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    release = threading.Event()

    def _blocking_download(model, on_progress, on_complete=None):
        on_progress(1000, 5000)
        loop.call_soon_threadsafe(started.set)
        release.wait(timeout=30)
        if on_complete is not None:
            on_complete()
        return root / "ggml-tiny.bin"

    store = models.ModelStore()

    async def _absent(_model):
        return None, False

    monkeypatch.setattr(store, "_accept_existing_or_refuse", _absent)
    # A finished download starts the ffmpeg decoder fetch; this test never reaches
    # the network, so that background fetch is not started.
    monkeypatch.setattr(store, "_start_decoder_autofetch", lambda: None)
    monkeypatch.setattr(models, "_download_blocking", _blocking_download)
    tiny = SimpleNamespace(name="tiny", size_bytes=5000)
    custom = models.custom_model("ggml-missing.bin", "0" * 64)
    assert custom is not None

    task = asyncio.create_task(store.ensure(tiny))
    try:
        await asyncio.wait_for(started.wait(), 30)
        assert await store.ensure(custom) is None

        # The shown status is still the running download, progress included.
        assert store.status["step"] == "downloading"
        assert store.status["model"] == "tiny"
        assert store.status["downloaded_bytes"] == 1000
        # The custom check's own result is kept, not dropped.
        assert store.status_for(models.CUSTOM_MODEL)["step"] == "failed"
        assert store.status_for("tiny")["step"] == "downloading"
    finally:
        release.set()
    await task

    assert store.status["step"] == "ready"
    assert store.status["model"] == "tiny"
    # Once nothing is downloading, any model's result is shown again.
    assert await store.ensure(custom) is None
    assert store.status["model"] == models.CUSTOM_MODEL
    assert store.status["step"] == "failed"


def test_status_for_a_model_never_checked_is_empty() -> None:
    assert models.ModelStore().status_for("base") == {}
