"""The gateway surfaces a consolidation span abandoned at its attempt cap.

The consolidator marks such a span consolidated so it stops re-billing a turn,
which also removes it from every "pending" listing. The bell note is the only
user-visible record that its history, preferences and lessons were dropped.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_gw(state: object) -> object:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.dashboard_state = state  # type: ignore[attr-defined]
    return gw


def test_an_abandoned_span_posts_a_bell_note():
    state = MagicMock()
    gw = _make_gw(state)

    gw._notify_consolidation_abandoned("dashboard:chat-1", 3100, "empty LLM result")

    state.notify.assert_called_once()
    args, kwargs = state.notify.call_args
    kind, title, body = args
    assert kind == "agent"
    assert "gave up" in title
    assert "3100 messages" in body
    assert "dashboard:chat-1" in body
    assert "empty LLM result" in body
    # System-originated (the consolidator gave up, no agent produced this note), so
    # the bridge attributes it by the system tag and still asks the session's profile.
    assert kwargs["meta"] == {
        "producer_system": "1",
        "session_key": "dashboard:chat-1",
        "kind": "consolidation-abandoned",
    }


def test_no_dashboard_means_no_note_and_no_error():
    gw = _make_gw(None)

    gw._notify_consolidation_abandoned("dashboard:chat-1", 10, "exception after the LLM call")
