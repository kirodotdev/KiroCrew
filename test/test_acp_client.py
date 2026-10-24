"""Tests for ACP client."""

import asyncio
import json
from collections import deque
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture
from acp_client_helpers import _stop_stderr_drain  # noqa: F401 - re-exported

from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    _format_acp_error,
)
from kiro_crew.acp.types import JSONRPC_METHOD_NOT_FOUND
from kiro_crew.agent_sdk import host_auth
from kiro_crew.testing.wait import await_until


def _scripted_process(lines, *, returncode=None):
    """Build a mock subprocess whose stdout.readline yields *lines* in order.

    Each entry of *lines* is a dict (serialized to a JSON-RPC line) or a raw
    bytes value. After the list is exhausted, readline blocks-then-returns an
    empty timeout-friendly value so _read_message's wait_for sees nothing new.
    """
    queue = deque(lines)

    async def _readline():
        if queue:
            item = queue.popleft()
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
            return (json.dumps(item) + "\n").encode()
        # Nothing left — emulate a quiet stream (no more frames this read).
        await asyncio.sleep(0)
        return b""

    proc = MagicMock()
    stdout = AsyncMock()
    stdout.readline = AsyncMock(side_effect=_readline)
    proc.stdout = stdout
    proc.returncode = returncode
    # stdin used by _send_error / _send_response.
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin
    return proc


