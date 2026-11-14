"""S1-04: approval is not a dispatch grant after Crew control state changes."""
from __future__ import annotations

import io
from types import SimpleNamespace

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.execution_guard import ExecutionGuard
from kiro_byo.mcp_client import ToolBinding
from kiro_byo.native_tools import NativeToolRuntime
from kiro_byo.providers.base import ToolCall


NAME = "mcp__kirocrew-core__browser"
BINDING = ToolBinding(NAME, "kirocrew-core", "browser", "browser", {},
                      annotations={"readOnlyHint": True, "idempotentHint": True})


class FakeMcp:
    def __init__(self):
        self.bindings = {NAME: BINDING}
        self.live = True
        self.calls = []
        self.result = "done"

    def is_live_binding(self, name):
        return self.live

    def call(self, name, args):
        self.calls.append((name, args))
        return self.result


def fixture(tmp_path, *, native=False):
    server = AcpServer(stdin=io.StringIO(), stdout=io.StringIO())
    server._runtime_event = lambda *a, **kw: None
    statuses = []
    server._emit_tool_call = lambda s, id, name, args: {"toolCallId": id}
    server._emit_tool_status = lambda s, id, *, status, text="": statuses.append((id, status, text))
    session = Session("tool-test", str(tmp_path), "model",
                      mcp=FakeMcp(), native=NativeToolRuntime(tmp_path) if native else None)
    return server, session, statuses


def finish(server, session):
    if session.native:
        session.native.close()
    server._executor.shutdown(wait=True)


def test_mcp_cancel_received_during_approval_blocks_dispatch(tmp_path):
    server, session, statuses = fixture(tmp_path)
    def approve_and_cancel(*args):
        session.cancel_event.set()
        return True
    server._request_tool_permission = approve_and_cancel
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert result == "cancelled"
        assert "before dispatch" in text
        assert session.mcp.calls == []
        assert statuses[-1][1] == "failed"
    finally:
        finish(server, session)


def test_mcp_steer_received_during_approval_blocks_dispatch(tmp_path):
    server, session, statuses = fixture(tmp_path)
    def approve_and_steer(*args):
        session.steer_event.set()
        return True
    server._request_tool_permission = approve_and_steer
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert result == "steered"
        assert session.mcp.calls == []
        assert statuses[-1][1] == "failed"
    finally:
        finish(server, session)


def test_mcp_grant_revoked_during_approval_blocks_dispatch(tmp_path):
    server, session, statuses = fixture(tmp_path)
    def approve_and_revoke(*args):
        session.mcp.live = False
        return True
    server._request_tool_permission = approve_and_revoke
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert result == ""
        assert "revoked" in text
        assert session.mcp.calls == []
        assert statuses[-1][1] == "refused"
    finally:
        finish(server, session)


def test_mcp_binding_identity_swap_during_approval_blocks_dispatch(tmp_path):
    server, session, statuses = fixture(tmp_path)
    def approve_and_replace(*args):
        session.mcp.bindings[NAME] = ToolBinding(NAME, "different", "different", "unsafe", {})
        return True
    server._request_tool_permission = approve_and_replace
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert result == ""
        assert "changed" in text
        assert session.mcp.calls == []
    finally:
        finish(server, session)


def test_native_file_write_cancelled_during_approval_has_no_side_effect(tmp_path):
    server, session, statuses = fixture(tmp_path, native=True)
    def approve_and_cancel(*args):
        session.cancel_event.set()
        return True
    server._request_tool_permission = approve_and_cancel
    try:
        call = ToolCall("n1", "write", '{"path":"new.txt","content":"should not exist"}')
        text, ms, result = server._execute_native_tool(session, call, call.arguments())
        assert result == "cancelled"
        assert not (tmp_path / "new.txt").exists()
        assert statuses[-1][1] == "failed"
    finally:
        finish(server, session)


def test_mcp_error_never_implies_replay_safe(tmp_path):
    server, session, statuses = fixture(tmp_path)
    session.mcp.result = "Error: upstream timed out"
    server._request_tool_permission = lambda *args: True
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert session.mcp.calls == [(NAME, {})]
        assert "Verify external state before retrying" in text
        assert result == ""
        assert statuses[-1][1] == "failed"
        assert server._tool_effect_class(session, NAME) == "unknown_write"
    finally:
        finish(server, session)


def test_mcp_cancel_during_call_reports_cancel_and_may_have_executed(tmp_path):
    server, session, statuses = fixture(tmp_path)
    server._request_tool_permission = lambda *args: True
    def execute_and_cancel(name, args):
        session.cancel_event.set()
        session.mcp.calls.append((name, args))
        return "OK"
    session.mcp.call = execute_and_cancel
    try:
        text, ms, result = server._execute_mcp_tool(session, ToolCall("c1", NAME, "{}"), {})
        assert result == "cancelled"
        assert session.mcp.calls == [(NAME, {})]
        assert statuses[-1][1] == "completed"  # cannot undo acknowledged remote effects
    finally:
        finish(server, session)


def test_duplicate_id_in_one_batch_does_not_dispatch_twice(tmp_path):
    server, session, statuses = fixture(tmp_path)
    dispatched = []
    results = []
    server._execute_mcp_tool = lambda sess, call, args: (dispatched.append(call.call_id) or "ok", 1.0, "")
    server._append_tool_result = lambda sess, id, result: results.append((id, result))
    server._observe_tool_execution_guard = lambda *args, **kwargs: (SimpleNamespace(action="continue"), "", "")
    try:
        outcome = server._execute_tool_batch_impl(
            session, tool_calls=[ToolCall("shared", NAME, "{}"), ToolCall("shared", NAME, "{}")],
            round_role="lead", round_provider=SimpleNamespace(id="p"),
            round_model=SimpleNamespace(model_id="m"), step=1,
            execution_guard=ExecutionGuard(), provider_rounds_total=1,
        )
        assert dispatched == ["shared"]
        assert len(results) == 2
        assert "duplicate" in results[1][1]
        assert statuses[-1][1] == "refused"
    finally:
        finish(server, session)
