"""Regression tests for _resolve_skill_root AIM-key resolution."""

from unittest.mock import patch

import pytest

import kiro_crew.dashboard.handlers._shared as _shared


class _FakeState:
    def __init__(self):
        self._slots = {}


def _no_extra_paths():
    """Mock that prevents real config from leaking extra_paths into tests."""
    raise FileNotFoundError("no config in test")


@pytest.fixture(autouse=True)
def _isolate_config():
    with patch.object(_shared.KiroCrewConfig, "load", side_effect=_no_extra_paths):
        yield


def test_resolve_skill_root_resolves_aim_nested_key(tmp_path, monkeypatch):
    aim_root = tmp_path / "aim_skills"
    skill_dir = aim_root / "Pkg" / "my-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("# hi", encoding="utf-8")

    empty_kirocrew = tmp_path / "kirocrew_skills"
    empty_kirocrew.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_kirocrew)
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: aim_root)

    resolved = _shared._resolve_skill_root("Pkg/my-skill", _FakeState())
    assert resolved == skill_dir.resolve()


def test_resolve_skill_root_still_prefers_kirocrew_root(tmp_path, monkeypatch):
    mc_root = tmp_path / "kirocrew_skills"
    (mc_root / "local-skill").mkdir(parents=True)
    (mc_root / "local-skill" / "SKILL.md").write_text("# local", encoding="utf-8")
    aim_root = tmp_path / "aim_skills"
    aim_root.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: mc_root)
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: aim_root)

    resolved = _shared._resolve_skill_root("local-skill", _FakeState())
    assert resolved == (mc_root / "local-skill").resolve()


def test_resolve_skill_root_rejects_traversal(tmp_path, monkeypatch):
    aim_root = tmp_path / "aim_skills"
    aim_root.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "mc")
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: aim_root)

    assert _shared._resolve_skill_root("Pkg/../../etc", _FakeState()) is None
    assert _shared._resolve_skill_root("../etc", _FakeState()) is None
    assert _shared._resolve_skill_root("/etc/passwd", _FakeState()) is None


def test_resolve_skill_root_finds_skill_in_extra_paths(tmp_path, monkeypatch):
    extra_root = tmp_path / "extra_skills"
    (extra_root / "custom-skill").mkdir(parents=True)
    (extra_root / "custom-skill" / "SKILL.md").write_text("# custom", encoding="utf-8")

    empty_mc = tmp_path / "kirocrew_skills"
    empty_mc.mkdir()
    empty_aim = tmp_path / "aim_skills"
    empty_aim.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_mc)
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: empty_aim)

    class _FakeConfig:
        class skills:  # noqa: N801
            extra_paths = [str(extra_root)]

    with patch.object(_shared.KiroCrewConfig, "load", return_value=_FakeConfig()):
        resolved = _shared._resolve_skill_root("custom-skill", _FakeState())
    assert resolved == (extra_root / "custom-skill").resolve()


def test_resolve_skill_root_rejects_tilde_prefix(tmp_path, monkeypatch):
    # ``~`` is not caught by the top-level guard (which only checks ``/``),
    # so the else-branch must reject it before probing.
    monkeypatch.setattr(_shared, "skills_dir", lambda: tmp_path / "mc")
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: tmp_path / "aim")

    assert _shared._resolve_skill_root("~", _FakeState()) is None
    assert _shared._resolve_skill_root("~root/.ssh", _FakeState()) is None


def test_resolve_skill_root_extra_paths_take_precedence_over_aim(tmp_path, monkeypatch):
    # Same skill name in BOTH an extra path and the AIM root must resolve to
    # the extra path, matching SkillsLoader.load_skill() precedence
    # (kirocrew -> extra_paths -> aim).
    extra_root = tmp_path / "extra_skills"
    (extra_root / "dup-skill").mkdir(parents=True)
    (extra_root / "dup-skill" / "SKILL.md").write_text("# extra", encoding="utf-8")

    aim_root = tmp_path / "aim_skills"
    (aim_root / "dup-skill").mkdir(parents=True)
    (aim_root / "dup-skill" / "SKILL.md").write_text("# aim", encoding="utf-8")

    empty_mc = tmp_path / "kirocrew_skills"
    empty_mc.mkdir()
    monkeypatch.setattr(_shared, "skills_dir", lambda: empty_mc)
    monkeypatch.setattr(_shared, "aim_skills_dir", lambda: aim_root)

    class _FakeConfig:
        class skills:  # noqa: N801
            extra_paths = [str(extra_root)]

    with patch.object(_shared.KiroCrewConfig, "load", return_value=_FakeConfig()):
        resolved = _shared._resolve_skill_root("dup-skill", _FakeState())
    assert resolved == (extra_root / "dup-skill").resolve()
