"""Tests for dashboard cron handler channel validation."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.dashboard.handlers import api_crons_create


class TestCronCreateChannel:
    @pytest.mark.asyncio
    async def test_valid_channel_accepted(self):
        mock_state = MagicMock()
        mock_job = MagicMock()
        mock_job.id = "abc"
        mock_job.agent_id = ""
        mock_state.crons.add_job_async = AsyncMock(return_value=mock_job)
        request = MagicMock()
        request.app = {"state": mock_state}
        attach_body(
            request,
            {"name": "test", "message": "msg", "every": 300, "channel": "C0AP77JJSN6"},
        )
        resp = await api_crons_create(request)
        assert resp.status == 200
        mock_state.crons.add_job_async.assert_called_once()
        call_kwargs = mock_state.crons.add_job_async.call_args
        assert (
            call_kwargs[1].get("channel") == "C0AP77JJSN6"
            or call_kwargs.kwargs.get("channel") == "C0AP77JJSN6"
        )

    @pytest.mark.asyncio
    async def test_invalid_channel_rejected(self):
        mock_state = MagicMock()
        request = MagicMock()
        request.app = {"state": mock_state}
        attach_body(
            request,
            {"name": "test", "message": "msg", "every": 300, "channel": "not-valid"},
        )
        resp = await api_crons_create(request)
        assert resp.status == 400
        mock_state.crons.add_job_async.assert_not_called()
