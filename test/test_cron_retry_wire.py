"""`last_retry_count` reaches the `GET /api/crons` wire.

A run that succeeded only after transient retries showed nothing about it: the
attempt counter lived on a runtime attribute the gateway callback cleared in its
own `finally` once the retry chain unwound, so nothing about a successful retry
reached the store, let alone the dashboard. Mirrors
test_dashboard_cron_result_visibility.py's harness.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.dashboard.handlers.cron import api_crons


def _request_with_job(**job_kw) -> MagicMock:
    defaults = dict(
        id="cj1",
        name="flaky-job",
        message="",
        schedule=CronSchedule(kind="every", every_secs=60),
        command="echo hello",
        last_status="ok",
    )
    defaults.update(job_kw)
    job = CronJob(**defaults)

    state = MagicMock()
    state.crons.list_jobs.return_value = [job]
    state.crons.list_jobs_async = AsyncMock(return_value=[job])
    state.crons.is_running.return_value = False
    state.crons.running_since.return_value = None
    state.has_slot.return_value = False
    request = MagicMock()
    request.app = {"state": state}
    return request


async def _job_payload(**job_kw) -> dict:
    resp = await api_crons(_request_with_job(**job_kw))
    assert resp.status == 200
    return json.loads(resp.body)["jobs"][0]


class TestRetryTelemetryOnTheWire:
    @pytest.mark.asyncio
    async def test_retry_count_and_ts_are_serialized(self) -> None:
        payload = await _job_payload(last_retry_count=2)
        assert payload["last_retry_count"] == 2

    @pytest.mark.asyncio
    async def test_a_clean_run_reports_zero_and_none(self) -> None:
        payload = await _job_payload()
        assert payload["last_retry_count"] == 0
