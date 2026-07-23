"""End-to-end auth tests for the ``autonudge_stop`` MCP tool.

These tests exercise the full tool -> HTTP -> auth chain rather than calling
the AutoNudgeService directly. They stand up a mock dashboard server that
emulates the REAL auth contract of the ``/api/autonudge*`` routes:

* the routes REJECT the machine-to-machine ``X-Internal-Secret`` header
  (returning 403), exactly as the production auth middleware does, and
* they only accept a user-scoped token minted by ``GET /api/token/local``
  (which itself requires the loopback ``X-Local-Secret`` header).

The original latent bug was that ``autonudge_stop`` called these routes with
``X-Internal-Secret`` (via the plain ``_get``/``_delete`` helpers) and so
always got ``403 Forbidden`` on the loop lookup. A test that drove the
AutoNudgeService directly could never have caught it because it bypasses the
HTTP + auth layer entirely — hence this server-backed test.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

import pytest

import kiro_crew.mcp_core as mcp_core
from kiro_crew.mcp_core import _call_tool_inner


class _MockAutonudgeHandler(BaseHTTPRequestHandler):
    """Mock dashboard emulating the user-token contract of /api/autonudge*.

    Mirrors the production auth middleware: X-Internal-Secret is NOT honored
    for these routes; only a ``?token=`` query param obtained from
    /api/token/local (loopback + X-Local-Secret) is accepted.
    """

    secret = "local-secret-xyz"
    issued_token = "user-token-abc"
    # Set per-test: the loop returned by GET /api/autonudge/slot/{slot_key}.
    loop_for_slot: dict | None = {"id": "loop-1", "slot_key": "chat-3-1700000000"}
    # Bodies received by POST /api/autonudge (monitor_start tests).
    created_bodies: list[dict] = []

    def _json(self, status: int, body: dict) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def _reject_internal_secret(self) -> bool:
        """True (and already responded 403) if the caller used the internal secret.

        The production middleware falls through to cookie/token auth for these
        routes; an X-Internal-Secret-only request never satisfies it -> 403.
        """
        if self.headers.get("X-Internal-Secret") is not None and not self._has_valid_token():
            self._json(403, {"error": "Token required"})
            return True
        return False

    def _has_valid_token(self) -> bool:
        # Naive query parse is enough for the mock.
        return f"token={self.issued_token}" in (self.path.split("?", 1)[1] if "?" in self.path else "")

    def do_GET(self):  # noqa: N802
        base = self.path.split("?", 1)[0]
        if base == "/api/token/local":
            # Loopback + X-Local-Secret mints a user token.
            if self.headers.get("X-Local-Secret") != self.secret:
                self._json(403, {"error": "invalid secret"})
                return
            self._json(200, {"token": self.issued_token, "expires_in": 900})
            return
        if base.startswith("/api/autonudge/slot/"):
            if self._reject_internal_secret():
                return
            if not self._has_valid_token():
                self._json(403, {"error": "Token required"})
                return
            self._json(200, {"loop": self.loop_for_slot})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):  # noqa: N802
        base = self.path.split("?", 1)[0]
        if base.startswith("/api/autonudge/"):
            if self._reject_internal_secret():
                return
            if not self._has_valid_token():
                self._json(403, {"error": "Token required"})
                return
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        base = self.path.split("?", 1)[0]
        if base == "/api/autonudge":
            if self._reject_internal_secret():
                return
            if not self._has_valid_token():
                self._json(403, {"error": "Token required"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            type(self).created_bodies.append(body)
            key = body.get("session_key") or body.get("slot_key") or ""
            self._json(
                200,
                {
                    "ok": True,
                    "loop": {
                        "id": "loop-new",
                        "slot_key": key,
                        "message": body.get("message", ""),
                        "idle_secs": body.get("idle_secs", 60),
                        "max_cycles": body.get("max_cycles", 0),
                    },
                },
            )
            return
        self._json(404, {"error": "not found"})

    def log_message(self, *args):  # noqa: A002
        pass


@pytest.fixture()
def mock_dashboard(tmp_path, monkeypatch):
    """Start the mock dashboard and point the MCP tool's HTTP layer at it."""
    server = HTTPServer(("127.0.0.1", 0), _MockAutonudgeHandler)
    port = server.server_address[1]
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # The tool reads the local secret from config_dir()/.local_secret.
    (tmp_path / ".local_secret").write_text(_MockAutonudgeHandler.secret)
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    # Point the tool's API base at the mock server, and give it a dashboard
    # session key so autonudge_stop proceeds past its "dashboard-only" guard.
    monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-3-1700000000")
    # Reset the in-process user-token cache so each test bootstraps fresh.
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))

    yield port
    server.shutdown()


