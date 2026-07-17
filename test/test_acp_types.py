"""Tests for ACP types."""

from kiro_crew.acp.types import AcpPromptStats, JsonRpcMessage, JsonRpcRequest


class TestJsonRpcRequest:
    def test_to_dict(self):
        req = JsonRpcRequest(method="initialize", params={"key": "val"}, id=1)
        d = req.to_dict()
        assert d["jsonrpc"] == "2.0"
        assert d["id"] == 1
        assert d["method"] == "initialize"
        assert d["params"] == {"key": "val"}


class TestJsonRpcMessage:
    def test_is_response_for_matching(self):
        msg = JsonRpcMessage(id=42, result={"ok": True})
        assert msg.is_response_for(42)

    def test_is_response_for_non_matching(self):
        msg = JsonRpcMessage(id=42, result={"ok": True})
        assert not msg.is_response_for(99)

    def test_is_response_for_rejects_request_with_colliding_id(self):
        # Regression: an inbound server→client REQUEST (has method) whose id
        # collides with our in-flight prompt req_id must NOT be treated as the
        # prompt's response — otherwise the turn ends early and the tool
        # permission is never answered (stuck Claude Code turn on follow-ups).
        perm_req = JsonRpcMessage(
            id=4, method="session/request_permission", params={"toolCall": {}}
        )
        assert not perm_req.is_response_for(4)

    def test_is_response_for_error_response(self):
        # An error response (id + error, no method) is still a response.
        msg = JsonRpcMessage(id=7, error={"code": -32602, "message": "bad"})
        assert msg.is_response_for(7)

    def test_is_method_matching(self):
        msg = JsonRpcMessage(method="session/update")
        assert msg.is_method("session/update")

    def test_is_method_non_matching(self):
        msg = JsonRpcMessage(method="session/update")
        assert not msg.is_method("session/prompt")

    def test_is_method_none(self):
        msg = JsonRpcMessage()
        assert not msg.is_method("anything")


class TestAcpPromptStats:
    def test_defaults(self):
        stats = AcpPromptStats()
        assert stats.event_count == 0
        assert stats.text_chunks == 0
        assert stats.tool_calls == []
        assert stats.context_pct == 0.0
        # Raw token counts default to 0 (unknown) until a usage_update arrives.
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0
