"""``hooks.auto_deny_tools`` in the ``@server/tool`` spelling binds to an ``mcp__`` title.

A harness that sends no ``_meta.kiro`` block (claude) names an MCP call only in its
title, ``mcp__<server>__<tool>``, so the call reaches the gate with no server. The
``mcp-auto-deny`` tier reads that title through the governance split and denies it
when an operator's ``@server/tool``, ``@server`` or ``Running: @server/*`` rule names
it. The reading is deny-only: the grant tiers never see it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import _dispatch
from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    hook_gate_kwargs,
)

_CLAUDE_MCP_CAPTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "claude" / "mcp-stdio-mount-live.jsonl"
)


@pytest.mark.parametrize("rule", ["@atlassian/delete*", "@atlassian", "Running: @atlassian/*"])
def test_an_identity_spelled_rule_denies_the_bare_mcp_title(rule):
    result = HookManager(HooksConfig(auto_deny_tools=[rule])).on_tool_call(
        "mcp__atlassian__deleteJiraIssue"
    )
    assert result.action == TOOL_DENY


def test_a_tool_level_rule_leaves_a_sibling_tool_alone():
    manager = HookManager(HooksConfig(auto_deny_tools=["@atlassian/delete*"]))
    assert manager.on_tool_call("mcp__atlassian__getJiraIssue").action == TOOL_ALLOW


def test_another_servers_rule_does_not_bind():
    manager = HookManager(HooksConfig(auto_deny_tools=["@github/*"]))
    assert manager.on_tool_call("mcp__atlassian__deleteJiraIssue").action == TOOL_ALLOW


def test_a_server_name_with_double_underscores_splits_on_the_last_one():
    # The governance split: the tool is the last ``__`` segment.
    manager = HookManager(HooksConfig(auto_deny_tools=["@npm__playwright/click"]))
    assert manager.on_tool_call("mcp__npm__playwright__click").action == TOOL_DENY


def test_a_title_with_no_server_is_not_read_as_an_identity():
    manager = HookManager(HooksConfig(auto_deny_tools=["@/x"]))
    assert manager.on_tool_call("mcp____x").action == TOOL_ALLOW


def test_the_title_reading_grants_nothing():
    manager = HookManager(HooksConfig(auto_approve_tools=["@atlassian/*"]))
    assert manager.on_tool_call("mcp__atlassian__deleteJiraIssue").action != TOOL_AUTO_APPROVE


def _claude_mcp_permission_event():
    caches: dict = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
        "tool_input_redacted_cache": {},
        "diff_path_cache": {},
        "harness_tool_name_cache": {},
    }
    for line in _CLAUDE_MCP_CAPTURE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        frame = json.loads(line)
        params = frame.get("params") or {}
        if frame.get("method") == "session/update":
            _dispatch.parse_session_update(params.get("update") or {}, **caches)
        elif frame.get("method") == "session/request_permission":
            msg = JsonRpcMessage(id=frame["id"], method=frame["method"], params=params)
            event, _ = _dispatch.build_permission_event(
                msg, **caches, harness_backend=ACP_BACKEND_CLAUDE
            )
            assert event is not None and event.kind == EVENT_PERMISSION_REQUEST
            return event
    raise AssertionError(f"no permission request in {_CLAUDE_MCP_CAPTURE.name}")


def test_a_live_claude_mcp_permission_request_is_denied_by_a_server_rule():
    event = _claude_mcp_permission_event()
    assert event.title == "mcp__crew-probe__crew_probe_echo"
    for rule, expected in (("@crew-probe/*", TOOL_DENY), ("@other/*", TOOL_ALLOW)):
        result = HookManager(HooksConfig(auto_deny_tools=[rule])).on_tool_call(
            event.title, **hook_gate_kwargs(event)
        )
        assert result.action == expected, rule
