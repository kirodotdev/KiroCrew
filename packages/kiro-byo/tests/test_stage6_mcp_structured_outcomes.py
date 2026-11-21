"""Stage 6: MCP protocol status and terminal tool receipts are independent of text."""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from kiro_byo.mcp_client import McpCallOutcome, McpManager, ToolBinding
from kiro_byo.mcp_transports import McpError, McpInputRequired, McpJsonRpcError, McpProtocolError
from kiro_byo.providers.base import ToolCall
from test_s104_tool_execution_guard import NAME, fixture, finish


class FakeServer:
    def __init__(self, result: dict[str, Any] | None = None, *, error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def is_running(self) -> bool:
        return True

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def close(self) -> None:
        pass


def make_manager(server: FakeServer) -> McpManager:
    manager = McpManager([])
    manager.bindings = {
        NAME: ToolBinding(NAME, "fake", "execute", "Test", {"type": "object"})
    }
    manager.servers = {"fake": server}  # type: ignore[assignment]
    return manager


@pytest.mark.parametrize("result,expected_failed", [
    ({"content": [{"type": "text", "text": "Error: count=0 is normal"}], "isError": False}, False),
    ({"content": [{"type": "text", "text": "Error: count=0 is normal"}]}, False),
    ({"content": [{"type": "text", "text": "done"}], "isError": True}, True),
    ({"content": [{"type": "text", "text": "done"}], "isError": False}, False),
])
def test_mcp_manager_uses_protocol_is_error_not_text(result, expected_failed):
    manager = make_manager(FakeServer(result))
    try:
        outcome = manager.call_with_status(NAME, {})
        assert outcome.dispatched is True
        assert outcome.is_error is expected_failed
        assert manager.call(NAME, {}) == outcome.text  # historical API retained
    finally:
        manager.close()


def test_mcp_manager_reports_post_dispatch_mcp_error_as_uncertain():
    manager = make_manager(FakeServer(error=McpError("call rejected")))
    try:
        outcome = manager.call_with_status(NAME, {})
        assert outcome.is_error and outcome.dispatched
        assert outcome.text == "Error: MCP tool transport failed."
    finally:
        manager.close()


@pytest.mark.parametrize("error,expected", [
    (McpError("https://private.example/secret?token=DO_NOT_LEAK"),
     "MCP tool transport failed"),
    (McpJsonRpcError(-32603, "Authorization: Bearer DO_NOT_LEAK"),
     "MCP tool returned a JSON-RPC error"),
    (McpProtocolError("requestState=DO_NOT_LEAK"),
     "MCP tool returned an unusable protocol response"),
    (McpInputRequired("requestState=DO_NOT_LEAK"),
     "MCP tool requires interactive client input"),
])
def test_remote_mcp_errors_never_expose_peer_payloads(error, expected):
    fake = FakeServer(error=error)
    manager = make_manager(fake)
    try:
        outcome = manager.call_with_status(NAME, {"intent": "read"})
        assert outcome.is_error is True
        assert outcome.dispatched is True  # cannot assume the remote action was a no-op
        assert expected in outcome.text
        assert "DO_NOT_LEAK" not in outcome.text
        assert "private.example" not in outcome.text
        assert fake.calls == [("execute", {"intent": "read"})]
    finally:
        manager.close()


def test_interactive_mcp_error_keeps_unknown_side_effect_receipt(tmp_path):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kw: events.append((event, kw.get("data", {})))
    host._request_tool_permission = lambda *_: True
    manager = make_manager(FakeServer(error=McpInputRequired("secret requestState")))
    session.mcp = manager
    try:
        result, _, _ = host._execute_mcp_tool(session, ToolCall("c-input", NAME, "{}"), {})
        assert "requires interactive client input" in result
        assert "secret requestState" not in result
        assert "Verify external state" in result
        assert statuses[-1][1] == "failed"
        uncertain = [data for name, data in events if name == "tool.effect.uncertain"]
        assert len(uncertain) == 1 and uncertain[0]["effect_class"] == "unknown_write"
    finally:
        finish(host, session)


def test_mcp_manager_closed_is_pre_dispatch_failure():
    manager = make_manager(FakeServer({"content": []}))
    manager.close()
    outcome = manager.call_with_status(NAME, {})
    assert outcome.is_error
    assert outcome.dispatched is False


def test_successful_error_prefixed_content_is_completed_not_uncertain(tmp_path):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kw: events.append(event)
    host._request_tool_permission = lambda *_: True
    session.mcp.call_with_status = lambda name, args: McpCallOutcome(
        "Error: count=0 in log file (normal output)", False, True,
    )
    try:
        text, elapsed, interruption = host._execute_mcp_tool(
            session, ToolCall("c6", NAME, "{}"), {}
        )
        assert text == "Error: count=0 in log file (normal output)"
        assert elapsed >= 0 and interruption == ""
        assert statuses[-1][1] == "completed"
        assert "tool.effect.uncertain" not in events
        assert "tool.call.completed" in events
    finally:
        finish(host, session)


def test_protocol_error_is_failed_with_uncertain_receipt(tmp_path):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kw: events.append((event, kw.get("data", {})))
    host._request_tool_permission = lambda *_: True
    session.mcp.call_with_status = lambda name, args: McpCallOutcome(
        "Error: MCP tool reported failure\npermission denied", True, True,
    )
    try:
        result, elapsed, stop = host._execute_mcp_tool(session, ToolCall("c7", NAME, "{}"), {})
        assert statuses[-1][1] == "failed"
        assert "permission denied" in result
        assert "Verify external state" in result
        assert len([e for e, _ in events if e == "tool.effect.uncertain"]) == 1
        assert events[-1][0] == "tool.call.completed"
    finally:
        finish(host, session)


def test_pre_dispatch_manager_error_is_terminal_but_not_uncertain(tmp_path):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kw: events.append(event)
    host._request_tool_permission = lambda *_: True
    session.mcp.call_with_status = lambda name, args: McpCallOutcome(
        "Error: MCP session is closed; tool was not dispatched.", True, False,
    )
    try:
        result, _, _ = host._execute_mcp_tool(session, ToolCall("c8", NAME, "{}"), {})
        assert statuses[-1][1] == "failed"
        assert "not dispatched" in result
        assert "tool.effect.uncertain" not in events
    finally:
        finish(host, session)


@pytest.mark.parametrize("error", [RuntimeError("secret://token/example"), ValueError("private data")])
def test_non_os_error_closes_receipt_without_leaking_or_retrying(tmp_path, error):
    host, session, statuses = fixture(tmp_path)
    events = []
    host._runtime_event = lambda event, sess, **kw: events.append((event, kw.get("data", {})))
    host._request_tool_permission = lambda *_: True
    attempts = []

    def broken(name, args):
        attempts.append((name, args))
        raise error

    session.mcp.call = broken
    try:
        result, elapsed, interruption = host._execute_mcp_tool(
            session, ToolCall("c9", NAME, "{}"), {}
        )
        assert attempts == [(NAME, {})]
        assert type(error).__name__ in result
        assert str(error) not in result
        assert "Verify external state" in result
        assert elapsed >= 0 and interruption == ""
        assert [s[1] for s in statuses][-2:] == ["in_progress", "failed"]
        assert any(e == "tool.effect.uncertain" for e, _ in events)
        assert events[-1][0] == "tool.call.completed"
    finally:
        finish(host, session)


def test_keyboard_interrupt_is_not_swallowed(tmp_path):
    host, session, statuses = fixture(tmp_path)
    host._request_tool_permission = lambda *_: True
    session.mcp.call = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
    try:
        with pytest.raises(KeyboardInterrupt):
            host._execute_mcp_tool(session, ToolCall("c10", NAME, "{}"), {})
    finally:
        finish(host, session)
