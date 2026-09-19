"""Owner gate on ``project_path`` for cron create/update.

``project_path`` binds a job's agent to an arbitrary cwd at fire time, so it
fires the agent against, and can return, that project's files. Before this
fix, ``POST /api/crons`` and ``PATCH /api/crons/{id}`` accepted a non-empty
``project_path`` from ANY caller: an allow-listed non-owner dashboard token
(``app == ""``, which sails through every app-token check) could create or
edit a job binding its cwd to another project and later read that project's
files back through the job's output. This is the write-side counterpart to
the read-side gate on ``GET /api/agents?project_path`` (see
``test_agents_project_path_owner_gate.py``): both routes now require
``is_owner_dashboard_request`` for a non-empty ``project_path``, but they
disagree on treatment by design -- the read-side fallback silently ignores a
non-owner's ``project_path`` (a probe must not learn "gate exists" from an
error, since the field is optional there), while these mutating routes
reject outright with 403 (a create/update has no silent-success shape that
also drops the field, and the caller must clearly hear that the write did not
happen as requested rather than persist a job with an empty ``project_path``
they thought was set).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons_create


def _create_request(body: dict, crons: CronService) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    attach_body(request, body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    request = _create_request(body, crons)
    request.match_info = {"job_id": job_id}
    return request


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestCronCreateProjectPathOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_create_with_project_path_is_denied(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        request = _create_request(
            {"name": "n", "message": "m", "every": 3600, "project_path": str(tmp_path)},
            crons,
        )
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_crons_create(request)
        assert resp.status == 403
        assert crons.list_jobs() == [], (
            "a denied create must not persist a job at all, not a job with "
            "project_path silently dropped"
        )

    @pytest.mark.asyncio
    async def test_owner_create_with_project_path_still_succeeds(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        request = _create_request(
            {"name": "n", "message": "m", "every": 3600, "project_path": str(tmp_path)},
            crons,
        )
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_crons_create(request)
        assert resp.status == 200
        jobs = crons.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].project_path == str(tmp_path)

    @pytest.mark.asyncio
    async def test_non_owner_create_without_project_path_still_succeeds(self, tmp_path):
        # The gate is scoped to a non-empty project_path -- an ordinary
        # non-owner create with no project_path at all must be unaffected.
        crons = CronService(base_dir=tmp_path)
        request = _create_request({"name": "n", "message": "m", "every": 3600}, crons)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_crons_create(request)
        assert resp.status == 200


class TestCronUpdateProjectPathOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_update_with_project_path_is_denied(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(
                _update_request({"project_path": str(tmp_path)}, crons, job.id)
            )
        assert resp.status == 403
        assert crons.list_jobs()[0].project_path == "", (
            "a denied update must leave the job's project_path unset, not " "silently apply it"
        )

    @pytest.mark.asyncio
    async def test_owner_update_with_project_path_still_succeeds(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(
                _update_request({"project_path": str(tmp_path)}, crons, job.id)
            )
        assert resp.status == 200
        assert crons.list_jobs()[0].project_path == str(tmp_path)

    @pytest.mark.asyncio
    async def test_non_owner_update_clearing_project_path_is_denied(self, tmp_path):
        # An empty-string project_path on update CLEARS an existing binding --
        # a privileged mutation of the same field as setting it, so it must
        # be gated the same way. Gating on the validated value's truthiness
        # (rather than the field's presence in the body) let this slip past
        # the check entirely: caught by review.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(_update_request({"project_path": ""}, crons, job.id))
        assert resp.status == 403
        assert crons.list_jobs()[0].project_path == str(
            tmp_path
        ), "a denied clear must leave the existing owner-set binding intact"

    @pytest.mark.asyncio
    async def test_owner_update_can_clear_project_path(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_update(_update_request({"project_path": ""}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].project_path == ""

    @pytest.mark.asyncio
    async def test_non_owner_update_without_project_path_still_succeeds(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(_update_request({"name": "renamed"}, crons, job.id))
        assert resp.status == 200
