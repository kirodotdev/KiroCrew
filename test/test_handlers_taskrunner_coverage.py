"""Coverage tests for the task-runner dashboard handlers.

Focus: request validation, run-lifecycle state transitions, error responses and
status codes for every endpoint in
``kiro_crew.dashboard.handlers.taskrunner`` — plus the redaction applied to
LLM-authored text on the status / plan / export surfaces.

Everything is driven through ``make_mocked_request`` against a fake
``DashboardState`` (matching the existing style in ``test_auto_approve.py`` and
``test_project_alias.py``): no network, no git, no subprocess, no real agent.

The refine handlers live in ``test_handlers_taskrunner_coverage_refine.py``; what every
handler takes from a request (an object body, the caller's execution context, the owner of
the shared plan, the exact task named) in
``test_handlers_taskrunner_coverage_request_contract.py``; the spec I/O kept off the event loop
in ``test_handlers_taskrunner_coverage_off_loop.py``; the shared helpers in
``handlers_taskrunner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _EXFIL_URL,
    _body,
    _chat_state,
    _planned_run,
    _request,
    _runner,
    _state,
    _stub_sel,
)

from kiro_crew.dashboard.handlers.taskrunner import (
    _validate_spec_path,
    api_taskrunner_cancel,
    api_taskrunner_delete,
    api_taskrunner_execute_plan,
    api_taskrunner_export_yaml,
    api_taskrunner_from_chat,
    api_taskrunner_pause,
    api_taskrunner_plan,
    api_taskrunner_plan_cancel,
    api_taskrunner_plan_context,
    api_taskrunner_rename,
    api_taskrunner_retry,
    api_taskrunner_start,
    api_taskrunner_status,
    api_taskrunner_to_chat,
    api_taskrunner_update_plan,
    api_taskrunner_update_task,
)
from kiro_crew.hooks import FileTooLargeError
from kiro_crew.taskrunner import Step, StepStatus, TaskRun
from kiro_crew.testing.wait import await_until

# ── GET /api/taskrunner ──


class TestStatus:
    @pytest.mark.asyncio
    async def test_running_describes_only_visible_runs(self, tmp_path: Path) -> None:
        """A hidden cron run must not make an idle visible result look active."""
        runner = _runner(tmp_path)
        runner.status.return_value = {
            "running": True,
            "runs": [
                {"source": "dashboard", "running": False},
                {"source": "cron", "running": True},
            ],
        }

        resp = await api_taskrunner_status(_request(_state(runner), "GET"))

        data = _body(resp)
        assert data["runs"] == [{"source": "dashboard", "running": False}]
        assert data["running"] is False

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
    async def test_disabled_task_runner_app_refuses_start(self, tmp_path: Path) -> None:
        # Disabled in Library means no new runs: the run would have no page.
        runner = _runner(tmp_path)
        with patch(
            "kiro_crew.apps.manager._read_installed",
            return_value=SimpleNamespace(enabled=False),
        ) as read:
            resp = await api_taskrunner_start(
                _request(_state(runner), json_body={"spec": "__inline__:# t"})
            )
        assert resp.status == 409
        assert _body(resp)["code"] == "app_disabled"
        assert "Task Runner is disabled" in _body(resp)["error"]
        read.assert_called_once_with("projects")
        runner.start_background.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("meta", [None, SimpleNamespace(enabled=True)])
    async def test_enabled_or_unregistered_app_still_starts(
        self, tmp_path: Path, meta: Any
    ) -> None:
        runner = _runner(tmp_path)
        with patch("kiro_crew.apps.manager._read_installed", return_value=meta):
            resp = await api_taskrunner_start(
                _request(_state(runner), json_body={"spec": "__inline__:# t"})
            )
        assert resp.status == 200
        runner.start_background.assert_awaited_once()

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
        with patch("kiro_crew.dashboard.handlers.taskrunner.is_sensitive_path", return_value=True):
            resp = await api_taskrunner_start(
                _request(_state(runner), json_body={"spec": str(spec)})
            )
        assert resp.status == 403
        assert _body(resp)["error"] == "access denied"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unc_spec",
        [r"\\server\share\x.md", "//server/share/x.md", r"\\?\UNC\server\share\x.md"],
    )
    async def test_unc_path_rejected_by_screen(self, tmp_path: Path, unc_spec: str) -> None:
        # A UNC / link-laundered spec is rejected by validate_file_path's
        # pre-resolve screen, so the handler returns 400 and never stats a
        # followed target that could open an SMB connection.
        with (
            patch(
                "kiro_crew.dashboard.handlers.taskrunner.validate_file_path", return_value=None
            ) as screen,
            patch("pathlib.Path.is_file") as is_file,
        ):
            resp = await api_taskrunner_start(
                _request(_state(_runner(tmp_path)), json_body={"spec": unc_spec, "source": "file"})
            )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid spec path"
        screen.assert_called_once_with(unc_spec)
        is_file.assert_not_called()

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
        resp = await api_taskrunner_start(
            _request(_state(runner), json_body={"spec": "__inline__:# t", "source": "bogus"})
        )
        assert resp.status == 200
        assert _body(resp)["task_id"] == "tid-1"
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
        resp = await api_taskrunner_cancel(_request(_state(runner), json_body={"task_id": "t9"}))
        assert resp.status == 200
        assert _body(resp) == {"ok": True}
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
            _request(_state(_runner(tmp_path)), match_info={"task_id": "t1", "index": "abc"})
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
        resp = await api_taskrunner_retry(_request(_state(runner), match_info={"task_id": "t1"}))
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
        "exc",
        [
            FileNotFoundError("missing spec"),
            ValueError("empty input"),
            # A descriptor-gate refusal and an oversize spec are the caller's to
            # see: the message names what was withheld instead of a bare 500.
            PermissionError("Spec file refused by the file gate: /w/spec.md"),
            FileTooLargeError("File exceeds 50 MB safety cap"),
        ],
    )
    async def test_expected_errors_are_400(self, tmp_path: Path, exc: Exception) -> None:
        runner = _runner(tmp_path)
        runner.plan = AsyncMock(side_effect=exc)
        resp = await api_taskrunner_plan(_request(_state(runner), json_body={"input": "x"}))
        assert resp.status == 400
        assert _body(resp)["error"] == str(exc)
        assert _body(resp)["error"] == str(exc)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "unc_spec",
        [r"\\server\share\x.md", "//server/share/x.md", r"\\?\UNC\server\share\x.md"],
    )
    async def test_unc_file_spec_rejected(self, tmp_path: Path, unc_spec: str) -> None:
        # The file-source branch shares the same validate_file_path screen.
        with (
            patch(
                "kiro_crew.dashboard.handlers.taskrunner.validate_file_path", return_value=None
            ) as screen,
            patch("pathlib.Path.is_file") as is_file,
        ):
            resp = await api_taskrunner_plan(
                _request(_state(_runner(tmp_path)), json_body={"source": "file", "spec": unc_spec})
            )
        assert resp.status == 400
        assert _body(resp)["error"] == "invalid spec path"
        screen.assert_called_once_with(unc_spec)
        is_file.assert_not_called()


class TestValidateSpecPath:
    """Unit tests for the shared _validate_spec_path screen."""

    @pytest.mark.parametrize(
        "unc_spec",
        [r"\\server\share\x.md", "//server/share/x.md", r"\\?\UNC\server\share\x.md"],
    )
    def test_screen_rejection_maps_to_invalid_without_stat(self, unc_spec: str) -> None:
        # validate_file_path screens a UNC / link-laundered path before any
        # resolve/stat; when it rejects, _validate_spec_path never stats a
        # followed target and returns the invalid_spec_path code.
        with (
            patch(
                "kiro_crew.dashboard.handlers.taskrunner.validate_file_path", return_value=None
            ) as screen,
            patch("pathlib.Path.is_file") as is_file,
        ):
            resolved, code = _validate_spec_path(unc_spec)
        assert (resolved, code) == (None, "invalid_spec_path")
        screen.assert_called_once_with(unc_spec)
        is_file.assert_not_called()

    def test_normal_relative_path_passes_the_screen(self, tmp_path: Path) -> None:
        spec = tmp_path / "sub" / "plan.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text("# t", encoding="utf-8")
        resolved, code = _validate_spec_path(str(spec))
        assert code == ""
        assert resolved == str(spec.resolve())

    def test_sensitive_canonical_path_maps_to_access_denied(self, tmp_path: Path) -> None:
        spec = tmp_path / "creds.md"
        spec.write_text("# t", encoding="utf-8")
        with (
            patch(
                "kiro_crew.dashboard.handlers.taskrunner.validate_file_path",
                return_value=str(spec),
            ),
            patch("kiro_crew.dashboard.handlers.taskrunner.is_sensitive_path", return_value=True),
        ):
            resolved, code = _validate_spec_path(str(spec))
        assert (resolved, code) == (None, "access_denied")

    def test_screened_out_sensitive_path_still_maps_to_access_denied(self) -> None:
        # validate_file_path fences a sensitive path itself and returns None; the
        # link-free lexical check must still map it to access_denied, not the
        # generic invalid_spec_path, so the 403 contract survives.
        with (
            patch("kiro_crew.dashboard.handlers.taskrunner.validate_file_path", return_value=None),
            patch(
                "kiro_crew.dashboard.handlers.taskrunner.is_sensitive_resolved_path",
                return_value=True,
            ) as sens,
            patch("pathlib.Path.is_file") as is_file,
        ):
            resolved, code = _validate_spec_path("/home/x/.aws/credentials")
        assert (resolved, code) == (None, "access_denied")
        sens.assert_called_once()
        is_file.assert_not_called()


class TestPlanCancel:
    @pytest.mark.asyncio
    async def test_no_runner_is_still_ok(self) -> None:
        resp = await api_taskrunner_plan_cancel(_request(_state(None)))
        assert _body(resp) == {"ok": True}

    @pytest.mark.asyncio
    async def test_delegates_to_runner(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        resp = await api_taskrunner_plan_cancel(_request(_state(runner)))
        assert resp.status == 200
        assert _body(resp) == {"ok": True}
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
                _state(_runner(tmp_path)),
                "PUT",
                match_info={"task_id": "p1"},
                raw_json_error=True,
            )
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_value_error_is_400(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        runner.update_plan = AsyncMock(side_effect=ValueError("run is not planned"))
        resp = await api_taskrunner_update_plan(
            _request(_state(runner), "PUT", match_info={"task_id": "p1"}, json_body={"steps": []})
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
        assert created.name == "make it"
        assert Path(created.work_dir).is_dir()
        runner._workflow_begin.assert_awaited_once_with(created)

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
        runner.delete_run.assert_awaited_once()
        assert list(runner._work_dir.glob("plan_*")) == []

    @pytest.mark.asyncio
    async def test_cancelled_new_plan_is_rolled_back(self, tmp_path: Path) -> None:
        runner = _runner(tmp_path)
        update_started = asyncio.Event()

        async def block_update(_task_id: str, _steps: list[Any]) -> TaskRun:
            update_started.set()
            await asyncio.Future()

        runner.update_plan = AsyncMock(side_effect=block_update)
        request_task = asyncio.create_task(
            api_taskrunner_from_chat(
                _request(_state(runner), json_body={"steps": [{"title": "first"}]})
            )
        )
        await await_until(update_started, what="update_plan being entered")
        created = next(iter(runner._runs.values()))
        created.workflow_run_id = "workflow-orphan-candidate"
        request_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await request_task

        runner.delete_run.assert_awaited_once_with(created.task_id)
        assert runner._runs == {}
        assert list(runner._work_dir.glob("plan_*")) == []
