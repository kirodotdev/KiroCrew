"""The two remaining async transcript deletions must run their file I/O off the loop.

``AcpProvider.cleanup_session`` (bound from ``providers.acp_lifecycle``) and
``AcpSessionHandle.destroy`` (whose ``finally`` deletes the transcript, via
``providers`` -> ``acp.session_handle_lifecycle``) both resolve the sessions dir
and unlink the ``.json``/``.jsonl`` files. On a slow mount a synchronous unlink on
the event-loop thread stalls every other session the shared loop serves and can
trip the gateway watchdog. Each caller must hop the blocking filesystem work onto
a worker thread with ``asyncio.to_thread`` -- the same hop
``providers.acp_runtime_start`` already does for the late-session teardown.

Both tests record, from inside the real unlink, whether a running event loop is
present: on the loop thread one is, on a ``to_thread`` worker none is. They fail
on an implementation that unlinks on the loop and pass once the hop is added.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from acp_runtime_helpers import _make_runtime, _register

from kiro_crew.acp.runtime import AcpSessionHandle
from kiro_crew.providers import acp_lifecycle
from kiro_crew.testing.wait import await_until


def _loop_running() -> bool:
    """True when called on a thread that is running an asyncio event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


@pytest.fixture()
def sessions_dir(tmp_path, monkeypatch):
    """Point the native sessions dir (read by both code paths) at a temp dir."""
    d = tmp_path / "sessions" / "cli"
    d.mkdir(parents=True)
    # ``kiro_sessions_dir`` is a name the acp_lifecycle and session_handle_lifecycle
    # owners share within their mirrored families, so patch it THROUGH each
    # facade (not on an owner): the mirror forwards the patch to the owner's
    # reader, and the facade-mirror guard forbids patching a shared name on a
    # single owner because the facade's undo would restore every holder to that
    # owner's value.
    monkeypatch.setattr("kiro_crew.providers.acp.kiro_sessions_dir", lambda: d)
    monkeypatch.setattr("kiro_crew.acp.session_handle.kiro_sessions_dir", lambda: d)
    return d


def _instrument_unlink(monkeypatch, record: list[bool]) -> None:
    """Record, at each real ``Path.unlink``, whether a loop is running here."""
    real_unlink = Path.unlink

    def _recording_unlink(self: Path, *args, **kwargs):
        record.append(_loop_running())
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _recording_unlink)


@pytest.mark.asyncio
async def test_cleanup_session_unlinks_off_the_loop(sessions_dir, monkeypatch):
    sid = "sess-cleanup"
    (sessions_dir / f"{sid}.json").write_text("{}")
    (sessions_dir / f"{sid}.jsonl").write_text("")
    where: list[bool] = []
    _instrument_unlink(monkeypatch, where)

    await acp_lifecycle.cleanup_session(SimpleNamespace(), sid)

    assert where, "cleanup_session never attempted to unlink the transcript files"
    assert not any(where), (
        "cleanup_session unlinked on the event-loop thread "
        f"(loop-running at unlink: {where}); it must run the deletion via "
        "asyncio.to_thread"
    )
    assert not (sessions_dir / f"{sid}.json").exists()
    assert not (sessions_dir / f"{sid}.jsonl").exists()


@pytest.mark.asyncio
async def test_destroy_unlinks_transcript_off_the_loop(sessions_dir, monkeypatch):
    sid = "sA"
    (sessions_dir / f"{sid}.json").write_text("{}")
    (sessions_dir / f"{sid}.jsonl").write_text("")
    where: list[bool] = []
    _instrument_unlink(monkeypatch, where)

    rt, _, _ = _make_runtime()
    q = _register(rt, sid)
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle = AcpSessionHandle(sid, q[sid], rt)

    await handle.destroy()

    assert where, "destroy never attempted to unlink the transcript files"
    assert not any(where), (
        "destroy unlinked the transcript on the event-loop thread "
        f"(loop-running at unlink: {where}); its finally must run the deletion "
        "via asyncio.to_thread"
    )
    assert not (sessions_dir / f"{sid}.json").exists()
    assert not (sessions_dir / f"{sid}.jsonl").exists()


def _gated_unlink(monkeypatch, entered: asyncio.Event, release: threading.Event):
    """Make the first real ``unlink`` block until ``release`` is set.

    The worker thread signals ``entered`` (an ``asyncio.Event``, set on the loop
    via ``call_soon_threadsafe`` so the awaiting task can wake without parking a
    pool thread) as it reaches the first unlink, then blocks on ``release``. The
    test can then cancel the awaiting task while the deletion is genuinely in
    flight on the worker — the window the shield must cover — before letting it
    finish. The ``release.wait`` bound is only a safety ceiling so a regression
    cannot hang the suite.
    """
    loop = asyncio.get_running_loop()
    real_unlink = Path.unlink
    first = {"done": False}

    def _blocking_unlink(self: Path, *args, **kwargs):
        if not first["done"]:
            first["done"] = True
            loop.call_soon_threadsafe(entered.set)
            release.wait(5)
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _blocking_unlink)


@pytest.mark.asyncio
async def test_cleanup_session_finishes_the_delete_even_when_cancelled(sessions_dir, monkeypatch):
    sid = "sess-cancel"
    (sessions_dir / f"{sid}.json").write_text("{}")
    (sessions_dir / f"{sid}.jsonl").write_text("")
    entered, release = asyncio.Event(), threading.Event()
    _gated_unlink(monkeypatch, entered, release)

    task = asyncio.ensure_future(acp_lifecycle.cleanup_session(SimpleNamespace(), sid))
    try:
        # Wait until the worker is inside the first unlink, then cancel the
        # awaiter. Bounded so a regression that never reaches the gated unlink
        # fails this test by name instead of hanging the whole shard.
        await await_until(entered, what="the worker reached the gated unlink")
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    # The shield let the worker finish: both files are gone despite the cancel.
    assert not (sessions_dir / f"{sid}.json").exists()
    assert not (sessions_dir / f"{sid}.jsonl").exists()


@pytest.mark.asyncio
async def test_destroy_finishes_the_delete_even_when_cancelled(sessions_dir, monkeypatch):
    sid = "sCancel"
    (sessions_dir / f"{sid}.json").write_text("{}")
    (sessions_dir / f"{sid}.jsonl").write_text("")
    entered, release = asyncio.Event(), threading.Event()
    _gated_unlink(monkeypatch, entered, release)

    rt, _, _ = _make_runtime()
    q = _register(rt, sid)
    rt._send_and_await = AsyncMock(return_value={})  # type: ignore[method-assign]
    handle = AcpSessionHandle(sid, q[sid], rt)

    task = asyncio.ensure_future(handle.destroy())
    try:
        await await_until(entered, what="the worker reached the gated unlink")
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task

    assert not (sessions_dir / f"{sid}.json").exists()
    assert not (sessions_dir / f"{sid}.jsonl").exists()
