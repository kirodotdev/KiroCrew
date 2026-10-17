"""Fixture and backend helpers shared by the ``test_mcp_gateway_backend_coverage`` family.

``_no_real_metrics_file`` is the autouse fixture every file of the family imports: it sets
``backend._METRICS_PATH`` to ``None`` so no test appends to the operator's real call-metrics file.
``_make_backend`` builds a real ``Backend`` over a mock process and a mock stdin under the
``PoolKey`` from ``_pool_key``; ``_frames`` decodes every JSON-RPC frame written to that stdin,
``_line`` encodes one frame as a stdout line, ``_drain`` decodes the next frame in a stub's inbox,
``_reader`` builds a pre-filled ``asyncio.StreamReader``, ``_settle_lease`` answers the newest
forwarded subscribe or unsubscribe with the server's verdict, and ``_settle`` awaits the backend's
background metric and MCP Apps tasks. The family is ``test_mcp_gateway_backend_coverage.py`` and its
siblings ``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py``,
``test_mcp_gateway_backend_coverage_mcp_apps.py`` and
``test_mcp_gateway_backend_coverage_process_lifecycle.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import PoolKey


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture moved verbatim; no test of the family calls monkeypatch.undo()
def _no_real_metrics_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never append to the operator's real call-metrics file.

    ``backend._METRICS_PATH`` is resolved from ``MCP_GATEWAY_CALL_METRICS_PATH``
    at IMPORT time. On a machine where that variable is set, response routing
    schedules ``_emit_call_metric`` and these tests would append to that real
    path -- a host-level side effect outside ``tmp_path``, and one that only
    appears on the machines that have the variable set. Force it off for the
    whole module rather than per-test, so a newly added test cannot reintroduce
    the leak by forgetting the guard.
    """

    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)


def _pool_key(server: str = "example-mcp") -> PoolKey:
    return PoolKey(
        server_name=server,
        command_args_hash="cah",
        effective_env_hash="eeh",
        work_dir="/nonexistent-work-dir",
        binary_version="1.0",
        os_uid=1000,
    )


def _make_backend(
    *,
    stdout: Optional[Any] = None,
    returncode: Optional[int] = None,
) -> Backend:
    """A real :class:`Backend` over a mock process + mock stdin writer."""
    proc = MagicMock()
    proc.returncode = returncode
    # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid moved verbatim; the process double is never signalled
    proc.pid = 4242
    proc.wait = AsyncMock(return_value=returncode if returncode is not None else 0)
    proc.kill = MagicMock()
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.close = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    return Backend(
        pool_key=_pool_key(),
        process=cast(Any, proc),
        stdin=cast(Any, stdin),
        stdout=cast(Any, stdout if stdout is not None else MagicMock()),
        created_at=now,
        last_used_at=now,
    )


def _frames(backend: Backend) -> list[dict]:
    """Decode every JSON-RPC frame written to the backend's mock stdin."""
    out: list[dict] = []
    for call in cast(Any, backend.stdin).write.call_args_list:
        for line in call.args[0].decode("utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def _reader(*chunks: bytes, eof: bool = True, limit: int = 65536) -> asyncio.StreamReader:
    """A pre-filled StreamReader. Must be built inside a running loop."""
    reader = asyncio.StreamReader(limit=limit)
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def _line(obj: Any) -> bytes:
    return (json.dumps(obj, separators=(",", ":")) + "\n").encode("utf-8")


async def _drain(inbox: "asyncio.Queue[bytes]") -> dict:
    return json.loads(inbox.get_nowait().decode("utf-8"))


async def _settle_lease(backend: Backend, *, error: Optional[dict] = None) -> None:
    """Answer the newest forwarded subscribe/unsubscribe with the server's
    verdict (success by default), driving the lease transition its response
    confirms or refuses."""
    fid = next(f for f, p in reversed(list(backend._pending_requests.items())) if p.resource_uri)
    msg: dict[str, Any] = {"id": fid}
    if error is not None:
        msg["error"] = error
    else:
        msg["result"] = {}
    await backend._route_backend_line(_line(msg))


async def _settle(backend: Backend) -> None:
    """Await any background metric/apps tasks so nothing is GC'd mid-flight."""
    tasks = list(backend._metric_tasks) + list(backend._apps_tasks)
    if tasks:
        # flake-ok: pre-existing unbounded await on the metric and apps tasks the backend started; each ends in-process, moved verbatim
        await asyncio.gather(*tasks, return_exceptions=True)
