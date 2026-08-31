"""Tests for dashboard cron PATCH handler's agent body-key mapping.

The dashboard UI's cron editor sends `{"agent": "..."}` in the PATCH body, but
the handler's kwargs allowlist used to only accept `"agent_id"`. That caused
agent changes to be silently dropped: save returned 200 OK, the UI refetched,
saw the unchanged `agent: null`, and reverted the dropdown to default.

These tests lock in the accepted-key mapping so the regression cannot return.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.dashboard.handlers import api_cron_update


def _make_request(body: dict, job_id: str = "abc123") -> MagicMock:
    mock_state = MagicMock()
    mock_job = MagicMock()
    mock_job.id = job_id
    mock_state.crons.update_job_async = AsyncMock(return_value=mock_job)

    request = MagicMock()
    request.app = {"state": mock_state}
    request.match_info = {"job_id": job_id}
    attach_body(request, body)
    return request


class TestCronUpdateAgent:
    @pytest.mark.asyncio
    async def test_agent_key_maps_to_agent_id_kwarg(self):
        """UI sends 'agent'; handler must translate it to the internal 'agent_id' kwarg."""
        request = _make_request({"agent": "bxt-brain-leader"})

        resp = await api_cron_update(request)

        assert resp.status == 200
        update_job_async = request.app["state"].crons.update_job_async
        update_job_async.assert_called_once()
        _, kwargs = update_job_async.call_args
        assert kwargs.get("agent_id") == "bxt-brain-leader"
        # Never pass 'agent' through to update_job_async (it is not an accepted kwarg).
        assert "agent" not in kwargs

    @pytest.mark.asyncio
    async def test_agent_id_key_still_accepted_for_scripted_callers(self):
        """Scripted callers that already use 'agent_id' must keep working."""
        request = _make_request({"agent_id": "bxt-brain-worker"})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == "bxt-brain-worker"

    @pytest.mark.asyncio
    async def test_agent_wins_over_agent_id_when_both_present(self):
        """If a caller sends both, the UI-shaped 'agent' key takes precedence."""
        request = _make_request({"agent": "winner", "agent_id": "loser"})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == "winner"

    @pytest.mark.asyncio
    async def test_empty_agent_clears_assignment(self):
        """Sending 'agent': '' must clear the agent assignment (revert to default)."""
        request = _make_request({"agent": ""})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        # Empty string is a legitimate update value (clear the agent).
        assert kwargs.get("agent_id") == ""

    @pytest.mark.asyncio
    async def test_other_fields_still_patched_alongside_agent(self):
        """Agent mapping must not disturb the existing name/message/channel path."""
        request = _make_request(
            {
                "name": "renamed",
                "message": "new msg",
                "agent": "bxt-brain-leader",
                "channel": "C0AP77JJSN6",
                "approval_mode": "auto",
                "silent": True,
            }
        )

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("name") == "renamed"
        assert kwargs.get("message") == "new msg"
        assert kwargs.get("agent_id") == "bxt-brain-leader"
        assert kwargs.get("channel") == "C0AP77JJSN6"
        assert kwargs.get("approval_mode") == "auto"
        assert kwargs.get("silent") is True

    @pytest.mark.asyncio
    async def test_missing_agent_does_not_touch_agent_id(self):
        """Patches that don't mention agent must not overwrite the current value."""
        request = _make_request({"name": "renamed"})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert "agent_id" not in kwargs

    @pytest.mark.asyncio
    async def test_agent_value_is_stripped(self):
        """Whitespace-padded agent values are stripped, matching api_crons_create."""
        request = _make_request({"agent": "  bxt-brain-leader  "})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == "bxt-brain-leader"

    @pytest.mark.asyncio
    async def test_agent_id_value_is_stripped(self):
        """The scripted 'agent_id' path is also stripped for consistency."""
        request = _make_request({"agent_id": "\tbxt-brain-worker\n"})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == "bxt-brain-worker"

    @pytest.mark.asyncio
    async def test_null_agent_coerces_to_empty_string(self):
        """JSON null for 'agent' is coerced to empty string, clearing the assignment."""
        request = _make_request({"agent": None})

        resp = await api_cron_update(request)

        assert resp.status == 200
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == ""

    @pytest.mark.asyncio
    async def test_job_not_found_returns_404(self):
        """When update_job_async returns None (unknown job_id), handler returns 404."""
        request = _make_request({"agent": "bxt-brain-leader"}, job_id="missing")
        request.app["state"].crons.update_job_async = AsyncMock(return_value=None)

        resp = await api_cron_update(request)

        assert resp.status == 404
        # Kwargs are still built and passed; the 404 comes from the return value, not input validation.
        request.app["state"].crons.update_job_async.assert_called_once()
        _, kwargs = request.app["state"].crons.update_job_async.call_args
        assert kwargs.get("agent_id") == "bxt-brain-leader"
