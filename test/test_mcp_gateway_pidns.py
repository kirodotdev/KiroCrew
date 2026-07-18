"""Tests for PID-namespace server-side identity resolution (Phase B).

Covers:
(a) get_peer_pid extraction from SO_PEERCRED
(b) gatewayd server-side resolution finds session_pid file via ancestry walk
(c) register response carries resolved key and stub adopts it
(d) no resolution when no pid file matches (existing behavior preserved)
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import struct
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.mcp_gateway import socketsec
from kiro_crew.mcp_gateway.socketsec import get_peer_pid

_HAS_SO_PEERCRED = hasattr(socket, "SO_PEERCRED")


# ---------------------------------------------------------------------------
# (a) get_peer_pid extraction
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _HAS_SO_PEERCRED, reason="SO_PEERCRED unavailable (non-Linux)")
def test_get_peer_pid_returns_real_pid() -> None:
    """On a real AF_UNIX socketpair, get_peer_pid returns the calling
    process's PID (both ends belong to us, so peer pid == our pid)."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        pid = get_peer_pid(a)
        assert pid == os.getpid()
        pid_b = get_peer_pid(b)
        assert pid_b == os.getpid()
    finally:
        a.close()
        b.close()


def test_get_peer_pid_returns_none_for_non_socket() -> None:
    """Non-socket objects return None (not crash)."""
    assert get_peer_pid("not a socket") is None
    assert get_peer_pid(None) is None


@pytest.mark.skipif(not _HAS_SO_PEERCRED, reason="SO_PEERCRED unavailable (non-Linux)")
def test_get_peer_pid_extracts_from_transport_like() -> None:
    """asyncio transport-like objects with get_extra_info work."""
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        class FakeTransport:
            def get_extra_info(self, name: str) -> Any:
                return a if name == "socket" else None

        pid = get_peer_pid(FakeTransport())
        assert pid == os.getpid()
    finally:
        a.close()
        b.close()


def test_get_peer_pid_returns_none_when_no_so_peercred() -> None:
    """When SO_PEERCRED is unavailable, returns None."""
    with patch.object(socketsec, "_SO_PEERCRED", None):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            assert get_peer_pid(a) is None
        finally:
            a.close()
            b.close()


# ---------------------------------------------------------------------------
# (b) _resolve_session_key_from_peer_pid
# ---------------------------------------------------------------------------


def test_resolve_session_key_finds_pid_file(tmp_path: Path) -> None:
    """Walk finds session_pid_<pid>.txt at an ancestor."""
    from kiro_crew.mcp_gateway.gatewayd import _resolve_session_key_from_peer_pid

    # Simulate: peer_pid=100, parent of 100 is 50, parent of 50 is 1
    # session_pid_50.txt exists with session key
    session_key = "dashboard:chat-42-abc123"
    (tmp_path / "session_pid_50.txt").write_text(session_key, encoding="utf-8")

    def mock_parent_pid(pid: int) -> int:
        return {100: 50, 50: 1}.get(pid, 0)

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch("kiro_crew.mcp_gateway.gatewayd._ppid_fn", side_effect=mock_parent_pid),
    ):
        result = _resolve_session_key_from_peer_pid(100)
    assert result == session_key


def test_resolve_session_key_direct_match(tmp_path: Path) -> None:
    """session_pid file for the peer pid itself (no walk needed)."""
    from kiro_crew.mcp_gateway.gatewayd import _resolve_session_key_from_peer_pid

    session_key = "slack:thread-99"
    (tmp_path / "session_pid_200.txt").write_text(session_key, encoding="utf-8")

    def mock_parent_pid(pid: int) -> int:
        return {200: 1}.get(pid, 0)

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch("kiro_crew.mcp_gateway.gatewayd._ppid_fn", side_effect=mock_parent_pid),
    ):
        result = _resolve_session_key_from_peer_pid(200)
    assert result == session_key


