"""Shared fakes for the dashboard chat test files (``test_dashboard_chat*.py``).

``_provider_mock`` stays importable from ``test_dashboard_chat`` as well, which
``test_chat_close_interrupts_turn.py`` imports it from.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _provider_mock() -> AsyncMock:
    """A stand-in for the ACP session provider a chat turn drives.

    The turn surface (``stream``, ``shutdown``, ``approve_tool`` ...) is async,
    so the double is an ``AsyncMock``. The telemetry accessors the runner reads
    after every turn -- ``context_usage_pct``, ``context_window_tokens``,
    ``context_used_tokens``, ``mcp_session_report``, ``available_models``, and the
    inner client's ``pop_pending_oauth_requests`` -- are SYNCHRONOUS on the real
    provider and are called without ``await``. Left as
    ``AsyncMock`` children each call would hand back a coroutine nobody awaits,
    which the interpreter reports at garbage collection against whichever later
    test happens to trigger it. Tests override any accessor they assert on.
    """
    client = AsyncMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client.context_used_tokens = MagicMock(return_value=0)
    client.mcp_session_report = MagicMock(return_value=None)
    client.available_models = MagicMock(return_value=[])
    client.client.pop_pending_oauth_requests = MagicMock(return_value=[])
    return client
