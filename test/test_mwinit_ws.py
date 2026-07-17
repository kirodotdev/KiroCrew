"""Tests for mwinit WebSocket handler with RSA-OAEP encryption."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard import handlers


@pytest.fixture
def mock_sel():
    """Mock SEL logging."""
    with patch.object(handlers, "sel") as m:
        m.return_value.log_api_access = MagicMock()
        yield m


class TestMwinitWsAuth:
    """Test authentication checks."""

    @pytest.mark.asyncio
    async def test_rejects_unauthenticated_before_upgrade(self, mock_sel) -> None:
        """Auth check must happen before WebSocket upgrade (deny-by-default)."""
        request = MagicMock()
        request.get.return_value = None  # no user — middleware misconfigured
        request.remote = "127.0.0.1"

        response = await handlers.api_mwinit_ws(request)

        assert isinstance(response, web.Response)
        assert response.status == 401
        mock_sel.return_value.log_api_access.assert_called_once()
        call_kwargs = mock_sel.return_value.log_api_access.call_args.kwargs
        assert call_kwargs["outcome"] == "denied"

    @pytest.mark.asyncio
    async def test_accepts_authenticated_user(self, mock_sel) -> None:
        """Authenticated user proceeds to WebSocket upgrade (then stub closes)."""
        request = MagicMock()
        request.get.return_value = "testuser"
        request.remote = "127.0.0.1"

        mock_ws = AsyncMock()
        mock_ws.prepare = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.closed = False

        with patch("aiohttp.web.WebSocketResponse", return_value=mock_ws):
            result = await handlers.api_mwinit_ws(request)

        mock_ws.prepare.assert_called_once_with(request)
        mock_sel.return_value.log_api_access.assert_called()
        # OSS stub reports unavailability and closes the connection — no subprocess.
        mock_ws.send_json.assert_called_once_with({"error": "not available in OSS"})
        mock_ws.close.assert_called_once()
        assert result is mock_ws


class TestMwinitWsLogging:
    """Test SEL audit logging."""

    @pytest.mark.asyncio
    async def test_logs_ws_open(self, mock_sel) -> None:
        """Logs mwinit.ws.open on successful auth."""
        request = MagicMock()
        request.get.return_value = "testuser"
        request.remote = "127.0.0.1"

        mock_ws = AsyncMock()
        mock_ws.prepare = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.closed = False

        with patch("aiohttp.web.WebSocketResponse", return_value=mock_ws):
            await handlers.api_mwinit_ws(request)

        calls = mock_sel.return_value.log_api_access.call_args_list
        ops = [c.kwargs.get("operation") or c[1].get("operation") for c in calls]
        assert "mwinit.ws.open" in ops


class TestMwinitWsIdleTimeout:
    """Test WebSocket idle timeout behavior."""

    @pytest.mark.asyncio
    async def test_ws_created_with_heartbeat_and_timeout(self, mock_sel) -> None:
        """WebSocketResponse must be created with heartbeat=30 and timeout=300."""
        request = MagicMock()
        request.get.return_value = "testuser"
        request.remote = "127.0.0.1"

        mock_ws = AsyncMock()
        mock_ws.prepare = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.close = AsyncMock()
        mock_ws.closed = False

        with patch("aiohttp.web.WebSocketResponse", return_value=mock_ws) as mock_ws_cls:
            await handlers.api_mwinit_ws(request)

        mock_ws_cls.assert_called_once_with(heartbeat=30, timeout=300)