def test_resolve_session_key_no_match(tmp_path: Path) -> None:
    """No pid file for any ancestor returns empty string."""
    from kiro_crew.mcp_gateway.gatewayd import _resolve_session_key_from_peer_pid

    def mock_parent_pid(pid: int) -> int:
        return {300: 250, 250: 1}.get(pid, 0)

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch("kiro_crew.mcp_gateway.gatewayd._ppid_fn", side_effect=mock_parent_pid),
    ):
        result = _resolve_session_key_from_peer_pid(300)
    assert result == ""


def test_resolve_session_key_handles_config_dir_error() -> None:
    """config_dir() raising returns empty gracefully."""
    from kiro_crew.mcp_gateway.gatewayd import _resolve_session_key_from_peer_pid

    with patch("kiro_crew.mcp_gateway.gatewayd._config_dir", side_effect=RuntimeError("boom")):
        result = _resolve_session_key_from_peer_pid(999)
    assert result == ""


# ---------------------------------------------------------------------------
# (c) register response carries resolved key and stub adopts it
# ---------------------------------------------------------------------------


def test_stub_adopts_resolved_session_key() -> None:
    """Stub adopts resolved_session_key from gatewayd register response,
    skipping the recaller loop (exercises the real stub.py helper)."""
    from kiro_crew.mcp_gateway.stub import adopt_resolved_session_key

    payload: dict[str, Any] = {
        "type": "register",
        "stub_uuid": "test-stub-001",
        "session_key": "",  # empty -- PID ns case
    }
    registered: dict[str, Any] = {
        "type": "registered",
        "backend_id": "pending-abc",
        "pool_label": "test:echo",
        "capabilities": ["ensure_backend"],
        "resolved_session_key": "dashboard:chat-99-xyz",
    }

    assert adopt_resolved_session_key(payload, registered) is True
    assert payload["session_key"] == "dashboard:chat-99-xyz"


def test_stub_does_not_adopt_when_already_has_key() -> None:
    """When payload already has a session_key, resolved_session_key is ignored."""
    from kiro_crew.mcp_gateway.stub import adopt_resolved_session_key

    payload: dict[str, Any] = {
        "session_key": "existing-key",
    }
    registered: dict[str, Any] = {
        "resolved_session_key": "should-not-adopt",
    }

    assert adopt_resolved_session_key(payload, registered) is False
    assert payload["session_key"] == "existing-key"


def test_stub_does_not_adopt_non_dict_or_empty_response() -> None:
    """Non-dict register responses and empty resolved keys are ignored."""
    from kiro_crew.mcp_gateway.stub import adopt_resolved_session_key

    payload: dict[str, Any] = {"session_key": ""}
    assert adopt_resolved_session_key(payload, None) is False
    assert adopt_resolved_session_key(payload, {"resolved_session_key": ""}) is False
    assert payload["session_key"] == ""


# ---------------------------------------------------------------------------
# (d) gatewayd integration: register with empty key triggers resolution
# ---------------------------------------------------------------------------


def _register_payload(session_key: str = "") -> dict[str, Any]:
    return {
        "type": "register",
        "stub_uuid": "pidns-stub-001",
        "server_name": "echo-mcp",
        "agent_name": "pidns-agent",
        "command_args_hash": "0" * 64,
        "effective_env_hash": "1" * 64,
        "work_dir": "/tmp",
        "binary_version": "deadbeef",
        "os_uid": os.getuid(),
        "sandbox_mode": "standard",
        "autoapprove_set_hash": "2" * 64,
        "approval_mode": "interactive",
        "trust_all_tools": False,
        "user_identity": "testuser",
        "channel_id": "C_TEST",
        "config_snapshot_hash": "3" * 64,
        "session_key": session_key,
        "session_type": "unknown" if not session_key else "dashboard",
        "principal_id": "testuser",
    }


class _FakeReader:
    """Feed pre-built frames to the connection handler."""

    def __init__(self, frames: list[dict[str, Any]]) -> None:
        self._q = [(json.dumps(f) + "\n").encode() for f in frames]

    async def readuntil(self, sep: bytes = b"\n") -> bytes:
        if not self._q:
            raise asyncio.IncompleteReadError(b"", None)
        return self._q.pop(0)


