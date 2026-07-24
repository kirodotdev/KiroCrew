"""Verify that the Slack handler passes the resolved agent to build_message."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.slack.handler import handle_message


@pytest.fixture()
def _clear_agent_cache():
    """Reset module-level agent caches between tests."""
    from kiro_crew.slack import handler

    old_cached = handler._cached_default_agent
    old_thread = dict(handler._thread_agents)
    handler._cached_default_agent = "kirocrew"
    handler._thread_agents.clear()
    yield
    handler._cached_default_agent = old_cached
    handler._thread_agents.clear()
    handler._thread_agents.update(old_thread)


async def _empty_stream(*a, **kw):
    return
    yield  # makes this an async generator


def _make_mocks():
    """Build minimal mocks for handle_message collaborators."""
    slack = AsyncMock()
    slack.add_reaction = AsyncMock()
    slack.remove_reaction = AsyncMock()
    slack.post_message = AsyncMock()
    slack.update_message = AsyncMock(return_value="1234.5678")
    slack.fetch_message = AsyncMock(return_value=None)

    mock_client = AsyncMock()
    mock_client.stream = _empty_stream

    sessions = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
    sessions.set_channel = AsyncMock()
    sessions.set_slack_link = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions.release = AsyncMock()

    context_builder = MagicMock()
    context_builder.build_message = MagicMock(return_value=("hello", MagicMock()))
    context_builder.conversation_log = None

    return slack, sessions, context_builder


@pytest.mark.usefixtures("_clear_agent_cache")
class TestAgentPassthrough:
    """build_message must receive the resolved agent name."""

    @pytest.mark.asyncio
    async def test_channel_agent_passed(self):
        slack, sessions, ctx = _make_mocks()

        with patch("kiro_crew.slack.handler.publish_session_pid"):
            await handle_message(
                slack=slack,
                sessions=sessions,
                channel="C123",
                text="hi",
                thread_ts=None,
                msg_ts="ts1",
                user_id="U1",
                context_builder=ctx,
                channel_agent="siads-etl-test",
            )

        ctx.build_message.assert_called_once()
        call_kwargs = ctx.build_message.call_args
        assert call_kwargs.kwargs.get("agent") == "siads-etl-test", (
            f"Expected agent='siads-etl-test', got call: {call_kwargs}"
        )

    @pytest.mark.asyncio
    async def test_no_agent_passes_none(self):
        slack, sessions, ctx = _make_mocks()

        from kiro_crew.slack import handler
        handler._cached_default_agent = ""

        with patch("kiro_crew.slack.handler.publish_session_pid"):
            await handle_message(
                slack=slack,
                sessions=sessions,
                channel="C123",
                text="hi",
                thread_ts=None,
                msg_ts="ts2",
                user_id="U1",
                context_builder=ctx,
                channel_agent=None,
            )

        ctx.build_message.assert_called_once()
        call_kwargs = ctx.build_message.call_args
        agent_val = call_kwargs.kwargs.get("agent")
        assert agent_val is None, f"Expected None, got {agent_val!r}"
