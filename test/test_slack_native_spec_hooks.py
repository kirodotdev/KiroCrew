"""An agent spec's own PreToolUse hooks on a native Slack turn's permission requests.

On a backend that never receives the spec's ``hooks`` (KAS here), Crew fires them
itself, and the projection turns every call such a hook covers into a permission
request so the hook can run first. A native Slack turn answers that request in
``_answer_permission_request``: the spec's guard must refuse it before the security
gate's auto-approve, the approval mode, Trust, YOLO or the approval card.

These drive the real ladder with the spec on disk and the real hook store, on a
provider double. The hook process (``run_script_hook``), the hooks' governance
check and the user's own hook files are stubbed out.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import kiro_crew.config.paths as paths_mod
import kiro_crew.hooks as hooks_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, ScriptHookResult, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.slack import handler
from kiro_crew.tool_permission import SPEC_HOOKS_UNREADABLE

_AGENT = "kas-slack-agent"


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def runs(monkeypatch) -> list[str]:
    """The command of every spec hook run; ``guard`` exits 2."""
    seen: list[str] = []
    monkeypatch.setattr(hooks_mod, "_script_hooks_capability_denied", lambda sk="": None)

    async def run(hook, context="", hook_event=None, **_kwargs):
        seen.append(hook.command)
        code = 2 if hook.command == "guard" else 0
        return ScriptHookResult(
            hook_id=hook.id,
            hook_name=hook.name,
            event=hook.event,
            exit_code=code,
            stderr="denied by the guard" if code == 2 else "",
        )

    monkeypatch.setattr(hooks_mod, "run_script_hook", run)
    monkeypatch.setattr(spec_hooks, "_user_hooks_dir", lambda: None)
    return seen


@pytest.fixture
def sel_rows(monkeypatch) -> MagicMock:
    sel_mock = MagicMock()
    monkeypatch.setattr(handler, "sel", lambda: sel_mock)

    async def no_card(*_a: Any, **_k: Any) -> str:
        raise AssertionError("the approval card must not open")

    monkeypatch.setattr(handler, "_request_approval", no_card)
    monkeypatch.setattr(handler, "is_yolo_mode", lambda: False)
    return sel_mock


def _write_spec(command: str, *, raw: str | None = None, matcher: str = "execute_bash") -> None:
    agents = paths_mod.kiro_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    hook = {"matcher": matcher, "command": command} if matcher else {"command": command}
    spec = {"name": _AGENT, "prompt": "p", "tools": ["*"], "hooks": {"preToolUse": [hook]}}
    (agents / f"{_AGENT}.json").write_text(raw or json.dumps(spec), encoding="utf-8")


def _client(tmp_path, backend: str) -> SimpleNamespace:
    return SimpleNamespace(
        capabilities=capabilities_for(backend),
        cwd=str(tmp_path),
        # Only a KAS session records the agent it runs.
        kas_projected_agent=_AGENT if backend == ACP_BACKEND_KAS else "",
        _agent=_AGENT,
        approve_tool=AsyncMock(),
        reject_tool=AsyncMock(),
    )


async def _answer(
    tmp_path,
    backend: str = ACP_BACKEND_KAS,
    *,
    agent: str = _AGENT,
    context_builder: Any = None,
) -> SimpleNamespace:
    """One permission request on an auto-approving native turn running *agent*'s spec."""
    client = _client(tmp_path, backend)
    turn = SimpleNamespace(
        slack=MagicMock(),
        channel="D1",
        reply_ts="1700.1",
        approval_mode=handler.APPROVAL_AUTO,
        context_builder=context_builder,
        session_key="slack:spec-hooks",
        _agent=f"{agent}-record",
        spec_agent=agent,
        answer=SimpleNamespace(accumulated=""),
        task=MagicMock(),
    )
    event = LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title="Say hi",
        tool_kind="execute",
        tool_input=json.dumps({"command": "echo hi"}),
        request_id="r1",
        tool_call_id="t1",
        is_shell=True,
        harness_tool_id="run_command",
    )
    assert await handler._answer_permission_request(turn, client, event) is False
    return client


