"""``agent.native_skill_projection``: a config switch for the native skill views.

With projection on, every spawn writes a ``kirocrew-skill-view-*`` view of every
agent it lists -- the operator's own kiro-cli agents included -- into the kiro
agents directory kiro-cli shares, and overlays
``chat.disableInheritingDefaultResources: true`` onto the workspace
``.kiro/settings/cli.json``, which a standalone kiro-cli then reads too.
``KIROCREW_NATIVE_SKILL_PROJECTION=0`` disables the same projection, but a
GUI-launched gateway never sees it. These tests pin what projection does to the
shared files and that ``agent.native_skill_projection: false`` takes the same
rollback path the environment variable does.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import native_skill_projection_helpers
import pytest

from kiro_crew.acp import skill_projection as projection
from kiro_crew.agent_spec_format import NATIVE_SKILL_ALIAS_PREFIX
from kiro_crew.config import loader as config_loader

_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)
native_tree = native_skill_projection_helpers.native_tree

_INHERIT = "chat.disableInheritingDefaultResources"


def _configure(monkeypatch, value):
    """Answer ``KiroCrewConfig.load()`` with *value* for the projection switch."""
    config = SimpleNamespace(agent=SimpleNamespace(native_skill_projection=value))
    monkeypatch.setattr(config_loader.KiroCrewConfig, "load", classmethod(lambda cls: config))


def _views(agents):
    return sorted(p.name for p in agents.iterdir() if p.name.startswith(NATIVE_SKILL_ALIAS_PREFIX))


def _operator_agent(agents):
    """An agent the operator wrote for kiro-cli, unrelated to Kiro Crew."""
    (agents / "custom.json").write_text(
        json.dumps({"name": "custom", "prompt": "You advise."}), encoding="utf-8"
    )


def test_projection_on_writes_into_the_shared_kiro_files(native_tree, monkeypatch):
    """The default: the operator's own agent gets a view, and the workspace gets a cli.json."""
    _home, agents, project = native_tree
    _configure(monkeypatch, True)
    _operator_agent(agents)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert len(_views(agents)) == 1
    view = json.loads((agents / _views(agents)[0]).read_text(encoding="utf-8"))
    assert view["prompt"] == "You advise."
    settings = json.loads((project / ".kiro/settings/cli.json").read_text(encoding="utf-8"))
    assert settings[_INHERIT] is True


def test_config_off_writes_nothing_and_reads_no_agent(native_tree, monkeypatch):
    _home, agents, project = native_tree
    _configure(monkeypatch, False)
    _operator_agent(agents)

    def unexpected(**kwargs):
        pytest.fail("disabled projection must not read authored agents")

    monkeypatch.setattr(projection, "list_agents", unexpected)

    assert projection.prepare_native_skill_projection(project) is None
    assert _views(agents) == []
    assert not (project / ".kiro").exists()


@pytest.mark.parametrize("original", [None, False])
def test_config_off_rolls_back_an_earlier_overlay(native_tree, monkeypatch, original):
    """Turning the switch off restores what the operator had before the overlay."""
    _home, agents, project = native_tree
    _operator_agent(agents)
    settings = project / ".kiro/settings/cli.json"
    if original is not None:
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({_INHERIT: original}), encoding="utf-8")
    _configure(monkeypatch, True)
    assert projection.prepare_native_skill_projection(project) is not None
    assert json.loads(settings.read_text(encoding="utf-8"))[_INHERIT] is True

    _configure(monkeypatch, False)
    assert projection.prepare_native_skill_projection(project) is None

    expected = {} if original is None else {_INHERIT: original}
    assert json.loads(settings.read_text(encoding="utf-8")) == expected


def test_environment_disable_wins_over_config_on(native_tree, monkeypatch):
    _home, agents, project = native_tree
    _configure(monkeypatch, True)
    _operator_agent(agents)
    monkeypatch.setenv("KIROCREW_NATIVE_SKILL_PROJECTION", "0")

    assert projection.prepare_native_skill_projection(project) is None
    assert _views(agents) == []


def test_a_config_load_that_raises_turns_projection_off(native_tree, monkeypatch):
    """The opt-out may be in the file that failed to load, so nothing is written."""
    _home, agents, project = native_tree
    _operator_agent(agents)

    def broken(cls):
        raise RuntimeError("config loader failed")

    monkeypatch.setattr(config_loader.KiroCrewConfig, "load", classmethod(broken))

    assert projection.prepare_native_skill_projection(project) is None
    assert _views(agents) == []
    assert not (project / ".kiro").exists()


def test_explicit_enabled_true_ignores_the_switch(native_tree, monkeypatch):
    """A warm set_mode refresh passes enabled=True and must not be refused by config."""
    _home, agents, project = native_tree
    _configure(monkeypatch, False)
    _operator_agent(agents)

    assert projection.prepare_native_skill_projection(project, enabled=True) is not None
