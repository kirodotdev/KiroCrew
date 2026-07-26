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
    # (loop_id, body) pairs received by PATCH /api/autonudge/{id}.
    patched: list[tuple[str, dict]] = []

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

    def do_PATCH(self):  # noqa: N802
        base = self.path.split("?", 1)[0]
        if base.startswith("/api/autonudge/"):
            if self._reject_internal_secret():
                return
            if not self._has_valid_token():
                self._json(403, {"error": "Token required"})
                return
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            type(self).patched.append((base.rsplit("/", 1)[-1], body))
            self._json(
                200,
                {
                    "ok": True,
                    "loop": {
                        "id": base.rsplit("/", 1)[-1],
                        "slot_key": "chat-3-1700000000",
                        "message": body.get("message", "old"),
                        "idle_secs": body.get("idle_secs", 300),
                        "max_cycles": body.get("max_cycles", 24),
                        "cycle_count": 7,
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


# ── Runaway-loop cap (monitor_start default max_cycles) ──


def test_monitor_start_defaults_to_a_bounded_cap(mock_dashboard):
    """Omitting max_cycles must NOT arm an unbounded loop.

    An unbounded loop stops only when the model volunteers autonudge_stop.
    Observed loop stores show that is unreliable: real babysit loops ran to
    24/24 and 20/20 delivered cycles and stopped only because a cap was set.
    """
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch PR"})
    body = _MockAutonudgeHandler.created_bodies[0]
    assert body["max_cycles"] == mcp_core._MONITOR_DEFAULT_MAX_CYCLES
    assert body["max_cycles"] > 0
    assert "no cycle cap" not in result.lower()


def test_monitor_start_explicit_zero_is_still_unlimited(mock_dashboard):
    """An explicit 0 means the caller really wants unlimited — and is told so."""
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch PR", "max_cycles": 0})
    assert _MockAutonudgeHandler.created_bodies[0]["max_cycles"] == 0
    assert "no cycle cap" in result.lower()


def test_monitor_start_success_states_idle_semantics_and_stop_duty(mock_dashboard):
    """The success string must not read as a fixed period, and must put the
    stop obligation on the caller rather than the cap."""
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch PR", "interval_secs": 300})
    assert "ends" in result.lower()
    assert "autonudge_stop" in result
    assert "backstop" in result.lower()


# ── Arm-failure diagnosability ──


def test_monitor_start_arm_failure_names_the_failing_step(mock_dashboard, tmp_path, monkeypatch):
    """A token-mint failure must be reported as an ARM failure, not as flakiness.

    The pre-fix code collapsed every failure into "Failed to start monitor
    loop: could not obtain local user token", which is indistinguishable from
    the transient MCP reconnects agents are told to retry through — so real arm
    failures got written off while no loop existed.
    """
    # Wrong local secret -> GET /api/token/local returns 403 -> no user token.
    (tmp_path / ".local_secret").write_text("not-the-secret")
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
    _MockAutonudgeHandler.created_bodies = []
    result = _call_tool_inner("monitor_start", {"message": "watch PR"})
    assert "could not arm" in result.lower()
    assert "no monitoring is active" in result.lower()
    assert "not a transient" in result.lower()
    # The failing step is named, not swallowed.
    assert "/api/token/local" in result
    assert "403" in result
    assert not _MockAutonudgeHandler.created_bodies


def test_local_user_token_reason_when_no_secret(tmp_path, monkeypatch):
    """No readable local secret is a distinct, named reason."""
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
    token, why = mcp_core._local_user_token_with_reason()
    assert token == ""
    assert "secret" in why.lower()


def test_local_user_token_wrapper_still_returns_bare_token(mock_dashboard):
    """The original one-value helper keeps working for existing callers."""
    assert mcp_core._local_user_token() == _MockAutonudgeHandler.issued_token


# ── monitor_update ──


def test_monitor_update_patches_this_sessions_loop(mock_dashboard):
    """A revised instruction reaches PATCH for the loop bound to this session."""
    _MockAutonudgeHandler.loop_for_slot = {"id": "loop-7", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner(
        "monitor_update",
        {"message": "PR moved on — now check the Coverage Gate only", "max_cycles": 40},
    )
    assert "updated" in result.lower()
    assert len(_MockAutonudgeHandler.patched) == 1
    loop_id, body = _MockAutonudgeHandler.patched[0]
    assert loop_id == "loop-7"
    assert body["message"] == "PR moved on — now check the Coverage Gate only"
    assert body["max_cycles"] == 40
    # Untouched fields are omitted, not defaulted over.
    assert "idle_secs" not in body


def test_monitor_update_interval_maps_to_idle_secs(mock_dashboard):
    _MockAutonudgeHandler.loop_for_slot = {"id": "loop-7", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    _call_tool_inner("monitor_update", {"interval_secs": 900})
    assert _MockAutonudgeHandler.patched[0][1] == {"idle_secs": 900}


def test_monitor_update_targets_only_the_callers_own_loop(mock_dashboard):
    """OWNERSHIP: the loop id comes from the caller's binding key, never the args.

    PATCH /api/autonudge/{loop_id} takes an opaque id and cannot tell whose
    loop it is, so accepting a model-supplied id would let one session rewrite
    another session's standing instruction. The tool exposes NO loop-id
    parameter at all, so the schema rejects any attempt to name one, and the id
    actually patched is the one resolved from this session's binding key.
    """
    from kiro_crew.validation import ValidationError

    _MockAutonudgeHandler.loop_for_slot = {"id": "mine-1", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    with pytest.raises(ValidationError, match="loop_id"):
        _call_tool_inner("monitor_update", {"message": "x", "loop_id": "someone-elses-loop"})
    assert not _MockAutonudgeHandler.patched

    # And a legitimate update patches only the caller's own resolved loop.
    _call_tool_inner("monitor_update", {"message": "x"})
    assert [lid for lid, _ in _MockAutonudgeHandler.patched] == ["mine-1"]


def test_monitor_update_without_a_loop_is_a_clean_noop(mock_dashboard):
    _MockAutonudgeHandler.loop_for_slot = None
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "x"})
    assert "no monitor loop" in result.lower()
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_requires_at_least_one_field(mock_dashboard):
    _MockAutonudgeHandler.loop_for_slot = {"id": "loop-7", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {})
    assert "nothing to change" in result.lower()
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_rejects_blank_message(mock_dashboard):
    """A whitespace-only message would blank the instruction — refuse it."""
    _MockAutonudgeHandler.loop_for_slot = {"id": "loop-7", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "   "})
    assert "must not be empty" in result.lower()
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_rejects_cron_context(mock_dashboard, monkeypatch):
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "cron:job-9")
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "x"})
    assert "only works" in result.lower()
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_refuses_pid_walked_identity(mock_dashboard, monkeypatch):
    """Same strict resolution as monitor_start: a subagent under the parent's
    process tree must not be able to rewrite the parent session's loop."""
    monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
    monkeypatch.setattr(
        mcp_core, "_resolve_session_key", lambda: "dashboard:chat-3-1700000000"
    )
    _MockAutonudgeHandler.loop_for_slot = {"id": "loop-7", "slot_key": "chat-3-1700000000"}
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "x"})
    assert "only works" in result.lower()
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_slack_session(mock_dashboard, monkeypatch):
    monkeypatch.setenv("KIROCREW_SESSION_KEY", "slack:1700000000.123456")
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-slack",
        "slot_key": "slack:1700000000.123456",
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "revised"})
    assert "updated" in result.lower()
    assert _MockAutonudgeHandler.patched[0][0] == "loop-slack"


