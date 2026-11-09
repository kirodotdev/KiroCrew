"""Dashboard admission and serialization for cron project directories."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from body_stream_helpers import attach_body

from kiro_crew.cron import CronJob, CronProjectBoundRefusal, CronSchedule, CronService
from kiro_crew.dashboard.handlers import api_cron_update, api_crons, api_crons_create
from kiro_crew.dashboard.handlers.cron_jobs import api_cron_enable
from kiro_crew.dashboard.handlers.cron_runs import api_cron_run


def _state(job: CronJob | None = None) -> MagicMock:
    state = MagicMock()
    state.has_slot.return_value = False
    made = job or CronJob(
        id="job1",
        name="nightly",
        message="run",
        schedule=CronSchedule(kind="every", every_secs=300),
    )
    state.crons.add_job_async = AsyncMock(return_value=made)
    state.crons.update_job_async = AsyncMock(return_value=made)
    state.crons.enable_job_async = AsyncMock(return_value=True)
    state.crons.get_job_async = AsyncMock(return_value=made)
    state.crons.get_job.return_value = made
    state.crons.list_jobs_async = AsyncMock(return_value=[made])
    state.crons.running_since.return_value = None
    state.crons.is_running.return_value = False
    return state


def _request(body: dict, state: MagicMock, *, app: str = "") -> MagicMock:
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"job_id": "job1"}
    request.get.side_effect = lambda key, default=None: app if key == "app" else default
    attach_body(request, body)
    return request


@pytest.fixture
def owner_gate():
    with (
        patch(
            "kiro_crew.dashboard.handlers._shared.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "kiro_crew.dashboard.handlers.cron_jobs.require_owner_dashboard_request",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "kiro_crew.dashboard.handlers.cron_jobs._project_agent_unavailable",
            new=AsyncMock(return_value=False),
        ),
    ):
        yield


class TestCronProjectPathCreate:
    @pytest.mark.asyncio
    async def test_owner_can_create_a_project_template_job(self, tmp_path, owner_gate):
        project = str(tmp_path.resolve())
        state = _state()
        request = _request(
            {
                "name": "nightly",
                "message": "run",
                "every": 300,
                "agent": "repo-agent",
                "agent_kind": "template",
                "project_path": project,
            },
            state,
        )

        response = await api_crons_create(request)

        assert response.status == 200
        _, kwargs = state.crons.add_job_async.call_args
        assert kwargs["agent_id"] == "repo-agent"
        assert kwargs["member_id"] == ""
        assert kwargs["agent_kind"] == "template"
        assert kwargs["project_path"] == project

    @pytest.mark.asyncio
    async def test_project_template_kind_prevents_same_name_member_capture(
        self, tmp_path, owner_gate
    ):
        state = _state()
        request = _request(
            {
                "name": "nightly",
                "message": "run",
                "every": 300,
                "agent": "shared-name",
                "agent_kind": "template",
                "project_path": str(tmp_path.resolve()),
            },
            state,
        )

        with patch("kiro_crew.cron_service.identity.split_cron_agent_member") as split:
            response = await api_crons_create(request)

        assert response.status == 200
        split.assert_not_called()

    @pytest.mark.asyncio
    async def test_app_cannot_add_a_project_directory(self, tmp_path):
        state = _state()
        request = _request(
            {
                "name": "nightly",
                "message": "run",
                "every": 300,
                "project_path": str(tmp_path.resolve()),
            },
            state,
            app="calendar",
        )

        response = await api_crons_create(request)

        assert response.status == 403
        assert json.loads(response.body)["code"] == "project_path_owner_required"
        state.crons.add_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_relative_project_directory_is_refused(self, owner_gate):
        state = _state()
        request = _request(
            {
                "name": "nightly",
                "message": "run",
                "every": 300,
                "project_path": "relative/project",
            },
            state,
        )

        response = await api_crons_create(request)

        assert response.status == 400
        assert json.loads(response.body)["code"] == "invalid_project_path"
        state.crons.add_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unknown_project_agent_is_refused_before_the_job_is_created(
        self, tmp_path, owner_gate
    ):
        state = _state()
        request = _request(
            {
                "name": "nightly",
                "message": "run",
                "every": 300,
                "agent": "missing-agent",
                "agent_kind": "template",
                "project_path": str(tmp_path.resolve()),
            },
            state,
        )

        with patch(
            "kiro_crew.dashboard.handlers.cron_jobs._project_agent_unavailable",
            new=AsyncMock(return_value=True),
        ):
            response = await api_crons_create(request)

        assert response.status == 400
        assert json.loads(response.body)["code"] == "unknown_agent"
        state.crons.add_job_async.assert_not_awaited()


class TestCronProjectPathUpdate:
    @pytest.mark.asyncio
    async def test_owner_can_set_and_clear_the_directory(self, tmp_path, owner_gate):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
        )
        state = _state(job)
        project = str(tmp_path.resolve())

        response = await api_cron_update(_request({"project_path": project}, state))
        assert response.status == 200
        assert state.crons.update_job_async.await_args.kwargs["project_path"] == project

        response = await api_cron_update(_request({"project_path": ""}, state))
        assert response.status == 200
        assert state.crons.update_job_async.await_args.kwargs["project_path"] == ""

    @pytest.mark.asyncio
    async def test_binding_a_directory_leaves_the_agent_namespace_alone(self, tmp_path, owner_gate):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            agent_id="shared-name",
        )
        state = _state(job)

        response = await api_cron_update(
            _request({"project_path": str(tmp_path.resolve()), "agent": "shared-name"}, state)
        )

        assert response.status == 200
        assert "agent_kind" not in state.crons.update_job_async.await_args.kwargs

    @pytest.mark.asyncio
    async def test_an_edit_that_omits_the_kind_keeps_a_bound_alias_resolvable(
        self, tmp_path, owner_gate
    ):
        project = tmp_path / "project"
        project.mkdir()
        home = tmp_path / "data"
        service = CronService(base_dir=home)
        job = service.add_job(
            "nightly",
            "run",
            every_secs=300,
            agent_id="default",
            project_path=str(project),
        )
        state = MagicMock()
        state.crons = service

        request = _request({"name": "renamed", "agent": "default"}, state)
        request.match_info = {"job_id": job.id}
        response = await api_cron_update(request)

        assert response.status == 200
        stored = CronService(base_dir=home).get_job(job.id)
        assert stored is not None
        assert (stored.name, stored.agent_id, stored.agent_kind) == ("renamed", "default", "")

    @pytest.mark.asyncio
    async def test_an_edit_that_omits_the_kind_keeps_a_stored_template_marker(
        self, tmp_path, owner_gate
    ):
        project = tmp_path / "project"
        project.mkdir()
        home = tmp_path / "data"
        service = CronService(base_dir=home)
        job = service.add_job(
            "nightly",
            "run",
            every_secs=300,
            agent_id="repo-agent",
            project_path=str(project),
        )
        service.update_job(job.id, agent_kind="template")
        state = MagicMock()
        state.crons = service

        request = _request({"name": "renamed", "agent": "repo-agent"}, state)
        request.match_info = {"job_id": job.id}
        response = await api_cron_update(request)

        assert response.status == 200
        stored = CronService(base_dir=home).get_job(job.id)
        assert stored is not None
        assert stored.agent_kind == "template"

    @pytest.mark.asyncio
    async def test_an_explicit_template_pick_records_the_custom_agent_namespace(
        self, tmp_path, owner_gate
    ):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            agent_id="old-agent",
            project_path=str(tmp_path.resolve()),
        )
        state = _state(job)

        response = await api_cron_update(
            _request({"agent": "shared-name", "agent_kind": "template"}, state)
        )

        assert response.status == 200
        kwargs = state.crons.update_job_async.await_args.kwargs
        assert kwargs["agent_id"] == "shared-name"
        assert kwargs["agent_kind"] == "template"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["", "member", None, 1])
    async def test_an_update_cannot_state_another_kind(self, kind, owner_gate):
        state = _state()

        response = await api_cron_update(_request({"agent": "x", "agent_kind": kind}, state))

        assert response.status == 400
        assert json.loads(response.body)["code"] == "invalid_agent_kind"
        state.crons.update_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_template_kind_needs_the_agent_it_describes(self, owner_gate):
        state = _state()

        response = await api_cron_update(_request({"agent_kind": "template"}, state))

        assert response.status == 400
        assert json.loads(response.body)["code"] == "agent_kind_without_agent"
        state.crons.update_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stored_member, body_member", [("member-1", None), ("", "member-1")])
    async def test_a_template_kind_cannot_name_a_bot(self, stored_member, body_member, owner_gate):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            member_id=stored_member,
        )
        state = _state(job)
        body: dict = {"agent": "x", "agent_kind": "template"}
        if body_member is not None:
            body["member_id"] = body_member

        response = await api_cron_update(_request(body, state))

        assert response.status == 400
        assert json.loads(response.body)["code"] == "agent_kind_conflict"
        state.crons.update_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_agent_change_conflicts_with_a_concurrent_project_bind(
        self, tmp_path, owner_gate
    ):
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        home = tmp_path / "data"
        service = CronService(base_dir=home)
        job = service.add_job(
            "nightly",
            "run",
            every_secs=300,
            agent_id="old-agent",
            project_path=str(first),
        )
        state = MagicMock()
        state.crons = service
        external = CronService(base_dir=home)

        async def move_project(_agent_id: str, _project_path: str) -> bool:
            await asyncio.to_thread(
                external.update_job,
                job.id,
                project_path=str(second),
            )
            return False

        with patch(
            "kiro_crew.dashboard.handlers.cron_jobs._project_agent_unavailable",
            new=move_project,
        ):
            request = _request({"agent": "new-agent"}, state)
            request.match_info = {"job_id": job.id}
            response = await api_cron_update(request)

        assert response.status == 409
        assert json.loads(response.body)["code"] == "stale_request"
        stored = CronService(base_dir=home).get_job(job.id)
        assert stored is not None
        assert (stored.project_path, stored.agent_id) == (str(second), "old-agent")
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_change_from_unbound_conflicts_with_a_concurrent_project_bind(
        self, tmp_path, owner_gate
    ):
        project = tmp_path / "project"
        project.mkdir()
        home = tmp_path / "data"
        service = CronService(base_dir=home)
        job = service.add_job(
            "nightly",
            "run",
            every_secs=300,
            agent_id="old-agent",
        )
        state = MagicMock()
        state.crons = service
        external = CronService(base_dir=home)
        update_job = service.update_job_async

        async def bind_then_update(job_id: str, **kwargs):
            await external.update_job_async(job.id, project_path=str(project))
            return await update_job(job_id, **kwargs)

        with patch.object(service, "update_job_async", side_effect=bind_then_update):
            request = _request({"agent": "new-agent"}, state)
            request.match_info = {"job_id": job.id}
            response = await api_cron_update(request)

        assert response.status == 409
        assert json.loads(response.body)["code"] == "stale_request"
        stored = CronService(base_dir=home).get_job(job.id)
        assert stored is not None
        assert (stored.project_path, stored.agent_id) == (str(project), "old-agent")
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_clearing_the_directory_leaves_the_agent_namespace_alone(self, owner_gate):
        state = _state()

        response = await api_cron_update(_request({"project_path": ""}, state))

        assert response.status == 200
        assert "agent_kind" not in state.crons.update_job_async.await_args.kwargs

    @pytest.mark.asyncio
    async def test_member_job_cannot_gain_a_project_directory(self, tmp_path, owner_gate):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            member_id="private-member",
        )
        state = _state(job)

        response = await api_cron_update(_request({"project_path": str(tmp_path.resolve())}, state))

        assert response.status == 400
        assert json.loads(response.body)["code"] == "project_path_member_conflict"
        state.crons.update_job_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_app_cannot_update_a_project_directory(self, tmp_path):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            created_by="app:calendar",
        )
        state = _state(job)

        response = await api_cron_update(
            _request(
                {"project_path": str(tmp_path.resolve())},
                state,
                app="calendar",
            )
        )

        assert response.status == 403
        assert json.loads(response.body)["code"] == "project_path_owner_required"
        state.crons.update_job_async.assert_not_awaited()


class TestCronProjectPathRun:
    @pytest.mark.asyncio
    async def test_internal_agent_trigger_refuses_a_project_bound_job(self, tmp_path):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            project_path=str(tmp_path.resolve()),
        )
        state = _state(job)
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"job_id": "job1"}
        request.rel_url.query = {"require_unbound": "1"}
        request.get.side_effect = lambda key, default=None: (
            True if key == "internal_auth" else default
        )
        state.crons.trigger_run.side_effect = CronProjectBoundRefusal()

        response = await api_cron_run.__wrapped__(request)

        assert response.status == 403
        assert json.loads(response.body)["code"] == "project_bound_owner_action_required"
        assert state.crons.trigger_run.call_args.kwargs["require_unbound"] is True
        assert isinstance(state.crons.trigger_run.call_args.kwargs["admission"], asyncio.Future)
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_unrelated_run_permission_error_is_not_a_project_refusal(self, tmp_path):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
        )
        state = _state(job)
        state.crons.trigger_run.side_effect = PermissionError("crons.json is unreadable")
        request = MagicMock()
        request.app = {"state": state}
        request.match_info = {"job_id": "job1"}
        request.rel_url.query = {"require_unbound": "1"}
        request.get.side_effect = lambda key, default=None: (
            True if key == "internal_auth" else default
        )

        with pytest.raises(PermissionError, match="crons.json is unreadable"):
            await api_cron_run.__wrapped__(request)

        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_app_receives_and_audits_a_fresh_store_binding_refusal(self, tmp_path):
        home = tmp_path / "data"
        service = CronService(base_dir=home)
        job = service.add_job(
            "nightly",
            "run",
            every_secs=300,
            created_by="app:calendar",
        )

        external = CronService(base_dir=home)

        async def stale_lookup(job_id: str):
            await asyncio.to_thread(
                external.update_job,
                job_id,
                project_path=str(tmp_path.resolve()),
            )
            return job

        service.get_job_async = stale_lookup  # type: ignore[method-assign]
        state = MagicMock()
        state.crons = service
        request = _request({}, state, app="calendar")
        request.match_info = {"job_id": job.id}
        request.rel_url.query = {}

        with patch("kiro_crew.dashboard.handlers.cron_runs._audit_app_cron") as audit:
            response = await api_cron_run.__wrapped__(request)

        assert response.status == 403
        assert json.loads(response.body)["code"] == "project_bound_owner_action_required"
        assert not service.is_running(job.id)
        audit.assert_called_once_with("calendar", "crons.run", "denied", job.id)
        state.push_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_app_rest_cannot_resume_or_run_a_project_bound_job(self, tmp_path):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            project_path=str(tmp_path.resolve()),
            created_by="app:calendar",
        )
        state = _state(job)
        state.crons.enable_job_async.side_effect = CronProjectBoundRefusal()
        state.crons.trigger_run.side_effect = CronProjectBoundRefusal()
        enable_request = _request({"enabled": True}, state, app="calendar")
        run_request = _request({}, state, app="calendar")
        run_request.rel_url.query = {}

        enable_response = await api_cron_enable.__wrapped__(enable_request)
        run_response = await api_cron_run.__wrapped__(run_request)

        assert enable_response.status == 403
        assert run_response.status == 403
        assert json.loads(enable_response.body)["code"] == ("project_bound_owner_action_required")
        assert json.loads(run_response.body)["code"] == "project_bound_owner_action_required"
        state.crons.enable_job_async.assert_awaited_once_with(
            "job1", enabled=True, require_unbound=True
        )
        assert state.crons.trigger_run.call_args.kwargs["require_unbound"] is True
        assert isinstance(state.crons.trigger_run.call_args.kwargs["admission"], asyncio.Future)

    @pytest.mark.asyncio
    async def test_unrelated_resume_permission_error_is_not_a_project_refusal(self):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            created_by="app:calendar",
        )
        state = _state(job)
        state.crons.enable_job_async.side_effect = PermissionError("crons.json is unwritable")

        with pytest.raises(PermissionError, match="crons.json is unwritable"):
            await api_cron_enable.__wrapped__(_request({"enabled": True}, state, app="calendar"))

        state.push_refresh.assert_not_called()


class TestCronProjectPathList:
    @pytest.mark.asyncio
    async def test_only_the_owner_receives_the_host_path(self, tmp_path):
        job = CronJob(
            id="job1",
            name="nightly",
            message="run",
            schedule=CronSchedule(kind="every", every_secs=300),
            project_path=str(tmp_path.resolve()),
        )
        state = _state(job)
        request = _request({}, state)

        with patch(
            "kiro_crew.dashboard.handlers.cron_board.is_owner_dashboard_request",
            return_value=True,
        ):
            owner_response = await api_crons(request)
        with patch(
            "kiro_crew.dashboard.handlers.cron_board.is_owner_dashboard_request",
            return_value=False,
        ):
            other_response = await api_crons(request)

        assert json.loads(owner_response.body)["jobs"][0]["project_path"] == job.project_path
        assert json.loads(other_response.body)["jobs"][0]["project_path"] is None
