"""Constants, the gateway-state reset and frame helpers of the stub session-token suites.

``test_mcp_gateway_stub_session_token.py`` and its ``_claim_binding`` and ``_minting``
siblings share the two session keys and tokens, the autouse ``_clean_gateway_state`` that
empties gatewayd's connection index and token bindings around every test, the register and
claim frames that carry a token, and the connection helpers (``_live_conn``, ``_attest``,
``_next_caller``, ``_close``) that drive one fake connection through gatewayd's handler.
Each module binds what it reads by assignment.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from test_mcp_gateway_claim import (
    _CALL,
    _claim,
    _handle,
    _patch_env,
    _QueueReader,
    _RecordingWriter,
    _register,
)

from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import stub as stub_mod
from kiro_crew.testing.wait import await_until

PARENT_KEY = "dashboard:chat-1-parent"
SUB_KEY = "dashboard:chat-1-parent:sub-7"
TOKEN_A = "a" * 64
TOKEN_B = "b" * 64


@pytest.fixture(autouse=True)
def _clean_gateway_state() -> Any:
    """Both module-level registries the token path touches.

    ``test_mcp_gateway_claim``'s own autouse fixture does not reach this module,
    and a leaked binding is exactly the state that would make a later test read
    a session name no claim in it ever pushed.
    """
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()


def _register_with_token(
    session_key: str,
    token: str,
    *,
    stub_uuid: str = "cp-stub-0001",
    ancestor_pids: list[int] | None = None,
) -> dict[str, Any]:
    frame = _register(session_key, ancestor_pids)
    frame["stub_uuid"] = stub_uuid
    if token:
        frame["stub_session_token"] = token
    return frame


def _claim_with_token(pid: Any, session_key: str, token: str) -> dict[str, Any]:
    frame = _claim(pid, session_key)
    if token:
        frame["stub_session_token"] = token
    return frame


def _stub_args() -> Any:
    return stub_mod._parse_args(
        [
            "--server",
            "echo-mcp",
            "--agent",
            "cp-agent",
            "--target-command",
            "python",
            "--work-dir",
            "/tmp",
            "--poolable",
        ]
    )


async def _live_conn(
    monkeypatch: pytest.MonkeyPatch,
    session_key: str,
    token: str,
    stub_uuid: str,
) -> tuple[Any, Any, Any]:
    """Drive one real connection handler to its first forwarded call."""
    backend, sel = _patch_env(monkeypatch)
    reader = _QueueReader()
    reader.feed(_register_with_token(session_key, token, stub_uuid=stub_uuid))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await await_until(backend.forwarded)
    return backend, reader, task


def _attest(monkeypatch: pytest.MonkeyPatch, host_chain: list[int]) -> None:
    """Make the kernel place this connection's peer under *host_chain*.

    The token's second factor is the SO_PEERCRED-derived chain gatewayd walks
    itself, so a test that wants a token honoured has to attest one; a test that
    wants it refused simply does not.
    """
    monkeypatch.setattr(gw.socketsec, "get_peer_pid", lambda _w: host_chain[0])
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: ("", list(host_chain)))


async def _close(reader: Any, task: Any) -> None:
    reader.feed({"type": "unregister"})
    # flake-ok: the unregister frame fed on the line above ends the handler task;
    # moved verbatim; tracked for a de-flake pass
    await task


async def _next_caller(backend: Any, reader: Any) -> Any:
    """The identity the connection forwards on its NEXT call."""
    backend.forwarded.clear()
    reader.feed(_CALL)
    await await_until(backend.forwarded)
    return backend.callers[-1]
