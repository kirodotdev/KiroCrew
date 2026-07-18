"""Tests for project alias handlers, delete_run, planning placeholder, and 'project run' command."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.dashboard.handlers import api_taskrunner_export_yaml, api_taskrunner_status
from kiro_crew.dashboard.handlers_project import (
    _redact,
    _run_to_project,
    api_activities_list,
    api_comment_add,
    api_comment_delete,
    api_comments_list,
    api_project_create,
    api_project_delete,
    api_project_get,
    api_project_update,
    api_projects_list,
)
from kiro_crew.slack.handler import _handle_run_command
from kiro_crew.task_models import Project
from kiro_crew.taskrunner import TaskRunner

# ── Fixtures ──


def _make_run(task_id="run1", name="Test Run", status="completed", started_at=100.0):
    return Project(
        spec_path="/tmp/spec.md",
        spec_content="",
        task_id=task_id,
        name=name,
        status=status,
        started_at=started_at,
        source="dashboard",
    )


def _make_app(runner=None):
    app = web.Application()
    app["state"] = SimpleNamespace(task_runner=runner)
    return app


def _make_request(app, method="GET", path="/", match_info=None, json_body=None):
    req = make_mocked_request(method, path, app=app, match_info=match_info or {})
    if json_body is not None:
        req._payload = AsyncMock()
        req.json = AsyncMock(return_value=json_body)
    return req


def _make_mock_sessions():
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    sessions._lock = asyncio.Lock()
    sessions._sessions = {}
    sessions.get_or_create = AsyncMock()
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.cancel_current = AsyncMock()
    return sessions


# ── _redact / _run_to_project ──


class TestRedactAndConvert:
    def test_redact_plain_text(self):
        assert _redact("hello world") == "hello world"

    def test_run_to_project_basic(self):
        run = _make_run()
        result = _run_to_project(run)
        assert result["id"] == "run1"
        assert result["name"] == "Test Run"
        assert result["status"] == "completed"
        assert result["created_at"] == 100.0

    def test_run_to_project_none_description(self):
        run = _make_run()
        run.description = None
        result = _run_to_project(run)
        assert result["description"] == ""

    def test_run_to_project_description_falls_back_to_spec_content(self):
        run = _make_run()
        run.spec_content = "Do the thing"
        result = _run_to_project(run)
        assert result["description"] == "Do the thing"

    def test_run_to_project_description_falls_back_to_original_input(self):
        run = _make_run()
        run.original_input = "Build a widget"
        result = _run_to_project(run)
        assert result["description"] == "Build a widget"

    def test_run_to_project_description_prefers_spec_over_input(self):
        run = _make_run()
        run.spec_content = "spec text"
        run.original_input = "input text"
        result = _run_to_project(run)
        assert result["description"] == "spec text"

    def test_run_to_project_description_truncated(self):
        run = _make_run()
        run.spec_content = "x" * 5000
        result = _run_to_project(run)
        assert len(result["description"]) <= 4000

    def test_run_to_project_fallback_name(self):
        run = _make_run(name="")
        result = _run_to_project(run)
        assert result["name"] == "run1"


# ── handlers_project API ──


class TestProjectHandlers:
    @pytest.mark.asyncio
    async def test_list_no_runner(self):
        req = _make_request(_make_app(runner=None))
        resp = await api_projects_list(req)
        assert json.loads(resp.body) == []

    @pytest.mark.asyncio
    async def test_list_with_runs(self):
        runner = MagicMock()
        runner._runs = {
            "r1": _make_run("r1", started_at=200),
            "r2": _make_run("r2", started_at=100),
        }
        req = _make_request(_make_app(runner=runner))
        resp = await api_projects_list(req)
        data = json.loads(resp.body)
        assert len(data) == 2
        assert data[0]["id"] == "r1"  # sorted desc by started_at

    @pytest.mark.asyncio
    async def test_list_excludes_cron_source(self):
        runner = MagicMock()
        cron_run = Project(
            spec_path="/tmp/spec.md",
            spec_content="",
            task_id="cron1",
            name="Cron Task",
            status="completed",
            started_at=300.0,
            source="cron",
        )
        runner._runs = {
            "r1": _make_run("r1", started_at=200),
            "cron1": cron_run,
        }
        req = _make_request(_make_app(runner=runner))
        resp = await api_projects_list(req)
        data = json.loads(resp.body)
        assert len(data) == 1
        assert data[0]["id"] == "r1"

    @pytest.mark.asyncio
    async def test_get_found(self):
        runner = MagicMock()
        runner._runs = {"r1": _make_run("r1")}
        req = _make_request(_make_app(runner=runner), match_info={"id": "r1"})
        resp = await api_project_get(req)
        assert json.loads(resp.body)["id"] == "r1"

    @pytest.mark.asyncio
    async def test_get_not_found(self):
        runner = MagicMock()
        runner._runs = {}
        req = _make_request(_make_app(runner=runner), match_info={"id": "nope"})
        with pytest.raises(web.HTTPNotFound):
            await api_project_get(req)

    @pytest.mark.asyncio
    async def test_create_returns_400(self):
        req = _make_request(_make_app())
        resp = await api_project_create(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_update_renames(self):
        run = _make_run("r1")
        runner = MagicMock()
        runner._runs = {"r1": run}
        req = _make_request(
            _make_app(runner=runner),
            method="PUT",
            match_info={"id": "r1"},
            json_body={"name": "New"},
        )
        resp = await api_project_update(req)
        assert resp.status == 200
        assert run.name == "New"
        runner._persist_runs.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_not_found(self):
        runner = MagicMock()
        runner._runs = {}
        req = _make_request(
            _make_app(runner=runner), method="PUT", match_info={"id": "x"}, json_body={}
        )
        with pytest.raises(web.HTTPNotFound):
            await api_project_update(req)

    @pytest.mark.asyncio
    async def test_delete_success(self):
        runner = MagicMock()
        runner.delete_run.return_value = True
        req = _make_request(_make_app(runner=runner), match_info={"id": "r1"})
        resp = await api_project_delete(req)
        assert json.loads(resp.body) == {"ok": True}

    @pytest.mark.asyncio
    async def test_delete_not_found(self):
        runner = MagicMock()
        runner.delete_run.return_value = False
        req = _make_request(_make_app(runner=runner), match_info={"id": "x"})
        with pytest.raises(web.HTTPNotFound):
            await api_project_delete(req)

    @pytest.mark.asyncio
    async def test_stub_endpoints(self):
        req = _make_request(_make_app())
        assert json.loads((await api_activities_list(req)).body) == []
        assert (await api_comment_add(req)).status == 201
        assert json.loads((await api_comments_list(req)).body) == []
        assert json.loads((await api_comment_delete(req)).body) == {"ok": True}


# ── TaskRunner.delete_run ──


class TestDeleteRun:
    def test_delete_nonexistent(self, tmp_path):
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        assert runner.delete_run("nope") is False

    def test_delete_completed_run(self, tmp_path):
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        runner._runs["r1"] = _make_run("r1", status="completed")
        with patch.object(runner, "_persist_runs"):
            assert runner.delete_run("r1") is True
        assert "r1" not in runner._runs

    def test_delete_running_sets_cancelling(self, tmp_path):
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        run = _make_run("r1", status="running")
        runner._runs["r1"] = run
        mock_task = MagicMock()
        mock_task.done.return_value = False
        runner._tasks["r1"] = mock_task
        with patch.object(runner, "_persist_runs"):
            assert runner.delete_run("r1") is True
        assert "r1" not in runner._runs
        mock_task.cancel.assert_called_once()

    def test_delete_planning_cancels_bg_task(self, tmp_path):
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        runner._runs["r1"] = _make_run("r1", status="planning")
        mock_task = MagicMock()
        mock_task.done.return_value = False
        runner._tasks["r1"] = mock_task
        with patch.object(runner, "_persist_runs"):
            assert runner.delete_run("r1") is True
        assert "r1" not in runner._runs
        mock_task.cancel.assert_called_once()


# ── start_background planning placeholder ──


class TestStartBackgroundPlanning:
    @pytest.mark.asyncio
    async def test_planning_placeholder_created(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock):
            task_id = runner.start_background(spec, name="test-bg")
            assert task_id in runner._runs
            assert runner._runs[task_id].status == "planning"
            assert runner._runs[task_id].name == "test-bg"
            assert runner._runs[task_id].spec_content != ""  # eagerly read

    @pytest.mark.asyncio
    async def test_planning_placeholder_fails_on_error(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock, side_effect=ValueError("boom")):
            with patch.object(runner, "_persist_runs"):
                task_id = runner.start_background(spec)
                await asyncio.sleep(0.1)  # let _wrapped() run
                run = runner._runs.get(task_id)
                assert run is not None
                assert run.status == "failed"
                assert "boom" in run.error

    @pytest.mark.asyncio
    async def test_planning_placeholder_source_passed(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("# Test\n## Steps\n1. Do thing\n   - run: echo hi")
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock):
            task_id = runner.start_background(spec, source="mcp")
            assert runner._runs[task_id].source == "mcp"

    @pytest.mark.asyncio
    async def test_planning_placeholder_spec_content_truncated(self, tmp_path):
        spec = tmp_path / "spec.md"
        spec.write_text("x" * 10000)
        runner = TaskRunner(sessions=_make_mock_sessions(), work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock):
            task_id = runner.start_background(spec)
            assert len(runner._runs[task_id].spec_content) <= 4000


# ── _handle_run_command: project run alias ──


class TestProjectRunAlias:
    def test_project_run_normalizes(self):
        runner = MagicMock()
        runner.status.return_value = {"running": False, "runs": []}
        _handle_run_command("project run status", runner, MagicMock(), "C123", "ts123")
        runner.status.assert_called_once()

    def test_non_matching_returns_none(self):
        result = _handle_run_command("hello world", MagicMock(), MagicMock(), "C123", "ts")
        assert result is None


# ── api_taskrunner_status: mcp source filtering ──


class TestTaskRunnerStatusMcpFilter:
    @pytest.mark.asyncio
    async def test_taskrunner_status_excludes_cron_runs(self):
        from kiro_crew.task_reporter import build_status

        runner = MagicMock()
        cron_run = Project(
            spec_path="/tmp/spec.md",
            spec_content="",
            task_id="cron1",
            name="Cron Task",
            status="completed",
            started_at=300.0,
            source="cron",
        )
        dash_run = Project(
            spec_path="/tmp/spec.md",
            spec_content="",
            task_id="dash1",
            name="Dashboard Task",
            status="completed",
            started_at=200.0,
            source="dashboard",
        )
        mcp_run = Project(
            spec_path="/tmp/spec.md",
            spec_content="",
            task_id="mcp1",
            name="MCP Task",
            status="completed",
            started_at=100.0,
            source="mcp",
        )
        runner._runs = {"cron1": cron_run, "dash1": dash_run, "mcp1": mcp_run}
        runner._tasks = {}
        runner._agent = ""
        runner.status.return_value = build_status(runner._runs, runner._tasks, runner._agent)

        app = _make_app(runner=runner)
        req = _make_request(app, path="/api/taskrunner")
        resp = await api_taskrunner_status(req)
        data = json.loads(resp.body)
        task_ids = [r["task_id"] for r in data["runs"]]
        assert "cron1" not in task_ids
        assert "dash1" in task_ids
        assert "mcp1" in task_ids

    @pytest.mark.asyncio
    async def test_taskrunner_status_redacts_lessons_learned(self):
        # lessons_learned is LLM-generated text surfaced to the dashboard JSON;
        # it must be scrubbed of credentials like the sibling error / task_details
        # fields the handler already redacts (build_status emits it raw).
        from kiro_crew.task_reporter import build_status

        runner = MagicMock()
        run = Project(
            spec_path="/tmp/spec.md",
            spec_content="",
            task_id="dash1",
            name="Dashboard Task",
            status="completed",
            started_at=100.0,
            source="dashboard",
        )
        run.lessons_learned = [
            "Use token AKIAIOSFODNN7EXAMPLE for the build",
            "A second, clean lesson with no secrets",
        ]
        runner._runs = {"dash1": run}
        runner._tasks = {}
        runner._agent = ""
        runner.status.return_value = build_status(runner._runs, runner._tasks, runner._agent)

        app = _make_app(runner=runner)
        req = _make_request(app, path="/api/taskrunner")
        resp = await api_taskrunner_status(req)
        data = json.loads(resp.body)
        lessons = data["runs"][0]["lessons_learned"]
        joined = " ".join(lessons)
        assert "AKIAIOSFODNN7EXAMPLE" not in joined  # secret scrubbed
        assert "[REDACTED" in lessons[0]  # redaction marker present
        assert lessons[1] == "A second, clean lesson with no secrets"  # clean text intact, list shape preserved

    @pytest.mark.asyncio
    async def test_taskrunner_status_surfaces_default_workspace_dir(self, tmp_path):
        # The UI pre-fills its per-run workspace-folder selector from this field:
        # the configured workspace_dir when set, else the base work dir.
        from kiro_crew.task_reporter import build_status

        runner = MagicMock()
        runner._runs = {}
        runner._tasks = {}
        runner._agent = ""
        runner._workspace_dir = ""
        runner._work_dir = tmp_path
        runner.status.return_value = build_status(runner._runs, runner._tasks, runner._agent)

        app = _make_app(runner=runner)
        resp = await api_taskrunner_status(_make_request(app, path="/api/taskrunner"))
        assert json.loads(resp.body)["default_workspace_dir"] == str(tmp_path)

        # A configured workspace_dir takes precedence over the base work dir.
        runner._workspace_dir = "/srv/work"
        resp2 = await api_taskrunner_status(_make_request(app, path="/api/taskrunner"))
        assert json.loads(resp2.body)["default_workspace_dir"] == "/srv/work"


class TestTaskRunnerExportYaml:
    """GET /api/taskrunner/{task_id}/plan.yaml — plan → YAML download."""

    def _run_with_tasks(self, task_id="r1", name="My Plan"):
        from kiro_crew.task_models import Task

        run = Project(spec_path="", spec_content="", task_id=task_id, name=name, status="planned")
        run.tasks = [
            Task(index=1, title="Set up DB", description="create schema"),
            Task(index=2, title="Wire API", description="endpoints", depends_on=[1]),
        ]
        return run

    @pytest.mark.asyncio
    async def test_export_success_roundtrips(self):
        from kiro_crew.task_planner import decompose_yaml

        runner = MagicMock()
        runner._runs = {"r1": self._run_with_tasks()}
        req = _make_request(_make_app(runner=runner), match_info={"task_id": "r1"})
        resp = await api_taskrunner_export_yaml(req)
        assert resp.status == 200
        assert resp.content_type == "application/x-yaml"
        assert "attachment" in resp.headers["Content-Disposition"]
        assert 'filename="My_Plan.yaml"' in resp.headers["Content-Disposition"]
        body = resp.text
        assert "agents:" in body
        # The downloaded file re-imports to the same task graph.
        rt = decompose_yaml(body)
        assert [t.title for t in rt] == ["Set up DB", "Wire API"]
        assert rt[1].depends_on == [1]

    @pytest.mark.asyncio
    async def test_export_unknown_task_id_returns_generic_404(self):
        runner = MagicMock()
        runner._runs = {}
        req = _make_request(_make_app(runner=runner), match_info={"task_id": "nope"})
        resp = await api_taskrunner_export_yaml(req)
        assert resp.status == 404
        # Generic message — does not reflect the requested id.
        assert "nope" not in resp.body.decode()

    @pytest.mark.asyncio
    async def test_export_empty_plan_returns_409(self):
        run = Project(spec_path="", spec_content="", task_id="r1", name="Empty", status="planned")
        run.tasks = []
        runner = MagicMock()
        runner._runs = {"r1": run}
        req = _make_request(_make_app(runner=runner), match_info={"task_id": "r1"})
        resp = await api_taskrunner_export_yaml(req)
        assert resp.status == 409

    @pytest.mark.asyncio
    async def test_export_no_runner_returns_400(self):
        req = _make_request(_make_app(runner=None), match_info={"task_id": "r1"})
        resp = await api_taskrunner_export_yaml(req)
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_export_sanitizes_unsafe_filename(self):
        run = self._run_with_tasks(name='../../etc/pw"; drop')
        runner = MagicMock()
        runner._runs = {"r1": run}
        req = _make_request(_make_app(runner=runner), match_info={"task_id": "r1"})
        resp = await api_taskrunner_export_yaml(req)
        cd = resp.headers["Content-Disposition"]
        # No path separators, quotes, or spaces leak into the header.
        assert "/" not in cd.split("filename=")[1]
        assert '"' not in cd.split("filename=")[1].strip('"')
        assert ".." not in cd
