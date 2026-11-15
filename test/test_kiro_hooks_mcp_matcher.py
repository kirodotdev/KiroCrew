"""kiro-cli's MCP matcher forms reach the spec kiro-cli runs.

kiro-cli matches an MCP tool with ``@server`` (every tool of a server) or
``@server/tool``. A config hook with either matcher is kept when the spec is
written, so the hook runs. These tests pin all three kiro-cli
paths (the config object form, the config array form and an autoimport header)
and the rules that stay: anything outside the two forms is still refused, and
hooks Crew fires itself keep their own rule.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import agent as agent_mod
from kiro_crew.agent import _apply_user_kiro_hooks, _autoimport_kiro_hooks
from kiro_crew.agent_sdk import spec_hooks

MCP_MATCHERS = (
    "@slack-mcp",
    "@aws-outlook-mcp/email_send",
    "@slack-mcp/post_*",
    "@git.v2/status",
    # An app's bridge is keyed <app>:<server> in the spec.
    "@action-radar:ledger",
    "@action-radar:ledger/radar_find",
)
REFUSED = (
    "@",
    "@:srv",
    "@/tool",
    "@srv/",
    "@srv/a/b",
    "@srv tool",
    "@.*",
    "srv/tool",
    "@srv;rm",
    "@" + "a" * 200,
)


def _hook_script(tmp_path: Path, name: str = "hook.sh", body: str = "exit 0\n") -> str:
    script = tmp_path / "hooks" / name
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    return str(script)


def _apply(kiro_hooks: object) -> dict:
    config: dict = {"hooks": {}}
    _apply_user_kiro_hooks(
        config, {"agent": {"kiro_hooks": kiro_hooks, "kiro_hooks_autoimport": False}}
    )
    return config["hooks"]


@pytest.fixture()
def sel_calls(monkeypatch) -> list[tuple[str, str, str]]:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        agent_mod,
        "_sel_hook_rejected",
        lambda event, command, reason: calls.append((event, command, reason)),
    )
    return calls


@pytest.mark.parametrize("matcher", MCP_MATCHERS)
def test_object_form_keeps_an_mcp_matcher(tmp_path: Path, matcher: str, sel_calls) -> None:
    command = _hook_script(tmp_path)
    hooks = _apply({"postToolUse": [{"matcher": matcher, "command": command}]})
    assert hooks == {"postToolUse": [{"command": command, "matcher": matcher}]}
    assert sel_calls == []


@pytest.mark.parametrize("matcher", MCP_MATCHERS)
def test_array_form_keeps_an_mcp_matcher(tmp_path: Path, matcher: str) -> None:
    command = _hook_script(tmp_path)
    document = {
        "name": "notify",
        "trigger": "PostToolUse",
        "matcher": matcher,
        "action": {"type": "command", "command": command},
    }
    assert _apply([document]) == {"postToolUse": [{"command": command, "matcher": matcher}]}


@pytest.mark.parametrize("matcher", MCP_MATCHERS)
def test_autoimport_header_keeps_an_mcp_matcher(tmp_path: Path, matcher: str) -> None:
    _hook_script(tmp_path, body=f"# event: postToolUse\n# matcher: {matcher}\nexit 0\n")
    result = _autoimport_kiro_hooks(tmp_path / "hooks")
    assert [entry["matcher"] for entry in result["postToolUse"]] == [matcher]


@pytest.mark.parametrize("matcher", REFUSED)
def test_other_shapes_are_still_refused(tmp_path: Path, matcher: str, sel_calls) -> None:
    command = _hook_script(tmp_path)
    assert (
        _apply({"postToolUse": [{"matcher": matcher, "command": command}]}).get("postToolUse", [])
        == []
    )
    assert sel_calls == [("postToolUse", command, "invalid matcher")]


@pytest.mark.parametrize("matcher", ("execute_bash", "*", "fs_write", "mcp__srv__tool"))
def test_the_existing_charset_still_passes(matcher: str) -> None:
    from kiro_crew.agent_materialization.kiro_hooks import _kiro_cli_matcher_ok

    assert _kiro_cli_matcher_ok(matcher)


def test_hooks_crew_fires_keep_their_rule() -> None:
    # This change widens only the materialized spec's rule; the spec-hook rule
    # still takes both MCP forms and still refuses a regex.
    assert spec_hooks._matcher_ok("@slack-mcp")
    assert spec_hooks._matcher_ok("@slack-mcp/post_message")
    assert not spec_hooks._matcher_ok("@.*")


def test_hooks_page_lists_an_unsent_document_with_an_mcp_matcher(tmp_path: Path) -> None:
    # Install accepts the matcher, so the Hooks page must judge the same document
    # by the same rule and list it as not sent (here: disabled).
    from kiro_crew.agent_materialization.kiro_hooks import unsent_hook_documents

    command = _hook_script(tmp_path)
    document = {
        "name": "notify",
        "trigger": "PostToolUse",
        "matcher": "@slack-mcp",
        "enabled": False,
        "action": {"type": "command", "command": command},
    }
    assert _apply([document]) == {}
    (row,) = unsent_hook_documents([document])
    assert row["matcher"] == "@slack-mcp"
