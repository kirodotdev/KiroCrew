"""Readers outside the catalog match an app wildcard against a flat-link row.

An app skill is linked at ``<skills>/<app>/<skill>`` and at a flat ``<skills>/<skill>``.
The catalog lists a skill whose name sorts before its app's under the flat link, so
``<app>/*/SKILL.md`` only reaches it through ``_alias_selected``. The exact read
served while the catalog builds and the dashboard's "used by" annotation must apply
the same rule.
"""

from pathlib import Path

from kiro_crew.skills import SkillsLoader, skills_dir
from kiro_crew.testing.links import make_dir_link


def _app_case(tmp_path: Path, skills_root: Path) -> tuple[Path, Path, str]:
    """``<skills>/a-first`` and ``<skills>/app/a-first`` both link to one skill."""
    target = tmp_path / "bundle" / "a-first"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text(
        "---\nname: a-first\ndescription: d\n---\nBODY a-first\n", encoding="utf-8"
    )
    (skills_root / "app").mkdir(parents=True)
    make_dir_link(skills_root / "a-first", target)
    make_dir_link(skills_root / "app" / "a-first", target)
    return skills_root, target, str(skills_root / "app" / "*" / "SKILL.md")


def test_exact_read_while_building_admits_a_flat_row_through_the_app_wildcard(
    tmp_path, monkeypatch, opened
):
    from kiro_crew.skill_runtime import catalog as catalog_mod

    skills_root, _target, pattern = _app_case(tmp_path, tmp_path / "skills")
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    loader = opened(SkillsLoader(skills_path=skills_root, install_builtins=False))
    monkeypatch.setattr(loader, "catalog_status", lambda project_dir=None: "building")

    body = loader._exact_read_while_building("a-first", [pattern], None, 99_000)
    assert body is not None and "BODY a-first" in body
    other = str(skills_root / "other-app" / "*" / "SKILL.md")
    assert loader._exact_read_while_building("a-first", [other], None, 99_000) is None


def test_dashboard_annotation_names_an_agent_mapping_the_app_wildcard(tmp_path):
    from kiro_crew.dashboard.handlers._shared import (
        _agent_loads_skill,
        _agents_loading_skill,
        _expand_agent_globs,
    )

    skills_root, _target, _pattern = _app_case(tmp_path, skills_dir())
    flat_row = skills_root / "a-first" / "SKILL.md"
    agent_path = tmp_path / "a.json"
    specs = {
        "app": {"resources": [f"skill://{skills_root}/app/*/SKILL.md"]},
        "other": {"resources": [f"skill://{skills_root}/other-app/*/SKILL.md"]},
    }

    expanded = _expand_agent_globs([(n, s, agent_path) for n, s in specs.items()])

    assert _agents_loading_skill(flat_row, expanded) == ["app"]
    assert _agent_loads_skill(specs["app"], agent_path, flat_row) is True
    assert _agent_loads_skill(specs["other"], agent_path, flat_row) is False


def test_agent_scoped_filter_keeps_a_flat_row_through_the_app_wildcard(tmp_path):
    """The ``?agent=`` picker filter applies the annotation's alias fallback.

    The catalog keeps only an app skill's flat link when its name sorts before
    the app's, and that row carries no other spelling. An agent mapping the
    app's ``<app>/*/SKILL.md`` loads it at runtime, so the agent-scoped listing
    must keep it too, as the ``loaded_by_agents`` badge does.
    """
    from kiro_crew.dashboard.handlers._shared import skill_rows_for_globs

    skills_root, _target, _pattern = _app_case(tmp_path, skills_dir())
    rows = [
        {"key": "a-first", "source": "kirocrew", "path": str(skills_root / "a-first" / "SKILL.md")}
    ]

    kept = skill_rows_for_globs(rows, [f"{skills_root}/app/*/SKILL.md"])
    other = skill_rows_for_globs(rows, [f"{skills_root}/other-app/*/SKILL.md"])

    assert [r["key"] for r in kept] == ["a-first"]
    assert other == []


def test_dashboard_alias_never_resolves_a_row_outside_the_skills_root(tmp_path, monkeypatch):
    """A project row sharing a leaf with an app skill is not resolved or matched."""
    import os

    from kiro_crew.dashboard.handlers._shared import _agents_loading_skill
    from kiro_crew.skill_runtime import catalog as catalog_mod

    skills_root, _target, pattern = _app_case(tmp_path, skills_dir())
    project_row = tmp_path / "project" / ".kiro" / "skills" / "a-first" / "SKILL.md"
    project_row.parent.mkdir(parents=True)
    project_row.write_text("x", encoding="utf-8")
    resolved: list[str] = []
    real = os.path.realpath

    def spy(path, *args, **kwargs):
        resolved.append(os.path.abspath(path))
        return real(path, *args, **kwargs)

    assert _agents_loading_skill(project_row, [("a", [pattern])]) == []
    monkeypatch.setattr(catalog_mod.os.path, "realpath", spy)
    assert catalog_mod._alias_matches(project_row, [pattern], [skills_root]) is False
    assert os.path.abspath(project_row) not in resolved
