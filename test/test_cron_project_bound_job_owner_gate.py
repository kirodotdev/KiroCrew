"""Owner gate on a project-BOUND job's mutation and execution.

Gating only the ``project_path`` field itself (see
``test_cron_project_path_owner_gate.py``) protected the BINDING but not the
already-bound JOB: a non-owner could still rewrite an owner-bound job's
``message`` (unrelated field, no field-level gate) via ``PATCH
/api/crons/{id}``, or trigger it directly via ``POST /api/crons/{id}/run``
(no owner gate at all) -- either way the job later executes with
``job.project_path`` as its cwd and can read that project's files back,
without the request ever mentioning ``project_path``. These tests lock in
that BOTH routes require owner authorization once a job HAS a persisted
``project_path`` binding, regardless of which field the request body
touches.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_run, api_cron_update


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": job_id}
    attach_body(request, body)
    return request


def _run_request(crons: CronService, job_id: str) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": job_id}
    return request


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestProjectBoundJobUpdateOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_edit_message_on_a_bound_job(self, tmp_path):
        # The request never mentions project_path at all -- only message.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(_update_request({"message": "new"}, crons, job.id))
        assert resp.status == 403
        assert (
            crons.list_jobs()[0].message == "m"
        ), "a denied edit must leave the bound job's message unchanged"

    @pytest.mark.asyncio
    async def test_owner_can_still_edit_message_on_a_bound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(_update_request({"message": "new"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].message == "new"

    @pytest.mark.asyncio
    async def test_non_owner_can_still_edit_message_on_an_unbound_job(self, tmp_path):
        # No project_path binding on the job -- an ordinary edit must be
        # unaffected by this gate.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(_update_request({"message": "new"}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].message == "new"


class TestProjectBoundJobRunOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_trigger_a_bound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_run(_run_request(crons, job.id))
        assert resp.status == 403
        assert (
            job.id not in crons._running_tasks
        ), "a denied trigger must not start a run task at all"

    @pytest.mark.asyncio
    async def test_owner_can_still_trigger_a_bound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_run(_run_request(crons, job.id))
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_non_owner_can_still_trigger_an_unbound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_run(_run_request(crons, job.id))
        assert resp.status == 200
