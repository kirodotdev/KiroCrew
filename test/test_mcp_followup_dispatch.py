"""Direct coverage for the ``suggest_followup`` MCP dispatch branch.

The endpoint, the arg schema and the frontend reducers each have their own
suites, but nothing exercised the dispatch adapter in
``_call_tool_inner`` — the layer that gates on strict dashboard identity,
derives the slot path, and turns the gateway's ``delivered`` count into the
sentence the model reads (GPT review, PR #461 round 10). ``_post`` is mocked, so
the HTTP layer is out of scope here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from kiro_crew import mcp_core


def _item(**over: object) -> dict:
    base = {
        "title": "Add rate limiting",
        "description": "The upload endpoint is unbounded.",
        "prompt": "Add a token-bucket limiter to POST /api/upload.",
    }
    base.update(over)
    return base


class TestSuggestFollowupDispatch:
    def _invoke(
        self,
        args: dict,
        *,
        session_key: str = "dashboard:test-slot",
        response: dict | None = None,
    ) -> str:
        captured: dict = {"calls": []}
        self._captured = captured

        def fake_post(path: str, body: dict | None = None) -> dict:
            captured["calls"].append((path, body))
            return response if response is not None else {"ok": True, "delivered": 1, "count": 1}

        with patch.object(
            mcp_core, "_resolve_session_key_strict", return_value=session_key
        ), patch.object(mcp_core, "_post", side_effect=fake_post):
            result = mcp_core._call_tool_inner("suggest_followup", args)
        self._captured = captured
        return result

    def test_posts_to_the_slot_derived_from_the_session_key(self):
        result = self._invoke({"items": [_item()]})
        assert len(self._captured["calls"]) == 1
        url, body = self._captured["calls"][0]
        assert url == "/api/chat/slots/test-slot/followup"
        assert body is not None and body["items"][0]["title"] == "Add rate limiting"
        assert "error" not in result.lower()

    @pytest.mark.parametrize(
        "session_key",
        ["slack:C123", "cron:nightly", "subagent:ag-1", ""],
    )
    def test_non_dashboard_sessions_are_refused_without_posting(self, session_key):
        """Slack/cron/subagent contexts have no card surface, and an unresolved
        identity must not be allowed to guess a slot."""
        result = self._invoke({"items": [_item()]}, session_key=session_key)
        assert result.startswith("Error:")
        assert "dashboard sessions" in result
        assert self._captured["calls"] == []

    def test_schema_violation_is_refused_at_the_dispatch_layer(self):
        """The tool re-validates before posting — the endpoint is not the only gate.

        ``_call_tool_inner`` RAISES ``ValidationError`` (the outer ``_call_tool``
        wrapper renders it for the model); what matters here is that nothing was
        posted.
        """
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            self._invoke({"items": [_item(branch="-rf")]})
        assert self._captured["calls"] == []

    def test_endpoint_error_is_surfaced_to_the_model(self):
        result = self._invoke({"items": [_item()]}, response={"error": "not found"})
        assert result == "Error: not found"

    def test_zero_delivered_is_reported_rather_than_a_bare_success(self):
        """With no listening client the card was dropped; the model must be told so
        it restates the follow-ups in its reply instead of assuming they showed."""
        result = self._invoke({"items": [_item()]}, response={"ok": True, "delivered": 0})
        assert "0" in result or "no" in result.lower()
