"""The skill-view ceiling grows with the authored agents in the directory.

One view per authored agent is the steady state, so a fixed ceiling trips on a
host with many agents before anything has leaked. The ceiling is the larger of
``SKILL_VIEW_PROJECTION_CEILING`` and two views per authored agent, and the
projection and the doctor size it the same way.
"""

from __future__ import annotations

import logging

import native_skill_projection_helpers
import pytest

from kiro_crew import agent_spec_format
from kiro_crew.acp import skill_projection as projection

_no_eager_prune = native_skill_projection_helpers._no_eager_prune
_prune_on_every_spawn = native_skill_projection_helpers._prune_on_every_spawn
_end_projections_before_tmp_path_goes = (
    native_skill_projection_helpers._end_projections_before_tmp_path_goes
)
native_tree = native_skill_projection_helpers.native_tree


@pytest.fixture
def _reset_ceiling_state(monkeypatch):
    monkeypatch.setattr(projection, "_CEILING_FALLBACK_WARNED", False)


def _write_authored(agents, count):
    for n in range(count):
        (agents / f"authored-{n}.json").write_text('{"name":"x"}', encoding="utf-8")


def _write_views(agents, count):
    for n in range(count):
        name = f"{agent_spec_format.NATIVE_SKILL_ALIAS_PREFIX}{n:024x}"
        (agents / f"{name}.json").write_text(f'{{"name":"{name}"}}', encoding="utf-8")


def test_the_ceiling_is_the_floor_for_few_agents_and_two_per_agent_above_it():
    floor = agent_spec_format.SKILL_VIEW_PROJECTION_CEILING
    assert agent_spec_format.skill_view_ceiling(0) == floor
    assert agent_spec_format.skill_view_ceiling(10) == floor
    assert agent_spec_format.skill_view_ceiling(103) == 206
    assert agent_spec_format.skill_view_ceiling(3, floor=4) == 6


def test_only_authored_specs_count_toward_the_ceiling(tmp_path):
    _write_authored(tmp_path, 3)
    _write_views(tmp_path, 5)
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")
    (tmp_path / "sub.json").mkdir()
    assert agent_spec_format.count_authored_agent_files(tmp_path) == 3


def test_markdown_specs_count_and_a_shadowed_pair_counts_once(tmp_path):
    (tmp_path / "md-only.md").write_text("---\nname: m\n---\n", encoding="utf-8")
    (tmp_path / "Upper.JSON").write_text('{"name":"u"}', encoding="utf-8")
    (tmp_path / "pair.json").write_text('{"name":"p"}', encoding="utf-8")
    (tmp_path / "pair.md").write_text("---\nname: p\n---\n", encoding="utf-8")
    assert agent_spec_format.count_authored_agent_files(tmp_path) == 3


def test_an_unreadable_directory_counts_no_agents(tmp_path):
    assert agent_spec_format.count_authored_agent_files(tmp_path / "missing") == 0


def test_many_agents_keep_projecting_past_the_floor(native_tree, monkeypatch, _reset_ceiling_state):
    """The reported host: one view per agent already sits near the fixed floor."""
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 3)
    _write_authored(agents, 3)  # 4 authored specs with custom.json: ceiling 8
    _write_views(agents, 5)

    prepared = projection.prepare_native_skill_projection(project)

    assert prepared is not None
    assert (agents / f"{prepared.agent('custom')}.json").exists()


def test_the_sized_ceiling_still_falls_back_once_reached(
    native_tree, monkeypatch, _reset_ceiling_state, caplog
):
    _home, agents, project = native_tree
    (agents / "custom.json").write_text('{"name":"custom"}', encoding="utf-8")
    monkeypatch.setattr(projection, "SKILL_VIEW_PROJECTION_CEILING", 3)
    _write_authored(agents, 3)
    _write_views(agents, 8)

    with caplog.at_level(logging.WARNING, logger=projection.logger.name):
        assert projection.prepare_native_skill_projection(project) is None

    fallback = [r for r in caplog.records if "fall back to authored agents" in r.message]
    assert fallback and "cap 8 reached" in fallback[0].getMessage()
