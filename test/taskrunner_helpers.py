"""The session, provider and deadline doubles the TaskRunner tests share.

``test_taskrunner.py`` and its ``_recovery``, ``_run_steps``, ``_planning`` and
``_run_inputs`` siblings import these by name: ``_make_mock_sessions``, a
``SessionManager`` stand-in with the methods ``TaskRunner`` uses (``open_task_session``
forwards to its ``AsyncMock`` ``get_or_create``); ``_make_mock_provider``, a provider that
streams one text chunk and completes; and ``_GENEROUS_DEADLINE``, the bound for a wait on
state that must arrive but whose latency the host controls. Nothing here is a fixture.
``test_taskrunner.py`` re-exports ``_make_mock_sessions`` for
``test_subagent_stop_reason_consistency.py`` and ``test_taskrunner_taskq.py``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

# Bound for a wait on state that MUST arrive but whose latency the host controls
# (an executor hop, an fsync). Returns as soon as the state is observed, so the
# value only matters when the test is about to fail by name instead of hanging.
_GENEROUS_DEADLINE = 30.0


def _make_mock_sessions() -> MagicMock:
    """Create a mock SessionManager with the methods TaskRunner uses."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions._lock = asyncio.Lock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    sessions.close_all = AsyncMock()

    async def _open_task_session(
        _parent_key, session_key, *, agent=None, cwd=None, approval_policy="", start_priority=None
    ):
        # Fake: the run-scoped shared runtime is mocked away; forward to whatever
        # get_or_create is set to (preserves per-step key/call assertions).
        return await sessions.get_or_create(session_key, agent=agent, cwd=cwd)

    sessions.open_task_session = _open_task_session
    sessions.release_subagent_runtime = AsyncMock()
    return sessions


def _make_mock_provider(text: str = "done") -> MagicMock:
    """Create a mock provider that yields a text chunk + complete event."""
    from kiro_crew.providers.base import LLMEvent

    provider = MagicMock()

    async def _stream(message: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider
