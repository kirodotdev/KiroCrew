"""Native metadata cannot grow with the catalog behind an authored mapping."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import iter_agent_spec_files


@pytest.fixture
def native_tree(tmp_path, monkeypatch):
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="custom", filename="custom.json", scope="global")],
    )
    return home, agents, project


def test_native_view_bounds_metadata_and_preserves_original_scope(native_tree):
    home, agents, project = native_tree
    source = agents / "custom.json"
    spec = {
        "name": "custom",
        "prompt": "file://instructions.md",
        "tools": ["read", "@kirocrew-core"],
        "allowedTools": ["read"],
        "resources": ["file://RULES.md", *[f"skill://catalog/s{n}/SKILL.md" for n in range(1024)]],
    }
    original = json.dumps(spec)
    source.write_text(original, encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.agent("custom")
    view = json.loads((agents / f"{alias}.json").read_text(encoding="utf-8"))
    assert source.read_text(encoding="utf-8") == original
    assert all(not r.startswith("skill://") for r in view["resources"])
    assert len(view["resources"]) == 4
    assert view["tools"] == spec["tools"] and view["allowedTools"] == spec["allowedTools"]
    assert view["prompt"] == "file://" + (agents / "instructions.md").as_posix()
    assert iter_agent_spec_files(agents) == [source]
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["chat.disableInheritingDefaultResources"] is True
    assert projection.prepare_native_skill_projection(project).agent("custom") == alias


def test_native_view_preserves_explicit_noninheritance_and_other_settings(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        '{"name":"custom","resources":["file://RULES.md"]}', encoding="utf-8"
    )
    settings_path = project / ".kiro/settings/cli.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": True, "toolSearch.enabled": False}),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == ["file://RULES.md"]
    assert json.loads(settings_path.read_text(encoding="utf-8"))["toolSearch.enabled"] is False


def test_transport_keeps_original_agent_identity_and_rejects_unprepared_modes():
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    request = {"sessionId": "s", "modeId": "custom"}
    assert prepared.request("session/set_mode", request)["modeId"] == "native-alias"
    assert request["modeId"] == "custom"
    frame = prepared.frame(
        {
            "result": {
                "modes": {
                    "currentModeId": "native-alias",
                    "availableModes": [
                        {"id": "native-alias", "name": "native-alias"},
                        {"id": "unbounded-original"},
                    ],
                }
            }
        }
    )
    assert frame["result"]["modes"] == {
        "currentModeId": "custom",
        "availableModes": [{"id": "custom", "name": "custom"}],
    }
    with pytest.raises(ValueError, match="no prepared"):
        prepared.request("session/set_mode", {"modeId": "unknown"})


@pytest.mark.parametrize(
    "command", ["/agent swap custom", {"command": "agent", "args": {"value": "swap custom"}}]
)
def test_native_agent_switch_cannot_escape_crew_scope(command):
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    with pytest.raises(ValueError, match="agent selector"):
        prepared.request("_kiro.dev/commands/execute", {"command": command})


def test_custom_agent_gets_only_the_scoped_search_capability(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "tools": ["read"],
                "allowedTools": [],
                "resources": ["skill://skills/a/SKILL.md"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["tools"] == ["read", "@kirocrew-core/skill_search"]
    assert view["allowedTools"] == []
    assert "kirocrew-core" in view["mcpServers"]
    assert "autoApprove" not in view["mcpServers"]["kirocrew-core"]


def test_global_inheritance_preference_is_refreshed(native_tree):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = json.loads((agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8"))
    assert view["resources"] == []


def test_projected_search_uses_the_managed_command_and_preserves_approval(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "mcpServers": {
                    "kirocrew-core": {"command": "other-server", "args": [], "autoApprove": []}
                },
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    entry = prepared.specs["custom"]["mcpServers"]["kirocrew-core"]
    assert entry["command"] == "test-core" and entry["autoApprove"] == []


def test_explicit_search_exclusion_fails_only_that_agent(native_tree):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": ["skill://skills/a/SKILL.md"],
                "excludedTools": ["@kirocrew-core/skill_search"],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    with pytest.raises(ValueError, match="explicitly excluded"):
        prepared.agent("custom")


def test_unmapped_custom_agent_does_not_gain_tools_or_servers(native_tree):
    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": ["read"], "excludedTools": ["@kirocrew-core/skill_search"]}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.specs["custom"]
    assert view["tools"] == ["read"]
    assert "mcpServers" not in view
    assert "custom" not in prepared.search_agents


@pytest.mark.parametrize(
    "field,value",
    [
        ("disabled", None),
        ("disabled", "false"),
        ("disabled", 0),
        ("disabledTools", None),
        ("disabledTools", "skill_search"),
        ("disabledTools", {}),
        ("disabledTools", [1]),
    ],
)
def test_invalid_core_restrictions_fail_only_the_affected_agent(
    native_tree, monkeypatch, field, value
):
    _home, agents, project = native_tree
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("custom", "healthy")
        ],
    )
    for name, core in (
        ("custom", {field: value}),
        ("healthy", {"disabled": False, "disabledTools": []}),
    ):
        (agents / f"{name}.json").write_text(
            json.dumps(
                {
                    "name": name,
                    "resources": ["skill://skills/a/SKILL.md"],
                    "mcpServers": {"kirocrew-core": core},
                }
            ),
            encoding="utf-8",
        )
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    with pytest.raises(ValueError, match=field):
        prepared.agent("custom")
    assert (agents / f"{prepared.agent('healthy')}.json").exists()
    assert prepared.search_agents == {"healthy"}


@pytest.mark.parametrize(
    "original",
    [
        {},
        {"chat.disableInheritingDefaultResources": False},
        {"chat.disableInheritingDefaultResources": True},
        {"chat.disableInheritingDefaultResources": None},
        {"chat.disableInheritingDefaultResources": "false"},
        {"chat.disableInheritingDefaultResources": 1},
    ],
)
def test_rollback_restores_original_local_value_and_presence(native_tree, monkeypatch, original):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps(original), encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    projection.prepare_native_skill_projection(project)
    current = json.loads(settings.read_text(encoding="utf-8"))
    current["toolSearch.enabled"] = False
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    restored = json.loads(settings.read_text(encoding="utf-8"))
    expected = {**original, "toolSearch.enabled": False}
    # JSON distinguishes numeric 1 from true, unlike Python dictionary equality.
    assert json.dumps(restored, sort_keys=True) == json.dumps(expected, sort_keys=True)
    # Repeated rollback does not recreate the overlay.
    before = settings.read_bytes()
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_bytes() == before


@pytest.mark.parametrize("operator_value", [False, None, "deleted"])
def test_rollback_preserves_operator_changes(native_tree, monkeypatch, operator_value):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)
    settings = project / ".kiro/settings/cli.json"
    current = json.loads(settings.read_text(encoding="utf-8"))
    key = "chat.disableInheritingDefaultResources"
    if operator_value == "deleted":
        current.pop(key)
        expected = {}
    else:
        current[key] = operator_value
        expected = {key: operator_value}
    settings.write_text(json.dumps(current), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_disabled_projection_does_not_enumerate_agents_or_create_settings(native_tree, monkeypatch):
    _home, agents, project = native_tree

    def unexpected(**kwargs):
        pytest.fail("disabled projection must not read authored agents")

    monkeypatch.setattr(projection, "list_agents", unexpected)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert list(agents.iterdir()) == []
    assert not (project / ".kiro").exists()


@pytest.mark.parametrize(
    "source,inherited,expected",
    [
        ("global", True, {}),
        ("local", True, {"chat.disableInheritingDefaultResources": False}),
        ("local", False, {"chat.disableInheritingDefaultResources": True}),
    ],
)
def test_rollback_of_legacy_owned_overlay(native_tree, monkeypatch, source, inherited, expected):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "kirocrew.skillDiscovery.inheritFiles": inherited,
                "kirocrew.skillDiscovery.inheritSource": source,
                "chat.disableInheritingDefaultResources": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_rollback_never_changes_unmanaged_settings(native_tree, monkeypatch):
    _home, _agents, project = native_tree
    settings = project / ".kiro/settings/cli.json"
    settings.parent.mkdir(parents=True)
    original = '{ "chat.disableInheritingDefaultResources": true, "other": 42 }'
    settings.write_text(original, encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    assert projection.prepare_native_skill_projection(project) is None
    assert settings.read_text(encoding="utf-8") == original


def test_running_projection_keeps_its_mode_until_restart(native_tree, monkeypatch):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(project)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    refreshed = projection.prepare_native_skill_projection(project, enabled=True)
    assert refreshed.aliases == prepared.aliases
    assert projection.prepare_native_skill_projection(project) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_client_spawn_uses_authored_agent_only_when_rolled_back(
    native_tree, monkeypatch, enabled
):
    from kiro_crew.acp import client as client_module

    home, agents, project = native_tree
    monkeypatch.setenv("KIRO_HOME", str(home))
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "1" if enabled else "0")
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(
        client_module, "_resolve_kiro_bin_for_spawn", AsyncMock(return_value="test-kiro")
    )
    monkeypatch.setattr(client_module, "ensure_agent_materialized", lambda agent: None)
    monkeypatch.setattr(client_module, "require_fresh_derived_spec", lambda *args: None)
    monkeypatch.setattr(client_module, "require_fork_governance", lambda *args: None)
    monkeypatch.setattr(
        client_module, "delegated_workspace_exposes_sealed_target", lambda path: None
    )

    class StopSpawn(Exception):
        pass

    captured = []

    def stop_at_sandbox(argv, **kwargs):
        captured.extend(argv)
        raise StopSpawn

    monkeypatch.setattr(client_module, "wrap_argv", stop_at_sandbox)
    client = client_module.AcpClient(work_dir=project, agent="custom", sandbox_mode="off")
    with pytest.raises(StopSpawn):
        await client._spawn()
    assert captured[:3] == ["test-kiro", "acp", "--agent"]
    if enabled:
        assert captured[3] == client._native_skill_projection.agent("custom")
    else:
        assert captured[3] == "custom"
        assert client._native_skill_projection is None


@pytest.mark.parametrize("value", ["false", 1, False, True])
@pytest.mark.parametrize("source", ["local", "global"])
def test_only_literal_true_suppresses_inherited_instruction_files(native_tree, value, source):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = (project / ".kiro" if source == "local" else home) / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    resources = prepared.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)


@pytest.mark.parametrize("value", ["false", 1, False, True])
def test_global_preference_refresh_uses_only_literal_true(native_tree, value):
    home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    settings = home / "settings" / "cli.json"
    settings.parent.mkdir()
    settings.write_text('{"chat.disableInheritingDefaultResources":true}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project)
    assert first.specs["custom"]["resources"] == []
    settings.write_text(
        json.dumps({"chat.disableInheritingDefaultResources": value}), encoding="utf-8"
    )
    refreshed = projection.prepare_native_skill_projection(project)
    resources = refreshed.specs["custom"]["resources"]
    assert any("AGENTS.md" in item for item in resources) is (value is not True)
    assert any("steering" in item for item in resources) is (value is not True)
