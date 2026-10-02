"""A resumed conversation resolves the agent its record names, even after a cleanup.

Two records outlive what they name. A crewmate row the startup prune removed is
covered for its basic shape in ``test_chat_agent_selection.py``; the cases here
pin the edges of that fallback. A ``kirocrew-skill-view-<digest>`` name recorded
while the native skill projection was on outlives its view file once a boot
drain or cleanup removes it; resolution maps it back to its source agent through
the projection's ownership sidecar before deciding the turn is unavailable.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from kiro_crew import execution_context, session_agent_selection
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, resolve_agent_bindings
from kiro_crew.session_agent_selection import resolve_session_agent_bindings

AGENT = "docs-author"
VIEW = "kirocrew-skill-view-" + "c" * 24


def _cfg(*, with_row: bool = False) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    if with_row:
        cfg.agents[AGENT] = KiroCrewAgentConfig(kiro_agent=AGENT, source="package")
    cfg.default_agent = "default"
    return cfg


def _stored(*, template: str = AGENT, selection_name: str = AGENT, kind: str = "member"):
    return execution_context.ExecutionContext(
        None,
        execution_context.MemoryStoreRef("default"),
        kind,
        template,
        selection_name=selection_name,
    )


@pytest.fixture
def installed(monkeypatch):
    """The agents whose specs are installed; a test may add or clear names."""
    names = {AGENT}
    monkeypatch.setattr(
        "kiro_crew.config.loader._materialized_kiro_agent",
        lambda name, project_dir=None: name if name in names else "",
    )
    # The fallback applies only to a row the prune recorded as removed.
    monkeypatch.setattr(
        "kiro_crew.crewmate_prune_migration.removed_crewmate_names",
        lambda: frozenset({AGENT, VIEW}),
    )
    return names


def _resume(monkeypatch, cfg, captured, agent_name=AGENT):
    monkeypatch.setattr(session_agent_selection, "read_session_execution", lambda _: captured)
    return resolve_session_agent_bindings(
        resolve_agent_bindings, cfg, "dashboard:chat-1-1", agent_name, "/tmp"
    )


# ── Edges of the pruned-crewmate fallback ──


def test_resume_matches_a_new_chat_on_the_same_agent(monkeypatch, installed):
    cfg = _cfg()
    resumed = _resume(monkeypatch, cfg, _stored())
    fresh = resolve_agent_bindings(cfg, AGENT, "/tmp", validate_memory_files=False)

    assert fresh.requested_resolved and fresh.selection_kind == "template"
    assert resumed.requested_resolved and resumed.selection_kind == fresh.selection_kind
    assert resumed.same_dispatch_binding(fresh)


def test_live_crewmate_row_still_resolves_as_member(monkeypatch, installed):
    bindings = _resume(monkeypatch, _cfg(with_row=True), _stored())

    assert bindings.requested_resolved
    assert bindings.selection_kind == "member"
    assert bindings.resolved_alias == AGENT


def test_renamed_crewmate_is_not_read_as_another_installed_agent(monkeypatch, installed):
    # Name != agent: not a row the prune generated or removes, even when the name
    # happens to be another installed agent.
    installed.add("atlas")
    bindings = _resume(monkeypatch, _cfg(), _stored(selection_name="atlas"), "atlas")

    assert not bindings.requested_resolved


# ── A recorded skill-view name resolves to the agent it was built from ──


@pytest.fixture
def view_sidecar(tmp_path, monkeypatch):
    """A view of AGENT whose view file is gone and whose ownership sidecar is kept."""
    from kiro_crew.acp import skill_projection as projection

    agents = tmp_path / "agents"
    metadata = agents / projection._PROJECTION_METADATA_DIR_NAME
    metadata.mkdir(parents=True)
    monkeypatch.setattr(projection, "_VIEW_SOURCES", {})
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    sidecar = metadata / f"{VIEW}.json"
    sidecar.write_text(
        json.dumps({"x-kirocrew-managed": "skill-view", "x-kirocrew-agent": AGENT}),
        encoding="utf-8",
    )
    assert not (agents / f"{VIEW}.json").exists()
    return sidecar


def test_session_without_a_record_stored_with_a_view_name_resolves(
    monkeypatch, installed, view_sidecar
):
    bindings = _resume(monkeypatch, _cfg(), None, VIEW)

    assert bindings.requested_resolved
    assert bindings.kiro_agent == AGENT


@pytest.mark.parametrize("kind", ["template", "member"])
def test_recorded_view_selection_resolves_to_its_agent(monkeypatch, installed, view_sidecar, kind):
    # "member": a synced crewmate bound to the view, which the prune removes too.
    captured = _stored(template=VIEW, selection_name=VIEW, kind=kind)
    bindings = _resume(monkeypatch, _cfg(), captured, VIEW)

    assert bindings.requested_resolved
    assert bindings.selection_kind == "template"
    assert bindings.kiro_agent == AGENT
    assert bindings.memory_store_name == "default"
    assert bindings.execution_context == replace(captured, selection_kind="template")


def test_a_view_nothing_records_still_fails_closed(monkeypatch, installed, view_sidecar):
    view_sidecar.unlink()
    bindings = _resume(monkeypatch, _cfg(), _stored(template=VIEW, selection_name=VIEW), VIEW)

    assert not bindings.requested_resolved


def test_a_view_of_an_uninstalled_agent_still_fails_closed(monkeypatch, installed, view_sidecar):
    installed.clear()
    captured = _stored(template=VIEW, selection_name=VIEW, kind="template")
    bindings = _resume(monkeypatch, _cfg(), captured, VIEW)

    assert not bindings.requested_resolved


# ── A view the REAL boot drain retired still resolves ──
#
# The fixtures above hand-place a sidecar. These publish a real view, leave the
# tree as a pre-ledger build did (no ``view-sources.json``), and then run the
# real ``drain_stale_aliases``: every path by which it deletes a sidecar must
# keep the name resolvable, because the stored conversation is still on disk.


@pytest.fixture
def published_view(tmp_path, monkeypatch):
    """``(agents, view)``: a released view of AGENT whose pre-ledger tree has no ledger."""
    import gc
    from types import SimpleNamespace

    from kiro_crew.acp import skill_projection as projection

    monkeypatch.delenv("KIROCREW_NATIVE_SKILL_PROJECTION", raising=False)
    home = tmp_path / "kiro"
    agents = home / "agents"
    agents.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(projection, "_VIEW_SOURCES", {})
    monkeypatch.setattr(projection, "kiro_home", lambda: home)
    monkeypatch.setattr(projection, "data_home", lambda: tmp_path / "crew", raising=False)
    monkeypatch.setattr(projection, "kiro_agents_dir", lambda: agents)
    monkeypatch.setattr(projection.platform_compat, "path_volume_is_remote", lambda path: False)
    monkeypatch.setattr(projection.platform_compat, "first_linked_ancestor", lambda path: None)
    monkeypatch.setattr(projection, "_DRAIN_BATCH_PAUSE_SECS", 0)
    monkeypatch.setattr(
        "kiro_crew.agent.managed_mcp_spec_entry",
        lambda name: {"command": "test-core", "args": []},
    )
    monkeypatch.setattr("kiro_crew.agent._KIRO_MCP_JSON", home / "settings" / "mcp.json")
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name=AGENT, filename=f"{AGENT}.json", scope="global")],
    )
    # A skill resource makes it a search view, so the view carries kirocrew-core:
    # the mark the drain's re-serialized-pair rule needs to treat it as projected.
    (agents / f"{AGENT}.json").write_text(
        json.dumps(
            {
                "name": AGENT,
                "resources": ["skill://catalog/s/SKILL.md"],
                "mcpServers": {"injected": {"command": "helper", "env": {"TOKEN": "v1"}}},
            }
        ),
        encoding="utf-8",
    )
    prepared = projection.prepare_native_skill_projection(project)
    view = prepared.agent(AGENT)
    del prepared
    gc.collect()  # the lease goes and no projection in this process holds the view
    metadata = agents / projection._PROJECTION_METADATA_DIR_NAME
    # A build that predates the ledger never wrote it; the sidecar is the
    # only record of which agent the view was built from.
    (metadata / projection._VIEW_LEDGER_NAME).unlink()
    assert (agents / f"{view}.json").is_file() and (metadata / f"{view}.json").is_file()
    projection._VIEW_SOURCES.clear()  # a restart
    return agents, view


@pytest.mark.parametrize("before_drain", ["alias-removed", "pair-kept", "alias-rewritten"])
def test_a_view_the_real_boot_drain_retired_still_resumes(
    monkeypatch, installed, published_view, before_drain
):
    from kiro_crew.acp import skill_projection as projection

    agents, view = published_view
    alias = agents / f"{view}.json"
    sidecar = agents / projection._PROJECTION_METADATA_DIR_NAME / f"{view}.json"
    if before_drain == "alias-removed":
        # A cleanup outside the core (a distribution's boot cleanup, an operator)
        # took the alias and kept the sidecar: the drain's orphan sweep retires it.
        alias.unlink()
    elif before_drain == "alias-rewritten":
        # A launcher re-stamped an env value: the sidecar does not bind the
        # current bytes, and the drain retires the pair as a re-serialized alias.
        spec = json.loads(alias.read_text(encoding="utf-8"))
        spec["mcpServers"]["injected"]["env"]["TOKEN"] = "v2"
        alias.write_text(json.dumps(spec), encoding="utf-8")
    # "pair-kept": the drain reclaims alias and sidecar together.

    projection.drain_stale_aliases()

    # The drain really did delete the sidecar, the record the name resolves through.
    assert not alias.exists() and not sidecar.exists()
    projection._VIEW_SOURCES.clear()
    captured = _stored(template=view, selection_name=view, kind="template")
    bindings = _resume(monkeypatch, _cfg(), captured, view)

    assert bindings.requested_resolved
    assert bindings.kiro_agent == AGENT


def test_an_unrecoverable_view_tells_the_user_to_pick_again_or_start_new():
    from kiro_crew.dashboard.chat_runner import unavailable_agent_message

    message = unavailable_agent_message(VIEW)
    assert "Pick the agent for this chat again, or start a new chat" in message
    assert "Crew Member" not in message and "restore it" not in message
    assert unavailable_agent_message(AGENT) == (
        f"Crew Member '{AGENT}' is unavailable; restore it or choose a member"
    )
