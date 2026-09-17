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

    @pytest.mark.asyncio
    async def test_non_owner_agent_change_on_a_bound_job_is_refused_before_the_probe(
        self, tmp_path
    ):
        """The 403 must answer FIRST; the save-time agent probe must not run at all.

        ``PATCH {"agent": "<anything>"}`` on an owner-bound job moves the binding,
        and the save-time probe's refusal names the folder it resolved against --
        the same absolute ``project_path`` the list endpoint withholds from a
        non-owner. A probe answered before the owner gate would therefore hand a
        non-owner that value in a 400, and it would also answer the yes/no
        question "does this folder declare an agent called X" (400 when it does
        not, 403 when it does). The probe is skipped for a non-owner on a bound
        job and the request falls straight through to the store's owner refusal.
        """
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(
            name="n", message="m", every_secs=3600, agent_id="a", project_path=str(tmp_path)
        )
        # A verdict, not a raise: the probe swallows its own failures as "let the
        # save through", so a raising stub could not show the leak. An UNRESOLVED
        # verdict is what the disclosing 400 is built from.
        probe = MagicMock(return_value=MagicMock(requested_resolved=False))
        with (
            patch(
                "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
                lambda request: False,
            ),
            patch("kiro_crew.dashboard.handlers.cron.resolve_agent_bindings", probe),
        ):
            resp = await api_cron_update(_update_request({"agent": "ghost-agent"}, crons, job.id))
        assert resp.status == 403, resp.body
        body = resp.body.decode()
        assert str(tmp_path) not in body, "the refusal disclosed the bound folder"
        assert "project_bound_job_owner_required" in body
        probe.assert_not_called()
        assert crons.list_jobs()[0].agent_id == "a"

    @pytest.mark.asyncio
    async def test_non_owner_agent_change_on_an_unbound_job_is_still_probed(self, tmp_path):
        # Control: the probe still guards an UNBOUND job's edit for a non-owner --
        # there is no folder to disclose there, and the refusal wording has none.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, agent_id="a")
        with (
            patch(
                "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
                lambda request: False,
            ),
            patch(
                "kiro_crew.dashboard.handlers.cron.resolve_agent_bindings",
                return_value=MagicMock(requested_resolved=False),
            ),
        ):
            resp = await api_cron_update(_update_request({"agent": "ghost-agent"}, crons, job.id))
        assert resp.status == 400
        body = resp.body.decode()
        assert "unknown_agent" in body
        assert "project directory" not in body
        assert crons.list_jobs()[0].agent_id == "a"


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
        assert job.id not in crons._claims, "a denied trigger must not start a run task at all"

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
