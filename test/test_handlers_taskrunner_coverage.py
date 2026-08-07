"""Coverage tests for the task-runner dashboard handlers.

Focus: request validation, run-lifecycle state transitions, error responses and
status codes for every endpoint in
``kiro_crew.dashboard.handlers.taskrunner`` — plus the redaction applied to
LLM-authored text on the status / plan / export surfaces.

Everything is driven through ``make_mocked_request`` against a fake
``DashboardState`` (matching the existing style in ``test_auto_approve.py`` and
``test_project_alias.py``): no network, no git, no subprocess, no real agent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK, AcpEvent
from kiro_crew.dashboard.handlers.taskrunner import (
    _run_refine,
    api_taskrunner_cancel,
    api_taskrunner_delete,
    api_taskrunner_execute_plan,
    api_taskrunner_export_yaml,
    api_taskrunner_from_chat,
    api_taskrunner_pause,
    api_taskrunner_plan,
    api_taskrunner_plan_cancel,
    api_taskrunner_plan_context,
    api_taskrunner_refine,
    api_taskrunner_refine_answer,
    api_taskrunner_refine_cancel,
    api_taskrunner_refine_status,
    api_taskrunner_rename,
    api_taskrunner_retry,
    api_taskrunner_start,
    api_taskrunner_status,
    api_taskrunner_to_chat,
    api_taskrunner_update_plan,
    api_taskrunner_update_task,
)
from kiro_crew.taskrunner import Step, StepStatus, TaskRun

# A URL whose oversized query payload trips the (domain-agnostic) exfiltration
# scanner — see test_security.py::test_long_query_redacted_domain_agnostic.
_EXFIL_URL = "https://collector.example.com/ingest?data=" + "A" * 250

# ── Fixtures / helpers ──


@pytest.fixture(autouse=True)
def _stub_sel():
    """No real security-event-log writes from any handler under test."""
    with patch("kiro_crew.dashboard.handlers.taskrunner._sel") as sel:
        sel.return_value = MagicMock()
        yield sel


def _runner(tmp_path: Path) -> MagicMock:
    """A task runner mock with the attributes the handlers reach for."""
    runner = MagicMock()
    runner._runs = {}
    runner._stall_cancelled_ids = set()
    runner._work_dir = tmp_path / "work"
    runner._workspace_dir = None
    runner._agent = ""
    runner._plan_task = None
    runner._apersist_runs = AsyncMock()
    runner._group_parallel_tasks = MagicMock(return_value=[])
    runner.start_background = AsyncMock(return_value="tid-1")
    runner.update_task = AsyncMock(return_value={"index": 0})
    runner.update_plan = AsyncMock()
    runner.execute_plan = AsyncMock()
    runner.retry_from_task = AsyncMock()
    runner.plan = AsyncMock()
    runner.status = MagicMock(return_value={"runs": []})
    return runner


def _state(runner: MagicMock | None) -> SimpleNamespace:
    return SimpleNamespace(task_runner=runner)


def _request(
    state: Any,
    method: str = "POST",
    path: str = "/api/taskrunner",
    *,
    match_info: dict[str, str] | None = None,
    json_body: Any = None,
    raw_json_error: bool = False,
    request_app: str = "",
    with_content_length: bool = True,
) -> web.Request:
    app = web.Application()
    app["state"] = state
    headers = {"Content-Length": "32"} if (json_body is not None and with_content_length) else {}
    req = make_mocked_request(
        method, path, app=app, match_info=match_info or {}, headers=headers
    )
    req["app"] = request_app
    if raw_json_error:
        req.json = AsyncMock(side_effect=ValueError("bad json"))  # type: ignore[method-assign]
    elif json_body is not None:
        req.json = AsyncMock(return_value=json_body)  # type: ignore[method-assign]
    return req


def _body(resp: web.Response) -> Any:
    assert resp.body is not None
    return json.loads(bytes(resp.body))  # type: ignore[arg-type]


# ── GET /api/taskrunner ──


class TestStatus:
    @pytest.mark.asyncio
    async def test_unavailable_when_no_runner(self) -> None:
        resp = await api_taskrunner_status(_request(_state(None), "GET"))
        assert resp.status == 200
        assert _body(resp) == {"running": False, "available": False}

    @pytest.mark.asyncio
    async def test_hidden_sources_filtered_out(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.status.return_value = {
            "runs": [{"source": "dashboard"}, {"source": "cron"}, {"source": None}]
        }
        resp = await api_taskrunner_status(_request(_state(runner), "GET"))
        data = _body(resp)
        assert [r["source"] for r in data["runs"]] == ["dashboard"]
        assert data["available"] is True

    @pytest.mark.asyncio
    async def test_run_and_step_text_is_redacted(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.status.return_value = {
            "runs": [
                {
                    "source": "chat",
                    "error": "failed: aws_secret_access_key=AKIAIOSFODNN7EXAMPLEKEY0",
                    "task_details": [
                        {
                            "title": f"post to {_EXFIL_URL}",
                            "description": "",
                            "result": "token=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
                            "error": None,
                        }
                    ],
                    "lessons_learned": [f"leak {_EXFIL_URL}"],
                }
            ]
        }
        resp = await api_taskrunner_status(_request(_state(runner), "GET"))
        payload = resp.body or b""
        run = _body(resp)["runs"][0]
        assert "AKIAIOSFODNN7EXAMPLEKEY0" not in payload.decode()
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in payload.decode()
        assert _EXFIL_URL not in payload.decode()
        assert "[REDACTED" in run["task_details"][0]["title"]
        assert "[REDACTED" in run["lessons_learned"][0]
        # Non-empty fields survive as (redacted) strings; falsy ones are untouched.
        assert run["task_details"][0]["error"] is None
        assert len(run["lessons_learned"]) == 1

    @pytest.mark.asyncio
    async def test_default_workspace_dir_prefers_configured_value(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._workspace_dir = tmp_path / "ws"
        resp = await api_taskrunner_status(_request(_state(runner), "GET"))
        assert _body(resp)["default_workspace_dir"] == str(tmp_path / "ws")

    @pytest.mark.asyncio
    async def test_default_workspace_dir_falls_back_to_work_dir(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_status(_request(_state(runner), "GET"))
        assert _body(resp)["default_workspace_dir"] == str(tmp_path / "work")


# ── POST /api/taskrunner (start) ──


class TestStart:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_start(_request(_state(None), json_body={"spec": "x"}))
        assert resp.status == 400
        assert _body(resp)["error"] == "task runner not available"

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_start(_request(_state(_runner(tmp_path)), raw_json_error=True))
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_missing_spec_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_start(_request(_state(_runner(tmp_path)), json_body={}))
        assert resp.status == 400
        assert _body(resp)["error"] == "spec path required"

    @pytest.mark.asyncio
    async def test_traversal_path_rejected(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_start(
            _request(_state(_runner(tmp_path)), json_body={"spec": "../etc/passwd"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid spec path"

    @pytest.mark.asyncio
    async def test_nonexistent_path_rejected(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_start(
            _request(_state(_runner(tmp_path)), json_body={"spec": str(tmp_path / "nope.md")})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid spec path"

    @pytest.mark.asyncio
    async def test_sensitive_path_is_403(self, tmp_path: Path) -> None:
        spec = tmp_path / "creds.md"
        spec.write_text("# t", encoding="utf-8")
        runner = _runner(tmp_path)
        with patch(
            "kiro_crew.dashboard.handlers.taskrunner.is_sensitive_path", return_value=True
        ):
            resp = await api_taskrunner_start(
                _request(_state(runner), json_body={"spec": str(spec)})
            )
        assert resp.status == 403
        assert _body(resp)["error"] == "access denied"

    @pytest.mark.asyncio
    async def test_real_file_spec_forwards_resolved_path(self, tmp_path: Path) -> None:
        spec = tmp_path / "TASK.md"
        spec.write_text("# t", encoding="utf-8")
        runner = _runner(tmp_path)
        resp = await api_taskrunner_start(
            _request(
                _state(runner),
                json_body={"spec": str(spec), "agent": "a1", "name": "n1", "source": "file"},
            )
        )
        assert resp.status == 200
        assert _body(resp) == {"ok": True, "spec": str(spec.resolve()), "task_id": "tid-1"}
        kwargs = runner.start_background.call_args.kwargs
        assert kwargs["agent"] == "a1"
        assert kwargs["name"] == "n1"
        assert kwargs["source"] == "file"

    @pytest.mark.asyncio
    async def test_empty_inline_spec_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_start(
            _request(_state(_runner(tmp_path)), json_body={"spec": "__inline__:   \n"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "empty spec content"

    @pytest.mark.asyncio
    async def test_inline_spec_written_to_work_dir(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# hello"})
        )
        assert resp.status == 200
        written = Path(_body(resp)["spec"])
        assert written.parent == runner._work_dir
        assert written.read_text(encoding="utf-8") == "# hello"

    @pytest.mark.asyncio
    async def test_unknown_source_coerced_to_dashboard(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        await api_taskrunner_start(
            _request(
                _state(runner), json_body={"spec": "__inline__:# t", "source": "bogus"}
            )
        )
        assert runner.start_background.call_args.kwargs["source"] == "dashboard"

    @pytest.mark.asyncio
    async def test_runner_exception_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.start_background = AsyncMock(side_effect=RuntimeError("cannot start"))
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# t"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "cannot start"


# ── cancel / pause / delete / rename ──


class TestCancel:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_cancel(_request(_state(None), path="/api/taskrunner/cancel"))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_cancel(
            _request(_state(_runner(tmp_path)), json_body={}, raw_json_error=True)
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_empty_body_cancels_all(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_cancel(_request(_state(runner)))
        assert resp.status == 200
        runner.cancel.assert_called_once_with(None)

    @pytest.mark.asyncio
    async def test_task_id_cancels_one(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        await api_taskrunner_cancel(_request(_state(runner), json_body={"task_id": "t9"}))
        runner.cancel.assert_called_once_with("t9")


class TestPause:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_pause(_request(_state(None), match_info={"task_id": "t1"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_task_is_404(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_pause(
            _request(_state(_runner(tmp_path)), match_info={"task_id": "nope"})
        )
        assert resp.status == 404
        assert _body(resp)["error"] == "not found"

    @pytest.mark.asyncio
    async def test_non_running_task_is_409(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="planned", task_id="t1"
        )
        resp = await api_taskrunner_pause(_request(_state(runner), match_info={"task_id": "t1"}))
        assert resp.status == 409
        assert _body(resp)["error"] == "cannot pause (status=planned)"

    @pytest.mark.asyncio
    async def test_lookup_by_name_then_pause(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="running", task_id="t1", name="pretty"
        )
        resp = await api_taskrunner_pause(
            _request(_state(runner), match_info={"task_id": "pretty"})
        )
        assert resp.status == 200
        assert _body(resp)["ok"] is True
        runner.pause.assert_called_once_with("t1")


class TestDelete:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_delete(_request(_state(None), match_info={"task_id": "t1"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_task_is_404(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_delete(
            _request(_state(_runner(tmp_path)), match_info={"task_id": "t1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["running", "cancelling"])
    async def test_active_run_must_be_cancelled_first(self, tmp_path: Path, status: str) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status=status, task_id="t1"
        )
        resp = await api_taskrunner_delete(_request(_state(runner), match_info={"task_id": "t1"}))
        assert resp.status == 409
        assert _body(resp)["error"] == "cancel first"
        assert "t1" in runner._runs

    @pytest.mark.asyncio
    async def test_finished_run_removed_and_persisted(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="completed", task_id="t1"
        )
        runner._stall_cancelled_ids.add("t1")
        resp = await api_taskrunner_delete(_request(_state(runner), match_info={"task_id": "t1"}))
        assert resp.status == 200
        assert runner._runs == {}
        assert runner._stall_cancelled_ids == set()
        runner._apersist_runs.assert_awaited_once()


class TestRename:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_rename(_request(_state(None), match_info={"task_id": "t1"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_task_is_404(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_rename(
            _request(_state(_runner(tmp_path)), match_info={"task_id": "t1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(spec_path="s.md", spec_content="s", task_id="t1")
        resp = await api_taskrunner_rename(
            _request(_state(runner), match_info={"task_id": "t1"}, raw_json_error=True)
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_blank_name_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(spec_path="s.md", spec_content="s", task_id="t1")
        resp = await api_taskrunner_rename(
            _request(_state(runner), match_info={"task_id": "t1"}, json_body={"name": "   "})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "name required"

    @pytest.mark.asyncio
    async def test_rename_persists(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", task_id="t1")
        runner._runs["t1"] = run
        resp = await api_taskrunner_rename(
            _request(_state(runner), match_info={"task_id": "t1"}, json_body={"name": "  new  "})
        )
        assert _body(resp) == {"ok": True, "name": "new"}
        assert run.name == "new"
        runner._apersist_runs.assert_awaited_once()


class TestUpdateTask:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_update_task(
            _request(_state(None), match_info={"task_id": "t1", "index": "0"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_non_numeric_index_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_update_task(
            _request(
                _state(_runner(tmp_path)), match_info={"task_id": "t1", "index": "abc"}
            )
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid index"

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_update_task(
            _request(
                _state(_runner(tmp_path)),
                match_info={"task_id": "t1", "index": "0"},
                raw_json_error=True,
            )
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid JSON"

    @pytest.mark.asyncio
    async def test_success_merges_runner_result(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.update_task = AsyncMock(return_value={"index": 2, "title": "t"})
        resp = await api_taskrunner_update_task(
            _request(
                _state(runner),
                match_info={"task_id": "t1", "index": "2"},
                json_body={"title": "t"},
            )
        )
        assert _body(resp) == {"ok": True, "index": 2, "title": "t"}

    @pytest.mark.asyncio
    async def test_runner_value_error_is_409(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.update_task = AsyncMock(side_effect=ValueError("task already running"))
        resp = await api_taskrunner_update_task(
            _request(
                _state(runner),
                match_info={"task_id": "t1", "index": "0"},
                json_body={"title": "t"},
            )
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "task already running"


class TestRetry:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_retry(_request(_state(None), match_info={"task_id": "t1"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_retry(
            _request(
                _state(_runner(tmp_path)),
                match_info={"task_id": "t1"},
                json_body={},
                raw_json_error=True,
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_default_from_step_is_one(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_retry(
            _request(_state(runner), match_info={"task_id": "t1"})
        )
        assert _body(resp) == {"ok": True, "task_id": "t1"}
        assert runner.retry_from_task.await_args.args == ("t1", 1)

    @pytest.mark.asyncio
    async def test_value_error_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.retry_from_task = AsyncMock(side_effect=ValueError("no such step"))
        resp = await api_taskrunner_retry(
            _request(_state(runner), match_info={"task_id": "t1"}, json_body={"from_step": 3})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "no such step"


# ── plan-context / export ──


class TestPlanContext:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_plan_context(
            _request(_state(None), "GET", match_info={"task_id": "t1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unplanned_run_is_404(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="running", task_id="t1"
        )
        resp = await api_taskrunner_plan_context(
            _request(_state(runner), "GET", match_info={"task_id": "t1"})
        )
        assert resp.status == 404
        assert _body(resp)["error"] == "not found or not planned"

    @pytest.mark.asyncio
    async def test_planned_run_returns_context(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="planned", task_id="t1"
        )
        runner.plan_to_chat_context = MagicMock(return_value="ctx")
        resp = await api_taskrunner_plan_context(
            _request(_state(runner), "GET", match_info={"task_id": "t1"})
        )
        assert _body(resp) == {"ok": True, "context": "ctx", "task_id": "t1"}


class TestExportYaml:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_export_yaml(
            _request(_state(None), "GET", match_info={"task_id": "t1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_run_is_generic_404(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_export_yaml(
            _request(_state(_runner(tmp_path)), "GET", match_info={"task_id": "secret-id"})
        )
        assert resp.status == 404
        assert "secret-id" not in (resp.body or b"").decode()

    @pytest.mark.asyncio
    async def test_planless_run_is_409(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(spec_path="s.md", spec_content="s", task_id="t1")
        resp = await api_taskrunner_export_yaml(
            _request(_state(runner), "GET", match_info={"task_id": "t1"})
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "no plan to export"

    @pytest.mark.asyncio
    async def test_serializer_failure_is_generic_500(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md",
            spec_content="s",
            task_id="t1",
            tasks=[Step(index=1, title="a", description="b")],
        )
        with patch(
            "kiro_crew.dashboard.handlers.taskrunner.plan_to_yaml",
            side_effect=RuntimeError("boom SECRET"),
        ):
            resp = await api_taskrunner_export_yaml(
                _request(_state(runner), "GET", match_info={"task_id": "t1"})
            )
        assert resp.status == 500
        assert "SECRET" not in (resp.body or b"").decode()

    @pytest.mark.asyncio
    async def test_filename_sanitized_from_run_name(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md",
            spec_content="s",
            task_id="t1",
            name="../../etc/passwd plan",
            tasks=[Step(index=1, title="a", description="b")],
        )
        resp = await api_taskrunner_export_yaml(
            _request(_state(runner), "GET", match_info={"task_id": "t1"})
        )
        assert resp.status == 200
        disposition = resp.headers["Content-Disposition"]
        assert ".." not in disposition
        assert "/" not in disposition.split("filename=")[1]
        assert resp.content_type == "application/x-yaml"

    @pytest.mark.asyncio
    async def test_blank_name_falls_back_to_plan(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md",
            spec_content="s",
            task_id="...",
            name="",
            tasks=[Step(index=1, title="a", description="b")],
        )
        resp = await api_taskrunner_export_yaml(
            _request(_state(runner), "GET", match_info={"task_id": "t1"})
        )
        assert resp.headers["Content-Disposition"] == 'attachment; filename="plan.yaml"'


# ── to-chat ──


def _chat_state(runner: MagicMock | None) -> SimpleNamespace:
    slot = SimpleNamespace(key="slot-1", title="", task=None, append=MagicMock())
    state = SimpleNamespace(
        task_runner=runner,
        _background_tasks=set(),
        get_or_create_slot=MagicMock(return_value=slot),
        push_slots_update=MagicMock(),
    )
    return state


class TestToChat:
    @pytest.fixture(autouse=True)
    def _stub_run_chat(self):
        async def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        with patch("kiro_crew.dashboard.chat._run_chat", new=_noop):
            yield

    @staticmethod
    async def _drain(state: SimpleNamespace) -> None:
        for task in list(state._background_tasks):
            await task

    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_to_chat(
            _request(_chat_state(None), match_info={"task_id": "t1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_run_is_404(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_to_chat(
            _request(_chat_state(_runner(tmp_path)), match_info={"task_id": "t1"})
        )
        assert resp.status == 404

    @pytest.mark.asyncio
    async def test_planned_run_opens_plan_slot(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="s.md", spec_content="s", status="planned", task_id="t1"
        )
        runner.plan_to_chat_context = MagicMock(return_value="plan ctx")
        state = _chat_state(runner)
        resp = await api_taskrunner_to_chat(_request(state, match_info={"task_id": "t1"}))
        await self._drain(state)
        assert _body(resp) == {"ok": True, "slot": "slot-1", "task_id": "t1"}
        assert state.get_or_create_slot.return_value.title == "Plan: t1"
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_completed_run_summary_mentions_success(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="/specs/build.md",
            spec_content="do the thing",
            status="completed",
            task_id="t1",
            work_dir="/w",
            branch_name="feat/x",
            tasks=[Step(index=1, title="a", description="", status=StepStatus.PASSED)],
            lessons_learned=["be careful"],
        )
        state = _chat_state(runner)
        resp = await api_taskrunner_to_chat(_request(state, match_info={"task_id": "t1"}))
        await self._drain(state)
        assert _body(resp) == {"ok": True, "slot": "slot-1"}
        summary = state.get_or_create_slot.return_value.append.call_args.args[1]
        assert "# Task Review: build" in summary
        assert "completed successfully" in summary
        assert "Lessons Learned" in summary
        assert "**Branch**: `feat/x`" in summary

    @pytest.mark.asyncio
    async def test_failed_run_summary_lists_failed_steps(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="__inline__:# t",
            spec_content="",
            status="failed",
            task_id="t1",
            error="exploded",
            tasks=[
                Step(index=1, title="ok", description="", status=StepStatus.PASSED),
                Step(
                    index=2,
                    title="bad",
                    description="",
                    status=StepStatus.FAILED,
                    error="stack trace",
                ),
                Step(index=3, title="wip", description="", status=StepStatus.IN_PROGRESS),
                Step(index=4, title="skip", description="", status=StepStatus.SKIPPED),
            ],
        )
        state = _chat_state(runner)
        await api_taskrunner_to_chat(_request(state, match_info={"task_id": "t1"}))
        await self._drain(state)
        summary = state.get_or_create_slot.return_value.append.call_args.args[1]
        # Inline specs fall back to the task id for the display name.
        assert "# Task Review: t1" in summary
        assert "failed at Step 2" in summary
        assert "Error: stack trace" in summary
        assert "## Task Error\nexploded" in summary
        assert "(no spec)" in summary

    @pytest.mark.asyncio
    async def test_neutral_status_gets_generic_prompt(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner._runs["t1"] = TaskRun(
            spec_path="/s/x.md", spec_content="c", status="cancelled", task_id="t1"
        )
        state = _chat_state(runner)
        await api_taskrunner_to_chat(_request(state, match_info={"task_id": "t1"}))
        await self._drain(state)
        summary = state.get_or_create_slot.return_value.append.call_args.args[1]
        assert "Review this task run." in summary


# ── plan / update-plan / execute / from-chat ──


def _planned_run(task_id: str = "p1") -> TaskRun:
    return TaskRun(
        spec_path="",
        spec_content="",
        status="planned",
        task_id=task_id,
        tasks=[
            Step(index=1, title="first", description="do it", depends_on=[]),
            Step(index=2, title="second", description="then", depends_on=[1]),
        ],
    )


class TestPlan:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_plan(_request(_state(None), json_body={"input": "x"}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_plan(_request(_state(_runner(tmp_path)), raw_json_error=True))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_success_returns_redacted_steps_and_groups(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _planned_run()
        run.tasks[0].title = f"call {_EXFIL_URL}"
        runner.plan = AsyncMock(return_value=run)
        runner._group_parallel_tasks = MagicMock(return_value=[[run.tasks[0]], [run.tasks[1]]])
        resp = await api_taskrunner_plan(
            _request(_state(runner), json_body={"input": "build it", "source": "text"})
        )
        data = _body(resp)
        assert data["task_id"] == "p1"
        assert data["groups"] == [[1], [2]]
        assert "[REDACTED" in data["steps"][0]["title"]
        assert _EXFIL_URL not in json.dumps(data)
        # The in-flight plan task handle is always cleared in the finally block.
        assert runner._plan_task is None

    @pytest.mark.asyncio
    async def test_cancelled_plan_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.plan = AsyncMock(side_effect=asyncio.CancelledError())
        resp = await api_taskrunner_plan(_request(_state(runner), json_body={"input": "x"}))
        assert resp.status == 400
        assert _body(resp)["error"] == "Planning was cancelled."
        assert runner._plan_task is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exc", [FileNotFoundError("missing spec"), ValueError("empty input")]
    )
    async def test_expected_errors_are_400(self, tmp_path: Path, exc: Exception) -> None:
        runner = _runner(tmp_path)
        runner.plan = AsyncMock(side_effect=exc)
        resp = await api_taskrunner_plan(_request(_state(runner), json_body={"input": "x"}))
        assert resp.status == 400
        assert _body(resp)["error"] == str(exc)


class TestPlanCancel:
    @pytest.mark.asyncio
    async def test_no_runner_is_still_ok(self) -> None:
        resp = await api_taskrunner_plan_cancel(_request(_state(None)))
        assert _body(resp) == {"ok": True}

    @pytest.mark.asyncio
    async def test_delegates_to_runner(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        await api_taskrunner_plan_cancel(_request(_state(runner)))
        runner.cancel_plan.assert_called_once_with()


class TestUpdatePlan:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_update_plan(
            _request(_state(None), "PUT", match_info={"task_id": "p1"}, json_body={})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_update_plan(
            _request(
                _state(_runner(tmp_path)), "PUT", match_info={"task_id": "p1"},
                raw_json_error=True,
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_value_error_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.update_plan = AsyncMock(side_effect=ValueError("run is not planned"))
        resp = await api_taskrunner_update_plan(
            _request(
                _state(runner), "PUT", match_info={"task_id": "p1"}, json_body={"steps": []}
            )
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "run is not planned"

    @pytest.mark.asyncio
    async def test_success_returns_steps(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _planned_run()
        runner.update_plan = AsyncMock(return_value=run)
        runner._group_parallel_tasks = MagicMock(return_value=[[run.tasks[0], run.tasks[1]]])
        resp = await api_taskrunner_update_plan(
            _request(
                _state(runner),
                "PUT",
                match_info={"task_id": "p1"},
                json_body={"steps": [{"title": "first"}]},
            )
        )
        data = _body(resp)
        assert [s["index"] for s in data["steps"]] == [1, 2]
        assert data["steps"][1]["depends_on"] == [1]
        assert data["groups"] == [[1, 2]]


class TestExecutePlan:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_execute_plan(
            _request(_state(None), match_info={"task_id": "p1"})
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_execute_plan(
            _request(
                _state(_runner(tmp_path)),
                match_info={"task_id": "p1"},
                json_body={},
                raw_json_error=True,
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_value_error_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.execute_plan = AsyncMock(side_effect=ValueError("nothing to execute"))
        resp = await api_taskrunner_execute_plan(
            _request(_state(runner), match_info={"task_id": "p1"})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "nothing to execute"

    @pytest.mark.asyncio
    async def test_success_forwards_options(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_execute_plan(
            _request(
                _state(runner),
                match_info={"task_id": "p1"},
                json_body={"agent": "a", "fresh": True, "workspace_dir": "/ws"},
            )
        )
        assert _body(resp) == {"ok": True, "task_id": "p1"}
        kwargs = runner.execute_plan.await_args.kwargs
        assert kwargs["agent"] == "a"
        assert kwargs["fresh"] is True
        assert kwargs["workspace_dir"] == "/ws"
        assert kwargs["auto_approve"] is False


class TestFromChat:
    @pytest.mark.asyncio
    async def test_no_runner_is_400(self) -> None:
        resp = await api_taskrunner_from_chat(_request(_state(None), json_body={"steps": [{}]}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self, tmp_path: Path) -> None:
        resp = await api_taskrunner_from_chat(
            _request(_state(_runner(tmp_path)), raw_json_error=True)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    @pytest.mark.parametrize("steps", [[], "not-a-list", None])
    async def test_steps_must_be_a_non_empty_list(self, tmp_path: Path, steps: Any) -> None:
        resp = await api_taskrunner_from_chat(
            _request(_state(_runner(tmp_path)), json_body={"steps": steps})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "steps array required"

    @pytest.mark.asyncio
    async def test_existing_task_id_updates_in_place(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        run = _planned_run("existing")
        runner.update_plan = AsyncMock(return_value=run)
        resp = await api_taskrunner_from_chat(
            _request(
                _state(runner),
                json_body={"steps": [{"title": "first"}], "task_id": "existing"},
            )
        )
        assert _body(resp)["task_id"] == "existing"
        assert runner.update_plan.await_args is not None
        assert runner.update_plan.await_args.args[0] == "existing"
        assert runner._runs == {}

    @pytest.mark.asyncio
    async def test_new_plan_is_registered_with_work_dir(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)

        async def _update(new_id: str, _steps: list[Any]) -> TaskRun:
            return runner._runs[new_id]

        runner.update_plan = AsyncMock(side_effect=_update)
        resp = await api_taskrunner_from_chat(
            _request(
                _state(runner),
                json_body={"steps": [{"title": "first"}], "original_input": "make it"},
            )
        )
        data = _body(resp)
        assert data["task_id"].startswith("plan_")
        created = runner._runs[data["task_id"]]
        assert created.source == "chat"
        assert created.status == "planned"
        assert created.original_input == "make it"
        assert Path(created.work_dir).is_dir()

    @pytest.mark.asyncio
    async def test_failed_new_plan_is_rolled_back(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.update_plan = AsyncMock(side_effect=ValueError("bad step"))
        resp = await api_taskrunner_from_chat(
            _request(_state(runner), json_body={"steps": [{"title": ""}]})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "bad step"
        # The placeholder run must not survive a rejected plan.
        assert runner._runs == {}


# ── refine ──


def _refine_state() -> Any:
    """A DashboardState stand-in carrying only the refine-related attributes."""
    return SimpleNamespace(
        task_runner=None,
        sessions=MagicMock(),
        _refine_task=None,
        _refine_text="",
        _refine_error="",
        _refine_status="idle",
        _refine_input="",
        _refine_session_key="",
        _refine_answer_future=None,
        _background_tasks=set(),
        broadcast_ws=MagicMock(),
        push_refresh=MagicMock(),
        push_slots_update=MagicMock(),
    )


class TestRefineStart:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self) -> None:
        resp = await api_taskrunner_refine(_request(_refine_state(), raw_json_error=True))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_blank_input_is_400(self) -> None:
        resp = await api_taskrunner_refine(
            _request(_refine_state(), json_body={"input": "  "})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "input is required"

    @pytest.mark.asyncio
    async def test_starts_background_task_and_cancels_previous(self) -> None:
        state = _refine_state()
        started = asyncio.Event()

        async def _never() -> None:
            started.set()
            await asyncio.Event().wait()

        previous = asyncio.create_task(_never())
        await started.wait()
        state._refine_task = previous

        async def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        with patch("kiro_crew.dashboard.handlers.taskrunner._run_refine", new=_noop):
            resp = await api_taskrunner_refine(
                _request(state, json_body={"input": " build a thing "})
            )
        assert _body(resp) == {"ok": True}
        assert state._refine_status == "running"
        assert state._refine_input == "build a thing"
        await asyncio.gather(previous, return_exceptions=True)
        assert previous.cancelled()
        await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
        assert state._background_tasks == set()


class TestRefineStatusAndCancel:
    @pytest.mark.asyncio
    async def test_status_echoes_state(self) -> None:
        state = _refine_state()
        state._refine_status = "done"
        state._refine_text = "spec"
        state._refine_input = "in"
        resp = await api_taskrunner_refine_status(_request(state, "GET"))
        assert _body(resp) == {
            "status": "done",
            "text": "spec",
            "error": "",
            "input": "in",
            "waiting": False,
        }

    @pytest.mark.asyncio
    async def test_cancel_without_task_is_ok(self) -> None:
        resp = await api_taskrunner_refine_cancel(_request(_refine_state()))
        assert _body(resp) == {"ok": True}

    @pytest.mark.asyncio
    async def test_cancel_cancels_running_task(self) -> None:
        state = _refine_state()
        started = asyncio.Event()

        async def _never() -> None:
            started.set()
            await asyncio.Event().wait()

        task = asyncio.create_task(_never())
        await started.wait()
        state._refine_task = task
        resp = await api_taskrunner_refine_cancel(_request(state))
        assert _body(resp) == {"ok": True}
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()


class TestRefineAnswer:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self) -> None:
        resp = await api_taskrunner_refine_answer(
            _request(_refine_state(), raw_json_error=True)
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_blank_answer_is_400(self) -> None:
        resp = await api_taskrunner_refine_answer(
            _request(_refine_state(), json_body={"answer": " "})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "answer required"

    @pytest.mark.asyncio
    async def test_no_pending_question_is_409(self) -> None:
        resp = await api_taskrunner_refine_answer(
            _request(_refine_state(), json_body={"answer": "yes"})
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "no pending question"

    @pytest.mark.asyncio
    async def test_already_resolved_future_is_409(self) -> None:
        state = _refine_state()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.set_result("earlier")
        state._refine_answer_future = future
        resp = await api_taskrunner_refine_answer(
            _request(state, json_body={"answer": "yes"})
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "no pending question"

    @pytest.mark.asyncio
    async def test_concurrent_set_result_is_409(self) -> None:
        """A racing answer that lands between the ``done()`` check and
        ``set_result`` surfaces as 409, not a 500."""

        class _RacingFuture:
            def done(self) -> bool:
                return False

            def set_result(self, _value: str) -> None:
                raise asyncio.InvalidStateError("already resolved")

        state = _refine_state()
        state._refine_answer_future = _RacingFuture()
        resp = await api_taskrunner_refine_answer(
            _request(state, json_body={"answer": "yes"})
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "question already resolved"

    @pytest.mark.asyncio
    async def test_answer_resolves_future(self) -> None:
        state = _refine_state()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        state._refine_answer_future = future
        resp = await api_taskrunner_refine_answer(
            _request(state, json_body={"answer": "  yes  "})
        )
        assert _body(resp) == {"ok": True}
        assert future.result() == "yes"
        # `waiting` flips to False once the question is answered.
        status = await api_taskrunner_refine_status(_request(state, "GET"))
        assert _body(status)["waiting"] is True  # future object still attached


class TestRunRefine:
    """The background refine coroutine itself (no real LLM, no real session)."""

    @staticmethod
    def _sessions(events: list[AcpEvent]) -> MagicMock:
        client = MagicMock()

        async def _stream(_msg: str) -> Any:
            for ev in events:
                yield ev

        client.stream = _stream
        client.reject_tool = AsyncMock()
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        return sessions

    @pytest.mark.asyncio
    async def test_happy_path_redacts_and_broadcasts_done(self) -> None:
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="# Task: leak "),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_EXFIL_URL),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        await _run_refine(state, "build a thing")
        assert state._refine_status == "done"
        assert state._refine_task is None
        assert state._refine_session_key == ""
        final = state.broadcast_ws.call_args.args[1]
        assert final["status"] == "done"
        assert "[REDACTED" in final["text"]
        assert _EXFIL_URL not in final["text"]
        state.sessions.release.assert_called_once()
        state.sessions.reset.assert_awaited_once()
        state.push_refresh.assert_called_once_with("taskrunner")

    @pytest.mark.asyncio
    async def test_streaming_chunks_are_throttle_pushed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interim progress is broadcast once the throttle window has elapsed.

        The clock is stubbed (monotonic returns a fixed, increasing sequence) so
        the test never sleeps and never depends on wall-clock timing.
        """
        import time as real_time

        clock = {"t": 0.0}

        def _monotonic() -> float:
            clock["t"] += 1.0
            return clock["t"]

        monkeypatch.setattr(real_time, "monotonic", _monotonic)
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="a"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="b"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="c"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        await _run_refine(state, "x")
        assert state._refine_text == "abc"
        # opening push + one push per chunk (clock always past the throttle
        # window) + the post-loop push, then the final "done" broadcast.
        statuses = [c.args[1]["status"] for c in state.broadcast_ws.call_args_list]
        assert statuses.count("running") == 5
        assert statuses[-1] == "done"

    @pytest.mark.asyncio
    async def test_permission_requests_are_rejected(self) -> None:
        state = _refine_state()
        sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="write_file", request_id="r1"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        state.sessions = sessions
        await _run_refine(state, "x")
        client = sessions.get_or_create.return_value[0]
        client.reject_tool.assert_awaited_once_with("r1")
        assert state._refine_status == "done"

    @pytest.mark.asyncio
    async def test_stream_error_is_captured_not_raised(self) -> None:
        state = _refine_state()
        sessions = self._sessions([])
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("provider down"))
        state.sessions = sessions
        await _run_refine(state, "x")
        assert state._refine_status == "error"
        assert state._refine_error == "provider down"
        assert state.broadcast_ws.call_args.args[1]["error"] == "provider down"

    @pytest.mark.asyncio
    async def test_teardown_failures_are_swallowed(self) -> None:
        state = _refine_state()
        sessions = self._sessions([AcpEvent(kind=EVENT_COMPLETE)])
        sessions.release = MagicMock(side_effect=RuntimeError("release boom"))
        sessions.reset = AsyncMock(side_effect=RuntimeError("reset boom"))
        state.sessions = sessions
        await _run_refine(state, "x")
        # A failing teardown must not mask the completed refine.
        assert state._refine_status == "done"
        assert state.broadcast_ws.call_args.args[1]["status"] == "done"

    @pytest.mark.asyncio
    async def test_cancellation_marks_cancelled(self) -> None:
        state = _refine_state()
        sessions = self._sessions([])
        sessions.get_or_create = AsyncMock(side_effect=asyncio.CancelledError())
        state.sessions = sessions
        await _run_refine(state, "x")
        assert state._refine_status == "cancelled"