class TestWaitForResponseDeferral:
    """F1: _wait_for_response must not spin on inbound server requests or
    foreign-id responses, must not drop them, and must re-inject them."""

    @pytest.mark.asyncio
    async def test_inbound_permission_request_deferred_not_spun(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # Awaiting response for req_id=7. A server->client permission request
        # arrives first carrying id=7 (colliding namespace), THEN the real
        # response for req 7 arrives.
        perm = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "options": [], "toolCall": {"title": "x"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        client._process = _scripted_process([perm, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(7, timeout=5.0)))
        assert result == {"ok": True}
        # Permission request must be re-injected (not dropped, not spun).
        assert len(client._buffer) == 1
        buffered = client._buffer.popleft()
        assert buffered.method == "session/request_permission"
        assert buffered.id == 7

    @pytest.mark.asyncio
    async def test_foreign_id_response_preserved(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        foreign = {"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        client._process = _scripted_process([foreign, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(3, timeout=5.0)))
        assert result == {"ok": True}
        assert len(client._buffer) == 1
        assert client._buffer.popleft().id == 99

    @pytest.mark.asyncio
    async def test_notification_goes_to_mcp_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"ok": True}}
        client._process = _scripted_process([notif, resp])

        result = await await_until(asyncio.ensure_future(client._wait_for_response(5, timeout=5.0)))
        assert result == {"ok": True}
        # Notification buffered for drain, NOT re-injected into _buffer.
        assert len(client._buffer) == 0
        assert len(client._mcp_notifications) == 1
        assert client._mcp_notifications[0].method == "session/update"

    @pytest.mark.asyncio
    async def test_deferred_reinjected_in_order_on_timeout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        first = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {},
        }
        second = {"jsonrpc": "2.0", "id": 88, "result": {"other": True}}
        # No matching response ever arrives -> timeout. Deferred frames must
        # still be re-injected in arrival order.
        client._process = _scripted_process([first, second])

        with pytest.raises(AcpError):
            await await_until(asyncio.ensure_future(client._wait_for_response(7, timeout=1.0)))
        assert len(client._buffer) == 2
        m0 = client._buffer.popleft()
        m1 = client._buffer.popleft()
        assert m0.method == "session/request_permission"
        assert m1.id == 88

    def test_session_timeout_progress_names_missing_failed_and_oauth_servers(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={
                    "serverName": "broken",
                    "error": "aws_secret_access_key=supersecret connection failed",
                },
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        progress = client._mcp_timeout_progress(
            [{"name": "ready"}, {"name": "broken"}, {"name": "silent"}]
        )

        assert "2/3 session-injected MCP server(s) reported" in progress
        assert "no report from silent" in progress
        assert "failed: broken" in progress
        assert "supersecret" not in progress
        assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_empty_roster_keeps_reports(self, tmp_path):
        """An empty ``mcpServers`` roster must not drop the staged reports.

        Mirrors ``AcpRuntime._mcp_init_progress``: with nothing to attribute
        against, the count is reported as ``roster unknown`` instead of the
        timeout reading as a bare budget.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            ),
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_init_failure",
                params={"serverName": "broken", "error": "connection failed"},
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"}),
        ]

        for expected in ([], None):
            progress = client._mcp_timeout_progress(expected)
            assert "2 MCP server report(s), roster unknown" in progress
            assert "session-injected" not in progress
            assert "failed: broken (connection failed)" in progress
            assert "awaiting authorization: oauth" in progress

    def test_session_timeout_progress_ignores_prior_attempt_frames(self, tmp_path):
        """A timed-out ``session/load``'s frames are not this attempt's progress.

        ``_drain_notifications`` clears the buffer only at the end of
        ``_initialize_session``, so the load -> ``session/new`` fallback still
        holds the previous attempt's ``server_initialized`` frames. The progress
        summary must honour the per-attempt floor ``_begin_session_report`` sets,
        or it asserts the stall is not in those servers on stale evidence.
        """
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        roster = [{"name": "ready"}]
        # Attempt 1 (session/load): the server reported and another asked for
        # authorization, then the load timed out.
        client._begin_session_report(roster)
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        client._mcp_notifications.append(
            JsonRpcMessage(method="_kiro.dev/mcp/oauth_request", params={"serverName": "oauth"})
        )
        # Attempt 2 (session/new fallback): nothing has reported yet.
        client._begin_session_report(roster)

        progress = client._mcp_timeout_progress(roster)

        assert "0/1 session-injected MCP server(s) reported" in progress
        assert "no report from ready" in progress
        assert "not in those servers" not in progress
        # The authorization the failed attempt asked for is still owed by the
        # user, so it is not floored (same rule as _drain_notifications).
        assert "awaiting authorization: oauth" in progress

        # A frame that arrives during attempt 2 is counted.
        client._mcp_notifications.append(
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"serverName": "ready"}
            )
        )
        progress = client._mcp_timeout_progress(roster)
        assert "1/1 session-injected MCP server(s) reported" in progress


class TestWaitForResponseActivityDeadline:
    """Low-A: a steady stream of notifications keeps _wait_for_response alive
    past the base timeout until the real response arrives."""

    @pytest.mark.asyncio
    async def test_streaming_notifications_extend_deadline(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)

        # Virtual clock so the test is fast and deterministic. Each readline
        # advances time by 0.05s; base timeout is 0.1s. Without activity-based
        # extension the call would die after ~2 reads; with it, the stream of
        # notifications keeps pushing the deadline out until the response lands.
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        frames = [notif] * 20 + [{"jsonrpc": "2.0", "id": 4, "result": {"loaded": True}}]
        queue = deque(frames)

        async def _readline():
            if queue:
                clock["t"] += 0.05  # each frame advances the virtual clock
                return (json.dumps(queue.popleft()) + "\n").encode()
            return b""

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        result = await await_until(asyncio.ensure_future(client._wait_for_response(4, timeout=0.1)))
        assert result == {"loaded": True}
        # All 20 notifications were drained while the deadline kept extending.
        assert len(client._mcp_notifications) == 20

    @pytest.mark.asyncio
    async def test_hard_cap_eventually_times_out(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        clock = {"t": 5000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}

        async def _readline():
            # Endless notifications, large time jumps — never a matching
            # response. The absolute hard cap must eventually fire.
            clock["t"] += 30.0
            return (json.dumps(notif) + "\n").encode()

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        with pytest.raises(AcpError):
            await await_until(asyncio.ensure_future(client._wait_for_response(1, timeout=0.1)))


class TestProcessMessageUnknownServerRequest:
    """F5: unknown server->client requests are classified for a -32601 reply,
    not silently skipped."""

    @pytest.mark.parametrize(
        ("msg_id", "method", "params", "expected"),
        [
            pytest.param(
                12,
                "fs/read_text_file",
                {"path": "/x"},
                "server_request_unknown",
                id="unknown_server_request_classified",
            ),
            pytest.param(
                3, "terminal/create", {}, "server_request_unknown", id="terminal_create_classified"
            ),
            # Unknown notification (method, NO id) is not a request -> skip.
            pytest.param(
                None, "some/unknown_notification", {}, "skip", id="notification_still_skipped"
            ),
            pytest.param(
                2,
                "session/request_permission",
                {},
                "permission",
                id="known_permission_request_not_unknown",
            ),
        ],
    )
    def test_process_message_classification(self, tmp_path, msg_id, method, params, expected):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=msg_id, method=method, params=params)
        assert client._process_message(msg, req_id=1) == expected

    @pytest.mark.asyncio
    async def test_reject_sends_method_not_found_error(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=42, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)

        client._process.stdin.write.assert_called_once()
        written = client._process.stdin.write.call_args[0][0].decode()
        payload = json.loads(written)
        assert payload["id"] == 42
        assert payload["error"]["code"] == JSONRPC_METHOD_NOT_FOUND
        assert "terminal/create" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_reject_noop_when_no_id(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=None, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)
        client._process.stdin.write.assert_not_called()


class TestFormatAcpError:
    """Tests for _format_acp_error — Bedrock-aware error rewriting.

    Covers the bug filed at task 86089e43: ACP backend errors used
    to be surfaced as the raw JSON-RPC dict (`Prompt error: {'code': -32603,
    ...}`), which dead-ends users when the picker can't expose a valid
    alternative. The helper rewrites known Bedrock failures into actionable
    text while preserving the request_id for support correlation, and scrubs
    embedded credentials / exfiltration URLs as defense-in-depth.
    """

    def test_non_dict_falls_back(self):
        assert _format_acp_error(None) == "Prompt error: None"
        assert _format_acp_error("boom") == "Prompt error: boom"

    def test_unknown_dict_preserves_raw(self):
        """An unrecognised shape must lose no information.

        The fallback now leads with the provider's own text rather than a repr
        of the JSON-RPC dict, but both fields still have to survive: the
        provider detail AND the non-boilerplate JSON-RPC message, which for
        codes other than -32603 is the only summary the error carries.
        """
        err = {"code": -32000, "message": "Something else", "data": "weird"}
        out = _format_acp_error(err)
        assert "Something else" in out
        assert "weird" in out
        # No dict repr: punctuation soup is not a user-facing error message.
        assert "Prompt error: {" not in out

    def test_model_not_available_rewrite(self):
        # With NO advertised list the caller cannot know whether this is an
        # entitlement or a capacity failure, so it keeps the capacity wording.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The model 'opus' "
                "is not available. Please use '/model' to select a different "
                "model and try again. (request_id: 3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e)"
            ),
        }
        out = _format_acp_error(err)
        assert "unavailable on the backend" in out
        assert "'opus'" in out
        assert "model picker" in out
        # Must NOT point at ~/.claude/settings.json — KiroCrew never reads it;
        # the model lives in config.json / the agent spec.
        assert "settings.json" not in out
        assert "config.json" in out
        # Nor echo the provider's own "use '/model'" advice: that is a kiro-cli
        # TUI command, inert in the dashboard, Slack, and cron.
        assert "/model" not in out
        # Request id is preserved for support correlation.
        assert "3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e" in out
        # Should not leak the raw dict prefix when we have a real rewrite.
        assert "Prompt error: {" not in out

    def test_unentitled_model_names_what_the_account_can_use(self):
        """The free-tier case: say it is an access problem and list the options.

        Upstream sends the SAME string for entitlement and capacity failures,
        so the advertised list is the only thing that distinguishes them.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'opus-4.8-1m' is not available. (request_id: abc-123)",
        }
        out = _format_acp_error(err, ["claude-sonnet-4-6", "claude-haiku-4-5"])
        assert "does not have access" in out
        assert "'opus-4.8-1m'" in out
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4-6" in out
        assert "claude-haiku-4-5" in out
        # Must NOT blame capacity or tell them to wait — no retry will help.
        assert "capacity" not in out
        assert "wait a minute" not in out
        assert "abc-123" in out

    def test_advertised_model_still_reads_as_capacity(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The model IS on offer, so a rejection really is a transient blip and
        # must keep the capacity wording (and stay retryable).
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        out = _format_acp_error(err, ["opus", "sonnet"])
        assert "unavailable on the backend" in out
        assert "does not have access" not in out
        assert _is_transient_raw_error(err, ["opus", "sonnet"]) is True

    def test_unentitled_model_is_terminal_not_retried(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The retry verdict must move with the wording -- retrying a model the
        # account was never offered just reproduces the same rejection.
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        assert _is_transient_raw_error(err, ["sonnet", "haiku"]) is False
        # Unknown entitlement -> unchanged (transient), never a false accusation.
        assert _is_transient_raw_error(err, None) is True
        assert _is_transient_raw_error(err, []) is True

    def test_unentitled_match_is_case_insensitive(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # An entitled-but-differently-cased model must not be called unentitled.
        err = {"code": -32603, "message": "x", "data": "The model 'Opus' is not available."}
        assert _is_transient_raw_error(err, ["opus"]) is True
        assert "does not have access" not in _format_acp_error(err, ["opus"])

    def test_long_available_list_is_capped(self):
        err = {"code": -32603, "message": "x", "data": "The model 'nope' is not available."}
        out = _format_acp_error(err, [f"model-{i}" for i in range(12)])
        assert "+4 more" in out
        assert "model-7" in out
        assert "model-8" not in out

    def test_throttling_exception_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ThrottlingException: Too many requests "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "wait" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    @pytest.mark.parametrize(
        ("message", "data", "expected"),
        [
            pytest.param(
                "x",
                "TooManyRequestsException: rate limited",
                "throttling",
                id="too_many_requests_rewrite",
            ),
            pytest.param(
                "x",
                "ExpiredToken: signature expired",
                "authentication failed",
                id="expired_token_rewrite",
            ),
            # Some backends put the trigger word in `message` rather than `data`.
            pytest.param("Rate limit exceeded", "", "throttling", id="throttle_keyword_in_message"),
        ],
    )
    def test_rewrite_names_the_failure_class(self, message, data, expected):
        err = {"code": -32603, "message": message, "data": data}
        assert expected in _format_acp_error(err).lower()

    def test_access_denied_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDeniedException: not authorized to invoke",
        }
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()
        assert "aws sso login" in out.lower()

    def test_missing_request_id_omits_suffix(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'sonnet' is not available.",
        }
        out = _format_acp_error(err)
        assert "request_id" not in out
        assert "'sonnet'" in out

    def test_credentials_in_data_are_redacted(self):
        """AWS access keys embedded in upstream errors must not leak to the UI.

        Recognized error patterns (auth/throttle/model-unavailable) already drop
        the `data` field when constructing the rewritten message, so the secret
        is gone simply by virtue of the rewrite. The redaction layer is the
        defense-in-depth fallback for the unknown-shape path; this test pins
        the absence guarantee on the recognized-pattern path.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDenied: AKIAIOSFODNN7EXAMPLE not authorized",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credentials_in_unknown_dict_fallback_are_redacted(self):
        """The fallback path echoes raw dict — must still scrub secrets."""
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # The scrubbed placeholder proves redaction ran on this path.
        assert "REDACTED" in out
        # The surrounding provider text still reaches the user.
        assert "leak:" in out

    def test_exfiltration_url_in_unknown_dict_fallback_is_redacted(self):
        """The fallback path echoes raw dict — exfil URLs must also be scrubbed.

        Pairs with `test_credentials_in_unknown_dict_fallback_are_redacted` to
        cover the second redaction layer (`redact_exfiltration_urls`).
        """
        # Use a URL whose query carries a base64-blob — matches _EXFIL_PATTERNS
        # and is what real provider error payloads tend to look like when they
        # echo signed callback URLs.
        leaked_blob = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVTRUNSRVRBQ0NFU1NLRVk" + "A" * 30
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": f"callback to https://attacker.example.com/exfil?token={leaked_blob}",
        }
        out = _format_acp_error(err)
        assert leaked_blob not in out, "leaked credential blob must be redacted"
        assert "REDACTED" in out

    def test_sensitive_content_emits_log_warning(self, caplog):
        """When the redaction layer scrubs anything, a warning MUST be logged.

        Silent scrubbing hides upstream-leak signals from security review;
        the warning lets operators notice that a provider echoed sensitive
        content back. The warning intentionally includes only counts — never
        the redacted values.
        """
        import logging

        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            _format_acp_error(err)

        warnings = [r for r in caplog.records if "sensitive content" in r.getMessage()]
        assert warnings, "expected a redaction warning to be logged"
        assert "AKIAIOSFODNN7EXAMPLE" not in warnings[0].getMessage()

    def test_internal_server_error_rewrite(self):
        """A transient 5xx from the backend must

        classify as a momentary backend error rather than dumping the raw
        -32603 JSON-RPC dict at the user.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                '{ name: "InternalServerError", ... please try again ... })) '
                "(request_id: 91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "retry in a moment" in out.lower()
        assert "5xx" in out
        # Request id preserved for support correlation.
        assert "91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f" in out
        # Must not dead-end the user with the raw dict.
        assert "Prompt error: {" not in out

    @pytest.mark.parametrize(
        ("data", "absent"),
        [
            # A bare 'please try again' transient hint (no explicit 5xx token)
            # still classifies as transient.
            pytest.param(
                "Service hiccup, please try again.",
                "Prompt error: {",
                id="please_try_again_phrasing_rewrite",
            ),
            pytest.param(
                "InternalServerError occurred",
                "request_id",
                id="transient_5xx_missing_request_id_omits_suffix",
            ),
            # kiro-cli's pre-stream generation failure wrapper gets the friendly
            # retry guidance instead of the raw-dict fallback: data carried no
            # request_id and no error class, only the generic wrapper string.
            pytest.param(
                "Kiro failed to generate a response",
                "Prompt error: {",
                id="kiro_generic_generation_failure_formats_as_transient",
            ),
        ],
    )
    def test_transient_rewrite(self, data, absent):
        err = {"code": -32603, "message": "Internal error", "data": data}
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert absent not in out

    @pytest.mark.parametrize(
        ("message", "data", "request_id"),
        [
            # DispatchFailure / connection-reset shapes are transient too.
            pytest.param(
                "x",
                "DispatchFailure: ConnectionReset (request_id: bbbb2222-cccc-3333-dddd-444455556666)",
                "bbbb2222-cccc-3333-dddd-444455556666",
                id="dispatch_failure_rewrite",
            ),
            # An HTTP/status-anchored 50x token IS a genuine transient signal.
            pytest.param(
                "Internal error",
                "upstream returned HTTP 503 (request_id: aaaa1111-bbbb-2222-cccc-333344445555)",
                "aaaa1111-bbbb-2222-cccc-333344445555",
                id="http_500_status_context_still_classifies_transient",
            ),
        ],
    )
    def test_transient_rewrite_keeps_the_request_id(self, message, data, request_id):
        err = {"code": -32603, "message": message, "data": data}
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert request_id in out

    def test_transient_branch_does_not_leak_credentials(self):
        """Defense-in-depth on the new transient path: an AWS key embedded in

        the upstream `data` must never reach the UI. The rewrite drops the
        `data` field (so the secret is gone by construction), and the function
        still routes the result through redact_credentials/redact_exfiltration_urls
        at the end — the new branch adds no early return that bypasses it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: leaked AKIAIOSFODNN7EXAMPLE please try again",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "transient error" in out.lower()

    @pytest.mark.parametrize(
        ("message", "data", "present", "absent"),
        [
            # A throttle that also says 'please try again' stays a throttle —
            # throttling precedence must survive the transient branch.
            pytest.param(
                "Internal error",
                "ThrottlingException: slow down, please try again",
                "throttling",
                "transient error",
                id="throttle_precedence_over_transient",
            ),
            # An aborted request leaves a transport error beside the rejection; the
            # credential branch is checked first so the 5xx family cannot win.
            pytest.param(
                "Encountered an error in the response stream",
                "DispatchFailure ConnectionResetError: the bearer token is invalid",
                "kiro-cli login",
                "transient error",
                id="invalid_bearer_token_wins_over_transport_error",
            ),
            # The auth-status branch must not swallow real 5xx errors.
            pytest.param(
                "Internal error",
                "ServiceUnavailableException: HTTP 503",
                "transient",
                "kiro-cli login",
                id="genuine_5xx_still_transient_with_auth_absent",
            ),
            # Session expiry checked before 5xx: a DispatchFailure wrapping a
            # session-expired message must surface as auth, not transient.
            pytest.param(
                "Internal error",
                "DispatchFailure ConnectionResetError: session expired",
                "kiro-cli login",
                "transient error",
                id="session_expired_with_5xx_token_wins",
            ),
        ],
    )
    def test_classification_precedence(self, message, data, present, absent):
        out = _format_acp_error({"code": -32603, "message": message, "data": data}).lower()
        assert present in out
        assert absent not in out

    def test_non_transient_minus_32603_falls_through_to_fallback(self):
        """A non-transient -32603 whose only 'internal'-ish signal is the

        canonical JSON-RPC message 'Internal error' must NOT be misclassified
        as transient. The transient branch keys off the provider `data` field
        only (never the generic `message`), so a deterministic failure like a
        ValidationException surfaces its real cause instead.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        # The real cause is shown verbatim — stronger than the old assertion,
        # which only required a dict repr containing it somewhere.
        assert "ValidationException: input contains an unsupported field 'foo'" in out
        # The -32603 boilerplate message is dropped as noise next to that.
        assert "Internal error" not in out

    @pytest.mark.parametrize(
        ("message", "data"),
        [
            # A standalone numeric (e.g. a token-limit value) must not match the
            # 5xx branch — the numeric match is anchored to an HTTP/status
            # context, not a bare 500/502/503 token.
            pytest.param(
                "Internal error",
                "max_tokens 500 exceeds the model limit of 200000",
                id="bare_500_token_is_not_treated_as_5xx",
            ),
            # The generation-failure pattern is scoped to `data` — the phrase
            # appearing only in the JSON-RPC `message` field must not trigger the
            # friendly transient branch (mirrors the model-unavailable scoping).
            pytest.param(
                "Kiro failed to generate a response",
                "ValidationException: input contains an unsupported field 'foo'",
                id="generation_failure_phrase_in_message_only_is_not_transient",
            ),
            # Scoped to `data` like its sibling: the phrase in the JSON-RPC
            # `message` alone must not flip a deterministic failure to transient.
            pytest.param(
                "The service failed to process the request",
                "ValidationException: input contains an unsupported field 'foo'",
                id="process_failure_phrase_in_message_only_is_not_transient",
            ),
        ],
    )
    def test_not_transient_and_shows_the_cause(self, message, data):
        err = {"code": -32603, "message": message, "data": data}
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert data in out

    def test_kiro_process_failure_formats_as_transient_and_keeps_request_id(self):
        """kiro-cli's post-stream sibling wrapper ("The service failed to
        process the request (request_id: ...)") gets the same retry guidance
        as the generation-failure branch, and the request_id survives the
        rewrite so a support thread can quote it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "The service failed to process the request "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out
        assert "Prompt error: {" not in out
        # The string fallback in llm_helpers classifies both the raw wrapper
        # and the rewrite, so a path that lost the structured flag still retries.
        from kiro_crew.llm_helpers import is_transient_backend_error

        assert is_transient_backend_error(str(err["data"])) is True
        assert is_transient_backend_error(out) is True

    def test_session_expired_rewrite(self):
        """An expired session gets actionable sign-in guidance rather than the
        misleading transient-5xx retry advice.

        The sign-in half is the failing harness's own declared remedy, so this
        asserts it is rendered verbatim rather than pinning the wording here: a
        second copy of that wording is what let the message name `kiro-cli login`
        to an operator on a harness that signs in some other way.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure: session expired",
        }
        out = _format_acp_error(err)
        # The signed-out MESSAGE, not the remedy. This arm reaches the text on real
        # evidence (a 401/403, or prose saying the session expired), so it may assert
        # the state; the remedy is the register for a caveat rendered having measured
        # nothing, and it deliberately makes no state claim.
        assert host_auth.signed_out_message("") in out
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading 5xx message.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_session_expired_names_the_failing_harness(self):
        """A harness that signs in elsewhere gets ITS message, not kiro-cli's.

        The regression this guards: the arm hardcoded `kiro-cli login`, so an
        expiry on a harness with its own credential file told the operator to run
        a command that could not fix it. The retry verdict is this arm's own
        finding about the error, so it must survive on every backend.

        Reads ``signed_out_message`` and not ``sign_in_remedy``, and the
        difference is the point: this arm HAS evidence -- a 401, or prose saying
        the session expired -- so it may assert the state. The remedy is the
        register for the places that render a standing line having measured
        nothing, and using it here would drop the diagnosis.
        """
        err = {"code": -32603, "message": "Internal error", "data": "HTTP 401"}
        for backend in ("claude", "codex"):
            out = _format_acp_error(err, backend=backend)
            assert host_auth.signed_out_message(backend) in out
            assert "kiro-cli login" not in out.lower(), backend
            assert "will not help" in out.lower(), backend

    def test_session_expired_by_http_status(self):
        """A bare 401/403 is the shape an expired session actually arrives in:
        the rejection carries no explanatory wording, so status alone must
        drive the classification."""
        for status in ("HTTP 401", "HTTP 403", "status code 401", "status 403"):
            err = {"code": -32603, "message": "Internal error", "data": status}
            out = _format_acp_error(err)
            assert "kiro-cli login" in out.lower(), f"No sign-in guidance for: {status!r}"
            assert "transient error" not in out.lower(), f"Misclassified: {status!r}"

    def test_session_expired_401_with_transport_error(self):
        """The reported failure mode: an aborted request leaves a transport
        error alongside the 401, and the 5xx family must not win and tell the
        user to retry."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: HTTP 401",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_invalid_bearer_token_rewrite(self):
        """The account-switch rejection: a credential the running child still
        holds is rejected as invalid, with no status code and no expiry wording,
        so it must still reach the sign-in guidance instead of the raw string."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The bearer token included in the request is invalid.",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading transient-5xx advice.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_unrelated_invalid_does_not_read_as_credential_failure(self):
        """The fenced gap must not let an unrelated 'invalid' in a combined
        haystack turn a validation fault into a sign-in prompt."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ValidationException: refreshed the bearer token successfully. "
                "Field 'temperature' is invalid"
            ),
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" not in out.lower()

    def test_session_expired_variants(self):
        """Various kiro-cli session-expiry error shapes are all classified."""
        variants = [
            "not logged in",
            "session has expired",
            "login expired",
            "authentication required",
            "session timed out",
            "not authenticated",
            "login required",
        ]
        for text in variants:
            err = {"code": -32603, "message": "Internal error", "data": text}
            out = _format_acp_error(err)
            assert "transient error" not in out.lower(), f"Failed for: {text!r}"
            assert "kiro-cli login" in out.lower(), f"No login guidance for: {text!r}"


class TestCompactionFailureDetail:
    """`compaction_failure_detail` is what stops the notice collapsing to
    "unknown error": it prefers a named reason and otherwise surfaces the raw
    shape, redacted, because it lands on a chat row."""

    def test_prefers_a_named_reason(self):
        from kiro_crew.acp.client import compaction_failure_detail

        assert (
            compaction_failure_detail({"status": {"type": "failed", "reason": "too large"}})
            == "too large"
        )

    def test_reads_a_nested_error_object(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {"status": {"type": "failed"}, "error": {"message": "throttled"}}
        )
        assert detail == "throttled"

    def test_falls_back_to_the_raw_shape(self):
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"status": {"type": "failed"}})
        assert "no reason reported" in detail
        assert "failed" in detail

    def test_is_bounded_and_redacted(self):
        from kiro_crew.acp.client import (
            _COMPACTION_DETAIL_MAX_CHARS,
            compaction_failure_detail,
        )

        detail = compaction_failure_detail({"status": {"type": "failed", "error": "x" * 5000}})
        assert len(detail) == _COMPACTION_DETAIL_MAX_CHARS

        secret = compaction_failure_detail(
            {"status": {"type": "failed", "error": "aws_secret_access_key=AKIAIOSFODNN7EXAMPLE"}}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in secret

    def test_rejects_a_placeholder_reason(self):
        """A named reason of "error" is not a reason. KAS's summarization_failed
        frame carried exactly that, which rendered as "Compaction failed: error"
        — no cause on the row and nothing to grep server-side. Falling through
        to the raw shape is strictly more evidence than the word."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail({"kind": "summarization_failed", "error": "error"})
        assert detail != "error"
        assert "no reason reported" in detail

    def test_prefers_the_user_facing_sentence_over_the_machine_reason(self):
        """The nested pair is the shape KAS reports a throttle in. The machine
        reason identifies the fault; the sentence is the one that tells the
        reader what to do about it, so the sentence wins."""
        from kiro_crew.acp.client import compaction_failure_detail

        detail = compaction_failure_detail(
            {
                "kind": "summarization_failed",
                "name": "ModelThrottleError",
                "cause": {"reason": "MODEL_TEMPORARILY_UNAVAILABLE", "httpStatusCode": 500},
                "userFacingSessionErrorMessage": "The model is experiencing high traffic.",
            }
        )
        assert detail == "The model is experiencing high traffic."


class TestKasSummarizationFailureLogging:
    """The KAS branch logs the WHOLE frame so the next failure is debuggable from
    our own logs. That frame carries ``conversationSummary`` — backend-echoed,
    conversation-derived text — so the dump has to be redacted, or a credential a
    user pasted is persisted into gateway.log by the very line added to make the
    failure diagnosable."""

    @staticmethod
    def _bare_handle():
        from kiro_crew.acp.session_handle import AcpSessionHandle

        handle = AcpSessionHandle.__new__(AcpSessionHandle)
        handle._compaction_failed_at = None
        handle.last_compaction_transient = False
        handle._session_id = "sess-test"
        return handle

    def test_the_frame_is_redacted_before_it_reaches_the_log(self, caplog):
        import logging

        handle = self._bare_handle()
        secret = "AKIAIOSFODNN7EXAMPLE"
        update = {
            "sessionUpdate": "session_info_update",
            "_meta": {
                "kiro": {
                    "kind": "summarization_failed",
                    "reason": "MODEL_TEMPORARILY_UNAVAILABLE",
                    "conversationSummary": (
                        f"The user pasted aws_secret_access_key={secret} while debugging."
                    ),
                }
            },
        }

        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
            events = handle._handle_kas_session_info(update)

        assert any("KAS summarization failed" in r.getMessage() for r in caplog.records)
        assert secret not in caplog.text
        # The verdict still lands: redacting the log must not cost the classification.
        assert handle.last_compaction_transient is True
        assert events and events[0].text == "failed"
