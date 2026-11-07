"""An app's skill links changing clears every open skills loader's catalog of that root.

A loader serves its list from memory for ``_ITER_CACHE_TTL_SECS``; before the bridge
cleared it, ``GET /api/skills`` still listed an uninstalled app's skills, and missed a
just-enabled app's, for up to that TTL. Each test warms a real loader's list first, so
a stale answer is the cached one, and asserts on the very next read: no clock, no
sleep. The cold walk runs under a budget only a hung walk exhausts, and every loader
is closed and its refresh worker joined before the test returns.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest
from app_bridges_helpers import app_env_fixture  # noqa: F401
from app_bridges_helpers import _make_app_source

from kiro_crew import skills as skills_module
from kiro_crew.apps.bridges import _deregister_skills, _register_skills, reconcile_app_skills
from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.skill_runtime import catalog as catalog_module
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS


@pytest.fixture
def loaders(monkeypatch: pytest.MonkeyPatch):
    """Build loaders over a root; close them and join their workers on the way out."""
    monkeypatch.setattr(skills_module, "_COLD_CATALOG_WAIT_SECS", FALLBACK_TIMEOUT_SECS)
    # Every app here counts as enabled: the list cache, not the enabled gate, is
    # what these tests pin.
    monkeypatch.setattr(catalog_module, "_disabled_app_names", lambda: frozenset())
    built: list[SkillsLoader] = []

    def make(root: Path | None = None) -> SkillsLoader:
        loader = SkillsLoader(skills_path=root, install_builtins=False)
        built.append(loader)
        return loader

    yield make
    for loader in built:
        loader.close()
        worker = loader._catalog_worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=FALLBACK_TIMEOUT_SECS)
            assert not worker.is_alive(), "a refresh worker outlived its loader"


def _install(tmp_path: Path, home: Path) -> tuple[AppManifest, Path]:
    install_app(_make_app_source(tmp_path))
    app_root = home / "apps" / "test-app"
    return AppManifest.from_json_file(app_root / APP_MANIFEST_FILENAME), app_root


def _keys(loader: SkillsLoader) -> set[str]:
    return {name for name, _path, _within in loader._iter()}


def _lists_the_app_skill(loader: SkillsLoader) -> bool:
    """Whether the list names the app's skill, under its flat or its namespaced link."""
    return bool(_keys(loader) & {"my-skill", "test-app/my-skill"})


class TestAppSkillLinksClearTheCatalog:
    def test_a_registered_skill_is_listed_on_the_next_read(self, tmp_path, app_env, loaders):
        manifest, app_root = _install(tmp_path, app_env["home"])
        loader = loaders()
        assert not _lists_the_app_skill(loader)

        _register_skills("test-app", manifest, app_root)

        assert _lists_the_app_skill(loader)

    def test_a_deregistered_skill_is_gone_on_the_next_read(self, tmp_path, app_env, loaders):
        manifest, app_root = _install(tmp_path, app_env["home"])
        _register_skills("test-app", manifest, app_root)
        loader = loaders()
        assert _lists_the_app_skill(loader)

        _deregister_skills("test-app")

        assert not _lists_the_app_skill(loader)

    def test_an_unchanged_reconcile_keeps_the_catalog(self, tmp_path, app_env, loaders):
        """The boot reconcile re-links what is there; that must not cost a cold walk."""
        manifest, app_root = _install(tmp_path, app_env["home"])
        _register_skills("test-app", manifest, app_root)
        loader = loaders()
        assert _lists_the_app_skill(loader)
        generation = loader._catalog_generation

        reconcile_app_skills("test-app")

        assert loader._catalog_generation == generation
        links = json.loads((app_env["home"] / "skills" / ".app-skill-links.json").read_text())
        assert links["links"] == {
            "my-skill": {"app": "test-app"},
            "test-app/my-skill": {"app": "test-app"},
        }

    def test_only_loaders_of_that_root_are_cleared(self, tmp_path, app_env, loaders):
        other_root = tmp_path / "elsewhere" / "skills"
        other_root.mkdir(parents=True)
        mine, other = loaders(), loaders(other_root)
        assert _keys(mine) == set() and _keys(other) == set()
        before_mine, before_other = mine._catalog_generation, other._catalog_generation
        from kiro_crew import skill_live_loaders as live_loaders

        assert live_loaders.invalidate_loaders_of(app_env["home"] / "skills") >= 1

        assert mine._catalog_generation != before_mine
        assert other._catalog_generation == before_other

    def test_a_closed_loader_is_skipped(self, app_env, loaders):
        loader = loaders()
        loader.close()
        generation = loader._catalog_generation
        from kiro_crew import skill_live_loaders as live_loaders

        live_loaders.invalidate_loaders_of(app_env["home"] / "skills")

        assert loader._catalog_generation == generation

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs a POSIX FIFO")
    def test_a_fifo_at_the_links_name_is_replaced_not_waited_on(self, tmp_path, app_env, loaders):
        """A FIFO with no writer there must not stall the register that compares against it."""
        manifest, app_root = _install(tmp_path, app_env["home"])
        links_path = app_env["home"] / "skills" / ".app-skill-links.json"
        links_path.parent.mkdir(parents=True, exist_ok=True)
        os.mkfifo(links_path)
        loader = loaders()
        assert not _lists_the_app_skill(loader)

        worker = threading.Thread(
            target=_register_skills, args=("test-app", manifest, app_root), daemon=True
        )
        worker.start()
        worker.join(timeout=FALLBACK_TIMEOUT_SECS)
        if worker.is_alive():
            # Free the blocked reader so the thread does not outlive the test.
            os.close(os.open(links_path, os.O_WRONLY | os.O_NONBLOCK))
            worker.join(timeout=FALLBACK_TIMEOUT_SECS)
            pytest.fail("registering an app's skills blocked on a FIFO at the links name")

        assert links_path.is_file()
        assert json.loads(links_path.read_text())["links"]["test-app/my-skill"] == {
            "app": "test-app"
        }
        assert _lists_the_app_skill(loader)
