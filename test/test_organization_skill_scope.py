"""A Kiro profile's organization skills stay in every resolved skill scope.

Kiro Enterprise Management syncs a profile's skills to
``~/.kiro/cloud-cache/resolved/config/organization/skills``, and kiro-cli and KAS
load them into every session. Crew's skill scope must keep them: an inheriting
agent gets them with the default trees, a mapped agent beside its own mapping, and
on KAS the skill-scope deny rule must not take them away.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_crew import agent_discovery
from kiro_crew.acp import kas_skill_scope


def _skill(root: Path, name: str) -> Path:
    folder = root / name
    folder.mkdir(parents=True)
    path = folder / "SKILL.md"
    path.write_text(f"---\nname: {name}\ndescription: {name} skill\n---\nBody\n", encoding="utf-8")
    return path


@pytest.fixture
def homes(tmp_path, monkeypatch):
    """A fake ``$HOME`` with one global skill and one organization skill."""
    home = tmp_path / "home"
    kiro = home / ".kiro"
    _skill(kiro / "skills", "deploy-helper")
    org = kiro / "cloud-cache" / "resolved" / "config" / "organization" / "skills"
    _skill(org, "code-review")
    # The suite's organization-folder isolation follows a faked home.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr("kiro_crew.config.paths.kiro_home", lambda: kiro)
    monkeypatch.setattr(
        "kiro_crew.agent_sdk.drivers.acp.inherits_default_resources", lambda project_dir: True
    )
    return kiro, org


def test_organization_glob_is_empty_without_a_synced_profile(homes, tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "nobody"))
    assert agent_discovery.organization_skill_globs() == []


def test_an_inheriting_agent_gets_the_organization_skills(homes):
    _, org = homes
    assert str(org / "*" / "SKILL.md") in agent_discovery.inherited_skill_globs(None)


def test_a_mapped_agent_keeps_the_organization_skills_beside_its_mapping(homes, tmp_path):
    _, org = homes
    mapped = str(tmp_path / "mine" / "*" / "SKILL.md")
    globs = agent_discovery.mate_conductor_scope("custom", {}, [mapped], None)
    assert globs == [mapped, str(org / "*" / "SKILL.md")]


def test_an_unmapped_scope_stays_empty(homes):
    # ``[]`` means "no mapping" (the whole catalog for a default agent), so the
    # organization glob must not turn it into a narrow scope.
    assert agent_discovery.mate_conductor_scope("custom", {}, [], None) == []


def test_kas_deny_rule_keeps_the_organization_skills_of_a_mapped_agent(homes, tmp_path):
    mine = _skill(tmp_path / "mine", "my-skill")
    globs = agent_discovery.mate_conductor_scope("custom", {}, [str(mine)], None)
    (rule,) = kas_skill_scope.skill_scope_rules(globs)
    assert rule["effect"] == "deny"
    assert rule["exclude"] == ["code-review", "my-skill"]


def test_kas_default_trees_include_the_organization_tree(homes):
    _, org = homes
    assert str(org / "*" / "SKILL.md") in kas_skill_scope._default_tree_globs(None)
