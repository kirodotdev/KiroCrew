"""Shared doubles and the registry reset for the ``test_mcp_gatewayd_coverage`` files.

It holds ``_FakeWriter`` (the ``asyncio.StreamWriter`` double), the ``_pool_key`` builder,
``_fake_backend`` (a ``Backend`` over mock pipes whose stdout pump is the inert
``_noop_pump``),
``_await_kwargs`` (the keyword arguments of a mock's most recent await), the ``_drain_task``
cancel-and-await helper, and ``_clean_gateway_globals``, the autouse fixture that clears
gatewayd's process-global stub-probe and connection-index registries around every test.
``test_mcp_gatewayd_coverage.py`` and ``test_mcp_gatewayd_coverage_acquire_respawn.py``
import from it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import PoolKey

# --- doubles -----------------------------------------------------------------


class _FakeWriter:
    """``asyncio.StreamWriter`` double recording writes, optionally failing."""

    def __init__(self, *, fail: Optional[BaseException] = None, hang: bool = False) -> None:
        self.writes: list[bytes] = []
        self.drains = 0
        self._fail = fail
        self._hang = hang

    def write(self, payload: bytes) -> None:
        self.writes.append(payload)

    async def drain(self) -> None:
        self.drains += 1
        if self._hang:
            # flake-ok: pre-existing hang that the write-reply deadline cancels, moved verbatim; tracked for a de-flake pass
            await asyncio.sleep(3600)
        if self._fail is not None:
            raise self._fail

    def frames(self) -> list[Any]:
        return [json.loads(p.decode("utf-8")) for p in self.writes]


def _pool_key(server: str = "demo-mcp", env_hash: str = "e" * 8) -> PoolKey:
    return PoolKey(
        server_name=server,
        command_args_hash="a" * 8,
        effective_env_hash=env_hash,
        work_dir="/tmp/cov",
        binary_version="1.0",
        os_uid=1000,
    )


async def _noop_pump() -> None:
    return None


def _fake_backend(key: Optional[PoolKey] = None, pid: int = 4242) -> Backend:
    """A ``Backend`` over mock pipes: alive, with an inert stdout pump."""
    proc = MagicMock()
    proc.returncode = None
    proc.pid = pid
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    # flake-ok: stamps the double's created and last-used times; no elapsed time is measured
    now = time.monotonic()
    backend = Backend(
        pool_key=key or _pool_key(),
        process=proc,
        stdin=stdin,
        stdout=MagicMock(),
        created_at=now,
        last_used_at=now,
    )
    # Never read the mock stdout: the acquire path starts the pump as a task.
    backend.run_stdout_pump = _noop_pump  # type: ignore[method-assign]
    return backend


def _await_kwargs(mock: Any) -> dict[str, Any]:
    """Keyword arguments of a mock's most recent await (fails loudly if none)."""
    assert mock.await_args is not None, "expected the mock to have been awaited"
    return dict(mock.await_args.kwargs)


async def _drain_task(task: Optional[asyncio.Task[Any]]) -> None:
    """Cancel and await a helper-created task so nothing outlives the test."""
    if task is None:
        return
    task.cancel()
    try:
        # flake-ok: awaits a task this helper has just cancelled
        await task
    except (asyncio.CancelledError, Exception):
        pass


@pytest.fixture(autouse=True)
def _clean_gateway_globals():
    """gatewayd keeps process-global stub/PID registries; never leak between tests."""
    gw._STUB_PROBES.clear()
    gw._CONN_INDEX.clear()
    yield
    gw._STUB_PROBES.clear()
    gw._CONN_INDEX.clear()
