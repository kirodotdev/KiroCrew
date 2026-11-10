"""The local-owner check behind a token mint does not wait on the default executor.

Each test fills the event loop's default executor with a job that blocks until the test
releases it, then runs a mint. Before ``local_auth_executor`` the check queued
behind that job, so the mint hung until the timeout below.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
from collections.abc import Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.member_memory_auth import LocalOwnerProvenance

SECRET = "local-secret-value"
MINT_DEADLINE_SECS = 10
_T = TypeVar("_T")


def _loopback_request(path: str, method: str = "GET") -> web.Request:
    sock = MagicMock()
    sock.family = socket.AF_INET
    transport = MagicMock()
    transport.get_extra_info.side_effect = lambda key, default=None: {
        "socket": sock,
        "peername": ("127.0.0.1", 43123),
    }.get(key, default)
    app = web.Application()
    app["local_secret"] = SECRET
    app["state"] = MagicMock(owner_id="owner-1")
    return make_mocked_request(
        method, path, headers={"X-Local-Secret": SECRET}, app=app, transport=transport
    )


@pytest.fixture
def owner_gates_pass(monkeypatch):
    monkeypatch.setattr(
        "kiro_crew.member_memory_auth.local_owner_bootstrap_provenance",
        lambda _request: LocalOwnerProvenance(),
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.sel", lambda: MagicMock())
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: MagicMock())


@contextlib.contextmanager
def _default_executor_held() -> Iterator[None]:
    """Make the event loop's default executor one thread, and hold that thread."""
    pool = ThreadPoolExecutor(max_workers=1)
    asyncio.get_running_loop().set_default_executor(pool)
    release = threading.Event()
    started = threading.Event()

    def _hold() -> None:
        started.set()
        release.wait()

    try:
        pool.submit(_hold)
        assert started.wait(MINT_DEADLINE_SECS)
        yield
    finally:
        release.set()


async def _finished_within_deadline(call: Coroutine[Any, Any, _T]) -> _T:
    task = asyncio.ensure_future(call)
    done, _pending = await asyncio.wait({task}, timeout=MINT_DEADLINE_SECS)
    if task not in done:
        task.cancel()
    assert task in done, "the owner check is still queued behind the default executor"
    return task.result()


@pytest.mark.asyncio
async def test_token_local_mints_while_the_default_executor_is_full(
    owner_gates_pass, monkeypatch
) -> None:
    from kiro_crew.dashboard.handlers import core

    monkeypatch.setattr(core, "generate_token", lambda *_a, **_kw: "issued-value")
    request = _loopback_request("/api/token/local?ttl=2m")

    with _default_executor_held():
        response = await _finished_within_deadline(core.api_token_local(request))

    assert response.status == 200


@pytest.mark.asyncio
async def test_relay_viewer_admits_main_while_the_default_executor_is_full(
    owner_gates_pass,
) -> None:
    from kiro_crew.dashboard.handlers import relay_viewer

    request = _loopback_request("/api/relay-viewer/token", method="POST")

    with _default_executor_held():
        refusal = await _finished_within_deadline(relay_viewer._local_main_refusal(request))

    assert refusal is None
