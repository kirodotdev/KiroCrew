"""A packaged skill copy of a former built-in, once that app is released onto its repository.

Core ships some apps' skills in ``builtin_skills/`` with an ``app: <name>`` line,
and the builtin sync copies them into the skills directory. Once the app is
adopted (``builtin_adoption.builtin_released``), the app's own repository ships
those skills under ``skills/<app>/``, so the core copy must neither show next to
them, nor stay after an uninstall, nor be rewritten by the sync into the
directory the adopted app's namespace shares. Once core deletes its copy from
``builtin_skills/``, a host's installed copy stays withheld (the recorded table
still names its app) and a host that never had it gets none.
"""

from __future__ import annotations

import json
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


# --------------------------------------------------------------------------- #
# A copy that has left the package
# --------------------------------------------------------------------------- #

LEFT = f"{NAME}-writing"


def _record_skill_apps(monkeypatch, path: Path, text: str) -> None:
    """Point the recorded table at a file holding *text*; the fixture clears the cache after."""
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(provenance_mod, "_SKILL_APPS_PATH", path)
    provenance_mod._recorded_skill_apps.cache_clear()


@pytest.fixture
def recorded_table():
    """Leave no fixture table cached for the next test, whichever way this one ends."""
    yield
    provenance_mod._recorded_skill_apps.cache_clear()


@pytest.fixture
def left_package(tmp_path, monkeypatch, recorded_table) -> Path:
    """``builtin_skills/`` once the app's copy is deleted from it: it ships ``other`` only,
    and the recorded table still names ``LEFT``'s app."""
    root = tmp_path / "packaged"
    _skill_md(root / "other", "other")
    monkeypatch.setattr(skills_mod, "_BUILTIN_SKILLS_DIR", root)
    monkeypatch.setattr(skills_mod, "_project_skills_dir", lambda: None)
    monkeypatch.setattr(catalog_mod, "_disabled_app_names", lambda: frozenset())
    _record_skill_apps(monkeypatch, tmp_path / "apps.json", json.dumps({"skills": {LEFT: NAME}}))
    return root


def test_an_adopted_apps_old_copy_stays_hidden_once_the_package_drops_it(
    wheel, left_package, tmp_path
):
    """The copy a host installed before stays withheld: no packaged skill claims ``LEFT``
    any more, and the recorded table still names its app."""
    root = tmp_path / "skills"
    _skill_md(root / LEFT, LEFT, app=NAME, body="core copy")
    _skill_md(root / NAME / LEFT, LEFT, body="repository copy")
    write_builtin_record()
    assert _keys(_loader(root)) == sorted([f"{NAME}/{LEFT}", LEFT])

    _adopt()
    loader = _loader(root)

    assert _keys(loader) == [f"{NAME}/{LEFT}"]
    assert loader.get_triggered_skills("fix my latex") == [f"{NAME}/{LEFT}"]
    assert [e.key for e in loader._scoped_entries(None, ["*"])] == [f"{NAME}/{LEFT}"]


@pytest.mark.parametrize("released", [set(), {NAME}], ids=["not-installed", "installed"])
def test_a_fresh_host_gets_no_copy_of_an_app_whose_copy_left_the_package(
    left_package, tmp_path, monkeypatch, released
):
    """Deleted rather than kept, the copy reaches no host that never had it: the sync
    installs only what the package ships, whether or not the app is installed."""
    _sync_released(monkeypatch, released)
    monkeypatch.setattr(catalog_mod, "_released_app_names", lambda: frozenset(released))
    base = tmp_path / "skills"

    skills_mod._ensure_builtin_skills(base)

    assert sorted(p.name for p in base.iterdir() if not p.name.startswith(".")) == ["other"]
    assert _keys(_loader(base)) == ["other"]


def test_every_packaged_skill_naming_an_app_is_recorded_with_that_app():
    """A packaged copy can be deleted only once the table holds its app, so every one
    the package ships is in it already."""
    shipped = catalog_mod._packaged_app_claims(str(skills_mod._BUILTIN_SKILLS_DIR))
    recorded = provenance_mod._recorded_skill_apps()

    assert recorded
    assert {key: recorded.get(key) for key in shipped} == shipped


def test_every_recorded_app_has_a_pin_row():
    """Only a pinned former built-in can be released, so a row for any other app is a mistake."""
    pinned = pins_mod.parse_adoption_pins(
        json.loads(pins_mod._PINS_PATH.read_text(encoding="utf-8"))
    )

    assert set(provenance_mod._recorded_skill_apps().values()) <= set(pinned)


def test_the_recorded_table_ships_with_the_package():
    setup = (Path(__file__).resolve().parents[1] / "setup.cfg").read_text(encoding="utf-8")
    assert "data/packaged_skill_apps.json" in setup


@pytest.mark.parametrize(
    "text",
    ["not json", "[]", '{"skills": []}', '{"other": {}}'],
    ids=["unparsable", "not-an-object", "skills-not-an-object", "no-skills"],
)
def test_an_unusable_table_records_nothing(tmp_path, monkeypatch, recorded_table, text):
    _record_skill_apps(monkeypatch, tmp_path / "apps.json", text)
    assert provenance_mod._recorded_skill_apps() == {}


def test_a_malformed_row_is_dropped_and_the_rest_kept(tmp_path, monkeypatch, recorded_table):
    rows = {"ok": "app", "a/b": "app", "nested": "a/b", "empty": "", "": "app", "n": 3, " k": "app"}
    _record_skill_apps(monkeypatch, tmp_path / "apps.json", json.dumps({"skills": rows}))
    assert provenance_mod._recorded_skill_apps() == {"ok": "app"}
