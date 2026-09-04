"""Tests for the per-run auto-approve (trust) toggle.

Covers:
- Project.auto_approve default (False)
- execute_plan()/run() set run.auto_approve
- _persist_runs()/_load_runs() round-trip the flag
- execute_task: run.auto_approve=True → tool auto-approved WITHOUT on_tool_approval
- execute_task: run.auto_approve=True + hook TOOL_DENY → tool STILL rejected
- force_approval task gate still triggers on_approval regardless of run.auto_approve
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from body_stream_helpers import BodyStreamPayload

from kiro_crew.dashboard.handlers.taskrunner import (
    api_taskrunner_execute_plan,
    api_taskrunner_start,
)
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.providers.base import LLMEvent
from kiro_crew.safety_override import reset_singleton, safety_override
from kiro_crew.task_models import Project
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner, _auto_approve_scope


@pytest.fixture(autouse=True)
def _reset_safety_override():
    """Isolate the SafetyOverride singleton (per-run grants) between tests."""
    reset_singleton()
    yield
    reset_singleton()


# ── Helpers ──


def _mock_sessions() -> MagicMock:
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock()

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()
    return s


def _perm_then_done_provider():
    """Provider that emits one permission_request, then text + complete."""
    provider = MagicMock()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request",
            title="write_file",
            text="",
            request_id="req-1",
            tool_kind="tool",
        )
        yield LLMEvent(kind="text_chunk", text="done")
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


def _plain_provider(text: str = "done"):
    provider = MagicMock()

    async def _stream(msg: str):
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


# ══════════════════════════════════════════════════════════════════════
# (i) default
# ══════════════════════════════════════════════════════════════════════


class TestAutoApproveDefault:
    def test_project_auto_approve_defaults_false(self) -> None:
        run = Project(spec_path="s.md", spec_content="s")
        assert run.auto_approve is False


# ══════════════════════════════════════════════════════════════════════
# (ii) execute_plan / run set it
# ══════════════════════════════════════════════════════════════════════


def _discard_scheduled_coroutine(coro):
    """Model create_task ownership without running execute_plan's worker."""
    coro.close()
    return MagicMock()


