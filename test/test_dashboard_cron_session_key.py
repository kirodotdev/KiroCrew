"""Tests for `session_key` in the GET /api/crons payload.

The owning session decides chat-side reachability (cron_list only lists a
session its own jobs), and the Schedule page is the surface a user looks at
when a job is "missing" from chat — so the serializer must carry the owner.
Mirrors test_dashboard_cron_folder_id.py's list-response test.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_crons


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


def _list_request(crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    state.has_slot = MagicMock(return_value=False)
    request = MagicMock()
    request.app = {"state": state}
    return request


class TestCronListSessionKey:
    """GET /api/crons must serialize the owning session on every job."""

    @pytest.mark.asyncio
    async def test_owned_job_serializes_session_key(self):
        crons = CronService()
        crons.add_job("owned", "hello", every_secs=3600, session_key="web-abc123")
        resp = await api_crons(_list_request(crons))
        assert resp.status == 200
        body = json.loads(resp.body)
        assert body["jobs"][0]["session_key"] == "web-abc123"

    @pytest.mark.asyncio
    async def test_ownerless_job_serializes_none_not_omitted(self):
        """An ownerless job must serialize the field as None, never omit it —
        the empty state is the one that explains why the job is invisible to
        cron_list in chat, so the frontend needs a value to branch on."""
        crons = CronService()
        crons.add_job("ownerless", "hello", every_secs=3600, session_key="")
        resp = await api_crons(_list_request(crons))
        assert resp.status == 200
        job = json.loads(resp.body)["jobs"][0]
        assert "session_key" in job
        assert job["session_key"] is None
