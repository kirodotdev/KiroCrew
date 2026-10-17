"""A pooled backend whose server lost its MCP session is handshaken again.

A pooling multiplexer keeps its thin client's daemon connection up while it
replaces the server process behind it, and the replacement is spawned cold. The gateway sent
``initialize`` once for that backend and answers every later stub from its cache,
so without a second handshake a Python MCP server refuses every request from then
on with ``-32602 Invalid request parameters`` (data ``""``) until the gateway
restarts. These tests pin the recovery: one re-sent handshake, one retry, and the
stub sees only the retry's answer.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import Backend
from kiro_crew.mcp_gateway.pool import PoolKey

LOST_SESSION = {"code": -32602, "message": "Invalid request parameters", "data": ""}
INIT_PARAMS = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"roots": {}},
    "clientInfo": {"name": "kiro-cli", "version": "1.0"},
}
CALL_PARAMS = {"name": "lookup_prefix", "arguments": {"query": "192.0.2.0/24"}}


@pytest.fixture(autouse=True)
def _no_real_metrics_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(backend_mod, "_METRICS_PATH", None)


def _make_backend() -> Backend:
    proc = MagicMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    proc.wait = AsyncMock(return_value=0)
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    now = time.monotonic()
    key = PoolKey(
        server_name="example-mcp",
        command_args_hash="c",
        effective_env_hash="e",
        work_dir="/nonexistent",
        binary_version="1",
        os_uid=1000,
    )
    return Backend(
        pool_key=key,
        process=cast(Any, proc),
        stdin=cast(Any, stdin),
        stdout=cast(Any, MagicMock()),
        created_at=now,
        last_used_at=now,
    )


def _frames(backend: Backend) -> list[dict]:
    out: list[dict] = []
    for call in cast(Any, backend.stdin).write.call_args_list:
        out.extend(json.loads(line) for line in call.args[0].decode().splitlines() if line)
    return out


def _line(obj: Any) -> bytes:
    return (json.dumps(obj) + "\n").encode()


def _inbox_frames(inbox: Any) -> list[dict]:
    out = []
    while not inbox.empty():
        out.append(json.loads(inbox.get_nowait().decode()))
    return out


async def _handshaken(stub: str = "s1") -> tuple[Backend, Any, dict]:
    """A backend that completed its one cached handshake, with ``stub`` attached,
    and the ``initialize`` frame that handshake sent upstream."""
    backend = _make_backend()
    inbox = await backend.attach_stub(stub)
    await backend.forward_from_stub(
        stub, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT_PARAMS}
    )
    first_upstream_init = _frames(backend)[0]
    init_fid = first_upstream_init["id"]
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init_fid, "result": {"capabilities": {}}})
    )
    _inbox_frames(inbox)  # the stub's own initialize reply
    cast(Any, backend.stdin).write.reset_mock()
    return backend, inbox, first_upstream_init


async def _call(
    backend: Backend,
    stub: str = "s1",
    rid: int = 7,
    method: str = "tools/call",
    params: Optional[dict] = None,
) -> str:
    await backend.forward_from_stub(
        stub,
        {
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": CALL_PARAMS if params is None else params,
        },
    )
    return cast(str, _frames(backend)[-1]["id"])


@pytest.mark.asyncio
async def test_lost_session_is_handshaken_again_and_the_call_retried_once() -> None:
    backend, inbox, first_upstream_init = await _handshaken()
    fid = await _call(backend)
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": fid, "error": LOST_SESSION}))

    # One write: initialize, initialized, then the same request under a new id.
    assert cast(Any, backend.stdin).write.call_count == 1
    init, initialized, retry = _frames(backend)
    # The very frame the backend was first handshaken with (extensions the
    # gateway injects included), not the stub's raw request.
    assert init["method"] == "initialize"
    assert init["params"] == first_upstream_init["params"]
    assert init["params"]["clientInfo"] == INIT_PARAMS["clientInfo"]
    assert initialized == {"jsonrpc": "2.0", "method": "notifications/initialized"}
    assert retry["method"] == "tools/call" and retry["params"] == CALL_PARAMS
    assert retry["id"] not in (fid, init["id"])
    # The refusal never reached the stub.
    assert _inbox_frames(inbox) == []

    # The initialize reply is swallowed; the retry's answer reaches the stub
    # under the stub's own id.
    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": init["id"], "result": {"capabilities": {}}})
    )
    await backend._route_backend_line(
        _line(
            {
                "jsonrpc": "2.0",
                "id": retry["id"],
                "result": {"content": [], "isError": False},
            }
        )
    )
    assert _inbox_frames(inbox) == [
        {"jsonrpc": "2.0", "id": 7, "result": {"content": [], "isError": False}}
    ]
    assert backend._pending_requests == {}


@pytest.mark.asyncio
async def test_a_retry_refused_again_is_delivered_not_retried() -> None:
    backend, inbox, _ = await _handshaken()
    fid = await _call(backend)
    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": fid, "error": LOST_SESSION}))
    retry_fid = _frames(backend)[-1]["id"]
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": retry_fid, "error": LOST_SESSION})
    )

    assert _frames(backend) == []
    assert _inbox_frames(inbox) == [{"jsonrpc": "2.0", "id": 7, "error": LOST_SESSION}]


@pytest.mark.parametrize(
    "error",
    [
        # A multiplexer's tool-filter gate, and a backend refusing an unknown tool.
        {
            "code": -32602,
            "message": "Tool lookup_prefix not found",
            "data": {"tool": "x"},
        },
        {"code": -32602, "message": "Invalid request parameters", "data": "detail"},
        {"code": -32603, "message": "Invalid request parameters", "data": ""},
    ],
)
@pytest.mark.asyncio
async def test_other_errors_are_delivered_untouched(error: dict) -> None:
    backend, inbox, _ = await _handshaken()
    fid = await _call(backend)
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": fid, "error": error}))

    assert _frames(backend) == []
    assert _inbox_frames(inbox) == [{"jsonrpc": "2.0", "id": 7, "error": error}]


@pytest.mark.asyncio
async def test_a_method_outside_the_retry_set_is_delivered_untouched() -> None:
    backend, inbox, _ = await _handshaken()
    fid = await _call(backend, method="logging/setLevel", params={"level": "info"})
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": fid, "error": LOST_SESSION}))

    assert _frames(backend) == []
    assert _inbox_frames(inbox) == [{"jsonrpc": "2.0", "id": 7, "error": LOST_SESSION}]


@pytest.mark.asyncio
async def test_no_retry_for_a_stub_that_has_detached() -> None:
    backend, _inbox, _ = await _handshaken()
    fid = await _call(backend)
    await backend.detach_stub("s1")
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(_line({"jsonrpc": "2.0", "id": fid, "error": LOST_SESSION}))

    assert [f.get("method") for f in _frames(backend)] == []


@pytest.mark.asyncio
async def test_the_retry_carries_the_injected_identity_blocks() -> None:
    """The retry is the frame that went upstream, not the stub's raw frame."""
    backend, _inbox, _ = await _handshaken()
    backend.supports_caller_identity = True
    await backend.forward_from_stub(
        "s1",
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": CALL_PARAMS},
        tenant_nonce="n-1",
    )
    first = _frames(backend)[-1]
    cast(Any, backend.stdin).write.reset_mock()

    await backend._route_backend_line(
        _line({"jsonrpc": "2.0", "id": first["id"], "error": LOST_SESSION})
    )

    retry = _frames(backend)[-1]
    assert retry["params"] == first["params"]
    assert "_meta" in retry["params"]
