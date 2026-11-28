"""Stage 5: MCP dispatch extraction preserves Crew approvals and fail-closed routing."""
from __future__ import annotations

from kiro_byo.acp_server import AcpServer
from kiro_byo.mcp_tool_dispatch import McpToolDispatchMixin
from kiro_byo.turn_runner import TurnRunnerMixin
from test_s104_tool_execution_guard import NAME, fixture, finish
from kiro_byo.providers.base import ToolCall


def test_mcp_dispatch_is_inherited_without_changing_turn_runner_public_entrypoint():
    assert issubclass(TurnRunnerMixin, McpToolDispatchMixin)
    assert TurnRunnerMixin._execute_mcp_tool is McpToolDispatchMixin._execute_mcp_tool
    assert AcpServer._execute_mcp_tool is McpToolDispatchMixin._execute_mcp_tool


def test_transport_exception_after_dispatch_is_uncertain_and_not_retried(tmp_path):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kwargs: events.append((event, kwargs.get('data', {})))
    host._request_tool_permission = lambda *args: True
    attempts = []
    def broken_transport(name, args):
        attempts.append((name, args))
        raise OSError('secret_broker_endpoint=https://internal.invalid/token')
    session.mcp.call = broken_transport
    try:
        result, elapsed, stop = host._execute_mcp_tool(session, ToolCall('c5', NAME, '{}'), {})
        assert 'MCP tool transport failed (OSError)' in result
        assert 'Verify external state before retrying' in result
        assert 'secret_broker_endpoint' not in result
        assert attempts == [(NAME, {})]
        assert elapsed >= 0 and stop == ''
        assert statuses[-1][1] == 'failed'
        assert any(k == 'tool.effect.uncertain' and d.get('reason') == 'tool_error'
                   for k, d in events)
    finally:
        finish(host, session)


def test_approved_mcp_tool_cannot_dispatch_after_binding_changes(tmp_path):
    host, session, statuses = fixture(tmp_path)
    host._request_tool_permission = lambda *_: session.mcp.bindings.pop(NAME) is not None
    try:
        result, ms, reason = host._execute_mcp_tool(session, ToolCall('c6', NAME, '{}'), {})
        assert 'revoked or changed' in result
        assert ms == 0.0 and reason == ''
        assert session.mcp.calls == []
        assert statuses[-1][1] == 'refused'
    finally:
        finish(host, session)
