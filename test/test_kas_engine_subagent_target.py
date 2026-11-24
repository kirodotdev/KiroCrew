"""The helper Kiro Crew ships for the KAS engine's sub-agent tool: its own surface.

Built-in tools of its own, and the servers its parent holds
(``test_kas_engine_child_scope.py``). Every other server that could reach it is
excluded whole: Kiro Crew's own, a pooled stub or a block server its parent does
not mount. It reaches no Kiro Crew tool, cannot start another agent, and carries
no hook.
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


def test_the_helper_is_named_kirocrew_helper_and_starts_no_server_of_its_own(tmp_path, monkeypatch):
    entry = _entry(tmp_path, monkeypatch)
    assert entry["id"] == "kirocrew-helper"
    assert not entry.get("mcpServers")
    assert not entry.get("hooks")


def test_the_helpers_own_tools_are_built_ins(tmp_path, monkeypatch):
    tools = _entry(tmp_path, monkeypatch)["tools"]
    assert all(isinstance(t, str) and not t.startswith("@") for t in tools)
    assert not set(tools) & _ids("use_subagent", "tool_search", "session", "introspect")
    assert set(tools) <= _ids(
        "fs_read", "fs_write", "execute_bash", "grep", "glob", "code", "web_fetch", "web_search"
    )


def test_every_server_the_parent_does_not_mount_is_excluded_from_the_helper(tmp_path, monkeypatch):
    """*_PARENT* lists no ``tools``, so it mounts neither its block server nor the stub."""
    excluded = set(_entry(tmp_path, monkeypatch).get("excludedTools") or ())
    for name in {*MANAGED_MCP_SERVER_NAMES, "kirocrew-core", "user-server", "pooled-server"}:
        assert f"@{name}" in excluded, name
    # KAS starts its always-on servers for every agent, the parent included, and
    # hands their tools out whatever ``tools`` says: the helper holds what its parent does.
    assert "@always-on" not in excluded


def test_the_helper_is_built_fresh_and_ignores_an_agent_file_of_its_name(tmp_path, monkeypatch):
    (tmp_path / "kirocrew-helper.json").write_text(
        '{"name": "kirocrew-helper", "tools": ["*"], "mcpServers": {"x": {"command": "x"}}}',
        encoding="utf-8",
    )
    entry = _entry(tmp_path, monkeypatch)
    assert not entry.get("mcpServers")
    assert "*" not in entry["tools"]


_PROMPT_MD = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "config" / "prompt.md"


def _rules_section() -> list[str]:
    """The lines of the ``## Rules`` section of the shipped default contract."""
    lines = _PROMPT_MD.read_text(encoding="utf-8").splitlines()
    start = lines.index("## Rules") + 1
    end = next(i for i in range(start, len(lines)) if lines[i].startswith("## "))
    return lines[start:end]


def test_the_helper_carries_the_parents_tool_rules_from_the_one_source():
    """Every rule the helper carries is a whole Rules bullet of ``config/prompt.md``."""
    from kiro_crew.acp.kas_engine_subagent_target import (
        HELPER_RULE_ANCHORS,
        helper_spec,
        helper_tool_rules,
    )

    rules = _rules_section()
    carried = helper_tool_rules()
    # Each anchor names exactly one bullet, and the helper carries every one of them.
    for anchor in HELPER_RULE_ANCHORS:
        assert sum(line.startswith(anchor) for line in rules) == 1, anchor
    assert len(carried) == len(HELPER_RULE_ANCHORS)
    for line in carried:
        assert line in rules, line
    prompt = helper_spec()["prompt"]
    for named in (
        "Do NOT read credential files directly",
        "Do NOT run destructive commands",
        "A blocked call is a policy decision, not a puzzle.",
    ):
        assert any(named in line for line in carried), named
        assert named in prompt, named
    assert prompt.endswith("\n".join(carried))


def test_the_helpers_listing_line_says_what_it_holds_and_what_it_does_not():
    """The engine lists the description under its sub-agent tool, where the parent picks."""
    from kiro_crew.acp.kas_engine_subagent_target import helper_spec

    assert helper_spec()["description"] == (
        "Runs one short, independent task with built-in tools and the chat agent's "
        "MCP servers (no Kiro Crew tool) and reports the result back."
    )


def test_the_helper_is_told_to_report_a_tool_it_lacks():
    from kiro_crew.acp.kas_engine_subagent_target import helper_spec

    own, _, _rules = helper_spec()["prompt"].partition("\n\n")
    assert own.endswith(
        "If the task needs a tool you do not have, say so in your reply instead of "
        "working around it."
    )


def test_a_rule_the_source_loses_or_rewords_leaves_the_helper(tmp_path, monkeypatch):
    """The helper holds no copy of its own: a bullet the source drops is not carried."""
    import kiro_crew.agent as agent_mod
    from kiro_crew.acp.kas_engine_subagent_target import HELPER_RULE_ANCHORS, helper_tool_rules

    lines = _PROMPT_MD.read_text(encoding="utf-8").splitlines()
    dropped = HELPER_RULE_ANCHORS[0]
    lines = [
        line.replace(dropped, "- Reworded:") if line.startswith(dropped) else line for line in lines
    ]
    (tmp_path / "prompt.md").write_text("\n".join(lines), encoding="utf-8")
    monkeypatch.setattr(agent_mod, "_BUNDLED_CFG_DIR", tmp_path)
    carried = helper_tool_rules()
    assert len(carried) == len(HELPER_RULE_ANCHORS) - 1
    assert not any(line.startswith(dropped) for line in carried)