class _FakeWriter:
    """Capture frames written by gatewayd."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self._sock = MagicMock()
        # Simulate SO_PEERCRED returning our own pid/uid
        self._sock.family = socket.AF_UNIX
        pid_uid_gid = struct.pack("@iII", os.getpid(), os.getuid(), os.getgid())
        self._sock.getsockopt = MagicMock(return_value=pid_uid_gid)

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "socket":
            return self._sock
        return default

    def write(self, data: bytes) -> None:
        try:
            self.frames.append(json.loads(data.decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError):
            pass

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        pass

    def is_closing(self) -> bool:
        return False


@pytest.mark.skipif(not _HAS_SO_PEERCRED, reason="SO_PEERCRED unavailable (non-Linux)")
@pytest.mark.asyncio
async def test_gatewayd_resolves_session_key_for_empty_register(tmp_path: Path) -> None:
    """When a stub registers with empty session_key, gatewayd resolves via
    peer PID ancestry and returns resolved_session_key in response."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway.pool import BackendPool

    session_key = "dashboard:chat-pidns-test"
    # Write a session_pid file for the current process's parent
    ppid = os.getppid()
    (tmp_path / f"session_pid_{ppid}.txt").write_text(session_key, encoding="utf-8")

    pool = BackendPool(max_backends=5)
    register_frame = _register_payload(session_key="")
    reader = _FakeReader([register_frame])
    writer = _FakeWriter()

    def fake_resolver(pk: Any) -> None:
        return None

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch.object(gw, "SecurityEventLog", MagicMock),
    ):
        await gw._handle_connection(reader, writer, pool, fake_resolver, tmp_path, None)

    # Find the registered response
    registered_frames = [f for f in writer.frames if f.get("type") == "registered"]
    assert len(registered_frames) == 1
    resp = registered_frames[0]
    assert resp.get("resolved_session_key") == session_key


@pytest.mark.skipif(not _HAS_SO_PEERCRED, reason="SO_PEERCRED unavailable (non-Linux)")
@pytest.mark.asyncio
async def test_gatewayd_no_resolved_key_when_no_pid_file(tmp_path: Path) -> None:
    """When no session_pid file matches, response has no resolved_session_key."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway.pool import BackendPool

    pool = BackendPool(max_backends=5)
    register_frame = _register_payload(session_key="")
    reader = _FakeReader([register_frame])
    writer = _FakeWriter()

    def fake_resolver(pk: Any) -> None:
        return None

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch.object(gw, "SecurityEventLog", MagicMock),
    ):
        await gw._handle_connection(reader, writer, pool, fake_resolver, tmp_path, None)

    registered_frames = [f for f in writer.frames if f.get("type") == "registered"]
    assert len(registered_frames) == 1
    resp = registered_frames[0]
    # No resolved key when nothing matched
    assert "resolved_session_key" not in resp


@pytest.mark.asyncio
async def test_gatewayd_no_resolution_when_key_already_present(tmp_path: Path) -> None:
    """When stub registers WITH a session_key, no server-side resolution runs."""
    from kiro_crew.mcp_gateway import gatewayd as gw
    from kiro_crew.mcp_gateway.pool import BackendPool

    # Write a pid file that WOULD match if resolution ran
    ppid = os.getppid()
    (tmp_path / f"session_pid_{ppid}.txt").write_text("should-not-use", encoding="utf-8")

    pool = BackendPool(max_backends=5)
    register_frame = _register_payload(session_key="original-key")
    reader = _FakeReader([register_frame])
    writer = _FakeWriter()

    def fake_resolver(pk: Any) -> None:
        return None

    with (
        patch("kiro_crew.mcp_gateway.gatewayd._config_dir", return_value=tmp_path),
        patch.object(gw, "SecurityEventLog", MagicMock),
    ):
        await gw._handle_connection(reader, writer, pool, fake_resolver, tmp_path, None)

    registered_frames = [f for f in writer.frames if f.get("type") == "registered"]
    assert len(registered_frames) == 1
    resp = registered_frames[0]
    # No resolution attempted when key already present
    assert "resolved_session_key" not in resp
