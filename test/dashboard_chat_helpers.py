"""Shared fakes for the dashboard chat test files (``test_dashboard_chat*.py``).

``_provider_mock`` stays importable from ``test_dashboard_chat`` as well, which
``test_chat_close_interrupts_turn.py`` imports it from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _provider_mock() -> AsyncMock:
    """A stand-in for the ACP session provider a chat turn drives.

    The turn surface is async, but the telemetry accessors (``context_usage_pct`` ...,
    ``pop_pending_oauth_requests``) are sync; as ``AsyncMock`` children they would leak
    unawaited coroutines reported against a later test. Tests override what they assert.
    """
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.available_models = MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
    return client
