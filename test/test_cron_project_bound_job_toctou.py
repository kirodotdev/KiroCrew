"""Owner gate on RE-ENABLING a project-bound job, and the update-path TOCTOU.

Two closely related follow-on findings after the create/update/run gates
(see ``test_cron_project_bound_job_owner_gate.py``):

1. ``api_cron_enable`` had NO owner gate at all -- a non-owner could
   re-enable an owner-disabled project-bound job, handing it back to the
   scheduler, which fires it against ``job.project_path`` the same as a
   manual trigger. Disabling carries no equivalent risk (it stops execution
   rather than starting it), so only the re-enable direction is gated.

2. The owner-authorization decision in ``api_cron_update`` (and now
   ``api_cron_enable``) reads the job's CURRENT ``project_path`` outside any
   lock, then applies the mutation in a SEPARATE later lock acquisition. A
   concurrent owner bind/unbind landing in that gap would let a non-owner's
   already-authorized (against the stale snapshot) request execute against
   a binding it was never actually checked against. Closed with an
   ``expect_project_path`` precondition re-verified atomically UNDER the
   lock, mirroring the existing ``expect_secret_env`` compare-and-swap.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronPendingMismatch, CronService
from kiro_crew.dashboard.handlers import api_cron_enable, api_cron_update


def _enable_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    state = MagicMock()
    state.crons = crons
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": job_id}
    attach_body(request, body)
    return request


def _update_request(body: dict, crons: CronService, job_id: str) -> MagicMock:
    return _enable_request(body, crons, job_id)


@pytest.fixture(autouse=True)
def _isolate_cron_store(monkeypatch, tmp_path):
    monkeypatch.setattr("kiro_crew.cron._DEFAULT_DIR", tmp_path)
    yield


class TestProjectBoundJobEnableOwnerGate:
    @pytest.mark.asyncio
    async def test_non_owner_cannot_reenable_a_bound_disabled_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(
            name="n",
            message="m",
            every_secs=3600,
            project_path=str(tmp_path),
            enabled=False,
        )
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_enable(_enable_request({"enabled": True}, crons, job.id))
        assert resp.status == 403
        assert (
            crons.list_jobs(include_disabled=True)[0].enabled is False
        ), "a denied re-enable must leave the job disabled"

    @pytest.mark.asyncio
    async def test_owner_can_still_reenable_a_bound_disabled_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(
            name="n",
            message="m",
            every_secs=3600,
            project_path=str(tmp_path),
            enabled=False,
        )
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: True,
        ):
            resp = await api_cron_enable(_enable_request({"enabled": True}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].enabled is True

    @pytest.mark.asyncio
    async def test_non_owner_can_still_disable_a_bound_job(self, tmp_path):
        # Only the RE-ENABLE direction is gated -- disabling stops execution
        # rather than starting it, so it must be unaffected.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_enable(_enable_request({"enabled": False}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs(include_disabled=True)[0].enabled is False

    @pytest.mark.asyncio
    async def test_non_owner_can_still_reenable_an_unbound_job(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, enabled=False)
        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_enable(_enable_request({"enabled": True}, crons, job.id))
        assert resp.status == 200
        assert crons.list_jobs()[0].enabled is True


class TestUpdatePathBindingToctou:
    @pytest.mark.asyncio
    async def test_a_concurrent_bind_between_check_and_write_is_rejected_not_applied(
        self,
        tmp_path,
    ):
        # Simulates the race: the outer gate reads an UNBOUND snapshot (so a
        # non-owner's message edit is allowed through), but by the time the
        # actual mutation reaches the locked core, an owner has concurrently
        # bound the job to a project. The precondition must catch this and
        # refuse, rather than let the non-owner's edit land on the
        # newly-bound job.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)

        real_get_job_async = crons.get_job_async

        async def _get_job_then_mutate_concurrently(job_id: str):
            result = await real_get_job_async(job_id)
            # A snapshot returned to a DIFFERENT concurrent request would be
            # an independent CronJob instance (each request's own disk
            # read/parse produces its own objects) -- copy here so the
            # mutation below cannot retroactively change the value this
            # request already captured, which would defeat the point of the
            # simulation.
            snapshot = dataclasses.replace(result)
            # Simulate the concurrent owner bind landing right after the
            # snapshot read this handler's gate just took, before the
            # handler's own later update_job_async call.
            crons._update_job_locked(job_id, project_path=str(tmp_path))
            return snapshot

        crons.get_job_async = _get_job_then_mutate_concurrently  # type: ignore[method-assign]

        with patch(
            "kiro_crew.dashboard.handlers.cron.is_owner_dashboard_request",
            lambda request: False,
        ):
            resp = await api_cron_update(_update_request({"message": "new"}, crons, job.id))
        assert resp.status == 409
        assert (
            crons.list_jobs()[0].message == "m"
        ), "the stale-precondition rejection must leave the message unchanged"

    def test_the_locked_core_raises_on_a_project_path_mismatch(self, tmp_path):
        # Direct unit check of the precondition itself, independent of the
        # handler-level race simulation above.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        with pytest.raises(CronPendingMismatch):
            crons._update_job_locked(job.id, message="new", expect_project_path="")

    def test_the_locked_core_accepts_a_matching_project_path_precondition(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        updated = crons._update_job_locked(
            job.id,
            message="new",
            expect_project_path=str(tmp_path),
        )
        assert updated is not None
        assert updated.message == "new"


class TestRunPathBindingToctou:
    """``run_job`` re-syncs its OWN fresh snapshot from disk once its task
    actually starts, independently of the REST handler's earlier check -- so
    the handler's "no await between check and dispatch" property (which does
    close the DISPATCH itself against interleaving) does NOT close this
    second, later read inside the dispatched task. Confirmed by reading
    ``run_job``'s actual body: ``_synced_snapshot(True)`` is a fresh disk
    read, not the snapshot the handler already checked.
    """

    @pytest.mark.asyncio
    async def test_run_refuses_when_the_binding_changed_before_the_task_actually_starts(
        self,
        tmp_path,
    ):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        # Simulate the owner binding the project between the handler's check
        # (which saw it unbound) and run_job's own later re-sync.
        crons._update_job_locked(job.id, project_path=str(tmp_path))
        ok = await crons.run_job(job.id, expect_project_path="")
        assert ok is False
        assert (
            job.id not in crons._executing
        ), "a refused run must never actually execute against the newly-bound project"

    @pytest.mark.asyncio
    async def test_run_still_executes_when_the_binding_is_unchanged(self, tmp_path):
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600, project_path=str(tmp_path))
        ok = await crons.run_job(job.id, expect_project_path=str(tmp_path))
        assert ok is True

    @pytest.mark.asyncio
    async def test_run_is_unaffected_without_a_precondition(self, tmp_path):
        # The owner's own request path passes no precondition at all -- an
        # unbound job must run normally regardless.
        crons = CronService(base_dir=tmp_path)
        job = crons.add_job(name="n", message="m", every_secs=3600)
        ok = await crons.run_job(job.id)
        assert ok is True
