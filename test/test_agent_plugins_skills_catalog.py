"""The skills of ``on`` Agent Plugins reach the skill catalog, and nothing else does.

Each test builds a powers home and a data home under ``tmp_path`` and drives the
real loader, so discovery, the owner's record, the trusted roots and the catalog
walk run end to end.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from agent_plugins_helpers import install, skill, symlink

from kiro_crew.agent_plugins import consent, skill_roots
from kiro_crew.skills import SkillsLoader, _trusted_skill_roots


@pytest.fixture
def env(tmp_path, monkeypatch):
    powers = tmp_path / "powers"
    crew = tmp_path / "crew"
    (crew / "skills").mkdir(parents=True)
    monkeypatch.setenv("KIRO_POWERS_HOME", str(powers))
    monkeypatch.setenv("KIROCREW_HOME", str(crew))
    monkeypatch.setattr(skill_roots, "_cache", None)
    return powers, crew


@pytest.fixture
def loader(env):
    _powers, crew = env
    built = SkillsLoader(skills_path=crew / "skills", install_builtins=False)
    yield built
    built.close()


def _plugin(powers: Path, name: str, *skills: str, body: str = "Body.\n") -> Path:
    files = {
        f"skills__{s}__SKILL.md": skill(s, f"{name} {s} helper").replace("Body.\n", body)
        for s in skills
    }
    return install(powers, name, **files)


def _turn_on(name: str) -> None:
    consent.set_enabled(name, True)


def _keys(loader: SkillsLoader) -> set[str]:
    loader._invalidate_iter_cache()
    report = loader.search_skills_report("", limit=500, browse=True)
    return {match["key"] for match in report.matches}


def test_an_on_plugins_skill_is_found_under_its_namespace(env, loader):
    powers, _crew = env
    root = _plugin(powers, "demo", "alpha", body="Plugin alpha body.\n")
    _turn_on("demo")

    assert "plugin-demo/alpha" in _keys(loader)
    found = loader.search_skills_report("demo alpha helper", limit=5)
    assert [m["key"] for m in found.matches] == ["plugin-demo/alpha"]
    page = loader.read_scoped_skill_page("plugin-demo/alpha")
    assert "Plugin alpha body." in page.content
    # Only that plugin's resolved skills/ directory is trusted.
    trusted = _trusted_skill_roots()
    assert os.path.realpath(root / "skills") in trusted
    assert os.path.realpath(powers / "installed") not in trusted
    assert os.path.realpath(root) not in trusted


def test_an_off_plugin_adds_no_skill_and_no_root(env, loader):
    powers, _crew = env
    root = _plugin(powers, "demo", "alpha")

    assert not {k for k in _keys(loader) if k.startswith("plugin-")}
    assert os.path.realpath(root / "skills") not in _trusted_skill_roots()
    assert loader.load_skill("plugin-demo/alpha") is None


def test_editing_an_on_plugins_skill_keeps_it_served(env, loader):
    powers, _crew = env
    root = _plugin(powers, "demo", "alpha", body="Version one.\n")
    _turn_on("demo")
    assert "Version one." in loader.read_scoped_skill_page("plugin-demo/alpha").content

    path = root / "skills" / "alpha" / "SKILL.md"
    path.write_text(
        skill("alpha", "demo alpha helper").replace("Body.\n", "Version two.\n"), encoding="utf-8"
    )

    assert "plugin-demo/alpha" in _keys(loader)
    assert os.path.realpath(root / "skills") in _trusted_skill_roots()
    assert "Version two." in loader.read_scoped_skill_page("plugin-demo/alpha").content


def test_a_link_into_an_off_plugins_skills_is_rejected(env, loader):
    powers, crew = env
    root = _plugin(powers, "demo", "alpha")
    symlink(crew / "skills" / "borrowed", root / "skills" / "alpha", directory=True)

    assert "borrowed" not in _keys(loader)
    assert loader.read_scoped_skill_page("borrowed").__class__.__name__ == "SkillReadRefusal"
    # The same link is admitted once the owner turns the plugin on, so the
    # refusal above is the plugin's state and not the link's shape.
    _turn_on("demo")
    assert {"borrowed", "plugin-demo/alpha"} <= _keys(loader)


def test_two_on_plugins_with_one_skill_name_do_not_collide(env, loader):
    powers, _crew = env
    _plugin(powers, "first", "alpha", body="First body.\n")
    _plugin(powers, "second", "alpha", body="Second body.\n")
    _turn_on("first")
    _turn_on("second")

    assert {"plugin-first/alpha", "plugin-second/alpha"} <= _keys(loader)
    assert "First body." in loader.read_scoped_skill_page("plugin-first/alpha").content
    assert "Second body." in loader.read_scoped_skill_page("plugin-second/alpha").content


def test_turning_a_plugin_off_removes_its_skills_at_the_next_refresh(env, loader):
    powers, _crew = env
    root = _plugin(powers, "demo", "alpha")
    _turn_on("demo")
    assert "plugin-demo/alpha" in _keys(loader)

    consent.set_enabled("demo", False)
    assert "plugin-demo/alpha" not in _keys(loader)
    assert os.path.realpath(root / "skills") not in _trusted_skill_roots()


def test_one_bad_plugin_does_not_hide_another(env, loader):
    powers, _crew = env
    install(powers, "broken", manifest="{not json", **{"skills__x__SKILL.md": skill("x")})
    _plugin(powers, "demo", "alpha")
    _turn_on("demo")

    assert "plugin-demo/alpha" in _keys(loader)


def test_a_discovery_failure_leaves_the_rest_of_the_catalog(env, loader, monkeypatch):
    powers, crew = env
    _plugin(powers, "demo", "alpha")
    _turn_on("demo")
    own = crew / "skills" / "mine" / "SKILL.md"
    own.parent.mkdir()
    own.write_text(skill("mine"), encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise RuntimeError("discovery broke")

    monkeypatch.setattr("kiro_crew.agent_plugins.discovery.discover_plugins", boom)
    assert skill_roots.on_plugin_skill_roots() == ()
    keys = _keys(loader)
    assert "mine" in keys
    assert "plugin-demo/alpha" not in keys


def test_the_default_agents_native_view_serves_the_plugin_skill(env, loader):
    """The native view drops ``skill://`` and serves skills through ``skill_search``.

    The default agent maps no skill, so its search scope is the whole catalog,
    the plugin's skill included.
    """
    from kiro_crew.acp.skill_projection import project_agent_view
    from kiro_crew.agent_discovery import session_skill_globs
    from kiro_crew.config.paths import data_home

    powers, crew = env
    _plugin(powers, "demo", "alpha")
    _turn_on("demo")
    spec = {"name": "kirocrew", "prompt": "p", "resources": []}
    view, search, refusal = project_agent_view(
        spec,
        crew / "kirocrew.json",
        "kirocrew",
        crew_home_id=data_home().absolute().as_posix(),
        inherits_skills=False,
        per_session_element=False,
    )
    assert refusal is None and view is not None
    assert search is True
    only = session_skill_globs("", "kirocrew")
    assert only is None
    loader._invalidate_iter_cache()
    report = loader.search_skills_report("", limit=500, only=only, browse=True)
    assert "plugin-demo/alpha" in {m["key"] for m in report.matches}
