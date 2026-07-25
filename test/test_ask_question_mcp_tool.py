"""HTTP-level tests for the ``ask_question`` MCP tool dispatch.

Exercises the real ``_call_tool_inner`` branch against a mock dashboard that
speaks the same user-token contract as production, so the test covers session
resolution, request body shape, the socket-timeout margin, and how each of the
three outcomes (answered / timeout / error) is rendered for the model.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew.mcp_core import _call_tool_inner

QUESTIONS = [
    {
        "question": "Which approach?",
        "header": "SCOPE",
        "options": [{"label": "Option A"}, {"label": "Option B"}],
    }
]


class _MockAskHandler(BaseHTTPRequestHandler):
    """Mock dashboard for POST /api/ask-question."""

    secret = "local-secret-xyz"
    issued_token = "user-token-abc"
    # Set per-test: the JSON body the ask endpoint responds with.
    response: dict = {"status": "answered", "answers": {"Which approach?": "Option B"}}
    status_code: int = 200
    received: list[dict] = []

    def _json(self, status: int, body: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _has_valid_token(self) -> bool:
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        return f"token={self.issued_token}" in query

    def do_GET(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/api/token/local":
            if self.headers.get("X-Local-Secret") != self.secret:
                self._json(403, {"error": "invalid secret"})
                return
            self._json(200, {"token": self.issued_token, "expires_in": 900})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        if self.path.split("?", 1)[0] == "/api/ask-question":
            if not self._has_valid_token():
                self._json(403, {"error": "Token required"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            type(self).received.append(json.loads(self.rfile.read(length) or b"{}"))
            self._json(type(self).status_code, type(self).response)
            return
        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # noqa: A002
        pass


@pytest.fixture()
def mock_dashboard(tmp_path, monkeypatch):
    server = HTTPServer(("127.0.0.1", 0), _MockAskHandler)
    port = server.server_address[1]
    Thread(target=server.serve_forever, daemon=True).start()

    (tmp_path / ".local_secret").write_text(_MockAskHandler.secret)
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-3-1700000000")
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
    _MockAskHandler.received = []
    _MockAskHandler.status_code = 200
    _MockAskHandler.response = {
        "status": "answered",
        "answers": {"Which approach?": "Option B"},
    }
    yield port
    server.shutdown()


def test_answered_question_is_returned_to_the_model(mock_dashboard):
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "The user answered:" in result
    assert "Which approach?: Option B" in result


def test_request_body_carries_full_session_key_and_questions(mock_dashboard):
    _call_tool_inner("ask_question", {"questions": QUESTIONS, "timeout_secs": 120})
    assert len(_MockAskHandler.received) == 1
    body = _MockAskHandler.received[0]
    # Unlike monitor_start (which needs the BARE slot key for autonudge), the
    # ask endpoint takes the namespaced session key and derives the slot itself.
    assert body["session_key"] == "dashboard:chat-3-1700000000"
    assert body["questions"] == QUESTIONS
    assert body["timeout_secs"] == 120


def test_timeout_response_tells_the_model_not_to_re_ask(mock_dashboard):
    _MockAskHandler.response = {"status": "timeout"}
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS, "timeout_secs": 30})
    assert "No answer within 30s" in result
    # The instruction matters: an auto-retry loop would spam the user's chat.
    assert "Do NOT re-ask automatically" in result


def test_multi_question_answers_are_all_rendered(mock_dashboard):
    _MockAskHandler.response = {
        "status": "answered",
        "answers": {"Which approach?": "Option A", "Which account?": "prod"},
    }
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "Which approach?: Option A" in result
    assert "Which account?: prod" in result


def test_error_response_is_surfaced_not_swallowed(mock_dashboard):
    _MockAskHandler.status_code = 404
    _MockAskHandler.response = {"error": "unknown slot 'chat-9'"}
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "Failed to ask the question" in result
    assert "chat-9" in result


def test_non_dashboard_session_is_refused_with_options_hint(mock_dashboard, monkeypatch):
    """Slack/Discord/cron have no question card — steer to the [OPTIONS:] tag."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1700000000.123456")
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "only works from a dashboard chat session" in result
    assert "[OPTIONS:" in result
    # Must not have hit the endpoint at all.
    assert _MockAskHandler.received == []


def test_subagent_without_session_key_is_refused(mock_dashboard, monkeypatch):
    """Strict resolution: no env key means no card in someone else's chat."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
    result = _call_tool_inner("ask_question", {"questions": QUESTIONS})
    assert "only works from a dashboard chat session" in result
    assert _MockAskHandler.received == []


def test_socket_timeout_exceeds_server_window(mock_dashboard, monkeypatch):
    """The HTTP read must outlive the server-side wait, or it trips first."""
    seen: dict[str, int] = {}
    real_post = mcp_core._post_user

    def spy(path: str, body: dict, timeout: int = 10):
        seen["timeout"] = timeout
        return real_post(path, body, timeout=timeout)

    monkeypatch.setattr(mcp_core, "_post_user", spy)
    _call_tool_inner("ask_question", {"questions": QUESTIONS, "timeout_secs": 300})
    assert seen["timeout"] > 300


def test_questions_is_required(mock_dashboard):
    # _call_tool is the agent-facing entrypoint: it runs schema validation and
    # converts a ValidationError into a message, rather than letting it escape
    # the stdio loop (which would kill the whole MCP server for the session).
    result = mcp_core._call_tool("ask_question", {})
    assert "questions" in result.lower()
    assert _MockAskHandler.received == []


def test_inner_dispatch_raises_for_missing_questions(mock_dashboard):
    """The inner branch itself does not swallow the schema error."""
    from kiro_crew.validation import ValidationError

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
