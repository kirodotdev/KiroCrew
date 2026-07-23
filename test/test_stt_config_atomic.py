"""Regression tests for the STT config PUT handler (Track A, bug 2).

The handler must perform its read-modify-write under the shared config lock
(mutual exclusion vs other config writers) and write atomically, matching the
established pattern used by the other config handlers in the module.
"""

from __future__ import annotations

import asyncio
import json
import threading
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web

import kiro_crew.dashboard.handlers.core as core
from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.handlers.agents import _get_config_lock


def _put(body: dict):
    req = MagicMock(spec=web.Request)
    req.method = "PUT"
    req.json = AsyncMock(return_value=body)
    return req


@pytest.fixture(autouse=True)
def _stub_probes(monkeypatch):
    # The GET-building tail probes the system (subprocess) — stub it out so the
    # test is fast and deterministic and doesn't depend on the host.
    monkeypatch.setattr(core, "_stt_prereq_commands", lambda provider: {})
    monkeypatch.setattr(core, "is_available", lambda stt: False)


@pytest.mark.asyncio
async def test_put_persists_atomically(tmp_path) -> None:
    resp = await core.api_stt_config(_put({"language_code": "fr-FR", "transcribe_region": "us-west-2"}))
    assert resp.status == 200

    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["language_code"] == "fr-FR"
    assert data["stt"]["transcribe_region"] == "us-west-2"
    # Atomic write leaves no temp files behind.
    assert list(config_path().parent.glob("*.tmp")) == []


@pytest.mark.asyncio
async def test_put_blocks_on_config_lock() -> None:
    """The handler must acquire the shared config lock: while another writer
    holds it, the STT PUT cannot proceed."""
    lock = _get_config_lock()
    await lock.acquire()
    try:
        task = asyncio.create_task(
            core.api_stt_config(_put({"language_code": "de-DE"}))
        )
        await asyncio.sleep(0.05)
        # Still blocked because the critical section is guarded by the lock.
        assert not task.done()
    finally:
        lock.release()

    resp = await task
    assert resp.status == 200
    data = json.loads(config_path().read_text(encoding="utf-8"))
    assert data["stt"]["language_code"] == "de-DE"


@pytest.mark.asyncio
async def test_put_offloads_atomic_write(monkeypatch) -> None:
    """A slow fsync-backed write must not run on the gateway event loop."""
    import kiro_crew.agent as agent_mod

    event_loop_thread = threading.get_ident()
    write_threads: list[int] = []
    original_write = agent_mod._atomic_json_write

    def recording_write(path, data) -> None:
        write_threads.append(threading.get_ident())
        original_write(path, data)

    monkeypatch.setattr(agent_mod, "_atomic_json_write", recording_write)

    resp = await core.api_stt_config(_put({"language_code": "it-IT"}))

    assert resp.status == 200
    assert write_threads
    assert all(thread_id != event_loop_thread for thread_id in write_threads)
