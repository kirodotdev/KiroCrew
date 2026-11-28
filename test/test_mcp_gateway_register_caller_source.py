"""Where a stub connection's caller comes from.

The identity gatewayd forwards with every call is decided by the daemon: a
session token a claim bound, the kernel-attested peer walk, or a claim pushed
later. The Register and ``recaller`` frames are written by the connecting
process, so their ``session_key`` / ``principal_id`` / ``channel_id`` are a
request at most. The one exception is a Register the daemon's owning gateway
sent itself (a resource-subscription holder), which the kernel's peer pid
proves.

Drives the real ``gatewayd._handle_connection`` loop with the claim suite's
fake backend, which records the caller of every forwarded call.
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

from kiro_crew import platform_compat
from kiro_crew.mcp_gateway import gatewayd as gw
from kiro_crew.mcp_gateway import socketsec
from kiro_crew.testing.wait import async_wait_until

pytestmark = pytest.mark.xdist_group("mcp_gateway")

_OWN = "dashboard:chat-own-1"
_OTHER = "dashboard:chat-other-2"
_PEER = 9100
_HOST_CHAIN = [_PEER, 9050, 9020]


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()
    yield
    gw._CONN_INDEX.clear()
    gw._TOKEN_BINDINGS.clear()


def _named(session_key: str) -> dict[str, Any]:
    frame = _register(session_key)
    frame["principal_id"] = "someone-else"
    frame["channel_id"] = "C_OTHER"
    return frame


def _peer(monkeypatch: pytest.MonkeyPatch, walk_key: str, peer_pid: int = _PEER) -> None:
    """The kernel reports *peer_pid*; gatewayd's own walk from it resolves *walk_key*."""
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: peer_pid)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: (walk_key, list(_HOST_CHAIN)))


async def _callers(frames: list[dict[str, Any]], backend: Any) -> list[Any]:
    reader = _QueueReader()
    for frame in frames:
        reader.feed(frame)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await async_wait_until(lambda: len(backend.callers) >= sum(f is _CALL for f in frames))
    reader.feed({"type": "unregister"})
    await task
    return list(backend.callers)


@pytest.mark.asyncio
async def test_register_session_fields_are_not_the_forwarded_caller(monkeypatch) -> None:
    backend, _sel = _patch_env(monkeypatch)
    _peer(monkeypatch, _OWN)
    callers = await _callers([_named(_OTHER), _CALL], backend)
    assert callers[0] is not None
    assert callers[0].session_key == _OWN
    assert callers[0].principal_id == ""
    assert callers[0].channel_id == ""


@pytest.mark.asyncio
async def test_register_naming_a_session_the_walk_cannot_place_has_no_caller(
    monkeypatch,
) -> None:
    backend, _sel = _patch_env(monkeypatch)
    _peer(monkeypatch, "")
    assert await _callers([_named(_OTHER), _CALL], backend) == [None]


@pytest.mark.asyncio
async def test_register_without_peer_pid_has_no_caller(monkeypatch) -> None:
    backend, _sel = _patch_env(monkeypatch)
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: None)
    assert await _callers([_named(_OTHER), _CALL], backend) == [None]


@pytest.mark.asyncio
async def test_a_second_register_on_the_connection_does_not_change_the_caller(
    monkeypatch,
) -> None:
    backend, _sel = _patch_env(monkeypatch)
    _peer(monkeypatch, _OWN)
    callers = await _callers([_register(""), _CALL, _named(_OTHER), _CALL], backend)
    assert {c.session_key for c in callers} == {_OWN}


@pytest.mark.asyncio
async def test_owner_gateway_register_keeps_its_caller(monkeypatch) -> None:
    """A resource-subscription holder registers from the owning gateway itself."""
    backend, _sel = _patch_env(monkeypatch)
    monkeypatch.setattr(gw, "_OWNER_PID", _PEER)
    _peer(monkeypatch, "")
    callers = await _callers([_named(_OTHER), _CALL], backend)
    assert callers[0] is not None and callers[0].session_key == _OTHER
    assert callers[0].channel_id == "C_OTHER"


