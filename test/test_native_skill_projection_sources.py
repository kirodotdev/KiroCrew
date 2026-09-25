"""The skill projection reads the SAME files the control-plane mount reads.

``session_mcp.kiro_control_plane_servers`` withholds ``kirocrew-core``'s
per-session element when ANY declaration the session is subject to -- the agent
spec's entry, the global ``mcp.json`` the dashboard's tool toggle writes, or the
per-project ``mcp.json`` -- carries a restriction the element cannot express.
``prepare_native_skill_projection`` decides whether an agent may rely on that
element. A projection that reads only the spec leaves the agent promised an
element the mount then withholds, and every session of that agent fails at
``session/new`` with an error pointing at the wrong file. These tests pin that
both readers answer from one predicate over one source set, and that a withheld
element refuses the agent at preparation with a message naming the file, the
restriction and the way back.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.acp import session_mcp
from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime import AcpRuntimeError

MANAGED = {"command": "test-core", "args": []}
# The shape ``/api/mcp/toggle-tool`` writes into the global settings: the managed
# launch, an explicit ``disabled: false``, and the one list that carries the toggle.
TOGGLED = {**MANAGED, "disabled": False, "disabledTools": ["learn_add"]}


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A kiro home, a project and the global settings file, all under tmp.

    ``kirocrew`` is a search agent by name; ``plain`` maps no skill and never asks
    for the element, so it shows which agents a restriction touches.
    """
    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    settings = home / "settings" / "mcp.json"
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr("kiro_crew.agent.managed_mcp_spec_entry", lambda name, **_kw: dict(MANAGED))
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", settings)
    monkeypatch.setattr(session_mcp, "managed_mcp_spec_entry", lambda name, **_kw: dict(MANAGED))
    monkeypatch.setattr(session_mcp, "_registry_mode", lambda: False)
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("kirocrew", "plain")
        ],
    )
    specs = {
        "kirocrew": {"name": "kirocrew", "tools": ["*"], "mcpServers": {"kirocrew-core": MANAGED}},
        "plain": {"name": "plain", "resources": ["file://RULES.md"]},
    }
    for name, spec in specs.items():
        (agents / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setattr(session_mcp, "_agent_spec_for", lambda agent, *a, **k: specs.get(agent))
    return SimpleNamespace(agents=agents, project=project, settings=settings, specs=specs)


def _write(path: Path, core: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"kirocrew-core": core}}), encoding="utf-8")


def _project_settings(tree) -> Path:
    return tree.project / ".kiro" / "settings" / "mcp.json"


async def _session_refusal(prepared, tree, entries=()) -> str:
    """The message a session of ``kirocrew`` is refused with on a warm runtime."""
    from test_acp_runtime import _make_runtime

    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    with pytest.raises(AcpRuntimeError) as refused:
        await runtime._unpooled_control_planes(list(entries), "kirocrew", tree.project)
    return str(refused.value)


def _mount(prepared, tree, entries=()) -> session_mcp.NativeControlPlaneMount:
    """What the mount yields for a ``kirocrew`` session over the view, as the runtime asks it."""
    return session_mcp.kiro_control_plane_servers(
        "kirocrew",
        work_dir=tree.project,
        existing_names={entry["name"] for entry in entries},
        spec_override=prepared.specs.get("kirocrew"),
    )


def _withheld(prepared, tree) -> str | None:
    """The sentence the mount's one read withholds ``kirocrew-core``'s element with."""
    verdict = _mount(prepared, tree).withheld.get("kirocrew-core")
    return None if verdict is None else verdict.explain("skill search")


@pytest.mark.asyncio
async def test_a_tool_disabled_in_the_global_settings_refuses_the_search_agents_sessions_by_name(
    tree,
):
    """The toggle is written into a file every agent of the process shares, so it
    refuses the search agent's SESSIONS, naming the file -- never the spawn, which
    every other agent's sessions ride on, and never the other agents."""
    _write(tree.settings, TOGGLED)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared is not None
    # The view stands: the spec is clean, so the spawn goes ahead under the alias.
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    assert (tree.agents / f"{prepared.agent('kirocrew')}.json").exists()
    reason = _withheld(prepared, tree)
    assert reason is not None
    assert reason.startswith("kirocrew-core is withheld by the global MCP settings")
    assert str(tree.settings) in reason
    assert "disabledTools lists learn_add" in reason
    assert "dashboard's MCP tab" in reason and reason.endswith("to restore skill search")
    assert await _session_refusal(prepared, tree) == f"Agent 'kirocrew': {reason}"
    # The agent that never asked for the element is untouched by the restriction.
    assert (tree.agents / f"{prepared.agent('plain')}.json").exists()
    assert "plain" not in prepared.search_agents


@pytest.mark.asyncio
async def test_a_tool_disabled_in_the_project_settings_refuses_the_search_agents_sessions(tree):
    _write(_project_settings(tree), TOGGLED)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"}
    reason = _withheld(prepared, tree)
    assert reason.startswith("kirocrew-core is withheld by the project's MCP settings")
    assert str(_project_settings(tree)) in reason and "learn_add" in reason
    assert await _session_refusal(prepared, tree) == f"Agent 'kirocrew': {reason}"


