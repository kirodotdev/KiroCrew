"""Native metadata cannot grow with the catalog behind an authored mapping.

This file holds what goes into the projected view, how mode and spawn names translate,
how the settings overlay rolls back, and the fallback at the view ceiling. Its siblings
hold the alias lifecycle: ``test_native_skill_projection_alias_reclaim.py``,
``test_native_skill_projection_lease_scan.py``,
``test_native_skill_projection_scan_bounds.py`` and
``test_native_skill_projection_rewrites.py``. The fixtures and helpers they share are in
``native_skill_projection_helpers.py``.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import native_skill_projection_helpers
import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import iter_agent_spec_files

# The shared fixtures and helpers, bound here by name: pytest registers the fixtures
# from this namespace.
_legacy_alias = native_skill_projection_helpers._legacy_alias
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
native_tree = native_skill_projection_helpers.native_tree


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


def test_launch_record_resources_match_the_alias_file_kiro_cli_loads(native_tree):
    """The runtime records the launch steering from the in-memory view while
    kiro-cli loads the alias file, so a file holding fewer resources than the
    view would make the folder skip a guide kiro-cli never loaded.
    """
    from kiro_crew import member_essential_context

    _home, agents, project = native_tree
    guides = {
        ".kiro/steering/conventions.md": "# Conventions\n",
        ".kiro/steering/nested/review.md": "# Review\n",
        "notes/handbook.md": "# Handbook\n",
        "AGENTS.md": "# Agents\n",
    }
    for relative, body in guides.items():
        path = project / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    settings = project / ".kiro" / "settings" / "cli.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({projection._INHERIT_SETTING: True}), encoding="utf-8")
    (agents / "custom.json").write_text(
        json.dumps(
            {
                "name": "custom",
                "resources": [
                    "file://.kiro/steering/**/*.md",
                    "file://notes/handbook.md",
                    "file://AGENTS.md",
                    "skill://skills/a/SKILL.md",
                ],
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    assert projection.inherits_default_resources(project) is False
    spec = prepared.specs["custom"]
    published = json.loads(
        (agents / f"{prepared.agent('custom')}.json").read_text(encoding="utf-8")
    )
    assert published["resources"] == spec["resources"]

    recorded = member_essential_context.projected_launch_paths(spec, str(project))
    loaded = member_essential_context.projected_launch_paths(published, str(project))
    assert recorded == loaded
    root = Path(os.path.realpath(project))
    assert recorded == [str(root / relative) for relative in guides]


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


def test_set_mode_activates_the_launched_agent_without_a_view_then_consumes_it():
    """The launched agent's FIRST activation passes; it is then consumed; other
    unprepared modes never pass.

    The direct-client startup activates the agent with ``session/set_mode``. When
    that agent has no prepared view, refusing its activation would strand a valid
    startup, so the request path tolerates the modeId that equals the recorded
    ``spawn_agent_name`` -- and only that one, and only ONCE. The first tolerated
    activation consumes the exemption, so a later switch back to the same agent
    takes the strict resolver and fails closed if its view is still absent -- no
    reactivating a cached spec the strict resolver existed to refuse.
    """
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    prepared.spawn_agent_name = "kirocrew"
    activated = prepared.request("session/set_mode", {"sessionId": "s", "modeId": "kirocrew"})
    assert activated["modeId"] == "kirocrew"
    # The exemption is spent: a second activation of the same agent fails closed.
    assert prepared.spawn_agent_name == ""
    with pytest.raises(ValueError, match="no prepared"):
        prepared.request("session/set_mode", {"modeId": "kirocrew"})
    # A prepared agent still maps to its alias.
    assert prepared.request("session/set_mode", {"modeId": "custom"})["modeId"] == "native-alias"
    # Any OTHER unprepared mode is still rejected, launched agent set or not.
    with pytest.raises(ValueError, match="no prepared"):
        prepared.request("session/set_mode", {"modeId": "some-other-agent"})


def test_frame_advertises_the_launched_agent_mode_independently_of_the_request_exemption():
    """A no-view launched agent's own mode stays in projected availableModes
    whenever ``advertised_launch_name`` is set, independent of the request
    exemption and NOT hidden after the initial set_mode is consumed.

    The start reads availableModes BEFORE it sends set_mode. If the launched
    agent has no prepared view, dropping its mode would advertise no mode the
    process could activate and fail the start. Advertising keys on
    ``advertised_launch_name`` -- a fixed launch fact set by BOTH the direct
    client and the shared runtime -- not on ``spawn_agent_name``, which the
    request path consumes and which the shared runtime leaves empty. So the
    launch mode must survive even after the exemption is spent, and an
    unprojected stranger is still never advertised.
    """
    prepared = projection.NativeSkillProjection(
        {}, spawn_agent_name="kirocrew", advertised_launch_name="kirocrew"
    )
    kept = prepared.frame(
        {"availableModes": [{"id": "kirocrew", "name": "kirocrew"}, {"id": "stranger"}]}
    )
    # The launched agent's mode survives; an unprojected stranger does not.
    assert kept["availableModes"] == [{"id": "kirocrew", "name": "kirocrew"}]
    # Consuming the request exemption does NOT hide the advertised launch mode:
    # advertising is a fixed fact, not the one-shot request tolerance.
    prepared.request("session/set_mode", {"modeId": "kirocrew"})
    assert prepared.spawn_agent_name == ""
    after = prepared.frame({"availableModes": [{"id": "kirocrew", "name": "kirocrew"}]})
    assert after["availableModes"] == [{"id": "kirocrew", "name": "kirocrew"}]


def test_frame_advertises_the_launch_mode_on_the_shared_runtime_with_empty_spawn_name():
    """The shared runtime leaves ``spawn_agent_name`` empty (so request() stays
    strict mid-session) but still sets ``advertised_launch_name``, so the no-view
    launch agent's mode is advertised and the session open finds it.

    This is the fail-closed-at-open bug the prior wiring had: keying advertising
    on the empty ``spawn_agent_name`` dropped the shared runtime's launch mode
    from availableModes, so ``_mode_available`` failed and the session was
    terminated before activation.
    """
    prepared = projection.NativeSkillProjection({}, advertised_launch_name="kirocrew")
    assert prepared.spawn_agent_name == ""
    kept = prepared.frame(
        {"availableModes": [{"id": "kirocrew", "name": "kirocrew"}, {"id": "stranger"}]}
    )
    assert kept["availableModes"] == [{"id": "kirocrew", "name": "kirocrew"}]
    # A projection no spawn claimed (both fields empty) advertises neither.
    unclaimed = projection.NativeSkillProjection({})
    hidden = unclaimed.frame({"availableModes": [{"id": "kirocrew", "name": "kirocrew"}]})
    assert hidden["availableModes"] == []


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


@pytest.mark.parametrize(
    ("resources", "expected"),
    [(["skill://skills/a/SKILL.md"], False), (["file://RULES.md"], True)],
)
def test_zero_tool_confirmation_judges_the_view_kiro_loads(
    native_tree, monkeypatch, resources, expected
):
    """The projection appends skill search to the view of a spec carrying a
    ``skill://`` resource, so an empty authored ``tools`` list does not confirm
    the ban for the alias kiro loads through ``--agent``. The view is captured
    when the projection is prepared, so a later edit of the authored spec does
    not change the answer."""
    from kiro_crew.acp import client as acp_client

    _home, agents, project = native_tree
    spec = {"name": "custom", "tools": [], "resources": resources}
    (agents / "custom.json").write_text(json.dumps(spec), encoding="utf-8")
    client = acp_client.AcpClient(agent="custom", work_dir=project)
    client._native_skill_projection = projection.prepare_native_skill_projection(
        project, per_session_element=False
    )
    view = client._native_skill_projection.specs["custom"]
    assert (view["tools"] == []) is expected
    assert client.effective_spec_declares_zero_tools() is expected

    (agents / "custom.json").write_text(json.dumps({**spec, "tools": ["read"]}), encoding="utf-8")
    assert client.effective_spec_declares_zero_tools() is expected


def test_rollback_switch_confirms_a_zero_tool_spec_through_the_authored_bracket(
    native_tree, monkeypatch
):
    """With the projection switched off no view exists and the harness loads the
    named spec itself, so the authored spec read before the spawn and again after
    start is the confirmation; a view-only answer would leave the pool unstartable."""
    from kiro_crew.acp import client as acp_client

    _home, _agents, project = native_tree
    project_agents = project / ".kiro" / "agents"
    project_agents.mkdir(parents=True)
    spec_path = project_agents / "custom.json"
    spec_path.write_text(json.dumps({"name": "custom", "tools": []}), encoding="utf-8")
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")
    client = acp_client.AcpClient(agent="custom", work_dir=project)
    client._native_skill_projection = projection.prepare_native_skill_projection(
        project, per_session_element=False
    )
    assert client._native_skill_projection is None
    before = client.authored_spec_declares_zero_tools()
    assert before is True
    assert client.effective_spec_declares_zero_tools(authored_before_spawn=before) is True

    spec_path.write_text(json.dumps({"name": "custom", "tools": ["read"]}), encoding="utf-8")
    assert client.effective_spec_declares_zero_tools(authored_before_spawn=before) is False


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


def test_spawn_passes_through_a_no_view_name_when_no_spec_was_refused(native_tree, monkeypatch):
    """With no refused spec, a genuinely-absent name spawns under its own name.

    When discovery refuses nothing, an unprojected name is provably spec-less, so
    ``spawn_agent`` returns the authored name rather than aborting an otherwise
    valid spawn. Readable-but-unprojected files on disk (a dedup twin, a plain doc)
    are not refusals and must not fail the spawn closed.
    """
    _home, agents, project = native_tree
    monkeypatch.setattr(projection, "list_agents", lambda **kw: [])
    # All readable, none projected (list_agents patched to []), none a refusal.
    (agents / "foo.json").write_text(json.dumps({"name": "bar"}), encoding="utf-8")
    (agents / "local-pkg-helper.json").write_text(json.dumps({"name": "helper"}), encoding="utf-8")

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared.spawn_agent("genuinely-absent") == "genuinely-absent"
    assert prepared.spawn_agent("bar") == "bar"


def test_the_spawned_view_keeps_the_specs_hooks(native_tree):
    """kiro-cli runs the spec's ``hooks`` itself, from the view it is launched with.

    So the ``--agent`` view a spawn resolves to carries the source's ``hooks``
    unchanged; a view without them would leave a hook-gated agent with no hooks
    on any backend.
    """
    home, agents, project = native_tree
    hooks = {
        "agentSpawn": [{"command": "init.sh"}],
        "userPromptSubmit": [{"command": "stamp.sh"}],
        "preToolUse": [{"matcher": "execute_bash", "command": "guard.sh"}],
        "stop": [{"command": "done.sh"}],
    }
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "hooks": hooks}), encoding="utf-8"
    )
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.spawn_agent("custom")
    assert alias != "custom"
    view = json.loads((agents / f"{alias}.json").read_text(encoding="utf-8"))
    assert view["hooks"] == hooks


def test_spawn_agent_keeps_the_authored_name_when_no_view_is_prepared():
    """A spawn of an agent the projection never prepared uses its own name.

    ``prepare_native_skill_projection`` returns a projection even when it mapped
    no agents (a work_dir carrying no matching spec), so the spawn path asks
    ``spawn_agent`` rather than the strict ``agent``: an unprojected agent keeps
    its authored transport name -- the same answer a ``None`` projection gives --
    instead of aborting the spawn. The strict ``agent`` still rejects it, because
    that resolver guards ``session/set_mode``.
    """
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    assert prepared.spawn_agent("custom") == "native-alias"
    assert prepared.spawn_agent("kirocrew") == "kirocrew"
    with pytest.raises(ValueError, match="no prepared"):
        prepared.agent("kirocrew")


def test_spawn_agent_still_raises_an_authored_restriction():
    """An authored refusal is a user-facing spawn refusal, not a silent skip.

    A ``kirocrew-core`` exclusion or a disabled ``skill_search`` records an
    ``errors`` entry naming the spec and the remedy; ``spawn_agent`` raises it so
    the runtime can wrap it as ``AcpRuntimeError`` and the startup paths can
    translate the sentence, exactly as the strict ``agent`` does.
    """
    prepared = projection.NativeSkillProjection(
        {}, errors={"custom": "skill_search is explicitly excluded; ..."}
    )
    with pytest.raises(ValueError, match="explicitly excluded"):
        prepared.spawn_agent("custom")


def test_unknown_name_with_no_matching_spec_still_passes_through_on_spawn():
    """A name that matches no alias or error is still an unprojected agent and
    spawn keeps its own name -- a no-view launch agent spawns under its own name."""
    prepared = projection.NativeSkillProjection({"custom": "native-alias"})
    assert prepared.spawn_agent("unrelated") == "unrelated"
    with pytest.raises(ValueError, match="no prepared"):
        prepared.agent("unrelated")


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


@pytest.fixture
def _reset_ceiling_state(monkeypatch):
    """Isolate the ceiling's process-global warn flag per test.

    ``_CEILING_FALLBACK_WARNED`` is set once per process and never reset in
    product code (one line per process is the point), so it would leak across
    tests; each ceiling test starts with it clear.
    """
    monkeypatch.setattr(projection, "_CEILING_FALLBACK_WARNED", False)


def _fill_agents_dir_with_views(agents: Path, count: int) -> None:
    """Write *count* ``kirocrew-skill-view-*.json`` files directly in *agents*."""
    for n in range(count):
        name = f"{projection.NATIVE_SKILL_ALIAS_PREFIX}{n:024x}"
        (agents / f"{name}.json").write_text(f'{{"name":"{name}"}}', encoding="utf-8")


def test_below_the_ceiling_a_view_is_created(native_tree, monkeypatch, _reset_ceiling_state):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    # One short of the ceiling: preparation still mints the view.
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 5)
    _fill_agents_dir_with_views(agents, 4)
    prepared = projection.prepare_native_skill_projection(project)
    assert prepared is not None
    alias = prepared.agent("custom")
    assert (agents / f"{alias}.json").exists()
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings["chat.disableInheritingDefaultResources"] is True


def test_at_or_above_the_ceiling_falls_back_and_writes_no_view(
    native_tree, monkeypatch, _reset_ceiling_state, caplog
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 5)
    # Exactly at the ceiling (>=): no new view, authored agent fallback.
    _fill_agents_dir_with_views(agents, 5)

    def _skill_views():
        return sorted(
            p.name
            for p in agents.iterdir()
            if p.name.startswith(projection.NATIVE_SKILL_ALIAS_PREFIX) and p.name.endswith(".json")
        )

    before = _skill_views()
    with caplog.at_level(logging.WARNING, logger=projection.logger.name):
        assert projection.prepare_native_skill_projection(project) is None
    # No NEW skill-view file: these fillers are not reclaimable (plain specs), so
    # the fallback's prune leaves them and mints nothing. The metadata/lock dirs
    # the prune may create are not skill-view files, so they do not count.
    assert _skill_views() == before
    # Same code path as KIROCREW_NATIVE_SKILL_PROJECTION=0: no overlay written
    # because no cli.json existed to roll back.
    assert not (project / ".kiro").exists()
    fallback = [r for r in caplog.records if "fall back to authored agents" in r.message]
    assert fallback
    # The warning says the count spans every home's views (the conductor's
    # option-a choice) and points at the doctor's foreign-file remedy.
    assert "regardless of which Kiro Crew home" in fallback[0].message
    assert "doctor" in fallback[0].message


def test_the_ceiling_fallback_warns_once_per_process(
    native_tree, monkeypatch, _reset_ceiling_state, caplog
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 3)
    _fill_agents_dir_with_views(agents, 3)
    with caplog.at_level(logging.WARNING, logger=projection.logger.name):
        for _ in range(4):
            assert projection.prepare_native_skill_projection(project) is None
    fallback_warnings = [r for r in caplog.records if "fall back to authored agents" in r.message]
    assert len(fallback_warnings) == 1
    # The warning qualifies the fallback as spawn-time only: new spawns stop
    # projecting, but a session already projecting keeps refreshing its own view.
    assert "new spawns" in fallback_warnings[0].message


def test_explicit_enabled_true_bypasses_the_ceiling(native_tree, monkeypatch, _reset_ceiling_state):
    """A warm-runtime refresh (explicit ``enabled=True``) must still prepare a view.

    The shared runtime re-prepares a live session's view with ``enabled=True`` at
    set_mode, and treats a ``None`` return as fatal (it raises AcpRuntimeError and
    refuses the start). The ceiling is a spawn-time admission decision only, so an
    explicit ``enabled=True`` must never be converted into the authored-agent
    fallback -- otherwise a backlog would abort every warm session.
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 3)
    _fill_agents_dir_with_views(agents, 10)  # well over the ceiling
    prepared = projection.prepare_native_skill_projection(project, enabled=True)
    assert prepared is not None
    assert (agents / f"{prepared.agent('custom')}.json").exists()


def test_the_ceiling_fallback_still_prunes_so_the_backlog_can_drain(
    native_tree, monkeypatch, _reset_ceiling_state
):
    """Above the ceiling, the fallback must still run the per-spawn prune.

    The safety net sets enabled=False, but it must not switch off the very
    reclaim that recovers from the backlog: this home's own stale aliases are
    pruned on the fallback path, so the count can fall back under the ceiling
    across spawns without a gateway restart (design-lane Watch item).
    """
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 3)
    # Reclaimable legacy aliases this home owns, aged past the reclaim floor.
    backlog = [_legacy_alias(agents, digest=f"{n:024x}") for n in range(5)]
    assert projection._count_projected_aliases(agents) >= 3  # over the ceiling
    # Fallback path (no view minted), but the prune still runs under the lock.
    assert projection.prepare_native_skill_projection(project) is None
    reclaimed = [p for p in backlog if not p.exists()]
    assert reclaimed, "the ceiling fallback did not prune this home's backlog"


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