# ── monitor_update: false-success guards ──


def test_monitor_update_revives_a_capped_loop(mock_dashboard):
    """Raising the cap on a capped loop must actually re-arm it.

    A loop that hit max_cycles has active=False; update() mutates fields but
    only re-arms an ACTIVE loop, so the documented "raise the cap near the end"
    flow reported "the loop wakes you" while nothing was armed — the exact
    false-success class this tool exists to remove.
    """
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-capped",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 24,
        "max_cycles": 24,
        "active": False,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"max_cycles": 40})
    assert len(_MockAutonudgeHandler.patched) == 1
    _, body = _MockAutonudgeHandler.patched[0]
    assert body["max_cycles"] == 40
    assert body["active"] is True, "capped loop was patched without being re-armed"
    assert "re-armed" in result


def test_monitor_update_refuses_a_cap_at_or_below_cycle_count(mock_dashboard):
    """A cap <= delivered cycles deactivates the loop without firing again,
    so promising another wake would be a lie."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 12,
        "max_cycles": 24,
        "active": True,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"max_cycles": 12})
    assert "at or below" in result
    assert "12" in result
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_zero_cap_is_allowed_on_a_capped_loop(mock_dashboard):
    """0 = unlimited always permits another cycle, so it revives rather than
    tripping the at-or-below guard."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-capped",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 24,
        "max_cycles": 24,
        "active": False,
    }
    _MockAutonudgeHandler.patched = []
    _call_tool_inner("monitor_update", {"max_cycles": 0})
    _, body = _MockAutonudgeHandler.patched[0]
    assert body["max_cycles"] == 0
    assert body["active"] is True


