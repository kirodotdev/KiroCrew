"""Tests for the prompt optimizer endpoint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard.handlers.optimizer import (
    OPTIMIZER_SYSTEM,
    handle_optimize,
)


class TestOptimizerSystem:
    """Test the system prompt content."""

    def test_system_prompt_contains_length_limit(self):
        assert "250 words" in OPTIMIZER_SYSTEM

    def test_system_prompt_contains_preservation_rule(self):
        assert "preserve existing behavior" in OPTIMIZER_SYSTEM

    def test_system_prompt_mentions_scope_constraint(self):
        assert "scope" in OPTIMIZER_SYSTEM.lower()

    def test_system_prompt_mentions_structure(self):
        assert "structure" in OPTIMIZER_SYSTEM.lower()


class TestOptimizerEndpoint:
    """Test the handle_optimize handler logic."""

    @pytest.mark.asyncio
    async def test_empty_prompt_returns_unchanged(self):
        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "", "context": ""})

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["changed"] is False
        assert data["optimized"] == ""

    @pytest.mark.asyncio
    async def test_invalid_json_returns_400(self):
        request = MagicMock()
        request.json = AsyncMock(side_effect=ValueError("bad json"))

        resp = await handle_optimize(request)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unchanged_response_from_llm(self):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        mock_client = AsyncMock()

        async def fake_stream(prompt):
            yield MagicMock(kind=EVENT_TEXT_CHUNK, text="UNCHANGED")
            yield MagicMock(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "refactor the auth module to be cleaner", "context": ""})
        request.app = {"state": mock_state}

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["changed"] is False
        assert data["optimized"] == "refactor the auth module to be cleaner"

    @pytest.mark.asyncio
    async def test_optimized_response_from_llm(self):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        mock_client = AsyncMock()
        optimized_text = "Refactor the auth module: extract token validation into a separate service."

        async def fake_stream(prompt):
            yield MagicMock(kind=EVENT_TEXT_CHUNK, text=optimized_text)
            yield MagicMock(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "refactor the auth module to be cleaner", "context": ""})
        request.app = {"state": mock_state}

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["changed"] is True
        assert data["optimized"] == optimized_text

    @pytest.mark.asyncio
    async def test_short_prompt_still_optimized(self):
        """Explicit user action means even short prompts get optimized."""
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        mock_client = AsyncMock()

        async def fake_stream(prompt):
            yield MagicMock(kind=EVENT_TEXT_CHUNK, text="Confirm and proceed with the previous action.")
            yield MagicMock(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "yes", "context": ""})
        request.app = {"state": mock_state}

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["changed"] is True
        assert data["optimized"] == "Confirm and proceed with the previous action."

    @pytest.mark.asyncio
    async def test_llm_error_returns_original(self):
        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(side_effect=RuntimeError("LLM unavailable"))

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "refactor the auth module", "context": ""})
        request.app = {"state": mock_state}

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["changed"] is False
        assert data["optimized"] == "refactor the auth module"

    @pytest.mark.asyncio
    async def test_quoted_response_stripped(self):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        mock_client = AsyncMock()

        async def fake_stream(prompt):
            yield MagicMock(kind=EVENT_TEXT_CHUNK, text='"Refactor the auth module cleanly"')
            yield MagicMock(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "refactor the auth module", "context": ""})
        request.app = {"state": mock_state}

        resp = await handle_optimize(request)
        data = json.loads(resp.body)
        assert data["optimized"] == "Refactor the auth module cleanly"

    @pytest.mark.asyncio
    async def test_context_truncated_to_2000_chars(self):
        from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK

        mock_client = AsyncMock()
        captured_prompt = []

        async def fake_stream(prompt):
            captured_prompt.append(prompt)
            yield MagicMock(kind=EVENT_TEXT_CHUNK, text="optimized result")
            yield MagicMock(kind=EVENT_COMPLETE)

        mock_client.stream = fake_stream

        mock_sessions = MagicMock()
        mock_sessions.get_or_create = AsyncMock(return_value=(mock_client, True, False))
        mock_sessions.release = MagicMock()

        mock_state = MagicMock()
        mock_state.sessions = mock_sessions

        long_context = "A" * 3000 + "B" * 2000
        request = MagicMock()
        request.json = AsyncMock(return_value={"prompt": "refactor the auth module to be better", "context": long_context})
        request.app = {"state": mock_state}

        await handle_optimize(request)
        # Context should be truncated to last 2000 chars (all B's)
        assert "B" * 2000 in captured_prompt[0]
        assert "A" * 3000 not in captured_prompt[0]
