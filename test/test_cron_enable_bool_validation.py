"""``POST /api/crons/{id}/enable`` honours only a real boolean ``enabled``.

The store keeps the value it is handed, so a string ``"false"`` (truthy) left the
job running while the route answered ``{"ok": true}``. The route now refuses a
non-boolean with the 400 ``invalid_enabled`` that ``POST /api/crons`` already
uses, and an absent field still means ``true``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_enable

pytestmark = pytest.mark.asyncio


def _enable_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": job_id}
    attach_body(request, body)
    return request


@pytest.mark.parametrize("value", ["false", "true", 0, None, [False]])
async def test_non_boolean_enabled_is_refused_and_changes_nothing(tmp_path, value) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)
    resp = await api_cron_enable(_enable_request({"enabled": value}, crons, job.id))
    assert resp.status == 400
    assert b"invalid_enabled" in resp.body
    stored = crons.list_jobs(include_disabled=True)[0]
    assert stored.enabled is True
    assert stored.user_paused is False


@pytest.mark.parametrize(("body", "expected"), [({"enabled": False}, False), ({}, True)])
async def test_boolean_or_absent_enabled_still_applies(tmp_path, body, expected) -> None:
    crons = CronService(base_dir=tmp_path)
    job = crons.add_job(name="n", message="m", every_secs=3600)
    if expected:
        crons.enable_job(job.id, enabled=False)
    resp = await api_cron_enable(_enable_request(body, crons, job.id))
    assert resp.status == 200
    assert crons.list_jobs(include_disabled=True)[0].enabled is expected