def test_monitor_update_active_loop_is_not_force_activated(mock_dashboard):
    """An already-active loop must not have `active` injected into the patch."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 3,
        "max_cycles": 24,
        "active": True,
    }
    _MockAutonudgeHandler.patched = []
    _call_tool_inner("monitor_update", {"message": "revised"})
    _, body = _MockAutonudgeHandler.patched[0]
    assert "active" not in body


def test_arm_failure_does_not_call_a_network_error_permanent(mock_dashboard):
    """A raised token request CAN be transient — don't claim a retry is futile.

    The deterministic wording is reserved for definite refusals (no readable
    secret, an HTTP status), not for a timeout/connection reset.
    """
    transient = {"error": f"could not obtain local user token — {mcp_core._TRANSIENT_TOKEN_MARKER} TimeoutError: timed out"}  # noqa: E501
    msg = mcp_core._monitor_arm_failure("monitor_start", "chat-3-1700000000", transient)
    assert "could NOT arm" in msg
    assert "may be transient" in msg
    assert "will not fix it" not in msg

    definite = {"error": "could not obtain local user token — GET /api/token/local returned HTTP 403"}
    msg2 = mcp_core._monitor_arm_failure("monitor_start", "chat-3-1700000000", definite)
    assert "will not fix it" in msg2
    assert "may be transient" not in msg2


def test_transient_token_marker_matches_the_emitted_reason(tmp_path, monkeypatch):
    """Guard against drift between the emitted reason and the marker constant."""
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    (tmp_path / ".local_secret").write_text("secret")
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
    # Unroutable base URL -> urlopen raises -> the exception-path reason.
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")
    _token, why = mcp_core._local_user_token_with_reason()
    assert mcp_core._TRANSIENT_TOKEN_MARKER in why


def test_indeterminate_write_failure_is_not_reported_as_no_monitoring(mock_dashboard):
    """A POST that never got an answer may still have armed the loop.

    The server-side arm is shielded, so claiming "NO monitoring is active" after
    a lost response is a false negative that invites a duplicate arm. The signal
    is the structural `indeterminate` flag, NOT a substring of the exception
    text — `http.client.RemoteDisconnected` stringifies to "Remote end closed
    connection without response", so message matching would never fire.
    """
    msg = mcp_core._monitor_arm_failure(
        "monitor_start",
        "chat-3-1700000000",
        {"error": "RemoteDisconnected: Remote end closed connection without response",
         "indeterminate": True},
    )
    assert "MAY have landed" in msg
    assert "NO monitoring is active" not in msg
    assert "check whether a loop exists" in msg


def test_lost_response_on_a_write_is_flagged_indeterminate(monkeypatch, tmp_path):
    """`_write_user` must set the flag itself; callers cannot infer it."""
    import http.server
    import threading

    class _Hangup(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.close_connection = True  # respond with nothing at all

        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"token":"t","expires_in":900}')

        def log_message(self, *_a):  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Hangup)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        (tmp_path / ".local_secret").write_text("secret")
        monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
        monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
        resp = mcp_core._write_user("/api/autonudge", {"x": 1}, method="POST")
        assert resp.get("indeterminate") is True, resp
        # The exception CLASS NAME is preserved, which bare str(e) would have
        # dropped. The specific class is platform-dependent (POSIX raises
        # RemoteDisconnected/IncompleteRead, Windows ConnectionAbortedError),
        # so pin the contract rather than the class.
        head = resp["error"].split(":", 1)[0]
        assert head and " " not in head and head.endswith(("Error", "Read", "Disconnected"))
    finally:
        server.shutdown()


def test_monitor_update_refuses_to_resume_an_explicitly_paused_loop(mock_dashboard):
    """Reviving must not be a side effect of a metadata edit.

    A loop paused by the user (or the stop control) has `active=False` without
    having hit its cap; resuming it from a `monitor_update` would restart
    unattended tool execution nobody asked for.
    """
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-paused",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 3,
        "max_cycles": 24,
        "active": False,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "revised"})
    assert "PAUSED" in result
    assert "will not resume" in result
    assert not _MockAutonudgeHandler.patched


def test_monitor_update_refuses_revival_without_raising_the_cap(mock_dashboard):
    """A cap-stopped loop is only revived when the cap is actually raised."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-capped",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 24,
        "max_cycles": 24,
        "active": False,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"message": "revised"})
    # Refused either as "paused" or by the at-or-below-cap guard; what matters is
    # that a message-only edit never silently re-arms it.
    assert "PAUSED" in result or "at or below" in result
    assert not _MockAutonudgeHandler.patched


