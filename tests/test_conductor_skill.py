"""Tests for kiro_crew.conductor_skill — conductor SKILL.md generation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@dataclass
class _FakeAgent:
    name: str
    description: str = ""
    filename: str = ""
    model: str = ""
    skills: list[str] = field(default_factory=list)
    mcp_servers: list[str] = field(default_factory=list)
    source: str = "builtin"
    package: str = ""


@pytest.fixture()
def skills_loader(tmp_path):
    loader = MagicMock()
    loader._dir = tmp_path
    return loader


def _read_skill(tmp_path: Path) -> str:
    return (tmp_path / "conductor" / "SKILL.md").read_text(encoding="utf-8")


@patch("kiro_crew.conductor_skill.list_agents")
@patch("kiro_crew.conductor_skill.load_all")
@patch("kiro_crew.conductor_skill.load")
@patch("kiro_crew.conductor_skill.save")
def test_includes_agents_from_metadata(mock_save, mock_load, mock_load_all, mock_list, skills_loader):
    from kiro_crew.conductor_skill import generate_conductor_skill

    mock_list.return_value = [
        _FakeAgent(name="code-reviewer", description="Reviews code"),
    ]
    mock_load.return_value = "Use for CR reviews and security audits."
    mock_load_all.return_value = {"code-reviewer": "Use for CR reviews and security audits."}
    generate_conductor_skill(skills_loader)
    content = _read_skill(skills_loader._dir)
    assert "code-reviewer" in content
    assert "Use for CR reviews and security audits." in content


@patch("kiro_crew.conductor_skill.list_agents")
@patch("kiro_crew.conductor_skill.load_all")
@patch("kiro_crew.conductor_skill.load")
@patch("kiro_crew.conductor_skill.save")
def test_auto_seeds_metadata_from_description(mock_save, mock_load, mock_load_all, mock_list, skills_loader):
    from kiro_crew.conductor_skill import generate_conductor_skill

    mock_list.return_value = [
        _FakeAgent(name="code-reviewer", description="Reviews code quality"),
    ]
    mock_load.return_value = ""  # no metadata file
    mock_load_all.return_value = {}
    generate_conductor_skill(skills_loader)
    mock_save.assert_called_once_with("code-reviewer", "Reviews code quality")


@patch("kiro_crew.conductor_skill.list_agents")
@patch("kiro_crew.conductor_skill.load_all")
@patch("kiro_crew.conductor_skill.load")
@patch("kiro_crew.conductor_skill.save")
def test_excludes_kirocrew_and_conductor(mock_save, mock_load, mock_load_all, mock_list, skills_loader):
    from kiro_crew.conductor_skill import generate_conductor_skill

    mock_list.return_value = [
        _FakeAgent(name="kirocrew", description="General"),
        _FakeAgent(name="kirocrew-conductor", description="Conductor"),
        _FakeAgent(name="code-reviewer", description="Reviews code"),
    ]
    mock_load.return_value = ""
    mock_load_all.return_value = {}
    generate_conductor_skill(skills_loader)
    content = _read_skill(skills_loader._dir)
    assert "kirocrew-conductor" not in content
    # kirocrew should not appear as a heading in roster
    assert "### kirocrew" not in content
    assert "### code-reviewer" in content


@patch("kiro_crew.conductor_skill.list_agents")
@patch("kiro_crew.conductor_skill.load_all")
@patch("kiro_crew.conductor_skill.load")
@patch("kiro_crew.conductor_skill.save")
def test_skill_has_always_true_and_delegation_guidelines(mock_save, mock_load, mock_load_all, mock_list, skills_loader):
    from kiro_crew.conductor_skill import generate_conductor_skill

    mock_list.return_value = [_FakeAgent(name="code-reviewer", description="Reviews code")]
    mock_load.return_value = ""
    mock_load_all.return_value = {}
    generate_conductor_skill(skills_loader)
    content = _read_skill(skills_loader._dir)
    assert "always: true" in content
    assert "When to delegate" in content
    assert "When NOT to delegate" in content
    assert "Effort scaling" in content
    assert "spawn_run" in content
