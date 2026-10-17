"""A project whose skills cannot load says why, instead of failing silently.

Covers the two silent refusals behind an empty project-skills list: a platform
without the no-follow directory-descriptor walk (Windows), and a path component
the walk refuses on POSIX (a symlinked directory, a file, a permission denial).
"""

from __future__ import annotations

import logging

import pytest

from kiro_crew import skill_trust
from kiro_crew import skills as skills_mod
from kiro_crew.skills import SkillsLoader

_LOGGER = "kiro_crew.skills"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    skill_trust.reset_cache_for_tests()
    _floor_monkeypatch.setattr(skills_mod, "_CHAIN_REFUSALS_WARNED", set())
    yield
    skill_trust.reset_cache_for_tests()


def _skill(root, name="genuine"):
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: d\n---\n\nBODY\n", encoding="utf-8"
    )


def _warnings(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


class TestUnsupportedPlatform:
    """Windows' branch: the gate refuses before any path is touched."""

    def _loader(self, tmp_path):
        return SkillsLoader(skills_path=tmp_path / "home-skills", install_builtins=False)

    def test_warns_once_naming_the_platform_limit(self, tmp_path, monkeypatch, caplog):
        project = tmp_path / "proj"
        _skill(project / ".kiro" / "skills")
        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: False)
        monkeypatch.setattr(skill_trust, "_project_skills_enabled", lambda: True)
        loader = self._loader(tmp_path)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            for _ in range(3):
                assert loader._iter(project) is not None
                loader._iter_cache = {}

        hits = [m for m in _warnings(caplog) if "not loaded on this platform" in m]
        assert len(hits) == 1, _warnings(caplog)

    def test_silent_when_the_operator_switched_the_feature_off(self, tmp_path, monkeypatch, caplog):
        project = tmp_path / "proj"
        _skill(project / ".kiro" / "skills")
        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: False)
        monkeypatch.setattr(skill_trust, "_project_skills_enabled", lambda: False)

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            self._loader(tmp_path)._iter(project)

        assert not [m for m in _warnings(caplog) if "not loaded on this platform" in m]

    def test_audit_names_the_platform_not_a_missing_grant(self, tmp_path, monkeypatch):
        calls: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kwargs):
                calls.append(kwargs)

        monkeypatch.setattr(skills_mod, "sel", lambda: _Sel())
        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: False)
        project = tmp_path / "proj"
        project.mkdir()

        self._loader(tmp_path)._iter(project)

        reasons = [c["reason"] for c in calls if c.get("rule") == "project_skills_trust_enforced"]
        assert reasons and "lacks no-follow directory traversal" in reasons[0], reasons

    def test_supported_platform_does_not_warn(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr(skill_trust, "project_skill_traversal_supported", lambda: True)
        monkeypatch.setattr(skill_trust, "_project_skills_enabled", lambda: True)
        project = tmp_path / "proj"
        project.mkdir()

        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            self._loader(tmp_path)._iter(project)

        assert not [m for m in _warnings(caplog) if "not loaded on this platform" in m]


@pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="the confined chain only runs with no-follow directory-descriptor traversal",
)
class TestChainRefusal:
    """POSIX branches of ``_open_project_dir_chain``: name the refused component."""

    def test_a_symlinked_component_is_named_once(self, tmp_path, caplog):
        real = tmp_path / "real-proj"
        _skill(real / ".kiro" / "skills")
        linked = tmp_path / "linked-proj"
        try:
            linked.symlink_to(real, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        base = linked / ".kiro" / "skills"

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            assert skills_mod._open_project_dir_chain(base) is None
            assert skills_mod._open_project_dir_chain(base) is None

        hits = [
            m for m in _warnings(caplog) if "'linked-proj' is a symlink or not a directory" in m
        ]
        assert len(hits) == 1, _warnings(caplog)
        assert str(base) in hits[0]

    def test_a_file_where_a_directory_belongs_is_named(self, tmp_path, caplog):
        project = tmp_path / "proj"
        (project / ".kiro").mkdir(parents=True)
        (project / ".kiro" / "skills").write_text("not a dir", encoding="utf-8")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            assert skills_mod._open_project_dir_chain(project / ".kiro" / "skills") is None

        assert [
            m for m in _warnings(caplog) if "'skills' is a symlink or not a directory" in m
        ], _warnings(caplog)

    def test_a_missing_skills_dir_stays_quiet(self, tmp_path, caplog):
        project = tmp_path / "proj"
        project.mkdir()

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            assert skills_mod._open_project_dir_chain(project / ".kiro" / "skills") is None

        assert _warnings(caplog) == []
        assert any("no '.kiro' component" in r.getMessage() for r in caplog.records)

    def test_a_real_tree_opens_and_logs_nothing(self, tmp_path, caplog):
        project = tmp_path / "proj"
        _skill(project / ".kiro" / "skills")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            fd = skills_mod._open_project_dir_chain(project / ".kiro" / "skills")
        assert fd is not None
        skills_mod.os.close(fd)
        assert _warnings(caplog) == []