def test_retryable_mint_status_is_reported_as_transient(mock_dashboard, tmp_path, monkeypatch):
    """429/5xx from the mint endpoint are retryable, not a definite refusal."""
    import http.server
    import threading

    class _Flaky(http.server.BaseHTTPRequestHandler):
        code = 503

        def do_GET(self):  # noqa: N802
            self.send_response(self.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error":"busy"}')

        def log_message(self, *_a):  # noqa: A002
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Flaky)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(mcp_core, "_API", f"http://127.0.0.1:{server.server_address[1]}")
        monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
        (tmp_path / ".local_secret").write_text("secret")
        monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("", 0.0))
        _t, why = mcp_core._local_user_token_with_reason()
        assert mcp_core._TRANSIENT_TOKEN_MARKER in why
        assert "503" in why
        msg = mcp_core._monitor_arm_failure(
            "monitor_start", "chat-3-1700000000", {"error": f"could not obtain … {why}"}
        )
        assert "may be transient" in msg
        assert "will not fix it" not in msg
    finally:
        server.shutdown()


def test_monitor_update_flags_a_cap_that_only_covers_the_running_cycle(mock_dashboard):
    """cycle_count excludes an in-flight cycle, so cap == count+1 may already be
    spent — say so rather than promising another wake."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 11,
        "max_cycles": 24,
        "active": True,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"max_cycles": 12})
    assert _MockAutonudgeHandler.patched, "a cap of count+1 is allowed, just caveated"
    assert "only one more delivered cycle" in result
    assert "do not count on another wake" in result


def test_monitor_update_comfortable_cap_still_promises_a_wake(mock_dashboard):
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 11,
        "max_cycles": 24,
        "active": True,
    }
    _MockAutonudgeHandler.patched = []
    result = _call_tool_inner("monitor_update", {"max_cycles": 30})
    assert "the loop wakes you with the revised instruction" in result
    assert "only one more delivered cycle" not in result


def test_connection_refused_is_a_definite_failure(monkeypatch, tmp_path):
    """A refusal happens BEFORE the request is sent, so nothing was applied.

    Classifying it indeterminate made monitor_start suggest an arm might exist
    when the gateway was simply not listening.
    """
    (tmp_path / ".local_secret").write_text("secret")
    monkeypatch.setattr(mcp_core, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(mcp_core, "_USER_TOKEN_CACHE", ("cached-t", 1e18))
    monkeypatch.setattr(mcp_core, "_API", "http://127.0.0.1:1")
    resp = mcp_core._write_user("/api/autonudge", {"x": 1}, method="POST")
    assert "indeterminate" not in resp, resp
    msg = mcp_core._monitor_arm_failure("monitor_start", "chat-3-1700000000", resp)
    assert "NO monitoring is active" in msg
    assert "MAY have landed" not in msg


def test_lost_patch_response_does_not_verify_on_the_message_alone(mock_dashboard, monkeypatch):
    """A multi-field patch must match on EVERY field before claiming success.

    A patch that raised max_cycles while leaving the message unchanged would
    "verify" on the message and report success while the stale cap keeps the
    loop stopped.
    """
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 3,
        "max_cycles": 24,
        "active": True,
        "message": "same text",
        "idle_secs": 300,
    }
    _MockAutonudgeHandler.patched = []
    # PATCH loses its response; the re-read shows the message matching (it was
    # never going to change) but the cap NOT applied.
    monkeypatch.setattr(
        mcp_core,
        "_patch_user",
        lambda *_a, **_k: {"error": "ConnectionResetError: boom", "indeterminate": True},
    )
    result = _call_tool_inner(
        "monitor_update", {"message": "same text", "max_cycles": 40}
    )
    assert "NO confirmed answer" in result
    assert "Verified applied" not in result


def test_monitor_update_reports_no_wake_when_the_response_says_inactive(
    mock_dashboard, monkeypatch
):
    """Trust the POST-patch state, not the pre-PATCH GET.

    The cap can deactivate the loop between the lookup and the PATCH, leaving a
    raised-cap loop inactive while the tool promises another wake.
    """
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-7",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 5,
        "max_cycles": 24,
        "active": True,
    }
    _MockAutonudgeHandler.patched = []
    monkeypatch.setattr(
        mcp_core,
        "_patch_user",
        lambda *_a, **_k: {
            "ok": True,
            "loop": {"id": "loop-7", "cycle_count": 24, "max_cycles": 40,
                     "idle_secs": 300, "active": False},
        },
    )
    result = _call_tool_inner("monitor_update", {"max_cycles": 40})
    assert "INACTIVE" in result
    assert "will NOT wake" in result


def test_lost_patch_response_requires_the_same_loop_identity(mock_dashboard, monkeypatch):
    """A concurrently replaced loop must not be mistaken for this update."""
    _MockAutonudgeHandler.loop_for_slot = {
        "id": "loop-old",
        "slot_key": "chat-3-1700000000",
        "cycle_count": 2,
        "max_cycles": 24,
        "active": True,
    }
    monkeypatch.setattr(
        mcp_core,
        "_patch_user",
        lambda *_a, **_k: {"error": "ConnectionResetError: boom", "indeterminate": True},
    )
    # The re-read returns a DIFFERENT loop id whose message happens to match.
    real_get = mcp_core._get_user

    def _get(path):
        if "/slot/" in path and _get.calls:
            return {"loop": {"id": "loop-new", "message": "revised",
                             "max_cycles": 24, "idle_secs": 300, "active": True}}
        _get.calls = True
        return real_get(path)

    _get.calls = False
    monkeypatch.setattr(mcp_core, "_get_user", _get)
    result = _call_tool_inner("monitor_update", {"message": "revised"})
    assert "NO confirmed answer" in result
    assert "Verified applied" not in result
