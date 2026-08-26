"""Contract tests for the stateless ``ask_question`` MCP tool (issue #755).

``ask_question`` no longer resolves a user-scoped token or blocks on a
server-side HTTP round-trip. It VALIDATES its arguments and returns a session
DIRECTIVE — a human confirmation plus an opaque marker carrying the validated
questions (and NO session key). The session-aware consumer
(``dashboard.chat_runner._run_chat``'s ``EVENT_TOOL_RESULT`` handler) decodes the
marker and, via ``dashboard.session_directive_apply.apply_session_directive``,
posts a NON-BLOCKING question card to ITS OWN slot; the agent then ends its turn
and the user's answer arrives as an ordinary next message.

The tests split along that seam:

* **Tool contract** — call the tool via ``_call_tool_inner`` and assert the
  returned directive decodes to the validated questions on a default install,
  and that a non-dashboard session gets a plain ``[OPTIONS:]`` steer (NO
  directive).
* **Applier** — call ``apply_session_directive(..., "ask_question", ...)`` with
  a fake state exposing ``post_question_card`` and assert the card is posted to
  the slot's key, that the confirmation tells the model to END its turn, and
  that a no-client post steers the model to ask in plain text instead.

The former mock-dashboard HTTP server, the ``_post_user`` user-token handshake,
the socket-timeout margin, and the answered/timeout/error round-trip tests are
gone: that logic no longer exists — the tool is stateless and the card is posted
in-process by the applier.
"""

from __future__ import annotations

import asyncio

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew import session_directive
from kiro_crew.dashboard.session_directive_apply import apply_session_directive
from kiro_crew.mcp_core import _call_tool, _call_tool_inner
from kiro_crew.validation import ValidationError, validate_ask_user_question

QUESTIONS = [
    {
        "question": "Which approach?",
        "header": "SCOPE",
        "options": [{"label": "Option A"}, {"label": "Option B"}],
    }
]


def _validated_questions() -> list:
    """The questions exactly as the tool validates them — the authoritative deep
    AskUserQuestion validation/normalization, which is what the directive and
    the applier now carry (not the shallow schema)."""
    return validate_ask_user_question({"questions": QUESTIONS})


@pytest.fixture()
def default_install(monkeypatch):
    """Default install: the strict resolver has no accepted identity source and
    returns ``""``, so ask_question RETURNS a directive rather than refusing.
    (Pooling off, unsandboxed, kiro-cli backend.)"""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "")
    return monkeypatch


# ── Tool contract ─────────────────────────────────────────────────────────────


def test_ask_question_returns_directive_with_validated_questions(default_install):
    """A valid call on a default install returns a directive that decodes to the
    validated questions payload — no session key, no HTTP round-trip."""
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    args = session_directive.decode(result, "ask_question")
    assert args == {"questions": _validated_questions()}


def test_ask_question_directive_confirmation_tells_the_model_to_end_its_turn(default_install):
    """The human-visible confirmation must instruct the model to end its turn so
    the non-blocking card can be answered as the next message."""
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "end your turn" in result.lower()


def test_non_dashboard_session_is_refused_with_options_hint(monkeypatch):
    """A non-empty, non-dashboard key (Slack/Discord) has no question card, so
    the tool steers to the [OPTIONS:] tag and emits NO directive."""
    monkeypatch.setattr(mcp_core, "_resolve_session_key_strict", lambda: "slack:C1")
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "only works from a dashboard chat session" in result
    assert "[OPTIONS:" in result
    # It is a plain message, not a directive.
    assert session_directive.decode(result, "ask_question") is None


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


# ── Applier (dashboard.session_directive_apply) ───────────────────────────────


class _FakeSlot:
    key = "chat-1-1700000000"


class _FakeState:
    """Fake dashboard state exposing only ``post_question_card``.

    ``post_question_card(slot_key, questions)`` returns the number of clients the
    card reached (mirrors the real broadcast-count contract)."""

    def __init__(self, clients: int):
        self._clients = clients
        self.calls: list[tuple[str, list]] = []

    async def post_question_card(self, slot_key, questions) -> int:
        self.calls.append((slot_key, questions))
        return self._clients


def test_applier_posts_the_card_to_the_slot_and_ends_the_turn():
    """The card is posted to slot.key with the validated questions, and the
    confirmation tells the model to END ITS TURN."""
    state = _FakeState(clients=1)
    slot = _FakeSlot()
    questions = _validated_questions()
    result = asyncio.run(
        apply_session_directive(
            state, slot, "dashboard:chat-1-1700000000", "ask_question", {"questions": questions}
        )
    )
    assert state.calls == [(slot.key, questions)]
    assert "end your turn" in result.lower()


def test_applier_with_no_attached_client_steers_to_plain_text():
    """When post_question_card returns 0 (no dashboard client attached), the
    confirmation tells the model to ask in plain text instead."""
    state = _FakeState(clients=0)
    slot = _FakeSlot()
    result = asyncio.run(
        apply_session_directive(
            state, slot, "dashboard:chat-1-1700000000", "ask_question",
            {"questions": _validated_questions()},
        )
    )
    assert len(state.calls) == 1
    assert "plain text" in result.lower()
