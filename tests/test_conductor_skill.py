"""Tests for kiro_crew.conductor_skill — conductor SKILL.md generation.

The conductor is now a STATIC delegation guide: the crew roster is resolved at
call time via the `select_crew` tool, not inlined here. So the skill neither
reads config nor lists individual crews.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def skills_loader(tmp_path):
    loader = MagicMock()
    loader._dir = tmp_path
    return loader


def _read_skill(tmp_path: Path) -> str:
    return (tmp_path / "conductor" / "SKILL.md").read_text(encoding="utf-8")


def _gen(skills_loader) -> str:
    from kiro_crew.conductor_skill import generate_conductor_skill

    generate_conductor_skill(skills_loader)
    return _read_skill(skills_loader._dir)


def test_points_at_select_crew(skills_loader):
    content = _gen(skills_loader)
    assert "select_crew" in content
    assert "spawn_run" in content


def test_has_always_true_and_delegation_guidelines(skills_loader):
    content = _gen(skills_loader)
    assert "always: true" in content
    assert "How to delegate" in content
    assert "When NOT to delegate" in content
    assert "Default behavior" in content


def test_does_not_inline_a_crew_roster(skills_loader):
    # The roster moved into select_crew; the always-on skill must not dump crews.
    content = _gen(skills_loader)
    assert "Available Crews" not in content
    assert "### " not in content


def test_is_static_and_stable(skills_loader):
    # No config read, so two generations are byte-identical.
    first = _gen(skills_loader)
    second = _gen(skills_loader)
    assert first == second
