"""Tests for ACP client: session initialisation, response waiting, notification draining and the
advertised model list.
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import _DRAIN_DURATION, _DRAIN_IDLE_EXIT, AcpClient, AcpError
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

# ── Coverage push: session lifecycle ──


class TestInitializeSession:
    """Tests for _initialize_session covering new session, resume, and model set."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so session-file writes are isolated.

        Without this, tests pollute the real ~/.kiro/sessions/cli/ on the
        build host and collide with parallel runs (hardcoded session names).
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _make_client(self, tmp_path, **kwargs):
        client = AcpClient(work_dir=tmp_path, **kwargs)
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._next_req_id = MagicMock(side_effect=range(1, 100))
        return client

    @pytest.mark.asyncio
    async def test_new_session_basic(self, tmp_path):
        """Happy path: initialize → session/new → set_mode → drain."""
        client = self._make_client(tmp_path)
        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {}},
            2: {"sessionId": "sess-abc"},
        }

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert client._session_id == "sess-abc"
        assert client._resumed is False
        client._drain_notifications.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_resume_success(self, tmp_path):
        """session/load succeeds when file exists and kiro-cli supports it."""
        client = self._make_client(tmp_path)
        client._resume_session_id = "old-sess"

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "old-sess.json"
        session_file.write_text("{}")

        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
            2: {"modes": ["chat"]},  # load success
        }

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "old-sess"
            assert client._resumed is True
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_session_resume_fallback_to_new(self, tmp_path):
        """session/load fails → falls back to session/new."""
        from kiro_crew.acp.client import AcpError

        client = self._make_client(tmp_path)
        client._resume_session_id = "bad-sess"

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "bad-sess.json"
        session_file.write_text("{}")

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            if call_idx[0] == 2:
                raise AcpError("session not found")
            if call_idx[0] == 3:
                return {"sessionId": "new-sess"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "new-sess"
            assert client._resumed is False
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_cc_resume_skips_load_when_transcript_missing(self, tmp_path):
        """claude backend: a stale persisted sid with NO transcript on disk
        must fall back to session/new (a fresh start), not replay via
        session/load. Guards against the ~38%-on-'hi' base-context bloat."""
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = self._make_client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "ghost-sess"  # no transcript exists for it

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            # session/load must NOT be called; the next request is session/new.
            return {"sessionId": "fresh-sess"}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()
        assert client._session_id == "fresh-sess"
        assert client._resumed is False

    @pytest.mark.asyncio
    async def test_set_model_when_non_default(self, tmp_path):
        """Non-default model triggers set_model request."""
        client = self._make_client(tmp_path)
        client._model = "claude-sonnet"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        # Track all send_request calls
        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" in send_calls

    @pytest.mark.asyncio
    async def test_no_set_model_when_auto(self, tmp_path):
        """Default model 'auto' skips set_model request."""
        client = self._make_client(tmp_path)
        client._model = "auto"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" not in send_calls


# ── Coverage push: JSON-RPC plumbing ──


class TestWaitForResponse:
    """Tests for _wait_for_response covering matching, buffering, and errors."""

    @pytest.mark.asyncio
    async def test_matching_response_returned(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, result={"data": "ok"})
        client._read_message = AsyncMock(return_value=msg)

        result = await client._wait_for_response(5, timeout=5.0)
        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_error_response_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, error={"code": -1, "message": "fail"})
        client._read_message = AsyncMock(return_value=msg)

        with pytest.raises(AcpError, match="JSON-RPC error"):
            await client._wait_for_response(5, timeout=5.0)

    @pytest.mark.asyncio
    async def test_notification_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        notif = JsonRpcMessage(method="mcp/serverReady", params={"name": "builder"})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[notif, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}
        assert len(client._mcp_notifications) == 1

    @pytest.mark.asyncio
    async def test_non_matching_response_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        other = JsonRpcMessage(id=99, result={"other": True})
        target = JsonRpcMessage(id=3, result={"target": True})
        client._read_message = AsyncMock(side_effect=[other, target])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"target": True}
        assert len(client._buffer) == 1

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpTimeoutError):
            await client._wait_for_response(1, timeout=0.1)

    @pytest.mark.asyncio
    async def test_shutdown_event_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with patch("kiro_crew.shutdown_event") as mock_ev:
            mock_ev.is_set.return_value = True
            with pytest.raises(AcpError, match="Shutdown"):
                await client._wait_for_response(1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_server_request_warned_and_dropped(self, tmp_path):
        """Unexpected server request (method + id) is warned and dropped."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        server_req = JsonRpcMessage(id=50, method="unexpected/request", params={})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[server_req, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}


class TestDrainNotifications:
    """Tests for _drain_notifications covering buffered and live messages."""

    @pytest.mark.asyncio
    async def test_drains_buffered_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "slack-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert len(client._mcp_notifications) == 0

    @pytest.mark.asyncio
    async def test_drains_live_messages(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        live_msg = JsonRpcMessage(method="mcp/serverReady", params={"name": "core"})
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return live_msg
            return None

        client._read_message = fake_read
        recorded = []
        client._mcp_report.record_frame = lambda msg, owned: recorded.append((msg, owned))

        await client._drain_notifications(duration=0.2)

        assert recorded == [(live_msg, True)]
        # Should have processed the live message without error

    @pytest.mark.asyncio
    async def test_handles_acp_error_during_drain(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []
        client._read_message = AsyncMock(side_effect=AcpError("process died"))

        assert await client._drain_notifications(duration=0.1) is None
        # The error ends the drain; it does not keep reading a dead process.
        assert client._read_message.await_count == 1
        assert client._mcp_notifications == []

    @pytest.mark.asyncio
    async def test_captures_buffered_mcp_oauth_request(self, tmp_path):
        """OAuth notifications buffered during init are captured into pending list."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "linear"
        assert pending[0]["oauthUrl"] == url
        # Drained — second pop returns empty.
        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_captures_live_mcp_oauth_request(self, tmp_path):
        """OAuth notifications arriving live during drain are also captured."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://auth.smithery.ai/neon/authorize?client_id=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "neon", "oauthUrl": url},
        )
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return oauth_msg
            return None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.5)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "neon"
        assert pending[0]["oauthUrl"] == url

    @pytest.mark.asyncio
    async def test_oauth_request_without_url_not_captured(self, tmp_path):
        """Malformed oauth notifications (no oauthUrl) are ignored."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "broken"},  # no oauthUrl key
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_oauth_request_dedupes_per_server(self, tmp_path):
        """kiro-cli may emit oauth_request multiple times per server probe — dedupe."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1, "duplicate oauth_request for same server should be collapsed"

    @pytest.mark.asyncio
    async def test_unsafe_url_does_not_consume_dedupe_slot(self, tmp_path):
        """Unsafe-scheme URL must be rejected *before* the dedupe key is recorded,
        so a later safe retry for the same server still surfaces."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": "javascript:alert(1)"},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize",
                },
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["oauthUrl"] == "https://mcp.linear.app/authorize"

    @pytest.mark.asyncio
    async def test_oauth_request_with_empty_server_name_dropped(self, tmp_path):
        """server_initialized/server_init_failure discard by server_name only —
        recording a banner with empty server_name would create a permanently-
        stuck dedupe entry.  Drop instead."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "", "oauthUrl": "https://example.com/auth"},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []
        assert client._oauth_emitted_servers == set()


class TestCaptureAvailableModels:
    """Capturing the backend-advertised model list from session responses."""

    def _client(self):
        return AcpClient(acp_backend=ACP_BACKEND_CLAUDE)

    def test_captures_versioned_models(self):
        c = self._client()
        c._capture_available_models(
            {
                "sessionId": "s",
                "models": {
                    "currentModelId": "claude-opus-4-8-1m",
                    "availableModels": [
                        {"modelId": "claude-opus-4-8-1m", "name": "Opus 4.8", "description": "new"},
                        {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
                    ],
                },
            }
        )
        am = c.available_models()
        assert [m["modelId"] for m in am] == ["claude-opus-4-8-1m", "claude-sonnet-4-6"]
        assert am[1]["description"] == ""  # missing description -> empty string
        assert c._resolved_model_id == "claude-opus-4-8-1m"

    def test_current_model_id_defaults_to_none(self):
        c = self._client()
        assert c._resolved_model_id is None

    def test_current_model_id_not_overwritten_when_absent(self):
        """A later session/new (or session/load) without currentModelId (e.g.
        a minimal/degenerate response) must not clobber an already-resolved
        model id — _track_metadata's window resolution should keep working."""
        c = self._client()
        c._capture_available_models(
            {"models": {"currentModelId": "claude-opus-4.6", "availableModels": []}}
        )
        assert c._resolved_model_id == "claude-opus-4.6"
        c._capture_available_models({"models": {"availableModels": []}})
        assert c._resolved_model_id == "claude-opus-4.6"

    def test_no_models_key_leaves_empty(self):
        c = self._client()
        c._capture_available_models({"sessionId": "s"})
        assert c.available_models() == []

    def test_entries_without_modelid_skipped(self):
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"name": "x"}, {"modelId": "ok", "name": "OK"}]}}
        )
        assert [m["modelId"] for m in c.available_models()] == ["ok"]

    def test_value_field_accepted_as_model_id(self):
        # ACP config-option shape uses "value" rather than "modelId".
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"value": "m1", "name": "M1"}]}}
        )
        assert c.available_models()[0]["modelId"] == "m1"

    def test_capture_matches_canonical_parser(self):
        """Drift-pin: the client-side capture normalizes exactly like
        ``parse_advertised_models`` — hard-coded expectation so a regression
        inside the canonical parser (name fallback, description default,
        value-as-id, non-dict skip) fails this pin too."""
        resp = {
            "models": {
                "currentModelId": "m1",
                "availableModels": [
                    {"modelId": "m1", "name": "M1", "description": "d"},
                    {"value": "m2"},
                    {"name": "no id — skipped"},
                    "not-a-dict",
                ],
            }
        }
        c = self._client()
        c._capture_available_models(resp)
        assert c.available_models() == [
            {"modelId": "m1", "name": "M1", "description": "d"},
            {"modelId": "m2", "name": "m2", "description": ""},
        ]

    def test_capture_delegates_to_canonical_parser(self, monkeypatch):
        """Anti-re-fork pin: the list must be SOURCED from
        ``parse_advertised_models`` AND called with the gated envelope
        ``{"models": models}`` — a restored inline walk, a whole-response
        re-resolution, or a wrong envelope all fail this pin.

        Depends on the function-local import in ``_capture_available_models``;
        if that import is ever hoisted to module scope, patch
        ``kiro_crew.acp.client.parse_advertised_models`` instead.
        """
        from kiro_crew.acp import session_handle as sh

        sentinel = [
            {"modelId": "sentinel-a", "name": "A", "description": ""},
            {"modelId": "sentinel-b", "name": "B", "description": ""},
        ]
        calls: list = []

        def _fake(resp):
            calls.append(resp)
            return list(sentinel)

        monkeypatch.setattr(sh, "parse_advertised_models", _fake)
        c = self._client()
        c._capture_available_models({"models": {"availableModels": [{"modelId": "real"}]}})
        assert c.available_models() == sentinel
        assert calls == [{"models": {"availableModels": [{"modelId": "real"}]}}]

    def test_malformed_later_response_keeps_prior_snapshot(self):
        """The non-empty assignment guard survives the consolidation: a later
        malformed response must not clear an already-captured list."""
        c = self._client()
        c._capture_available_models({"models": {"availableModels": [{"modelId": "m1"}]}})
        assert [m["modelId"] for m in c.available_models()] == ["m1"]
        c._capture_available_models({"models": {"availableModels": "nope"}})
        assert [m["modelId"] for m in c.available_models()] == ["m1"]

    def test_empty_models_object_ignores_top_level_available_models(self):
        """An EMPTY (falsy) ``models`` object must not let the parser's
        dict-or-list fallback source the list from a top-level
        ``availableModels`` key the dict gate never saw."""
        c = self._client()
        c._capture_available_models({"models": {}, "availableModels": [{"modelId": "x"}]})
        assert c.available_models() == []


