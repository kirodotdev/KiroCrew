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

    def test_carry_over_preserves_context_state_intra_session(self):
        """Within ONE session, context state must survive the per-turn re-init
        (#2932's correct half: a turn boundary must not re-report an empty
        context) while per-turn counters restart at zero."""
        stats = AcpPromptStats(
            event_count=7,
            text_chunks=3,
            tool_calls=[("execute", "ls")],
            context_pct=88.5,
            context_used_tokens=177_000,
            context_window_tokens=200_000,
            context_tokens_from_usage=True,
        )
        carried = stats.carry_over()
        assert carried.context_pct == 88.5
        assert carried.context_used_tokens == 177_000
        assert carried.context_window_tokens == 200_000
        assert carried.context_tokens_from_usage is True
        assert carried.context_pct_unknown is False
        # Per-turn counters do NOT carry.
        assert carried.event_count == 0
        assert carried.text_chunks == 0
        assert carried.tool_calls == []

    def test_reset_context_state_drops_session_scoped_fields(self):
        """At a warm-pool handoff every context field must drop (#2932's bug
        half: the stats describe whatever the runtime did before the re-bind),
        landing on plain dataclass defaults. unknown stays False — the claimed
        runtime serves a fresh never-prompted session, and True would collide
        with the compacted-in-place recycle signal (pct==0 and unknown)."""
        stats = AcpPromptStats(
            context_pct=92.0,
            context_used_tokens=184_000,
            context_window_tokens=200_000,
            context_tokens_from_usage=True,
            context_pct_unknown=True,
        )
        stats.reset_context_state()
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0
        assert stats.context_tokens_from_usage is False
        assert stats.context_pct_unknown is False

    def test_reset_context_state_does_not_match_recycle_predicate(self):
        """A just-claimed provider must not read as 'compacted in place'
        (pct == 0.0 and unknown) — that predicate drives the background
        recycle decision, and matching it would self-recycle every pool
        claim at its first turn-end if the two paths ever meet."""
        stats = AcpPromptStats(context_pct=88.0, context_used_tokens=170_000)
        stats.reset_after_compaction()
        assert stats.context_pct == 0.0 and stats.context_pct_unknown is True
        stats.reset_context_state()
        assert not (stats.context_pct == 0.0 and stats.context_pct_unknown)
