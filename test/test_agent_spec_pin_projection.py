"""The kiro-cli skill view of a contested agent name is built from the pinned file.

``list_agents`` keeps one row per name, so the native projection asks the declared-name
resolver (``agent.spec_pins`` applied) which file a contested name's view is built from,
and builds none when the resolver refuses: kiro-cli is never left to choose a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import native_skill_projection_helpers
import pytest

from kiro_crew import agent_spec_pins

# The shared fixtures, bound here by name: pytest registers the fixtures from this namespace.
_legacy_alias = native_skill_projection_helpers._legacy_alias
_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)
native_tree = native_skill_projection_helpers.native_tree


@pytest.fixture
def pins(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    current: dict[str, str] = {}
    monkeypatch.setattr(agent_spec_pins, "spec_pins", lambda: dict(current))
    return current


def _write(directory: Path, file_name: str, **spec: object) -> Path:
    path = directory / file_name
    path.write_text(json.dumps(spec), encoding="utf-8")
    return path


@pytest.fixture
def contested_tree(native_tree, monkeypatch):
    from types import SimpleNamespace

    from kiro_crew.acp import skill_projection as projection

    home, agents, project = native_tree
    _write(agents, "PkgA-helper.json", name="helper", prompt="A", tools=["read"])
    _write(agents, "PkgB-helper.json", name="helper", prompt="B", tools=["read"])
    # The roster keeps one row per name, here the file the pin does NOT choose.
    monkeypatch.setattr(
        projection,
        "list_agents",
        lambda **kw: [SimpleNamespace(name="helper", filename="PkgA-helper.json", scope="global")],
    )
    return agents, project


def _view_prompt(agents: Path, alias: str) -> str:
    return json.loads((agents / f"{alias}.json").read_text(encoding="utf-8"))["prompt"]


def test_the_kiro_cli_view_is_built_from_the_pinned_file(contested_tree, pins):
    from kiro_crew.acp import skill_projection as projection

    agents, project = contested_tree
    pins["helper"] = "PkgB-helper.json"
    prepared = projection.prepare_native_skill_projection(project)
    alias = prepared.agent("helper")
    assert alias.startswith("kirocrew-") and alias != "helper"
    assert _view_prompt(agents, alias) == "B"


def test_no_pin_builds_no_kiro_cli_view_for_a_contested_name(contested_tree, pins):
    from kiro_crew.acp import skill_projection as projection

    _agents, project = contested_tree
    prepared = projection.prepare_native_skill_projection(project)
    with pytest.raises(Exception):
        prepared.agent("helper")


def test_a_pin_whose_file_is_gone_builds_no_view_of_the_file_left(contested_tree, pins):
    """With one file left, a pin naming another one still refuses: kiro-cli is
    never handed a view of a file the operator did not pin."""
    from kiro_crew.acp import skill_projection as projection

    agents, project = contested_tree
    pins["helper"] = "PkgB-helper.json"
    (agents / "PkgB-helper.json").unlink()
    prepared = projection.prepare_native_skill_projection(project)
    assert "helper" not in prepared.aliases
    assert "PkgB-helper.json" in prepared.errors["helper"]
    with pytest.raises(ValueError):
        prepared.agent("helper")
