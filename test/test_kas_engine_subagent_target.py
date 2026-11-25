"""The helper Kiro Crew ships for the KAS engine's sub-agent tool: its own surface.

A copy of the chat's own agent, the one an unnamed ``spawn_run`` runs: its built-in
tools, grants, rules and prompt, and the servers it holds
(``test_kas_engine_child_scope.py``). Every other server that could reach it is
excluded whole: Kiro Crew's own, a pooled stub or a block server its parent does
not mount. It reaches no Kiro Crew tool and cannot start another agent.
"""

from __future__ import annotations

from kiro_crew.acp.kas_agents_mcp_servers import MANAGED_MCP_SERVER_NAMES

_PARENT = {
    "id": "kirocrew",
    "prompt": "You are the chat's agent.",
    "description": "the chat's agent",
    "tools": ["fs_read", "execute_bash", "introspect", "invoke_sub_agent", "@kirocrew-core"],
    "excludedTools": ["@kirocrew-core/spawn_sub_agents"],
    "permissions": {
        "rules": [
            {"capability": "web_fetch", "effect": "allow"},
            {"capability": "shell", "match": ["rm *"], "effect": "deny"},
            {"capability": "shell", "match": ["git *"], "effect": "ask"},
        ]
    },
    "resources": ["file://.kiro/steering/**/*.md"],
    "mcpServers": {"kirocrew-core": {}, "user-server": {}},
    "_crewAnsweredBuiltins": ["fs_read"],
}


def _entry(monkeypatch, parent=None, stubs=frozenset({"pooled-server"})) -> dict:
    from kiro_crew.acp.kas_engine_subagent_target import helper_entry

    monkeypatch.setenv("ASBX_KIRO_MANDATORY_MCPS", "always-on")
    return helper_entry(parent or _PARENT, stub_server_names=stubs, settings_dir=None)


def test_the_helper_is_named_kirocrew_helper_and_starts_no_server_of_its_own(monkeypatch):
    entry = _entry(monkeypatch)
    assert entry["id"] == "kirocrew-helper"
    assert not entry.get("mcpServers")
    assert not entry.get("hooks")


def test_the_helper_is_a_copy_of_the_chats_own_agent(monkeypatch):
    """What an unnamed spawn_run child runs: the agent's built-ins, rules and resources."""
    entry = _entry(monkeypatch)
    assert entry["tools"] == ["fs_read", "execute_bash", "introspect"]
    rules = entry["permissions"]["rules"]
    for rule in _PARENT["permissions"]["rules"]:
        assert rule in rules, rule
    assert entry["resources"] == _PARENT["resources"]
    assert "@kirocrew-core/spawn_sub_agents" in entry["excludedTools"]


def test_the_helper_starts_no_sub_agent(monkeypatch):
    from kiro_crew.acp.kas_subagent_targets import NO_ENGINE_SUBAGENTS_RULE, mounts_subagent_tool

    entry = _entry(monkeypatch)
    assert "invoke_sub_agent" in entry["excludedTools"]
    assert NO_ENGINE_SUBAGENTS_RULE in entry["permissions"]["rules"]
    assert not mounts_subagent_tool(entry)
    # Every tool too: the exclusion and the deny hold against ``"*"``.
    wide = _entry(monkeypatch, {**_PARENT, "tools": "*"})
    assert not mounts_subagent_tool(wide)
    assert NO_ENGINE_SUBAGENTS_RULE in wide["permissions"]["rules"]


def test_kiro_crews_records_of_the_parent_are_not_copied(monkeypatch):
    entry = _entry(monkeypatch)
    assert "_crewAnsweredBuiltins" not in entry
    assert _PARENT["_crewAnsweredBuiltins"] == ["fs_read"]


def test_every_server_the_parent_does_not_share_is_excluded_from_the_helper(monkeypatch):
    """*_PARENT* mounts only ``kirocrew-core``, which is Kiro Crew's own."""
    entry = _entry(monkeypatch)
    excluded = set(entry.get("excludedTools") or ())
    for name in {*MANAGED_MCP_SERVER_NAMES, "kirocrew-core", "user-server", "pooled-server"}:
        assert f"@{name}" in excluded, name
    assert not any(t.startswith("@") for t in entry["tools"])
    # KAS starts its always-on servers for every agent, the parent included, and
    # hands their tools out whatever ``tools`` says: the helper holds what its parent does.
    assert "@always-on" not in excluded


def test_the_helpers_listing_line_says_what_it_is_and_what_it_lacks(monkeypatch):
    """The engine lists the description under its sub-agent tool, where the parent picks."""
    from kiro_crew.acp.kas_engine_subagent_target import HELPER_DESCRIPTION

    assert _entry(monkeypatch)["description"] == HELPER_DESCRIPTION
    assert "this chat's own agent" in HELPER_DESCRIPTION
    assert "without Kiro Crew's own tools or sub-agents" in HELPER_DESCRIPTION


def test_the_helpers_prompt_is_its_parents_and_says_what_it_lacks(monkeypatch):
    from kiro_crew.acp.kas_engine_subagent_target import CHILD_NOTE

    prompt = _entry(monkeypatch)["prompt"]
    assert prompt == f"{_PARENT['prompt']}\n\n{CHILD_NOTE}"
    for lacks in ("no Kiro Crew tool", "cannot start sub-agents", "no model, folder or turn"):
        assert lacks in CHILD_NOTE, lacks
    assert CHILD_NOTE.endswith(
        "If the task needs something you do not have, say so in your reply instead of "
        "working around it."
    )
    no_prompt = {k: v for k, v in _PARENT.items() if k != "prompt"}
    assert _entry(monkeypatch, no_prompt)["prompt"] == CHILD_NOTE