@pytest.mark.asyncio
async def test_non_owner_register_on_an_owned_daemon_is_not_taken(monkeypatch) -> None:
    backend, _sel = _patch_env(monkeypatch)
    monkeypatch.setattr(gw, "_OWNER_PID", 4242)
    monkeypatch.setattr(platform_compat, "is_exec_supervisor_of", lambda _o, _p: False)
    _peer(monkeypatch, _OWN)
    callers = await _callers([_named(_OTHER), _CALL], backend)
    assert callers[0] is not None and callers[0].session_key == _OWN


@pytest.mark.asyncio
async def test_recaller_cannot_name_a_session_the_walk_does_not(monkeypatch) -> None:
    backend, sel = _patch_env(monkeypatch)
    walk = {"key": ""}
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: _PEER)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: (walk["key"], _HOST_CHAIN))
    recaller = {"type": "recaller", "session_key": _OTHER, "session_type": "dashboard"}
    callers = await _callers([_register(""), _CALL, recaller, _CALL], backend)
    assert callers == [None, None]
    rekey = [e for e in sel if e.get("operation") == "mcp-gateway.caller-rekey"]
    assert [e["outcome"] for e in rekey] == ["denied"]


@pytest.mark.asyncio
async def test_recaller_adopts_the_session_the_walk_attests(monkeypatch) -> None:
    """Warm-pool repair: the mapping appears after the stub registered."""
    backend, _sel = _patch_env(monkeypatch)
    walk = {"key": ""}
    monkeypatch.setattr(socketsec, "get_peer_pid", lambda _w: _PEER)
    monkeypatch.setattr(gw, "_resolve_peer_identity", lambda _pid: (walk["key"], _HOST_CHAIN))
    reader = _QueueReader()
    reader.feed(_register(""))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await async_wait_until(lambda: len(backend.callers) >= 1)
    walk["key"] = _OWN
    reader.feed({"type": "recaller", "session_key": _OWN, "session_type": "dashboard"})
    reader.feed(_CALL)
    await async_wait_until(lambda: len(backend.callers) >= 2)
    reader.feed({"type": "unregister"})
    await task
    assert backend.callers[0] is None
    assert backend.callers[1] is not None and backend.callers[1].session_key == _OWN


@pytest.mark.asyncio
async def test_claimed_session_token_still_names_the_caller(monkeypatch) -> None:
    """The real launch path: the gateway's claim binds the token before the stub registers."""
    backend, _sel = _patch_env(monkeypatch)
    _peer(monkeypatch, "")
    claim = _claim(_HOST_CHAIN[-1], _OWN)
    claim["stub_session_token"] = "t" * 64
    await gw._apply_claim(claim)
    frame = _named(_OTHER)
    frame["stub_session_token"] = "t" * 64
    callers = await _callers([frame, _CALL], backend)
    assert callers[0] is not None and callers[0].session_key == _OWN


@pytest.mark.asyncio
async def test_claim_push_still_names_a_tokenless_connection(monkeypatch) -> None:
    """Reconnect before the mapping exists: claim-push is what names the stub."""
    backend, _sel = _patch_env(monkeypatch)
    _peer(monkeypatch, "")
    reader = _QueueReader()
    reader.feed(_named(_OTHER))
    reader.feed(_CALL)
    task = asyncio.create_task(_handle(reader, _RecordingWriter()))
    await async_wait_until(lambda: len(backend.callers) >= 1)
    ack = await gw._apply_claim(_claim(_HOST_CHAIN[-1], _OWN))
    assert ack["type"] == "claimed"
    reader.feed(_CALL)
    await async_wait_until(lambda: len(backend.callers) >= 2)
    reader.feed({"type": "unregister"})
    await task
    assert backend.callers[0] is None
    assert backend.callers[1].session_key == _OWN
    assert backend.callers[1].channel_id == "C_CP"
