"""A packaged skill copy of a former built-in, once that app is released onto its repository.

Core ships some apps' skills in ``builtin_skills/`` with an ``app: <name>`` line,
and the builtin sync copies them into the skills directory. Once the app is
adopted (``builtin_adoption.builtin_released``), the app's own repository ships
those skills under ``skills/<app>/``, so the core copy must neither show next to
them, nor stay after an uninstall, nor be rewritten by the sync into the
directory the adopted app's namespace shares.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from builtin_adoption_helpers import (
    NAME,
    PACKAGE,
    REPOSITORY,
    install_pins,
    pin_row,
    shipped_manifest,
    write_builtin_record,
    write_tree,
)
from skills_helpers import _settled

from kiro_crew import skills as skills_mod
from kiro_crew import skills_builtin_provenance as provenance_mod
from kiro_crew import skills_builtin_sync as sync_mod
from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.config.loader import KiroCrewConfig, SkillsConfig
from kiro_crew.skill_runtime import catalog as catalog_mod
from kiro_crew.skills import SkillsLoader

APP = "acme"
SKILL = f"{APP}-writing"


def _skill_md(directory: Path, name: str, *, app: str | None = None, body: str = "b") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    owner = f"app: {app}\n" if app else ""
    path = directory / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\n{owner}description: d\ntriggers: latex\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return path


def _loader(skills: Path) -> SkillsLoader:
    return _settled(
        SkillsLoader(
            skills_path=skills,
            install_builtins=False,
            config=KiroCrewConfig(skills=SkillsConfig(max_triggered=3)),
        )
    )


def _keys(loader: SkillsLoader) -> list[str]:
    return sorted(entry["key"] for entry in loader.list_skills())


@pytest.fixture
def released(monkeypatch):
    """Which apps the catalog reads as released; empty until a test fills it.

    The packaged tree claims ``SKILL`` for ``APP``, as ``builtin_skills/`` does.
    """
    names: set[str] = set()
    monkeypatch.setattr(catalog_mod, "_released_app_names", lambda: frozenset(names))
    monkeypatch.setattr(catalog_mod, "_packaged_app_claims", lambda root: {SKILL: APP})
    return names


@pytest.fixture
def skills(tmp_path, monkeypatch) -> Path:
    """A skills directory holding the core copy and the adopted app's namespaced copy."""
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    root = tmp_path / "skills"
    _skill_md(root / SKILL, SKILL, app=APP, body="core copy")
    _skill_md(root / APP / SKILL, SKILL, body="repository copy")
    return root


# --------------------------------------------------------------------------- #
# The catalog
# --------------------------------------------------------------------------- #


def test_the_core_copy_of_a_builtin_owned_app_stays_visible(skills, released):
    assert _keys(_loader(skills)) == sorted([f"{APP}/{SKILL}", SKILL])


def test_the_core_copy_of_a_released_app_is_hidden_beside_its_namespaced_copy(skills, released):
    released.add(APP)
    loader = _loader(skills)

    assert _keys(loader) == [f"{APP}/{SKILL}"]
    assert loader.get_triggered_skills("fix my latex") == [f"{APP}/{SKILL}"]
    assert [e.key for e in loader._scoped_entries(None, ["*"])] == [f"{APP}/{SKILL}"]


def test_the_core_copy_of_a_released_and_uninstalled_app_is_hidden(skills, released):
    for child in (skills / APP).iterdir():
        for f in child.iterdir():
            f.unlink()
        child.rmdir()
    (skills / APP).rmdir()
    released.add(APP)

    assert _keys(_loader(skills)) == []


