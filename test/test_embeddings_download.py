"""``ModelDownloadManager.ensure_model`` checks the model file off the event loop.

``ensure_model`` runs at gateway start and on every dashboard Enable/Retry
click. Its readiness checks stat the model file (``model_file_present``) and
resolve the configured custom model (``embedding_model_is_custom``); on a slow
or network-mounted data home those must not stall the gateway's one loop.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import kiro_crew.embeddings_download as download_mod
from kiro_crew.embeddings_download import ModelDownloadManager


def _spy(monkeypatch, attr, answers=None):
    """Replace ``embeddings_download.<attr>`` with a thread-recording wrapper.

    With ``answers`` the spy returns them in order; without, it calls the real
    function.
    """
    real = getattr(download_mod, attr)
    seen: list[int] = []
    queue = list(answers) if answers is not None else None

    def _wrapper(*args, **kwargs):
        seen.append(threading.get_ident())
        if queue is None:
            return real(*args, **kwargs)
        return queue.pop(0)

    monkeypatch.setattr(download_mod, attr, _wrapper)
    return seen


def _write_model(path: Path) -> Path:
    """Write a sparse stand-in model big enough to pass the placeholder check."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as fh:
        fh.truncate(download_mod._GGUF_MIN_BYTES + 1)
    return path


@pytest.mark.asyncio
async def test_present_model_is_checked_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """A model already in place is stat-ed on a worker thread and reported ready."""
    target = _write_model(tmp_path / "models" / "default.gguf")
    seen = _spy(monkeypatch, "model_file_present")
    loop_thread = threading.get_ident()

    mgr = ModelDownloadManager(target=target)
    assert await mgr.ensure_model(attempts=1) is True
    assert mgr.status["step"] == "ready"

    assert seen, "ensure_model never checked the model file"
    assert all(t != loop_thread for t in seen)


@pytest.mark.asyncio
async def test_custom_model_is_resolved_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The custom-model check (exists + is_file + stat) runs on a worker thread."""
    seen_ready = _spy(monkeypatch, "model_file_present")
    seen_custom = _spy(monkeypatch, "embedding_model_is_custom", answers=[True])
    loop_thread = threading.get_ident()

    mgr = ModelDownloadManager(target=tmp_path / "models" / "default.gguf")
    assert await mgr.ensure_model(attempts=1) is False
    assert mgr.status["step"] == "custom_model"

    assert seen_ready and seen_custom
    assert all(t != loop_thread for t in seen_ready + seen_custom)


@pytest.mark.asyncio
async def test_recheck_under_the_lock_runs_off_the_event_loop(tmp_path: Path, monkeypatch) -> None:
    """The re-check after taking the lock also stats the file on a worker thread.

    The first check misses and the second finds the file, as when a concurrent
    caller finished the download: no download starts and the model is ready.
    """
    monkeypatch.delenv("KIROCREW_SKIP_MODEL_DOWNLOAD", raising=False)
    seen_ready = _spy(monkeypatch, "model_file_present", answers=[False, True])
    seen_custom = _spy(monkeypatch, "embedding_model_is_custom", answers=[False])
    loop_thread = threading.get_ident()

    mgr = ModelDownloadManager(target=tmp_path / "models" / "default.gguf")

    def _no_download() -> tuple[bool, str]:
        raise AssertionError("the re-check found the model; no download may start")

    monkeypatch.setattr(mgr, "_download_once", _no_download)
    assert await mgr.ensure_model(attempts=1) is True
    assert mgr.status["step"] == "ready"

    assert len(seen_ready) == 2 and len(seen_custom) == 1
    assert all(t != loop_thread for t in seen_ready + seen_custom)
