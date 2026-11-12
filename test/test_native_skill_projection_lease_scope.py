"""A projection leases only the views of the agents its caller launches.

Every projection still publishes a view for every authored agent, but a session
that runs one agent must not pin the view of every other agent. Before this, an
edit to N agents while sessions were open left N old views on disk per open
session, which filled the 128-view ceiling and sent new sessions to their
authored agents without kirocrew-core.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import native_skill_projection_helpers

from kiro_crew.acp import skill_projection as projection
from kiro_crew.acp.runtime_mode_activation import _leased_agents

# The shared fixtures, bound here by name so pytest registers them for this module.
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)
native_tree = native_skill_projection_helpers.native_tree


def _two_agents(monkeypatch):
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [
            SimpleNamespace(name=name, filename=f"{name}.json", scope="global")
            for name in ("alpha", "beta")
        ],
    )


def _lease_records():
    lease_dir = projection._projection_lease_dir()
    return [
        json.loads(p.read_bytes())["aliases"]
        for p in lease_dir.glob(f"*{projection._PROJECTION_LEASE_RECORD_SUFFIX}")
    ]


def test_a_scoped_projection_leases_only_its_agents_view(native_tree, monkeypatch):
    _two_agents(monkeypatch)
    _home, agents, project = native_tree
    (agents / "alpha.json").write_text('{"name":"alpha"}', encoding="utf-8")
    (agents / "beta.json").write_text('{"name":"beta"}', encoding="utf-8")

    prepared = projection.prepare_native_skill_projection(project, lease_agents=("alpha",))

    assert prepared is not None
    assert set(prepared.aliases) >= {"alpha", "beta"}, "every agent still gets a view"
    assert (agents / f"{prepared.aliases['beta']}.json").exists()
    assert _lease_records() == [[prepared.aliases["alpha"]]]
    assert prepared.leased_aliases == frozenset({prepared.aliases["alpha"]})
    assert prepared.aliases["beta"] not in projection._active_aliases()


def test_an_edited_agents_old_view_is_not_pinned_by_an_open_session(native_tree, monkeypatch):
    """The reported leak: a session open across an edit of ANOTHER agent."""
    _two_agents(monkeypatch)
    _home, agents, project = native_tree
    (agents / "alpha.json").write_text('{"name":"alpha"}', encoding="utf-8")
    beta = agents / "beta.json"
    beta.write_text('{"name":"beta","prompt":"v1"}', encoding="utf-8")
    open_session = projection.prepare_native_skill_projection(project, lease_agents=("alpha",))
    old_beta = agents / f"{open_session.aliases['beta']}.json"

    beta.write_text('{"name":"beta","prompt":"v2"}', encoding="utf-8")
    later = projection.prepare_native_skill_projection(project, lease_agents=("alpha",))

    assert later.aliases["beta"] != old_beta.stem
    assert not old_beta.exists(), "the open alpha session still pinned beta's old view"
    assert (agents / f"{open_session.aliases['alpha']}.json").exists()
    assert (agents / f"{later.aliases['beta']}.json").exists()


def test_an_unscoped_projection_still_leases_every_view(native_tree, monkeypatch):
    """``lease_agents=None`` keeps the old contract for any caller that needs it."""
    _two_agents(monkeypatch)
    _home, agents, project = native_tree
    (agents / "alpha.json").write_text('{"name":"alpha"}', encoding="utf-8")
    beta = agents / "beta.json"
    beta.write_text('{"name":"beta","prompt":"v1"}', encoding="utf-8")
    held = projection.prepare_native_skill_projection(project)
    old_beta = agents / f"{held.aliases['beta']}.json"

    beta.write_text('{"name":"beta","prompt":"v2"}', encoding="utf-8")
    projection.prepare_native_skill_projection(project)

    assert held.leased_aliases == frozenset(held.aliases.values())
    assert old_beta.exists()


def test_a_carried_lease_follows_an_edited_agent_to_its_new_view(native_tree, monkeypatch):
    """set_mode carries the replaced projection's leased agents by NAME. A start
    for alpha still in flight when alpha is edited retries with alpha's new
    view, so that view must be leased; alpha's old view is not carried."""
    _two_agents(monkeypatch)
    _home, agents, project = native_tree
    alpha = agents / "alpha.json"
    alpha.write_text('{"name":"alpha","prompt":"v1"}', encoding="utf-8")
    (agents / "beta.json").write_text('{"name":"beta"}', encoding="utf-8")
    first = projection.prepare_native_skill_projection(project, lease_agents=("alpha",))
    assert _leased_agents(first) == ("alpha",)

    alpha.write_text('{"name":"alpha","prompt":"v2"}', encoding="utf-8")
    second = projection.prepare_native_skill_projection(
        project, lease_agents=("beta", *_leased_agents(first))
    )

    assert second.aliases["alpha"] != first.aliases["alpha"]
    assert second.leased_aliases == frozenset({second.aliases["alpha"], second.aliases["beta"]})
    assert first.aliases["alpha"] not in second.leased_aliases


def test_no_projection_carries_no_agents():
    assert _leased_agents(None) == ()
