"""The fake ACP backend's delegation directives (the GUI test's stand-in for an
agent deciding to delegate). The bridge is exercised against a stub HTTP server
standing in for the gateway, so these tests need no gateway, no kiro-cli and no
network beyond loopback."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from kiro_crew.testing import fake_acp_backend as fab

_H = "[CURRENT USER REQUEST — respond to this]\n"


class TestParse:
    def test_no_directive_leaves_text_untouched(self):
        assert fab.parse_directives(_H + "plain [[SLOW]] text") == ([], "plain [[SLOW]] text")

    def test_single_spawn_strips_only_the_directive(self):
        d, rest = fab.parse_directives(_H + "[[SPAWN:remote-demo]] [[SLOW]] Write twenty facts.")
        assert d == [("SPAWN", "remote-demo")]
        # The subagent's own sentinel survives inside the task text.
        assert rest == "[[SLOW]] Write twenty facts."

    def test_two_spawns_in_one_message(self):
        d, rest = fab.parse_directives(
            _H
            + "[[SPAWN:remote-demo]] Reply with only the word A [[SPAWN:local]] Reply with only the word B"
        )
        assert d == [("SPAWN", "remote-demo"), ("SPAWN", "local")]
        assert "[[" not in rest

    def test_other_verbs(self):
        for verb in ("CONTINUE", "STEER", "FOLLOWUP"):
            d, _ = fab.parse_directives(_H + f"[[{verb}:remote-demo]] x")
            assert d == [(verb, "remote-demo")]

    def test_rejects_shouty_or_unsafe_agent_names(self):
        assert fab.parse_directives(_H + "[[SPAWN:Remote Demo]] x")[0] == []
        assert fab.parse_directives(_H + "[[SPAWN:../evil]] x")[0] == []

    def test_only_the_current_user_request_is_read(self):
        """A housekeeping prompt quoting the transcript must not re-trigger."""
        quoted = (
            "You are a session naming agent. Name ONLY the conversation below.\n"
            "user: [[SPAWN:remote-demo]] Reply with only the word OSPREY-3\n"
            "assistant: Delegated to remote-demo (run abc).\n"
        )
        # No user-request header: this is a background prompt, nothing fires.
        assert fab.parse_directives(quoted)[0] == []
        # With the header present, only what follows it counts.
        framed = (
            "[SESSION CONTEXT]\nuser earlier said [[SPAWN:remote-demo]] something\n"
            "[END OF SESSION CONTEXT]\n\n[CURRENT USER REQUEST — respond to this]\n"
            "[[CONTINUE:remote-demo]] What was the codeword?"
        )
        d, rest = fab.parse_directives(framed)
        assert d == [("CONTINUE", "remote-demo")]
        assert rest == "What was the codeword?"
        # Header present, directive only in the quoted context: nothing fires.
        framed_quiet = framed.replace("[[CONTINUE:remote-demo]] ", "")
        assert fab.parse_directives(framed_quiet)[0] == []


class _StubGateway(BaseHTTPRequestHandler):
    calls: list[tuple[str, dict, dict]] = []
    responses: dict[str, tuple[int, dict]] = {}

    def do_POST(self):  # noqa: N802 - http.server API
        n = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).calls.append((self.path, body, dict(self.headers)))
        status, payload = type(self).responses.get(
            self.path, (200, {"id": "run-1", "status": "spawned"})
        )
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *_a):  # silence
        return


@pytest.fixture
def stub_gateway(tmp_path, monkeypatch):
    srv = HTTPServer(("127.0.0.1", 0), _StubGateway)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    _StubGateway.calls = []
    _StubGateway.responses = {}
    # The credential file the gateway would have written for this port.
    run = tmp_path / "run"
    run.mkdir()
    (run / f"gateway-{port}.secret").write_text("s3cret\n", encoding="utf-8")
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setenv("KIROCREW_BOUND_PORT", str(port))
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:test-slot")
    monkeypatch.setenv(fab.DELEGATION_BRIDGE_ENV, "1")
    fab._LAST_RUN_BY_AGENT.clear()
    try:
        yield port
    finally:
        srv.shutdown()
        srv.server_close()


class TestBridge:
    def test_disabled_by_default_says_so(self, monkeypatch):
        monkeypatch.delenv(fab.DELEGATION_BRIDGE_ENV, raising=False)
        out = fab._run_directives([("SPAWN", "remote-demo")], "task")
        assert "delegation bridge disabled" in out

    def test_spawn_posts_like_the_mcp_tool(self, stub_gateway):
        out = fab._run_directives([("SPAWN", "remote-demo")], "Reply with only the word OSPREY-3")
        assert "Delegated to remote-demo (run run-1)" in out
        path, body, headers = _StubGateway.calls[0]
        assert path == "/api/spawn"
        assert (
            body["agent"] == "remote-demo" and body["task"] == "Reply with only the word OSPREY-3"
        )
        assert body["parent_session"] == "dashboard:test-slot" and body["keep"] is True
        assert "batch_id" not in body
        assert headers.get("X-Internal-Secret") == "s3cret"

    def test_local_alias_means_default_agent(self, stub_gateway):
        fab._run_directives([("SPAWN", "local")], "hi")
        assert _StubGateway.calls[0][1]["agent"] == ""

    def test_two_spawns_form_one_batch(self, stub_gateway):
        fab._run_directives([("SPAWN", "remote-demo"), ("SPAWN", "local")], "x")
        b1, b2 = _StubGateway.calls[0][1], _StubGateway.calls[1][1]
        assert b1["batch_id"] == b2["batch_id"] and b1["batch_total"] == 2 == b2["batch_total"]

    def test_continue_targets_the_last_run_for_that_agent(self, stub_gateway):
        fab._run_directives([("SPAWN", "remote-demo")], "Remember the codeword HERON-9")
        _StubGateway.responses["/api/spawn/run-1/continue"] = (
            200,
            {"id": "run-2", "conversation": "run-1"},
        )
        out = fab._run_directives([("CONTINUE", "remote-demo")], "What was the codeword?")
        assert "Continued the remote-demo conversation (run run-2)" in out
        path, body, _ = _StubGateway.calls[-1]
        assert path == "/api/spawn/run-1/continue" and body["task"] == "What was the codeword?"

    def test_continue_without_prior_run_is_explained(self, stub_gateway):
        out = fab._run_directives([("CONTINUE", "remote-demo")], "x")
        assert "Nothing to continue" in out and not _StubGateway.calls

    def test_interrupt_refusal_is_shown_verbatim(self, stub_gateway):
        fab._run_directives([("SPAWN", "remote-demo")], "[[SLOW]] facts")
        _StubGateway.responses["/api/spawn/run-1/steer"] = (
            409,
            {
                "error": "steer_unsupported: this run's backend does not support mid-turn interrupt",
                "code": "steer_unsupported",
            },
        )
        out = fab._run_directives([("STEER", "remote-demo")], "Stop")
        assert "steer_unsupported" in out and "does not support mid-turn interrupt" in out
        assert _StubGateway.calls[-1][1] == {"message": "Stop", "mode": "interrupt"}

    def test_followup_uses_follow_up_mode(self, stub_gateway):
        fab._run_directives([("SPAWN", "remote-demo")], "x")
        out = fab._run_directives([("FOLLOWUP", "remote-demo")], "Now say STEERED-OK")
        assert "Sent a follow-up to remote-demo" in out
        assert _StubGateway.calls[-1][1]["mode"] == "follow_up"

    def test_credential_falls_back_to_home_wide_file(self, stub_gateway, tmp_path):
        (tmp_path / "run" / f"gateway-{stub_gateway}.secret").unlink()
        (tmp_path / ".local_secret").write_text("wide\n", encoding="utf-8")
        fab._run_directives([("SPAWN", "remote-demo")], "x")
        assert _StubGateway.calls[0][2].get("X-Internal-Secret") == "wide"
