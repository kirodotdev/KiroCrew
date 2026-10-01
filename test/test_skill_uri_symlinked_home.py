"""A ``skill://~/...`` mapping must select its skill when ``$HOME`` is a symlink.

``skills.extra_paths`` roots are stored resolved (they go through
``Path.resolve()``) while ``expand_skill_uri`` expands ``~`` from ``$HOME`` as
spelled, so the two sides of the scope match name the same file through
different paths unless the glob's literal prefix is canonicalized too.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kiro_crew.agent_discovery import expand_skill_uri
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.skills import SkillsLoader

needs_posix_symlinks = pytest.mark.skipif(
    not hasattr(os, "symlink") or os.name == "nt", reason="needs POSIX symlinks"
)


def _write_skill(root: Path, name: str) -> Path:
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\ndescription: {name} skill\n---\n# {name}\nBODY {name}\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def symlinked_home(tmp_path, monkeypatch):
    real_home = tmp_path / "data" / "home" / "alice"
    real_home.mkdir(parents=True)
    link_home = tmp_path / "home-alice"
    link_home.symlink_to(real_home, target_is_directory=True)
    monkeypatch.setenv("HOME", str(link_home))
    assert Path.home() == link_home
    return link_home, real_home


def _loader(tmp_path: Path, extra_paths: list[str], opened) -> SkillsLoader:
    cfg = KiroCrewConfig()
    cfg.skills.extra_paths = extra_paths
    return opened(
        SkillsLoader(skills_path=tmp_path / "builtin-skills", install_builtins=False, config=cfg)
    )


@needs_posix_symlinks
def test_tilde_mapping_selects_a_skill_in_a_resolved_catalog_root(tmp_path, symlinked_home, opened):
    link_home, _real_home = symlinked_home
    _write_skill(link_home / "my-skills", "demo")
    _write_skill(link_home / "my-skills", "other")
    loader = _loader(tmp_path, [str(link_home / "my-skills")], opened)
    glob = expand_skill_uri("skill://~/my-skills/demo/SKILL.md", tmp_path / "a.json")
    assert glob is not None

    rows = loader.scoped_skills(only=[glob])

    assert [row["name"] for row in rows] == ["demo"]
    assert "BODY demo" in (loader.read_scoped_skill(rows[0]["key"], only=[glob]) or "")


@needs_posix_symlinks
def test_tilde_mapping_still_selects_in_a_session_with_an_unrelated_project(
    tmp_path, symlinked_home, opened
):
    link_home, _real_home = symlinked_home
    _write_skill(link_home / "my-skills", "demo")
    project = tmp_path / "proj"
    project.mkdir()
    loader = _loader(tmp_path, [str(link_home / "my-skills")], opened)
    glob = expand_skill_uri("skill://~/my-skills/demo/SKILL.md", tmp_path / "a.json")

    rows = loader.scoped_skills(project_dir=project, only=[glob])

    assert [row["name"] for row in rows] == ["demo"]


@needs_posix_symlinks
def test_tilde_wildcard_mapping_keeps_matching_through_the_symlink(
    tmp_path, symlinked_home, opened
):
    link_home, _real_home = symlinked_home
    _write_skill(link_home / "my-skills", "alpha")
    _write_skill(link_home / "my-skills", "beta")
    loader = _loader(tmp_path, [str(link_home / "my-skills")], opened)
    glob = expand_skill_uri("skill://~/my-skills/*/SKILL.md", tmp_path / "a.json")

    names = sorted(row["name"] for row in loader.scoped_skills(only=[glob]))

    assert names == ["alpha", "beta"]


@needs_posix_symlinks
def test_tilde_mapping_outside_every_catalog_root_is_still_mapped(tmp_path, symlinked_home, opened):
    link_home, _real_home = symlinked_home
    _write_skill(link_home / "loose", "solo")
    loader = _loader(tmp_path, [], opened)
    glob = expand_skill_uri("skill://~/loose/solo/SKILL.md", tmp_path / "a.json")

    rows = loader.scoped_skills(only=[glob])

    assert [row["name"] for row in rows] == ["solo"]
    assert "BODY solo" in (loader.read_scoped_skill(rows[0]["key"], only=[glob]) or "")


@needs_posix_symlinks
def test_tilde_mapping_admits_an_exact_read_while_the_catalog_builds(
    tmp_path, symlinked_home, monkeypatch, opened
):
    link_home, _real_home = symlinked_home
    _write_skill(link_home / "my-skills", "demo")
    loader = _loader(tmp_path, [str(link_home / "my-skills")], opened)
    glob = expand_skill_uri("skill://~/my-skills/demo/SKILL.md", tmp_path / "a.json")
    monkeypatch.setattr(loader, "catalog_status", lambda project_dir=None: "building")

    body = loader._exact_read_while_building("demo", [glob], None, 99_000)

    assert body is not None and "BODY demo" in body


def test_a_unc_mapping_glob_is_never_resolved_outside_the_fence(tmp_path, monkeypatch, opened):
    import kiro_crew.skills as skills_module
    from kiro_crew.hooks import is_unc_shape

    real_validate = skills_module.validate_file_path
    real_realpath = os.path.realpath

    def windows_gate(raw: str) -> str | None:
        # The Windows gate as validate_file_path applies it: an untrusted UNC
        # spelling is refused before any resolution. On Windows the real gate
        # runs instead of this stand-in.
        return None if is_unc_shape(raw) else real_validate(raw)

    def probing_realpath(path, *args, **kwargs):
        if is_unc_shape(os.fspath(path)):
            pytest.fail(f"realpath ran on a UNC spelling: {path!r}")
        return real_realpath(path, *args, **kwargs)

    if os.name != "nt":
        monkeypatch.setattr(skills_module, "validate_file_path", windows_gate)
    monkeypatch.setattr(os.path, "realpath", probing_realpath)
    glob = "//attacker/share/*/SKILL.md"
    loader = _loader(tmp_path, [], opened)

    assert skills_module._with_canonical_globs([glob]) == [glob]
    assert loader.scoped_skills(only=[glob]) == []


@needs_posix_symlinks
def test_a_project_mapping_and_the_project_root_are_never_resolved(tmp_path, monkeypatch, opened):
    import kiro_crew.skills as skills_module

    project = tmp_path / "proj"
    (project / ".kiro" / "skills").mkdir(parents=True)
    resolved: list[str] = []
    real_validate = skills_module.validate_file_path

    def recording_validate(raw: str) -> str | None:
        resolved.append(raw)
        return real_validate(raw)

    monkeypatch.setattr(skills_module, "validate_file_path", recording_validate)
    project_glob = str(project / ".kiro" / "skills" / "*" / "SKILL.md")
    outside_glob = str(tmp_path / "elsewhere" / "*" / "SKILL.md")
    loader = _loader(tmp_path, [], opened)

    assert skills_module._canonical_glob(project_glob, project) == project_glob
    loader.scoped_skills(project_dir=project, only=[project_glob, outside_glob])

    project_root = os.path.abspath(project)
    assert not [raw for raw in resolved if os.path.abspath(raw).startswith(project_root)]


@needs_posix_symlinks
def test_a_project_mapping_selects_through_a_symlinked_project_spelling(tmp_path, opened):
    from kiro_crew import skill_trust

    if not skill_trust.project_skill_traversal_supported():
        pytest.skip("project skill traversal unsupported on this platform")
    real_project = tmp_path / "data" / "proj"
    _write_skill(real_project / ".kiro" / "skills", "demo")
    link_project = tmp_path / "proj-link"
    link_project.symlink_to(real_project, target_is_directory=True)
    skill_trust.grant_project_trust(link_project)
    loader = _loader(tmp_path, [], opened)
    glob = expand_skill_uri(
        "skill://.kiro/skills/*/SKILL.md", tmp_path / "a.json", project_dir=link_project
    )
    assert glob is not None and glob.startswith(str(link_project))

    rows = loader.scoped_skills(project_dir=link_project, only=[glob])

    assert [row["name"] for row in rows] == ["demo"]


@needs_posix_symlinks
def test_a_resolved_prefix_holding_glob_characters_matches_only_itself(tmp_path, monkeypatch):
    import kiro_crew.skills as skills_module

    target = tmp_path / "skills[x]"
    _write_skill(target, "demo")
    _write_skill(tmp_path / "skillsx", "demo")
    link = tmp_path / "linked"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(skills_module, "validate_file_path", os.path.realpath)

    canonical = skills_module._canonical_glob(str(link / "*" / "SKILL.md"))

    assert skills_module._matches_any(str(target / "demo" / "SKILL.md"), [canonical])
    assert not skills_module._matches_any(
        str(tmp_path / "skillsx" / "demo" / "SKILL.md"), [canonical]
    )


def test_loader_roots_are_resolved_once_however_many_mappings(tmp_path, monkeypatch, opened):
    import kiro_crew.skills as skills_module

    loader = _loader(tmp_path, [], opened)
    seen: list[str] = []
    real_validate = skills_module.validate_file_path

    def recording_validate(raw: str) -> str | None:
        seen.append(raw)
        return real_validate(raw)

    monkeypatch.setattr(skills_module, "validate_file_path", recording_validate)
    globs = [str(tmp_path / f"elsewhere{n}" / "*" / "SKILL.md") for n in range(5)]
    loader.scoped_skills(only=globs)

    assert seen.count(str(loader._dir)) == 1


@needs_posix_symlinks
def test_the_dashboard_annotation_agrees_with_the_loader(tmp_path, symlinked_home):
    from kiro_crew.dashboard.handlers._shared import (
        _agent_loads_skill,
        _agents_loading_skill,
        _expand_agent_globs,
    )

    link_home, real_home = symlinked_home
    _write_skill(link_home / "my-skills", "demo")
    resolved_skill = real_home / "my-skills" / "demo" / "SKILL.md"
    spec = {"resources": ["skill://~/my-skills/*/SKILL.md"]}
    agent_path = tmp_path / "a.json"

    expanded = _expand_agent_globs([("a", spec, agent_path)])

    assert _agents_loading_skill(resolved_skill, expanded) == ["a"]
    assert _agent_loads_skill(spec, agent_path, resolved_skill) is True
