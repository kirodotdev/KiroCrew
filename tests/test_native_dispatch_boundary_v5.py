"""Fault injection for V5 native dispatch; assertions concern real boundaries.

Tests exercise AcpServer -> inherited NativeToolDispatchMixin with an actual
sandbox runtime, not mocked route/control code. No external Provider required.
"""
from __future__ import annotations

import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.execution_guard import ExecutionGuard
from kiro_byo.native_tools import NativeToolRuntime, PermissionDecision
from kiro_byo.providers.base import ToolCall
from kiro_byo.sandbox import SandboxError


@pytest.fixture
def fixture(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    session = Session("v5-native", str(workspace), "model", native=NativeToolRuntime(workspace))
    events, statuses, audits = [], [], []
    server._runtime_event = lambda name, *args, **kwargs: events.append((name, kwargs))
    server._emit_tool_call = lambda *args, **kwargs: None
    server._emit_tool_status = lambda s, id, *, status, text="": statuses.append((status, text))
    session.native.audit.record = lambda **kwargs: audits.append(kwargs)
    try:
        yield SimpleNamespace(server=server, session=session, workspace=workspace,
                              outside=outside, events=events, statuses=statuses, audits=audits)
    finally:
        session.native.close()
        server._executor.shutdown(wait=True)


def _outside_write(fx):
    target = fx.outside / "effect.txt"
    call = ToolCall("native-w", "write", f'{{"path": "{target}", "content": "changed"}}')
    return target, call


def _denied_after_approval(fx, *, cancelled=False):
    target, call = _outside_write(fx)
    def approve(*args):
        (fx.session.cancel_event if cancelled else fx.session.steer_event).set()
        return "allow_once"
    fx.server._request_tool_permission_choice = approve
    with patch.object(fx.session.native, "grant_scope_roots", side_effect=AssertionError("grant after interruption")):
        text, _, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert outcome == ("cancelled" if cancelled else "steered")
    assert "before dispatch" in text
    assert not target.exists()
    assert any(name == "tool.dispatch.blocked" for name, _ in fx.events)


def test_allow_once_reverted_after_success(fixture):
    fx = fixture
    target, call = _outside_write(fx)
    fx.server._request_tool_permission_choice = lambda *args: "allow_once"
    result, elapsed, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert outcome == "" and elapsed >= 0
    assert target.read_text() == "changed"
    assert fx.statuses[-1][0] == "completed"
    assert fx.audits[-1]["permission"] == "scope_once"
    assert fx.audits[-1]["outcome"] == "completed"
    with pytest.raises(SandboxError):
        fx.session.native.preflight(fx.session.native.validate("read", {"path": str(target)}))


@pytest.mark.parametrize("cancelled", [False, True])
def test_cancel_or_steer_during_scope_approval_blocks_grant(fixture, cancelled):
    _denied_after_approval(fixture, cancelled=cancelled)


def test_preflight_failure_revokes_allow_once(fixture):
    fx = fixture
    target, call = _outside_write(fx)
    fx.server._request_tool_permission_choice = lambda *args: "allow_once"
    with patch.object(fx.session.native, "preflight", side_effect=SandboxError("injected preflight")):
        result, _, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert "injected preflight" in result and outcome == ""
    assert not target.exists()
    assert fx.audits[-1]["outcome"] == "preflight_failed"
    with pytest.raises(SandboxError):
        fx.session.native.preflight(fx.session.native.validate("read", {"path": str(target)}))


def test_exception_during_execution_revokes_scope_and_audits_failure(fixture):
    fx = fixture
    target, call = _outside_write(fx)
    fx.server._request_tool_permission_choice = lambda *args: "allow_once"
    with patch.object(fx.session.native, "execute", side_effect=OSError("injected disk error")):
        result, _, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert result == "Error: injected disk error" and outcome == ""
    assert not target.exists()
    assert fx.statuses[-1][0] == "failed"
    assert fx.audits[-1]["outcome"] == "failed"
    assert any(name == "tool.call.completed" for name, _ in fx.events)
    with pytest.raises(SandboxError):
        fx.session.native.preflight(fx.session.native.validate("read", {"path": str(target)}))


def test_cancel_after_scope_grant_before_execution_revokes_scope(fixture):
    fx = fixture
    target, call = _outside_write(fx)
    fx.server._request_tool_permission_choice = lambda *args: "allow_once"
    original_preflight = fx.session.native.preflight
    def cancel_after_preflight(invocation):
        original_preflight(invocation)
        fx.session.cancel_event.set()
    with patch.object(fx.session.native, "preflight", side_effect=cancel_after_preflight):
        result, _, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert outcome == "cancelled"
    assert "before dispatch" in result
    assert not target.exists()
    with pytest.raises(SandboxError):
        original_preflight(fx.session.native.validate("read", {"path": str(target)}))


def test_static_permission_denial_never_prompts_or_executes(fixture):
    fx = fixture
    target, call = _outside_write(fx)
    with patch.object(fx.session.native.permission_engine, "evaluate", return_value=PermissionDecision.DENY), \
         patch.object(fx.server, "_request_tool_permission", side_effect=AssertionError("should not prompt")), \
         patch.object(fx.session.native, "execute", side_effect=AssertionError("should not execute")):
        result, _, outcome = fx.server._execute_native_tool(fx.session, call, call.arguments())
    assert "Permission denied" in result and outcome == ""
    assert fx.audits[-1]["permission"] == "local_deny"
    assert not target.exists()


def test_cancel_mid_batch_prevents_later_dispatch(fixture):
    fx = fixture
    dispatched = []
    fx.server._append_tool_result = lambda *args: None
    def dispatch(session, call, arguments):
        dispatched.append(call.call_id)
        session.cancel_event.set()
        return "done", 1.0, "cancelled"
    fx.server._execute_native_tool = dispatch
    names = [ToolCall("first", "read", '{}'), ToolCall("second", "read", '{}')]
    outcome = fx.server._execute_tool_batch_impl(
        fx.session, tool_calls=names, round_role="lead",
        round_provider=SimpleNamespace(id="p"), round_model=SimpleNamespace(model_id="m"),
        step=1, execution_guard=ExecutionGuard(), provider_rounds_total=1,
    )
    assert outcome.cancelled is True
    assert dispatched == ["first"]


def test_batch_duplicate_call_id_cannot_repeat_side_effect(fixture):
    fx = fixture
    dispatched, results = [], []
    fx.server._execute_native_tool = lambda session, call, args: (dispatched.append(call.call_id) or "ok", 1.0, "")
    fx.server._append_tool_result = lambda s, id, result: results.append(result)
    fx.server._observe_tool_execution_guard = lambda *a, **k: (SimpleNamespace(action="continue"), "", "")
    calls = [ToolCall("same", "write", '{"path":"x.txt","content":"one"}'),
             ToolCall("same", "write", '{"path":"x.txt","content":"two"}')]
    outcome = fx.server._execute_tool_batch_impl(
        fx.session, tool_calls=calls, round_role="lead",
        round_provider=SimpleNamespace(id="p"), round_model=SimpleNamespace(model_id="m"),
        step=1, execution_guard=ExecutionGuard(), provider_rounds_total=1,
    )
    assert outcome.cancelled is False
    assert dispatched == ["same"]
    assert len(results) == 2 and "duplicate" in results[-1]
    assert any(name == "tool.dispatch.blocked" for name, _ in fx.events)
