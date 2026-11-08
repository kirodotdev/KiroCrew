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

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import task_executor
from kiro_crew.context import ContextBuilder
from kiro_crew.hooks import TOOL_ALLOW, TOOL_AUTO_APPROVE, HookManager, ToolHookResult
from kiro_crew.providers.base import LLMEvent
from kiro_crew.sel import SecurityEventLog
from kiro_crew.task_models import Project, Task


def _mock_sessions(provider):
    s = MagicMock()
    s.get_or_create = AsyncMock(return_value=(provider, True, False))

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy="", start_priority=None):
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
        yield LLMEvent(kind="permission_request", title="read", text="",
                       request_id="req-1", tool_kind="tool")
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


def _run_and_task():
    run = Project(spec_path="t.md", spec_content="s", status="running", task_id="tid")
    task = Task(index=1, title="T", description="d")
    run.tasks = [task]
    return run, task


@pytest.fixture(name="audit_rows")
def audit_rows_fixture(tmp_path, monkeypatch):
    """A synchronous SEL under ``tmp_path``; returns a reader of its tool rows.

    The process singleton is swapped through ``monkeypatch``, so the original
    instance is back in place when the test ends.
    """
    monkeypatch.setattr(SecurityEventLog, "_instance", None)
    log = SecurityEventLog(base_dir=tmp_path / "sel", sync=True)

    def _rows():
        path = tmp_path / "sel" / "security_events.jsonl"
        if not path.exists():
            return []
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        return [
            (
                e["operation"],
                e["outcome"],
                e["request_id"],
                (e.get("metadata") or {}).get("reason"),
            )
            for e in events
            if e.get("event_type") == "tool_invocation"
        ]

    assert log is SecurityEventLog()
    return _rows


@pytest.mark.asyncio
async def test_hook_auto_approve_bypasses_interactive_prompt(tmp_path, audit_rows):
    """TOOL_AUTO_APPROVE (config auto_approve_tools) → no interactive prompt."""
    prompt = AsyncMock(return_value=True)
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run, task=task, sessions=sessions, ctx=ctx, agent="",
            on_tool_approval=prompt, auto_test=False, test_cmd=None,
            work_dir=Path(tmp_path), on_notify=AsyncMock(), session_key="k",
        )
    assert audit_rows() == [("read", "approved", "req-1", "hook_auto_approve")]
    prompt.assert_not_called()
    provider.approve_tool.assert_awaited_once_with("req-1")


@pytest.mark.asyncio
async def test_headless_no_authorization_rejects(tmp_path, audit_rows):
    """No handler + no explicit hook auto-approve → deny-by-default (reject)."""
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_ALLOW)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run, task=task, sessions=sessions, ctx=ctx, agent="",
            on_tool_approval=None, auto_test=False, test_cmd=None,
            work_dir=Path(tmp_path), on_notify=AsyncMock(), session_key="k",
        )
    assert audit_rows() == [("read", "rejected", "req-1", "headless_no_authorization")]
    provider.reject_tool.assert_awaited_once_with("req-1")
    provider.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_hook_auto_approve_still_approves(tmp_path, audit_rows):
    """No handler but tool is on the user's auto_approve_tools allowlist
    (TOOL_AUTO_APPROVE) → approve. Explicit trust works headless."""
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_AUTO_APPROVE)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run, task=task, sessions=sessions, ctx=ctx, agent="",
            on_tool_approval=None, auto_test=False, test_cmd=None,
            work_dir=Path(tmp_path), on_notify=AsyncMock(), session_key="k",
        )
    assert audit_rows() == [("read", "approved", "req-1", "hook_auto_approve")]
    provider.approve_tool.assert_awaited_once_with("req-1")
    provider.reject_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_prompt_fires_when_handler_present(tmp_path, audit_rows):
    """Handler present + no auto lever (TOOL_ALLOW) → interactive prompt fires."""
    prompt = AsyncMock(return_value=True)
    provider = _provider_one_tool_then_done()
    sessions = _mock_sessions(provider)
    run, task = _run_and_task()
    ctx = _ctx_with_hook_action(TOOL_ALLOW)
    with patch.object(task_executor.KiroCrewConfig, "load") as cfg:
        cfg.return_value.agent.provider = "acp"
        await task_executor.execute_task(
            run=run, task=task, sessions=sessions, ctx=ctx, agent="",
            on_tool_approval=prompt, auto_test=False, test_cmd=None,
            work_dir=Path(tmp_path), on_notify=AsyncMock(), session_key="k",
        )
    assert audit_rows() == [("read", "approved", "req-1", "interactive_approved")]
    prompt.assert_awaited_once()
    provider.approve_tool.assert_awaited_once_with("req-1")