def test_autonudge_stop_succeeds_via_user_token(mock_dashboard):
    """The tool bootstraps a user token and stops the loop — no 403."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-1",
        "slot_key": "chat-3-1700000000",
    }
    result = _call_tool_inner("autonudge_stop", {"reason": "test"})
    assert "stopped" in result.lower()
    assert "loop-1" in result
    assert "403" not in result
    assert "Failed to look up loop" not in result


def test_autonudge_stop_no_loop_is_clean_noop(mock_dashboard):
    """When no loop is bound, the tool reports nothing-to-stop (still no 403)."""
    _MockAutonudgeHandler.loop_for_slot = None
    result = _call_tool_inner("autonudge_stop", {"reason": "test"})
    assert "no active auto-nudge loop" in result.lower()
    assert "403" not in result


def test_internal_secret_handshake_would_403(mock_dashboard):
    """Guard: the OLD handshake (X-Internal-Secret only) is rejected by the route.

    This pins the contract the bug violated — if someone reverts the tool to
    the plain ``_get`` helper, this proves the route still 403s it.
    """
    # The plain helper sends X-Internal-Secret and no token -> 403.
    resp = mcp_core._get("/api/autonudge/slot/chat-3-1700000000")
    assert "error" in resp
    assert "Token required" in resp["error"] or "403" in resp["error"]


# ── monitor_start (babysit loops) ──


def test_monitor_start_dashboard_session(mock_dashboard):
    """monitor_start from a dashboard session posts the bare slot key."""
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner(
        "monitor_start",
        {"message": "check PR #1 until green", "interval_secs": 300, "max_cycles": 5},
    )
    assert "started" in result.lower()
    assert "loop-new" in result
    assert len(_MockAutonudgeHandler.created_bodies) == 1
    body = _MockAutonudgeHandler.created_bodies[0]
    assert body["session_key"] == "chat-3-1700000000"
    assert body["message"] == "check PR #1 until green"
    assert body["idle_secs"] == 300
    assert body["max_cycles"] == 5


def test_monitor_start_slack_session(mock_dashboard, monkeypatch):
    """monitor_start from a Slack thread session passes the namespaced key through."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1700000000.123456")
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch CI"})
    assert "started" in result.lower()
    body = _MockAutonudgeHandler.created_bodies[0]
    assert body["session_key"] == "slack:1700000000.123456"
    assert body["idle_secs"] == 300  # default interval


def test_monitor_start_discord_session(mock_dashboard, monkeypatch):
    """monitor_start from a Discord DM session passes the namespaced key through."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "discord:kirocrew:direct:42")
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch the deploy"})
    assert "started" in result.lower()
    assert _MockAutonudgeHandler.created_bodies[0]["session_key"] == "discord:kirocrew:direct:42"


def test_monitor_start_rejects_cron_context(mock_dashboard, monkeypatch):
    """Non-nudge-able contexts (cron/hook/subagent) get a clean noop message."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "cron:job-9")
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch"})
    assert "only works" in result.lower()
    assert not _MockAutonudgeHandler.created_bodies


def test_monitor_start_refuses_pid_walked_identity(mock_dashboard, monkeypatch):
    """STRICT session resolution: no env var -> refuse, even if a PID walk
    would resolve to a parent session.

    A subagent spawned under a dashboard/Slack/Discord slot lives in the
    parent's process tree; the lenient resolver would let it inherit the
    parent identity and mint a persistent unattended loop the parent user
    never asked for. monitor_start must use the env-var-only resolver.
    """
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    # Simulate a PID walk that WOULD succeed if (wrongly) consulted.
    monkeypatch.setattr(
        mcp_core, "_resolve_session_key", lambda: "dashboard:chat-3-1700000000"
    )
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch"})
    assert "only works" in result.lower()
    assert not _MockAutonudgeHandler.created_bodies


def test_autonudge_stop_refuses_pid_walked_identity(mock_dashboard, monkeypatch):
    """autonudge_stop also mutates another session's loop state -> same
    strict env-var-only resolution as monitor_start."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.setattr(
        mcp_core, "_resolve_session_key", lambda: "dashboard:chat-3-1700000000"
    )
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-9",
        "slot_key": "chat-3-1700000000",
    }
    result = _call_tool_inner("autonudge_stop", {})
    assert "only works" in result.lower()
    assert "loop-9" not in result


def test_autonudge_stop_slack_session(mock_dashboard, monkeypatch):
    """autonudge_stop works from a Slack session (channel-bound loop)."""
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1700000000.123456")
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-2",
        "slot_key": "slack:1700000000.123456",
    }
    result = _call_tool_inner("autonudge_stop", {"reason": "PR is green"})
    assert "stopped" in result.lower()
    assert "loop-2" in result