class TestAcpClientDrainEarlyExit:
    """_drain_notifications exits early once MCP servers go quiet, bounded by
    the hard cap. Guards the TTFT optimization (idle early-exit) against
    regressing into either a full-cap wait or a premature exit while active."""

    def test_idle_exit_constant_below_duration_constant(self):
        # The idle early-exit only short-circuits the drain if it can fire
        # before the hard cap. If a future edit bumps _DRAIN_IDLE_EXIT to or
        # above _DRAIN_DURATION, the cap always fires first, the idle path
        # becomes dead code, and cold-start TTFT silently regresses to the full
        # cap. The default-bound drain tests pass explicit duration/idle_exit
        # args, so only this assertion guards the module constants themselves.
        assert _DRAIN_IDLE_EXIT < _DRAIN_DURATION, (
            "_DRAIN_IDLE_EXIT must stay strictly below _DRAIN_DURATION "
            "to keep the idle early-exit reachable"
        )

    @pytest.mark.asyncio
    async def test_drain_exits_early_when_quiet(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Pre-buffered notification (arrived during _wait_for_response) — exercises
        # the buffered-drain path in addition to the live-read path (CR feedback).
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "buffered-mcp"}
            ),
        ]

        # Two live MCP notifications, then quiet (None) forever.
        msgs = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "builder-mcp"}
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/server_initialized", params={"name": "node"}),
        ]
        calls = {"n": 0}

        async def fake_read(timeout):
            if calls["n"] < len(msgs):
                m = msgs[calls["n"]]
                calls["n"] += 1
                return m
            await asyncio.sleep(timeout)  # honor the poll timeout, return nothing
            return None

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        # Hard cap 10s; idle window 0.2s for a fast test.
        await client._drain_notifications(duration=10.0, idle_exit=0.2)
        elapsed = time.monotonic() - start

        # Tight window proves it was the IDLE exit that fired (~0.2s after the last
        # live message), not the 10s cap NOR an instant exit from a swallowed
        # exception / always-None read (CR feedback). Floor guards against the
        # idle logic being deleted; ceiling guards against the full-cap wait.
        # flake-ok: the idle-exit window is the subject here: the floor proves the 0.2 s idle exit fired and the ceiling that the 10 s cap did not; a virtual clock is tracked for a de-flake pass
        assert 0.15 < elapsed < 1.0, f"expected ~0.2s idle exit, got {elapsed:.2f}s"
        assert calls["n"] == 2  # both LIVE notifications were drained
        # The buffered notification is consumed + the buffer cleared by the drain.
        assert client._mcp_notifications == []

    @pytest.mark.asyncio
    async def test_drain_respects_hard_cap_when_chatty(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []

        # A server that never goes quiet — the cap must bound the wait.
        reads = {"n": 0}

        async def fake_read(timeout):
            reads["n"] += 1
            return JsonRpcMessage(method="_kiro.dev/mcp/progress", params={"name": "slow"})

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        await client._drain_notifications(duration=0.5, idle_exit=5.0)
        elapsed = time.monotonic() - start

        # Idle never triggers (always chatty), so the 0.5s cap bounds it.
        # flake-ok: the hard cap is the subject here: the floor proves the 0.5 s cap was waited out and the ceiling that it bounded the chatty stream; a virtual clock is tracked for a de-flake pass
        assert 0.4 < elapsed < 2.0, f"hard cap not respected (took {elapsed:.2f}s)"
        # Confirm the drain loop actually iterated (processed reads), not just
        # that the timer expired (CR feedback).
        assert reads["n"] > 0, "drain loop never read a message during the cap window"
