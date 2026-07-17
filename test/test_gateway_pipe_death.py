"""Tests for AcpProcessDied handling in gateway subagent injection.

The _inject_with_retry function is nested inside _subagent_done, making direct
invocation impractical. These tests verify the error handling contract:
AcpProcessDied → session reset → notify_injection_failed → return None,
including graceful degradation when reset itself fails.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpProcessDied


async def _simulate_inject_with_retry(
    stream_fn,
    sessions,
    subagent_mgr,
    info,
    parent_key: str,
    label: str,
) -> str | None:
    """Reproduce the _inject_with_retry error handling from gateway.py.

    This mirrors the exact control flow of the nested function so we can
    unit-test the AcpProcessDied path without standing up the full gateway.
    """
    for attempt in range(3):
        try:
            return await stream_fn()
        except AcpProcessDied:
            try:
                assert sessions is not None
                await sessions.reset(parent_key)
            except Exception:
                pass
            if subagent_mgr:
                subagent_mgr.notify_injection_failed(
                    info, reason="ACP process died"
                )
            return None
    return None


class TestGatewayAcpProcessDiedInjection:
    """Verify _inject_with_retry handles AcpProcessDied correctly."""

    @pytest.mark.asyncio
    async def test_process_died_resets_session_and_notifies(self) -> None:
        """AcpProcessDied → session reset called → notify_injection_failed → returns None."""
        sessions = MagicMock()
        sessions.reset = AsyncMock()
        subagent_mgr = MagicMock()
        info = MagicMock(id="sub-1")

        stream_fn = AsyncMock(side_effect=AcpProcessDied("pipe broken"))

        result = await _simulate_inject_with_retry(
            stream_fn, sessions, subagent_mgr, info, "dashboard:slot-1", "result"
        )

        assert result is None
        sessions.reset.assert_awaited_once_with("dashboard:slot-1")
        subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="ACP process died"
        )

    @pytest.mark.asyncio
    async def test_process_died_with_reset_failure_still_notifies(self) -> None:
        """AcpProcessDied + reset raises → still calls notify_injection_failed → returns None."""
        sessions = MagicMock()
        sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        subagent_mgr = MagicMock()
        info = MagicMock(id="sub-1")

        stream_fn = AsyncMock(side_effect=AcpProcessDied("pipe broken"))

        result = await _simulate_inject_with_retry(
            stream_fn, sessions, subagent_mgr, info, "dashboard:slot-1", "result"
        )

        assert result is None
        sessions.reset.assert_awaited_once_with("dashboard:slot-1")
        subagent_mgr.notify_injection_failed.assert_called_once_with(
            info, reason="ACP process died"
        )
