"""V3 capability advertisement must not drift from the executable ACP surface."""
from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from kiro_byo.acp_server import AcpServer
from kiro_byo.protocol import initialize_result, runtime_capability_report
from scripts.acp_capability_audit import _dispatch_names, audit_contract

ROOT = Path(__file__).resolve().parents[1]


def test_advertised_extensions_are_real_handlers_not_every_internal_method():
    report, errors = audit_contract(ROOT)
    assert errors == []
    assert report["advertised_extensions"] == ["_session/steer", "_kiro/session/compact"]
    assert report["unadvertised_extension_count"] >= 1
    assert "_kiro/run/retry" in report["unadvertised_crew_and_legacy_extensions"]
    assert "session/fork" not in report["standard_methods"]
    assert report["fork_supported"] is False
    assert report["mcp_interactive_input_supported"] is False


def test_missing_public_extension_handler_fails_release_contract():
    with patch("scripts.acp_capability_audit._dispatch_names", return_value={"initialize", "session/new"}):
        _, errors = audit_contract(ROOT)
    assert any("advertised extension missing" in error for error in errors)
    assert any("standard methods missing handlers" in error for error in errors)


def test_runtime_report_matches_initialized_extensions_without_exposing_legacy():
    initialized = initialize_result()["agentCapabilities"]["_meta"]["kiro"]["extensionMethods"]
    report = runtime_capability_report(native_tools=[], mcp_transports=[])
    assert report["kiro_extensions"] == initialized
    assert "session/fork" not in report["standard_methods"]
    assert "session/set_model" not in report["standard_methods"]
    assert "_kiro.dev/session/terminate" not in initialized


def test_unknown_or_unimplemented_fork_returns_method_not_found(tmp_path, monkeypatch):
    monkeypatch.setenv("KIRO_BYO_HOME", str(tmp_path))
    output = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=output)
    try:
        server.handle({"jsonrpc": "2.0", "id": 51, "method": "session/fork", "params": {"sessionId": "x"}})
        frame = json.loads(output.getvalue().splitlines()[-1])
        assert frame["id"] == 51
        assert frame["error"]["code"] == -32601
    finally:
        server._executor.shutdown(wait=True)
