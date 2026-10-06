"""A kiro session lock left by a dead holder is removed so session/load resumes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.providers.acp import AcpProvider

LOCKED = RuntimeError("Failed to start session: Session is active in another process (PID 1)")


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def _runtime(load_session: AsyncMock) -> MagicMock:
    rt = MagicMock()
    rt.is_alive.return_value = True
    rt.load_session = load_session
    return rt


async def _load(tmp_path, lock_text: str | None, side_effect: list) -> tuple[object, MagicMock]:
    if lock_text is not None:
        (tmp_path / "sid.lock").write_text(lock_text, encoding="utf-8")
    provider = AcpProvider.__new__(AcpProvider)
    rt = _runtime(AsyncMock(side_effect=side_effect))
    with (
        patch("kiro_crew.providers.acp.kiro_sessions_dir", return_value=tmp_path),
        patch("kiro_crew.providers.acp.asyncio.sleep", new=AsyncMock()),
    ):
        got = await provider._load_session_with_retry(rt, "/s.json", "sid", None, None)
    return got, rt


@pytest.mark.asyncio
async def test_dead_holder_lock_is_removed_and_load_retried(tmp_path):
    handle = object()
    lock = json.dumps({"pid": _dead_pid(), "started_at": "2026-07-16T23:14:07Z"})
    got, rt = await _load(tmp_path, lock, [LOCKED, handle])
    assert got is handle
    assert rt.load_session.await_count == 2
    assert not (tmp_path / "sid.lock").exists()


@pytest.mark.asyncio
async def test_live_holder_lock_is_untouched_and_falls_back(tmp_path):
    lock = json.dumps({"pid": os.getpid(), "started_at": "2026-07-16T23:14:07Z"})
    got, rt = await _load(tmp_path, lock, [LOCKED] * 4)
    assert got is None
    assert rt.load_session.await_count == 4
    assert (tmp_path / "sid.lock").read_text(encoding="utf-8") == lock


@pytest.mark.asyncio
@pytest.mark.parametrize("lock", ["not json", '{"started_at": "x"}', '{"pid": "12"}', "[1]"])
async def test_unparseable_lock_is_untouched(tmp_path, lock):
    got, rt = await _load(tmp_path, lock, [LOCKED] * 4)
    assert got is None
    assert rt.load_session.await_count == 4
    assert (tmp_path / "sid.lock").read_text(encoding="utf-8") == lock


@pytest.mark.asyncio
async def test_sweep_retries_only_once(tmp_path):
    # The retried load is refused again: the normal backoff takes over and
    # no second sweep happens, so the attempt budget is unchanged.
    lock = json.dumps({"pid": _dead_pid()})
    got, rt = await _load(tmp_path, lock, [LOCKED] * 5)
    assert got is None
    assert rt.load_session.await_count == 5
