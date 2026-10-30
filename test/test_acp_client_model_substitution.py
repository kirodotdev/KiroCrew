"""Tests for ACP client: model-substitution advisories and the session/new re-issue."""

from unittest.mock import MagicMock, patch

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    AcpClient,
    AcpError,
    _is_model_substitution_advisory,
    _substitute_model_from_advisory,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE


class TestSubstitutionAdvisory:
    """The model-substitution advisory (-32603 'Using X instead') is non-fatal.

    claude-agent-acp returns a JSON-RPC -32603 error frame on session/new (and
    related calls) when admin-tier / headless-tier policy substitutes the
    requested model for another. The substitute is already live, so Kiro Crew
    must treat the advisory as a warning and keep the session, NOT raise.
    """

    @pytest.mark.parametrize(
        ("err", "expected"),
        [
            pytest.param(
                {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {
                        "details": (
                            'Model "opus" is restricted by your organization\'s '
                            "settings. Using global.anthropic.claude-sonnet-4-6[1m] "
                            "instead."
                        )
                    },
                },
                True,
                id="advisory_detected",
            ),
            pytest.param(
                {
                    "code": -32603,
                    "message": "Internal error",
                    "data": 'Model "x" is restricted ... Using y instead.',
                },
                True,
                id="advisory_detected_data_is_plain_string",
            ),
            # A genuine -32603 with no substitution wording must NOT be swallowed.
            pytest.param(
                {"code": -32603, "message": "Internal error", "data": "request_id: abc-123"},
                False,
                id="real_internal_error_still_raises",
            ),
            pytest.param(
                {
                    "code": -32603,
                    "message": "ThrottlingException: rate exceeded",
                    "data": "rate limit",
                },
                False,
                id="throttle_not_treated_as_advisory",
            ),
            # Right wording, wrong code -> still raise (only -32603 qualifies).
            pytest.param(
                {
                    "code": -32602,
                    "message": "Invalid params",
                    "data": {"details": 'Model "x" is restricted ... Using y instead'},
                },
                False,
                id="wrong_code_not_advisory",
            ),
            # "is restricted" without the "Using X instead" half must not match.
            pytest.param(
                {
                    "code": -32603,
                    "message": "Internal error",
                    "data": {"details": 'Model "opus" is restricted by your settings.'},
                },
                False,
                id="partial_wording_not_advisory",
            ),
        ],
    )
    def test_advisory_classification(self, err, expected):
        assert _is_model_substitution_advisory(err) is expected

    def test_non_dict_not_advisory(self):
        assert _is_model_substitution_advisory("plain string") is False
        assert _is_model_substitution_advisory(None) is False

    def test_empty_or_missing_data_not_advisory(self):
        assert (
            _is_model_substitution_advisory({"code": -32603, "message": "Internal error"}) is False
        )
        assert _is_model_substitution_advisory({"code": -32603, "data": ""}) is False

    @pytest.mark.asyncio
    async def test_wait_for_response_returns_result_on_advisory(self, tmp_path):
        """_wait_for_response returns the result (no raise) on a substitution advisory."""
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = {"sessionId": "abc-123"}
        msg.method = None
        msg.id = 7

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(7, timeout=1.0)
        assert out == {"sessionId": "abc-123"}

    @pytest.mark.asyncio
    async def test_wait_for_response_raises_on_real_error(self, tmp_path):
        """A genuine -32603 (no substitution wording) still raises AcpError."""
        client = AcpClient(work_dir=tmp_path)
        real_err = {"code": -32603, "message": "Internal error", "data": "request_id: zzz"}
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 9

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        with pytest.raises(AcpError):
            await client._wait_for_response(9, timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_response_records_substitute_on_error_only_advisory(self, tmp_path):
        """Spec-real advisory frame: error set, result is None.

        A spec-compliant JSON-RPC response carries either result or error,
        not both. On the realistic error-only advisory (no co-located result):
          1. ``_wait_for_response`` must NOT raise; it returns ``{}``.
          2. It MUST record the gateway-named substitute model on
             ``self._last_substitution_model`` so the session/new caller can
             adopt it and re-issue creation. Without this, ``_session_id``
             would silently become None and the next call would crash on the
             ``Cannot set config option before session is initialized`` guard.
        """
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = None  # spec-real: error-only frame, no co-located result
        msg.method = None
        msg.id = 11

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(11, timeout=1.0)

        assert out == {}, "advisory must surface as empty dict, not raise"
        assert (
            client._last_substitution_model == "global.anthropic.claude-sonnet-4-6[1m]"
        ), "substitute model must be recorded for the session/new caller to adopt"


class TestSubstituteModelExtractor:
    """_substitute_model_from_advisory pulls the gateway-served model id out of
    the -32603 advisory so the session/new caller can adopt it."""

    @pytest.mark.parametrize(
        ("err", "expected"),
        [
            pytest.param(
                {
                    "code": -32603,
                    "data": {
                        "details": (
                            'Model "opus" is restricted by your organization\'s '
                            "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                        )
                    },
                },
                "global.anthropic.claude-sonnet-4-6[1m]",
                id="extracts_versioned_substitute",
            ),
            pytest.param(
                {
                    "code": -32603,
                    "data": {
                        "details": "Model X is restricted. Using claude-opus-4-6[1m] instead."
                    },
                },
                "claude-opus-4-6[1m]",
                id="extracts_with_trailing_period_and_quotes",
            ),
            pytest.param(
                {"code": -32603, "data": "request_id: abc-123"},
                None,
                id="returns_none_for_real_internal_error",
            ),
            pytest.param(
                {"code": -32602, "data": {"details": "is restricted ... Using y instead"}},
                None,
                id="returns_none_for_wrong_code",
            ),
        ],
    )
    def test_substitute_extraction(self, err, expected):
        assert _substitute_model_from_advisory(err) == expected

    def test_returns_none_for_non_dict(self):
        assert _substitute_model_from_advisory(None) is None
        assert _substitute_model_from_advisory("plain") is None


class TestSubstitutionFollow:
    """session/new returning a substitution advisory carries NO sessionId.
    _new_session_following_substitution must adopt the substitute model and
    re-issue session/new so a real session is actually created -- otherwise the
    next step (set_config_option) crashes on the uninitialized-session guard.
    This is the no-sessionId path the earlier suite did not cover.
    """

    def _advisory_resp(self):
        # Mirrors what _wait_for_response returns on the advisory: empty dict,
        # plus it sets _last_substitution_model as a side effect (simulated here).
        return {}

    @pytest.mark.asyncio
    async def test_adopts_substitute_and_reissues(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        # Avoid touching the real filesystem seeding.
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        # First wait: advisory (no sessionId) + record substitute, as the real
        # _wait_for_response does. Second wait: real session on the substitute.
        calls = {"n": 0}

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            calls["n"] += 1
            if calls["n"] == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
                return {}  # advisory frame -> no sessionId
            return {"sessionId": "real-sess-1"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "real-sess-1"
        # Adopted the gateway-served model and re-seeded settings before retry.
        assert client._model == "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings.assert_called_once()
        # Exactly two session/new issues: the original + one retry (bounded).
        assert sent.count("session/new") == 2

    async def _run_substitution_retry_case(self, tmp_path, monkeypatch, *, lose_surface):
        from kiro_crew import model_registry
        from kiro_crew.acp import seed_provenance as sp

        monkeypatch.setattr(model_registry, "_ADVERTISED_MODELS", {})
        monkeypatch.setattr(sp, "_RECORDS", {})
        monkeypatch.setattr(sp, "_LIVE", {})
        monkeypatch.setattr(sp, "_SHARERS", {})
        monkeypatch.setattr(sp, "_sidecar_path", lambda: tmp_path / "seeds.json")

        owner = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        owner._model = "global.anthropic.claude-opus-4-8[1m]"
        owner._write_claude_local_settings()
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._model = owner._model
        client._write_claude_local_settings()
        assert client._claude_settings_shared is True
        assert client._permission_surface_governed is True

        server = {
            "name": "governed",
            "command": "/bin/governed",
            "args": [],
            "env": [],
            "type": "stdio",
        }
        client._session_mcp_cache = [server]
        client._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("old", "old")
        resolve_calls = []

        def _resolve():
            resolve_calls.append(client._permission_surface_governed)
            client._session_mcp_snapshot = acp_client.DerivedSpecSnapshot("new", "new")
            return [server] if client._permission_surface_governed else []

        client._resolve_session_mcp_servers = _resolve  # type: ignore[assignment]
        report_calls = []
        guard_calls = []
        client._begin_session_report = (  # type: ignore[assignment]
            lambda servers: report_calls.append(list(servers or []))
        )
        client._guard_unresolved_mcp_refs = (  # type: ignore[assignment]
            lambda wire_servers: guard_calls.append(list(wire_servers or []))
        )
        sent = []
        settings = tmp_path / ".claude" / "settings.local.json"

        async def _send(method, params):
            sent.append(list(params.get("mcpServers") or []))
            if len(sent) == 1 and lose_surface:
                # A link: the one shape that can be neither governed nor left out
                # of the session's setting sources.
                settings.unlink()
                settings.symlink_to(tmp_path / "elsewhere.json")
            return len(sent)

        waits = 0

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            nonlocal waits
            waits += 1
            if waits == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
                return {}
            return {"sessionId": "retry-session"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        response = await client._new_session_following_substitution()
        assert response == {"sessionId": "retry-session"}
        assert sent[0] == [server]
        return sent[1], server, resolve_calls, report_calls, guard_calls

    @pytest.mark.asyncio
    async def test_substitution_retry_withholds_mcp_when_surface_lost(self, tmp_path, monkeypatch):
        retried, _server, resolves, reports, guards = await self._run_substitution_retry_case(
            tmp_path, monkeypatch, lose_surface=True
        )
        assert retried == []
        assert resolves == [False]
        assert reports == [[_server], []]
        assert guards == [[_server], []]

    @pytest.mark.asyncio
    async def test_substitution_retry_keeps_mcp_when_surface_intact(self, tmp_path, monkeypatch):
        retried, server, resolves, reports, guards = await self._run_substitution_retry_case(
            tmp_path, monkeypatch, lose_surface=False
        )
        assert retried == [server]
        assert resolves == [True]
        assert reports == [[server], [server]]
        assert guards == [[server], [server]]

    @pytest.mark.asyncio
    async def test_happy_path_no_retry(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"sessionId": "sess-ok"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "sess-ok"
        # No substitution -> no re-seed, single session/new.
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("model", "substitute"),
        [
            # Advisory fired but substitute could not be parsed (None) -> we do NOT
            # retry; caller gets an empty resp and raises a clear error upstream.
            pytest.param(
                "global.anthropic.claude-opus-4-8[1m]",
                None,
                id="no_infinite_retry_when_substitute_unparseable",
            ),
            # Defensive: if the gateway "substitutes" to the model we already asked
            # for, don't loop -- treat as terminal (no retry).
            pytest.param(
                "global.anthropic.claude-sonnet-4-6[1m]",
                "global.anthropic.claude-sonnet-4-6[1m]",
                id="no_retry_when_substitute_same_as_current",
            ),
        ],
    )
    async def test_terminal_advisory_is_not_retried(self, tmp_path, model, substitute):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = model
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            client._last_substitution_model = substitute
            return {}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") is None
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1


class TestSubstitutionWrappersAndRedaction:
    """Pin the wrapper raise paths and the dual-redaction discipline applied to
    backend-derived strings before they hit logger sinks (which fan out to the
    dashboard activity feed and Slack).
    """

    @pytest.mark.asyncio
    async def test_initialize_session_raises_on_no_sessionid(self, tmp_path):
        """If the helper still returns no sessionId after the substitution-follow
        retry, _initialize_session raises AcpError loudly instead of letting the
        next handshake step crash on the opaque "Cannot set config option before
        session is initialized" guard.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session(*args, **kwargs):
            return {}

        client._new_session_following_substitution = _no_session  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "session/new returned no sessionId" in str(excinfo.value)
        assert "the backend did not create a session" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_omits_substitute_when_unchanged(self, tmp_path):
        """When the helper returns no sessionId and self._model is unchanged
        (no substitution happened), the AcpError text MUST NOT include the
        "even after adopting substitute model" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_no_substitution(*args, **kwargs):
            # Helper returned without changing self._model -> no substitution.
            return {}

        client._new_session_following_substitution = _no_session_no_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_includes_substitute_when_changed(self, tmp_path):
        """When the helper adopted a substitute (self._model != model_before),
        the AcpError text MUST include the "even after adopting substitute
        model {...}" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_substitution(*args, **kwargs):
            # Helper adopted the gateway-served substitute before giving up.
            client._model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._new_session_following_substitution = _no_session_with_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" in str(excinfo.value)
        assert "claude-sonnet-4-6" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_new_session_following_substitution_redacts_log_payload(self, tmp_path):
        """The substitute model logged in _new_session_following_substitution
        MUST flow through redact_exfiltration_urls + redact_credentials before
        reaching logger.warning. Drives the helper with a substitute that
        contains a URL-shaped token and asserts the emitted log payload was
        redacted.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        # Inject a substitute that embeds an AWS access-key-shaped substring
        # so the dual redactor heuristic actually fires (redact_exfiltration_urls
        # matches the URL because the query carries an AKIA token, and
        # redact_credentials matches the standalone AKIA pattern). A bare
        # arbitrary URL is too tame for the heuristic and would leave the test
        # asserting on a no-op pass-through.
        url_shaped = "http://attacker.example.com/exfil?token=AKIAIOSFODNN7EXAMPLE"

        sent: list[str] = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        wait_calls = {"n": 0}

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                client._last_substitution_model = url_shaped
                return {}  # no sessionId on first call
            return {"sessionId": "real-sess"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._new_session_following_substitution()

        # Find the warning call that mentions the substitution adoption.
        adopt_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and "adopting gateway-served model" in c.args[0]
        ]
        assert adopt_calls, "expected an 'adopting gateway-served model' warning"
        # The redacted payload is the second positional argument.
        emitted = adopt_calls[0].args[1]
        # The credential MUST be gone. The URL MUST be replaced with the
        # redactor diagnostic marker. The redactor preserves the host name
        # inside "[REDACTED: suspicious URL to <host>]" so operators can
        # investigate the leak source -- that is BY DESIGN, not a bypass.
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/exfil?token=" not in emitted
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_redacts_model_in_info_log(self, tmp_path):
        """The "ACP session created: ... (model=...)" info log MUST flow
        self._model through dual-redaction. Drive _initialize_session through
        the happy path with a URL-shaped self._model and confirm the emitted
        info log does not leak it.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        # AWS access-key-shaped substring guarantees both redactors fire (URL
        # query matches _EXFIL_PATTERNS; standalone key matches _CREDENTIAL_PATTERNS).
        client._model = "http://attacker.example.com/m?token=AKIAIOSFODNN7EXAMPLE"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _ok_session(*args, **kwargs):
            return {"sessionId": "sess-ok"}

        client._new_session_following_substitution = _ok_session  # type: ignore[assignment]
        client._capture_available_models = MagicMock()  # type: ignore[assignment]
        client._store_session_config = MagicMock()  # type: ignore[assignment]

        async def _drain():
            return None

        client._drain_notifications = _drain  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._initialize_session()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "ACP session created" in c.args[0]
        ]
        assert info_calls, "expected an 'ACP session created' info log"
        # Format: logger.info("ACP session created: %s (model=%s)", sid, model_log)
        # The third positional arg is the redacted model.
        emitted_model = info_calls[0].args[2]
        # See sibling test for the design note: redactor keeps the host as a
        # diagnostic label inside "[REDACTED: suspicious URL to <host>]".
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted_model
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted_model
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/m?token=" not in emitted_model
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_acperror_redacts_substitute_model(self, tmp_path):
        """The AcpError raised on no-sessionId-after-substitution MUST flow
        self._model through dual-redaction before interpolating it into the
        message. AcpError propagates to the dashboard activity feed and Slack
        via the same path as the logger sinks, so the redaction discipline
        applies to exception messages too -- not just logger.* calls.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0, *, method="", expected_mcp=None):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_credshaped(*args, **kwargs):
            # Helper adopted a credential-shaped substitute then gave up without
            # producing a sessionId. Drives the "even after adopting substitute
            # model X" branch where X is credential-shaped.
            client._model = "AKIAIOSFODNN7EXAMPLE"
            return {}

        client._new_session_following_substitution = _no_session_with_credshaped  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()

        msg = str(excinfo.value)
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in msg
        ), "AWS access-key-shaped substitute MUST be redacted before reaching the AcpError message"
        assert "even after adopting substitute model" in msg

    @pytest.mark.asyncio
    async def test_wait_for_response_catchall_raise_redacts_msg_error(self, tmp_path):
        """The catch-all `raise AcpError(f"JSON-RPC error: {msg.error}")` MUST
        flow msg.error through dual-redaction before f-string-interpolating it
        into the exception message.
        """
        client = AcpClient(work_dir=tmp_path)
        real_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "request_id: AKIAIOSFODNN7EXAMPLE backend trace failed",
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 13

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(13, timeout=1.0)

        emitted = str(excinfo.value)
        assert "AKIAIOSFODNN7EXAMPLE" not in emitted, (
            "AWS access-key-shaped substring in msg.error MUST be redacted "
            "before reaching the catch-all AcpError message"
        )
        assert "JSON-RPC error" in emitted, "the catch-all AcpError prefix MUST still be present"
