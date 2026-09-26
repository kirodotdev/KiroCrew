"""Regression tests for task runner must honor explicit auto-approve.

Before the fix, task_executor.execute_task's EVENT_PERMISSION_REQUEST loop only
handled TOOL_DENY and unconditionally fell through to the interactive
`on_tool_approval` prompt — ignoring the user-configured auto-approve trust
(`hooks.auto_approve_tools` → TOOL_AUTO_APPROVE). These tests drive a single
permission_request through execute_task with an interactive handler present and
assert the handler is bypassed only on the explicit-trust path, and still fires
otherwise. (Global YOLO / safety-override is deliberately NOT honored here.)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import task_executor
from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    HookManager,
    ScriptHookResult,
    ToolHookResult,
)
from kiro_crew.providers.base import LLMEvent
from kiro_crew.task_models import Project, Task


def _mock_sessions(provider):
    s = MagicMock()
    s.get_or_create = AsyncMock(return_value=(provider, True, False))

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release_subagent_runtime = AsyncMock()
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    return s


def _provider_one_tool_then_done():
    provider = MagicMock()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request", title="read", text="", request_id="req-1", tool_kind="tool"
        )
        yield LLMEvent(kind="text_chunk", text="done")
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


def _ctx_with_hook_action(action: str) -> ContextBuilder:
    hooks = MagicMock(spec=HookManager)
    hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=action))
    ctx = MagicMock(spec=ContextBuilder)
    ctx.hooks = hooks
    ctx.build_message = MagicMock(return_value=("prompt", None))
    return ctx


@pytest.fixture(autouse=True)
def _hook_store(monkeypatch):
    store = MagicMock()
    store.fire = AsyncMock(return_value=[])
    monkeypatch.setattr(task_executor, "get_global_hook_store", lambda: store)
    return store


def _run_and_task():
    run = Project(spec_path="t.md", spec_content="s", status="running", task_id="tid")
    task = Task(index=1, title="T", description="d")
    run.tasks = [task]
    return run, task


@pytest.mark.asyncio
async def test_hook_auto_approve_bypasses_interactive_prompt(tmp_path):
    """TOOL_AUTO_APPROVE (config auto_approve_tools) → no interactive prompt."""
    prompt = AsyncMock(return_value=True)
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=prompt,
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )
    prompt.assert_not_called()
    provider.approve_tool.assert_awaited_once_with("req-1")


@pytest.mark.asyncio
async def test_headless_no_authorization_rejects(tmp_path):
    """No handler + no explicit hook auto-approve → deny-by-default (reject)."""
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_ALLOW)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=None,
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )
    provider.reject_tool.assert_awaited_once_with("req-1")
    provider.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_hook_auto_approve_still_approves(tmp_path):
    """No handler but tool is on the user's auto_approve_tools allowlist
    (TOOL_AUTO_APPROVE) → approve. Explicit trust works headless."""
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=None,
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )
    provider.approve_tool.assert_awaited_once_with("req-1")
    provider.reject_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_prompt_fires_when_handler_present(tmp_path):
    """Handler present + no auto lever (TOOL_ALLOW) → interactive prompt fires."""
    prompt = AsyncMock(return_value=True)
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_ALLOW)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=prompt,
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )
    prompt.assert_awaited_once()
    provider.approve_tool.assert_awaited_once_with("req-1")


@pytest.mark.asyncio
async def test_task_permission_request_hook_deny_rejects_and_audits_first(tmp_path, _hook_store):
    provider = _provider_one_tool_then_done()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request",
            title="read",
            request_id="req-1",
            tool_kind="tool",
            tool_input='["value"]',
        )
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    _hook_store.fire.return_value = [
        ScriptHookResult(
            hook_id="h1",
            hook_name="deny",
            event=HOOK_EVENT_PRE_TOOL_USE,
            exit_code=2,
            stderr="nope",
        )
    ]
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    order: list[str] = []
    audit = MagicMock()
    audit.log_tool_invocation.side_effect = lambda **_: order.append("audit")

    async def _reject(_request_id):
        order.append("reject")

    provider.reject_tool = AsyncMock(side_effect=_reject)
    with (
        patch.object(task_executor.KiroCrewConfig, "load") as cfg,
        patch.object(task_executor, "sel", return_value=audit),
    ):
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=AsyncMock(return_value=True),
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )

    provider.reject_tool.assert_awaited_once_with("req-1")
    provider.approve_tool.assert_not_awaited()
    assert _hook_store.fire.await_args.kwargs["tool_input"] == ["value"]
    assert order == ["audit", "reject"]
    assert audit.log_tool_invocation.call_count == 1
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "hook_blocked"


@pytest.mark.asyncio
async def test_task_permission_request_without_context_still_gates(tmp_path, _hook_store):
    provider = _provider_one_tool_then_done()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request",
            title="read",
            request_id="req-ctxless",
            tool_kind="tool",
            tool_input='{"path": "/tmp/x"}',
        )
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    _hook_store.fire.return_value = [
        ScriptHookResult(
            hook_id="h1",
            hook_name="deny",
            event=HOOK_EVENT_PRE_TOOL_USE,
            exit_code=2,
            stderr="nope",
        )
    ]
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    order: list[str] = []
    audit = MagicMock()
    audit.log_tool_invocation.side_effect = lambda **_: order.append("audit")

    async def _reject(_request_id):
        order.append("reject")

    provider.reject_tool = AsyncMock(side_effect=_reject)
    prompt = AsyncMock(return_value=True)
    with (
        patch.object(task_executor.KiroCrewConfig, "load") as cfg,
        patch.object(task_executor, "sel", return_value=audit),
    ):
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=None,
            agent="",
            on_tool_approval=prompt,
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )

    provider.reject_tool.assert_awaited_once_with("req-ctxless")
    provider.approve_tool.assert_not_awaited()
    prompt.assert_not_awaited()
    assert order == ["audit", "reject"]
    assert audit.log_tool_invocation.call_count == 1
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "hook_blocked"


@pytest.mark.asyncio
async def test_task_permission_request_without_hook_store_rejects(tmp_path, monkeypatch):
    provider = _provider_one_tool_then_done()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request",
            title="read",
            request_id="req-1",
            tool_kind="tool",
            tool_input='{"path": "/tmp/x"}',
        )
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    monkeypatch.setattr(task_executor, "get_global_hook_store", lambda: None)
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    audit = MagicMock()
    with (
        patch.object(task_executor.KiroCrewConfig, "load") as cfg,
        patch.object(task_executor, "sel", return_value=audit),
    ):
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=AsyncMock(return_value=True),
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )

    provider.reject_tool.assert_awaited_once_with("req-1")
    provider.approve_tool.assert_not_awaited()
    assert audit.log_tool_invocation.call_count == 1
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "hook_blocked"
    assert audit.log_tool_invocation.call_args.kwargs["error"] == "hook store not initialized"


@pytest.mark.asyncio
async def test_task_permission_request_hook_error_rejects_and_audits_first(tmp_path, _hook_store):
    provider = _provider_one_tool_then_done()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="permission_request",
            title="read",
            request_id="req-error",
            tool_kind="tool",
            tool_input='{"path": "/tmp/x"}',
        )
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    _hook_store.fire.side_effect = RuntimeError("hook boom")
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    order: list[str] = []
    audit = MagicMock()
    audit.log_tool_invocation.side_effect = lambda **_: order.append("audit")

    async def _reject(_request_id):
        order.append("reject")

    provider.reject_tool = AsyncMock(side_effect=_reject)
    with (
        patch.object(task_executor.KiroCrewConfig, "load") as cfg,
        patch.object(task_executor, "sel", return_value=audit),
    ):
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=AsyncMock(return_value=True),
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )

    provider.reject_tool.assert_awaited_once_with("req-error")
    provider.approve_tool.assert_not_awaited()
    assert order == ["audit", "reject"]
    assert audit.log_tool_invocation.call_count == 1
    assert audit.log_tool_invocation.call_args.kwargs["outcome"] == "hook_error"
    assert audit.log_tool_invocation.call_args.kwargs["error"] == "hook boom"


@pytest.mark.asyncio
async def test_task_combined_events_fire_once_per_identity(tmp_path, _hook_store):
    provider = _provider_one_tool_then_done()

    async def _stream(msg: str):
        yield LLMEvent(
            kind="tool_call",
            title="read",
            tool_call_id="call-1",
            tool_input='{"path": "/tmp/one"}',
        )
        yield LLMEvent(
            kind="permission_request",
            title="read",
            request_id="req-1",
            tool_call_id="call-1",
            tool_input='{"path": "/tmp/one"}',
        )
        yield LLMEvent(
            kind="tool_call",
            title="read",
            tool_call_id="call-2",
            tool_input='"scalar"',
        )
        yield LLMEvent(kind="complete")

    provider.stream = _stream
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    with (
        patch.object(task_executor.KiroCrewConfig, "load") as cfg,
        patch.object(task_executor, "sel"),
    ):
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run,
            task=task,
            sessions=sessions,
            ctx=ctx,
            agent="",
            on_tool_approval=AsyncMock(return_value=True),
            auto_test=False,
            test_cmd=None,
            work_dir=Path(tmp_path),
            on_notify=AsyncMock(),
            session_key="k",
        )

    provider.approve_tool.assert_awaited_once_with("req-1")
    assert _hook_store.fire.await_count == 2
    assert [call.kwargs["tool_input"] for call in _hook_store.fire.await_args_list] == [
        {"path": "/tmp/one"},
        "scalar",
    ]
