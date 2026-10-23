"""The session-manager and context-builder doubles the ``SubagentManager`` tests share.

``_mock_sessions`` is a session manager whose provider streams nothing,
``_mock_ctx_builder`` a context builder that leaves spawn approval to a callback, and
``_mock_ctx_builder_auto_spawn`` one that auto-approves spawns. ``test_subagent.py`` and
``test_subagent_spawn_guards.py`` import all three and ``test_subagent_run_loop.py`` the
two it uses; the import in ``test_subagent.py`` keeps ``from test_subagent import ...``
working for the modules that reuse them.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def _mock_sessions() -> MagicMock:
    """Create a mock SessionManager with async methods."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        """Async generator that yields nothing — simulates an empty LLM stream."""
        return
        yield  # noqa: unreachable — makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    # A real, empty map: the teardown paths read it once before their reset,
    # and a miss (no live session, nothing to kill) is what these tests mean.
    sessions._sessions = {}
    return sessions


def _mock_ctx_builder() -> MagicMock:
    """Create a mock ContextBuilder."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    return ctx


def _mock_ctx_builder_auto_spawn() -> MagicMock:
    """Create a mock ContextBuilder with auto_approve_subagent_spawn enabled."""
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    return ctx
