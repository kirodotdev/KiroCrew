"""Tests for channel-scoped MCP pool partitioning via AcpRuntime.create_session.

Verifies that channel_id is threaded to pooled_session_servers so sessions in
different channels get different pool keys (different --channel-id args on the
broker stubs), and that channel-less sessions (CLI/cron) leave it absent.

Fixes #1056.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.types import METHOD_SESSION_NEW


def _make_initialized_runtime() -> AcpRuntime:
    """An initialized AcpRuntime wired to a fake subprocess (no real process)."""
    rt = AcpRuntime(work_dir="/tmp", mcp_gateway_overlay="/fake/overlay")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = 9999
    rt._process = proc
    rt._pid = 9999
    rt._initialized = True
    return rt


@pytest.mark.asyncio
async def test_create_session_passes_channel_id_to_pool(monkeypatch):
    """Two sessions with different channel_ids get different pool keys.

    The pool key is determined by the --channel-id flag in the MCP stub args
    returned by pooled_session_servers. If channel_id is correctly forwarded,
    two calls with different channels produce different server entries.
    """
    rt = _make_initialized_runtime()
    captured_calls: list[tuple] = []

    def _fake_pooled(overlay_dir, agent, channel_id=None):
        captured_calls.append((overlay_dir, agent, channel_id))
        # Return a minimal stub entry so the test proceeds
        return []

    monkeypatch.setattr("kiro_crew.acp.runtime.pooled_session_servers", _fake_pooled)

    async def _fake_send(method, params, timeout=30.0):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": f"sid-{len(captured_calls)}"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    # Session in channel A
    await rt.create_session(cwd="/w", agent="kirocrew", channel_id="channel-A")
    # Session in channel B
    await rt.create_session(cwd="/w", agent="kirocrew", channel_id="channel-B")

    assert len(captured_calls) == 2
    # Each call must pass the correct channel_id
    assert captured_calls[0][2] == "channel-A"
    assert captured_calls[1][2] == "channel-B"
    # The two calls have DIFFERENT channel_ids - proving partitioning
    assert captured_calls[0][2] != captured_calls[1][2]


@pytest.mark.asyncio
async def test_create_session_no_channel_leaves_pool_unscoped(monkeypatch):
    """A session with no channel (CLI/cron origin) passes None, preserving the
    channel-less pool key path - no bogus channel is injected."""
    rt = _make_initialized_runtime()
    captured_calls: list[tuple] = []

    def _fake_pooled(overlay_dir, agent, channel_id=None):
        captured_calls.append((overlay_dir, agent, channel_id))
        return []

    monkeypatch.setattr("kiro_crew.acp.runtime.pooled_session_servers", _fake_pooled)

    async def _fake_send(method, params, timeout=30.0):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-no-channel"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    # No channel_id passed (default)
    await rt.create_session(cwd="/w", agent="kirocrew")
    assert len(captured_calls) == 1
    assert captured_calls[0][2] is None


@pytest.mark.asyncio
async def test_create_session_explicit_mcp_servers_skips_pool(monkeypatch):
    """When caller supplies explicit mcp_servers, pooled_session_servers is not
    called at all, regardless of channel_id."""
    rt = _make_initialized_runtime()
    pooled_called = []

    def _fake_pooled(overlay_dir, agent, channel_id=None):
        pooled_called.append(True)
        return []

    monkeypatch.setattr("kiro_crew.acp.runtime.pooled_session_servers", _fake_pooled)

    async def _fake_send(method, params, timeout=30.0):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-explicit"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(
        cwd="/w", agent="kirocrew", mcp_servers=[{"name": "custom"}], channel_id="ch-X"
    )
    # pooled_session_servers must NOT have been called
    assert pooled_called == []


@pytest.mark.asyncio
async def test_provider_threads_channel_id_to_runtime(monkeypatch, tmp_path):
    """End-to-end: AcpProvider._start_kiro_runtime_impl extracts channel_id from
    self._client and passes it to runtime.create_session, which forwards it to
    pooled_session_servers. Two providers with different channels produce
    different pool scoping."""
    from kiro_crew.providers.acp import AcpProvider

    captured_channel_ids: list[str | None] = []

    def _fake_pooled(overlay_dir, agent, channel_id=None):
        captured_channel_ids.append(channel_id)
        return []

    monkeypatch.setattr("kiro_crew.acp.runtime.pooled_session_servers", _fake_pooled)

    # Mock spawn() so no real process is created
    async def _fake_spawn(self):
        self._process = MagicMock()
        self._process.pid = 7777
        self._process.returncode = None
        self._process.stdin = MagicMock()
        self._process.stdin.write = MagicMock()
        self._process.stdin.drain = AsyncMock()
        self._process.stdout = asyncio.StreamReader()
        self._process.stderr = asyncio.StreamReader()
        self._pid = 7777
        self._initialized = True
        self._spawn_monotonic = 0.0

    monkeypatch.setattr(AcpRuntime, "spawn", _fake_spawn)

    # Mock _send_and_await on runtime
    async def _fake_send(self, method, params, timeout=30.0):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-provider-test"}
        return {}

    monkeypatch.setattr(AcpRuntime, "_send_and_await", _fake_send)

    # Mock session_pid tracking to avoid file writes
    monkeypatch.setattr("kiro_crew.acp.runtime._track_pid", lambda pid: None)
    monkeypatch.setattr("kiro_crew.acp.runtime._track_session_pid", lambda pid: None)
    monkeypatch.setattr("kiro_crew.acp.runtime.register_protected_pid", lambda pid: None)

    # Create a provider with a channel_id
    provider = AcpProvider(
        work_dir=str(tmp_path),
        channel_id="slack-channel-123",
        mcp_gateway_overlay=str(tmp_path / "overlay"),
    )

    phases: dict[str, float] = {}
    await provider._start_kiro_runtime_impl(phases)

    # The channel_id from the AcpClient must have reached pooled_session_servers
    assert len(captured_channel_ids) == 1
    assert captured_channel_ids[0] == "slack-channel-123"
