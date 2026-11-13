"""Both async transcript deletions run off the event loop and finish when cancelled.

``AcpProvider.cleanup_session`` (``providers.acp_lifecycle``) and
``AcpSessionHandle.destroy`` (``acp.session_handle_lifecycle``) unlink kiro-cli's
``<sid>.json`` + ``.jsonl``. On a slow home mount an unlink on the loop thread stalls
every session the gateway serves. Teardown is also where cancellations land, and a
transcript whose delete is dropped is never deleted by anything else. Each test drives
the real code against real files and observes the real ``Path.unlink``.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.providers import acp_lifecycle
from kiro_crew.testing.wait import await_until, default_timeout


async def _cleanup_session(sid: str) -> None:
    await acp_lifecycle.cleanup_session(
        SimpleNamespace(is_claude_backend=False, is_codex_backend=False), sid
    )


async def _destroy(sid: str) -> None:
    runtime, _, _ = _make_runtime()
    queues = _register(runtime, sid)
    runtime._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    await AcpSessionHandle(sid, queues[sid], runtime).destroy()


_DELETES = [
    pytest.param(_cleanup_session, id="cleanup_session"),
    pytest.param(_destroy, id="destroy"),
]


@pytest.fixture()
def transcript(tmp_path, monkeypatch) -> tuple[str, list[Path], list[int]]:
    """The transcript pair, and the threads that looked its sessions dir up."""
    sessions = tmp_path / "sessions" / "cli"
    sessions.mkdir(parents=True)
    lookups: list[int] = []

    def _sessions_dir() -> Path:
        lookups.append(threading.get_ident())
        return sessions

    # Through each facade: its mirror forwards the patch to the owner that reads it.
    monkeypatch.setattr("kiro_crew.providers.acp.kiro_sessions_dir", _sessions_dir)
    monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", _sessions_dir)
    sid = "sid-delete"
    files = [sessions / f"{sid}.json", sessions / f"{sid}.jsonl"]
    for file in files:
        file.write_text("{}", encoding="utf-8")
    return sid, files, lookups


@pytest.mark.asyncio
@pytest.mark.parametrize("delete", _DELETES)
async def test_the_delete_runs_off_the_event_loop(delete, transcript, monkeypatch):
    sid, files, threads = transcript
    names = {file.name for file in files}
    real_unlink = Path.unlink

    def _recording_unlink(self: Path, *args, **kwargs):
        if self.name in names:
            threads.append(threading.get_ident())
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _recording_unlink)

    await delete(sid)

    assert len(threads) == 3  # the sessions-dir lookup, then both unlinks
    assert threading.get_ident() not in threads
    assert not any(file.exists() for file in files)


@pytest.mark.asyncio
@pytest.mark.parametrize("delete", _DELETES)
async def test_a_cancelled_caller_unwinds_only_once_the_delete_finished(
    delete, transcript, monkeypatch
):
    sid, files, _ = transcript
    names = {file.name for file in files}
    loop = asyncio.get_running_loop()
    entered, release = asyncio.Event(), threading.Event()
    calls: list[Path] = []
    real_unlink = Path.unlink

    def _gated_unlink(self: Path, *args, **kwargs):
        if self.name not in names:
            return real_unlink(self, *args, **kwargs)
        calls.append(self)
        if len(calls) == 1:
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(default_timeout()), "the gated unlink was never released"
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _gated_unlink)
    task = asyncio.ensure_future(delete(sid))
    done_at_release: list[bool] = []

    def _release() -> None:
        done_at_release.append(task.done())
        release.set()

    try:
        await await_until(entered, what="the delete reached its first unlink")
        task.cancel()
        # Queued behind the cancellation's delivery to the task: a caller that
        # unwound on it, abandoning the delete in flight, is already done here.
        loop.call_soon(_release)
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    assert done_at_release == [False]
    assert not any(file.exists() for file in files)


@pytest.mark.asyncio
async def test_a_cancel_during_the_delete_still_reclaims_the_work_dir():
    reclaimed: list[bool] = []

    async def _cancelled_cleanup(_sid: str) -> None:
        raise asyncio.CancelledError

    async def _reclaim() -> None:
        reclaimed.append(True)

    provider = SimpleNamespace(
        session_id="sid-delete",
        _client=SimpleNamespace(shutdown=AsyncMock()),
        memory_mode="ephemeral",
        cleanup_session=_cancelled_cleanup,
        _disposable_work_dir=True,
        _reclaim_work_dir=_reclaim,
    )

    with pytest.raises(asyncio.CancelledError):
        await acp_lifecycle.shutdown(provider)  # type: ignore[arg-type]

    assert reclaimed == [True]
