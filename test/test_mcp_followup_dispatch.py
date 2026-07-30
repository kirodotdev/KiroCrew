"""Direct coverage for the stateless ``suggest_followup`` MCP path (#755).

The dispatch branch in ``_call_tool_inner`` no longer resolves session
identity, no longer refuses non-dashboard sessions, and no longer POSTs to the
gateway. It VALIDATES its items and returns a session DIRECTIVE (see
``kiro_crew.session_directive``). The session-aware consumer renders the card
against ITS OWN slot via
``kiro_crew.dashboard.session_directive_apply.apply_session_directive`` — that
applier is exercised directly here against a fake state whose
``deliver_ws_owners`` returns a delivered-client count.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard.session_directive_apply import apply_session_directive


def _item(**over: object) -> dict:
    base = {
        "title": "Add rate limiting",
        "description": "The upload endpoint is unbounded.",
        "prompt": "Add a token-bucket limiter to POST /api/upload.",
    }
    base.update(over)
    return base


class TestSuggestFollowupDispatch:
    """The tool validates and returns a directive; nothing is posted here."""

    def test_returns_directive_with_items(self):
        items = [_item()]
        result = mcp_core._call_tool_inner("suggest_followup", {"items": items})
        # Validation sanitizes items in place, so the encoded payload equals the
        # (possibly-cleaned) items list we passed in.
        assert session_directive.decode(result, "suggest_followup") == {"items": items}

    def test_schema_violation_is_refused_at_the_dispatch_layer(self):
        """The tool re-validates before producing a directive — a bad branch is
        rejected with ``ValidationError`` and no directive is returned."""
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("suggest_followup", {"items": [_item(branch="-rf")]})


class TestSuggestFollowupApplier:
    """The consumer delivers the card to its own owner channel."""

    @pytest.mark.asyncio
    async def test_delivers_followup_card_to_owner_channel(self):
        calls: list = []

        async def deliver_ws_owners(event, payload):
            calls.append((event, payload))
            return 1

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:chat-1")
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "suggest_followup", {"items": [_item()]}
        )
        assert len(calls) == 1
        event, payload = calls[0]
        assert event == "followup_card"
        assert payload["slot"] == "dashboard:chat-1"
        assert payload["items"] and payload["items"][0]["title"] == "Add rate limiting"
        assert "error" not in result.lower()

    @pytest.mark.asyncio
    async def test_zero_delivered_tells_model_to_restate(self):
        """With no listening client the card was dropped; the confirmation must
        tell the model to restate the follow-ups in its reply."""

        async def deliver_ws_owners(event, payload):
            return 0

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:chat-1")
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "suggest_followup", {"items": [_item()]}
        )
        assert "restate" in result.lower()
