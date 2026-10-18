"""A project whose skills cannot load says why, instead of failing silently.

Covers the silent refusals behind a project skill that never loads: a platform
without the no-follow directory-descriptor walk (Windows); a path component the
walk refuses on POSIX (a symlinked ``.kiro/skills`` directory, a file, a
permission denial); and a linked entry directly under a real ``.kiro/skills``,
which the no-follow walk skips and, without this warning, drops without a word.
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
    # Both the chain-refusal and the linked-entry warnings dedup through the one
    # _CHAIN_REFUSALS_WARNED set. It is a mirror-routed name (the skills facade
    # re-binds it), so it must be reset THROUGH the facade -- a direct owner patch
    # would let the facade's undo leak into the family (test_facade_mirror_guards).
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


@pytest.mark.skipif(
    not skill_trust.project_skill_traversal_supported(),
    reason="the confined walk only runs with no-follow directory-descriptor traversal",
)
class TestLinkedSkillEntry:
    """A linked entry directly under a real ``.kiro/skills`` is named, not dropped."""

    def _linked_skill_project(self, tmp_path):
        """A real ``.kiro/skills`` with one real skill and one linked skill dir."""
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        target = project / ".agents" / "dsop"
        _skill(target.parent, name="dsop")  # creates .agents/dsop/SKILL.md
        try:
            (skills_dir / "dsop").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")
        return project, skills_dir

    def _entry_hits(self, caplog):
        return [m for m in _warnings(caplog) if "linked entr" in m]

    def test_warns_once_per_project_root_and_still_loads_real_skills(self, tmp_path, caplog):
        project, skills_dir = self._linked_skill_project(tmp_path)

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            first = skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))
            second = skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        # The real skill still loads; the linked one is skipped (not followed).
        assert [name for name, _ in first] == ["realskill"]
        assert first == second

        hits = self._entry_hits(caplog)
        assert len(hits) == 1, _warnings(caplog)
        assert str(skills_dir / "dsop") in hits[0]
        assert "replace each link with a real directory" in hits[0]

    def test_several_links_are_all_named_in_one_warning(self, tmp_path, caplog):
        """Every skipped top-level link is named in the single per-root WARNING."""
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        agents = project / ".agents"
        for name in ("alpha", "beta", "gamma"):
            _skill(agents, name=name)
        try:
            for name in ("alpha", "beta", "gamma"):
                (skills_dir / name).symlink_to(agents / name, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            loaded = skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        assert [name for name, _ in loaded] == ["realskill"]
        hits = self._entry_hits(caplog)
        assert len(hits) == 1, _warnings(caplog)
        for name in ("alpha", "beta", "gamma"):
            assert str(skills_dir / name) in hits[0]

    def test_a_linked_plain_file_is_named_generically(self, tmp_path, caplog):
        """A top-level symlink to a FILE is a skipped linked entry, named the same way."""
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        real_file = project / ".agents" / "shared-skill-md"
        real_file.parent.mkdir(parents=True)
        real_file.write_text("x", encoding="utf-8")
        try:
            (skills_dir / "linked.md").symlink_to(real_file)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        hits = self._entry_hits(caplog)
        assert len(hits) == 1, _warnings(caplog)
        # The message must NOT claim the entry is a directory.
        assert "linked entr" in hits[0] and "linked skill directory" not in hits[0]

    def test_a_dangling_link_is_still_named(self, tmp_path, caplog):
        """A dangling top-level link is skipped and named from its own lstat (no resolution)."""
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        try:
            (skills_dir / "gone").symlink_to(
                project / ".agents" / "missing", target_is_directory=True
            )
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        assert len(self._entry_hits(caplog)) == 1, _warnings(caplog)

    def test_a_linked_subdir_inside_a_loaded_skill_stays_quiet(self, tmp_path, caplog):
        """A skill's own internal link (myskill/references -> shared) is not a lost skill."""
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="myskill")
        shared = project / ".agents" / "shared-docs"
        shared.mkdir(parents=True)
        (shared / "note.md").write_text("x", encoding="utf-8")
        try:
            (skills_dir / "myskill" / "references").symlink_to(shared, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            loaded = skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        assert [name for name, _ in loaded] == ["myskill"]
        assert self._entry_hits(caplog) == []

    def test_a_dot_link_stays_quiet(self, tmp_path, caplog):
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        target = project / ".agents" / "dsop"
        _skill(target.parent, name="dsop")
        try:
            (skills_dir / ".hidden").symlink_to(target, target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        assert self._entry_hits(caplog) == []

    def test_a_real_only_tree_stays_quiet(self, tmp_path, caplog):
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        assert self._entry_hits(caplog) == []

    def test_a_nested_linked_skill_md_does_not_load(self, tmp_path, caplog):
        """A real nested skill dir whose own SKILL.md is a symlink does not load.

        The warn-only change names a linked TOP-level child; a file-level link
        deeper in the tree is out of its scope. This pins only the load outcome
        (the walk keeps regular SKILL.md files, not links), not the logging.
        """
        project = tmp_path / "proj"
        skills_dir = project / ".kiro" / "skills"
        _skill(skills_dir, name="realskill")
        (skills_dir / "nested").mkdir()
        real_md = project / ".agents" / "nested-skill-md"
        real_md.parent.mkdir(parents=True)
        real_md.write_text("name: nested\n", encoding="utf-8")
        try:
            (skills_dir / "nested" / "SKILL.md").symlink_to(real_md)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable on this platform")

        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            loaded = skills_mod._iter_skill_files(skills_dir, confine_to=(str(project),))

        # The nested skill with the linked SKILL.md does not load.
        assert [name for name, _ in loaded] == ["realskill"]