class TestSettersSetAutoApprove:
    @pytest.mark.asyncio
    async def test_execute_plan_sets_auto_approve(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="planned", task_id="t1")
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        # Prevent the background _execute task from actually running.
        with patch(
            "kiro_crew.taskrunner.asyncio.create_task",
            side_effect=_discard_scheduled_coroutine,
        ):
            await runner.execute_plan("t1", auto_approve=True)
        assert run.auto_approve is True
        assert safety_override().is_scope_active(_auto_approve_scope("t1")) is True

    @pytest.mark.asyncio
    async def test_execute_plan_defaults_false(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="planned", task_id="t1")
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        with patch(
            "kiro_crew.taskrunner.asyncio.create_task",
            side_effect=_discard_scheduled_coroutine,
        ):
            await runner.execute_plan("t1")
        assert run.auto_approve is False
        assert safety_override().is_scope_active(_auto_approve_scope("t1")) is False

    @pytest.mark.asyncio
    async def test_run_sets_auto_approve(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: Trust run\n## Steps\n1. Do thing", encoding="utf-8")
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        runner._decompose = AsyncMock(return_value=[Step(index=1, title="s", description="d")])
        with patch.object(runner, "_execute_tasks", new_callable=AsyncMock, return_value=True):
            run = await runner.run(spec, auto_approve=True)
        assert run.auto_approve is True
        # NOTE: run() completes its full lifecycle here, whose teardown
        # (_release_run_runtime) deactivates the scoped grant by design — so we
        # assert the persisted intent flag; live-grant activation is covered by
        # test_execute_plan_sets_auto_approve and the execution tests.

    @pytest.mark.asyncio
    async def test_start_background_sets_auto_approve(self, tmp_path: Path) -> None:
        spec = tmp_path / "spec.md"
        spec.write_text("# Bg\n## Steps\n1. Do thing", encoding="utf-8")
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        with patch.object(runner, "run", new_callable=AsyncMock) as mock_run:
            task_id = await runner.start_background(spec, auto_approve=True)
            await asyncio.sleep(0)  # let the wrapper task run
        # Placeholder Project carries the flag immediately …
        assert runner._runs[task_id].auto_approve is True
        # … and it is threaded through into run().
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("auto_approve") is True


# ══════════════════════════════════════════════════════════════════════
# (iii) persist / load round-trip
# ══════════════════════════════════════════════════════════════════════


class TestAutoApprovePersistence:
    def test_persist_and_load_round_trip(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(
            spec_path=str(tmp_path / "s.md"),
            spec_content="s",
            status="paused",
            task_id="t1",
        )
        run.work_dir = str(tmp_path)
        run.auto_approve = True
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        runner._persist_runs()

        # Fresh runner loads from the same runs.json on __init__.
        runner2 = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        loaded = runner2._runs["t1"]
        assert loaded.auto_approve is True

    def test_trust_reset_on_crash_recovery(self, tmp_path: Path) -> None:
        """A run recovered from an active state must not silently retain trust."""
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(
            spec_path=str(tmp_path / "s.md"),
            spec_content="s",
            status="running",  # gateway "crashed" mid-execution
            task_id="t1",
        )
        run.work_dir = str(tmp_path)
        run.auto_approve = True
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        runner._persist_runs()

        # Fresh runner recovers the crashed run on __init__.
        runner2 = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        recovered = runner2._runs["t1"]
        assert recovered.status == "paused"  # recovered as resumable
        assert recovered.auto_approve is False  # trust dropped — must re-affirm on resume

    def test_load_defaults_false_when_absent(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(
            spec_path=str(tmp_path / "s.md"),
            spec_content="s",
            status="paused",
            task_id="t1",
        )
        run.work_dir = str(tmp_path)
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        runner._persist_runs()
        runner2 = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        assert runner2._runs["t1"].auto_approve is False


# ══════════════════════════════════════════════════════════════════════
# (iv) auto-approve without interactive handler
# ══════════════════════════════════════════════════════════════════════


class TestAutoApproveExecution:
    @pytest.mark.asyncio
    async def test_auto_approve_skips_interactive_handler(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _perm_then_done_provider()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        on_tool_approval = AsyncMock(return_value=True)
        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        runner._on_tool_approval = on_tool_approval

        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running", task_id="t1")
        run.auto_approve = True
        safety_override().activate_scoped(_auto_approve_scope("t1"), source="dashboard")  # live grant
        step = Step(index=1, title="Write", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            success = await runner._execute_single_task(run, step)

        assert success is True
        assert step.status == StepStatus.PASSED
        # Auto-approved WITHOUT prompting the interactive handler.
        provider.approve_tool.assert_awaited_once_with("req-1")
        on_tool_approval.assert_not_called()

    @pytest.mark.asyncio
    async def test_expired_trust_is_revoked_and_not_auto_approved(self, tmp_path: Path) -> None:
        """No live scoped grant → trust is not honored: no auto-approve, intent cleared."""
        sessions = _mock_sessions()
        provider = _perm_then_done_provider()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        # No interactive handler → after trust lapses, deny-by-default applies.
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running", task_id="t1")
        run.auto_approve = True  # intent set, but NO active SafetyOverride grant (expired/absent)
        step = Step(index=1, title="Write", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            await runner._execute_single_task(run, step)

        # Trust lapsed → not auto-approved (rejected via deny-by-default) and revoked.
        provider.approve_tool.assert_not_called()
        provider.reject_tool.assert_awaited_with("req-1")
        assert run.auto_approve is False

    @pytest.mark.asyncio
    async def test_without_auto_approve_headless_rejects(self, tmp_path: Path) -> None:
        """Quick check: with auto_approve off and no handler, the tool is denied."""
        sessions = _mock_sessions()
        provider = _perm_then_done_provider()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
        # No on_tool_approval handler configured, auto_approve off.
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        step = Step(index=1, title="Write", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            await runner._execute_single_task(run, step)

        provider.reject_tool.assert_awaited_with("req-1")
        provider.approve_tool.assert_not_called()


# ══════════════════════════════════════════════════════════════════════
# (v) hook deny-list still blocks a trusted run
# ══════════════════════════════════════════════════════════════════════


class TestAutoApproveRespectsHookDeny:
    @pytest.mark.asyncio
    async def test_hook_deny_still_rejects_when_auto_approve(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _perm_then_done_provider()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        # ctx.hooks.on_tool_call returns TOOL_DENY (deny-list / sensitive-path block).
        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("prompt", {}))
        ctx.hooks.on_tool_call = MagicMock(return_value=MagicMock(action=TOOL_DENY))

        runner = TaskRunner(
            sessions=sessions, context_builder=ctx, auto_test=False, work_dir=tmp_path
        )
        runner._on_tool_approval = AsyncMock(return_value=True)

        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.auto_approve = True
        step = Step(index=1, title="Delete", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.self_review", return_value=True):
            await runner._execute_single_task(run, step)

        # Deny-list is evaluated BEFORE auto-approve, so the tool is rejected
        # even though the run is trusted.
        provider.reject_tool.assert_awaited_with("req-1")
        provider.approve_tool.assert_not_called()
        runner._on_tool_approval.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_auto_approve_reason_preserved(self, tmp_path: Path) -> None:
        """An explicit hook TOOL_AUTO_APPROVE keeps its own reason, not run's."""
        sessions = _mock_sessions()
        provider = _perm_then_done_provider()
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        ctx = MagicMock()
        ctx.build_message = MagicMock(return_value=("prompt", {}))
        ctx.hooks.on_tool_call = MagicMock(return_value=MagicMock(action=TOOL_AUTO_APPROVE))

        runner = TaskRunner(
            sessions=sessions, context_builder=ctx, auto_test=False, work_dir=tmp_path
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.auto_approve = False  # only the hook trusts it
        step = Step(index=1, title="Read", description="d")
        run.tasks = [step]

        with patch("kiro_crew.task_executor.self_review", return_value=True), patch(
            "kiro_crew.task_executor.sel"
        ) as mock_sel:
            await runner._execute_single_task(run, step)

        provider.approve_tool.assert_awaited_once_with("req-1")
        # The approval audit records reason=hook_auto_approve.
        reasons = [
            c.kwargs.get("metadata", {}).get("reason")
            for c in mock_sel().log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "approved"
        ]
        assert "hook_auto_approve" in reasons


# ══════════════════════════════════════════════════════════════════════
# (vi) force_approval task gate unaffected by auto_approve
# ══════════════════════════════════════════════════════════════════════


class TestForceApprovalGateUnaffected:
    @pytest.mark.asyncio
    async def test_force_approval_still_prompts_when_auto_approve(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        provider = _plain_provider("done")
        sessions.get_or_create = AsyncMock(return_value=(provider, True, False))

        on_approval = AsyncMock(return_value=True)
        runner = TaskRunner(
            sessions=sessions, auto_test=False, work_dir=tmp_path, on_approval=on_approval
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.auto_approve = True  # trusted run — must NOT bypass the task gate
        step = Step(
            index=1, title="Deploy", description="d", requires_approval=True, force_approval=True
        )
        run.tasks = [step]

        with patch.object(runner, "self_review", return_value=True):
            success = await runner._execute_single_task(run, step, "hk")

        # The task-level force_approval gate still fired despite auto_approve.
        on_approval.assert_awaited_once()
        assert success is True
        assert step.status == StepStatus.PASSED

    @pytest.mark.asyncio
    async def test_force_approval_denied_pauses_even_when_auto_approve(self, tmp_path: Path) -> None:
        sessions = _mock_sessions()
        runner = TaskRunner(
            sessions=sessions,
            auto_test=False,
            work_dir=tmp_path,
            on_approval=AsyncMock(return_value=False),
        )
        run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
        run.auto_approve = True
        step = Step(
            index=1, title="Deploy", description="d", requires_approval=True, force_approval=True
        )
        run.tasks = [step]

        result = await runner._execute_single_task(run, step, "hk")
        assert result is False
        assert step.status == StepStatus.PENDING
        assert run.status == "paused"


# ══════════════════════════════════════════════════════════════════════
# Design-review #1: trust provenance enforced at the API boundary
# ══════════════════════════════════════════════════════════════════════


class TestAutoApproveProvenanceGating:
    """`auto_approve` is honored only for dashboard-launched runs.

    Cron/MCP/chat/file callers cannot mint a self-trusted run even if they
    send ``auto_approve: true`` — the SEL ``run_auto_approve`` signal stays a
    human-at-the-dashboard signal.
    """

    async def _auto_approve_passed(self, tmp_path: Path, source: str, auto_approve: bool, request_app: str = ""):
        runner = MagicMock()
        runner._work_dir = tmp_path
        runner.start_background = MagicMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        start_body = {
            "spec": "__inline__:# t\n## Steps\n1. do",
            "source": source,
            "auto_approve": auto_approve,
        }
        req = make_mocked_request(
            "POST", "/api/taskrunner", app=app,
            payload=BodyStreamPayload(json.dumps(start_body).encode()),
        )
        req["app"] = request_app  # set by token_auth_middleware; "" == dashboard itself
        # Kept alive: ``api_taskrunner_start`` reads uncapped (``max_bytes=None``)
        # and consumes ``request.json()``.
        req.json = AsyncMock(return_value=start_body)
        await api_taskrunner_start(req)
        return runner.start_background.call_args.kwargs["auto_approve"]

    @pytest.mark.asyncio
    async def test_app_embedded_caller_ignored(self, tmp_path: Path) -> None:
        # Even with source="dashboard", an app/proxy-embedded caller cannot self-trust.
        assert await self._auto_approve_passed(tmp_path, "dashboard", True, request_app="someapp") is False

    @pytest.mark.asyncio
    async def test_dashboard_source_allows_trust(self, tmp_path: Path) -> None:
        assert await self._auto_approve_passed(tmp_path, "dashboard", True) is True

    @pytest.mark.asyncio
    async def test_cron_source_ignores_trust(self, tmp_path: Path) -> None:
        assert await self._auto_approve_passed(tmp_path, "cron", True) is False

    @pytest.mark.asyncio
    async def test_mcp_source_ignores_trust(self, tmp_path: Path) -> None:
        assert await self._auto_approve_passed(tmp_path, "mcp", True) is False

    async def _execute_auto_approve_passed(self, request_app: str, auto_approve: bool = True):
        runner = MagicMock()
        runner.execute_plan = AsyncMock(return_value=None)
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        exec_body = {"auto_approve": auto_approve}
        raw = json.dumps(exec_body).encode()
        req = make_mocked_request(
            "POST", "/api/taskrunner/t1/execute", app=app, match_info={"task_id": "t1"},
            headers={"Content-Length": str(len(raw))}, payload=BodyStreamPayload(raw),
        )
        req["app"] = request_app
        await api_taskrunner_execute_plan(req)
        return runner.execute_plan.call_args.kwargs["auto_approve"]

    @pytest.mark.asyncio
    async def test_execute_dashboard_context_allows_trust(self) -> None:
        assert await self._execute_auto_approve_passed(request_app="") is True

    @pytest.mark.asyncio
    async def test_execute_app_embedded_ignored(self) -> None:
        # The /execute endpoint must enforce the SAME gate as /start.
        assert await self._execute_auto_approve_passed(request_app="someapp") is False

    @pytest.mark.asyncio
    async def test_execute_gate_audit_failure_fails_closed(self) -> None:
        """A SEL/audit failure inside the provenance gate is CONTAINED in the
        gate itself (CWE-755): it neither leaks an unsanitized traceback as a 500
        nor grants trust. The gate fails closed to ``auto_approve=False`` and the
        plan proceeds without auto-approval.

        Regression guard: the containment invariant lives in ``_gate_auto_approve``
        (so every current and future caller is protected), NOT in a per-endpoint
        try/except that a later adopter could forget to add.
        """
        runner = MagicMock()
        runner.execute_plan = AsyncMock(return_value=None)
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        raw = json.dumps({"auto_approve": True}).encode()
        req = make_mocked_request(
            "POST", "/api/taskrunner/t1/execute", app=app, match_info={"task_id": "t1"},
            headers={"Content-Length": str(len(raw))}, payload=BodyStreamPayload(raw),
        )
        req["app"] = ""  # dashboard context → requested trust is honored, so the gate audits

        boom = MagicMock()
        boom.log_tool_invocation.side_effect = RuntimeError("sel backend down: SECRET-INTERNAL-DETAIL")
        with patch("kiro_crew.dashboard.handlers.taskrunner._sel", return_value=boom):
            resp = await api_taskrunner_execute_plan(req)

        # No unsanitized 500 escapes; the request succeeds.
        assert resp.status == 200
        # The raw exception text must not leak to the client.
        assert "SECRET-INTERNAL-DETAIL" not in resp.body.decode()
        # Fail closed: trust was NOT granted despite the request asking for it,
        # and the plan still ran (without auto-approval).
        runner.execute_plan.assert_called_once()
        assert runner.execute_plan.call_args.kwargs["auto_approve"] is False
        # The audit MUST be requested SYNCHRONOUSLY (critical=True) so a real
        # (async) SEL write failure reaches this fail-closed handler rather than
        # being swallowed by the background writer after the gate has returned.
        assert boom.log_tool_invocation.call_args.kwargs["critical"] is True


# ══════════════════════════════════════════════════════════════════════
# Inline spec ownership: a rejected start must not leak its temp file
# ══════════════════════════════════════════════════════════════════════


class TestInlineSpecCleanup:
    """POST /api/taskrunner writes inline specs to work/TASK_<id>.md before
    calling start_background; when the start path raises, the handler owns
    that file and must remove it — while a caller-supplied external path is
    never the handler's to delete."""

    async def _start(self, tmp_path: Path, body: dict, raising: bool):
        runner = MagicMock()
        runner._work_dir = str(tmp_path)
        if raising:
            runner.start_background = AsyncMock(side_effect=RuntimeError("boom: rejected"))
        else:
            runner.start_background = AsyncMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request(
            "POST", "/api/taskrunner", app=app,
            payload=BodyStreamPayload(json.dumps(body).encode()),
        )
        req["app"] = ""
        # Kept alive: ``api_taskrunner_start`` reads uncapped (``max_bytes=None``)
        # and consumes ``request.json()``.
        req.json = AsyncMock(return_value=body)
        return await api_taskrunner_start(req), runner

    @pytest.mark.asyncio
    async def test_a_rejected_inline_start_leaves_no_task_file_behind(self, tmp_path: Path) -> None:
        resp, _ = await self._start(
            tmp_path,
            {"spec": "__inline__:# rejected plan", "source": "dashboard"},
            raising=True,
        )
        assert resp.status == 400
        assert list(tmp_path.glob("TASK_*.md")) == []

    @pytest.mark.asyncio
    async def test_the_original_error_survives_even_if_cleanup_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A best-effort unlink that itself fails must not replace the startup
        # error the client is about to receive.
        calls = {"n": 0}

        def _flaky(self: Path, missing_ok: bool = False) -> None:
            calls["n"] += 1
            raise OSError("disk gone")

        monkeypatch.setattr(Path, "unlink", _flaky, raising=False)
        resp, _ = await self._start(
            tmp_path,
            {"spec": "__inline__:# rejected plan", "source": "dashboard"},
            raising=True,
        )
        assert calls["n"] >= 1  # the cleanup was attempted
        assert resp.status == 400
        assert json.loads(resp.text)["error"].startswith("boom: rejected")

    @pytest.mark.asyncio
    async def test_an_external_spec_is_never_deleted_on_a_rejected_start(
        self, tmp_path: Path
    ) -> None:
        external = tmp_path / "caller-owned.md"
        external.write_text("# caller's own spec")
        resp, _ = await self._start(
            tmp_path,
            {"spec": str(external), "source": "dashboard"},
            raising=True,
        )
        assert resp.status == 400
        assert external.exists(), "the handler deleted a caller-owned spec file"

    @pytest.mark.asyncio
    async def test_a_successful_inline_start_keeps_its_spec_file(self, tmp_path: Path) -> None:
        resp, _ = await self._start(
            tmp_path,
            {"spec": "__inline__:# good plan", "source": "dashboard"},
            raising=False,
        )
        assert resp.status == 200
        payload = json.loads(resp.text)
        assert Path(payload["spec"]).is_file()
