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
from kiro_crew.session_surface import has_dashboard_surface


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
            state,
            slot,
            "dashboard:chat-1",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=True,
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
            state,
            slot,
            "dashboard:chat-1",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=True,
        )
        assert "restate" in result.lower()

    @pytest.mark.asyncio
    async def test_unscoped_slot_warns_that_the_worktree_button_is_disabled(self):
        """A slot with no project directory renders the card's 'Start in new
        worktree' button DISABLED (FollowUpCard.tsx gates on projectDir). The
        confirmation is the model's only window into that, so it must say so —
        otherwise the agent recommends a route that cannot work (Research Lab
        worker slots are always unscoped: auto_research/handlers.py)."""

        async def deliver_ws_owners(event, payload):
            return 1

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:research-1", project="")
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:research-1",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=True,
        )
        assert "no project directory" in result.lower()
        assert "add to this session" in result.lower()
        # Still a success, not an error: the card WAS shown.
        assert "error" not in result.lower()

    @pytest.mark.asyncio
    async def test_scoped_slot_gets_the_plain_confirmation(self):
        """With a project directory every card action works — the confirmation
        must not carry the disabled-button note, which would steer the model
        away from the worktree route for no reason."""

        async def deliver_ws_owners(event, payload):
            return 1

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:chat-1", project="/repo")
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:chat-1",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=True,
        )
        assert "no project directory" not in result.lower()
        assert "shown below the composer" in result.lower()


# ──────────────── provenance: a tab is not an entitlement ────────────────────


class TestHeadlessProducersAreRefused:
    """The tool's own description promises that "Slack, cron, and subagent
    contexts are rejected", but the dashboard-only gate it sits behind asks only
    whether an open tab EXISTS. A cron turn can run on a user's slot and a
    sub-agent can share its parent's slot, so both inherit a tab they never
    opened and clear that gate without a human having asked for a card.
    """

    def test_the_surface_gate_cannot_tell_a_headless_turn_apart(self):
        """The premise, pinned so the test below cannot be misread as belt on
        braces: the surface gate admits ANY ``dashboard:``-prefixed key, so a
        refusal can only come from provenance.
        """
        assert has_dashboard_surface("dashboard:ridden-by-cron") is True

    @pytest.mark.asyncio
    async def test_a_headless_producer_is_refused_with_a_tab_open(self):
        """The case a tabless test cannot reach: the session HAS a surface, so
        the dashboard-only gate admits it and only provenance can refuse. No
        card reaches the owner channel.
        """
        calls: list = []

        async def deliver_ws_owners(event, payload):
            calls.append((event, payload))
            return 1

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:ridden-by-cron", project="/repo")
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:ridden-by-cron",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=False,
        )
        assert "headless" in result
        assert "Nothing was changed" in result
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_human_turn_on_the_same_slot_is_admitted(self):
        """Negative control for the test above: the refusal must be caused by the
        producer's provenance, not by anything else about this slot or key.
        """
        calls: list = []

        async def deliver_ws_owners(event, payload):
            calls.append((event, payload))
            return 1

        state = SimpleNamespace(deliver_ws_owners=deliver_ws_owners)
        slot = SimpleNamespace(key="dashboard:ridden-by-cron", project="/repo")
        result = await apply_session_directive(
            state,
            slot,
            "dashboard:ridden-by-cron",
            "suggest_followup",
            {"items": [_item()]},
            producer_is_user_facing=True,
        )
        assert len(calls) == 1
        assert "error" not in result.lower()