def _errors(sel_rows: MagicMock) -> list[Any]:
    return [kw.get("error") for _, kw in sel_rows.log_tool_invocation.call_args_list]


@pytest.mark.asyncio
async def test_a_spec_pre_tool_use_deny_refuses_before_auto_approval(tmp_path, runs, sel_rows):
    """The repro: an auto-approving Slack turn must not run a call the spec's guard refuses."""
    _write_spec("guard")
    client = await _answer(tmp_path)
    assert runs == ["guard"]
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()
    assert _errors(sel_rows) == ["spec_hook"]


@pytest.mark.asyncio
async def test_a_call_the_spec_allows_still_reaches_auto_approval(tmp_path, runs, sel_rows):
    _write_spec("allow")
    client = await _answer(tmp_path)
    assert runs == ["allow"]
    client.approve_tool.assert_awaited_once_with("r1")
    client.reject_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_security_gates_auto_approve_does_not_outrank_the_spec(
    tmp_path, runs, sel_rows, monkeypatch
):
    async def grants(*_a: Any, **_k: Any) -> ToolHookResult:
        return ToolHookResult(action=TOOL_AUTO_APPROVE)

    monkeypatch.setattr(handler, "run_in_tool_gate_pool", grants)
    _write_spec("guard")
    client = await _answer(tmp_path, context_builder=SimpleNamespace(hooks=MagicMock()))
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_security_gate_deny_refuses_before_the_spec_hook_runs(
    tmp_path, runs, sel_rows, monkeypatch
):
    async def denies(*_a: Any, **_k: Any) -> ToolHookResult:
        return ToolHookResult(action=TOOL_DENY, reason="blocked by policy")

    monkeypatch.setattr(handler, "run_in_tool_gate_pool", denies)
    _write_spec("guard")
    client = await _answer(tmp_path, context_builder=SimpleNamespace(hooks=MagicMock()))
    assert runs == []
    client.reject_tool.assert_awaited_once_with("r1")
    assert _errors(sel_rows) == ["hook_deny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE])
async def test_a_backend_crew_does_not_gate_reads_no_spec(
    tmp_path, runs, sel_rows, monkeypatch, backend
):
    """kiro-cli runs the spec itself and claude's spec gates nothing: neither is read."""
    read = AsyncMock()
    monkeypatch.setattr(handler, "spec_turn", read)
    _write_spec("guard")
    client = await _answer(tmp_path, backend)
    read.assert_not_awaited()
    assert runs == []
    client.approve_tool.assert_awaited_once_with("r1")


@pytest.mark.asyncio
async def test_an_unreadable_spec_refuses_every_request(tmp_path, runs, sel_rows, monkeypatch):
    steered: list[tuple[str, str]] = []

    async def steer(_provider, _event, reason, *, cause, audited):
        steered.append((reason, cause))

    monkeypatch.setattr(handler, "_steer_host_deny", steer)
    _write_spec("guard", raw="{not json")
    client = await _answer(tmp_path)
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()
    assert steered == [(SPEC_HOOKS_UNREADABLE, handler.DENY_CAUSE_POLICY)]
    assert _errors(sel_rows) == ["spec_hook"]


@pytest.mark.asyncio
async def test_a_goose_turn_is_gated_by_its_template_agent_not_its_record_name(
    tmp_path, runs, sel_rows
):
    """A turn's ``_agent`` may be a Crew record's name, which has no spec."""
    _write_spec("guard", matcher="")
    client = await _answer(tmp_path, ACP_BACKEND_GOOSE)
    assert runs == ["guard"]
    client.reject_tool.assert_awaited_once_with("r1")
    client.approve_tool.assert_not_awaited()