def test_a_released_app_keeps_skills_whose_path_names_it(tmp_path, released, monkeypatch):
    """Only a frontmatter-only owner is a core copy: the app's own flat link resolves
    under the apps root and stays."""
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    app_skill = manager.apps_dir() / APP / "skills" / SKILL
    _skill_md(app_skill, SKILL, app=APP)
    root = tmp_path / "skills"
    root.mkdir()
    try:
        (root / SKILL).symlink_to(app_skill, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this platform")
    released.add(APP)

    assert _keys(_loader(root)) == [SKILL]


def test_the_cold_catalog_exact_read_withholds_a_released_copy(skills, released, monkeypatch):
    released.add(APP)
    loader = _loader(skills)
    monkeypatch.setattr(loader, "catalog_status", lambda project_dir=None: "building")

    assert loader._exact_read_while_building(SKILL, None, None, 10_000) is None
    assert "repository copy" in (
        loader._exact_read_while_building(f"{APP}/{SKILL}", None, None, 10_000) or ""
    )


def test_a_catalog_without_a_packaged_copy_reads_no_adoption_state(tmp_path, monkeypatch):
    """The per-message path asks nothing about adoption unless a packaged copy is listed."""
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    monkeypatch.setattr(catalog_mod, "_packaged_app_claims", lambda root: {SKILL: APP})
    asked: list[int] = []
    monkeypatch.setattr(catalog_mod, "_released_app_names", lambda: asked.append(1) or frozenset())
    root = tmp_path / "skills"
    _skill_md(root / "other", "other")
    loader = _loader(root)

    assert _keys(loader) == ["other"]
    assert loader.get_triggered_skills("fix my latex") == ["other"]
    assert asked == []


# --------------------------------------------------------------------------- #
# The released set, from real adoption state
# --------------------------------------------------------------------------- #


@pytest.fixture
def wheel(tmp_path, monkeypatch):
    """The adoption fixture app shipped in a wheel and pinned, with no record yet."""
    root = tmp_path / "wheel"
    write_tree(root / PACKAGE, shipped_manifest())
    monkeypatch.setattr(exec_mod, "_builtin_manifest_sources", lambda: (root.resolve(),))
    install_pins(monkeypatch, tmp_path / "pins.json", [pin_row("a" * 40, "b" * 64, "c" * 64)])
    yield
    pins_mod.load_adoption_pins.cache_clear()


def _adopt() -> None:
    record = manager._read_installed(NAME)
    assert record is not None
    record.source, record.origin, record.lifecycle = f"registry:{NAME}", "registry", "gateway"
    record.sourceUrl, record.sourceCommit = REPOSITORY, "a" * 40
    manager._write_installed(NAME, record)


def test_released_builtin_names_follows_the_record(wheel):
    write_builtin_record()
    assert adoption.released_builtin_names() == frozenset()
    _adopt()
    assert adoption.released_builtin_names() == frozenset({NAME})


def test_an_adopted_app_hides_its_core_copy_end_to_end(wheel, tmp_path, monkeypatch):
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    packaged = tmp_path / "packaged"
    _skill_md(packaged / f"{NAME}-writing", f"{NAME}-writing", app=NAME)
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", packaged)
    root = tmp_path / "skills"
    _skill_md(root / f"{NAME}-writing", f"{NAME}-writing", app=NAME)
    write_builtin_record()
    assert _keys(_loader(root)) == [f"{NAME}-writing"]

    _adopt()
    assert _keys(_loader(root)) == []


# --------------------------------------------------------------------------- #
# The builtin sync
# --------------------------------------------------------------------------- #


@pytest.fixture
def packaged(tmp_path, monkeypatch) -> Path:
    """A packaged-builtin source root shipping ``<app>/SKILL.md`` with ``app: <app>``."""
    root = tmp_path / "packaged"
    _skill_md(root / APP, APP, app=APP, body="packaged")
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", root)
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: None)
    return root


def _sync_released(monkeypatch, names: set[str]) -> None:
    """In both owners that bind it: the sync and the currency report."""
    for owner in (sync_mod, provenance_mod):
        monkeypatch.setattr(owner, "_released_skill_owners", lambda: frozenset(names))


def test_the_sync_installs_a_builtin_owned_apps_copy(packaged, tmp_path, monkeypatch):
    _sync_released(monkeypatch, set())
    base = tmp_path / "skills"
    skills_mod._ensure_builtin_skills(base)
    assert (base / APP / "SKILL.md").read_text(encoding="utf-8").endswith("packaged\n")


def test_the_sync_does_not_install_a_released_apps_copy(packaged, tmp_path, monkeypatch):
    _sync_released(monkeypatch, {APP})
    base = tmp_path / "skills"
    skills_mod._ensure_builtin_skills(base)
    assert not (base / APP / "SKILL.md").exists()


def test_the_sync_leaves_a_released_apps_shared_directory_alone(packaged, tmp_path, monkeypatch):
    """An update due on the copy whose destination is the adopted app's namespace dir
    would claim the whole directory, app skills included, into a user-backup."""
    base = tmp_path / "skills"
    _sync_released(monkeypatch, set())
    skills_mod._ensure_builtin_skills(base)
    namespaced = _skill_md(base / APP / SKILL, SKILL, body="repository copy")
    newer = (packaged / APP / "SKILL.md").stat().st_mtime + 60
    os.utime(packaged / APP / "SKILL.md", (newer, newer))
    _sync_released(monkeypatch, {APP})

    skills_mod._ensure_builtin_skills(base)

    assert namespaced.read_text(encoding="utf-8").endswith("repository copy\n")
    assert not list(base.glob(f".{APP}.user-backup*"))


@pytest.mark.skipif(os.name == "nt", reason="installed_skill_currency is POSIX-only")
def test_the_currency_report_skips_a_released_apps_copy(packaged, tmp_path, monkeypatch):
    """The doctor judges what the sync would install; a copy it never updates
    is not reported behind a package it will never install."""
    base = tmp_path / "skills"
    monkeypatch.setattr(skills_mod, "skills_dir", lambda: base)
    _sync_released(monkeypatch, set())
    skills_mod._ensure_builtin_skills(base)
    _skill_md(packaged / APP, APP, app=APP, body="packaged v2")

    def states() -> list[str]:
        return [e.state for e in skills_mod.installed_skill_currency() if e.name == APP]

    assert states() == [skills_mod.SKILL_INSTALL_BEHIND]
    _sync_released(monkeypatch, {APP})
    assert states() == []
