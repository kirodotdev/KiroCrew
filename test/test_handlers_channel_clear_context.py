"""Tests for /api/channels/{id}/clear-context handler."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.dashboard.handlers_channel import api_channel_clear_context


def _make_agent(agent_id: str, role: str, session_key: str):
    agent = MagicMock()
    agent.id = agent_id
    agent.role = role
    agent.session_key = session_key
    return agent


def _make_channel(ch_id: str, agents: dict):
    ch = MagicMock()
    ch.id = ch_id
    ch.members = agents
    ch.messages = [MagicMock(), MagicMock()]
    ch._msg_index = {"msg1": MagicMock(), "msg2": MagicMock()}
    ch.exchange_counts = {("a", "b"): 3}
    ch._save = MagicMock()
    return ch


def _make_request(ch_id: str, body: dict, channel=None, sessions=None):
    request = MagicMock()
    request.match_info = {"id": ch_id}
    request.json = AsyncMock(return_value=body)

    mgr = MagicMock()
    mgr.get.return_value = channel
    request.app = {
        "state": MagicMock(sessions=sessions or AsyncMock()),
        "channel_manager": mgr,
    }
    return request


class TestChannelClearContext:
    @pytest.mark.asyncio
    async def test_returns_404_when_channel_not_found(self):
        request = _make_request("nonexistent", {}, channel=None)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=None)),
        ):
            resp = await api_channel_clear_context(request)
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_clears_all_agents(self):
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Writer", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.reset = AsyncMock()

        request = _make_request("ch1", {"scope": "all"}, channel=ch, sessions=sessions)
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        assert set(body["cleared"]) == {"Researcher", "Writer"}
        assert sessions.reset.call_count == 2
        assert ch.messages == []
        assert ch._msg_index == {}
        assert ch.exchange_counts == {}
        ch._save.assert_called_once()

    @pytest.mark.asyncio
    async def test_clears_single_agent(self):
        agents = {
            "a1": _make_agent("a1", "Researcher", "channel:ch1:a1"),
            "a2": _make_agent("a2", "Writer", "channel:ch1:a2"),
        }
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()
        sessions.reset = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "a1"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        body = json.loads(resp.body)
        assert body["ok"] is True
        assert body["cleared"] == ["Researcher"]
        sessions.reset.assert_called_once_with("channel:ch1:a1")
        # Messages and exchange_counts NOT cleared for single-agent scope
        assert len(ch.messages) == 2

    @pytest.mark.asyncio
    async def test_returns_404_for_unknown_agent_id(self):
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent", "agent_id": "nonexistent"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_returns_400_on_missing_body(self):
        """A request with no parseable body returns 400 (not a silent clear-all)."""
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request("ch1", {}, channel=ch, sessions=sessions)
        request.json = AsyncMock(side_effect=Exception("no body"))
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_returns_400_when_scope_agent_but_no_agent_id(self):
        """scope=agent without agent_id returns 400 (not a silent clear-all)."""
        agents = {"a1": _make_agent("a1", "Researcher", "channel:ch1:a1")}
        ch = _make_channel("ch1", agents)
        sessions = AsyncMock()

        request = _make_request(
            "ch1", {"scope": "agent"}, channel=ch, sessions=sessions
        )
        with patch(
            "kiro_crew.dashboard.handlers_channel._mgr",
            return_value=MagicMock(get=MagicMock(return_value=ch)),
        ):
            resp = await api_channel_clear_context(request)

        assert resp.status == 400