@pytest.mark.parametrize(
    "core, remedy",
    [
        (TOGGLED, "remove the disabledTools entry there"),
        ({**MANAGED, "disabled": True}, "set disabled to false there"),
    ],
    ids=["disabledTools", "mute"],
)
def test_the_dashboard_is_the_way_back_only_for_the_file_the_dashboard_writes(tree, core, remedy):
    """The MCP tab writes the global settings and nothing else, so a restriction in
    the project's file or the spec is offered only the edit that removes it there;
    a remedy that sent the user to the tab would leave the restriction in place and
    refuse the next session again."""
    tab = session_mcp._NATIVE_REMEDY_DASHBOARD
    _write(tree.settings, core)
    from_global = session_mcp.native_mount_withholding(
        "kirocrew-core", MANAGED, session_mcp.native_settings_sources(tree.project)
    )
    assert from_global is not None and from_global.remedy.startswith("re-enable")
    assert tab in from_global.remedy and from_global.remedy.endswith(remedy)
    tree.settings.unlink()
    _write(_project_settings(tree), core)
    from_project = session_mcp.native_mount_withholding(
        "kirocrew-core", MANAGED, session_mcp.native_settings_sources(tree.project)
    )
    assert from_project is not None and from_project.source.startswith(
        session_mcp.NATIVE_SOURCE_PROJECT
    )
    assert tab not in from_project.remedy and from_project.remedy.endswith(remedy)
    from_spec = session_mcp.native_mount_withholding("kirocrew-core", core, [])
    assert from_spec is not None and from_spec.source == session_mcp.NATIVE_SOURCE_SPEC
    assert tab not in from_spec.remedy and from_spec.remedy.endswith(remedy)
    assert "dashboard" not in from_project.remedy and "dashboard" not in from_spec.remedy


@pytest.mark.parametrize(
    "core, named",
    [
        ({**MANAGED, "disabled": True}, "it is disabled"),
        ({**MANAGED, "disabled": "yes"}, "it is disabled"),
        ({**MANAGED, "type": "http"}, "its type is 'http', not stdio"),
        ({**MANAGED, "headers": {"x-key": "k"}}, "its entry carries headers"),
        ("kirocrew", "its entry is not a server object"),
    ],
)
def test_every_restriction_the_mount_knows_refuses_the_session_with_its_name(tree, core, named):
    _write(tree.settings, core)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"}
    reason = _withheld(prepared, tree)
    assert named in reason and "the global MCP settings" in reason
    assert _mount(prepared, tree).elements == []


@pytest.mark.parametrize(
    "core, reason",
    [
        (
            {**MANAGED, "disabledTools": ["learn_add"]},
            "kirocrew-core is withheld by the agent spec",
        ),
        # A mute is caught by the spec-shape check ahead of the predicate.
        ({**MANAGED, "disabled": True}, "skill_search is disabled"),
        ({**MANAGED, "type": "http"}, "kirocrew-core is withheld by the agent spec"),
    ],
)
def test_a_restriction_authored_in_the_spec_entry_refuses_the_view_at_preparation(
    tree, core, reason
):
    """The spec is this preparation's input and a restriction there is static for
    the view's life, so the view is refused now, naming the spec."""
    spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert "kirocrew" not in prepared.search_agents
    assert prepared.errors["kirocrew"].startswith(reason)
    with pytest.raises(ValueError, match=reason.split(" is ")[0]):
        prepared.agent("kirocrew")


