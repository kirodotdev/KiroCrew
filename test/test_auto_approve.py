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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

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


class TestSettersSetAutoApprove:
    @pytest.mark.asyncio
    async def test_execute_plan_sets_auto_approve(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="planned", task_id="t1")
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        # Prevent the background _execute task from actually running.
        with patch("kiro_crew.taskrunner.asyncio.create_task", return_value=MagicMock()):
            await runner.execute_plan("t1", auto_approve=True)
        assert run.auto_approve is True
        assert safety_override().is_scope_active(_auto_approve_scope("t1")) is True

    @pytest.mark.asyncio
    async def test_execute_plan_defaults_false(self, tmp_path: Path) -> None:
        runner = TaskRunner(sessions=_mock_sessions(), auto_test=False, work_dir=tmp_path)
        run = TaskRun(spec_path="s.md", spec_content="s", status="planned", task_id="t1")
        run.tasks = [Step(index=1, title="A", description="d")]
        runner._runs = {"t1": run}
        with patch("kiro_crew.taskrunner.asyncio.create_task", return_value=MagicMock()):
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
        req = make_mocked_request("POST", "/api/taskrunner", app=app)
        req["app"] = request_app  # set by token_auth_middleware; "" == dashboard itself
        req.json = AsyncMock(
            return_value={
                "spec": "__inline__:# t\n## Steps\n1. do",
                "source": source,
                "auto_approve": auto_approve,
            }
        )
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
        # /execute now reads the planned run's in-memory spec to honor a declared
        # `approval: auto`; give it a real run with NO directive so this test
        # isolates the UI-flag path (the spec-directive path is covered below).
        runner._runs = {
            "t1": Project(
                spec_path="s.md", spec_content="# Task: t\n## Steps\n1. do\n",
                status="planned", task_id="t1",
            )
        }
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request(
            "POST", "/api/taskrunner/t1/execute", app=app, match_info={"task_id": "t1"},
            headers={"Content-Length": "32"},
        )
        req["app"] = request_app
        req.json = AsyncMock(return_value={"auto_approve": auto_approve})
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
        runner._runs = {
            "t1": Project(
                spec_path="s.md", spec_content="# Task: t\n## Steps\n1. do\n",
                status="planned", task_id="t1",
            )
        }
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request(
            "POST", "/api/taskrunner/t1/execute", app=app, match_info={"task_id": "t1"},
            headers={"Content-Length": "32"},
        )
        req["app"] = ""  # dashboard context → requested trust is honored, so the gate audits
        req.json = AsyncMock(return_value={"auto_approve": True})

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


