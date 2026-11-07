"""Fail-closed ACP permission-response checks for an independent BYO Harness."""
from __future__ import annotations

import pytest

from kiro_byo.acp_server import AcpServer
from kiro_byo.acp_session_state import Session


@pytest.mark.parametrize("response", [
    None, [], "allow_once", {}, {"outcome": "selected"},
    {"outcome": []}, {"outcome": {"outcome": "selected"}},
    {"outcome": {"outcome": "selected", "optionId": "allow"}},
    {"outcome": {"outcome": "selected", "optionId": "ALLOW_ONCE"}},
    {"outcome": {"outcome": "selected", "optionId": "reject_once"}},
    {"outcome": {"outcome": "cancelled", "optionId": "allow_once"}},
    {"outcome": {"outcome": "selected", "optionId": "allow_always "}},
])
def test_unadvertised_or_malformed_host_response_cannot_approve(response):
    server = object.__new__(AcpServer)
    session = Session("s1", ".", "model")
    server._emit_tool_call = lambda *_a, **_k: {"toolCallId": "call-1"}
    server._request_client = lambda *_a, **_k: response
    assert server._request_tool_permission_choice(session, "call-1", "shell", {"command": "echo no"}) == ""


@pytest.mark.parametrize("option_id", ["allow_once", "allow_always"])
def test_only_advertised_option_ids_approve(option_id):
    server = object.__new__(AcpServer)
    session = Session("s1", ".", "model")
    server._emit_tool_call = lambda *_a, **_k: {"toolCallId": "call-1"}
    observed = []
    def approve(method, params, **kwargs):
        observed.append((method, params))
        return {"outcome": {"outcome": "selected", "optionId": option_id}}
    server._request_client = approve
    assert server._request_tool_permission_choice(session, "call-1", "shell", {"command": "echo ok"}) == option_id
    assert observed[0][0] == "session/request_permission"
    assert option_id in {o["optionId"] for o in observed[0][1]["options"]}
