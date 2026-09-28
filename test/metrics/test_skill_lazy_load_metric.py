"""Tests for kirocrew.skill.lazy_load.duration histogram + count counter.

Verifies that SkillsLoader.load_skill() emits OTEL metrics on both cache-hit
(skill found) and cache-miss (skill not found) paths.
"""

from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.skills import SkillsLoader


class _CapturingRecorder:
    """Stand-in recorder that captures histogram() and counter() calls."""

    def __init__(self) -> None:
        self.histograms: list[tuple[str, float, dict]] = []
        self.counters: list[tuple[str, dict]] = []

    def histogram(self, name, value, *, unit="ms", attrs=None, **kwargs) -> None:
        self.histograms.append((name, value, dict(attrs or {})))

    def counter(self, name, attrs=None, **kwargs) -> None:
        self.counters.append((name, dict(attrs or {})))


@pytest.fixture()
def skill_dir(tmp_path: Path) -> Path:
    """Create a minimal skills directory with one skill."""
    skill = tmp_path / "test-skill" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Test Skill\nBody content here.", encoding="utf-8")
    return tmp_path


def test_load_skill_hit_emits_metric(skill_dir: Path):
    """A successful load emits both histogram and counter with hit=True."""
    mgr = SkillsLoader(skills_path=skill_dir, install_builtins=False)
    rec = _CapturingRecorder()

    with patch("kiro_crew.skills.get_recorder", return_value=rec):
        result = mgr.load_skill("test-skill")

    assert result is not None
    assert len(rec.histograms) == 1
    name, value_ms, attrs = rec.histograms[0]
    assert name == "kirocrew.skill.lazy_load.duration"
    assert value_ms >= 0
    assert attrs == {"hit": True}

    assert len(rec.counters) == 1
    cname, cattrs = rec.counters[0]
    assert cname == "kirocrew.skill.lazy_load.count"
    assert cattrs == {"hit": True}


def test_load_skill_miss_emits_metric(skill_dir: Path):
    """A miss (skill not found) emits both metrics with hit=False."""
    mgr = SkillsLoader(skills_path=skill_dir, install_builtins=False)
    rec = _CapturingRecorder()

    with patch("kiro_crew.skills.get_recorder", return_value=rec):
        result = mgr.load_skill("nonexistent-skill")

    assert result is None
    assert len(rec.histograms) == 1
    _, _, attrs = rec.histograms[0]
    assert attrs == {"hit": False}

    assert len(rec.counters) == 1
    _, cattrs = rec.counters[0]
    assert cattrs == {"hit": False}


def test_load_skill_invalid_name_no_metric(skill_dir: Path):
    """An invalid name (rejected by _safe_name) should NOT emit metrics."""
    mgr = SkillsLoader(skills_path=skill_dir, install_builtins=False)
    rec = _CapturingRecorder()

    with patch("kiro_crew.skills.get_recorder", return_value=rec):
        result = mgr.load_skill("../escape-attempt")

    assert result is None
    assert len(rec.histograms) == 0
    assert len(rec.counters) == 0
