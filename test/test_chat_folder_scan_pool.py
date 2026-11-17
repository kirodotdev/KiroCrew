"""The chat folders' archived-session scans answer while ``subprocess_executor`` is full.

``GET /api/chat/folders`` and the empty-only ``DELETE`` count archived sessions on a
worker thread. Process-tree walks share ``subprocess_executor`` and can hold every one
of its workers past their callers' timeouts, so the scans run on ``discovery_executor``,
the pool for the dashboard's read-only filesystem scans.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state

from kiro_crew import executors
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

_AGENT_HEADERS = {
    "X-Internal-Secret": "s3cret",
    "X-Internal-Caller": "kirocrew-dashboard",
    "X-Session-Key": "dashboard:conductor",
}


def _hold_every_subprocess_worker(release: threading.Event) -> list[Any]:
    pool = executors.subprocess_executor()
    started = threading.Barrier(pool._max_workers + 1)

    def hold() -> None:
        started.wait(FALLBACK_TIMEOUT_SECS)
        release.wait(FALLBACK_TIMEOUT_SECS)

    futures = [pool.submit(hold) for _ in range(pool._max_workers)]
    started.wait(FALLBACK_TIMEOUT_SECS)
    return futures


async def _answered(request: Awaitable[Any]) -> Any:
    """The response to *request*, which must arrive while every worker is held."""
    task = asyncio.ensure_future(request)
    done, _ = await asyncio.wait({task}, timeout=FALLBACK_TIMEOUT_SECS)
    if task not in done:
        task.cancel()
    assert task in done, "the route waited for a subprocess_executor worker"
    return task.result()


@pytest.mark.asyncio
async def test_folder_list_and_empty_delete_answer_while_subprocess_pool_is_full(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("conductor")
    release = threading.Event()
    async with TestClient(TestServer(_make_folder_app(state))) as client:
        created = await client.post(
            "/api/chat/folders", json={"name": "Empty"}, headers=_AGENT_HEADERS
        )
        assert created.status in (200, 201), await created.text()
        fid = (await created.json())["id"]
        holders = _hold_every_subprocess_worker(release)
        try:
            listed = await _answered(client.get("/api/chat/folders"))
            assert not release.is_set()
            assert listed.status == 200
            assert fid in {f["id"] for f in await listed.json()}
            deleted = await _answered(
                client.delete(f"/api/chat/folders/{fid}?if_empty=true", headers=_AGENT_HEADERS)
            )
            assert deleted.status == 200, await deleted.text()
        finally:
            release.set()
            for future in holders:
                future.result(timeout=FALLBACK_TIMEOUT_SECS)
    assert fid not in {f["id"] for f in state._folders}