class TestSpecDeclaredApprovalGating:
    """A spec that declares ``approval: auto`` requests trust through the SAME
    provenance gate as the UI flag — it can never self-elevate a non-dashboard
    launch. (Issue #2068: spec-declared approval mode.)
    """

    async def _start_auto_approve_passed(
        self, tmp_path: Path, spec_body: str, source: str,
        request_app: str = "", ui_flag: bool = False,
    ) -> bool:
        runner = MagicMock()
        runner._work_dir = tmp_path
        runner.start_background = AsyncMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request("POST", "/api/taskrunner", app=app)
        req["app"] = request_app  # "" == dashboard itself
        req.json = AsyncMock(
            return_value={
                "spec": f"__inline__:{spec_body}",
                "source": source,
                "auto_approve": ui_flag,
            }
        )
        await api_taskrunner_start(req)
        return runner.start_background.call_args.kwargs["auto_approve"]

    _SPEC_AUTO = "---\napproval: auto\n---\n# Task: t\n## Steps\n1. do\n"
    _SPEC_PLAIN = "# Task: t\n## Steps\n1. do\n"

    @pytest.mark.asyncio
    async def test_dashboard_spec_auto_grants_without_ui_flag(self, tmp_path: Path) -> None:
        # Dashboard launch + spec-declared auto → trust, even though the UI flag
        # was not set. The spec directive is a first-class request path.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_AUTO, "dashboard", request_app="", ui_flag=False
        ) is True

    @pytest.mark.asyncio
    async def test_cron_spec_auto_still_denied(self, tmp_path: Path) -> None:
        # SAME directive, cron source → the provenance gate still denies it.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_AUTO, "cron", request_app="", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_mcp_spec_auto_still_denied(self, tmp_path: Path) -> None:
        # task_run (MCP) source → denied despite the spec asking for auto.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_AUTO, "mcp", request_app="", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_app_embedded_spec_auto_denied(self, tmp_path: Path) -> None:
        # Even a dashboard-source claim cannot self-trust from an app/proxy embed.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_AUTO, "dashboard", request_app="someapp", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_dashboard_plain_spec_no_trust(self, tmp_path: Path) -> None:
        # No directive + no UI flag → deny-by-default, even on the dashboard.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_PLAIN, "dashboard", request_app="", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_ui_flag_still_works_without_directive(self, tmp_path: Path) -> None:
        # The pre-existing UI-flag path is unchanged by the OR with the directive.
        assert await self._start_auto_approve_passed(
            tmp_path, self._SPEC_PLAIN, "dashboard", request_app="", ui_flag=True
        ) is True

    @pytest.mark.asyncio
    async def test_non_utf8_spec_file_does_not_crash_start(self, tmp_path: Path) -> None:
        # A non-UTF-8 spec FILE raises UnicodeDecodeError (a ValueError, NOT an
        # OSError) on the bounded approval-prefix read. The handler must swallow
        # it and fall back to empty mode (deny-by-default), NOT 500 (GPT review,
        # PR #2129). Uses the file-path branch — inline specs are always UTF-8.
        bad = tmp_path / "spec.md"
        bad.write_bytes(b"---\napproval: auto\n---\n# Task \xff\xfe not utf-8\n")
        runner = MagicMock()
        runner._work_dir = tmp_path
        runner.start_background = AsyncMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request("POST", "/api/taskrunner", app=app)
        req["app"] = ""  # dashboard itself
        req.json = AsyncMock(
            return_value={"spec": str(bad), "source": "dashboard", "auto_approve": False}
        )
        resp = await api_taskrunner_start(req)  # must NOT raise
        assert resp.status == 200
        # Mode could not be read → deny-by-default; no unattended trust granted.
        assert runner.start_background.call_args.kwargs["auto_approve"] is False

    def test_truncated_spec_cannot_forge_directive(self, tmp_path: Path) -> None:
        # A file whose approval-scan window ends mid-line must NOT let the
        # truncated prefix read as a directive: ``approval: auto is NOT enabled``
        # cut at the window boundary would otherwise yield ``approval: auto``
        # (GPT review, PR #2129, B7). The bounded read drops the incomplete final
        # line, so no forged directive survives → deny-by-default.
        from kiro_crew.dashboard.handlers.taskrunner import (
            _SPEC_APPROVAL_READ_CHARS,
            _read_spec_head,
        )
        from kiro_crew.task_planner import parse_spec_approval_mode

        # Pad so the poisoned line straddles the read boundary, then continues.
        pad = "# filler\n" * ((_SPEC_APPROVAL_READ_CHARS // 9) + 1)
        spec = pad + "approval: auto is NOT actually enabled, just prose\n"
        bad = tmp_path / "spec.md"
        bad.write_text(spec, encoding="utf-8")
        head = _read_spec_head(str(bad))
        # The truncated tail line is dropped, so the forged directive is absent.
        assert "approval: auto is NOT" not in head
        assert parse_spec_approval_mode(head) == ""
        # A short spec that fits entirely is returned intact (no over-trimming).
        short = tmp_path / "short.md"
        short.write_text("approval: auto\n# Task\n", encoding="utf-8")
        assert parse_spec_approval_mode(_read_spec_head(str(short))) == "auto"

    @pytest.mark.asyncio
    async def test_file_backed_auto_pins_immutable_snapshot(self, tmp_path: Path) -> None:
        # TOCTOU (GPT review, PR #2129, B10): when a FILE-backed spec's directive
        # grants auto, execution must be pinned to a snapshot of the approved
        # bytes, NOT the original path — else an edit between the approval read
        # and run()'s later re-read would execute different tasks under the stale
        # grant. Assert start_background is handed a path OTHER than the original
        # and that its content equals what was approved.
        spec = tmp_path / "spec.md"
        approved = "---\napproval: auto\n---\n# Task: approved\n## Steps\n1. safe\n"
        spec.write_text(approved, encoding="utf-8")
        runner = MagicMock()
        runner._work_dir = tmp_path
        runner.start_background = AsyncMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request("POST", "/api/taskrunner", app=app)
        req["app"] = ""  # dashboard itself
        req.json = AsyncMock(
            return_value={"spec": str(spec), "source": "dashboard", "auto_approve": False}
        )
        resp = await api_taskrunner_start(req)
        assert resp.status == 200
        kw = runner.start_background.call_args
        run_path = kw.args[0] if kw.args else kw.kwargs["spec_path"]
        assert kw.kwargs["auto_approve"] is True          # directive granted auto
        assert str(run_path) != str(spec)                 # NOT the original path
        # Simulate the attacker editing the original AFTER approval; the snapshot
        # the runner will execute must still hold the approved content.
        spec.write_text("---\napproval: auto\n---\n# Task: MALICIOUS\n", encoding="utf-8")
        assert Path(run_path).read_text(encoding="utf-8") == approved

    @pytest.mark.asyncio
    async def test_file_backed_per_action_not_snapshotted(self, tmp_path: Path) -> None:
        # A file-backed spec WITHOUT an auto directive keeps running from its
        # original path (no snapshot), so per-action spec-file resume/edit
        # workflows are unaffected by the B10 fix.
        spec = tmp_path / "spec.md"
        spec.write_text("# Task: t\n## Steps\n1. do\n", encoding="utf-8")
        runner = MagicMock()
        runner._work_dir = tmp_path
        runner.start_background = AsyncMock(return_value="tid")
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request("POST", "/api/taskrunner", app=app)
        req["app"] = ""
        req.json = AsyncMock(
            return_value={"spec": str(spec), "source": "dashboard", "auto_approve": False}
        )
        resp = await api_taskrunner_start(req)
        assert resp.status == 200
        kw = runner.start_background.call_args
        run_path = kw.args[0] if kw.args else kw.kwargs["spec_path"]
        assert kw.kwargs["auto_approve"] is False
        assert str(run_path) == str(spec)  # unchanged: runs from the original file

    # ── /execute launch-path parity ──
    #
    # plan→/execute is the SECOND dashboard launch path. It operates on an
    # already-planned run whose spec was captured in memory at plan time, so it
    # parses ``run.spec_content`` (no fresh disk read → no TOCTOU) and routes the
    # request through the SAME provenance gate as /start. The gate here carries no
    # source claim (the run already exists), so only the dashboard-context check
    # separates a human launch from an app/proxy-embedded one.

    async def _execute_auto_approve_passed(
        self, spec_body: str, request_app: str = "", ui_flag: bool = False,
    ) -> bool:
        runner = MagicMock()
        runner.execute_plan = AsyncMock(return_value=None)
        runner._runs = {
            "t1": Project(
                spec_path="s.md", spec_content=spec_body,
                status="planned", task_id="t1",
            )
        }
        app = web.Application()
        app["state"] = SimpleNamespace(task_runner=runner)
        req = make_mocked_request(
            "POST", "/api/taskrunner/t1/execute", app=app, match_info={"task_id": "t1"},
            headers={"Content-Length": "32"},
        )
        req["app"] = request_app  # "" == dashboard itself
        req.json = AsyncMock(return_value={"auto_approve": ui_flag})
        await api_taskrunner_execute_plan(req)
        return runner.execute_plan.call_args.kwargs["auto_approve"]

    @pytest.mark.asyncio
    async def test_execute_dashboard_spec_auto_grants_without_ui_flag(self) -> None:
        # Dashboard /execute + spec-declared auto → trust, without the UI flag —
        # identical to the /start behavior, so the directive is path-consistent.
        assert await self._execute_auto_approve_passed(
            self._SPEC_AUTO, request_app="", ui_flag=False
        ) is True

    @pytest.mark.asyncio
    async def test_execute_app_embedded_spec_auto_denied(self) -> None:
        # An app/proxy-embedded caller cannot self-trust on /execute either, even
        # when the planned run's spec declares auto.
        assert await self._execute_auto_approve_passed(
            self._SPEC_AUTO, request_app="someapp", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_execute_dashboard_plain_spec_no_trust(self) -> None:
        # No directive + no UI flag → deny-by-default on /execute.
        assert await self._execute_auto_approve_passed(
            self._SPEC_PLAIN, request_app="", ui_flag=False
        ) is False

    @pytest.mark.asyncio
    async def test_execute_ui_flag_still_works_without_directive(self) -> None:
        # The pre-existing UI-flag path on /execute is unchanged by the OR with
        # the spec directive.
        assert await self._execute_auto_approve_passed(
            self._SPEC_PLAIN, request_app="", ui_flag=True
        ) is True