@pytest.mark.parametrize("where", ["spec", "global", "project"])
def test_a_timeout_on_the_entry_keeps_the_declaration_native_and_names_the_key(tree, where):
    """The element replaces the declaration it is named after and carries no
    ``timeout`` (``acp_server_element`` emits none), so mounting it would run the
    server on the default timeout with nothing to say so: the declaration stays
    native, the element is withheld, and the verdict names the key -- from the
    spec at preparation, from a settings file at that session's start."""
    core = {**MANAGED, "timeout": 60000}
    element = session_mcp.acp_server_element("kirocrew-core", core)
    assert element is not None and "timeout" not in element  # the reason it withholds
    reason = "kirocrew-core is withheld by "
    restriction = "its entry carries timeout, which a per-session element cannot express"
    if where == "spec":
        spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
        (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
        tree.specs["kirocrew"] = spec
        prepared = projection.prepare_native_skill_projection(tree.project)
        assert "kirocrew" not in prepared.search_agents
        assert prepared.errors["kirocrew"].startswith(reason + session_mcp.NATIVE_SOURCE_SPEC)
        assert restriction in prepared.errors["kirocrew"]
        return
    path = tree.settings if where == "global" else _project_settings(tree)
    label = (
        session_mcp.NATIVE_SOURCE_GLOBAL if where == "global" else session_mcp.NATIVE_SOURCE_PROJECT
    )
    _write(path, core)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    withheld = _withheld(prepared, tree)
    assert withheld is not None and withheld.startswith(f"{reason}{label} (")
    assert restriction in withheld and str(path) in withheld
    assert _mount(prepared, tree).elements == []


def test_the_element_key_table_is_the_spec_rebuilds_minus_what_the_element_cannot_carry():
    """One table for "what a managed entry may carry", read from the spec rebuild,
    less the one key in it that ``acp_server_element`` leaves behind."""
    table = session_mcp._agent_mod._MANAGED_MCP_ENTRY_KEYS
    assert "timeout" in table
    assert session_mcp._NATIVE_ELEMENT_KEYS == table - {"timeout"}
    carried = set(session_mcp.acp_server_element("kirocrew-core", {**MANAGED, "env": {"A": "1"}}))
    # Every key the element emits from a declaration is one the table admits.
    assert carried - {"name"} <= session_mcp._NATIVE_ELEMENT_KEYS


def test_the_registry_marker_on_the_spec_entry_is_crews_own_and_restricts_nothing(tree):
    """``agent.mcp_registry_mode`` stamps ``type: registry`` on every managed spec
    entry; there it is a marker the rebuild owns, not a transport a user chose."""
    core = {**MANAGED, "type": session_mcp._KIRO_REGISTRY_TYPE}
    spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    tree.specs["kirocrew"] = spec
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    assert prepared.agent("kirocrew")  # the spawn has an alias to run
    assert _withheld(prepared, tree) is None
    # A transport a user chose is still judged.
    _write(tree.settings, {**MANAGED, "type": "sse"})
    assert "its type is 'sse', not stdio" in _withheld(prepared, tree)


@pytest.mark.parametrize("where", ["global", "project"])
def test_the_registry_marker_in_a_settings_file_keeps_the_declaration_native(tree, where):
    """No Crew writer puts the marker in a settings file, so there it is a
    declaration a user or an admin wrote -- and outside registry mode kiro-cli
    drops a marked entry. Mounting the element for it would launch a server
    kiro-cli refuses, so the declaration stays native and the session is refused
    naming the file and the type."""
    core = {**MANAGED, "type": session_mcp._KIRO_REGISTRY_TYPE}
    path = tree.settings if where == "global" else _project_settings(tree)
    _write(path, core)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    withheld = _withheld(prepared, tree)
    assert withheld is not None and str(path) in withheld
    assert "its type is 'registry', not stdio" in withheld
    assert _mount(prepared, tree).elements == []


@pytest.mark.parametrize(
    "core, named",
    [
        ({**MANAGED, "type": "http"}, "its type is 'http', not stdio"),
        ({**MANAGED, "headers": {"x-key": "k"}}, "its entry carries headers"),
    ],
)
def test_the_spec_entry_is_judged_as_authored_not_as_the_managed_launch_replaces_it(
    tree, core, named
):
    """A ``type`` or a key the element cannot carry is not copied onto the managed
    replacement, so judging the replacement would let it vanish unjudged and the
    control plane mount despite the restriction the mount's own arms read from
    the declaration itself. The authored declaration is what is judged."""
    spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    tree.specs["kirocrew"] = spec  # what the mount reads with no view to override it
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert "kirocrew" not in prepared.search_agents
    assert "kirocrew" not in prepared.specs
    reason = prepared.errors["kirocrew"]
    assert reason.startswith("kirocrew-core is withheld by the agent spec") and named in reason
    assert _mount(prepared, tree).elements == []


@pytest.mark.parametrize(
    "core",
    [
        None,
        {**MANAGED, "disabled": False, "disabledTools": []},
        # A stale launch is repaired to the managed one, never a restriction.
        {"command": "kirocrew", "args": ["mcp-core"]},
    ],
)
def test_no_restriction_anywhere_leaves_the_search_agent_exactly_as_before(tree, core):
    if core is not None:
        _write(tree.settings, core)
        _write(_project_settings(tree), core)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    view = json.loads((tree.agents / f"{prepared.agent('kirocrew')}.json").read_text("utf-8"))
    assert view["mcpServers"]["kirocrew-core"] == MANAGED
    assert _mount(prepared, tree).elements[0]["name"] == "kirocrew-core"


@pytest.mark.asyncio
async def test_settings_that_cannot_be_read_refuse_the_search_agents_sessions_by_file(tree):
    tree.settings.parent.mkdir(parents=True)
    tree.settings.write_text("{", encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    reason = _withheld(prepared, tree)
    assert reason.startswith(f"the global MCP settings ({tree.settings}) could not be read safely")
    assert reason.endswith("to restore skill search")
    assert await _session_refusal(prepared, tree) == f"Agent 'kirocrew': {reason}"
    assert _mount(prepared, tree).elements == []


@pytest.mark.asyncio
async def test_a_session_of_an_agent_the_spec_refused_reaches_no_guard(tree):
    """The ``session/new`` guard is unreachable once the projection has refused."""
    from test_acp_runtime import _make_runtime

    spec = {
        **tree.specs["kirocrew"],
        "mcpServers": {"kirocrew-core": {**MANAGED, "disabledTools": ["learn_add"]}},
    }
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    tree.specs["kirocrew"] = spec
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert "kirocrew" not in prepared.search_agents
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    try:
        servers = await runtime._unpooled_control_planes([], "kirocrew", tree.project)
    except AcpRuntimeError as exc:  # pragma: no cover - the defect this module pins
        pytest.fail(f"session start raised for a restriction the projection can read: {exc}")
    assert servers == []


STUB = {"name": "kirocrew-core", "command": "stub", "args": [], "env": [], "type": "stdio"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction, named",
    [
        pytest.param(
            lambda tree: _write(tree.settings, TOGGLED),
            "the global MCP settings",
            id="global-toggle",
        ),
        pytest.param(
            lambda tree: _write(_project_settings(tree), {**MANAGED, "disabled": True}),
            "the project's MCP settings",
            id="project-mute",
        ),
    ],
)
async def test_a_broker_stub_in_the_array_does_not_carry_a_restricted_agents_session(
    tree, restriction, named
):
    """The sources decide; a stub ``session/new`` would mount is not one of them.

    A stub element can carry a kiro-cli-only restriction no better than the native
    element can, and the overlay it comes from is written once, from the spec and
    the global file as they were then, so a stub's presence in the array is no
    evidence that the restriction reaches the session. The runtime asks the files
    before it accepts the stub, and refuses the session with the file's name.
    """
    restriction(tree)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"}
    message = await _session_refusal(prepared, tree, entries=[dict(STUB)])
    assert message.startswith(f"Agent 'kirocrew': kirocrew-core is withheld by {named}")
    assert "Cannot bind skill_search" not in message


@pytest.mark.asyncio
async def test_a_broker_stub_carries_the_session_while_the_files_allow_the_element(tree):
    from test_acp_runtime import _make_runtime

    prepared = projection.prepare_native_skill_projection(tree.project)
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    servers = await runtime._unpooled_control_planes([dict(STUB)], "kirocrew", tree.project)
    assert servers == [STUB]


def _corrupt(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{", encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "edit, named",
    [
        pytest.param(
            lambda tree: _write(tree.settings, TOGGLED),
            ("the global MCP settings", "disabledTools lists learn_add", "dashboard's MCP tab"),
            id="global-toggle",
        ),
        pytest.param(
            lambda tree: _write(_project_settings(tree), {**MANAGED, "disabled": True}),
            ("the project's MCP settings", "it is disabled"),
            id="project-mute",
        ),
        pytest.param(
            lambda tree: _corrupt(tree.settings),
            ("the global MCP settings", "could not be read safely"),
            id="global-unreadable",
        ),
    ],
)
async def test_a_restriction_written_after_the_spawn_refuses_the_next_session_by_its_file(
    tree, edit, named
):
    """The settings files outlive a spawn, so they are judged at every session start.

    The dashboard's tool toggle writes the global one while a runtime is warm. A
    session started after the edit is refused -- the view already dropped the
    agent's skill resources on the element's promise, and without the element
    ``kirocrew-core`` mounts natively with no identity -- with the sentence that
    names the file and the way back, never the wrong-file guard; the spawn and the
    other agents' sessions stand.
    """
    from test_acp_runtime import _make_runtime

    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"}
    assert _withheld(prepared, tree) is None
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    first = await runtime._unpooled_control_planes([], "kirocrew", tree.project)
    assert [entry["name"] for entry in first] == ["kirocrew-core"]
    edit(tree)
    with pytest.raises(AcpRuntimeError) as refused:
        await runtime._unpooled_control_planes([], "kirocrew", tree.project)
    message = str(refused.value)
    assert message.startswith("Agent 'kirocrew': ")
    for phrase in named:
        assert phrase in message
    assert message.endswith("to restore skill search")
    assert "Cannot bind skill_search" not in message and "server configuration" not in message
    # One sentence, whichever reader reaches it: the mount withholds on the same
    # files, and a fresh preparation still grants the view (the spec is clean) and
    # answers the session question identically.
    assert _mount(prepared, tree).elements == []
    fresh = projection.prepare_native_skill_projection(tree.project)
    assert fresh.search_agents == {"kirocrew"} and fresh.errors == {}
    assert message == f"Agent 'kirocrew': {_withheld(fresh, tree)}"


@pytest.mark.asyncio
async def test_a_stub_ahead_of_the_element_does_not_carry_a_session_started_after_the_edit(tree):
    """The stub array comes from an overlay written before the edit, so the stub
    is exactly what a restriction written since cannot have reached; the files are
    asked before it is accepted, and the session is refused with the file's name."""
    from test_acp_runtime import _make_runtime

    prepared = projection.prepare_native_skill_projection(tree.project)
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    first = await runtime._unpooled_control_planes([dict(STUB)], "kirocrew", tree.project)
    assert first == [STUB]
    _write(tree.settings, TOGGLED)
    with pytest.raises(AcpRuntimeError) as refused:
        await runtime._unpooled_control_planes([dict(STUB)], "kirocrew", tree.project)
    message = str(refused.value)
    assert message.startswith(
        "Agent 'kirocrew': kirocrew-core is withheld by the global MCP settings"
    )
    assert "disabledTools lists learn_add" in message
    # Undoing the toggle restores the next session without a respawn.
    _write(tree.settings, MANAGED)
    again = await runtime._unpooled_control_planes([dict(STUB)], "kirocrew", tree.project)
    assert again == [STUB]


def test_the_session_start_guard_asks_the_projection_and_both_session_starts_reach_it():
    """Structural pin: the guard on the one seam create and load share refuses on
    the mount's OWN verdict -- no second read of the files, no re-ask through the
    projection -- and it is not nested under the stub acceptance."""
    from kiro_crew.acp import runtime as runtime_mod

    guard = inspect.getsource(runtime_mod.AcpRuntime._unpooled_control_planes)
    assert 'mount.withheld.get("kirocrew-core")' in guard
    assert "withholding" not in guard and "native_settings_sources" not in guard
    assert "for entry in [*entries, *native]" not in guard
    for entry_point in (runtime_mod.AcpRuntime.create_session, runtime_mod.AcpRuntime.load_session):
        assert "_unpooled_control_planes(" in inspect.getsource(entry_point)
    # The projection has no settings-reading seam left for a guard to reach for.
    assert not hasattr(projection.NativeSkillProjection, "withholding")


def test_the_mount_and_the_projection_ask_one_predicate_with_one_source_set(tree, monkeypatch):
    """Structural pin: one definition, both callers, the same inputs.

    Preparation asks over the spec alone (no settings source); the mount asks
    over the view's entry and the source set, once per session start, and hands
    that one verdict on."""
    seen: list[tuple[str, object, list[str]]] = []

    def recorder(name, spec_entry, settings):
        labels = [source.label for source in settings]
        seen.append((name, spec_entry, labels))
        if not labels:
            return None
        return session_mcp.NativeMountWithholding(name, "a test source", "a test reason", "undo it")

    monkeypatch.setattr(session_mcp, "native_mount_withholding", recorder)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"}
    assert seen == [("kirocrew-core", MANAGED, [])]
    mount = _mount(prepared, tree)
    assert mount.elements == []
    assert mount.withheld["kirocrew-core"].explain("skill search") == (
        "kirocrew-core is withheld by a test source: a test reason; undo it to restore skill search"
    )
    assert seen[1:] == [
        (
            "kirocrew-core",
            MANAGED,
            [session_mcp.NATIVE_SOURCE_GLOBAL, session_mcp.NATIVE_SOURCE_PROJECT],
        )
    ]


def test_neither_reader_keeps_a_private_copy_of_the_merge_or_the_predicate():
    mount = inspect.getsource(session_mcp.kiro_control_plane_servers)
    assert "native_settings_sources(" in mount and "native_mount_withholding(" in mount
    for private in (
        '"disabledTools"',
        "mcp_entry_is_muted(",
        "_global_settings(",
        "_read_mcp_settings(",
    ):
        assert private not in mount
    projected = inspect.getsource(projection)
    assert "session_mcp.native_mount_withholding(" in projected
    # The mount is the ONE reader of the settings files: the projection judges the
    # spec entry and reads none of them, by any route.
    assert "native_settings_sources" not in projected
    for private in ("_global_settings", "_read_mcp_settings", "mcp_entry_is_muted", "mcp.json"):
        assert private not in projected


def test_settings_sources_are_the_global_file_then_the_project_file(tree):
    _write(tree.settings, TOGGLED)
    _write(_project_settings(tree), MANAGED)
    sources = session_mcp.native_settings_sources(tree.project)
    assert [(s.label, s.path) for s in sources] == [
        (session_mcp.NATIVE_SOURCE_GLOBAL, tree.settings),
        (session_mcp.NATIVE_SOURCE_PROJECT, _project_settings(tree)),
    ]
    assert sources[0].servers["kirocrew-core"] == TOGGLED
    assert [s.label for s in session_mcp.native_settings_sources(None)] == [
        session_mcp.NATIVE_SOURCE_GLOBAL
    ]
    # Absence is "no restrictions", and never a source that fails the read.
    tree.settings.unlink()
    assert session_mcp.native_settings_sources(tree.project)[0].servers == {}


def test_an_unreadable_settings_file_is_named_with_its_cause(tree):
    _write(tree.settings, TOGGLED)
    _project_settings(tree).parent.mkdir(parents=True)
    _project_settings(tree).write_text("[]", encoding="utf-8")
    with pytest.raises(session_mcp.NativeSettingsUnreadable) as caught:
        session_mcp.native_settings_sources(tree.project)
    assert caught.value.label == session_mcp.NATIVE_SOURCE_PROJECT
    assert caught.value.path == _project_settings(tree)
    assert isinstance(caught.value.__cause__, ValueError)
    assert isinstance(caught.value, ValueError)


def test_the_message_bounds_a_long_tool_list_and_types_a_malformed_one():
    many = [f"tool_{n}" for n in range(11)]
    withheld = session_mcp.native_mount_withholding(
        "kirocrew-core",
        {**MANAGED, "disabledTools": many},
        [session_mcp.NativeSettingsSource("unused", Path("unused"), {})],
    )
    assert withheld is not None
    assert "lists tool_0, tool_1, tool_2, tool_3, tool_4, tool_5, tool_6, tool_7 and 3 more" in (
        withheld.restriction
    )
    malformed = session_mcp.native_mount_withholding(
        "kirocrew-core", {**MANAGED, "disabledTools": "skill_search"}, []
    )
    assert malformed is not None and "is 'skill_search', not a list of tool names" in (
        malformed.restriction
    )
    assert session_mcp.native_mount_withholding("kirocrew-core", MANAGED, []) is None


def _cut(text: str) -> str:
    """What the message repeats of one value: the bound's worth, ending in ``...``."""
    bound = session_mcp._NATIVE_NAMED_CHARS
    return text if len(text) <= bound else text[: bound - 3] + "..."


def _ceiling() -> int:
    # The message is RETAINED per agent for the life of the runtime, and the file
    # it repeats is read up to the reader's 50 MB ceiling: the count bound alone
    # bounds how many values it repeats, not how long each one is.
    return session_mcp._NATIVE_NAMED_TOOLS * (session_mcp._NATIVE_NAMED_CHARS + 2) + 256


HUGE = 100_000


@pytest.mark.parametrize(
    "core, restriction",
    [
        pytest.param(
            {**MANAGED, "disabledTools": "x" * HUGE},
            lambda: f"its disabledTools is {_cut(repr('x' * HUGE))}, not a list of tool names",
            id="one-huge-string-where-a-list-should-be",
        ),
        pytest.param(
            {**MANAGED, "disabledTools": [str(n) * HUGE for n in range(8)]},
            lambda: "its disabledTools lists " + ", ".join(_cut(str(n) * HUGE) for n in range(8)),
            id="eight-huge-names",
        ),
        pytest.param(
            {**MANAGED, "disabledTools": [str(n) * HUGE for n in range(9)]},
            lambda: "its disabledTools lists "
            + ", ".join(_cut(str(n) * HUGE) for n in range(8))
            + " and 1 more",
            id="huge-names-past-the-count",
        ),
        pytest.param(
            {**MANAGED, "disabledTools": [None, "y" * HUGE]},
            lambda: f"its disabledTools is {_cut(repr([None, 'y' * HUGE]))}, not a list of tool"
            " names",
            id="a-huge-list-that-is-not-all-names",
        ),
        pytest.param(
            {**MANAGED, "k" * HUGE: 1},
            lambda: f"its entry carries {_cut('k' * HUGE)}, which a per-session element cannot"
            " express",
            id="one-huge-key",
        ),
        pytest.param(
            {**MANAGED, **{f"key{n:02d}" + "k" * HUGE: 1 for n in range(20)}},
            lambda: "its entry carries "
            + ", ".join(_cut(f"key{n:02d}" + "k" * HUGE) for n in range(8))
            + " and 12 more, which a per-session element cannot express",
            id="many-huge-keys",
        ),
        pytest.param(
            {**MANAGED, "type": "t" * HUGE},
            lambda: f"its type is {_cut(repr('t' * HUGE))}, not stdio",
            id="a-huge-transport",
        ),
    ],
)
def test_every_value_the_message_repeats_is_cut_to_the_named_character_bound(core, restriction):
    withheld = session_mcp.native_mount_withholding("kirocrew-core", core, [])
    assert withheld is not None
    assert len(withheld.explain("skill search")) <= _ceiling()
    assert withheld.restriction == restriction()
    assert "..." in withheld.restriction
    assert len(_cut("x" * HUGE)) == session_mcp._NATIVE_NAMED_CHARS


def test_a_value_within_the_bound_is_repeated_whole():
    name = "n" * session_mcp._NATIVE_NAMED_CHARS
    withheld = session_mcp.native_mount_withholding(
        "kirocrew-core", {**MANAGED, "disabledTools": [name]}, []
    )
    assert withheld is not None and withheld.restriction == f"its disabledTools lists {name}"


FORGERY = "learn_add\n[ERROR] forged line\x1b[31m red\x1b[0m\r\ndone"


@pytest.mark.parametrize(
    "core, repeated",
    [
        pytest.param(
            {**MANAGED, "disabledTools": [FORGERY]},
            "its disabledTools lists learn_add [ERROR] forged line[31m red[0m done",
            id="a-tool-name",
        ),
        pytest.param(
            {**MANAGED, "k\r\n[ERROR] forged\x1bz": 1},
            "its entry carries k [ERROR] forgedz, which a per-session element cannot express",
            id="a-key",
        ),
        pytest.param(
            {**MANAGED, "disabledTools": ["AKIAIOSFODNN7EXAMPLE"]},
            None,
            id="a-credential-shaped-name",
        ),
    ],
)
def test_the_message_repeats_a_value_cleaned_never_as_the_file_spells_it(core, repeated):
    """The value is text from a settings file -- the project's is checked out with
    the repository -- and the message reaches the gateway's log, so a newline or an
    escape sequence in it would write a log line of its own. Every value goes
    through the package's one sink cleaner before it is cut: control characters
    are dropped, a credential is redacted, and neither survives to the message."""
    withheld = session_mcp.native_mount_withholding("kirocrew-core", core, [])
    assert withheld is not None
    sentence = withheld.explain("skill search")
    assert "\n" not in sentence and "\r" not in sentence and "\x1b" not in sentence
    assert all(ch.isprintable() for ch in sentence)
    if repeated is None:
        assert "AKIAIOSFODNN7EXAMPLE" not in sentence and "[REDACTED" in withheld.restriction
    else:
        assert withheld.restriction == repeated


def test_the_cleaner_reads_a_window_and_the_cut_still_ends_in_the_marker():
    """Cleaning runs over the first :data:`_NATIVE_NAMED_WINDOW` characters whole and
    the cut follows it, so a credential is never split by the cut and a huge value
    costs the reader a window rather than the file; a value the window did not
    hold whole is a cut value, whatever the cleaner left of it."""
    window, bound = session_mcp._NATIVE_NAMED_WINDOW, session_mcp._NATIVE_NAMED_CHARS
    assert window >= 4 * bound
    padded = session_mcp.native_mount_withholding(
        "kirocrew-core", {**MANAGED, "disabledTools": ["\n" * 50 + "x" * HUGE]}, []
    )
    assert (
        padded is not None and padded.restriction == f"its disabledTools lists {_cut('x' * HUGE)}"
    )
    hollow = session_mcp.native_mount_withholding(
        "kirocrew-core", {**MANAGED, "disabledTools": ["a" * 10 + "\x1b" * (2 * window)]}, []
    )
    assert hollow is not None and hollow.restriction == "its disabledTools lists aaaaaaaaaa..."
    assert len(hollow.explain("skill search")) <= _ceiling()


def test_a_settings_path_is_repeated_cleaned_in_the_source_and_the_unreadable_error():
    path = Path("/work/x\x1b[31m\nevil/.kiro/settings/mcp.json")
    source = session_mcp.NativeSettingsSource(session_mcp.NATIVE_SOURCE_PROJECT, path, {})
    unreadable = session_mcp.NativeSettingsUnreadable(session_mcp.NATIVE_SOURCE_PROJECT, path)
    for text in (source.named, str(unreadable), unreadable.explain("skill search")):
        assert "\n" not in text and "\x1b" not in text and all(ch.isprintable() for ch in text)
        assert "evil/.kiro/settings/mcp.json" in text
    assert unreadable.path == path


@pytest.mark.parametrize(
    "core, restriction, action, edit",
    [
        (
            {**MANAGED, "disabled": True},
            "it is disabled",
            "re-enable the server",
            "set disabled to false there",
        ),
        (
            TOGGLED,
            "its disabledTools lists learn_add",
            "re-enable those tools",
            "remove the disabledTools entry there",
        ),
    ],
    ids=["mute", "disabledTools"],
)
def test_the_remedy_reads_as_one_sentence_with_and_without_the_dashboard(
    tree, core, restriction, action, edit
):
    """Two ways back are joined by ``or``; one way back stands alone. Neither arm
    runs two clauses together."""
    tab = session_mcp._NATIVE_REMEDY_DASHBOARD
    _write(tree.settings, core)
    from_global = session_mcp.native_mount_withholding(
        "kirocrew-core", MANAGED, session_mcp.native_settings_sources(tree.project)
    )
    assert from_global is not None and from_global.remedy == f"{action} {tab}, or {edit}"
    assert from_global.explain("skill search") == (
        f"kirocrew-core is withheld by {from_global.source}: {restriction};"
        f" {action} {tab}, or {edit} to restore skill search"
    )
    from_spec = session_mcp.native_mount_withholding("kirocrew-core", core, [])
    assert from_spec is not None and from_spec.remedy == edit
    assert from_spec.explain("skill search") == (
        f"kirocrew-core is withheld by the agent spec: {restriction}; {edit} to restore skill search"
    )


@pytest.mark.parametrize(
    "where", ["global-marker", "project-marker", "spec-derived-record", "both-keys"]
)
def test_crews_own_bookkeeping_keys_on_an_entry_are_not_restrictions(tree, where):
    """The global-file sync stamps the entries it authors with the provenance
    marker and the spec rebuild records derived fields; kiro-cli reads neither.
    An element that omits them expresses everything the declaration says, so
    they must not refuse the default agent for Crew's own stamp."""
    from kiro_crew import mcp_provenance

    marked = {**MANAGED, mcp_provenance.MARKER_KEY: {"managed": True}}
    derived = {**MANAGED, mcp_provenance.DERIVED_KEY: {"from": "kirocrew", "emitted": "test-core"}}
    if where == "global-marker":
        _write(tree.settings, marked)
    elif where == "project-marker":
        _write(_project_settings(tree), marked)
    else:
        spec_core = {**marked, **derived} if where == "both-keys" else derived
        spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": spec_core}}
        (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
        tree.specs["kirocrew"] = spec
        if where == "both-keys":
            _write(tree.settings, marked)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert prepared.search_agents == {"kirocrew"} and prepared.errors == {}
    assert [entry["name"] for entry in _mount(prepared, tree).elements] == ["kirocrew-core"]
    # The stamp is not a licence for anything else on the same entry.
    _write(tree.settings, {**marked, "disabled": True})
    assert "it is disabled" in _withheld(prepared, tree)


def test_a_settings_path_in_a_retained_message_is_cut_from_the_front_keeping_the_file():
    cap = session_mcp._NATIVE_NAMED_PATH_CHARS
    deep = Path("/" + "/".join("d" * 200 for _ in range(30))) / ".kiro" / "settings" / "mcp.json"
    assert len(str(deep)) > cap
    source = session_mcp.NativeSettingsSource(
        session_mcp.NATIVE_SOURCE_PROJECT, deep, {"kirocrew-core": {**MANAGED, "disabled": True}}
    )
    withheld = session_mcp.native_mount_withholding("kirocrew-core", MANAGED, [source])
    assert withheld is not None and withheld.source == source.named
    label = session_mcp.NATIVE_SOURCE_PROJECT
    assert source.named.startswith(f"{label} (") and source.named.endswith(")")
    named_path = source.named[len(label) + 2 : -1]
    assert len(named_path) == cap and named_path.startswith("...")
    assert named_path.endswith(str(Path(".kiro") / "settings" / "mcp.json"))
    unreadable = session_mcp.NativeSettingsUnreadable(session_mcp.NATIVE_SOURCE_PROJECT, deep)
    for text in (str(unreadable), unreadable.explain("skill search")):
        assert named_path in text and str(deep) not in text
    assert unreadable.path == deep  # the full path stays on the exception for a caller
    short_path = Path("/tmp/p/.kiro/settings/mcp.json")  # under the bound: repeated whole
    short = session_mcp.NativeSettingsSource("s", short_path, {})
    assert short.named == f"s ({short_path})"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "core",
    [{**MANAGED, "disabledTools": ["learn_add"]}, {**MANAGED, "type": "http"}],
    ids=["disabledTools", "type"],
)
async def test_a_view_the_spec_refused_fails_the_spawn_as_the_error_the_startup_paths_translate(
    tree, monkeypatch, core
):
    """A restriction authored in the spec refuses the view at preparation, and the
    spawn that would have loaded that view stops there. What it raises is the
    error the provider's startup paths handle around ``spawn()`` --
    ``AcpRuntimeError`` -- carrying the projection's own sentence, so the user
    reads the spec and the remedy instead of an internal failure."""
    from kiro_crew.acp import runtime as runtime_mod
    from kiro_crew.acp.harness import SpawnPlan

    spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    runtime = runtime_mod.AcpRuntime(work_dir=tree.project)

    async def plan() -> SpawnPlan:
        return SpawnPlan(argv=["kiro-cli", "acp", "--agent", "kirocrew"])

    async def no_process(*args, **kwargs):  # pragma: no cover - the refusal comes first
        raise AssertionError("a refused view must not reach the process spawn")

    monkeypatch.setattr(runtime, "_resolve_spawn_plan", plan)
    monkeypatch.setattr(runtime_mod.asyncio, "create_subprocess_exec", no_process)
    with pytest.raises(AcpRuntimeError) as refused:
        await runtime.spawn()
    message = str(refused.value)
    assert message.startswith(
        f"Agent 'kirocrew': kirocrew-core is withheld by {session_mcp.NATIVE_SOURCE_SPEC}: "
    )
    assert message == f"Agent 'kirocrew': {runtime._native_skill_projection.errors['kirocrew']}"
    assert isinstance(refused.value.__cause__, ValueError)
    assert runtime._process is None


@pytest.mark.asyncio
async def test_a_direct_client_session_keeps_an_unrelated_disabled_tool_and_spawns(tree):
    """The direct client runs one kiro-cli process for one session, puts the identity
    on the process environment and mounts ``kirocrew-core`` natively from the view,
    so no per-session element replaces the declaration there: a ``disabledTools``
    naming other tools is honoured by kiro-cli as written and leaves ``skill_search``
    standing. Withholding the view for it -- the shared runtime's element question
    -- refused a supported customization and aborted the spawn with an internal
    ``ValueError``; the spawn must go ahead under the view, restriction included."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from test_acp_client import _stop_stderr_drain
    from test_update_provider import _UNALLOCATABLE_PID

    from kiro_crew.acp.client import AcpClient

    spec = {
        **tree.specs["kirocrew"],
        "tools": ["fs_read"],
        "mcpServers": {"kirocrew-core": {**MANAGED, "disabledTools": ["learn_add"]}},
    }
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    client = AcpClient(work_dir=tree.project, session_key="one-session")
    launched: list[list[str]] = []

    def wrap(argv, *_args, **_kwargs):
        launched.append(list(argv))
        return list(argv), None

    with (
        patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
        patch("kiro_crew.acp.client.wrap_argv", side_effect=wrap),
        patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as exec_mock,
        patch("kiro_crew.session._track_pid"),
        patch("kiro_crew.session._track_session_pid"),
    ):
        process = MagicMock()
        process.pid = _UNALLOCATABLE_PID
        process.returncode = None
        exec_mock.return_value = process
        try:
            await client._spawn()
        finally:
            await _stop_stderr_drain(client)
    prepared = client._native_skill_projection
    assert prepared is not None and prepared.errors == {}
    assert "kirocrew" in prepared.search_agents
    alias = prepared.agent("kirocrew")
    assert launched and launched[0][launched[0].index("--agent") + 1] == alias
    view = json.loads((tree.agents / f"{alias}.json").read_text("utf-8"))
    core = view["mcpServers"]["kirocrew-core"]
    # The restriction reaches the session as written, on the managed launch.
    assert core["disabledTools"] == ["learn_add"] and core["command"] == MANAGED["command"]
    assert projection._SEARCH_TOOL in view["tools"] and "skill_search" not in core["disabledTools"]
    # The same spec on the shared runtime still refuses the view: there the element
    # would replace the declaration and cannot carry the list.
    shared = projection.prepare_native_skill_projection(tree.project)
    assert shared.errors["kirocrew"].startswith("kirocrew-core is withheld by the agent spec")


@pytest.mark.parametrize("where", ["spec-disabledTools", "spec-timeout", "spec-skill_search"])
def test_the_direct_client_still_refuses_a_view_whose_spec_disables_skill_search(tree, where):
    """What the direct client keeps is a restriction that leaves ``skill_search``
    standing; the spec disabling ``skill_search`` itself still refuses the view."""
    core = {
        "spec-disabledTools": {**MANAGED, "disabledTools": ["learn_add", "memory_recall"]},
        "spec-timeout": {**MANAGED, "timeout": 60000},
        "spec-skill_search": {**MANAGED, "disabledTools": ["skill_search"]},
    }[where]
    spec = {**tree.specs["kirocrew"], "mcpServers": {"kirocrew-core": core}}
    (tree.agents / "kirocrew.json").write_text(json.dumps(spec), encoding="utf-8")
    prepared = projection.prepare_native_skill_projection(tree.project, per_session_element=False)
    if where == "spec-skill_search":
        assert prepared.errors["kirocrew"].startswith("skill_search is disabled")
        return
    assert prepared.errors == {} and "kirocrew" in prepared.search_agents
    view = json.loads((tree.agents / f"{prepared.agent('kirocrew')}.json").read_text("utf-8"))
    kept = "disabledTools" if where == "spec-disabledTools" else "timeout"
    assert view["mcpServers"]["kirocrew-core"][kept] == core[kept]


@pytest.mark.parametrize(
    "shape, entries",
    [
        pytest.param("withheld", [], id="withheld"),
        pytest.param("withheld", [dict(STUB)], id="withheld-behind-a-stub"),
        pytest.param("unreadable", [], id="unreadable"),
        pytest.param("allowed", [], id="allowed"),
        pytest.param("allowed", [dict(STUB)], id="allowed-behind-a-stub"),
    ],
)
def test_the_mount_hands_on_the_verdict_it_withheld_on_from_its_one_read(tree, shape, entries):
    """The array and the reason a name is missing from it come from the same read.

    A name a broker stub already carries gets no element either way, but the
    restriction a declaration puts on it is still judged and surfaced -- the
    stub carries a kiro-cli-only restriction no better than the element does, and
    the guard that refuses the session has to learn it from this read."""
    if shape == "withheld":
        _write(tree.settings, TOGGLED)
    elif shape == "unreadable":
        _corrupt(tree.settings)
    prepared = projection.prepare_native_skill_projection(tree.project)
    mount = _mount(prepared, tree, entries)
    assert isinstance(mount, session_mcp.NativeControlPlaneMount)
    if shape == "allowed":
        assert mount.withheld == {}
        assert [e["name"] for e in mount.elements] == ([] if entries else ["kirocrew-core"])
        return
    assert mount.elements == []
    verdict = mount.withheld["kirocrew-core"]
    if shape == "withheld":
        assert isinstance(verdict, session_mcp.NativeMountWithholding)
        assert verdict.source.startswith(session_mcp.NATIVE_SOURCE_GLOBAL)
        assert "learn_add" in verdict.restriction
    else:
        assert isinstance(verdict, session_mcp.NativeSettingsUnreadable)
        # Nothing can be read, so nothing identity-bound is mounted: every name says so.
        assert set(mount.withheld) == set(session_mcp.IDENTITY_BOUND_SERVERS)
    assert verdict.explain("skill search").endswith("to restore skill search")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction, entries",
    [
        pytest.param(None, [], id="allowed"),
        pytest.param(TOGGLED, [], id="withheld"),
        pytest.param(TOGGLED, [dict(STUB)], id="withheld-behind-a-stub"),
        pytest.param(None, [dict(STUB)], id="allowed-behind-a-stub"),
    ],
)
async def test_a_session_start_reads_the_settings_files_exactly_once(
    tree, monkeypatch, restriction, entries
):
    """One read per session start, whatever it answers and whatever the array
    already carries: the mount's. The runtime asks nothing again."""
    from test_acp_runtime import _make_runtime

    if restriction is not None:
        _write(tree.settings, restriction)
    reads: list[object] = []
    read = session_mcp.native_settings_sources

    def counted(work_dir):
        reads.append(work_dir)
        return read(work_dir)

    monkeypatch.setattr(session_mcp, "native_settings_sources", counted)
    prepared = projection.prepare_native_skill_projection(tree.project)
    assert reads == []  # preparation judges the spec and reads no settings file
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    if restriction is None:
        await runtime._unpooled_control_planes(list(entries), "kirocrew", tree.project)
    else:
        with pytest.raises(AcpRuntimeError, match="withheld by the global MCP settings"):
            await runtime._unpooled_control_planes(list(entries), "kirocrew", tree.project)
    assert reads == [tree.project]


@pytest.mark.asyncio
@pytest.mark.parametrize("entries", [[], [dict(STUB)]], ids=["no-stub", "stub"])
async def test_a_toggle_undone_between_two_reads_cannot_send_the_session_to_the_wrong_guard(
    tree, monkeypatch, entries
):
    """The window a second read opened: the mount read the toggle and withheld the
    element; the user undid the toggle; a re-read for the guard answered "allowed",
    and the session fell to the generic guard naming the agent's configuration
    (or, behind a stub, was carried while its array was built under the
    restriction). With one read there is no second answer: the session is
    refused for the verdict the array was built on, naming the file."""
    from test_acp_runtime import _make_runtime

    read = session_mcp.native_settings_sources
    answers: list[list[session_mcp.NativeSettingsSource]] = []

    def toggled_then_undone(work_dir):
        _write(tree.settings, TOGGLED if not answers else MANAGED)
        answers.append(read(work_dir))
        return answers[-1]

    monkeypatch.setattr(session_mcp, "native_settings_sources", toggled_then_undone)
    prepared = projection.prepare_native_skill_projection(tree.project)
    runtime, _, _ = _make_runtime()
    runtime._native_skill_projection = prepared
    with pytest.raises(AcpRuntimeError) as refused:
        await runtime._unpooled_control_planes(list(entries), "kirocrew", tree.project)
    message = str(refused.value)
    assert message.startswith(
        "Agent 'kirocrew': kirocrew-core is withheld by the global MCP settings"
    )
    assert "disabledTools lists learn_add" in message
    assert "Cannot bind skill_search" not in message and "server configuration" not in message
    assert len(answers) == 1
