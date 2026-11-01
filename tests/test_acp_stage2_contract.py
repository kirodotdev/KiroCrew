"""Stage 2: ACP state-change safety and capability honesty.

Tests run offline against the real message dispatcher; no Crew data or API key is used.
"""
from __future__ import annotations

import json
from io import StringIO
from tempfile import TemporaryDirectory
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session
from kiro_byo.acp_conformance import STANDARD_REQUEST_METHODS, STANDARD_NOTIFICATION_METHODS
from kiro_byo.config import KiroByoConfig, ModelSpec, ProviderSpec
from kiro_byo.protocol import initialize_result


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # ACP tests must never create trace/session data in the source checkout.
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))


def request(server: AcpServer, output: StringIO, id: int, method: str, **params: object) -> dict:
    server.handle({"jsonrpc": "2.0", "id": id, "method": method, "params": params})
    return next(json.loads(line) for line in reversed(output.getvalue().splitlines()) if json.loads(line).get("id") == id)


def _cfg() -> KiroByoConfig:
    model = ModelSpec(provider_id="test", provider_name="Test", model_id="model", name="Test Model")
    provider = ProviderSpec(id="test", name="Test", type="openai-compatible",
                            base_url="https://example.invalid/v1", api_key="", models=(model,))
    return KiroByoConfig(providers=(provider,), default_model="Test Model")


def test_legacy_set_model_must_not_report_success_for_unknown_session_or_missing_id():
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    try:
        unknown = request(server, out, 1, "session/set_model", sessionId="missing", modelId="test/model")
        assert unknown["error"]["code"] == -32602
        session = Session("known", str(Path.cwd()), "test/model")
        server._put_session(session)
        missing = request(server, out, 2, "session/set_model", sessionId="known")
        assert missing["error"]["code"] == -32602
        assert session.model == "test/model"
    finally:
        server._executor.shutdown(wait=True)


@pytest.mark.parametrize("method,payload", [
    ("session/set_model", {"modelId": "test/model"}),
    ("session/set_config_option", {"configId": "model", "value": "test/model"}),
])
def test_model_changes_fail_closed_during_active_turn(method: str, payload: dict):
    with TemporaryDirectory() as td, patch.dict("os.environ", {"KIRO_BYO_HOME": td}):
        out = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=out)
        session = Session("known", td, "test/model")
        server._put_session(session)
        session.turn_lock.acquire()
        try:
            with patch.object(server, "_config", return_value=_cfg()):
                answer = request(server, out, 5, method, sessionId="known", **payload)
            assert answer["error"]["code"] == -32002
            assert session.model == "test/model"
            assert not server._session_path("known").exists()
        finally:
            session.turn_lock.release()
            server._executor.shutdown(wait=True)


def test_legacy_set_model_accepts_valid_concrete_selection():
    with TemporaryDirectory() as td, patch.dict("os.environ", {"KIRO_BYO_HOME": td}):
        out = StringIO()
        server = AcpServer(stdin=StringIO(), stdout=out)
        session = Session("known", td, "kiro-byo/echo")
        server._put_session(session)
        try:
            with patch.object(server, "_config", return_value=_cfg()):
                answer = request(server, out, 4, "session/set_model", sessionId="known", modelId="test/model")
            assert answer["result"] == {}
            assert session.model == "Test Model"
            assert server._session_path("known").exists()
        finally:
            server._executor.shutdown(wait=True)


def test_initialize_capabilities_do_not_claim_unsupported_fork_or_interactive_mcp():
    caps = initialize_result()["agentCapabilities"]
    assert "fork" not in caps["sessionCapabilities"]
    assert "sse" not in caps["mcpCapabilities"]
    assert "session/prompt" in STANDARD_REQUEST_METHODS
    assert STANDARD_NOTIFICATION_METHODS == {"session/cancel"}
