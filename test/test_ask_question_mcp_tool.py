"""Contract tests for the MCP ``ask_question`` tool's advertised surface.

``ask_question`` is ALWAYS blocking: with an attested dashboard session it opens
a card on that session's own slot and returns the answers as its tool result
(the blocking path is covered in ``test_ask_question_blocking.py``). It is NOT a
session directive. Without an attested dashboard session it refuses in plain
text -- no directive, no out-of-band post -- so an answer can never come back as
a user-role message. These tests pin the schema, the description and those
refusals.
"""

from __future__ import annotations

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import session_directive
from kiro_crew.mcp_core import _call_tool, _call_tool_inner
from kiro_crew.validation import ValidationError

QUESTIONS = [
    {
        "question": "Which approach?",
        "header": "SCOPE",
        "options": [{"label": "Option A"}, {"label": "Option B"}],
    }
]


@pytest.fixture()
def default_install(monkeypatch):
    """Default install: the strict resolver has no accepted identity source and
    returns ``""``, so there is no session to block on and ask_question refuses.
    (Pooling off, unsandboxed, kiro-cli backend.)"""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    return monkeypatch


# ── Tool contract ─────────────────────────────────────────────────────────────


def test_unattested_caller_gets_a_plain_refusal_not_a_directive(default_install, gateway_posts):
    """With no attested session there is no card to block on. The tool says so in
    its result -- it publishes no directive and posts nothing, so no answer can
    ever come back as a user message."""
    result = _call_tool("ask_question", {"questions": QUESTIONS})
    assert session_directive.decode(result, "ask_question") is None
    assert "only works from a dashboard chat session" in result
    assert "[OPTIONS:" in result
    assert gateway_posts == []


def test_non_dashboard_session_is_refused_with_options_hint(monkeypatch, gateway_posts):
    """A non-empty, non-dashboard key (Slack/Discord) has no question card, so
    the tool steers to the [OPTIONS:] tag and emits NO directive."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "slack:C1")
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "only works from a dashboard chat session" in result
    assert "[OPTIONS:" in result
    # It is a plain message, not a directive.
    assert session_directive.decode(result, "ask_question") is None
    # A refusal must not publish: no marker, no parked record.
    assert gateway_posts == []


def test_surfaced_channel_session_is_refused(monkeypatch, gateway_posts):
    """A channel session with an open dashboard tab is still refused: its agent answers to the channel."""
    import kiro_crew.mcp_tools.control as control

    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "slack:C1")
    monkeypatch.setattr(control, "has_dashboard_surface", lambda sk: True)
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "only works from a dashboard chat session" in result
    assert gateway_posts == []


def test_questions_is_required(default_install):
    # _call_tool is the agent-facing entrypoint: it runs schema validation and
    # converts a ValidationError into a message, rather than letting it escape
    # the stdio loop (which would kill the whole MCP server for the session).
    result = _call_tool("ask_question", {})
    assert "questions" in result.lower()


def test_inner_dispatch_raises_for_missing_questions(default_install):
    """The inner branch itself does not swallow the schema error."""
    with pytest.raises(ValidationError):
        _call_tool_inner("ask_question", {})


def test_ask_question_is_advertised_in_the_tool_list():
    names = {t["name"] for t in mcp_core._list_tools()}
    assert "ask_question" in names
    spec = next(t for t in mcp_core._list_tools() if t["name"] == "ask_question")
    assert spec["inputSchema"]["required"] == ["questions"]
    # The description must steer away from using this when ending a turn,
    # otherwise it displaces the cheaper [OPTIONS:] tag everywhere.
    assert "[OPTIONS:" in spec["description"]


def test_advertised_description_states_the_blocking_contract():
    """The description is what an agent reads BEFORE its first call, so it has to
    match the tool. On the dashboard the call blocks and the answers are its
    RESULT, so the agent must continue rather than end its turn; the directive
    fallback (no attached dashboard) is the one case that still ends the turn,
    and the result says so when it happens.
    """
    spec = next(t for t in mcp_core._list_tools() if t["name"] == "ask_question")
    lowered = spec["description"].lower()
    assert "blocks until the user responds" in lowered
    assert "tool's result" in lowered
    assert "do not end your turn" in lowered
    assert "replied in chat" in lowered
    # The old non-blocking instruction would now strand the agent: it would end
    # the turn while the card it just posted is still waiting on this very call.
    assert "non-blocking" not in lowered
    assert "next ordinary message" not in lowered


def test_timeout_secs_is_not_advertised_but_is_still_accepted(default_install):
    """`timeout_secs` cannot do anything here: the directive carries only the
    questions, so nothing downstream reads a deadline. It is therefore absent
    from the advertised schema — a knob with no effect should not be offered to a
    model — while remaining a lenient field so a caller that still passes it gets
    its card rather than a validation error.
    """
    spec = next(t for t in mcp_core._list_tools() if t["name"] == "ask_question")
    assert "timeout_secs" not in spec["inputSchema"]["properties"]
    assert "timeout_secs" not in spec["description"]

    result = _call_tool_inner("ask_question", {"questions": QUESTIONS, "timeout_secs": 60})
    assert not result.startswith("Error")


# ── Not a directive ────────────────────────────────────────────────────────────


def test_ask_question_is_not_a_session_directive():
    """ask_question blocks and returns its answers as the tool result. Registered
    as a directive, its result was tagged as a refused directive and the applier
    would post a non-blocking card whose answer arrives as a user message."""
    assert "ask_question" not in session_directive.DIRECTIVE_TOOLS
