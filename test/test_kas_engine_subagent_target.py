"""The helper Kiro Crew ships for the KAS engine's sub-agent tool holds built-in tools only.

Every server the session-level array carries reaches every agent of a KAS
session's batch, whatever that agent's ``tools`` list names. So the helper's
entry excludes each of them whole, and its own surface is built-in tools: it
reaches no Kiro Crew tool, cannot start another agent, and carries no hook.
"""

from __future__ import annotations

from pathlib import Path

from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES
from kiro_crew.platform.tool_names import KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME

_PARENT = {"id": "kirocrew", "mcpServers": {"kirocrew-core": {}, "user-server": {}}}


def _entry(tmp_path: Path, monkeypatch, stubs=frozenset({"pooled-server"})) -> dict:
    from kiro_crew.acp.kas_engine_subagent_target import helper_entry

    monkeypatch.setenv("ASBX_KIRO_MANDATORY_MCPS", "always-on")
    return helper_entry(
        tmp_path, _PARENT, stub_server_names=stubs, session_key="dashboard:t", settings_dir=None
    )


def _ids(*kiro_cli_names: str) -> set[str]:
    """Each kiro-cli name and the KAS ids its family carries."""
    return {i for n in kiro_cli_names for i in (n, *KAS_TOOL_FAMILY_BY_KIRO_CLI_NAME.get(n, ()))}


def test_the_helper_is_named_kirocrew_helper_and_carries_no_server(tmp_path, monkeypatch):
    entry = _entry(tmp_path, monkeypatch)
    assert entry["id"] == "kirocrew-helper"
    assert not entry.get("mcpServers")
    assert not entry.get("hooks")


def test_the_helpers_tools_are_built_in_only(tmp_path, monkeypatch):
    tools = _entry(tmp_path, monkeypatch)["tools"]
    assert all(isinstance(t, str) and not t.startswith("@") for t in tools)
    assert not set(tools) & _ids("use_subagent", "tool_search", "session", "introspect")
    assert set(tools) <= _ids(
        "fs_read", "fs_write", "execute_bash", "grep", "glob", "code", "web_fetch", "web_search"
    )


def test_every_session_level_server_is_excluded_from_the_helper(tmp_path, monkeypatch):
    excluded = set(_entry(tmp_path, monkeypatch).get("excludedTools") or ())
    for name in {
        *MANAGED_MCP_SERVER_NAMES,
        "kirocrew-core",
        "user-server",
        "pooled-server",
        "always-on",
    }:
        assert f"@{name}" in excluded, name


def test_the_helper_is_built_fresh_and_ignores_an_agent_file_of_its_name(tmp_path, monkeypatch):
    (tmp_path / "kirocrew-helper.json").write_text(
        '{"name": "kirocrew-helper", "tools": ["*"], "mcpServers": {"x": {"command": "x"}}}',
        encoding="utf-8",
    )
    entry = _entry(tmp_path, monkeypatch)
    assert not entry.get("mcpServers")
    assert "*" not in entry["tools"]
