"""An unmapped custom agent keeps the skill trees kiro-cli lets it inherit.

kiro-cli gives a custom agent the default skill trees (``~/.kiro/skills`` and
the workspace's ``.kiro/skills``) unless ``chat.disableInheritingDefaultResources``
turns that off. Crew's native launches turn it off to bound the startup catalog,
so Crew supplies those trees itself: as the agent's skill scope, and with the
``skill_search`` tool on its native view so the listed skills can load.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew import agent_discovery, context, skill_trust
from kiro_crew.acp import skill_projection as projection


@pytest.fixture
def kiro_tree(tmp_path, monkeypatch):
    """A kiro-cli home holding one global skill, an agents dir and a project."""
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    skill = home / "skills" / "deploy-helper"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: deploy-helper\n---\nBody\n", encoding="utf-8")
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: home)
    return home, agents, project


def _write_agent(agents: Path, spec: dict) -> None:
    (agents / f"{spec['name']}.json").write_text(json.dumps(spec), encoding="utf-8")


def _disable_inheritance(project: Path) -> None:
    settings = project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({projection._INHERIT_SETTING: True}), encoding="utf-8")


def _trust(monkeypatch, project: Path) -> None:
    """Trust *project* and give it a ``.kiro/skills`` tree."""
    (project / ".kiro" / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(skill_trust, "project_skills_enabled", lambda: True)
    monkeypatch.setattr(skill_trust, "is_project_trusted", lambda p: Path(p) == project)


def _globs(agent: str, agents: Path, **kw) -> list[str]:
    return agent_discovery.agent_skill_globs(agent, agents_dir=agents, **kw)


def test_unmapped_custom_agent_inherits_the_global_skill_tree(kiro_tree):
    home, agents, _project = kiro_tree
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    assert _globs("custom", agents, inherit_defaults=True) == [
        str(home / "skills" / "*" / "SKILL.md")
    ]
    # Listing callers (no inherit_defaults) still read the authored mapping only.
    assert _globs("custom", agents) == []


@pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="no-follow project traversal is unsupported on this platform",
)
def test_a_trusted_workspace_tree_is_inherited_too(kiro_tree, monkeypatch):
    home, _agents, project = kiro_tree
    _trust(monkeypatch, project)
    assert agent_discovery.inherited_skill_globs(project) == [
        str(home / "skills" / "*" / "SKILL.md"),
        str(project / ".kiro" / "skills" / "*" / "SKILL.md"),
    ]


def test_an_untrusted_workspace_tree_is_not_probed(kiro_tree, monkeypatch):
    """Trust is asked first, so a workspace-controlled path is never opened."""
    from kiro_crew import skills

    home, _agents, project = kiro_tree
    (project / ".kiro" / "skills").mkdir(parents=True)
    opened = []
    monkeypatch.setattr(skills, "_open_project_dir_chain", lambda base: opened.append(base))
    assert agent_discovery.inherited_skill_globs(project) == [
        str(home / "skills" / "*" / "SKILL.md")
    ]
    assert opened == []


@pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="no-follow project traversal is unsupported on this platform",
)
def test_a_linked_workspace_tree_is_refused_not_followed(kiro_tree, monkeypatch, tmp_path):
    home, _agents, project = kiro_tree
    _trust(monkeypatch, project)
    (project / ".kiro" / "skills").rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    try:
        (project / ".kiro" / "skills").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert agent_discovery.inherited_skill_globs(project) == [
        str(home / "skills" / "*" / "SKILL.md")
    ]


def test_a_missing_global_tree_is_left_out(kiro_tree):
    home, _agents, _project = kiro_tree
    for child in (home / "skills").rglob("*"):
        if child.is_file():
            child.unlink()
    (home / "skills" / "deploy-helper").rmdir()
    (home / "skills").rmdir()
    assert agent_discovery.inherited_skill_globs(None) == []


def test_workspace_that_opts_out_of_inheritance_gets_no_skills(kiro_tree):
    _home, _agents, project = kiro_tree
    _disable_inheritance(project)
    assert agent_discovery.inherited_skill_globs(project) == []


def test_global_opt_out_of_inheritance_gets_no_skills(kiro_tree):
    home, agents, _project = kiro_tree
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({projection._INHERIT_SETTING: True}), encoding="utf-8")
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    assert _globs("custom", agents, inherit_defaults=True) == []


def test_mapped_custom_agent_keeps_only_its_own_mapping(kiro_tree):
    _home, agents, _project = kiro_tree
    _write_agent(agents, {"name": "custom", "resources": ["skill:///opt/skills/one/SKILL.md"]})
    assert _globs("custom", agents, inherit_defaults=True) == ["/opt/skills/one/SKILL.md"]


@pytest.mark.parametrize(
    "spec",
    [
        {"tools": []},
        {"excludedTools": ["@kirocrew-core/skill_search"]},
        {"excludedTools": ["@kirocrew-core"]},
        {"mcpServers": {"kirocrew-core": {"disabled": True}}},
        {"mcpServers": {"kirocrew-core": {"disabledTools": ["skill_search"]}}},
        {},
        {"tools": ["*"]},
    ],
    ids=[
        "zero-tools",
        "search-excluded",
        "core-excluded",
        "core-disabled",
        "search-disabled",
        "no-tool-list",
        "wildcard-tools",
    ],
)
def test_agent_that_withholds_skill_search_inherits_nothing(kiro_tree, spec):
    _home, agents, _project = kiro_tree
    _write_agent(agents, {"name": "custom", **spec})
    assert _globs("custom", agents, inherit_defaults=True) == []


def test_default_agent_is_unchanged(kiro_tree):
    _home, agents, _project = kiro_tree
    _write_agent(agents, {"name": "kirocrew", "tools": ["*"]})
    assert _globs("kirocrew", agents, inherit_defaults=True) == []


@pytest.mark.parametrize(
    ("resources", "scope"),
    [([], None), (["skill:///mirrored/SKILL.md"], ["/mirrored/SKILL.md"])],
)
def test_the_worker_sees_what_the_default_agent_sees(kiro_tree, monkeypatch, resources, scope):
    """The worker maps exactly the default's ``skill://`` entries, so mapping none means
    its dispatcher sees the whole catalog: so does the worker, not the inherited trees."""
    _home, agents, project = kiro_tree
    _write_agent(agents, {"name": "kirocrew-worker", "tools": ["read"], "resources": resources})
    assert _globs("kirocrew-worker", agents, inherit_defaults=True) == (scope or [])
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        agent_discovery,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="kirocrew-worker", filename="kirocrew-worker.json", scope="global")
        ],
    )
    acp = context.PROVIDER_ACP
    search_scope = agent_discovery.session_skill_globs(
        "", "kirocrew-worker", project_dir=project, provider_type=acp
    )
    assert search_scope == scope
    assert context._skills_injection_plan(
        "kirocrew-worker", is_cc=False, project_dir=project, provider_type=acp
    ) == (True, scope or [])


def test_a_worker_without_its_spec_reads_as_the_default_does(kiro_tree, monkeypatch):
    """Boot rewrites the worker spec, so a missing one is a recovery path. The worker
    then gets what the default gets with no spec, the whole catalog, in skill search
    as in the injected list. A custom agent with no spec still refuses."""
    _home, agents, project = kiro_tree
    monkeypatch.setattr(agent_discovery, "list_agents", lambda **kw: [])
    acp = context.PROVIDER_ACP
    for agent in ("kirocrew", "kirocrew-worker"):
        assert _globs(agent, agents, strict=True, inherit_defaults=True) == []
        assert (
            agent_discovery.session_skill_globs("", agent, project_dir=project, provider_type=acp)
            is None
        )
        assert context._skills_injection_plan(
            agent, is_cc=False, project_dir=project, provider_type=acp
        ) == (True, [])
    with pytest.raises(agent_discovery.SkillScopeResolutionError):
        agent_discovery.session_skill_globs("", "custom", project_dir=project, provider_type=acp)


def test_injection_plan_gives_the_inherited_trees_to_an_unmapped_custom_agent(
    kiro_tree, monkeypatch
):
    home, agents, project = kiro_tree
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        agent_discovery,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    inject, globs = context._skills_injection_plan(
        "custom", is_cc=False, project_dir=project, provider_type=context.PROVIDER_ACP
    )
    assert inject is True
    assert globs == [str(home / "skills" / "*" / "SKILL.md")]

    _disable_inheritance(project)
    assert context._skills_injection_plan(
        "custom", is_cc=False, project_dir=project, provider_type=context.PROVIDER_ACP
    ) == (False, [])


@pytest.fixture
def native_custom(kiro_tree, tmp_path, monkeypatch):
    home, agents, project = kiro_tree
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    return agents, project


def test_native_view_of_an_inheriting_agent_gets_skill_search(native_custom):
    agents, project = native_custom
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    view = projection.prepare_native_skill_projection(project).specs["custom"]
    assert view["tools"] == ["read", projection._SEARCH_TOOL]
    assert "kirocrew-core" in view["mcpServers"]


def test_native_view_of_a_zero_tool_agent_stays_zero_tool(native_custom):
    agents, project = native_custom
    _write_agent(agents, {"name": "custom", "tools": []})
    view = projection.prepare_native_skill_projection(project).specs["custom"]
    assert view["tools"] == []
    assert "kirocrew-core" not in view.get("mcpServers", {})


def test_native_view_without_inheritance_gets_no_search(native_custom):
    agents, project = native_custom
    _disable_inheritance(project)
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    view = projection.prepare_native_skill_projection(project).specs["custom"]
    assert view["tools"] == ["read"]


def test_only_a_kiro_cli_session_gets_the_inherited_trees(kiro_tree, monkeypatch):
    """Only kiro-cli lends these trees; KAS, Claude Code and an unnamed one do not."""
    home, agents, project = kiro_tree
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        agent_discovery,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    assert context._skills_injection_plan(
        "custom", is_cc=True, project_dir=project, provider_type="claude_code"
    ) == (False, [])
    assert context._skills_injection_plan(
        "custom", is_cc=False, project_dir=project, provider_type="kas"
    ) == (False, [])
    assert context._skills_injection_plan("custom", is_cc=False, project_dir=project) == (
        False,
        [],
    )
    assert context._skills_injection_plan(
        "custom", is_cc=False, project_dir=project, provider_type=context.PROVIDER_ACP
    ) == (True, [str(home / "skills" / "*" / "SKILL.md")])


def test_inheriting_agent_is_not_a_search_agent(native_custom):
    """Its sessions are never refused over the search element it did not ask for."""
    agents, project = native_custom
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    prepared = projection.prepare_native_skill_projection(project)
    assert "custom" not in prepared.search_agents


def test_an_unmapped_worker_is_a_search_agent_like_the_default(native_custom, monkeypatch):
    """Its directory lists the whole catalog, so its view needs the search the default's
    view requires and is refused by name, not started without it."""
    agents, project = native_custom
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name="kirocrew-worker", filename="kirocrew-worker.json", scope="global")
        ],
    )
    _write_agent(agents, {"name": "kirocrew-worker", "tools": ["read"]})
    prepared = projection.prepare_native_skill_projection(project)
    assert "kirocrew-worker" in prepared.search_agents


def test_inheriting_agent_keeps_its_view_when_search_cannot_be_wired(native_custom, monkeypatch):
    agents, project = native_custom
    monkeypatch.setattr("kiro_crew.agent.managed_mcp_spec_entry", lambda name: None)
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read"]
    assert "kirocrew-core" not in view.get("mcpServers", {})


def test_mapped_agent_is_still_refused_when_search_cannot_be_wired(native_custom, monkeypatch):
    agents, project = native_custom
    monkeypatch.setattr("kiro_crew.agent.managed_mcp_spec_entry", lambda name: None)
    _write_agent(
        agents,
        {"name": "custom", "tools": ["read"], "resources": ["skill:///opt/s/one/SKILL.md"]},
    )
    prepared = projection.prepare_native_skill_projection(project)
    assert "custom" not in prepared.specs


def test_one_spelling_of_the_search_refusal():
    assert agent_discovery.skill_search_refusal({}) is None
    assert "excluded" in agent_discovery.skill_search_refusal(
        {"excludedTools": ["@kirocrew-core/*"]}
    )
    assert "disabled" in agent_discovery.skill_search_refusal(
        {"mcpServers": {"kirocrew-core": {"disabledTools": ["skill_search"]}}}
    )


@pytest.mark.parametrize(
    ("provider", "inherits"),
    [("acp", True), ("kas", False), ("claude_code", False), (None, False)],
)
def test_skill_search_scope_follows_the_same_provider_rule(
    kiro_tree, monkeypatch, provider, inherits
):
    home, agents, project = kiro_tree
    _write_agent(agents, {"name": "custom", "tools": ["read"]})
    monkeypatch.setattr(agent_discovery, "_kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        agent_discovery,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    scope = agent_discovery.session_skill_globs(
        "", "custom", project_dir=project, provider_type=provider
    )
    assert scope == ([str(home / "skills" / "*" / "SKILL.md")] if inherits else [])


def test_a_wildcard_agent_that_already_mounts_kirocrew_core_inherits(kiro_tree):
    home, agents, _project = kiro_tree
    _write_agent(
        agents,
        {
            "name": "custom",
            "tools": ["*"],
            "mcpServers": {"kirocrew-core": {"command": "kirocrew"}},
        },
    )
    assert _globs("custom", agents, inherit_defaults=True) == [
        str(home / "skills" / "*" / "SKILL.md")
    ]


def test_native_view_of_a_wildcard_agent_gains_no_kirocrew_core(native_custom):
    """Adding the server to a "*" agent would grant every Crew tool."""
    agents, project = native_custom
    _write_agent(agents, {"name": "custom", "tools": ["*"]})
    view = projection.prepare_native_skill_projection(project).specs["custom"]
    assert "kirocrew-core" not in view.get("mcpServers", {})


def test_no_live_session_names_no_harness():
    assert agent_discovery.served_provider_label(None) is None
    assert agent_discovery.served_provider_label(object()) is None
